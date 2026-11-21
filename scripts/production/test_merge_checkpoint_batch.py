"""Check that a failed batch reports each checkpoint."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Callable
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

    def interrupt_batch(self, *lines: str) -> str:
        self.executable(self.bin / "checkpoint-test", STUB + "raise KeyboardInterrupt\n")
        with self.assertRaises(KeyboardInterrupt):
            _ = self.land_batch(list(lines))
        self.executable(self.bin / "checkpoint-test", STUB)
        return self.git("rev-parse", "HEAD")

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
        interrupted = "the batch was interrupted; send the row again"
        cancelled = "merge test 1 cancelled because the tree changed"
        cancel_status = checkpoint_gate.TREE_CHANGED_STATUS
        for outcome, cause, own_tests, status in (
            ("red package", package, (True, False), 1),
            ("red full gate", full_gate, (True, True), 1),
            ("main conflicts with row 2", "origin/main conflicts: src/beta.txt", no_red, 1),
            ("staged merge resolution", "staged paths in merge checkout: src/beta.txt", no_red, 1),
            ("interrupted batch", interrupted, no_red, 1),
            ("first merge interrupted", interrupted, no_red, 1),
            ("crash before reports", interrupted, no_red, 1),
            ("pre-existing merge", "a merge was open before the batch", no_red, 1),
            ("cancelled test", cancelled, no_red, cancel_status),
            ("cancelled with tracked edit", cancelled, no_red, cancel_status),
            ("invalid land ledger", package, (True, False), 1),
            ("repeated checkpoint", "repeats a checkpoint in this batch", (False, False, False), 1),
            ("identical rows", "repeats a checkpoint in this batch", (False, False, False), 1),
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
        if outcome == "identical rows":
            rows.append(rows[0])
        lines = [self.batch_line(*row) for row in rows]
        with patch.dict("os.environ", self.env):
            land_queue.add("next item", batch="", commits=[next_commit], first=False, before=None)
        main_tip = start
        if outcome == "main conflicts with row 2":
            main_tip = self.push_main_change("src/beta.txt", "main change\n")

        check = merge_checkpoint.merge_checked_checkpoint

        def interrupt(request: merge_checkpoint.MergeRequest, *, merging: Callable[[], None],
                      merged: Callable[[str], None]) -> merge_checkpoint.CheckedCheckpointMerge:
            if outcome == "first merge interrupted":
                merging()
                _ = self.git("merge", "--no-commit", "--no-ff", alpha)
                raise KeyboardInterrupt
            if request.commit == beta:
                raise KeyboardInterrupt
            return check(request, merging=merging, merged=merged)

        if outcome in ("interrupted batch", "first merge interrupted"):
            with (patch.object(merge_checkpoint, "merge_checked_checkpoint", side_effect=interrupt),
                  self.assertRaises(KeyboardInterrupt)):
                _ = self.land_batch(lines)
            self.assertTrue(self.batch_record().is_file())
            if outcome == "first merge interrupted":
                self.assertEqual(self.git("rev-parse", "HEAD"), start)
                self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), alpha)
            else:
                self.assertEqual(self.git("rev-parse", "HEAD^2"), alpha)

        def stage_beta() -> None:
            _ = self.git("merge", "--no-commit", "--no-ff", beta)
            self.write(self.checkout, "src/beta.txt", "staged resolution\n")
            _ = self.git("add", "src/beta.txt")
            self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), beta)
            self.assertEqual(self.git("diff", "--cached", "--name-only"), "src/beta.txt")

        def stage_resolution(request: merge_checkpoint.MergeRequest, *, merging: Callable[[], None],
                             merged: Callable[[str], None]) -> merge_checkpoint.CheckedCheckpointMerge:
            if request.commit == beta:
                merging()
                stage_beta()
            return check(request, merging=merging, merged=merged)

        if outcome == "pre-existing merge":
            stage_beta()

        package_marker = self.state / "fail-always-test-alpha-proof"
        if outcome in ("red package", "crash before reports", "invalid land ledger"):
            package_marker.touch()
        if outcome == "red full gate":
            self.production_doc(merge_tests_line="- **Merge tests:** `merge_gate.py`")
            self.executable(self.bin / "merge_gate.py", STUB + 'print("--- hooks/test_first.py exit=1 ---")\nraise SystemExit(1)\n')
        cancelled = status == checkpoint_gate.TREE_CHANGED_STATUS
        blocked = outcome == "cancelled with tracked edit"
        if cancelled:
            edit = 'Path("src/beta.txt").write_text("local edit\\n")\n' if blocked else ""
            self.executable(self.bin / "checkpoint-test", STUB + edit + f"raise SystemExit({status})\n")
        with patch.dict("os.environ", self.env):
            ledger = land_runs.results_path()
        if outcome == "invalid land ledger":
            ledger.parent.mkdir(parents=True, exist_ok=True)
            _ = ledger.write_bytes(b"\xff\n")

        report = merge_checkpoint.report_checkpoint_stop

        def report_after_reset(production: str, row: merge_checkpoint.CheckpointNotice, error: checkpoint_gate.Stop) -> int:
            head = self.git("rev-parse", "HEAD")
            if blocked:
                self.assertNotEqual(head, start)
            else:
                self.assertEqual(head, start)
            owned = outcome not in ("repeated checkpoint", "identical rows", "pre-existing merge")
            self.assertEqual(self.batch_record().exists(), owned)
            return report(production, row, error)

        def crash_after_reset(_production: str, _row: merge_checkpoint.CheckpointNotice, _error: checkpoint_gate.Stop) -> int:
            self.assertEqual(self.git("rev-parse", "HEAD"), start)
            self.assertTrue(self.batch_record().is_file())
            raise KeyboardInterrupt

        if outcome == "crash before reports":
            with (patch.object(merge_checkpoint, "report_checkpoint_stop", side_effect=crash_after_reset),
                  self.assertRaises(KeyboardInterrupt)):
                _ = self.land_batch(lines)
            self.assertFalse(ledger.exists())

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
        if outcome in ("repeated checkpoint", "identical rows"):
            repeated_phase = rows[-1][1]
            for notice in notices:
                self.assertIn(f"alpha phase {repeated_phase} ({alpha[:12]}) repeats a checkpoint in this batch", notice)
            self.assertNotIn("resume it alone", notices[0])
            self.assertNotIn("resume it alone", notices[1])
            self.assertIn("resume it alone", notices[2])
        if outcome == "invalid land ledger":
            self.assertEqual(result.stdout.count("land result: not recorded"), len(rows))
            self.assertEqual(ledger.read_bytes(), b"\xff\n")
        package_runs = [["test", "alpha-proof"]] if any(own_tests) or cancelled or outcome == "crash before reports" else []
        self.assertEqual(self.events("verify.sh"), package_runs)
        self.assertEqual(self.events("checkpoint-test"), [[]] if cancelled else [])
        self.assertEqual(self.events("merge_gate.py"), [[]] if outcome == "red full gate" else [])
        if blocked:
            command = shlex.join(("git", "-C", str(self.checkout), "reset", "--keep", start))
            for message in (result.stdout, *notices):
                self.assertIn("preserve the edits in src/beta.txt", message)
                self.assertIn(command, message)
                self.assertIn("branch --show-current", message)
                self.assertIn("rev-parse HEAD", message)
                self.assertIn("ls-remote --exit-code origin", message)
                self.assertIn("merge-base --is-ancestor", message)
                self.assertIn("--git-path MERGE_HEAD", message)
            self.assertTrue(self.batch_record().is_file())
            self.assertEqual((self.checkout / "src/beta.txt").read_text(), "local edit\n")
            retained = self.root / "retained-edit"
            _ = shutil.copyfile(self.checkout / "src/beta.txt", retained)
            _ = self.git("restore", "src/beta.txt")
            retry = self.land_batch(lines)
            self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
            self.assertEqual(retry.stdout.count("the batch was interrupted; send the row again"), len(rows))
            self.assertEqual(retained.read_text(), "local edit\n")
            self.assertEqual(self.events("checkpoint-test"), [[]])
        elif outcome == "pre-existing merge":
            self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), beta)
            self.assertEqual(self.git("show", ":src/beta.txt"), "staged resolution")
            self.assertEqual((self.checkout / "src/beta.txt").read_text(), "staged resolution\n")
            _ = self.git("merge", "--abort")
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

    def test_interrupted_batch_uses_only_recorded_ownership(self) -> None:
        for case in (
            "same start with unrelated merge", "unrelated HEAD", "only alpha published", "origin after alpha",
            "external alpha merge", "origin past both rows", "new push policy", "fetch failure",
            "legacy record", "legacy record without start",
        ):
            with self.subTest(case=case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.check_interrupted_ownership(case)

    def check_interrupted_ownership(self, case: str) -> None:
        if case == "new push policy":
            self.push = "validate_and_push"
            self.production_doc()
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint(content="alpha change\n")
        beta = self.beta_checkpoint("src/beta.txt", "beta change\n")
        flags = ("--cancel-prior",) if case == "new push policy" else ()
        lines = [self.batch_line("alpha", "2", alpha, *flags), self.batch_line("beta", "3", beta)]
        beta_merge = self.interrupt_batch(*lines)
        alpha_merge = self.git("rev-parse", "HEAD^1")
        path = self.batch_record()
        saved = path.read_bytes()
        self.assertEqual(self.git("rev-parse", f"{alpha_merge}^2"), alpha)
        self.assertEqual(self.git("rev-parse", f"{beta_merge}^2"), beta)

        published: dict[str, str] = {}
        expected_head = start
        moved = False
        origin = start
        if case == "same start with unrelated merge":
            _ = self.git("reset", "--keep", start)
            _ = self.git("merge", "--no-commit", "--no-ff", beta)
            self.write(self.checkout, "src/beta.txt", "unrelated staged edit\n")
            _ = self.git("add", "src/beta.txt")
            moved = True
        elif case == "unrelated HEAD":
            self.write(self.checkout, "unrelated.txt", "unrelated commit\n")
            _ = self.git("add", "unrelated.txt")
            _ = self.git("commit", "-m", "Unrelated commit")
            expected_head = self.git("rev-parse", "HEAD")
            moved = True
        elif case in ("only alpha published", "origin after alpha", "origin past both rows"):
            published["alpha"] = alpha_merge
            origin = alpha_merge
            expected_head = alpha_merge
            if case == "origin past both rows":
                published["beta"] = beta_merge
                origin = beta_merge
            _ = self.git("push", "origin", f"{origin}:production")
            if case != "only alpha published":
                main = self.push_main_change("src/later.txt", "later main change\n")
                _ = self.git("fetch", "origin", "main", "production", cwd=self.other)
                _ = self.git("switch", "--detach", origin, cwd=self.other)
                _ = self.git("merge", "--no-ff", "-m", "Merge later origin/main", main, cwd=self.other)
                origin = self.git("rev-parse", "HEAD", cwd=self.other)
                _ = self.git("push", "origin", "HEAD:production", cwd=self.other)
                expected_head = beta_merge
        elif case == "external alpha merge":
            _ = self.git("fetch", "origin", "alpha-branch", cwd=self.other)
            _ = self.git("switch", "--detach", start, cwd=self.other)
            _ = self.git("merge", "--no-ff", "-m", "Merge alpha in another checkout", alpha, cwd=self.other)
            origin = self.git("rev-parse", "HEAD", cwd=self.other)
            self.assertNotEqual(origin, alpha_merge)
            self.assertEqual(self.git("rev-parse", "HEAD^2", cwd=self.other), alpha)
            _ = self.git("push", "origin", "HEAD:production", cwd=self.other)
            expected_head = beta_merge
        elif case == "new push policy":
            self.push = "git"
            self.production_doc()
        elif case in ("legacy record", "legacy record without start"):
            prefix = f"start-tip\t{start}\n" if case == "legacy record" else ""
            _ = path.write_text(f"{prefix}{alpha}\t{lines[0]}\n{beta}\t{lines[1]}\n", encoding="utf-8")
            expected_head = beta_merge
        elif case == "fetch failure":
            _ = self.git("remote", "set-url", "origin", str(self.root / "missing-origin"))
            lines = [self.batch_line("alpha", "4", alpha), self.batch_line("beta", "5", beta)]
            expected_head = beta_merge

        before = self.git("status", "--porcelain")
        result = self.land_batch(lines)
        expected_status = 0 if case == "origin past both rows" else 1
        self.assertEqual(result.returncode, expected_status, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual(len(notices), 2, result.stdout)
        cause = ("an interrupted batch waits for a fetch; send the row again" if case == "fetch failure"
                 else "the batch was interrupted; send the row again")
        phases = ("4", "5") if case == "fetch failure" else ("2", "3")
        for notice, unit, phase, commit in zip(notices, ("alpha", "beta"), phases, (alpha, beta), strict=True):
            self.assertTrue(notice.startswith(f"send {unit}: phase {phase} "), notice)
            if unit in published:
                self.assertIn(f"merged and pushed as {published[unit]};", notice)
                self.assertIn(f"{unit} phase {phase} ({commit}) merged as {published[unit]};", self.log.read_text())
            else:
                self.assertIn(f"({commit[:12]})", notice)
                self.assertIn(cause, notice)
        if moved:
            self.assertIn("the checkout moved after the interruption", result.stdout)
        if case in ("origin after alpha", "external alpha merge"):
            self.assertIn("the fetched tip is outside the rollback target", result.stdout)
        if case.startswith("legacy record"):
            self.assertIn("the prior batch record has no merge IDs", result.stdout)
        if case == "same start with unrelated merge":
            self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), beta)
            self.assertEqual(self.git("show", ":src/beta.txt"), "unrelated staged edit")
        if case == "fetch failure":
            self.assertEqual(path.read_bytes(), saved)
        else:
            self.assertFalse(path.exists())
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), expected_head)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), origin)
        self.assertEqual(self.events("checkpoint-test"), [[]])
        self.assertEqual([(row["merge_commit"], row["source_checkpoint"]) for row in self.receipts()],
                         [(published[unit], commit) for unit, commit in (("alpha", alpha), ("beta", beta))
                          if unit in published])

    def test_single_checkpoint_succeeds_when_the_completion_marker_fails(self) -> None:
        alpha = self.checkpoint(content="alpha change\n")
        touch = Path.touch

        def fail_marker(path: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
            if path.name == "promotion":
                raise PermissionError("fixture cannot write the completion marker")
            touch(path, mode=mode, exist_ok=exist_ok)

        with patch.object(Path, "touch", fail_marker):
            result = self.run_checkpoint(alpha).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "production", cwd=self.origin)
        success = f"send alpha: phase 2 merged and pushed as {tip}; continue with the next step"
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("send ")], [success])
        self.assertIn("fixture cannot write the completion marker", result.stdout)
        with patch.dict("os.environ", self.env):
            landed = land_runs.last_results()[alpha]
        self.assertTrue(landed["landed"])
        self.assertEqual(self.log.read_text().count(f"({alpha}) merged as {tip}"), 1)

        resumed = self.run_checkpoint(alpha).process
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual([line for line in resumed.stdout.splitlines() if line.startswith("send ")], [success])
        self.assertEqual(self.log.read_text().count(f"({alpha}) merged as {tip}"), 1)


if __name__ == "__main__":
    _ = unittest.main()
