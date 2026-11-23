#!/usr/bin/env python3
"""List a showrunner's recorded units, each with its session now."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, NamedTuple

from scripts.production import session_staff, showrunners, staff
from scripts.production import unit_lookup
from scripts.accounts import session_state
from scripts.herdr import herdr


class LiveUnit(NamedTuple):
    unit: str
    # None when no observed herdr session has the unit identity.
    session: unit_lookup.LabeledSession | None
    status: session_state.SessionState | Literal["unknown"] = "exited"
    member: staff.StaffRow | session_state.UnrecordedStaff = session_state.UnrecordedStaff()
    machine: str = ""
    owner: session_staff.UnitOwner = session_staff.ProvedAbsentUnit()
    lifecycle: session_state.StaffState = "active"

    @property
    def name(self) -> str:
        """The unit's session name now; empty when no Claude is known to run in it."""
        if isinstance(self.owner, session_staff.ProvedLiveUnit):
            owner = self.owner.owner
            claude = owner.claude if isinstance(owner, unit_lookup.LabeledSession) else owner
            if isinstance(claude, unit_lookup.LiveClaude):
                return claude.name
        return ""


class UnitListing(NamedTuple):
    units: list[LiveUnit]
    anomalies: tuple[str, ...]
    unassigned_panes: tuple[session_staff.UnassignedPaneObservation, ...] = ()
    unreadable_pane_lists: tuple[session_staff.UnreadablePaneList, ...] = ()
    failures: tuple[str, ...] = ()

    def checked_unit(self, name: str) -> session_staff.UnitOwner:
        for unit in self.units:
            if unit.unit == name:
                return unit.owner
        return session_staff.UnknownUnit(self.failures[0] if self.failures else "unit liveness is unavailable")


def read_units(production_name: str, *,
               record: staff.StaffSnapshot | None = None,
               check: Callable[[int, str], session_staff.ReportProcessIdentity] | None = None) -> UnitListing:
    """Return staff units and every anomaly from this observation."""
    record = staff.snapshot() if record is None else record
    sources = unit_lookup.member_sources()
    metadata = unit_lookup.unit_metadata(record, name=production_name)
    for problem in metadata.problems:
        print(f"live_units: {problem.name}: unavailable: {problem.reason}", file=sys.stderr)
    return _observe_units(production_name, record, sources, check)


def _observe_units(production_name: str, record: staff.StaffSnapshot, sources: unit_lookup.MemberSources,
                   check: Callable[[int, str], session_staff.ReportProcessIdentity] | None = None) -> UnitListing:
    with session_staff.anomalies(check) as lines, herdr.observation_snapshot():
        every = unit_lookup.every_labeled_session(record=record, sources=sources)
        units = _with_status(production_name, every, record=record)
    return UnitListing(units, tuple(lines), every.unassigned_panes, every.unreadable_pane_lists, every.failures)


def units_of(production_name: str) -> list[LiveUnit]:
    """List recorded unit children with their current panes."""
    listing = read_units(production_name)
    for line in listing.anomalies:
        print(line, file=sys.stderr)
    return listing.units


def _with_status(production_name: str, every: unit_lookup.LabeledProductions, *,
                 record: staff.StaffSnapshot) -> list[LiveUnit]:
    names = every.members.get(production_name, ())
    if not names:
        return []
    rows = session_staff.production_members(record).get(production_name, ())
    detached = session_state.running_detached_work(replace(record, rows=rows))
    members = {row.name: row for row in rows}
    result: list[LiveUnit] = []
    panes = every.units.get(production_name, {})
    for name in names:
        row = members.get(name)
        observed = panes.get(name)
        owner = every.checked_unit(production_name, name)
        activity: session_state.LiveActivity | session_state.NoLiveSession
        match owner:
            case session_staff.ProvedLiveUnit(owner=unit_lookup.LabeledSession(
                    claude=unit_lookup.LiveClaude(activity=state))) | session_staff.ProvedLiveUnit(
                        owner=unit_lookup.LiveClaude(activity=state)):
                activity = session_state.LiveActivity(state)
            case session_staff.ProvedLiveUnit():
                activity = session_state.LiveActivity("idle")
            case session_staff.ProvedAbsentUnit() | session_staff.UnknownUnit():
                activity = session_state.NoLiveSession()
        member = row if row is not None else session_state.UnrecordedStaff()
        lifecycle = session_state.staff_state(member)
        state = ("unknown" if isinstance(owner, session_staff.UnknownUnit)
                 else session_state.classify(member, activity, detached=detached))
        machine = staff.worktree_machine(row) if row is not None else ""
        result.append(LiveUnit(name, observed, state, member, machine, owner, lifecycle))
    return result


def units_in(production_name: str, every: unit_lookup.LabeledProductions) -> list[LiveUnit]:
    """Join the named production to the caller's session snapshot."""
    if not every.members.get(production_name):
        return []
    return _with_status(production_name, every, record=staff.snapshot())


def listing_in(production_name: str, every: unit_lookup.LabeledProductions) -> UnitListing:
    """Keep report failures with the resulting units."""
    return UnitListing(units_in(production_name, every), every.failures, every.unassigned_panes,
                       every.unreadable_pane_lists, every.failures)


class LiveUnitIds(NamedTuple):
    """Keep unknown units and observation failures with the proved Claude IDs."""

    proved_ids: list[str]
    unknown: dict[str, session_staff.UnknownUnit]
    failures: tuple[str, ...]


def live_unit_ids(runner: showrunners.Showrunner) -> LiveUnitIds:
    """Return proved Claude directors and retain unknown units."""
    name = runner["name"]
    try:
        record = staff.snapshot()
        listing = read_units(name, record=record)
    except OSError as error:
        reason = f"live_units: the units of {name} could not be listed: {error}"
        print(reason, file=sys.stderr)
        return LiveUnitIds([], {}, (reason,))
    for line in listing.anomalies:
        print(line, file=sys.stderr)
    live: list[str] = []
    unknown: dict[str, session_staff.UnknownUnit] = {}
    for unit in listing.units:
        if isinstance(unit.owner, session_staff.UnknownUnit):
            unknown[unit.unit] = unit.owner
        elif isinstance(unit.owner, session_staff.ProvedLiveUnit):
            owner = unit.owner.owner
            claude = owner.claude if isinstance(owner, unit_lookup.LabeledSession) else owner
            if isinstance(claude, unit_lookup.LiveClaude):
                member = record.by_current_session(claude.session_id)
                if member is not None:
                    live.append(str(member.id))
    return LiveUnitIds(live, unknown, listing.failures)


def main(arguments: list[str]) -> int:
    """Print one line per unit: unit id, pane, whether Claude runs (live, stopped, unknown, gone), name."""
    status = arguments[:1] == ["--status"]
    if status:
        arguments = arguments[1:]
    try:
        match arguments:
            case ["--production", name]:
                listing = _observe_units(name, staff.snapshot(), unit_lookup.member_sources())
                for line in listing.anomalies:
                    print(line, file=sys.stderr)
                units = listing.units
            case [showrunner] if not showrunner.startswith("-"):
                units = units_of(showrunners.named(showrunner)["name"])
            case _:
                print("usage: live_units.py --production <staff name>", file=sys.stderr)
                return 2
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    for unit in units:
        session_id = ""
        match unit.owner:
            case session_staff.ProvedLiveUnit(owner=unit_lookup.LabeledSession(identity=unit_lookup.LiveSession(id=identity))):
                running, session_id = "live", identity
            case session_staff.ProvedLiveUnit(owner=unit_lookup.LiveClaude(session_id=identity)):
                running, session_id = "live", identity
            case session_staff.ProvedLiveUnit():
                running = "unknown"
            case session_staff.ProvedAbsentUnit():
                running = "gone" if unit.session is None else "stopped"
            case session_staff.UnknownUnit():
                running = "unknown"
        columns = (unit.unit, unit_lookup.place_text(unit.session.place) if unit.session is not None else "", running, unit.name)
        if status:
            columns += (session_id,)
        print("\t".join(columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
