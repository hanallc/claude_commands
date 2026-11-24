"""Test shared page questions with private records and a fake sender."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
import threading
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import call, patch

from scripts.lib import atomic_file, process_start
from scripts.message import send_result
from scripts.production import delivery_state, staff
from scripts.review_links import change_page, image_store, page_question, question_record


def question_outcome(outcome: send_result.Outcome = "sent", detail: str = "fixture receipt"
                     ) -> tuple[send_result.ConfirmedSendOutcome, str]:
    return send_result.ConfirmedSendOutcome(send_result.OutcomeHeader(outcome, detail, "")), detail


class FakeQuestionTransport:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.receipts: set[str] = set()

    def send(self, _recipient: staff.StaffId, _message: str, message_id: str
             ) -> tuple[send_result.ConfirmedSendOutcome, str]:
        self.calls.append(message_id)
        self.receipts.add(message_id)
        return question_outcome()


class FixtureStore:
    def __init__(self) -> None:
        self.items: dict[str, tuple[question_record.Question, ...]] = {}

    def questions(self, item: str) -> tuple[question_record.Question, ...]:
        return self.items.get(item, ())

    def add(self, item: str, question: question_record.Question) -> None:
        if any(saved.question_id == question.question_id for saved in self.questions(item)):
            raise question_record.AnswerRefused('duplicate id')
        self.items[item] = (*self.questions(item), question)

    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        questions = self.questions(item)
        current = next((question for question in questions if question.question_id == question_id), None)
        if current is None or current.answer is not None:
            raise question_record.AnswerRefused('not an open question')
        self.items[item] = tuple(replace(question, answer=answer) if question.question_id == question_id else question
                                 for question in questions)


class PageQuestionTests(unittest.TestCase):
    root: Path = Path()
    store: FixtureStore = FixtureStore()
    status: change_page.DeliveryStatus = change_page.DeliveryStatus(Path())
    sent: list[tuple[staff.StaffId, str, str]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='page-question-')))
        config = self.root / 'config'
        config.mkdir()
        _ = (config / 'review_links.conf').write_text('# whole: 1..20\ndelivery_tries=2\n# range: 0..300\ndelivery_pause_seconds=0\n')
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'CONF_DIR': str(config)})))
        self.store = FixtureStore()
        self.status = change_page.DeliveryStatus(self.root / 'delivery')
        self.sent = []
        _ = self.enterContext(patch.object(change_page, "send_question", side_effect=self.capture))

    def capture(self, recipient: staff.StaffId, message: str, message_id: str
                ) -> tuple[send_result.ConfirmedSendOutcome, str]:
        self.sent.append((recipient, message_id, message))
        return question_outcome()

    def ask(self, item: str = 'image-1', words: str = 'Which check passed?') -> change_page.MessageDelivery:
        target = change_page.QuestionDeliveryTarget(self.root / item, item, staff.StaffId('fixture-owner'),
            'Images '+item, 'The gate', 'images.py answer --id {item} --question {question_id} --text "<answer>"')
        return page_question.ask(self.store, target, words, self.status)

    def test_question_ids_start_at_one_for_each_item_and_increase_after_answers(self) -> None:
        _ = self.ask()
        _ = self.ask('image-2')
        result = page_question.answer(self.store, 'image-1', 'Q1', 'The gate passed.')
        self.assertIsInstance(result, page_question.Answered)
        _ = self.ask(words='Which repair worked?')
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1', 'Q2'])
        self.assertEqual([question.question_id for question in self.store.questions('image-2')], ['Q1'])

    def test_ask_saves_before_sending_and_a_repeat_sends_only_once(self) -> None:
        def capture(recipient: staff.StaffId, message: str, message_id: str
                    ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            self.assertEqual(self.store.questions("image-1")[0].words, "Which check passed?")
            self.assertEqual(self.attempt(delivery).state, delivery_state.QuestionSendState.SENDING)
            return self.capture(recipient, message, message_id)

        with patch.object(change_page, "send_question", side_effect=capture):
            delivery = self.ask()
            self.assertEqual(self.sent, [])
            change_page.deliver(delivery, self.status)
            change_page.deliver(self.ask(), self.status)
            self.status = change_page.DeliveryStatus(self.status.root)
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.assertEqual(self.sent[0][2], 'Images image-1: Ask question\nThe gate\nWhich check passed?\n'
            +'Answer with this command: images.py answer --id image-1 --question Q1 --text "<answer>"')
        self.assertEqual(self.status.slots(delivery.page, delivery.row).intents(), ())

    def attempt(self, delivery: change_page.MessageDelivery) -> delivery_state.QuestionAttempt:
        return change_page.read_question_attempt(self.status.path(delivery.page, delivery.row))

    def effective(self, delivery: change_page.MessageDelivery) -> delivery_state.QuestionSendState:
        with self.status.locked():
            return change_page.question_delivery_view(self.status, self.job(delivery).delivery.saved).state

    def job(self, delivery: change_page.MessageDelivery) -> change_page.QuestionJob:
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        return delivery.job

    def test_failed_delivery_keeps_the_question_for_a_retry(self) -> None:
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "owner offline")) as sender,
              patch.object(change_page, "alert_delivery_failures")):
            delivery = self.ask()
            change_page.deliver(delivery, self.status)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(len(self.store.questions("image-1")), 1)
        self.assertIsNone(self.store.questions("image-1")[0].answer)
        self.assertFalse(self.status.question_jobs)
        self.status = change_page.DeliveryStatus(self.status.root)
        failed = self.attempt(delivery)
        self.assertEqual((failed.state, failed.attempt_id, failed.error),
                         (delivery_state.QuestionSendState.FAILED, delivery.attempt, "owner offline"))
        retry = self.ask()
        self.assertNotEqual(retry.attempt, delivery.attempt)
        self.assertEqual(self.attempt(retry).message_id, failed.message_id)
        change_page.deliver(retry, self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions("image-1")), 1)
        self.assertEqual(self.status.slots(delivery.page, delivery.row).intents(), ())
        self.assertEqual(self.attempt(retry).state, delivery_state.QuestionSendState.SENT)

    def test_a_live_claim_prevents_a_second_sender(self) -> None:
        started, release = threading.Event(), threading.Event()
        transport = FakeQuestionTransport()

        def capture(recipient: staff.StaffId, message: str, message_id: str
                    ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            started.set()
            self.assertTrue(release.wait(5))
            return transport.send(recipient, message, message_id)

        first = self.ask()
        status = self.status
        with patch.object(change_page, "send_question", side_effect=capture), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(change_page.deliver, first, status)
            try:
                self.assertTrue(started.wait(5))
                self.status = change_page.DeliveryStatus(status.root)
                change_page.deliver(self.ask(), self.status)
                self.assertEqual(self.effective(first), delivery_state.QuestionSendState.SENDING)
            finally:
                release.set()
            result.result(5)
        self.assertEqual(transport.calls, [self.attempt(first).message_id])
        self.assertEqual(transport.receipts, set(transport.calls))

    def test_a_retry_finishes_a_save_that_stops_after_the_question_write(self) -> None:
        original = self.store.add

        def interrupted_add(item: str, question: question_record.Question) -> None:
            original(item, question)
            raise OSError('interrupted after write')

        with patch.object(self.store, 'add', side_effect=interrupted_add):
            with self.assertRaisesRegex(OSError, 'interrupted after write'):
                _ = self.ask()
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.status = change_page.DeliveryStatus(self.status.root)
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions('image-1')), 1)

    def test_retry_sends_the_saved_question_after_the_intent_update_fails(self) -> None:
        publish = change_page.publish_question_attempt

        def fail_after_question_save(path: Path, attempt: delivery_state.QuestionAttempt) -> None:
            if attempt.state is delivery_state.QuestionSendState.READY:
                raise OSError("Cannot update the delivery intent.")
            publish(path, attempt)

        with patch.object(change_page, "publish_question_attempt", side_effect=fail_after_question_save):
            refused = self.ask()
        assert isinstance(refused, change_page.PageQuestionSend)
        self.assertIsInstance(refused.job, change_page.NoQuestionJob)
        failed = self.attempt(refused)
        self.assertEqual(failed.state, delivery_state.QuestionSendState.FAILED)
        self.assertEqual(failed.error, "Cannot update the delivery intent.")
        self.assertIsInstance(failed.owner, delivery_state.NoQuestionSendOwner)
        self.assertEqual(len(self.store.questions('image-1')), 1)
        change_page.deliver(refused, self.status)
        self.status = change_page.DeliveryStatus(self.status.root)
        change_page.recover_review_messages(self.status)
        self.assertEqual(self.sent, [])

        change_page.deliver(self.ask(), self.status)
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('--question Q1', self.sent[0][2])
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1'])

    def test_a_held_lock_blocks_resend_for_an_unverified_claim(self) -> None:
        first = self.ask()
        for identity in (
            process_start.Started("reused"), process_start.Zombie("zombie"),
            process_start.Unreadable("cannot read"),
        ):
            with self.subTest(identity=identity), patch.object(process_start, "process_start", return_value=identity):
                self.assertEqual(self.effective(first), delivery_state.QuestionSendState.UNKNOWN)
                with self.status.locked():
                    view = change_page.question_delivery_view(self.status, self.job(first).delivery.saved)
                self.assertIsInstance(view, change_page.ResendBlocked)
        _ = self.job(first).release()
        self.assertEqual(self.effective(first), delivery_state.QuestionSendState.READY)
        retry = self.ask()
        change_page.deliver(retry, self.status)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_reuses_the_send_marker_with_or_without_a_receipt(self) -> None:
        store = image_store.ImageQuestionStore(self.status.root / image_store.IMAGES_FOLDER)
        for number, accepted in enumerate((False, True)):
            with self.subTest(accepted=accepted):
                item = f"{number:012x}"
                folder = store.root / item
                folder.mkdir(parents=True)
                image: image_store.ImageRecord = {"id": item, "file": "gate.png", "title": "The gate", "look_for": [],
                    "owner": "fixture-owner", "subject": "Test", "registered_at": "2026-11-20T12:00:00+00:00"}
                image_store.write_record(folder, image)
                target = change_page.image_question_target(folder / image_store.RECORD, image)
                first = page_question.ask(store, target, "Which check passed?", self.status)
                path = self.status.path(first.page, first.row)
                attempt = self.attempt(first)
                with self.status.locked():
                    change_page.advance_question_attempt(path, attempt,
                                                         replace(attempt, state=delivery_state.QuestionSendState.SENDING))
                _ = self.job(first).release()
                self.status = change_page.DeliveryStatus(self.status.root)
                self.assertEqual(self.effective(first), delivery_state.QuestionSendState.UNKNOWN)
                outcome = question_outcome("skipped" if accepted else "sent")
                with patch.object(change_page, "send_question", return_value=outcome) as send:
                    change_page.recover_review_messages(self.status)
                    change_page.recover_review_messages(self.status)
                send.assert_called_once_with(attempt.recipient_id, attempt.message, attempt.message_id)
                recovered = self.attempt(first)
                self.assertEqual(recovered.state, delivery_state.QuestionSendState.SENT)
                self.assertEqual(recovered.attempt_id, attempt.attempt_id)
                self.assertEqual(recovered.receipt, delivery_state.QuestionSendReceipt("kept" if accepted else "sent",
                                                                                       "fixture receipt"))
                self.assertEqual([question.question_id for question in store.questions(item)], ["Q1"])

    def test_stale_job_cannot_replace_the_new_result(self) -> None:
        first = self.ask()
        old = self.attempt(first)
        _ = self.job(first).release()
        second = self.ask()
        change_page.deliver(second, self.status)
        sent = self.attempt(second)
        with self.status.locked(), self.assertRaisesRegex(ValueError, "stale"):
            change_page.advance_question_attempt(self.status.path(first.page, first.row), old,
                                                 replace(old, state=delivery_state.QuestionSendState.FAILED))
        self.assertEqual(self.attempt(second), sent)

    def test_result_publication_after_rename_keeps_acceptance(self) -> None:
        first = self.ask()
        path = self.status.path(first.page, first.row)
        transport = FakeQuestionTransport()
        sync = atomic_file.sync_folder

        def fail_after_result(folder: Path) -> None:
            if self.attempt(first).state is delivery_state.QuestionSendState.SENT:
                raise OSError("Cannot sync the receipt.")
            sync(folder)

        with (patch.object(change_page, "send_question", side_effect=transport.send),
              patch.object(atomic_file, "sync_folder", side_effect=fail_after_result)):
            change_page.deliver(first, self.status)
            self.status = change_page.DeliveryStatus(self.status.root)
            change_page.recover_review_messages(self.status)
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_failed_rename_keeps_the_published_send_marker(self) -> None:
        first = self.ask()
        path = self.status.path(first.page, first.row)
        transport = FakeQuestionTransport()
        replace_file = os.replace

        def fail_result_rename(source: Path, destination: Path) -> None:
            candidate = change_page.read_question_attempt(source)
            if candidate.state is delivery_state.QuestionSendState.SENT:
                raise OSError("Cannot replace the delivery record.")
            replace_file(source, destination)

        with (patch.object(change_page, "send_question", side_effect=transport.send),
              patch.object(os, "replace", side_effect=fail_result_rename)):
            with self.assertRaises(atomic_file.AtomicPublicationFailed):
                change_page.deliver(first, self.status)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENDING)
        self.assertEqual(self.effective(first), delivery_state.QuestionSendState.UNKNOWN)
        with patch.object(change_page, "send_question", side_effect=transport.send):
            change_page.recover_review_messages(change_page.DeliveryStatus(self.status.root))
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_typed_transport_results_set_the_delivery_state(self) -> None:
        state = delivery_state.QuestionSendState
        cases: tuple[tuple[send_result.Outcome, delivery_state.QuestionSendState], ...] = (
            ("sent", state.SENT), ("kept", state.SENT), ("queued", state.QUEUED),
            ("failed", state.FAILED), ("refused", state.FAILED), ("skipped", state.SENT),
            ("replaced", state.FAILED), ("held", state.UNKNOWN), ("pending", state.UNKNOWN),
            ("retry", state.UNKNOWN),
        )
        for outcome, expected in cases:
            with (self.subTest(outcome=outcome), patch.object(change_page, "alert_delivery_failures"),
                  patch.object(change_page, "send_question", return_value=question_outcome(outcome)) as send):
                delivery = self.ask(outcome)
                change_page.deliver(delivery, self.status)
                result = self.attempt(delivery)
                self.assertEqual(result.state, expected)
                tries = 2 if expected in (state.FAILED, state.UNKNOWN) else 1
                self.assertEqual(send.call_args_list, [call(result.recipient_id, result.message, result.message_id)] * tries)
                path = self.status.path(delivery.page, delivery.row)
                self.assertNotIn('"resend_requests"', path.read_text())
                self.assertEqual(set(self.status.take_deferred_questions()), {path} if expected is state.UNKNOWN else set())
                if outcome == "skipped":
                    self.assertEqual(result.receipt, delivery_state.QuestionSendReceipt("kept", "fixture receipt"))

    def test_an_empty_question_does_not_write_or_claim(self) -> None:
        with self.assertRaisesRegex(question_record.AnswerRefused, 'Enter a question first'):
            _ = self.ask(words='  ')
        self.assertEqual(self.store.questions('image-1'), ())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.status.request_attempts, {})

    def test_distinct_open_questions_get_distinct_ids_and_equal_words_reuse_the_claim(self) -> None:
        first = self.ask()
        second = self.ask(words='Another question?')
        repeat = self.ask()
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1', 'Q2'])
        self.assertEqual(first.attempt, repeat.attempt)
        self.assertNotEqual(first.attempt, second.attempt)
        self.assertEqual(self.sent, [])
        change_page.deliver(first, self.status)
        change_page.deliver(second, self.status)
        change_page.deliver(repeat, self.status)
        self.assertEqual(len(self.sent), 2)

    def test_an_answer_refuses_unknown_empty_and_already_answered_ids(self) -> None:
        _ = self.ask()
        before = self.store.questions('image-1')
        for identity, words in (('Q2', 'Answer'), ('Q1', '  ')):
            self.assertIsInstance(page_question.answer(self.store, 'image-1', identity, words), page_question.AnswerRefused)
        self.assertEqual(self.store.questions('image-1'), before)
        result = page_question.answer(self.store, 'image-1', 'Q1', '  The gate passed.  ')
        assert isinstance(result, page_question.Answered)
        self.assertEqual(result.question.answer, self.store.questions('image-1')[0].answer)
        self.assertIsInstance(page_question.answer(self.store, 'image-1', 'Q1', 'The gate passed.'),
                              page_question.AnswerRefused)
        self.assertIsInstance(page_question.answer(self.store, 'image-1', 'Q1', 'Changed answer'),
                              page_question.AnswerRefused)
        self.assertEqual(self.store.questions('image-1')[0].answer, result.question.answer)

    def test_a_store_refusal_preserves_the_first_answer_in_a_race(self) -> None:
        _ = self.ask()
        original = self.store.set_answer

        def answer_first(item: str, identity: str, answer: question_record.Answer) -> None:
            original(item, identity, question_record.Answer('First answer', answer.answered_at))
            original(item, identity, answer)

        with patch.object(self.store, 'set_answer', side_effect=answer_first):
            result = page_question.answer(self.store, 'image-1', 'Q1', 'Late answer')
        self.assertIsInstance(result, page_question.AnswerRefused)
        answer = self.store.questions('image-1')[0].answer
        assert answer is not None
        self.assertEqual(answer.text, 'First answer')

    def test_question_and_answer_html_escape_words_and_keep_both_texts(self) -> None:
        _ = self.ask(words='Use <wide> & tall?')
        question = self.store.questions('image-1')[0]
        opened = page_question.open_question_html(question)
        self.assertIn('Use &lt;wide&gt; &amp; tall?', opened)
        self.assertIn('waiting for an answer', opened)
        self.assertNotIn('<time ', opened)
        result = page_question.answer(self.store, 'image-1', 'Q1', '<Yes> & checked')
        assert isinstance(result, page_question.Answered)
        answer = result.question.answer
        assert answer is not None
        rendered = page_question.answered_question_html(result.question, answer)
        self.assertIn('<h3>Answer</h3>', rendered)
        self.assertNotIn('<time ', rendered)
        self.assertIn('Use &lt;wide&gt; &amp; tall?', rendered)
        self.assertIn('&lt;Yes&gt; &amp; checked', rendered)
        self.assertNotIn('waiting for an answer', rendered)
        self.assertEqual(question_record.read(question_record.stored(result.question)), result.question)

    def test_invalid_stored_question_data_is_refused(self) -> None:
        for value in ({}, {'question_id': 'Q0', 'asked_at': 'today', 'words': 'Ask', 'answer': None},
                      {'question_id': 'Q1', 'asked_at': '2026-11-17T12:00:00+00:00', 'words': 'Ask', 'answer': 5}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = question_record.read(value)
