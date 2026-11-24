"""Decode delivery records and check reply IDs."""
from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from scripts.production import staff
from scripts.review_links import question_record
from scripts.review_links.review_events import require_waiting_why as require_waiting_why
from scripts.review_links.review_events import waiting_why


class QuestionSendState(StrEnum):
    PREPARED = "prepared"
    READY = "ready"
    CLAIMED = "claimed"
    SENDING = "sending"
    SENT = "sent"
    QUEUED = "queued"
    FAILED = "failed"
    UNKNOWN = "unknown"

    @property
    def waits(self) -> bool:
        return self in (self.CLAIMED, self.SENDING, self.SENT, self.QUEUED)


@dataclass(frozen=True)
class NoSavedListKeeper:
    """An Image question or an older List attempt has no saved keeper."""


@dataclass(frozen=True)
class QuestionSource:
    path: str
    item_id: str
    question_id: str
    words: str
    row: str
    keeper: staff.StaffId | NoSavedListKeeper = NoSavedListKeeper()

    def matches_attempt(self, previous: QuestionSource) -> bool:
        """Allow an older attempt to omit the keeper."""
        return (self == previous or isinstance(previous.keeper, NoSavedListKeeper)
                and self == replace(previous, keeper=self.keeper))

    def message_id(self, recipient: staff.StaffId) -> str:
        parts = [self.path, self.item_id, self.question_id, recipient]
        return "page-question:" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()


@dataclass(frozen=True)
class QuestionSendOwner:
    host: str
    boot_id: str
    pid: int
    process_start: str
    job_id: str


@dataclass(frozen=True)
class NoQuestionSendOwner:
    """The record has no job that can change its state."""


@dataclass(frozen=True)
class QuestionSendReceipt:
    outcome: Literal["sent", "kept", "queued"]
    detail: str


@dataclass(frozen=True)
class NoQuestionSendReceipt:
    """The record has no confirmed acceptance."""


@dataclass(frozen=True)
class QuestionAttempt:
    source: QuestionSource
    recipient_id: staff.StaffId
    message: str
    revision: int
    state: QuestionSendState
    attempt_id: str
    owner: QuestionSendOwner | NoQuestionSendOwner = NoQuestionSendOwner()
    receipt: QuestionSendReceipt | NoQuestionSendReceipt = NoQuestionSendReceipt()
    error: str = ""
    earlier_unconfirmed: bool = False
    resend_requests: tuple[str, ...] = ()

    @property
    def message_id(self) -> str:
        return self.source.message_id(self.recipient_id)


class QuestionAttemptMissing(ValueError):
    """The delivery file has no question attempt in the current format."""


def read_fields(path: Path) -> dict[str, object]:
    """Read one JSON object without accepting partial delivery data."""
    return _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a delivery record object.")
    return cast(dict[str, object], value)


def _text(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise ValueError(f"Invalid delivery field {key}.")
    return value


def _positive_int(fields: dict[str, object], key: str) -> int:
    value = fields.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"Invalid delivery field {key}.")
    return value


def _hex_id(fields: dict[str, object], key: str) -> str:
    value = _text(fields, key)
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError(f"Invalid delivery field {key}.")
    return value


def decode_question_attempt(fields: dict[str, object]) -> QuestionAttempt:
    """Decode the authoritative record before any job or reader uses it."""
    if "question_attempt" not in fields:
        raise QuestionAttemptMissing()
    data = _object(fields["question_attempt"])
    if _positive_int(data, "schema") != 2:
        raise ValueError("Invalid question delivery schema.")
    source = _object(data.get("source"))
    owner: QuestionSendOwner | NoQuestionSendOwner = NoQuestionSendOwner()
    if data.get("owner") is not None:
        identity = _object(data["owner"])
        owner = QuestionSendOwner(_text(identity, "host"), _text(identity, "boot_id"), _positive_int(identity, "pid"),
                                  _text(identity, "process_start"), _hex_id(identity, "job_id"))
        if not all((owner.host.strip(), owner.boot_id.strip(), owner.process_start.strip())):
            raise ValueError("Incomplete question delivery owner.")
    receipt: QuestionSendReceipt | NoQuestionSendReceipt = NoQuestionSendReceipt()
    if data.get("receipt") is not None:
        result = _object(data["receipt"])
        outcome = result.get("outcome")
        if outcome not in ("sent", "kept", "queued"):
            raise ValueError("Invalid question delivery receipt.")
        receipt = QuestionSendReceipt(outcome, _text(result, "detail"))
    earlier_unconfirmed = data.get("earlier_unconfirmed", False)
    if not isinstance(earlier_unconfirmed, bool):
        raise ValueError("Invalid earlier delivery outcome.")
    requests = data.get("resend_requests", [])
    if not isinstance(requests, list) or not all(isinstance(request, str) for request in cast(list[object], requests)):
        raise ValueError("Invalid question resend requests.")
    keeper = staff.StaffId(_text(source, "keeper")) if "keeper" in source else NoSavedListKeeper()
    record = QuestionAttempt(
        QuestionSource(_text(source, "path"), _text(source, "item_id"), _text(source, "question_id"),
                       _text(source, "words"), _text(source, "row"), keeper),
        staff.StaffId(_text(data, "recipient_id")), _text(data, "message"), _positive_int(data, "revision"),
        QuestionSendState(_text(data, "state")), _hex_id(data, "attempt_id"), owner, receipt, _text(data, "error"),
        earlier_unconfirmed, tuple(cast(list[str], requests)))
    if (record.message_id != data.get("message_id")
            or isinstance(owner, QuestionSendOwner) != (record.state in (QuestionSendState.CLAIMED, QuestionSendState.SENDING))
            or isinstance(receipt, QuestionSendReceipt) != (record.state in (QuestionSendState.SENT, QuestionSendState.QUEUED))
            or (isinstance(receipt, QuestionSendReceipt)
                and (receipt.outcome == "queued") != (record.state is QuestionSendState.QUEUED))):
        raise ValueError("Contradictory question delivery record.")
    return record


def legacy_question_state(fields: dict[str, object], failure_fields: dict[str, object] | None,
                          owns_attempt: Callable[[str], bool],
                          source: QuestionSource, recipient: staff.StaffId,
                          matches_message: Callable[[str], bool]) -> QuestionSendState:
    """Read the earlier question format without inferring a safe automatic retry."""
    try:
        saved = _object(fields.get("review_message"))
        if (str(Path(_text(saved, "source")).resolve()) != source.path or saved.get("owner") != recipient
                or saved.get("row") != source.row or saved.get("saved") is not True
                or not matches_message(_text(saved, "message"))):
            return QuestionSendState.UNKNOWN
        verdict = fields.get("verdict")
        intent = decode_slot(verdict, "verdict")
        if "verdict" in fields and not isinstance(intent, DeliveryIntent):
            return QuestionSendState.UNKNOWN
        if isinstance(intent, DeliveryIntent) and intent.recipients and intent.recipients != (recipient,):
            return QuestionSendState.UNKNOWN
        dispatch = _object(saved["image_question_dispatch"]) if "image_question_dispatch" in saved else {}
        dispatch_attempt = dispatch.get("attempt")
        dispatch_state = dispatch.get("state")
        if "image_question_dispatch" in saved and (not isinstance(dispatch_attempt, str) or not dispatch_attempt
                or dispatch_state not in ("started", "started_after_unconfirmed", "accepted")):
            return QuestionSendState.UNKNOWN
        if dispatch_state == "accepted":
            if isinstance(intent, DeliveryIntent) and intent.attempt != dispatch_attempt:
                return QuestionSendState.UNKNOWN
            return QuestionSendState.SENT
        if failure_fields is None:
            return QuestionSendState.UNKNOWN
        if "failed_delivery" in failure_fields:
            failure = _object(failure_fields["failed_delivery"])
            attempt = _text(failure, "attempt")
            if failure.get("row") != source.row or failure.get("review_list") != source.path:
                return QuestionSendState.UNKNOWN
            if (isinstance(intent, DeliveryIntent) and intent.attempt == attempt
                    or dispatch_state == "started" and dispatch_attempt == attempt
                    or not dispatch and verdict is None):
                return QuestionSendState.FAILED
            if verdict is None and not dispatch:
                return QuestionSendState.UNKNOWN
        if verdict is not None or dispatch:
            attempt = intent.attempt if isinstance(intent, DeliveryIntent) else dispatch_attempt
            return QuestionSendState.CLAIMED if isinstance(attempt, str) and owns_attempt(attempt) else QuestionSendState.UNKNOWN
        return QuestionSendState.SENT
    except ValueError:
        return QuestionSendState.UNKNOWN


@dataclass(frozen=True)
class NoDelivery:
    """This delivery kind has no message that needs recovery."""


@dataclass(frozen=True)
class DeliveryIntent:
    """One message attempt, without a second copy of a saved verdict."""

    attempt: str
    kind: Literal["verdict", "validation", "question"]
    feedback: str = ""
    recipients: tuple[staff.StaffId, ...] = ()


@dataclass(frozen=True)
class ValidationRequested:
    """The showrunner owns validation after a saved request."""

    attempt: str
    feedback: str


@dataclass(frozen=True)
class NoQuestion:
    """The user has not asked a question about this row."""


@dataclass(frozen=True)
class OpenQuestion:
    """The user waits for the showrunner's answer."""

    question_id: str
    text: str
    delivery: DeliveryIntent | NoDelivery


@dataclass(frozen=True)
class AnsweredQuestion:
    """The showrunner's answer returns the row to the user."""

    question_id: str
    text: str
    answer: str


class StaleQuestionExchange(ValueError):
    """The answer does not belong to the current question exchange."""


def require_exchange_id(current_id: str | NoQuestion | NoDelivery, reply_id: str) -> None:
    """Require the current exchange ID before a native answer saves."""
    if not isinstance(current_id, str):
        raise StaleQuestionExchange('this row has no open question')
    if current_id != reply_id:
        raise StaleQuestionExchange('this question id does not match the current question')


def question_exchange_for_reply(question: NoQuestion | OpenQuestion | AnsweredQuestion,
                                question_id: str, text: str) -> OpenQuestion | AnsweredQuestion:
    """Check the exchange ID and answer without changing the record."""
    require_exchange_id(question if isinstance(question, NoQuestion) else question.question_id, question_id)
    if isinstance(question, AnsweredQuestion) and question.answer != text:
        raise StaleQuestionExchange('this row has no open question')
    return cast(OpenQuestion | AnsweredQuestion, question)


@dataclass(frozen=True)
class DeliverySlots:
    """Keep validation ownership and message attempts in one record."""

    validation: DeliveryIntent | ValidationRequested | NoDelivery
    verdict: DeliveryIntent | NoDelivery
    question: NoQuestion | OpenQuestion | AnsweredQuestion = NoQuestion()
    waiting_why: str = ""

    @property
    def validation_requested(self) -> bool:
        return not isinstance(self.validation, NoDelivery)

    def waits_for_user(self, verdict_saved: bool) -> bool:
        return not (verdict_saved or self.validation_requested or isinstance(self.question, OpenQuestion))

    def intents(self) -> tuple[DeliveryIntent, ...]:
        question = self.question.delivery if isinstance(self.question, OpenQuestion) else NoDelivery()
        return tuple(intent for intent in (self.validation, self.verdict, question) if isinstance(intent, DeliveryIntent))

    def updated(self, kind: Literal["verdict", "validation", "question"], intent: DeliveryIntent | NoDelivery) -> DeliverySlots:
        if kind == "question":
            return replace(self, question=replace(self.question, delivery=intent)) if isinstance(self.question, OpenQuestion) else self
        return replace(self, verdict=intent) if kind == "verdict" else replace(self, validation=intent)


def decode_validation(value: object) -> DeliveryIntent | ValidationRequested | NoDelivery:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    attempt, feedback = fields.get("attempt"), fields.get("feedback", "")
    if fields.get("state") in ("sent", "failed") and isinstance(attempt, str) and isinstance(feedback, str):
        return ValidationRequested(attempt, feedback)
    return decode_slot(fields, "validation")

def decode_question(value: object) -> NoQuestion | OpenQuestion | AnsweredQuestion:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    text, question_id = fields.get("text"), fields.get("question_id")
    if not isinstance(text, str) or not text.strip() or not isinstance(question_id, str) or not question_id:
        return NoQuestion()
    answer = fields.get("answer")
    if isinstance(answer, str):
        return AnsweredQuestion(question_id, text, answer)
    return OpenQuestion(question_id, text, decode_slot(fields.get("delivery"), "question"))

def decode_slot(value: object, kind: Literal["verdict", "validation", "question"], *,
                require_attempt: bool = False) -> DeliveryIntent | NoDelivery:
    if not isinstance(value, dict):
        return NoDelivery()
    fields = cast(dict[str, object], value)
    attempt, state = fields.get("attempt"), fields.get("state")
    if require_attempt and state == "pending" and (not isinstance(attempt, str) or not attempt.strip()):
        raise ValueError(f"pending {kind} has no attempt ID")
    feedback = fields.get("feedback", "") if kind == "validation" else ""
    if not isinstance(attempt, str) or state != "pending" or not isinstance(feedback, str):
        return NoDelivery()
    return DeliveryIntent(attempt, kind, feedback, decode_recipients(fields.get("recipients")))

def decode_recipients(value: object) -> tuple[staff.StaffId, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(staff.StaffId(item) for item in cast(list[object], value) if isinstance(item, str))


def decode(content: str) -> DeliverySlots:
    try:
        value = cast(object, json.loads(content))
    except ValueError:
        return DeliverySlots(NoDelivery(), NoDelivery())
    return decode_value(value)


def decode_value(value: object, *, require_verdict_attempt: bool = False) -> DeliverySlots:
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    reason = waiting_why(fields.get("waiting_why"))
    if "validation" in fields or "verdict" in fields or "question" in fields:
        return DeliverySlots(decode_validation(fields.get("validation")),
                             decode_slot(fields.get("verdict"), "verdict", require_attempt=require_verdict_attempt),
                             decode_question(fields.get("question")), reason)
    # Older records contain one intent.
    kind = fields.get("kind")
    if kind == "validation":
        return DeliverySlots(decode_validation(fields), NoDelivery(), waiting_why=reason)
    if kind == "verdict":
        return DeliverySlots(NoDelivery(), decode_slot(fields, "verdict", require_attempt=require_verdict_attempt),
                             waiting_why=reason)
    feedback = fields.get("feedback")
    if isinstance(feedback, str):
        return DeliverySlots(ValidationRequested("", feedback), NoDelivery(), waiting_why=reason)
    return DeliverySlots(NoDelivery(), NoDelivery(), waiting_why=reason)


def read_list_questions(text: str) -> tuple[question_record.Question, ...]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ValueError("invalid List questions")
    questions = tuple(question_record.read(question) for question in cast(list[object], value))
    if len({question.question_id for question in questions}) != len(questions):
        raise ValueError("duplicate question id")
    return questions
