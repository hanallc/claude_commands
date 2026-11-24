"""Share stored questions and the Review delivery claims between pages."""
from __future__ import annotations

import html
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from scripts.production.delivery_state import QuestionAttempt, QuestionSendState
from scripts.review_links import change_page, question_record


@dataclass(frozen=True)
class Answered:
    question: question_record.Question


@dataclass(frozen=True)
class AnswerRefused:
    reason: str


class QuestionStore(Protocol):
    """Read atomic records; serialize adds and answer checks under the record's lock."""

    def questions(self, item: str) -> tuple[question_record.Question, ...]: ...
    def add(self, item: str, question: question_record.Question) -> None: ...
    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        """Refuse an unknown or answered id with question_record.AnswerRefused."""
        ...


def ask(store: QuestionStore, target: change_page.QuestionDeliveryTarget,
        words: str, status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    """Save and claim the question before the server answers the browser."""
    words = words.strip()
    if not words:
        raise question_record.AnswerRefused("Enter a question first.")
    with status.locked():
        item = target.item
        questions = store.questions(item)
        question = next((question for question in questions
                         if question.answer is None and question.words == words), None)
        new_question = question is None
        if question is None:
            number = max((int(question.question_id[1:]) for question in questions), default=0) + 1
            question = question_record.Question(f"Q{number}", datetime.now(UTC).isoformat(), words)
        saved = target.saved(question)
        page = saved.page()
        path = status.path(page, saved.source.row)
        prepared = QuestionAttempt(saved.source, saved.recipient, saved.message, 1, QuestionSendState.PREPARED,
                                   secrets.token_hex(32))
        view = change_page.question_delivery_view(status, saved)
        previous = view.attempt
        record = previous if isinstance(previous, QuestionAttempt) else prepared
        if isinstance(view, change_page.QuestionDeliveryFailed):
            return change_page.question_envelope(page, record)
        if isinstance(view, change_page.ResendBlocked):
            if view.state is QuestionSendState.UNKNOWN and new_question:
                raise question_record.AnswerRefused(view.reason)
            if isinstance(previous, QuestionAttempt):
                status.defer_question(path)
            return change_page.question_envelope(page, record)
        if isinstance(view, change_page.QuestionDeliveryAccepted):
            return change_page.question_envelope(page, record)
        if not new_question and view.state is QuestionSendState.UNKNOWN:
            return change_page.question_envelope(page, record)
        if isinstance(previous, QuestionAttempt):
            unsaved_draft = (new_question and previous.state is QuestionSendState.PREPARED
                             and not previous.earlier_unconfirmed and previous.message_id == prepared.message_id)
            if new_question and not unsaved_draft and not saved.matches(previous):
                raise question_record.AnswerRefused("The question does not match its delivery record.")
            if unsaved_draft or view.state in (QuestionSendState.FAILED, QuestionSendState.UNKNOWN):
                retry = replace(prepared, revision=record.revision + 1,
                                earlier_unconfirmed=record.earlier_unconfirmed or (
                                    not unsaved_draft and view.state is QuestionSendState.UNKNOWN))
                change_page.advance_question_attempt(path, record, retry)
                record = retry
        else:
            record = replace(prepared, earlier_unconfirmed=not new_question and view.state is QuestionSendState.UNKNOWN)
            change_page.publish_question_attempt(path, record)
        if new_question:
            store.add(item, question)
            if question not in store.questions(item):
                raise question_record.AnswerRefused("The saved question does not match this delivery.")
        return change_page.ready_and_claim_question(status, path, record, saved)


def answer(store: QuestionStore, item: str, question_id: str, text: str) -> Answered | AnswerRefused:
    """Write one answer; the store checks the id again under its record lock."""
    if not text.strip():
        return AnswerRefused('Enter an answer first.')
    question = next((question for question in store.questions(item) if question.question_id == question_id), None)
    if question is None:
        return AnswerRefused('Unknown question id.')
    if question.answer is not None:
        return AnswerRefused('This question already has an answer.')
    response = question_record.Answer(text.strip(), datetime.now(UTC).isoformat())
    try:
        store.set_answer(item, question_id, response)
    except question_record.AnswerRefused as error:
        return AnswerRefused(str(error))
    return Answered(question_record.Question(question.question_id, question.asked_at, question.words, response))


def question_text_html(question_id: str, words: str, *, heading_suffix: str = "", asked_time_html: str = "") -> str:
    return f'<h3>Question {html.escape(question_id + heading_suffix)}</h3>{asked_time_html}<p>{html.escape(words)}</p>'


def answer_text_html(words: str, *, answer_time_html: str = "", answer_mark_html: str = "") -> str:
    return f'<h3>Answer{answer_mark_html}</h3>{answer_time_html}<p>{html.escape(words)}</p>'


def open_question_html(question: question_record.Question, *, asked_time_html: str = "", failed: bool = False) -> str:
    waiting = "" if failed else " — waiting for an answer"
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + question_text_html(question.question_id, question.words, heading_suffix=waiting, asked_time_html=asked_time_html)
            + change_page.question_failure_html(failed) + '</section>')


def answered_question_html(question: question_record.Question, answer: question_record.Answer, *,
                           asked_time_html: str = "", answer_time_html: str = "", answer_mark_html: str = "") -> str:
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + question_text_html(question.question_id, question.words, asked_time_html=asked_time_html)
            + answer_text_html(answer.text, answer_time_html=answer_time_html, answer_mark_html=answer_mark_html)
            + '</section>')
