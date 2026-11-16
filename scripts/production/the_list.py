"""Keep The List as numbered records under the producer's fixed staff ID."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict, cast

from scripts.lib.user_zone import user_zone
from scripts.production import record_store, staff


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


Eta = datetime | EtaRange | EtaStatus


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
class ListItem:
    number: int
    title: str
    owner: staff.StaffId
    unit: str
    state: ItemState
    next_check: Timestamp
    eta: Eta
    unread_since: Timestamp
    parked_until: Timestamp
    blocked_by: tuple[ItemDependency, ...]
    related_to: tuple[int, ...]
    requested_by: RequestedBy
    opened: datetime
    updated: datetime
    closed: Timestamp
    closing_words: str
    next_step: str
    latest_status: str
    evidence: str
    done_when: str
    log: tuple[ItemChange, ...]

    @property
    def id(self) -> str:
        return f"L{self.number}"


class ItemChanges(TypedDict, total=False):
    title: str
    owner: staff.StaffId
    unit: str
    state: ItemState
    next_check: Timestamp
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


def eta_value(text: str) -> Eta:
    if text in EtaStatus:
        return EtaStatus(text)
    if "/" in text:
        start, end = text.split("/", 1)
        value = EtaRange(_required_time(start), _required_time(end))
        if value.earliest > value.latest:
            raise ListError("the ETA range ends before it starts")
        return value
    return _required_time(text)


def eta_text(value: Eta) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, EtaRange):
        return f"{value.earliest.isoformat()}/{value.latest.isoformat()}"
    return value.value


def _line(value: str, name: str) -> None:
    if not value.strip() or value.splitlines() != [value]:
        raise ListError(f"{name} must have one nonempty line")


def _validate(item: ListItem) -> None:
    if item.number < 1:
        raise ListError("number must be positive")
    _line(item.title, "title")
    if not item.owner or Path(item.owner).name != item.owner or item.owner in (".", ".."):
        raise ListError("invalid owner staff id")
    if item.unit and item.unit.splitlines() != [item.unit]:
        raise ListError("unit must have one line")
    for moment in (item.opened, item.updated, item.next_check, item.unread_since, item.parked_until, item.closed):
        if isinstance(moment, datetime) and moment.utcoffset() is None:
            raise ListError("a time must have a time zone")
    _ = eta_value(eta_text(item.eta))
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
    for change in item.log:
        _line(change.what, "log text")
        _line(change.source, "source")
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


def load(keeper: staff.StaffId, identity: str, *, sources: list[Path] | None = None) -> ListItem:
    store = _store(keeper)
    fields, body = store.read(identity, sources=sources)
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
        changes: list[ItemChange] = []
        for line in body.splitlines():
            dated, source = line.removeprefix("- ").rsplit(" — ", 1)
            date, clock, what = dated.split(" ", 2)
            if not line.startswith("- "):
                raise ListError("invalid log line")
            changes.append(ItemChange(_required_time(date + "T" + clock), what, source))
        item = ListItem(int(fields["number"]), fields["title"], staff.StaffId(fields["owner"]),
                        fields["unit"], ItemState(fields["state"]), time_value(fields["next_check"]),
                        eta_value(fields["eta"]), time_value(fields["unread_since"]), time_value(fields["parked_until"]),
                        _dependencies(fields["blocked_by"]), _related(fields["related_to"]), RequestedBy(fields["requested_by"]),
                        _required_time(fields["opened"]), _required_time(fields["updated"]), time_value(fields["closed"]),
                        fields["closing_words"], texts[0], texts[1], texts[2], texts[3], tuple(changes))
        if item.id != identity or not changes:
            raise ListError("the item number must match its path and the log must not be empty")
        _validate(item)
        return item
    except (ValueError, KeyError, TypeError) as error:
        raise store.invalid(identity, error) from None


def _save(store: record_store.NumberedRecords, item: ListItem) -> None:
    _validate(item)
    fields = {"id": item.id, "number": str(item.number), "title": item.title,
              "owner": item.owner, "unit": item.unit, "state": item.state.value,
              "next_check": _time_text(item.next_check), "eta": eta_text(item.eta),
              "unread_since": _time_text(item.unread_since), "parked_until": _time_text(item.parked_until),
              "blocked_by": json.dumps([(entry.number, entry.condition) for entry in item.blocked_by], ensure_ascii=False),
              "related_to": json.dumps(item.related_to), "requested_by": item.requested_by.value,
              "opened": item.opened.isoformat(), "updated": item.updated.isoformat(),
              "closed": _time_text(item.closed), "closing_words": item.closing_words}
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


def _check_links(keeper: staff.StaffId, number: int, changes: ItemChanges) -> None:
    for related in changes.get("related_to", ()):
        _ = load(keeper, f"L{related}")
    visited: set[int] = set()

    def visit(target: int) -> None:
        if target == number:
            raise ListError("dependencies must not form a cycle")
        if target in visited:
            return
        visited.add(target)
        for dependency in load(keeper, f"L{target}").blocked_by:
            visit(dependency.number)

    for dependency in changes.get("blocked_by", ()):
        visit(dependency.number)


def _change_text(changes: ItemChanges) -> str:
    return "; ".join(f"{key}: {json.dumps(str(value), ensure_ascii=False)}" for key, value in changes.items())


def add(keeper: staff.StaffId, title: str, *, owner: staff.StaffId, source: str,
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
        item = ListItem(chosen, title, owner, "", ItemState.NEW, NoTimestamp.UNSET, EtaStatus.NOT_ASKED,
                        moment, NoTimestamp.UNSET, (), (), requested_by, moment, moment, NoTimestamp.UNSET, "",
                        "", "", "", "", ())
        if changes:
            item = replace(item, **changes)
        initial: ItemChanges = {"title": item.title, "owner": item.owner, "state": item.state, "eta": item.eta,
                                "next_step": item.next_step, "latest_status": item.latest_status}
        initial.update(changes)
        item = replace(item, log=(ItemChange(moment, "Added; " + _change_text(initial), source),))
        _validate(item)
        _check_links(keeper, item.number, changes)
        _save(store, item)
        return item


def _change(keeper: staff.StaffId, identity: str, edit: Callable[[ListItem, datetime], ListItem],
            what: str, source: str, *, unread: bool = True) -> ListItem:
    store = _store(keeper)
    _ = store.path(identity)
    with store.locked():
        item = load(keeper, identity)
        moment = now()
        changed = edit(item, moment)
        if changed == item:
            return item
        changed = replace(changed, updated=moment, unread_since=moment if unread else NoTimestamp.UNSET,
                          log=(*item.log, ItemChange(moment, what, source)))
        _validate(changed)
        _save(store, changed)
        return changed


def _open(item: ListItem) -> None:
    if item.state == ItemState.CLOSED:
        raise ListError(f"{item.id}: reopen the Closed item before changing it")


def update(keeper: staff.StaffId, identity: str, changes: ItemChanges, *, source: str) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        if changes.get("state") == ItemState.CLOSED:
            raise ListError("use close to record closing words and a time")
        changed = replace(item, **changes)
        _validate(changed)
        _check_links(keeper, changed.number, changes)
        return changed
    what = "Updated " + _change_text(changes)
    return _change(keeper, identity, edit, what, source)


def park(keeper: staff.StaffId, identity: str, until: Timestamp, *, source: str) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        _open(item)
        return replace(item, parked_until=until)
    return _change(keeper, identity, edit, "Parked until " + (_time_text(until) or "not parked"), source)


def close(keeper: staff.StaffId, identity: str, words: str, *, source: str) -> ListItem:
    if not words.strip():
        raise ListError("closing words must not be empty")
    def edit(item: ListItem, moment: datetime) -> ListItem:
        if item.state == ItemState.CLOSED:
            if item.closing_words == words:
                return item
            raise ListError("the item is already Closed with different closing words")
        return replace(item, state=ItemState.CLOSED, closed=moment, closing_words=words)
    return _change(keeper, identity, edit, "Closed: " + " ".join(words.split()), source)


def reopen(keeper: staff.StaffId, identity: str, *, source: str) -> ListItem:
    def edit(item: ListItem, _: datetime) -> ListItem:
        if item.state != ItemState.CLOSED:
            return item
        return replace(item, state=ItemState.NEW, closed=NoTimestamp.UNSET, closing_words="", parked_until=NoTimestamp.UNSET)
    return _change(keeper, identity, edit, "Reopened", source)


def mark_read(keeper: staff.StaffId, identity: str, *, source: str) -> ListItem:
    return _change(keeper, identity, lambda item, _: replace(item, unread_since=NoTimestamp.UNSET),
                   "Marked read", source, unread=False)


def table(keeper: staff.StaffId, *, include_closed: bool = False, include_parked: bool = False) -> str:
    moment = now()
    items = records(keeper, include_closed=include_closed)
    items = [item for item in items if include_parked or not isinstance(item.parked_until, datetime) or item.parked_until <= moment]
    items.sort(key=lambda item: (item.state != ItemState.WAITING_FOR_NATE, not isinstance(item.unread_since, datetime),
                                item.next_check.timestamp() if isinstance(item.next_check, datetime) else float("inf"), item.number))
    record = staff.snapshot()
    lines = ["| # | Owner | Item | State | Next step | ETA | Update |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in items:
        owner = record.by_id(item.owner)
        state = item.state.value
        if isinstance(item.next_check, datetime):
            state += "; " + ("check overdue " if item.next_check < moment else "check ") + item.next_check.isoformat()
        if isinstance(item.parked_until, datetime) and item.parked_until > moment:
            state += "; parked until " + item.parked_until.isoformat()
        cells = (str(item.number), owner.name if owner is not None else item.owner, item.title, state,
                 item.next_step, eta_text(item.eta), "●" if isinstance(item.unread_since, datetime) else "—")
        lines.append("| " + " | ".join(" ".join(cell.split()).replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines)


def _cli_changes(args: argparse.Namespace) -> ItemChanges:
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
        owner = staff.snapshot().by_id(staff.StaffId(owner_id))
        if owner is None or owner.kind != "showrunner":
            raise ListError("owner must name a showrunner staff id")
        changes["owner"] = owner.id
    state, next_check, eta, requested = (options.get(key) for key in ("state", "next_check", "eta", "requested_by"))
    if isinstance(state, str):
        changes["state"] = ItemState(state)
    if isinstance(next_check, str):
        changes["next_check"] = time_value(next_check)
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
    for name in ("add", "update", "park", "close", "reopen", "mark-read", "list"):
        command = commands.add_parser(name)
        if name != "list":
            _ = command.add_argument("--source", required=True)
        if name not in ("add", "list"):
            _ = command.add_argument("--id", required=True)
        if name in ("add", "update"):
            _ = command.add_argument("--title", required=name == "add")
            _ = command.add_argument("--owner", required=name == "add")
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
    try:
        record = staff.snapshot()
        caller = record.by_current_session(staff.caller_identity())
        if caller is None:
            raise ListError("the current session has no staff row")
        if caller.kind != "producer" or caller.name != "morty":
            raise ListError("the current session must be the morty producer")
        keeper = caller.id
        name = cast(str, args.command)
        if name == "list":
            print(table(keeper, include_closed=cast(bool, args.all),
                        include_parked=cast(bool, args.all) or cast(bool, args.include_parked)))
            return 0
        source = cast(str, args.source)
        if name == "add":
            changes = _cli_changes(args)
            number = cast(dict[str, object], vars(args)).get("number")
            owner = changes.get("owner")
            if owner is None:
                raise ListError("owner must name a showrunner staff id")
            print(add(keeper, cast(str, args.title), owner=owner, source=source,
                      number=number if isinstance(number, int) else NextNumber.AVAILABLE, changes=changes).id)
        elif name == "update":
            _ = update(keeper, cast(str, args.id), _cli_changes(args), source=source)
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
