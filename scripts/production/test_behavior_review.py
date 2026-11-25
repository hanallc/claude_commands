"""Verify the review command with private records and a fake reviewer."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.astra import astra_review
from scripts.direct import progress_history
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase
from scripts.notify import review
from scripts.production import behavior_check, session_staff, staff, update_registration, worker
from scripts.review_links import review_events


class BehaviorReviewTests(ScriptRepositoryTestCase):
    producer: staff.StaffRow = staff.StaffRow(staff.StaffId("e31b40f3-fd25-4c3c-97e3-f80783736401"), "producer",
                                             "fixture", staff.STAFF_ROOT, current_session=staff.StaffId("claude-fixture"))
    identities: update_registration.QuestionStaff = update_registration.QuestionStaff(
        staff.StaffSnapshot((producer,)), session_staff.MemberSources(()))
    folder: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment["SHOWRUNNER_STATE_DIR"] = str(self.root / "showrunner")
        self.folder = self.root / "review-run"
        self.folder.mkdir()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))
        _ = self.enterContext(mock.patch.object(astra_review, "run_folder", return_value=self.folder))
        _ = self.enterContext(mock.patch.object(update_registration.QuestionStaff, "read", return_value=self.identities))
        _ = self.enterContext(mock.patch.object(staff, "local_snapshot", return_value=self.identities.record))
        _ = self.enterContext(mock.patch.object(staff, "initial_snapshot", return_value=self.identities.record))
        _ = self.enterContext(mock.patch.object(update_registration, "user_zone", return_value=ZoneInfo("UTC")))
        self.seed()

    def commit(self, path: str, content: str) -> str:
        _ = self.write(self.repo / path, content)
        _ = self.git("add", path)
        _ = self.git("commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def changed_range(self) -> tuple[list[str], str]:
        base = self.commit("test_fixture.py", "def test_move():\n    assert 'move now'\n")
        commit = self.commit("test_fixture.py", "def test_move():\n    assert 'wait first'\n")
        key = behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, commit)).key
        return ["--repo", str(self.repo), "--base", base, "--commit", commit], key

    def cli(self, *args: str) -> tuple[int, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(output):
            code = behavior_check.main(list(args))
        return code, output.getvalue()

    def reply(self, text: str) -> None:
        _ = self.write(self.folder / "review.md", text)

    def test_run_without_changed_tests_starts_no_worker_and_writes_no_record(self) -> None:
        base = self.git("rev-parse", "HEAD").stdout.strip()
        commit = self.commit("application.txt", "change\n")
        with mock.patch.object(astra_review, "run_review") as runner:
            status, output = self.cli("run", "--repo", str(self.repo), "--base", base, "--commit", commit)
        self.assertEqual((status, output), (0, ""))
        runner.assert_not_called()
        self.assertFalse(Path(self.environment["SHOWRUNNER_STATE_DIR"]).exists())

    def test_none_found_saves_clear_with_the_reply_and_does_not_file_a_question(self) -> None:
        args, key = self.changed_range()
        text = '[{"test":"test_fixture.py::test_move","finding":"none"}]'
        self.reply(text)
        with mock.patch.object(astra_review, "run_review", return_value=0) as runner:
            code, output = self.cli("run", *args)
            self.assertEqual(code, 0, output)
            self.assertEqual(self.cli("run", *args), (0, output))
        runner.assert_called_once()
        self.assertEqual(behavior_check.read_record(key), behavior_check.Clear(text))
        self.assertIn("move now", (self.folder / "tests.json").read_text())
        self.assertIn("+    assert 'wait first'", (self.folder / "code.diff").read_text())
        self.assertFalse(list(Path(self.environment["SHOWRUNNER_STATE_DIR"]).glob("*/questions/*")))

    def test_a_submitted_review_is_collected_without_starting_another_worker(self) -> None:
        args, key = self.changed_range()
        text = '[{"test":"test_fixture.py::test_move","finding":"none"}]\nDONE fixture-pass\n'
        with mock.patch.object(astra_review, "run_review", return_value=worker.DispatchSubmission.SUBMITTED) as runner:
            code, output = self.cli("run", *args)
            self.assertEqual(code, 1, output)
            self.assertEqual(behavior_check.read_record(key), behavior_check.NoReview.MISSING)
            with mock.patch.object(progress_history, "read_completed_reply",
                                   return_value=progress_history.ValidatedDispatchReply(text)):
                code, output = self.cli("run", *args)
        runner.assert_called_once()
        self.assertEqual(code, 0, output)
        self.assertEqual(behavior_check.read_record(key), behavior_check.Clear(text))

    def test_an_incomplete_reply_refuses_land_and_permits_a_new_review(self) -> None:
        args, key = self.changed_range()
        self.reply("[]")
        with mock.patch.object(astra_review, "run_review", return_value=0) as runner:
            code, output = self.cli("run", *args)
            self.assertEqual(code, 1, output)
            self.assertIn("missing findings", output)
            self.assertEqual(behavior_check.read_record(key), behavior_check.NoReview.MISSING)
            self.reply('[{"test":"test_fixture.py::test_move","finding":"none"}]')
            self.assertEqual(self.cli("run", *args), (0, f"{key} clear\n"))
        self.assertEqual(runner.call_count, 2)

    def test_the_specified_behavior_files_one_question_and_status_saves_the_users_answer(self) -> None:
        args, key = self.changed_range()
        source = self.write(self.root / "decision.md", "Move at once.")
        self.reply(json.dumps([{"test": "test_fixture.py::test_move", "finding": "specified", "quote": "Move at once.",
                                "source": str(source), "date": "2026-11-23", "change": "The move waits for the current step."}]))

        def accept(_key: review.QuestionKey, question: str) -> review.QuestionAccepted:
            return review.QuestionAccepted(question)

        with (mock.patch.object(astra_review, "run_review", return_value=0) as runner,
              mock.patch.object(review, "review_question", side_effect=accept) as text_check):
            code, output = self.cli("run", *args, "--production", self.producer.name)
            self.assertEqual(code, 1, output)
            self.assertIn("The land waits", output)
            waiting = behavior_check.read_record(key)
            assert isinstance(waiting, behavior_check.Waiting)
            # Retry a stop after the question was saved and before the landing record was saved.
            behavior_check.record_path(key).unlink()
            self.assertEqual(self.cli("run", *args, "--production", self.producer.name), (1, output))
        runner.assert_called_once()
        text_check.assert_called_once()
        self.assertEqual(behavior_check.read_record(key), waiting)
        records = update_registration.question_records_for_owner(self.producer.id, strict=True)
        self.assertEqual(len(records), 1)
        question = records[0]
        shown = question.item.get("question", "")
        for words in ("Move at once.", "The move waits for the current step.", "Yes. Land", "No. Keep"):
            self.assertIn(words, shown)

        update_registration.write_question_record(replace(question, status="closed"))
        for words in ("Yes", "No"):
            with self.subTest(answer=words):
                update_registration.record_review_log(question, f"answer: {words}")
                saved = update_registration.numbered_question(question.owner, waiting.question_id, question.path.parent)
                assert saved is not None
                event = review_events.decode(saved.item.get("review_log", ""))[-1]
                behavior_check.write_record(key, waiting)
                self.assertEqual(self.cli("status", "--key", key)[0], 0 if words == "Yes" else 1)
                approval = behavior_check.Approval(words.lower())
                self.assertEqual(behavior_check.read_record(key),
                                 behavior_check.Answered(waiting.question_id, approval, words, event.time))
