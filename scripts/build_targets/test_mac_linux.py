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

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED
from scripts.build_targets import mac_linux
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
shell = shlex.split(command)[-1]
cleanup = 'flock -n -E 75' in shell
with Path(os.environ['FIXTURE_SSH_TRACE']).open('a') as trace:
    trace.write(('cleanup' if cleanup else 'run') + '\\n')
home = Path(os.environ['FIXTURE_GUEST_HOME'])
if not cleanup and os.environ['FIXTURE_GUEST_RESULT'] == 'offline':
    print('fixture SSH refused the connection', file=sys.stderr)
    sys.exit(255)
if not cleanup and os.environ['FIXTURE_GUEST_RESULT'] == 'disconnect':
    match = re.search(r'^run="\\$HOME"/(mac-linux-[a-f0-9]+)$', shell, re.M)
    assert match
    (home / match[1]).mkdir()
    print('fixture SSH lost its connection during upload', file=sys.stderr)
    sys.exit(255)
sys.exit(subprocess.call(['bash', '-c', shell], env=dict(os.environ, HOME=str(home))))
'''


GATE_FIXTURE = '''import os
from pathlib import Path
import subprocess

from scripts.production.gate_modules import discover_test_modules
from scripts.production.gate_routes import GateJobResult, GateRun, SelectedModules, SuiteMode


def checked_git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


def WholeMachine():
    return None


def run_suites(repo, *, full, mode, machine, selected, module_time_limit_s, time_limit):
    assert Path.home() == repo.parent / 'home'
    assert (Path.home() / '.claude/config').resolve() == repo / 'config'
    assert os.environ['USER'] == os.environ['LOGNAME']
    assert os.environ['USER'] == os.environ['FIXTURE_GUEST_USER']
    assert not {'SSH_AUTH_SOCK', 'GH_TOKEN', 'XDG_CONFIG_HOME', 'CONF_DIR'} & os.environ.keys()
    assert os.cpu_count() == 2 and time_limit.seconds == 7 and module_time_limit_s == 3
    assert checked_git(repo, 'show', 'origin/main:history.txt') == 'old source'
    assert checked_git(repo, 'log', '-1', '--format=%s') in ('candidate', 'Mac Linux gate snapshot')
    assert full == (mode is SuiteMode.GATE)
    print('A guest log line must stay outside the JSON result.')
    scenario = os.environ['FIXTURE_GUEST_RESULT']
    if scenario == 'runner-failure':
        raise RuntimeError('fixture guest runner failed')
    timing = dict(wall_s=1.3, cpu_s=1.0, tests_s=0.7, tests_run=3, failures=0, errors=0, skipped=1, tests=[])
    paths = [(suite, path) for suite, path in discover_test_modules(repo)
             if not isinstance(selected, SelectedModules) or str(path.relative_to(repo)) in selected.paths]
    jobs = tuple(GateJobResult(suite, path.name, int(scenario == 'red'), 1.3, 'fixture module output',
                               cpu_seconds=1.0, times=timing, peak_mib=22) for suite, path in paths)
    return GateRun(jobs, 2.0, 30, selection_certified=isinstance(selected, SelectedModules), executions=jobs)
'''


GUEST_BOOTSTRAP = '''from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import runpy
import sys

from scripts.aws_target import gate_box, ledger

os.chdir(sys.argv[1])
spec = spec_from_file_location('scripts.production.gate', Path('scripts/production/gate.py'))
gate = module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
runpy.run_path(sys.argv[2], run_name='__main__')
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
            'kind.mac-linux=ssh\nplatform.mac-linux=linux\nsettings.mac-linux=mac_linux.conf\nenabled.mac-linux=on\n',
            encoding='utf-8')
        _ = (config / 'mac_linux.conf').write_text(
            f'ssh_path=fixture-mac fixture-guest\naccount={account}\njobs=2\nrun_time_limit_seconds=7\n',
            encoding='utf-8')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            'CONF_DIR': str(config), 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
            'FIXTURE_SSH_TRACE': str(self.trace), 'FIXTURE_GUEST_HOME': str(self.guest_home),
            'FIXTURE_GUEST_USER': account, 'FIXTURE_GUEST_RESULT': 'pass',
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
        _ = self.git('remote', 'add', 'origin', 'https://example.test/repo.git')
        _ = self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        for folder in ('config', 'scripts/lib', 'scripts/production', 'scripts/fixture'):
            (self.repo / folder).mkdir(parents=True)
        _ = (self.repo / 'config/fixture.conf').touch()
        _ = (self.repo / 'scripts/production/gate.py').write_text(GATE_FIXTURE, encoding='utf-8')
        for module in ('test_first.py', 'test_second.py'):
            (self.repo / 'scripts/fixture' / module).touch()
        package_root = shlex.quote(str(Path(__file__).resolve().parents[2]))
        self.executable(self.repo / 'scripts/lib/py', '#!/usr/bin/env bash\n'
                        + f'candidate="$PWD"\ncd {package_root}\nexport PYTHONPATH={package_root}\n'
                        + f'exec {shlex.quote(sys.executable)} -B "$1" "$2"'
                        + f' -c {shlex.quote(GUEST_BOOTSTRAP)} "$candidate" "$3"\n')
        _ = self.git('add', '.')
        _ = self.git('commit', '-qm', 'candidate')

    @staticmethod
    def executable(path: Path, content: str) -> None:
        _ = path.write_text(content, encoding='utf-8')
        path.chmod(0o755)

    def git(self, *args: str) -> str:
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True, timeout=10).strip()

    def route(self, scenario: str = 'pass', selected: AllModules | TreeSelection = ALL_MODULES) -> GateRun | str:
        mode = SuiteMode.GATE if isinstance(selected, AllModules) else SuiteMode.TARGETED
        with patch.dict(os.environ, {'FIXTURE_GUEST_RESULT': scenario}):
            return mac_linux.route(self.repo, mode=mode, full=mode is SuiteMode.GATE, selected=selected,
                                   module_time_limit_s=3, gate_job=GATE_JOB_NOT_RECORDED)

    def assert_guest_clean(self, events: list[str]) -> None:
        self.assertEqual(list(self.guest_home.iterdir()), [])
        self.assertEqual(self.trace.read_text(encoding='utf-8').splitlines(), events)

    def test_switch_off_returns_local_reason_without_ssh(self) -> None:
        _ = self.registry.write_text('enabled.mac-linux=off\n', encoding='utf-8')
        self.assertEqual(self.route(), 'mac-linux is off (build_targets.conf enabled.mac-linux=off)')
        self.assert_guest_clean([])

    def test_transport_failure_returns_reason(self) -> None:
        result = self.route('offline')
        self.assertIsInstance(result, str)
        self.assertIn('fixture SSH refused the connection', str(result))
        self.assert_guest_clean(['run', 'cleanup'])

    def test_guest_returns_module_metrics(self) -> None:
        result = self.route()
        assert isinstance(result, GateRun), result
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
        self.assert_guest_clean(['run'])

    def test_red_module_returns_a_failed_gate(self) -> None:
        result = self.route('red')
        assert isinstance(result, GateRun), result
        self.assertFalse(result.passed)
        self.assertEqual([job.returncode for job in result.jobs], [1, 1])
        self.assert_guest_clean(['run'])

    def test_guest_runner_failure_returns_reason_and_cleans(self) -> None:
        result = self.route('runner-failure')
        self.assertIsInstance(result, str)
        self.assertIn('fixture guest runner failed', str(result))
        self.assert_guest_clean(['run', 'cleanup'])

    def test_cleanup_removes_a_partial_upload(self) -> None:
        result = self.route('disconnect')
        self.assertIsInstance(result, str)
        self.assertIn('lost its connection during upload', str(result))
        self.assert_guest_clean(['run', 'cleanup'])

    def test_selected_modules_keep_subset_coverage(self) -> None:
        tree = self.git('rev-parse', 'HEAD^{tree}')
        result = self.route(selected=TreeSelection(('scripts/fixture/test_second.py',), tree))
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual(result.coverage, ModuleCoverage.SUBSET)
        self.assertEqual([job.module for job in result.jobs], ['test_second.py'])
        self.assert_guest_clean(['run'])

    def test_dirty_modules_are_included_in_the_snapshot(self) -> None:
        (self.repo / 'scripts/fixture/test_added.py').touch()
        result = self.route()
        assert isinstance(result, GateRun), result
        self.assertTrue(result.passed)
        self.assertEqual([job.module for job in result.jobs], ['test_added.py', 'test_first.py', 'test_second.py'])
        self.assert_guest_clean(['run'])


if __name__ == '__main__':
    _ = unittest.main()
