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
        words: str, status: change_page.DeliveryStatus, *,
        resend_unknown: bool = False, resend_request: str = "") -> change_page.MessageDelivery:
    """Save and claim the question before the server answers the browser."""
    if not resend_request:
        words = words.strip()
    if not words.strip():
        raise question_record.AnswerRefused("Enter a question first.")
    with status.locked():
        item = target.item
        questions = store.questions(item)
        question = next((question for question in questions
                         if question.answer is None and question.words == words), None)
        new_question = question is None
        if question is None:
            if resend_request:
                raise question_record.AnswerRefused("This item has no open question with that id.")
            number = max((int(question.question_id[1:]) for question in questions), default=0) + 1
            question = question_record.Question(f"Q{number}", datetime.now(UTC).isoformat(), words)
        saved = target.saved(question)
        page = saved.page()
        path = status.path(page, saved.source.row)
        requests = (resend_request,) if resend_request else ()
        prepared = QuestionAttempt(saved.source, saved.recipient, saved.message, 1, QuestionSendState.PREPARED,
                                   secrets.token_hex(32), resend_requests=requests)
        view = change_page.question_delivery_view(status, saved)
        previous = view.attempt
        record = previous if isinstance(previous, QuestionAttempt) else prepared
        repeated_resend = isinstance(previous, QuestionAttempt) and resend_request in previous.resend_requests
        if repeated_resend and view.state in (QuestionSendState.FAILED, QuestionSendState.UNKNOWN):
            return change_page.question_envelope(page, record)
        if isinstance(view, change_page.ResendBlocked):
            if view.state is QuestionSendState.UNKNOWN and (resend_request or new_question):
                raise question_record.AnswerRefused(view.reason)
            return change_page.question_envelope(page, record)
        if isinstance(view, change_page.QuestionDeliveryAccepted):
            return change_page.question_envelope(page, record)
        if not new_question and view.state is QuestionSendState.UNKNOWN and not resend_unknown:
            return change_page.question_envelope(page, record)
        if isinstance(previous, QuestionAttempt):
            unsaved_draft = (new_question and previous.state is QuestionSendState.PREPARED
                             and not previous.earlier_unconfirmed and previous.message_id == prepared.message_id)
            if new_question and not unsaved_draft and not saved.matches(previous):
                raise question_record.AnswerRefused("The question does not match its delivery record.")
            if unsaved_draft or view.state in (QuestionSendState.FAILED, QuestionSendState.UNKNOWN) or (
                    resend_request and not repeated_resend):
                retry = replace(prepared, revision=record.revision + 1,
                                earlier_unconfirmed=record.earlier_unconfirmed or (
                                    not unsaved_draft and view.state is QuestionSendState.UNKNOWN),
                                resend_requests=(*record.resend_requests, *requests))
                change_page.advance_question_attempt(path, record, retry)
                record = retry
        else:
            record = replace(prepared, earlier_unconfirmed=not new_question and view.state is QuestionSendState.UNKNOWN)
            change_page.publish_question_attempt(path, record)
        if new_question:
            store.add(item, question)
            if question not in store.questions(item):
                raise question_record.AnswerRefused("The saved question does not match this delivery.")
        return change_page.claim_question(status, change_page.ready_question(path, record, saved))


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


def open_question_html(question: question_record.Question, *, asked_time_html: str = "") -> str:
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + f'<h3>Question {html.escape(question.question_id)} — waiting for an answer</h3>'
            + asked_time_html + f'<p>{html.escape(question.words)}</p></section>')


def answered_question_html(question: question_record.Question, answer: question_record.Answer, *,
                           asked_time_html: str = "", answer_time_html: str = "", answer_mark_html: str = "") -> str:
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + f'<h3>Question {html.escape(question.question_id)}</h3>'
            + asked_time_html + f'<p>{html.escape(question.words)}</p>'
            + '<h3>Answer' + answer_mark_html + '</h3>'
            + answer_time_html + f'<p>{html.escape(answer.text)}</p></section>')
