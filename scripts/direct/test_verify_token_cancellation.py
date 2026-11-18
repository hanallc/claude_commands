"""Real verify.sh token cancellation behavior."""
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenCancellationTests(VerifyTokenOwnershipFixture):
    def test_group_cleanup_ends_without_pidfd_support(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                 start_new_session=True)
        try:
            # Pin the race where a killed member is still visible in the first
            # snapshot, so portable cleanup must take its polling branch.
            with patch.object(os, "pidfd_open", side_effect=AttributeError("pidfd_open unavailable"),
                              create=True), patch.object(self, "live_group_processes",
                                                         side_effect=[[child.pid], []]):
                self.end_group(child.pid)
            self.assertNotEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(self.live_group_processes(child.pid), [])
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
