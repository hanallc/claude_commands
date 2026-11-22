"""File a New item from a session through the console server, as the New item window does."""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import subprocess
import sys
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from typing import Literal, cast

from scripts.lib import conf, main_publish
from scripts.production import investigation, producer_map, staff, the_list
from scripts.review_links import for_you, new_item_dialog, review_page

FEEDBACK = "feedback"
LABELS = {str(kind): label for kind, label, _ in new_item_dialog.KINDS}
SOURCE = "nate via /new_item"
OPTIONS_TARGET = f"{review_page.NEW_ITEM_PATH}?page=list"
FORM = "/new_item <type> <assignee> <description> [<extra info>]"
MAY_BE_FILED = "The item may be filed. Check the List before a retry."


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
class Assignee:
    """The member that nate named and the To row recipient that receives the item."""

    member: staff.StaffRow
    recipient: Recipient


@dataclass(frozen=True)
class Refusal:
    reason: str


def filed_kind(item_type: str) -> new_item_dialog.SubmissionKind:
    """Feedback files as an investigation, as the New item window does."""
    return new_item_dialog.SubmissionKind.INVESTIGATION if item_type == FEEDBACK else new_item_dialog.SubmissionKind(item_type)


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


def _names(rows: tuple[Recipient, ...] | tuple[staff.StaffRow, ...]) -> str:
    return ", ".join(row.name for row in rows) or "none"


def resolve(reference: str, options: Options, members: staff.StaffSnapshot) -> Assignee | Refusal:
    """A To row name or id files to that recipient; a unit files to its first recipient up the owner chain."""
    folded = reference.casefold()
    listed = [row for row in options.recipients if row.id == reference.lower() or row.name.casefold() == folded]
    if len(listed) > 1:
        return Refusal(f"{reference}: more than one To row match; use a staff id: "
                       + ", ".join(f"{row.name} {row.id}" for row in listed))
    member = members.by_id(listed[0].id) if listed else None
    if listed and member is not None:
        return Assignee(member, listed[0])
    unit = producer_map.find_member(reference, members, ("unit",))
    if isinstance(unit, staff.StaffRow):
        by_id = {row.id: row for row in options.recipients}
        recipient = next((by_id[identity] for identity in investigation.owner_chain(unit.id, members)
                          if identity in by_id), None)
        if recipient is None:
            return Refusal(f"{unit.name}: no showrunner or producer above this unit is in the To row: "
                           + _names(options.recipients))
        return Assignee(unit, recipient)
    if unit.matches:
        return Refusal(unit.reason(("unit",)))
    return Refusal(f"{reference}: not in the To row and not an active unit. To row: {_names(options.recipients)}. "
                   + f"Active units: {_names(staff.active(members, 'unit'))}.")


def request_body(options: Options, kind: new_item_dialog.SubmissionKind, recipient: Recipient, words: str) -> bytes:
    """Send the List window fields for a new List draft."""
    return json.dumps({
        "token": options.token, "request_id": "r6-" + secrets.token_hex(16), "kind": kind.value,
        "recipient_id": recipient.id, "words": words, "evidence": {"kind": "none"}, "images": [],
        "page": "list", "page_recipient": new_item_dialog.PageRecipient.SELECTED.value,
    }).encode()


def words_text(description: str, extra: str) -> str:
    return description.strip() + (f"\n\n{extra.strip()}" if extra.strip() else "")


def set_unit(list_key: str, unit: staff.StaffRow, members: staff.StaffSnapshot) -> None:
    """Name the unit on the List item; a unit producer row overrides the producer from the recipient."""
    keeper, _, identity = list_key.partition("/")
    item = the_list.load(staff.StaffId(keeper), identity)
    changes: the_list.ItemChanges = {"unit": unit.name}
    producer = producer_map.producer_for(unit.id, members)
    if isinstance(producer, staff.StaffRow) and producer.id != item.producer:
        changes["producer"] = producer.id
    _ = the_list.update(staff.StaffId(keeper), identity, changes, source=SOURCE)


def usage() -> str:
    try:
        recipients = _names(read_options(console_address()).recipients)
    except (ServerUnavailable, ValueError) as error:
        recipients = f"unavailable: {error}"
    return "\n".join((
        f"Usage: {FORM}",
        f"Types: {', '.join(LABELS)}. Feedback files as an investigation.",
        f"Assignee: a To row name or staff id. To row: {recipients}.",
        "A unit name is allowed. The item goes to the unit's showrunner, and the List item names the unit.",
        "Extra info, such as timing instructions, follows the description after a blank line.",
    ))


class Arguments(argparse.Namespace):
    item_type: str = ""
    assignee: str = ""
    description: str = ""
    extra: str = ""


def main(arguments: list[str] | None = None) -> int:
    argv = sys.argv[1:] if arguments is None else arguments
    if platform.system() == "Darwin":
        # The List unit field is written on natedev, so the Mac sends the whole command there.
        return main_publish.forward("review_links/new_item.py", argv, label="new_item",
                                    ssh_program=os.environ.get("ZONE_SSH") or "ssh")
    if not argv:
        print(usage())
        return 0
    parser = argparse.ArgumentParser(prog="new_item.py", description=__doc__)
    _ = parser.add_argument("item_type", type=str.lower, choices=list(LABELS))
    _ = parser.add_argument("assignee")
    _ = parser.add_argument("description")
    _ = parser.add_argument("extra", nargs="?", default="")
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.description.strip():
        parser.error("the description is empty")
    try:
        address = console_address()
        options = read_options(address)
        members = staff.snapshot()
        assignee = resolve(args.assignee, options, members)
        if isinstance(assignee, Refusal):
            print(f"new_item: {assignee.reason}", file=sys.stderr)
            return 2
        member = assignee.member
        if member.kind != "producer" and isinstance(producer_map.producer_for(member.id, members), producer_map.NoProducer):
            mapped = producer_map.map_member(member, members)
            print(f"{member.name} has no producer. Pass {mapped.id} ({members.qualified_name(mapped)}) to producer_map.py set. "
                  + f"Active producers: {_names(staff.active(members, 'producer'))}.")
            return 3
        reply = exchange(address, "POST", review_page.NEW_ITEM_PATH, request_body(
            options, filed_kind(args.item_type), assignee.recipient, words_text(args.description, args.extra)))
        item_id, list_key = reply.fields.get("id"), reply.fields.get("list_key")
        if reply.status != 200 or not isinstance(item_id, str) or not isinstance(list_key, str):
            print(f"new_item: {_server_error(reply)}", file=sys.stderr)
            return 1
    except (ServerUnavailable, OSError, ValueError, staff.StaffRefused) as error:
        print(f"new_item: {error}", file=sys.stderr)
        return 1
    filed = f"{LABELS[args.item_type]} {item_id} to {member.name}; List {list_key}"
    if member.kind == "unit":
        try:
            set_unit(list_key, member, members)
        except (OSError, ValueError, staff.StaffRefused) as error:
            print(f"new_item: filed {filed}, but the List unit field was not set: {error}", file=sys.stderr)
            return 1
    print(filed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
