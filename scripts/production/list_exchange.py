"""Question and answer history filed on a List item."""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class FiledExchange(Exchange):
    """An exchange stored on a List item."""

    @classmethod
    def from_exchange(cls, value: Exchange) -> FiledExchange:
        return cls(value.source, value.source_key, value.exchange_id, value.direction, value.question,
                   value.asked_at, value.asker, value.answerer, value.answer)


def _legacy_content(exchange: Exchange, answer: ExchangeAnswer) -> FiledExchange:
    return replace(FiledExchange.from_exchange(exchange), exchange_id="", answer=replace(answer, read=AnswerRead.UNKNOWN))


def retain_legacy_ids[Entry: Exchange](exchanges: tuple[Entry, ...], saved: Iterable[FiledExchange]) -> tuple[Entry, ...]:
    """Keep the filed ID when one anonymous log exchange matches one old slot exchange."""
    current_ids = {(exchange.source_key, exchange.exchange_id) for exchange in exchanges}
    saved_ids: set[tuple[str, str]] = set()
    old_slots: dict[FiledExchange, list[str]] = defaultdict(list)
    for exchange in saved:
        key = exchange.source_key, exchange.exchange_id
        saved_ids.add(key)
        if (exchange.source is Source.SCOPED_REPLY and exchange.exchange_id.startswith("question:")
                and key not in current_ids and isinstance(exchange.answer, ExchangeAnswer)):
            old_slots[_legacy_content(exchange, exchange.answer)].append(exchange.exchange_id)
    if not old_slots:
        return exchanges
    log_entries: dict[FiledExchange, list[int]] = defaultdict(list)
    for index, exchange in enumerate(exchanges):
        if (exchange.source is Source.SCOPED_REPLY and exchange.exchange_id.startswith("log:")
                and (exchange.source_key, exchange.exchange_id) not in saved_ids
                and isinstance(exchange.answer, ExchangeAnswer)):
            content = _legacy_content(exchange, exchange.answer)
            if content in old_slots:
                log_entries[content].append(index)
    identified = list(exchanges)
    for content, positions in log_entries.items():
        identities = old_slots[content]
        if len(positions) == len(identities) == 1:
            index = positions[0]
            identified[index] = replace(exchanges[index], exchange_id=identities[0])
    return tuple(identified)


def _time_text(time: datetime | TimeNotRecorded) -> str:
    return time.isoformat() if isinstance(time, datetime) else ""


def _read_time(value: str) -> datetime | TimeNotRecorded:
    return datetime.fromisoformat(value) if value else TimeNotRecorded.ABSENT


def encode(exchanges: tuple[FiledExchange, ...]) -> str:
    rows: list[dict[str, str | dict[str, str]]] = []
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
    return json.dumps(rows, ensure_ascii=False)


def _field(row: dict[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise ValueError(f"invalid exchange field {key}")
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
        exchanges.append(FiledExchange(Source(_field(row, "source")), key, identity,
            Direction(_field(row, "direction")), _field(row, "question"), _read_time(_field(row, "asked_at")),
            Nate.USER if asker == Nate.USER else StaffId(asker), answerer, answer))
    return tuple(exchanges)
