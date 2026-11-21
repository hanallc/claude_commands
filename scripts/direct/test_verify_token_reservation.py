"""Real verify.sh token reservation behavior."""
from __future__ import annotations

from scripts.lib.helper_tree_test_support import VERIFY_HELPERS, copy_helpers

from scripts.build_queue import admission_records

from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.direct.wait_for_test_state import wait_until
from scripts.build_queue import admission_protocol as wire

import subprocess
from typing import cast, override
import unittest
from pathlib import Path
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenReservationTests(VerifyTokenWaitFixture):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    @override
    def setUp(self) -> None:
        super().setUp()
        self.admission = caller_admission(self, self.root, self.environment)

    def test_a_memory_wait_and_local_cargo_take_no_board_token(self) -> None:
        # A step waits for memory without its unit's cargo token, so the unit's
        # other calls keep building. The board wrapper logs each token acquire
        # next to the memory waits; the reply filter checks the lock at each wait.
        home = self.root / "busy-home"
        delegate = home / ".claude/scripts/direct"
        delegate.mkdir(parents=True)
        _ = copy_helpers(home, *VERIFY_HELPERS)
        _ = (delegate / "board.sh").write_text(
            '#!/bin/bash\n'
            + 'if [[ "$1" == acquire ]]; then printf "token\\n" >> "$TEST_SLEEP_LOG"; fi\n'
            + 'exec bash "$TEST_REAL_BOARD" "$@"\n')
        self.write_wait_sleep('printf "sleep %s\\n" "$*" >> "$TEST_SLEEP_LOG"\n'
                             + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"\n'
                             + 'exit 0\n')
        _ = self.admission.occupy_host()
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        log = self.root / "sleep-log"
        lock = self.root / "board/locks/cargo.d"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_SLEEP_LOG": str(log),
                       "BUILDLOG_MEM_POLL_S": "1", "BUILDLOG_MEM_WAIT_LIMIT_S": "2"}
        def free_after_wait(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait) or (isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait)):
                with log.open("a") as stream:
                    _ = stream.write("wait held\n" if lock.exists() else "wait\n")
                self.admission.free(60 * admission_records.GIB)
            return reply

        self.admission.server.reply_filter = free_after_wait
        result = self.verify(environment)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("memory wait limit reached", output)
        self.assertNotIn("starting anyway", output)
        lines = log.read_text().splitlines()
        self.assertNotIn("token", lines)
        self.assertNotIn("wait held", lines, "a memory waiter holds no cargo token")
        self.assertIn("wait", lines)
        self.assertIn("check", (self.root / "cargo-calls").read_text())

    def test_old_two_pass_client_does_not_block_local_placement(self) -> None:
        board = self.root / "board"
        old_shell = self.root / "old-client.sh"
        _ = old_shell.write_text('''set -e
source "$TEST_MEMORY_GATE"
buildlog_wait_for_memory cargo check -p sample
: > "$TEST_OLD_READY"
python3 "$TEST_WAIT_HELPER" "$TEST_OLD_RELEASE"
buildlog_memory_python "$BUILDLOG_MEM_ADMIT_SCRIPT" token-wait "$BUILDLOG_MEM_RESERVATION"
BUILDLOG_MEM_RESERVATION=""
: > "$TEST_OLD_GAP"
# Wait for the new client to take the token after the memory refund.
python3 "$TEST_WAIT_HELPER" "$TEST_RUNNING"
bash "$TEST_BOARD" acquire "$TEST_BOARD_DIR" old cargo --pid $$ --hold 3600 --wait 30
trap 'bash "$TEST_BOARD" release "$TEST_BOARD_DIR" old cargo --pid $$' EXIT
BUILDLOG_MEM_TOKEN_BACK_AT=$(python3 -c 'import time; print(time.time())')
export BUILDLOG_MEM_TOKEN_BACK_AT
BUILDLOG_MEM_WAIT_SINCE=$BUILDLOG_MEM_WAIT_FROM buildlog_wait_for_memory cargo check -p sample
buildlog_release_memory 0
''')
        ready = self.root / "old-ready"
        gap = self.root / "old-gap"
        release = self.root / "old-release"
        old_errors = self.root / "old.err"
        with old_errors.open("wb") as stream:
            old = subprocess.Popen(["bash", str(old_shell)], cwd=self.root, env={**self.environment,
                "TEST_MEMORY_GATE": str(VERIFY.parents[1] / "lint/memory_gate.sh"),
                "TEST_BOARD": str(BOARD), "TEST_BOARD_DIR": str(board),
                "TEST_OLD_READY": str(ready), "TEST_OLD_GAP": str(gap), "TEST_OLD_RELEASE": str(release)},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(old.pid)
        self.wait_for_running(ready, old)
        with self.admission.loop_turn():
            self.assertEqual(len(self.admission.server.reservations), 1)
            self.admission.free(20 * admission_records.GIB)
        new_errors = self.root / "new.err"
        new = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "new", "TEST_BLOCK": "1"}, new_errors)
        _ = release.touch()
        self.wait_for_running(gap, old)
        self.wait_for_running(self.root / "running", new)
        _ = (self.root / "release").touch()
        self.assertEqual(new.wait(timeout=TEST_HANG_GUARD_SECS), 0, new_errors.read_text())
        self.assertEqual(old.wait(timeout=TEST_HANG_GUARD_SECS), 0, old_errors.read_text())
        wait_until(self.admission.callers_released, self.admission.state_dir)
        self.assertEqual(self.admission.server.waiters, {})
        rows = [line for line in (self.admission.state_dir / "queue_times.jsonl").read_text().splitlines()
                if '"token_gap"' in line]
        self.assertEqual(len(rows), 1)



if __name__ == "__main__":
    _ = unittest.main()
