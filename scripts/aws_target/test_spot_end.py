"""Verify that every Hana end path cancels each persistent Spot request before it terminates a box.

The fake models the AWS rule behind the 2026-11-04 stray box: a terminated box under a live persistent
request opens that request again, and AWS launches a new box with no tags."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from io import StringIO
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import cast
from unittest import mock

from scripts.aws_target import deploy, gpu_size_trial, ledger, remote, spot_end
from scripts.aws_target.aws_cli import AwsCli, AwsUnavailable
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.ledger_test_support import CostFixture

TAG: tuple[str, str] = ('HanaDeployment', 'fixture-deployment')
STACK = 'fixture-hana'
LIVE = ('pending', 'running', 'stopping', 'stopped', 'shutting-down')


@dataclass
class Box:
    state: str
    request: str
    tags: dict[str, str]


@dataclass
class Request:
    kind: str
    state: str
    instance: str
    tags: dict[str, str]


@dataclass
class FakeEc2:
    """An AWS executor with persistent Spot requests and a Hana stack."""
    boxes: dict[str, Box] = field(default_factory=dict)
    requests: dict[str, Request] = field(default_factory=dict)
    operations: list[str] = field(default_factory=list)
    stack: bool = False
    replace: bool = False
    fail_wait: str = ''
    fail: str = ''
    launch_on_cancel: bool = False
    launch_on_execute: bool = False
    count: int = 0

    def launch(self, *, tagged: bool, request: str = '') -> str:
        self.count += 1
        box = f'box-{self.count}'
        tags = dict([TAG]) if tagged else {}
        if not request:
            request = f'request-{self.count}'
            self.requests[request] = Request('persistent', 'active', box, dict([TAG]))
        self.requests[request].state, self.requests[request].instance = 'active', box
        self.boxes[box] = Box('running', request, tags)
        return box

    def live_requests(self) -> list[str]:
        return [key for key, row in self.requests.items() if row.state in spot_end.LIVE_REQUEST_STATES]

    def live_boxes(self) -> list[str]:
        return [key for key, row in self.boxes.items() if row.state in LIVE]

    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float, check: bool,
                 env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = capture_output, text, timeout, check, env
        offset = list(arguments).index('--region') + 2
        operation = arguments[offset + 1]
        rest = list(arguments[offset + 2:-2])
        label = operation + (' ' + rest[0] if operation == 'wait' else '')
        self.operations.append(label)
        if self.fail in (operation, label):
            return subprocess.CompletedProcess(list(arguments), 1, '', 'fixture failure')
        value: dict[str, object] = {}
        if operation == 'describe-instances':
            value = {'Reservations': [{'Instances': [
                {'InstanceId': key, 'SpotInstanceRequestId': row.request, 'State': {'Name': row.state},
                 'Tags': [{'Key': name, 'Value': tag} for name, tag in row.tags.items()]}
                for key, row in self.boxes.items() if self.matches(rest, row)]}]}
        elif operation == 'describe-spot-instance-requests':
            ids = rest[1:] if rest[0] == '--spot-instance-request-ids' else []
            value = {'SpotInstanceRequests': [
                {'SpotInstanceRequestId': key, 'Type': row.kind, 'State': row.state, 'InstanceId': row.instance}
                for key, row in self.requests.items()
                if (key in ids if ids else row.tags.get(TAG[0]) == TAG[1])]}
        elif operation == 'cancel-spot-instance-requests':
            for key in rest[1:]:
                if self.launch_on_cancel and self.requests[key].state == 'open':
                    _ = self.launch(tagged=False, request=key)
                self.requests[key].state = 'cancelled'
        elif operation == 'terminate-instances':
            for key in rest[1:]:
                row = self.boxes[key]
                row.state = 'terminated'
                request = self.requests.get(row.request)
                if request and request.kind == 'persistent' and request.state == 'active':
                    # AWS opens the persistent request again and launches a box with no tags.
                    request.state = 'open'
                    _ = self.launch(tagged=False, request=row.request)
        elif operation == 'describe-stacks':
            if not self.stack:
                return subprocess.CompletedProcess(list(arguments), 255, '', 'ValidationError: Stack does not exist')
            value = {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': []}]}
        elif operation == 'describe-change-set':
            change = {'ResourceChange': {'LogicalResourceId': 'Instance', 'ResourceType': 'AWS::EC2::Instance',
                                         'Action': 'Modify', 'Replacement': 'True' if self.replace else 'False'}}
            value = {'Status': 'CREATE_COMPLETE', 'Changes': [change]}
        elif operation == 'execute-change-set':
            if self.launch_on_execute:
                _ = self.launch(tagged=True)
        elif operation == 'delete-stack':
            self.stack = False
        elif operation == 'wait' and rest[0] == self.fail_wait:
            return subprocess.CompletedProcess(list(arguments), 255, '', 'Waiter failed: fixture rollback')
        elif operation not in ('wait', 'create-change-set', 'delete-change-set'):
            raise AssertionError('Unexpected fake AWS operation: ' + operation)
        return subprocess.CompletedProcess(list(arguments), 0, json.dumps(value), '')

    @staticmethod
    def matches(arguments: list[str], row: Box) -> bool:
        for item in arguments[1:]:
            name, _, values = item.removeprefix('Name=').partition(',Values=')
            allowed = values.split(',')
            if name == 'instance-state-name' and row.state not in allowed:
                return False
            if name == 'spot-instance-request-id' and row.request not in allowed:
                return False
            if name.startswith('tag:') and row.tags.get(name.removeprefix('tag:')) not in allowed:
                return False
        return True


class SpotEndTests(CostFixture):
    def api(self, fake: FakeEc2) -> AwsCli:
        return AwsCli('fixture-admin', 'us-east-2', 1, fake)

    def test_terminate_before_cancel_leaves_an_open_request_and_a_new_box(self) -> None:
        fake = FakeEc2()
        box = fake.launch(tagged=True)
        _ = self.api(fake).call('terminate-instances', '--instance-ids', box)
        self.assertEqual(len(fake.live_requests()), 1)
        self.assertEqual(len(fake.live_boxes()), 1)
        self.assertEqual(fake.boxes[fake.live_boxes()[0]].tags, {})

    def test_end_boxes_cancels_before_terminate_and_leaves_nothing_live(self) -> None:
        fake = FakeEc2()
        _ = fake.launch(tagged=True)
        self.assertEqual(spot_end.end_boxes(self.api(fake), TAG), 1)
        self.assertLess(fake.operations.index('cancel-spot-instance-requests'), fake.operations.index('terminate-instances'))
        self.assertEqual((fake.live_requests(), fake.live_boxes()), ([], []))

    def test_end_boxes_finds_an_untagged_box_a_request_launched_again(self) -> None:
        fake = FakeEc2()
        box = fake.launch(tagged=True)
        fake.boxes[box].state = 'terminated'
        fake.requests[fake.boxes[box].request].state = 'open'
        _ = fake.launch(tagged=False, request=fake.boxes[box].request)
        self.assertEqual(spot_end.end_boxes(self.api(fake), TAG), 1)
        self.assertEqual((fake.live_requests(), fake.live_boxes()), ([], []))

    def test_end_boxes_reads_again_after_a_launch_between_the_cancel_and_the_terminate(self) -> None:
        fake = FakeEc2(launch_on_cancel=True)
        box = fake.launch(tagged=True)
        fake.boxes[box].state = 'terminated'
        fake.requests[fake.boxes[box].request].state = 'open'
        self.assertEqual(spot_end.end_boxes(self.api(fake), TAG), 1)
        self.assertEqual((fake.live_requests(), fake.live_boxes()), ([], []))

    def test_a_failed_cancel_terminates_nothing(self) -> None:
        fake = FakeEc2(fail='cancel-spot-instance-requests')
        _ = fake.launch(tagged=True)
        with self.assertRaises(AwsUnavailable):
            _ = spot_end.end_boxes(self.api(fake), TAG)
        self.assertNotIn('terminate-instances', fake.operations)
        self.assertEqual(len(fake.live_boxes()), 1)

    def test_end_boxes_keeps_only_the_named_box_and_its_request(self) -> None:
        fake = FakeEc2()
        kept = fake.launch(tagged=True)
        _ = fake.launch(tagged=True)
        self.assertEqual(spot_end.end_boxes(self.api(fake), TAG, keep=kept), 1)
        self.assertEqual(fake.live_boxes(), [kept])
        self.assertEqual(fake.live_requests(), [fake.boxes[kept].request])

    def hana_settings(self) -> DeploymentSettings:
        return cast(DeploymentSettings, cast(object, SimpleNamespace(stack_name=STACK, profile='fixture-admin', region='us-east-2', timeout_s=1)))

    def subject(self, fake: FakeEc2) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.root / 'output', execute=fake)

    def change(self, fake: FakeEc2, keep: str = '') -> None:
        subject = self.subject(fake)
        subject._hana_stack_change(self.api(fake), self.hana_settings(), self.root / 'template.json',  # pyright: ignore[reportPrivateUsage]
                                   {'DeploymentId': TAG[1]}, keep=keep)

    def test_rollback_of_a_failed_create_leaves_no_request_and_no_box(self) -> None:
        fake = FakeEc2(launch_on_execute=True, fail_wait='stack-create-complete')
        with self.assertRaisesRegex(AwsUnavailable, 'ended 1 Spot boxes'):
            self.change(fake)
        self.assertEqual((fake.live_requests(), fake.live_boxes()), ([], []))

    def test_rollback_of_a_failed_update_keeps_only_the_recorded_box(self) -> None:
        fake = FakeEc2(stack=True, launch_on_execute=True, fail_wait='stack-update-complete')
        kept = fake.launch(tagged=True)
        with self.assertRaisesRegex(AwsUnavailable, 'ended 1 Spot boxes'):
            self.change(fake, kept)
        self.assertEqual(fake.live_boxes(), [kept])
        self.assertEqual(fake.live_requests(), [fake.boxes[kept].request])

    def test_replace_is_refused_before_any_change(self) -> None:
        fake = FakeEc2(stack=True, replace=True)
        kept = fake.launch(tagged=True)
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'refuses replacement: Instance'):
            self.change(fake, kept)
        self.assertNotIn('execute-change-set', fake.operations)
        self.assertIn('delete-change-set', fake.operations)
        self.assertEqual(fake.live_boxes(), [kept])
        self.assertEqual(fake.live_requests(), [fake.boxes[kept].request])

    def test_teardown_ends_every_box_before_the_stack_delete(self) -> None:
        fake = FakeEc2(stack=True)
        box = fake.launch(tagged=True)
        fake.boxes[box].state = 'stopped'
        hana = self.state / 'hana'
        hana.mkdir()
        _ = (hana / 'setup.json').write_text(json.dumps({'deployment': TAG[1], 'stack': STACK}))
        _ = (hana / 'lifecycle.json').write_text('{}')
        subject = self.subject(fake)
        with mock.patch.object(subject, '_check_credentials'), mock.patch.object(subject, '_api', return_value=self.api(fake)), \
                mock.patch.object(subject, '_hana_inputs', return_value=(self.hana_settings(), {}, b'')), \
                mock.patch('sys.stdout', new_callable=StringIO):
            self.assertTrue(subject.hana_down(approve_destroy=True))
        self.assertEqual((fake.live_requests(), fake.live_boxes()), ([], []))
        self.assertLess(fake.operations.index('cancel-spot-instance-requests'), fake.operations.index('terminate-instances'))
        self.assertLess(fake.operations.index('wait instance-terminated'), fake.operations.index('delete-stack'))
        self.assertFalse(fake.stack)
        self.assertEqual(sorted(path.name for path in hana.iterdir()), [])

    def test_teardown_without_approval_changes_nothing(self) -> None:
        fake = FakeEc2()
        with mock.patch('sys.stdout', new_callable=StringIO):
            self.assertTrue(self.subject(fake).hana_down())
        self.assertEqual(fake.operations, [])


class OneTimeLaunchTests(CostFixture):
    def test_only_hana_uses_a_persistent_spot_request(self) -> None:
        gate = ledger.mapping(remote.read_record(deploy.TEMPLATE)['Resources'])
        for name, resource in gate.items():
            data = ledger.mapping(ledger.mapping(resource).get('Properties', {})).get('LaunchTemplateData')
            if data is None:
                continue
            options = ledger.mapping(ledger.mapping(ledger.mapping(data)['InstanceMarketOptions'])['SpotOptions'])
            self.assertEqual((options['SpotInstanceType'], options['InstanceInterruptionBehavior']), ('one-time', 'terminate'), name)
        self.assertEqual(sorted(gate.keys() & {'Instance', 'LaunchTemplate', 'DataVolume'}), [])

    def test_gate_launches_keep_the_template_market_options(self) -> None:
        source = (Path(deploy.__file__).parent / 'cloud_gate.py').read_text()
        self.assertNotIn('InstanceMarketOptions', source)
        self.assertNotIn('persistent', source)

    def test_gpu_size_trial_launches_one_time_spot_with_terminate(self) -> None:
        source = Path(gpu_size_trial.__file__).read_text()
        self.assertIn("'SpotInstanceType': 'one-time'", source)
        self.assertIn("'InstanceInterruptionBehavior': 'terminate'", source)
        self.assertNotIn("'persistent'", source)
