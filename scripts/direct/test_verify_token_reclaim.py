"""Check when the queue can release a dead caller's local grant."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
from typing import override
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit as gate


class VerifyTokenReclaimTests(VerifyTokenOwnershipFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        if sys.platform != "linux":
            self.skipTest("build scopes require Linux")
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if not runtime or not (Path(runtime) / "systemd/private").is_socket():
            self.skipTest("build scopes require a systemd user manager")
        self.environment["BUILDLOG_SCOPE"] = "1"
        for name in ("systemd-run", "systemctl"):
            executable = shutil.which(name)
            if executable is None:
                self.skipTest(f"build scopes require {name}")
            tool = self.root / "stubs" / name
            _ = tool.write_text(f"#!/bin/sh\nexport XDG_RUNTIME_DIR={shlex.quote(runtime)}\n"
                                + f'exec {shlex.quote(executable)} "$@"\n')
            tool.chmod(0o755)

        admission = caller_admission(self, self.root, self.environment)
        guard = gate.check_memory_reading
        reservation = admission.readings.reservation

        scope_membership: set[Path] = set()

        def membership_only(path: Path) -> None:
            if path in scope_membership:
                return
            if path.name == "cgroup.procs":
                for sidecar in (self.root / "tmp").glob("buildlog.*.peak.cgroup"):
                    try:
                        if sidecar.read_text().strip() == str(path.parent):
                            scope_membership.add(path)
                            return
                    except FileNotFoundError:
                        pass
            guard(path)

        def scoped_memory(path: Path, row: records.Reservation, now: datetime,
                          processes: dict[int, gate.ProcessIdentity], proc: Path,
                          policy: records.AdmissionPolicy) -> records.StepMemory:
            if row["sidecar"]:
                return records.MeasuredMemory(records.GIB)
            return reservation(path, row, now, processes, proc, policy)

        # Read process membership from the test's scope. Supply memory separately.
        _ = self.enterContext(mock.patch.object(gate, "check_memory_reading", side_effect=membership_only))
        _ = self.enterContext(mock.patch.object(admission.readings, "reservation", side_effect=scoped_memory))
        self.addCleanup(admission.run_pass)

    def start_cargo(self) -> tuple[subprocess.Popen[bytes], int]:
        log = self.root / "first-output"
        with log.open("wb") as output:
            first = subprocess.Popen(
                ["bash", str(VERIFY), "check", "sample"], cwd=self.root,
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
        os.kill(step_group, signal.SIGKILL)
        self.assertNotEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0)
        self.assert_next_build_starts_after_cargo_stops(step_group)

    def test_step_leader_death_defers_release_until_the_scope_is_empty(self) -> None:
        stop = self.root / "scope-stop"
        allow_stop = self.root / "allow-scope-stop"
        systemctl = self.root / "stubs/systemctl"
        _ = systemctl.write_text(systemctl.read_text().replace("#!/bin/sh\n", r'''#!/bin/sh
if [ "$2" = kill ]; then
    : > "$PACKAGE_ROOT/scope-stop"
    python3 "$TEST_WAIT_HELPER" "$PACKAGE_ROOT/allow-scope-stop"
fi
''', 1))
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
                os.kill(step_group, signal.SIGKILL)
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

    def test_failed_scope_kill_keeps_the_grant_and_returns_an_error(self) -> None:
        systemctl = self.root / "stubs/systemctl"
        _ = systemctl.write_text(systemctl.read_text().replace("#!/bin/sh\n", r'''#!/bin/sh
if [ "$2" = kill ]; then
    echo 'test manager refused the scope kill' >&2
    exit 7
fi
''', 1))
        first, step_group = self.start_cargo()
        admission = caller_admission(self, self.root, self.environment)
        record, = admission.caller_reservations().values()
        try:
            self.assertTrue(record.sidecar)
            _ = admission.run_pass()
            os.kill(step_group, signal.SIGKILL)
            status = first.wait(timeout=TEST_HANG_GUARD_SECS)
            log = (self.root / "first-output").read_text()
            self.assertIn("test manager refused the scope kill", log)
            self.assertEqual(status, 125, log)
            self.assertTrue(Path(record.sidecar).is_file())
            with admission.loop_turn():
                _ = admission.server.run_pass()
                self.assertIn(record.episode, admission.server.reservations)
        finally:
            self.end_group(step_group)
            with admission.loop_turn():
                _ = admission.server.run_pass()

    def test_dead_caller_retains_its_memory_grant_until_the_scope_empties(self) -> None:
        # Pause the manager command so the service observes the dead owner and live Cargo.
        stop = self.root / "scope-stop"
        systemctl = self.root / "stubs/systemctl"
        _ = systemctl.write_text(systemctl.read_text().replace("#!/bin/sh\n", r'''#!/bin/sh
if [ "$2" = kill ]; then
    : > "$PACKAGE_ROOT/scope-stop"
    python3 "$TEST_WAIT_HELPER" "$PACKAGE_ROOT/allow-scope-stop"
fi
''', 1))
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
