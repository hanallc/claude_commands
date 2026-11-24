"""Find the producer that a showrunner or unit reports to, and save one map row through /tune."""
from __future__ import annotations

import argparse
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass

from scripts.lib import conf, tune
from scripts.lib.waiting_rows import NAMED_ROWS
from scripts.production import investigation, staff
from scripts.production.producer_map_rows import MAPPED_KINDS

STEM = NAMED_ROWS
ASSIGNEE_KINDS: tuple[staff.Kind, ...] = ("producer", *MAPPED_KINDS)


@dataclass(frozen=True)
class MappedProducer:
    """The producer, and the member whose row sets it: the member itself, an owner, or the producer."""

    producer: staff.StaffRow
    set_by: staff.StaffRow


@dataclass(frozen=True)
class NoRow:
    """No row and no active producer is on the owner chain."""


NO_ROW = NoRow()


@dataclass(frozen=True)
class StaleRow:
    """The nearest row names a producer that is not active; an answer replaces the row of its holder."""

    holder: staff.StaffRow
    producer_name: str

    def reason(self) -> str:
        return f"The row of {self.holder.name} names {self.producer_name}, who is not active."


NoProducer = NoRow | StaleRow


@dataclass(frozen=True)
class UnknownMember:
    """The reference names no active member, or more than one."""

    reference: str
    matches: tuple[str, ...]

    def reason(self, kinds: tuple[staff.Kind, ...]) -> str:
        if self.matches:
            return f"{self.reference}: more than one match: {', '.join(self.matches)}. Use a path or a staff id."
        return f"{self.reference}: no active {' or '.join(kinds)} has this name or staff id."


def _rows() -> dict[str, str]:
    return {key.lower(): value.lower() for key, value in conf.read_or_repository(STEM).values().items()}


def _mapped(identity: str, holder: staff.StaffRow, members: staff.StaffSnapshot) -> MappedProducer | NoProducer:
    row = members.by_id(staff.StaffId(identity))
    if row is not None and row in staff.active(members, "producer"):
        return MappedProducer(row, holder)
    if holder.kind == "producer":
        return NO_ROW
    return StaleRow(holder, row.name if row is not None else identity)


def producer_for(identity: staff.StaffId, members: staff.StaffSnapshot) -> MappedProducer | NoProducer:
    """Read the map at use; the nearest row or producer on the owner chain wins."""
    rows = _rows()
    for candidate in investigation.owner_chain(identity, members):
        row = members.by_id(candidate)
        if row is None:
            continue
        if row.kind == "producer":
            return _mapped(row.id, row, members)
        target = rows.get(candidate.lower())
        if target is not None:
            return _mapped(target, row, members)
    return NO_ROW


def showrunner_of(member: staff.StaffRow, members: staff.StaffSnapshot) -> staff.StaffRow:
    """Name a unit's own showrunner; any other member, or a unit with none, stands for itself."""
    if member.kind != "unit":
        return member
    showrunners = (row for identity in investigation.owner_chain(member.id, members)
                   if (row := members.by_id(identity)) is not None and row.kind == "showrunner")
    return next(showrunners, member)


def find_member(reference: str, members: staff.StaffSnapshot,
                kinds: tuple[staff.Kind, ...]) -> staff.StaffRow | UnknownMember:
    """Resolve a staff id, a staff path or a unique name among active members of these kinds."""
    rows = [row for kind in kinds for row in staff.active(members, kind)]
    by_id = next((row for row in rows if row.id == reference.lower()), None)
    if by_id is not None:
        return by_id
    by_path = members.by_path(reference) if "/" in reference else None
    if by_path is not None and by_path in rows:
        return by_path
    named = [row for row in rows if row.name.casefold() == reference.casefold()]
    if len(named) == 1:
        return named[0]
    return UnknownMember(reference, tuple(members.qualified_name(row) for row in named))


def mapped_member(assignee: staff.StaffRow, members: staff.StaffSnapshot, *, unit_only: bool) -> staff.StaffRow:
    """Name the member that an answer maps.

    A stale row is replaced in place. Else one answer maps a unit's showrunner, so its other units ask nothing.
    "For <unit> only" maps the unit.
    """
    if unit_only:
        return assignee
    found = producer_for(assignee.id, members)
    if isinstance(found, StaleRow):
        return found.holder
    return showrunner_of(assignee, members)


def saved_line(assignee: staff.StaffRow, mapped: staff.StaffRow, producer: staff.StaffRow,
               members: staff.StaffSnapshot) -> str:
    """Say what the saved row covers; a unit with its own row keeps its producer."""
    saved = f"Saved: {mapped.name} = {producer.name}"
    if mapped.kind == "unit":
        return f"{saved}."
    rows = _rows()
    own_rows = any(unit.id.lower() in rows for unit in staff.active(members, "unit")
                   if showrunner_of(unit, members) == mapped)
    if assignee.kind == "unit":
        return f"{saved}, for each of its units with no row." if own_rows else f"{saved}, for each of its units."
    return f"{saved}. Its units with no row use {producer.name} too." if own_rows else f"{saved}. Its units use {producer.name} too."


class Arguments(argparse.Namespace):
    action: str = ""
    member: str = ""
    producer: str = ""
    user_words: str = ""
    unit_only: bool = False


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    lookup = actions.add_parser("lookup", help="print the producer of a showrunner or unit")
    _ = lookup.add_argument("member", help="a name, staff path or staff id")
    change = actions.add_parser("set", help="save the producer of the assignee that /new_item asked about")
    _ = change.add_argument("member", help="the /new_item assignee: a showrunner or unit name, staff path or staff id")
    _ = change.add_argument("producer", help="a producer name or staff id")
    _ = change.add_argument("--user-words", required=True, help="nate's answer, in his words")
    _ = change.add_argument("--unit-only", action="store_true",
                            help="map only this unit, not its showrunner (nate said \"for <unit> only\")")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        members = staff.snapshot()
        kinds = ASSIGNEE_KINDS if args.action == "lookup" else MAPPED_KINDS
        member = find_member(args.member, members, kinds)
        if isinstance(member, UnknownMember):
            print(f"producer_map: {member.reason(kinds)}", file=sys.stderr)
            return 2
        if args.action == "lookup":
            found = producer_for(member.id, members)
            if isinstance(found, StaleRow):
                print(found.reason())
                return 3
            if isinstance(found, NoRow):
                print(f"{member.name}: no producer")
                return 3
            print(f"{member.name} -> {found.producer.name} ({found.producer.id}), set by {found.set_by.name}")
            return 0
        if args.unit_only and member.kind != "unit":
            print(f"producer_map: {member.name}: --unit-only needs a unit", file=sys.stderr)
            return 2
        producer = find_member(args.producer, members, ("producer",))
        if isinstance(producer, UnknownMember):
            print(f"producer_map: {producer.reason(('producer',))}", file=sys.stderr)
            return 2
        mapped = mapped_member(member, members, unit_only=args.unit_only)
        # The tune detail goes to stderr, so the Saved line is the only output for nate.
        with redirect_stdout(sys.stderr):
            code = tune.main([STEM, str(mapped.id), str(producer.id),
                              "--why", f"{mapped.name} reports to producer {producer.name}",
                              "--user-words", args.user_words])
        # tune also exits 2 when the row is live and only main lacks it.
        if code and _rows().get(mapped.id.lower()) != producer.id.lower():
            return code
        print(saved_line(member, mapped, producer, members))
        return 0
    except (OSError, ValueError, staff.StaffRefused) as error:
        print(f"producer_map: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
