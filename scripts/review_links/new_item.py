"""File a New item from a session."""
from __future__ import annotations

import argparse
import calendar
import difflib
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from enum import IntEnum, StrEnum
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.lib import atomic_file, conf, main_publish
from scripts.lib.file_lock import exclusive
from scripts.lib.user_zone import user_zone
from scripts.message import send_result
from scripts.production import investigation, producer_map, record_store, repeat_rule, staff, the_list, unrouted_alert
from scripts.review_links import change_page, for_you, list_draft, new_item_dialog, review_items, review_page, review_submissions

SOURCE = "nate via /new_item"
OPTIONS_TARGET = f"{review_page.NEW_ITEM_PATH}?page=list"
FORM = "/new_item <type> <assignee> <description> [extra, such as a time]"
MAY_BE_FILED = "The item may be filed. Check the List before a retry."


def file_review_save_bug(failure: review_submissions.ReviewSaveFailure) -> int:
    """File a failed Review save for morty and return its List number."""
    members = staff.snapshot()
    owner = producer_map.find_member("morty", members, ("producer",))
    if isinstance(owner, producer_map.UnknownMember) or owner.name != "morty":
        raise record_store.SubmissionError("The Bug needs one active producer named morty.")
    bug = review_submissions.file_review_save_bug(failure, owner.id, members)
    origin = bug.submission
    assert isinstance(origin, new_item_dialog.ReviewSubmission)
    key = review_items.investigation_record_key(bug)
    evidence = review_page.PAGE_PATH + "?show=all#" + urlencode({"show": "open", "jump": key})
    return the_list.add_review_item(owner.id, bug.title, owner=owner.id, producer=owner.id,
                                    unit="", words=bug.question, evidence=evidence, review_key=key,
                                    source="nate via Review", reservation=review_submissions.list_filing_reservation(origin))


class ItemType(StrEnum):
    BUG = "bug"
    QUESTION = "question"
    INVESTIGATION = "investigation"
    PROPOSAL = "proposal"
    EFFICIENCY = "efficiency"
    FEEDBACK = "feedback"
    ENHANCEMENT = "enhancement"


type _NewItemRequestKind = new_item_dialog.SubmissionKind | Literal["enhancement"]


# The server files Feedback, Efficiency and Enhancement as investigations.
FILED_KINDS: dict[ItemType, _NewItemRequestKind] = {
    ItemType.BUG: new_item_dialog.SubmissionKind.BUG,
    ItemType.QUESTION: new_item_dialog.SubmissionKind.QUESTION,
    ItemType.INVESTIGATION: new_item_dialog.SubmissionKind.INVESTIGATION,
    ItemType.PROPOSAL: new_item_dialog.SubmissionKind.PROPOSAL,
    ItemType.EFFICIENCY: new_item_dialog.SubmissionKind.INVESTIGATION,
    ItemType.FEEDBACK: new_item_dialog.SubmissionKind.INVESTIGATION,
    ItemType.ENHANCEMENT: "enhancement",
}


class Outcome(IntEnum):
    """The exit code tells the command doc what to do next."""

    FILED = 0
    FAILED = 1
    REFUSED = 2
    NO_PRODUCER = 3
    UNKNOWN_NAME = 4
    USAGE = 5


class ServerUnavailable(Exception):
    """The console server does not answer, or answers without a JSON object."""


@dataclass(frozen=True)
class ServerReply:
    status: int
    fields: dict[str, object]


@dataclass(frozen=True)
class Recipient:
    """One name in the To row of the New item window."""

    id: staff.StaffId
    name: str


@dataclass(frozen=True)
class Options:
    token: str
    recipients: tuple[Recipient, ...]


@dataclass(frozen=True)
class Refusal:
    reason: str


_UTC_OFFSET = r"Z\b|[+-][0-9]{2}:[0-5][0-9]"
_ZONE_NAME = r"[a-z0-9_+/-]+"
_START_TIME = re.compile(
    r"(?:(?P<iso>[0-9]{4}-[0-9]{2}-[0-9]{2})[T ]|"
    + r"(?P<weekday>[a-z]{3})\s+(?P<day>[a-z]{3}\s+[0-9]{1,2})(?:\s+(?P<year>[0-9]{4}))?\s+)"
    + rf"(?P<clock>[0-9]{{2}}:[0-5][0-9])\s*(?P<offset>{_UTC_OFFSET})?\s*(?P<zone>\[{_ZONE_NAME}\]|{_ZONE_NAME})?",
    re.IGNORECASE,
)
_WEEKDAYS = {day.name.lower(): day for day in repeat_rule.Weekday}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
_MENU_FREQUENCIES = {
    "daily": repeat_rule.Frequency.DAILY, "weekly": repeat_rule.Frequency.WEEKLY,
    "monthly": repeat_rule.Frequency.MONTHLY, "annually": repeat_rule.Frequency.YEARLY,
}
_CUSTOM_FREQUENCIES = {
    "day": repeat_rule.Frequency.DAILY, "week": repeat_rule.Frequency.WEEKLY,
    "month": repeat_rule.Frequency.MONTHLY, "year": repeat_rule.Frequency.YEARLY,
}
_REPEAT_MENU_ERROR = "Use a repeat from the List menu or a Custom repeat."
Scheduled = the_list.ScheduledRun | the_list.ScheduledSeries


class NoRepeat(StrEnum):
    ONCE = "one scheduled run"


def _start_time(text: str, *, now: datetime, zone: ZoneInfo) -> datetime:
    match = _START_TIME.fullmatch(text)
    if match is None:
        raise ValueError("Use a start time such as Fri Nov 27 2026 13:00 or 2026-11-27T13:00.")
    name, offset = match["zone"], match["offset"]
    try:
        named_zone = ZoneInfo(name.removeprefix("[").removesuffix("]")) if name else zone
    except ZoneInfoNotFoundError:
        raise ValueError("Use an IANA time zone such as America/Los_Angeles or a UTC offset.") from None
    fixed_zone = time.fromisoformat("00:00" + offset.upper()).tzinfo if offset else named_zone
    start_zone = named_zone if name else fixed_zone
    if match["iso"]:
        day = match["iso"]
    else:
        year = match["year"] or str(now.astimezone(start_zone).year)
        dated = datetime.strptime(f"{match['day']} {year}", "%b %d %Y")
        if dated.strftime("%a").lower() != match["weekday"].lower():
            raise ValueError(f"The start date falls on {dated:%A}.")
        day = dated.date().isoformat()
    wall = datetime.fromisoformat(f"{day}T{match['clock']}")
    start = wall.replace(tzinfo=fixed_zone).astimezone(start_zone) if offset else wall.replace(tzinfo=named_zone)
    instant = start.astimezone(UTC)
    if instant.astimezone(start.tzinfo).replace(tzinfo=None) != wall:
        if offset:
            raise ValueError("The start offset does not match its time zone.")
        raise ValueError("The start time does not exist in its time zone.")
    if not offset and start.utcoffset() != start.replace(fold=1).utcoffset():
        raise ValueError("This start time occurs twice. Give its UTC offset and time zone.")
    if instant <= now.astimezone(UTC):
        raise ValueError("The start must be in the future.")
    return start


def _repeat_end(text: str, start: datetime) -> tuple[
        str, repeat_rule.RepeatUntilLimit | repeat_rule.RunCount | repeat_rule.RunUntil]:
    rule, _, end = re.sub(r"[.,;]?\s+ends\s+", " ends ", text).partition(" ends ")
    if not end:
        match = re.fullmatch(r"(.+?)\s+(never|until .+|after .+|for .+)", rule)
        if match is not None:
            rule, end = match[1], match[2]
    if not end or end == "never":
        return rule, repeat_rule.RepeatUntilLimit.REACHED
    count = re.fullmatch(r"(?:after|for) ([0-9]+) (?:occurrences?|runs?)", end)
    if count is not None:
        return rule, repeat_rule.RunCount(int(count[1]))
    until = re.fullmatch(r"(?:on|until) (.+)", end)
    if until is not None:
        for form in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
            try:
                day = datetime.strptime(until[1], form).date()
            except ValueError:
                continue
            last = datetime.combine(day, time(23, 59, 59), start.tzinfo).astimezone(UTC)
            return rule, repeat_rule.RunUntil(last)
        raise ValueError("Use an end date such as Mar 1, 2027 or 2027-03-01.")
    raise ValueError("End a repeat with Never, On a date, or After a number of occurrences.")


def _monthly_days(selector: str, start: datetime) -> repeat_rule.MonthDay | repeat_rule.MonthWeekday:
    if selector in ("same day number", "the same day number"):
        return repeat_rule.MonthDay(start.day)
    day = re.fullmatch(r"day ([0-9]{1,2})", selector)
    if day is not None:
        return repeat_rule.MonthDay(int(day[1]))
    if selector.removesuffix(" of the month") in ("same weekday", "the same weekday"):
        weekday = tuple(repeat_rule.Weekday)[start.weekday()]
        last_day = calendar.monthrange(start.year, start.month)[1]
        ordinal = (start.day - 1) // 7 + 1
        is_last = start.day + 7 > last_day
        if ordinal == 4 and is_last:
            name = weekday.name.title()
            raise ValueError(f"Choose the fourth {name} or the last {name}.")
        return repeat_rule.MonthWeekday(-1 if is_last else ordinal, weekday)
    weekday_of_month = re.fullmatch(r"the (first|second|third|fourth|last) ([a-z]+)", selector)
    if weekday_of_month is not None and (weekday := _WEEKDAYS.get(weekday_of_month[2])) is not None:
        return repeat_rule.MonthWeekday(_ORDINALS[weekday_of_month[1]], weekday)
    raise ValueError(_REPEAT_MENU_ERROR)


def _repeat_days(frequency: repeat_rule.Frequency, selector: str, start: datetime
                 ) -> repeat_rule.StartDay | repeat_rule.WeeklyDays | repeat_rule.MonthDay | repeat_rule.MonthWeekday:
    if not selector:
        return repeat_rule.StartDay.SAME
    if frequency is repeat_rule.Frequency.WEEKLY:
        names = re.split(r",\s*(?:and )?|\s+and\s+", selector)
        if all(name in _WEEKDAYS for name in names):
            days = sorted((_WEEKDAYS[name] for name in names), key=lambda day: day.day_number)
            return repeat_rule.WeeklyDays(tuple(days))
    if frequency is repeat_rule.Frequency.MONTHLY:
        return _monthly_days(selector, start)
    if frequency is repeat_rule.Frequency.YEARLY and selector in (
            start.strftime("%B %-d").lower(), start.strftime("%b %-d").lower()):
        return repeat_rule.StartDay.SAME
    raise ValueError(_REPEAT_MENU_ERROR)


def _repeat(text: str, start: datetime) -> repeat_rule.RepeatRule | NoRepeat:
    text = " ".join(text.lower().split()).removeprefix("repeat ")
    if text in ("", "does not repeat"):
        return NoRepeat.ONCE
    rule, end = _repeat_end(text, start)
    if rule in ("every weekday", "every weekday (monday to friday)"):
        result = repeat_rule.RepeatRule(repeat_rule.Frequency.WEEKLY, 1,
                                        repeat_rule.WeeklyDays(tuple(repeat_rule.Weekday)[:5]), end)
    else:
        cadence, _, selector = rule.partition(" on ")
        frequency = _MENU_FREQUENCIES.get(cadence)
        interval = 1
        if frequency is None:
            custom = re.fullmatch(r"every (?:([0-9]+) )?(day|week|month|year)s?", cadence)
            if custom is None:
                raise ValueError(_REPEAT_MENU_ERROR)
            frequency = _CUSTOM_FREQUENCIES[custom[2]]
            if custom[1]:
                interval = int(custom[1])
        result = repeat_rule.RepeatRule(frequency, interval, _repeat_days(frequency, selector, start), end)
    result = repeat_rule.parse(result.rrule)
    result.validate_start(start)
    return result


def read_schedule(text: str, *, now: datetime, zone: ZoneInfo) -> Scheduled:
    """Read the instruction, start, and repeat from the user's words."""
    parts = re.fullmatch(r"(.+)\s+Start\s+(.+)", text.strip(), re.IGNORECASE | re.DOTALL)
    if parts is None or not parts[1].strip():
        raise ValueError("Give the instruction followed by Start and a date and time.")
    start_words, _, repeat_words = parts[2].partition(".")
    start = _start_time(start_words.strip(), now=now, zone=zone)
    run = the_list.ScheduledRun(start, parts[1].strip())
    rule = _repeat(repeat_words.strip().removesuffix(".").rstrip(), start)
    if isinstance(rule, NoRepeat):
        return run
    limit = the_list.series_run_limit()
    if isinstance(rule.end, repeat_rule.RunCount) and rule.end.count > limit:
        raise ValueError(f"The repeat exceeds the series limit of {limit} runs.")
    return the_list.ScheduledSeries(run, rule, limit)


def schedule_lines(schedule: Scheduled) -> tuple[str, str]:
    start = schedule.first.start if isinstance(schedule, the_list.ScheduledSeries) else schedule.start
    local = start.astimezone(user_zone())
    starts = f"Starts: {start:%a %b %-d %Y %H:%M %Z}"
    if start.utcoffset() != local.utcoffset():
        equivalent = "%H:%M %Z" if start.date() == local.date() else "%a %b %-d %Y %H:%M %Z"
        starts += f" ({local:{equivalent}})"
    if isinstance(schedule, the_list.ScheduledRun):
        return starts, "Does not repeat"
    rule = schedule.rule
    if isinstance(rule.end, repeat_rule.RunUntil):
        calendar_date = rule.end.utc.astimezone(start.tzinfo).date()
        cadence = replace(rule, end=repeat_rule.RepeatUntilLimit.REACHED).describe(start)
        return starts, f"Repeats: {cadence} until {calendar_date:%b %-d, %Y}"
    return starts, f"Repeats: {rule.describe(start)}"


def _say_schedule(lines: tuple[str, str]) -> None:
    print(*lines, sep="\n", flush=True)


def usage() -> str:
    return "\n".join((
        FORM,
        f"Types: {', '.join((*ItemType, new_item_dialog.SCHEDULED_KIND))}",
        "Assignee: a producer, a showrunner or a unit",
        "/new_item scheduled natedev Set the time zone back to Eastern time. Start Fri Nov 27 13:00.",
    ))


def unit_of(member: staff.StaffRow, members: staff.StaffSnapshot) -> str:
    """Name a unit with its showrunner: "tile, a unit of cargo-liner"."""
    showrunner = producer_map.showrunner_of(member, members)
    return member.name if showrunner == member else f"{member.name}, a unit of {showrunner.name}"


def _which_one(name: str, matches: list[str] | tuple[str, ...]) -> str:
    return f'More than one session has the name "{name}": {", ".join(matches)}. Which one?'


def unknown_name(unknown: producer_map.UnknownMember, members: staff.StaffSnapshot) -> str:
    """Ask about a name that is not known, with the nearest name as a guess."""
    if unknown.matches:
        return _which_one(unknown.reference, unknown.matches)
    rows = [row for kind in producer_map.ASSIGNEE_KINDS for row in staff.active(members, kind)]
    line = f'No session has the name "{unknown.reference}".'
    guess = difflib.get_close_matches(unknown.reference.casefold(), {row.name.casefold() for row in rows}, n=1)
    if not guess:
        return line
    named = [row for row in rows if row.name.casefold() == guess[0]]
    if len(named) > 1:
        # A "yes" to a name that many sessions share would only get this question next.
        return f"{line} {_which_one(named[0].name, [members.qualified_name(row) for row in named])}"
    return f"{line} Do you mean {unit_of(named[0], members)}? yes / a different name"


def producer_question(member: staff.StaffRow, missing: producer_map.NoProducer, members: staff.StaffSnapshot) -> str:
    """Ask nate which producer; the answer replaces a stale row, else it maps the showrunner of a unit."""
    if isinstance(missing, producer_map.StaleRow):
        question = f"{missing.reason()} Which producer for {missing.holder.name}?"
    else:
        showrunner = producer_map.showrunner_of(member, members)
        question = (f"{member.name} has no producer yet." if showrunner == member else
                    f"{member.name} is a unit of {showrunner.name}. Neither has a producer yet.") + " Which producer?"
    producers = " / ".join(row.name for row in staff.active(members, "producer"))
    return f"{question}\n{producers}"


def assignment_receipt(item_type: ItemType | Literal["scheduled"], member: staff.StaffRow,
                       mapped: producer_map.MappedProducer, members: staff.StaffSnapshot, *, this_session: bool) -> str:
    to = unit_of(member, members) + (", this session" if this_session else "")
    return f"{item_type.capitalize()} → {to}.{producer_line(member, mapped)}"


def filing_number_label(number: int, *, label: Literal["List item", "Bug"] = "List item") -> str:
    return f"{label} {number}"


def filing_receipt(assignment: str, identity: str) -> str:
    return f"{assignment} {filing_number_label(int(identity.removeprefix('L')))}."


def producer_line(member: staff.StaffRow, mapped: producer_map.MappedProducer) -> str:
    """Name the producer selected for a new filing."""
    if member.kind == "producer":
        return ""
    why = "set for" if mapped.set_by == member else "from"
    return f" Producer: {mapped.producer.name} ({why} {mapped.set_by.name})."


def console_address() -> for_you.ConsoleAddress:
    """Find the console server as the statusline does."""
    try:
        return for_you.console_address()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ServerUnavailable(f"cannot find the console server address: {error}") from error


def _unavailable(reason: str, *, sent: bool) -> ServerUnavailable:
    """A failure after the POST was sent can follow a filed item."""
    return ServerUnavailable(f"{reason}. {MAY_BE_FILED}" if sent else reason)


def exchange(address: for_you.ConsoleAddress, method: Literal["GET", "POST"], target: str, body: bytes = b"") -> ServerReply:
    """Send one request and read the JSON object in the reply."""
    timeout = conf.read_or_repository("review_links").whole("page_request_timeout_seconds")
    connection = HTTPConnection(address.host, address.port, timeout=timeout)
    origin = f"http://{address.host}:{address.port}"
    sent = False
    try:
        headers = ({"Content-Type": "application/json"} if method == "POST"
                   else {"X-New-Item-Route": "producer"})
        connection.request(method, target, body=body if method == "POST" else None, headers=headers)
        sent = method == "POST"
        response = connection.getresponse()
        status, raw = response.status, response.read()
    except (HTTPException, OSError) as error:
        raise _unavailable(f"the console server at {origin} does not answer: {error}", sent=sent) from error
    finally:
        connection.close()
    try:
        payload = cast(object, json.loads(raw))
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise _unavailable(f"the console server at {origin} sent HTTP {status} without a JSON object",
                           sent=method == "POST")
    return ServerReply(status, cast(dict[str, object], payload))


def _server_error(reply: ServerReply) -> str:
    error = reply.fields.get("error")
    return error if isinstance(error, str) and error else f"the console server sent HTTP {reply.status}"


def read_options(address: for_you.ConsoleAddress) -> Options:
    """Read the window token and the To row."""
    reply = exchange(address, "GET", OPTIONS_TARGET)
    if reply.status != 200:
        raise ServerUnavailable(_server_error(reply))
    disabled = reply.fields.get("send_disabled")
    token = reply.fields.get("token")
    entries = reply.fields.get("recipients")
    if isinstance(disabled, str) and disabled:
        raise ServerUnavailable(disabled)
    if not isinstance(token, str) or not token or not isinstance(entries, list):
        raise ServerUnavailable("the console server sent no New item token or To row")
    recipients: list[Recipient] = []
    for entry in cast(list[object], entries):
        fields = cast(dict[str, object], entry) if isinstance(entry, dict) else {}
        identity, name = fields.get("id"), fields.get("name")
        if isinstance(identity, str) and isinstance(name, str):
            recipients.append(Recipient(staff.StaffId(identity), name))
    return Options(token, tuple(recipients))


def _names(rows: tuple[Recipient, ...]) -> str:
    return ", ".join(row.name for row in rows) or "none"


def recipient_for(producer: staff.StaffRow, options: Options) -> Recipient | Refusal:
    """Find the mapped producer in the To row."""
    found = next((row for row in options.recipients if row.id == producer.id), None)
    if found is None:
        return Refusal(f"{producer.name} is not in the To row: {_names(options.recipients)}")
    return found


def request_body(options: Options, item_type: ItemType, recipient: staff.StaffId, words: str,
                 assignee: staff.StaffId, request_id: str, routing: new_item_dialog.PageRecipient) -> bytes:
    """Send the List window fields for a new List draft."""
    return json.dumps({
        "token": options.token, "request_id": request_id, "kind": FILED_KINDS[item_type],
        "notice": {"kind": item_type, "from_command": True},
        "recipient_id": recipient, "words": words, "evidence": {"kind": "none"}, "images": [],
        "page": "list", "page_recipient": routing,
        ("assignee_id" if routing == new_item_dialog.PageRecipient.PRODUCER else "list_assignee"): assignee,
    }).encode()


@dataclass(frozen=True)
class PendingListDraft:
    """Keep the request ID and recipient while the result is uncertain."""

    request_id: str
    recipient: staff.StaffId
    routing: new_item_dialog.PageRecipient


@dataclass(frozen=True)
class NoPendingListDraft:
    """The command has no draft with an uncertain result."""


def draft_path(address: for_you.ConsoleAddress, session: str, assignee: staff.StaffId,
               item_type: ItemType, words: str, *, legacy: bool = False) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "new-item"
    scope: list[object] = [address.host, address.port, session, assignee, FILED_KINDS[item_type], words]
    if not legacy:
        scope.append(item_type)
    return state / hashlib.sha256(json.dumps(scope).encode()).hexdigest()


def read_pending_draft(path: Path) -> PendingListDraft | NoPendingListDraft:
    try:
        fields = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return NoPendingListDraft()
    if len(fields) == 2:
        fields.append(new_item_dialog.PageRecipient.SELECTED.value)
    if (len(fields) != 3 or not re.fullmatch(r"r6-[0-9a-f]{32}", fields[0]) or not fields[1]
            or fields[2] not in (new_item_dialog.PageRecipient.SELECTED, new_item_dialog.PageRecipient.PRODUCER)):
        raise OSError("Cannot read the pending New item draft.")
    return PendingListDraft(fields[0], staff.StaffId(fields[1]), new_item_dialog.PageRecipient(fields[2]))


def save_pending_draft(path: Path, recipient: staff.StaffId) -> PendingListDraft:
    draft = PendingListDraft("r6-" + secrets.token_hex(16), recipient, new_item_dialog.PageRecipient.PRODUCER)
    atomic_file.write_text_atomic(path, f"{draft.request_id}\n{draft.recipient}\n{draft.routing}\n", new_file_mode=0o600)
    return draft


def post_item(address: for_you.ConsoleAddress, options: Options, item_type: ItemType,
              draft: PendingListDraft, words: str, assignee: staff.StaffId) -> ServerReply:
    """Retry an unconfirmed POST with the same draft ID and recipient."""
    def send(current: Options) -> ServerReply:
        return exchange(address, "POST", review_page.NEW_ITEM_PATH,
                        request_body(current, item_type, draft.recipient, words, assignee, draft.request_id, draft.routing))

    try:
        reply = send(options)
    except ServerUnavailable:
        reply = send(options)
    else:
        if reply.status == 503:
            reply = send(options)
    if reply.status == 403 and reply.fields.get("code") == "stale_token":
        reply = send(read_options(address))
    return reply


def words_text(item_type: ItemType, description: str, extra: str) -> str:
    """Add the Efficiency prefix and append any extra words."""
    first = f"Efficiency: {description.strip()}" if item_type == ItemType.EFFICIENCY else description.strip()
    return first + (f"\n\n{extra.strip()}" if extra.strip() else "")


class FilingRefusal(StrEnum):
    INVALID_SCHEDULE = "invalid schedule"
    UNKNOWN_ASSIGNEE = "unknown assignee"
    UNMAPPED_PRODUCER = "unmapped producer"
    UNAVAILABLE_OWNER = "unavailable owner"
    UNAVAILABLE_PRODUCER = "unavailable producer"


class FilingRefused(record_store.SubmissionError):
    """The filing checks failed before the item was saved."""

    def __init__(self, reason: FilingRefusal, message: str) -> None:
        super().__init__(message)
        self.reason: FilingRefusal = reason


def _assignment(assignee: str, members: staff.StaffSnapshot) -> tuple[staff.StaffRow, producer_map.MappedProducer]:
    member = producer_map.find_member(assignee, members, producer_map.ASSIGNEE_KINDS)
    if isinstance(member, producer_map.UnknownMember):
        raise FilingRefused(FilingRefusal.UNKNOWN_ASSIGNEE, unknown_name(member, members))
    return member, _mapped_producer(member, members)


def _mapped_producer(member: staff.StaffRow, members: staff.StaffSnapshot) -> producer_map.MappedProducer:
    mapped = producer_map.producer_for(member.id, members)
    if isinstance(mapped, producer_map.NoProducer):
        raise FilingRefused(FilingRefusal.UNMAPPED_PRODUCER, producer_question(member, mapped, members))
    return mapped


def _checked_schedule(words: str) -> Scheduled:
    try:
        return read_schedule(words, now=the_list.now(), zone=user_zone())
    except (ValueError, OverflowError) as error:
        raise FilingRefused(FilingRefusal.INVALID_SCHEDULE, str(error)) from error


@dataclass(frozen=True)
class ScheduledFiling:
    keeper: staff.StaffId
    member: staff.StaffRow
    producer: staff.StaffId
    schedule: Scheduled
    assignment: str
    readback: tuple[str, str]

    @property
    def title(self) -> str:
        run = self.schedule.first if isinstance(self.schedule, the_list.ScheduledSeries) else self.schedule
        return " ".join(run.words.split())

    def save(self, *, new_item_draft_id: review_submissions.ListDraftOrigin = review_submissions.NoNewItemDraft.ABSENT,
             before_save: Callable[[int], None] | None = None) -> the_list.ListItem:
        changes: the_list.ItemChanges = {
            "state": the_list.ItemState.SCHEDULED, "schedule": self.schedule, "producer": self.producer,
            "unit": self.member.name if self.member.kind == "unit" else "",
        }
        return the_list.add(self.keeper, self.title, owner=self.member.id, source=SOURCE, changes=changes,
                            new_item_draft_id=new_item_draft_id, before_save=before_save)


def prepare_scheduled(assignee: str | staff.StaffRow, words: str, members: staff.StaffSnapshot, *,
                      producer: staff.StaffRow | None = None) -> ScheduledFiling:
    """Check the schedule, owner and producer before the List write."""
    schedule = _checked_schedule(words)
    if isinstance(assignee, str):
        member, mapped = _assignment(assignee, members)
        try:
            review_submissions.require_available_recipient(member, members)
        except record_store.SubmissionConflict as error:
            raise FilingRefused(FilingRefusal.UNAVAILABLE_OWNER, str(error)) from error
    elif producer is None:
        member, mapped = assignee, _mapped_producer(assignee, members)
    else:
        member, mapped = assignee, producer_map.MappedProducer(producer, producer)
    if member.id != mapped.producer.id:
        try:
            _ = review_submissions.selected_recipient(mapped.producer.id, members)
        except record_store.SubmissionConflict as error:
            raise FilingRefused(FilingRefusal.UNAVAILABLE_PRODUCER, str(error)) from error
    keeper = the_list.producer_id(members, "morty")
    run = schedule.first if isinstance(schedule, the_list.ScheduledSeries) else schedule
    if run.start.astimezone(UTC) <= the_list.now():
        raise FilingRefused(FilingRefusal.INVALID_SCHEDULE, "The start must be in the future.")
    assignment = assignment_receipt(new_item_dialog.SCHEDULED_KIND, member, mapped, members, this_session=False)
    return ScheduledFiling(keeper, member, mapped.producer.id, schedule, assignment, schedule_lines(schedule))


def _assignment_notices(title: str, producer_id: staff.StaffId, keeper: staff.StaffId, member: staff.StaffRow,
                        members: staff.StaffSnapshot) -> tuple[tuple[review_submissions.ListAssignmentNotice, ...], str]:
    if producer_id == keeper:
        return (), ""
    producer = members.by_id(producer_id)
    if producer is None:
        return (), "At least one assignment notice failed. The List item is saved."
    showrunner = producer_map.showrunner_of(member, members)
    title = title if title.endswith((".", "?", "!")) else title + "."
    if showrunner.kind == "showrunner":
        notices = (
            review_submissions.ListAssignmentNotice(producer.id, "producer", f"{title} Its showrunner is {showrunner.name}."),
            review_submissions.ListAssignmentNotice(showrunner.id, "showrunner", f"{title} "
                + f"Send design questions on this item to {producer.name}.", producer_name=producer.name),
        )
        return notices, ""
    notice = review_submissions.ListAssignmentNotice(producer.id, "producer", f"{title} The item has no assigned showrunner.")
    return (notice,), "At least one assignment notice failed. The List item is saved."


def _assignment_message(notice: review_submissions.ListAssignmentNotice, identity: str) -> str:
    number = identity.removeprefix("L")
    if notice.role == "producer":
        launcher = cast(str, getattr(the_list, "_LAUNCHER"))
        update = ", ".join(f"`{option}`" for option in the_list.PRODUCER_SCHEDULE_UPDATE_OPTIONS)
        return (f"You are the producer of item {number}: {notice.text}\n"
                + f"Run `{launcher} list --all --latest-status` to read it. "
                + f"Run `{launcher} update --id {identity}` to update it. Use any of {update}.")
    if notice.producer_name:
        return f"{notice.producer_name} is the producer of item {number}: {notice.text}"
    return f"Item {number}: {notice.text}"


def _deliver_scheduled_notice(notice: review_submissions.ListAssignmentNotice, identity: str, key: str) -> bool:
    message = _assignment_message(notice, identity)
    try:
        outcome, error = unrouted_alert.message_outcome(
            notice.recipient, "morty", message, "--recipient-kind", notice.role,
            *change_page.review_send_options(key, notice.recipient))
    except (OSError, subprocess.SubprocessError, UnicodeError) as failure:
        error = str(failure)
    else:
        if isinstance(outcome, send_result.ConfirmedSendOutcome) and outcome.header.outcome in {
                "sent", "kept", "queued", "skipped"}:
            return True
    print(f"new_item: {identity} notice to {notice.recipient}: {error}", file=sys.stderr)
    return False


def _load_list_draft(keeper: staff.StaffId, identity: str) -> list_draft.ListDraftOrigin:
    return the_list.load(keeper, identity).new_item_draft_id


def refuse_scheduled_draft_reuse(request_id: str) -> None:
    review_submissions.refuse_scheduled_draft_reuse(request_id, load_list_draft=_load_list_draft)


def submit_scheduled(request: new_item_dialog.ScheduledSubmission) -> review_submissions.ListItemDestination:
    """File a browser Schedule and recover its receipt and notices on Retry."""
    fields: list[object] = [request.page, request.recipient_id, request.words]
    if request.producer_route:
        fields.extend(("producer", new_item_dialog.assignment_id(request.assignment)))
    digest = hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    @contextmanager
    def prepare() -> Generator[tuple[review_submissions.ListDraftClaim, review_submissions.ListDraftWriter]]:
        with investigation.recipient_locks(request.recipient_id):
            members = staff.snapshot()
            recipient = review_submissions.selected_recipient(request.recipient_id, members)
            if request.producer_route and recipient.kind != "producer":
                raise record_store.SubmissionError("Pick a producer.")
            producer = recipient if request.producer_route else None
            owner = review_submissions.assigned_member(request.assignment, recipient, members)
            filing = prepare_scheduled(owner, request.words, members, producer=producer)
            notices, warning = _assignment_notices(filing.title, filing.producer, filing.keeper, owner, members)
            claim = review_submissions.ListDraftClaim(digest, filing.keeper, filing.assignment, filing.readback, notices, warning)
            draft = review_submissions.NewItemDraft(request.request_id, claim)

            def save(claim_number: Callable[[int], None]) -> str:
                return filing.save(new_item_draft_id=draft, before_save=claim_number).id

            yield claim, save

    return review_submissions.file_list_draft(request.request_id, digest, prepare=prepare,
                                              load_list_draft=_load_list_draft)


def deliver_scheduled_notices(root: Path, draft_id: new_item_dialog.ScheduledDraftId) -> None:
    review_submissions.deliver_list_notices(root, draft_id, _deliver_scheduled_notice)


def recover_scheduled_notices(root: Path) -> None:
    for draft_id in review_submissions.pending_scheduled_notices(root):
        try:
            deliver_scheduled_notices(root, draft_id)
        except (OSError, ValueError) as error:
            print(f"Review Scheduled notice {draft_id.value}: {error}", file=sys.stderr)


def scheduled_notice(item: the_list.ListItem, keeper: staff.StaffId, members: staff.StaffSnapshot) -> str:
    """Send the assignment notices after filing the item."""
    member = members.by_id(staff.StaffId(item.owner))
    if member is None:
        return "The owner is unavailable. The List item is saved."
    notices, warning = _assignment_notices(item.title, item.producer, keeper, member, members)
    send = cast(Callable[[staff.StaffRow, str], None], getattr(the_list, "_send_assignment_notice"))
    if warning and notices:
        print(f"the_list: {item.id} was saved. The showrunner notice to {item.owner} failed. "
              + "the owner has no current showrunner staff row", file=sys.stderr)
    for notice in notices:
        recipient = members.by_id(notice.recipient)
        assert recipient is not None
        try:
            send(recipient, _assignment_message(notice, item.id))
        except (ValueError, OSError, staff.StaffRefused, subprocess.SubprocessError) as error:
            print(f"new_item: {item.id} notice to {notice.recipient}: {error}", file=sys.stderr)
            warning = "At least one assignment notice failed. The List item is saved."
    return warning


class Arguments(argparse.Namespace):
    item_type: str = ""
    assignee: str = ""
    description: str = ""
    extra: str = ""
    session: str = ""
    preview: bool = False


def main(arguments: list[str] | None = None) -> int:
    argv = sys.argv[1:] if arguments is None else arguments
    if not argv:
        print(usage())
        return Outcome.USAGE
    if platform.system() == "Darwin":
        # The Mac uses the natedev staff snapshot and console address.
        return main_publish.forward("review_links/new_item.py", [*argv, "--session", staff.caller_identity()],
                                    label="new_item", ssh_program=os.environ.get("ZONE_SSH") or "ssh")
    parser = argparse.ArgumentParser(prog="new_item.py", description=__doc__)
    _ = parser.add_argument("item_type", type=str.lower, choices=(*ItemType, new_item_dialog.SCHEDULED_KIND))
    _ = parser.add_argument("assignee")
    _ = parser.add_argument("description")
    _ = parser.add_argument("extra", nargs="?", default="")
    _ = parser.add_argument("--session", default=staff.caller_identity(), help=argparse.SUPPRESS)
    _ = parser.add_argument("--preview", action="store_true", help="read a Scheduled start and repeat without saving")
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.description.strip():
        parser.error("the description is empty")
    if args.preview and args.item_type != new_item_dialog.SCHEDULED_KIND:
        parser.error("--preview requires the scheduled type")
    try:
        if args.item_type == new_item_dialog.SCHEDULED_KIND:
            words = f"{args.description} {args.extra}"
            if args.preview:
                _say_schedule(schedule_lines(_checked_schedule(words)))
            else:
                members = staff.snapshot()
                filing = prepare_scheduled(args.assignee, words, members)
                _say_schedule(filing.readback)
                saved = filing.save()
                print(filing_receipt(filing.assignment, saved.id))
                warning = scheduled_notice(saved, filing.keeper, members)
                if warning:
                    print(warning)
            return Outcome.FILED
        item_type = ItemType(args.item_type)
        members = staff.snapshot()
        member = producer_map.find_member(args.assignee, members, producer_map.ASSIGNEE_KINDS)
        if isinstance(member, producer_map.UnknownMember):
            print(unknown_name(member, members))
            return Outcome.UNKNOWN_NAME
        caller = members.by_current_session(args.session) if args.session else None
        to = unit_of(member, members) + (", this session" if caller is not None and caller.id == member.id else "")
        result = f"{item_type.value.capitalize()} → {to}."
        words = words_text(item_type, args.description, args.extra)
        try:
            address = console_address()
        except ServerUnavailable:
            mapped = producer_map.producer_for(member.id, members)
            if isinstance(mapped, producer_map.NoProducer):
                print(producer_question(member, mapped, members))
                return Outcome.NO_PRODUCER
            raise
        path = draft_path(address, args.session, member.id, item_type, words)
        legacy_path = draft_path(address, args.session, member.id, item_type, words, legacy=True)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with exclusive(legacy_path.with_suffix(".lock")):
            draft = read_pending_draft(path)
            if isinstance(draft, NoPendingListDraft):
                legacy_draft = read_pending_draft(legacy_path)
                if isinstance(legacy_draft, PendingListDraft):
                    path, draft = legacy_path, legacy_draft
            if isinstance(draft, NoPendingListDraft):
                mapped = producer_map.producer_for(member.id, members)
                if isinstance(mapped, producer_map.NoProducer):
                    print(producer_question(member, mapped, members))
                    return Outcome.NO_PRODUCER
                options = read_options(address)
                recipient = recipient_for(mapped.producer, options)
                if isinstance(recipient, Refusal):
                    print(f"new_item: {recipient.reason}", file=sys.stderr)
                    return Outcome.REFUSED
                draft = save_pending_draft(path, recipient.id)
                result += producer_line(member, mapped)
            else:
                options = read_options(address)
            reply = post_item(address, options, item_type, draft, words, member.id)
            refused = reply.status == 400 or reply.status == 409 and reply.fields.get("code") != "already_filed"
            if reply.status == 200 or refused:
                path.unlink()
        if reply.status != 200:
            print(f"new_item: {_server_error(reply)}", file=sys.stderr)
            return Outcome.FAILED
    except FilingRefused as error:
        if error.reason in (FilingRefusal.INVALID_SCHEDULE, FilingRefusal.UNAVAILABLE_OWNER, FilingRefusal.UNAVAILABLE_PRODUCER):
            print(f"new_item: {error}", file=sys.stderr)
            return Outcome.REFUSED
        print(error)
        return Outcome.NO_PRODUCER if error.reason is FilingRefusal.UNMAPPED_PRODUCER else Outcome.UNKNOWN_NAME
    except (ServerUnavailable, OSError, ValueError, staff.StaffRefused) as error:
        print(f"new_item: {error}", file=sys.stderr)
        return Outcome.FAILED
    number = reply.fields.get("list_number")
    if type(number) is int and number > 0:
        filed = f"{filing_number_label(number)}."
    else:
        review_id = reply.fields.get("id")
        shown = review_id if isinstance(review_id, str) and review_id else "a Review item"
        filed = f"Filed as {shown}. The console server returned no List number."
    print(f"{result} {filed}")
    return Outcome.FILED


if __name__ == "__main__":
    raise SystemExit(main())
