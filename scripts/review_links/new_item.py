"""File a New item from a session through the console server, as the New item window does."""
from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import secrets
import subprocess
import sys
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from http.client import HTTPConnection, HTTPException
from typing import Literal, cast
from urllib.parse import urlencode

from scripts.lib import conf, main_publish
from scripts.production import investigation, producer_map, staff, the_list
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
    ENHANCEMENT = "enhancement"


type _WireKind = new_item_dialog.SubmissionKind | Literal["enhancement"]


# The server files Feedback, Efficiency and Enhancement as investigations.
FILED_KINDS: dict[ItemType, _WireKind] = {
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


class UnlinkedListItem(Exception):
    """The List item exists, but its link to the Review item failed."""

    def __init__(self, number: int, cause: Exception) -> None:
        super().__init__(f"List item {number} exists without its Review link: {cause}")
        self.number: int = number


def usage() -> str:
    return "\n".join((
        FORM,
        f"Types: {', '.join(ItemType)}",
        "Assignee: a producer, a showrunner or a unit",
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
                members: staff.StaffSnapshot, *, this_session: bool, filed: str) -> str:
    """Say who does the work, who tracks it and why, and where the item is filed."""
    to = unit_of(member, members) + (", this session" if this_session else "")
    producer = ""
    if member.kind != "producer":
        why = "set for" if mapped.set_by == member else "from"
        producer = f" Producer: {mapped.producer.name} ({why} {mapped.set_by.name})."
    return f"{item_type.value.capitalize()} → {to}.{producer} {filed}"


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


def request_body(options: Options, kind: _WireKind, recipient: Recipient, words: str) -> bytes:
    """Send the List window fields for a new List draft."""
    return json.dumps({
        "token": options.token, "request_id": "r6-" + secrets.token_hex(16), "kind": kind,
        "recipient_id": recipient.id, "words": words, "evidence": {"kind": "none"}, "images": [],
        "page": "list", "page_recipient": new_item_dialog.PageRecipient.SELECTED.value,
    }).encode()


def words_text(item_type: ItemType, description: str, extra: str) -> str:
    """Add the Efficiency prefix and append any extra words."""
    first = f"Efficiency: {description.strip()}" if item_type == ItemType.EFFICIENCY else description.strip()
    return first + (f"\n\n{extra.strip()}" if extra.strip() else "")


def add_list_item(review_key: str, words: str, recipient: Recipient, member: staff.StaffRow,
                  mapped: producer_map.MappedProducer, members: staff.StaffSnapshot) -> the_list.ListItem:
    """Make the List item of a filed Review item in morty's folder, which alone allocates List numbers.

    Each run sends a new request ID, so the server never gives one Review key to two runs.
    """
    keeper = the_list.producer_id(members, "morty")
    evidence = review_page.PAGE_PATH + "?show=all#" + urlencode({"show": "open", "jump": review_key})
    # The item names a unit assignee and takes the assignee's producer, not the recipient's.
    changes: the_list.ItemChanges = {"latest_status": words, "evidence": evidence, "producer": mapped.producer.id}
    if member.kind == "unit":
        changes["unit"] = member.name
    item = the_list.add(keeper, review_submissions.submission_title(words), owner=recipient.id, source=SOURCE,
                        changes=changes)
    try:
        return the_list.link(keeper, item.id, (review_key,), source=SOURCE)
    except Exception as error:
        raise UnlinkedListItem(item.number, error) from error


class Arguments(argparse.Namespace):
    item_type: str = ""
    assignee: str = ""
    description: str = ""
    extra: str = ""
    session: str = ""


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
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.description.strip():
        parser.error("the description is empty")
    item_type = ItemType(args.item_type)
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
        address = console_address()
        options = read_options(address)
        recipient = recipient_for(member, options, members)
        if isinstance(recipient, Refusal):
            print(f"new_item: {recipient.reason}", file=sys.stderr)
            return Outcome.REFUSED
        caller = members.by_current_session(args.session) if args.session else None
        this_session = caller is not None and caller.id == member.id
        words = words_text(item_type, args.description, args.extra)
        kind = FILED_KINDS[item_type]
        reply = exchange(address, "POST", review_page.NEW_ITEM_PATH,
                         request_body(options, kind, recipient, words))
        if reply.status != 200:
            print(f"new_item: {_server_error(reply)}", file=sys.stderr)
            return Outcome.FAILED
    except (ServerUnavailable, OSError, ValueError, staff.StaffRefused) as error:
        print(f"new_item: {error}", file=sys.stderr)
        return Outcome.FAILED
    # The server filed the item, so each path from here exits FILED: a second run would file it again.
    review_id, review_key = reply.fields.get("id"), reply.fields.get("key")
    try:
        if not isinstance(review_key, str) or not review_key:
            raise ValueError("the console server sent no Review item key")
        if kind == "enhancement":
            words = new_item_dialog.read_words(words, "Your words", kind=kind)
        item = add_list_item(review_key, words, recipient, member, mapped, members)
    except UnlinkedListItem as error:
        print(result_line(item_type, member, mapped, members, this_session=this_session, filed=str(error)))
        return Outcome.FILED
    except Exception as error:
        shown = review_id if isinstance(review_id, str) and review_id else "a Review item"
        print(result_line(item_type, member, mapped, members, this_session=this_session,
                          filed=f"Filed as {shown}, but no List item exists: {error}"))
        return Outcome.FILED
    print(result_line(item_type, member, mapped, members, this_session=this_session,
                      filed=f"List item {item.number}."))
    return Outcome.FILED


if __name__ == "__main__":
    raise SystemExit(main())
