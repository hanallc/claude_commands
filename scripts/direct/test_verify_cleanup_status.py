"""A cleanup failure keeps the step status; an unexpected stop keeps its run log."""

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


class FolderRemovalTests(unittest.TestCase):
    """A failed tree-watch folder removal prints one line and changes no exit path."""

    def run_script(self, names: tuple[str, ...], body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        script = "set -euo pipefail\n" + shell_functions(VERIFY, "run_cleanup", *names)
        script += f"""
verify_helper() {{
    if [[ "$2" == tool ]]; then printf '%s\\n' "$FIXTURE_DIR"; return 0; fi
    printf 'build folders: remove refused\\n' >&2
    return {REMOVE_REFUSED}
}}
note_event() {{ :; }}
FIXTURE_DIR=$1 BUILD_FOLDERS_SCRIPT=build_folders.py TREE_WATCH_DIR=$1 TREE_WATCH_PID=""
MAC_RUNNER_PID="" MAC_RUNNER_LAUNCHING=0 MAC_RUNNER_PRIOR_JOB="" CMD=test RUN_STARTED=$SECONDS
"""
        script += body
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        return run_bash(script, str(directory)), directory

    def assert_cleanup_line(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertIn(
            f"verify.sh: cleanup failed: tree-watch folder removal (status {REMOVE_REFUSED})\n", result.stderr,
        )

    def test_cancelled_run_exits_77_and_reports_the_cleanup(self) -> None:
        result, _directory = self.run_script(
            ("after_tree_change", "stop_tree_watch", "find_mac_runner_pid", "cancel_tree_run"),
            """
collect_child() { :; }
TREE_CHANGED_STATUS=77
printf 'changed\\n' > "$FIXTURE_DIR/fired"
cancel_tree_run
exit 0
""",
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assert_cleanup_line(result)

    def test_forwarded_signal_collects_the_runner_and_keeps_its_status(self) -> None:
        result, directory = self.run_script(
            ("stop_tree_watch", "find_mac_runner_pid", "forward_mac_runner_signal"),
            """
collect_child() { printf '%s %s\\n' "$1" "$2" > "$FIXTURE_DIR/collected"; }
MAC_RUNNER_PID=4242 MAC_RESULT_FILE="" MAC_LOG_FILE=""
forward_mac_runner_signal TERM 143
""",
        )
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assert_cleanup_line(result)
        self.assertEqual((directory / "collected").read_text(encoding="utf-8"), "4242 TERM\n", result.stderr)

    def test_empty_watch_start_returns_without_a_folder(self) -> None:
        result, _directory = self.run_script(
            ("start_tree_watch",),
            """
set_verdict_keys() { :; }
start_tree_watch
printf 'folder=[%s]\\n' "$TREE_WATCH_DIR"
""",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_cleanup_line(result)
        self.assertIn("folder=[]\n", result.stdout)


class ExitTrapTests(unittest.TestCase):
    """A step failure keeps its old result; an unexpected stop keeps its run log as stopped.log."""

    def run_exit_trap(self, command: str, last: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        script = "set -euo pipefail\n" + shell_functions(VERIFY, "run", "report_stop", "verify_exit", "finish_run")
        script += f"""
run_step() {{ return "$STEP_STATUS"; }}
finish_step_token() {{ return "$2"; }}
verify_cleanup() {{ finish_run "$1"; }}
finish_verify_run() {{ :; }}
note_event() {{ :; }}
build_seconds() {{ printf '0'; }}
lint_failure_is_the_tree() {{ [[ "$CMD" == lint ]]; }}
cache_record() {{ printf '%s\\n' "$1" > "$CACHE_DIR/record"; }}
CMD={command} CACHE_DIR=$1 RUN_LOG=$1/run.log RUN_STARTED=$SECONDS LAST_STEP_STATUS="" EXIT_STATUS=0
STEP_STATUS={TEST_FAILED}
printf 'older stop\\n' > "$CACHE_DIR/stopped.log"
printf 'this run\\n' > "$RUN_LOG"
{exit_trap()}
{last}
"""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        return run_bash(script, str(directory)), directory

    def test_failed_step_prints_no_stop_line(self) -> None:
        for command in ("check", "lint"):
            with self.subTest(command=command):
                result, directory = self.run_exit_trap(command, "step() { run cargo clippy; }\nstep")
                self.assertEqual(result.returncode, TEST_FAILED, result.stderr)
                self.assertNotIn("stopped by a failed command", result.stderr)
                self.assertEqual((directory / "stopped.log").read_text(encoding="utf-8"), "older stop\n")

    def test_failed_lint_step_keeps_its_fail_record(self) -> None:
        result, directory = self.run_exit_trap("lint", "run cargo clippy")
        self.assertEqual(result.returncode, TEST_FAILED, result.stderr)
        self.assertEqual((directory / "record").read_text(encoding="utf-8"), "fail\n", result.stderr)

    def test_failed_check_step_removes_its_run_log(self) -> None:
        result, directory = self.run_exit_trap("check", "run cargo check")
        self.assertEqual(result.returncode, TEST_FAILED, result.stderr)
        self.assertFalse((directory / "run.log").exists(), result.stderr)
        self.assertFalse((directory / "record").exists(), result.stderr)

    def test_failed_command_keeps_the_run_log_as_stopped_log(self) -> None:
        for command in ("test", "lint"):
            with self.subTest(command=command):
                result, directory = self.run_exit_trap(command, REFUSED_COMMAND)
                stopped = directory / "stopped.log"
                self.assertEqual(result.returncode, REMOVE_REFUSED, result.stderr)
                self.assertEqual(
                    result.stderr,
                    f"verify.sh: stopped by a failed command (status {REMOVE_REFUSED}): {REFUSED_COMMAND};"
                    + f" run log kept: {stopped}\n",
                )
                self.assertEqual(stopped.read_text(encoding="utf-8"), "this run\n")
                self.assertFalse((directory / "run.log").exists(), result.stderr)
                self.assertFalse((directory / "record").exists(), result.stderr)

    def test_failed_return_names_the_function_not_the_return(self) -> None:
        last = f"helper() {{ local status={REMOVE_REFUSED}; return \"$status\"; }}\nouter() {{ helper; }}\nouter"
        result, directory = self.run_exit_trap("lint", last)
        self.assertEqual(result.returncode, REMOVE_REFUSED, result.stderr)
        self.assertEqual(
            result.stderr,
            f"verify.sh: stopped by a failed command (status {REMOVE_REFUSED}) in outer;"
            + f" run log kept: {directory / 'stopped.log'}\n",
        )
        self.assertFalse((directory / "record").exists(), result.stderr)

    def test_exit_command_removes_the_run_log_without_a_stop_line(self) -> None:
        result, directory = self.run_exit_trap("test", f"exit {TEST_FAILED}")
        self.assertEqual(result.returncode, TEST_FAILED, result.stderr)
        self.assertFalse((directory / "run.log").exists(), result.stderr)
        self.assertFalse((directory / "record").exists(), result.stderr)
        self.assertNotIn("stopped by a failed command", result.stderr)

    def test_lint_failure_by_exit_keeps_its_fail_record(self) -> None:
        result, directory = self.run_exit_trap("lint", "exit 1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((directory / "record").read_text(encoding="utf-8"), "fail\n")


if __name__ == "__main__":
    _ = unittest.main()
