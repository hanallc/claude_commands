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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed
from scripts.lib.user_zone import user_zone, zone_label
from scripts.message import send_result, session_send
from scripts.production import delivery_state, record_store, staff
from scripts.review_links import change_page, page_question, question_record


class ListError(ValueError):
    """The item cannot accept the requested operation."""


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


class ParkUntilDelivered(StrEnum):
    DELIVERED = "delivered"


Parking = Timestamp | ParkUntilDelivered


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


class KeeperRecipient(StrEnum):
    """Mark a notice that goes to the List keeper."""

    KEEPER = ""


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
    recipient: staff.StaffId | KeeperRecipient = KeeperRecipient.KEEPER

    def key(self) -> PageRequest:
        return PageRequest(self.request, self.action, self.words, self.form_words, self.parked_until)


@dataclass(frozen=True)
class PageItemNotice:
    """Save an item action and its notice in the same write."""

    request: str
    words: str
    action: Literal["remove", "park", "unpark"]
    form_words: str = ""
    parked_until: Parking = NoTimestamp.UNSET


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


PageActivity = PageFeedback | PageRequest | QuestionRequestReceipt | UnparkNoChangeReceipt
ListEvent = (FieldChange | QuestionAnswered | QuestionClosed | MarkedRead | Reopened | Unparked
             | ReviewLinksChanged | EvidenceCopyRemoved | PageActivity)
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


def parking_value(text: str) -> Parking:
    return ParkUntilDelivered.DELIVERED if text == ParkUntilDelivered.DELIVERED else time_value(text)


def parking_active(value: Parking, moment: datetime) -> bool:
    return value is ParkUntilDelivered.DELIVERED or isinstance(value, datetime) and value > moment


def _parking_text(value: Parking) -> str:
    return value.value if isinstance(value, ParkUntilDelivered) else _time_text(value)


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


def _staff_id(value: str, name: str) -> None:
    if not value or Path(value).name != value or value in (".", ".."):
        raise ListError(f"invalid {name} staff id")


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


def _validate(item: ListItem) -> None:
    if item.number < 1:
        raise ListError("number must be positive")
    _line(item.title, "title")
    _staff_id(item.owner, "owner")
    _staff_id(item.producer, "producer")
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
    if "eta" in fields:
        eta = fields["eta"]
        values["eta"] = {"value": eta_text(eta), "kind": _eta_kind(eta)}
    if "next_check" in fields:
        values["next_check"] = next_check_text(fields["next_check"])
    if "parked_until" in fields:
        values["parked_until"] = _parking_text(fields["parked_until"])
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
                    _staff_id(text, field)
                    values[field] = UnassignedOwner(text) if text in UnassignedOwner else staff.StaffId(text)
                case "producer":
                    _staff_id(text, field)
                    values[field] = staff.StaffId(text)
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
        fields["parked_until"] = _parking_text(request.parked_until)
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
    if isinstance(content, PageFeedback):
        fields = _request_fields(content.key())
        fields.update(state=content.state, alerted_to=content.alerted_to)
        if isinstance(content.read_update, UnreadUpdate):
            fields["read_update"] = content.read_update.fingerprint
        if content.recipient is not KeeperRecipient.KEEPER:
            fields["recipient"] = content.recipient
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
                        delivery_state.read_list_questions(fields.get("questions", "[]")))
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
            raise ListError(f"{producer.name}: retired producer. Choose an active producer.")


def _save(store: record_store.NumberedRecords, item: ListItem) -> None:
    _validate(item)
    fields = {"id": item.id, "number": str(item.number), "title": item.title,
              "owner": item.owner, "producer": item.producer, "unit": item.unit, "state": item.state.value,
              "next_check": next_check_text(item.next_check), "eta": eta_text(item.eta),
              "eta_kind": _eta_kind(item.eta),
              "unread_since": _time_text(item.unread_since), "parked_until": _parking_text(item.parked_until),
              "blocked_by": json.dumps([(entry.number, entry.condition) for entry in item.blocked_by], ensure_ascii=False),
              "related_to": json.dumps(item.related_to), "reviews": json.dumps(item.reviews),
              "requested_by": item.requested_by.value,
              "opened": _time_text(item.opened), "updated": _time_text(item.updated),
              "closed": _time_text(item.closed), "closing_words": item.closing_words}
    if item.questions:
        fields["questions"] = json.dumps([question_record.stored(question) for question in item.questions], ensure_ascii=False)
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
            assigned = fields.get("producer", keeper)
            _staff_id(assigned, "producer")
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
    target = _list_question_target(keeper, item)
    waiting = True
    failed: set[str] = set()
    for question in unanswered:
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
        moment = now()
        defaults = _CREATION_DEFAULTS.copy()
        defaults["requested_by"] = requested_by
        item = ListItem(chosen, title, owner, keeper, unread_since=moment, opened=moment, updated=moment,
                        closed=NoTimestamp.UNSET, log=(), **defaults)
        if changes:
            item = replace(item, **changes)
        initial: LogFields = {"title": item.title, "owner": item.owner, "producer": item.producer,
                                "state": item.state, "eta": item.eta,
                                "next_step": item.next_step, "latest_status": item.latest_status,
                                "requested_by": item.requested_by, **changes}
        item = replace(item, log=(ItemChange(moment, FieldChange("Added", initial), source),))
        _validate(item)
        _check_links(read_reference, item.number, changes)
        _require_active_open_producers((item,))
        store.reserve_number(chosen)
        _save(store, item)
        return item


def _change(keeper: staff.StaffId, identity: str, edit: Callable[[ListItem, datetime], ListItem],
            what: ListEvent | Callable[[ListItem], ListEvent], source: str, *, unread: bool = True,
            page_action: PageItemNotice | PageRequest | NoPageRequest = NoPageRequest.ABSENT,
            assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListEdit:
    store = _store(keeper)
    _ = store.path(identity)
    with store.locked():
        if assigned_to is AllItems.INCLUDED:
            item = load(keeper, identity)
        else:
            fields, body = store.read(identity)
            if fields.get("producer", keeper) != assigned_to:
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
            changed = replace(changed, updated=moment, unread_since=moment if unread else NoTimestamp.UNSET)
            if (isinstance(event, FieldChange) and event.action == "Updated"
                    and event.fields.get("state") == ItemState.DELIVERED
                    and changed.parked_until is ParkUntilDelivered.DELIVERED):
                changed = replace(changed, parked_until=NoTimestamp.UNSET)
                entries.append(ItemChange(moment, UnparkedForDelivery(ItemDelivered.RECORDED), source))
        if isinstance(request, PageRequest):
            if entries and isinstance(page_action, PageItemNotice):
                pending = PageFeedback(page_action.request, page_action.words, action=page_action.action,
                                       form_words=request.form_words, parked_until=request.parked_until)
                entries.append(ItemChange(moment, pending, PAGE_SOURCE))
            elif request.action == "read" and changed.state != item.state:
                entries.append(ItemChange(moment, _next_step_notice(request, changed), PAGE_SOURCE))
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


def make_top_priority(keeper: staff.StaffId, identity: str, *,
                      assigned_to: staff.StaffId | AllItems = AllItems.INCLUDED) -> ListItem:
    """Make an open item the top priority item of its producer. A producer names only its own items."""
    item = load(keeper, identity)
    if item.state == ItemState.CLOSED:
        raise ListError(f"{item.id} is Closed. Only an open item can be the top priority item.")
    if assigned_to is not AllItems.INCLUDED and item.producer != assigned_to:
        raise ListError(f"{item.id} is assigned to another producer")
    try:
        _ = staff.set_producer_top_item(item.producer, staff.TopItem(item.number))
    except (staff.StaffRefused, staff.StaffNotFound) as error:
        raise ListError(f"{item.id}: {error}") from error
    return item


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
           notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT) -> ListEdit:
    """Clear an active parking time and save its notice in the same write."""
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if not parking_active(item.parked_until, moment):
            return item
        _open(item)
        return replace(item, parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, Unparked(), source, page_action=notice)


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
                       questions=answered, log=(*item.log, *closed_questions))
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


def unread_update(item: ListItem) -> UnreadUpdate | NoUnreadUpdate:
    """Keep an update identity stable when page feedback adds log entries."""
    if not isinstance(item.unread_since, datetime):
        return NoUnreadUpdate.ABSENT
    changes = [(index, entry.time.isoformat(), _unread_log_content(entry.what), entry.source) for index, entry in enumerate(item.log)
               if not (entry.source == PAGE_SOURCE and
                       (isinstance(entry.what, MarkedRead) or entry.what == "Marked read"
                        or _is_feedback(entry) or is_page_request_receipt(entry)))]
    text = json.dumps((item.unread_since.isoformat(), changes), ensure_ascii=False)
    return UnreadUpdate(hashlib.sha256(text.encode()).hexdigest())


def unread_answer_ids(item: ListItem) -> frozenset[str]:
    """Find answers since the last action that cleared the item's unread state."""
    if not isinstance(item.unread_since, datetime):
        return frozenset()
    unread: set[str] = set()
    for change in reversed(item.log):
        content = change.what
        if (isinstance(content, (MarkedRead, Reopened)) or content in ("Marked read", "Reopened")
                or isinstance(content, FieldChange) and content.action == "Closed"):
            break
        if isinstance(content, QuestionAnswered):
            unread.add(content.question_id)
    return frozenset(unread)


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
        working = ItemChange(moment, FieldChange("Updated", {"state": ItemState.WORKING}), source)
        return replace(read, state=ItemState.WORKING, log=(*item.log, working))
    return _change(keeper, identity, edit, MarkedRead(), source, unread=False, page_action=page_request)


def has_open_question(item: ListItem) -> bool:
    return any(question.answer is None for question in item.questions)


def _waits_for_nate(item: ListItem, moment: datetime) -> bool:
    """Return True for an unparked item that waits for nate with no open question."""
    # Item 232 adds open asks; an item with an open ask must also return False here.
    return (item.state == ItemState.WAITING_FOR_NATE and not has_open_question(item)
            and not parking_active(item.parked_until, moment))


def _next_step_notice(request: PageRequest, item: ListItem) -> PageFeedback:
    """Ask the item's producer for the next step and save the producer as the recipient."""
    producer = staff.snapshot().by_id(item.producer)
    name = shlex.quote(producer.name if producer is not None else item.producer)
    words = (f"List page: nate read item {item.number}, so it is now Working. Write its next step: "
             + f"`{_LAUNCHER} update --id {item.id} --source {name} --next-step \"<step>\"`.")
    # The page words stay in form_words, so a retry matches the page request.
    return PageFeedback(request.request, words, action="read", form_words=request.words, recipient=item.producer)


def notice_recipient(keeper: staff.StaffId, feedback: PageFeedback) -> staff.StaffId:
    """Return the saved recipient, or the keeper when the notice saved none."""
    return keeper if feedback.recipient is KeeperRecipient.KEEPER else feedback.recipient


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
    recipient = fields.get("recipient", "")
    if (state not in ("pending", "sent", "alerted") or not isinstance(alerted_to, str) or not isinstance(target, str)
            or not isinstance(recipient, str)):
        raise ListError("invalid List feedback log")
    if action not in ("tell", "remove", "park", "unpark", "read") or not words.strip():
        raise ListError("invalid List feedback action")
    return PageFeedback(request, words, state, alerted_to,
                        UnreadUpdate(target) if target else NoUnreadUpdate.ABSENT, action, form_words, parked_until,
                        staff.StaffId(recipient) if recipient else KeeperRecipient.KEEPER)


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


@dataclass(frozen=True)
class _ListQuestionStore:
    keeper: staff.StaffId
    source: str
    ask_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT

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
        receipt = ItemChange(now(), QuestionRequestReceipt(request, question.question_id), self.source)
        _ = _save_page_request(_store(self.keeper), self.keeper, replace(item, log=(*item.log, receipt)), request)

    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        store = _store(self.keeper)
        with store.locked():
            current = load(self.keeper, item)
            question = next((question for question in current.questions if question.question_id == question_id), None)
            if question is None or question.answer is not None:
                raise question_record.AnswerRefused("This item has no open question with that id.")
            questions = tuple(replace(question, answer=answer) if question.question_id == question_id else question
                              for question in current.questions)
            moment = now()
            entry = ItemChange(moment, QuestionAnswered(question_id), self.source)
            changed = replace(current, questions=questions, updated=moment, unread_since=moment, log=(*current.log, entry))
            try:
                _save(store, changed)
            except AtomicDurabilityUnconfirmed:
                if load(self.keeper, item) != changed:
                    raise


def ask_question(keeper: staff.StaffId, identity: str, request: PageRequest,
                 status: change_page.DeliveryStatus) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Save the question and its signed request receipt before delivery."""
    if request.action != "question" or not request.words.strip() or len(request.words) > 10000:
        raise question_record.AnswerRefused("Enter a question first.")
    request = replace(request, words=request.words.strip())
    store = _store(keeper)
    questions = _ListQuestionStore(keeper, PAGE_SOURCE, request)
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
        return _claim_question(questions, item, request.words, status)


def _claim_question(questions: _ListQuestionStore, item: ListItem, words: str,
                    status: change_page.DeliveryStatus) -> change_page.MessageDelivery:
    return page_question.ask(questions, _list_question_target(questions.keeper, item), words, status)


def _list_question_target(keeper: staff.StaffId, item: ListItem) -> change_page.QuestionDeliveryTarget:
    owner = None if isinstance(item.owner, UnassignedOwner) else item.owner
    return change_page.list_question_target(_store(keeper).path(item.id), item.id, item.number, item.title,
                                           keeper, alert_owner=owner)


def save_feedback(keeper: staff.StaffId, identity: str, feedback: PageFeedback) -> PageFeedback:
    """Save the request identity, words, and pending state in one locked write."""
    if not feedback.words.strip() or feedback.state != "pending":
        raise ListError("enter a new request and words for morty")
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


def sent_notice_time(item: ListItem) -> Timestamp:
    """Keep the sent notice until the next writer adds a log entry."""
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


def _cli_changes(args: argparse.Namespace, members: staff.StaffSnapshot) -> ItemChanges:
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
    return changes


def _cli_item_id(text: str) -> str:
    """Accept an item number with or without its L prefix."""
    return f"L{text}" if text.isascii() and text.isdecimal() else text


def _send_assignment_notice(recipient: staff.StaffRow, text: str) -> None:
    command = session_send.send_argv("--to", recipient.id, "--recipient-kind", recipient.kind,
                                     "--from", "morty", "--text", text)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if send_result.delivered_or_kept(result.returncode, result.stdout, accept_skipped=True):
        return
    raise ListError((result.stderr or result.stdout).strip() or "message delivery failed")


def _notify_producer_assignment(item: ListItem, previous_producer: staff.StaffId, keeper: staff.StaffId) -> int:
    if item.producer in (keeper, previous_producer):
        return 0
    try:
        members = staff.snapshot()
    except (ValueError, staff.StaffRefused, OSError) as error:
        raise ListError(f"{item.id} was saved. The producer and showrunner notices were not sent. {error}") from error
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    answer = commands.add_parser("answer", help="save an answer to a List question")
    _ = answer.add_argument("id", help="the List item ID, such as L1")
    _ = answer.add_argument("--question", required=True, help="the question ID, such as Q1")
    _ = answer.add_argument("--text", required=True, help="the answer to show on The List")
    _ = answer.add_argument("--keeper", type=staff.StaffId,
                            help="the staff ID of the record keeper (defaults to your staff ID)")
    top = commands.add_parser("top-priority", help="make an open item the top priority item of its producer")
    _ = top.add_argument("--id", required=True, help="the List item ID, such as L1")
    for name in ("add", "update", "park", "close", "reopen", "mark-read", "clean-evidence", "link", "unlink", "list"):
        command = commands.add_parser(name)
        if name != "list":
            _ = command.add_argument("--source", required=name != "update")
        if name in ("link", "unlink"):
            _ = command.add_argument("id")
            _ = command.add_argument("--review", dest="reviews", action="append", type=ReviewByShownId, default=[])
            _ = command.add_argument("--review-key", dest="reviews", action="append", type=ReviewByKey, default=[])
        elif name not in ("add", "list", "clean-evidence"):
            _ = command.add_argument("--id", required=True)
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
        caller = record.by_current_session(staff.caller_identity())
        if caller is None:
            raise ListError("the current session has no staff row")
        if caller.kind != "producer":
            raise ListError("the current session must be a producer")
        if name == "answer":
            identity, question = cast(str, args.id), cast(str, args.question)
            target = cast(staff.StaffId | None, args.keeper)
            keeper = caller.id if target is None else target
            if keeper != caller.id and caller.id != producer_id(record, "morty"):
                raise ListError("only the record keeper or morty can answer")
            result = page_question.answer(_ListQuestionStore(keeper, caller.name), identity, question,
                                          cast(str, args.text))
            if isinstance(result, page_question.AnswerRefused):
                raise ListError(result.reason)
            print(f"answered {identity} {question}")
            return 0
        keeper = producer_id(record, "morty")
        assigned_to = AllItems.INCLUDED if caller.id == keeper else caller.id
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
            identity = _cli_item_id(cast(str, args.id))
            if assigned_to is not AllItems.INCLUDED:
                options = cast(dict[str, object], vars(args))
                _check_producer_update_fields(key for key, value in options.items()
                                              if value is not None and key not in {"command", "id", "source"})
                _ = _update(keeper, identity, _cli_changes(args, record), source=source or "",
                            add_evidence=add_evidence or "", assigned_to=caller)
                return 0
            if source is None:
                parser.error("update requires --source for morty")
            changed = _update(keeper, identity, _cli_changes(args, record), source=source, add_evidence=add_evidence or "")
            if isinstance(changed, UnchangedEdit):
                return 0
            return _notify_producer_assignment(changed.item, changed.before.producer, keeper)
        if name == "top-priority":
            item = make_top_priority(keeper, _cli_item_id(cast(str, args.id)), assigned_to=assigned_to)
            producer = record.by_id(item.producer)
            print(f"{item.id} is the top priority item of {producer.name if producer is not None else item.producer}")
            return 0
        if assigned_to is not AllItems.INCLUDED and name not in ("add", "park", "close", "reopen"):
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
            changes = _cli_changes(args, record)
            number = cast(dict[str, object], vars(args)).get("number")
            if assigned_to is not AllItems.INCLUDED:
                if changes.get("producer", caller.id) != caller.id:
                    raise ListError("a producer can add only its own items")
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
            return _notify_producer_assignment(item, caller.id, keeper)
        elif name == "park":
            _ = park(keeper, cast(str, args.id), parking_value(cast(str, args.until)), source=source,
                     assigned_to=assigned_to)
        elif name == "close":
            _ = close(keeper, cast(str, args.id), cast(str, args.words), source=source, assigned_to=assigned_to)
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
