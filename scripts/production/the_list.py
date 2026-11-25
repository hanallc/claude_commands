"""Keep The List as numbered records under morty's fixed staff ID."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from enum import Enum, StrEnum
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

from scripts.console_text import check as console_text

from scripts.lib import conf
from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, sync_folder, write_text_atomic
from scripts.lib.user_zone import user_zone, zone_label
from scripts.message import send_result, session_send
from scripts.production import delivery_state, record_store, session_pane, staff
from scripts.review_links import change_page, page_question, question_path, question_record


class ListError(ValueError):
    """The item cannot accept the requested operation."""


class SavedReadFailed(OSError):
    """The save is on disk, but the item could not be read again."""


class RetiredListProducer(ListError):
    """A retired producer cannot take an open List item."""


class ItemState(StrEnum):
    NEW = "New"
    WORKING = "Working"
    BLOCKED = "Blocked"
    WAITING_FOR_NATE = "Waiting for nate"
    DELIVERED = "Delivered"
    DONE = "Done"
    PAUSED = "Paused"
    CLOSED = "Closed"


class RequestedBy(StrEnum):
    NATE = "nate"
    DWIGHT = "dwight"


@dataclass(frozen=True)
class ReviewByShownId:
    """Select a review through its shown ID, which can be ambiguous."""

    value: str


@dataclass(frozen=True)
class ReviewByKey:
    """Select a review through an exact key."""

    value: str


class NoTimestamp(StrEnum):
    UNSET = ""


Timestamp = datetime | NoTimestamp


ParkUntilDelivered = question_path.UntilDelivered
Parking = question_path.LaterValue | NoTimestamp


class EtaStatus(StrEnum):
    NOT_ASKED = "Not asked"
    PENDING = "Pending"
    NO_TIME_YET = "No time yet"
    WAITING_FOR_NATE = "Waiting for nate"
    PAUSED = "Paused"
    DELIVERED = "Delivered"


@dataclass(frozen=True)
class EtaRange:
    earliest: datetime
    latest: datetime


@dataclass(frozen=True)
class EtaDescription:
    text: str


@dataclass(frozen=True)
class CheckCondition:
    text: str


class UnassignedOwner(StrEnum):
    NATE = "nate"
    NOT_RECORDED = "Not recorded"


ItemOwner = staff.StaffId | UnassignedOwner
NextCheck = Timestamp | CheckCondition
Eta = datetime | date | EtaRange | EtaStatus | EtaDescription


@dataclass(frozen=True)
class ItemDependency:
    number: int
    condition: str


@dataclass(frozen=True)
class ItemChange:
    time: datetime
    what: LogContent
    source: str


@dataclass(frozen=True)
class UnreadUpdate:
    """Identify the unread update that a feedback request can mark read."""

    fingerprint: str


class NoUnreadUpdate(StrEnum):
    ABSENT = ""


class ReadCurrentUpdate(StrEnum):
    CURRENT = "current update"


@dataclass(frozen=True)
class PageRequest:
    """Identify the action and its input for a retry."""

    request: str
    action: Literal["tell", "remove", "park", "unpark", "read", "question"]
    words: str
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET


class NoPageRequest(StrEnum):
    ABSENT = ""


@dataclass(frozen=True)
class QuestionRequestReceipt:
    request: PageRequest
    question_id: str


class UnparkNoChangeReason(StrEnum):
    EXPIRED = "expired"
    NO_ACTIVE_PARKING = "no_active_parking"


@dataclass(frozen=True)
class UnparkNoChangeReceipt:
    request: PageRequest
    reason: UnparkNoChangeReason


class LegacyFeedbackRecipient(StrEnum):
    KEEPER = ""


type FeedbackRecipient = staff.StaffId | LegacyFeedbackRecipient


@dataclass(frozen=True)
class PageFeedback:
    request: str
    words: str
    state: Literal["pending", "sent", "alerted"] = "pending"
    alerted_to: str = ""
    read_update: UnreadUpdate | NoUnreadUpdate = NoUnreadUpdate.ABSENT
    action: Literal["tell", "remove", "park", "unpark", "read"] = "tell"
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET
    recipient: FeedbackRecipient = LegacyFeedbackRecipient.KEEPER

    def key(self) -> PageRequest:
        return PageRequest(self.request, self.action, self.words, self.form_words, self.parked_until)

    def recipient_id(self, keeper: staff.StaffId) -> staff.StaffId:
        return keeper if self.recipient is LegacyFeedbackRecipient.KEEPER else self.recipient


@dataclass(frozen=True)
class PageItemNotice:
    """Save an item action and its notice in the same write."""

    request: str
    words: str
    action: Literal["remove", "park", "unpark"]
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET
    recipient: FeedbackRecipient = LegacyFeedbackRecipient.KEEPER


class AskKind(StrEnum):
    GO = "go"
    APPROVAL = "approval"


ASK_ID = re.compile(r"A[1-9][0-9]*")


@dataclass(frozen=True)
class Ask:
    """Name one action that waits for nate's Go or Approved on the List page."""

    ask_id: str
    kind: AskKind
    what: str
    why: str
    target: staff.StaffId
    config: bool
    opened: datetime
    closed: Timestamp = NoTimestamp.UNSET
    go_id: str = ""
    withdrawn: bool = False

    @property
    def is_open(self) -> bool:
        return not isinstance(self.closed, datetime)


PAGE_SOURCE = "nate via List"
_LEGACY_RECEIPT = "List request receipt: "
_LEGACY_FEEDBACK = "Feedback to morty: "
feedback_claims: set[tuple[Path, str]] = set()
feedback_claims_lock = threading.Lock()


@dataclass(frozen=True)
class ListItem:
    number: int
    title: str
    owner: ItemOwner
    producer: staff.StaffId
    unit: str
    state: ItemState
    next_check: NextCheck
    eta: Eta
    unread_since: Timestamp
    parked_until: Parking
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    opened: Timestamp
    updated: Timestamp
    closed: Timestamp
    closing_words: str
    next_step: str
    latest_status: str
    evidence: str
    done_when: str
    log: tuple[ItemChange, ...]
    reviews: tuple[str, ...] = ()
    questions: tuple[question_record.Question, ...] = ()
    asks: tuple[Ask, ...] = ()

    def __post_init__(self) -> None:
        if self.state != ItemState.CLOSED or not isinstance(self.unread_since, datetime):
            return
        # A Closed item can have an unread answer after its last close.
        for entry in reversed(self.log):
            if isinstance(entry.what, QuestionAnswered):
                if entry.time == self.unread_since:
                    return
                break
            if isinstance(entry.what, MarkedRead) or isinstance(entry.what, FieldChange) and entry.what.action == "Closed":
                break
        object.__setattr__(self, "unread_since", NoTimestamp.UNSET)

    @property
    def id(self) -> str:
        return f"L{self.number}"

    @property
    def latest_status_time(self) -> Timestamp:
        for entry in reversed(self.log):
            if isinstance(entry.what, FieldChange) and "latest_status" in entry.what.fields:
                return entry.time
        return NoTimestamp.UNSET


class AllItems(StrEnum):
    INCLUDED = "all items"


@dataclass(frozen=True)
class SavedEdit:
    before: ListItem
    item: ListItem


@dataclass(frozen=True)
class UnchangedEdit:
    item: ListItem


ListEdit = SavedEdit | UnchangedEdit


class ProducerRecordOmission(StrEnum):
    OUTSIDE_SCOPE = "outside the producer's scope"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ProducerListRead:
    items: list[ListItem]
    incomplete: bool


class ItemChanges(TypedDict, total=False):
    title: str
    owner: ItemOwner
    producer: staff.StaffId
    unit: str
    state: ItemState
    next_check: NextCheck
    eta: Eta
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    next_step: str
    latest_status: str
    evidence: str
    done_when: str


class LogFields(ItemChanges, total=False):
    parked_until: Parking
    closing_words: str


@dataclass(frozen=True)
class FieldChange:
    action: Literal["Added", "Updated", "Parked", "Closed"]
    fields: LogFields


@dataclass(frozen=True)
class LegacyFieldChange(FieldChange):
    """Keep the original note for the unread update identity."""

    note: str


@dataclass(frozen=True)
class PageParkingChange(FieldChange):
    """Link the parking change to the List page request."""

    action: Literal["Parked"]
    request: str


@dataclass(frozen=True)
class QuestionAnswered:
    question_id: str


@dataclass(frozen=True)
class QuestionClosed:
    question: question_record.Question


@dataclass(frozen=True)
class MarkedRead:
    pass


@dataclass(frozen=True)
class Reopened:
    pass


@dataclass(frozen=True)
class Unparked:
    pass


class ItemDelivered(StrEnum):
    RECORDED = "delivered"


@dataclass(frozen=True)
class LinkedChangeDelivered:
    key: str


@dataclass(frozen=True)
class UnparkedForDelivery(Unparked):
    reason: ItemDelivered | LinkedChangeDelivered


@dataclass(frozen=True)
class ReviewLinksChanged:
    action: Literal["Linked reviews", "Unlinked reviews"]
    keys: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceCopyRemoved:
    copy: Literal["checkins.json", "the List row"]


@dataclass(frozen=True)
class AskOpened:
    """Keep the ask as the producer opened it."""

    ask: Ask


@dataclass(frozen=True)
class AskWithdrawn:
    ask: Ask
    reason: str


@dataclass(frozen=True)
class GoGiven:
    """Keep the closed ask with the go id of nate's click."""

    ask: Ask


@dataclass(frozen=True)
class GoNotDelivered:
    """Show nate that the Go message did not reach one recipient."""

    ask: Ask
    recipient: staff.StaffId


PageActivity = PageFeedback | PageRequest | QuestionRequestReceipt | UnparkNoChangeReceipt
ListEvent = (FieldChange | QuestionAnswered | QuestionClosed | MarkedRead | Reopened | Unparked
             | ReviewLinksChanged | EvidenceCopyRemoved | AskOpened | AskWithdrawn | GoGiven | GoNotDelivered | PageActivity)
LogContent = str | ListEvent


class NextNumber(StrEnum):
    AVAILABLE = "next available number"


def _store(keeper: staff.StaffId) -> record_store.NumberedRecords:
    return record_store.NumberedRecords(keeper, "list", "L", "list item", ListError)


def folder(keeper: staff.StaffId) -> Path:
    return _store(keeper).folder()


def now() -> datetime:
    return datetime.now(UTC)


def time_value(text: str) -> Timestamp:
    """Read an offset time or a local date; an empty field has no timestamp."""
    if not text:
        return NoTimestamp.UNSET
    value = datetime.fromisoformat(text)
    if len(text) == 10:
        value = value.replace(tzinfo=user_zone())
    _validate_time(value)
    return value


def read_parking_time(text: str) -> question_path.LaterValue:
    def local_date(value: datetime) -> datetime:
        if len(text) != 10:
            _validate_time(value)
        return value.replace(tzinfo=user_zone())

    return question_path.read_later(text, local_date)


def parking_value(text: str) -> Parking:
    return read_parking_time(text) if text else NoTimestamp.UNSET


def later_value(parking: Parking) -> question_path.Later:
    return question_path.NO_LATER if parking is NoTimestamp.UNSET else parking


def parking_active(value: Parking, moment: datetime) -> bool:
    return question_path.later_active(later_value(value), moment)


def _request_parking(value: Parking) -> Parking:
    return value.astimezone(UTC) if isinstance(value, datetime) else value


def _required_time(text: str) -> datetime:
    value = time_value(text)
    if not isinstance(value, datetime):
        raise ListError("a required time is empty")
    return value


def parking_date(moment: datetime, zone: tzinfo) -> str:
    return moment.astimezone(zone).date().isoformat()


def list_time(moment: datetime, today: datetime) -> str:
    local = moment.astimezone(today.tzinfo)
    return local.strftime("%H:%M %Z" if local.date() == today.date() else "%Y-%m-%d %H:%M %Z")


def _time_text(value: Timestamp) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:T[0-9:.+Z-]+)?")


def next_check_value(text: str) -> NextCheck:
    if not text or ISO_TIME.fullmatch(text):
        return time_value(text)
    _line(text, "next check")
    return CheckCondition(text)


def next_check_text(value: NextCheck) -> str:
    return value.text if isinstance(value, CheckCondition) else _time_text(value)


def eta_value(text: str) -> Eta:
    if text in EtaStatus:
        return EtaStatus(text)
    parts = text.split("/")
    if len(parts) not in (1, 2) or not all(ISO_TIME.fullmatch(part) for part in parts):
        _line(text, "ETA")
        return EtaDescription(text)
    if len(parts) == 1:
        return _required_time(text)
    earliest, latest = (_required_time(part) for part in parts)
    if earliest > latest:
        raise ListError("the ETA range ends before it starts")
    return EtaRange(earliest, latest)


_ETA_INPUT_STATUSES = (EtaStatus.NO_TIME_YET, EtaStatus.WAITING_FOR_NATE, EtaStatus.PAUSED, EtaStatus.DELIVERED)
_ETA_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ETA_DATE = re.compile(r"([A-Z][a-z]{2}) ([0-9]{1,2})(?: ([01][0-9]|2[0-3]):([0-5][0-9]) (\S+))?")
_ETA_INPUT_ERROR = ("Use --eta with Mon D, Mon D HH:MM ZONE, No time yet, Waiting for nate, Paused, or Delivered. "
                    + "For other details, put the reason in --latest-status.")


def _cli_eta(text: str) -> datetime | date | EtaStatus:
    if text in _ETA_INPUT_STATUSES:
        return EtaStatus(text)
    match = _ETA_DATE.fullmatch(text)
    if match is None or match[1] not in _ETA_MONTHS:
        raise ListError(_ETA_INPUT_ERROR)
    month, day = _ETA_MONTHS.index(match[1]) + 1, int(match[2])
    zone = user_zone()
    today = now().astimezone(zone).date()
    dates: list[date] = []
    # Four years on each side also cover the nearest leap day.
    for year in range(max(date.min.year, today.year - 4), min(date.max.year, today.year + 4) + 1):
        try:
            dates.append(date(year, month, day))
        except ValueError:
            continue
    if not dates:
        raise ListError(_ETA_INPUT_ERROR)
    nearest = min(dates, key=lambda candidate: abs(candidate - today))
    if match[5] is None:
        return nearest
    moment = datetime(nearest.year, month, day, int(match[3]), int(match[4]), tzinfo=zone)
    for fold in (0, 1):
        local = moment.replace(fold=fold)
        if local.astimezone(UTC).astimezone(zone) == local and zone_label(local) == match[5]:
            return local
    raise ListError(_ETA_INPUT_ERROR)


def eta_text(value: Eta) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, EtaRange):
        return f"{value.earliest.isoformat()}/{value.latest.isoformat()}"
    return value.text if isinstance(value, EtaDescription) else value.value


def eta_due(eta: Eta) -> datetime | None:
    if isinstance(eta, datetime):
        return eta
    if isinstance(eta, date) and eta < date.max:
        return datetime.combine(eta + timedelta(days=1), time.min, tzinfo=user_zone())
    if isinstance(eta, EtaRange):
        return eta.latest
    return None


def _eta_kind(eta: Eta) -> Literal["text", "time or status"]:
    return "text" if isinstance(eta, EtaDescription) else "time or status"


def _read_eta(text: str, kind: str) -> Eta:
    if kind == "text":
        _line(text, "ETA")
        return EtaDescription(text)
    if kind == "time or status":
        if len(text) == 10 and ISO_TIME.fullmatch(text):
            return date.fromisoformat(text)
        eta = eta_value(text)
        if not isinstance(eta, EtaDescription):
            return eta
    raise ListError("the ETA value must match its kind")


def _line(value: str, name: str) -> None:
    if not value.strip() or value.splitlines() != [value]:
        raise ListError(f"{name} must have one nonempty line")


def _log_line(value: str, name: str) -> None:
    if not value.strip() or "\n" in value or "\r" in value:
        raise ListError(f"{name} must have one nonempty line")


_SOURCE_CLOCK = re.compile(r"(?:^|(?<=[\s,]))(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:[.,]\d+)?)?"
                           + r"(?: [A-Z]{2,6}|Z| ?[+-]\d{2}(?::?\d{2})?)?$")


def source_without_clock(source: str) -> str:
    """Remove a hand-written clock time from the end of a log source."""
    text = source.rstrip()
    for boundary in re.finditer(r"^|[ \t,]", text):
        start = boundary.end()
        suffix = text[start:]
        try:
            _ = datetime.fromisoformat(suffix)
        except ValueError:
            continue
        try:
            _ = date.fromisoformat(suffix)
        except ValueError:
            return text[:start].rstrip(" ,\t")

    clock = _SOURCE_CLOCK.search(text)
    return text[:clock.start()].rstrip(" ,\t") if clock else source


def staff_id(value: str, name: str) -> staff.StaffId:
    """Read a staff ID in a List record."""
    if not value or Path(value).name != value or value in (".", ".."):
        raise ListError(f"invalid {name} staff id")
    return staff.StaffId(value)


def _field_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ListError(f"invalid List field: {name}")
    return value


def _item_state(value: str) -> ItemState:
    if value not in ItemState:
        raise ListError("invalid item state")
    return ItemState(value)


def _requester(value: str) -> RequestedBy:
    if value not in RequestedBy:
        raise ListError("invalid requester")
    return RequestedBy(value)


def _validate_unit(value: str) -> None:
    if value and value.splitlines() != [value]:
        raise ListError("unit must have one line")


def _validate_time(value: Timestamp) -> None:
    if isinstance(value, datetime) and value.utcoffset() is None:
        raise ListError("a time must have a time zone")


def _validate_closing_words(value: str) -> None:
    if not value.strip():
        raise ListError("closing words must not be empty")


def _validate_dependencies(entries: tuple[ItemDependency, ...], number: int) -> None:
    for dependency in entries:
        if dependency.number < 1 or dependency.number == number:
            raise ListError("an item cannot depend on itself or a nonpositive number")
        _line(dependency.condition, "dependency condition")
    if len({entry.number for entry in entries}) != len(entries):
        raise ListError("a dependency number must occur only once")


def _validate_related(entries: tuple[int, ...], number: int) -> None:
    if any(entry < 1 or entry == number for entry in entries):
        raise ListError("a related number must name another positive item")


def _validate_review_keys(keys: tuple[str, ...]) -> None:
    if len(set(keys)) != len(keys):
        raise ListError("a review key must occur only once")
    for key in keys:
        _line(key, "review key")


def _validate_ask(ask: Ask) -> None:
    if ASK_ID.fullmatch(ask.ask_id) is None or ask.kind not in AskKind:
        raise ListError("invalid List ask")
    _line(ask.what, "ask what")
    _line(ask.why, "ask why")
    _ = staff_id(ask.target, "ask target")
    for moment in (ask.opened, ask.closed):
        _validate_time(moment)
    if ask.withdrawn and ask.is_open:
        raise ListError("a withdrawn ask needs a close time")
    if bool(ask.go_id) != (not ask.is_open and not ask.withdrawn) or any(char.isspace() for char in ask.go_id):
        raise ListError("only a closed ask with a Go has a go id, without spaces")


def _validate_asks(asks: tuple[Ask, ...]) -> None:
    for ask in asks:
        _validate_ask(ask)
    if len({ask.ask_id for ask in asks}) != len(asks):
        raise ListError("an ask id must occur only once")


def _stored_ask(ask: Ask) -> dict[str, object]:
    fields: dict[str, object] = {
        "ask_id": ask.ask_id, "kind": ask.kind.value, "what": ask.what, "why": ask.why, "target": ask.target,
        "config": ask.config, "opened": ask.opened.isoformat(), "closed": _time_text(ask.closed), "go_id": ask.go_id,
    }
    if ask.withdrawn:
        fields["withdrawn"] = True
    return fields


_ASK_FIELDS: frozenset[str] = frozenset(("ask_id", "kind", "what", "why", "target", "config", "opened", "closed", "go_id"))


def _read_ask(value: object) -> Ask:
    fields = _json_fields(value)
    withdrawn = fields.get("withdrawn") is True
    expected = _ASK_FIELDS | {"withdrawn"} if withdrawn else _ASK_FIELDS
    if fields.keys() != expected:
        raise ListError("invalid List ask")
    texts = {name: fields[name] for name in _ASK_FIELDS if name != "config"}
    if not all(isinstance(text, str) for text in texts.values()) or not isinstance(fields["config"], bool):
        raise ListError("invalid List ask")
    text = cast(dict[str, str], texts)
    if text["kind"] not in AskKind:
        raise ListError("invalid List ask kind")
    ask = Ask(text["ask_id"], AskKind(text["kind"]), text["what"], text["why"], staff.StaffId(text["target"]),
              fields["config"], _required_time(text["opened"]), time_value(text["closed"]), text["go_id"], withdrawn)
    _validate_ask(ask)
    return ask


def _read_asks(text: str) -> tuple[Ask, ...]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ListError("asks must be a list")
    asks = tuple(_read_ask(entry) for entry in cast(list[object], value))
    _validate_asks(asks)
    return asks


def open_asks(item: ListItem) -> tuple[Ask, ...]:
    return tuple(ask for ask in item.asks if ask.is_open)


def _validate(item: ListItem) -> None:
    if item.number < 1:
        raise ListError("number must be positive")
    _line(item.title, "title")
    _ = staff_id(item.owner, "owner")
    _ = staff_id(item.producer, "producer")
    _validate_unit(item.unit)
    _ = _item_state(item.state)
    _ = _requester(item.requested_by)
    for moment in (item.opened, item.updated, item.unread_since, item.closed):
        _validate_time(moment)
    if not isinstance(item.parked_until, ParkUntilDelivered):
        _validate_time(item.parked_until)
    _ = _read_eta(eta_text(item.eta), _eta_kind(item.eta))
    _ = next_check_value(next_check_text(item.next_check))
    if (item.state == ItemState.CLOSED) != isinstance(item.closed, datetime):
        raise ListError("only a Closed item must have a closed time")
    if item.state == ItemState.CLOSED:
        _validate_closing_words(item.closing_words)
    elif item.closing_words:
        raise ListError("only a Closed item can have closing words")
    _validate_dependencies(item.blocked_by, item.number)
    _validate_related(item.related_to, item.number)
    _validate_review_keys(item.reviews)
    _validate_asks(item.asks)
    for name, value in (("next_step", item.next_step), ("latest_status", item.latest_status),
                        ("evidence", item.evidence), ("done_when", item.done_when)):
        _ = _field_text(value, name)
    for change in item.log:
        if isinstance(change.what, str):
            _log_line(change.what, "log text")
        if isinstance(change.what, (PageFeedback, PageRequest, QuestionRequestReceipt, UnparkNoChangeReceipt)):
            _validate_page_source(change.source)
        _log_line(change.source, "source")
        if " — " in change.source or change.time.utcoffset() is None:
            raise ListError("a log source cannot contain its separator; its time must have a time zone")


def _dependencies(value: object) -> tuple[ItemDependency, ...]:
    if not isinstance(value, list):
        raise ListError("blocked_by must be a list")
    entries: list[ItemDependency] = []
    for row in cast(list[object], value):
        if not isinstance(row, list):
            raise ListError("invalid dependency")
        cells = cast(list[object], row)
        if len(cells) != 2 or type(cells[0]) is not int or not isinstance(cells[1], str):
            raise ListError("invalid dependency")
        entries.append(ItemDependency(cells[0], cells[1]))
    return tuple(entries)


def _related(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ListError("related_to must be a list")
    entries = cast(list[object], value)
    if any(type(number) is not int for number in entries):
        raise ListError("related_to must contain item numbers")
    return tuple(cast(list[int], entries))


def _review_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ListError("reviews must be a list of keys")
    entries = cast(list[object], value)
    if any(not isinstance(entry, str) for entry in entries):
        raise ListError("reviews must be a list of keys")
    return tuple(cast(list[str], entries))


def _log_fields(fields: LogFields) -> dict[str, object]:
    values: dict[str, object] = dict(fields)
    if "producer" in fields:
        values["producer"] = staff_id(fields["producer"], "producer")
    if "eta" in fields:
        eta = fields["eta"]
        values["eta"] = {"value": eta_text(eta), "kind": _eta_kind(eta)}
    if "next_check" in fields:
        values["next_check"] = next_check_text(fields["next_check"])
    if "parked_until" in fields:
        values["parked_until"] = question_path.write_later(later_value(fields["parked_until"]))
    if "blocked_by" in fields:
        values["blocked_by"] = [(dependency.number, dependency.condition) for dependency in fields["blocked_by"]]
    return values


def _json_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ListError("a log object must contain named fields")
    return cast(dict[str, object], value)


def _read_log_fields(raw: object, number: int) -> LogFields:
    values: dict[str, object] = {}
    for field, value in _json_fields(raw).items():
        if field not in LogFields.__annotations__:
            raise ListError(f"unknown List log field: {field}")
        if field == "eta":
            eta = _json_fields(value)
            text, kind = eta.get("value"), eta.get("kind")
            if not isinstance(text, str) or not isinstance(kind, str):
                raise ListError("invalid log ETA")
            values[field] = _read_eta(text, kind)
        elif field == "blocked_by":
            dependencies = _dependencies(value)
            _validate_dependencies(dependencies, number)
            values[field] = dependencies
        elif field == "related_to":
            related = _related(value)
            _validate_related(related, number)
            values[field] = related
        else:
            text = _field_text(value, field)
            match field:
                case "owner":
                    identity = staff_id(text, field)
                    values[field] = UnassignedOwner(text) if text in UnassignedOwner else identity
                case "producer":
                    values[field] = staff_id(text, field)
                case "state":
                    values[field] = _item_state(text)
                case "requested_by":
                    values[field] = _requester(text)
                case "next_check":
                    values[field] = next_check_value(text)
                case "parked_until":
                    values[field] = parking_value(text)
                case _:
                    if field == "title":
                        _line(text, field)
                    elif field == "unit":
                        _validate_unit(text)
                    elif field == "closing_words":
                        _validate_closing_words(text)
                    values[field] = text
    return cast(LogFields, cast(object, values))


_LEGACY_CONSTRUCTORS: dict[str, Callable[..., object]] = {
    "EtaDescription": EtaDescription, "EtaRange": EtaRange, "CheckCondition": CheckCondition,
    "ItemDependency": ItemDependency, "datetime.datetime": datetime, "datetime.timedelta": timedelta,
    "datetime.timezone": timezone, "zoneinfo.ZoneInfo": ZoneInfo,
}
_LEGACY_FIELD = re.compile(r"([a-z_]+): ")


def _legacy_repr(node: ast.expr) -> object:
    if isinstance(node, ast.Tuple):
        return tuple(_legacy_repr(value) for value in node.elts)
    if isinstance(node, ast.Attribute) and ast.unparse(node) == "datetime.timezone.utc":
        return UTC
    if isinstance(node, ast.Call):
        constructor = _LEGACY_CONSTRUCTORS[ast.unparse(node.func)]
        kwargs: dict[str, object] = {}
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in kwargs:
                raise ListError("invalid arguments in old List log")
            kwargs[keyword.arg] = _legacy_repr(keyword.value)
        return constructor(*(_legacy_repr(arg) for arg in node.args), **kwargs)
    return cast(object, ast.literal_eval(node))


def _legacy_log_field(field: str, text: str) -> object:
    if field == "eta":
        if text.startswith(("EtaRange(", "EtaDescription(")):
            eta = _legacy_repr(ast.parse(text, mode="eval").body)
            if isinstance(eta, (EtaRange, EtaDescription)):
                return _log_fields({"eta": eta})["eta"]
            raise ListError("invalid ETA in old List log")
        candidate = text.replace(" ", "T", 1)
        eta = eta_value(candidate if ISO_TIME.fullmatch(candidate) else text)
        return _log_fields({"eta": eta})["eta"]
    if field == "next_check":
        if text.startswith("CheckCondition("):
            check = _legacy_repr(ast.parse(text, mode="eval").body)
            if isinstance(check, CheckCondition):
                return check.text
            raise ListError("invalid next check in old List log")
        candidate = text.replace(" ", "T", 1)
        return candidate if ISO_TIME.fullmatch(candidate) else text
    if field in ("blocked_by", "related_to"):
        value = _legacy_repr(ast.parse(text, mode="eval").body)
        if isinstance(value, tuple):
            parts = cast(tuple[object, ...], value)
            if field == "related_to":
                return list(parts)
            if all(isinstance(part, ItemDependency) for part in parts):
                return [[part.number, part.condition] for part in parts if isinstance(part, ItemDependency)]
        raise ListError("invalid item links in old List log")
    return text


def _read_legacy_note(note: str, source: str, number: int) -> LogContent:
    action, _, fields = note.partition(" ")
    try:
        if source == PAGE_SOURCE and note.startswith((_LEGACY_FEEDBACK, _LEGACY_RECEIPT)):
            receipt = note.startswith(_LEGACY_RECEIPT)
            prefix = _LEGACY_RECEIPT if receipt else _LEGACY_FEEDBACK
            activity = _json_fields(cast(object, json.loads(note.removeprefix(prefix))))
            if not receipt:
                _ = activity.setdefault("action", "tell")
            return _read_page_activity(activity, source, receipt=receipt)
        if note.startswith("Parked until "):
            date = note.removeprefix("Parked until ")
            until = NoTimestamp.UNSET if date == "not parked" else _required_time(date)
            return LegacyFieldChange("Parked", {"parked_until": until}, note)
        if action == "Closed:" and fields.strip():
            return LegacyFieldChange("Closed", {"closing_words": fields}, note)
    except ValueError:
        return note
    if action not in ("Added;", "Updated"):
        return note
    decoder = json.JSONDecoder()
    values: dict[str, object] = {}
    offset = 0
    try:
        while True:
            match = _LEGACY_FIELD.match(fields, offset)
            if match is None:
                return note
            field = match[1]
            text, end = cast(tuple[object, int], decoder.raw_decode(fields, match.end()))
            if not isinstance(text, str) or field in values:
                return note
            values[field] = _legacy_log_field(field, text)
            if end == len(fields):
                return LegacyFieldChange("Added" if action == "Added;" else "Updated", _read_log_fields(values, number), note)
            if not fields.startswith("; ", end):
                return note
            offset = end + 2
    except (AttributeError, KeyError, OverflowError, RecursionError, SyntaxError, TypeError, ValueError):
        return note


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ListError("enter a request ID without spaces")
    return value


def _question_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"Q[1-9][0-9]*", value) is None:
        raise ListError("invalid List question ID")
    return value


def _request_fields(request: PageRequest) -> dict[str, str]:
    fields = {"request": _request_id(request.request), "action": request.action, "words": request.words, "form_words": request.form_words}
    if request.parked_until is not NoTimestamp.UNSET:
        fields["parked_until"] = question_path.write_later(later_value(request.parked_until))
    return fields


def _log_content(content: LogContent) -> dict[str, object]:
    if isinstance(content, str):
        return {"note": content}
    if isinstance(content, LegacyFieldChange):
        return {"note": content.note}
    if isinstance(content, PageParkingChange):
        return {"action": content.action, "fields": _log_fields(content.fields), "request": _request_id(content.request)}
    if isinstance(content, FieldChange):
        return {"action": content.action, "fields": _log_fields(content.fields)}
    if isinstance(content, QuestionAnswered):
        return {"action": "Answered", "question_id": _question_id(content.question_id)}
    if isinstance(content, QuestionClosed):
        return {"action": "Question closed with item", "question": question_record.stored(content.question)}
    if isinstance(content, MarkedRead):
        return {"action": "Marked read"}
    if isinstance(content, Reopened):
        return {"action": "Reopened"}
    if isinstance(content, UnparkedForDelivery):
        if isinstance(content.reason, LinkedChangeDelivered):
            # Preserve old entries when saving the item.
            return {"action": "Unparked", "delivered_change": content.reason.key}
        return {"action": "Unparked", "delivered_item": True}
    if isinstance(content, Unparked):
        return {"action": "Unparked"}
    if isinstance(content, ReviewLinksChanged):
        return {"action": content.action, "keys": content.keys}
    if isinstance(content, EvidenceCopyRemoved):
        return {"action": "Removed Evidence copy", "copy": content.copy}
    if isinstance(content, AskOpened):
        return {"action": "Ask opened", "ask": _stored_ask(content.ask)}
    if isinstance(content, AskWithdrawn):
        return {"action": "Ask withdrawn", "ask": _stored_ask(content.ask), "reason": content.reason}
    if isinstance(content, GoGiven):
        return {"action": "Go given", "ask": _stored_ask(content.ask)}
    if isinstance(content, GoNotDelivered):
        return {"action": "Go not delivered", "ask": _stored_ask(content.ask), "recipient": content.recipient}
    if isinstance(content, PageFeedback):
        fields = _request_fields(content.key())
        fields.update(state=content.state, alerted_to=content.alerted_to)
        if isinstance(content.read_update, UnreadUpdate):
            fields["read_update"] = content.read_update.fingerprint
        if content.recipient is not LegacyFeedbackRecipient.KEEPER:
            fields["recipient"] = staff_id(content.recipient, "feedback recipient")
        return {"feedback": fields}
    if isinstance(content, QuestionRequestReceipt):
        return {"receipt": {**_request_fields(content.request), "question_id": _question_id(content.question_id)}}
    if isinstance(content, UnparkNoChangeReceipt):
        return {"receipt": {**_request_fields(content.request), "unpark_no_change": content.reason.value}}
    return {"receipt": _request_fields(content)}


def _read_field_change(action: Literal["Added", "Updated", "Parked", "Closed"], raw: object, number: int) -> FieldChange:
    fields = _read_log_fields(raw, number)
    match action:
        case "Added":
            valid = {"title", "owner", "producer", "state", "eta", "next_step", "latest_status"} <= fields.keys()
            valid = valid and fields.keys() <= ItemChanges.__annotations__.keys()
        case "Updated":
            valid = bool(fields) and fields.keys() <= ItemChanges.__annotations__.keys()
        case "Parked":
            valid = fields.keys() == {"parked_until"}
        case "Closed":
            valid = fields.keys() == {"closing_words"}
    if not valid:
        raise ListError(f"invalid fields in a {action} log entry")
    return FieldChange(action, fields)


def _read_log_line(line: str, number: int) -> ItemChange:
    if line.startswith("- {"):
        fields = _json_fields(cast(object, json.loads(line[2:])))
        time, source = fields.pop("time"), fields.pop("source")
        if not isinstance(time, str) or not isinstance(source, str):
            raise ListError("invalid log time or source")
        content: LogContent
        match fields:
            case {"note": str(note)} if len(fields) == 1:
                content = _read_legacy_note(note, source, number)
            case {"action": "Parked", "fields": values, "request": request} if len(fields) == 3:
                parking = _read_field_change("Parked", values, number)
                content = PageParkingChange("Parked", parking.fields, _request_id(request))
            case {"action": action, "fields": values} if len(fields) == 2 and action in ("Added", "Updated", "Parked", "Closed"):
                content = _read_field_change(action, values, number)
            case {"action": "Answered", "question_id": question_id} if len(fields) == 2:
                content = QuestionAnswered(_question_id(question_id))
            case {"action": "Question closed with item", "question": value} if len(fields) == 2:
                question = question_record.read(value)
                if question.answer is not None:
                    raise ListError("a question closed with the item has no answer")
                content = QuestionClosed(question)
            case {"action": "Marked read"} if len(fields) == 1:
                content = MarkedRead()
            case {"action": "Reopened"} if len(fields) == 1:
                content = Reopened()
            case {"action": "Unparked", "delivered_change": str(key)} if len(fields) == 2:
                _line(key, "review key")
                content = UnparkedForDelivery(LinkedChangeDelivered(key))
            case {"action": "Unparked", "delivered_item": True} if len(fields) == 2:
                content = UnparkedForDelivery(ItemDelivered.RECORDED)
            case {"action": "Unparked"} if len(fields) == 1:
                content = Unparked()
            case {"action": action, "keys": values} if len(fields) == 2 and action in ("Linked reviews", "Unlinked reviews"):
                keys = _review_keys(values)
                _validate_review_keys(keys)
                if not keys:
                    raise ListError("a review link event must name a review key")
                content = ReviewLinksChanged(action, keys)
            case {"action": "Removed Evidence copy", "copy": copy} if len(fields) == 2 and copy in ("checkins.json", "the List row"):
                content = EvidenceCopyRemoved(copy)
            case {"action": "Ask opened", "ask": value} if len(fields) == 2:
                ask = _read_ask(value)
                if not ask.is_open:
                    raise ListError("an opened ask has no close time")
                content = AskOpened(ask)
            case {"action": "Ask withdrawn", "ask": value, "reason": str(reason)} if len(fields) == 3:
                ask = _read_ask(value)
                if not ask.withdrawn:
                    raise ListError("an ask withdrawal needs a withdrawn ask")
                _line(reason, "withdrawal reason")
                content = AskWithdrawn(ask, reason)
            case {"action": "Go given", "ask": value} if len(fields) == 2:
                ask = _read_ask(value)
                if not ask.go_id:
                    raise ListError("a Go needs a closed ask with a go id")
                content = GoGiven(ask)
            case {"action": "Go not delivered", "ask": value, "recipient": str(recipient)} if len(fields) == 3:
                ask = _read_ask(value)
                _ = staff_id(recipient, "Go recipient")
                if not ask.go_id:
                    raise ListError("an undelivered Go needs a closed ask with a go id")
                content = GoNotDelivered(ask, staff.StaffId(recipient))
            case {"feedback": feedback} if len(fields) == 1:
                content = _read_page_activity(_json_fields(feedback), source, receipt=False)
            case {"receipt": receipt} if len(fields) == 1:
                content = _read_page_activity(_json_fields(receipt), source, receipt=True)
            case _:
                raise ListError("invalid List log entry")
        return ItemChange(_required_time(time), content, source)
    if not line.startswith("- "):
        raise ListError("invalid log line")
    dated, source = line[2:].rsplit(" — ", 1)
    date, clock, note = dated.split(" ", 2)
    return ItemChange(_required_time(date + "T" + clock), _read_legacy_note(note, source, number), source)


def load(keeper: staff.StaffId, identity: str, *, sources: list[Path] | None = None) -> ListItem:
    store = _store(keeper)
    fields, body = store.read(identity, sources=sources)
    return _read_item(store, identity, fields, body)


def _read_item(store: record_store.NumberedRecords, identity: str, fields: dict[str, str], body: str) -> ListItem:
    try:
        marker = "## Next step\n\n"
        if not body.startswith(marker):
            raise ListError("missing Next step section")
        body = body[len(marker):]
        texts: list[str] = []
        for key, name in (("next_step", "Latest status"), ("latest_status", "Evidence"),
                          ("evidence", "Done when"), ("done_when", "Log")):
            text, body = record_store.split_section(body, int(fields[key + "_length"]), name)
            texts.append(text)
        log_lines = body.removesuffix("\n").split("\n") if body else []
        number = int(fields["number"])
        changes = tuple(_read_log_line(line.removesuffix("\r"), number) for line in log_lines)
        item = ListItem(number, fields["title"],
                        UnassignedOwner(fields["owner"]) if fields["owner"] in UnassignedOwner else staff.StaffId(fields["owner"]),
                        staff.StaffId(fields.get("producer", store.owner)),
                        fields["unit"], _item_state(fields["state"]), next_check_value(fields["next_check"]),
                        _read_eta(fields["eta"], fields["eta_kind"]),
                        time_value(fields["unread_since"]), parking_value(fields["parked_until"]),
                        _dependencies(cast(object, json.loads(fields["blocked_by"]))),
                        _related(cast(object, json.loads(fields["related_to"]))), _requester(fields["requested_by"]),
                        time_value(fields["opened"]), time_value(fields["updated"]), time_value(fields["closed"]),
                        fields["closing_words"], texts[0], texts[1], texts[2], texts[3], changes,
                        _review_keys(cast(object, json.loads(fields.get("reviews", "[]")))),
                        delivery_state.read_list_questions(fields.get("questions", "[]")),
                        _read_asks(fields.get("asks", "[]")))
        if item.id != identity or not changes:
            raise ListError("the item number must match its path and the log must not be empty")
        _validate(item)
        return item
    except (ValueError, KeyError, TypeError) as error:
        raise store.invalid(identity, error) from None


def _require_active_open_producers(items: Sequence[ListItem]) -> None:
    producers = dict.fromkeys(item.producer for item in items if item.state != ItemState.CLOSED)
    if not producers:
        return
    try:
        record = staff.authority_snapshot()
    except staff.StaffAbsent:
        return
    for identity in producers:
        producer = record.by_id(identity)
        if producer is not None and isinstance(producer.retirement, staff.RetiredProducer):
            raise RetiredListProducer(f"{producer.name}: retired producer. Choose an active producer.")


def _save(store: record_store.NumberedRecords, item: ListItem) -> None:
    _validate(item)
    fields = {"id": item.id, "number": str(item.number), "title": item.title,
              "owner": item.owner, "producer": item.producer, "unit": item.unit, "state": item.state.value,
              "next_check": next_check_text(item.next_check), "eta": eta_text(item.eta),
              "eta_kind": _eta_kind(item.eta),
              "unread_since": _time_text(item.unread_since),
              "parked_until": question_path.write_later(later_value(item.parked_until)),
              "blocked_by": json.dumps([(entry.number, entry.condition) for entry in item.blocked_by], ensure_ascii=False),
              "related_to": json.dumps(item.related_to), "reviews": json.dumps(item.reviews),
              "requested_by": item.requested_by.value,
              "opened": _time_text(item.opened), "updated": _time_text(item.updated),
              "closed": _time_text(item.closed), "closing_words": item.closing_words}
    if item.questions:
        fields["questions"] = json.dumps([question_record.stored(question) for question in item.questions], ensure_ascii=False)
    if item.asks:
        fields["asks"] = json.dumps([_stored_ask(ask) for ask in item.asks], ensure_ascii=False)
    body = "## Next step\n\n" + item.next_step
    log = "\n".join("- " + json.dumps({"time": entry.time.isoformat(), "source": entry.source,
                                         **_log_content(entry.what)}, ensure_ascii=False) for entry in item.log)
    for key, text, heading, following in (("next_step", item.next_step, "Latest status", item.latest_status),
                                          ("latest_status", item.latest_status, "Evidence", item.evidence),
                                          ("evidence", item.evidence, "Done when", item.done_when),
                                          ("done_when", item.done_when, "Log", log)):
        fields[key + "_length"] = str(len(text))
        body += record_store.section(heading) + following
    store.write(item.id, fields, body)


def records(keeper: staff.StaffId, *, include_closed: bool = False,
            sources: list[Path] | None = None) -> list[ListItem]:
    items = _store(keeper).records(lambda identity: load(keeper, identity), sources=sources)
    return sorted((item for item in items if include_closed or item.state != ItemState.CLOSED), key=lambda item: item.number)


def _producer_records(keeper: staff.StaffId, producer: staff.StaffId, *, include_closed: bool) -> ProducerListRead:
    store = _store(keeper)

    def read_visible(identity: str) -> ListItem | ProducerRecordOmission:
        try:
            fields, body = store.read(identity)
            assigned = staff_id(fields.get("producer", keeper), "producer")
            if assigned != producer:
                return ProducerRecordOmission.OUTSIDE_SCOPE
            return _read_item(store, identity, fields, body)
        except (ListError, OSError):
            return ProducerRecordOmission.UNREADABLE

    try:
        # Request source paths so the directory scan reports read errors.
        results = store.records(read_visible, sources=[])
    except OSError:
        return ProducerListRead([], incomplete=True)
    items = [item for item in results if isinstance(item, ListItem) and (include_closed or item.state != ItemState.CLOSED)]
    return ProducerListRead(items, incomplete=ProducerRecordOmission.UNREADABLE in results)


@dataclass(frozen=True)
class WaitingForAnswer:
    """Each unanswered question reached the producer or has a live or scheduled send."""


@dataclass(frozen=True)
class QuestionAttention:
    failed: frozenset[str] = frozenset()


def question_visibility(keeper: staff.StaffId, item: ListItem,
                        status: change_page.DeliveryStatus) -> WaitingForAnswer | QuestionAttention:
    """Read the published state of each unanswered question."""
    unanswered = tuple(question for question in item.questions if question.answer is None)
    if not unanswered:
        return QuestionAttention()
    waiting = True
    failed: set[str] = set()
    for question in unanswered:
        target = _list_question_target(keeper, item, question.recipient_id(keeper))
        delivery = change_page.published_question_delivery_view(status, target.saved(question))
        waiting &= delivery.state.waits
        if delivery.state is delivery_state.QuestionSendState.FAILED:
            failed.add(question.question_id)
    return WaitingForAnswer() if waiting else QuestionAttention(frozenset(failed))


def _check_links(read: Callable[[int], ListItem], number: int, changes: ItemChanges) -> None:
    for related in changes.get("related_to", ()):
        _ = read(related)
    visited: set[int] = set()

    def visit(target: int) -> None:
        if target == number:
            raise ListError("dependencies must not form a cycle")
        if target in visited:
            return
        visited.add(target)
        for dependency in read(target).blocked_by:
            visit(dependency.number)

    for dependency in changes.get("blocked_by", ()):
        visit(dependency.number)


def create_records(keeper: staff.StaffId, items: Sequence[ListItem]) -> None:
    """Create complete typed records after checking the whole batch under one lock."""
    store = _store(keeper)
    with store.list_allocation():
        next_number = store.next_number()
        for item in items:
            if item.number < next_number:
                raise ListError(f"{item.id}: the next available number is L{next_number}")
        if any(store.folder().glob("*.md")):
            raise ListError("the list folder already holds a record")
        if os.path.lexists(store.counter_path()):
            raise ListError("the list folder already has a number sequence")
        pending = {item.number: item for item in items}
        if len(pending) != len(items):
            raise ListError("item numbers must be unique")
        def read(number: int) -> ListItem:
            item = pending.get(number)
            return item if item is not None else load(keeper, f"L{number}")
        for item in items:
            _validate(item)
            if not item.log:
                raise ListError("the log must not be empty")
            if store.path(item.id).exists():
                raise ListError(f"{item.id}: the item already exists")
            _check_links(read, item.number, {"blocked_by": item.blocked_by, "related_to": item.related_to})
        _require_active_open_producers(items)
        if pending:
            store.reserve_number(max(pending))
        for item in items:
            _save(store, item)


class _CreationDefaults(TypedDict):
    unit: str
    state: ItemState
    next_check: NextCheck
    eta: Eta
    parked_until: Parking
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    closing_words: str
    next_step: str
    latest_status: str
    evidence: str
    done_when: str


_CREATION_DEFAULTS: _CreationDefaults = {
    "unit": "", "state": ItemState.NEW, "next_check": NoTimestamp.UNSET, "eta": EtaStatus.NOT_ASKED,
    "parked_until": NoTimestamp.UNSET, "blocked_by": (), "related_to": (), "requested_by": RequestedBy.NATE,
    "closing_words": "", "next_step": "", "latest_status": "", "evidence": "", "done_when": "",
}


def add(keeper: staff.StaffId, title: str, *, owner: ItemOwner, source: str,
        changes: ItemChanges,
        number: int | NextNumber = NextNumber.AVAILABLE, requested_by: RequestedBy = _CREATION_DEFAULTS["requested_by"],
        assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED,
        ) -> ListItem:
    """Create an item without reusing an existing record number."""
    store = _store(keeper)
    def read_reference(number: int) -> ListItem:
        identity = f"L{number}"
        if assigned_to is AllItems.INCLUDED:
            return load(keeper, identity)
        try:
            fields, body = store.read(identity)
            if fields.get("producer", keeper) == assigned_to:
                return _read_item(store, identity, fields, body)
        except (ListError, OSError):
            pass
        raise ListError("the referenced List item does not exist")

    with store.list_allocation():
        next_number = store.next_number()
        chosen = next_number if isinstance(number, NextNumber) else number
        identity = f"L{chosen}"
        if os.path.lexists(store.path(identity)):
            raise ListError(f"{identity}: the item already exists")
        if chosen < next_number:
            raise ListError(f"{identity}: the next available number is L{next_number}")
        item = _new_item(chosen, title, owner, keeper, source, changes, requested_by=requested_by)
        _check_links(read_reference, item.number, changes)
        _require_active_open_producers((item,))
        store.reserve_number(chosen)
        _save(store, item)
        return item


def _new_item(number: int, title: str, owner: ItemOwner, keeper: staff.StaffId, source: str,
              changes: ItemChanges, *, requested_by: RequestedBy = RequestedBy.NATE,
              reviews: tuple[str, ...] = ()) -> ListItem:
    moment = now()
    defaults = _CREATION_DEFAULTS.copy()
    defaults["requested_by"] = requested_by
    item = ListItem(number, title, owner, keeper, unread_since=moment, opened=moment, updated=moment,
                    closed=NoTimestamp.UNSET, log=(), reviews=reviews, **defaults)
    if changes:
        item = replace(item, **changes)
    initial: LogFields = {"title": item.title, "owner": item.owner, "producer": item.producer,
                          "state": item.state, "eta": item.eta, "next_step": item.next_step,
                          "latest_status": item.latest_status, "requested_by": item.requested_by, **changes}
    log = (ItemChange(moment, FieldChange("Added", initial), source),)
    if reviews:
        log += (ItemChange(moment, ReviewLinksChanged("Linked reviews", reviews), source),)
    item = replace(item, log=log)
    _validate(item)
    return item


def reserved_review_number(path: Path) -> int | NextNumber:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return NextNumber.AVAILABLE
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ListError(f"{path}: invalid List number reservation")
    return int(value)


def add_review_item(keeper: staff.StaffId, title: str, *, owner: staff.StaffId, producer: staff.StaffId,
                    unit: str, words: str, evidence: str, review_key: str, source: str, reservation: Path) -> int:
    """Reserve a number for this Review filing and publish its linked row under the List lock."""
    store = _store(keeper)
    number = reserved_review_number(reservation)
    if isinstance(number, int) and store.path(f"L{number}").is_file():
        sync_folder(store.folder())
        return number
    with store.list_allocation():
        number = reserved_review_number(reservation)
        if isinstance(number, int) and store.path(f"L{number}").is_file():
            sync_folder(store.folder())
            return number
        chosen = store.next_number() if isinstance(number, NextNumber) else number
        item = _new_item(chosen, title, owner, keeper, source,
                         {"producer": producer, "unit": unit, "latest_status": words, "evidence": evidence},
                         reviews=(review_key,))
        _require_active_open_producers((item,))
        if isinstance(number, NextNumber):
            store.reserve_number(chosen)
            write_text_atomic(reservation, f"{chosen}\n", new_file_mode=0o600)
        _save(store, item)
        return chosen


def _change(keeper: staff.StaffId, identity: str, edit: Callable[[ListItem, datetime], ListItem],
            what: ListEvent | Callable[[ListItem], ListEvent], source: str, *,
            unread: bool | Callable[[ListItem], bool] = True,
            page_action: PageItemNotice | PageRequest | NoPageRequest = NoPageRequest.ABSENT,
            assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED,
            follow: Callable[[ListItem, ListItem, datetime], tuple[ItemChange, ...]] | None = None) -> ListEdit:
    """Save one edit and its log entry under the write lock.

    `follow` gives the entries that come after the edit's own entry, from the item before and after the edit.
    """
    store = _store(keeper)
    _ = store.path(identity)
    with store.locked():
        if assigned_to is AllItems.INCLUDED:
            item = load(keeper, identity)
        else:
            fields, body = store.read(identity)
            if staff_id(fields.get("producer", keeper), "producer") != assigned_to:
                raise ListError(f"{identity} is assigned to another producer. "
                                + "A producer can change only its own items.")
            item = _read_item(store, identity, fields, body)
        request = page_action
        if isinstance(page_action, PageItemNotice):
            if not page_action.words.strip():
                raise ListError("an item notice needs words")
            request = PageRequest(page_action.request, page_action.action, page_action.words, page_action.form_words,
                                  page_action.parked_until)
        if isinstance(request, PageRequest) and _previous_page_request(item, request) is not NoPageRequest.ABSENT:
            return UnchangedEdit(item)
        moment = now()
        changed = edit(item, moment)
        entries: list[ItemChange] = []
        if changed != item:
            event = what(changed) if callable(what) else what
            entries.append(ItemChange(moment, event, source))
            if follow is not None:
                entries.extend(follow(item, changed, moment))
            stays_unread = unread(changed) if callable(unread) else unread
            changed = replace(changed, updated=moment, unread_since=moment if stays_unread else NoTimestamp.UNSET)
            if (isinstance(event, FieldChange) and event.action == "Updated"
                    and event.fields.get("state") == ItemState.DELIVERED
                    and changed.parked_until is ParkUntilDelivered.DELIVERED):
                changed = replace(changed, parked_until=NoTimestamp.UNSET)
                entries.append(ItemChange(moment, UnparkedForDelivery(ItemDelivered.RECORDED), source))
        if isinstance(request, PageRequest):
            if entries and isinstance(page_action, PageItemNotice):
                pending = PageFeedback(page_action.request, page_action.words, action=page_action.action,
                                       form_words=request.form_words, parked_until=request.parked_until,
                                       recipient=page_action.recipient)
                entries.append(ItemChange(moment, pending, PAGE_SOURCE))
            elif request.action == "read" and changed.state != item.state:
                notice = _next_step_notice(keeper, request.request, "nate read", changed, form_words=request.words)
                entries.append(ItemChange(moment, notice, PAGE_SOURCE))
            elif request.action == "unpark":
                reason = (UnparkNoChangeReason.EXPIRED if isinstance(item.parked_until, datetime)
                          else UnparkNoChangeReason.NO_ACTIVE_PARKING)
                entries.insert(0, ItemChange(moment, UnparkNoChangeReceipt(request, reason), PAGE_SOURCE))
            else:
                entries.insert(0, ItemChange(moment, request, PAGE_SOURCE))
        if not entries:
            return UnchangedEdit(item)
        changed = replace(changed, log=(*changed.log, *entries))
        _validate(changed)
        if item.state == ItemState.CLOSED or changed.producer != item.producer:
            _require_active_open_producers((changed,))
        if isinstance(request, PageRequest):
            changed = _save_page_request(store, keeper, changed, request)
        else:
            _save(store, changed)
        return SavedEdit(item, changed)


def _open(item: ListItem) -> None:
    if item.state == ItemState.CLOSED:
        raise ListError(f"{item.id}: reopen the Closed item before changing it")


def _without_evidence_block(evidence: str, start: int, end: int) -> str:
    before = re.sub(r"(?:\r?\n[ \t]*)+\Z", "", evidence[:start])
    if not before.strip():
        before = ""
    after = re.sub(r"\A(?:[ \t]*\r?\n)+", "", evidence[end:])
    return before + (("\n\n" if before else "") + after if after.strip() else "")


def _without_checkins_copy(evidence: str) -> str:
    marker = re.search(r"(?m)^Source checkins\.json item:\r?\n", evidence)
    if marker is None:
        return evidence
    tail = evidence[marker.end():]
    copied_text = tail.lstrip()
    try:
        copied, end = cast(tuple[object, int], json.JSONDecoder().raw_decode(copied_text))
    except ValueError:
        return evidence
    if not isinstance(copied, dict) or re.match(r"[ \t]*(?:\r?\n|\Z)", copied_text[end:]) is None:
        return evidence
    return _without_evidence_block(evidence, marker.start(), marker.end() + len(tail) - len(copied_text) + end)


def _without_row_copy(evidence: str, number: int) -> str:
    heading = (rf"(?m)^## {number}\. [^\r\n]+\r?\n(?:[ \t]*\r?\n)+"
               + r"\*\*List state:\*\*[^\r\n]*\r?\n(?:[ \t]*\r?\n)+(?=- \*\*[^*\r\n]+:\*\*)")
    for match in reversed(list(re.finditer(heading, evidence))):
        end = match.end()
        for line in evidence[end:].splitlines(keepends=True):
            if line.strip() and re.match(r"(?:- \*\*[^*\r\n]+:\*\*|(?: {2,}|\t)[ \t]*\S)", line) is None:
                break
            end += len(line)
        evidence = _without_evidence_block(evidence, match.start(), end)
    return evidence


def clean_evidence(keeper: staff.StaffId, *, source: str) -> tuple[int, int]:
    """Remove migration copies from each record's Evidence section."""
    _log_line(source, "source")
    store = _store(keeper)
    changed = unchanged = 0
    with store.locked():
        for item in records(keeper, include_closed=True):
            evidence = _without_checkins_copy(item.evidence)
            cleaned = _without_row_copy(evidence, item.number)
            if cleaned == item.evidence:
                unchanged += 1
                continue
            moment = now()
            entries: list[ItemChange] = []
            if evidence != item.evidence:
                entries.append(ItemChange(moment, EvidenceCopyRemoved("checkins.json"), source))
            if cleaned != evidence:
                entries.append(ItemChange(moment, EvidenceCopyRemoved("the List row"), source))
            _save(store, replace(item, evidence=cleaned, updated=moment, log=(*item.log, *entries)))
            changed += 1
    return changed, unchanged


def update(keeper: staff.StaffId, identity: str, changes: ItemChanges, *, source: str) -> ListItem:
    return _update(keeper, identity, changes, source=source).item


_LAUNCHER = "~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py"
_PRODUCER_UPDATE_FIELDS = ("title", "done_when", "unit", "latest_status", "next_step", "eta", "state", "next_check",
                           "evidence", "add_evidence")
_PRODUCER_UPDATE_OPTIONS = tuple("--" + field.replace("_", "-") for field in _PRODUCER_UPDATE_FIELDS)


def _check_producer_update_fields(fields: Iterable[str]) -> None:
    refused = sorted("--" + field.replace("_", "-") for field in set(fields).difference(_PRODUCER_UPDATE_FIELDS))
    if refused:
        raise ListError(f"producers can update only {', '.join(_PRODUCER_UPDATE_OPTIONS)}. "
                        + f"Refused options: {', '.join(refused)}")


def _append_evidence(evidence: str, line: str) -> str:
    separator = "\n" if evidence and not evidence.endswith("\n") else ""
    return evidence + separator + line


def log_field_changes(item: ListItem) -> Generator[dict[str, object]]:
    """Replay the saved fields and yield the fields to show for each log entry."""
    previous: dict[str, object] = {}
    for entry in item.log:
        event = entry.what
        shown: dict[str, object] = {}
        if isinstance(event, FieldChange):
            fields: dict[str, object] = dict(event.fields)
            if event.action == "Added":
                previous = dict(_CREATION_DEFAULTS)
                _ = previous.pop("requested_by")
            if event.action == "Closed":
                previous["state"] = ItemState.CLOSED
            shown = fields
            if event.action == "Updated":
                shown = {field: value for field, value in fields.items()
                         if field not in previous or previous[field] != value}
                evidence, old_evidence = shown.get("evidence"), previous.get("evidence")
                if isinstance(evidence, str):
                    old_lines: set[str] = set(old_evidence.splitlines()) if isinstance(old_evidence, str) else set()
                    new_lines = [line for line in evidence.splitlines() if line not in old_lines]
                    if new_lines:
                        shown["evidence"] = "\n".join(new_lines) or "\n"
                    elif evidence or not old_evidence:
                        del shown["evidence"]
            previous.update(fields)
        elif isinstance(event, Reopened) or event == "Reopened":
            previous.update(state=ItemState.NEW, closing_words="", parked_until=NoTimestamp.UNSET)
        elif isinstance(event, Unparked):
            previous["parked_until"] = NoTimestamp.UNSET
            if isinstance(event, UnparkedForDelivery):
                previous["state"] = ItemState.DELIVERED
        yield shown


def _update(keeper: staff.StaffId, identity: str, changes: ItemChanges, *, source: str, add_evidence: str = "",
            assigned_to: staff.StaffRow | AllItems = AllItems.INCLUDED) -> ListEdit:
    scope: staff.StaffId | AllItems = AllItems.INCLUDED
    if assigned_to is not AllItems.INCLUDED:
        _check_producer_update_fields(changes)
        if assigned_to.kind != "producer":
            raise ListError("the assigned producer has no producer staff row")
        if isinstance(assigned_to.retirement, staff.RetiredProducer):
            raise ListError(f"{assigned_to.name}: retired producer. Choose an active producer.")
        source = assigned_to.name
        scope = assigned_to.id

    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        if changes.get("state") == ItemState.CLOSED:
            raise ListError("use close to record closing words and a time")
        changed = replace(item, **changes)
        if add_evidence:
            changed = replace(changed, evidence=_append_evidence(changed.evidence, add_evidence))
        _validate(changed)
        _check_links(lambda number: load(keeper, f"L{number}"), changed.number, changes)
        return changed

    def event(changed: ListItem) -> FieldChange:
        fields: LogFields = {**changes}
        if add_evidence:
            fields["evidence"] = changed.evidence
        return FieldChange("Updated", fields)
    return _change(keeper, identity, edit, event, source, assigned_to=scope)


def move_open_producer(keeper: staff.StaffId, identity: str, previous: staff.StaffId,
                       producer: staff.StaffId, *, source: str) -> ListItem:
    """Move an open item only while it belongs to the previous producer."""
    def edit(item: ListItem, _: datetime) -> ListItem:
        if item.state == ItemState.CLOSED or item.producer != previous:
            return item
        return replace(item, producer=producer)

    return _change(keeper, identity, edit, FieldChange("Updated", {"producer": producer}), source).item


@contextmanager
def require_no_open_producer_items(keeper: staff.StaffId, producer: staff.StaffId) -> Generator[None]:
    """Hold the List lock after confirming the producer has no open items."""
    store = _store(keeper)
    with store.locked():
        for path in store.folder().iterdir():
            if path.suffix == ".md" and store.valid_id(path.stem):
                item = load(keeper, path.stem)
                if item.state != ItemState.CLOSED and item.producer == producer:
                    raise ListError("new open items belong to the producer. Run agents retire again.")
        yield


def link(keeper: staff.StaffId, identity: str, reviews: Sequence[str], *, source: str) -> ListItem:
    """Add unique review keys after the command resolves the requested reviews."""
    chosen = tuple(dict.fromkeys(reviews))
    _validate_review_keys(chosen)
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, reviews=tuple(dict.fromkeys((*item.reviews, *chosen))))
    return _change(keeper, identity, edit, ReviewLinksChanged("Linked reviews", chosen), source).item


def unlink(keeper: staff.StaffId, identity: str, reviews: Sequence[str], *, source: str) -> ListItem:
    """Remove review keys while retaining the order of the other keys."""
    chosen = tuple(dict.fromkeys(reviews))
    _validate_review_keys(chosen)
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, reviews=tuple(review for review in item.reviews if review not in chosen))
    return _change(keeper, identity, edit, ReviewLinksChanged("Unlinked reviews", chosen), source).item


def park(keeper: staff.StaffId, identity: str, until: Parking, *, source: str,
         notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT,
         assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListItem:
    fields: LogFields = {"parked_until": until}
    event = FieldChange("Parked", fields)
    if isinstance(until, datetime):
        _validate_time(until)
    if isinstance(notice, PageItemNotice) and until is not NoTimestamp.UNSET:
        notice = replace(notice, parked_until=_request_parking(until))
        event = PageParkingChange("Parked", fields, notice.request)

    def edit(item: ListItem, moment: datetime) -> ListItem:
        if isinstance(notice, PageItemNotice) and not parking_active(until, moment):
            raise ListError("Choose a future parking date.")
        _open(item)
        if until is ParkUntilDelivered.DELIVERED and item.state in (ItemState.DELIVERED, ItemState.DONE):
            raise ListError("This item is already delivered.")
        return replace(item, parked_until=until)
    return _change(keeper, identity, edit, event, source, page_action=notice, assigned_to=assigned_to).item


def unread_delivery(item: ListItem) -> bool:
    if item.state != ItemState.DELIVERED or not isinstance(item.unread_since, datetime):
        return False
    for change in reversed(item.log):
        event = change.what
        if isinstance(event, (MarkedRead, Reopened)) or event in ("Marked read", "Reopened"):
            break
        if isinstance(event, UnparkedForDelivery):
            return True
    return False


def unpark(keeper: staff.StaffId, identity: str, *, source: str,
           notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT,
           assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListEdit:
    """Clear an active parking time and save its notice in the same write."""
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if not parking_active(item.parked_until, moment):
            return item
        _open(item)
        return replace(item, parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, Unparked(), source, page_action=notice, assigned_to=assigned_to)


def close(keeper: staff.StaffId, identity: str, words: str, *, source: str,
          notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT,
          assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListItem:
    _validate_closing_words(words)
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if item.state == ItemState.CLOSED:
            if item.closing_words == words:
                return item
            raise ListError("the item is already Closed with different closing words")
        answered = tuple(question for question in item.questions if question.answer is not None)
        closed_questions = tuple(ItemChange(moment, QuestionClosed(question), source)
                                 for question in item.questions if question.answer is None)
        return replace(item, state=ItemState.CLOSED, closed=moment, closing_words=words,
                       parked_until=NoTimestamp.UNSET, questions=answered, log=(*item.log, *closed_questions))
    return _change(keeper, identity, edit, FieldChange("Closed", {"closing_words": words}), source,
                   unread=False, page_action=notice, assigned_to=assigned_to).item


def reopen(keeper: staff.StaffId, identity: str, *, source: str,
           assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        if item.state != ItemState.CLOSED:
            return item
        return replace(item, state=ItemState.NEW, closed=NoTimestamp.UNSET, closing_words="", parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, Reopened(), source, assigned_to=assigned_to).item


def _unread_log_content(content: LogContent) -> str | dict[str, object]:
    if isinstance(content, LegacyFieldChange):
        return content.note
    return content if isinstance(content, str) else _log_content(content)


_WORKING_CHANGE = FieldChange("Updated", {"state": ItemState.WORKING})


def _start_working(item: ListItem, moment: datetime, source: str) -> ListItem:
    entry = ItemChange(moment, _WORKING_CHANGE, source)
    return replace(item, state=ItemState.WORKING, log=(*item.log, entry))


def _is_tell_move(log: tuple[ItemChange, ...], index: int) -> bool:
    """Identify the state change saved with the Tell feedback."""
    if log[index].what != _WORKING_CHANGE or index + 1 == len(log):
        return False
    feedback = log[index + 1]
    return (feedback.source == PAGE_SOURCE and feedback.time == log[index].time
            and isinstance(feedback.what, PageFeedback) and feedback.what.action == "tell")


def unread_update(item: ListItem) -> UnreadUpdate | NoUnreadUpdate:
    """Keep an update identity stable when page feedback adds log entries."""
    if not isinstance(item.unread_since, datetime):
        return NoUnreadUpdate.ABSENT
    changes = [(index, entry.time.isoformat(), _unread_log_content(entry.what), entry.source) for index, entry in enumerate(item.log)
               if not (_own_page_entry(entry) or _is_tell_move(item.log, index))]
    text = json.dumps((item.unread_since.isoformat(), changes), ensure_ascii=False)
    return UnreadUpdate(hashlib.sha256(text.encode()).hexdigest())


def _own_page_entry(entry: ItemChange) -> bool:
    """Find nate's own List page entries: a read, his words and the request receipts. They are not unread content."""
    return entry.source == PAGE_SOURCE and (isinstance(entry.what, MarkedRead) or entry.what == "Marked read"
                                            or _is_feedback(entry) or is_page_request_receipt(entry))


def _unread_changes(item: ListItem, *, include_park_receipts: bool = False) -> Generator[ItemChange]:
    """Give unread changes in reverse order. Exclude page feedback and its Tell state change.

    A read or a close ends the walk. A reopen is the last change that the walk gives.
    """
    if not isinstance(item.unread_since, datetime):
        return
    for index in range(len(item.log) - 1, -1, -1):
        change = item.log[index]
        content = change.what
        if (isinstance(content, MarkedRead) or content == "Marked read"
                or isinstance(content, FieldChange) and content.action == "Closed"):
            return
        park_receipt = include_park_receipts and isinstance(content, PageRequest) and content.action == "park"
        if park_receipt or not (_own_page_entry(change) or _is_tell_move(item.log, index)):
            yield change
        if isinstance(content, Reopened) or content == "Reopened":
            return


def unread_answer_ids(item: ListItem) -> frozenset[str]:
    """Find unread answers after accounting for parking actions."""
    read_times: list[datetime] = []
    answers: set[str] = set()
    for change in _unread_changes(item, include_park_receipts=True):
        content = change.what
        if isinstance(content, QuestionAnswered) and question_path.answer_is_unread(change.time, read_times):
            answers.add(content.question_id)
        elif (isinstance(content, FieldChange) and content.action == "Parked"
              and content.fields.get("parked_until", NoTimestamp.UNSET) is not NoTimestamp.UNSET
              or isinstance(content, PageRequest) and content.action == "park"):
            read_times.append(change.time)
    return frozenset(answers)


def mark_read(keeper: staff.StaffId, identity: str, *, source: str,
              update: UnreadUpdate | NoUnreadUpdate | ReadCurrentUpdate = ReadCurrentUpdate.CURRENT,
              page_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT) -> ListItem:
    return mark_read_edit(keeper, identity, source=source, update=update, page_request=page_request).item


def mark_read_edit(keeper: staff.StaffId, identity: str, *, source: str,
                   update: UnreadUpdate | NoUnreadUpdate | ReadCurrentUpdate,
                   page_request: PageRequest | NoPageRequest) -> ListEdit:
    """Return the result and the item read under the write lock.

    A List page Mark read on an item that waits for nate moves it to Working.
    The same write saves a notice that asks the item's producer for the next step.
    """
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if update is not ReadCurrentUpdate.CURRENT and unread_update(item) != update:
            return item
        read = replace(item, unread_since=NoTimestamp.UNSET)
        if not isinstance(page_request, PageRequest) or not _waits_for_nate(item, moment):
            return read
        return _start_working(read, moment, source)
    return _change(keeper, identity, edit, MarkedRead(), source, unread=False, page_action=page_request)


def add_ask(keeper: staff.StaffId, identity: str, *, kind: AskKind, target: staff.StaffId, what: str, why: str,
            config: bool, requester: staff.StaffId, source: str) -> Ask:
    """Open an ask for nate; the open ask makes the item unread."""
    _line(what, "--what")
    _line(why, "--why")
    _ = staff_id(target, "target")

    def edit(item: ListItem, moment: datetime) -> ListItem:
        _open(item)
        if requester not in (keeper, item.producer):
            raise ListError("only the item's producer or morty can open an ask")
        ask = Ask(f"A{len(item.asks) + 1}", kind, what, why, target, config, moment)
        return replace(item, asks=(*item.asks, ask))
    return _change(keeper, identity, edit, lambda changed: AskOpened(changed.asks[-1]), source).item.asks[-1]


def withdraw_ask(keeper: staff.StaffId, identity: str, ask_id: str, reason: str, *, caller: staff.StaffRow) -> None:
    """Close an open ask and record the reason under the item lock."""
    reason = reason.strip()
    _line(reason, "--reason")
    console_text.require(reason, "list field", refusal=ListError)

    def edit(item: ListItem, moment: datetime) -> ListItem:
        if caller.id not in (keeper, item.producer):
            raise ListError("only the item's producer or morty can withdraw an ask")
        _open(item)
        if not any(ask.ask_id == ask_id for ask in open_asks(item)):
            raise ListError(f"{identity} has no open ask {ask_id}")
        return replace(item, asks=tuple(replace(ask, closed=moment, withdrawn=True) if ask.ask_id == ask_id else ask
                                        for ask in item.asks))

    def event(changed: ListItem) -> AskWithdrawn:
        return AskWithdrawn(next(ask for ask in changed.asks if ask.ask_id == ask_id), reason)

    _ = _change(keeper, identity, edit, event, caller.name)


def give_go(keeper: staff.StaffId, identity: str, ask_id: str, go_id: str, *, source: str) -> ListEdit:
    """Close the ask with the go id; mark the item read when no answerable ask stays open.

    When the Go closes the last answerable ask of an item that waits for nate, the item moves to Working.
    The same write saves a notice that asks the item's producer for the next step, as Mark read does.
    """
    def edit(item: ListItem, moment: datetime) -> ListItem:
        _open(item)
        if not any(ask.ask_id == ask_id for ask in open_asks(item)):
            raise ListError("This ask is no longer open.")
        closed = replace(item, asks=tuple(replace(ask, closed=moment, go_id=go_id) if ask.ask_id == ask_id else ask
                                          for ask in item.asks))
        return replace(closed, state=ItemState.WORKING) if _waits_for_nate(closed, moment) else closed

    def event(changed: ListItem) -> GoGiven:
        return GoGiven(next(ask for ask in changed.asks if ask.ask_id == ask_id))

    def stays_unread(changed: ListItem) -> bool:
        # Another open ask or an unread change other than an ask or a Go keeps the item unread.
        return bool(answerable_asks(changed, staff.snapshot())) or any(
            not isinstance(change.what, (AskOpened, GoGiven)) for change in _unread_changes(changed))

    def working(item: ListItem, changed: ListItem, moment: datetime) -> tuple[ItemChange, ...]:
        if changed.state == item.state:
            return ()
        state = ItemChange(moment, FieldChange("Updated", {"state": ItemState.WORKING}), source)
        # As with Mark read, the read comes after the state change, so a later unread walk stops before it.
        read = () if stays_unread(changed) else (ItemChange(moment, MarkedRead(), PAGE_SOURCE),)
        notice = _next_step_notice(keeper, go_next_step_request(go_id), "nate gave the Go on", changed)
        return (state, *read, ItemChange(moment, notice, PAGE_SOURCE))
    before: list[ListItem] = []

    def save(item: ListItem, moment: datetime) -> ListItem:
        before.append(item)
        return edit(item, moment)
    try:
        return _change(keeper, identity, save, event, source, unread=stays_unread, follow=working)
    except AtomicDurabilityUnconfirmed:
        # The new content can be visible. Keep the Go when the saved ask holds this go id.
        try:
            published = load(keeper, identity)
        except (OSError, ValueError) as error:
            raise SavedReadFailed(f"the Go is saved; the item could not be read again: {error}") from error
        if not any(ask.ask_id == ask_id and ask.go_id == go_id and not ask.is_open for ask in published.asks):
            raise
        return SavedEdit(before[0] if before else published, published)


def log_go_not_delivered(keeper: staff.StaffId, identity: str, go_id: str, recipient: staff.StaffId) -> ListItem:
    """Add the log line that tells nate a Go message did not reach its recipient."""
    def edit(item: ListItem, _: datetime) -> ListItem:
        # The line changes no field. Clear the update time so that the save always differs; the save sets it again.
        return replace(item, updated=NoTimestamp.UNSET)

    def event(changed: ListItem) -> GoNotDelivered:
        ask = next((ask for ask in changed.asks if ask.go_id and ask.go_id == go_id), None)
        if ask is None:
            raise ListError("no closed ask has this go id")
        return GoNotDelivered(ask, recipient)
    return _change(keeper, identity, edit, event, "the List").item


def has_open_question(item: ListItem) -> bool:
    return any(question.answer is None for question in item.questions)


def answerable_asks(item: ListItem, members: staff.StaffSnapshot) -> tuple[Ask, ...]:
    """Find the open asks that nate can answer: the item is open and the ask's target has a staff row.

    The page shows only these asks, so only these asks keep an item waiting for nate.
    """
    if item.state == ItemState.CLOSED:
        return ()
    return tuple(ask for ask in open_asks(item) if members.by_id(ask.target) is not None)


def _waits_for_nate(item: ListItem, moment: datetime) -> bool:
    """Return True for an unparked item that waits for nate with no open question and no ask that nate can answer."""
    return (item.state == ItemState.WAITING_FOR_NATE and not has_open_question(item)
            and not answerable_asks(item, staff.snapshot()) and not parking_active(item.parked_until, moment))


_GO_NEXT_STEP = "-next-step"


def go_next_step_request(go_id: str) -> str:
    """Name the next-step notice of one Go. Its own name gives it a delivery key apart from the Go."""
    return go_id + _GO_NEXT_STEP


def next_step_go_id(request: str) -> str | None:
    """Find the go id of a Go's next-step notice, or None for each other request."""
    go_id = request.removesuffix(_GO_NEXT_STEP)
    return go_id if go_id != request else None


def _next_step_notice(keeper: staff.StaffId, request: str, done: str, item: ListItem, *, form_words: str = "") -> PageFeedback:
    """Ask the item's producer for the next step and save the producer as the recipient.

    `done` names what nate did, such as "nate read". A Mark read keeps its page words in `form_words`,
    so a retry matches the page request.
    """
    members = staff.snapshot()
    producer = members.by_id(item.producer)
    name = shlex.quote(producer.name if producer is not None else item.producer)
    keeper_option = "" if keeper in record_store.morty_ids(members) else f" --keeper {shlex.quote(keeper)}"
    words = (f"List page: {done} item {item.number}, so it is now Working. Write its next step: "
             + f"`{_LAUNCHER} update --id {item.id}{keeper_option} --source {name} --next-step \"<step>\"`.")
    return PageFeedback(request, words, action="read", form_words=form_words, recipient=item.producer)


def is_page_request_receipt(entry: ItemChange) -> bool:
    return entry.source == PAGE_SOURCE and (isinstance(entry.what, (PageRequest, QuestionRequestReceipt, UnparkNoChangeReceipt))
        or isinstance(entry.what, str) and entry.what.startswith(_LEGACY_RECEIPT))


def _is_feedback(entry: ItemChange) -> bool:
    return entry.source == PAGE_SOURCE and (isinstance(entry.what, PageFeedback)
        or isinstance(entry.what, str) and entry.what.startswith(_LEGACY_FEEDBACK))


def _validate_page_source(source: str) -> None:
    if source != PAGE_SOURCE:
        raise ListError("a List page request must use the List page source")


def _read_page_activity(fields: dict[str, object], source: str, *, receipt: bool) -> PageActivity:
    _validate_page_source(source)
    request = _request_id(fields.get("request"))
    words, form_words = fields.get("words"), fields.get("form_words", "")
    action = fields.get("action")
    if not isinstance(words, str) or not isinstance(form_words, str):
        raise ListError("invalid List request log")
    parking_time = fields.get("parked_until", "")
    if not isinstance(parking_time, str) or (parking_time and action != "park"):
        raise ListError("invalid List request parking time")
    parked_until = _request_parking(parking_value(parking_time))
    if "unpark_no_change" in fields and (not receipt or action != "unpark"):
        raise ListError("invalid Unpark receipt")
    if receipt:
        if action not in ("read", "remove", "park", "unpark", "question") or (action != "read" and not words.strip()):
            raise ListError("invalid List request receipt")
        received = PageRequest(request, action, words, form_words, parked_until)
        if action == "question":
            return QuestionRequestReceipt(received, _question_id(fields.get("question_id")))
        if "unpark_no_change" in fields:
            reason = fields["unpark_no_change"]
            if reason not in (UnparkNoChangeReason.EXPIRED, UnparkNoChangeReason.NO_ACTIVE_PARKING):
                raise ListError("invalid Unpark outcome")
            return UnparkNoChangeReceipt(received, UnparkNoChangeReason(reason))
        return received
    state, alerted_to, target = fields.get("state"), fields.get("alerted_to", ""), fields.get("read_update", "")
    if state not in ("pending", "sent", "alerted") or not isinstance(alerted_to, str) or not isinstance(target, str):
        raise ListError("invalid List feedback log")
    if action not in ("tell", "remove", "park", "unpark", "read") or not words.strip():
        raise ListError("invalid List feedback action")
    recipient = _field_text(fields.get("recipient", ""), "feedback recipient")
    return PageFeedback(request, words, state, alerted_to,
                        UnreadUpdate(target) if target else NoUnreadUpdate.ABSENT, action, form_words, parked_until,
                        recipient=staff_id(recipient, "feedback recipient") if recipient else LegacyFeedbackRecipient.KEEPER)


def _page_requests(item: ListItem) -> dict[str, PageActivity]:
    requests: dict[str, PageActivity] = {}
    for entry in item.log:
        content = entry.what
        if entry.source == PAGE_SOURCE and isinstance(content, (PageRequest, PageFeedback, QuestionRequestReceipt, UnparkNoChangeReceipt)):
            identity = (content.request.request if isinstance(content, (QuestionRequestReceipt, UnparkNoChangeReceipt))
                        else content.request)
            requests[identity] = content
    return requests


def feedback_requests(item: ListItem) -> dict[str, PageFeedback]:
    """Read the latest delivery state for each request from the item log."""
    return {request: saved for request, saved in _page_requests(item).items() if isinstance(saved, PageFeedback)}


def has_park_receipt(item: ListItem, request: str) -> bool:
    """Check whether this exact Park request saved."""
    saved = _page_requests(item).get(_request_id(request))
    return isinstance(saved, (PageRequest, PageFeedback)) and saved.action == "park"


def unpark_no_change_reason(item: ListItem, request: str) -> UnparkNoChangeReason:
    """Read the original reason from an Unpark receipt."""
    saved = _page_requests(item).get(_request_id(request))
    if isinstance(saved, UnparkNoChangeReceipt):
        return saved.reason
    if isinstance(saved, PageRequest) and saved.action == "unpark":
        # Older receipts do not record why the request made no change.
        return UnparkNoChangeReason.NO_ACTIVE_PARKING
    raise ListError("the List item has no Unpark receipt for this request")


def _previous_page_request(
        item: ListItem, request: PageRequest) -> PageActivity | NoPageRequest:
    previous = _page_requests(item).get(_request_id(request.request), NoPageRequest.ABSENT)
    if previous is not NoPageRequest.ABSENT:
        if isinstance(previous, PageFeedback) and previous.action == "read":
            # A Mark read notice keeps the page words in form_words.
            key = PageRequest(previous.request, "read", previous.form_words)
        elif isinstance(previous, PageFeedback):
            key = previous.key()
        elif isinstance(previous, (QuestionRequestReceipt, UnparkNoChangeReceipt)):
            key = previous.request
        else:
            key = previous
        if key.action == request.action == "park" and request.parked_until is not NoTimestamp.UNSET:
            target = key.parked_until
            if target is NoTimestamp.UNSET:
                recorded = [change.fields.get("parked_until", NoTimestamp.UNSET) for entry in item.log
                            if isinstance(change := entry.what, PageParkingChange) and change.request == key.request]
                if len(recorded) == 1:
                    target = _request_parking(recorded[0])
            if target != request.parked_until:
                raise ListError("this request already has a different parking time")
            if key.form_words != request.form_words:
                raise ListError("this request already has a different action or words")
        elif key != request:
            raise ListError("this request already has a different action or words")
    return previous


def _save_page_request(store: record_store.NumberedRecords, keeper: staff.StaffId,
                       item: ListItem, request: PageRequest) -> ListItem:
    try:
        _save(store, item)
    except AtomicDurabilityUnconfirmed:
        published = load(keeper, item.id)
        if _previous_page_request(published, request) is NoPageRequest.ABSENT:
            raise
        return published
    return item


class NoQuestionAnswerer(StrEnum):
    ABSENT = "absent"


class _NoActiveMorty(Enum):
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class _QuestionAnswerer:
    """The caller and the active morty ID, if available."""
    member: staff.StaffRow
    morty: staff.StaffId | _NoActiveMorty


@dataclass(frozen=True)
class _ListQuestionStore:
    keeper: staff.StaffId
    ask_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT
    answerer: _QuestionAnswerer | NoQuestionAnswerer = NoQuestionAnswerer.ABSENT

    def questions(self, item: str) -> tuple[question_record.Question, ...]:
        current = load(self.keeper, item)
        if isinstance(self.ask_request, PageRequest):
            # The shared allocator reserves IDs of resolved questions through its answer field.
            archived = tuple(replace(entry.what.question, answer=question_record.Answer(
                "Closed with the item.", entry.time.isoformat()))
                for entry in current.log if isinstance(entry.what, QuestionClosed))
            return (*current.questions, *archived)
        return current.questions

    def add(self, item: str, question: question_record.Question) -> None:
        # ask_question holds the List lock from the receipt check through this write.
        assert isinstance(self.ask_request, PageRequest)
        current = load(self.keeper, item)
        self.save_request(replace(current, questions=(*current.questions, question)), self.ask_request, question)

    def save_request(self, item: ListItem, request: PageRequest, question: question_record.Question) -> None:
        receipt = ItemChange(now(), QuestionRequestReceipt(request, question.question_id), PAGE_SOURCE)
        _ = _save_page_request(_store(self.keeper), self.keeper, replace(item, log=(*item.log, receipt)), request)

    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        store = _store(self.keeper)
        with store.locked():
            current = load(self.keeper, item)
            question = next((question for question in current.questions if question.question_id == question_id), None)
            if question is None or question.answer is not None:
                raise question_record.AnswerRefused("This item has no open question with that id.")
            answerer = self.answerer
            allowed = (self.keeper, question.recipient_id(self.keeper))
            if answerer is NoQuestionAnswerer.ABSENT or answerer.member.id not in (*allowed, answerer.morty):
                raise question_record.AnswerRefused("Only the question recipient or morty can answer.")
            console_text.require(answer.text, "list field", refusal=question_record.AnswerRefused)
            questions = tuple(replace(saved, answer=answer) if saved.question_id == question_id else saved
                              for saved in current.questions)
            moment = now()
            entry = ItemChange(moment, QuestionAnswered(question_id), answerer.member.name)
            changed = replace(current, questions=questions, updated=moment, unread_since=moment, log=(*current.log, entry))
            try:
                _save(store, changed)
            except AtomicDurabilityUnconfirmed:
                if load(self.keeper, item) != changed:
                    raise


def _new_question_recipient(keeper: staff.StaffId, producer: staff.StaffId
                            ) -> staff.StaffId | question_record.KeeperForUnavailableProducer:
    if producer == keeper:
        return keeper
    member = next((row for row in staff.active(staff.snapshot(), "producer") if row.id == producer), None)
    if member is not None and not isinstance(member.exited, staff.Exited):
        try:
            _ = session_pane.session_place(member.current_session)
        except (session_pane.NoSuchTarget, OSError, session_pane.HerdrFailure, ValueError):
            pass
        else:
            return producer
    return question_record.KeeperForUnavailableProducer(keeper, producer)


def _prepare_question_recipient(path: Path, request: PageRequest, keeper: staff.StaffId, producer: staff.StaffId
                                ) -> staff.StaffId | question_record.KeeperForUnavailableProducer:
    """Keep the first recipient if the List write fails."""
    fields = _request_fields(request)
    try:
        saved = delivery_state.read_fields(path)
    except FileNotFoundError:
        recipient = _new_question_recipient(keeper, producer)
        record = {"request": fields, **question_record.recipient_fields(recipient)}
        write_text_atomic(path, json.dumps(record) + "\n", new_file_mode=0o600)
        return recipient
    if saved.get("request") != fields:
        raise ListError("this request already has a different action or words")
    recipient = question_record.read_recipient(saved)
    if recipient is question_record.DefaultQuestionRecipient.PAGE:
        raise ListError("the prepared question has no recipient")
    return recipient


def ask_question(keeper: staff.StaffId, identity: str, request: PageRequest,
                 status: change_page.DeliveryStatus) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Save the question and its signed request receipt before delivery."""
    if request.action != "question" or not request.words.strip() or len(request.words) > 10000:
        raise question_record.AnswerRefused("Enter a question first.")
    request = replace(request, words=request.words.strip())
    store = _store(keeper)
    questions = _ListQuestionStore(keeper, request)
    with store.locked():
        item = load(keeper, identity)
        previous = _previous_page_request(item, request)
        if item.state == ItemState.CLOSED:
            if isinstance(previous, QuestionRequestReceipt):
                return change_page.NoMessageDelivery()
            raise question_record.AnswerRefused("This item is Closed.")
        open_question = next((question for question in item.questions
                              if question.answer is None and question.words == request.words), None)
        if isinstance(previous, QuestionRequestReceipt):
            if open_question is None or open_question.question_id != previous.question_id:
                return change_page.NoMessageDelivery()
        elif open_question is not None:
            questions.save_request(item, request, open_question)
        request_key = json.dumps([str(store.path(item.id).resolve()), _request_id(request.request)]).encode()
        preparation = status.root / (".review-link-list-ask-" + hashlib.sha256(request_key).hexdigest())
        recipient = (_prepare_question_recipient(preparation, request, keeper, item.producer) if open_question is None
                     else open_question.recipient_id(keeper))
        delivery = _claim_question(questions, item, request.words, recipient, status)
        with suppress(OSError):
            preparation.unlink()
        return delivery


def _claim_question(questions: _ListQuestionStore, item: ListItem, words: str,
                    recipient: staff.StaffId | question_record.KeeperForUnavailableProducer,
                    status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    recipient_id = recipient.keeper if isinstance(recipient, question_record.KeeperForUnavailableProducer) else recipient
    target = _list_question_target(questions.keeper, item, recipient_id)
    return page_question.ask(questions, replace(target, initial_recipient=recipient), words, status)


def _list_question_target(keeper: staff.StaffId, item: ListItem, recipient: staff.StaffId
                          ) -> change_page.QuestionDeliveryTarget:
    owner = None if isinstance(item.owner, UnassignedOwner) else item.owner
    return change_page.list_question_target(_store(keeper).path(item.id), item.id, item.number, item.title,
                                           recipient, keeper=keeper, alert_owner=owner)


def save_feedback(keeper: staff.StaffId, identity: str, feedback: PageFeedback) -> PageFeedback:
    """Save pending feedback and any Tell state change in one locked write."""
    if not feedback.words.strip() or feedback.state != "pending":
        raise ListError("Enter a new request with feedback words.")
    store = _store(keeper)
    with store.locked():
        item = load(keeper, identity)
        request = feedback.key()
        previous = _previous_page_request(item, request)
        if isinstance(previous, PageFeedback):
            return previous
        if isinstance(previous, PageRequest):
            raise ListError("this request has no feedback to deliver")
        moment = now()
        if feedback.action == "tell" and item.state in (ItemState.WAITING_FOR_NATE, ItemState.DELIVERED):
            item = _start_working(item, moment, PAGE_SOURCE)
        changed = replace(item, updated=moment, log=(*item.log, ItemChange(moment, feedback, PAGE_SOURCE)))
        _validate(changed)
        _ = _save_page_request(store, keeper, changed, request)
        return feedback


def deliver_feedback(keeper: staff.StaffId, identity: str, request: str,
                     send: Callable[[ListItem, PageFeedback], PageFeedback]) -> None:
    """Claim one request in this process and release the List lock before sending."""
    store = _store(keeper)
    claim = store.path(identity), request
    with feedback_claims_lock:
        if claim in feedback_claims:
            return
        feedback_claims.add(claim)
    try:
        with store.locked():
            item = load(keeper, identity)
            feedback = feedback_requests(item).get(request)
        if feedback is None or feedback.state != "pending":
            return
        result = send(item, feedback)
        if result.state == "pending":
            return
        with store.locked():
            current = load(keeper, identity)
            latest = feedback_requests(current).get(request)
            if latest is None or latest.state != "pending":
                return
            moment = now()
            changed = replace(current, updated=moment, log=(*current.log, ItemChange(moment, result, PAGE_SOURCE)))
            _save(store, changed)
    finally:
        with feedback_claims_lock:
            feedback_claims.remove(claim)


def feedback_notice_time(item: ListItem) -> Timestamp:
    """Keep the feedback notice until the next writer adds a log entry."""
    for entry in reversed(item.log):
        if entry.source != PAGE_SOURCE:
            break
        if isinstance(entry.what, PageFeedback) and entry.what.state == "pending":
            return entry.time
    return NoTimestamp.UNSET


def order(item: ListItem) -> tuple[bool, bool, float, int]:
    """Put user decisions first, unread updates next, and early checks before late checks."""
    return (item.state != ItemState.WAITING_FOR_NATE, not isinstance(item.unread_since, datetime),
            item.next_check.timestamp() if isinstance(item.next_check, datetime) else float("inf"), item.number)


def table(keeper: staff.StaffId, *, include_closed: bool = False, include_parked: bool = False) -> str:
    return _format_table(records(keeper, include_closed=include_closed), staff.snapshot(), include_parked=include_parked)


def _format_table(items: Sequence[ListItem], record: staff.StaffSnapshot, *, include_parked: bool,
                  show_latest_status: bool = False) -> str:
    moment = now()
    today = moment.astimezone(user_zone()) if show_latest_status else moment
    visible = [item for item in items if include_parked or not parking_active(item.parked_until, moment)]
    visible.sort(key=order)
    columns = ("#", "Owner", "Producer", "Item", "State", "Next step", "ETA", "Update")
    if show_latest_status:
        columns += ("Latest status",)
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for item in visible:
        owner = record.by_id(item.owner) if not isinstance(item.owner, UnassignedOwner) else None
        state = item.state.value
        if item.state != ItemState.CLOSED and isinstance(item.next_check, datetime):
            state += "; " + ("check overdue " if item.next_check < moment else "check ") + item.next_check.isoformat()
        if item.state != ItemState.CLOSED and isinstance(item.next_check, CheckCondition):
            state += "; check: " + item.next_check.text
        if item.parked_until is ParkUntilDelivered.DELIVERED:
            state += "; parked until delivered"
        elif isinstance(item.parked_until, datetime) and item.parked_until > moment:
            state += "; parked until " + parking_date(item.parked_until, user_zone())
        producer = record.by_id(item.producer)
        cells = (str(item.number), owner.name if owner is not None else item.owner,
                 producer.name if producer is not None else item.producer, item.title, state,
                 item.next_step, eta_text(item.eta), "●" if isinstance(item.unread_since, datetime) else "—")
        if show_latest_status:
            status = item.latest_status
            status_time = item.latest_status_time
            if status and isinstance(status_time, datetime):
                status += " · " + list_time(status_time, today)
            cells += (status,)
        lines.append("| " + " | ".join(" ".join(cell.split()).replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines)


def producer_id(members: staff.StaffSnapshot, reference: str) -> staff.StaffId:
    """Resolve a producer name or fixed staff id; refuse every other staff row."""
    matches = [row for row in staff.active(members, "producer") if reference in (row.id, row.name)]
    if len(matches) != 1:
        raise ListError("producer must name one producer staff row by name or staff id")
    return matches[0].id


def _check_cli_text_limits(changes: ItemChanges, add_evidence: str | None) -> None:
    lengths: list[tuple[str, str, int]] = []
    for field, value in (("latest_status", changes.get("latest_status")), ("next_step", changes.get("next_step"))):
        if value is not None:
            lengths.append((f"max_chars.{field}", "--" + field.replace("_", "-"), len(value)))
    if "evidence" in changes:
        longest = max(map(len, changes["evidence"].splitlines()), default=0)
        lengths.append(("max_chars.evidence_line", "--evidence", longest))
    if add_evidence is not None:
        lengths.append(("max_chars.evidence_line", "--add-evidence", len(add_evidence)))
    if not lengths:
        return
    settings = conf.read("the_list")
    for key, option, length in lengths:
        limit = settings.origin(key, conf.positive_whole)
        if not isinstance(limit, conf.UsableLiveValue):
            raise ListError(f"{option} needs a valid {key} in {settings.source}")
        if length > limit.value:
            unit = "characters per line" if key == "max_chars.evidence_line" else "characters"
            raise ListError(f"{option} limit is {limit.value} {unit}. write it for nate: two short sentences")


def _cli_changes(args: argparse.Namespace, members: staff.StaffSnapshot, add_evidence: str | None) -> ItemChanges:
    options = cast(dict[str, object], vars(args))
    changes: ItemChanges = {}
    text_fields: tuple[Literal["title", "unit", "next_step", "latest_status", "evidence", "done_when"], ...] = (
        "title", "unit", "next_step", "latest_status", "evidence", "done_when")
    for key in text_fields:
        value = options.get(key)
        if isinstance(value, str):
            changes[key] = value
    owner_id = options.get("owner")
    if isinstance(owner_id, str):
        if owner_id in UnassignedOwner:
            changes["owner"] = UnassignedOwner(owner_id)
        else:
            owner = members.by_id(staff.StaffId(owner_id))
            if owner is None or owner.kind != "showrunner":
                raise ListError("owner must name a showrunner staff id")
            changes["owner"] = owner.id
    producer = options.get("producer")
    if isinstance(producer, str):
        changes["producer"] = producer_id(members, producer)
    state, next_check, eta, requested = (options.get(key) for key in ("state", "next_check", "eta", "requested_by"))
    if isinstance(state, str):
        changes["state"] = ItemState(state)
    if isinstance(next_check, str):
        changes["next_check"] = next_check_value(next_check)
    if isinstance(eta, str):
        changes["eta"] = _cli_eta(eta)
    if isinstance(requested, str):
        changes["requested_by"] = RequestedBy(requested)
    blocked_by, related_to = options.get("blocked_by"), options.get("related_to")
    if isinstance(blocked_by, list):
        dependencies: list[ItemDependency] = []
        for text in cast(list[str], blocked_by):
            number, condition = text.split(":", 1)
            dependencies.append(ItemDependency(int(number.removeprefix("L").removeprefix("#")), condition))
        changes["blocked_by"] = tuple(dependencies)
    if isinstance(related_to, list):
        changes["related_to"] = tuple(int(text.removeprefix("L").removeprefix("#")) for text in cast(list[str], related_to))
    _check_cli_text_limits(changes, add_evidence)
    descriptions = (value.text for value in (changes.get("eta"), changes.get("next_check"))
                    if isinstance(value, (EtaDescription, CheckCondition)))
    console_text.require((changes.get("title", ""), changes.get("next_step", ""), changes.get("latest_status", ""),
                          changes.get("evidence", ""), changes.get("done_when", ""), add_evidence or "",
                          *descriptions, *(dependency.condition for dependency in changes.get("blocked_by", ()))),
                         "list field", refusal=ListError)
    return changes


def _send_assignment_notice(recipient: staff.StaffRow, text: str) -> None:
    command = session_send.send_argv("--to", recipient.id, "--recipient-kind", recipient.kind,
                                     "--from", "morty", "--text", text)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if send_result.delivered_or_kept(result.returncode, result.stdout, accept_skipped=True):
        return
    raise ListError((result.stderr or result.stdout).strip() or "message delivery failed")


def _notify_producer_assignment(item: ListItem, previous_producer: staff.StaffId, keeper: staff.StaffId, *,
                                caller: staff.StaffId) -> int:
    if item.producer in (keeper, previous_producer):
        return 0
    notify_producer = item.producer != caller
    try:
        members = staff.snapshot()
    except (ValueError, staff.StaffRefused, OSError) as error:
        unsent = "The producer and showrunner notices were" if notify_producer else "The showrunner notice was"
        raise ListError(f"{item.id} was saved. {unsent} not sent. {error}") from error
    producer = next((row for row in staff.active(members, "producer") if row.id == item.producer), None)
    owner = next((row for row in staff.active(members, "showrunner") if row.id == item.owner), None)
    showrunner = f"Its showrunner is {owner.name}." if owner is not None else "The item has no assigned showrunner."
    item_text = f"item {item.number}: {item.title}"
    update_options = ", ".join(f"`{option}`" for option in _PRODUCER_UPDATE_OPTIONS)
    notices = (
        ("producer", item.producer, producer, f"You are the producer of {item_text}. {showrunner}\n"
         + f"Run `{_LAUNCHER} list --all --latest-status` to read it. "
         + f"Run `{_LAUNCHER} update --id {item.id}` to update it. Use any of {update_options}."),
        ("showrunner", item.owner, owner, f"The saved assignment for {item_text} needs a new producer."
         if producer is None else f"{producer.name} is the producer of {item_text}. "
         + f"Send design questions on this item to {producer.name}."),
    )
    status = 0
    for role, reference, recipient, message in notices:
        if role == "producer" and not notify_producer:
            continue
        try:
            if recipient is None:
                if role == "producer":
                    raise ListError("The producer has no current producer staff row.")
                if isinstance(reference, UnassignedOwner):
                    raise ListError("the item has no assigned showrunner")
                raise ListError("the owner has no current showrunner staff row")
            _send_assignment_notice(recipient, message)
        except (ListError, OSError, subprocess.SubprocessError, UnicodeError) as error:
            details = " ".join(str(error).splitlines())
            print(f"the_list: {item.id} was saved. The {role} notice to {reference} failed. {details}", file=sys.stderr)
            status = 1
    return status


_KEEPER_HELP = "the staff ID of the record keeper"


class _KeeperRouting(StrEnum):
    ANSWER = "answer"
    UPDATE = "update"


def _record_keeper(morty: staff.StaffId | _NoActiveMorty, caller: staff.StaffId, identity: str,
                   chosen: staff.StaffId | None, *, policy: _KeeperRouting) -> staff.StaffId:
    """Select the folder for an answer or an update.

    The locked write checks the caller against the saved record.
    """
    if chosen is not None:
        if not _store(chosen).folder().is_dir():
            raise ListError(f"no List folder for keeper {chosen}")
        return chosen
    if policy is _KeeperRouting.ANSWER:
        own = _store(caller)
        if own.valid_id(identity) and os.path.lexists(own.path(identity)):
            return caller
    if morty is _NoActiveMorty.UNAVAILABLE:
        raise ListError("default List routing requires one active morty staff row")
    return morty


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    answer = commands.add_parser("answer", help="save an answer to a List question")
    _ = answer.add_argument("id", help="the List item ID, such as L1")
    _ = answer.add_argument("--question", required=True, help="the question ID, such as Q1")
    _ = answer.add_argument("--text", required=True, help="the answer to show on The List")
    _ = answer.add_argument("--keeper", type=staff.StaffId,
                            help=_KEEPER_HELP + " (defaults to your folder if it holds the item, else morty's)")
    ask = commands.add_parser("ask", help="ask nate for a Go or an approval on the List page")
    _ = ask.add_argument("id", help="the List item ID, such as L1")
    _ = ask.add_argument("--kind", required=True, choices=[kind.value for kind in AskKind])
    _ = ask.add_argument("--target", required=True, help="the staff ID of the session that needs the Go")
    _ = ask.add_argument("--what", required=True, help="the one change that nate approves")
    _ = ask.add_argument("--why", required=True, help="why the session needs nate's word")
    _ = ask.add_argument("--config", action="store_true", help="the change is a settings.json, CLAUDE.md or config change")
    withdrawal = commands.add_parser("withdraw-ask", help="withdraw an open List ask")
    _ = withdrawal.add_argument("id", help="the List item ID, such as L1")
    _ = withdrawal.add_argument("--ask", required=True, help="the ask ID, such as A1")
    _ = withdrawal.add_argument("--reason", required=True, help="why the ask is no longer needed")
    for name in ("add", "update", "park", "unpark", "close", "reopen", "mark-read", "clean-evidence", "link", "unlink", "list"):
        command = commands.add_parser(name)
        if name != "list":
            _ = command.add_argument("--source", required=name != "update")
        if name in ("link", "unlink"):
            _ = command.add_argument("id")
            _ = command.add_argument("--review", dest="reviews", action="append", type=ReviewByShownId, default=[])
            _ = command.add_argument("--review-key", dest="reviews", action="append", type=ReviewByKey, default=[])
        elif name not in ("add", "list", "clean-evidence"):
            _ = command.add_argument("--id", required=True)
        if name == "update":
            _ = command.add_argument("--keeper", type=staff.StaffId, help=_KEEPER_HELP + " (defaults to morty's)")
        if name in ("add", "update"):
            _ = command.add_argument("--title", required=name == "add")
            _ = command.add_argument("--owner", required=name == "add")
            _ = command.add_argument("--producer")
            for key in ("unit", "next-check", "eta", "next-step", "latest-status", "evidence", "add-evidence", "done-when"):
                _ = command.add_argument("--" + key)
            _ = command.add_argument("--state", choices=[state.value for state in ItemState if state != ItemState.CLOSED])
            _ = command.add_argument("--requested-by", choices=list(RequestedBy))
            _ = command.add_argument("--blocked-by", nargs="*")
            _ = command.add_argument("--related-to", nargs="*")
        if name == "add":
            _ = command.add_argument("--number", type=int)
        elif name == "park":
            _ = command.add_argument("--until", required=True, help="a date, a time with a UTC offset, or delivered")
        elif name == "close":
            _ = command.add_argument("--words", required=True)
        elif name == "list":
            _ = command.add_argument("--all", action="store_true")
            _ = command.add_argument("--include-parked", action="store_true")
            _ = command.add_argument("--latest-status", action="store_true", help="show the latest status column")
    args = parser.parse_args(argv)
    name = cast(str, args.command)
    if name in ("link", "unlink") and not cast(list[ReviewByShownId | ReviewByKey], args.reviews):
        parser.error("use --review or --review-key")
    try:
        source = cast(str | None, getattr(args, "source", None))
        if source is not None and source_without_clock(source) != source:
            raise ListError("Omit the clock time from --source because the tool adds the time.")
        add_evidence = cast(str | None, getattr(args, "add_evidence", None))
        if add_evidence is not None:
            _line(add_evidence, "evidence to add")
        record = staff.snapshot()
        session = staff.runtime_id() if name == "withdraw-ask" else staff.caller_identity()
        caller = record.by_current_session(session) if session or name != "withdraw-ask" else None
        if caller is None:
            raise ListError("the current session has no staff row")
        if caller.kind != "producer":
            raise ListError("the current session must be a producer")
        if name == "answer":
            identity, question = cast(str, args.id), cast(str, args.question)
            try:
                answer_morty = producer_id(record, "morty")
            except ListError:
                answer_morty = _NoActiveMorty.UNAVAILABLE
            keeper = _record_keeper(answer_morty, caller.id, identity, cast(staff.StaffId | None, args.keeper),
                                    policy=_KeeperRouting.ANSWER)
            answerer = _QuestionAnswerer(caller, answer_morty)
            result = page_question.answer(_ListQuestionStore(keeper, answerer=answerer), identity, question,
                                          cast(str, args.text))
            if isinstance(result, page_question.AnswerRefused):
                raise ListError(result.reason)
            print(f"answered {identity} {question}")
            return 0
        morty = producer_id(record, "morty")
        keeper = morty
        if name in ("ask", "withdraw-ask"):
            identity = cast(str, args.id)
            identity = f"L{identity}" if identity.isascii() and identity.isdecimal() else identity
            if name == "withdraw-ask":
                ask_id = cast(str, args.ask)
                withdraw_ask(keeper, identity, ask_id, cast(str, args.reason), caller=caller)
                print(f"withdrew {identity} {ask_id}")
                return 0
            target = staff.StaffId(cast(str, args.target))
            if record.by_id(target) is None:
                raise ListError("--target must name a staff id")
            what, why = cast(str, args.what), cast(str, args.why)
            console_text.require((what, why), "list field", refusal=ListError)
            opened = add_ask(keeper, identity, kind=AskKind(cast(str, args.kind)), target=target,
                             what=what, why=why, config=cast(bool, args.config),
                             requester=caller.id, source=caller.name)
            print(f"{identity} {opened.ask_id}")
            return 0
        # Only morty updates every field of every item, in each keeper's folder.
        assigned_to = AllItems.INCLUDED if caller.id == morty else caller.id
        if name == "list":
            include_closed = cast(bool, args.all)
            if assigned_to is AllItems.INCLUDED:
                items = records(keeper, include_closed=include_closed)
                incomplete = False
            else:
                read = _producer_records(keeper, assigned_to, include_closed=include_closed)
                items = read.items
                incomplete = read.incomplete
            print(_format_table(items, record, include_parked=include_closed or cast(bool, args.include_parked),
                                show_latest_status=cast(bool, args.latest_status)))
            if incomplete:
                raise ListError("The List results are incomplete. Could not read every record.")
            return 0
        if name == "update":
            identity = cast(str, args.id)
            if identity.isascii() and identity.isdecimal():
                identity = f"L{identity}"
            keeper = _record_keeper(morty, caller.id, identity, cast(staff.StaffId | None, args.keeper),
                                    policy=_KeeperRouting.UPDATE)
            reassigns = cast(str | None, args.producer) is not None or cast(str | None, args.owner) is not None
            # A producer gets the field refusal below, because it never changes these fields.
            if assigned_to is AllItems.INCLUDED and keeper != morty and reassigns:
                raise ListError("move the item to morty's List before you change its producer or owner")
            if assigned_to is not AllItems.INCLUDED:
                options = cast(dict[str, object], vars(args))
                _check_producer_update_fields(key for key, value in options.items()
                                              if value is not None and key not in {"command", "id", "keeper", "source"})
                _ = _update(keeper, identity, _cli_changes(args, record, add_evidence), source=source or "",
                            add_evidence=add_evidence or "", assigned_to=caller)
                return 0
            if source is None:
                parser.error("update requires --source for morty")
            changed = _update(keeper, identity, _cli_changes(args, record, add_evidence), source=source,
                              add_evidence=add_evidence or "")
            if isinstance(changed, UnchangedEdit):
                return 0
            return _notify_producer_assignment(changed.item, changed.before.producer, keeper, caller=caller.id)
        if assigned_to is not AllItems.INCLUDED and name not in ("add", "park", "unpark", "close", "reopen"):
            raise ListError(f"only morty can run {name}")
        assert source is not None
        if name == "clean-evidence":
            changed, unchanged = clean_evidence(keeper, source=source)
            print(f"{changed} changed; {unchanged} unchanged")
        elif name in ("link", "unlink"):
            identity = cast(str, args.id)
            selections = cast(list[ReviewByShownId | ReviewByKey], args.reviews)
            current = load(keeper, identity)
            _open(current)
            from scripts.review_links import review_items, review_link

            needs_read = any(isinstance(selection, ReviewByShownId)
                             or (name == "link" and selection.value not in current.reviews) for selection in selections)
            known = ({item.key: item for item in review_items.read(review_link.review_root(), members=record)}
                     if needs_read else {})
            resolved: list[str] = []
            for selection in selections:
                if isinstance(selection, ReviewByKey):
                    if name == "link" and selection.value not in current.reviews and selection.value not in known:
                        raise ListError("unknown review key: " + selection.value)
                    resolved.append(selection.value)
                    continue
                matches = [item for item in known.values() if item.id == selection.value]
                if len(matches) > 1:
                    choices = ", ".join(f"{item.key} ({item.type.value})" for item in matches)
                    raise ListError(f"ambiguous review id: {selection.value}; {choices}")
                if not matches and name == "link":
                    raise ListError("unknown review id: " + selection.value)
                resolved.extend(item.key for item in matches)
            edit_reviews = link if name == "link" else unlink
            _ = edit_reviews(keeper, identity, resolved, source=source)
        elif name == "add":
            changes = _cli_changes(args, record, add_evidence)
            number = cast(dict[str, object], vars(args)).get("number")
            if assigned_to is not AllItems.INCLUDED:
                if changes.get("producer", caller.id) != caller.id:
                    raise ListError(f"To add an item, use --producer {caller.name} or your staff ID.")
                if number is not None:
                    raise ListError("only morty can choose an item number")
                changes["producer"] = caller.id
                source = caller.name
            if add_evidence is not None:
                changes["evidence"] = _append_evidence(changes.get("evidence", ""), add_evidence)
            owner = changes.get("owner")
            if owner is None:
                raise ListError("owner must name a showrunner staff id")
            item = add(keeper, cast(str, args.title), owner=owner, source=source,
                       number=number if isinstance(number, int) else NextNumber.AVAILABLE, changes=changes,
                       assigned_to=assigned_to)
            print(item.id)
            return _notify_producer_assignment(item, keeper, keeper, caller=caller.id)
        elif name == "park":
            _ = park(keeper, cast(str, args.id), parking_value(cast(str, args.until)), source=source,
                     assigned_to=assigned_to)
        elif name == "unpark":
            _ = unpark(keeper, cast(str, args.id), source=source, assigned_to=assigned_to)
        elif name == "close":
            words = cast(str, args.words)
            console_text.require(words, "list field", refusal=ListError)
            _ = close(keeper, cast(str, args.id), words, source=source, assigned_to=assigned_to)
        elif name == "reopen":
            _ = reopen(keeper, cast(str, args.id), source=source, assigned_to=assigned_to)
        else:
            _ = mark_read(keeper, cast(str, args.id), source=source)
        return 0
    except (ValueError, staff.StaffRefused, OSError, UnicodeError) as error:
        print(f"the_list: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
