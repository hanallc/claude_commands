"""Real verify.sh token reclaim behavior."""
from __future__ import annotations

import json
import subprocess
from typing import cast
from scripts.direct.verify_token_wait_test_support import BOARD, VERIFY
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture
from scripts.lib.process_supervisor import launch
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenReclaimTests(VerifyTokenOwnershipFixture):
    def test_stop_before_reading_the_sleep_pid_leaves_no_live_sleep(self) -> None:
        gate = self.root / "stop-renewer.sh"
        _ = gate.write_text('''set -T
stop_before_sleep_wait() {
    if [[ "${FUNCNAME[1]:-}" == start_token_renewer &&
          "$BASH_COMMAND" == 'renew_sleep_pid=$!' ]]; then
        trap - DEBUG
        printf '%s\\n' "$BASHPID" > "$TEST_STOPPED_RENEWER"
        kill -TERM "$BASHPID"
    fi
}
trap stop_before_sleep_wait DEBUG
''')
        harness = self.root / "renew-and-stop.sh"
        _ = harness.write_text("set -eu\n"
                               + shell_functions(VERIFY, "collect_child", "renew_token", "start_token_renewer")
                               + '''BOARD_HELPER=$1 BOARD_DIR=$2 BOARD_SLOT=holder
TOKEN_HELD=1 TOKEN_HOLD_S=120 TOKEN_RENEW_S=60
bash "$BOARD_HELPER" acquire "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$ --hold 120
start_token_renewer
wait "$TOKEN_RENEWER_PID"
bash "$BOARD_HELPER" release "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$
''')
        stopped = self.root / "stopped-renewer"
        process = self.enterContext(launch(
            ["bash", str(harness), str(BOARD), str(self.root / "board")],
            cwd=self.root,
            env={**self.environment, "BASH_ENV": str(gate), "TEST_STOPPED_RENEWER": str(stopped)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ))
        try:
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertNotEqual(int(stopped.read_text()), process.pid)
            self.wait_for_group_exit(process.session_id, timeout=5, guardian=process.session_id)
            self.assertEqual(self.live_owned_processes(process), [])
        finally:
            self.end_group(process.pid)
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_killed_verify_holder_is_reclaimed_promptly(self) -> None:
        self.write_wait_sleep('''if [ "$1" = 3 ]; then
    printf '%s\\n' "$1" >> "$TEST_RECLAIM_POLLS"
fi
exit 0
''')
        polls = self.root / "reclaim-polls"
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_POLLS": str(polls)}
        first = self.enterContext(launch(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True))
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists(),
                            "first verify call did not reach cargo check")
            recorded_pid = (board / "locks" / "cargo.d" / "holder_pid").read_text()
            self.assertEqual(recorded_pid, str(first.pid))
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            self.started_groups.add(cast(int, step_record["group"]))

            self.end_group(first.pid)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertFalse(polls.exists(), "reclaimed holder must not require a token polling sleep")
            self.assertIn(f"token cargo reclaimed from first: holder pid {first.pid} is gone",
                          (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
            if first.stdout is not None:
                first.stdout.close()
            if first.stderr is not None:
                first.stderr.close()

    def test_reclaim_stops_old_build_before_next_build(self) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = self.enterContext(launch(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL))
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists())
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)
            self.assertNotEqual(step_group, first.pid)
            first.kill()
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.live_owned_processes(first), [])
            self.assertEqual(self.live_group_processes(step_group), [])
        finally:
            self.end_group(first.pid)

    def test_old_cleanup_keeps_the_new_grant_in_a_reused_wait_folder(self) -> None:
        memory_gate = VERIFY.parent.parent / "lint/memory_gate.sh"
        harness = self.root / "reuse-placement.sh"
        _ = harness.write_text("set -e\n"
                               + shell_functions(VERIFY, "placement_once")
                               + shell_functions(memory_gate, "buildlog_release_memory", "buildlog_wait_for_memory")
                               + '''
verify_helper() { :; }
buildlog_check_nested_step() { :; }
buildlog_memory_python() { :; }

HOLD_WAIT_DIR=$1
DOC_REPO_TOP=$PWD DOC_SOURCE_KEY=fixture-tree
BUILDLOG_FIRST_STEP_REQUEST="$HOLD_WAIT_DIR/request"
mkdir "$HOLD_WAIT_DIR"

place() {
    BUILDLOG_CALL_ID=$1 PLACEMENT_STEP_NUMBER=0 PLACEMENT_EPISODE=""
    placement_once
    printf 'BUILDLOG_MEM_RESERVATION=%s\\n' "$1" > "$PLACEMENT_GRANT_FILE"
}

place old
old_grant=$PLACEMENT_GRANT_FILE old_assignment=$PLACEMENT_ASSIGNMENT_FILE
old_episode=$PLACEMENT_EPISODE
rm -- "$old_grant"
place new

BUILDLOG_MEM_RESERVATION=old BUILDLOG_SELECTED_LOCAL_GRANT=$old_grant \\
    BUILDLOG_PLACEMENT_FILE=$old_assignment BUILDLOG_PLACEMENT_EPISODE=$old_episode \\
    buildlog_release_memory 0

BUILDLOG_SELECTED_LOCAL_GRANT=$PLACEMENT_GRANT_FILE
buildlog_wait_for_memory
[[ "$BUILDLOG_MEM_RESERVATION" == new ]]
[[ "$PLACEMENT_ASSIGNMENT_FILE" != "$old_assignment" ]]
[[ ! -e "$PLACEMENT_GRANT_FILE.outcome" ]]
''')
        result = subprocess.run(
            ["bash", str(harness), str(self.root / "wait")], cwd=self.root,
            env=self.environment, capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
