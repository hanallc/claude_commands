"""Question and answer history filed on a List item."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import cast

from scripts.production.staff import StaffId


class Source(StrEnum):
    STAFF_QUESTION = "staff-question"
    SCOPED_REPLY = "sent-question/scoped-reply"
    LIST = "list"
    PICTURE = "picture"


class Direction(StrEnum):
    QUESTION_TO_NATE = "question to nate"
    ANSWER_TO_NATE = "answer to nate"


class AnswerRead(StrEnum):
    UNREAD = "unread"
    READ = "read"
    UNKNOWN = "read state not recorded"
    NOT_TRACKED = "the answer went to staff"


class TimeNotRecorded(StrEnum):
    ABSENT = "time not recorded"


class QuestionWithoutAnswer(StrEnum):
    OPEN = "waiting for an answer"
    CLOSED = "closed without a saved answer"


class Nate(StrEnum):
    USER = "nate"


@dataclass(frozen=True)
class RecordedAnswerer:
    """The List log records this name without a fixed staff ID."""

    name: str


@dataclass(frozen=True)
class ExchangeAnswer:
    text: str
    time: datetime | TimeNotRecorded
    read: AnswerRead


@dataclass(frozen=True)
class QuestionRecord:
    identity: str


@dataclass(frozen=True)
class QuestionSlot:
    identity: str


@dataclass(frozen=True)
class QuestionEvent:
    position: int
    slot_id: str = ""


type QuestionOrigin = QuestionRecord | QuestionSlot | QuestionEvent


@dataclass(frozen=True)
class TaggedReply:
    tag: str


@dataclass(frozen=True)
class LoggedReply:
    position: int
    digest: str


class ReplyWithoutEvent(StrEnum):
    SLOT = "slot answer"
    NATIVE_RECORD = "native record answer"
    NO_ANSWER = "no saved answer"


type ReplyOrigin = TaggedReply | LoggedReply | ReplyWithoutEvent


@dataclass(frozen=True)
class ExchangeBinding:
    question: QuestionOrigin
    reply: ReplyOrigin

    @property
    def slot_id(self) -> str:
        if isinstance(self.question, QuestionSlot):
            return self.question.identity
        if isinstance(self.question, QuestionEvent):
            return self.question.slot_id
        return ""


class UnboundExchange(StrEnum):
    LEGACY = "no binding recorded"


@dataclass(frozen=True)
class Exchange:
    source: Source
    source_key: str
    exchange_id: str
    direction: Direction
    question: str
    asked_at: datetime | TimeNotRecorded
    asker: StaffId | Nate
    answerer: StaffId | Nate | RecordedAnswerer
    answer: ExchangeAnswer | QuestionWithoutAnswer
    binding: ExchangeBinding | UnboundExchange = field(default=UnboundExchange.LEGACY, kw_only=True)


@dataclass(frozen=True)
class FiledExchange(Exchange):
    """An exchange stored on a List item."""

    # Keep the old encoding for receipt verification until the writer saves the new form.
    legacy_binding: str = field(default="", kw_only=True)

    @classmethod
    def from_exchange(cls, value: Exchange) -> FiledExchange:
        return cls(value.source, value.source_key, value.exchange_id, value.direction, value.question,
                   value.asked_at, value.asker, value.answerer, value.answer, binding=value.binding)

    def updated(self, current: Exchange) -> FiledExchange:
        """Keep the saved answer and binding as the source changes."""
        incoming = current if isinstance(current, FiledExchange) else self.from_exchange(current)
        binding = incoming.binding
        if isinstance(self.binding, ExchangeBinding):
            if not isinstance(binding, ExchangeBinding) or self.binding.question != binding.question:
                return self
            if isinstance(self.answer, ExchangeAnswer):
                if self.binding.reply != binding.reply:
                    if not isinstance(binding.question, QuestionRecord):
                        return self
                    if self.binding.reply is not ReplyWithoutEvent.NATIVE_RECORD:
                        binding = self.binding
        if isinstance(self.answer, QuestionWithoutAnswer):
            return incoming
        if not isinstance(incoming.answer, ExchangeAnswer) or self.answer.text != incoming.answer.text:
            return self
        time = self.answer.time if isinstance(self.answer.time, datetime) else incoming.answer.time
        return replace(incoming, answer=ExchangeAnswer(self.answer.text, time, incoming.answer.read), binding=binding)


def _store_binding(binding: ExchangeBinding) -> dict[str, dict[str, str | int]]:
    match binding.question:
        case QuestionEvent(position, slot_id):
            question: dict[str, str | int] = {"kind": "log", "position": position}
            if slot_id:
                question["slot_id"] = slot_id
        case QuestionSlot(identity):
            question = {"kind": "slot", "id": identity}
        case QuestionRecord(identity):
            question = {"kind": "record", "id": identity}
    match binding.reply:
        case TaggedReply(tag):
            reply: dict[str, str | int] = {"kind": "tag", "tag": tag}
        case LoggedReply(position, digest):
            reply = {"kind": "event", "position": position, "digest": digest}
        case ReplyWithoutEvent() as unlogged:
            reply = {"kind": unlogged}
    return {"question": question, "reply": reply}


def _fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid exchange binding")
    return cast(dict[str, object], value)


def _position(fields: dict[str, object]) -> int:
    value = fields["position"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid exchange event position")
    return value


def _read_binding(value: object) -> tuple[ExchangeBinding, str]:
    fields = _fields(value)
    question, reply = _fields(fields["question"]), _fields(fields["reply"])
    match _field(question, "kind"):
        case "event":
            position = _position(question)
            tag, slot_id = _field(question, "tag"), _field(question, "slot_id")
            origin: QuestionOrigin = QuestionSlot(tag) if tag else QuestionEvent(position, slot_id)
        case "log":
            origin = QuestionEvent(_position(question), _nonempty_field(question, "slot_id") if "slot_id" in question else "")
        case "slot":
            origin = QuestionSlot(_nonempty_field(question, "id"))
        case "record":
            origin = QuestionRecord(_nonempty_field(question, "id"))
        case _:
            raise ValueError("invalid exchange question origin")
    match _field(reply, "kind"):
        case "tag":
            answer: ReplyOrigin = TaggedReply(_nonempty_field(reply, "tag"))
        case "event":
            digest = _field(reply, "digest")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("invalid exchange event digest")
            answer = LoggedReply(_position(reply), digest)
        case "answer held only in the slot":
            answer = ReplyWithoutEvent.NATIVE_RECORD if isinstance(origin, QuestionRecord) else ReplyWithoutEvent.SLOT
            if isinstance(origin, QuestionEvent) and origin.slot_id:
                origin = QuestionSlot(origin.slot_id)
        case kind:
            answer = ReplyWithoutEvent(kind)
    legacy = _field(question, "kind") == "event" or _field(reply, "kind") == "answer held only in the slot"
    return ExchangeBinding(origin, answer), json.dumps(fields, ensure_ascii=False) if legacy else ""


def _time_text(time: datetime | TimeNotRecorded) -> str:
    return time.isoformat() if isinstance(time, datetime) else ""


def _read_time(value: str) -> datetime | TimeNotRecorded:
    return datetime.fromisoformat(value) if value else TimeNotRecorded.ABSENT


def encode(exchanges: tuple[FiledExchange, ...]) -> str:
    rows: list[dict[str, object]] = []
    for exchange in exchanges:
        answer = exchange.answer
        answerer = exchange.answerer
        rows.append({
            "source": exchange.source, "source_key": exchange.source_key, "exchange_id": exchange.exchange_id,
            "direction": exchange.direction, "question": exchange.question, "asked_at": _time_text(exchange.asked_at),
            "asker": exchange.asker, "answerer": {"name": answerer.name} if isinstance(answerer, RecordedAnswerer)
            else answerer, "answer": {"text": answer.text, "time": _time_text(answer.time), "read": answer.read}
            if isinstance(answer, ExchangeAnswer) else answer,
        })
        if isinstance(exchange.binding, ExchangeBinding):
            rows[-1]["binding"] = (cast(object, json.loads(exchange.legacy_binding))
                                   if exchange.legacy_binding else _store_binding(exchange.binding))
    return json.dumps(rows, ensure_ascii=False)


def _field(row: dict[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise ValueError(f"invalid exchange field {key}")
    return value


def _nonempty_field(row: dict[str, object], key: str) -> str:
    value = _field(row, key)
    if not value:
        raise ValueError(f"empty exchange field {key}")
    return value


def decode(raw: str) -> tuple[FiledExchange, ...]:
    rows = cast(object, json.loads(raw))
    if not isinstance(rows, list):
        raise ValueError("expected a list of exchanges")
    exchanges: list[FiledExchange] = []
    seen: set[tuple[str, str]] = set()
    for value in cast(list[object], rows):
        if not isinstance(value, dict):
            raise ValueError("invalid filed exchange")
        row = cast(dict[str, object], value)
        key, identity = _field(row, "source_key"), _field(row, "exchange_id")
        if not key or not identity or (key, identity) in seen:
            raise ValueError("invalid or repeated exchange identity")
        seen.add((key, identity))
        answer = row["answer"]
        if isinstance(answer, dict):
            fields = cast(dict[str, object], answer)
            answer = ExchangeAnswer(_field(fields, "text"), _read_time(_field(fields, "time")),
                                    AnswerRead(_field(fields, "read")))
        elif isinstance(answer, str):
            answer = QuestionWithoutAnswer(answer)
        else:
            raise ValueError("invalid filed answer")
        asker = _field(row, "asker")
        answerer = row["answerer"]
        if isinstance(answerer, dict):
            answerer = RecordedAnswerer(_field(cast(dict[str, object], answerer), "name"))
        elif isinstance(answerer, str):
            answerer = Nate.USER if answerer == Nate.USER else StaffId(answerer)
        else:
            raise ValueError("invalid filed answerer")
        binding, legacy = _read_binding(row["binding"]) if "binding" in row else (UnboundExchange.LEGACY, "")
        exchanges.append(FiledExchange(Source(_field(row, "source")), key, identity,
            Direction(_field(row, "direction")), _field(row, "question"), _read_time(_field(row, "asked_at")),
            Nate.USER if asker == Nate.USER else StaffId(asker), answerer, answer,
            binding=binding, legacy_binding=legacy))
    return tuple(exchanges)
