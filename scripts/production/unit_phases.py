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
            text = (f"merges with {place.owner}" if carried else "waiting to merge")
            text += f", {ordinal(place.number)} in line"
            return f"{text}; last attempt {try_text(failed, now=now, zone=zone)}" if failed else text
        case land_queue.LandEnded(result) if result["landed"]:
            return try_text(result, now=now, zone=zone)
        case land_queue.LandEnded(result):
            return f"did not merge: {try_text(result, now=now, zone=zone)}"
        case land_queue.NotInLine():
            return "submitted; absent from the merge line; reason not recorded"
        case land_queue.LineUnused():
            return "submitted; merge position not recorded"


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


class UnitMergeNotes(NamedTuple):
    """The unit's merge sentences and unresolved merge evidence."""

    text: str
    grouped_phases: frozenset[str]
    entries: tuple[str, ...]
    pending: bool


def joined_names(names: Sequence[str]) -> str:
    """Join phase names for a sentence."""
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def sent_sentence(phase: str, state: land_queue.LandState, *, now: float, zone: ZoneInfo) -> str:
    """Describe one submitted phase outside a shared merge group."""
    text = land_text(state, now=now, zone=zone)
    if isinstance(state, land_queue.NotInLine | land_queue.LineUnused):
        return f"{phase} was {text}."
    if isinstance(state, land_queue.LandEnded) and not state.result["landed"]:
        return f"{phase} {text}."
    return f"{phase} is done; {text}."


def unit_lands(unit: str, land: land_queue.LandView, *, open_sent: dict[str, PhaseSentWork], now: float, zone: ZoneInfo,
               on_main: Callable[[str], bool]) -> UnitMergeNotes:
    """Describe each merge group once and retain failed results without open rows."""
    prefix = f"{unit} "

    def own(owner: str) -> str:
        return owner.removeprefix(prefix) if owner.startswith(prefix) else ""

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
        if result["unit"] == unit and (phase not in newest or result["time"] > newest[phase]["time"]):
            newest[phase] = result
    parts: list[str] = []
    grouped: set[str] = set()
    pending = bool(merging or items)
    shown = set(open_merging)
    for phases in open_items.values():
        shown.update(phases)
    if merging:
        parts.append(f"{joined_names(merging)} {'is' if len(merging) == 1 else 'are'} done; merging now.")
        if len(merging) > 1:
            grouped.update(open_merging)
    for number, phases in sorted(items.items()):
        if len(phases) > 1:
            parts.append(f"{joined_names(phases)} are done; merge together, {ordinal(number)} in line.")
            grouped.update(open_items.get(number, ()))
        elif phases:
            phase = phases[0]
            sent = open_sent.get(phase)
            state = land_queue.land_state(land, sent.commit) if isinstance(sent, SentCommit) else None
            parts.append(sent_sentence(phase, state, now=now, zone=zone) if isinstance(state, land_queue.InLine)
                         else f"{phase} is done; waiting to merge, {ordinal(number)} in line.")
    for phase, sent in open_sent.items():
        if isinstance(sent, NoSentWork) or phase in shown:
            continue
        state = land_queue.land_state(land, sent.commit) if isinstance(sent, SentCommit) else land_queue.LineUnused()
        parts.append(sent_sentence(phase, state, now=now, zone=zone))
        pending = pending or not (isinstance(state, land_queue.LandEnded) and state.result["landed"])
    for phase, result in newest.items():
        if (not result["landed"] and phase not in open_sent and phase not in queued and phase not in merging
                and not on_main(result["commit"])):
            parts.append(f"{phase} did not merge: {try_text(result, now=now, zone=zone)}.")
            pending = True
    return UnitMergeNotes(" · ".join(parts), frozenset(grouped), tuple(parts), pending)


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
        case waits_on.PhaseSent(commit):
            state = land_queue.land_state(land, commit)
            return land_text(state, now=now, zone=zone)
        case waits_on.NotOnMain(commit):
            state = land_queue.land_state(land, commit)
            if isinstance(state, land_queue.NotInLine | land_queue.LineUnused):
                return "absent from the local copy of main"
            return f"absent from the local copy of main; {land_text(state, now=now, zone=zone)}"
        case waits_on.PhaseOpen():
            return "started"
        case waits_on.PhaseNotStarted():
            return "not started"
        case _:
            return "dependency not found locally"


def queued_text(item: unit_queue.QueuedItem, found: waits_on.WaitState | None,
                land: land_queue.LandView, *, now: float, zone: ZoneInfo) -> str:
    """Keep the full queue text and append its dependency state."""
    if not item.after or found is None:
        return item.text
    if isinstance(found, waits_on.OnMain):
        return f"{item.text.rstrip().rstrip('.')}. Ready: {item.after} is on the local copy of main."
    return f"{item.text.rstrip().rstrip('.')}. After {item.after}: {wait_text(found, land, now=now, zone=zone)}."


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
        return {}
    value = run.start.get("project_plan_doc") or run.start.get("plan_doc")
    if not isinstance(value, str) or not value:
        return {}
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


class TaskLine(NamedTuple):
    """One current task and its execution state."""

    text: str
    state: Literal["running", "waiting", "unexplained", "result"]


class ReadyNext(NamedTuple):
    """The first next item has no recorded wait."""

    work: str


class BlockedNext(NamedTuple):
    """A recorded dependency prevents the first next item."""

    reason: str


class NoNextWork(NamedTuple):
    """No queue item or pending plan phase remains."""


class NextUnavailable(NamedTuple):
    """The queue could not establish the next item."""


class NextWork(NamedTuple):
    """The ordered queue, pending plan text, and first next item's state."""

    queue: tuple[str, ...]
    plan: str
    empty: str
    first: ReadyNext | BlockedNext | NoNextWork | NextUnavailable


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

    @property
    def bucket(self) -> str:
        if (any(task.state == "running" for task in self.tasks)
                or self.busy and any(task.state in ("unexplained", "result") for task in self.tasks)):
            return "working"
        if self.held or (not self.busy and self.readable and not self.has_open and not self.merge.pending
                         and isinstance(self.next_work.first, NoNextWork)):
            return "idle"
        if any(task.state == "waiting" for task in self.tasks):
            return "waiting on other work"
        if self.merge.pending:
            return "waiting to merge"
        if isinstance(self.next_work.first, BlockedNext):
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


def wait_sentence(work: str, wait: phase_waits.PhaseWait, *, snapshot: staff.StaffSnapshot,
                  now: float, zone: ZoneInfo) -> str:
    """Name the wait's resource, latest holder, and original start."""
    since = short_time(datetime.fromtimestamp(wait.started, UTC), now=now, zone=zone)
    if isinstance(wait.holder, phase_waits.NamedWaitHolder):
        member = snapshot.by_member_reference(wait.holder.label)
        holder = member.name if member is not None else wait.holder.label
        if wait.resource_kind == "memory" or wait.resource == "memory":
            return f"{work} needs build memory in use by {holder}; since {since}."
        if wait.resource_kind == "slot":
            return f"{work} waits for a build slot used by {holder}; since {since}."
        return f"{work} waits for {single_spaced(wait.reason)}, in use by {holder}; since {since}."
    return f"{work} waits for {single_spaced(wait.reason)}; holder not recorded; since {since}."


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


def role_words(role: str, name: str) -> tuple[str, str]:
    """Use a recorded pass role to describe the worker's action."""
    words = {"impl": ("implementation worker", "writes"), "implement": ("implementation worker", "writes"),
             "test": ("test worker", "writes the tests for"), "fix": ("fix worker", "fixes the review findings for"),
             "review": ("review worker", "reviews"), "astra-designer": ("designer", "writes the design for"),
             "astra designer": ("designer", "writes the design for")}
    return words.get(role, (name, "works on"))


def phase_tasks(phase: phase_table.OpenPhase, events: list[dict[str, object]], *,
                workers: tuple[census.CensusWorker, ...], busy: bool, snapshot: staff.StaffSnapshot,
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
                tasks.append(TaskLine(wait_sentence(work, wait, snapshot=snapshot, now=now, zone=zone), "waiting"))
                used_waits.add(index)
            continue
        role, verb = role_words(str(event.get("pass_kind", "")), worker.name)
        runs = [run for run in worker.runs if run.state == "running"]
        if runs:
            tasks.extend(TaskLine(f"The {role} runs {run.label} for {in_sentence(work)} and waits for its result.", "running")
                         for run in runs)
        elif any(run.state == "finished_queued" for run in worker.runs):
            tasks.append(TaskLine(f"{work}: the worker finished; the director has not read its result.", "result"))
        elif worker.runs:
            tasks.append(TaskLine(f"{work} is open, but nothing runs; reason not recorded.", "unexplained"))
        elif worker.status == "busy":
            tasks.append(TaskLine(f"The {role} {verb} {in_sentence(work)}.", "running"))
    for index, wait in enumerate(waits):
        if index not in used_waits:
            tasks.append(TaskLine(wait_sentence(work, wait, snapshot=snapshot, now=now, zone=zone), "waiting"))
    activities = director_activities(instance)
    director_waits = any(not wait.pass_instance_id for wait in waits)
    if activities and not director_waits:
        tasks.extend(TaskLine(f"The director {activity} for {in_sentence(work)}. No worker is needed for this step.", "running")
                     for activity in activities)
    if not tasks:
        tasks.append(TaskLine(f"The director works on {in_sentence(work)}. No worker runs; the director's step is not recorded.", "running")
                     if busy else TaskLine(f"{work} is open, but nothing runs; reason not recorded.", "unexplained"))
    if phase.eta_trail:
        promise = phase.eta_trail[-1][0]
        eta = f" Estimated finish {short_time(promise, now=now, zone=zone)}"
        if now > promise.timestamp():
            eta += f", late {elapsed_text(now - promise.timestamp())}"
        first = next((index for index, task in enumerate(tasks) if task.state == "running"), 0)
        tasks[first] = TaskLine(tasks[first].text + eta + ".", tasks[first].state)
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
        return tuple(TaskLine(wait_sentence(work_name(phase.phase, phase.title), wait, snapshot=snapshot,
                                            now=now, zone=zone), "waiting") for phase, wait in waits)
    return tuple(TaskLine(f"{shell.name} runs; the director waits for its result.", "running") for shell in session.shells)


def next_work(queue: UnitWorkQueue | UnreadableWorkQueue, record: object, *, open_ids: tuple[str, ...],
              read_plan: Callable[[Path], list[plan_history.PlanPhase]],
              wait_state: Callable[[str], waits_on.WaitState], land: land_queue.LandView,
              now: float, zone: ZoneInfo) -> NextWork:
    """Keep queue order and explain the first pending plan phase."""
    fields = record_fields(record)
    rows = plan_phase_rows(fields)
    pending = [row for row in rows if row.get("status") not in ("done", "running", "open")
               and row.get("phase") not in open_ids]
    plan = ""
    first: ReadyNext | BlockedNext | NoNextWork | NextUnavailable = NoNextWork()
    if pending:
        phase_id = pending[0].get("phase", "")
        work = work_name(phase_id, pending[0].get("title", ""))
        plan = "Plan next: " + work
        if len(pending) > 1:
            plan += "; then " + ", ".join(row.get("phase", "") for row in pending[1:])
        plan += "."
        first = ReadyNext(work)
        path = fields.get("plan", "")
        details = read_plan(Path(path)) if path else []
        detail = next((row for row in details if row["id"] == phase_id), None)
        if detail is not None:
            if waits_for := detail.get("waits_for", ""):
                plan += f" The plan says it waits for: {waits_for.rstrip().rstrip('.')}."
                first = BlockedNext(f"The next plan phase waits for {waits_for.rstrip().rstrip('.')}.")
            done = {row.get("phase") for row in rows if row.get("status") == "done"}
            for dependency in detail.get("depends_on", ()):
                if dependency not in done:
                    plan += f" After phase {dependency}: not done."
                    if isinstance(first, ReadyNext):
                        first = BlockedNext(f"The next plan phase waits for phase {dependency}.")
    if isinstance(queue, UnreadableWorkQueue):
        return NextWork((), plan, "Queue unavailable; next work cannot be determined.", NextUnavailable())
    items: list[str] = []
    unstarted: list[unit_queue.QueuedItem] = []
    prefixes = tuple(phase_id + ":" for phase_id in open_ids)
    # A queue item that names current work is that work, not work after it.
    for item in queue.items:
        if item.text.startswith(prefixes):
            continue
        items.append(queued_text(item, wait_state(item.after) if item.after else None, land, now=now, zone=zone))
        unstarted.append(item)
    if unstarted:
        item = unstarted[0]
        first = (BlockedNext(f"The first queued item waits for {item.after}.")
                 if item.after and not isinstance(wait_state(item.after), waits_on.OnMain) else ReadyNext(item.text))
    empty = ("Nothing queued after the current work."
             if open_ids or any(row.get("status") in ("running", "open") for row in rows)
             else "Nothing queued. The plan is complete." if rows else "Nothing queued. No plan is assigned.")
    return NextWork(tuple(items), plan, empty, first)


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
    return f"{actor}: {hold.why.strip() or 'reason not recorded'}; since {since}."


def field_lines(label: str, entries: Sequence[str], *, numbered: bool = False) -> list[str]:
    """Render one block field without changing its stored words."""
    if not entries:
        return []
    if len(entries) == 1 and not numbered:
        return [f"- **{label}:** {entries[0]}"]
    return [f"- **{label}:**", *(f"  {index}. {entry}" for index, entry in enumerate(entries, 1))]


def unit_block(state: UnitBoardState) -> str:
    """Render the unit's current tasks, merge groups, and next work."""
    machine = f" ({state.machine})" if state.machine else ""
    lines = [f"**{state.name}**{machine} — {state.bucket}"]
    running = [task.text for task in state.tasks if task.state == "running"]
    other = [task.text for task in state.tasks if task.state != "running"]
    now = running or other or ([] if state.workers_unavailable else [state.fallback])
    if state.workers_unavailable:
        now.append("Workers unavailable; who runs cannot be determined.")
    if state.no_workers:
        now.append("Workers cannot start because no-worker mode is on.")
    lines.extend(field_lines("Now", now))
    if running:
        lines.extend(field_lines("Also", other))
    lines.extend(field_lines("Merge", state.merge.entries))
    next_item = state.next_work
    if next_item.queue:
        lines.extend(field_lines("Next", next_item.queue, numbered=True))
        if next_item.plan:
            lines.append("\n  " + next_item.plan)
    else:
        lines.extend(field_lines("Next", (next_item.plan or next_item.empty,)))
        if isinstance(next_item.first, NextUnavailable) and next_item.plan:
            lines.append("  " + next_item.empty)
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
            except (OSError, UnicodeError):
                plans[path] = []
        return plans[path]

    def read_queue(identity: staff.StaffId) -> UnitWorkQueue | UnreadableWorkQueue:
        if identity not in queues:
            queues[identity] = read_work_queue(identity)
        return queues[identity]

    def wait_state(after: str) -> waits_on.WaitState:
        if after not in wait_states:
            wait_states[after] = waits_on.state(after, repo=REPOSITORY, snapshot=snapshot, read_run=read_run)
        return wait_states[after]

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
                if isinstance(opened, OpenUnitRun):
                    plan_path = record_fields(record).get("plan", "")
                    built = phase_table.build_run(Path(plan_path), run_events)
                    positions = {row.get("phase"): position
                                 for position, row in enumerate(plan_phase_rows(record_fields(record)))}
                    phases = tuple(sorted((phase for phase in built.phases if isinstance(phase, phase_table.OpenPhase)),
                                          key=lambda phase: positions.get(phase.phase, len(positions))))
                    if planless:
                        progress_record = built
            if isinstance(view, CensusView):
                workers = view.workers.get(member.id, ())
        rows, plan_eta = status_rows(unit.unit, status, (machines[index],) if machines else (), progress_record, run_events,
                                     phases, land, now=now, zone=zone)
        table_rows.extend(rows)
        show_plan_eta = show_plan_eta or plan_eta
        open_sent = {phase.phase: phase_sent_work([
            event for event in run_events if event.get("phase_instance_id") == phase.instance_id]) for phase in phases}
        merge = unit_lands(unit.unit, land, open_sent=open_sent, now=now, zone=zone, on_main=on_main)
        open_ids = tuple(phase.phase for phase in phases)
        next_item = next_work(queue, record, open_ids=open_ids, read_plan=read_plan, wait_state=wait_state,
                              land=land, now=now, zone=zone)
        tasks = tuple(task for phase in phases if isinstance(open_sent[phase.phase], NoSentWork)
                      for task in phase_tasks(phase, run_events, workers=workers, busy=busy, snapshot=snapshot,
                                               now=now, zone=zone))
        shells = shell_waits(view, unit, now=now, zone=zone, snapshot=snapshot, events=run_events, phases=phases)
        # A shell and a phase can report the same resource wait.
        tasks += tuple(task for task in shells if not any(existing.text.startswith(task.text) for existing in tasks))
        fallback = hold
        if not fallback:
            if busy:
                fallback = "The director is busy, but its task is not recorded. No worker runs."
            elif isinstance(next_item.first, BlockedNext):
                fallback = "No task runs. " + next_item.first.reason
            elif isinstance(next_item.first, ReadyNext):
                fallback = f"No task runs. {next_item.first.work} has not started; reason not recorded."
            elif isinstance(next_item.first, NextUnavailable):
                fallback = "No task runs; reason not recorded."
            elif merge.pending:
                fallback = "No task runs. Completed work waits to merge."
            else:
                fallback = ("Nothing to do: the plan is complete and the queue is empty."
                            if plan_phase_rows(record_fields(record)) else "Nothing to do: no plan or queued work.")
        readable = (not isinstance(queue, UnreadableWorkQueue) and "unavailable" not in record_fields(record)
                    and not isinstance(view, census.CensusFailed))
        states.append(UnitBoardState(unit.unit, machines[index] if machines else "", tasks, next_item, merge,
                                     fallback, bool(hold), busy, bool(phases), readable,
                                     isinstance(view, census.CensusFailed), no_workers))
    buckets = ("working", "waiting to merge", "waiting on other work", "idle", "unknown")
    counts = {bucket: sum(state.bucket == bucket for state in states) for bucket in buckets}
    summary = " · ".join(f"{bucket} {counts[bucket]}" for bucket in buckets if bucket != "unknown" or counts[bucket])
    summary += f" · {len(states)} units"
    if not states:
        return summary
    return "\n\n".join((summary, status_table(table_rows, machines=bool(machines), show_plan_eta=show_plan_eta,
                                                 columns=columns), *(unit_block(state) for state in states)))


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
