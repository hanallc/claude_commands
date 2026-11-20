"""Test deployment recovery with private settings and a fake AWS transport."""
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest
from unittest import mock

from scripts.aws_target import aws_trial, cloud_gate, deploy, ledger, plan_identity
from scripts.aws_target.config import deployment_record, save_deployment_record
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


HOME = 'us-east-2'
FALLBACK = 'us-west-2'
STACK = 'recover-fixture'
REGIONAL_STACK = f'{STACK}-gates-{FALLBACK}'
DEPLOYMENT = 'ab' * 16
REVISION = 'cd' * 20
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

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.calls: list[tuple[str, str, str]] = []
        self.missing: set[str] = set()
        self.statuses: dict[str, str] = {}
        self.regional_deployment: str = DEPLOYMENT

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = root / 'recovery-settings.conf'
        self.state = root / 'state'
        self.foundation = self.state / 'deployment.json'
        self.manifest = self.state / 'gate-deployment.json'
        write_text_atomic(self.settings, f'region={HOME}\nstack_name={STACK}\ndeployment_timeout_s=3\n')
        gates = root / 'recovery-gates.conf'
        write_text_atomic(gates, f'regions={HOME} {FALLBACK}\n')
        gate_settings = conf.read(gates)
        _ = self.enterContext(mock.patch.object(conf, 'read_or_repository', return_value=gate_settings))
        _ = self.enterContext(mock.patch.object(plan_identity, 'plan_credentials', return_value=PLAN_CREDENTIAL))
        _ = self.enterContext(mock.patch.object(aws_trial, 'host_nixpkgs_revision', return_value=REVISION))

    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.state.parent / 'output', execute=self.execute)

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
        for private in (OWNER.account, 'arn:', DEPLOYMENT, REVISION, SCALER_ID, BUCKET, ROLE, 'regions',
                        'subnet-fixture', 'sg-fixture', 'ami-fixture', 'fixture-private-budget'):
            self.assertNotIn(private, printed)
        return status, printed

    def test_creates_both_records_from_live_stack_values(self) -> None:
        status, output = self.recover()
        self.assertEqual(status, 0, output)
        home = self.stack(HOME, STACK)
        self.assertEqual(deployment_record(self.state), {'account_id': OWNER.account, 'region': HOME, 'stack': STACK,
            'stack_id': self.stack_arn(HOME, STACK), 'deployment_id': DEPLOYMENT, 'budget_name': 'fixture-private-budget',
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
        saved = deployment_record(self.state)
        saved['fingerprint'] = 'fixture-local-preparation'
        save_deployment_record(self.state, saved)
        with mock.patch.object(deploy, 'save_deployment_record') as foundation, mock.patch.object(cloud_gate, 'save') as gates:
            status, output = self.recover('--write')
        self.assertEqual(status, 0, output)
        self.assertEqual(output.count('already matches'), 2)
        foundation.assert_not_called()
        gates.assert_not_called()

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
