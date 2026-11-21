"""Exercise durable cloud gates with private files and a fake AWS transport."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout, suppress
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from functools import cached_property
from hashlib import sha256
from io import StringIO
import base64
import faulthandler
import fcntl
import gzip
import json
import os
import re
import selectors
import signal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
import threading
import time
from types import SimpleNamespace
from typing import BinaryIO, cast, override
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import aws_trial, cloud_gate, gate_box, ledger, s3_presign
from scripts.aws_target.aws_target import USAGE_ONLY, AwsCli, AwsUnavailable
from scripts.aws_target.config import read_settings
from scripts.aws_target.ledger_test_support import (
    CostFixture, account_reply, install_aws_fixture_settings, instant, numeric_settings,
    override_fixture_settings, text_setting,
)
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile, queue_place
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_queue.admission_wire import CloudGroup, Owner, ReservedCloudSlot
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.buildlog import index, store
from scripts.build_targets import target_config
from scripts.production import affected_modules
from scripts.production import merge_gate as gate
from scripts.production.gate_modules import GateRefused
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.process_group import send_process_group
from scripts.lib.process_start import Gone, Started, Unreadable, process_start
from scripts.build_hold.build_hold_test_support import IsolatedBuildHoldTest, invoke_hold, write_holder
from scripts.build_hold.owning_session import OwningSession, owning_session
from scripts.build_queue import admission_records, queue_evidence
from scripts.lib.build_steps import read_step_names
from scripts.message.unit_labels import OwnerRole, UnitLabels


def service_environment(command: Sequence[str]) -> dict[str, str]:
    """Read the environment assignments from a service command."""
    return dict(argument.removeprefix('--setenv=').split('=', 1)
                for argument in command if argument.startswith('--setenv='))


TRIAL_GROUP_LEADER = """import subprocess, sys

with subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE) as coordinator:
    while chunk := coordinator.stdout.read1(65536):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    status = coordinator.wait()
sys.exit(status if status >= 0 else 128 - status)
"""
WATCHDOG_RESULT_TIMEOUT_SECS = TEST_HANG_GUARD_SECS / 2


def stop_trial_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            send_process_group(process.pid, signal.SIGKILL)


@dataclass(frozen=True)
class TrialDiagnostics:
    stderr: BinaryIO
    worker_stacks: tuple[Path, ...]

    def read(self) -> str:
        output: list[bytes] = []
        with suppress(OSError, ValueError):
            output.append(os.pread(self.stderr.fileno(), 64 * 1024, 0))
        for path in self.worker_stacks:
            with suppress(OSError), path.open('rb') as worker:
                output.append(worker.read(64 * 1024))
        return b''.join(output).decode(errors='replace')


class CloudGateFixtureWatchdog:
    """Report a blocked test and stop the child processes before it exits."""

    def __init__(self, name: str, limit_seconds: float) -> None:
        self.name: str = name
        self.limit_seconds: float = limit_seconds
        self.deadline: float = float('inf')
        self.trials: dict[subprocess.Popen[bytes], TrialDiagnostics] = {}
        self.trials_lock: threading.Lock = threading.Lock()

    @contextmanager
    def watch(self, *, report_fd: int = 2) -> Generator[None]:
        self.deadline = time.monotonic() + self.limit_seconds
        watchdog = threading.Timer(self.limit_seconds, self.expire, (report_fd,))
        watchdog.daemon = True
        watchdog.start()
        try:
            yield
        finally:
            watchdog.cancel()
            watchdog.join()

    def expire(self, report_fd: int) -> None:
        with self.trials_lock:
            for process in self.trials:
                stop_trial_group(process)
            for process in self.trials:
                with suppress(subprocess.TimeoutExpired):
                    _ = process.wait(timeout=WATCHDOG_RESULT_TIMEOUT_SECS)
            reports = [diagnostics.read() for diagnostics in self.trials.values()]
        _ = os.write(report_fd, f'{self.name}: cloud gate fixture watchdog expired\n'.encode())
        for report in reports:
            _ = os.write(report_fd, report.encode())
        faulthandler.dump_traceback(file=report_fd, all_threads=True)
        os._exit(1)

    @contextmanager
    def trial(self, command: Sequence[str], *, reap_timeout: float,
              worker_stacks: tuple[Path, ...] = ()) -> Generator[tuple[subprocess.Popen[bytes], TrialDiagnostics]]:
        with TemporaryFile() as errors:
            with self.trials_lock:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=errors, start_new_session=True)
                diagnostics = TrialDiagnostics(errors, worker_stacks)
                self.trials[process] = diagnostics
            try:
                yield process, diagnostics
            finally:
                assert process.stdout is not None
                with process.stdout, self.trials_lock:
                    try:
                        stop_trial_group(process)
                        _ = process.wait(timeout=reap_timeout)
                    finally:
                        del self.trials[process]

    def run_trial(self, command: Sequence[str], worker_stacks: tuple[Path, ...]) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        remaining = max(0, self.deadline - started)
        # Keep the group leader alive until the workers close the coordinator's stdout pipe.
        command = [sys.executable, '-c', TRIAL_GROUP_LEADER, *command, str(started + remaining / 4)]
        stop_at = started + remaining / 2
        output = bytearray()
        with (self.trial(command, reap_timeout=remaining / 4, worker_stacks=worker_stacks) as (process, diagnostics),
              selectors.DefaultSelector() as stdout_ready):
            assert process.stdout is not None
            _ = stdout_ready.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    seconds = stop_at - time.monotonic()
                    if seconds <= 0 or not stdout_ready.select(seconds):
                        raise subprocess.TimeoutExpired(command, remaining / 2)
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output.extend(chunk)
                # Serialize reaping with the watchdog's check and group stop.
                with self.trials_lock:
                    status = process.wait(timeout=max(0, stop_at - time.monotonic()))
            except subprocess.TimeoutExpired:
                with self.trials_lock:
                    stop_trial_group(process)
                    with suppress(subprocess.TimeoutExpired):
                        _ = process.wait(timeout=remaining / 4)
                raise AssertionError('The cancellation fixture did not exit.\n'
                                     + output.decode(errors='replace') + diagnostics.read()) from None
            return subprocess.CompletedProcess(command, status, output.decode(errors='replace'), diagnostics.read())


class CloudGateHangGuard(unittest.TestCase):
    @cached_property
    def watchdog(self) -> CloudGateFixtureWatchdog:
        # The 600-second limit gives one test time to finish under load.
        return CloudGateFixtureWatchdog(self.id(), TEST_HANG_GUARD_SECS)

    @override
    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult | None:
        with self.watchdog.watch():
            return super().run(result)


class CloudWorkerHoldTests(CloudGateHangGuard, IsolatedBuildHoldTest):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        install_aws_fixture_settings(cls.seed)

    def test_forwarded_worker_session_cannot_pass_another_sessions_hold(self) -> None:
        configuration = conf.read(self.scratch / 'cloud_gate.conf')
        for paired in (False, True):
            with (self.subTest(paired=paired), mock.patch.dict(os.environ,
                  {'CLAUDE_CODE_SESSION_ID': 'fixture-caller', 'CODEX_THREAD_ID': ''}),
                  mock.patch.object(cloud_gate, 'settings', return_value=configuration)):
                command = cloud_gate.service_command('scripts.aws_target.aws_trial',
                    ['--paired-worker', str(self.scratch / 'trial')], 'fixture', paired=paired)
                environment = {**os.environ, **service_environment(command)}
                self.assertEqual(owning_session(environment), OwningSession('fixture-caller'))
                holders = Path(environment['BUILD_HOLD_DIR'])
                holders.mkdir(exist_ok=True)
                path = write_holder(holders, 'fixture-other', '2026-11-13T12:00:00+00:00', 'fixture work')
                result = invoke_hold(['wait'], environment)
                self.assertEqual(result.returncode, 10, result.stdout + result.stderr)
                self.assertIn('fixture-other', result.stdout)
                path.unlink()


class FakeGateAws:
    """Implement only the operations that a gate scaler can request."""
    def __init__(self) -> None:
        self.log: str = 'fixture cloud output\n'
        self.commands: list[list[str]] = []
        self.launches: list[dict[str, object]] = []
        self.refusals: list[str] = []
        self.result: dict[str, object] = {}
        self.has_result: bool = True
        self.boot_error: str = ''
        self.has_log: bool = True
        self.state: str = 'running'
        self.state_reason: dict[str, str] = {}
        self.has_instance: bool = True
        self.terminated: bool = False
        self.terminated_instances: set[str] = set()
        self.confirm_termination: bool = True
        self.upload_error: str = ''
        self.upload_timeout: bool = False
        self.launch_timeouts: int = 0
        self.launch_errors: list[subprocess.SubprocessError] = []
        self.launch_reply: dict[str, object] = {'Instances': [{'InstanceId': 'i-fixture'}]}
        self.describe_errors: list[AwsUnavailable | subprocess.TimeoutExpired] = []
        self.read_timeouts: list[str] = []
        self.billing_pages: list[dict[str, object]] = []
        self.spot_history: dict[str, list[dict[str, object]]] = {}

    def artifact_bodies(self) -> dict[str, str]:
        objects: dict[str, str] = {}
        if self.has_log:
            objects['gate.log'] = self.log
        if self.boot_error:
            objects['boot-error.json'] = json.dumps({'error': self.boot_error}, sort_keys=True) + '\n'
        if self.has_result:
            objects['result.json'] = json.dumps(self.result, sort_keys=True) + '\n'
        return objects

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        index = args.index('--region') + 2
        service, operation = args[index:index + 2]
        if self.read_timeouts and operation == self.read_timeouts[0]:
            _ = self.read_timeouts.pop(0)
            raise subprocess.TimeoutExpired(f'aws s3api {operation}', 30)
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = self.billing_pages.pop(0) if self.billing_pages else account_reply(arguments)
        elif operation == 'describe-spot-price-history':
            reply = {'SpotPriceHistory': self.spot_history.get(args[args.index('--region') + 1], [])}
        elif operation == 'export-credentials':
            assert service == 'configure'
            reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret', 'SessionToken': 'fixture-token'}
        elif operation == 'put-object':
            if self.upload_timeout:
                raise subprocess.TimeoutExpired('aws s3api put-object', 30)
            error = self.upload_error
        elif operation == 'run-instances':
            self.launches.append(ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1]))))
            if self.launch_timeouts:
                self.launch_timeouts -= 1
                raise subprocess.TimeoutExpired('aws run-instances', 1)
            if self.launch_errors:
                raise self.launch_errors.pop(0)
            error = self.refusals.pop(0) if self.refusals else ''
            reply = self.launch_reply
        elif operation == 'list-objects-v2':
            prefix = args[args.index('--prefix') + 1]
            contents = [{'Key': prefix + name, 'ETag': '"' + sha256(body.encode()).hexdigest() + '"'}
                        for name, body in self.artifact_bodies().items()]
            if contents:
                reply['Contents'] = contents
        elif operation == 'get-object':
            key_at = args.index('--key') + 1
            key, destination = args[key_at], Path(args[key_at + 1])
            objects = self.artifact_bodies()
            name = key.rsplit('/', 1)[-1]
            if name in objects:
                _ = destination.write_text(objects[name])
            else:
                error = 'NoSuchKey'
        elif operation == 'terminate-instances':
            self.terminated = True
            self.terminated_instances.update(args[args.index('--instance-ids') + 1:args.index('--output')])
        elif operation == 'describe-instances':
            if self.describe_errors:
                raise self.describe_errors.pop(0)
            instance_ids = args[args.index('--instance-ids') + 1:args.index('--output')] if '--instance-ids' in args else ['i-fixture']
            instances: list[dict[str, object]] = []
            for instance_id in instance_ids:
                state = 'terminated' if instance_id in self.terminated_instances and self.confirm_termination else self.state
                instance: dict[str, object] = {'InstanceId': instance_id, 'State': {'Name': state},
                                              'LaunchTime': '2026-01-15T00:00:00+00:00'}
                if self.state_reason:
                    instance['StateReason'] = self.state_reason
                instances.append(instance)
            reply = {'Reservations': [{'Instances': instances}] if self.has_instance else []}
        else:
            raise AssertionError((service, operation))
        return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)


class CloudGateTests(CloudGateHangGuard, CostFixture):
    now: float = 0
    book: ledger.CostLedger = ledger.CostLedger(Path())
    fake: FakeGateAws = FakeGateAws()
    folder: Path = Path()
    request: dict[str, object] = {}
    deployment: dict[str, object] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root / "buildlog")})))
        override_fixture_settings(self.root / "merge_gate.conf", "GATE_TIME_LIMIT_FACTOR=2")
        override_fixture_settings(self.root / "test_times.conf", "TOP_RUNS=3\nGATE_FAILURE_LOG_KIB=4")
        # The fixture keeps two sizes, so the shipped type list can change without these cases.
        override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=on')
        override_fixture_settings(self.root / 'cloud_gate.conf',
            'instance_types=m7a.8xlarge m7a.4xlarge\nspot_max_usd_per_hour=0.8\n'
            + 'attempt_allowance_usd=0.02\nlaunch_reply_tries=2\n')
        self.now = float(instant('2026-01-15T01:00:00+00:00'))
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(int(self.now) - 1,
            (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
        self.book.observe(ledger.InstanceObservation(int(self.now) - 1, 'stopped', 'fixture'))
        self.fake = FakeGateAws()
        self.folder = self.root / 'attempt'
        self.folder.mkdir()
        _ = (self.folder / 'source.bundle').write_bytes(b'private bundle')
        self.request = {'attempt': 'attempt', 'commit': 'commit', 'tree': 'tree', 'mode': 'merge-gate',
            'origin': '/private/origin', 'modules': ['scripts/fixture/test_ok.py'],
            'expires': int(self.now) + 1200, 'module_time_limit_s': 1800, 'source_key': 'source/private.bundle'}
        cloud_gate.save(self.folder / 'request.json', self.request)
        self.deployment = {'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'revision': 'fixture-revision',
            'regions': [{'region': 'fixture-region', 'template': 'fixture-template', 'version': '1',
                'security_group': 'fixture-group',
                'subnets': [{'id': 'fixture-a', 'zone': 'a'}, {'id': 'fixture-b', 'zone': 'b'}]}]}
        self.fake.result = self.result()

    def test_time_limit_returns_red_with_traceback_and_ends_the_box(self) -> None:
        import sqlite3
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection, connection:
            connection.execute("""INSERT INTO gate_runs
                (id, started_at, step, vcpus, max_jobs, status, aborted, selection, wall_s, modules_run)
                VALUES ('normal', '2026-01-01', 'merge-gate', 32, 32, 0, 0, 'full', 7, 1)""").close()
        modules = ['scripts/fixture/test_ok.py', 'scripts/fixture/nested/test_busy.py']
        log = ('Runner: 2 modules, whole machine, 32 CPUs\n'
               'Starting module: fixture/test_ok.py\n'
               '--- fixture/test_ok.py exit=1 ---\n'
               'Traceback (most recent call last):\nAssertionError: fixture red\n'
               '--- end module failure ---\n'
               'fixture/test_ok.py exit=1 seconds=0.50\n'
               'Starting module: fixture/nested/test_busy.py\n')
        config = self.root / 'cloud_gate.conf'
        for attempt in ('time-limit', 'spot-deadline', 'list-timeout', 'spot-list-timeout',
                        'spot-during-list', 'spot-boot-log-timeout', 'ec2-recovered'):
            with self.subTest(attempt=attempt):
                folder = self.root / attempt
                folder.mkdir()
                request = {**self.request, 'attempt': attempt, 'modules': modules, 'source_key': ''}
                cloud_gate.save(folder / 'request.json', request)
                fake = FakeGateAws()
                fake.has_result = False
                fake.log = log
                stopped = attempt in ('spot-deadline', 'spot-list-timeout', 'spot-boot-log-timeout')
                started = self.now
                poll_s = {'spot-during-list': '13', 'ec2-recovered': '3'}.get(attempt, '15')
                numeric_settings(config, {'result_poll_s': poll_s, 'observe_outage_s': '10'})
                if attempt == 'ec2-recovered':
                    _ = config.write_text(re.sub(r'(?m)^instance_check_s=.*$', 'instance_check_s=3', config.read_text()))

                def advance_to_deadline(seconds: float) -> None:
                    if stopped:
                        self.advance(1)
                        fake.state = 'shutting-down'
                        fake.state_reason = {'Code': 'Server.SpotInstanceTermination'}
                        seconds -= 1
                    self.advance(seconds)
                    if attempt in ('list-timeout', 'spot-list-timeout'):
                        fake.read_timeouts = ['list-objects-v2']
                    if attempt == 'spot-boot-log-timeout':
                        fake.log += 'fixture boot failed\n'
                        fake.boot_error = 'fixture boot failed'
                    if attempt == 'ec2-recovered' and self.now - started < 15:
                        fake.describe_errors = [AwsUnavailable('Could not connect to the endpoint')]

                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if (attempt == 'spot-boot-log-timeout' and self.now > started and 'get-object' in arguments
                            and arguments[arguments.index('--key') + 1].endswith('/gate.log')):
                        fake.read_timeouts = ['get-object']
                    reply = fake(arguments, **options)
                    if 'list-objects-v2' in arguments:
                        elapsed = self.now - started
                        if attempt == 'spot-during-list' and elapsed == 13:
                            self.advance(2)
                            fake.state = 'shutting-down'
                            fake.state_reason = {'Code': 'Server.SpotInstanceTermination'}
                        if attempt == 'ec2-recovered' and elapsed == 15:
                            self.assertIn('describe-instances', fake.commands[-2])
                            return subprocess.CompletedProcess(list(arguments), 255, '', 'Could not connect to the endpoint')
                    return reply

                worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
                    self.book, self.settings, folder / 'reservation.cgroup', clock=lambda: self.now, pause=advance_to_deadline)
                with (mock.patch.object(time, 'monotonic', side_effect=lambda: self.now),
                      redirect_stdout(StringIO()) as output):
                    outcome = worker.execute(folder, request, self.deployment)
                self.assertTrue(fake.terminated)
                self.assertFalse(fake.read_timeouts)
                self.assertEqual(sum('list-objects-v2' in command for command in fake.commands),
                                 {'spot-during-list': 3, 'ec2-recovered': 6}.get(attempt, 2))
                if attempt == 'spot-boot-log-timeout':
                    self.assertEqual(outcome, {'status': 'box-failed', 'reason': 'fixture boot failed'})
                    downloads = [command[command.index('--key') + 1].rsplit('/', 1)[-1]
                                 for command in fake.commands if 'get-object' in command]
                    self.assertEqual(downloads, ['gate.log', 'boot-error.json', 'gate.log'])
                    continue
                if stopped or attempt == 'spot-during-list':
                    self.assertEqual(outcome, {
                        'status': 'interrupted', 'interruption_code': 'Server.SpotInstanceTermination',
                        'reason': 'The instance stopped without a final result (Server.SpotInstanceTermination)'})
                    self.assertFalse((folder / cloud_gate.RESULT_FILE).exists())
                    continue
                if attempt == 'ec2-recovered':
                    self.assertEqual(output.getvalue().count('Cloud gate poll skipped:'), 5)
                self.assertEqual(outcome['status'], 'complete')
                row = cloud_gate.outcome_result(folder, outcome)
                cloud_gate.validate_result(row, request)
                run = gate_box.decode_run(row['gate'])
                self.assertFalse(run.passed)
                self.assertIn('limit 14 s; elapsed 15.000 s; running modules: fixture/nested/test_busy.py', run.time_limit_report)
                self.assertIn('Traceback (most recent call last):\nAssertionError: fixture red', run.time_limit_report)
                self.assertIn('fixture/test_ok.py', run.time_limit_report)
                self.assertEqual(run.jobs[0].returncode, 1)
                self.assertEqual(run.jobs[1].returncode, 124)
                self.assertEqual(ledger.mapping(row['record'])['vcpus'], 32)

    def test_finished_result_wins_when_polling_crosses_the_time_limit(self) -> None:
        import sqlite3
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection, connection:
            connection.execute("""INSERT INTO gate_runs
                (id, started_at, step, vcpus, max_jobs, status, aborted, selection, wall_s, modules_run)
                VALUES ('normal', '2026-01-01', 'merge-gate', 32, 32, 0, 0, 'full', 1, 1)""").close()
        for arrival in ('before-poll', 'at-stop', 'at-stop-log-timeout', 'ec2-timeout', 'at-deadline',
                        'ec2-outage-limit', 'boot-and-result'):
            with self.subTest(arrival=arrival):
                folder = self.root / arrival
                folder.mkdir()
                request = {**self.request, 'attempt': arrival, 'source_key': '', 'expires': int(self.now) + 1200}
                cloud_gate.save(folder / 'request.json', request)
                fake = FakeGateAws()
                fake.has_result = False
                fake.log = 'Runner: 1 modules, whole machine, 32 CPUs\nStarting module: fixture/test_ok.py\n'
                fake.result = {**self.result(), 'attempt': arrival}
                ledger.mapping(fake.result['record'])['id'] = 'gate-' + arrival
                poll_s = 12 if arrival == 'ec2-outage-limit' else 3
                numeric_settings(self.root / 'cloud_gate.conf', {'result_poll_s': str(poll_s), 'observe_outage_s': '10'})
                if arrival == 'ec2-outage-limit':
                    fake.describe_errors = [AwsUnavailable('Could not connect to the endpoint')]
                pauses: list[float] = []

                def result_arrives(seconds: float) -> None:
                    self.advance(seconds)
                    pauses.append(seconds)
                    if arrival in ('before-poll', 'ec2-timeout', 'ec2-outage-limit', 'boot-and-result'):
                        fake.has_result = True
                    if arrival == 'ec2-timeout':
                        fake.describe_errors = [subprocess.TimeoutExpired('aws ec2 describe-instances', 1)]
                    if arrival == 'ec2-outage-limit':
                        fake.describe_errors = [AwsUnavailable('Could not connect to the endpoint')]
                    if arrival == 'boot-and-result':
                        fake.boot_error = 'fixture boot failed'
                        fake.log += 'fixture boot failed\n'
                    if arrival in ('at-stop', 'at-stop-log-timeout'):
                        fake.state = 'shutting-down'
                        fake.state_reason = {'Code': 'Server.SpotInstanceTermination'}
                    if arrival == 'at-stop-log-timeout':
                        fake.log += 'fixture/test_ok.py exit=0 seconds=1.00\n'

                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if (arrival == 'at-stop-log-timeout' and pauses and 'get-object' in arguments
                            and arguments[arguments.index('--key') + 1].endswith('/gate.log')
                            and not (folder / cloud_gate.RESULT_FILE).exists()):
                        raise subprocess.TimeoutExpired('aws s3api get-object', 1)
                    reply = fake(arguments, **options)
                    if pauses and not fake.terminated and 'describe-instances' in arguments:
                        fake.has_result = True
                    if arrival == 'at-deadline' and fake.has_result and 'list-objects-v2' in arguments:
                        self.now = float(ledger.instant(request, 'expires'))
                    return reply

                worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
                    self.book, self.settings, folder / 'reservation.cgroup', clock=lambda: self.now, pause=result_arrives)
                with mock.patch.object(time, 'monotonic', side_effect=lambda: self.now):
                    outcome = worker.execute(folder, request, self.deployment)
                self.assertEqual(pauses, [poll_s])
                self.assertFalse(fake.describe_errors)
                self.assertEqual(sum('list-objects-v2' in command for command in fake.commands), 2)
                self.assertTrue(fake.terminated)
                if arrival == 'boot-and-result':
                    self.assertEqual(outcome, {'status': 'box-failed', 'reason': 'fixture boot failed'})
                    self.assertFalse((folder / cloud_gate.RESULT_FILE).exists())
                    self.assertEqual((folder / 'gate.log').read_text(), fake.log)
                    continue
                self.assertEqual(outcome['status'], 'complete')
                result = cloud_gate.outcome_result(folder, outcome)
                cloud_gate.validate_result(result, request)
                run = gate_box.decode_run(result['gate'])
                self.assertTrue(run.passed, run.time_limit_report)
                self.assertEqual(run.time_limit_report, '')
                if arrival in ('at-stop', 'at-stop-log-timeout'):
                    listing = next(i for i, command in reversed(list(enumerate(fake.commands))) if 'list-objects-v2' in command)
                    self.assertTrue(all('describe-instances' in command for command in fake.commands[listing - 2:listing]))

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def result(self, status: int = 0) -> dict[str, object]:
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', status, 1, 'OK'),), 1, 2)
        record = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'fixture-guest', self.now,
                                            self.now + 1, 'merge-gate')
        return {'attempt': 'attempt', 'commit': 'commit', 'tree': 'tree', 'host': 'fixture-guest',
                'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run), 'record': record, 'gate_s': 1.0}

    def execute(self) -> dict[str, object]:
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        return worker.execute(self.folder, self.request, self.deployment)

    def steering_fixture(self) -> tuple[cloud_gate.GateWorker, list[cloud_gate.GateLaunchVariant]]:
        types = ('m7a.8xlarge', 'c7a.8xlarge', 'm7a.4xlarge')
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', ' '.join(types))
        numeric_settings(self.root / 'cloud_gate.conf', {'remote_region_transfer_usd': '0',
            'gate_wait_usd_per_minute': '0'})
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_price_live_read_timeout_s=.*$',
                                  'spot_price_live_read_timeout_s=30', path.read_text()))
        text_setting(self.root / 'cloud_gate.conf', 'region_extra_gate_seconds', '{}')
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region,
            'subnets': [{'id': region + suffix, 'zone': region + suffix} for suffix in ('a', 'b')]}
            for region in ('us-east-2', 'us-west-2')]
        variants = [cloud_gate.GateLaunchVariant(region, size, region + suffix, region + suffix)
                    for region in ('us-east-2', 'us-west-2') for size in types for suffix in ('a', 'b')]
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        self.assertIsInstance(self.book.reserve_gate(self.settings, ledger.GateReservation(
            'attempt', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'))), ledger.GateReserved)
        return worker, variants

    def variant_prices(self, variants: Sequence[cloud_gate.GateLaunchVariant],
                       rates: dict[tuple[str, str, str], str], *, age: int = 0) -> None:
        ledger.write_spot_prices(self.state, [ledger.RegionalSpotPrice(row.region, row.zone, row.instance_type,
            Decimal(rates.get((row.region, row.instance_type, row.zone), '.5')), int(self.now) - age) for row in variants])

    def west_quota(self, standard_vcpus: int, *, gpu_vcpus: int = 0, age: int = 0) -> None:
        path = self.state / 'spot-prices.json'
        document = cloud_gate.read(path)
        document['regions'] = [{'region': 'us-west-2', 'read_at': int(self.now) - age,
                                'standard_vcpus': str(standard_vcpus), 'gpu_vcpus': str(gpu_vcpus)}]
        cloud_gate.save(path, document)

    def record_running_gate(self, attempt: str, instance_type: str, region: str) -> None:
        self.assertIsInstance(self.book.reserve_gate(self.settings, ledger.GateReservation(
            attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'),
            region=region)), ledger.GateReserved)
        with mock.patch.object(time, 'time', return_value=self.now):
            self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt), instance_type=instance_type)

    def live_price_fixture(self) -> tuple[cloud_gate.GateWorker, list[cloud_gate.GateLaunchVariant]]:
        worker, variants = self.steering_fixture()
        regions = ('us-east-2', 'us-west-2', 'eu-north-1', 'eu-south-2')
        regional = ledger.rows(self.deployment, 'regions')[0]
        self.deployment['regions'] = [{**regional, 'region': region,
            'subnets': [{'id': region + suffix, 'zone': region + suffix} for suffix in ('a', 'b')]}
            for region in regions]
        types = tuple(dict.fromkeys(row.instance_type for row in variants))
        variants = [cloud_gate.GateLaunchVariant(region, size, region + suffix, region + suffix)
                    for region in regions for size in types for suffix in ('a', 'b')]
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone):
            '.1' if row.region == 'us-east-2' else '.5' for row in variants})
        for region in regions:
            self.fake.spot_history[region] = [dict(InstanceType=row.instance_type, AvailabilityZone=row.zone,
                SpotPrice='.05' if region == 'eu-south-2' else '.6', Timestamp='2026-01-15T00:59:00Z')
                for row in variants if row.region == region]
        return worker, variants

    def test_live_prices_change_launch_order_without_replacing_the_hourly_file(self) -> None:
        worker, _ = self.live_price_fixture()
        hourly = (self.state / 'spot-prices.json').read_bytes()
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(ledger.rows(saved, 'variants')[0]['region'], 'eu-south-2')
        self.assertEqual(saved['region'], 'eu-south-2')
        self.assertEqual((self.state / 'spot-prices.json').read_bytes(), hourly)
        reads = ledger.rows(saved, 'spot_price_reads')
        self.assertEqual([row['region'] for row in reads], ['us-east-2', 'us-west-2', 'eu-north-1', 'eu-south-2'])
        self.assertTrue(all(row['used'] and float(str(row['seconds'])) >= 0 for row in reads))
        launch = self.book.launches()[0]
        self.assertEqual(launch['spot_price_reads'], list(reads))
        calls = [args for args in self.fake.commands if 'describe-spot-price-history' in args]
        self.assertEqual(len(calls), 4)
        for args in calls:
            self.assertEqual(args[args.index('--instance-types') + 1:args.index('--product-descriptions')],
                             ['m7a.8xlarge', 'c7a.8xlarge', 'm7a.4xlarge'])
            self.assertEqual(args[args.index('--product-descriptions') + 1], 'Linux/UNIX')

    def test_live_price_timeout_keeps_hourly_rows_and_bounds_the_whole_read(self) -> None:
        worker, variants = self.live_price_fixture()
        hourly = (self.state / 'spot-prices.json').read_bytes()
        release = threading.Event()
        started = threading.Event()
        stopped = threading.Event()
        read_limit_s = .1
        now = 0.0
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_price_live_read_timeout_s=.*$',
                                  f'spot_price_live_read_timeout_s={read_limit_s}', path.read_text()))
        timeouts: list[float] = []
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' in arguments and 'us-east-2' in arguments:
                timeouts.append(float(str(options['timeout'])))
                started.set()
                try:
                    self.assertTrue(release.wait(TEST_HANG_GUARD_SECS))
                    raise subprocess.TimeoutExpired('fixture price read', read_limit_s)
                finally:
                    stopped.set()
            return self.fake(arguments, **options)
        def wait_for_deadline(futures: Iterable[Future[cloud_gate.RegionalLiveSpotRead]], *, timeout: float):
            nonlocal now
            pending = tuple(futures)
            self.assertEqual(len(pending), 4)
            self.assertEqual(timeout, read_limit_s)
            self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
            self.assertFalse(wait(pending[1:], timeout=TEST_HANG_GUARD_SECS).not_done)
            now += timeout
            return wait(pending, timeout=0)
        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        try:
            with (mock.patch.object(cloud_gate, 'time', SimpleNamespace(monotonic=lambda: now)),
                  mock.patch.object(cloud_gate, 'wait', side_effect=wait_for_deadline)):
                reads = worker.live_spot_prices(variants)
            self.assertFalse(stopped.is_set())
            east = next(row for row in reads if row.region == 'us-east-2')
            self.assertFalse(east.used)
            self.assertEqual(east.prices, ())
            prices = [price for reading in reads for price in reading.prices]
            ranked = worker.rank_variants(variants, 'merge-gate', prices)
            # Every type in eu-south-2 has the live price, so all of them come before us-east-2.
            self.assertEqual([row.region for row in ranked[:8]], ['eu-south-2'] * 6 + ['us-east-2'] * 2)
            self.assertEqual((self.state / 'spot-prices.json').read_bytes(), hourly)
        finally:
            release.set()
            self.assertTrue(stopped.wait(TEST_HANG_GUARD_SECS))
        self.assertEqual(len(timeouts), 1)
        self.assertGreater(timeouts[0], 0)
        self.assertLessEqual(timeouts[0], read_limit_s)

    def test_all_live_price_failures_keep_the_hourly_order(self) -> None:
        worker, variants = self.live_price_fixture()
        expected = worker.rank_variants(variants, 'merge-gate')
        calls: list[str] = []
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' in arguments:
                calls.append(arguments[arguments.index('--region') + 1])
                raise AwsUnavailable('An error occurred (AccessDenied): fixture refusal')
            return self.fake(arguments, **options)
        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(ledger.rows(saved, 'variants'), tuple(asdict(row) for row in expected))
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(not row['used'] for row in ledger.rows(saved, 'spot_price_reads')))

    def test_resumed_launch_keeps_live_price_evidence_and_does_not_read_again(self) -> None:
        worker, _ = self.live_price_fixture()
        save = cloud_gate.save
        def stop_before_request(path: Path, row: dict[str, object]) -> None:
            save(path, row)
            if 'arguments' in row:
                raise OSError('The fixture stopped before the launch request')
        with mock.patch.object(cloud_gate, 'save', side_effect=stop_before_request):
            with self.assertRaisesRegex(OSError, 'before the launch request'):
                _ = worker.launch(self.folder, self.request, self.deployment)
        saved = cloud_gate.read(self.folder / 'launch.json')
        with mock.patch.object(worker, 'live_spot_prices', side_effect=AssertionError('The resume must not read prices')):
            self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        current = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(current['variants'], saved['variants'])
        self.assertEqual(current['spot_price_reads'], saved['spot_price_reads'])
        self.assertEqual(self.book.launches()[0]['spot_price_reads'], saved['spot_price_reads'])

    def test_regional_wait_cost_changes_the_order_only_when_enabled(self) -> None:
        worker, variants = self.live_price_fixture()
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone):
            '.49' if row.region == 'eu-south-2' else '.5' for row in variants})
        text_setting(self.root / 'cloud_gate.conf', 'region_extra_gate_seconds', '{"eu-south-2": 60}')
        numeric_settings(self.root / 'cloud_gate.conf', {'gate_wait_usd_per_minute': '.01'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].region, 'us-east-2')
        numeric_settings(self.root / 'cloud_gate.conf', {'gate_wait_usd_per_minute': '0'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].region, 'eu-south-2')

    def test_a_far_region_pays_its_own_transfer(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.48'})
        text_setting(self.root / 'cloud_gate.conf', 'region_transfer_usd', '{}')
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])
        # The 0.02 USD/h saving over the 20 minute lifetime is 0.0067 USD, below the 0.01 USD transfer.
        text_setting(self.root / 'cloud_gate.conf', 'region_transfer_usd', '{"us-west-2": 0.01}')
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].region, 'us-east-2')

    def test_a_region_without_runs_adds_its_unmeasured_seconds(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.48'})
        self.gate_history([('m7a.8xlarge', 'us-east-2', 300)])
        text_setting(self.root / 'cloud_gate.conf', 'unmeasured_region_extra_seconds', '{}')
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])
        # us-west-2 borrows the 300 s run: 0.48 USD/h for 360 s costs more than 0.5 USD/h for 300 s.
        text_setting(self.root / 'cloud_gate.conf', 'unmeasured_region_extra_seconds', '{"us-west-2": 60, "us-east-2": 600}')
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].region, 'us-east-2')

    def test_region_maps_refuse_a_negative_number(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {})
        for key in ('region_transfer_usd', 'unmeasured_region_extra_seconds', 'region_extra_gate_seconds'):
            with self.subTest(key=key):
                text_setting(self.root / 'cloud_gate.conf', key, '{"us-west-2": -1}')
                with self.assertRaisesRegex(ValueError, key):
                    _ = worker.rank_variants(variants, 'merge-gate')
                text_setting(self.root / 'cloud_gate.conf', key, '{}')

    def test_live_price_reads_run_concurrently(self) -> None:
        worker, variants = self.live_price_fixture()
        completed = threading.Event()
        barrier = threading.Barrier(4, action=completed.set, timeout=TEST_HANG_GUARD_SECS)
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-spot-price-history' in arguments:
                _ = barrier.wait()
            return self.fake(arguments, **options)

        def wait_for_reads(futures: Iterable[Future[cloud_gate.RegionalLiveSpotRead]], **_options: float):
            return wait(futures, timeout=TEST_HANG_GUARD_SECS)

        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        with (mock.patch.object(cloud_gate, 'time', SimpleNamespace(monotonic=lambda: 0.0)),
              mock.patch.object(cloud_gate, 'wait', new=wait_for_reads)):
            reads = worker.live_spot_prices(variants)
        self.assertTrue(completed.is_set())
        self.assertEqual(len(reads), 4)
        self.assertTrue(all(row.used for row in reads))

    def test_variant_prices_prefer_the_cheapest_zone_and_preserve_equal_price_order(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[:4], [variants[0], variants[1], variants[6], variants[7]])
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual(ranked[:2], [variants[7], variants[0]])
        self.assertFalse(any('describe-spot-price-history' in command for command in self.fake.commands))

    def gate_history(self, runs: Sequence[tuple[str, str, int]], refusals: Sequence[tuple[str, str, int]] = ()) -> None:
        """Write finished runs (type, region, box seconds) and refused requests before each launch."""
        rows: list[dict[str, object]] = []
        for index, (size, region, box_s) in enumerate(runs):
            row: dict[str, object] = {'attempt': f'run-{index}', 'instance_type': size, 'region': region, 'mode': 'merge-gate'}
            rows.extend(({**row, 'time': self.now - 7200 - box_s, 'result': 'launched'},
                         {**row, 'time': self.now - 7200, 'result': 'finished', 'gate_s': box_s - 60}))
        for index, (size, region, count) in enumerate(refusals):
            for step in range(count):
                rows.append({'attempt': f'refused-{index}', 'instance_type': size, 'region': region, 'mode': 'merge-gate',
                             'time': self.now - 9000 + step * 6, 'result': 'refused', 'error_code': 'InsufficientInstanceCapacity'})
            rows.append({'attempt': f'refused-{index}', 'instance_type': size, 'region': region, 'mode': 'merge-gate',
                         'time': self.now - 9000 + count * 6, 'result': 'launched'})
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_a_type_and_region_above_max_gate_minutes_is_not_tried(self) -> None:
        worker, variants = self.steering_fixture()
        first, second, third = conf.read(self.root / 'cloud_gate.conf').words('instance_types')
        cap = conf.read(self.root / 'cloud_gate.conf').whole('max_gate_minutes') * 60
        # The third type is the cheapest per run, but its median gate time is one minute above the cap.
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone): '.1' if row.instance_type == third else '.5'
                                       for row in variants})
        self.gate_history([(first, 'us-east-2', cap - 60), (third, 'us-east-2', cap + 60)])
        with redirect_stdout(StringIO()) as output:
            ranked = worker.rank_variants(variants, 'merge-gate')
        # us-west-2 has no runs of the third type, so it uses the type's time in us-east-2 and is skipped too.
        self.assertEqual({row.instance_type for row in ranked}, {first, second})
        self.assertEqual(ranked[0].instance_type, first)
        self.assertEqual(output.getvalue().count(f'Cloud gate skips {third} in '), 2)
        # A median at the cap is still tried; a type without runs is never skipped.
        self.gate_history([(first, 'us-east-2', cap), (third, 'us-east-2', cap)])
        with redirect_stdout(StringIO()):
            self.assertEqual(len(worker.rank_variants(variants, 'merge-gate')), len(variants))

    def test_a_region_whose_spot_quota_is_below_one_box_stays_in_the_order(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        self.west_quota(31)
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertCountEqual(ranked, variants)
        self.assertCountEqual(ranked[-4:], variants[6:10])
        # One box fits at equality. An expired snapshot does not set the order.
        for west, age in ((32, 0), (0, conf.read(self.settings).whole('price_max_age_seconds') + 1)):
            with self.subTest(west=west, age=age):
                self.west_quota(west, age=age)
                self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])

    def test_every_type_above_max_gate_minutes_falls_back_before_launch(self) -> None:
        cap = conf.read(self.root / 'cloud_gate.conf').whole('max_gate_minutes') * 60
        self.gate_history([(size, ledger.text(region, 'region'), cap + 60)
                           for size in conf.read(self.root / 'cloud_gate.conf').words('instance_types')
                           for region in ledger.rows(self.deployment, 'regions')])
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIn('Cloud gate skips ', output.getvalue())
        self.assertEqual(self.fake.launches, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_lowest_expected_cost_per_run_comes_before_hourly_price_and_type_order(self) -> None:
        worker, variants = self.steering_fixture()
        first, _second, third = conf.read(self.root / 'cloud_gate.conf').words('instance_types')
        # The third type costs less per hour but runs twice as long, so it costs more per run.
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone): '.3' if row.instance_type == third else '.5'
                                       for row in variants})
        self.gate_history([(first, 'us-east-2', 300), (third, 'us-east-2', 600)])
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual([row.instance_type for row in ranked[:2]], [first, first])
        # The region uses its own box time; us-west-2 has none, so it uses us-east-2's time.
        self.assertEqual({row.region for row in ranked[:4]}, {'us-east-2', 'us-west-2'})
        self.gate_history([(first, 'us-east-2', 900), (third, 'us-east-2', 600)])
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0].instance_type, third)

    def test_expected_refusals_count_only_when_wait_time_has_a_cost(self) -> None:
        worker, variants = self.steering_fixture()
        first, second, _third = conf.read(self.root / 'cloud_gate.conf').words('instance_types')
        self.variant_prices(variants, {(row.region, row.instance_type, row.zone): '.5' if row.instance_type == first else '.51'
                                       for row in variants})
        self.gate_history([(first, 'us-east-2', 600), (second, 'us-east-2', 600)], [(first, 'us-east-2', 100)])
        def home_types() -> list[str]:
            return [row.instance_type for row in worker.rank_variants(variants, 'merge-gate') if row.region == 'us-east-2']
        self.assertEqual(home_types()[:2], [first, first])
        # 50 refusals per launch, 6 s apart, at .01 USD a minute is 0.05 USD: more than the price gap.
        numeric_settings(self.root / 'cloud_gate.conf', {'gate_wait_usd_per_minute': '.01'})
        self.assertEqual(home_types()[:2], [second, second])
        # A region without refusals keeps the type first.
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], cloud_gate.GateLaunchVariant(
            'us-west-2', first, 'us-west-2a', 'us-west-2a'))

    def test_equal_cost_per_run_keeps_the_configured_type_order(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {})
        ranked = worker.rank_variants(variants, 'merge-gate')
        types = conf.read(self.root / 'cloud_gate.conf').words('instance_types')
        self.assertEqual([row.instance_type for row in ranked if row.region == 'us-east-2'], [size for size in types for _ in range(2)])

    def test_absent_stale_or_invalid_prices_preserve_configured_order(self) -> None:
        worker, variants = self.steering_fixture()
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), variants)
        self.variant_prices(variants, {}, age=7201)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), variants)
        _ = (self.state / 'spot-prices.json').write_text('{')
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), variants)

    def test_missing_prices_follow_priced_variants_of_the_same_size(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices([variants[6]], {})
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual(ranked[0], variants[6])
        self.assertEqual(ranked[1:4], [variants[0], variants[1], variants[7]])
        self.assertEqual(ranked[-4:], [variants[4], variants[5], variants[10], variants[11]])

    def test_zone_capacity_cooldown_ends_after_the_configured_window(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1',
                                      ('us-west-2', 'm7a.8xlarge', 'us-west-2a'): '.2'})
        for code in ('InsufficientInstanceCapacity', 'InsufficientSpotCapacity', 'SpotMaxPriceTooLow'):
            with self.subTest(code=code):
                _ = (self.state / 'cloud-gate-launches.jsonl').write_text(json.dumps(dict(
                    time=self.now - 600, result='refused', region='us-west-2', instance_type='m7a.8xlarge',
                    availability_zone='us-west-2b', error_code=code)) + '\n')
                ranked = worker.rank_variants(variants, 'merge-gate')
                self.assertEqual(ranked[0], variants[6])
                self.assertEqual(ranked[-1], variants[7])
        self.advance(1260)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])

    def test_quota_refusal_does_not_cool_a_region_after_a_box_ends(self) -> None:
        worker, variants = self.steering_fixture()
        self.record_running_gate('ended', 'm7a.8xlarge', 'us-west-2')
        self.advance(600)
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        self.west_quota(32)
        self.book.resolve_gate('ended', ledger.GateTerminated(int(self.now), 'i-ended'))
        for code in ('MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded'):
            with self.subTest(code=code):
                _ = (self.state / 'cloud-gate-launches.jsonl').write_text(json.dumps(dict(
                    time=self.now - 300, result='refused', region='us-west-2', instance_type='m7a.8xlarge',
                    availability_zone='us-west-2b', error_code=code)) + '\n')
                ranked = worker.rank_variants(variants, 'merge-gate')
                self.assertEqual(ranked[0], variants[7])
                self.assertCountEqual(ranked, variants)
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])

    def test_live_gate_vcpus_rank_a_full_region_after_regions_with_room(self) -> None:
        worker, variants = self.steering_fixture()
        east, full, fits = variants[0], variants[7], variants[10]
        gpu = replace(full, instance_type='g6.xlarge')
        variants = [full, east, fits, gpu]
        self.record_running_gate('standard', 'm7a.4xlarge', 'us-west-2')
        self.record_running_gate('gpu', 'g6.2xlarge', 'us-west-2')
        self.record_running_gate('east', 'm7a.8xlarge', 'us-east-2')
        for priced in (True, False):
            with self.subTest(priced=priced):
                self.variant_prices(variants if priced else [], {
                    (full.region, full.instance_type, full.zone): '.1',
                    (fits.region, fits.instance_type, fits.zone): '.2',
                    (gpu.region, gpu.instance_type, gpu.zone): '.3'})
                self.west_quota(32, gpu_vcpus=12)
                ranked = worker.rank_variants(variants, 'merge-gate')
                self.assertEqual(ranked[-1], full)
                self.assertCountEqual(ranked[:-1], [east, fits, gpu])
        self.advance(1200)
        self.variant_prices(variants, {(full.region, full.instance_type, full.zone): '.1'})
        self.west_quota(32, gpu_vcpus=12)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], full)

    def test_full_gpu_quota_preserves_the_standard_variant_with_room(self) -> None:
        worker, variants = self.steering_fixture()
        east, standard = variants[0], variants[7]
        gpu = replace(standard, instance_type='g6.xlarge')
        graphics_trial = 'g6.8xlarge'
        self.assertNotIn(graphics_trial, cloud_gate.settings().words('gpu_instance_types'))
        self.record_running_gate('graphics', graphics_trial, 'us-west-2')
        self.variant_prices([east, standard, gpu], {
            (standard.region, standard.instance_type, standard.zone): '.2',
            (gpu.region, gpu.instance_type, gpu.zone): '.1'})
        self.west_quota(32, gpu_vcpus=32)
        self.assertEqual(worker.rank_variants([east, standard, gpu], 'merge-gate'), [standard, east, gpu])

    def test_launch_quota_refusal_marks_the_class_until_live_use_drops(self) -> None:
        worker, variants = self.steering_fixture()
        numeric_settings(self.root / 'cloud_gate.conf', {'capacity_cooldown_seconds': '30'})
        west = variants[7]
        self.record_running_gate('other', west.instance_type, west.region)
        self.variant_prices(variants, {(west.region, west.instance_type, west.zone): '.1'})
        self.west_quota(256)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], west)
        self.fake.refusals = ['MaxSpotInstanceCountExceeded']
        with mock.patch.object(time, 'time', return_value=self.now):
            self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        launches = [command for command in self.fake.commands if 'run-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in launches], ['us-west-2', 'us-east-2'])
        pool = ledger.SpotQuotaPool(west.region, ledger.SpotQuotaClass.STANDARD)
        self.assertEqual(self.book.read_snapshot().gate_quota_refusals, [ledger.GateQuotaRefusal(pool, 32, int(self.now))])
        ranked = worker.rank_variants(variants, 'merge-gate')
        self.assertEqual(ranked[:6], variants[:6])
        self.assertCountEqual(ranked[6:], variants[6:])
        self.book.resolve_gate('other', ledger.GateTerminated(int(self.now) + 1, 'i-other'))
        self.advance(1)
        self.assertEqual(self.book.read_snapshot().gate_quota_refusals, [])
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], west)
        self.record_running_gate('replacement', west.instance_type, west.region)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], west)

    def test_switch_off_after_capacity_refusal_prevents_the_next_variant(self) -> None:
        worker, _ = self.steering_fixture()
        self.fake.refusals = ['InsufficientInstanceCapacity']

        def turn_off_after_launch(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            reply = self.fake(arguments, **options)
            if 'run-instances' in arguments:
                override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=off')
            return reply

        with mock.patch.object(worker.api, 'execute', side_effect=turn_off_after_launch):
            outcome = worker.launch(self.folder, self.request, self.deployment)
        self.assertEqual(outcome, cloud_gate.LocalFallback(cloud_gate.CLOUD_GATE_OFF))
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[-1].state, ledger.GateAbsent)

    def test_switch_off_leaves_an_uncertain_launch_for_cleanup(self) -> None:
        worker, _ = self.steering_fixture()
        self.fake.launch_timeouts = 1
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = worker.launch(self.folder, self.request, self.deployment)
        override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=off')
        self.fake.describe_errors = [AwsUnavailable(
            'An error occurred (UnauthorizedOperation) when calling the DescribeInstances operation')]
        calls = len(self.fake.commands)
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment),
                         cloud_gate.LocalFallback(cloud_gate.CLOUD_GATE_OFF))
        self.assertEqual(self.fake.commands[calls:], [])
        self.assertEqual(len(self.fake.launches), 1)

        self.assertFalse(cloud_gate.cleanup_folder(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[-1].state, ledger.GateLaunchPending)
        self.assertFalse(self.fake.terminated)
        self.assertTrue(cloud_gate.cleanup_folder(self.folder, worker))
        self.assertEqual(self.book.read_snapshot().gate_reservations[-1].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        lookups = [command for command in self.fake.commands[calls:] if '--filters' in command]
        self.assertEqual(len(lookups), 2)
        token = ledger.text(self.fake.launches[0], 'ClientToken')
        self.assertTrue(all('Name=client-token,Values=' + token in command for command in lookups))

    def test_quota_refusal_at_zero_live_vcpus_expires_at_the_cooldown(self) -> None:
        worker, variants = self.steering_fixture()
        numeric_settings(self.root / 'cloud_gate.conf', {'capacity_cooldown_seconds': '30'})
        east, west = variants[0], variants[7]
        variants = [east, west]
        self.variant_prices(variants, {(west.region, west.instance_type, west.zone): '.1'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), [west, east])
        pool = ledger.SpotQuotaPool(west.region, ledger.SpotQuotaClass.STANDARD)
        self.book.record_gate_quota_refusal(pool, self.now)
        self.assertEqual(self.book.read_snapshot().gate_quota_refusals, [ledger.GateQuotaRefusal(pool, 0, int(self.now))])
        self.advance(29)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), [east, west])
        self.advance(1)
        self.assertEqual(worker.rank_variants(variants, 'merge-gate'), [west, east])
        self.book.resolve_gate('attempt', ledger.GateAbsent(int(self.now)))
        self.assertEqual(self.book.read_snapshot().gate_quota_refusals, [])

    def test_saved_variant_order_survives_capacity_refusal_and_price_changes(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        self.fake.refusals = ['InsufficientInstanceCapacity']
        save = cloud_gate.save
        def stop_after_refusal(path: Path, row: dict[str, object]) -> None:
            save(path, row)
            if row.get('variant') == 1 and 'arguments' not in row:
                raise OSError('fixture stop after refusal')
        with mock.patch.object(cloud_gate, 'save', side_effect=stop_after_refusal):
            with self.assertRaisesRegex(OSError, 'fixture stop after refusal'):
                _ = worker.launch(self.folder, self.request, self.deployment)
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(saved['variant'], 1)
        self.assertEqual(len(ledger.rows(saved, 'variants')), len(variants))
        self.variant_prices(variants, {('us-west-2', 'c7a.8xlarge', 'us-west-2a'): '.001'})
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', 'm7a.4xlarge')
        with mock.patch.object(worker, 'rank_variants', side_effect=AssertionError('The order must remain saved')):
            self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        self.assertEqual(self.fake.launches[-1]['ClientToken'], 'attempt-1')
        self.assertEqual(self.fake.launches[-1]['InstanceType'], 'm7a.8xlarge')
        self.assertEqual(self.fake.launches[-1]['NetworkInterfaces'], [{'DeviceIndex': 0, 'SubnetId': 'us-east-2a',
            'Groups': ['fixture-group'], 'AssociatePublicIpAddress': True}])
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json')['variants'], saved['variants'])

    def test_old_variant_offsets_keep_configured_order_despite_new_prices(self) -> None:
        worker, variants = self.steering_fixture()
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.1'})
        cloud_gate.save(self.folder / 'launch.json', {'variant': 1})
        self.assertEqual(worker.launch(self.folder, self.request, self.deployment), 'i-fixture')
        self.assertEqual(self.fake.launches[-1]['ClientToken'], 'attempt-1')
        self.assertEqual(self.fake.launches[-1]['NetworkInterfaces'], [{'DeviceIndex': 0, 'SubnetId': 'us-east-2b',
            'Groups': ['fixture-group'], 'AssociatePublicIpAddress': True}])
        self.assertNotIn('variants', cloud_gate.read(self.folder / 'launch.json'))

    def test_remote_transfer_uses_the_recent_mode_median_or_the_lifetime(self) -> None:
        worker, variants = self.steering_fixture()
        numeric_settings(self.root / 'cloud_gate.conf', {'remote_region_transfer_usd': '.005'})
        self.variant_prices(variants, {('us-west-2', 'm7a.8xlarge', 'us-west-2b'): '.48'})
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[7])
        # Box times of another type give the mode's median when the ranked types have no runs.
        self.gate_history([('other.8xlarge', 'us-east-2', duration) for duration in (300, 600, 900)])
        self.assertEqual(worker.rank_variants(variants, 'merge-gate')[0], variants[0])

    def test_regions_try_home_types_and_zones_before_west_and_keep_home_transfers(self) -> None:
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region, 'image': 'ami-' + suffix,
            'template': 'template-' + suffix} for region, suffix in (('us-east-2', 'home'), ('us-west-2', 'west'))]
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 4
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        reply = worker.execute(self.folder, self.request, self.deployment)
        self.assertEqual(reply['status'], 'complete')
        launches = [command for command in self.fake.commands if 'run-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in launches], ['us-east-2'] * 4 + ['us-west-2'])
        self.assertEqual([row['InstanceType'] for row in self.fake.launches],
                         ['m7a.8xlarge'] * 2 + ['m7a.4xlarge'] * 2 + ['m7a.8xlarge'])
        later = [command for command in self.fake.commands if 'terminate-instances' in command or 'describe-instances' in command]
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-west-2' for command in later))
        transfers = [command for command in self.fake.commands if 's3api' in command]
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-east-2' for command in transfers))
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].region, 'us-west-2')
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json')['region'], 'us-west-2')
        self.assertTrue(all(row['region'] in ('us-east-2', 'us-west-2') for row in self.book.launches()))

    def test_saved_west_launch_replays_its_region_and_token_after_type_change(self) -> None:
        regional = ledger.rows(self.deployment, 'regions')[0]
        self.deployment['regions'] = [{**regional, 'region': region} for region in ('us-east-2', 'us-west-2')]
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 4
        interrupted = False
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            nonlocal interrupted
            reply = self.fake(arguments, **options)
            if 'run-instances' in arguments and arguments[arguments.index('--region') + 1] == 'us-west-2' and not interrupted:
                interrupted = True
                raise subprocess.TimeoutExpired('aws run-instances', 1)
            return reply
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=transport), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'fallback')
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual(saved['region'], 'us-west-2')
        self.assertNotIn('instance', saved)
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^instance_types=.*$', 'instance_types=m7a.4xlarge', path.read_text()))
        self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'complete')
        calls = [args for args in self.fake.commands if 'run-instances' in args]
        self.assertEqual(calls[-1][calls[-1].index('--region') + 1], 'us-west-2')
        self.assertEqual(self.fake.launches[-1], saved['arguments'])
        self.assertEqual(self.fake.launches[-1]['ClientToken'], self.fake.launches[-2]['ClientToken'])
        self.assertEqual(self.fake.launches[-1]['InstanceType'], 'm7a.8xlarge')

    def test_regional_reservations_share_one_daily_cap(self) -> None:
        numeric_settings(self.settings, {'daily_limit_usd': '3.5'})
        first = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'east', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'), region='us-east-2'))
        second = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'west', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02'), region='us-west-2'))
        self.assertIsInstance(first, ledger.GateReserved)
        self.assertIsInstance(second, ledger.GateCapReached)
        self.assertEqual([row.region for row in self.book.read_snapshot().gate_reservations], ['us-east-2'])

    def test_all_regions_refuse_before_local_fallback(self) -> None:
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region} for region in ('us-east-2', 'us-west-2')]
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 8
        reply = self.execute()
        self.assertEqual(reply['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 8)
        self.assertFalse(self.fake.terminated)

    def test_unsupported_type_skips_to_next_variant(self) -> None:
        self.fake.refusals = ['An error occurred (Unsupported): instance type is not supported'] * 2
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(self.fake.launches[-1]['InstanceType'], 'm7a.4xlarge')

    def test_type_outside_the_deployed_stack_is_skipped(self) -> None:
        self.deployment['instance_types'] = ['m7a.4xlarge']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual({launch['InstanceType'] for launch in self.fake.launches}, {'m7a.4xlarge'})

    def test_unauthorized_type_is_skipped_for_the_next_type(self) -> None:
        self.fake.refusals = ['An error occurred (UnauthorizedOperation) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual([launch['InstanceType'] for launch in self.fake.launches], ['m7a.8xlarge', 'm7a.4xlarge'])
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json')['unauthorized_types'], ['m7a.8xlarge'])

    def test_unauthorized_last_type_falls_back(self) -> None:
        self.fake.refusals = ['An error occurred (UnauthorizedOperation) when calling the RunInstances operation: fixture'] * 2
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual([launch['InstanceType'] for launch in self.fake.launches], ['m7a.8xlarge', 'm7a.4xlarge'])

    def test_stack_without_a_configured_type_falls_back_before_launch(self) -> None:
        self.deployment['instance_types'] = ['c7a.4xlarge']
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_expired_pending_sweep_searches_both_regions(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^(regions|spot_candidate_regions)=.*$',
                                  r'\1=us-east-2 us-west-2', path.read_text()))
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'lost', int(self.now), int(self.now) + 1, Decimal('.8'), Decimal('.02')))
        self.advance(2)
        self.fake.has_instance = False
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        calls = [command for command in self.fake.commands if 'describe-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in calls], ['us-east-2', 'us-west-2'])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_complete_result_keeps_log_and_confirms_termination(self) -> None:
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        self.assertEqual((self.folder / 'gate.log').read_text(), 'fixture cloud output\n')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)
        self.assertTrue(self.fake.terminated)

    def test_box_end_uses_result_receipt_only_with_a_valid_final_result(self) -> None:
        for result_state in ('valid', 'absent', 'invalid'):
            with self.subTest(result_state=result_state):
                attempt = 'end-' + result_state
                folder = self.root / attempt
                folder.mkdir()
                request = {**self.request, 'attempt': attempt, 'source_key': ''}
                cloud_gate.save(folder / 'request.json', request)
                self.fake = FakeGateAws()
                self.fake.result = {**self.result(), 'attempt': attempt}
                ledger.mapping(self.fake.result['record'])['id'] = 'gate-' + attempt
                self.fake.has_result = result_state != 'absent'
                if result_state == 'invalid':
                    self.fake.result['tree'] = 'other-tree'
                self.fake.state = 'shutting-down'
                received_at = int(self.now)

                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if 'terminate-instances' in arguments:
                        self.advance(16)
                    return self.fake(arguments, **options)

                worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
                    self.book, self.settings, folder / 'reservation', clock=lambda: self.now, pause=self.advance)
                outcome = worker.execute(folder, request, self.deployment)
                self.assertEqual(outcome['status'], 'complete' if result_state == 'valid' else 'fallback')
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt)
                self.assertEqual(reservation.state, ledger.GateTerminated(
                    received_at if result_state == 'valid' else received_at + 16, 'i-fixture'))
                self.assertTrue(self.fake.terminated)

    def test_saved_image_changes_only_the_launch_image(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        official = self.fake.launches[-1]
        ledger.rows(self.deployment, 'regions')[0]['image'] = 'ami-saved-gate'
        self.folder = self.root / 'saved-image-attempt'
        self.folder.mkdir()
        self.request = {**self.request, 'attempt': self.folder.name, 'source_key': ''}
        self.fake.result = {**self.result(), 'attempt': self.folder.name}
        ledger.mapping(self.fake.result['record'])['id'] = 'gate-' + self.folder.name
        self.assertEqual(self.execute()['status'], 'complete')
        saved = self.fake.launches[-1]
        self.assertEqual(saved['ImageId'], 'ami-saved-gate')
        self.assertEqual({key: value for key, value in saved.items() if key not in ('ImageId', 'ClientToken', 'UserData', 'TagSpecifications')},
                         {key: value for key, value in official.items() if key not in ('ImageId', 'ClientToken', 'UserData', 'TagSpecifications')})

    def test_poll_read_timeout_retries_and_completes(self) -> None:
        self.fake.read_timeouts = ['list-objects-v2', 'get-object']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(sum('list-objects-v2' in command for command in self.fake.commands), 3)
        reads = [command[command.index('--key') + 1] for command in self.fake.commands if 'get-object' in command]
        self.assertEqual(reads, ['artifacts/attempt/' + name
                                for name in ('result.json', 'result.json', 'gate.log')])
        self.assertEqual(len(self.fake.launches), 1)
        self.assertTrue(self.fake.terminated)

    def outage_worker(self, outage: Callable[[list[str]], bool]) -> cloud_gate.GateWorker:
        """A worker whose AWS calls fail with a connect error while outage(arguments) is true."""
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if outage(list(arguments)):
                self.fake.commands.append(list(arguments))
                region = arguments[arguments.index('--region') + 1]
                return subprocess.CompletedProcess(list(arguments), 255, '',
                    f'Could not connect to the endpoint URL: "https://ec2.{region}.amazonaws.com/"')
            return self.fake(arguments, **options)
        return cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)

    def test_short_poll_outage_keeps_the_box_and_completes(self) -> None:
        self.fake.has_result = False
        failed = 0
        def outage(arguments: list[str]) -> bool:
            nonlocal failed
            if 'list-objects-v2' not in arguments or failed == 3:
                return False
            failed += 1
            self.fake.has_result = failed == 3
            return True
        with redirect_stdout(StringIO()) as output:
            outcome = self.outage_worker(outage).execute(self.folder, self.request, self.deployment)
        self.assertEqual(outcome['status'], 'complete')
        self.assertEqual(output.getvalue().count('Cloud gate poll skipped:'), 3)
        self.assertEqual(len(self.fake.launches), 1)
        self.assertTrue(self.fake.terminated)

    def test_long_poll_outage_ends_the_cloud_try_after_observe_outage_s(self) -> None:
        for operation, interval in (('describe-instances', 30), ('list-objects-v2', 2)):
            with self.subTest(operation=operation):
                self.fake = FakeGateAws()
                self.fake.has_result = False
                self.folder = self.root / operation
                self.folder.mkdir()
                request = {**self.request, 'attempt': operation, 'source_key': ''}
                cloud_gate.save(self.folder / 'request.json', request)
                started = self.now
                failed_checks: list[float] = []
                instance_checks: list[float] = []

                def outage(arguments: list[str]) -> bool:
                    if self.fake.terminated:
                        return False
                    if 'describe-instances' in arguments:
                        instance_checks.append(self.now - started)
                    if operation not in arguments:
                        return False
                    failed_checks.append(self.now - started)
                    return True

                with redirect_stdout(StringIO()), mock.patch.object(time, 'monotonic', side_effect=lambda: self.now):
                    outcome = self.outage_worker(outage).execute(self.folder, request, self.deployment)
                self.assertEqual(failed_checks, list(range(0, 181, interval)))
                self.assertEqual(instance_checks, list(range(0, 181, 30)))
                self.assertEqual(outcome['status'], 'fallback')
                self.assertIn('Could not connect to the endpoint', str(outcome['reason']))
                self.assertGreaterEqual(self.now - started, cloud_gate.settings().number('observe_outage_s'))
                self.assertTrue(self.fake.terminated)
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == operation)
                self.assertIsInstance(reservation.state, ledger.GateTerminated)

    def test_termination_repeats_through_an_outage(self) -> None:
        failed = False
        def outage(arguments: list[str]) -> bool:
            nonlocal failed
            if 'terminate-instances' in arguments and not failed:
                failed = True
                return True
            return False
        with redirect_stdout(StringIO()) as output:
            outcome = self.outage_worker(outage).execute(self.folder, self.request, self.deployment)
        self.assertEqual(outcome['status'], 'complete')
        self.assertIn('Cloud gate termination waits on fixture-region:', output.getvalue())
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)

    def test_unreachable_region_moves_the_launch_to_the_next_region(self) -> None:
        self.deployment['regions'] = [{**ledger.rows(self.deployment, 'regions')[0], 'region': region}
                                      for region in ('us-east-2', 'us-west-2')]
        def outage(arguments: list[str]) -> bool:
            return 'run-instances' in arguments and arguments[arguments.index('--region') + 1] == 'us-east-2'
        with redirect_stdout(StringIO()) as output:
            outcome = self.outage_worker(outage).execute(self.folder, self.request, self.deployment)
        self.assertEqual(outcome['status'], 'complete')
        launches = [command[command.index('--region') + 1] for command in self.fake.commands if 'run-instances' in command]
        self.assertEqual(launches, ['us-east-2', 'us-east-2', 'us-west-2'])
        self.assertIn('Cloud gate skips us-east-2:', output.getvalue())
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].region, 'us-west-2')

    def test_unreachable_last_region_still_falls_back(self) -> None:
        def outage(arguments: list[str]) -> bool:
            return 'run-instances' in arguments
        with redirect_stdout(StringIO()):
            outcome = self.outage_worker(outage).execute(self.folder, self.request, self.deployment)
        self.assertEqual(outcome['status'], 'fallback')
        self.assertFalse(self.fake.terminated)
        self.assertEqual(sum('run-instances' in command for command in self.fake.commands),
                         cloud_gate.settings().whole('launch_reply_tries'))

    def test_worker_kills_recover_through_cleanup_or_the_next_start(self) -> None:
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'other-live', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
        identity = process_start(os.getpid())
        self.assertIsInstance(identity, Started)
        assert isinstance(identity, Started)
        self.book.gate_owner('other-live', ledger.GateWorkerOwner(os.getpid(), identity.start, str(self.root / 'other-live')))
        self.book.resolve_gate('other-live', ledger.GateRunning('i-other-live'))
        for number, during_launch in ((signal.SIGTERM, -1), (signal.SIGTERM, False), (signal.SIGTERM, True),
                                      (signal.SIGKILL, False), (signal.SIGKILL, True)):
            with self.subTest(number=number, during_launch=during_launch):
                folder = self.root / f'killed-{number}-{during_launch}'
                folder.mkdir()
                attempt = folder.name
                request = {**self.request, 'attempt': attempt}
                cloud_gate.save(folder / 'request.json', request)
                cloud_gate.save(folder / 'deployment.json', self.deployment)
                script = r'''import json, signal, sys
from pathlib import Path
from unittest import mock
from scripts.aws_target import cloud_gate, ledger
from scripts.aws_target.aws_target import AwsCli
from scripts.aws_target.test_cloud_gate import FakeGateAws
folder, state, configuration = map(Path, sys.argv[1:4])
at, number, during_launch = float(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
fake = FakeGateAws()
fake.has_result = False
handlers = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
def execute(arguments, **options):
    assert handlers == {number: signal.getsignal(number) for number in handlers}
    reply = fake(arguments, **options)
    raise_now = 'put-object' in arguments if during_launch < 0 else 'run-instances' in arguments if during_launch else 'list-objects-v2' in arguments
    if raise_now:
        signal.raise_signal(number)
    return reply
api = AwsCli('build-scaler', 'fixture-region', 1, execute=execute)
worker = cloud_gate.GateWorker(api, ledger.CostLedger(state), configuration, folder / 'reservation.cgroup', clock=lambda: at)
with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
      mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
      mock.patch.object(signal, 'signal', side_effect=AssertionError('handler')),
      mock.patch.object(sys, 'argv', ['cloud-gate', '--worker', str(folder), '--directory', str(state),
                                   '--sidecar', str(folder / 'reservation.cgroup'), '--settings', str(configuration)])):
    raise SystemExit(cloud_gate.main())
'''
                reply = subprocess.run([sys.executable, '-c', script, str(folder), str(self.state), str(self.settings),
                    str(self.now), str(number), str(int(during_launch))], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(reply.returncode, -number, reply.stderr)
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt)
                self.assertIsInstance(reservation.owner, ledger.GateWorkerOwner)
                self.assertIsInstance(reservation.state, ledger.GateLaunchPending if during_launch else ledger.GateRunning)
                fake = FakeGateAws()
                api = AwsCli('fixture', 'fixture-region', 1, execute=fake)
                worker = cloud_gate.GateWorker(api, self.book, self.settings, folder / 'reservation',
                    clock=lambda: self.now, pause=self.advance)
                command = ['cloud-gate', '--cleanup', str(folder), '--directory', str(self.state),
                           '--sidecar', str(folder / 'reservation'), '--settings', str(self.settings)]
                with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
                      mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
                      mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
                    if number == signal.SIGKILL:
                        outcome = worker.execute(folder, request, self.deployment)
                        self.assertEqual(outcome['status'], 'fallback')
                    else:
                        with mock.patch.object(sys, 'argv', command):
                            self.assertEqual(cloud_gate.main(), 0)
                    before = len(fake.commands)
                    with mock.patch.object(sys, 'argv', command):
                        self.assertEqual(cloud_gate.main(), 0)
                    self.assertEqual(len(fake.commands), before)
                reservation = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == attempt)
                self.assertIsInstance(reservation.state, ledger.GateAbsent if during_launch == -1 else ledger.GateTerminated)
                self.assertEqual(fake.terminated, during_launch != -1)
                self.assertFalse((folder / 'outcome.json').exists())
                if during_launch == -1:
                    self.assertFalse((folder / 'launch.json').exists())
                    before_restart = len(fake.commands)
                    self.assertEqual(worker.execute(folder, request, self.deployment),
                        {'status': 'fallback', 'reason': 'The saved attempt has ended'})
                    self.assertEqual(len(fake.commands), before_restart)
                other = next(row for row in self.book.read_snapshot().gate_reservations if row.attempt == 'other-live')
                self.assertEqual(other.state, ledger.GateRunning('i-other-live'))
                self.assertNotIn('i-other-live', fake.terminated_instances)

    def test_reconcile_preserves_live_and_unreadable_worker_owners(self) -> None:
        worker = self.worker_with_pending_gate()
        owner = ledger.GateWorkerOwner(os.getpid(), 'fixture-start', str(self.folder))
        self.book.gate_owner('orphan', owner)
        self.advance(1201)
        for identity in (Started(owner.start), Unreadable('fixture outage')):
            with mock.patch.object(cloud_gate, 'process_start', return_value=identity):
                worker.reconcile()
        self.assertEqual(self.fake.commands, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)

    def test_reconcile_closes_the_missing_box_in_a_batch(self) -> None:
        worker, _ = self.steering_fixture()
        timeout = cloud_gate.settings().number('termination_timeout_s')
        pause: Callable[[float], None] = lambda _seconds: self.advance(timeout)
        worker.pause = pause
        self.record_running_gate('missing', 'm7a.8xlarge', 'us-west-2')
        self.record_running_gate('present', 'm7a.8xlarge', 'us-west-2')
        self.advance(1200)
        self.fake.confirm_termination = False

        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-instances' in arguments and 'i-missing' in arguments:
                raise AwsUnavailable('An error occurred (InvalidInstanceID.NotFound) when calling the DescribeInstances operation')
            return self.fake(arguments, **options)

        worker.apis.clear()
        worker.api = AwsCli('fixture', 'us-east-2', 1, execute=transport)
        worker.reconcile()
        by_attempt = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertIsInstance(by_attempt['missing'], ledger.GateTerminated)
        self.assertEqual(by_attempt['present'], ledger.GateRunning('i-present'))
        self.assertIn('i-present', self.fake.terminated_instances)

    def test_trial_sweep_preserves_an_expired_trial_with_a_live_worker(self) -> None:
        worker = self.worker_with_pending_gate()
        current = self.root / 'trials' / 'current'
        previous = current.parent / 'previous' / 'orphan'
        current.mkdir(parents=True)
        previous.mkdir(parents=True)
        cloud_gate.save(previous / 'request.json', {'attempt': 'orphan', 'expires': int(self.now) + 1200})
        cloud_gate.save(previous / 'launch.json', {'instance': 'i-live'})
        self.book.gate_owner('orphan', ledger.GateWorkerOwner(os.getpid(), 'fixture-start', str(previous)))
        self.advance(1201)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Started('fixture-start')):
            aws_trial.sweep_trial_resources(current, worker)
        self.assertEqual(self.fake.commands, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)

    def test_trial_sweep_terminates_only_the_dead_owner_in_an_expired_trial(self) -> None:
        worker = self.worker_with_pending_gate()
        self.assertIsInstance(self.book.reserve_gate(self.settings, ledger.GateReservation(
            'live', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal(0))), ledger.GateReserved)
        current = self.root / 'trials' / 'current'
        current.mkdir(parents=True)
        for attempt, pid, instance in (('orphan', 9001, 'i-dead'), ('live', 9002, 'i-live')):
            folder = current.parent / 'previous' / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {'attempt': attempt, 'expires': int(self.now) + 1200})
            cloud_gate.save(folder / 'launch.json', {'instance': instance})
            self.book.gate_owner(attempt, ledger.GateWorkerOwner(pid, 'fixture-start', str(folder)))
            self.book.resolve_gate(attempt, ledger.GateRunning(instance))
        self.advance(1201)
        def owner_state(pid: int) -> Gone | Started:
            return Gone() if pid == 9001 else Started('fixture-start')
        with mock.patch.object(cloud_gate, 'process_start', side_effect=owner_state):
            aws_trial.sweep_trial_resources(current, worker)
        self.assertEqual(self.fake.terminated_instances, {'i-dead'})
        states = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertEqual(states['orphan'], ledger.GateTerminated(int(self.now), 'i-dead'))
        self.assertEqual(states['live'], ledger.GateRunning('i-live'))

    def test_cleanup_failure_retains_booking_and_retry_closes_it(self) -> None:
        worker = self.worker_with_pending_gate()
        cloud_gate.save(self.folder / 'request.json', {'attempt': 'orphan', 'expires': int(self.now) + 1200})
        cloud_gate.save(self.folder / 'launch.json', {'instance': 'i-fixture'})
        self.book.resolve_gate('orphan', ledger.GateRunning('i-fixture'))
        self.fake.describe_errors = [AwsUnavailable('fixture outage')]
        self.fake.confirm_termination = False
        self.assertFalse(cloud_gate.cleanup_folder(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)
        self.fake.confirm_termination = True
        self.assertTrue(cloud_gate.cleanup_folder(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)

    def test_each_log_download_publishes_module_progress(self) -> None:
        config = self.root / 'cloud_gate.conf'
        _ = config.write_text(re.sub(r'(?m)^instance_check_s=.*$', 'instance_check_s=6', config.read_text()))
        self.fake.has_log = False
        self.fake.has_result = False
        self.fake.log = 'Runner: 1 modules, whole machine, 32 CPUs\n'
        started = self.now
        polls: dict[float, list[str]] = {}

        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            elapsed = self.now - started
            if 'list-objects-v2' in arguments:
                self.assertEqual(arguments[arguments.index('--prefix') + 1], 'artifacts/attempt/')
                self.assertEqual(arguments[arguments.index('--bucket') + 1], 'fixture-bucket')
                polls.setdefault(elapsed, []).append('list-objects-v2')
            elif 'get-object' in arguments:
                polls[elapsed].append(arguments[arguments.index('--key') + 1].rsplit('/', 1)[-1])
            elif 'describe-instances' in arguments and self.fake.launches and not self.fake.terminated:
                polls.setdefault(elapsed, []).append('describe-instances')
            return self.fake(arguments, **options)

        def advance_poll(seconds: float) -> None:
            self.advance(seconds)
            elapsed = self.now - started
            self.fake.has_log = elapsed >= 2
            if elapsed == 6:
                self.fake.log += 'fixture/test_ok.py exit=0 seconds=1.00\n'
            self.fake.has_result = elapsed >= 10

        sidecar = self.folder / 'reservation.cgroup'
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
            self.book, self.settings, sidecar, clock=lambda: self.now, pause=advance_poll)
        with (mock.patch.object(time, 'monotonic', side_effect=lambda: self.now),
              mock.patch.object(progress_profile, 'publish_counter', wraps=progress_profile.publish_counter) as publish):
            self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'complete')
        self.assertEqual(polls, {
            0: ['describe-instances', 'list-objects-v2'],
            2: ['list-objects-v2', 'gate.log'],
            4: ['list-objects-v2'],
            6: ['describe-instances', 'list-objects-v2', 'gate.log'],
            8: ['list-objects-v2'],
            10: ['list-objects-v2', 'result.json', 'gate.log'],
        })
        started_counter = progress_profile.ProgressCounter('modules', 0, 1)
        finished_counter = progress_profile.ProgressCounter('modules', 1, 1)
        self.assertEqual(progress_profile.read_counter(sidecar), finished_counter)
        self.assertEqual(publish.call_args_list, [mock.call(sidecar, counter)
            for counter in (started_counter, finished_counter, finished_counter)])

    def test_module_progress_counts_each_module_once(self) -> None:
        text = ('Runner: 2 modules, whole machine, 16 CPUs\n'
                + 'fixture/test_one.py exit=1 seconds=1.00\n'
                + 'fixture/test_two.py exit=-15 seconds=2.00 stopped\n'
                + 'fixture/test_one.py exit=0 seconds=1.00\n')
        self.assertEqual(cloud_gate.log_progress(text), progress_profile.ProgressCounter('modules', 2, 2))
        self.assertIsInstance(cloud_gate.log_progress('Preparing tools\n'), progress_profile.NoCounter)
        self.assertIsInstance(cloud_gate.log_progress('Runner: 0 modules, whole machine, 32 CPUs\n'),
                              progress_profile.NoCounter)

    def test_each_log_download_publishes_cloud_state_and_latest_failures(self) -> None:
        self.fake.log = ('Runner: 2 modules, whole machine, 16 CPUs\n'
                        + 'fixture/test_one.py exit=1 seconds=1.00\n'
                        + 'fixture/test_two.py exit=-15 seconds=2.00 stopped\n'
                        + 'fixture/test_one.py exit=0 seconds=1.00\n'
                        + 'Cloud gate: uploading\n')
        sidecar = self.folder / 'reservation.cgroup'

        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'run-instances' in arguments:
                self.assertEqual(cloud_progress.read(sidecar),
                    cloud_progress.CloudProgress('requesting', 32, 0, self.now, stage='launch'))
            elif not self.fake.launches:
                self.assertIsInstance(cloud_progress.read(sidecar), cloud_progress.NoCloudProgress)
            return self.fake(arguments, **options)

        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=transport),
            self.book, self.settings, sidecar, clock=lambda: self.now, pause=self.advance)
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'complete')
        expected = cloud_progress.CloudProgress('uploading', 16, 1, self.now,
            stage='uploading', region='fixture-region', zone='a')
        self.assertEqual(cloud_progress.read(sidecar), expected)
        self.assertEqual(publish.call_args_list[-1], mock.call(sidecar, expected))
        states = [cast(cloud_progress.CloudProgress, call.args[1]).state for call in publish.call_args_list]
        self.assertEqual(states, ['requesting', 'requesting', 'uploading'])

    def test_paired_log_uses_the_last_stage_without_changing_merge_failure_counts(self) -> None:
        boot = ('2026-11-12T13:57:00Z stage=boot\n'
                '2026-11-12T13:57:01Z stage=tools ready\n'
                '2026-11-12T13:57:02Z stage=tools checked\n'
                '2026-11-12T13:57:03Z stage=source ready\n')
        cases = (
            (boot, 'setup', 'source ready'),
            (boot + '2026-11-12T13:57:04Z stage=warm-up base\n', 'setup', 'warm-up base'),
            (boot + '2026-11-12T13:57:04Z stage=round 2 of 3 base\n', 'running', 'round 2 of 3 base'),
            (boot + '2026-11-12T13:57:04Z stage=round 2 of 3 base\n2026-11-12T13:57:05Z stage=finish\n',
             'running', 'finish'),
        )
        for log, state, stage in cases:
            with self.subTest(stage=stage):
                result = cloud_gate.log_cloud_progress(log, 32, 100)
                self.assertEqual((result.state, result.stage, result.failed_modules), (state, stage, 0))

    def test_merge_log_clears_stages_after_the_runner_and_keeps_its_counter(self) -> None:
        log = ('2026-11-12T13:57:00Z stage=tests start\n'
               'Runner: 3 modules, whole machine, 16 CPUs\n'
               'fixture/test_one.py exit=1 seconds=1.0\n'
               'fixture/test_two.py exit=0 seconds=1.0\n'
               '2026-11-12T13:57:01Z stage=round 2 of 3 base\n')
        result = cloud_gate.log_cloud_progress(log, 32, 100)
        self.assertEqual(result, cloud_progress.CloudProgress('running', 16, 1, 100))
        self.assertEqual(cloud_gate.log_progress(log), progress_profile.ProgressCounter('modules', 2, 3))
        result = cloud_gate.log_cloud_progress(log + 'Cloud gate: uploading\n', 32, 101)
        self.assertEqual(result, cloud_progress.CloudProgress('uploading', 16, 1, 101, stage='uploading'))

    def test_empty_log_shows_launch_until_the_first_box_stage(self) -> None:
        self.assertEqual(cloud_gate.log_cloud_progress('', 32, 100),
                         cloud_progress.CloudProgress('requesting', 32, 0, 100, stage='launch'))
        self.assertEqual(cloud_gate.log_cloud_progress('2026-11-12T13:57:00Z stage=boot\n', 32, 101),
                         cloud_progress.CloudProgress('setup', 32, 0, 101, stage='boot'))

    def test_launched_gate_publishes_its_region_and_zone_after_launch(self) -> None:
        worker, _ = self.steering_fixture()
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 6
        self.fake.log = '2026-11-12T13:57:00Z stage=round 2 of 3 base\n'
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(worker.execute(self.folder, self.request, self.deployment)['status'], 'complete')
        updates = [cast(cloud_progress.CloudProgress, call.args[1]) for call in publish.call_args_list]
        self.assertTrue(all((row.region, row.zone) == ('', '') for row in updates[:-2]))
        self.assertEqual(updates[-2], cloud_progress.CloudProgress('requesting', 32, 0, self.now,
            stage='launch', region='us-west-2', zone='us-west-2a'))
        final = updates[-1]
        self.assertEqual((final.region, final.zone, final.stage), ('us-west-2', 'us-west-2a', 'round 2 of 3 base'))
        saved = cloud_gate.read(self.folder / 'launch.json')
        self.assertEqual((saved['region'], saved['zone']), ('us-west-2', 'us-west-2a'))

    def test_cloud_state_tracks_setup_running_and_capacity_fallback_size(self) -> None:
        self.assertEqual(cloud_gate.log_cloud_progress('Preparing tools\n', 32, 1),
                         cloud_progress.CloudProgress('setup', 32, 0, 1))
        self.assertEqual(cloud_gate.log_cloud_progress('Runner: 3 modules, whole machine, 16 CPUs\n', 32, 2),
                         cloud_progress.CloudProgress('running', 16, 0, 2))
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture'] * 3
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        sizes = [cast(cloud_progress.CloudProgress, call.args[1]).size_vcpus for call in publish.call_args_list]
        self.assertEqual(sizes, [32, 32, 16, 16, 16, 16])

    def test_instance_and_volume_tags_use_the_machine_name(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        for resource in ledger.rows(self.fake.launches[0], 'TagSpecifications'):
            tags = {str(row['Key']): row['Value'] for row in ledger.rows(resource, 'Tags')}
            self.assertEqual(tags['Name'], 'cloud-claude-gate')
            self.assertEqual(tags['Workload'], 'cloud-claude-gate')

    def test_first_gate_reconciles_the_period_before_deployment(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        charge_files = list((self.state / 'charges').glob('*.json'))
        self.assertEqual(len(charge_files), 1)
        reading = ledger.read_charge(charge_files[0])
        self.assertLess(reading.period_end, self.book.read_snapshot().inventories[0].at)
        self.assertEqual(reading.estimated_usd, Decimal(0))
        self.assertEqual(reading.actual_usd, Decimal(0))
        commands = [row for row in self.fake.commands if 'get-cost-and-usage' in row]
        self.assertEqual(commands[0][commands[0].index('--filter') + 1], USAGE_ONLY)
        self.assertEqual(commands[0][commands[0].index('--group-by') + 1], 'Type=DIMENSION,Key=USAGE_TYPE')
        self.fake.billing_pages = [account_reply(commands[0], '-0.0000036')]
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
                                      self.book, self.settings, self.folder / 'reservation.cgroup',
                                      clock=lambda: self.now + 86400)
        _ = worker.charges()
        self.assertLess(min(row.usd for row in self.book.read_snapshot().reconciliations), Decimal(0))
        self.fake.billing_pages = [{'ResultsByTime': []}]
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
                                      self.book, self.settings, self.folder / 'reservation.cgroup',
                                      clock=lambda: self.now + 172800)
        with self.assertRaisesRegex(ValueError, 'daily reply is unavailable'):
            worker.charges()

    def test_remote_failure_remains_red_without_fallback(self) -> None:
        self.fake.result = self.result(1)
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        # The outcome names the downloaded result and holds no second copy.
        self.assertEqual(outcome.get('result_file'), cloud_gate.RESULT_FILE)
        self.assertNotIn('result', outcome)
        result = cloud_gate.outcome_result(self.folder, outcome)
        self.assertFalse(gate_box.decode_run(result['gate']).passed)
        self.assertEqual(self.book.launches()[-1]['result'], 'finished')

    def test_invalid_result_does_not_record_finished_time(self) -> None:
        self.fake.result['tree'] = 'other-tree'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual([row['result'] for row in self.book.launches()], ['launched'])

    def test_capacity_tries_every_zone_before_the_smaller_size(self) -> None:
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture'] * 3
        self.assertEqual(self.execute()['status'], 'complete')
        choices = [(row['InstanceType'], ledger.rows(row, 'NetworkInterfaces')[0]['SubnetId'])
                   for row in self.fake.launches]
        self.assertEqual(choices, [('m7a.8xlarge', 'fixture-a'), ('m7a.8xlarge', 'fixture-b'),
                                   ('m7a.4xlarge', 'fixture-a'), ('m7a.4xlarge', 'fixture-b')])
        self.assertEqual(self.book.launches()[-1], {'time': self.now, 'attempt': 'attempt',
            'instance_type': 'm7a.4xlarge', 'vcpus': 16, 'result': 'finished', 'gate_s': 1.0, 'mode': 'merge-gate',
            'region': 'fixture-region', 'gate_job': {'kind': 'GateJobNotRecorded'}})
        spends = self.book.spend_days(read_settings(self.settings), int(self.now))
        self.assertEqual(sum(day.runs for day in spends), 1)
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].instance_type, 'm7a.4xlarge')

    def test_targeted_run_records_mode_and_gate_job_in_every_event(self) -> None:
        self.request['mode'] = 'targeted-test'
        job_record = {'kind': 'StepJob', 'step': 'test-affected', 'unit': '', 'branch': '', 'modules': 0}
        self.request['gate_job'] = job_record
        cloud_gate.save(self.folder / 'request.json', self.request)
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'complete')
        rows = self.book.launches()
        self.assertEqual([row['result'] for row in rows], ['refused', 'launched', 'finished'])
        self.assertTrue(all(row['mode'] == 'targeted-test' for row in rows))
        self.assertEqual([row['gate_job'] for row in rows], [job_record] * 3)

    def test_worker_completes_when_the_request_job_is_unreadable(self) -> None:
        jobs: tuple[tuple[str, object], ...] = (
            ('unknown', {'kind': 'FutureGateJob'}),
            ('damaged', {'kind': 'StepJob', 'step': 'test-all', 'modules': []}),
        )
        for attempt, job in jobs:
            with self.subTest(attempt=attempt):
                folder = self.root / attempt
                folder.mkdir()
                request = {**self.request, 'attempt': attempt, 'gate_job': job, 'source_key': ''}
                cloud_gate.save(folder / 'request.json', request)
                fake = FakeGateAws()
                fake.result = {**self.result(), 'attempt': attempt}
                ledger.mapping(fake.result['record'])['id'] = 'gate-' + attempt
                worker = cloud_gate.GateWorker(AwsCli('fixture', 'fixture-region', 1, execute=fake),
                    self.book, self.settings, folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
                with mock.patch.object(worker, 'reconcile', wraps=worker.reconcile) as reconcile:
                    self.assertEqual(worker.execute(folder, request, self.deployment)['status'], 'complete')
                reconcile.assert_called_once_with()
                rows = [row for row in self.book.launches() if row['attempt'] == attempt]
                self.assertEqual([row['result'] for row in rows], ['launched', 'finished'])
                self.assertEqual([row['gate_job'] for row in rows], [{'kind': 'GateJobNotRecorded'}] * 2)

    def test_all_capacity_refusals_allow_local_fallback(self) -> None:
        self.fake.refusals = ['An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation: fixture'] * 4
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['mode'] == 'merge-gate' for row in rows))
        self.assertTrue(all(row['result'] == 'refused' for row in rows))
        self.assertEqual(output.getvalue().count('Cloud gate refused:'), 4)

    def test_rejected_parameter_falls_back_and_records_a_refusal(self) -> None:
        reason = ('An error occurred (InvalidParameterValue) when calling the RunInstances operation: '
                  + 'Encoded User data is limited to 25600 bytes; account 123456789012 subnet-fixture i-private')
        self.fake.refusals = [reason]
        with redirect_stdout(StringIO()) as output:
            outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('InvalidParameterValue', str(outcome['reason']))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        evidence = cloud_gate.read(self.folder / 'launch.json')['spot_price_reads']
        self.assertEqual(rows, [{'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
            'vcpus': 32, 'availability_zone': 'a', 'result': 'refused', 'mode': 'merge-gate', 'error_code': 'InvalidParameterValue',
            'reason': reason, 'region': 'fixture-region', 'spot_price_reads': evidence,
            'gate_job': {'kind': 'GateJobNotRecorded'}}])
        self.assertEqual(output.getvalue(), 'Cloud gate refused: m7a.8xlarge in a: InvalidParameterValue\n')
        for private in ('123456789012', 'subnet-fixture', 'i-private', 'RED'):
            self.assertNotIn(private, output.getvalue() + str(outcome))

    def test_launched_box_records_vcpus_and_download_links(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        evidence = cloud_gate.read(self.folder / 'launch.json')['spot_price_reads']
        self.assertEqual(rows, [{'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
            'vcpus': 32, 'availability_zone': 'a', 'result': 'launched', 'error_code': '', 'reason': '', 'mode': 'merge-gate',
            'region': 'fixture-region', 'spot_price_reads': evidence, 'gate_job': {'kind': 'GateJobNotRecorded'}},
            {'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
             'vcpus': 32, 'result': 'finished', 'gate_s': 1.0, 'mode': 'merge-gate', 'region': 'fixture-region',
             'gate_job': {'kind': 'GateJobNotRecorded'}}])
        data = ledger.text(self.fake.launches[0], 'UserData')
        self.assertTrue(data.startswith('#!/bin/sh\n'))
        link_rows = data.split("<<'GATE_LINKS'\n", 1)[1].split('\nGATE_LINKS', 1)[0]
        links: dict[str, object] = {}
        for line in link_rows.splitlines():
            name, _, _, value = line.split('\t', 3)
            links[cast(str, json.loads(name))] = cast(object, json.loads(value))
        for key in ('guest', 'request', 'source', 'log', 'result', 'boot_error'):
            parsed = urlsplit(ledger.text(links, key))
            query = parse_qs(parsed.query)
            self.assertEqual(parsed.scheme, 'https')
            self.assertEqual(query['X-Amz-Expires'], ['1200'])
            self.assertEqual(query['X-Amz-Security-Token'], ['fixture-token'])
            self.assertTrue(query['X-Amz-Credential'][0].startswith('fixture-access/'))
        self.assertEqual(urlsplit(ledger.text(links, 'log')).path, '/artifacts/attempt/gate.log')
        self.assertNotIn('fixture-secret', data)
        self.assertNotIn('s3://', data)
        self.assertLess(len(base64.b64encode(data.encode())), 25600)
        self.assertNotIn('test_module_0000.py', data)
        self.assertIn('requests/attempt.guest.py', data)
        self.assertIn('requests/attempt.json', data)
        self.assertEqual((self.folder / 'guest.py').read_text(), gate_box.GUEST)

    def test_aws_outage_before_launch_allows_fallback(self) -> None:
        self.fake.upload_error = 'EndpointConnectionError'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))

    def test_saved_variant_without_arguments_records_absence_before_launch(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        cloud_gate.save(self.folder / 'launch.json', {'variant': 1})
        self.fake.launches.clear()
        with mock.patch.object(s3_presign, 'presign', side_effect=OSError('Cannot prepare the launch')):
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])
        self.assertEqual(cloud_gate.read(self.folder / 'launch.json'), {'variant': 1})
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))

    def test_missing_image_allows_fallback(self) -> None:
        self.fake.refusals = ['An error occurred (InvalidAMIID.NotFound) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'fallback')

    def test_access_denial_allows_local_fallback(self) -> None:
        self.fake.upload_error = 'AccessDenied'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_upload_timeout_allows_local_fallback(self) -> None:
        self.fake.upload_timeout = True
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('timed out after 30 seconds', str(outcome['reason']))
        self.assertEqual(self.fake.launches, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_lost_launch_reply_reuses_exact_arguments_and_token(self) -> None:
        self.fake.refusals = ['EndpointConnectionError']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(len(self.fake.launches), 2)
        self.assertEqual(self.fake.launches[0], self.fake.launches[1])

    def test_uncertain_launch_keeps_reservation_before_local_fallback(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(self.fake.launches[0], self.fake.launches[-1])
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_uncertain_launch_subprocess_error_keeps_reservation(self) -> None:
        self.fake.launch_errors = [subprocess.CalledProcessError(1, 'aws run-instances')]
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertFalse(self.fake.terminated)

    def test_uncertain_launch_cli_error_keeps_reservation(self) -> None:
        reasons = [('Read timeout on endpoint URL: "https://fixture"', 1), ('run-instances: invalid reply', 1)]
        reasons.extend((f'An error occurred ({code}) when calling the RunInstances operation: fixture',
                        cloud_gate.settings().whole('launch_reply_tries'))
                       for code in ('InternalFailure', 'ServerInternal', 'Unavailable'))
        for reason, tries in reasons:
            with self.subTest(reason=reason):
                self.fake.refusals = [reason] * tries
                self.fake.launches.clear()
                self.assertEqual(self.execute(), {'status': 'fallback', 'reason': reason})
                self.assertEqual(len(self.fake.launches), tries)
                self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
                self.assertFalse(self.fake.terminated)

    def test_uncertain_launch_reply_keeps_reservation(self) -> None:
        replies: tuple[dict[str, object], ...] = (
            {}, {'Instances': []}, {'Instances': [{'InstanceId': 'i-first'}, {'InstanceId': 'i-second'}]})
        for reply in replies:
            with self.subTest(reply=reply):
                self.fake.launch_reply = reply
                self.assertEqual(self.execute()['status'], 'fallback')
                self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
                self.assertFalse(self.fake.terminated)
        self.assertEqual(len(self.fake.launches), len(replies))

    def test_uncertain_launch_log_write_error_keeps_reservation(self) -> None:
        with mock.patch.object(cloud_gate.GateWorker, 'record_launch', side_effect=OSError('Cannot write the launch log')):
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertFalse(self.fake.terminated)

    def test_uncertain_launch_outage_reconciles_a_running_instance_at_expiry(self) -> None:
        tries = cloud_gate.settings().whole('launch_reply_tries')
        self.fake.refusals = ['EndpointConnectionError'] * tries
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), tries)
        self.assertTrue(all(row == self.fake.launches[0] for row in self.fake.launches))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.advance(1200)
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated(int(self.now), 'i-fixture'))
        queries = [command for command in self.fake.commands if '--filters' in command]
        self.assertTrue(queries)

    def test_uncertain_launch_timeout_reconciles_no_instance_at_expiry(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.fake.has_instance = False
        self.advance(1200)
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))
        queries = [command for command in self.fake.commands if '--filters' in command]
        self.assertTrue(queries)

    def test_expired_uncertain_launch_keeps_reservation_until_reconciliation(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.fake.has_instance = False
        self.advance(1200)
        outcome = self.execute()
        self.assertEqual(outcome, {'status': 'fallback', 'reason': 'The expired launch remains unconfirmed'})
        self.assertEqual(len(self.fake.launches), 1)
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        for error in (subprocess.TimeoutExpired('aws describe-instances', 1),
                      AwsUnavailable('EndpointConnectionError')):
            with self.subTest(error=type(error).__name__):
                self.fake.describe_errors = [error]
                self.assertEqual(self.execute()['status'], 'fallback')
                self.assertEqual(len(self.fake.launches), 1)
                self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)

    def test_unauthorized_launch_records_absence_at_once(self) -> None:
        # With no other type left, the refusal ends the launch.
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', 'm7a.4xlarge')
        self.fake.refusals = ['An error occurred (UnauthorizedOperation) when calling the RunInstances operation: fixture']
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(len(self.fake.launches), 1)
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))

    def test_invalid_result_identity_verdict_or_modules_is_rejected(self) -> None:
        for field, value in (('attempt', 'other'), ('tree', 'other'), ('commit', 'other'),
                             ('host', ''), ('verdict', 'fail')):
            with self.subTest(field=field):
                row = {**self.result(), field: value}
                with self.assertRaises(ValueError):
                    cloud_gate.validate_result(row, self.request)
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_other.py', 0, 1, ''),), 1, 1)
        with self.assertRaises(ValueError):
            cloud_gate.validate_result({**self.result(), 'gate': gate_box.encode_run(run)}, self.request)

    def test_missing_final_log_allows_local_fallback(self) -> None:
        self.fake.has_log = False
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertTrue(self.fake.terminated)

    def test_dead_instance_without_result_allows_local_fallback(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'terminated'
        self.assertEqual(self.execute(), {'status': 'fallback', 'reason': 'The instance stopped without a final result'})
        self.assertTrue(self.fake.terminated)

    def test_stopped_instance_reports_a_confirmed_spot_interruption(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'shutting-down'
        code = 'Server.SpotInstanceTermination'
        message = 'Server.SpotInstanceTermination: Spot instance termination'
        self.fake.state_reason = {'Code': code, 'Message': message}
        self.assertEqual(self.execute(), {
            'status': 'interrupted', 'interruption_code': code,
            'reason': f'The instance stopped without a final result ({code}: {message})'})
        descriptions = [command for command in self.fake.commands if 'describe-instances' in command]
        self.assertEqual(len(descriptions), 3)
        self.assertTrue(self.fake.terminated)

    def test_spot_shutdown_uses_the_code_and_requires_confirmed_cleanup(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'shutting-down'
        self.fake.state_reason = {'Code': 'Server.SpotInstanceShutdown'}
        self.fake.confirm_termination = False
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('Termination is unconfirmed', str(outcome['reason']))
        self.assertNotIsInstance(self.book.read_snapshot().gate_reservations[-1].state, ledger.GateTerminated)

    def test_a_spot_message_without_a_spot_code_does_not_request_a_retry(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'terminated'
        self.fake.state_reason = {'Code': 'Client.UserInitiatedShutdown', 'Message': 'Server.SpotInstanceTermination'}
        self.assertEqual(self.execute()['status'], 'fallback')

    def test_a_final_failure_has_priority_over_the_spot_state_reason(self) -> None:
        self.fake.result = self.result(1)
        self.fake.state = 'terminated'
        self.fake.state_reason = {'Code': 'Server.SpotInstanceTermination'}
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        self.assertFalse(gate_box.decode_run(cloud_gate.outcome_result(self.folder, outcome)['gate']).passed)

    def test_expired_instance_without_result_allows_local_fallback(self) -> None:
        self.fake.has_result = False
        self.request['expires'] = int(self.now) + 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertTrue(self.fake.terminated)

    def test_unconfirmed_termination_keeps_charging(self) -> None:
        self.fake.confirm_termination = False
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)
        before = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(60)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, before)

    def worker_with_pending_gate(self) -> cloud_gate.GateWorker:
        reservation = ledger.GateReservation('orphan', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal(0))
        self.assertIsInstance(self.book.reserve_gate(self.settings, reservation), ledger.GateReserved)
        return cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)

    def test_expired_pending_gate_without_instance_stops_charging(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^(regions|spot_candidate_regions)=.*$',
                                  r'\1=us-east-2 us-west-2', path.read_text()))
        worker = self.worker_with_pending_gate()
        self.fake.has_instance = False
        self.advance(8 * 3600)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        self.advance(3600)
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        self.assertEqual(len(self.fake.commands), 2)
        for query in self.fake.commands:
            self.assertEqual(query[query.index('--filters') + 1], 'Name=tag:GateAttempt,Values=orphan')

    def test_expired_pending_gate_with_terminated_instance_stops_charging(self) -> None:
        worker = self.worker_with_pending_gate()
        self.fake.state = 'terminated'
        self.advance(1200)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        charged = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(3600)
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, charged)

    def test_expired_pending_gate_with_live_instance_is_observed_next_time(self) -> None:
        worker = self.worker_with_pending_gate()
        self.advance(1200)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated(int(self.now), 'i-fixture'))
        self.fake.state = 'terminated'
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        self.assertIn('--instance-ids', self.fake.commands[-1])

    def test_pending_gate_before_expiry_is_unchanged(self) -> None:
        worker = self.worker_with_pending_gate()
        self.advance(1199)
        with mock.patch.object(cloud_gate, 'process_start', return_value=Gone()):
            worker.reconcile()
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertEqual(self.fake.commands, [])

    def test_combined_rust_cost_refuses_gate_before_launch(self) -> None:
        self.book.observe(ledger.InstanceObservation(int(self.now), 'running', 'fixture'))
        self.advance(18 * 3600)
        self.request['expires'] = int(self.now) + 1200
        self.assertEqual(self.execute(), {'status': 'fallback', 'reason': 'daily_limit'})
        self.assertEqual(self.fake.launches, [])

    def test_parallel_admissions_share_one_cost_reservation_lock(self) -> None:
        self.costs({'daily_limit_usd': '4', 'limit_reserve_usd': '3'})
        def reserve(index: int) -> ledger.GateReserved | ledger.GateCapReached | ledger.GateAttemptEnded:
            return self.book.reserve_gate(self.settings, ledger.GateReservation(
                str(index), int(self.now), int(self.now) + 3600, Decimal('.6'), Decimal(0)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(isinstance(row, ledger.GateReserved) for row in results), 1)
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_future_reservation_checks_the_next_day_and_month(self) -> None:
        self.costs({'daily_limit_usd': '4', 'monthly_limit_usd': '4', 'limit_reserve_usd': '3'})
        boundary = instant('2026-01-31T23:59:59+00:00')
        result = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'boundary', boundary, boundary + 3600, Decimal('1.1'), Decimal(0)))
        self.assertIsInstance(result, ledger.GateCapReached)
        self.assertEqual(self.book.read_snapshot().gate_reservations, [])

    def test_gate_reservation_also_restricts_rust_admission(self) -> None:
        self.costs({'daily_limit_usd': '4', 'limit_reserve_usd': '3'})
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'busy', int(self.now), int(self.now) + 3600, Decimal('.99'), Decimal(0)))
        self.book.observe(ledger.InstanceObservation(int(self.now), 'running', 'fixture'))
        decision = self.book.guard(self.settings, 'admission', int(self.now) + 60, notify=lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingRefused)

    def test_import_files_under_aws_keeps_remote_host_and_appends_once(self) -> None:
        buildlog = self.root / 'buildlog'
        destination = buildlog / 'fixture-guest.jsonl'
        destination.parent.mkdir()
        traceback = 'Traceback (most recent call last):\nAssertionError: retained cloud failure\n'
        for retried in (False, True):
            with self.subTest(retried=retried):
                passed = gate.GateJobResult('fixture', 'test_ok.py', 0, 1, 'OK')
                failed = gate.GateJobResult('fixture', 'test_ok.py', 1, 2, traceback)
                run = gate.GateRun((passed,), 3, 2, executions=(failed, passed) if retried else (passed,))
                result = self.result()
                result['gate'] = gate_box.encode_run(run)
                result['record'] = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'fixture-guest',
                                                             self.now, self.now + 3, 'merge-gate')
                _ = destination.write_text('{"interrupted":')
                cloud_gate.save(self.folder / cloud_gate.RESULT_FILE, result)
                with (mock.patch.dict(os.environ, {'BUILDLOG_DIR': str(buildlog)}),
                      mock.patch.object(store, 'host_file', return_value=destination) as host_file):
                    cloud_gate.import_record(self.folder, result)
                    cloud_gate.import_record(self.folder, result)
                self.assertEqual(host_file.call_args.args[0], 'aws')
                # The result leaves the gate copy for the store, beside the gate run record.
                self.assertFalse((self.folder / cloud_gate.RESULT_FILE).exists())
                kept_result = ledger.text(cloud_gate.read(self.folder / 'imported.json'), 'result')
                self.assertTrue(kept_result.startswith('aws/results/'))
                if not retried:
                    # The store keeps the first result of a record id, as it keeps the first record.
                    self.assertEqual(json.loads(gzip.decompress((buildlog / kept_result).read_bytes())), result)
                records = [ledger.mapping(cast(object, json.loads(line))) for line in destination.read_text().splitlines()]
                self.assertEqual([row['id'] for row in records], ['gate-attempt'])
                self.assertEqual(records[0]['host'], 'fixture-guest')
                modules = ledger.rows(records[0], 'modules')
                if retried:
                    first, second = modules
                    self.assertNotEqual(first['log'], '')
                    kept = gzip.decompress((buildlog / ledger.text(first, 'log')).read_bytes()).decode('utf-8')
                    self.assertIn(traceback, kept)
                    self.assertEqual((first['attempt'], first['status']), (1, 1))
                    self.assertEqual((second['attempt'], second['status'], second['log']), (2, 0, ''))
                else:
                    self.assertEqual(modules[0]['log'], '')
        # A store that cannot keep the result leaves it in the gate copy.
        result = self.result()
        cloud_gate.save(self.folder / cloud_gate.RESULT_FILE, result)
        with (mock.patch.dict(os.environ, {'BUILDLOG_DIR': str(buildlog)}),
              mock.patch.object(store, 'host_file', return_value=destination),
              mock.patch('scripts.buildlog.record.keep_result', side_effect=OSError('fixture store is full')),
              mock.patch('scripts.test_times.gate_record.report_not_written') as reported):
            cloud_gate.import_record(self.folder, result)
        self.assertTrue((self.folder / cloud_gate.RESULT_FILE).exists())
        self.assertNotIn('result', cloud_gate.read(self.folder / 'imported.json'))
        reported.assert_called_once()

    def test_first_import_skips_existing_rows(self) -> None:
        destination = store.host_file('aws', self.now)
        destination.parent.mkdir(parents=True)
        prefix = b'{"id":"older-record"}\n' * 10000 + b'invalid JSON\n'
        _ = destination.write_bytes(prefix)

        cloud_gate.import_record(self.folder, self.result())

        saved = cloud_gate.read(self.folder / 'import-boundary.json')
        self.assertEqual(saved, {'id': 'gate-attempt', 'destination': str(destination), 'offset': len(prefix)})
        contents = destination.read_bytes()
        self.assertEqual(contents[:len(prefix)], prefix)
        self.assertEqual(len(contents[len(prefix):].splitlines()), 1)
        self.assertEqual(ledger.mapping(cast(object, json.loads(contents[len(prefix):])))['id'], 'gate-attempt')

    def test_import_retries_after_append_without_appending_again(self) -> None:
        destination = store.host_file('aws', self.now)
        result_path = self.folder / cloud_gate.RESULT_FILE
        imported = self.folder / 'imported.json'
        result = self.result()
        cloud_gate.save(result_path, result)
        save = cloud_gate.save

        def interrupt_imported_save(path: Path, row: dict[str, object]) -> None:
            if path == imported:
                raise OSError('fixture interrupted the import')
            save(path, row)

        with mock.patch('scripts.test_times.gate_record.keep_failure_logs') as keep_failure_logs:
            with mock.patch.object(cloud_gate, 'save', side_effect=interrupt_imported_save):
                with self.assertRaisesRegex(OSError, 'fixture interrupted'):
                    cloud_gate.import_record(self.folder, result)
            self.assertFalse(imported.exists())
            store.append_line(destination, {'id': 'another-record'})
            contents = destination.read_bytes()
            cloud_gate.import_record(self.folder, cloud_gate.read(result_path))

        keep_failure_logs.assert_called_once()
        self.assertEqual(destination.read_bytes(), contents)
        self.assertEqual(cloud_gate.read(imported)['id'], 'gate-attempt')
        self.assertFalse(result_path.exists())

    def test_reused_import_folder_ignores_the_previous_record_marker(self) -> None:
        destination = store.host_file('aws', self.now)
        cloud_gate.import_record(self.folder, self.result())
        first_end = destination.stat().st_size
        result = self.result()
        result['attempt'] = 'next-attempt'
        ledger.mapping(result['record'])['id'] = 'gate-next-attempt'

        cloud_gate.import_record(self.folder, result)

        self.assertEqual([row['id'] for row in store.locked_rows(destination)], ['gate-attempt', 'gate-next-attempt'])
        self.assertEqual(cloud_gate.read(self.folder / 'import-boundary.json'),
                         {'id': 'gate-next-attempt', 'destination': str(destination), 'offset': first_end})

    def test_import_saves_the_offset_after_tail_repair(self) -> None:
        destination = store.host_file('aws', self.now)
        store.append_line(destination, {'id': 'older-record'})
        whole_end = destination.stat().st_size
        with destination.open('ab') as handle:
            _ = handle.write(b'{"id":"unfinished')

        cloud_gate.import_record(self.folder, self.result())
        cloud_gate.import_record(self.folder, self.result())

        self.assertEqual(cloud_gate.read(self.folder / 'import-boundary.json')['offset'], whole_end)
        rows = [ledger.mapping(cast(object, json.loads(line))) for line in destination.read_bytes().splitlines()]
        self.assertEqual([row['id'] for row in rows], ['older-record', 'gate-attempt'])

    def test_remote_red_never_calls_the_local_runner(self) -> None:
        remote = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', 1, 1, ''),), 1, 1)
        for mode, full in ((gate.SuiteMode.MERGE_GATE, False), (gate.SuiteMode.TARGETED_TEST, True)):
            with (self.subTest(mode=mode, full=full),
                  mock.patch.object(cloud_gate, 'run', return_value=cloud_gate.CloudCompleted(remote)) as run,
                  mock.patch.object(gate, 'select_affected', side_effect=AssertionError('The full suite needs no selection')),
                  mock.patch.object(gate, 'run_suites') as local):
                self.assertEqual(gate.run_gate_suites(self.root, mode=mode, full=full), remote)
            local.assert_not_called()
            self.assertIsInstance(cast(object, run.call_args.kwargs['selected']), gate.AllModules)

    def test_allowed_fallback_calls_the_original_local_runner(self) -> None:
        with (mock.patch.object(cloud_gate, 'run', return_value=cloud_gate.LocalFallback('AWS is down')),
              mock.patch.object(gate, 'run_suites') as local, redirect_stdout(StringIO())):
            _ = gate.run_gate_suites(self.root, full=True, mode=gate.SuiteMode.TARGETED_TEST)
        self.assertTrue(local.call_args.kwargs['full'])
        self.assertEqual(local.call_args.kwargs['mode'], gate.SuiteMode.TARGETED_TEST)


    def test_paired_result_uses_shared_worker_and_confirms_cleanup(self) -> None:
        request = self.request
        request.update(task='paired', base={'commit': 'base', 'tree': 'base-tree'},
                       changed={'commit': 'commit', 'tree': 'tree'}, module='fixture/test_ok.py', rounds=1)
        sample = {'module': 'fixture/test_ok.py', 'variant': 'base', 'round': 1, 'cpu_s': 1.0,
                  'peak_mib': 1.0, 'wall_s': 1.0, 'status': 0, 'tests': 1, 'failures': 0, 'errors': 0,
                  'instance_type': 'm7a.8xlarge', 'box': ''}
        self.fake.result = {**request, 'runs': [sample, {**sample, 'variant': 'changed'}], 'verdict': 'pass',
                            'gate_s': 2.0, 'log_error': 'The log PUT timed out'}
        cloud_gate.save(self.folder / 'request.json', request)
        reply = self.execute()
        self.assertEqual(reply['status'], 'complete')
        self.assertTrue(self.fake.terminated)
        result = cloud_gate.outcome_result(self.folder, reply)
        self.assertTrue(all(row['box'] == 'i-fixture' for row in ledger.rows(result, 'runs')))
        self.assertNotIn('record', result)

    def test_paired_worker_resume_keeps_a_missing_job(self) -> None:
        request = {**self.request, 'task': 'paired', 'base': {'commit': 'base', 'tree': 'base-tree'},
                   'changed': {'commit': 'commit', 'tree': 'tree'}, 'module': 'fixture/test_ok.py', 'rounds': 1}
        cloud_gate.save(self.folder / 'request.json', request)
        cloud_gate.save(self.folder / 'deployment.json', self.deployment)
        cloud_gate.save(self.folder / 'launch.json', {'instance': 'i-attempt', 'region': 'fixture-region',
                                                       'arguments': {'InstanceType': 'm7a.8xlarge'}})
        self.record_running_gate('attempt', 'm7a.8xlarge', 'fixture-region')
        self.fake.result = {**request, 'runs': [], 'verdict': 'error', 'gate_s': 1.0}
        worker = cloud_gate.GateWorker(AwsCli('fixture-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        connection = mock.Mock(recv=mock.Mock(return_value='launch'))
        group = CloudGroup('fixture', 1, self.now, Owner(1, 'fixture'))
        with (mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
              mock.patch.object(cloud_gate, 'cloud_reservation',
                                return_value=nullcontext(cloud_gate.CloudReservation(worker.sidecar)))):
            aws_trial.paired_module(self.folder, self.state, self.settings, connection, group=group)

        self.assertEqual(cloud_gate.read(self.folder / 'outcome.json')['status'], 'complete')
        self.assertEqual(cloud_gate.read(self.folder / 'request.json'), request)
        self.assertEqual(self.fake.launches, [])
        row, = self.book.launches()
        self.assertEqual((row['result'], row['gate_job']), ('finished', {'kind': 'GateJobNotRecorded'}))

    def test_paired_cancellation_after_recorded_launch_cleans_before_slot_release(self) -> None:
        import multiprocessing
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                attempt = 'launch-cancel-' + str(confirmed)
                folder = self.root / attempt
                folder.mkdir()
                request = {**self.request, 'attempt': attempt, 'task': 'paired', 'source_key': '',
                           'expires': int(self.now) + 1200}
                cloud_gate.save(folder / 'request.json', request)
                cloud_gate.save(folder / 'deployment.json', self.deployment)
                self.fake = FakeGateAws()
                self.fake.confirm_termination = confirmed
                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if 'terminate-instances' in arguments:
                        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                        self.assertTrue(set(aws_trial.TRIAL_SIGNALS) <= blocked)
                    return self.fake(arguments, **options)
                api = AwsCli('fixture', 'us-east-1', 1, execute=transport)
                worker = cloud_gate.GateWorker(api, self.book, self.settings, folder / 'reservation',
                                              clock=lambda: self.now, pause=self.advance)
                resolve = self.book.resolve_gate
                def cancel(attempt: str, state: ledger.GateRunning | ledger.GateAbsent | ledger.GateTerminated, *, instance_type: str = '') -> None:
                    if isinstance(state, ledger.GateRunning):
                        self.assertEqual(cloud_gate.read(folder / 'launch.json')['instance'], 'i-fixture')
                        raise KeyboardInterrupt
                    resolve(attempt, state, instance_type=instance_type)
                trial_group = CloudGroup(folder.parent.name, 1, self.now, Owner(1, 'fixture'))
                @contextmanager
                def slot(_repo: Path, sidecar: Path, job: StepJob, *,
                         group: CloudGroup) -> Generator[cloud_gate.CloudReservation]:
                    self.assertEqual(job, aws_trial.PAIRED_TRIAL_JOB)
                    self.assertEqual(group, trial_group)
                    try:
                        yield cloud_gate.CloudReservation(sidecar)
                    finally:
                        self.assertTrue(self.fake.terminated)
                        _, cleanup = aws_trial.trial_cost(self.book, {attempt}, self.settings, int(self.now))
                        self.assertEqual(cleanup, confirmed)
                parent, child = multiprocessing.get_context('fork').Pipe()
                parent.send('launch')
                previous = {number: signal.getsignal(number) for number in aws_trial.TRIAL_SIGNALS}
                try:
                    with (mock.patch.object(aws_trial, 'AwsCli', return_value=api),
                          mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
                          mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
                          mock.patch.object(self.book, 'resolve_gate', side_effect=cancel),
                          mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
                        aws_trial.paired_module(folder, self.state, self.settings, child, group=trial_group)
                finally:
                    parent.close()
                    child.close()
                    for number, handler in previous.items():
                        self.assertEqual(signal.getsignal(number), handler)
                self.assertEqual(cloud_gate.read(folder / 'outcome.json')['status'], 'fallback')

    def test_paired_cleanup_resolves_boxes_and_absent_launches_once(self) -> None:
        cohort = self.root / 'cleanup-cohort'
        for attempt, launched in (('first', True), ('second', True), ('absent', False)):
            folder = cohort / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt})
            _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
            if launched:
                cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'region': 'us-east-1', 'arguments': {}})
                self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt))
        handlers = {number: signal.getsignal(number) for number in aws_trial.TRIAL_SIGNALS}
        api = AwsCli('fixture', 'us-east-1', 1, execute=self.fake)
        with (mock.patch.object(aws_trial, 'AwsCli', return_value=api),
              mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
            self.assertEqual(aws_trial.main(['--paired-cleanup', str(cohort)], self.settings, self.state), 0)
            calls = len(self.fake.commands)
            self.assertEqual(aws_trial.main(['--paired-cleanup', str(cohort)], self.settings, self.state), 0)
            self.assertEqual(len(self.fake.commands), calls)
            empty = self.root / 'empty-trial'
            empty.mkdir()
            with mock.patch.object(cloud_gate.GateWorker, 'charges'):
                self.assertEqual(aws_trial.coordinate_trial(empty, self.state, self.settings), 0)
        self.assertEqual({number: signal.getsignal(number) for number in handlers}, handlers)
        terminated = [command[command.index('--instance-ids') + 1] for command in self.fake.commands
                      if 'terminate-instances' in command]
        self.assertEqual(terminated, ['i-first'])
        self.assertTrue({'i-first', 'i-second'} <= self.fake.terminated_instances)
        states = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertIsInstance(states['absent'], ledger.GateAbsent)
        self.assertIsInstance(states['first'], ledger.GateTerminated)
        self.assertIsInstance(states['second'], ledger.GateTerminated)

    def test_cleanup_terminates_five_recorded_boxes_before_four_slow_launch_lookups(self) -> None:
        for lookups_timeout in (True, False):
            with self.subTest(lookups_timeout=lookups_timeout):
                cohort = self.root / ('mixed-' + str(lookups_timeout))
                recorded = {'i-recorded-' + str(number) for number in range(5)}
                for number in range(9):
                    attempt = cohort.name + '-' + str(number)
                    folder = cohort / str(number)
                    folder.mkdir(parents=True)
                    cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt})
                    _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                        attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
                    if number < 4:
                        launch: dict[str, object] = {'arguments': {'ClientToken': str(number)}}
                    else:
                        instance = 'i-recorded-' + str(number - 4)
                        launch = {'instance': instance, 'arguments': {}}
                        self.book.resolve_gate(attempt, ledger.GateRunning(instance))
                    cloud_gate.save(folder / 'launch.json', launch)
                self.fake = FakeGateAws()
                started = self.now
                found: set[str] = set()
                lookups: list[str] = []
                def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if 'describe-instances' in arguments and '--filters' in arguments:
                        self.assertTrue(recorded | found <= self.fake.terminated_instances)
                        token = arguments[arguments.index('--filters') + 1]
                        lookups.append(token)
                        timeout = cast(float, options['timeout'])
                        self.assertLessEqual(timeout, cloud_gate.settings().number('termination_timeout_s') - (self.now - started))
                        if lookups_timeout:
                            self.advance(timeout)
                            raise subprocess.TimeoutExpired('fixture lookup', timeout)
                        self.advance(min(5, timeout))
                        instance = 'i-found-' + str(len(lookups))
                        found.add(instance)
                        reply = {'Reservations': [{'Instances': [{'InstanceId': instance}]}]}
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                    return self.fake(arguments, **options)
                worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=transport),
                    self.book, self.settings, cohort / 'reservation', clock=lambda: self.now, pause=self.advance)
                self.assertEqual(aws_trial.cleanup_trial(cohort, worker), not lookups_timeout)
                self.assertEqual(len(lookups), 4)
                self.assertTrue(recorded | found <= self.fake.terminated_instances)
                self.assertLessEqual(self.now - started, cloud_gate.settings().number('termination_timeout_s'))
                if lookups_timeout:
                    self.assertEqual(self.now - started, cloud_gate.settings().number('termination_timeout_s'))
                else:
                    self.assertEqual(len(found), 4)

    def test_nine_box_cleanup_requests_all_terminations_before_one_deadline(self) -> None:
        cohort = self.root / 'nine-boxes'
        instances = {'i-' + str(number) for number in range(9)}
        for number in range(9):
            attempt = str(number)
            folder = cohort / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt})
            cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'arguments': {}})
            _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                attempt, int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
            self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt))
        started = self.now
        timeout = cloud_gate.settings().number('termination_timeout_s')
        self.fake.confirm_termination = False
        def transport(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if 'describe-instances' in arguments:
                self.assertTrue(instances <= self.fake.terminated_instances)
                remaining = timeout - (self.now - started)
                self.assertLessEqual(cast(float, options['timeout']), remaining)
                self.advance(min(7, cast(float, options['timeout'])))
            return self.fake(arguments, **options)
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=transport),
            self.book, self.settings, cohort / 'reservation', clock=lambda: self.now, pause=self.advance)
        self.assertFalse(aws_trial.cleanup_trial(cohort, worker))
        self.assertEqual(self.now - started, timeout)
        self.assertTrue(all(isinstance(row.state, ledger.GateRunning) for row in self.book.read_snapshot().gate_reservations))
        command = cloud_gate.service_command('scripts.aws_target.aws_trial',
            ['--paired-worker', str(cohort)], 'fixture', paired=True)
        stop = next(value for value in command if value.startswith('--property=TimeoutStopSec='))
        self.assertEqual(float(stop.rsplit('=', 1)[1]), timeout + 2 * cloud_gate.settings().number('api_timeout_s'))
        self.assertGreater(float(stop.rsplit('=', 1)[1]), timeout)

    def test_paired_cleanup_failure_keeps_the_reservation(self) -> None:
        cloud_gate.save(self.folder / 'request.json', self.request)
        cloud_gate.save(self.folder / 'launch.json', {'instance': 'i-fixture', 'arguments': {}})
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'attempt', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal('.02')))
        self.book.resolve_gate('attempt', ledger.GateRunning('i-fixture'))
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation', clock=lambda: self.now)
        with mock.patch.object(worker, 'confirm_terminations', return_value=False):
            self.assertFalse(aws_trial.cleanup_trial(self.folder, worker))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)

    def test_trial_start_sweeps_only_expired_unresolved_trials(self) -> None:
        trials = self.root / 'trials'
        current = trials / 'current'
        current.mkdir(parents=True)
        for attempt, expiry in (('expired', int(self.now) + 1), ('live', int(self.now) + 1200)):
            folder = trials / attempt / attempt
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {**self.request, 'attempt': attempt, 'expires': expiry})
            cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'arguments': {}})
            _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
                attempt, int(self.now), expiry, Decimal('.8'), Decimal('.02')))
            self.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt))
        self.advance(2)
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake),
            self.book, self.settings, current / 'reservation', clock=lambda: self.now)
        with (mock.patch.object(aws_trial, 'AwsCli', return_value=worker.api),
              mock.patch.object(time, 'time', return_value=self.now),
              mock.patch.object(cloud_gate.GateWorker, 'charges')):
            self.assertEqual(aws_trial.coordinate_trial(current, self.state, self.settings), 0)
        states = {row.attempt: row.state for row in self.book.read_snapshot().gate_reservations}
        self.assertIsInstance(states['expired'], ledger.GateTerminated)
        self.assertIsInstance(states['live'], ledger.GateRunning)
        self.assertTrue((trials / 'expired' / 'expired' / 'launch.json').exists())

    def test_paired_cost_records_each_refresh_when_saved_query_fee_is_unchanged(self) -> None:
        numeric_settings(self.settings, {'cost_explorer_read_usd': '.01', 'cost_explorer_interval_seconds': '3600'})
        api = AwsCli('fixture', 'us-east-1', 1, execute=self.fake)
        worker = cloud_gate.GateWorker(api, self.book, self.settings, self.folder / 'reservation', clock=lambda: self.now)
        worker.charges()
        initial = sum((row.query_usd for row in self.book.read_bills()), Decimal(0))
        self.assertEqual(initial, Decimal('.01'))
        cohort = self.root / 'query-cohort'
        cohort.mkdir()
        for advance, expected in ((3601, '.01'), (0, '.01'), (3601, '.02')):
            with self.subTest(advance=advance, expected=expected):
                self.advance(advance)
                output = StringIO()
                with (mock.patch.object(aws_trial, 'AwsCli', return_value=api),
                      mock.patch.object(time, 'time', return_value=self.now), redirect_stdout(output)):
                    self.assertEqual(aws_trial.coordinate_trial(cohort, self.state, self.settings), 0)
                total = ledger.mapping(cast(object, json.loads(output.getvalue())))
                self.assertEqual(Decimal(str(total['total_cost_usd'])), Decimal(expected))
                self.assertEqual(Decimal(str(cloud_gate.read(cohort / 'query-cost.json')['usd'])), Decimal(expected))
                self.assertEqual(sum((row.query_usd for row in self.book.read_bills()), Decimal(0)), initial)

    def test_paired_cohort_daily_and_monthly_limits_rollback_before_launch(self) -> None:
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now, pause=self.advance)
        for limit in ('daily_limit_usd', 'monthly_limit_usd'):
            with self.subTest(limit=limit):
                numeric_settings(self.settings, {'daily_limit_usd': '20', 'monthly_limit_usd': '400', limit: '3.5'})
                requests = [{**self.request, 'attempt': limit + str(number)} for number in range(2)]
                with mock.patch.object(cloud_gate.GateWorker, 'charges'), self.assertRaises(GateRefused):
                    aws_trial.reserve_trial(worker, requests)
                self.assertEqual(self.fake.launches, [])
                self.assertTrue(all(isinstance(row.state, ledger.GateAbsent) for row in self.book.read_snapshot().gate_reservations))

    def test_paired_cohort_reserves_the_highest_regional_rate(self) -> None:
        reading = ledger.ChargeRead('2026-01-14', int(self.now), int(self.now) - 90000, int(self.now) - 3600,
            Decimal('.6'), Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.1'), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('USW2-SpotUsage:m7a.8xlarge', Decimal('.5'), Decimal(1), 'Hrs')))
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^instance_types=.*$', 'instance_types=m7a.8xlarge', path.read_text()))
        _ = path.write_text(re.sub(r'(?m)^(regions|spot_candidate_regions)=.*$',
                                  r'\1=us-east-2 us-west-2', path.read_text()))
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now)
        with (mock.patch.object(worker, 'reconcile'), mock.patch.object(worker, 'charges'),
              mock.patch.object(self.book, 'read_bills', return_value=(reading,)), redirect_stderr(StringIO()) as output):
            aws_trial.reserve_trial(worker, [self.request])
        reserved = self.book.read_snapshot().gate_reservations[0]
        self.assertEqual(reserved.rate.usd_per_hour, Decimal('.5'))
        self.assertEqual(reserved.region, '')
        estimate = ledger.box_cost(reserved.rate, 1200, read_settings(self.settings).box_minimum_seconds) + Decimal('.02')
        self.assertIn(f'Paired trial estimate: ${estimate};', output.getvalue())

    def test_paired_cohort_reserves_highest_type_rate_and_prices_actual_minimum(self) -> None:
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1, execute=self.fake), self.book,
            self.settings, self.folder / 'reservation', clock=lambda: self.now)
        reading = ledger.ChargeRead('2026-01-14', int(self.now), int(self.now) - 90000, int(self.now) - 3600,
            Decimal('2.8'), Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.8'), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal('2'), Decimal(1), 'Hrs')))
        with (mock.patch.object(cloud_gate.GateWorker, 'charges'),
              mock.patch.object(self.book, 'read_bills', return_value=(reading,))):
            aws_trial.reserve_trial(worker, [self.request])
            reserved = self.book.read_snapshot().gate_reservations[0]
            self.assertEqual(reserved.usd_per_hour, Decimal('0.8'))
            self.assertEqual(reserved.rate.usd_per_hour, Decimal(2))
            self.assertEqual(reserved.instance_type, 'm7a.4xlarge')
            self.book.resolve_gate('attempt', ledger.GateTerminated(int(self.now) + 1, 'i-fixture'), instance_type='m7a.8xlarge')
            cost, cleanup = aws_trial.trial_cost(self.book, {'attempt'}, self.settings, int(self.now) + 1)
        self.assertTrue(cleanup)
        self.assertEqual(cost, ledger.box_cost(ledger.BoxRate(Decimal('0.8'), 'actual'), 1,
            read_settings(self.settings).box_minimum_seconds) + Decimal('0.02'))

    def test_paired_coordinator_launches_modules_concurrently_and_keeps_json_order(self) -> None:
        import multiprocessing
        from contextlib import contextmanager, redirect_stdout
        barrier = multiprocessing.get_context('fork').Barrier(2)
        cohort = self.root / 'cohort'
        cohort.mkdir()
        cost_lock = self.state / 'paired-trial-slots' / 'controller.lock'
        cost_lock.parent.mkdir()
        for name in ('first', 'second'):
            folder = cohort / name
            folder.mkdir()
            request = {**self.request, 'attempt': name, 'task': 'paired', 'rounds': 1, 'module': name + '/test_ok.py',
                       'base': {'commit': 'base', 'tree': 'base-tree'}, 'changed': {'commit': 'commit', 'tree': 'tree'}}
            cloud_gate.save(folder / 'request.json', request)
            cloud_gate.save(folder / 'deployment.json', self.deployment)
        @contextmanager
        def slot(_repo: Path, sidecar: Path, job: StepJob, *,
                 group: CloudGroup) -> Generator[cloud_gate.CloudReservation]:
            self.assertEqual(job, aws_trial.PAIRED_TRIAL_JOB)
            self.assertEqual(group.key, cohort.name)
            with cost_lock.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            yield cloud_gate.CloudReservation(sidecar)
        def execute(worker: cloud_gate.GateWorker, folder: Path, request: dict[str, object],
                    _deployment: dict[str, object]) -> dict[str, object]:
            attempt = ledger.text(request, 'attempt')
            cloud_gate.save(folder / 'launch.json', {'instance': 'i-' + attempt, 'arguments': {'InstanceType': 'm7a.8xlarge'}})
            worker.book.resolve_gate(attempt, ledger.GateRunning('i-' + attempt), instance_type='m7a.8xlarge')
            _ = barrier.wait(timeout=TEST_HANG_GUARD_SECS)
            samples = [dict(module=request['module'], variant=variant, round=1, cpu_s=1.0, peak_mib=2.0,
                wall_s=1.0, status=0, tests=1, failures=0, errors=0, instance_type='m7a.8xlarge', box='i-' + attempt)
                for variant in ('base', 'changed')]
            row = {**request, 'runs': samples, 'verdict': 'pass', 'gate_s': 2.0}
            cloud_gate.save(folder / 'remote-result.json', row)
            worker.book.resolve_gate(attempt, ledger.GateTerminated(int(self.now) + 60, 'i-' + attempt))
            return {'status': 'complete', 'result': row}
        output = StringIO()
        with (mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
              mock.patch.object(cloud_gate.GateWorker, 'execute', new=execute),
              mock.patch.object(cloud_gate.GateWorker, 'charges'), mock.patch.object(cloud_gate.GateWorker, 'reconcile'),
              mock.patch.object(time, 'time', return_value=self.now), redirect_stdout(output)):
            self.assertEqual(aws_trial.coordinate_trial(cohort, self.state, self.settings), 0)
            first = output.getvalue()
            _ = output.seek(0)
            _ = output.truncate(0)
            self.assertEqual(aws_trial.coordinate_trial(cohort, self.state, self.settings), 0)
            self.assertEqual(output.getvalue().splitlines(), first.splitlines()[-3:])
        rows = [ledger.mapping(cast(object, json.loads(line))) for line in first.splitlines()]
        self.assertEqual(len(rows), 7)
        self.assertTrue(all('variant' in row for row in rows[:4]))
        self.assertTrue(all(row['all_passed'] for row in rows[4:6]))
        self.assertEqual(set(rows[-1]), {'total_cost_usd', 'cost_basis', 'cleanup_complete'})
        self.assertTrue(rows[-1]['cleanup_complete'])


    def test_paired_queue_wait_does_not_shorten_the_box_lifetime(self) -> None:
        cohort = self.root / 'waited'
        folder = cohort / 'waited'
        folder.mkdir(parents=True)
        # The request was written before a long wait for a cloud slot, so its first expiry is almost spent.
        request = {**self.request, 'attempt': 'waited', 'task': 'paired', 'rounds': 1, 'module': 'waited/test_ok.py',
                   'expires': int(self.now) + 60, 'base': {'commit': 'base', 'tree': 'base-tree'},
                   'changed': {'commit': 'commit', 'tree': 'tree'}}
        cloud_gate.save(folder / 'request.json', request)
        cloud_gate.save(folder / 'deployment.json', self.deployment)
        @contextmanager
        def slot(_repo: Path, sidecar: Path, job: StepJob, *,
                 group: CloudGroup) -> Generator[cloud_gate.CloudReservation]:
            self.assertEqual(job, aws_trial.PAIRED_TRIAL_JOB)
            self.assertEqual(group.key, cohort.name)
            yield cloud_gate.CloudReservation(sidecar)
        def execute(worker: cloud_gate.GateWorker, folder: Path, request: dict[str, object],
                    _deployment: dict[str, object]) -> dict[str, object]:
            cloud_gate.save(folder / 'launched-request.json', request)
            worker.book.resolve_gate('waited', ledger.GateAbsent(int(self.now)))
            return {'status': 'fallback', 'reason': 'fixture'}
        with (mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
              mock.patch.object(cloud_gate.GateWorker, 'execute', new=execute),
              mock.patch.object(cloud_gate.GateWorker, 'charges'), mock.patch.object(cloud_gate.GateWorker, 'reconcile'),
              mock.patch.object(time, 'time', return_value=self.now), redirect_stdout(StringIO())):
            _ = aws_trial.coordinate_trial(cohort, self.state, self.settings)
        expires = int(self.now) + cloud_gate.settings().whole('lifetime_minutes') * 60
        self.assertEqual(cloud_gate.read(folder / 'launched-request.json')['expires'], expires)
        self.assertEqual(cloud_gate.read(folder / 'request.json')['expires'], expires)
        self.assertEqual([row.expires for row in self.book.read_snapshot().gate_reservations], [expires])

    def test_paired_uncertain_cost_keeps_full_reservation_and_absent_cost_is_zero(self) -> None:
        reservation = ledger.GateReservation('uncertain', int(self.now), int(self.now) + 1200,
            Decimal('0.8'), Decimal('0.02'))
        _ = self.book.reserve_gate(self.settings, reservation)
        cost, complete = aws_trial.trial_cost(self.book, {'uncertain'}, self.settings, int(self.now) + 1)
        self.assertFalse(complete)
        self.assertEqual(cost, Decimal('0.8') / 3 + Decimal('0.02'))
        self.book.resolve_gate('uncertain', ledger.GateAbsent(int(self.now) + 2))
        self.assertEqual(aws_trial.trial_cost(self.book, {'uncertain'}, self.settings, int(self.now) + 2), (Decimal(0), True))

    def test_paired_coordinator_interrupt_and_kill_cleanup(self) -> None:
        script = r'''import faulthandler, multiprocessing, os, signal, sys, threading, time
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from scripts.aws_target import aws_trial, cloud_gate, ledger
from scripts.aws_target.aws_target import AwsCli
from scripts.aws_target.test_cloud_gate import FakeGateAws
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
folder, directory, settings = map(Path, sys.argv[1:4])
at, number, before_launch = float(sys.argv[4]), int(sys.argv[5]), bool(int(sys.argv[6]))
coordinator = os.getpid()
barrier = multiprocessing.get_context('fork').Barrier(2)
reserve_trial = aws_trial.reserve_trial
paired_module = aws_trial.paired_module
trace_at = float(sys.argv[7])

def reserve(*args, **kwargs):
    if before_launch:
        os.kill(coordinator, number)
    return reserve_trial(*args, **kwargs)

def trace_worker(folder, *args, **kwargs):
    with (folder / 'worker-stacks.log').open('w') as stacks:
        faulthandler.dump_traceback_later(max(0.001, trace_at - time.monotonic()), file=stacks)
        try:
            paired_module(folder, *args, **kwargs)
        finally:
            faulthandler.cancel_dump_traceback_later()

@contextmanager
def slot(repo, sidecar, job, *, group):
    assert job == aws_trial.PAIRED_TRIAL_JOB
    assert group.key == folder.name
    try:
        yield cloud_gate.CloudReservation(sidecar)
    finally:
        (repo / 'released').touch()
def create_api(*args, **kwargs):
    fake = FakeGateAws()
    fake.has_result = False
    def execute(arguments, **options):
        assert not before_launch or 'run-instances' not in arguments
        reply = fake(arguments, **options)
        if 'list-objects-v2' in arguments:
            turn = barrier.wait(timeout=TEST_HANG_GUARD_SECS)
            if turn == 0:
                os.kill(coordinator, number)
            if number != signal.SIGINT:
                os._exit(0)
            if not threading.Event().wait(TEST_HANG_GUARD_SECS):
                raise AssertionError('The coordinator did not stop the worker')
        return reply
    return AwsCli('fixture', 'us-east-1', 1, execute=execute)
with (mock.patch.object(aws_trial, 'AwsCli', side_effect=create_api),
      mock.patch.object(aws_trial, 'reserve_trial', side_effect=reserve),
      mock.patch.object(aws_trial, 'paired_module', side_effect=trace_worker),
      mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=slot),
      mock.patch.object(cloud_gate.GateWorker, 'charges'), mock.patch.object(cloud_gate.GateWorker, 'reconcile'),
      mock.patch.object(aws_trial.time, 'time', return_value=at)):
    raise SystemExit(aws_trial.coordinate_trial(folder, directory, settings))
'''
        for number, before_launch in ((signal.SIGKILL, True), (signal.SIGINT, False),
                                      (signal.SIGTERM, False), (signal.SIGKILL, False)):
            with self.subTest(number=number, before_launch=before_launch):
                cohort = self.root / f'cancel-cohort-{number}-{before_launch}'
                cohort.mkdir()
                attempts = (cohort.name + '-first', cohort.name + '-second')
                for name in attempts:
                    folder = cohort / name
                    folder.mkdir()
                    request = {**self.request, 'attempt': name, 'task': 'paired', 'rounds': 1, 'module': name + '/test_ok.py',
                               'base': {'commit': 'base', 'tree': 'base-tree'}, 'changed': {'commit': 'commit', 'tree': 'tree'}}
                    cloud_gate.save(folder / 'request.json', request)
                    cloud_gate.save(folder / 'deployment.json', self.deployment)
                result = self.watchdog.run_trial([sys.executable, '-c', script, str(cohort), str(self.state),
                    str(self.settings), str(self.now), str(number), str(int(before_launch))],
                    tuple(cohort / name / 'worker-stacks.log' for name in attempts))
                stdout, stderr = result.stdout, result.stderr
                self.assertEqual(result.returncode, 1 if number == signal.SIGINT else 128 + number, stderr)
                if before_launch:
                    for name in attempts:
                        self.assertTrue((cohort / name / 'released').is_file(), stderr)
                        self.assertFalse((cohort / name / 'launch.json').exists())
                    continue
                api = AwsCli('fixture', 'us-east-1', 1, execute=FakeGateAws())
                with mock.patch.object(aws_trial, 'AwsCli', return_value=api):
                    self.assertEqual(aws_trial.main(['--paired-cleanup', str(cohort)], self.settings, self.state), 0)
                reservations = [row for row in self.book.read_snapshot().gate_reservations
                                if row.attempt in attempts]
                self.assertEqual(len(reservations), 2)
                self.assertTrue(all(isinstance(row.state, ledger.GateTerminated) for row in reservations),
                    repr(reservations) + stderr + repr([cloud_gate.read(path) for path in cohort.glob('*/outcome.json')]))
                rows = [ledger.mapping(cast(object, json.loads(line))) for line in stdout.splitlines()]
                if number == signal.SIGINT:
                    self.assertEqual(len(rows), 3)
                    self.assertTrue(rows[-1]['cleanup_complete'])
                    self.assertTrue(all(row['all_passed'] is False for row in rows[:-1]))
                for path in cohort.glob('*/*.json'):
                    if path.name not in ('request.json', 'deployment.json'):
                        path.unlink()


class CloudGateWatchdogTests(CostFixture):
    def test_fixture_watchdog_reports_and_stops_a_blocked_trial(self) -> None:
        worker = r'''import faulthandler, sys, threading

def wait_for_watchdog():
    with open(sys.argv[1], 'w') as stacks:
        faulthandler.dump_traceback(file=stacks)
    threading.Event().wait()

wait_for_watchdog()
'''
        watchdog = CloudGateFixtureWatchdog('blocked trial', limit_seconds=2.0)
        stacks = self.root / 'worker-stacks.log'
        stacks.touch()
        with (TemporaryFile() as report,
              mock.patch.object(sys.modules[__name__], 'send_process_group', wraps=send_process_group) as stop,
              watchdog.trial([sys.executable, '-c', worker, str(stacks)], reap_timeout=WATCHDOG_RESULT_TIMEOUT_SECS,
                             worker_stacks=(stacks,)) as (process, _),
              selectors.DefaultSelector() as stdout_ready):
            assert process.stdout is not None
            _ = stdout_ready.register(process.stdout, selectors.EVENT_READ)
            try:
                wait_until(lambda: 'in wait_for_watchdog' in stacks.read_text(), self.root,
                           WATCHDOG_RESULT_TIMEOUT_SECS, watch_writes=True)
            except TimeoutError:
                self.fail('The blocked worker did not write its stack')
            with mock.patch.object(os, '_exit') as exit_process, watchdog.watch(report_fd=report.fileno()):
                self.assertTrue(stdout_ready.select(WATCHDOG_RESULT_TIMEOUT_SECS), 'The worker kept its pipe open')
                self.assertEqual(os.read(process.stdout.fileno(), 1), b'')
            exit_process.assert_called_once_with(1)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            _ = report.seek(0)
            output = report.read().decode(errors='replace')
            self.assertIn('cloud gate fixture watchdog expired', output)
            self.assertIn('in wait_for_watchdog', output)
        stop.assert_called_once_with(process.pid, signal.SIGKILL)


class CloudRouteTests(CloudGateHangGuard, CostFixture):
    repo: Path = Path()
    modules: list[tuple[str, Path]] = []
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.settings.write_text(self.settings.read_text().replace('region=us-east-1', 'region=us-east-2'))
        _ = self.enterContext(mock.patch.object(cloud_gate, 'account_settings_path', side_effect=lambda: Path(os.environ.get('AWS_TARGET_CONFIG', str(self.settings)))))
        override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=on')
        override_fixture_settings(self.root / 'cloud_gate.conf',
            'credential_file=~/.config/aws-build/credentials\n')
        self.repo = self.root / 'repo'
        folder = self.repo / 'scripts/fixture'
        folder.mkdir(parents=True)
        for name in ('test_one.py', 'test_two.py'):
            _ = (folder / name).write_text('import unittest\n')
        for args in (('init', '-q', '-b', 'fixture'), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'), ('add', '.'), ('commit', '-qm', 'Fixture'),
                     ('remote', 'add', 'origin', '/private/origin'), ('update-ref', 'refs/remotes/origin/main', 'HEAD')):
            _ = gate.checked_git(self.repo, *args)
        self.modules = [('fixture', path) for path in sorted(folder.glob('test_*.py'))]
        _ = self.enterContext(mock.patch.object(cloud_gate, 'discover_test_modules', return_value=self.modules))
        _ = self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state))
        self.admission = caller_admission(self, self.root, {'BUILDLOG_DIR': str(self.root / 'buildlog')})

    def retry_fixture(self) -> CloudRouteTests:
        fixture = CloudRouteTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def run_spot_route(self, interruptions: int, *, box_failures: int = 0, log_error: str = '',
                       status: int = 0, one_choice: bool = False, limit_after_first: bool = False,
                       disable_after_first: bool = False,
                       crash_after: int | None = None,
                       crash_on_retry_save: bool = False,
                       lower_limit_on_resume: bool = False) -> tuple[cloud_gate.LocalFallback | cloud_gate.CloudCompleted,
                                                               list[Path], list[FakeGateAws]]:
        values = cloud_gate.settings()
        size = values.words('instance_types')[0]
        text_setting(self.root / 'cloud_gate.conf', 'instance_types', size)
        regions = values.words('regions')
        deployment: dict[str, object] = {'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'revision': 'fixture-revision',
            'regions': [{'region': region, 'template': 'fixture-template', 'version': '1', 'image': 'fixture-image',
                'security_group': 'fixture-group', 'subnets': [{'id': region + suffix, 'zone': region + suffix}
                    for suffix in (('a',) if one_choice else ('a', 'b'))]}
                for region in (regions[:1] if one_choice else regions)]}
        cloud_gate.save(self.state / 'gate-deployment.json', deployment)
        book = ledger.CostLedger(self.state)
        now = time.time()
        book.record_inventory(ledger.ResourceInventory(int(now), (), 'fixture', builder_removed=True))
        folders: list[Path] = []
        fakes: list[FakeGateAws] = []
        sidecars: dict[int, list[Path]] = {}
        admissions: dict[int, list[object]] = {}
        controller = 0
        crashed = False
        save = cloud_gate.save
        def save_state(path: Path, row: dict[str, object]) -> None:
            nonlocal crashed
            save(path, row)
            if ((crash_on_retry_save or lower_limit_on_resume and row.get('counted_interruption') != row.get('attempt'))
                    and not crashed and path.name == 'current.json'
                    and int(str(row.get('spot_interruptions', 0))) > 0):
                crashed = True
                raise SystemExit('fixture controller crash')
        def service(command: Sequence[str]) -> int:
            nonlocal crashed
            folder = Path(command[command.index('--worker') + 1])
            sidecar = Path(command[command.index('--sidecar') + 1])
            active = self.admission.caller_reservations()
            self.assertEqual(len(active), 1)
            admissions.setdefault(controller, []).append(next(iter(active)))
            sidecars.setdefault(controller, []).append(sidecar)
            self.assertEqual(sidecar, sidecars[controller][0])
            self.assertTrue(all(fake.terminated for fake in fakes))
            request = cloud_gate.read(folder / 'request.json')
            fake = FakeGateAws()
            failed_box = len(folders) < box_failures
            fake.has_result = len(folders) >= box_failures + interruptions or (failed_box and bool(log_error))
            if failed_box and not log_error:
                fake.boot_error = 'The box failed at stage result upload (exit 1)'
            fake.state = 'running' if fake.has_result or fake.boot_error else 'shutting-down'
            fake.state_reason = {'Code': 'Server.SpotInstanceTermination'}
            run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, status, 1, '') for _, path in self.modules), 1, 1)
            row: dict[str, object] = {key: request[key] for key in ('attempt', 'commit', 'tree')}
            row.update(host='fixture-guest', verdict='pass' if run.passed else 'fail', gate=gate_box.encode_run(run), gate_s=1,
                record=gate_box.completion_record(run, ledger.text(request, 'attempt'), ledger.text(request, 'commit'),
                    ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, ledger.text(request, 'mode')))
            if failed_box and log_error:
                fake.has_log = False
                row['log_error'] = log_error
            fake.result = row
            worker = cloud_gate.GateWorker(AwsCli('fixture-scaler', regions[0], 1, execute=fake), book,
                self.settings, sidecar, clock=lambda: now)
            with mock.patch.object(worker, 'charges') as charges:
                result = worker.execute(folder, request, deployment)
                charges.assert_called_once()
            cloud_gate.save(folder / 'outcome.json', result)
            folders.append(folder)
            fakes.append(fake)
            if limit_after_first:
                self.costs({'daily_limit_usd': '0'})
            if disable_after_first:
                override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=off')
            if len(folders) == crash_after:
                crashed = True
                raise SystemExit('fixture controller crash')
            return 0
        with (mock.patch.object(cloud_gate, 'AwsCli', side_effect=AssertionError('The session must not call AWS')),
              mock.patch.object(store, 'host_file', return_value=self.root / 'remote-record.jsonl'),
              mock.patch.object(cloud_gate, 'save', side_effect=save_state),
              redirect_stdout(StringIO())):
            if crash_after is not None or crash_on_retry_save or lower_limit_on_resume:
                with self.assertRaisesRegex(SystemExit, 'fixture controller crash'):
                    _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                        module_time_limit_s=values.number('module_time_limit_s'), service=service)
                self.assertTrue(self.admission.callers_released())
                if lower_limit_on_resume:
                    path = self.root / 'cloud_gate.conf'
                    limit = max(0, cloud_gate.settings().whole('spot_interruption_retries') - 1)
                    _ = path.write_text(re.sub(r'(?m)^spot_interruption_retries=.*$',
                        'spot_interruption_retries=' + str(limit), path.read_text()))
                controller += 1
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                module_time_limit_s=values.number('module_time_limit_s'), service=service)
        self.assertTrue(all(len(set(rows)) == 1 for rows in admissions.values()))
        self.assertTrue(self.admission.callers_released())
        reservations = book.read_snapshot().gate_reservations
        self.assertTrue(all(isinstance(row.state, ledger.GateTerminated) for row in reservations))
        self.assertEqual(len(reservations), sum(bool(fake.launches) for fake in fakes))
        return outcome, folders, fakes

    def test_spot_interruption_retries_the_next_zone_then_succeeds(self) -> None:
        self.assertGreater(cloud_gate.settings().whole('spot_interruption_retries'), 0)
        for interruptions, box_failures, log_error in ((1, 0, ''), (0, 1, ''), (0, 1, 'The log PUT timed out')):
            with self.subTest(interruptions=interruptions, box_failures=box_failures, log_error=log_error):
                fixture = self.retry_fixture() if box_failures else self
                outcome, folders, fakes = fixture.run_spot_route(
                    interruptions, box_failures=box_failures, log_error=log_error)
                self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
                self.assertEqual(len(folders), 2)
                first = cloud_gate.read(folders[0] / 'launch.json')
                second = cloud_gate.read(folders[1] / 'launch.json')
                self.assertEqual(second['variants'], first['variants'])
                self.assertEqual(second['variant'], ledger.instant(first, 'variant') + 1)
                self.assertNotEqual(second['zone'], first['zone'])
                self.assertEqual(second['region'], first['region'])
                self.assertEqual(len({folder.name for folder in folders}), len(folders))
                self.assertNotEqual(fakes[0].launches[0]['ClientToken'], fakes[1].launches[0]['ClientToken'])
                self.assertEqual(fixture.fallback_rows(), [])
                if box_failures:
                    reason = 'The final log upload failed' if log_error else fakes[0].boot_error
                    self.assertEqual(cloud_gate.read(folders[0] / 'outcome.json'),
                                     {'status': 'box-failed', 'reason': reason})
                if fakes[0].boot_error:
                    calls = fakes[0].commands
                    self.assertEqual(sum('list-objects-v2' in call for call in calls), 1)
                    self.assertEqual([call[call.index('--key') + 1] for call in calls if 'get-object' in call],
                                     [f'artifacts/{folders[0].name}/{name}' for name in ('boot-error.json', 'gate.log')])
                    self.assertEqual((folders[0] / 'gate.log').read_text(), fakes[0].log)

    def test_spot_interruption_limit_records_one_final_fallback(self) -> None:
        retries = cloud_gate.settings().whole('spot_interruption_retries')
        expected = retries + 1
        outcome, folders, _ = self.run_spot_route(expected)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        assert isinstance(outcome, cloud_gate.LocalFallback)
        self.assertEqual(len(folders), expected)
        self.assertIn(f'Spot interruption on {expected} attempts', outcome.reason)
        rows = self.fallback_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reason'], outcome.reason)
        launches = [cloud_gate.read(folder / 'launch.json') for folder in folders]
        self.assertEqual([row['variant'] for row in launches], list(range(expected)))
        if expected > 2:
            self.assertNotEqual(launches[1]['region'], launches[2]['region'])

        signed_url = 'https://fixture.invalid/gate.log?X-Amz-Signature=fixture'
        for log_error in ('', f'curl PUT timed out for {signed_url}'):
            with self.subTest(box_failures=2, log_error=log_error):
                fixture = self.retry_fixture()
                outcome, folders, fakes = fixture.run_spot_route(0, box_failures=2, log_error=log_error, crash_after=2)
                self.assertEqual(len(folders), 2)
                reason = 'The final log upload failed' if log_error else fakes[-1].boot_error
                fallback_reason = f'The box failure retry limit is reached: {reason}'
                self.assertEqual(outcome, cloud_gate.LocalFallback(fallback_reason))
                for folder in folders:
                    self.assertEqual(cloud_gate.read(folder / 'outcome.json'), {'status': 'box-failed', 'reason': reason})
                current = cloud_gate.read(folders[-1].parent / 'current.json')
                self.assertEqual(current['box_failures'], 2)
                self.assertEqual(current['spot_interruptions'], 0)
                rows = fixture.fallback_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['reason'], fallback_reason)
                self.assertNotIn(signed_url, (fixture.state / 'cloud-gate-launches.jsonl').read_text())
                service = mock.Mock(side_effect=AssertionError('The box failure retry limit is reached'))
                resumed = cloud_gate.run(fixture.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                    module_time_limit_s=cloud_gate.settings().number('module_time_limit_s'), service=service)
                self.assertEqual(resumed, outcome)
                service.assert_not_called()

    def test_spot_interruption_without_another_choice_falls_back(self) -> None:
        outcome, folders, _ = self.run_spot_route(1, one_choice=True)
        self.assertEqual(len(folders), 1)
        self.assertEqual(outcome, cloud_gate.LocalFallback('Spot interruption on 1 attempts; No cloud capacity choice remains'))

    def test_switch_off_after_a_spot_interruption_skips_the_next_service(self) -> None:
        outcome, folders, fakes = self.run_spot_route(1, disable_after_first=True)
        self.assertEqual(outcome, cloud_gate.LocalFallback('Spot interruption on 1 attempts; ' + cloud_gate.CLOUD_GATE_OFF))
        self.assertEqual(len(folders), 1)
        self.assertEqual(len(fakes[0].launches), 1)
        self.assertTrue(fakes[0].terminated)

    def test_zero_spot_retries_keeps_the_first_interruption_as_the_limit(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_interruption_retries=.*$', 'spot_interruption_retries=0', path.read_text()))
        expected = cloud_gate.settings().whole('spot_interruption_retries') + 1
        outcome, folders, _ = self.run_spot_route(expected)
        self.assertEqual(len(folders), expected)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        assert isinstance(outcome, cloud_gate.LocalFallback)
        self.assertIn(f'Spot interruption on {expected} attempts', outcome.reason)

        with self.subTest(box_failures=1, interruptions=1):
            fixture = self.retry_fixture()
            path = fixture.root / 'cloud_gate.conf'
            _ = path.write_text(re.sub(r'(?m)^spot_interruption_retries=.*$', 'spot_interruption_retries=0',
                                       path.read_text()))
            outcome, folders, _ = fixture.run_spot_route(1, box_failures=1)
            self.assertEqual(len(folders), 2)
            self.assertEqual(outcome, cloud_gate.LocalFallback(
                'Spot interruption on 1 attempts; The Spot interruption retry limit is reached'))
            current = cloud_gate.read(folders[-1].parent / 'current.json')
            self.assertEqual(current['box_failures'], 1)
            self.assertEqual(current['spot_interruptions'], 1)

    def test_spot_retry_checks_the_spend_limit_again(self) -> None:
        outcome, folders, fakes = self.run_spot_route(1, limit_after_first=True)
        self.assertEqual(len(folders), 2)
        self.assertEqual(outcome, cloud_gate.LocalFallback('Spot interruption on 1 attempts; daily_limit'))
        self.assertEqual(fakes[1].launches, [])
        self.assertFalse((folders[1] / 'launch.json').exists())

    def test_normal_gate_failure_does_not_retry(self) -> None:
        outcome, folders, _ = self.run_spot_route(0, status=1)
        self.assertEqual(len(folders), 1)
        self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
        assert isinstance(outcome, cloud_gate.CloudCompleted)
        self.assertFalse(outcome.run.passed)
        self.assertEqual(self.fallback_rows(), [])

    def test_spot_retry_budget_survives_a_crash_after_the_last_interruption(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_interruption_retries=.*$', 'spot_interruption_retries=1', path.read_text()))
        retries = cloud_gate.settings().whole('spot_interruption_retries')
        expected = retries + 1
        outcome, folders, _ = self.run_spot_route(expected, crash_after=expected)
        self.assertEqual([cloud_gate.read(folder / 'launch.json')['variant'] for folder in folders], list(range(expected)))
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        assert isinstance(outcome, cloud_gate.LocalFallback)
        self.assertIn(f'Spot interruption on {expected} attempts', outcome.reason)
        self.assertEqual(self.fallback_rows()[-1]['reason'], outcome.reason)
        service = mock.Mock(side_effect=AssertionError('The retry budget is spent'))
        resumed = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
            module_time_limit_s=cloud_gate.settings().number('module_time_limit_s'), service=service)
        self.assertEqual(resumed, outcome)
        service.assert_not_called()
        current = cloud_gate.read(folders[-1].parent / 'current.json')
        self.assertEqual(current['spot_interruptions'], expected)
        self.assertEqual(ledger.mapping(current['retry_launch'])['variant'], expected)

    def test_spot_retry_resumes_the_next_choice_after_the_first_interruption(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_interruption_retries=.*$', 'spot_interruption_retries=1', path.read_text()))
        expected = cloud_gate.settings().whole('spot_interruption_retries') + 1
        outcome, folders, _ = self.run_spot_route(expected, crash_after=1)
        self.assertEqual([cloud_gate.read(folder / 'launch.json')['variant'] for folder in folders], list(range(expected)))
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        assert isinstance(outcome, cloud_gate.LocalFallback)
        self.assertIn(f'Spot interruption on {expected} attempts', outcome.reason)
        self.assertEqual(len(self.fallback_rows()), 1)

    def test_spot_retry_count_does_not_repeat_after_the_state_save(self) -> None:
        expected = cloud_gate.settings().whole('spot_interruption_retries') + 1
        outcome, folders, _ = self.run_spot_route(expected, crash_on_retry_save=True)
        self.assertEqual([cloud_gate.read(folder / 'launch.json')['variant'] for folder in folders], list(range(expected)))
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        assert isinstance(outcome, cloud_gate.LocalFallback)
        self.assertIn(f'Spot interruption on {expected} attempts', outcome.reason)

    def test_spot_retry_checks_a_lower_limit_before_preparation_on_resume(self) -> None:
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^spot_interruption_retries=.*$', 'spot_interruption_retries=1', path.read_text()))
        interruptions = cloud_gate.settings().whole('spot_interruption_retries')
        outcome, folders, fakes = self.run_spot_route(interruptions, lower_limit_on_resume=True)
        self.assertEqual(cloud_gate.settings().whole('spot_interruption_retries'), interruptions - 1)
        self.assertEqual(len(folders), interruptions)
        self.assertEqual(sum(len(fake.launches) for fake in fakes), interruptions)
        self.assertEqual(outcome, cloud_gate.LocalFallback(
            f'Spot interruption on {interruptions} attempts; The Spot interruption retry limit is reached'))
        current = cloud_gate.read(folders[0].parent / 'current.json')
        pending = folders[0].parent / ledger.text(current, 'attempt')
        self.assertNotEqual(pending, folders[0])
        self.assertFalse((pending / 'request.json').exists())
        self.assertFalse((pending / 'launch.json').exists())

    def test_main_commit_needs_no_bundle(self) -> None:
        override_fixture_settings(self.root / 'cloud_gate.conf',
            'clone_url=https://example.invalid/fixture-source.git\n')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 1800)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(request['commit'], gate.checked_git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(request['head'], request['commit'])
        self.assertEqual(request['origin_main'], request['commit'])
        self.assertEqual(request['clone_url'], 'https://example.invalid/fixture-source.git')
        self.assertEqual(request['source_key'], '')
        self.assertFalse((folder / 'source.bundle').exists())
        self.assertEqual(request['modules'], ['scripts/fixture/test_one.py', 'scripts/fixture/test_two.py'])

    def test_bundle_contains_only_the_commit_ahead_of_main(self) -> None:
        base = gate.checked_git(self.repo, 'rev-parse', 'refs/remotes/origin/main')
        _ = (self.repo / 'new-file').write_text('one new commit\n')
        _ = gate.checked_git(self.repo, 'add', 'new-file')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'New commit')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 1800)
        bundle = folder / 'source.bundle'
        header = bundle.read_bytes().split(b'\n\n', 1)[0].decode()
        self.assertIn('-' + base + ' ', header)
        self.assertEqual(request['origin_main'], base)
        refs = gate.checked_git(self.repo, 'bundle', 'list-heads', str(bundle))
        self.assertEqual(refs.splitlines(), [str(request['commit']) + ' refs/cloud-gates/attempt'])
        pack = bundle.read_bytes().split(b'\n\n', 1)[1]
        indexed = subprocess.run(['git', '-C', str(self.repo), 'index-pack', '--stdin', '--fix-thin'],
            input=pack, check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        pack_hash = indexed.stdout.decode().split()[-1]
        objects = gate.checked_git(self.repo, 'verify-pack', '-v', str(self.repo / '.git/objects/pack' / ('pack-' + pack_hash + '.idx')))
        commits = [line.split()[0] for line in objects.splitlines() if len(line.split()) > 1 and line.split()[1] == 'commit']
        self.assertEqual(commits, [request['commit']])

    def test_snapshot_preserves_checkout_and_fetches_into_clone(self) -> None:
        _ = (self.repo / '.gitignore').write_text('ignored\n')
        _ = gate.checked_git(self.repo, 'add', '.gitignore')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Ignore fixture output')
        _ = gate.checked_git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        clone = self.root / 'clone'
        _ = gate.checked_git(self.repo, 'clone', '-q', str(self.repo), str(clone))
        _ = self.modules[0][1].write_text('tracked edit\n')
        _ = self.modules[1][1].write_text('staged edit\n')
        _ = gate.checked_git(self.repo, 'add', str(self.modules[1][1]))
        _ = (self.repo / 'untracked').write_text('untracked file\n')
        _ = (self.repo / 'ignored').write_text('ignored file\n')
        status = gate.checked_git(self.repo, 'status', '--porcelain')
        index = (self.repo / '.git/index').read_bytes()
        head = gate.checked_git(self.repo, 'rev-parse', 'HEAD')
        refs = gate.checked_git(self.repo, 'for-each-ref')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 1800)
        commit = ledger.text(request, 'commit')
        self.assertNotEqual(commit, head)
        self.assertEqual(request['head'], head)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', commit + '^{tree}'))
        self.assertEqual(gate.checked_git(self.repo, 'rev-parse', commit + '^'), head)
        for name, content in (('scripts/fixture/test_one.py', 'tracked edit'),
                              ('scripts/fixture/test_two.py', 'staged edit'), ('untracked', 'untracked file')):
            self.assertEqual(gate.checked_git(self.repo, 'show', commit + ':' + name), content)
        self.assertNotIn('ignored', gate.checked_git(self.repo, 'ls-tree', '--name-only', commit).splitlines())
        self.assertEqual(gate.checked_git(self.repo, 'status', '--porcelain'), status)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual(gate.checked_git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(gate.checked_git(self.repo, 'for-each-ref'), refs)
        _ = gate.checked_git(clone, 'fetch', str(folder / 'source.bundle'), commit)
        _ = gate.checked_git(clone, 'checkout', '--detach', commit)
        self.assertEqual(gate.checked_git(clone, 'rev-parse', 'HEAD^{tree}'), request['tree'])
        self.assertEqual((clone / 'untracked').read_text(), 'untracked file\n')

    def test_working_tree_reads_a_same_size_edit_at_the_index_mtime(self) -> None:
        _ = gate.checked_git(self.repo, 'config', 'core.trustctime', 'false')
        source = self.repo / 'racy'
        _ = source.write_text('before\n')
        timestamp = 1_600_000_000_000_000_000
        os.utime(source, ns=(timestamp, timestamp))
        _ = gate.checked_git(self.repo, 'add', 'racy')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Add the racy file')
        index = Path(gate.checked_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        os.utime(index, ns=(timestamp, timestamp))
        _ = source.write_text('edited\n')
        index_mtime = index.stat().st_mtime_ns
        os.utime(source, ns=(index_mtime, index_mtime))
        environment = {**os.environ, 'GIT_INDEX_FILE': str(self.root / 'expected-index')}
        for arguments in (('read-tree', 'HEAD'), ('add', '-A')):
            _ = subprocess.run(['git', *arguments], cwd=self.repo, env=environment,
                               check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        expected = subprocess.run(['git', 'write-tree'], cwd=self.repo, env=environment,
                                  check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        self.assertNotEqual(expected, gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        tree, dirty = cloud_gate.working_tree(self.repo)
        self.assertIn('racy', dirty)
        self.assertEqual(tree, expected)

    def test_targeted_selection_runs_locally_and_sends_only_selected_paths(self) -> None:
        for dirty, paths, later in ((False, ('test_one.py',), ''), (True, ('test_one.py',), ''),
                                    (False, ('test_two.py',), ''), (True, (), ''),
                                    (False, ('test_one.py',), 'edit'), (False, ('test_one.py',), 'commit')):
            selection = affected_modules.AffectedSelection(
                affected_modules.CertifiedBase('base', '2026-01-15T00:00:00+00:00'),
                ('scripts/fixture/test_one.py',), tuple(self.repo / 'scripts/fixture' / name for name in paths))
            with self.subTest(dirty=dirty, paths=paths, later=later):
                _ = gate.checked_git(self.repo, 'reset', '--hard', 'HEAD')
                if dirty:
                    _ = self.modules[0][1].write_text('import os\n')
                expected_tree = gate.clean_test_tree(self.repo)
                with (mock.patch.object(gate.PassedTreeLedger, 'certified_base', autospec=True,
                                        return_value=selection.base) as certified,
                      mock.patch.object(gate, 'select_affected', return_value=selection) as select,
                      redirect_stdout(StringIO())):
                    chosen, book = gate.select_targeted_modules(self.repo, expected_tree, self.modules)
                    self.assertEqual(book.tree, expected_tree)
                    self.assertIsInstance(chosen, affected_modules.AffectedSelection)
                    assert isinstance(chosen, affected_modules.AffectedSelection)
                    selected = gate.TreeSelection(tuple(str(path.relative_to(self.repo)) for path in chosen.modules),
                                                  cloud_gate.working_tree(self.repo)[0])
                    self.assertEqual(select.call_count, 1)
                    if later:
                        _ = self.modules[1][1].write_text('import sys\n')
                        if later == 'commit':
                            _ = gate.checked_git(self.repo, 'add', '.')
                            _ = gate.checked_git(self.repo, 'commit', '-qm', 'Change the selected tree')
                        folder = self.root / ('refused-' + later)
                        with (mock.patch.object(cloud_gate, 'save') as save,
                              mock.patch.object(gate, 'checked_git', wraps=gate.checked_git) as git):
                            with self.assertRaisesRegex(GateRefused, 'The checkout changed after the test selection'):
                                _ = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.TARGETED_TEST, 1800,
                                                       selected=selected)
                        save.assert_not_called()
                        self.assertFalse((folder / 'request.json').exists())
                        self.assertFalse(any('bundle' in call.args for call in git.call_args_list))
                        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
                        with mock.patch.object(cloud_gate, 'run_service', side_effect=AssertionError('No service may start')) as service:
                            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                                     module_time_limit_s=1800, selected=selected)
                        self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed after the test selection'))
                        service.assert_not_called()
                        continue
                    request = cloud_gate.prepare(self.repo, self.root / 'attempt', gate.SuiteMode.TARGETED_TEST, 1800,
                                                selected=selected)
                    self.assertEqual(select.call_count, 1)
                self.assertEqual(select.call_args.args[0], self.repo)
                self.assertEqual(select.call_args.args[3], affected_modules.dirty_paths(self.repo))
                self.assertEqual(cast(gate.PassedTreeLedger, certified.call_args.args[0]).tree, expected_tree)
                self.assertEqual(request['modules'], ['scripts/fixture/' + name for name in paths])

    def test_retry_uses_a_new_attempt_when_the_booking_closed_without_an_outcome(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        folders: list[Path] = []
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        book.record_inventory(ledger.ResourceInventory(now, (ledger.PricedResource('fixture', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(now, 'stopped', 'fixture'))
        def service(command: Sequence[str]) -> int:
            folder = Path(command[command.index('--worker') + 1])
            folders.append(folder)
            request = cloud_gate.read(folder / 'request.json')
            _ = book.reserve_gate(self.settings, ledger.GateReservation(
                folder.name, now, ledger.instant(request, 'expires'), Decimal('.8'), Decimal('.02')))
            book.resolve_gate(folder.name, ledger.GateAbsent(now))
            return 0
        for _ in range(2):
            _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                               module_time_limit_s=1800, service=service)
        self.assertNotEqual(folders[0], folders[1])
        self.assertFalse(any((folder / 'outcome.json').exists() for folder in folders))

    def _run_gate_for_resume(self, service: Callable[[Sequence[str]], int]) -> cloud_gate.LocalFallback | cloud_gate.CloudCompleted:
        with mock.patch.object(cloud_gate, 'AwsCli', side_effect=AssertionError('The controller must not call AWS')):
            return cloud_gate.run_cloud(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                module_time_limit_s=1800, service=service, attempt=cloud_gate.CloudAttempt(self.state, gate.SuiteMode.MERGE_GATE))

    def _finish_gate_for_resume(self, command: Sequence[str], *, status: int = 0) -> int:
        folder = Path(command[command.index('--worker') + 1])
        request = cloud_gate.read(folder / 'request.json')
        run = gate.GateRun(tuple(gate.GateJobResult(suite, path.name, status, 1, '') for suite, path in self.modules), 1, 1)
        result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
        result.update(host='fixture-guest', verdict='pass' if run.passed else 'fail', gate=gate_box.encode_run(run),
            record=gate_box.completion_record(run, folder.name, ledger.text(request, 'commit'),
                ledger.text(request, 'tree'), 'fixture-guest', 0, 1, 'merge-gate'))
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        booked = book.reserve_gate(self.settings, ledger.GateReservation(
            folder.name, now, ledger.instant(request, 'expires'), Decimal('.8'), Decimal('.02')))
        self.assertIsInstance(booked, ledger.GateReserved)
        book.resolve_gate(folder.name, ledger.GateTerminated(now, 'fixture-guest'))
        cloud_gate.save(folder / cloud_gate.RESULT_FILE, result)
        _ = (folder / 'gate.log').write_text('fixture gate log\n')
        cloud_gate.save(folder / 'outcome.json', {'status': 'complete', 'result_file': cloud_gate.RESULT_FILE})
        return 0

    def _unimported_gate(self, *, status: int = 0) -> Path:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        book.record_inventory(ledger.ResourceInventory(now, (), 'fixture', builder_removed=True))

        def interrupt_after_completion(command: Sequence[str]) -> int:
            _ = self._finish_gate_for_resume(command, status=status)
            raise SystemExit('fixture controller crash')

        with self.assertRaisesRegex(SystemExit, 'fixture controller crash'):
            _ = self._run_gate_for_resume(interrupt_after_completion)
        root = Path(gate.checked_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
        return root / ledger.text(cloud_gate.read(root / 'current.json'), 'attempt')

    def test_complete_unimported_gate_resumes_without_a_service(self) -> None:
        folder = self._unimported_gate(status=1)
        (self.state / 'gate-deployment.json').unlink()
        service = mock.Mock(side_effect=AssertionError('A completed gate needs no service'))
        with (mock.patch.object(cloud_gate, 'service_command', side_effect=AssertionError('A completed gate needs no command')),
              mock.patch.object(queue_place, 'release_place', wraps=queue_place.release_place) as release,
              redirect_stdout(StringIO()) as output):
            outcome = self._run_gate_for_resume(service)

        assert isinstance(outcome, cloud_gate.CloudCompleted)
        self.assertFalse(outcome.run.passed)
        self.assertIn('fixture gate log', output.getvalue())
        service.assert_not_called()
        self.assertEqual(release.call_args.args[1], 1)
        self.assertTrue(self.admission.callers_released())
        self.assertEqual([path for path in folder.parent.iterdir() if path.is_dir()], [folder])
        self.assertEqual([row['id'] for row in store.locked_rows(store.host_file('aws', 0))], ['gate-' + folder.name])
        self.assertEqual(cloud_gate.read(folder / 'imported.json')['id'], 'gate-' + folder.name)

    def test_complete_imported_gate_starts_a_new_attempt(self) -> None:
        folder = self._unimported_gate()
        cloud_gate.import_record(folder, cloud_gate.read(folder / cloud_gate.RESULT_FILE))
        self.assertFalse((folder / cloud_gate.RESULT_FILE).exists())

        with redirect_stdout(StringIO()):
            outcome = self._run_gate_for_resume(self._finish_gate_for_resume)

        self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
        current = cloud_gate.read(folder.parent / 'current.json')
        self.assertNotEqual(current['attempt'], folder.name)
        self.assertEqual([row['id'] for row in store.locked_rows(store.host_file('aws', 0))],
                         ['gate-' + folder.name, 'gate-' + ledger.text(current, 'attempt')])

    def test_complete_gate_for_a_different_tree_starts_a_new_attempt(self) -> None:
        folder = self._unimported_gate()
        request = cloud_gate.read(folder / 'request.json')
        result = cloud_gate.read(folder / cloud_gate.RESULT_FILE)
        request['tree'] = result['tree'] = 'different-tree'
        ledger.mapping(result['record'])['tree'] = 'different-tree'
        cloud_gate.save(folder / 'request.json', request)
        cloud_gate.save(folder / cloud_gate.RESULT_FILE, result)

        service = mock.Mock(wraps=self._finish_gate_for_resume)
        with redirect_stdout(StringIO()):
            outcome = self._run_gate_for_resume(service)

        self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
        current = cloud_gate.read(folder.parent / 'current.json')
        self.assertNotEqual(current['attempt'], folder.name)
        service.assert_called_once()
        self.assertFalse((folder / 'imported.json').exists())
        self.assertEqual([row['id'] for row in store.locked_rows(store.host_file('aws', 0))],
                         ['gate-' + ledger.text(current, 'attempt')])

    def test_complete_gate_with_a_missing_result_starts_a_new_attempt(self) -> None:
        folder = self._unimported_gate()
        (folder / cloud_gate.RESULT_FILE).unlink()

        service = mock.Mock(wraps=self._finish_gate_for_resume)
        with redirect_stdout(StringIO()):
            outcome = self._run_gate_for_resume(service)

        self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
        current = cloud_gate.read(folder.parent / 'current.json')
        self.assertNotEqual(current['attempt'], folder.name)
        service.assert_called_once()
        self.assertFalse((folder / 'imported.json').exists())
        self.assertEqual([row['id'] for row in store.locked_rows(store.host_file('aws', 0))],
                         ['gate-' + ledger.text(current, 'attempt')])

    def test_resume_after_gate_record_append_keeps_one_record(self) -> None:
        folder = self._unimported_gate()
        imported = folder / 'imported.json'
        save = cloud_gate.save

        def interrupt_before_receipt(path: Path, row: dict[str, object]) -> None:
            if path == imported:
                raise SystemExit('fixture controller crash after append')
            save(path, row)

        with mock.patch.object(cloud_gate, 'save', side_effect=interrupt_before_receipt):
            with self.assertRaisesRegex(SystemExit, 'fixture controller crash after append'):
                cloud_gate.import_record(folder, cloud_gate.read(folder / cloud_gate.RESULT_FILE))
        self.assertFalse(imported.exists())
        service = mock.Mock(side_effect=AssertionError('A completed gate needs no service'))
        destination = store.host_file('aws', 0)
        previous = destination.read_bytes()
        with redirect_stdout(StringIO()):
            outcome = self._run_gate_for_resume(service)

        self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(cloud_gate.read(imported)['id'], 'gate-' + folder.name)
        service.assert_not_called()

    def test_head_change_during_gate_resume_falls_back(self) -> None:
        folder = self._unimported_gate()
        reserve = cloud_gate.cloud_reservation

        @contextmanager
        def change_head(repo: Path, sidecar: Path, mode: gate.SuiteMode, *,
                        gate_job: StepJob | GateJobNotRecorded) -> Generator[cloud_gate.CloudReservation]:
            with reserve(repo, sidecar, mode, gate_job=gate_job) as reservation:
                _ = gate.checked_git(repo, 'commit', '--allow-empty', '-qm', 'Fixture changed HEAD')
                yield reservation

        service = mock.Mock(side_effect=AssertionError('A completed gate needs no service'))
        with mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=change_head):
            outcome = self._run_gate_for_resume(service)

        self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed during the cloud attempt'))
        self.assertFalse((folder / 'imported.json').exists())
        self.assertFalse(store.host_file('aws', 0).exists())
        service.assert_not_called()

    def test_invalid_unimported_gate_result_falls_back(self) -> None:
        folder = self._unimported_gate()
        result = cloud_gate.read(folder / cloud_gate.RESULT_FILE)
        result['tree'] = 'different-tree'
        cloud_gate.save(folder / cloud_gate.RESULT_FILE, result)
        cloud_gate.save(folder / 'imported.json', {'id': 'gate-previous-attempt'})

        service = mock.Mock(side_effect=AssertionError('A completed gate needs no service'))
        outcome = self._run_gate_for_resume(service)

        self.assertEqual(outcome, cloud_gate.LocalFallback('The result differs from the current tree'))
        self.assertFalse(store.host_file('aws', 0).exists())
        service.assert_not_called()

    def test_legacy_pending_attempt_keeps_its_unknown_job_on_fallback_and_finish(self) -> None:
        mode = gate.SuiteMode.MERGE_GATE
        incoming_job = StepJob('integrate', unit='new-unit', branch='new-branch')
        unknown_job = {'kind': 'GateJobNotRecorded'}
        cloud_gate.save(self.state / 'gate-deployment.json',
                        {'bucket': 'fixture-bucket', 'image': 'fixture-image', 'template': 'fixture-template'})
        root = Path(gate.checked_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
        folder = root / 'legacy-attempt'
        request = cloud_gate.prepare(self.repo, folder, mode, 1800)
        cloud_gate.save(root / 'current.json', {'attempt': folder.name, 'tree': request['tree'], 'commit': request['head'],
            'mode': mode.value, 'full': False, 'module_time_limit_s': 1800, 'modules': 'all', 'selection_tree': 'all'})
        cloud_gate.save(folder / 'launch.json', {'instance': 'i-fixture', 'region': 'us-east-2',
                                                'arguments': {'InstanceType': 'm7a.8xlarge'}})
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        book.record_inventory(ledger.ResourceInventory(now, (), 'fixture', builder_removed=True))
        self.assertIsInstance(book.reserve_gate(self.settings, ledger.GateReservation(
            folder.name, now, ledger.instant(request, 'expires'), Decimal('.8'), Decimal('.02'))), ledger.GateReserved)
        book.resolve_gate(folder.name, ledger.GateRunning('i-fixture'), instance_type='m7a.8xlarge')
        fake = FakeGateAws()
        run = gate.GateRun(tuple(gate.GateJobResult(suite, path.name, 0, 1, '') for suite, path in self.modules), 1, 1)
        fake.result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
        fake.result.update(host='fixture-guest', verdict='pass', gate=gate_box.encode_run(run), gate_s=1,
            record=gate_box.completion_record(run, folder.name, ledger.text(request, 'commit'),
                ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, mode.value))

        def service(command: Sequence[str]) -> int:
            self.assertEqual(Path(command[command.index('--worker') + 1]), folder)
            worker_request = cloud_gate.read(folder / 'request.json')
            self.assertNotIn('gate_job', worker_request)
            if fake.has_result:
                sidecar = Path(command[command.index('--sidecar') + 1])
                worker = cloud_gate.GateWorker(AwsCli('fixture-scaler', 'us-east-2', 1, execute=fake),
                    book, self.settings, sidecar, clock=lambda: now)
                deployment = cloud_gate.read(folder / 'deployment.json')
                cloud_gate.save(folder / 'outcome.json', worker.execute(folder, worker_request, deployment))
            return 0

        fake.has_result = False
        with redirect_stdout(StringIO()):
            first = cloud_gate.run(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                   service=service, gate_job=incoming_job)
            self.assertEqual(first, cloud_gate.LocalFallback('The scaler service has no final result'))
            fake.has_result = True
            second = cloud_gate.run(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                    service=service, gate_job=replace(incoming_job, unit='other-unit'))
        self.assertIsInstance(second, cloud_gate.CloudCompleted)
        self.assertNotIn('gate_job', cloud_gate.read(folder / 'request.json'))
        self.assertEqual(fake.launches, [])
        self.assertEqual([(row['attempt'], row['result'], row['gate_job']) for row in book.launches()],
                         [(folder.name, 'fallback', unknown_job), (folder.name, 'finished', unknown_job)])

    def test_commands_with_different_jobs_resume_one_box_and_keep_its_job(self) -> None:
        mode = gate.SuiteMode.MERGE_GATE
        job_a = StepJob('integrate', unit='unit-a', branch='branch-a')
        job_b = StepJob('integrate', unit='unit-b', branch='branch-b')
        job_record = cast(object, json.loads(wire.encode_record(job_a)))
        cloud_gate.save(self.state / 'gate-deployment.json', {'deployment_id': 'fixture',
            'bucket': 'fixture-bucket', 'revision': 'fixture-revision', 'image': 'fixture-image',
            'template': 'fixture-template', 'version': '1', 'security_group': 'fixture-group',
            'subnets': [{'id': 'fixture-a', 'zone': 'a'}]})
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        book.record_inventory(ledger.ResourceInventory(now, (), 'fixture', builder_removed=True))
        fake = FakeGateAws()
        folders: list[Path] = []

        def service(command: Sequence[str]) -> int:
            folder = Path(command[command.index('--worker') + 1])
            folders.append(folder)
            request = cloud_gate.read(folder / 'request.json')
            self.assertEqual(request['gate_job'], job_record)
            sidecar = Path(command[command.index('--sidecar') + 1])
            worker = cloud_gate.GateWorker(AwsCli('fixture-scaler', 'us-east-2', 1, execute=fake),
                book, self.settings, sidecar, clock=lambda: now)
            deployment = cloud_gate.read(folder / 'deployment.json')
            if len(folders) == 1:
                self.assertIsInstance(book.reserve_gate(self.settings, ledger.GateReservation(
                    folder.name, now, ledger.instant(request, 'expires'), Decimal('.8'), Decimal('.02'))),
                    ledger.GateReserved)
                self.assertEqual(worker.launch(folder, request, deployment), 'i-fixture')
            if len(folders) == 3:
                run = gate.GateRun(tuple(gate.GateJobResult(suite, path.name, 0, 1, '') for suite, path in self.modules), 1, 1)
                fake.result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                fake.result.update(host='fixture-guest', verdict='pass', gate=gate_box.encode_run(run), gate_s=1,
                    record=gate_box.completion_record(run, folder.name, ledger.text(request, 'commit'),
                        ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, mode.value))
                result = worker.execute(folder, request, deployment)
                self.assertEqual(result['status'], 'complete')
                cloud_gate.save(folder / 'outcome.json', result)
                raise OSError('The fixture lost its service connection')
            cloud_gate.save(folder / 'outcome.json', {'status': 'pending', 'reason': 'fixture pending'})
            return 0

        no_service = mock.Mock(side_effect=AssertionError('A completed gate needs no service'))
        with redirect_stdout(StringIO()):
            for job, reason in ((job_a, 'fixture pending'), (job_a, 'fixture pending'),
                                (job_b, 'The fixture lost its service connection')):
                outcome = cloud_gate.run(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                         service=service, gate_job=job)
                self.assertEqual(outcome, cloud_gate.LocalFallback(reason))
            imported = cloud_gate.run(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                      service=no_service, gate_job=job_a)

        self.assertIsInstance(imported, cloud_gate.CloudCompleted)
        no_service.assert_not_called()
        folder = folders[0]
        self.assertEqual(folders, [folder] * 3)
        self.assertEqual([path for path in folder.parent.iterdir() if path.is_dir()], [folder])
        self.assertTrue((folder / 'imported.json').exists())
        self.assertEqual(len(fake.launches), 1)
        self.assertEqual(cloud_gate.read(folder / 'request.json')['gate_job'], job_record)
        current = cloud_gate.read(folder.parent / 'current.json')
        self.assertEqual(current['attempt'], folder.name)
        self.assertEqual(current['gate_job'], job_record)
        rows = book.launches()
        self.assertEqual([row['result'] for row in rows], ['launched', 'fallback', 'fallback', 'finished', 'fallback'])
        self.assertTrue(all((row['attempt'], row['gate_job']) == (folder.name, job_record) for row in rows))

    def test_prepare_failure_preserves_the_saved_job_for_the_next_command(self) -> None:
        mode = gate.SuiteMode.MERGE_GATE
        job_a = StepJob('integrate', unit='unit-a')
        job_b = StepJob('integrate', unit='unit-b')
        recorded_job = cast(object, json.loads(wire.encode_record(job_a)))
        unknown_job = {'kind': 'GateJobNotRecorded'}
        root = Path(gate.checked_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
        current = root / 'current.json'
        cloud_gate.save(self.state / 'gate-deployment.json', {'deployment_id': 'fixture',
            'bucket': 'fixture-bucket', 'revision': 'fixture-revision', 'image': 'fixture-image',
            'template': 'fixture-template', 'version': '1', 'security_group': 'fixture-group',
            'subnets': [{'id': 'fixture-a', 'zone': 'a'}]})
        book = ledger.CostLedger(self.state)
        now = int(time.time())
        book.record_inventory(ledger.ResourceInventory(now, (), 'fixture', builder_removed=True))
        run = gate.GateRun(tuple(gate.GateJobResult(suite, path.name, 0, 1, '') for suite, path in self.modules), 1, 1)

        for legacy, expected in ((False, recorded_job), (True, unknown_job)):
            with self.subTest(legacy=legacy), redirect_stdout(StringIO()):
                if legacy:
                    tree, _dirty = cloud_gate.working_tree(self.repo)
                    cloud_gate.save(current, {'attempt': 'legacy-attempt', 'tree': tree,
                        'commit': gate.checked_git(self.repo, 'rev-parse', 'HEAD'), 'mode': mode.value,
                        'full': False, 'module_time_limit_s': 1800, 'modules': 'all', 'selection_tree': 'all'})
                before = len(book.launches())
                no_service = mock.Mock(side_effect=AssertionError('Preparation failed before the service could start'))
                with mock.patch.object(cloud_gate, 'prepare', side_effect=OSError('fixture preparation failed')):
                    failed = cloud_gate.run(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                            service=no_service, gate_job=job_a)
                self.assertEqual(failed, cloud_gate.LocalFallback('fixture preparation failed'))
                no_service.assert_not_called()
                pointer = cloud_gate.read(current)
                folder = root / ledger.text(pointer, 'attempt')
                if legacy:
                    self.assertEqual(folder.name, 'legacy-attempt')
                self.assertEqual(pointer['gate_job'], expected)
                self.assertFalse((folder / 'request.json').exists())
                self.assertEqual(self.fallback_rows()[-1]['gate_job'], expected)
                fake = FakeGateAws()

                def service(command: Sequence[str]) -> int:
                    self.assertEqual(Path(command[command.index('--worker') + 1]), folder)
                    request = cloud_gate.read(folder / 'request.json')
                    self.assertEqual(request.get('gate_job', unknown_job), expected)
                    fake.result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                    fake.result.update(host='fixture-guest', verdict='pass', gate=gate_box.encode_run(run), gate_s=1,
                        record=gate_box.completion_record(run, folder.name, ledger.text(request, 'commit'),
                            ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, mode.value))
                    sidecar = Path(command[command.index('--sidecar') + 1])
                    worker = cloud_gate.GateWorker(AwsCli('fixture-scaler', 'us-east-2', 1, execute=fake),
                        book, self.settings, sidecar, clock=lambda: now)
                    deployment = cloud_gate.read(folder / 'deployment.json')
                    with mock.patch.object(worker, 'charges'):
                        cloud_gate.save(folder / 'outcome.json', worker.execute(folder, request, deployment))
                    return 0

                resumed = cloud_gate.run(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                         service=service, gate_job=job_b)
                self.assertIsInstance(resumed, cloud_gate.CloudCompleted)
                self.assertEqual(len(fake.launches), 1)
                rows = book.launches()[before:]
                self.assertEqual([row['result'] for row in rows], ['fallback', 'launched', 'finished'])
                self.assertTrue(all((row['attempt'], row['gate_job']) == (folder.name, expected) for row in rows))

    def test_pending_attempt_reuses_only_the_same_working_tree(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        folders: list[Path] = []
        def service(command: Sequence[str]) -> int:
            folder = Path(command[command.index('--worker') + 1])
            folders.append(folder)
            cloud_gate.save(folder / 'outcome.json', {'status': 'pending', 'reason': 'fixture pending'})
            return 0
        for content in ('first edit\n', 'first edit\n', 'second edit\n'):
            _ = self.modules[0][1].write_text(content)
            _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                               module_time_limit_s=1800, service=service)
        self.assertEqual(folders[0], folders[1])
        self.assertNotEqual(folders[1], folders[2])
        paths = ('scripts/fixture/test_one.py',)
        tree, dirty = cloud_gate.working_tree(self.repo)
        self.assertTrue(dirty)
        selected = gate.TreeSelection(paths, tree)
        _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                           module_time_limit_s=1800, service=service, selected=selected)
        stale = gate.TreeSelection(paths, gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        with self.subTest(selection='different tree with a saved request'):
            count = len(folders)
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                     module_time_limit_s=1800, service=service, selected=stale)
            self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed after the test selection'))
            self.assertEqual(len(folders), count)
        reserve = cloud_gate.cloud_reservation

        @contextmanager
        def changed_reservation(repo: Path, sidecar: Path, mode: gate.SuiteMode, *,
                                gate_job: StepJob | GateJobNotRecorded) -> Generator[cloud_gate.CloudReservation]:
            with reserve(repo, sidecar, mode, gate_job=gate_job) as reservation:
                _ = self.modules[1][1].write_text('import sys\n')
                yield reservation

        with (self.subTest(selection='tree changed during reservation'),
              mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=changed_reservation),
              mock.patch.object(cloud_gate, 'run_service', side_effect=AssertionError('No service may start')) as start):
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                     module_time_limit_s=1800, service=service,
                                     selected=gate.TreeSelection(('scripts/fixture/test_two.py',), tree))
        self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed after the test selection'))
        start.assert_not_called()

    def test_both_service_types_have_stop_cleanup(self) -> None:
        folder = self.root / 'trial with spaces'
        for paired in (False, True):
            module = 'scripts.aws_target.aws_trial' if paired else 'scripts.aws_target.cloud_gate'
            options = ['--paired-worker', str(folder)] if paired else ['--worker', str(folder),
                '--directory', str(self.state), '--sidecar', str(folder / 'reservation'), '--settings', str(self.settings)]
            command = cloud_gate.service_command(module, options, 'fixture', paired=paired)
            cleanup = [argument for argument in command if argument.startswith('--property=ExecStopPost=')]
            self.assertEqual(len(cleanup), 1)
            import shlex
            arguments = shlex.split(cleanup[0].split('=', 2)[2])
            expected = ['--paired-cleanup', str(folder)] if paired else ['--cleanup' if value == '--worker' else value for value in options]
            self.assertEqual(arguments, [sys.executable, '-m', module, *expected])

    def test_service_command_omits_conf_dir_without_caller_override(self) -> None:
        configuration = cloud_gate.settings()
        for paired in (False, True):
            with (self.subTest(paired=paired), mock.patch.dict(os.environ),
                  mock.patch.object(cloud_gate, 'settings', return_value=configuration)):
                _ = os.environ.pop('CONF_DIR', None)
                command = cloud_gate.service_command('scripts.aws_target.aws_trial', ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                self.assertFalse(any(argument.startswith('--setenv=CONF_DIR=') for argument in command))

    def test_service_commands_forward_only_set_owner_variables(self) -> None:
        owner = {'CODEX_THREAD_ID': 'fixture-codex', 'CLAUDE_CODE_SESSION_ID': 'fixture-claude',
                 'CLAUDE_SESSION_NAME': 'fixture caller', 'BUILDLOG_UNIT': 'fixture-unit'}
        for paired in (False, True):
            for values in ({}, owner, {'CLAUDE_CODE_SESSION_ID': 'fixture-claude'}, {'BUILDLOG_UNIT': ''}):
                with self.subTest(paired=paired, values=values), mock.patch.dict(os.environ):
                    for key in owner:
                        _ = os.environ.pop(key, None)
                    os.environ.update(values)
                    command = cloud_gate.service_command('scripts.aws_target.aws_trial',
                        ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                    forwarded = service_environment(command)
                    self.assertEqual({key: value for key, value in forwarded.items() if key in owner}, values)
                    self.assertEqual(set(forwarded) - set(owner), {'AWS_SHARED_CREDENTIALS_FILE', 'CONF_DIR'})
                    self.assertIn('--property=UnsetEnvironment=AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CONFIG_FILE AWS_PROFILE', command)

    def test_forwarded_worker_records_keep_the_callers_labels_and_unit(self) -> None:
        override_fixture_settings(self.root / 'build_steps.conf', 'owned.merge-gate={unit} merge\n')
        owner = UnitLabels('fixture caller', 'fixture-production', 'fixture-unit', OwnerRole.UNIT)
        for paired in (False, True):
            for session_key in ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'):
                with self.subTest(paired=paired, session_key=session_key), mock.patch.dict(os.environ):
                    _ = os.environ.pop('CODEX_THREAD_ID', None)
                    _ = os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
                    os.environ.update({session_key: 'fixture-owner', 'CLAUDE_SESSION_NAME': owner.session,
                                       'BUILDLOG_UNIT': owner.unit})
                    command = cloud_gate.service_command('scripts.aws_target.aws_trial',
                        ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                    forwarded = service_environment(command)
                    for key in (session_key, 'CLAUDE_SESSION_NAME', 'BUILDLOG_UNIT'):
                        del os.environ[key]
                    os.environ.update(forwarded)
                    with cloud_gate.cloud_reservation(self.repo, self.root / 'worker.sidecar', gate.SuiteMode.MERGE_GATE):
                        record, = self.admission.caller_reservations().values()
                        identity = record.queue_waiter
                        self.assertEqual((identity.get('session_id'), identity.get('session_name'), identity.get('unit')),
                                         ('fixture-owner', owner.session, owner.unit))
                        step = admission_records.WaitingStep(record.owner.pid, identity.get('session_id', ''),
                            identity.get('session_name', ''), record.step, record.worktree, identity['since'],
                            record.need, admission_records.ReadyForAdmission(), unit=identity.get('unit', ''))
                        self.assertEqual(queue_evidence.step_labels(step, {'fixture-owner': owner}), owner)
                        self.assertEqual(read_step_names().for_owner(step.step, owner.unit, owner.role), 'fixture-unit merge')

    def test_paired_trial_admission_records_its_named_test_job(self) -> None:
        pid = os.getpid()
        started = admission_records.process_start_time(pid)
        self.assertIsNotNone(started)
        assert started is not None
        group = CloudGroup('trial', 1, time.time(), Owner(pid, started))
        with cloud_gate.cloud_reservation(self.repo, self.root / 'trial.sidecar', aws_trial.PAIRED_TRIAL_JOB,
                                           group=group):
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.step, 'test-modules')
            self.assertEqual(record.queue_waiter.get('step'), 'test-modules')
            self.assertEqual(record.gate_job, aws_trial.PAIRED_TRIAL_JOB)
            reading = admission_client.view()
            assert isinstance(reading, wire.CloudQueueRead)
            row, = reading.cloud
            self.assertEqual(row.identity.job, aws_trial.PAIRED_TRIAL_JOB)
            self.assertEqual(row.place, wire.CloudPlace('aws', group))
            self.assertEqual(row.wait, wire.CloudSlotWait(1, reading.cloud_slots, ReservedCloudSlot()))

    def test_service_command_preserves_credential_override(self) -> None:
        credential = self.root / 'override-credentials'
        for paired in (False, True):
            with (self.subTest(paired=paired),
                  mock.patch.dict(os.environ, {'AWS_SHARED_CREDENTIALS_FILE': str(credential)})):
                command = cloud_gate.service_command('scripts.aws_target.aws_trial',
                    ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                overrides = [argument for argument in command
                             if argument.startswith('--setenv=AWS_SHARED_CREDENTIALS_FILE=')]
                self.assertEqual(overrides, ['--setenv=AWS_SHARED_CREDENTIALS_FILE=' + str(credential)])

    def test_service_command_preserves_caller_conf_dir(self) -> None:
        configuration = cloud_gate.settings()
        for paired in (False, True):
            for directory in ('', str(self.root / 'caller-config')):
                with (self.subTest(paired=paired, directory=directory),
                      mock.patch.dict(os.environ, {'CONF_DIR': directory}),
                      mock.patch.object(cloud_gate, 'settings', return_value=configuration)):
                    command = cloud_gate.service_command('scripts.aws_target.aws_trial', ['--paired-worker', str(self.root / 'trial')], 'fixture', paired=paired)
                    overrides = [argument for argument in command if argument.startswith('--setenv=CONF_DIR=')]
                    self.assertEqual(overrides, ['--setenv=CONF_DIR=' + directory])

    def test_session_runs_only_scaler_service_and_imports_explicit_record(self) -> None:
        for dirty, later_edit, changed_head in ((False, False, False), (True, False, False),
                                               (True, True, False), (True, False, True)):
            with self.subTest(dirty=dirty, later_edit=later_edit, changed_head=changed_head):
                if dirty:
                    _ = self.modules[0][1].write_text('snapshot edit\n')
                cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
                commands: list[list[str]] = []

                def service(command: Sequence[str]) -> int:
                    commands.append(list(command))
                    folder = Path(command[command.index('--worker') + 1])
                    request = cloud_gate.read(folder / 'request.json')
                    run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, 0, 1, '') for _, path in self.modules), 1, 1)
                    row: dict[str, object] = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                    row.update(host='remote-host', verdict='pass', gate=gate_box.encode_run(run), record=gate_box.completion_record(
                        run, ledger.text(request, 'attempt'), ledger.text(request, 'commit'), ledger.text(request, 'tree'),
                        'remote-host', 0, 1, 'merge-gate'))
                    record = ledger.mapping(row['record'])
                    record.update(head=request['head'], dirty=bool(request['dirty_paths']), dirty_paths=request['dirty_paths'])
                    if later_edit:
                        _ = self.modules[0][1].write_text('later edit\n')
                    if changed_head:
                        _ = gate.checked_git(self.repo, 'add', '.')
                        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Later HEAD')
                    cloud_gate.save(folder / 'outcome.json', {'status': 'complete', 'result': row})
                    _ = (folder / 'gate.log').write_text('cloud log\n')
                    return 0

                destination = self.root / f'record-{dirty}-{later_edit}-{changed_head}.jsonl'
                before = len(self.fallback_rows())
                with (mock.patch.object(cloud_gate, 'AwsCli', side_effect=AssertionError('The session must not call AWS')),
                      mock.patch.object(store, 'host_file', return_value=destination),
                      mock.patch.dict(os.environ, {'MERGE_GATE_PASSED_DIR': str(self.root / 'passed')}),
                      redirect_stdout(StringIO()) as output):
                    outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                             module_time_limit_s=1800, service=service)
                if changed_head:
                    self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed during the cloud attempt'))
                    self.assertFalse(destination.exists())
                    rows = self.fallback_rows()
                    self.assertEqual(len(rows), before + 1)
                    self.assertEqual(rows[-1]['reason'], 'The checkout changed during the cloud attempt')
                    continue
                self.assertEqual(len(self.fallback_rows()), before)
                self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
                self.assertIn('cloud log', output.getvalue())
                self.assertEqual(commands[0][0], 'systemd-run')
                sidecar = Path(commands[0][commands[0].index('--sidecar') + 1])
                folder = Path(commands[0][commands[0].index('--worker') + 1])
                self.assertEqual(sidecar, folder / 'reservation.cgroup')
                self.assertIn('--setenv=AWS_SHARED_CREDENTIALS_FILE=' + str(self.root / '.config/aws-build/credentials'), commands[0])
                self.assertIn('--property=KillMode=control-group', commands[0])
                self.assertIn('--working-directory=' + str(self.root / '.claude'), commands[0])
                self.assertEqual(ledger.mapping(cast(object, json.loads(destination.read_text())))['host'], 'remote-host')

                record = cloud_gate.read(destination)
                request = cloud_gate.read(folder / 'request.json')
                self.assertEqual(record['sha'], request['commit'])
                self.assertEqual(record['head'], request['head'])
                self.assertEqual(record['dirty'], dirty)
                self.assertFalse((self.root / 'passed/passed-trees.jsonl').exists())

    def test_absent_stack_needs_no_service_or_aws_call(self) -> None:
        service = mock.Mock(side_effect=AssertionError('An absent stack needs no service'))
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                 module_time_limit_s=1800, service=service)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        service.assert_not_called()

    def test_disabled_cloud_gate_skips_deployment_and_aws(self) -> None:
        override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=off')
        fake = FakeGateAws()
        api = AwsCli('fixture', 'us-east-2', 1, fake)
        service = mock.Mock(side_effect=AssertionError('A disabled cloud gate must not start a service'))
        with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
              mock.patch.object(cloud_gate, 'control_lock') as lock,
              mock.patch.object(cloud_gate, 'gate_deployment') as deployment):
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                                     module_time_limit_s=1800, service=service)
        reason = 'Cloud gates are off (build_targets.conf enabled.aws-gate=off)'
        self.assertEqual(outcome, cloud_gate.LocalFallback(reason))
        lock.assert_not_called()
        deployment.assert_not_called()
        service.assert_not_called()
        self.assertEqual(fake.commands, [])
        self.assertEqual([row['reason'] for row in self.fallback_rows()], [reason])

    def test_invalid_cloud_gate_switch_falls_back_with_config_error(self) -> None:
        override_fixture_settings(self.root / 'build_targets.conf', 'enabled.aws-gate=invalid')
        service = mock.Mock(side_effect=AssertionError('An invalid switch must not start a service'))
        mode = gate.SuiteMode.MERGE_GATE
        outcome = cloud_gate.run_cloud(self.repo, mode=mode, full=False, module_time_limit_s=1800,
                                       service=service, attempt=cloud_gate.CloudAttempt(self.state, mode))
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        assert isinstance(outcome, cloud_gate.LocalFallback)
        self.assertIn("build_targets.conf: enabled.aws-gate='invalid':", outcome.reason)
        service.assert_not_called()

    def test_fallback_row_write_error_keeps_the_original_fallback(self) -> None:
        service = mock.Mock(side_effect=AssertionError('An absent stack needs no service'))
        with (mock.patch.object(cloud_gate, 'append_launch',
                                side_effect=PermissionError('fixture row write denied')) as append,
              redirect_stderr(StringIO()) as errors):
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                module_time_limit_s=cloud_gate.settings().number('module_time_limit_s'), service=service)
        self.assertEqual(outcome, cloud_gate.LocalFallback('image or stack is absent'))
        self.assertEqual(errors.getvalue(), 'Cloud gate fallback row was not written: fixture row write denied\n')
        append.assert_called_once()
        service.assert_not_called()
        self.assertFalse((self.state / 'cloud-gate-launches.jsonl').exists())

    def fallback_rows(self) -> list[dict[str, object]]:
        return [row for row in ledger.CostLedger(self.state).launches() if row['result'] == 'fallback']

    def test_early_fallback_records_without_an_attempt(self) -> None:
        for missing in ('deployment', 'image', 'template'):
            with self.subTest(missing=missing):
                deployment: dict[str, object] = {'image': 'fixture-image', 'template': 'fixture-template'}
                if missing != 'deployment':
                    _ = deployment.pop(missing)
                    cloud_gate.save(self.state / 'gate-deployment.json', dict(deployment))
                before = len(self.fallback_rows())
                service = mock.Mock(side_effect=AssertionError('No service may start'))
                outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                    module_time_limit_s=cloud_gate.settings().number('module_time_limit_s'), service=service)
                self.assertEqual(outcome, cloud_gate.LocalFallback('image or stack is absent'))
                rows = self.fallback_rows()
                self.assertEqual(len(rows), before + 1)
                self.assertEqual((rows[-1]['attempt'], rows[-1]['reason'], rows[-1]['mode']),
                                 ('', 'image or stack is absent', gate.SuiteMode.TARGETED_TEST.value))
                self.assertEqual(rows[-1]['gate_job'], {'kind': 'GateJobNotRecorded'})
                self.assertIsInstance(rows[-1]['time'], (int, float))
                service.assert_not_called()

    def test_admission_and_preparation_errors_record_the_created_attempt(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        root = Path(gate.checked_git(self.repo, 'rev-parse', '--path-format=absolute', '--git-path', 'cloud-gates'))
        for phase, error in (('cloud_reservation', GateRefused('fixture admission refusal')),
                             ('prepare', GateRefused('The checkout changed after the test selection')),
                             ('prepare', OSError('fixture source read error'))):
            with (self.subTest(phase=phase, reason=str(error)),
                  mock.patch.object(cloud_gate, phase, side_effect=error)):
                before = len(self.fallback_rows())
                service = mock.Mock(side_effect=AssertionError('No service may start'))
                outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.TARGETED_TEST, full=False,
                    module_time_limit_s=cloud_gate.settings().number('module_time_limit_s'), service=service)
                self.assertEqual(outcome, cloud_gate.LocalFallback(str(error)))
                rows = self.fallback_rows()
                self.assertEqual(len(rows), before + 1)
                self.assertEqual(rows[-1]['reason'], str(error))
                attempt = ledger.text(rows[-1], 'attempt')
                self.assertTrue(attempt)
                self.assertTrue((root / attempt).is_dir())
                self.assertFalse((root / attempt / 'request.json').exists())
                self.assertTrue(self.admission.callers_released())
                service.assert_not_called()

    def test_service_fallback_paths_record_one_final_reason(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({}, 'The scaler service has no final result'),
            ({'status': 'fallback', 'reason': 'Spot capacity is unavailable in every configured region'},
             'Spot capacity is unavailable in every configured region'),
            ({'status': 'fallback', 'reason': 'fixture permission refusal'}, 'fixture permission refusal'),
            ({'status': 'pending', 'reason': 'fixture pending cleanup'}, 'fixture pending cleanup'),
            ({'status': 'complete', 'result': {}}, 'The result differs from the current attempt'),
        )
        for reply, reason in cases:
            with self.subTest(reply=reply):
                attempts: list[str] = []
                def service(command: Sequence[str]) -> int:
                    folder = Path(command[command.index('--worker') + 1])
                    attempts.append(folder.name)
                    (folder / 'outcome.json').unlink(missing_ok=True)
                    if reply:
                        cloud_gate.save(folder / 'outcome.json', reply)
                    return 1
                before = len(self.fallback_rows())
                outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                    module_time_limit_s=cloud_gate.settings().number('module_time_limit_s'), service=service)
                self.assertEqual(outcome, cloud_gate.LocalFallback(reason))
                rows = self.fallback_rows()
                self.assertEqual(len(rows), before + 1)
                self.assertEqual((rows[-1]['attempt'], rows[-1]['reason']), (attempts[-1], reason))
                self.assertTrue(self.admission.callers_released())

    def test_cloud_admission_uses_route_identity_and_no_local_memory(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        self.admission.free(0)
        requests: list[wire.AdmitRequest] = []
        admit = admission_client.admit
        def record(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
                   refresh: Callable[[], wire.AdmitRequest], *,
                   _attempt: Callable[[], None]) -> wire.Admitted | wire.Refused | admission_client.LeftQueue:
            requests.append(request)
            return admit(request, on_wait, refresh, _attempt=_attempt)
        def service(command: Sequence[str]) -> int:
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.need, 0)
            sidecar = Path(command[command.index('--sidecar') + 1])
            self.assertEqual(str(sidecar), requests[-1].sidecar)
            self.assertIsInstance(cloud_progress.read(sidecar), cloud_progress.NoCloudProgress)
            reading = admission_client.view()
            assert isinstance(reading, wire.CloudQueueRead)
            row, = reading.cloud
            self.assertEqual(row.wait, wire.CloudSlotWait(1, reading.cloud_slots, ReservedCloudSlot()))
            folder = Path(command[command.index('--worker') + 1])
            self.assertEqual(cloud_gate.read(folder / 'request.json')['gate_job'],
                             json.loads(wire.encode_record(record.gate_job)))
            cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': 'capacity'})
            return 0
        with mock.patch.object(admission_client, 'admit', side_effect=record):
            for mode, job in ((gate.SuiteMode.MERGE_GATE, StepJob('integrate', unit='fixture', branch='integration')),
                              (gate.SuiteMode.TARGETED_TEST, StepJob('test-all'))):
                outcome = cloud_gate.run(self.repo, mode=mode, full=True, module_time_limit_s=1800,
                                         service=service, gate_job=job)
                self.assertIsInstance(outcome, cloud_gate.LocalFallback)
                self.assertTrue(self.admission.callers_released())
                request = requests[-1]
                self.assertEqual(request.place, wire.CloudPlace('aws'))
                self.assertEqual(request.identity.step, mode.value)
                self.assertEqual(request.identity.job, job)
                self.assertEqual(self.fallback_rows()[-1]['gate_job'], json.loads(wire.encode_record(job)))
                self.assertEqual(request.owner.pid, os.getpid())
                self.assertEqual(request.run, wire.RunIdentity(request.owner.pid, request.owner.start_time))

    def test_cloud_fallback_releases_before_local_memory_admission(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        def service(command: Sequence[str]) -> int:
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.need, 0)
            folder = Path(command[command.index('--worker') + 1])
            cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': 'capacity'})
            return 0
        run = cloud_gate.run
        def route(repo: Path, *, mode: gate.SuiteMode, full: bool, module_time_limit_s: float,
                  selected: gate.AllModules | gate.TreeSelection, gate_job: StepJob | GateJobNotRecorded
                  ) -> cloud_gate.LocalFallback | cloud_gate.CloudCompleted:
            return run(repo, mode=mode, full=full, module_time_limit_s=module_time_limit_s,
                       selected=selected, gate_job=gate_job, service=service)
        def local(repo: Path, *_args: object, **_kwargs: object) -> gate.GateRun:
            self.assertTrue(self.admission.callers_released())
            with gate.gate_reservation(repo, self.root, gate.BuildHoldWait(), mode=gate.SuiteMode.TARGETED_TEST) as place:
                record, = self.admission.caller_reservations().values()
                self.assertGreater(record.need, 0)
                place.status = 0
            return gate.GateRun((), 0, 0)
        with (mock.patch.object(cloud_gate, 'run', side_effect=route),
              mock.patch.object(gate, 'run_suites', side_effect=local), redirect_stdout(StringIO())):
            _ = gate.run_gate_suites(self.repo, full=True, mode=gate.SuiteMode.TARGETED_TEST)
        self.assertTrue(self.admission.callers_released())

    def test_cloud_service_error_releases_its_slot(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        for status_error in (False, True):
            with (self.subTest(status_error=status_error),
                  mock.patch.object(affected_modules, 'dirty_paths', return_value=set(),
                                    side_effect=affected_modules.GraphError('fixture status error') if status_error else None)):
                service = mock.Mock(side_effect=OSError('fixture service error'))
                outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                                         module_time_limit_s=1800, service=service)
                self.assertIsInstance(outcome, cloud_gate.LocalFallback)
                self.assertTrue(self.admission.callers_released())
                if status_error:
                    self.assertEqual(outcome, cloud_gate.LocalFallback('fixture status error'))
                    service.assert_not_called()

    def test_route_uses_installed_limit_and_handles_missing_results(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True, module_time_limit_s=1800,
                                 service=lambda _command: 0)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        self.assertTrue(self.admission.callers_released())

        installed = self.root / '.claude/config/aws_target.conf'
        installed.parent.mkdir(parents=True)
        _ = installed.write_bytes(self.settings.read_bytes())
        numeric_settings(installed, {'daily_limit_usd': '1'})
        checkout = self.repo / 'config'
        checkout.mkdir()
        for source in self.root.glob('*.conf'):
            _ = (checkout / source.name).write_bytes(source.read_bytes())
        for missing_minimum in (True, False):
            with self.subTest(missing_minimum=missing_minimum):
                contents = self.settings.read_text()
                if missing_minimum:
                    contents = '\n'.join(line for line in contents.splitlines()
                                         if not line.startswith('box_minimum_seconds=')) + '\n'
                _ = (checkout / 'aws_target.conf').write_text(contents)
                fake = FakeGateAws()
                now = int(time.time())
                book = ledger.CostLedger(self.state)
                book.record_inventory(ledger.ResourceInventory(now, (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
                book.observe(ledger.InstanceObservation(now, 'stopped', 'fixture'))
                def service(command: Sequence[str]) -> int:
                    folder = Path(command[command.index('--worker') + 1])
                    request = cloud_gate.read(folder / 'request.json')
                    run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, 0, 1, '')
                                             for _, path in self.modules), 1, 1)
                    fake.result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                    fake.result.update(host='fixture-guest', verdict='pass', gate=gate_box.encode_run(run),
                        record=gate_box.completion_record(run, ledger.text(request, 'attempt'),
                            ledger.text(request, 'commit'), ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, 'merge-gate'))
                    api = AwsCli('build-scaler', 'fixture-region', 1, execute=fake)
                    arguments = list(command[command.index('--worker'):])
                    with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
                          mock.patch.object(sys, 'argv', ['cloud-gate', *arguments])):
                        return cloud_gate.main()
                deployment: dict[str, object] = {'image': 'fixture-image', 'template': 'fixture-template',
                    'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'version': '1',
                    'security_group': 'fixture-group', 'revision': 'fixture-revision',
                    'subnets': [{'id': 'fixture-a', 'zone': 'a'}]}
                cloud_gate.save(self.state / 'gate-deployment.json', deployment)
                with mock.patch.dict(os.environ, {'CONF_DIR': str(checkout), 'AWS_TARGET_CONFIG': str(installed)}):
                    outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                                             module_time_limit_s=1800, service=service)
                self.assertEqual(outcome, cloud_gate.LocalFallback('daily_limit'))
                self.assertEqual(fake.launches, [])
                self.assertTrue(self.admission.callers_released())
