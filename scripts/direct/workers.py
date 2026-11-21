"""Read worker titles, director identity, and detached command guards."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
except FileNotFoundError:
    pass

import argparse
import sys

from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Never, Protocol, cast

from scripts.agents import detached_run_process, worker_lifecycle
from scripts.direct import recorder_state
from scripts.production import staff, worker_admission, session_staff
from scripts.lib import unit_runs

if TYPE_CHECKING:
    from scripts.message.sessions import RegistryRead
    from scripts.lib.names import JobKey

STATE = "progress_history_state.json"
_scratch_cleanup_owner: ContextVar[unit_runs.PreparedRunEnded | unit_runs.CleanupOwnerFromState] = ContextVar(
    "scratch_cleanup_owner", default=unit_runs.CleanupOwnerFromState.RECORDED)


class WorkerUnavailable(Exception):
    """No safe ownership transition can be made from the current evidence."""


class TitleRenderer(Protocol):
    def unit_worker_title(self, production: str, unit: str, job: JobKey, ordinal: int, address: str) -> str: ...

    def worker_title(self, session_dir: str, name: str, record: Mapping[str, object]) -> str: ...


@dataclass(frozen=True)
class LiveUnitDirector:
    session_id: str
    pid: int


@dataclass(frozen=True)
class NoLiveUnitDirector:
    """The evidence proves there is no live director."""


@dataclass(frozen=True)
class UnknownUnitDirector:
    """The evidence cannot establish whether the director is live."""

    reason: str

    def __bool__(self) -> Never:
        raise WorkerUnavailable(self.reason)


type UnitDirector = LiveUnitDirector | NoLiveUnitDirector | UnknownUnitDirector


def resolve_live_unit_director(
    run: Path, record: staff.StaffSnapshot, registry: RegistryRead, dependencies: Callable[[Path], None],
) -> UnitDirector:
    """Check the recorded director's current conversation."""
    from scripts.production import unit_lookup

    dependencies(run / STATE)
    context = worker_lifecycle.read_json_object(run / STATE)
    owner = unit_runs.run_owner(context, record)
    if not isinstance(owner, staff.StaffRow) or owner.kind not in ("unit", "showrunner"):
        return NoLiveUnitDirector()
    match unit_lookup.checked_conversation(owner.current_session, record=record, registry=registry):
        case unit_lookup.ProvedLiveUnit(
            owner=unit_lookup.LabeledSession(claude=unit_lookup.LiveClaude() as claude)
        ) | unit_lookup.ProvedLiveUnit(owner=unit_lookup.LiveClaude() as claude):
            return LiveUnitDirector(claude.session_id, claude.pid)
        case unit_lookup.ProvedLiveUnit(owner=unit_lookup.LabeledSession(
                identity=unit_lookup.LiveSession(id=identity),
                process=session_staff.ProvedCodexPaneProcess(pid=pid))):
            return LiveUnitDirector(identity, pid)
        case unit_lookup.ProvedLiveUnit():
            return UnknownUnitDirector("the checked director has no native process identity")
        case unit_lookup.UnknownUnit(reason=reason):
            return UnknownUnitDirector(reason)
        case unit_lookup.ProvedAbsentUnit():
            return NoLiveUnitDirector()


def live_unit_director(run: Path) -> UnitDirector:
    """Resolve the director across restarts with one owner snapshot."""
    from scripts.message import sessions

    try:
        dependencies = staff.RecordInputs()
        record = staff.initial_snapshot()
        registry = sessions.read_registry(dependencies=dependencies)
        return resolve_live_unit_director(run, record, registry, dependencies)
    except (OSError, ValueError) as error:
        return UnknownUnitDirector(str(error))


def run_has_live_director(run: Path) -> bool | UnknownUnitDirector:
    director = live_unit_director(run)
    return director if isinstance(director, UnknownUnitDirector) else isinstance(director, LiveUnitDirector)


def live_detached_runs(run: Path) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Recorded supervisors only; no process discovery by name."""
    records: list[tuple[Path, dict[str, object]]] = []
    for path in sorted((run / "detached_runs").glob("*.json")):
        record = worker_lifecycle.read_json_object(path)
        pid = record.get("pid")
        if record.get("status") == "running" and isinstance(pid, int) and detached_run_process.supervises(pid, path):
            records.append((path, record))
    return tuple(records)


def protected_working_directories(run: Path) -> tuple[Path, ...]:
    from scripts.herdr import worker_panes

    membership = staff.local_snapshot()
    return tuple(worker_panes.worker_directory(membership, staff.StaffId(str(record["staff_id"]))).resolve()
                 for _, record in retained_detached_runs(run, prepared=_scratch_cleanup_owner.get()))


def retained_detached_runs(
    run: Path, *,
    prepared: unit_runs.PreparedRunEnded | unit_runs.CleanupOwnerFromState = unit_runs.CleanupOwnerFromState.RECORDED,
) -> tuple[tuple[Path, dict[str, object]], ...]:
    """Commands owned by unit workers, including workers about to be paused."""
    membership = staff.local_snapshot()
    owner = worker_admission.cleanup_parent(membership, run, prepared)
    members = membership.find(kind="worker", parent=owner.id)
    references = {str(row.id) for row in members}
    histories = {run, *(path for row in members for path in session_staff.worker_run_index(membership).get(row.id, ()))}
    return tuple((path, record) for history in sorted(histories)
                 for path, record in live_detached_runs(history)
                 if record.get("staff_id") in references)


def finish_prepared_session(run: Path, ended_runs: Sequence[Path], remove_scratch: Callable[[], int]) -> int:
    """End the current pointer last and pass its verified owner to batch cleanup."""
    from scripts.astra import design_roles
    from scripts.direct import progress_history, remove_workers

    membership = staff.local_snapshot()
    owner = membership.by_current_session(staff.runtime_id())
    if owner is None or owner.kind not in ("unit", "showrunner") or not unit_runs.points_to_run(owner, run):
        print(f"end_session: {run}: the prepared run pointer changed.", file=sys.stderr)
        return 1
    for folder in ended_runs:
        state = recorder_state.read_state(folder / unit_runs.STATE_FILE)
        if state.get(unit_runs.OWNER_FIELD) != owner.id or not unit_runs.terminal_run(state):
            print(f"end_session: {folder}: the run has no completed end for this owner.", file=sys.stderr)
            return 1
    ended = progress_history.end_run(run, progress_history.RunEndRequested("stopped"))
    if isinstance(ended, unit_runs.RunExecutionBlocker):
        print(f"end_session: {run}: run end remains pending. {ended.reason}.", file=sys.stderr)
        return 1
    prepared = ended if isinstance(ended, unit_runs.PreparedRunEnded) else unit_runs.CleanupOwnerFromState.RECORDED
    if worker_admission.cleanup_parent(staff.local_snapshot(), run, prepared).id != owner.id:
        print(f"end_session: {run}: the run owner changed before cleanup.", file=sys.stderr)
        return 1
    status = 0
    runs = (run, *ended_runs)
    for folder in runs:
        status |= design_roles.main(["unbind-run", str(folder)])
    status |= remove_workers.remove_selected_workers({folder.resolve() for folder in runs}, prepared=prepared)
    token = _scratch_cleanup_owner.set(prepared)
    try:
        status |= remove_scratch()
    finally:
        _scratch_cleanup_owner.reset(token)
    return status


def held_claude_sessions() -> set[str]:
    record = staff.local_snapshot()
    return {row.current_session for row in record.find(kind="worker")
            if isinstance(record.backends.get(row.id), staff.ClaudeBackend)}


def shown_name(run: Path, address: str, *, role: str = "", held_design_id: str = "", held_role: str = "") -> str:
    """Render current unit context and stored assignment without changing the address."""
    titles = cast(TitleRenderer, cast(object, import_module("scripts.message.roster")))
    record: dict[str, object] = {}
    if held_design_id:
        return titles.worker_title(str(run), address, {"held_design_id": held_design_id, "held_role": held_role})
    membership = staff.local_snapshot()
    member = worker_admission.member(run, address, membership)
    if member is None:
        return address
    parent = membership.parent_of(member)
    if parent is None:
        return address
    if parent.kind != "unit":
        return titles.worker_title(str(run), address, record)
    owner = membership.parent_of(parent)
    assigned_role = role
    if not assigned_role:
        assigned_role = member.role.value if isinstance(member.role, staff.KnownWorkerRole) else ""
    jobs: dict[str, JobKey] = {"impl": "implementer", "test": "tester", "fix": "repair_worker",
                               "review": "code_reviewer"}
    peers = [row.name for row in membership.find(kind="worker", parent=parent.id)]
    ordinal = peers.index(member.name) + 1
    return titles.unit_worker_title(owner.name if owner is not None else "", parent.name,
                                    jobs.get(assigned_role, "worker"),
                                    ordinal, address)


class WorkerTitleArguments(argparse.Namespace):
    session_dir: Path = Path()
    name: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    display = commands.add_parser("shown-name")
    _ = display.add_argument("--session-dir", type=Path, required=True)
    _ = display.add_argument("--name", required=True)
    args = parser.parse_args(namespace=WorkerTitleArguments())
    try:
        print(shown_name(args.session_dir, args.name))
        return 0
    except (WorkerUnavailable, OSError, ValueError) as error:
        print(f"workers: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
