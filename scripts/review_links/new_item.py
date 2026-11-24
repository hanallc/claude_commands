"""File a New item from a session through the console server, as the New item window does."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Literal, cast

from scripts.lib import atomic_file, conf, main_publish
from scripts.lib.file_lock import exclusive
from scripts.production import investigation, producer_map, staff
from scripts.review_links import for_you, new_item_dialog, review_page

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


def request_body(options: Options, item_type: ItemType, recipient: staff.StaffId, words: str,
                 assignee: staff.StaffId, request_id: str) -> bytes:
    """Send the List window fields for a new List draft."""
    return json.dumps({
        "token": options.token, "request_id": request_id, "kind": FILED_KINDS[item_type],
        "notice": {"kind": item_type, "from_command": True},
        "recipient_id": recipient, "words": words, "evidence": {"kind": "none"}, "images": [],
        "page": "list", "page_recipient": new_item_dialog.PageRecipient.SELECTED.value, "list_assignee": assignee,
    }).encode()


@dataclass(frozen=True)
class PendingListDraft:
    """Keep the request ID and recipient while the result is uncertain."""

    request_id: str
    recipient: staff.StaffId


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
    if (len(fields) != 2 or not re.fullmatch(r"r6-[0-9a-f]{32}", fields[0])
            or not fields[1]):
        raise OSError("Cannot read the pending New item draft.")
    return PendingListDraft(fields[0], staff.StaffId(fields[1]))


def save_pending_draft(path: Path, recipient: staff.StaffId) -> PendingListDraft:
    draft = PendingListDraft("r6-" + secrets.token_hex(16), recipient)
    atomic_file.write_text_atomic(path, f"{draft.request_id}\n{draft.recipient}\n", new_file_mode=0o600)
    return draft


def post_item(address: for_you.ConsoleAddress, options: Options, item_type: ItemType,
              draft: PendingListDraft, words: str, assignee: staff.StaffId) -> ServerReply:
    """Retry an unconfirmed POST with the same draft ID and recipient."""
    def send(current: Options) -> ServerReply:
        return exchange(address, "POST", review_page.NEW_ITEM_PATH,
                        request_body(current, item_type, draft.recipient, words, assignee, draft.request_id))

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
        # The Mac uses the natedev staff snapshot and console address.
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
                recipient = recipient_for(member, options, members)
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
    except (ServerUnavailable, OSError, ValueError, staff.StaffRefused) as error:
        print(f"new_item: {error}", file=sys.stderr)
        return Outcome.FAILED
    number = reply.fields.get("list_number")
    if type(number) is int and number > 0:
        filed = f"List item {number}."
    else:
        review_id = reply.fields.get("id")
        shown = review_id if isinstance(review_id, str) and review_id else "a Review item"
        filed = f"Filed as {shown}. The console server returned no List number."
    print(f"{result} {filed}")
    return Outcome.FILED


if __name__ == "__main__":
    raise SystemExit(main())
