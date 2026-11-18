"""Check local step cleanup after the verify caller dies."""

from __future__ import annotations

import os
import signal
import subprocess

from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenReclaimTests(VerifyTokenOwnershipFixture):
    def test_caller_death_stops_old_cargo_before_the_next_build(self) -> None:
        first = subprocess.Popen(
            ["bash", str(VERIFY), "check", "sample"], cwd=self.root,
            env={**self.environment, "TEST_BLOCK": "1"}, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running(self.root / "running", first)
            cargo_pid = int((self.root / "cargo-pid").read_text())
            step_group = os.getpgid(cargo_pid)
            self.started_groups.add(step_group)
            self.assertNotEqual(step_group, first.pid)

            first.kill()
            self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGKILL)
            second = self.verify({**self.environment, "TEST_RECLAIM_CHECK": "1"})
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.live_group_processes(step_group), [])
        finally:
            (self.root / "release").touch()
            self.end_group(first.pid)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
