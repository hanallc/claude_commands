"""Share stored questions and the Review delivery claims between pages."""
from __future__ import annotations

import html
import json
import re
import secrets
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, cast

from scripts.production import staff
from scripts.review_links import change_page


@dataclass(frozen=True)
class Answer:
    text: str
    answered_at: str


@dataclass(frozen=True)
class Question:
    """A missing answer means that the owner must answer this question."""

    question_id: str
    asked_at: str
    words: str
    answer: Answer | None = None


@dataclass(frozen=True)
class Answered:
    question: Question


@dataclass(frozen=True)
class AnswerRefused:
    reason: str


class QuestionStore(Protocol):
    """Read atomic records; serialize adds and answer checks under the record's lock."""

    def questions(self, item: str) -> tuple[Question, ...]: ...
    def add(self, item: str, question: Question) -> None: ...
    def set_answer(self, item: str, question_id: str, answer: Answer) -> None:
        """Refuse an unknown or answered id with change_page.AnswerRefused."""
        ...


class StoredAnswer(TypedDict):
    text: str
    answered_at: str


class StoredQuestion(TypedDict):
    question_id: str
    asked_at: str
    words: str
    answer: StoredAnswer | None


def stored(question: Question) -> StoredQuestion:
    answer = question.answer
    return {'question_id': question.question_id, 'asked_at': question.asked_at, 'words': question.words,
            'answer': None if answer is None else {'text': answer.text, 'answered_at': answer.answered_at}}


def read(value: object) -> Question:
    """Check a stored question before it enters the page's domain."""
    if not isinstance(value, dict):
        raise ValueError('invalid stored question')
    fields = cast(dict[str, object], value)
    identity, asked_at, words = (fields.get(key) for key in ('question_id', 'asked_at', 'words'))
    if not isinstance(identity, str) or re.fullmatch(r'Q[1-9][0-9]*', identity) is None or not isinstance(
            asked_at, str) or not isinstance(words, str) or not words.strip() or 'answer' not in fields:
        raise ValueError('invalid stored question')
    _ = datetime.fromisoformat(asked_at)
    value = fields['answer']
    if value is None:
        return Question(identity, asked_at, words)
    if not isinstance(value, dict):
        raise ValueError('invalid stored answer')
    answer = cast(dict[str, object], value)
    text, answered_at = answer.get('text'), answer.get('answered_at')
    if not isinstance(text, str) or not text.strip() or not isinstance(answered_at, str):
        raise ValueError('invalid stored answer')
    _ = datetime.fromisoformat(answered_at)
    return Question(identity, asked_at, words, Answer(text, answered_at))


def ask(store: QuestionStore, item: str, owner: staff.StaffId, source_path: Path, row: str, title: str,
        words: str, answer_command: str, status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    """Save and claim without sending. The caller delivers after it answers the browser.

    Row names the page and item. The command has {item} and {question_id} fields.
    Equal words retry an open question. Different words create another question.
    """
    words = words.strip()
    if not words:
        raise change_page.AnswerRefused('Enter a question first.')
    with status.locked():
        questions = store.questions(item)
        question = next((question for question in questions
                         if question.answer is None and question.words == words), None)
        new_question = question is None
        if question is None:
            number = max((int(question.question_id[1:]) for question in questions), default=0) + 1
            question = Question(f'Q{number}', datetime.now(UTC).isoformat(), words)
        command = answer_command.format(item=shlex.quote(item), question_id=question.question_id)
        message = f'{row}: Ask question\n{title}\n{words}\nAnswer with this command: {command}'
        delivery_row = f'{row}/{question.question_id}'
        saved: change_page.StoredReviewMessage = {'saved': not new_question, 'source': str(source_path),
            'owner': owner, 'message': message, 'row': delivery_row}
        page = change_page.ReviewMessagePage(source_path, source_path.parent, (delivery_row,), '', saved)
        slots = status.slots(page, delivery_row)
        intent = slots.verdict
        if isinstance(intent, change_page.DeliveryIntent) and status.owns_live_attempt(page, delivery_row, intent):
            return change_page.MessageDelivery(page, delivery_row, (owner,), message, intent.attempt, (owner,))
        attempt = secrets.token_hex()
        delivery = change_page.MessageDelivery(page, delivery_row, (owner,), message, attempt, (owner,))
        # A failure or an abandoned intent permits a new attempt for the same question.
        if not new_question and not isinstance(intent, change_page.DeliveryIntent):
            previous: change_page.StoredDeliverySlots
            try:
                previous = cast(change_page.StoredDeliverySlots, json.loads(status.path(page, delivery_row).read_text()))
            except FileNotFoundError:
                previous = {}
            prior_message = previous.get('review_message')
            if (prior_message is not None and prior_message['saved']
                    and isinstance(status.failed_delivery(page, delivery_row), change_page.NoFailedDelivery)):
                return delivery
        intent = change_page.DeliveryIntent(attempt, 'verdict', recipients=(owner,))
        status.write_slots(page, delivery_row, change_page.DeliverySlots(change_page.NoDelivery(), intent))
        status.request_attempts[attempt] = change_page.AttemptOwnership(status.path(page, delivery_row), 'claimed')
        try:
            if new_question:
                store.add(item, question)
            saved['saved'] = True
            status.write_slots(page, delivery_row, status.slots(page, delivery_row))
        except BaseException:
            _ = status.request_attempts.pop(attempt, None)
            status.clear_locked(page, delivery_row, status.slots(page, delivery_row), 'verdict')
            raise
    return delivery


def answer(store: QuestionStore, item: str, question_id: str, text: str) -> Answered | AnswerRefused:
    """Write one answer; the store checks the id again under its record lock."""
    if not text.strip():
        return AnswerRefused('Enter an answer first.')
    question = next((question for question in store.questions(item) if question.question_id == question_id), None)
    if question is None:
        return AnswerRefused('Unknown question id.')
    if question.answer is not None:
        return AnswerRefused('This question already has an answer.')
    response = Answer(text.strip(), datetime.now(UTC).isoformat())
    try:
        store.set_answer(item, question_id, response)
    except change_page.AnswerRefused as error:
        return AnswerRefused(str(error))
    return Answered(Question(question.question_id, question.asked_at, question.words, response))


def open_question_html(question: Question) -> str:
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + f'<h3>Question {html.escape(question.question_id)} — waiting for an answer</h3>'
            + f'<p>{html.escape(question.words)}</p></section>')


def answered_question_html(question: Question) -> str:
    if question.answer is None:
        raise ValueError('This question has no answer.')
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + f'<h3>Question {html.escape(question.question_id)}</h3><p>{html.escape(question.words)}</p>'
            + f'<h3>Answer</h3><p>{html.escape(question.answer.text)}</p></section>')
