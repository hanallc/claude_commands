"""Test leased guest commands and root fences with private source and process state."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
from typing import NamedTuple, cast, override
import unittest
from unittest import mock
from urllib.request import Request

from scripts.aws_target import hana_execution as execution, hana_guest as guest, hana_receiver as receiver
from scripts.aws_target import hana_result, hana_runner as runner, hana_source as source
from scripts.aws_target.hana_placement import HanaHostGeneration
from scripts.aws_target.hana_result import HanaAttemptKey, LogicalStepLink
from scripts.aws_target import test_hana_source as source_tests
from scripts.build_queue import placement
from scripts.lib import build_folders, build_slots
from scripts.mac_test import linux_runner
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class DiskReading(NamedTuple):
    total: int
    used: int
    free: int


class PrivateSupervisor:
    def __init__(self) -> None:
        self.starts: list[list[str]] = []
        self.stops: list[Path] = []
        self.mounted_value: bool = True
        self.departure_value: receiver.CommandDeparture = receiver.CommandDeparture(False, False)
        self.outcome_value: runner.HanaRunOutcome = runner.HanaExecutionLost('pending')
        self.source_starts: list[execution.HanaExecutionRequest] = []
        self.source_departure_value: receiver.CommandDeparture = receiver.CommandDeparture(False, False)
        self.source_outcome_value: runner.HanaSourceUpdateOutcome = runner.HanaSourceUpdateRefused('pending')

    def mounted(self) -> bool:
        return self.mounted_value

    def start(self, envelope: execution.HanaCommandEnvelope, directory: Path, arguments: list[str]) -> None:
        _ = envelope, directory
        self.starts.append(arguments)

    def stop(self, directory: Path) -> None:
        self.stops.append(directory)

    def departure(self, directory: Path, slot: int) -> receiver.CommandDeparture:
        _ = directory, slot
        if directory.name.endswith('-source'):
            return self.source_departure_value
        return self.departure_value

    def outcome(self, directory: Path) -> runner.HanaRunOutcome:
        _ = directory
        return self.outcome_value

    def start_source_update(self, request: execution.HanaExecutionRequest, directory: Path, arguments: list[str]) -> None:
        _ = directory, arguments
        self.source_starts.append(request)

    def source_outcome(self, directory: Path) -> runner.HanaSourceUpdateOutcome:
        _ = directory
        return self.source_outcome_value


class HanaRunnerTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.fixture: source_tests.HanaSourceTests
        self.root: Path
        self.binaries: Path
        self.manifest: source.SourceManifest
        self.request: execution.HanaExecutionRequest
        self.paths: guest.GuestPaths
        self.permit: guest.WorkPermit
        self.settings: guest.SafetySettings
        self.descriptor: dict[str, object]
        self.access: execution.HanaExecutionAccess
        self.envelope: execution.HanaCommandEnvelope
        self.supervisor: PrivateSupervisor
        self.compiler_version: Path

    @override
    def setUp(self) -> None:
        self.fixture = source_tests.HanaSourceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        self.compiler_version = self.root / 'compiler-version'
        _ = self.compiler_version.write_text('1.2.3\n')
        _ = self.enterContext(mock.patch.object(runner, 'IMAGE_COMPILER_VERSION', self.compiler_version))
        _ = self.enterContext(mock.patch.object(shutil, 'disk_usage',
            return_value=DiskReading(200 * 1024**3, 100 * 1024**3, 100 * 1024**3)))
        self.binaries = self.root / 'bin'
        self.binaries.mkdir()
        home = self.root / 'home'
        home.mkdir()
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
        key = HanaAttemptKey('episode', 'attempt', placement.assigned_claim_id('episode', 'attempt'),
                             HanaHostGeneration('deployment', 'start', 'boot'))
        self.request = execution.HanaExecutionRequest(key, self.manifest, 1,
            placement.SourceKeyKnown('verify-source'), self.manifest.argv, LogicalStepLink('call', 'episode'))
        self.paths = guest.GuestPaths(self.root / 'root-control', self.root / 'boot-id')
        self.paths.directory.mkdir(mode=0o700)
        _ = self.paths.boot_id.write_text('boot')
        _ = self.paths.generation.write_text('1')
        self.permit = guest.WorkPermit(1100, '1', 'boot')
        guest.atomic_record(self.paths.permit, {'expires_at': 1100, 'generation': '1', 'boot_id': 'boot'})
        self.settings = replace(guest.read_settings(self.fixture.configuration), enabled=True)
        self.descriptor = {'version': 1, 'deployment': 'deployment', 'start_token': 'start', 'expires': 1200}
        self.access = execution.HanaExecutionAccess(
            'https://fixture.s3.us-east-2.amazonaws.com/observation',
            'https://fixture.s3.us-east-2.amazonaws.com/result', 1200)
        self.envelope = execution.HanaCommandEnvelope(self.request, self.permit, self.access)
        self.supervisor = PrivateSupervisor()

    def tool(self, name: str, body: str) -> None:
        path = self.binaries / name
        _ = path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)

    def run_request(self, request: execution.HanaExecutionRequest):
        with self.fixture.guest():
            return runner.materialize_and_run(request, root=self.root / 'guest', fetch=self.fixture.objects.fetch)

    def receive(self, envelope: execution.HanaExecutionEnvelope):
        return receiver.receive_execution(self.settings, self.descriptor, envelope, self.supervisor,
            paths=self.paths, clock=lambda: 1000, uptime=lambda: 10)

    def test_exact_command_keeps_cargo_environment_and_compiler_failure_result(self) -> None:
        outcome = self.run_request(self.request)
        self.assertIsInstance(outcome, runner.HanaCommandFinished)
        assert isinstance(outcome, runner.HanaCommandFinished)
        result = outcome.result
        self.assertEqual((result.status, result.argv, result.key), (13, self.request.argv, self.request.key))
        self.assertIn(b'hana-slot1|1|ci|check', result.stdout)
        self.assertEqual(result.commit, self.manifest.commit)
        self.assertEqual(result.toolchain, self.manifest.toolchain)
        self.assertEqual(result.host, 'aws-hana')
        self.assertEqual(build_folders.build_folder_state(self.root / 'guest-slots/hana-slot1'), 'free')
        self.assertEqual(runner.decode_outcome(runner.encode_outcome(outcome)), outcome)

    def test_command_mismatch_and_exact_slot_overflow_refuse(self) -> None:
        for request in (replace(self.request, argv=('cargo', 'clippy')), replace(self.request, slot=2)):
            with self.subTest(request=request.slot):
                self.assertIsInstance(self.run_request(request), runner.HanaExecutionRefused)
        self.assertFalse((self.root / 'guest-slots/hana-slot2').exists())

    def test_missing_basis_reports_the_same_source_without_acquiring_a_slot(self) -> None:
        missing = replace(self.manifest, basis_commits=('f' * 40,))
        outcome = self.run_request(replace(self.request, manifest=missing))
        assert isinstance(outcome, runner.HanaExecutionRefused)
        self.assertIn('MissingBasis:', outcome.reason)
        self.assertIn('f' * 40, outcome.reason)
        self.assertFalse((self.root / 'guest-slots/hana-slot1').exists())

    def test_output_bound_retains_a_lost_outcome_instead_of_a_compiler_result(self) -> None:
        self.tool('cargo', 'printf \'%0100d\' 1\n')
        with self.fixture.guest():
            outcome = runner.materialize_and_run(self.request, root=self.root / 'guest',
                fetch=self.fixture.objects.fetch, output_max_bytes=32)
        self.assertIsInstance(outcome, runner.HanaExecutionLost)

    def test_silent_command_deadline_kills_its_descendants_and_releases_the_slot(self) -> None:
        pids = self.root / 'hung-pids'
        code = ('import os, pathlib, subprocess, sys, threading; '
            + 'child = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait(60)"]); '
            + f'pathlib.Path({str(pids)!r}).write_text(str(os.getpid()) + " " + str(child.pid)); '
            + 'threading.Event().wait(60)')
        self.tool('cargo', 'exec ' + shlex.join([sys.executable, '-c', code]) + '\n')
        _ = write_config_fixture(self.fixture.configuration.source.with_name('build_targets.conf'),
            '# range: 0.001..\nloopback.command_timeout_s=0.25\n')
        outcome = self.run_request(self.request)
        assert isinstance(outcome, runner.HanaExecutionLost)
        self.assertIn('execution deadline', outcome.reason)
        for value in pids.read_text().split():
            try:
                self.assertIn(linux_runner.process_fields(int(value))[0], ('Z', 'X'))
            except FileNotFoundError:
                pass
        self.assertEqual(build_folders.build_folder_state(self.root / 'guest-slots/hana-slot1'), 'free')

    def test_command_deadline_also_bounds_the_wait_after_output_closes(self) -> None:
        self.tool('cargo', 'exec >/dev/null 2>&1\nexec sleep 60\n')
        _ = write_config_fixture(self.fixture.configuration.source.with_name('build_targets.conf'),
            '# range: 0.001..\nloopback.command_timeout_s=0.1\n')
        outcome = self.run_request(self.request)
        assert isinstance(outcome, runner.HanaExecutionLost)
        self.assertIn('execution deadline', outcome.reason)

    def test_complete_source_update_repairs_the_original_capture_without_running_cargo(self) -> None:
        self.fixture.sender.acknowledge(source.SourceIdentity(self.manifest.repository,
            self.manifest.commit, self.manifest.toolchain))
        _ = (self.fixture.checkout / 'tracked').write_text('captured update\n')
        incremental = self.fixture.prepare()
        original = replace(self.request, manifest=incremental, argv=incremental.argv)
        refused = self.run_request(original)
        assert isinstance(refused, runner.HanaExecutionRefused)
        self.assertIn('MissingBasis:', refused.reason)
        _ = (self.fixture.checkout / 'tracked').write_text('later caller edit\n')
        answer = source.MissingBasis(incremental.repository, incremental.basis_commits)
        complete = self.fixture.sender.retry_complete(self.fixture.checkout, incremental, answer)
        self.assertEqual(complete.commit, incremental.commit)
        self.assertEqual(complete.basis_commits, ())
        with (self.fixture.guest(), mock.patch.object(linux_runner, 'run_slot') as command,
              mock.patch.object(linux_runner, 'prepare_shell') as shell):
            updated = runner.materialize_source_update(replace(original, manifest=complete),
                root=self.root / 'guest', fetch=self.fixture.objects.fetch)
        assert isinstance(updated, runner.HanaSourceUpdated)
        self.assertEqual(updated.identity.commit, original.manifest.commit)
        self.assertEqual((self.root / 'guest-slots/hana-slot1/tracked').read_text(), 'captured update\n')
        self.assertEqual(runner.decode_source_outcome(runner.encode_source_outcome(updated)), updated)
        command.assert_not_called()
        shell.assert_not_called()
        self.assertEqual(build_folders.build_folder_state(self.root / 'guest-slots/hana-slot1'), 'free')

    def test_source_update_entry_point_materializes_source_without_cargo_or_shell_preparation(self) -> None:
        bundle = self.root / 'complete.bundle'
        _ = bundle.write_bytes(self.fixture.objects.objects[self.manifest.bundle_sha256])
        self.tool('curl', 'while [ "$#" -gt 0 ]; do\n'
            + f'if [ "$1" = --output ]; then shift; cp {shlex.quote(str(bundle))} "$1"; exit; fi\n'
            + 'shift\ndone\nexit 1\n')
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
            'request': cast(object, json.loads(execution.encode_request(self.request)))}).encode()
        code = ('from pathlib import Path\nfrom scripts.aws_target import hana_runner\n'
            + 'original = hana_runner.materialize_source_update\n'
            + f'hana_runner.materialize_source_update = lambda request: original(request, root=Path({str(self.root / "guest")!r}))\n'
            + code)
        result = subprocess.run([sys.executable, '-c', code], input=content,
            capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        updated = runner.decode_source_outcome(result.stdout)
        self.assertIsInstance(updated, runner.HanaSourceUpdated)
        self.assertFalse(cargo.exists() or shell.exists())

    def test_shell_failure_and_compiler_mismatch_start_no_cargo(self) -> None:
        cargo = self.root / 'cargo-started'
        self.tool('cargo', f'touch {cargo}\n')
        self.tool('nix', 'exit 9\n')
        self.assertIsInstance(self.run_request(self.request), runner.HanaExecutionRefused)
        self.assertFalse(cargo.exists())
        self.tool('nix', 'printf \'export TEST_SHELL_PROOF=ci\\n\'\n')
        self.tool('rustc', 'printf \'rustc 0.0.1 (wrong)\\n\'\n')
        self.assertIsInstance(self.run_request(self.request), runner.HanaExecutionRefused)
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
                assert isinstance(outcome, runner.HanaExecutionRefused)
                self.assertIn('HanaCompilerVersionUnavailable:', outcome.reason)
                materialize.assert_not_called()

    def test_execution_settings_use_the_image_and_shared_deadlines_and_disk_floor(self) -> None:
        _ = write_config_fixture(self.fixture.configuration.source.with_name('build_targets.conf'),
            'loopback.shell_timeout_s=7\nloopback.command_timeout_s=19\n')
        _ = write_config_fixture(self.fixture.configuration.source, 'free_floor_gib=11\n')
        settings = runner.read_settings(self.fixture.configuration)
        self.assertEqual((settings.compiler_version, settings.shell_timeout_s,
            settings.command_timeout_s, settings.free_floor_gib), ('1.2.3', 7, 19, 11))

    def test_missing_disk_space_refuses_before_cargo(self) -> None:
        cargo = self.root / 'cargo-started'
        self.tool('cargo', f'touch {shlex.quote(str(cargo))}\n')
        with mock.patch.object(shutil, 'disk_usage',
                return_value=DiskReading(200 * 1024**3, 200 * 1024**3, 0)):
            outcome = self.run_request(self.request)
        assert isinstance(outcome, runner.HanaExecutionRefused)
        self.assertIn('required free disk space', outcome.reason)
        self.assertFalse(cargo.exists())

    def test_wrapper_death_keeps_the_slot_held_until_its_child_exits(self) -> None:
        manifest = self.root / 'manifest.json'
        _ = manifest.write_bytes(self.manifest.encode())
        bundle = self.root / 'bundle'
        _ = bundle.write_bytes(self.fixture.objects.objects[self.manifest.bundle_sha256])
        ready_read, ready_write = os.pipe()
        self.addCleanup(os.close, ready_read)
        self.addCleanup(os.close, ready_write)
        code = '''import os, pathlib, shutil, sys
from unittest import mock
from scripts.aws_target import hana_source as source
from scripts.lib import build_slots, conf
from scripts.mac_test import linux_runner
manifest_path, bundle, root = map(pathlib.Path, sys.argv[1:4])
ready = int(sys.argv[4])
limits = source.read_limits(conf.read_or_repository('hana_aws'))
manifest = source.decode_manifest(manifest_path.read_bytes(), limits)
def fetch(url, destination, limits):
    shutil.copyfile(bundle, destination)
with mock.patch('socket.gethostname', return_value='aws-hana'):
    with source.materialize(manifest, limits, root=root, fetch=fetch,
        showrunner='hana', unit='test', work='child', selection=build_slots.ExactSlotRequest(1)) as leased:
        execution = linux_runner.LeasedSlotExecution(leased.slot, (*leased.descriptors, ready))
        child = "import os,sys; os.write(int(sys.argv[1]), b'ready'); sys.stdin.buffer.read(1)"
        linux_runner.capture_leased_command([sys.executable, '-c', child, str(ready)], execution, os.environ, 1024, timeout_s=30)
'''
        wrapper = subprocess.Popen([sys.executable, '-c', code, str(manifest), str(bundle),
            str(self.root / 'guest'), str(ready_write)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(ready_write,))
        try:
            readable, _, _ = select.select([ready_read], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(readable, 'The command child did not signal readiness')
            self.assertEqual(os.read(ready_read, 5), b'ready')
            wrapper.kill()
            _ = wrapper.wait(timeout=TEST_HANG_GUARD_SECS)
            slot = self.root / 'guest-slots/hana-slot1'
            self.assertEqual(build_folders.build_folder_state(slot), 'held')
            with self.fixture.guest(), self.assertRaises(build_slots.SlotsFull), self.fixture.materialize(self.manifest):
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
        self.assertIn(str(receiver.RUNTIME / 'runner.py'), arguments)

    def test_fence_acknowledgement_prevents_a_delayed_command(self) -> None:
        fence = execution.HanaFenceEnvelope(self.request.key, 1, self.access)
        observed = self.receive(fence)
        self.assertTrue(observed.fenced and observed.terminated and observed.lease_free)
        delayed = self.receive(self.envelope)
        self.assertFalse(delayed.accepted)
        self.assertTrue(delayed.fenced)
        self.assertEqual(self.supervisor.starts, [])

    def test_accepted_command_keeps_fence_pending_until_cgroup_and_lease_release(self) -> None:
        _ = self.receive(self.envelope)
        fence = execution.HanaFenceEnvelope(self.request.key, 1, self.access)
        pending = self.receive(fence)
        self.assertTrue(pending.fenced and pending.accepted)
        self.assertFalse(pending.terminated or pending.lease_free)
        self.supervisor.departure_value = receiver.CommandDeparture(True, False)
        self.assertFalse(self.receive(fence).lease_free)
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        self.assertTrue(self.receive(fence).lease_free)
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
        observed = self.receive(replace(self.envelope, permission=replace(self.permit, expires_at=1099)))
        self.assertTrue(observed.terminated and observed.lease_free)
        self.assertEqual(self.supervisor.starts, [])

    def test_source_update_acknowledgement_requires_departure_and_never_repeats_cargo(self) -> None:
        _ = self.receive(self.envelope)
        self.supervisor.outcome_value = runner.HanaExecutionRefused('MissingBasis: fixture')
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        self.supervisor.source_outcome_value = runner.HanaSourceUpdated(source.SourceIdentity(
            self.manifest.repository, self.manifest.commit, self.manifest.toolchain))
        update = execution.HanaSourceUpdateEnvelope(self.request.key, self.request.slot, self.manifest, self.access)
        self.assertFalse(self.receive(update).source_updated)
        self.supervisor.source_departure_value = receiver.CommandDeparture(True, False)
        self.assertFalse(self.receive(update).source_updated)
        self.supervisor.source_departure_value = receiver.CommandDeparture(True, True)
        self.assertTrue(self.receive(update).source_updated)
        self.assertTrue(self.receive(update).source_updated)
        self.assertEqual(len(self.supervisor.source_starts), 1)
        self.assertEqual(len(self.supervisor.starts), 1)

    def test_source_update_rejects_a_changed_capture_and_fencing_stops_its_unit(self) -> None:
        _ = self.receive(self.envelope)
        self.supervisor.outcome_value = runner.HanaExecutionRefused('MissingBasis: fixture')
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        update = execution.HanaSourceUpdateEnvelope(self.request.key, self.request.slot, self.manifest, self.access)
        for manifest in (replace(self.manifest, commit='0' * 40),
                         replace(self.manifest, argv=('cargo', 'clippy')),
                         replace(self.manifest, basis_commits=('f' * 40,))):
            with self.subTest(manifest=manifest.commit), self.assertRaises(ValueError):
                _ = self.receive(replace(update, manifest=manifest))
        self.assertEqual(self.supervisor.source_starts, [])
        _ = self.receive(update)
        fence = execution.HanaFenceEnvelope(self.request.key, self.request.slot, self.access)
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
        assert isinstance(outcome, runner.HanaCommandFinished)
        self.supervisor.outcome_value = outcome
        self.supervisor.departure_value = receiver.CommandDeparture(True, True)
        prefix = 'https://fixture.s3.us-east-2.amazonaws.com/'
        descriptor = {**self.descriptor, 'control_url': prefix + 'control', 'report_url': prefix + 'report',
                      'command_url': prefix + 'command', 'fence_url': prefix + 'fence'}
        published: list[str] = []

        def fetch(request: Request, timeout: int) -> bytes:
            _ = timeout
            url = request.full_url
            if request.get_method() == 'PUT':
                published.append(url)
                return b'' if url.startswith('https:') else b'imds-token'
            if url.endswith('/user-data'):
                return json.dumps(descriptor).encode()
            if url.endswith('/command'):
                return execution.encode_envelope(self.envelope)
            if url.endswith('/fence'):
                return b'{"version":1,"action":"idle"}'
            return b'{}'

        running = iter((True, False))
        with mock.patch.object(receiver, 'GuestCommandSupervisor', return_value=self.supervisor):
            receiver.run_receiver(self.settings, receiver.ReceiverSettings(10, 1), paths=self.paths,
                fetch=fetch, pause=lambda _seconds: None, clock=lambda: 1000,
                keep_running=lambda: next(running))
        self.assertLess(published.index(self.access.result_url), published.index(self.access.observation_url))
        self.assertEqual(len(self.supervisor.starts), 1)

    def test_root_keeps_publication_urls_outside_the_runner_input(self) -> None:
        directory = self.paths.directory / ('a' * 64)
        directory.mkdir(mode=0o700)
        supervisor = receiver.GuestCommandSupervisor(10)
        with mock.patch.object(subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())) as launch:
            supervisor.start(self.envelope, directory,
                ['systemd-run', '--unit=hana-command-1', '--uid=hana-runner', '--', 'runner'])
        arguments = cast(list[str], launch.call_args.args[0])
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
        supervisor = receiver.GuestCommandSupervisor(10)
        poll = mock.Mock(return_value=0)
        child = mock.Mock(poll=poll)
        supervisor.children[directory] = cast(subprocess.Popen[bytes], child)
        inactive = subprocess.CompletedProcess(['systemctl'], 0,
            'LoadState=not-found\nActiveState=inactive\nControlGroup=\nJob=0\n', '')
        with mock.patch.object(subprocess, 'run', return_value=inactive), self.fixture.guest():
            with mock.patch.object(build_folders, 'build_folder_state', return_value='held'):
                self.assertEqual(supervisor.departure(directory, 1), receiver.CommandDeparture(True, False))
            with mock.patch.object(build_folders, 'build_folder_state', return_value='free'):
                self.assertEqual(supervisor.departure(directory, 1), receiver.CommandDeparture(True, True))
                (directory / 'launcher-finished.json').unlink()
                poll.return_value = -9
                self.assertEqual(supervisor.departure(directory, 1), receiver.CommandDeparture(False, False))

    def test_supervisor_reports_a_terminal_deadline_after_the_cgroup_and_slot_release(self) -> None:
        directory = self.paths.directory / ('c' * 64)
        directory.mkdir(mode=0o700)
        supervisor = receiver.GuestCommandSupervisor(10)
        supervisor.children[directory] = cast(subprocess.Popen[bytes], mock.Mock(poll=mock.Mock(return_value=1)))
        expired = subprocess.CompletedProcess(['systemctl'], 0,
            'LoadState=loaded\nActiveState=failed\nControlGroup=\nJob=0\nResult=timeout\n', '')
        with mock.patch.object(subprocess, 'run', return_value=expired), self.fixture.guest():
            with mock.patch.object(build_folders, 'build_folder_state', return_value='held'):
                self.assertFalse(supervisor.departure(directory, 1).lease_free)
            with mock.patch.object(build_folders, 'build_folder_state', return_value='free'):
                self.assertEqual(supervisor.departure(directory, 1), receiver.CommandDeparture(True, True))
        outcome = supervisor.outcome(directory)
        assert isinstance(outcome, runner.HanaExecutionLost)
        self.assertIn('execution deadline', outcome.reason)

    def test_missing_mount_changed_permit_and_boot_refuse_launch(self) -> None:
        self.supervisor.mounted_value = False
        observed = self.receive(self.envelope)
        self.assertTrue(observed.terminated and observed.lease_free)
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
                    def fetch(url: str, destination: Path, limits: source.SourceLimits) -> None:
                        expire()
                        self.fixture.objects.fetch(url, destination, limits)
                    with self.fixture.guest():
                        _ = runner.materialize_and_run(self.request, root=self.root / 'guest', fetch=fetch)
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
