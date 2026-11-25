"""Check Verify in a binary workspace and in repositories without Cargo."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from scripts.direct.verify_dev_features_test_support import DevFeatureFixture, VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyCheckNoLibraryTests(DevFeatureFixture):
    def test_binary_workspace_checks_all_targets(self) -> None:
        (self.root / "src/lib.rs").unlink()
        _ = (self.root / "src/main.rs").write_text("fn main() {}\n")
        result = self.verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_made(), ["check --workspace --all-targets"])

    def assert_rejects_missing_manifest(self, *extra_lines: str) -> None:
        (self.root / "Cargo.toml").unlink()
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("The repository fixture requires Git.")
        git = Path(self.environment["PATH"]) / "git"
        git.unlink()
        git.symlink_to(real_git)
        _ = subprocess.run(["git", "init", "--quiet"], cwd=self.root, env=self.environment,
            capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
        _ = self.calls.write_text("")
        _ = self.metadata_calls.write_text("")
        result = subprocess.run(["bash", str(VERIFY), "final"], cwd=self.root, env=self.environment,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        details = (result.stdout + result.stderr + "\ncargo calls: " + self.calls.read_text()
                   + "\nmetadata calls: " + self.metadata_calls.read_text())
        self.assertEqual(result.returncode, 2, details)
        self.assertEqual(result.stderr.splitlines(), [
            f"verify.sh: {self.root} has no Cargo.toml; verify.sh checks Cargo repositories only",
            *extra_lines,
        ], details)
        self.assertEqual(self.calls_made(), [])
        self.assertEqual(self.metadata_calls.read_text().splitlines(), [])

    def test_repository_without_cargo_manifest_gets_the_cargo_refusal(self) -> None:
        self.assert_rejects_missing_manifest()

    def test_claude_commands_checkout_gets_the_test_route(self) -> None:
        marker = self.root / "scripts/copies/copies.py"
        marker.parent.mkdir(parents=True)
        marker.touch()
        self.assert_rejects_missing_manifest(
            "claude_commands: From the worktree root, run ~/.claude/scripts/lib/py ~/.claude/scripts/copies/copies.py run --repo . --commit HEAD -- ./scripts/lib/py scripts/production/gate.py --affected")


if __name__ == "__main__":
    _ = unittest.main()
