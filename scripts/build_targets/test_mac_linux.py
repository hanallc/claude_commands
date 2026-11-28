"""Test the Mac Linux route with a local SSH fixture."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, chdir, contextmanager, nullcontext, redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
from datetime import timedelta
import errno
import fcntl
from functools import partial
import io
import json
import os
from pathlib import Path
import pwd
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
from threading import Event, Thread, Timer
import time
from types import CodeType
from typing import ClassVar, cast, override
import unittest
from unittest.mock import Mock, call, patch

from scripts.build_hold.owning_session import OwningSession
from scripts.build_queue import admission_client, admission_protocol as wire, admission_records, admission_wire, execution_identity, first_step, mac_run, placement, progress_profile, queue_place
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_queue.snapshot_codec import UNKNOWN_CALLER
from scripts.build_targets import build_targets, mac_linux, target_config
from scripts.build_targets.mac_linux import send_guest_execution
from scripts.buildlog import machine_facts, store, treekey
from scripts.copies import copies
from scripts.lib import atomic_file, build_folders, conf, process_group, process_start, scratch
from scripts.lib.build_copies import CopyQueuePosition
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state, eligibility, mac_test, offload
from scripts.production import checkpoint_gate, gate
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import ALL_MODULES, AllModules, GateJobKind, GateResultUnknown, GateRun, GuestGateBlocked, ModuleCoverage, SuiteMode, TreeSelection
from scripts.production.test_checkpoint_gate import GatePlacementFixture
from scripts.test_times.gate_record import NoGateTimeLimit, WallClockSpan


CHECKOUT = Path(__file__).resolve().parents[2]


SSH_FIXTURE = '''import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

host, command = sys.argv[-2:]
connect_timeout = next(arg for arg in sys.argv if arg.startswith('ConnectTimeout='))
if os.environ.get('FIXTURE_CHECK_SIDECAR'):
    assert 'BUILDLOG_PLACEMENT_SIDECAR' not in os.environ
if host == 'fixture-mac':
    environment = dict(os.environ, TMPDIR=str(Path(os.environ['FIXTURE_SSH_TRACE']).parent),
                       FIXTURE_MAC_CONNECT_TIMEOUT=connect_timeout)
    if 'bundle unbundle -' in command:
        time.sleep(float(os.environ.get('FIXTURE_MAC_STREAM_DELAY', '0')))
    mac_shell = os.environ.get('FIXTURE_MAC_SHELL', 'bash')
    status = subprocess.call([mac_shell, '-c', command], env=environment)
    if status == 254 and 'bundle unbundle -' in command and os.environ.get('FIXTURE_MASK_STREAM_STATUS'):
        status = 0
    sys.exit(status)
assert host == 'fixture-guest'
assert sys.argv[-4:-2] == ['-l', os.environ['FIXTURE_GUEST_USER']]
shell = shlex.split(command)[-1]
assert 'checkout="$HOME/.claude"' in shell
assert shell.count('$HOME') == 1
scenario = os.environ['FIXTURE_GUEST_RESULT']
identity = "printf 'checkout %s %s" in shell
cleanup = 'prove_end()' in shell
arm_records = 'full_suffix, window, lock_name, descriptor = sys.argv[1:]' in shell
fence = arm_records and ' recover mac-linux-armed mac-linux-cancel ' in shell
probe = not cleanup and not arm_records and 'bundle unbundle -' not in shell
trace_path = Path(os.environ['FIXTURE_SSH_TRACE'])
stage = 'cleanup' if cleanup else 'fence' if fence else 'arm' if arm_records else 'probe' if probe else 'run'
if not identity or scenario == 'offline':
    with trace_path.open('a') as trace:
        trace.write(stage + '\\n')
faults = os.environ.get('FIXTURE_SSH_' + stage.upper(), '').split(',')
index = trace_path.read_text().splitlines().count(stage) - 1 if not identity else -1
fault = faults[index] if 0 <= index < len(faults) else ''
reset = 'ssh: connect to host 127.0.0.1 port 31022: Connection reset by peer'
if fault == 'broken-after-banner':
    assert cleanup
    print('banner exchange: Connection to 127.0.0.1 port 31022: Broken pipe', file=sys.stderr)
    sys.exit(255)
if fault in ('reset', 'refused', 'denied'):
    errors = {'reset': reset, 'refused': reset.replace('reset by peer', 'refused'),
              'denied': 'fixture-guest: Permission denied (publickey).'}
    if '-E' in sys.argv:
        with open(sys.argv[sys.argv.index('-E') + 1], 'a') as client_log:
            client_log.write(errors[fault] + '\\r\\n')
    else:
        print(errors[fault], file=sys.stderr)
    sys.exit(255)
if fault == 'slow-connect':
    timing = trace_path.with_name('connect-timing')
    timing.write_text(f"{os.environ['FIXTURE_MAC_CONNECT_TIMEOUT']} {connect_timeout} {time.monotonic()}\\n")
    time.sleep(7)
    sys.exit(255)
if fault in ('reset-before-lock', 'forged-marker'):
    first_command = shell.splitlines()[0]
    assert 'mac-linux command accepted' in first_command
    subprocess.run(['bash', '-c', first_command], check=True)
    received = sys.stdin.buffer.read(16)
    trace_path.with_name('received-before-lock').write_bytes(received)
    if fault == 'forged-marker':
        print('mac-linux inner SSH connect failed', file=sys.stderr)
    print(reset, file=sys.stderr)
    sys.exit(254 if fault == 'forged-marker' else 255)
home = Path(os.environ['FIXTURE_GUEST_HOME'])
if scenario == 'offline' or scenario == 'main-probe-failure' and probe and not identity:
    print('ssh: connect to host fixture-mac port 22: Connection refused', file=sys.stderr)
    print('fixture SSH refused the connection', file=sys.stderr)
    sys.exit(255)
environment = dict(os.environ, HOME=str(home))
if 'FIXTURE_GUEST_PATH' in os.environ:
    environment['PATH'] = os.environ['FIXTURE_GUEST_PATH']
if 'FIXTURE_GUEST_MACHINE' in os.environ:
    shell = shell.replace('cat /etc/machine-id', 'printf %s ' + shlex.quote(os.environ['FIXTURE_GUEST_MACHINE']))
if identity:
    sys.exit(subprocess.call(['bash', '-c', shell], stdin=subprocess.DEVNULL, env=environment))
if arm_records:
    reply = subprocess.run(['bash', '-c', shell], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=environment)
    if scenario != 'missing-arm-ack' or fence:
        sys.stdout.buffer.write(reply.stdout)
        sys.stdout.buffer.flush()
    sys.exit(reply.returncode)
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
outer_seconds = int(os.environ['FIXTURE_RUN_SECONDS']) + int(os.environ['FIXTURE_TRANSFER_SECONDS'])
assert f'timeout --signal=KILL {outer_seconds} python3' in shell
offset = re.search(r'^head -c ([0-9]+) [|] tar', shell, re.M)
assert offset
bootstrap_bytes = int(offset[1])
payload = Path(os.environ['FIXTURE_SOURCE_BYTES']).with_suffix('.payload')
payload.unlink(missing_ok=True)


def record_bundle():
    bundle = payload.read_bytes()[bootstrap_bytes:] if payload.exists() else b''
    if b'\\n\\n' not in bundle:
        return
    assert bundle.startswith((b'# v2 git bundle\\n', b'# v3 git bundle\\n'))
    header, pack = bundle.split(b'\\n\\n', 1)
    assert pack.startswith(b'PACK')
    Path(os.environ['FIXTURE_SOURCE_BYTES']).write_text(str(len(bundle)))
    with Path(os.environ['FIXTURE_BUNDLE_FORMS']).open('a') as forms:
        forms.write('delta\\n' if any(line.startswith(b'-') for line in header.splitlines()) else 'first\\n')


# A separate process carries the input, as the SSH hops do: the guest input ends when the natedev link ends,
# not when this process ends. A disconnect ends the link inside the bundle.
guest_input, link = os.pipe()
forward = ['head', '-c', str(bootstrap_bytes + 16)] if scenario == 'disconnect' else ['tee', str(payload)]
_ = subprocess.Popen(forward, stdout=link)
before_copy = scenario in ('disconnect-before-copy', 'interrupted-before-copy')
lost_reply = scenario in ('lost-run-reply', 'lost-confirmed-reply')
# A guest whose SSH server has not seen the end of the link keeps its input open.
held = before_copy or 'FIXTURE_HELD_LINK' in os.environ
guest = subprocess.Popen(['bash', '-c', shell], stdin=guest_input,
                         stdout=subprocess.DEVNULL if before_copy or lost_reply or fault == 'reset-after-start' else None,
                         stderr=subprocess.PIPE if before_copy or lost_reply else None,
                         env=environment, start_new_session=True, pass_fds=(link,) if held else ())
os.close(guest_input)
os.close(link)
if before_copy or lost_reply:
    import selectors

    with selectors.DefaultSelector() as ready:
        ready.register(guest.stderr, selectors.EVENT_READ)
        assert ready.select(3), 'The guest gave no start diagnostic.'
    first_line = guest.stderr.readline()
    accepted = first_line.startswith(b'mac-linux command accepted ')
    if accepted:
        sys.stderr.buffer.write(first_line)
        sys.stderr.buffer.flush()
if lost_reply:
    assert accepted
    diagnostics = guest.stderr.read()
    if scenario == 'lost-confirmed-reply':
        for line in diagnostics.splitlines(keepends=True):
            if line.startswith(b'mac-linux run start '):
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
if before_copy:
    if accepted:
        first_line = guest.stderr.readline()
    assert first_line.startswith(b'mac-linux run locked ')
    assert guest.stderr.readline() == b'fixture pause before take_copy\\n'
    record_bundle()
    if scenario == 'interrupted-before-copy':
        print('fixture local SSH is still active', file=sys.stderr, flush=True)
        signal.pause()
    os._exit(255)
status = guest.wait()
record_bundle()
if lost_reply:
    assert status == 0, status
    sys.exit(255)
if status == 0 and fault == 'reset-after-start':
    print(reset, file=sys.stderr)
    sys.exit(255)
sys.exit(status)
'''


BWRAP_FIXTURE = '''import os
from pathlib import Path
import subprocess
import sys

if sys.argv[1] == '--json-status-fd':
    # The read limit of a hand made run: run the argv after the limit words, and write its exit status.
    words = sys.argv[1:]
    end = words.index('--')
    assert {'--unshare-net', '--remount-ro', '--chdir'} <= set(words[:end]), words
    status = subprocess.call(words[end + 1:])
    os.write(int(words[1]), f'{{"exit-code": {status}}}\\n'.encode())
    sys.exit(status)
flags = ['--die-with-parent', '--unshare-pid', '--dev-bind', '/', '/',
         '--proc', '/proc', '--bind', os.environ['TMPDIR'], '/tmp', '--tmpfs', '/dev/shm', '--']
assert sys.argv[1:1 + len(flags)] == flags, sys.argv[1:]
assert Path(os.environ['TMPDIR']).is_dir()
os.environ['FIXTURE_SANDBOX'] = 'bwrap'
command = sys.argv[1 + len(flags):]
os.execvp(command[0], command)
'''


SANDBOX_SUITE = '''import fcntl
import os
from pathlib import Path
import signal
import socket
import sys

tmp_device, tmp_inode, shm_device, pid_namespace, lock = map(int, sys.argv[1:])
tmp = os.stat('/tmp')
assert (tmp.st_dev, tmp.st_ino) == (tmp_device, tmp_inode), 'The suite /tmp must use the run folder.'
assert os.stat('/dev/shm').st_dev != shm_device, 'The suite /dev/shm must have its own file system.'
assert os.stat('/proc/self/ns/pid').st_ino != pid_namespace, 'The suite must have its own PID namespace.'
assert os.readlink('/proc/self') == str(os.getpid()), 'The suite /proc must show its own PIDs.'
Path('/tmp/suite-output').write_text('suite output')
Path('/dev/shm/suite-output').write_text('shared memory output')
if os.fork() == 0:
    # Limit the child lifetime if the sandbox fails to stop it.
    signal.alarm(6)
    os.setsid()
    assert os.getsid(0) == os.getpid()
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('The separate session holds the run lock.', flush=True)
signal.pause()
'''


GATE_FIXTURE = '''from dataclasses import replace
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
from time import sleep

from scripts.copies import copies
from scripts.production.gate_modules import discover_test_modules
from scripts.production.gate_routes import AllModules, GateJobKind, GateJobResult, GateRun, SelectedModules, SuiteMode
from scripts.test_times.gate_record import TYPE_CHECK_JOB, WallClockSpan


def checked_git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


class WholeMachine:
    module_progress = None
    rerun_failed_modules = False

    def __init__(self, module_progress=None, *, rerun_failed_modules=False):
        self.module_progress = module_progress
        self.rerun_failed_modules = rerun_failed_modules


def run_suites(repo, *, type_dirs, full, mode, machine, selected, module_time_limit_s, time_limit):
    assert os.environ['FIXTURE_SANDBOX'] == 'bwrap'
    guest = Path(os.environ['FIXTURE_GUEST_HOME'])
    assert repo == guest / 'build/claude' / os.environ['FIXTURE_EXPECTED_UNIT'] / '1', repo
    assert Path.home().is_relative_to(repo / 'target')
    assert Path(os.environ['XDG_RUNTIME_DIR']).is_relative_to(repo / 'target')
    assert isinstance(machine, WholeMachine)
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
    assert module_time_limit_s == float(os.environ.get('FIXTURE_MODULE_SECONDS', '3'))
    assert checked_git(repo, 'show', 'origin/main:history.txt') == 'old source'
    assert int(checked_git(repo, 'rev-list', '--count', 'main', 'origin/main')) >= 2
    assert checked_git(repo, 'rev-parse', 'main') == os.environ['FIXTURE_EXPECTED_MAIN']
    assert checked_git(repo, 'rev-parse', 'origin/main') == os.environ['FIXTURE_EXPECTED_MAIN']
    assert full == (mode is SuiteMode.GATE or isinstance(selected, AllModules))
    print('A guest log line must stay outside the JSON result.')
    scenario = os.environ['FIXTURE_GUEST_RESULT']
    if scenario == 'child-stdout':
        subprocess.run(['echo', 'A child log line must stay outside the JSON result.'], check=True)
    if scenario == 'hang':
        Path(os.environ['FIXTURE_RUN_GROUP']).write_text(str(os.getpgrp()))
        signal.pause()
    if scenario == 'runner-failure':
        raise RuntimeError('fixture guest runner failed')
    if scenario in ('lost-run-reply', 'lost-confirmed-reply'):
        Path(os.environ['FIXTURE_RUN_GROUP']).write_text(str(os.getpgrp()))
    if scenario == 'time-limit':
        sleep(time_limit.seconds)
        report = f'time limit: limit {time_limit.seconds:g} s'
        jobs = tuple(GateJobResult(suite, path.name, 124, time_limit.seconds, report)
                     for suite, path in discover_test_modules(repo))
        return GateRun(jobs, time_limit.seconds, 0, time_limit_report=report)
    if scenario == 'leave-edit':
        (repo / 'history.txt').write_text('copy edit\\n')
    timing = dict(wall_s=1.3, cpu_s=1.0, tests_s=0.7, tests_run=3, failures=0, errors=0, tests=[])
    paths = [(suite, path) for suite, path in discover_test_modules(repo)
             if not isinstance(selected, SelectedModules) or str(path.relative_to(repo)) in selected.paths]
    machine.module_progress('Runner: 3 modules, whole machine, 2 CPUs')
    for suite, path in paths:
        machine.module_progress(f'{suite}/{path.name} exit=0 seconds=1.3')
    jobs = tuple(GateJobResult(suite, path.name, int(scenario == 'red'), 1.3, 'fixture module output',
                               cpu_seconds=1.0, times=dict(timing, skipped=int(path.name == 'test_second.py')),
                               peak_mib=22) for suite, path in paths)
    if scenario == 'sequential':
        first, second, other = jobs
        assert [job.suite for job in jobs] == ['fixture', 'fixture', 'other']
        jobs = (replace(first, seconds=1, wall_clock=WallClockSpan(1000, 1001)),
                replace(second, seconds=1, wall_clock=WallClockSpan(1001, 1002)),
                replace(other, seconds=4, wall_clock=WallClockSpan(1005, 1009)))
        return GateRun(jobs, 9, 30)
    if type_dirs:
        result = subprocess.run([os.environ['GATE_BASEDPYRIGHT'], *type_dirs], cwd=repo,
                                capture_output=True, text=True)
        jobs += (GateJobResult(*TYPE_CHECK_JOB, result.returncode, 0, result.stdout + result.stderr,
                               kind=GateJobKind.TYPE_CHECK),)
    executions = jobs
    if scenario == 'rerun-alone':
        assert machine.rerun_failed_modules, 'The guest gate did not enable failure reruns.'
        first = replace(jobs[0], returncode=1, output='fixture first failure')
        executions = (first, *jobs[1:], jobs[0])
    return GateRun(jobs, 2.0, 30, selection_certified=isinstance(selected, SelectedModules), executions=executions)
'''


TYPE_CHECK_FIXTURE = '''import os
from pathlib import Path
import sys

repo = Path.cwd()
state = Path(os.environ['GATE_STATE_DIR'])
assert state.parent == Path(os.environ['TMPDIR']).parent
assert state.is_relative_to(repo / 'target')
state.mkdir()
paths = [str(Path(path).relative_to(repo)) for path in sys.argv[1:]]
print('Type check: ' + ', '.join(paths))
'''


GUEST_BOOTSTRAP = '''from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import runpy
import signal
import socket
import sys

import scripts
from scripts.aws_target import gate_box, ledger

os.chdir(sys.argv[1])
socket.gethostname = lambda: 'nixos'
if os.environ['FIXTURE_GUEST_RESULT'] in ('disconnect-before-copy', 'interrupted-before-copy'):
    print('fixture pause before take_copy', file=sys.stderr, flush=True)
    signal.pause()
sys.argv = sys.argv[2:]
runner_root = Path(sys.argv[0]).parents[2]
scripts.__path__ = [str(runner_root / 'scripts'), *scripts.__path__]
if '--run-copy' in sys.argv:
    spec = spec_from_file_location('scripts.production.gate', runner_root / 'scripts/production/gate.py')
    gate = module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
runpy.run_path(sys.argv[0], run_name='__main__')
'''


class GuestCheckoutTests(unittest.TestCase):
    settings: mac_linux.GuestSettings = mac_linux.GuestSettings(
        ('unused-mac', 'unused-guest'), pwd.getpwuid(os.getuid()).pw_name, 1, 10, 5)
    machine_id: str = 'a' * 32
    repo: Path = Path('/natedev/verify checkout')

    @contextmanager
    def probe_replies(self, *replies: tuple[str, str, int]) -> Generator[Mock]:
        commands: list[tuple[str, ...]] = []
        for stdout, stderr, status in replies:
            script = (f'import sys\nsys.stdout.buffer.write({stdout.encode()!r})\n'
                      + f'sys.stderr.buffer.write({stderr.encode()!r})\nraise SystemExit({status})')
            commands.append((sys.executable, '-I', '-S', '-c', script))
        with patch.object(mac_linux.GuestSettings, 'command', side_effect=commands) as probe:
            yield probe

    def probe_reply(self, stdout: str, stderr: str = '') -> mac_linux.GuestCheckoutIdentity:
        with self.probe_replies((stdout, stderr, 0), (stdout, stderr, 0)):
            return mac_linux.read_guest_checkout(self.settings, self.repo)

    def test_read_guest_checkout_quotes_probe_output_on_invalid_identity(self) -> None:
        identity = f'checkout {self.machine_id} /guest/.claude\n'
        stderr = 'fixture warning\r\nsecond line\n'
        for stdout in ('shell notice\n' + identity, identity + 'shell notice\n',
                       f'checkout {self.machine_id} relative/path\n', identity.replace('/guest', '/gu\0est'),
                       identity.replace('/guest', '/gu\rest'), identity.replace(self.machine_id, 'A' * 32)):
            with self.subTest(stdout=stdout):
                with self.assertRaises(mac_linux.GuestUnavailable) as caught:
                    _ = self.probe_reply(stdout, stderr)
                self.assertIn('the guest checkout probe returned an invalid identity', str(caught.exception))
                self.assertIn(f'natedev checkout {str(self.repo)!r}', str(caught.exception))
                self.assertIn(f'exit 0 stdout={stdout!r} stderr={stderr!r}', str(caught.exception))

    def test_read_guest_checkout_bounds_probe_diagnostics(self) -> None:
        stdout, stderr = 'out\n' * 200, 'err\r' * 200
        for status in (0, 255):
            with self.subTest(status=status):
                reply = (stdout, stderr, status)
                with (self.probe_replies(reply, reply), self.assertRaises(mac_linux.GuestUnavailable) as caught):
                    _ = mac_linux.read_guest_checkout(self.settings, self.repo)
                details = str(caught.exception).split(' | ')
                self.assertEqual(len(details), 1 if status else 2)
                for diagnostic in details:
                    bounded = f'exit {status} stdout={repr(stdout)[:300]} stderr={repr(stderr)[:300]}'
                    self.assertTrue(diagnostic.endswith(bounded), diagnostic)

    def test_read_guest_checkout_accepts_only_lf_and_crlf_line_endings(self) -> None:
        path = '/guest home/.claude'
        identity = f'checkout {self.machine_id} {path}'
        expected = mac_linux.GuestCheckoutIdentity(self.machine_id, path)
        self.assertEqual(self.probe_reply(identity + '\n'), expected)
        self.assertEqual(self.probe_reply(identity + '\r\n'), expected)
        with self.assertRaises(mac_linux.GuestUnavailable):
            _ = self.probe_reply(identity + '\r')

    def test_read_guest_checkout_retries_one_invalid_reply(self) -> None:
        path = '/guest/.claude'
        identity = f'checkout {self.machine_id} {path}\n'
        with self.probe_replies(('shell notice\n' + identity, '', 0), (identity, '', 0)) as probe:
            self.assertEqual(mac_linux.read_guest_checkout(self.settings, self.repo),
                             mac_linux.GuestCheckoutIdentity(self.machine_id, path))
        self.assertEqual(probe.call_count, 2)

    def test_read_guest_checkout_reports_both_invalid_replies(self) -> None:
        first = ('first bad reply\n', 'first warning\n', 0)
        second = ('second bad reply\r\n', 'second warning\r\n', 0)
        with (self.probe_replies(first, second) as probe, self.assertRaises(mac_linux.GuestUnavailable) as caught):
            _ = mac_linux.read_guest_checkout(self.settings, self.repo)
        self.assertEqual(probe.call_count, 2)
        message = str(caught.exception)
        self.assertIn(f'natedev checkout {str(self.repo)!r}', message)
        first_stdout, first_stderr, _ = first
        second_stdout, second_stderr, _ = second
        self.assertTrue(message.endswith(f'exit 0 stdout={first_stdout!r} stderr={first_stderr!r} | '
                                         + f'exit 0 stdout={second_stdout!r} stderr={second_stderr!r}'), message)

    def test_read_guest_checkout_does_not_cache_a_failed_probe(self) -> None:
        bad = ('unexpected reply\n', '', 0)
        path = '/other-guest/.claude'
        with self.probe_replies(bad, bad, (f'checkout {self.machine_id} {path}\n', '', 0)) as probe:
            with self.assertRaises(mac_linux.GuestUnavailable):
                _ = mac_linux.read_guest_checkout(self.settings, self.repo)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(mac_linux.read_guest_checkout(self.settings, self.repo),
                             mac_linux.GuestCheckoutIdentity(self.machine_id, path))
        self.assertEqual(probe.call_count, 3)

    def test_read_guest_checkout_does_not_retry_nonzero_exit(self) -> None:
        stdout = f'checkout {self.machine_id} /guest/.claude\n'
        stderr = 'fixture connection closed\n'
        with (self.probe_replies((stdout, stderr, 255)) as probe,
              self.assertRaises(mac_linux.GuestUnavailable) as caught):
            _ = mac_linux.read_guest_checkout(self.settings, self.repo)
        probe.assert_called_once()
        self.assertIn(f'exit 255 stdout={stdout!r} stderr={stderr!r}', str(caught.exception))

    def test_read_guest_checkout_does_not_retry_a_timeout(self) -> None:
        with (self.probe_replies(('', '', 0)) as probe,
              patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 1)) as run,
              self.assertRaises(subprocess.TimeoutExpired)):
            _ = mac_linux.read_guest_checkout(self.settings, self.repo)
        probe.assert_called_once()
        run.assert_called_once()

    def test_read_guest_checkout_reports_partial_output_when_retry_times_out(self) -> None:
        first_stdout, first_stderr = 'first invalid\n' * 100, 'first warning\r' * 100
        retry_stdout, retry_stderr = 'retry output\n' * 100, 'retry warning\r' * 100
        first = subprocess.CompletedProcess(('fixture',), 0, first_stdout.encode(), first_stderr.encode())
        timeout = subprocess.TimeoutExpired('fixture', 1, output=retry_stdout.encode(), stderr=retry_stderr.encode())
        with (patch.object(subprocess, 'run', side_effect=[first, timeout]) as run,
              self.assertRaises(mac_linux.GuestUnavailable) as caught):
            _ = mac_linux.read_guest_checkout(self.settings, self.repo)
        self.assertEqual(run.call_count, 2)
        message = str(caught.exception)
        self.assertIn(f'natedev checkout {str(self.repo)!r}', message)
        self.assertTrue(message.endswith(
            f'exit 0 stdout={repr(first_stdout)[:300]} stderr={repr(first_stderr)[:300]} | '
            + f'retry timed out stdout={repr(retry_stdout)[:300]} stderr={repr(retry_stderr)[:300]}'), message)

    def test_guest_checkout_ignores_cd_output_in_the_probe_and_guard(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = root / 'physical home'
        home.mkdir()
        alias = root / 'linked-home'
        alias.symlink_to(home, target_is_directory=True)
        expected = mac_linux.GuestCheckoutIdentity(Path('/etc/machine-id').read_text().strip(),
                                                   str(home.resolve() / '.claude'))
        preamble = 'cd() { builtin cd "$@" && printf "cd output\\n"; }\n'

        def local_bash(shell: str) -> tuple[str, ...]:
            return ('bash', '-c', preamble + shell)

        with patch.dict(os.environ, {'HOME': str(alias), 'CDPATH': str(root)}):
            with (self.subTest(command='probe'),
                  patch.object(mac_linux.GuestSettings, 'command', side_effect=local_bash)):
                self.assertEqual(mac_linux.read_guest_checkout(self.settings, self.repo), expected)
            with self.subTest(command='guard'):
                settings = replace(self.settings, checkout_identity=expected)
                reply = subprocess.run(local_bash(mac_linux.guest_checkout_guard(settings)), capture_output=True,
                                       check=False, timeout=5)
                self.assertEqual(reply.returncode, 0, reply.stderr)
                self.assertEqual(reply.stdout, b'')


class GuestEntryTests(unittest.TestCase):
    def test_gate_entry_gives_the_runner_the_request_and_the_run_copy_word(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runner = root / mac_linux.RUNNER_PATH
        runner.parent.mkdir(parents=True)
        candidate = Path(__file__).resolve().parents[2]
        library = root / 'scripts/lib'
        library.mkdir()
        _ = shutil.copy2(candidate / 'scripts/lib/py', library / 'py')
        _ = shutil.copytree(candidate / 'scripts/lib/python_startup', library / 'python_startup')
        _ = runner.with_name('runner_fixture.py').write_text("marker = 'runner commit'\n", encoding='utf-8')
        _ = runner.write_text('import json, os, sys\n'
                              + 'from scripts.build_targets.runner_fixture import marker\n'
                              + 'print(json.dumps(sys.argv))\nprint(marker)\nprint(os.getcwd())\n',
                              encoding='utf-8')
        request = root / 'request.json'
        command = mac_linux.gate_entry_command(1, runner, request, 7)
        output = subprocess.run(command, cwd=candidate, capture_output=True, text=True, check=True, timeout=30,
                                env={**os.environ, 'CLAUDE_PYTHON': sys.executable}).stdout.splitlines()
        self.assertEqual(output, [f'["{runner}", "{request}", "--run-copy", "7"]', 'runner commit', str(candidate)])


class MacLinuxSandboxTests(unittest.TestCase):
    def test_deadline_stops_the_sandbox_and_releases_the_run_lock(self) -> None:
        if shutil.which('bwrap') is None:
            self.fail('This Mac Linux sandbox test requires Bubblewrap (bwrap) on PATH.')
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        tmp = root / 'suite-tmp'
        tmp.mkdir()
        tmp_identity = tmp.stat()
        run_lock = root / 'run.lock'
        output_path = root / 'suite.log'
        with run_lock.open('wb') as lock, output_path.open('wb') as output:
            descriptor = lock.fileno()
            command = mac_linux.sandboxed_suite_command(tmp, [
                sys.executable, '-I', '-S', '-c', SANDBOX_SUITE, str(tmp_identity.st_dev), str(tmp_identity.st_ino),
                str(os.stat('/dev/shm').st_dev),
                str(os.stat('/proc/self/ns/pid').st_ino), str(descriptor),
            ])
            # A surviving child must not keep the deadline check waiting for a pipe.
            result = subprocess.run(['timeout', '--signal=KILL', '3', *command], pass_fds=(descriptor,),
                                    stdout=output, stderr=subprocess.STDOUT, timeout=7, check=False)
        report = output_path.read_text()
        self.assertIn('The separate session holds the run lock.', report, report)
        self.assertEqual(result.returncode, -signal.SIGKILL, report)
        self.assertEqual((tmp / 'suite-output').read_text(), 'suite output')
        unlocked = subprocess.run(['flock', '--exclusive', '--timeout', '1', str(run_lock), 'true'],
                                  timeout=2, check=False)
        self.assertEqual(unlocked.returncode, 0, 'The guest run lock is still held after the deadline.')


# A hand made program under the guest read limit: it prints the source text of its copy, then yes or no for each
# try. It also tries to write each outside path; the fixture is under /tmp, so such a write can land in the private
# /tmp of the limit, and the test reads the real paths.
LIMIT_PROGRAM = '''from pathlib import Path
import socket
import sys

copy, sibling, key, port, *outside = sys.argv[1:]


def connect():
    with socket.create_connection(('127.0.0.1', int(port)), timeout=2):
        pass


tries = (lambda: Path(sibling, 'source.txt').read_text(), lambda: Path(key).read_text(),
         lambda: Path(copy, 'written').write_text('copy'), lambda: Path(copy, 'target/built').write_text('output'),
         lambda: Path('/tmp/written').write_text('tmp'), lambda: Path('/hand-made-written').write_text('root'),
         connect)
results = []
for attempt in tries:
    try:
        attempt()
    except OSError:
        results.append('no')
    else:
        results.append('yes')
for path in outside:
    try:
        Path(path).write_text('outside')
    except OSError:
        pass
print(Path(copy, 'source.txt').read_text().strip(), *results, flush=True)
'''


@contextmanager
def standard_output(path: Path) -> Generator[None]:
    """Send file descriptor 1, which the guest run writes, to the file while the block runs."""
    _ = sys.stdout.flush()
    saved = os.dup(1)
    try:
        with path.open('wb') as output:
            _ = os.dup2(output.fileno(), 1)
        yield
    finally:
        _ = sys.stdout.flush()
        _ = os.dup2(saved, 1)
        os.close(saved)


class GuestLimitTests(unittest.TestCase):
    """A hand made argv runs under the read limit with the real bwrap, in a fixture home with two numbered copies."""
    root: Path = Path()
    home: Path = Path()
    copy: Path = Path()
    sibling: Path = Path()
    tmp: Path = Path()

    @override
    def setUp(self) -> None:
        if shutil.which('bwrap') is None:
            self.fail('This Mac Linux sandbox test requires Bubblewrap (bwrap) on PATH.')
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / 'home'
        self.copy, self.sibling = self.home / 'build/claude/unit/1', self.home / 'build/claude/unit/2'
        # The runner makes the run tool folder in the copy target folder.
        self.tmp = self.copy / 'target/runtime/tmp'
        for folder in (self.home / '.ssh', self.sibling, self.tmp):
            folder.mkdir(parents=True)
        _ = (self.home / '.ssh/id_fixture').write_text('key\n')
        for folder in (self.copy, self.sibling):
            _ = (folder / 'source.txt').write_text(f'copy {folder.name}\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'HOME': str(self.home)})))

    def run_limited(self, argv: list[str], tmp: Path | None = None) -> tuple[int, str, str]:
        """Run the argv as the runner does: the status, the stream standard output and the runner stderr."""
        runtime = self.copy / 'target/runtime'
        environment = {**os.environ, 'HOME': str(runtime / 'home'), 'TMPDIR': str(self.tmp),
                       'CARGO_HOME': str(runtime / 'home/.cargo'), 'RUSTUP_HOME': str(runtime / 'home/.rustup'),
                       'CARGO_TARGET_DIR': str(self.copy / 'target')}
        output, errors = self.root / 'output', io.StringIO()
        with standard_output(output), redirect_stderr(errors):
            status = mac_linux.run_limited(tmp or self.tmp, argv, self.copy, environment, 'fixture')
        return status, output.read_text(), errors.getvalue()

    def test_a_hand_made_argv_reads_and_writes_only_its_copy_and_has_no_network(self) -> None:
        outside = (self.root / 'outside', self.home / '.ssh/written', self.sibling / 'written')
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            port = cast(tuple[str, int], server.getsockname())[1]
            status, output, errors = self.run_limited([
                os.path.realpath(sys.executable), '-I', '-c', LIMIT_PROGRAM, str(self.copy), str(self.sibling),
                str(self.home / '.ssh/id_fixture'), str(port), *map(str, outside)])
        self.assertEqual(status, 0, output + errors)
        # The sibling copy and the key do not exist for the program. It writes its copy, its output folder and its
        # own /tmp, and not the root of the limit; and it reaches no port outside.
        self.assertEqual(output, f'{mac_linux.run_start_line("fixture")}\ncopy 1 no no yes yes yes no no\n')
        self.assertEqual([(self.copy / 'written').exists(), (self.copy / 'target/built').exists(),
                          (self.tmp / 'written').exists()], [True, True, True])
        self.assertEqual([path for path in outside if path.exists()], [])

    def test_a_limit_that_bwrap_cannot_set_up_starts_nothing_and_refuses_the_run(self) -> None:
        mark = self.copy / 'ran'
        status, output, errors = self.run_limited(['touch', str(mark)], tmp=self.root / 'missing-tmp')
        self.assertEqual((status, output), (1, f'{mac_linux.run_refused_line("fixture")}\n'))
        self.assertIn(mac_linux.BWRAP_FAILED.format(status=1), errors)
        self.assertFalse(mark.exists())
        with patch.dict(os.environ, {'PATH': str(self.root)}):
            status, output, errors = self.run_limited(['touch', str(mark)])
        self.assertEqual((status, output, errors),
                         (1, f'{mac_linux.run_refused_line("fixture")}\n', mac_linux.BWRAP_MISSING + '\n'))
        self.assertFalse(mark.exists())

    def test_a_hand_made_guest_drops_sccache_and_disables_the_cargo_wrapper(self) -> None:
        cache = self.root / 'cache'
        cache.mkdir()
        inherited = {'SCCACHE_DIR': str(cache), 'SCCACHE_CACHE_SIZE': '77G',
                     'SCCACHE_SERVER_UDS': str(cache / 'server.sock'),
                     'RUSTC_WRAPPER': 'sccache', 'CARGO_BUILD_RUSTC_WRAPPER': 'sccache'}
        probe = ("import os, sys\n"
                 "print(repr(os.environ['RUSTC_WRAPPER']), repr(os.environ['CARGO_BUILD_RUSTC_WRAPPER']))\n"
                 "print(any(name in os.environ for name in sys.argv[2:]),"
                 " os.path.exists(sys.argv[1]))")
        with patch.dict(os.environ, inherited):
            status, output, errors = self.run_limited([
                os.path.realpath(sys.executable), '-I', '-c', probe, str(cache),
                'SCCACHE_DIR', 'SCCACHE_CACHE_SIZE', 'SCCACHE_SERVER_UDS'])
        self.assertEqual(status, 0, output + errors)
        self.assertEqual(output, f"{mac_linux.run_start_line('fixture')}\n'' ''\nFalse False\n")


class GuestCommandParityTests(unittest.TestCase):
    """The guest starts only a file that natedev read, or a tool: command_refusal checks each argv word."""
    copy: Path = Path()
    tools: Path = Path()
    user: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.copy, self.tools, self.user = root / 'copy', root / 'tools', root / 'user'
        for folder in (self.copy / 'target', self.copy / 'scripts', self.tools, self.user):
            folder.mkdir(parents=True)
        _ = self.enterContext(patch.object(mac_linux, 'TOOL_FOLDERS', (self.tools,)))

    @staticmethod
    def program(path: Path, data: bytes) -> Path:
        _ = path.write_bytes(data)
        path.chmod(0o755)
        return path

    def refusal(self, *argv: str, checked: tuple[str, ...] = (), search_path: str | None = None) -> str:
        path = f'{self.tools}{os.pathsep}{self.user}' if search_path is None else search_path
        return mac_linux.command_refusal(self.copy, argv, list(checked), path)

    def test_a_word_with_a_checked_file_runs_the_checked_blob(self) -> None:
        _ = self.program(self.copy / 'scripts/run.sh', b'echo checked\n')
        for word in ('scripts/run.sh', './scripts/run.sh'):
            with self.subTest(word=word):
                self.assertEqual(self.refusal(word, checked=('scripts/run.sh',)), '')

    def test_a_word_with_a_slash_and_no_checked_file_must_name_no_guest_file(self) -> None:
        _ = self.program(self.copy / 'target/run.sh', b'echo unread\n')
        outside = self.program(self.user / 'outside.sh', b'echo outside\n')
        for word, reason in (('target/run.sh', f'the guest has a file at {self.copy / "target/run.sh"}'),
                             ('target/', f'the guest has a file at {self.copy / "target"}'),
                             ('../user/outside.sh', 'the path has ..'),
                             (str(outside), f'the guest has a file at {outside}')):
            with self.subTest(word=word):
                self.assertEqual(self.refusal('bash', word),
                                 f'The guest refuses the command word {word}: {reason}, and natedev read no file there.')
        self.assertEqual(self.refusal('bash', 'target/missing.sh', '--out=a/b', 'target/run.sh/x'), '')

    def test_a_word_with_a_slash_through_a_symbolic_link_is_refused(self) -> None:
        # Outside the sandbox /proc/self/cwd is the main checkout, so a check that follows the link finds no file;
        # in the copy the link leads to the unread target/h/run.sh.
        (self.copy / 'target/h').mkdir()
        _ = self.program(self.copy / 'target/h/run.sh', b'echo unread\n')
        (self.copy / 'target/d').symlink_to('/proc/self/cwd/target/h')
        self.assertEqual(self.refusal('bash', 'target/d/run.sh'),
                         f'The guest refuses the command word target/d/run.sh: {self.copy / "target/d"} is a symbolic '
                         + 'link, and natedev read no file there.')

    def test_a_path_that_the_guest_cannot_read_is_refused(self) -> None:
        denied = OSError(errno.EACCES, 'Permission denied')
        with patch.object(Path, 'lstat', side_effect=denied):
            for argv, word in ((('bash', 'target/run.sh'), 'target/run.sh'), (('bash', 'helper'), 'helper')):
                with self.subTest(word=word):
                    self.assertEqual(self.refusal(*argv),
                                     f'The guest refuses the command word {word}: '
                                     + f'{self.copy / Path(word).parts[0]} cannot be checked (Permission denied), '
                                     + 'and natedev read no file there.')

    def test_a_later_bare_word_with_an_unread_file_in_the_copy_is_refused(self) -> None:
        # The copy keeps the stamp that an earlier run wrote, and bash opens a later word from the copy.
        _ = (self.copy / '.lint-sweep-used').write_text('rm -rf ~/work\n')
        (self.copy / 'linked').symlink_to(self.copy / 'scripts')
        for word, reason in (('.lint-sweep-used', f'the guest has a file at {self.copy / ".lint-sweep-used"}'),
                             ('linked', f'{self.copy / "linked"} is a symbolic link')):
            with self.subTest(word=word):
                self.assertEqual(self.refusal('bash', word),
                                 f'The guest refuses the command word {word}: {reason}, and natedev read no file there.')
        _ = self.program(self.copy / 'scripts/run.sh', b'echo checked\n')
        self.assertEqual(self.refusal('bash', 'run.sh', checked=('run.sh',)), '')
        # A plain folder passes: cargo test runs with a test folder in the copy.
        (self.copy / 'test').mkdir()
        self.assertEqual(self.refusal('cargo', 'test'), '')

    def test_a_bare_word_runs_only_a_tool_or_a_compiled_file(self) -> None:
        _ = self.program(self.tools / 'wrapped', b'#!/bin/sh\necho tool\n')
        _ = self.program(self.user / 'compiled', b'\x7fELF\x02\x01\x01')
        (self.user / 'linked').symlink_to(self.tools / 'wrapped')
        for word in ('missing-program', 'wrapped', 'compiled', 'linked'):
            with self.subTest(word=word):
                self.assertEqual(self.refusal('timeout', '60', word), '')

    def test_a_bare_word_with_a_text_match_outside_the_tool_folders_is_refused(self) -> None:
        _ = self.program(self.user / 'helper', b'rm -rf ~/work\n')
        # A wrapper goes on to a later match when a start fails, so the later match counts too.
        _ = self.program(self.tools / 'both', b'#!/missing/interpreter\n')
        _ = self.program(self.user / 'both', b'#!/bin/sh\necho later\n')
        for word in ('helper', 'both'):
            with self.subTest(word=word):
                self.assertEqual(self.refusal('timeout', '60', word),
                                 f'The guest refuses the command word {word}: the guest PATH finds {self.user / word}, '
                                 + 'a program file that natedev did not read; use a file in the checkout.')

    def test_a_later_word_with_only_read_access_on_the_path_is_refused(self) -> None:
        # bash finds a script name on PATH with no execute bit; execvp of the first word does not.
        helper = self.user / 'helper'
        _ = helper.write_bytes(b'rm -rf ~/work\n')
        helper.chmod(0o644)
        self.assertEqual(self.refusal('bash', 'helper'),
                         f'The guest refuses the command word helper: the guest PATH finds {helper}, '
                         + 'a program file that natedev did not read; use a file in the checkout.')
        self.assertEqual(self.refusal('helper'), '')

    def test_a_later_word_that_names_a_folder_with_a_main_file_is_refused(self) -> None:
        # Python 3.13 runs a folder that holds only __main__.pyc.
        _ = (self.copy / 'target/__main__.pyc').write_bytes(b'compiled')
        _ = (self.copy / 'scripts/__main__.py').write_text('print("unread")\n')
        _ = (self.copy.parent / '__main__.py').write_text('print("unread")\n')
        for word, folder in (('target', self.copy / 'target'), ('./target', self.copy / 'target'),
                             ('scripts', self.copy / 'scripts'), ('..', self.copy / '..')):
            with self.subTest(word=word):
                self.assertEqual(self.refusal('python3', word),
                                 f'The guest refuses the command word {word}: python runs a __main__ file of the '
                                 + f'folder {folder}, and natedev read no file there.')
        # A plain folder is no package: cargo test runs with a test folder in the copy.
        (self.copy / 'test').mkdir()
        self.assertEqual(self.refusal('cargo', 'test'), '')

    def test_main_folder_counts_a_stat_error_and_passes_a_missing_entry(self) -> None:
        # A link to itself makes os.stat fail with ELOOP: the check cannot tell what the word names.
        (self.copy / 'loop').symlink_to('loop')
        self.assertTrue(mac_linux.main_folder(str(self.copy / 'loop')))
        # The refusal names the stat error, not a __main__ file.
        self.assertEqual(self.refusal('python3', 'loop'),
                         f'The guest refuses the command word loop: {self.copy / "loop"} cannot be checked '
                         + '(Too many levels of symbolic links), and natedev read no file there.')
        self.assertFalse(mac_linux.main_folder(str(self.copy / 'missing')))
        self.assertFalse(mac_linux.main_folder(str(self.copy / 'missing' / 'below')))

    def test_a_bare_word_found_in_the_copy_is_refused(self) -> None:
        _ = self.program(self.copy / 'target/built', b'\x7fELF\x02\x01\x01')
        refusal = self.refusal('built', search_path=f'target{os.pathsep}{self.tools}')
        self.assertIn(f'the guest PATH finds {self.copy / "target/built"}', refusal)


# A Mac claim identity for a fixture reservation that holds no claim.
FIXTURE_CLAIM = claim_state.ClaimIdentity(mac_test.MAC_LINUX_CLAIM_ID_PREFIX + 'fixture', 'episode', 'attempt')


class MacLinuxRouteTests(unittest.TestCase):
    bootstrap_cache: ClassVar[Path]
    root: Path = Path()
    guest_home: Path = Path()
    trace: Path = Path()
    repo: Path = Path()
    runner_repo: Path = Path()
    registry: Path = Path()
    placed: placement.PlacementAssignment = placement.PlacementAssignment(
        'gate-episode', 'gate-attempt', placement.PlacementDestination(
            'ssh', mac_linux.TARGET, 'fixture-guest', claim_state.RepositoryScope('claude'), 1),
        execution_identity.SourceKeyUnavailable('fixture'), placement.SubmittingOwner('natedev', 1, 'start'),
        OwningSession('submitting-session'), 0.0)
    # Each consume request, the SSH trace at that moment, and whether the gate held the named Mac claim.
    consumed: tuple[tuple[wire.ConsumePermission, str, bool], ...] = ()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.bootstrap_cache = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix='mac-linux-bytecode-'))).resolve()
        _ = subprocess.run(
            [sys.executable, '-I', '-X', f'pycache_prefix={cls.bootstrap_cache}', '-c',
             'import sys\n'
             + 'sys.path.insert(0, sys.argv[1])\n'
             + 'from scripts.aws_target import gate_box, ledger\n'
             + 'from scripts.build_targets import mac_linux\n'
             + 'from scripts.copies import copies\n'
             + 'from scripts.production import gate_modules, gate_routes\n'
             + 'from scripts.test_times import gate_record\n',
             str(CHECKOUT)], check=True, timeout=20)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.guest_home = self.root / 'guest'
        self.guest_home.mkdir()
        (self.root / 'target-state').mkdir()
        self.trace = self.root / 'ssh.log'
        self.trace.touch()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        account = pwd.getpwuid(os.getuid()).pw_name
        binaries = self.root / 'bin'
        binaries.mkdir()
        self.executable(binaries / 'ssh', f'#!{sys.executable} -B\n' + SSH_FIXTURE)
        self.executable(binaries / 'bwrap', f'#!{sys.executable} -B\n' + BWRAP_FIXTURE)
        (binaries / 'chromium').symlink_to(sys.executable)
        self.executable(binaries / 'basedpyright', f'#!{sys.executable} -B\n' + TYPE_CHECK_FIXTURE)
        (binaries / 'uv').symlink_to(sys.executable)
        config = self.root / 'config'
        config.mkdir()
        self.registry = config / 'build_targets.conf'
        _ = write_config_fixture(self.registry, f'enabled.mac-linux=on\n{target_config.UNREACHABLE_WAIT_KEY}=20\n')
        _ = (config / 'mac_linux.conf').write_text(
            f'ssh.hops=fixture-mac fixture-guest\nguest.user={account}\ntest.jobs=2\n'
            + 'run.time_limit_seconds=7\n',
            encoding='utf-8')
        _ = write_config_fixture(config / 'mac_test.conf', 'unreachable_backoff_s=30\n')
        _ = (config / 'build_admission.conf').write_text(
            '# range: 0.01..\nquery_timeout_seconds=1\n', encoding='utf-8')
        _ = (config / 'machines.conf').write_text(
            'aliases=natedev mac\nnatedev.platform=linux\nnatedev.hosts=natedev\n'
            + 'mac.platform=darwin\nmac.hosts=fixture-mac\n', encoding='utf-8')
        _ = write_config_fixture(config / 'build_folders.conf')
        _ = write_config_fixture(config / 'test_times.conf')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            'GATE_STATE_DIR': str(self.root / 'gate-state'),
            'GATE_PASSED_DIR': str(self.root / 'gate-passes'),
            'BUILDLOG_DIR': str(self.root / 'buildlog'),
            'MACHINE_FACTS_FILE': str(self.root / 'machine-facts.json'),
            'BUILD_TARGETS_STATE_DIR': str(self.root / 'target-state'),
            'MAC_TEST_STATE_DIR': str(self.root / 'mac-state'),
            'CONF_DIR': str(config), 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
            'GATE_BASEDPYRIGHT': str(binaries / 'basedpyright'), 'GATE_UV': str(binaries / 'uv'),
            'FIXTURE_SSH_TRACE': str(self.trace), 'FIXTURE_GUEST_HOME': str(self.guest_home),
            'FIXTURE_SOURCE_BYTES': str(self.root / 'source.bytes'),
            'FIXTURE_BUNDLE_FORMS': str(self.root / 'bundle.forms'),
            'FIXTURE_GUEST_USER': account, 'FIXTURE_GUEST_RESULT': 'pass',
            'FIXTURE_RUN_SECONDS': '7', 'FIXTURE_RUN_GROUP': str(self.root / 'run.group'),
            'FIXTURE_TRANSFER_SECONDS': str(mac_linux.TRANSFER_ALLOWANCE_SECONDS),
            'BUILDLOG_UNIT': 'caller', 'FIXTURE_EXPECTED_UNIT': 'caller',
            'GH_TOKEN': 'fixture-token', 'SSH_AUTH_SOCK': str(self.root / 'agent.sock'),
            'XDG_CONFIG_HOME': str(self.root / 'outside-config'),
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
            'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test',
            'ADMISSION_SOCKET': str(self.root / 'no-queue.sock'),
        })))
        self.consumed = ()

        def consume(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            holder = request.holder
            assert isinstance(holder, placement.ClaimHolderIdentity)
            held = mac_run.claim_held(mac_linux.GUEST_CLAIM_TARGET, request.claim.claim_id, holder.pid, holder.start_time)
            self.consumed = (*self.consumed, (request, self.trace.read_text(encoding='utf-8'), held))
            return wire.FirstAuthorization(request.assignment, request.claim)

        _ = self.enterContext(patch.object(admission_client, 'consume_permission', consume))
        def report(request: wire.ExecutionReport, *, deadline: float, timeout: float) -> wire.DispatchStatus:
            self.assertGreater(deadline, time.monotonic())
            self.assertGreater(timeout, 0)
            state = (placement.ClosedAssignment('completed') if isinstance(request.outcome, placement.AttemptCompleted)
                     else placement.CurrentAssignment())
            return wire.DispatchStatus(request.assignment, state, placement.PermissionConsumed(1.0))

        _ = self.enterContext(patch.object(mac_linux, 'send_guest_execution', report))
        _ = self.git('init', '-q', '--initial-branch=main')
        _ = (self.repo / 'history.txt').write_text('old source\n', encoding='utf-8')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'base')
        _ = self.git('remote', 'add', 'origin', 'https://example.test/claude_commands.git')
        for folder in ('config', 'scripts/lib', 'scripts/production', 'scripts/fixture', 'scripts/build_targets'):
            (self.repo / folder).mkdir(parents=True)
        _ = shutil.copyfile(config / 'build_folders.conf', self.repo / 'config/build_folders.conf')
        _ = (self.repo / 'scripts/production/gate.py').write_text(GATE_FIXTURE, encoding='utf-8')
        for module in ('test_first.py', 'test_second.py'):
            (self.repo / 'scripts/fixture' / module).touch()
        package_root = shlex.quote(str(CHECKOUT))
        cache_option = shlex.quote(f'pycache_prefix={self.bootstrap_cache}')
        shim = """#!/usr/bin/env bash
candidate="$PWD"
options=()
if [[ "$1" == "-X" ]]; then
    options=("$1" "$2")
    shift 2
fi
"""
        self.executable(self.repo / 'scripts/lib/py', shim
                        + f'cd {package_root}\nexport PYTHONPATH={package_root}\n'
                        + f'exec {shlex.quote(sys.executable)} -B "${{options[@]}}" -X {cache_option}'
                        + f' -c {shlex.quote(GUEST_BOOTSTRAP)} "$candidate" "$@"\n')
        _ = shutil.copyfile(Path(mac_linux.__file__), self.repo / mac_linux.RUNNER_PATH)
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'main checkout')
        _ = self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.runner_repo = self.root / 'runner'
        _ = self.git('clone', '-q', str(self.repo), str(self.runner_repo))
        _ = self.enterContext(patch.object(mac_linux, '__file__', str(self.runner_repo / mac_linux.RUNNER_PATH)))

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
              gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED, *,
              type_dirs: tuple[str, ...] = (),
              module_time_limit_s: float | None = 3) -> GateRun | mac_linux.GuestNotStarted | GuestGateBlocked | str:
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
                                     module_time_limit_s=module_time_limit_s, gate_job=gate_job, assignment=self.placed,
                                     type_dirs=type_dirs)
        self.assertEqual(self.git('for-each-ref', 'refs/mac-linux'), '')
        return result

    def not_started(self, result: GateRun | mac_linux.GuestNotStarted | GuestGateBlocked | str) -> str:
        self.assertIsInstance(result, mac_linux.GuestNotStarted)
        assert isinstance(result, mac_linux.GuestNotStarted)
        return result.reason

    def blocked(self, result: GateRun | mac_linux.GuestNotStarted | GuestGateBlocked | str) -> str:
        self.assertIsInstance(result, GuestGateBlocked)
        assert isinstance(result, GuestGateBlocked)
        return result.reason

    def unconfirmed(self, result: GateRun | mac_linux.GuestNotStarted | GuestGateBlocked | str) -> str:
        reason = self.not_started(result)
        assert isinstance(result, mac_linux.GuestNotStarted)
        self.assertTrue(result.settlement_required)
        self.assertTrue(result.retry_before_settlement)
        return reason

    def assert_guest_clean(self, *events: str) -> None:
        main = self.guest_home / '.claude'
        build = self.guest_home / 'build'
        self.assertLessEqual(set(self.guest_home.iterdir()), {main, build})
        self.assertFalse((main / '.git/mac-linux').exists())
        claim_locks = build / '.build-claim-locks'
        self.assertEqual([path for path in build.rglob('mac-linux*')
                          if not (path.name == 'mac-linux.lock' and path.is_relative_to(claim_locks))], [])
        self.assertEqual(self.trace.read_text(encoding='utf-8').splitlines(), list(events))

    def assert_bundle(self, run: mac_linux.MacLinuxGateRun, form: mac_linux.BundleForm,
                      reason: mac_linux.BundleReason, *forms: str) -> None:
        self.assertEqual(run.bundle_bytes, int((self.root / 'source.bytes').read_text(encoding='utf-8')))
        assert isinstance(run.bundle_bytes, int)
        self.assertGreater(run.bundle_bytes, 0)
        self.assertEqual((run.bundle_form, run.bundle_reason), (form, reason))
        self.assertEqual((self.root / 'bundle.forms').read_text(encoding='utf-8').splitlines(), list(forms))

    def test_switch_off_returns_local_reason_without_ssh(self) -> None:
        _ = self.registry.write_text('enabled.mac-linux=off\n', encoding='utf-8')
        self.assertEqual(self.route(), 'mac-linux is off (build_targets.conf enabled.mac-linux=off)')
        self.assert_guest_clean()

    def test_guest_end_proof_is_durable_before_the_report_send_fails(self) -> None:
        assignment = replace(self.placed, attempt=f'{16:032x}')
        destination = assignment.destination
        claim = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                                                 'claim-fixture', assignment.episode, assignment.attempt)
        armed = mac_linux.ArmedGuestAttempt('different-guest-arm-id', 'f' * 32, 'boot-id', 10, 'user', '/fixture')
        dispatch = mac_linux.GuestGateDispatch(assignment, claim, started=armed, settled=True)
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        path.parent.mkdir(parents=True)
        partial = b'{"unfinished":'
        _ = path.write_bytes(partial)
        expected = {'attempt': execution_identity.attempt_key(assignment.episode, assignment.attempt),
                    'claim': claim.claim_id, 'result': 'proven', 'detail': 'The guest confirmed the run ended',
                    'written_at': 123.5}
        real_fsync = os.fsync
        synced: list[tuple[int, bytes]] = []

        def sync(fd: int) -> None:
            with path.open('rb') as reader, self.assertRaises(BlockingIOError):
                fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)
            real_fsync(fd)
            synced.append((os.fstat(fd).st_ino, path.read_bytes()))

        def failed_send(request: wire.ExecutionReport, *, deadline: float, timeout: float) -> wire.DispatchStatus:
            self.assertEqual(request.assignment, assignment)
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(timeout, 1)
            before, line = path.read_bytes().splitlines()
            self.assertEqual(before, partial)
            self.assertEqual(json.loads(line), expected)
            self.assertEqual(synced[:2], [(entry.stat().st_ino, path.read_bytes()) for entry in (path, path.parent)])
            raise wire.AdmissionUnavailable('fixture socket outage')

        with (patch.object(mac_linux, 'time', return_value=123.5),
              patch.object(os, 'fsync', side_effect=sync),
              patch.object(mac_linux, 'send_guest_execution', side_effect=failed_send) as send,
              redirect_stderr(io.StringIO()) as errors):
            mac_linux.settle_guest_dispatch(dispatch, placement.AttemptCompleted(0))
        send.assert_called_once()
        self.assertIn('fixture socket outage', errors.getvalue())
        self.assertEqual(mac_linux.settlement(assignment.episode, 16), placement.Settled())

    def test_guest_end_report_accepts_pending_settlement_and_warns_on_refusal(self) -> None:
        dispatch = self.guest_dispatch()
        dispatch.started = mac_linux.ArmedGuestAttempt('stream-attempt', 'f' * 32, 'boot-id', 10, 'guest', '/fixture')
        permission = placement.PermissionConsumed(1.0)
        pending = wire.DispatchStatus(dispatch.assignment, placement.CurrentAssignment(), permission)
        saved = admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
            dispatch.assignment, pending.state, dispatch.claim, permission,
            placement.TerminalAttemptReport(placement.AttemptCompleted(0), placement.DispatchPathPending('pending'), 1.0)))

        for reason in ('', 'fixture refusal'):
            with (self.subTest(reason=reason), redirect_stderr(io.StringIO()) as errors,
                  patch.object(mac_linux, 'send_guest_execution', return_value=replace(pending, reason=reason)) as send,
                  patch.object(mac_linux, '_guest_queue_query', return_value=saved) as query):
                mac_linux.settle_guest_dispatch(dispatch, placement.AttemptCompleted(0))
                send.assert_called_once()
                request = cast(wire.ExecutionReport, send.call_args.args[0])
                self.assertEqual(request.assignment, dispatch.assignment)
                self.assertEqual(request.outcome, placement.AttemptCompleted(0))
                self.assertEqual(request.path, placement.DispatchPathSettled())
                expected = f'Mac Linux guest completion report failed: {reason}\n' if reason else ''
                self.assertEqual(errors.getvalue(), expected)
                if reason:
                    query.assert_not_called()
                else:
                    query.assert_called_once_with(admission_wire.PlacementStatusRequest(
                        dispatch.assignment.episode, dispatch.assignment.attempt, include_permission=True,
                        code_version=admission_client.LOADED_ADMISSION_CODE_VERSION), **send.call_args.kwargs)

    def test_guest_settlement_reads_only_a_complete_matching_proof(self) -> None:
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        episode = 'fixture-episode'
        attempt = 16

        def proof(owner: str, number: int, result: str) -> bytes:
            row = {'attempt': execution_identity.attempt_key(owner, f'{number:032x}'),
                   'claim': 'fixture-claim', 'result': result, 'detail': 'fixture detail', 'written_at': 123.0}
            return (json.dumps(row) + '\n').encode()

        self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)
        self.assertFalse(path.parent.exists())
        path.parent.mkdir(parents=True)
        history = (b'bad JSON\n\xff\n' + proof(episode, attempt, 'unproven')
                   + proof('another-episode', attempt, 'proven') + proof(episode, attempt + 1, 'proven'))
        _ = path.write_bytes(history)
        self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)
        self.assertEqual(path.read_bytes(), history)
        with path.open('ab') as writer:
            fcntl.flock(writer, fcntl.LOCK_EX)
            _ = writer.write(proof(episode, attempt, 'proven').rstrip(b'\n'))
            writer.flush()
            self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)
            _ = writer.write(b'\n')
            writer.flush()
            self.assertEqual(mac_linux.settlement(episode, attempt), placement.Settled())

    def test_guest_reports_start_and_completion_for_its_placement(self) -> None:
        self.placed = replace(self.placed, attempt=f'{16:032x}')
        reports: list[wire.ExecutionReport] = []
        permission = placement.PermissionConsumed(1.0)
        pending = wire.DispatchStatus(self.placed, placement.CurrentAssignment(), permission)

        def report(request: wire.ExecutionReport, *, deadline: float, timeout: float) -> wire.DispatchStatus:
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(timeout, 1)
            reports.append(request)
            if isinstance(request.path, placement.DispatchPathSettled):
                self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')
                self.assertEqual(claim_state.read_claims(claim_state.state_paths(mac_linux.GUEST_CLAIM_TARGET)), ())
                path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
                launch, line = path.read_bytes().splitlines()
                self.assertEqual(cast(dict[str, object], json.loads(launch))['result'], 'awaiting arm')
                proof = cast(dict[str, object], json.loads(line))
                self.assertEqual(proof['claim'], self.consumed[0][0].claim.claim_id)
                self.assertEqual(proof['result'], 'proven')
                self.assertIsInstance(mac_linux.settlement(self.placed.episode, 16), placement.Pending)
                return pending
            with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
                mac_linux.reconcile_unproven(self.placed.episode, 16)
            submit.assert_not_called()
            mac_linux.reconcile_guest_end(self.placed.episode, 16)
            self.assertIsInstance(mac_linux.settlement(self.placed.episode, 16), placement.Pending)
            return pending

        def status(_request: admission_wire.PlacementStatusRequest, *, deadline: float,
                   timeout: float) -> admission_wire.PlacementStatusReply:
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(timeout, 1)
            completion = reports[-1]
            assert isinstance(completion.outcome, placement.AttemptCompleted)
            saved = placement.TerminalAttemptReport(completion.outcome, placement.DispatchPathPending('pending'), 1.0)
            return admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
                self.placed, pending.state, completion.claim, permission, saved))

        with (patch.object(mac_linux, 'send_guest_execution', report),
              patch.object(mac_linux, '_guest_queue_query', side_effect=status) as query,
              redirect_stderr(io.StringIO()) as errors):
            result = self.route()
        query.assert_called_once()
        self.assertIsInstance(result, GateRun)
        self.assertEqual(errors.getvalue(), '')
        report_row, completion = reports
        self.assertEqual((report_row.assignment, report_row.claim), (self.placed, self.consumed[0][0].claim))
        self.assertEqual((completion.assignment, completion.claim, completion.evidence),
                         (report_row.assignment, report_row.claim, report_row.evidence))
        self.assertEqual(completion.outcome, placement.AttemptCompleted(0))
        self.assertEqual(completion.path, placement.DispatchPathSettled())
        assert isinstance(report_row.evidence, execution_identity.ObservedExecution)
        identity = report_row.evidence.identity
        assert isinstance(identity, execution_identity.GuestExecution)
        self.assertEqual((identity.target, identity.attempt), (mac_linux.TARGET, self.placed.attempt))
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')
        self.assertIsInstance(mac_linux.settlement(self.placed.episode, 16), placement.Pending)
        with patch.object(process_start, 'process_start', return_value=process_start.Gone()):
            self.assertEqual(mac_linux.settlement(self.placed.episode, 16), placement.Settled())

    def test_checkout_change_blocks_the_guest_route_after_a_completion_report_timeout(self) -> None:
        completed_run = mac_linux.completed_run
        completions: list[wire.ExecutionReport] = []

        def change_checkout(reply: mac_linux.GuestReply, request: mac_linux.GuestRequest,
                            source: mac_linux.CandidateSource) -> mac_linux.MacLinuxGateRun:
            run = completed_run(reply, request, source)
            self.assertTrue(run.passed)
            _ = (self.repo / 'history.txt').write_text('changed source\n')
            return run

        def report(request: wire.ExecutionReport, *, deadline: float, timeout: float) -> wire.DispatchStatus:
            self.assertGreater(deadline, time.monotonic())
            if isinstance(request.outcome, placement.AttemptCompleted):
                completions.append(request)
                raise wire.AdmissionUnavailable(f'no answer in {timeout:g} s')
            return wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))

        with (patch.object(mac_linux, 'completed_run', side_effect=change_checkout),
              patch.object(mac_linux, 'send_guest_execution', report),
              redirect_stderr(io.StringIO()) as errors):
            result = self.route()
        self.assertEqual(result, GuestGateBlocked('the checkout changed during the guest run'))
        completion, = completions
        self.assertEqual((completion.assignment, completion.claim), (self.placed, self.consumed[0][0].claim))
        self.assertEqual(completion.outcome, placement.AttemptCompleted(1))
        self.assertEqual(completion.path, placement.DispatchPathSettled())
        self.assertEqual(errors.getvalue(), 'Mac Linux guest completion report failed: no answer in 1 s\n')

    def test_valid_guest_reply_uses_completed_run_once(self) -> None:
        with patch.object(mac_linux, 'completed_run', wraps=mac_linux.completed_run) as validate:
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        validate.assert_called_once()

    def test_complete_guest_verdict_survives_a_bad_byte_count(self) -> None:
        real_stream = mac_linux.stream_guest_bundle

        def broken_count(repo: Path, command: list[str], bootstrap: bytes, timeout: int, link: int,
                         armed: mac_linux.ArmedGuestAttempt, dispatch: mac_linux.GuestGateDispatch, *,
                         connect_deadline: float) -> subprocess.CompletedProcess[bytes]:
            stream = real_stream(repo, command, bootstrap, timeout, link, armed, dispatch,
                                 connect_deadline=connect_deadline)
            verdict, separator, tail = stream.stdout.partition(b'\n')
            self.assertEqual(separator, b'\n')
            self.assertTrue(tail.strip().isdigit())
            return subprocess.CompletedProcess(command, 255, verdict + b'\n\n' + count,
                stream.stderr + b'client_loop: send disconnect: Broken pipe\n')

        for attempt, count in enumerate((b'80', b'bad count\n', b'', b'1\n'), start=1):
            self.placed = replace(self.placed, attempt=f'{attempt:032x}')
            with (self.subTest(count=count),
                  patch.object(mac_linux, 'stream_guest_bundle', side_effect=broken_count)):
                result = self.route('red')
            assert isinstance(result, mac_linux.MacLinuxGateRun), result
            self.assertEqual([job.returncode for job in result.jobs], [1, 1])
            self.assertIsInstance(result.bundle_bytes, mac_linux.BundleSizeUnavailable)

    def test_guest_that_ran_without_a_received_start_line_offers_a_retry(self) -> None:
        attempt = 16
        self.placed = replace(self.placed, attempt=f'{attempt:032x}')
        pending = wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))
        with patch.object(mac_linux, 'send_guest_execution', return_value=pending) as report:
            reason = self.unconfirmed(self.route('lost-run-reply'))
        self.assertTrue((self.root / 'run.group').is_file())
        self.assertIn('Guest cleanup: ended', reason)
        self.assertIn('did not confirm whether the run started', reason)
        report.assert_not_called()
        self.assertIsInstance(mac_linux.settlement(self.placed.episode, attempt), placement.Pending)
        with patch.object(process_start, 'process_start', return_value=process_start.Gone()):
            self.assertEqual(mac_linux.settlement(self.placed.episode, attempt), placement.Settled())

    def test_checkpoint_does_not_retry_an_unknown_guest_result(self) -> None:
        fixture = GatePlacementFixture(self)
        self.placed = fixture.guest
        args = ('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo))

        def child(command: list[str], _folder: Path) -> subprocess.CompletedProcess[str]:
            with redirect_stderr(io.StringIO()) as errors:
                status = gate.main(args)
            return subprocess.CompletedProcess(command, status, '', errors.getvalue())

        pending = wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))
        run = Mock(side_effect=child)
        with (patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'lost-confirmed-reply',
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main'),
                                     'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file)}),
              patch.object(gate, 'current_gate_placement', return_value=wire.Assignment(self.placed)),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(scratch, 'copy_lock', return_value=nullcontext()),
              patch.object(mac_linux, 'send_guest_execution', return_value=pending),
              patch.object(admission_client, 'request_settlement') as settle,
              self.assertRaises(checkpoint_gate.Stop) as stopped):
            _ = fixture.run_step(run, retry_failed_steps=True)
        self.assertEqual(stopped.exception.state, 'blocked')
        self.assertIn('launch settlement', stopped.exception.detail)
        output = fixture.log.read_text()
        self.assertIn('Guest cleanup: ended', output)
        self.assertIn('merge_test_1_EXIT=80', output)
        self.assertNotIn('RERUN', output)
        self.assertTrue((self.root / 'run.group').is_file())
        settle.assert_called_once_with(wire.SettlementRequest(self.placed))
        fixture.place_again.assert_not_called()
        run.assert_called_once()

    def test_unconfirmed_guest_failure_exits_79_with_exact_claim_failure(self) -> None:
        fixture = GatePlacementFixture(self)
        self.placed = fixture.guest
        args = ('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo))
        statuses: list[int] = []

        def run() -> subprocess.CompletedProcess[str]:
            if admission_wire.read_assignment(fixture.assignment_file).assignment == fixture.guest:
                with redirect_stderr(io.StringIO()) as errors:
                    statuses.append(gate.main(args))
                return subprocess.CompletedProcess(args, statuses[-1], '', errors.getvalue())
            return subprocess.CompletedProcess(args, 0, '', '')

        def replace_assignment(request: wire.PlacementRequest) -> None:
            self.assertEqual(request, fixture.request)
            admission_wire.write_assignment(fixture.assignment_file, fixture.local)

        fixture.report_failure.return_value = wire.DispatchStatus(self.placed, placement.CurrentAssignment(),
            placement.PermissionConsumed(1.0), retry=placement.RetryAt(1.0))
        with (patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'reset,reset,reset',
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main'),
                                     'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file)}),
              patch.object(gate, 'current_gate_placement', return_value=wire.Assignment(self.placed)),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(mac_linux, 'sleep'),
              patch.object(admission_client, 'request_settlement') as settle,
              redirect_stdout(io.StringIO()) as output):
            result = first_step.run_gate_with_placement(run, fixture.assignment_file, fixture.request, replace_assignment)
        self.assertEqual(statuses, [79])
        self.assertEqual(result.returncode, 0)
        report = wire.parse_request(fixture.report_file.read_bytes())
        assert isinstance(report, wire.ClaimFailure), report
        self.assertEqual((report.assignment, report.reason), (self.placed, 'mac_linux_start_unconfirmed'))
        fixture.report_failure.assert_called_once_with(report)
        settle.assert_not_called()
        self.assertIn('did not confirm a start', output.getvalue())
        self.assertNotIn('waiting for Mac Linux', output.getvalue())

    def test_zero_exit_inner_reset_and_cleanup_banner_break_end_with_natedev_result(self) -> None:
        fixture = GatePlacementFixture(self)
        self.placed = fixture.guest
        args = ('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo))
        attempts: list[tuple[str, int, str]] = []
        replies: list[subprocess.CompletedProcess[bytes]] = []
        stream = mac_linux.stream_guest_bundle

        def record_stream(repo: Path, command: list[str], bootstrap: bytes, timeout: int, link: int,
                          armed: mac_linux.ArmedGuestAttempt, dispatch: mac_linux.GuestGateDispatch, *,
                          connect_deadline: float) -> subprocess.CompletedProcess[bytes]:
            reply = stream(repo, command, bootstrap, timeout, link, armed, dispatch, connect_deadline=connect_deadline)
            replies.append(reply)
            return reply

        def placement_file(*_args: object, **_options: object) -> wire.Assignment | wire.LocalPlacementGranted:
            return admission_wire.read_assignment(fixture.assignment_file)

        def run() -> subprocess.CompletedProcess[str]:
            target = placement_file().assignment.destination.target
            with redirect_stderr(io.StringIO()) as errors:
                status = gate.main(args)
            attempts.append((target, status, errors.getvalue()))
            return subprocess.CompletedProcess(args, status, '', errors.getvalue())

        def replace_assignment(_request: wire.PlacementRequest) -> None:
            admission_wire.write_assignment(fixture.assignment_file, fixture.local)

        place = Mock(side_effect=replace_assignment)
        fixture.report_failure.return_value = wire.DispatchStatus(self.placed, placement.CurrentAssignment(),
            placement.PermissionConsumed(1.0), retry=placement.RetryAt(1.0))
        local_run = GateRun((gate.GateJobResult('fixture', 'test_first.py', 0, 1, ''),), 1, 0)
        sidecar = fixture.assignment_file.with_name('progress')
        with (patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'reset,reset,reset',
                                     'FIXTURE_MASK_STREAM_STATUS': '1',
                                     'FIXTURE_SSH_CLEANUP': 'broken-after-banner',
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main'),
                                     'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file),
                                     'BUILDLOG_PLACEMENT_SIDECAR': str(sidecar)}),
              patch.object(gate, 'current_gate_placement', side_effect=placement_file),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=record_stream),
              patch.object(mac_linux, 'sleep'),
              patch.object(gate, 'run_suites', return_value=local_run) as local,
              patch.object(gate, 'release_local_gate') as release):
            result = first_step.run_gate_with_placement(run, fixture.assignment_file, fixture.request, place)
        self.assertEqual([(target, status) for target, status, _ in attempts], [('mac-linux', 79), ('natedev', 0)])
        self.assertEqual(result.returncode, 0)
        self.assertEqual([(reply.returncode, reply.stdout) for reply in replies], [(0, b'\n\n')] * 3)
        for reply in replies:
            self.assertIn(b'Connection reset by peer', reply.stderr)
        self.assertIn('Guest cleanup: ended after banner exchange:', attempts[0][2])
        self.assertIn('Broken pipe', attempts[0][2])
        self.assert_guest_clean('probe', 'arm', 'run', 'run', 'run', 'cleanup', 'cleanup')
        place.assert_called_once_with(fixture.request)
        local.assert_called_once()
        release.assert_called_once_with(gate.LocalGateAssignment(fixture.local.grant, fixture.local.assignment.owner, sidecar), 0)

    def lose_guest_permission_reply(self, attempt: int) -> wire.ConsumePermission:
        self.placed = replace(self.placed, attempt=f'{attempt:032x}')
        with patch.object(admission_client, 'consume_permission',
                          side_effect=wire.AdmissionUnavailable('fixture lost authorization reply')) as consume:
            self.assertEqual(self.route(),
                mac_linux.GuestNotStarted('the launch permission is unavailable: fixture lost authorization reply',
                                          settlement_required=True))
        consume.assert_called_once()
        request = cast(wire.ConsumePermission, consume.call_args.args[0])
        self.assertEqual(request.assignment, self.placed)
        self.assertEqual(self.trace.read_text(), '')
        return request

    def test_lost_guest_permission_reply_with_exact_consumption_exits_79(self) -> None:
        fixture = GatePlacementFixture(self)
        guest = self.placed = replace(fixture.guest, attempt=f'{23:032x}')
        admission_wire.write_assignment(fixture.assignment_file, wire.Assignment(guest))
        consume = admission_client.consume_permission

        def lose_reply(request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
            saved = consume(request)
            self.assertIsInstance(saved, wire.FirstAuthorization)
            raise wire.AdmissionUnavailable('fixture lost authorization reply')

        def status(query: admission_wire.PlacementStatusRequest, *, deadline: float,
                   timeout: float) -> admission_wire.PlacementStatusReply:
            self.assertEqual((query.episode, query.attempt, query.include_permission), (guest.episode, guest.attempt, True))
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(timeout, 1)
            (request, _trace, _held), = self.consumed
            return admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
                guest, placement.CurrentAssignment(), request.claim, placement.PermissionConsumed(1.0)))

        with (patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file)}),
              patch.object(gate, 'current_gate_placement', return_value=wire.Assignment(guest)),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(admission_client, 'consume_permission', side_effect=lose_reply) as lost,
              patch.object(mac_linux, '_guest_queue_query', side_effect=status) as query,
              patch.object(mac_linux, 'send_guest_execution') as execution,
              redirect_stderr(io.StringIO()) as errors):
            result = gate.main(('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo)))
        self.assertEqual(result, 79, errors.getvalue())
        self.assertIn('fixture lost authorization reply', errors.getvalue())
        report = wire.parse_request(fixture.report_file.read_bytes())
        assert isinstance(report, wire.ClaimFailure), report
        self.assertEqual((report.assignment, report.reason), (guest, 'mac_linux_start_unconfirmed'))
        self.assertEqual(mac_linux.settlement(guest.episode, 23), placement.Settled())
        lost.assert_called_once()
        query.assert_called_once()
        execution.assert_not_called()
        self.assert_guest_clean()

    def test_lost_permission_without_consumption_proof_requests_settlement_without_retry(self) -> None:
        fixture = GatePlacementFixture(self)
        self.placed = fixture.guest
        args = ('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo))

        def run() -> subprocess.CompletedProcess[str]:
            with redirect_stderr(io.StringIO()) as errors:
                status = gate.main(args)
            return subprocess.CompletedProcess(args, status, '', errors.getvalue())

        with (patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file)}),
              patch.object(gate, 'current_gate_placement', return_value=wire.Assignment(fixture.guest)),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(admission_client, 'consume_permission', side_effect=wire.AdmissionUnavailable('queue down')),
              patch.object(mac_linux, '_guest_queue_query', side_effect=wire.AdmissionUnavailable('queue down')),
              patch.object(admission_client, 'request_settlement') as settle):
            result = first_step.run_gate_with_placement(run, fixture.assignment_file, fixture.request, fixture.place_again)
        self.assertEqual(result.returncode, 80, result.stderr)
        self.assertIn('The queue did not confirm consumption for this launch claim', result.stderr)
        self.assertFalse(fixture.report_file.exists())
        settle.assert_called_once_with(wire.SettlementRequest(fixture.guest))
        fixture.report_failure.assert_not_called()
        fixture.place_again.assert_not_called()
        self.assert_guest_clean()

    def test_lost_guest_permission_reply_settles_after_the_guest_fences_the_attempt(self) -> None:
        request = self.lose_guest_permission_reply(23)
        episode = request.assignment.episode
        self.assertFalse(self.run_lock().exists())
        self.assertIsInstance(mac_linux.settlement(episode, 23), placement.Pending)

        with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
            mac_linux.reconcile_unproven(episode, 23)
        submit.assert_called_once()
        command = cast(list[str], submit.call_args.args[0])
        self.assertEqual(command[-3:], ['--reconcile-end', episode, '23'])

        with redirect_stderr(io.StringIO()) as errors:
            with patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'offline'}):
                mac_linux.reconcile_guest_end(episode, 23)
            self.assertIn('fixture SSH refused the connection', errors.getvalue())
        self.assertIsInstance(mac_linux.settlement(episode, 23), placement.Pending)

        mac_linux.reconcile_guest_end(episode, 23)
        self.assertIsInstance(mac_linux.settlement(episode, 23), placement.Pending)
        self.assertEqual(self.trace.read_text().splitlines(), ['fence'])

        delay = conf.read('mac_test').number('unreachable_backoff_s')
        with patch.object(mac_linux, 'time', return_value=time.time() + delay + 1):
            mac_linux.reconcile_guest_end(episode, 23)
        self.assertEqual(mac_linux.settlement(episode, 23), placement.Settled())
        self.assert_guest_clean('fence', 'fence')
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        _, _, proof = path.read_bytes().splitlines()
        record = cast(dict[str, object], json.loads(proof))
        self.assertEqual(record['attempt'], execution_identity.attempt_key(episode, self.placed.attempt))
        self.assertEqual(record['claim'], request.claim.claim_id)
        self.assertEqual(record['result'], 'not started')

    def test_lost_launch_replies_request_settlement_after_releasing_the_holder(self) -> None:
        for attempt, scenario in ((48, 'missing-arm-ack'), (49, 'lost-consume-reply'), (51, 'already-consumed')):
            with self.subTest(scenario=scenario):
                self.placed = replace(self.placed, attempt=f'{attempt:032x}')
                status_reply = nullcontext()
                if scenario == 'already-consumed':
                    permission = placement.PermissionConsumed(1.0)
                    reply = wire.DispatchStatus(self.placed, placement.CurrentAssignment(),
                        permission, 'launch permission was already consumed')
                    consume = patch.object(admission_client, 'consume_permission', return_value=reply)
                    destination = self.placed.destination
                    other_claim = placement.ProviderClaimReference(destination.provider, destination.target,
                        destination.scope, 'other-claim', self.placed.episode, self.placed.attempt)
                    saved = admission_wire.KnownPlacementStatus(self.placed, placement.CurrentAssignment(),
                        other_claim, permission)
                    status_reply = patch.object(mac_linux, '_guest_queue_query',
                        return_value=admission_wire.PlacementStatusReply(saved))
                elif scenario == 'lost-consume-reply':
                    consume = patch.object(admission_client, 'consume_permission',
                                           side_effect=wire.AdmissionUnavailable('fixture lost consume reply'))
                else:
                    consume = nullcontext()
                with consume, status_reply, patch.object(mac_linux, 'send_guest_execution') as report:
                    outcome = self.route(scenario)
                if scenario == 'missing-arm-ack':
                    _ = self.unconfirmed(outcome)
                else:
                    reason = ('the queue refused the launch permission: launch permission was already consumed'
                              if scenario == 'already-consumed'
                              else 'the launch permission is unavailable: fixture lost consume reply')
                    self.assertEqual(outcome, mac_linux.GuestNotStarted(reason, settlement_required=True))
                report.assert_not_called()
                with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
                    mac_linux.reconcile_unproven(self.placed.episode, attempt)
                submit.assert_called_once()
                mac_linux.reconcile_guest_end(self.placed.episode, attempt)
                self.assertEqual(mac_linux.settlement(self.placed.episode, attempt), placement.Settled())
        self.assert_guest_clean('probe', 'arm', 'fence', 'fence', 'fence')

    def test_guest_fence_refuses_a_gate_that_arms_after_reconciliation(self) -> None:
        request = self.lose_guest_permission_reply(29)
        episode = request.assignment.episode
        attempts: list[str] = []
        arm_guest = mac_linux.arm_guest

        def record_arm(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
            attempts.append(attempt)
            return arm_guest(settings, attempt)

        self.assertIsInstance(mac_linux.settlement(episode, 29), placement.Pending)
        with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
            mac_linux.reconcile_unproven(episode, 29)
        submit.assert_called_once()
        mac_linux.reconcile_guest_end(episode, 29)
        self.assertEqual(mac_linux.settlement(episode, 29), placement.Settled())

        with patch.object(mac_linux, 'arm_guest', record_arm):
            outcome = self.route()
        self.assertIn('The guest attempt was refused.', self.not_started(outcome))
        self.assert_guest_clean('fence', 'probe', 'arm')
        self.assertEqual(list(self.run_lock().parent.glob(f'{mac_linux.ARM_RECORD_PREFIX}.*')), [])

        settings = mac_linux.read_settings(conf.read(self.registry))
        other = arm_guest(settings, 'other-fixture')
        self.assertIsInstance(mac_linux.prove_guest_end(settings, other), mac_linux.GuestEndProven)
        attempt, = attempts
        with self.assertRaisesRegex(mac_linux.GuestUnavailable, 'The guest attempt was refused.'):
            _ = arm_guest(settings, attempt + mac_linux.FULL_BUNDLE_RETRY_SUFFIX)

    def test_guest_reconciliation_waits_for_the_run_lock_then_ends_the_armed_attempt(self) -> None:
        self.placed = replace(self.placed, attempt=f'{31:032x}')
        episode = self.placed.episode
        attempts: list[mac_linux.ArmedGuestAttempt] = []
        arm_guest = mac_linux.arm_guest

        def stop_after_arm(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
            attempts.append(arm_guest(settings, attempt))
            raise mac_linux.GuestTookNoRun('fixture stop after arm')

        with patch.object(mac_linux, 'arm_guest', stop_after_arm):
            self.assertIn('fixture stop after arm', self.not_started(self.route()))

        settings = mac_linux.read_settings(conf.read(self.registry))
        armed, = attempts
        other = mac_linux.arm_guest(settings, 'other-fixture')
        self.assertIsInstance(mac_linux.prove_guest_end(settings, other), mac_linux.GuestEndProven)

        with self.run_lock().open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _ = lock.write(f'{os.getpid()} {armed.attempt}\n')
            lock.flush()
            with patch.object(mac_linux, '_warn_guest_reconcile') as warn:
                mac_linux.reconcile_guest_end(episode, 31)
            warn.assert_called_once()
            self.assertEqual(warn.call_args.args[1], 'The guest run lock is held.')
            self.assertIsInstance(mac_linux.settlement(episode, 31), placement.Pending)
        mac_linux.reconcile_guest_end(episode, 31)
        self.assertEqual(mac_linux.settlement(episode, 31), placement.Settled())
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        proof = cast(dict[str, object], json.loads(path.read_bytes().splitlines()[-1]))
        self.assertEqual(proof['result'], 'proven')
        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        shell = mac_linux.run_shell(settings, source, 0, armed)
        result = subprocess.run(['bash', '-c', shell], env={**os.environ, 'HOME': str(self.guest_home)},
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=6)
        self.assertEqual(result.returncode, mac_linux.GUEST_NOT_LOCKED_STATUS)
        self.assertIn('The guest attempt was refused.', result.stderr)
        self.assertFalse((self.guest_home / 'build').exists())

    def test_guest_reconciliation_leaves_a_changed_guest_address_pending(self) -> None:
        request = self.lose_guest_permission_reply(34)
        episode = request.assignment.episode
        settings = mac_linux.read_settings(conf.read(self.registry))
        changed = replace(settings, ssh_hops=(settings.ssh_hops[0], 'other-guest'))
        with (patch.object(mac_linux, 'read_settings', return_value=changed),
              redirect_stderr(io.StringIO()) as errors):
            mac_linux.reconcile_guest_end(episode, 34)
        self.assertIn('The Mac Linux guest address changed for this attempt.', errors.getvalue())
        self.assertIsInstance(mac_linux.settlement(episode, 34), placement.Pending)
        self.assert_guest_clean()

    def test_guest_reconciliation_refuses_a_new_guest_behind_the_same_alias(self) -> None:
        request = self.lose_guest_permission_reply(35)
        episode = request.assignment.episode
        actual = Path('/etc/machine-id').read_text().strip()
        other_machine = 'f' * 32 if actual != 'f' * 32 else 'e' * 32
        with (patch.dict(os.environ, {'FIXTURE_GUEST_MACHINE': other_machine}),
              redirect_stderr(io.StringIO()) as errors):
            mac_linux.reconcile_guest_end(episode, 35)
        self.assertIn('A different guest answered the SSH request.', errors.getvalue())
        self.assertIsInstance(mac_linux.settlement(episode, 35), placement.Pending)
        self.assertFalse(self.run_lock().exists())
        mac_linux.reconcile_guest_end(episode, 35)
        self.assertEqual(mac_linux.settlement(episode, 35), placement.Settled())

    def test_guest_arm_refuses_a_new_checkout_behind_the_same_alias(self) -> None:
        self.placed = replace(self.placed, attempt=f'{36:032x}')
        other_home = self.root / 'other-guest'
        other_home.mkdir()

        def change_checkout(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            os.environ['FIXTURE_GUEST_HOME'] = str(other_home)
            return wire.FirstAuthorization(request.assignment, request.claim)

        with patch.dict(os.environ), patch.object(admission_client, 'consume_permission', change_checkout):
            outcome = self.route()
        self.assertIn('The guest checkout differs from the saved checkout.', self.not_started(outcome))
        self.assertEqual(list(other_home.iterdir()), [])
        self.assertIsInstance(mac_linux.settlement(self.placed.episode, 36), placement.Pending)
        mac_linux.reconcile_guest_end(self.placed.episode, 36)
        self.assertEqual(mac_linux.settlement(self.placed.episode, 36), placement.Settled())

    def test_failed_main_probe_after_consumption_saves_proof_without_arming(self) -> None:
        self.placed = replace(self.placed, attempt=f'{38:032x}')
        self.assertIn('the guest main probe failed', self.unconfirmed(self.route('main-probe-failure')))
        self.assertEqual(len(self.consumed), 1)
        self.assertEqual(mac_linux.settlement(self.placed.episode, 38), placement.Settled())
        self.assert_guest_clean('probe')
        self.assertFalse(self.run_lock().exists())

    def test_failed_launch_release_exits_with_a_settlement_error(self) -> None:
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        unreadable = path.with_name('journal-directory')
        unreadable.mkdir(parents=True)
        armed = mac_linux.ArmedGuestAttempt('fixture-attempt', 'f' * 32, 'boot-id', 10, 'guest', '/fixture')
        settings = mac_linux.read_settings(conf.read(self.registry))
        for attempt, reason in ((50, 'the guest main probe failed'), (52, 'the guest end is unproven')):
            with (self.subTest(reason=reason),
                  patch.object(mac_linux, 'new_run_settings', return_value=settings),
                  patch.object(process_start, 'process_start', return_value=process_start.Started('123'))):
                self.placed = replace(self.placed, attempt=f'{attempt:032x}')
                transfer = (patch.object(mac_linux, 'transfer_source', side_effect=mac_linux.GuestEndUnproven(reason, armed))
                            if attempt == 52 else nullcontext())
                with (transfer, patch.object(target_config, 'mac_linux_end_proof_journal',
                                             side_effect=[path, unreadable, unreadable]),
                      patch.object(mac_linux, 'send_guest_execution') as report, redirect_stderr(io.StringIO())):
                    if attempt == 52:
                        with self.assertRaisesRegex(mac_linux.GuestEndSaveFailed, reason) as failure:
                            _ = self.route('main-probe-failure')
                        self.assertIn('Cannot save the Mac Linux guest end', ' '.join(failure.exception.__notes__))
                        held, = claim_state.read_claims(claim_state.state_paths('mac'))
                        self.assertEqual(held.record['claim_id'], self.consumed[-1][0].claim.claim_id)
                    else:
                        blocked = self.blocked(self.route('main-probe-failure'))
                        self.assertIn('Cannot release the Mac Linux launch holder', blocked)
                        self.assertIn(reason, blocked)
                report.assert_not_called()
                self.assertIsInstance(mac_linux.settlement(self.placed.episode, attempt), placement.Pending)
                with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
                    mac_linux.reconcile_unproven(self.placed.episode, attempt)
                    submit.assert_not_called()
                    with patch.object(process_start, 'process_start', return_value=process_start.Gone()):
                        mac_linux.reconcile_unproven(self.placed.episode, attempt)
                    submit.assert_called_once()

    def test_direct_affected_retries_after_a_consumed_guest_takes_no_run(self) -> None:
        self.assertEqual(gate.gate_state_dir(), self.root / 'gate-state')
        key = treekey.tree_key(str(self.repo))
        assert key is not None
        started = process_start.process_start(os.getpid())
        assert isinstance(started, process_start.Started)
        owner = replace(self.placed.owner, host=store.host_name(), pid=os.getpid(), start_time=started.start)
        guest = replace(self.placed, owner=owner, attempt=f'{46:032x}', source_key=execution_identity.SourceKeyKnown(key))
        local = replace(guest, attempt=f'{47:032x}', destination=replace(
            guest.destination, provider='local', target='natedev', host='natedev'))
        args = ('--affected', '--full', '--repo', str(self.repo))
        command = ('gate.py', *args)
        constraints = placement.PlacementConstraints(guest.source_key, command, str(self.repo), '', guest.session,
            (placement.PlacementCandidate(guest.destination), placement.PlacementCandidate(local.destination)))
        profile = admission_records.ProgramProfile('gate', str(self.repo), str(self.repo),
            admission_records.HistoryPeakSizing(1, admission_records.FirstProgramPeak(1), 0))
        submitted = wire.PlacementRequest(guest.episode, guest.owner, guest.since, profile, constraints)
        path = self.root / 'assignment.json'
        grant = wire.Assignment(guest)
        admission_client.write_assignment(path, grant)
        admitted = first_step.FirstStepAdmission(CopyQueuePosition(guest.episode, guest.since),
            first_step.OwnedGatePlacement(grant, submitted), path)
        pending = wire.DispatchStatus(guest, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))

        def report(request: wire.ClaimFailure) -> wire.DispatchStatus:
            self.assertEqual(request.assignment, guest)
            self.assertEqual(request.reason, 'mac_linux_start_unconfirmed')
            self.assertIsInstance(request.claim, placement.NoProviderClaim)
            self.assertEqual(mac_linux.settlement(guest.episode, 46), placement.Settled())
            return replace(pending, retry=placement.RetryAt(1.0))

        def status(_episode: str, attempt: str) -> admission_wire.PlacementStatusReply:
            assignment = guest if attempt == guest.attempt else local
            return admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
                assignment, placement.CurrentAssignment(), placement.NoProviderClaim()))

        def run_gate() -> subprocess.CompletedProcess[str]:
            environment = {'BUILDLOG_PREPARED_COPY': str(self.repo)}
            errors = io.StringIO()
            reader, writer = os.pipe()
            with os.fdopen(reader, 'rb') as pipe:
                with os.fdopen(writer, 'wb') as token:
                    _ = token.write(f'{os.getpid()}\n'.encode('ascii'))
                # gate.main closes the descriptor that it reads.
                environment[copies.PREPARED_GATE_FD] = str(os.dup(pipe.fileno()))
                with patch.dict(os.environ, environment), redirect_stderr(errors):
                    code = gate.main(args)
            return subprocess.CompletedProcess(command, code, "", errors.getvalue())

        with (patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'main-probe-failure'}),
              patch.object(admission_client, 'placement_status', side_effect=status),
              patch.object(gate, 'test_run_folder', return_value=nullcontext()),
              patch.object(mac_linux, 'send_guest_execution') as execution,
              patch.object(admission_client, 'claim_failure', side_effect=report) as decline,
              patch.object(admission_client, 'request_settlement') as settle,
              patch.object(queue_place, 'wait_for_placement', return_value=wire.Assignment(local)) as again,
              patch.object(gate, 'run_suites', return_value=GateRun((), 0, 0, selection_certified=True)) as run):
            admitted.handoff(self.repo)
            result = admitted.run_gate(run_gate)
            self.assertEqual(result.returncode, 0, result.stderr)
        again.assert_called_once()
        self.assertEqual(again.call_args.args[0], submitted)
        run.assert_called_once()
        decline.assert_called_once()
        execution.assert_not_called()
        settle.assert_not_called()
        self.assert_guest_clean('probe')

    def test_bundle_preparation_failure_after_consumption_saves_proof_without_arming(self) -> None:
        self.placed = replace(self.placed, attempt=f'{39:032x}')
        with patch.object(mac_linux, 'runner_bootstrap', side_effect=OSError('fixture bootstrap failed')):
            self.assertIn('fixture bootstrap failed', self.unconfirmed(self.route()))
        self.assertEqual(len(self.consumed), 1)
        self.assertEqual(mac_linux.settlement(self.placed.episode, 39), placement.Settled())
        self.assert_guest_clean('probe')
        self.assertFalse(self.run_lock().exists())

    def test_arm_ssh_start_failure_saves_not_started_proof(self) -> None:
        self.placed = replace(self.placed, attempt=f'{42:032x}')
        start = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

        def start_other_commands(argv: Sequence[str], **kwargs: object) -> subprocess.Popen[bytes]:
            if argv[0] == 'ssh' and ' arm mac-linux-armed mac-linux-cancel ' in argv[-1]:
                raise FileNotFoundError('fixture arm SSH could not start')
            return start(argv, **kwargs)

        with patch.object(subprocess, 'Popen', start_other_commands):
            self.assertIn('fixture arm SSH could not start', self.unconfirmed(self.route()))
        self.assertEqual(len(self.consumed), 1)
        self.assertEqual(mac_linux.settlement(self.placed.episode, 42), placement.Settled())
        self.assert_guest_clean('probe')

    def test_arm_ssh_popen_eagain_reports_unconfirmed_start(self) -> None:
        fixture = GatePlacementFixture(self)
        guest = self.placed = replace(fixture.guest, attempt=f'{42:032x}')
        admission_wire.write_assignment(fixture.assignment_file, wire.Assignment(guest))
        start = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

        def start_other_commands(argv: Sequence[str], **kwargs: object) -> subprocess.Popen[bytes]:
            if argv[0] == 'ssh' and ' arm mac-linux-armed mac-linux-cancel ' in argv[-1]:
                raise BlockingIOError(errno.EAGAIN, 'fixture arm SSH could not start')
            return start(argv, **kwargs)

        with (patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file)}),
              patch.object(gate, 'current_gate_placement', return_value=wire.Assignment(guest)),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(subprocess, 'Popen', start_other_commands),
              patch.object(mac_linux, 'send_guest_execution') as execution,
              redirect_stderr(io.StringIO()) as errors):
            status = gate.main(('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo)))
        self.assertEqual(status, 79, errors.getvalue())
        self.assertIn('fixture arm SSH could not start', errors.getvalue())
        report = wire.parse_request(fixture.report_file.read_bytes())
        assert isinstance(report, wire.ClaimFailure), report
        self.assertEqual((report.assignment, report.reason), (guest, 'mac_linux_start_unconfirmed'))
        execution.assert_not_called()
        self.assertEqual(len(self.consumed), 1)
        self.assertEqual(mac_linux.settlement(guest.episode, 42), placement.Settled())
        self.assert_guest_clean('probe')

    def test_guest_refusal_write_failure_keeps_exit_80_with_a_legacy_report(self) -> None:
        fixture = GatePlacementFixture(self)
        self.placed = fixture.guest
        _ = fixture.report_file.write_text(admission_wire.encode_request(fixture.refusal))
        args = ('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo))

        def run() -> subprocess.CompletedProcess[str]:
            with redirect_stderr(io.StringIO()) as errors:
                status = gate.main(args)
            return subprocess.CompletedProcess(args, status, '', errors.getvalue())

        with (patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'main-probe-failure',
                                     'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file)}),
              patch.object(gate, 'current_gate_placement', return_value=wire.Assignment(fixture.guest)),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(gate, 'write_gate_claim_failure', side_effect=OSError('fixture sidecar write failed')),
              patch.object(admission_client, 'request_settlement') as settle):
            result = first_step.run_gate_with_placement(run, fixture.assignment_file, fixture.request, fixture.place_again)
        self.assertEqual(result.returncode, 80, result.stderr)
        self.assertIn('fixture sidecar write failed', result.stderr)
        self.assertEqual(wire.parse_request(fixture.report_file.read_bytes()), fixture.refusal)
        self.assertEqual(len(self.consumed), 1)
        settle.assert_called_once_with(wire.SettlementRequest(fixture.guest))
        fixture.report_failure.assert_not_called()
        fixture.place_again.assert_not_called()
        self.assert_guest_clean('probe')

    def test_arm_connect_failures_save_not_started_proof(self) -> None:
        self.placed = replace(self.placed, attempt=f'{42:032x}')
        with (patch.dict(os.environ, {'FIXTURE_SSH_ARM': 'reset,refused,reset'}),
              patch.object(mac_linux, 'sleep')):
            outcome = self.route()
        self.assertIn('Connection reset by peer after 3 connect attempts', self.unconfirmed(outcome))
        self.assertEqual(len(self.consumed), 1)
        self.assertEqual(mac_linux.settlement(self.placed.episode, 42), placement.Settled())
        self.assert_guest_clean('probe', 'arm', 'arm', 'arm')
        self.assertFalse(self.run_lock().exists())

    def test_arm_connect_retries_exhausted_replaces_guest_at_original_rank(self) -> None:
        fixture = GatePlacementFixture(self)
        guest = self.placed = replace(fixture.guest, attempt=f'{42:032x}')
        admission_wire.write_assignment(fixture.assignment_file, wire.Assignment(guest))
        args = ('--tests-only', '--full', '--module-time-limit-s', '3', '--repo', str(self.repo))
        attempts: list[tuple[str, int]] = []

        def run() -> subprocess.CompletedProcess[str]:
            target = admission_wire.read_assignment(fixture.assignment_file).assignment.destination.target
            with redirect_stderr(io.StringIO()) as errors:
                status = gate.main(args)
            attempts.append((target, status))
            return subprocess.CompletedProcess(args, status, '', errors.getvalue())

        def replace_assignment(request: wire.PlacementRequest) -> None:
            self.assertEqual(request, fixture.request)
            with patch.object(process_start, 'process_start', return_value=process_start.Gone()):
                self.assertEqual(mac_linux.settlement(guest.episode, 42), placement.Settled())
            admission_wire.write_assignment(fixture.assignment_file, fixture.local)

        fixture.report_failure.return_value = wire.DispatchStatus(guest, placement.CurrentAssignment(),
            placement.PermissionConsumed(1.0), retry=placement.RetryAt(1.0))
        local_run = GateRun((gate.GateJobResult('fixture', 'test_first.py', 0, 1, ''),), 1, 0)
        sidecar = fixture.assignment_file.with_name('progress')
        with (patch.dict(os.environ, {'FIXTURE_SSH_ARM': 'reset,refused,reset',
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main'),
                                     'BUILDLOG_PLACEMENT_FILE': str(fixture.assignment_file),
                                     'BUILDLOG_PLACEMENT_SIDECAR': str(sidecar)}),
              patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              patch.object(gate, 'current_gate_placement', side_effect=[wire.Assignment(guest), fixture.local]),
              patch.object(mac_linux, 'sleep'),
              patch.object(mac_linux, 'send_guest_execution') as execution,
              patch.object(admission_client, 'request_settlement') as settle,
              patch.object(gate, 'run_suites', return_value=local_run) as local,
              patch.object(gate, 'release_local_gate') as release):
            result = first_step.run_gate_with_placement(run, fixture.assignment_file, fixture.request, replace_assignment)
        self.assertEqual(attempts, [('mac-linux', 79), ('natedev', 0)], result.stderr)
        self.assertEqual(result.returncode, 0)
        report = wire.parse_request(fixture.report_file.read_bytes())
        assert isinstance(report, wire.ClaimFailure), report
        self.assertEqual((report.assignment, report.reason), (guest, 'mac_linux_start_unconfirmed'))
        fixture.report_failure.assert_called_once_with(report)
        execution.assert_not_called()
        settle.assert_not_called()
        local.assert_called_once()
        release.assert_called_once_with(gate.LocalGateAssignment(fixture.local.grant, guest.owner, sidecar), 0)
        self.assert_guest_clean('probe', 'arm', 'arm', 'arm')

    def test_recovery_uses_a_saved_guest_when_the_holder_exits_without_releasing(self) -> None:
        request = self.lose_guest_permission_reply(43)
        episode = request.assignment.episode
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        launch, _ = path.read_bytes().splitlines(keepends=True)
        _ = path.write_bytes(launch)
        with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
            mac_linux.reconcile_unproven(episode, 43)
            submit.assert_not_called()
            with patch.object(process_start, 'process_start', return_value=process_start.Gone()):
                mac_linux.reconcile_unproven(episode, 43)
                submit.assert_called_once()
                self.assertIsInstance(mac_linux.settlement(episode, 43, legacy_holder_ended=True), placement.Pending)
                mac_linux.reconcile_guest_end(episode, 43)
                self.assertEqual(mac_linux.settlement(episode, 43), placement.Settled())
        self.assert_guest_clean('fence')

    def test_unrecorded_attempt_with_no_holder_identity_stays_pending(self) -> None:
        episode = self.placed.episode
        with (patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit,
              patch.object(mac_linux, 'recover_guest_launch') as fence):
            mac_linux.reconcile_unproven(episode, 44)
            mac_linux.reconcile_guest_end(episode, 44)
        submit.assert_not_called()
        fence.assert_not_called()
        self.assertIsInstance(mac_linux.settlement(episode, 44), placement.Pending)
        self.assert_guest_clean()

    def test_reconcile_unproven_checks_for_proof_after_preparing_submission(self) -> None:
        request = self.lose_guest_permission_reply(40)
        episode = request.assignment.episode
        source_file = Path(mac_linux.__file__)
        resolve = Path.resolve

        def finish_recovery(path: Path, strict: bool = False) -> Path:
            if path == source_file:
                mac_linux.reconcile_guest_end(episode, 40)
            return resolve(path, strict=strict)

        with (patch.object(Path, 'resolve', finish_recovery),
              patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit):
            mac_linux.reconcile_unproven(episode, 40)
        submit.assert_not_called()
        self.assertEqual(mac_linux.settlement(episode, 40), placement.Settled())
        self.assert_guest_clean('fence')

    def test_reconcile_unproven_skips_an_attempt_with_a_proven_end(self) -> None:
        request = self.lose_guest_permission_reply(37)
        episode = request.assignment.episode
        mac_linux.reconcile_guest_end(episode, 37)
        self.assertEqual(mac_linux.settlement(episode, 37), placement.Settled())
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        history = path.read_bytes()
        with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as submit:
            mac_linux.reconcile_unproven(episode, 37)
        submit.assert_not_called()
        mac_linux.reconcile_guest_end(episode, 37)
        self.assertEqual(path.read_bytes(), history)
        self.assert_guest_clean('fence')

    def test_guest_services_read_proof_from_a_relative_state_root_after_chdir(self) -> None:
        episode = self.placed.episode
        journal = target_config.mac_linux_end_proof_journal()
        journal.parent.mkdir()
        common = {'claim': 'fixture', 'detail': 'Fixture proof', 'written_at': time.time()}
        armed = mac_linux.ArmedGuestAttempt('guest-run', 'f' * 32, 'boot-id', 10, 'user', '/fixture')
        proof = {**common, 'attempt': execution_identity.attempt_key(episode, f'{23:032x}'), 'result': 'proven'}
        pending = {**common, 'attempt': execution_identity.attempt_key(episode, f'{24:032x}'),
                   'result': 'unproven', 'armed': armed.to_record()}
        _ = journal.write_text(''.join(json.dumps(row) + '\n' for row in (proof, pending)))
        child_directory = self.root / 'child'
        child_directory.mkdir()
        run = subprocess.run
        probe = (
            'import sys\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'from scripts.build_queue.placement import Settled\n'
            'from scripts.build_targets.mac_linux import settlement\n'
            'result = settlement(sys.argv[2], 23)\n'
            'assert isinstance(result, Settled), result\n'
        )

        with chdir(self.root), patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': 'target-state'}):
            self.assertEqual(mac_linux.settlement(episode, 23), placement.Settled())
            with patch('scripts.aws_target.cloud_gate.start_reconcile_service') as reconcile:
                mac_linux.reconcile_unproven(episode, 24)
            reconcile.assert_called_once()
            with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as watcher:
                mac_test.start_claim_watcher(claim_state.state_paths('mac'))
            watcher.assert_called_once()

            for launcher, command in (
                ('guest recovery', cast(list[str], reconcile.call_args.args[0])),
                ('claim watcher', cast(list[str], watcher.call_args.args[0])),
            ):
                setting = '--setenv=BUILD_TARGETS_STATE_DIR='
                forwarded, = (arg.removeprefix(setting) for arg in command if arg.startswith(setting))
                with self.subTest(launcher=launcher):
                    child = run([sys.executable, '-B', '-c', probe, str(CHECKOUT), episode],
                                cwd=child_directory, env={**os.environ, 'BUILD_TARGETS_STATE_DIR': forwarded},
                                capture_output=True, text=True, timeout=10, check=False)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(Path(forwarded), journal.parent.parent)

    def test_reconcile_unproven_guest_after_the_route_returns(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'HOME': str(self.root)})))
        _ = os.environ.pop('BUILD_TARGETS_STATE_DIR', None)
        target_config.build_targets_state_root().mkdir(parents=True)
        attempt = 23
        self.placed = replace(self.placed, attempt=f'{attempt:032x}')
        episode = self.placed.episode
        armed = mac_linux.ArmedGuestAttempt('guest-run', 'f' * 32, 'boot-id', 10, 'user', '/fixture')
        with (patch.object(mac_linux, 'transfer_source',
                           side_effect=mac_linux.GuestEndUnproven('guest unavailable', armed)),
              patch.object(mac_linux, 'send_guest_execution') as report):
            with self.assertRaises(mac_linux.GuestEndUnproven):
                _ = self.route()
        report.assert_not_called()

        def submit(command: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertIn('--no-block', command)
            self.assertNotIn('--wait', command)
            self.assertEqual(command[-3:], ['--reconcile-end', episode, str(attempt)])
            return subprocess.CompletedProcess(list(command), 0, '', '')

        with (patch.object(subprocess, 'run', side_effect=submit) as start,
              patch.object(mac_linux, 'prove_kept_guest_end', side_effect=[
                  mac_linux.GuestEndPending('guest unavailable'),
                  mac_linux.GuestEndPending('guest unavailable'),
                  mac_linux.GuestEndProven()]) as prove,
              redirect_stderr(io.StringIO()) as errors):
            for _index in range(3):
                self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)
                mac_linux.reconcile_unproven(episode, attempt)
                self.assertEqual(prove.call_count, _index)
                mac_linux.reconcile_guest_end(episode, attempt)
            self.assertEqual(mac_linux.settlement(episode, attempt), placement.Settled())
            mac_linux.reconcile_unproven(episode, attempt)
            self.assertEqual(start.call_count, 3)
            mac_linux.reconcile_guest_end(episode, attempt)
        for launch in start.call_args_list:
            command = cast(list[str], launch.args[0])
            self.assertEqual([arg for arg in command if arg.startswith('--setenv=BUILD_TARGETS_STATE_DIR=')], [])
        self.assertEqual(prove.call_args_list, [call(armed)] * 3)
        self.assertEqual(errors.getvalue().splitlines(), ['Mac Linux guest reconcile failed: guest unavailable'])

    def test_guest_end_check_skips_ssh_during_backoff(self) -> None:
        attempt = 23
        episode = self.placed.episode
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.ArmedGuestAttempt('guest-run', 'f' * 32, 'boot-id', 10, settings.guest_user, '/fixture')
        journal = target_config.mac_linux_end_proof_journal()
        journal.parent.mkdir(exist_ok=True)
        saved = {'attempt': execution_identity.attempt_key(episode, f'{attempt:032x}'), 'claim': 'fixture-claim',
                 'result': 'unproven', 'detail': 'The guest did not answer', 'written_at': time.time(),
                 'armed': armed.to_record()}
        _ = journal.write_text(json.dumps(saved) + '\n', encoding='utf-8')
        delay = conf.read('mac_test').number('unreachable_backoff_s')
        now = time.time()
        self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)

        failed = subprocess.CompletedProcess(['ssh'], 255, '', 'ssh: connect to host fixture-mac port 22: Connection timed out')
        held = subprocess.CompletedProcess(['ssh'], mac_linux.GUEST_END_PENDING_STATUS, '',
                                          'The guest run lock is still held.\n')
        ended = subprocess.CompletedProcess(['ssh'], 0, 'ended\n', '')
        with (patch.object(mac_linux, 'run_guest_ssh', side_effect=[failed, held, ended]) as ssh,
              patch.object(mac_linux, 'time', return_value=now) as clock, redirect_stderr(io.StringIO())):
            mac_linux.reconcile_guest_end(episode, attempt)
            self.assertIs(eligibility.read_backoff(now + delay - 1, mac_linux.TARGET), eligibility.ACTIVE_BACKOFF)
            mac_linux.reconcile_guest_end(episode, attempt)
            self.assertEqual(ssh.call_count, 1, 'The end-proof check used SSH during guest backoff.')
            self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)

            clock.return_value = now + delay + 1
            mac_linux.reconcile_guest_end(episode, attempt)
            self.assertEqual(ssh.call_count, 2)
            self.assertFalse(eligibility.backoff_path(mac_linux.TARGET).exists())
            self.assertIsInstance(mac_linux.settlement(episode, attempt), placement.Pending)

            mac_linux.reconcile_guest_end(episode, attempt)
            self.assertEqual(ssh.call_count, 3)
            self.assertEqual(mac_linux.settlement(episode, attempt), placement.Settled())

    def test_reconcile_unproven_does_not_warn_for_an_existing_submission(self) -> None:
        attempt = 23
        self.placed = replace(self.placed, attempt=f'{attempt:032x}')
        episode = self.placed.episode
        armed = mac_linux.ArmedGuestAttempt('guest-run', 'f' * 32, 'boot-id', 10, 'user', '/fixture')
        with (patch.object(mac_linux, 'transfer_source',
                           side_effect=mac_linux.GuestEndUnproven('guest unavailable', armed)),
              self.assertRaises(mac_linux.GuestEndUnproven)):
            _ = self.route()

        def duplicate(command: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            unit = next(word.removeprefix('--unit=') for word in command if word.startswith('--unit='))
            return subprocess.CompletedProcess(list(command), 1, '',
                f'Failed to start transient service unit: Unit {unit}.service was already loaded or has a fragment file.\n')

        with (patch.object(subprocess, 'run', side_effect=duplicate) as start,
              patch.object(mac_linux, 'prove_kept_guest_end', return_value=mac_linux.GuestEndPending('guest unavailable')),
              redirect_stderr(io.StringIO()) as errors):
            mac_linux.reconcile_unproven(episode, attempt)
            start.assert_called_once()
            self.assertEqual(errors.getvalue(), '')
            mac_linux.reconcile_guest_end(episode, attempt)
            self.assertEqual(errors.getvalue(), 'Mac Linux guest reconcile failed: guest unavailable\n')

    def test_completion_report_does_not_wait_for_stalled_configuration(self) -> None:
        entered = Event()
        release = Event()
        finished = Event()
        read = conf.read
        dispatch = self.guest_dispatch()
        dispatch.started = mac_linux.ArmedGuestAttempt('stream-attempt', 'f' * 32, 'boot-id', 10, 'guest', '/fixture')

        def read_config(file: str | Path) -> conf.ConfigRead:
            if file == 'build_admission':
                entered.set()
                _ = release.wait(3)
                finished.set()
            return read(file)

        with (patch.object(conf, 'read', read_config),
              patch.object(mac_linux, 'SSH_CONNECT_TIMEOUT_SECONDS', 0.2),
              patch.object(mac_linux, 'send_guest_execution') as send,
              redirect_stderr(io.StringIO()) as errors):
            try:
                mac_linux.settle_guest_dispatch(dispatch, placement.AttemptCompleted(0))
                self.assertTrue(entered.is_set())
                self.assertFalse(finished.is_set(), 'The completion report waited for configuration.')
            finally:
                release.set()
                if entered.is_set():
                    self.assertTrue(finished.wait(1))
            send.assert_not_called()
        self.assertEqual(errors.getvalue(), 'Mac Linux guest completion report failed: '
                         + 'the queue report configuration read timed out\n')

    def test_guest_report_failure_warns_once_and_keeps_the_result(self) -> None:
        with (redirect_stderr(io.StringIO()) as errors,
              patch.object(mac_linux, 'send_guest_execution', side_effect=wire.AdmissionUnavailable('fixture refusal'))):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual(errors.getvalue().count('Mac Linux guest queue report failed: fixture refusal'), 1)
        self.assertEqual(errors.getvalue().count('Mac Linux guest completion report failed: fixture refusal'), 1)

    def test_unsaved_guest_completion_warns_once_and_keeps_the_result(self) -> None:
        permission = placement.PermissionConsumed(1.0)
        pending = wire.DispatchStatus(self.placed, placement.CurrentAssignment(), permission)

        def status(_request: admission_wire.PlacementStatusRequest, *, deadline: float,
                   timeout: float) -> admission_wire.PlacementStatusReply:
            self.assertGreater(deadline, time.monotonic())
            self.assertEqual(timeout, 1)
            return admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
                self.placed, pending.state, self.consumed[0][0].claim, permission))

        with (patch.object(mac_linux, 'send_guest_execution', return_value=pending) as send,
              patch.object(mac_linux, '_guest_queue_query', side_effect=status) as query,
              redirect_stderr(io.StringIO()) as errors):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        _start, completion = send.call_args_list
        report = cast(wire.ExecutionReport, completion.args[0])
        self.assertEqual(report.outcome, placement.AttemptCompleted(0))
        query.assert_called_once_with(admission_wire.PlacementStatusRequest(
            self.placed.episode, self.placed.attempt, include_permission=True,
            code_version=admission_client.LOADED_ADMISSION_CODE_VERSION), **completion.kwargs)
        self.assertEqual(errors.getvalue(), 'Mac Linux guest completion report failed: the queue did not save the guest completion\n')

    def test_closed_stderr_keeps_the_failed_guest_verdict_after_a_report_timeout(self) -> None:
        completions: list[wire.ExecutionReport] = []

        def report(request: wire.ExecutionReport, *, deadline: float, timeout: float) -> wire.DispatchStatus:
            self.assertGreater(deadline, time.monotonic())
            if isinstance(request.outcome, placement.AttemptCompleted):
                completions.append(request)
                raise wire.AdmissionUnavailable(f'no answer in {timeout:g} s')
            return wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))

        with (redirect_stderr(io.StringIO()) as errors,
              patch.object(errors, 'write', side_effect=BrokenPipeError('fixture stderr pipe closed')) as write,
              patch.object(mac_linux, 'send_guest_execution', report)):
            result = self.route('red')
        assert isinstance(result, GateRun), result
        self.assertFalse(result.passed)
        self.assertEqual([job.returncode for job in result.jobs], [1, 1])
        completion, = completions
        self.assertEqual(completion.outcome, placement.AttemptCompleted(1))
        self.assertEqual(completion.path, placement.DispatchPathSettled())
        write.assert_called_once_with('Mac Linux guest completion report failed: no answer in 1 s')

    def test_guest_module_progress_uses_the_placement_sidecar(self) -> None:
        sidecar = self.root / 'placement.cgroup'
        response = wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))
        with (patch.dict(os.environ, {'BUILDLOG_PLACEMENT_SIDECAR': str(sidecar)}),
              patch.object(mac_linux, 'send_guest_execution', return_value=response)):
            result = self.route()
        self.assertIsInstance(result, GateRun)
        self.assertEqual(progress_profile.read_counter(sidecar), progress_profile.ProgressCounter('modules', 2, 3))

    def test_guest_ssh_does_not_receive_the_placement_sidecar(self) -> None:
        sidecar = self.root / 'placement.cgroup'
        with patch.dict(os.environ, {'BUILDLOG_PLACEMENT_SIDECAR': str(sidecar), 'FIXTURE_CHECK_SIDECAR': '1'}):
            result = self.route()
            self.assertEqual(os.environ.get('BUILDLOG_PLACEMENT_SIDECAR'), str(sidecar))
        self.assertIsInstance(result, GateRun, result)
        self.assertEqual(progress_profile.read_counter(sidecar), progress_profile.ProgressCounter('modules', 2, 3))

    def guest_dispatch(self) -> mac_linux.GuestGateDispatch:
        destination = self.placed.destination
        claim = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                                                 FIXTURE_CLAIM.claim_id, self.placed.episode, self.placed.attempt)
        return mac_linux.GuestGateDispatch(self.placed, claim)

    def test_stream_reports_start_and_progress_before_the_guest_exits(self) -> None:
        armed = mac_linux.ArmedGuestAttempt('stream-attempt', 'f' * 32, 'boot-id', 10, 'guest', '/fixture/checkout')
        dispatch = self.guest_dispatch()
        sidecar = self.root / 'placement.cgroup'
        publish_counter = progress_profile.publish_counter
        reports: list[wire.ExecutionReport] = []

        caller, guest = socket.socketpair()
        with caller, guest:
            def report(request: wire.ExecutionReport, *, deadline: float, timeout: float) -> wire.DispatchStatus:
                self.assertGreater(deadline, time.monotonic())
                self.assertEqual(timeout, 1)
                reports.append(request)
                caller.sendall(b's')
                return wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))

            def publish(path: Path, counter: progress_profile.ProgressCounter) -> None:
                publish_counter(path, counter)
                if counter.done == 2:
                    caller.sendall(b'd')

            start = mac_linux.run_start_line(armed.attempt)
            records = '\n'.join(mac_linux.GUEST_PROGRESS + json.dumps(line) for line in (
                'Runner: 3 modules, whole machine, 2 CPUs', 'first exit=0 seconds=0.1', 'second exit=0 seconds=0.1'))
            program = ('import os, sys; fd = int(sys.argv[1]); '
                       + 'print("mac-linux run start wrong-attempt", file=sys.stderr, flush=True); '
                       + f'print({start!r}, file=sys.stderr, flush=True); '
                       + 'assert os.read(fd, 1) == b"s"; '
                       + f'print({start!r}, file=sys.stderr, flush=True); '
                       + f'print({records!r}, flush=True); '
                       + 'assert os.read(fd, 1) == b"d"; print("reply")')
            command = [sys.executable, '-c', program, str(guest.fileno())]
            with (patch.dict(os.environ, {'BUILDLOG_PLACEMENT_SIDECAR': str(sidecar)}),
                  patch.object(mac_linux, 'send_guest_execution', report),
                  patch.object(progress_profile, 'publish_counter', publish)):
                result = mac_linux.stream_guest_bundle(self.repo, command, b'', 5, guest.fileno(), armed, dispatch)
        self.assertEqual((result.returncode, result.stdout), (0, b'reply\n'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(progress_profile.read_counter(sidecar), progress_profile.ProgressCounter('modules', 2, 3))

    def test_guest_ignores_progress_lines_in_module_stderr(self) -> None:
        sidecar = self.root / 'placement.cgroup'
        start = mac_linux.run_start_line('stream-attempt')
        diagnostics = (start + '\nRunner: 1 modules, whole machine, 2 CPUs\nfake exit=0 seconds=0.1\n'
                       + 'Runner: ' + '9' * 5000 + ' modules, whole machine, 2 CPUs\n').encode()
        record = mac_linux.GUEST_PROGRESS + json.dumps('Runner: 3 modules, whole machine, 2 CPUs')
        program = f'import sys; sys.stderr.buffer.write({diagnostics!r}); print({record!r}); print("reply")'
        result = self.run_guest_stream(program, sidecar)
        self.assertEqual((result.stdout, result.stderr), (b'reply\n', diagnostics))
        self.assertEqual(progress_profile.read_counter(sidecar), progress_profile.ProgressCounter('modules', 0, 3))

    def test_guest_progress_parses_each_record_once(self) -> None:
        from scripts.aws_target import cloud_gate

        sidecar = self.root / 'placement.cgroup'
        records = ('Runner: 4 modules, whole machine, 2 CPUs', 'first exit=0 seconds=0.1',
                   'second exit=0 seconds=0.1', 'first exit=0 seconds=0.1')
        stream = '\n'.join(mac_linux.GUEST_PROGRESS + json.dumps(record) for record in records)
        start = mac_linux.run_start_line('stream-attempt')
        program = f'import sys; print({start!r}, file=sys.stderr); print({stream!r}); print("reply")'
        observe = cloud_gate.ModuleProgress.observe
        observed: list[str] = []

        def record_progress(progress: cloud_gate.ModuleProgress, line: str) -> None:
            observed.append(line)
            observe(progress, line)

        with patch.object(cloud_gate.ModuleProgress, 'observe', record_progress):
            result = self.run_guest_stream(program, sidecar)
        self.assertEqual(result.stdout, b'reply\n')
        self.assertEqual(observed, list(records))
        self.assertEqual(progress_profile.read_counter(sidecar), progress_profile.ProgressCounter('modules', 2, 4))

    def test_stream_collects_the_report_timeout_before_returning(self) -> None:
        timeout = 0.2
        _ = (self.root / 'config/build_admission.conf').write_text(
            f'# range: 0.01..\nquery_timeout_seconds={timeout}\n', encoding='utf-8')
        start = mac_linux.run_start_line('stream-attempt')
        program = f'import sys; print({start!r}, file=sys.stderr); print("reply")'
        address = str(Path(self.enterContext(short_socket_directory())) / 'report.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as queue:
            queue.bind(address)
            queue.listen()
            queue.settimeout(1)
            with (patch.dict(os.environ, {'ADMISSION_SOCKET': address}),
                  patch.object(mac_linux, 'send_guest_execution', side_effect=send_guest_execution),
                  redirect_stderr(io.StringIO()) as errors):
                result = self.run_guest_stream(program, self.root / 'placement.cgroup')
            warning = f'Mac Linux guest queue report failed: no answer in {timeout:g} s'
            self.assertEqual(errors.getvalue().count(warning), 1, errors.getvalue())
            connection = queue.accept()[0]
            with connection, connection.makefile('rb') as request_file:
                report = wire.parse_request(request_file.readline())
        self.assertEqual((result.returncode, result.stdout), (0, b'reply\n'))
        self.assertIsInstance(report, wire.ExecutionReport)

    def test_stalled_report_config_read_does_not_hold_the_guest_result(self) -> None:
        read = conf.read
        entered = Event()
        release = Event()
        finished = Event()

        def read_config(file: str | Path) -> conf.ConfigRead:
            if file == 'build_admission':
                entered.set()
                _ = release.wait(3)
                finished.set()
            return read(file)

        start = mac_linux.run_start_line('stream-attempt')
        program = f'import sys; print({start!r}, file=sys.stderr); print("reply")'
        try:
            with (patch.object(conf, 'read', read_config),
                  patch.object(mac_linux, 'SSH_CONNECT_TIMEOUT_SECONDS', 0.2),
                  patch.object(mac_linux, 'send_guest_execution', side_effect=send_guest_execution),
                  redirect_stderr(io.StringIO()) as errors):
                result = self.run_guest_stream(program, self.root / 'placement.cgroup')
                self.assertTrue(entered.is_set())
                self.assertFalse(finished.is_set(), 'The guest waited for the report configuration read.')
                self.assertEqual((result.returncode, result.stdout), (0, b'reply\n'))
                self.assertEqual(errors.getvalue().count('Mac Linux guest queue report failed:'), 1)
        finally:
            release.set()
            if entered.is_set():
                self.assertTrue(finished.wait(1))
        self.assertEqual(errors.getvalue().count('Mac Linux guest queue report failed:'), 1)

    def test_timed_out_stream_stops_ssh_while_the_report_is_stalled(self) -> None:
        entered = Event()
        release = Event()
        finished = Event()
        stop = mac_linux.stop_ssh_process

        def report(*_args: object, **_kwargs: object) -> None:
            entered.set()
            _ = release.wait(3)
            finished.set()
            raise wire.AdmissionUnavailable('fixture late refusal')

        def stop_ssh(process: subprocess.Popen[bytes]) -> None:
            stop(process)
            self.assertTrue(entered.is_set())
            self.assertFalse(finished.is_set(), 'SSH cleanup waited for the report.')
            self.assertEqual(process.returncode, -signal.SIGKILL)

        start = mac_linux.run_start_line('stream-attempt')
        program = f'import signal, sys; print({start!r}, file=sys.stderr, flush=True); signal.pause()'
        with (patch.object(mac_linux, 'report_guest_start', report),
              patch.object(mac_linux, 'stop_ssh_process', stop_ssh),
              redirect_stderr(io.StringIO()) as errors):
            try:
                with self.assertRaisesRegex(mac_linux.GuestResultUnavailable, 'exceeded 1 seconds'):
                    _ = self.run_guest_stream(program, self.root / 'placement.cgroup', timeout=1)
            finally:
                release.set()
                if entered.is_set():
                    self.assertTrue(finished.wait(1))
            self.assertEqual(errors.getvalue().count('Mac Linux guest queue report failed:'), 1, errors.getvalue())

    def test_stream_timeout_preserves_stderr_already_read(self) -> None:
        diagnostic = b'diagnostic: \xff\r\n'
        program = f'import signal, sys; sys.stderr.buffer.write({diagnostic!r}); sys.stderr.flush(); signal.pause()'
        with self.assertRaisesRegex(mac_linux.GuestUnavailable, 'diagnostic:') as caught:
            _ = self.run_guest_stream(program, self.root / 'placement.cgroup', timeout=2)
        self.assertNotIsInstance(caught.exception, mac_linux.GuestResultUnavailable)
        reason = caught.exception.__cause__
        assert isinstance(reason, subprocess.TimeoutExpired)
        self.assertEqual(reason.stderr, diagnostic)

    def run_guest_stream(self, program: str, sidecar: Path, timeout: int = 5) -> subprocess.CompletedProcess[bytes]:
        armed = mac_linux.ArmedGuestAttempt('stream-attempt', 'f' * 32, 'boot-id', 10, 'guest', '/fixture/checkout')
        with (mac_linux.guest_link() as link,
              patch.dict(os.environ, {'BUILDLOG_PLACEMENT_SIDECAR': str(sidecar)})):
            return mac_linux.stream_guest_bundle(self.repo, [sys.executable, '-c', program], b'', timeout,
                                                 link, armed, self.guest_dispatch())

    def test_stream_preserves_stdout_and_all_stderr_bytes(self) -> None:
        output = b'reply\n' * 16000
        errors = b'first\r\ninvalid byte: \xff\n' * 5000 + b'last'
        bootstrap = b'x' * 100000
        program = ('import sys; sys.stdout.buffer.write(b"reply\\n" * 16000); '
                   + 'sys.stderr.buffer.write(b"first\\r\\ninvalid byte: \\xff\\n" * 5000 + b"last"); '
                   + 'assert sys.stdin.buffer.read() == b"x" * 100000')
        armed = mac_linux.ArmedGuestAttempt('stream-attempt', 'f' * 32, 'boot-id', 10, 'guest', '/fixture/checkout')
        dispatch = self.guest_dispatch()
        with mac_linux.guest_link() as link:
            result = mac_linux.stream_guest_bundle(self.repo, [sys.executable, '-c', program], bootstrap, 5,
                                                   link, armed, dispatch)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, output, errors))

    def test_a_targeted_run_without_the_natedev_module_list_fails_before_ssh(self) -> None:
        result = mac_linux.route(self.repo, mode=SuiteMode.TARGETED, full=False, selected=ALL_MODULES,
                                 module_time_limit_s=3, gate_job=GATE_JOB_NOT_RECORDED, assignment=self.placed)
        self.assertEqual(result, 'mac-linux: the targeted run has no module list from the natedev selection')
        self.assert_guest_clean()

    def test_mac_claim_refuses_another_sessions_block(self) -> None:
        paths = claim_state.state_paths('mac')
        paths.directory.mkdir()
        now = mac_test.utc_now()
        delivery = claim_state.RecordedSession('block-owner', 'fixture-owner')
        showrunner = claim_state.NoShowrunner()
        expires = now + timedelta(hours=1)
        pending = claim_state.PendingMacBlock('fixture-owner', 'fixture block', now, expires, delivery,
                                              showrunner, 'not_configured', claim_state.WaitingOnCiJob())
        active = claim_state.ActiveMacBlock('fixture-owner', 'fixture block', now, expires, delivery,
                                            showrunner, 'not_configured', claim_state.FreeMessageNotNeeded())
        with patch.object(claim_state, 'submitting_session', return_value=OwningSession('submitting-session')):
            for block in (pending, active):
                with self.subTest(state=type(block).__name__):
                    mac_test.write_block(paths.block, block)
                    with self.assertRaisesRegex(mac_run.MacRunRefused, 'blocked by .*fixture block'):
                        with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                            self.fail('The guest acquired a claim during a Mac block.')
                    self.assertRegex(self.not_started(self.route()), r'^blocked by .*fixture block$')
                    self.assertEqual(claim_state.read_claims(paths), ())
        self.assertIs(eligibility.read_backoff(time.time(), mac_linux.TARGET), eligibility.NO_BACKOFF)
        self.assert_guest_clean()

    def test_the_gate_holds_its_launch_permission_before_the_guest_starts(self) -> None:
        report_received = Event()

        def receive_report(report: wire.ExecutionReport, **_timing: float) -> wire.DispatchStatus:
            if isinstance(report.outcome, placement.AttemptCompleted):
                return wire.DispatchStatus(self.placed, placement.ClosedAssignment('completed'),
                                           placement.PermissionConsumed(1.0))
            report_received.set()
            return wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))

        with patch.object(mac_linux, 'send_guest_execution', side_effect=receive_report) as report_start:
            result = self.route()
            self.assertTrue(report_received.wait(2))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        (request, trace, held), = self.consumed
        start = process_start.process_start(os.getpid())
        assert isinstance(start, process_start.Started)
        self.assertEqual(request.assignment, self.placed)
        self.assertTrue(request.guest_proof_required)
        self.assertEqual(request.holder, placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start))
        destination = self.placed.destination
        self.assertEqual(request.claim, placement.ProviderClaimReference(destination.provider, destination.target,
            destination.scope, request.claim.claim_id, self.placed.episode, self.placed.attempt))
        self.assertTrue(request.claim.claim_id.startswith(mac_test.MAC_LINUX_CLAIM_ID_PREFIX))
        self.assertTrue(held)
        self.assertEqual(trace, '')
        started, completed = report_start.call_args_list
        reported = cast(wire.ExecutionReport, started.args[0])
        self.assertEqual((reported.assignment, reported.claim), (self.placed, request.claim))
        self.assertIsInstance(reported.outcome, placement.AttemptOutcomeUnavailable)
        completion = cast(wire.ExecutionReport, completed.args[0])
        self.assertEqual((completion.assignment, completion.claim, completion.evidence, completion.outcome),
                         (reported.assignment, reported.claim, reported.evidence, placement.AttemptCompleted(0)))
        decoded = wire.parse_request(wire.encode_request(reported))
        assert isinstance(decoded, wire.ExecutionReport)
        self.assertEqual(decoded.evidence, execution_identity.ObservedExecution(execution_identity.GuestExecution(
            mac_linux.TARGET, self.placed.attempt, str(self.guest_home / 'build/claude/caller/1'), str(self.guest_home))))
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def check_folder_report_stream(self, *, arm_requested: bool = False, timeout: int = 2) -> None:
        attempt = self.placed.attempt
        marker = mac_linux.guest_start_line(attempt, self.repo, self.guest_home)
        command = [sys.executable, '-c',
                   "import sys; print(sys.argv[1], file=sys.stderr); sys.stderr.write('x' * 131072); print('result')",
                   marker]
        armed = mac_linux.ArmedGuestAttempt(attempt, 'f' * 32, 'boot-id', 10, 'guest', str(self.repo))
        dispatch = self.guest_dispatch()
        dispatch.arm_requested = arm_requested
        with mac_linux.guest_link() as link:
            result = mac_linux.stream_guest_bundle(self.repo, command, b'', timeout, link, armed, dispatch)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'result\n')
        self.assertEqual(result.stderr, marker.encode() + b'\n' + b'x' * 131072)

    def test_a_blocked_folder_report_does_not_discard_the_completed_command(self) -> None:
        report_started = Event()
        release_report = Event()
        report_finished = Event()

        def publish(request: wire.ExecutionReport, **_timing: float) -> wire.DispatchStatus:
            self.assertIsInstance(request.evidence, execution_identity.ObservedExecution)
            if isinstance(request.evidence, execution_identity.ObservedExecution):
                self.assertIsInstance(request.evidence.identity, execution_identity.GuestExecution)
            report_started.set()
            _ = release_report.wait()
            report_finished.set()
            return wire.DispatchStatus(self.placed, placement.CurrentAssignment(), placement.PermissionConsumed(1.0))

        try:
            with patch.object(mac_linux, 'send_guest_execution', publish):
                started = time.monotonic()
                self.check_folder_report_stream(arm_requested=True)
                self.assertLess(time.monotonic() - started, 0.5, 'The guest result waited for the report deadline.')
                self.assertTrue(report_started.wait(2))
                self.assertFalse(report_finished.is_set())
        finally:
            release_report.set()
            if report_started.is_set():
                self.assertTrue(report_finished.wait(2))

    def test_a_folder_publisher_start_failure_preserves_the_command_output(self) -> None:
        self.check_folder_report_thread_failure('report')

    def test_a_stalled_folder_config_read_warns_after_the_guest_result(self) -> None:
        guard = LauncherHangGuard(self.id())
        read_started = Event()
        release_read = Event()
        expire_join = Event()
        preparation_join_timeouts: list[float | None] = []
        output = io.StringIO()
        read = conf.read

        def read_config(file: str | Path) -> conf.ConfigRead:
            if file == 'build_admission':
                read_started.set()
                guard.event(release_read)
                raise TimeoutError('the fixture released the stalled read')
            return read(file)

        def join_preparation(timeout: float | None = None) -> None:
            preparation_join_timeouts.append(timeout)
            guard.event(expire_join)

        with (guard.protect(), redirect_stdout(output), redirect_stderr(output),
              patch.object(conf, 'read', read_config),
              patch.object(mac_linux, 'monotonic', return_value=0.0), ExitStack() as threads):
            def new_thread(*, target: Callable[[], None], daemon: bool) -> Thread:
                worker = Thread(target=target, daemon=daemon)
                if target.__name__ == 'prepare':
                    self.assertTrue(worker.daemon, 'The stalled read must let the process exit.')
                elif target.__name__ == 'collect':
                    self.assertFalse(worker.daemon, 'The collector must keep the process alive to report the timeout.')
                _ = threads.callback(guard.join, worker)
                if target.__name__ == 'prepare':
                    _ = threads.callback(release_read.set)
                    _ = threads.enter_context(patch.object(worker, 'join', join_preparation))
                elif target.__name__ == 'collect':
                    # Let the collector warn before releasing the read.
                    _ = threads.callback(expire_join.set)
                return worker

            _ = threads.enter_context(patch.object(mac_linux, 'Thread', new_thread))
            self.check_folder_report_stream()
            guard.event(read_started)
            print('Guest result returned.', flush=True)
        self.assertEqual(preparation_join_timeouts, [2.0], 'The join must end by the guest deadline.')
        self.assertEqual(output.getvalue().splitlines(), [
            'Guest result returned.',
            'Mac Linux guest queue report failed: the queue report configuration read timed out',
        ])

    def test_a_stalled_folder_config_read_does_not_block_interpreter_exit(self) -> None:
        program = """
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

from scripts.build_targets import mac_linux
from scripts.build_targets.test_mac_linux import MacLinuxRouteTests
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

read_started = Event()
read = conf.read

def read_config(file):
    if file == 'build_admission':
        read_started.set()
        Event().wait()
    return read(file)

case = MacLinuxRouteTests('runTest')
case.repo, case.guest_home = map(Path, sys.argv[1:])
with (patch.object(conf, 'read', read_config),
      patch.object(mac_linux, 'SSH_CONNECT_TIMEOUT_SECONDS', 0)):
    case.check_folder_report_stream(timeout=TEST_HANG_GUARD_SECS)
    read_started.wait()
    print('The guest result returned and the config read is still blocked.', flush=True)
"""
        process = subprocess.run([sys.executable, '-B', '-c', program, str(self.repo), str(self.guest_home)],
                                 cwd=CHECKOUT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertIn('The guest result returned and the config read is still blocked.',
                      process.stdout.splitlines(), process.stdout)

    def test_a_report_preparation_start_failure_preserves_the_command_output(self) -> None:
        self.check_folder_report_thread_failure('prepare')

    def check_folder_report_thread_failure(self, failing_target: str) -> None:
        start_refused = Event()

        class UnavailableThread(Thread):
            @override
            def start(self) -> None:
                start_refused.set()
                raise RuntimeError('fixture thread cannot start')

        def new_thread(*, target: Callable[[], None], daemon: bool) -> Thread:
            thread = UnavailableThread if target.__name__ == failing_target else Thread
            return thread(target=target, daemon=daemon)

        with patch.object(mac_linux, 'Thread', side_effect=new_thread):
            self.check_folder_report_stream()
            self.assertTrue(start_refused.wait(2))

    def test_first_guest_launch_syncs_the_journal_directory_before_consume(self) -> None:
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        self.assertFalse(path.parent.exists())
        consume = admission_client.consume_permission
        fsync = os.fsync
        sync_folder = atomic_file.sync_folder
        synced: list[int] = []
        folders: list[Path] = []

        def sync_journal_folder(folder: Path) -> None:
            self.assertIn(folder, (path.parent.parent, path.parent))
            sync_folder(folder)
            folders.append(folder)

        def record_sync(fd: int) -> None:
            fsync(fd)
            synced.append(os.fstat(fd).st_ino)

        def check_launch(request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
            record = cast(dict[str, object], json.loads(path.read_bytes()))
            self.assertEqual(record['result'], 'awaiting arm')
            self.assertEqual(record['claim'], request.claim.claim_id)
            self.assertIn(path.stat().st_ino, synced)
            self.assertIn(path.parent.stat().st_ino, synced)
            self.assertGreater(synced.index(path.parent.stat().st_ino), synced.index(path.stat().st_ino))
            self.assertEqual(folders, [path.parent, path.parent.parent])
            return consume(request)

        with (patch.object(process_start, 'process_start', return_value=process_start.Started('123')),
              patch.object(atomic_file, 'sync_folder', side_effect=sync_journal_folder),
              patch.object(os, 'fsync', side_effect=record_sync),
              patch.object(admission_client, 'consume_permission', side_effect=check_launch) as send):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        send.assert_called_once()

    def test_a_missing_state_root_refuses_launch_before_consume(self) -> None:
        root = self.root / 'target-state'
        root.rmdir()
        with patch.object(process_start, 'process_start', return_value=process_start.Started('123')):
            result = self.route()
        self.assertEqual(self.consumed, ())
        self.assertFalse(root.exists())
        reason = self.not_started(result)
        self.assertEqual(reason, f'The Mac Linux state root {root} must be a directory. '
                         + 'The admission service creates it at startup. '
                         + 'To repair it, run build_targets.py setup-mac-linux on natedev.')
        self.assert_guest_clean()

    def test_setup_mac_linux_syncs_existing_ancestors_of_the_state_root(self) -> None:
        root = self.root / '.local/state/build-targets'
        root.parent.mkdir(parents=True)
        sync_folder = atomic_file.sync_folder

        def sync_parent(folder: Path) -> None:
            self.assertTrue(root.is_dir())
            sync_folder(folder)

        with (patch.object(sys, 'argv', ['build_targets.py', 'setup-mac-linux']),
              patch.dict(os.environ, {'HOME': str(self.root), 'BUILD_TARGETS_STATE_DIR': str(root)}),
              patch.object(atomic_file, 'sync_folder', side_effect=sync_parent) as sync):
            self.assertEqual(build_targets.main(), 0)
        self.assertEqual(sync.call_args_list, [call(root.parent), call(root.parent.parent), call(self.root)])

    def test_setup_mac_linux_refuses_a_symbolic_link_in_the_state_root_path(self) -> None:
        target = self.root / 'fresh-state'
        target.mkdir()
        link = self.root / 'state-link'
        link.symlink_to(target, target_is_directory=True)
        root = link / 'build-targets'

        with (patch.dict(os.environ, {'HOME': str(self.root)}),
              patch.object(Path, 'mkdir') as mkdir,
              patch.object(atomic_file, 'sync_folder') as sync,
              self.assertRaises(OSError) as raised):
            target_config.provision_mac_linux_state_root(root)

        self.assertEqual(str(raised.exception),
                         f'The Mac Linux state root path {link} is a symbolic link. Use a real directory')
        mkdir.assert_not_called()
        sync.assert_not_called()
        self.assertFalse((target / root.name).exists())

    def test_a_failed_journal_directory_sync_does_not_consume_permission(self) -> None:
        path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        fsync = os.fsync

        def fail_directory_sync(fd: int) -> None:
            if path.parent.exists() and os.fstat(fd).st_ino == path.parent.stat().st_ino:
                raise OSError('fixture journal directory sync failed')
            fsync(fd)

        with (patch.object(process_start, 'process_start', return_value=process_start.Started('123')),
              patch.object(os, 'fsync', side_effect=fail_directory_sync),
              patch.object(admission_client, 'consume_permission') as consume):
            self.assertEqual(self.route(), 'mac-linux: fixture journal directory sync failed')
        consume.assert_not_called()
        self.assert_guest_clean()

    def test_journal_sync_stops_at_the_state_root_even_after_a_failed_sync(self) -> None:
        new_root = self.root / 'new' / 'target-state'
        new_root.mkdir(parents=True)
        path = new_root / 'mac-linux' / 'end-proofs.jsonl'
        sync_folder = atomic_file.sync_folder
        fsync = os.fsync
        failed = False
        synced: list[int] = []

        def sync_journal_folder(folder: Path) -> None:
            nonlocal failed
            if folder not in (path.parent, new_root):
                raise PermissionError('fixture ancestor is searchable only')
            if not failed and folder == new_root:
                failed = True
                raise OSError('fixture ancestor sync failed')
            sync_folder(folder)

        def record_sync(fd: int) -> None:
            fsync(fd)
            synced.append(os.fstat(fd).st_ino)

        consume = admission_client.consume_permission

        def check_launch(request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
            expected = [entry.stat().st_ino for entry in (path, path.parent, new_root)]
            first = synced.index(expected[0])
            self.assertEqual(synced[first:first + len(expected)], expected)
            return consume(request)

        with (patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(new_root)}),
              patch.object(process_start, 'process_start', return_value=process_start.Started('123')),
              patch.object(atomic_file, 'sync_folder', side_effect=sync_journal_folder),
              patch.object(os, 'fsync', side_effect=record_sync),
              patch.object(admission_client, 'consume_permission', side_effect=check_launch) as send):
            self.assertEqual(self.route(), 'mac-linux: fixture ancestor sync failed')
            send.assert_not_called()
            self.assertTrue(path.is_file())
            synced.clear()
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        send.assert_called_once()

    def test_a_refused_launch_permission_starts_nothing_on_the_guest(self) -> None:
        refusal = wire.DispatchStatus(self.placed, placement.ClosedAssignment('cancelled'), placement.PermissionAvailable(),
                                      'the attempt is closed: cancelled')
        def refuse(_request: wire.ConsumePermission) -> wire.DispatchStatus:
            return refusal

        cases: tuple[tuple[Callable[[wire.ConsumePermission], wire.DispatchStatus] | wire.AdmissionUnavailable, str], ...] = (
            (refuse, 'the queue refused the launch permission: the attempt is closed: cancelled'),
            (wire.AdmissionUnavailable('the queue is down'), 'the launch permission is unavailable: the queue is down'))
        for answer, reason in cases:
            with self.subTest(reason=reason):
                with patch.object(admission_client, 'consume_permission', side_effect=answer) as consume:
                    self.assertEqual(self.not_started(self.route()), reason)
                consume.assert_called_once()
                self.assertEqual(claim_state.read_claims(claim_state.state_paths(mac_linux.GUEST_CLAIM_TARGET)), ())
                self.assert_guest_clean()

    def test_the_route_consumes_the_assignment_it_got_and_reads_no_placement_file(self) -> None:
        # The gate checked its assignment at its start. The route reads no placement file again.
        placement_file = self.root / 'assignment.json'
        admission_client.write_assignment(placement_file, wire.Assignment(self.placed))
        self.placed = replace(self.placed, attempt='checked-attempt')
        with (patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(placement_file)}),
              patch.object(admission_client, 'read_assignment',
                           side_effect=AssertionError('the route read the placement file'))):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        (request, _, _), = self.consumed
        self.assertEqual(request.assignment, self.placed)

    def test_busy_mac_claim_refuses_the_route_before_ssh(self) -> None:
        paths = claim_state.state_paths('mac')
        with mac_run.held(self.repo, 'mac', StepJob('test-all'), GATE_JOB_NOT_RECORDED):
            claims = claim_state.read_claims(paths)
            self.assertEqual(len(claims), 1)
            self.assertEqual(self.not_started(self.route()), 'mac is busy with mac-linux test-all')
            self.assertEqual(claim_state.read_claims(paths), claims)
        self.assertIs(eligibility.read_backoff(time.time(), mac_linux.TARGET), eligibility.NO_BACKOFF)
        self.assert_guest_clean()

    def test_free_now_recovers_the_guest_enable_key_from_the_repository(self) -> None:
        paths = mac_test.state_paths(mac_linux.TARGET)
        configuration = self.registry.with_name('mac_linux.conf')
        _ = write_config_fixture(configuration, '')
        fallback = self.registry.parent / 'repository' / self.registry.name
        _ = write_config_fixture(fallback, 'enabled.mac-linux=on\n')
        _ = self.registry.write_text(self.registry.read_text().replace('enabled.mac-linux=on\n', ''))
        with (patch.dict(os.environ), patch.object(conf, 'directory', return_value=self.registry.parent),
              patch.object(conf, '_repository_path', return_value=fallback)):
            _ = os.environ.pop('CONF_DIR', None)
            self.assertEqual(mac_test.free_now(paths, configuration, self.repo), 'free')

    def test_free_now_reads_the_probe_backoff_for_a_registered_guest(self) -> None:
        _ = write_config_fixture(self.registry, 'pinned_targets=hana-gpu,mac-linux\n')
        paths = mac_test.state_paths(mac_linux.TARGET)
        configuration = self.registry.with_name('mac_linux.conf')
        _ = write_config_fixture(configuration, '')
        self.assertEqual(mac_test.free_now(paths, configuration, self.repo), 'free')
        now = time.time()
        with patch.object(mac_linux, 'time', return_value=now):
            result = self.not_started(self.route('offline'))
        self.assertIn('fixture SSH refused the connection', result)
        self.assertIs(eligibility.read_backoff(now + 29, mac_linux.TARGET), eligibility.ACTIVE_BACKOFF)
        self.assertIs(eligibility.read_backoff(now + 31, mac_linux.TARGET), eligibility.NO_BACKOFF)
        self.assertIs(eligibility.read_backoff(now), eligibility.NO_BACKOFF)
        self.assertEqual(mac_test.free_now(paths, configuration, self.repo), 'backoff')
        self.assertEqual(self.not_started(self.route()), 'guest unreachable during retry backoff')
        self.assert_guest_clean('probe')

    def test_guest_main_skips_ssh_after_connection_failure(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        delay = conf.read('mac_test').number('unreachable_backoff_s')
        now = time.time()
        failed = subprocess.CompletedProcess(['ssh'], 255, '', 'ssh: connect to host fixture-mac port 22: Connection timed out')
        with (patch.object(mac_linux, 'run_guest_ssh', return_value=failed) as ssh,
              patch.object(mac_linux, 'time', return_value=now)):
            with self.assertRaisesRegex(mac_linux.GuestUnavailable, 'Connection timed out'):
                _ = mac_linux.read_guest_main(settings)
            with self.assertRaises(mac_linux.GuestUnavailable):
                _ = mac_linux.read_guest_main(settings)
        self.assertEqual(ssh.call_count, 1, 'The failed guest main read did not stop the next SSH call.')
        self.assertIs(eligibility.read_backoff(now + delay - 1, mac_linux.TARGET), eligibility.ACTIVE_BACKOFF)
        self.assertIs(eligibility.read_backoff(now + delay + 1, mac_linux.TARGET), eligibility.NO_BACKOFF)

    def test_guest_main_backoff_for_pre_authentication_transport_failure(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        backoff = eligibility.backoff_path(mac_linux.TARGET)
        for diagnostic, expected in (
            ('Connection timed out during banner exchange\nConnection to fixture-mac port 22 timed out',
             eligibility.ACTIVE_BACKOFF),
            ('banner exchange: Connection to 127.0.0.1 port 31022: Broken pipe', eligibility.ACTIVE_BACKOFF),
            ('kex_exchange_identification: Connection closed by remote host', eligibility.ACTIVE_BACKOFF),
            ('kex_exchange_identification: read: Connection reset by peer', eligibility.ACTIVE_BACKOFF),
            ('fixture-guest: Permission denied (publickey).', eligibility.NO_BACKOFF),
            ('Host key verification failed.', eligibility.NO_BACKOFF),
        ):
            with self.subTest(diagnostic=diagnostic):
                backoff.unlink(missing_ok=True)
                failed = subprocess.CompletedProcess(['ssh'], 255, '', diagnostic + '\n')
                with (patch.object(mac_linux, 'run_guest_ssh', return_value=failed),
                      self.assertRaises(mac_linux.GuestUnavailable) as error):
                    _ = mac_linux.read_guest_main(settings)
                self.assertIn(diagnostic, str(error.exception))
                self.assertIs(eligibility.read_backoff(time.time(), mac_linux.TARGET), expected)

    def test_guest_main_invalid_reply_does_not_start_backoff(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        invalid = subprocess.CompletedProcess(['ssh'], 0, 'invalid commit ID\n', '')
        valid = subprocess.CompletedProcess(['ssh'], 0, 'a' * 40 + '\n', '')
        with patch.object(mac_linux, 'run_guest_ssh', side_effect=[invalid, valid]) as ssh:
            with self.assertRaisesRegex(mac_linux.GuestUnavailable, 'invalid commit ID'):
                _ = mac_linux.read_guest_main(settings)
            self.assertFalse(eligibility.backoff_path(mac_linux.TARGET).exists())
            self.assertEqual(mac_linux.read_guest_main(settings), mac_linux.GuestMain('a' * 40))
        self.assertEqual(ssh.call_count, 2)

    def test_guest_main_first_success_clears_backoff(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        offload.write_backoff(time.time(), mac_linux.TARGET, strict=True)
        backoff = eligibility.backoff_path(mac_linux.TARGET)
        self.assertTrue(backoff.is_file())
        commit = 'a' * 40
        good = subprocess.CompletedProcess(['ssh'], 0, commit + '\n', '')
        with patch.object(mac_linux, 'run_guest_ssh', return_value=good) as ssh:
            self.assertEqual(mac_linux.read_guest_main(settings), mac_linux.GuestMain(commit))
            cleared_after_first = not backoff.exists()
            self.assertEqual(mac_linux.read_guest_main(settings), mac_linux.GuestMain(commit))
        self.assertEqual(ssh.call_count, 2, 'Both guest main reads must use SSH.')
        self.assertTrue(cleared_after_first, 'The first good answer left the backoff record.')

    def test_route_records_backoff_for_a_guest_outside_the_placement_lists(self) -> None:
        _ = write_config_fixture(self.registry, 'targets=hana-aws,mac,natedev\npinned_targets=hana-gpu\n')
        registry = conf.read(self.registry)
        for key in ('targets', 'pinned_targets'):
            self.assertNotIn(mac_linux.TARGET, registry.text(key).split(','))

        now = time.time()
        with patch.object(mac_linux, 'time', return_value=now):
            result = self.not_started(self.route('offline'))
            self.assertIn('fixture SSH refused the connection', result)
            backoff = self.root / 'target-state' / mac_linux.TARGET / 'unreachable_until'
            self.assertAlmostEqual(float(backoff.read_text()), now + 30, places=5)
            self.assertEqual(self.not_started(self.route()), 'guest unreachable during retry backoff')
        self.assert_guest_clean('probe')

    def test_invalid_checkout_replies_record_backoff_before_launch_permission(self) -> None:
        first = ('bash', '-c', 'printf "first bad reply\\n"')
        second = ('bash', '-c', 'printf "second bad reply\\n"')
        now = time.time()
        with (patch.object(mac_linux, 'time', return_value=now),
              patch.object(mac_linux.GuestSettings, 'command', side_effect=(first, second)) as probe):
            reason = self.not_started(self.route())
        self.assertEqual(probe.call_count, 2)
        self.assertIn(f'natedev checkout {str(self.repo.resolve())!r}', reason)
        self.assertIn('first bad reply', reason)
        self.assertIn('second bad reply', reason)
        self.assertEqual(self.consumed, ())
        self.assertIs(eligibility.read_backoff(now + 29, mac_linux.TARGET), eligibility.ACTIVE_BACKOFF)
        self.assert_guest_clean()

    def test_backoff_write_error_keeps_the_probe_failure(self) -> None:
        _ = (self.root / 'target-state/mac-linux').write_text('This file blocks the backoff directory.\n')
        reason = self.not_started(self.route('offline'))
        self.assertIn('fixture SSH refused the connection', reason)
        self.assertIn(f'Cannot record the {mac_linux.TARGET} backoff:', reason)
        self.assertIn('File exists', reason)
        self.assert_guest_clean('probe')

    def test_unreachable_wait_refuses_a_missing_key_without_a_committed_value(self) -> None:
        key = target_config.UNREACHABLE_WAIT_KEY
        settings = mac_linux.read_settings(conf.read(self.registry))
        self.assertEqual(settings.unreachable_wait_seconds, 20)
        _ = self.registry.write_text(self.registry.read_text().replace(f'{key}=20\n', ''))
        result = self.route()
        self.assertIsInstance(result, str)
        self.assertIn(key, str(result))
        self.assertIn('key is missing', str(result))
        self.assert_guest_clean()

    def test_unreachable_wait_uses_the_committed_value_when_the_key_is_missing(self) -> None:
        registry = self.repo / 'config/build_targets.conf'
        original = self.registry.read_text().replace(
            'settings.mac-linux=mac_linux.conf', f"settings.mac-linux={self.registry.with_name('mac_linux.conf')}")
        _ = registry.write_text(original)
        _ = self.git('add', 'config/build_targets.conf')
        _ = self.git('commit', '-qm', 'Add the target registry')
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = registry.write_text(original.replace(f'{key}=20\n', ''))
        settings = mac_linux.read_settings(conf.read(registry))
        self.assertEqual(settings.unreachable_wait_seconds, 20)

    def test_unreachable_wait_requires_whole_seconds_in_range(self) -> None:
        key = target_config.UNREACHABLE_WAIT_KEY
        configuration = self.registry
        original = configuration.read_text()
        for value, reason in (
            ('0', 'expected a positive whole number'),
            ('-1', 'expected a positive whole number'),
            ('1.5', 'expected a whole number'),
            ('1' + '0' * 309, 'expected at most 86400 seconds'),
            ('86401', 'expected at most 86400 seconds'),
        ):
            with self.subTest(value=value):
                _ = configuration.write_text(original.replace(f'{key}=20', f'{key}={value}'))
                result = self.route()
                self.assertIn(key, str(result))
                self.assertIn(reason, str(result))
                self.assert_guest_clean()

        _ = configuration.write_text(original.replace(f'{key}=20', f'{key}=86400'))
        settings = mac_linux.read_settings(conf.read(self.registry))
        self.assertEqual(settings.unreachable_wait_seconds, 86400)

    def test_missing_bwrap_refuses_before_upload(self) -> None:
        binaries = self.root / 'without-bwrap'
        binaries.mkdir()
        for name in ('bash', 'cat', 'id'):
            executable = shutil.which(name)
            assert executable is not None
            (binaries / name).symlink_to(executable)
        with patch.dict(os.environ, {'FIXTURE_GUEST_PATH': str(binaries)}):
            result = self.not_started(self.route())
        self.assertIn('bwrap is absent from the guest PATH', result)
        self.assertFalse(eligibility.backoff_path(mac_linux.TARGET).exists())
        self.assert_guest_clean('probe')
        self.assertFalse((self.guest_home / '.claude').exists())

    def test_other_repository_is_refused_before_ssh(self) -> None:
        _ = self.git('remote', 'set-url', 'origin', 'https://example.test/other-repo.git')
        with self.assertRaises(mac_linux.GuestRepositoryRefused) as raised:
            _ = mac_linux.candidate_source(self.repo, ALL_MODULES)
        self.assertEqual(raised.exception.repository, 'other-repo')
        self.assertIn('source repository is other-repo', str(self.route()))
        self.assert_guest_clean()

    def test_local_timeout_kills_the_recorded_group_and_proves_end(self) -> None:
        configuration = self.registry.with_name('mac_linux.conf')
        _ = configuration.write_text(configuration.read_text().replace('seconds=7', 'seconds=30'))
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = self.registry.write_text(self.registry.read_text().replace(f'{key}=20', f'{key}=4'))
        with (patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '30', 'FIXTURE_HELD_LINK': '1'}),
              patch.object(mac_linux, 'GUEST_KILL_GRACE_SECONDS', 1),
              patch.object(mac_linux.GuestSettings, 'transfer_timeout_seconds', 6),
              self.assertRaisesRegex(GateResultUnknown, 'SSH or guest run exceeded 6 seconds') as result):
            _ = self.route('hang')
        group = (self.root / 'run.group').read_text()
        self.assertIn('Guest cleanup: ended', str(result.exception))
        self.assertEqual(self.run_lock().read_text().split()[0], group)
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

    def run_lock(self) -> Path:
        return self.guest_home / '.claude' / mac_linux.RUN_LOCK_PATH

    @staticmethod
    def process_sessions() -> dict[int, tuple[int, int, int]]:
        """The parent, process group and session of each live process."""
        found: dict[int, tuple[int, int, int]] = {}
        for entry in Path('/proc').iterdir():
            if entry.name.isdigit():
                with suppress(OSError):
                    state, parent, group, session = entry.joinpath('stat').read_text().rpartition(')')[2].split()[:4]
                    if state != 'Z':
                        found[int(entry.name)] = (int(parent), int(group), int(session))
        return found

    def assert_run_ends(self, deadline: float, group: int, *sessions: int) -> None:
        """Before the deadline, the run group and the given sessions have no live process, and the run lock is free."""
        while True:
            live = [pid for pid, (_, process_group, session) in self.process_sessions().items()
                    if process_group == group or session in sessions]
            free = subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']) == 0
            if not live and free:
                return
            self.assertLess(time.monotonic(), deadline, f'live processes {live}, run lock free: {free}')
            time.sleep(0.05)

    def test_a_stopped_natedev_side_ends_the_guest_run_with_no_cleanup(self) -> None:
        program = '\n'.join((
            'from pathlib import Path',
            'import sys',
            'from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED',
            'from scripts.build_targets import mac_linux',
            'from scripts.production.gate_routes import ALL_MODULES, SuiteMode',
            'from scripts.build_queue import admission_wire',
            f'mac_linux.__file__ = {str(self.runner_repo / mac_linux.RUNNER_PATH)!r}',
            'mac_linux.consume_launch_permission = lambda claim, assignment: None',
            'mac_linux.report_guest_start = lambda armed, dispatch, **timing: None',
            'mac_linux.route(Path(sys.argv[1]), mode=SuiteMode.GATE, full=True, module_time_limit_s=3,',
            '                selected=ALL_MODULES, gate_job=GATE_JOB_NOT_RECORDED,',
            '                assignment=admission_wire.read_assignment(Path(sys.argv[2])).assignment)'))
        placement_file = self.root / 'assignment.json'
        admission_client.write_assignment(placement_file, wire.Assignment(self.placed))
        environment = {**os.environ, 'FIXTURE_GUEST_RESULT': 'hang',
                       'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main')}
        group_record = self.root / 'run.group'
        with (self.root / 'natedev.log').open('wb') as log, subprocess.Popen(
                [sys.executable, '-c', program, str(self.repo), str(placement_file)], cwd=Path(__file__).resolve().parents[2],
                env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log) as natedev:
            try:
                started = time.monotonic() + 30
                while not group_record.is_file() or not group_record.read_text():
                    self.assertIsNone(natedev.poll(), (self.root / 'natedev.log').read_text())
                    self.assertLess(time.monotonic(), started, 'The guest run did not start.')
                    time.sleep(0.05)
                stream, = (pid for pid, (parent, _, session) in self.process_sessions().items()
                           if parent == natedev.pid and session == pid)
            finally:
                natedev.kill()
        # SIGKILL runs no code in the natedev process, so only the end of its link can stop the guest run.
        deadline = time.monotonic() + mac_linux.GUEST_KILL_GRACE_SECONDS
        self.assert_run_ends(deadline, int(group_record.read_text()), stream)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run'])

    def test_the_link_watcher_ends_with_the_run_and_leaves_no_process(self) -> None:
        # The guest keeps its input open after the run, so a watcher that outlives the run stays live.
        with patch.dict(os.environ, {'FIXTURE_HELD_LINK': '1'}):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        group = int(self.run_lock().read_text().split()[0])
        self.assert_run_ends(time.monotonic() + mac_linux.GUEST_KILL_GRACE_SECONDS, group)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

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
        armed = mac_linux.arm_guest(settings, 'expired-arm-fixture')
        expired = replace(armed, deadline_ns=time.clock_gettime_ns(time.CLOCK_BOOTTIME) - 1)
        git = self.run_lock().parent
        record = git / f'{mac_linux.ARM_RECORD_PREFIX}.{armed.boot_id}.{armed.deadline_ns}.{armed.attempt}'
        _ = record.rename(git / f'{mac_linux.ARM_RECORD_PREFIX}.{expired.boot_id}.{expired.deadline_ns}.{expired.attempt}')
        shell = mac_linux.run_shell(settings, source, 0, expired)
        run = subprocess.run(['bash', '-c', shell],
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

    def test_cleanup_refuses_a_different_guest_before_making_a_checkout(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'different-machine-fixture')
        other_machine_id = 'f' * 32 if armed.machine_id != 'f' * 32 else 'e' * 32
        original_attempt = replace(armed, machine_id=other_machine_id)
        replacement_home = self.root / 'replacement-guest'
        replacement_home.mkdir()
        with patch.dict(os.environ, {'FIXTURE_GUEST_HOME': str(replacement_home)}):
            proof = mac_linux.prove_guest_end(settings, original_attempt)
        assert isinstance(proof, mac_linux.GuestEndPending), proof
        self.assertIn('different guest', proof.detail)
        self.assertEqual(list(replacement_home.iterdir()), [])

    def test_cleanup_accepts_the_same_guest_after_a_reboot(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'rebooted-machine-fixture')
        previous = replace(armed, boot_id='00000000-0000-0000-0000-000000000000')
        self.assertNotEqual(armed.boot_id, previous.boot_id)
        suffix = f'{previous.boot_id}.{previous.deadline_ns}.{previous.attempt}'
        git = self.run_lock().parent
        old_arm = git / f'{mac_linux.ARM_RECORD_PREFIX}.{suffix}'
        old_arm.touch()
        self.assertIsInstance(mac_linux.prove_guest_end(settings, previous), mac_linux.GuestEndProven)
        self.assertTrue((git / f'{mac_linux.CANCEL_RECORD_PREFIX}.{suffix}').is_file())
        self.assertFalse(old_arm.exists())

    def test_armed_attempt_record_round_trip(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'saved-machine-fixture')
        record = armed.to_record()
        self.assertEqual(record, {
            'attempt': 'saved-machine-fixture',
            'machine_id': Path('/etc/machine-id').read_text(encoding='ascii').strip(),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip(),
            'deadline_ns': armed.deadline_ns,
            'guest_user': settings.guest_user,
            'checkout': str((self.guest_home / '.claude').resolve()),
        })
        self.assertEqual(mac_linux.ArmedGuestAttempt.from_record(record), armed)

    def test_missing_arm_acknowledgement_sends_no_run(self) -> None:
        self.placed = replace(self.placed, attempt=f'{41:032x}')
        episode = self.placed.episode
        result = self.route('missing-arm-ack')
        self.assertIn('did not acknowledge the armed deadline', str(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm'])
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertFalse((self.guest_home / 'build').exists())
        self.assertIsInstance(mac_linux.settlement(episode, 41), placement.Pending)
        mac_linux.reconcile_guest_end(episode, 41)
        self.assertEqual(mac_linux.settlement(episode, 41), placement.Settled())
        self.assert_guest_clean('probe', 'arm', 'fence')

    def test_disconnect_before_take_copy_kills_the_run_and_proves_end(self) -> None:
        with patch.object(mac_linux, 'send_guest_execution') as report:
            reason = self.unconfirmed(self.route('disconnect-before-copy'))
        report.assert_not_called()
        self.assertIn('SSH or source stream exited 255', reason)
        self.assertIn('Guest cleanup: ended', reason)
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertFalse((self.guest_home / 'build').exists())
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_unreachable_guest_releases_the_mac_claim_after_proof(self) -> None:
        events: list[str] = []
        prove_guest_end = mac_linux.prove_guest_end
        paths = claim_state.state_paths('mac')

        def prove(settings: mac_linux.GuestSettings,
                  armed: mac_linux.ArmedGuestAttempt, *,
                  proof_deadline: float) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
            claim, = claim_state.read_claims(paths)
            self.assertEqual(claim.record['pid'], os.getpid())
            self.assertEqual(claim.record['worktree'], str(self.repo))
            cancel = self.run_lock().parent / (
                f'{mac_linux.CANCEL_RECORD_PREFIX}.{armed.boot_id}.{armed.deadline_ns}.{armed.attempt}')
            self.assertFalse(cancel.exists())
            proof = prove_guest_end(settings, armed, proof_deadline=proof_deadline)
            if isinstance(proof, mac_linux.GuestEndPending):
                self.assertIn('fixture cleanup SSH refused the connection', proof.detail)
                self.assertFalse(cancel.exists())
                events.append('unreachable')
            else:
                self.assertTrue(cancel.is_file())
                self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
                events.append('ended')
            self.assertEqual(claim_state.read_claims(paths), (claim,))
            return proof

        with (patch.dict(os.environ, {'FIXTURE_OFFLINE_PROOFS': '2'}),
              patch.object(mac_linux, 'prove_guest_end', prove),
              patch.object(mac_linux, 'sleep') as retry_delay):
            result = self.route('disconnect-before-copy')
        self.assertIn('Guest cleanup: ended after fixture cleanup SSH refused the connection', str(result))
        self.assertEqual(events, ['unreachable', 'unreachable', 'ended'])
        self.assertEqual(claim_state.read_claims(paths), ())
        self.assertEqual(retry_delay.call_args_list, [call(mac_linux.SSH_CONNECT_TIMEOUT_SECONDS)] * 2)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup', 'cleanup', 'cleanup'])

    def test_proof_wait_expires_and_a_second_route_cannot_take_the_guest_lock(self) -> None:
        configuration = self.registry
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = configuration.write_text(configuration.read_text().replace(f'{key}=20', f'{key}=2'))
        settings = mac_linux.read_settings(conf.read(self.registry))
        attempts: list[mac_linux.ArmedGuestAttempt] = []
        unproven: list[mac_linux.GuestUnavailable] = []
        arm_guest = mac_linux.arm_guest
        reservation = mac_linux.mac_linux_reservation
        now = 0.0

        @contextmanager
        def record_failure(repo: Path, work: SuiteMode | StepJob,
                           gate_job: StepJob | GateJobNotRecorded) -> Generator[claim_state.ClaimIdentity]:
            with reservation(repo, work, gate_job) as claim:
                try:
                    yield claim
                except mac_linux.GuestUnavailable as error:
                    unproven.append(error)
                    raise

        def record_arm(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
            armed = arm_guest(settings, attempt)
            attempts.append(armed)
            if len(attempts) == 1:
                refs = self.git('for-each-ref', '--format=%(refname)', 'refs/mac-linux').splitlines()
                candidate, = (ref for ref in refs if ref.endswith('/candidate'))
                (self.repo / '.git' / f'{candidate}.lock').touch()
            return armed

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        lock = self.run_lock()
        try:
            with (patch.dict(os.environ, {'FIXTURE_OFFLINE_PROOFS': '1',
                                          'FIXTURE_GUEST_RESULT': 'disconnect-before-copy'}),
                  patch.object(mac_linux, 'mac_linux_reservation', record_failure),
                  patch.object(mac_linux, 'arm_guest', record_arm),
                  patch.object(mac_linux, 'monotonic', lambda: now),
                  patch.object(mac_linux, 'sleep', advance),
                  patch.object(mac_linux, 'GUEST_KILL_GRACE_SECONDS', 0),
                  patch.object(mac_linux, 'prove_guest_end', wraps=mac_linux.prove_guest_end) as proof):
                with self.assertRaises(mac_linux.GuestEndUnproven) as raised:
                    _ = mac_linux.route(self.repo, mode=SuiteMode.GATE, full=True, module_time_limit_s=3,
                                        selected=ALL_MODULES, gate_job=GATE_JOB_NOT_RECORDED, assignment=self.placed)
                first, = attempts
                failure, = unproven
                assert isinstance(failure, mac_linux.GuestEndUnproven), failure
                self.assertEqual(failure.armed, first)
                candidate, = self.git('for-each-ref', '--format=%(refname)', 'refs/mac-linux').splitlines()
                self.assertTrue(candidate.endswith('/candidate'))
                note = f'Cannot remove bundle ref {candidate}'
                self.assertTrue(any(note in line for line in failure.__notes__))
                self.assertIs(raised.exception, failure)
                (self.repo / '.git' / f'{candidate}.lock').unlink()
                _ = self.git('update-ref', '-d', candidate)
                self.assertIn('guest unreachable: fixture-guest did not confirm the run ended '
                              + f'within 2 seconds ({key})', str(failure))
                path = self.root / 'target-state/mac-linux/end-proofs.jsonl'
                launch, line = path.read_bytes().splitlines()
                self.assertEqual(cast(dict[str, object], json.loads(launch))['result'], 'awaiting arm')
                row = cast(dict[str, object], json.loads(line))
                self.assertEqual(row['attempt'], execution_identity.attempt_key(self.placed.episode, self.placed.attempt))
                self.assertEqual(row['claim'], self.consumed[0][0].claim.claim_id)
                self.assertEqual((row['result'], row['detail']), ('unproven', str(failure)))
                self.assertIs(eligibility.read_backoff(time.time(), mac_linux.TARGET), eligibility.NO_BACKOFF)
                identity = mac_linux.GuestCheckoutIdentity(first.machine_id, first.checkout)
                proof.assert_called_once_with(replace(settings, checkout_identity=identity), first, proof_deadline=2.0)
                self.assertEqual(now, 2.0)
                self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])
                self.assertEqual(claim_state.read_claims(claim_state.state_paths('mac')), ())
                owner = lock.read_text()
                self.assertEqual(owner.split()[1], first.attempt)
                self.assertEqual(subprocess.call(['flock', '-x', '-n', lock, 'true'], timeout=2), 1)

                second = self.not_started(self.route())
                self.assertIn('The guest run lock is held', second)
                self.assertEqual(lock.read_text(), owner)
                self.assertEqual(subprocess.call(['flock', '-x', '-n', lock, 'true'], timeout=2), 1)
                self.assertFalse((self.guest_home / 'build').exists())
                self.assertEqual(list(lock.parent.glob(f'{mac_linux.CANCEL_RECORD_PREFIX}.*')), [])
        finally:
            if attempts:
                self.assertIsInstance(mac_linux.prove_guest_end(settings, attempts[0]), mac_linux.GuestEndProven)

    def test_bundle_ref_cleanup_failure_raises_after_normal_exit(self) -> None:
        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        with self.assertRaisesRegex(GateRefused, r'candidate\.lock'):
            with mac_linux.bundle_refs(self.repo, source, 'ref-cleanup-fixture') as prefix:
                (self.repo / '.git' / f'{prefix}/candidate.lock').touch()
        self.assertEqual(self.git('for-each-ref', '--format=%(refname)', 'refs/mac-linux'),
                         'refs/mac-linux/ref-cleanup-fixture/candidate')

    def test_interrupt_in_ref_cleanup_ends_the_route_instead_of_a_retry(self) -> None:
        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        real_git = gate.checked_git

        def interrupt_delete(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ('update-ref', '-d'):
                raise KeyboardInterrupt('fixture interruption in ref cleanup')
            return real_git(repo, *arguments)

        with (patch.object(gate, 'checked_git', interrupt_delete),
              self.assertRaises(KeyboardInterrupt) as raised):
            with mac_linux.bundle_refs(self.repo, source, 'interrupt-cleanup-fixture'):
                raise mac_linux.GuestMissingPrerequisite('fixture missing prerequisite')
        self.assertTrue(any('Interrupted while handling' in note and 'fixture missing prerequisite' in note
                            for note in raised.exception.__notes__))

    def test_interrupt_in_ref_cleanup_keeps_the_unproven_guest_end(self) -> None:
        source = mac_linux.candidate_source(self.repo, ALL_MODULES)
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'unproven-cleanup-fixture')
        real_git = gate.checked_git

        def interrupt_delete(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ('update-ref', '-d'):
                raise KeyboardInterrupt('fixture interruption in ref cleanup')
            return real_git(repo, *arguments)

        with (patch.object(gate, 'checked_git', interrupt_delete),
              self.assertRaises(mac_linux.GuestEndUnproven) as raised):
            with mac_linux.bundle_refs(self.repo, source, 'unproven-cleanup-fixture'):
                raise mac_linux.GuestEndUnproven('fixture unproven end', armed)
        self.assertEqual(raised.exception.armed, armed)
        self.assertTrue(any('fixture interruption in ref cleanup' in note for note in raised.exception.__notes__))
        self.assertIsInstance(mac_linux.prove_guest_end(settings, armed), mac_linux.GuestEndProven)

    def test_cleanup_refuses_a_changed_guest_account_without_ssh(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'changed-account-fixture')
        changed = replace(settings, guest_user='replacement-account')
        with patch.object(subprocess, 'run') as ssh:
            proof = mac_linux.prove_guest_end(changed, armed)
        assert isinstance(proof, mac_linux.GuestEndPending), proof
        self.assertIn(f'changed from {settings.guest_user} to replacement-account', proof.detail)
        ssh.assert_not_called()
        self.assertIsInstance(mac_linux.prove_guest_end(settings, armed), mac_linux.GuestEndProven)

    def test_cleanup_refuses_a_different_checkout_before_making_one(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'moved-checkout-fixture')
        self.assertEqual(armed.checkout, str((self.guest_home / '.claude').resolve()))
        original = replace(armed, checkout=str(self.root / 'original-checkout'))
        proof = mac_linux.prove_guest_end(settings, original)
        assert isinstance(proof, mac_linux.GuestEndPending), proof
        self.assertIn('differs from the armed checkout', proof.detail)
        self.assertFalse((self.root / 'original-checkout').exists())
        cancel = self.run_lock().parent / (
            f'{mac_linux.CANCEL_RECORD_PREFIX}.{armed.boot_id}.{armed.deadline_ns}.{armed.attempt}')
        self.assertFalse(cancel.exists())
        self.assertIsInstance(mac_linux.prove_guest_end(settings, armed), mac_linux.GuestEndProven)

    def test_cleanup_removes_the_bootstrap_under_the_run_lock(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'stopped-runner-fixture')
        lock = self.run_lock()
        runner = lock.parent / 'mac-linux' / mac_linux.RUNNER_PATH
        runner.parent.mkdir(parents=True)
        runner.touch()

        with lock.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with patch.object(mac_linux, 'GUEST_KILL_GRACE_SECONDS', 0):
                proof = mac_linux.prove_guest_end(settings, armed)
            self.assertIsInstance(proof, mac_linux.GuestEndPending)
            self.assertTrue(runner.is_file())

        self.assertIsInstance(mac_linux.prove_guest_end(settings, armed), mac_linux.GuestEndProven)
        self.assertFalse((lock.parent / 'mac-linux').exists())

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
        arm_guest = mac_linux.arm_guest

        @contextmanager
        def reservation(*_args: object) -> Generator[claim_state.ClaimIdentity]:
            try:
                yield FIXTURE_CLAIM
            finally:
                self.assertEqual([type(proof) for proof in proofs],
                                 [mac_linux.GuestEndAnsweredPending, mac_linux.GuestEndAnsweredPending,
                                  mac_linux.GuestEndProven])

        lock = self.run_lock()
        lock.parent.mkdir(parents=True)
        with lock.open('w') as held:
            def arm(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
                armed = arm_guest(settings, attempt)
                fcntl.flock(held, fcntl.LOCK_EX)
                return armed

            def prove(settings: mac_linux.GuestSettings,
                      armed: mac_linux.ArmedGuestAttempt, *,
                      proof_deadline: float) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
                proof = prove_guest_end(settings, armed, proof_deadline=proof_deadline)
                proofs.append(proof)
                if len(proofs) == 2:
                    fcntl.flock(held, fcntl.LOCK_UN)
                return proof

            with (patch.object(mac_linux, 'mac_linux_reservation', reservation),
                  patch.object(mac_linux, 'arm_guest', arm),
                  patch.object(mac_linux, 'prove_guest_end', prove),
                  patch.object(mac_linux, 'sleep'),
                  patch.object(mac_linux, 'GUEST_KILL_GRACE_SECONDS', 0)):
                result = self.route()
        self.assertIn('Guest cleanup: ended', self.not_started(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup', 'cleanup', 'cleanup'])

    def test_full_stderr_pipe_does_not_extend_the_proof_wait(self) -> None:
        end_wait = mac_linux.GuestEndWait

        def short_proof_wait(settings: mac_linux.GuestSettings,
                             armed: mac_linux.ArmedGuestAttempt) -> mac_linux.GuestEndWait:
            # Give the stream time to start. Limit only the proof wait.
            return end_wait(replace(settings, unreachable_wait_seconds=1), armed)

        started: list[float] = []
        reader, writer = os.pipe()
        with os.fdopen(reader, 'rb') as blocked_reader, os.fdopen(writer, 'w') as blocked_stderr:
            os.set_blocking(writer, False)
            for chunk in (b'x' * 4096, b'x'):
                with suppress(BlockingIOError):
                    while True:
                        _ = os.write(writer, chunk)
            os.set_blocking(writer, True)
            # Drain the pipe only if the route stalls.
            guard = Timer(2.5, os.read, args=(blocked_reader.fileno(), 65536))

            def pending(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                        proof_deadline: float) -> mac_linux.GuestEndPending:
                self.assertLessEqual(proof_deadline - time.monotonic(), 1)
                if not started:
                    started.append(time.monotonic())
                    guard.start()
                return mac_linux.GuestEndPending('fixture cleanup pending')

            try:
                with (patch.object(mac_linux, 'GuestEndWait', side_effect=short_proof_wait),
                      patch.object(mac_linux, 'prove_guest_end', pending), redirect_stderr(blocked_stderr),
                      self.assertRaisesRegex(mac_linux.GuestEndUnproven, 'guest unreachable: fixture-guest')):
                    _ = self.route('lost-confirmed-reply')
                returned = time.monotonic()
            finally:
                if started:
                    guard.cancel()
                    guard.join()
        first_probe, = started
        self.assertLess(returned - first_probe, 1.75)

    def test_local_read_past_the_proof_deadline_starts_no_cleanup_ssh(self) -> None:
        configuration = self.registry
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = configuration.write_text(configuration.read_text().replace(f'{key}=20', f'{key}=1'))
        helper = Path(process_group.__file__)
        read_text = Path.read_text
        now = 0.0
        started = time.monotonic()

        def read_with_delay(path: Path, **options: str | None) -> str:
            nonlocal now
            contents = read_text(path, **options)
            if path == helper:
                now += 1.25
            return contents

        with (patch.object(Path, 'read_text', read_with_delay),
              patch.object(mac_linux, 'monotonic', lambda: started + now),
              self.assertRaisesRegex(mac_linux.GuestEndUnproven, 'guest unreachable: fixture-guest')):
            _ = self.route('lost-confirmed-reply')
        # The cleanup command reads the helper after the run command. The cleanup read passes the deadline.
        self.assertEqual(now, 2.5)
        self.assert_guest_clean('probe', 'arm', 'run')

    def test_failed_guest_verdict_survives_an_answered_cleanup_refusal(self) -> None:
        now = 50.0
        real_stream = mac_linux.stream_guest_bundle

        def disconnect(repo: Path, command: list[str], bootstrap: bytes, timeout: int, link: int,
                       armed: mac_linux.ArmedGuestAttempt, dispatch: mac_linux.GuestGateDispatch, *,
                       connect_deadline: float) -> subprocess.CompletedProcess[bytes]:
            stream = real_stream(repo, command, bootstrap, timeout, link, armed, dispatch,
                                 connect_deadline=connect_deadline)
            self.assertEqual(stream.returncode, 0)
            return subprocess.CompletedProcess(command, 255, stream.stdout,
                stream.stderr + b'client_loop: send disconnect: Broken pipe\n')

        def refuse(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                   proof_deadline: float) -> mac_linux.GuestEndPending:
            nonlocal now
            now = proof_deadline
            return mac_linux.GuestEndAnsweredPending('fixture cleanup refusal')

        with (patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=disconnect),
              patch.object(mac_linux, 'prove_guest_end', side_effect=refuse) as probe):
            result = self.route('red')
        probe.assert_called_once()
        assert isinstance(result, GateRun), result
        self.assertFalse(result.passed)
        self.assertEqual([job.returncode for job in result.jobs], [1, 1])
        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        last = cast(dict[str, object], json.loads(journal.read_bytes().splitlines()[-1]))
        self.assertEqual(last['result'], 'unproven')
        self.assertNotIn('outage', last)

    def test_first_inner_refusal_sets_the_cleanup_deadline(self) -> None:
        now = 50.0
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=600)
        arrivals: list[float] = []
        deadlines: list[float] = []

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        def refuse(*_args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            advance(2 if not arrivals else 1)
            arrivals.append(now)
            return subprocess.CompletedProcess([], mac_linux.INNER_CONNECT_STATUS, b'',
                (mac_linux.INNER_CONNECT_MARKER + '\n'
                 + 'ssh: connect to host fixture-guest port 22: Connection refused\n').encode())

        def cleanup(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                    proof_deadline: float) -> mac_linux.GuestEndConnectPending:
            deadlines.append(proof_deadline)
            advance(proof_deadline - now)
            return mac_linux.GuestEndConnectPending('fixture cleanup refusal')

        with (patch.object(mac_linux, 'read_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'time', lambda: 1000 + now),
              patch.object(mac_linux, 'sleep', advance),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=refuse),
              patch.object(mac_linux, 'prove_guest_end', side_effect=cleanup)):
            result = self.route()
        self.assertIn('did not confirm the run ended within 600 seconds', self.not_started(result))
        self.assertEqual(arrivals, [52.0, 54.0, 56.0])
        self.assertEqual(deadlines, [652.0])

    def test_first_late_cleanup_miss_extends_the_outage_deadline(self) -> None:
        now = 0.0
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=600)
        probes: list[tuple[float, float]] = []

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        def no_verdict(*args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            armed, dispatch = args[-2:]
            assert isinstance(armed, mac_linux.ArmedGuestAttempt)
            assert isinstance(dispatch, mac_linux.GuestGateDispatch)
            dispatch.started = armed
            return subprocess.CompletedProcess([], 0, b'', b'')

        def cleanup(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                    proof_deadline: float) -> mac_linux.GuestEndPending:
            nonlocal now
            probes.append((now, proof_deadline))
            if len(probes) == 1:
                now = 590
                return mac_linux.GuestEndAnsweredPending('fixture guest answered')
            now = proof_deadline
            return mac_linux.GuestEndTransportPending('fixture guest did not answer')

        with (patch.object(mac_linux, 'read_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'time', lambda: 1000 + now),
              patch.object(mac_linux, 'sleep', advance),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=no_verdict),
              patch.object(mac_linux, 'prove_guest_end', side_effect=cleanup),
              self.assertRaises(mac_linux.GuestOutage) as lost):
            _ = self.route()
        self.assertEqual(probes, [(0.0, 600.0), (598.0, 600.0), (608.0, 1198.0)])
        self.assertEqual((now, lost.exception.outage_since), (1198.0, 1598.0))

    def test_ssh_configuration_error_precedes_the_first_refusal(self) -> None:
        now = 50.0
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=600)
        refusal = mac_linux.INNER_CONNECT_MARKER + '\nssh: connect to host guest port 22: Connection refused\n'
        config_error = '/fixture/ssh_config: line 2: Bad configuration option: bad'
        deadlines: list[float] = []

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        def send(*args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            if now == 50:
                advance(2)
                return subprocess.CompletedProcess([], mac_linux.INNER_CONNECT_STATUS, b'', refusal.encode())
            armed = args[-2]
            assert isinstance(armed, mac_linux.ArmedGuestAttempt)
            advance(60 - now)
            diagnostic = mac_linux.run_start_line(armed.attempt) + '\n' + config_error + '\n'
            return subprocess.CompletedProcess([], 255, b'', diagnostic.encode())

        def cleanup(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                    proof_deadline: float) -> mac_linux.GuestEndTransportPending:
            deadlines.append(proof_deadline)
            advance(proof_deadline - now)
            return mac_linux.GuestEndTransportPending('fixture cleanup transport failure')

        with (patch.object(mac_linux, 'read_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'time', lambda: 1000 + now),
              patch.object(mac_linux, 'sleep', advance),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=send),
              patch.object(mac_linux, 'prove_guest_end', side_effect=cleanup),
              self.assertRaises(mac_linux.GuestEndUnproven) as failure):
            _ = self.route()
        self.assertIs(type(failure.exception), mac_linux.GuestEndUnproven)
        self.assertIn(config_error, ' '.join(failure.exception.__notes__))
        self.assertEqual(deadlines, [660.0])
        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        self.assertNotIn(b'"outage"', journal.read_bytes())

    def test_broken_pipe_before_acceptance_keeps_the_first_refusal_deadline(self) -> None:
        now = 50.0
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=600)
        deadlines: list[float] = []

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        def send(*_args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            if now == 50:
                advance(2)
                diagnostic = mac_linux.INNER_CONNECT_MARKER + '\nssh: connect to host guest port 22: Connection refused\n'
                return subprocess.CompletedProcess([], mac_linux.INNER_CONNECT_STATUS, b'', diagnostic.encode())
            advance(60 - now)
            return subprocess.CompletedProcess([], 255, b'', b'client_loop: send disconnect: Broken pipe\n')

        def cleanup(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                    proof_deadline: float) -> mac_linux.GuestEndTransportPending:
            deadlines.append(proof_deadline)
            advance(proof_deadline - now)
            return mac_linux.GuestEndTransportPending('fixture cleanup transport failure')

        with (patch.object(mac_linux, 'read_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'time', lambda: 1000 + now),
              patch.object(mac_linux, 'sleep', advance),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=send),
              patch.object(mac_linux, 'prove_guest_end', side_effect=cleanup)):
            result = self.route()
        self.assertIn('did not confirm the run ended within 600 seconds', self.not_started(result))
        self.assertEqual(deadlines, [652.0])

    def test_cleanup_timeout_keeps_the_ssh_configuration_error(self) -> None:
        now = time.monotonic() + 1
        settings = mac_linux.read_settings(conf.read(self.registry))
        real_ssh = mac_linux.run_guest_ssh
        config_error = b'/fixture/ssh_config: line 2: Bad configuration option: bad\n'
        broken_pipe = b'client_loop: send disconnect: Broken pipe\n'

        def disconnect(*args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            armed, dispatch = args[-2:]
            assert isinstance(armed, mac_linux.ArmedGuestAttempt)
            assert isinstance(dispatch, mac_linux.GuestGateDispatch)
            dispatch.started = armed
            return subprocess.CompletedProcess([], 255, b'', broken_pipe)

        def timeout_cleanup(command: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
            nonlocal now
            if 'prove_end ||' not in command[-1]:
                return real_ssh(command, timeout)
            now += timeout
            raise subprocess.TimeoutExpired(command, timeout, stderr=broken_pipe + config_error)

        with (patch.object(mac_linux, 'new_run_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=disconnect),
              patch.object(mac_linux, 'run_guest_ssh', side_effect=timeout_cleanup),
              self.assertRaises(mac_linux.GuestEndUnproven) as failure):
            _ = self.route()
        self.assertIs(type(failure.exception), mac_linux.GuestEndUnproven)
        self.assertIn(config_error.decode().strip(), ' '.join(failure.exception.__notes__))
        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        self.assertNotIn(b'"outage"', journal.read_bytes())

    def test_bad_reply_and_local_launch_failure_do_not_become_outages(self) -> None:
        now = 50.0

        def cleanup(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                    proof_deadline: float) -> mac_linux.GuestEndTransportPending:
            nonlocal now
            now = proof_deadline
            return mac_linux.GuestEndTransportPending('fixture cleanup transport failure')

        for attempt, fault in enumerate(('malformed', 'local launch'), start=1):
            self.placed = replace(self.placed, attempt=f'{attempt:032x}')

            def send(*args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
                if fault == 'local launch':
                    raise OSError('fixture local launch failed')
                armed, dispatch = args[-2:]
                assert isinstance(armed, mac_linux.ArmedGuestAttempt)
                assert isinstance(dispatch, mac_linux.GuestGateDispatch)
                dispatch.started = armed
                return subprocess.CompletedProcess([], 0, b'{broken json}\n10\n', b'')

            with (self.subTest(fault=fault), patch.object(mac_linux, 'monotonic', lambda: now),
                  patch.object(mac_linux, 'stream_guest_bundle', side_effect=send),
                  patch.object(mac_linux, 'prove_guest_end', side_effect=cleanup),
                  self.assertRaises(mac_linux.GuestEndUnproven) as failure):
                _ = self.route()
            self.assertIs(type(failure.exception), mac_linux.GuestEndUnproven)
            self.assertIn('fixture local launch failed' if fault == 'local launch' else 'the guest reply is unusable',
                          ' '.join(failure.exception.__notes__))

        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        rows = [cast(dict[str, object], json.loads(line)) for line in journal.read_bytes().splitlines()]
        self.assertEqual([row['result'] for row in rows],
                         ['awaiting arm', 'unproven', 'awaiting arm', 'unproven'])
        self.assertTrue(all('outage' not in row for row in rows))

    def test_keepalive_and_end_proof_share_the_guest_outage_budget(self) -> None:
        clock = 50.0
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=600)

        def silence(*args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal clock
            armed, dispatch = args[-2:]
            assert isinstance(armed, mac_linux.ArmedGuestAttempt)
            assert isinstance(dispatch, mac_linux.GuestGateDispatch)
            dispatch.started = armed
            clock += 600
            return subprocess.CompletedProcess([], 255, b'', b'Timeout, server fixture-guest not responding.\n')

        with (patch.object(mac_linux, 'read_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: clock),
              patch.object(mac_linux, 'time', lambda: 1000 + clock),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=silence),
              patch.object(mac_linux, 'prove_guest_end') as proof,
              self.assertRaises(mac_linux.GuestOutage) as lost):
            _ = self.route()
        self.assertEqual((clock, lost.exception.outage_since), (650.0, 1050.0))
        proof.assert_not_called()

        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        last = cast(dict[str, object], json.loads(journal.read_bytes().splitlines()[-1]))
        self.assertEqual(last['result'], 'unproven')
        self.assertEqual((last['outage'], last['outage_since']), ('GUEST_END_UNPROVEN', 1050.0))
        paths = claim_state.state_paths('mac')
        held, = claim_state.read_claims(paths)
        fence, = claim_state.read_lost_mac_fences(paths)
        self.assertEqual(held.record['claim_id'], last['claim'])
        self.assertEqual(held.record, fence.record)
        self.assertEqual(lost.exception.saved_claim, claim_state.claim_identity(held.record))
        self.assertFalse(claim_state.claim_owner_ended(held.record))

        with (patch.object(mac_test, 'start_claim_watcher'),
              self.assertRaisesRegex(mac_run.MacRunRefused, 'is busy')):
            with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                self.fail('A second claim replaced the live owner')
        self.assertEqual(claim_state.read_claims(paths), (held,))
        self.assertEqual(claim_state.read_lost_mac_fences(paths), (fence,))

        original_journal = journal.read_bytes()
        with patch.object(process_start, 'process_start', return_value=process_start.Started('next-owner')):
            with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED) as new_claim:
                replacement, = claim_state.read_claims(paths)
                self.assertEqual(claim_state.claim_identity(replacement.record), new_claim)
                self.assertNotEqual(new_claim, claim_state.claim_identity(held.record))
                self.assertEqual(claim_state.read_lost_mac_fences(paths), ())
        self.assertEqual(claim_state.read_claims(paths), ())
        self.assertEqual(journal.read_bytes(), original_journal)

    def test_guest_failed_fence_save_prevents_outage_publication(self) -> None:
        now = 0.0
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=600)

        def silence(*args: object, **_options: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal now
            armed, dispatch = args[-2:]
            assert isinstance(armed, mac_linux.ArmedGuestAttempt)
            assert isinstance(dispatch, mac_linux.GuestGateDispatch)
            dispatch.started = armed
            now += 600
            return subprocess.CompletedProcess([], 255, b'', b'Timeout, server fixture-guest not responding.\n')

        with (patch.object(mac_linux, 'read_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=silence),
              patch.object(claim_state, 'save_lost_mac_fence', return_value=False) as save,
              self.assertRaises(mac_linux.GuestEndSaveFailed) as failure):
            _ = self.route()
        save.assert_called_once()
        self.assertIn('Cannot save the exact lost Mac Linux claim', failure.exception.__notes__)
        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        last = cast(dict[str, object], json.loads(journal.read_bytes().splitlines()[-1]))
        self.assertEqual(last['result'], 'unproven')
        self.assertNotIn('outage', last)
        paths = claim_state.state_paths('mac')
        held, = claim_state.read_claims(paths)
        self.assertEqual(held.record['claim_id'], last['claim'])
        self.assertEqual(claim_state.read_lost_mac_fences(paths), ())

    def fail_guest_end_record_sync(self, *, full_bundle_retry: bool = False) -> None:
        clock = time.monotonic() + 1
        settings = mac_linux.read_settings(conf.read(self.registry))
        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'
        fsync = os.fsync
        stream = mac_linux.stream_guest_bundle

        def silence(repo: Path, arguments: list[str], bootstrap: bytes, timeout: int, link: int,
                    armed: mac_linux.ArmedGuestAttempt, dispatch: mac_linux.GuestGateDispatch, *,
                    connect_deadline: float) -> subprocess.CompletedProcess[bytes]:
            nonlocal clock
            if full_bundle_retry and not armed.attempt.endswith(mac_linux.FULL_BUNDLE_RETRY_SUFFIX):
                return stream(repo, arguments, bootstrap, timeout, link, armed, dispatch,
                              connect_deadline=connect_deadline)
            dispatch.started = armed
            clock += settings.unreachable_wait_seconds
            return subprocess.CompletedProcess([], 255, b'', b'Timeout, server fixture-guest not responding.\n')

        def fail_end_sync(fd: int) -> None:
            if journal.exists() and os.fstat(fd).st_ino == journal.stat().st_ino:
                rows = journal.read_bytes().splitlines()
                if rows and b'"result": "unproven"' in rows[-1]:
                    raise OSError('fixture guest end fsync failed')
            fsync(fd)

        with (patch.object(mac_linux, 'new_run_settings', return_value=settings),
              patch.object(mac_linux, 'monotonic', lambda: clock),
              patch.object(mac_linux, 'stream_guest_bundle', side_effect=silence),
              patch.object(os, 'fsync', side_effect=fail_end_sync),
              self.assertRaises(mac_linux.GuestEndSaveFailed) as failure):
            _ = self.route()
        self.assertIn('Cannot save the Mac Linux guest end: fixture guest end fsync failed', failure.exception.__notes__)

    def test_guest_failed_end_record_keeps_the_claim_and_fence_after_owner_exit(self) -> None:
        self.fail_guest_end_record_sync()
        paths = claim_state.state_paths('mac')
        held, = claim_state.read_claims(paths)
        fence, = claim_state.read_lost_mac_fences(paths)
        self.assertEqual(fence.record, held.record)
        owner = claim_state.ClaimProcessOwner(held.record['pid'], held.record['proc_start'])
        identity = claim_state.claim_identity(held.record)
        with (patch.object(process_start, 'process_start', return_value=process_start.Gone()),
              claim_state.locked(paths.lock)):
            self.assertTrue(claim_state.claim_owner_ended(held.record))
            claim_state.recover_lost_mac_retirement(paths)
            self.assertEqual(mac_test.live_claims(paths), (held,))
        self.assertFalse(claim_state.retire_lost_mac_claim(paths, identity, owner))
        self.assertEqual(claim_state.read_claims(paths), (held,))
        self.assertEqual(claim_state.read_lost_mac_fences(paths), (fence,))

    def test_later_exact_guest_end_proof_clears_a_pending_fence_for_the_next_claim(self) -> None:
        paths = claim_state.state_paths('mac')
        journal = self.root / 'target-state/mac-linux/end-proofs.jsonl'

        def assert_busy() -> None:
            with self.assertRaisesRegex(mac_run.MacRunRefused, 'is busy'):
                with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                    self.fail('The next claim succeeded before the exact proof arrived.')

        for attempt, legacy in ((16, False), (17, True)):
            with self.subTest(legacy=legacy):
                self.placed = replace(self.placed, attempt=f'{attempt:032x}')
                self.fail_guest_end_record_sync()
                held, = claim_state.read_claims(paths)
                identity = claim_state.claim_identity(held.record)
                published = claim_state.lost_mac_fence_path(paths, identity, held.path.name)
                published_before_proof = published.exists()
                if legacy:
                    _ = published.write_text(json.dumps(held.record) + '\n')
                    _ = published.with_suffix('.guest-end-pending').write_text('')
                fence, = claim_state.read_lost_mac_fences(paths)
                key = execution_identity.attempt_key(self.placed.episode, self.placed.attempt)
                wrong_attempt = execution_identity.attempt_key(self.placed.episode, f'{attempt + 1000:032x}')
                with (patch.object(process_start, 'process_start', return_value=process_start.Started('next-owner')),
                      patch.object(mac_test, 'start_claim_watcher')):
                    assert_busy()
                    with journal.open('a') as stream:
                        self.assertNotEqual(wrong_attempt, key)
                        _ = stream.write(json.dumps({'attempt': wrong_attempt, 'claim': identity.claim_id,
                            'result': 'proven', 'detail': 'Unrelated proof', 'written_at': time.time()}) + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                    assert_busy()
                    with patch.object(mac_linux, 'prove_kept_guest_end', return_value=mac_linux.GuestEndProven()) as prove:
                        mac_linux.reconcile_guest_end(self.placed.episode, attempt)
                    prove.assert_called_once()
                    self.assertEqual(claim_state.read_claims(paths), (held,))
                    self.assertEqual(claim_state.read_lost_mac_fences(paths), (fence,))
                    with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED) as replacement:
                        self.assertNotEqual(replacement, identity)
                        current, = claim_state.read_claims(paths)
                        self.assertEqual(claim_state.claim_identity(current.record), replacement)
                        self.assertEqual(claim_state.read_lost_mac_fences(paths), ())
                self.assertFalse(published_before_proof)

    def test_legacy_full_bundle_fence_requires_an_exact_retry_link_and_end_proof(self) -> None:
        self.placed = replace(self.placed, attempt=f'{18:032x}')
        (self.repo / 'scripts/fixture/test_added.py').touch()
        with patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': self.git('rev-parse', 'origin/main')}):
            self.fail_guest_end_record_sync(full_bundle_retry=True)
        paths = claim_state.state_paths('mac')
        held, = claim_state.read_claims(paths)
        local = claim_state.claim_identity(held.record)
        published = claim_state.lost_mac_fence_path(paths, local, held.path.name)
        _ = published.write_text(json.dumps(held.record) + '\n')
        pending = published.with_suffix('.guest-end-pending')
        _ = pending.write_text('')

        journal = target_config.mac_linux_end_proof_journal()
        original = journal.read_bytes()
        launch, retry = [cast(dict[str, object], json.loads(line)) for line in original.splitlines()]
        self.assertEqual(launch['result'], 'awaiting arm')
        self.assertEqual(retry['result'], 'unproven')
        self.assertEqual(launch['claim'], retry['claim'])
        self.assertNotEqual(launch['claim'], local.claim_id)
        proof = {**retry, 'result': 'proven'}
        _ = proof.pop('armed')
        wrong_owner = {**launch, 'holder': {'pid': held.record['pid'], 'start': 'unrelated-owner'}}
        wrong_retry = {**retry, 'armed': {'attempt': claim_state.placed_guest_attempt(str(launch['attempt']))}}
        other_attempt = execution_identity.attempt_key(self.placed.episode, f'{19:032x}')
        other_retry = {**retry, 'attempt': other_attempt,
                       'armed': {'attempt': claim_state.placed_guest_attempt(other_attempt)
                                 + mac_linux.FULL_BUNDLE_RETRY_SUFFIX}}

        with (patch.object(process_start, 'process_start', return_value=process_start.Started('next-owner')),
              patch.object(mac_test, 'start_claim_watcher')):
            for rows in ((retry, proof), (wrong_owner, retry, proof), (launch, wrong_retry, proof),
                         (launch, proof, retry),
                         (launch, retry, {**proof, 'attempt': other_attempt}),
                         (launch, retry, proof, {**launch, 'attempt': other_attempt}, other_retry)):
                with self.subTest(rows=rows):
                    _ = journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                    with self.assertRaisesRegex(mac_run.MacRunRefused, 'is busy'):
                        with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED):
                            self.fail('The claim succeeded without an exact retry link and proof.')

            _ = journal.write_bytes(original)
            with patch.object(mac_linux, 'prove_kept_guest_end', return_value=mac_linux.GuestEndProven()) as prove:
                mac_linux.reconcile_guest_end(self.placed.episode, 18)
            prove.assert_called_once()
            with mac_run.held(self.repo, 'mac', SuiteMode.GATE, GATE_JOB_NOT_RECORDED) as replacement:
                self.assertNotEqual(replacement, local)
                self.assertEqual(claim_state.read_lost_mac_fences(paths), ())
            self.assertFalse(published.exists())
            self.assertFalse(pending.exists())

    def test_proof_received_at_the_deadline_refuses(self) -> None:
        started = time.monotonic() + 1
        now = started

        def late_proof(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                       proof_deadline: float) -> mac_linux.GuestEndProven:
            nonlocal now
            self.assertEqual(proof_deadline, started + 20)
            now = proof_deadline
            return mac_linux.GuestEndProven()

        with (patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'prove_guest_end', late_proof),
              self.assertRaisesRegex(mac_linux.GuestEndUnproven, 'guest unreachable: fixture-guest')):
            _ = self.route('lost-confirmed-reply')

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

    def test_each_guest_ssh_hop_waits_the_unreachable_wait_for_a_keepalive_answer(self) -> None:
        # A busy Mac can be silent for more than 30 s, so each hop waits the unreachable wait of 600 s.
        settings = mac_linux.GuestSettings(('fixture-mac', 'fixture-guest'), 'gate-runner', 4, 1800, 600)
        command = settings.command('true')
        alive = ['-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=40']
        mac_hop = list(command[:-2])
        inner = next(line for line in command[-1].splitlines() if line.startswith('LC_ALL=C ssh -E'))
        guest_hop = shlex.split(inner.split(" fixture-guest ", 1)[0])
        guest_hop = guest_hop[:guest_hop.index('-l')]
        self.assertEqual(command[-2], 'fixture-mac')
        for hop in (mac_hop, guest_hop):
            # ssh uses the first value of an option, so each hop names each keepalive option once.
            self.assertEqual(hop[-4:], alive)
            self.assertEqual([word for word in hop if word.startswith('ServerAlive')], alive[1::2])

    def test_kept_guest_end_banner_timeout_starts_backoff(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.ArmedGuestAttempt('kept-attempt', 'f' * 32, 'boot-id', 10, settings.guest_user, '/fixture')
        diagnostic = 'Connection timed out during banner exchange\nConnection to fixture-mac port 22 timed out'
        failed = subprocess.CompletedProcess(['ssh'], 255, '', diagnostic + '\n')
        with patch.object(mac_linux, 'run_guest_ssh', return_value=failed) as ssh:
            self.assertEqual(mac_linux.prove_kept_guest_end(armed), mac_linux.GuestEndPending(diagnostic))
            self.assertIs(eligibility.read_backoff(time.time(), mac_linux.TARGET), eligibility.ACTIVE_BACKOFF)
            self.assertEqual(mac_linux.prove_kept_guest_end(armed),
                             mac_linux.GuestEndPending('guest unreachable during retry backoff'))
            ssh.assert_called_once()

    def test_kept_guest_end_proof_refuses_unreadable_settings(self) -> None:
        settings_user = mac_linux.read_settings(conf.read(self.registry)).guest_user
        settings = self.registry.with_name('mac_linux.conf')
        settings.unlink()
        settings.mkdir()
        armed = mac_linux.ArmedGuestAttempt('kept-attempt', 'f' * 32, 'boot-id', 10, settings_user, '/fixture/checkout')
        proof = mac_linux.prove_kept_guest_end(armed)
        assert isinstance(proof, mac_linux.GuestEndPending), proof
        self.assertIn(str(settings), proof.detail)
        self.assertIn('Is a directory', proof.detail)
        self.assert_guest_clean()

    def test_kept_guest_end_proof_uses_the_route_settings_and_exact_attempt(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        settings_user = settings.guest_user
        armed = mac_linux.ArmedGuestAttempt('kept-attempt', 'f' * 32, 'boot-id', 10, settings_user, '/fixture/checkout')
        command = settings.command(mac_linux.cleanup_shell(settings, armed))
        for status, stdout, stderr, expected in (
            (0, 'ended\n', '', mac_linux.GuestEndProven()),
            (76, '', 'The guest run lock is still held.\n',
             mac_linux.GuestEndAnsweredPending('The guest run lock is still held.')),
        ):
            with (self.subTest(status=status),
                  patch.dict(os.environ, {'BUILDLOG_PLACEMENT_SIDECAR': '/fixture/reservation.cgroup'}),
                  patch.object(subprocess, 'run',
                               return_value=subprocess.CompletedProcess(command, status, stdout, stderr)) as ssh):
                environment = {key: value for key, value in os.environ.items() if key != 'BUILDLOG_PLACEMENT_SIDECAR'}
                environment['LC_ALL'] = 'C'
                self.assertEqual(mac_linux.prove_kept_guest_end(armed), expected)
                ssh.assert_called_once_with(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                            errors='replace', timeout=settings.unreachable_wait_seconds,
                                            check=False, env=environment)

    def test_keyboard_interrupt_proves_the_end_before_release(self) -> None:
        def interrupt_source(process: subprocess.Popen[bytes], bootstrap: bytes, _timeout: int,
                             armed: mac_linux.ArmedGuestAttempt, _dispatch: mac_linux.GuestGateDispatch,
                             _reports: ExitStack, *, connect_deadline: float,
                             ) -> tuple[bytes, bytes]:
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            self.assertGreater(connect_deadline, time.monotonic())
            self.assertTrue(select.select([process.stdout], [], [], 6)[0])
            self.assertEqual(process.stdout.readline(), f'{mac_linux.INNER_CONNECT_READY}\n'.encode())
            _ = process.stdin.write(b'8\n')
            process.stdin.flush()
            self.assertTrue(select.select([process.stderr], [], [], 6)[0])
            self.assertEqual(process.stderr.readline(), f'{mac_linux.GUEST_ACCEPTED} {armed.attempt}\n'.encode())
            _ = process.stdin.write(b'\n' + bootstrap)
            process.stdin.close()
            self.assertTrue(select.select([process.stderr], [], [], 6)[0])
            self.assertEqual(process.stderr.readline(), b'fixture local SSH is still active\n')
            raise KeyboardInterrupt

        @contextmanager
        def reservation(*_args: object) -> Generator[claim_state.ClaimIdentity]:
            try:
                yield FIXTURE_CLAIM
            finally:
                self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])
                self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)

        with (patch.object(mac_linux, 'communicate_guest_bundle', interrupt_source),
              patch.object(mac_linux, 'mac_linux_reservation', reservation),
              self.assertRaises(KeyboardInterrupt)):
            _ = self.route('interrupted-before-copy')

    def test_interrupt_at_cleanup_entry_keeps_the_fixture_reservation_until_proof(self) -> None:
        prove_guest_end = mac_linux.prove_guest_end
        first_interrupt = KeyboardInterrupt('fixture interruption at cleanup entry')
        events: list[str] = []
        lock_statuses: list[int] = []

        @contextmanager
        def reservation(*_args: object) -> Generator[claim_state.ClaimIdentity]:
            events.append('reserved')
            try:
                yield FIXTURE_CLAIM
            finally:
                events.append('released')

        def prove(settings: mac_linux.GuestSettings,
                  armed: mac_linux.ArmedGuestAttempt, *,
                  proof_deadline: float) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
            if events == ['reserved']:
                lock_statuses.append(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']))
                events.append('interrupted')
                raise first_interrupt
            proof = prove_guest_end(settings, armed, proof_deadline=proof_deadline)
            if isinstance(proof, mac_linux.GuestEndProven):
                lock_statuses.append(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']))
                events.append('ended')
            return proof

        def interrupt_retry(_seconds: float) -> None:
            if 'retry interrupted' not in events:
                events.append('retry interrupted')
                raise SystemExit('fixture interruption during retry')

        with (patch.object(mac_linux, 'mac_linux_reservation', reservation),
              patch.object(mac_linux, 'prove_guest_end', prove),
              patch.object(mac_linux, 'sleep', interrupt_retry),
              self.assertRaises(KeyboardInterrupt) as raised):
            _ = self.route('disconnect-before-copy')
        self.assertIs(raised.exception, first_interrupt)
        self.assertEqual(events, ['reserved', 'interrupted', 'retry interrupted', 'ended', 'released'])
        self.assertEqual(lock_statuses, [1, 0])
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

    def test_interrupt_at_retry_jump_keeps_the_fixture_reservation_until_proof(self) -> None:
        stream_guest_bundle = mac_linux.stream_guest_bundle
        interruption = KeyboardInterrupt('fixture interruption at the retry jump')
        events: list[str] = []
        lock_statuses: list[int] = []

        @contextmanager
        def reservation(*_args: object) -> Generator[claim_state.ClaimIdentity]:
            events.append('reserved')
            try:
                yield FIXTURE_CLAIM
            finally:
                lock_statuses.append(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']))
                events.append('released')

        def fail_stream(repo: Path, command: list[str], bootstrap: bytes, timeout: int,
                        link: int, armed: mac_linux.ArmedGuestAttempt,
                        dispatch: mac_linux.GuestGateDispatch, *,
                        connect_deadline: float) -> subprocess.CompletedProcess[bytes]:
            stream = stream_guest_bundle(repo, command, bootstrap, timeout, link, armed, dispatch,
                                         connect_deadline=connect_deadline)
            self.assertEqual(stream.returncode, 255)
            events.append('SSH failed')
            raise mac_linux.GuestUnavailable('fixture SSH failed')

        def interrupt_jump(_code: CodeType, origin: int, destination: int) -> None:
            if destination < origin and events == ['reserved', 'SSH failed']:
                lock_statuses.append(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']))
                events.append('interrupted')
                raise interruption

        monitoring = sys.monitoring
        run_code = mac_linux.run_remote.__code__
        tool_id = next(index for index in range(6) if monitoring.get_tool(index) is None)
        monitoring.use_tool_id(tool_id, 'guest retry jump fixture')
        try:
            _ = monitoring.register_callback(tool_id, monitoring.events.JUMP, interrupt_jump)
            monitoring.set_local_events(tool_id, run_code, monitoring.events.JUMP)
            with (patch.object(mac_linux, 'mac_linux_reservation', reservation),
                  patch.object(mac_linux, 'stream_guest_bundle', fail_stream),
                  self.assertRaises(KeyboardInterrupt) as raised):
                _ = self.route('disconnect-before-copy')
        finally:
            monitoring.set_local_events(tool_id, run_code, 0)
            _ = monitoring.register_callback(tool_id, monitoring.events.JUMP, None)
            monitoring.free_tool_id(tool_id)
        self.assertIs(raised.exception, interruption)
        self.assertEqual(events, ['reserved', 'SSH failed', 'interrupted', 'released'])
        self.assertEqual(lock_statuses, [1, 0])
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

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
        with (patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '3', 'FIXTURE_TRANSFER_SECONDS': '4'}),
              patch.object(mac_linux, 'TRANSFER_ALLOWANCE_SECONDS', 4)):
            with self.assertRaisesRegex(GateResultUnknown, r'SSH or source stream exited (9|137):') as result:
                _ = self.route('hang')
        self.assertIn('Guest cleanup: ended', str(result.exception))
        self.assertTrue((self.root / 'run.group').is_file())
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

    def test_guest_returns_its_time_limit_result(self) -> None:
        configuration = self.registry.with_name('mac_linux.conf')
        _ = configuration.write_text(configuration.read_text().replace('seconds=7', 'seconds=2'))
        with patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '2'}):
            result = self.route('time-limit')
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assertFalse(result.passed)
        self.assertEqual(result.time_limit_report, 'time limit: limit 2 s')
        self.assertEqual([job.returncode for job in result.jobs], [124, 124])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_retries_one_inner_connect_failure_for_each_ssh_call(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_SSH_PROBE': 'reset', 'FIXTURE_SSH_ARM': 'refused',
                                     'FIXTURE_SSH_RUN': 'reset', 'FIXTURE_SSH_CLEANUP': 'reset'}):
            result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assertTrue(result.passed)
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN, 'first')
        self.assertEqual(len(self.consumed), 1)
        self.assert_guest_clean('probe', 'probe', 'arm', 'arm', 'run', 'run', 'cleanup', 'cleanup')

    def test_three_inner_connect_resets_refuse_the_run_with_the_attempt_count(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'reset,reset,reset'}):
            reason = self.unconfirmed(self.route())
        self.assertIn('Connection reset by peer after 3 connect attempts', reason)
        self.assertIn('Guest cleanup: ended', reason)
        self.assert_guest_clean('probe', 'arm', 'run', 'run', 'run', 'cleanup')
        self.assertFalse((self.guest_home / 'build').exists())

    def test_zero_exit_with_empty_reply_retries_only_the_exact_inner_connect_diagnostic(self) -> None:
        detail = b'ssh: connect to host 127.0.0.1 port 31022: Connection reset by peer\n'
        diagnostics = mac_linux.INNER_CONNECT_MARKER.encode() + b'\n' + detail
        for data, stderr, bundle, retries in (
                (b'', diagnostics, True, True),
                (b'\n\n', diagnostics, True, True),
                (b'\n\n', diagnostics, False, False),
                (b'\n1\n', diagnostics, True, False),
                (b'', detail, True, False),
                (b'', b'mac-linux command accepted fixture\n' + diagnostics, True, False)):
            with self.subTest(data=data, stderr=stderr, bundle=bundle):
                failed = subprocess.CompletedProcess[bytes]([], 0, data, stderr)
                success = subprocess.CompletedProcess[bytes]([], 0, b'reply\n17\n', b'')
                stream = Mock(side_effect=[failed, success])
                with patch.object(mac_linux, 'sleep') as delay:
                    result = mac_linux.retry_guest_connect(stream, time.monotonic() + 10, bundle_stream=bundle)
                self.assertIs(result, success if retries else failed)
                self.assertEqual(stream.call_count, 2 if retries else 1)
                self.assertEqual(delay.call_count, int(retries))

    def test_inner_reset_after_the_guest_started_does_not_retry(self) -> None:
        with (patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'reset,reset-after-start'}),
              self.assertRaises(GateResultUnknown) as result):
            _ = self.route()
        self.assertIn(mac_linux.RUN_START, str(result.exception))
        self.assertIn('Connection reset by peer', str(result.exception))
        self.assert_guest_clean('probe', 'arm', 'run', 'run', 'cleanup')

    def test_a_different_ssh_error_does_not_retry(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'reset,denied'}):
            reason = self.unconfirmed(self.route())
        self.assertIn('Permission denied (publickey)', reason)
        self.assert_guest_clean('probe', 'arm', 'run', 'run', 'cleanup')
        self.assertFalse((self.guest_home / 'build').exists())

    def test_the_guest_connect_deadline_can_stop_retries_before_three_attempts(self) -> None:
        settings = replace(mac_linux.read_settings(conf.read(self.registry)), unreachable_wait_seconds=1)
        with (patch.dict(os.environ, {'FIXTURE_SSH_PROBE': 'reset'}),
              patch.object(mac_linux, 'sleep') as delay,
              self.assertRaisesRegex(mac_linux.GuestUnavailable, 'after 1 connect attempt')):
            _ = mac_linux.read_guest_main(settings)
        delay.assert_not_called()
        self.assert_guest_clean('probe')

    def test_guest_stderr_after_input_before_lock_cannot_retry(self) -> None:
        for fault in ('reset-before-lock', 'forged-marker'):
            with self.subTest(fault=fault), patch.dict(os.environ, {'FIXTURE_SSH_RUN': fault}):
                _ = self.trace.write_text('')
                reason = self.unconfirmed(self.route())
                self.assertIn('SSH or source stream exited 255', reason)
                self.assertIn('Connection reset by peer', reason)
                if fault == 'forged-marker':
                    self.assertIn('The guest SSH command exited 254.', reason)
                self.assertEqual(len((self.root / 'received-before-lock').read_bytes()), 16)
                self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')
                self.assertEqual(list(self.root.glob('mac-linux-ssh.*')), [])

    def test_stream_connect_deadline_bounds_both_ssh_hops_and_the_local_wait(self) -> None:
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = self.registry.write_text(self.registry.read_text().replace(f'{key}=20', f'{key}=5'))
        with patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'slow-connect', 'FIXTURE_MAC_STREAM_DELAY': '2.1'}):
            result = self.route()
        elapsed = time.monotonic()
        mac_timeout, guest_timeout, started = (self.root / 'connect-timing').read_text().split()
        mac_seconds = int(mac_timeout.removeprefix('ConnectTimeout='))
        guest_seconds = int(guest_timeout.removeprefix('ConnectTimeout='))
        self.assertLessEqual(guest_seconds, mac_seconds - 1)
        self.assertLess(elapsed - float(started), 4)
        reason = self.unconfirmed(result)
        self.assertIn('guest SSH connect deadline expired before command acceptance', reason)
        self.assertIn('did not confirm the run ended within 5 seconds', reason)
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run'])

    def test_inner_ssh_does_not_start_with_less_than_one_second_left(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        now = 0.0
        read = os.read
        mac_ready = (mac_linux.INNER_CONNECT_READY + '\n').encode()

        def read_after_outer_hop(fd: int, size: int) -> bytes:
            nonlocal now
            data = read(fd, size)
            if data == mac_ready:
                now = settings.unreachable_wait_seconds - 0.5
            return data

        with (patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(os, 'read', read_after_outer_hop)):
            reason = self.unconfirmed(self.route())
        self.assertIn('Less than one second remains for the guest SSH connection.', reason)
        self.assertIn('Guest cleanup: ended', reason)
        self.assert_guest_clean('probe', 'arm', 'cleanup')

    def test_mac_wrapper_runs_under_bash_and_zsh(self) -> None:
        for shell in ('bash', 'zsh'):
            with self.subTest(shell=shell), patch.dict(os.environ, {'FIXTURE_MAC_SHELL': shell}):
                _ = self.trace.write_text('')
                result = self.route()
                assert isinstance(result, GateRun), result
                self.assertTrue(result.passed)
                self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')
                self.assertEqual(list(self.root.glob('mac-linux-ssh.*')), [])

    def test_guest_probe_retries_a_crlf_ssh_client_log(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        with (patch.dict(os.environ, {'FIXTURE_SSH_PROBE': 'reset'}),
              patch.object(mac_linux, 'sleep') as delay):
            self.assertIsInstance(mac_linux.read_guest_main(settings), mac_linux.EmptyGuestMain)
        delay.assert_called_once_with(mac_linux.CONNECT_RETRY_SECONDS)
        self.assert_guest_clean('probe', 'probe')

    def test_arm_stays_valid_through_a_connect_retry(self) -> None:
        with (patch.object(mac_linux, 'SSH_CONNECT_TIMEOUT_SECONDS', 1),
              patch.dict(os.environ, {'FIXTURE_SSH_RUN': 'reset'})):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assert_guest_clean('probe', 'arm', 'run', 'run', 'cleanup')

    def test_cleanup_recovers_after_three_inner_connect_failures(self) -> None:
        with (patch.object(mac_linux, 'sleep') as delay,
              patch.dict(os.environ, {'FIXTURE_SSH_CLEANUP': 'reset,reset,reset'})):
            result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual(delay.call_args_list, [call(1), call(1), call(8)])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'cleanup', 'cleanup', 'cleanup')

    def test_interrupted_cleanup_pause_keeps_the_connect_attempt_count(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        armed = mac_linux.arm_guest(settings, 'cleanup-connect-interrupt')
        end = mac_linux.GuestEndWait(settings, armed)
        with (patch.object(mac_linux, 'sleep', side_effect=[None, KeyboardInterrupt(), None, None]) as delay,
              patch.dict(os.environ, {'FIXTURE_SSH_CLEANUP': 'reset,reset,reset'})):
            with self.assertRaises(KeyboardInterrupt):
                end.run()
            end.run()
        self.assertTrue(end.proven)
        self.assertEqual(delay.call_args_list, [call(1), call(1), call(1), call(8)])
        self.assert_guest_clean('arm', 'cleanup', 'cleanup', 'cleanup', 'cleanup')

    def test_sandboxed_guest_returns_module_metrics(self) -> None:
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

    def recorded_guest_gate(self) -> dict[str, object]:
        paths = sorted((store.root() / mac_linux.TARGET).glob('*.jsonl'))
        self.assertEqual(len(paths), 1, 'The guest gate did not save a buildlog file.')
        line, = paths[0].read_text(encoding='utf-8').splitlines()
        return cast(dict[str, object], json.loads(line))

    def write_mac_load(self, load: float, cpus: int, written: float) -> None:
        facts: machine_facts.SavedFacts = {'version': machine_facts.VERSION, 'written': written,
                                         'machines': {'mac': machine_facts.facts('read', load=load, cpus=cpus)}}
        _ = machine_facts.state_path().write_text(json.dumps(facts), encoding='utf-8')

    def test_green_guest_saves_each_module_time(self) -> None:
        result = self.route()
        assert isinstance(result, GateRun) and result.passed, result
        row = self.recorded_guest_gate()
        self.assertEqual((row['kind'], row['host'], row['status']), ('gate_run', mac_linux.TARGET, 0))
        self.assertEqual(row['max_jobs'], 2)
        self.assertNotIn('vcpus', row)
        modules = cast(list[dict[str, object]], row['modules'])
        self.assertEqual([(module['suite'], module['module'], module['wall_s'], module['cpu_s']) for module in modules],
                         [('fixture', 'test_first.py', 1.3, 1.0), ('fixture', 'test_second.py', 1.3, 1.0)])

    def test_guest_affected_full_records_full_selection(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main')}):
            result = gate.run_gate_suites(self.repo, full=True, mode=SuiteMode.TARGETED, module_time_limit_s=3,
                                          target=gate.MacLinuxAssignment(self.placed))
        self.assertTrue(result.passed)
        self.assertEqual([job.module for job in result.jobs], ['test_first.py', 'test_second.py'])
        row = self.recorded_guest_gate()
        self.assertEqual(row['step'], 'test-affected')
        self.assertEqual(row['selection'], 'full')

    def test_guest_records_each_suite_wall_time(self) -> None:
        other = self.repo / 'scripts/other'
        other.mkdir()
        (other / 'test_other.py').touch()
        result = self.route('sequential')
        assert isinstance(result, GateRun) and result.passed, result
        self.assertEqual([job.wall_clock for job in result.jobs],
                         [WallClockSpan(1000, 1001), WallClockSpan(1001, 1002), WallClockSpan(1005, 1009)])
        row = self.recorded_guest_gate()
        self.assertEqual(row['wall_s'], 9)
        self.assertEqual(row['suites'], [{'suite': 'fixture', 'wall_s': 2, 'status': 0},
                                         {'suite': 'other', 'wall_s': 4, 'status': 0}])

    def test_named_guest_run_writes_no_gate_record(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'pass',
                                     'FIXTURE_MODULE_SECONDS': '7',
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main')}):
            result = gate.run_gate_suites(self.repo, mode=SuiteMode.TARGETED, target=gate.MacLinuxAssignment(self.placed),
                                          named=('fixture/test_first.py', 'fixture/test_second.py'))
        self.assertTrue(result.passed)
        self.assertEqual([job.module for job in result.jobs], ['test_first.py', 'test_second.py'])
        self.assertEqual(list((store.root() / mac_linux.TARGET).glob('*.jsonl')), [])

    def test_guest_saves_distinct_start_and_end_mac_load(self) -> None:
        start = time.time() - 2
        end = start + 1
        self.write_mac_load(4.75, 12, start)

        def update_facts(*_args: object, **_options: object) -> None:
            self.write_mac_load(8.25, 12, end)

        with patch.object(mac_linux, 'start_guest_report', side_effect=update_facts) as report:
            result = self.route()
        assert isinstance(result, GateRun) and result.passed, result
        report.assert_called_once()
        row = self.recorded_guest_gate()
        self.assertEqual(row['mac_load_start'], {'state': 'read', 'load': 4.75, 'cpus': 12, 'written': start})
        self.assertEqual(row['mac_load_end'], {'state': 'read', 'load': 8.25, 'cpus': 12, 'written': end})

    def test_guest_saves_absent_mac_load_with_its_reason(self) -> None:
        def expire_facts(*_args: object, **_options: object) -> None:
            self.write_mac_load(4.75, 12, time.time() - machine_facts.STALE_SECONDS - 1)

        with patch.object(mac_linux, 'start_guest_report', side_effect=expire_facts) as report:
            result = self.route()
        assert isinstance(result, GateRun) and result.passed, result
        report.assert_called_once()
        row = self.recorded_guest_gate()
        self.assertEqual(row['mac_load_start'], {'state': 'absent', 'reason': 'The machine facts file is missing or invalid.'})
        self.assertEqual(row['mac_load_end'], {'state': 'absent', 'reason': 'The machine facts reading is stale.'})

    def test_child_text_on_standard_output_leaves_the_reply_clean(self) -> None:
        result = self.route('child-stdout')
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assertTrue(result.passed)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_dbus_run_session_text_on_standard_output_leaves_the_reply_clean(self) -> None:
        marker = self.root / 'dbus-run-session.ran'
        self.executable(self.root / 'bin/dbus-run-session',
                        f'#!/bin/sh\necho >> {shlex.quote(str(marker))}\n'
                        + 'echo "A dbus-run-session line must stay outside the JSON result."\n'
                        + '[ "$1" = -- ] && shift\nexec "$@"\n')
        result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assertTrue(result.passed)
        self.assertTrue(marker.exists(), 'The gate did not start under dbus-run-session.')
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_red_module_returns_a_failed_gate(self) -> None:
        result = self.route('red')
        assert isinstance(result, GateRun), result
        self.assertFalse(result.passed)
        self.assertEqual([job.returncode for job in result.jobs], [1, 1])
        row = self.recorded_guest_gate()
        self.assertEqual((row['kind'], row['host'], row['status']), ('gate_run', mac_linux.TARGET, 1))
        modules = cast(list[dict[str, object]], row['modules'])
        self.assertEqual([(module['wall_s'], module['status']) for module in modules], [(1.3, 1), (1.3, 1)])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_runner_failure_returns_reason_and_cleans(self) -> None:
        with self.assertRaisesRegex(GateResultUnknown, 'fixture guest runner failed') as result:
            _ = self.route('runner-failure')
        self.assertIn('Guest cleanup: ended', str(result.exception))
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_runner_failure_before_a_test_reports_an_unconfirmed_start(self) -> None:
        # Without Chromium on the guest PATH, the runner stops before a test starts.
        (self.root / 'bin/chromium').unlink()
        # A host can have Chromium, as the guest has: a PATH folder that holds it changes to a fixture folder of links
        # to each of its other entries.
        folders = os.environ['PATH'].split(os.pathsep)
        for index, folder in enumerate(folders):
            if folder and shutil.which('chromium', path=folder):
                links = self.root / f'path-{index}'
                links.mkdir()
                for entry in Path(folder).iterdir():
                    if entry.name != 'chromium':
                        (links / entry.name).symlink_to(entry)
                folders[index] = str(links)
        os.environ['PATH'] = os.pathsep.join(folders)
        self.assertIsNone(shutil.which('chromium'))
        reason = self.unconfirmed(self.route())
        self.assertRegex(reason, r'^SSH or source stream exited 1: .*Chromium is absent from the guest PATH')
        self.assertIn('did not confirm whether the run started', reason)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_unusable_reply_names_the_exit_status_and_the_stderr_tail(self) -> None:
        reply = mac_linux.GuestReply('', 1, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN,
                                     'SSH or source stream exited 0: fixture stderr tail', False)
        source = mac_linux.CandidateSource('head', 'commit', 'tree', 'main', 'origin', 'runner',
                                           frozenset(), ModuleCoverage.COMPLETE)
        request = cast(mac_linux.GuestRequest, cast(object, {}))
        with self.assertRaises(mac_linux.GuestTookNoRun) as before:
            _ = mac_linux.completed_run(reply, request, source)
        self.assertEqual(str(before.exception), 'the guest reply is unusable '
                         + "(Expecting value: line 1 column 1 (char 0)); reply start ''; "
                         + 'SSH or source stream exited 0: fixture stderr tail')
        with self.assertRaises(mac_linux.GuestUnavailable) as after:
            _ = mac_linux.completed_run(replace(reply, tests_started=True), request, source)
        self.assertNotIsInstance(after.exception, mac_linux.GuestTookNoRun)
        self.assertEqual(str(after.exception), 'the guest reply is unusable (Expecting value: line 1 column 1 '
                         + "(char 0)); reply start ''; SSH or source stream exited 0: fixture stderr tail")

    def test_unusable_reply_shows_only_its_first_200_characters(self) -> None:
        reply = mac_linux.GuestReply('a' * 200 + 'b' * 100, 1, mac_linux.BundleForm.FIRST,
                                     mac_linux.BundleReason.FIRST_RUN, 'SSH or source stream exited 0: fixture', False)
        source = mac_linux.CandidateSource('head', 'commit', 'tree', 'main', 'origin', 'runner',
                                           frozenset(), ModuleCoverage.COMPLETE)
        request = cast(mac_linux.GuestRequest, cast(object, {}))
        with self.assertRaises(mac_linux.GuestTookNoRun) as refusal:
            _ = mac_linux.completed_run(reply, request, source)
        self.assertIn(f"; reply start '{'a' * 200}'; SSH or source stream exited 0: fixture", str(refusal.exception))
        self.assertNotIn('b', str(refusal.exception).partition('reply start')[2])

    def test_bad_byte_count_before_the_start_line_took_no_run_and_after_it_refuses(self) -> None:
        attempt = 'fixture-attempt'
        send = mac_linux.BundleSend('', '', mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN)
        for started, error in ((False, mac_linux.GuestTookNoRun), (True, mac_linux.GuestResultUnavailable)):
            with self.subTest(started=started):
                stream = subprocess.CompletedProcess[bytes]([], 0, b'{"reply": 1}\nno count\xff\n', b'fixture stderr\n')
                with self.assertRaises(mac_linux.GuestUnavailable) as refusal:
                    _ = mac_linux.stream_reply(stream, attempt, 10, send, 'Guest cleanup: ended.', tests_started=started)
                self.assertIs(type(refusal.exception), error)
                self.assertTrue(str(refusal.exception).startswith(
                    "the guest stream gave no byte count ('no count\ufffd'); SSH or source stream exited 0: "),
                    str(refusal.exception))

    def test_stream_result_uses_the_accepted_guest_start(self) -> None:
        stream_guest_bundle = mac_linux.stream_guest_bundle
        for folder in (False, True):
            for status, reason in ((1, 'SSH or source stream exited 1'), (0, 'the guest stream gave no byte count')):
                def stream_result(repo: Path, _command: list[str], _bootstrap: bytes, timeout: int,
                                  link: int, armed: mac_linux.ArmedGuestAttempt,
                                  dispatch: mac_linux.GuestGateDispatch,
                                  **_timing: float) -> subprocess.CompletedProcess[bytes]:
                    marker = (mac_linux.guest_start_line(armed.attempt, self.repo, self.guest_home) if folder
                              else mac_linux.run_start_line(armed.attempt))
                    program = 'import sys; print(sys.argv[1], file=sys.stderr); print("bad count"); sys.exit(int(sys.argv[2]))'
                    return stream_guest_bundle(repo, [sys.executable, '-c', program, marker, str(status)],
                                               b'', timeout, link, armed, dispatch)

                with (self.subTest(folder=folder, status=status),
                      patch.object(mac_linux, 'stream_guest_bundle', side_effect=stream_result)):
                    with self.assertRaisesRegex(GateResultUnknown, reason) as raised:
                        _ = self.route()
                    self.assertIn('Guest cleanup: ended', str(raised.exception))

    def test_copy_can_retry_after_a_partial_upload(self) -> None:
        main = self.guest_home / '.claude'
        self.assertIsInstance(self.route(), GateRun)
        _ = (self.repo / 'scripts/fixture/test_added.py').touch()
        reason = self.unconfirmed(self.route('disconnect'))
        self.assertTrue(reason.startswith('SSH or source stream exited 1:'), reason)
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

    def test_nightly_owner_without_a_unit_gets_a_numbered_copy(self) -> None:
        owner_variables = dict.fromkeys(admission_records.OWNER_VARIABLES, '')
        with patch.dict(os.environ, {**owner_variables, 'CLAUDE_SESSION_NAME': 'nightly',
                                     'BUILDLOG_UNIT': '',
                                     'FIXTURE_EXPECTED_UNIT': 'c410c68e-7115-552d-b80c-bbaa69545d75'}):
            result = self.route(gate_job=StepJob('test-nightly', branch='main'))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_gate_without_an_owner_refuses_before_guest_contact(self) -> None:
        owner_variables = dict.fromkeys((*admission_records.OWNER_VARIABLES, 'BUILDLOG_UNIT'), '')
        with (patch.dict(os.environ, owner_variables),
              patch.object(admission_records, 'family_owner',
                           return_value=admission_records.OwnerNotGiven('systemd · fixture.service')),
              patch.object(admission_records, 'folder_owner', return_value='')):
            result = self.route(gate_job=StepJob('test-nightly', branch='main'))
        self.assertEqual(result, 'mac-linux: the gate has no owner unit for a numbered copy')
        self.assertEqual(self.trace.read_text(), '')
        self.assertEqual(self.consumed, ())

    def test_unknown_caller_with_a_session_gets_a_numbered_copy(self) -> None:
        owner = admission_records.QueueCaller('detached-nightly-session', UNKNOWN_CALLER, '')
        with (patch.object(admission_records, 'request_owner', return_value=owner),
              patch.dict(os.environ, {'FIXTURE_EXPECTED_UNIT': '269f6b52-0174-51c0-a5fc-a785808a5512'})):
            result = self.route(gate_job=StepJob('test-nightly', branch='main'))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_unknown_caller_without_a_session_refuses_before_guest_contact(self) -> None:
        owner = admission_records.QueueCaller('', UNKNOWN_CALLER, '')
        with patch.object(admission_records, 'request_owner', return_value=owner):
            result = self.route(gate_job=StepJob('test-nightly', branch='main'))
        self.assertEqual(result, 'mac-linux: the gate has no owner unit for a numbered copy')
        self.assertEqual(self.trace.read_text(), '')
        self.assertEqual(self.consumed, ())

    def test_full_and_delta_bundles_run_the_committed_runner_with_its_modules(self) -> None:
        main = self.guest_home / '.claude'
        candidate_main = self.git('rev-parse', 'origin/main')
        selected = TreeSelection(('scripts/fixture/test_first.py',), self.git('rev-parse', 'HEAD^{tree}'))
        runner = self.runner_repo / mac_linux.RUNNER_PATH
        runner_code = runner.read_text(encoding='utf-8')
        for form, reason in ((mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN),
                             (mac_linux.BundleForm.DELTA, mac_linux.BundleReason.DELTA)):
            with self.subTest(form=form):
                # Advance only the runner. Each transfer must send the new commit.
                module = self.runner_repo / 'scripts/fixture/runner_only.py'
                _ = module.write_text(f'REVISION = {form.value!r}\n', encoding='utf-8')
                _ = runner.write_text(runner_code.replace(
                    'from scripts.aws_target import gate_box, ledger',
                    'from scripts.fixture.runner_only import REVISION\n'
                    + f'assert REVISION == {form.value!r}\n'
                    + 'from scripts.aws_target import gate_box, ledger', 1), encoding='utf-8')
                _ = self.git_at(self.runner_repo, 'add', 'scripts')
                _ = self.git_at(self.runner_repo, 'commit', '-qm', 'Advance the guest runner')
                commit = self.git_at(self.runner_repo, 'rev-parse', 'HEAD')
                result = self.route(selected=selected)
                assert isinstance(result, mac_linux.MacLinuxGateRun), result
                self.assertTrue(result.passed)
                self.assertEqual([job.module for job in result.jobs], ['test_first.py'])
                self.assertEqual((result.bundle_form, result.bundle_reason), (form, reason))
                self.assertEqual(self.git_at(main, 'rev-parse', 'HEAD', 'origin/main').splitlines(),
                                 [candidate_main, candidate_main])
                self.assertEqual(self.git_at(main, 'cat-file', '-t', commit), 'commit')
                self.assertFalse((main / 'scripts/fixture/runner_only.py').exists())
                stream = (self.root / 'source.payload').read_bytes()
                with tarfile.open(fileobj=io.BytesIO(stream)) as archive:
                    self.assertEqual(archive.getnames(), ['request.json'])
                    request = archive.extractfile('request.json')
                    assert request is not None
                    row = cast(mac_linux.GuestRequest, json.load(request))
                    self.assertEqual(row['runner_commit'], commit)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_dirty_runner_scripts_refuse_the_guest_before_claim_or_ssh(self) -> None:
        runner = self.runner_repo / mac_linux.RUNNER_PATH
        original = runner.read_text()
        for changed in (runner, runner.with_name('untracked.py')):
            with self.subTest(path=changed.name):
                _ = changed.write_text('# Changed runner source.\n', encoding='utf-8')
                with self.assertRaisesRegex(mac_linux.GuestUnavailable, 'runner scripts have uncommitted changes'):
                    _ = mac_linux.checkout_source(self.repo)
                self.assertIn('runner scripts have uncommitted changes', self.not_started(self.route()))
                self.assert_guest_clean()
                self.assertEqual(self.consumed, ())
                if changed == runner:
                    _ = runner.write_text(original, encoding='utf-8')

    def test_runner_index_flags_refuse_even_a_clean_runner(self) -> None:
        runner = self.runner_repo / mac_linux.RUNNER_PATH
        for flag in ('assume-unchanged', 'skip-worktree'):
            with self.subTest(flag=flag):
                _ = self.git_at(self.runner_repo, 'update-index', f'--{flag}', '--', mac_linux.RUNNER_PATH)
                try:
                    with self.assertRaisesRegex(mac_linux.GuestTookNoRun, flag):
                        _ = mac_linux.runner_commit(self.repo)
                    _ = runner.write_text('# Hidden runner change.\n', encoding='utf-8')
                    with self.assertRaisesRegex(mac_linux.GuestTookNoRun, flag):
                        _ = mac_linux.runner_commit(self.repo)
                finally:
                    _ = self.git_at(self.runner_repo, 'update-index', f'--no-{flag}', '--', mac_linux.RUNNER_PATH)
                    _ = self.git_at(self.runner_repo, 'restore', '--', mac_linux.RUNNER_PATH)
        self.assert_guest_clean()

    def test_runner_scripts_must_match_the_captured_commit(self) -> None:
        initial = self.git_at(self.runner_repo, 'rev-parse', 'HEAD')
        runner = self.runner_repo / mac_linux.RUNNER_PATH
        with runner.open('a', encoding='utf-8') as script:
            _ = script.write('\n# Change the runner.\n')
        _ = self.git_at(self.runner_repo, 'commit', '-qam', 'Change the runner')
        changed = self.git_at(self.runner_repo, 'rev-parse', 'HEAD')
        _ = self.git_at(self.runner_repo, 'checkout', '-q', '--detach', initial)
        checked_git = gate.checked_git

        def advance_after_capture(folder: Path, *arguments: str) -> str:
            result = checked_git(folder, *arguments)
            if arguments[0] == 'rev-parse' and arguments[-1] == 'HEAD':
                _ = self.git_at(self.runner_repo, 'checkout', '-q', '--detach', changed)
            return result

        with (patch.object(gate, 'checked_git', advance_after_capture),
              self.assertRaisesRegex(mac_linux.GuestTookNoRun, 'runner scripts have uncommitted changes')):
            _ = mac_linux.runner_commit(self.repo)
        self.assertEqual(self.git_at(self.runner_repo, 'status', '--porcelain'), '')
        self.assert_guest_clean()

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
        assert isinstance(second.bundle_bytes, int) and isinstance(first.bundle_bytes, int)
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
        assert isinstance(result.bundle_bytes, int) and isinstance(first.bundle_bytes, int)
        self.assertLess(result.bundle_bytes, first.bundle_bytes)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_guest_missing_prerequisite_retries_once_with_a_full_bundle(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        attempts: list[tuple[str, claim_state.ClaimIdentity]] = []
        paths = claim_state.state_paths('mac')
        arm = mac_linux.arm_guest

        def record_arm(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
            claim, = claim_state.read_claims(paths)
            attempts.append((attempt, claim_state.claim_identity(claim.record)))
            return arm(settings, attempt)

        with (patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': self.git('rev-parse', 'origin/main')}),
              patch.object(mac_linux, 'arm_guest', record_arm)):
            result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        (first_attempt, first_claim), (retry_attempt, retry_claim) = attempts
        self.assertEqual(retry_attempt, first_attempt + mac_linux.FULL_BUNDLE_RETRY_SUFFIX)
        self.assertNotEqual(first_claim, retry_claim)
        self.assertEqual(claim_state.read_claims(paths), ())
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.GUEST_LACKS_PREREQUISITE,
                           'delta', 'first')
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'arm', 'run', 'cleanup')

    def test_lost_full_bundle_arm_acknowledgement_settles_after_recovery(self) -> None:
        self.placed = replace(self.placed, attempt=f'{45:032x}')
        episode = self.placed.episode
        (self.repo / 'scripts/fixture/test_added.py').touch()
        arm = mac_linux.arm_guest

        def lose_retry_reply(settings: mac_linux.GuestSettings, attempt: str) -> mac_linux.ArmedGuestAttempt:
            if attempt.endswith(mac_linux.FULL_BUNDLE_RETRY_SUFFIX):
                with patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'missing-arm-ack'}):
                    return arm(settings, attempt)
            return arm(settings, attempt)

        with (patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': self.git('rev-parse', 'origin/main')}),
              patch.object(mac_linux, 'arm_guest', lose_retry_reply)):
            outcome = self.route()
        assert isinstance(outcome, mac_linux.GuestNotStarted), outcome
        self.assertTrue(outcome.settlement_required)
        self.assertIn('did not acknowledge the armed deadline', outcome.reason)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'arm')
        self.assertFalse((self.guest_home / 'build').exists())
        self.assertIsInstance(mac_linux.settlement(episode, 45), placement.Pending)
        mac_linux.reconcile_guest_end(episode, 45)
        self.assertEqual(mac_linux.settlement(episode, 45), placement.Settled())
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'arm', 'fence')

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
        with (patch.dict(os.environ, {'FIXTURE_MODULE_SECONDS': '7'}),
              patch.object(mac_linux, 'read_settings', wraps=mac_linux.read_settings) as settings):
            result = self.route(selected=TreeSelection(('scripts/fixture/test_second.py',), tree),
                                module_time_limit_s=None)
        settings.assert_called_once()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual(result.coverage, ModuleCoverage.SUBSET)
        self.assertEqual([job.module for job in result.jobs], ['test_second.py'])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_runs_type_checks_with_the_selected_tests(self) -> None:
        tree = self.git('rev-parse', 'HEAD^{tree}')
        result = self.route(selected=TreeSelection(('scripts/fixture/test_second.py',), tree),
                            type_dirs=('scripts/fixture', str(self.repo / 'scripts/lib')))
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assertTrue(result.passed, result.jobs)
        module, types = result.jobs
        self.assertEqual((module.kind, module.module), (GateJobKind.TEST_MODULE, 'test_second.py'))
        self.assertIs(types.kind, GateJobKind.TYPE_CHECK)
        self.assertEqual(types.output, 'Type check: scripts/fixture, scripts/lib\n')
        self.assertEqual(result.module_skips, {'scripts/fixture/test_second.py': 1})
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_enables_failure_reruns_and_reports_the_pass_on_natedev(self) -> None:
        with (patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': 'rerun-alone',
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main')}),
              redirect_stdout(io.StringIO()) as output):
            result = gate.run_gate_suites(self.repo, full=True, module_time_limit_s=3,
                                          target=gate.MacLinuxAssignment(self.placed))
        self.assertTrue(result.passed)
        attempts = [job for job in result.executions if job.module == 'test_first.py']
        self.assertEqual([job.returncode for job in attempts], [1, 0])
        self.assertEqual(attempts[0].output, 'fixture first failure')
        label = '--- fixture/test_first.py passed on rerun alone; first run exit=1 ---'
        self.assertEqual(output.getvalue().count(label), 1)
        self.assertIn('fixture first failure', output.getvalue())
        self.assertIn('Mac Linux guest gate passed: 2 modules', output.getvalue())
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_refuses_when_the_type_check_tool_is_missing(self) -> None:
        missing = self.root / 'missing-basedpyright'
        with patch.dict(os.environ, {'GATE_BASEDPYRIGHT': str(missing)}):
            reason = self.unconfirmed(self.route(type_dirs=('scripts',)))
        self.assertTrue(reason.startswith('SSH or source stream exited 1:'), reason)
        self.assertIn(f'The guest cannot find the type-check tool {missing}', reason)
        self.assertIn('Guest cleanup: ended', reason)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_missing_type_tool_on_guest_and_natedev_ends_the_gate_red(self) -> None:
        fixture = GatePlacementFixture(self)
        self.placed = guest = replace(fixture.guest, attempt=f'{48:032x}')
        path = fixture.assignment_file
        sidecar = path.with_name('progress')
        local = replace(fixture.local, grant=replace(fixture.local.grant, max_path=str(self.root / 'gate.max')))
        _ = write_config_fixture(self.registry.with_name('build_admission.conf'))
        admission_wire.write_assignment(path, wire.Assignment(guest))
        fixture.report_failure.return_value = wire.DispatchStatus(guest, placement.CurrentAssignment(),
            placement.PermissionConsumed(1.0), retry=placement.RetryAt(1.0))
        attempts: list[tuple[str, int]] = []
        missing = self.root / 'missing-uv'

        def run() -> subprocess.CompletedProcess[str]:
            grant = admission_wire.read_assignment(path)
            target = (gate.LocalGateAssignment(grant.grant, grant.assignment.owner, sidecar)
                      if isinstance(grant, wire.LocalPlacementGranted) else gate.MacLinuxAssignment(grant.assignment))
            error = ''
            try:
                result = gate.run_gate_suites(self.repo, ('scripts',), full=True, target=target)
                status = int(not result.passed)
            except gate.MacLinuxTookNoRun as refused:
                status, error = refused.status, str(refused)
                self.assertIn(f'The guest cannot find the type-check tool {missing}', error)
            attempts.append((grant.assignment.destination.target, status))
            return subprocess.CompletedProcess(fixture.request.constraints.command, status, '', error)

        def place_again(request: wire.PlacementRequest) -> None:
            self.assertEqual(request, fixture.request)
            admission_wire.write_assignment(path, local)

        place = Mock(side_effect=place_again)
        run_types = partial(gate.run_suites, machine=gate.WholeMachine(), selected=gate.SelectedModules(()),
                            time_limit=NoGateTimeLimit('type tool fixture'))
        with (patch.dict(os.environ, {'GATE_UV': str(missing),
                                     'FIXTURE_EXPECTED_MAIN': self.git('rev-parse', 'origin/main'),
                                     'BUILDLOG_PLACEMENT_FILE': str(path)}),
              patch.object(gate, 'module_runner', return_value=CHECKOUT / 'scripts/test_times/module_run.py'),
              patch.object(build_folders, 'configured_gate_root', return_value=self.root),
              patch.object(gate, 'run_suites', side_effect=run_types),
              patch.object(gate, 'release_local_gate') as release,
              redirect_stdout(io.StringIO()) as output):
            result = first_step.run_gate_with_placement(run, path, fixture.request, place)
        self.assertEqual(attempts, [('mac-linux', 79), ('natedev', 1)], result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertIn('basedpyright: RED\ntype-check venv missing:', output.getvalue())
        self.assertIn(str(missing), output.getvalue())
        refusal = wire.parse_request(fixture.report_file.read_bytes())
        assert isinstance(refusal, wire.ClaimFailure), refusal
        self.assertEqual((refusal.assignment, refusal.reason), (guest, placement.MAC_LINUX_START_UNCONFIRMED))
        fixture.report_failure.assert_called_once_with(refusal)
        place.assert_called_once_with(fixture.request)
        release.assert_called_once_with(gate.LocalGateAssignment(local.grant, guest.owner, sidecar), 1)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_command_request_runs_the_argv_in_the_numbered_copy_and_proves_the_end(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        for status in (0, 4):
            program = f'import os, sys\nprint("guest ran in", os.getcwd(), flush=True)\nsys.exit({status})\n'
            script = self.repo / 'scripts/fixture/hand.py'
            _ = script.write_text(program, encoding='utf-8')
            command = mac_linux.GuestCommand(settings)
            try:
                process = command.start(self.repo, ['python3', 'scripts/fixture/hand.py'], work='hand-made',
                                        unit='caller', bootstrap=self.root / 'bootstrap.tar',
                                        checked={script: program.encode()})
                with process:
                    output, _ = process.communicate(timeout=60)
            finally:
                command.finish()
            self.assertEqual(process.returncode, status, output)
            self.assertTrue(output.decode().endswith(f'guest ran in {self.guest_home / "build/claude/caller/1"}\n'),
                            output)
            # start reads the start line, so the process output does not hold it.
            self.assertNotIn(mac_linux.RUN_START.encode(), output)
            self.assertEqual(self.git('for-each-ref', 'refs/mac-linux'), '')
            self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_command_request_refuses_a_guest_file_that_natedev_did_not_read(self) -> None:
        settings = mac_linux.read_settings(conf.read(self.registry))
        command = mac_linux.GuestCommand(settings)
        write = "from pathlib import Path; Path('target/run.sh').write_text('echo unread')"
        try:
            process = command.start(self.repo, ['python3', '-c', write], work='hand-made', unit='caller',
                                    bootstrap=self.root / 'bootstrap.tar', checked={})
            with process:
                output, _ = process.communicate(timeout=60)
        finally:
            command.finish()
        self.assertEqual(process.returncode, 0, output)
        # The numbered copy keeps target/, so the guest has the file and natedev has none.
        command = mac_linux.GuestCommand(settings)
        with self.assertRaisesRegex(mac_linux.GuestRefusedCommand, 'The guest refuses the command word target/run.sh: '
                                    + r'the guest has a file at /\S+/target/run\.sh, and natedev read no file there\.'):
            try:
                _ = command.start(self.repo, ['bash', 'target/run.sh'], work='hand-made', unit='caller',
                                  bootstrap=self.root / 'bootstrap.tar', checked={})
            finally:
                command.finish()
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup', 'probe', 'arm', 'run', 'cleanup')

    def test_command_request_with_a_busy_run_lock_is_refused_at_once_with_no_end_proof(self) -> None:
        command = mac_linux.GuestCommand(mac_linux.read_settings(conf.read(self.registry)))
        lock = self.run_lock()
        lock.parent.mkdir(parents=True)
        with lock.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(mac_linux.GuestNotLocked, '^The guest run lock is held.$'):
                try:
                    _ = command.start(self.repo, ['python3', '-c', 'print(1)'], work='hand-made', unit='caller',
                                      bootstrap=self.root / 'bootstrap.tar', checked={})
                finally:
                    command.finish()
        assert command.process is not None and command.process.stdout is not None
        self.assertTrue(command.process.stdout.closed)
        self.assertEqual(command.process.returncode, mac_linux.GUEST_NOT_LOCKED_STATUS)
        self.assertEqual(self.git('for-each-ref', 'refs/mac-linux'), '')
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run'])

    def test_dirty_modules_are_included_in_the_snapshot(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual([job.module for job in result.jobs], ['test_added.py', 'test_first.py', 'test_second.py'])
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')


if __name__ == '__main__':
    _ = unittest.main()
