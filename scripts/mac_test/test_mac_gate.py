"""Exercise the Mac gate route and the module list with private fixtures."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
import signal
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box
from scripts.buildlog import store
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.direct.wait_for_test_state import wait_until
from scripts.lint import folder_audit, tmp_sweep
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib import scratch
from scripts.lib.checkout_test_support import write_stdlib_tool
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.process_start import Started, Unreadable, process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import mac_gate
from scripts.mac_test.test_mac_queue_run import queue_fixture
from scripts.production import gate
from scripts.production.gate_modules import GateRefused
from scripts.test_times import gate_record

FAKE_QUEUE = """\
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
assert args[args.index('--kind') + 1] == 'run'
assert Path(args[args.index('--lifecycle-script') + 1]).is_file()
folder = Path(args[-2])
request = json.loads((folder / 'request.json').read_text())
assert (folder / 'module_seconds.json').is_file()
assert (folder / 'source.bundle').is_file() == bool(request['source_key'])
case = os.environ['MAC_GATE_CASE']
if case != 'cleanup-refused':
    assert '--no-wait' not in args
if case == 'timeout':
    time.sleep(60)
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
if case == 'timed':
    row.update(started_at='2026-01-01T00:00:00Z', vcpus=12)
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
import os, shlex, signal, subprocess, sys
command = shlex.split(sys.argv[-1])
environment = {
    # Keep the fixture PATH when the NixOS startup file runs.
    **os.environ, '__ETC_ZSHENV_SOURCED': '1',
    'ZDOTDIR': os.environ['FAKE_QUEUE_STATE'],
}
signal.signal(signal.SIGINT, signal.SIG_DFL)
raise SystemExit(subprocess.run(command, env=environment, start_new_session=True).returncode)
"""

LOCAL_MAC = """\
import json, os, shlex, shutil, sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ['MAC_GATE_FIXTURE_ROOT'])
from scripts.lib import scratch
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
    close = shlex.join([*command, 'close'])
    log = shlex.quote(str(root / 'close.log'))
    status = shlex.quote(str(root / 'close_status'))
    pending = shlex.quote(str(root / 'close_status.new'))
    # The close hook runs under zsh with the fixture PATH, which has no mv.
    argv_file.with_suffix('.json.close').write_text(
        f'{close} >{log} 2>&1; rc=$?; printf "%s" "$rc" >{pending}; {shlex.quote(sys.executable)} -c "import os, sys; os.replace(*sys.argv[1:])" {pending} {status}; exit "$rc"')
elif phase == 'guest':
    try:
        mac_gate.run_guest(folder, full=True)
    except KeyboardInterrupt:
        record = cloud_gate.read(folder / 'run.json')
        cloud_gate.save(root / 'interrupted', record)
        raise
else:
    if phase == 'close':
        (root / 'close_started').touch()
    if phase == 'cleanup' and (root / 'close_refusal').exists():
        source = Path(sys.argv[3])
        print(f'{mac_gate.CLEANUP_ERROR_PREFIX}{source.name}:The Mac kept its gate folder', flush=True)
        raise SystemExit(1)
    if phase == 'close' and (root / 'fail_close_hook').exists():
        raise RuntimeError('fixture close hook failed')
    kept = []
    if (folder / 'checkout.json').exists():
        kept = [scratch.KeptFolder(mac_gate.read_checkout(folder), 'fixture task ended')]
    with patch.object(scratch, 'end', return_value=kept):
        mac_gate.stop_checkout(folder)
"""

QUEUE_SIGNAL_TO_SENDER = """\
import os, runpy, signal, sys, threading

original_kill = os.kill

def kill_on_sender(pid, signum):
    if (pid == os.getpid() and signum == signal.SIGINT
            and threading.current_thread() is not threading.main_thread()):
        signal.pthread_kill(threading.get_ident(), signum)
    else:
        original_kill(pid, signum)

os.kill = kill_on_sender
entry = os.environ.pop('MAC_GATE_QUEUE_ENTRY')
sys.argv[0] = entry
runpy.run_path(entry, run_name='__main__')
"""

CALL_ROUTE = """\
import os, signal, sys
from pathlib import Path

signal.signal(signal.SIGINT, signal.SIG_DFL)
sys.path.insert(0, os.environ['MAC_GATE_FIXTURE_ROOT'])
from scripts.mac_test import mac_gate
from scripts.production.gate_routes import SuiteMode
from scripts.production import gate_routes

repo, callback, fd = sys.argv[1:]
mac_gate.__file__ = callback
if wrapper := os.environ.pop('MAC_GATE_QUEUE_WRAPPER', ''):
    os.environ['MAC_GATE_QUEUE_ENTRY'] = mac_gate.QUEUE_COMMAND[1]
    mac_gate.QUEUE_COMMAND = (sys.executable, wrapper)
cancellation = gate_routes.MacGateCancellation(int(fd)) if fd else mac_gate.USE_CALLER_PROCESS_LIFETIME
print(mac_gate.route(Path(repo), mode=SuiteMode.GATE, full=True,
                     module_time_limit_s=30, cancellation=cancellation), flush=True)
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
path = root / 'module_pid'
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
class MacGateTests(unittest.TestCase):
    root = Path()
    repo = Path()
    tree = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(
            os.environ, {'GATE_STATE_DIR': str(self.root / 'gate-state')})))
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

    def run_case(self, case: str, job: gate.GateJobResult) -> gate.GateRun | str:
        command = self.root / 'queue.py'
        _ = command.write_text(FAKE_QUEUE)
        payload = self.root / 'gate.json'
        _ = payload.write_text(json.dumps(gate_box.encode_run(gate.GateRun((job,), 1, 0))))
        timeout = 0.2 if case == 'timeout' else 15.0
        with (mock.patch.dict(os.environ, {'MAC_GATE_CASE': case, 'MAC_GATE_RESULT': str(payload)}),
              mock.patch.object(mac_gate, 'QUEUE_COMMAND', (sys.executable, str(command))),
              mock.patch.object(mac_gate, 'MAC_TIME_LIMIT_S', timeout)):
            selection = gate.TreeSelection((module_path(job),), self.tree)
            return mac_gate.route(self.repo, mode=gate.SuiteMode.GATE, full=False,
                                  module_time_limit_s=30, selected=selection)

    def test_returns_pass_and_test_failure(self) -> None:
        for status in (0, 1):
            with self.subTest(status=status):
                job = module_job('one', status=status)
                result = self.run_case('completed', job)
                assert isinstance(result, gate.GateRun), result
                self.assertEqual(result.jobs, (job,))
                self.assertEqual(result.passed, status == 0)

    def test_validated_mac_result_updates_only_passing_module_times(self) -> None:
        _ = write_config_fixture(self.root / 'gate.conf', 'GATE_TIME_LIMIT_FACTOR=2')
        _ = write_config_fixture(self.root / 'test_times.conf', 'TOP_RUNS=5')
        with mock.patch.dict(os.environ, {'CONF_DIR': str(self.root), 'BUILDLOG_DIR': str(self.root / 'buildlog')}):
            for status in (1, 0):
                with self.subTest(status=status):
                    result = self.run_case('timed', module_job('one', status=status))
                    assert isinstance(result, gate.GateRun), result
                    limit = gate.read_time_limit(12, ('fixture/test_one.py',), 2, True)
                    if status:
                        self.assertIsInstance(limit, gate_record.NoGateTimeLimit)
                    else:
                        self.assertEqual(limit, gate_record.GateTimeLimit(0.2))
            self.assertIsInstance(gate.read_time_limit(12, (), 12, False), gate_record.NoGateTimeLimit)

    def test_queue_failure_uses_natedev(self) -> None:
        for case, reason in (('status', 'status 23'), ('timeout', 'exceeded'),
                             ('lost', 'no unique gate result')):
            with self.subTest(case=case):
                result = self.run_case(case, module_job('one'))
                assert isinstance(result, str), result
                self.assertIn(reason, result)

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
                request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.GATE, 30)
                cloud_gate.save(folder / 'request.json', request)
                cloud_gate.save(folder / 'checkout.json', {'path': str(clone)})
                _ = (self.repo / 'scripts/fixture/test_one.py').write_text('# fixture\n')
                _ = self.exercise_bundle(clone, folder, request, expected=kind != 'current')

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

    def test_remote_commands_set_pythonpath_in_zsh(self) -> None:
        folder = self.root / 'mac-gate-fixture'
        request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.GATE, 30)
        cloud_gate.save(folder / 'request.json', request)
        clone = self.exercise_bundle(self.repo, folder, request, expected=False)

        home = self.root / 'mac home'
        remote = home / mac_gate.REMOTE_ROOT / folder.name
        entry = remote / 'mac_gate.py'
        remote.mkdir(parents=True)
        entry.with_suffix('.py.new').touch()
        shim = home / '.claude/scripts/lib/py'
        shim.parent.mkdir(parents=True)
        write_stdlib_tool(shim, """\
import json, os, sys
print(json.dumps([os.environ["PYTHONPATH"], *sys.argv[1:]]))
""")
        environment = {**os.environ, 'HOME': str(home), 'ZDOTDIR': str(home),
                       'PYTHONPATH': str(self.root / 'local-pythonpath')}
        argv_file = folder / 'argv.json'
        stage = cast(list[str], json.loads(argv_file.read_text()))
        self.assertEqual(stage[:2], ['/bin/zsh', '-c'])
        stop = argv_file.with_suffix('.json.close').read_text()
        for action, command, arguments in (
            ('clone', ['/bin/zsh', '-c', clone], ['fixture-owner', 'fixture-project', folder.name]),
            ('stage', stage, ['1']),
            ('stop', ['/bin/zsh', '-c', stop], []),
        ):
            with self.subTest(action=action):
                result = subprocess.run(command, cwd=home, env=environment, capture_output=True,
                                        text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 [str(home / '.claude'), str(entry), action, str(remote), *arguments])

    def exercise_bundle(self, clone: Path, folder: Path, request: dict[str, object], *, expected: bool) -> str:
        argv_file = folder / 'argv.json'
        _ = (folder / 'local-repo').write_text(str(self.repo))
        captured = subprocess.run
        clone_commands: list[str] = []

        def reply(command: list[str], *, text: bool) -> str:
            self.assertTrue(text)
            remote = command[-1]
            if 'cat-file' in remote:
                return captured(['/bin/sh', '-c', remote], capture_output=True, check=True, text=True).stdout
            if ' clone ' in remote:
                self.assertIn('fixture-owner fixture-project', remote)
                clone_commands.append(remote)
                return f'mac_gate_checkout={clone}'
            return 'created'

        sends: list[list[str]] = []

        def send(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
            self.assertTrue(check)
            sends.append(command)
            return subprocess.CompletedProcess(command, 0)

        registered: list[copies.RemoteMirror] = []
        reconciled_hosts: list[str] = []

        def reconcile(host: str, select: Callable[[copies.RemoteMirror], bool]) -> None:
            self.assertEqual(len(registered), 1)
            self.assertIs(select, mac_gate.is_mac_gate_checkout)
            reconciled_hosts.append(host)

        lease = self.root / 'lease'
        with (mock.patch.dict(os.environ, {'MAC_QUEUE_HOST': 'fixture-mac', 'CODEX_THREAD_ID': folder.name}),
              mock.patch.object(copies, 'source_owner', return_value='fixture-owner'),
              mock.patch.object(copies, 'repository_name', return_value='fixture-project'),
              mock.patch.object(copies, 'remote_lease_path', return_value=lease),
              mock.patch.object(copies, 'register_remote', registered.append),
              mock.patch.object(copies, 'reconcile_remote_mirrors', reconcile),
              mock.patch.object(subprocess, 'check_output', side_effect=reply),
              mock.patch.object(subprocess, 'run', side_effect=send)):
            self.assertEqual(mac_gate.lifecycle('prepare', argv_file, folder, '1'), 0)
        self.assertEqual(reconciled_hosts, ['fixture-mac'])
        self.assertEqual(registered, [
            copies.RemoteMirror('fixture-owner', copies.SelectedRepository('fixture-project'),
                                'fixture-mac', str(clone), folder.name, task='gate:' + folder.name),
        ])
        self.assertEqual(argv_file.with_suffix('.json.checkout').read_text(), str(clone))
        self.assertEqual(argv_file.with_suffix('.json.lease').read_text(), str(lease))
        self.assertIn(str(folder / 'request.json'), sends[1])
        self.assertEqual(str(folder / 'source.bundle') in sends[1], expected)
        self.assertIn(' stage ', argv_file.read_text())
        self.assertIn(' stop ', argv_file.with_suffix('.json.close').read_text())
        self.assertEqual(request['source_key'], '')
        clone_command, = clone_commands
        return clone_command

    def test_checkout_probe_rejects_a_nonzero_remote_status(self) -> None:
        folder = self.root / 'mac-gate-fixture'
        request = cloud_gate.prepare_request(self.repo, folder, gate.SuiteMode.GATE, 30)
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
              mock.patch.object(scratch, 'end', return_value=[]) as end):
            with self.assertRaisesRegex(GateRefused, 'Cannot prove ownership'):
                _ = mac_gate.stop_checkout(root)
            end.assert_not_called()
            self.assertIsNone(job.poll())
            self.assertTrue(temporary.exists())
            recovery.add_group(job.pid)
            self.assertEqual(mac_gate.stop_checkout(root), mac_gate.CheckoutCleanup.REMOVED)
        end.assert_called_once_with('gate:mac-gate-fixture')
        self.assertEqual(job.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.assertIsNone(unrelated.poll())
        self.assertFalse(temporary.exists())
        self.assertFalse(root.exists())

    def test_retained_checkout_keeps_its_registration_until_removal(self) -> None:
        folder = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-fixture'
        folder.mkdir(parents=True)
        task = mac_gate.checkout_task(folder)
        with mock.patch.dict(os.environ, copies_environment(self.root)):
            checkout = scratch.new(task, 'checkout')
            cloud_gate.save(folder / 'checkout.json', {'path': str(checkout)})
            mirror = copies.RemoteMirror('fixture', copies.SelectedRepository('fixture'),
                                         'localhost', str(checkout), folder.name)
            with copies.remote_lease(mirror):
                copies.register_remote(mirror)
            registration = copies.remote_record_path(mirror)
            with redirect_stderr(StringIO()) as notices:
                for _ in range(2):
                    self.assertEqual(mac_gate.stop_checkout(folder), mac_gate.CheckoutCleanup.RETAINED)
                    self.assertEqual(mac_gate.read_checkout(folder), checkout)
                    self.assertTrue(registration.is_file())
            self.assertEqual(notices.getvalue().splitlines(), [
                scratch.retained_task_folder_notice(checkout, f'task {task} ended'),
            ])
            with self.assertRaisesRegex(GateRefused, 'checkout task has ended'), mac_gate.record_guest(folder, checkout):
                self.fail('The closed gate started a guest')
            shutil.rmtree(checkout)
            self.assertEqual(mac_gate.stop_checkout(folder), mac_gate.CheckoutCleanup.REMOVED)
            self.assertFalse(registration.exists())
            self.assertFalse(folder.exists())

    def test_stop_checkout_accepts_pre_promotion_task_cleanup(self) -> None:
        class LegacyTaskCleanup(list[Path]):
            def __init__(self, removed: Path, kept: scratch.KeptFolder) -> None:
                super().__init__([removed])
                self.kept: list[scratch.KeptFolder] = [kept]

        folder = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-fixture'
        folder.mkdir(parents=True)
        removed = self.root / 'removed'
        kept = self.root / 'kept'
        kept.mkdir()
        cloud_gate.save(folder / 'checkout.json', {'path': str(removed)})
        result = LegacyTaskCleanup(removed, scratch.KeptFolder(kept, 'fixture hold'))
        with (mock.patch.dict(os.environ, copies_environment(self.root)),
              mock.patch.object(scratch, 'end', return_value=result) as end,
              mock.patch.object(copies, 'unregister_remote') as unregister,
              redirect_stderr(StringIO()) as notices):
            for _ in range(2):
                self.assertEqual(mac_gate.stop_checkout(folder), mac_gate.CheckoutCleanup.RETAINED)
        end.assert_called_once_with(mac_gate.checkout_task(folder))
        unregister.assert_called_once_with('localhost', str(removed))
        self.assertEqual(notices.getvalue().splitlines(), [
            scratch.retained_task_folder_notice(kept, 'fixture hold'),
        ])
        self.assertTrue(kept.is_dir())
        self.assertEqual(cloud_gate.read(folder / 'task-ended.json'), {str(kept): 'fixture hold'})

    def test_lifecycle_unregisters_only_after_remote_removal(self) -> None:
        folder = self.root / 'mac-gate-fixture'
        argv = self.root / 'argv.json'
        checkout = '/fixture/remote/checkout'
        _ = argv.with_suffix('.json.checkout').write_text(checkout)
        notice = scratch.retained_task_folder_notice(Path(checkout), 'fixture task ended')
        retained = subprocess.CompletedProcess(['ssh'], 0, mac_gate.CheckoutCleanup.RETAINED + '\n', notice + '\n')
        removed = subprocess.CompletedProcess(['ssh'], 0, mac_gate.CheckoutCleanup.REMOVED + '\n', '')
        with (mock.patch.dict(os.environ, {'MAC_QUEUE_HOST': 'fixture-mac'}),
              mock.patch.object(subprocess, 'run', side_effect=[retained, removed]),
              mock.patch.object(copies, 'unregister_remote') as unregister,
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(mac_gate.lifecycle('cleanup', argv, folder, '1'), 0)
            unregister.assert_not_called()
            self.assertEqual(errors.getvalue().splitlines(), [notice])
            self.assertEqual(mac_gate.lifecycle('cleanup', argv, folder, '1'), 0)
            unregister.assert_called_once_with('fixture-mac', checkout)

    def test_ended_lifecycle_stops_the_old_attempt_with_the_installed_gate(self) -> None:
        home = self.root / 'Mac home'
        folder = home / mac_gate.REMOTE_ROOT / 'mac-gate-old'
        folder.mkdir(parents=True)
        argv = self.root / 'argv.json'
        checkout = self.root / 'removed checkout'
        _ = argv.with_suffix('.json.checkout').write_text(str(checkout))
        cloud_gate.save(folder / 'checkout.json', {'path': str(checkout)})
        _ = (folder / 'mac_gate.py').write_text("raise SystemExit('Old gate script')\n")
        shim = home / '.claude/scripts/lib/py'
        shim.parent.mkdir(parents=True)
        installed = home / '.claude/scripts/mac_test/mac_gate.py'
        installed.parent.mkdir()
        _ = installed.touch()
        write_stdlib_tool(shim, 'import json, sys\nprint(json.dumps(sys.argv[1:]))\n')
        run = subprocess.run

        def ssh(command: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[:-1], ['ssh', '-n', '-o', 'BatchMode=yes', 'fixture-mac'])
            self.assertTrue(capture_output and text and not check)
            expanded = run(['/bin/sh', '-c', command[-1]], capture_output=True, text=True,
                           timeout=TEST_HANG_GUARD_SECS, check=True)
            words = cast(list[str], json.loads(expanded.stdout))
            self.assertEqual(words, [str(installed), 'stop', str(folder), '--checkout', str(checkout)])
            with mock.patch.object(sys, 'argv', words), redirect_stdout(StringIO()) as output:
                status = mac_gate.main()
            return subprocess.CompletedProcess(command, status, output.getvalue(), '')

        with (mock.patch.dict(os.environ, {**copies_environment(home), 'MAC_QUEUE_HOST': 'fixture-mac'}),
              mock.patch.object(subprocess, 'run', side_effect=ssh),
              mock.patch.object(scratch, 'end', return_value=[]) as end,
              mock.patch.object(copies, 'unregister_remote') as unregister):
            self.assertEqual(mac_gate.lifecycle('cleanup-ended', argv, self.root / folder.name, '0'), 0)
            self.assertFalse(folder.exists())
            self.assertEqual(unregister.call_args_list, [mock.call('localhost', str(checkout)),
                                                         mock.call('fixture-mac', str(checkout))])
            unregister.reset_mock()
            checkout.mkdir()
            self.assertEqual(mac_gate.lifecycle('cleanup-ended', argv, self.root / folder.name, '0'), 0)
            unregister.assert_not_called()
            checkout.rmdir()
            checkout.symlink_to(self.root / 'missing')
            self.assertEqual(mac_gate.lifecycle('cleanup-ended', argv, self.root / folder.name, '0'), 0)
            unregister.assert_not_called()
            checkout.unlink()
            self.assertEqual(mac_gate.lifecycle('cleanup-ended', argv, self.root / folder.name, '0'), 0)
            unregister.assert_called_once_with('fixture-mac', str(checkout))
        end.assert_called_once_with('gate:mac-gate-old')

    def test_inline_checkout_waits_for_its_legacy_test_processes(self) -> None:
        folder = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-inline'
        repo = folder / 'repo'
        repo.mkdir(parents=True)
        pid = 21815
        identity = Started('old test')
        in_use = tmp_sweep.ProcessUsesKnown([(str(repo / 'target/test'), pid, 'test')])
        with (mock.patch.dict(os.environ, copies_environment(self.root)),
              mock.patch.object(scratch, 'end', return_value=[]) as end,
              mock.patch.object(folder_audit, 'process_uses', return_value=in_use) as scan,
              mock.patch.object(mac_gate, 'process_start', side_effect=[identity, Started('reused')]),
              mock.patch.object(os, 'kill') as kill):
            with self.assertRaisesRegex(GateRefused, 'Test processes still use the Mac checkout'):
                _ = mac_gate.stop_checkout(folder)
            kill.assert_not_called()
            end.assert_not_called()
            self.assertTrue(repo.is_dir())
            scan.assert_called_with([repo])
        with (mock.patch.dict(os.environ, copies_environment(self.root)),
              mock.patch.object(scratch, 'end', return_value=[]),
              mock.patch.object(folder_audit, 'process_uses',
                                side_effect=[in_use, in_use, tmp_sweep.ProcessUsesKnown([])]),
              mock.patch.object(mac_gate, 'process_start', return_value=identity),
              mock.patch.object(os, 'kill') as kill):
            self.assertEqual(mac_gate.stop_checkout(folder), mac_gate.CheckoutCleanup.REMOVED)
            kill.assert_called_once_with(pid, signal.SIGKILL)
        self.assertFalse(folder.exists())

    def test_installed_stop_accepts_old_attempts_without_a_journal_or_script(self) -> None:
        checkout = self.root / 'retained checkout'
        checkout.mkdir()
        with (mock.patch.dict(os.environ, copies_environment(self.root)),
              mock.patch.object(scratch, 'end', return_value=[]),
              mock.patch.object(copies, 'unregister_remote') as unregister):
            for has_receipt in (False, True):
                with self.subTest(has_receipt=has_receipt):
                    folder = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-old'
                    folder.mkdir(parents=True)
                    args = ['mac_gate.py', 'stop', str(folder)]
                    expected = mac_gate.CheckoutCleanup.REMOVED
                    if has_receipt:
                        cloud_gate.save(folder / 'checkout.json', {'path': str(checkout)})
                        args.extend(('--checkout', str(checkout)))
                        expected = mac_gate.CheckoutCleanup.RETAINED
                    with mock.patch.object(sys, 'argv', args), redirect_stdout(StringIO()) as output:
                        self.assertEqual(mac_gate.main(), 0)
                    self.assertEqual(output.getvalue().strip(), expected)
                    self.assertEqual(folder.exists(), has_receipt)
            unregister.assert_not_called()
        self.assertTrue(checkout.is_dir())

    def test_missing_close_script_checks_the_mac_checkout_path(self) -> None:
        folder = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-fixture'
        argv = self.root / 'argv.json'
        receipt = argv.with_suffix('.json.checkout')
        checkout = self.root / "Mac's gate checkout"
        shim = self.root / '.claude/scripts/lib/py'
        shim.parent.mkdir(parents=True)
        shim.symlink_to(sys.executable)
        run = subprocess.run

        def ssh(command: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[0], 'ssh')
            self.assertTrue(capture_output and text and not check)
            return run(['/bin/sh', '-c', command[-1]], capture_output=True, text=True,
                       timeout=TEST_HANG_GUARD_SECS)

        with (mock.patch.dict(os.environ, {'MAC_QUEUE_HOST': 'fixture-mac', 'HOME': str(self.root)}),
              mock.patch.object(subprocess, 'run', side_effect=ssh),
              mock.patch.object(copies, 'unregister_remote') as unregister):
            checkout.mkdir()
            for case in ('no receipt', 'folder', 'dangling symlink', 'gone'):
                with self.subTest(case=case):
                    folder.mkdir(parents=True)
                    if case == 'folder':
                        _ = receipt.write_text(str(checkout))
                    elif case == 'dangling symlink':
                        checkout.rmdir()
                        checkout.symlink_to(self.root / 'missing')
                    elif case == 'gone':
                        checkout.unlink()
                    self.assertEqual(mac_gate.lifecycle('cleanup', argv, folder, '1'), 0)
                    self.assertFalse(folder.exists())
                    if case == 'gone':
                        unregister.assert_called_once_with('fixture-mac', str(checkout))
                    else:
                        unregister.assert_not_called()
            self.assertFalse(checkout.is_symlink())

    def install_held_module(self) -> None:
        shim = self.repo / 'scripts/lib/py'
        shim.parent.mkdir()
        shim.symlink_to(sys.executable)
        runner = self.repo / 'scripts/test_times/module_run.py'
        runner.parent.mkdir()
        _ = runner.write_text(HELD_MODULE)
        _ = commit_repository(self.repo, 'gate fixture')

    def test_route_cancellation_runs_the_mac_close_hook(self) -> None:
        self.install_held_module()
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                self.cancel_route(Path(temporary), explicit=explicit, force_signal_to_sender=explicit)

    def test_route_cancellation_reports_a_close_failure(self) -> None:
        self.install_held_module()
        self.cancel_route(self.root, explicit=True, cleanup_refused=True)

    def test_route_fixture_reports_a_failed_close_hook(self) -> None:
        self.install_held_module()
        (self.root / 'fail_close_hook').touch()
        with self.assertRaisesRegex(AssertionError, 'RuntimeError: fixture close hook failed'):
            self.cancel_route(self.root, explicit=True)

    def test_route_cancellation_stops_bundle_git_and_removes_refs(self) -> None:
        self.cancel_route(self.root, explicit=True, prepare_bundle=True)

    def cancel_route(self, root: Path, *, explicit: bool, cleanup_refused: bool = False,
                     prepare_bundle: bool = False, force_signal_to_sender: bool = False) -> None:
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
        if force_signal_to_sender:
            wrapper = root / 'queue-wrapper.py'
            _ = wrapper.write_text(QUEUE_SIGNAL_TO_SENDER)
            environment['MAC_GATE_QUEUE_WRAPPER'] = str(wrapper)
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
            caller = [sys.executable, '-c', CALL_ROUTE, str(self.repo), str(callback),
                      str(reader) if explicit else '']
            result = root / 'route.log'
            with result.open('w+') as output, subprocess.Popen(
                caller, env=environment, stdout=output, stderr=output, pass_fds=(reader,) if explicit else (),
            ) as process:
                try:
                    marker = root / ('bundle_pid' if prepare_bundle else 'module_pid')
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
                        self.assertFalse(isinstance(process_start(child_pid), Started))
                        self.assertEqual(gate.checked_git(self.repo, 'for-each-ref', 'refs/cloud-gates/'), '')
                        return
                    close_status = root / 'close_status'
                    wait_until(close_status.exists, root)
                    self.assertEqual(int(close_status.read_text()), 0, (root / 'close.log').read_text())
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

    def test_listed_production_modules_exist(self) -> None:
        checkout = Path(__file__).resolve().parents[2]
        entries = (checkout / mac_gate.MODULE_LIST).read_text().splitlines()
        missing = [name for line in entries
                   if (name := line.partition('#')[0].strip()).startswith('scripts/production/')
                   and not (checkout / name).is_file()]
        self.assertEqual(missing, [])

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
