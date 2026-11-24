"""Measure trial lifecycles with a fake AWS command and clock."""
from __future__ import annotations

from scripts.lib.checkout_test_support import copy_startup, copy_test_runner

from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout, suppress
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import json
from math import inf
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import os
import pathlib
import select
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
from typing import Never, cast, final, override
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import admin_secrets, aws_trial as trial, cloud_gate, gate_box, ledger
from scripts.aws_target.ledger_test_support import CostFixture, install_aws_fixture_settings, override_fixture_settings, text_setting
from scripts.aws_target.aws_target import AwsCli
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue import cloud_progress, program_start, queue_place
from scripts.build_queue.admission_wire import CloudGroup, Owner
from scripts.lib.git_test_support import fixture_git_environment
from scripts.production import gate
from scripts.production.gate_modules import GateRefused
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lib.boot_identity import boot_identity
from scripts.lib.process_start import Gone, Started, process_start
from scripts.lib.fork_test_support import ForkChildren
from scripts.lib.socket_test_support import short_socket_directory
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.herdr.missing_binary import MISSING_HERDR_BIN


FIXTURE_BOOT = '01234567-89ab-cdef-0123-456789abcdef'


def assert_term_during_cleanup(test: unittest.TestCase, action: Callable[[Callable[[], Never]], int],
                               timeout: float) -> None:
    with ForkChildren() as children:
        reader, writer = children.pipe()

        def hang(*_args: object, **_options: object) -> Never:
            _ = os.write(writer, b'1')
            _ = threading.Event().wait()
            raise AssertionError('The fixture wait ended')

        child = children.start(lambda: action(hang), pass_fds=(writer,))
        test.assertTrue(select.select([reader], [], [], TEST_HANG_GUARD_SECS)[0])
        ready = os.read(reader, 1)
        if not ready:
            test.fail('The child exited before cleanup.\n' + child.result().stderr)
        os.kill(child.pid, signal.SIGTERM)
        reply = child.result(timeout=timeout + 3)
        test.assertEqual(reply.returncode, -signal.SIGTERM, reply.stderr)


def write_guest_config(root: Path, *, slow_test_s: float = 1.0) -> None:
    override_fixture_settings(root / 'cloud_gate.conf',
        'packages=git python3 cargo rustc rustup cargo-nextest uv curl openssh rsync gnupg ripgrep jq '
        + 'gcc pkg-config tmux taplo zsh bash gh sqlite chromium nodejs util-linux bubblewrap\n')
    _ = (root / 'test_times.conf').write_text(
        f'# range: 0..\nSLOW_TEST_S={slow_test_s}\n'
        + '# whole: 1..\nGATE_FAILURE_LOG_KIB=64\n'
        + '# whole: 1..\nTOP_RUNS=5\n')


@final
class GuestTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir='/tmp')))
        install_aws_fixture_settings(root)
        write_guest_config(root)
        _ = (root / 'gate.conf').write_text('# range: 1..\nGATE_TIME_LIMIT_FACTOR=1.5\n')
        _ = (root / 'buildlog.conf').write_text(
            '# range: 0..\nINDEX_UNUSED_DAYS=2\n'
            + '# range: 0..\nINDEX_TOUCH_MINUTES=60\n'
            + '# whole: 1..\nINDEX_SPILL_PAGES=65536\n')
        _ = (root / 'build_folders.conf').write_text(
            f'roots.system={root}\nroots.gate={root}\nroots.build={root / "build"}\n'
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
                namespace: dict[str, object] = {'pathlib': pathlib}
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
                self.assertEqual(namespace['memory_peak'], 800000 * 1024 if present else None)


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
            def reap(job: gate.RunningGateJob) -> None:
                nonlocal broken_finished
                original_reap(job)
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
        self.install_scope_fixture()
        _ = self.settings.write_text(self.settings.read_text().replace('region=us-east-1', 'region=us-east-2'))
        _ = self.enterContext(mock.patch.object(trial, 'account_settings_path', return_value=self.settings))
        # clear=True drops the caller's GIT_* variables, such as GIT_DIR or GIT_INDEX_FILE.
        self.enterContext(mock.patch.dict(os.environ, fixture_git_environment(self.root), clear=True))
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
        _ = (self.repo / 'scripts/buildlog/store.py').write_text(
            '"""The paired guest uploads its result without using the build log."""\n')
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
        _ = write_config_fixture(self.root / 'build_admission.conf',
                                 'program_stop_seconds=1\nprogram_sample_seconds=0.05\n')

    def install_scope_fixture(self) -> None:
        scopes = self.root / 'fake-scopes'
        scopes.mkdir()
        startup = self.root / 'startup'
        startup.mkdir()
        _ = (startup / 'sitecustomize.py').write_text(fr'''import io, os, shutil, signal
from pathlib import Path
# Initialize shutil before the fixture replaces os.open.
prefix = Path('/sys/fs/cgroup/fixture/builds.slice')
root = Path({str(scopes)!r})
old_open, old_iterdir, old_realpath = Path.open, Path.iterdir, os.path.realpath
old_os_open = os.open
old_write = Path.write_text
def mapped(path):
    return root / path.relative_to(prefix) if path.is_relative_to(prefix) else path
def live_members(path):
    members = []
    with old_open(mapped(path)) as source:
        for member in source.read().split():
            if member == '2147483647':
                members.append(member)
                continue
            try:
                state = Path('/proc', member, 'stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                continue
            if state != 'Z':
                members.append(member)
    return members
def fixture_open(path, *args, **kwargs):
    mode = args[0] if args else kwargs.get('mode', 'r')
    if path.is_relative_to(prefix) and path.name == 'cgroup.procs' and mode == 'r':
        return io.StringIO(' '.join(live_members(path)))
    if path == Path('/proc/self/cgroup') and 'FIXTURE_GIT_SCOPE' in os.environ:
        return io.StringIO('0::/fixture/builds.slice/' + os.environ['FIXTURE_GIT_SCOPE'] + '\n')
    return old_open(mapped(path), *args, **kwargs)
def fixture_os_open(file, flags, *args, **kwargs):
    if isinstance(file, (str, Path)):
        path = Path(file)
        if path.is_relative_to(prefix) and path.name == 'cgroup.events':
            population = '1' if live_members(path.with_name('cgroup.procs')) else '0'
            file = mapped(path)
            old_write(file, 'populated ' + population + '\n')
    return old_os_open(file, flags, *args, **kwargs)
def fixture_iterdir(path):
    return old_iterdir(mapped(path))
def fixture_realpath(path, *args, **kwargs):
    if isinstance(path, (str, Path)) and Path(path).is_relative_to(prefix):
        path = mapped(Path(path))
    return old_realpath(path, *args, **kwargs)
def fixture_write(path, data, *args, **kwargs):
    if path.is_relative_to(prefix) and path.name == 'cgroup.kill':
        for member in live_members(path.with_name('cgroup.procs')):
            if member != '2147483647':
                try:
                    os.kill(int(member), signal.SIGKILL)
                except ProcessLookupError:
                    pass
    return old_write(path, data, *args, **kwargs)
Path.open, Path.iterdir, os.path.realpath = fixture_open, fixture_iterdir, fixture_realpath
Path.write_text = fixture_write
os.open = fixture_os_open
''')
        systemd_run = self.root / 'systemd-run'
        _ = systemd_run.write_text(f'''#!{sys.executable}
import os, sys
from pathlib import Path
command = sys.argv[sys.argv.index('--') + 1:]
unit, = (arg.removeprefix('--unit=') for arg in sys.argv if arg.startswith('--unit='))
scope = Path({str(scopes)!r}) / unit
scope.mkdir()
(scope / 'cgroup.procs').write_text('2147483647' if os.environ.get('FIXTURE_GIT_BUSY') == '1' else '')
os.environ['FIXTURE_GIT_SCOPE'] = unit
os.execvp(command[0], command)
''')
        systemd_run.chmod(0o700)
        pythonpath = os.pathsep.join(filter(None, (str(startup), os.environ.get('PYTHONPATH', ''))))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'PATH': str(self.root) + os.pathsep + os.environ['PATH'],
            'PYTHONPATH': pythonpath,
            'FIXTURE_SCOPE_ROOT': str(scopes),
        })))

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
                           *, instance_type: str = '', lock_deadline: float = inf) -> None:
            self.assertIsInstance(outcome, ledger.GateAbsent)
            resolve(attempt, confirmed)
            costs.append(trial.trial_cost(book, {attempt}, self.settings, now)[0])
            resolve(attempt, outcome, instance_type=instance_type, lock_deadline=lock_deadline)
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

    def fixture_push(self, source: str, branch: str) -> None:
        _ = subprocess.run(['git', '-C', str(self.repo), 'push', str(self.origin), source + ':' + branch],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)

    def invoke_trial(self) -> int:
        if sys.platform != 'linux':
            self.skipTest('The trial controller requires Linux scopes')
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

        def inspect_git(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
            check_handlers(arguments[0])
            return git(repo, *arguments, recovery=recovery)

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
            self.fixture_push(self.base, 'refs/heads/' + name)
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
        _ = config.write_text(config.read_text().replace('lifetime_minutes=20', f'lifetime_minutes={lifetime}'))

        def first_service(_command: Sequence[str], _unit: str) -> int:
            live = self.remote_branches()
            self.assertEqual(len(set(live) - set(before)), 2)
            folder = next((self.state / 'paired-trials').iterdir())
            request = cloud_gate.read(next(folder.glob('*/request.json')))
            self.assertEqual(request['expires'], now + lifetime * 60)
            _ = config.write_text(config.read_text().replace(f'lifetime_minutes={lifetime}', 'lifetime_minutes=1'))

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
        self.fixture_push(self.base, branch)
        before = self.remote_branches()
        git = trial.checked_git
        for failure in ('list', 'delete'):
            with self.subTest(failure=failure):
                def refuse_sweep(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
                    listing = arguments[0] == 'ls-remote' and arguments[-1] == 'refs/heads/gate-trial-*'
                    deleting = arguments[0] == 'push' and '--delete' in arguments and arguments[-1] == branch
                    if (failure == 'list' and listing) or (failure == 'delete' and deleting):
                        raise subprocess.CalledProcessError(1, ['git', *arguments])
                    return git(repo, *arguments, recovery=recovery)

                errors = StringIO()
                with (mock.patch.object(trial, 'checked_git', side_effect=refuse_sweep),
                      mock.patch.object(time, 'time', return_value=now),
                      mock.patch.object(trial, 'paired_service', return_value=0) as service,
                      redirect_stderr(errors)):
                    self.assertEqual(self.invoke_trial(), 0)
                service.assert_called_once()
                self.assertEqual(errors.getvalue().splitlines(), ['paired trial: could not sweep old branches for gate-trial'])
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
                          mock.patch.object(trial, 'bounded_systemctl', return_value=(0, '')) as stop):
                        result = service(command, unit)
                        stop.assert_called_once_with(['stop', unit], mock.ANY)
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

                def fail_push(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
                    if arguments[0] == 'push' and '--delete' not in arguments:
                        if len(pushed) == fail_after:
                            raise subprocess.CalledProcessError(1, ['git', *arguments])
                        reply = git(repo, *arguments, recovery=recovery)
                        pushed.append(arguments[-1])
                        return reply
                    return git(repo, *arguments, recovery=recovery)

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

        def fail_after_acceptance(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
            reply = git(repo, *arguments, recovery=recovery)
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

                def refuse_listing(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
                    if arguments[0] == 'ls-remote' and arguments[-1] != 'refs/heads/gate-trial-*':
                        listings.append(arguments[-1])
                        raise subprocess.CalledProcessError(1, ['git', *arguments])
                    return git(repo, *arguments, recovery=recovery)

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

    def test_keyboard_interrupt_after_first_push_deletes_the_pushed_branch(self) -> None:
        git = trial.checked_git
        branches = self.remote_branches()
        pushed: list[str] = []

        def interrupt_push(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
            reply = git(repo, *arguments, recovery=recovery)
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
        git = trial.checked_git
        for status in (0, 7):
            with self.subTest(status=status):
                branches = self.remote_branches()
                refused: list[str] = []

                def refuse_delete(repo: Path, *arguments: str, recovery: Path | None = None) -> str:
                    if arguments[0] == 'push' and '--delete' in arguments and arguments[-1].endswith('-base'):
                        refused.append(arguments[-1])
                        raise subprocess.CalledProcessError(1, ['git', *arguments])
                    return git(repo, *arguments, recovery=recovery)

                errors = StringIO()
                with (mock.patch.object(trial, 'checked_git', side_effect=refuse_delete),
                      mock.patch.object(trial, 'paired_service', return_value=status), redirect_stderr(errors)):
                    self.assertEqual(self.invoke_trial(), status)
                self.assertEqual(len(refused), 1)
                self.assertEqual(errors.getvalue().splitlines(), [
                    'paired trial: could not delete branch ' + refused[0].removeprefix('refs/heads/')])
                self.assertEqual(self.remote_branches(), {**branches, refused[0]: self.base})

    @unittest.skipUnless(sys.platform == 'linux', 'The test observes process exit with pidfds')
    def test_network_git_stops_the_scope_and_its_child(self) -> None:
        for operation, event in (('push', 'timeout'), ('push', 'interrupt'), ('delete', 'timeout'),
                                 ('delete', 'TERM'), ('delete', 'recovery')):
            with self.subTest(operation=operation, event=event):
                self.assert_network_scope_stops(operation, event)

    def assert_network_scope_stops(self, operation: str, event: str) -> None:
        timeout = 8 if event == 'timeout' else 60
        stop_seconds = 1
        _ = write_config_fixture(self.root / 'cloud_gate.conf', f'git_network_timeout_s={timeout}\n')
        folder = self.root / f'{operation}-{event}'
        folder.mkdir()
        git = shutil.which('git')
        assert git is not None
        fake = folder / 'git'
        branches = self.remote_branches()
        with short_socket_directory() as socket_dir, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
            marker = str(Path(socket_dir) / 'ready')
            ready.bind(marker)
            _ = fake.write_text(f'''#!{sys.executable}
import os, signal, socket, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if {operation!r} == 'delete' and 'ls-remote' in args:
    print('fixture\\t' + args[-1].removesuffix('*') + 'base')
    raise SystemExit(0)
if 'push' not in args or ('--delete' in args) != ({operation!r} == 'delete'):
    os.execv({git!r}, [{git!r}, *args])
if {operation!r} == 'push':
    subprocess.run([{git!r}, *args], check=True, stdout=subprocess.DEVNULL)
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])
os.kill(child.pid, signal.SIGSTOP)
os.waitpid(child.pid, os.WUNTRACED)
scope = os.environ['FIXTURE_GIT_SCOPE']
members = Path(os.environ['FIXTURE_SCOPE_ROOT'], scope, 'cgroup.procs')
members.write_text(f'{{os.getpid()}} {{child.pid}}')
blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
assert not blocked.intersection((signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as report:
    report.sendto(f'{{os.getpid()}} {{child.pid}} {{scope}}'.encode(), {marker!r})
signal.pause()
''')
            fake.chmod(0o700)

            def run() -> int:
                if operation == 'push':
                    with mock.patch.object(trial, 'paired_service', side_effect=AssertionError('unexpected service')):
                        return self.invoke_trial()
                trial.delete_trial_branches(self.repo, str(self.origin), 'gate-trial', 123, folder)
                return 0

            def reap_recoverer(child: subprocess.Popen[str]) -> None:
                try:
                    child.kill()
                    _ = child.wait(timeout=stop_seconds + 1)
                except subprocess.TimeoutExpired:
                    self.fail('The Git recovery helper did not exit after SIGKILL.')
                finally:
                    for stream in (child.stdin, child.stdout, child.stderr):
                        if stream is not None:
                            stream.close()

            with (ForkChildren() as children, ExitStack() as descriptors,
                  mock.patch.dict(os.environ, {'PATH': str(folder) + os.pathsep + os.environ['PATH']})):
                controller = children.start(run)
                self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0])
                token, descendant, unit = ready.recv(256).decode().split()
                pids = int(token), int(descendant)
                exits = tuple(os.pidfd_open(pid) for pid in pids)
                for exit_fd in exits:
                    _ = descriptors.callback(os.close, exit_fd)
                try:
                    records = list((self.state / 'paired-trials').glob(f'*/{trial.GIT_RECOVERY}'))
                    record = records[0] if operation == 'push' else folder / trial.GIT_RECOVERY
                    saved = cloud_gate.read(record)
                    self.assertEqual(saved['unit'], unit)
                    self.assertEqual(Path(record.with_suffix('.cgroup').read_text().strip()).name, unit)
                    owner = ledger.instant(saved, 'owner')
                    owner_exit = os.pidfd_open(owner)
                    _ = descriptors.callback(os.close, owner_exit)
                    recoverer = None
                    if event == 'recovery':
                        recoverer = subprocess.Popen([sys.executable, '-c',
                            'import sys\nfrom pathlib import Path\nfrom scripts.aws_target.aws_trial import recover_git\n'
                            + 'print("ready", flush=True)\n'
                            + 'if sys.stdin.readline():\n    recover_git(Path(sys.argv[1]), float(sys.argv[2]), 0.05)',
                            str(record), str(stop_seconds)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
                        _ = descriptors.callback(reap_recoverer, recoverer)
                        assert recoverer.stdout is not None
                        self.assertTrue(select.select([recoverer.stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                        self.assertEqual(recoverer.stdout.readline(), 'ready\n')
                        os.kill(owner, signal.SIGSTOP)
                        os.kill(controller.pid, signal.SIGTERM)
                        os.kill(owner, signal.SIGKILL)
                        self.assertTrue(select.select([owner_exit], [], [], TEST_HANG_GUARD_SECS)[0])
                    deadline = (float(str(saved['deadline'])) if event == 'timeout'
                                else time.monotonic() + stop_seconds) + 1
                    if event == 'TERM':
                        os.kill(controller.pid, signal.SIGTERM)
                    if event == 'interrupt':
                        os.kill(controller.pid, signal.SIGINT)
                    if recoverer is not None:
                        _, errors = recoverer.communicate('recover\n', timeout=max(0, deadline - time.monotonic()))
                        self.assertEqual(recoverer.returncode, 0, errors)
                    for exit_fd in (*exits, owner_exit):
                        self.assertTrue(select.select([exit_fd], [], [], max(0, deadline - time.monotonic()))[0])
                    self.assertLessEqual(time.monotonic(), deadline)
                    reply = controller.result(timeout=TEST_HANG_GUARD_SECS)
                    status = 1 if operation == 'push' else 0 if event == 'timeout' else -signal.SIGTERM
                    self.assertEqual(reply.returncode, status, reply.stderr)
                    if event == 'timeout':
                        self.assertIn('timed out' if operation == 'push' else 'could not delete branch', reply.stderr)
                    self.assertFalse(record.exists())
                    self.assertEqual(self.remote_branches(), branches)
                finally:
                    for exit_fd in exits:
                        with suppress(ProcessLookupError):
                            signal.pidfd_send_signal(exit_fd, signal.SIGKILL)

    def test_git_recovery_stops_only_the_published_scope(self) -> None:
        path = self.root / trial.GIT_RECOVERY
        unit = 'cloud-trial-git-' + 'a' * 32 + '.scope'
        scope = Path('/sys/fs/cgroup/fixture/builds.slice') / unit
        owner = Owner(101, 'original')
        cloud_gate.save(path, {'boot': FIXTURE_BOOT, 'owner': owner.pid, 'owner_start': owner.start_time,
                               'unit': unit, 'deadline': 60.0})
        published = program_start.ProgramStart(owner, Owner(102, 'original'), str(scope))
        with (mock.patch.object(trial, 'boot_identity', return_value=FIXTURE_BOOT),
              mock.patch.object(trial, 'process_start', return_value=Gone()) as identity,
              mock.patch.object(queue_place, 'read_program_start', return_value=published) as read_start,
              mock.patch.object(queue_place, 'stop_scope') as stop,
              mock.patch.object(signal, 'pidfd_send_signal', create=True) as signal_group):
            trial.recover_git(path, 0.2, 0.05)
        identity.assert_called_once_with(owner.pid)
        read_start.assert_called_once_with(path.with_suffix('.scope.json'))
        stop.assert_called_once_with(scope, mock.ANY, 0.05)
        signal_group.assert_not_called()
        self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == 'linux', 'The scope helpers use Linux cgroups')
    def test_scoped_git_publishes_before_exec_and_keeps_a_timed_out_record(self) -> None:
        scopes = self.root / 'fake-scopes'
        git = self.root / 'git'
        _ = git.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
record = Path(sys.argv[1])
saved = json.loads(record.read_text())
published = json.loads(record.with_suffix('.scope.json').read_text())
assert saved['unit'] == os.environ['FIXTURE_GIT_SCOPE']
assert published['ready'] is True
assert Path(published['scope']).name == saved['unit']
assert record.with_suffix('.cgroup').read_text().strip() == published['scope']
Path(sys.argv[2]).write_text(saved['unit'])
print('Git read the published scope')
''')
        git.chmod(0o700)
        for busy in (False, True):
            with (self.subTest(busy=busy), mock.patch.dict(os.environ,
                  {'FIXTURE_GIT_BUSY': str(int(busy))})):
                folder = self.root / f'scope-{busy}'
                folder.mkdir()
                record = folder / trial.GIT_RECOVERY
                observed = folder / 'observed'
                command = [str(git), str(record), str(observed)]
                if busy:
                    with self.assertRaises(GateRefused) as stopped:
                        _ = trial.supervised_git(command, record, time.monotonic() + 60, 1)
                    self.assertIn('still has live processes', str(stopped.exception))
                    self.assertEqual(cloud_gate.read(record)['unit'], observed.read_text())
                    self.assertTrue(record.with_suffix('.scope.json').exists())
                    self.assertEqual((scopes / observed.read_text() / 'cgroup.kill').read_text(), '1\n')
                else:
                    self.assertEqual(trial.supervised_git(command, record, time.monotonic() + 60, 1), 'Git read the published scope')
                    self.assertFalse(record.exists())
                    self.assertTrue(observed.is_file())

    def test_git_recovery_keeps_records_without_a_valid_unit(self) -> None:
        path = self.root / trial.GIT_RECOVERY
        for unit in ({}, {'unit': ''}, {'unit': 5}, {'unit': 'unrelated.scope'}):
            with self.subTest(unit=unit):
                row: dict[str, object] = {'boot': FIXTURE_BOOT, 'owner': 101, 'owner_start': 'original',
                                          'deadline': 60.0, **unit}
                cloud_gate.save(path, row)
                with (mock.patch.object(trial, 'boot_identity') as boot,
                      mock.patch.object(trial, 'bounded_systemctl') as command,
                      mock.patch.object(queue_place, 'stop_scope') as stop,
                      self.assertRaisesRegex(GateRefused, 'UNRESOLVED: invalid Git recovery record')):
                    trial.recover_git(path, 1, 0.05)
                boot.assert_not_called()
                command.assert_not_called()
                stop.assert_not_called()
                self.assertEqual(cloud_gate.read(path), row)

    def test_git_recovery_keeps_a_record_with_a_truncated_boot_id(self) -> None:
        path = self.root / trial.GIT_RECOVERY
        row: dict[str, object] = {
            'boot': FIXTURE_BOOT[:-1], 'owner': 101, 'owner_start': 'original',
            'unit': 'cloud-trial-git-' + 'a' * 32 + '.scope', 'deadline': 60.0,
        }
        cloud_gate.save(path, row)
        with (mock.patch.object(trial, 'boot_identity', return_value=FIXTURE_BOOT),
              mock.patch.object(trial, 'query_git_scope') as query,
              self.assertRaisesRegex(GateRefused, 'UNRESOLVED: invalid Git recovery record')):
            trial.recover_git(path, 1, 0.05)
        query.assert_not_called()
        self.assertEqual(cloud_gate.read(path), row)

    @unittest.skipUnless(sys.platform == 'linux', 'The trial controller uses Linux scopes')
    def test_late_scope_exits_without_starting_git_after_supervisor_exit(self) -> None:
        path = self.root / trial.GIT_RECOVERY
        published = path.with_suffix('.scope.json')
        sidecar = path.with_suffix('.cgroup')
        unit = 'cloud-trial-git-' + 'a' * 32 + '.scope'
        started = self.root / 'git-started'
        git = self.root / 'git'
        _ = git.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(started)!r}).touch()\n')
        git.chmod(0o700)

        with ForkChildren() as children:
            reader, writer = children.pipe()

            def authorize() -> int:
                owner = os.getpid()
                identity = process_start(owner)
                assert isinstance(identity, Started)
                cloud_gate.save(path, {'boot': boot_identity(), 'owner': owner, 'owner_start': identity.start,
                                       'unit': unit, 'deadline': time.monotonic() + TEST_HANG_GUARD_SECS})
                _ = os.write(writer, b'1')
                return 0

            supervisor = children.start(authorize, pass_fds=(writer,))
            exited = supervisor.result()
            self.assertEqual(exited.returncode, 0, exited.stderr)
            record = trial.GitRecovery.read(path)
            with mock.patch.object(trial, 'query_git_scope', return_value=None):
                trial.recover_git(path, 1, 0.05)
            self.assertFalse(path.exists())

            result = subprocess.run([
                'systemd-run', '--unit=' + unit, '--', sys.executable, queue_place.__file__,
                '--scope-start', '--authorized', str(sidecar), str(record.owner), record.owner_start, str(published),
                str(reader), str(TEST_HANG_GUARD_SECS), str(git),
            ], pass_fds=(reader,), capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertTrue((self.root / 'fake-scopes' / unit).is_dir())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('the queue place holder exited before scope startup', result.stderr)
        self.assertFalse(published.exists())
        self.assertFalse(started.exists())

    @unittest.skipUnless(sys.platform == 'linux', 'The trial controller uses Linux scopes')
    def test_scope_launch_refusal_uses_the_saved_unit_and_clears_after_absence(self) -> None:
        record = self.root / trial.GIT_RECOVERY
        launches = self.root / 'scope-launches'
        systemd_run = self.root / 'systemd-run'
        _ = systemd_run.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
with Path({str(launches)!r}).open('a') as trace:
    trace.write(json.dumps(sys.argv[1:]) + '\\n')
saved = json.loads(Path({str(record)!r}).read_text())
assert '--unit=' + saved['unit'] in sys.argv
assert '--scope-start' in sys.argv
assert 'authorized' not in saved
raise SystemExit(17)
''')
        systemctl = self.root / 'systemctl'
        _ = systemctl.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
saved = json.loads(Path({str(record)!r}).read_text())
assert sys.argv[-1] == saved['unit']
assert sys.argv[1:3] == ['--user', 'show']
print('LoadState=not-found\\nActiveState=inactive\\nControlGroup=\\nJob=0')
raise SystemExit(1)
''')
        systemctl.chmod(0o700)

        with self.assertRaises(subprocess.CalledProcessError) as refused:
            _ = trial.supervised_git(['false'], record, time.monotonic() + 60, 5)
        self.assertEqual(refused.exception.returncode, 17)
        self.assertEqual(len(launches.read_text().splitlines()), 1)
        self.assertFalse(record.exists())

    def test_git_recovery_stops_an_unpublished_scope_by_unit(self) -> None:
        path = self.root / trial.GIT_RECOVERY
        unit = 'cloud-trial-git-' + 'b' * 32 + '.scope'
        group = '/fixture/builds.slice/' + unit
        cloud_gate.save(path, {'boot': FIXTURE_BOOT, 'owner': 101, 'owner_start': 'original',
                               'unit': unit, 'deadline': 60.0})
        reply = 0, f'LoadState=loaded\nActiveState=active\nControlGroup={group}\nJob=0\n'
        with (mock.patch.object(trial, 'boot_identity', return_value=FIXTURE_BOOT),
              mock.patch.object(trial, 'process_start', return_value=Gone()),
              mock.patch.object(trial, 'bounded_systemctl', return_value=reply) as show,
              mock.patch.object(queue_place, 'stop_scope') as stop,
              mock.patch.object(time, 'monotonic', return_value=100)):
            trial.recover_git(path, 1, 0.05)
        show.assert_called_once_with(
            ['show', '--all', '--property=LoadState,ActiveState,ControlGroup,Job', '--', unit], 101)
        stop.assert_called_once_with(Path('/sys/fs/cgroup' + group), 101, 0.05)
        self.assertFalse(path.exists())

    def test_scope_query_does_not_wait_for_a_stuck_child(self) -> None:
        clock = 100.0
        killed: list[float] = []

        class StuckChild:
            def poll(self) -> None:
                return None

            def kill(self) -> None:
                killed.append(clock)

        def pause(seconds: float) -> None:
            nonlocal clock
            clock += seconds

        with (mock.patch.object(time, 'monotonic', side_effect=lambda: clock),
              mock.patch.object(time, 'sleep', side_effect=pause),
              mock.patch.object(subprocess, 'Popen', return_value=StuckChild())):
            with self.assertRaisesRegex(GateRefused, 'UNRESOLVED: cannot determine'):
                _ = trial.query_git_scope('fixture.scope', 100.2)
        self.assertEqual(len(killed), 1)
        self.assertLess(killed[0], 100.2)
        self.assertEqual(clock, 100.2)

    def test_direct_coordinator_reports_a_kept_git_record(self) -> None:
        folder = self.state / 'paired-trials' / 'failed'
        folder.mkdir(parents=True)
        cloud_gate.save(folder / trial.GIT_RECOVERY, {})
        current = folder.parent / 'current'
        current.mkdir()
        with (mock.patch.object(trial, 'sweep_trial_resources'),
              mock.patch.object(trial, 'reserve_trial'),
              mock.patch.object(trial, 'cleanup_trial', return_value=True),
              mock.patch.object(trial, 'trial_cost', return_value=(Decimal(0), True)),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(trial.coordinate_trial(current, self.state, self.settings), 0)
        summary = ledger.mapping(cast(object, json.loads(output.getvalue())))
        self.assertIs(summary['cleanup_complete'], False)

    @unittest.skipUnless(sys.platform == 'linux', 'The trial controller uses Linux scopes')
    def test_git_recovery_uses_scope_absence_and_allows_an_unrelated_trial(self) -> None:
        boot = boot_identity()
        trials = self.state / 'paired-trials'
        absent = trials / 'absent' / trial.GIT_RECOVERY
        unknown = trials / 'unknown' / trial.GIT_RECOVERY
        for digit, path in (('a', absent), ('b', unknown)):
            path.parent.mkdir(parents=True)
            cloud_gate.save(path, {'boot': boot, 'owner': os.getpid(), 'owner_start': 'old',
                'unit': 'cloud-trial-git-' + digit * 32 + '.scope', 'deadline': 60.0})
        systemctl = self.root / 'systemctl'
        _ = systemctl.write_text(r'''#!/bin/sh
case "$*" in
    *cloud-trial-git-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.scope) printf 'LoadState=not-found\nActiveState=inactive\nControlGroup=\nJob=0\n' ;;
esac
exit 1
''')
        systemctl.chmod(0o700)

        def service(command: Sequence[str], _unit: str) -> int:
            folder = Path(command[command.index('--paired-worker') + 1])
            cloud_gate.save(folder / trial.TRIAL_COST_RECORD,
                            {'total_cost_usd': 1, 'cost_basis': 'estimate', 'cleanup_complete': True})
            return 0

        with (mock.patch.object(trial, 'paired_service', side_effect=service) as paired,
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            self.assertEqual(self.invoke_trial(), 0)
        paired.assert_called_once()
        self.assertFalse(absent.exists())
        self.assertTrue(unknown.exists())
        self.assertIn(f'UNRESOLVED: Git recovery kept {unknown}', errors.getvalue())
        self.assertEqual(json.loads(output.getvalue()),
                         {'total_cost_usd': 1, 'cost_basis': 'estimate', 'cleanup_complete': False})

    def test_module_validation_rejects_duplicate_missing_and_traversal(self) -> None:
        for modules in (('fixture/test_sample.py',) * 2, ('fixture/missing.py',), ('../test_sample.py',), ('/fixture/test_sample.py',)):
            with self.subTest(modules=modules), self.assertRaises((ValueError, GateRefused)):
                _ = self.request(modules)
        with self.assertRaises(ValueError):
            _ = self.request(rounds=0)

    def test_partial_clone_refuses_a_missing_module_without_fetching(self) -> None:
        name = 'scripts/fixture/test_sample.py'
        oid = trial.checked_git(self.repo, 'rev-parse', 'HEAD:' + name)
        relative = Path('objects') / oid[:2] / oid[2:]
        missing = self.repo / '.git' / relative
        self.assertTrue((self.origin / relative).is_file())
        missing.unlink()
        _ = trial.checked_git(self.repo, 'config', 'core.repositoryformatversion', '1')
        _ = trial.checked_git(self.repo, 'config', 'extensions.partialClone', 'origin')
        _ = trial.checked_git(self.repo, 'config', 'remote.origin.url', str(self.origin))
        _ = trial.checked_git(self.repo, 'config', 'remote.origin.promisor', 'true')
        trace = self.root / 'git-trace'
        with (mock.patch.dict(os.environ, {'GIT_NO_LAZY_FETCH': '0', 'GIT_TRACE': str(trace)}),
              self.assertRaisesRegex(GateRefused, 'Local Git failed')):
            _ = self.request()
        calls = trace.read_text()
        self.assertIn('built-in: git cat-file -t', calls)
        self.assertNotIn(' fetch ', calls)
        self.assertFalse(missing.exists())

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
        release_error: cloud_gate.CloudReleaseUnresolved | None = None

        @contextmanager
        def reserve(repo: Path, path: Path, job: gate.SuiteMode | StepJob, *,
                    group: CloudGroup) -> Generator[cloud_gate.CloudReservation]:
            self.assertEqual((repo, path, job, group),
                (folder, sidecar, trial.PAIRED_TRIAL_JOB, trial_group))
            yield slot
            if release_error is not None:
                raise release_error

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

        for case in ('released', 'release timeout', 'saved result and release timeout'):
            with self.subTest(case=case):
                if case != 'released':
                    release_error = cloud_gate.CloudReleaseUnresolved(
                        queue_place.CleanupUnresolved('the service did not confirm release'), time.monotonic())
                outcome_path = folder / 'outcome.json'
                saved: dict[str, object] = {'status': 'complete'}
                if case == 'saved result and release timeout':
                    saved.update(result_file='saved-result.json', cleanup='UNRESOLVED: earlier cleanup')
                    cloud_gate.save(outcome_path, saved)
                else:
                    outcome_path.unlink(missing_ok=True)
                events.clear()
                with (mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
                      mock.patch.object(cloud_gate, 'cloud_reservation', side_effect=reserve),
                      mock.patch.object(worker, 'execute', side_effect=execute),
                      mock.patch.object(trial, 'cleanup_trial', return_value=True),
                      redirect_stderr(StringIO()) as errors):
                    trial.paired_module(folder, self.state, self.settings, Barrier(), group=trial_group)
                self.assertEqual(events, ['ready', 'launch', 'closed'])
                if release_error is not None:
                    self.assertIn(str(release_error), errors.getvalue())
                    saved['cleanup'] = '\n'.join(filter(None, (str(saved.get('cleanup', '')), str(release_error))))
                self.assertEqual(cloud_gate.read(outcome_path), saved)
                self.assertEqual(slot.status, 0)

    def test_terminal_exception_stops_the_owning_service(self) -> None:
        with (mock.patch.object(subprocess, 'call', side_effect=OSError('fixture')),
              mock.patch.object(trial, 'bounded_systemctl', return_value=(0, '')) as stop,
              mock.patch.object(signal, 'signal', side_effect=AssertionError('handler'))):
            with self.assertRaises(OSError):
                _ = trial.paired_service(['fixture'], 'fixture-unit')
        stop.assert_called_once_with(['stop', 'fixture-unit'], mock.ANY)

    def test_terminal_interrupt_stops_the_owning_service(self) -> None:
        for number in (signal.SIGINT,):
            script = '''import signal, sys
from unittest import mock
from scripts.aws_target import aws_trial
with mock.patch.object(aws_trial.subprocess, 'call', side_effect=lambda command: signal.raise_signal(int(sys.argv[1]))), mock.patch.object(aws_trial, 'bounded_systemctl', return_value=(0, '')) as stop:
    assert aws_trial.paired_service(['fixture'], 'fixture-unit') == 1
    stop.assert_called_once_with(['stop', 'fixture-unit'], mock.ANY)
'''
            reply = subprocess.run([sys.executable, '-c', script, str(number)], capture_output=True, text=True,
                                   timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(reply.returncode, 0, reply.stderr)

    def test_term_during_each_trial_cleanup(self) -> None:
        _ = write_config_fixture(self.root / 'build_admission.conf', 'program_stop_seconds=2\n')
        _ = write_config_fixture(self.root / 'cloud_gate.conf',
                                 'api_timeout_s=1\ntermination_timeout_s=2\ngit_network_timeout_s=60\n')
        cohort = self.root / 'term-trial'
        cohort.mkdir()
        folder = cohort / 'module'
        folder.mkdir()
        cloud_gate.save(folder / 'request.json', {'attempt': folder.name})
        group = CloudGroup(cohort.name, 1, time.time(), Owner(os.getpid(), 'fixture'))

        def cleanup_command(hang: Callable[[], Never]) -> int:
            with mock.patch.object(trial, 'cleanup_trial', side_effect=hang):
                return trial.paired_cleanup_main(['--paired-cleanup', str(cohort)], self.settings, self.state)

        def cleanup_worker(hang: Callable[[], Never]) -> int:
            parent, connection = multiprocessing.get_context('fork').Pipe()
            slot = cloud_gate.CloudReservation(folder / 'reservation.cgroup')
            with (parent, connection,
                  mock.patch.object(cloud_gate, 'cloud_reservation', return_value=nullcontext(slot)),
                  mock.patch.object(trial, 'cleanup_trial', side_effect=hang)):
                parent.send('abort')
                trial.paired_module(folder, self.state, self.settings, connection, group=group)
            return 0

        def waiting_worker(_folder: Path, _directory: Path, _settings: Path, connection: trial.LaunchBarrier,
                           *, group: CloudGroup) -> None:
            del group
            connection.send('ready')
            _ = connection.recv()
            connection.close()

        def cleanup_coordinator(hang: Callable[[], Never], *, abort: bool) -> int:
            send = Connection.send

            def send_command(connection: Connection, message: object) -> None:
                if message == 'launch' and abort:
                    raise BrokenPipeError('launch failed')
                send(connection, message)

            with (mock.patch.object(trial, 'sweep_trial_resources'), mock.patch.object(trial, 'reserve_trial'),
                  mock.patch.object(trial, 'paired_module', new=waiting_worker),
                  mock.patch.object(Connection, 'send', new=send_command),
                  mock.patch.object(trial, 'cleanup_trial', side_effect=hang)):
                if abort:
                    with mock.patch.object(BaseProcess, 'join', side_effect=hang):
                        return trial.coordinate_trial(cohort, self.state, self.settings)
                return trial.coordinate_trial(cohort, self.state, self.settings)

        def stop_service(hang: Callable[[], Never]) -> int:
            with (mock.patch.object(subprocess, 'call', side_effect=KeyboardInterrupt),
                  mock.patch.object(trial, 'bounded_systemctl', side_effect=hang)):
                return trial.paired_service(['fixture'], 'fixture-unit')

        def delete_branches(hang: Callable[[], Never]) -> int:
            with (mock.patch.object(trial, 'paired_service', return_value=0),
                  mock.patch.object(trial, 'delete_trial_branches', side_effect=hang)):
                return self.invoke_trial()

        cases: tuple[tuple[str, Callable[[Callable[[], Never]], int]], ...] = (
            ('cleanup command', cleanup_command), ('worker cleanup', cleanup_worker),
            ('abort during launch', lambda hang: cleanup_coordinator(hang, abort=True)),
            ('coordinator cleanup', lambda hang: cleanup_coordinator(hang, abort=False)),
            ('service stop', stop_service), ('branch deletion', delete_branches),
        )
        for name, action in cases:
            with self.subTest(name=name):
                assert_term_during_cleanup(self, action, cloud_gate.cleanup_timeout_s())

    def test_guest_failed_sample_keeps_measurements_and_counts(self) -> None:
        module = self.repo / 'scripts/fixture/test_sample.py'
        _ = module.write_text(module.read_text().replace("self.assertFalse(pathlib.Path('generated').exists())", "self.fail('fixture failure')"))
        _ = trial.checked_git(self.repo, 'add', '.')
        _ = trial.checked_git(self.repo, 'commit', '-qm', 'Failing fixture')
        self.changed = trial.checked_git(self.repo, 'rev-parse', 'HEAD')
        self.fixture_push('HEAD', 'refs/heads/failure')
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
