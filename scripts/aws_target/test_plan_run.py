"""Check the Hana setup plan with private fixtures and a restricted AWS fake."""
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import admin_secrets, deploy, hana_box, ledger, plan_identity, plan_run, test_deploy
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf


OWNER = plan_identity.PlanAccount('aws', '0' * 12)
CREDENTIAL = plan_identity.PlanCredentials('fixture-plan-key', 'fixture-plan-secret', OWNER)
SCALER = OWNER.iam('user/fixture/fixture-scaler')
POLICY = OWNER.iam('policy/fixture/scaler')
DEPLOYMENT = 'a' * 32
BUCKET_POLICY = {
    'Version': '2012-10-17',
    'Statement': [{
        'Effect': 'Deny',
        'Principal': '*',
        'Action': 's3:*',
        'Resource': ['arn:aws:s3:::fixture-bucket', 'arn:aws:s3:::fixture-bucket/*'],
        'Condition': {'Bool': {'aws:SecureTransport': 'false'}},
    }],
}
ALLOWED = frozenset((*plan_identity.READ_ACTIONS, *plan_identity.STACK_READS, *plan_identity.IAM_READS,
                     *plan_identity.BUCKET_READS, *plan_identity.CHANGE_SET_ACTIONS))
S3_SIMULATIONS = [('iam', 's3:PutObject'), ('iam', 's3:GetObject')]
PLAN_CALLS = (
    ('sts', 'get-caller-identity'),
    ('cloudformation', 'describe-stacks'),
    ('ec2', 'describe-instances'),
    ('ec2', 'describe-volumes'),
    ('cloudformation', 'get-template'),
    ('configure', 'export-credentials'),
    ('sts', 'get-caller-identity'),
    ('cloudformation', 'describe-stack-resource'),
    ('cloudformation', 'describe-stacks'),
    ('cloudformation', 'create-change-set'),
    ('cloudformation', 'describe-change-set'),
    ('cloudformation', 'delete-change-set'),
    ('cloudformation', 'describe-change-set'),
    ('ec2', 'describe-instances'),
    ('iam', 'ec2:DescribeInstances'),
    ('iam', 'ec2:DescribeSpotInstanceRequests'),
    ('iam', 'ec2:DescribeInstanceAttribute'),
    ('iam', 'ec2:ModifyInstanceAttribute'),
    ('iam', 'ec2:StartInstances'),
    ('iam', 'ec2:StopInstances'),
)


@final
class PlanSetupTests(CostFixture):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.calls: list[tuple[str, str]] = []
        self.change_name = ''
        self.change_exists = False
        self.deleting = False
        self.failure = ''
        self.region = ''
        self.change: dict[str, str] = {'Action': 'Modify', 'LogicalResourceId': 'ScalerPolicy',
                                      'ResourceType': 'AWS::IAM::ManagedPolicy', 'Replacement': 'False'}
        self.parameters: dict[str, str] = {}
        self.live_template: dict[str, object] = {}
        self.expect_bucket_policy = True
        self.credentials_path = Path()
        self.setup_path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.region = conf.read(conf.path('hana_aws')).text('region')
        self.credentials_path = self.root / '.config/aws-build/credentials'
        self.credentials_path.parent.mkdir(parents=True)
        _ = self.credentials_path.write_text('[build-scaler]\naws_access_key_id = fixture-scaler-key\n')
        self.credentials_path.chmod(0o600)
        self.setup_path = self.state / 'hana/setup.json'
        account = DeploymentSettings(self.region, 'fixture-admin', 'fixture-home', 'fixture-image',
            'fixture-owner', 'fixture-release', self.region + 'a', 'fixture-vpc', 'fixture-subnet',
            '/dev/xvda', 1, 2, 3, 1, 300, 'fixture-scaler')
        self.replace('stack_wait_timeout_s', '15')
        _ = self.enterContext(mock.patch.object(deploy, 'read_deployment_settings', return_value=account))
        _ = self.enterContext(mock.patch.object(plan_identity, 'plan_credentials', return_value=CREDENTIAL))
        _ = self.enterContext(mock.patch.object(deploy, 'execute_command', new=self.execute))
        _ = self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'get',
                                               side_effect=AssertionError('Plan opened administrator credentials.')))
        _ = self.enterContext(mock.patch('scripts.aws_target.deploy.time.sleep', return_value=None))

    @property
    def stack_arn(self) -> str:
        return f'arn:aws:cloudformation:{self.region}:{OWNER.account}:stack/fixture-hana/fixture-stack'

    def run_plan(self, *arguments: str) -> tuple[int, str]:
        before = self.files()
        output, errors = StringIO(), StringIO()
        argv = ['deploy.py', *arguments, '--settings', str(self.settings), '--directory', str(self.state),
                '--output', str(self.root / 'output'), '--plan']
        with mock.patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(errors):
            status = deploy.main()
        self.assertEqual(self.files(), before)
        printed = output.getvalue()
        for private in (OWNER.account, self.stack_arn, CREDENTIAL.access_key_id, 'fixture-private-physical',
                        'fixture-scaler-key', 'fixture-instance', 'fixture-disk'):
            self.assertNotIn(private, printed + errors.getvalue())
        return status, printed

    def files(self) -> dict[Path, bytes | None]:
        return {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob('*')}

    def prepare_hana(self) -> None:
        workload = conf.path('hana_aws')
        text = workload.read_text().replace('stack_name=' + conf.read(workload).text('stack_name'), 'stack_name=fixture-hana')
        _ = workload.write_text(text)
        self.live_template = test_deploy.HanaSetupFailureTests.stack_owned_hana_template()
        self.parameters = dict.fromkeys(ledger.mapping(self.live_template['Parameters']), 'fixture-live-value')
        self.parameters.update(DeploymentId=DEPLOYMENT, ScalerUser='fixture-scaler')
        self.setup_path.parent.mkdir()
        identity = hana_box.DeploymentIdentity(DEPLOYMENT, 'fixture-instance', 'fixture-request', self.region)
        _ = self.setup_path.write_text(json.dumps({
            'version': 1, 'requested': True, 'generation': 0, 'stack': 'fixture-hana', 'region': self.region,
            'deployment': DEPLOYMENT, 'instance': identity.instance, 'spot_request': identity.spot_request,
            'data_volume': 'fixture-disk', 'bucket': 'fixture-bucket', 'parameters_sha256': 'fixture',
            'template_sha256': 'fixture',
        }))
        hana_box.HanaController(self.state, self.settings, workload, hana_box.ProviderNotInstalled()).register(identity)

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertTrue(capture_output and text and timeout > 0 and not check)
        def option(name: str) -> str:
            return arguments[arguments.index(name) + 1]

        if '--profile' in arguments:
            service, operation = arguments[5:7]
            self.calls.append((service, operation))
            self.assertEqual((env or {}).get('AWS_SHARED_CREDENTIALS_FILE'), str(self.credentials_path))
            self.assertNotIn('AWS_ACCESS_KEY_ID', env or {})
            if (service, operation) == ('configure', 'export-credentials'):
                reply = {'AccessKeyId': 'fixture-scaler-key', 'SecretAccessKey': 'fixture-scaler-secret'}
            else:
                self.assertEqual((service, operation), ('sts', 'get-caller-identity'))
                self.assertIn(plan_run.action_name(service, operation), ALLOWED)
                reply = {'Arn': OWNER.user_arn if self.failure == 'wrong-scaler' else SCALER, 'Account': OWNER.account}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        self.assertEqual((env or {}).get('AWS_ACCESS_KEY_ID'), CREDENTIAL.access_key_id)
        self.assertNotIn('--dry-run', arguments)
        self.assertNotIn('--template-url', arguments)
        service, operation = arguments[3:5]
        self.assertIn(plan_run.action_name(service, operation), ALLOWED)
        self.calls.append((service, operation))

        def error(code: str, action: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 1, '',
                f'An error occurred ({code}) when calling the {action} operation: fixture-private-physical')

        if operation == 'get-caller-identity':
            reply = {'Arn': OWNER.user_arn, 'Account': OWNER.account}
        elif operation == 'describe-stacks':
            outputs = {'Instance': 'fixture-instance', 'DataVolume': 'fixture-disk',
                       'LaunchTemplate': 'fixture-template', 'ControlBucket': 'fixture-bucket'}
            reply = {'Stacks': [{'StackId': self.stack_arn, 'StackStatus': 'CREATE_COMPLETE',
                'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()],
                'Parameters': [{'ParameterKey': key, 'ParameterValue': value} for key, value in self.parameters.items()]}]}
        elif operation == 'get-template':
            reply = {'TemplateBody': self.live_template}
        elif operation == 'describe-stack-resource':
            self.assertEqual(option('--stack-name'), self.stack_arn)
            self.assertEqual(option('--logical-resource-id'), 'ScalerPolicy')
            reply = {'StackResourceDetail': {'ResourceType': 'AWS::IAM::ManagedPolicy',
                      'LogicalResourceId': 'ScalerPolicy', 'PhysicalResourceId': POLICY}}
        elif operation == 'create-change-set':
            self.assertEqual(option('--stack-name'), self.stack_arn)
            self.assertEqual(option('--change-set-type'), 'UPDATE')
            self.change_name = option('--change-set-name')
            self.assertTrue(self.change_name.startswith(plan_identity.CHANGE_SET_PREFIX))
            template = ledger.mapping(cast(object, json.loads(option('--template-body'))))
            expected_policy = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_bytes())))
            self.assertEqual(template, self.live_template | {'Resources': ledger.mapping(self.live_template['Resources'])
                | {'ScalerPolicy': ledger.mapping(expected_policy['Resources'])['ScalerPolicy']}})
            values = ledger.rows({'rows': cast(object, json.loads(option('--parameters')))}, 'rows')
            self.assertCountEqual(values, [{'ParameterKey': key, 'UsePreviousValue': True} for key in self.parameters])
            self.change_exists = True
            reply = {}
        elif operation == 'describe-change-set':
            self.assertEqual(option('--change-set-name'), self.change_name)
            if self.failure == 'describe' and not self.deleting:
                return error('AccessDenied', 'DescribeChangeSet')
            if not self.change_exists:
                return error('ChangeSetNotFound', 'DescribeChangeSet')
            reply = {'Status': 'DELETE_IN_PROGRESS' if self.deleting else 'CREATE_COMPLETE',
                     'Changes': [{'ResourceChange': self.change | {'PhysicalResourceId': POLICY}}],
                     'Parameters': [{'ParameterKey': key, 'UsePreviousValue': True} for key in self.parameters]}
        elif operation == 'delete-change-set':
            self.assertEqual(option('--change-set-name'), self.change_name)
            self.deleting = True
            if self.failure == 'delete-timeout':
                raise subprocess.TimeoutExpired('fixture delete', timeout)
            if self.failure == 'delete-error':
                return error('AccessDenied', 'DeleteChangeSet')
            self.change_exists = self.failure == 'delete-lingers'
            reply = {}
        elif operation == 'simulate-principal-policy':
            model = Path((env or {})['AWS_DATA_PATH']) / 'iam/2010-05-08/service-2.sdk-extras.json'
            self.assertTrue(model.is_file())
            request = ledger.mapping(cast(object, json.loads(option('--cli-input-json'))))
            self.assertEqual(request['PolicySourceArn'], SCALER)
            self.assertEqual(request['PolicyExclusionList'], [{'PolicyArn': POLICY}])
            document, = cast(list[str], request['PolicyInputList'])
            self.assertNotIn('Fn::', document)
            self.assertIn(f'arn:aws:ec2:{self.region}:{OWNER.account}:instance/*', document)
            self.assertIn(f'arn:aws:s3:::fixture-bucket/control/{DEPLOYMENT}/*', document)
            self.assertNotIn('"ec2:Attribute"', document)
            action, = cast(list[str], request['ActionNames'])
            resource, = cast(list[str], request['ResourceArns'])
            if action.startswith('s3:'):
                if self.expect_bucket_policy:
                    self.assertEqual(cast(object, json.loads(ledger.text(request, 'ResourcePolicy'))), BUCKET_POLICY)
                else:
                    self.assertNotIn('ResourcePolicy', request)
                self.assertEqual(resource, f'arn:aws:s3:::fixture-bucket/control/{DEPLOYMENT}/setup-probe.json')
                self.assertIn({'ContextKeyName': 'aws:SecureTransport', 'ContextKeyType': 'boolean',
                               'ContextKeyValues': ['true']}, cast(list[object], request['ContextEntries']))
            elif action in ('ec2:StartInstances', 'ec2:StopInstances', 'ec2:ModifyInstanceAttribute'):
                self.assertEqual(resource, f'arn:aws:ec2:{self.region}:{OWNER.account}:instance/fixture-instance')
            else:
                self.assertEqual(resource, '*')
            self.calls[-1] = (service, action)
            reply = {'EvaluationResults': [{'EvalActionName': action, 'EvalResourceName': resource,
                     'EvalDecision': 'implicitDeny' if self.failure == action else 'allowed'}]}
        elif operation == 'describe-instances':
            instance = {'InstanceId': 'fixture-instance', 'LaunchTime': 'fixture-launch',
                        'State': {'Name': 'running' if self.failure == 'running' else 'stopped'},
                        'Tags': [{'Key': 'HanaDeployment', 'Value': DEPLOYMENT}, {'Key': 'Workload', 'Value': 'hana'}]}
            reply = {'Reservations': [{'Instances': [instance]}]}
        elif operation == 'describe-volumes':
            reply = {'Volumes': [{'VolumeId': 'fixture-disk', 'Attachments': [
                {'InstanceId': 'fixture-instance', 'Device': '/dev/sdf', 'State': 'attached'}]}]}
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def test_stack_owned_plan_passes_with_the_current_allow_list(self) -> None:
        self.prepare_hana()
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS])
        self.assertIn('plan guard: Hana instance stopped and data disk attached', output)
        self.assertIn('plan IAM: Modify ScalerPolicy AWS::IAM::ManagedPolicy replacement=False', output)
        self.assertIn('ec2:DescribeInstanceAttribute simulation only', output)
        self.assertIn('s3:PutObject allowed', output)
        self.assertIn('s3:GetObject allowed', output)
        self.assertNotIn('plan note:', output)
        self.assertTrue(output.endswith('plan run: pass\n'))
        self.assertFalse(self.change_exists)

    def test_stack_owned_plan_passes_without_a_bucket_policy(self) -> None:
        self.prepare_hana()
        resources = ledger.mapping(self.live_template['Resources'])
        del resources['ControlBucketPolicy']
        self.expect_bucket_policy = False
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS])
        self.assertIn('plan note: no bucket policy in the stack template', output)
        self.assertTrue(output.endswith('plan run: pass\n'))

    def test_stack_owned_plan_passes_with_an_unresolved_bucket_policy(self) -> None:
        self.prepare_hana()
        resources = ledger.mapping(self.live_template['Resources'])
        policy = ledger.mapping(ledger.mapping(resources['ControlBucketPolicy'])['Properties'])
        policy['PolicyDocument'] = {'Fn::GetAtt': ['UnreadResource', 'Policy']}
        self.expect_bucket_policy = False
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS])
        self.assertIn('plan note: bucket policy not simulated (ControlBucketPolicy)', output)
        self.assertNotIn('plan note: no bucket policy in the stack template', output)
        self.assertTrue(output.endswith('plan run: pass\n'))

    def test_s3_simulation_denial_refuses_the_plan(self) -> None:
        self.prepare_hana()
        self.failure = 's3:PutObject'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, ('iam', 's3:PutObject')])
        self.assertTrue(output.endswith('plan run: refused: Hana scaler s3:PutObject denied.\n'))

    def assert_unsupported(self, reason: str, *arguments: str) -> None:
        status, output = self.run_plan(*arguments)
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [])
        self.assertTrue(output.endswith(f'plan run: refused: no plan run exists for this state ({reason})\n'))

    def test_first_setup_refuses_before_aws(self) -> None:
        self.assert_unsupported('first-time Hana setup', 'hana-setup')

    def test_pending_setup_refuses_before_aws(self) -> None:
        self.setup_path.parent.mkdir()
        for field in ('pending_launch', 'pending_volume', 'stop_pending'):
            with self.subTest(field=field):
                _ = self.setup_path.write_text(json.dumps({'version': 2, field: True}))
                self.assert_unsupported('Hana setup has pending instance or volume work', 'hana-setup')

    def test_direct_owned_setup_refuses_before_aws(self) -> None:
        self.setup_path.parent.mkdir()
        _ = self.setup_path.write_text(json.dumps({'version': 2, 'instance': 'fixture-instance'}))
        self.assert_unsupported('Hana uses direct-owned resources', 'hana-setup')

    def test_credential_mode_refuses_before_aws(self) -> None:
        self.prepare_hana()
        self.credentials_path.chmod(0o644)
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertIn('credential_file must name a private mode 0600 file', output)
        self.assertEqual(self.calls, [])

    def test_wrong_scaler_profile_refuses_before_a_change_set(self) -> None:
        self.prepare_hana()
        self.failure = 'wrong-scaler'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, list(PLAN_CALLS[:7]))
        self.assertIn('STS did not confirm the configured Hana scaler user', output)

    def test_hana_plan_deletes_the_change_set_when_the_guard_refuses(self) -> None:
        self.prepare_hana()
        self.change = {'Action': 'Modify', 'LogicalResourceId': 'DataVolume',
                       'ResourceType': 'AWS::EC2::Volume', 'Replacement': 'True'}
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, list(PLAN_CALLS[:13]))
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.endswith('plan run: refused: hana-setup refuses replacement: DataVolume\n'))

    def test_plan_deletes_the_change_set_when_description_fails(self) -> None:
        self.prepare_hana()
        self.failure = 'describe'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, list(PLAN_CALLS[:13]))
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.splitlines()[-1].startswith('plan run: refused: An error occurred (AccessDenied)'))

    def assert_delete_failed(self, failure: str) -> None:
        self.prepare_hana()
        self.failure = failure
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [*PLAN_CALLS[:12],
            *([('cloudformation', 'describe-change-set')] * len(plan_identity.PROOF_RETRY_DELAYS))])
        self.assertTrue(output.endswith('plan run: refused: change set not removed\n'))
        self.assertIn('plan stack: change set may remain: ' + self.change_name, output)
        self.assertTrue(self.change_exists)

    def test_delete_times_out(self) -> None:
        self.assert_delete_failed('delete-timeout')

    def test_delete_returns_error(self) -> None:
        self.assert_delete_failed('delete-error')

    def test_delete_succeeds_and_describe_still_lists_the_change_set(self) -> None:
        self.assert_delete_failed('delete-lingers')

    def test_denied_scaler_action_refuses_the_plan(self) -> None:
        self.prepare_hana()
        self.failure = 'ec2:StartInstances'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertTrue(output.endswith('plan run: refused: Hana scaler ec2:StartInstances denied.\n'))

    def test_plan_approval_flags_refuse_without_contacting_aws(self) -> None:
        status, output = self.run_plan('up', '--workload', 'merge-gates', '--approve-resources')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [])
        self.assertTrue(output.splitlines()[-1].startswith('plan run: refused:'))

    def test_inline_limit_refuses_before_a_change_set_exists(self) -> None:
        plan = plan_run.PlanRun(lambda: CREDENTIAL, self.execute, lambda _seconds: None)
        with self.assertRaisesRegex(plan_run.PlanRefused, '51,200-byte inline'), plan.change(
            plan.api(self.region, 1), 'fixture-hana', b'x' * (plan_run.INLINE_TEMPLATE_LIMIT + 1), [], 1,
        ):
            self.fail('The plan accepted an oversized template.')
        self.assertEqual(self.calls, [])

    def test_cold_plan_key_refuses_before_any_aws_call(self) -> None:
        self.prepare_hana()
        with mock.patch.object(plan_identity, 'plan_credentials',
            side_effect=plan_identity.PlanAgentCold('Run github-warmup in a terminal on natedev.')):
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [])
        self.assertTrue(output.endswith('plan run: refused: Run github-warmup in a terminal on natedev.\n'))


GATE_HOME = 'us-east-2'
GATE_REGION = 'us-west-2'
GATE_LAST = 'eu-north-1'
GATE_USER = OWNER.iam('user/fixture-scaler')
GATE_USER_ID = 'fixture-immutable-user'
GATE_BUCKET = f'cloud-claude-gate-{DEPLOYMENT}'
GATE_POLICY_NAMES = ('GateScalerPolicy', 'GateGpuTrialScalerPolicy')


@final
class GatePlanTests(CostFixture):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.regions: tuple[str, ...] = ()
        self.statuses: dict[str, str] = {}
        self.change_sets: dict[str, str] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.failure = ''
        self.failure_region = ''
        self.credentials_path = Path()
        self.template: dict[str, object] = {}
        self.enable_gates = True

    @override
    def setUp(self) -> None:
        super().setUp()
        self.regions = (GATE_HOME, GATE_REGION)
        self.credentials_path = self.root / '.config/aws-build/credentials'
        self.credentials_path.parent.mkdir(parents=True)
        _ = self.credentials_path.write_text('[build-scaler]\naws_access_key_id = fixture-scaler-key\n')
        self.credentials_path.chmod(0o600)
        self.template = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_bytes())))
        account = DeploymentSettings(GATE_HOME, 'fixture-admin', 'fixture-home', 'ami-home',
            'fixture-owner', 'fixture-release', GATE_HOME + 'a', 'vpc-' + GATE_HOME, 'subnet-' + GATE_HOME,
            '/dev/xvda', 1, 2, 3, 1, 300, 'fixture-scaler')
        self.replace('stack_wait_timeout_s', '15')
        _ = self.enterContext(mock.patch.object(deploy, 'read_deployment_settings', return_value=account))
        _ = self.enterContext(mock.patch.object(plan_identity, 'plan_credentials', return_value=CREDENTIAL))
        _ = self.enterContext(mock.patch.object(deploy, 'execute_command', new=self.execute))
        _ = self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'get',
                                               side_effect=AssertionError('Plan opened administrator credentials.')))
        _ = self.enterContext(mock.patch('scripts.aws_target.deploy.time.sleep', return_value=None))
        self.save_deployment()

    @staticmethod
    def stack_name(region: str) -> str:
        return 'fixture-home' if region == GATE_HOME else 'fixture-home-gates-' + region

    def stack_id(self, region: str) -> str:
        return f'arn:aws:cloudformation:{region}:{OWNER.account}:stack/{self.stack_name(region)}/fixture-stack'

    def save_deployment(self, **overrides: object) -> None:
        saved = {'stack': self.stack_name(GATE_HOME), 'stack_id': self.stack_id(GATE_HOME),
                 'region': GATE_HOME, 'account_id': OWNER.account, 'deployment_id': DEPLOYMENT,
                 'scaler_user': {'name': 'fixture-scaler', 'arn': GATE_USER, 'user_id': GATE_USER_ID}}
        _ = (self.state / 'deployment.json').write_text(json.dumps(saved | overrides))

    def run_plan(self, *arguments: str) -> tuple[int, str]:
        gate = Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf'
        lines = ['regions=' + ' '.join(self.regions) if line.startswith('regions=') else line
                 for line in gate.read_text().splitlines()]
        _ = conf.path('cloud_gate').write_text('\n'.join(lines) + '\n')
        before = {path: path.read_bytes() if path.is_file() else None for path in self.root.rglob('*')}
        output, errors = StringIO(), StringIO()
        argv = ['deploy.py', 'up', '--workload', 'merge-gates', '--settings', str(self.settings),
                '--directory', str(self.state), '--output', str(self.root / 'output'), '--plan', *arguments]
        with mock.patch.object(sys, 'argv', argv), redirect_stdout(output), redirect_stderr(errors):
            status = deploy.main()
        self.assertEqual({path: path.read_bytes() if path.is_file() else None for path in self.root.rglob('*')}, before)
        printed = output.getvalue()
        for secret in (OWNER.account, CREDENTIAL.access_key_id, CREDENTIAL.secret_access_key,
                       'fixture-scaler-key', 'fixture-scaler-secret', 'fixture-stack', 'fixture-private-physical'):
            self.assertNotIn(secret, printed + errors.getvalue())
        self.assertEqual(self.change_sets, {})
        return status, printed

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertTrue(capture_output and text and timeout > 0 and not check)

        def option(name: str) -> str:
            return arguments[arguments.index(name) + 1]

        region = option('--region')
        if '--profile' in arguments:
            self.assertEqual(option('--profile'), 'build-scaler')
            self.assertEqual((env or {}).get('AWS_SHARED_CREDENTIALS_FILE'), str(self.credentials_path))
            self.assertNotIn('AWS_ACCESS_KEY_ID', env or {})
            service, operation = arguments[5:7]
            self.calls.append((region, service, operation))
            if (service, operation) == ('configure', 'export-credentials'):
                result = {'AccessKeyId': 'fixture-scaler-key', 'SecretAccessKey': 'fixture-scaler-secret'}
            else:
                self.assertEqual((service, operation), ('sts', 'get-caller-identity'))
                arn = OWNER.user_arn if self.failure == 'scaler' else GATE_USER
                result = {'Account': OWNER.account, 'Arn': arn, 'UserId': GATE_USER_ID}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(result), '')

        self.assertEqual((env or {}).get('AWS_ACCESS_KEY_ID'), CREDENTIAL.access_key_id)
        service, operation = arguments[3:5]
        self.assertIn(plan_run.action_name(service, operation), ALLOWED)
        self.assertTrue(set(arguments).isdisjoint(('--dry-run', '--template-url', '--role-arn')))
        self.calls.append((region, service, operation))

        def error(code: str, action: str, reason: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 1, '',
                f'An error occurred ({code}) when calling the {action} operation: {reason}')

        if (service, operation) == ('sts', 'get-caller-identity'):
            reply: dict[str, object] = {'Arn': OWNER.user_arn, 'Account': OWNER.account}
        elif operation == 'describe-stacks':
            self.assertEqual(option('--stack-name'), self.stack_name(region))
            status = self.statuses.get(region, 'UPDATE_COMPLETE')
            if status == 'absent':
                return error('ValidationError', 'DescribeStacks', f'Stack with id {self.stack_name(region)} does not exist')
            outputs = {'ScalerUserName': 'fixture-scaler', 'GateBucket': GATE_BUCKET,
                       'GateLaunchTemplate': 'lt-' + region, 'GateSecurityGroup': 'sg-' + region}
            parameters = {'VpcId': 'vpc-' + GATE_HOME, 'DeploymentId': DEPLOYMENT, 'BudgetName': '',
                          'ScalerUserName': 'fixture-scaler', 'EnableGates': 'true' if self.enable_gates else 'false'}
            reply = {'Stacks': [{'StackName': self.stack_name(region), 'StackId': self.stack_id(region),
                'StackStatus': status, 'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()],
                'Parameters': [{'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()]}]}
        elif operation == 'get-template':
            self.assertEqual((region, option('--stack-name')), (GATE_HOME, self.stack_id(GATE_HOME)))
            reply = {'TemplateBody': self.template}
        elif operation == 'describe-stack-resource':
            self.assertEqual((region, option('--stack-name')), (GATE_HOME, self.stack_id(GATE_HOME)))
            logical_id = option('--logical-resource-id')
            self.assertIn(logical_id, GATE_POLICY_NAMES)
            reply = {'StackResourceDetail': {'ResourceType': 'AWS::IAM::ManagedPolicy',
                'LogicalResourceId': logical_id, 'PhysicalResourceId': OWNER.iam('policy/fixture/' + logical_id)}}
        elif operation == 'describe-route-tables':
            reply = {'RouteTables': [{'Associations': [{'Main': True}], 'Routes': [
                {'DestinationCidrBlock': '0.0.0.0/0', 'State': 'active', 'GatewayId': 'igw-fixture'}]}]}
        elif operation == 'describe-subnets':
            reply = {'Subnets': [{'SubnetId': 'subnet-' + region, 'AvailabilityZone': region + 'a', 'State': 'available'}]}
        elif operation == 'describe-vpcs':
            reply = {'Vpcs': [{'VpcId': 'vpc-' + region}]}
        elif operation == 'describe-images':
            images = [{'ImageId': 'ami-home' if region == GATE_HOME else 'ami-' + region,
                       'OwnerId': OWNER.account, 'Name': 'fixture-image'}]
            reply = {'Images': [] if self.failure == 'image' and region == self.failure_region else images}
        elif operation in ('create-change-set', 'describe-change-set', 'delete-change-set'):
            self.assertEqual(option('--stack-name'), self.stack_id(region))
            name = option('--change-set-name')
            self.assertTrue(name.startswith(plan_identity.CHANGE_SET_PREFIX))
            if operation == 'create-change-set':
                self.assertNotIn(region, self.change_sets)
                self.assertEqual(option('--change-set-type'), 'UPDATE')
                expected_template = self.template if region == GATE_HOME else deploy.region_gate_template()
                self.assertEqual(cast(object, json.loads(option('--template-body'))), expected_template)
                parameters = ledger.rows({'rows': cast(object, json.loads(option('--parameters')))}, 'rows')
                by_name = {ledger.text(row, 'ParameterKey'): row for row in parameters}
                if region == GATE_HOME:
                    self.assertEqual(by_name['DeploymentId'], {'ParameterKey': 'DeploymentId', 'UsePreviousValue': True})
                    self.assertEqual(by_name['BudgetName'], {'ParameterKey': 'BudgetName', 'UsePreviousValue': True})
                    self.assertEqual(by_name['EnableGates']['ParameterValue'], 'true')
                else:
                    self.assertEqual(by_name['DeploymentId']['ParameterValue'], DEPLOYMENT)
                    self.assertEqual(by_name['GateImage']['ParameterValue'], 'ami-' + region)
                    self.assertEqual(by_name['VpcId']['ParameterValue'], 'vpc-' + region)
                    self.assertEqual(by_name['HomeGateProfileArn']['ParameterValue'],
                                     OWNER.iam('instance-profile/' + GATE_BUCKET))
                self.change_sets[region] = name
                reply = {}
            elif region not in self.change_sets:
                return error('ChangeSetNotFound', 'DescribeChangeSet', 'fixture-private-physical')
            else:
                self.assertEqual(self.change_sets[region], name)
                if operation == 'delete-change-set':
                    del self.change_sets[region]
                    reply = {}
                elif self.failure == 'no changes' and region == self.failure_region:
                    reply = {'Status': 'FAILED', 'StatusReason': 'No updates are to be performed.', 'Changes': []}
                else:
                    obsolete = self.failure == 'obsolete' and region == self.failure_region
                    logical_id = 'ScalerUser' if self.failure == 'non-gate' and region == self.failure_region else 'GateLaunchTemplate'
                    reply = {'Status': 'CREATE_COMPLETE', 'ExecutionStatus': 'OBSOLETE' if obsolete else 'AVAILABLE',
                             'Changes': [{'ResourceChange': {'LogicalResourceId': logical_id, 'Action': 'Modify'}}]}
        elif (service, operation) == ('iam', 'simulate-principal-policy'):
            request = ledger.mapping(cast(object, json.loads(option('--cli-input-json'))))
            reply = self.simulation(region, request)
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def simulation(self, region: str, request: dict[str, object]) -> dict[str, object]:
        self.assertEqual(request['PolicySourceArn'], GATE_USER)
        if self.enable_gates:
            self.assertEqual(request['PolicyExclusionList'],
                             [{'PolicyArn': OWNER.iam('policy/fixture/' + name)} for name in GATE_POLICY_NAMES])
        else:
            self.assertNotIn('PolicyExclusionList', request)
        gate, gpu = cast(list[str], request['PolicyInputList'])
        self.assertNotIn('Fn::', gate + gpu)
        self.assertIn('iam:PassRole', gate)
        self.assertIn('ec2:DescribeInstanceTypeOfferings', gpu)
        self.assertIn(OWNER.iam('instance-profile/' + GATE_BUCKET), gate)
        self.assertIn('arn:aws:ec2:us-east-2::image/ami-home', gate)
        if region != GATE_HOME:
            self.assertIn(f'arn:aws:ec2:{region}::image/ami-{region}', gate)
        action, = cast(list[str], request['ActionNames'])
        resource, = cast(list[str], request['ResourceArns'])
        self.calls[-1] = (region, 'iam', action)
        contexts = {ledger.text(row, 'ContextKeyName'): row
                    for row in ledger.rows({'rows': request['ContextEntries']}, 'rows')}
        self.assertEqual(contexts['aws:RequestedRegion']['ContextKeyValues'], [region])
        if action == 'ec2:RunInstances' and ':instance/' in resource:
            self.assertEqual(contexts['ec2:InstanceMarketType']['ContextKeyValues'], ['spot'])
            self.assertEqual(contexts['aws:RequestTag/GateDeployment']['ContextKeyValues'], [DEPLOYMENT])
            self.assertEqual(contexts['aws:RequestTag/GpuSizeTrial']['ContextKeyValues'], ['setup-probe'])
        elif action == 'ec2:RunInstances' and ':subnet/' in resource:
            self.assertEqual(contexts['ec2:Vpc']['ContextKeyValues'],
                             [f'arn:aws:ec2:{region}:{OWNER.account}:vpc/vpc-{region}'])
        elif action == 'ec2:TerminateInstances':
            self.assertEqual(contexts['ec2:ResourceTag/Workload']['ContextKeyValues'], ['cloud-claude-gate'])
        elif action.startswith('s3:'):
            self.assertEqual(region, GATE_HOME)
            self.assertEqual(contexts['aws:SecureTransport']['ContextKeyType'], 'boolean')
            self.assertEqual(contexts['aws:SecureTransport']['ContextKeyValues'], ['true'])
            document = cast(object, json.loads(ledger.text(request, 'ResourcePolicy')))
            self.assertEqual(document, {'Version': '2012-10-17', 'Statement': [{
                'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
                'Resource': ['arn:aws:s3:::' + GATE_BUCKET, f'arn:aws:s3:::{GATE_BUCKET}/*'],
                'Condition': {'Bool': {'aws:SecureTransport': 'false'}},
            }]})
            if action == 's3:ListBucket':
                self.assertEqual(contexts['s3:prefix']['ContextKeyValues'], ['artifacts/gpu-size-trial/setup-probe.json'])
        decision = 'implicitDeny' if self.failure == action and self.failure_region == region else 'allowed'
        return {'EvaluationResults': [{'EvalActionName': action, 'EvalResourceName': resource, 'EvalDecision': decision}]}

    @staticmethod
    def gpu_types() -> set[str]:
        from scripts.aws_target import cloud_gate, gpu_size_trial

        return set(gpu_size_trial.settings().spot_max_usd_per_hour) | set(cloud_gate.settings().words('hana_gpu_instance_types'))

    def calls_in(self, region: str) -> list[tuple[str, str]]:
        return [(service, operation) for place, service, operation in self.calls if place == region]

    def test_existing_stacks_pass_in_configured_order(self) -> None:
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertEqual(output.splitlines()[-3:], [
            'plan gate: us-east-2: pass (UPDATE preview and scaler permissions passed)',
            'plan gate: us-west-2: pass (UPDATE preview and scaler permissions passed)', 'plan run: pass'])
        change = [('cloudformation', operation) for operation in (
            'create-change-set', 'describe-change-set', 'delete-change-set', 'describe-change-set')]
        home = [('sts', 'get-caller-identity'), ('cloudformation', 'describe-stacks'),
                ('configure', 'export-credentials'), ('sts', 'get-caller-identity'), ('cloudformation', 'get-template'),
                ('ec2', 'describe-route-tables'), ('ec2', 'describe-subnets'), ('ec2', 'describe-images'),
                ('cloudformation', 'describe-stack-resource'), ('cloudformation', 'describe-stack-resource')]
        regional = [('ec2', 'describe-vpcs'), ('ec2', 'describe-route-tables'), ('ec2', 'describe-subnets'),
                    ('ec2', 'describe-images'), ('cloudformation', 'describe-stacks')]
        reads = [('iam', action) for action in ('ec2:DescribeInstances', 'ec2:DescribeInstanceTypeOfferings',
                 'ec2:DescribeSpotPriceHistory', 'servicequotas:GetServiceQuota', 'ce:GetCostAndUsage')]
        launch = [('iam', action) for action in (
            *(['ec2:RunInstances'] * 6), 'ec2:CreateTags', 'ec2:RunInstances', 'ec2:CreateTags')]
        finish = [('iam', 'iam:PassRole'), ('iam', 'ec2:TerminateInstances')]
        s3 = [('iam', action) for action in ('s3:PutObject', 's3:GetObject', 's3:PutObject', 's3:GetObject',
                                           's3:ListBucket', 's3:AbortMultipartUpload')]
        count = len(self.gpu_types())
        self.assertEqual(self.calls_in(GATE_HOME), home + change + reads + launch * count + finish + s3)
        self.assertEqual(self.calls_in(GATE_REGION), regional + change + launch * count + finish)

    def test_existing_foundation_accepts_new_gate_policies(self) -> None:
        self.enable_gates = False
        self.regions = (GATE_HOME,)
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertNotIn(('cloudformation', 'describe-stack-resource'), self.calls_in(GATE_HOME))
        self.assertTrue(output.endswith('plan run: pass\n'))

    def test_unchanged_home_update_passes(self) -> None:
        self.failure, self.failure_region = 'no changes', GATE_HOME
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertIn('pass (UPDATE has no changes; scaler permissions passed)', output)

    def test_rolled_back_regional_stack_passes_after_its_permission_check(self) -> None:
        self.statuses[GATE_REGION] = 'ROLLBACK_COMPLETE'
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertIn('pass (permissions passed; approved setup must recreate the rolled-back stack)', output)
        self.assertNotIn(('cloudformation', 'create-change-set'), self.calls_in(GATE_REGION))
        self.assertIn(('iam', 'ec2:TerminateInstances'), self.calls_in(GATE_REGION))

    def test_unsupported_home_states_never_create_change_sets(self) -> None:
        for state, reason in (
            ('absent', 'the gate stack is absent'),
            ('REVIEW_IN_PROGRESS', 'the home gate stack is in REVIEW_IN_PROGRESS'),
            ('ROLLBACK_COMPLETE', 'reconcile the stack before deployment: ROLLBACK_COMPLETE'),
        ):
            with self.subTest(state=state):
                self.calls.clear()
                self.statuses[GATE_HOME] = state
                status, output = self.run_plan()
                self.assertEqual(status, 1, output)
                self.assertIn(reason, output)
                self.assertEqual(self.calls_in(GATE_HOME), [('sts', 'get-caller-identity'),
                                                            ('cloudformation', 'describe-stacks')])
                self.assertEqual(self.calls_in(GATE_REGION), [])

    def test_regional_review_state_refuses_without_a_change_set(self) -> None:
        self.statuses[GATE_REGION] = 'REVIEW_IN_PROGRESS'
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('the regional gate stack is in REVIEW_IN_PROGRESS', output)
        self.assertNotIn(('cloudformation', 'create-change-set'), self.calls_in(GATE_REGION))

    def test_regional_recovery_requires_a_passing_simulation(self) -> None:
        self.statuses[GATE_REGION] = 'ROLLBACK_COMPLETE'
        self.failure, self.failure_region = 'ec2:TerminateInstances', GATE_REGION
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('us-west-2: refused: Gate scaler ec2:TerminateInstances denied.', output)
        self.assertNotIn(('cloudformation', 'create-change-set'), self.calls_in(GATE_REGION))

    def test_absent_regional_stack_never_creates_a_review_stack(self) -> None:
        self.statuses[GATE_REGION] = 'absent'
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('us-east-2: pass (UPDATE', output)
        self.assertIn('the gate stack is absent; its CREATE preview can leave a review stack', output)
        self.assertNotIn(('cloudformation', 'create-change-set'), self.calls_in(GATE_REGION))

    def test_mismatched_identity_and_scaler_credentials_refuse_before_preview(self) -> None:
        self.save_deployment(stack_id='fixture-private-physical')
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('differs from the saved deployment identity', output)
        self.save_deployment()
        self.failure = 'scaler'
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('STS did not confirm the saved gate scaler user', output)
        self.assertNotIn(('cloudformation', 'create-change-set'), self.calls_in(GATE_HOME))
        self.assertEqual(self.calls_in(GATE_REGION), [])

    def test_nonprivate_scaler_file_refuses_before_any_aws_call(self) -> None:
        self.credentials_path.chmod(0o644)
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('credential_file must name a private mode 0600 file', output)
        self.assertEqual(self.calls, [])

    def test_regional_failures_do_not_hide_later_regions(self) -> None:
        self.regions = (GATE_HOME, GATE_REGION, GATE_LAST)
        self.failure_region = GATE_REGION
        for failure, reason in (
            ('non-gate', 'changes a non-gate resource'),
            ('obsolete', 'did not provide an available gate change set'),
            ('ec2:TerminateInstances', 'Gate scaler ec2:TerminateInstances denied'),
            ('image', 'the regional gate image needs an EC2 copy'),
        ):
            with self.subTest(failure=failure):
                self.calls.clear()
                self.failure = failure
                status, output = self.run_plan()
                self.assertEqual(status, 1, output)
                self.assertIn(reason, output)
                self.assertIn('plan gate: eu-north-1: pass (UPDATE', output)
                lines = [line for line in output.splitlines() if line.startswith('plan gate:')]
                self.assertEqual([line.split()[2].rstrip(':') for line in lines], list(self.regions))
