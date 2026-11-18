"""Verify preparation, explicit approval, and isolated stack setup with private fakes."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import re
import subprocess
import time
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import aws_cli, deploy, hana_box, hana_image, hana_replacement, hana_spend, ledger, plan_run, remote
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.ledger_test_support import CostFixture, text_setting, write_hana_config
from scripts.aws_target.test_hana_box import FakeProvider
from scripts.aws_target.test_hana_provider import FakeAws, IDENTITY
from scripts.lib import conf


@final
class RootReplacementAws:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls: list[str] = []
        self.state = 'stopped'
        self.image = 'ami-fixture-original'
        self.root = 'vol-fixture-original'
        self.boot = 'uefi'
        self.data_retained = True
        self.attached_at = '2026-01-15T11:00:00Z'
        self.tasks: dict[str, dict[str, object]] = {}
        self.initial_task_states: list[str] = []
        self.tokens: list[str] = []
        self.die_after = ''
        self.create_refusal = ''
        self.interrupted = False
        self.attachment_changes = False
        self.stop_deferred = False
        self.root_gib = 20
        self.policy_installed = False

    def cloudformation(self, operation: str, arguments: tuple[str, ...]) -> dict[str, object]:
        saved = remote.read_record(self.directory / 'hana/setup.json')
        template = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_bytes())))
        resources = ledger.mapping(template['Resources'])
        outputs = {'ControlBucket': 'fixture-bucket', 'LaunchTemplate': 'fixture-template'}
        if saved['version'] == 1:
            outputs.update(Instance=IDENTITY.instance, DataVolume='vol-fixture-data')
            resources.update(Instance={'Type': 'AWS::EC2::Instance'}, DataVolume={'Type': 'AWS::EC2::Volume'},
                             DataAttachment={'Type': 'AWS::EC2::VolumeAttachment'})
        else:
            outputs.update(LaunchTemplateVersion='3', SecurityGroup='fixture-group')
        parameters = {key: 'fixture' for key in ledger.mapping(template['Parameters'])}
        parameters.update(DeploymentId=IDENTITY.deployment, ScalerUser='fixture-scaler')
        rows = [{'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()]
        if operation == 'describe-stacks':
            return {'Stacks': [{'StackId': 'fixture-stack', 'StackStatus': 'UPDATE_COMPLETE', 'Parameters': rows,
                'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}]}
        if operation == 'get-template':
            return {'TemplateBody': template}
        if operation == 'create-change-set':
            path = Path(arguments[arguments.index('--template-body') + 1].removeprefix('file://'))
            proposed = ledger.mapping(cast(object, json.loads(path.read_text())))
            proposed_resources = ledger.mapping(proposed['Resources'])
            policy = ledger.mapping(ledger.mapping(proposed_resources.pop('ScalerPolicy'))['Properties'])
            statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
            actions = tuple(row['Action'] for row in statements)
            if (proposed_resources != {key: value for key, value in resources.items() if key != 'ScalerPolicy'}
                    or actions.count('ec2:CreateReplaceRootVolumeTask') != 3 or 'ec2:CreateTags' in actions):
                raise AssertionError('The change set must install the scoped grants in ScalerPolicy.')
            return {}
        if operation == 'describe-change-set':
            return {'Status': 'CREATE_COMPLETE', 'Parameters': rows, 'Changes': [{'ResourceChange': {
                'LogicalResourceId': 'ScalerPolicy', 'ResourceType': 'AWS::IAM::ManagedPolicy',
                'Action': 'Modify', 'Replacement': 'False'}}]}
        if operation == 'execute-change-set':
            self.policy_installed = True
            return {}
        if operation == 'wait':
            return {}
        raise AssertionError(operation)

    def _mutation(self, operation: str) -> dict[str, object]:
        if operation == self.die_after:
            self.die_after = ''
            raise KeyboardInterrupt
        return {}

    def complete(self, task: dict[str, object]) -> None:
        task['TaskState'] = 'succeeded'
        self.image, self.root = 'ami-fixture-reviewed', 'vol-fixture-replacement'
        if self.attachment_changes:
            self.attached_at = '2026-01-15T11:01:00Z'

    def call(self, operation: str, *arguments: str, service: str = 'ec2', timeout_s: float | None = None
             ) -> dict[str, object]:
        _ = timeout_s
        self.calls.append(operation)
        if service == 'cloudformation':
            return self.cloudformation(operation, arguments)
        if operation == 'describe-instances':
            return {'Reservations': [{'Instances': [{
                'InstanceId': IDENTITY.instance, 'SpotInstanceRequestId': IDENTITY.spot_request,
                'InstanceLifecycle': 'spot', 'State': {'Name': self.state}, 'ImageId': self.image,
                'RootDeviceName': '/dev/xvda', 'RootDeviceType': 'ebs', 'CurrentInstanceBootMode': 'uefi',
                'Architecture': 'x86_64', 'VirtualizationType': 'hvm', 'PlatformDetails': 'Linux/UNIX',
                'UsageOperation': 'RunInstances', 'BlockDeviceMappings': [
                    {'DeviceName': '/dev/xvda', 'Ebs': {'VolumeId': self.root, 'DeleteOnTermination': True}},
                    {'DeviceName': '/dev/sdf', 'Ebs': {'VolumeId': 'vol-fixture-data',
                                                      'DeleteOnTermination': self.data_retained is False}},
                ],
            }]}]}
        if operation == 'describe-volumes':
            if arguments[-1] != 'vol-fixture-data':
                raise AssertionError(arguments)
            return {'Volumes': [{'VolumeId': 'vol-fixture-data', 'Attachments': [{
                'InstanceId': IDENTITY.instance, 'Device': '/dev/sdf', 'State': 'attached',
                'DeleteOnTermination': not self.data_retained, 'AttachTime': self.attached_at,
            }]}]}
        if operation == 'describe-images':
            return {'Images': [{'ImageId': 'ami-fixture-reviewed', 'OwnerId': '000000000000',
                'Name': 'nixos-hana-fixture', 'State': 'available', 'RootDeviceType': 'ebs',
                'BootMode': self.boot, 'Architecture': 'x86_64', 'VirtualizationType': 'hvm',
                'PlatformDetails': 'Linux/UNIX', 'UsageOperation': 'RunInstances', 'RootDeviceName': '/dev/xvda',
                'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {
                    'VolumeSize': self.root_gib, 'SnapshotId': 'snap-fixture-image'}}]}]}
        if operation == 'describe-spot-instance-requests':
            return {'SpotInstanceRequests': [{'SpotInstanceRequestId': IDENTITY.spot_request,
                'InstanceId': IDENTITY.instance, 'Type': 'persistent', 'InstanceInterruptionBehavior': 'stop',
                'State': 'disabled' if self.state == 'stopped' else 'active', 'Status': {
                    'Code': 'instance-stopped-no-capacity' if self.interrupted else 'instance-stopped-by-user'}}]}
        if operation == 'describe-replace-root-volume-tasks':
            if len(arguments) != 2 or arguments[0] != '--replace-root-volume-task-ids':
                raise AssertionError('Describe must select one task by ID.')
            match = [task for task in self.tasks.values() if task['ReplaceRootVolumeTaskId'] == arguments[1]]
            return {'ReplaceRootVolumeTasks': match}

        record = hana_replacement.read(self.directory)
        if isinstance(record, hana_replacement.NoReplacement):
            raise AssertionError('An AWS mutation requires the saved replacement.')
        if operation == 'start-instances':
            if self.interrupted:
                raise AssertionError('EC2 refuses to start an interrupted Spot instance.')
            self.state = 'running'
            return self._mutation(operation)
        if operation == 'stop-instances':
            if not self.stop_deferred:
                self.state = 'stopped'
            return self._mutation(operation)
        if operation == 'create-replace-root-volume-task':
            token = arguments[arguments.index('--client-token') + 1]
            self.tokens.append(token)
            if token != record.token or '--delete-replaced-root-volume' not in arguments or '--tag-specifications' in arguments:
                raise AssertionError('The task must use the saved token and delete the old root.')
            if self.state != 'running':
                raise AssertionError('EC2 needs a running instance to create the root task.')
            if self.create_refusal:
                raise aws_cli.AwsUnavailable(self.create_refusal)
            if token in self.tasks:
                return {'ReplaceRootVolumeTask': self.tasks[token]}
            state = self.initial_task_states.pop(0) if self.initial_task_states else 'succeeded'
            task: dict[str, object] = {'ReplaceRootVolumeTaskId': 'replacevol-fixture-' + str(len(self.tasks)),
                    'InstanceId': IDENTITY.instance, 'ImageId': 'ami-fixture-reviewed', 'TaskState': state,
                    'DeleteReplacedRootVolume': True}
            self.tasks[token] = task
            if state == 'succeeded':
                self.complete(task)
            _ = self._mutation(operation)
            return {'ReplaceRootVolumeTask': task}
        raise AssertionError(operation)


class HanaSetupTests(CostFixture):
    workload: Path = Path()
    clock: TestClock = TestClock()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.workload = write_hana_config()
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'private setup inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'private setup account'))

    def subject(self, fake: FakeAws) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.root / 'output', execute=fake, clock=self.clock, notify=lambda _event: True)

    def test_prepare_is_local_and_saves_separate_configuration_evidence(self) -> None:
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        # The existing validator reads account metadata before Hana replaces resource inputs.
        with mock.patch.object(deploy, 'read_deployment_settings') as read:
            read.return_value = self.base_settings()
            prepared = subject.prepare_hana()
            again = subject.prepare_hana()
        self.assertEqual(prepared.fingerprint, again.fingerprint)
        self.assertEqual(fake.calls, [])
        settings = hana_spend.read_settings(conf.read(self.workload))
        self.assertEqual(prepared.parameters['RootGiB'], str(settings.root_gib))
        self.assertEqual(prepared.parameters['DataGiB'], str(settings.data_gib))
        self.assertEqual(prepared.parameters['SpotMaxPrice'], str(settings.spot_max_usd_per_hour))
        self.assertTrue((subject.output / 'hana/preflight.json').exists())
        self.assertFalse((subject.output / 'preflight.json').exists())
        self.assertFalse(settings.enabled)

    def base_settings(self) -> DeploymentSettings:
        return DeploymentSettings('us-east-2', 'fixture-admin', 'fixture-gates', 'fixture-image',
            '000000000000', 'fixture-release', 'us-east-2b', 'fixture-vpc', 'fixture-subnet', '/dev/xvda',
            1, 2, 3, conf.read(self.workload).whole('aws_timeout_s'),
            conf.read(self.settings).whole('preflight_max_age_s'), 'fixture-scaler')

    def test_setup_requires_current_preparation_before_any_aws_call(self) -> None:
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch('sys.stdout', new_callable=StringIO):
            _ = subject.prepare_hana()
            data_gib = conf.read(self.workload).whole('data_gib')
            _ = self.workload.write_text(self.workload.read_text().replace(f'data_gib={data_gib}\n', f'data_gib={data_gib + 1}\n'))
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'preparation expired|inputs changed'):
                _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.calls, [])

    @contextmanager
    def root_replacement(self, *, version: int = 2, plan: bool = False, install_policy: bool = False
                         ) -> Generator[tuple[deploy.Deployment, RootReplacementAws, StringIO]]:
        aws = RootReplacementAws(self.state)
        workload = conf.read(self.workload)
        mode = plan_run.PlanRun(mock.Mock(), mock.Mock(), mock.Mock()) if plan else plan_run.APPROVED_SETUP
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', clock=self.clock, mode=mode)
        path = self.state / 'hana/setup.json'
        path.parent.mkdir(exist_ok=True)
        setup = {'version': version, 'requested': True, 'generation': 0, 'region': IDENTITY.region,
                 'stack': workload.text('stack_name'), 'instance': IDENTITY.instance, 'spot_request': IDENTITY.spot_request,
                 'deployment': IDENTITY.deployment, 'data_volume': 'vol-fixture-data', 'bucket': 'fixture-bucket',
                 'parameters_sha256': 'fixture', 'template_sha256': 'fixture'}
        if version == 2:
            setup.update(launch_template='LaunchTemplateId=fixture-template,Version=3', security_group='fixture-group')
        _ = path.write_text(json.dumps(setup))
        path.with_name('lifecycle.json').unlink(missing_ok=True)
        hana_box.HanaController(self.state, self.settings, self.workload, hana_box.ProviderNotInstalled()).register(IDENTITY)
        _ = hana_image.save(self.state, hana_image.RegisteredImage('nixos-hana-fixture', 'ami-fixture-reviewed',
            '000000000000', 'vpc-fixture', 'subnet-fixture'))

        def checks(_timeout: int, _interval: int) -> Generator[float]:
            for _ in range(12):
                self.clock.advance(1)
                yield self.clock()

        api = aws_cli.AwsCli('fixture-admin', IDENTITY.region, 1, mock.Mock(side_effect=AssertionError))
        provider = mock.Mock()
        provider.api = api
        with (mock.patch.object(api, 'call', side_effect=aws.call),
              mock.patch.object(api, 'in_region', return_value=api),
              mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()),
              mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_check_credentials'),
              mock.patch.object(subject, '_api', return_value=api),
              mock.patch.object(subject, '_hana_provider', return_value=provider),
              nullcontext() if install_policy else mock.patch.object(subject, '_hana_policy_stack'),
              nullcontext() if install_policy else mock.patch.object(subject, '_install_hana_policy'),
              mock.patch.object(subject, '_preview_hana_policy'),
              mock.patch.object(subject, '_hana_check_times', side_effect=checks),
              mock.patch.object(plan_run.PlanRun, 'confirm_account'),
              mock.patch('sys.stdout', new_callable=StringIO) as output):
            yield subject, aws, output

    def assert_root_replaced(self, aws: RootReplacementAws, output: StringIO) -> None:
        self.assertIn('Hana image replacement complete. The box is stopped.', output.getvalue())
        self.assertFalse(hana_replacement.held(self.state))
        setup = remote.read_record(self.state / 'hana/setup.json')
        self.assertEqual((setup['image'], setup['instance'], setup['spot_request'], setup['data_volume']),
                         ('ami-fixture-reviewed', IDENTITY.instance, IDENTITY.spot_request, 'vol-fixture-data'))
        self.assertEqual(aws.state, 'stopped')
        self.assertEqual(aws.attached_at, '2026-01-15T11:00:00Z')
        lifecycle = remote.read_record(self.state / 'hana/lifecycle.json')
        self.assertEqual(lifecycle['state'], 'idle-stopped')
        self.assertEqual(lifecycle['claims'], [])
        self.assertIsNone(lifecycle['delivered'])
        self.assertNotIn('run-instances', aws.calls)
        self.assertNotIn('create-change-set', aws.calls)

    def test_root_replacement_preserves_both_ownership_versions_and_reports_current(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version), self.root_replacement(version=version) as (subject, aws, output):
                self.assertTrue(subject.hana_setup(approve_resources=True))
                self.assert_root_replaced(aws, output)
                self.assertEqual(remote.read_record(self.state / 'hana/setup.json')['version'], version)
                aws.calls.clear()
                self.assertTrue(subject.hana_setup(approve_resources=True))
                self.assertTrue(output.getvalue().endswith('The Hana image is current.\n'))
                self.assertTrue(all(call.startswith('describe-') for call in aws.calls))

    def test_root_replacement_installs_the_scaler_policy_before_starting(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version), self.root_replacement(version=version, install_policy=True) as (subject, aws, _output):
                self.assertTrue(subject.hana_setup(approve_resources=True))
                self.assertTrue(aws.policy_installed)
                self.assertLess(aws.calls.index('execute-change-set'), aws.calls.index('start-instances'))

    def test_root_replacement_does_not_start_an_interrupted_box(self) -> None:
        with self.root_replacement() as (subject, aws, _output):
            aws.interrupted = True
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, '^' + re.escape(hana_replacement.INTERRUPTED) + '$'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertNotIn('start-instances', aws.calls)
            self.assertTrue(hana_replacement.held(self.state))

    def test_root_replacement_replays_the_token_and_polls_the_returned_task_id(self) -> None:
        with self.root_replacement() as (subject, aws, output):
            aws.initial_task_states = ['in-progress']
            aws.die_after = 'create-replace-root-volume-task'
            with self.assertRaises(KeyboardInterrupt):
                _ = subject.hana_setup(approve_resources=True)
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(aws.tokens, [aws.tokens[0], aws.tokens[0]])
            self.assertIn('describe-replace-root-volume-tasks', aws.calls)
            aws.complete(aws.tasks[aws.tokens[0]])
            self.assertTrue(subject.hana_setup(approve_resources=True))
            self.assert_root_replaced(aws, output)

    def test_root_replacement_publishes_when_aws_stops_the_done_box(self) -> None:
        with self.root_replacement() as (subject, aws, output):
            aws.initial_task_states = ['in-progress']
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)
            aws.complete(aws.tasks[aws.tokens[0]])
            aws.interrupted = True
            aws.calls.clear()
            self.assertTrue(subject.hana_setup(approve_resources=True))
            self.assert_root_replaced(aws, output)
            self.assertNotIn('start-instances', aws.calls)
            self.assertNotIn('describe-replace-root-volume-tasks', aws.calls)
            self.assertEqual(aws.calls.count('stop-instances'), 1)

    def test_root_replacement_hands_an_unconfirmed_stop_to_the_controller(self) -> None:
        with self.root_replacement() as (subject, aws, output):
            aws.stop_deferred = True
            self.assertTrue(subject.hana_setup(approve_resources=True))
            self.assertIn('Hana image replacement complete. The controller stops the box.', output.getvalue())
            self.assertEqual(aws.calls.count('stop-instances'), 1)
            self.assertEqual(remote.read_record(self.state / 'hana/lifecycle.json')['state'], 'stopping')
            self.assertFalse(hana_replacement.held(self.state))
            compute, = ledger.CostLedger(self.state).read_snapshot().hana_compute
            self.assertIsInstance(compute.state, ledger.HanaExposureClosed)

    def test_root_replacement_stops_when_the_attachment_timestamp_changes(self) -> None:
        with self.root_replacement() as (subject, aws, _output):
            aws.attachment_changes = True
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'data attachment changed'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(aws.calls.count('stop-instances'), 1)
            self.assertNotIn('image', remote.read_record(self.state / 'hana/setup.json'))
            self.assertTrue(hana_replacement.held(self.state))
            compute, = ledger.CostLedger(self.state).read_snapshot().hana_compute
            self.assertIsInstance(compute.state, ledger.HanaExposureOpen)

    def test_the_controller_stops_the_held_box_at_the_reservation_deadline(self) -> None:
        with self.root_replacement() as (subject, aws, _output):
            aws.initial_task_states = ['in-progress']
            aws.stop_deferred = True
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(aws.state, 'running')
            exposure, = ledger.CostLedger(self.state).read_snapshot().hana_compute
            self.assertEqual(exposure.expires - exposure.at, conf.read(self.workload).whole('stop_wait_timeout_s'))
            provider = FakeProvider(self.state, self.clock, state='running')
            controller = hana_box.HanaController(self.state, self.settings, self.workload, provider, clock=self.clock)
            self.assertEqual(controller.request('fixture'), hana_box.ClaimRefused(hana_replacement.HELD))
            self.assertEqual(controller.permission('fixture', 'fixture-boot', 1),
                             hana_box.ClaimRefused(hana_replacement.HELD))
            self.assertEqual(controller.tick(), hana_box.Lifecycle.IDLE_STOPPED)
            self.clock.advance(exposure.expires - self.clock.now)
            self.assertEqual(controller.tick(), hana_box.Lifecycle.STOPPING)
            self.assertEqual(controller.tick(), hana_box.Lifecycle.STOPPING)
            self.assertEqual(controller.tick(), hana_box.Lifecycle.IDLE_STOPPED)
            self.assertEqual(provider.state, 'stopped')
            self.assertTrue(hana_replacement.held(self.state))

    def test_stopped_observation_leaves_the_renewed_replacement_reservation_open(self) -> None:
        with self.root_replacement() as (subject, aws, _output):
            aws.initial_task_states = ['in-progress']
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)

            book = ledger.CostLedger(self.state)
            original, = book.read_snapshot().hana_compute
            self.clock.advance(original.expires - self.clock.now)
            spending = hana_spend.HanaSpending(book, self.settings, hana_spend.read_settings(conf.read(self.workload)))
            deadline = self.clock.now + 60

            def renew() -> None:
                result = spending.reserve_setup(self.clock.now, deadline, expected_generation=original.generation)
                self.assertIsInstance(result, hana_spend.StartReserved)

            provider = FakeProvider(self.state, self.clock, during_call=renew)
            controller = hana_box.HanaController(self.state, self.settings, self.workload, provider, clock=self.clock)
            self.assertEqual(controller.tick(), hana_box.Lifecycle.IDLE_STOPPED)
            self.assertEqual([call.action for call in provider.calls], [hana_box.Action.OBSERVE])
            renewed, = book.read_snapshot().hana_compute
            self.assertEqual((renewed.generation, renewed.expires), (original.generation, deadline))
            self.assertIsInstance(renewed.state, ledger.HanaExposureOpen)
            self.assertTrue(hana_replacement.held(self.state))

    def test_root_replacement_reports_a_failed_task_then_uses_a_new_token(self) -> None:
        for state in ('failed', 'failed-detached'):
            with self.subTest(state=state), self.root_replacement() as (subject, aws, output):
                aws.initial_task_states = [state]
                message = f'The Hana root task {state}. AWS returned no status message.'
                if state == 'failed-detached':
                    message += (' EC2 detached the root volume. The data volume is intact.'
                                ' Run deploy.py hana-setup --approve-resources again.')
                with self.assertRaisesRegex(hana_replacement.ReplacementRefused, '^' + re.escape(message) + '$'):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(len(aws.tokens), 1)
                self.assertEqual(aws.calls[-1], 'stop-instances')
                self.assertTrue(hana_replacement.held(self.state))
                if state == 'failed-detached':
                    aws.create_refusal = 'An error occurred (IncorrectInstanceState) when calling the CreateReplaceRootVolumeTask operation: The instance has no root volume.'
                    with self.assertRaisesRegex(hana_replacement.ReplacementRefused, '^' + re.escape(aws.create_refusal) + '$'):
                        _ = subject.hana_setup(approve_resources=True)
                    self.assertEqual(len(set(aws.tokens)), 2)
                    self.assertEqual(aws.calls[-1], 'stop-instances')
                    self.assertEqual(output.getvalue(), '')
                    aws.create_refusal = ''
                self.assertTrue(subject.hana_setup(approve_resources=True))
                self.assert_root_replaced(aws, output)
                self.assertEqual(len(set(aws.tokens)), 2)

    def test_root_replacement_renews_a_running_attempt_only_within_the_cap(self) -> None:
        with self.root_replacement() as (subject, aws, _output):
            book = ledger.CostLedger(self.state)
            aws.initial_task_states = ['in-progress']
            aws.stop_deferred = True
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)
            original, = book.read_snapshot().hana_compute
            self.clock.advance(original.expires - self.clock.now - 2)
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)
            extended, = book.read_snapshot().hana_compute
            self.assertEqual((extended.at, extended.generation), (original.at, original.generation))
            timeout = original.expires - original.at
            self.assertEqual(extended.expires, original.expires - 1 + timeout)
            self.assertIsInstance(extended.state, ledger.HanaExposureOpen)

            self.clock.advance(extended.expires - self.clock.now - 2)
            minimum = conf.read(self.settings).whole('box_minimum_seconds')
            reserved = ledger.hana_intervals(book.read_snapshot(), self.clock.now, minimum, reserve=True)
            cost = sum((interval.usd for interval in reserved), Decimal(0))
            text_setting(self.workload, 'daily_budget_usd', str(cost + Decimal('.001')))
            aws.calls.clear()
            aws.stop_deferred = False
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'hana_daily_budget_usd'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(book.read_snapshot().hana_compute, [extended])
            self.assertEqual(aws.state, 'stopped')
            self.assertEqual(aws.calls[-1], 'stop-instances')
            self.assertNotIn('start-instances', aws.calls)
            self.assertNotIn('create-replace-root-volume-task', aws.calls)

    def test_root_replacement_plan_refuses_without_a_registration(self) -> None:
        with self.root_replacement(version=1, plan=True) as (subject, aws, output):
            (self.state / hana_image.RECORD).unlink()
            with self.assertRaisesRegex(deploy.DeploymentRefused, re.escape(hana_replacement.REGISTER_IMAGE)):
                _ = subject.hana_setup()
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(aws.calls, [])

    def test_root_replacement_plan_checks_the_instance_during_a_pending_attempt(self) -> None:
        with self.root_replacement(version=1, plan=True) as (subject, aws, output):
            hana_replacement.save(self.state, hana_replacement.ReplacementAttempt(
                'ami-fixture-reviewed', aws.attached_at, 'fixture-token', 0))
            self.assertTrue(subject.hana_setup())
            self.assertIn('Resume the pending root replacement.', output.getvalue())
            self.assertIn('describe-instances', aws.calls)

    def test_root_replacement_plan_refuses_a_terminated_box_with_a_pending_attempt(self) -> None:
        with self.root_replacement(plan=True) as (subject, aws, output):
            hana_replacement.save(self.state, hana_replacement.ReplacementAttempt(
                'ami-fixture-reviewed', aws.attached_at, 'fixture-token', 0))
            aws.state = 'terminated'
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'cannot resume root replacement'):
                _ = subject.hana_setup()
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(aws.calls, ['describe-instances'])

    def test_root_replacement_rerun_checks_the_current_root_size_before_any_write(self) -> None:
        with self.root_replacement() as (subject, aws, output):
            aws.initial_task_states = ['in-progress']
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'has not finished'):
                _ = subject.hana_setup(approve_resources=True)
            record = hana_replacement.record_path(self.state).read_bytes()
            snapshot = ledger.CostLedger(self.state).read_snapshot()
            size = conf.read(self.workload).whole('root_gib')
            _ = self.workload.write_text(self.workload.read_text().replace(f'root_gib={size}\n',
                                                                          f'root_gib={aws.root_gib - 1}\n'))
            aws.calls.clear()
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused, 'registered Hana image root is'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(aws.calls, ['describe-instances', 'describe-images'])
            self.assertEqual(hana_replacement.record_path(self.state).read_bytes(), record)
            self.assertEqual(ledger.CostLedger(self.state).read_snapshot(), snapshot)
            self.assertEqual(output.getvalue(), '')

    def test_root_replacement_refuses_an_image_larger_than_the_configured_root(self) -> None:
        with self.root_replacement() as (subject, aws, output):
            size = conf.read(self.workload).whole('root_gib')
            aws.root_gib = size + 1
            with self.assertRaisesRegex(hana_replacement.ReplacementRefused,
                                        f'{size + 1} GiB.*{size} GiB'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(aws.calls, ['describe-instances', 'describe-images'])
            self.assertFalse(hana_replacement.held(self.state))

    def test_root_replacement_plan_has_no_provider_or_local_writes(self) -> None:
        with self.root_replacement(version=1, plan=True) as (subject, aws, output):
            def snapshot() -> dict[Path, bytes]:
                return {path: path.read_bytes() for path in self.state.rglob('*') if path.is_file()}

            before = snapshot()
            self.assertTrue(subject.hana_setup())
            self.assertEqual(snapshot(), before)
            self.assertEqual(output.getvalue(), 'Plan Hana: Replace the root volume of the box with the registered image.\n' +
                             'Plan Hana: Keep the instance and the data volume.\n')
            self.assertTrue(all(call.startswith('describe-') for call in aws.calls))

    def test_root_replacement_preflight_refuses_without_a_mutation(self) -> None:
        for case, message in (
                ('busy', 'must be stopped'), ('claim', 'lifecycle is busy'), ('registration', 'no verified image'),
                ('boot', 'incompatible'), ('volume', 'retained Hana data attachment'), ('identity', 'registration differs')):
            with self.subTest(case=case), self.root_replacement() as (subject, aws, output):
                lifecycle = self.state / 'hana/lifecycle.json'
                row = remote.read_record(lifecycle)
                if case == 'busy':
                    aws.state = 'running'
                elif case == 'claim':
                    row['claims'] = [{'execution': 'fixture-active', 'token': 'fixture-claim',
                                      'requested_at': self.clock.now, 'active': True}]
                elif case == 'registration':
                    _ = hana_image.save(self.state, hana_image.PendingImageRegistration('fixture', 'snap-fixture'))
                elif case == 'boot':
                    aws.boot = 'legacy-bios'
                elif case == 'volume':
                    aws.data_retained = False
                else:
                    ledger.mapping(row['identity'])['instance'] = 'i-fixture-mismatch'
                _ = lifecycle.write_text(json.dumps(row))
                with self.assertRaisesRegex(ValueError, message):
                    _ = subject.hana_setup(approve_resources=True)
                self.assertEqual(output.getvalue(), '')
                self.assertFalse(hana_replacement.held(self.state))
                self.assertTrue(all(call.startswith('describe-') for call in aws.calls))

    def setup_api(self, fake: FakeAws, calls: list[str], spot_price: Callable[[], Decimal], *, stop_state: str = '',
                  launch: Callable[[tuple[str, ...]], None] = lambda _arguments: None,
                  volume_ready: Callable[[], None] = lambda: None
                  ) -> Callable[..., dict[str, object]]:
        workload = conf.read(self.workload)
        limits = hana_spend.read_settings(workload)
        zone = workload.text('availability_zone')
        other_zone = 'us-east-2c'
        saved_path = self.state / 'hana/setup.json'
        version = 2
        attached = False

        def api_call(operation: str, *arguments: str, service: str = 'ec2', timeout_s: float | None = None) -> dict[str, object]:
            nonlocal version, attached
            calls.append(operation)
            if operation == 'describe-subnets':
                return {'Subnets': [
                    {'SubnetId': 'fixture-other-subnet', 'VpcId': 'fixture-vpc', 'AvailabilityZone': other_zone,
                     'State': 'available', 'DefaultForAz': True},
                    {'SubnetId': 'fixture-private-subnet', 'VpcId': 'fixture-vpc', 'AvailabilityZone': 'us-east-2a',
                     'State': 'available', 'DefaultForAz': False},
                ]}
            if operation == 'describe-spot-price-history':
                return {'SpotPriceHistory': [{
                    'InstanceType': size, 'AvailabilityZone': priced_zone, 'SpotPrice': str(price),
                    'Timestamp': datetime.fromtimestamp(self.clock.now, UTC).isoformat(),
                } for size, priced_zone, price in (
                    (limits.instance_type, zone, spot_price()),
                    (limits.fallback_instance_types[0], other_zone, '0.10'),
                    (limits.fallback_instance_types[1], zone, '0.12'),
                    (limits.fallback_instance_types[2], other_zone, limits.spot_max_usd_per_hour + 1),
                    (limits.instance_type, 'us-east-2a', '0.01'),
                )]}
            if operation == 'execute-change-set':
                evidence = ledger.CostLedger(self.state).read_snapshot()
                self.assertTrue(any(isinstance(row.state, ledger.HanaExposureOpen) for row in evidence.hana_compute))
                self.assertTrue(evidence.hana_storage)
                self.assertEqual(service, 'cloudformation')
                self.assertIn(workload.text('stack_name'), arguments)
                version += 1
                return {}
            if operation == 'describe-change-set':
                return {'Status': 'CREATE_COMPLETE', 'Changes': []}
            if operation == 'create-change-set':
                rows = cast(object, json.loads(arguments[arguments.index('--parameters') + 1]))
                self.assertFalse({'InstanceType', 'SubnetId', 'AvailabilityZone', 'DataGiB', 'FallbackInstanceTypes'}
                                 & {row['ParameterKey'] for row in ledger.rows({'rows': rows}, 'rows')})
                return {}
            if operation == 'wait' and service == 'cloudformation':
                return {}
            if operation == 'describe-stacks':
                if version == 2:
                    raise aws_cli.AwsUnavailable('ValidationError: Stack does not exist')
                outputs = {'ControlBucket': 'fixture-bucket', 'LaunchTemplate': 'fixture-template',
                           'LaunchTemplateVersion': str(version), 'SecurityGroup': 'fixture-group'}
                return {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': [
                    {'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}]}
            if operation == 'describe-stack-resources':
                return {'StackResources': []}
            if operation == 'run-instances':
                saved = remote.read_record(saved_path)
                self.assertEqual(ledger.mapping(saved['pending_launch'])['token'], arguments[arguments.index('--client-token') + 1])
                self.assertIn(f'LaunchTemplateId=fixture-template,Version={version}', arguments)
                launch(arguments)
                return {'Instances': [{'InstanceId': IDENTITY.instance, 'SpotInstanceRequestId': IDENTITY.spot_request}]}
            if operation == 'wait' and arguments[0] == 'instance-exists':
                self.assertEqual(timeout_s, conf.read(self.settings).whole('stack_wait_timeout_s'))
                saved = remote.read_record(saved_path)
                self.assertEqual(saved['instance'], IDENTITY.instance)
                self.assertEqual(saved['spot_request'], IDENTITY.spot_request)
                return {}
            if operation == 'create-volume':
                saved = remote.read_record(saved_path)
                self.assertEqual(arguments[arguments.index('--availability-zone') + 1], saved['zone'])
                token = arguments[arguments.index('--client-token') + 1]
                self.assertEqual(saved['pending_volume'], token)
                self.assertIn(token, arguments[arguments.index('--tag-specifications') + 1])
                self.assertEqual(arguments[arguments.index('--size') + 1], str(limits.data_gib))
                self.assertIn('--encrypted', arguments)
                self.assertIn('gp3', arguments)
                return {'VolumeId': 'fixture-volume'}
            if operation == 'wait' and arguments[0].startswith('volume-'):
                self.assertEqual(remote.read_record(saved_path)['data_volume'], 'fixture-volume')
                if arguments[0] == 'volume-available':
                    volume_ready()
                return {}
            if operation == 'describe-volumes':
                return {'Volumes': [{'VolumeId': 'fixture-volume', 'AvailabilityZone': remote.read_record(saved_path)['zone'],
                    'Attachments': [{'InstanceId': IDENTITY.instance, 'Device': '/dev/sdf', 'State': 'attached'}] if attached else []}]}
            if operation == 'attach-volume':
                self.assertIn(fake.state, ('running', 'stopped'))
                self.assertEqual(arguments, ('--volume-id', 'fixture-volume', '--instance-id', IDENTITY.instance,
                                             '--device', '/dev/sdf'))
                attached = True
                return {}
            self.assertNotEqual(operation, 'wait')
            result = fake(['aws', '--profile', 'fixture-admin', '--region', IDENTITY.region,
                service, operation, *arguments, '--output', 'json'], capture_output=True, text=True,
                timeout=workload.whole('aws_timeout_s') if timeout_s is None else timeout_s, check=False)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, 'fake')
            if operation == 'stop-instances' and stop_state:
                fake.state = stop_state
            return ledger.mapping(cast(object, json.loads(result.stdout)))
        return api_call

    def stop_progress(self, output: StringIO) -> list[str]:
        return [line for line in output.getvalue().splitlines() if line.startswith('waiting for the Hana box to stop:')]

    def test_setup_reserves_costs_before_creating_resources_and_registers_only_after_stop(self) -> None:
        fake = FakeAws(self.clock, state='pending')
        subject = self.subject(fake)
        limits = hana_spend.read_settings(conf.read(self.workload))
        calls: list[str] = []
        attempts: list[tuple[str, ...]] = []
        pauses: list[float] = []
        other_zone = 'us-east-2c'
        saved_path = self.state / 'hana/setup.json'

        def launch(arguments: tuple[str, ...]) -> None:
            attempts.append(arguments)
            if len(attempts) == 1:
                raise aws_cli.AwsUnavailable(
                    'aws: [ERROR]: An error occurred (InsufficientInstanceCapacity) '
                    + 'when calling the RunInstances operation: capacity unavailable')

        api_call = self.setup_api(fake, calls, lambda: limits.spot_max_usd_per_hour, stop_state='stopping', launch=launch)

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            self.clock.advance(int(seconds))
            self.assertFalse((self.state / 'hana/lifecycle.json').exists())
            if len(pauses) == 1:
                fake.state = 'running'
            if len(pauses) == 3:
                fake.state = 'stopped'

        subject.pause = pause
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs') as validate, \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch.object(time, 'monotonic', self.clock), mock.patch('sys.stdout', new_callable=StringIO) as output:
            prepared = subject.prepare_hana()
            saved = remote.read_record(saved_path)
            saved['deployment'] = IDENTITY.deployment
            _ = saved_path.write_text(json.dumps(saved))
            # Refresh preparation for the chosen fake deployment identity.
            _ = subject.prepare_hana()
            self.assertTrue(subject.hana_setup(approve_resources=True))
            validate.assert_called_once()
        record = remote.read_record(self.state / 'hana/lifecycle.json')
        self.assertEqual(record['state'], hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(ledger.mapping(record['identity'])['spot_request'], IDENTITY.spot_request)
        self.assertIn('stop-instances', calls)
        self.assertEqual(pauses, [10, 10, 10])
        volume = calls.index('create-volume')
        self.assertEqual(calls[volume:volume + 5], ['create-volume', 'wait', 'describe-instances', 'describe-instances', 'attach-volume'])
        self.assertEqual(self.stop_progress(output), [
            'waiting for the Hana box to stop: stopping, 0 s of 600 s',
            'waiting for the Hana box to stop: stopping, 10 s of 600 s',
            'waiting for the Hana box to stop: stopped, 20 s of 600 s',
        ])
        stop = fake.calls.index(('ec2', 'stop-instances', 10))
        probe = fake.calls.index(('configure', 'export-credentials', 10))
        self.assertEqual(fake.calls[stop + 1:probe], [('ec2', 'describe-instances', 10)] * 3)
        self.assertLess(calls.index('describe-spot-price-history'), calls.index('create-change-set'))
        self.assertLess(calls.index('create-change-set'), calls.index('execute-change-set'))
        first_launch = calls.index('run-instances')
        self.assertEqual(calls[first_launch:first_launch + 2], ['run-instances', 'run-instances'])
        first, second = attempts
        self.assertIn(limits.instance_type, first)
        self.assertIn('fixture-subnet', first)
        self.assertIn(limits.fallback_instance_types[0], second)
        self.assertIn('fixture-other-subnet', second)
        self.assertNotEqual(first[first.index('--client-token') + 1], second[second.index('--client-token') + 1])
        self.assertIn('LaunchTemplateId=fixture-template,Version=3', second)
        self.assertIn('fixture-group', second)
        self.assertEqual([line.split(':', 1)[0] for line in output.getvalue().splitlines() if line.startswith('attempt ')],
                         ['attempt 1 of 3', 'attempt 1 failed', 'attempt 2 of 3'])
        self.assertIn(f'skip {limits.instance_type} in {other_zone}. AWS lists no Spot price.', output.getvalue())
        self.assertIn(f'skip {limits.fallback_instance_types[2]} in {other_zone}.', output.getvalue())
        self.assertIn('USD/h exceeds', output.getvalue())
        self.assertNotIn('us-east-2a', output.getvalue())
        compute, = ledger.CostLedger(self.state).read_snapshot().hana_compute
        self.assertEqual(compute.instance_type, limits.fallback_instance_types[0])
        self.assertNotIn('deploy', calls)
        self.assertNotIn('start-instances', calls)
        self.assertFalse((self.state / 'gate-deployment.json').exists())
        self.assertEqual(conf.read(self.workload).text('enabled'), 'off')
        self.assertEqual(prepared.parameters['InstanceType'], conf.read(self.workload).text('instance_type'))

    @contextmanager
    def prepared_stop_setup(self, fake: FakeAws, calls: list[str]) -> Generator[tuple[deploy.Deployment, StringIO]]:
        subject = self.subject(fake)
        ceiling = hana_spend.read_settings(conf.read(self.workload)).spot_max_usd_per_hour
        api_call = self.setup_api(fake, calls, lambda: ceiling, stop_state='stopping')
        output = StringIO()
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs'), \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch.object(time, 'monotonic', self.clock), mock.patch('sys.stdout', output):
            _ = subject.prepare_hana()
            yield subject, output

    def test_stop_timeout_preserves_setup_and_resumes_after_preparation_expires(self) -> None:
        _ = self.workload.write_text(self.workload.read_text().replace('stop_wait_timeout_s=600\n', 'stop_wait_timeout_s=25\n'))
        self.replace('preflight_max_age_s', '20')
        fake = FakeAws(self.clock, state='running')
        calls: list[str] = []
        pauses: list[float] = []

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            self.clock.advance(int(seconds))

        with self.prepared_stop_setup(fake, calls) as (subject, output):
            subject.pause = pause
            with self.assertRaisesRegex(deploy.DeploymentRefused,
                                        r'The Hana box is not yet stopped\. Run deploy\.py hana-setup --approve-resources again'):
                _ = subject.hana_setup(approve_resources=True)
            self.assertEqual(pauses, [10, 10, 5])
            self.assertEqual(self.stop_progress(output), [
                'waiting for the Hana box to stop: stopping, 0 s of 25 s',
                'waiting for the Hana box to stop: stopping, 10 s of 25 s',
                'waiting for the Hana box to stop: stopping, 20 s of 25 s',
            ])
            self.assertFalse((self.state / 'hana/lifecycle.json').exists())
            saved_path = self.state / 'hana/setup.json'
            saved = remote.read_record(saved_path)
            self.assertIs(saved['stop_pending'], True)
            self.assertEqual(saved['instance'], IDENTITY.instance)
            stop = fake.calls.index(('ec2', 'stop-instances', 10))
            self.assertEqual(fake.calls[stop + 1:], [('ec2', 'describe-instances', 10)] * 3)
            self.assertRegex(output.getvalue().splitlines()[-1], r'^hana-setup: confirming the idle stop failed after ')
            fake.state = 'stopped'
            calls.clear()
            self.assertTrue(subject.hana_setup(approve_resources=True))
        self.assertNotIn('create-change-set', calls)
        self.assertNotIn('stop-instances', calls)
        del saved['stop_pending']
        self.assertEqual(remote.read_record(saved_path), saved)

    def test_setup_polls_an_already_stopped_box_once(self) -> None:
        fake = FakeAws(self.clock, state='stopped')
        calls: list[str] = []
        with self.prepared_stop_setup(fake, calls) as (subject, output), mock.patch.object(subject, 'pause') as pause:
            self.assertTrue(subject.hana_setup(approve_resources=True))
            pause.assert_not_called()
        self.assertNotIn('stop-instances', calls)
        self.assertEqual(self.stop_progress(output), ['waiting for the Hana box to stop: stopped, 0 s of 600 s'])
        attachment = calls.index('attach-volume')
        probe = calls.index('export-credentials')
        self.assertEqual(calls[attachment + 1:probe], ['wait', 'describe-instances', 'describe-instances'])

    def test_setup_attaches_when_the_instance_stops_during_volume_creation(self) -> None:
        fake = FakeAws(self.clock, state='pending')
        calls: list[str] = []
        subject = self.subject(fake)
        ceiling = hana_spend.read_settings(conf.read(self.workload)).spot_max_usd_per_hour

        def volume_ready() -> None:
            self.assertEqual(fake.state, 'pending')
            fake.state = 'stopped'

        api_call = self.setup_api(fake, calls, lambda: ceiling, volume_ready=volume_ready)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs'), \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch.object(subject, 'pause') as pause, mock.patch('sys.stdout', new_callable=StringIO):
            _ = subject.prepare_hana()
            self.assertTrue(subject.hana_setup(approve_resources=True))
            pause.assert_not_called()
        self.assertEqual(calls.count('attach-volume'), 1)
        self.assertNotIn('stop-instances', calls)

    def test_setup_updates_the_launch_template_after_refusals_and_a_ceiling_change(self) -> None:
        key = 'spot_max_usd_per_hour'
        ceiling = conf.read(self.workload).text(key)
        _ = self.workload.write_text(self.workload.read_text().replace(f'{key}={ceiling}\n', f'{key}=0.09\n'))
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        saved_path = self.state / 'hana/setup.json'
        attempts: list[tuple[str, ...]] = []
        calls: list[str] = []
        price = Decimal('0.08')

        def launch(arguments: tuple[str, ...]) -> None:
            attempts.append(arguments)
            saved = remote.read_record(saved_path)
            if len(attempts) == 1:
                self.assertEqual(saved['spot_max_price'], '0.09')
                raise aws_cli.AwsUnavailable('An error occurred (SpotMaxPriceTooLow) when calling the RunInstances operation')
            self.assertEqual(saved['spot_max_price'], '0.13')
            self.assertEqual(saved['launch_template'], 'LaunchTemplateId=fixture-template,Version=4')
            compute = ledger.CostLedger(self.state).read_snapshot().hana_compute
            self.assertIsInstance(compute[0].state, ledger.HanaExposureClosed)
            self.assertEqual(compute[1].usd_per_hour, Decimal('0.13'))

        api_call = self.setup_api(fake, calls, lambda: price, launch=launch)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs'), \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call) as aws, \
                mock.patch('sys.stdout', new_callable=StringIO):
            _ = subject.prepare_hana()
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'No Hana Spot candidate launched'):
                _ = subject.hana_setup(approve_resources=True)
            saved = remote.read_record(saved_path)
            self.assertNotIn('instance', saved)
            self.assertNotIn('pending_launch', saved)
            price = Decimal('0.11')
            with self.assertRaisesRegex(deploy.DeploymentRefused, r'/tune hana_aws spot_max_usd_per_hour 0\.13'):
                _ = subject.hana_setup(approve_resources=True)
            conf.check(self.workload, key, '0.13')
            _ = self.workload.write_text(self.workload.read_text().replace(f'{key}=0.09\n', f'{key}=0.13\n'))
            _ = subject.prepare_hana()
            self.assertTrue(subject.hana_setup(approve_resources=True))
            first, second = [cast(tuple[str, ...], call.args) for call in aws.call_args_list
                             if call.args[0] == 'create-change-set']
            self.assertEqual(first[first.index('--change-set-type') + 1], 'CREATE')
            self.assertEqual(second[second.index('--change-set-type') + 1], 'UPDATE')
            parameters = cast(object, json.loads(second[second.index('--parameters') + 1]))
            self.assertIn({'ParameterKey': 'SpotMaxPrice', 'ParameterValue': '0.13'},
                          ledger.rows({'parameters': parameters}, 'parameters'))
            _ = self.workload.write_text(self.workload.read_text().replace(f'{key}=0.13\n', f'{key}=0.14\n'))
            _ = subject.prepare_hana()
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'The approved Hana parameters changed'):
                _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(calls.count('create-change-set'), 2)
        self.assertEqual(len(attempts), 2)

    def assert_requested_setup_recovers(self, retry_price: Callable[[], Decimal]) -> None:
        fake = FakeAws(self.clock, state='running', lose_response='describe-instances')
        subject = self.subject(fake)
        limits = hana_spend.read_settings(conf.read(self.workload))
        calls: list[str] = []
        recovering = False

        def spot_price() -> Decimal:
            return retry_price() if recovering else limits.spot_max_usd_per_hour

        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs'), \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=self.setup_api(fake, calls, spot_price)), \
                mock.patch('sys.stdout', new_callable=StringIO):
            _ = subject.prepare_hana()
            with self.assertRaises(subprocess.TimeoutExpired):
                _ = subject.hana_setup(approve_resources=True)
            saved = remote.read_record(self.state / 'hana/setup.json')
            self.assertIs(saved['requested'], True)
            self.assertIn('execute-change-set', calls)
            self.assertEqual(fake.state, 'running')

            recovering = True
            calls.clear()
            self.assertTrue(subject.hana_setup(approve_resources=True))

        self.assertNotIn('describe-spot-price-history', calls)
        self.assertNotIn('create-change-set', calls)
        self.assertIn('stop-instances', calls)
        self.assertEqual(fake.state, 'stopped')
        self.assertEqual(remote.read_record(self.state / 'hana/lifecycle.json')['state'], hana_box.Lifecycle.IDLE_STOPPED)

    def test_requested_setup_recovers_when_the_price_exceeds_the_ceiling(self) -> None:
        limits = hana_spend.read_settings(conf.read(self.workload))
        self.assert_requested_setup_recovers(lambda: limits.spot_max_usd_per_hour * 2)

    def test_requested_setup_recovers_when_the_price_read_fails(self) -> None:
        def price_read_failure() -> Decimal:
            raise aws_cli.AwsUnavailable('fixture Spot read failed')

        self.assert_requested_setup_recovers(price_read_failure)

    def test_setup_reservation_does_not_enable_start_or_command_permission(self) -> None:
        settings = hana_spend.read_settings(conf.read(self.workload))
        spending = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, settings)
        reservation = spending.reserve_setup(self.clock.now, self.clock.now + settings.permit_max_seconds)
        self.assertIsInstance(reservation, hana_spend.StartReserved)
        assert isinstance(reservation, hana_spend.StartReserved)
        self.assertIsInstance(spending.check_start(self.clock.now, reservation.exposure.generation), hana_spend.PermitRefused)
        self.assertIsInstance(spending.bind_start(self.clock.now, reservation.exposure.generation, 'fixture-boot'), hana_spend.PermitRefused)

    def test_template_provisions_the_launch_resources_and_keeps_scaler_permissions_scoped(self) -> None:
        template = remote.read_record(deploy.HANA_TEMPLATE)
        resources = ledger.mapping(template['Resources'])
        launch = ledger.mapping(ledger.mapping(ledger.mapping(resources['LaunchTemplate'])['Properties'])['LaunchTemplateData'])
        market = ledger.mapping(ledger.mapping(launch['InstanceMarketOptions'])['SpotOptions'])
        self.assertEqual(market['SpotInstanceType'], 'persistent')
        self.assertEqual(market['InstanceInterruptionBehavior'], 'stop')
        self.assertEqual(market['MaxPrice'], {'Ref': 'SpotMaxPrice'})
        self.assertNotIn('IamInstanceProfile', launch)
        self.assertNotIn('KeyName', launch)
        self.assertFalse(resources.keys() & {'Instance', 'DataVolume', 'DataAttachment'})
        self.assertFalse(ledger.mapping(template['Outputs']).keys() & {'Instance', 'DataVolume'})
        self.assertFalse(ledger.mapping(template['Parameters']).keys() & {'InstanceType', 'SubnetId', 'AvailabilityZone', 'DataGiB'})
        policy = ledger.mapping(ledger.mapping(resources['ScalerPolicy'])['Properties'])['PolicyDocument']
        encoded = json.dumps(policy, separators=(',', ':'))
        rendered = re.sub(r'\$\{[^}]+\}', 'x' * 64, encoded).replace('{"Ref":"DeploymentId"}', '"' + 'x' * 32 + '"')
        self.assertLessEqual(len(rendered), 6144)
        for name in ('Instance', 'DataVolume', 'DataAttachment'):
            self.assertNotIn(f'{{"Ref":"{name}"}}', encoded)
        for statement in ledger.rows(ledger.mapping(policy), 'Statement')[:2]:
            condition = ledger.mapping(ledger.mapping(statement['Condition'])['StringEquals'])
            self.assertEqual(condition['ec2:ResourceTag/HanaDeployment'], {'Ref': 'DeploymentId'})
            self.assertEqual(condition['ec2:ResourceTag/Workload'], 'hana')
        self.assertNotIn('AWS::IAM::User', deploy.HANA_TEMPLATE.read_text())

    def built_image(self) -> Path:
        result = self.root / 'store/0123456789abcdefghij-nixos-image-amazon'
        (result / 'nix-support').mkdir(parents=True, exist_ok=True)
        disk = result / 'nixos-image-amazon.vhd'
        _ = disk.write_bytes(b'fixture disk')
        _ = (result / 'nix-support/image-info.json').write_text(json.dumps({
            'label': '26.05.fixture', 'boot_mode': 'legacy-bios', 'system': 'x86_64-linux',
            'logical_bytes': str(4 * 2**30), 'file': str(disk)}))
        return result

    def test_built_image_name_follows_the_store_hash_and_refuses_unknown_files(self) -> None:
        built = hana_image.read_built(self.built_image())
        self.assertEqual(built.name, 'nixos-hana-26.05.fixture-0123456789ab')
        self.assertEqual(built.source_format, 'vpc')
        self.assertEqual(built.boot_mode, 'legacy-bios')
        with self.assertRaises(hana_image.ImageRefused):
            _ = hana_image.read_built(self.root / 'missing')

    def image_aws(self, existing: bool, *, registration_failure: bool = False,
                  snapshot_states: tuple[str, ...] = ()) -> tuple[list[tuple[str, tuple[str, ...]]], object]:
        calls: list[tuple[str, tuple[str, ...]]] = []
        def api_call(operation: str, *arguments: str, service: str = 'ec2', timeout_s: float | None = None) -> dict[str, object]:
            _ = service, timeout_s
            calls.append((operation, arguments))
            if operation == 'describe-route-tables':
                return {'RouteTables': [{'Associations': [{'Main': True}], 'Routes': [
                    {'DestinationCidrBlock': '0.0.0.0/0', 'State': 'active', 'GatewayId': 'igw-fixture'}]}]}
            if operation == 'describe-subnets':
                return {'Subnets': [{'SubnetId': 'fixture-subnet-c', 'AvailabilityZone': 'us-east-2c', 'State': 'available'},
                                    {'SubnetId': 'fixture-subnet-b', 'AvailabilityZone': 'us-east-2b', 'State': 'available'}]}
            if operation == 'describe-images':
                if '--image-ids' in arguments or existing:
                    return {'Images': [{'ImageId': 'fixture-hana-image', 'OwnerId': 'fixture-owner', 'State': 'available'}]}
                return {'Images': []}
            if operation == 'describe-snapshots':
                return {'Snapshots': [{'SnapshotId': 'fixture-snapshot' if index == 0 else f'fixture-snapshot-{index}',
                                       'State': state} for index, state in enumerate(snapshot_states)]}
            if operation == 'register-image':
                if registration_failure:
                    raise deploy.DeploymentRefused('fixture registration failure')
                return {'ImageId': 'fixture-hana-image'}
            return {}
        return calls, api_call

    def run_image(self, existing: bool, *, wait_failure: bool = False,
                  registration_failure: bool = False,
                  snapshot_states: tuple[str, ...] = ()) -> tuple[list[tuple[str, tuple[str, ...]]], list[list[str]], list[list[str]]]:
        calls, api_call = self.image_aws(existing, registration_failure=registration_failure, snapshot_states=snapshot_states)
        local: list[list[str]] = []
        uploads: list[list[str]] = []
        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool,
                    env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = capture_output, text, timeout, check, env
            local.append(list(arguments))
            if arguments[0] == 'qemu-img':
                _ = Path(arguments[-1]).write_bytes(b'raw')
            return subprocess.CompletedProcess(list(arguments), 0, '/nix/store/fixture-coldsnap\n', '')
        def admin(_execute: object) -> object:
            def authorized(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                uploads.append(list(arguments))
                if snapshot_states:
                    self.assertNotIn('upload', arguments)
                if 'upload' in arguments:
                    self.assertIn('describe-snapshots', dict(calls))
                if 'wait' in arguments:
                    self.assertEqual(hana_image.saved(self.state), hana_image.PendingImageRegistration(
                        'nixos-hana-26.05.fixture-0123456789ab', 'fixture-snapshot'))
                    if wait_failure:
                        return subprocess.CompletedProcess(list(arguments), 1, '', 'fixture wait failure')
                return subprocess.CompletedProcess(list(arguments), 0, 'fixture-snapshot\n', '')
            return authorized
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=execute,
                                    clock=self.clock, notify=lambda _event: True)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_guard'), \
                mock.patch.object(subject.admin, 'executor', side_effect=admin), \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch('sys.stdout', new_callable=StringIO):
            self.assertTrue(subject.hana_image(self.built_image(), approve_resources=True))
        self.assertFalse((subject.output / 'hana/image.raw').exists())
        return calls, local, uploads

    def test_image_without_approval_makes_no_aws_call(self) -> None:
        fake = FakeAws(self.clock)
        with mock.patch('sys.stdout', new_callable=StringIO):
            self.assertFalse(self.subject(fake).hana_image(self.built_image()))
        self.assertEqual(fake.calls, [])
        self.assertIsInstance(hana_image.saved(self.state), hana_image.NoRegisteredImage)

    def test_image_preview_reads_placement_with_invalid_spending_settings(self) -> None:
        fake = FakeAws(self.clock)
        text_setting(self.workload, 'enabled', 'on')
        text_setting(self.workload, 'fallback_instance_types', 'c7a.4xlarge,c7a.4xlarge')
        with mock.patch('sys.stdout', new_callable=StringIO) as output:
            self.assertFalse(self.subject(fake).hana_image(self.built_image()))
        self.assertIn('Hana placement is enabled. This command does not change it.', output.getvalue())
        self.assertEqual(fake.calls, [])
        self.assertIsInstance(hana_image.saved(self.state), hana_image.NoRegisteredImage)

    def test_image_upload_registers_imds_v2_root_disk_and_saves_the_zone_subnet(self) -> None:
        calls, local, uploads = self.run_image(existing=False)
        self.assertEqual(local[0][:5], ['qemu-img', 'convert', '-f', 'vpc', '-O'])
        self.assertEqual(uploads[0][0], '/nix/store/fixture-coldsnap/bin/coldsnap')
        self.assertNotIn('--wait', uploads[0])
        tag = conf.read(self.settings)
        cost_tag = f"Key={tag.text('cost_explorer_tag_key')},Value={tag.text('cost_explorer_tag_value')}"
        self.assertEqual(uploads[0][uploads[0].index('--tag') + 1], cost_tag)
        self.assertEqual(uploads[1][3:], ['wait', '--seconds-between-attempts', '15',
                                        '--max-attempts', '240', 'fixture-snapshot'])
        self.assertLessEqual(int(uploads[1][uploads[1].index('--max-attempts') + 1]), 255)
        register = dict(calls)['register-image']
        self.assertEqual(register[register.index('--tag-specifications') + 1:],
                         (f'ResourceType=image,Tags=[{{{cost_tag}}}]',))
        self.assertIn('v2.0', register)
        self.assertIn('DeviceName=/dev/xvda,Ebs={SnapshotId=fixture-snapshot,VolumeType=gp3,DeleteOnTermination=true}', register)
        self.assertEqual(hana_image.saved(self.state), hana_image.RegisteredImage(
            'nixos-hana-26.05.fixture-0123456789ab', 'fixture-hana-image', 'fixture-owner', 'fixture-vpc', 'fixture-subnet-b'))

    def test_image_with_the_same_name_is_not_uploaded_again(self) -> None:
        _ = hana_image.save(self.state, hana_image.PendingImageRegistration(
            'nixos-hana-26.05.fixture-0123456789ab', 'fixture-snapshot'))
        calls, local, uploads = self.run_image(existing=True)
        self.assertEqual((local, uploads), ([], []))
        self.assertNotIn('register-image', dict(calls))
        self.assertIsInstance(hana_image.saved(self.state), hana_image.RegisteredImage)

    def test_completed_snapshot_with_the_image_description_is_registered_without_upload(self) -> None:
        calls, local, uploads = self.run_image(existing=False, snapshot_states=('completed',))
        self.assertEqual(dict(calls)['describe-snapshots'], ('--owner-ids', 'self', '--filters',
                         'Name=description,Values=nixos-hana-26.05.fixture-0123456789ab'))
        self.assertEqual([call[0] for call in local], ['nix'])
        self.assertEqual([call[3:] for call in uploads], [['wait', '--seconds-between-attempts', '15',
                                                        '--max-attempts', '240', 'fixture-snapshot']])
        self.assertIn('SnapshotId=fixture-snapshot', ' '.join(dict(calls)['register-image']))
        self.assertIsInstance(hana_image.saved(self.state), hana_image.RegisteredImage)

    def test_pending_snapshot_with_the_image_description_is_saved_before_wait(self) -> None:
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'fixture wait failure'):
            _ = self.run_image(existing=False, snapshot_states=('pending',), wait_failure=True)
        self.assertEqual(hana_image.saved(self.state), hana_image.PendingImageRegistration(
            'nixos-hana-26.05.fixture-0123456789ab', 'fixture-snapshot'))
        calls, local, uploads = self.run_image(existing=False)
        self.assertNotIn('describe-snapshots', dict(calls))
        self.assertEqual([call[0] for call in local], ['nix'])
        self.assertEqual([call[3:] for call in uploads], [['wait', '--seconds-between-attempts', '15',
                                                        '--max-attempts', '240', 'fixture-snapshot']])
        self.assertIn('register-image', dict(calls))

    def test_multiple_snapshots_or_error_snapshot_refuse_without_upload(self) -> None:
        subject = self.subject(FakeAws(self.clock))
        result = self.built_image()
        for states in (('completed', 'pending'), ('error',)):
            with self.subTest(states=states):
                calls, api_call = self.image_aws(existing=False, snapshot_states=states)
                authorized = mock.Mock()
                with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                        mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_guard'), \
                        mock.patch.object(subject, 'execute') as execute, \
                        mock.patch.object(subject.admin, 'executor', return_value=authorized), \
                        mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                        mock.patch('sys.stdout', new_callable=StringIO):
                    with self.assertRaisesRegex(deploy.DeploymentRefused,
                            f'{len(states)} matching snapshots; states: ' + ', '.join(states)):
                        _ = subject.hana_image(result, approve_resources=True)
                execute.assert_not_called()
                authorized.assert_not_called()
                self.assertNotIn('register-image', dict(calls))
                self.assertIsInstance(hana_image.saved(self.state), hana_image.NoRegisteredImage)

    def test_no_matching_snapshot_uploads_with_the_image_description(self) -> None:
        calls, _, uploads = self.run_image(existing=False)
        operations = [operation for operation, _ in calls]
        self.assertLess(operations.index('describe-images'), operations.index('describe-snapshots'))
        self.assertLess(operations.index('describe-snapshots'), operations.index('register-image'))
        self.assertEqual(uploads[0][3], 'upload')
        self.assertEqual(uploads[0][uploads[0].index('--description') + 1],
                         'nixos-hana-26.05.fixture-0123456789ab')

    def test_image_wait_failure_keeps_the_snapshot_and_retry_does_not_upload(self) -> None:
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'fixture wait failure'):
            _ = self.run_image(existing=False, wait_failure=True)
        self.assertEqual(hana_image.saved(self.state), hana_image.PendingImageRegistration(
            'nixos-hana-26.05.fixture-0123456789ab', 'fixture-snapshot'))
        self.assertFalse((self.root / 'output/hana/image.raw').exists())
        calls, local, uploads = self.run_image(existing=False)
        self.assertEqual([call[0] for call in local], ['nix'])
        self.assertEqual([call[3:] for call in uploads], [['wait', '--seconds-between-attempts', '15',
                                                        '--max-attempts', '240', 'fixture-snapshot']])
        self.assertIn('SnapshotId=fixture-snapshot', ' '.join(dict(calls)['register-image']))
        self.assertIsInstance(hana_image.saved(self.state), hana_image.RegisteredImage)

    def test_image_registration_failure_keeps_the_pending_snapshot(self) -> None:
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'fixture registration failure'):
            _ = self.run_image(existing=False, registration_failure=True)
        self.assertEqual(hana_image.saved(self.state), hana_image.PendingImageRegistration(
            'nixos-hana-26.05.fixture-0123456789ab', 'fixture-snapshot'))
        self.assertFalse((self.root / 'output/hana/image.raw').exists())
        calls, _, uploads = self.run_image(existing=False)
        self.assertEqual([call[3:] for call in uploads], [['wait', '--seconds-between-attempts', '15',
                                                        '--max-attempts', '240', 'fixture-snapshot']])
        self.assertIn('register-image', dict(calls))
        self.assertIsInstance(hana_image.saved(self.state), hana_image.RegisteredImage)

    def test_pending_snapshot_for_another_image_does_not_skip_upload(self) -> None:
        _ = hana_image.save(self.state, hana_image.PendingImageRegistration('another-image', 'another-snapshot'))
        _, _, uploads = self.run_image(existing=False)
        self.assertEqual(uploads[0][3], 'upload')
        self.assertEqual(uploads[1][3:], ['wait', '--seconds-between-attempts', '15',
                                        '--max-attempts', '240', 'fixture-snapshot'])

    def test_pending_snapshot_does_not_supply_hana_inputs(self) -> None:
        _ = hana_image.save(self.state, hana_image.PendingImageRegistration('fixture-name', 'fixture-snapshot'))
        text_setting(self.workload, 'ami_id', '')
        text_setting(self.workload, 'ami_owner_id', '')
        subject = self.subject(FakeAws(self.clock))
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()):
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'Configure the reviewed Hana image'):
                _ = subject._hana_inputs()  # pyright: ignore[reportPrivateUsage]

    def test_saved_image_fills_only_empty_configuration_values(self) -> None:
        _ = hana_image.save(self.state, hana_image.RegisteredImage('fixture-name', 'saved-image', 'saved-owner',
                                                                   'saved-vpc', 'saved-subnet'))
        text_setting(self.workload, 'ami_id', '')
        text_setting(self.workload, 'ami_owner_id', '')
        subject = self.subject(FakeAws(self.clock))
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()):
            settings, parameters, _ = subject._hana_inputs()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((settings.ami_id, settings.ami_owner_id), ('saved-image', 'saved-owner'))
        self.assertEqual((settings.vpc_id, parameters['SubnetId']), ('fixture-vpc', 'fixture-subnet'))
