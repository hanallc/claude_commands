"""Exercise the Mac gate route and the module list with private fixtures."""
from __future__ import annotations

from dataclasses import replace
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import final, override
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box
from scripts.buildlog import store
from scripts.copies import copies
from scripts.lib import scratch
from scripts.lib.git_test_support import commit_repository
from scripts.lib.process_start import Started, process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import mac_gate
from scripts.production import merge_gate as gate
from scripts.production.gate_modules import GateRefused
from scripts.test_times import gate_record

FAKE_QUEUE = """\
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
assert args[args.index('--kind') + 1] == 'run'
assert args.count('--no-wait') == 1
assert Path(args[args.index('--lifecycle-script') + 1]).is_file()
folder = Path(args[-2])
request = json.loads((folder / 'request.json').read_text())
assert (folder / 'module_seconds.json').is_file()
assert (folder / 'source.bundle').is_file() == bool(request['source_key'])
case = os.environ['MAC_GATE_CASE']
if case == 'timeout':
    time.sleep(60)
if case == 'busy':
    sys.exit(78)
if case == 'lost':
    sys.exit(0)
if case == 'status':
    sys.exit(23)

row = {key: request[key] for key in ('attempt', 'tree', 'commit')}
row['gate'] = json.loads(Path(os.environ['MAC_GATE_RESULT']).read_text())
if case in ('attempt', 'commit', 'tree'):
    row[case] = 'wrong'
if case == 'modules':
    row['gate']['jobs'][0]['module'] = 'test_wrong.py'
print('mac_gate_result=' + json.dumps(row))
print('mac_exit=0')
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
            return mac_gate.route(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                  module_time_limit_s=30, selected=selection)

    def test_returns_pass_and_test_failure(self) -> None:
        for status in (0, 1):
            with self.subTest(status=status):
                job = module_job('one', status=status)
                result = self.run_case('completed', job)
                assert isinstance(result, gate.GateRun), result
                self.assertEqual(result.jobs, (job,))
                self.assertEqual(result.passed, status == 0)

    def test_queue_failure_uses_natedev(self) -> None:
        for case, reason in (('status', 'status 23'), ('busy', 'busy'), ('timeout', 'exceeded'),
                             ('lost', 'no unique gate result')):
            with self.subTest(case=case):
                result = self.run_case(case, module_job('one'))
                assert isinstance(result, str), result
                self.assertIn(reason, result)

    def test_identity_mismatch_uses_natedev(self) -> None:
        for case, reason in (('attempt', 'attempt'), ('tree', 'tree'), ('commit', 'commit'),
                             ('modules', 'module list')):
            with self.subTest(case=case):
                result = self.run_case(case, module_job('one'))
                assert isinstance(result, str), result
                self.assertIn(reason, result)

    def test_prepare_bundles_only_commits_the_mac_lacks(self) -> None:
        tip = commit_repository(self.repo, 'source branch', allow_empty=True)
        for present in (True, False):
            with self.subTest(present=present):
                folder = self.root / f'mac-gate-fixture_{int(present)}'
                argv_file = self.root / f'argv_{int(present)}.json'
                _ = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
                _ = (folder / 'local-repo').write_text(str(self.repo))
                _ = (folder / 'module_seconds.json').write_text('{}')

                checkout = str(self.root / 'mac scratch')

                def reply(command: list[str], *, text: bool) -> str:
                    self.assertTrue(text)
                    remote = command[-1]
                    if 'cat-file' in remote:
                        return 'present' if present else ''
                    if ' clone ' in remote:
                        self.assertIn('fixture-owner fixture-project', remote)
                        return f'mac_gate_checkout={checkout}'
                    return 'created'

                sends: list[list[str]] = []

                def send(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(check)
                    sends.append(command)
                    return subprocess.CompletedProcess(command, 0)

                lease = self.root / 'lease'
                with (mock.patch.dict(os.environ, {'MAC_QUEUE_HOST': 'fixture-mac'}),
                      mock.patch.object(copies, 'source_owner', return_value='fixture-owner'),
                      mock.patch.object(copies, 'repository_name', return_value='fixture-project'),
                      mock.patch.object(copies, 'remote_lease_path', return_value=lease),
                      mock.patch.object(copies, 'register_remote') as register,
                      mock.patch.object(subprocess, 'check_output', side_effect=reply),
                      mock.patch.object(subprocess, 'run', side_effect=send)):
                    self.assertEqual(mac_gate.lifecycle('prepare', argv_file, folder, '1'), 0)
                register.assert_called_once()
                self.assertEqual(argv_file.with_suffix('.json.checkout').read_text(), checkout)
                self.assertEqual(argv_file.with_suffix('.json.lease').read_text(), str(lease))
                sent = sends[1]
                self.assertIn(str(folder / 'request.json'), sent)
                self.assertEqual(str(folder / 'source.bundle') in sent, not present)
                self.assertIn(' stage ', argv_file.read_text())
                self.assertIn(' stop ', argv_file.with_suffix('.json.close').read_text())
                if not present:
                    self.assertIn(tip, gate.checked_git(self.repo, 'bundle', 'list-heads', str(folder / 'source.bundle')))

    def test_stage_gets_the_tip_and_base_without_fetching_origin(self) -> None:
        repo = self.root / 'mac-checkout'
        base = commit_repository(self.repo, 'new base', allow_empty=True)
        _ = gate.checked_git(self.repo, 'update-ref', 'refs/remotes/origin/main', base)
        _ = gate.checked_git(self.repo, 'clone', '--quiet', str(self.repo), str(repo))
        tip = commit_repository(self.repo, 'new commit', allow_empty=True)
        origin = 'git@fixture.invalid:fixture/project.git'
        _ = gate.checked_git(self.repo, 'remote', 'set-url', 'origin', origin)
        folder = self.root / 'mac-gate-fixture'
        _ = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, 30)
        cloud_gate.save(folder / 'checkout.json', {'path': str(repo)})
        with mock.patch.object(os, 'execv') as start_guest:
            mac_gate.stage_checkout(folder, '1')
        start_guest.assert_called_once()
        self.assertEqual(gate.checked_git(repo, 'rev-parse', 'HEAD'), tip)
        self.assertEqual(gate.checked_git(repo, 'rev-parse', 'origin/main'), base)
        self.assertEqual(gate.checked_git(repo, 'remote', 'get-url', 'origin'), origin)

    def test_cleanup_stops_the_guest_and_its_recorded_group(self) -> None:
        root = self.root / mac_gate.REMOTE_ROOT / 'mac-gate-fixture'
        root.mkdir(parents=True)
        guest, job, unrelated = (self.sleeping_process() for _ in range(3))
        owner = process_start(guest.pid)
        assert isinstance(owner, Started)
        recovery = gate.GateRecovery(root / 'run.json', str(self.repo), guest.pid, owner.start)
        recovery.add_group(job.pid)
        self.assertIn(job.pid, recovery.groups)
        with (mock.patch.object(Path, 'home', return_value=self.root),
              mock.patch.object(scratch, 'end', return_value=scratch.TaskCleanup()) as end):
            mac_gate.stop_checkout(root)
        end.assert_called_once_with('gate:mac-gate-fixture')
        self.assertEqual(guest.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGTERM)
        self.assertEqual(job.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGTERM)
        self.assertIsNone(unrelated.poll())
        self.assertFalse(root.exists())

    def sleeping_process(self) -> subprocess.Popen[bytes]:
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True, cwd=self.root)
        self.addCleanup(process.wait, TEST_HANG_GUARD_SECS)
        self.addCleanup(process.kill)
        return process

    def test_missing_empty_and_absent_module_list_entries(self) -> None:
        path = self.repo / mac_gate.MODULE_LIST
        self.assertEqual(mac_gate.read_modules(self.repo), ())
        _ = path.write_text('\n')
        self.assertEqual(mac_gate.read_modules(self.repo), ())
        _ = path.write_text('scripts/fixture/test_absent.py\nscripts/fixture/test_one.py\n')
        self.assertEqual(mac_gate.read_modules(self.repo), ('scripts/fixture/test_one.py',))

    def test_list_writer_requires_matching_counts_and_passes_on_both_hosts(self) -> None:
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
        self.assertEqual((self.repo / mac_gate.MODULE_LIST).read_text(), 'scripts/fixture/test_one.py\n')

    def test_list_writer_refuses_a_record_from_another_tree(self) -> None:
        result = self.root / 'mac.json'
        _ = result.write_text(json.dumps({'tree': self.tree, 'gate': gate_box.encode_run(gate.GateRun((), 1, 0))}))
        baseline_path = self.root / 'natedev.jsonl'
        store.append_line(baseline_path, {'kind': 'gate_run', 'host': 'natedev', 'tree': 'wrong',
                                         'selection': 'full', 'dirty': False, 'aborted': False})
        with self.assertRaisesRegex(GateRefused, 'No complete natedev gate record'):
            mac_gate.write_modules(self.repo, result, baseline_path)
        self.assertFalse((self.repo / mac_gate.MODULE_LIST).exists())
