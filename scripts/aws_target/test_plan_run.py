"""Check the Hana setup plan with private fixtures and a restricted AWS fake."""
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import admin_secrets, deploy, hana_box, ledger, plan_identity, plan_run, test_deploy
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.controller_lock import control_lock
from scripts.aws_target.ledger_test_support import CostFixture, text_setting
from scripts.lib import conf, file_lock


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
CONFIRM_REMOVAL = ('cloudformation', 'describe-change-set')
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
        self.execution_status = 'AVAILABLE'
        self.check_claim: Callable[[], None] = lambda: None
        self.confirmations = 0
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

        def inject(stage: str, action: str) -> subprocess.CompletedProcess[str] | None:
            if self.failure == stage + '-interrupt':
                raise KeyboardInterrupt
            if self.failure == stage + '-timeout':
                raise subprocess.TimeoutExpired(operation, timeout)
            if self.failure == stage + '-error':
                return error('AccessDenied', action)
            return None

        if operation == 'get-caller-identity':
            reply = {'Arn': OWNER.user_arn, 'Account': OWNER.account}
        elif operation == 'describe-stacks':
            outputs = {'Instance': 'fixture-instance', 'DataVolume': 'fixture-disk',
                       'LaunchTemplate': 'fixture-template', 'ControlBucket': 'fixture-bucket'}
            reply = {'Stacks': [{'StackId': self.stack_arn, 'StackStatus': 'CREATE_COMPLETE',
                'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()],
                'Parameters': [{'ParameterKey': key, 'ParameterValue': value} for key, value in self.parameters.items()]}]}
        elif operation == 'get-template':
            self.check_claim()
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
            injected = inject('create', 'CreateChangeSet')
            if injected is not None:
                return injected
            if self.failure == 'create-unknown':
                return subprocess.CompletedProcess(arguments, 0, '{}', '')
            if self.failure == 'create-late':
                raise subprocess.TimeoutExpired(operation, timeout)
            self.change_exists = True
            reply = {'Id': 'fixture-change-id', 'StackId': self.stack_arn}
        elif operation == 'describe-change-set':
            self.assertEqual(option('--change-set-name'), self.change_name)
            if self.failure == 'describe' and not self.deleting:
                return error('AccessDenied', 'DescribeChangeSet')
            injected = inject('confirm' if self.deleting else 'describe', 'DescribeChangeSet')
            if injected is not None:
                return injected
            if self.deleting:
                self.confirmations += 1
                if self.failure == 'create-late' and self.confirmations == 2:
                    self.change_exists = True
            if not self.change_exists:
                return error('ChangeSetNotFound', 'DescribeChangeSet')
            reply = {'Status': 'DELETE_IN_PROGRESS' if self.deleting else 'CREATE_COMPLETE',
                     'ChangeSetName': self.change_name, 'ExecutionStatus': self.execution_status,
                     'Changes': [{'ResourceChange': self.change | {'PhysicalResourceId': POLICY}}],
                     'Parameters': [{'ParameterKey': key, 'UsePreviousValue': True} for key in self.parameters]}
            if self.failure == 'wait-interrupt' and not self.deleting:
                reply['Status'] = 'CREATE_IN_PROGRESS'
        elif operation == 'delete-change-set':
            self.assertEqual(option('--change-set-name'), self.change_name)
            self.deleting = True
            injected = inject('delete', 'DeleteChangeSet')
            if injected is not None:
                return injected
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
            if action == 's3:GetObject':
                self.check_claim()
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

    def test_stack_owned_plan_accepts_the_explicit_hana_workload(self) -> None:
        self.prepare_hana()
        status, output = self.run_plan('hana-setup', '--workload', 'hana')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS])

    def test_plan_blocks_claims_after_the_idle_check_and_through_the_last_simulation(self) -> None:
        self.prepare_hana()
        workload = conf.path('hana_aws')
        text_setting(workload, 'enabled', 'on')
        controller = hana_box.HanaController(self.state, self.settings, workload, hana_box.ProviderNotInstalled())
        blocked: list[str] = []

        @contextmanager
        def try_controller_lock(directory: Path) -> Generator[None]:
            with file_lock.exclusive(directory / 'controller.lock', wait=False) as result:
                if result is file_lock.LockResult.BUSY:
                    raise BlockingIOError
                yield

        def check_claim() -> None:
            with mock.patch.object(hana_box, 'control_lock', new=try_controller_lock), \
                    self.assertRaises(BlockingIOError):
                _ = controller.request('fixture-claim')
            blocked.append(self.calls[-1][1])

        self.check_claim = check_claim
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(blocked, ['get-template', 'simulate-principal-policy'])
        self.assertIsInstance(controller.request('fixture-claim'), hana_box.ClaimCommitted)

    def test_busy_controller_lock_refuses_before_aws(self) -> None:
        self.prepare_hana()
        with control_lock(self.state):
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [])
        self.assertTrue(output.endswith('plan run: refused: The Hana lifecycle is busy. '
                                        + 'Run hana-setup again when the box is idle.\n'))

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

    def test_merge_gate_plan_refuses_before_aws(self) -> None:
        self.assert_unsupported('merge gate setup', 'up', '--workload', 'merge-gates')

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

    def test_change_set_cleanup_after_failure_at_each_step(self) -> None:
        self.prepare_hana()
        for stage in ('create', 'describe', 'delete', 'confirm'):
            for failure in ('error', 'timeout', 'interrupt'):
                with self.subTest(stage=stage, failure=failure):
                    self.calls.clear()
                    self.change_exists = False
                    self.deleting = False
                    self.failure = f'{stage}-{failure}'
                    status, output = self.run_plan('hana-setup')
                    self.assertEqual(status, 1, output)
                    self.assertIn(('cloudformation', 'delete-change-set'), self.calls)
                    confirmed = 'plan stack: change set removal confirmed'
                    if stage == 'describe':
                        self.assertIn(confirmed, output)
                        self.assertFalse(self.change_exists)
                    else:
                        self.assertNotIn(confirmed, output)
                    if failure == 'interrupt':
                        self.assertIn(self.change_name, output)
                        self.assertTrue(output.endswith('plan run: refused: interrupted\n'))
                    elif stage != 'describe':
                        self.assertIn(self.change_name, output)
                        self.assertTrue(output.endswith('plan run: refused: change set not removed\n'))
                    else:
                        reason = ('An error occurred (AccessDenied)' if failure == 'error'
                                  else 'AWS did not confirm describe-change-set')
                        self.assertTrue(output.splitlines()[-1].startswith('plan run: refused: ' + reason))
                    if stage == 'create':
                        self.assertEqual(self.calls, [*PLAN_CALLS[:10], PLAN_CALLS[11],
                            *([CONFIRM_REMOVAL] * len(plan_identity.PROOF_RETRY_DELAYS))])
                        self.assertIn('plan stack: change set may still appear: ' + self.change_name, output)

    def test_unknown_create_reply_requires_the_full_removal_check(self) -> None:
        self.prepare_hana()
        self.failure = 'create-unknown'
        with mock.patch.object(time, 'sleep') as pause:
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(pause.call_args_list, [mock.call(delay) for delay in plan_identity.PROOF_RETRY_DELAYS if delay])
        self.assertNotIn('plan stack: change set removal confirmed', output)
        self.assertIn('plan stack: change set may still appear: ' + self.change_name, output)
        self.assertTrue(output.endswith('plan run: refused: change set not removed\n'))

    def test_late_change_set_gets_a_second_delete_and_confirmation(self) -> None:
        self.prepare_hana()
        self.failure = 'create-late'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [*PLAN_CALLS[:10], PLAN_CALLS[11],
            CONFIRM_REMOVAL, CONFIRM_REMOVAL, PLAN_CALLS[11], CONFIRM_REMOVAL])
        self.assertFalse(self.change_exists)
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.endswith('plan run: refused: AWS did not confirm create-change-set before the call ended.\n'))

    def test_interrupt_in_the_change_set_wait_reports_the_name(self) -> None:
        self.prepare_hana()
        self.failure = 'wait-interrupt'
        with mock.patch.object(time, 'sleep', side_effect=KeyboardInterrupt):
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, list(PLAN_CALLS[:13]))
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertIn(self.change_name, output)
        self.assertTrue(output.endswith('plan run: refused: interrupted\n'))

    def test_caller_interrupt_cannot_suppress_a_cleanup_refusal(self) -> None:
        created = subprocess.CompletedProcess('fixture create', 0, '{"Id": "fixture-change-id"}', '')
        present = subprocess.CompletedProcess(
            'fixture describe', 0, '{"Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE"}', '')
        deleted = subprocess.CompletedProcess('fixture delete', 0, '{}', '')
        checks = len(plan_identity.PROOF_RETRY_DELAYS)
        execute = mock.Mock(side_effect=[created, present, deleted, *([present] * checks)])
        plan = plan_run.PlanRun(lambda: CREDENTIAL, execute, lambda _seconds: None)
        output = StringIO()
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            with redirect_stdout(output), self.assertRaisesRegex(plan_run.PlanRefused, '^change set not removed$'):
                with plan.change(plan.api(self.region, 1), self.stack_arn, b'{}', [], 1) as change:
                    self.assertEqual(change['Status'], 'CREATE_COMPLETE')
        self.assertEqual(execute.call_count, 3 + checks)
        self.assertNotIn('plan stack: interrupted', output.getvalue())

    def test_obsolete_change_set_refuses_and_confirms_removal(self) -> None:
        self.prepare_hana()
        self.execution_status = 'OBSOLETE'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, list(PLAN_CALLS[:13]))
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.endswith('plan run: refused: The change set execution status is OBSOLETE. Expected AVAILABLE.\n'))

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
