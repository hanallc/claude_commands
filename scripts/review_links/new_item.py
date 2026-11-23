"""File a New item from a session."""
from __future__ import annotations

import argparse
import calendar
import difflib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from enum import Enum, IntEnum, StrEnum
from http.client import HTTPConnection, HTTPException
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.lib import conf, main_publish
from scripts.lib.user_zone import user_zone
from scripts.production import investigation, producer_map, repeat_rule, staff, the_list
from scripts.review_links import for_you, new_item_dialog, review_page, review_submissions

SOURCE = "nate via /new_item"
OPTIONS_TARGET = f"{review_page.NEW_ITEM_PATH}?page=list"
FORM = "/new_item <type> <assignee> <description> [extra, such as a time]"
MAY_BE_FILED = "The item may be filed. Check the List before a retry."


class ItemType(StrEnum):
    BUG = "bug"
    QUESTION = "question"
    INVESTIGATION = "investigation"
    PROPOSAL = "proposal"
    EFFICIENCY = "efficiency"
    FEEDBACK = "feedback"


# The console server takes four kinds. Feedback and efficiency file as an investigation.
FILED_KINDS: dict[ItemType, new_item_dialog.SubmissionKind] = {
    ItemType.BUG: new_item_dialog.SubmissionKind.BUG,
    ItemType.QUESTION: new_item_dialog.SubmissionKind.QUESTION,
    ItemType.INVESTIGATION: new_item_dialog.SubmissionKind.INVESTIGATION,
    ItemType.PROPOSAL: new_item_dialog.SubmissionKind.PROPOSAL,
    ItemType.EFFICIENCY: new_item_dialog.SubmissionKind.INVESTIGATION,
    ItemType.FEEDBACK: new_item_dialog.SubmissionKind.INVESTIGATION,
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
_EASTERN = ZoneInfo("America/New_York")
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
    return the_list.ScheduledSeries(run, rule)


def schedule_lines(schedule: Scheduled) -> tuple[str, str]:
    start = schedule.first.start if isinstance(schedule, the_list.ScheduledSeries) else schedule.start
    eastern = start.astimezone(_EASTERN)
    starts = f"Starts: {start:%a %b %-d %Y %H:%M %Z}"
    if start.utcoffset() != eastern.utcoffset():
        equivalent = "%H:%M %Z" if start.date() == eastern.date() else "%a %b %-d %Y %H:%M %Z"
        starts += f" ({eastern:{equivalent}})"
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


def result_line(item_type: ItemType | Literal["scheduled"], member: staff.StaffRow, mapped: producer_map.MappedProducer,
                members: staff.StaffSnapshot, *, this_session: bool, list_key: str) -> str:
    """Say who does the work, who tracks it and why, and the List item number."""
    assignment = assignment_receipt(item_type, member, mapped, members, this_session=this_session)
    return filing_receipt(assignment, list_key.partition("/")[2])


def assignment_receipt(item_type: ItemType | Literal["scheduled"], member: staff.StaffRow,
                       mapped: producer_map.MappedProducer, members: staff.StaffSnapshot, *, this_session: bool) -> str:
    to = unit_of(member, members) + (", this session" if this_session else "")
    producer = ""
    if member.kind != "producer":
        why = "set for" if mapped.set_by == member else "from"
        producer = f" Producer: {mapped.producer.name} ({why} {mapped.set_by.name})."
    return f"{item_type.capitalize()} → {to}.{producer}"


def filing_receipt(assignment: str, identity: str) -> str:
    return f"{assignment} List item {identity.removeprefix('L')}."


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
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
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


def recipient_for(member: staff.StaffRow, options: Options, members: staff.StaffSnapshot) -> Recipient | Refusal:
    """The member, or the first owner above it, that is in the To row receives the item."""
    by_id = {row.id: row for row in options.recipients}
    found = next((by_id[identity] for identity in investigation.owner_chain(member.id, members) if identity in by_id), None)
    if found is None:
        return Refusal(f"{member.name}: neither it nor an owner above it is in the To row: {_names(options.recipients)}")
    return found


def request_body(options: Options, kind: new_item_dialog.SubmissionKind, recipient: Recipient, words: str) -> bytes:
    """Send the List window fields for a new List draft."""
    return json.dumps({
        "token": options.token, "request_id": "r6-" + secrets.token_hex(16), "kind": kind.value,
        "recipient_id": recipient.id, "words": words, "evidence": {"kind": "none"}, "images": [],
        "page": "list", "page_recipient": new_item_dialog.PageRecipient.SELECTED.value,
    }).encode()


def words_text(item_type: ItemType, description: str, extra: str) -> str:
    """A type that files as another server kind names itself first; feedback files as the window files it."""
    marked = FILED_KINDS[item_type].value != item_type.value and item_type != ItemType.FEEDBACK
    first = f"{item_type.value.capitalize()}: {description.strip()}" if marked else description.strip()
    return first + (f"\n\n{extra.strip()}" if extra.strip() else "")


def set_assignee(list_key: str, member: staff.StaffRow, mapped: producer_map.MappedProducer) -> None:
    """The List item names a unit assignee and takes the assignee's producer, not the recipient's."""
    keeper, _, identity = list_key.partition("/")
    item = the_list.load(staff.StaffId(keeper), identity)
    changes: the_list.ItemChanges = {"unit": member.name} if member.kind == "unit" else {}
    if mapped.producer.id != item.producer:
        changes["producer"] = mapped.producer.id
    if changes:
        _ = the_list.update(staff.StaffId(keeper), identity, changes, source=SOURCE)


class FilingRefusal(StrEnum):
    INVALID_SCHEDULE = "invalid schedule"
    UNKNOWN_ASSIGNEE = "unknown assignee"
    UNMAPPED_PRODUCER = "unmapped producer"
    UNAVAILABLE_PRODUCER = "unavailable producer"


class FilingRefused(ValueError):
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


class ListAssignmentNotice(Protocol):
    def __call__(self, item: the_list.ListItem, previous_producer: staff.StaffId, keeper: staff.StaffId, *,
                 caller: staff.StaffId) -> int: ...


def _assignment_warning(item: the_list.ListItem, keeper: staff.StaffId, member: staff.StaffRow,
                        members: staff.StaffSnapshot) -> str:
    # Use the List CLI sender until the List exports its assignment notice function.
    send = cast(ListAssignmentNotice, getattr(the_list, "_notify_producer_assignment"))
    showrunner = producer_map.showrunner_of(member, members)
    notice_item = replace(item, owner=showrunner.id) if showrunner.id != item.owner else item
    try:
        if send(notice_item, keeper, keeper, caller=keeper) == 0:
            return ""
    except (ValueError, OSError, staff.StaffRefused) as error:
        print(f"new_item: {error}", file=sys.stderr)
    return "At least one assignment notice failed. The List item is saved."


class DirectScheduledFiling(StrEnum):
    CLI = "direct CLI filing"


class UnfiledScheduledDraft(Enum):
    UNSAVED = "no saved List item"


@dataclass(frozen=True)
class ScheduledDraftIdentity:
    id: str

    def evidence(self) -> str:
        return f"New item draft {self.id}"

    def saved_item(self, keeper: staff.StaffId) -> the_list.ListItem | UnfiledScheduledDraft:
        evidence = self.evidence()
        try:
            records = list(the_list.folder(keeper).iterdir())
        except FileNotFoundError:
            return UnfiledScheduledDraft.UNSAVED
        for path in records:
            if not re.fullmatch(r"L[1-9][0-9]*\.md", path.name):
                continue
            item = the_list.load(keeper, path.stem)
            if not item.log:
                continue
            added = item.log[0].what
            if (isinstance(added, the_list.FieldChange) and added.action == "Added"
                    and added.fields.get("evidence") == evidence):
                return item
        return UnfiledScheduledDraft.UNSAVED


@dataclass(frozen=True)
class ScheduledFiling:
    keeper: staff.StaffId
    member: staff.StaffRow
    producer: staff.StaffId
    schedule: Scheduled
    assignment: str
    readback: tuple[str, str]

    def save(self, draft: ScheduledDraftIdentity | DirectScheduledFiling = DirectScheduledFiling.CLI) -> the_list.ListItem:
        run = self.schedule.first if isinstance(self.schedule, the_list.ScheduledSeries) else self.schedule
        changes: the_list.ItemChanges = {
            "state": the_list.ItemState.SCHEDULED, "schedule": self.schedule, "producer": self.producer,
            "unit": self.member.name if self.member.kind == "unit" else "",
        }
        if isinstance(draft, ScheduledDraftIdentity):
            # The List saves this field in the creation log with the item.
            changes["evidence"] = draft.evidence()
        return the_list.add(self.keeper, " ".join(run.words.split()), owner=self.member.id, source=SOURCE, changes=changes)


def prepare_scheduled(assignee: str | staff.StaffRow, words: str, members: staff.StaffSnapshot) -> ScheduledFiling:
    """Check the schedule, owner and producer before the List write."""
    schedule = _checked_schedule(words)
    if isinstance(assignee, str):
        member, mapped = _assignment(assignee, members)
    else:
        member, mapped = assignee, _mapped_producer(assignee, members)
    if isinstance(assignee, str) or member.id != mapped.producer.id:
        try:
            _ = review_submissions.selected_recipient(mapped.producer.id, members)
        except new_item_dialog.SubmissionConflict as error:
            raise FilingRefused(FilingRefusal.UNAVAILABLE_PRODUCER, str(error)) from error
    keeper = the_list.producer_id(members, "morty")
    run = schedule.first if isinstance(schedule, the_list.ScheduledSeries) else schedule
    if run.start.astimezone(UTC) <= the_list.now():
        raise FilingRefused(FilingRefusal.INVALID_SCHEDULE, "The start must be in the future.")
    assignment = assignment_receipt(new_item_dialog.SCHEDULED_KIND, member, mapped, members, this_session=False)
    return ScheduledFiling(keeper, member, mapped.producer.id, schedule, assignment, schedule_lines(schedule))


def scheduled_notice(item: the_list.ListItem, keeper: staff.StaffId, members: staff.StaffSnapshot) -> str:
    """Send the assignment notices after filing the item."""
    member = members.by_id(staff.StaffId(item.owner))
    if member is None:
        return "The owner is unavailable. The List item is saved."
    return _assignment_warning(item, keeper, member, members)


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
        # The List fields are written on natedev, so the Mac sends the whole command there with its session.
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
        member, mapped = _assignment(args.assignee, members)
        address = console_address()
        options = read_options(address)
        recipient = recipient_for(member, options, members)
        if isinstance(recipient, Refusal):
            print(f"new_item: {recipient.reason}", file=sys.stderr)
            return Outcome.REFUSED
        reply = exchange(address, "POST", review_page.NEW_ITEM_PATH, request_body(
            options, FILED_KINDS[item_type], recipient, words_text(item_type, args.description, args.extra)))
        list_key = reply.fields.get("list_key")
        if reply.status != 200 or not isinstance(list_key, str):
            print(f"new_item: {_server_error(reply)}", file=sys.stderr)
            return Outcome.FAILED
    except FilingRefused as error:
        if error.reason in (FilingRefusal.INVALID_SCHEDULE, FilingRefusal.UNAVAILABLE_PRODUCER):
            print(f"new_item: {error}", file=sys.stderr)
            return Outcome.REFUSED
        print(error)
        return Outcome.NO_PRODUCER if error.reason is FilingRefusal.UNMAPPED_PRODUCER else Outcome.UNKNOWN_NAME
    except (ServerUnavailable, OSError, ValueError, staff.StaffRefused) as error:
        print(f"new_item: {error}", file=sys.stderr)
        return Outcome.FAILED
    caller = members.by_current_session(args.session) if args.session else None
    filed = result_line(item_type, member, mapped, members,
                        this_session=caller is not None and caller.id == member.id, list_key=list_key)
    if member.id != recipient.id:
        try:
            set_assignee(list_key, member, mapped)
        except (OSError, ValueError, staff.StaffRefused) as error:
            print(f"new_item: filed, but the List item does not name the assignee and its producer: {filed} {error}",
                  file=sys.stderr)
            return Outcome.FAILED
    print(filed)
    return Outcome.FILED


if __name__ == "__main__":
    raise SystemExit(main())
