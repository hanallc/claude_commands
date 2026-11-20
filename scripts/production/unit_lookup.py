#!/usr/bin/env python3
"""Find recorded production units and join their herdr panes by staff identity.

Current names, places and messaging sockets come from herdr and live session
records. Run state comes from /unit:direct records.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import socket
import struct
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from scripts.message import sessions  # noqa: E402
from scripts.herdr import herdr_types  # noqa: E402
from scripts.herdr.place import HerdrPlace  # noqa: E402
from scripts.message.sessions import SessionRecord, sessions_dir as sessions_dir  # noqa: E402
from scripts.production import staff, session_staff, unit_config  # noqa: E402
from scripts.agents import remote
from scripts.lib import unit_runs  # noqa: E402

if TYPE_CHECKING:
    from scripts.herdr import herdr

USAGE = "usage: unit_lookup.py list <production name> | pane <production name> <unit>"


from scripts.production.session_staff import (
    ShowrunnerMember as ShowrunnerMember,
    UnitMember as UnitMember,
    ObservedUnit as ObservedUnit,
    MemberSelector as MemberSelector,
    MemberFound as MemberFound,
    NoSuchMember as NoSuchMember,
    unit_match as _unit_match,
    MemberSources as MemberSources,
    member_sources as member_sources,
    LiveClaude as LiveClaude,
    ClaudeNotRunning as ClaudeNotRunning,
    ClaudeUnknown as ClaudeUnknown,
    Claude as Claude,
    Place as Place,
    LabeledSession as LabeledSession,
    ObservedProcess as ObservedProcess,
    ProcessUnobserved as ProcessUnobserved,
    LiveSession as LiveSession,
    NoSession as NoSession,
    place_text as place_text,
    holder_text as holder_text,
    LabeledProductions as LabeledProductions,
    ProvedLiveUnit as ProvedLiveUnit,
    ProvedAbsentUnit as ProvedAbsentUnit,
    UnknownUnit as UnknownUnit,
    UnitOwner as UnitOwner,
    production_members as production_members,
    group_labeled_sessions as group_labeled_sessions,
    retained_session_labels,
    session_staff_and_directors as session_staff_and_directors,
)


def doc_owner(doc: Path, record: staff.StaffSnapshot) -> staff.StaffRow:
    """The showrunner row whose staff `doc` is this local production document."""
    owner = record.by_doc(remote.local_machine(), doc.resolve())
    if owner is None or owner.kind != "showrunner":
        raise staff.StaffUnreadable(f"{doc}: no showrunner staff row")
    return owner


def doc_units(doc: Path, record: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """The production's unit rows in staff.json row order; a retired row has a RemovedWorktree."""
    return tuple(record.find(kind="unit", parent=doc_owner(doc, record).id))


def local_worktree(selected: MemberFound | NoSuchMember) -> staff.PresentWorktree | staff.RemovedWorktree:
    """Require a recorded local location without changing its lifecycle state."""
    if isinstance(selected, NoSuchMember):
        raise staff.StaffUnreadable(f"{selected.selector}: no matching staff member")
    location = selected.row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise staff.StaffUnreadable(f"{selected.row.name}: staff worktree is unfilled")
    if location.machine != remote.local_machine():
        raise staff.StaffUnreadable(f"{selected.row.name}: staff worktree is on {location.machine}: {location.path}")
    return location


@dataclass(frozen=True)
class UnitProblem:
    unit_id: staff.StaffId
    name: str
    reason: str


@dataclass(frozen=True)
class UnitMetadata:
    cells: dict[staff.StaffId, dict[str, str]]
    problems: tuple[UnitProblem, ...]


def unit_metadata(record: staff.StaffSnapshot, *,
                  name: str | None = None) -> UnitMetadata:
    """Return unit facts and problems without writing diagnostics."""
    productions = production_members(record)
    rows = (tuple(row for children in productions.values() for row in children) if name is None
            else productions.get(name, ()))
    resolver = session_staff.RowResolver(record, rows=rows)
    metadata: dict[staff.StaffId, dict[str, str]] = {}
    issues: list[UnitProblem] = []
    for row in rows:
        cells = {"Unit": row.name}
        problems: list[str] = []
        tree = resolver.unit_worktree(row)
        if isinstance(tree, staff.WorktreeUnfilled):
            cells["Worktree"] = "unavailable: the worktree is not recorded"
            problems.append("the worktree is not recorded")
        else:
            cells["Worktree"] = f"`{tree.path}`"
        branch = resolver.unit_branch(row)
        cells["Branch"] = branch.name if isinstance(branch, session_staff.Branch) else "unavailable: " + branch.reason
        if not isinstance(branch, session_staff.Branch):
            problems.append(branch.reason)
        try:
            config = resolver.unit_config(row)
        except unit_config.ConfigRefused as error:
            cells["Plan"] = "unavailable: " + str(error)
            problems.append(str(error))
        else:
            if isinstance(config, unit_config.UnitConfig):
                if isinstance(config.plan, unit_config.PlanFile):
                    cells["Plan"] = ("unavailable: the worktree is not recorded"
                                     if isinstance(tree, staff.WorktreeUnfilled)
                                     else "active" if config.plan.path == Path("active")
                                     else str(tree.path / config.plan.path))
                else:
                    cells["Plan"] = "standby" if isinstance(config.plan, unit_config.StandbyPlan) else "permanent"
                if isinstance(config.port, unit_config.AppPort):
                    cells["Port"] = str(config.port.number)
            else:
                reason = config.reason if not isinstance(config, session_staff.NoUnitConfig) else "no unit configuration"
                cells["Plan"] = "unavailable: " + reason
                problems.append(reason)
        doc = resolver.production_doc(row)
        if not isinstance(doc, session_staff.ProductionDoc):
            problems.append(doc.reason)
        if problems:
            causes = dict.fromkeys(" ".join(reason.splitlines()) for reason in problems)
            issues.append(UnitProblem(row.id, row.name, "; ".join(causes)))
        metadata[row.id] = cells
    return UnitMetadata(metadata, tuple(issues))


@dataclass(frozen=True)
class UnitProduction:
    """The current parent and its source document, resolved from the staff edge."""

    name: str
    doc: str


def unit_row(unit: str, record: staff.StaffSnapshot, *,
             reference: session_staff.StoredReference | session_staff.LiveSession | session_staff.UnprovedPaneIdentity
             | session_staff.ByName = session_staff.BY_NAME) -> staff.StaffRow:
    """Resolve a selected identity without a name fallback."""
    found = _unit_match(ObservedUnit(unit), record, reference)
    if isinstance(found, NoSuchMember):
        raise staff.StaffUnreadable(f"{unit}: no unique staff entry")
    return found.row


def row_production(row: staff.StaffRow, record: staff.StaffSnapshot) -> UnitProduction:
    """Resolve a unit's current parent from the caller's owner snapshot."""
    parent = record.parent_of(row)
    if parent is not None and parent.kind == "showrunner" and isinstance(parent.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return UnitProduction(parent.name, str(parent.doc.path))
    raise staff.StaffUnreadable(f"{row.name}: no showrunner parent document")


@dataclass(frozen=True)
class NoSessionShowrunner:
    """The session has no staff row with a showrunner at or above it."""


def session_showrunner(session_id: str, *, record: staff.StaffSnapshot,
                       sources: MemberSources) -> MemberFound | NoSessionShowrunner:
    """Follow the newest launch's staff id to its first showrunner."""
    from scripts.herdr import pane_launch

    row = record.by_current_session(session_id)
    launches = [launch for launch in sources.launches
                if row is not None and record.by_member_reference(pane_launch.reference_id(launch)) == row]
    if not launches:
        return NoSessionShowrunner()
    while row is not None:
        if row.kind == "showrunner":
            return MemberFound(row)
        row = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
    return NoSessionShowrunner()


def unit_parent(unit: str,
                reference: session_staff.StoredReference | session_staff.LiveSession | session_staff.ByName = session_staff.BY_NAME, *,
                record: staff.StaffSnapshot) -> UnitProduction:
    """Resolve identity and its current parent without another owner read."""
    row = unit_row(unit, record, reference=reference)
    return row_production(row, record)


class UnitState(Enum):
    RUNNING = "running"
    RUN_FINISHED = "run-finished"
    STANDING_BY = "standing-by"


def runs_dir() -> Path:
    """Where /unit:direct keeps one record per run. Read at each call, as the recorder reads it."""
    return unit_runs.history_root() / "runs"


class RunRecord(NamedTuple):
    path: Path
    state: UnitState


class HeldRun(NamedTuple):
    """A directed run with its history, worktree, state, and held folder."""

    path: Path
    worktree: Path
    state: UnitState
    folder: Path


@dataclass(frozen=True)
class NoUnfinishedRun:
    """The director holds no run with an unfinished history."""


@dataclass(frozen=True)
class HeldRunUnknown:
    """A held history cannot establish the unit's run state."""

    path: Path
    reason: str


class HeldRunLookup:
    """Read only held histories and retain each result for one watch pass."""

    def __init__(self, *, record: staff.StaffSnapshot | None = None) -> None:
        self._record: staff.StaffSnapshot | None = record
        self._histories: dict[Path, HeldRun | HeldRunUnknown] = {}

    def unfinished_held_run(self, session_id: str) -> HeldRun | HeldRunUnknown | NoUnfinishedRun:
        if not session_id:
            return NoUnfinishedRun()
        if self._record is None:
            self._record = staff.snapshot()
        row = self._record.by_current_session(session_id)
        folders = [row.run.path] if row is not None and isinstance(row.run, staff.OpenRun) else []
        for folder in folders:
            if unit_runs.director(folder, record=self._record) != session_id:
                continue
            path = runs_dir() / (Path(folder).name + ".jsonl")
            if path not in self._histories:
                try:
                    with path.open("rb") as handle:
                        first = _event(handle.readline().decode("utf-8"))
                    if first is None or first.get("event_type") != "run_started":
                        raise ValueError("history does not start with run_started")
                    row = self._record.by_member_reference(session_id)
                    if row is None or isinstance(row.worktree, staff.WorktreeUnfilled):
                        raise ValueError(f"{session_id}: the staff worktree is unavailable")
                    state = UnitState.RUNNING if unit_runs.run_is_open(Path(folder)) else UnitState.RUN_FINISHED
                    self._histories[path] = HeldRun(path, row.worktree.path, state, Path(folder))
                except (OSError, UnicodeError, ValueError) as error:
                    self._histories[path] = HeldRunUnknown(path, str(error))
            run = self._histories[path]
            if isinstance(run, HeldRunUnknown) or run.state is UnitState.RUNNING:
                return run
        return NoUnfinishedRun()


class RunStates(NamedTuple):
    records: dict[str, RunRecord]

    def state(self, worktree: Path) -> UnitState:
        record = self.records.get(str(worktree.expanduser().resolve()))
        return record.state if record is not None else UnitState.STANDING_BY


class _StartedRun(NamedTuple):
    at: float
    path: Path


def _event(line: str) -> dict[str, object] | None:
    try:
        event = cast(object, json.loads(line))
    except ValueError:
        return None
    return cast(dict[str, object], event) if isinstance(event, dict) else None


def read_run_states(*, record: staff.StaffSnapshot | None = None) -> RunStates:
    """Join each run's director to its staff folder and read the newest run state."""
    directory = runs_dir()
    record = staff.snapshot() if record is None else record
    newest: dict[str, _StartedRun] = {}
    for path in directory.glob("*.jsonl"):
        with path.open("rb") as handle:
            first = _event(handle.readline().decode("utf-8"))
        if first is None or first.get("event_type") != "run_started":
            continue
        row = unit_runs.run_owner(first, record)
        if isinstance(row, unit_runs.RunOwnerUnrecorded) or isinstance(row.worktree, staff.WorktreeUnfilled):
            continue
        worktree = str(row.worktree.path)
        started = first.get("run_started_at")
        at = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else 0.0
        if worktree not in newest or at > newest[worktree].at:
            newest[worktree] = _StartedRun(at, path)
    records: dict[str, RunRecord] = {}
    for worktree, run in newest.items():
        records[worktree] = RunRecord(run.path, UnitState.RUN_FINISHED)
    for row in record.rows:
        if isinstance(row.run, staff.OpenRun) and not isinstance(row.worktree, staff.WorktreeUnfilled):
            if unit_runs.run_is_open(row.run.path):
                path = directory / f"{row.run.path.name}.jsonl"
                records[str(row.worktree.path)] = RunRecord(path, UnitState.RUNNING)
    return RunStates(records)


def run_state(worktree: Path) -> UnitState:
    """A unit's run state, read from the newest record of a /unit:direct run in its worktree.

    No record means the unit has started no run, so it is standing by. Raises OSError when the
    records cannot be read, which says nothing about the unit.
    """
    return read_run_states().state(worktree)


class RecordsRead(NamedTuple):
    """Every session record was readable."""


class RecordsPartlyUnread(NamedTuple):
    reason: str


def _place_of(pid: int, places: dict[int, HerdrPlace]) -> herdr.ProcessPlacement:
    from scripts.herdr import herdr

    place = places.get(pid)
    if place is not None:
        return place
    # A live registry PID may be absent from the snapshot because its
    # working directory was deleted. Placement needs no cwd.
    return herdr.process_placement(pid)


@dataclass(frozen=True)
class ClaudeRegistryEvidence:
    """Keep registry failures and standalone owner proofs."""

    places: dict[HerdrPlace, str]
    unplaced: RecordsRead | RecordsPartlyUnread
    registry_unknowns: dict[str, UnknownUnit] = field(default_factory=dict)
    standalone_owners: list[ProvedClaudeOwner] = field(default_factory=list)


@dataclass(frozen=True)
class CheckedSocket:
    path: str


@dataclass(frozen=True)
class SocketUnavailable:
    reason: str


@dataclass(frozen=True)
class ClaudeOwnerMismatch(SocketUnavailable):
    """The available evidence identifies a different owner."""


@dataclass(frozen=True)
class ClaudeSocketAbsent(SocketUnavailable):
    """The connection did not reach any listener."""


type ClaudeAddress = CheckedSocket | SocketUnavailable


@dataclass(frozen=True)
class RegisteredSocketPath:
    """The path recorded for this generation, before a peer check."""

    path: str


@dataclass(frozen=True)
class ProvedClaudeOwner:
    pid: int
    recorded_start: str
    conversation: str
    placement: HerdrPlace | herdr.NotInHerdr
    address: RegisteredSocketPath | SocketUnavailable
    name: str
    activity: str

    def session(self, *, probe_address: bool) -> LiveClaude:
        path = ""
        if probe_address and isinstance(self.address, RegisteredSocketPath):
            checked = _check_claude_address(self.address.path, self.pid, self.recorded_start)
            if isinstance(checked, CheckedSocket):
                path = checked.path
        return LiveClaude(self.name, self.conversation, path, self.pid, self.activity)


@dataclass(frozen=True)
class ProvedCodexOwner:
    pid: int
    started: str
    conversation: str
    pane: LabeledSession


type ConversationProof = ProvedClaudeOwner | ProvedCodexOwner
type PaneObservation = session_staff.UnitPaneObservation | ConversationProof


@dataclass(frozen=True)
class UnknownClaudeOwner:
    reason: str


def current_claude_record(pid: int, started: str, conversation: str) -> SessionRecord | SocketUnavailable:
    """Read the current registry details for the proved conversation and process generation."""
    current = sessions.read_session(sessions.sessions_dir() / f"{pid}.json")
    if isinstance(current, sessions.UnreadableSessionRecord):
        return SocketUnavailable("the current Claude registry record is unreadable")
    if (current["pid"] != pid or not started or current.get("procStart", "") != started
            or current["sessionId"] != conversation):
        return ClaudeOwnerMismatch("the current Claude registry record does not match the owner")
    return current


def _peer_pid(connection: socket.socket) -> int:
    if sys.platform == "linux":
        pid, _, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return pid
    if sys.platform == "darwin":
        # Darwin defines SOL_LOCAL as 0 and LOCAL_PEERPID as 2.
        return struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
    raise OSError("Unix peer credentials are unavailable on this platform")


@dataclass(frozen=True)
class CheckedSocketOwner:
    """The connected peer matches the expected process generation."""


def validate_claude_connection(connection: socket.socket, pid: int, started: str) -> CheckedSocketOwner | SocketUnavailable:
    """Validate the peer PID and process start on an existing connection."""
    try:
        if _peer_pid(connection) != pid:
            return ClaudeOwnerMismatch("the messaging socket peer is not the Claude owner")
        observed_start = sessions.process_start(pid)
        if not observed_start:
            return SocketUnavailable("the Claude owner process start is unreadable")
        if observed_start != started:
            return ClaudeOwnerMismatch("the Claude owner process start changed")
        return CheckedSocketOwner()
    except (OSError, ValueError, struct.error) as error:
        return SocketUnavailable(f"cannot check the messaging socket: {error}")


def connect_claude_socket(path: str, pid: int, started: str, *, timeout: float = 0.0) -> socket.socket | SocketUnavailable:
    """Return the connected socket only when its peer owns the expected process generation."""
    if not path:
        return SocketUnavailable("the owner has no messaging socket")
    if not Path(path).is_absolute() or "\0" in path:
        return SocketUnavailable("the messaging socket path is invalid")
    observed_start = sessions.process_start(pid)
    if not started or not observed_start:
        return SocketUnavailable("the Claude owner process start is unreadable")
    if observed_start != started:
        return ClaudeOwnerMismatch("the Claude owner process start changed")
    connection = None
    keep = False
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as error:
            name = "ENOENT" if isinstance(error, FileNotFoundError) else "ECONNREFUSED"
            return ClaudeSocketAbsent(f"cannot connect to the messaging socket: {name}: {error}")
        owner = validate_claude_connection(connection, pid, started)
        if isinstance(owner, SocketUnavailable):
            return owner
        keep = True
        return connection
    except (OSError, ValueError, struct.error) as error:
        return SocketUnavailable(f"cannot check the messaging socket: {error}")
    finally:
        if connection is not None and not keep:
            connection.close()


def _check_claude_address(path: str, pid: int, started: str) -> ClaudeAddress:
    connection = connect_claude_socket(path, pid, started)
    if isinstance(connection, SocketUnavailable):
        return connection
    connection.close()
    return CheckedSocket(path)


def _validate_claude_owner(record: SessionRecord, placement: HerdrPlace | herdr.NotInHerdr,
                           read_native: Callable[[], herdr.NativeAgentProcess]) -> ProvedClaudeOwner | UnknownClaudeOwner:
    pid = record["pid"]
    started = record.get("procStart", "")
    if not started:
        return UnknownClaudeOwner(f"the session record of pid {pid} has no recorded process start")
    try:
        native = read_native()
        conversation = native.current_identity
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return UnknownClaudeOwner(str(error))
    if started != native.started or record["sessionId"] != conversation:
        return UnknownClaudeOwner("Claude registry does not match the native conversation or process start")
    current = current_claude_record(pid, started, conversation)
    if isinstance(current, SocketUnavailable):
        return ProvedClaudeOwner(pid, started, conversation, placement, current, record["name"], "")
    path = current["messagingSocketPath"]
    address = (RegisteredSocketPath(path) if path == record["messagingSocketPath"]
               else SocketUnavailable("the snapshot names a different Claude messaging socket"))
    return ProvedClaudeOwner(pid, started, conversation, placement, address, current["name"], current.get("status", ""))


def _claudes(processes: list[herdr.HerdrProcess], *,
             running: Sequence[SessionRecord] | None = None, registry: sessions.RegistryRead | None = None
             ) -> tuple[dict[HerdrPlace, dict[int, list[SessionRecord]]], ClaudeRegistryEvidence]:
    """Collect live registry candidates by pane and PID, newest first."""
    from scripts.herdr import herdr

    places = {process.pid: process.place for process in processes}
    entries: Iterable[SessionRecord]
    placed: dict[HerdrPlace, dict[int, list[SessionRecord]]] = {}
    unread: dict[HerdrPlace, str] = {}
    unplaced: RecordsRead | RecordsPartlyUnread = RecordsRead()
    if running is None:
        registry = sessions.read_registry() if registry is None else registry
        if sessions.sessions_dir() in registry.unreadable:
            return {}, ClaudeRegistryEvidence({}, RecordsPartlyUnread("cannot list session records"))
        entries = (item.record for item in registry.entries)
        for path in registry.unreadable:
            unread_pid = int(path.stem) if path.stem.isdigit() else None
            place = None if unread_pid is None else _place_of(unread_pid, places)
            if isinstance(place, HerdrPlace):
                unread[place] = f"the session record of pid {unread_pid} could not be read"
            else:
                unplaced = RecordsPartlyUnread("one or more session records could not be read")
    else:
        entries = running
    unknowns: dict[str, UnknownUnit] = {}
    standalone: list[ProvedClaudeOwner] = []
    for record in sorted(entries, key=lambda entry: entry["updatedAt"], reverse=True):
        pid = record["pid"]
        identity = record["sessionId"]
        if sessions.headless(record):
            continue
        started = record.get("procStart", "")
        if not started:
            _ = unknowns.setdefault(
                identity, UnknownUnit(f"the session record of pid {pid} has no recorded process start"))
        elif pid not in places:
            liveness = sessions.process_liveness(pid, started)
            if isinstance(liveness, sessions.SessionNotLive):
                continue
            if isinstance(liveness, sessions.SessionLivenessUnknown):
                _ = unknowns.setdefault(identity, UnknownUnit(liveness.reason))
                continue
        place = _place_of(pid, places)
        if isinstance(place, HerdrPlace):
            placed.setdefault(place, {}).setdefault(pid, []).append(record)
        elif isinstance(place, herdr.UnprovedHerdrPlacement):
            _ = session_staff.collect_anomaly(place.reason, pid=pid, check_identity=False)
            _ = unknowns.setdefault(identity, UnknownUnit(place.reason))
        else:
            checked = _validate_claude_owner(record, place, lambda: herdr.native_process(pid, "claude"))
            if isinstance(checked, ProvedClaudeOwner):
                standalone.append(checked)
            else:
                _ = unknowns.setdefault(identity, UnknownUnit(checked.reason))
    return placed, ClaudeRegistryEvidence(unread, unplaced, unknowns, standalone)


def _unproved_pane(place: HerdrPlace, processes: list[herdr.HerdrProcess], claudes: dict[int, list[SessionRecord]],
                   reason: str, pane: Mapping[str, object], *,
                   registry_problem: str = "", native_identity: str = "") -> LabeledSession:
    """Retain possible unit identities for an unknown result."""
    from scripts.herdr import herdr

    candidates = {process.marks[key] for process in processes
                  for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID") if process.marks.get(key)}
    candidates.update(record["sessionId"] for records in claudes.values() for record in records)
    if native_identity:
        candidates.add(native_identity)
    if reported := herdr.pane_session(pane):
        candidates.add(reported)
    # Keep a PID for the diagnostic. It does not identify the pane owner.
    agent = next((process for process in processes if process.command in ("codex", "node", "nodejs", "claude")), None)
    process = ObservedProcess(agent.pid, agent.command) if agent is not None else ProcessUnobserved()
    if registry_problem and agent is not None:
        _ = session_staff.collect_anomaly(f"pid {agent.pid} in {place_text(place)}: {registry_problem}",
                                          pid=agent.pid, check_identity=False)
    return LabeledSession("", ClaudeUnknown(reason), place, process,
                          session_staff.UnprovedPaneIdentity(frozenset(candidates)))


@dataclass(frozen=True)
class _RegisteredClaudeWithoutArgv(ObservedProcess):
    """The foreground PID lacks argv and agrees with the pane and registry."""

    conversation: str


def _herdr_labeled(registry: dict[HerdrPlace, dict[int, list[SessionRecord]]], evidence: ClaudeRegistryEvidence,
                   processes: list[herdr.HerdrProcess]) -> Iterable[PaneObservation]:
    """Join each pane's proved owner to its current conversation."""
    from scripts.herdr import herdr

    clients = {session["name"]: herdr.Herdr(session["name"]) for session in herdr.live_sessions()}
    panes: dict[HerdrPlace, dict[str, object]] = {}
    unreadable: dict[str, str] = {}
    for name, client in clients.items():
        try:
            listed: dict[HerdrPlace, dict[str, object]] = {}
            for row in client.listed("pane"):
                pane_id = herdr.json_text(row.get("pane_id"), "unit pane id")
                listed[HerdrPlace(name, pane_id)] = row
            panes.update(listed)
        except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            reason = f"cannot read panes for herdr session {name}: {error}"
            unreadable[name] = reason
            session_staff.report_anomaly(reason)
            yield session_staff.UnreadablePaneList(name, reason)
    places: dict[HerdrPlace, list[herdr.HerdrProcess]] = {place: [] for place in panes}
    for process in processes:
        if process.place.session in clients:
            places.setdefault(process.place, []).append(process)
    for place in registry:
        if place.session in clients:
            _ = places.setdefault(place, [])
    for place, holders in places.items():
        claudes = registry.get(place, {})
        client = clients[place.session]
        pane = panes.get(place, {"pane_id": place.pane})
        problem = evidence.places.get(place, evidence.unplaced.reason
                                    if isinstance(evidence.unplaced, RecordsPartlyUnread) else "")
        if place.session in unreadable:
            yield _unproved_pane(place, holders, claudes, problem or unreadable[place.session],
                                 pane, registry_problem=problem)
            continue
        try:
            info = herdr.json_object(client.result("pane", "process-info", "--pane", place.pane).get("process_info"),
                                     "unit pane process info")
            owner = herdr.pane_foreground(place, info)
            if isinstance(owner, herdr.ObservedEmpty):
                holders = [process for process in holders
                           if not process.ancestry.is_detached_from_shell(process.pid, owner.shell_pid)]
                if problem or claudes or any(process.pid != owner.shell_pid for process in holders):
                    yield _unproved_pane(place, holders, claudes, problem or "no foreground agent", pane)
                elif holders and (identity := holders[0].marks.get("CLAUDE_CODE_SESSION_ID")):
                    shell = holders[0]
                    yield LabeledSession("", ClaudeNotRunning(), place, ObservedProcess(shell.pid, shell.command),
                                         session_staff.UnprovedPaneIdentity(frozenset({identity})), owner)
                elif holders and holders[0].marks.get("CODEX_THREAD_ID"):
                    unknown = _unproved_pane(place, holders, claudes, "no foreground agent", pane)
                    yield replace(unknown, empty=owner)
                else:
                    yield LabeledSession("", ClaudeNotRunning(), place, empty=owner)
                continue
            if isinstance(owner, herdr.UnknownPaneOwner):
                label = _unproved_pane(place, holders, claudes, problem or owner.reason, pane, registry_problem=problem)
                if isinstance(owner, herdr.ClaudePaneWithoutArgv) and not problem and place in panes:
                    registered = claudes.get(owner.pid, ())
                    if len(registered) == 1:
                        conversation = registered[0]["sessionId"]
                        report = herdr.checked_pane_session(pane)
                        if isinstance(report, herdr.NoReportedAgentSession) or report.session_id == conversation:
                            label = replace(label, process=_RegisteredClaudeWithoutArgv(owner.pid, "claude", conversation))
                yield label
                continue
            native = herdr.read_foreground_pane_owner(owner, pane)
            identity = LiveSession(native.current_identity)
            if isinstance(owner, herdr.ProvedClaudeForeground):
                reason = problem
                for record in claudes.get(owner.pid, ()):
                    checked = _validate_claude_owner(record, place, lambda: native)
                    if isinstance(checked, ProvedClaudeOwner):
                        yield checked
                        break
                    reason = reason or checked.reason
                else:
                    if not reason:
                        # The native identity excludes other candidates. The registry details stay unknown.
                        yield LabeledSession("", ClaudeUnknown("no live Claude registry record"), place,
                                             ObservedProcess(owner.pid, owner.kind), identity)
                    else:
                        yield _unproved_pane(place, holders, claudes, reason, pane,
                                             registry_problem=problem, native_identity=identity.id)
                continue
            process = session_staff.ProvedCodexPaneProcess(owner.pid, owner.kind)
            label = LabeledSession("", ClaudeUnknown(problem or "no live Claude registry record"), place, process, identity,
                                   generation=session_staff.ProvedProcessGeneration(owner.pid, native.started))
            yield ProvedCodexOwner(owner.pid, native.started, identity.id, label)
        except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            yield _unproved_pane(place, holders, claudes, problem or str(error), pane, registry_problem=problem)


def _reduce_owner_proofs(proofs: Iterable[ConversationProof]) -> dict[str, ConversationProof | UnknownUnit]:
    """Select one process generation per conversation."""
    owners: dict[str, dict[tuple[int, str], ConversationProof]] = {}
    for proof in proofs:
        start = proof.recorded_start if isinstance(proof, ProvedClaudeOwner) else proof.started
        _ = owners.setdefault(proof.conversation, {}).setdefault((proof.pid, start), proof)
    return {
        conversation: (next(iter(generations.values())) if len(generations) == 1
                       else UnknownUnit(f"conflicting owner proofs for conversation {conversation}"))
        for conversation, generations in owners.items()
    }


def _resolved_labels(
    panes: list[LabeledSession | ConversationProof], evidence: ClaudeRegistryEvidence, *, probe_address: bool,
) -> tuple[tuple[LabeledSession, ...], dict[str, ProvedLiveUnit | UnknownUnit]]:
    """Publish pane labels and standalone owners after resolving each conversation."""
    pane_proofs = (pane for pane in panes if not isinstance(pane, LabeledSession))
    resolved = _reduce_owner_proofs(chain(pane_proofs, evidence.standalone_owners))
    owners: dict[str, ProvedLiveUnit | UnknownUnit] = dict(evidence.registry_unknowns)
    for identity, result in resolved.items():
        _ = owners.pop(identity, None)
        if isinstance(result, UnknownUnit):
            owners[identity] = result
        elif isinstance(result, ProvedClaudeOwner) and not isinstance(result.placement, HerdrPlace):
            owners[identity] = ProvedLiveUnit(result.session(probe_address=probe_address),
                                             session_staff.ProvedProcessGeneration(result.pid, result.recorded_start))

    labels: list[LabeledSession] = []
    for pane in panes:
        if isinstance(pane, LabeledSession):
            labels.append(pane)
            continue
        result = resolved[pane.conversation]
        if not isinstance(result, UnknownUnit) and result is not pane:
            continue
        if isinstance(pane, ProvedCodexOwner):
            label = pane.pane
            if isinstance(result, UnknownUnit):
                label = replace(label, claude=ClaudeUnknown(result.reason), process=ObservedProcess(pane.pid, "codex"),
                                generation=session_staff.NoProvedProcessGeneration())
        else:
            assert isinstance(pane.placement, HerdrPlace)
            claude = (ClaudeUnknown(result.reason) if isinstance(result, UnknownUnit)
                      else pane.session(probe_address=probe_address))
            generation = (session_staff.NoProvedProcessGeneration() if isinstance(result, UnknownUnit)
                          else session_staff.ProvedProcessGeneration(pane.pid, pane.recorded_start))
            label = LabeledSession("", claude, pane.placement, ObservedProcess(pane.pid, "claude"),
                                   LiveSession(pane.conversation), generation=generation)
        labels.append(label)
    return tuple(labels), owners


def observed_session_labels(*, record: staff.StaffSnapshot | None = None,
                            running: Sequence[SessionRecord] | None = None,
                            registry: sessions.RegistryRead | None = None,
                            probe_address: bool = True) -> session_staff.SessionLabelObservations:
    """Read native identities and retain failures to list panes."""
    from scripts.herdr import herdr

    del record
    try:
        with herdr.observation_snapshot():
            processes = herdr.herdr_processes(inherited_marks_only=True)
            claudes, evidence = _claudes(processes, running=running, registry=registry)
            panes: list[LabeledSession | ConversationProof] = []
            failed: list[session_staff.UnreadablePaneList] = []
            observed_places: set[HerdrPlace] = set()
            for item in _herdr_labeled(claudes, evidence, processes):
                if isinstance(item, session_staff.UnreadablePaneList):
                    failed.append(item)
                else:
                    panes.append(item)
                    if isinstance(item, LabeledSession):
                        observed_places.add(item.place)
                    elif isinstance(item, ProvedCodexOwner):
                        observed_places.add(item.pane.place)
                    else:
                        assert isinstance(item.placement, HerdrPlace)
                        observed_places.add(item.placement)
            for place in claudes.keys() - observed_places:
                reason = f"cannot read the registered pane at {place_text(place)}"
                for records in claudes[place].values():
                    for candidate in records:
                        _ = evidence.registry_unknowns.setdefault(candidate["sessionId"], UnknownUnit(reason))
            labels, owners = _resolved_labels(panes, evidence, probe_address=probe_address)
            registry_failures = (evidence.unplaced.reason,) if isinstance(evidence.unplaced, RecordsPartlyUnread) else ()
            if registry_failures:
                session_staff.report_anomaly(registry_failures[0])
            return session_staff.SessionLabelObservations(labels, tuple(failed), registry_failures, owners)
    except herdr_types.HerdrFailure as error:
        raise OSError(str(error)) from error


@dataclass(frozen=True)
class ClaudeArgvUnavailable(UnknownUnit):
    """The exact conversation's pane reports a Claude PID without argv."""

    pid: int
    place: HerdrPlace


def checked_conversation(identity: str, *, record: staff.StaffSnapshot | None = None,
                         registry: sessions.RegistryRead | None = None, probe_address: bool = True) -> UnitOwner:
    """Check the exact conversation with fresh pane evidence."""
    from scripts.herdr import herdr

    try:
        with herdr.process_action():
            if probe_address and registry is None:
                observed = observed_session_labels(record=record)
            else:
                observed = observed_session_labels(record=record, registry=registry, probe_address=probe_address)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return UnknownUnit(str(error))
    checked = observed.checked_conversation(identity)
    if (not isinstance(checked, UnknownUnit) or identity in observed.registry_owners
            or observed.unreadable_pane_lists or observed.registry_failures):
        return checked
    candidates = [pane for pane in observed.labels
                  if pane.identity == LiveSession(identity)
                  or isinstance(pane.identity, session_staff.UnprovedPaneIdentity) and identity in pane.identity.candidates]
    if len(candidates) == 1:
        pane = candidates[0]
        if (isinstance(pane.process, _RegisteredClaudeWithoutArgv) and pane.process.conversation == identity
                and isinstance(pane.claude, ClaudeUnknown)):
            return ClaudeArgvUnavailable(pane.claude.reason, pane.process.pid, pane.place)
    return checked


def every_labeled_session(*, record: staff.StaffSnapshot | None = None,
                      sources: MemberSources | None = None,
                      running: Sequence[SessionRecord] | None = None) -> LabeledProductions:
    """Read all herdr units once. An unreadable host raises OSError."""
    record = record if record is not None else staff.snapshot()
    return group_labeled_sessions(observed_session_labels(record=record, running=running), record=record, sources=sources)


def labeled_sessions(production_name: str, *, record: staff.StaffSnapshot | None = None,
                 sources: MemberSources | None = None) -> dict[str, LabeledSession]:
    """Return the selected process state for each unit."""
    found = every_labeled_session(record=record, sources=sources)
    panes = found.status_units(production_name)
    for name in panes.keys() | set(found.members.get(production_name, ())):
        owner = found.checked_unit(production_name, name)
        if isinstance(owner, UnknownUnit):
            raise OSError(f"{name}: {owner.reason}")
        if isinstance(owner, ProvedLiveUnit) and isinstance(owner.owner, LiveClaude):
            raise OSError(f"{name}: the live owner has no proved herdr pane")
    return panes


def _selected_unit(production_name: str, name: str, *, record: staff.StaffSnapshot | None = None,
                   sources: MemberSources | None = None,
                   running: Sequence[SessionRecord] | None = None) -> tuple[LabeledProductions, str]:
    record = record if record is not None else staff.snapshot()
    sources = sources if sources is not None else member_sources()
    selected = sources.member(UnitMember(production_name, name), record)
    marked = observed_session_labels(record=record, running=running)
    if isinstance(selected, MemberFound):
        found = retained_session_labels(marked, selected.row, production_name, record=record, sources=sources)
        return found, selected.row.name
    return group_labeled_sessions(marked, record=record, sources=sources), name


def checked_unit(production_name: str, name: str, *, record: staff.StaffSnapshot | None = None) -> UnitOwner:
    """Read the selected unit with fresh owner evidence."""
    from scripts.herdr import herdr

    try:
        with herdr.process_action():
            found, label = _selected_unit(production_name, name, record=record)
    except OSError as error:
        return UnknownUnit(str(error))
    return found.checked_unit(production_name, label)


def labeled_session(production_name: str, name: str, *, record: staff.StaffSnapshot | None = None,
                    sources: MemberSources | None = None,
                    running: Sequence[SessionRecord] | None = None) -> LabeledSession | None:
    """Read pane details. Raise if incomplete evidence prevents finding the pane."""
    found, label = _selected_unit(production_name, name, record=record, sources=sources, running=running)
    match found.checked_unit(production_name, label):
        case UnknownUnit(reason=reason):
            raise OSError(reason)
        case ProvedLiveUnit(owner=LabeledSession() as pane):
            return pane
        case ProvedLiveUnit():
            raise OSError("the live owner has no proved herdr pane")
        case ProvedAbsentUnit(pane=LabeledSession() as pane):
            return pane
        case ProvedAbsentUnit():
            return None


@dataclass(frozen=True)
class UnitPaneAbsent:
    pass


@dataclass(frozen=True)
class UnitPaneUnknown:
    reason: str


def select_unit_pane(production_name: str, name: str) -> HerdrPlace | UnitPaneAbsent | UnitPaneUnknown:
    """Return a pane address only after proof of its owner."""
    match checked_unit(production_name, name):
        case ProvedLiveUnit(owner=LabeledSession(place=place)):
            return place
        case ProvedLiveUnit():
            return UnitPaneUnknown("no proved pane owner")
        case ProvedAbsentUnit():
            return UnitPaneAbsent()
        case UnknownUnit(reason=reason):
            return UnitPaneUnknown(reason)


def main(arguments: list[str]) -> int:
    """Exit 0 found or done, 1 no such unit session, 2 usage, 3 cannot tell or could not do it."""
    try:
        match arguments:
            case ["list", production_name]:
                report = every_labeled_session()
                for name in report.members.get(production_name, ()):
                    address, session, told = "", "", ("", "", "")
                    match report.checked_unit(production_name, name):
                        case ProvedLiveUnit(owner=owner):
                            running = "live"
                            if isinstance(owner, LabeledSession):
                                address, session = place_text(owner.place), owner.place.session
                                claude = owner.claude
                            else:
                                claude = owner
                            if isinstance(claude, LiveClaude):
                                told = (claude.name, claude.session_id, claude.socket)
                        case ProvedAbsentUnit(pane=LabeledSession(place=place)):
                            running = "stopped"
                            address, session = place_text(place), place.session
                        case ProvedAbsentUnit():
                            continue
                        case UnknownUnit():
                            running = "unknown"
                    print("\t".join((name, address, session, running, *told)))
                if report.failures:
                    print(f"unit_lookup: {report.failures[0]}", file=sys.stderr)
                    return 3
            case ["pane", production_name, name]:
                pane = select_unit_pane(production_name, name)
                if isinstance(pane, UnitPaneAbsent):
                    return 1
                if isinstance(pane, UnitPaneUnknown):
                    print(f"unit_lookup: {pane.reason}", file=sys.stderr)
                    return 3
                print(place_text(pane))
            case _:
                print(USAGE, file=sys.stderr)
                return 2
    except OSError as error:
        print(f"unit_lookup: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
