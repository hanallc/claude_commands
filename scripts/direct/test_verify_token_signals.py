"""Real verify.sh token signals behavior."""
from __future__ import annotations

import signal
from typing import override

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_scope_test_support import use_fixture_scope_readings, write_real_scope_launcher
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture


class VerifyTokenSignalsTests(VerifyTokenOwnershipFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        write_real_scope_launcher(self.root / "stubs")
        use_fixture_scope_readings(self, self.root, caller_admission(self, self.root, self.environment))

    def test_sigterm_stops_step_on_caller_interrupt(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGTERM)

    def test_sigint_stops_step_on_caller_interrupt(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGINT)
