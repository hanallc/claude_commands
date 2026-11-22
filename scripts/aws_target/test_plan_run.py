"""Check the Hana setup plan with private fixtures and a restricted AWS fake."""
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal, cast, final, override
from unittest import mock

from scripts.aws_target.aws_cli import AwsUnavailable
from scripts.aws_target import admin_secrets, aws_trial, deploy, hana_box, hana_image, hana_placement, hana_render, hana_replacement, ledger, plan_identity, plan_run, test_deploy
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.controller_lock import HanaCommandAccess, account_stack_lock, control_lock, hana_command_lock
from scripts.aws_target.ledger_test_support import CostFixture, text_setting, set_hana_switch
from scripts.aws_target.test_hana_box import IDENTITY, FakeProvider, record_lifecycle_image, write_lifecycle_configuration
from scripts.lib import conf
from scripts.lib.file_lock import LockResult


OWNER = plan_identity.PlanAccount('aws', '0' * 12)
CREDENTIAL = plan_identity.PlanCredentials('AKIA' + 'P' * 16, 'p' * 40, OWNER)
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
    ('ec2', 'describe-instances'),
    ('ec2', 'describe-images'),
    ('ec2', 'describe-volumes'),
    ('cloudformation', 'describe-stacks'),
    ('cloudformation', 'get-template'),
    ('sts', 'get-caller-identity'),
    ('cloudformation', 'describe-stack-resource'),
    ('ec2', 'describe-instances'),
    ('iam', 'ec2:DescribeInstances'),
    ('iam', 'ec2:DescribeSpotInstanceRequests'),
    ('iam', 'ec2:DescribeInstanceAttribute'),
    ('iam', 'ec2:ModifyInstanceAttribute'),
    ('iam', 'ec2:StartInstances'),
    ('iam', 'ec2:StopInstances'),
)
REPLACEMENT_SIMULATIONS = [
    ('iam', 'ec2:DescribeImages'),
    ('iam', 'ec2:DescribeVolumes'),
    ('iam', 'ec2:DescribeReplaceRootVolumeTasks'),
    *([('iam', 'ec2:CreateReplaceRootVolumeTask')] * 5),
]
CREATE_CHANGE = ('cloudformation', 'create-change-set')
DELETE_CHANGE = ('cloudformation', 'delete-change-set')



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
        self.image_description = ''
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
            'fixture-owner', 'fixture-release', self.region + 'a',
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
        digest = hana_render.guest_digest(workload)
        self.image_description = 'Hana guest digest ' + digest.value
        _ = hana_image.save(self.state, hana_image.RegisteredImage(
            'fixture-image', 'ami-fixture-next', OWNER.account, 'fixture-vpc', 'fixture-subnet', digest))

    def run_change(self) -> tuple[int, str]:
        output = StringIO()
        plan = plan_run.PlanRun(lambda: CREDENTIAL, self.execute, time.sleep)
        with redirect_stdout(output):
            try:
                with plan.change(plan.api(self.region, 1), self.stack_arn, b'{}', [], 15) as change:
                    self.assertEqual(change['Status'], 'CREATE_COMPLETE')
            except KeyboardInterrupt:
                print('plan run: refused: interrupted')
                return 1, output.getvalue()
            except (AwsUnavailable, plan_run.PlanRefused) as error:
                print('plan run: refused: ' + plan_run.safe_reason(error))
                return 1, output.getvalue()
        return 0, output.getvalue()

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
            self.assertEqual(option('--template-body'), '{}')
            self.assertEqual(option('--parameters'), '[]')
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
            elif action == 'ec2:CreateReplaceRootVolumeTask':
                self.assertIn(':' + self.region + ':', resource)
                self.assertNotIn('*', resource)
            else:
                self.assertEqual(resource, '*')
            self.calls[-1] = (service, action)
            reply = {'EvaluationResults': [{'EvalActionName': action, 'EvalResourceName': resource,
                     'EvalDecision': 'implicitDeny' if self.failure == action else 'allowed'}]}
        elif operation == 'describe-instances':
            instance = {'InstanceId': 'fixture-instance', 'LaunchTime': 'fixture-launch',
                        'SpotInstanceRequestId': 'fixture-request', 'InstanceLifecycle': 'spot',
                        'RootDeviceName': '/dev/xvda', 'RootDeviceType': 'ebs', 'ImageId': 'ami-fixture-prev',
                        'CurrentInstanceBootMode': 'uefi', 'Architecture': 'x86_64', 'VirtualizationType': 'hvm',
                        'PlatformDetails': 'Linux/UNIX', 'UsageOperation': 'RunInstances',
                        'BlockDeviceMappings': [
                            {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeId': 'vol-fixture-old'}},
                            {'DeviceName': '/dev/sdf', 'Ebs': {'VolumeId': 'fixture-disk', 'DeleteOnTermination': False}},
                        ],
                        'State': {'Name': 'running' if self.failure == 'running' else 'stopped'},
                        'Tags': [{'Key': 'HanaDeployment', 'Value': DEPLOYMENT}, {'Key': 'Workload', 'Value': 'hana'}]}
            reply = {'Reservations': [{'Instances': [instance]}]}
        elif operation == 'describe-images':
            reply = {'Images': [{'ImageId': 'ami-fixture-next', 'OwnerId': OWNER.account, 'Name': 'fixture-image',
                'State': 'available', 'RootDeviceType': 'ebs', 'RootDeviceName': '/dev/xvda', 'BootMode': 'uefi',
                'Description': self.image_description,
                'Architecture': 'x86_64', 'VirtualizationType': 'hvm',
                'PlatformDetails': 'Linux/UNIX', 'UsageOperation': 'RunInstances',
                'BlockDeviceMappings': [{'DeviceName': '/dev/xvda',
                    'Ebs': {'VolumeSize': 20, 'SnapshotId': 'snap-fixture-next'}}]}]}
        elif operation == 'describe-volumes':
            reply = {'Volumes': [{'VolumeId': 'fixture-disk', 'Attachments': [
                {'InstanceId': 'fixture-instance', 'Device': '/dev/sdf', 'State': 'attached',
                 'DeleteOnTermination': False, 'AttachTime': 'fixture-attach-time'}]}]}
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def test_registered_current_image_keeps_the_stale_notice_and_policy_plan(self) -> None:
        self.prepare_hana()
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertTrue(output.startswith(hana_box.HANA_IMAGE_STALE + '\n'))
        self.assertEqual(output.splitlines().count(hana_box.HANA_IMAGE_STALE), 1)
        self.assertIn('Plan Hana: Keep the instance and the data volume.', output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS, *REPLACEMENT_SIMULATIONS])

    def test_stack_owned_plan_passes_with_the_current_allow_list(self) -> None:
        self.prepare_hana()
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS, *REPLACEMENT_SIMULATIONS])
        self.assertIn('plan IAM: Modify ScalerPolicy AWS::IAM::ManagedPolicy replacement=False', output)
        self.assertIn('ec2:DescribeInstanceAttribute simulation only', output)
        self.assertIn('s3:PutObject allowed', output)
        self.assertIn('s3:GetObject allowed', output)
        self.assertNotIn('plan note:', output)
        self.assertTrue(output.endswith('plan run: pass\n'))
        self.assertFalse(self.change_exists)

    def test_stack_owned_plan_accepts_the_explicit_hana_workload(self) -> None:
        self.prepare_hana()
        for invalid_timing in (False, True):
            with self.subTest(invalid_timing=invalid_timing):
                if invalid_timing:
                    workload = conf.path('hana_aws')
                    text_setting(workload, 'setup_idle_wait_timeout_s', 'invalid')
                    text_setting(workload, 'setup_idle_heartbeat_s', 'invalid')
                self.calls.clear()
                self.deleting = False
                self.confirmations = 0
                status, output = self.run_plan('hana-setup', '--workload', 'hana')
                self.assertEqual(status, 0, output)
                self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS, *REPLACEMENT_SIMULATIONS])

    def test_plan_blocks_claims_after_the_idle_check_and_through_the_last_simulation(self) -> None:
        workload = conf.path('hana_aws')
        set_hana_switch(self.root, 'on')
        self.prepare_hana()
        controller = hana_box.HanaController(self.state, self.settings, workload, hana_box.ProviderNotInstalled())
        blocked: list[str] = []

        with ThreadPoolExecutor(max_workers=1) as worker:
            def check_claim() -> None:
                claim = worker.submit(controller.request, 'fixture-claim')
                self.assertIsInstance(claim.result(timeout=5), hana_box.HanaResourcesHeld)
                tick = worker.submit(controller.tick)
                self.assertIsInstance(tick.result(timeout=5), hana_box.HanaResourcesHeld)
                blocked.append(self.calls[-1][1])

            self.check_claim = check_claim
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(blocked, ['get-template', 'simulate-principal-policy'])
        with mock.patch.object(controller, 'image_is_current', return_value=True):
            self.assertIsInstance(controller.request('fixture-claim'), hana_box.ClaimCommitted)

    def test_hana_down_refuses_while_the_plan_holds_the_box(self) -> None:
        self.prepare_hana()
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=self.execute)
        refused: list[str] = []

        def check_teardown() -> None:
            before = list(self.calls)
            with redirect_stdout(StringIO()), self.assertRaisesRegex(deploy.HanaControlRefused,
                    r'^hana-down: Another command is using the Hana resources\. '
                    + r'Run the same command again when that command ends\.$'):
                _ = subject.hana_down(approve_destroy=True)
            self.assertEqual(self.calls, before)
            refused.append(before[-1][1])

        self.check_claim = check_teardown
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(refused, ['get-template', 'simulate-principal-policy'])

    def test_busy_controller_lock_refuses_before_aws(self) -> None:
        self.prepare_hana()
        with control_lock(self.state):
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [])
        self.assertTrue(output.endswith('plan run: refused: The Hana controller holds the lock. '
                                        + 'Run the same command again when the controller releases it.\n'))

    def test_claimed_box_refuses_without_waiting_or_creating_a_change_set(self) -> None:
        workload = conf.path('hana_aws')
        set_hana_switch(self.root, 'on')
        self.prepare_hana()
        controller = hana_box.HanaController(self.state, self.settings, workload, hana_box.ProviderNotInstalled())
        with mock.patch.object(controller, 'image_is_current', return_value=True):
            self.assertIsInstance(controller.request('existing-claim'), hana_box.ClaimCommitted)
        with mock.patch.object(time, 'sleep') as pause:
            status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, list(PLAN_CALLS[:1]))
        self.assertTrue(output.endswith('plan run: refused: The Hana lifecycle is busy. '
                                        + 'Run hana-setup again when the box is idle.\n'))
        pause.assert_not_called()

    def test_stack_owned_plan_passes_without_a_bucket_policy(self) -> None:
        self.prepare_hana()
        resources = ledger.mapping(self.live_template['Resources'])
        del resources['ControlBucketPolicy']
        self.expect_bucket_policy = False
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS, *REPLACEMENT_SIMULATIONS])
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
        self.assertEqual(self.calls, [*PLAN_CALLS, *S3_SIMULATIONS, *REPLACEMENT_SIMULATIONS])
        self.assertIn('plan note: bucket policy not simulated (ControlBucketPolicy)', output)
        self.assertNotIn('plan note: no bucket policy in the stack template', output)
        self.assertTrue(output.endswith('plan run: pass\n'))

    def test_s3_simulation_denial_refuses_the_plan(self) -> None:
        self.prepare_hana()
        self.failure = 's3:PutObject'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [*PLAN_CALLS, ('iam', 's3:PutObject')])
        self.assertTrue(output.startswith(hana_box.HANA_IMAGE_STALE + '\n'))
        self.assertTrue(output.endswith('plan run: refused: Hana scaler s3:PutObject denied.\n'))

    def assert_unsupported(self, reason: str, *arguments: str) -> None:
        status, output = self.run_plan(*arguments)
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [])
        self.assertTrue(output.endswith(f'plan run: refused: no plan run exists for this state ({reason})\n'))

    def test_first_setup_refuses_before_aws(self) -> None:
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(output, 'plan run: refused: Hana has no saved setup. Run deploy.py prepare --workload hana.\n')

    def test_pending_setup_refuses_before_aws(self) -> None:
        self.setup_path.parent.mkdir()
        (self.state / 'controller.lock').touch()
        for field in ('pending_launch', 'pending_volume', 'stop_pending'):
            with self.subTest(field=field):
                _ = self.setup_path.write_text(json.dumps({'version': 2, field: True}))
                self.assert_unsupported('Hana setup has pending instance or volume work', 'hana-setup')

    def test_no_image_refuses_before_aws(self) -> None:
        self.prepare_hana()
        (self.state / hana_image.RECORD).unlink()
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertEqual(output, hana_box.HANA_IMAGE_STALE + '\n' +
                         'plan run: refused: Hana has no verified image registration. '
                         + hana_replacement.REGISTER_IMAGE + '\n')
        self.assertEqual(self.calls, [])

    def test_direct_owned_setup_without_image_registration_refuses_before_aws(self) -> None:
        self.setup_path.parent.mkdir()
        (self.state / 'controller.lock').touch()
        _ = self.setup_path.write_text(json.dumps({'version': 2, 'instance': 'fixture-instance'}))
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(output, hana_box.HANA_IMAGE_STALE + '\n' +
                         'plan run: refused: Hana has no verified image registration. '
                         + hana_replacement.REGISTER_IMAGE + '\n')

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

    def test_hana_plan_refuses_a_stack_disk_change_without_a_change_set(self) -> None:
        self.prepare_hana()
        resources = ledger.mapping(self.live_template['Resources'])
        ledger.mapping(resources['DataVolume'])['Type'] = 'AWS::S3::Bucket'
        status, output = self.run_plan('hana-setup')
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, list(PLAN_CALLS[:6]))
        self.assertEqual(output, hana_box.HANA_IMAGE_STALE + '\n' +
                         'plan run: refused: The live template does not match the saved Hana setup.\n')
        self.assertFalse(self.change_exists)

    def test_plan_deletes_the_change_set_when_description_fails(self) -> None:
        self.failure = 'describe'
        status, output = self.run_change()
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, [CREATE_CHANGE, CONFIRM_REMOVAL, DELETE_CHANGE, CONFIRM_REMOVAL])
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.splitlines()[-1].startswith('plan run: refused: An error occurred (AccessDenied)'))

    def test_change_set_cleanup_after_failure_at_each_step(self) -> None:
        for stage in ('create', 'describe', 'delete', 'confirm'):
            for failure in ('error', 'timeout', 'interrupt'):
                with self.subTest(stage=stage, failure=failure):
                    self.calls.clear()
                    self.change_exists = False
                    self.deleting = False
                    self.failure = f'{stage}-{failure}'
                    status, output = self.run_change()
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
                        self.assertEqual(self.calls, [CREATE_CHANGE, DELETE_CHANGE,
                            *([CONFIRM_REMOVAL] * len(plan_identity.PROOF_RETRY_DELAYS))])
                        self.assertIn('plan stack: change set may still appear: ' + self.change_name, output)

    def test_unknown_create_reply_requires_the_full_removal_check(self) -> None:
        self.failure = 'create-unknown'
        with mock.patch.object(time, 'sleep') as pause:
            status, output = self.run_change()
        self.assertEqual(status, 1, output)
        self.assertEqual(pause.call_args_list, [mock.call(delay) for delay in plan_identity.PROOF_RETRY_DELAYS if delay])
        self.assertNotIn('plan stack: change set removal confirmed', output)
        self.assertIn('plan stack: change set may still appear: ' + self.change_name, output)
        self.assertTrue(output.endswith('plan run: refused: change set not removed\n'))

    def test_late_change_set_gets_a_second_delete_and_confirmation(self) -> None:
        self.failure = 'create-late'
        status, output = self.run_change()
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [CREATE_CHANGE, DELETE_CHANGE,
            CONFIRM_REMOVAL, CONFIRM_REMOVAL, DELETE_CHANGE, CONFIRM_REMOVAL])
        self.assertFalse(self.change_exists)
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.endswith('plan run: refused: AWS did not confirm create-change-set before the call ended.\n'))

    def test_interrupt_in_the_change_set_wait_reports_the_name(self) -> None:
        self.failure = 'wait-interrupt'
        with mock.patch.object(time, 'sleep', side_effect=KeyboardInterrupt):
            status, output = self.run_change()
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [CREATE_CHANGE, CONFIRM_REMOVAL, DELETE_CHANGE, CONFIRM_REMOVAL])
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
        self.execution_status = 'OBSOLETE'
        status, output = self.run_change()
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [CREATE_CHANGE, CONFIRM_REMOVAL, DELETE_CHANGE, CONFIRM_REMOVAL])
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertTrue(output.endswith('plan run: refused: The change set execution status is OBSOLETE. Expected AVAILABLE.\n'))

    def assert_delete_failed(self, failure: str) -> None:
        self.failure = failure
        status, output = self.run_change()
        self.assertEqual(status, 1, output)
        self.assertEqual(self.calls, [CREATE_CHANGE, CONFIRM_REMOVAL, DELETE_CHANGE,
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
MANAGED_POLICY = 'AWS::IAM::ManagedPolicy'
GATE_TRIAL_GPU_TYPE = 'g6.xlarge'
GATE_HANA_GPU_TYPE = 'g6.2xlarge'


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
        self.displaced_policies: dict[str, dict[str, str]] = {}
        self.unresolved_policy = ''
        self.cleanup_timeouts: list[tuple[float, float]] = []
        self.on_aws_call: Callable[[str, str, str], None] = lambda _region, _service, _operation: None

    @override
    def setUp(self) -> None:
        super().setUp()
        self.regions = (GATE_HOME, GATE_REGION)
        with account_stack_lock(self.state):
            pass
        self.credentials_path = self.root / '.config/aws-build/credentials'
        self.credentials_path.parent.mkdir(parents=True)
        _ = self.credentials_path.write_text('[build-scaler]\naws_access_key_id = fixture-scaler-key\n')
        self.credentials_path.chmod(0o600)
        self.template = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_bytes())))
        account = DeploymentSettings(GATE_HOME, 'fixture-admin', 'fixture-home', 'ami-home',
            'fixture-owner', 'fixture-release', GATE_HOME + 'a',
            '/dev/xvda', 1, 2, 3, 30, 300, 'fixture-scaler')
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

    def write_gate_configuration(self) -> None:
        _ = conf.path('cloud_gate').write_text(f'''\
regions={' '.join(self.regions)}
instance_types=m7a.8xlarge
credential_file={self.credentials_path}
hana_gpu_instance_types={GATE_HANA_GPU_TYPE}
hana_gpu_spot_max_usd_per_hour={json.dumps({GATE_HANA_GPU_TYPE: '0.5'})}
gpu_size_trial_spot_max_usd_per_hour={json.dumps({GATE_TRIAL_GPU_TYPE: '0.25'})}
gpu_size_trial_limit_usd=2
gpu_size_trial_box_extra_usd=0.05
# whole: 1..
gpu_size_trial_lifetime_minutes=10
# range: 1..
api_timeout_s=5
# range: 0..
spot_max_usd_per_hour=0.5
# whole: 1..
root_gib=30
# whole: 1..
artifact_days=7
# whole: 1..
log_days=7
# whole: 1..
expiry_schedule_minutes=5
# whole: 1..
expiry_timeout_s=20
# whole: 1..
expiry_memory_mib=128
# range: 0..
shared_usd_per_month=1
''')

    def run_plan(self, *arguments: str) -> tuple[int, str]:
        self.write_gate_configuration()
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
        if self.failure != 'cleanup-fails':
            self.assertEqual(self.change_sets, {})
        return status, printed

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertTrue(capture_output and text and timeout > 0 and not check)

        def option(name: str) -> str:
            return arguments[arguments.index(name) + 1]

        region = option('--region')
        with account_stack_lock(self.state) as access:
            self.assertIs(access, LockResult.BUSY)
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
        self.on_aws_call(region, service, operation)

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
            self.assertEqual(option('--stack-name'), self.stack_id(region))
            logical_id = option('--logical-resource-id')
            self.assertTrue(logical_id in self.displaced_policies.get(region, {})
                            or region == GATE_HOME and logical_id in GATE_POLICY_NAMES)
            reply = {'StackResourceDetail': {'ResourceType': MANAGED_POLICY, 'LogicalResourceId': logical_id}}
            if logical_id != self.unresolved_policy:
                ledger.mapping(reply['StackResourceDetail'])['PhysicalResourceId'] = OWNER.iam('policy/fixture/' + logical_id)
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
            if region != GATE_HOME and '--image-ids' in arguments:
                self.assertEqual(option('--image-ids'), 'ami-' + region)
                reply = {'Images': [dict(images[0], State='available')]}
            else:
                reply = {'Images': [] if self.failure == 'image' and region == self.failure_region else images}
        elif operation in ('create-change-set', 'describe-change-set', 'delete-change-set'):
            self.assertEqual(option('--stack-name'), self.stack_id(region))
            name = option('--change-set-name')
            self.assertTrue(name.startswith(plan_identity.CHANGE_SET_PREFIX))
            failure = self.failure if region == self.failure_region else ''
            reads = self.calls_in(region).count(CONFIRM_REMOVAL)
            if operation == 'delete-change-set' or (operation == 'describe-change-set' and reads > 1):
                self.cleanup_timeouts.append((time.monotonic(), timeout))
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
                if failure == 'create-missing-id':
                    reply = {}
                else:
                    self.change_sets[region] = name
                    reply = {'Id': 'fixture-change-id'}
            elif failure == 'create-missing-id' and operation == 'describe-change-set' and reads == 2:
                self.change_sets[region] = name
                reply = {'ChangeSetName': name, 'Status': 'CREATE_COMPLETE'}
            elif region not in self.change_sets:
                action = plan_run.action_name('cloudformation', operation).split(':')[1]
                return error('ChangeSetNotFound', action, 'fixture-private-physical')
            else:
                self.assertEqual(self.change_sets[region], name)
                if operation == 'delete-change-set':
                    if failure == 'cleanup-fails':
                        return error('AccessDenied', 'DeleteChangeSet', 'fixture-private-physical')
                    if failure == 'preview-interrupted' and reads == 1:
                        del self.change_sets[region]
                        raise KeyboardInterrupt
                    if failure == 'delete-ready-last' and reads == 1:
                        return error('InvalidChangeSetStatus', 'DeleteChangeSet', 'fixture-private-physical')
                    del self.change_sets[region]
                    reply = {}
                elif failure == 'delete-ready-last' and 1 < reads <= len(plan_identity.PROOF_RETRY_DELAYS):
                    reply = {'Status': 'CREATE_IN_PROGRESS'}
                elif failure == 'cleanup-fails' and reads > 1:
                    reply = {'Status': 'DELETE_FAILED'}
                elif failure == 'no changes':
                    reply = {'Status': 'FAILED', 'StatusReason': 'No updates are to be performed.', 'Changes': []}
                else:
                    logical_id = 'ScalerUser' if failure == 'non-gate' else 'GateLaunchTemplate'
                    resources = [{'LogicalResourceId': logical_id, 'Action': 'Modify', 'ResourceType': 'AWS::EC2::LaunchTemplate'}]
                    resources.extend({'LogicalResourceId': policy_id, 'Action': action, 'ResourceType': MANAGED_POLICY,
                                      'Replacement': 'True'} for policy_id, action in self.displaced_policies.get(region, {}).items())
                    reply = {'Status': 'CREATE_COMPLETE', 'ExecutionStatus': 'OBSOLETE' if failure == 'obsolete' else 'AVAILABLE',
                             'Changes': [{'ResourceChange': resource} for resource in resources]}
        elif (service, operation) == ('iam', 'simulate-principal-policy'):
            request = ledger.mapping(cast(object, json.loads(option('--cli-input-json'))))
            reply = self.simulation(region, request)
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def simulation(self, region: str, request: dict[str, object]) -> dict[str, object]:
        self.assertEqual(request['PolicySourceArn'], GATE_USER)
        active = GATE_POLICY_NAMES if self.enable_gates else ()
        exclusions = dict.fromkeys((*active, *(name for place in self.regions
                                               if self.failure != 'cleanup-fails' or place != self.failure_region
                                               for name in self.displaced_policies.get(place, {}))))
        if exclusions:
            self.assertEqual(request['PolicyExclusionList'],
                             [{'PolicyArn': OWNER.iam('policy/fixture/' + name)} for name in exclusions])
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
        if action == 'ec2:GetSpotPlacementScores':
            self.assertEqual(resource, '*')
            self.assertEqual(contexts['ec2:Region']['ContextKeyValues'], [region])
        elif action == 'ec2:RunInstances' and ':instance/' in resource:
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
                ('ec2', 'describe-images'), ('ec2', 'describe-route-tables'), ('ec2', 'describe-subnets')]
        policies = [('cloudformation', 'describe-stack-resource')] * 2
        regional = [('ec2', 'describe-vpcs'), ('ec2', 'describe-route-tables'), ('ec2', 'describe-subnets'),
                    ('ec2', 'describe-images'), ('cloudformation', 'describe-stacks')]
        reads = [('iam', action) for action in ('ec2:DescribeInstances', 'ec2:DescribeInstanceTypeOfferings',
                 'ec2:DescribeSpotPriceHistory', 'servicequotas:GetServiceQuota', 'ce:GetCostAndUsage')]
        scores = [('iam', 'ec2:GetSpotPlacementScores')]
        launch = [('iam', action) for action in (
            *(['ec2:RunInstances'] * 6), 'ec2:CreateTags', 'ec2:RunInstances', 'ec2:CreateTags')]
        finish = [('iam', 'iam:PassRole'), ('iam', 'ec2:TerminateInstances')]
        s3 = [('iam', action) for action in ('s3:PutObject', 's3:GetObject', 's3:PutObject', 's3:GetObject',
                                           's3:ListBucket', 's3:AbortMultipartUpload')]
        count = len((GATE_TRIAL_GPU_TYPE, GATE_HANA_GPU_TYPE))
        for region, calls in (
            (GATE_HOME, home + change + policies + reads + scores + launch * count + finish + s3),
            (GATE_REGION, regional + change + scores + launch * count + finish),
        ):
            with self.subTest(region=region):
                self.assertEqual(self.calls_in(region), calls)

    def test_busy_account_stack_lock_refuses_the_plan_before_aws(self) -> None:
        with account_stack_lock(self.state) as access:
            self.assertIs(access, LockResult.ACQUIRED)
            status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertEqual(output, 'plan run: refused: up --workload merge-gates --plan: '
                                 + 'Another command is using the AWS account stack. '
                                 + 'Run the same command again when that command ends.\n')
        self.assertEqual(self.calls, [])

    def test_gate_plan_can_run_during_a_hana_resource_hold(self) -> None:
        with hana_command_lock(self.state) as access:
            self.assertIs(access, HanaCommandAccess.ACQUIRED)
            status, output = self.run_plan()
        self.assertEqual(status, 0, output)

    @contextmanager
    def hana_activity(
        self, directory: Path, claim: str, *, renew_on: str, release_on: str,
    ) -> Generator[tuple[TestClock, Callable[[str], None]]]:
        clock = TestClock()
        workload = conf.path('hana_aws')
        write_lifecycle_configuration(workload)
        book = ledger.CostLedger(directory)
        book.record_inventory(ledger.ResourceInventory(clock.now, (), 'private inventory', True))
        book.observe(ledger.InstanceObservation(clock.now, 'terminated', 'private account inventory'))
        provider = FakeProvider(directory, clock)
        controller = hana_box.HanaController(directory, self.settings, workload, provider, clock=clock)
        controller.register(IDENTITY)
        _ = record_lifecycle_image(directory, workload)
        self.assertIsInstance(controller.request(claim), hana_box.ClaimCommitted)
        for _step in range(4):
            clock.advance(1)
            _ = controller.tick()
        initial = controller.inspect_for_settlement()
        assert isinstance(initial, hana_box.HanaLifecycleEvidence)
        permit = initial.permit
        assert isinstance(permit, hana_box.DeliveredPermit)
        settled: list[str] = []

        def renew_and_settle(operation: str) -> None:
            if operation == renew_on:
                clock.now = permit.expires - 100
                self.assertEqual(controller.tick(), hana_box.Lifecycle.READY)
                clock.advance(1)
                self.assertEqual(controller.tick(), hana_box.Lifecycle.READY)
                current = controller.inspect_for_settlement()
                assert isinstance(current, hana_box.HanaLifecycleEvidence)
                assert isinstance(current.permit, hana_box.DeliveredPermit)
                self.assertGreater(current.permit.expires, permit.expires)
                self.assertEqual(provider.published[-1].expected_generation, permit.generation)
                settled.append('renewed')
            if operation == release_on:
                self.assertIsInstance(controller.release(claim), hana_placement.HanaClaimReleased)
                self.assertIsInstance(controller.claim_status(claim), hana_box.ClaimRefused)
                settled.append('released')

        yield clock, renew_and_settle
        self.assertEqual(settled, ['renewed', 'released'])

    def test_gate_plan_renews_a_hana_permit_and_settles_the_claim_during_aws_calls(self) -> None:
        with self.hana_activity(self.state, 'gate-plan-claim', renew_on='create-change-set',
                                release_on='delete-change-set') as (_clock, observe):
            def on_aws_call(region: str, service: str, operation: str) -> None:
                if (region, service) == (GATE_HOME, 'cloudformation'):
                    observe(operation)

            self.on_aws_call = on_aws_call
            self.write_gate_configuration()
            plan = plan_run.PlanRun(lambda: CREDENTIAL, self.execute, lambda _seconds: None)
            subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=self.execute, mode=plan)
            with redirect_stdout(StringIO()):
                subject.gate_plan()
        self.assertEqual(self.change_sets, {})

    def test_account_stack_lock_excludes_writes_and_allows_hana_activity(self) -> None:
        unexpected = mock.Mock(side_effect=AssertionError('Unexpected AWS call'))
        approved = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=unexpected)
        operations: tuple[tuple[str, Callable[[], object]], ...] = (
            ('up --workload merge-gates', lambda: approved.gate_up(approve_resources=True)),
            ('remove-images', lambda: approved.remove_images(approve_destroy=True)),
            ('apply', lambda: approved.apply(approve_resources=True)),
            ('down', lambda: approved.down(approve_destroy=True)),
        )
        refused: list[str] = []

        def check_exclusion(region: str, service: str, operation: str) -> None:
            if (region, service, operation) != (GATE_HOME, 'sts', 'get-caller-identity'):
                return
            for command, run in operations:
                with self.subTest(command=command), self.assertRaises(deploy.DeploymentRefused) as error:
                    _ = run()
                self.assertEqual(str(error.exception), f'{command}: Another command is using the AWS account stack. '
                                 + 'Run the same command again when that command ends.')
                refused.append(command)

        self.on_aws_call = check_exclusion
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertEqual(refused, [command for command, _run in operations])
        unexpected.assert_not_called()

        self.regions = (GATE_HOME,)
        self.write_gate_configuration()
        self.replace('deploy_progress_interval_s', '60')
        self.text_setting('admin_secret_file', str(self.root / 'admin.gpg'))
        self.text_setting('deployment_profile', 'fixture-admin')
        approved_rows: tuple[tuple[Literal['gate-up', 'remove-images'], str, str], ...] = (
            ('gate-up', 'create-change-set', 'execute-change-set'),
            ('remove-images', 'get-template', 'delete-snapshot snap-old'),
        )
        for command, renew_on, release_on in approved_rows:
            with self.subTest(command=command):
                self.check_approved_gate_command(command, renew_on, release_on)

    def check_approved_gate_command(
        self, command: Literal['gate-up', 'remove-images'], renew_on: str, release_on: str,
    ) -> None:
        directory = self.root / 'approved' / command
        directory.mkdir(parents=True)
        _ = (directory / 'deployment.json').write_bytes((self.state / 'deployment.json').read_bytes())
        images = test_deploy.GateImageAws()
        images.manifest = directory / 'gate-deployment.json'
        if command == 'remove-images':
            _ = images.manifest.write_text(json.dumps({'regions': [{'region': GATE_HOME, 'image': 'ami-old'}]}))
            (directory / 'gate-image').mkdir()

        with self.hana_activity(directory, command, renew_on=renew_on, release_on=release_on) as (clock, observe):
            def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                with account_stack_lock(directory) as access:
                    self.assertIs(access, LockResult.BUSY)
                operation = arguments[arguments.index('--region') + 3]
                observe(operation + ' ' + arguments[arguments.index('--snapshot-id') + 1]
                        if operation == 'delete-snapshot' else operation)
                if operation == 'get-caller-identity':
                    return subprocess.CompletedProcess(arguments, 0, json.dumps({'Account': OWNER.account}), '')
                reply = images(arguments)
                if operation == 'describe-stacks':
                    body = ledger.mapping(cast(object, json.loads(reply.stdout)))
                    stack, = ledger.rows(body, 'Stacks')
                    stack['StackId'] = self.stack_id(GATE_HOME)
                    outputs = {'GateBucket': GATE_BUCKET, 'GateLaunchTemplate': 'lt-fixture',
                               'GateLaunchVersion': str(images.version), 'GateSecurityGroup': 'sg-fixture',
                               'GateRoleId': 'fixture-role', 'ScalerUserName': 'fixture-scaler'}
                    stack['Outputs'] = [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]
                    reply.stdout = json.dumps(body)
                return reply

            subject = deploy.Deployment(self.settings, directory, self.root / command, execute=execute, clock=clock)
            credentials = admin_secrets.AdminCredentials('fixture-key', 'fixture-secret', 'fixture-id', 'fixture-oauth')
            with (mock.patch.object(subject.admin, 'get', return_value=credentials), redirect_stdout(StringIO())):
                if command == 'gate-up':
                    with (mock.patch.object(aws_trial, 'host_nixpkgs_revision', return_value='fixture-revision'),
                          mock.patch.object(deploy, 'gate_subnets', return_value=[{'id': 'subnet-fixture', 'zone': GATE_HOME + 'a'}]),
                          mock.patch.object(subject, '_fill_prices'),
                          mock.patch.object(subject, '_probe_gate_gpu', return_value=True)):
                        self.assertTrue(subject.gate_up(approve_resources=True))
                else:
                    self.assertTrue(subject.remove_images(approve_destroy=True))

    def test_plan_reuses_saved_regional_image_by_id(self) -> None:
        self.failure, self.failure_region = 'image', GATE_REGION
        _ = (self.state / 'regional-images.json').write_text(json.dumps({
            f'{GATE_REGION}:ami-home': {'region': GATE_REGION, 'image': 'ami-' + GATE_REGION},
        }))
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls_in(GATE_REGION).count(('ec2', 'describe-images')), 2)

    def test_missing_home_image_refuses_all_regions(self) -> None:
        self.regions = (GATE_HOME, GATE_REGION, GATE_LAST)
        self.failure, self.failure_region = 'image', GATE_HOME
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertEqual(output.splitlines()[1:], [
            *(f'plan gate: {region}: refused: EC2 did not return the configured home gate image.'
              for region in self.regions),
            'plan run: refused: one or more gate regions failed',
        ])
        self.assertNotIn(('cloudformation', 'create-change-set'), self.calls_in(GATE_HOME))

    def test_missing_change_set_id_waits_for_late_creation_before_removal_proof(self) -> None:
        self.regions = (GATE_HOME,)
        self.failure, self.failure_region = 'create-missing-id', GATE_HOME
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        cleanup = [operation for service, operation in self.calls_in(GATE_HOME)
                   if service == 'cloudformation' and operation.endswith('-change-set')]
        self.assertEqual(cleanup, ['create-change-set', 'delete-change-set', 'describe-change-set',
                                   'describe-change-set', 'delete-change-set', 'describe-change-set'])
        self.assertEqual(output.count('plan stack: change set removal confirmed'), 1)
        self.assertIn('plan gate: us-east-2: refused: Id: expected nonempty text', output)

    def test_last_cleanup_check_can_retry_delete_and_confirm_removal(self) -> None:
        self.regions = (GATE_HOME,)
        self.failure, self.failure_region = 'delete-ready-last', GATE_HOME
        self.replace('stack_wait_timeout_s', '20')
        now = 0.0

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        with mock.patch.object(time, 'monotonic', side_effect=lambda: now), \
                mock.patch.object(time, 'sleep', side_effect=advance):
            status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        cleanup = [operation for service, operation in self.calls_in(GATE_HOME)
                   if service == 'cloudformation' and operation.endswith('-change-set')]
        self.assertEqual(cleanup, ['create-change-set', 'describe-change-set', 'delete-change-set',
                                   *(['describe-change-set'] * len(plan_identity.PROOF_RETRY_DELAYS)), 'delete-change-set',
                                   'describe-change-set'])
        self.assertIn('plan stack: change set removal confirmed', output)
        self.assertEqual(self.cleanup_timeouts[-1], (15.0, 5.0))
        self.assertTrue(all(start + duration <= 20 for start, duration in self.cleanup_timeouts))

    def test_cleanup_deadline_includes_delete_and_confirmation_calls(self) -> None:
        now = 0.0
        calls: list[tuple[str, float]] = []
        name = ''

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal now, name
            self.assertTrue(capture_output and text and not check)
            self.assertEqual((env or {}).get('AWS_ACCESS_KEY_ID'), CREDENTIAL.access_key_id)
            operation = arguments[4]
            calls.append((operation, timeout))
            if operation == 'create-change-set':
                name = arguments[arguments.index('--change-set-name') + 1]
                return subprocess.CompletedProcess(arguments, 0, '{"Id": "fixture-change-id"}', '')
            if len(calls) == 2:
                reply = '{"Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE"}'
                return subprocess.CompletedProcess(arguments, 0, reply, '')
            now += timeout
            if operation == 'delete-change-set':
                return subprocess.CompletedProcess(arguments, 0, '{}', '')
            raise subprocess.TimeoutExpired(operation, timeout)

        plan = plan_run.PlanRun(lambda: CREDENTIAL, execute, lambda _seconds: None)
        output = StringIO()
        with mock.patch.object(time, 'monotonic', side_effect=lambda: now), redirect_stdout(output):
            with self.assertRaisesRegex(plan_run.PlanRefused, '^change set not removed$'):
                with plan.change(plan.api(GATE_HOME, 30), self.stack_id(GATE_HOME), b'{}', [], 4) as change:
                    self.assertEqual(change['Status'], 'CREATE_COMPLETE')
        self.assertEqual(calls, [('create-change-set', 30), ('describe-change-set', 30),
                                 ('delete-change-set', 2), ('describe-change-set', 2)])
        self.assertEqual(now, 4)
        self.assertIn('plan stack: change set may remain: ' + name, output.getvalue())
        self.assertNotIn('plan stack: change set removal confirmed', output.getvalue())

    def test_cleanup_failure_keeps_legacy_policy_evidence_and_blocks_every_region(self) -> None:
        self.failure, self.failure_region = 'cleanup-fails', GATE_REGION
        self.displaced_policies = {GATE_REGION: {'GateLegacyPolicy': 'Remove'}}
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertEqual(list(self.change_sets), [GATE_REGION])
        self.assertIn('plan stack: change set may remain: ' + self.change_sets[GATE_REGION], output)
        self.assertIn('plan gate: us-west-2: refused: change set not removed', output)
        self.assertIn('plan gate: us-east-2: refused: Gate policy exclusions are incomplete '
                      + 'because the preview in us-west-2 did not pass. Policies found there: GateLegacyPolicy.', output)
        self.assertNotIn('pass (', output)
        self.assertFalse(any(service == 'iam' for _, service, _ in self.calls))

    def test_interrupted_preview_refuses_every_region(self) -> None:
        self.failure, self.failure_region = 'preview-interrupted', GATE_REGION
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        self.assertIn('plan gate: us-east-2: refused: Gate policy exclusions are incomplete', output)
        self.assertIn('plan gate: us-west-2: refused: The gate preview was interrupted.', output)
        self.assertTrue(output.endswith('plan run: refused: interrupted\n'))
        self.assertFalse(any(service == 'iam' for _, service, _ in self.calls))

    def test_preview_excludes_removed_and_replaced_policies_from_every_simulation(self) -> None:
        self.displaced_policies = {GATE_HOME: {'GateLegacyPolicy': 'Remove'},
                                   GATE_REGION: {'GateOldTrialPolicy': 'Modify'}}
        status, output = self.run_plan()
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls_in(GATE_HOME).count(('cloudformation', 'describe-stack-resource')), 3)
        self.assertEqual(self.calls_in(GATE_REGION).count(('cloudformation', 'describe-stack-resource')), 1)
        self.assertIn(('iam', 'ec2:TerminateInstances'), self.calls_in(GATE_HOME))
        self.assertIn(('iam', 'ec2:TerminateInstances'), self.calls_in(GATE_REGION))

    def test_unresolved_removed_policy_refuses_before_simulation(self) -> None:
        self.unresolved_policy = 'GateLegacyPolicy'
        self.displaced_policies = {GATE_REGION: {self.unresolved_policy: 'Remove'}}
        status, output = self.run_plan()
        self.assertEqual(status, 1, output)
        for region in self.regions:
            self.assertIn(f'plan gate: {region}: refused: CloudFormation did not identify the displaced '
                          + 'GateLegacyPolicy policy in us-west-2.', output)
        self.assertFalse(any(service == 'iam' for _, service, _ in self.calls))
        self.assertNotIn('pass (', output)

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
        for action in ('ec2:TerminateInstances', 'ec2:GetSpotPlacementScores'):
            with self.subTest(action=action):
                self.calls.clear()
                self.failure, self.failure_region = action, GATE_REGION
                status, output = self.run_plan()
                self.assertEqual(status, 1, output)
                self.assertIn(f'{GATE_REGION}: refused: Gate scaler {action} denied.', output)
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
            ('obsolete', 'The change set execution status is OBSOLETE. Expected AVAILABLE.'),
            ('ec2:TerminateInstances', 'Gate scaler ec2:TerminateInstances denied'),
            ('image', 'the regional gate image needs an EC2 copy'),
        ):
            with self.subTest(failure=failure):
                self.calls.clear()
                self.failure = failure
                status, output = self.run_plan()
                self.assertEqual(status, 1, output)
                self.assertIn(reason, output)
                outcome = 'pass (UPDATE' if failure == 'ec2:TerminateInstances' else 'refused: Gate policy exclusions'
                self.assertIn(f'plan gate: eu-north-1: {outcome}', output)
                lines = [line for line in output.splitlines() if line.startswith('plan gate:')]
                self.assertEqual([line.split()[2].rstrip(':') for line in lines], list(self.regions))
