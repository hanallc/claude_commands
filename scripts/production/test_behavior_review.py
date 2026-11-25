"""Verify the review command with private records and a fake reviewer."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.astra import astra_review
from scripts.direct import pass_record, progress_history
from scripts.lib import unit_runs
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase
from scripts.notify import review
from scripts.production import behavior_check, behavior_review, session_staff, staff, update_registration, worker
from scripts.review_links import review_events


CLEAR_REPLY = '[{"test":"test_fixture.py::test_move","finding":"none"}]'


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
        _ = self.enterContext(mock.patch.object(unit_runs, "ASTRA_RUN_ROOT", self.root))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment, clear=True)))
        _ = self.enterContext(mock.patch.object(astra_review, "review_attempt_folder", return_value=self.folder))
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
        tests = behavior_check.changed_tests(behavior_check.GitRange(self.repo, base, commit))
        assert isinstance(tests, behavior_check.ChangedTests)
        key = tests.key
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

    def test_clear_requires_a_saved_reply_for_this_patch_and_each_test(self) -> None:
        args, key = self.changed_range()
        self.reply(CLEAR_REPLY)
        with mock.patch.object(astra_review, "run_review", return_value=0) as runner:
            code, output = self.cli("run", *args)
            self.assertEqual(code, 0, output)
            self.assertEqual(self.cli("run", *args), (0, output))
        runner.assert_called_once()
        self.assertEqual(behavior_check.read_record(key), behavior_check.Clear(self.folder / "completed-reply.txt"))
        self.assertIn("move now", (self.folder / "tests.json").read_text())
        self.assertIn("+    assert 'wait first'", (self.folder / "code.diff").read_text())
        self.assertFalse(list(Path(self.environment["SHOWRUNNER_STATE_DIR"]).glob("*/questions/*")))

        self.assertEqual(self.cli("check", *args), (0, ""))
        saved = self.folder / "completed-reply.txt"
        foreign = self.write(self.root / "another-reply.txt", CLEAR_REPLY)
        behavior_check.write_record(key, behavior_check.Clear(foreign))
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        behavior_check.write_record(key, behavior_check.Clear(saved))
        _ = self.write(self.folder / "review-key", "different patch")
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        _ = self.write(self.folder / "review-key", key)
        _ = self.write(saved, "[]")
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        saved.unlink()
        self.assertIn("no valid proof", self.cli("check", *args)[1])

    def test_a_submitted_review_is_collected_without_starting_another_worker(self) -> None:
        args, key = self.changed_range()
        text = '[{"test":"test_fixture.py::test_move","finding":"none"}]\nDONE fixture-pass\n'
        with mock.patch.object(astra_review, "run_review", return_value=worker.DispatchSubmission.SUBMITTED) as runner:
            code, output = self.cli("run", *args)
            self.assertEqual(code, 1, output)
            self.assertEqual(behavior_check.read_record(key), behavior_check.NoReview.MISSING)
            self.pass_status("active")
            self.assertIn("waits for the independent review", self.cli("run", *args)[1])
            self.pass_status("completed")
            with mock.patch.object(progress_history, "read_completed_reply",
                                   return_value=progress_history.ValidatedDispatchReply(text)):
                code, output = self.cli("run", *args)
        runner.assert_called_once()
        self.assertEqual(code, 0, output)
        self.assertEqual(behavior_check.read_record(key), behavior_check.Clear(self.folder / "completed-reply.txt"))

    def pass_status(self, status: str) -> None:
        _ = self.write(pass_record.state_path(self.folder), json.dumps({"pass": {"review": {"status": status}}}))

    def test_retries_replace_unstarted_and_failed_review_attempts(self) -> None:
        args, key = self.changed_range()
        pointer = behavior_check.record_path(key).with_suffix(".run")
        for state in ("unstarted", "canceled", "refused"):
            with self.subTest(state=state):
                prior = self.root / state
                prior.mkdir()
                _ = self.write(pointer, f"{prior}\n")
                if state != "unstarted":
                    _ = self.write(pass_record.state_path(prior), json.dumps({"pass": {"review": {"status": state}}}))
                behavior_check.record_path(key).unlink(missing_ok=True)
                self.reply(CLEAR_REPLY)
                with mock.patch.object(astra_review, "run_review", return_value=0) as runner:
                    self.assertEqual(self.cli("run", *args), (0, f"{key} clear\n"))
                runner.assert_called_once()
                self.assertEqual(pointer.read_text().strip(), str(self.folder))
                (self.folder / "completed-reply.txt").unlink()

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

    def filed_question(self) -> tuple[list[str], str, behavior_check.Waiting, update_registration.QuestionRecord]:
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

        return args, key, waiting, question

    def test_review_choice_text_maps_to_yes_and_no(self) -> None:
        _, key, waiting, question = self.filed_question()
        update_registration.write_question_record(replace(question, status="closed"))
        update_registration.record_review_log(question, "answer: Yes, please.")
        self.assertIn("without one of its filed choices", self.cli("status", "--key", key)[1])
        for words, approval in ((behavior_review.YES_CHOICE, behavior_check.Approval.YES),
                                (behavior_review.NO_CHOICE, behavior_check.Approval.NO)):
            with self.subTest(answer=words):
                update_registration.record_review_log(question, f"answer: {words}")
                saved = update_registration.numbered_question(question.owner, waiting.question_id, question.path.parent)
                assert saved is not None
                event = review_events.decode(saved.item.get("review_log", ""))[-1]
                behavior_check.write_record(key, waiting)
                self.assertEqual(self.cli("status", "--key", key)[0], 0 if approval is behavior_check.Approval.YES else 1)
                self.assertEqual(behavior_check.read_record(key),
                                 behavior_check.Answered(waiting.question_id, waiting.owner, approval, words, event.time))


    def test_yes_requires_the_original_closed_question_and_the_users_yes(self) -> None:
        args, key, waiting, question = self.filed_question()
        forged = behavior_check.Answered(waiting.question_id, waiting.owner, behavior_check.Approval.YES,
                                         behavior_review.YES_CHOICE, datetime.now(timezone.utc))
        behavior_check.write_record(key, forged)
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        update_registration.write_question_record(replace(question, status="closed"))
        update_registration.record_review_log(question, f"answer: {behavior_review.YES_CHOICE}", who="producer")
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        update_registration.record_review_log(question, f"answer: {behavior_review.YES_CHOICE}")
        self.assertEqual(self.cli("check", *args), (0, ""))
        update_registration.record_review_log(question, f"answer: {behavior_review.NO_CHOICE}")
        self.assertIn("no valid proof", self.cli("check", *args)[1])
