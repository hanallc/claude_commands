"""Share the queue rule and Later values for List and Review."""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
import json
import re

from scripts.review_links.review_events import ReviewLog


class Queue(StrEnum):
    NEEDS_ME = "needs-me"
    WAITING_FOR_ANSWER = "waiting-for-answer"
    LATER = "later"
    WORKING = "working"
    BLOCKED = "blocked"
    VALIDATING = "validating"
    DONE = "done"
    CLOSED = "closed"


class UntilDelivered(StrEnum):
    DELIVERED = "delivered"


@dataclass(frozen=True)
class NoLaterTime:
    """The item has no active or expired Later value."""


type LaterValue = datetime | UntilDelivered
type Later = LaterValue | NoLaterTime
NO_LATER = NoLaterTime()
NEW_ANSWER = "New answer"
NEW_ANSWER_HTML = '<small data-new-answer>' + NEW_ANSWER + '</small>'


def read_later(text: str, local_time: Callable[[datetime], datetime]) -> LaterValue:
    """Read a time or the delivery condition in its existing storage format."""
    if text == UntilDelivered.DELIVERED:
        return UntilDelivered.DELIVERED
    value = datetime.fromisoformat(text)
    return local_time(value) if value.tzinfo is None else value


def read_legacy_later(value: object) -> datetime | NoLaterTime:
    """Read the local minute in a native Question or reminder."""
    if value is None:
        return NO_LATER
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value) is None:
        raise ValueError("expected YYYY-MM-DDTHH:MM")
    try:
        later = read_later(value, lambda time: time)
    except ValueError:
        raise ValueError("expected a valid YYYY-MM-DDTHH:MM") from None
    assert isinstance(later, datetime)
    return later


def write_later(value: Later) -> str:
    if isinstance(value, NoLaterTime):
        return ""
    return value.isoformat() if isinstance(value, datetime) else value.value


def later_label(value: LaterValue, format_time: Callable[[datetime], str]) -> str:
    if isinstance(value, datetime):
        return format_time(value)
    return "Until delivered"


def resolve_later_time(value: datetime, zone: tzinfo) -> datetime:
    """Apply the configured zone to a legacy time without an offset."""
    return value.replace(tzinfo=zone) if value.tzinfo is None else value


def later_active(value: Later, now: datetime) -> bool:
    return value is UntilDelivered.DELIVERED or isinstance(value, datetime) and value.timestamp() > now.timestamp()


def next_later_deadline(values: Iterable[Later], now: datetime) -> str:
    """Return the first future deadline in epoch seconds."""
    current = now.timestamp()
    future = (stamp for value in values if isinstance(value, datetime) and (stamp := value.timestamp()) > current)
    first = min(future, default=None)
    return '' if first is None else str(int(first))


@dataclass(frozen=True)
class NoAnswerTime:
    """The record has no saved answer time or Answer event."""


NO_ANSWER_TIME = NoAnswerTime()
type AnswerTime = datetime | NoAnswerTime


def answer_time(saved: AnswerTime, log: tuple[ReviewLog, ...]) -> AnswerTime:
    """Use the saved answer time or the latest time in the raw log."""
    if isinstance(saved, datetime):
        return saved
    return max((event.time for event in log if event.words.startswith('Answer: ')), default=saved)


def answer_log_id(question_id: str) -> str:
    """Use distinct log IDs for the question and its answer."""
    return 'answer:' + question_id


def has_unread_answer(answered_at: AnswerTime, log: tuple[ReviewLog, ...]) -> bool:
    """An answer is unread if no read action occurred at or after it."""
    return isinstance(answered_at, datetime) and not any(
        event.time >= answered_at and event.words.startswith(('Marked read', 'Later until ')) for event in log)


def queue(base: Queue, *, unread_answer: bool, later: bool, unread_delivery: bool = False,
          waiting_for_answer: bool = False) -> Queue:
    """Apply question and Later state to the item's own state."""
    if base is Queue.CLOSED:
        return base
    if unread_answer:
        return Queue.NEEDS_ME
    if later:
        return Queue.LATER
    if unread_delivery:
        return Queue.NEEDS_ME
    if waiting_for_answer:
        return Queue.WAITING_FOR_ANSWER
    return base


def queue_script() -> str:
    """Give the browser the same outcomes for changes to row data."""
    flags = (False, True)
    outcomes = {
        base: [queue(base, unread_answer=answer, later=later,
                     unread_delivery=delivery, waiting_for_answer=waiting)
               for answer in flags for later in flags for delivery in flags for waiting in flags]
        for base in Queue
    }
    return (f'const questionQueues={json.dumps(outcomes, separators=(",", ":"))};'
            +'const questionQueue=(base,answer,later,delivery,waiting)=>'
            +'questionQueues[base][8*answer+4*later+2*delivery+waiting];')
