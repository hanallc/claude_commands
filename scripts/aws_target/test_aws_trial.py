"""Measure trial lifecycles with a fake AWS command and clock."""
from __future__ import annotations

from scripts.lib.checkout_test_support import copy_startup, copy_test_runner

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import json
import multiprocessing
from multiprocessing.connection import Connection
import os
import pathlib
import shlex
import shutil
import signal
import socket
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import cast, final, override
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import admin_secrets, aws_trial as trial, cloud_gate, gate_box, ledger
from scripts.aws_target.ledger_test_support import CostFixture, install_aws_fixture_settings, override_fixture_settings, text_setting
from scripts.aws_target.aws_target import AwsCli
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue import cloud_progress
from scripts.build_queue.admission_wire import CloudGroup, Owner
from scripts.agents import remote
from scripts.lib import origin_deletion
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lint.memory_test_support import config_environment
from scripts.production import gate, staff
from scripts.production.staff_test_support import fixture_member
from scripts.lib.process_group import send_process_group
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.herdr.missing_binary import MISSING_HERDR_BIN


def write_guest_config(root: Path, *, slow_test_s: float = 1.0) -> None:
    override_fixture_settings(root / 'cloud_gate.conf',
        'packages=git python3 cargo rustc rustup cargo-nextest uv curl openssh rsync gnupg ripgrep jq '
        + 'gcc pkg-config tmux taplo zsh bash gh sqlite chromium nodejs util-linux bubblewrap\n')
    _ = (root / 'test_times.conf').write_text(
        f'# range: 0..\nslow_test_s={slow_test_s}\n'
        + '# whole: 1..\ngate_failure_log_kib=64\n'
        + '# whole: 1..\ntop_runs=5\n')


@final
class GuestTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir='/tmp')))
        install_aws_fixture_settings(root)
        write_guest_config(root)
        _ = (root / 'gate.conf').write_text('# range: 1..\ngate_time_limit_factor=1.5\n')
        _ = (root / 'buildlog.conf').write_text(
            '# range: 0..\nindex_unused_days=2\n'
            + '# range: 0..\nindex_touch_minutes=60\n'
            + '# whole: 1..\nindex_spill_pages=65536\n')
        _ = (root / 'build_folders.conf').write_text(
            f'roots.system={root}\nroots.build={root / "build"}\n'
            + '# whole: 1..\nnames.suffix_length=8\n'
            + 'names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_\n'
            + 'patterns.legacy-unique={purpose}{suffix}\n'
            + 'patterns.tool={purpose}{suffix}\n'
            + '# range: 0.001..60\nlocks.probe_wait_seconds=0.25\n'
            + '# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, config_environment(root))))

    def test_user_data_fits_ec2_limit_with_module_times_and_400_character_signed_links(self) -> None:
        prefix = 'https://fixture.invalid/object?X-Amz-Signature='
        url = prefix + 'a' * (400 - len(prefix))
        links = {'step': 'size', 'commit': 'a' * 40,
                 **dict.fromkeys(('source', 'guest', 'log', 'result', 'ready-cold', 'ready-warm', 'module_seconds'), url)}
        data = trial.user_data(links, 'f' * 40, 30, 30, 600)
        self.assertLessEqual(len(data.encode()), 16384)

    def tool_session(self) -> str:
        links: dict[str, str] = dict.fromkeys(('source', 'guest', 'log', 'result', 'ready-cold', 'ready-warm'), 'https://fixture.invalid/object')
        links['step'] = 'size'
        data = trial.user_data(links, 'f' * 40, 30, 30, 600)
        return shlex.split(data[data.index('nix --extra-experimental-features'):])[-1]

    def test_tests_use_an_unprivileged_user_after_root_setup(self) -> None:
        session = self.tool_session()
        self.assertIn('runuser -u gate-trial -- env HOME=/home/gate-trial', session)
        self.assertLess(session.index('chown -R gate-trial:users'), session.index('runuser -u gate-trial'))
        self.assertLess(session.index('mount --bind'), session.index('runuser -u gate-trial'))
        self.assertNotIn('/root', trial.GUEST)
        self.assertIn("home = pathlib.Path.home() / '.claude'", trial.GUEST)
        self.assertIn('XDG_RUNTIME_DIR=/run/user/$(id -u gate-trial)', session)

    def test_initialized_environment_preserves_fixture_path_in_zsh(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / 'profile'
            _ = profile.write_text('export __NIXOS_SET_ENVIRONMENT_DONE=1\nexport PATH=/run/current-system/sw/bin\n')
            tool = root / 'fixture-tool'
            _ = tool.write_text('#!/bin/sh\nexit 0\n')
            tool.chmod(0o700)
            bwrap = root / 'bwrap'
            _ = bwrap.write_text('#!/bin/sh\nexit 0\n')
            bwrap.chmod(0o700)
            basedpyright = root / 'basedpyright'
            _ = basedpyright.write_text('#!/bin/sh\necho basedpyright fixture-version\n')
            basedpyright.chmod(0o700)
            python = root / 'python3'
            _ = python.write_text('#!' + sys.executable + '\nimport json, os, subprocess\n'
                + "print(json.dumps({'initialized':os.environ.get('__NIXOS_SET_ENVIRONMENT_DONE'),"
                + "'tool':subprocess.check_output(['zsh','-c','command -v fixture-tool'],text=True).strip(),"
                + "'basedpyright':subprocess.check_output(['basedpyright','--version'],text=True).strip()}))\n")
            python.chmod(0o700)
            script = shlex.split(self.tool_session().rsplit('bash -c ', 1)[1])[0]
            script = script.replace('. /etc/profile', '. ' + shlex.quote(str(profile)))
            result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, check=True,
                timeout=TEST_HANG_GUARD_SECS,
                env={**os.environ, 'HOME': str(root), 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                     '__NIXOS_SET_ENVIRONMENT_DONE': 'inherited-root'})
            payload = ledger.mapping(cast(object, json.loads(result.stdout)))
            self.assertEqual(payload['initialized'], '1')
            self.assertEqual(payload['tool'], str(tool))
            self.assertEqual(payload['basedpyright'], 'basedpyright fixture-version')

    def test_system_profile_copy_preserves_external_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / 'outside'
            _ = outside.write_text('fixture command')
            source = root / 'source'
            source.mkdir()
            original = source / 'command'
            original.symlink_to(outside)
            destination = root / 'destination'
            destination.mkdir()
            command = shlex.split(next(line for line in self.tool_session().splitlines() if line.startswith('cp ')))
            command[-2] = str(source) + '/.'
            command[-1] = str(destination) + '/'
            _ = subprocess.run(command, check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            copied_target = (destination / 'command').readlink()
            self.assertEqual(copied_target, original.readlink())
            self.assertNotIn(str(source), str(copied_target))

    def test_fixed_system_profile_path_resolves_the_tool_python(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tools = root / 'tools'
            tools.mkdir()
            system = root / 'system'
            system.mkdir()
            (tools / 'python3').symlink_to(sys.executable)
            (tools / 'chromium').symlink_to(sys.executable)
            mount = tools / 'mount'
            _ = mount.write_text('#!/bin/sh\ncp -a "$2"/. "$3"/\n')
            mount.chmod(0o700)
            session = self.tool_session()
            setup = session[session.index('ln -sf'):].split('chown -R', 1)[0]
            setup = setup.replace('/run/current-system/sw/bin', str(system)).replace('$box_dir/tool-profile', str(root / 'profile'))
            setup = setup.replace('/bin/bash', str(root / 'bash')).replace('/bin/zsh', str(root / 'zsh'))
            _ = subprocess.run(['bash', '-ec', setup], check=True, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS,
                env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
            version = subprocess.check_output([str(system / 'python3'), '-c',
                'import json, sys; print(json.dumps(list(sys.version_info[:2])))'], text=True,
                timeout=TEST_HANG_GUARD_SECS)
            self.assertGreaterEqual(cast(list[int], json.loads(version)), [3, 13])
            self.assertEqual((system / 'python3').resolve(), Path(sys.executable).resolve())
            self.assertEqual((system / 'google-chrome').resolve(), (tools / 'chromium').resolve())
            session = self.tool_session()
            self.assertLess(session.index('$box_dir/tool-profile/google-chrome'), session.index('mount --bind'))

    def test_guest_samples_peak_machine_memory_every_two_seconds(self) -> None:
        for present in (True, False):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as folder:
                meminfo = Path(folder) / 'meminfo'
                def write(available: int) -> None:
                    if present:
                        _ = meminfo.write_text(f'MemTotal: 1000000 kB\nMemAvailable: {available} kB\n')
                write(900000)
                result: dict[str, object] = {'memory_peak_bytes': None}
                namespace: dict[str, object] = {'pathlib': pathlib, 'result': result}
                code = gate_box.CANDIDATE_GUEST[gate_box.CANDIDATE_GUEST.index('def sample_memory():'):gate_box.CANDIDATE_GUEST.index('memory_stop =')]
                code = code.replace("'/proc/meminfo'", repr(str(meminfo)))
                exec(compile(code, '<memory sampler>', 'exec'), namespace)
                waits: list[float] = []
                def pause(seconds: float) -> bool:
                    waits.append(seconds)
                    write(200000 if len(waits) == 1 else 700000)
                    return len(waits) == 3
                stop = threading.Event()
                with mock.patch.object(stop, 'wait', new=pause):
                    cast(Callable[[threading.Event], None], namespace['monitor_memory'])(stop)
                self.assertEqual(waits, [2, 2, 2])
                self.assertEqual(result['memory_peak_bytes'], 800000 * 1024 if present else None)


    def test_guest_uses_whole_machine_without_types_with_timeout_and_counts(self) -> None:
        socket_folder = self.enterContext(short_socket_directory())
        with tempfile.TemporaryDirectory() as folder, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
            root = Path(folder)
            ready_path = Path(socket_folder) / 'stalled'
            ready.bind(str(ready_path))
            ready.settimeout(TEST_HANG_GUARD_SECS)
            module_limit_s = TEST_HANG_GUARD_SECS
            repo = root / 'repo'
            suite = repo / 'scripts' / 'fixture'
            suite.mkdir(parents=True)
            copy_startup(repo)
            links = {'step': 'size', 'commit': 'fixture-head', 'source': 'https://fixture.invalid/source',
                     'log': 'https://fixture.invalid/log', 'result': 'https://fixture.invalid/result',
                     'module_seconds': 'https://fixture.invalid/module_seconds'}
            _ = (root / 'links.json').write_text(json.dumps({**links, 'module_time_limit_s': module_limit_s}))
            _ = (root / 'boot.log').write_text('fixture tool preparation\n')
            _ = (root / 'timeline.log').write_text('fixture timeline\n')
            runner = root / 'module_run.py'
            _ = runner.write_text('import json, os, pathlib, socket, sys, threading\n'
                + f'root=pathlib.Path({str(root)!r})\n'
                + "name=sys.argv[2]\n(root/name).write_text(json.dumps({'args':sys.argv[1:], 'env':dict(os.environ)}))\n"
                + "if name=='test_stuck.py':\n"
                + "    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:\n"
                + f"        ready.sendto(b'ready', {str(ready_path)!r})\n"
                + "    threading.Event().wait()\n"
                + "pathlib.Path(os.environ['TEST_TIMES_OUT']).write_text(json.dumps({'wall_s':1, 'cpu_s':1, 'tests_s':1, 'tests_run':3, 'skipped':1, 'failures':1, 'errors':0, 'tests':[]}))\n"
                + "print('fixture failure')\nsys.exit(1)\n")
            types = root / 'basedpyright'
            _ = types.write_text('#!' + sys.executable + '\nimport json, pathlib, sys\n'
                + f'pathlib.Path({str(root / "types-args")!r}).write_text(json.dumps(sys.argv[1:]))\n'
                + "print('0 errors, 0 warnings, 0 notes')\n")
            types.chmod(0o700)
            modules = [('fixture', suite / 'test_stuck.py'), ('fixture', suite / 'test_broken.py')]
            uploads: dict[str, bytes] = {}
            timings = {'fixture/test_stuck.py': 20.0, 'fixture/test_broken.py': 1.0}
            real_run = subprocess.run
            def transport(arguments: Sequence[str], **options: object
                          ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
                if arguments[0] == 'awk':
                    return real_run(arguments, input=cast(bytes, options['input']), stdout=subprocess.PIPE, check=True)
                if arguments[0] == 'curl':
                    self.assertIn(arguments[arguments.index('--max-time') + 1], ('30', '60'))
                    configuration = Path(arguments[arguments.index('--config') + 1])
                    url = cast(str, json.loads(configuration.read_text().partition('= ')[2]))
                    if '-X' not in arguments:
                        content = json.dumps(timings).encode() if url == links['module_seconds'] else b'fixture bundle'
                        _ = Path(arguments[arguments.index('-o') + 1]).write_bytes(content)
                    else:
                        uploads[url] = cast(bytes, options['input'])
                    return subprocess.CompletedProcess(arguments, 0, b'200')
                return subprocess.CompletedProcess(arguments, 0, 'version', '')
            code = trial.GUEST.replace("'/proc/meminfo'", repr(str(root / 'meminfo'))).replace("'/var/lib/gate-trial'", repr(str(root)))
            candidate = gate_box.CANDIDATE_GUEST.replace("'/proc/meminfo'", repr(str(root / 'meminfo'))).replace("'/proc/stat'", repr(str(root / 'stat')))
            (repo / 'scripts/aws_target').mkdir()
            _ = (repo / 'scripts/aws_target/gate_box.py').write_text('fixture candidate')
            _ = (root / 'stat').write_text('cpu 100 0 100 10000 1000 0 0 0 9999 9999\n')
            _ = (root / 'meminfo').write_text('MemTotal: 1000000 kB\nMemAvailable: 200000 kB\n')
            original_reap = gate.reap_gate_job
            broken_finished = False
            def reap(job: gate.RunningGateJob, *, block: bool = False) -> None:
                nonlocal broken_finished
                original_reap(job, block=block)
                if job.module == 'test_broken.py' and job.process.returncode is not None:
                    broken_finished = True
                if (job.module == 'test_stuck.py' and broken_finished and
                        job.process.returncode is None and not job.killed_for):
                    self.assertEqual(ready.recv(5), b'ready')
                    job.started = time.monotonic() - module_limit_s
            original_run = gate.run_suites
            def run_suites(repo: Path, *, full: bool, machine: gate.WholeMachine,
                           module_time_limit_s: float) -> gate.GateRun:
                self.assertEqual(machine.module_seconds, timings)
                run = original_run(repo, full=full, machine=machine, module_time_limit_s=module_time_limit_s)
                _ = (root / 'stat').write_text('cpu 500 0 100 20000 5000 0 0 400 20000 20000\n')
                return run
            with (mock.patch.dict(os.environ, {'GATE_BASEDPYRIGHT': str(types), 'GATE_PYTHON': sys.executable,
                                                 'GATE_BOX_DIR': str(root), 'box_dir': str(root),
                                                 'BUILDLOG_DIR': str(root / 'buildlog')}),
                  mock.patch.object(sys, 'path', list(sys.path)),
                  mock.patch.object(gate_box, 'CANDIDATE_GUEST', candidate),
                  mock.patch.object(gate_box, '__file__', str(repo / 'scripts/aws_target/gate_box.py')),
                  mock.patch.object(Path, 'home', return_value=root),
                  mock.patch('subprocess.run', side_effect=transport),
                  mock.patch('os.cpu_count', return_value=2),
                  mock.patch.object(gate, 'reap_gate_job', side_effect=reap),
                  mock.patch.object(gate, 'run_suites', side_effect=run_suites),
                  mock.patch.object(gate, 'module_runner', return_value=runner),
                  mock.patch.object(gate, 'discover_test_modules', return_value=modules) as discover,
                  mock.patch.object(gate, 'BuildHoldWait', side_effect=AssertionError('hold read')),
                  mock.patch.object(gate, 'gate_reservation', side_effect=AssertionError('admission')),
                  mock.patch.object(gate, 'shared_cpu_pool', side_effect=AssertionError('CPU pool')),
                  mock.patch.object(gate, 'suite_scope_prefix', side_effect=AssertionError('scope')),
                  mock.patch.object(gate, 'merge_gate_governor_gib', side_effect=AssertionError('governor')),
                  mock.patch.object(gate, 'gate_state_dir', side_effect=AssertionError('test ledger')),
                  mock.patch.object(gate, 'typecheck_python', side_effect=AssertionError('type preparation during tests-only trial'))):
                previous = Path.cwd()
                try:
                    exec(compile(code, '<guest>', 'exec'), {})
                finally:
                    os.chdir(previous)
            discover.assert_called_once_with(repo)
            payload = ledger.mapping(cast(object, json.loads(uploads[links['result']])))
            self.assertEqual(payload['verdict'], 'fail', payload.get('error'))
            self.assertNotIn('governor_gib', payload)
            self.assertEqual(ledger.mapping(payload['tools'])['python_sqlite'], sqlite3.sqlite_version)
            self.assertEqual(payload['module_time_limit_s'], module_limit_s)
            self.assertEqual(payload['memory_peak_bytes'], 800000 * 1024)
            self.assertEqual(payload['max_running_jobs'], 2)
            self.assertEqual(payload['vcpus'], 2)
            busy = 400 / os.sysconf('SC_CLK_TCK')
            self.assertEqual(payload['busy_cpu_s'], busy)
            seconds = float(str(payload['gate_finished_at'])) - float(str(payload['gate_started_at']))
            self.assertEqual(payload['gate_s'], seconds)
            self.assertAlmostEqual(float(str(payload['cpu_use_percent'])), 100 * busy / (2 * seconds))
            self.assertEqual(payload['tests'], 3)
            self.assertEqual(payload['skips'], 1)
            self.assertEqual(set(cast(list[str], payload['failed_modules'])), {'fixture/test_broken.py', 'fixture/test_stuck.py'})
            measured = {str(row['module']): row for value in cast(list[object], payload['modules']) if (row := ledger.mapping(value))}
            self.assertEqual(len(measured), 2)
            self.assertEqual(measured['fixture/test_broken.py']['status'], 1)
            self.assertNotIn('types/basedpyright', measured)
            self.assertEqual(measured['fixture/test_stuck.py']['stopped'], True)
            self.assertEqual(measured['fixture/test_stuck.py']['status'], -signal.SIGTERM)
            self.assertIn('gate_started_at', payload)
            self.assertIn('gate_finished_at', payload)
            for _, path in modules:
                call = ledger.mapping(cast(object, json.loads((root / path.name).read_text())))
                self.assertEqual(call['args'], [str(suite), path.name])
                environment = ledger.mapping(call['env'])
                self.assertEqual(environment['HERDR_BIN'], MISSING_HERDR_BIN)
                for key in ('TEST_TIMES_OUT', 'TMPDIR', 'STAFF_FILE', 'BUILDLOG_DIR', 'BUILDLOG_LIVE_MEMORY_TRIPWIRE'):
                    self.assertIn(key, environment)
                self.assertNotIn('GATE_BOX_DIR', environment)
                self.assertNotIn('box_dir', environment)
            self.assertFalse((root / 'types-args').exists())
            content = uploads[links['log']].decode()
            self.assertLess(content.index('fixture/test_broken.py exit=1'), content.index('fixture/test_stuck.py exit=-15'))
            self.assertIn(' stopped', content)
            self.assertNotIn('types/basedpyright', content)
            self.assertEqual(sum(line.startswith('fixture/test_broken.py exit=') for line in content.splitlines()), 1)
            self.assertEqual(sum(line.startswith('fixture/test_stuck.py exit=') for line in content.splitlines()), 1)
            self.assertIn('fixture tool preparation\n', content)
            self.assertIn('fixture timeline\n', content)
            self.assertIn('UTC: ', content)


@final
class SigningTests(unittest.TestCase):
    def test_aws_published_query_vector_and_put_method(self) -> None:
        # AWS publishes these dummy credentials and the GET signature in its S3 query example.
        credentials = trial.SigningKey('AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')
        at = datetime(2013, 5, 24, tzinfo=UTC)
        get = trial.presign('GET', 'examplebucket.s3.amazonaws.com', 'test.txt', 'us-east-1', credentials, at, 86400)
        self.assertEqual(parse_qs(urlsplit(get).query)['X-Amz-Signature'],
            ['aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404'])
        put = trial.presign('PUT', 'examplebucket.s3.amazonaws.com', 'test.txt', 'us-east-1', credentials, at, 86400)
        # The AWS SDK confirms this PUT vector with the same AWS example inputs.
        self.assertEqual(parse_qs(urlsplit(put).query)['X-Amz-Signature'],
            ['f4db56459304dafaa603a99a23c6bea8821890259a65c18ff503a4a72a80efd9'])


@final
class PairedTrialTests(CostFixture):
    repo: Path = Path()
    origin: Path = Path()
    base: str = ''
    changed: str = ''

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.settings.write_text(self.settings.read_text().replace('region=us-east-1', 'region=us-east-2'))
        _ = self.enterContext(mock.patch.object(trial, 'account_settings_path', return_value=self.settings))
        record = self.root / 'staff.json'
        _ = record.write_text('{"schema": 2, "rows": []}')
        # clear=True drops the caller's GIT_* variables, such as GIT_DIR or GIT_INDEX_FILE.
        self.enterContext(mock.patch.dict(os.environ, {
            **fixture_git_environment(self.root), 'STAFF_FILE': str(record), 'GIT_ALLOW_PROTOCOL': 'file',
        }, clear=True))
        shipped = Path(__file__).resolve().parents[2]
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for args in (('init', '-q'), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid')):
            _ = trial.checked_git(self.repo, *args)
        copy_test_runner(self.repo)
        for relative in ('scripts/aws_target/__init__.py', 'scripts/aws_target/gate_box.py',
                         'scripts/build_queue/admission_jobs.py', 'scripts/build_queue/snapshot_codec.py',
                         'scripts/buildlog/parse.py', 'scripts/lib/build_steps.py',
                         'scripts/lib/proc_text.py', 'scripts/lib/process_group.py',
                         'scripts/production/gate_modules.py', 'scripts/production/gate_routes.py',
                         'scripts/test_times/gate_record.py'):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes((shipped / relative).read_bytes())
        (self.repo / 'scripts/production').mkdir(exist_ok=True)
        _ = (self.repo / 'scripts/production/gate.py').write_text(
            "def test_module_environment(root, environment):\n"
            + "    return {**environment, 'TMPDIR': str(root), 'TEST_TIMES_OUT': str(root / 'test-times.json')}\n"
            + "def process_memory_mib(roots):\n"
            + "    return {pid: 0.0 for pid in roots}\n")
        (self.repo / 'config').mkdir(exist_ok=True)
        (self.repo / 'scripts/fixture').mkdir()
        _ = (self.repo / 'scripts/fixture/test_sample.py').write_text('''import json, os, pathlib, unittest
class Sample(unittest.TestCase):
    def test_sample(self):
        self.assertFalse(pathlib.Path('generated').exists())
        pathlib.Path('generated').touch()
        with open(os.environ['PAIRED_TRACE'], 'a') as trace:
            trace.write(json.dumps(dict(pid=os.getpid(), cwd=os.getcwd(), home=os.environ['HOME'],
                                       temporary=os.environ['TMPDIR'], config=os.environ['CONF_DIR'])) + '\\n')
''')
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Base fixture')
        self.base = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        _ = (self.repo / 'change').write_text('changed')
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Changed fixture')
        self.changed = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        self.origin = self.root / 'origin.git'
        _ = subprocess.run(['git', 'clone', '--bare', str(self.repo), str(self.origin)], check=True, capture_output=True)
        gate_settings = self.root / 'cloud_gate.conf'
        text_setting(gate_settings, 'clone_url', str(self.origin))
        text_setting(gate_settings, 'credential_file', str(self.root / 'credentials'))
        text_setting(gate_settings, 'regions', 'us-east-2 us-west-2')
        text_setting(gate_settings, 'trial_branch_prefix', 'gate-trial')

    def test_a_caller_git_variable_does_not_reach_the_fixture_commands(self) -> None:
        folder = self.root / 'caller'
        folder.mkdir()
        caller = {'GIT_INDEX_FILE': str(folder / 'index'), 'GIT_DIR': str(folder / 'repository.git')}
        probe = PairedTrialTests('test_expired_sweep_preserves_a_racing_coordinator_termination')
        with mock.patch.dict(os.environ, caller):
            try:
                probe.setUp()
                self.assertFalse(set(caller) & set(os.environ))
                _ = trial.checked_git(probe.repo, 'add', 'change')
            finally:
                probe.doCleanups()
        self.assertEqual(list(folder.iterdir()), [])

    def test_expired_sweep_preserves_a_racing_coordinator_termination(self) -> None:
        now = 1700000000
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(now - 3600,
            (ledger.PricedResource('fixture', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(now - 3600, 'stopped', 'fixture'))
        _ = book.reserve_gate(self.settings, ledger.GateReservation(
            'race', now - 3600, now - 1, Decimal('.8'), Decimal('.02')))
        current = self.state / 'paired-trials' / 'current'
        previous = current.parent / 'expired' / 'race'
        current.mkdir(parents=True)
        previous.mkdir(parents=True)
        cloud_gate.save(previous / 'request.json', {'attempt': 'race', 'expires': now - 1})
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-2', 1), book, self.settings,
            current / 'reservation', clock=lambda: now)
        resolve = book.resolve_gate
        confirmed = ledger.GateTerminated(now - 2, 'i-race')
        costs: list[Decimal] = []
        def racing_resolve(attempt: str, outcome: ledger.GateRunning | ledger.GateAbsent | ledger.GateTerminated,
                           *, expected_owner: ledger.GateWorkerOwner | ledger.GateOwnerUnknown) -> None:
            self.assertIsInstance(outcome, ledger.GateAbsent)
            resolve(attempt, confirmed)
            costs.append(trial.trial_cost(book, {attempt}, self.settings, now)[0])
            resolve(attempt, outcome, expected_owner=expected_owner)
        with (mock.patch.object(book, 'resolve_gate', side_effect=racing_resolve),
              mock.patch.object(worker.api, 'call', side_effect=AssertionError('unexpected AWS call'))):
            trial.sweep_trial_resources(current, worker)
        self.assertEqual(book.read_snapshot().gate_reservations[0].state, confirmed)
        self.assertGreater(costs[0], Decimal(0))
        self.assertEqual(trial.trial_cost(book, {'race'}, self.settings, now), (costs[0], True))

    def request(self, modules: tuple[str, ...] = ('fixture/test_sample.py',), rounds: int = 3) -> trial.PairedTrialRequest:
        return trial.trial_request(self.repo, self.base, self.changed, modules, rounds)

    def sample(self, variant: str, number: int, cpu: float) -> dict[str, object]:
        return {'module': 'fixture/test_sample.py', 'variant': variant, 'round': number, 'cpu_s': cpu,
                'peak_mib': 12.0, 'wall_s': 1.0, 'status': 0, 'tests': 1, 'failures': 0, 'errors': 0,
                'instance_type': 'm7a.8xlarge', 'box': 'i-fixture'}

    def remote_branches(self) -> dict[str, str]:
        rows = trial.checked_git(self.origin, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/')
        return dict(row.split() for row in rows.splitlines())

    def invoke_trial(self) -> int:
        cloud_gate.save(self.state / 'gate-deployment.json', {'template': 'fixture'})
        return trial.paired_main(['--base', self.base, '--changed', self.changed, '--repo', str(self.repo),
                                  '--modules', 'fixture/test_sample.py'], self.state)

    def test_pushed_refs_and_modules_do_not_change_checkout(self) -> None:
        _ = (self.repo / 'private').write_text('local only')
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Local fixture')
        self.changed = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        self.assertNotIn(self.changed, self.remote_branches().values())
        _ = (self.repo / 'change').write_text('staged edit')
        _ = trial.checked_git(self.repo, 'add', 'change')
        _ = (self.repo / 'change').write_text('unstaged edit')
        _ = (self.repo / 'untracked').write_text('untracked')
        before = trial.checked_git(self.repo, 'show-ref')
        status = trial.checked_git(self.repo, 'status', '--porcelain')
        index = (self.repo / '.git/index').read_bytes()
        branches = self.remote_branches()
        request = self.request()
        self.assertEqual(request.base.commit, self.base)
        self.assertEqual(request.changed.commit, self.changed)

        def service(_command: Sequence[str], _unit: str) -> int:
            folder = next((self.state / 'paired-trials').iterdir())
            expires = 1700000000 + cloud_gate.settings().whole('lifetime_minutes') * 60
            request_path = next(folder.glob('*/request.json'))
            saved = cloud_gate.read(request_path)
            self.assertEqual(saved['expires'], expires)
            self.assertEqual(saved['gate_job'], {'kind': 'StepJob', 'step': 'test-modules',
                                                'unit': '', 'branch': '', 'modules': 1})
            self.assertEqual(self.remote_branches(), {**branches,
                f'refs/heads/gate-trial-{expires}-{folder.name}-base': self.base,
                f'refs/heads/gate-trial-{expires}-{folder.name}-changed': self.changed})
            self.assertEqual(trial.checked_git(self.repo, 'show-ref'), before)
            self.assertEqual(trial.checked_git(self.repo, 'status', '--porcelain'), status)
            return 0

        with (mock.patch.object(trial, 'paired_service', side_effect=service),
              mock.patch.object(time, 'time', return_value=1700000000)):
            self.assertEqual(self.invoke_trial(), 0)
        self.assertEqual(self.remote_branches(), branches)
        self.assertEqual(trial.checked_git(self.repo, 'show-ref'), before)
        self.assertEqual(trial.checked_git(self.repo, 'status', '--porcelain'), status)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)

    def test_rerun_pushes_unique_branches_without_force_and_cleans_pass_or_failure(self) -> None:
        config = self.root / 'cloud_gate.conf'
        _ = config.write_text(config.read_text().replace('trial_branch_prefix=gate-trial', 'trial_branch_prefix=fixture-trial'))
        branches = self.remote_branches()
        names: list[set[str]] = []
        statuses = iter((0, 7))

        def service(_command: Sequence[str], _unit: str) -> int:
            remote = self.remote_branches()
            names.append(set(remote) - set(branches))
            self.assertEqual(len(names[-1]), 2)
            self.assertTrue(all(name.startswith('refs/heads/fixture-trial-') for name in names[-1]))
            self.assertEqual({remote[name] for name in names[-1]}, {self.base, self.changed})
            return next(statuses)

        with (mock.patch.object(trial, 'checked_git', wraps=trial.checked_git) as git,
              mock.patch.object(trial, 'paired_service', side_effect=service)):
            for expected in (0, 7):
                self.assertEqual(self.invoke_trial(), expected)
                self.assertEqual(self.remote_branches(), branches)
        self.assertTrue(names[0].isdisjoint(names[1]))
        pushes = [call.args[1:] for call in git.call_args_list if call.args[1] == 'push' and '--delete' not in call.args]
        self.assertEqual(len(pushes), 4)
        for arguments in pushes:
            self.assertEqual(arguments[1], str(self.origin))
            self.assertNotIn('-f', arguments)
            self.assertNotIn('--force', arguments)
            self.assertFalse(cast(str, arguments[2]).startswith('+'))
        listings = [call.args for call in git.call_args_list if call.args[1] == 'ls-remote']
        self.assertEqual(len(listings), 4)
        self.assertTrue(all(cast(str, arguments[-1]).startswith('refs/heads/fixture-trial-') for arguments in listings))

    def test_paired_main_never_installs_a_signal_handler(self) -> None:
        handlers = {number: signal.getsignal(number) for number in trial.TRIAL_SIGNALS}
        git = trial.checked_git
        observations: list[str] = []

        def check_handlers(label: str) -> None:
            self.assertEqual({number: signal.getsignal(number) for number in trial.TRIAL_SIGNALS}, handlers)
            observations.append(label)

        def inspect_git(repo: Path, *arguments: str) -> str:
            check_handlers(arguments[0])
            return git(repo, *arguments)

        def service(_command: Sequence[str], _unit: str) -> int:
            check_handlers('service')
            return 0

        with (mock.patch.object(trial, 'checked_git', side_effect=inspect_git),
              mock.patch.object(trial, 'paired_service', side_effect=service),
              mock.patch.object(signal, 'signal', side_effect=AssertionError('signal handler installation'))):
            self.assertEqual(self.invoke_trial(), 0)
        check_handlers('finished')
        self.assertIn('push', observations)
        self.assertIn('service', observations)

    def test_start_sweep_deletes_only_expired_trial_branches(self) -> None:
        now = 1700000000
        old = now - 1
        expired = {f'gate-trial-{old}-' + 'a' * 32 + '-' + variant for variant in ('base', 'changed')}
        preserved = {
            f'gate-trial-{now + 1}-' + 'b' * 32 + '-base',
            f'gate-trial-{now}-' + 'c' * 32 + '-changed',
            f'gate-trial-{now + 60}-' + 'd' * 32 + '-base',
            'gate-trial-not-an-epoch-' + 'e' * 32 + '-base',
            f'gate-trial-{old}-invalid-base',
            f'gate-trial-{old}-' + 'f' * 32 + '-other',
            f'gate-trial-{old}-' + 'f' * 32 + '-base-extra',
            f'other-trial-{old}-' + 'a' * 32 + '-base',
            'unrelated-work',
        }
        for name in expired | preserved:
            _ = trial.checked_git(self.repo, 'push', str(self.origin), self.base + ':refs/heads/' + name)
        kept = next(name for name in expired if name.endswith('-base'))
        expired.remove(kept)
        doc = self.root / 'protected-sweep.md'
        _ = doc.write_text(f'- **Merge branch:** {kept}\n')
        _ = fixture_member('showrunner', name='sweep-protection', doc=staff.LiveDoc(remote.local_machine(), doc))
        before = self.remote_branches()
        expected = {ref: commit for ref, commit in before.items() if ref.removeprefix('refs/heads/') not in expired}

        def service(_command: Sequence[str], _unit: str) -> int:
            remote = self.remote_branches()
            self.assertEqual({ref: commit for ref, commit in remote.items() if ref in before}, expected)
            self.assertEqual(len(set(remote) - set(before)), 2)
            return 0

        with (mock.patch.object(trial, 'paired_service', side_effect=service),
              mock.patch.object(time, 'time', return_value=now),
              mock.patch.object(trial, 'checked_git', wraps=trial.checked_git) as git):
            self.assertEqual(self.invoke_trial(), 0)
        self.assertEqual(self.remote_branches(), expected)
        listings = [call.args for call in git.call_args_list if call.args[1] == 'ls-remote']
        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[0][-1], 'refs/heads/gate-trial-*')

    def test_lower_lifetime_keeps_live_trial_branches(self) -> None:
        now = 1700000000
        before = self.remote_branches()
        config = self.root / 'cloud_gate.conf'
        lifetime = 5
        override_fixture_settings(config, f'lifetime_minutes={lifetime}\n')

        def first_service(_command: Sequence[str], _unit: str) -> int:
            live = self.remote_branches()
            self.assertEqual(len(set(live) - set(before)), 2)
            folder = next((self.state / 'paired-trials').iterdir())
            request = cloud_gate.read(next(folder.glob('*/request.json')))
            self.assertEqual(request['expires'], now + lifetime * 60)
            override_fixture_settings(config, 'lifetime_minutes=1\n')

            def second_service(_command: Sequence[str], _unit: str) -> int:
                remote = self.remote_branches()
                self.assertEqual({ref: commit for ref, commit in remote.items() if ref in live}, live)
                self.assertEqual(len(set(remote) - set(live)), 2)
                return 0

            with (mock.patch.object(time, 'time', return_value=now + 120),
                  mock.patch.object(trial, 'paired_service', side_effect=second_service)):
                self.assertEqual(self.invoke_trial(), 0)
            self.assertEqual(self.remote_branches(), live)
            return 0

        with (mock.patch.object(time, 'time', return_value=now),
              mock.patch.object(trial, 'paired_service', side_effect=first_service)):
            self.assertEqual(self.invoke_trial(), 0)
        self.assertEqual(self.remote_branches(), before)

    def test_start_sweep_failure_reports_once_and_continues(self) -> None:
        now = 1700000000
        old = now - 1
        branch = f'refs/heads/gate-trial-{old}-' + 'a' * 32 + '-base'
        _ = trial.checked_git(self.repo, 'push', str(self.origin), self.base + ':' + branch)
        before = self.remote_branches()
        git = trial.checked_git
        delete = origin_deletion.delete
        for failure in ('list', 'delete'):
            with self.subTest(failure=failure):
                def refuse_sweep(repo: Path, *arguments: str) -> str:
                    listing = arguments[0] == 'ls-remote' and arguments[-1] == 'refs/heads/gate-trial-*'
                    if failure == 'list' and listing:
                        raise subprocess.CalledProcessError(1, ['git', *arguments])
                    return git(repo, *arguments)

                def refuse_delete(repo: Path, destination: str, ref: str, expected: str, *, timeout: float
                                  ) -> origin_deletion.Deleted | origin_deletion.Absent | origin_deletion.Kept:
                    if failure == 'delete' and ref == branch:
                        self.assertEqual(expected, self.base)
                        return origin_deletion.Kept('fixture deletion failed', origin_deletion.RetentionCause.GIT_FAILURE)
                    return delete(repo, destination, ref, expected, timeout=timeout)

                errors = StringIO()
                with (mock.patch.object(trial, 'checked_git', side_effect=refuse_sweep),
                      mock.patch.object(origin_deletion, 'delete', side_effect=refuse_delete),
                      mock.patch.object(time, 'time', return_value=now),
                      mock.patch.object(trial, 'paired_service', return_value=0) as service,
                      redirect_stderr(errors)):
                    self.assertEqual(self.invoke_trial(), 0)
                service.assert_called_once()
                notice = ('paired trial: could not sweep old branches for gate-trial' if failure == 'list'
                          else f'paired trial: kept {branch}: fixture deletion failed')
                self.assertEqual(errors.getvalue().splitlines(), [notice])
                self.assertEqual(self.remote_branches(), before)

    def test_interrupted_service_deletes_both_trial_branches(self) -> None:
        service = trial.paired_service
        branches = self.remote_branches()
        for number in (signal.SIGINT,):
            with self.subTest(signal=number):
                def stop_terminal(_command: Sequence[str]) -> None:
                    signal.raise_signal(number)

                def interrupted(command: Sequence[str], unit: str) -> int:
                    self.assertEqual(len(set(self.remote_branches()) - set(branches)), 2)
                    with (mock.patch.object(subprocess, 'call', side_effect=stop_terminal),
                          mock.patch.object(subprocess, 'run') as stop):
                        result = service(command, unit)
                        stop.assert_called_once_with(['systemctl', '--user', 'stop', unit], check=False)
                        return result

                with mock.patch.object(trial, 'paired_service', side_effect=interrupted):
                    self.assertEqual(self.invoke_trial(), 1)
                self.assertEqual(self.remote_branches(), branches)

    def test_setup_failure_deletes_only_branches_already_pushed(self) -> None:
        git = trial.checked_git
        branches = self.remote_branches()
        for fail_after in (0, 1, 2):
            with self.subTest(pushed=fail_after):
                pushed: list[str] = []

                def fail_push(repo: Path, *arguments: str) -> str:
                    if arguments[0] == 'push' and '--delete' not in arguments:
                        if len(pushed) == fail_after:
                            raise subprocess.CalledProcessError(1, ['git', *arguments])
                        reply = git(repo, *arguments)
                        pushed.append(arguments[-1])
                        return reply
                    return git(repo, *arguments)

                output = StringIO()
                with (mock.patch.object(trial, 'checked_git', side_effect=fail_push),
                      mock.patch.object(cloud_gate, 'service_command', side_effect=ValueError('fixture setup failure')),
                      mock.patch.object(trial, 'paired_service') as service,
                      redirect_stdout(output), redirect_stderr(StringIO())):
                    self.assertEqual(self.invoke_trial(), 1)
                service.assert_not_called()
                self.assertEqual(len(pushed), fail_after)
                self.assertEqual(self.remote_branches(), branches)

    def test_accepted_push_with_client_error_is_deleted(self) -> None:
        git = trial.checked_git
        branches = self.remote_branches()
        accepted: list[str] = []

        def fail_after_acceptance(repo: Path, *arguments: str) -> str:
            reply = git(repo, *arguments)
            if arguments[0] == 'push' and '--delete' not in arguments:
                accepted.append(arguments[-1])
                raise subprocess.CalledProcessError(1, ['git', *arguments])
            return reply

        with (mock.patch.object(trial, 'checked_git', side_effect=fail_after_acceptance),
              mock.patch.object(trial, 'paired_service') as service,
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            self.assertEqual(self.invoke_trial(), 1)
        self.assertEqual(len(accepted), 1)
        service.assert_not_called()
        self.assertEqual(self.remote_branches(), branches)

    def test_cleanup_list_failure_names_trial_once_and_keeps_status(self) -> None:
        git = trial.checked_git
        for status in (0, 7):
            with self.subTest(status=status):
                listings: list[str] = []

                def refuse_listing(repo: Path, *arguments: str) -> str:
                    if arguments[0] == 'ls-remote' and arguments[-1] != 'refs/heads/gate-trial-*':
                        listings.append(arguments[-1])
                        raise subprocess.CalledProcessError(1, ['git', *arguments])
                    return git(repo, *arguments)

                errors = StringIO()
                with (mock.patch.object(trial, 'checked_git', side_effect=refuse_listing),
                      mock.patch.object(trial, 'paired_service', return_value=status), redirect_stderr(errors)):
                    self.assertEqual(self.invoke_trial(), status)
                self.assertEqual(len(listings), 1)
                name = listings[0].removeprefix('refs/heads/gate-trial-').removesuffix('-*')
                _epoch, _separator, trial_id = name.partition('-')
                self.assertEqual(errors.getvalue().splitlines(), [
                    'paired trial: could not list branches for trial ' + trial_id])
                self.assertEqual(self.remote_branches()['refs/heads/gate-trial-' + name + '-base'], self.base)
                self.assertEqual(self.remote_branches()['refs/heads/gate-trial-' + name + '-changed'], self.changed)

    def stalled_network_trial(self, *, interrupt: bool) -> None:
        git = shutil.which('git')
        assert git is not None
        tools = self.root / 'network-tools'
        tools.mkdir()
        pids = self.root / 'git-processes.json'
        group_record = self.root / 'git-process-group'
        fake = tools / 'git'
        _ = fake.write_text(f'''#!{sys.executable}
import json, os, pathlib, signal, subprocess, sys
arguments = sys.argv[1:]
if 'push' in arguments and '--delete' not in arguments:
    group_record = pathlib.Path({str(group_record)!r})
    pending_record = group_record.with_suffix('.tmp')
    pending_record.write_text(str(os.getpgrp()))
    pending_record.replace(group_record)
    subprocess.run([{git!r}, *arguments], check=True, stdout=subprocess.DEVNULL)
    child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])
    blocked = sorted(int(number) for number in signal.pthread_sigmask(signal.SIG_BLOCK, []))
    pathlib.Path({str(pids)!r}).write_text(json.dumps(dict(group=os.getpid(), child=child.pid, blocked=blocked)))
    os.write(sys.stdout.fileno(), b'R')
    if {interrupt!r}:
        os.kill(os.getppid(), signal.SIGINT)
    signal.pause()
os.execv({git!r}, [{git!r}, *arguments])
''')
        fake.chmod(0o700)
        cloud_gate.save(self.state / 'gate-deployment.json', {'template': 'fixture'})
        branches = self.remote_branches()
        script = '''import ctypes, json, os, select, signal, subprocess, sys
from pathlib import Path
from unittest import mock
from scripts.aws_target import aws_trial
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
repo, base, changed, state, settings, pids, interrupted = sys.argv[1:]
class FixtureGitProcess(subprocess.Popen):
    def communicate(self, input=None, timeout=None):
        if 'push' in self.args and '--delete' not in self.args and timeout is not None:
            assert select.select([self.stdout], [], [], TEST_HANG_GUARD_SECS)[0], 'Git did not report readiness'
            assert os.read(self.stdout.fileno(), 1) == b'R'
            timeout = TEST_HANG_GUARD_SECS if interrupted == '1' else 0
        return super().communicate(input=input, timeout=timeout)
with (mock.patch.object(aws_trial, 'account_settings_path', return_value=Path(settings)),
      mock.patch.object(aws_trial.subprocess, 'Popen', FixtureGitProcess),
      mock.patch.object(aws_trial, 'paired_service', side_effect=AssertionError('unexpected service'))):
    status = aws_trial.paired_main(['--repo', repo, '--base', base, '--changed', changed,
                                   '--modules', 'fixture/test_sample.py'], Path(state))
assert status == 1
processes = json.loads(Path(pids).read_text())
assert signal.SIGINT not in processes['blocked']
assert os.waitpid(processes['child'], 0)[0] == processes['child']
try:
    send_process_group(processes['group'], 0)
except ProcessLookupError:
    pass
else:
    raise AssertionError('Git process group survived')
'''
        try:
            reply = subprocess.run([sys.executable, '-c', script, str(self.repo), self.base, self.changed,
                                    str(self.state), str(self.settings), str(pids), str(int(interrupt))],
                                   capture_output=True, text=True,
                                   env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']},
                                   timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(reply.returncode, 0, reply.stderr)
            if not interrupt:
                self.assertIn('timed out', reply.stderr)
            self.assertEqual(self.remote_branches(), branches)
        finally:
            if group_record.exists():
                try:
                    send_process_group(int(group_record.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(sys.platform.startswith('linux'), 'The test requires a Linux child subreaper')
    def test_hung_push_kills_its_process_group_and_cleans_remote_branch(self) -> None:
        self.stalled_network_trial(interrupt=False)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'The test requires a Linux child subreaper')
    def test_keyboard_interrupt_during_push_kills_its_process_group_and_cleans_remote_branch(self) -> None:
        self.stalled_network_trial(interrupt=True)

    def test_keyboard_interrupt_after_first_push_deletes_the_pushed_branch(self) -> None:
        git = trial.checked_git
        branches = self.remote_branches()
        pushed: list[str] = []

        def interrupt_push(repo: Path, *arguments: str) -> str:
            reply = git(repo, *arguments)
            if arguments[0] == 'push' and '--delete' not in arguments:
                pushed.append(arguments[-1])
                raise KeyboardInterrupt
            return reply

        with (mock.patch.object(trial, 'checked_git', side_effect=interrupt_push),
              mock.patch.object(trial, 'paired_service') as service,
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            self.assertEqual(self.invoke_trial(), 1)
        self.assertEqual(len(pushed), 1)
        service.assert_not_called()
        self.assertEqual(self.remote_branches(), branches)

    def test_delete_failure_names_branch_once_and_keeps_trial_status(self) -> None:
        delete = origin_deletion.delete
        for status, protected in ((0, False), (7, False), (0, True)):
            with self.subTest(status=status, protected=protected):
                branches = self.remote_branches()
                retained: list[str] = []

                def service(_command: Sequence[str], _unit: str) -> int:
                    branch, = (ref for ref in self.remote_branches().keys() - branches.keys() if ref.endswith('-base'))
                    retained.append(branch)
                    if protected:
                        doc = self.root / 'protected-trial.md'
                        _ = doc.write_text(f"- **Merge branch:** {branch.removeprefix('refs/heads/')}\n")
                        _ = fixture_member('showrunner', name='trial-protection', doc=staff.LiveDoc(remote.local_machine(), doc))
                    return status

                def refuse_delete(repo: Path, destination: str, ref: str, expected: str, *, timeout: float = 30
                                  ) -> origin_deletion.Deleted | origin_deletion.Absent | origin_deletion.Kept:
                    if not protected and retained == [ref]:
                        return origin_deletion.Kept('fixture deletion failed', origin_deletion.RetentionCause.GIT_FAILURE)
                    return delete(repo, destination, ref, expected, timeout=timeout)

                errors = StringIO()
                with (mock.patch.object(origin_deletion, 'delete', side_effect=refuse_delete),
                      mock.patch.object(trial, 'paired_service', side_effect=service), redirect_stderr(errors)):
                    self.assertEqual(self.invoke_trial(), status)
                self.assertEqual(len(retained), 1)
                reason = 'trial-protection uses this production merge branch' if protected else 'fixture deletion failed'
                self.assertEqual(errors.getvalue().splitlines(), [f'paired trial: kept {retained[0]}: {reason}'])
                self.assertEqual(self.remote_branches(), {**branches, retained[0]: self.base})

    def test_module_validation_rejects_duplicate_missing_and_traversal(self) -> None:
        for modules in (('fixture/test_sample.py',) * 2, ('fixture/missing.py',), ('../test_sample.py',), ('/fixture/test_sample.py',)):
            with self.subTest(modules=modules), self.assertRaises((ValueError, subprocess.SubprocessError)):
                _ = self.request(modules)
        with self.assertRaises(ValueError):
            _ = self.request(rounds=0)

    def test_paired_median_uses_pairs_and_keeps_partial_failures(self) -> None:
        runs = [self.sample(variant, number, cpu) for number, pair in enumerate(((1, 0), (10, 9), (100, 2)), 1)
                for variant, cpu in zip(('base', 'changed'), pair)]
        summary = trial.module_summary('fixture/test_sample.py', 3, runs)
        self.assertEqual(summary['paired_median_saving_cpu_s'], 1)
        self.assertEqual(summary['base_median_cpu_s'], 10)
        self.assertEqual(summary['changed_median_cpu_s'], 2)
        self.assertTrue(summary['all_passed'])
        runs[0]['status'] = 1
        runs[1]['tests'] = 2
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['all_passed'])
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['same_test_count'])
        partial = trial.module_summary('fixture/test_sample.py', 3, runs[1:])
        self.assertFalse(partial['all_passed'])
        self.assertFalse(partial['same_test_count'])
        self.assertIsNone(trial.module_summary('fixture/test_sample.py', 3, [])['base_median_cpu_s'])

    def test_result_validation_checks_identity_rounds_and_box(self) -> None:
        request = trial.module_request(self.request(), 'fixture/test_sample.py', 'attempt', int(time.time()) + 600)
        row = {**request, 'verdict': 'pass', 'runs': [self.sample('base', 1, 1)]}
        trial.validate_paired_result(row, request, 'i-fixture')
        for key, value in (('attempt', 'other'), ('base', dict[str, object]()), ('rounds', 99)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                trial.validate_paired_result({**row, key: value}, request, 'i-fixture')
        with self.assertRaises(ValueError):
            trial.validate_paired_result({**row, 'runs': [self.sample('base', 1, 1)] * 2}, request, 'i-fixture')
        with self.assertRaises(ValueError):
            trial.validate_paired_result(row, request, 'i-other')

    def guest(self, *, limit: float | None = None) -> tuple[dict[str, object], list[dict[str, object]]]:
        from scripts.aws_target import gate_box, cloud_gate
        box = self.root / 'box'
        box.mkdir()
        guest_deadline_s = 600
        guest_delivery_margin_s = 60
        request = trial.module_request(self.request(), 'fixture/test_sample.py', 'attempt', int(time.time()) + guest_deadline_s)
        request['module_time_limit_s'] = guest_deadline_s if limit is None else limit
        cloud_gate.save(box / 'request.json', request)
        cloud_gate.save(box / 'links.json', {'production': True, 'step': 'size', 'log': 'log', 'result': 'result',
                                           'instance_type': 'm7a.8xlarge'})
        tools = self.root / 'tools'
        tools.mkdir()
        curl = tools / 'curl'
        _ = curl.write_text(f'''#!{sys.executable}
import json, pathlib, sys
config = pathlib.Path(sys.argv[sys.argv.index('--config') + 1])
name = json.loads(config.read_text().partition('= ')[2])
pathlib.Path({str(box)!r}, 'uploaded-' + name).write_bytes(sys.stdin.buffer.read())
print('200', end='')
''')
        curl.chmod(0o755)
        _ = (box / 'guest.py').write_text(gate_box.GUEST)
        trace = self.root / 'trace.jsonl'
        # The guest selects the candidate checkout after it clones the source.
        result = subprocess.run([sys.executable, '-E', str(box / 'guest.py')], capture_output=True, text=True,
            timeout=guest_deadline_s + guest_delivery_margin_s,
            env={**os.environ, 'GATE_BOX_DIR': str(box), 'PAIRED_TRACE': str(trace),
                 'PATH': str(tools) + os.pathsep + os.environ['PATH'], 'HOME': str(box)})
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = cloud_gate.read(box / 'uploaded-result')
        traces = [ledger.mapping(cast(object, json.loads(line))) for line in trace.read_text().splitlines()] if trace.exists() else []
        return reply, traces

    def test_local_guest_uses_two_trees_alternating_isolated_processes(self) -> None:
        reply, traces = self.guest()
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(reply['verdict'], 'pass', reply)
        self.assertEqual([row['variant'] for row in runs], ['base', 'changed', 'changed', 'base', 'base', 'changed'])
        self.assertEqual([row['round'] for row in runs], [1, 1, 2, 2, 3, 3])
        self.assertEqual(len({row['pid'] for row in traces}), 8)
        self.assertEqual(len({row['temporary'] for row in traces}), 8)
        self.assertEqual(len({row['home'] for row in traces}), 8)
        self.assertEqual(len({row['cwd'] for row in traces}), 2)
        self.assertTrue(all(row['tests'] == 1 and row['status'] == 0 for row in runs))
        self.assertTrue(all(isinstance(row['cpu_s'], float) and isinstance(row['peak_mib'], float) for row in runs))
        self.assertTrue((self.root / 'box/measure').exists())
        self.assertEqual(set(runs[0]), trial.RUN_FIELDS)

    def test_guest_warms_both_trees_without_result_rows_or_measurements(self) -> None:
        reply, traces = self.guest()
        self.assertEqual(len(traces), 8)
        self.assertEqual([Path(str(row['cwd'])).name for row in traces[:2]], ['base', 'changed'])
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertTrue(all(row['round'] in (1, 2, 3) for row in runs))
        log = (self.root / 'box/gate.log').read_text()
        self.assertEqual(sum(line.startswith('{"module":') for line in log.splitlines()), 6)
        summary = trial.module_summary('fixture/test_sample.py', 3, runs)
        self.assertEqual(summary['rounds'], 3)
        self.assertTrue(summary['all_passed'])

    def test_guest_timeout_keeps_runs_with_missing_cpu(self) -> None:
        reply, _ = self.guest(limit=0)
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertTrue(all(row['status'] == 124 and row['cpu_s'] is None for row in runs))
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['all_passed'])

    def test_slot_limit_refuses_before_service_or_admin_access(self) -> None:
        with mock.patch.object(subprocess, 'call', side_effect=AssertionError('service start')):
            output = StringIO()
            with redirect_stdout(output):
                status = trial.paired_main(['--base', self.base, '--changed', self.changed,
                    '--modules', *[f'fixture/test_{number}.py' for number in range(10)]], self.state)
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {'total_cost_usd': 0, 'cost_basis': 'estimate', 'cleanup_complete': True})

    def test_paired_worker_exit_during_cost_check_aborts_the_survivor(self) -> None:
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(int(time.time()),
            (ledger.PricedResource('fixture', Decimal(0)),), 'fixture'))
        cohort = self.root / 'trial'
        for name in ('first', 'second'):
            folder = cohort / name
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {'attempt': name, 'module': name, 'rounds': 1})
        context = multiprocessing.get_context('fork')
        cost_check = context.Event()
        pipe_closed = context.Event()
        received = cohort / 'received'

        def module(folder: Path, _directory: Path, _settings: Path, connection: trial.LaunchBarrier,
                   *, group: CloudGroup) -> None:
            self.assertEqual(group.size, 2)
            try:
                connection.send('ready')
                if folder.name == 'second':
                    self.assertTrue(cost_check.wait(TEST_HANG_GUARD_SECS))
                else:
                    _ = received.write_text(str(connection.recv()))
            finally:
                connection.close()
                if folder.name == 'second':
                    pipe_closed.set()

        def charges(_record_query: Callable[[Decimal], None]) -> None:
            cost_check.set()
            self.assertTrue(pipe_closed.wait(TEST_HANG_GUARD_SECS))

        errors = StringIO()
        with (mock.patch.object(trial, 'paired_module', new=module),
              mock.patch.object(trial, 'cleanup_trial', return_value=True),
              mock.patch.object(cloud_gate.GateWorker, 'reconcile'),
              mock.patch.object(cloud_gate.GateWorker, 'charges', side_effect=charges),
              redirect_stdout(StringIO()), redirect_stderr(errors)):
            self.assertEqual(trial.coordinate_trial(cohort, self.state, self.settings), 1)
        self.assertIn('second: The module ended before launch', errors.getvalue())
        self.assertEqual(received.read_text(), 'abort')

    def test_paired_launch_send_failure_aborts_only_workers_still_waiting(self) -> None:
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(int(time.time()),
            (ledger.PricedResource('fixture', Decimal(0)),), 'fixture'))
        cohort = self.root / 'trial'
        names = ('first', 'second', 'third', 'fourth')
        # The coordinator sends in the order of the request paths.
        for index, name in enumerate(names):
            folder = cohort / f'{index}-{name}'
            folder.mkdir(parents=True)
            cloud_gate.save(folder / 'request.json', {'attempt': folder.name, 'module': name, 'rounds': 1})
        context = multiprocessing.get_context('fork')
        exit_worker = context.Event()
        pipe_closed = context.Event()
        last_received = context.Event()

        def module(folder: Path, _directory: Path, _settings: Path, connection: trial.LaunchBarrier,
                   *, group: CloudGroup) -> None:
            self.assertEqual(group.size, len(names))
            try:
                connection.send('ready')
                if folder.name == '1-second':
                    self.assertTrue(exit_worker.wait(TEST_HANG_GUARD_SECS))
                    return
                message = connection.recv()
                if folder.name == '3-fourth':
                    last_received.set()
                if folder.name == '0-first':
                    self.assertTrue(last_received.wait(TEST_HANG_GUARD_SECS))
                _ = (folder / 'received').write_text(str(message))
            finally:
                connection.close()
                if folder.name == '1-second':
                    pipe_closed.set()

        send = Connection.send

        def send_then_stop_worker(connection: Connection, message: object) -> None:
            send(connection, message)
            if message == 'launch':
                exit_worker.set()
                self.assertTrue(pipe_closed.wait(TEST_HANG_GUARD_SECS))

        errors = StringIO()
        with (mock.patch.object(Connection, 'send', new=send_then_stop_worker),
              mock.patch.object(trial, 'paired_module', new=module),
              mock.patch.object(trial, 'reserve_trial'),
              mock.patch.object(trial, 'cleanup_trial', return_value=True),
              redirect_stdout(StringIO()), redirect_stderr(errors)):
            self.assertEqual(trial.coordinate_trial(cohort, self.state, self.settings), 1)
        self.assertIn('Broken pipe', errors.getvalue())
        self.assertEqual((cohort / '0-first' / 'received').read_text(), 'launch')
        for folder in ('2-third', '3-fourth'):
            self.assertEqual((cohort / folder / 'received').read_text(), 'abort')

    def test_paired_worker_leaves_launch_progress_to_the_box_request(self) -> None:
        folder = self.root / 'trial' / 'module'
        folder.mkdir(parents=True)
        request: dict[str, object] = {'attempt': 'module', 'gate_job': {
            'kind': 'StepJob', 'step': 'test-modules', 'unit': 'saved-unit', 'branch': '', 'modules': 1}}
        cloud_gate.save(folder / 'request.json', request)
        cloud_gate.save(folder / 'deployment.json', {})
        sidecar = folder / 'reservation.cgroup'
        now = 1_800_000_000.0
        trial_group = CloudGroup('trial', 3, now, Owner(1, 'fixture'))
        worker = cloud_gate.GateWorker(AwsCli('fixture', 'us-east-1', 1), ledger.CostLedger(self.state),
            self.settings, sidecar, clock=lambda: now)
        slot = cloud_gate.CloudReservation(sidecar)
        events: list[object] = []

        @contextmanager
        def reserve(repo: Path, path: Path, job: gate.SuiteMode | StepJob, *,
                    group: CloudGroup) -> Generator[cloud_gate.CloudReservation]:
            self.assertEqual((repo, path, job, group),
                (folder, sidecar, trial.PAIRED_TRIAL_JOB, trial_group))
            yield slot

        testcase = self

        class Barrier:
            def send(self, obj: object, /) -> None:
                events.append(obj)

            def recv(self) -> str:
                testcase.assertIsInstance(cloud_progress.read(sidecar), cloud_progress.NoCloudProgress)
                events.append('launch')
                return 'launch'

            def close(self) -> None:
                events.append('closed')

        def execute(path: Path, saved: dict[str, object], deployment: dict[str, object]) -> dict[str, object]:
            self.assertEqual((path, saved, deployment), (folder, request, {}))
            self.assertIsInstance(cloud_progress.read(sidecar), cloud_progress.NoCloudProgress)
            return {'status': 'complete'}

        with (mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
              mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=reserve),
              mock.patch.object(worker, 'execute', side_effect=execute),
              mock.patch.object(trial, 'cleanup_trial', return_value=True)):
            trial.paired_module(folder, self.state, self.settings, Barrier(), group=trial_group)
        self.assertEqual(events, ['ready', 'launch', 'closed'])
        self.assertEqual(cloud_gate.read(folder / 'outcome.json'), {'status': 'complete'})
        self.assertEqual(slot.status, 0)

    def test_terminal_exception_stops_the_owning_service(self) -> None:
        with (mock.patch.object(subprocess, 'call', side_effect=OSError('fixture')),
              mock.patch.object(subprocess, 'run') as stop,
              mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
            with self.assertRaises(OSError):
                _ = trial.paired_service(['fixture'], 'fixture-unit')
        stop.assert_called_once_with(['systemctl', '--user', 'stop', 'fixture-unit'], check=False)

    def test_terminal_interrupt_stops_the_owning_service(self) -> None:
        from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
        for number in (signal.SIGINT,):
            script = '''import signal, sys
from unittest import mock
from scripts.aws_target import aws_trial
with mock.patch.object(aws_trial.subprocess, 'call', side_effect=lambda command: signal.raise_signal(int(sys.argv[1]))), mock.patch.object(aws_trial.subprocess, 'run') as stop:
    assert aws_trial.paired_service(['fixture'], 'fixture-unit') == 1
    stop.assert_called_once_with(['systemctl', '--user', 'stop', 'fixture-unit'], check=False)
'''
            reply = subprocess.run([sys.executable, '-c', script, str(number)], capture_output=True, text=True,
                                   timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(reply.returncode, 0, reply.stderr)

    def test_guest_failed_sample_keeps_measurements_and_counts(self) -> None:
        module = self.repo / 'scripts/fixture/test_sample.py'
        _ = module.write_text(module.read_text().replace("self.assertFalse(pathlib.Path('generated').exists())", "self.fail('fixture failure')"))
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Failing fixture')
        self.changed = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        _ = trial.checked_git(self.repo, 'push', str(self.origin), 'HEAD:refs/heads/failure')
        reply, _ = self.guest()
        runs = ledger.rows(reply, 'runs')
        self.assertEqual(len(runs), 6)
        self.assertEqual([row['status'] for row in runs], [0, 1, 1, 0, 0, 1])
        self.assertTrue(all(row['cpu_s'] is not None and row['tests'] == 1 for row in runs))
        self.assertFalse(trial.module_summary('fixture/test_sample.py', 3, runs)['all_passed'])

    def test_paired_cli_preserves_the_ordered_regional_deployment(self) -> None:
        from scripts.aws_target import cloud_gate
        regions = [{'region': region, 'template': 'fixture-' + region} for region in ('us-east-2', 'us-west-2')]
        cloud_gate.save(self.state / 'gate-deployment.json', {'regions': regions, 'bucket': 'fixture-bucket'})
        with mock.patch.object(trial, 'paired_service', return_value=0):
            self.assertEqual(trial.main(['--base', self.base, '--changed', self.changed, '--repo', str(self.repo),
                '--modules', 'fixture/test_sample.py'], self.settings, self.state), 0)
        path = next((self.state / 'paired-trials').glob('*/*/deployment.json'))
        self.assertEqual(cloud_gate.read(path)['regions'], regions)

    def test_paired_cli_uses_scaler_without_admin_or_new_settings(self) -> None:
        from scripts.aws_target import cloud_gate
        cloud_gate.save(self.state / 'gate-deployment.json', {'template': 'fixture'})
        with (mock.patch.object(admin_secrets.AdminSecretSession, 'get', side_effect=AssertionError('admin read')),
              mock.patch.object(trial, 'paired_service', return_value=0) as service):
            self.assertEqual(trial.main(['--base', self.base, '--changed', self.changed, '--repo', str(self.repo),
                '--modules', 'fixture/test_sample.py'], self.settings, self.state), 0)
        command = cast(list[str], service.call_args.args[0])
        self.assertIn('--property=KillMode=mixed', command)
        self.assertTrue(any(value.startswith('--setenv=AWS_SHARED_CREDENTIALS_FILE=') for value in command))
        paths = list((self.state / 'paired-trials').glob('*/*/request.json'))
        self.assertEqual(len(paths), 1)
        self.assertEqual(cloud_gate.read(paths[0])['rounds'], 3)
