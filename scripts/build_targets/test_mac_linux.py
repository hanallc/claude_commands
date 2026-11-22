"""Test the Mac Linux route with a local SSH fixture."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr
import fcntl
from io import StringIO
import os
from pathlib import Path
import pwd
import select
import shlex
import subprocess
import sys
import tempfile
import time
from typing import cast, override
import unittest
from unittest.mock import call, patch

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_targets import mac_linux
from scripts.lib import build_folders, conf
from scripts.production import gate
from scripts.production.gate_routes import ALL_MODULES, AllModules, GateRun, ModuleCoverage, SuiteMode, TreeSelection


SSH_FIXTURE = '''import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys

host, command = sys.argv[-2:]
if host == 'fixture-mac':
    sys.exit(subprocess.call(['bash', '-c', command]))
assert host == 'fixture-guest'
assert sys.argv[-4:-2] == ['-l', os.environ['FIXTURE_GUEST_USER']]
shell = shlex.split(command)[-1]
assert 'checkout="$HOME/.claude"' in shell
assert shell.count('$HOME') == 1
cleanup = 'prove_end()' in shell
arm = 'prefix, attempt, window = sys.argv[1:]' in shell
probe = not cleanup and not arm and 'bundle unbundle -' not in shell
trace_path = Path(os.environ['FIXTURE_SSH_TRACE'])
with trace_path.open('a') as trace:
    trace.write(('cleanup' if cleanup else 'arm' if arm else 'probe' if probe else 'run') + '\\n')
home = Path(os.environ['FIXTURE_GUEST_HOME'])
if os.environ['FIXTURE_GUEST_RESULT'] == 'offline':
    print('fixture SSH refused the connection', file=sys.stderr)
    sys.exit(255)
environment = dict(os.environ, HOME=str(home))
scenario = os.environ['FIXTURE_GUEST_RESULT']
if arm:
    reply = subprocess.check_output(['bash', '-c', shell], stdin=subprocess.DEVNULL, env=environment)
    if scenario != 'missing-arm-ack':
        sys.stdout.buffer.write(reply)
        sys.stdout.buffer.flush()
    sys.exit(0)
if cleanup:
    if scenario in ('disconnect-before-copy', 'interrupted-before-copy'):
        lock = home / '.claude/.git/mac-linux-run.lock'
        proof_attempt = trace_path.read_text().splitlines().count('cleanup')
        if proof_attempt == 1:
            assert subprocess.call(['flock', '-x', '-n', lock, 'true']) == 1
            assert not (home / 'build').exists()
        if proof_attempt <= int(os.environ.get('FIXTURE_OFFLINE_PROOFS', '0')):
            print('fixture cleanup SSH refused the connection', file=sys.stderr)
            sys.exit(255)
    sys.exit(subprocess.call(['bash', '-c', shell], stdin=subprocess.DEVNULL, env=environment))
if probe:
    assert 'mkdir' not in shell
    if 'FIXTURE_PROBED_MAIN' in os.environ:
        print(os.environ['FIXTURE_PROBED_MAIN'])
        sys.exit(0)
    sys.exit(subprocess.call(['bash', '-c', shell], stdin=subprocess.DEVNULL, env=environment))
assert 'mkdir -p -- "$checkout/.git"' in shell
assert 'O_NOFOLLOW' in shell
assert 'EPOCHREALTIME' not in shell
assert f'timeout --signal=KILL {os.environ["FIXTURE_RUN_SECONDS"]} python3' in shell
payload = sys.stdin.buffer.read()
offset = re.search(r'^head -c ([0-9]+) [|] tar', shell, re.M)
assert offset
bootstrap_bytes = int(offset[1])
bundle = payload[bootstrap_bytes:]
assert bundle.startswith((b'# v2 git bundle\\n', b'# v3 git bundle\\n'))
header, pack = bundle.split(b'\\n\\n', 1)
assert pack.startswith(b'PACK')
Path(os.environ['FIXTURE_SOURCE_BYTES']).write_text(str(len(bundle)))
with Path(os.environ['FIXTURE_BUNDLE_FORMS']).open('a') as forms:
    forms.write('delta\\n' if any(line.startswith(b'-') for line in header.splitlines()) else 'first\\n')
if scenario == 'disconnect':
    payload = payload[:-1]
if scenario in ('disconnect-before-copy', 'interrupted-before-copy'):
    import selectors

    guest = subprocess.Popen(['bash', '-c', shell], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, env=environment, start_new_session=True)
    guest.stdin.write(payload)
    guest.stdin.close()
    with selectors.DefaultSelector() as ready:
        ready.register(guest.stderr, selectors.EVENT_READ)
        assert ready.select(3), 'The guest did not reach the pause before take_copy.'
    assert guest.stderr.readline() == b'fixture pause before take_copy\\n'
    if scenario == 'interrupted-before-copy':
        print('fixture local SSH is still active', file=sys.stderr, flush=True)
        signal.pause()
    os._exit(255)
sys.exit(subprocess.run(['bash', '-c', shell], input=payload, env=environment,
                        start_new_session=True).returncode)
'''


GATE_FIXTURE = '''import fcntl
import os
from pathlib import Path
import signal
import subprocess

from scripts.copies import copies
from scripts.production.gate_modules import discover_test_modules
from scripts.production.gate_routes import GateJobResult, GateRun, SelectedModules, SuiteMode


def checked_git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


def WholeMachine(*, gate_parent):
    return gate_parent


def run_suites(repo, *, full, mode, machine, selected, module_time_limit_s, time_limit):
    guest = Path(os.environ['FIXTURE_GUEST_HOME'])
    assert repo == guest / 'build/claude' / os.environ['FIXTURE_EXPECTED_UNIT'] / '1', repo
    assert Path.home().is_relative_to(repo / 'target')
    assert Path(os.environ['XDG_RUNTIME_DIR']).is_relative_to(repo / 'target')
    assert machine.path == Path(os.environ['TMPDIR'])
    assert machine.path.is_relative_to(repo / 'target')
    assert (Path.home() / '.claude/config').resolve() == repo / 'config'
    assert os.environ['CARGO_TARGET_DIR'] == str(repo / 'target')
    assert Path(os.environ['RUSTUP_HOME']).is_relative_to(repo / 'target')
    with repo.with_suffix('.lock').open('r') as lock:
        descriptor, run_descriptor = copies.COMMAND_LOCKS.get()
        assert os.path.samestat(os.fstat(descriptor), os.fstat(lock.fileno()))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('The numbered copy is not locked.')
    with (guest / '.claude/.git/mac-linux-run.lock').open() as lock:
        assert os.path.samestat(os.fstat(run_descriptor), os.fstat(lock.fileno()))
        assert int(lock.read().split()[0]) == os.getpgrp()
    assert os.environ['USER'] == os.environ['LOGNAME']
    assert os.environ['USER'] == os.environ['FIXTURE_GUEST_USER']
    assert not {'SSH_AUTH_SOCK', 'GH_TOKEN', 'XDG_CONFIG_HOME', 'CONF_DIR'} & os.environ.keys()
    assert os.cpu_count() == 2 and time_limit.seconds == int(os.environ['FIXTURE_RUN_SECONDS'])
    assert module_time_limit_s == 3
    assert checked_git(repo, 'show', 'origin/main:history.txt') == 'old source'
    assert int(checked_git(repo, 'rev-list', '--count', 'main', 'origin/main')) >= 2
    assert checked_git(repo, 'rev-parse', 'main') == os.environ['FIXTURE_EXPECTED_MAIN']
    assert checked_git(repo, 'rev-parse', 'origin/main') == os.environ['FIXTURE_EXPECTED_MAIN']
    assert full == (mode is SuiteMode.GATE)
    print('A guest log line must stay outside the JSON result.')
    scenario = os.environ['FIXTURE_GUEST_RESULT']
    if scenario == 'hang':
        Path(os.environ['FIXTURE_RUN_GROUP']).write_text(str(os.getpgrp()))
        signal.pause()
    if scenario == 'runner-failure':
        raise RuntimeError('fixture guest runner failed')
    if scenario == 'leave-edit':
        (repo / 'history.txt').write_text('copy edit\\n')
    timing = dict(wall_s=1.3, cpu_s=1.0, tests_s=0.7, tests_run=3, failures=0, errors=0, tests=[])
    paths = [(suite, path) for suite, path in discover_test_modules(repo)
             if not isinstance(selected, SelectedModules) or str(path.relative_to(repo)) in selected.paths]
    jobs = tuple(GateJobResult(suite, path.name, int(scenario == 'red'), 1.3, 'fixture module output',
                               cpu_seconds=1.0, times=dict(timing, skipped=int(path.name == 'test_second.py')),
                               peak_mib=22) for suite, path in paths)
    return GateRun(jobs, 2.0, 30, selection_certified=isinstance(selected, SelectedModules), executions=jobs)
'''


GUEST_BOOTSTRAP = '''from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import runpy
import signal
import socket
import sys

from scripts.aws_target import gate_box, ledger

os.chdir(sys.argv[1])
socket.gethostname = lambda: 'nixos'
if os.environ['FIXTURE_GUEST_RESULT'] in ('disconnect-before-copy', 'interrupted-before-copy'):
    print('fixture pause before take_copy', file=sys.stderr, flush=True)
    signal.pause()
if sys.argv[-1] == '--run-copy':
    spec = spec_from_file_location('scripts.production.gate', Path('scripts/production/gate.py'))
    gate = module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
path = sys.argv[2]
sys.argv = sys.argv[2:]
runpy.run_path(path, run_name='__main__')
'''


class MacLinuxRouteTests(unittest.TestCase):
    root: Path = Path()
    guest_home: Path = Path()
    trace: Path = Path()
    repo: Path = Path()
    registry: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.guest_home = self.root / 'guest'
        self.guest_home.mkdir()
        self.trace = self.root / 'ssh.log'
        self.trace.touch()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        account = pwd.getpwuid(os.getuid()).pw_name
        binaries = self.root / 'bin'
        binaries.mkdir()
        self.executable(binaries / 'ssh', f'#!{sys.executable} -B\n' + SSH_FIXTURE)
        (binaries / 'chromium').symlink_to(sys.executable)
        config = self.root / 'config'
        config.mkdir()
        self.registry = config / 'build_targets.conf'
        _ = self.registry.write_text(
            'kind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=mac_linux.conf\n'
            + 'enabled.mac-linux=on\n',
            encoding='utf-8')
        _ = (config / 'mac_linux.conf').write_text(
            f'ssh.hops=fixture-mac fixture-guest\nguest.user={account}\ntest.jobs=2\n'
            + 'run.time_limit_seconds=7\n',
            encoding='utf-8')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            'CONF_DIR': str(config), 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
            'FIXTURE_SSH_TRACE': str(self.trace), 'FIXTURE_GUEST_HOME': str(self.guest_home),
            'FIXTURE_SOURCE_BYTES': str(self.root / 'source.bytes'),
            'FIXTURE_BUNDLE_FORMS': str(self.root / 'bundle.forms'),
            'FIXTURE_GUEST_USER': account, 'FIXTURE_GUEST_RESULT': 'pass',
            'FIXTURE_RUN_SECONDS': '7', 'FIXTURE_RUN_GROUP': str(self.root / 'run.group'),
            'BUILDLOG_UNIT': 'caller', 'FIXTURE_EXPECTED_UNIT': 'caller',
            'GH_TOKEN': 'fixture-token', 'SSH_AUTH_SOCK': str(self.root / 'agent.sock'),
            'XDG_CONFIG_HOME': str(self.root / 'outside-config'),
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
            'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test',
        })))
        _ = self.git('init', '-q', '--initial-branch=main')
        _ = (self.repo / 'history.txt').write_text('old source\n', encoding='utf-8')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'base')
        _ = self.git('remote', 'add', 'origin', 'https://example.test/claude_commands.git')
        for folder in ('config', 'scripts/lib', 'scripts/production', 'scripts/fixture'):
            (self.repo / folder).mkdir(parents=True)
        model = Path(__file__).resolve().parents[2] / 'config/build_folders.conf'
        _ = (self.repo / 'config/build_folders.conf').write_bytes(model.read_bytes())
        _ = (self.repo / 'scripts/production/gate.py').write_text(GATE_FIXTURE, encoding='utf-8')
        for module in ('test_first.py', 'test_second.py'):
            (self.repo / 'scripts/fixture' / module).touch()
        package_root = shlex.quote(str(Path(__file__).resolve().parents[2]))
        shim = """#!/usr/bin/env bash
candidate="$PWD"
options=()
if [[ "$1" == "-X" ]]; then
    options=("$1" "$2")
    shift 4
fi
"""
        self.executable(self.repo / 'scripts/lib/py', shim
                        + f'cd {package_root}\nexport PYTHONPATH={package_root}\n'
                        + f'exec {shlex.quote(sys.executable)} -B "${{options[@]}}"'
                        + f' -c {shlex.quote(GUEST_BOOTSTRAP)} "$candidate" "$@"\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'main checkout')
        _ = self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    @staticmethod
    def executable(path: Path, content: str) -> None:
        _ = path.write_text(content, encoding='utf-8')
        path.chmod(0o755)

    def git(self, *args: str) -> str:
        return self.git_at(self.repo, *args)

    @staticmethod
    def git_at(folder: Path, *args: str) -> str:
        return subprocess.check_output(['git', *args], cwd=folder, text=True, timeout=10).strip()

    def route(self, scenario: str = 'pass', selected: AllModules | TreeSelection = ALL_MODULES,
              gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED) -> GateRun | str:
        mode = SuiteMode.GATE if isinstance(selected, AllModules) else SuiteMode.TARGETED
        temporary_directory = tempfile.TemporaryDirectory

        def snapshot_index(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
            self.assertEqual(prefix, 'cloud-gate-index-')
            return temporary_directory(prefix=prefix)

        with (patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': scenario,
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main')}),
              patch.object(tempfile, 'TemporaryDirectory', snapshot_index),
              patch.object(tempfile, 'TemporaryFile', side_effect=AssertionError('Keep the bootstrap in memory.'))):
            result = mac_linux.route(self.repo, mode=mode, full=mode is SuiteMode.GATE, selected=selected,
                                     module_time_limit_s=3, gate_job=gate_job)
        self.assertEqual(self.git('for-each-ref', 'refs/mac-linux'), '')
        return result

    def assert_guest_clean(self, *events: str) -> None:
        main = self.guest_home / '.claude'
        build = self.guest_home / 'build'
        self.assertLessEqual(set(self.guest_home.iterdir()), {main, build})
        self.assertFalse((main / '.git/mac-linux').exists())
        self.assertEqual(list(build.rglob('mac-linux*')), [])
        self.assertEqual(self.trace.read_text(encoding='utf-8').splitlines(), list(events))

    def assert_bundle(self, run: mac_linux.MacLinuxGateRun, form: mac_linux.BundleForm,
                      reason: mac_linux.BundleReason, *forms: str) -> None:
        self.assertEqual(run.bundle_bytes, int((self.root / 'source.bytes').read_text(encoding='utf-8')))
        self.assertGreater(run.bundle_bytes, 0)
        self.assertEqual((run.bundle_form, run.bundle_reason), (form, reason))
        self.assertEqual((self.root / 'bundle.forms').read_text(encoding='utf-8').splitlines(), list(forms))

    def test_switch_off_returns_local_reason_without_ssh(self) -> None:
        _ = self.registry.write_text('enabled.mac-linux=off\n', encoding='utf-8')
        self.assertEqual(self.route(), 'mac-linux is off (build_targets.conf enabled.mac-linux=off)')
        self.assert_guest_clean()

    def test_transport_failure_returns_reason(self) -> None:
        result = self.route('offline')
        self.assertIsInstance(result, str)
        self.assertIn('fixture SSH refused the connection', str(result))
        self.assert_guest_clean('probe')

    def test_other_repository_is_refused_before_ssh(self) -> None:
        _ = self.git('remote', 'set-url', 'origin', 'https://example.test/other-repo.git')
        with self.assertRaises(mac_linux.GuestRepositoryRefused) as raised:
            _ = mac_linux.candidate_source(self.repo, ALL_MODULES)
        self.assertEqual(raised.exception.repository, 'other-repo')
        self.assertIn('source repository is other-repo', str(self.route()))
        self.assert_guest_clean()

    def test_gate_uses_the_runtime_parent_inside_the_copy(self) -> None:
        runtime = self.guest_home / 'build/claude/caller/1/target/mac-linux/tmp'
        runtime.mkdir(parents=True)
        machine = gate.WholeMachine(gate_parent=build_folders.ExplicitBuildParent(runtime))
        with patch.object(build_folders, 'tmpfs_temporary_parent', return_value=Path('/dev/shm')):
            self.assertEqual(gate.gate_work_parent(machine), runtime)
            self.assertEqual(gate.gate_work_parent(gate.WholeMachine()), Path('/dev/shm'))

    def test_local_timeout_kills_the_recorded_group_and_proves_end(self) -> None:
        configuration = self.registry.with_name('mac_linux.conf')
        _ = configuration.write_text(configuration.read_text().replace('seconds=7', 'seconds=30'))
        with (patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '30'}),
              patch.object(mac_linux.GuestSettings, 'transfer_timeout_seconds', 6)):
            result = self.route('hang')
        self.assertIsInstance(result, str)
        group = (self.root / 'run.group').read_text()
        self.assertIn('SSH or guest run exceeded 6 seconds', str(result))
        self.assertIn('Guest cleanup: ended', str(result))
        self.assertEqual(self.run_lock().read_text().split()[0], group)
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

    def run_lock(self) -> Path:
        return self.guest_home / '.claude' / mac_linux.RUN_LOCK_PATH

    def test_cleanup_cancels_an_attempt_before_it_takes_the_lock(self) -> None:
        git = self.run_lock().parent
        git.mkdir(parents=True)
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        expired = time.clock_gettime_ns(time.CLOCK_BOOTTIME) - (mac_linux.SSH_CONNECT_TIMEOUT_SECONDS + 1) * 10**9
        old_record = git / f'{mac_linux.CANCEL_RECORD_PREFIX}.{boot}.{expired}.old-fixture'
        outside = self.root / 'outside-cancel-record'
        _ = outside.write_text('keep this file\n')
        old_record.symlink_to(outside)
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'delayed-fixture')
        proof = mac_linux.prove_guest_end(settings, armed)
        self.assertIsInstance(proof, mac_linux.GuestEndProven)
        cancel = git / f'{mac_linux.CANCEL_RECORD_PREFIX}.{armed.boot_id}.{armed.deadline_ns}.{armed.attempt}'
        self.assertTrue(cancel.is_file())
        self.assertFalse(old_record.is_symlink())
        self.assertEqual(outside.read_text(), 'keep this file\n')

        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        shell = mac_linux.run_shell(settings, source, 0, armed)
        run = subprocess.run(['bash', '-c', shell], env={**os.environ, 'HOME': str(self.guest_home)},
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
                             timeout=6, start_new_session=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('attempt was cancelled', run.stderr)
        self.assertEqual(self.run_lock().read_bytes(), b'')
        self.assertFalse((git / 'HEAD').exists())
        self.assertFalse((self.guest_home / 'build').exists())

    def test_run_refuses_work_after_the_armed_deadline(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        with patch.object(mac_linux, 'SSH_CONNECT_TIMEOUT_SECONDS', 1):
            armed = mac_linux.arm_guest(settings, 'expired-arm-fixture')
        shell = mac_linux.run_shell(settings, source, 0, armed)
        launch = 'import os, sys, time; time.sleep(1.2); os.execvp("bash", ["bash", "-c", sys.argv[1]])'
        run = subprocess.run([sys.executable, '-I', '-S', '-c', launch, shell],
                             env={**os.environ, 'HOME': str(self.guest_home)}, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, check=False, timeout=6, start_new_session=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('armed deadline passed', run.stderr)
        self.assertEqual(self.run_lock().read_bytes(), b'')
        self.assertFalse((self.run_lock().parent / 'HEAD').exists())

    def test_cleanup_keeps_cancel_records_until_their_armed_deadline(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'first-cancel-fixture')
        self.assertIsInstance(mac_linux.prove_guest_end(settings, armed), mac_linux.GuestEndProven)
        cancel = self.run_lock().parent / (
            f'{mac_linux.CANCEL_RECORD_PREFIX}.{armed.boot_id}.{armed.deadline_ns}.{armed.attempt}')
        next_armed = mac_linux.arm_guest(settings, 'second-cancel-fixture')
        self.assertIsInstance(mac_linux.prove_guest_end(settings, next_armed), mac_linux.GuestEndProven)
        self.assertLess(time.clock_gettime_ns(time.CLOCK_BOOTTIME), armed.deadline_ns)
        self.assertTrue(cancel.is_file())

    def test_missing_arm_acknowledgement_sends_no_run(self) -> None:
        result = self.route('missing-arm-ack')
        self.assertIn('did not acknowledge the armed deadline', str(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm'])
        self.assertFalse(self.run_lock().exists())

    def test_disconnect_before_take_copy_kills_the_run_and_proves_end(self) -> None:
        result = self.route('disconnect-before-copy')
        self.assertIn('SSH or source stream exited 255', str(result))
        self.assertIn('Guest cleanup: ended', str(result))
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertFalse((self.guest_home / 'build').exists())
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

    def test_unreachable_guest_keeps_the_claim_until_proof(self) -> None:
        events: list[str] = []
        prove_guest_end = mac_linux.prove_guest_end

        @contextmanager
        def reservation(*_args: object) -> Generator[None]:
            events.append('reserved')
            try:
                yield
            finally:
                events.append('released')

        def prove(settings: mac_linux.GuestSettings,
                  armed: mac_linux.ArmedGuestAttempt) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
            self.assertNotIn('released', events)
            cancel = self.run_lock().parent / (
                f'{mac_linux.CANCEL_RECORD_PREFIX}.{armed.boot_id}.{armed.deadline_ns}.{armed.attempt}')
            self.assertFalse(cancel.exists())
            proof = prove_guest_end(settings, armed)
            if isinstance(proof, mac_linux.GuestEndPending):
                self.assertIn('fixture cleanup SSH refused the connection', proof.detail)
                self.assertFalse(cancel.exists())
                events.append('unreachable')
            else:
                self.assertTrue(cancel.is_file())
                self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
                events.append('ended')
            return proof

        with (patch.dict(os.environ, {'FIXTURE_OFFLINE_PROOFS': '2'}),
              patch.object(mac_linux, 'mac_linux_reservation', reservation),
              patch.object(mac_linux, 'prove_guest_end', prove),
              patch.object(mac_linux, 'sleep') as retry_delay,
              redirect_stderr(StringIO()) as log):
            result = self.route('disconnect-before-copy')
        self.assertIn('Guest cleanup: ended after fixture cleanup SSH refused the connection', str(result))
        self.assertEqual(log.getvalue().splitlines(), ['Mac Linux guest run not proven ended; the Mac claim stays held'])
        self.assertEqual(events, ['reserved', 'unreachable', 'unreachable', 'ended', 'released'])
        self.assertEqual(retry_delay.call_args_list, [call(mac_linux.GUEST_PROOF_RETRY_SECONDS)] * 2)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup', 'cleanup', 'cleanup'])

    def test_cleanup_refuses_an_invalid_group_with_the_lock_held(self) -> None:
        lock = self.run_lock()
        lock.parent.mkdir(parents=True)
        registry = conf.read(self.registry)
        settings = mac_linux.read_settings(registry)
        armed = mac_linux.arm_guest(settings, 'invalid-group-fixture')
        with lock.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            _ = held.write('-1 invalid-group-fixture\n')
            held.flush()
            proof = mac_linux.prove_guest_end(settings, armed)
        self.assertIsInstance(proof, mac_linux.GuestEndPending)
        assert isinstance(proof, mac_linux.GuestEndPending)
        self.assertIn('process group ID is invalid', proof.detail)
        self.assertIn('run lock is still held', proof.detail)

    def test_reachable_guest_with_a_held_lock_retries(self) -> None:
        proofs: list[mac_linux.GuestEndProven | mac_linux.GuestEndPending] = []
        prove_guest_end = mac_linux.prove_guest_end

        @contextmanager
        def reservation(*_args: object) -> Generator[None]:
            try:
                yield
            finally:
                self.assertEqual([type(proof) for proof in proofs],
                                 [mac_linux.GuestEndPending, mac_linux.GuestEndPending, mac_linux.GuestEndProven])

        lock = self.run_lock()
        lock.parent.mkdir(parents=True)
        with lock.open('w') as held, redirect_stderr(StringIO()) as log:
            fcntl.flock(held, fcntl.LOCK_EX)

            def prove(settings: mac_linux.GuestSettings,
                      armed: mac_linux.ArmedGuestAttempt) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
                proof = prove_guest_end(settings, armed)
                proofs.append(proof)
                if len(proofs) == 2:
                    fcntl.flock(held, fcntl.LOCK_UN)
                return proof

            with (patch.object(mac_linux, 'mac_linux_reservation', reservation),
                  patch.object(mac_linux, 'prove_guest_end', prove),
                  patch.object(mac_linux, 'GUEST_KILL_GRACE_SECONDS', 0)):
                result = self.route()
        self.assertIn('Guest cleanup: ended', str(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup', 'cleanup', 'cleanup'])
        self.assertEqual(log.getvalue().splitlines(), ['Mac Linux guest run not proven ended; the Mac claim stays held'])

    def test_broken_stderr_keeps_the_reservation_until_proof(self) -> None:
        events: list[str] = []
        prove_guest_end = mac_linux.prove_guest_end

        class ClosedStderr(StringIO):
            @override
            def write(self, message: str) -> int:
                events.append('stderr failed')
                raise BrokenPipeError(message)

        def prove(settings: mac_linux.GuestSettings, armed: mac_linux.ArmedGuestAttempt) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
            if events == ['reserved']:
                events.append('pending')
                return mac_linux.GuestEndPending('fixture cleanup pending')
            proof = prove_guest_end(settings, armed)
            events.append('ended' if isinstance(proof, mac_linux.GuestEndProven) else 'pending')
            return proof

        @contextmanager
        def reservation(*_args: object) -> Generator[None]:
            events.append('reserved')
            try:
                yield
            finally:
                events.append('released')

        with (patch.object(mac_linux, 'mac_linux_reservation', reservation),
              patch.object(mac_linux, 'prove_guest_end', prove), redirect_stderr(ClosedStderr())):
            result = self.route()
        self.assertIsInstance(result, GateRun)
        self.assertEqual(events, ['reserved', 'pending', 'stderr failed', 'ended', 'released'])

    def test_cleanup_error_timeout_or_unexpected_reply_is_pending(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'reachable-fixture')
        for status, output in ((1, ''), (0, 'unexpected')):
            with (self.subTest(status=status),
                  patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], status, output, ''))):
                proof = mac_linux.prove_guest_end(settings, armed)
                self.assertIsInstance(proof, mac_linux.GuestEndPending)

        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            self.assertIsInstance(mac_linux.prove_guest_end(settings, armed), mac_linux.GuestEndPending)

    def test_keyboard_interrupt_proves_the_end_before_release(self) -> None:
        communicate = cast(Callable[[subprocess.Popen[bytes], bytes | None, float | None], tuple[bytes, bytes]],
                           getattr(subprocess.Popen, 'communicate'))

        def interrupt_source(process: subprocess.Popen[bytes], input: bytes | None = None,
                             timeout: float | None = None) -> tuple[bytes, bytes]:
            arguments = process.args
            if not isinstance(arguments, list) or mac_linux.STREAM_SOURCE not in arguments:
                return communicate(process, input, timeout)
            assert process.stdin is not None and process.stderr is not None and input is not None
            _ = process.stdin.write(input)
            process.stdin.close()
            self.assertTrue(select.select([process.stderr], [], [], 6)[0])
            self.assertEqual(process.stderr.readline(), b'fixture local SSH is still active\n')
            raise KeyboardInterrupt

        @contextmanager
        def reservation(*_args: object) -> Generator[None]:
            try:
                yield
            finally:
                self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])
                self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)

        with (patch.object(subprocess.Popen, 'communicate', interrupt_source),
              patch.object(mac_linux, 'mac_linux_reservation', reservation),
              self.assertRaises(KeyboardInterrupt)):
            _ = self.route('interrupted-before-copy')

    def test_run_and_cleanup_refuse_a_symlink_lock(self) -> None:
        lock = self.run_lock()
        lock.parent.mkdir(parents=True)
        outside = self.root / 'outside-lock'
        _ = outside.write_text('keep this file\n')
        lock.symlink_to(outside)
        settings = mac_linux.read_settings(conf.read(self.registry))
        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        armed = mac_linux.arm_guest(settings, 'symlink-fixture')
        run = subprocess.run(['bash', '-c', mac_linux.run_shell(settings, source, 0, armed)],
                             env={**os.environ, 'HOME': str(self.guest_home)}, capture_output=True, text=True, check=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('run lock must not be a symlink', run.stderr)
        proof = mac_linux.prove_guest_end(settings, armed)
        self.assertIsInstance(proof, mac_linux.GuestEndPending)
        assert isinstance(proof, mac_linux.GuestEndPending)
        self.assertIn('run lock must not be a symlink', proof.detail)
        self.assertEqual(outside.read_text(), 'keep this file\n')

    def test_guest_deadline_stops_a_hung_runner(self) -> None:
        configuration = self.registry.with_name('mac_linux.conf')
        _ = configuration.write_text(configuration.read_text().replace('seconds=7', 'seconds=3'))
        with patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '3'}):
            result = self.route('hang')
        self.assertIsInstance(result, str)
        self.assertRegex(str(result), r'SSH or source stream exited (9|137):')
        self.assertIn('Guest cleanup: ended', str(result))
        self.assertTrue((self.root / 'run.group').is_file())
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

    def test_guest_returns_module_metrics(self) -> None:
        result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assertTrue(result.passed)
        self.assertEqual(result.peak_memory_mib, 30)
        self.assertEqual(result.seconds, 2)
        self.assertEqual([(job.suite, job.module) for job in result.jobs],
                         [('fixture', 'test_first.py'), ('fixture', 'test_second.py')])
        job = result.jobs[0]
        self.assertEqual((job.returncode, job.seconds, job.output, job.cpu_seconds, job.peak_mib),
                         (0, 1.3, 'fixture module output', 1.0, 22))
        assert isinstance(job.times, dict)
        self.assertEqual(job.times['tests_run'], 3)
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN, 'first')
        self.assertEqual(result.module_skips, {'scripts/fixture/test_first.py': 0, 'scripts/fixture/test_second.py': 1})
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_red_module_returns_a_failed_gate(self) -> None:
        result = self.route('red')
        assert isinstance(result, GateRun), result
        self.assertFalse(result.passed)
        self.assertEqual([job.returncode for job in result.jobs], [1, 1])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_runner_failure_returns_reason_and_cleans(self) -> None:
        result = self.route('runner-failure')
        self.assertIsInstance(result, str)
        self.assertIn('fixture guest runner failed', str(result))
        self.assertIn('Guest cleanup: ended', str(result))
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_copy_can_retry_after_a_partial_upload(self) -> None:
        main = self.guest_home / '.claude'
        self.assertIsInstance(self.route(), GateRun)
        _ = (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route('disconnect')
        self.assertIsInstance(result, str)
        self.assertIn('SSH or source stream exited', str(result))
        self.assertEqual((main / 'history.txt').read_text(encoding='utf-8'), 'old source\n')
        self.assertFalse((main / 'scripts/fixture/test_added.py').exists())
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')
        retry = self.route()
        assert isinstance(retry, GateRun), retry
        self.assertTrue(retry.passed)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_copy_uses_the_job_unit(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_EXPECTED_UNIT': 'checkpoint-owner'}):
            result = self.route(gate_job=StepJob('integrate', unit='checkpoint-owner'))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_delta_updates_main_and_reuses_the_numbered_copy(self) -> None:
        main = self.guest_home / '.claude'
        numbered = self.guest_home / 'build/claude/caller/1'
        first = self.route()
        assert isinstance(first, mac_linux.MacLinuxGateRun), first
        old_main = self.git('rev-parse', 'origin/main')
        use_stamp = numbered / 'target/fixture-output'
        use_stamp.touch()
        (self.repo / 'scripts/fixture/test_first.py').unlink()
        _ = self.git('commit', '-qam', 'advance main')
        _ = self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        (self.repo / 'scripts/fixture/test_added.py').touch()
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'candidate change')
        second = self.route()
        assert isinstance(second, mac_linux.MacLinuxGateRun), second
        self.assert_bundle(second, mac_linux.BundleForm.DELTA, mac_linux.BundleReason.DELTA, 'first', 'delta')
        self.assertLess(second.bundle_bytes, first.bundle_bytes)
        self.assertEqual(self.git_at(main, 'rev-parse', 'HEAD', 'origin/main').splitlines(),
                         [self.git('rev-parse', 'origin/main')] * 2)
        self.assertEqual(self.git_at(main, 'rev-parse', 'HEAD^'), old_main)
        self.assertEqual(self.git_at(numbered, 'rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD'))
        self.assertFalse((main / 'scripts/fixture/test_first.py').exists())
        self.assertFalse((main / 'scripts/fixture/test_added.py').exists())
        self.assertTrue((numbered / 'scripts/fixture/test_added.py').exists())
        self.assertTrue(use_stamp.is_file())
        self.assertTrue(numbered.with_suffix('.lease.json').is_file())
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_empty_guest_repository_takes_first_bundle_and_then_empty_delta(self) -> None:
        main = self.guest_home / '.claude'
        _ = self.git_at(self.guest_home, 'init', '-q', '--initial-branch=main', str(main))
        first = self.route()
        assert isinstance(first, mac_linux.MacLinuxGateRun), first
        self.assert_bundle(first, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN, 'first')
        result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assert_bundle(result, mac_linux.BundleForm.DELTA, mac_linux.BundleReason.DELTA, 'first', 'delta')
        self.assertLess(result.bundle_bytes, first.bundle_bytes)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_guest_missing_prerequisite_retries_once_with_a_full_bundle(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        with patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': self.git('rev-parse', 'origin/main')}):
            result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.GUEST_LACKS_PREREQUISITE,
                           'delta', 'first')
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'arm', 'run', 'cleanup')

    def test_unknown_guest_main_uses_full_bundle_with_cause(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': 'f' * 40}):
            result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.SOURCE_LACKS_BASE, 'first')
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_copy_rescues_edits_after_main_moves(self) -> None:
        self.assertIsInstance(self.route('leave-edit'), GateRun)
        main = self.guest_home / '.claude'
        numbered = self.guest_home / 'build/claude/caller/1'
        _ = (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route()
        assert isinstance(result, GateRun), result
        rescue = subprocess.check_output(['git', '-C', str(main), 'for-each-ref', '--format=%(refname)',
                                          'refs/rescue/'], text=True).strip()
        saved = subprocess.check_output(['git', '-C', str(main), 'show', f'{rescue}:history.txt'], text=True)
        self.assertEqual(saved, 'copy edit\n')
        self.assertEqual((numbered / 'history.txt').read_text(encoding='utf-8'), 'old source\n')

    def test_selected_modules_keep_subset_coverage(self) -> None:
        tree = self.git('rev-parse', 'HEAD^{tree}')
        result = self.route(selected=TreeSelection(('scripts/fixture/test_second.py',), tree))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual(result.coverage, ModuleCoverage.SUBSET)
        self.assertEqual([job.module for job in result.jobs], ['test_second.py'])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_dirty_modules_are_included_in_the_snapshot(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual([job.module for job in result.jobs], ['test_added.py', 'test_first.py', 'test_second.py'])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')


if __name__ == '__main__':
    _ = unittest.main()
