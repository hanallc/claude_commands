"""A killed checkpoint resumes unfinished work using its own merge evidence."""
from __future__ import annotations

from scripts.production import checkpoint_gate

import argparse
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
from types import ModuleType
from typing import cast, final
import unittest
from unittest.mock import patch

from scripts.production import merge_checkpoint
from scripts.production import change_review
from scripts.lib import atomic_file
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import land_runs, staff, test_merge_checkpoint_batch, unit_lookup
from scripts.production.cli_fixture import process_boundary
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, unit_membership
from scripts.production.merge_checkpoint_test_support import (
    DELIVERS, MergeCheckpointFixture, STARTED, STUB, TRIAL, configure_test_environment,
)


class Killed(BaseException):
    """Leave disk state intact, as a process killed without exception cleanup would."""


def setUpModule() -> None:
    configure_test_environment()


@final
class CheckpointRerunTests(MergeCheckpointFixture):
    def review_checkpoint(self, *changes: str, unit: str = "alpha") -> tuple[Path, str]:
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.promotions = str(self.promote)
        self.production_doc(change_review=review)
        return review, self.dated_checkpoint(f"checkpoint({unit}): change\n\n" + "\n".join(
            f"Change-Review: {change} | image 012345abcdef" for change in changes))

    def test_checkpoint_reuses_its_staff_snapshot_after_the_file_changes(self) -> None:
        review, checkpoint = self.review_checkpoint("One change | Open it | Result")
        with patch.dict("os.environ", self.env):
            unit = next(row for row in staff.snapshot().rows if row.name == "alpha")
        readers = ModuleType("checkpoint_staff_fixture")
        vars(readers).update(vars(staff))
        with (patch.object(readers, "snapshot", wraps=staff.snapshot) as snapshot,
              patch.object(merge_checkpoint, "staff", readers),
              patch.object(change_review, "staff", readers),
              self.after_input(lambda path: path.write_bytes(b"invalid staff JSON\n"))):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual([row["Producer"] for row in change_review.pending(review)],
                         [unit.id], result.stdout)

    def save_case(self) -> Path:
        snapshot = self.root.with_name("rerun-seed")
        _ = shutil.copytree(self.root, snapshot)
        self.addCleanup(shutil.rmtree, snapshot)
        # Merge commits must be identical in the uninterrupted and resumed cases.
        self.env["GIT_COMMITTER_DATE"] = "2026-11-06T12:00:00-05:00"
        self.env["GIT_AUTHOR_DATE"] = self.env["GIT_COMMITTER_DATE"]
        # A row records the minute it is added; one fixed clock keeps both cases on the same minute.
        _ = self.enterContext(patch.object(change_review, "now",
                                           return_value=datetime.fromisoformat(self.env["GIT_COMMITTER_DATE"])))
        return snapshot

    def after_input(self, change: Callable[[Path], object]) -> AbstractContextManager[object]:
        """Change the staff file after the checkpoint reads its unit from that file."""
        staff_file = Path(self.env["STAFF_FILE"])
        read = merge_checkpoint.request_from

        def request_from(args: argparse.Namespace) -> merge_checkpoint.MergeRequest:
            request = read(args)
            _ = change(staff_file)
            return request

        return patch.object(merge_checkpoint, "request_from", side_effect=request_from)

    def restore_case(self, snapshot: Path) -> None:
        shutil.rmtree(self.root)
        _ = shutil.copytree(snapshot, self.root)

    def assert_review_recovered(self, stop: str) -> None:
        review, checkpoint = self.review_checkpoint("First change | Open first | First result", "Second change | Open second | Second result")
        snapshot = self.save_case()
        uninterrupted = self.run_checkpoint(checkpoint).process
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        expected = review.read_bytes()
        merge = self.git("rev-parse", "HEAD")
        expected_tests = self.events("checkpoint-test")
        self.restore_case(snapshot)
        with patch.object(merge_checkpoint, stop, side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        self.assertFalse(review.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        resumed = self.run_checkpoint(checkpoint).process
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertTrue(review.exists(), "rerun must restore the missing Change-Review rows")
        self.assertEqual(review.read_bytes(), expected)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(self.events("checkpoint-test"), expected_tests)
        repeated = self.run_checkpoint(checkpoint).process
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn("change review: failed", repeated.stdout)
        self.assertEqual(review.read_bytes(), expected)

    def test_review_rows_recover_after_merge_before_tests(self) -> None:
        self.assert_review_recovered("tests")

    def test_review_rows_recover_after_push_before_recording(self) -> None:
        self.assert_review_recovered("record_publication_changes")

    @process_boundary
    def test_interrupted_batch_push_recovers_validation_evidence(self) -> None:
        for interruption in ("unpublished shared file", "published before validator exit", "before beta merge",
                             "beta already published", "rollback fails"):
            with self.subTest(interruption=interruption):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                beta_already_published = interruption == "beta already published"
                partial = interruption == "before beta merge" or beta_already_published
                published = interruption == "published before validator exit"
                rollback_fails = interruption == "rollback fails"
                self.env["MERGE_GATE_STATE_DIR"] = str(self.root / "merge-gate")
                path = "src/shared.toml"
                middle = "".join(f"key{index} = {index}\n" for index in range(12))
                self.write(self.checkout, path, f"alpha = 0\n{middle}beta = 0\n")
                _ = self.git("add", path)
                _ = self.git("commit", "-m", "Add shared settings")
                start = self.git("rev-parse", "HEAD")
                _ = self.git("push", "origin", "production", "production:main")
                for tree in (self.unit, self.other):
                    _ = self.git("merge", "--ff-only", start, cwd=tree)
                alpha = self.checkpoint(path, f"alpha = 1\n{middle}beta = 0\n")
                beta_path = "src/beta.txt" if partial else path
                self.write(self.other, beta_path, "beta change\n" if partial else f"alpha = 0\n{middle}beta = 1\n")
                _ = self.git("add", beta_path, cwd=self.other)
                _ = self.git("commit", "-m", "Change beta settings", cwd=self.other)
                beta = self.git("rev-parse", "HEAD", cwd=self.other)
                _ = self.git("push", "origin", "beta-branch", cwd=self.other)
                batch_items = {unit: [
                    unit, "2", commit, "--started", STARTED, "--review-trial", TRIAL,
                    "--delivers", DELIVERS, "--scratch", str(self.state),
                ] for unit, commit in (("alpha", alpha), ("beta", beta))}
                if beta_already_published:
                    self.push = "git"
                    self.production_doc()
                    landed = self.invoke(["--production", "example", *batch_items["beta"]])
                    self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)
                    start = self.git("rev-parse", "HEAD")
                    self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
                self.push = "validate_and_push"
                self.production_doc()
                batch = self.root / "batch.txt"
                _ = batch.write_text("".join(shlex.join(item) + "\n" for item in batch_items.values()))

                if partial:
                    real_git = shutil.which("git")
                    assert real_git is not None
                    self.executable(self.bin / "git", f"""#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
args = sys.argv[1:]
if args[:4] == ["-C", {str(self.checkout)!r}, "merge", "--no-ff"] and args[-1] == {alpha!r}:
    result = subprocess.run([{real_git!r}, *args], check=False)
    if result.returncode == 0:
        os.kill(os.getppid(), signal.SIGKILL)
    raise SystemExit(result.returncode)
os.execv({real_git!r}, [{real_git!r}, *args])
""")
                cause = "fatal: CI discovery failed after publication"
                formatter_edit = "partial formatter edit\n"
                self.executable(self.bin / "validate_and_push.sh", STUB + f"""
import signal
import subprocess
first_attempt = state / "first-push-attempt"
if not {partial!r} and not first_attempt.is_file():
    first_attempt.touch()
    if {published!r}:
        print("=== STEP: direct push to production ===", flush=True)
        subprocess.run(["git", "push", "origin", "HEAD:refs/heads/production"],
                       check=True, timeout=TEST_HANG_GUARD_SECS)
        print("publication finished", flush=True)
        print({cause!r}, file=sys.stderr, flush=True)
    os.kill(os.getppid(), signal.SIGKILL)
else:
    if {rollback_fails!r}:
        Path({path!r}).write_text({formatter_edit!r})
    print("=== STEP: taplo ===", flush=True)
    print("error: invalid TOML", file=sys.stderr, flush=True)
    print("  ┌─ {path}:2:5", file=sys.stderr, flush=True)
    print("!!! VALIDATION FAILED at step: taplo !!!", flush=True)
raise SystemExit(1)
""")
                args = ["--production", "example", "--batch", str(batch)]
                interrupted = self.invoke(args)
                self.assertEqual(interrupted.returncode, -signal.SIGKILL, interrupted.stdout + interrupted.stderr)
                combined = self.git("rev-parse", "HEAD")
                self.assertEqual(self.git("rev-parse", "HEAD^2"), alpha if partial else beta)
                self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), combined if published else start)
                batch_record = Path(self.git("rev-parse", "--path-format=absolute", "--git-path",
                                             "checkpoint-batches/production"))
                saved_record = batch_record.read_bytes() if rollback_fails else b""
                if partial:
                    self.assertIn(beta, batch_record.read_text())
                with patch.dict("os.environ", self.env):
                    results_before = land_runs.last_results()
                result = self.invoke(args)
                push_log = self.state / f"merge_{alpha[:7]}_push.log"
                if published:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(self.events("validate_and_push.sh")), 1)
                    self.assertIn(f"publication finished\n{cause}\n", push_log.read_text())
                else:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(len(self.events("validate_and_push.sh")), 1 if partial else 2)
                    self.assertIn(f"error: invalid TOML — log {push_log}", result.stdout)
                    notices = [line for line in result.stdout.splitlines() if line.startswith("send ")]
                    if beta_already_published:
                        self.assertEqual(notices, [
                            f"send alpha: repair the push failure in {path} and send a new checkpoint",
                        ])
                        with patch.dict("os.environ", self.env):
                            beta_land = land_runs.last_results()[beta]
                        self.assertTrue(results_before[beta]["landed"])
                        self.assertEqual(beta_land, results_before[beta])
                        self.assertFalse(batch_record.exists())
                        self.assertEqual(self.git("rev-parse", "HEAD"), start)
                    elif partial:
                        self.assertEqual(notices, [
                            f"send alpha: repair the push failure in {path} and send a new checkpoint",
                            "send beta: nothing was pushed; the push failed in alpha's change; "
                            + "send this checkpoint again after that repair",
                        ])
                        with patch.dict("os.environ", self.env):
                            beta_land = land_runs.last_results()[beta]
                        self.assertEqual((beta_land["unit"], beta_land["landed"], beta_land["step"], beta_land["state"]),
                                         ("beta", False, "push", "failed"))
                        self.assertFalse(batch_record.exists())
                        self.assertEqual(self.git("rev-parse", "HEAD"), start)
                    else:
                        self.assertEqual(notices, [f"send {unit}: the showrunner will inspect the failed command"
                                                   for unit in ("alpha", "beta")])
                    if rollback_fails:
                        self.assertIn("rollback failed", result.stdout)
                        self.assertEqual((self.checkout / path).read_text(), formatter_edit)
                        self.assertEqual(self.git("rev-parse", "HEAD"), combined)
                        self.assertTrue(batch_record.exists())
                        self.assertEqual(batch_record.read_bytes(), saved_record)
                    self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)

    def test_review_rows_written_before_receipt_are_not_duplicated(self) -> None:
        review, checkpoint = self.review_checkpoint("A change | Open it | Result")
        original = Path.touch

        def touch(path: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
            if path.name == "promotion":
                raise Killed
            original(path, mode=mode, exist_ok=exist_ok)

        # The list write completes before the promotion receipt records completion.
        with patch.object(Path, "touch", touch), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        expected = review.read_bytes()
        result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("change review: failed", result.stdout)
        self.assertEqual(review.read_bytes(), expected)

    def assert_staff_refusal(self, refused: subprocess.CompletedProcess[str], review: Path,
                             baseline: str, origin: str) -> None:
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("change review: failed", refused.stdout)
        self.assertIn("the staff record is read before the push", refused.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), origin)
        self.assertEqual(self.git("rev-parse", "HEAD"), baseline)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(review.exists())
        self.assertEqual(self.events("checkpoint-test"), [])

    def test_an_unreadable_staff_record_stops_before_push_and_a_repair_writes_each_change_once(self) -> None:
        review, checkpoint = self.review_checkpoint("First change | Open first | First result", "Second change | Open second | Second result")
        baseline = self.git("rev-parse", "HEAD")
        origin = self.git("rev-parse", "production", cwd=self.origin)
        staff_file = Path(self.env["STAFF_FILE"])
        saved_staff = staff_file.read_bytes()
        _ = staff_file.write_bytes(b"invalid staff JSON\n")
        refused = self.run_checkpoint(checkpoint).process
        self.assert_staff_refusal(refused, review, baseline, origin)

        _ = staff_file.write_bytes(saved_staff)
        repaired = self.run_checkpoint(checkpoint).process
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(merge, baseline)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual([row["Change"] for row in change_review.pending(review)],
                         ["First change", "Second change"])

    def test_an_unreadable_staff_record_undoes_a_resumed_unpushed_merge_before_tests(self) -> None:
        review, checkpoint = self.review_checkpoint("A change | Open it | Result")
        baseline = self.git("rev-parse", "HEAD")
        origin = self.git("rev-parse", "production", cwd=self.origin)
        with patch.object(merge_checkpoint, "checkpoint_tests", side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), baseline)
        self.assertEqual(self.events("checkpoint-test"), [])
        with patch.object(change_review, "publication_changes", side_effect=staff.StaffUnreadable("invalid staff JSON")):
            refused = self.run_checkpoint(checkpoint).process
        self.assert_staff_refusal(refused, review, baseline, origin)

    def test_a_production_without_a_review_list_pushes_with_an_unreadable_staff_record(self) -> None:
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): change\n\nChange-Review: A change | Open it | Result | image 012345abcdef")
        with self.after_input(Path.unlink):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the production doc has no Change review line; no rows written", result.stdout)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), self.git("rev-parse", "HEAD"))

    def test_a_merge_keeps_its_units_producer_after_the_staff_row_goes(self) -> None:
        review, checkpoint = self.review_checkpoint("First change | Open first | First result",
                                                    "Second change | Open second | Second result")
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            unit_staff_id = unit_lookup.unit_row("alpha", staff.snapshot()).id

        def unregister(staff_file: Path) -> None:
            document = cast(dict[str, list[dict[str, object]]], json.loads(staff_file.read_text(encoding="utf-8")))
            document["rows"] = [row for row in document["rows"] if row["name"] != "alpha"]
            _ = staff_file.write_text(json.dumps(document), encoding="utf-8")

        with self.after_input(unregister):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        notes = [line for line in result.stdout.splitlines() if line.startswith("change review: no staff row for unit")]
        self.assertEqual(notes, [])
        self.assertEqual([row["Producer"] for row in change_review.pending(review)], [unit_staff_id, unit_staff_id])

    def test_an_already_pushed_merge_keeps_the_row_failure_report_for_an_unreadable_staff_record(self) -> None:
        review, checkpoint = self.review_checkpoint("A change | Open it | Result")
        with patch.object(merge_checkpoint, "record_publication_changes", side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_checkpoint(checkpoint)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        with patch.object(change_review, "publication_changes", side_effect=staff.StaffUnreadable("invalid staff JSON")):
            result = self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("change review: failed", result.stdout)
        self.assertIn("retry after the repair", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertFalse(review.exists())

    def test_handoff_killed_after_merge_retests_before_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        snapshot = self.save_case()
        uninterrupted = self.run_handoff(checkpoint)
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        merge = self.git("rev-parse", "HEAD")
        expected_tests = self.events("checkpoint-test")
        self.assertTrue(expected_tests)
        self.restore_case(snapshot)
        with patch.object(merge_checkpoint, "checkpoint_tests", side_effect=Killed), self.assertRaises(Killed):
            _ = self.run_handoff(checkpoint)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertNotEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(self.events("checkpoint-test"), [])
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("checkpoint-test"), expected_tests)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(len(self.events("send.py")), 1)

    def assert_handoff_promotion_recovered(self, failed_promotion: bool) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        snapshot = self.save_case()
        uninterrupted = self.run_handoff(checkpoint)
        self.assertEqual(uninterrupted.returncode, 0, uninterrupted.stdout + uninterrupted.stderr)
        merge = self.git("rev-parse", "HEAD")
        expected_tests = self.events("checkpoint-test")
        expected_notice = self.events("send.py")
        expected_log = self.log.read_text().split(": ", 1)[1]
        self.restore_case(snapshot)
        if failed_promotion:
            with patch.object(merge_checkpoint, "promote", side_effect=checkpoint_gate.Stop(
                    "promote", "failed", "injected failure", "retry promotion")):
                failed = self.run_handoff(checkpoint)
            self.assertNotEqual(failed.returncode, 0)
        else:
            with patch.object(merge_checkpoint, "promote", side_effect=Killed), self.assertRaises(Killed):
                _ = self.run_handoff(checkpoint)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertNotEqual(self.git("rev-parse", "HEAD", cwd=self.promote), merge)
        self.assertEqual(self.events("send.py"), [])
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), merge)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), merge)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.events("checkpoint-test"), expected_tests)
        self.assertEqual(self.events("send.py"), expected_notice)
        self.assertEqual(self.log.read_text().split(": ", 1)[1], expected_log)
        self.assertEqual(self.events("style_sync.py"), [["notify", "--repo-dir", str(self.checkout),
                                                        "--branch", "production", "--tip", merge]])

    def test_handoff_killed_after_push_retries_promotion_before_acknowledgment(self) -> None:
        self.assert_handoff_promotion_recovered(False)

    def test_handoff_failed_promotion_retries_before_acknowledgment(self) -> None:
        self.assert_handoff_promotion_recovered(True)


@final
class BatchRecoveryTests(test_merge_checkpoint_batch.BatchFixture):
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


if __name__ == "__main__":
    _ = unittest.main()
