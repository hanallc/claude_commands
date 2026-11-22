"""Check repaired and formatted trees are reused by the lint cache."""

from __future__ import annotations

import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.test_verify_lint_judgment import VerifyLintJudgmentFixture
from scripts.lib import conf


class VerifyLintScopeTests(VerifyLintJudgmentFixture):
    def test_cached_lint_uses_no_slot_at_limit_zero(self) -> None:
        cache = self.dirty_cache()
        fixture = caller_admission(self, self.root, self.environment)
        requests: list[wire.VerifyRunSlotRequest] = []
        handle = fixture.server.handle_request

        def record(request: wire.Request, connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                   ) -> list[wire.Reply]:
            if isinstance(request, wire.VerifyRunSlotRequest):
                requests.append(request)
            return handle(request, connection)

        with mock.patch.object(fixture.server, "handle_request", side_effect=record):
            _ = self.verify("lint", "sample")
            self.assertEqual(len(list(cache.glob("*.pass"))), 1)
            self.assertEqual(len(requests), 1)
            repository = requests.pop().repository
            config = self.root / "build_admission.conf"
            _ = config.write_text(config.read_text() + f"\nmax_runs.{conf.host_key_name(fixture.server.host)}.{repository}=0\n")

            result = self.verify("lint", "sample")
            self.assertIn("PASS (recorded)", result.stdout)
            self.assertFalse(requests)

    def test_fixed_and_formatted_tree_is_cached(self) -> None:
        cache = self.dirty_cache()
        formatted = "pub fn value() -> u8 {\n    2\n}\n"
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101",
            "TEST_FIX_SOURCE": "pub fn value()->u8{2}\n",
            "TEST_FMT_SOURCE": formatted,
            "TEST_CLIPPY_EXPECT_SOURCE": formatted,
        })
        result = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls(fix_passes=2)
        self.assertEqual((self.root / "src/lib.rs").read_text(encoding="utf-8"), formatted)
        self.assertNotIn("the tree changed while lint ran", result.stdout)
        self.assertEqual(len(list(cache.glob("*.pass"))), 1)

        result = self.verify("lint", "sample")
        self.assertIn("PASS (recorded)", result.stdout)
        self.assertFalse(self.clippy_ran())


if __name__ == "__main__":
    _ = unittest.main()
