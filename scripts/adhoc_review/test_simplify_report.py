"""Test simplification reports with a fixture config and a fake sender."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from subprocess import DEVNULL, PIPE, CompletedProcess
import sys
from tempfile import TemporaryDirectory
from typing import cast, final
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from scripts.adhoc_review import simplify_report


REPORT_ID = "fdb79e3a87e649b0b4f322a935591533"
SENDER = "fixture reviewer"
RECIPIENT = "fixture editor"
BEFORE = "The editor surface becomes unavailable on monitor disconnect.\n\nmove / leave hidden / elaborate / simplify"
AFTER = "The editor disappears when you unplug its monitor.\n\nmove / leave hidden / elaborate / simplify"
FAILURE = "The first item used internal words for the editor."
FEEDBACK = "Please name the monitor before you describe the editor."


@final
class ReviewFixture:
    def __init__(self, test: unittest.TestCase) -> None:
        root = Path(test.enterContext(TemporaryDirectory()))
        _ = cast(object, test.enterContext(patch.dict(os.environ, {"CONF_DIR": str(root)})))
        self.config = root / "adhoc_review.conf"
        self.set_recipient(RECIPIENT)
        self.before = root / "before.md"
        self.after = root / "after.md"
        self.words = root / "feedback.txt"
        _ = self.before.write_text(BEFORE, encoding="utf-8")
        _ = self.after.write_text(AFTER, encoding="utf-8")
        _ = self.words.write_text(FEEDBACK, encoding="utf-8")
        _ = test.enterContext(patch.object(simplify_report, "uuid4", return_value=UUID(REPORT_ID)))
        self.sender = Mock(return_value=CompletedProcess([], 0, "SENT: fixture delivery\n"))
        _ = test.enterContext(patch.object(simplify_report, "run", new=self.sender))
        self.stdout = test.enterContext(redirect_stdout(StringIO()))
        self.stderr = test.enterContext(redirect_stderr(StringIO()))

    def set_recipient(self, name: str) -> None:
        _ = self.config.write_text(f"simplify_report_recipient={name}\n", encoding="utf-8")

    def report(self) -> int:
        return simplify_report.main(["--from", SENDER, "report", "--before", str(self.before),
                                     "--after", str(self.after), "--failure", FAILURE])

    def feedback(self, report_id: str) -> int:
        return simplify_report.main(["--from", SENDER, "feedback", "--report", report_id,
                                     "--item", str(self.after), "--words", str(self.words)])


class SimplifyReportTests(unittest.TestCase):
    def test_report_sends_both_versions_and_failure(self) -> None:
        fixture = ReviewFixture(self)
        self.assertEqual(fixture.report(), 0)
        fixture.sender.assert_called_once_with(
            [sys.executable, str(simplify_report.SEND_SCRIPT), f"--to={RECIPIENT}", f"--from={SENDER}"],
            input=(f"Ad hoc review simplify report {REPORT_ID}\n\n"
                   f"Why the first item failed\n{FAILURE}\n\n"
                   f"Before\n{BEFORE}\n\nAfter\n{AFTER}"),
            encoding="utf-8", stdout=PIPE, stderr=DEVNULL, check=False,
        )
        self.assertEqual(fixture.stdout.getvalue(), REPORT_ID + "\n")
        self.assertEqual(fixture.stderr.getvalue(), "")

    def test_feedback_sends_exact_words_with_the_same_item_and_report_id(self) -> None:
        fixture = ReviewFixture(self)
        self.assertEqual(fixture.report(), 0)
        report_id = fixture.stdout.getvalue().strip()
        fixture.sender.reset_mock()
        self.assertEqual(fixture.feedback(report_id), 0)
        fixture.sender.assert_called_once_with(
            [sys.executable, str(simplify_report.SEND_SCRIPT), f"--to={RECIPIENT}", f"--from={SENDER}"],
            input=(f"Ad hoc review simplify feedback for report {REPORT_ID}\n\n"
                   f"Simplified item\n{AFTER}\n\nUser feedback\n{FEEDBACK}"),
            encoding="utf-8", stdout=PIPE, stderr=DEVNULL, check=False,
        )
        self.assertEqual(fixture.stdout.getvalue(), REPORT_ID + "\n")
        self.assertEqual(fixture.stderr.getvalue(), "")

    def test_report_prints_id_for_confirmed_queued_or_kept_send(self) -> None:
        fixture = ReviewFixture(self)
        for status, output in ((1, "QUEUED: fixture offline\n"), (0, "KEPT: fixture hold\n")):
            with self.subTest(output=output):
                fixture.sender.return_value = CompletedProcess([], status, output)
                self.assertEqual(fixture.report(), 0)
        self.assertEqual(fixture.stdout.getvalue(), (REPORT_ID + "\n") * 2)
        self.assertEqual(fixture.stderr.getvalue(), "")

    def test_report_refuses_an_unconfirmed_exit_one_or_a_skipped_send(self) -> None:
        fixture = ReviewFixture(self)
        for status, output in ((1, "child process error\n"), (0, "SKIPPED: fixture skipped\n")):
            with self.subTest(output=output):
                fixture.sender.return_value = CompletedProcess([], status, output)
                self.assertNotEqual(fixture.report(), 0)
        self.assertEqual(fixture.stdout.getvalue(), "")
        self.assertEqual(
            fixture.stderr.getvalue().splitlines(),
            [
                "simplify_report.py: send.py did not confirm delivery or storage (exit 1)",
                "simplify_report.py: send.py returned SKIPPED",
            ],
        )

    def test_reports_read_the_recipient_from_the_current_fixture_config(self) -> None:
        fixture = ReviewFixture(self)
        self.assertEqual(fixture.report(), 0)
        fixture.set_recipient("other fixture editor")
        self.assertEqual(fixture.report(), 0)
        self.assertEqual(
            [call.args[0][2] for call in fixture.sender.call_args_list],
            [f"--to={RECIPIENT}", "--to=other fixture editor"],
        )

    def test_report_refuses_missing_recipient_key(self) -> None:
        fixture = ReviewFixture(self)
        _ = fixture.config.write_text("# No recipient.\n", encoding="utf-8")
        self.assertEqual(fixture.report(), 2)
        fixture.sender.assert_not_called()
        self.assertEqual(fixture.stdout.getvalue(), "")
        self.assertIn("requires simplify_report_recipient", fixture.stderr.getvalue())

    def test_feedback_refuses_a_malformed_report_id(self) -> None:
        fixture = ReviewFixture(self)
        self.assertEqual(fixture.feedback("g" + REPORT_ID[1:]), 2)
        fixture.sender.assert_not_called()
        self.assertIn("--report requires 32 hexadecimal characters", fixture.stderr.getvalue())
