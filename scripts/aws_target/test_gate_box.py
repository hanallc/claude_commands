"""Test the shared guest and the disposable resource definitions."""
from __future__ import annotations

from collections.abc import Callable
import base64
import inspect
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from typing import Never, Self, cast, override
import unittest
from unittest import mock

from scripts.aws_target import aws_trial, cloud_gate, gate_box, ledger
from scripts.aws_target import test_aws_trial as trial_tests
from scripts.aws_target.ledger_test_support import write_gate_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import gate
from scripts.test_times import gate_record


ROOT = Path(__file__).resolve().parents[2]
TRANSFER_MARKER = 'fixture-transfer-query-marker'


class GateBoxTests(unittest.TestCase):
    def test_paired_guest_uploads_warm_up_round_and_finish_stages_in_run_order(self) -> None:
        fixture = trial_tests.PairedTrialTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        check_transfers = '''import subprocess
run = subprocess.run

def check_transfer(args, *positional, **options):
    if args[0] == 'curl':
        assert args == ['curl', '--disable', '--config', args[3], '--fail', '--silent',
                        '--write-out', '%{http_code}', '--max-time', '30',
                        '-X', 'PUT', '--data-binary', '@-', '-o', '/dev/null'], args
    return run(args, *positional, **options)

subprocess.run = check_transfer
'''
        with mock.patch.object(gate_box, 'GUEST', check_transfers + gate_box.GUEST):
            reply, _ = fixture.guest()
        self.assertNotIn('log_error', reply)
        log = (fixture.root / 'box/uploaded-log').read_text()
        lines = [line for line in log.splitlines() if ' stage=' in line]
        self.assertEqual([line.split(' stage=', 1)[1] for line in lines], [
            'source ready', 'warm-up base', 'warm-up changed', 'round 1 of 3 base', 'round 1 of 3 changed',
            'round 2 of 3 changed', 'round 2 of 3 base', 'round 3 of 3 base', 'round 3 of 3 changed', 'finish'])
        for line in lines:
            self.assertRegex(line, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ stage=')
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertTrue(all(set(row) == gate_box.RUN_FIELDS for row in runs))
        self.assertEqual(sum(line.startswith('{"module":') for line in log.splitlines()), 6)

    def test_paired_guest_refuses_a_link_or_a_different_git_root_before_reset(self) -> None:
        for commands, detail in (
            (("(root / 'base').rename(root / 'saved-base')",
              "(root / 'base').symlink_to(root / 'saved-base', target_is_directory=True)"), 'without links'),
            (("alias = root.parent / 'box-alias'",
              "alias.symlink_to(root, target_is_directory=True)", "root = alias"), 'without links'),
            (("(root / 'base/.git').rename(root / 'saved-git-file')",
              "subprocess.run(['git', 'init', '-q', str(root)], check=True)"), 'working tree root'),
        ):
            with self.subTest(commands=commands):
                fixture = trial_tests.PairedTrialTests()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                setup = "        (root / 'base/keep').write_text('fixture output')\n"
                setup += ''.join('        ' + command + '\n' for command in commands)
                stage = "        stage('source ready')"
                guest = gate_box.GUEST.replace(stage, setup + stage, 1)
                with mock.patch.object(gate_box, 'GUEST', guest):
                    reply, traces = fixture.guest()
                self.assertEqual(reply['verdict'], 'error')
                error = ledger.text(reply, 'error')
                self.assertIn('Cannot reset', error)
                self.assertIn(detail, error)
                self.assertEqual(traces, [])
                self.assertEqual((fixture.root / 'box/base/keep').read_text(), 'fixture output')

    def test_paired_log_put_removes_signed_urls_from_errors(self) -> None:
        fixture = trial_tests.PairedTrialTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        inject = '''
with (root / 'boot.log').open('a') as boot:
    os.dup2(boot.fileno(), 2)
run = subprocess.run
def fail_log_put(args, *positional, **options):
    if args[0] == 'curl':
        config = pathlib.Path(args[args.index('--config') + 1])
        if json.loads(config.read_text().partition('= ')[2]) == links['log']:
            return subprocess.CompletedProcess(args, 22, stdout=b'403')
    return run(args, *positional, **options)
subprocess.run = fail_log_put
'''
        load_links = "links = json.loads((root / 'links.json').read_text())\n"
        signed = 'https://fixture.invalid/gate.log?signature=' + TRANSFER_MARKER
        guest = gate_box.GUEST.replace(load_links, load_links + f"links['log'] = {signed!r}\n" + inject, 1)
        with mock.patch.object(gate_box, 'GUEST', guest):
            reply, _ = fixture.guest()
        box = fixture.root / 'box'
        for path in (box / 'uploaded-result', box / 'gate.log', box / 'boot.log'):
            self.assertFalse(TRANSFER_MARKER in path.read_text(), path.name)
        self.assertEqual(reply['verdict'], 'error')
        for field in ('error', 'log_error'):
            detail = ledger.text(reply, field)
            self.assertIn('PUT log failed (curl exit 22, HTTP 403)', detail)
            self.assertNotIn('CalledProcessError', detail)

    def test_paired_guest_stage_write_errors_keep_all_runs_and_pass_verdict(self) -> None:
        failure = '''class FailedStageWrite:
    def __enter__(self):
        return self
    def __exit__(self, *error):
        pass
    def write(self, text):
        (root / 'failed-stage-write').write_text(text)
        raise OSError('The stage write failed')

'''
        for name in ('warm-up base', 'finish'):
            with self.subTest(stage=name):
                fixture = trial_tests.PairedTrialTests()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                path = fixture.repo / 'scripts/aws_target/gate_box.py'
                source = path.read_text().replace('def stage(name):', failure + 'def stage(name):')
                source = source.replace("(root / 'boot.log').open('a')",
                    f"(FailedStageWrite() if name == {name!r} else (root / 'boot.log').open('a'))")
                _ = path.write_text(source)
                _ = aws_trial.checked_git(fixture.repo, 'add', '.')
                _ = aws_trial.checked_git(fixture.repo, 'commit', '-qm', 'Fail the candidate stage writer')
                fixture.changed = aws_trial.checked_git(fixture.repo, 'rev-parse', 'HEAD')
                _ = aws_trial.checked_git(fixture.repo, 'push', str(fixture.origin), 'HEAD:refs/heads/stage')
                reply, traces = fixture.guest()
                self.assertIn(' stage=' + name + '\n', (fixture.root / 'box/failed-stage-write').read_text())
                self.assertEqual(len(traces), 8)
                runs = ledger.rows(reply, 'runs')
                self.assertEqual([(row['round'], row['variant']) for row in runs],
                                 [(1, 'base'), (1, 'changed'), (2, 'changed'), (2, 'base'), (3, 'base'), (3, 'changed')])
                self.assertTrue(all(row['status'] == 0 for row in runs))
                self.assertEqual(reply['verdict'], 'pass')
                self.assertNotIn('error', reply)

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        write_gate_config(root / 'cloud_gate.conf')
        trial_tests.write_guest_config(root, slow_test_s=0.5)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'CONF_DIR': str(root)})))

    def test_trial_and_route_share_one_guest(self) -> None:
        self.assertIs(aws_trial.GUEST, gate_box.GUEST)
        _ = compile(gate_box.GUEST, '<gate guest>', 'exec')
        self.assertNotIn('basedpyright', gate_box.GUEST)
        self.assertNotIn('merge_gate', gate_box.GUEST)
        self.assertIn('gate_runner.SelectedModules', gate_box.CANDIDATE_GUEST)
        self.assertIn("'HEAD^{tree}'", gate_box.GUEST)
        self.assertIn("'update-ref', 'refs/remotes/origin/main', gate_request['origin_main']", gate_box.GUEST)
        self.assertIn("'fetch', str(root / 'source.bundle'), links['commit']", gate_box.GUEST)

    def test_guest_names_no_gate_signal_function(self) -> None:
        for code in (gate_box.GUEST, gate_box.CANDIDATE_GUEST, gate_box.PAIRED_GUEST):
            self.assertNotIn('rollback_signals', code)
            self.assertNotRegex(code, r'\bgate_runner\.\w*signal\w*')
            self.assertNotIn('signal.signal', code)

    def test_term_during_guest_sampler_cleanup(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stop_timeout = 2.0
        result: dict[str, object] = {}

        def stage(_name: str) -> None:
            pass

        namespace: dict[str, object] = {
            'root': root, 'log': root / 'gate.log', 'result': result,
            'stage': stage,
            'links': {'module_time_limit_s': 60, 'program_stop_seconds': stop_timeout},
        }

        def cleanup(hang: Callable[[], Never]) -> int:
            def join(*, timeout: float) -> Never:
                self.assertEqual(timeout, stop_timeout)
                hang()

            with (mock.patch.object(gate, 'run_suites', return_value=SimpleNamespace(passed=True, jobs=())),
                  mock.patch.object(threading.Thread, 'join', side_effect=join)):
                exec(gate_box.CANDIDATE_GUEST, namespace)
            return 0

        trial_tests.assert_term_during_cleanup(self, cleanup, stop_timeout)

    def test_boot_timestamps_use_local_kernel_and_cached_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / 'links.json').write_text('{}')
            metadata = root / 'metadata.json'
            cloud_init = root / 'instance-data.json'
            stat = root / 'stat'
            _ = stat.write_text('cpu 1 2 3\nbtime 1768438700\n')
            prefix = gate_box.GUEST.split("links['boot'] =", 1)[0].replace(
                "'/proc/stat'", repr(str(stat))).replace(
                "'/etc/ec2-metadata/instance-identity-document'", repr(str(metadata))).replace(
                "'/run/cloud-init/instance-data.json'", repr(str(cloud_init)))
            document = {'pendingTime': '2026-01-15T01:00:00Z'}
            for cached in ('absent', 'identity', 'cloud-init', 'invalid'):
                with self.subTest(cached=cached), mock.patch.dict(os.environ,
                        {'GATE_BOX_DIR': str(root), 'GATE_USER_DATA_STARTED_AT': '1768438801'}):
                    metadata.unlink(missing_ok=True)
                    cloud_init.unlink(missing_ok=True)
                    if cached == 'identity':
                        _ = metadata.write_text(json.dumps(document))
                    elif cached == 'cloud-init':
                        _ = cloud_init.write_text(json.dumps({'ds': {'dynamic': {'instance-identity': {'document': document}}}}))
                    elif cached == 'invalid':
                        _ = metadata.write_text('{broken')
                    namespace: dict[str, object] = {}
                    exec(compile(prefix, '<boot timestamps>', 'exec'), namespace)
                    timestamps = ledger.mapping(namespace['timestamps'])
                    self.assertEqual(timestamps['kernel_boot_at'], 1768438700)
                    self.assertEqual(timestamps['user_data_started_at'], 1768438801)
                    self.assertGreater(float(str(timestamps['nix_ready_at'])), 1768438801)
                    if cached in ('identity', 'cloud-init'):
                        self.assertEqual(timestamps['instance_running_at'], 1768438800)
                    else:
                        self.assertNotIn('instance_running_at', timestamps)

    def test_production_boot_has_absolute_expiry_and_no_type_package(self) -> None:
        links = {'production': 'true', 'gate_time_limit_s': '2.5', 'expires': '1800000000', 'step': 'size',
                 'guest': 'https://fixture.invalid/requests/attempt.guest.py', 'request': 'https://fixture.invalid/requests/attempt.json',
                 'result': 'https://fixture.invalid/artifacts/attempt/result.json',
                 'log': 'https://fixture.invalid/artifacts/attempt/gate.log'}
        script = gate_box.user_data(links, 'fixture-revision', 20, 5, 1800)
        self.assertIn('--on-calendar=@1800000000', script)
        self.assertIn('shutdown -h now', script)
        self.assertIn('while sleep 5;', script)
        self.assertNotIn('aws s3 cp', script)
        self.assertNotIn('instance-id', script)
        self.assertNotIn('#awscli2', script)
        self.assertNotIn('#basedpyright', script)
        self.assertIn('#chromium', script)
        self.assertIn('github:NixOS/nixpkgs/fixture-revision#bubblewrap', script)
        self.assertIn('stage=bwrap unavailable: bwrap or unprivileged user namespaces do not work for gate-trial', script)
        self.assertLessEqual(len(script.encode()), 16384)
        result = subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True,
                                timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('base64 -d', script)
        self.assertNotIn(gate_box.GUEST, script)
        for name in ('guest.py', 'request.json'):
            with self.subTest(download=name):
                self.assertIn(f'-o "$box_dir/{name}"', script)
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        self.assertLess(session.index("gate_curl 'GET guest'"), session.index('exec python3'))
        check = session.index('if ! bwrap --ro-bind / / true; then')
        self.assertLess(session.index('stage=tools ready'), check)
        self.assertLess(session.index('runuser -u gate-trial'), check)
        self.assertLess(session.index('export PATH="$tool_path:$PATH"'), check)
        self.assertLess(check, session.index('stage=bwrap ready'))
        self.assertLess(session.index('stage=bwrap ready'), session.index('exec python3'))

    def test_full_gate_downloads_program_and_request_without_inlining_modules(self) -> None:
        modules = [f'scripts/fixture/test_module_{index:04d}.py' for index in range(740)]
        links = {'production': 'true', 'gate_time_limit_s': '2.5', 'expires': '1800000000', 'step': 'size',
                 'attempt': 'a' * 32, 'commit': 'b' * 40, 'tree': 'c' * 40, 'mode': 'gate',
                 'origin': 'https://example.invalid/team/project.git', 'source': 'https://fixture.invalid/source/' + 'd' * 64 + '.bundle',
                 'guest': 'https://fixture.invalid/requests/attempt.guest.py', 'request': 'https://fixture.invalid/requests/attempt.json',
                 'log': 'https://fixture.invalid/artifacts/attempt/gate.log',
                 'result': 'https://fixture.invalid/artifacts/attempt/result.json'}
        script = gate_box.user_data(links, 'f' * 40, 20, 5, 1800)
        self.assertIn('set -eu\numask 077\n', script)
        self.assertLess(len(base64.b64encode(script.encode())), 25600)
        self.assertNotIn('test_module_', script)
        self.assertNotIn('"modules"', script)
        self.assertNotIn(base64.b64encode(gate_box.GUEST.encode()).decode(), script)
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        downloads = session[:session.index('ln -sf')]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / 'source.py').write_text(gate_box.GUEST)
            _ = (root / 'manifest.json').write_text(json.dumps({'modules': modules}))
            curl = root / 'curl'
            _ = curl.write_text('#!/bin/sh\nwhile [ "$1" != "--config" ]; do shift; done\n'
                                + 'config=$2\nwhile [ "$1" != "-o" ]; do shift; done\n'
                                + 'case "$config" in */guest.curl) cp "$FIXTURE/source.py" "$2" ;;'
                                + ' */request.curl) cp "$FIXTURE/manifest.json" "$2" ;; *) exit 1 ;; esac\n')
            curl.chmod(0o700)
            result = subprocess.run(['sh', '-ec', 'umask 077\n' + downloads + 'mkdir "$box_dir/session-directory"\n'],
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'FIXTURE': str(root), 'box_dir': str(root)},
                text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'session-directory').stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / 'guest.py').read_text(), gate_box.GUEST)
            self.assertEqual(json.loads((root / 'request.json').read_text()), {'modules': modules})

    def test_production_boot_stages_and_errors_with_local_stubs(self) -> None:
        self.production_boot_stubs(('', 'boot', 'nix', 'tools shell', 'tools ready', 'bwrap unavailable',
                                    'source ready', 'tests start', 'result upload'))

    def test_transfer_failures_remove_signed_urls_from_boot_and_result(self) -> None:
        self.production_boot_stubs(('source download', 'timings download', 'final log upload',
                                    'paired request download'))

    def test_older_candidate_error_is_safe_in_bootstrap_records(self) -> None:
        self.production_boot_stubs(('',), candidate_change='old transfer')

    def test_signaled_shell_log_put_is_safe_in_boot_log(self) -> None:
        self.production_boot_stubs(('shell log signal',))

    def test_shell_boot_error_filters_the_completed_record(self) -> None:
        self.production_boot_stubs(('boot error record',))

    def test_production_boot_powers_off_when_the_error_record_write_fails(self) -> None:
        self.production_boot_stubs(('boot error write',))

    def test_signed_shell_transfers_suppress_curl_output_and_shell_traces(self) -> None:
        signed = 'https://fixture.invalid/artifact?signature=' + TRANSFER_MARKER
        links = {**dict.fromkeys(('guest', 'request', 'log', 'result', 'boot_error'), signed), 'step': 'size'}
        script = gate_box.user_data(links, 'fixture-revision', 20, 5, 1800)
        self.assertTrue(script.startswith('#!/bin/sh\nset +x +v\n'))
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        self.assertTrue(session.startswith('set +x +v\n'))
        self.assertFalse(TRANSFER_MARKER in session)
        curls = [line for line in script.splitlines() if 'gate_curl ' in line]
        self.assertEqual(len(curls), 6)
        for line in curls:
            self.assertTrue('--config ' in line)
            self.assertFalse(TRANSFER_MARKER in line)
        for line in (line for line in script.splitlines() if '$(curl ' in line):
            self.assertTrue("--write-out " in line and '2>/dev/null' in line)
            self.assertFalse('--show-error' in line)

    def test_candidate_renamed_suite_runner_uses_its_own_entry_point(self) -> None:
        self.production_boot_stubs(('',), candidate_change='rename')

    def test_candidate_accepts_the_affected_suite_mode(self) -> None:
        self.production_boot_stubs(('',), mode=gate.SuiteMode.TARGETED)

    def test_candidate_accepts_the_new_suite_mode_values(self) -> None:
        for mode in gate.SuiteMode:
            with self.subTest(mode=mode):
                self.production_boot_stubs(('transient result upload',), mode=mode, wire=False)

    def test_candidate_without_entry_point_publishes_named_refusal(self) -> None:
        self.production_boot_stubs(('',), candidate_change='missing')

    def test_bootstrap_renders_with_mock_process_memory_mib(self) -> None:
        code = '''from unittest import mock
from scripts.production import gate
with mock.patch.object(gate, 'process_memory_mib'):
    from scripts.aws_target import gate_box
    compile(gate_box.GUEST, '<bootstrap>', 'exec')
    assert 'merge_gate' not in gate_box.GUEST
    assert 'inspect.getsource' not in gate_box.GUEST
'''
        reply = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                               timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(reply.returncode, 0, reply.stdout + reply.stderr)

    def production_boot_stubs(self, failures: tuple[str, ...], *, candidate_change: str = '',
                              mode: gate.SuiteMode = gate.SuiteMode.GATE, wire: bool = True) -> None:
        stages = ['boot', 'tool preparation', 'tools ready', 'bwrap ready', 'tools checked',
                  'source ready', 'tests start', 'result upload']
        boot_errors = {
            'boot': 'The box failed at stage boot (exit 7)',
            'boot error record': 'The box failed at stage boot (exit 7)',
            'nix': 'Tool preparation failed (exit 7)',
            'tools shell': 'Tool preparation failed (exit 7)',
            'tools ready': 'The box failed at stage tools ready (exit 7)',
            'bwrap unavailable': 'stage=bwrap unavailable: bwrap or unprivileged user namespaces do not work for gate-trial (exit 1)',
            'result upload': 'The box failed at stage result upload (exit 1)',
            'paired request download': 'The box failed at stage tools ready (exit 7)',
        }
        for failure in failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                tools, box, repo = root / 'bin', root / 'box', root / 'repo'
                tools.mkdir()
                (root / 'home').mkdir()
                if failure == 'boot error write':
                    (box / 'boot-error.json').mkdir(parents=True)
                for name in ('scripts', 'scripts/production', 'scripts/aws_target', 'scripts/lib', 'config'):
                    folder = repo / name
                    folder.mkdir(parents=True, exist_ok=True)
                    if name != 'config':
                        _ = (folder / '__init__.py').write_text('')
                library = repo / 'scripts/lib'
                _ = (library / 'py').write_text('#!/bin/sh\nexec python3 "$@"\n')
                self.assertFalse((library / 'python_startup/sitecustomize.py').exists())
                _ = (repo / 'scripts/production/gate.py').write_text(
                    'from __future__ import annotations\nfrom types import SimpleNamespace\nfrom enum import Enum\nfrom typing import override\nimport os\n'
                    + inspect.getsource(gate.SuiteMode)
                    + 'WholeMachine = lambda times: times\nSelectedModules = tuple\n'
                    + 'gate_record = SimpleNamespace(GateTimeLimit=lambda seconds: seconds, NoGateTimeLimit=lambda reason: reason)\n'
                    + 'def run_suites(*args, **kwargs):\n'
                    + '    if os.environ["FAIL_STAGE"] == "tests start": raise RuntimeError("fixture tests start failure")\n'
                    + '    with open(os.environ["FIXTURE"] + "/received-limit", "w") as output: output.write(str(kwargs["time_limit"]))\n'
                    + '    with open(os.environ["FIXTURE"] + "/received-mode", "w") as output: output.write(kwargs["mode"].value)\n'
                    + '    return SimpleNamespace(passed=True, jobs=(), seconds=1, peak_memory_mib=0)\n')
                _ = (repo / 'scripts/aws_target/gate_box.py').write_text(
                    'import json\nfrom pathlib import Path\nfrom typing import cast\n'
                    + f'GUEST_TRANSFERS = {gate_box.GUEST_TRANSFERS!r}\n'
                    + f'PAIRED_GUEST = {gate_box.PAIRED_GUEST!r}\n'
                    + f'CANDIDATE_GUEST = {gate_box.CANDIDATE_GUEST!r}\n'
                    + inspect.getsource(gate_box.run_guest)
                    + '\ndef encode_run(run): return {"passed": run.passed}\n'
                    + 'def completion_record(*args, **kwargs): return {"kind": "gate_run"}\n')
                if candidate_change == 'rename':
                    for relative in ('scripts/aws_target/gate_box.py', 'scripts/production/gate.py'):
                        path = repo / relative
                        _ = path.write_text(path.read_text().replace('run_suites', 'candidate_suites'))
                elif candidate_change == 'missing':
                    path = repo / 'scripts/aws_target/gate_box.py'
                    _ = path.write_text(path.read_text().replace('def run_guest(', 'def old_guest('))
                elif candidate_change == 'old transfer':
                    _ = (repo / 'scripts/aws_target/gate_box.py').write_text('''import subprocess
from urllib.parse import urlsplit

def run_guest(root, links, result):
    url = links['source']
    parsed = urlsplit(url)
    partial = parsed.path + '?' + parsed.query
    result['log_error'] = 'Older upload failed for ' + partial
    (root / 'gate.log').write_text('Older transfer log: ' + url + '\\n')
    raise subprocess.CalledProcessError(22, ['curl', url])
''')
                _ = (root / 'source.py').write_text(gate_box.GUEST)
                request_mode = mode.wire if wire else mode.value
                _ = (root / 'request.json').write_text(json.dumps({
                    'clone_url': 'https://example.invalid/repo.git', 'origin_main': 'b' * 40, 'modules': [],
                    'mode': request_mode,
                    'head': 'e' * 40, 'dirty_paths': ['scripts/fixture/test_one.py']}))
                stub = '#!' + sys.executable + '\n' + r'''import json, os, pathlib, shutil, signal, subprocess, sys
from urllib.parse import urlsplit
root = pathlib.Path(os.environ['FIXTURE'])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
def event(value):
    with (root / 'trace').open('a') as output: output.write(value + '\n')
def fail(stage, url=''):
    marker = root / 'failed'
    if os.environ['FAIL_STAGE'] == stage and not marker.exists():
        marker.touch()
        if url:
            parsed = urlsplit(url)
            print('curl: request failed for ' + parsed.path + '?' + parsed.query, file=sys.stderr)
            print('403', end='')
        sys.exit(7)
event(name)
if name == 'nix':
    command = args[args.index('--command') + 1:]
    event('tools shell')
    if os.environ['FAIL_STAGE'] == 'nix': sys.exit(7)
    fail('tools shell')
    sys.exit(subprocess.call(command))
elif name == 'curl':
    config = pathlib.Path(args[args.index('--config') + 1])
    assert config.stat().st_mode & 0o077 == 0
    url = json.loads(config.read_text().partition('= ')[2])
    assert all(url not in word for word in args)
    destination = url.partition('?')[0]
    if destination.endswith('/gate.log') and os.environ['FAIL_STAGE'] == 'shell log signal':
        marker = root / 'signaled'
        if not marker.exists():
            marker.touch()
            os.kill(os.getpid(), signal.SIGTERM)
    retries = int(args[args.index('--retry') + 1]) if '--retry' in args else 0
    if retries:
        assert retries == 2, args
        assert '--retry-all-errors' in args, args
        assert args[args.index('--retry-delay') + 1] == '1', args
    else:
        assert destination.endswith('/gate.log'), args
        assert '--retry-all-errors' not in args and '--retry-delay' not in args, args
    assert args[args.index('--max-time') + 1] == ('30' if '-X' in args else '60'), args
    assert args[args.index('--write-out') + 1] == '%{http_code}'
    if destination.endswith('/result.json'):
        fail('result upload', url)
        if os.environ['FAIL_STAGE'] == 'transient result upload':
            event('result upload failed')
            if not retries:
                sys.exit(7)
            event('result upload retried')
    if '-X' not in args:
        fail('tools ready', url)
        if destination.endswith('/attempt.json'):
            fail('paired request download', url)
        if destination.endswith('/snapshot.bundle'):
            fail('source download', url)
        if destination.endswith('/module_seconds.json'):
            fail('timings download', url)
        shutil.copyfile(root / ('source.py' if destination.endswith('.guest.py') else 'request.json'), args[args.index('-o') + 1])
    else:
        source = args[args.index('--data-binary') + 1]
        body = sys.stdin.read() if source == '@-' else pathlib.Path(source[1:]).read_text()
        if destination.endswith('/gate.log') and 'Cloud gate: uploading' in body:
            fail('final log upload', url)
        event('upload result' if destination.endswith(('result.json', 'boot-error.json')) else 'upload log')
        with (root / 'uploads').open('a') as output:
            output.write(json.dumps({'destination': destination, 'body': body, 'retries': retries}) + '\n')
    print('200', end='')
elif name == 'runuser':
    command = args[args.index('--') + 1:]
    command = ['HOME=' + str(root / 'home') if word.startswith('HOME=') else word for word in command]
    sys.exit(subprocess.call(command))
elif name == 'bwrap':
    assert args == ['--ro-bind', '/', '/', 'true'], args
    fail('bwrap unavailable')
elif name == 'ps':
    if 'etimes,pcpu,rss,args' in args:
        print('1 0.0 0 fixture-process')
        print('1 0.0 0 transfer-wrapper ' + os.environ['FIXTURE_SIGNED_LINK'])
elif name == 'id':
    if not (root / 'user').exists(): sys.exit(1)
    print('1000')
elif name == 'useradd':
    (root / 'user').touch()
elif name == 'git' and args[0] == 'clone':
    fail('source ready')
    print('Cloning the fixture repository.')
    shutil.copytree(root / 'repo', args[-1])
elif name == 'git' and 'fetch' in args:
    event('fetch ' + args[-1])
elif name == 'git' and 'rev-parse' in args:
    print('c' * 40)
elif name == 'loginctl':
    fail('boot')
    fail('boot error record')
    fail('boot error write')
elif name == 'shutdown':
    assert args == ['-h', 'now'], args
    event('shutdown -h now')
elif name in ('systemd-run', 'mount', 'chown', 'ln', 'cp', 'git'):
    pass
else:
    print('fixture-version')
'''
                for name in ('nix', 'runuser', 'systemd-run', 'loginctl', 'useradd', 'mount', 'curl', 'ps',
                             'id', 'git', 'chown', 'ln', 'cp', 'shutdown', 'cargo', 'rustc', 'rustdoc', 'rustup',
                             'cargo-nextest', 'uv', 'sqlite3', 'bwrap'):
                    command = tools / name
                    _ = command.write_text(stub)
                    command.chmod(0o700)
                (tools / 'python3').symlink_to(sys.executable)
                links = {'production': 'true', 'gate_time_limit_s': '2.5', 'expires': '1800000000', 'step': 'size', 'region': 'us-west-2',
                         'attempt': 'attempt', 'commit': 'a' * 40, 'tree': 'c' * 40, 'mode': request_mode,
                         'origin': 'https://example.invalid/repo.git', 'guest': 'https://fixture.invalid/requests/attempt.guest.py',
                         'source': 'https://fixture.invalid/source/snapshot.bundle',
                         'request': 'https://fixture.invalid/requests/attempt.json',
                         'log': 'https://fixture.invalid/artifacts/attempt/gate.log',
                         'result': 'https://fixture.invalid/artifacts/attempt/result.json',
                         'boot_error': 'https://fixture.invalid/artifacts/attempt/boot-error.json'}
                if failure == 'paired request download':
                    links['paired'] = 'true'
                if failure == 'timings download':
                    links['module_seconds'] = 'https://fixture.invalid/artifacts/attempt/module_seconds.json'
                signed = {key: value + '?key=' + key + '&signature=' + TRANSFER_MARKER
                          if key != 'origin' and value.startswith('https://') else value for key, value in links.items()}
                script = gate_box.user_data(signed, 'f' * 40, 20, 5, 1800)
                if failure == 'boot error record':
                    script = script.replace('"error":"%s (exit %s)"', '"error":"%s (exit %s)","detail":"%s"')
                    script = script.replace('"$message" "$status" > "$file"',
                                            '"$message" "$status" "$FIXTURE_SIGNED_LINK" > "$file"')
                self.assertLess(script.index('push_logs &'), script.rindex('nix --extra-experimental-features'))
                self.assertNotIn('#awscli2', script)
                self.assertNotIn('instance-id', script)
                boot_script = root / 'boot.sh'
                _ = boot_script.write_text(script)
                reply = subprocess.run(['bash', '-x', str(boot_script)], text=True, capture_output=True, check=False,
                    timeout=TEST_HANG_GUARD_SECS, cwd=root, env={**os.environ, 'FIXTURE': str(root),
                    'GATE_BOX_DIR': str(box), 'FAIL_STAGE': failure, 'FIXTURE_SIGNED_LINK': signed['log'],
                    'PATH': str(tools) + os.pathsep + os.environ['PATH']})
                log = (box / 'boot.log').read_text()
                uploaded = (root / 'uploads').read_text()
                for output in (log, reply.stdout, reply.stderr, uploaded):
                    self.assertFalse(TRANSFER_MARKER in output, 'A transfer record contains a signed URL')
                self.assertTrue('fixture-process' in uploaded, 'The transfer omitted the process snapshot')
                uploads = [ledger.mapping(cast(object, json.loads(line))) for line in uploaded.splitlines()]
                results = [ledger.mapping(cast(object, json.loads(ledger.text(row, 'body'))))
                           for row in uploads if ledger.text(row, 'destination').endswith(('/result.json', '/boot-error.json'))]
                trace = (root / 'trace').read_text().splitlines()
                if failure == 'boot error write':
                    self.assertIn('boot-error.json:', log)
                    self.assertIn('shutdown -h now', trace)
                    self.assertEqual(reply.returncode, 7, log + reply.stderr)
                    self.assertEqual(results, [])
                    continue
                self.assertTrue(results, log + reply.stderr)
                passed = failure in ('', 'final log upload', 'shell log signal', 'transient result upload')
                expected_error = not passed or candidate_change in ('missing', 'old transfer')
                self.assertEqual(results[-1]['verdict'], 'error' if expected_error else 'pass', log)
                if candidate_change == 'old transfer':
                    error = ledger.text(results[-1], 'error')
                    self.assertIn('CalledProcessError', error)
                    self.assertIn('[URL removed]', error)
                    self.assertFalse(links['source'] in error)
                    self.assertIn('Older upload failed for', ledger.text(results[-1], 'log_error'))
                    self.assertTrue('Older transfer log:' in uploaded)
                    self.assertFalse(TRANSFER_MARKER in (box / 'gate.log').read_text().split('Traceback')[-1])
                if candidate_change == 'missing':
                    self.assertEqual(results[-1]['error'], 'candidate predates guest entry point')
                    refusals = [row for row in uploads if row['destination'] == links['boot_error']]
                    self.assertEqual(len(refusals), 1)
                    self.assertEqual(json.loads(ledger.text(refusals[0], 'body'))['error'],
                                     'candidate predates guest entry point')
                    self.assertNotIn('stage=tests start', log)
                if failure == 'shell log signal':
                    self.assertTrue((root / 'signaled').exists())
                    self.assertIn('PUT log failed (curl exit 143, HTTP 000)', log)
                elif not passed:
                    self.assertIn('error', results[-1])
                if failure in ('source download', 'timings download', 'final log upload'):
                    field = 'log_error' if failure == 'final log upload' else 'error'
                    stage = {'source download': 'GET source', 'timings download': 'GET module_seconds',
                             'final log upload': 'PUT log'}[failure]
                    detail = ledger.text(results[-1], field)
                    self.assertIn(f'{stage} failed (curl exit 7, HTTP 403)', detail)
                    self.assertNotIn('CalledProcessError', detail)
                if failure in ('bwrap unavailable', 'result upload'):
                    self.assertEqual(reply.returncode, 1, log + reply.stderr)
                    self.assertIn('shutdown -h now', trace)
                    self.assertLess(trace.index('upload result'), trace.index('shutdown -h now'))
                if failure in boot_errors:
                    self.assertEqual(results[-1]['error'], boot_errors[failure], log)
                    if failure == 'boot error record':
                        self.assertTrue(TRANSFER_MARKER in (box / 'boot-error.json').read_text())
                        self.assertEqual(results[-1]['detail'], '[URL removed]')
                    self.assertIn('stage=boot', ledger.text(uploads[0], 'body'))
                    self.assertEqual([row['destination'] for row in uploads[-2:]],
                                     [links['log'], links['boot_error']])
                    self.assertEqual([row['retries'] for row in uploads], [2] * (len(uploads) - 2) + [0, 2])
                    if failure == 'result upload':
                        self.assertIn('Traceback', ledger.text(uploads[-2], 'body'))
                    if failure == 'paired request download':
                        self.assertIn('GET request failed (curl exit 7, HTTP 403)', log)
                    if failure == 'bwrap unavailable':
                        self.assertIn('stage=bwrap unavailable: bwrap or unprivileged user namespaces do not work for gate-trial',
                                      ledger.text(uploads[-2], 'body'))
                        self.assertNotIn('stage=tools checked', log)
                        self.assertNotIn('stage=tests start', log)
                else:
                    self.assertTrue(all(row['retries'] == 2 for row in uploads))
                if failure in ('', 'transient result upload') and candidate_change not in ('missing', 'old transfer'):
                    self.assertEqual((root / 'received-limit').read_text(), '2.5')
                    self.assertEqual((root / 'received-mode').read_text(), mode.value)
                    self.assertEqual(reply.returncode, 0, log + reply.stderr)
                    result = results[-1]
                    self.assertGreaterEqual(float(str(result['nix_ready_at'])), int(str(result['user_data_started_at'])))
                    self.assertGreater(int(str(result['user_data_started_at'])), int(str(result['kernel_boot_at'])))
                    self.assertNotIn('source_ready_at', result)
                    self.assertEqual(list(ledger.mapping(results[-1]['tools'])),
                                     ['git', 'python3', 'cargo', 'rustc', 'rustdoc', 'rustup',
                                      'cargo-nextest', 'uv', 'sqlite3', 'python_sqlite'])
                    lines = [line for line in log.splitlines() if ' stage=' in line]
                    self.assertEqual([line.split(' stage=', 1)[1] for line in lines], stages)
                    for line in lines:
                        self.assertRegex(line, r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ stage=')
                    self.assertIn('shutdown -h now', trace)
                    self.assertLess(trace.index('upload result'), trace.index('shutdown -h now'))
                    if failure == 'transient result upload':
                        self.assertEqual([event for event in trace if event.startswith('result upload ')],
                                         ['result upload failed', 'result upload retried'])
                        self.assertEqual([row['destination'] for row in uploads if row['destination'] == links['result']],
                                         [links['result']])
                    self.assertIn('fetch ' + links['commit'], trace)
                    record = ledger.mapping(results[-1]['record'])
                    self.assertEqual(record['head'], 'e' * 40)
                    self.assertTrue(record['dirty'])
                    self.assertEqual(record['dirty_paths'], ['scripts/fixture/test_one.py'])
                    self.assertEqual(record['dirty_path_count'], 1)
                    self.assertLess(trace.index('upload log'), trace.index('tools shell'))
                    print('Production boot stages: ' + ' -> '.join(stages))

    def test_result_sends_one_job_list_and_leaves_test_times_and_reads_to_the_record(self) -> None:
        times: gate_record.ModuleTimes = {
            "wall_s": 2.0, "cpu_s": 1.5, "tests_s": 1.0, "tests_run": 2, "failures": 0, "errors": 0, "skipped": 0,
            "tests": [{"test": "Fixture.test_slow", "s": 9.0, "status": "pass"},
                      {"test": "Fixture.test_fast", "s": 0.1, "status": "pass"}],
            "config_reads": ["config/a.conf"], "folder_scans": ["scripts/fixture"],
            "content_reads": ["scripts/fixture/data.json"]}
        job = gate.GateJobResult("fixture", "test_one.py", 0, 2, "ok", cpu_seconds=1.5, times=times,
                                 wall_clock=gate_record.WallClockSpan(10, 12))
        run = gate.GateRun((job,), 2, 3, executions=(job,))
        encoded = gate_box.encode_run(run)
        self.assertNotIn("executions", encoded)
        sent = ledger.mapping(ledger.rows(encoded, "jobs")[0]["times"])
        self.assertEqual(sent, {"wall_s": 2.0, "cpu_s": 1.5, "tests_s": 1.0, "tests_run": 2, "failures": 0,
                                "errors": 0, "skipped": 0, "tests": []})
        decoded = gate_box.decode_run(encoded)
        self.assertEqual(decoded.executions, decoded.jobs)
        self.assertTrue(decoded.passed)
        record = gate_box.completion_record(run, "attempt", "commit", "tree", "remote-host", 9, 15, "gate")
        module, = ledger.rows(record, "modules")
        self.assertEqual(module["content_reads"], ["scripts/fixture/data.json"])
        self.assertEqual(module["folder_scans"], ["scripts/fixture"])
        self.assertEqual([row["test"] for row in ledger.rows(module, "tests")], ["Fixture.test_slow"])

    def test_aws_selection_records_the_assignment(self) -> None:
        from scripts.production.gate_routes import ModuleCoverage

        job = gate.GateJobResult("fixture", "test_one.py", 0, 2, "")
        run = gate.GateRun((job,), 2, 0, coverage=ModuleCoverage.SUBSET)
        affected = gate_box.completion_record(
            run, "attempt", "commit", "tree", "remote-host", 9, 15, "targeted-test")
        self.assertEqual(affected["selection"], "affected")
        self.assertEqual(affected["step"], "test-affected")
        for mode in ("targeted-test", "merge-gate"):
            with self.subTest(mode=mode):
                record = gate_box.completion_record(
                    run, "attempt", "commit", "tree", "remote-host", 9, 15, mode, split=True)
                self.assertEqual(record["selection"], "subset")

    def test_controller_reclassifies_an_older_guest_record_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            job = gate.GateJobResult("fixture", "test_one.py", 0, 2, "")
            run = gate.GateRun((job,), 2, 0)
            keys = {"attempt": "attempt", "commit": "commit", "tree": "tree"}
            guest_record = gate_box.completion_record(run, "attempt", "commit", "tree", "remote-host", 9, 15, "merge-gate")
            self.assertEqual(guest_record["selection"], "full")
            guest_record["step"] = "merge-gate"
            row: dict[str, object] = {
                **keys, "host": "remote-host", "verdict": "pass", "gate": gate_box.encode_run(run),
                "record": guest_record,
            }
            cloud_gate.save(folder / "outcome.json", {"status": "complete", "result": row})
            _ = (folder / "gate.log").write_text("")
            for mode, split, selection in (("merge-gate", True, "subset"), ("targeted-test", False, "affected")):
                with self.subTest(mode=mode, split=split):
                    request: dict[str, object] = {
                        **keys, "head": "head", "mode": mode, "modules": ["scripts/fixture/test_one.py"], "split": split,
                    }
                    with (mock.patch.object(gate, "checked_git", return_value="head"),
                          mock.patch.object(cloud_gate, "import_record") as import_record):
                        result = cloud_gate.run_service(folder, folder, request, (), lambda _: 0)
                    self.assertIsInstance(result, cloud_gate.CloudCompleted)
                    import_record.assert_called_once_with(folder, {**row, "record": {**guest_record, "selection": selection}})

    def test_result_round_trip_preserves_reruns_and_test_failures(self) -> None:
        first = gate.GateJobResult('fixture', 'test_one.py', 1, 2, 'failed', cpu_seconds=1, peak_mib=3,
                                   wall_clock=gate_record.WallClockSpan(10, 12))
        second = gate.GateJobResult('fixture', 'test_one.py', 0, 1, 'passed', wall_clock=gate_record.WallClockSpan(13, 14))
        run = gate.GateRun((second,), 4, 3, executions=(first, second))
        self.assertEqual(gate_box.decode_run(gate_box.encode_run(run)), run)
        self.assertEqual([job.output for job in gate_box.execution_records(gate_box.decode_run(gate_box.encode_run(run)))],
                         ['failed', 'passed'])
        executions = gate_box.execution_records(gate_box.decode_run(gate_box.encode_run(run)))
        self.assertEqual([job.wall_clock for job in executions],
                         [gate_record.WallClockSpan(10, 12), gate_record.WallClockSpan(13, 14)])
        record = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'remote-host', 9, 15, 'gate')
        self.assertEqual((record['kind'], record['host'], record['status']), ('gate_run', 'remote-host', 0))
        self.assertEqual(len(ledger.rows(record, 'modules')), 2)
        self.assertEqual([(row['started_at'], row['ended_at']) for row in ledger.rows(record, 'modules')],
                         [('1970-01-01T00:00:10.000Z', '1970-01-01T00:00:12.000Z'),
                          ('1970-01-01T00:00:13.000Z', '1970-01-01T00:00:14.000Z')])
        with self.subTest(recorded_times=False):
            legacy = gate_box.encode_run(run)
            for key in ('jobs', 'executions'):
                for job in ledger.rows(legacy, key):
                    self.assertNotIn('wall_clock', job)
                    del job['started_at']
                    del job['ended_at']
            decoded = gate_box.decode_run(legacy)
            absent = gate_record.WallClockNotRecorded('box result has no module times')
            self.assertEqual([job.wall_clock for job in decoded.jobs], [absent])
            self.assertEqual([job.wall_clock for job in gate_box.execution_records(decoded)], [absent, absent])
            self.assertEqual(gate_box.decode_run(gate_box.encode_run(decoded)), decoded)
            legacy_record = gate_box.completion_record(
                decoded, 'legacy', 'commit', 'tree', 'remote-host', 9, 15, 'gate')
            for module in ledger.rows(legacy_record, 'modules'):
                self.assertNotIn('started_at', module)
                self.assertNotIn('ended_at', module)
        for finished in (first, second):
            for missing in ('started_at', 'ended_at'):
                with self.subTest(missing=missing, status=finished.returncode):
                    partial = gate_box.encode_run(gate.GateRun((finished,), finished.seconds, finished.peak_mib))
                    del ledger.rows(partial, 'jobs')[0][missing]
                    decoded = gate_box.decode_run(partial)
                    absent = gate_record.WallClockNotRecorded('box result has only one module time')
                    self.assertEqual(decoded.jobs[0].wall_clock, absent)
                    self.assertEqual(decoded.jobs[0].returncode, finished.returncode)
                    self.assertEqual(decoded.passed, finished.returncode == 0)
                    partial_record = gate_box.completion_record(
                        decoded, 'partial', 'commit', 'tree', 'remote-host', 9, 15, 'gate')
                    self.assertEqual(partial_record['status'], finished.returncode)
                    for module in ledger.rows(partial_record, 'modules'):
                        self.assertNotIn('started_at', module)
                        self.assertNotIn('ended_at', module)

    def test_result_without_executions_reads_the_jobs_as_executions(self) -> None:
        first = gate.GateJobResult('fixture', 'test_one.py', 1, 2, 'failed', wall_clock=gate_record.WallClockSpan(10, 12))
        second = gate.GateJobResult('fixture', 'test_one.py', 0, 1, 'passed', wall_clock=gate_record.WallClockSpan(13, 14))
        with self.subTest(form='with executions'):
            run = gate.GateRun((second,), 4, 3, executions=(first, second))
            self.assertEqual(gate_box.decode_run(gate_box.encode_run(run)), run)
        with self.subTest(form='without executions'):
            run = gate.GateRun((second,), 4, 3, executions=(second,))
            encoded = gate_box.encode_run(run)
            # The older writer encodes executions equal to jobs; the newer writer leaves them out.
            _ = encoded.pop('executions', None)
            decoded = gate_box.decode_run(encoded)
            self.assertEqual(decoded, run)
            self.assertEqual([job.output for job in gate_box.execution_records(decoded)], ['passed'])

    def test_result_rejects_missing_or_invalid_status_and_metrics(self) -> None:
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_one.py', 0, 1, ''),), 1, 1)
        for field, value in (('returncode', False), ('seconds', -1), ('seconds', float('nan')), ('seconds', 10**400),
                             ('kind', 'UNKNOWN'), ('restart', 'other'),
                             ('started_at', -1), ('started_at', float('nan')),
                             ('ended_at', -1), ('ended_at', float('nan'))):
            with self.subTest(field=field, value=value):
                row = gate_box.encode_run(run)
                job = ledger.rows(row, 'jobs')[0]
                job[field] = value
                with self.assertRaises((ValueError, KeyError)):
                    _ = gate_box.decode_run(row)
        with self.assertRaises(KeyError):
            _ = gate_box.decode_run({})


class GateResourcesTests(unittest.TestCase):
    def template(self) -> dict[str, object]:
        return ledger.mapping(cast(object, json.loads((ROOT / 'docs/plans/builds-aws/cloudformation.json').read_text())))

    def resources(self) -> dict[str, object]:
        return ledger.mapping(self.template()['Resources'])

    def test_rust_resources_remain_identical(self) -> None:
        original = subprocess.run(['git', '-C', str(ROOT), 'show', 'HEAD:docs/plans/builds-aws/cloudformation.json'],
                                  check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        before = ledger.mapping(cast(object, json.loads(original.stdout)))
        current = self.template()
        # The retired builder pieces must stay removed; every other Rust resource must stay unchanged.
        retired = {'InstanceRole', 'InstanceProfile', 'SecurityGroup', 'LaunchTemplate', 'Instance', 'DataVolume',
                   'DataAttachment', 'ScalerPolicy', 'StopRole', 'BudgetStop', 'StopPolicy', 'InstanceId', 'DataVolumeId',
                   'InstanceType', 'AmiId', 'AvailabilityZone', 'SubnetId', 'RootVolumeGiB', 'DataVolumeGiB', 'DataVolumeIops',
                   'DataVolumeThroughput', 'SpotMaxPrice', 'Bootstrap', 'EnableCompute', 'ExistingDataVolumeId',
                   'ComputeEnabled', 'ExistingDataInput'}
        for section in ('Resources', 'Parameters', 'Outputs'):
            rows = ledger.mapping(current[section])
            self.assertFalse(retired & set(rows), section)
            for name, row in ledger.mapping(before[section]).items():
                if name.startswith("Gate") or name.startswith("EnableGates") or name in retired:
                    continue
                self.assertEqual(rows[name], row, (section, name))

    def test_cloud_resources_use_the_machine_name(self) -> None:
        names = {'GateBucket': 'BucketName', 'GateRole': 'RoleName', 'GateProfile': 'InstanceProfileName',
                 'GateSecurityGroup': 'GroupName', 'GateLaunchTemplate': 'LaunchTemplateName',
                 'GateExpiryRole': 'RoleName', 'GateExpiryFunction': 'FunctionName', 'GateExpirySchedule': 'Name',
                 'GateExpiryLog': 'LogGroupName'}
        for name, field in names.items():
            with self.subTest(resource=name):
                properties = ledger.mapping(ledger.mapping(self.resources()[name])['Properties'])
                suffix = '-expiry' if name in ('GateExpiryRole', 'GateExpiryFunction', 'GateExpirySchedule', 'GateExpiryLog') else ''
                prefix = '/aws/lambda/' if name == 'GateExpiryLog' else ''
                self.assertEqual(properties[field], {'Fn::Sub': prefix + 'cloud-claude-gate-${DeploymentId}' + suffix})
                if name in ('GateProfile', 'GateExpirySchedule'):
                    continue
                tags = (ledger.rows(properties, 'Tags') if name != 'GateLaunchTemplate' else
                        ledger.rows(ledger.rows(properties, 'TagSpecifications')[0], 'Tags'))
                self.assertIn({'Key': 'Workload', 'Value': 'cloud-claude-gate'}, tags)
                self.assertIn({'Key': 'Name', 'Value': 'cloud-claude-gate'}, tags)

    def test_launch_template_terminates_spot_and_deletes_its_root(self) -> None:
        resource = ledger.mapping(self.resources()['GateLaunchTemplate'])
        data = ledger.mapping(ledger.mapping(resource['Properties'])['LaunchTemplateData'])
        market = ledger.mapping(data['InstanceMarketOptions'])
        self.assertEqual(market['MarketType'], 'spot')
        options = ledger.mapping(market['SpotOptions'])
        self.assertEqual(options['SpotInstanceType'], 'one-time')
        self.assertEqual(options['InstanceInterruptionBehavior'], 'terminate')
        self.assertEqual(data['InstanceInitiatedShutdownBehavior'], 'terminate')
        self.assertIs(ledger.mapping(ledger.rows(data, 'BlockDeviceMappings')[0]['Ebs'])['DeleteOnTermination'], True)
        bucket = ledger.mapping(ledger.mapping(self.resources()['GateBucket'])['Properties'])
        rules = ledger.rows(ledger.mapping(bucket['LifecycleConfiguration']), 'Rules')
        self.assertEqual(rules[0]['ExpirationInDays'], {'Ref': 'GateArtifactDays'})

    def test_scaler_grants_have_workload_and_resource_scopes(self) -> None:
        properties = ledger.mapping(ledger.mapping(self.resources()['GateScalerPolicy'])['Properties'])
        everything = ledger.rows(ledger.mapping(properties['PolicyDocument']), 'Statement')
        # The regional image grant exists only when another gate region has an image.
        regional_images = [row for row in everything if 'Fn::If' in row]
        self.assertEqual([cast(list[object], row['Fn::If'])[0] for row in regional_images], ['GateHasRegionImages'])
        statements = [row for row in everything if 'Fn::If' not in row]
        by_action = {str(row['Action']): row for row in statements}
        self.assertEqual(properties['Users'], [{'Ref': 'ScalerUser'}])
        terminate = by_action[str(['ec2:TerminateInstances'])]
        self.assertEqual(ledger.mapping(ledger.mapping(terminate['Condition'])['StringEquals']), {
            'ec2:ResourceTag/GateDeployment': {'Ref': 'DeploymentId'}, 'ec2:ResourceTag/Workload': 'cloud-claude-gate'})
        role = by_action[str(['iam:PassRole'])]
        self.assertEqual(role['Resource'], {'Fn::GetAtt': ['GateRole', 'Arn']})
        self.assertEqual(role['Condition'], {'StringEquals': {'iam:PassedToService': 'ec2.amazonaws.com'}})
        for row in statements:
            if row['Resource'] == '*':
                self.assertIn(row['Action'], (['ec2:DescribeInstances'], ['ce:GetCostAndUsage']))
        launches = [row for row in statements if row['Action'] == ['ec2:RunInstances']]
        self.assertEqual(len(launches), 6)
        tagged = next(row for row in launches if 'security-group/*' in json.dumps(row['Resource']))
        self.assertEqual(ledger.mapping(ledger.mapping(tagged['Condition'])['StringEquals']), {
            'aws:ResourceTag/GateDeployment': {'Ref': 'DeploymentId'}, 'aws:ResourceTag/Workload': 'cloud-claude-gate'})
        subnet = next(row for row in launches if 'subnet/*' in json.dumps(row['Resource']))
        self.assertEqual(subnet['Condition'], {'ArnEquals': {'ec2:Vpc': {'Ref': 'GateVpcs'}}})
        instance = next(row for row in launches if 'instance/*' in json.dumps(row['Resource']))
        condition = ledger.mapping(instance['Condition'])
        self.assertEqual(ledger.mapping(condition['StringEquals'])['ec2:InstanceType'], {'Ref': 'GateInstanceTypes'})
        self.assertEqual(ledger.mapping(condition['StringEquals'])['ec2:InstanceMarketType'], 'spot')
        self.assertIn('ec2:InstanceProfile', ledger.mapping(condition['ArnEquals']))
        self.assertIn({'Fn::Sub': '${GateBucket.Arn}/artifacts/*'},
                      ledger.rows(by_action[str(['s3:PutObject'])], 'Resource'))
        self.assertEqual(ledger.rows(by_action[str(['s3:GetObject'])], 'Resource'), (
            {'Fn::Sub': '${GateBucket.Arn}/artifacts/*'}, {'Fn::Sub': '${GateBucket.Arn}/source/*'},
            {'Fn::Sub': '${GateBucket.Arn}/requests/*'}))

    def test_guest_role_has_no_object_permissions(self) -> None:
        role = ledger.mapping(ledger.mapping(self.resources()['GateRole'])['Properties'])
        self.assertNotIn('Policies', role)
        self.assertEqual(ledger.mapping(self.template()['Outputs'])['GateRoleId'],
                         {'Condition': 'Gates', 'Value': {'Fn::GetAtt': ['GateRole', 'RoleId']}})

    def test_expiry_function_finds_all_pages_and_terminates_only_expired_instances(self) -> None:
        properties = ledger.mapping(ledger.mapping(self.resources()['GateExpiryFunction'])['Properties'])
        code = ledger.text(ledger.mapping(properties['Code']), 'ZipFile')
        expired = {'InstanceId': 'expired', 'Tags': [{'Key': 'ExpiresAt', 'Value': '99'}]}
        live = {'InstanceId': 'live', 'Tags': [{'Key': 'ExpiresAt', 'Value': '101'}]}
        invalid = {'InstanceId': 'invalid', 'Tags': [{'Key': 'ExpiresAt', 'Value': 'invalid'}]}
        class ExpiryClient:
            operation: str = ''
            filters: list[dict[str, object]] = []
            terminated: list[list[str]] = []

            def get_paginator(self, operation: str) -> Self:
                self.operation = operation
                return self

            def paginate(self, *, Filters: list[dict[str, object]]) -> list[dict[str, object]]:
                self.filters = Filters
                return [{'Reservations': [{'Instances': [live]}]},
                        {'Reservations': [{'Instances': [expired, invalid]}]}]

            def terminate_instances(self, *, InstanceIds: list[str]) -> None:
                self.terminated = [*self.terminated, InstanceIds]

        client = ExpiryClient()

        def aws_client(service: str) -> ExpiryClient:
            self.assertEqual(service, 'ec2')
            return client
        namespace: dict[str, object] = {}
        with (mock.patch.dict('sys.modules', {'boto3': SimpleNamespace(client=aws_client)}),
              mock.patch.dict(os.environ, {'DEPLOYMENT': 'fixture'}), mock.patch('time.time', return_value=100)):
            exec(compile(code, '<expiry>', 'exec'), namespace)
            cast(Callable[[object, object], None], namespace['handler'])({}, None)
        self.assertEqual(client.terminated, [['expired']])
        self.assertEqual(client.operation, 'describe_instances')
        filters = client.filters
        self.assertIn({'Name': 'tag:GateDeployment', 'Values': ['fixture']}, filters)
        self.assertIn({'Name': 'tag:Workload', 'Values': ['cloud-claude-gate']}, filters)
        schedule = ledger.mapping(ledger.mapping(self.resources()['GateExpirySchedule'])['Properties'])
        self.assertIn('rate(1 minute)', json.dumps(schedule))
