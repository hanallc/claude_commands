"""Share stored questions and the Review delivery claims between pages."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed
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
            try:
                try:
                    store.add(item, question)
                except AtomicDurabilityUnconfirmed:
                    if question not in store.questions(item):
                        raise
                if question not in store.questions(item):
                    raise question_record.AnswerRefused("The saved question does not match this delivery.")
            except Exception:
                status.question_sources_pending.set()
                raise
        saved = target.saved(question)
        record = save_attempt(status, saved, question, replace_orphan=new_question)
        page = saved.page()
        path = status.path(page, saved.source.row)
        if record is None:
            return change_page.question_envelope(page, new_attempt(saved, question))
        if not saved.matches(record):
            status.defer_question(path)
            return change_page.question_envelope(page, record)
        if change_page.page_progress(record).state.pending:
            status.defer_question(path)
        return change_page.ready_and_claim_question(status, record, saved)


def save_attempt(status: change_page.DeliveryStatus, saved: change_page.SavedQuestionDelivery,
                 question: question_record.Question, *, replace_orphan: bool = False) -> QuestionAttempt | None:
    """Write the first delivery record from the source under the delivery lock."""
    path = status.path(saved.page(), saved.source.row)
    if not replace_orphan:
        try:
            record = change_page.read_question_attempt(path)
            if saved.matches(record) or change_page.page_progress(record).saved_at >= (
                    datetime.fromisoformat(question.asked_at).timestamp()):
                return record
        except FileNotFoundError:
            pass
        except change_page.QuestionAttemptMissing:
            status.question_sources_pending.set()
            status.defer_question(path, change_page.UnreadableDeliveryRecord())
            return None
    record = new_attempt(saved, question)
    try:
        change_page.publish_question_attempt(path, record)
    except (OSError, ValueError):
        status.question_sources_pending.set()
        raise
    status.defer_question(path)
    return record


def new_attempt(saved: change_page.SavedQuestionDelivery, question: question_record.Question) -> QuestionAttempt:
    source = saved.source
    attempt = change_page.source_question_key(source.path, source.row, question.asked_at, question.words)
    progress = change_page.QuestionProgress(attempt, question.question_id,
                                            datetime.fromisoformat(question.asked_at).timestamp())
    return change_page.PageAttempt(source, saved.recipient, saved.message, 1, QuestionSendState.READY,
                                   attempt, progress=progress)


def recover_source(status: change_page.DeliveryStatus, target: change_page.QuestionDeliveryTarget,
                   questions: tuple[question_record.Question, ...]) -> None:
    for question in questions:
        saved = target.saved(question)
        if status.migration_pending(status.path(saved.page(), saved.source.row)):
            continue
        if question.answer is not None:
            change_page.record_page_question_answer(status, target, question)
            continue
        try:
            with status.locked():
                _ = save_attempt(status, saved, question)
        except (OSError, ValueError) as error:
            status.defer_source(status.path(saved.page(), saved.source.row), error)


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


def open_question_html(question: question_record.Question, *, asked_time_html: str = "", failed: bool = False) -> str:
    waiting = "" if failed else " — waiting for an answer"
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + f'<h3>Question {html.escape(question.question_id)}{waiting}</h3>'
            + asked_time_html + f'<p>{html.escape(question.words)}</p>'
            + change_page.question_failure_html(failed) + '</section>')


def answered_question_html(question: question_record.Question, answer: question_record.Answer, *,
                           asked_time_html: str = "", answer_time_html: str = "", answer_mark_html: str = "") -> str:
    return (f'<section class="page-question" data-question="{html.escape(question.question_id, quote=True)}">'
            + f'<h3>Question {html.escape(question.question_id)}</h3>'
            + asked_time_html + f'<p>{html.escape(question.words)}</p>'
            + '<h3>Answer' + answer_mark_html + '</h3>'
            + answer_time_html + f'<p>{html.escape(answer.text)}</p></section>')
