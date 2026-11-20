"""Check changing trees stop after the lint repair pass limit."""

from __future__ import annotations

import unittest

from scripts.direct.test_verify_lint_judgment import VerifyLintJudgmentFixture


class VerifyLintScopeTests(VerifyLintJudgmentFixture):
    def test_unchanged_tree_stops_after_one_fix_when_mend_identity_is_unavailable(self) -> None:
        cache = self.dirty_cache()
        (self.root / "bin/cargo-mend").unlink()
        self.environment.update({"TEST_CLIPPY_FIRST_STATUS": "101", "TEST_CLIPPY_STATUS": "101"})

        result = self.invoke_verify("lint", "sample", cwd=self.root)

        self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
        self.assert_fix_and_judgment_calls()
        self.assertEqual(list(cache.glob("*.fail")), [])

    def test_changing_tree_stops_after_three_fix_passes_and_failed_lint_runs_again(self) -> None:
        cache = self.enable_cache()
        self.environment.update({
            "TEST_CLIPPY_FIRST_STATUS": "101",
            "TEST_FIX_FILE": "src/fix-progress.rs",
            "TEST_GIT_UNTRACKED": "src/fix-progress.rs",
            "TEST_FIX_APPEND": "// fix pass\n",
            "TEST_CLIPPY_STATUS": "101",
        })
        for call in (1, 2):
            with self.subTest(call=call):
                result = self.invoke_verify("lint", "sample", cwd=self.root)
                self.assertEqual(result.returncode, 101, result.stdout + result.stderr)
                self.assertNotIn("FAIL (recorded)", result.stdout)
                self.assert_fix_and_judgment_calls(fix_passes=3)
                self.assertEqual(list(cache.glob("*.fail")), [])
                self.assertEqual(self.calls("doc"), [])
                self.assertEqual((self.root / "src/fix-progress.rs").read_text(encoding="utf-8"),
                                 "// fix pass\n" * (3 * call))


if __name__ == "__main__":
    _ = unittest.main()
