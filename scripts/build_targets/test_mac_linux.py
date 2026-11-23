"""Test the Mac Linux route with a local SSH fixture."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr, suppress
from dataclasses import replace
from datetime import timedelta
import errno
import fcntl
import os
from pathlib import Path
import pwd
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from threading import Timer
import time
from types import CodeType
from typing import cast, override
import unittest
from unittest.mock import call, patch

from scripts.build_hold.owning_session import OwningSession
from scripts.build_queue import admission_client, admission_protocol as wire, execution_identity, mac_run, placement
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_targets import mac_linux, target_config
from scripts.buildlog import store
from scripts.lib import conf, process_group, process_start
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state, eligibility, mac_test
from scripts.production import gate
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import ALL_MODULES, AllModules, GateJobKind, GateRun, ModuleCoverage, SuiteMode, TreeSelection


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
if 'FIXTURE_GUEST_PATH' in os.environ:
    environment['PATH'] = os.environ['FIXTURE_GUEST_PATH']
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
# A guest whose SSH server has not seen the end of the link keeps its input open.
held = before_copy or 'FIXTURE_HELD_LINK' in os.environ
guest = subprocess.Popen(['bash', '-c', shell], stdin=guest_input,
                         stdout=subprocess.DEVNULL if before_copy else None,
                         stderr=subprocess.PIPE if before_copy else None,
                         env=environment, start_new_session=True, pass_fds=(link,) if held else ())
os.close(guest_input)
os.close(link)
if before_copy:
    import selectors

    with selectors.DefaultSelector() as ready:
        ready.register(guest.stderr, selectors.EVENT_READ)
        assert ready.select(3), 'The guest did not take the run lock.'
    assert guest.stderr.readline().startswith(b'mac-linux run locked ')
    assert guest.stderr.readline() == b'fixture pause before take_copy\\n'
    record_bundle()
    if scenario == 'interrupted-before-copy':
        print('fixture local SSH is still active', file=sys.stderr, flush=True)
        signal.pause()
    os._exit(255)
status = guest.wait()
record_bundle()
sys.exit(status)
'''


BWRAP_FIXTURE = '''import os
from pathlib import Path
import sys

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


GATE_FIXTURE = '''import fcntl
import os
from pathlib import Path
import signal
import subprocess
from time import sleep

from scripts.copies import copies
from scripts.production.gate_modules import discover_test_modules
from scripts.production.gate_routes import GateJobKind, GateJobResult, GateRun, SelectedModules, SuiteMode
from scripts.test_times.gate_record import TYPE_CHECK_JOB


def checked_git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


def WholeMachine():
    return 'configured gate root'


def run_suites(repo, *, type_dirs, full, mode, machine, selected, module_time_limit_s, time_limit):
    assert os.environ['FIXTURE_SANDBOX'] == 'bwrap'
    guest = Path(os.environ['FIXTURE_GUEST_HOME'])
    assert repo == guest / 'build/claude' / os.environ['FIXTURE_EXPECTED_UNIT'] / '1', repo
    assert Path.home().is_relative_to(repo / 'target')
    assert Path(os.environ['XDG_RUNTIME_DIR']).is_relative_to(repo / 'target')
    assert machine == 'configured gate root'
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
    if scenario == 'child-stdout':
        subprocess.run(['echo', 'A child log line must stay outside the JSON result.'], check=True)
    if scenario == 'hang':
        Path(os.environ['FIXTURE_RUN_GROUP']).write_text(str(os.getpgrp()))
        signal.pause()
    if scenario == 'runner-failure':
        raise RuntimeError('fixture guest runner failed')
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
    jobs = tuple(GateJobResult(suite, path.name, int(scenario == 'red'), 1.3, 'fixture module output',
                               cpu_seconds=1.0, times=dict(timing, skipped=int(path.name == 'test_second.py')),
                               peak_mib=22) for suite, path in paths)
    if type_dirs:
        result = subprocess.run([os.environ['GATE_BASEDPYRIGHT'], *type_dirs], cwd=repo,
                                capture_output=True, text=True)
        jobs += (GateJobResult(*TYPE_CHECK_JOB, result.returncode, 0, result.stdout + result.stderr,
                               kind=GateJobKind.TYPE_CHECK),)
    return GateRun(jobs, 2.0, 30, selection_certified=isinstance(selected, SelectedModules), executions=jobs)
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

from scripts.aws_target import gate_box, ledger

os.chdir(sys.argv[1])
socket.gethostname = lambda: 'nixos'
if os.environ['FIXTURE_GUEST_RESULT'] in ('disconnect-before-copy', 'interrupted-before-copy'):
    print('fixture pause before take_copy', file=sys.stderr, flush=True)
    signal.pause()
if '--run-copy' in sys.argv:
    spec = spec_from_file_location('scripts.production.gate', Path('scripts/production/gate.py'))
    gate = module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
entry = sys.argv[2:]
if entry[0] == '-c':
    # Python sets sys.argv to ['-c', *arguments] for a -c entry.
    sys.argv = ['-c', *entry[2:]]
    exec(entry[1], {'__name__': '__main__'})
else:
    sys.argv = entry
    runpy.run_path(entry[0], run_name='__main__')
'''


class GuestEntryTests(unittest.TestCase):
    def test_gate_entry_gives_the_runner_the_request_and_the_run_copy_word(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runner = root / 'runner.py'
        _ = runner.write_text("import json, sys\nprint(json.dumps(sys.argv))\n"
                              + "print(sys.argv[2:3] == ['--run-copy'])\n", encoding='utf-8')
        request = root / 'request.json'
        command = mac_linux.gate_entry_command(1, runner, request, 7)
        output = subprocess.run(command, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                                check=True, timeout=30).stdout.splitlines()
        self.assertEqual(output, [f'["{runner}", "{request}", "--run-copy", "7"]', 'True'])


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
    root: Path = Path()
    guest_home: Path = Path()
    trace: Path = Path()
    repo: Path = Path()
    registry: Path = Path()
    placed: placement.PlacementAssignment = placement.PlacementAssignment(
        'gate-episode', 'gate-attempt', placement.PlacementDestination(
            'ssh', mac_linux.TARGET, 'fixture-guest', claim_state.RepositoryScope('claude', slot=1), 1),
        execution_identity.SourceKeyUnavailable('fixture'), placement.SubmittingOwner('natedev', 1, 'start'),
        OwningSession('submitting-session'), 0.0)
    # Each consume request, the SSH trace at that moment, and whether the gate held the named Mac claim.
    consumed: tuple[tuple[wire.ConsumePermission, str, bool], ...] = ()

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
        _ = (config / 'machines.conf').write_text(
            'aliases=natedev mac\nnatedev.platform=linux\nnatedev.hosts=natedev\n'
            + 'mac.platform=darwin\nmac.hosts=fixture-mac\n', encoding='utf-8')
        _ = write_config_fixture(config / 'build_folders.conf', '# whole: 1..\nslots.fixture-mac.default_max=1\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
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
        _ = self.git('init', '-q', '--initial-branch=main')
        _ = (self.repo / 'history.txt').write_text('old source\n', encoding='utf-8')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'base')
        _ = self.git('remote', 'add', 'origin', 'https://example.test/claude_commands.git')
        for folder in ('config', 'scripts/lib', 'scripts/production', 'scripts/fixture'):
            (self.repo / folder).mkdir(parents=True)
        _ = shutil.copyfile(config / 'build_folders.conf', self.repo / 'config/build_folders.conf')
        _ = (self.repo / 'scripts/production/gate.py').write_text(GATE_FIXTURE, encoding='utf-8')
        for module in ('test_first.py', 'test_second.py'):
            (self.repo / 'scripts/fixture' / module).touch()
        package_root = shlex.quote(str(Path(__file__).resolve().parents[2]))
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
              gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED, *,
              type_dirs: tuple[str, ...] = ()) -> GateRun | mac_linux.GuestNotStarted | str:
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
                                     module_time_limit_s=3, gate_job=gate_job, assignment=self.placed,
                                     type_dirs=type_dirs)
        self.assertEqual(self.git('for-each-ref', 'refs/mac-linux'), '')
        return result

    def not_started(self, result: GateRun | mac_linux.GuestNotStarted | str) -> str:
        self.assertIsInstance(result, mac_linux.GuestNotStarted)
        assert isinstance(result, mac_linux.GuestNotStarted)
        return result.reason

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
        result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        (request, trace, held), = self.consumed
        start = process_start.process_start(os.getpid())
        assert isinstance(start, process_start.Started)
        self.assertEqual(request.assignment, self.placed)
        self.assertEqual(request.holder, placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start))
        destination = self.placed.destination
        self.assertEqual(request.claim, placement.ProviderClaimReference(destination.provider, destination.target,
            destination.scope, request.claim.claim_id, self.placed.episode, self.placed.attempt))
        self.assertTrue(request.claim.claim_id.startswith(mac_test.MAC_LINUX_CLAIM_ID_PREFIX))
        self.assertTrue(held)
        self.assertEqual(trace, '')
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

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

    def test_free_now_reads_the_probe_backoff_for_a_registered_guest(self) -> None:
        _ = write_config_fixture(self.registry, 'pinned_targets=hana-gpu,mac-linux\n')
        paths = mac_test.state_paths(mac_linux.TARGET)
        configuration = self.registry.with_name('mac_linux.conf')
        _ = write_config_fixture(configuration, 'slots=1\n')
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

    def test_backoff_write_error_keeps_the_probe_failure(self) -> None:
        _ = (self.root / 'target-state').write_text('This file blocks creation of the state directory.\n')
        reason = self.not_started(self.route('offline'))
        self.assertIn('fixture SSH refused the connection', reason)
        self.assertIn(f'Cannot record the {mac_linux.TARGET} backoff:', reason)
        self.assertIn('Not a directory', reason)
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
        for name in ('bash', 'id'):
            executable = shutil.which(name)
            assert executable is not None
            (binaries / name).symlink_to(executable)
        with patch.dict(os.environ, {'FIXTURE_GUEST_PATH': str(binaries)}):
            result = self.not_started(self.route())
        self.assertIn('bwrap is absent from the guest PATH', result)
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
        with (patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '30', 'FIXTURE_HELD_LINK': '1'}),
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
            'mac_linux.consume_launch_permission = lambda claim, assignment: None',
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
        result = self.route('missing-arm-ack')
        self.assertIn('did not acknowledge the armed deadline', str(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm'])
        self.assertFalse(self.run_lock().exists())

    def test_disconnect_before_take_copy_kills_the_run_and_proves_end(self) -> None:
        reason = self.not_started(self.route('disconnect-before-copy'))
        self.assertIn('SSH or source stream exited 255', reason)
        self.assertIn('Guest cleanup: ended', reason)
        self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)
        self.assertFalse((self.guest_home / 'build').exists())
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])

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
                result = mac_linux.route(self.repo, mode=SuiteMode.GATE, full=True, module_time_limit_s=3,
                                         selected=ALL_MODULES, gate_job=GATE_JOB_NOT_RECORDED, assignment=self.placed)
                first, = attempts
                failure, = unproven
                assert isinstance(failure, mac_linux.GuestEndUnproven), failure
                self.assertEqual(failure.armed, first)
                candidate, = self.git('for-each-ref', '--format=%(refname)', 'refs/mac-linux').splitlines()
                self.assertTrue(candidate.endswith('/candidate'))
                note = f'Cannot remove bundle ref {candidate}'
                self.assertTrue(any(note in line for line in failure.__notes__))
                self.assertIn(note, str(result))
                (self.repo / '.git' / f'{candidate}.lock').unlink()
                _ = self.git('update-ref', '-d', candidate)
                self.assertIn('guest unreachable: fixture-guest did not confirm the run ended '
                              + f'within 2 seconds ({key})', str(result))
                self.assertIs(eligibility.read_backoff(time.time(), mac_linux.TARGET), eligibility.NO_BACKOFF)
                proof.assert_called_once_with(settings, first, proof_deadline=2.0)
                self.assertEqual(now, 2.0)
                self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])
                self.assertEqual(claim_state.read_claims(claim_state.state_paths('mac')), ())
                owner = lock.read_text()
                self.assertEqual(owner.split()[1], first.attempt)
                self.assertEqual(subprocess.call(['flock', '-x', '-n', lock, 'true'], timeout=2), 1)

                self.assertIn('The guest run lock is held', str(self.route()))
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
        def reservation(*_args: object) -> Generator[claim_state.ClaimIdentity]:
            try:
                yield FIXTURE_CLAIM
            finally:
                self.assertEqual([type(proof) for proof in proofs],
                                 [mac_linux.GuestEndPending, mac_linux.GuestEndPending, mac_linux.GuestEndProven])

        lock = self.run_lock()
        lock.parent.mkdir(parents=True)
        with lock.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            def prove(settings: mac_linux.GuestSettings,
                      armed: mac_linux.ArmedGuestAttempt, *,
                      proof_deadline: float) -> mac_linux.GuestEndProven | mac_linux.GuestEndPending:
                proof = prove_guest_end(settings, armed, proof_deadline=proof_deadline)
                proofs.append(proof)
                if len(proofs) == 2:
                    fcntl.flock(held, fcntl.LOCK_UN)
                return proof

            with (patch.object(mac_linux, 'mac_linux_reservation', reservation),
                  patch.object(mac_linux, 'prove_guest_end', prove),
                  patch.object(mac_linux, 'sleep'),
                  patch.object(mac_linux, 'GUEST_KILL_GRACE_SECONDS', 0)):
                result = self.route()
        self.assertIn('Guest cleanup: ended', self.not_started(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup', 'cleanup', 'cleanup'])

    def test_full_stderr_pipe_does_not_extend_the_proof_wait(self) -> None:
        configuration = self.registry
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = configuration.write_text(configuration.read_text().replace(f'{key}=20', f'{key}=1'))
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
                with (patch.object(mac_linux, 'prove_guest_end', pending), redirect_stderr(blocked_stderr)):
                    result = self.route()
                    returned = time.monotonic()
            finally:
                if started:
                    guard.cancel()
                    guard.join()
        first_probe, = started
        self.assertIn('guest unreachable: fixture-guest', str(result))
        self.assertLess(returned - first_probe, 1.75)

    def test_local_read_past_the_proof_deadline_starts_no_cleanup_ssh(self) -> None:
        configuration = self.registry
        key = target_config.UNREACHABLE_WAIT_KEY
        _ = configuration.write_text(configuration.read_text().replace(f'{key}=20', f'{key}=1'))
        helper = Path(process_group.__file__)
        read_text = Path.read_text
        now = 0.0

        def read_with_delay(path: Path, **options: str | None) -> str:
            nonlocal now
            contents = read_text(path, **options)
            if path == helper:
                now += 1.25
            return contents

        with (patch.object(Path, 'read_text', read_with_delay),
              patch.object(mac_linux, 'monotonic', lambda: now)):
            result = self.route()
        # The run command and the cleanup command each read the helper once; the cleanup read passes the deadline.
        self.assertEqual(now, 2.5)
        self.assertIn('guest unreachable: fixture-guest', str(result))
        self.assert_guest_clean('probe', 'arm', 'run')

    def test_proof_received_at_the_deadline_refuses(self) -> None:
        now = 0.0

        def late_proof(_settings: mac_linux.GuestSettings, _armed: mac_linux.ArmedGuestAttempt, *,
                       proof_deadline: float) -> mac_linux.GuestEndProven:
            nonlocal now
            self.assertEqual(proof_deadline, 20)
            now = proof_deadline
            return mac_linux.GuestEndProven()

        with (patch.object(mac_linux, 'monotonic', lambda: now),
              patch.object(mac_linux, 'prove_guest_end', late_proof)):
            result = self.route()
        self.assertIn('guest unreachable: fixture-guest', str(result))

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
        self.assertEqual(mac_hop[-4:], alive)
        self.assertEqual(command[-2], 'fixture-mac')
        guest_hop = shlex.split(command[-1])
        self.assertEqual(guest_hop[guest_hop.index('-l') - 4:guest_hop.index('-l')], alive)

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
             mac_linux.GuestEndPending('The guest run lock is still held.')),
        ):
            with self.subTest(status=status), patch.object(
                    subprocess, 'run', return_value=subprocess.CompletedProcess(command, status, stdout, stderr)) as ssh:
                self.assertEqual(mac_linux.prove_kept_guest_end(armed), expected)
                ssh.assert_called_once_with(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                            errors='replace', timeout=mac_linux.GUEST_CLEANUP_TIMEOUT_SECONDS,
                                            check=False)

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
        def reservation(*_args: object) -> Generator[claim_state.ClaimIdentity]:
            try:
                yield FIXTURE_CLAIM
            finally:
                self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'arm', 'run', 'cleanup'])
                self.assertEqual(subprocess.call(['flock', '-x', '-n', self.run_lock(), 'true']), 0)

        with (patch.object(subprocess.Popen, 'communicate', interrupt_source),
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
                        link: int) -> subprocess.CompletedProcess[bytes]:
            stream = stream_guest_bundle(repo, command, bootstrap, timeout, link)
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
            result = self.route('hang')
        self.assertIsInstance(result, str)
        self.assertRegex(str(result), r'SSH or source stream exited (9|137):')
        self.assertIn('Guest cleanup: ended', str(result))
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
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_guest_runner_failure_returns_reason_and_cleans(self) -> None:
        result = self.route('runner-failure')
        self.assertIsInstance(result, str)
        self.assertIn('fixture guest runner failed', str(result))
        self.assertIn('Guest cleanup: ended', str(result))
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_runner_failure_before_a_test_lets_natedev_take_the_gate(self) -> None:
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
        reason = self.not_started(self.route())
        self.assertRegex(reason, r'^SSH or source stream exited 1: .*Chromium is absent from the guest PATH')
        self.assert_guest_clean('probe', 'arm', 'run', 'cleanup')

    def test_unusable_reply_names_the_exit_status_and_the_stderr_tail(self) -> None:
        reply = mac_linux.GuestReply('', 1, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN,
                                     'SSH or source stream exited 0: fixture stderr tail', False)
        source = mac_linux.CandidateSource('head', 'commit', 'tree', 'main', 'origin', frozenset(), ModuleCoverage.COMPLETE)
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
        source = mac_linux.CandidateSource('head', 'commit', 'tree', 'main', 'origin', frozenset(), ModuleCoverage.COMPLETE)
        request = cast(mac_linux.GuestRequest, cast(object, {}))
        with self.assertRaises(mac_linux.GuestTookNoRun) as refusal:
            _ = mac_linux.completed_run(reply, request, source)
        self.assertIn(f"; reply start '{'a' * 200}'; SSH or source stream exited 0: fixture", str(refusal.exception))
        self.assertNotIn('b', str(refusal.exception).partition('reply start')[2])

    def test_bad_byte_count_before_the_start_line_took_no_run_and_after_it_refuses(self) -> None:
        attempt = 'fixture-attempt'
        send = mac_linux.BundleSend('', '', mac_linux.BundleForm.FIRST, mac_linux.BundleReason.FIRST_RUN)
        for stderr, error in ((b'fixture stderr\n', mac_linux.GuestTookNoRun),
                              (mac_linux.run_start_line(attempt).encode() + b'\nfixture stderr\n',
                               mac_linux.GuestUnavailable)):
            with self.subTest(started=error is mac_linux.GuestUnavailable):
                stream = subprocess.CompletedProcess[bytes]([], 0, b'{"reply": 1}\nno count\xff\n', stderr)
                with self.assertRaises(mac_linux.GuestUnavailable) as refusal:
                    _ = mac_linux.stream_reply(stream, attempt, 10, send, 'Guest cleanup: ended.')
                self.assertIs(type(refusal.exception), error)
                self.assertTrue(str(refusal.exception).startswith(
                    "the guest stream gave no byte count ('no count\ufffd'); SSH or source stream exited 0: "),
                    str(refusal.exception))

    def test_copy_can_retry_after_a_partial_upload(self) -> None:
        main = self.guest_home / '.claude'
        self.assertIsInstance(self.route(), GateRun)
        _ = (self.repo / 'scripts/fixture/test_added.py').touch()
        reason = self.not_started(self.route('disconnect'))
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
        self.assertNotEqual(first_attempt, retry_attempt)
        self.assertNotEqual(first_claim, retry_claim)
        self.assertEqual(claim_state.read_claims(paths), ())
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

    def test_guest_refuses_when_the_type_check_tool_is_missing(self) -> None:
        missing = self.root / 'missing-basedpyright'
        with patch.dict(os.environ, {'GATE_BASEDPYRIGHT': str(missing)}):
            reason = self.not_started(self.route(type_dirs=('scripts',)))
        self.assertTrue(reason.startswith('SSH or source stream exited 1:'), reason)
        self.assertIn(f'The guest cannot find the type-check tool {missing}', reason)
        self.assertIn('Guest cleanup: ended', reason)
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
