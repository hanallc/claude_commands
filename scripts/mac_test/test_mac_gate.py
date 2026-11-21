"""Exercise the Mac gate route and the module list with private fixtures."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
import signal
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, TextIO, cast, final, override
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_wire as wire, execution_identity, placement
from scripts.buildlog import store
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.direct.wait_for_test_state import wait_until
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib import conf, process_group, scratch
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.process_start import Started, Unreadable, process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, eligibility, mac_gate, mac_queue_run, mac_test, offload
from scripts.mac_test.test_mac_queue_run import queue_fixture
from scripts.production import merge_gate as gate
from scripts.production.gate_routes import MacGateCompleted, MacGatePrelaunchRefused, MacGateUnresolved
from scripts.production.gate_modules import GateRefused
from scripts.test_times import gate_record

GATE_QUEUE_FIXTURE = 'gate-queue-fixture.py'
FAKE_QUEUE = """\
import atexit, json, os, select, shutil, socket, sys
from pathlib import Path

args = sys.argv[1:]
assert args[args.index('--kind') + 1] == 'run'
assert Path(args[args.index('--lifecycle-script') + 1]).is_file()
folder = Path(args[-2])
if '--gate-preparation-fd' in args:
    sys.path.insert(0, os.environ['MAC_GATE_FIXTURE_ROOT'])
    from scripts.mac_test.mac_gate import assigned_gate_paths

    paths = assigned_gate_paths(folder.name.removeprefix('mac-gate-'))
    paths.journal.parent.mkdir(parents=True, exist_ok=True)
    for path in (paths.journal, paths.preparation_lock):
        path.touch()
        atexit.register(path.unlink, missing_ok=True)
    with socket.socket(fileno=int(args[args.index('--gate-preparation-fd') + 1])) as preparation:
        folder.parent.mkdir(parents=True)
        atexit.register(shutil.rmtree, folder.parent, ignore_errors=True)
        preparation.sendall(b'1')
        if preparation.recv(1) != b'1':
            sys.exit(2)
request = json.loads((folder / 'request.json').read_text())
assert (folder / 'module_seconds.json').is_file()
assert (folder / 'source.bundle').is_file() == bool(request['source_key'])
case = os.environ['MAC_GATE_CASE']
if '--assignment-file' in args:
    sys.path.insert(0, os.environ['MAC_GATE_FIXTURE_ROOT'])
    from scripts.build_queue import admission_wire as wire, execution_identity
    reply = wire.parse_reply(Path(args[args.index('--assignment-file') + 1]).read_text())
    assert isinstance(reply, wire.Assignment)
    assignment = reply.assignment
    assert request['attempt'] == 'mac-gate-' + assignment.attempt
    assert assignment.source_key == execution_identity.SourceKeyKnown(request['tree'])
    if case == 'assigned-refusal':
        (folder / os.environ['MAC_GATE_REFUSAL_FILE']).write_text(os.environ['MAC_GATE_REFUSAL'])
        sys.exit(2)
    if case != 'unsettled':
        (folder / os.environ['MAC_GATE_COMPLETION_FILE']).write_text(assignment.attempt)
if case != 'cleanup-refused':
    assert '--no-wait' not in args
if case == 'timeout':
    caller = int(args[args.index('--caller-fd') + 1])
    ready, _, _ = select.select([caller], [], [], float(os.environ['MAC_GATE_HANG_GUARD']))
    assert ready and os.read(caller, 1) == b''
    Path(os.environ['MAC_GATE_RESULT']).with_name('timeout-cleaned').touch()
    sys.exit(2)
if case == 'lost':
    sys.exit(0)
if case == 'status':
    sys.exit(23)
if case == 'refused':
    print('Read the Mac queue settings', flush=True)
    print('mac queue: the Mac settings are invalid', file=sys.stderr, flush=True)
    print('  ', file=sys.stderr, flush=True)
    sys.exit(2)
if case == 'cleanup-refused':
    print(f'mac_gate_cleanup_error={folder.name}:The Mac kept its gate folder', flush=True)
    print('mac queue: the Mac run may still be live', flush=True)
    print('  ', flush=True)
    sys.exit(2)

row = {key: request[key] for key in ('attempt', 'tree', 'commit')}
row['gate'] = json.loads(Path(os.environ['MAC_GATE_RESULT']).read_text())
if case in ('attempt', 'commit', 'tree'):
    row[case] = 'wrong'
if case == 'modules':
    row['gate']['jobs'][0]['module'] = 'test_wrong.py'
print('mac_gate_result=' + json.dumps(row))
print('mac_exit=0')
"""


UPLOADED_MAC_ENTRY = """\
import builtins, runpy, sys
from pathlib import Path

uploaded = Path(sys.argv[1])
original_import = builtins.__import__

def entry_import(name, namespace=None, *args, **kwargs):
    if namespace and namespace.get('__file__') == str(uploaded):
        assert name != 'scripts.production.gate_routes', 'The uploaded entry imported the controller'
    return original_import(name, namespace, *args, **kwargs)

builtins.__import__ = entry_import
sys.argv = sys.argv[1:]
runpy.run_path(str(uploaded), run_name='__main__')
"""


LOCAL_MAC_SSH = """\
import os, shlex, subprocess, sys
command = [*shlex.split(os.environ.get('MAC_GATE_FIXTURE_REMOTE_LAUNCHER', '')), '/bin/sh', '-c', sys.argv[-1]]
environment = {
    # Keep the fixture PATH when the NixOS startup file runs.
    **os.environ, '__ETC_ZSHENV_SOURCED': '1',
    'ZDOTDIR': os.environ['FAKE_QUEUE_STATE'],
}
raise SystemExit(subprocess.run(command, env=environment, start_new_session=True).returncode)
"""

LOCAL_MAC = """\
import json, os, shlex, shutil, sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ['MAC_GATE_FIXTURE_ROOT'])
from scripts.lib import conf, process_group, scratch
from scripts.mac_test import mac_gate
from scripts.aws_target import cloud_gate
from scripts.copies import copies

root = Path(os.environ['FAKE_QUEUE_STATE'])
folder = root / mac_gate.REMOTE_ROOT / 'mac-gate-fixture'
phase = sys.argv[1]
if phase == 'prepare':
    argv_file, source = map(Path, sys.argv[2:4])
    if (root / 'prepare_bundle').exists():
        request = cloud_gate.read(source / 'request.json')
        checkout = Path((source / 'local-repo').read_text())
        with mac_gate.hold_gate_creation_commands():
            cloud_gate.create_source_bundle(checkout, source / 'source.bundle', [request['commit']])
        raise AssertionError('the fixture bundle was not interrupted')
    folder.mkdir(parents=True)
    for name in ('request.json', 'module_seconds.json'):
        shutil.copyfile(source / name, folder / name)
    checkout = (source / 'local-repo').read_text()
    mirror = copies.RemoteMirror('fixture', copies.SelectedRepository('fixture'), 'localhost', checkout, folder.name)
    with copies.remote_lease(mirror):
        copies.register_remote(mirror)
    cloud_gate.save(folder / 'checkout.json', {'path': checkout})
    command = [sys.executable, __file__]
    argv_file.write_text(json.dumps([*command, 'guest']))
    argv_file.with_suffix('.json.close').write_text(shlex.join([*command, 'close']))
elif phase == 'guest':
    with (root / 'guest.log').open('w') as output:
        os.dup2(output.fileno(), sys.stdout.fileno())
        os.dup2(output.fileno(), sys.stderr.fileno())
    try:
        mac_gate.run_guest(folder, full=True)
    except KeyboardInterrupt:
        record = cloud_gate.read(folder / 'run.json')
        cloud_gate.save(root / 'interrupted', record)
        raise
else:
    (root / 'close_started').touch()
    if phase == 'cleanup' and (root / 'close_refusal').exists():
        source = Path(sys.argv[3])
        print(f'{mac_gate.CLEANUP_ERROR_PREFIX}{source.name}:The Mac kept its gate folder', flush=True)
        raise SystemExit(1)
    with patch.object(scratch, 'end', return_value=scratch.TaskCleanup()):
        mac_gate.stop_checkout(folder)
    (root / 'close_finished').touch()
"""

CALL_RUN_REMOTE = """\
import os, sys
from pathlib import Path

sys.path.insert(0, os.environ['MAC_GATE_FIXTURE_ROOT'])
from scripts.mac_test import mac_gate
from scripts.production.merge_gate import ALL_MODULES, SuiteMode
from scripts.production import gate_routes

repo, callback, fd = sys.argv[1:]
mac_gate.__file__ = callback
if 'MAC_GATE_FORCE_QUEUE' in os.environ:
    os.environ['MAC_GATE_QUEUE_ENTRY'] = mac_gate.QUEUE_COMMAND[1]
    mac_gate.QUEUE_COMMAND = (sys.executable, os.environ['MAC_GATE_FORCE_QUEUE'])
cancellation = gate_routes.MacGateCancellation(int(fd)) if fd else mac_gate.USE_CALLER_PROCESS_LIFETIME
print(mac_gate.run_remote(Path(repo), mode=SuiteMode.MERGE_GATE, full=True,
                           module_time_limit_s=30, selected=ALL_MODULES, cancellation=cancellation), flush=True)
"""

HELD_BUNDLE_GIT = """\
import os, signal, sys
from pathlib import Path
if sys.argv[1:3] == ['bundle', 'create']:
    marker = Path(os.environ['FAKE_QUEUE_STATE']) / 'bundle_pid'
    marker.with_suffix('.new').write_text(str(os.getpid()))
    marker.with_suffix('.new').rename(marker)
    signal.alarm(60)
    signal.pause()
    raise AssertionError('the fixture Git process was not stopped')
os.execv(os.environ['FIXTURE_REAL_GIT'], [os.environ['FIXTURE_REAL_GIT'], *sys.argv[1:]])
"""


HELD_MODULE = """\
import os, time
from pathlib import Path
root = Path(os.environ['FAKE_QUEUE_STATE'])
path = root / 'guest_ready'
path.with_suffix('.new').write_text(str(os.getpid()))
path.with_suffix('.new').rename(path)
time.sleep(60)
"""


def module_job(name: str, *, status: int = 0, tests: int = 10, skips: int = 2) -> gate.GateJobResult:
    times: gate_record.ModuleTimes = {
        'tests_run': tests, 'skipped': skips, 'failures': int(status != 0), 'errors': 0,
        'wall_s': 0.1, 'cpu_s': 0.1, 'tests_s': 0.1, 'tests': [],
    }
    return gate.GateJobResult('fixture', f'test_{name}.py', status, 0.1, '', times=times,
                              wall_clock=gate_record.WallClockSpan(1, 1.1))


def module_path(job: gate.GateJobResult) -> str:
    return f'scripts/{job.suite}/{job.module}'


@final
class StalledCleanupProcess:
    pid = 2
    stdin = stdout = stderr = None
    returncode: int | None = None

    def __init__(self, budget: float, *, exits_after_kill: bool) -> None:
        self.budget = budget
        self.exits_after_kill = exits_after_kill
        self.killed = False
        self.wait_deadlines: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def communicate(self, _input: str | None = None, *, timeout: float) -> tuple[str, str]:
        if not 0 < timeout <= self.budget:
            raise AssertionError('The cleanup command has no valid deadline')
        raise subprocess.TimeoutExpired('cleanup', timeout)

    def terminate(self) -> None:
        return

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None or not 0 <= timeout <= self.budget:
            raise AssertionError('The last wait has no cleanup deadline')
        self.wait_deadlines.append(timeout)
        if self.killed and self.exits_after_kill:
            self.returncode = -signal.SIGKILL
            return self.returncode
        raise subprocess.TimeoutExpired('cleanup', timeout)

    def __enter__(self) -> StalledCleanupProcess:
        return self

    def __exit__(self, *_args: object) -> None:
        _ = self.wait()


@final
class MacGateTests(unittest.TestCase):
    root = Path()
    repo = Path()
    tree = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        _ = gate.checked_git(self.repo, 'init', '-q')
        self.add_module(module_job('one'))
        (self.repo / 'config').mkdir()
        _ = (self.repo / 'config/fixture.conf').write_text('fixture\n')
        head = commit_repository(self.repo, 'fixture')
        _ = gate.checked_git(self.repo, 'update-ref', 'refs/remotes/origin/main', head)
        _ = gate.checked_git(self.repo, 'remote', 'add', 'origin', str(self.repo))
        self.tree = gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}')

    def add_module(self, job: gate.GateJobResult) -> None:
        path = self.repo / module_path(job)
        path.parent.mkdir(exist_ok=True, parents=True)
        _ = path.write_text('# fixture\n')

    @contextmanager
    def queue_case(self, case: str, job: gate.GateJobResult) -> Generator[gate.TreeSelection]:
        command = self.root / GATE_QUEUE_FIXTURE
        _ = command.write_text(FAKE_QUEUE)
        payload = self.root / 'gate.json'
        _ = payload.write_text(json.dumps(gate_box.encode_run(gate.GateRun((job,), 1, 0))))
        timeout = 0.2 if case == 'timeout' else 15.0
        with (mock.patch.dict(os.environ, {'MAC_GATE_CASE': case, 'MAC_GATE_RESULT': str(payload),
                                           'MAC_GATE_HANG_GUARD': str(TEST_HANG_GUARD_SECS),
                                           'MAC_GATE_FIXTURE_ROOT': str(Path(__file__).resolve().parents[2])}),
              mock.patch.object(mac_gate, 'QUEUE_COMMAND', (sys.executable, str(command))),
              mock.patch.object(mac_gate, 'MAC_TIME_LIMIT_S', timeout)):
            yield gate.TreeSelection((module_path(job),), self.tree)

    def run_case(self, case: str, job: gate.GateJobResult) -> gate.GateRun | str:
        with self.queue_case(case, job) as selection:
            outcome = mac_gate.run_remote(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                module_time_limit_s=30, selected=selection, cancellation=mac_gate.USE_CALLER_PROCESS_LIFETIME)
            return outcome if isinstance(outcome, str) else outcome.run

    def test_assigned_gate_requires_a_valid_result_and_confirmed_completion(self) -> None:
        destination = placement.PlacementDestination('mac', 'mac', 'fixture-mac', claim_state.WHOLE_MAC)
        assignment = placement.PlacementAssignment('episode', 'attempt', destination,
            execution_identity.SourceKeyKnown(self.tree), placement.SubmittingOwner(store.host_name(), os.getpid(), 'start'),
            NoOwningSession(), 1)
        refusal = wire.DispatchStatus(assignment, placement.ClosedAssignment('blocked'), placement.PermissionAvailable(),
                                     retry=placement.DoNotRetry('blocked'))
        for case in ('completed', 'assigned-refusal', 'unsettled', 'lost', 'tree'):
            with (self.subTest(case=case),
                  self.queue_case(case, module_job('one', status=1)) as selection,
                  mock.patch.dict(os.environ, {
                      'MAC_GATE_REFUSAL': wire.encode_reply(refusal),
                      'MAC_GATE_REFUSAL_FILE': mac_gate.ASSIGNED_REFUSAL,
                      'MAC_GATE_COMPLETION_FILE': mac_gate.ASSIGNED_COMPLETION,
                  })):
                result = mac_gate.run_assigned(self.repo, assignment=assignment, mode=gate.SuiteMode.MERGE_GATE,
                                               full=False, module_time_limit_s=30, selected=selection)
                if case == 'completed':
                    assert isinstance(result, MacGateCompleted), result
                    self.assertFalse(result.run.passed)
                else:
                    expected = MacGatePrelaunchRefused if case == 'assigned-refusal' else MacGateUnresolved
                    self.assertIsInstance(result, expected)

    def test_returns_pass_and_test_failure(self) -> None:
        for status in (0, 1):
            with self.subTest(status=status):
                job = module_job('one', status=status)
                result = self.run_case('completed', job)
                assert isinstance(result, gate.GateRun), result
                self.assertEqual(result.jobs, (job,))
                self.assertEqual(result.passed, status == 0)

    def test_queue_failure_uses_natedev(self) -> None:
        for case, reason in (('status', 'status 23'), ('timeout', 'exceeded'),
                             ('lost', 'no unique gate result')):
            with self.subTest(case=case):
                result = self.run_case(case, module_job('one'))
                assert isinstance(result, str), result
                self.assertIn(reason, result)
                if case == 'timeout':
                    self.assertTrue((self.root / 'timeout-cleaned').exists())

    def test_queue_cancellation_uses_a_cleanup_deadline(self) -> None:
        communicate = cast(Callable[[subprocess.Popen[str], str | None, float | None], tuple[str, str | None]],
                           getattr(subprocess.Popen, 'communicate'))
        deadlines: list[float | None] = []

        def record_queue_deadline(process: subprocess.Popen[str], input: str | None = None,
                                  timeout: float | None = None) -> tuple[str, str | None]:
            if isinstance(process.args, list) and str(self.root / GATE_QUEUE_FIXTURE) in process.args:
                deadlines.append(timeout)
            return communicate(process, input, timeout)

        with mock.patch.object(subprocess.Popen, 'communicate', record_queue_deadline):
            outcome = self.run_case('timeout', module_job('one'))
        assert isinstance(outcome, str), outcome
        self.assertIn('exceeded', outcome)
        self.assertEqual(len(deadlines), 2)
        self.assertIsNotNone(deadlines[1], 'The gate drained the cancelled queue without a deadline')

    def test_claim_lock_and_watcher_share_a_finite_cleanup_budget(self) -> None:
        paths = claim_state.StatePaths(self.root / 'provider', 'mac')
        identity = claim_state.ClaimIdentity('claim', 'episode', 'attempt')
        with mock.patch.dict(os.environ, {'MAC_TEST_MESSAGE_TIMEOUT_S': '0.125'}):
            answers: list[mac_test.ClaimsReleased | mac_test.ClaimReleaseRefused] = []
            with claim_state.locked(paths.lock):
                def release() -> None:
                    answers.append(mac_test.release_exact_claims(paths, os.getpid(), identity, ''))

                worker = threading.Thread(target=release)
                worker.start()
                try:
                    worker.join(1)
                    self.assertFalse(worker.is_alive(), 'Claim release blocked on state.lock')
                finally:
                    self.addCleanup(worker.join, TEST_HANG_GUARD_SECS)
            self.assertEqual(answers, [mac_test.ClaimReleaseRefused('the Mac claim state lock is held')])
            process = StalledCleanupProcess(mac_test.message_timeout_s(), exits_after_kill=True)
            def kill_watcher(_pid: int, _signal: int) -> None:
                process.kill()

            with (mock.patch.object(subprocess, 'Popen', return_value=process),
                  mock.patch.object(subprocess, 'run', side_effect=AssertionError('Watcher start has an implicit wait')),
                  mock.patch.object(process_group, 'send_process_group', side_effect=kill_watcher),
                  redirect_stderr(io.StringIO()) as warning):
                mac_test.start_claim_watcher(paths)
            self.assertIn('claim watcher could not be started', warning.getvalue())
            self.assertEqual(len(process.wait_deadlines), 1)

    def test_claim_release_starts_block_watcher_without_mac_gate_configuration(self) -> None:
        paths = claim_state.StatePaths(self.root / 'provider', 'mac')
        identity = claim_state.ClaimIdentity('claim', 'episode', 'attempt')
        def read_config(*args: object, **kwargs: object) -> conf.ConfigRead:
            self.fail(f'Ordinary release read configuration {args} {kwargs}')

        communicate = mock.Mock(return_value=('', ''))
        process = mock.Mock(returncode=0, stdin=None, stdout=None, stderr=None, communicate=communicate)
        with (mock.patch.object(mac_test, 'read_block', return_value=mock.Mock(spec=claim_state.PendingMacBlock)),
              mock.patch.object(conf, 'read', side_effect=read_config),
              mock.patch.object(subprocess, 'Popen', return_value=process) as start):
            self.assertEqual(mac_test.release_exact_claims(paths, os.getpid(), identity, ''),
                             mac_test.ClaimsReleased(0, ()))
        self.assertEqual(start.call_args.args[0][0], 'systemd-run')
        self.assertGreater(cast(float, communicate.call_args.kwargs['timeout']), 0)

    def test_recovery_bounds_removal_and_passes_ownership_to_the_process(self) -> None:
        environment = queue_fixture(self.root, LOCAL_MAC_SSH)
        with mock.patch.dict(os.environ, environment, clear=True):
            paths = mac_gate.assigned_gate_paths('stalled')
            paths.journal.parent.mkdir(parents=True)
            paths.journal.touch()
            run = mac_queue_run.ManualMacRun(paths, {
                'lifecycle_script': '', 'arguments': [], 'host': '',
                'settlement': mac_queue_run.GateSettlement.RELEASE_CONFIRMED,
            }, {'pid': 99, 'start': 'ended', 'boot': 'fixture'})
            process = StalledCleanupProcess(mac_gate.checkout_cleanup_seconds(), exits_after_kill=True)
            def kill_removal(_pid: int, _signal: int) -> None:
                process.kill()

            with (mock.patch.object(mac_queue_run, 'read_ended_manual_mac_run', return_value=run),
                  mock.patch.object(mac_queue_run, 'queue_creators_ended', return_value=True),
                  mock.patch.object(mac_queue_run, 'cleanup_lifecycle', return_value=0),
                  mock.patch.object(shutil, 'rmtree', side_effect=AssertionError('Recovery removed files in its thread')),
                  mock.patch.object(subprocess, 'Popen', return_value=process) as start,
                  mock.patch.object(process_group, 'send_process_group', side_effect=kill_removal),
                  self.assertRaises(subprocess.TimeoutExpired)):
                mac_queue_run.recover_manual_mac_run(run, environment)
            self.assertEqual(len(cast(tuple[int, ...], start.call_args.kwargs['pass_fds'])), 2)
            self.assertEqual(len(process.wait_deadlines), 1)
            self.assertTrue(paths.journal.exists())
            with mac_queue_run.creation_lease(paths.lock, fcntl.LOCK_EX):
                pass

    def test_registered_recovery_supervises_a_stalled_journal_write(self) -> None:
        environment = queue_fixture(self.root, LOCAL_MAC_SSH)
        with mock.patch.dict(os.environ, environment, clear=True):
            paths = mac_gate.assigned_gate_paths('stalled')
            paths.journal.parent.mkdir(parents=True)
            assignment = placement.PlacementAssignment('episode', 'stalled',
                placement.PlacementDestination('mac', 'mac', 'fixture-mac', claim_state.RepositoryScope('fixture')),
                execution_identity.SourceKeyKnown('tree'), placement.SubmittingOwner('local', 42, 'ended'),
                NoOwningSession(), 1)
            saved: mac_queue_run.SavedRun = {
                'lifecycle_script': '', 'arguments': [], 'host': 'fixture-mac',
                'lifecycle_started': False, 'settlement': mac_queue_run.GateSettlement.RELEASE_PENDING,
            }
            mac_queue_run.save_run(paths.journal, saved)
            original = paths.journal.read_bytes()
            run = mac_queue_run.RegisteredMacRun(assignment, paths, saved,
                {'pid': 42, 'start': 'ended', 'boot': 'fixture'})
            process = StalledCleanupProcess(mac_gate.checkout_cleanup_seconds(), exits_after_kill=True)

            def kill_writer(_pid: int, _signal: int) -> None:
                process.kill()

            with (mock.patch.object(mac_queue_run, 'read_registered_mac_run', return_value=run),
                  mock.patch.object(mac_queue_run, 'registered_mac_runner_live', return_value=False),
                  mock.patch.object(mac_queue_run, 'write_text_atomic',
                                    side_effect=AssertionError('Recovery wrote its journal in the service thread')),
                  mock.patch.object(subprocess, 'Popen', return_value=process) as start,
                  mock.patch.object(process_group, 'send_process_group', side_effect=kill_writer),
                  mock.patch.object(mac_queue_run, 'release_assigned_claim') as release,
                  self.assertRaises(subprocess.TimeoutExpired)):
                mac_queue_run.recover_registered_mac_run(run, service_settled=False)
            self.assertIn('write-run', cast(list[str], start.call_args.args[0]))
            self.assertEqual(len(cast(tuple[int, ...], start.call_args.kwargs['pass_fds'])), 2)
            self.assertEqual(len(process.wait_deadlines), 1)
            self.assertEqual(paths.journal.read_bytes(), original)
            release.assert_not_called()
            with mac_queue_run.creation_lease(paths.lock, fcntl.LOCK_EX):
                pass

    def test_remote_checkout_cleanup_preserves_records_while_the_registry_lock_is_held(self) -> None:
        environment = queue_fixture(self.root, LOCAL_MAC_SSH)
        _ = write_config_fixture(self.root / 'config/mac_gate.conf', 'terminate_grace_seconds=0.2\n')
        _ = write_config_fixture(self.root / 'config/build_admission.conf', 'program_stop_seconds=0.2\n')
        with mock.patch.dict(os.environ, environment, clear=True):
            folder = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-locked'
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'checkout.json', {'path': str(self.repo)})
            registry = copies.remote_registry()
            mirror = copies.RemoteMirror('fixture', copies.SelectedRepository('fixture'), 'localhost',
                                         str(self.repo), 'locked')
            copies.register_remote(mirror)
            record = copies.remote_record_path(mirror)
            with (mock.patch.object(scratch, 'end', return_value=scratch.TaskCleanup()),
                  ThreadPoolExecutor(max_workers=1) as pool,
                  (registry / 'records.lock').open('a') as lock):
                fcntl.flock(lock, fcntl.LOCK_EX)
                cleanup = pool.submit(mac_gate.stop_checkout, folder)
                try:
                    with self.assertRaises(TimeoutError):
                        cleanup.result(timeout=2 * mac_gate.checkout_cleanup_seconds())
                    self.assertTrue(cleanup.done(), 'The remote cleanup waited beyond its deadline')
                    self.assertTrue(record.exists())
                    self.assertTrue((folder / 'checkout.json').exists())
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            with mock.patch.object(scratch, 'end', return_value=scratch.TaskCleanup()):
                mac_gate.stop_checkout(folder)
            self.assertFalse(record.exists())
            self.assertFalse(folder.exists())

    def remote_checkout_environment(self) -> dict[str, str]:
        installed = self.root / '.claude/scripts'
        installed.parent.mkdir(exist_ok=True)
        installed.symlink_to(Path(mac_gate.__file__).resolve().parents[1], target_is_directory=True)
        return {**queue_fixture(self.root, LOCAL_MAC_SSH), 'MAC_QUEUE_HOST': 'fixture-mac'}

    def test_remote_settlement_stops_stalled_fallback_before_returning(self) -> None:
        environment = self.remote_checkout_environment()
        _ = write_config_fixture(self.root / 'config/mac_gate.conf', 'terminate_grace_seconds=0.2\n')
        _ = write_config_fixture(self.root / 'config/build_admission.conf', 'program_stop_seconds=3.6\n')
        folder = self.root / 'mac-gate-stalled'
        remote = self.root / mac_gate.REMOTE_ROOT / folder.name
        remote.mkdir(parents=True)
        record = self.root / mac_gate.REMOTE_PREPARATION / folder.name
        marker = self.root / 'removal-started'
        remove = self.root / 'rm'
        _ = remove.write_text(f'#!{sys.executable}\n' + """import os, signal, sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / '.claude'))
from scripts.lib.process_start import Started, process_start

pid = os.getpid()
owner = process_start(pid)
assert isinstance(owner, Started)
Path(os.environ['FAKE_QUEUE_STATE'], 'removal-started').write_text(f'{pid} {owner.start}')
signal.pause()
""")
        remove.chmod(0o755)
        output = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
            budget = mac_gate.checkout_cleanup_seconds()
            started = time.monotonic()
            try:
                self.assertEqual(mac_gate.lifecycle('cleanup', self.root / 'argv.json', folder, '1'), 1)
                self.assertTrue(marker.exists(), output.getvalue())
                pid, stamp = marker.read_text().split()
                self.assertNotEqual(process_start(int(pid)), Started(stamp), 'Remote removal outlived cleanup')
                self.assertLess(time.monotonic() - started, budget)
                self.assertIn(mac_gate.CLEANUP_ERROR_PREFIX + folder.name, output.getvalue())
                self.assertIn('The Mac remote checkout cleanup timed out', output.getvalue())
                self.assertEqual(record.read_bytes(), b'closed')
                self.assertTrue(remote.exists())
                with record.open('r') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if marker.exists():
                    pid, stamp = marker.read_text().split()
                    if process_start(int(pid)) == Started(stamp):
                        os.kill(int(pid), signal.SIGKILL)

    def test_bundle_git_writes_stay_in_the_callback_group(self) -> None:
        clone = self.root / 'mac-checkout'
        _ = gate.checked_git(self.root, 'clone', '-q', str(self.repo), str(clone))
        self.add_module(module_job('added'))
        _ = commit_repository(self.repo, 'added module')
        folder = self.root / 'mac-gate-bundle'
        request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
        cloud_gate.save(folder / 'request.json', request)
        argv_file = self.root / 'argv.json'
        real_git = shutil.which('git')
        assert real_git is not None
        shim = self.root / 'git'
        log = self.root / 'groups.jsonl'
        source = ("import json, os, sys\n"
                  + f"with open({str(log)!r}, 'a') as rows:\n"
                  + "    rows.write(json.dumps([os.getpgrp(), sys.argv[1:]]) + '\\n')\n"
                  + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
        _ = shim.write_text(f"#!{sys.executable}\n" + source)
        shim.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}):
            self.exercise_bundle(clone, folder, argv_file, request, expected=True)
        rows = [cast(tuple[int, list[str]], json.loads(line)) for line in log.read_text().splitlines()]
        writes = [(group, words) for group, words in rows if words[0] in ('update-ref', 'bundle')]
        self.assertTrue(any(words[:2] == ['bundle', 'create'] for _, words in writes))
        self.assertTrue(any(words[0] == 'update-ref' and words[1] != '-d' for _, words in writes))
        self.assertEqual({group for group, _ in writes}, {os.getpgrp()}, 'Bundle Git left the callback group')

    def test_cleanup_ssh_uses_a_deadline(self) -> None:
        folder = self.root / 'mac-gate-attempt'
        process = StalledCleanupProcess(mac_gate.checkout_cleanup_seconds(), exits_after_kill=True)

        def kill_process(_pid: int, _signal: signal.Signals) -> None:
            process.kill()

        with (mock.patch.dict(os.environ, {'MAC_QUEUE_HOST': 'fixture-mac'}),
              mock.patch.object(subprocess, 'Popen', return_value=process),
              mock.patch.object(process_group, 'send_process_group',
                                side_effect=kill_process) as kill):
            self.assertEqual(mac_gate.lifecycle('cleanup', self.root / 'argv.json', folder, '1'), 1)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(len(process.wait_deadlines), 1)

    def test_registered_release_bounds_block_watcher_start(self) -> None:
        paths = claim_state.StatePaths(self.root / 'provider', 'mac')
        identity = claim_state.ClaimIdentity('claim', 'episode', 'attempt')
        budget = conf.read('mac_gate').number('terminate_grace_seconds')
        process = StalledCleanupProcess(budget, exits_after_kill=False)
        warning = io.StringIO()
        with (mock.patch.object(mac_test, 'read_block', return_value=mock.Mock(spec=claim_state.PendingMacBlock)),
              mock.patch.dict(os.environ, {'MAC_TEST_MESSAGE_TIMEOUT_S': str(budget)}),
              mock.patch.object(subprocess, 'Popen', return_value=process),
              mock.patch.object(subprocess, 'run', side_effect=AssertionError('Watcher start has an implicit wait')),
              mock.patch.object(process_group, 'send_process_group') as kill,
              redirect_stderr(warning)):
            released = mac_test.release_exact_claims(paths, os.getpid(), identity, '', wait_for_provider_lock=False)
        self.assertEqual(released, mac_test.ClaimsReleased(0, ()))
        self.assertIn('block watcher could not be started', warning.getvalue())
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(len(process.wait_deadlines), 1)

    def test_ordinary_leftover_check_uses_bounded_reaping(self) -> None:
        connection = eligibility.read_connection_settings(eligibility.DEFAULT_MAC_SETTINGS)
        process = StalledCleanupProcess(connection.leftover_timeout_s, exits_after_kill=False)
        with (mock.patch.object(subprocess, 'Popen', return_value=process),
              mock.patch.object(subprocess, 'run', side_effect=AssertionError('SSH cleanup has an implicit wait')),
              mock.patch.object(process_group, 'send_process_group') as kill):
            result = offload.check_leftovers(connection, 'fixture-mac', eligibility.KnownRepository('hana'))
        self.assertIsInstance(result, offload.LeftoverCheckFailed)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(len(process.wait_deadlines), 1)

    def test_unreaped_leftover_check_keeps_release_unresolved(self) -> None:
        connection = eligibility.read_connection_settings(eligibility.DEFAULT_MAC_SETTINGS)
        process = StalledCleanupProcess(connection.leftover_timeout_s, exits_after_kill=False)
        with (mock.patch.object(subprocess, 'Popen', return_value=process),
              mock.patch.object(subprocess, 'run', side_effect=AssertionError('SSH cleanup has an implicit wait')),
              mock.patch.object(process_group, 'send_process_group') as kill):
            observed = offload.read_leftovers(connection, 'fixture-mac', 'fixture cleanup', 'mac')
        self.assertIsInstance(observed.result, offload.LeftoverCheckFailed)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(len(process.wait_deadlines), 1)

    def test_unreaped_gate_and_recovery_callback_have_bounded_waits(self) -> None:
        budget = mac_gate.checkout_cleanup_seconds()
        for path in ('gate', 'recovery'):
            with self.subTest(path=path):
                process = StalledCleanupProcess(budget, exits_after_kill=False)
                with (mock.patch.object(subprocess, 'Popen', return_value=process),
                      mock.patch.object(process_group, 'send_process_group') as kill_group):
                    with self.assertRaisesRegex(ValueError, 'cleanup deadline'):
                        if path == 'gate':
                            with ExitStack() as lifetime, mac_gate.queue_call(['fixture'], [], [], lifetime, budget):
                                pass
                        else:
                            _ = mac_queue_run.run_callback('fixture', 'cleanup', self.root / 'argv.json', [], {},
                                                           timeout=budget)
                if path == 'gate':
                    kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
                else:
                    self.assertEqual(kill_group.call_args_list,
                                     [mock.call(process.pid, signal.SIGTERM), mock.call(process.pid, signal.SIGKILL)])

    def test_queue_refusal_reports_the_last_nonblank_line(self) -> None:
        self.assertEqual(self.run_case('refused', module_job('one')),
                         'The Mac queue call ended with status 2: mac queue: the Mac settings are invalid')

    def test_queue_cleanup_failure_preserves_the_last_nonblank_line(self) -> None:
        self.assertEqual(self.run_case('cleanup-refused', module_job('one')),
                         'The Mac kept its gate folder: mac queue: the Mac run may still be live')

    def test_uploaded_entry_point_starts_with_the_installed_scripts(self) -> None:
        entry = self.root / 'uploaded/mac_gate.py'
        entry.parent.mkdir()
        _ = shutil.copyfile(Path(mac_gate.__file__), entry)
        home = self.root / 'mac-home'
        home.mkdir()
        installed = os.environ.get('MAC_GATE_TEST_INSTALL')
        if installed:
            command = [sys.executable, str(entry)]
        else:
            installed = str(Path(__file__).resolve().parents[2])
            command = [sys.executable, '-c', UPLOADED_MAC_ENTRY, str(entry)]
        result = subprocess.run([*command, 'stop',
                                 str(home / mac_gate.REMOTE_ROOT / 'mac-gate-fixture')],
                                cwd=home, env={**os.environ, 'HOME': str(home), 'PYTHONPATH': installed},
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_identity_mismatch_uses_natedev(self) -> None:
        for case, reason in (('attempt', 'attempt'), ('tree', 'tree'), ('commit', 'commit'),
                             ('modules', 'module list')):
            with self.subTest(case=case):
                result = self.run_case(case, module_job('one'))
                assert isinstance(result, str), result
                self.assertIn(reason, result)

    def test_bundle_supplies_a_missing_commit_and_base(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, fixture_git_environment(self.root), clear=True))
        old = gate.checked_git(self.repo, 'rev-parse', 'HEAD')
        clones = {kind: self.root / f'mac scratch {kind}' for kind in ('behind', 'dirty', 'current')}
        for kind in ('behind', 'dirty'):
            _ = gate.checked_git(self.repo, 'clone', '--quiet', str(self.repo), str(clones[kind]))
        base = commit_repository(self.repo, 'new main', allow_empty=True)
        _ = gate.checked_git(self.repo, 'update-ref', 'refs/remotes/origin/main', base)
        _ = gate.checked_git(self.repo, 'clone', '--quiet', str(self.repo), str(clones['current']))
        origin = 'git@fixture.invalid:fixture/project.git'
        _ = gate.checked_git(self.repo, 'remote', 'set-url', 'origin', origin)
        for kind, clone in clones.items():
            with self.subTest(kind=kind):
                folder = self.root / f'mac-gate-{kind}'
                if kind == 'dirty':
                    _ = (self.repo / 'scripts/fixture/test_one.py').write_text('# dirty fixture\n')
                request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
                cloud_gate.save(folder / 'request.json', request)
                cloud_gate.save(folder / 'checkout.json', {'path': str(clone)})
                _ = (self.repo / 'scripts/fixture/test_one.py').write_text('# fixture\n')
                self.exercise_bundle(clone, folder, folder / 'argv.json', request, expected=kind != 'current')

                bundle = folder / 'source.bundle'
                if kind != 'current':
                    heads = gate.checked_git(self.repo, 'bundle', 'list-heads', str(bundle))
                    self.assertIn(str(request['commit']), heads)
                    self.assertIn(base, heads)
                    self.assertIn(old, gate.checked_git(clone, 'bundle', 'verify', str(bundle)))
                with mock.patch.object(os, 'execv') as start_guest:
                    mac_gate.stage_checkout(folder, '1')
                start_guest.assert_called_once()
                self.assertEqual(gate.checked_git(clone, 'rev-parse', 'HEAD'), request['commit'])
                self.assertEqual(gate.checked_git(clone, 'rev-parse', 'origin/main'), base)
                self.assertEqual(gate.checked_git(clone, 'remote', 'get-url', 'origin'), origin)

    def test_interrupted_ref_writes_keep_the_plan_until_deletion_succeeds(self) -> None:
        clone = self.root / 'mac-checkout'
        _ = gate.checked_git(self.repo, 'clone', '-q', str(self.repo), str(clone))
        self.add_module(module_job('two'))
        _ = commit_repository(self.repo, 'New commit for the Mac')
        directory = self.root / mac_gate.QUEUE_RUNS / 'reffailure'
        directory.mkdir(parents=True)
        (directory / 'lock').touch()
        journal = directory / 'run.json'
        # The bundle ref plan belongs beside the other files for this callback.
        folder = directory / 'mac-gate-reffailure'
        request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
        cloud_gate.save(folder / 'request.json', request)
        cloud_gate.save(journal, {
            'host': 'fixture-mac', 'lifecycle_script': str(Path(mac_gate.__file__)), 'arguments': [str(folder), '1'],
        })
        git = gate.git
        first_ref = 'refs/cloud-gates/' + folder.name
        next_ref = first_ref + '-1'
        refs_journal = directory / 'argv.json.bundle-refs'
        plans_at_write: dict[str, dict[str, object]] = {}

        def interrupt_ref_write(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args[0] == 'update-ref' and args[1] != '-d':
                plans_at_write[args[1]] = cloud_gate.read(refs_journal) if refs_journal.exists() else {}
                if args[1] == next_ref:
                    raise KeyboardInterrupt()
            if args[:3] == ('update-ref', '-d', first_ref):
                return subprocess.CompletedProcess(args, 1, '', 'fixture ref deletion failed')
            return git(repo, *args)

        def cleanup(_script: str, phase: str, argv: Path, _arguments: list[str],
                    environment: dict[str, str], **_options: object) -> int:
            with mock.patch.dict(os.environ, environment):
                try:
                    return mac_gate.lifecycle(phase, argv, folder, '1')
                except GateRefused:
                    return 1

        environment = self.remote_checkout_environment()
        with (mock.patch.dict(os.environ, environment),
              mock.patch.object(mac_queue_run, 'run_callback', side_effect=cleanup)):
            copies.remote_registry().mkdir(parents=True)
            with (mock.patch.object(gate, 'git', side_effect=interrupt_ref_write),
                  mock.patch.object(mac_gate, 'bounded_bundle_git', side_effect=interrupt_ref_write, create=True)):
                with self.assertRaisesRegex(GateRefused, 'update-ref -d|Git did not remove'):
                    self.exercise_bundle(clone, folder, directory / 'argv.json', request, expected=True)
                self.assertEqual(tuple(plans_at_write), (first_ref, next_ref))
                for plan in plans_at_write.values():
                    self.assertEqual(plan, {'repository': str(self.repo.resolve()), 'refs': [first_ref, next_ref]},
                                     'The Git ref plan was missing when update-ref started')
                self.assertEqual(gate.checked_git(self.repo, 'for-each-ref', '--format=%(refname)', 'refs/cloud-gates'),
                                 first_ref)
                mac_queue_run.clean_ended_runs(dict(os.environ))
                self.assertTrue(journal.exists(), 'The queue lost the only plan for the Git refs')
                self.assertTrue((folder / 'local-repo').is_file())
                self.assertTrue(refs_journal.is_file())
            mac_queue_run.clean_ended_runs(dict(os.environ))
            self.assertFalse(journal.exists())
            self.assertEqual(gate.checked_git(self.repo, 'for-each-ref', 'refs/cloud-gates'), '')

    def test_delayed_fence_initialization_refuses_after_ssh_ends(self) -> None:
        identity = claim_state.ClaimIdentity('claim-mac', 'episode-mac', 'attempt-mac')
        prepare = offload.prepare_start_fence_command(identity, 'mac')
        command_file = self.root / 'in-flight-command'
        fixture_ssh = (
            'import sys\n'
            'from pathlib import Path\n'
            'from scripts.lib.atomic_file import write_text_atomic\n'
            'write_text_atomic(Path(sys.argv[1]), sys.argv[2])\n'
            'sys.stdin.read()\n'
        )
        environment = {**os.environ, 'HOME': str(self.root)}
        with subprocess.Popen([sys.executable, '-c', fixture_ssh, str(command_file), prepare],
                              env=environment, stdin=subprocess.PIPE) as transport:
            try:
                wait_until(lambda: command_file.exists() or transport.poll() is not None,
                           self.root, processes=(transport,))
                self.assertTrue(command_file.exists())
            finally:
                transport.kill()
                _ = transport.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(transport.returncode, -signal.SIGKILL)
        settle = offload.start_fence_command(identity, 'mac', settle=True)
        closed = subprocess.run(['/bin/sh', '-c', settle], capture_output=True, text=True, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertIn('dispatch_start_settled=', closed.stdout)
        late = subprocess.run(['/bin/sh', '-c', command_file.read_text()], capture_output=True, text=True,
                              env=environment, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(late.returncode, 0, 'Delayed SSH created a fence after settlement')
        self.assertFalse((self.root / offload.start_fence_relative(identity, 'mac').with_suffix('.lock')).exists())

    def test_late_remote_preparation_cannot_create_a_cleaned_request(self) -> None:
        folder = self.root / 'mac-gate-delayed'
        request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
        cloud_gate.save(folder / 'request.json', request)
        _ = (folder / 'local-repo').write_text(str(self.repo))
        argv = folder / 'argv.json'
        commands: list[str] = []

        def delay_mkdir(command: list[str], **_kwargs: object) -> str:
            self.assertIn('mkdir --', command[-1])
            commands.append(command[-1])
            raise GateRefused('The fixture disconnected before remote mkdir')

        environment = self.remote_checkout_environment()
        remote = self.root / mac_gate.REMOTE_ROOT / folder.name
        with mock.patch.dict(os.environ, environment):
            with (mock.patch.object(copies, 'source_owner', return_value='fixture-owner'),
                  mock.patch.object(copies, 'repository_name', return_value='fixture-repo'),
                  mock.patch.object(subprocess, 'check_output', side_effect=delay_mkdir)):
                with self.assertRaisesRegex(GateRefused, 'disconnected'):
                    _ = mac_gate.lifecycle('prepare', argv, folder, '1')
            self.assertEqual(mac_gate.lifecycle('cleanup', argv, folder, '1'), 0)
            self.assertFalse(remote.exists())
            command, = commands
            late = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True,
                                  timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertFalse(remote.exists(), late.stdout + late.stderr)
            self.assertNotEqual(late.returncode, 0)

    def exercise_bundle(self, clone: Path, folder: Path, argv_file: Path,
                        request: dict[str, object], *, expected: bool) -> None:
        _ = (folder / 'local-repo').write_text(str(self.repo))
        _ = (folder / 'module_seconds.json').write_text('{}')
        captured = subprocess.run

        def reply(command: list[str], *, text: bool, pass_fds: tuple[int, ...]) -> str:
            self.assertTrue(text)
            self.assertEqual(pass_fds, (creation.fileno(),))
            remote = command[-1]
            if 'cat-file' in remote:
                return captured(['/bin/sh', '-c', remote], capture_output=True, check=True, text=True).stdout
            if ' clone ' in remote:
                self.assertIn('fixture-owner fixture-project', remote)
                return f'mac_gate_checkout={clone}'
            return 'created'

        sends: list[list[str]] = []

        def send(command: list[str], *, check: bool, pass_fds: tuple[int, ...],
                 stdin: TextIO | BinaryIO | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(check)
            self.assertEqual(pass_fds, (creation.fileno(),))
            if stdin is not None:
                sends.append([str(stdin.name)])
            else:
                sends.append(command)
            return subprocess.CompletedProcess(command, 0)

        lease = self.root / 'lease'
        with ((folder / 'creation.lock').open('w') as creation,
              mock.patch.dict(os.environ, {'MAC_QUEUE_HOST': 'fixture-mac',
                                           mac_gate.CREATION_LEASE_FD: str(creation.fileno())}),
              mac_gate.hold_gate_creation_commands(),
              mock.patch.object(copies, 'source_owner', return_value='fixture-owner'),
              mock.patch.object(copies, 'repository_name', return_value='fixture-project'),
              mock.patch.object(copies, 'remote_lease_path', return_value=lease),
              mock.patch.object(copies, 'register_remote') as register,
              mock.patch.object(subprocess, 'check_output', side_effect=reply),
              mock.patch.object(subprocess, 'run', side_effect=send)):
            self.assertEqual(mac_gate.lifecycle('prepare', argv_file, folder, '1'), 0)
        register.assert_called_once()
        self.assertEqual(argv_file.with_suffix('.json.checkout').read_text(), str(clone))
        self.assertEqual(argv_file.with_suffix('.json.lease').read_text(), str(lease))
        sources = [source for send in sends[1:] for source in send]
        self.assertIn(str(folder / 'request.json'), sources)
        self.assertEqual(str(folder / 'source.bundle') in sources, expected)
        self.assertIn(' stage ', argv_file.read_text())
        self.assertIn(' remove-checkout ', argv_file.with_suffix('.json.close').read_text())
        self.assertEqual(request['source_key'], '')

    def test_checkout_probe_rejects_a_nonzero_remote_status(self) -> None:
        folder = self.root / 'mac-gate-fixture'
        request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
        error = subprocess.CalledProcessError(3, ['ssh'], output=f"{request['commit']}\npresent\npresent\n")
        with (mock.patch.object(subprocess, 'check_output', side_effect=error),
              self.assertRaises(subprocess.CalledProcessError)):
            _ = mac_gate.checkout_bundle(['ssh', 'fixture-mac'], str(self.repo), self.repo, folder, request)
        self.assertFalse((folder / 'source.bundle').exists())

    def test_aws_verdict_error_precedes_module_error(self) -> None:
        request: dict[str, object] = {'attempt': 'fixture', 'commit': 'commit', 'tree': 'tree',
                                      'modules': ['scripts/fixture/test_one.py']}
        result = {**request, 'gate': gate_box.encode_run(gate.GateRun((), 1, 0)), 'verdict': 'invalid'}
        with self.assertRaisesRegex(ValueError, 'no complete verdict and log'):
            cloud_gate.validate_result(result, request)

    def test_dead_guest_cleanup_requires_group_identity(self) -> None:
        root = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-fixture'
        root.mkdir(parents=True)
        guest, job, unrelated = (self.sleeping_process() for _ in range(3))
        owner = process_start(guest.pid)
        assert isinstance(owner, Started)
        recovery = mac_gate.MacGateRecovery(root / 'run.json', str(self.repo), guest.pid, owner.start)
        temporary = self.root / 'gate-temporary'
        temporary.mkdir()
        recovery.folders.append(str(temporary))
        with mock.patch.object(gate, 'process_start', return_value=Unreadable('fixture')):
            recovery.add_group(job.pid)
        self.assertEqual(recovery.groups, {job.pid: ''})
        guest.kill()
        _ = guest.wait(timeout=TEST_HANG_GUARD_SECS)
        with (mock.patch.object(Path, 'home', return_value=self.root),
              mock.patch.object(scratch, 'end', return_value=scratch.TaskCleanup()) as end):
            with self.assertRaisesRegex(GateRefused, 'Cannot prove ownership'):
                mac_gate.stop_checkout(root)
            end.assert_not_called()
            self.assertIsNone(job.poll())
            self.assertTrue(temporary.exists())
            recovery.add_group(job.pid)
            mac_gate.stop_checkout(root)
        end.assert_called_once_with('gate:mac-gate-fixture')
        self.assertEqual(job.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.assertIsNone(unrelated.poll())
        self.assertFalse(temporary.exists())
        self.assertFalse(root.exists())

    def install_held_module(self) -> None:
        shim = self.repo / 'scripts/lib/py'
        shim.parent.mkdir()
        shim.symlink_to(sys.executable)
        runner = self.repo / 'scripts/test_times/module_run.py'
        runner.parent.mkdir()
        _ = runner.write_text(HELD_MODULE)
        _ = commit_repository(self.repo, 'gate fixture')

    def test_run_remote_cancellation_runs_the_mac_close_hook(self) -> None:
        self.install_held_module()
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                self.cancel_remote(Path(temporary), explicit=explicit)

    def test_run_remote_cancellation_reports_a_close_failure(self) -> None:
        self.install_held_module()
        self.cancel_remote(self.root, explicit=True, cleanup_refused=True)

    def test_run_remote_cancellation_stops_bundle_git_and_removes_refs(self) -> None:
        self.cancel_remote(self.root, explicit=True, prepare_bundle=True)

    def cancel_remote(self, root: Path, *, explicit: bool, cleanup_refused: bool = False,
                     prepare_bundle: bool = False) -> None:
        environment = {**queue_fixture(root, LOCAL_MAC_SSH),
                       **copies_environment(root),
                       'PATH': os.pathsep.join((str(root), os.defpath)),
                       'MAC_GATE_FIXTURE_ROOT': str(Path(__file__).resolve().parents[2])}
        gate_root = root / 'gate-root'
        gate_root.mkdir()
        _ = write_config_fixture(root / 'config/build_folders.conf', f'roots.gate={gate_root}\n')
        callback = root / 'callback'
        _ = callback.write_text(f'#!{sys.executable}\n' + LOCAL_MAC)
        callback.chmod(0o755)
        if cleanup_refused:
            (root / 'close_refusal').touch()
        git = shutil.which('git')
        assert git is not None
        executable = Path(git).absolute()
        shim = root / 'git'
        if prepare_bundle:
            environment['FIXTURE_REAL_GIT'] = str(executable)
            (root / 'prepare_bundle').touch()
            _ = shim.write_text(f'#!{sys.executable}\n' + HELD_BUNDLE_GIT)
            shim.chmod(0o755)
        else:
            shim.symlink_to(executable)
        reader, writer = os.pipe()
        try:
            caller = [sys.executable, '-c', CALL_RUN_REMOTE, str(self.repo), str(callback),
                      str(reader) if explicit else '']
            result = root / 'route.log'
            with result.open('w+') as output, subprocess.Popen(
                caller, env=environment, stdout=output, stderr=output, pass_fds=(reader,) if explicit else (),
            ) as process:
                try:
                    marker = root / ('bundle_pid' if prepare_bundle else 'guest_ready')
                    wait_until(lambda: marker.exists() or process.poll() is not None, root, processes=(process,))
                    self.assertTrue(marker.exists(), result.read_text())
                    child_pid = int(marker.read_text())
                    if prepare_bundle:
                        self.assertTrue(gate.checked_git(self.repo, 'for-each-ref', 'refs/cloud-gates/'))
                    if explicit:
                        self.assertEqual(os.write(writer, b'x'), 1)
                    else:
                        process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0 if explicit else -signal.SIGTERM,
                                     result.read_text())
                    if explicit:
                        message = 'The Mac kept its gate folder' if cleanup_refused else 'The Mac gate was cancelled'
                        self.assertTrue(any(line.startswith(message) for line in result.read_text().splitlines()),
                                        result.read_text())
                    if prepare_bundle:
                        self.assertFalse(isinstance(process_start(child_pid), Started), result.read_text())
                        self.assertEqual(gate.checked_git(self.repo, 'for-each-ref', 'refs/cloud-gates/'), '')
                        return
                    wait_until((root / 'close_finished').exists, root)
                    self.assertTrue((root / 'close_started').exists())
                    self.assertFalse(isinstance(process_start(child_pid), Started))
                    record = cloud_gate.read(root / 'interrupted')
                    self.assertEqual(record['groups'], {})
                    self.assertEqual(record['folders'], [])
                finally:
                    if process.poll() is None:
                        process.kill()
        finally:
            os.close(reader)
            os.close(writer)

    def sleeping_process(self) -> subprocess.Popen[bytes]:
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True, cwd=self.root)
        self.addCleanup(process.wait, TEST_HANG_GUARD_SECS)
        self.addCleanup(process.kill)
        return process

    def test_missing_empty_and_absent_module_list_entries(self) -> None:
        path = self.repo / mac_gate.MODULE_LIST
        missing = mac_gate.read_modules(self.repo)
        _ = path.write_text('\n')
        empty = mac_gate.read_modules(self.repo)
        self.assertNotEqual(missing, empty)

        from scripts.production.gate_routes import MacGateExcludeFileMissing, MacGateExclusions

        self.assertIsInstance(missing, MacGateExcludeFileMissing)
        self.assertEqual(empty, MacGateExclusions(()))
        _ = path.write_text('scripts/fixture/test_absent.py\nscripts/fixture/test_one.py\n')
        self.assertEqual(mac_gate.read_modules(self.repo), MacGateExclusions(('scripts/fixture/test_one.py',)))

    def test_list_writer_excludes_failures_and_count_mismatches(self) -> None:
        mac = (module_job('one'), module_job('tests', tests=11), module_job('skips', skips=1),
               module_job('mac_failure', status=1), module_job('natedev_failure'))
        for job in mac[1:]:
            self.add_module(job)
        _ = commit_repository(self.repo, 'modules')
        tree, _dirty = cloud_gate.working_tree(self.repo)
        baseline_path = self.root / 'natedev.jsonl'
        baseline = (module_job('one'), module_job('tests'), module_job('skips'),
                    module_job('mac_failure'), module_job('natedev_failure'))
        natedev_jobs = (*baseline, replace(baseline[-1], returncode=1))
        row: dict[str, object] = {'kind': 'gate_run', 'host': 'natedev', 'tree': tree, 'selection': 'full',
               'dirty': False, 'aborted': False,
               'modules': gate_record.module_runs(gate_box.execution_records(gate.GateRun(natedev_jobs, 1, 0)), 1)}
        store.append_line(baseline_path, row)
        result = self.root / 'mac.json'
        _ = result.write_text(json.dumps({'tree': tree, 'gate': gate_box.encode_run(gate.GateRun(mac, 1, 0))}))
        mac_gate.write_modules(self.repo, result, baseline_path)
        self.assertEqual(
            (self.repo / mac_gate.MODULE_LIST).read_text(),
            mac_gate.MODULE_LIST_HEADER
            + 'scripts/fixture/test_mac_failure.py\n'
            + 'scripts/fixture/test_natedev_failure.py\n'
            + 'scripts/fixture/test_skips.py\n'
            + 'scripts/fixture/test_tests.py\n',
        )

    def test_list_writer_refuses_a_record_from_another_tree(self) -> None:
        result = self.root / 'mac.json'
        _ = result.write_text(json.dumps({'tree': self.tree, 'gate': gate_box.encode_run(gate.GateRun((), 1, 0))}))
        baseline_path = self.root / 'natedev.jsonl'
        store.append_line(baseline_path, {'kind': 'gate_run', 'host': 'natedev', 'tree': 'wrong',
                                         'selection': 'full', 'dirty': False, 'aborted': False})
        with self.assertRaisesRegex(GateRefused, 'No complete natedev gate record'):
            mac_gate.write_modules(self.repo, result, baseline_path)
        self.assertFalse((self.repo / mac_gate.MODULE_LIST).exists())
