"""Keep The List as numbered records under the producer's fixed staff ID."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import json
import re
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict, cast

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed
from scripts.lib.user_zone import user_zone
from scripts.production import record_store, staff
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


class EtaStatus(StrEnum):
    NOT_ASKED = "Not asked"
    PENDING = "Pending"
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
Eta = datetime | EtaRange | EtaStatus | EtaDescription


@dataclass(frozen=True)
class ItemDependency:
    number: int
    condition: str


@dataclass(frozen=True)
class ItemChange:
    time: datetime
    what: str
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
    """Identify a List action and the words it must match on retry."""

    request: str
    action: Literal["tell", "remove", "park", "read", "question"]
    words: str
    form_words: str = ""


class NoPageRequest(StrEnum):
    ABSENT = ""


@dataclass(frozen=True)
class QuestionRequestReceipt:
    request: PageRequest
    question_id: str


@dataclass(frozen=True)
class PageFeedback:
    request: str
    words: str
    state: Literal["pending", "sent", "alerted"] = "pending"
    alerted_to: str = ""
    read_update: UnreadUpdate | NoUnreadUpdate = NoUnreadUpdate.ABSENT
    action: Literal["tell", "remove", "park"] = "tell"
    form_words: str = ""

    def key(self) -> PageRequest:
        return PageRequest(self.request, self.action, self.words, self.form_words)


@dataclass(frozen=True)
class PageItemNotice:
    """Save an item action and its notice in the same write."""

    request: str
    words: str
    action: Literal["remove", "park"]
    form_words: str = ""


PAGE_SOURCE = "nate via List"
PAGE_REQUEST_RECEIPT = "List request receipt: "
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
    parked_until: Timestamp
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

    @property
    def id(self) -> str:
        return f"L{self.number}"


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
    if value.utcoffset() is None:
        raise ListError("a time must have a time zone")
    return value


def _required_time(text: str) -> datetime:
    value = time_value(text)
    if not isinstance(value, datetime):
        raise ListError("a required time is empty")
    return value


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


def eta_text(value: Eta) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, EtaRange):
        return f"{value.earliest.isoformat()}/{value.latest.isoformat()}"
    return value.text if isinstance(value, EtaDescription) else value.value


def _line(value: str, name: str) -> None:
    if not value.strip() or value.splitlines() != [value]:
        raise ListError(f"{name} must have one nonempty line")


def _log_line(value: str, name: str) -> None:
    if not value.strip() or "\n" in value or "\r" in value:
        raise ListError(f"{name} must have one nonempty line")


def _staff_id(value: str, name: str) -> None:
    if not value or Path(value).name != value or value in (".", ".."):
        raise ListError(f"invalid {name} staff id")


def _validate(item: ListItem) -> None:
    if item.number < 1:
        raise ListError("number must be positive")
    _line(item.title, "title")
    _staff_id(item.owner, "owner")
    _staff_id(item.producer, "producer")
    if item.unit and item.unit.splitlines() != [item.unit]:
        raise ListError("unit must have one line")
    for moment in (item.opened, item.updated, item.next_check, item.unread_since, item.parked_until, item.closed):
        if isinstance(moment, datetime) and moment.utcoffset() is None:
            raise ListError("a time must have a time zone")
    if isinstance(item.eta, EtaDescription):
        _line(item.eta.text, "ETA")
    else:
        _ = eta_value(eta_text(item.eta))
    _ = next_check_value(next_check_text(item.next_check))
    if (item.state == ItemState.CLOSED) != isinstance(item.closed, datetime):
        raise ListError("only a Closed item must have a closed time")
    if item.state == ItemState.CLOSED:
        if not item.closing_words.strip():
            raise ListError("closing words must not be empty")
    elif item.closing_words:
        raise ListError("only a Closed item can have closing words")
    for dependency in item.blocked_by:
        if dependency.number < 1 or dependency.number == item.number:
            raise ListError("an item cannot depend on itself or a nonpositive number")
        _line(dependency.condition, "dependency condition")
    if len({entry.number for entry in item.blocked_by}) != len(item.blocked_by):
        raise ListError("a dependency number must occur only once")
    if any(number < 1 or number == item.number for number in item.related_to):
        raise ListError("a related number must name another positive item")
    if len(set(item.reviews)) != len(item.reviews):
        raise ListError("a review key must occur only once")
    for review in item.reviews:
        _line(review, "review key")
    for change in item.log:
        _log_line(change.what, "log text")
        _log_line(change.source, "source")
        if " — " in change.source or change.time.utcoffset() is None:
            raise ListError("a log source cannot contain its separator; its time must have a time zone")


def _dependencies(text: str) -> tuple[ItemDependency, ...]:
    value = cast(object, json.loads(text))
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


def _related(text: str) -> tuple[int, ...]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ListError("related_to must be a list")
    entries = cast(list[object], value)
    if any(type(number) is not int for number in entries):
        raise ListError("related_to must contain item numbers")
    return tuple(cast(list[int], entries))


def _review_keys(text: str) -> tuple[str, ...]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ListError("reviews must be a list of keys")
    entries = cast(list[object], value)
    if any(not isinstance(entry, str) for entry in entries):
        raise ListError("reviews must be a list of keys")
    return tuple(cast(list[str], entries))


def _read_questions(text: str) -> tuple[question_record.Question, ...]:
    value = cast(object, json.loads(text))
    if not isinstance(value, list):
        raise ListError("invalid List questions")
    questions = tuple(question_record.read(question) for question in cast(list[object], value))
    if len({question.question_id for question in questions}) != len(questions):
        raise ListError("duplicate question id")
    return questions


def load(keeper: staff.StaffId, identity: str, *, sources: list[Path] | None = None) -> ListItem:
    store = _store(keeper)
    fields, body = store.read(identity, sources=sources)
    try:
        if fields["eta_kind"] not in ("text", "time or status"):
            raise ListError("invalid ETA kind")
        marker = "## Next step\n\n"
        if not body.startswith(marker):
            raise ListError("missing Next step section")
        body = body[len(marker):]
        texts: list[str] = []
        for key, name in (("next_step", "Latest status"), ("latest_status", "Evidence"),
                          ("evidence", "Done when"), ("done_when", "Log")):
            text, body = record_store.split_section(body, int(fields[key + "_length"]), name)
            texts.append(text)
        changes: list[ItemChange] = []
        log_lines = body.removesuffix("\n").split("\n") if body else []
        for line in log_lines:
            line = line.removesuffix("\r")
            dated, source = line.removeprefix("- ").rsplit(" — ", 1)
            date, clock, what = dated.split(" ", 2)
            if not line.startswith("- "):
                raise ListError("invalid log line")
            changes.append(ItemChange(_required_time(date + "T" + clock), what, source))
        item = ListItem(int(fields["number"]), fields["title"],
                        UnassignedOwner(fields["owner"]) if fields["owner"] in UnassignedOwner else staff.StaffId(fields["owner"]),
                        staff.StaffId(fields.get("producer", keeper)),
                        fields["unit"], ItemState(fields["state"]), next_check_value(fields["next_check"]),
                        EtaDescription(fields["eta"]) if fields["eta_kind"] == "text" else eta_value(fields["eta"]),
                        time_value(fields["unread_since"]), time_value(fields["parked_until"]),
                        _dependencies(fields["blocked_by"]), _related(fields["related_to"]), RequestedBy(fields["requested_by"]),
                        time_value(fields["opened"]), time_value(fields["updated"]), time_value(fields["closed"]),
                        fields["closing_words"], texts[0], texts[1], texts[2], texts[3], tuple(changes),
                        _review_keys(fields.get("reviews", "[]")), _read_questions(fields.get("questions", "[]")))
        if item.id != identity or not changes:
            raise ListError("the item number must match its path and the log must not be empty")
        _validate(item)
        return item
    except (ValueError, KeyError, TypeError) as error:
        raise store.invalid(identity, error) from None


def _save(store: record_store.NumberedRecords, item: ListItem) -> None:
    _validate(item)
    fields = {"id": item.id, "number": str(item.number), "title": item.title,
              "owner": item.owner, "producer": item.producer, "unit": item.unit, "state": item.state.value,
              "next_check": next_check_text(item.next_check), "eta": eta_text(item.eta),
              "eta_kind": "text" if isinstance(item.eta, EtaDescription) else "time or status",
              "unread_since": _time_text(item.unread_since), "parked_until": _time_text(item.parked_until),
              "blocked_by": json.dumps([(entry.number, entry.condition) for entry in item.blocked_by], ensure_ascii=False),
              "related_to": json.dumps(item.related_to), "reviews": json.dumps(item.reviews),
              "requested_by": item.requested_by.value,
              "opened": _time_text(item.opened), "updated": _time_text(item.updated),
              "closed": _time_text(item.closed), "closing_words": item.closing_words}
    if item.questions:
        fields["questions"] = json.dumps([question_record.stored(question) for question in item.questions], ensure_ascii=False)
    body = "## Next step\n\n" + item.next_step
    log = "\n".join(f"- {entry.time.isoformat(sep=' ')} {entry.what} — {entry.source}" for entry in item.log)
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
    with store.locked():
        if any(store.folder().glob("*.md")):
            raise ListError("the list folder already holds a record")
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
        for item in items:
            _save(store, item)


def _change_text(changes: ItemChanges) -> str:
    return "; ".join(f"{key}: {json.dumps(str(value), ensure_ascii=False)}" for key, value in changes.items())


def add(keeper: staff.StaffId, title: str, *, owner: ItemOwner, source: str,
        changes: ItemChanges,
        number: int | NextNumber = NextNumber.AVAILABLE, requested_by: RequestedBy = RequestedBy.NATE,
        ) -> ListItem:
    """Create an item without reusing an existing record number."""
    store = _store(keeper)
    with store.locked():
        chosen = store.next_number() if isinstance(number, NextNumber) else number
        identity = f"L{chosen}"
        if store.path(identity).exists():
            raise ListError(f"{identity}: the item already exists")
        moment = now()
        item = ListItem(chosen, title, owner, keeper, "", ItemState.NEW, NoTimestamp.UNSET, EtaStatus.NOT_ASKED,
                        moment, NoTimestamp.UNSET, (), (), requested_by, moment, moment, NoTimestamp.UNSET, "",
                        "", "", "", "", ())
        if changes:
            item = replace(item, **changes)
        initial: ItemChanges = {"title": item.title, "owner": item.owner, "producer": item.producer,
                                "state": item.state, "eta": item.eta,
                                "next_step": item.next_step, "latest_status": item.latest_status}
        initial.update(changes)
        item = replace(item, log=(ItemChange(moment, "Added; " + _change_text(initial), source),))
        _validate(item)
        _check_links(lambda number: load(keeper, f"L{number}"), item.number, changes)
        _save(store, item)
        return item


def _change(keeper: staff.StaffId, identity: str, edit: Callable[[ListItem, datetime], ListItem],
            what: str, source: str, *, unread: bool = True,
            page_action: PageItemNotice | PageRequest | NoPageRequest = NoPageRequest.ABSENT) -> ListItem:
    store = _store(keeper)
    _ = store.path(identity)
    with store.locked():
        item = load(keeper, identity)
        request = page_action
        if isinstance(page_action, PageItemNotice):
            if not page_action.words.strip():
                raise ListError("an item notice needs words")
            request = PageRequest(page_action.request, page_action.action, page_action.words, page_action.form_words)
        if isinstance(request, PageRequest) and _previous_page_request(item, request) is not NoPageRequest.ABSENT:
            return item
        moment = now()
        changed = edit(item, moment)
        entries: list[ItemChange] = []
        if changed != item:
            entries.append(ItemChange(moment, what, source))
            changed = replace(changed, updated=moment, unread_since=moment if unread else NoTimestamp.UNSET)
        if isinstance(request, PageRequest):
            if entries and isinstance(page_action, PageItemNotice):
                pending = PageFeedback(page_action.request, page_action.words, action=page_action.action,
                                       form_words=page_action.form_words)
                entries.append(_feedback_entry(pending, moment))
            else:
                entries.insert(0, _request_receipt(request, moment))
        if not entries:
            return item
        changed = replace(changed, log=(*item.log, *entries))
        _validate(changed)
        if isinstance(request, PageRequest):
            return _save_page_request(store, keeper, changed, request)
        _save(store, changed)
        return changed


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
                entries.append(ItemChange(moment, "Removed the migration copy of checkins.json from Evidence", source))
            if cleaned != evidence:
                entries.append(ItemChange(moment, "Removed the migration copy of the List row from Evidence", source))
            _save(store, replace(item, evidence=cleaned, updated=moment, log=(*item.log, *entries)))
            changed += 1
    return changed, unchanged


def update(keeper: staff.StaffId, identity: str, changes: ItemChanges, *, source: str) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        if changes.get("state") == ItemState.CLOSED:
            raise ListError("use close to record closing words and a time")
        changed = replace(item, **changes)
        _validate(changed)
        _check_links(lambda number: load(keeper, f"L{number}"), changed.number, changes)
        return changed
    what = "Updated " + _change_text(changes)
    return _change(keeper, identity, edit, what, source)


def link(keeper: staff.StaffId, identity: str, reviews: Sequence[str], *, source: str) -> ListItem:
    """Add unique review keys after the command resolves the requested reviews."""
    chosen = tuple(dict.fromkeys(reviews))
    for review in chosen:
        _line(review, "review key")
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, reviews=tuple(dict.fromkeys((*item.reviews, *chosen))))
    return _change(keeper, identity, edit, "Linked reviews: " + ", ".join(chosen), source)


def unlink(keeper: staff.StaffId, identity: str, reviews: Sequence[str], *, source: str) -> ListItem:
    """Remove review keys while retaining the order of the other keys."""
    chosen = tuple(dict.fromkeys(reviews))
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, reviews=tuple(review for review in item.reviews if review not in chosen))
    return _change(keeper, identity, edit, "Unlinked reviews: " + ", ".join(chosen), source)


def park(keeper: staff.StaffId, identity: str, until: Timestamp, *, source: str,
         notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT) -> ListItem:
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if isinstance(notice, PageItemNotice) and (not isinstance(until, datetime) or until <= moment):
            raise ListError("Choose a future parking date.")
        _open(item)
        return replace(item, parked_until=until)
    return _change(keeper, identity, edit, "Parked until " + (_time_text(until) or "not parked"), source, page_action=notice)


def close(keeper: staff.StaffId, identity: str, words: str, *, source: str,
          notice: PageItemNotice | NoPageRequest = NoPageRequest.ABSENT) -> ListItem:
    if not words.strip():
        raise ListError("closing words must not be empty")
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if item.state == ItemState.CLOSED:
            if item.closing_words == words:
                return item
            raise ListError("the item is already Closed with different closing words")
        return replace(item, state=ItemState.CLOSED, closed=moment, closing_words=words)
    return _change(keeper, identity, edit, "Closed: " + " ".join(words.split()), source, page_action=notice)


def reopen(keeper: staff.StaffId, identity: str, *, source: str) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        if item.state != ItemState.CLOSED:
            return item
        return replace(item, state=ItemState.NEW, closed=NoTimestamp.UNSET, closing_words="", parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, "Reopened", source)


def unread_update(item: ListItem) -> UnreadUpdate | NoUnreadUpdate:
    """Keep an update identity stable when page feedback adds log entries."""
    if not isinstance(item.unread_since, datetime):
        return NoUnreadUpdate.ABSENT
    changes = [(index, entry.time.isoformat(), entry.what, entry.source) for index, entry in enumerate(item.log)
               if not (entry.source == PAGE_SOURCE and
                       (entry.what == "Marked read" or entry.what.startswith("Feedback to morty: ")
                        or is_page_request_receipt(entry)))]
    text = json.dumps((item.unread_since.isoformat(), changes), ensure_ascii=False)
    return UnreadUpdate(hashlib.sha256(text.encode()).hexdigest())


def mark_read(keeper: staff.StaffId, identity: str, *, source: str,
              update: UnreadUpdate | ReadCurrentUpdate = ReadCurrentUpdate.CURRENT,
              page_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        if isinstance(update, UnreadUpdate) and unread_update(item) != update:
            return item
        return replace(item, unread_since=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, "Marked read", source, unread=False, page_action=page_request)


def is_page_request_receipt(entry: ItemChange) -> bool:
    return entry.source == PAGE_SOURCE and entry.what.startswith(PAGE_REQUEST_RECEIPT)


def _page_requests(item: ListItem) -> dict[str, PageFeedback | PageRequest | QuestionRequestReceipt]:
    requests: dict[str, PageFeedback | PageRequest | QuestionRequestReceipt] = {}
    for entry in item.log:
        receipt = is_page_request_receipt(entry)
        if entry.source != PAGE_SOURCE or not (receipt or entry.what.startswith("Feedback to morty: ")):
            continue
        prefix = PAGE_REQUEST_RECEIPT if receipt else "Feedback to morty: "
        value = cast(object, json.loads(entry.what.removeprefix(prefix)))
        if not isinstance(value, dict):
            raise ListError("invalid List request log")
        fields = cast(dict[str, object], value)
        request, words, form_words = fields.get("request"), fields.get("words"), fields.get("form_words", "")
        action = fields.get("action", "tell")
        if not isinstance(request, str) or not isinstance(words, str) or not isinstance(form_words, str):
            raise ListError("invalid List request log")
        if receipt:
            if action not in ("read", "remove", "park", "question"):
                raise ListError("invalid List request receipt")
            received = PageRequest(request, action, words, form_words)
            if action == "question":
                question_id = fields.get("question_id")
                if not isinstance(question_id, str) or re.fullmatch(r"Q[1-9][0-9]*", question_id) is None:
                    raise ListError("invalid List question receipt")
                requests[request] = QuestionRequestReceipt(received, question_id)
            else:
                requests[request] = received
            continue
        state = fields.get("state")
        if state not in ("pending", "sent", "alerted"):
            raise ListError("invalid List feedback log")
        alerted_to = fields.get("alerted_to", "")
        if not isinstance(alerted_to, str):
            raise ListError("invalid List feedback alert")
        target = fields.get("read_update", "")
        if not isinstance(target, str):
            raise ListError("invalid List feedback read update")
        if action not in ("tell", "remove", "park"):
            raise ListError("invalid List feedback action")
        requests[request] = PageFeedback(request, words, state, alerted_to,
                                       UnreadUpdate(target) if target else NoUnreadUpdate.ABSENT,
                                       action, form_words)
    return requests


def feedback_requests(item: ListItem) -> dict[str, PageFeedback]:
    """Read the latest delivery state for each request from the item log."""
    return {request: saved for request, saved in _page_requests(item).items() if isinstance(saved, PageFeedback)}


def _previous_page_request(
        item: ListItem, request: PageRequest) -> PageFeedback | PageRequest | QuestionRequestReceipt | NoPageRequest:
    if not request.request:
        raise ListError("enter a request ID")
    previous = _page_requests(item).get(request.request, NoPageRequest.ABSENT)
    if previous is not NoPageRequest.ABSENT:
        if isinstance(previous, PageFeedback):
            key = previous.key()
        elif isinstance(previous, QuestionRequestReceipt):
            key = previous.request
        else:
            key = previous
        if key != request:
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


def _request_receipt(request: PageRequest, moment: datetime, *, question_id: str = "") -> ItemChange:
    return ItemChange(moment, PAGE_REQUEST_RECEIPT + json.dumps({
        "request": request.request, "action": request.action, "words": request.words,
        "form_words": request.form_words, **({"question_id": question_id} if question_id else {})},
        ensure_ascii=False), PAGE_SOURCE)


@dataclass(frozen=True)
class _ListQuestionStore:
    keeper: staff.StaffId
    ask_request: PageRequest | NoPageRequest = NoPageRequest.ABSENT

    def questions(self, item: str) -> tuple[question_record.Question, ...]:
        return load(self.keeper, item).questions

    def add(self, item: str, question: question_record.Question) -> None:
        # ask_question holds the List lock from the receipt check through this write.
        assert isinstance(self.ask_request, PageRequest)
        current = load(self.keeper, item)
        self.save_request(replace(current, questions=(*current.questions, question)), self.ask_request, question)

    def save_request(self, item: ListItem, request: PageRequest, question: question_record.Question) -> None:
        receipt = _request_receipt(request, now(), question_id=question.question_id)
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
            entry = ItemChange(moment, f"Answered question {question_id}", "morty")
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
    source = store.path(identity)
    questions = _ListQuestionStore(keeper, request)
    with store.locked():
        item = load(keeper, identity)
        previous = _previous_page_request(item, request)
        open_question = next((question for question in item.questions
                              if question.answer is None and question.words == request.words), None)
        if isinstance(previous, QuestionRequestReceipt):
            if open_question is None or open_question.question_id != previous.question_id:
                return change_page.NoMessageDelivery()
        elif open_question is not None:
            questions.save_request(item, request, open_question)
        command = ('~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py '
                   + 'answer {item} --question {question_id} --text "<answer>"')
        return page_question.ask(questions, identity, keeper, source, f"List item {item.number}", item.title,
                                 request.words, command, status)


def _feedback_entry(feedback: PageFeedback, moment: datetime) -> ItemChange:
    target = {"read_update": feedback.read_update.fingerprint} if isinstance(feedback.read_update, UnreadUpdate) else {}
    action = {"action": feedback.action} if feedback.action != "tell" else {}
    form_words = {"form_words": feedback.form_words} if feedback.form_words else {}
    return ItemChange(moment, "Feedback to morty: " + json.dumps({"request": feedback.request,
        "words": feedback.words, "state": feedback.state, "alerted_to": feedback.alerted_to, **target, **action,
        **form_words}, ensure_ascii=False), PAGE_SOURCE)


def save_feedback(keeper: staff.StaffId, identity: str, feedback: PageFeedback) -> PageFeedback:
    """Save the request identity, words, and pending state in one locked write."""
    if not feedback.request or not feedback.words.strip() or feedback.state != "pending":
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
        changed = replace(item, updated=moment, log=(*item.log, _feedback_entry(feedback, moment)))
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
            changed = replace(current, updated=moment, log=(*current.log, _feedback_entry(result, moment)))
            _save(store, changed)
    finally:
        with feedback_claims_lock:
            feedback_claims.remove(claim)


def sent_to_morty(item: ListItem) -> Timestamp:
    """Keep the sent notice until the next writer adds a log entry."""
    for entry in reversed(item.log):
        if entry.source != PAGE_SOURCE:
            break
        if entry.what.startswith("Feedback to morty: "):
            feedback = next(iter(feedback_requests(replace(item, log=(entry,))).values()))
            if feedback.state == "pending":
                return entry.time
    return NoTimestamp.UNSET


def order(item: ListItem) -> tuple[bool, bool, float, int]:
    """Put user decisions first, unread updates next, and early checks before late checks."""
    return (item.state != ItemState.WAITING_FOR_NATE, not isinstance(item.unread_since, datetime),
            item.next_check.timestamp() if isinstance(item.next_check, datetime) else float("inf"), item.number)


def table(keeper: staff.StaffId, *, include_closed: bool = False, include_parked: bool = False) -> str:
    moment = now()
    items = records(keeper, include_closed=include_closed)
    items = [item for item in items if include_parked or not isinstance(item.parked_until, datetime) or item.parked_until <= moment]
    items.sort(key=order)
    record = staff.snapshot()
    lines = ["| # | Owner | Producer | Item | State | Next step | ETA | Update |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in items:
        owner = record.by_id(item.owner) if not isinstance(item.owner, UnassignedOwner) else None
        state = item.state.value
        if item.state != ItemState.CLOSED and isinstance(item.next_check, datetime):
            state += "; " + ("check overdue " if item.next_check < moment else "check ") + item.next_check.isoformat()
        if item.state != ItemState.CLOSED and isinstance(item.next_check, CheckCondition):
            state += "; check: " + item.next_check.text
        if isinstance(item.parked_until, datetime) and item.parked_until > moment:
            state += "; parked until " + item.parked_until.isoformat()
        producer = record.by_id(item.producer)
        cells = (str(item.number), owner.name if owner is not None else item.owner,
                 producer.name if producer is not None else item.producer, item.title, state,
                 item.next_step, eta_text(item.eta), "●" if isinstance(item.unread_since, datetime) else "—")
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
        changes["eta"] = eta_value(eta)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    answer = commands.add_parser("answer", help="answer a List question and mark the item unread")
    _ = answer.add_argument("id", help="the List item ID, such as L1")
    _ = answer.add_argument("--question", required=True, help="the question ID, such as Q1")
    _ = answer.add_argument("--text", required=True, help="the answer to show on The List")
    for name in ("add", "update", "park", "close", "reopen", "mark-read", "clean-evidence", "link", "unlink", "list"):
        command = commands.add_parser(name)
        if name != "list":
            _ = command.add_argument("--source", required=True)
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
            for key in ("unit", "next-check", "eta", "next-step", "latest-status", "evidence", "done-when"):
                _ = command.add_argument("--" + key)
            _ = command.add_argument("--state", choices=[state.value for state in ItemState if state != ItemState.CLOSED])
            _ = command.add_argument("--requested-by", choices=list(RequestedBy))
            _ = command.add_argument("--blocked-by", nargs="*")
            _ = command.add_argument("--related-to", nargs="*")
        if name == "add":
            _ = command.add_argument("--number", type=int)
        elif name == "park":
            _ = command.add_argument("--until", required=True)
        elif name == "close":
            _ = command.add_argument("--words", required=True)
        elif name == "list":
            _ = command.add_argument("--all", action="store_true")
            _ = command.add_argument("--include-parked", action="store_true")
    args = parser.parse_args(argv)
    name = cast(str, args.command)
    if name in ("link", "unlink") and not cast(list[ReviewByShownId | ReviewByKey], args.reviews):
        parser.error("use --review or --review-key")
    try:
        record = staff.snapshot()
        caller = record.by_current_session(staff.caller_identity())
        if caller is None:
            raise ListError("the current session has no staff row")
        if caller.kind != "producer" or caller.name != "morty":
            raise ListError("the current session must be the morty producer")
        keeper = caller.id
        if name == "list":
            print(table(keeper, include_closed=cast(bool, args.all),
                        include_parked=cast(bool, args.all) or cast(bool, args.include_parked)))
            return 0
        if name == "answer":
            identity, question = cast(str, args.id), cast(str, args.question)
            result = page_question.answer(_ListQuestionStore(keeper), identity, question, cast(str, args.text))
            if isinstance(result, page_question.AnswerRefused):
                raise ListError(result.reason)
            print(f"answered {identity} {question}")
            return 0
        source = cast(str, args.source)
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
            owner = changes.get("owner")
            if owner is None:
                raise ListError("owner must name a showrunner staff id")
            print(add(keeper, cast(str, args.title), owner=owner, source=source,
                      number=number if isinstance(number, int) else NextNumber.AVAILABLE, changes=changes).id)
        elif name == "update":
            _ = update(keeper, cast(str, args.id), _cli_changes(args, record), source=source)
        elif name == "park":
            _ = park(keeper, cast(str, args.id), time_value(cast(str, args.until)), source=source)
        elif name == "close":
            _ = close(keeper, cast(str, args.id), cast(str, args.words), source=source)
        elif name == "reopen":
            _ = reopen(keeper, cast(str, args.id), source=source)
        else:
            _ = mark_read(keeper, cast(str, args.id), source=source)
        return 0
    except (ValueError, staff.StaffRefused, OSError, UnicodeError) as error:
        print(f"the_list: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
