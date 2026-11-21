"""Land ordered checkpoint batches against private Git repositories."""

from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
from typing import final
from unittest.mock import patch

from scripts.production import land_runs, merge_checkpoint, staff
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
        for outcome in ("shared list", "push failure", "one unit push failure", "unmatched push failure",
                        "earlier step push failure",
                        "separate lists", "second list fails", "second directory sync fails",
                        "unrelated branch renamed", "unpublished main integration"):
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
                file_diagnostic = outcome in ("one unit push failure", "unmatched push failure", "earlier step push failure")
                beta_path = "src/beta.toml" if file_diagnostic else "src/beta.txt"
                second = self.beta_checkpoint(beta_path, "beta batch change\n", visible=True)
                push_failed = outcome.endswith("push failure")
                if push_failed:
                    self.push = "validate_and_push"
                    self.production_doc(change_review=review)
                    if outcome == "push failure":
                        failure_command = """
import subprocess
origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                        text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
subprocess.run(["git", "push", origin, "HEAD:refs/heads/production"],
               check=True, timeout=TEST_HANG_GUARD_SECS)
"""
                    else:
                        failed_file = f"outside/{beta_path}" if outcome == "unmatched push failure" else beta_path
                        failure_command = ""
                        if outcome == "earlier step push failure":
                            failure_command = """
print("=== STEP: cargo-mend autofix ===", flush=True)
print("error: recovered compiler error", file=sys.stderr, flush=True)
print(" --> src/alpha.txt:2:5", file=sys.stderr, flush=True)
"""
                        failure_command += f"""
print("=== STEP: taplo ===", flush=True)
print("error: invalid TOML", file=sys.stderr, flush=True)
print({f"  ┌─ {self.checkout / failed_file}:2:5"!r}, file=sys.stderr, flush=True)
print("!!! VALIDATION FAILED at step: taplo !!!", flush=True)
print("still waiting for memory: src/alpha.txt", file=sys.stderr, flush=True)
"""
                    self.executable(self.bin / "validate_and_push.sh", STUB + failure_command + "raise SystemExit(1)\n")
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
                if push_failed:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    tip = self.git("rev-parse", "production", cwd=self.origin)
                    if outcome == "push failure":
                        self.assertNotEqual(tip, start)
                        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
                        self.assertEqual([self.git("rev-parse", "HEAD~1^2"), self.git("rev-parse", "HEAD^2")],
                                         [first, second])
                        self.assertEqual([line.split()[2] for line in (self.state / "pushed-refs").read_text().splitlines()],
                                         ["refs/heads/production"])
                        push_log = self.state / f"merge_{first[:7]}_push.log"
                        self.assertIn(f"no clear failure line (exit 1) — log {push_log}", result.stdout)
                    else:
                        self.assertEqual(tip, start)
                        self.assertEqual(self.git("rev-parse", "HEAD"), start)
                    self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), start)
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertEqual(len(self.events("validate_and_push.sh")), 1)
                    self.assertNotIn("batch: retry", result.stdout)
                    notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
                    if outcome in ("one unit push failure", "earlier step push failure"):
                        self.assertEqual(notices, [
                            "send alpha: nothing was pushed; the push failed in beta's change; "
                            + "send this checkpoint again after that repair",
                            f"send beta: repair the push failure in {beta_path} and send a new checkpoint",
                        ])
                        self.assertNotIn("the showrunner will inspect the failed command", result.stdout)
                    else:
                        self.assertEqual(notices, [f"send {unit}: the showrunner will inspect the failed command"
                                                   for unit in ("alpha", "beta")])
                    self.assertIn("push: failed", result.stdout)
                    if outcome == "earlier step push failure":
                        self.assertIn("error: invalid TOML", result.stdout)
                        self.assertNotIn("recovered compiler error", result.stdout)
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

    def test_main_conflict_returns_every_row_before_and_after_tests(self) -> None:
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
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
                self.assertEqual([line.split(":", 1)[0] for line in notices], ["send alpha", "send beta", "send alpha"])
                for notice in notices:
                    self.assertIn("origin/main conflicts: src/beta.txt", notice)
                self.assertEqual(self.events("checkpoint-test"), [[]] if main_moves_after_tests else [])
                self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_commit)
                self.assert_rolled_back(start)

    def test_main_conflict_preserves_preexisting_unpublished_work(self) -> None:
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
        self.assertEqual([line.split(":", 1)[0] for line in notices], ["send alpha", "send beta"])
        for notice in notices:
            self.assertIn("origin/main conflicts: existing.txt", notice)
        self.assertNotIn("batch: retry", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), published)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_commit)
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_batch_preserves_an_open_merge_and_its_staged_resolution(self) -> None:
        start = self.git("rev-parse", "HEAD")
        first = self.checkpoint(content="alpha batch side\n")
        second = self.beta_checkpoint("src/alpha.txt", "beta conflicting side\n")
        third = self.checkpoint("src/third.txt", "alpha third change\n")
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

    def assert_rolled_back(self, start: str) -> None:
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.checkout / ".git/MERGE_HEAD").exists())
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
        self.assertFalse(self.batch_record().exists())
        self.assertEqual(self.receipts(), [])

    def assert_red_batch(self, result: subprocess.CompletedProcess[str], alpha: str, beta: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual([line.split(":", 1)[0] for line in notices], ["send alpha", "send beta"], result.stdout)
        self.assertIn(f"phase 2 ({alpha[:12]}): this row's tests include a red step", notices[0])
        self.assertIn(f"phase 2 ({beta[:12]}): this row's tests include no reported red step", notices[1])
        for notice in notices:
            self.assertIn("package alpha-proof", notice)
            self.assertIn(str(self.state / f"merge_{alpha[:7]}_test.log"), notice)
        self.assertEqual(self.events("verify.sh"), [["test", "alpha-proof"]])
        self.assertEqual(self.events("checkpoint-test"), [])

    def test_red_batch_returns_both_rows_and_the_next_item_merges(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
        beta = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
        (self.state / "fail-always-test-alpha-proof").touch()

        result = self.land_batch([self.batch_line("alpha", "2", alpha), self.batch_line("beta", "2", beta)])

        self.assert_red_batch(result, alpha, beta)
        self.assert_rolled_back(start)
        next_item = self.land_batch([self.batch_line("beta", "2", beta)])
        self.assertEqual(next_item.returncode, 0, next_item.stdout + next_item.stderr)
        _ = self.assert_landed(start, [beta])

    def test_resumed_batch_returns_both_rows_after_one_red_test(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
        beta = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
        rows = [self.batch_line("alpha", "2", alpha), self.batch_line("beta", "2", beta)]
        with (patch.object(merge_checkpoint, "prepare_checkpoint", side_effect=KeyboardInterrupt),
              self.assertRaises(KeyboardInterrupt)):
            _ = self.land_batch(rows)
        self.assertEqual([self.git("rev-parse", "HEAD^1^2"), self.git("rev-parse", "HEAD^2")], [alpha, beta])
        self.assertTrue(self.batch_record().exists())
        self.assertEqual(self.events("verify.sh"), [])
        (self.state / "fail-always-test-alpha-proof").touch()

        result = self.land_batch(rows)

        self.assertIn("batch: resume — alpha phase 2 is already merged", result.stdout)
        self.assert_red_batch(result, alpha, beta)
        self.assert_rolled_back(start)
        next_item = self.land_batch([self.batch_line("beta", "2", beta)])
        self.assertEqual(next_item.returncode, 0, next_item.stdout + next_item.stderr)
        _ = self.assert_landed(start, [beta])

    def test_formatter_commit_rolls_back_when_validation_fails(self) -> None:
        self.promotions = str(self.promote)
        self.push = "validate_and_push"
        self.production_doc()
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint("src/formatter-trigger.txt", "format this\n")
        beta = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
        self.executable(self.bin / "validate_and_push.sh", STUB + r'''
import subprocess
trigger = Path("src/formatter-trigger.txt")
if not trigger.is_file():
    subprocess.run(["git", "push", "origin", "production"], check=True, timeout=TEST_HANG_GUARD_SECS)
    raise SystemExit(0)
trigger.write_text("formatted\n")
subprocess.run(["git", "commit", "-am", "Format alpha"], check=True, timeout=TEST_HANG_GUARD_SECS)
formatter = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=TEST_HANG_GUARD_SECS)
(state / "formatter-tip").write_text(formatter.strip())
print("=== STEP: taplo ===", flush=True)
print("error: invalid TOML", file=sys.stderr, flush=True)
raise SystemExit(1)
''')

        result = self.land_batch([self.batch_line("alpha", "2", alpha), self.batch_line("beta", "2", beta)])

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        formatter = (self.state / "formatter-tip").read_text()
        self.assertEqual(self.git("show", "-s", "--format=%s", formatter), "Format alpha")
        self.assertIn("error: invalid TOML", result.stdout)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual([line.split(":", 1)[0] for line in notices], ["send alpha", "send beta"])
        self.assertEqual(len(self.events("validate_and_push.sh")), 1)
        self.assert_rolled_back(start)
        next_item = self.land_batch([self.batch_line("beta", "2", beta)])
        self.assertEqual(next_item.returncode, 0, next_item.stdout + next_item.stderr)
        _ = self.assert_landed(start, [beta])

    def test_batch_stop_keeps_the_gate_failure_and_notifies_pending_rows(self) -> None:
        for case in ("invalid result log", "unrelated later row", "completed later row",
                     "reset fails", "later merge parent"):
            with self.subTest(case=case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.promotions = str(self.promote)
                self.production_doc()
                self.env["MERGE_GATE_STATE_DIR"] = str(self.root / "merge-gate")
                with patch.dict("os.environ", self.env):
                    results = land_runs.results_path()
                start = self.git("rev-parse", "HEAD")
                alpha = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                        '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
                beta = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
                alpha_row = self.batch_line("alpha", "2", alpha)
                beta_row = self.batch_line("beta", "2", beta)
                rows = [alpha_row, beta_row]
                expected = ["send alpha", "send beta"]
                rollback_tip = start
                beta_result: land_runs.LandResult | None = None
                if case == "invalid result log":
                    results.parent.mkdir(parents=True)
                    _ = results.write_bytes(b"\xff\n")
                elif case == "unrelated later row":
                    unrelated = self.git("commit-tree", "HEAD^{tree}", "-m", "Unrelated checkpoint", cwd=self.other)
                    _ = self.git("push", "origin", f"{unrelated}:refs/heads/unrelated", cwd=self.other)
                    rows.insert(1, self.batch_line("beta", "1", unrelated))
                    expected.insert(1, "send beta")
                    merge_checkpoint_row = merge_checkpoint.merge_checked_checkpoint

                    def interrupt_after_alpha(request: merge_checkpoint.MergeRequest) -> merge_checkpoint.CheckedCheckpointMerge:
                        if request.commit != alpha:
                            raise KeyboardInterrupt
                        return merge_checkpoint_row(request)

                    with (patch.object(merge_checkpoint, "merge_checked_checkpoint", interrupt_after_alpha),
                          self.assertRaises(KeyboardInterrupt)):
                        _ = self.land_batch(rows)
                    self.assertEqual(self.git("rev-parse", "HEAD^2"), alpha)
                elif case == "completed later row":
                    with (patch.object(merge_checkpoint, "finish_checkpoint", side_effect=KeyboardInterrupt),
                          self.assertRaises(KeyboardInterrupt)):
                        _ = self.land_batch(rows)
                    completed = self.land_batch([beta_row])
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    rollback_tip = self.git("rev-parse", "HEAD")
                    with patch.dict("os.environ", self.env):
                        beta_result = land_runs.last_results()[beta]
                    self.assertTrue(beta_result["landed"])
                    _ = self.push_main_change("crates/alpha-proof/src/lib.rs", "pub fn main_fix() {}\n")
                    (self.state / "events.jsonl").unlink()
                    expected = ["send alpha"]
                elif case == "later merge parent":
                    later = self.checkpoint("src/later.txt", "change outside the package\n")
                    rows.insert(1, self.batch_line("alpha", "3", later))
                    expected.insert(1, "send alpha")
                    with (patch.object(merge_checkpoint, "prepare_checkpoint", side_effect=KeyboardInterrupt),
                          self.assertRaises(KeyboardInterrupt)):
                        _ = self.land_batch(rows)
                    self.assertEqual(self.git("rev-parse", "HEAD^1^2"), later)

                (self.state / "fail-always-test-alpha-proof").touch()
                if case == "reset fails":
                    with patch.object(merge_checkpoint, "reset_batch", side_effect=OSError("fixture reset failed")):
                        result = self.land_batch(rows)
                else:
                    result = self.land_batch(rows)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
                self.assertEqual([line.split(":", 1)[0] for line in notices], expected, result.stdout)
                self.assertTrue(all("package alpha-proof" in line for line in notices), result.stdout)
                self.assertEqual(self.events("verify.sh"), [["test", "alpha-proof"]])
                self.assertEqual(self.events("checkpoint-test"), [])
                if case == "invalid result log":
                    self.assertEqual(results.read_bytes(), b"\xff\n")
                    self.assertEqual(result.stdout.count("land result: not recorded"), 2)
                    self.assertLess(result.stdout.index(notices[-1]), result.stdout.index("land result: not recorded"))
                elif case == "unrelated later row":
                    self.assertIn("changed paths unknown", notices[1])
                    self.assertIn("this row's tests include no reported red step", notices[2])
                elif case == "completed later row":
                    with patch.dict("os.environ", self.env):
                        self.assertEqual(land_runs.last_results()[beta], beta_result)
                elif case == "reset fails":
                    self.assertIn("red: failed — package alpha-proof failed", result.stdout)
                    warning = "rollback failed: fixture reset failed"
                    self.assertIn(warning, result.stdout)
                    self.assertLess(result.stdout.index(notices[-1]), result.stdout.index(warning))
                    self.assertEqual(self.git("rev-parse", "HEAD^2"), beta)
                    self.assertTrue(self.batch_record().exists())
                    continue
                elif case == "later merge parent":
                    self.assertIn("this row's tests include no reported red step", notices[1])

                if case == "completed later row":
                    self.assertEqual(self.git("rev-parse", "HEAD"), rollback_tip)
                    self.assertFalse(self.batch_record().exists())
                    (self.state / "fail-always-test-alpha-proof").unlink()
                    next_checkpoint = self.beta_checkpoint("src/independent.txt", "independent beta change\n")
                    next_item = self.land_batch([self.batch_line("beta", "3", next_checkpoint)])
                    self.assertEqual(next_item.returncode, 0, next_item.stdout + next_item.stderr)
                    self.assertEqual(self.git("show", "origin/production:src/independent.txt"), "independent beta change")
                else:
                    self.assert_rolled_back(rollback_tip)
                    next_item = self.land_batch([beta_row])
                    self.assertEqual(next_item.returncode, 0, next_item.stdout + next_item.stderr)
                    _ = self.assert_landed(start, [beta])

    def test_fetch_failure_returns_each_current_row(self) -> None:
        start = self.git("rev-parse", "HEAD")
        alpha = self.checkpoint()
        beta = self.beta_checkpoint("src/beta.txt", "clean beta change\n")
        self.env["MERGE_GATE_STATE_DIR"] = str(self.root / "merge-gate")
        with patch.dict("os.environ", self.env):
            results = land_runs.results_path()
        results.parent.mkdir(parents=True)
        _ = results.write_bytes(b"\xff\n")
        _ = self.git("remote", "set-url", "origin", str(self.root / "missing-origin"))
        result = self.land_batch([self.batch_line("alpha", "2", alpha), self.batch_line("beta", "2", beta)])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
        self.assertEqual([line.split(":", 1)[0] for line in notices], ["send alpha", "send beta"])
        for notice in notices:
            self.assertIn("fetch failed", notice)
            self.assertIn("send this checkpoint again when origin is available", notice)
        self.assertEqual(result.stdout.count("land result: not recorded"), 2)
        self.assertLess(result.stdout.index(notices[-1]), result.stdout.index("land result: not recorded"))
        self.assertEqual(self.git("rev-parse", "HEAD"), start)
        self.assertFalse(self.batch_record().exists())

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
