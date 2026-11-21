"""Check when the queue can release a dead caller's local grant."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
from typing import override
from unittest import TestCase, mock

from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_scope_test_support import use_fixture_scope_readings, write_real_scope_launcher
from scripts.direct.verify_scoped_step import drain_scope
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class ScopeShutdownTests(TestCase):
    def test_kill_write_error_is_safe_if_the_scope_emptied(self) -> None:
        scope = Path(self.enterContext(tempfile.TemporaryDirectory()))
        events = scope / "cgroup.events"
        write_text = Path.write_text
        _ = write_text(events, "populated 1\n")

        def finish_before_kill(path: Path, contents: str) -> int:
            self.assertEqual(path, scope / "cgroup.kill")
            self.assertEqual(contents, "1\n")
            _ = write_text(events, "populated 0\n")
            raise FileNotFoundError("the scope ended before the kill write")

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=finish_before_kill):
            drain_scope(scope)


class VerifyTokenReclaimTests(VerifyTokenOwnershipFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        write_real_scope_launcher(self.root / "stubs")
        self.environment["BUILDLOG_SCOPE"] = "1"
        supervisor = self.scope_fixture_script().with_name("verify_scoped_step.py")
        source = supervisor.read_text()
        marker = 'if __name__ == "__main__":'
        self.assertEqual(source.count(marker), 1)
        hook = r"""
    from scripts.direct.wait_for_test_state import wait_for_file

    root = Path(os.environ["TEST_SCOPE_FIXTURE"])
    _ = (root / "scope-step-leader").write_text(sys.argv[2])
    if os.environ.get("TEST_PAUSE_SCOPE_KILL"):
        drain = drain_scope

        def pause(scope: Path) -> None:
            (root / "scope-stop").touch()
            wait_for_file(root / "allow-scope-stop")
            drain(scope)

        drain_scope = pause
"""
        _ = supervisor.write_text(source.replace(marker, marker + "\n" + hook))
        self.environment["TEST_SCOPE_FIXTURE"] = str(self.root)

        admission = caller_admission(self, self.root, self.environment)
        use_fixture_scope_readings(self, self.root, admission)
        self.addCleanup(admission.run_pass)

    def scope_fixture_script(self) -> Path:
        return self.root / "home/.claude/scripts/direct/verify.sh"

    def start_cargo(self) -> tuple[subprocess.Popen[bytes], int]:
        log = self.root / "first-output"
        with log.open("wb") as output:
            first = subprocess.Popen(
                ["bash", str(self.scope_fixture_script()), "check", "sample"], cwd=self.root,
                env={**self.environment, "TEST_BLOCK": "1"}, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True,
            )
        self.started_groups.add(first.pid)
        self.addCleanup(first.wait, TEST_HANG_GUARD_SECS)
        self.addCleanup(self.end_group, first.pid)
        wait_until(lambda: (self.root / "running").exists() or first.poll() is not None,
                   self.root, processes=(first,))
        self.assertTrue((self.root / "running").exists(), log.read_text())
        cargo_pid = int((self.root / "cargo-pid").read_text())
        step_group = os.getpgid(cargo_pid)
        self.started_groups.add(step_group)
        self.assertNotEqual(step_group, first.pid)
        return first, step_group

    def stop_step_leader(self) -> None:
        leader = int((self.root / "scope-step-leader").read_text())
        os.kill(leader, signal.SIGKILL)

    def refuse_manager_stop(self) -> None:
        systemctl = self.root / "stubs/systemctl"
        _ = systemctl.write_text("#!/bin/sh\necho 'test manager refused the scope stop' >&2\nexit 7\n")
        systemctl.chmod(0o755)

    def assert_next_build_starts_after_cargo_stops(self, step_group: int) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.allow_next_start()
        second = self.verify({**self.environment, "TEST_RECLAIM_CHECK": "1"})
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.live_group_processes(step_group), [])

    def test_caller_death_stops_old_cargo_before_the_next_build(self) -> None:
        first, step_group = self.start_cargo()
        first.kill()
        self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.assert_next_build_starts_after_cargo_stops(step_group)

    def test_step_leader_death_stops_the_other_scope_members(self) -> None:
        first, step_group = self.start_cargo()
        self.stop_step_leader()
        self.assertNotEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0)
        self.assert_next_build_starts_after_cargo_stops(step_group)

    def test_caller_death_drains_the_scope_when_the_manager_refuses(self) -> None:
        self.refuse_manager_stop()
        first, step_group = self.start_cargo()
        state, = (path for path in (self.root / "tmp").glob("verify*/scope-release.*")
                  if not path.name.endswith(".output"))
        first.kill()
        self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        result = subprocess.run(["flock", "-x", "-w", "20", str(state), "cat", str(state)],
                                capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(result.stdout, "", "scope shutdown failed")
        self.assertEqual(self.live_group_processes(step_group), [])

    def test_supervisor_stops_the_scope_when_cargo_output_is_blocked(self) -> None:
        release = self.root / "scope-release"
        release.touch()
        peak = self.root / "scope-peak"
        output_inode = self.root / "cargo-output-inode"
        scope_shell = subprocess.check_output(
            ["bash", "-c", 'source "$1"; printf "%s" "$BUILDLOG_SCOPE_SH"', "fixture",
             str(VERIFY.parent.parent / "lint/invoke.sh")], cwd=self.root, env=self.environment, text=True)
        cargo = r'''
import os
from pathlib import Path
import sys

inode = Path(sys.argv[1])
ready = inode.with_suffix(".ready")
ready.write_text(str(os.fstat(1).st_ino))
ready.replace(inode)
while True:
    os.write(1, b"x" * 65536)
'''
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, process_group=0) as leader:
            self.started_groups.add(leader.pid)
            read_fd, write_fd = os.pipe()
            with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb") as writer:
                capacity = fcntl.fcntl(writer.fileno(), fcntl.F_GETPIPE_SZ)
                self.assertEqual(os.write(writer.fileno(), b"x" * capacity), capacity)
                supervisor = subprocess.Popen(
                    [sys.executable, str(VERIFY.with_name("verify_scoped_step.py")), str(os.getpid()),
                     str(leader.pid), str(release), str(peak), scope_shell,
                     sys.executable, "-c", cargo, str(output_inode)],
                    cwd=self.root, env=self.environment, stdout=writer)
                try:
                    wait_until(lambda: output_inode.is_file() or supervisor.poll() is not None,
                               self.root, processes=(supervisor,))
                    self.assertTrue(output_inode.is_file(), f"scope launcher exited with {supervisor.poll()}")
                    self.assertEqual(int(output_inode.read_text()), os.fstat(reader.fileno()).st_ino)
                    leader.terminate()
                    self.assertEqual(supervisor.wait(timeout=30), 137)
                    self.assertEqual(release.read_bytes(), b"")
                    self.wait_for_group_exit(leader.pid)
                finally:
                    # If the relay regression blocks the supervisor, close its reader before collection.
                    reader.close()
                    leader.terminate()
                    _ = supervisor.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_step_leader_death_defers_release_until_the_scope_is_empty(self) -> None:
        stop = self.root / "scope-stop"
        allow_stop = self.root / "allow-scope-stop"
        self.environment["TEST_PAUSE_SCOPE_KILL"] = "1"
        flock = self.root / "stubs/flock"
        real_flock = flock.resolve()
        flock.unlink()
        _ = flock.write_text(f'''#!/bin/sh
if [ "$1" = -x ] && [ "$2" = -w ]; then : > "$PACKAGE_ROOT/scope-release-wait"; fi
exec {shlex.quote(str(real_flock))} "$@"
''')
        flock.chmod(0o755)

        admission = caller_admission(self, self.root, self.environment)
        previous = admission.server.handle_request
        release = self.root / "release-request"

        def handle(request: wire.Request,
                   connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            if isinstance(request, (wire.CancelPlacement, wire.ReleaseRequest, wire.ExecutionReport)):
                release.touch()
            return previous(request, connection)

        first, step_group = self.start_cargo()
        with mock.patch.object(admission.server, "handle_request", side_effect=handle):
            try:
                self.stop_step_leader()
                waiting = self.root / "scope-release-wait"
                wait_until(lambda: (release.is_file() or first.poll() is not None
                                    or (stop.is_file() and waiting.is_file())), self.root, processes=(first,))
                self.assertFalse(release.exists(), "verify sent a release while the scope was populated")
                self.assertIsNone(first.poll())
            finally:
                allow_stop.touch()
                self.wait_for_group_exit(step_group)
                self.assertNotEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertTrue(release.exists())

    def test_buildlog_scope_zero_cannot_disable_the_placed_scope(self) -> None:
        self.environment["BUILDLOG_SCOPE"] = "0"
        first, step_group = self.start_cargo()
        try:
            admission = caller_admission(self, self.root, self.environment)
            record, = admission.caller_reservations().values()
            self.assertTrue(record.sidecar, "the local grant has no scope sidecar")
            scope = Path(Path(record.sidecar).read_text().strip())
            pid = (self.root / "cargo-pid").read_text().strip()
            self.assertIn(pid, (scope / "cgroup.procs").read_text().split())
            _ = admission.run_pass()
        finally:
            first.terminate()
            self.assertNotEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.wait_for_group_exit(step_group)

    def test_scope_stop_error_returns_while_a_descendant_keeps_output_open(self) -> None:
        self.refuse_manager_stop()
        cargo = self.root / "stubs/cargo"
        original = cargo.read_text()
        blocking = 'if [ "${TEST_BLOCK:-0}" = 1 ]; then'
        self.assertEqual(original.count(blocking), 1)
        _ = cargo.write_text(original.replace(blocking, blocking + r"""
        python3 "$TEST_WAIT_HELPER" "$TEST_RELEASE.descendant" &
        printf '%s\n' "$!" > "$PACKAGE_ROOT/cargo-descendant"
"""))
        first, step_group = self.start_cargo()
        admission = caller_admission(self, self.root, self.environment)
        record, = admission.caller_reservations().values()
        sidecar = Path(record.sidecar)
        scope = Path(sidecar.read_text().strip())
        kill_file = scope / "cgroup.kill"
        child = int((self.root / "cargo-descendant").read_text())
        self.assertIn(str(child), (scope / "cgroup.procs").read_text().split())
        kill_file.chmod(0)
        try:
            _ = admission.run_pass()
            (self.root / "release").touch()
            status = first.wait(timeout=30)
            log = (self.root / "first-output").read_text()
            self.assertIn("keeping the reservation", log)
            self.assertIn("cgroup.kill", log)
            self.assertEqual(status, 125, log)
            self.assertIn(str(child), (scope / "cgroup.procs").read_text().split())
            with admission.loop_turn():
                _ = admission.server.run_pass()
                self.assertIn(record.episode, admission.server.reservations)
        finally:
            kill_file.chmod(0o200)
            _ = kill_file.write_text("1\n")
            self.wait_for_group_exit(step_group)
            _ = admission.run_pass()

    def test_dead_caller_retains_its_memory_grant_until_the_scope_empties(self) -> None:
        stop = self.root / "scope-stop"
        self.environment["TEST_PAUSE_SCOPE_KILL"] = "1"
        first, step_group = self.start_cargo()
        admission = caller_admission(self, self.root, self.environment)
        reservations = admission.caller_reservations()
        self.assertEqual(len(reservations), 1)
        record, = reservations.values()
        sidecar = Path(record.sidecar)
        self.assertTrue(record.sidecar and sidecar.is_file(), "the grant has no published build scope")
        cargo_pid = (self.root / "cargo-pid").read_text().strip()
        scope = Path(sidecar.read_text().strip())
        self.assertIn(cargo_pid, (scope / "cgroup.procs").read_text().split())
        first.kill()
        self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        try:
            wait_until(stop.is_file, self.root)
            with admission.loop_turn():
                _ = admission.server.run_pass()
                self.assertIn(record.episode, admission.server.reservations)
            self.assertIn(int(cargo_pid), self.live_group_processes(step_group))
        finally:
            (self.root / "allow-scope-stop").touch()
            self.wait_for_group_exit(step_group)
            with admission.loop_turn():
                _ = admission.server.run_pass()
        self.assert_next_build_starts_after_cargo_stops(step_group)

    def test_old_cleanup_keeps_the_new_grant_in_a_reused_wait_folder(self) -> None:
        memory_gate = VERIFY.parent.parent / "lint/memory_gate.sh"
        harness = self.root / "reuse-placement.sh"
        _ = harness.write_text("set -e\n"
                               + shell_functions(VERIFY, "placement_once")
                               + shell_functions(memory_gate, "buildlog_release_memory", "buildlog_wait_for_memory")
                               + '''
verify_helper() { :; }
local_cargo_build_folder() { printf '%s\\n' "$PWD/target" "$PWD/build"; }
buildlog_check_nested_step() { :; }
buildlog_memory_python() { :; }

HOLD_WAIT_DIR=$1
DOC_REPO_TOP=$PWD DOC_TREE=fixture-tree DOC_SOURCE_KEY=fixture-tree
BUILDLOG_FIRST_STEP_REQUEST="$HOLD_WAIT_DIR/request"
PLACEMENT_COMMAND=(cargo check --package sample)
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
