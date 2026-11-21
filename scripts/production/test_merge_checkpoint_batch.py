"""Check that a failed batch reports each checkpoint."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import final
import unittest
from unittest.mock import patch

from scripts.production import checkpoint_gate, land_queue, land_runs, merge_checkpoint

from scripts.production.merge_checkpoint_test_support import (
    DELIVERS, MergeCheckpointFixture, STARTED, STUB, TRIAL, configure_test_environment,
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

    def batch_record(self) -> Path:
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "checkpoint-batches/production"))

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
    def test_batch_stop_restores_the_tip_and_reports_each_row(self) -> None:
        package = "package alpha-proof"
        full_gate = "merge test 1 (hooks/test_first.py)"
        no_red = (False, False)
        for outcome, cause, own_tests, status in (
            ("red package", package, (True, False), 1),
            ("red full gate", full_gate, (True, True), 1),
            ("main conflicts with row 2", "origin/main conflicts: src/beta.txt", no_red, 1),
            ("staged merge resolution", "staged paths in merge checkout: src/beta.txt", no_red, 1),
            ("interrupted batch", package, (True, False), 1),
            ("cancelled test", "merge test 1 cancelled because the tree changed", no_red,
             checkpoint_gate.TREE_CHANGED_STATUS),
            ("invalid land ledger", package, (True, False), 1),
            ("repeated checkpoint", "repeats a checkpoint in this batch", (False, False, False), 1),
        ):
            with self.subTest(outcome=outcome):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.check_batch_stop(outcome, cause, own_tests, status)

    def check_batch_stop(self, outcome: str, cause: str, own_tests: tuple[bool, ...], status: int) -> None:
        self.env["MERGE_GATE_STATE_DIR"] = str(self.root / "gate-state")
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
        beta = self.beta_checkpoint("src/beta.txt", "beta change\n")
        next_commit = self.checkpoint("src/next.txt", "next item\n")
        rows = [("alpha", "2", alpha), ("beta", "2", beta)]
        if outcome == "repeated checkpoint":
            rows.append(("alpha", "3", alpha))
        lines = [self.batch_line(*row) for row in rows]
        with patch.dict("os.environ", self.env):
            land_queue.add("next item", batch="", commits=[next_commit], first=False, before=None)
        main_tip = start
        if outcome == "main conflicts with row 2":
            main_tip = self.push_main_change("src/beta.txt", "main change\n")

        check = merge_checkpoint.merge_checked_checkpoint

        def interrupt(request: merge_checkpoint.MergeRequest) -> merge_checkpoint.CheckedCheckpointMerge:
            if request.commit == beta:
                raise KeyboardInterrupt
            return check(request)

        if outcome == "interrupted batch":
            with (patch.object(merge_checkpoint, "merge_checked_checkpoint", side_effect=interrupt),
                  self.assertRaises(KeyboardInterrupt)):
                _ = self.land_batch(lines)
            self.assertTrue(self.batch_record().is_file())
            self.assertEqual(self.git("rev-parse", "HEAD^2"), alpha)

        def stage_resolution(request: merge_checkpoint.MergeRequest) -> merge_checkpoint.CheckedCheckpointMerge:
            if request.commit == beta:
                _ = self.git("merge", "--no-commit", "--no-ff", beta)
                self.write(self.checkout, "src/beta.txt", "staged resolution\n")
                _ = self.git("add", "src/beta.txt")
                self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), beta)
                self.assertEqual(self.git("diff", "--cached", "--name-only"), "src/beta.txt")
            return check(request)

        package_marker = self.state / "fail-always-test-alpha-proof"
        if outcome in ("red package", "interrupted batch", "invalid land ledger"):
            package_marker.touch()
        if outcome == "red full gate":
            self.production_doc(merge_tests_line="- **Merge tests:** `merge_gate.py`")
            self.executable(self.bin / "merge_gate.py", STUB + 'print("--- hooks/test_first.py exit=1 ---")\nraise SystemExit(1)\n')
        if outcome == "cancelled test":
            self.executable(self.bin / "checkpoint-test", STUB + f"raise SystemExit({status})\n")
        with patch.dict("os.environ", self.env):
            ledger = land_runs.results_path()
        if outcome == "invalid land ledger":
            ledger.parent.mkdir(parents=True, exist_ok=True)
            _ = ledger.write_bytes(b"\xff\n")

        report = merge_checkpoint.report_checkpoint_stop

        def report_after_reset(request: merge_checkpoint.MergeRequest, error: checkpoint_gate.Stop) -> int:
            self.assertEqual(self.git("rev-parse", "HEAD"), start)
            self.assertFalse(self.batch_record().exists())
            return report(request, error)

        with ExitStack() as fixtures:
            if outcome == "staged merge resolution":
                _ = fixtures.enter_context(patch.object(merge_checkpoint, "merge_checked_checkpoint", side_effect=stage_resolution))
            _ = fixtures.enter_context(patch.object(merge_checkpoint, "report_checkpoint_stop", side_effect=report_after_reset))
            result = self.land_batch(lines)

        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual(len(notices), len(rows), result.stdout)
        for notice, (unit, phase, commit), selects_red in zip(notices, rows, own_tests, strict=True):
            self.assertTrue(notice.startswith(f"send {unit}: phase {phase} ({commit[:12]}): "), notice)
            self.assertIn(cause, notice)
            match = "a red step" if selects_red else "no reported red step"
            self.assertIn(f"this row's tests include {match}", notice)
            if any(own_tests) or status == checkpoint_gate.TREE_CHANGED_STATUS:
                self.assertIn(f"test log: {self.state / f'merge_{alpha[:7]}_test.log'}", notice)
        if outcome == "repeated checkpoint":
            for notice in notices:
                self.assertIn(f"alpha phase 3 ({alpha[:12]}) repeats a checkpoint in this batch", notice)
            self.assertNotIn("resume it alone", notices[1])
            self.assertIn("resume it alone", notices[2])
        if outcome == "invalid land ledger":
            self.assertEqual(result.stdout.count("land result: not recorded"), len(rows))
            self.assertEqual(ledger.read_bytes(), b"\xff\n")
        package_runs = [["test", "alpha-proof"]] if any(own_tests) or status == checkpoint_gate.TREE_CHANGED_STATUS else []
        self.assertEqual(self.events("verify.sh"), package_runs)
        self.assertEqual(self.events("checkpoint-test"), [[]] if status == checkpoint_gate.TREE_CHANGED_STATUS else [])
        self.assertEqual(self.events("merge_gate.py"), [[]] if outcome == "red full gate" else [])
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_tip)
        self.assertEqual(self.receipts(), [])
        self.assertFalse((self.state / "pushed-refs").exists())
        self.assertFalse(self.batch_record().exists())

        package_marker.unlink(missing_ok=True)
        self.executable(self.bin / "checkpoint-test", STUB)
        self.production_doc()
        with patch.dict("os.environ", self.env):
            self.assertEqual(land_queue.take_next(), "next item")
        next_result = self.land_batch([self.batch_line("alpha", "4", next_commit)])
        self.assertEqual(next_result.returncode, 0, next_result.stdout + next_result.stderr)
        self.assertIn("send alpha: phase 4 merged and pushed", next_result.stdout)
        merge = "production^2" if main_tip == start else "production^1^2"
        self.assertEqual(self.git("rev-parse", merge, cwd=self.origin), next_commit)


if __name__ == "__main__":
    _ = unittest.main()
