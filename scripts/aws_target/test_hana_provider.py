"""Exercise scoped lifecycle calls without a network or live deployment state."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
import base64
import fcntl
from io import StringIO
import json
from pathlib import Path
import subprocess
import time
from typing import cast, override
from unittest import mock
from urllib.parse import urlsplit

from scripts.aws_target import hana_protocol, hana_execution, hana_result, test_hana_execution
from scripts.aws_target import aws_target, hana_box as box, hana_guest, hana_provider as provider, hana_receiver, deploy, ledger, remote
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.aws_target.test_hana_box import record_lifecycle_image, write_lifecycle_configuration
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic

REPOSITORY = Path(__file__).resolve().parents[2]
IDENTITY = box.DeploymentIdentity('fixture-deployment', 'fixture-instance', 'fixture-request', 'us-east-2')


@dataclass
class FakeAws:
    clock: TestClock
    state: str = 'stopped'
    userdata: str = ''
    failure: str = ''
    failure_stderr: str = 'private-provider-id signed-secret'
    lose_response: str = ''
    put_error_after_save: str = ''
    puts_before_timeout: int = -1
    spot_read_seconds: int = 0
    stop_during_instance_read_seconds: int = 0
    status: str = 'instance-stopped-by-user'
    ownership: str = IDENTITY.deployment
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    uploaded_objects: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple[str, str, float]] = field(default_factory=list)
    environments: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = capture_output, text, check
        offset = arguments.index('--region') + 2
        service, operation = arguments[offset:offset + 2]
        self.calls.append((service, operation, timeout))
        if env is not None:
            self.environments.append(env)
        value: dict[str, object] = {}
        if self.failure == operation:
            return subprocess.CompletedProcess(list(arguments), 1, '', self.failure_stderr)
        if '--dry-run' in arguments:
            action = ''.join(word.title() for word in operation.split('-'))
            return subprocess.CompletedProcess(arguments, 1, '',
                f'An error occurred (DryRunOperation) when calling the {action} operation: Fixture permission')
        if operation == 'describe-instances':
            if self.stop_during_instance_read_seconds:
                self.clock.advance(self.stop_during_instance_read_seconds)
                self.state = 'stopped'
            value = {'Reservations': [{'Instances': [{'InstanceId': IDENTITY.instance, 'SpotInstanceRequestId': IDENTITY.spot_request,
                'ImageId': 'fixture-image',
                'State': {'Name': self.state}, 'LaunchTime': 'fixture-launch',
                'Tags': [{'Key': 'HanaDeployment', 'Value': self.ownership}, {'Key': 'Workload', 'Value': 'hana'}]}]}]}
        elif operation == 'describe-spot-instance-requests':
            self.clock.advance(self.spot_read_seconds)
            value = {'SpotInstanceRequests': [{'SpotInstanceRequestId': IDENTITY.spot_request, 'InstanceId': IDENTITY.instance,
                'Type': 'persistent', 'InstanceInterruptionBehavior': 'stop', 'Status': {'Code': self.status}}]}
        elif operation == 'export-credentials':
            value = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret'}
        elif operation == 'modify-instance-attribute':
            self.userdata = arguments[arguments.index('--value') + 1]
        elif operation == 'describe-instance-attribute':
            value = {'UserData': {'Value': self.userdata}}
        elif operation == 'start-instances':
            self.state = 'running'
        elif operation == 'stop-instances' or operation == 'wait':
            self.state = 'stopped'
            self.clock.now += 1
        elif operation in ('put-object', 'get-object'):
            key = arguments[arguments.index('--key') + 1]
            if operation == 'put-object':
                if self.puts_before_timeout == 0:
                    raise subprocess.TimeoutExpired('fixture put', timeout)
                self.puts_before_timeout -= 1
                content = Path(arguments[arguments.index('--body') + 1]).read_bytes()
                self.uploaded_objects[key] = content
                self.objects[key] = ledger.mapping(cast(object, json.loads(content)))
                if self.put_error_after_save:
                    return subprocess.CompletedProcess(list(arguments), 1, '', self.put_error_after_save)
            elif key in self.objects:
                _ = Path(arguments[-3]).write_text(json.dumps(self.objects[key]))
            else:
                return subprocess.CompletedProcess(list(arguments), 1, '', 'NoSuchKey')
        else:
            raise AssertionError('Unexpected fake AWS operation: ' + operation)
        if self.lose_response == operation:
            self.lose_response = ''
            raise subprocess.TimeoutExpired('private-provider-id', timeout)
        return subprocess.CompletedProcess(list(arguments), 0, json.dumps(value), '')


class HanaProviderTests(CostFixture):
    clock: TestClock = TestClock()
    workload: Path = Path()

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.fake: FakeAws = FakeAws(self.clock)
        self.provider: provider.AwsHanaProvider = provider.AwsHanaProvider(aws_target.AwsCli('', '', 1, self.fake),
            Path(), '', 1, result_transfer_timeout_s=83)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.workload = self.root / 'hana_aws.conf'
        write_lifecycle_configuration(self.workload)
        self.fake = FakeAws(self.clock)
        settings = conf.read(self.workload)
        self.provider = provider.AwsHanaProvider(aws_target.AwsCli('fixture-scaler', IDENTITY.region,
            settings.whole('aws_timeout_s'), self.fake), self.state, 'fixture-bucket',
            settings.whole('control_url_seconds'), result_transfer_timeout_s=83, clock=self.clock)

    def operation(self, action: box.Action) -> box.PendingOperation:
        return box.PendingOperation(action, 'fixture-' + action.value, self.clock.now, 1)

    def start(self) -> box.DemandStart:
        return box.DemandStart('fixture-start', self.clock.now, self.clock.now + conf.read(self.workload).whole('start_timeout_seconds'))

    def mutation_path(self, operation: box.PendingOperation) -> Path:
        return self.state / 'hana' / 'provider' / (operation.token + '.json')

    def test_bootstrap_start_observe_publish_stop_and_policy_cover_the_same_calls(self) -> None:
        start = self.start()
        for action in (box.Action.BOOTSTRAP, box.Action.START):
            result = self.provider.step(IDENTITY, start, self.operation(action))
            self.assertIsInstance(result, box.OperationAccepted)
        descriptor = ledger.mapping(cast(object, json.loads(base64.b64decode(self.fake.userdata))))
        self.assertEqual(descriptor['start_token'], start.token)

        key = test_hana_execution.HanaExecutionTests().key
        key = replace(key, generation=replace(key.generation, deployment=IDENTITY.deployment, start_token=start.token))
        self.assertEqual(self.provider.boot_control_deadline(key), descriptor['expires'])
        prefix = 'control/' + IDENTITY.deployment + '/' + start.token
        self.fake.objects[prefix + '/report.json'] = {'deployment': IDENTITY.deployment,
            'start_token': start.token, 'boot_id': 'fixture-boot', 'at': self.clock.now}
        observed = self.provider.step(IDENTITY, start, self.operation(box.Action.OBSERVE))
        self.assertIsInstance(observed, box.BoxObservation)
        assert isinstance(observed, box.BoxObservation)
        self.assertEqual(observed.boot, box.VerifiedBoot('fixture-boot', start.token))
        permit = hana_guest.WorkPermit(self.clock.now + conf.read(self.workload).whole('permit_max_seconds'), '1', 'fixture-boot')
        publication = box.PendingOperation(box.Action.PUBLISH, 'fixture-publication', self.clock.now, 1, permit, 0, 'fixture-execution')
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, publication)
        self.fake.objects[prefix + '/report.json'].update(token=publication.token, generation=1, execution=publication.execution)
        self.assertIsInstance(self.provider.step(IDENTITY, start, publication), box.PermitAccepted)
        stop = self.operation(box.Action.STOP)
        dispatched_at = self.clock.now
        _ = self.provider.step(IDENTITY, start, stop)
        self.assertEqual(self.fake.state, 'stopped')
        saved = remote.read_record(self.mutation_path(stop))
        self.assertEqual(saved['generation'], stop.generation)
        self.assertEqual(saved['dispatched_at'], dispatched_at)
        self.assertEqual(saved['accepted_at'], dispatched_at + 1)
        self.assertEqual(saved['acceptance_source'], 'direct')
        actions = {provider.PROVIDER_OPERATIONS[op] for service, op, _timeout in self.fake.calls if service != 'configure'}
        template = ledger.mapping(cast(object, json.loads(deploy.HANA_TEMPLATE.read_text())))
        policy = ledger.mapping(ledger.mapping(ledger.mapping(template['Resources'])['ScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
        granted: set[str] = set()
        for row in statements:
            values = row['Action']
            assert isinstance(values, list)
            for action in cast(list[object], values):
                assert isinstance(action, str)
                granted.add(action)
        self.assertTrue(actions <= granted)
        replacement_reads = {'ec2:DescribeImages', 'ec2:DescribeVolumes', 'ec2:DescribeReplaceRootVolumeTasks'}
        self.assertEqual(set(provider.PROVIDER_OPERATIONS.values()) | replacement_reads, granted)
        self.assertTrue(all(timeout == conf.read(self.workload).whole('aws_timeout_s') for _, _, timeout in self.fake.calls))
        self.assertEqual([op for _, op, _ in self.fake.calls].count('put-object'), 4)
        self.assertIn('command_url', descriptor)
        self.assertIn('fence_url', descriptor)

    def test_lost_start_response_recovers_without_another_start(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        self.fake.lose_response = 'start-instances'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
        _ = self.provider.step(IDENTITY, start, operation)
        self.assertEqual([op for _, op, _ in self.fake.calls].count('start-instances'), 1)

    def test_stop_retries_when_aws_still_reports_running(self) -> None:
        self.fake.state = 'running'
        self.fake.failure = 'stop-instances'
        operation = self.operation(box.Action.STOP)
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, box.NoDemandStart(), operation)
        self.fake.failure = ''
        self.assertIsInstance(self.provider.step(IDENTITY, box.NoDemandStart(), operation), box.OperationAccepted)
        self.assertEqual(self.fake.state, 'stopped')
        self.assertEqual([op for _, op, _ in self.fake.calls].count('stop-instances'), 2)

    def test_execution_objects_use_the_exact_boot_and_scoped_policy(self) -> None:
        fixture = test_hana_execution.HanaExecutionTests()
        request = fixture.request
        access = self.provider.execution_access(request.key)
        envelope = hana_protocol.HanaCommandEnvelope(request,
            hana_guest.WorkPermit(access.expires_at, '3', request.key.generation.boot_id), access)
        content = hana_protocol.encode_envelope(envelope)
        self.provider.publish_execution(content)
        prefix = 'control/' + request.key.generation.deployment + '/' + request.key.generation.start_token
        self.assertEqual(self.fake.objects[prefix + '/command.json']['action'], 'execute')
        self.assertEqual(self.fake.uploaded_objects[prefix + '/command.json'], content)
        observation = fixture.objects.observe_execution(request.key)
        observation_key = urlsplit(access.observation_url).path.lstrip('/')
        self.fake.objects[observation_key] = ledger.mapping(cast(object, json.loads(hana_protocol.encode_observation(observation))))
        for timeout in (3, 15):
            with self.subTest(timeout=timeout):
                self.assertEqual(self.provider.observe_execution(request.key, timeout_s=timeout), observation)
                self.assertEqual(self.fake.calls[-1], ('s3api', 'get-object', min(timeout, 10)))
        self.assertTrue(all(operation in provider.PROVIDER_OPERATIONS or operation == 'export-credentials'
                            for _, operation, _ in self.fake.calls))

    def test_result_download_uses_its_own_limit_and_reports_a_timeout(self) -> None:
        _ = (self.state / 'hana').mkdir()
        _ = (self.state / 'hana' / 'setup.json').write_text('{"bucket":"fixture-bucket"}')
        subject = provider.AwsHanaProvider.from_settings(self.settings, self.state, self.workload, execute=self.fake,
            credentials_file=lambda: self.root / 'private-credentials')
        fixture = test_hana_execution.HanaExecutionTests()
        access = subject.execution_access(fixture.key)
        result_key = urlsplit(access.result_url).path.lstrip('/')
        document = hana_result.encode_result(fixture.command_result())
        self.fake.objects[result_key] = ledger.mapping(cast(object, json.loads(document)))
        self.assertEqual(hana_result.decode_result(subject.execution_result(fixture.key)), fixture.command_result())
        for name, expected_error, seconds in (
                ('result', hana_result.HanaResultDownloadTimedOut, 83), ('observation', provider.ProviderPending, 10)):
            with self.subTest(name=name):
                if name == 'observation':
                    observation_key = urlsplit(access.observation_url).path.lstrip('/')
                    self.fake.objects[observation_key] = {}
                self.fake.lose_response = 'get-object'
                with self.assertRaises(expected_error) as failure:
                    if name == 'result':
                        _ = subject.execution_result(fixture.key)
                    else:
                        _ = subject.observe_execution(fixture.key)
                self.assertEqual(self.fake.calls[-1], ('s3api', 'get-object', seconds))
                self.assertNotIn('private-provider-id', str(failure.exception))
                if name == 'result':
                    self.assertIn('83 s (result_transfer_timeout_s)', str(failure.exception))

    def test_fence_put_errors_require_acknowledgement_or_window_expiry(self) -> None:
        for response, saved in (('timeout', True), ('403', False), ('AccessDenied', False), ('AccessDenied', True)):
            with self.subTest(response=response, saved=saved):
                fixture = test_hana_execution.HanaExecutionTests()
                fixture.root = self.root / f'{response}-{saved}'
                _ = fixture.dispatch()
                access = self.provider.execution_access(fixture.key)
                observation_key = urlsplit(access.observation_url).path.lstrip('/')
                generation = fixture.key.generation
                fence_key = f'control/{generation.deployment}/{generation.start_token}/fence.json'
                self.fake.objects.clear()
                self.fake.calls.clear()
                self.fake.failure = '' if saved else 'put-object'
                self.fake.lose_response = 'put-object' if response == 'timeout' else ''
                retries = ' (reached max retries: 1)' if saved else ''
                self.fake.failure_stderr = (
                    f'aws: [ERROR]: An error occurred ({response}) when calling the PutObject operation{retries}: fixture')
                self.fake.put_error_after_save = self.fake.failure_stderr if saved and response != 'timeout' else ''
                current = 1000.0
                observation = replace(fixture.objects.observe_execution(fixture.key),
                                      fenced=True, terminated=True, lease_free=True)

                def advance(seconds: float) -> None:
                    nonlocal current
                    current += seconds
                    if saved:
                        self.assertEqual(self.fake.objects[fence_key]['action'], 'fence')
                        self.fake.objects[observation_key] = ledger.mapping(
                            cast(object, json.loads(hana_protocol.encode_observation(observation))))

                window = hana_execution.FenceAcknowledgementWindow(1002.5, 1)
                with (mock.patch.object(time, 'monotonic', side_effect=lambda: current),
                      mock.patch.object(time, 'sleep', side_effect=advance)):
                    settled = hana_execution.settle_execution(fixture.root, fixture.request, self.provider,
                        release_claim=fixture.release, reason='The remote attempt ended', fence_window=window)
                operations = [operation for service, operation, _ in self.fake.calls if service == 's3api']
                if saved:
                    self.assertIsInstance(settled, hana_execution.HanaSettled)
                    self.assertEqual(current, 1001)
                    self.assertEqual(fixture.releases, 1)
                    self.assertEqual(operations, ['put-object', 'get-object', 'get-object'])
                else:
                    self.assertIsInstance(settled, hana_execution.HanaSettlementPending)
                    self.assertEqual(current, window.deadline)
                    self.assertEqual(fixture.releases, 0)
                    self.assertEqual(operations, ['put-object', 'get-object', 'get-object', 'get-object'])
                    self.assertTrue(hana_execution.claim_dispatch_pending(fixture.root, fixture.reference))

    def test_execution_observation_rejects_oversized_objects(self) -> None:
        fixture = test_hana_execution.HanaExecutionTests()
        key = urlsplit(self.provider.execution_access(fixture.key).observation_url).path.lstrip('/')
        self.fake.objects[key] = {'oversized': 'x' * (hana_protocol.CONTROL_MAX_BYTES + 1)}
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.observe_execution(fixture.key)

    def test_uncertain_stopped_start_remains_unresolved(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        self.fake.failure = 'start-instances'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.fake.failure = ''
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationUnresolved)

    def test_abandoned_start_with_a_lost_reply_keeps_exposure_until_a_later_stop(self) -> None:
        controller = box.HanaController(self.state, self.settings, self.workload, self.provider, clock=self.clock)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'fixture inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'fixture account inventory'))
        controller.register(IDENTITY)
        _ = record_lifecycle_image(self.state, self.workload)
        _ = controller.request('fixture-execution')
        self.assertEqual(controller.tick(), box.Lifecycle.STARTING)
        self.fake.lose_response = 'start-instances'
        with self.assertRaises(provider.ProviderPending):
            _ = controller.tick()
        self.fake.state = 'stopped'
        settings = conf.read(self.workload)
        self.clock.advance(settings.whole('start_timeout_seconds'))
        fraction = 0.95
        self.provider.clock = lambda: self.clock() + fraction
        self.fake.spot_read_seconds = 2
        self.assertEqual(controller.tick(), box.Lifecycle.STARTING)
        self.assertIsInstance(book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.clock.advance(settings.whole('aws_timeout_s'))
        fraction = 0.05
        self.assertEqual(controller.tick(), box.Lifecycle.STARTING)
        self.assertIsInstance(book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.fake.spot_read_seconds = 0
        self.assertEqual(controller.tick(), box.Lifecycle.IDLE_STOPPED)
        self.assertIsInstance(book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertEqual([op for _, op, _ in self.fake.calls].count('start-instances'), 1)

    def test_stop_during_instance_read_closes_exposure_at_observation_completion(self) -> None:
        controller = box.HanaController(self.state, self.settings, self.workload, self.provider, clock=self.clock)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'fixture inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'fixture account inventory'))
        controller.register(IDENTITY)
        _ = record_lifecycle_image(self.state, self.workload)
        _ = controller.request('fixture-execution')
        self.assertEqual(controller.tick(), box.Lifecycle.STARTING)
        self.clock.advance(1)
        self.assertEqual(controller.tick(), box.Lifecycle.STARTING)
        self.assertEqual(self.fake.state, 'running')
        self.assertIsInstance(book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)

        self.clock.advance(1)
        self.fake.stop_during_instance_read_seconds = 2
        self.fake.spot_read_seconds = 1
        completed_at = self.clock.now + 3
        self.assertEqual(controller.tick(), box.Lifecycle.STOPPED_UNKNOWN)
        self.assertEqual(book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed(completed_at))

    def test_recovery_fences_a_call_that_has_not_started(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        cancelled_at = self.clock.now
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
        saved = remote.read_record(self.mutation_path(operation))
        self.assertEqual(saved['generation'], operation.generation)
        self.assertEqual(saved['cancellation_at'], cancelled_at)
        self.assertNotIn('dispatched_at', saved)
        self.assertNotIn('accepted_at', saved)
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.assertNotIn('start-instances', [op for _, op, _ in self.fake.calls])

    def test_in_flight_lock_preserves_an_unresolved_mutation(self) -> None:
        operation = self.operation(box.Action.START)
        folder = self.state / 'hana/provider'
        folder.mkdir(parents=True)
        with (folder / 'mutation.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIsInstance(self.provider.recover(IDENTITY, self.start(), operation), box.MutationUnresolved)

    def test_stopped_bootstrap_reuses_the_saved_descriptor_before_start(self) -> None:
        controller = box.HanaController(self.state, self.settings, self.workload, self.provider, clock=self.clock)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'fixture inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'fixture account inventory'))
        controller.register(IDENTITY)
        _ = record_lifecycle_image(self.state, self.workload)
        _ = controller.request('fixture-execution')
        self.fake.failure = 'put-object'
        self.fake.userdata = 'fixture-old-data'
        with self.assertRaises(provider.ProviderPending):
            _ = controller.tick()
        pending = controller.inspect()
        assert isinstance(pending, box.HanaLifecycleEvidence) and isinstance(pending.operation, box.PendingOperation)
        self.assertEqual(pending.operation.action, box.Action.BOOTSTRAP)
        saved_path = self.mutation_path(pending.operation)
        saved = ledger.mapping(cast(object, json.loads(saved_path.read_text())))
        self.assertEqual(saved['dispatched'], True)
        self.assertEqual(saved['generation'], pending.operation.generation)
        self.assertEqual(saved['dispatched_at'], self.clock.now)
        self.assertNotIn('accepted_at', saved)
        self.fake.failure = ''
        self.clock.advance(1)
        self.assertEqual(controller.tick(), box.Lifecycle.STARTING)
        accepted = ledger.mapping(cast(object, json.loads(saved_path.read_text())))
        self.assertEqual(accepted['accepted'], True)
        self.assertEqual(accepted['dispatched_at'], saved['dispatched_at'])
        self.assertEqual(accepted['accepted_at'], self.clock.now)
        self.assertEqual(accepted['acceptance_source'], 'direct')
        self.assertEqual(accepted['descriptor'], saved['descriptor'])
        self.assertEqual(accepted['token'], pending.operation.token)
        self.assertEqual(cast(object, json.loads(base64.b64decode(self.fake.userdata))), saved['descriptor'])
        self.assertEqual(len(self.fake.objects), 3)
        operations = [op for _, op, _ in self.fake.calls]
        self.assertEqual(operations.count('export-credentials'), 1)
        self.assertNotIn('start-instances', operations)
        ready_to_start = controller.inspect()
        assert isinstance(ready_to_start, box.HanaLifecycleEvidence)
        assert isinstance(ready_to_start.operation, box.PendingOperation)
        self.assertEqual(ready_to_start.operation.action, box.Action.START)

    def test_bootstrap_restarts_at_the_next_unconfirmed_put(self) -> None:
        operation, start = self.operation(box.Action.BOOTSTRAP), self.start()
        for _step in range(2):
            self.fake.puts_before_timeout = 1
            with self.assertRaises(provider.ProviderPending):
                _ = self.provider.step(IDENTITY, start, operation)
            self.assertEqual(self.fake.userdata, '')
        self.fake.puts_before_timeout = 1
        self.assertEqual(self.provider.step(IDENTITY, start, operation), box.OperationAccepted(operation.token))
        self.assertEqual(len(self.fake.objects), 3)
        operations = [op for _, op, _ in self.fake.calls]
        self.assertEqual(operations.count('export-credentials'), 1)
        self.assertEqual(operations.count('put-object'), 5)
        self.assertEqual(operations.count('modify-instance-attribute'), 1)

    def test_resumed_bootstrap_uses_the_tick_for_the_next_unconfirmed_write(self) -> None:
        operation, start = self.operation(box.Action.BOOTSTRAP), self.start()
        self.fake.puts_before_timeout = 1
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        before = len(self.fake.calls)
        self.fake.failure = 'describe-instance-attribute'
        self.fake.puts_before_timeout = 2
        self.assertEqual(self.provider.step(IDENTITY, start, operation), box.OperationAccepted(operation.token))
        self.assertEqual([op for _, op, _ in self.fake.calls[before:]], [
            'describe-instances', 'describe-spot-instance-requests', 'put-object', 'put-object',
            'modify-instance-attribute'])
        self.assertEqual(len(self.fake.objects), 3)

    def test_bootstrap_recovery_reads_the_saved_user_data(self) -> None:
        operation, start = self.operation(box.Action.BOOTSTRAP), self.start()
        self.fake.lose_response = 'modify-instance-attribute'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.clock.advance(1)
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
        observed = remote.read_record(self.mutation_path(operation))
        self.assertNotIn('accepted', observed)
        self.assertNotIn('accepted_at', observed)
        before = len(self.fake.calls)
        self.clock.advance(1)
        self.assertEqual(self.provider.step(IDENTITY, start, operation), box.OperationAccepted(operation.token))
        accepted = remote.read_record(self.mutation_path(operation))
        self.assertEqual(accepted['accepted_at'], self.clock.now)
        self.assertEqual(accepted['acceptance_source'], 'observed')
        operations = [op for _, op, _ in self.fake.calls[before:]]
        self.assertIn('describe-instance-attribute', operations)
        self.assertNotIn('modify-instance-attribute', operations)
        self.assertNotIn('put-object', operations)

    def test_start_cancellation_keeps_the_fractional_fence_time(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        self.fake.lose_response = 'start-instances'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.fake.state = 'stopped'
        self.provider.clock = lambda: self.clock() + 0.75
        result = self.provider.recover(IDENTITY, start, operation, abandon_start=True)
        self.assertIsInstance(result, box.MutationUnresolved)
        cancelled = remote.read_record(self.mutation_path(operation))
        self.assertNotIn('accepted_at', cancelled)
        self.assertEqual(cancelled['cancellation_at'], self.clock.now)
        self.assertEqual(cancelled['cancelled_at'], self.clock.now + 0.75)
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation, abandon_start=True), box.MutationUnresolved)
        self.assertEqual(remote.read_record(self.mutation_path(operation)), cancelled)

    def test_legacy_mutation_records_read_without_a_rewrite(self) -> None:
        start = self.start()
        self.fake.state = 'running'
        for action, accepted_fields in ((box.Action.START, {}), (box.Action.STOP, {'accepted': True})):
            with self.subTest(action=action):
                operation = self.operation(action)
                path = self.mutation_path(operation)
                path.parent.mkdir(parents=True, exist_ok=True)
                legacy = json.dumps({'version': 1, 'token': operation.token, 'action': action.value,
                    'deployment': IDENTITY.deployment, 'dispatched': True, **accepted_fields})
                _ = path.write_text(legacy)
                self.assertEqual(self.provider.step(IDENTITY, start, operation), box.OperationAccepted(operation.token))
                self.assertEqual(path.read_text(), legacy)
        self.assertNotIn('stop-instances', [op for _, op, _ in self.fake.calls])

    def test_provider_retains_the_mutation_write_order(self) -> None:
        start = self.start()
        report_key = 'control/' + IDENTITY.deployment + '/' + start.token + '/report.json'
        writes: list[tuple[list[str], list[str]]] = []

        def record_write(path: Path, document: str, *, new_file_mode: int) -> None:
            saved = ledger.mapping(cast(object, json.loads(document)))
            flags = [flag for flag in ('dispatched', 'accepted', 'cancelled') if saved.get(flag) is True]
            writes.append(([op for _, op, _ in self.fake.calls], flags))
            write_text_atomic(path, document, new_file_mode=new_file_mode)

        with mock.patch.object(provider, 'write_text_atomic', side_effect=record_write):
            for action, request in ((box.Action.START, 'start-instances'), (box.Action.STOP, 'stop-instances'),
                                    (box.Action.PUBLISH, 'put-object')):
                with self.subTest(action=action):
                    self.fake.calls.clear()
                    writes.clear()
                    operation = self.operation(action)
                    expected: box.OperationAccepted | box.PermitAccepted = box.OperationAccepted(operation.token)
                    if action == box.Action.PUBLISH:
                        permit = hana_guest.WorkPermit(self.clock.now + 1, '1', 'fixture-boot')
                        operation = replace(operation, permit=permit, execution='fixture-execution')
                        self.fake.objects[report_key] = {'deployment': IDENTITY.deployment, 'start_token': start.token,
                            'token': operation.token, 'boot_id': permit.boot_id, 'generation': operation.generation,
                            'execution': operation.execution}
                        expected = box.PermitAccepted(operation.token, IDENTITY, box.VerifiedBoot(permit.boot_id, start.token),
                            operation.generation, operation.execution)
                    self.fake.lose_response = request
                    with self.assertRaises(provider.ProviderPending):
                        _ = self.provider.step(IDENTITY, start, operation)
                    self.clock.advance(1)
                    self.assertIsInstance(self.provider.recover(IDENTITY, start, operation), box.MutationSettled)
                    self.assertEqual(self.provider.step(IDENTITY, start, operation), expected)
                    self.assertEqual(writes, [([], ['dispatched'])])
                    self.assertNotIn('accepted_at', remote.read_record(self.mutation_path(operation)))

                    self.fake.calls.clear()
                    writes.clear()
                    direct = replace(operation, token=operation.token + '-direct')
                    if isinstance(expected, box.PermitAccepted):
                        self.fake.objects[report_key]['token'] = direct.token
                    self.assertEqual(self.provider.step(IDENTITY, start, direct), replace(expected, token=direct.token))
                    self.assertEqual(writes, [([], ['dispatched']), ([request], ['dispatched', 'accepted'])])

            self.fake.calls.clear()
            writes.clear()
            unstarted = replace(self.operation(box.Action.START), token='fixture-unstarted')
            self.assertIsInstance(self.provider.recover(IDENTITY, start, unstarted), box.MutationSettled)
            self.assertEqual(writes, [([], ['cancelled'])])

    def test_bootstrap_recovery_leaves_a_running_box_unchanged(self) -> None:
        operation, start = self.operation(box.Action.BOOTSTRAP), self.start()
        self.fake.failure = 'put-object'
        with self.assertRaises(provider.ProviderPending):
            _ = self.provider.step(IDENTITY, start, operation)
        self.fake.failure, self.fake.state = '', 'running'
        before = len(self.fake.calls)
        with self.assertRaisesRegex(provider.ProviderPending, 'bootstrap operation remains unresolved'):
            _ = self.provider.step(IDENTITY, start, operation)
        self.assertNotIn('put-object', [op for _, op, _ in self.fake.calls[before:]])
        self.assertEqual(self.fake.userdata, '')

    def test_abandoned_stopped_mutations_refuse_a_late_dispatch(self) -> None:
        for action, failure in ((box.Action.BOOTSTRAP, 'put-object'), (box.Action.START, 'start-instances')):
            with self.subTest(action=action):
                operation, start = self.operation(action), self.start()
                self.fake.failure = failure
                with self.assertRaises(provider.ProviderPending):
                    _ = self.provider.step(IDENTITY, start, operation)
                self.fake.failure = ''
                reply = self.provider.recover(IDENTITY, start, operation, abandon_start=True)
                if action == box.Action.START:
                    self.assertIsInstance(reply, box.MutationUnresolved)
                else:
                    self.assertIsInstance(reply, box.MutationSettled)
                before = len(self.fake.calls)
                with self.assertRaisesRegex(provider.ProviderPending, 'operation is cancelled'):
                    _ = self.provider.step(IDENTITY, start, operation)
                self.assertEqual(len(self.fake.calls), before)
                self.assertEqual(self.fake.state, 'stopped')

    def test_abandoned_accepted_start_can_still_need_a_stop(self) -> None:
        operation, start = self.operation(box.Action.START), self.start()
        _ = self.provider.step(IDENTITY, start, operation)
        self.fake.state = 'stopped'
        self.assertIsInstance(self.provider.recover(IDENTITY, start, operation, abandon_start=True), box.StartMayBeRunning)

    def test_ownership_mismatch_and_errors_reveal_no_private_data(self) -> None:
        self.fake.ownership = 'other-deployment'
        with self.assertRaisesRegex(provider.ProviderPending, '^Hana instance ownership differs$'):
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.OBSERVE))
        self.fake.failure = 'stop-instances'
        with self.assertRaisesRegex(provider.ProviderPending, '^Hana provider call failed: ec2:StopInstances$'):
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.STOP))

    def test_cli_failure_reports_only_the_aws_error_code(self) -> None:
        self.fake.failure = 'modify-instance-attribute'
        self.fake.failure_stderr = (
            'An error occurred (UnauthorizedOperation) when calling the ModifyInstanceAttribute operation: '
            + 'You are not authorized to perform this operation. User: arn:aws:sts::000000000000:assumed-role/x '
            + 'Encoded authorization failure message: abcDEF'
        )
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.BOOTSTRAP))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:ModifyInstanceAttribute (UnauthorizedOperation)')

    def test_cli_failure_reports_a_code_after_the_error_prefix(self) -> None:
        self.fake.failure = 'modify-instance-attribute'
        self.fake.failure_stderr = (
            '\naws: [ERROR]: An error occurred (UnauthorizedOperation) when calling the '
            + 'ModifyInstanceAttribute operation: Fixture access is denied'
        )
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.BOOTSTRAP))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:ModifyInstanceAttribute (UnauthorizedOperation)')

    def test_cli_failure_reports_a_code_when_retries_are_exhausted(self) -> None:
        self.fake.failure = 'describe-instances'
        self.fake.failure_stderr = (
            'aws: [ERROR]: An error occurred (RequestLimitExceeded) when calling the '
            + 'DescribeInstances operation (reached max retries: 2): Fixture request limit is exceeded'
        )
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.OBSERVE))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:DescribeInstances (RequestLimitExceeded)')

    def test_cli_failure_omits_a_code_with_invalid_characters(self) -> None:
        self.fake.failure = 'modify-instance-attribute'
        self.fake.failure_stderr = 'An error occurred (unexpected/private) when calling the ModifyInstanceAttribute operation'
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.BOOTSTRAP))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:ModifyInstanceAttribute')

    def test_cli_failure_omits_key_and_account_like_codes(self) -> None:
        self.fake.failure = 'describe-instances'
        for token in ('AKIAFAKEFAKEFAKE', '000000000000', 'Client.AKIAFAKEFAKEFAKE'):
            with self.subTest(token=token):
                self.fake.failure_stderr = f'An error occurred ({token}) when calling the DescribeInstances operation'
                with self.assertRaises(provider.ProviderPending) as pending:
                    _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.OBSERVE))
                self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:DescribeInstances')

    def test_cli_failure_omits_a_header_after_other_text(self) -> None:
        self.fake.failure = 'describe-instances'
        self.fake.failure_stderr = (
            'Fixture diagnostic\n'
            + 'An error occurred (UnauthorizedOperation) when calling the DescribeInstances operation'
        )
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.OBSERVE))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:DescribeInstances')

    def test_cli_failure_reports_a_dotted_aws_code(self) -> None:
        self.fake.failure = 'describe-instances'
        self.fake.failure_stderr = (
            '\n  An error occurred (InvalidInstanceID.NotFound) when calling the DescribeInstances operation: '
            + 'Fixture instance is missing'
        )
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.OBSERVE))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:DescribeInstances (InvalidInstanceID.NotFound)')

    def test_cli_timeout_reports_only_the_action(self) -> None:
        self.fake.lose_response = 'modify-instance-attribute'
        with self.assertRaises(provider.ProviderPending) as pending:
            _ = self.provider.step(IDENTITY, self.start(), self.operation(box.Action.BOOTSTRAP))
        self.assertEqual(str(pending.exception), 'Hana provider call failed: ec2:ModifyInstanceAttribute')

    def test_scaler_environment_removes_inherited_admin_credentials(self) -> None:
        with mock.patch.dict('os.environ', {'AWS_ACCESS_KEY_ID': 'inherited-admin', 'AWS_SESSION_TOKEN': 'inherited-token'}):
            subject = provider.AwsHanaProvider.from_settings(self.settings, self.state, self.workload, execute=self.fake)
            _ = subject.step(IDENTITY, box.NoDemandStart(), self.operation(box.Action.OBSERVE))
        self.assertNotIn('AWS_ACCESS_KEY_ID', self.fake.environments[-1])
        self.assertNotIn('AWS_SESSION_TOKEN', self.fake.environments[-1])
        self.assertEqual(self.fake.environments[-1]['AWS_CONFIG_FILE'], '/dev/null')

    def test_receiver_ignores_legacy_state_and_checks_the_shutdown_fence(self) -> None:
        folder = self.root / 'guest'
        folder.mkdir()
        boot = self.root / 'boot-id'
        _ = boot.write_text('fixture-boot')
        paths = hana_guest.GuestPaths(folder, boot)
        settings = hana_guest.read_settings(conf.read(self.workload))
        descriptor: dict[str, object] = {'version': 1, 'deployment': IDENTITY.deployment, 'start_token': 'fixture-start',
            'expires': self.clock.now + conf.read(self.workload).whole('control_url_seconds')}
        control: dict[str, object] = {'deployment': IDENTITY.deployment, 'start_token': 'fixture-start', 'token': 'fixture-token',
            'expected_generation': 0, 'execution': 'fixture-execution',
            'permit': {'expires_at': self.clock.now + settings.permit_max_seconds, 'generation': '1', 'boot_id': 'fixture-boot'}}
        legacy = json.dumps(control['permit'])
        _ = (folder / 'work-permit.json').write_text(legacy)
        _ = (folder / 'generation').write_text('1')
        with hana_guest.guest_lock(paths):
            self.assertIs(hana_guest.bound_permit(settings, paths, clock=self.clock, uptime=lambda: 1),
                          hana_guest.PermitRefusal.NOT_INSTALLED)
        with self.assertRaisesRegex(ValueError, 'generation changed'):
            _ = hana_receiver.receive(settings, descriptor, {**control, 'expected_generation': 1},
                                      paths=paths, clock=self.clock)
        reply = hana_receiver.receive(settings, descriptor, control, paths=paths, clock=self.clock)
        self.assertEqual(reply['generation'], 1)
        self.assertEqual(hana_guest.read_permit(paths.permit),
                         hana_guest.WorkPermit(self.clock.now + settings.permit_max_seconds, '1', 'fixture-boot'))
        self.assertEqual((folder / 'work-permit.json').read_text(), legacy)
        self.assertEqual((folder / 'generation').read_text(), '1')
        self.assertEqual(hana_receiver.receive(settings, descriptor, control, paths=paths, clock=self.clock), reply)
        _ = paths.fence.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'shutdown is fenced'):
            _ = hana_receiver.receive(settings, descriptor, control, paths=paths, clock=self.clock)

    def test_receiver_program_compiles_with_the_reviewed_runtime_entry_point(self) -> None:
        rendered = hana_receiver.receiver_program()
        _ = compile(rendered, '<hana receiver>', 'exec')
        self.assertIn('from scripts.aws_target.hana_receiver import installed_main', rendered)

    def test_no_approval_prints_only_the_plan_and_makes_no_external_call(self) -> None:
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output', execute=self.fake)
        with mock.patch.object(conf, 'read_or_repository', return_value=conf.read(self.workload)), \
                mock.patch('sys.stdout', new_callable=StringIO) as output:
            self.assertFalse(subject.hana_setup())
        self.assertIn('persistent Spot', output.getvalue())
        self.assertIn('Hana placement is enabled. This command does not change it.', output.getvalue())
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.state / 'hana/setup.json').exists())
