"""Find the producer that a showrunner or unit reports to, and save one map row through /tune."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from scripts.lib import conf, tune
from scripts.lib.waiting_rows import NAMED_ROWS
from scripts.production import investigation, staff
from scripts.production.producer_map_rows import MAPPED_KINDS

STEM = NAMED_ROWS


@dataclass(frozen=True)
class NoProducer:
    """No producer row, parent row or producer identity names an active producer."""


NO_PRODUCER = NoProducer()


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


def _active_producer(identity: str, members: staff.StaffSnapshot) -> staff.StaffRow | NoProducer:
    row = members.rows_by_id.get(staff.StaffId(identity))
    return row if row is not None and row in staff.active(members, "producer") else NO_PRODUCER


def producer_for(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow | NoProducer:
    """Read the map at use; the nearest row or producer on the owner chain wins."""
    rows = _rows()
    for candidate in investigation.owner_chain(identity, members):
        row = members.by_id(candidate)
        if row is not None and row.kind == "producer":
            return _active_producer(row.id, members)
        target = rows.get(candidate.lower())
        if target is not None:
            return _active_producer(target, members)
    return NO_PRODUCER


def map_member(member: staff.StaffRow, members: staff.StaffSnapshot) -> staff.StaffRow:
    """Name the member whose row maps the most: a unit's own showrunner, else the member itself."""
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


class Arguments(argparse.Namespace):
    action: str = ""
    member: str = ""
    producer: str = ""
    user_words: str = ""


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    lookup = actions.add_parser("lookup", help="print the producer of a showrunner or unit")
    _ = lookup.add_argument("member", help="a name, staff path or staff id")
    change = actions.add_parser("set", help="save the producer of a showrunner or unit")
    _ = change.add_argument("member", help="a showrunner or unit name, staff path or staff id")
    _ = change.add_argument("producer", help="a producer name or staff id")
    _ = change.add_argument("--user-words", required=True, help="nate's answer, in his words")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        members = staff.snapshot()
        kinds: tuple[staff.Kind, ...] = (*MAPPED_KINDS, "producer") if args.action == "lookup" else MAPPED_KINDS
        member = find_member(args.member, members, kinds)
        if isinstance(member, UnknownMember):
            print(f"producer_map: {member.reason(kinds)}", file=sys.stderr)
            return 2
        if args.action == "lookup":
            found = producer_for(member.id, members)
            if isinstance(found, NoProducer):
                print(f"{member.name}: no producer")
                return 3
            print(f"{member.name} -> {found.name} ({found.id})")
            return 0
        producer = find_member(args.producer, members, ("producer",))
        if isinstance(producer, UnknownMember):
            print(f"producer_map: {producer.reason(('producer',))}", file=sys.stderr)
            return 2
        code = tune.main([STEM, str(member.id), str(producer.id),
                          "--why", f"{member.name} reports to producer {producer.name}",
                          "--user-words", args.user_words])
        # tune also exits 2 when the row is live and only main lacks it.
        if code and _rows().get(member.id.lower()) == producer.id.lower():
            print(f"{member.name} -> {producer.name} is live; main gets the row later")
            return 0
        return code
    except (OSError, ValueError, staff.StaffRefused) as error:
        print(f"producer_map: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
