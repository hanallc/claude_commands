"""Encode review events inside each item's existing store."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypedDict, cast


@dataclass(frozen=True)
class ReviewLog:
    time: datetime
    who: str
    words: str


class ReviewMark(StrEnum):
    """Whose turn a page action leaves while the store's own record stays open."""

    DONE = 'done'
    WAITING = 'waiting'


@dataclass(frozen=True)
class NoMark:
    """The store's own record alone sets the item's turn."""


@dataclass(frozen=True)
class KeepMark:
    """A log line that leaves the mark as it is."""


type MarkChange = ReviewMark | NoMark | KeepMark


def require_waiting(is_waiting: bool) -> None:
    """Refuse a reason update after the item leaves Waiting."""
    if not is_waiting:
        raise ValueError('The item is not Waiting.')


def require_waiting_why(text: str) -> str:
    """Require the filer's reason before an item enters Waiting."""
    reason = ' '.join(text.split())
    if not reason:
        raise ValueError('Waiting items need a why this is waiting text.')
    return reason


def waiting_why(value: object) -> str:
    """Read the stored reason; older records have no reason."""
    return value.strip() if isinstance(value, str) else ''


def changed_mark(before: ReviewMark | NoMark, change: MarkChange) -> ReviewMark | NoMark:
    return before if isinstance(change, KeepMark) else change


class StoredReviewLog(TypedDict):
    time: str
    who: str
    words: str


def decode(text: str) -> tuple[ReviewLog, ...]:
    if not text:
        return ()
    return from_rows(cast(list[StoredReviewLog], json.loads(text)))


def encode(events: tuple[ReviewLog, ...]) -> str:
    return json.dumps(rows(events), ensure_ascii=False).replace("\u2028", r"\u2028").replace("\u2029", r"\u2029")


def from_rows(stored: list[StoredReviewLog]) -> tuple[ReviewLog, ...]:
    return tuple(ReviewLog(datetime.fromisoformat(row['time']), row['who'], row['words']) for row in stored)


def rows(events: tuple[ReviewLog, ...]) -> list[StoredReviewLog]:
    return [{'time':event.time.isoformat(),'who':event.who,'words':event.words} for event in events]


def append(events: tuple[ReviewLog, ...], event: ReviewLog) -> tuple[ReviewLog, ...]:
    return events if events and (events[-1].who, events[-1].words) == (event.who, event.words) else (*events, event)
