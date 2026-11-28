"""Start, observe, and close staff workers through proved native panes."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.direct import pass_record as pass_recorder

import argparse
import errno
import fcntl
import hashlib
import io
import json
import shlex
import os
import re
from pathlib import Path
import subprocess
import sys
import time
from threading import Event
from collections.abc import Callable, Generator, Hashable, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager, redirect_stderr
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from functools import cache, partial
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, Literal, cast, final
from uuid import UUID

from scripts.herdr import herdr, pane_launch
from scripts.herdr import herdr_types
from scripts.lib import conf, models_registry, nix_env, unit_runs
from scripts.lib.transcripts import uncompressed_path
from scripts.whoami import quota_state
from scripts.production import session_staff, staff, worker_admission
from scripts.production.production_doc import EnrolledPaths, ProductionPathsError, resolve_production_paths
from scripts.production.staff import serving_merge_unit
from scripts.agents import detached_run_process, pane_record, shutdown_record, worker_lifecycle
from scripts.direct import phase_reservation, progress_history, recorder_state

if TYPE_CHECKING:
    from scripts.accounts import accounts

StaffId = staff.StaffId
WorkerName = staff.WorkerName
WorkerRole = staff.WorkerRole

WORKER_SANDBOX = "danger-full-access"
DESIGNER_SANDBOX = "danger-full-access"
READ_ONLY_SANDBOX = "read-only"
REVIEW_ROLES: frozenset[WorkerRole] = frozenset({"review", "astra-reviewer"})
CLAUDE_REVIEW_TOOLS = "Read,Grep,Glob"
CLAUDE_REVIEW_MCP = '{"mcpServers":{}}'
CLAUDE_REVIEW_ARGUMENTS = ("--tools", CLAUDE_REVIEW_TOOLS, "--mcp-config", CLAUDE_REVIEW_MCP, "--strict-mcp-config")


class OrdinaryWorkerOwner(Enum):
    LEGACY_OR_UNIT = "the worker uses the existing unit or legacy policy"


def enrolled_worker_owner(owner: staff.StaffRow) -> EnrolledPaths | OrdinaryWorkerOwner:
    if owner.kind == "showrunner" and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)):
        paths = resolve_production_paths(owner.doc.path)
        if isinstance(paths, EnrolledPaths):
            return paths
    return OrdinaryWorkerOwner.LEGACY_OR_UNIT


def worker_directory(record: staff.StaffSnapshot, reference: StaffId) -> Path:
    row = record.by_id(reference)
    owner = record.parent_of(row) if row is not None and row.kind == "worker" else row
    if owner is not None:
        paths = enrolled_worker_owner(owner)
        if isinstance(paths, EnrolledPaths):
            return paths.main
    return session_staff.worker_directory(record, reference)


def review_role_refusal(owner: staff.StaffRow, role: WorkerRole | WorkerRoleUnavailable) -> AdmissionRefused | None:
    if isinstance(role, str) and role in REVIEW_ROLES:
        return None
    merge = serving_merge_unit(owner.id)
    route = f"unit {merge.name}" if merge is not None else "a unit with the assigned work"
    return AdmissionRefused(f"{owner.name}: only review and astra-reviewer can run here. Send write work to {route}.")


def review_run_refusal(owner: staff.StaffRow, run: Path, paths: EnrolledPaths, *artifacts: Path) -> AdmissionRefused | None:
    root = run.absolute()
    if not isinstance(owner.run, staff.OpenRun) or root != owner.run.path.absolute():
        return AdmissionRefused("showrunner reviews require the owner's recorded run directory")
    return review_artifact_refusal(root, paths, *artifacts)


def review_artifact_refusal(run: Path, paths: EnrolledPaths, *artifacts: Path) -> AdmissionRefused | None:
    root = run.absolute()
    if ".." in root.parts or root.resolve() != root or not root.is_dir():
        return AdmissionRefused("showrunner reviews require the owner's recorded run directory")
    if any(root.is_relative_to(tree) or tree.is_relative_to(root) for tree in (paths.main, paths.integration)):
        return AdmissionRefused("the showrunner run directory overlaps a checkout")
    for artifact in artifacts:
        path = artifact.absolute()
        if ".." in path.parts or not path.is_relative_to(root):
            return AdmissionRefused("showrunner review orders and replies must stay in the run directory")
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            return AdmissionRefused(f"{path}: showrunner review paths cannot contain symbolic links")
    return None


@dataclass(frozen=True)
class StaffWorker:
    parent: StaffId
    name: WorkerName
    role: WorkerRole


@dataclass(frozen=True)
class ReplyToCaller:
    """Send the answer to the caller with the message command."""


@dataclass(frozen=True)
class TranscriptReply:
    """Read the answer from the consultation transcript."""


REPLY_TO_CALLER = ReplyToCaller()


@dataclass(frozen=True)
class ConsultationPane:
    label: str
    cwd: Path
    question: str = ""
    delivery: ReplyToCaller | TranscriptReply = REPLY_TO_CALLER


class NoDesignPaneLaunch(Enum):
    UNRECORDED = "the role has no pane launch marker"
    NOT_ISSUED = "the launcher has not started"


@dataclass(frozen=True)
class DesignPaneLaunch:
    token: UUID


@dataclass(frozen=True)
class RetryDesignPaneLaunch(DesignPaneLaunch):
    """Start only in the shell from the original launch."""


@dataclass(frozen=True)
class DurableDesignRole:
    parent: StaffId
    name: WorkerName
    role: WorkerRole
    claim: staff.DesignWorkerClaim | staff.NoDesignWorkerClaim = staff.NO_DESIGN_WORKER_CLAIM
    pane_launch: DesignPaneLaunch | NoDesignPaneLaunch = NoDesignPaneLaunch.UNRECORDED


DESIGN_PANE_LAUNCH_ENV = "UNIT_ASTRA_PANE_LAUNCH"


PaneTarget = StaffWorker | ConsultationPane | DurableDesignRole

CLAIMED_WORKING_DIR_ENV = "UNIT_CLAIMED_WORKING_DIR"


@dataclass(frozen=True)
class DispatchRun:
    directory: Path


class HolderRun(Enum):
    CURRENT = "holder's run"


@dataclass(frozen=True)
class CreatedWorkerPane:
    """This start created the pane with this terminal."""

    terminal: str


@dataclass(frozen=True)
class NoCreatedWorkerPane:
    """This start owns no new pane."""


NO_CREATED_WORKER_PANE = NoCreatedWorkerPane()


@dataclass(frozen=True)
class OpenWorker:
    row: staff.StaffRow
    pane: str
    terminal: str
    status: herdr.AgentStatus
    created_pane: CreatedWorkerPane | NoCreatedWorkerPane = field(default=NO_CREATED_WORKER_PANE, compare=False)


@dataclass(frozen=True)
class StoppedWorker:
    row: staff.StaffRow
    pane: str
    terminal: str


@dataclass(frozen=True)
class ClosedWorker:
    row: staff.StaffRow


@dataclass(frozen=True)
class UncertainWorker:
    row: staff.StaffRow
    reason: str


@dataclass(frozen=True)
class ProcessUnknownWorker:
    row: staff.StaffRow
    pane: str
    terminal: str
    reason: str
    created_pane: CreatedWorkerPane | NoCreatedWorkerPane = field(default=NO_CREATED_WORKER_PANE, compare=False)


@dataclass(frozen=True)
class AdmissionRefused:
    reason: str


@dataclass(frozen=True)
class IncompleteStart:
    row: staff.StaffRow
    pane: str
    reason: str
    created_pane: CreatedWorkerPane | NoCreatedWorkerPane = field(default=NO_CREATED_WORKER_PANE, compare=False)


class UnpublishedCleanup(Enum):
    NOT_STARTED = "pane {pane} retained; this call did not start it"
    PANE_CLOSED = "pane {pane} closed"
    PANE_CLOSE_FAILED = "pane {pane} close failed"
    AGENT_STOPPED = "agent stopped; existing pane {pane} retained at its shell"
    AGENT_STOP_FAILED = "agent stop failed; existing pane {pane} retained"


@dataclass(frozen=True)
class UnpublishedWorker:
    pane: str
    cause: str
    cleanup: UnpublishedCleanup = UnpublishedCleanup.NOT_STARTED


@dataclass(frozen=True)
class DestinationAfterSourceRelease:
    """Select the switch destination after the source pane closes."""


class StartRefusal(Enum):
    UNKNOWN_ROLE = "unknown role"
    ROLE_UNAVAILABLE = "dispatch role unavailable"
    INVALID_ROLE_CONFIGURATION = "invalid role configuration"
    BUSY_ROLE_CHANGE = "role change blocked by active work"
    ACCOUNT_UNAVAILABLE = "account unavailable"
    PANE_NOT_AT_SHELL = "start requires a stopped pane"


@dataclass(frozen=True)
class StartRefused:
    reason: StartRefusal
    detail: str


WorkerPane = OpenWorker | StoppedWorker | ClosedWorker | ProcessUnknownWorker | UncertainWorker
WorkerStart = OpenWorker | IncompleteStart | AdmissionRefused | UnpublishedWorker | ProcessUnknownWorker | UncertainWorker | StartRefused


@dataclass(frozen=True)
class Closed:
    """The requested close is complete."""


@dataclass(frozen=True)
class CloseAllowed:
    """The close checks permit closure; the pane remains open."""


class CompletionRecovery(Enum):
    NO_PENDING_NOTICE = "no pending completion notice"
    RECOVERED = "completion recovery finished"
    PENDING_CLOSE = "completion has pending pane cleanup"
    WAITING_ON_RUNS = "the completion waits for a detached run"
    WAITING_ON_REPLY = "a detached run result awaits a matching DONE"
    WAITING_ON_REAP = "reap must record the run result"


@dataclass(frozen=True)
class Kept:
    reason: str | CompletionRecovery

    @property
    def blocker(self) -> str:
        return self.reason if isinstance(self.reason, str) else self.reason.value


@dataclass(frozen=True)
class WorkerRoleUnavailable:
    reason: str


class ExitCode(IntEnum):
    OK = 0
    REFUSED = 2
    NO_DONE = 3
    BLOCKED = 4
    STALLED = 5
    BUSY = 6
    KEPT = 7
    UNCERTAIN = 8


class ConfiguredWorkerAccount(Enum):
    CURRENT = "configured account"


@dataclass(frozen=True)
class DestinationWorkerAccount:
    label: str


class WorkerLockAtUse(Enum):
    ACQUIRE = "acquire the worker lock"


def _worker_lock_path(key: str) -> Path:
    return staff.record_path().parent / "locks" / f"{key}.lock"


def _worker_name_lock_path(parent: StaffId, name: str) -> Path:
    return _worker_lock_path(f"{parent}-{name}")


@contextmanager
def worker_id_lock(identity: StaffId, *, blocking: bool = False,
                   deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[None]:
    """Hold the same worker lock across renames."""
    path = _worker_lock_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        if deadline == worker_lifecycle.NO_LOCK_DEADLINE:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        else:
            try:
                worker_lifecycle.acquire_flock(handle, deadline=deadline)
            except worker_lifecycle.OperationDeadlineExpired as error:
                raise BlockingIOError(errno.EAGAIN, "dispatch lock is held") from error
        yield


@final
class WorkerLockHold:
    """Hold the worker's fixed-id lock through startup and dispatch."""

    def __init__(self, parent: StaffId, name: WorkerName, operations: ExitStack, after_unlock: ExitStack, *,
                 blocking: bool) -> None:
        self.parent = parent
        self.name = name
        self._operations = operations
        self._after_unlock = after_unlock
        self._deferred: set[Hashable] = set()
        self._held: set[StaffId] = set()
        self._blocking = blocking
        self.active = True

    def bind(self, row: staff.StaffRow, *, deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
        """Acquire the fixed-id lock before creation serialization ends."""
        if self.owns(row):
            return
        if (not self.active or self.bound or row.kind != "worker"
                or not row.reports_to(self.parent) or row.name != self.name.value):
            raise ValueError("worker lock does not own this worker")
        self._operations.enter_context(worker_id_lock(row.id, blocking=self._blocking, deadline=deadline))
        self._held.add(row.id)

    @property
    def bound(self) -> bool:
        return bool(self._held)

    def owns(self, row: staff.StaffRow) -> bool:
        return self.active and row.id in self._held and row.kind == "worker" and row.reports_to(self.parent)


    def defer_until_unlock(self, key: Hashable, action: Callable[[], None]) -> None:
        """Run one action per key after this context releases both locks."""
        if not self.active:
            raise ValueError("the worker lock is no longer active")
        if key not in self._deferred:
            self._deferred.add(key)
            _ = self._after_unlock.callback(action)


@contextmanager
def worker_lock(parent: StaffId, name: WorkerName, *, blocking: bool = False,
                deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> Generator[WorkerLockHold | Kept]:
    """Hold the name lock and the fixed-id lock through startup and dispatch.

    A deadline limits the wait for both locks.
    """
    with ExitStack() as after_unlock, ExitStack() as operations:
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        path = _worker_name_lock_path(parent, name.value)
        path.parent.mkdir(parents=True, exist_ok=True)
        creation = operations.enter_context(path.open("a"))
        try:
            if deadline == worker_lifecycle.NO_LOCK_DEADLINE:
                fcntl.flock(creation, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            else:
                worker_lifecycle.acquire_flock(creation, deadline=deadline)
        except (BlockingIOError, worker_lifecycle.OperationDeadlineExpired):
            yield Kept("worker creation lock is held" if row is None else "dispatch lock is held")
            return
        if row is None:
            row = worker_admission.find_worker(staff.snapshot(), parent, name.value)
        hold = WorkerLockHold(parent, name, operations, after_unlock, blocking=blocking)
        try:
            if row is not None:
                hold.bind(row, deadline=deadline)
        except BlockingIOError:
            yield Kept("dispatch lock is held")
            return
        try:
            yield hold
        finally:
            hold.active = False


class UseRolePolicy(Enum):
    CURRENT = "resolve the role policy"


class ToolAccess(Enum):
    ROLE_DEFAULT = "use the role's tools"
    SHOWRUNNER_REVIEW = "permit Claude read tools only"


@dataclass(frozen=True)
class LaunchPolicy:
    settings: models_registry.LaunchSettings
    sandbox: str
    permission: str
    tools: ToolAccess = ToolAccess.ROLE_DEFAULT


@dataclass(frozen=True)
class CheckedRolePolicy:
    """The launch policy that passed the check for this worker role."""

    role: WorkerRole
    launch: LaunchPolicy


_checked_resume_policies: ContextVar[Mapping[StaffId, CheckedRolePolicy]] = ContextVar("checked_resume_policies")


@contextmanager
def use_checked_resume_policies(policies: Mapping[StaffId, CheckedRolePolicy]) -> Generator[None]:
    """Keep the checked launch policy for each native conversation."""
    token = _checked_resume_policies.set(policies)
    try:
        yield
    finally:
        _checked_resume_policies.reset(token)


@dataclass(frozen=True)
class PaneEvidence:
    panes: list[dict[str, object]]
    scan: herdr.RowPaneScan
    states: list[WorkerPane]
    layout_terminals: frozenset[str]
    parent_tabs: frozenset[str]


@dataclass(frozen=True)
class SelectedWorkers:
    """Judge only these workers from one complete pane read."""

    names: frozenset[str]


@dataclass(frozen=True)
class SelectedWorkerRows:
    """Judge these fixed rows and retain incomplete absence evidence."""

    ids: frozenset[StaffId]


class AllWorkers(Enum):
    CURRENT = "all workers"


@dataclass(frozen=True)
class LaunchRecords:
    entries: dict[str, pane_launch.PaneLaunch]


@dataclass(frozen=True)
class LaunchRecordsUnavailable:
    reason: str


@dataclass(frozen=True)
class PaneRead:
    panes: list[dict[str, object]]
    scan: herdr.RowPaneScan
    statuses: dict[str, herdr.AgentStatus]
    launches: LaunchRecords | LaunchRecordsUnavailable
    launch_owners: dict[str, StaffId]


@dataclass(frozen=True)
class PaneReadUnavailable:
    reason: str


def _client() -> herdr.Herdr:
    return herdr.Herdr(herdr.shared_session())


def _text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


_DEFER_WORKER_ANOMALIES: ContextVar[bool] = ContextVar("_DEFER_WORKER_ANOMALIES", default=False)


@contextmanager
def defer_worker_anomalies() -> Generator[None]:
    """Let the caller report the reason from the returned worker state."""
    token = _DEFER_WORKER_ANOMALIES.set(True)
    try:
        yield
    finally:
        _DEFER_WORKER_ANOMALIES.reset(token)


def _uncertain(row: staff.StaffRow, reason: str) -> UncertainWorker:
    if not _DEFER_WORKER_ANOMALIES.get():
        print(f"anomaly: {row.name}: {' '.join(reason.splitlines())}; correct the evidence and retry", file=sys.stderr)
    return UncertainWorker(row, reason)


def agent_name(owner: staff.StaffRow, name: str) -> str:
    """Use the owner name to distinguish workers on the shared server."""
    prefix = f"{owner.name}-"
    return name if name.startswith(prefix) else prefix + name


def rename_workers(owner: staff.StaffRow, client: herdr.Herdr) -> None:
    """Rename worker panes before the owner name changes."""
    record = staff.snapshot()
    if not record.find(kind="worker", parent=owner.id):
        return
    for observed in _evidence(owner.id, record, client).states:
        if isinstance(observed, (OpenWorker, StoppedWorker)):
            name = agent_name(owner, observed.row.name)
            if isinstance(observed, OpenWorker):
                _ = client.run("agent", "rename", observed.pane, name)
            _ = client.retitle(observed.pane, name, tab=False)


def agent_inventory(client: herdr.Herdr) -> list[herdr.AgentOccupant]:
    """Read agent status for worker and freestanding displays."""
    return client.agents()


def _stop_row(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
    stop = cast(Callable[..., bool], import_module("scripts.agents.restart").stop_row)
    return stop(row, client, deadline=deadline)


def _read_panes(record: staff.StaffSnapshot, client: herdr.Herdr) -> PaneRead | PaneReadUnavailable:
    try:
        panes = [dict(pane) for pane in client.panes()]
        scan = client.scan_row_panes(record, panes)
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return PaneReadUnavailable(str(error))
    launches: LaunchRecords | LaunchRecordsUnavailable
    try:
        launches = LaunchRecords(pane_launch.read_valid_entries(
            pane_launch.record_directory() / f"{client.session}.json", record=record))
    except (OSError, ValueError) as error:
        launches = LaunchRecordsUnavailable(f"launch records are unavailable: {error}")
    try:
        statuses = {agent["pane_id"]: agent["agent_status"] for agent in agent_inventory(client)}
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
        statuses = {}
    # The staff owner of each launched pane, read once for all parents.
    launch_owners = ({pane: reference.id for pane, entry in launches.entries.items()
                      if isinstance(reference := pane_launch.launch_reference(entry, record), pane_launch.StaffLaunchReference)}
                     if isinstance(launches, LaunchRecords) else {})
    return PaneRead(panes, scan, statuses, launches, launch_owners)


def _parent_evidence(parent: StaffId, record: staff.StaffSnapshot,
                     read: PaneRead | PaneReadUnavailable, client: herdr.Herdr, *,
                     selection: SelectedWorkers | SelectedWorkerRows | AllWorkers = AllWorkers.CURRENT) -> PaneEvidence:
    workers = record.find(kind="worker", parent=parent)
    selected = [row for row in workers if isinstance(selection, AllWorkers)
                or isinstance(selection, SelectedWorkers) and row.name in selection.names
                or isinstance(selection, SelectedWorkerRows) and row.id in selection.ids]
    if isinstance(read, PaneReadUnavailable):
        return PaneEvidence([], herdr.RowPaneScan({}, []), [_uncertain(row, read.reason) for row in selected],
                            frozenset(), frozenset())
    panes, scan, statuses, launch_owners = read.panes, read.scan, read.statuses, read.launch_owners
    parent_panes = scan.found.get(parent, []) + [pane for pane in panes
        if launch_owners.get(_text(pane, "pane_id")) == parent
        and not any(pane in found for owner, found in scan.found.items() if owner != parent)]
    parent_tabs = {_text(pane, "tab_id") for pane in parent_panes} - {""}
    # Only a worker that launched a pane in a parent tab can recover one.
    launched_here = {launch_owners[pane_id] for pane in panes
                     if (pane_id := _text(pane, "pane_id")) in launch_owners and _text(pane, "tab_id") in parent_tabs}
    states: list[WorkerPane] = []
    for row in selected:
        matches = scan.found.get(row.id, [])
        if len(matches) > 1:
            states.append(_uncertain(row, "duplicate native identities; panes retained"))
            continue
        if matches:
            pane = matches[0]
            identity, terminal = _text(pane, "pane_id"), _text(pane, "terminal_id")
            conflicts = [key for key, found in scan.found.items() if key != row.id and pane in found]
            if not terminal or sum(_text(item, "terminal_id") == terminal for item in panes) != 1 or conflicts:
                states.append(_uncertain(row, "terminal identity is missing, duplicated, or shared"))
            else:
                states.append(OpenWorker(row, identity, terminal, statuses.get(identity, herdr.AgentStatus.UNKNOWN)))
            continue
        if isinstance(read.launches, LaunchRecordsUnavailable):
            states.append(_uncertain(row, read.launches.reason))
            continue
        recovery = [] if row.id not in launched_here else [pane for pane in pane_launch.reference_panes(
                    pane_launch.StaffLaunchReference(row.id), record, client, read.launches.entries,
                    evidence=pane_launch.NativePaneEvidence(panes, scan))
                    if launch_owners.get(_text(pane, "pane_id")) == row.id
                    and _text(pane, "tab_id") in parent_tabs
                    and not any(pane in scan.found.get(sibling.id, []) for sibling in workers)]
        related_unknown = [item for item in scan.unknown
                           if launch_owners.get(item.pane) == row.id and item.tab in parent_tabs]
        if related_unknown:
            unknown = related_unknown[0]
            location = next(pane for pane in panes if _text(pane, "pane_id") == unknown.pane)
            states.append(ProcessUnknownWorker(row, unknown.pane, _text(location, "terminal_id"), unknown.reason))
        elif len(recovery) > 1:
            states.append(_uncertain(row, "multiple recovery panes"))
        elif recovery:
            pane = recovery[0]
            identity, terminal = _text(pane, "pane_id"), _text(pane, "terminal_id")
            if not terminal or sum(_text(item, "terminal_id") == terminal for item in panes) != 1:
                states.append(_uncertain(row, "recovery terminal identity is missing or duplicated"))
                continue
            owners = [key for key, found in scan.found.items() if pane in found]
            if owners:
                states.append(_uncertain(row, "recovery pane belongs to another native session"))
                continue
            states.append(StoppedWorker(row, identity, terminal))
        elif not isinstance(selection, SelectedWorkers) and any(item.tab in parent_tabs for item in scan.unknown):
            states.append(_uncertain(row, "pane evidence is incomplete; absence is unproved"))
        else:
            states.append(ClosedWorker(row))
    terminals: frozenset[str] = frozenset()
    if isinstance(selection, (SelectedWorkers, SelectedWorkerRows)):
        worker_ids = {row.id for row in workers}
        terminals = frozenset(_text(pane, "terminal_id") for pane in panes
            if (launch_owners.get(_text(pane, "pane_id")) in worker_ids
                or any(pane in scan.found.get(row.id, []) for row in workers))
            and _text(pane, "tab_id") in parent_tabs)
    return PaneEvidence(panes, scan, states, terminals, frozenset(parent_tabs))


def _evidence(parent: StaffId, record: staff.StaffSnapshot, client: herdr.Herdr, *,
              selection: SelectedWorkers | SelectedWorkerRows | AllWorkers = AllWorkers.CURRENT) -> PaneEvidence:
    return _parent_evidence(parent, record, _read_panes(record, client), client, selection=selection)


class WorkerNotObserved(Enum):
    CURRENT = "the worker has no observation"


def observe_selected(parent: StaffId, record: staff.StaffSnapshot, names: Sequence[WorkerName]) -> list[WorkerPane]:
    """Judge the requested rows from one complete identity scan."""
    selection = SelectedWorkers(frozenset(name.value for name in names))
    return _evidence(parent, record, _client(), selection=selection).states


def observe_worker(parent: StaffId, row: staff.StaffRow, record: staff.StaffSnapshot) -> WorkerPane | WorkerNotObserved:
    """Read all pane identities and judge the selected worker."""
    current = record.by_id(row.id)
    if current is None:
        return WorkerNotObserved.CURRENT
    states = observe_selected(parent, record, [WorkerName.parse(current.name)])
    return next((state for state in states if state.row.id == current.id), WorkerNotObserved.CURRENT)


def observe(parent: StaffId, record: staff.StaffSnapshot) -> list[WorkerPane]:
    return _evidence(parent, record, _client()).states


def run_execution_blocker(run: Path, record: staff.StaffSnapshot, owner: StaffId,
                          identities: frozenset[StaffId]) -> unit_runs.RunExecutionState:
    """Check execution and launch locks without using reply evidence."""
    for path in _worker_lock_path("").parent.glob(f"{owner}-*.lock"):
        try:
            with path.open("r") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return unit_runs.RunExecutionBlocker(f"{path.stem}: a worker operation is in progress")
        except FileNotFoundError:
            continue
    for identity in sorted(identities):
        row = record.by_id(identity)
        if row is None or row.kind != "worker":
            continue
        parent = record.parent_of(row)
        if parent is None:
            return unit_runs.RunExecutionBlocker(f"{row.name}: the worker owner is unavailable")
        if worker_run(row, record=record).resolve() != run.resolve():
            continue
        state = observe_worker(parent.id, row, record)
        if isinstance(state, (ClosedWorker, StoppedWorker)):
            continue
        if not isinstance(state, OpenWorker) or not state.status.idle:
            return unit_runs.RunExecutionBlocker(f"{row.name}: the worker can still be running")
        if isinstance(locked := check_worker_lock(row), Kept):
            return unit_runs.RunExecutionBlocker(f"{row.name}: {locked.blocker}")
    return unit_runs.NoRunExecutionBlocker.IDLE


def observe_parents(parents: Sequence[staff.StaffRow], record: staff.StaffSnapshot, *,
                    selection: SelectedWorkerRows | AllWorkers = AllWorkers.CURRENT) -> dict[StaffId, list[WorkerPane]]:
    """Judge the selected workers of these parents from one pane read."""
    if not parents:
        return {}
    client = _client()
    read = _read_panes(record, client)
    return {parent.id: _parent_evidence(parent.id, record, read, client, selection=selection).states for parent in parents}


def worker_state(observed: WorkerPane) -> Literal["idle", "busy", "unknown"]:
    """Use one activity rule for staff workers and held roles."""
    if isinstance(observed, OpenWorker):
        return "idle" if observed.status in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE) else "unknown" if observed.status == herdr.AgentStatus.UNKNOWN else "busy"
    if isinstance(observed, (StoppedWorker, ClosedWorker)):
        return "idle"
    return "unknown"


def selected_pane(panes: Sequence[Mapping[str, object]], candidates: Sequence[Mapping[str, object]],
                  name: str) -> Mapping[str, object] | herdr.NoPane:
    """Require one candidate with a unique terminal."""
    if not candidates:
        return herdr.NoPane(name)
    if len(candidates) != 1:
        raise ValueError(f"{name}: several panes match")
    pane = candidates[0]
    terminal = pane.get("terminal_id")
    if not terminal or sum(item.get("terminal_id") == terminal for item in panes) != 1:
        raise ValueError(f"{name}: terminal identity is missing or duplicated")
    return pane


def worker_fallback_run(parent: staff.StaffRow, *,
                        record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL) -> Path:
    """Use an inherited run only when its staff owner matches the current parent."""
    record = staff.local_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
    current = record.by_id(parent.id)
    if current is not None and (supplied := unit_runs.environ("RUN_DIR")):
        fields = worker_lifecycle.read_json_object(Path(supplied) / recorder_state.STATE_FILENAME)
        if fields.get(unit_runs.OWNER_FIELD) == current.id:
            return Path(supplied)
    return unit_runs.state_dir() / "staff" / "workers" / parent.id


def parent_run_roots() -> tuple[Path, ...]:
    design_root = cast(Callable[[], Path], import_module("scripts.astra.design_roles").state_root)()
    return unit_runs.ASTRA_RUN_ROOT, design_root


def parent_run_index(record: staff.StaffSnapshot) -> dict[str, list[str]]:
    """Read the launcher's run roots once for all parent sessions."""
    return unit_runs.held_by_session(os.environ.get("ROSTER_RUN_ROOT"), record=record,
                                     additional_roots=parent_run_roots())


def run_state(run: Path) -> dict[str, object]:
    return worker_lifecycle.read_json_object(run / recorder_state.STATE_FILENAME)


def worker_run_directories(record: staff.StaffSnapshot, *, scanned: dict[str, list[str]] | None = None,
                           read: Callable[[Path], dict[str, object]] | None = None) -> unit_runs.LauncherRunDirectories:
    """Resolve each worker through the launcher and retain its candidate directories."""
    workers = record.find(kind="worker")
    if not workers:
        return unit_runs.LauncherRunDirectories({}, {})

    scanned = parent_run_index(record) if scanned is None else scanned
    linked = progress_history.recorded_dispatch_runs()
    read = cache(run_state) if read is None else read
    parents: dict[StaffId, tuple[list[Path], Path, tuple[Path, ...]]] = {}
    found: dict[StaffId, tuple[Path, ...]] = {}
    for row in workers:
        parent = record.parent_of(row) or row
        if parent.id not in parents:
            held = unit_runs.held(str(parent.current_session), record=record, scanned=scanned)
            recorded = (parent.run.path,) if isinstance(parent.run, staff.OpenRun) else ()
            fallback = worker_fallback_run(parent, record=record)
            runs = [Path(run) for run in _parent_runs(parent, held)]
            parents[parent.id] = (runs, runs[0] if runs else fallback,
                                  (*runs, *recorded, fallback))
        runs, first, shared = parents[parent.id]
        dispatched = linked[row.id].directory if row.id in linked else None
        try:
            selected = (_worker_run_in(row, runs, dispatched, first, record=record, read=read) if parent is not row
                        else worker_run(row, record=record, scanned=scanned))
        except recorder_state.OldRecorderState as error:
            print(error, file=sys.stderr)
            # Detached records prove their worker by staff id.
            selected = first
        found[row.id] = tuple(dict.fromkeys((selected, *shared, *((dispatched,) if dispatched is not None else ()))))
    return unit_runs.LauncherRunDirectories(found, scanned)


def worker_run(member: staff.StaffRow, *,
               record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL,
               scanned: dict[str, list[str]] | None = None) -> Path:
    """Find dispatches by fixed worker id within the current parent's runs."""
    record = staff.local_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
    current = record.by_id(member.id)
    if current is None:
        return unit_runs.state_dir() / "staff" / "workers" / member.id
    member = current
    parent = record.parent_of(member) if member.kind == "worker" else member
    if parent is None:
        return unit_runs.state_dir() / "staff" / "workers" / member.id
    if scanned is None:
        scanned = parent_run_index(record)
    runs = [Path(run) for run in _parent_runs(parent, unit_runs.held(str(parent.current_session), record=record, scanned=scanned))]
    first = runs[0] if runs else worker_fallback_run(parent, record=record)
    if member.kind != "worker":
        return first

    dispatched = progress_history.recorded_dispatch_run(member.id)
    linked = dispatched.directory if isinstance(dispatched, progress_history.RecordedDispatchRun) else None
    return _worker_run_in(member, runs, linked, first, record=record, read=run_state)


def _parent_runs(parent: staff.StaffRow, held: list[str]) -> list[str]:
    """Order the parent's open run, its held runs, then a supplied run that it owns."""
    runs = list(dict.fromkeys((str(parent.run.path), *held))) if isinstance(parent.run, staff.OpenRun) else held
    if supplied := unit_runs.environ("RUN_DIR"):
        fields = worker_lifecycle.read_json_object(Path(supplied) / recorder_state.STATE_FILENAME)
        if fields.get(unit_runs.OWNER_FIELD) == parent.id:
            runs = list(dict.fromkeys((*runs, supplied)))
    return runs


def _worker_run_in(member: staff.StaffRow, runs: Sequence[Path], linked: Path | None, fallback: Path, *,
                   record: staff.StaffSnapshot, read: Callable[[Path], dict[str, object]]) -> Path:
    """Select the parent run or linked run that holds the worker's newest pass."""
    candidates = dict.fromkeys((*runs, *(() if linked is None else (linked,)), fallback))
    passes = [(run, passed) for run in candidates if (passed := _pass_in(member, _run_state(run, read), record))]
    return max(passes, key=lambda item: _pass_started_at(item[1]))[0] if passes else fallback


def _run_state(run: Path, read: Callable[[Path], dict[str, object]] = run_state) -> dict[str, object]:
    state = read(run)
    recorder_state.require_current_shape(run / recorder_state.STATE_FILENAME, state)
    return state


def _passes(state: dict[str, object]) -> list[dict[str, object]]:
    value = state.get("pass", {})
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    return [fields] if "instance_id" in fields else [cast(dict[str, object], item)
        for item in fields.values() if isinstance(item, dict)]


def _pass_started_at(item: dict[str, object]) -> float:
    value = item.get("started_at")
    return value if isinstance(value, (int, float)) else float("-inf")


def worker_pass(row: staff.StaffRow, run: Path, *,
                record: staff.StaffSnapshot | None = None) -> dict[str, object]:
    """Select the fixed participant after checking its current staff parent."""
    record = staff.local_snapshot() if record is None else record
    return _pass_in(row, _run_state(run), record)


def _pass_in(row: staff.StaffRow, fields: dict[str, object], record: staff.StaffSnapshot) -> dict[str, object]:
    no_pass: dict[str, object] = {}
    return max(_worker_passes_in(row, fields, record), key=_pass_started_at, default=no_pass)


def _worker_passes_in(row: staff.StaffRow, fields: dict[str, object],
                      record: staff.StaffSnapshot) -> list[dict[str, object]]:
    current = record.by_id(row.id)
    owner = unit_runs.run_owner(fields, record)
    if (current is None or current.kind != "worker" or not isinstance(owner, staff.StaffRow)
            or current.parent != staff.ReportsTo(owner.id)):
        return []
    return [item for item in reversed(_passes(fields)) if item.get("worker_id") == current.id]


def dispatch_role(row: staff.StaffRow) -> WorkerRole | WorkerRoleUnavailable:
    """Read the worker role from its staff row."""
    return row.role.value if isinstance(row.role, staff.KnownWorkerRole) else WorkerRoleUnavailable("dispatch role is unavailable")


class NoTranscriptTurn(Enum):
    UNRECORDED = "no verified turn after the dispatch prompt"


@dataclass(frozen=True)
class TranscriptCursor:
    """Identify a boundary within one transcript stream."""

    stream: str
    position: int
    prefix: str

    def at_or_after(self, boundary: TranscriptCursor) -> bool:
        return self.stream == boundary.stream and self.position >= boundary.position

    @classmethod
    def from_json(cls, value: object) -> TranscriptCursor | NoTranscriptTurn:
        if not isinstance(value, dict):
            return NoTranscriptTurn.UNRECORDED
        fields = cast(dict[str, object], value)
        stream, position, prefix = fields.get("stream"), fields.get("position"), fields.get("prefix")
        if (not isinstance(stream, str) or not stream or not isinstance(position, int) or isinstance(position, bool)
                or position < 1 or not isinstance(prefix, str) or not re.fullmatch(r"[a-f0-9]{64}", prefix)):
            return NoTranscriptTurn.UNRECORDED
        return cls(stream, position, prefix)


def worker_input_digest(text: str) -> str:
    """Hash the input text with the transcript's trailing whitespace rule."""
    return hashlib.sha256(text.rstrip().encode()).hexdigest()


def transcript_stream(row: staff.StaffRow, source: Path) -> str:
    return f"{row.current_session}:{uncompressed_path(source.resolve())}"


def transcript_record_cursors[T: str | dict[str, object] | worker_admission.TornTranscriptRecord](
    entries: Iterable[T], stream: str,
) -> Generator[tuple[T, TranscriptCursor]]:
    """Identify each record by its position and the hash of the transcript prefix."""
    prefix = hashlib.sha256()
    for position, entry in enumerate(entries, 1):
        data = None if isinstance(entry, worker_admission.TornTranscriptRecord) else entry
        prefix.update(json.dumps(data, sort_keys=True).encode() + b"\n")
        yield entry, TranscriptCursor(stream, position, prefix.hexdigest())


def _transcript_messages(row: staff.StaffRow) -> list[str] | Kept:
    try:
        backend = staff.backend(row)
        if isinstance(backend, staff.BackendUnavailable):
            return Kept("worker transcript backend is unavailable")
        source = worker_admission.transcript_source(row, backend)
        entries = list(worker_admission.transcript_entries(source, set()))
        if any(isinstance(entry, worker_admission.TornTranscriptRecord) for entry in entries):
            return Kept("worker transcript has incomplete records")
        return [entry for entry in entries if isinstance(entry, str) and entry]
    except (OSError, ValueError) as error:
        return Kept(f"completion evidence is unavailable: {error}")


def _completed_turn(row: staff.StaffRow, dispatch: object) -> CloseAllowed | Kept:
    messages = _transcript_messages(row)
    if isinstance(messages, Kept):
        return messages
    if (not isinstance(dispatch, str) or not dispatch or not messages
            or not messages[-1].startswith("assistant:\n")
            or not any(worker_admission.reply_ends_dispatch(messages[-1], identity)
                       for identity in (dispatch, stage_two_completion_id(dispatch)))):
        return Kept("last turn does not end with the dispatch DONE line")
    return CloseAllowed()


@dataclass(frozen=True)
class PassDetachedResults:
    instance_id: str
    started_at: float
    live_runs_checked: bool


class RecordedCompletionEvidence(Protocol):
    def completion_close_evidence(self, row: staff.StaffRow, delivery: pass_recorder.DispatchDelivery, *,
                                  run: Path, reply_file: str) -> CloseAllowed | Kept: ...

    def pass_reply_evidence(self, row: staff.StaffRow, delivery: pass_recorder.DispatchDelivery, *,
                            run: Path, reply_file: str, require_completion: bool,
                            detached_results: PassDetachedResults) -> CloseAllowed | Kept: ...


def recorded_completion_close(row: staff.StaffRow, run: Path, passed: dict[str, object], *,
                              worker_is_working: bool = False) -> CloseAllowed | Kept:
    """Accept a finished hook or check the saved reply against the transcript."""

    completion = progress_history.hook_completion(passed)
    if isinstance(completion, progress_history.HookCompletion) and completion.finished:
        return Kept("agent state is working") if worker_is_working else CloseAllowed()
    delivery = pass_recorder.dispatch_delivery(passed)
    if not delivery.line or not delivery.completion_id:
        return Kept("the dispatch has no saved completion id")
    reply_file = completion.reply_file if isinstance(completion, progress_history.HookCompletion) else delivery.reply_file
    receipts = cast(RecordedCompletionEvidence, cast(object, import_module("scripts.production.worker")))
    return receipts.completion_close_evidence(row, delivery, run=run, reply_file=reply_file)


NO_FINAL_REPLY = "last turn does not end with an assistant reply"


def _final_reply(row: staff.StaffRow) -> CloseAllowed | Kept:
    messages = _transcript_messages(row)
    if isinstance(messages, Kept):
        return messages
    if not messages or not messages[-1].startswith("assistant:\n"):
        return Kept(NO_FINAL_REPLY)
    return CloseAllowed()


@dataclass(frozen=True)
class DispatchHoldsWorker:
    reason: str | CompletionRecovery
    parked: bool = False

    @property
    def blocker(self) -> str:
        return self.reason if isinstance(self.reason, str) else self.reason.value


@dataclass(frozen=True)
class CompletedWorkerPass:
    dispatch_id: str


class StoppedPassCleanup(Enum):
    COMPLETE = "no stop cleanup needs a retry"
    RETRY = "retry the stop cleanup"


class StoppedDiagnosticRecovery(Enum):
    SETTLED = "all diagnostics were delivered"
    PENDING = "diagnostic delivery remains pending"
    UNREADABLE = "diagnostic evidence is unreadable"


@dataclass(frozen=True)
class StoppedRunPassCompletion:
    outcome: str
    send_notice: Callable[[], StoppedPassCleanup]


class NoOwningPanePass(Enum):
    UNRECORDED = "the run has no open or stopped pane pass"


class ReleasedWorkerPass(Enum):
    NO_PROMPT_DELIVERED = "no prompt was delivered"
    ENDED = "the pass ended"
    STALE_ACTIVE = "the launcher ended without finishing the pass"


WorkerDispatchState = DispatchHoldsWorker | CompletedWorkerPass | ReleasedWorkerPass


class DispatchLockAvailable(Enum):
    AVAILABLE = "the dispatch lock does not block this operation"


def check_worker_lock(row: staff.StaffRow,
                      lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE, *,
                      include_name: bool = False) -> DispatchLockAvailable | Kept:
    """Check the fixed-ID lock and optionally the name lock without creating files."""
    if not isinstance(lock, WorkerLockHold) or not lock.owns(row):
        paths = [_worker_lock_path(row.id)]
        if include_name and isinstance(row.parent, staff.ReportsTo):
            paths.insert(0, _worker_name_lock_path(row.parent.id, row.name))
        for path in paths:
            try:
                with path.open("r") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return Kept("dispatch lock is held")
            except FileNotFoundError:
                continue
    return DispatchLockAvailable.AVAILABLE


class RunningDetachedWorkers(Protocol):
    @property
    def staff_ids(self) -> frozenset[staff.StaffId]: ...

    @property
    def unreadable_workers(self) -> frozenset[staff.StaffId]: ...


class DetachedWorkAtUse(Enum):
    READ = "read detached runs for this worker"


def worker_dispatch_state(row: staff.StaffRow, run: Path,
                          lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE, *,
                          detached: RunningDetachedWorkers | DetachedWorkAtUse = DetachedWorkAtUse.READ,
                          record: staff.StaffSnapshot | None = None, related_runs: Sequence[Path] = (),
                          read: Callable[[Path], dict[str, object]] = run_state,
                          ) -> WorkerDispatchState:
    """Check the worker lock, detached runs, and all saved passes for this worker."""

    locked = check_worker_lock(row, lock)
    if isinstance(locked, Kept):
        return DispatchHoldsWorker(locked.blocker)
    if isinstance(detached, DetachedWorkAtUse):
        live_runs = cast(Callable[..., Sequence[object]], import_module("scripts.agents.detached_runs").live_runs)
        live = bool(live_runs(run / "detached_runs", row.name, identity=row.id))
    else:
        if row.id in detached.unreadable_workers:
            return DispatchHoldsWorker("a detached run record is unreadable")
        live = row.id in detached.staff_ids
    if live:
        membership = staff.local_snapshot() if record is None else record
        passed = _pass_in(row, _run_state(run, read), membership)
        return DispatchHoldsWorker("a detached run is live", parked=progress_history.parked_worker_pass(passed))
    record = staff.local_snapshot() if record is None else record
    if isinstance(detached, DetachedWorkAtUse):
        # Direct pane operations use the current pass.
        passed = worker_pass(row, run, record=record)
        if passed:
            return _pass_dispatch_state(row, run, passed, passed.get("status"), passed.get("instance_id"),
                                        live_runs_checked=False)
        passes = []
    else:
        passes = [(directory, item) for directory in dict.fromkeys((run, *related_runs))
                  for item in _worker_passes_in(row, _run_state(directory, read), record)]
    if not passes:
        latest = progress_history.last_dispatch(row.id)
        if isinstance(latest, progress_history.DispatchHistoryUnknown):
            return DispatchHoldsWorker(latest.reason)
        if (not isinstance(latest, progress_history.RecordedDispatch)
                or latest.status is progress_history.DispatchDetailNotRecorded.UNRECORDED):
            return ReleasedWorkerPass.NO_PROMPT_DELIVERED
        return _pass_dispatch_state(row, run, {}, latest.status, latest.instance_id, live_runs_checked=False)

    passes.sort(key=lambda item: _pass_started_at(item[1]), reverse=True)
    directory, passed = passes[0]
    newest = _pass_dispatch_state(row, directory, passed, passed.get("status"), passed.get("instance_id"),
                                  live_runs_checked=True)
    if isinstance(newest, DispatchHoldsWorker):
        return newest
    for directory, passed in passes[1:]:
        if passed.get("status") == "active":
            state = _pass_dispatch_state(row, directory, passed, "active", passed.get("instance_id"),
                                         live_runs_checked=True)
            if isinstance(state, DispatchHoldsWorker):
                return state
    return newest


def _pass_dispatch_state(row: staff.StaffRow, run: Path, passed: dict[str, object], status: object,
                         dispatch: object, *, live_runs_checked: bool) -> WorkerDispatchState:
    if status == "canceled":
        return ReleasedWorkerPass.ENDED
    delivery = pass_recorder.dispatch_delivery(passed)
    completion = progress_history.hook_completion(passed)
    active = status == "active" and bool(delivery.line)
    finished = isinstance(completion, progress_history.HookCompletion) and completion.finished
    reply_ended = (isinstance(completion, progress_history.HookCompletion) and completion.outcome == "stopped"
                   and completion.stopped_reply is progress_history.StoppedReplyDisposition.ENDED_WITHOUT_DONE)
    if active or (delivery.line and delivery.completion_id and not (finished or reply_ended)):
        evidence = cast(RecordedCompletionEvidence, cast(object, import_module("scripts.production.worker")))
        reply_file = completion.reply_file if isinstance(completion, progress_history.HookCompletion) else delivery.reply_file
        started_at = _pass_started_at(passed)
        hold = evidence.pass_reply_evidence(row, delivery, run=run, reply_file=reply_file, require_completion=active,
            detached_results=PassDetachedResults(dispatch if isinstance(dispatch, str) else "",
                                               0 if started_at == float("-inf") else started_at, live_runs_checked))
        if isinstance(hold, Kept):
            return DispatchHoldsWorker(hold.reason, parked=progress_history.parked_worker_pass(passed))
        if active:
            return CompletedWorkerPass(dispatch if isinstance(dispatch, str) else "")
    if status == "completed":
        if isinstance(completion, progress_history.HookCompletion) and completion.outcome == "done":
            return ReleasedWorkerPass.ENDED
        return CompletedWorkerPass(dispatch if isinstance(dispatch, str) else "")
    if status == "active":
        pid, command = passed.get("launcher_pid"), passed.get("launcher_command")
        if isinstance(pid, int) and isinstance(command, list):
            parts = cast(list[object], command)
            if len(parts) > 1 and all(isinstance(part, str) for part in parts):
                saved = cast(list[str], parts)
                launcher = [str(Path(saved[1]).with_suffix(".py")), *saved[2:]]
                if detached_run_process.runs_command(pid, launcher):
                    return DispatchHoldsWorker("launcher is live")
        return ReleasedWorkerPass.STALE_ACTIVE
    if status in ("", "error", "refused"):
        return ReleasedWorkerPass.NO_PROMPT_DELIVERED
    if status in ("canceled", "interrupted"):
        return ReleasedWorkerPass.ENDED
    return DispatchHoldsWorker("launcher has not recorded a completed pass")


def _idle_checks(row: staff.StaffRow, run: Path,
                 lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE) -> CloseAllowed | Kept:
    state = worker_dispatch_state(row, run, lock)
    return Kept(state.reason) if isinstance(state, DispatchHoldsWorker) else CloseAllowed()


def stage_two_completion_id(dispatch_id: str) -> str:
    """Return the completion id for the second review stage."""
    return dispatch_id + "-2"


@dataclass(frozen=True)
class ParkWaitingPass:
    """Close an idle pane while its accepted pass waits only on live runs."""

    run: Path
    instance: str
    stage: str


class CloseEndedPane(Enum):
    CHECK = "use the normal closure rules"


def can_close(worker: OpenWorker | StoppedWorker | ClosedWorker, *,
              run: DispatchRun | HolderRun = HolderRun.CURRENT,
              lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE,
              park: ParkWaitingPass | CloseEndedPane = CloseEndedPane.CHECK) -> CloseAllowed | Kept:
    record = staff.snapshot()
    parent = record.parent_of(worker.row)
    if parent is None:
        return Kept("worker parent is unavailable")
    if isinstance(park, ParkWaitingPass):
        if not isinstance(lock, WorkerLockHold) or not lock.owns(worker.row):
            return Kept("the worker lock is unavailable for parking")
        if not isinstance(worker, OpenWorker) or not worker.status.idle:
            return Kept("the worker pane is no longer idle")
        if worker_run(worker.row, record=record) != park.run:
            return Kept("the worker run changed before parking")
        return CloseAllowed()
    directory = run.directory if isinstance(run, DispatchRun) else worker_run(worker.row, record=record)
    if isinstance(worker, ClosedWorker):
        return _idle_checks(worker.row, directory, lock)
    state = worker_dispatch_state(worker.row, directory, lock)
    if isinstance(state, DispatchHoldsWorker):
        return Kept(state.reason)

    working = isinstance(worker, OpenWorker) and worker.status == herdr.AgentStatus.WORKING
    if isinstance(state, CompletedWorkerPass):
        passed = worker_pass(worker.row, directory)
        if pass_recorder.dispatch_delivery(passed).line:
            completion = recorded_completion_close(worker.row, directory, passed, worker_is_working=working)
        elif isinstance(worker, StoppedWorker):
            return CloseAllowed()
        else:
            completion = _completed_turn(worker.row, state.dispatch_id)
    elif state is ReleasedWorkerPass.ENDED:
        passed = worker_pass(worker.row, directory)
        if passed.get("status") == "canceled":
            if isinstance(worker, OpenWorker) and not worker.status.idle:
                return Kept(f"agent state is {worker.status.value}")
            return CloseAllowed()
        hook = progress_history.hook_completion(passed)
        if isinstance(hook, progress_history.HookCompletion) and hook.outcome == "stopped":
            if isinstance(worker, OpenWorker) and not worker.status.idle:
                return Kept(f"agent state is {worker.status.value}")
            if hook.stopped_reply is progress_history.StoppedReplyDisposition.ENDED_WITHOUT_DONE:
                return CloseAllowed()
        delivery = progress_history.dispatch_delivery(passed)
        if delivery.line:
            completion = recorded_completion_close(worker.row, directory, passed, worker_is_working=working)
        elif isinstance(worker, StoppedWorker):
            return CloseAllowed()
        elif isinstance(hook, progress_history.HookCompletion) and hook.outcome in ("done", "late DONE"):
            completion = _completed_turn(worker.row, passed.get("instance_id"))
        else:
            return (CloseAllowed() if worker.status.idle else Kept(f"agent state is {worker.status.value}"))
    elif isinstance(worker, StoppedWorker):
        return CloseAllowed()
    else:
        return _final_reply(worker.row)
    if isinstance(worker, OpenWorker) and worker.status not in (herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE):
        if isinstance(completion, Kept):
            return Kept(f"agent state is {worker.status.value}")
    return completion


def launch_policy(role: WorkerRole, family: Literal["claude", "codex", "assigned"] = "assigned") -> LaunchPolicy:
    """Read the role's launch policy; a named family reads that family's row instead of the assigned one."""
    section, key = ("design_roles", role.removeprefix("astra-")) if role.startswith("astra-") else ("direct", role)
    settings = models_registry.launch_settings(section, key, family)
    sandbox = READ_ONLY_SANDBOX if role in REVIEW_ROLES else WORKER_SANDBOX
    if role == "astra-designer":
        sandbox = DESIGNER_SANDBOX
    return _launch_policy(settings, sandbox)


def checked_role_policy(role: WorkerRole | WorkerRoleUnavailable,
                        family: Literal["claude", "codex", "assigned"] = "assigned") -> CheckedRolePolicy | StartRefused:
    """Read the role policy or give the reason it cannot start a worker."""
    if isinstance(role, WorkerRoleUnavailable):
        return StartRefused(StartRefusal.ROLE_UNAVAILABLE, role.reason)
    if role not in staff.WORKER_ROLES:
        return StartRefused(StartRefusal.UNKNOWN_ROLE, str(role))
    try:
        return CheckedRolePolicy(role, launch_policy(role, family))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, f"{role}: {error}")


def parked_launch_policy(row: staff.StaffRow, passed: dict[str, object]
                         ) -> LaunchPolicy | progress_history.ParkedLaunchUnavailable:
    """Use the saved policy only for the same native conversation."""
    from scripts.direct import progress_history

    saved = progress_history.parked_worker_launch(passed)
    backend = staff.backend(row)
    if (isinstance(saved, progress_history.ParkedLaunchUnavailable)
            or isinstance(backend, staff.BackendUnavailable) or saved.family != backend.family
            or saved.thread != row.current_session):
        return progress_history.ParkedLaunchUnavailable.MISSING
    settings = models_registry.LaunchSettings(saved.family, saved.model, saved.effort, saved.service_tier)
    return LaunchPolicy(settings, saved.sandbox, saved.permission)


def astra_review_policy(model: str, effort: str) -> LaunchPolicy:
    """Use the supplied review settings with fixed read-only Codex access."""
    settings = models_registry.LaunchSettings("codex", model, effort, "")
    return LaunchPolicy(settings, READ_ONLY_SANDBOX, "plan")


def worker_policy(settings: models_registry.LaunchSettings) -> LaunchPolicy:
    """Use the worker's file permissions with the supplied model settings."""
    return _launch_policy(settings, WORKER_SANDBOX)


def _launch_policy(settings: models_registry.LaunchSettings, sandbox: str) -> LaunchPolicy:
    permission = "plan" if sandbox == READ_ONLY_SANDBOX else "bypassPermissions"
    return LaunchPolicy(settings, sandbox, permission)


def turn_end_command() -> list[str]:
    """Build the shared turn handler command for the Claude launch settings."""
    scripts = Path(__file__).resolve().parents[1]
    return [str(scripts / "lib/py"), str(scripts / "herdr/turn_end.py")]


def _turn_end_arguments(kind: Literal["codex", "claude"]) -> list[str]:
    if kind == "codex":
        return []
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": shlex.join(turn_end_command()), "async": True}]}]}}
    return ["--settings", json.dumps(hooks, separators=(",", ":"))]


def _policy_arguments(policy: LaunchPolicy) -> list[str]:
    settings = policy.settings
    if settings.kind == "codex":
        arguments = ["--model", settings.model, "--sandbox", policy.sandbox, "--ask-for-approval", "never",
                      "-c", f'model_reasoning_effort="{settings.effort}"',
                      "-c", "notice.hide_rate_limit_model_nudge=true"]
        if settings.service_tier:
            arguments += ["-c", f'service_tier="{settings.service_tier}"']
    else:
        arguments = ["--model", settings.model, "--permission-mode", policy.permission]
        if settings.effort and settings.effort != "unset":
            arguments += ["--effort", settings.effort]
        if policy.tools is ToolAccess.SHOWRUNNER_REVIEW:
            arguments += CLAUDE_REVIEW_ARGUMENTS
    return arguments + _turn_end_arguments(settings.kind)


def _claude_worker_settings(settings: models_registry.LaunchSettings,
                            preferred: pane_record.ClaudeTurnSettings | pane_record.NoClaudeTurn
                            | pane_record.ClaudeResumeSettings) -> models_registry.LaunchSettings:
    resume = pane_record.claude_resume_settings(preferred,
        fallback=pane_record.ClaudeResumeSettings(settings.model, settings.effort))
    return settings._replace(model=resume.model, effort=resume.effort)


def kept_settings(settings: models_registry.LaunchSettings, arguments: Sequence[str]) -> models_registry.LaunchSettings:
    """Keep the saved launch settings and use the role for missing Claude flags."""
    if settings.kind == "claude":
        saved = pane_record.parse_claude_flags(arguments).values
        return _claude_worker_settings(settings, pane_record.ClaudeResumeSettings(
            saved.get("--model", ""), saved.get("--effort", "")))
    recorded = {"--model": "", "--effort": "", "model_reasoning_effort": "", "service_tier": ""}
    for flag, value in zip(arguments, arguments[1:]):
        name, _, setting = value.partition("=") if flag == "-c" else (flag, "", value)
        if name in recorded:
            recorded[name] = setting.strip('"')
    if not recorded["--model"]:
        return settings
    return settings._replace(model=recorded["--model"], effort=recorded["--effort"] or recorded["model_reasoning_effort"],
                             service_tier=recorded["service_tier"])


def launch_arguments(policy: LaunchPolicy, identity: str, directory: Path | str) -> list[str]:
    if policy.settings.kind == "codex":
        launch = (["resume", identity] if identity else []) + ["--no-daemon"]
        if identity:
            launch += ["--cd", str(directory)]
    else:
        launch = ["--resume", identity] if identity else []
    return launch + _policy_arguments(policy)


def launch_environment(policy: LaunchPolicy, account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection,
                         resume: bool) -> dict[str, str] | StartRefused:
    from scripts.accounts import accounts
    from scripts.whoami import account as account_reader, agent_accounts
    try:
        if policy.settings.kind == "codex":
            if isinstance(account, accounts.CodexWorkersAccount):
                return {"CODEX_HOME": str(account.home)}
        elif isinstance(account, accounts.NamedAccount):
            return account_reader.named_account_environment(account.dir)
        if isinstance(account, (accounts.CodexWorkersAccount, accounts.NamedAccount)):
            family = "codex" if isinstance(account, accounts.CodexWorkersAccount) else "claude"
            print(f"anomaly: {account.label}: account family {family} does not match the {policy.settings.kind} worker; "
                  + "using the configured account", file=sys.stderr)
            account = ConfiguredWorkerAccount.CURRENT
        if policy.settings.kind == "codex":
            selected = (agent_accounts.codex_account_home(account.label) if isinstance(account, DestinationWorkerAccount)
                        else accounts.role_account("workers"))
            if isinstance(selected, agent_accounts.CodexSetupHome):
                return StartRefused(StartRefusal.ACCOUNT_UNAVAILABLE, f"{selected.label}: account is not ready")
            return {"CODEX_HOME": str(selected.home)}
        if isinstance(account, DestinationWorkerAccount):
            readiness = accounts.account_directory_readiness(account.label)
            if not isinstance(readiness, accounts.AccountDirectoryReady):
                return StartRefused(StartRefusal.ACCOUNT_UNAVAILABLE, "; ".join(readiness.problems))
            selected_claude = accounts.prepare_claude_launch(account.label, resume=resume)
            return selected_claude.variables
        selected_claude_account = accounts.account_for_unscoped_launch(os.environ, workers=True)
        return (account_reader.named_account_environment(selected_claude_account.dir)
                if isinstance(selected_claude_account, accounts.NamedAccount) else {})
    except (OSError, ValueError) as error:
        return StartRefused(StartRefusal.ACCOUNT_UNAVAILABLE, str(error))


def _argument_values(arguments: Sequence[str], flags: frozenset[str]) -> list[str]:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        flag, equals, value = argument.partition("=")
        if flag in flags:
            values.append(value if equals else arguments[index + 1] if index + 1 < len(arguments) else "")
    return values


def _codex_read_only(arguments: Sequence[str]) -> bool:
    if any(arg.partition("=")[0] in ("--yolo", "--dangerously-bypass-approvals-and-sandbox", "--full-auto")
           for arg in arguments):
        return False
    if _argument_values(arguments, frozenset({"--sandbox", "-s"})) != [READ_ONLY_SANDBOX]:
        return False
    if _argument_values(arguments, frozenset({"--ask-for-approval", "-a"})) != ["never"]:
        return False
    return all(setting.partition("=")[0].strip() in
               ("model_reasoning_effort", "notice.hide_rate_limit_model_nudge", "service_tier")
               for setting in _argument_values(arguments, frozenset({"-c", "--config"})))


def require_review_process(client: herdr.Herdr, pane: str, paths: EnrolledPaths) -> None:
    """Prove the running worker uses the review tools in main."""
    info = herdr.json_object(client.result("pane", "process-info", "--pane", pane).get("process_info"), "review launch")
    owner = herdr.foreground_pane_owner(info)
    if not isinstance(owner, (herdr.ProvedCodexForeground, herdr.ProvedClaudeForeground)):
        raise ValueError("the running showrunner review process is unproved")
    if not isinstance(owner.cwd, str) or Path(owner.cwd).resolve() != paths.main:
        raise ValueError("the retained showrunner worker does not run in main")
    actual = owner.arguments
    if isinstance(owner, herdr.ProvedCodexForeground):
        if _codex_read_only(actual):
            return
    elif (not any(arg.partition("=")[0] in ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                                           "--allowedTools", "--allowed-tools", "--plugin-dir", "--add-dir") for arg in actual)
          and _argument_values(actual, frozenset({"--permission-mode"})) == ["plan"]
          and _argument_values(actual, frozenset({"--tools"})) == [CLAUDE_REVIEW_TOOLS]
          and _argument_values(actual, frozenset({"--mcp-config"})) == [CLAUDE_REVIEW_MCP]
          and "--strict-mcp-config" in actual):
        return
    raise ValueError("the retained showrunner worker does not have the read-only review policy")


def _live_policy(worker: OpenWorker, policy: LaunchPolicy, client: herdr.Herdr, *, same_role: bool) -> bool | UncertainWorker:
    try:
        info = herdr.json_object(client.result("pane", "process-info", "--pane", worker.pane).get("process_info"), "worker launch policy")
        owner = herdr.foreground_pane_owner(info)
        if isinstance(owner, (herdr.NoForegroundAgent, herdr.UnprovedForegroundOwner)) or owner.kind != policy.settings.kind:
            return _uncertain(worker.row, "running launch policy is unproved")
        expected = _policy_arguments(policy)
        actual = owner.arguments
        required = ("--model", "--sandbox", "--ask-for-approval") if policy.settings.kind == "codex" else ("--model", "--permission-mode")
        if any(flag not in actual for flag in required):
            return _uncertain(worker.row, "running launch policy is incomplete")
        pairs = [actual[pos:pos + 2] for pos in range(len(actual) - 1)]
        same_permissions = (["--sandbox", policy.sandbox] in pairs and ["--ask-for-approval", "never"] in pairs
                            if policy.settings.kind == "codex" else ["--permission-mode", policy.permission] in pairs)
        if same_permissions and same_role:
            hook_arguments = _turn_end_arguments(policy.settings.kind)
            return not hook_arguments or hook_arguments in pairs
        if policy.settings.kind == "claude" and ["--model", policy.settings.model] not in pairs:
            launched = herdr.read_foreground_pane_owner(owner, {})
            expected = models_registry.claude_arguments(expected, launched.environment)
        return all(expected[index:index + 2] in [actual[pos:pos + 2] for pos in range(len(actual) - 1)]
                   for index in range(0, len(expected), 2))
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        return _uncertain(worker.row, f"running launch policy is unproved: {error}")


def _interrupted_pane(parent: staff.StaffRow, name: str, evidence: PaneEvidence,
                      client: herdr.Herdr) -> Mapping[str, object] | herdr.NoPane | StartRefused | UncertainWorker | ProcessUnknownWorker:
    """Recover an interrupted start only when no record or native staff owner claims its pane."""
    parents = evidence.scan.found.get(parent.id, [])
    tab = _text(parents[0], "tab_id") if len(parents) == 1 else ""
    if not tab:
        return herdr.NoPane(name)
    try:
        records = pane_launch.read_strict(pane_launch.record_directory() / f"{client.session}.json")
        record = staff.snapshot()
        references = pane_launch.named_references(name, record, records) | pane_launch.named_references(
            agent_name(parent, name), record, records)
        for reference in references:
            if isinstance(reference, pane_launch.StaffLaunchReference):
                continue
            matches = pane_launch.reference_panes(reference, record, client, records,
                evidence=pane_launch.NativePaneEvidence(evidence.panes, evidence.scan))
            candidates = [pane for pane in matches if _text(pane, "tab_id") == tab
                          and (not (identities := pane_launch.native_sessions(client, pane))
                               or reference.session in identities)]
            selected = selected_pane(evidence.panes, candidates, name)
            if not isinstance(selected, herdr.NoPane):
                return selected
        # Interrupted worker starts use names only before a native identity and launch record exist.
        for label in (agent_name(parent, name), name):
            candidates = [pane for pane in evidence.panes
                if _text(pane, "tab_id") == tab and pane.get("label") == label
                and _text(pane, "pane_id") not in records]
            pending: list[Mapping[str, object]] = []
            for pane in candidates:
                if any(pane in found for found in evidence.scan.found.values()):
                    return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "recovery pane holds another live staff session")
                try:
                    identities = pane_launch.native_sessions(client, pane)
                except (herdr.CodexPaneStarting, herdr.CodexSessionPending):
                    identities = set[str]()
                except (herdr_types.HerdrFailure, OSError, ValueError) as error:
                    return ProcessUnknownWorker(parent, _text(pane, "pane_id"), _text(pane, "terminal_id"), str(error))
                if not identities:
                    pending.append(pane)
            selected = selected_pane(evidence.panes, pending, name)
            if not isinstance(selected, herdr.NoPane):
                return selected
    except (herdr_types.HerdrFailure, OSError, ValueError) as error:
        return _uncertain(parent, str(error))
    return herdr.NoPane(name)


def _design_launch_pane(client: herdr.Herdr, launch: DesignPaneLaunch) -> herdr.ExistingPane | herdr.NoPane:
    """Find the terminal from the marker set when herdr split its pane."""
    panes = client.panes()
    matches: list[herdr.ExistingPane] = []
    unreadable = ""
    for row in panes:
        pane = row["pane_id"]
        try:
            info = herdr.json_object(client.result("pane", "process-info", "--pane", pane).get("process_info"),
                                     "design pane process info")
            shell = info.get("shell_pid")
            if type(shell) is not int or shell <= 0:
                raise herdr_types.HerdrFailure("pane shell identity is unavailable")
            marker = herdr.process_environment(shell).get(DESIGN_PANE_LAUNCH_ENV)
        except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            if not unreadable:
                unreadable = f"{pane}: {error}"
            continue
        if marker == str(launch.token):
            terminal = row.get("terminal_id", "")
            if not terminal or sum(item.get("terminal_id") == terminal for item in panes) != 1:
                raise herdr_types.HerdrFailure(f"{pane}: pane terminal identity is unavailable or duplicated")
            matches.append(herdr.ExistingPane(pane, terminal))
    if len(matches) > 1:
        raise herdr_types.HerdrFailure("the design launch marker identifies more than one terminal")
    if not matches and unreadable:
        raise herdr_types.HerdrFailure(f"design pane absence is unproved: {unreadable}")
    return matches[0] if matches else herdr.NoPane(str(launch.token))


def _design_launch_process(client: herdr.Herdr, location: herdr.ExistingPane, launch: DesignPaneLaunch,
                           ) -> herdr.ForegroundPaneAgent | herdr.PaneAtShell:
    observed = client.pane_row(location.pane)
    if isinstance(observed, herdr.NoPane) or observed.get("terminal_id") != location.terminal:
        raise herdr_types.HerdrFailure("the design launch terminal changed")
    info = herdr.json_object(client.result("pane", "process-info", "--pane", location.pane).get("process_info"),
                             "design pane process info")
    owner = herdr.foreground_pane_owner(info)
    if isinstance(owner, (herdr.ProvedCodexForeground, herdr.ProvedClaudeForeground)):
        if herdr.process_environment(owner.pid).get(DESIGN_PANE_LAUNCH_ENV) == str(launch.token):
            return owner
    elif isinstance(owner, herdr.NoForegroundAgent) and isinstance(client.pane_activity(location.pane), herdr.PaneAtShell):
        return herdr.PaneAtShell()
    raise herdr_types.HerdrFailure("the design launch does not prove the foreground process")


def recover_design_launch(parent: StaffId, name: WorkerName, claim: staff.DesignWorkerClaim,
                          launch: DesignPaneLaunch, run: Path) -> None:
    """Publish the proved native worker from an interrupted launch."""
    allowed = worker_admission.WorkerStartAllowed(name.value, parent, run, worker_admission.NewWorkerSlot(), claim)
    with staff.row_operation(parent), worker_lock(parent, name, blocking=True) as lock:
        if isinstance(lock, Kept):
            raise worker_admission.AdmissionRefused(lock.blocker)
        with worker_admission.worker_operation(allowed):
            client = _client()
            location = _design_launch_pane(client, launch)
            if isinstance(location, herdr.NoPane):
                raise worker_admission.AdmissionRefused("no recorded worker; replacement refused; run end to clear the claim")
            owner = _design_launch_process(client, location, launch)
            if isinstance(owner, herdr.PaneAtShell):
                raise worker_admission.AdmissionRefused("the design pane has no agent; run end to close the pane")
            process = client.pane_agent_process(location.pane, location.terminal)
            if isinstance(process.start_ticks, herdr.ProcessGenerationUnavailable) or process.pid != owner.pid:
                raise herdr_types.HerdrFailure("the design launch process changed")
            if isinstance(owner, herdr.ProvedCodexForeground):
                _ = _codex_start_session(client, location.pane)
            native = client.native_pane_process(location.pane, owner.kind, herdr.PROGRAM_TIMEOUT_SECONDS)
            if (native.environment.get(DESIGN_PANE_LAUNCH_ENV) != str(launch.token)
                    or client.pane_agent_process(location.pane, location.terminal) != process):
                raise herdr_types.HerdrFailure("the design launch process changed")
            codex_home = native.environment.get("CODEX_HOME", "") if owner.kind == "codex" else ""
            home = staff.SessionHome(Path(codex_home)) if codex_home else staff.HomeUnknown()
            row = worker_admission.publish(allowed, native.current_identity, launch_home=home).row
            role: WorkerRole = "astra-designer" if claim.role == "designer" else "astra-reviewer"
            _ = staff.set_worker_role(row.id, staff.KnownWorkerRole(role))


def close_design_launch(launch: DesignPaneLaunch) -> None:
    """Close the proved terminal from an interrupted launch."""
    client = _client()
    location = _design_launch_pane(client, launch)
    if isinstance(location, herdr.NoPane):
        return
    owner = _design_launch_process(client, location, launch)
    if isinstance(owner, herdr.PaneAtShell):
        closed = client.close_tab_confirmed(location.pane, terminal=location.terminal)
    else:
        process = client.pane_agent_process(location.pane, location.terminal)
        pane = client.pane_row(location.pane)
        if isinstance(pane, herdr.NoPane) or pane.get("terminal_id") != location.terminal:
            raise herdr_types.HerdrFailure("the design launch terminal changed")
        record = staff.authority_snapshot()
        if any(record.by_current_session(identity) is not None for identity in pane_launch.native_sessions(client, pane)):
            raise herdr_types.HerdrFailure("the design launch session still belongs to staff")
        close_pane = cast(Callable[..., bool], client.close_pane)
        closed = (not isinstance(process.start_ticks, herdr.ProcessGenerationUnavailable)
                  and process.pid == owner.pid and close_pane(location.pane, target=process))
        if closed:
            closed = all(row.get("pane_id") != location.pane and row.get("terminal_id") != location.terminal
                         for row in client.panes())
    if not closed:
        raise herdr_types.HerdrFailure("design pane close is incomplete")


def _parent_split(pane: str, ratio: float, label: str) -> herdr.SplitPane:
    return herdr.SplitPane(pane, "right", ratio, label)


def _destination(parent: staff.StaffRow, name: str, evidence: PaneEvidence,
                 client: herdr.Herdr, *, split_only: bool = False,
                 ) -> herdr.SplitPane | herdr.ExistingPane | StartRefused | UncertainWorker | ProcessUnknownWorker:
    parents = evidence.scan.found.get(parent.id, [])
    if len(parents) != 1:
        return _uncertain(parent, "parent pane is unavailable or duplicated")
    parent_pane = parents[0]
    tab = _text(parent_pane, "tab_id")
    label = agent_name(parent, name)
    pane = herdr.NoPane(name) if split_only else _interrupted_pane(parent, name, evidence, client)
    if isinstance(pane, (StartRefused, UncertainWorker, ProcessUnknownWorker)):
        return pane
    if not isinstance(pane, herdr.NoPane):
        identity, terminal = str(pane["pane_id"]), str(pane["terminal_id"])
        if not isinstance(client.pane_activity(identity), herdr.PaneAtShell):
            return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "interrupted recovery pane is live")
        return herdr.ExistingPane(identity, terminal)
    ratio = conf.read("direct").number("worker_split_ratio")
    workers = {item.pane for item in evidence.states if isinstance(item, (OpenWorker, StoppedWorker))
               and any(_text(pane, "pane_id") == item.pane and _text(pane, "tab_id") == tab for pane in evidence.panes)}
    workers.update(_text(pane, "pane_id") for pane in evidence.panes
                   if _text(pane, "terminal_id") in evidence.layout_terminals and _text(pane, "tab_id") == tab)
    if not workers:
        return _parent_split(_text(parent_pane, "pane_id"), ratio, label)
    layout = herdr.json_object(client.socket_request("layout.export", tab_id=tab).get("layout"), "worker layout")
    nodes = [layout.get("root")]
    ordered: list[str] = []
    parent_in_layout = False
    while nodes:
        node = herdr.json_object(nodes.pop(), "layout node")
        if node.get("type") == "pane":
            pane = _text(node, "pane_id")
            parent_in_layout = parent_in_layout or pane == _text(parent_pane, "pane_id")
            if pane in workers:
                ordered.append(pane)
        else:
            nodes.extend((node.get("second"), node.get("first")))
    if not ordered:
        if parent_in_layout:
            return _parent_split(_text(parent_pane, "pane_id"), ratio, label)
        return _uncertain(parent, "worker panes are absent from the split tree")
    return herdr.SplitPane(ordered[-1], "down", ratio, label)


def _worker_phase(state: dict[str, object], owner: staff.StaffRow, row: staff.StaffRow | None,
                  record: staff.StaffSnapshot) -> str:
    """Read the worker's phase from the pass, the only open phase, or the saved launch."""
    phases = recorder_state.open_phase_ids(state)
    if len(phases) == 1:
        return phases[0]
    if row is not None:
        passed = _pass_in(row, state, record)
        phase = pass_recorder.string((pass_recorder.object_dict(passed.get("phase")) or {}).get("id"))
        if phase in phases:
            return phase
        launch = (pane_launch.newest_publication(pane_launch.record_directory(), (row.id, row.current_session))
                  if phases else None)
        if launch is not None:
            saved = launch.entry["environment"].get(phase_reservation.SESSION_ENVIRONMENT)
            return next((phase for phase in phases
                         if phase_reservation.phase_session_id(owner.current_session, phase) == saved), "")
    return ""


@herdr.pane_evidence_run()
def start_or_resume(parent: StaffId, name: WorkerName, role: WorkerRole | WorkerRoleUnavailable, *,
                    account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection = ConfiguredWorkerAccount.CURRENT,
                    lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE,
                    policy: UseRolePolicy | LaunchPolicy = UseRolePolicy.CURRENT, runs: dict[str, list[str]] | None = None,
                    codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT,
                    selection: SelectedWorkerRows | Literal["by_name"] = "by_name",
                    phase: str | None = None,
                    design_claim: staff.DesignWorkerClaim | staff.NoDesignWorkerClaim = staff.NO_DESIGN_WORKER_CLAIM,
                    design_pane_launch: DesignPaneLaunch | NoDesignPaneLaunch = NoDesignPaneLaunch.UNRECORDED,
                    ) -> WorkerStart:
    if isinstance(role, WorkerRoleUnavailable):
        return StartRefused(StartRefusal.ROLE_UNAVAILABLE, role.reason)
    if role not in staff.WORKER_ROLES:
        return StartRefused(StartRefusal.UNKNOWN_ROLE, str(role))
    record = staff.snapshot()
    owner = record.by_id(parent)
    if owner is None or owner.kind not in ("unit", "showrunner"):
        return AdmissionRefused("only a unit or showrunner can admit a worker")
    try:
        paths = enrolled_worker_owner(owner)
        if isinstance(paths, EnrolledPaths) and (refusal := review_role_refusal(owner, role)):
            return refusal
    except (OSError, ValueError, ProductionPathsError, staff.StaffRefused, staff.StaffUnreadable) as error:
        return AdmissionRefused(str(error))
    expected = worker_admission.find_worker(record, parent, name.value)
    keep_model = isinstance(policy, UseRolePolicy)
    try:
        with ExitStack() as operations:
            held = operations.enter_context(worker_lock(parent, name, blocking=expected is None)) if isinstance(lock, WorkerLockAtUse) else lock
            if isinstance(held, Kept):
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, held.blocker)
            record = staff.snapshot()
            existing = worker_admission.find_worker(record, parent, name.value)
            if (isinstance(design_claim, staff.DesignWorkerClaim) and existing is not None
                    and existing.design_claim != design_claim):
                return AdmissionRefused("the worker belongs to another role")
            if (not held.active or held.parent != parent or held.name != name
                    or (existing is None and held.bound)
                    or (existing is not None and not held.owns(existing))
                    or (expected is not None and (existing is None or existing.id != expected.id
                                                  or existing.current_session != expected.current_session))):
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, "worker lock does not own this worker")
            if isinstance(policy, UseRolePolicy):
                saved = _checked_resume_policies.get({}).get(existing.current_session) if existing is not None else None
                checked = saved if saved is not None and saved.role == role else checked_role_policy(role)
                if isinstance(checked, StartRefused):
                    return checked
                policy = checked.launch
            run = worker_run(existing if existing is not None else owner, record=record, scanned=runs)
            if existing is not None and not isinstance(worker_dispatch_state(existing, run, held), DispatchHoldsWorker):
                run = worker_run(owner, record=record, scanned=runs)
            if isinstance(paths, EnrolledPaths):
                if refusal := review_run_refusal(owner, run, paths):
                    return refusal
                if policy.sandbox != READ_ONLY_SANDBOX or policy.permission != "plan":
                    return AdmissionRefused("showrunner review requires a read-only launch policy")
                policy = replace(policy, tools=ToolAccess.SHOWRUNNER_REVIEW)
            run_fields = unit_runs.read_run_fields(run)
            if unit_runs.terminal_run(run_fields):
                return AdmissionRefused(f"{run}: the run has ended dispatch admission")
            phase = _worker_phase(run_fields, owner, existing, record) if phase is None else phase
            berth_session = (phase_reservation.phase_session_id(owner.current_session, phase)
                             if phase and phase not in ("ad hoc", "adhoc", pass_recorder.AS_BUILT_PHASE) else "")
            allowed = worker_admission.WorkerStartAllowed(name.value, parent, run,
                worker_admission.ExistingWorkerSlot(existing) if existing is not None else worker_admission.NewWorkerSlot(),
                design_claim)
            target = SelectedWorkers(frozenset({name.value})) if selection == "by_name" else selection
            with worker_admission.worker_operation(allowed):
                result = _start_locked(owner, name, role, policy, account, allowed, held, codex, paths,
                                       keep_model=keep_model, selection=target, berth_session=berth_session,
                                       design_pane_launch=design_pane_launch)
                if isinstance(result, OpenWorker) and result.row.role != staff.KnownWorkerRole(role):
                    return replace(result, row=staff.set_worker_role(result.row.id, staff.KnownWorkerRole(role)))
                return result
    except (worker_admission.AdmissionRefused, staff.StaffRefused) as error:
        return AdmissionRefused(str(error))


def first_turn_session(client: herdr.Herdr, pane: str, terminal: str) -> str:
    """Read the session report, then wait for the first Codex turn to end."""
    settings = conf.read("direct")
    text = settings.text("worker_first_turn_text")
    seconds = settings.number("worker_first_turn_seconds")
    deadline = time.monotonic() + seconds
    client.prompt_terminal(pane, terminal, text)
    failure = "first turn timed out"
    while time.monotonic() < deadline:
        try:
            session = client.native_session(pane, "codex", herdr.PROGRAM_TIMEOUT_SECONDS)
        except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            failure = str(error)
            _ = Event().wait(max(0, min(herdr.PANE_POLL_SECONDS, deadline - time.monotonic())))
            continue
        remaining_ms = int(max(0, deadline - time.monotonic()) * 1000)
        if isinstance(client.wait_turn_end(pane, remaining_ms), herdr.WaitTimedOut):
            raise herdr_types.HerdrFailure("first turn timed out")
        return session
    raise herdr_types.HerdrFailure(failure)


def _codex_start_session(client: herdr.Herdr, pane: str, *,
                         pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> str:
    row = client.pane_row(pane) if isinstance(pane_row, herdr.PaneNotRead) else pane_row
    if isinstance(row, herdr.NoPane):
        raise herdr_types.HerdrFailure("Codex pane is missing")
    if row.get("session_id"):
        return client.native_session(pane, "codex", herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=row)
    return first_turn_session(client, pane, str(row.get("terminal_id", "")))



def unpublished_cleanup_text(result: UnpublishedWorker) -> str:
    return result.cleanup.value.format(pane=result.pane)


def _start_locked(owner: staff.StaffRow, name: WorkerName, role: WorkerRole, policy: LaunchPolicy,
                  account: ConfiguredWorkerAccount | DestinationWorkerAccount | accounts.AccountSelection,
                  allowed: worker_admission.WorkerStartAllowed, lock: WorkerLockHold,
                  codex: herdr.CodexConversations | herdr.CodexLaunchRead,
                  paths: EnrolledPaths | OrdinaryWorkerOwner, *, keep_model: bool,
                  selection: SelectedWorkers | SelectedWorkerRows, berth_session: str,
                  design_pane_launch: DesignPaneLaunch | NoDesignPaneLaunch) -> WorkerStart:
    started = time.monotonic()
    try:
        settings = conf.read("direct")
        start_seconds = settings.number("pane_agent_start_seconds")
        publish_lock_seconds = settings.number("worker_publish_lock_seconds")
    except conf.Unusable as error:
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(error))
    deadline = started + start_seconds
    client = _client()
    record = staff.snapshot()
    refreshed = worker_admission.find_worker(record, owner.id, name.value)
    if isinstance(allowed.slot, worker_admission.ExistingWorkerSlot):
        expected = allowed.slot.row
        if (refreshed is None or refreshed.id != expected.id or not lock.owns(refreshed)
                or refreshed.current_session != expected.current_session):
            return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, "the worker changed before startup")
    elif refreshed is not None:
        return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, "the worker changed before startup")

    retry_pane: herdr.ExistingPane | herdr.NoPane = herdr.NoPane(name.value)
    if isinstance(design_pane_launch, RetryDesignPaneLaunch):
        try:
            retry_pane = _design_launch_pane(client, design_pane_launch)
            if isinstance(retry_pane, herdr.NoPane):
                return AdmissionRefused("the original design pane is missing")
            if not isinstance(_design_launch_process(client, retry_pane, design_pane_launch), herdr.PaneAtShell):
                return AdmissionRefused("the design pane already has an agent; run recover")
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            return AdmissionRefused(str(error))

    def read_evidence(membership: staff.StaffSnapshot) -> PaneEvidence:
        herdr.refresh_restore_pane_evidence()
        return _evidence(owner.id, membership, client, selection=selection)

    evidence = read_evidence(record)
    current = next((state for state in evidence.states if state.row.name == name.value), None)
    row = current.row if current is not None else None
    backend = staff.backend(row) if row is not None else staff.BACKEND_UNAVAILABLE
    switching = not isinstance(backend, staff.BackendUnavailable) and backend.family != policy.settings.kind
    if row is not None and not lock.owns(row):
        return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, "the worker changed before startup")
    if isinstance(current, (ProcessUnknownWorker, UncertainWorker)):
        return current
    if isinstance(paths, EnrolledPaths):
        try:
            if isinstance(current, OpenWorker):
                require_review_process(client, current.pane, paths)
            elif row is not None and isinstance(backend, staff.CodexBackend):
                retained = pane_record.publication_for_session(row.current_session, record=record)
                if retained is not None and not _codex_read_only(retained.entry.get("codex_policy", [])):
                    return AdmissionRefused("the retained Codex worker launch does not prove read-only access")
        except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            return AdmissionRefused(str(error))
    if current is None and isinstance(design_pane_launch, NoDesignPaneLaunch):
        pane = _interrupted_pane(owner, name.value, evidence, client)
        if isinstance(pane, (StartRefused, UncertainWorker, ProcessUnknownWorker)):
            return pane
        if not isinstance(pane, herdr.NoPane):
            pane = dict(pane)
            identity = _text(pane, "pane_id")
            unknown = next((item for item in evidence.scan.unknown if item.pane == identity), None)
            try:
                activity = client.pane_activity(identity)
                if unknown is not None:
                    info = herdr.json_object(client.result("pane", "process-info", "--pane", identity).get("process_info"),
                                             "recovery process info")
                    if not (policy.settings.kind == "codex" and herdr.codex_without_session(info)):
                        return ProcessUnknownWorker(owner, identity, _text(pane, "terminal_id"), unknown.reason)
            except (worker_admission.AdmissionRefused, herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                return ProcessUnknownWorker(owner, identity, _text(pane, "terminal_id"), str(error))
            if not isinstance(activity, herdr.PaneAtShell):
                if isinstance(paths, EnrolledPaths):
                    try:
                        require_review_process(client, identity, paths)
                    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                        return AdmissionRefused(str(error))
                try:
                    native = (_codex_start_session(client, identity, pane_row=pane) if policy.settings.kind == "codex" else
                              client.native_session(identity, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane))
                    if policy.settings.kind == "codex":
                        entry = client.codex_launch(identity)
                        entry["name"] = name.value
                        pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", entry)
                        row = worker_admission.publish(allowed, native,
                            launch_home=pane_launch.config_home(entry, record=record)).row
                        lock.bind(row, deadline=time.monotonic() + publish_lock_seconds)
                        pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", entry)
                    else:
                        row = worker_admission.publish(allowed, native).row
                        lock.bind(row, deadline=time.monotonic() + publish_lock_seconds)
                except (worker_admission.AdmissionRefused, herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    if row is not None:
                        return _uncertain(row, f"launch proof is incomplete: {error}")
                    return UnpublishedWorker(identity, str(error))
                record = staff.snapshot()
                recovered = read_evidence(record)
                state = next((item for item in recovered.states if item.row.id == row.id), None)
                if isinstance(state, OpenWorker) and berth_session:
                    current, evidence = state, recovered
                elif isinstance(state, (OpenWorker, ProcessUnknownWorker, UncertainWorker)):
                    _layout(owner.id, recovered)
                    return state
                else:
                    return _uncertain(row, "retained launch proof is incomplete")
    if isinstance(current, OpenWorker):
        passed = worker_pass(current.row, allowed.run)
        dispatch = worker_dispatch_state(current.row, worker_run(current.row, record=record), lock)
        if passed.get("kind") != role and isinstance(dispatch, DispatchHoldsWorker):
            return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, dispatch.blocker)
        if not switching:
            matches = _live_policy(current, policy, client, same_role=keep_model and passed.get("kind") == role)
            if isinstance(matches, UncertainWorker):
                return matches
            same_run = True
            same_account = True
            same_berth = True
            check_run = owner.kind == "unit" and not passed
            if matches:
                try:
                    pane_row = next(pane for pane in evidence.panes if pane.get("pane_id") == current.pane)
                    launched = client.native_pane_process(current.pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=pane_row)
                except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                    if berth_session or passed.get("status") != "active":
                        return _uncertain(current.row, f"running launch environment is unproved: {error}")
                else:
                    same_berth = launched.environment.get(phase_reservation.SESSION_ENVIRONMENT, "") == berth_session
                    if check_run:
                        same_run = launched.environment.get("UNIT_RUN_DIR") == str(allowed.run)
                    expected = launch_environment(policy, account, True)
                    if isinstance(expected, StartRefused):
                        return expected
                    from scripts.accounts import accounts

                    key = "CODEX_HOME" if policy.settings.kind == "codex" else "CLAUDE_CONFIG_DIR"
                    default_home = str(Path.home() / ".codex" if policy.settings.kind == "codex" else accounts.main_config_dir())
                    running_home = Path(launched.environment.get(key, default_home)).expanduser().resolve()
                    selected_home = Path(expected.get(key, default_home)).expanduser().resolve()
                    same_account = running_home == selected_home
            if matches and same_run and same_account and same_berth:
                _layout(owner.id, evidence)
                return current
            closable = can_close(current, lock=lock)
            if isinstance(closable, Kept):
                return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, closable.blocker)
    if switching and current is not None:
        closable = can_close(current, lock=lock)
        if isinstance(closable, Kept):
            return StartRefused(StartRefusal.BUSY_ROLE_CHANGE, closable.blocker)
    environment = launch_environment(policy, account, row is not None and not switching)
    if isinstance(environment, StartRefused):
        return environment
    launch_home = (staff.SessionHome(Path(environment["CODEX_HOME"]))
                   if policy.settings.kind == "codex" else staff.HomeUnknown())
    environment["UNIT_TEAM_ROLE"] = name.value
    unset_environment = frozenset({"STAFF_ID"})
    if berth_session:
        environment[phase_reservation.SESSION_ENVIRONMENT] = berth_session
    else:
        unset_environment |= {phase_reservation.SESSION_ENVIRONMENT}
    if owner.kind == "unit" or isinstance(paths, EnrolledPaths):
        environment["UNIT_RUN_DIR"] = str(allowed.run)
    if temporary := os.environ.get("TMPDIR"):
        environment["TMPDIR"] = temporary
    if isinstance(paths, EnrolledPaths):
        directory = paths.main
    elif claimed := os.environ.get(CLAIMED_WORKING_DIR_ENV):
        directory = Path(claimed).resolve()
    else:
        directory = owner.worktree.path if isinstance(owner.worktree, staff.PresentWorktree) else Path.cwd()
    prepared = herdr.prepare_session_environment(str(row.current_session) if row is not None and not switching else "", str(directory), environment,
                                               unset_environment)
    if isinstance(prepared, nix_env.Refused):
        return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(prepared))
    if isinstance(design_pane_launch, DesignPaneLaunch):
        prepared = replace(prepared, env={**prepared.env, DESIGN_PANE_LAUNCH_ENV: str(design_pane_launch.token)},
                           unset_environment=prepared.unset_environment - {DESIGN_PANE_LAUNCH_ENV})
    if isinstance(current, OpenWorker) and not switching:
        herdr.forget_pane_evidence(client.session, current.pane)
        if not _stop_row(current.row, client, deadline=deadline):
            return _uncertain(current.row, "agent stop is incomplete; pane retained")
        current = StoppedWorker(current.row, current.pane, current.terminal)
    if switching:
        destination = DestinationAfterSourceRelease()
    elif isinstance(current, StoppedWorker):
        _retire_launch(current.row, client, record)
        destination = herdr.ExistingPane(current.pane, current.terminal)
    elif isinstance(retry_pane, herdr.ExistingPane):
        destination = retry_pane
    else:
        try:
            destination = _destination(owner, name.value, evidence, client,
                                       split_only=isinstance(design_pane_launch, DesignPaneLaunch))
        except conf.Unusable as error:
            return StartRefused(StartRefusal.INVALID_ROLE_CONFIGURATION, str(error))
        if isinstance(destination, (StartRefused, UncertainWorker, ProcessUnknownWorker)):
            return destination
    if isinstance(destination, herdr.ExistingPane):
        panes = client.panes()
        if sum(pane.get("terminal_id") == destination.terminal for pane in panes) != 1:
            return _uncertain(row or owner, "terminal moved or duplicated before start")
        if not any(pane["pane_id"] == destination.pane and pane.get("terminal_id") == destination.terminal for pane in panes):
            return _uncertain(row or owner, "terminal moved before start")
        if not isinstance(current, StoppedWorker) and not isinstance(client.pane_activity(destination.pane), herdr.PaneAtShell):
            return StartRefused(StartRefusal.PANE_NOT_AT_SHELL, "recovery pane has foreground work")
    launches: list[herdr.AgentLaunched] = []
    launch_terminals: dict[str, str] = {}
    launch_processes: dict[str, herdr.AgentProcessIdentity] = {}
    launch_destinations: dict[str, herdr.SplitPane | herdr.ExistingPane] = {}
    first_turn_launches: dict[str, pane_launch.PaneLaunch] = {}

    def start(handoff: worker_admission.FamilyHandoff) -> str:
        del handoff
        launch_destination = destination
        launch_evidence = evidence
        if isinstance(launch_destination, DestinationAfterSourceRelease):
            if isinstance(current, (OpenWorker, StoppedWorker)):
                if not client.close_tab_confirmed(current.pane, target=str(current.row.current_session), terminal=current.terminal):
                    raise herdr_types.HerdrFailure("source pane close is incomplete")
                _retire_launch(current.row, client, record)
            fresh = read_evidence(record)
            selected = _destination(owner, name.value, fresh, client)
            if isinstance(selected, StartRefused):
                raise worker_admission.AdmissionRefused(selected.detail)
            if isinstance(selected, (UncertainWorker, ProcessUnknownWorker)):
                raise worker_admission.AdmissionRefused(selected.reason)
            launch_destination = selected
            launch_evidence = fresh
        identity = str(row.current_session) if row is not None and not switching else ""
        launch = partial(herdr.launch_prepared_agent, codex=codex) if isinstance(codex, herdr.CodexConversations) else herdr.launch_prepared_agent
        resume_policy = policy
        if (identity and row is not None and keep_model and policy.settings.kind == "claude"
                and row.role == staff.KnownWorkerRole(role)):
            homes = [row.home.path] if isinstance(row.home, staff.SessionHome) else []
            if target_home := environment.get("CLAUDE_CONFIG_DIR"):
                homes.append(Path(target_home))
            turn = pane_record.claude_turn_settings(identity, *homes)
            resume_policy = replace(policy, settings=_claude_worker_settings(policy.settings, turn))
        arguments = launch_arguments(resume_policy, identity, directory)
        def launch_at(selected: herdr.SplitPane | herdr.ExistingPane) -> herdr.AgentLaunched:
            return launch(client.session, selected, prepared, policy.settings.kind,
                arguments, herdr.NoOpeningPrompt(),
                int(start_seconds * 1000), agent=name.value, agent_name=agent_name(owner, name.value))

        try:
            launched = launch_at(launch_destination)
        except herdr_types.HerdrFailure as error:
            parents = launch_evidence.scan.found.get(owner.id, [])
            if (error.code != "pane_not_found" or not isinstance(launch_destination, herdr.SplitPane)
                    or len(parents) != 1 or launch_destination.of_pane == _text(parents[0], "pane_id")):
                raise
            launch_destination = _parent_split(_text(parents[0], "pane_id"), launch_destination.ratio,
                                               launch_destination.label)
            launched = launch_at(launch_destination)
        launches.append(launched)
        launch_destinations[launched.pane] = launch_destination
        pane = client.pane_row(launched.pane)
        if isinstance(pane, herdr.NoPane) or not (terminal := pane.get("terminal_id", "")):
            raise herdr_types.HerdrFailure("launched terminal is missing")
        if isinstance(launch_destination, herdr.ExistingPane) and terminal != launch_destination.terminal:
            raise herdr_types.HerdrFailure("launched terminal changed")
        launch_terminals[launched.pane] = terminal
        process = client.pane_agent_process(launched.pane, terminal)
        if isinstance(process.start_ticks, herdr.ProcessGenerationUnavailable):
            raise herdr_types.HerdrFailure(process.start_ticks.reason)
        launch_processes[launched.pane] = process
        if isinstance(paths, EnrolledPaths):
            require_review_process(client, launched.pane, paths)
        if not identity and policy.settings.kind == "codex":
            if isinstance(launched.outcome, herdr.PromptNotSubmitted):
                raise herdr_types.HerdrFailure(launched.outcome.reason)
            native = _codex_start_session(client, launched.pane)
            entry = pane_launch.launch(launched.pane, native, str(prepared.directory),
                prepared.env, ["codex", *arguments], kind="codex")
            entry["name"] = name.value
            pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", entry)
            first_turn_launches[launched.pane] = entry
            return native
        return client.native_session(launched.pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS)

    def created_pane() -> CreatedWorkerPane | NoCreatedWorkerPane:
        launched = launches[-1]
        if isinstance(launch_destinations[launched.pane], herdr.ExistingPane):
            return NO_CREATED_WORKER_PANE
        return CreatedWorkerPane(launch_terminals.get(launched.pane, ""))

    def cleanup(identity: str) -> worker_admission.CleanupOutcome:
        if not launches:
            return worker_admission.NoDestinationToStop()
        pane = launches[-1].pane
        with redirect_stderr(io.StringIO()) as errors:
            try:
                terminal = launch_terminals.get(pane, "")
                observed = client.pane_row(pane)
                if not terminal or isinstance(observed, herdr.NoPane) or not observed.get("terminal_id"):
                    raise herdr_types.HerdrFailure("launched terminal is missing")
                if observed.get("terminal_id") != terminal:
                    raise herdr_types.HerdrFailure("launched terminal changed")
                process = launch_processes.get(pane)
                if process is None:
                    raise herdr_types.HerdrFailure("launched process identity is unavailable")
                proved = identity
                if switching or isinstance(launch_destinations[pane], herdr.ExistingPane):
                    proved = client.native_session(pane, policy.settings.kind, herdr.PROGRAM_TIMEOUT_SECONDS, pane_row=observed)
                if client.pane_agent_process(pane, terminal) != process:
                    raise herdr_types.HerdrFailure("launched agent process changed")
                if switching or isinstance(launch_destinations[pane], herdr.ExistingPane):
                    if identity and proved != identity:
                        raise herdr_types.HerdrFailure("launched agent identity changed")
                    client.quit_agent(pane, terminal, proved, start_seconds)
                    if switching and not isinstance(launch_destinations[pane], herdr.ExistingPane):
                        if not client.close_terminal_pane(pane, terminal):
                            raise herdr_types.HerdrFailure("pane close was not confirmed")
                else:
                    close_pane = cast(Callable[..., bool], client.close_pane)
                    if not close_pane(pane, target=process):
                        raise herdr_types.HerdrFailure("pane close was not confirmed")
            except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
                detail = " ".join((errors.getvalue() or str(error)).split())
                result = worker_admission.DestinationRetained(pane, detail)
            else:
                return worker_admission.DestinationStopped()
        print(f"anomaly: {pane}: cleanup failed: {result.reason}", file=sys.stderr)
        return result

    identity = ""
    switched = False
    try:
        if switching:
            request = hashlib.sha256(f"{row.current_session if row else ''}:{policy.settings.kind}".encode()).hexdigest()
            published = worker_admission.switch_worker(allowed, policy.settings.kind, start=start,
                dispatch=worker_admission.DispatchOwner.TURN_SUBMISSION, cleanup=cleanup, request=request,
                launch_home=launch_home)
            row = published.row
            switched = True
        else:
            identity = start(worker_admission.NoFamilyHandoff())
            if row is not None and identity != row.current_session:
                return _uncertain(row, "launch proves another native session; pane retained")
            if row is None:
                row = worker_admission.publish(allowed, identity, launch_home=launch_home).row
                lock.bind(row, deadline=time.monotonic() + publish_lock_seconds)
        launched = launches[-1]
        if launched.pane in first_turn_launches:
            pane_launch.write(pane_launch.record_directory() / f"{client.session}.json", first_turn_launches[launched.pane])
    except (worker_admission.AdmissionRefused, herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        if not launches:
            return _uncertain(row or owner, f"agent launch is incomplete; pane retained: {error}")
        if row is not None:
            if (not switching or switched) and not isinstance(launch_destinations[launches[-1].pane], herdr.ExistingPane):
                _ = cleanup(identity)
            return _uncertain(row, f"launch proof is incomplete: {error}")
        launched = launches[-1]
        admitted = staff.initial_snapshot().by_current_session(identity) if identity else None
        if admitted is not None:
            return IncompleteStart(admitted, launched.pane, str(error), created_pane())
        outcome = cleanup(identity)
        existing_pane = isinstance(launch_destinations.get(launched.pane), herdr.ExistingPane)
        stopped = isinstance(outcome, worker_admission.DestinationStopped)
        status = ((UnpublishedCleanup.AGENT_STOPPED if stopped else UnpublishedCleanup.AGENT_STOP_FAILED)
                  if existing_pane else (UnpublishedCleanup.PANE_CLOSED if stopped else UnpublishedCleanup.PANE_CLOSE_FAILED))
        return UnpublishedWorker(launched.pane, str(error), status)
    launched = launches[-1]
    if isinstance(launched.outcome, herdr.PromptNotSubmitted):
        return IncompleteStart(row, launched.pane, launched.outcome.reason, created_pane())
    herdr.forget_pane_evidence(client.session, launched.pane)
    fresh = read_evidence(staff.snapshot())
    result = next((state for state in fresh.states if state.row.id == row.id), None)
    _layout(owner.id, fresh)
    if isinstance(result, (OpenWorker, ProcessUnknownWorker)):
        return replace(result, created_pane=created_pane())
    if isinstance(result, UncertainWorker):
        return result
    state_kind = type(result).__name__ if result is not None else "missing"
    scan_reason = next((item.reason for item in fresh.scan.unknown if item.pane == launched.pane), "none")
    tab = next((_text(pane, "tab_id") for pane in fresh.panes if _text(pane, "pane_id") == launched.pane), "")
    parent_tab = "yes" if tab and tab in fresh.parent_tabs else "no"
    return _uncertain(row,
        f"agent launch is incomplete; state={state_kind}; scan={scan_reason}; parent_tab={parent_tab}")


def _retire_launch(row: staff.StaffRow, client: herdr.Herdr, record: staff.StaffSnapshot) -> None:
    path = pane_launch.record_directory() / f"{client.session}.json"
    for pane, entry in pane_launch.read_valid_entries(path).items():
        owner = record.by_member_reference(pane_launch.reference_id(entry))
        if owner is not None and owner.id == row.id and pane_launch.agent_kind(entry) == "codex":
            pane_launch.retire_launch(path, pane, entry)


def _parked_launch(worker: OpenWorker, client: herdr.Herdr) -> progress_history.ParkedWorkerLaunch:
    from scripts.direct import progress_history

    herdr.forget_pane_evidence(client.session, worker.pane)
    info = herdr.json_object(client.result("pane", "process-info", "--pane", worker.pane).get("process_info"),
                             "worker launch policy")
    owner = herdr.foreground_pane_owner(info)
    pane = client.pane_row(worker.pane)
    if (not isinstance(owner, (herdr.ProvedCodexForeground, herdr.ProvedClaudeForeground))
            or isinstance(pane, herdr.NoPane) or pane.get("terminal_id") != worker.terminal
            or herdr.read_foreground_pane_owner(owner, pane).current_identity != worker.row.current_session):
        raise ValueError("the worker launch does not match the pane")
    settings = kept_settings(models_registry.LaunchSettings(owner.kind, "", "", ""), owner.arguments)
    pairs = dict(zip(owner.arguments, owner.arguments[1:]))
    sandbox, permission = pairs.get("--sandbox", ""), pairs.get("--permission-mode", "")
    if (not settings.model or owner.kind == "codex" and
            (not sandbox or pairs.get("--ask-for-approval") != "never") or owner.kind == "claude" and not permission):
        raise ValueError("the worker launch settings are incomplete")
    return progress_history.ParkedWorkerLaunch(str(worker.row.current_session), owner.kind, settings.model,
        settings.effort, settings.service_tier, sandbox, permission)


@herdr.pane_evidence_run()
def close(parent: StaffId, name: WorkerName, *,
          lock: WorkerLockHold | WorkerLockAtUse = WorkerLockAtUse.ACQUIRE,
          park: ParkWaitingPass | CloseEndedPane = CloseEndedPane.CHECK) -> Closed | Kept | UncertainWorker:
    with ExitStack() as operations:
        held = operations.enter_context(worker_lock(parent, name, blocking=True)) if isinstance(lock, WorkerLockAtUse) else lock
        if isinstance(held, Kept):
            return held
        client = _client()
        record = staff.snapshot()
        row = worker_admission.find_worker(record, parent, name.value)
        if (not held.active or held.parent != parent or held.name != name
                or (row is None and held.bound) or (row is not None and not held.owns(row))):
            return Kept("the worker changed before close")
        if row is None:
            _layout(parent, _evidence(parent, record, client))
            return Closed()
        with staff.row_operation(str(row.id)):
            read = _read_panes(record, client)
            if isinstance(read, PaneReadUnavailable):
                return _uncertain(row, read.reason)
            evidence = _parent_evidence(parent, record, read, client,
                                        selection=SelectedWorkers(frozenset({name.value})))
            observed = next((item for item in evidence.states if item.row.id == row.id), ClosedWorker(row))
            if isinstance(observed, UncertainWorker):
                return observed
            if isinstance(observed, ProcessUnknownWorker):
                return _uncertain(row, observed.reason)
            if isinstance(observed, (OpenWorker, StoppedWorker)):
                permission = (can_close(observed, lock=held, park=park) if isinstance(park, ParkWaitingPass)
                              else can_close(observed, lock=held))
                if isinstance(permission, Kept):
                    return permission
                if isinstance(park, ParkWaitingPass):
                    from scripts.direct import progress_history

                    assert isinstance(observed, OpenWorker)
                    try:
                        launch = _parked_launch(observed, client)
                    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                        return _uncertain(row, str(error))
                    if not progress_history.record_worker_park(park.run, row.id, park.instance, park.stage, launch=launch):
                        return Kept("the worker pass changed before parking")
                if not client.close_terminal_pane(observed.pane, observed.terminal):
                    return _uncertain(row, "pane close is incomplete")
            _retire_launch(row, client, record)
            _layout(parent, evidence)
            return Closed()


def close_canceled_worker(run: Path, identity: StaffId, instance: str) -> None:
    """Close the idle pane only if the canceled pass is still current."""
    try:
        record = staff.snapshot()
        row = record.by_id(identity)
        if row is None or row.kind != "worker":
            return
        parent = record.parent_of(row)
        if parent is None:
            return
        name = WorkerName.parse(row.name)
        with worker_lock(parent.id, name, blocking=True) as hold:
            if isinstance(hold, Kept) or not hold.owns(row) or worker_run(row) != run:
                return
            current = worker_pass(row, run)
            if current.get("instance_id") != instance or current.get("status") != "canceled":
                return
            with redirect_stderr(io.StringIO()) as diagnostics:
                closed = close(parent.id, name, lock=hold)
            if isinstance(closed, Closed):
                _ = sys.stderr.write(diagnostics.getvalue())
                return
            reason = closed.blocker if isinstance(closed, Kept) else closed.reason
            print(f"anomaly: {row.name}: canceled pane kept: {' '.join(reason.splitlines())}", file=sys.stderr)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
            staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"anomaly: {identity}: canceled pane close failed: {' '.join(str(error).splitlines())}",
              file=sys.stderr)


def forget_restart(row: staff.StaffRow) -> None:
    """Remove a suspended worker from the restart record: a restart never starts it."""
    try:
        shutdown_record.forget([str(row.id)])
    except (OSError, ValueError) as error:
        print(f"{row.name}: restart record anomaly: {' '.join(str(error).split())}", file=sys.stderr)


def _layout(parent: StaffId, evidence: PaneEvidence) -> None:
    client = _client()
    parent_tabs = {_text(pane, "tab_id") for pane in evidence.scan.found.get(parent, [])} - {""}
    selected = {item.terminal for item in evidence.states if isinstance(item, (OpenWorker, StoppedWorker))
                and item.row.reports_to(parent) and any(_text(pane, "pane_id") == item.pane
                and _text(pane, "tab_id") in parent_tabs for pane in evidence.panes)}
    selected.update(evidence.layout_terminals)
    panes = client.panes()
    tabs = {pane.get("tab_id", "") for pane in panes if pane.get("terminal_id", "") in selected}
    for tab in tabs - {""}:
        key = hashlib.sha256(f"layout:{client.session}:{tab}".encode()).hexdigest()
        with staff.slot_operation(key, "worker layout"):
            layout = herdr.json_object(client.socket_request("layout.export", tab_id=tab).get("layout"), "worker layout")
            terminals = {pane["pane_id"] for pane in panes
                         if pane.get("terminal_id", "") in selected and pane.get("tab_id", "") == tab}
            def balance(value: object, path: list[bool]) -> set[str]:
                node = herdr.json_object(value, "layout node")
                if node.get("type") == "pane":
                    return {_text(node, "pane_id")}
                first = balance(node.get("first"), [*path, False])
                second = balance(node.get("second"), [*path, True])
                leaves = first | second
                if node.get("direction") == "down" and leaves <= terminals and first and second:
                    ratio = len(first) / len(leaves)
                    if node.get("ratio") != ratio:
                        _ = client.socket_request("layout.set_split_ratio", pane_id=next(iter(terminals)), path=path, ratio=ratio)
                return leaves
            _ = balance(layout.get("root"), [])


def _result(result: WorkerStart | Closed | Kept) -> ExitCode:
    if isinstance(result, StartRefused):
        print(f"{result.reason.value}: {result.detail}", file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, AdmissionRefused):
        print(result.reason, file=sys.stderr)
        return ExitCode.REFUSED
    if isinstance(result, Kept):
        print(result.blocker, file=sys.stderr)
        return ExitCode.KEPT
    if isinstance(result, (UncertainWorker, ProcessUnknownWorker, IncompleteStart, UnpublishedWorker)):
        if isinstance(result, UnpublishedWorker):
            print(f"{result.cause}; {unpublished_cleanup_text(result)}", file=sys.stderr)
        elif isinstance(result, IncompleteStart):
            print(f"{result.row.name}: {result.reason}; retain the pane and retry", file=sys.stderr)
        return ExitCode.UNCERTAIN
    print("done")
    return ExitCode.OK


@final
class Arguments(argparse.Namespace):
    command: str = ""
    name: str = ""
    role: str = ""
    account: str = ""
    json: bool = False


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start-or-resume")
    _ = start.add_argument("name")
    _ = start.add_argument("--role", required=True)
    _ = start.add_argument("--account", default="")
    end = commands.add_parser("close")
    _ = end.add_argument("name")
    listing = commands.add_parser("list")
    _ = listing.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments, namespace=Arguments())
    try:
        record = staff.snapshot()
        owner = record.by_current_session(staff.runtime_id())
        if owner is None or owner.kind not in ("unit", "showrunner"):
            return _result(AdmissionRefused("caller is not a unit or showrunner"))
        if args.command == "list":
            states = observe(owner.id, record)
            fields = [{"name": item.row.name, "id": item.row.id, "state": type(item).__name__,
                       **({"pane": item.pane, "terminal": item.terminal} if isinstance(item, (OpenWorker, StoppedWorker, ProcessUnknownWorker)) else {})}
                      for item in states]
            if args.json:
                print(json.dumps(fields))
            else:
                for item in fields:
                    print(f"{item['name']} {item['state']}")
            return ExitCode.OK
        name = WorkerName.parse(args.name)
        if args.command == "close":
            if worker_admission.find_worker(record, owner.id, name.value) is None:
                return _result(AdmissionRefused(f"no worker named {name.value}"))
            return _result(close(owner.id, name))
        if quota_state.no_worker_mode():
            print(f"worker_panes.py: {quota_state.NO_WORKER_START}", file=sys.stderr)
            return ExitCode.REFUSED
        account = DestinationWorkerAccount(args.account) if args.account else ConfiguredWorkerAccount.CURRENT
        return _result(start_or_resume(owner.id, name, cast(WorkerRole, args.role), account=account))
    except recorder_state.OldRecorderState as error:
        print(error, file=sys.stderr)
        return ExitCode.UNCERTAIN
    except (staff.StaffUnreadable, staff.StaffRefused, OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"anomaly: {error}; correct the evidence and retry", file=sys.stderr)
        return ExitCode.UNCERTAIN


if __name__ == "__main__":
    raise SystemExit(main())
