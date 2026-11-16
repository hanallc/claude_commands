#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""lint mend-fix: mend fixes the copy, then clippy judges the fixed copy."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from scripts.lib import test_hang_guard

LINT = Path(__file__).with_name("lint")

# Each stub records its words; MEND_STATUS and CLIPPY_STATUS set the answers.
STUBS = r'''
resolve_scope() { SCOPE=(); }
invoke_mend() { printf 'mend %s\n' "$*"; return "${MEND_STATUS:-0}"; }
invoke_clippy() { printf 'clippy %s\n' "$*"; return "${CLIPPY_STATUS:-0}"; }
'''


def mend_fix_function() -> str:
    """Read the real mend_fix body from the lint script."""
    body = LINT.read_text().split("\nmend_fix() {", 1)[1].split("\n}\n", 1)[0]
    return "mend_fix() {" + body + "\n}\n"


def run_mend_fix(*words: str, mend: int = 0, clippy: int = 0) -> subprocess.CompletedProcess[str]:
    script = "set -euo pipefail\n" + STUBS + mend_fix_function() + 'mend_fix "$@"\necho finished\n'
    return subprocess.run(
        ["bash", "-c", script, "lint", *words], capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "MEND_STATUS": str(mend), "CLIPPY_STATUS": str(clippy)},
        timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
    )


class MendFixTests(unittest.TestCase):
    def test_clippy_judges_the_fixed_copy(self) -> None:
        result = run_mend_fix("--workspace", "--features", "sample/extra")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "mend --fix --fail-on-warn --workspace --features sample/extra",
            "clippy --workspace --all-targets --all-features",
            "finished",
        ])

    def test_joined_features_word_stays_off_clippy(self) -> None:
        result = run_mend_fix("--workspace", "--features=sample/extra")
        self.assertIn("clippy --workspace --all-targets --all-features", result.stdout.splitlines())

    def test_remaining_mend_finding_fails_before_clippy(self) -> None:
        result = run_mend_fix("--workspace", mend=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines(), ["mend --fix --fail-on-warn --workspace"])

    def test_clippy_finding_fails_the_pass(self) -> None:
        result = run_mend_fix("--workspace", clippy=101)
        self.assertEqual(result.returncode, 101)
        self.assertNotIn("finished", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
