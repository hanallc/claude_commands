"""Real verify.sh token memory history behavior."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.wait_for_test_state import wait_until
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenMemoryHistoryTests(VerifyTokenWaitFixture):
    def test_a_memory_waiter_holds_no_token_and_records_its_memory_wait(self) -> None:
        board = self.root / "board"
        self.record_phase(board)
        admission = caller_admission(self, self.root, self.environment)
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        errors = self.root / "memory-wait.err"
        process = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "impl"}, errors)
        lock = board / "locks/cargo.d"
        self.assertFalse(lock.exists(), "a memory waiter holds no cargo token")
        _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
        admission.free(48 * 1024 ** 3)
        self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
        self.assertNotIn("Traceback", errors.read_text())
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
        self.assertFalse(lock.exists())
        starts = [row for row in self.phase_waits(board) if row["event_type"] == "wait_started"]
        ends = [row for row in self.phase_waits(board) if row["event_type"] == "wait_finished"]
        self.assertIn("memory admission", {row["wait_reason"] for row in starts})
        self.assertEqual({row["wait_id"] for row in starts}, {row["wait_id"] for row in ends})

    def test_cancel_during_memory_wait_releases_token_and_waiter(self) -> None:
        board = self.root / "board"
        admission = caller_admission(self, self.root, self.environment)
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        process = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board), "BUILDLOG_SYNC": "1",
            "UNIT_TEAM_ROLE": "impl"}, self.root / "cancel.err")
        os.kill(process.pid, signal.SIGTERM)
        self.assertNotEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)

        wait_until(admission.callers_released, admission.state_dir)
        self.assertFalse((board / "locks/cargo.d").exists())
        self.assertEqual(self.live_group_processes(process.pid), [])
        self.assertFalse((self.root / "cargo-calls").exists())

    def test_connection_wait_holds_no_token_before_the_first_reply(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(unreachable_limit_seconds=12)
        board = self.root / "board"
        hidden = admission.socket_path.with_suffix(".unavailable")
        handle_request = admission.server.handle_request
        interrupted = False

        def interrupt_first_placement(request: wire.Request,
                connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            nonlocal interrupted
            if isinstance(request, wire.PlacementRequest) and not interrupted:
                interrupted = True
                _ = admission.socket_path.rename(hidden)
                assert isinstance(connection, service.Connection)
                connection.socket.shutdown(socket.SHUT_RDWR)
                return []
            return handle_request(request, connection)

        _ = self.enterContext(mock.patch.object(admission.server, "handle_request", side_effect=interrupt_first_placement))
        errors = self.root / "connection.err"
        with errors.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--local", "connection wait fixture", "--no-cache"], cwd=self.root,
                env={**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        try:
            self.wait_for_output(errors, "build queue not reachable; trying for", process)
            with admission.loop_turn():
                self.assertEqual(admission.server.waiters, {})
                self.assertEqual(admission.server.reservations, {})
            self.assertFalse((board / "locks/cargo.d").exists(), "a connection wait holds no cargo token")
            _ = hidden.rename(admission.socket_path)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            self.assertFalse((board / "locks/cargo.d").exists())
        finally:
            if hidden.exists():
                _ = hidden.rename(admission.socket_path)

    def test_unreachable_queue_stops_and_releases_the_token_without_running_cargo(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(unreachable_limit_seconds=0.2)
        board = self.root / "board"
        hidden = admission.socket_path.with_suffix(".unavailable")
        _ = admission.socket_path.rename(hidden)
        errors = self.root / "unreachable.err"
        try:
            with errors.open("wb") as stream:
                process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                    env={**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"},
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            self.started_groups.add(process.pid)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 2, errors.read_text())
            lines = errors.read_text().splitlines()
            self.assertEqual(lines.count("build queue not reachable; trying for 0.2 s"), 1)
            self.assertEqual(lines.count("build queue unreachable for 0.2 s; step stopped; cargo token given back"), 1)
            self.assertFalse((board / "locks/cargo.d").exists())
            self.assertEqual(list((board / "token_waiters").glob(f"*.{process.pid}*")), [])
            self.assertFalse((self.root / "cargo-calls").exists())
            with admission.loop_turn():
                self.assertEqual(admission.server.waiters, {})
                self.assertEqual(admission.server.reservations, {})
        finally:
            _ = hidden.rename(admission.socket_path)

    def test_unreachable_estimate_refresh_releases_the_token_without_running_cargo(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(unreachable_limit_seconds=0.2)
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]}])
        board = self.root / "board"
        hidden = admission.socket_path.with_suffix(".unavailable")
        handle_request = admission.server.handle_request
        generations = 0

        def disconnect_refresh(request: wire.Request,
                               connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                               ) -> list[wire.Reply]:
            nonlocal generations
            if isinstance(request, wire.BuildHistoryRequest):
                generations += 1
                if generations == 2:
                    _ = admission.socket_path.rename(hidden)
                    assert isinstance(connection, service.Connection)
                    connection.socket.shutdown(socket.SHUT_RDWR)
                    return []
            elif isinstance(request, (wire.AdmitRequest, wire.PlacementRequest)):
                admission.server.build_history_generation += 1
            return handle_request(request, connection)

        errors = self.root / "refresh-unreachable.err"
        try:
            with mock.patch.object(admission.server, "handle_request", side_effect=disconnect_refresh), \
                    errors.open("wb") as stream:
                process = subprocess.Popen(["bash", str(VERIFY), "build", "sample"], cwd=self.root,
                    env=environment, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                self.started_groups.add(process.pid)
                self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 2, errors.read_text())
            self.assertEqual(generations, 2)
            lines = errors.read_text().splitlines()
            self.assertEqual(lines.count("build queue not reachable; trying for 0.2 s"), 1)
            self.assertEqual(lines.count("build queue unreachable for 0.2 s; step stopped; cargo token given back"), 1)
            self.assertFalse((board / "locks/cargo.d").exists())
            self.assertEqual(list((board / "token_waiters").glob(f"*.{process.pid}*")), [])
            self.assertFalse((self.root / "cargo-calls").exists())

            wait_until(admission.callers_released, admission.state_dir)
        finally:
            if hidden.exists():
                _ = hidden.rename(admission.socket_path)

    def test_a_token_lost_after_the_grant_refuses_execution_and_the_retry_takes_it_again(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        board = self.root / "board"
        errors = self.root / "lost-token.err"
        attempts = self.root / "attempt-statuses"
        released = self.root / "token-released"
        shell = self.root / "observe-attempts.sh"
        _ = shell.write_text('''set -T
observe_attempt_status() {
    if [[ "${FUNCNAME[1]:-}" == buildlog_exec && ! -e "$TEST_TOKEN_RELEASED" ]]; then
        : > "$TEST_TOKEN_RELEASED"
        bash "$TEST_BOARD" release "$UNIT_RUN_DIR" impl cargo --pid $$ >/dev/null 2>&1
    fi
    if [[ "${FUNCNAME[1]:-}" == run_step_attempt && "$BASH_COMMAND" == 'buildlog_retry_unstarted_step "$@"' ]]; then
        printf '%s\\n' "$status" >> "$TEST_ATTEMPT_STATUSES"
    fi
}
trap observe_attempt_status DEBUG
''')
        with errors.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                env={**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl",
                     "BASH_ENV": str(shell), "TEST_ATTEMPT_STATUSES": str(attempts),
                     "TEST_TOKEN_RELEASED": str(released), "TEST_BOARD": str(BOARD)},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
        self.assertTrue(released.exists())
        self.assertEqual(attempts.read_text().splitlines()[0], "125")
        self.assertIn("lost the cargo token before its step started", errors.read_text())
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
        self.assertFalse((board / "locks/cargo.d").exists())
        wait_until(admission.callers_released, admission.state_dir)

    def test_two_boards_share_memory_without_a_resource_cycle(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.free(20 * 1024 ** 3)
        first_board = self.root / "first-board"
        second_board = self.root / "second-board"
        errors = self.root / "first.err"
        with errors.open("wb") as stream:
            first = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                env={**self.environment, "UNIT_RUN_DIR": str(first_board), "UNIT_TEAM_ROLE": "impl", "TEST_BLOCK": "1"},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(first.pid)
        self.wait_for_running(self.root / "running", first)
        second_errors = self.root / "second.err"
        second = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(second_board),
            "UNIT_TEAM_ROLE": "impl"}, second_errors)
        self.assertEqual((first_board / "locks/cargo.d/holder_pid").read_text(), str(first.pid))
        self.assertFalse((second_board / "locks/cargo.d").exists(), "a memory waiter holds no cargo token")
        _ = (self.root / "release").touch()
        self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
        self.assertEqual(second.wait(timeout=TEST_HANG_GUARD_SECS), 0, second_errors.read_text())
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
        self.assertEqual(admission.caller_reservations(), {})



if __name__ == "__main__":
    _ = unittest.main()
