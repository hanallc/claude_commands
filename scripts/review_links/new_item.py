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
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import IntEnum, StrEnum
from http.client import HTTPConnection, HTTPException
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.lib import conf, main_publish
from scripts.lib.user_zone import user_zone
from scripts.production import investigation, producer_map, repeat_rule, staff, the_list
from scripts.review_links import for_you, new_item_dialog, review_page

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
    SCHEDULED = "scheduled"


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


_UTC_OFFSET = r"Z|[+-][0-9]{2}:[0-5][0-9]"
_ZONE_NAME = r"[a-z0-9_+/-]+"
_START_TIME = re.compile(
    r"(?:(?P<iso>[0-9]{4}-[0-9]{2}-[0-9]{2})[T ]|"
    + r"(?P<weekday>[a-z]{3})\s+(?P<day>[a-z]{3}\s+[0-9]{1,2})(?:\s+(?P<year>[0-9]{4}))?\s+)"
    + rf"(?P<clock>[0-9]{{2}}:[0-5][0-9])\s*(?P<zone>{_UTC_OFFSET}|\[{_ZONE_NAME}\]|{_ZONE_NAME})?",
    re.IGNORECASE,
)
_WEEKDAYS = {day.name.lower(): day for day in repeat_rule.Weekday}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
_MENU_FREQUENCIES = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "annually": "YEARLY"}
_CUSTOM_FREQUENCIES = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}
_REPEAT_MENU_ERROR = "Use a repeat from the List menu or a Custom repeat."


def _start_time(text: str, *, now: datetime, zone: ZoneInfo) -> datetime:
    match = _START_TIME.fullmatch(text)
    if match is None:
        raise ValueError("Use a start time such as Fri Nov 27 2026 13:00 or 2026-11-27T13:00.")
    name = match["zone"]
    if not name:
        start_zone = zone
    elif re.fullmatch(_UTC_OFFSET, name, re.IGNORECASE):
        start_zone = time.fromisoformat("00:00" + name.upper()).tzinfo
    else:
        try:
            start_zone = ZoneInfo(name.removeprefix("[").removesuffix("]"))
        except ZoneInfoNotFoundError:
            raise ValueError("Use an IANA time zone such as America/Los_Angeles or a UTC offset.") from None
    if match["iso"]:
        day = match["iso"]
    else:
        year = match["year"] or str(now.astimezone(start_zone).year)
        dated = datetime.strptime(f"{match['day']} {year}", "%b %d %Y")
        if dated.strftime("%a").lower() != match["weekday"].lower():
            raise ValueError(f"The start date falls on {dated:%A}.")
        day = dated.date().isoformat()
    start = datetime.fromisoformat(f"{day}T{match['clock']}").replace(tzinfo=start_zone)
    instant = start.astimezone(UTC)
    if instant.astimezone(start.tzinfo).replace(tzinfo=None) != start.replace(tzinfo=None):
        raise ValueError("The start time does not exist in its time zone.")
    if instant <= now.astimezone(UTC):
        raise ValueError("The start must be in the future.")
    return start


def _repeat_end(text: str, start: datetime) -> tuple[str, str]:
    rule, _, end = re.sub(r"[.,;]?\s+ends\s+", " ends ", text).partition(" ends ")
    if not end:
        match = re.fullmatch(r"(.+?)\s+(never|until .+|after .+|for .+)", rule)
        if match is not None:
            rule, end = match[1], match[2]
    if not end or end == "never":
        return rule, ""
    count = re.fullmatch(r"(?:after|for) ([0-9]+) (?:occurrences?|runs?)", end)
    if count is not None:
        return rule, f";COUNT={count[1]}"
    until = re.fullmatch(r"(?:on|until) (.+)", end)
    if until is not None:
        for form in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
            try:
                day = datetime.strptime(until[1], form).date()
            except ValueError:
                continue
            last = datetime.combine(day, time(23, 59, 59), start.tzinfo).astimezone(UTC)
            return rule, f";UNTIL={last:%Y%m%dT%H%M%SZ}"
        raise ValueError("Use an end date such as Mar 1, 2027 or 2027-03-01.")
    raise ValueError("End a repeat with Never, On a date, or After a number of occurrences.")


def _monthly_days(selector: str, start: datetime) -> str:
    if selector in ("same day number", "the same day number"):
        return f";BYMONTHDAY={start.day}"
    day = re.fullmatch(r"day ([0-9]{1,2})", selector)
    if day is not None:
        return f";BYMONTHDAY={day[1]}"
    if selector.removesuffix(" of the month") in ("same weekday", "the same weekday"):
        weekday = tuple(repeat_rule.Weekday)[start.weekday()]
        last_day = calendar.monthrange(start.year, start.month)[1]
        ordinal = -1 if start.day + 7 > last_day else (start.day - 1) // 7 + 1
        return f";BYDAY={ordinal}{weekday}"
    weekday_of_month = re.fullmatch(r"the (first|second|third|fourth|last) ([a-z]+)", selector)
    if weekday_of_month is not None and (weekday := _WEEKDAYS.get(weekday_of_month[2])) is not None:
        return f";BYDAY={_ORDINALS[weekday_of_month[1]]}{weekday}"
    raise ValueError(_REPEAT_MENU_ERROR)


def _repeat_days(frequency: str, selector: str, start: datetime) -> str:
    if not selector:
        return ""
    if frequency == "WEEKLY":
        names = re.split(r",\s*(?:and )?|\s+and\s+", selector)
        if all(name in _WEEKDAYS for name in names):
            return ";BYDAY=" + ",".join(_WEEKDAYS[name] for name in names)
    if frequency == "MONTHLY":
        return _monthly_days(selector, start)
    if frequency == "YEARLY" and selector in (start.strftime("%B %-d").lower(), start.strftime("%b %-d").lower()):
        return ""
    raise ValueError(_REPEAT_MENU_ERROR)


def _repeat(text: str, start: datetime) -> repeat_rule.RepeatRule | the_list.NoSchedule:
    text = " ".join(text.lower().split()).removeprefix("repeat ")
    if text in ("", "does not repeat"):
        return the_list.NoSchedule.UNSET
    rule, end = _repeat_end(text, start)
    if rule in ("every weekday", "every weekday (monday to friday)"):
        fields = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    else:
        cadence, _, selector = rule.partition(" on ")
        frequency = _MENU_FREQUENCIES.get(cadence)
        interval = ""
        if frequency is None:
            custom = re.fullmatch(r"every (?:([0-9]+) )?(day|week|month|year)s?", cadence)
            if custom is None:
                raise ValueError(_REPEAT_MENU_ERROR)
            frequency = _CUSTOM_FREQUENCIES[custom[2]]
            if custom[1]:
                interval = f";INTERVAL={custom[1]}"
        fields = f"FREQ={frequency}{interval}{_repeat_days(frequency, selector, start)}"
    result = repeat_rule.parse(fields + end)
    result.validate_start(start)
    return result


def read_schedule(text: str, *, now: datetime, zone: ZoneInfo) -> the_list.ScheduledRun | the_list.ScheduledSeries:
    """Read the instruction, start and repeat from the user's words."""
    parts = re.fullmatch(r"(.+)\s+Start\s+(.+)", text.strip(), re.IGNORECASE | re.DOTALL)
    if parts is None or not parts[1].strip():
        raise ValueError("Give the instruction followed by Start and a date and time.")
    start_words, _, repeat_words = parts[2].partition(".")
    start = _start_time(start_words.strip(), now=now, zone=zone)
    run = the_list.ScheduledRun(start, parts[1].strip())
    rule = _repeat(repeat_words.strip().removesuffix(".").rstrip(), start)
    return the_list.ScheduledSeries(run, rule) if isinstance(rule, repeat_rule.RepeatRule) else run


def _say_schedule(schedule: the_list.ScheduledRun | the_list.ScheduledSeries) -> None:
    start = schedule.first.start if isinstance(schedule, the_list.ScheduledSeries) else schedule.start
    print(f"Starts: {start:%a %b %-d %Y %H:%M %Z}", flush=True)
    if isinstance(schedule, the_list.ScheduledSeries):
        print(f"Repeats: {schedule.rule.describe(start)}", flush=True)
    else:
        print("Does not repeat", flush=True)


def usage() -> str:
    return "\n".join((
        FORM,
        f"Types: {', '.join(ItemType)}",
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


def result_line(item_type: ItemType, member: staff.StaffRow, mapped: producer_map.MappedProducer,
                members: staff.StaffSnapshot, *, this_session: bool, list_key: str) -> str:
    """Say who does the work, who tracks it and why, and the List item number."""
    to = unit_of(member, members) + (", this session" if this_session else "")
    producer = ""
    if member.kind != "producer":
        why = "set for" if mapped.set_by == member else "from"
        producer = f" Producer: {mapped.producer.name} ({why} {mapped.set_by.name})."
    number = list_key.partition("/")[2].removeprefix("L")
    return f"{item_type.value.capitalize()} → {to}.{producer} List item {number}."


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
    _ = parser.add_argument("item_type", type=str.lower, choices=[kind.value for kind in ItemType])
    _ = parser.add_argument("assignee")
    _ = parser.add_argument("description")
    _ = parser.add_argument("extra", nargs="?", default="")
    _ = parser.add_argument("--session", default=staff.caller_identity(), help=argparse.SUPPRESS)
    _ = parser.add_argument("--preview", action="store_true", help="read a Scheduled start and repeat without saving")
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.description.strip():
        parser.error("the description is empty")
    item_type = ItemType(args.item_type)
    schedule: the_list.Schedule = the_list.NoSchedule.UNSET
    if item_type is ItemType.SCHEDULED:
        try:
            schedule = read_schedule(f"{args.description} {args.extra}", now=the_list.now(), zone=user_zone())
        except (OSError, ValueError, OverflowError) as error:
            print(f"new_item: {error}", file=sys.stderr)
            return Outcome.REFUSED
        if args.preview:
            _say_schedule(schedule)
            return 0
    elif args.preview:
        parser.error("--preview requires the scheduled type")
    try:
        members = staff.snapshot()
        member = producer_map.find_member(args.assignee, members, producer_map.ASSIGNEE_KINDS)
        if isinstance(member, producer_map.UnknownMember):
            print(unknown_name(member, members))
            return Outcome.UNKNOWN_NAME
        mapped = producer_map.producer_for(member.id, members)
        if isinstance(mapped, producer_map.NoProducer):
            print(producer_question(member, mapped, members))
            return Outcome.NO_PRODUCER
        if not isinstance(schedule, the_list.NoSchedule):
            keeper = the_list.producer_id(members, "morty")
            run = schedule.first if isinstance(schedule, the_list.ScheduledSeries) else schedule
            if run.start.astimezone(UTC) <= the_list.now():
                print("new_item: The start must be in the future.", file=sys.stderr)
                return Outcome.REFUSED
            _say_schedule(schedule)
            item = the_list.add(keeper, " ".join(run.words.split()), owner=member.id, source=SOURCE, changes={
                "state": the_list.ItemState.SCHEDULED, "schedule": schedule, "producer": mapped.producer.id,
                "unit": member.name if member.kind == "unit" else "",
            })
            print(result_line(item_type, member, mapped, members, this_session=False, list_key=f"{keeper}/{item.id}"))
            return Outcome.FILED
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
