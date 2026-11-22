"""A cleanup failure keeps the step status; an errexit stop keeps the run log."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.lib.helper_tree_test_support import verify_test_environment
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


VERIFY = Path(__file__).with_name("verify.sh")
SOURCE = VERIFY.read_text(encoding="utf-8")
REMOVE_REFUSED = 4
TEST_FAILED = 101
REFUSED_COMMAND = f"sh -c 'exit {REMOVE_REFUSED}'"


def test_case_body() -> str:
    """The commands of the verify.sh `test)` path."""
    match = re.search(r"(?ms)^    test\)\n(.*?)^        ;;", SOURCE)
    if match is None:
        raise AssertionError("missing the test) path in verify.sh")
    return match.group(1)


def exit_trap() -> str:
    """The trap line that installs verify_exit."""
    match = re.search(r"(?m)^trap 'verify_exit .*' EXIT$", SOURCE)
    if match is None:
        raise AssertionError("missing the verify_exit trap in verify.sh")
    return match.group(0)


def run_bash(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    # A socket on standard input makes Bash read the system bashrc, which changes BASH_COMMAND.
    return subprocess.run(
        ["bash", "-c", script, "cleanup-status", *arguments], stdin=subprocess.DEVNULL,
        env=verify_test_environment(), capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
    )


class TreeWatchCleanupTests(unittest.TestCase):
    """The test path exits with the step status when the tree-watch folder removal fails."""

    def run_test_path(self, step_status: int) -> subprocess.CompletedProcess[str]:
        script = "set -euo pipefail\n" + shell_functions(
            VERIFY, "run_cleanup", "after_tree_change", "stop_tree_watch", "cancel_tree_run", "finish_tree_watch",
        )
        script += f"""
verify_helper() {{ printf 'build folders: remove refused\\n' >&2; return {REMOVE_REFUSED}; }}
FIXTURE_DIR=$1 STEP_STATUS=$2
start_tree_watch() {{ TREE_WATCH_DIR=$FIXTURE_DIR; }}
run_local_test() {{ return "$STEP_STATUS"; }}
TREE_WATCH_PID="" TREE_WATCH_DIR="" BUILD_FOLDERS_SCRIPT=build_folders.py
"""
        script += test_case_body()
        with tempfile.TemporaryDirectory() as directory:
            return run_bash(script, directory, str(step_status))

    def test_passing_step_exits_zero_and_reports_the_cleanup(self) -> None:
        result = self.run_test_path(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"verify.sh: cleanup failed: tree-watch folder removal (status {REMOVE_REFUSED})\n", result.stderr,
        )

    def test_failing_step_exits_with_the_step_status_and_reports_the_cleanup(self) -> None:
        result = self.run_test_path(TEST_FAILED)
        self.assertEqual(result.returncode, TEST_FAILED, result.stderr)
        self.assertIn(
            f"verify.sh: cleanup failed: tree-watch folder removal (status {REMOVE_REFUSED})\n", result.stderr,
        )


class ErrexitStopTests(unittest.TestCase):
    """The EXIT trap keeps the run log and writes no record after an errexit stop."""

    def run_exit_trap(self, command: str, last: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        script = "set -euo pipefail\n" + shell_functions(VERIFY, "verify_exit", "finish_run")
        script += f"""
verify_cleanup() {{ finish_run "$1"; }}
finish_verify_run() {{ :; }}
note_event() {{ :; }}
build_seconds() {{ printf '0'; }}
lint_failure_is_the_tree() {{ [[ "$CMD" == lint ]]; }}
cache_record() {{ printf '%s\\n' "$1" > "$CACHE_DIR/record"; }}
CMD={command} CACHE_DIR=$1 RUN_LOG=$1/run.log RUN_STARTED=$SECONDS VERIFY_STOPPED_AT="" EXIT_STATUS=0
: > "$RUN_LOG"
{exit_trap()}
{last}
"""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        return run_bash(script, str(directory)), directory / "run.log", directory / "record"

    def test_errexit_stop_keeps_the_run_log_and_names_the_command(self) -> None:
        for command in ("test", "lint"):
            with self.subTest(command=command):
                result, log, record = self.run_exit_trap(command, REFUSED_COMMAND)
                self.assertEqual(result.returncode, REMOVE_REFUSED, result.stderr)
                self.assertTrue(log.exists(), result.stderr)
                self.assertFalse(record.exists(), result.stderr)
                self.assertIn(
                    f"verify.sh: stopped by a failed command (status {REMOVE_REFUSED}): {REFUSED_COMMAND};"
                    + f" run log kept: {log}\n",
                    result.stderr,
                )

    def test_exit_command_removes_the_run_log_without_a_stop_line(self) -> None:
        result, log, record = self.run_exit_trap("test", f"exit {TEST_FAILED}")
        self.assertEqual(result.returncode, TEST_FAILED, result.stderr)
        self.assertFalse(log.exists(), result.stderr)
        self.assertFalse(record.exists(), result.stderr)
        self.assertNotIn("stopped by a failed command", result.stderr)

    def test_lint_failure_by_exit_keeps_its_fail_record(self) -> None:
        result, _log, record = self.run_exit_trap("lint", "exit 1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(record.read_text(encoding="utf-8"), "fail\n")


if __name__ == "__main__":
    _ = unittest.main()
