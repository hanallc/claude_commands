"""Test deployment recovery with private settings and a fake AWS transport."""
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import aws_trial, cloud_gate, deploy, ledger, plan_identity, remote
from scripts.aws_target.config import deployment_record, read_budget_notification_address, save_deployment_record
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import install_cost_settings, override_fixture_settings
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


HOME = 'us-east-2'
FALLBACK = 'us-west-2'
STACK = 'recover-fixture'
REGIONAL_STACK = f'{STACK}-gates-{FALLBACK}'
DEPLOYMENT = 'ab' * 16
REVISION = 'cd' * 20
FINGERPRINT = 'ef' * 32
OWNER = plan_identity.PlanAccount('aws', '7' * 12)
SCALER = 'fixture-scaler'
SCALER_ARN = OWNER.iam(f'user/{SCALER}')
SCALER_ID = 'fixture-private-user'
BUCKET = 'fixture-private-bucket'
ROLE = 'fixture-private-role'
GATE_PROFILE = OWNER.iam('instance-profile/fixture-profile')
PLAN_CREDENTIAL = plan_identity.PlanCredentials('AKIA' + 'X' * 16, 'x' * 40, OWNER)


class DeploymentRecoveryTests(unittest.TestCase):
    settings: Path = Path()
    state: Path = Path()
    foundation: Path = Path()
    manifest: Path = Path()
    output: Path = Path()

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.calls: list[tuple[str, str, str]] = []
        self.missing: set[str] = set()
        self.statuses: dict[str, str] = {}
        self.regional_deployment: str = DEPLOYMENT
        self.clock: TestClock = TestClock()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = root / 'recovery-settings.conf'
        self.state = root / 'state'
        self.foundation = self.state / 'deployment.json'
        self.manifest = self.state / 'gate-deployment.json'
        self.output = root / 'output'
        write_text_atomic(self.output / 'template.json', '{}\n')
        write_text_atomic(self.output / 'parameters.json', json.dumps([
            {'ParameterKey': 'DeploymentId', 'ParameterValue': DEPLOYMENT}]) + '\n')
        write_text_atomic(self.output / 'preflight.json', remote.record_text({'at': 0, 'fingerprint': FINGERPRINT}))
        write_text_atomic(self.settings, f'region={HOME}\nstack_name={STACK}\ndeployment_timeout_s=3\n')
        gates = root / 'recovery-gates.conf'
        write_text_atomic(gates, f'regions={HOME} {FALLBACK}\n')
        gate_settings = conf.read(gates)
        _ = self.enterContext(mock.patch.object(conf, 'read_or_repository', return_value=gate_settings))
        _ = self.enterContext(mock.patch.object(plan_identity, 'plan_credentials', return_value=PLAN_CREDENTIAL))
        _ = self.enterContext(mock.patch.object(aws_trial, 'host_nixpkgs_revision', return_value=REVISION))

    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.output, execute=self.execute, clock=self.clock,
                                 notify=lambda _event: False)

    def configure_preparation(self) -> None:
        root = self.state.parent
        notification = root / 'notification-email'
        write_text_atomic(notification, 'fixture@example.invalid\n')
        install_cost_settings(self.settings)
        override_fixture_settings(self.settings,
            f'region={HOME}\navailability_zone={HOME}a\nstack_name={STACK}\ndeployment_timeout_s=3\n'
            + f'scaler_user_name={SCALER}\nnotification_email_file={notification}\nadmin_secret_file={root / "admin.gpg"}\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ,
            {'AWS_SHARED_CREDENTIALS_FILE': str(root / 'credentials')})))

    @staticmethod
    def stack_arn(region: str, name: str) -> str:
        return f'arn:aws:cloudformation:{region}:{OWNER.account}:stack/{name}/fixture-private-stack'

    def stack(self, region: str, name: str) -> dict[str, object]:
        outputs = {'GateLaunchTemplate': f'lt-fixture-{region}', 'GateLaunchVersion': '4',
                   'GateSecurityGroup': f'sg-fixture-{region}'}
        parameters = {'VpcId': f'vpc-fixture-{region}', 'GateImage': f'ami-fixture-{region}'}
        if region == HOME:
            outputs.update(ScalerUserName=SCALER, BudgetName='fixture-private-budget', GateBucket=BUCKET,
                           GateRoleId=ROLE, GateProfileArn=GATE_PROFILE)
            parameters.update(DeploymentId=DEPLOYMENT, GateInstanceTypes='c8a.8xlarge, m8a.8xlarge',
                              GateGpuInstanceTypes='g6.8xlarge')
        else:
            parameters.update(DeploymentId=self.regional_deployment, HomeGateProfileArn=GATE_PROFILE)
        return {'StackName': name, 'StackId': self.stack_arn(region, name),
                'StackStatus': self.statuses.get(region, 'UPDATE_COMPLETE'),
                'Parameters': [{'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()],
                'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertTrue(capture_output and text)
        self.assertFalse(check)
        self.assertEqual(timeout, 3)
        self.assertIsNotNone(env)
        assert env is not None
        self.assertEqual(env['AWS_ACCESS_KEY_ID'], PLAN_CREDENTIAL.access_key_id)
        self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'], '/dev/null')
        self.assertEqual(arguments[:2], ['aws', '--region'])
        region, service, operation, option, value = arguments[2:7]
        self.assertIn(region, (HOME, FALLBACK))
        self.assertEqual(arguments[7:], ['--output', 'json'])
        self.calls.append((region, service, operation))
        reply: dict[str, object]
        match service, operation, option:
            case 'cloudformation', 'describe-stacks', '--stack-name':
                self.assertEqual(value, STACK if region == HOME else REGIONAL_STACK)
                if region in self.missing:
                    error = f'An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id {value} does not exist'
                    return subprocess.CompletedProcess(arguments, 1, '', error)
                reply = {'Stacks': [self.stack(region, value)]}
            case 'cloudformation', 'describe-stack-resources', '--stack-name':
                self.assertEqual((region, value), (HOME, self.stack_arn(HOME, STACK)))
                reply = {'StackResources': [{'LogicalResourceId': 'ScalerUser', 'PhysicalResourceId': SCALER,
                                             'ResourceType': 'AWS::IAM::User'}]}
            case 'iam', 'get-user', '--user-name':
                self.assertEqual((region, value), (HOME, SCALER))
                reply = {'User': {'Arn': SCALER_ARN, 'UserId': SCALER_ID}}
            case 'ec2', 'describe-route-tables', '--filters':
                self.assertEqual(value, f'Name=vpc-id,Values=vpc-fixture-{region}')
                reply = {'RouteTables': [{'Associations': [{'Main': True}], 'Routes': [
                    {'DestinationCidrBlock': '0.0.0.0/0', 'State': 'active', 'GatewayId': 'igw-fixture'}]}]}
            case 'ec2', 'describe-subnets', '--filters':
                self.assertEqual(value, f'Name=vpc-id,Values=vpc-fixture-{region}')
                reply = {'Subnets': [{'SubnetId': f'subnet-fixture-{region}', 'AvailabilityZone': f'{region}a',
                                      'State': 'available'}]}
            case _:
                self.fail(f'Unexpected AWS call: {operation}')
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def recover(self, *options: str) -> tuple[int, str]:
        output, errors = StringIO(), StringIO()
        arguments = ['deploy.py', 'recover', '--settings', str(self.settings), '--directory', str(self.state), *options]
        with (mock.patch.object(sys, 'argv', arguments), mock.patch.object(deploy, 'Deployment', return_value=self.deployment()),
              redirect_stdout(output), redirect_stderr(errors)):
            status = deploy.main()
        printed = output.getvalue() + errors.getvalue()
        for private in (OWNER.account, 'arn:', DEPLOYMENT, FINGERPRINT, SCALER_ID, BUCKET, ROLE, 'regions',
                        'subnet-fixture', 'sg-fixture', 'ami-fixture', 'fixture-private-budget'):
            self.assertNotIn(private, printed)
        return status, printed

    def test_creates_both_records_from_live_stack_values(self) -> None:
        status, output = self.recover()
        self.assertEqual(status, 0, output)
        self.assertIn(f'nixpkgs revision from host lock {aws_trial.NIXOS_LOCK} is {REVISION}.', output)
        home = self.stack(HOME, STACK)
        self.assertEqual(deployment_record(self.state), {'account_id': OWNER.account, 'region': HOME, 'stack': STACK,
            'stack_id': self.stack_arn(HOME, STACK), 'deployment_id': DEPLOYMENT, 'budget_name': 'fixture-private-budget',
            'fingerprint': FINGERPRINT,
            'scaler_user': {'name': SCALER, 'arn': SCALER_ARN, 'user_id': SCALER_ID},
            'outputs': {row['OutputKey']: row['OutputValue'] for row in ledger.rows(home, 'Outputs')}})
        self.assertEqual(cloud_gate.read(self.manifest), {'deployment_id': DEPLOYMENT, 'revision': REVISION,
            'instance_types': ['c8a.8xlarge', 'm8a.8xlarge'], 'gpu_instance_types': ['g6.8xlarge'],
            'bucket': BUCKET, 'artifact_prefix': f'artifacts/{ROLE}:',
            'regions': [{'region': region, 'image': f'ami-fixture-{region}', 'template': f'lt-fixture-{region}',
                'subnets': [{'id': f'subnet-fixture-{region}', 'zone': f'{region}a'}],
                'security_group': f'sg-fixture-{region}', 'version': '4',
                **({'stack': REGIONAL_STACK} if region == FALLBACK else {})} for region in (HOME, FALLBACK)]})
        self.assertEqual(self.calls, [(HOME, 'cloudformation', 'describe-stacks'),
            (HOME, 'cloudformation', 'describe-stack-resources'), (HOME, 'iam', 'get-user'),
            (HOME, 'ec2', 'describe-route-tables'), (HOME, 'ec2', 'describe-subnets'),
            (FALLBACK, 'cloudformation', 'describe-stacks'), (FALLBACK, 'ec2', 'describe-route-tables'),
            (FALLBACK, 'ec2', 'describe-subnets')])

    def test_matching_records_stay_unchanged(self) -> None:
        self.assertEqual(self.recover()[0], 0)
        with mock.patch.object(deploy, 'publish_recovery_records') as publish:
            status, output = self.recover('--write')
        self.assertEqual(status, 0, output)
        self.assertEqual(output.count('already matches'), 2)
        publish.assert_not_called()

    def test_foundation_equals_the_record_written_by_apply(self) -> None:
        self.configure_preparation()
        identity: dict[str, object] = {'account_id': OWNER.account, 'region': HOME, 'stack': STACK,
            'deployment_id': DEPLOYMENT, 'stack_id': self.stack_arn(HOME, STACK),
            'scaler_user': {'name': SCALER, 'arn': SCALER_ARN, 'user_id': SCALER_ID}}
        previous = {**identity, 'outputs': {'RetainedOutput': 'fixture-retained'}, 'fingerprint': 'fixture-old'}
        save_deployment_record(self.state, previous)
        outputs = {ledger.text(row, 'OutputKey'): ledger.text(row, 'OutputValue')
                   for row in ledger.rows(self.stack(HOME, STACK), 'Outputs')}
        subject = self.deployment()
        prepared = subject.prepare()
        with (mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs'),
              mock.patch.object(subject, '_stack', return_value=outputs), mock.patch.object(subject, '_deploy'),
              mock.patch.object(subject, 'scaler_key')):
            self.assertEqual(subject.apply(approve_resources=True), outputs)
        applied = self.foundation.read_bytes()
        self.assertEqual(deployment_record(self.state), {**identity, 'fingerprint': prepared.fingerprint,
            'budget_name': outputs['BudgetName'], 'outputs': {**outputs, 'RetainedOutput': 'fixture-retained'}})
        save_deployment_record(self.state, previous)
        status, output = self.recover('--write')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.foundation.read_bytes(), applied)

    def test_recovery_rebuilds_preparation_for_apply_after_a_wipe(self) -> None:
        self.configure_preparation()
        subject = self.deployment()
        prepared = subject.prepare()
        provisional_id = prepared.parameters['DeploymentId']
        self.assertNotEqual(provisional_id, DEPLOYMENT)
        self.assertEqual(deployment_record(self.state)['deployment_id'], provisional_id)
        files = (self.foundation, self.output / 'template.json', self.output / 'parameters.json',
                 self.output / 'preflight.json')
        original = [path.read_bytes() for path in files]
        self.clock.advance(1)

        status, output = self.recover()
        self.assertEqual(status, 1, output)
        self.assertIn('The preparation bundle has a different deployment ID.', output)
        self.assertNotIn(provisional_id, output)
        self.assertEqual([path.read_bytes() for path in files], original)
        self.assertFalse(self.manifest.exists())

        replace_file = os.replace

        def fail_manifest(source: Path, target: Path) -> None:
            if target == self.manifest:
                raise OSError('fixture manifest replacement failure')
            replace_file(source, target)

        with mock.patch.object(os, 'replace', side_effect=fail_manifest):
            status, output = self.recover('--write')
        self.assertEqual(status, 1, output)
        self.assertEqual([path.read_bytes() for path in files], original)
        self.assertFalse(self.manifest.exists())

        status, output = self.recover('--write')
        self.assertEqual(status, 0, output)
        written = cast(object, json.loads((self.output / 'parameters.json').read_text()))
        self.assertEqual(deploy.stack_parameter_values({'Parameters': written}),
                         {**prepared.parameters, 'DeploymentId': DEPLOYMENT})
        preflight = remote.read_record(self.output / 'preflight.json')
        recovered = deployment_record(self.state)
        self.assertEqual(recovered['deployment_id'], DEPLOYMENT)
        self.assertEqual(recovered['fingerprint'], preflight['fingerprint'])
        self.assertNotEqual(recovered['fingerprint'], prepared.fingerprint)
        self.assertEqual(preflight['at'], self.clock.now)
        self.assertGreater(ledger.instant(preflight, 'at'), prepared.at)

        outputs = {ledger.text(row, 'OutputKey'): ledger.text(row, 'OutputValue')
                   for row in ledger.rows(self.stack(HOME, STACK), 'Outputs')}
        recovered_files = [path.read_bytes() for path in files]
        with (mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs'),
              mock.patch.object(subject, '_stack', return_value=outputs), mock.patch.object(subject, '_deploy'),
              mock.patch.object(subject, 'scaler_key')):
            self.assertEqual(subject.apply(approve_resources=True), outputs)
        self.assertEqual([path.read_bytes() for path in files], recovered_files)

    def test_recovery_matches_fresh_preparation_after_the_monthly_limit_changes(self) -> None:
        self.configure_preparation()
        subject = self.deployment()
        provisioned = subject.prepare()
        self.assertNotEqual(provisioned.parameters['DeploymentId'], DEPLOYMENT)
        notification = read_budget_notification_address(self.settings)
        load = subject._load  # pyright: ignore[reportPrivateUsage]
        override_fixture_settings(self.settings, 'monthly_limit_usd=250\n')
        self.clock.advance(1)
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'preparation expired or its inputs changed'):
            _ = load(notification)

        status, output = self.recover('--write')
        self.assertEqual(status, 0, output)
        recovered = load(notification)
        self.assertEqual(recovered[1]['MonthlyLimitUsd'], '250')
        self.assertEqual(recovered[1]['DeploymentId'], DEPLOYMENT)
        paths = tuple(self.output / name for name in ('template.json', 'parameters.json', 'preflight.json'))
        recovered_files = [path.read_bytes() for path in paths]
        preflight = remote.read_record(self.output / 'preflight.json')
        decision = ledger.mapping(preflight['decision'])
        self.assertEqual(decision['monthly_limit_usd'], '250')
        self.assertEqual(decision['at'], self.clock.now)
        self.assertEqual(preflight['at'], self.clock.now)

        fresh = subject.prepare()
        self.assertEqual(fresh.parameters['DeploymentId'], DEPLOYMENT)
        self.assertEqual([path.read_bytes() for path in paths], recovered_files)
        self.assertEqual(load(notification), recovered)

    def test_recovery_refuses_the_same_monthly_limit_as_fresh_preparation(self) -> None:
        self.configure_preparation()
        subject = self.deployment()
        _ = subject.prepare()
        files = (self.foundation, *(self.output / name for name in ('template.json', 'parameters.json', 'preflight.json')))
        original = [path.read_bytes() for path in files]
        override_fixture_settings(self.settings, 'monthly_limit_usd=0\n')
        self.clock.advance(1)

        status, output = self.recover('--write')
        self.assertEqual(status, 1, output)
        self.assertIn('monthly_limit', output)
        self.assertEqual([path.read_bytes() for path in files], original)
        self.assertFalse(self.manifest.exists())

        save_deployment_record(self.state, {**deployment_record(self.state), 'deployment_id': DEPLOYMENT})
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'monthly_limit'):
            _ = subject.prepare()
        self.assertEqual([path.read_bytes() for path in files[1:]], original[1:])

    def test_missing_preparation_refuses_before_aws_or_record_publication(self) -> None:
        (self.output / 'preflight.json').unlink()
        with self.assertRaisesRegex(deploy.PreparationRequired, r'Run deploy\.py prepare first'):
            _ = self.deployment().recover(write=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.foundation.exists())
        self.assertFalse(self.manifest.exists())

    def test_manifest_replace_failure_restores_both_records(self) -> None:
        replace_record = os.replace

        def fail_manifest(source: Path, target: Path) -> None:
            if target == self.manifest:
                self.assertEqual(deployment_record(self.state)['fingerprint'], FINGERPRINT)
                raise OSError('fixture manifest replacement failure')
            replace_record(source, target)

        with self.subTest('Existing records'):
            write_text_atomic(self.foundation, '{ "stack": "fixture-prior" }\n')
            write_text_atomic(self.manifest, '{ "bucket": "fixture-prior" }\n')
            before = self.foundation.read_bytes(), self.manifest.read_bytes()
            with mock.patch.object(os, 'replace', side_effect=fail_manifest) as replace_call:
                status, output = self.recover('--write')
            self.assertEqual(status, 1, output)
            self.assertEqual((self.foundation.read_bytes(), self.manifest.read_bytes()), before)
            self.assertEqual(replace_call.call_count, 3)

        with self.subTest('Missing records'):
            self.foundation.unlink()
            self.manifest.unlink()
            with mock.patch.object(os, 'replace', side_effect=fail_manifest) as replace_call:
                status, output = self.recover()
            self.assertEqual(status, 1, output)
            self.assertFalse(self.foundation.exists())
            self.assertFalse(self.manifest.exists())
            self.assertEqual(replace_call.call_count, 2)

    def test_different_records_require_write_and_report_only_field_labels(self) -> None:
        self.assertEqual(self.recover()[0], 0)
        foundation, gates = self.foundation.read_bytes(), self.manifest.read_bytes()
        changed = deployment_record(self.state)
        changed['account_id'] = OWNER.account[::-1] + '1'
        save_deployment_record(self.state, changed)
        manifest = cloud_gate.read(self.manifest)
        ledger.rows(manifest, 'regions')[1]['image'] = 'ami-fixture-old'
        cloud_gate.save(self.manifest, manifest)
        before = self.foundation.read_bytes(), self.manifest.read_bytes()
        status, output = self.recover()
        self.assertEqual(status, 1, output)
        self.assertIn('deployment.json differs in account.', output)
        self.assertIn('gate-deployment.json differs in regional launch settings.', output)
        self.assertEqual((self.foundation.read_bytes(), self.manifest.read_bytes()), before)
        self.assertEqual(self.recover('--write')[0], 0)
        self.assertEqual((self.foundation.read_bytes(), self.manifest.read_bytes()), (foundation, gates))

    def test_conflict_leaves_the_other_record_missing_without_write(self) -> None:
        cloud_gate.save(self.manifest, {'bucket': 'fixture-old-bucket'})
        status, output = self.recover()
        self.assertEqual(status, 1, output)
        self.assertFalse(self.foundation.exists())

    def test_missing_regional_stack_refuses_before_either_record_is_saved(self) -> None:
        self.missing.add(FALLBACK)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.MissingRecoveryStack, REGIONAL_STACK):
            _ = self.deployment().recover(write=True)
        self.assertFalse(self.foundation.exists())
        self.assertFalse(self.manifest.exists())

    def test_missing_home_stack_names_the_stack(self) -> None:
        self.missing.add(HOME)
        status, output = self.recover()
        self.assertEqual(status, 1, output)
        self.assertIn(f'Stack {STACK} in {HOME} does not exist.', output)
        self.assertFalse(self.foundation.exists())
        self.assertFalse(self.manifest.exists())

    def test_incomplete_regional_stack_preserves_existing_records(self) -> None:
        self.assertEqual(self.recover()[0], 0)
        before = self.foundation.read_bytes(), self.manifest.read_bytes()
        self.statuses[FALLBACK] = 'UPDATE_IN_PROGRESS'
        with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.IncompleteRecoveryStack, REGIONAL_STACK):
            _ = self.deployment().recover(write=True)
        self.assertEqual((self.foundation.read_bytes(), self.manifest.read_bytes()), before)

    def test_regional_stack_from_another_deployment_refuses(self) -> None:
        self.regional_deployment = 'ef' * 16
        status, output = self.recover('--write')
        self.assertEqual(status, 1, output)
        self.assertIn(f'Stack {REGIONAL_STACK} in {FALLBACK} belongs to a different deployment.', output)
        self.assertFalse(self.foundation.exists())
        self.assertFalse(self.manifest.exists())
