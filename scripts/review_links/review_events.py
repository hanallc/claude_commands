"""Encode review events inside each item's existing store."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NotRequired, TypedDict, cast


@dataclass(frozen=True)
class ReviewLog:
    time: datetime
    who: str
    words: str
    feedback_request_id: str = ''

    def matches_feedback(self, request_id: str, text: str) -> bool:
        """Match by ID when the log has one."""
        if self.feedback_request_id:
            return self.feedback_request_id == request_id
        return self.words.rstrip().splitlines() == ('feedback: ' + text).rstrip().splitlines()


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
    feedback_request_id: NotRequired[str]


def decode(text: str) -> tuple[ReviewLog, ...]:
    if not text:
        return ()
    return from_rows(cast(list[StoredReviewLog], json.loads(text)))


def encode(events: tuple[ReviewLog, ...]) -> str:
    text = json.dumps(rows(events), ensure_ascii=False)
    return text.replace("\u0085", r"\u0085").replace("\u2028", r"\u2028").replace("\u2029", r"\u2029")


def from_rows(stored: list[StoredReviewLog]) -> tuple[ReviewLog, ...]:
    return tuple(ReviewLog(datetime.fromisoformat(row['time']), row['who'], row['words'],
                           row.get('feedback_request_id', '')) for row in stored)


def rows(events: tuple[ReviewLog, ...]) -> list[StoredReviewLog]:
    stored: list[StoredReviewLog] = []
    for event in events:
        row: StoredReviewLog = {'time': event.time.isoformat(), 'who': event.who, 'words': event.words}
        if event.feedback_request_id:
            row['feedback_request_id'] = event.feedback_request_id
        stored.append(row)
    return stored


def append(events: tuple[ReviewLog, ...], event: ReviewLog) -> tuple[ReviewLog, ...]:
    if event.feedback_request_id:
        if any(saved.feedback_request_id == event.feedback_request_id for saved in events):
            return events
    elif events:
        last = events[-1]
        if not last.feedback_request_id and (last.who, last.words) == (event.who, event.words):
            return events
    return (*events, event)
