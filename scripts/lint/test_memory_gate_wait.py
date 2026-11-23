"""A new admission wait must not use its parent's queue identity."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment, memory_gate_shell_environment
from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted
from scripts.build_queue.admission_records import process_start_time
from scripts.build_queue import admission_protocol as wire
from scripts.lib.shell_test_support import shell_functions


class MemoryGateWaitTests(unittest.TestCase):
    def test_step_command_inherits_the_grant_holder_marker_only_during_execution(self) -> None:
        invoke = Path(__file__).with_name("invoke.sh")
        shell = shell_functions(invoke, "buildlog_exec") + r'''
BUILDLOG_MEM_RESERVATION=4242-start-1
BUILDLOG_MEM_MAX_PATH="$1/admission/4242-start-1.max"
BUILDLOG_PEAK=""
buildlog_exec /bin/sh -c 'printf "%s\n" "$BUILDLOG_RESERVED_STEP"'
[[ -z "${BUILDLOG_RESERVED_STEP:-}" ]]
'''
        with tempfile.TemporaryDirectory() as work:
            environment = {**memory_gate_shell_environment(), "ADMISSION_SOCKET": f"{work}/queue.sock"}
            result = subprocess.run(["bash", "-c", shell, "test", work], env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"4242-start-1\t4242\t{work}/admission\t{work}/queue.sock\n")

    def test_relative_socket_marker_keeps_the_step_folder_after_chdir(self) -> None:
        invoke = Path(__file__).with_name("invoke.sh")
        shell = shell_functions(invoke, "buildlog_exec") + r'''
BUILDLOG_MEM_RESERVATION=4242-start-1
BUILDLOG_MEM_MAX_PATH="$1/admission/4242-start-1.max"
BUILDLOG_PEAK=""
buildlog_exec /bin/sh -c 'cd child && printf "%s\n" "$BUILDLOG_RESERVED_STEP"'
'''
        with tempfile.TemporaryDirectory() as work:
            root = Path(work).resolve()
            (root / "child").mkdir()
            environment = {**memory_gate_shell_environment(), "ADMISSION_SOCKET": "queue.sock"}
            result = subprocess.run(["bash", "-c", shell, "test", str(root)], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"4242-start-1\t4242\t{root}/admission\t{root}/queue.sock\n")

    def test_nested_step_refuses_before_memory_or_cargo_token_wait(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        token = shell_functions(scripts / "direct/verify.sh", "acquire_token")
        shell = r'''
source "$1"
sync_token_state() { :; }
token_wait_clock() { touch "$TEST_TOKEN_ATTEMPT"; exit 99; }
TOKEN_USED=1 TOKEN_HELD=0
'''
        shell += token + '\n"$TEST_NESTED_ACTION"\n'
        with AdmissionFixture() as fixture:
            start = process_start_time(os.getpid())
            assert start is not None
            outer = fixture.request(owner=wire.Owner(os.getpid(), start))
            with fixture.loop_turn():
                grant = only_admitted(fixture.server.handle_request(outer))
            marker = f"{grant.reservation}\t{outer.owner.pid}\t{fixture.state_dir.resolve()}"
            attempted = fixture.root / "token-attempt"
            for action in ("acquire_token", "buildlog_wait_for_memory"):
                with self.subTest(action=action):
                    environment = {**memory_gate_shell_environment(), "BUILDLOG_RESERVED_STEP": marker,
                                   "TEST_TOKEN_ATTEMPT": str(attempted), "TEST_NESTED_ACTION": action}
                    result = subprocess.run(["bash", "-c", shell, "test", str(scripts / "lint/memory_gate.sh")],
                                            env=environment, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                    self.assertIn(f"{grant.reservation} (pid {outer.owner.pid})", result.stderr)
                    self.assertFalse(attempted.exists())

    def test_relative_source_keeps_the_launcher_after_a_directory_change(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            cache = root / "cache"
            seed_interpreter_cache(cache)
            environment = {**memory_gate_shell_environment(), **config_environment(root),
                           "HOME": str(root), "XDG_CACHE_HOME": str(cache)}
            _ = environment.pop("CLAUDE_PYTHON", None)
            shell = 'source scripts/lint/memory_gate.sh && cd "$1" && buildlog_memory_python -c \'print(123)\''
            result = subprocess.run(
                ["bash", "-c", shell, "test", str(root)],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "123\n")

    def test_admission_pipeline_preserves_pipefail_and_the_client_exit_status(self) -> None:
        gate = Path(__file__).with_name("memory_gate.sh")
        self.assertNotRegex(gate.read_text(), r"(?m)^\s*local\s+-\s*(?:#.*)?$")
        shell = r'''
source "$1"
set +o pipefail
owner_before=$BASHPID
buildlog_memory_python() {
    local file owner
    while (( $# )); do
        case "$1" in
            --pid) owner=$2; shift 2 ;;
            --shell-state) file=$2; shift 2 ;;
            *) shift ;;
        esac
    done
    printf '%s\n' "$owner" > "$TEST_CLIENT_OWNER"
    printf 'BUILDLOG_MEM_OUTCOME=Granted\n' > "$file"
    printf 'admission event\n'
    return "$TEST_CLIENT_STATUS"
}
case "$TEST_WAIT_PATH" in
    mac) OSTYPE=darwin; unset BUILDLOG_MEMINFO ;;
    target) OSTYPE=linux-gnu; BUILDLOG_CALLER=verify-linux ;;
    full) OSTYPE=linux-gnu; BUILDLOG_CALLER=verify ;;
esac
status=0
buildlog_wait_for_memory /bin/true || status=$?
if [[ -o pipefail ]]; then printf 'pipefail leaked\n' >&2; exit 91; fi
if [[ "$TEST_WAIT_PATH" == full ]]; then
    [[ "$(cat "$TEST_CLIENT_OWNER")" == "$owner_before" ]] || exit 92
else
    [[ ! -e "$TEST_CLIENT_OWNER" ]] || exit 93
fi
printf '%s|%s\n' "$status" "$BUILDLOG_MEM_OUTCOME"
'''
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            environment = {**memory_gate_shell_environment(), **config_environment(root), "BUILDLOG_CALLER": "",
                           "TEST_CLIENT_OWNER": str(root / "client-owner")}
            for path, status, outcome in (("mac", 0, "Granted"), ("target", 0, "Granted"),
                                          ("full", 0, "Granted"), ("full", 37, "Refused")):
                with self.subTest(path=path, status=status):
                    result = subprocess.run(["bash", "-c", shell, "test", str(gate)],
                        env={**environment, "TEST_WAIT_PATH": path, "TEST_CLIENT_STATUS": str(status)},
                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, f"{status}|{outcome}\n")

    def test_parent_queue_identity_does_not_age_a_new_launch_or_step(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            parent_wait = root / "parent-first-wait"
            _ = parent_wait.write_text("1\n")
            environment = {**memory_gate_shell_environment(), **config_environment(root),
                           "BUILDLOG_MEM_WAIT_SINCE": "1",
                           "BUILDLOG_MEM_WAIT_FIRST_FILE": str(parent_wait)}
            shell = r'''
source "$1"
# Capture the real admission arguments without a live service.
buildlog_memory_python() {
    local file since
    while (( $# )); do
        case "$1" in
            --since) since=$2; shift 2 ;;
            --shell-state) file=$2; shift 2 ;;
            *) shift ;;
        esac
    done
    printf '%s\n' "$since" >> "$TEST_WAIT_OBSERVED"
    printf 'BUILDLOG_MEM_OUTCOME=Granted\n' > "$file"
}
buildlog_wait_for_memory
buildlog_wait_for_memory /bin/true
[[ -z "$BUILDLOG_MEM_WAIT_FIRST_FILE" ]]
'''
            observed = root / "observed"
            result = subprocess.run(["bash", "-c", shell, "test", str(Path(__file__).with_name("memory_gate.sh"))],
                                    env={**environment, "TEST_WAIT_OBSERVED": str(observed)},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(all(int(since) > 1 for since in observed.read_text().splitlines()))
            self.assertEqual(len(observed.read_text().splitlines()), 2)
            self.assertEqual(parent_wait.read_text(), "1\n")
