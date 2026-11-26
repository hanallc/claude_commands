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
from scripts.lib import process_start, unit_runs
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase
from scripts.notify import review
from scripts.production import behavior_check, behavior_review, session_staff, staff, update_registration, worker
from scripts.review_links import reply_proofs, review_events


CLEAR_REPLY = '[{"test":"test_fixture.py::test_move","finding":"none"}]'
REVIEWER = "fixture-reviewer"
MOVE_QUOTE = "Move at once. Use scripts/direct/assign.py to restart the work."
MOVE_QUESTION = update_registration.QuestionParts(
    ask="Allow a wait before an account move? (2026-11-23)",
    context="You set a rule for immediate moves and restarts. A wait lets the running build finish.",
    items=["A build can take 90 minutes."],
    choices=["Yes (the change goes in: wait for the build)", "No (the change stays out: move at once)"],
    recommend="Yes, because the unit can finish the build.",
)


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
        self.environment["ASTRA_ROLES_STATE"] = str(self.root / "astra-roles")
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

    def commit(self, path: str, content: str, message: str = "fixture") -> str:
        _ = self.write(self.repo / path, content)
        _ = self.git("add", path)
        _ = self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def changed_range(self, *, phase_merge: bool = False) -> tuple[list[str], str]:
        base = self.commit("test_fixture.py", "def test_move():\n    assert 'move now'\n", "Set the current behavior.")
        if phase_merge:
            _ = self.git("switch", "-c", "unit")
        test_edit = self.commit("test_fixture.py", "def test_move():\n    assert 'wait first'\n", "Change move timing.")
        if phase_merge:
            unit_tip = self.commit("test_fixture.py", "def test_move():\n    assert 'wait first'\n\nVALUE = 1\n", "Set a value.")
            _ = self.git("switch", "main")
            _ = self.git("merge", "--no-ff", "unit", "-m", f"Merge moves phase 7 ({unit_tip[:7]}) into production")
            commit = self.git("rev-parse", "HEAD").stdout.strip()
        else:
            commit = test_edit
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

    def question_file(self, parts: update_registration.QuestionParts) -> Path:
        return self.write(self.root / "question.json", json.dumps(parts))

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
        prompt = cast(str, runner.call_args.args[1])
        self.assertIn("Run each shell command to completion in one tool call.", prompt)
        self.assertIn("`timeout --kill-after=1s 20s` and set `yield_time_ms` to 25000", prompt)
        self.assertIn("Never call `write_stdin`.", prompt)
        self.assertIn("rg --search-zip --files-with-matches --hidden --no-ignore", prompt)
        self.assertIn("Parse only those files for matching user turns.", prompt)
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
            running = process_start.process_start(os.getpid())
            assert isinstance(running, process_start.Started)
            self.reviewer_process(running.start)
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
        record = {"status": status, "delivery": {"actions": {"held_role": REVIEWER}}}
        _ = self.write(pass_record.state_path(self.folder), json.dumps({"pass": {"review": record}}))

    def reviewer_process(self, start: str) -> None:
        path = Path(self.environment["ASTRA_ROLES_STATE"]) / f"{REVIEWER}.json"
        _ = self.write(path, json.dumps({"staff_id": REVIEWER, "pid": os.getpid(), "pid_start": start}))

    def test_a_dead_reviewer_or_reused_pid_allows_a_new_dispatch(self) -> None:
        args, key = self.changed_range()
        pointer = behavior_check.record_path(key).with_suffix(".run")
        prior = self.root / "prior-review"
        normal_folder = self.folder
        self.reviewer_process("original-start")
        self.reply(CLEAR_REPLY)
        for observed in (process_start.Gone(), process_start.Zombie("original-start"),
                         process_start.Started("different-start")):
            with self.subTest(observed=type(observed).__name__):
                _ = self.write(pointer, f"{prior}\n")
                self.folder = prior
                self.pass_status("active")
                self.folder = normal_folder
                with (mock.patch.object(process_start, "process_start", return_value=observed) as process,
                      mock.patch.object(astra_review, "run_review", return_value=0) as runner):
                    self.assertEqual(self.cli("run", *args), (0, f"{key} clear\n"))
                process.assert_called_once_with(os.getpid())
                runner.assert_called_once()
                self.assertEqual(pointer.read_text().strip(), str(normal_folder))
                behavior_check.record_path(key).unlink()
                (normal_folder / "completed-reply.txt").unlink()

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
            self.reply(json.dumps([{"test": "test_fixture.py::test_move", "finding": "specified", "quote": "Move at once.",
                                    "source": str(self.root / "decision.md"), "date": "2026-11-23",
                                    "change": "The move now waits.\nAn extra line."}]))
            code, output = self.cli("run", *args)
            self.assertEqual(code, 1, output)
            self.assertIn("one line about the change", output)
            self.assertFalse(behavior_check.record_path(key).with_suffix(".run").exists())
            self.reply('[{"test":"test_fixture.py::test_move","finding":"none"}]')
            self.assertEqual(self.cli("run", *args), (0, f"{key} clear\n"))
        self.assertEqual(runner.call_count, 3)

    def filed_question(self) -> tuple[list[str], str, behavior_check.Waiting, update_registration.QuestionRecord]:
        args, key = self.changed_range()
        self.specified_move_reply()
        draft = self.question_file(MOVE_QUESTION)

        def accept(_key: review.QuestionKey, question: str, *, console_kind: str) -> review.QuestionAccepted:
            self.assertEqual(console_kind, "question")
            return review.QuestionAccepted(question)

        with (mock.patch.object(astra_review, "run_review", return_value=0) as runner,
              mock.patch.object(review, "review_question", side_effect=accept) as text_check):
            code, output = self.cli("run", *args, "--production", self.producer.name, "--question", str(draft))
            self.assertEqual(code, 1, output)
            self.assertIn("The land waits", output)
            waiting = behavior_check.read_record(key)
            assert isinstance(waiting, behavior_check.Waiting)
            draft.unlink()
            self.assertEqual(self.cli("run", *args), (1, output))
            # Recover a question filed before the command could save the landing record.
            behavior_check.record_path(key).unlink()
            self.assertEqual(self.cli("run", *args), (1, output))
        runner.assert_called_once()
        prompt = cast(str, runner.call_args.args[1])
        self.assertIn("write one plain line for the author about how the behavior changes", prompt)
        self.assertFalse((self.folder / "commit-messages.txt").exists())
        text_check.assert_called_once()
        self.assertEqual(behavior_check.read_record(key), waiting)
        records = update_registration.question_records_for_owner(self.producer.id, strict=True)
        self.assertEqual(len(records), 1)
        question = records[0]
        self.assertEqual(question.item.get("parts"), MOVE_QUESTION)
        self.assertIn(MOVE_QUOTE, (self.folder / "completed-reply.txt").read_text())
        self.assertNotIn("scripts/direct/assign.py", question.item.get("question", ""))
        self.assertNotIn("test_move", question.item.get("question", ""))
        self.assertNotIn("test_fixture.py", question.item.get("question", ""))
        return args, key, waiting, question

    def specified_move_reply(self) -> None:
        source = self.write(self.root / "decision.md", MOVE_QUOTE)
        self.reply(json.dumps([{"test": "test_fixture.py::test_move", "finding": "specified", "quote": MOVE_QUOTE,
                                "source": str(source), "date": "2026-11-23",
                                "change": "The move waits for the current step."}]))

    def test_a_finding_without_a_question_reports_its_words_and_unit(self) -> None:
        args, key = self.changed_range(phase_merge=True)
        self.specified_move_reply()
        with (mock.patch.object(astra_review, "run_review", return_value=0),
              mock.patch.object(update_registration, "ask") as ask):
            code, output = self.cli("run", *args)
        self.assertEqual(code, 1)
        self.assertIn(f"Nate's words (2026-11-23, {self.root / 'decision.md'}): {MOVE_QUOTE}", output)
        self.assertIn("Change: The move waits for the current step.\nAsk moves (phase 7)\n", output)
        self.assertIn("Nate was not asked. Write the question for nate in the question template as JSON, "
                      + "then run again with --question FILE.\n", output)
        ask.assert_not_called()
        self.assertEqual(behavior_check.read_record(key), behavior_check.NoReview.MISSING)

    def test_failed_history_lookup_keeps_the_finding_and_names_the_tip(self) -> None:
        path = "test_fixture.py"
        base = self.commit(path, "def test_move():\n    assert 'move now'\n")
        updated = "def test_move():\n    assert 'wait first'\n"
        _ = self.commit(path, updated + "\n" + updated, "Duplicate the test.")
        head = self.commit(path, updated, "Keep one test definition.")
        args = ["--repo", str(self.repo), "--base", base, "--commit", head]
        self.specified_move_reply()
        with mock.patch.object(astra_review, "run_review", return_value=0) as runner:
            code, output = self.cli("run", *args)
            self.assertEqual(code, 1)
            self.assertIn(f"{MOVE_QUOTE}\nChange: The move waits for the current step.\n"
                          + f"{head} Keep one test definition.\nNate was not asked.", output)
            with mock.patch.object(behavior_check, "describe_tip", side_effect=OSError("Cannot read the subject.")):
                code, output = self.cli("run", *args)
            self.assertEqual(code, 1)
            self.assertIn(f"{MOVE_QUOTE}\nChange: The move waits for the current step.\n"
                          + f"Ask the author of {head}\nNate was not asked.", output)
        runner.assert_called_once()

    def test_a_question_requires_both_choice_starts_and_the_date(self) -> None:
        args, key = self.changed_range()
        self.specified_move_reply()
        yes, no = MOVE_QUESTION["choices"]
        choice_error = 'start one choice with "Yes (the change goes in" and one with "No (the change stays out".'
        cases: list[tuple[update_registration.QuestionParts, str]] = [
            ({**MOVE_QUESTION, "choices": ["Yes. Keep moving at once.", no]}, choice_error),
            ({**MOVE_QUESTION, "choices": [yes, "No (wait for the build)"]}, choice_error),
            ({**MOVE_QUESTION, "choices": ["Yes (the change goes inside)", no]}, choice_error),
            ({**MOVE_QUESTION, "choices": [yes, no, "Later"]}, choice_error),
            ({**MOVE_QUESTION, "ask": "Allow a wait before an account move?"}, "omit 2026-11-23"),
        ]
        with (mock.patch.object(astra_review, "run_review", return_value=0) as runner,
              mock.patch.object(update_registration, "ask") as ask):
            for parts, reason in cases:
                with self.subTest(reason=reason):
                    code, output = self.cli("run", *args, "--question", str(self.question_file(parts)))
                    self.assertEqual(code, 1)
                    self.assertIn(reason, output)
        runner.assert_called_once()
        ask.assert_not_called()
        self.assertEqual(behavior_check.read_record(key), behavior_check.NoReview.MISSING)

    def test_a_console_hold_reports_that_nate_was_not_asked_and_refuses_land(self) -> None:
        args, key = self.changed_range()
        self.specified_move_reply()
        draft = self.question_file(MOVE_QUESTION)
        hold = review.QuestionAwaitingFacts("draft", ("Explain the wait.",))
        with (mock.patch.object(astra_review, "run_review", return_value=0),
              mock.patch.object(review, "review_question", return_value=hold) as text_check):
            code, output = self.cli("run", *args, "--production", self.producer.name, "--question", str(draft))
        text_check.assert_called_once()
        self.assertEqual(code, 1)
        self.assertIn("question held for review", output)
        self.assertIn("Explain the wait.", output)
        self.assertIn(f"{args[-1]} Change move timing.\n", output)
        self.assertIn(f"Nate was not asked. Revise {draft} and run again.\n", output)
        self.assertEqual(update_registration.question_records_for_owner(self.producer.id, strict=True), [])
        self.assertEqual(behavior_check.read_record(key), behavior_check.NoReview.MISSING)
        self.assertEqual(self.cli("check", *args)[0], 1)

    def test_review_choice_text_maps_to_yes_and_no(self) -> None:
        _, key, waiting, question = self.filed_question()
        update_registration.write_question_record(replace(question, status="closed"))
        parts = question.item.get("parts")
        assert parts is not None
        yes, no = parts["choices"]
        for words, approval in ((yes, behavior_check.Approval.YES), (no, behavior_check.Approval.NO)):
            with self.subTest(answer=words):
                update_registration.record_review_log(question, f"answer: {words}")
                saved = update_registration.numbered_question(question.owner, waiting.question_id, question.path.parent)
                assert saved is not None
                event = review_events.decode(saved.item.get("review_log", ""))[-1]
                behavior_check.write_record(key, waiting)
                self.assertEqual(self.cli("status", "--key", key)[0], 0 if approval is behavior_check.Approval.YES else 1)
                self.assertEqual(behavior_check.read_record(key),
                                 behavior_check.Answered(waiting.question_id, waiting.owner, approval, words, event.time))

    def test_status_collects_an_answer_when_only_review_saved_the_question(self) -> None:
        args, key, waiting, question = self.filed_question()
        behavior_check.record_path(key).unlink()
        self.assertEqual(self.cli("status", "--key", key), (1, f"The land waits for Nate's Yes or No on {waiting.question_id}.\n"))
        self.assertEqual(behavior_check.read_record(key), waiting)

        behavior_check.record_path(key).unlink()
        update_registration.write_question_record(replace(question, status="closed"))
        update_registration.record_review_log(question, "answer: Yes")
        self.assertEqual(self.cli("status", "--key", key), (0, f"{key} yes ({waiting.question_id})\n"))
        saved = behavior_check.read_record(key)
        assert isinstance(saved, behavior_check.Answered)
        self.assertEqual(saved.approval, behavior_check.Approval.YES)
        self.assertEqual(saved.question_id, waiting.question_id)
        self.assertEqual(self.cli("check", *args), (0, ""))

    def test_the_first_word_sets_the_verdict_and_other_answers_record_a_refusal(self) -> None:
        args, key, waiting, question = self.filed_question()
        update_registration.write_question_record(replace(question, status="closed"))
        words = "Yesterday was too soon.\nPlease explain."
        update_registration.record_review_log(question, f"answer: {words}")
        reason = f"Nate's answer to {waiting.question_id}, {words!r}, is not Yes or No."
        self.assertEqual(self.cli("run", *args), (1, reason + "\n"))
        self.assertEqual(self.cli("status", "--key", key), (1, reason + "\n"))
        saved = behavior_check.read_record(key)
        self.assertEqual(saved, behavior_check.InvalidAnswer(waiting.question_id, waiting.owner, reason))
        status, output = self.cli("check", *args)
        self.assertEqual(status, 1)
        self.assertIn(reason, output)

        for words, approval in (("  yEs  ", behavior_check.Approval.YES),
                                ("Yes, please.", behavior_check.Approval.YES),
                                ("  nO  ", behavior_check.Approval.NO),
                                ("NO… keep it.", behavior_check.Approval.NO)):
            with self.subTest(answer=words):
                update_registration.record_review_log(question, f"answer: {words}")
                code = 0 if approval is behavior_check.Approval.YES else 1
                self.assertEqual(self.cli("run", *args), (code, f"{key} {approval} ({waiting.question_id})\n"))
                status, output = self.cli("check", *args)
                self.assertEqual(status, code, output)
                if approval is behavior_check.Approval.NO:
                    self.assertIn("answered No", output)
                behavior_check.write_record(key, waiting)

    def test_questions_with_the_same_behavior_title_keep_distinct_request_keys(self) -> None:
        _, key, first, question = self.filed_question()
        title = question.item["text"]
        second_key = "e" * 64
        keys: list[review.QuestionKey] = []

        def accept(key: review.QuestionKey, text: str, *, console_kind: str) -> review.QuestionAccepted:
            self.assertEqual(console_kind, "question")
            keys.append(key)
            return review.QuestionAccepted(text)

        with mock.patch.object(review, "review_question", side_effect=accept):
            second = behavior_review.ask(second_key, MOVE_QUESTION, self.producer.name)
            self.assertEqual(behavior_review.ask(second_key, MOVE_QUESTION, self.producer.name), second)
        self.assertEqual(len(keys), 1)
        self.assertIn(second_key, keys[0].text)
        self.assertNotEqual(first.question_id, second.question_id)
        scope = update_registration.ProducerQuestions(self.producer)
        records = update_registration.native_question_records(scope, self.identities, strict=True)
        by_id = {record.item.get("review_id"): record for record in records}
        self.assertTrue(update_registration.question_request(by_id[first.question_id]) == key)
        self.assertTrue(update_registration.question_request(by_id[second.question_id]) == second_key)
        self.assertTrue(all("request_key" not in record.path.read_text() for record in records))
        self.assertEqual(by_id[second.question_id].item["text"], title)
        self.assertTrue(all(record.status == "open" for record in records))


    def test_move_restores_staff_question_retry_and_answer(self) -> None:
        _, key, waiting, question = self.filed_question()
        clean = question.path.read_text()
        identity = reply_proofs.StaffQuestionIdentity(question.owner, waiting.question_id)
        reply_proofs.path_for(identity).unlink()
        old = clean.replace("registration: {", "registration: {" + json.dumps({"request_key": key})[1:-1] + ", ", 1)
        _ = question.path.write_text(old)
        with self.assertRaisesRegex(behavior_check.BehaviorCheckError, "Cannot read the behavior question"):
            _ = behavior_review.question_answer(key, waiting.owner, waiting.question_id)
        self.assertEqual(update_registration.move_reply_proof(question.path, dry_run=True), 1)
        self.assertTrue(question.path.read_text() == old)
        self.assertFalse(reply_proofs.path_for(identity).exists())
        self.assertEqual(update_registration.move_reply_proof(question.path, dry_run=False), 1)
        self.assertEqual(update_registration.move_reply_proof(question.path, dry_run=False), 0)
        self.assertFalse("request_key" in question.path.read_text())
        self.assertEqual(behavior_review.ask(key, MOVE_QUESTION, self.producer.name), waiting)
        self.assertEqual(behavior_review.question_answer(key, waiting.owner, waiting.question_id), waiting)

    def test_yes_requires_the_original_closed_question_and_the_users_yes(self) -> None:
        args, key, waiting, question = self.filed_question()
        forged = behavior_check.Answered(waiting.question_id, waiting.owner, behavior_check.Approval.YES,
                                         "Yes", datetime.now(timezone.utc))
        behavior_check.write_record(key, forged)
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        update_registration.write_question_record(replace(question, status="closed"))
        update_registration.record_review_log(question, "answer: Yes", who="producer")
        self.assertIn("no valid proof", self.cli("check", *args)[1])
        update_registration.record_review_log(question, "answer: Yes")
        self.assertEqual(self.cli("check", *args), (0, ""))
        update_registration.record_review_log(question, "answer: No")
        self.assertIn("no valid proof", self.cli("check", *args)[1])
