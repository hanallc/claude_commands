"""Check formatting and judgment after a failed clippy repair."""

from __future__ import annotations

import unittest
from scripts.direct.test_verify_lint_judgment import VerifyLintJudgmentFixture


class VerifyLintScopeTests(VerifyLintJudgmentFixture):
    def test_clippy_repair_tooling_failure_stops_before_the_next_judgment(self) -> None:
        cache = self.enable_cache()
        self.environment.update(TEST_CLIPPY_FIRST_STATUS="101", TEST_CLIPPY_FIX_STATUS="3")
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                first, repair = self.calls("clippy")
                self.assertNotIn("--fix", first)
                self.assertIn("--fix", repair)
                self.assertEqual(list(cache.glob("*.fail")), [])

    def test_ordinary_cargo_101_judgment_is_cached(self) -> None:
        cache = self.enable_cache()
        self.environment.update(TEST_CLIPPY_FIRST_STATUS="101", TEST_CLIPPY_STATUS="101")
        first = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(first.returncode, 101, first.stdout + first.stderr)
        self.assert_fix_and_judgment_calls()
        self.assertEqual(len(list(cache.glob("*.fail"))), 1)

        recorded = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(recorded.returncode, 101, recorded.stdout + recorded.stderr)
        self.assertIn("FAIL (recorded)", recorded.stdout)
        self.assertEqual(self.calls("clippy"), [])

    def test_failed_fix_still_formats_and_returns_judgment_status(self) -> None:
        self.environment["TEST_CLIPPY_FIRST_STATUS"] = "101"
        self.environment["TEST_CLIPPY_FIX_STATUS"] = "41"
        for judgment_status in (0, 43):
            with self.subTest(judgment_status=judgment_status):
                self.environment["TEST_CLIPPY_STATUSES"] = f"[101, {judgment_status}]"
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, judgment_status, result.stdout + result.stderr)
                self.assert_fix_and_judgment_calls()
                stages = self.lint_stages()
                self.assertEqual(stages, [
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[0]],
                    ["clippy", *self.calls("clippy")[1]],
                    ["+nightly", "fmt", "-p", "sample"],
                    ["clippy", *self.calls("clippy")[2]],
                ])
                self.assertEqual(self.calls("doc"), [])


if __name__ == "__main__":
    _ = unittest.main()
