"""Real verify.sh token history behavior."""
from __future__ import annotations

import subprocess
import unittest
from typing import cast
from pathlib import Path
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.recorder_fixture import record_wait_workers
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_pressure_reading
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id


class VerifyTokenHistoryTests(VerifyTokenWaitFixture):
    def test_waiting_for_peer_call_records_token_seconds(self) -> None:
        board = self.root / "board"
        self.record_phase(board)
        admission = caller_admission(self, self.root, self.environment)
        runner = staff.caller_identity()
        if runner:
            record_wait_workers(self.root, board, self.environment, {"fixture-second": "thread-second"},
                                runtime_ids={"fixture-second": runner})
        waiting = self.root / "waiting-for-token"
        resume = self.root / "resume-acquisition"
        first_clock = self.root / "first-clock"
        second_clock = self.root / "second-clock"
        _ = first_clock.write_text("1000\n")
        _ = second_clock.write_text("1000\n")
        self.write_wait_sleep('''if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then
    : > "$TEST_WAITING"
    exec python3 "$TEST_WAIT_HELPER" "$TEST_WAIT_GATE"
fi
exit 0
''')
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "CODEX_THREAD_ID": runner or fixture_runtime_id("thread-first"),
                             "TEST_BLOCK": "1",
                             "UNIT_TOKEN_CLOCK_FILE": str(first_clock)}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "CODEX_THREAD_ID": runner or fixture_runtime_id("thread-second"),
                              "TEST_WAITING": str(waiting),
                              "TEST_WAIT_GATE": str(resume),
                              "UNIT_TOKEN_CLOCK_FILE": str(second_clock)}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running(self.root / "running", first)
            admission.hold_starts()
            prior_waits = len(self.phase_waits(board))
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env=second_environment, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.started_groups.add(second.pid)
            try:
                def memory_wait_recorded() -> bool:
                    return any(row["event_type"] == "wait_started" and row["wait_reason"] == "memory admission"
                               for row in self.phase_waits(board)[prior_waits:])

                wait_until(lambda: memory_wait_recorded() or second.poll() is not None,
                           self.root / "history/runs", processes=(second,), watch_writes=True)
                self.assertTrue(memory_wait_recorded(), "second verify never recorded its memory wait")
                write_pressure_reading(admission.readings.pressure_path)
                admission.server.wake_loop()
                self.wait_for_running(waiting, second)
                self.assertIsNone(second.poll(), "second verify exited before the token was released")
                self.assertEqual(len(self.records()), 0)
                _ = second_clock.write_text("1020\n")
                _ = (self.root / "release").touch()
                first_output = first.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(first.returncode, 0, first_output)
                resume.touch()
                second_output = second.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(first.returncode, 0, first_output)
                self.assertEqual(second.returncode, 0, second_output)
            finally:
                if second.poll() is None:
                    self.end_group(second.pid)
                    _ = second.communicate()
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
                _ = first.communicate()
        records = self.records()
        self.assertEqual(len(records), 2)
        first_record = next((record for record in records
                             if f"-{first.pid}-" in cast(str, record["id"])), None)
        second_record = next((record for record in records
                              if f"-{second.pid}-" in cast(str, record["id"])), None)
        if first_record is None or second_record is None:
            self.fail("both verify calls must have buildlog records")
        first_wait = cast(int, first_record["token_wait_s"])
        second_wait = cast(int, second_record["token_wait_s"])
        self.assertEqual(first_wait, 0)
        self.assertEqual(second_wait, 20)
        self.assertGreater(second_wait, first_wait)
        starts = [row for row in self.phase_waits(board) if row["event_type"] == "wait_started"]
        finished = [row for row in self.phase_waits(board) if row["event_type"] == "wait_finished"]
        # The second call waits for memory admission, then for the token.
        self.assertEqual([(row["wait_kind"], row["wait_reason"], row["phase_id"]) for row in starts],
                         [("WaitingForMemoryOrSlot", "memory admission", "6"),
                          ("WaitingForMemoryOrSlot", "cargo token", "6")], second_output)
        self.assertEqual([row["wait_id"] for row in starts], [row["wait_id"] for row in finished])

    def test_unblocked_verification_records_no_resource_wait(self) -> None:
        session = Path(self.environment["UNIT_SESSION_DIR"])
        self.record_phase(session)
        runner = staff.caller_identity()
        if runner:
            record_wait_workers(self.root, session, self.environment, {"fixture-impl": "thread-impl"},
                                runtime_ids={"fixture-impl": runner})
            self.environment["CODEX_THREAD_ID"] = runner
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.phase_waits(session), [])



if __name__ == "__main__":
    _ = unittest.main()
