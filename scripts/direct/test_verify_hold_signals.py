"""Real verification hold waits: signals."""

from __future__ import annotations

import json
import fcntl
import subprocess
import os
import signal
import time
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.lib.fixture_environment_test_support import verify_test_environment
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.process_group import send_process_group
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.wait_for_test_state import wait_for_file, wait_for_pid
from scripts.direct.verify_hold_wait_test_support import (
    VerifyHoldWaitFixture,
    WaitObservation,
    read_cycle_file,
)


INVOKE = VERIFY.parent.parent / "lint/invoke.sh"
SIGNAL_EXIT_SECONDS = 5
_SIGNAL_FIXTURE_PARENT = Path("/dev/shm") if Path("/dev/shm").is_dir() else None


@contextmanager
def full_output_pipe() -> Generator[int]:
    read_fd, write_fd = os.pipe()
    try:
        contents = b"x" * fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
        written = os.write(write_fd, contents)
        assert written == len(contents)
        yield write_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    @classmethod
    @override
    def setUpClass(cls, *, fixture_parent: Path | None = _SIGNAL_FIXTURE_PARENT) -> None:
        # Keep fixture copies and cleanup writes off the disk shared by other tests.
        super().setUpClass(fixture_parent=fixture_parent)

    def assert_group_stop_clears_memory_wait(self, signum: signal.Signals) -> None:
        process = self.start_memory_queued_lint()
        queued = self.memory_queue_pids()
        self.assertTrue(queued, "the admission service must register the waiting step")
        self.assertEqual(queued, {process.pid},
                         "the coordinator waits at admission before any step starts")
        started = time.monotonic()
        send_process_group(process.pid, signum)
        # Drain the log pipes while the coordinator records the interrupted call.
        code, output = self.finish(process)
        survivors = self.wait_for_owned_exit(max(0, started + SIGNAL_EXIT_SECONDS - time.monotonic()))
        elapsed = time.monotonic() - started
        remaining = self.memory_queue_pids()
        diagnostic = (f"{signum.name}: exit={code}, elapsed={elapsed:.3f}s, "
                      + f"queued={sorted(queued)}, remaining={sorted(remaining)}, survivors={sorted(survivors)}\n"
                      + "\n".join(output.splitlines()[-80:]))
        self.assertEqual(survivors, set(), diagnostic)
        self.assertTrue(queued.isdisjoint(remaining), diagnostic)
        self.assertEqual(code, 128 + signum, diagnostic)
        self.assertLessEqual(elapsed, SIGNAL_EXIT_SECONDS, diagnostic)
        self.assertEqual(self.fixture.last_call()["outcome"], "interrupted", output)
        self.assertEqual(self.fixture.saved_results(), [], output)

    def test_group_signals_end_memory_wait_and_all_children(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                if signum != signal.SIGTERM:
                    self.assertTrue(self.doCleanups(), "the preceding signal fixture did not stop")
                    self.setUp()
                self.assert_group_stop_clears_memory_wait(signum)

    def test_sigint_before_placement_pid_is_saved_reaps_the_waiter(self) -> None:
        hold = self.fixture.runner_state
        os.mkfifo(hold / "placement-ready")
        interrupted = shell_functions(VERIFY, "verify_interrupted")
        after_handler = VERIFY.read_text().split(interrupted, 1)[1]
        # Use the signal bindings from Verify with its handler.
        signal_bindings = after_handler.split("\nif ", 1)[0]
        harness = (
            shell_functions(VERIFY, "placement_once", "interrupt_hold_wait", "finish_exit_diagnostics")
            + interrupted + signal_bindings + "\n"
            + r'''
set -eu
HOLD_WAIT_DIR=$TEST_RUNNER_STATE
DOC_REPO_TOP=$PWD STEP_RECORD_SCRIPT=$1
CMD=test PKG=sample BUILDLOG_PEAK="" BUILDLOG_MEM_ADMIT_SCRIPT=placement-fixture
PLACEMENT_EPISODE=fixture PLACEMENT_SINCE=0 PLACEMENT_SOURCE_KEY=fixture PLACEMENT_SESSION=fixture
PLACEMENT_ASSIGNMENT_FILE=$HOLD_WAIT_DIR/assignment PLACEMENT_GRANT_FILE=$HOLD_WAIT_DIR/grant
PLACEMENT_COMMAND=()
HOLD_WAITER_PID="" RUN_STEP_PID="" BUILDLOG_HOLD_WAITER_FILE=""
exit_diagnostics=$(mktemp "$HOLD_WAIT_DIR/exit.XXXXXX")
unset BUILDLOG_FIRST_STEP_REQUEST BUILDLOG_FIRST_STEP_ASSIGNMENT
exec 7<&0 8<>"$HOLD_WAIT_DIR/placement-ready"

verify_helper() {
    [[ "$2" == place-step ]]
    exec 9>"$HOLD_WAIT_DIR/placement-lock"
    flock -x 9
    printf 'locked\n' >&8
    read -r -u 7 unused
}
verify_cleanup() {
    flock -x "$HOLD_WAIT_DIR/placement-lock" true
    wait "$placement_pid" 2>/dev/null || true
}
finish_verify_run() { :; }
stop_interrupted_run_log() { :; }

before_placement_pid() {
    if [[ "$BASHPID" == "$$" && "$BASH_COMMAND" == 'HOLD_WAITER_PID=$!' ]]; then
        trap - DEBUG
        [[ -z "$HOLD_WAITER_PID" ]]
        placement_pid=$!
        read -r -u 8 ready
        [[ "$ready" == locked ]]
        printf '%s\n' "$placement_pid" > "$HOLD_WAIT_DIR/placement-pid"
        read -r -u 7 unused
    fi
}
set -T
trap before_placement_pid DEBUG
placement_once
'''
        )
        step_record = self.fixture.verify_script.with_name("step_record.py")
        process = subprocess.Popen(
            ["bash", "-c", harness, "verify-placement", str(step_record)],
            cwd=self.fixture.root, env=self.fixture.environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        placement_pid = wait_for_pid(hold / "placement-pid", processes=(process,))
        process.send_signal(signal.SIGINT)
        # Keep stdin open until Bash exits so the fixture cannot release the lock.
        try:
            status = process.wait(timeout=SIGNAL_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail(f"Verify did not exit after SIGINT while placement PID {placement_pid} held the lock")
        self.assertEqual(status, 130)
        self.assertFalse(Path(f"/proc/{placement_pid}").exists())

    def verify_exit_command(self, token: Path, action: str) -> list[str]:
        harness = shell_functions(INVOKE, "close_verify_exit_stderr") + shell_functions(
            VERIFY, "save_verify_exit_stderr", "finish_exit_diagnostics",
            "run_log_tee", "start_run_log", "stop_interrupted_run_log",
            "report_stop", "verify_exit", "verify_interrupted",
        ) + r'''
RUN_LOG=$1 RUN_LOG_PIDS=() CMD=test held_token=$2 VERIFY_START_DIR=$3
RUN_STEP_PID=""
PY=$VERIFY_START_DIR/../lib/py
exec 74>"$RUN_LOG.lease"
save_verify_exit_stderr
[[ /dev/fd/74 -ef "$RUN_LOG.lease" && -n "$VERIFY_EXIT_STDERR_FD" ]] || exit 90
exit_diagnostics="$RUN_LOG.exit-diagnostics"
: > "$exit_diagnostics"
interrupt_hold_wait() { printf 'hold diagnostic\n' >&2; }
release_token() { rm -- "$held_token"; }
verify_cleanup() {
    printf '%*s\n' "${cleanup_diagnostic_width:-0}" 'cleanup diagnostic' >&2
    release_token
    printf '%s\n' "$1"
}
finish_verify_run() { :; }
'''
        return ["bash", "-c", harness + action, "test", str(self.fixture.root / "run.log"), str(token), str(VERIFY.parent)]

    def test_exit_reports_interruption_when_the_log_cannot_report(self) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        action = "start_run_log\nexec 2>/dev/null\nverify_exit 0 'printf output'\n"
        result = subprocess.run(self.verify_exit_command(token, action),
                                env=verify_test_environment(), capture_output=True, text=True,
                                check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, 128, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "\n".join((
            "hold diagnostic", "cleanup diagnostic", "interrupted",
            "verify.sh: interrupted before test completed (status 128).", "",
        )))
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.fixture.root / "run.log.exit-diagnostics").exists())

    def test_interrupted_exit_finishes_with_full_stderr(self) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        with full_output_pipe() as write_fd:
            result = subprocess.run(self.verify_exit_command(token, "verify_exit 0 'printf output'"),
                                    env=verify_test_environment(), stdout=subprocess.PIPE, stderr=write_fd, text=True,
                                    check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, 128, result.stdout)
        self.assertFalse(token.exists())
        self.assertFalse((self.fixture.root / "run.log.exit-diagnostics").exists())

    def test_normal_exit_keeps_result_messages_on_stdout(self) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        result = subprocess.run(self.verify_exit_command(token, "start_run_log\nverify_exit 0 exit"),
                                env=verify_test_environment(), capture_output=True, text=True,
                                check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "completed\n")
        self.assertEqual(result.stderr, "cleanup diagnostic\n")
        self.assertFalse(token.exists())

    def test_normal_exit_releases_the_token_with_full_stdout(self) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        with full_output_pipe() as write_fd:
            result = subprocess.run(self.verify_exit_command(token, "verify_exit 0 exit"),
                                    env=verify_test_environment(), stdout=write_fd, stderr=subprocess.PIPE,
                                    text=True, check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "cleanup diagnostic\n")
        self.assertFalse(token.exists())

    def test_normal_exit_does_not_repeat_cleanup_on_term_during_replay(self) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        action = r'''
trap 'verify_interrupted 15' TERM
finish_verify_run() { printf 'recorded %s\n' "$1"; }
real_py=$PY
replay_helper() {
    if [[ "${!#}" == */verify-result.* ]]; then
        [[ ! -e "$held_token" ]] || exit 90
        kill -TERM "$BASHPID"
    fi
    "$real_py" "$@"
}
PY=replay_helper
verify_exit 23 'exit 23'
'''
        result = subprocess.run(self.verify_exit_command(token, action),
                                env=verify_test_environment(), capture_output=True, text=True,
                                check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "error\nrecorded 23\n")
        self.assertEqual(result.stderr, "cleanup diagnostic\n")
        self.assertFalse(token.exists())

    def test_normal_exit_keeps_stdout_when_the_result_buffer_fails(self) -> None:
        for fail_mktemp in ("return 1", 'printf "%s/child\\n" "$held_token"'):
            with self.subTest(fail_mktemp=fail_mktemp):
                token = self.fixture.root / "held-token"
                token.touch()
                action = f"mktemp() {{ {fail_mktemp}; }}\nverify_exit 0 exit"
                result = subprocess.run(self.verify_exit_command(token, action),
                                        env=verify_test_environment(), capture_output=True, text=True,
                                        check=False, timeout=SIGNAL_EXIT_SECONDS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "completed\n")
                self.assertEqual(result.stderr.count("cleanup diagnostic\n"), 1, result.stderr)
                self.assertFalse(token.exists())

    def test_stderr_closes_while_a_step_child_is_still_running(self) -> None:
        cargo = self.fixture.bin_directory / "cargo"
        _ = cargo.rename(cargo.with_name("cargo-original"))
        release = self.fixture.runner_state / "detached-release"
        started = self.fixture.runner_state / "detached-pid"
        os.mkfifo(release)
        self.fixture.write_executable(cargo, r"""#!/usr/bin/env bash
if [[ "$1" == check ]]; then
    (
        printf '%s\n' "$BASHPID" > "$TEST_RUNNER_STATE/detached-pid"
        read -r unused < "$TEST_RUNNER_STATE/detached-release"
    ) </dev/null >/dev/null 2>&1 &
fi
exec "${0%/*}/cargo-original" "$@"
""")
        process = self.start("check", "sample", "--local", "needs the local test fixture")
        release_fd = os.open(release, os.O_RDWR)
        try:
            pid = wait_for_pid(started, processes=(process,))
            output, errors = process.communicate(timeout=SIGNAL_EXIT_SECONDS)
            self.assertEqual(process.returncode, 0, output + errors)
            os.kill(pid, 0)
        finally:
            try:
                _ = os.write(release_fd, b"go\n")
            finally:
                os.close(release_fd)

    def test_exit_and_term_release_token_when_diagnostics_cannot_open(self) -> None:
        setup = r'''
set -e
exit_diagnostics="$exit_diagnostics/child"
trap 'verify_exit "$?" "$BASH_COMMAND"' EXIT
trap 'verify_interrupted 15' TERM
'''
        for action, status in (("exit 23", 23), ('kill -TERM "$$"', 143)):
            with self.subTest(status=status):
                token = self.fixture.root / "held-token"
                token.touch()
                with full_output_pipe() as write_fd:
                    result = subprocess.run(self.verify_exit_command(token, setup + action),
                                            env=verify_test_environment(), stdout=subprocess.PIPE,
                                            stderr=write_fd, text=True, check=False, timeout=SIGNAL_EXIT_SECONDS)
                self.assertEqual(result.returncode, status, result.stdout)
                self.assertFalse(token.exists())

    def assert_dead_run_log_releases_token(self, command: str, status: int) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        action = r'''
start_run_log
reader_pid=${RUN_LOG_PIDS[1]}
kill -KILL "$reader_pid" || exit 90
wait "$reader_pid" 2>/dev/null || true
if kill -0 "$reader_pid" 2>/dev/null; then exit 91; fi
trap 'verify_exit "$?" "$BASH_COMMAND"' EXIT
trap 'verify_interrupted 15' TERM
''' + command
        result = subprocess.run(self.verify_exit_command(token, action), env=verify_test_environment(),
                                capture_output=True, text=True, check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        self.assertFalse(token.exists(), result.stdout + result.stderr)

    def test_nonzero_exit_releases_token_after_log_reader_dies(self) -> None:
        self.assert_dead_run_log_releases_token("exit 23", 23)

    def test_term_releases_token_after_log_reader_dies(self) -> None:
        self.assert_dead_run_log_releases_token('kill -TERM "$$"', 143)

    def test_term_releases_token_with_live_log_reader_and_full_stderr(self) -> None:
        token = self.fixture.root / "held-token"
        token.touch()
        action = r'''
start_run_log
kill -0 "${RUN_LOG_PIDS[1]}" || exit 90
# Keep the original stderr pipe full during diagnostic forwarding.
cleanup_diagnostic_width=1048576
trap 'verify_interrupted 15' TERM
kill -TERM "$$"
'''
        with full_output_pipe() as write_fd:
            result = subprocess.run(self.verify_exit_command(token, action), env=verify_test_environment(),
                                    stdout=subprocess.PIPE, stderr=write_fd, text=True,
                                    check=False, timeout=SIGNAL_EXIT_SECONDS)
        self.assertEqual(result.returncode, 143)
        self.assertFalse(token.exists())

    def test_hup_reaps_log_readers_when_the_output_pipe_is_full(self) -> None:
        # Fill the consumer's pipe before starting verify's real log helpers.
        # No consumer drains it until after shutdown, so EOF on tee's input
        # cannot finish a pending output write.
        with full_output_pipe() as write_fd:
            log = self.fixture.root / "blocked-run.log"
            harness = shell_functions(
                VERIFY, "run_log_tee", "start_run_log", "stop_interrupted_run_log",
            ) + r'''
RUN_LOG=$1 RUN_LOG_PIDS=()
trap 'stop_interrupted_run_log; exit 129' HUP
exec 3>&2
start_run_log
printf '%8192s' x
printf '%s\n' "${RUN_LOG_PIDS[*]}" >&3
read -r command
'''
            with subprocess.Popen(["bash", "-c", harness, "test", str(log)],
                                  env=verify_test_environment(), stdin=subprocess.PIPE, stdout=write_fd,
                                  stderr=subprocess.PIPE, start_new_session=True, text=True) as process:
                try:
                    assert process.stderr is not None
                    reader_pids = cast(str, process.stderr.readline())
                    readers = [int(pid) for pid in reader_pids.split()]
                    self.assertEqual(len(readers), 2)
                    wait_for_file(log)
                    send_process_group(process.pid, signal.SIGHUP)
                    self.assertEqual(process.wait(timeout=5), 129)
                    self.assertTrue(all(not Path(f"/proc/{pid}").exists() for pid in readers))
                finally:
                    try:
                        send_process_group(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _ = process.communicate(timeout=5)

    def test_interrupted_verification_removes_its_registered_call(self) -> None:
        self.hold()
        observation = self.observe_waits()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        process.send_signal(signal.SIGTERM)
        code, output = self.finish(process)
        self.assertEqual(code, 143, output)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())

    def test_signal_at_waiting_announcement_records_wait_and_removes_the_call(self) -> None:
        from scripts.build_targets import build_targets

        self.hold()
        wait_file = self.fixture.root / "announced-wait"
        started = datetime.fromisoformat("2026-10-14T00:00:00+00:00")

        def interrupt_on_announcement(*messages: object, **_options: object) -> None:
            if "; waiting (tests that can run on the Mac" in " ".join(str(message) for message in messages):
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)

        with mock.patch.dict(os.environ, {**self.fixture.environment,
                                          "BUILDLOG_HOLD_WAIT_FILE": str(wait_file)}), \
             mock.patch.object(build_targets, "wait_clock", side_effect=[started, started + timedelta(seconds=7)]), \
             mock.patch("builtins.print", side_effect=interrupt_on_announcement), \
             self.assertRaises(SystemExit) as interrupted:
            _ = build_targets.command_wait_local("announcement-call")
        self.assertEqual(interrupted.exception.code, 143)
        self.assertEqual(wait_file.read_text(), "7.000000\n")
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])

    def test_interrupt_during_a_step_wait_records_seconds_and_removes_the_call(self) -> None:
        history = self.record_phase()
        self.hold_after_first_step()
        observation = self.observe_waits()
        process = self.start("final")
        _ = self.wait_for_call(process)
        process.send_signal(signal.SIGTERM)
        code, output = self.finish(process)
        self.assertEqual(code, 143, output)
        self.assertEqual(self.fixture.local_runs(), [])
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started = [row for row in rows if row["event_type"] == "wait_started"]
        finished = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertTrue(started)
        self.assertEqual({row["wait_id"] for row in started}, {row["wait_id"] for row in finished})
        self.assertTrue(all(cast(dict[str, object], row["wait_end"])["state"] == "Finished" for row in finished))


if __name__ == "__main__":
    _ = unittest.main()
