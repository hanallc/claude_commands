"""Real verify.sh token handoff behavior."""
from __future__ import annotations

from scripts.lib.process_supervisor import ManagedProcess, launch

import subprocess
import unittest
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import fixture_runtime_id


class VerifyTokenHandoffTests(VerifyTokenWaitFixture):
    def test_a_hold_waiter_holds_no_token_and_takes_it_after_the_hold(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        environment = self.hold_environment(board, 0)
        output = self.root / "held.out"
        held = self.start_hold_waiter(environment, output)
        peer: ManagedProcess[bytes] | None = None
        try:
            self.assertFalse(lock.exists(), "the hold wait kept the token")
            peer_holds = self.root / "peer-holds"
            peer_holds.mkdir()
            peer = self.enterContext(launch(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                    cwd=self.root,
                                    env={**environment, "BUILD_HOLD_DIR": str(peer_holds), "TEST_BLOCK": "1",
                                         "CLAUDE_CODE_SESSION_ID": fixture_runtime_id("fixture-hold-owner"),
                                         "CODEX_THREAD_ID": fixture_runtime_id("fixture-hold-owner")},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            self.wait_for_running(self.root / "running", peer)
            self.assertEqual((lock / "holder_pid").read_text(), str(peer.pid))

            (self.root / "hold-release").touch()
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            _ = (self.root / "release").touch()
            self.assertEqual(peer.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(held.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertFalse(lock.exists())
        finally:
            for marker in ("hold-release", "release"):
                (self.root / marker).touch()
            for process in (held, peer):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_a_hold_wait_that_leaves_for_the_mac_never_takes_the_token(self) -> None:
        board = self.root / "board"
        environment = self.hold_environment(board, 75)
        output = self.root / "held.out"
        held = self.start_hold_waiter(environment, output)
        try:
            (self.root / "hold-release").touch()
            _ = held.wait(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if held.poll() is None:
                self.end_group(held.pid)
                _ = held.wait(timeout=TEST_HANG_GUARD_SECS)
        log = (board / "board.log").read_text() if (board / "board.log").exists() else ""
        self.assertNotIn("token cargo acquired", log, "a Mac step took the natedev token")
        self.assertFalse((board / "locks" / "cargo.d").exists())
        self.assertFalse((self.root / "cargo-calls").exists(), "the step ran on natedev")

    def test_no_hold_gives_nothing_back(self) -> None:
        board = self.root / "board"
        (self.root / "build-hold").mkdir()
        result = self.verify({**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "impl"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (board / "board.log").read_text()
        # The memory wait keeps the initial token.
        self.assertEqual((log.count("token cargo acquired"), log.count("token cargo released")), (1, 1), log)



if __name__ == "__main__":
    _ = unittest.main()
