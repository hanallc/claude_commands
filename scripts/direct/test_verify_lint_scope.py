"""Check workspace and package lint commands through the real shell pipeline."""

from __future__ import annotations

import unittest

from scripts.direct import verify_lint_scope_test_support as support
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB as CARGO_STUB
from scripts.lib import conf


def assert_clippy_calls(fixture: support.VerifyLintScopeFixture, *, fix_passes: int = 1) -> None:
    judgment = ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"]
    expected = [judgment]
    if fix_passes:
        jobs = conf.read(fixture.config).whole("clippy_fix_jobs")
        fix = ["--jobs", str(jobs), "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs", *judgment]
        expected.extend([fix] * fix_passes)
        expected.append(judgment)
    fixture.assertEqual(fixture.calls("clippy"), expected)


class VerifyLintScopeTests(support.VerifyLintScopeFixture):
    def test_cache_reads_build_info_again_only_for_publication(self) -> None:
        _ = self.enable_cache()
        events = self.root / "version-events"
        self.environment["TEST_VERSION_EVENTS"] = str(events)
        for build_id, recorded in (("before", False), ("before", True), ("after", False)):
            with self.subTest(build_id=build_id, recorded=recorded):
                events.unlink(missing_ok=True)
                support.write_fixture_cargo_mend(self.root / "bin/cargo-mend", build_id)
                result = self.verify("lint", "sample")
                self.assertEqual("(recorded)" in result.stdout, recorded)
                expected = ["MEND", "TAPLO", "RUSTFMT", "CLIPPY"]
                if not recorded:
                    expected.append("MEND")
                self.assertEqual(events.read_text().splitlines(), expected)
                self.assertEqual(self.clippy_ran(), not recorded)

    def test_uncached_lint_reads_only_the_formatter_availability_version(self) -> None:
        events = self.root / "version-events"
        self.environment["TEST_VERSION_EVENTS"] = str(events)
        _ = self.verify("lint", "sample")
        self.assertEqual(events.read_text().splitlines(), ["RUSTFMT"])

    def test_clean_tree_runs_one_judgment_without_repairs_or_rustdoc(self) -> None:
        result = self.verify("lint")
        self.assertEqual(self.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings",
        ]])
        self.assertEqual(self.lint_stages(), [
            ["+nightly", "fmt", "--all"],
            ["clippy", *self.calls("clippy")[0]],
        ])
        self.assertIn("style-lint: clippy start", result.stdout.splitlines())
        self.assertIn("style-lint: clippy passed", result.stdout.splitlines())

    def test_lint_matches_ci_and_preserves_other_stages(self) -> None:
        self.environment["TEST_CLIPPY_STATUSES"] = "[101, 0]"
        _ = self.verify("lint", "sample")
        self.assert_workspace_taplo_check()
        assert_clippy_calls(self)
        self.assertEqual(self.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "-p", "sample"],
            ["fmt", "-p", "sample"],
        ])
        self.assertEqual(self.calls("doc"), [])

    def test_clippy_off_names_the_full_scope_without_running_it(self) -> None:
        self.write_config(clippy="off")
        result = self.verify("lint", "sample")
        skip_lines = [line for line in result.stdout.splitlines()
                      if line.startswith("SKIPPED:") and "clippy" in line]
        self.assertEqual(skip_lines, [
            "SKIPPED: cargo clippy --workspace --all-targets --all-features"
            + f" — clippy=off in {self.config}"
        ])
        self.assertEqual(self.calls("clippy"), [])
        self.assertEqual(len(self.calls("mend")), 1)
        self.assertEqual(self.calls("doc"), [])
        self.assert_workspace_taplo_check()



if __name__ == "__main__":
    _ = unittest.main()
