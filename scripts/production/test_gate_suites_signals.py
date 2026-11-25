"""Test the gate suite runner."""

from __future__ import annotations

import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout, suppress
from dataclasses import asdict, replace
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
import time
from typing import cast, final
from unittest import mock
from scripts.build_queue import admission_records
from scripts.build_queue.queue_place import QueuePlace
from scripts.copies import copies
from scripts.lib import build_folder_lock, build_folders, process_group
from scripts.lib.python_startup.sitecustomize import GATE_PARENT_SOURCE_FD
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.process_group import send_process_group
from scripts.lib.process_start import Gone, Started, Unreadable, Zombie, process_start
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.production import gate, gate_suites_test_support
from scripts.production.gate_modules import GateRefused
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.gate_test_support import gate_suites
from scripts.production.gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class GateProcessStateTests(unittest.TestCase):
    def test_gate_jobs_only_name_descriptors_passed_to_them(self) -> None:
        request = build_folders.UniqueBuildFolder("gate-job-fds-", build_folders.InheritedTemporaryParent())
        with ExitStack() as stack:
            root = stack.enter_context(build_folders.temporary_build_folder(request))
            lease = stack.enter_context(Path(os.devnull).open("rb"))
            second_lease = stack.enter_context(Path(os.devnull).open("rb"))
            omitted = stack.enter_context(Path(os.devnull).open("rb"))
            source = stack.enter_context(Path(gate.__file__).open("rb"))
            leases = (lease.fileno(), second_lease.fileno())
            token = copies.COMMAND_LOCKS.set(leases)
            _ = stack.callback(copies.COMMAND_LOCKS.reset, token)
            _ = stack.enter_context(mock.patch.object(gate, "_gate_program_source", return_value=source))
            _ = stack.enter_context(mock.patch.object(gate, "python_shim", return_value=Path(sys.executable)))
            config = root / "config"
            _ = write_config_fixture(config / "build_admission.conf")
            stack.enter_context(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
            stop_seconds = admission_records.read_memory_config().program_stop_seconds
            environment = {
                "BUILDLOG_CALLER": "gate",
                build_folders.NUMBERED_COPY_LOCK_ENV: str(lease.fileno()),
                "BUILD_FOLDER_LOCK_FD": str(omitted.fileno()),
                "EXAMPLE_LOCK_FD": str(omitted.fileno()),
                "EXAMPLE_LEASE_FDS": f"{second_lease.fileno()},{omitted.fileno()},{lease.fileno()}",
                "LISTEN_FDS": str(lease.fileno()),
                "COPIES_COMMAND_LEASE_FDS": f"{lease.fileno()},{omitted.fileno()}",
                "COPIES_MIRROR_LEASE_FDS": str(omitted.fileno()),
                "COPIES_STEP_SOURCE_LOCK_FD": str(source.fileno()),
                "STAFF_ROW_FD_fixture": str(omitted.fileno()),
                GATE_PARENT_SOURCE_FD: str(omitted.fileno()),
            }

            for kind in (gate.GateJobKind.TEST_MODULE, gate.GateJobKind.TYPE_CHECK):
                with self.subTest(kind=kind), mock.patch.object(subprocess, "Popen") as launch:
                    job = gate.start_gate_job("hooks", root / "test_fixture.py", [], root, environment,
                                              root / "output.log", kind=kind)
                    job.output.close()
                    expected = {
                        "BUILDLOG_CALLER": "gate", "GATE_PARENT_PID": str(os.getpid()),
                        build_folders.NUMBERED_COPY_LOCK_ENV: str(lease.fileno()),
                        "COPIES_COMMAND_LEASE_FDS": str(lease.fileno()),
                        "EXAMPLE_LEASE_FDS": f"{second_lease.fileno()},{lease.fileno()}",
                    }
                    passed = leases
                    if kind is gate.GateJobKind.TEST_MODULE and sys.platform == "linux":
                        passed += (source.fileno(),)
                        expected[GATE_PARENT_SOURCE_FD] = str(source.fileno())
                    launch.assert_called_once()
                    child = cast(dict[str, str], launch.call_args.kwargs["env"])
                    permission = int(child["GATE_START_FD"])
                    self.assertNotIn(permission, (*leases, source.fileno(), omitted.fileno()))
                    self.assertEqual(cast(tuple[int, ...], launch.call_args.kwargs["pass_fds"]), (*passed, permission))
                    self.assertEqual(child, {**expected, "GATE_START_FD": str(permission),
                                             "GATE_START_SECONDS": str(stop_seconds)})

    def test_recovery_keeps_canaries_and_saved_gate_folders(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="gate-recovery-test-")).resolve()
        self.addCleanup(shutil.rmtree, root)
        home = root / "home"
        directory = root / "recovery"
        directory.mkdir()
        recovery = gate.GateRecovery(directory / "dead.json", str(root), 424242, "fixture")
        for location in (home, home / "rust", home / "worktrees", home / ".claude", root / "gate"):
            folder = location / "merge-gate-fixture"
            folder.mkdir(parents=True)
            _ = (folder / "keep").write_text("fixture data")
            recovery.folders.append(str(folder))
        recovery.save()
        with (mock.patch.object(gate, "process_start", return_value=Zombie("dead")),
              mock.patch.object(admission_records, "read_memory_config",
                                return_value=mock.Mock(program_stop_seconds=0)),
              redirect_stderr(io.StringIO()) as error):
            gate.recover_gate_directory(directory, own_directory=directory)
            gate.recover_gate_directory(directory, own_directory=directory)
        self.assertEqual(error.getvalue().splitlines(), [
            f"kept for a manual removal list: {folder} (gate, the gate owner ended)"
            for folder in recovery.folders
        ])
        for folder in recovery.folders:
            self.assertEqual((Path(folder) / "keep").read_text(), "fixture data")
        saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
        self.assertEqual(saved["folders"], recovery.folders)
        self.assertIs(saved["finished"], True)

    def test_finished_recovery_skips_a_marker_without_search_permission(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gate-markers-") as temporary:
            root = Path(temporary)
            blocked = root / "blocked"
            available = root / "available"
            recovery = gate.GateRecovery(root / "finished.json", str(root), os.getpid(), "ended-owner",
                                        folders=[str(blocked), str(available)], finished=True)
            recovery.save()
            for folder in (blocked, available):
                folder.mkdir()
                (folder / build_folders.RECOVERY_MARKER).symlink_to(recovery.path)
            blocked_marker = blocked / build_folders.RECOVERY_MARKER
            is_symlink = Path.is_symlink

            def check_marker(path: Path) -> bool:
                if path == blocked_marker:
                    raise PermissionError(f"fixture denies search in {blocked}")
                return is_symlink(path)

            with (mock.patch.object(Path, "is_symlink", autospec=True, side_effect=check_marker),
                  redirect_stdout(io.StringIO()) as report):
                gate.recover_gate_directory(root, own_directory=root)
            self.assertIn(f"fixture denies search in {blocked}", report.getvalue())
            self.assertEqual(gate.read_gate_recovery(recovery.path), recovery)
            self.assertEqual(blocked_marker.readlink(), recovery.path)
            self.assertFalse((available / build_folders.RECOVERY_MARKER).is_symlink())

    def test_recovery_reports_foreign_refusals_and_recovers_later_records(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="gate-recovery-test-")))
        checkout, other = root / "checkout", root / "other"
        _ = self.enterContext(mock.patch.object(gate, "gate_state_dir", return_value=root / "state"))
        directory = gate.gate_run_directory(other)
        directory.mkdir(parents=True)
        unreadable_owner = gate.GateRecovery(directory / "a-owner.json", str(other), 5000001, "fixture")
        unreadable_group = gate.GateRecovery(directory / "b-group.json", str(other), 5000002, "fixture",
                                             groups={5000003: "fixture"})
        recoverable = gate.GateRecovery(directory / "c-dead.json", str(other), 5000004, "fixture")
        for recovery in (unreadable_owner, unreadable_group, recoverable):
            recovery.save()
        identities = {
            unreadable_owner.pid: Unreadable("owner status\ncannot be read"),
            unreadable_group.pid: Gone(),
            5000003: Unreadable("group status cannot be read"),
            recoverable.pid: Gone(),
            os.getpid(): Started("caller"),
        }
        with (mock.patch.object(gate, "process_start", side_effect=identities.__getitem__) as read_start,
              mock.patch.object(admission_records, "read_memory_config",
                                return_value=mock.Mock(program_stop_seconds=0)),
              redirect_stderr(io.StringIO()) as report):
            gate.recover_gate_runs(checkout)
            with gate.gate_recovery(checkout) as current:
                self.assertTrue(current.path.is_file())
        self.assertEqual(read_start.call_args_list.count(mock.call(unreadable_owner.pid)), 2)
        self.assertEqual(report.getvalue().splitlines(), [
            f"Gate recovery kept {str(unreadable_owner.path)!r}: cannot recover gate 5000001: owner status cannot be read",
            f"Gate recovery kept {str(unreadable_group.path)!r}: cannot recover gate group 5000003: group status cannot be read",
        ])
        self.assertTrue(unreadable_owner.path.is_file())
        self.assertTrue(unreadable_group.path.is_file())
        self.assertFalse(recoverable.path.exists())

    def test_recovery_refuses_the_callers_checkout(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="gate-recovery-test-")))
        checkout = root / "checkout"
        _ = self.enterContext(mock.patch.object(gate, "gate_state_dir", return_value=root / "state"))
        directory = gate.gate_run_directory(checkout)
        directory.mkdir(parents=True)
        recovery = gate.GateRecovery(directory / "unreadable.json", str(checkout), 5000001, "fixture")
        recovery.save()
        with (mock.patch.object(gate, "process_start", return_value=Unreadable("owner status cannot be read")),
              self.assertRaisesRegex(GateRefused, "cannot recover gate 5000001: owner status cannot be read"),
              gate.gate_recovery(checkout)):
            self.fail("the gate started with an unreadable owner in its checkout")
        self.assertEqual(list(directory.iterdir()), [recovery.path])

    def test_process_exit_counts_as_stopped_at_stat_read_or_pidfd_event(self) -> None:
        fixture = GateSuitesFixture()
        for initial_state in (FileNotFoundError(), ProcessLookupError(), "1 (fixture) S"):
            status_read = mock.Mock(side_effect=[initial_state, "1 (fixture) S"])
            status = mock.Mock(read_text=status_read)
            opened = mock.Mock(return_value=47)
            closed = mock.Mock()
            kernel = mock.Mock(pidfd_open=opened, close=closed)
            readiness = mock.Mock(select=mock.Mock(return_value=([47], [], [])))
            with self.subTest(initial_state=initial_state), \
                    mock.patch.object(gate_suites_test_support, "Path", return_value=status), \
                    mock.patch.object(gate_suites_test_support, "os", kernel), \
                    mock.patch.object(gate_suites_test_support, "select", readiness):
                fixture.assert_process_stopped(1234)
                status_read.assert_called_once_with()
                self.assertEqual(opened.call_count, int(isinstance(initial_state, str)))
                if isinstance(initial_state, str):
                    closed.assert_called_once_with(47)
                else:
                    closed.assert_not_called()


@final
class MergeGateSignalsTests(GateSuitesFixture):
    def abandoned_gate(self, recovery: gate.GateRecovery) -> Path:
        folder = self.root / "gate" / "merge-gate-abandoned"
        folder.mkdir(parents=True, exist_ok=True)
        self.write(folder / "module-output", "unfinished")
        recovery.folders.append(str(folder))
        return folder

    def create_jobserver(self, path: Path, tokens: bytes) -> None:
        os.mkfifo(path)
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        self.environment["BUILDLOG_JOBSERVER_FIFO"] = str(path)
        self.supply_jobserver(tokens)

    def test_command_stops_the_member_that_keeps_stderr_open(self) -> None:
        self.configure_gate("program_stop_seconds=15\nprogram_sample_seconds=0.05")
        processes = OwnedTestProcesses()
        self.addCleanup(processes.finish)
        source = """import subprocess, sys
member = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                          stdout=subprocess.DEVNULL, stderr=sys.stderr)
print(member.pid)
print("command stderr", file=sys.stderr)
"""
        with mock.patch.dict(os.environ, processes.environment(self.environment), clear=True), \
                gate.gate_recovery(self.checkout) as recovery:
            config = admission_records.read_memory_config()
            recovery.rollback_deadline = time.monotonic() + config.program_stop_seconds
            result = gate.command([sys.executable, "-c", source], self.checkout)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "command stderr\n")
            self.assertIsInstance(process_start(int(result.stdout)), (Gone, Zombie))
            self.assertEqual(json.loads(recovery.path.read_text())["groups"], {})

    def test_recovery_kills_a_term_ignoring_member_after_its_leader_ends(self) -> None:
        self.supply_jobserver(b"Q")
        source = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c',
    'import os, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(os.getpid(), flush=True); signal.pause()'],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
print(child.stdout.readline(), end='', flush=True)
sys.stdin.read(1)
"""
        leader = subprocess.Popen([sys.executable, "-c", source], env=self.environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        member = 0
        try:
            output = leader.stdout
            assert output is not None
            member = int(cast(str, output.readline()))
            recovery = gate.GateRecovery(self.state / "dead-gate.json", str(self.checkout),
                                               leader.pid, "ended-owner")
            recovery.add_group(leader.pid)
            stdout, stderr = leader.communicate(input="x", timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(leader.returncode, 0, stdout + stderr)
            folder = self.abandoned_gate(recovery)
            _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
            recovery.rollback = self.base
            recovery.rollback_head = self.git(self.checkout, "rev-parse", "HEAD")
            recovery.rollback_source = self.checkpoint
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            try:
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    recovery.path = gate.gate_run_directory(self.checkout) / "dead-gate.json"
                    recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1)))
                    recovery.save()
                    recovery.recover()
            finally:
                os.close(descriptor)
            self.assert_process_stopped(member)
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assertTrue(folder.is_dir())
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assertTrue(recovery.path.is_file())
            first_record = recovery.path
            first_contents = first_record.read_text()
        finally:
            if leader.poll() is None:
                leader.kill()
            _ = leader.communicate(timeout=TEST_HANG_GUARD_SECS)
            if member:
                try:
                    send_process_group(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        for live_holder in (False, True):
            with self.subTest(live_holder=live_holder), mock.patch.dict(os.environ, self.environment, clear=True):
                with ExitStack() as holder:
                    if live_holder:
                        _ = holder.enter_context(gate.gate_recovery(self.checkout))
                    directory = gate.gate_run_directory(self.checkout)
                    directory.mkdir(parents=True, exist_ok=True)
                    recovery = gate.GateRecovery(directory / "dead-owner.json", str(self.checkout),
                                                       leader.pid, "ended-owner")
                    folder = self.abandoned_gate(recovery)
                    _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
                    merged = self.git(self.checkout, "rev-parse", "HEAD")
                    recovery.rollback = self.base
                    recovery.rollback_head = merged
                    recovery.rollback_source = self.checkpoint
                    descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
                    try:
                        recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1)))
                        recovery.save()
                    finally:
                        os.close(descriptor)
                    if live_holder:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            recovery.recover()
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
                        record = cast(dict[str, object], json.loads(recovery.path.read_text()))
                        self.assertEqual(record["rollback"], self.base)
                        self.assertEqual(record["groups"], {})
                        self.assertEqual(record["tokens"], [])
                        self.assertEqual(record["folders"], [str(folder)])
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        self.assertIn("rollback waits for the live gate", output.getvalue())
                    else:
                        with gate.gate_recovery(self.unit):
                            self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertTrue(folder.is_dir())
                with gate.gate_recovery(self.checkout):
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertTrue(recovery.path.is_file())
                self.assertCountEqual(directory.glob("*.json"), [first_record, recovery.path])
                self.assertEqual(first_record.read_text(), first_contents)

    def test_recovery_drops_a_non_fifo_token_without_writing(self) -> None:
        pool = self.root / "token-pool"
        self.write(pool, "original pool")
        recovery = gate.GateRecovery(self.state / "dead-gate.json", str(self.checkout),
                                     0, "ended-owner")
        environment = {**self.environment, "BUILDLOG_JOBSERVER_FIFO": str(pool)}
        with mock.patch.dict(os.environ, environment, clear=True):
            path = self.abandoned_gate(recovery)
            with pool.open("rb") as stream:
                recovery.tokens.append(recovery.token(stream.fileno(), b"Q"))
            recovery.save()
            recovery.recover()
            self.assertEqual(recovery.tokens, [])
            self.assertTrue(recovery.path.is_file())
            self.assertTrue(path.is_dir())
            self.assertEqual(pool.read_text(), "original pool")

    def test_recovery_replays_only_held_tokens_for_the_recorded_fifo(self) -> None:
        self.supply_jobserver(b"QR")
        with mock.patch.dict(os.environ, self.environment, clear=True), gate.shared_cpu_pool() as pool:
            assert isinstance(pool, gate.SharedCPUPool)
            directory = gate.gate_run_directory(self.checkout)
            recovery = gate.GateRecovery(directory / "dead-gate.json", str(self.checkout),
                                               os.getpid(), "ended-owner")
            held = recovery.token(pool.descriptor, os.read(pool.descriptor, 1))
            pending = recovery.token(pool.descriptor, b"R", "returning")
            recovery.tokens.extend([held, pending, replace(held, inode=held.inode ^ 1),
                                    replace(held, device=held.device ^ 1)])
            recovery.save()
            data = cast(dict[str, object], json.loads(recovery.path.read_text()))
            tokens = cast(list[dict[str, object]], data["tokens"])
            del tokens[0]["operation"]
            self.write(recovery.path, json.dumps(data))
            output = io.StringIO()
            with redirect_stdout(output):
                for _ in range(2):
                    gate.recover_gate_directory(directory, own_directory=directory)
                    self.assertEqual(self.jobserver_tokens(), b"RQ")
                    saved = gate.read_gate_recovery(recovery.path)
                    assert saved is not None
                    self.assertEqual(saved.tokens, [pending])
            self.assertIn(f"UNRESOLVED: the FIFO has an interrupted slot operation. Record: {recovery.path}",
                          output.getvalue())

    def test_invalid_records_stay_and_allow_other_recovery(self) -> None:
        self.supply_jobserver(b"Q")
        with mock.patch.dict(os.environ, self.environment, clear=True), gate.shared_cpu_pool() as pool:
            assert isinstance(pool, gate.SharedCPUPool)
            directory = gate.gate_run_directory(self.checkout)
            valid = gate.GateRecovery(directory / "valid.json", str(self.checkout),
                                            os.getpid(), "ended-owner")
            valid.save()
            invalid: dict[Path, str] = {}
            for index, value in enumerate(("zz", "", "5152")):
                path = directory / f"invalid-{index}.json"
                data = cast(dict[str, object], json.loads(valid.path.read_text()))
                token = replace(valid.token(pool.descriptor, b"Q"), value=value)
                data["tokens"] = [asdict(token)]
                invalid[path] = json.dumps(data)
                self.write(path, invalid[path])
            invalid_token = replace(valid.token(pool.descriptor, b"Q"), path="/\ud800")
            for name, update in (
                ("null-folder", {"folders": [None]}),
                ("large-pid", {"pid": 10**399}),
                ("empty-start", {"start": "", "groups": {str(os.getpid()): "fixture-group"}}),
                ("empty-group-start", {"groups": {str(os.getpid()): ""}}),
                ("absolute-unencodable-token", {"tokens": [asdict(invalid_token)]}),
                ("relative-folder", {"folders": ["relative"]}),
                ("relative-repo", {"repo": "relative"}),
                ("relative-scope", {"scope": "relative"}),
            ):
                path = directory / f"{name}.json"
                data = cast(dict[str, object], json.loads(valid.path.read_text()))
                data.update(update)
                invalid[path] = json.dumps(data)
                self.write(path, invalid[path])
            output = io.StringIO()
            with redirect_stdout(output), \
                    mock.patch.object(gate, "process_start", wraps=process_start) as read_start, \
                    mock.patch.object(process_group, "send_process_group") as send:
                gate.recover_gate_directory(directory, own_directory=directory)
            read_start.assert_called_once_with(os.getpid())
            send.assert_not_called()
            self.assertFalse(valid.path.exists())
            self.assertEqual(self.jobserver_tokens(), b"Q")
            for path, original in invalid.items():
                self.assertEqual(path.read_text(), original)
                self.assertIn(f"Record: {path}", output.getvalue())
            self.assertEqual(output.getvalue().count("UNRESOLVED: invalid CPU slot value"), 3)
            self.assertIn("UNRESOLVED: invalid folder path", output.getvalue())
            self.assertIn("UNRESOLVED: invalid pid", output.getvalue())
            self.assertIn("UNRESOLVED: invalid start: expected nonempty text", output.getvalue())
            self.assertIn("UNRESOLVED: invalid process group start: expected nonempty text", output.getvalue())
            self.assertIn("UNRESOLVED: invalid token path: path cannot be encoded", output.getvalue())
            self.assertIn("UNRESOLVED: invalid repo path", output.getvalue())
            self.assertIn("UNRESOLVED: invalid scope path", output.getvalue())

    def test_gate_scope_sidecar_names_the_slice_before_the_child_starts(self) -> None:
        sidecar = self.state / "gate.cgroup"
        sidecar.touch()
        record = self.state / "run.json"
        recovery = gate.GateRecovery(record, str(self.checkout), os.getpid(), "fixture")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            configuration = admission_records.read_memory_config()
            reservation = QueuePlace(self.state / "gate", sidecar, configuration=configuration)
            prefix = gate.publish_gate_scope(recovery, reservation, ["--slice=builds.slice", "--"])
        scope = Path(sidecar.read_text().strip())
        self.assertEqual(str(scope), recovery.scope)
        self.assertEqual(scope.parent, Path(self.environment["BUILDLOG_BUILDS_CGROUP"]))
        self.assertEqual(prefix, [f"--slice={scope.name}", "--"])
        self.assertEqual(json.loads(record.read_text())["scope"], str(scope))
        self.assertEqual(sidecar.with_name(build_folders.RECOVERY_MARKER).readlink(), record)
        self.assertEqual(recovery.folders, [str(sidecar.parent)])
        self.assertTrue(reservation.keep_folder)

    def test_cpu_wait_ends_at_the_gate_deadline_or_when_a_slot_arrives(self) -> None:
        self.configure_gate("program_stop_seconds=0.25\nprogram_sample_seconds=1")
        for has_deadline in (False, True):
            self.supply_jobserver(b"")
            with self.subTest(has_deadline=has_deadline), mock.patch.dict(os.environ, self.environment, clear=True), \
                    gate.shared_cpu_pool() as pool:
                assert isinstance(pool, gate.SharedCPUPool)
                blocking = pool.blocking
                clock = 100.0
                if has_deadline:
                    pool.deadline = clock + 1.5
                ready = select.select
                slices: list[float] = []

                def advance_wait(readers: list[int], writers: list[int], errors: list[int],
                                 timeout: float | None = None) -> tuple[list[int], list[int], list[int]]:
                    nonlocal clock
                    if blocking not in readers:
                        return ready(readers, writers, errors, timeout)
                    assert timeout is not None
                    slices.append(timeout)
                    clock += timeout
                    if len(slices) == 1:
                        return [], [], []
                    self.assertEqual(len(slices), 2)
                    self.supply_jobserver(b"Q")
                    return [blocking], [], []

                with mock.patch.object(time, "monotonic", side_effect=lambda: clock), \
                        mock.patch.object(select, "select", side_effect=advance_wait):
                    if has_deadline:
                        with self.assertRaisesRegex(TimeoutError, "gate time limit while waiting for a CPU slot"):
                            _ = pool.acquire()
                        self.assertEqual(slices, [1.0, 0.5])
                        self.assertEqual(self.jobserver_tokens(), b"Q")
                    else:
                        self.assertEqual(pool.acquire().token, b"Q")
                        self.assertEqual(slices, [1.0, 1.0])
            self.assertEqual(self.jobserver_tokens(), b"Q")

    def test_start_barrier_expires_while_the_parent_holds_the_writer(self) -> None:
        self.configure_gate("program_stop_seconds=0.25")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            stop_seconds = admission_records.read_memory_config().program_stop_seconds
        marker = self.state / "child-started"
        reader, writer = os.pipe()
        try:
            began = time.monotonic()
            result = subprocess.run([sys.executable, "-I", "-S", "-c", gate.GATE_START_PERMISSION
                                     + f"open({str(marker)!r}, 'w').close()\n"],
                                    env={**self.environment, "GATE_START_FD": str(reader),
                                         "GATE_START_SECONDS": str(stop_seconds)},
                                    pass_fds=(reader,), timeout=stop_seconds + 1, check=False)
            self.assertLessEqual(time.monotonic() - began, stop_seconds + 1)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(marker.exists())
        finally:
            os.close(reader)
            os.close(writer)

    def test_recovery_refuses_an_amended_merge_with_the_same_parents(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            _ = self.git(self.checkout, "merge", "--no-ff", "--no-edit", self.checkpoint)
            merged = self.git(self.checkout, "rev-parse", "HEAD")
            record = gate.gate_run_directory(self.checkout) / "amended.json"
            recovery = gate.GateRecovery(record, str(self.checkout), os.getpid(), "ended-owner",
                                        rollback=self.base, rollback_head=merged, rollback_source=self.checkpoint)
            recovery.save()
            _ = self.git(self.checkout, "commit", "--amend", "-m", "Amend the gate merge")
            amended = self.git(self.checkout, "rev-parse", "HEAD")
            self.assertNotEqual(amended, merged)
            self.assertEqual(self.git(self.checkout, "show", "-s", "--format=%P", amended),
                             self.git(self.checkout, "show", "-s", "--format=%P", merged))
            with self.assertRaisesRegex(gate.GateRollbackMismatch, "differs from the recorded merge"):
                gate.recover_gate_directory(record.parent, own_directory=record.parent)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), amended)
            self.assertEqual(gate.read_gate_recovery(record), recovery)
            recovery.rollback_head = self.base
            recovery.save()
            gate.recover_gate_directory(record.parent, own_directory=record.parent)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertFalse(record.exists())

    def test_busy_numbered_copy_does_not_block_other_gate_recovery(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            copy = build_folders.copy_path("fixture", "unit", 1)
            copy.mkdir(parents=True)
            directory = gate.gate_run_directory(copy)
            first = gate.GateRecovery(directory / "first.json", str(copy), os.getpid(), "ended-owner",
                                      rollback=self.base)
            second = gate.GateRecovery(directory / "second.json", str(copy), os.getpid(), "ended-owner")
            first.save()
            second.save()
            with (copy.with_suffix(".lock").open("w") as holder, redirect_stdout(io.StringIO()) as output):
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                gate.recover_shared_gate_runs(copy)
            self.assertIn("numbered copy is busy", output.getvalue())
            self.assertEqual(gate.read_gate_recovery(first.path), first)
            self.assertFalse(second.path.exists())

    def test_held_nested_lease_keeps_the_record_unfinished_and_allows_other_recovery(self) -> None:
        self.supply_jobserver(b"Q")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            directory = gate.gate_run_directory(self.checkout)
            first = gate.GateRecovery(directory / "first.json", str(self.checkout),
                                            os.getpid(), "ended-owner")
            second = gate.GateRecovery(directory / "second.json", str(self.checkout),
                                             os.getpid(), "ended-owner")
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
            parent = self.root / "recorded-work"
            parent.mkdir()
            first.folders.append(str(parent))
            first.tokens.append(first.token(descriptor, os.read(descriptor, 1)))
            first.save()
            second.save()
            marker = parent / build_folders.RECOVERY_MARKER
            marker.symlink_to(first.path)
            nested = build_folders.BuildFolderSpec(kind="tool", parent=parent, purpose="nested", suffix_length=0)
            with build_folders.leased_build_folder(nested) as holder:
                output = io.StringIO()
                with redirect_stdout(output), gate.gate_recovery(self.checkout):
                    self.assertIn("UNRESOLVED", output.getvalue())
                    self.assertTrue(holder.is_dir())
                    saved = gate.read_gate_recovery(first.path)
                    assert saved is not None
                    self.assertFalse(saved.finished)
                    self.assertEqual(saved.folders, [str(parent)])
                    self.assertEqual(marker.readlink(), first.path)
                    self.assertFalse(second.path.exists())
                    self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assertFalse(build_folders.reclaim_build_folder(parent, wait=False))
            with gate.gate_recovery(self.checkout):
                self.assertTrue(parent.is_dir())
                saved = gate.read_gate_recovery(first.path)
                assert saved is not None
                self.assertTrue(saved.finished)
                self.assertEqual(saved.folders, [str(parent)])
                self.assertEqual(self.jobserver_tokens(), b"Q")
                self.assertFalse(marker.is_symlink())
            marker.symlink_to(first.path)
            gate.recover_gate_directory(first.path.parent, own_directory=first.path.parent)
            self.assertTrue(build_folders.reclaim_build_folder(parent, wait=False))
            self.assertFalse(parent.exists())

    def test_recovery_holds_nested_lease_until_finished_record_is_saved(self) -> None:
        folder = self.root / "recorded-work"
        folder.mkdir()
        lock = folder / "nested.lock"
        marker = folder / build_folders.RECOVERY_MARKER
        save = gate.GateRecovery.save
        attempts: list[bool] = []

        def compete_at_save(record: gate.GateRecovery) -> None:
            if record.finished:
                saved = gate.read_gate_recovery(record.path)
                assert saved is not None
                self.assertFalse(saved.finished)
                try:
                    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    attempts.append(False)
                else:
                    attempts.append(True)
            save(record)

        with mock.patch.dict(os.environ, self.environment, clear=True), lock.open("xb") as holder:
            build_folder_lock.mark_lock(holder.fileno())
            directory = gate.gate_run_directory(self.checkout)
            recovery = gate.GateRecovery(directory / "dead.json", str(self.checkout),
                                         os.getpid(), "ended-owner", folders=[str(folder)])
            recovery.save()
            marker.symlink_to(recovery.path)
            with mock.patch.object(gate.GateRecovery, "save", autospec=True, side_effect=compete_at_save):
                gate.recover_gate_directory(directory, own_directory=directory)
            self.assertEqual(attempts, [False])
            saved = gate.read_gate_recovery(recovery.path)
            assert saved is not None
            self.assertTrue(saved.finished)
            self.assertFalse(marker.is_symlink())
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @gate_suites("hooks")
    def test_both_gate_modes_recover_the_private_run_record(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            root = build_folders.configured_gate_root()
            for index, machine in enumerate((gate.SHARED_MACHINE, gate.WholeMachine()), start=1):
                with self.subTest(machine=type(machine).__name__):
                    tool = build_folders.GATE_RECOVERY_TOOL
                    spec = build_folders.BuildFolderSpec(kind="tool", parent=root, purpose=tool,
                                                        suffix=f"-{index}", suffix_length=0)
                    with build_folders.leased_build_folder(spec) as folder:
                        work = self.root / f"abandoned-work-{index}"
                        work.mkdir()
                        self.write(work / "output", "unfinished")
                        recovery = gate.GateRecovery(folder / "run.json", str(self.checkout), os.getpid(),
                                                           "ended-owner", folders=[str(work)])
                        recovery.save()
                    older_folder = Path(tempfile.mkdtemp(prefix=f"{tool}-", dir=root))
                    older = replace(recovery, path=older_folder / "run.json", folders=[])
                    older.save()
                    output = io.StringIO()
                    with redirect_stdout(output):
                        run = gate.run_suites(self.checkout, machine=machine, full=True)
                    self.assertTrue(run.passed, output.getvalue())
                    saved = gate.read_gate_recovery(recovery.path)
                    assert saved is not None
                    self.assertTrue(saved.finished)
                    self.assertEqual(saved.folders, [str(work)])
                    self.assertTrue(work.is_dir())
                    self.assertFalse(older_folder.exists())

    def test_module_timeout_removes_fixture_and_nested_gate_folders(self) -> None:
        parent = self.root / "nested-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        host = socket.gethostname().partition(".")[0]
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_folders.conf",
                                 f"roots.gate.{host}={parent}\n")
        record = self.state / "nested-folders.json"
        module = self.checkout / "scripts" / "hooks" / "test_first.py"
        program = f'''import json
from pathlib import Path
import signal
import sys
from contextlib import contextmanager

with open(Path({str(Path(gate.__file__).parents[2])!r}) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {{"__file__": package_root.name}})
from scripts.lib import build_folders
from scripts.production import gate
from scripts.production.gate_test_support import MergeGateFixture

MergeGateFixture.setUpClass()
allocate = build_folders.temporary_build_folder

@contextmanager
def hold_nested_gate(request, **options):
    with allocate(request, **options) as folder:
        Path({str(record)!r}).write_text(json.dumps([
            str(MergeGateFixture.fixture_root.parent), str(folder)]))
        signal.pause()
        yield folder

build_folders.temporary_build_folder = hold_nested_gate
gate.run_suites(Path.cwd(), full=True)
'''
        helper = self.bin / "nested-gate.py"
        self.write(helper, program)
        shim = self.bin.parent / "scripts/lib/py"
        self.write(module, "import os\n" + f"os.execv({str(shim)!r}, [{str(shim)!r}, {str(helper)!r}])\n")
        result = self.run_gate("--tests-only", "--module-time-limit-s", "10", own_process=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("killed: still running after 10 s, the module time limit", result.stdout)
        paths = cast(list[str], json.loads(record.read_text()))
        self.assertEqual(len(paths), 2)
        outer, nested = (Path(value).resolve() for value in paths)
        self.assertTrue(outer.is_relative_to(parent), str(outer))
        self.assertTrue(nested.is_relative_to(parent), str(nested))
        self.assertFalse(nested.exists())
        self.assertEqual(list(parent.iterdir()), [])

    def test_cancellation_sends_term_to_all_jobs_before_reaping(self) -> None:
        jobs: list[gate.RunningGateJob] = []
        for name in ("first", "second"):
            job = gate.start_gate_job(
                "hooks", Path(f"test_{name}.py"),
                [sys.executable, "-c", "import signal; signal.pause()"],
                self.checkout, self.environment, self.state / f"{name}.log")
            self.addCleanup(job.output.close)
            self.addCleanup(gate.kill_job_group, job)
            jobs.append(job)
        sent: list[int] = []
        reap = gate.reap_gate_job

        def send_record(pid: int, number: int) -> None:
            if number == signal.SIGTERM:
                sent.append(pid)
            send_process_group(pid, number)

        def reap_after_all_terms(job: gate.RunningGateJob) -> None:
            self.assertEqual(sent, [item.process.pid for item in jobs])
            reap(job)

        with mock.patch("scripts.production.gate.process_group.send_process_group", side_effect=send_record), \
                mock.patch.object(gate, "reap_gate_job", side_effect=reap_after_all_terms):
            gate.stop_gate_jobs(jobs)
        for job in jobs:
            self.assertIsNotNone(job.process.returncode)

    def test_gate_removes_its_folder_on_normal_exit(self) -> None:
        parent = self.root / "gate-temporary"
        parent.mkdir()
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with gate.private_gate_recovery(self.checkout, parent) as recovery:
                with gate.gate_temporary(recovery, "merge-gate", parent) as folder:
                    self.assertTrue(folder.is_dir())
                    self.assertEqual(recovery.folders, [str(folder)])
                    self.assertEqual(build_folders.build_folder_state(folder), "held")
                    self.write(folder / "assets.lock" / "output", "ordinary content")
                self.assertFalse(folder.exists())
                saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
                self.assertEqual(saved["folders"], [])
        self.assertEqual(list(parent.iterdir()), [])

    @gate_suites("hooks")
    def test_module_cleanup_retains_a_folder_with_its_own_lease_held(self) -> None:
        self.supply_jobserver(b"Q")
        finish = gate.finish_gate_job
        held: list[Path] = []
        with mock.patch.dict(os.environ, self.environment, clear=True), ExitStack() as holder:
            def finish_with_held_lease(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
                folder = job.log.parent
                # Keep the flock after the gate closes its descriptor.
                descriptor = os.dup(build_folders.build_folder_lock_fd(folder))
                _ = holder.callback(os.close, descriptor)
                held.append(folder)
                self.write(folder / "held-output", "the holder owns this file")
                return finish(job, status)

            with (mock.patch.object(gate, "finish_gate_job", side_effect=finish_with_held_lease),
                  self.assertRaises(gate.GateRecoveryUnresolved)):
                _ = gate.run_suites(self.checkout)
            [folder] = held
            self.assertEqual((folder / "held-output").read_text(), "the holder owns this file")
            self.assertEqual(build_folders.build_folder_state(folder), "held")
            _ = self.assert_unresolved_folder_cleanup([folder.parent, folder])

    @gate_suites("hooks")
    def test_module_cleanup_retains_a_folder_with_a_held_nested_lease(self) -> None:
        self.supply_jobserver(b"Q")
        finish = gate.finish_gate_job
        held: list[Path] = []
        with mock.patch.dict(os.environ, self.environment, clear=True), ExitStack() as holder:
            def finish_with_nested_lease(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
                folder = job.log.parent
                spec = build_folders.BuildFolderSpec(kind="tool", parent=folder, purpose="nested", suffix_length=0)
                nested = holder.enter_context(build_folders.leased_build_folder(spec))
                held.append(nested)
                self.write(nested / "output", "the holder owns this file")
                return finish(job, status)

            with (mock.patch.object(gate, "finish_gate_job", side_effect=finish_with_nested_lease),
                  self.assertRaisesRegex(gate.GateRecoveryUnresolved, "UNRESOLVED: could not remove")):
                _ = gate.run_suites(self.checkout)
            [nested] = held
            folder = nested.parent
            self.assertEqual((nested / "output").read_text(), "the holder owns this file")
            self.assertEqual(build_folders.build_folder_state(nested), "held")
            _ = self.assert_unresolved_folder_cleanup([folder.parent, folder])

    def test_gate_keeps_its_folder_and_record_when_removal_refuses(self) -> None:
        parent = self.root / "kept-gate"
        parent.mkdir()
        with mock.patch.dict(os.environ, self.environment, clear=True):
            for scope in ("shared", "private"):
                with self.subTest(scope=scope):
                    recovery_scope = (gate.gate_recovery(self.checkout) if scope == "shared"
                                      else gate.private_gate_recovery(self.checkout, parent))
                    with (self.assertRaisesRegex(gate.GateRecoveryUnresolved, "UNRESOLVED: could not remove"),
                          recovery_scope as recovery,
                          mock.patch.object(build_folders, "may_remove_build_path", return_value=False) as remove,
                          gate.gate_temporary(recovery, "merge-gate", parent) as folder):
                        self.assertTrue(folder.is_dir())
                    remove.assert_called_once()
                    self.assertTrue(folder.is_dir())
                    saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
                    self.assertEqual(saved["folders"], [str(folder)])
                    self.assertIs(saved["finished"], False)
                    self.assertEqual((folder / build_folders.RECOVERY_MARKER).readlink(), recovery.path)

    def test_child_removal_error_keeps_the_recovery_marker(self) -> None:
        unlink = os.unlink

        def fail_child(name: str, *, dir_fd: int | None = None) -> None:
            if name == "blocked-child":
                # Force the order where recursive removal visits the marker first.
                with suppress(FileNotFoundError):
                    unlink(build_folders.RECOVERY_MARKER, dir_fd=dir_fd)
                raise OSError(errno.EIO, "fixture cannot remove the child", name)
            unlink(name, dir_fd=dir_fd)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            with (self.assertRaisesRegex(gate.GateRecoveryUnresolved, "fixture cannot remove the child"),
                  gate.gate_recovery(self.checkout) as recovery,
                  mock.patch.object(os, "unlink", side_effect=fail_child),
                  gate.gate_temporary(recovery, "merge-gate", self.root) as folder):
                self.write(folder / "blocked-child", "keep")
            self.assertEqual((folder / "blocked-child").read_text(), "keep")
            marker = folder / build_folders.RECOVERY_MARKER
            retained_marker = build_folders.recovery_marker_sidecar(folder)
            self.assertEqual(marker.readlink(), recovery.path)
            self.assertEqual(retained_marker.readlink(), recovery.path)
            saved = gate.read_gate_recovery(recovery.path)
            assert saved is not None
            self.assertFalse(saved.finished)
            marker.unlink()
            self.assertFalse(build_folders.reclaim_build_folder(folder, wait=False))
            with mock.patch.object(gate, "process_start", return_value=Zombie("ended-owner")):
                gate.recover_gate_directory(recovery.path.parent, own_directory=recovery.path.parent)
            self.assertFalse(retained_marker.is_symlink())
            self.assertTrue(build_folders.reclaim_build_folder(folder, wait=False))

    def assert_unresolved_folder_cleanup(self, folders: list[Path]) -> gate.GateRecovery:
        [record_path] = gate.gate_run_directory(self.checkout).glob("*.json")
        recovery = gate.read_gate_recovery(record_path)
        assert recovery is not None
        self.assertFalse(recovery.finished)
        self.assertEqual(recovery.folders, list(map(str, folders)))
        for folder in folders:
            self.assertTrue(folder.is_dir())
            self.assertEqual((folder / build_folders.RECOVERY_MARKER).readlink(), record_path)
        return recovery

    @gate_suites("hooks")
    def test_module_cleanup_checks_a_lock_before_its_folder_exists(self) -> None:
        self.supply_jobserver(b"Q")
        finish = gate.finish_gate_job
        locks: list[Path] = []
        with mock.patch.dict(os.environ, self.environment, clear=True), ExitStack() as holder:
            def lock_before_allocation(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
                lock = job.log.parent / "allocating.lock"
                stream = holder.enter_context(lock.open("xb"))
                build_folder_lock.mark_lock(stream.fileno())
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locks.append(lock)
                return finish(job, status)

            with (mock.patch.object(gate, "finish_gate_job", side_effect=lock_before_allocation),
                  self.assertRaises(gate.GateRecoveryUnresolved)):
                _ = gate.run_suites(self.checkout)
            [lock] = locks
            self.assertTrue(lock.is_file())
            self.assertFalse(lock.with_suffix("").exists())
            self.assertFalse(lock.with_suffix(".lease.json").exists())
            recovery = self.assert_unresolved_folder_cleanup([lock.parent.parent, lock.parent])
            with self.assertRaises(gate.GateRecoveryUnresolved), ExitStack() as leases:
                gate.check_gate_folder_leases(recovery, leases)

    @gate_suites("hooks", "lib")
    def test_top_level_lease_keeps_folder_and_record_after_modules_finish(self) -> None:
        self.supply_jobserver(b"Q")
        finish = gate.finish_gate_job
        folders: list[Path] = []
        with mock.patch.dict(os.environ, self.environment, clear=True), ExitStack() as holder:
            def keep_gate_lease(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
                if not folders:
                    folder = job.log.parent.parent
                    descriptor = os.dup(build_folders.build_folder_lock_fd(folder))
                    _ = holder.callback(os.close, descriptor)
                    folders.append(folder)
                return finish(job, status)

            with (mock.patch.object(gate, "finish_gate_job", side_effect=keep_gate_lease),
                  self.assertRaises(gate.GateRecoveryUnresolved)):
                _ = gate.run_suites(self.checkout, max_running_jobs=1)
            self.assert_all_modules_started()
            [folder] = folders
            self.assertEqual(build_folders.build_folder_state(folder), "held")
            _ = self.assert_unresolved_folder_cleanup([folder])
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assert_gate_released()

    @gate_suites("hooks")
    def test_module_cleanup_keeps_folder_and_record_when_lock_open_fails(self) -> None:
        self.supply_jobserver(b"Q")
        finish = gate.finish_gate_job
        open_lock = build_folder_lock.open_lock
        blocked: list[Path] = []

        def refuse_open(path: Path, directory: int, *, create: bool) -> int:
            if path in blocked:
                raise PermissionError("fixture refuses the module lock")
            return open_lock(path, directory, create=create)

        def refuse_after_finish(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            blocked.append(job.log.parent)
            return finish(job, status)

        with (mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(build_folder_lock, "open_lock", side_effect=refuse_open),
              mock.patch.object(gate, "finish_gate_job", side_effect=refuse_after_finish)):
            with self.assertRaisesRegex(gate.GateRecoveryUnresolved, "fixture refuses the module lock"):
                _ = gate.run_suites(self.checkout)
            [folder] = blocked
            self.assertTrue(folder.with_name(folder.name + ".lock").is_file())
            _ = self.assert_unresolved_folder_cleanup([folder.parent, folder])

    def test_gate_keeps_the_recovery_entry_when_a_lease_file_stays(self) -> None:
        parent = self.root / "gate-with-lease-file"
        parent.mkdir()
        unlink = os.unlink

        with mock.patch.dict(os.environ, self.environment, clear=True):
            for suffix in (".lease.json", ".lock"):
                with (self.subTest(suffix=suffix),
                      gate.private_gate_recovery(self.checkout, parent) as recovery,
                      ExitStack() as cleanup):
                    folder = cleanup.enter_context(gate.gate_temporary(recovery, "merge-gate", parent))
                    blocked = folder.with_name(folder.name + suffix)
                    self.assertTrue(blocked.is_file())

                    def refuse_lease_file(path: str | bytes | Path, *, dir_fd: int | None = None) -> None:
                        if dir_fd is not None and path == blocked.name:
                            raise PermissionError("fixture refuses the lease file")
                        unlink(path, dir_fd=dir_fd)

                    with (self.assertRaisesRegex(gate.GateRecoveryUnresolved, "fixture refuses the lease file"),
                          mock.patch.object(os, "unlink", side_effect=refuse_lease_file)):
                        cleanup.close()
                    self.assertFalse(folder.exists())
                    self.assertTrue(blocked.is_file())
                    self.assertEqual(recovery.folders, [str(folder)])
                saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
                self.assertEqual(saved["folders"], [str(folder)])
                self.assertIs(saved["finished"], False)

    def test_failed_gate_removes_its_folder(self) -> None:
        parent = self.root / "gate-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        host = socket.gethostname().partition(".")[0]
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_folders.conf",
                                 f"roots.gate.{host}={parent}\n")
        self.write(self.state / "red-hooks", "red")

        result = self.run_gate(own_process=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(list(parent.iterdir()), [], result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_stop_after_launch_reaps_child_before_returning_capacity(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        launched = self.state / "launched-job"
        preamble = f"""start = gate.start_gate_job
def stop_after_start(*arguments, **options):
    job = start(*arguments, **options)
    if arguments[0] == "hooks":
        from scripts.direct.wait_for_test_state import wait_until
        wait_until((Path(os.environ["GATE_TEST_STATE"]) / "module-held").exists,
                   Path(os.environ["GATE_TEST_STATE"]), timeout={TEST_HANG_GUARD_SECS})
        Path({str(launched)!r}).write_text(str(job.process.pid))
        os.kill(os.getpid(), signal.SIGTERM)
    return job
gate.start_gate_job = stop_after_start
"""
        result = self.run_gate("--tests-only", own_process=True, preamble=preamble)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stdout + result.stderr)
        self.assertEqual(len(launched.read_text().splitlines()), 1)
        (self.state / "hold-module").unlink()
        recovered = self.run_gate("--tests-only", "--full", own_process=True)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assert_process_stopped(int(launched.read_text()))
        self.assertEqual(self.jobserver_tokens(), b"Q")
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_interrupt_after_module_starts_reaps_before_the_gate_returns(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        launched = self.state / "launched-job"
        preamble = f"""import json
from scripts.direct.wait_for_test_state import wait_until

start = gate.start_gate_job
release = gate.SharedCPUSlot.release
launched = Path({str(launched)!r})

def release_after_stop(slot):
    if not slot.returned and launched.exists():
        assert not gate.live_group(int(launched.read_text()))
    release(slot)

def interrupt_after_start(*arguments, **options):
    job = start(*arguments, **options)
    state = Path(os.environ["GATE_TEST_STATE"])
    wait_until((state / "module-held").exists, state, timeout={TEST_HANG_GUARD_SECS})
    recovery = gate.ACTIVE_GATE_RECOVERY.get()
    assert str(job.process.pid) in json.loads(recovery.path.read_text())["groups"]
    launched.write_text(str(job.process.pid))
    raise KeyboardInterrupt

gate.SharedCPUSlot.release = release_after_stop
gate.start_gate_job = interrupt_after_start
"""
        result = self.run_gate("--tests-only", own_process=True, preamble=preamble)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_process_stopped(int(launched.read_text()))
        self.assertEqual(self.jobserver_tokens(), b"Q")
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_unresolved_module_cleanup_keeps_scope_and_folders(self) -> None:
        self.supply_jobserver(b"Q")
        self.configure_gate("program_stop_seconds=1\nprogram_sample_seconds=0.05")
        self.environment.update(BUILDLOG_SCOPE="1", DETACHED_RUN_SLICE="builds.slice")
        processes = OwnedTestProcesses()
        self.addCleanup(processes.finish)
        environment = processes.environment(self.environment)
        descendant = self.state / "detached-pid"
        runner = self.state / "scope-runner.py"
        self.write(runner, f"""import subprocess
import sys
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
Path({str(descendant)!r}).write_text(str(child.pid))
""")
        finish = gate.finish_gate_job
        send = os.kill
        escaped = 0
        began = 0.0
        signals: list[int] = []
        leaders: list[int] = []
        scopes: list[Path] = []

        def record_scope(job: gate.RunningGateJob, status: int) -> gate.GateJobResult:
            nonlocal escaped, began
            escaped = int(descendant.read_text())
            self.assertEqual(os.getsid(escaped), escaped)
            self.assertFalse(gate.live_group(job.process.pid))
            assert job.scope is not None
            job.scope.mkdir(parents=True)
            self.write(job.scope.parent / "cgroup.procs", "")
            self.write(job.scope / "cgroup.procs", f"{escaped}\n")
            leaders.append(job.process.pid)
            scopes.append(job.scope.parent)
            began = time.monotonic()
            return finish(job, status)

        def retain_fixture_member(pid: int, number: int) -> None:
            if pid == escaped and number in (signal.SIGTERM, signal.SIGKILL):
                signals.append(number)
            else:
                send(pid, number)

        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(gate, "module_runner", return_value=runner), \
                mock.patch.object(gate, "finish_gate_job", side_effect=record_scope), \
                mock.patch.object(os, "kill", side_effect=retain_fixture_member):
            stop_seconds = admission_records.read_memory_config().program_stop_seconds
            with self.assertRaisesRegex(gate.GateRecoveryUnresolved, "scope .* still has live processes"):
                _ = gate.run_suites(self.checkout)
            self.assertLessEqual(time.monotonic() - began, stop_seconds + 1)
        self.assertTrue(signals)
        self.assertEqual(os.getsid(escaped), escaped)
        self.assertEqual(self.jobserver_tokens(), b"Q")
        self.assert_gate_released()
        [path] = (self.state / "runner" / "active-runs").glob("*/*.json")
        record = cast(dict[str, object], json.loads(path.read_text()))
        [scope] = scopes
        self.assertEqual(record["scope"], str(scope))
        self.assertIs(record["finished"], False)
        [leader] = leaders
        self.assertIn(str(leader), cast(dict[str, str], record["groups"]))
        folders = cast(list[str], record["folders"])
        self.assertTrue(folders)
        self.assertTrue(all(Path(folder).is_dir() for folder in folders))

    @gate_suites("hooks", "lib")
    def test_stop_with_pending_modules_returns_owned_slot_and_reservation(self) -> None:
        self.assertTrue(self.fixture_root.is_relative_to(Path("/tmp").resolve()))
        self.supply_jobserver(b"S")
        self.write(self.state / "hold-module", "hold")
        process = subprocess.Popen(
            [*self.gate_command(), "--tests-only", "--repo", str(self.checkout)],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            self.assertTrue((self.state / "module-held").exists())
            self.assertEqual(self.jobserver_tokens(), b"")
            self.assertEqual(len(self.admission.server.reservations), 1)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
            (self.state / "hold-module").unlink()
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertEqual(self.jobserver_tokens(), b"S")
            self.assert_gate_released()
            self.assert_process_stopped(int((self.state / "module-held").read_text()))
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            held = self.state / "module-held"
            if held.exists():
                try:
                    send_process_group(int(held.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_finished_module_leaves_no_running_child(self) -> None:
        self.write(self.state / "leak-child", "leak")
        result = self.run_gate("--tests-only")
        child = int((self.state / "child-pid").read_text())
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_process_stopped(child)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_termination_signals_roll_back_and_clean_module_groups(self) -> None:
        active_runs = self.state / "runner/active-runs"
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                previous_records = set(active_runs.glob("*/*.json"))
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                self.write(self.state / "leak-child", "leak")
                self.write(self.state / "detached-child", "detached")
                # Each signal uses a separate pool.
                self.create_jobserver(self.root / f"jobserver-{sig.name}", b"Q")
                parent = self.root / f"interrupted-{sig.name}"
                parent.mkdir()
                self.environment["TMPDIR"] = str(parent)
                # The supported whole-machine path has no queue-owned handlers.
                preamble = """signal.signal(signal.SIGINT, signal.SIG_DFL)
def whole_gate(repo, module_time_limit_s, **options):
    with gate.shared_cpu_pool() as pool:
        pool.try_acquire()
        return gate.run_suites(repo, module_time_limit_s=module_time_limit_s, machine=gate.WholeMachine())
gate.run_gate_suites = whole_gate
"""
                process = subprocess.Popen(
                    [*self.gate_command(preamble=preamble), "--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
                     "--unit", "fixture", "--phase", "repair",
                     "--production", "quiet"], env=self.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                               self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
                    if not (self.state / "module-held").exists():
                        self.fail("fixture module never became ready")
                    child = int((self.state / "module-held").read_text())
                    leftover = int((self.state / "child-pid").read_text())
                    fields = Path(f"/proc/{leftover}/stat").read_text().rsplit(")", 1)[1].split()
                    self.assertEqual(fields[2:4], [str(leftover), str(leftover)])
                    self.assertEqual(self.jobserver_tokens(), b"")
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, -sig, output + errors)
                    self.assert_process_stopped(child)
                    records = set(active_runs.glob("*/*.json"))
                    self.assertLessEqual(previous_records, records)
                    records = records - previous_records
                    self.assertEqual(len(records), 1)
                    record_path, = records
                    record = cast(dict[str, object], json.loads(record_path.read_text()))
                    self.assertEqual(record["pid"], process.pid)
                    self.assertEqual(record["rollback"], self.base)
                    folders = [Path(folder) for folder in cast(list[str], record["folders"])]
                    self.assertTrue(folders)
                    self.assertTrue(all(folder.is_dir() for folder in folders))
                    (self.state / "hold-module").unlink()
                    (self.state / "leak-child").unlink()
                    expected_head = self.base
                    recovery_message = "merge undone"
                    if sig == signal.SIGTERM:
                        merged = self.git(self.checkout, "rev-parse", "HEAD")
                        self.write(self.checkout / "later.txt", "an unrelated later commit")
                        later = self.commit(self.checkout, "Add an unrelated later commit")
                        refused = self.run_gate("--tests-only", "--full", own_process=True)
                        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                        self.assertIn("differs from the recorded merge", refused.stderr)
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), later)
                        self.assertTrue(record_path.is_file())
                        _ = self.git(self.checkout, "reset", "--hard", merged)
                    recovered = self.run_gate("--tests-only", "--full", own_process=True)
                    self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                    self.assertIn(recovery_message, recovered.stdout)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), expected_head)
                    self.assert_process_stopped(child)
                    self.assert_process_stopped(leftover)
                    self.assertTrue(all(folder.is_dir() for folder in folders))
                    saved = cast(dict[str, object], json.loads(record_path.read_text()))
                    self.assertEqual(saved["folders"], record["folders"])
                    self.assertIs(saved["finished"], True)
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assert_not_pushed()
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    if child:
                        try:
                            send_process_group(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    _ = self.git(self.checkout, "reset", "--keep", self.base)

    def test_signal_during_merge_aborts_pending_merge_and_its_hook(self) -> None:
        ready = self.state / "merge-hook-ready"
        hook = self.checkout / ".git" / "hooks" / "prepare-commit-msg"
        # The pid is published by rename, so the test never reads a created but
        # still empty file.
        self.write(hook, f"#!{sys.executable}\nimport os, signal\nfrom pathlib import Path\n"
                   + f"Path({str(ready) + '.tmp'!r}).write_text(str(os.getpid()))\n"
                   + f"os.replace({str(ready) + '.tmp'!r}, {str(ready)!r})\nsignal.pause()\n")
        hook.chmod(0o755)
        process = subprocess.Popen(
            [*self.gate_command(), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
             "--unit", "fixture", "--phase", "repair",
             "--production", "quiet"],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        hook_pid = 0
        try:
            wait_until(lambda: ready.exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            if not ready.exists():
                self.fail("merge hook did not become ready")
            hook_pid = int(ready.read_text())
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
            hook.unlink()
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("merge undone", recovered.stdout)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())
            self.assert_process_stopped(hook_pid)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assert_not_pushed()
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            if hook_pid:
                try:
                    os.kill(hook_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    _ = unittest.main()
