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

from scripts.build_queue import admission_records as records
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

        def membership_only(path: Path) -> None:
            if path.name == "cgroup.procs":
                for sidecar in (self.root / "tmp").glob("buildlog.*.peak.cgroup"):
                    try:
                        if sidecar.read_text().strip() == str(path.parent):
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
