"""Step ownership excludes detached work and preserves shell exit codes."""

from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, override
import unittest

from scripts.direct.test_verify_mac_offload import stub_build_tool_path
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.process_supervisor import ManagedProcess, launch
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_function


VERIFY = Path(__file__).with_name("verify.sh")
INVOKE = VERIFY.parent.parent / "lint" / "invoke.sh"
BOARD = VERIFY.with_name("board.sh")
WAIT = VERIFY.with_name("wait_for_test_state.py")


class StepOwnership(TypedDict):
    start_format: str
    group: int
    start: str


class StepProcess(TypedDict):
    pid: int
    group: int


class TokenWaitNoteTests(unittest.TestCase):
    def test_holder_without_trailing_newline_is_named_in_wait_note(self) -> None:
        with tempfile.TemporaryDirectory(prefix="verify-wait-note-") as directory:
            board = Path(directory)
            lock = board / "locks/cargo.d"
            lock.mkdir(parents=True)
            _ = (lock / "holder").write_bytes(b"w74")
            script = "\n".join((
                "set -euo pipefail",
                'BOARD_DIR="$1"',
                "TOKEN_WAITERS_AHEAD=0",
                shell_function(VERIFY.read_text(), "token_wait_note"),
                "token_wait_note still 5",
            ))
            result = subprocess.run(["bash", "-c", script, "wait-note-test", str(board)],
                                    text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("w74 (pid unknown) holds it", result.stderr)
            self.assertNotIn("it came free just now", result.stderr)


class VerifyStepOwnerTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}
    shell: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="verify-owner-test-")))
        for name in ("home", "tmp", "runtime", "state", "buildlog", "stubs"):
            (self.root / name).mkdir()
        self.environment = {
            "PATH": stub_build_tool_path(self.root / "stubs"),
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_PEAK": "",
            "BUILD_FOLDERS_SCRIPT": str(VERIFY.parent.parent / "lib" / "build_folders.py"),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "BOARD_DIR": str(self.root / "board"),
            "BOARD_HELPER": str(BOARD),
            "TOKEN_HELD": "1", "BOARD_SLOT": "fixture",
            "TOKEN_HOLD_S": "30", "TOKEN_RENEW_S": "60",
            "HOLD_WAIT_DIR": str(self.root / "tmp"),
            "TEST_ROOT": str(self.root),
            "TEST_PYTHON": sys.executable,
            "PY": str(VERIFY.parent.parent / "lib/py"),
            "TEST_WAIT": str(WAIT),
            "TEST_COMMAND": str(self.root / "step.py"),
        }
        verify = VERIFY.read_text()
        start = verify.index("STEP_OWNER_PY='\n")
        end = verify.index("\nrelease_token() {", start)
        invoke = INVOKE.read_text()
        self.shell = "\n".join((
            "set -euo pipefail",
            shell_function(invoke, "buildlog_exec"),
            shell_function(verify, "collect_child"),
            verify[start:end],
            shell_function(invoke, "sweep_after_step"),
            'mkdir -p "$BOARD_DIR/locks/cargo.d"',
            'printf "%s" "$$" > "$BOARD_DIR/locks/cargo.d/holder_pid"',
            'printf fixture > "$BOARD_DIR/locks/cargo.d/holder"',
        ))
        _ = (self.root / "step.py").write_text('''import json
import os
from pathlib import Path
import sys
with open(sys.argv[1] + "/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.wait_for_test_state import wait_for_file

root = Path(os.environ["TEST_ROOT"])
name = sys.argv[2]
ready = root / (name + ".pid")
temporary = ready.with_suffix(".tmp")
temporary.write_text(json.dumps({"pid": os.getpid(), "group": os.getpgrp()}))
temporary.replace(ready)
wait_for_file(root / (name + ".release"))
(root / (name + ".finished")).touch()
''')
        self.environment["TEST_REPO"] = str(VERIFY.parent.parent.parent)

    def start_wrapper(self, *command: str) -> ManagedProcess[str]:
        arguments = ["bash", "-c", self.shell + '\nbuildlog_exec "$@"', "step-owner-test", *command]
        return launch(arguments, cwd=self.root, env=self.environment,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wrapper(self, *command: str) -> subprocess.CompletedProcess[str]:
        with self.start_wrapper(*command) as process:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            return subprocess.CompletedProcess(process.args, process.wait(), output, errors)

    def background_sweep(self) -> ManagedProcess[str]:
        script = self.shell + '''
buildlog_exec "$TEST_PYTHON" "$TEST_COMMAND" "$TEST_REPO" real &
real_step=$!
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/real.pid"
invoke_sweep() {
    local status=0
    buildlog_exec "$TEST_PYTHON" "$TEST_COMMAND" "$TEST_REPO" sweep || status=$?
    printf '%s' "$status" > "$TEST_ROOT/sweep.status.tmp"
    mv "$TEST_ROOT/sweep.status.tmp" "$TEST_ROOT/sweep.status"
}
SWEEP_EVERY_S=300
sweep_after_step fixture-step
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/sweep.pid"
: > "$TEST_ROOT/both-started"
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/real.release"
wait "$real_step"
: > "$TEST_ROOT/step-done"
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/cleanup.release"
bash "$BOARD_HELPER" release "$BOARD_DIR" fixture cargo --pid "$$"
: > "$TEST_ROOT/cleanup-done"
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/sweep.status"
'''
        process = self.enterContext(launch(
            ["bash", "-c", script], cwd=self.root, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        self.addCleanup(self.finish_background_sweep, process)
        return process

    def finish_background_sweep(self, process: ManagedProcess[str]) -> None:
        for name in ("real.release", "cleanup.release", "sweep.release"):
            (self.root / name).touch()
        try:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            raise
        self.assertEqual(process.returncode, 0, output + errors)

    def wait_for(self, name: str, process: ManagedProcess[str]) -> None:
        path = self.root / name
        wait_until(lambda: path.exists() or process.poll() is not None,
                   self.root, processes=(process,))
        self.assertTrue(path.exists(), f"fixture exited before {name}")

    def step_process(self, name: str) -> StepProcess:
        return cast(StepProcess, json.loads((self.root / (name + ".pid")).read_text()))

    def test_background_sweep_leaves_the_real_step_as_the_owner(self) -> None:
        process = self.background_sweep()
        self.wait_for("both-started", process)
        record = cast(StepOwnership, json.loads(
            (self.root / "board" / "locks" / "cargo.d" / "owned").read_text()))
        self.assertEqual(record["start_format"], "darwin-lstart-utc-c-v1" if sys.platform == "darwin" else "linux-proc-stat22-v1")
        self.assertEqual(record["group"], self.step_process("real")["group"])
        self.assertNotEqual(record["group"], self.step_process("sweep")["group"])
        self.assertFalse((self.root / "real.finished").exists())
        self.assertFalse((self.root / "sweep.finished").exists())

    def test_releasing_after_a_step_leaves_its_background_sweep_running(self) -> None:
        process = self.background_sweep()
        self.wait_for("both-started", process)
        (self.root / "real.release").touch()
        self.wait_for("step-done", process)
        self.assertFalse((self.root / "sweep.finished").exists())
        (self.root / "cleanup.release").touch()
        self.wait_for("cleanup-done", process)
        self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())
        (self.root / "sweep.release").touch()
        self.wait_for("sweep.status", process)
        self.assertEqual((self.root / "sweep.status").read_text(), "0")
        self.assertTrue((self.root / "sweep.finished").exists())

    def test_signal_deaths_of_the_command_and_owner_use_shell_exit_codes(self) -> None:
        for victim in ("os.getpid()", "os.getppid()"):
            for signum, expected in ((signal.SIGKILL, 137), (signal.SIGTERM, 143)):
                with self.subTest(victim=victim, signal=signum):
                    source = f"import os; os.kill({victim}, {int(signum)})"
                    result = self.wrapper(sys.executable, "-c", source)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_normal_step_exit_codes_pass_through(self) -> None:
        for status in (0, 23, 125, 255):
            with self.subTest(status=status):
                result = self.wrapper(sys.executable, "-c", f"raise SystemExit({status})")
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def test_term_before_workload_launch_closes_the_owner_pipe(self) -> None:
        guardian = self.root / "guardian.py"
        _ = guardian.write_text("""\
import os
from pathlib import Path
import runpy
import select
import sys

owner_read, status_write, result_write = map(int, sys.argv[2:5])
if not select.select([owner_read], [], [], 10)[0]:
    raise TimeoutError("The owner pipe did not close.")
supervisor = runpy.run_path(sys.argv[1])
status = supervisor["supervise"](owner_read, status_write, result_write, sys.argv[5:])
assert status == 0
Path(os.environ["TEST_ROOT"], "guardian-finished").touch()
""")
        self.environment["TEST_GUARDIAN"] = str(guardian)
        launch = "with process_supervisor.launch("
        self.assertIn(launch, self.shell)
        terminate_after_spawn = f"""\
start_process = subprocess.Popen
def start_guardian(command, **options):
    guardian = start_process([*command[:3], os.environ["TEST_GUARDIAN"], *command[3:]], **options)
    os.kill(os.getpid(), {int(signal.SIGTERM)})
    return guardian
subprocess.Popen = start_guardian

"""
        self.shell = self.shell.replace(launch, terminate_after_spawn + launch, 1)
        result = self.wrapper(sys.executable, "-c", "pass")
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertTrue((self.root / "guardian-finished").exists(), result.stdout + result.stderr)
        self.assertFalse((self.root / "board" / "locks" / "cargo.d" / "owned").exists())

    def test_term_after_workload_launch_stops_the_group(self) -> None:
        launch_workload = "with process_supervisor.launch("
        self.assertIn(launch_workload, self.shell)
        self.shell = self.shell.replace(
            launch_workload, 'os.environ["TEST_STEP_OWNER"] = str(os.getpid())\n' + launch_workload, 1)
        with self.start_wrapper(sys.executable, "-c", """
import os, signal, subprocess, sys

member = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
assert os.getpgid(member.pid) == os.getpgrp()
print("workload and child started", flush=True)
os.kill(int(os.environ["TEST_STEP_OWNER"]), signal.SIGTERM)
signal.pause()
""") as process:
            # The group members hold the output pipes open until they exit.
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 143, output + errors)
            self.assertIn("workload and child started", output)


if __name__ == "__main__":
    _ = unittest.main()
