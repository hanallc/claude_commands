"""Real verify.sh token history behavior."""
from __future__ import annotations

import unittest
from pathlib import Path
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture


class VerifyTokenHistoryTests(VerifyTokenWaitFixture):
    def test_unblocked_verification_records_no_resource_wait(self) -> None:
        session = Path(self.environment["UNIT_SESSION_DIR"])
        self.record_phase(session)
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.phase_waits(session), [])



if __name__ == "__main__":
    _ = unittest.main()
