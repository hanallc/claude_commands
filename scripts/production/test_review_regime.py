#!/usr/bin/env python3
"""Check how the review ledger identifies current and older rows."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.production import review_regime

STARTED = "2026-10-12T09:00:00-04:00"
MERGED = "2026-10-12T11:00:00-04:00"
FIRST_PLAN = "docs/plans/dashboard.md"
SECOND_PLAN = "docs/plans/dashboard-link.md"


@final
class ReviewRegimeTests(unittest.TestCase):
    ledger: Path = Path()
    checkpoint: str = ""
    other_checkpoint: str = ""

    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = home / "review_regime.jsonl"
        _ = self.enterContext(cast(contextlib.AbstractContextManager[object],
                                   mock.patch.dict(os.environ, {"HOME": str(home)})))
        _ = self.enterContext(mock.patch.object(review_regime, "LEDGER", self.ledger))
        _ = self.enterContext(contextlib.chdir(home))
        _ = self.git("init", "--quiet", "--template=")
        tree = self.git("mktree")
        self.checkpoint = self.git("commit-tree", tree, "-m", "First checkpoint")
        self.other_checkpoint = self.git("commit-tree", tree, "-m", "Second checkpoint")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=Review Test", "-c", "user.email=review@example.invalid",
                               "-c", "commit.gpgsign=false", *args],
                              input="", capture_output=True, text=True, check=True).stdout.strip()

    def add(self, *identity: str, checkpoint: str = "",
            started: str = STARTED, merged: str = MERGED) -> str:
        arguments = ["review_regime.py", "add", *identity, "--regime", "after", "--started", started,
                     "--merged", merged, "--holds", "0", "--merge-defects", "0",
                     "--checkpoint", checkpoint or self.checkpoint]
        output = io.StringIO()
        with (mock.patch("sys.argv", arguments), contextlib.redirect_stdout(output),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(review_regime.main(), 0)
        return output.getvalue()

    def add_row(self, plan: str, phase: str) -> None:
        _ = self.add("--unit", "dashboard", "--plan", plan, "--phase", phase)

    def report(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            review_regime.report(None)
        return output.getvalue()

    def test_add_preserves_the_legacy_review_record_and_report(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                     "--ux-findings", "2", "--code-findings", "3", "--review-minutes", "14")
        row = review_regime.read_rows()[0]
        self.assertEqual((row.ux_findings, row.code_findings, row.review_minutes), (2, 3, 14))
        report = self.report()
        self.assertNotIn("simplify", report)
        self.assertIn("| UX reviewer findings per phase (mean) | - | - | 2.0 |", report)
        self.assertIn("| code findings per phase (mean) | - | - | 3.0 |", report)

    def test_old_simplify_fields_do_not_change_the_known_review_values(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                     "--ux-findings", "2", "--code-findings", "3", "--review-minutes", "14")
        record = cast(dict[str, object], json.loads(self.ledger.read_text()))
        record.update({"simplify_findings": 5, "simplify_minutes": 2.5})
        _ = self.ledger.write_text(json.dumps(record) + "\n")

        rows = review_regime.read_rows()
        self.assertEqual([(row.unit, row.plan, row.phase, row.ux_findings,
                           row.code_findings, row.review_minutes) for row in rows],
                         [("dashboard", FIRST_PLAN, "1", 2, 3, 14)])
        report = self.report()
        self.assertIn("| code findings per phase (mean) | - | - | 3.0 |", report)
        self.assertNotIn("simplify", report)

    def test_each_review_minute_field_reads_numbers_and_skips_other_values(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1")
        base = cast(dict[str, object], json.loads(self.ledger.read_text()))
        for field in ("review_minutes", "ux_check_minutes", "ux_repair_minutes"):
            for value, expected in ((7, 7.0), (2.5, 2.5), ("7", None), (None, None)):
                with self.subTest(field=field, value=value):
                    _ = self.ledger.write_text(json.dumps({**base, field: value}) + "\n")
                    read = cast(object, getattr(review_regime.read_rows()[0], field))
                    self.assertEqual(read, expected)
                    self.assertIs(type(read), type(expected))
            with self.subTest(field=field, value="missing"):
                _ = self.ledger.write_text(json.dumps({key: value for key, value in base.items() if key != field}) + "\n")
                self.assertIsNone(cast(object, getattr(review_regime.read_rows()[0], field)))

    def test_add_serializes_the_review_fields_and_full_checkpoint(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1")
        expected = {
            "unit": "dashboard", "plan": FIRST_PLAN, "phase": "1", "regime": "after",
            "started": STARTED, "merged": MERGED, "holds": 0, "merge_defects": 0,
            "ux_findings": None, "code_findings": None, "review_minutes": None,
            "ux_check_minutes": None, "ux_repair_minutes": None, "note": None, "excluded": None,
            "checkpoint": self.checkpoint,
        }
        self.assertEqual(self.ledger.read_bytes(), (json.dumps(expected) + "\n").encode())

    def test_a_units_second_plan_records_its_own_phase_1(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([(row.unit, row.plan, row.phase) for row in review_regime.read_rows()], [
            ("dashboard", FIRST_PLAN, "1"),
            ("dashboard", SECOND_PLAN, "1"),
        ])

    def test_two_checkpoints_of_one_phase_add_two_rows(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1", checkpoint=self.other_checkpoint)

        self.assertEqual([row.checkpoint for row in review_regime.read_rows()],
                         [self.checkpoint, self.other_checkpoint])

    def test_two_short_forms_of_the_same_checkpoint_keep_the_first_merge(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1", checkpoint=self.checkpoint[:8])
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1", checkpoint=self.other_checkpoint)
        before = self.ledger.read_bytes()

        output = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                          "--note", "repair merge", checkpoint=self.checkpoint[:12],
                          started="2026-10-12T10:00:00-04:00", merged="2026-10-13T11:00:00-04:00")

        self.assertEqual(output, f"kept dashboard phase 1 of {FIRST_PLAN}: "
                         + f"a row exists from the merge at {MERGED}; nothing added\n")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual([row.checkpoint for row in review_regime.read_rows()],
                         [self.checkpoint, self.other_checkpoint])

    def test_add_requires_a_checkpoint_before_writing(self) -> None:
        arguments = ["review_regime.py", "add", "--unit", "dashboard", "--plan", FIRST_PLAN,
                     "--phase", "1", "--regime", "after", "--started", STARTED, "--merged", MERGED,
                     "--holds", "0", "--merge-defects", "0"]
        errors = io.StringIO()
        with mock.patch("sys.argv", arguments), contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as refused:
                _ = review_regime.main()

        self.assertEqual(refused.exception.code, 2)
        self.assertIn("the following arguments are required: --checkpoint", errors.getvalue())
        self.assertFalse(self.ledger.exists())

    def test_a_row_without_a_checkpoint_counts_but_does_not_match(self) -> None:
        old = {"unit": "dashboard", "plan": FIRST_PLAN, "phase": "1", "regime": "after", "started": STARTED,
               "merged": MERGED, "holds": 0, "merge_defects": 0}
        before = json.dumps(old) + "\n"
        _ = self.ledger.write_text(before)

        output = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                          started="2026-10-12T13:00:00+00:00")
        self.assertIn("added dashboard phase 1", output)
        self.assertTrue(self.ledger.read_text().startswith(before))
        self.assertEqual([row.checkpoint for row in review_regime.read_rows()], [None, self.checkpoint])
        self.assertIn("| phases merged | 0 | 0 | 2 |", self.report())

    def test_a_repeated_phase_still_refuses_invalid_merge_times(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        before = self.ledger.read_bytes()
        for started, merged, expected in (
            ("2026-10-12T09:00:00", MERGED,
             "review_regime: --started '2026-10-12T09:00:00' needs a UTC offset"),
            (STARTED, "2026-10-12T11:00:00",
             "review_regime: --merged '2026-10-12T11:00:00' needs a UTC offset"),
            (STARTED, "2026-10-12T08:00:00-04:00", "review_regime: --merged is before --started"),
        ):
            with self.subTest(started=started, merged=merged):
                with self.assertRaises(SystemExit) as refused:
                    _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                                 started=started, merged=merged)
                self.assertEqual(str(refused.exception), expected)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_a_row_written_before_the_plan_was_recorded_reads_and_blocks_nothing(self) -> None:
        old = {"unit": "dashboard", "phase": "1", "regime": "after", "started": STARTED, "merged": MERGED,
               "holds": 0, "merge_defects": 0}
        _ = self.ledger.write_text(json.dumps(old) + "\n")

        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([row.plan for row in review_regime.read_rows()], [None, SECOND_PLAN])

    def test_add_needs_the_plan(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            _ = self.add("--unit", "dashboard", "--phase", "1")

        self.assertEqual(refused.exception.code, 2)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    _ = unittest.main()
