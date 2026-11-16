#!/usr/bin/env python3
"""Print a showrunner's unit blocks or style ticket table.

Usage: unit_phases.py [--production <staff name>]

Without `--production`, the caller's staff row names the production.
The phase and task come from the unit's run records.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import sys
import subprocess
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from collections.abc import Callable, Sequence
from typing import Literal, NamedTuple, TypedDict, cast
from enum import Enum

from scripts.production import census, live_units
from scripts.production import session_staff, showrunners, staff, staff_sources, unit_lookup
from scripts.production.production_doc import Refusal, production_field, read_production
from scripts.agents import remote
from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import unit_config
from scripts.message import session_send
from scripts.herdr import herdr
from scripts.lib.process_failure import failure_detail

from scripts.style import style_config
from scripts.direct import phase_table, phase_waits
from scripts.direct import plan_history

from scripts.production import display_cells, land_queue, land_runs
from scripts.production import unit_queue, waits_on
from scripts.lib import unit_runs
from scripts.whoami import quota_state

RunEventsReader = Callable[[Path], list[dict[str, object]]]


WORK_EVENT = re.compile(r'"event_type"\s*:\s*"([^"\\]+)"')
WORK_EVENTS = frozenset(("run_started", "reopened", "phase_started", "phase_finished", "run_finished", "pass_started", "pass_finished",
                         "activity_started", "activity_finished", "eta_stated", "wait_started", "wait_finished",
                         "wait_unclosed", "wait_holder_seen", "checkpoint"))


def read_work_events(path: Path) -> list[dict[str, object]]:
    """Read phase, step, ETA, and wait events."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        kind = WORK_EVENT.search(line)
        if kind is None or kind.group(1) not in WORK_EVENTS:
            continue
        try:
            value = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(cast(dict[str, object], value))
    return events


def single_spaced(text: str) -> str:
    """Join the words of text with one space each."""
    return " ".join(text.split())


class BoardPlanPhase(TypedDict, total=False):
    phase: str
    title: str
    status: str


class BoardPlanRecord(TypedDict, total=False):
    plan: str
    phases: list[BoardPlanPhase]
    unavailable: str


def record_fields(record: object) -> BoardPlanRecord:
    """Read the board's plan fields from a built plan or its JSON form."""
    if isinstance(record, phase_table.PhaseRecord):
        return {"plan": str(record.plan),
                "phases": [{"phase": row.phase, "title": row.title,
                            "status": ("done" if isinstance(row, phase_table.DonePhase) else
                                       "running" if isinstance(row, phase_table.OpenPhase) else "todo")}
                           for row in record.phases]}
    return cast(BoardPlanRecord, cast(object, record)) if isinstance(record, dict) else {}


def elapsed_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def short_time(moment: datetime, *, now: float, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    today = datetime.fromtimestamp(now, zone).date()
    return local.strftime("%H:%M" if local.date() == today else "%Y-%m-%d %H:%M")


class ProgressState(NamedTuple):
    square: str
    label: str


PROGRESS_DONE = ProgressState("🟩", "done")
PROGRESS_OPEN = ProgressState("🟨", "open")
PROGRESS_NOT_STARTED = ProgressState("⬜", "not started")


def progress_cell(record: object) -> str:
    """Show phase states as a colored bar."""
    rows = plan_phase_rows(record_fields(record))
    if not rows:
        return display_cells.NONE
    size = min(20, len(rows))
    cells: list[str] = []
    for index in range(size):
        states = {row.get("status") for row in rows[index * len(rows) // size:(index + 1) * len(rows) // size]}
        progress = PROGRESS_OPEN if states & {"running", "open"} else PROGRESS_NOT_STARTED if "todo" in states else PROGRESS_DONE
        cells.append(progress.square)
    return "".join(cells)


def progress_legend() -> str:
    """Describe the squares in the Progress bar."""
    return "Progress: " + " · ".join(f"{state.square} {state.label}" for state in (
        PROGRESS_DONE, PROGRESS_OPEN, PROGRESS_NOT_STARTED))



def phase_cell(record: object) -> str:
    """Show the completed phase count and total."""
    rows = plan_phase_rows(record_fields(record))
    return f"{sum(row.get('status') == 'done' for row in rows)}/{len(rows)}" if rows else display_cells.NONE



def phase_eta_cell(phase: phase_table.OpenPhase | phase_table.TodoPhase, *, now: float, zone: ZoneInfo) -> str:
    """Show the latest promise for one phase, including its delay."""
    if not phase.eta_trail:
        return "no ETA stated"
    promise = phase.eta_trail[-1][0]
    text = short_time(promise, now=now, zone=zone)
    if now > promise.timestamp():
        text += " · late " + elapsed_text(now - promise.timestamp())
    return text


def running_plan(record: object) -> bool:
    """Return whether the record has unfinished plan phases."""
    return any(phase.get("status") != "done" for phase in plan_phase_rows(record_fields(record)))


def eta_plan_cell(record: object, *, now: float, zone: ZoneInfo) -> str:
    """Show the predicted finish of an unfinished plan."""
    if (running_plan(record) and isinstance(record, phase_table.PhaseRecord)
            and isinstance(record.plan_finish, phase_table.PredictedFinish)):
        return short_time(record.plan_finish.at, now=now, zone=zone)
    return display_cells.NONE


def pending_eta_cell(events: list[dict[str, object]], open_phase_ids: tuple[str, ...], land: land_queue.LandView, *,
                     unit: str, now: float, zone: ZoneInfo, completed_phase_ids: tuple[str, ...] = ()) -> str:
    """Name promises with no later finish, sent work, or open row."""
    targets: dict[str, tuple[datetime, float]] = {}
    instances: dict[str, str] = {}
    for event in events:
        kind = event.get("event_type")
        instance = str(event.get("phase_instance_id", ""))
        phase_id = str(event.get("phase_id") or instances.get(instance, ""))
        if kind == "phase_started":
            instances[instance] = phase_id
        elif kind in ("phase_finished", "checkpoint"):
            _ = targets.pop(phase_id, None)
        elif kind == "eta_stated":
            target = str(event.get("eta_phase_id", phase_id))
            at, stated = event.get("eta_at"), event.get("timestamp_epoch")
            if (target and target not in open_phase_ids and target not in completed_phase_ids
                    and isinstance(at, int | float)):
                targets[target] = (datetime.fromtimestamp(at, UTC), float(stated) if isinstance(stated, int | float) else 0.0)
    results: dict[str, float] = {}
    for result in land.results.values():
        if result["unit"] != unit:
            continue
        try:
            at = datetime.fromisoformat(result["time"]).timestamp()
        except ValueError:
            continue
        results[result["phase"]] = max(results.get(result["phase"], at), at)
    parts: list[str] = []
    for phase_id, (promise, stated) in targets.items():
        if results.get(phase_id, stated) > stated:
            continue
        text = f"phase {phase_id}: {short_time(promise, now=now, zone=zone)}"
        if now > promise.timestamp():
            text += " · late " + elapsed_text(now - promise.timestamp())
        parts.append(text)
    return " · ".join(parts) or display_cells.NONE




def eta_cell(record: object, *, now: float, zone: ZoneInfo,
             open_run_phases: tuple[phase_table.OpenPhase, ...] = ()) -> str:
    """Show the promise for open work or the next promised plan phase."""
    phases = record.phases if isinstance(record, phase_table.PhaseRecord) else []
    phase_ids = {phase.phase for phase in phases}
    opened = [phase for phase in open_run_phases if phase.phase in phase_ids]
    if not opened:
        opened = [phase for phase in phases if isinstance(phase, phase_table.OpenPhase)]
    current = record.current if isinstance(record, phase_table.PhaseRecord) else phase_table.NoOpenPhase()
    selected: phase_table.OpenPhase | phase_table.TodoPhase | phase_table.NoOpenPhase
    if opened:
        selected = (current if isinstance(current, phase_table.OpenPhase)
                    and any(phase.phase == current.phase for phase in opened) else opened[-1])
    elif open_run_phases:
        selected = open_run_phases[-1]
    elif isinstance(current, phase_table.OpenPhase):
        selected = current
    else:
        selected = next((phase for phase in phases if isinstance(phase, phase_table.TodoPhase) and phase.eta_trail),
                        phase_table.NoOpenPhase())
    if isinstance(selected, phase_table.NoOpenPhase):
        return display_cells.NONE
    return phase_eta_cell(selected, now=now, zone=zone)


def status_rows(unit: str, status: str, machine: tuple[str, ...], record: object, run_events: list[dict[str, object]],
                phases: tuple[phase_table.OpenPhase, ...], land: land_queue.LandView, *,
                now: float, zone: ZoneInfo) -> tuple[tuple[tuple[str, ...], ...], bool]:
    """Build the unit's status table rows and whether they show a plan ETA."""
    eta_record = (phase_table.with_run_promises(record, run_events)
                  if run_events and isinstance(record, phase_table.PhaseRecord) else record)
    show_plan_eta = running_plan(record)
    plan_eta = eta_plan_cell(eta_record, now=now, zone=zone)
    open_ids = tuple(phase.phase for phase in phases)
    done_ids = {phase.get("phase", "") for phase in plan_phase_rows(record_fields(record)) if phase.get("status") == "done"}
    for event in run_events:
        if event.get("event_type") != "phase_finished":
            continue
        phase_id = str(event.get("phase_id", ""))
        if event.get("status") == "completed":
            done_ids.add(phase_id)
        else:
            done_ids.discard(phase_id)
    if run_events and not run_is_finished(run_events):
        pending = pending_eta_cell(run_events, open_ids, land, unit=unit, now=now, zone=zone,
                                   completed_phase_ids=tuple(done_ids))
        if pending != display_cells.NONE:
            plan_eta = " · ".join(cell for cell in (plan_eta, pending) if cell != display_cells.NONE)
            show_plan_eta = True
    if not phases:
        return ((unit, status, *machine, progress_cell(record), phase_cell(record),
                 eta_cell(eta_record, now=now, zone=zone, open_run_phases=phases), plan_eta),), show_plan_eta
    blank = (display_cells.NONE,) * len(machine)
    return tuple((unit, status if index == 0 else display_cells.NONE, *(machine if index == 0 else blank),
                  progress_cell(record) if index == 0 else display_cells.NONE, phase.phase,
                  phase_eta_cell(phase, now=now, zone=zone), plan_eta if index == 0 else display_cells.NONE)
                 for index, phase in enumerate(phases)), show_plan_eta


def status_table(rows: list[tuple[str, ...]], *, machines: bool, show_plan_eta: bool, columns: int | None) -> str:
    """Render the status table and its Progress legend."""
    headers: tuple[str, ...] = ("Unit", "Status", *(("Machine",) if machines else ()), "Progress", "Phase",
                                "ETA Next", "ETA Plan")
    if not show_plan_eta:
        headers = headers[:-1]
        rows = [cells[:-1] for cells in rows]
    progress_column = 3 if machines else 2
    show_progress = any(cells[progress_column] != display_cells.NONE for cells in rows)
    headers, rows = display_cells.wrapped(headers, rows, columns)
    lines = ["| " + " | ".join(display_cells.escaped(header) for header in headers) + " |",
             "| " + " | ".join(("---",) * len(headers)) + " |",
             *("| " + " | ".join(display_cells.escaped(cell) for cell in cells) + " |" for cells in rows)]
    if show_progress:
        lines.extend(("", progress_legend()))
    return "\n".join(lines)


class CensusView(NamedTuple):
    """One census, indexed for the table rows."""

    sessions: dict[str, census.CensusSession]
    reachable: frozenset[str]
    workers: dict[staff.StaffId, tuple[census.CensusWorker, ...]]


def census_view(read: census.Census | census.CensusFailed) -> CensusView | census.CensusFailed:
    if isinstance(read, census.CensusFailed):
        return read
    workers: dict[staff.StaffId, list[census.CensusWorker]] = {}
    for worker in read.workers:
        workers.setdefault(worker.parent_staff_id, []).append(worker)
    return CensusView({session.session: session for session in read.sessions},
                      frozenset(machine.name for machine in read.machines if machine.reachable),
                      {parent: tuple(rows) for parent, rows in workers.items()})


def _unit_session(unit: live_units.LiveUnit) -> str | None:
    """Return the staff row's current session, else the session of the Claude in the unit's pane."""
    if isinstance(unit.member, staff.StaffRow):
        return unit.member.current_session
    claude = unit.session.claude if unit.session is not None else None
    return claude.session_id if isinstance(claude, unit_lookup.LiveClaude) else None


def census_status(view: CensusView | census.CensusFailed, unit: live_units.LiveUnit) -> str:
    """Return the tile's status word, `exited` for an unlisted unit on a scanned machine, or `—`."""
    if isinstance(view, census.CensusFailed):
        return display_cells.NONE
    session_id = _unit_session(unit)
    if session_id is not None and (session := view.sessions.get(session_id)) is not None:
        return session.status or display_cells.NONE
    return "exited" if (unit.machine or remote.local_machine()) in view.reachable else display_cells.NONE


def plan_phase_rows(fields: BoardPlanRecord) -> list[BoardPlanPhase]:
    return fields.get("phases", [])


# The repository whose local origin/main a queued item's wait is checked against; no fetch.
REPOSITORY = Path(__file__).resolve().parents[2]

WORK_STARTS = frozenset({"phase_started", "pass_started", "activity_started"})


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def try_text(result: land_runs.LandResult, *, now: float, zone: ZoneInfo) -> str:
    """Describe a merge result with its recorded time."""
    try:
        when = short_time(datetime.fromisoformat(result["time"]), now=now, zone=zone)
    except (ValueError, TypeError):
        when = "time not recorded"
    if result["landed"]:
        return single_spaced(f"merged {when}")
    if conflicts := re.findall(r"Merge conflict in (\S+)", result["detail"]):
        return f"merge conflict in {', '.join(dict.fromkeys(conflicts))}; {when}"
    detail = single_spaced(result["detail"].splitlines()[0]) if result["detail"].strip() else ""
    failure = f"{result['state']} at {result['step']}" + (f": {detail}" if detail else "")
    return f"{failure}; {when}"


def land_text(state: land_queue.LandState, *, now: float, zone: ZoneInfo) -> str:
    """Describe the merge state of a submitted commit."""
    match state:
        case land_queue.LandRunning():
            return "merging now"
        case land_queue.InLine(place, failed, carried):
            text = (f"merges with {place.owner}; " if carried else "")
            text += f"{ordinal(place.number)} in the merge line"
            return f"{text}; last attempt {try_text(failed, now=now, zone=zone)}" if failed else text
        case land_queue.LandEnded(result) if result["landed"]:
            return try_text(result, now=now, zone=zone)
        case land_queue.LandEnded(result):
            return f"did not merge: {try_text(result, now=now, zone=zone)}"
        case land_queue.NotInLine():
            return "not in the merge line; reason not recorded"
        case land_queue.LineUnused():
            return "merge position not recorded"


class SentCommit(NamedTuple):
    """The commit sent after the phase's last work start."""

    commit: str


class SentCommitUnrecorded(NamedTuple):
    """The phase sent work without a recorded commit."""


class NoSentWork(NamedTuple):
    """The phase has no sent work after its last work start."""


PhaseSentWork = SentCommit | SentCommitUnrecorded | NoSentWork


def phase_sent_work(events: list[dict[str, object]]) -> PhaseSentWork:
    """Find the sent commit after the phase's last work start."""
    for event in reversed(events):
        kind = event.get("event_type")
        if kind == "checkpoint":
            commit = event.get("checkpoint_commit")
            return SentCommit(commit) if isinstance(commit, str) and commit else SentCommitUnrecorded()
        if kind in WORK_STARTS:
            return NoSentWork()
    return NoSentWork()


class WorkItem(NamedTuple):
    """One work name and its separate display state."""

    work: str
    state_text: str


class UnitMergeNotes(NamedTuple):
    """The unit's merge entries and unresolved merge evidence."""

    grouped_phases: frozenset[str]
    entries: tuple[WorkItem, ...]
    pending: bool
    wait_targets: frozenset[str]


def merge_state_text(state: land_queue.LandState, production: str) -> str:
    """Describe the live merge action without copying its command log."""
    actor = production or "The production"
    if isinstance(state, land_queue.LandRunning):
        return f"{actor} merges it now"
    if isinstance(state, land_queue.InLine):
        return f"{ordinal(state.place.number)} in {actor}'s merge line"
    if isinstance(state, land_queue.LandEnded):
        if state.result["landed"]:
            return f"{actor} merged it"
        detail = state.result["detail"].strip()
        conflicts = re.findall(r"Merge conflict in (\S+)", detail)
        cause = ("merge conflict in " + ", ".join(dict.fromkeys(conflicts)) if conflicts else
                 single_spaced(detail.splitlines()[0].split("; log ", 1)[0]) if detail else "failure cause not recorded")
        return f"{actor} cannot merge it: {cause}"
    if isinstance(state, land_queue.NotInLine):
        return "Merge submission not in the merge line; reason not recorded"
    return "Merge position not recorded"


def unit_lands(unit: str, land: land_queue.LandView, *, open_sent: dict[str, PhaseSentWork], now: float, zone: ZoneInfo,
               on_main: Callable[[str], bool], unit_waits: Sequence[phase_waits.UnitWait] = (),
               done_ids: tuple[str, ...] = (), phase_titles: tuple[tuple[str, str], ...] = (),
               production: str = "") -> UnitMergeNotes:
    """Describe each merge group once and retain failed results without open rows."""
    prefix = f"{unit} "
    titles = dict(phase_titles)
    phase_ids = set(titles) | set(open_sent)
    phase_ids.update(result["phase"] for result in land.results.values() if result["unit"] == unit)
    phase_ids.update(owner.removeprefix(prefix) for owner in land.running.values() if owner.startswith(prefix))
    if not isinstance(land.line, land_queue.NoLine):
        phase_ids.update(place.owner.removeprefix(prefix) for place in land.line.values() if place.owner.startswith(prefix))

    def helper(phase: str) -> bool:
        parent, _, suffix = phase.rpartition("-")
        return suffix in ("shrink", "repair") and parent in phase_ids

    def named(phase: str) -> str:
        work = work_name(phase, titles.get(phase, ""))
        return work[:1].upper() + work[1:]

    def own(owner: str) -> str:
        phase = owner.removeprefix(prefix) if owner.startswith(prefix) else ""
        return "" if helper(phase) else phase

    open_sent = {phase: sent for phase, sent in open_sent.items() if not helper(phase)}
    merging: list[str] = []
    items: dict[int, list[str]] = {}
    for phase, sent in open_sent.items():
        if not isinstance(sent, SentCommit):
            continue
        state = land_queue.land_state(land, sent.commit)
        if isinstance(state, land_queue.LandRunning):
            merging.append(phase)
        elif isinstance(state, land_queue.InLine):
            items.setdefault(state.place.number, []).append(phase)
    open_merging = frozenset(merging)
    open_items = {number: frozenset(phases) for number, phases in items.items()}
    for owner in land.running.values():
        if (phase := own(owner)) and phase not in merging:
            merging.append(phase)
    for place in ({} if isinstance(land.line, land_queue.NoLine) else land.line).values():
        if (phase := own(place.owner)) and phase not in items.setdefault(place.number, []):
            items[place.number].append(phase)
    queued = {phase for phases in items.values() for phase in phases}
    newest: dict[str, land_runs.LandResult] = {}
    for result in land.results.values():
        phase = result["phase"]
        if (result["unit"] == unit and not helper(phase)
                and (phase not in newest or result["time"] > newest[phase]["time"])):
            newest[phase] = result
    wait_targets: set[str] = set()
    holds: dict[str, phase_waits.UnitWait] = {}
    for wait in unit_waits:
        if helper(wait.target):
            continue
        sent = open_sent.get(wait.target, NoSentWork())
        if (wait.target not in done_ids and isinstance(sent, NoSentWork)
                and wait.target not in queued and wait.target not in merging and wait.target not in newest):
            continue
        wait_targets.add(wait.target)
        result = newest.get(wait.target)
        if (wait.target in queued or wait.target in merging
                or result is not None and (not isinstance(sent, SentCommit) or result["commit"] == sent.commit)):
            continue
        if isinstance(sent, SentCommit) and not isinstance(land_queue.land_state(land, sent.commit),
                                                          land_queue.NotInLine | land_queue.LineUnused):
            continue
        if isinstance(sent, SentCommit) and on_main(sent.commit):
            continue
        holds[wait.target] = wait
    parts: list[WorkItem] = []
    grouped: set[str] = set()
    pending = bool(merging or items)
    shown = set(open_merging)
    for phases in open_items.values():
        shown.update(phases)
    if merging:
        for index, phase in enumerate(merging):
            text = merge_state_text(land_queue.LandRunning(), production) if index == 0 else f"Merges with {phase_reference(merging[0])}"
            parts.append(WorkItem(named(phase), text))
        if len(merging) > 1:
            grouped.update(open_merging)
    for number, phases in sorted(items.items()):
        for index, phase in enumerate(phases):
            text = (f"{ordinal(number)} in {production or 'the production'}'s merge line" if index == 0
                    else f"Merges with {phase_reference(phases[0])}")
            parts.append(WorkItem(named(phase), text))
        if len(phases) > 1:
            grouped.update(open_items.get(number, ()))
    for phase, sent in open_sent.items():
        if isinstance(sent, NoSentWork) or phase in shown:
            continue
        state = land_queue.land_state(land, sent.commit) if isinstance(sent, SentCommit) else land_queue.LineUnused()
        text = (unit_wait_state(holds[phase], now=now, zone=zone) if phase in holds
                else merge_state_text(state, production))
        parts.append(WorkItem(named(phase), text))
        pending = pending or not (isinstance(state, land_queue.LandEnded) and state.result["landed"])
    for phase, wait in holds.items():
        if isinstance(open_sent.get(phase, NoSentWork()), NoSentWork):
            parts.append(WorkItem(named(phase), unit_wait_state(wait, now=now, zone=zone)))
            pending = True
    for phase, result in newest.items():
        if (not result["landed"] and phase not in open_sent and phase not in queued and phase not in merging
                and not on_main(result["commit"])):
            parts.append(WorkItem(named(phase), merge_state_text(land_queue.LandEnded(result), production or result["production"])))
            pending = True
    return UnitMergeNotes(frozenset(grouped), tuple(parts), pending, frozenset(wait_targets))


def completed_unit_phases(events: Sequence[dict[str, object]]) -> dict[str, PhaseSentWork]:
    """Read completed phases and their sent work from their latest instances."""
    instances: dict[str, str] = {}
    phases: dict[str, list[dict[str, object]]] = {}
    completed: set[str] = set()
    for event in events:
        kind = event.get("event_type")
        phase = str(event.get("phase_id") or instances.get(str(event.get("phase_instance_id", "")), ""))
        if kind == "phase_started":
            instances[str(event.get("phase_instance_id", ""))] = phase
            phases[phase] = []
            completed.discard(phase)
        if phase in phases:
            phases[phase].append(event)
            if kind == "phase_finished" and event.get("status") == "completed":
                completed.add(phase)
    return {phase: phase_sent_work(history) for phase, history in phases.items() if phase in completed}


class OpenRunPhase(NamedTuple):
    """The id and current step of a phase that has not finished."""

    phase_id: str
    step: display_cells.OpenStep | display_cells.NoOpenStep


class OpenUnitRun(NamedTuple):
    """A unit's run with at least one phase still open."""

    started: float
    path: Path
    phases: tuple[OpenRunPhase, ...]


class NewestUnitRun(NamedTuple):
    """The newest run found for one unit."""

    started: float
    path: Path
    start: dict[str, object]


class NoOpenUnitRun(NamedTuple):
    """The unit's newest run has no open phase."""


class OpenPhaseHistory(NamedTuple):
    """Keep the events for one phase until it closes."""

    phase_id: str
    events: list[dict[str, object]]


def newest_unit_runs(snapshot: staff.StaffSnapshot) -> dict[staff.StaffId, NewestUnitRun]:
    """Select each unit's current staff run before newer ad hoc history."""
    snapshot = staff.StaffSnapshot(staff.active(snapshot, "unit"))
    newest: dict[staff.StaffId, NewestUnitRun] = {}
    for path, started_event in plan_history.run_starts():
        try:
            owner = unit_runs.run_owner(started_event, snapshot)
        except staff.StaffUnreadable:
            continue
        if isinstance(owner, unit_runs.RunOwnerUnrecorded):
            continue
        started = next((float(value) for value in (started_event.get("run_started_at"), started_event.get("timestamp_epoch"))
                        if isinstance(value, int | float) and not isinstance(value, bool)), 0.0)
        previous = newest.get(owner.id)
        current_run = owner.run.path.name if isinstance(owner.run, staff.OpenRun) else ""
        if previous is None or (path.stem == current_run, started, path.name) > (
                previous.path.stem == current_run, previous.started, previous.path.name):
            newest[owner.id] = NewestUnitRun(started, path, started_event)
    return newest


def run_is_finished(events: list[dict[str, object]]) -> bool:
    """The latest finish or reopen event determines whether the run is finished."""
    for event in reversed(events):
        kind = event.get("event_type")
        if kind in ("run_finished", "reopened"):
            return kind == "run_finished"
    return False


def open_unit_runs(snapshot: staff.StaffSnapshot, *,
                   read_run: RunEventsReader = read_work_events,
                   newest: dict[staff.StaffId, NewestUnitRun] | None = None) -> dict[staff.StaffId, OpenUnitRun]:
    """Read each unit's staff pointer and retain its open phases."""
    result: dict[staff.StaffId, OpenUnitRun] = {}
    for owner in snapshot.find(kind="unit"):
        if newest is not None:
            latest = newest.get(owner.id)
            if latest is None:
                continue
            path = latest.path
        else:
            if not isinstance(owner.run, staff.OpenRun) or not unit_runs.run_is_open(owner.run.path):
                continue
            path = plan_history.run_events_path(owner.run.path)
        events = read_run(path)
        if run_is_finished(events):
            continue
        start = next((event for event in events if event.get("event_type") == "run_started"), dict[str, object]())
        started = next((float(value) for value in (start.get("run_started_at"), start.get("timestamp_epoch"))
                        if isinstance(value, int | float) and not isinstance(value, bool)), 0.0)
        phases: dict[str, OpenPhaseHistory] = {}
        current = ""
        for event in events:
            kind = event.get("event_type")
            key = str(event.get("phase_instance_id") or event.get("phase_id") or current)
            if kind == "phase_started":
                current = key
                phases[key] = OpenPhaseHistory(str(event.get("phase_id", "")), [event])
            elif kind == "phase_finished":
                _ = phases.pop(key, None)
            elif key in phases:
                phases[key].events.append(event)
        previous = newest.get(owner.id) if newest is not None else None
        if previous is not None and previous.path == path:
            started = previous.started
        if phases:
            result[owner.id] = OpenUnitRun(started, path, tuple(
                OpenRunPhase(phase.phase_id, display_cells.open_step(phase.events)) for phase in phases.values()))
    return result


def wait_text(found: waits_on.WaitState, land: land_queue.LandView, *, now: float, zone: ZoneInfo) -> str:
    """Describe a dependency from the existing local records."""
    match found:
        case waits_on.PhaseSent(commit, wait):
            state = land_queue.land_state(land, commit)
            if (isinstance(wait, phase_waits.UnitWait)
                    and isinstance(state, land_queue.NotInLine | land_queue.LineUnused)):
                return f"done; {in_sentence(wait.by)} keeps it from the merge until {wait.reason.rstrip().rstrip('.')}"
            return land_text(state, now=now, zone=zone)
        case waits_on.NotOnMain(commit):
            state = land_queue.land_state(land, commit)
            if isinstance(state, land_queue.NotInLine | land_queue.LineUnused):
                return "absent from the local copy of main"
            if isinstance(state, land_queue.InLine | land_queue.LandRunning):
                return land_text(state, now=now, zone=zone)
            return f"absent from the local copy of main; {land_text(state, now=now, zone=zone)}"
        case waits_on.PhaseOpen():
            return "started"
        case waits_on.PhaseNotStarted():
            return "not started"
        case _:
            return "dependency not found locally"


def phase_reference(phase: str) -> str:
    """Name a phase of the same unit by its id; a numeric id reads as `phase <n>`."""
    return f"phase {phase}" if phase.isdecimal() else phase


def dependency_text(reference: str, unit: str, *, title: str = "", merge: bool = False) -> str:
    """Describe a dependency with its local ID or external unit and title."""
    owner, slash, phase = reference.partition("/")
    action = " to merge" if merge else ""
    if not slash or owner == unit:
        target = phase if slash else reference
        return f"Waits for {phase_reference(target)}{action}"
    if title:
        return f"Waits for {owner}{action}: {title}"
    return f"Waits for {owner} phase {phase}{action}"


def queued_text(item: unit_queue.QueuedItem, found: waits_on.WaitState | None,
                land: land_queue.LandView, *, now: float, zone: ZoneInfo, unit: str = "") -> WorkItem:
    """Separate the queue name from its short dependency state."""
    target, colon, title = item.text.partition(":")
    work = work_name(target.strip(), title.strip()) if colon else item.text
    work = work[:1].upper() + work[1:]
    if not item.after or found is None or isinstance(found, waits_on.OnMain):
        return WorkItem(work, "Ready")
    if isinstance(found, waits_on.PhaseSent) and isinstance(found.wait, phase_waits.UnitWait):
        state = land_queue.land_state(land, found.commit)
        if isinstance(state, land_queue.NotInLine | land_queue.LineUnused):
            return WorkItem(work, unit_wait_state(found.wait, now=now, zone=zone))
    return WorkItem(work, dependency_text(item.after, unit, merge=True))


class UnitWorkQueue(NamedTuple):
    """The work items that a unit has not started."""

    items: tuple[unit_queue.QueuedItem, ...]


class UnreadableWorkQueue(NamedTuple):
    """The unit's work queue could not be read."""


def read_work_queue(staff_id: staff.StaffId) -> UnitWorkQueue | UnreadableWorkQueue:
    """Read the unit's work queue and report a read failure."""
    try:
        return UnitWorkQueue(tuple(unit_queue.read_items(staff_id)))
    except (OSError, ValueError) as error:
        print(f"unit_phases: {unit_queue.state_path(staff_id)}: {error}", file=sys.stderr)
        return UnreadableWorkQueue()


def fallback_plan(member: staff.StaffRow, run: NewestUnitRun, opened: OpenUnitRun | NoOpenUnitRun,
                  record: object, *, read_plan: Callable[[Path], list[plan_history.PlanPhase]]) -> BoardPlanRecord:
    """Read unfinished plan headings against the newest run's open phases."""
    if not isinstance(member.worktree, staff.PresentWorktree):
        return record_fields(record)
    value = run.start.get("project_plan_doc") or run.start.get("plan_doc")
    if not isinstance(value, str) or not value:
        return record_fields(record)
    previous = run.start.get("working_dir")
    if isinstance(previous, str) and previous and value.startswith(previous + "/"):
        value = str(Path(value).relative_to(previous))
    path = plan_history.resolve_plan_path(member.worktree.path, value)
    if isinstance(record, phase_table.PhaseRecord) and record.plan == path:
        return record_fields(record)
    running = {phase.phase_id for phase in opened.phases} if isinstance(opened, OpenUnitRun) else set[str]()
    return {"plan": str(path), "phases": [
        {"phase": phase["id"], "title": phase["title"],
         "status": "done" if phase["done"] else "running" if phase["id"] in running else "todo"}
        for phase in read_plan(path)]}


class PlanSource(Enum):
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    EMPTY = "readable without phases"
    READABLE = "readable with phases"


class TaskAttempt(NamedTuple):
    phase: str
    scope: str
    title: str
    instance: str
    outcome: Literal["current", "stopped", "completed"]
    finished: datetime | phase_table.PhaseNotFinished
    reason: str


class UnitTaskFacts(NamedTuple):
    """The plan and run attempts behind both unit displays."""

    plan_source: PlanSource
    plan_rows: tuple[BoardPlanPhase, ...]
    attempts: tuple[TaskAttempt, ...]


def task_facts(record: object, events: Sequence[dict[str, object]]) -> UnitTaskFacts:
    fields = record_fields(record)
    rows = tuple(plan_phase_rows(fields))
    source = (PlanSource.UNAVAILABLE if "unavailable" in fields else PlanSource.READABLE if rows
              else PlanSource.EMPTY if "phases" in fields else PlanSource.ABSENT)
    attempts: dict[str, TaskAttempt] = {}
    scope = ""
    for event in events:
        kind = event.get("event_type")
        if kind == "run_started":
            scope = str(event.get("project_plan_doc") or event.get("plan_doc") or "")
        if kind not in ("phase_started", "phase_finished"):
            continue
        instance = str(event.get("phase_instance_id") or event.get("phase_id") or "")
        previous = attempts.get(instance)
        phase = str(event.get("phase_id") or (previous.phase if previous is not None else ""))
        if not phase:
            continue
        title = str(event.get("phase_title") or (previous.title if previous is not None else ""))
        task_scope = str(event.get("plan_doc") or event.get("project_plan_doc") or scope)
        at = event.get("timestamp_epoch")
        finished = (datetime.fromtimestamp(at, UTC) if kind == "phase_finished" and isinstance(at, int | float)
                    else phase_table.PhaseNotFinished())
        outcome: Literal["current", "stopped", "completed"] = (
            "current" if kind == "phase_started" else "completed" if event.get("status") == "completed" else "stopped")
        attempts[instance] = TaskAttempt(phase, task_scope, title, instance, outcome, finished,
                                        single_spaced(str(event.get("reason") or event.get("stop_reason") or "")))
    return UnitTaskFacts(source, rows, tuple(attempts.values()))


def past_tasks(facts: UnitTaskFacts, *, zone: ZoneInfo) -> tuple[tuple[str, WorkItem], ...]:
    """Resolve stopped attempts only through a later completion of the same scoped task."""
    rows: list[tuple[str, WorkItem]] = []
    shown: set[tuple[str, str]] = set()
    for index, attempt in enumerate(facts.attempts):
        key = (attempt.scope, attempt.phase)
        if attempt.outcome != "stopped" or key in shown:
            continue
        shown.add(key)
        later = next((other for other in reversed(facts.attempts[index + 1:])
                      if (other.scope, other.phase) == key and other.outcome == "completed"), None)
        if later is not None:
            owner = re.search(r"\bthe ([\w-]+) unit owns " + re.escape(attempt.phase) + r"\b", later.title)
            if owner:
                text = f"The {owner.group(1)} unit finished this work"
            elif isinstance(later.finished, datetime):
                text = "A later attempt finished on " + later.finished.astimezone(zone).strftime("%d %b").lstrip("0")
            else:
                text = "A later attempt finished; finish date not recorded"
            rows.append(("Done", WorkItem(work_name(attempt.phase, attempt.title), text)))
        else:
            rows.append(("Stopped", WorkItem(work_name(attempt.phase, attempt.title),
                                             "Stopped; " + (attempt.reason or "reason not recorded"))))
    return tuple(rows)


def event_time(event: dict[str, object], *, now: float, zone: ZoneInfo) -> str:
    at = event.get("timestamp_epoch")
    return short_time(datetime.fromtimestamp(at, UTC), now=now, zone=zone) if isinstance(at, int | float) else "time not recorded"


def finished_queue_activity(target: str, events: Sequence[dict[str, object]]) -> dict[str, object]:
    starts: dict[str, str] = {}
    found: dict[str, object] = {}
    for event in events:
        kind = event.get("event_type")
        identity = str(event.get("activity_instance_id", ""))
        label = str(event.get("activity_text") or event.get("activity_label") or starts.get(identity, ""))
        if kind == "activity_started":
            starts[identity] = label
        if (kind == "activity_finished" and target
                and re.match(re.escape(target) + r"(?=$|[\s:—–-])", label)):
            found = event
    return found


class TaskLine(NamedTuple):
    """One current task and its execution state."""

    work: str
    state_text: str
    kind: Literal["running", "waiting", "unit_wait", "unexplained", "result"]


class ReadyNext(NamedTuple):
    """The first next item has no recorded wait."""

    work: str


class BlockedNext(NamedTuple):
    """A recorded dependency prevents the first next item."""

    reason: str
    wait: phase_waits.UnitWait | phase_waits.NoUnitWait = phase_waits.NO_UNIT_WAIT


class NoNextWork(NamedTuple):
    """No queue item or pending plan phase remains."""


class NextUnavailable(NamedTuple):
    """The queue could not establish the next item."""


class NoPlanNext(NamedTuple):
    """The plan has no pending phase to display."""


class NextWork(NamedTuple):
    """The ordered queue, pending plan text, and first next item's state."""

    queue: tuple[WorkItem, ...]
    plan: WorkItem | NoPlanNext
    empty: str
    first: ReadyNext | BlockedNext | NoNextWork | NextUnavailable
    wait_targets: frozenset[str]
    later: tuple[WorkItem, ...] = ()


class UnitBoardState(NamedTuple):
    """The facts that supply one unit block and one summary count."""

    name: str
    machine: str
    tasks: tuple[TaskLine, ...]
    next_work: NextWork
    merge: UnitMergeNotes
    fallback: str
    held: bool
    busy: bool
    has_open: bool
    readable: bool
    workers_unavailable: bool
    no_workers: bool
    past: tuple[tuple[str, WorkItem], ...] = ()
    current_work: str = ""

    @property
    def bucket(self) -> str:
        if (any(task.kind == "running" for task in self.tasks)
                or self.busy and any(task.kind in ("unexplained", "result") for task in self.tasks)):
            return "working"
        if self.held or (not self.busy and self.readable and not self.has_open and not self.merge.pending
                         and not any(task.kind == "unit_wait" for task in self.tasks)
                         and isinstance(self.next_work.first, NoNextWork)):
            return "idle"
        if any(task.kind == "waiting" for task in self.tasks):
            return "waiting on other work"
        if self.merge.pending:
            return "waiting to merge"
        if isinstance(self.next_work.first, BlockedNext) or any(task.kind == "unit_wait" for task in self.tasks):
            return "waiting on other work"
        return "unknown"


def work_name(phase_id: str, title: str) -> str:
    """Name the complete work title with its phase id once."""
    title = single_spaced(title)
    title = re.sub(rf"(?i)^(?:phase\s+)?{re.escape(phase_id)}\s*[:—–-]\s*", "", title)
    return f"{title} ({phase_id})" if title else phase_id


SENTENCE_START_WORDS = frozenset({"The", "A", "An", "This", "That", "These", "Those", "Each", "Every", "All", "No",
                                  "Some", "One", "Two", "Three", "Four", "Five", "Six"})


def in_sentence(work: str) -> str:
    """Lower a title's first word inside a sentence when it is an article or a number; names keep their capital."""
    first, _, rest = work.partition(" ")
    return f"{first.lower()} {rest}" if rest and first in SENTENCE_START_WORDS else work


def unit_wait_reason(wait: phase_waits.UnitWait, target: str) -> str:
    """Name the actor and the condition that permits the target work."""
    actor = wait.by[:1].upper() + wait.by[1:]
    return f"{actor} keeps {in_sentence(target)} waiting until {wait.reason.rstrip().rstrip('.')}"


def unit_wait_state(wait: phase_waits.UnitWait, *, now: float, zone: ZoneInfo) -> str:
    """Add the wait's original start time to its reason."""
    since = short_time(datetime.fromtimestamp(wait.started, zone), now=now, zone=zone)
    text = unit_wait_reason(wait, 'it')
    return f"{text[:1].upper() + text[1:]}; since {since}"


def wait_state_text(wait: phase_waits.PhaseWait, *, snapshot: staff.StaffSnapshot,
                    now: float, zone: ZoneInfo) -> str:
    """Name the wait's resource, latest holder, and original start."""
    since = short_time(datetime.fromtimestamp(wait.started, UTC), now=now, zone=zone)
    if isinstance(wait.holder, phase_waits.NamedWaitHolder):
        member = snapshot.by_member_reference(wait.holder.label)
        holder = member.name if member is not None else wait.holder.label
        if wait.resource_kind == "memory" or wait.resource == "memory":
            return f"Needs build memory in use by {holder}; since {since}"
        if wait.resource_kind == "slot":
            return f"Waits for a build slot used by {holder}; since {since}"
        return f"Waits for {single_spaced(wait.reason)}, in use by {holder}; since {since}"
    return f"Waits for {single_spaced(wait.reason)}; holder not recorded; since {since}"


def open_passes(events: Sequence[dict[str, object]]) -> tuple[dict[str, object], ...]:
    """Retain each pass until its matching finish."""
    opened: dict[str, dict[str, object]] = {}
    for event in events:
        kind = event.get("event_type")
        key = str(event.get("pass_instance_id", ""))
        if kind == "pass_started":
            opened[key] = event
        elif kind == "pass_finished":
            _ = opened.pop(key, None)
    return tuple(opened.values())


def director_activities(events: Sequence[dict[str, object]]) -> tuple[str, ...]:
    """Retain activities that have no worker pass attribution."""
    opened: dict[str, dict[str, object]] = {}
    for event in events:
        key = str(event.get("activity_instance_id", ""))
        if event.get("event_type") == "activity_started":
            opened[key] = event
        elif event.get("event_type") == "activity_finished":
            _ = opened.pop(key, None)
    return tuple(single_spaced(str(event.get("activity_text") or event.get("activity_label") or "works"))
                 for event in opened.values() if not event.get("pass_instance_id") and not event.get("worker_id"))


def role_words(role: str, name: str) -> str:
    """Use a recorded pass role to describe the worker's action."""
    words = {"impl": "Implementation worker", "implement": "Implementation worker", "test": "Test worker",
             "fix": "Fix worker", "review": "Review worker", "astra-designer": "Designer", "astra designer": "Designer"}
    return words.get(role, name)


def finished_step_text(events: Sequence[dict[str, object]], *, now: float, zone: ZoneInfo) -> str:
    """Name the last finished step without claiming a current action."""
    latest = next((event for event in reversed(events)
                   if event.get("event_type") in ("pass_finished", "activity_finished")), dict[str, object]())
    if not latest:
        return "Director's current step not recorded"
    when = event_time(latest, now=now, zone=zone)
    if latest.get("event_type") == "pass_finished":
        role = str(latest.get("pass_kind", ""))
        if not role:
            identity = latest.get("pass_instance_id")
            role = next((str(event.get("pass_kind", "")) for event in reversed(events)
                         if event.get("event_type") == "pass_started" and event.get("pass_instance_id") == identity), "")
        label = role_words(role, "Worker")
    else:
        label = single_spaced(str(latest.get("activity_text") or latest.get("activity_label") or "Director activity"))
    return f"{label} finished at {when}; director's next step not recorded"


def phase_tasks(phase: phase_table.OpenPhase, events: list[dict[str, object]], *,
                workers: tuple[census.CensusWorker, ...], snapshot: staff.StaffSnapshot,
                now: float, zone: ZoneInfo) -> tuple[TaskLine, ...]:
    """Join the selected phase's passes, workers, activities, and waits."""
    instance = [event for event in events if event.get("phase_instance_id") == phase.instance_id]
    passes = open_passes(instance)
    waits = phase_waits.phase_waits(events, phase.instance_id)
    work = work_name(phase.phase, phase.title)
    tasks: list[TaskLine] = []
    used_waits: set[int] = set()
    matched: set[staff.StaffId] = set()
    for event in passes:
        worker_id = event.get("worker_id")
        worker = next((row for row in workers if row.staff_id == worker_id), None)
        if worker is None:
            worker = next((row for row in workers if row.staff_id not in matched
                           and isinstance(row.assignment, census.CensusPass)
                           and row.assignment.phase == phase.instance_id), None)
        if worker is None or worker.staff_id in matched:
            continue
        matched.add(worker.staff_id)
        worker_waits = [(index, wait) for index, wait in enumerate(waits)
                        if wait.pass_instance_id and wait.pass_instance_id == event.get("pass_instance_id")]
        if worker_waits:
            for index, wait in worker_waits:
                tasks.append(TaskLine(work, wait_state_text(wait, snapshot=snapshot, now=now, zone=zone), "waiting"))
                used_waits.add(index)
            continue
        role = role_words(str(event.get("pass_kind", "")), worker.name)
        runs = [run for run in worker.runs if run.state == "running"]
        if runs:
            tasks.extend(TaskLine(work, f"{role} runs {run.label}", "running")
                         for run in runs)
        elif any(run.state == "finished_queued" for run in worker.runs):
            tasks.append(TaskLine(work, "Worker finished; director has not read the result", "result"))
        elif worker.runs:
            tasks.append(TaskLine(work, "Worker activity not recorded", "unexplained"))
        elif worker.status == "busy":
            tasks.append(TaskLine(work, f"{role} runs it", "running"))
    for index, wait in enumerate(waits):
        if index not in used_waits:
            tasks.append(TaskLine(work, wait_state_text(wait, snapshot=snapshot, now=now, zone=zone), "waiting"))
    activities = director_activities(instance)
    director_waits = any(not wait.pass_instance_id for wait in waits)
    if activities and not director_waits:
        for activity in activities:
            action = re.sub(r"^phase " + re.escape(phase.phase) + r" review\b", "reviews it", activity.rstrip("."))
            tasks.append(TaskLine(work, f"Director {action}", "running"))
    if not tasks:
        tasks.append(TaskLine(work, finished_step_text(instance, now=now, zone=zone), "unexplained"))
    tasks = list(dict.fromkeys(tasks))
    if phase.eta_trail:
        promise = phase.eta_trail[-1][0]
        eta = f"; ETA {short_time(promise, now=now, zone=zone)}"
        if now > promise.timestamp():
            eta += f", late {elapsed_text(now - promise.timestamp())}"
        first = next((index for index, task in enumerate(tasks) if task.kind == "running"), 0)
        tasks[first] = TaskLine(tasks[first].work, tasks[first].state_text + eta, tasks[first].kind)
    return tuple(tasks)


def shell_waits(view: CensusView | census.CensusFailed, unit: live_units.LiveUnit, *, now: float,
                zone: ZoneInfo, snapshot: staff.StaffSnapshot, events: list[dict[str, object]],
                phases: tuple[phase_table.OpenPhase, ...]) -> tuple[TaskLine, ...]:
    """Describe the director's commands with their recorded resource waits."""
    if isinstance(view, census.CensusFailed):
        return ()
    session_id = _unit_session(unit)
    session = view.sessions.get(session_id) if session_id is not None else None
    if session is None:
        return ()
    waits = [(phase, wait) for phase in phases for wait in phase_waits.phase_waits(events, phase.instance_id)
             if not wait.pass_instance_id and (not wait.owner_session or wait.owner_session == session_id)]
    if waits and session.shells:
        return tuple(TaskLine(work_name(phase.phase, phase.title),
                              wait_state_text(wait, snapshot=snapshot, now=now, zone=zone), "waiting")
                     for phase, wait in waits)
    return tuple(TaskLine(shell.name, "Runs; the director waits for its result", "running") for shell in session.shells)


def next_work(queue: UnitWorkQueue | UnreadableWorkQueue, record: object, *, open_ids: tuple[str, ...],
              read_plan: Callable[[Path], list[plan_history.PlanPhase]],
              wait_state: Callable[[str], waits_on.WaitState], land: land_queue.LandView,
              now: float, zone: ZoneInfo, unit_waits: Sequence[phase_waits.UnitWait] = (), unit: str = "",
              dependency_title: Callable[[str], str] = lambda _: "",
              events: Sequence[dict[str, object]] = (), facts: UnitTaskFacts) -> NextWork:
    """Name every pending task from the cached plan, run, and queue."""
    fields = record_fields(record)
    rows = facts.plan_rows
    path = fields.get("plan", "")
    has_pending = any(row.get("status") not in ("done", "running", "open")
                      and row.get("phase") not in open_ids for row in rows)
    details = {row["id"]: row for row in read_plan(Path(path))} if path and has_pending else {}
    waits = {wait.target: wait for wait in unit_waits}
    targets: set[str] = set()
    items: list[WorkItem] = []
    queue_ids: set[str] = set()
    first: ReadyNext | BlockedNext | NoNextWork | NextUnavailable = NoNextWork()
    if isinstance(queue, UnitWorkQueue):
        for item in queue.items:
            target = item.text.partition(":")[0]
            colon = ":" in item.text
            target = target.strip()
            if colon and target in open_ids:
                continue
            queue_ids.add(target)
            found = wait_state(item.after) if item.after else None
            entry = queued_text(item, found, land, now=now, zone=zone, unit=unit)
            held_dependency = (isinstance(found, waits_on.PhaseSent) and isinstance(found.wait, phase_waits.UnitWait)
                               and isinstance(land_queue.land_state(land, found.commit),
                                              land_queue.NotInLine | land_queue.LineUnused))
            if item.after and not isinstance(found, waits_on.OnMain) and not held_dependency:
                entry = WorkItem(entry.work, dependency_text(item.after, unit, title=dependency_title(item.after), merge=True))
            finished = finished_queue_activity(target, events)
            if finished:
                when = event_time(finished, now=now, zone=zone)
                entry = WorkItem("Not recorded", f"The queue still lists {target}, which finished at {when}; the director must clear it")
            if colon and target in waits:
                targets.add(target)
                entry = WorkItem(entry.work, unit_wait_state(waits[target], now=now, zone=zone))
            if not items:
                first = (BlockedNext(unit_wait_reason(waits[target], target), waits[target]) if target in waits
                         else BlockedNext(entry.state_text)
                         if item.after and not isinstance(found, waits_on.OnMain) else ReadyNext(entry.work))
            items.append(entry)
    pending: list[WorkItem] = []
    done = {row.get("phase") for row in rows if row.get("status") == "done"}
    for row in rows:
        target = row.get("phase", "")
        if row.get("status") in ("done", "running", "open") or target in open_ids or target in queue_ids:
            continue
        text = "Not started"
        detail = details.get(target)
        if detail is not None:
            dependencies = [dependency for dependency in detail.get("depends_on", ()) if dependency not in done]
            if dependencies:
                text = "\n".join(dependency_text(dependency, unit, title=dependency_title(dependency))
                                 for dependency in dependencies)
            elif condition := detail.get("waits_for", ""):
                condition = re.sub(r"\([^)]*\bis met:[^)]*\)\s*", "", condition)
                text = f"Waits for {condition.rstrip().rstrip('.')}"
        if target in waits:
            targets.add(target)
            text = unit_wait_state(waits[target], now=now, zone=zone)
        work = work_name(target, row.get("title", ""))
        if not items and not pending:
            first = BlockedNext(text, waits.get(target, phase_waits.NO_UNIT_WAIT)) if text != "Not started" else ReadyNext(work)
        pending.append(WorkItem(work, text))
    if isinstance(queue, UnreadableWorkQueue):
        first = NextUnavailable()
    complete = facts.plan_source == PlanSource.READABLE and all(row.get("status") == "done" for row in rows)
    empty = "Nothing queued; the plan is complete" if complete else "The director has not chosen the next task"
    if isinstance(queue, UnreadableWorkQueue):
        empty = "Queue unavailable; next task not recorded"
    return NextWork(tuple(items), pending[0] if pending else NoPlanNext(), empty, first,
                    frozenset(targets), tuple(pending[1:]))



def hold_text(member: staff.StaffRow, *, now: float, zone: ZoneInfo) -> str:
    """Describe a recorded pause or stop."""
    hold = member.paused if isinstance(member.paused, staff.Paused) else member.exited
    if isinstance(hold, staff.Active):
        return ""
    try:
        since = short_time(datetime.fromisoformat(hold.since), now=now, zone=zone)
    except ValueError:
        since = hold.since or "start not recorded"
    verb = "paused" if isinstance(hold, staff.Paused) else "stopped"
    actor = f"{hold.by.strip()} {verb} this unit" if hold.by.strip() else f"The unit is {verb}"
    return f"{actor}: {hold.why.strip() or 'reason not recorded'}; since {since}"


def item_rows(label: str, entries: Sequence[WorkItem | TaskLine], *, numbered: bool = False) -> list[tuple[str, ...]]:
    """Label work entries before the table wraps their cells."""
    if not entries:
        return []
    rows: list[tuple[str, ...]] = []
    for index, entry in enumerate(entries, 1):
        name = f"{label} {index}" if numbered or len(entries) > 1 and label != "Also" else label
        for line, text in enumerate(entry.state_text.splitlines() or [""]):
            rows.append((name, entry.work if line == 0 else "", text.rstrip().rstrip(".")))
    return rows


def unit_block(state: UnitBoardState, *, columns: int | None) -> str:
    """Render the unit's current tasks, merge groups, and next work."""
    machine = f" ({state.machine})" if state.machine else ""
    running = [task for task in state.tasks if task.kind == "running"]
    other = [task for task in state.tasks if task.kind != "running"]
    current: list[WorkItem | TaskLine] = [*running, *state.merge.entries, *other]
    if state.current_work:
        current.sort(key=lambda entry: entry.work != state.current_work)
    states: dict[str, list[str]] = {}
    for entry in current:
        lines = states.setdefault(entry.work, [])
        for line in entry.state_text.splitlines():
            if line not in lines:
                lines.append(line)
    current = [WorkItem(work, "\n".join(lines)) for work, lines in states.items()]
    if not current and not state.workers_unavailable:
        current.append(WorkItem("Not recorded", state.fallback))
    if state.workers_unavailable:
        current.append(WorkItem("Not recorded", "Workers unavailable; who runs cannot be determined"))
    if state.no_workers:
        current.append(WorkItem("Not recorded", "Workers cannot start because no-worker mode is on"))
    rows = item_rows("Now", current[:1])
    rows.extend(item_rows("Also", current[1:]))
    next_item = state.next_work
    pending = list(next_item.queue)
    if isinstance(next_item.plan, WorkItem):
        pending.append(next_item.plan)
    pending.extend(next_item.later)
    if pending:
        rows.extend(item_rows("Next", pending[:1]))
        rows.extend(item_rows("Later", pending[1:]))
    else:
        rows.extend(item_rows("Next", (WorkItem("Not recorded", next_item.empty),)))
    for label in ("Stopped", "Done"):
        rows.extend((label, entry.work, entry.state_text) for kind, entry in state.past if kind == label)
    headers, rows = display_cells.wrapped(("Item", "Work", "State"), rows, columns, keep=(0, 1, 2))
    lines = [f"**{state.name}**{machine} — {state.bucket}", "",
             "| " + " | ".join(display_cells.escaped(header) for header in headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(display_cells.escaped(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def table(units: list[live_units.LiveUnit], records: dict[str, object], *,
          now: float | None = None, zone: ZoneInfo | None = None,
          snapshot: staff.StaffSnapshot | None = None, census_read: census.Census | census.CensusFailed,
          columns: int | None = None) -> str:
    """Build each unit's state once for its status row, block and summary bucket."""
    snapshot = plan_history.staff_record() if snapshot is None else snapshot
    now = plan_history.now_epoch() if now is None else now
    zone = user_zone() if zone is None else zone
    view = census_view(census_read)
    land = land_queue.land_view(REPOSITORY)
    events: dict[Path, list[dict[str, object]]] = {}
    plans: dict[Path, list[plan_history.PlanPhase]] = {}
    plan_failures: dict[Path, str] = {}
    queues: dict[staff.StaffId, UnitWorkQueue | UnreadableWorkQueue] = {}
    wait_states: dict[str, waits_on.WaitState] = {}
    main_states: dict[str, bool] = {}

    def read_run(path: Path) -> list[dict[str, object]]:
        if path not in events:
            events[path] = plan_history.read_run_events(path)
        return events[path]

    def read_plan(path: Path) -> list[plan_history.PlanPhase]:
        if path not in plans:
            try:
                plans[path] = plan_history.plan_phases(path)
            except (OSError, UnicodeError) as error:
                plans[path] = []
                plan_failures[path] = str(error)
        return plans[path]

    def read_queue(identity: staff.StaffId) -> UnitWorkQueue | UnreadableWorkQueue:
        if identity not in queues:
            queues[identity] = read_work_queue(identity)
        return queues[identity]

    def wait_state(after: str) -> waits_on.WaitState:
        if after not in wait_states:
            wait_states[after] = waits_on.state(after, repo=REPOSITORY, snapshot=snapshot, read_run=read_run)
        return wait_states[after]

    def dependency_title(reference: str) -> str:
        owner, slash, phase = reference.partition("/")
        if not slash:
            return ""
        for row in plan_phase_rows(record_fields(records.get(owner))):
            if row.get("phase") == phase and row.get("title"):
                return row.get("title", "")
        member = next((member for member in snapshot.rows if member.name == owner and member.kind == "unit"), None)
        if member is not None and isinstance(member.run, staff.OpenRun):
            history = events.get(plan_history.run_events_path(member.run.path), [])
            for event in reversed(history):
                if event.get("phase_id") == phase and event.get("phase_title"):
                    return str(event["phase_title"])
        return ""

    def on_main(commit: str) -> bool:
        if commit not in main_states:
            main_states[commit] = waits_on.on_main(REPOSITORY, commit)
        return main_states[commit]

    unit_snapshot = staff.StaffSnapshot(tuple(unit.member for unit in units if isinstance(unit.member, staff.StaffRow)))
    newest = newest_unit_runs(unit_snapshot)
    runs = open_unit_runs(unit_snapshot, read_run=read_run, newest=newest)
    machines = display_cells.machine_column([unit.machine for unit in units])
    no_workers = quota_state.no_worker_mode()
    states: list[UnitBoardState] = []
    table_rows: list[tuple[str, ...]] = []
    show_plan_eta = False
    for index, unit in sorted(enumerate(units), key=lambda pair: pair[1].unit.casefold()):
        record = records.get(unit.unit)
        progress_record = record
        member = unit.member
        status = census_status(view, unit)
        busy = status == "busy"
        run_events: list[dict[str, object]] = []
        phases: tuple[phase_table.OpenPhase, ...] = ()
        queue: UnitWorkQueue | UnreadableWorkQueue = UnreadableWorkQueue()
        workers: tuple[census.CensusWorker, ...] = ()
        hold = ""
        if isinstance(member, staff.StaffRow):
            queue = read_queue(member.id)
            hold = hold_text(member, now=now, zone=zone)
            run = newest.get(member.id)
            opened = runs.get(member.id, NoOpenUnitRun())
            if run is not None:
                run_events = read_run(run.path)
                planless = not plan_phase_rows(record_fields(record))
                if planless:
                    record = fallback_plan(member, run, opened, record, read_plan=read_plan)
                plan_path = record_fields(record).get("plan", "")
                if Path(plan_path) in plan_failures:
                    record = {**record_fields(record), "unavailable": plan_failures[Path(plan_path)]}
                if isinstance(opened, OpenUnitRun):
                    plan_path = record_fields(record).get("plan", "")
                    built = phase_table.build_run(Path(plan_path), run_events)
                    positions: dict[str, int] = {row.get("phase", ""): position
                                                 for position, row in enumerate(plan_phase_rows(record_fields(record)))}
                    phases = tuple(sorted((phase for phase in built.phases if isinstance(phase, phase_table.OpenPhase)),
                                          key=lambda phase: positions.get(phase.phase, len(positions))))
                    if planless:
                        progress_record = built
            if isinstance(view, CensusView) and member.id in view.workers:
                workers = view.workers[member.id]
        rows, plan_eta = status_rows(unit.unit, status, (machines[index],) if machines else (), progress_record, run_events,
                                     phases, land, now=now, zone=zone)
        table_rows.extend(rows)
        show_plan_eta = show_plan_eta or plan_eta
        facts = task_facts(record, run_events)
        titles = {row.get("phase", ""): row.get("title", "") for row in facts.plan_rows}
        for attempt in facts.attempts:
            if attempt.phase not in titles or not titles[attempt.phase]:
                titles[attempt.phase] = attempt.title
        owner = snapshot.parent_of(member) if isinstance(member, staff.StaffRow) else None
        production_name = owner.name if owner is not None else ""
        open_sent = {phase.phase: phase_sent_work([
            event for event in run_events if event.get("phase_instance_id") == phase.instance_id]) for phase in phases}
        waits = phase_waits.unit_waits(run_events)
        done = completed_unit_phases(run_events) if waits else {}
        for wait in waits:
            if wait.target in done and wait.target not in open_sent:
                open_sent[wait.target] = done[wait.target]
        merge = unit_lands(unit.unit, land, open_sent=open_sent, now=now, zone=zone, on_main=on_main,
                           unit_waits=waits, done_ids=tuple(done), phase_titles=tuple(titles.items()),
                           production=production_name)
        open_ids = tuple(dict.fromkeys([*(phase.phase for phase in phases),
                                       *(attempt.phase for attempt in facts.attempts)]))
        next_item = next_work(queue, record, open_ids=open_ids, read_plan=read_plan, wait_state=wait_state,
                              land=land, now=now, zone=zone,
                              unit_waits=tuple(wait for wait in waits if wait.target not in merge.wait_targets),
                              unit=unit.unit, dependency_title=dependency_title, events=run_events, facts=facts)
        tasks = tuple(task for phase in phases if isinstance(open_sent[phase.phase], NoSentWork)
                      for task in phase_tasks(phase, run_events, workers=workers, snapshot=snapshot,
                                               now=now, zone=zone))
        shells = shell_waits(view, unit, now=now, zone=zone, snapshot=snapshot, events=run_events, phases=phases)
        # A shell and a phase can report the same resource wait.
        tasks += tuple(task for task in shells if not any(existing.work == task.work
                       and existing.state_text.startswith(task.state_text) for existing in tasks))
        tasks += tuple(TaskLine(wait.target, unit_wait_state(wait, now=now, zone=zone), "unit_wait")
                       for wait in waits if wait.target not in merge.wait_targets | next_item.wait_targets)
        fallback = hold or finished_step_text(run_events, now=now, zone=zone)
        if not hold and isinstance(next_item.first, BlockedNext):
            fallback = next_item.first.reason
        readable = (not isinstance(queue, UnreadableWorkQueue) and "unavailable" not in record_fields(record)
                    and not isinstance(view, census.CensusFailed))
        current = max((phase for phase in phases if isinstance(open_sent[phase.phase], NoSentWork)),
                      key=lambda phase: phase.started, default=None)
        current_work = work_name(current.phase, current.title) if current is not None else ""
        states.append(UnitBoardState(unit.unit, machines[index] if machines else "", tasks, next_item, merge,
                                     fallback, bool(hold), busy, bool(phases), readable,
                                     isinstance(view, census.CensusFailed), no_workers, past_tasks(facts, zone=zone), current_work))
    buckets = ("working", "waiting to merge", "waiting on other work", "idle", "unknown")
    counts = {bucket: sum(state.bucket == bucket for state in states) for bucket in buckets}
    summary = " · ".join(f"{bucket} {counts[bucket]}" for bucket in buckets if bucket != "unknown" or counts[bucket])
    summary += f" · {len(states)} units"
    if not states:
        return summary
    return "\n\n".join((summary, status_table(table_rows, machines=bool(machines), show_plan_eta=show_plan_eta,
                                                 columns=columns), *(unit_block(state, columns=columns) for state in states)))


def production_records(doc: Path) -> dict[str, object]:
    """Build phase facts once for each configured unit plan."""
    snapshot = plan_history.staff_record()
    resolver = session_staff.RowResolver(snapshot)
    records: dict[str, object] = {}
    plans: dict[Path, phase_table.PhaseRecord | dict[str, object]] = {}
    for member in unit_lookup.doc_units(doc, snapshot):
        try:
            tree = resolver.unit_worktree(member)
            config = resolver.unit_config(member)
            if not isinstance(tree, staff.PresentWorktree):
                records[member.name] = {"unavailable": "the worktree is not recorded"}
            elif not isinstance(config, unit_config.UnitConfig):
                records[member.name] = {"unavailable": ("configuration is unavailable"
                                                     if isinstance(config, session_staff.NoUnitConfig) else config.reason)}
            elif isinstance(config.plan, unit_config.PlanFile):
                path = (tree.path / config.plan.path).resolve()
                if path not in plans:
                    try:
                        plans[path] = phase_table.build_plan(path, resolver=resolver)
                    except phase_table.NoPlan as error:
                        plans[path] = {"unavailable": str(error)}
                records[member.name] = plans[path]
        except ValueError as error:
            records[member.name] = {"unavailable": str(error)}
    return records


def is_style_production(doc: Path) -> bool:
    try:
        return production_field(doc.read_text(encoding="utf-8").splitlines(), "Kind") == "style"
    except Refusal:
        return False


def style_ledger_paths(doc: Path, units: list[live_units.LiveUnit]) -> dict[str, Path]:
    """Resolve ledgers from the staff rows, even when phase records fail."""
    from scripts.style import style_ledger

    try:
        rows = {row.name: row for row in unit_lookup.doc_units(doc, plan_history.staff_record())}
    except (OSError, staff.StaffRefused) as error:
        print(f"unit_phases: {error}", file=sys.stderr)
        return {}
    paths: dict[str, Path] = {}
    for unit in units:
        row = rows.get(unit.unit)
        if (row is None or not isinstance(row.worktree, staff.PresentWorktree)
                or row.worktree.machine != remote.local_machine()):
            continue
        try:
            paths[unit.unit] = style_ledger.ledger_location(row.worktree.path).database
        except (OSError, ValueError) as error:
            print(f"unit_phases: {unit.unit}: {error}", file=sys.stderr)
    return paths


def send_anomalies(lines: tuple[str, ...]) -> None:
    """Send one notice; a delivery failure does not change the table result."""
    if not lines:
        return
    try:
        target = conf.read("production").text("UNIT_ANOMALIES_TO")
        done = subprocess.run(session_send.send_argv("--to", target, "--from", "unit_phases.py", "--text", "\n".join(lines),
                               send_script=showrunners.SEND),
                              capture_output=True, text=True, check=False)
        if done.returncode:
            detail = " ".join(failure_detail(done).splitlines())
            print(f"anomaly: could not send unit anomalies to {target}: {detail}")
    except (OSError, conf.Unusable) as error:
        print(f"anomaly: could not send unit anomalies: {' '.join(str(error).splitlines())}")


def main(arguments: list[str], columns: int | None = None) -> int:
    match arguments:
        case ["--help"] | ["-h"]:
            print("usage: unit_phases.py [--production <staff name>]")
            return 0
        case ["--production", name]:
            pass
        case []:
            name = showrunners.own_name()
            if not name:
                print("unit_phases: this session is no production's showrunner; pass --production <staff name>",
                      file=sys.stderr)
                return 1
        case _:
            print("usage: unit_phases.py [--production <staff name>]", file=sys.stderr)
            return 2
    with session_staff.anomalies() as rendering_lines, herdr.observation_snapshot():
        try:
            snapshot = staff.snapshot()
            with plan_history.history_snapshot(snapshot):
                production = read_production(name, record=snapshot)
                doc = production.doc
                style = is_style_production(doc)
                with ThreadPoolExecutor(max_workers=3) as reads:
                    tiles = None if style else reads.submit(copy_context().run, census.read)
                    sessions = reads.submit(copy_context().run, live_units.read_units, name, record=snapshot,
                                            check=staff_sources.CodexProcessThreads().check)
                    records = (cast(dict[str, object], json.loads(phase_table.show_production(doc)))
                               if style else production_records(doc))
                    listing = sessions.result()
                    census_read = tiles.result() if tiles is not None else census.CensusFailed("style production")
                    units = sorted(listing.units, key=lambda unit: unit.unit.casefold())
                configured: set[str] = set()
                if not style and style_config.has_style_units(doc.read_text().splitlines()):
                    configured = {assignment.unit for assignment in style_config.read_config(doc).assignments}
                ticket_units = [unit for unit in units if style or unit.unit in configured]
                product_units = [unit for unit in units if unit not in ticket_units]
                paths = style_ledger_paths(doc, ticket_units) if ticket_units else {}
                if not units:
                    output = "no live units"
                else:
                    sections: list[str] = []
                    if product_units:
                        if isinstance(census_read, census.CensusFailed):
                            session_staff.report_anomaly(census_read.reason)
                        sections.append(table(product_units, records, zone=production.zone, census_read=census_read,
                                              columns=columns))
                    if ticket_units:
                        from scripts.production import style_units
                        sections.append(style_units.table(ticket_units, records, doc=doc, ledger_paths=paths))
                    output = "\n\n".join(sections)
        except (OSError, ValueError, Refusal) as error:
            print(f"unit_phases: {error}", file=sys.stderr)
            return 1
    print(output)
    lines = tuple(dict.fromkeys((*listing.anomalies, *rendering_lines)))
    for line in lines:
        print(line)
    send_anomalies(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], display_cells.pane_columns()))
