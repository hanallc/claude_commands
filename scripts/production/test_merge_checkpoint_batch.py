"""Check that a failed batch reports each checkpoint."""

from __future__ import annotations

import shlex
import subprocess
from typing import final

from scripts.production.merge_checkpoint_test_support import (
    DELIVERS, MergeCheckpointFixture, STARTED, TRIAL, configure_test_environment,
)


def setUpModule() -> None:
    configure_test_environment()


class BatchFixture(MergeCheckpointFixture):
    def batch_line(self, unit: str, phase: str, checkpoint: str, *flags: str) -> str:
        code_flags = [] if "--shrink" in flags or "--as-built" in flags else ["--delivers", DELIVERS]
        return shlex.join([unit, phase, checkpoint, "--started", STARTED,
                           "--review-trial", TRIAL, *code_flags, "--scratch", str(self.state), *flags])

    def beta_checkpoint(self, relative: str, content: str, *, visible: bool = False) -> str:
        self.write(self.other, relative, content)
        _ = self.git("add", relative, cwd=self.other)
        review = "Beta changes | Open beta | The beta change appears | image 012345abcdef" if visible else "none — fixture code only"
        _ = self.git("commit", "-m", "checkpoint(beta): batch change\n\nChange-Review: " + review, cwd=self.other)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("push", "origin", "beta-branch", cwd=self.other)
        return checkpoint

    def land_batch(self, lines: list[str]) -> subprocess.CompletedProcess[str]:
        batch = self.root / "batch.txt"
        _ = batch.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.executable(self.origin / "hooks/post-receive", '''#!/usr/bin/env python3
from pathlib import Path
import sys
with Path(''' + repr(str(self.state / "pushed-refs")) + ''').open("a") as output:
    output.write(sys.stdin.read())
''')
        return self.invoke(["--production", "example", "--batch", str(batch)])


@final
class MergeCheckpointBatchTests(BatchFixture):
    def test_red_batch_stops_and_reports_every_row(self) -> None:
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
        beta = self.beta_checkpoint("src/beta.txt", "beta change\n")
        (self.state / "fail-always-test-alpha-proof").touch()

        result = self.land_batch([self.batch_line("alpha", "2", alpha), self.batch_line("beta", "2", beta)])

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual([line.split(":", 1)[0] for line in notices], ["send alpha", "send beta"], result.stdout)
        self.assertIn(f"phase 2 ({alpha[:12]}): this row's tests include a red step; repair package alpha-proof",
                      notices[0])
        self.assertIn(f"phase 2 ({beta[:12]}): this row's tests include no reported red step; package alpha-proof failed",
                      notices[1])
        test_log = self.state / f"merge_{alpha[:7]}_test.log"
        for notice in notices:
            self.assertIn(f"test log: {test_log}", notice)
        self.assertEqual(self.events("verify.sh"), [["test", "alpha-proof"]])
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertEqual(self.git("status", "--porcelain"), "")
        for branch in ("production", "main"):
            self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), start)
        self.assertEqual(self.receipts(), [])
