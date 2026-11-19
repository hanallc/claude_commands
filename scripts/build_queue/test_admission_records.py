"""Test process identity reads for admission clients."""
from __future__ import annotations

from contextlib import redirect_stderr
import fcntl
import io
from itertools import permutations
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.build_queue import admission_client as client, admission_records as records, admission_service as service
from scripts.build_queue.admission_test_support import FakeClock
from scripts.lib import process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ProcessStartTimeTests(unittest.TestCase):
    def test_process_stat_with_invalid_name_keeps_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            directory = proc / "123"
            directory.mkdir()
            _ = (directory / "stat").write_bytes(b"123 (verify\xff) S " + b"0 " * 18 + b"4200\n")

            with patch.object(records, "Path", return_value=proc):
                self.assertEqual(records.process_start_time(123), "4200")


class PlacementWaitRecordsTests(unittest.TestCase):
    def test_a_restarted_service_bounds_preparation_at_the_last_record(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,
                {"BUILDLOG_DIR": root, "BUILDLOG_PROC": str(Path(root) / "proc")}):
            clock = FakeClock()
            clock.epoch = 13
            with subprocess.Popen(["bash", "-c", "read -r line"], stdin=subprocess.PIPE) as child:
                try:
                    started = records.process_start_time(child.pid)
                    assert started is not None
                    owner = records.PlacementWaitOwner(child.pid, started)
                    call_id = records.verify_run_call_id(owner.pid, owner.start_time)
                    with patch.object(client, "copy_call_start", return_value=10.0), patch.object(client, "CLOCK", clock):
                        self.assertEqual(client.main(["placement-wait-start", "--call-id", call_id,
                            "--pid", str(owner.pid), "--wait-at", "12"]), 0)
                    self.assertEqual(records.read_placement_waits(call_id), ())
                finally:
                    child.kill()
                    _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)

            clock.advance(2)
            recorder = service.PlacementWaitRecorder(Path(root), service.SystemProc(), clock)
            recorder.sweep_owners()
            self.assertEqual(records.read_placement_waits(call_id), ())
            self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": 2})

    def test_owner_scan_removes_the_private_temp_of_a_dead_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root / "placement-waits/active"
            active.mkdir(parents=True)
            dead = active / ".wait-owner.1234.fixture.tmp"
            live = active / ".wait-owner.1235.fixture.tmp"
            unrelated = active / ".other.1234.fixture.tmp"
            for path in (dead, live, unrelated):
                path.touch()

            def check_pid(pid: int, signal: int) -> None:
                self.assertEqual(signal, 0)
                if pid == 1234:
                    raise ProcessLookupError()
                self.assertEqual(pid, 1235)

            with patch.object(os, "kill", side_effect=check_pid):
                service.PlacementWaitRecorder(root, service.SystemProc(), FakeClock()).sweep_owners()
            self.assertEqual(set(active.iterdir()), {live, unrelated})

    def test_only_preserves_a_guard_on_wait_start_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            step = records.PlacementWaitKey("guarded", "step")
            reasons = records.PlacementWaitReason
            records.record_placement_wait(step, reasons.FOLDER, 10, only=reasons.HOLD)
            records.record_placement_wait(step, reasons.HOLD, 11)
            records.record_placement_wait(step, reasons.FOLDER, 12, only=reasons.HOLD)
            records.record_placement_wait(step, records.NO_PLACEMENT_WAIT, 13, only=reasons.MEMORY)
            records.finish_placement_waits(step.call_id, 20)
            self.assertEqual([(row["reason"], row["started_at"], row["ended_at"])
                              for row in records.read_placement_waits(step.call_id)],
                             [(reasons.HOLD, 11, 12), (reasons.FOLDER, 12, 20)])

    def test_registration_appends_without_syncing_the_owner_file_or_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            pid = os.getpid()
            started = records.process_start_time(pid)
            assert started is not None
            call_id = records.verify_run_call_id(pid, started)
            with patch.object(client, "copy_call_start", return_value=10.0), \
                    patch.object(os, "fsync", side_effect=AssertionError("the recorder called fsync")), \
                    patch.object(os, "fdatasync", side_effect=AssertionError("the recorder called fdatasync")):
                self.assertEqual(client.main(["placement-wait-start", "--call-id", call_id,
                    "--pid", str(pid), "--wait-at", "12"]), 0)
                records.finish_placement_waits(call_id, 15)
            self.assertEqual(records.read_placement_waits(call_id), ())
            self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": 5})

    def test_an_attempt_stays_closed_when_late_opens_arrive_before_its_close_record(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            first = records.PlacementWaitKey("late-open", "step", "first")
            next_attempt = records.PlacementWaitKey(first.call_id, first.episode, "second")
            start = records.PlacementWaitReason.REMOTE_START
            records.record_placement_wait(first, start, 10)
            records.record_placement_wait(first, start, 15)
            records.record_placement_wait(first, start, 16)
            records.record_placement_wait(first, records.NO_PLACEMENT_WAIT, 14)
            records.record_placement_wait(next_attempt, start, 18)
            records.record_placement_wait(next_attempt, records.NO_PLACEMENT_WAIT, 20)

            notes = io.StringIO()
            with redirect_stderr(notes):
                rows = records.read_placement_waits(first.call_id)
            self.assertEqual([(row["started_at"], row["ended_at"]) for row in rows], [(10, 14), (18, 20)])
            self.assertEqual(notes.getvalue().count("ignored placement wait after attempt close"), 1)

    def test_named_waits_and_setup_cover_the_time_before_the_first_command(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            key = records.PlacementWaitKey("command-bound", "step")
            records.register_placement_waits(key.call_id, records.UNTRACKED_WAIT_OWNER, 10, entered_at=11)
            records.record_placement_wait(key, records.PlacementWaitReason.MEMORY, 12)
            records.record_placement_wait(key, records.NO_PLACEMENT_WAIT, 15)
            records.stamp_verify_command(key.call_id, 20, 20)
            records.record_placement_wait(key, records.PlacementWaitReason.SLOT, 25)
            records.stamp_verify_command(key.call_id, 26, 26)
            records.finish_placement_waits(key.call_id, 30)

            totals = records.placement_wait_totals(key.call_id)
            self.assertEqual(totals, {"memory": 3, "local_setup": 7})
            self.assertEqual(sum(totals.values()), 10)
            self.assertEqual(len(records.read_placement_waits(key.call_id)), 2)

    def test_failed_command_sync_still_executes_and_keeps_the_report_whole(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            key = records.PlacementWaitKey("failed-command-sync", "step")
            records.register_placement_waits(key.call_id, records.UNTRACKED_WAIT_OWNER, 10, entered_at=10)
            records.record_placement_wait(key, records.PlacementWaitReason.MEMORY, 12)
            records.record_placement_wait(key, records.NO_PLACEMENT_WAIT, 15)
            def record(call_id: str, _pid: int) -> None:
                records.stamp_verify_command(call_id, 20, 20)

            with tempfile.TemporaryFile(mode="w+") as notes, redirect_stderr(notes), \
                    patch.object(records, "record_started_verify_command", side_effect=record), \
                    patch.object(os, "fsync", side_effect=OSError("fixture sync failed")):
                self.assertEqual(client.main(["verify-command", "--call-id", key.call_id, "--",
                                             sys.executable, "-c", "raise SystemExit(23)"]), 23)
                _ = notes.seek(0)
                self.assertEqual(notes.read().splitlines(), [
                    "buildlog: cannot record command start for failed-command-sync: fixture sync failed"])
            records.finish_placement_waits(key.call_id, 30)

            self.assertEqual(records.placement_wait_totals(key.call_id), {"memory": 3, "local_setup": 7})

    def test_failed_command_write_still_executes_on_the_first_or_a_later_step(self) -> None:
        for first_command_recorded, setup in ((False, 20), (True, 10)):
            with self.subTest(first_command_recorded=first_command_recorded), tempfile.TemporaryDirectory() as root, \
                    patch.dict(os.environ, {"BUILDLOG_DIR": root}):
                call_id = "failed-command-write"
                records.register_placement_waits(call_id, records.UNTRACKED_WAIT_OWNER, 10, entered_at=10)
                if first_command_recorded:
                    records.stamp_verify_command(call_id, 20, 20)
                def record(call_id: str, _pid: int) -> None:
                    with patch.object(records, "_append_wait_event", side_effect=OSError("fixture write failed")):
                        records.stamp_verify_command(call_id, 25, 25)

                with tempfile.TemporaryFile(mode="w+") as notes, redirect_stderr(notes), \
                        patch.object(records, "record_started_verify_command", side_effect=record):
                    self.assertEqual(client.main(["verify-command", "--call-id", call_id, "--",
                                                 sys.executable, "-c", "raise SystemExit(23)"]), 23)
                    _ = notes.seek(0)
                    self.assertEqual(len(notes.read().splitlines()), 1)
                records.finish_placement_waits(call_id, 30)

                self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": setup})

    def test_failed_exec_leaves_no_command_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            call_id = "failed-exec"
            records.register_placement_waits(call_id, records.UNTRACKED_WAIT_OWNER, 10, entered_at=10)
            with redirect_stderr(io.StringIO()), patch.object(records, "record_started_verify_command") as record:
                self.assertEqual(client.main(["verify-command", "--call-id", call_id, "--", str(Path(root) / "cargo")]), 125)
            record.assert_not_called()
            records.finish_placement_waits(call_id, 30)
            self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": 20})

    @unittest.skipUnless(hasattr(time, "CLOCK_BOOTTIME"), "Linux process start ticks are required")
    def test_a_fast_child_uses_its_kernel_start_time_after_exit(self) -> None:
        call_id = "fast-child"
        start = Mock()
        with patch.object(process_start, "process_start", return_value=process_start.Zombie("200")), \
                patch.object(os, "sysconf", return_value=100), patch.object(time, "time", return_value=25.0), \
                patch.object(time, "clock_gettime", return_value=7.0), \
                patch.object(records, "Thread", return_value=SimpleNamespace(start=start)) as thread:
            records.record_started_verify_command(call_id, 1234)
        thread.assert_called_once_with(target=records.stamp_verify_command, args=(call_id, 20.0, 25.0), daemon=True)
        start.assert_called_once_with()

    def test_the_detached_owner_supersedes_the_end_of_the_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            path = Path(root)
            call_id = "detached"
            launcher = records.PlacementWaitOwner(1234, "1000")
            worker = records.PlacementWaitOwner(1235, "1008")
            records.register_placement_waits(call_id, launcher, 10, entered_at=10)
            _ = records.close_orphaned_placement_waits(path, lambda _owner, _seen: records.OwnerLastSeen(12))
            records.finish_placement_waits(call_id, 13)
            records.register_placement_waits(call_id, worker, 14, entered_at=14, continued=True)
            self.assertEqual(records.placement_wait_totals(call_id), {})
            records.stamp_verify_command(call_id, 17, 17)
            _ = records.close_orphaned_placement_waits(path, lambda _owner, _seen: records.OwnerLastSeen(15))
            self.assertEqual(records.placement_wait_totals(call_id), {"local_setup": 7})

    def test_a_torn_close_stays_discarded_after_the_next_append(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            key = records.PlacementWaitKey("fixture", "step")
            records.record_placement_wait(key, records.PlacementWaitReason.ROUTING, 10)
            path, = (Path(root) / "placement-waits").glob("*.jsonl")
            initial = path.read_bytes()
            records.finish_placement_waits(key.call_id, 12)
            _ = path.write_bytes(path.read_bytes()[:-1])
            self.assertEqual(records.read_placement_waits(key.call_id), ())

            records.finish_placement_waits(key.call_id, 15)
            row, = records.read_placement_waits(key.call_id)
            self.assertEqual(row["ended_at"], 15)
            self.assertTrue(path.read_bytes().startswith(initial))

    def test_the_report_counts_a_replayed_interval_id_once(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            key = records.PlacementWaitKey("fixture", "step")
            records.record_placement_wait(key, records.PlacementWaitReason.ROUTING, 10)
            path, = (Path(root) / "placement-waits").glob("*.jsonl")
            repeated_open = path.read_bytes()
            records.record_placement_wait(key, records.PlacementWaitReason.SLOT, 12)
            records.finish_placement_waits(key.call_id, 15)
            routing, slot = records.read_placement_waits(key.call_id)
            with path.open("ab") as stream:
                _ = stream.write(repeated_open)
            self.assertEqual(records.read_placement_waits(key.call_id), (routing, slot))
            self.assertNotEqual(routing["interval_id"], slot["interval_id"])
            self.assertIn(routing["interval_id"].encode(), repeated_open)

    def test_the_report_replays_out_of_order_recorder_rows(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            key = records.PlacementWaitKey("shuffled", "step")
            reasons = records.PlacementWaitReason
            records.record_placement_wait(key, reasons.ROUTING, 10)
            records.record_placement_wait(key, reasons.MEMORY, 12)
            records.finish_placement_waits(key.call_id, 20)
            expected = records.read_placement_waits(key.call_id)
            self.assertEqual([(row["reason"], row["placement_wait_s"]) for row in expected],
                             [(reasons.ROUTING, 2), (reasons.MEMORY, 8)])
            journal, = (Path(root) / "placement-waits").glob("*.jsonl")
            frames = journal.read_bytes().rstrip(b"\n").split(b"\n")
            for order in permutations(frames):
                _ = journal.write_bytes(b"\n".join(order) + b"\n")
                self.assertEqual(records.read_placement_waits(key.call_id), expected)

            journal.unlink()
            records.finish_placement_waits(key.call_id, 20)
            records.record_placement_wait(key, reasons.MEMORY, 12)
            records.record_placement_wait(key, reasons.ROUTING, 10)
            self.assertEqual(records.read_placement_waits(key.call_id), expected)

    def test_a_locked_call_does_not_block_another_call(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            first = records.PlacementWaitKey("first", "step")
            second = records.PlacementWaitKey("second", "step")
            records.record_placement_wait(first, records.PlacementWaitReason.ROUTING, 10)
            directory = Path(root) / "placement-waits"
            journal, = directory.glob("*.json*")
            flock = fcntl.flock

            def lock_without_wait(descriptor: int, operation: int) -> None:
                self.assertEqual(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                flock(descriptor, operation)

            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                flock(directory_fd, fcntl.LOCK_EX)
                with journal.open("rb") as held:
                    flock(held.fileno(), fcntl.LOCK_EX)
                    with patch.object(fcntl, "flock", side_effect=lock_without_wait):
                        records.record_placement_wait(second, records.PlacementWaitReason.SLOT, 20)
                        records.finish_placement_waits(second.call_id, 25)
            finally:
                os.close(directory_fd)

            row, = records.read_placement_waits(second.call_id)
            self.assertEqual((row["started_at"], row["ended_at"], row["placement_wait_s"]), (20, 25, 5))

    def test_the_report_rejects_unknown_wait_reasons_with_one_note(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"BUILDLOG_DIR": root}):
            key = records.PlacementWaitKey("fixture", "step")
            reasons = records.PlacementWaitReason
            records.record_placement_wait(key, reasons.ROUTING, 10)
            records.record_placement_wait(key, reasons.HOLD, 12)
            records.record_placement_wait(key, reasons.SLOT, 14)
            records.finish_placement_waits(key.call_id, 20)
            path, = (Path(root) / "placement-waits").glob("*.json*")
            _ = path.write_text(path.read_text().replace('"routing"', '"unknown"').replace('"hold"', '"unknown"'))
            with redirect_stderr(io.StringIO()) as notes:
                row, = records.read_placement_waits(key.call_id)
            self.assertIs(row["reason"], reasons.SLOT)
            self.assertEqual(notes.getvalue().count("invalid placement wait"), 1)
