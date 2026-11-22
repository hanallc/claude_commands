"""Test leased guest commands and root fences with private source and process state."""
from __future__ import annotations

from array import array
import errno
import fcntl
import hashlib
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from http.client import IncompleteRead
from io import BytesIO
import json
import os
from pathlib import Path
import select
import selectors
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
from types import SimpleNamespace
from typing import BinaryIO, NamedTuple, cast, override
import unittest
from unittest import mock
from urllib.request import Request

from scripts.build_queue import execution_identity
from scripts.aws_target import hana_protocol, hana_render
from scripts.aws_target import hana_guest as guest, hana_receiver as receiver
from scripts.aws_target import hana_result, hana_runner as runner, hana_source as source
from scripts.aws_target.hana_placement import HanaHostGeneration
from scripts.aws_target.hana_result import HanaAttemptKey, LogicalStepLink
from scripts.aws_target.ledger_test_support import override_fixture_settings
from scripts.aws_target import test_hana_receiver as receiver_tests, test_hana_source as source_tests
from scripts.lib import build_folders, build_slots, process_stat
from scripts.mac_test import linux_runner
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class DiskReading(NamedTuple):
    total: int
    used: int
    free: int


@dataclass
class FixtureCurl:
    returncode: int | None
    polls: int = 0
    kills: int = 0
    reaps: int = 0

    def poll(self) -> int | None:
        self.polls += 1
        return self.returncode

    def kill(self) -> None:
        self.kills += 1
        self.returncode = -9

    def wait(self) -> int:
        self.reaps += 1
        assert self.returncode is not None
        return self.returncode


class PrivateSupervisor:
    def __init__(self) -> None:
        self.starts: list[list[str]] = []
        self.stops: list[Path] = []
        self.mounted_value: bool = True
        self.departure_value: receiver.CommandDeparture = receiver.CommandDeparture(False, False)
        self.outcome_value: hana_protocol.HanaRunOutcome = hana_protocol.HanaExecutionLost('pending')
        self.source_starts: list[hana_protocol.HanaExecutionRequest] = []
        self.source_departure_value: receiver.CommandDeparture = receiver.CommandDeparture(False, False)
        self.source_outcome_value: hana_protocol.HanaSourceUpdateOutcome = hana_protocol.HanaSourceUpdateRefused('pending')

    def mounted(self) -> bool:
        return self.mounted_value

    def start(self, envelope: hana_protocol.HanaCommandEnvelope, directory: Path, arguments: list[str]) -> None:
        _ = envelope, directory
        self.starts.append(arguments)

    def stop(self, directory: Path) -> None:
        self.stops.append(directory)

    def departure(self, directory: Path, slot: int) -> receiver.CommandDeparture:
        _ = directory, slot
        if directory.name.endswith('-source'):
            return self.source_departure_value
        return self.departure_value

    def outcome(self, directory: Path) -> hana_protocol.HanaRunOutcome:
        _ = directory
        return self.outcome_value

    def start_source_update(self, request: hana_protocol.HanaExecutionRequest, directory: Path, arguments: list[str]) -> None:
        _ = directory, arguments
        self.source_starts.append(request)

    def source_outcome(self, directory: Path) -> hana_protocol.HanaSourceUpdateOutcome:
        _ = directory
        return self.source_outcome_value


class HanaRunnerTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.fixture: source_tests.HanaSourceTests
        self.root: Path
        self.binaries: Path
        self.manifest: hana_protocol.SourceManifest
        self.request: hana_protocol.HanaExecutionRequest
        self.paths: guest.GuestPaths
        self.permit: guest.WorkPermit
        self.settings: guest.SafetySettings
        self.descriptor: dict[str, object]
        self.access: hana_protocol.HanaExecutionAccess
        self.envelope: hana_protocol.HanaCommandEnvelope
        self.supervisor: PrivateSupervisor
        self.compiler_version: Path
        self.slot_policy: Path
        self.receiver_settings: receiver.ReceiverSettings

    @override
    def setUp(self) -> None:
        self.fixture = source_tests.HanaSourceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        _ = write_config_fixture(self.fixture.configuration.source.with_name('build_targets.conf'),
                                 'loopback.command_timeout_s=600\n')
        self.root = self.fixture.root
        self.receiver_settings = receiver.ReceiverSettings(7, 1, 83, 1048576, 120, 600)
        self.slot_policy = self.root / 'image-slots.conf'
        receiver_tests.write_policy(self.slot_policy, self.root / 'guest-build')
        self.compiler_version = self.root / 'compiler-version'
        _ = self.compiler_version.write_text('1.2.3\n')
        _ = self.enterContext(mock.patch.object(runner, 'IMAGE_COMPILER_VERSION', self.compiler_version))
        _ = self.enterContext(mock.patch.object(shutil, 'disk_usage',
            return_value=DiskReading(200 * 1024**3, 100 * 1024**3, 100 * 1024**3)))
        self.binaries = self.root / 'bin'
        self.binaries.mkdir()
        home = self.root / 'home'
        self.enterContext(mock.patch.dict(os.environ, {'HOME': str(home),
            'RUSTUP_HOME': str(home / '.rustup'), 'CARGO_HOME': str(home / '.cargo'),
            'PATH': str(self.binaries) + os.pathsep + os.environ['PATH']}))
        for name, body in {
            'nix': 'printf \'export TEST_SHELL_PROOF=ci\\n\'\n',
            'rustup': 'exit 0\n',
            'rustc': 'printf \'rustc 1.2.3 (fixture)\\n\'\n',
            'cargo': 'test -e /proc/$$/fd/$BUILD_FOLDER_LOCK_FD || exit 99\n'
                     + 'printf \'%s|%s|%s|%s\\n\' "$CARGO_BUILD_BUILD_DIR" "$RUST_BACKTRACE" '
                     + '"$TEST_SHELL_PROOF" "$*"\nexit 13\n',
        }.items():
            self.tool(name, body)
        _ = (self.fixture.checkout / 'flake.nix').write_text('{}\n')
        _ = (self.fixture.checkout / 'flake.lock').write_text('{}\n')
        self.manifest = self.fixture.prepare()
        key = HanaAttemptKey('episode', 'attempt', execution_identity.assigned_claim_id('episode', 'attempt'),
                             HanaHostGeneration('deployment', 'start', 'boot'))
        self.request = hana_protocol.HanaExecutionRequest(key, self.manifest, 1,
            execution_identity.SourceKeyKnown('verify-source'), self.manifest.argv, LogicalStepLink('call', 'episode'))
        self.paths = guest.GuestPaths(self.root / 'root-control', self.root / 'boot-id')
        self.paths.directory.mkdir(mode=0o700)
        _ = self.paths.boot_id.write_text('boot')
        self.permit = guest.WorkPermit(1100, '1', 'boot')
        self.settings = replace(guest.read_settings(self.fixture.configuration), enabled=True)
        guest.write_permit(self.settings, self.permit, expected_generation=0, paths=self.paths, clock=lambda: 1000)
        self.descriptor = {'version': 1, 'deployment': 'deployment', 'start_token': 'start', 'expires': 1200}
        self.access = hana_protocol.HanaExecutionAccess(
            'https://fixture.s3.us-east-2.amazonaws.com/observation',
            'https://fixture.s3.us-east-2.amazonaws.com/result', 1200)
        self.envelope = hana_protocol.HanaCommandEnvelope(self.request, self.permit, self.access)
        self.supervisor = PrivateSupervisor()

    def tool(self, name: str, body: str) -> None:
        path = self.binaries / name
        _ = path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def run_request(self, request: hana_protocol.HanaExecutionRequest, *, command_timeout_s: float = 600,
                    output_max_bytes: int = 16 * 1024 * 1024, build_start_fd: int = -1) -> hana_protocol.HanaRunOutcome:
        with self.fixture.guest(), self.fixture.verified_bundle(request.manifest) as bundle:
            return runner.materialize_and_run(request, bundle,
                command_timeout_s=command_timeout_s, output_max_bytes=output_max_bytes, build_start_fd=build_start_fd)

    def receive(self, envelope: hana_protocol.HanaExecutionEnvelope):
        return receiver.receive_execution(self.settings, self.descriptor, envelope, self.supervisor,
            paths=self.paths, clock=lambda: 1000, uptime=lambda: 10)

    def test_exact_command_keeps_cargo_environment_and_compiler_failure_result(self) -> None:
        override_fixture_settings(self.fixture.pool_settings, 'patterns.legacy-stable=other-{purpose}\n')
        with tempfile.TemporaryFile() as marker:
            outcome = self.run_request(self.request, build_start_fd=marker.fileno())
            _ = marker.seek(0)
            self.assertEqual(marker.read(), b'\n')
        self.assertIsInstance(outcome, hana_protocol.HanaCommandFinished)
        assert isinstance(outcome, hana_protocol.HanaCommandFinished)
        result = outcome.result
        self.assertEqual((result.status, result.argv, result.key), (13, self.request.argv, self.request.key))
        self.assertIn(b'hana/aws-hana/1/target|1|ci|check', result.stdout)
        cache = self.root / 'guest-build/hana/main/shells'
        self.assertEqual(len(list(cache.glob('*/exports.sh'))), 1)
        self.assertEqual(result.commit, self.manifest.commit)
        self.assertEqual(result.toolchain, self.manifest.toolchain)
        self.assertEqual(result.host, 'aws-hana')
        self.assertEqual(build_folders.build_folder_state(self.root / 'guest-build/hana/aws-hana/1'), 'free')
        self.assertEqual(hana_protocol.decode_outcome(hana_protocol.encode_outcome(outcome)), outcome)

    def test_cargo_exit_127_differs_from_a_missing_executable(self) -> None:
        # Limit the prepared shell to fixture executables.
        for name in ('flock', 'mkdir', 'python3'):
            tool = shutil.which(name)
            assert tool is not None
            (self.binaries / name).symlink_to(tool)
        hook = shlex.quote('export PATH=' + shlex.quote(str(self.binaries)))
        self.tool('nix', 'printf "%s\\n" ' + shlex.quote(f'shellHook={hook}') + '\n')

        for cargo_present in (True, False):
            with self.subTest(cargo_present=cargo_present), tempfile.TemporaryFile() as marker:
                if cargo_present:
                    self.tool('cargo', 'exit 127\n')
                else:
                    (self.binaries / 'cargo').unlink()
                outcome = self.run_request(self.request, build_start_fd=marker.fileno())
                _ = marker.seek(0)
                if cargo_present:
                    assert isinstance(outcome, hana_protocol.HanaCommandFinished)
                    self.assertEqual(outcome.result.status, 127)
                    self.assertEqual(marker.read(), b'\n')
                else:
                    assert isinstance(outcome, hana_protocol.HanaExecutionRefused)
                    self.assertIn('The guest could not start Cargo', outcome.reason)
                    self.assertEqual(marker.read(), b'')
                    _ = self.receive(self.envelope)
                    self.supervisor.departure_value = receiver.CommandDeparture(True, True)
                    self.supervisor.outcome_value = outcome
                    observation = self.receive(self.envelope)
                    assert isinstance(observation, hana_protocol.ConfirmedCommandAdmissionRefusal)
                    self.assertEqual(observation.refusal_code, guest.CommandAdmissionRefusalCode.PRE_COMMAND_FAILED)
                    self.assertEqual(observation.reason, outcome.reason)

    def test_command_mismatch_and_exact_slot_overflow_refuse(self) -> None:
        for request in (replace(self.request, argv=('cargo', 'clippy')), replace(self.request, slot=2)):
            with self.subTest(request=request.slot):
                self.assertIsInstance(self.run_request(request), hana_protocol.HanaExecutionRefused)
        self.assertFalse((self.root / 'guest-build/hana/aws-hana/2').exists())

    def test_missing_basis_reports_the_same_source_without_acquiring_a_slot(self) -> None:
        missing = replace(self.manifest, basis_commits=('f' * 40,))
        outcome = self.run_request(replace(self.request, manifest=missing))
        assert isinstance(outcome, hana_protocol.HanaExecutionRefused)
        self.assertIn('MissingBasis:', outcome.reason)
        self.assertIn('f' * 40, outcome.reason)
        self.assertFalse((self.root / 'guest-build/hana/aws-hana/1').exists())

    def test_output_bound_retains_a_lost_outcome_instead_of_a_compiler_result(self) -> None:
        directory = self.root / 'command-output'
        directory.mkdir()
        supervisor = receiver.GuestCommandSupervisor(self.receiver_settings, slot_policy=self.slot_policy)
        one_write = shlex.join([sys.executable, '-c', "import os; os.write(1, b'x' * 128)"])
        for script in ("printf '%0100d' 1\n", f'exec {one_write}\n'):
            with self.subTest(script=script), (directory / 'output').open('wb') as output:
                self.tool('cargo', script)
                outcome = self.run_request(self.request, output_max_bytes=32, build_start_fd=output.fileno())
                self.assertIsInstance(outcome, hana_protocol.HanaExecutionLost)
                _ = output.write(hana_protocol.encode_outcome(outcome))
            self.assertEqual(supervisor.outcome(directory), outcome)

    def test_command_exit_drains_output_while_a_descendant_holds_both_pipes(self) -> None:
        slot = build_slots.Slot(self.root, self.root, 'hana', 1, {})
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb'), os.fdopen(write_fd, 'wb'):
            script = '''
import os, sys
gate = int(sys.argv[1])
if os.fork() == 0:
    os.read(gate, 1)
    os._exit(0)
print('compiler output')
print('compiler diagnostic', file=sys.stderr)
raise SystemExit(13)
'''
            execution = linux_runner.LeasedSlotExecution(slot, (read_fd,))
            started = time.monotonic()
            result = linux_runner.capture_leased_command([sys.executable, '-c', script, str(read_fd)],
                execution, os.environ, 1024, timeout_s=10)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.status, result.stdout, result.stderr),
                         (13, b'compiler output\n', b'compiler diagnostic\n'))

    def require_command_exit(self, child: subprocess.Popen[bytes]) -> None:
        with os.fdopen(os.pidfd_open(child.pid), 'rb') as exit_fd:
            ready, _, _ = select.select([exit_fd], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(ready, 'The command did not exit')

    def test_command_exit_wins_when_the_runner_resumes_after_its_deadline(self) -> None:
        slot = build_slots.Slot(self.root, self.root, 'hana', 1, {})
        execution = linux_runner.LeasedSlotExecution(slot, ())
        command = [sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1); print("done"); sys.exit(13)']
        clock = 1000.0
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as child:
            assert child.stdin is not None
            release = child.stdin.close

            def resume(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
                nonlocal clock
                release()
                self.require_command_exit(child)
                clock = 1002.0
                return child

            with (mock.patch.object(subprocess, 'Popen', side_effect=resume),
                  mock.patch.object(time, 'monotonic', side_effect=lambda: clock)):
                result = linux_runner.capture_leased_command(command, execution, os.environ,
                    1024, timeout_s=1)
        self.assertEqual((result.status, result.stdout, result.stderr), (13, b'done\n', b''))

    def test_command_exit_reads_only_pending_bytes_from_pipes_with_a_continuous_writer(self) -> None:
        slot = build_slots.Slot(self.root, self.root, 'hana', 1, {})
        read_fd, write_fd = os.pipe()
        ready_read, ready_write = os.pipe()
        script = '''
import os, sys
if os.fork() == 0:
    if not os.read(int(sys.argv[1]), 1):
        os._exit(0)
    try:
        os.write(1, b'late output\\n')
        os.write(2, b'late diagnostic\\n')
        os.write(int(sys.argv[2]), b'r')
        while True:
            os.write(1, b'x' * 4096)
            os.write(2, b'y' * 4096)
    except BrokenPipeError:
        os._exit(0)
print('compiler output')
print('compiler diagnostic', file=sys.stderr)
raise SystemExit(13)
'''
        command = [sys.executable, '-c', script, str(read_fd), str(ready_write)]
        execution = linux_runner.LeasedSlotExecution(slot, (read_fd, ready_write))
        payload = b'x' * (3 * 65536 + 1)
        buffered = BytesIO(payload)
        read = os.read
        ioctl = fcntl.ioctl
        with (os.fdopen(read_fd, 'rb'), os.fdopen(write_fd, 'wb', buffering=0) as release,
              os.fdopen(ready_read, 'rb') as ready, os.fdopen(ready_write, 'wb'),
              subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               pass_fds=execution.descriptors) as child):
            self.require_command_exit(child)
            assert child.stdout is not None and child.stderr is not None
            stdout_fd = child.stdout.fileno()
            stderr_fd = child.stderr.fileno()
            pending_counts: dict[int, int] = {}

            def pending_at_exit(fd: int, request: int, count: array[int]) -> int:
                self.assertNotIn(fd, pending_counts)
                self.assertEqual(request, termios.FIONREAD)
                status = cast(int, ioctl(fd, request, count))
                # Simulate a pipe with capacity above the kernel default.
                if fd == stdout_fd:
                    count[0] += len(payload)
                pending_counts[fd] = count[0]
                if len(pending_counts) == 2:
                    _ = release.write(b'x')
                    readable, _, _ = select.select([ready], [], [], TEST_HANG_GUARD_SECS)
                    self.assertTrue(readable, 'The descendant did not write to the pipes')
                    self.assertEqual(ready.read(1), b'r')
                return status

            def read_buffered(fd: int, size: int) -> bytes:
                self.assertEqual(set(pending_counts), {stdout_fd, stderr_fd})
                if fd == stdout_fd and (chunk := buffered.read(size)):
                    return chunk
                return read(fd, size)

            started = time.monotonic()
            with (mock.patch.object(subprocess, 'Popen', return_value=child),
                  mock.patch.object(fcntl, 'ioctl', side_effect=pending_at_exit),
                  mock.patch.object(os, 'read', side_effect=read_buffered)):
                result = linux_runner.capture_leased_command(command, execution, os.environ,
                    len(payload) + 1024, timeout_s=10)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.status, result.stdout, result.stderr),
                         (13, payload + b'compiler output\n', b'compiler diagnostic\n'))

    def test_command_deadline_leaves_a_live_command_for_the_unit_to_stop(self) -> None:
        slot = build_slots.Slot(self.root, self.root, 'hana', 1, {})
        execution = linux_runner.LeasedSlotExecution(slot, ())
        script = '''
import os, sys
if sys.argv[1] == 'True':
    os.close(1)
    os.close(2)
os.write(int(sys.argv[2]), b'r')
sys.stdin.buffer.read(1)
'''
        for close_output in (False, True):
            with self.subTest(close_output=close_output):
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, 'rb') as ready, os.fdopen(write_fd, 'wb'):
                    command = [sys.executable, '-c', script, str(close_output), str(write_fd)]
                    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, pass_fds=(write_fd,), start_new_session=True) as child:
                        assert child.stdin is not None
                        try:
                            readable, _, _ = select.select([ready], [], [], TEST_HANG_GUARD_SECS)
                            self.assertTrue(readable, 'The command did not signal readiness')
                            self.assertEqual(ready.read(1), b'r')
                            started = time.monotonic()
                            with selectors.DefaultSelector() as streams:
                                def monotonic() -> float:
                                    count = len(streams.get_map())
                                    if not count:
                                        return 0.0
                                    if close_output and count > 1:
                                        self.assertLess(time.monotonic() - started, TEST_HANG_GUARD_SECS,
                                                        'The runner did not observe both closed output streams')
                                        return 0.0
                                    return 1.0

                                clock = SimpleNamespace(time=time.time, monotonic=monotonic)
                                selector = SimpleNamespace(DefaultSelector=lambda: streams, EVENT_READ=selectors.EVENT_READ)
                                with (mock.patch.object(subprocess, 'Popen', return_value=child),
                                      mock.patch.object(linux_runner, 'time', clock),
                                      mock.patch.object(linux_runner, 'selectors', selector),
                                      self.assertRaisesRegex(linux_runner.LeasedCommandLost, 'execution deadline')):
                                    _ = linux_runner.capture_leased_command(command, execution, os.environ,
                                        1024, timeout_s=1)
                            self.assertIsNone(child.poll())
                        finally:
                            child.stdin.close()
                            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_complete_source_update_repairs_the_original_capture_without_running_cargo(self) -> None:
        self.fixture.sender.acknowledge(hana_protocol.SourceIdentity(self.manifest.repository,
            self.manifest.commit, self.manifest.toolchain))
        _ = (self.fixture.checkout / 'tracked').write_text('captured update\n')
        incremental = self.fixture.prepare()
        original = replace(self.request, manifest=incremental, argv=incremental.argv)
        refused = self.run_request(original)
        assert isinstance(refused, hana_protocol.HanaExecutionRefused)
        self.assertIn('MissingBasis:', refused.reason)
        _ = (self.fixture.checkout / 'tracked').write_text('later caller edit\n')
        answer = source.MissingBasis(incremental.repository, incremental.basis_commits)
        with ExitStack() as staging:
            complete, upload = self.fixture.sender.retry_complete(self.fixture.checkout, incremental, answer,
                                                                  staging=staging)
            upload()
        self.assertEqual(complete.commit, incremental.commit)
        self.assertEqual(complete.basis_commits, ())
        with (self.fixture.guest(), self.fixture.verified_bundle(complete) as bundle,
              mock.patch.object(linux_runner, 'run_slot') as command,
              mock.patch.object(linux_runner, 'prepare_shell') as shell):
            updated = runner.materialize_source_update(replace(original, manifest=complete), bundle)
        assert isinstance(updated, hana_protocol.HanaSourceUpdated)
        self.assertEqual(updated.identity.commit, original.manifest.commit)
        self.assertEqual((self.root / 'guest-build/hana/aws-hana/1/tracked').read_text(), 'captured update\n')
        self.assertEqual(hana_protocol.decode_source_outcome(hana_protocol.encode_source_outcome(updated)), updated)
        command.assert_not_called()
        shell.assert_not_called()
        self.assertEqual(build_folders.build_folder_state(self.root / 'guest-build/hana/aws-hana/1'), 'free')

    def test_source_update_entry_point_materializes_source_without_cargo_or_shell_preparation(self) -> None:
        cargo = self.root / 'cargo-started'
        shell = self.root / 'shell-started'
        self.tool('cargo', f'touch {shlex.quote(str(cargo))}\n')
        self.tool('nix', f'touch {shlex.quote(str(shell))}\n')
        code = '''from unittest import mock
from scripts.aws_target import hana_runner
with mock.patch('socket.gethostname', return_value='aws-hana'):
    raise SystemExit(hana_runner.main())
'''
        content = json.dumps({'action': 'source-update',
            'request': cast(object, json.loads(hana_protocol.encode_request(self.request)))}).encode()
        with self.fixture.verified_bundle(self.manifest) as bundle:
            result = subprocess.run([sys.executable, '-c', code, '--bundle-fd', str(bundle.descriptor),
                '--bundle-offset', str(bundle.offset), '--bundle-size', str(bundle.size),
                '--command-timeout-s', '600'], input=content, pass_fds=(bundle.descriptor,),
                env={**os.environ, 'HOME': str(self.fixture.guest_home)},
                capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        updated = hana_protocol.decode_source_outcome(result.stdout)
        self.assertIsInstance(updated, hana_protocol.HanaSourceUpdated)
        self.assertFalse(cargo.exists() or shell.exists())

        # The bootstrap must apply the image limits before the runner acquires the exact slot.
        bootstrap_root = self.root / 'bootstrap'
        bootstrap_root.mkdir()
        slot_root = self.root / 'image-build'
        receiver_tests.write_policy(bootstrap_root / 'policy.conf', slot_root)
        entry = f'import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n' + code
        config = Path(os.environ['CONF_DIR'])
        payload = receiver_tests.archive_bytes(
            ('source.bundle', self.fixture.member(self.manifest, 'source.bundle'), tarfile.REGTYPE),
            ('runtime/abi', f'{hana_protocol.RUNTIME_ABI}\n'.encode(), tarfile.REGTYPE),
            ('runtime/runner.py', entry.encode(), tarfile.REGTYPE),
            *((f'runtime/config/{name}', (config / name).read_bytes(), tarfile.REGTYPE)
              for name in ('build_folders.conf', 'hana_aws.conf', 'copies.conf')),
        )
        request = replace(self.request, manifest=replace(self.manifest, bundle_sha256=hashlib.sha256(payload).hexdigest()))
        bootstrap, content, env = receiver_tests.bootstrap_fixture(bootstrap_root, payload, request,
                                                                  action='source-update')
        result = subprocess.run(bootstrap, input=content, capture_output=True, timeout=TEST_HANG_GUARD_SECS,
                                check=False, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIsInstance(hana_protocol.decode_source_outcome(result.stdout), hana_protocol.HanaSourceUpdated,
                              result.stdout)
        self.assertEqual((slot_root / 'hana/aws-hana/1/tracked').read_text(), 'committed\n')
        self.assertFalse(cargo.exists() or shell.exists())

    def test_isolated_runtime_writes_a_refusal_for_input_import_and_runtime_errors(self) -> None:
        runtime = self.root / 'runtime'
        for file in hana_render.runtime_files(Path(__file__).resolve().parents[2], self.fixture.configuration):
            path = runtime / file.path
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes(file.content)
            path.chmod(file.mode)
        source_module = runtime / 'scripts/aws_target/hana_source.py'
        source_text = source_module.read_text()
        runner_module = runtime / 'scripts/aws_target/hana_runner.py'
        runner_text = runner_module.read_text()
        request = cast(object, json.loads(hana_protocol.encode_request(self.request)))
        fixture_message = 'The source worker failed at /private/credentials with fixture-private-text'

        for case, action, expected, diagnostic in (
            ('arguments', 'execute', 'The Hana runner refused its arguments', 'The verified bundle range is invalid'),
            ('input', 'execute', 'The Hana runner stopped: ValueError', 'Invalid Hana integer: output_max_bytes'),
            ('missing-dependency', 'execute', 'The Hana runner stopped: ImportError', "cannot import name 'hana_source'"),
            ('missing-runner', 'execute', 'The Hana runner stopped: ImportError', "cannot import name 'hana_runner'"),
            ('runtime-error', 'execute', 'The Hana runner stopped: RuntimeError', fixture_message),
            ('runtime-error', 'source-update', 'The Hana runner stopped: RuntimeError', fixture_message),
            ('broken-string', 'execute', 'The Hana runner stopped: UnprintableError', '<exception str() failed>'),
            ('broken-stderr', 'execute', 'The Hana runner stopped: RuntimeError', None),
        ):
            with self.subTest(case=case, action=action), self.fixture.verified_bundle(self.manifest) as bundle:
                if case == 'missing-dependency':
                    source_module.unlink()
                else:
                    _ = source_module.write_text(source_text)
                if case == 'missing-runner':
                    runner_module.unlink()
                else:
                    script = runner_text
                    if case == 'runtime-error':
                        operation = 'materialize_and_run' if action == 'execute' else 'materialize_source_update'
                        script += f'\ndef {operation}(*args, **kwargs):\n    raise RuntimeError({fixture_message!r})\n'
                    if case == 'broken-string':
                        script += (
                            '\nclass UnprintableError(Exception):\n'
                            + '    def __str__(self):\n        raise RuntimeError("fixture-private-text")\n'
                            + '\ndef materialize_and_run(*args, **kwargs):\n    raise UnprintableError()\n')
                    if case == 'broken-stderr':
                        script += (
                            '\nclass UnwritableStderr:\n'
                            + '    def write(self, text):\n        raise OSError("The diagnostic write failed")\n'
                            + '    def flush(self):\n        pass\n'
                            + '\ndef materialize_and_run(*args, **kwargs):\n'
                            + '    sys.stderr = UnwritableStderr()\n    raise RuntimeError("fixture failure")\n')
                    _ = runner_module.write_text(script)
                fields = {'action': action, 'request': request}
                if case != 'input':
                    fields['output_max_bytes'] = 1024
                size = 0 if case == 'arguments' else bundle.size
                result = subprocess.run([sys.executable, '-IB', str(runtime / 'runner.py'),
                    '--bundle-fd', str(bundle.descriptor), '--bundle-offset', str(bundle.offset),
                    '--bundle-size', str(size), '--command-timeout-s', '600'],
                    input=json.dumps(fields).encode(), pass_fds=(bundle.descriptor,), capture_output=True,
                    cwd=runtime, env={**os.environ, 'CONF_DIR': str(runtime / 'config')},
                    timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0)
                if diagnostic is None:
                    self.assertEqual(result.stderr, b'')
                else:
                    self.assertIn(diagnostic.encode(), result.stderr)
                    if case != 'arguments':
                        self.assertIn(b'Traceback (most recent call last):', result.stderr)
                outcome = (hana_protocol.decode_source_outcome(result.stdout) if action == 'source-update'
                    else hana_protocol.decode_outcome(result.stdout))
                assert isinstance(outcome, (hana_protocol.HanaExecutionRefused, hana_protocol.HanaSourceUpdateRefused))
                self.assertEqual(outcome.reason, expected)

    def test_shell_failure_and_compiler_mismatch_start_no_cargo(self) -> None:
        cargo = self.root / 'cargo-started'
        self.tool('cargo', f'touch {cargo}\n')
        self.tool('nix', 'exit 9\n')
        self.assertIsInstance(self.run_request(self.request), hana_protocol.HanaExecutionRefused)
        self.assertFalse(cargo.exists())
        self.tool('nix', 'printf \'export TEST_SHELL_PROOF=ci\\n\'\n')
        self.tool('rustc', 'printf \'rustc 0.0.1 (wrong)\\n\'\n')
        self.assertIsInstance(self.run_request(self.request), hana_protocol.HanaExecutionRefused)
        self.assertFalse(cargo.exists())

    def test_missing_or_invalid_image_compiler_refuses_before_source_and_cargo(self) -> None:
        for value in (None, '', 'invalid', b'\xff'):
            with self.subTest(value=value):
                if value is None:
                    self.compiler_version.unlink()
                elif isinstance(value, bytes):
                    _ = self.compiler_version.write_bytes(value)
                else:
                    _ = self.compiler_version.write_text(value)
                with mock.patch.object(source, 'materialize') as materialize:
                    outcome = self.run_request(self.request)
                assert isinstance(outcome, hana_protocol.HanaExecutionRefused)
                self.assertIn('HanaCompilerVersionUnavailable:', outcome.reason)
                materialize.assert_not_called()

    def test_execution_settings_use_the_image_and_shared_deadlines_and_disk_floor(self) -> None:
        _ = write_config_fixture(self.fixture.configuration.source.with_name('build_targets.conf'),
            'loopback.shell_timeout_s=7\nloopback.command_timeout_s=600\n')
        override_fixture_settings(self.fixture.configuration.source, 'free_floor_gib=11\n')
        settings = runner.read_settings(self.fixture.configuration, 19)
        self.assertEqual((settings.compiler_version, settings.shell_timeout_s,
            settings.command_timeout_s, settings.free_floor_gib), ('1.2.3', 7, 19, 11))

    def test_missing_disk_space_refuses_before_cargo(self) -> None:
        cargo = self.root / 'cargo-started'
        self.tool('cargo', f'touch {shlex.quote(str(cargo))}\n')
        with mock.patch.object(shutil, 'disk_usage',
                return_value=DiskReading(200 * 1024**3, 200 * 1024**3, 0)):
            outcome = self.run_request(self.request)
        assert isinstance(outcome, hana_protocol.HanaExecutionRefused)
        self.assertIn('required free disk space', outcome.reason)
        self.assertFalse(cargo.exists())

    def test_wrapper_death_keeps_the_slot_held_until_its_child_exits(self) -> None:
        manifest = self.root / 'manifest.json'
        _ = manifest.write_bytes(self.manifest.encode())
        ready_read, ready_write = os.pipe()
        self.addCleanup(os.close, ready_read)
        self.addCleanup(os.close, ready_write)
        code = '''import os, pathlib, sys
from unittest import mock
from scripts.aws_target import hana_protocol, hana_source as source
from scripts.lib import conf
from scripts.mac_test import linux_runner
manifest_path = pathlib.Path(sys.argv[1])
ready = int(sys.argv[2])
bundle = source.VerifiedGitBundle(*map(int, sys.argv[3:]))
limits = source.read_limits(conf.read_or_repository('hana_aws'))
manifest = hana_protocol.decode_manifest(manifest_path.read_bytes(), limits)
with mock.patch('socket.gethostname', return_value='aws-hana'):
    with source.materialize(manifest, limits, bundle, number=1, work='child') as leased:
        execution = linux_runner.LeasedSlotExecution(leased.copy, (*leased.descriptors, ready))
        child = "import os,sys; os.write(int(sys.argv[1]), b'ready'); sys.stdin.buffer.read(1)"
        linux_runner.capture_leased_command([sys.executable, '-c', child, str(ready)], execution, os.environ, 1024,
                                            timeout_s=30)
'''
        with self.fixture.verified_bundle(self.manifest) as bundle:
            wrapper = subprocess.Popen([sys.executable, '-c', code, str(manifest),
                str(ready_write), str(bundle.descriptor), str(bundle.offset), str(bundle.size)], stdin=subprocess.PIPE,
                env={**os.environ, 'HOME': str(self.fixture.guest_home)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(ready_write, bundle.descriptor))
        try:
            readable, _, _ = select.select([ready_read], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(readable, 'The command child did not signal readiness')
            self.assertEqual(os.read(ready_read, 5), b'ready')
            wrapper.kill()
            _ = wrapper.wait(timeout=TEST_HANG_GUARD_SECS)
            slot = self.root / 'guest-build/hana/aws-hana/1'
            self.assertEqual(build_folders.build_folder_state(slot), 'held')
            with self.fixture.guest(), self.assertRaises(BlockingIOError), self.fixture.materialize(self.manifest):
                self.fail('The source reset ignored the child lease')
            assert wrapper.stdin is not None
            _ = wrapper.stdin.write(b'x')
            wrapper.stdin.close()
            # EOF proves that every inherited command output descriptor has closed.
            assert wrapper.stdout is not None
            self.assertEqual(wrapper.stdout.read(), b'')
            self.assertEqual(build_folders.build_folder_state(slot), 'free')
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
            if wrapper.stdin is not None and not wrapper.stdin.closed:
                _ = wrapper.stdin.write(b'x')
                wrapper.stdin.close()
            _ = wrapper.wait(timeout=TEST_HANG_GUARD_SECS)
            if wrapper.stdout is not None:
                wrapper.stdout.close()
            if wrapper.stderr is not None:
                wrapper.stderr.close()

    def test_command_accepts_once_and_preserves_resource_limits(self) -> None:
        first = self.receive(self.envelope)
        self.assertTrue(first.accepted)
        self.assertFalse(first.terminated)
        self.assertEqual(self.receive(self.envelope).key, first.key)
        self.assertEqual(len(self.supervisor.starts), 1)
        arguments = self.supervisor.starts[0]
        self.assertIn('--uid=hana-runner', arguments)
        self.assertIn('--property=CPUQuota=400%', arguments)
        self.assertIn('--property=MemoryMax=25769803776', arguments)
        self.assertIn('-I', arguments)
        self.assertTrue(any('from scripts.aws_target.hana_payload import main' in word for word in arguments))

    def test_fence_acknowledgement_prevents_a_delayed_command(self) -> None:
        fence = hana_protocol.HanaFenceEnvelope(self.request.key, 1, self.access)
        observed = self.receive(fence)
        self.assertTrue(observed.fenced and observed.terminated and observed.lease_free)
        delayed = self.receive(self.envelope)
        self.assertFalse(delayed.accepted)
        self.assertTrue(delayed.fenced)
        self.assertEqual(self.supervisor.starts, [])

    def test_accepted_command_keeps_fence_pending_until_cgroup_and_lease_release(self) -> None:
        _ = self.receive(self.envelope)
        fence = hana_protocol.HanaFenceEnvelope(self.request.key, 1, self.access)
        pending = self.receive(fence)
        self.assertTrue(pending.fenced and pending.accepted)
        self.assertFalse(pending.terminated or pending.lease_free)
        self.supervisor.departure_value = receiver.CommandDeparture(True, False, {'unit_exit_observed_at': 980})
        pending = self.receive(fence)
        self.assertFalse(pending.lease_free)
        self.assertEqual(pending.timing, {'unit_exit_observed_at': 980})
        self.supervisor.departure_value = receiver.CommandDeparture(True, True,
            {'unit_exit_observed_at': 985, 'lease_free_at': 990})
        departed = self.receive(fence)
        self.assertTrue(departed.lease_free)
        self.assertEqual(departed.timing, {'unit_exit_observed_at': 980, 'lease_free_at': 990})
        self.supervisor.departure_value = receiver.CommandDeparture(True, True,
            {'unit_exit_observed_at': 995, 'lease_free_at': 996})
        self.assertEqual(self.receive(fence).timing, departed.timing)
        self.assertEqual(len(self.supervisor.starts), 1)

    def test_receiver_restart_does_not_repeat_acceptance(self) -> None:
        _ = self.receive(self.envelope)
        self.supervisor = PrivateSupervisor()
        self.assertTrue(self.receive(self.envelope).accepted)
        self.assertEqual(self.supervisor.starts, [])

    def test_damaged_journal_refuses_another_launch(self) -> None:
        _ = self.receive(self.envelope)
        directory = receiver.command_directory(self.paths, self.request.key)
        record = hana_result.object_fields(cast(object, json.loads((directory / 'command.json').read_text())))
        record['accepted'] = 1
        _ = (directory / 'command.json').write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            _ = self.receive(self.envelope)
        self.assertEqual(len(self.supervisor.starts), 1)

    def test_changed_permission_refuses_the_exact_command(self) -> None:
        self.supervisor.outcome_value = hana_protocol.HanaExecutionRefused(hana_protocol.COMMAND_RESULT_PENDING)
        observed = self.receive(replace(self.envelope, permission=replace(self.permit, expires_at=1099)))
        self.assertTrue(observed.terminated and observed.lease_free)
        self.assertEqual(self.supervisor.starts, [])
        self.assertEqual(observed.evidence, execution_identity.ExecutionEvidenceUnavailable(
            'The acknowledged command permit changed', 1000))

    def test_source_update_acknowledgement_requires_departure_and_never_repeats_cargo(self) -> None:
        _ = self.receive(self.envelope)
        self.supervisor.outcome_value = hana_protocol.HanaExecutionRefused('MissingBasis: fixture')
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        self.supervisor.source_outcome_value = hana_protocol.HanaSourceUpdated(hana_protocol.SourceIdentity(
            self.manifest.repository, self.manifest.commit, self.manifest.toolchain))
        update = hana_protocol.HanaSourceUpdateEnvelope(self.request.key, self.request.slot, self.manifest, self.access)
        self.assertFalse(self.receive(update).source_updated)
        self.supervisor.source_departure_value = receiver.CommandDeparture(True, False)
        self.assertFalse(self.receive(update).source_updated)
        self.supervisor.source_departure_value = receiver.CommandDeparture(True, True)
        self.assertTrue(self.receive(update).source_updated)
        self.assertTrue(self.receive(update).source_updated)
        self.assertEqual(len(self.supervisor.source_starts), 1)
        self.assertEqual(len(self.supervisor.starts), 1)

    def test_failed_source_update_reports_its_own_cause_after_departure(self) -> None:
        for case, reason, source_starts in (
            ('download', 'The full bundle download failed', 1),
            ('directory', f'The Hana source repair directory could not be created: {os.strerror(errno.EEXIST)}', 0),
        ):
            with self.subTest(case=case):
                key = replace(self.request.key, attempt=case,
                    claim_id=execution_identity.assigned_claim_id(self.request.key.episode, case))
                envelope = replace(self.envelope, request=replace(self.request, key=key))
                self.supervisor = PrivateSupervisor()
                _ = self.receive(envelope)
                self.supervisor.outcome_value = hana_protocol.HanaExecutionRefused('MissingBasis: fixture')
                self.supervisor.departure_value = receiver.CommandDeparture(True, True)
                self.supervisor.source_outcome_value = hana_protocol.HanaSourceUpdateRefused(reason)
                update = hana_protocol.HanaSourceUpdateEnvelope(key, self.request.slot, self.manifest, self.access)
                self.supervisor.source_departure_value = receiver.CommandDeparture(True, False)
                directory = receiver.source_update_directory(receiver.command_directory(self.paths, key))
                if case == 'directory':
                    directory.touch()
                else:
                    self.assertNotIsInstance(self.receive(update), hana_protocol.ConfirmedCommandAdmissionRefusal)
                    self.supervisor.source_departure_value = receiver.CommandDeparture(True, True)
                refused = self.receive(update)
                assert isinstance(refused, hana_protocol.ConfirmedCommandAdmissionRefusal)
                self.assertEqual(refused.reason, reason)
                self.assertTrue(refused.terminated and refused.lease_free)
                self.assertFalse(refused.fenced)
                if case == 'directory':
                    directory.unlink()
                fence = hana_protocol.HanaFenceEnvelope(key, self.request.slot, self.access)
                for message in (update, envelope, fence):
                    observed = self.receive(message)
                    assert isinstance(observed, hana_protocol.ConfirmedCommandAdmissionRefusal)
                    self.assertEqual(observed.reason, refused.reason)
                    self.assertTrue(observed.terminated and observed.lease_free)
                    self.assertEqual(observed.fenced, message == fence)
                self.assertEqual(len(self.supervisor.source_starts), source_starts)
                self.assertEqual(len(self.supervisor.starts), 1)

    def test_source_update_rejects_a_changed_capture_and_fencing_stops_its_unit(self) -> None:
        _ = self.receive(self.envelope)
        self.supervisor.outcome_value = hana_protocol.HanaExecutionRefused('MissingBasis: fixture')
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        update = hana_protocol.HanaSourceUpdateEnvelope(self.request.key, self.request.slot, self.manifest, self.access)
        for manifest in (replace(self.manifest, commit='0' * 40),
                         replace(self.manifest, argv=('cargo', 'clippy')),
                         replace(self.manifest, basis_commits=('f' * 40,))):
            with self.subTest(manifest=manifest.commit), self.assertRaises(ValueError):
                _ = self.receive(replace(update, manifest=manifest))
        self.assertEqual(self.supervisor.source_starts, [])
        _ = self.receive(update)
        fence = hana_protocol.HanaFenceEnvelope(self.request.key, self.request.slot, self.access)
        observed = self.receive(fence)
        self.assertFalse(observed.lease_free)
        self.assertEqual(len(self.supervisor.stops), 2)
        self.assertTrue(self.supervisor.stops[1].name.endswith('-source'))
        with self.assertRaises(ValueError):
            _ = self.receive(update)
        self.supervisor.source_departure_value = receiver.CommandDeparture(True, True)
        self.assertTrue(self.receive(fence).lease_free)

    def test_result_publication_precedes_terminal_observation_and_idle_objects_do_nothing(self) -> None:
        outcome = self.run_request(self.request)
        assert isinstance(outcome, hana_protocol.HanaCommandFinished)
        departure_timing: hana_protocol.HanaCommandTiming = {'unit_exit_observed_at': 980, 'lease_free_at': 990}
        self.supervisor.departure_value = receiver.CommandDeparture(True, True, departure_timing)
        prefix = 'https://fixture.s3.us-east-2.amazonaws.com/'
        transport = self.receiver_settings
        for case, boot_deadline, access_deadline, cause in (
                ('uploaded', 1400, 1400, 'The command result is pending'),
                ('timeout', 1400, 1400, 'HanaResultUploadTimedOut: The result upload exceeded 83 s '
                                       + '(result_transfer_timeout_s)'),
                ('http-error', 1400, 1400, 'HanaResultUploadFailed: curl exited with status 22'),
                ('restart', 1400, 1400, 'HanaResultUploadFailed: The receiver restarted before it confirmed '
                                        + 'the result upload'),
                ('short-boot', 1195, 1400, 'HanaResultUploadFailed: The signed access window cannot fit a result '
                                           + 'upload of 83 s and settlement'),
                ('short-attempt', 1400, 1195, 'HanaResultUploadFailed: The signed access window cannot fit a result '
                                              + 'upload of 83 s and settlement')):
            with self.subTest(case=case):
                key = replace(self.request.key, attempt=case, claim_id=execution_identity.assigned_claim_id('episode', case))
                result = replace(outcome.result, key=key)
                self.supervisor.outcome_value = hana_protocol.HanaCommandFinished(result)
                envelope = replace(self.envelope, request=replace(self.request, key=key),
                                   access=replace(self.access, expires_at=access_deadline))
                descriptor = {**self.descriptor, 'expires': boot_deadline, 'control_url': prefix + 'control',
                              'report_url': prefix + 'report', 'command_url': prefix + 'command',
                              'fence_url': prefix + 'fence'}
                published: list[str] = []
                observations: list[hana_protocol.HanaExecutionObservation] = []

                def fetch(request: Request, timeout: int) -> bytes:
                    self.assertEqual(timeout, 7)
                    url = request.full_url
                    if request.get_method() == 'PUT':
                        published.append(url)
                        if url == self.access.observation_url:
                            assert request.data is not None
                            observations.append(hana_protocol.decode_observation(cast(bytes, request.data)))
                        return b'' if url.startswith('https:') else b'imds-token'
                    if url.endswith('/user-data'):
                        return json.dumps(descriptor).encode()
                    if url.endswith('/command'):
                        return hana_protocol.encode_envelope(envelope)
                    if url.endswith('/fence'):
                        return b'{"version":1,"action":"idle"}'
                    return b'{}'

                directory = receiver.command_directory(self.paths, key)
                if case == 'restart':
                    _ = self.receive(envelope)
                    guest.atomic_record(directory / 'result-upload.json', {'state': 'started', 'upload_started_at': 995})
                status = {'timeout': 28, 'http-error': 22}.get(case, 0)
                curl = FixtureCurl(status)

                def launch(arguments: list[str], *, stdin: BinaryIO, pass_fds: tuple[int, ...],
                           **_options: int) -> FixtureCurl:
                    self.assertNotIn(self.access.result_url, arguments)
                    self.assertIn(self.access.result_url.encode(), stdin.read())
                    self.assertEqual(arguments[arguments.index('--max-time') + 1], '83')
                    upload_path = Path(arguments[arguments.index('--upload-file') + 1])
                    self.assertEqual(upload_path, Path(f'/proc/self/fd/{pass_fds[0]}'))
                    self.assertEqual(hana_result.decode_result(upload_path.read_bytes()), result)
                    self.assertEqual(receiver.read_document((directory / 'result-upload.json').read_bytes()),
                                     {'state': 'started', 'upload_started_at': 1000})
                    published.append(self.access.result_url)
                    return curl

                with (mock.patch.object(receiver, 'GuestCommandSupervisor', return_value=self.supervisor),
                      mock.patch.object(subprocess, 'Popen', side_effect=launch) as popen):
                    for passes in (2, 1):
                        running = iter([True] * passes + [False])
                        receiver.run_receiver(self.settings, transport, paths=self.paths, slot_policy=self.slot_policy, fetch=fetch,
                            pause=lambda _seconds: None, clock=lambda: 1000, keep_running=lambda: next(running))
                if case.startswith('short-') or case == 'restart':
                    popen.assert_not_called()
                    expected_record: dict[str, object] = {'state': 'lost', 'reason': cause}
                    if case == 'restart':
                        expected_record['upload_started_at'] = 995
                    self.assertEqual(receiver.read_document((directory / 'result-upload.json').read_bytes()),
                                     expected_record)
                else:
                    popen.assert_called_once()
                    self.assertLess(published.index(self.access.result_url), published.index(self.access.observation_url))
                    self.assertEqual(curl.polls, 1)
                    self.assertEqual(curl.kills, 0)
                self.assertGreaterEqual(len(observations), 2)
                expected_timing = dict(departure_timing)
                if not case.startswith('short-'):
                    expected_timing['upload_started_at'] = 995 if case == 'restart' else 1000
                for observation in observations:
                    self.assertTrue(observation.terminated and observation.lease_free)
                    self.assertEqual(observation.evidence, execution_identity.ExecutionEvidenceUnavailable(cause, 1000))
                    self.assertEqual(observation.timing, expected_timing)
        self.assertEqual(len(self.supervisor.starts), 6)

    def test_receiver_renews_and_fences_during_an_upload_despite_failed_control_reads(self) -> None:
        outcome = self.run_request(self.request)
        assert isinstance(outcome, hana_protocol.HanaCommandFinished)
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        prefix = 'https://fixture.s3.us-east-2.amazonaws.com/'
        descriptor = {**self.descriptor, 'expires': 1400, 'control_url': prefix + 'control',
                      'report_url': prefix + 'report', 'command_url': prefix + 'command', 'fence_url': prefix + 'fence'}
        first = replace(self.envelope, access=replace(self.access, expires_at=1400))
        renewal = guest.WorkPermit(1150, '2', 'boot')
        next_key = replace(self.request.key, attempt='next', claim_id=execution_identity.assigned_claim_id('episode', 'next'))
        next_access = hana_protocol.HanaExecutionAccess(prefix + 'next-observation', prefix + 'next-result', 1400)
        second = hana_protocol.HanaCommandEnvelope(replace(self.request, key=next_key), renewal, next_access)
        fence = hana_protocol.HanaFenceEnvelope(self.request.key, 1, first.access)
        first_curl, second_curl = FixtureCurl(None), FixtureCurl(0)
        observations: list[hana_protocol.HanaExecutionObservation] = []
        fence_reads: list[int] = []
        acknowledged: list[object] = []
        turn = 0

        def fetch(request: Request, timeout: int) -> bytes:
            self.assertEqual(timeout, 7)
            url = request.full_url
            if url.endswith('/api/token'):
                return b'imds-token'
            if url.endswith('/user-data'):
                return json.dumps(descriptor).encode()
            if url.endswith('/control'):
                if turn == 1:
                    raise TimeoutError('fixture permit read timed out')
                if turn == 3:
                    return b'{"permit":'
                permit = renewal if turn >= 2 else self.permit
                control = {'deployment': 'deployment', 'start_token': 'start', 'token': 'permit-operation',
                           'execution': self.request.key.claim_id, 'permit': asdict(permit),
                           'expected_generation': 1}
                return json.dumps(control).encode()
            if url.endswith('/report'):
                assert request.data is not None
                report = receiver.read_document(cast(bytes, request.data))
                if turn == 2:
                    self.assertEqual(report['generation'], 2)
                    raise TimeoutError('fixture report write timed out')
                acknowledged.append(report['generation'])
                return b''
            if url.endswith('/fence'):
                fence_reads.append(turn)
                return (hana_protocol.encode_envelope(fence) if turn == 3 else b'{"version":1,"action":"idle"}')
            if url.endswith('/command'):
                self.supervisor.outcome_value = (hana_protocol.HanaCommandFinished(replace(outcome.result, key=next_key))
                                                 if turn >= 3 else outcome)
                return hana_protocol.encode_envelope(second if turn >= 3 else first)
            assert request.data is not None
            observation = hana_protocol.decode_observation(cast(bytes, request.data))
            if observation.fenced:
                self.assertEqual(first_curl.kills, 1)
                self.assertEqual(first_curl.reaps, 1)
            observations.append(observation)
            return b''

        def pause(_seconds: float) -> None:
            nonlocal turn
            if turn < 3:
                self.assertEqual(first_curl.polls, turn)
                self.assertEqual(first_curl.kills, 0)
                self.assertEqual(observations, [])
            turn += 1

        with (mock.patch.object(receiver, 'GuestCommandSupervisor', return_value=self.supervisor),
              mock.patch.object(subprocess, 'run', side_effect=AssertionError('Unexpected subprocess')),
              mock.patch.object(subprocess, 'Popen', side_effect=[first_curl, second_curl]) as launch):
            receiver.run_receiver(self.settings, self.receiver_settings, paths=self.paths, slot_policy=self.slot_policy,
                fetch=fetch, pause=pause, clock=lambda: 1000, keep_running=lambda: turn < 5)
        self.assertEqual(fence_reads, [0, 1, 2, 3, 4])
        self.assertEqual(guest.read_permit(self.paths.permit), renewal)
        self.assertEqual(acknowledged, [1, 2])
        self.assertEqual(self.supervisor.stops, [receiver.command_directory(self.paths, self.request.key)])
        self.assertEqual(len(self.supervisor.starts), 2)
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(first_curl.polls, 2)
        self.assertEqual(second_curl.polls, 1)
        fenced = observations[0]
        self.assertEqual(fenced.key, self.request.key)
        self.assertTrue(fenced.fenced and fenced.terminated and fenced.lease_free)
        self.assertEqual(fenced.timing, {'upload_started_at': 1000})

    def test_receiver_processes_fence_before_failing_permit_steps(self) -> None:
        prefix = 'https://fixture.s3.us-east-2.amazonaws.com/'
        descriptor = {**self.descriptor, 'expires': 1400, 'control_url': prefix + 'control',
                      'report_url': prefix + 'report', 'command_url': prefix + 'command', 'fence_url': prefix + 'fence'}
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        for case in ('truncated-report', 'restart-with-invalid-permit'):
            with self.subTest(case=case):
                key = replace(self.request.key, attempt=case, claim_id=execution_identity.assigned_claim_id('episode', case))
                access = replace(self.access, expires_at=1400)
                fence = hana_protocol.HanaFenceEnvelope(key, self.request.slot, access)
                permit = self.permit if case == 'truncated-report' else replace(self.permit, generation='invalid')
                control = {'deployment': 'deployment', 'start_token': 'start', 'token': 'permit-operation',
                           'execution': key.claim_id, 'permit': asdict(permit), 'expected_generation': 1}
                if case == 'restart-with-invalid-permit':
                    command = replace(self.envelope, request=replace(self.request, key=key), access=access)
                    self.assertTrue(self.receive(command).accepted)
                steps: list[str] = []

                def fetch(request: Request, _timeout: int) -> bytes:
                    url = request.full_url
                    if url.endswith('/api/token'):
                        return b'imds-token'
                    if url.endswith('/user-data'):
                        return json.dumps(descriptor).encode()
                    if url == access.observation_url:
                        assert request.data is not None
                        observation = hana_protocol.decode_observation(cast(bytes, request.data))
                        self.assertEqual(observation.key, key)
                        self.assertEqual(observation.accepted, case == 'restart-with-invalid-permit')
                        self.assertTrue(observation.fenced and observation.terminated and observation.lease_free)
                        steps.append('acknowledge fence')
                        return b''
                    if url == prefix + 'fence':
                        steps.append('read fence')
                        return hana_protocol.encode_envelope(fence)
                    if url == prefix + 'control':
                        steps.append('read permit')
                        return json.dumps(control).encode()
                    if url == prefix + 'report':
                        steps.append('acknowledge permit')
                        raise IncompleteRead(b'{', 1)
                    self.assertEqual(url, prefix + 'command')
                    steps.append('read command')
                    return b'{"version":1,"action":"idle"}'

                passes = iter((True, False))
                with (mock.patch.object(receiver, 'GuestCommandSupervisor', return_value=self.supervisor),
                      mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Unexpected upload'))):
                    receiver.run_receiver(self.settings, self.receiver_settings, paths=self.paths, slot_policy=self.slot_policy,
                        fetch=fetch, pause=lambda _seconds: None, clock=lambda: 1000, keep_running=lambda: next(passes))
                expected = ['read fence', 'acknowledge fence', 'read permit']
                if case == 'truncated-report':
                    expected.append('acknowledge permit')
                self.assertEqual(steps, [*expected, 'read command'])

    def test_root_keeps_publication_urls_outside_the_runner_input(self) -> None:
        _ = self.receive(self.envelope)
        directory = self.paths.directory / ('a' * 64)
        directory.mkdir(mode=0o700)
        supervisor = receiver.GuestCommandSupervisor(replace(self.receiver_settings, aws_timeout_s=10), slot_policy=self.slot_policy)
        with mock.patch.object(subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())) as launch:
            _ = supervisor.start(self.envelope, directory, self.supervisor.starts[0])
        arguments = cast(list[str], launch.call_args.args[0])
        self.assertIn('--property=ExitType=main', arguments)
        self.assertIn('--property=KillMode=control-group', arguments)
        self.assertIn('--property=RuntimeMaxSec=600', arguments)
        self.assertIn('--property=TimeoutStopSec=10', arguments)
        self.assertIn('--property=EnvironmentFile=/etc/hana-aws/runner-environment', arguments)
        content = (directory / 'input').read_bytes()
        self.assertNotIn(self.access.result_url.encode(), content)
        self.assertNotIn(self.access.observation_url.encode(), content)
        self.assertIn(self.manifest.bundle_url.encode(), content)

    def test_supervisor_requires_launcher_completion_and_the_exact_slot_lock_release(self) -> None:
        directory = self.paths.directory / ('b' * 64)
        directory.mkdir(mode=0o700)
        times = iter((1000.0, 1001.0, 1002.0))
        supervisor = receiver.GuestCommandSupervisor(replace(self.receiver_settings, aws_timeout_s=10),
                                                     slot_policy=self.slot_policy, clock=lambda: next(times))
        poll = mock.Mock(return_value=0)
        child = mock.Mock(poll=poll)
        supervisor.children[directory] = cast(subprocess.Popen[bytes], child)
        inactive = subprocess.CompletedProcess(['systemctl'], 0,
            'LoadState=not-found\nActiveState=inactive\nControlGroup=\nJob=0\n', '')
        journal_reads: list[list[str]] = []

        def inspect_unit(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
            if arguments[0] == 'systemctl':
                return inactive
            self.assertEqual(arguments[0], 'journalctl')
            self.assertIn('UNIT=' + receiver.command_unit(directory), arguments)
            self.assertIn('_PID=1', arguments)
            journal_reads.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, b'{"__REALTIME_TIMESTAMP":"999750000"}\n', b'')

        with mock.patch.object(subprocess, 'run', side_effect=inspect_unit), self.fixture.guest():
            slot = self.root / 'guest-build/hana/aws-hana/1.lock'
            slot.parent.mkdir(parents=True, exist_ok=True)
            with slot.open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self.assertEqual(supervisor.departure(directory, 1),
                                 receiver.CommandDeparture(True, False,
                                     {'unit_exited_at': 999.75, 'unit_exit_observed_at': 1000.0}))
            self.assertEqual(supervisor.departure(directory, 1),
                             receiver.CommandDeparture(True, True,
                                 {'unit_exited_at': 999.75, 'unit_exit_observed_at': 1001.0, 'lease_free_at': 1002.0}))
            (directory / 'launcher-finished.json').unlink()
            poll.return_value = -9
            self.assertEqual(supervisor.departure(directory, 1),
                             receiver.CommandDeparture(False, False, {'unit_exited_at': 999.75}))
        self.assertEqual(len(journal_reads), 2)

    def test_supervisor_recovers_the_unit_exit_time_after_a_receiver_restart(self) -> None:
        directory = self.paths.directory / ('d' * 64)
        directory.mkdir(mode=0o700)
        guest.atomic_record(directory / 'launcher.json', {'pid': 123, 'start_time': 456})
        supervisor = receiver.GuestCommandSupervisor(self.receiver_settings, slot_policy=self.slot_policy,
                                                     clock=lambda: 1000)
        inactive = subprocess.CompletedProcess(['systemctl'], 0,
            'LoadState=not-found\nActiveState=inactive\nControlGroup=\nJob=0\n', '')
        entry = subprocess.CompletedProcess(['journalctl'], 0, b'{"__REALTIME_TIMESTAMP":"999750000"}\n', b'')
        with (mock.patch.object(process_stat, 'process_fields', side_effect=FileNotFoundError),
              mock.patch.object(subprocess, 'run', side_effect=[inactive, entry, inactive]) as run,
              self.fixture.guest()):
            self.assertEqual(supervisor.departure(directory, 1),
                             receiver.CommandDeparture(False, False, {'unit_exited_at': 999.75}))
            (self.root / 'guest-build').mkdir(exist_ok=True)
            guest.atomic_record(directory / 'stopped.json', {'stopped': True})
            self.assertEqual(supervisor.departure(directory, 1), receiver.CommandDeparture(True, True, {
                'unit_exited_at': 999.75, 'unit_exit_observed_at': 1000, 'lease_free_at': 1000,
            }))
        arguments = cast(list[str], run.call_args_list[1].args[0])
        self.assertIn('--boot', arguments)
        self.assertIn('UNIT=' + receiver.command_unit(directory), arguments)

    def test_supervisor_reads_a_late_exit_time_on_the_next_pass(self) -> None:
        directory = self.paths.directory / ('e' * 64)
        directory.mkdir(mode=0o700)
        supervisor = receiver.GuestCommandSupervisor(self.receiver_settings, slot_policy=self.slot_policy,
                                                     clock=lambda: 1000)
        child = mock.Mock(poll=mock.Mock(return_value=0))
        supervisor.children[directory] = cast(subprocess.Popen[bytes], child)
        inactive = subprocess.CompletedProcess(['systemctl'], 0,
            'LoadState=not-found\nActiveState=inactive\nControlGroup=\nJob=0\n', '')
        missing = subprocess.CompletedProcess(['journalctl'], 0, b'', b'')
        entry = subprocess.CompletedProcess(['journalctl'], 0, b'{"__REALTIME_TIMESTAMP":"999750000"}\n', b'')
        with (mock.patch.object(subprocess, 'run', side_effect=[inactive, missing, inactive, entry, inactive]) as run,
              self.fixture.guest()):
            slot = self.root / 'guest-build/hana/aws-hana/1.lock'
            slot.parent.mkdir(parents=True, exist_ok=True)
            with slot.open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                pending = supervisor.departure(directory, 1)
                self.assertEqual(pending, receiver.CommandDeparture(True, False, {'unit_exit_observed_at': 1000}))
                recovered = supervisor.departure(directory, 1)
                self.assertEqual(recovered, receiver.CommandDeparture(True, False, {
                    'unit_exit_observed_at': 1000, 'unit_exited_at': 999.75,
                }))
            completed = supervisor.departure(directory, 1)
            self.assertEqual(completed, receiver.CommandDeparture(True, True, {
                'unit_exit_observed_at': 1000, 'unit_exited_at': 999.75, 'lease_free_at': 1000,
            }))
        self.assertEqual(run.call_count, 5)

    def test_supervisor_reports_a_terminal_deadline_after_the_cgroup_and_slot_release(self) -> None:
        directory = self.paths.directory / ('c' * 64)
        directory.mkdir(mode=0o700)
        supervisor = receiver.GuestCommandSupervisor(replace(self.receiver_settings, aws_timeout_s=10), slot_policy=self.slot_policy)
        supervisor.children[directory] = cast(subprocess.Popen[bytes], mock.Mock(poll=mock.Mock(return_value=1)))
        expired = subprocess.CompletedProcess(['systemctl'], 0,
            'LoadState=loaded\nActiveState=failed\nControlGroup=\nJob=0\nResult=timeout\n', '')
        with mock.patch.object(subprocess, 'run', return_value=expired), self.fixture.guest():
            slot = self.root / 'guest-build/hana/aws-hana/1.lock'
            slot.parent.mkdir(parents=True, exist_ok=True)
            with slot.open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self.assertFalse(supervisor.departure(directory, 1).lease_free)
            departure = supervisor.departure(directory, 1)
            self.assertTrue(departure.terminated and departure.lease_free)
        outcome = supervisor.outcome(directory)
        assert isinstance(outcome, hana_protocol.HanaExecutionRefused)
        self.assertIn('could not start the build', outcome.reason)
        _ = (directory / 'output').write_bytes(b'\n')
        outcome = supervisor.outcome(directory)
        assert isinstance(outcome, hana_protocol.HanaExecutionLost)
        self.assertIn('execution deadline', outcome.reason)
        completed = self.run_request(self.request)
        assert isinstance(completed, hana_protocol.HanaCommandFinished)
        _ = (directory / 'output').write_bytes(b'\n' + hana_protocol.encode_outcome(completed))
        self.assertEqual(supervisor.outcome(directory), outcome)

    def test_missing_mount_changed_permit_and_boot_refuse_launch(self) -> None:
        self.supervisor.mounted_value = False
        self.supervisor.outcome_value = hana_protocol.HanaExecutionRefused('Unrelated runner diagnostic')
        observed = self.receive(self.envelope)
        self.assertTrue(observed.terminated and observed.lease_free)
        assert isinstance(observed, hana_protocol.ConfirmedCommandAdmissionRefusal)
        self.assertEqual(observed.refusal_code, guest.CommandAdmissionRefusalCode.DATA_NOT_MOUNTED)
        self.assertEqual(observed.evidence, execution_identity.ExecutionEvidenceUnavailable('The Hana data directory is not mounted', 1000))
        self.assertEqual(self.receive(self.envelope), observed)
        self.assertEqual(self.supervisor.starts, [])
        with self.assertRaises(ValueError):
            _ = self.receive(replace(self.envelope, request=replace(self.request,
                key=replace(self.request.key, generation=HanaHostGeneration('deployment', 'start', 'other')))))

    def test_watchdog_stops_during_source_and_shell_preparation(self) -> None:
        for phase in ('source', 'shell'):
            with self.subTest(phase=phase):
                self.paths.fence.unlink(missing_ok=True)
                commands: list[list[str]] = []

                def expire() -> None:
                    guest.run_safety('watchdog', self.settings, paths=self.paths,
                        clock=lambda: 1200, uptime=lambda: 200, run=commands.append)

                if phase == 'source':
                    copy_bundle = source.VerifiedGitBundle.copy_to

                    def copy(bundle: source.VerifiedGitBundle, destination: Path) -> None:
                        expire()
                        copy_bundle(bundle, destination)

                    with mock.patch.object(source.VerifiedGitBundle, 'copy_to', copy):
                        _ = self.run_request(self.request)
                else:
                    prepare = linux_runner.prepare_shell
                    def shell(root: Path, cache: Path, selector: str, timeout_s: float, *,
                              pass_fds: tuple[int, ...]):
                        self.assertTrue(pass_fds)
                        self.assertEqual(pass_fds, linux_runner.execution_descriptors())
                        expire()
                        return prepare(root, cache, selector, timeout_s, pass_fds=pass_fds)
                    with mock.patch.object(linux_runner, 'prepare_shell', side_effect=shell):
                        _ = self.run_request(self.request)
                self.assertEqual(commands[0], ['systemctl', 'kill', '--kill-whom=all', '--signal=KILL', guest.WORK_SLICE])
                self.assertEqual(commands[1], ['systemctl', 'poweroff', '--no-block'])

    def test_actual_mount_check_rejects_a_directory_on_the_root_filesystem(self) -> None:
        mountinfo = self.root / 'mountinfo'
        _ = mountinfo.write_text('1 0 8:1 / / rw - ext4 /dev/root rw\n')
        self.assertFalse(receiver.data_mount_ready(mountinfo))
        _ = mountinfo.write_text('2 1 8:2 / /var/lib/hana rw - ext4 /dev/data rw\n')
        self.assertTrue(receiver.data_mount_ready(mountinfo))


if __name__ == '__main__':
    _ = unittest.main()
