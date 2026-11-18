"""Test shared page questions with private records and a fake sender."""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.production import staff
from scripts.review_links import change_page, page_question
from scripts.review_links import question_record


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
        _ = self.enterContext(patch.object(change_page, 'send_to_staff', side_effect=self.capture))

    def capture(self, recipient: staff.StaffId, sender: str, message: str) -> None:
        self.sent.append((recipient, sender, message))

    def ask(self, item: str = 'image-1', words: str = 'Which check passed?') -> change_page.MessageDelivery:
        return page_question.ask(self.store, item, staff.StaffId('fixture-owner'), self.root / item,
            'Images '+item, 'The gate', words,
            'images.py answer --id {item} --question {question_id} --text "<answer>"', self.status)

    def test_question_ids_start_at_one_for_each_item_and_increase_after_answers(self) -> None:
        _ = self.ask()
        _ = self.ask('image-2')
        result = page_question.answer(self.store, 'image-1', 'Q1', 'The gate passed.')
        self.assertIsInstance(result, page_question.Answered)
        _ = self.ask(words='Which repair worked?')
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1', 'Q2'])
        self.assertEqual([question.question_id for question in self.store.questions('image-2')], ['Q1'])

    def test_ask_saves_before_sending_and_a_repeat_sends_only_once(self) -> None:
        def capture(recipient: staff.StaffId, sender: str, message: str) -> None:
            self.assertEqual(self.store.questions('image-1')[0].words, 'Which check passed?')
            self.assertEqual([ownership.state for ownership in self.status.request_attempts.values()], ['sending'])
            self.capture(recipient, sender, message)

        with patch.object(change_page, 'send_to_staff', side_effect=capture):
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

    def test_failed_delivery_keeps_the_question_for_a_retry(self) -> None:
        with patch.object(change_page, 'send_to_staff', side_effect=RuntimeError('owner offline')) as sender:
            delivery = self.ask()
            change_page.deliver(delivery, self.status)
        self.assertEqual(sender.call_count, 3)
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.assertIsNone(self.store.questions('image-1')[0].answer)
        self.assertEqual(self.status.request_attempts, {})
        self.status = change_page.DeliveryStatus(self.status.root)
        failed = self.status.failed_delivery(delivery.page, delivery.row)
        self.assertEqual(failed, change_page.FailedDelivery(delivery.attempt, delivery.page.review_list,
                                                          delivery.row, 'owner offline'))
        previous_attempt = delivery.attempt
        delivery = self.ask()
        self.assertNotEqual(delivery.attempt, previous_attempt)
        self.assertEqual(self.status.failed_delivery(delivery.page, delivery.row), failed)
        change_page.deliver(delivery, self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions('image-1')), 1)
        self.assertEqual(self.status.slots(delivery.page, delivery.row).intents(), ())
        self.assertIsInstance(self.status.failed_delivery(delivery.page, delivery.row), change_page.NoFailedDelivery)

    def test_a_live_claim_prevents_a_second_sender(self) -> None:
        def capture(recipient: staff.StaffId, sender: str, message: str) -> None:
            change_page.deliver(self.ask(), self.status)
            self.capture(recipient, sender, message)

        with patch.object(change_page, 'send_to_staff', side_effect=capture):
            change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.store.questions('image-1')), 1)

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
        write = self.status.write_slots
        failed = False

        def fail_after_question_save(page: change_page.ReviewPage, row: str, slots: change_page.DeliverySlots) -> None:
            nonlocal failed
            if not failed and self.store.questions('image-1'):
                failed = True
                raise OSError('Cannot update the delivery intent.')
            write(page, row, slots)

        with patch.object(self.status, 'write_slots', side_effect=fail_after_question_save):
            with self.assertRaisesRegex(OSError, 'Cannot update the delivery intent'):
                _ = self.ask()
        self.status = change_page.DeliveryStatus(self.status.root)
        change_page.recover_review_messages(self.status)
        self.assertEqual(self.sent, [])

        change_page.deliver(self.ask(), self.status)
        change_page.deliver(self.ask(), self.status)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('--question Q1', self.sent[0][2])
        self.assertEqual([question.question_id for question in self.store.questions('image-1')], ['Q1'])

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
        self.assertIn('Use &lt;wide&gt; &amp; tall?', page_question.open_question_html(question))
        self.assertIn('waiting for an answer', page_question.open_question_html(question))
        result = page_question.answer(self.store, 'image-1', 'Q1', '<Yes> & checked')
        assert isinstance(result, page_question.Answered)
        rendered = page_question.answered_question_html(result.question)
        self.assertIn('Use &lt;wide&gt; &amp; tall?', rendered)
        self.assertIn('&lt;Yes&gt; &amp; checked', rendered)
        self.assertNotIn('waiting for an answer', rendered)
        self.assertEqual(question_record.read(question_record.stored(result.question)), result.question)

    def test_invalid_stored_question_data_is_refused(self) -> None:
        for value in ({}, {'question_id': 'Q0', 'asked_at': 'today', 'words': 'Ask', 'answer': None},
                      {'question_id': 'Q1', 'asked_at': '2026-11-17T12:00:00+00:00', 'words': 'Ask', 'answer': 5}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = question_record.read(value)
