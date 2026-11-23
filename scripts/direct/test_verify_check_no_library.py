"""Check the Cargo selection for a binary-only workspace."""

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
        for has_manifest in (True, False):
            with self.subTest(has_manifest=has_manifest):
                if has_manifest:
                    result = self.verify("check", "sample")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.calls_made(), ["check --workspace --all-targets"])
                else:
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
                    self.assertEqual(result.stderr,
                        f"verify.sh: {self.root} has no Cargo.toml; verify.sh checks Cargo repositories only\n", details)
                    self.assertEqual(self.calls_made(), [])
                    self.assertEqual(self.metadata_calls.read_text().splitlines(), [])


if __name__ == "__main__":
    _ = unittest.main()
