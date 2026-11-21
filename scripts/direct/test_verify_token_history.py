"""Real verify.sh token history behavior."""
from __future__ import annotations

import unittest
from pathlib import Path
from scripts.direct.recorder_fixture import record_wait_workers
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture
from scripts.production import staff


class VerifyTokenHistoryTests(VerifyTokenWaitFixture):
    def test_unblocked_verification_records_no_resource_wait(self) -> None:
        session = Path(self.environment["UNIT_SESSION_DIR"])
        self.record_phase(session)
        runner = staff.caller_identity()
        if runner:
            record_wait_workers(self.root, session, self.environment, {"fixture-impl": "thread-impl"},
                                runtime_ids={"fixture-impl": runner})
            self.environment["CODEX_THREAD_ID"] = runner
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.phase_waits(session), [])



if __name__ == "__main__":
    _ = unittest.main()
