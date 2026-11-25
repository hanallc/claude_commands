"""Check the filing patterns and the shared model review."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from scripts.console_text import check
from scripts.notify import review


class ConsoleTextCheckTests(unittest.TestCase):
    def test_point_and_small_yes_no_length(self) -> None:
        point = "Your download needs 2.5 GB. The disk is full. The download waits"
        self.assertEqual(check.fast_check("Start the download?\n\n" + point, "question"), ())
        extra = point + ". Other downloads still work."
        self.assertEqual(check.fast_check("Start the download?\n\n" + extra, "question"),
                         (review.RuleFailure("point-at-most-three-sentences", extra),))

        facts = "1. The test takes one minute.\n2. You can keep working.\n3. It runs on your Mac."
        choices = "\n\nChoices:\n- Yes, start it.\n- No, wait."
        title = "May the test start?\n\n"
        self.assertEqual(check.fast_check(title + facts + choices, "question"), ())
        extra_fact = "4. You can cancel it."
        self.assertEqual(check.fast_check(title + facts + "\n" + extra_fact + choices, "question"),
                         (review.RuleFailure("yes-no-at-most-three-fact-lines", extra_fact),))

    def test_command_patterns_require_the_user_to_run_the_command(self) -> None:
        for command in ("`pwd`", "`git status`", "git log --oneline", "$ git status", "```sh\npwd\n```"):
            with self.subTest(command=command):
                failures = check.fast_check(command, "reply")
                self.assertEqual([failure.rule for failure in failures], ["command-for-user-only"])
                self.assertIn(failures[0].quote, command)
                self.assertEqual(check.fast_check("Run this command:\n\n" + command, "reply"), ())
        text = "Run this command: `cat /tmp/check.txt`\nRun this command: `git status`"
        self.assertEqual(check.fast_check(text, "reply"),
                         (review.RuleFailure("command-for-user-only", "`git status`"),))

    def test_paths_commits_runs_and_sessions(self) -> None:
        for rule, rejected, accepted in (
                ("omit-file-path", "/tmp/layout.toml", "the layout settings"),
                ("omit-file-path", "scripts/build.py", "the build command"),
                ("omit-commit-hash", "07ffc9e69e", "build 20261125"),
                ("omit-run-id", "20261120T183844-3231580-a4b0", "20 November at 18:38"),
                ("omit-run-id", "20261120T183844-3231580-a4b0@01a1b67d-cd80-7e90-805b-0a4f8d9169c5", "the test"),
                ("omit-session-id", "da479fe0-42f8-492c-8634-a721a2c3bbac", "your editor")):
            with self.subTest(rejected=rejected):
                self.assertEqual(check.fast_check(f"The result is in {rejected}.", "reply"),
                                 (review.RuleFailure(rule, rejected),))
                self.assertEqual(check.fast_check(f"The result is in {accepted}.", "reply"), ())
        self.assertEqual(check.fast_check("Open the image at https://example.org/image/07ffc9e69e.", "image caption"), ())

    def test_paths_require_a_file_sign(self) -> None:
        for text in ("The date is 11/25/2026.", "Open Tools/Audio/Output."):
            with self.subTest(text=text):
                self.assertEqual(check.fast_check(text, "reply"), ())
        for path in ("/tmp/proof", "~/notes/proof", "./notes/proof", "scripts/review_links/check.py"):
            with self.subTest(path=path):
                self.assertEqual(check.fast_check(f"Open {path}.", "reply"),
                                 (review.RuleFailure("omit-file-path", path),))

    def test_fast_only_kinds_skip_the_model(self) -> None:
        text = "The image shows the old layout."
        with patch.object(review, "review_question") as reviewer:
            for kind in ("list field", "efficiency candidate"):
                with self.subTest(kind=kind):
                    self.assertEqual(check.check(text, kind), review.QuestionAccepted(text))
            reviewer.assert_not_called()

    def test_console_reader_holds_a_rewrite_with_its_quote(self) -> None:
        text = "Step B finished."
        key = review.QuestionKey("example", "Review the layout")
        failure = review.RuleFailure("console-standard-5", "Step B")
        response = review.VerdictRecord(
            verdict="rewrite", title="", action=None, body="The display is ready.", questions=[],
            failed_rules=[{"rule": failure.rule, "quote": failure.quote}],
        )
        call = review.StructuredCall("opus", review.StructuredAnswer(response))

        def guide(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "Question rules", "")

        with TemporaryDirectory() as folder:
            root = Path(folder)
            with (patch.object(review, "state_path", return_value=root),
                  patch.object(review, "GUIDE", root),
                  patch.object(review, "reviewer_environment", return_value=({}, "fixture")),
                  patch.object(review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(root / "claude")),
                  patch.object(review, "reviewer_assignment", return_value=("opus", "medium")),
                  patch.object(review, "structured_call", return_value=call) as reader):
                result = review.review_question(key, text, runner=guide, console_kind="reply")
            self.assertEqual(result, review.QuestionReviewFailed(text, (), (failure,)))
            reader.assert_called_once()
            system_prompt = cast(str, reader.call_args.kwargs["system_prompt"])
            self.assertIn(review.CONSOLE_STANDARD.read_text(), system_prompt)
            self.assertIn("console-standard-N", system_prompt)
            self.assertIn("Console text kind: reply", cast(str, reader.call_args.kwargs["prompt"]))
            hold = root / "questions" / f"{key.digest}.json"
            self.assertIn("console-standard-5: Step B", hold.read_text())

    def test_console_reader_checks_a_separate_title(self) -> None:
        title = "Phase 15 is ready"
        text = "The display is ready."
        draft = f"## {title}\n\n{text}"
        key = review.QuestionKey("example", title)
        failure = review.RuleFailure("console-standard-5", "Phase 15")
        with patch.object(review, "review_question", side_effect=(
                review.QuestionReviewFailed(draft, (), (failure,)), review.QuestionAccepted(draft))) as reader:
            self.assertEqual(check.check(text, "question", key=key, title=title),
                             review.QuestionReviewFailed(text, (), (failure,)))
            reader.assert_called_once_with(key, draft, console_kind="question")
            self.assertEqual(check.check(text, "question", key=key, title=title),
                             review.QuestionAccepted(text))
