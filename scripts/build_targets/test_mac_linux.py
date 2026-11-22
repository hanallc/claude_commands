"""Test the Mac Linux route with a local SSH fixture."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest
from unittest.mock import patch

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_targets import mac_linux
from scripts.lib import build_folders
from scripts.production import gate
from scripts.production.gate_routes import ALL_MODULES, AllModules, GateRun, ModuleCoverage, SuiteMode, TreeSelection


SSH_FIXTURE = '''import os
from pathlib import Path
import re
import shlex
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
cleanup = 'numbered copy lock is free' in shell
probe = not cleanup and 'bundle unbundle -' not in shell
with Path(os.environ['FIXTURE_SSH_TRACE']).open('a') as trace:
    trace.write(('cleanup' if cleanup else 'probe' if probe else 'run') + '\\n')
home = Path(os.environ['FIXTURE_GUEST_HOME'])
if os.environ['FIXTURE_GUEST_RESULT'] == 'offline':
    print('fixture SSH refused the connection', file=sys.stderr)
    sys.exit(255)
environment = dict(os.environ, HOME=str(home))
if cleanup:
    sys.exit(subprocess.call(['bash', '-c', shell], stdin=subprocess.DEVNULL, env=environment))
if probe:
    assert 'mkdir' not in shell
    if 'FIXTURE_PROBED_MAIN' in os.environ:
        print(os.environ['FIXTURE_PROBED_MAIN'])
        sys.exit(0)
    sys.exit(subprocess.call(['bash', '-c', shell], stdin=subprocess.DEVNULL, env=environment))
assert 'mkdir -p -- "$checkout"' in shell
assert 'exec 9< .' in shell
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
if os.environ['FIXTURE_GUEST_RESULT'] == 'disconnect':
    payload = payload[:-1]
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
        descriptor, = copies.COMMAND_LOCKS.get()
        assert os.path.samestat(os.fstat(descriptor), os.fstat(lock.fileno()))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError('The numbered copy is not locked.')
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
import socket
import sys

from scripts.aws_target import gate_box, ledger

os.chdir(sys.argv[1])
socket.gethostname = lambda: 'nixos'
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

    def test_local_timeout_stops_the_recorded_group_and_confirms_the_copy_lock(self) -> None:
        configuration = self.registry.with_name('mac_linux.conf')
        _ = configuration.write_text(configuration.read_text().replace('seconds=7', 'seconds=30'))
        with (patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '30'}),
              patch.object(mac_linux.GuestSettings, 'transfer_timeout_seconds', 6)):
            result = self.route('hang')
        self.assertIsInstance(result, str)
        group = (self.root / 'run.group').read_text()
        self.assertIn('SSH or guest run exceeded 6 seconds', str(result))
        self.assertIn(f'stopped guest process group {group}; numbered copy lock is free', str(result))
        self.assertEqual(self.trace.read_text().splitlines(), ['probe', 'run', 'cleanup'])

    def test_guest_deadline_stops_a_hung_runner(self) -> None:
        configuration = self.registry.with_name('mac_linux.conf')
        _ = configuration.write_text(configuration.read_text().replace('seconds=7', 'seconds=3'))
        with patch.dict(os.environ, {'FIXTURE_RUN_SECONDS': '3'}):
            result = self.route('hang')
        self.assertIsInstance(result, str)
        self.assertIn('SSH or source stream exited 124', str(result))
        self.assertIn('numbered copy lock is free', str(result))
        self.assert_guest_clean('probe', 'run', 'cleanup')

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
        self.assert_guest_clean('probe', 'run')

    def test_red_module_returns_a_failed_gate(self) -> None:
        result = self.route('red')
        assert isinstance(result, GateRun), result
        self.assertFalse(result.passed)
        self.assertEqual([job.returncode for job in result.jobs], [1, 1])
        self.assert_guest_clean('probe', 'run')

    def test_guest_runner_failure_returns_reason_and_cleans(self) -> None:
        result = self.route('runner-failure')
        self.assertIsInstance(result, str)
        self.assertIn('fixture guest runner failed', str(result))
        self.assertIn('numbered copy lock is free', str(result))
        self.assert_guest_clean('probe', 'run', 'cleanup')

    def test_copy_can_retry_after_a_partial_upload(self) -> None:
        main = self.guest_home / '.claude'
        self.assertIsInstance(self.route(), GateRun)
        _ = (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route('disconnect')
        self.assertIsInstance(result, str)
        self.assertIn('SSH or source stream exited', str(result))
        self.assertEqual((main / 'history.txt').read_text(encoding='utf-8'), 'old source\n')
        self.assertFalse((main / 'scripts/fixture/test_added.py').exists())
        self.assert_guest_clean('probe', 'run', 'probe', 'run', 'cleanup')
        retry = self.route()
        assert isinstance(retry, GateRun), retry
        self.assertTrue(retry.passed)
        self.assert_guest_clean('probe', 'run', 'probe', 'run', 'cleanup', 'probe', 'run')

    def test_copy_uses_the_job_unit(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_EXPECTED_UNIT': 'checkpoint-owner'}):
            result = self.route(gate_job=StepJob('integrate', unit='checkpoint-owner'))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assert_guest_clean('probe', 'run')

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
        self.assert_guest_clean('probe', 'run', 'probe', 'run')

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
        self.assert_guest_clean('probe', 'run', 'probe', 'run')

    def test_guest_missing_prerequisite_retries_once_with_a_full_bundle(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        with patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': self.git('rev-parse', 'origin/main')}):
            result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.GUEST_LACKS_PREREQUISITE,
                           'delta', 'first')
        self.assert_guest_clean('probe', 'run', 'run')

    def test_unknown_guest_main_uses_full_bundle_with_cause(self) -> None:
        with patch.dict(os.environ, {'FIXTURE_PROBED_MAIN': 'f' * 40}):
            result = self.route()
        assert isinstance(result, mac_linux.MacLinuxGateRun), result
        self.assert_bundle(result, mac_linux.BundleForm.FIRST, mac_linux.BundleReason.SOURCE_LACKS_BASE, 'first')
        self.assert_guest_clean('probe', 'run')

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
        self.assert_guest_clean('probe', 'run')

    def test_dirty_modules_are_included_in_the_snapshot(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual([job.module for job in result.jobs], ['test_added.py', 'test_first.py', 'test_second.py'])
        self.assert_guest_clean('probe', 'run')


if __name__ == '__main__':
    _ = unittest.main()
