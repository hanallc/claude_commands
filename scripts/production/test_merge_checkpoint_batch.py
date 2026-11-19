"""Land ordered checkpoint batches against private Git repositories."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
from typing import final
from unittest.mock import patch

from scripts.production import merge_checkpoint, staff
from scripts.production.staff_test_support import recorded_showrunner, unit_membership
from scripts.lib import atomic_file
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.merge_checkpoint_test_support import (
    DELIVERS, MergeCheckpointFixture, STARTED, STUB, TRIAL, configure_test_environment,
)


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointBatchTests(MergeCheckpointFixture):
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

    def assert_landed(self, start: str, checkpoints: list[str]) -> list[str]:
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        merges = self.git("rev-list", "--reverse", "--first-parent", "--merges", start + ".." + tip).splitlines()
        self.assertEqual([self.git("rev-parse", merge + "^2") for merge in merges], checkpoints)
        self.assertEqual([line.split()[2] for line in (self.state / "pushed-refs").read_text().splitlines()],
                         ["refs/heads/production", "refs/heads/main"])
        receipts = self.receipts()
        self.assertEqual([(row["merge_commit"], row["source_checkpoint"]) for row in receipts],
                         list(zip(merges, checkpoints)))
        self.assertEqual(len(self.events("style_sync.py")), 1)
        return merges

    def test_two_code_items_share_tests_push_and_promotion(self) -> None:
        for outcome in ("shared list", "push failure", "separate lists", "second list fails",
                        "second directory sync fails", "unrelated branch renamed", "unpublished main integration"):
            with self.subTest(outcome=outcome):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.push = "git"
                self.promotions = str(self.promote)
                review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
                self.production_doc(change_review=review)
                beta_review = review
                beta_log = self.log
                if outcome in ("separate lists", "second list fails", "second directory sync fails"):
                    other_checkout = self.root / "other-production"
                    _ = self.git("init", "-b", "main", str(other_checkout))
                    other_doc = self.root / "other-production.md"
                    beta_review = review.with_name("change-review - other.md")
                    beta_log = other_checkout / "production.log"
                    _ = other_doc.write_text(f"- **Repository:** `{other_checkout}`\n"
                                           + f"- **Showrunner checkout:** `{other_checkout}`\n"
                                           + "- **Merge branch:** main\n- **Log:** production.log\n"
                                           + f"- **Change review:** `{beta_review}`\n")
                    owner = recorded_showrunner("other", other_doc)
                    beta = unit_membership("example", "beta")
                    _ = staff.move(beta.id, staff.ReportsTo(owner.id))
                    if outcome == "second list fails":
                        beta_review.parent.mkdir(parents=True, exist_ok=True)
                        _ = beta_review.write_text("# Incomplete list\n")
                start = self.git("rev-parse", "HEAD")
                if outcome == "unrelated branch renamed":
                    _ = self.git("update-ref", "refs/remotes/origin/topic", start)
                    _ = self.git("update-ref", "refs/heads/topic/rework", start, cwd=self.origin)
                if outcome == "unpublished main integration":
                    published = start
                    main = self.push_main_change("docs/main-fix.md", "Main fix\n")
                    _ = self.git("fetch", "--no-write-fetch-head", "--refmap=", "origin", "refs/heads/main")
                    _ = self.git("merge", "--no-ff", "-m", "Merge origin/main into production", main)
                    start = self.git("rev-parse", "HEAD")
                    self.assertEqual(self.git("rev-parse", "HEAD^2"), main)
                    self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), published)
                    record = self.checkout / self.git("rev-parse", "--git-path", "checkpoint-batches/production")
                    self.assertFalse(record.exists())
                first = self.checkpoint(subject="checkpoint(alpha): batch change\n\n"
                                        + "Change-Review: Alpha changes | Open alpha | The alpha change appears | image 012345abcdef")
                second = self.beta_checkpoint("src/beta.txt", "beta batch change\n", visible=True)
                if outcome == "push failure":
                    self.push = "validate_and_push"
                    self.production_doc(change_review=review)
                    self.executable(self.bin / "validate_and_push.sh", STUB + """
import subprocess
origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                        text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
subprocess.run(["git", "push", origin, "HEAD:refs/heads/production"],
               check=True, timeout=TEST_HANG_GUARD_SECS)
raise SystemExit(1)
""")
                sync_folder = atomic_file.sync_folder

                def sync(folder: Path) -> None:
                    if (folder == beta_review.parent and beta_review.is_file()
                            and "Beta changes" in beta_review.read_text()):
                        raise OSError("fixture directory sync failed")
                    sync_folder(folder)

                batch = [self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second)]
                if outcome == "second directory sync fails":
                    with patch.object(atomic_file, "sync_folder", side_effect=sync):
                        result = self.land_batch(batch)
                else:
                    result = self.land_batch(batch)
                if outcome == "push failure":
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    tip = self.git("rev-parse", "production", cwd=self.origin)
                    self.assertNotEqual(tip, start)
                    self.assertEqual(self.git("rev-parse", "HEAD"), tip)
                    self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), start)
                    self.assertEqual([self.git("rev-parse", "HEAD~1^2"), self.git("rev-parse", "HEAD^2")],
                                     [first, second])
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertEqual(len(self.events("validate_and_push.sh")), 1)
                    self.assertEqual([line.split()[2] for line in (self.state / "pushed-refs").read_text().splitlines()],
                                     ["refs/heads/production"])
                    self.assertNotIn("batch: retry", result.stdout)
                    for unit in ("alpha", "beta"):
                        self.assertIn(f"send {unit}: repair the push failure", result.stdout)
                    self.assertIn("push: failed", result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    merges = self.assert_landed(start, [first, second])
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    for unit, merge in zip(("alpha", "beta"), merges):
                        self.assertEqual(result.stdout.count(f"send {unit}: phase 2 merged and pushed as {merge};"), 1)
                    self.assertEqual([call[call.index("--unit") + 1] for call in self.events("review_regime.py")],
                                     ["alpha", "beta"])
                    self.assertIn("Alpha changes", review.read_text())
                    if outcome == "second list fails":
                        self.assertEqual(beta_review.read_text(), "# Incomplete list\n")
                        self.assertIn(f"1 row(s) added to {review}; | C1 | Alpha changes |", result.stdout)
                        self.assertIn(f"change review: failed — {beta_review} not written:", result.stdout)
                        self.assertNotIn(f"{review} not written", result.stdout)
                    else:
                        self.assertIn("Beta changes", beta_review.read_text())
                    self.assertIn("alpha phase 2", self.log.read_text())
                    self.assertIn("beta phase 2", beta_log.read_text())
                    if outcome == "second directory sync fails":
                        self.assertIn(f"change review: warning — {beta_review}: durability unconfirmed", result.stdout)
                        self.assertIn("fixture directory sync failed", result.stdout)
                        self.assertNotIn(f"{beta_review} not written", result.stdout)
                    if outcome in ("separate lists", "second directory sync fails"):
                        lines = [line for line in result.stdout.splitlines() if "row(s) added to" in line]
                        self.assertEqual(len(lines), 2, result.stdout)
                        for path, change in ((review, "Alpha changes"), (beta_review, "Beta changes")):
                            self.assertTrue(any(f"1 row(s) added to {path}; | C1 | {change} |" in line
                                                for line in lines), lines)

    def test_rerun_resumes_promotion_of_merged_items_before_new_items_land(self) -> None:
        self.promotions = str(self.promote) + " — live checkout"
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        marker = self.root / "reject-main"
        _ = marker.touch()
        self.executable(self.origin / "hooks/pre-receive", "#!/bin/sh\nwhile read old new ref; do\n"
                        + f'  if [ "$ref" = "refs/heads/main" ] && [ -e "{marker}" ]; then exit 1; fi\n'
                        + "done\nexit 0\n")
        first = self.checkpoint(content="alpha batch change\n")
        second = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
        lines = [self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second)]
        failed = self.land_batch(lines)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn("promote: failed", failed.stdout)
        self.assertFalse(self.batch_record().exists())
        pushed = self.git("rev-parse", "production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), start)
        marker.unlink()
        third = self.checkpoint("src/third.txt", "later alpha change\n")
        rerun = self.land_batch([*lines, self.batch_line("alpha", "3", third)])
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertNotIn("resume it alone with the single command", rerun.stdout)
        self.assertEqual(rerun.stdout.count(f"already pushed at {pushed}"), 2)
        tip = self.git("rev-parse", "production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "HEAD^1"), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), third)

    def test_main_conflict_drops_only_the_conflicting_unit_before_clean_items_land(self) -> None:
        for main_moves_after_tests in (False, True):
            with self.subTest(main_moves_after_tests=main_moves_after_tests):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.promotions = str(self.promote)
                self.production_doc()
                start = self.git("rev-parse", "HEAD")
                first = self.checkpoint(content="clean alpha change\n")
                second = self.beta_checkpoint("src/beta.txt", "beta conflict\n")
                third = self.checkpoint("src/third.txt", "another clean alpha change\n")
                main_commit = self.push_main_change("src/beta.txt", "main conflict\n")
                if main_moves_after_tests:
                    _ = self.git("update-ref", "refs/heads/main", start, cwd=self.origin)
                    self.executable(self.bin / "checkpoint-test", STUB + f'''
import subprocess
subprocess.run(["git", "-C", {str(self.root / "main-fix-writer")!r}, "push", "origin", "main"],
               check=True, timeout=TEST_HANG_GUARD_SECS)
''')
                result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                          self.batch_line("alpha", "3", third)])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
                repair = [line for line in notices if "merge origin/main" in line]
                self.assertEqual(repair, ["send beta: merge origin/main into your own unit branch, "
                                          + "resolve src/beta.txt, and send a new checkpoint"])
                self.assertIn(f"beta phase 2 ({second[:12]}): origin/main conflicts: src/beta.txt", result.stdout)
                self.assertNotIn("merge origin/main into the merge branch", result.stdout)
                self.assertIn("send alpha: phase 2 merged", result.stdout)
                self.assertIn("send alpha: phase 3 merged", result.stdout)
                self.assertNotIn("send beta: phase 2 merged", result.stdout)
                self.assertEqual(result.stdout.count("batch: retry"), 1)
                tip = self.git("rev-parse", "HEAD")
                for branch in ("production", "main"):
                    self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), tip)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
                merges = self.git("rev-list", "--reverse", "--first-parent", "--merges", start + ".." + tip).splitlines()
                self.assertEqual([self.git("rev-parse", merge + "^2") for merge in merges], [first, third, main_commit])
                self.assertEqual([row["source_checkpoint"] for row in self.receipts()], [first, third, main_commit])
                self.assertEqual(self.git("show", "HEAD:src/beta.txt"), "main conflict")
                self.assertEqual(self.git("show", "HEAD:src/alpha.txt"), "clean alpha change")
                self.assertEqual(self.git("show", "HEAD:src/third.txt"), "another clean alpha change")
                self.assertEqual(self.git("status", "--porcelain"), "")

    def test_bisection_main_conflict_keeps_the_clean_unit_out_of_repair_notices(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        self.write(self.unit, "src/broken.txt", "shared test failure\n")
        _ = self.git("add", "src/broken.txt", cwd=self.unit)
        first = self.checkpoint(content="alpha conflicts with main\n")
        second = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
        third = self.checkpoint(content="main resolution\n")
        main_commit = self.push_main_change("src/alpha.txt", "main resolution\n")
        self.executable(self.bin / "checkpoint-test", STUB + '''
raise SystemExit(1 if Path("src/broken.txt").is_file() else 0)
''')
        result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                  self.batch_line("alpha", "3", third)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual([line for line in notices if line.startswith("send beta:")],
                         [line for line in notices if line.startswith("send beta: phase 2 merged and pushed")])
        self.assertEqual(sum(line.startswith("send beta:") for line in notices), 1)
        self.assertIn("send alpha: merge origin/main into your own unit branch, resolve src/alpha.txt, "
                      + "and send a new checkpoint", result.stdout)
        self.assertIn(f"send alpha: resolve alpha phase 2 ({first[:12]}), then resend this checkpoint", result.stdout)
        self.assertNotIn("into the merge branch", result.stdout)
        self.assertEqual(result.stdout.count("batch: retry"), 1)
        # The base check adds one run of the red command.
        self.assertEqual(self.events("checkpoint-test"), [[], [], [], []])
        tip = self.git("rev-parse", "HEAD")
        for branch in ("production", "main"):
            self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), tip)
        merges = self.git("rev-list", "--reverse", "--first-parent", "--merges", start + ".." + tip).splitlines()
        self.assertEqual([self.git("rev-parse", merge + "^2") for merge in merges], [second, main_commit])
        self.assertEqual(self.git("show", "HEAD:src/beta.txt"), "clean beta change")
        self.assertEqual(self.git("show", "HEAD:src/alpha.txt"), "main resolution")
        self.assertEqual(self.git("ls-tree", "HEAD", "--", "src/broken.txt"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_main_conflict_in_existing_work_names_clean_items_as_able_to_land_alone(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        first = self.checkpoint(content="clean alpha change\n")
        second = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
        self.write(self.checkout, "existing.txt", "existing unpublished work\n")
        _ = self.git("add", "existing.txt")
        _ = self.git("commit", "-m", "existing unpublished work")
        start = self.git("rev-parse", "HEAD")
        published = self.git("rev-parse", "production", cwd=self.origin)
        main_commit = self.push_main_change("existing.txt", "main change\n")
        result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second)])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual(notices, [f"send {unit}: your checkpoint is clean against origin/main and can land alone; "
                                  + "the showrunner will inspect the combined merge conflict" for unit in ("alpha", "beta")])
        self.assertNotIn("merge origin/main into", result.stdout)
        self.assertNotIn("batch: retry", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), published)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_commit)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_conflicting_second_item_drops_before_the_remaining_items_retry(self) -> None:
        for preexisting_merge, held_case in ((False, ""), (True, ""), (False, "two holds"), (False, "later item")):
            with self.subTest(preexisting_merge=preexisting_merge, held_case=held_case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.promotions = str(self.promote)
                self.production_doc()
                start = self.git("rev-parse", "HEAD")
                if held_case:
                    if held_case == "two holds":
                        first = self.checkpoint(content="alpha clean item\n")
                        second = self.beta_checkpoint("src/beta.txt", "beta invalid shrink\n")
                        third = self.checkpoint("docs/wrong.md", "alpha invalid shrink\n")
                        lines = [self.batch_line("alpha", "2", first),
                                 self.batch_line("beta", "2", second, "--shrink"),
                                 self.batch_line("alpha", "3", third, "--shrink")]
                        landed, retries = [first], 2
                    else:
                        first = self.checkpoint("docs/wrong.md", "alpha invalid shrink\n")
                        second = self.checkpoint("src/later.txt", "alpha later item\n")
                        third = self.beta_checkpoint("src/beta.txt", "beta clean item\n")
                        lines = [self.batch_line("alpha", "2", first, "--shrink"),
                                 self.batch_line("alpha", "3", second), self.batch_line("beta", "2", third)]
                        landed, retries = [third], 1
                    result = self.land_batch(lines)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, landed)
                    self.assertEqual(result.stdout.count("batch: retry"), retries)
                    self.assertIn("scope: held", result.stdout)
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertNotIn("batch retry stopped", result.stdout)
                    if held_case == "two holds":
                        self.assertIn("send alpha: phase 2 merged", result.stdout)
                        self.assertNotIn("send beta: phase 2 merged", result.stdout)
                        self.assertNotIn("send alpha: phase 3 merged", result.stdout)
                        self.assertEqual(result.stdout.count("scope: held"), 2)
                    else:
                        self.assertIn(f"later item follows alpha phase 2 ({first[:12]})", result.stdout)
                        self.assertIn(f"send alpha: resolve alpha phase 2 ({first[:12]}), then resend this checkpoint",
                                      result.stdout)
                        self.assertIn("send beta: phase 2 merged", result.stdout)
                        self.assertNotIn("send alpha: phase 3 merged", result.stdout)
                        self.assertEqual(result.stdout.count("scope: held"), 2)
                    continue
                first = self.checkpoint(content="alpha batch side\n")
                second = self.beta_checkpoint("src/alpha.txt", "beta conflicting side\n")
                third = self.checkpoint("src/third.txt", "alpha third change\n")
                if preexisting_merge:
                    _ = self.git("merge", "--no-ff", "--no-commit", first)
                    self.write(self.checkout, "src/alpha.txt", "saved merge resolution\n")
                    _ = self.git("add", "src/alpha.txt")
                    saved = (self.git("rev-parse", "MERGE_HEAD"), self.git("write-tree"),
                             self.git("diff", "--cached", "--binary"))
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("scope: held", result.stdout)
                    self.assertEqual(self.git("rev-parse", "HEAD"), start)
                    self.assertEqual((self.git("rev-parse", "MERGE_HEAD"), self.git("write-tree"),
                                      self.git("diff", "--cached", "--binary")), saved)
                    self.assertEqual((self.checkout / "src/alpha.txt").read_text(), "saved merge resolution\n")
                    for branch in ("production", "main"):
                        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), start)
                    self.assertEqual(self.events("checkpoint-test"), [])
                    self.assertEqual(self.receipts(), [])
                else:
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertIn("conflicts: held", result.stdout)
                    self.assertIn("send beta:", result.stdout)
                    self.assertNotIn("send beta: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertEqual(self.git("show", "HEAD:src/alpha.txt"), "alpha batch side")
                    absent = subprocess.run(["git", "merge-base", "--is-ancestor", second, "HEAD"],
                                            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
                                            timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(absent.returncode, 1, absent.stdout + absent.stderr)

    def test_a_red_already_on_main_holds_every_item_without_a_bisect(self) -> None:
        _ = (self.state / "tree-fail-rows").write_text(json.dumps({"src/main-red.txt": "main_red"}), encoding="utf-8")
        self.production_doc(merge_tests_line="- **Merge tests:** `verify.sh test cargo-tile`")
        start = self.git("rev-parse", "HEAD")
        _ = self.push_main_change("src/main-red.txt", "red on main\n")
        first = self.checkpoint(content="alpha batch side\n")
        second = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
        result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second)])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("merge director: merge test 1 (main_red) red on base "), 1, result.stdout)
        for unit in ("alpha", "beta"):
            self.assertIn(f"send {unit}: your checkpoint adds no red", result.stdout)
        self.assertNotIn("repair merge test", result.stdout)
        self.assertNotIn("bisect:", result.stdout)
        self.assertEqual(result.stdout.count("batch: retry"), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
        self.assertFalse(self.batch_record().exists())

    def batch_record(self) -> Path:
        return Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "checkpoint-batches/production"))

    def interrupt_batch(self, *lines: str) -> str:
        self.executable(self.bin / "checkpoint-test", STUB + "raise KeyboardInterrupt\n")
        with self.assertRaises(KeyboardInterrupt):
            _ = self.land_batch(list(lines))
        self.executable(self.bin / "checkpoint-test", STUB)
        return self.git("rev-parse", "HEAD")

    def assert_batch_requires_reset(self, result: subprocess.CompletedProcess[str], published: str, unpublished: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("reset the merge branch to its pushed tip, then start a fresh batch", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), unpublished)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), published)

    def test_a_stopped_batch_refuses_a_change_to_shrink(self) -> None:
        published = self.git("rev-parse", "HEAD")
        commit = self.checkpoint(content="alpha batch change\n")
        unpublished = self.interrupt_batch(self.batch_line("alpha", "2", commit))
        result = self.land_batch([self.batch_line("alpha", "2", commit, "--shrink")])
        self.assert_batch_requires_reset(result, published, unpublished)

    def test_a_stopped_batch_refuses_a_moved_checkpoint_ref(self) -> None:
        published = self.git("rev-parse", "HEAD")
        first = self.checkpoint(content="alpha batch change\n")
        second = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
        ref = "refs/tags/beta-checkpoint"
        _ = self.git("update-ref", ref, second, cwd=self.other)
        lines = [self.batch_line("alpha", "2", first), self.batch_line("beta", "2", ref)]
        unpublished = self.interrupt_batch(*lines)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), second)
        self.assertEqual(self.git("rev-parse", "HEAD^1^2"), first)

        moved = self.beta_checkpoint("src/beta.txt", "later beta change\n")
        _ = self.git("update-ref", ref, moved, second, cwd=self.other)
        result = self.land_batch(lines)
        self.assert_batch_requires_reset(result, published, unpublished)
        self.assertIn("input: failed", result.stdout)

    def test_a_stopped_batch_refuses_a_moved_ref_for_an_item_that_has_not_merged(self) -> None:
        published = self.git("rev-parse", "HEAD")
        first = self.checkpoint(content="alpha batch change\n")
        _ = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
        ref = "refs/remotes/origin/beta-branch"
        _ = self.git("update-ref", "-d", ref)
        lines = [self.batch_line("alpha", "2", first), self.batch_line("beta", "2", ref)]
        merge = merge_checkpoint.merge

        def stop_after_first_merge(request: merge_checkpoint.MergeRequest) -> str:
            self.assertEqual(request.commit, first)
            _ = merge(request)
            raise KeyboardInterrupt

        with (patch.object(merge_checkpoint, "merge", side_effect=stop_after_first_merge),
              self.assertRaises(KeyboardInterrupt)):
            _ = self.land_batch(lines)
        unpublished = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^2"), first)

        _ = self.beta_checkpoint("src/beta.txt", "later beta change\n")
        result = self.land_batch(lines)
        self.assert_batch_requires_reset(result, published, unpublished)
        self.assertIn("input: failed", result.stdout)

    def test_a_stopped_batch_resumes_an_annotated_tag_object(self) -> None:
        commit = self.checkpoint(content="alpha batch change\n")
        _ = self.git("tag", "-a", "-m", "Checkpoint", "alpha-checkpoint", commit, cwd=self.unit)
        tag = self.git("rev-parse", "refs/tags/alpha-checkpoint", cwd=self.unit)
        self.assertNotEqual(tag, commit)
        line = self.batch_line("alpha", "2", tag)
        unpublished = self.interrupt_batch(line)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), commit)

        result = self.land_batch([line])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("batch: resume — alpha phase 2 is already merged", result.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), unpublished)

    def test_an_unresolvable_item_refuses_the_batch_before_any_merge(self) -> None:
        published = self.git("rev-parse", "HEAD")
        first = self.checkpoint(content="alpha batch change\n")
        missing = "refs/tags/absent-beta-checkpoint"
        lines = [self.batch_line("alpha", "2", first), self.batch_line("beta", "2", missing)]

        with patch.object(merge_checkpoint, "merge", wraps=merge_checkpoint.merge) as merge:
            result = self.land_batch(lines)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, rf"input: failed.*beta phase 2.*{missing}")
        merge.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), published)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), published)
        self.assertFalse(self.batch_record().exists())

    def test_a_stopped_batch_resumes_after_fetching_a_later_checkpoint(self) -> None:
        phase_refs = ("refs/remotes/origin/beta-phase", "origin/beta-phase")
        unit_revisions = ("remotes/origin/beta-branch", "refs/remotes/origin/beta-branch~1")
        for case in ("unit branch hash", "unit branch ref", "phase branch only", "unit branch missing",
                     *phase_refs, *unit_revisions):
            with self.subTest(case=case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                published = self.git("rev-parse", "HEAD")
                first = self.checkpoint(content="alpha batch change\n")
                _ = self.git("update-ref", "refs/heads/alpha-checkpoint", first, cwd=self.origin)
                _ = self.git("update-ref", "refs/heads/alpha-branch", published, cwd=self.origin)
                writer = self.root / "beta-writer"
                _ = self.git("clone", "-b", "beta-branch", str(self.origin), str(writer))
                self.write(writer, "src/beta.txt", "beta batch change\n")
                _ = self.git("add", "src/beta.txt", cwd=writer)
                _ = self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m",
                             "beta work\n\nChange-Review: none — fixture code only", cwd=writer)
                second = self.git("rev-parse", "HEAD", cwd=writer)
                if case == "refs/remotes/origin/beta-branch~1":
                    _ = self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                 "commit", "--allow-empty", "-m", "Later beta checkpoint", cwd=writer)
                phase_branch = case in ("phase branch only", "unit branch missing", *phase_refs)
                target = "HEAD:refs/heads/beta-phase" if phase_branch else "beta-branch"
                _ = self.git("push", "origin", target, cwd=writer)
                if case == "unit branch missing":
                    _ = self.git("update-ref", "-d", "refs/heads/beta-branch", cwd=self.origin)
                if case == "refs/remotes/origin/beta-phase":
                    _ = self.git("update-ref", case, published)
                if case in unit_revisions:
                    _ = self.git("update-ref", "-d", "refs/remotes/origin/beta-branch")
                    self.assertEqual(self.git("for-each-ref", "refs/remotes/origin/beta-branch"), "")
                absent = subprocess.run(["git", "cat-file", "-e", f"{second}^{{commit}}"], cwd=self.checkout,
                                        env=self.env, capture_output=True, text=True, check=False,
                                        timeout=TEST_HANG_GUARD_SECS)
                self.assertNotEqual(absent.returncode, 0)
                checkpoint = "refs/remotes/origin/beta-branch" if case == "unit branch ref" else second
                if case in (*phase_refs, *unit_revisions):
                    checkpoint = case
                lines = [self.batch_line("alpha", "2", first), self.batch_line("beta", "2", checkpoint)]

                if phase_branch:
                    result = self.land_batch(lines)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.git("rev-parse", "production^2", cwd=self.origin), second)
                    self.assertEqual(self.git("rev-parse", "production^1^2", cwd=self.origin), first)
                    continue

                unpublished = self.interrupt_batch(*lines)
                self.assertEqual(self.git("rev-parse", "HEAD^2"), second)
                self.assertEqual(self.git("rev-parse", "HEAD^1^2"), first)
                self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), published)
                saved = self.batch_record().read_text(encoding="utf-8").splitlines()

                result = self.land_batch(lines)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(saved, [f"{first}\t{lines[0]}", f"{second}\t{lines[1]}"])
                for unit in ("alpha", "beta"):
                    self.assertIn(f"batch: resume — {unit} phase 2 is already merged", result.stdout)
                self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), unpublished)
                self.assertFalse(self.batch_record().exists())

    def test_a_batch_record_unlink_error_warns_and_allows_promotion(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        published = self.git("rev-parse", "HEAD")
        commit = self.checkpoint(content="alpha batch change\n")
        record = self.batch_record()
        real_unlink = Path.unlink

        def refuse_record(path: Path, missing_ok: bool = False) -> None:
            if path == record:
                raise PermissionError("fixture cannot remove the batch record")
            real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", refuse_record):
            result = self.land_batch([self.batch_line("alpha", "2", commit)])

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"batch: warning — {record}", result.stdout)
        self.assertEqual(result.stdout.count("fixture cannot remove the batch record"), 1)
        self.assertTrue(record.is_file())
        _ = self.assert_landed(published, [commit])

    def test_a_published_batch_retires_its_record_and_a_new_batch_runs(self) -> None:
        commit = self.checkpoint(content="alpha batch change\n")
        result = self.land_batch([self.batch_line("alpha", "2", commit)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.batch_record().exists())
        beta = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
        fresh = self.land_batch([self.batch_line("beta", "2", beta)])
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertEqual(self.git("rev-parse", "production^2", cwd=self.origin), beta)

    def test_unpushed_merges_without_a_batch_record_require_a_reset(self) -> None:
        published = self.git("rev-parse", "HEAD")
        commit = self.checkpoint(content="alpha batch change\n")
        line = self.batch_line("alpha", "2", commit)
        unpublished = self.interrupt_batch(line)
        self.batch_record().unlink()
        result = self.land_batch([line])
        self.assert_batch_requires_reset(result, published, unpublished)
        self.assertFalse(self.batch_record().exists())
        _ = self.git("reset", "--keep", published)
        fresh = self.land_batch([line])
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        self.assertEqual(self.git("rev-parse", "production^2", cwd=self.origin), commit)

    def test_a_changed_batch_cannot_resume_unpushed_merges_but_the_same_batch_can(self) -> None:
        start = self.git("rev-parse", "HEAD")
        first = self.checkpoint(content="alpha batch change\n")
        second = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
        original = self.batch_line("alpha", "2", first)
        self.executable(self.bin / "checkpoint-test", STUB + "raise KeyboardInterrupt\n")
        with self.assertRaises(KeyboardInterrupt):
            _ = self.land_batch([original])
        unpublished = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^2"), first)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
        self.executable(self.bin / "checkpoint-test", STUB)

        changed = self.land_batch([original, self.batch_line("beta", "2", second)])
        self.assertEqual(changed.returncode, 1, changed.stdout + changed.stderr)
        self.assertIn("reset the merge branch to its pushed tip, then start a fresh batch", changed.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), unpublished)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)

        equivalent = "  " + original.replace("alpha 2", "'alpha' 2", 1)
        same = self.land_batch([equivalent])
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
        self.assertIn("batch: resume — alpha phase 2 is already merged", same.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), unpublished)
        self.assertFalse(self.batch_record().exists())

    def test_red_package_drops_only_the_item_that_selects_its_command(self) -> None:
        for interrupted, shrink, shared_red, probe_case in ((False, False, False, ""), (True, False, False, ""),
                                                           (True, True, False, ""), (False, False, True, ""),
                                                           (False, False, False, "main"), (False, False, False, "rename")):
            with self.subTest(interrupted=interrupted, shrink=shrink, shared_red=shared_red, probe_case=probe_case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                if interrupted:
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                            '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
                    if shrink:
                        second = self.checkpoint("docs/plans/alpha.md", "shrink plan\n", subject="shrink")
                        second_line = self.batch_line("alpha", "2", second, "--shrink")
                    else:
                        second = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
                        second_line = self.batch_line("beta", "2", second)
                    lines = [self.batch_line("alpha", "2", first), second_line]
                    merge = merge_checkpoint.merge

                    def stop_before_second(request: merge_checkpoint.MergeRequest) -> str:
                        if request.commit == first:
                            return merge(request)
                        raise KeyboardInterrupt

                    with (patch.object(merge_checkpoint, "merge", side_effect=stop_before_second),
                          self.assertRaises(KeyboardInterrupt)):
                        _ = self.land_batch(lines)
                    unpublished = self.git("rev-parse", "HEAD")
                    self.assertNotEqual(unpublished, start)
                    self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
                    self.assertEqual(self.git("rev-parse", "HEAD^2"), first)
                    self.assertEqual(self.events("verify.sh"), [])
                    self.assertEqual(self.events("checkpoint-test"), [])
                    (self.state / "fail-always-test-alpha-proof").touch()
                    result = self.land_batch(lines)
                    # The merged alpha item resumes alone: its red tests undo its unpushed merge.
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertNotIn("resume it alone with the single command", result.stdout)
                    self.assertIn("batch: resume — alpha phase 2 is already merged", result.stdout)
                    self.assertIn("send alpha: repair package alpha-proof", result.stdout)
                    self.assertEqual(result.stdout.count("batch: retry"), 0)
                    if shrink:
                        self.assertIn(f"send alpha: resolve alpha phase 2 ({first[:12]}), then resend this checkpoint",
                                      result.stdout)
                        self.assertEqual(self.git("rev-parse", "HEAD"), start)
                        for branch in ("production", "main"):
                            self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), start)
                        self.assertEqual(self.receipts(), [])
                    else:
                        self.assertIn("send beta: phase 2 merged and pushed", result.stdout)
                        tip = self.git("rev-parse", "HEAD")
                        self.assertEqual([self.git("rev-parse", "HEAD^1"), self.git("rev-parse", "HEAD^2")],
                                         [start, second])
                        for branch in ("production", "main"):
                            self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), tip)
                        self.assertEqual([row["source_checkpoint"] for row in self.receipts()], [second])
                    self.assertNotEqual(self.git("rev-parse", "HEAD"), unpublished)
                    self.assertEqual(self.events("verify.sh"), [["test", "alpha-proof"]] * 2)
                    self.assertEqual(self.events("checkpoint-test"), [[]] * (1 if shrink else 2))
                elif probe_case == "main":
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.beta_checkpoint("src/main-sensitive.txt", "fails with new main\n")
                    second = self.checkpoint(content="alpha batch side\n")
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    writer = self.root / "main-writer"
                    _ = self.git("clone", "-b", "main", str(self.origin), str(writer))
                    _ = self.git("config", "user.name", "Checkpoint Test", cwd=writer)
                    _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=writer)
                    self.write(writer, "main-ready.txt", "main dependency\n")
                    _ = self.git("add", "main-ready.txt", cwd=writer)
                    _ = self.git("commit", "-m", "new main dependency", cwd=writer)
                    main_commit = self.git("rev-parse", "HEAD", cwd=writer)
                    self.executable(self.checkout / ".git/hooks/post-merge", f'''#!/usr/bin/env python3
import subprocess
parent = subprocess.run(["git", "rev-parse", "HEAD^2"], capture_output=True, text=True,
                        check=True, timeout={TEST_HANG_GUARD_SECS}).stdout.strip()
if parent == {third!r}:
    subprocess.run(["git", "-C", {str(writer)!r}, "push", "origin", "main"],
                   check=True, timeout={TEST_HANG_GUARD_SECS})
''')
                    self.executable(self.bin / "checkpoint-test", STUB + '''
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                     check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
with (state / "main-probe-tips").open("a") as output:
    output.write(tip + "\\n")
red = Path("main-ready.txt").is_file() and Path("src/main-sensitive.txt").is_file()
print("main probe " + tip)
raise SystemExit(1 if red else 0)
''')
                    result = self.land_batch([self.batch_line("beta", "2", first), self.batch_line("alpha", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    tip = self.git("rev-parse", "HEAD")
                    for branch in ("production", "main"):
                        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), tip)
                    merges = self.git("rev-list", "--reverse", "--first-parent", "--merges", start + ".." + tip).splitlines()
                    self.assertEqual([self.git("rev-parse", merge + "^2") for merge in merges], [second, third, main_commit])
                    repairs = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                               if line.startswith("send ") and ": repair " in line]
                    self.assertEqual(repairs, ["send beta"])
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertEqual(self.git("ls-tree", "HEAD", "--", "src/main-sensitive.txt"), "")
                    tested = (self.state / "main-probe-tips").read_text().splitlines()
                    self.assertEqual(len(tested), 8)
                    for tested_tip in set(tested):
                        self.assertEqual(self.git("merge-base", "--is-ancestor", main_commit, tested_tip), "")
                elif probe_case == "rename":
                    self.promotions = str(self.promote)
                    self.production_doc()
                    self.write(self.checkout, "crates/shared/Cargo.toml",
                               '[package]\nname = "old-proof"\nversion = "0.1.0"\n')
                    self.write(self.checkout, "crates/shared/src/lib.rs", "pub fn initial() {}\n")
                    _ = self.git("add", "crates/shared")
                    _ = self.git("commit", "-m", "initial package")
                    _ = self.git("push", "origin", "HEAD:production", "HEAD:main")
                    start = self.git("rev-parse", "HEAD")
                    for worktree in (self.unit, self.other):
                        _ = self.git("merge", "--ff-only", "production", cwd=worktree)
                    first = self.checkpoint("crates/shared/src/lib.rs", "pub fn first() {}\n")
                    second = self.beta_checkpoint("crates/shared/Cargo.toml",
                                                  '[package]\nname = "new-proof"\nversion = "0.1.0"\n')
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    (self.state / "fail-always-test-new-proof").touch()
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    repairs = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                               if line.startswith("send ") and ": repair " in line]
                    self.assertEqual(repairs, ["send beta"])
                    self.assertEqual(self.events("verify.sh"), [["test", "new-proof"], ["test", "new-proof"],
                                                                 ["test", "old-proof"]])
                    self.assertEqual(self.events("checkpoint-test"), [[], []])
                    self.assertEqual(result.stdout.count("bisect: green"), 1)
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertEqual(self.git("show", "HEAD:crates/shared/Cargo.toml"),
                                     '[package]\nname = "old-proof"\nversion = "0.1.0"')
                elif shared_red:
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.checkpoint(content="alpha batch side\n")
                    second = self.beta_checkpoint("src/broken.txt", "broken shared test import\n")
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    self.executable(self.bin / "checkpoint-test", STUB + '''
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                     text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
with (state / "checkpoint-tips").open("a") as output:
    output.write(tip + "\\n")
red = Path("src/broken.txt").is_file()
print("checkpoint proof " + tip + (": red" if red else ": green"))
raise SystemExit(1 if red else 0)
''')
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    repairs = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                               if line.startswith("send ") and ": repair " in line]
                    self.assertEqual(repairs, ["send beta"])
                    self.assertNotIn("send beta: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    tips = (self.state / "checkpoint-tips").read_text().splitlines()
                    self.assertEqual(self.events("checkpoint-test"), [[]] * 7)
                    # The base check runs the red command once on the start tip, before the bisect.
                    self.assertEqual(tips.pop(2), start)
                    self.assertEqual([self.git("rev-parse", tip + "^2") for tip in tips[2:-1]], [second, second, first])
                    probes = [line for line in result.stdout.splitlines() if line.startswith("bisect:")]
                    self.assertEqual([line.split(" — ", 1)[0] for line in probes], ["bisect: red", "bisect: green"])
                    self.assertIn(f"beta phase 2 at {tips[2]}", probes[0])
                    self.assertIn(f"alpha phase 2 at {tips[4]}", probes[1])
                    log = (self.state / f"merge_{first[:7]}_test.log").read_text()
                    for tip in set(tips[2:-1]):
                        self.assertIn("checkpoint proof " + tip, log)
                    self.assertEqual(self.events("verify.sh"), [])
                    self.assertEqual(self.git("ls-tree", "HEAD", "--", "src/broken.txt"), "")
                else:
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.checkpoint(content="alpha batch side\n")
                    second = self.beta_checkpoint("crates/beta-proof/Cargo.toml",
                                                  '[package]\nname = "beta-proof"\nversion = "0.1.0"\n')
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    (self.state / "fail-always-test-beta-proof").touch()
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertIn("red: failed", result.stdout)
                    self.assertIn("send beta: repair package beta-proof", result.stdout)
                    self.assertNotIn("send beta: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    self.assertEqual(self.events("verify.sh"), [["test", "beta-proof"], ["test", "beta-proof"]])
                    self.assertEqual(self.events("checkpoint-test"), [[], []])
                    self.assertEqual(self.git("ls-tree", "HEAD", "--", "crates/beta-proof/Cargo.toml"), "")


if __name__ == "__main__":
    _ = unittest.main()
