#!/usr/bin/env python3
"""Run a worker command detached and deliver its result through the native worker route.

Start writes a run record and returns after it starts the supervisor.
The supervisor runs the command and delivers its result to the current worker.
A unit director or showrunner names its run folder and receives its result through send.py.
Reap finishes records whose supervisor disappeared and delivers their results.
The command runs in its own session; the record names its process group.
`stop` uses the run's slice on Linux and the saved group on systems without pidfds.
The supervisor records and delivers the result.
A watcher in the command group holds the supervisor pipe. If the supervisor ends first, the watcher
stops the command group and delivers the STOPPED result.
Exit 3 means that the caller is neither a registered worker nor a director with a run folder.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Collection, Generator, Mapping, Sequence

import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum, StrEnum
from importlib import import_module
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, SourceFileLoader
from importlib.util import spec_from_file_location
from pathlib import Path
from types import CodeType, ModuleType
from typing import IO, TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, TypeGuard, cast, override

from scripts.agents import detached_run_process, worker_lifecycle
from scripts.buildlog import treekey
from scripts.direct import recorder_state
from scripts.lib import atomic_file, conf, process_group, process_start, process_stop, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.python_startup.sitecustomize import startup_pythonpath
from scripts.lib.cancellation import CANCELLATION_SIGNALS
from scripts.message import send_budget
from scripts.production import staff, worker_admission

if TYPE_CHECKING:
    from scripts.accounts.session_state import RunEvidence
    from scripts.herdr import worker_panes
    from scripts.message import send

# Exit status of `start` when no worker or director owns the run: run the command attached.
NOT_A_DETACHED_WORKER = 3
# A worker at its live-run limit must keep its command pending, never run it attached.
LIVE_RUN_LIMIT_REACHED = 4
# Lines of the log the result message carries.
RESULT_TAIL_LINES = 20
# verify.sh's VERIFY_SUPERSEDED_STATUS: a newer call of the same verb from the same worker in the
# same worktree replaced this one while it waited for the cargo token. It ran nothing, so it is
# neither a pass nor a failure, and the newer call's result answers instead.
SUPERSEDED_STATUS = 76
TREE_CHANGED_STATUS = 77
TREE_CHANGED_INSTRUCTION = "Resubmit after your edits."
SUPERSEDED_INSTRUCTION = ("A newer call of the same verb replaced this one before it built. "
                          + "Nothing failed and nothing needs a repair. Read the newer call's result.")
# The verdict of a run whose supervisor is gone: it reported no exit status.
ORPHAN_VERDICT = ("FAIL (no exit status): the run's supervisor process is gone, so the command was killed or "
                  + "crashed before it reported. Start the run again if you still need its result.")
from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR as DETACHED_RUNS_DIR, DETACHED_RUN_START_GRACE_SECS
# Each run slice is a child of builds-detached.slice, which is under builds.slice.
# The launcher publishes the slice identity before it starts the command.
# A missing slice identity after launch means that the command did not start there.
# The command then runs without a scope. BUILDLOG_SCOPE=0 disables the scope, as it does for invoke.sh.
SCOPE_SCRIPT = """import os, sys
from pathlib import Path
root, slice_name, unit, *command = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.process_stop import ScopeUnit, launch_in_slice
sys.exit(launch_in_slice(Path(os.environ["UNIT_DETACHED_RECORD"]), ScopeUnit(slice_name, unit), command))
"""
# The gate shell keeps its PID across exec and waits for identity publication.
# Its watcher reads the supervisor pipe. An end of input without "done" means that the supervisor ended
# before the command: a kill of any kind or a crash. The watcher then runs `supervisor-gone`.
COMMAND_GATE = ('read -r ready || exit 125; watcher_python=$1 watcher_script=$2; shift 2; '
                + '(read -r end; [ "$end" = done ] || exec "$watcher_python" "$watcher_script" supervisor-gone '
                + '"$UNIT_DETACHED_RECORD") <&0 & exec 0</dev/null; exec "$@"')
SUPERVISOR_GONE_REASON = "the supervisor ended before the command, so its watcher stopped the command"
CONFIRMED_STOP_SUFFIX = ".stop-confirmed"
STAGED_STOP_SUFFIX = ".stop-staged"


class PaneDispatchUnavailable(TypedDict):
    unavailable: str


class StopFields(TypedDict, total=False):
    stop_reason: str
    scope_error: str
    hold_stop_requests: list[str]
    stop_pass_requested: Literal[True]
    self_stop: Literal[True]


class DetachedRunKind(StrEnum):
    DETECT = "detect"
    PYTHON_TESTS = "python-tests"
    OTHER = "other"


class RunRecord(StopFields):
    pending_hold_stop_requests: NotRequired[list[str]]
    run_id: str
    status: Literal["running", "finished"]
    session_dir: str
    worker: str
    staff_id: str
    label: str
    command: list[str]
    # Older records have no kind.
    kind: NotRequired[Literal["detect", "python-tests", "other"]]
    log: str
    started_at: float
    pid: int
    supervisor_started: NotRequired[str]
    command_pid: NotRequired[int]
    # The command leads its own session, so this group is the command's process group.
    command_group: NotRequired[int]
    command_started: NotRequired[str]
    command_slice: NotRequired[process_stop.RunSliceIdentity]
    stop_notice_id: NotRequired[str]
    slice_cleanup_pending: NotRequired[Literal[True]]
    slice_leftovers: NotRequired[Literal[True]]
    director_dir: str
    done_message_file: str
    # The tree key of cwd at start, "" when unknown; records before 2026-10-21 have none.
    tree_key: NotRequired[str]
    exit_code: NotRequired[int]
    finished_at: NotRequired[str]
    delivery: NotRequired[str]
    delivered_at: NotRequired[float]
    worker_result_receipt: NotRequired[object]
    blocked_notice_accepted: NotRequired[Literal[True]]
    push_retries: NotRequired[int]
    push_alert_attempted: NotRequired[Literal[True]]
    # Set when the run ended without its supervisor: "supervisor gone".
    completion_pending: NotRequired[bool]
    ended: NotRequired[str]
    # An empty id records a worker run outside a pass. Older records omit the field.
    pane_dispatch_id: NotRequired[str | PaneDispatchUnavailable]
    orphan_result_superseded: NotRequired[Literal[True]]


@dataclass(frozen=True)
class NotDetachedWorker:
    """The caller is not a registered worker, so its command runs attached."""


@dataclass(frozen=True)
class PaneCaller:
    """A worker with a published staff parent."""

    session_dir: str
    name: str
    staff_id: staff.StaffId


@dataclass(frozen=True)
class DirectorCaller:
    """A unit director or showrunner that owns a run in its own folder."""

    name: str
    staff_id: staff.StaffId


Caller = PaneCaller | DirectorCaller | NotDetachedWorker


def caller_from_environment(environment: dict[str, str]) -> Caller:
    """Find the worker, unit director, or showrunner through its current session."""
    membership = staff.local_snapshot()
    member = membership.by_current_session(staff.runtime_id())
    if member is None or member.kind != "worker":
        identity = environment.get("CODEX_THREAD_ID") or environment.get("CLAUDE_CODE_SESSION_ID", "")
        member = membership.by_current_session(identity) or member
    if member is not None and member.kind == "worker":
        from scripts.herdr import worker_panes

        parent = membership.parent_of(member)
        if parent is not None:
            return PaneCaller(str(worker_panes.worker_run(member, record=membership)), member.name, member.id)
    if member is not None and member.kind in ("unit", "showrunner"):
        return DirectorCaller(member.name, member.id)
    return NotDetachedWorker()


@dataclass(frozen=True)
class RunOwnerUnavailable:
    reason: str


def _run_owner(record: RunRecord, membership: staff.StaffSnapshot) -> staff.StaffRow | RunOwnerUnavailable:
    """The worker, unit director, or showrunner that owns the run."""
    member = membership.by_id(staff.StaffId(record.get("staff_id", "")))
    if member is None or member.kind not in ("worker", "unit", "showrunner"):
        reason = f"{record['worker']}: detached run has no worker or director staff row"
        return RunOwnerUnavailable(reason)
    return member


def _runs_dir(session_dir: str) -> Path:
    return Path(session_dir) / DETACHED_RUNS_DIR


@contextlib.contextmanager
def record_lock(path: Path) -> Generator[None]:
    """Serialize supervisor updates and drain stop reasons for one record."""
    with path.with_suffix(".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


PENDING_STOP_SUFFIX = ".stop-pending"
_RUN_INDEX_SUFFIXES = (PENDING_STOP_SUFFIX, process_stop.RUN_SLICE_RECORD_SUFFIX)


def _write_record(path: Path, record: RunRecord) -> RunRecord:
    with record_lock(path):
        current = _read_record(path)
        if current["run_id"] != record["run_id"]:
            raise SystemExit(f"detached_runs: {path} no longer holds run {record['run_id']}")
        fields: dict[str, object] = {**current, **record}
        if current["status"] == "finished" and record["status"] == "running":
            fields.update(current)
        if delivery := current.get("delivery"):
            fields["delivery"] = delivery
        stop_fields = ("stop_reason", "hold_stop_requests", "stop_pass_requested", "self_stop")
        if _has_saved_cgroup(current):
            stop_fields += ("scope_error", "slice_leftovers", "slice_cleanup_pending", "stop_notice_id",
                            "pending_hold_stop_requests")
        saved = cast(Mapping[str, object], current)
        for key in stop_fields:
            if key in saved:
                fields[key] = saved[key]
            else:
                _ = fields.pop(key, None)
        if fields["status"] == "finished":
            _ = fields.pop("slice_cleanup_pending", None)
            if fields.get("scope_error"):
                _ = fields.pop("finished_at", None)
            elif current["status"] == "running":
                stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                if _has_saved_cgroup(current):
                    # Only a stamp saved after the slice cleanup counts.
                    fields["finished_at"] = current.get("finished_at", stamp)
                else:
                    _ = fields.setdefault("finished_at", stamp)
        if not _is_run_record(fields):
            raise SystemExit(f"detached_runs: {path} update is not a run record")
        write_text_atomic(path, json.dumps(fields, indent=2, sort_keys=True))
        if fields["status"] == "finished":
            if fields.get("stop_pass_requested"):
                path.with_suffix(PENDING_STOP_SUFFIX).unlink(missing_ok=True)
            if _has_saved_cgroup(current):
                path.with_suffix(process_stop.RUN_SLICE_RECORD_SUFFIX).unlink(missing_ok=True)
        return fields


def _is_hold_stop_requests(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item) is not None
        for item in cast(list[object], value)
    )


def _is_run_record(raw: dict[str, object]) -> TypeGuard[RunRecord]:
    pane_dispatch = raw.get("pane_dispatch_id")
    match raw:
        case {
            "run_id": str(run_id), "status": "running" | "finished", "session_dir": str(),
            "worker": str(), "staff_id": str(), "label": str(), "command": list(),
            "log": str(), "started_at": int() | float(), "pid": int(),
            "director_dir": str(), "done_message_file": str(),
        }:
            return (bool(run_id) and not isinstance(raw["started_at"], bool)
                    and not isinstance(raw["pid"], bool)
                    and all(isinstance(arg, str) for arg in cast(list[object], raw["command"]))
                    and all(key not in raw or type(raw[key]) is int
                            for key in ("command_pid", "command_group", "exit_code", "push_retries"))
                    and all(key not in raw or isinstance(raw[key], str)
                            for key in ("supervisor_started", "command_started", "tree_key", "finished_at",
                                        "delivery", "ended", "stop_reason", "scope_error", "stop_notice_id"))
                    and ("hold_stop_requests" not in raw or _is_hold_stop_requests(raw["hold_stop_requests"]))
                    and ("pending_hold_stop_requests" not in raw or (
                        raw.get("stop_pass_requested") is True and "command_slice" in raw
                        and _is_hold_stop_requests(raw["pending_hold_stop_requests"])))
                    and ("delivered_at" not in raw or type(raw["delivered_at"]) in (int, float))
                    and ("completion_pending" not in raw or isinstance(raw["completion_pending"], bool))
                    and ("command_slice" not in raw or process_stop.is_slice_identity(raw["command_slice"]))
                    and all(key not in raw or raw[key] is True
                            for key in ("stop_pass_requested", "self_stop", "orphan_result_superseded",
                                        "blocked_notice_accepted", "push_alert_attempted",
                                        "slice_cleanup_pending", "slice_leftovers"))
                    and ("pane_dispatch_id" not in raw or isinstance(pane_dispatch, str)
                         or (isinstance(pane_dispatch, dict)
                             and isinstance(cast(dict[str, object], pane_dispatch).get("unavailable"), str))))
        case _:
            return False


def _read_stop_fields(path: Path, run_id: object) -> StopFields:
    saved = worker_lifecycle.read_json_object(path)
    if not saved:
        return {}
    reason = saved.get("stop_reason")
    requests = saved.get("hold_stop_requests", [])
    if (not isinstance(run_id, str) or saved.get("run_id") != run_id or not isinstance(reason, str)
            or not _is_hold_stop_requests(requests)
            or any(key in saved and saved[key] is not True for key in ("stop_pass_requested", "self_stop"))):
        raise worker_lifecycle.WorkerStateUnreadable(path, "invalid stop fields")
    fields: StopFields = {"stop_reason": reason}
    if isinstance(error := saved.get("scope_error"), str):
        fields["scope_error"] = error
    if requests:
        fields["hold_stop_requests"] = requests
    if saved.get("stop_pass_requested") is True:
        fields["stop_pass_requested"] = True
    if saved.get("self_stop") is True:
        fields["self_stop"] = True
    return fields


def _read_record_if_present(path: Path) -> RunRecord | None:
    raw = worker_lifecycle.read_json_object(path)
    if not raw and not path.exists():
        return None
    if not _is_run_record(raw):
        raise SystemExit(f"detached_runs: {path} is not a run record")
    if path.name != f"{raw['run_id']}.json":
        raise SystemExit(f"detached_runs: {path} does not match run ID {raw['run_id']}")
    return _with_confirmed_stop(path, raw)


def _with_confirmed_stop(path: Path, record: RunRecord) -> RunRecord:
    confirmed = _read_stop_fields(path.with_suffix(CONFIRMED_STOP_SUFFIX), record["run_id"])
    requests = confirmed.get("hold_stop_requests")
    if requests:
        confirmed["hold_stop_requests"] = list(dict.fromkeys([*record.get("hold_stop_requests", []), *requests]))
    return {**record, **confirmed} if confirmed else record


def confirms_hold_stop(path: Path, run_id: str, request_id: str) -> bool:
    """Check the published confirmation of this hold request."""
    try:
        record = _read_record(path)
    except (SystemExit, worker_lifecycle.WorkerStateUnreadable):
        return False
    return record["run_id"] == run_id and request_id in record.get("hold_stop_requests", [])


def _read_record(path: Path) -> RunRecord:
    record = _read_record_if_present(path)
    if record is None:
        raise SystemExit(f"detached_runs: {path} is not a run record")
    return record


def _recover_staged_pane_stop(path: Path, record: RunRecord) -> RunRecord:
    """Recover the pane request under the record lock after the command exits."""
    if record.get("stop_pass_requested") or not path.with_suffix(PENDING_STOP_SUFFIX).exists():
        return record
    staged = path.with_suffix(STAGED_STOP_SUFFIX)
    fields = _read_stop_fields(staged, record["run_id"])
    if not fields.get("stop_pass_requested"):
        return record
    _ = staged.replace(path.with_suffix(CONFIRMED_STOP_SUFFIX))
    atomic_file.sync_folder(path.parent)
    return {**record, **fields}


def _write_staged_stop(path: Path, stopping: Mapping[str, object]) -> None:
    write_text_atomic(path.with_suffix(STAGED_STOP_SUFFIX), json.dumps(stopping, indent=2, sort_keys=True),
                      new_file_mode=path.stat().st_mode & 0o777)


def stop_with_reason(
    path: Path, reason: str, *, verify_group: Callable[[], CommandGroup | CommandGroupUnverified],
    signal_group: Callable[[int, int], None], stop_pass: bool = False, self_stop: bool = False,
    unconfirmed: str = "",
    hold_request_id: str = "",
) -> bool:
    """Publish the stop reason after signaling the verified group."""
    with record_lock(path):
        try:
            record = _read_record(path)
        except SystemExit:
            return False
        if record.get("stop_pass_requested") is True:
            return False
        if isinstance(verify_group(), CommandGroupUnverified):
            return False
        staged = path.with_suffix(STAGED_STOP_SUFFIX)
        confirmed = path.with_suffix(CONFIRMED_STOP_SUFFIX)
        stopping = {**record, "stop_reason": reason}
        if unconfirmed:
            stopping["scope_error"] = unconfirmed
        if hold_request_id:
            stopping["hold_stop_requests"] = [*record.get("hold_stop_requests", []), hold_request_id]
        pending_stop = path.with_suffix(PENDING_STOP_SUFFIX)
        if stop_pass:
            # Index the run before it can outlive its supervisor.
            pending_stop.touch(exist_ok=True)
            stopping["stop_pass_requested"] = True
            if self_stop:
                stopping["self_stop"] = True
        signaled = False
        try:
            try:
                _write_staged_stop(path, stopping)
                command = verify_group()
                if isinstance(command, CommandGroupUnverified):
                    return False
                signal_group(command.group, signal.SIGTERM)
                signaled = True
                try:
                    _ = staged.replace(path)
                except OSError:
                    # Publish the staged content only after the signal succeeds.
                    _ = staged.replace(confirmed)
                    atomic_file.sync_folder(path.parent)
                    raise
                confirmed.unlink(missing_ok=True)
                atomic_file.sync_folder(path.parent)
            finally:
                if stop_pass and not signaled:
                    pending_stop.unlink(missing_ok=True)
                staged.unlink(missing_ok=True)
        except OSError as error:
            if not signaled:
                raise
            print(f"detached_runs: {path}: sent SIGTERM but could not finalize the stop record: {error}",
                  file=sys.stderr)
        return True


@dataclass(frozen=True)
class CommandGroup:
    """The recorded process group of a running command and the start identity of its leader."""
    group: int
    started: str


@dataclass(frozen=True)
class CommandGroupUnverified:
    """The current command group could not be confirmed."""


def command_group(path: Path) -> CommandGroup | None:
    """Read the command group of a running record, or None before the supervisor publishes it."""
    record = worker_lifecycle.read_json_object(path)
    return _recorded_group(record) if record.get("status") == "running" else None


def _recorded_group(record: Mapping[str, object]) -> CommandGroup | None:
    group, started = record.get("command_group"), record.get("command_started")
    if isinstance(group, int) and group > 0 and isinstance(started, str) and started:
        return CommandGroup(group, started)
    return None


def _signal_stopped_command(command: CommandGroup, number: int) -> None:
    process_stop.stop_group(process_stop.ProcessLifetime(command.group, command.started), number)


@contextlib.contextmanager
def _scope_stop_lock(path: Path, *, blocking: bool) -> Generator[bool]:
    """Serialize scope stops and completion without holding the record lock."""
    with path.with_suffix(".scope-stop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if blocking:
                raise
            yield False
        else:
            yield True


def _scope_stop_graces() -> tuple[float, float]:
    settings = conf.read("shutdown")
    return float(settings.text("stop_grace_seconds")), float(settings.text("kill_grace_seconds"))


def _finish_slice_stop(
    path: Path, run_slice: process_stop.RunSliceIdentity, *, ended: bool,
    grace: float, kill_grace: float, cleanup: bool = False, commit_synced: bool = False,
) -> None:
    """Confirm the end of an authorized stop under the stop lock."""
    if not ended:
        if cleanup:
            with record_lock(path):
                record = _read_record(path)
                if not record.get("slice_leftovers"):
                    record["slice_leftovers"] = True
                    write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        elif not commit_synced:
            atomic_file.sync_folder(path.parent)
        process_stop.stop_slice(run_slice, grace, kill_grace)
    _confirm_slice_end(path, run_slice)


def _confirm_slice_end(path: Path, run_slice: process_stop.RunSliceIdentity) -> None:
    """Confirm pending hold requests after the caller verifies the slice ended."""
    with record_lock(path):
        record = _read_record(path)
        if record.get("command_slice") != run_slice:
            raise process_stop.SliceStopFailed("the saved run slice changed during the stop")
        cleanup_pending = record.pop("slice_cleanup_pending", False)
        pending_requests = record.pop("pending_hold_stop_requests", [])
        if pending_requests:
            record["hold_stop_requests"] = list(dict.fromkeys([*record.get("hold_stop_requests", []), *pending_requests]))
        if cleanup_pending or pending_requests:
            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))


def _resume_slice_stop(path: Path, run_slice: process_stop.RunSliceIdentity, *, allow_cleanup: bool) -> None:
    with _scope_stop_lock(path, blocking=False) as acquired:
        if not acquired:
            return
        record = _read_record(path)
        if record.get("command_slice") != run_slice or record["status"] != "running":
            return
        cleanup = allow_cleanup and bool(record.get("slice_cleanup_pending")) and not record.get("stop_pass_requested")
        if not (record.get("stop_pass_requested") or cleanup):
            return
        grace, kill_grace = _scope_stop_graces()
        ended = process_stop.slice_has_ended(run_slice)
        _finish_slice_stop(path, run_slice, ended=ended, grace=grace, kill_grace=kill_grace, cleanup=cleanup)


def _read_slice_end(run_slice: process_stop.RunSliceIdentity, *, retry_seconds: float) -> bool:
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            return process_stop.slice_has_ended(run_slice)
        except process_stop.SliceStopFailed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.05, remaining))


def _stop_slice_command(
    path: Path, run_slice: process_stop.RunSliceIdentity, reason: str, *,
    check: Callable[[], bool], retry_slice_read: bool, stop_notice_id: str, hold_request_id: str,
) -> bool:
    grace, kill_grace = _scope_stop_graces()
    with _scope_stop_lock(path, blocking=True):
        record = _read_record_if_present(path)
        if record is None or record.get("command_slice") != run_slice or record["status"] != "running":
            return False
        ended = _read_slice_end(run_slice, retry_seconds=grace if retry_slice_read else 0.0)
        with record_lock(path):
            record = _read_record_if_present(path)
            if record is None or record.get("command_slice") != run_slice or record["status"] != "running" or not check():
                return False
            recovered_commit = bool(record.get("stop_pass_requested"))
            pending_requests = record.get("pending_hold_stop_requests", [])
            new_hold_request = (bool(hold_request_id) and hold_request_id not in pending_requests
                                and hold_request_id not in record.get("hold_stop_requests", []))
            if ended and (not recovered_commit or new_hold_request):
                return False
            if not recovered_commit:
                record.update(stop_reason=reason, stop_pass_requested=True)
                if stop_notice_id:
                    record["stop_notice_id"] = stop_notice_id
            if new_hold_request:
                record["pending_hold_stop_requests"] = [*pending_requests, hold_request_id]
            commit_synced = not recovered_commit or new_hold_request
            if commit_synced:
                write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        _finish_slice_stop(path, run_slice, ended=ended, grace=grace, kill_grace=kill_grace, commit_synced=commit_synced)
    return True


def _stop_caller_is_owner(owner: object, environment: Mapping[str, str]) -> bool:
    """Match the caller's native process to the run's worker staff ID."""
    from scripts.agents import pane_record
    from scripts.herdr import herdr, herdr_types
    from scripts.message import session_records

    pid = os.getpid()
    sessions = session_records.sessions_dir()
    try:
        for _ in range(pane_record.ANCESTOR_LIMIT):
            try:
                _ = (sessions / f"{pid}.json").stat()
            except FileNotFoundError:
                arguments = pane_record.process_arguments(pid)
                if herdr.is_claude_process(arguments):
                    return False
                if herdr.is_codex_process(arguments):
                    native = herdr.native_process(pid, "codex")
                    current = native.identity.id if isinstance(native.identity, herdr.PaneSessionReport) else ""
                    break
            else:
                current = staff.runtime_id(pid)
                break
            if pid <= 1:
                return False
            pid = pane_record.process_parent(pid)
        else:
            return False
        identities = {identity for identity in (environment.get("CODEX_THREAD_ID"),
                                               environment.get("CLAUDE_CODE_SESSION_ID")) if identity}
        if not current or identities - {current}:
            return False
        member = staff.local_snapshot().by_current_session(current)
    except (OSError, ValueError, IndexError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError):
        return False
    return member is not None and member.kind == "worker" and member.id == owner


def stop_command(
    path: Path, reason: str, *, check: Callable[[], bool] = lambda: True,
    stop_pass: bool = True, signal_group: Callable[[int, int], None] | None = None,
    retry_slice_read: bool = False, stop_notice_id: str = "", hold_request_id: str = "",
) -> bool:
    """Stop the saved run slice or signal the verified command group."""
    if not reason:
        return False
    use_pidfd = process_stop.supported()
    record = worker_lifecycle.read_json_object(path)
    supervisor = _integer(record.get("pid"))
    if "command_slice" in record:
        if not use_pidfd:
            return False
        run_slice = record["command_slice"]
        if not process_stop.is_slice_identity(run_slice):
            raise process_stop.SliceStopFailed("the saved run slice is invalid")
        return _stop_slice_command(path, run_slice, reason, check=check, retry_slice_read=retry_slice_read,
                                   stop_notice_id=stop_notice_id, hold_request_id=hold_request_id)
    if stop_notice_id:
        return False
    starting_command = _recorded_group(record)
    if starting_command is None or record.get("status") != "running":
        return False

    def verify_group() -> CommandGroup | CommandGroupUnverified:
        command = command_group(path)
        if (command is None or command != starting_command
                or _has_saved_cgroup(worker_lifecycle.read_json_object(path))
                or not check()
                or (stop_pass and detached_run_process.supervisor_match(supervisor, path) is not True
                    and not isinstance(process_start.process_start(supervisor), (process_start.Gone, process_start.Zombie)))
                or ((signal_group is None or not use_pidfd) and process_start.process_start(command.group)
                    != process_start.Started(command.started))):
            return CommandGroupUnverified()
        return command

    def signal_command(group: int, number: int) -> None:
        if group != starting_command.group:
            raise ProcessLookupError("the command changed")
        if signal_group is not None:
            signal_group(group, number)
        elif use_pidfd:
            _signal_stopped_command(starting_command, number)
        else:
            process_group.send_process_group(group, number)

    cancel_pass = stop_pass and not use_pidfd
    try:
        sent = stop_with_reason(
            path, reason, verify_group=verify_group, signal_group=signal_command, stop_pass=cancel_pass,
            self_stop=cancel_pass and _stop_caller_is_owner(record.get("staff_id"), os.environ),
            unconfirmed="the command group received TERM without a verified scope" if use_pidfd else "",
            hold_request_id=hold_request_id if not use_pidfd else "",
        )
        return sent and not use_pidfd
    except (ProcessLookupError, PermissionError):
        return False


def acknowledge_stop_notice(path: Path, notice_id: str) -> None:
    """Release notice evidence after a saved notice or confirmed abandonment."""
    if notice_id and worker_lifecycle.read_json_object(path).get("stop_notice_id") == notice_id:
        with record_lock(path):
            record = _read_record_if_present(path)
            if record is not None and record.get("stop_notice_id") == notice_id:
                del record["stop_notice_id"]
                write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
                return
    atomic_file.sync_folder(path.parent)


@dataclass(frozen=True)
class PaneRunSeat:
    name: str
    staff_id: staff.StaffId


@dataclass(frozen=True)
class UnregisteredRunSeat:
    name: str


def _run_seat(session_dir: str, name: str) -> PaneRunSeat | UnregisteredRunSeat:
    """Resolve the current staff worker through its active recorder slot."""
    run = Path(session_dir)
    membership = staff.local_snapshot()
    try:
        owner = worker_admission.parent(membership, run=run)
    except worker_admission.RunOwnerUnavailable:
        return UnregisteredRunSeat(name)
    state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
    passes = state.get("pass")
    current = cast(dict[str, object], passes).get(name) if isinstance(passes, dict) else None
    member = None
    if isinstance(current, dict):
        dispatch = cast(dict[str, object], current)
        if dispatch.get("status") == "active":
            name = str(dispatch.get("worker_name", "")) or name
            member = membership.by_id(staff.StaffId(str(dispatch.get("worker_id", ""))))
    if member is None or member.kind != "worker" or not member.reports_to(owner.id):
        member = worker_admission.find_worker(membership, owner.id, name)
    if member is not None:
        return PaneRunSeat(member.name, member.id)
    return UnregisteredRunSeat(name)


def _integer(value: object) -> int:
    return value if type(value) is int else 0


def _number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _group_is_live(
    group: int, *, budget: process_start.ProcessReadBudget = process_start.STANDARD_PROCESS_READ_BUDGET,
) -> bool:
    """Keep the run pending while its group has live processes or cannot be read."""
    timeout = process_start.ps_read_timeout(budget)
    if timeout <= 0:
        return True
    try:
        result = subprocess.run(["ps", "-A", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, check=True, timeout=timeout)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return True
    if result.stderr or not result.stdout.strip():
        return True
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            return True
        if int(fields[0]) == group and not fields[1].startswith("Z"):
            return True
    return False


@dataclass(frozen=True)
class SavedProcessIdReused:
    """A different process now owns the saved PID."""


def _saved_process(
    pid: object, started: object,
    *, budget: process_start.ProcessReadBudget = process_start.STANDARD_PROCESS_READ_BUDGET,
) -> process_start.ProcessStart | SavedProcessIdReused:
    """Read the saved process identity and identify a reused PID."""
    if not isinstance(started, str) or not started or _integer(pid) <= 0:
        return process_start.Gone()
    observed = process_start.process_start(_integer(pid), budget=budget)
    if isinstance(observed, (process_start.Started, process_start.Zombie)) and observed.start != started:
        return SavedProcessIdReused()
    return observed


def _orphaned(path: Path, raw: Mapping[str, object], now: float) -> bool:
    """Recover a running record after its command and owned group processes end."""
    if raw.get("status") != "running":
        return False
    pid = _integer(raw.get("pid"))
    if pid > 0:
        return not detached_run_process.supervises(pid, path) and _command_has_ended(raw, pid)
    if _has_saved_cgroup(raw):
        return _command_has_ended(raw, pid)
    started = _number(raw.get("started_at"), now)
    return now - started >= DETACHED_RUN_START_GRACE_SECS


def _has_saved_cgroup(raw: Mapping[str, object]) -> bool:
    return "command_slice" in raw


def _command_has_ended(
    raw: Mapping[str, object], pid: int,
    *, budget: process_start.ProcessReadBudget = process_start.STANDARD_PROCESS_READ_BUDGET,
) -> bool:
    if "command_slice" in raw:
        run_slice = raw["command_slice"]
        try:
            return process_stop.is_slice_identity(run_slice) and process_stop.slice_has_ended(run_slice)
        except process_stop.SliceStopFailed:
            return False
    command = _saved_process(raw.get("command_pid"), raw.get("command_started"), budget=budget)
    if isinstance(command, (process_start.Started, process_start.Unreadable)):
        return False
    group = _integer(raw.get("command_group"))
    if group > 0:
        leader = group == _integer(raw.get("command_pid"))
        if leader and isinstance(command, SavedProcessIdReused):
            return True
        if not _group_is_live(group, budget=budget):
            return True
        # The group ID can be reused after the last member exits.
        return leader and isinstance(
            _saved_process(group, raw.get("command_started"), budget=budget), SavedProcessIdReused)
    supervisor = _saved_process(pid, raw.get("supervisor_started"), budget=budget)
    owns_group = isinstance(supervisor, (process_start.Started, process_start.Zombie))
    if not owns_group and isinstance(command, process_start.Zombie):
        try:
            owns_group = os.getpgid(_integer(raw.get("command_pid"))) == pid
        except ProcessLookupError:
            pass
        except OSError:
            return False
    return not owns_group or not _group_is_live(pid, budget=budget)


def _stopped_group_has_ended(record: RunRecord) -> bool:
    return (_has_saved_cgroup(record) or _integer(record.get("command_group")) > 0) and _command_has_ended(
        record, record["pid"])


def _cancels_pass(record: RunRecord) -> bool:
    return bool(record.get("stop_pass_requested") and not record.get("self_stop") and not record.get("scope_error"))


def _retired_worker_result(record: RunRecord) -> bool:
    if record.get("self_stop") and _worker_result_pending(record.get("delivery", "")):
        with contextlib.suppress(staff.StaffUnreadable):
            return staff.local_snapshot().by_id(staff.StaffId(record["staff_id"])) is None
    return False


SUPERSEDED_ORPHAN_DELIVERY = "nothing delivered: the supervisor is gone and a newer run of the same command answers"
OTHER_PASS_DELIVERY = "nothing delivered: the result belongs to a different pane pass"
COVERED_BY_DONE = "finished: the pass DONE covers the result"
INTERRUPTED_PASS_DELIVERY = "interrupted pass result: "


@dataclass
class OrphanReapResult:
    """Keep reaped run IDs and the completion events that must follow the admission lock."""

    run_ids: list[str] = field(default_factory=list)
    pending_completions: list[tuple[Path, RunRecord]] = field(default_factory=list)

    def queue_completion(self, path: Path, record: RunRecord) -> None:
        """Create the claim file while the caller holds the admission lock."""
        path.with_suffix(".completion.lock").touch(exist_ok=True)
        self.pending_completions.append((path, record))

    def complete(self) -> None:
        for path, _ in self.pending_completions:
            # Pruning can remove the claim file after collection. Do not create it here.
            try:
                claim = path.with_suffix(".completion.lock").open("r")
            except FileNotFoundError:
                continue
            try:
                with claim:
                    try:
                        fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    current = _read_record(path)
                    pending = _needs_push_retry(current)
                    if _needs_completion(current):
                        pending |= _replay_completion(path, current) == CompletionReplay.RETRY
                if pending:
                    _alert_failed_push(path, check_receipt=True)
            except (SystemExit, worker_lifecycle.WorkerStateUnreadable) as error:
                print(" ".join(str(error).splitlines()), file=sys.stderr)


def reap_orphans(session_dir: str, worker: str = "") -> list[str]:
    """Recover results after the command and owned group processes end.

    Deliver the saved command status when available.
    The start lock serializes orphan scans.
    """
    runs = _runs_dir(session_dir)
    result = OrphanReapResult()
    if runs.is_dir():
        with (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            result = _reap_orphans_locked(runs, worker)
        result.complete()
    run = runs.parent
    if next(run.glob(".*.pending-done.json"), None) is not None:
        retry_done = cast(Callable[[Path, str], None], import_module("scripts.herdr.turn_end").retry_pending_done)
        retry_done(run, worker)
    if next(run.glob(".*.pass-notices.jsonseq"), None) is not None:
        retry_notices = cast(Callable[[Path, str], None], import_module("scripts.herdr.pass_notices").retry)
        retry_notices(run, worker)
    return result.run_ids


def _reap_orphans_locked(runs: Path, worker: str = "") -> OrphanReapResult:
    """Reap orphan records while the caller holds the runs-folder admission lock."""
    now = time.time()
    records: list[tuple[Path, RunRecord]] = []
    for path in sorted(runs.glob("*.json")):
        try:
            records.append((path, _read_record(path)))
        except (SystemExit, worker_lifecycle.WorkerStateUnreadable) as error:
            print(" ".join(str(error).splitlines()), file=sys.stderr)
    result = OrphanReapResult()
    for path, raw in records:
        if worker and raw.get("worker") != worker:
            continue
        if raw["status"] == "finished" and (_needs_completion(raw) or _needs_push_retry(raw)):
            if not detached_run_process.supervises(raw["pid"], path):
                result.queue_completion(path, raw)
            continue
        if raw["status"] != "running":
            continue
        supervisor_alive = raw["pid"] > 0 and detached_run_process.supervises(raw["pid"], path)
        if (run_slice := raw.get("command_slice")) and (
                raw.get("stop_pass_requested") or (raw.get("slice_cleanup_pending") and not supervisor_alive)):
            try:
                _resume_slice_stop(path, run_slice, allow_cleanup=not supervisor_alive)
            except OSError as error:
                print(f"detached_runs: {path.stem}: stop recovery failed: {error}", file=sys.stderr)
                continue
        if supervisor_alive:
            continue
        try:
            with record_lock(path), contextlib.ExitStack() as scope_lock:
                record = _read_record(path)
                if _has_saved_cgroup(record) and not scope_lock.enter_context(_scope_stop_lock(path, blocking=False)):
                    continue
                if record.get("pending_hold_stop_requests") or not _orphaned(path, record, now):
                    continue
                record = _recover_staged_pane_stop(path, record)
            newer = any(other.get("worker") == record["worker"] and other.get("command") == record["command"]
                        and other.get("staff_id") == record["staff_id"]
                        and _number(other.get("started_at"), 0.0) > record["started_at"]
                        for _, other in records)
            if not record.get("delivery"):
                if _cancels_pass(record):
                    if newer:
                        record["orphan_result_superseded"] = True
                else:
                    record["delivery"] = (SUPERSEDED_ORPHAN_DELIVERY if newer and not record.get("self_stop")
                                          else ORPHAN_RESULT_PENDING)
            record.update(status="finished", ended="supervisor gone", completion_pending=True)
            record = _write_record(path, record)
            result.queue_completion(path, record)
            result.run_ids.append(record["run_id"])
        except (SystemExit, worker_lifecycle.WorkerStateUnreadable) as error:
            print(" ".join(str(error).splitlines()), file=sys.stderr)
    return result


class CompletionReplay(StrEnum):
    HANDLED = "the completion event needs no replay"
    RETRY = "the completion event needs another read"
    CLOSE_PENDING = "the result is saved and the pane still needs to close"


def _finish_record(path: Path, record: RunRecord) -> CompletionReplay:
    """Publish the finished run and claim its pending completion."""
    with path.with_suffix(".completion.lock").open("a") as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        current = _read_record(path)
        if current["status"] == "finished" and current.get("completion_pending") is True:
            return _replay_completion(path, current)
        if (current["status"] != "finished"
                or not (current.get("completion_pending") is False or current.get("delivery"))):
            if _has_saved_cgroup(current) and not _command_has_ended(current, current["pid"]):
                return CompletionReplay.RETRY
            record.update(status="finished", completion_pending=True)
            record = _write_record(path, record)
            return _replay_completion(path, record)
        if not _cancels_pass(current):
            return CompletionReplay.HANDLED
        current["completion_pending"] = True
        current = _write_record(path, current)
        return _settle_completion_diagnostics(path, current)


def _replay_completion(path: Path, record: RunRecord) -> CompletionReplay:
    """Keep an unresolved completion available for the next event."""
    delivery = record.get("delivery", "")
    try:
        if _result_push_failed(delivery):
            if _result_pass_changed(record):
                return CompletionReplay.HANDLED
            if not _unconfirmed_pass_result(record):
                return CompletionReplay.RETRY
        if _cancels_pass(record) and delivery in ("", ORPHAN_RESULT_PENDING):
            delivery = _complete_stopped_run(path, record)
            if delivery is CompletionReplay.RETRY or _worker_result_pending(delivery):
                return CompletionReplay.RETRY
            if _stop_fallback_delivered(record, delivery) and _complete_worker_dispatch(record) == CompletionReplay.RETRY:
                return CompletionReplay.RETRY
            if delivery.startswith("finished: the pane pass "):
                pending_close = cast(Callable[[staff.StaffId, Path], bool],
                    import_module("scripts.herdr.turn_end").completion_close_pending)
                if pending_close(staff.StaffId(record["staff_id"]), Path(record["session_dir"])):
                    return CompletionReplay.CLOSE_PENDING
        elif _retired_worker_result(record):
            if _result_push_failed(deliver(record, command_result(record))):
                return CompletionReplay.RETRY
        else:
            if delivery == ORPHAN_RESULT_PENDING:
                delivery = deliver(record, command_result(record))
                if _worker_result_pending(delivery) and delivery != PARKED_WORKER_RESULT:
                    return CompletionReplay.RETRY
            if delivery != OTHER_PASS_DELIVERY and _complete_worker_dispatch(record) == CompletionReplay.RETRY:
                return CompletionReplay.RETRY
            if delivery == PARKED_WORKER_RESULT and _worker_result_pending(_read_record(path).get("delivery", "")):
                return CompletionReplay.RETRY
    except (Exception, SystemExit) as error:
        detail = " ".join(str(error).splitlines())
        with contextlib.suppress(SystemExit, worker_lifecycle.WorkerStateUnreadable):
            _ = _save_delivery(path, FAILED_RESULT_PUSH + detail, if_current=ORPHAN_RESULT_PENDING)
        print(f"anomaly: {record['run_id']}: completion event failed: {detail}", file=sys.stderr)
        return CompletionReplay.RETRY
    if delivery.startswith(UNCONFIRMED_WORKER_RESULT):
        current = _read_record(path)
        if current.get("completion_pending") is True and current.get("delivery", "").startswith(UNCONFIRMED_WORKER_RESULT):
            return CompletionReplay.RETRY
    return _settle_completion_diagnostics(path, record)


def _settle_completion_diagnostics(path: Path, record: RunRecord) -> CompletionReplay:
    if record.get("pane_dispatch_id") != "":
        from scripts.herdr.worker_panes import StoppedDiagnosticRecovery

        retry = cast(Callable[[Path, staff.StaffId], StoppedDiagnosticRecovery],
                     import_module("scripts.herdr.turn_end").retry_stopped_diagnostics)
        if retry(Path(record["session_dir"]), staff.StaffId(record["staff_id"])) is not StoppedDiagnosticRecovery.SETTLED:
            return CompletionReplay.RETRY
    _clear_completion_pending(path)
    return CompletionReplay.HANDLED


def _complete_stopped_run(path: Path, record: RunRecord) -> str | Literal[CompletionReplay.RETRY]:
    """End the proved pane pass or deliver the ordinary stopped result."""
    from scripts.herdr import worker_panes

    run = Path(record["session_dir"])
    runs = _runs_dir(record["session_dir"])

    @contextlib.contextmanager
    def guard_ended() -> Generator[worker_panes.CloseAllowed | worker_panes.Kept]:
        # The caller holds the worker lock before it takes admission and the recorder lock.
        with (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            if not _stopped_group_has_ended(record):
                yield worker_panes.Kept("the command group has no confirmed end")
                return
            now = time.time()
            for other in _run_record_paths(runs):
                if other == path:
                    continue
                raw = worker_lifecycle.read_json_object(other)
                if raw.get("staff_id") != record["staff_id"]:
                    continue
                if raw.get("status") == "running" and not _orphaned(other, raw, now) or (
                        raw.get("status") == "finished"
                        and (_has_saved_cgroup(raw) or raw.get("stop_pass_requested") is True)
                        and not _command_has_ended(raw, _integer(raw.get("pid")))):
                    yield worker_panes.Kept(f"another detached run can still be live: {other.stem}")
                    return
            yield worker_panes.CloseAllowed()

    stop_pass = cast(Callable[[Path, RunRecord, Callable[
        [], contextlib.AbstractContextManager[worker_panes.CloseAllowed | worker_panes.Kept]]],
        worker_panes.StoppedRunPassCompletion | worker_panes.Kept | worker_panes.NoOwningPanePass],
        import_module("scripts.herdr.turn_end").stop_pass)
    result = stop_pass(run, record, guard_ended)
    if isinstance(result, worker_panes.StoppedRunPassCompletion):
        if result.send_notice() is worker_panes.StoppedPassCleanup.RETRY:
            return CompletionReplay.RETRY
        return _save_delivery(path, f"finished: the pane pass {result.outcome}")
    if isinstance(result, worker_panes.Kept):
        print(f"detached_runs: {record['run_id']}: kept the pane pass: {result.blocker}", file=sys.stderr)
    if record.get("orphan_result_superseded"):
        return _save_delivery(path, SUPERSEDED_ORPHAN_DELIVERY)
    return deliver(record, command_result(record))


def _clear_completion_pending(path: Path) -> None:
    try:
        lock = path.with_suffix(".record.lock").open("r")
    except FileNotFoundError:
        return
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = worker_lifecycle.read_json_object(path)
        if (_is_run_record(record) and path.name == f"{record['run_id']}.json"
                and record.get("completion_pending") is True):
            record["completion_pending"] = False
            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))


def acknowledge_worker_completion(run: Path, row: staff.StaffRow, passed: dict[str, object]) -> CompletionReplay:
    """Clear covered result flags after the worker pane closes."""
    instance = passed.get("instance_id")
    started = passed.get("started_at")
    if not isinstance(instance, str) or not instance or not isinstance(started, (int, float)):
        return CompletionReplay.HANDLED
    runs = run / DETACHED_RUNS_DIR
    completion = CompletionReplay.HANDLED
    evidence: ResultDoneEvidence | ResultTranscriptUnknown | Literal[ResultDoneCoverage.NEEDS_DELIVERY] | None = None
    for record in _registered_runs(runs, row.id):
        delivery = record.get("delivery", "")
        if (record["staff_id"] != row.id or record["status"] != "finished"
                or record.get("completion_pending") is not True or not delivery):
            continue
        if result_is_from_another_pass(record, instance):
            if (record.get("self_stop") and record.get("stop_pass_requested")
                    and delivery.startswith(DEFERRED_WORKER_RESULT)
                    and _save_delivery(runs / f"{record['run_id']}.json", OTHER_PASS_DELIVERY,
                                       if_current=delivery) == OTHER_PASS_DELIVERY):
                _clear_completion_pending(runs / f"{record['run_id']}.json")
            continue
        if not (record.get("pane_dispatch_id") == instance
                or "pane_dispatch_id" not in record and record["started_at"] >= started):
            continue
        if _worker_result_pending(delivery):
            if evidence is None:
                read_evidence = cast(Callable[[Path, staff.StaffRow, dict[str, object]],
                    ResultDoneEvidence | ResultTranscriptUnknown | Literal[ResultDoneCoverage.NEEDS_DELIVERY]],
                    import_module("scripts.production.worker").read_result_done_evidence)
                evidence = read_evidence(run, row, passed)
            if isinstance(evidence, ResultTranscriptUnknown):
                completion = CompletionReplay.RETRY
                continue
            if evidence is ResultDoneCoverage.NEEDS_DELIVERY:
                continue
            coverage = evidence.covers_run(record, run_finish_time(record))
            if isinstance(coverage, ResultTranscriptUnknown):
                completion = CompletionReplay.RETRY
            if coverage != ResultDoneCoverage.COVERED:
                continue
            if not _result_push_failed(delivery):
                path = runs / f"{record['run_id']}.json"
                if _save_delivery(path, COVERED_BY_DONE, if_current=delivery) != COVERED_BY_DONE:
                    continue
        _clear_completion_pending(runs / f"{record['run_id']}.json")
    return completion


def _result_pass_changed(record: RunRecord) -> bool:
    """Check whether a different pass is current."""
    from scripts.herdr import worker_panes

    linked = record.get("pane_dispatch_id")
    if not isinstance(linked, str) or not linked:
        return False
    membership = staff.local_snapshot()
    member = _run_owner(record, membership)
    if not isinstance(member, staff.StaffRow) or member.kind != "worker":
        return False
    run = worker_panes.worker_run(member, record=membership)
    passed = worker_panes.worker_pass(member, run, record=membership)
    return result_is_from_another_pass(record, passed.get("instance_id"))


def _complete_worker_dispatch(record: RunRecord) -> CompletionReplay:
    """Recheck the pass after the finished record becomes visible."""
    if not record["staff_id"] or _result_pass_changed(record):
        return CompletionReplay.HANDLED
    member = _run_owner(record, staff.local_snapshot())
    if isinstance(member, staff.StaffRow) and member.kind == "worker":
        module = import_module("scripts.herdr.turn_end")
        link = record.get("pane_dispatch_id", "")
        if isinstance(link, str) and link:
            return cast(Callable[[staff.StaffId, str], CompletionReplay], module.run_finished)(member.id, link)
        return cast(Callable[[staff.StaffId], CompletionReplay], module.run_finished)(member.id)
    return CompletionReplay.HANDLED


# Keep the record lock at its path until the record is gone.
_RUN_FILES = ("{}.log", "{}.completion.lock", ".{}.delivery.lock", "{}.stop-staged",
              "{}" + CONFIRMED_STOP_SUFFIX, "{}" + PENDING_STOP_SUFFIX,
              "{}" + process_stop.RUN_SLICE_RECORD_SUFFIX, "{}.json", "{}.record.lock", "{}.scope-stop.lock")


def prune_finished(runs: Path) -> list[str]:
    """Delete the folder's finished runs that no reader needs; the user keeps no old run files."""
    if not runs.is_dir():
        return []
    with (runs / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        return _prune_finished_locked(runs)


def _prune_finished_locked(runs: Path) -> list[str]:
    """Delete settled finished runs while the caller holds the admission lock.

    Keep a worker result until the saved stage and reply prove completion.
    Keep another owner's result until that owner starts again.
    Keep stop notice evidence until the caller acknowledges the notice ID.
    """
    from scripts.build_hold.override_guard import SharedStateWriteRefused
    from scripts.direct import progress_history
    from scripts.herdr import worker_panes

    membership = staff.local_snapshot()
    records = [(path, raw) for path in sorted(runs.glob("*.json"))
               if (raw := worker_lifecycle.read_json_object(path)).get("run_id")]
    newest: dict[str, float] = {}
    for _, raw in records:
        owner = str(raw.get("worker", ""))
        newest[owner] = max(newest.get(owner, 0.0), _number(raw.get("started_at"), 0.0))
    passes: dict[staff.StaffId, dict[str, object]] = {}
    settled: dict[staff.StaffId, bool] = {}
    removed: list[str] = []
    for path, raw in records:
        run_id = raw.get("run_id")
        if (raw.get("status") != "finished" or raw.get("completion_pending")
                or _worker_result_pending(str(raw.get("delivery", "")))
                or not isinstance(run_id, str) or path.name != f"{run_id}.json"):
            continue
        started = _number(raw.get("started_at"), 0.0)
        staff_id = str(raw.get("staff_id", ""))
        member = membership.by_id(staff.StaffId(staff_id)) if staff_id else None
        if member is not None and member.kind == "worker":
            if member.id not in passes:
                passes[member.id] = worker_panes.worker_pass(member, runs.parent, record=membership)
            passed = passes[member.id]
            if passed and started >= _number(passed.get("started_at"), 0.0):
                if member.id not in settled:
                    completion = progress_history.hook_completion(passed)
                    settled[member.id] = (
                        passed.get("status") == "completed"
                        and (not isinstance(completion, progress_history.HookCompletion) or completion.pane_closed)
                        and isinstance(worker_panes.recorded_completion_close(member, runs.parent, passed),
                                       worker_panes.CloseAllowed))
                if not settled[member.id]:
                    continue
        elif (member is not None or not staff_id) and started >= newest[str(raw.get("worker", ""))]:
            continue
        with record_lock(path):
            current = worker_lifecycle.read_json_object(path)
            if (current.get("run_id") != run_id or current.get("status") != "finished"
                    or current.get("stop_notice_id") or current.get("completion_pending")
                    or _worker_result_pending(str(current.get("delivery", "")))):
                continue
            if _has_saved_cgroup(current) and not _command_has_ended(current, _integer(current.get("pid"))):
                continue
            if _is_run_record(current):
                record = _with_confirmed_stop(path, current)
                if requests := record.get("hold_stop_requests"):
                    try:
                        _preserve_hold_stop_confirmations(requests)
                    except (OSError, SharedStateWriteRefused) as error:
                        print(f"detached_runs: kept {path}: could not copy hold stop confirmations: {error}",
                              file=sys.stderr)
                        continue
            for name in _RUN_FILES:
                (runs / name.format(run_id)).unlink(missing_ok=True)
        removed.append(run_id)
    return removed


def _preserve_hold_stop_confirmations(request_ids: Sequence[str]) -> None:
    """Copy confirmed signals before the runner deletes their source record."""
    from scripts.build_hold import override_guard, shared_state_defaults

    # The enforcer takes the release lock before the record lock.
    # Each marker has a unique request ID and can record the same signal twice.
    for request_id in request_ids:
        if shared_state_defaults.read_stop_confirmation(request_id) == "stopped":
            continue
        path = shared_state_defaults.stop_confirmation_path(request_id)
        override_guard.require_hold_write(path)
        shared_state_defaults.write_stop_confirmation(path, "stopped")


def _run_record_paths(runs: Path) -> list[Path]:
    """List run records; only an absent folder counts as empty."""
    try:
        with os.scandir(runs) as entries:
            return sorted(runs / entry.name for entry in entries if entry.name.endswith(".json"))
    except FileNotFoundError:
        return []


def _user_run_slice_directory() -> Path:
    uid = os.getuid()
    return (process_stop.CGROUP_ROOT / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
            / "builds.slice" / "builds-detached.slice")


def _run_candidates(runs: Path, *, scan_records: bool, slice_units: Collection[str] = ()) -> set[Path]:
    """Find records that the reader must check."""
    paths: set[Path] = set()
    try:
        with os.scandir(runs) as entries:
            for entry in entries:
                if entry.name.endswith(_RUN_INDEX_SUFFIXES):
                    paths.add(Path(entry.path).with_suffix(".json"))
                elif entry.name.endswith(".json") and (scan_records or
                        (slice_units and _command_slice_unit(entry.name.removesuffix(".json")) in slice_units)):
                    paths.add(Path(entry.path))
    except FileNotFoundError:
        pass
    return paths


def live_twin(runs: Path, identity: staff.StaffId, command: list[str], tree_key: str) -> RunRecord | None:
    """A pending run for the same worker, command, folder and tree key.
    After an interrupt on 2026-10-21, hana_catalyst-inspectors started its lint run three times
    on one tree; each run took memory, the cargo token and build time."""
    if not identity or not tree_key:
        return None
    for path in _run_record_paths(runs):
        raw = worker_lifecycle.read_json_object(path)
        if (raw.get("staff_id") == identity and raw.get("command") == command and raw.get("tree_key") == tree_key
                and _command_can_still_run(path, raw)):
            return cast(RunRecord, cast(object, raw))
    return None


def _command_can_still_run(path: Path, raw: dict[str, object]) -> bool:
    """Protect a duplicate start while the command can still run."""
    if raw.get("status") != "running":
        return False
    pid = _integer(raw.get("pid"))
    return (_run_is_pending(path, raw, lambda owner: detached_run_process.supervises(owner, path))
            or (pid > 0 and not _command_has_ended(raw, pid)))


def _ambiguous_legacy_twin(runs: Path, name: str, command: list[str], tree_key: str) -> Path | None:
    if not tree_key:
        return None
    for path in _run_record_paths(runs):
        raw = worker_lifecycle.read_json_object(path)
        if (not raw.get("staff_id") and raw.get("worker") == name and raw.get("command") == command
                and raw.get("tree_key") == tree_key and _command_can_still_run(path, raw)):
            return path
    return None


def worker_runs(runs: Path, worker: str, *, since: float = 0) -> list[RunRecord]:
    """Read this worker's runs that started during the selected dispatch."""
    found: list[RunRecord] = []
    for path in _run_record_paths(runs):
        raw = worker_lifecycle.read_json_object(path)
        if raw.get("worker") == worker and _number(raw.get("started_at"), 0) >= since:
            found.append(cast(RunRecord, cast(object, raw)))
    return found


def _record_owner(path: Path) -> str:
    """Read the fixed staff id from a new run name."""
    return path.stem.partition("@")[2]


def _worker_run_objects(runs: Path, identity: staff.StaffId) -> Generator[tuple[Path, dict[str, object]]]:
    for path in _run_record_paths(runs):
        if (owner := _record_owner(path)) and owner != identity:
            continue
        yield path, worker_lifecycle.read_json_object(path)


def _registered_runs(runs: Path, identity: staff.StaffId) -> Generator[RunRecord]:
    for path, record in _worker_run_objects(runs, identity):
        if _is_run_record(record) and path.name == f"{record['run_id']}.json":
            yield record


class DeliveryTimeUnknown(Enum):
    UNRECORDED = "the run does not record when the worker received the result"


class WorkerResultDeferred(Enum):
    PENDING = "the run still needs a finished result or delivery"
    REAP_REQUIRED = "reap must record the run result"


class WorkerResultDeliverySuppressed(Enum):
    SUCCESS_ONLY = "the worker requested pane delivery only on success"


class RunFinishTimeUnknown(Enum):
    UNRECORDED = "the run has no recorded finish time"


@dataclass(frozen=True)
class RunFinishedBefore:
    exclusive_bound: float


class WorkerResultCovered(Enum):
    BY_DONE = "the pass DONE covers the run result"


class ResultDoneCoverage(StrEnum):
    COVERED = "DONE covers the result"
    NEEDS_DELIVERY = "DONE does not cover the result"
    NO_MATCHING_DONE = "the pass has no matching DONE"


@dataclass(frozen=True)
class ResultTranscriptUnknown:
    reason: str


class ResultDoneEvidence(Protocol):
    """Use one selected DONE to check each eligible result."""

    def covers_run(self, record: RunRecord, finished_at: float | RunFinishedBefore | RunFinishTimeUnknown,
                   ) -> ResultDoneCoverage | ResultTranscriptUnknown: ...


def run_finish_time(record: RunRecord) -> float | RunFinishedBefore | RunFinishTimeUnknown:
    """Read the finish time without assuming precision in old records."""
    value = record.get("finished_at", "")
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return RunFinishTimeUnknown.UNRECORDED
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    # Old records truncate to seconds.
    return at.timestamp() if value[19:20] == "." else RunFinishedBefore(at.timestamp() + 1)


@dataclass(frozen=True)
class WorkerRunReceipt:
    run: RunRecord
    finished_at: float | RunFinishedBefore | RunFinishTimeUnknown
    delivered_at: float | DeliveryTimeUnknown | WorkerResultDeferred | WorkerResultCovered | WorkerResultDeliverySuppressed
    requires_done_after_finish: bool = False


DEFERRED_WORKER_RESULT = "deferred: "
ORPHAN_RESULT_PENDING = DEFERRED_WORKER_RESULT + "the supervisor ended before delivery"
PARKED_WORKER_RESULT = DEFERRED_WORKER_RESULT + "the worker is parked"
FAILED_WORKER_SUBMISSION = "submission failed: "
UNCONFIRMED_WORKER_RESULT = "unconfirmed: "
WORKER_INPUT_CHECKPOINT = UNCONFIRMED_WORKER_RESULT + "the prompt may have sent input"
FAILED_RESULT_PUSH = "result push failed: "
UNCONFIRMED_RESULT_PUSH = UNCONFIRMED_WORKER_RESULT + "result push: "


def _worker_result_pending(delivery: str) -> bool:
    return delivery.startswith((DEFERRED_WORKER_RESULT, FAILED_WORKER_SUBMISSION, UNCONFIRMED_WORKER_RESULT,
                                FAILED_RESULT_PUSH))


def _result_push_failed(delivery: str) -> bool:
    return delivery.startswith((FAILED_RESULT_PUSH, FAILED_WORKER_SUBMISSION, UNCONFIRMED_WORKER_RESULT))


def _needs_push_retry(record: RunRecord) -> bool:
    return (record["status"] == "finished" and not record.get("push_alert_attempted")
            and _result_push_failed(record.get("delivery", "")))


def _unconfirmed_pass_result(record: RunRecord) -> bool:
    link = record.get("pane_dispatch_id")
    return (isinstance(link, str) and bool(link)
            and record.get("delivery", "").startswith(UNCONFIRMED_WORKER_RESULT))


def _needs_completion(record: RunRecord) -> bool:
    return ((not _result_push_failed(record.get("delivery", "")) or _unconfirmed_pass_result(record))
            and bool(record.get("completion_pending") or _retired_worker_result(record)))


def _stop_fallback_delivered(record: RunRecord, delivery: str) -> bool:
    return (_cancels_pass(record) and record.get("pane_dispatch_id") != ""
            and delivery.startswith(("sent: ", "queued: ", "skipped: ", "already delivered: ")))


def result_is_from_another_pass(record: Mapping[str, object], instance: object) -> bool:
    """Require two known pass IDs before rejecting a result."""
    linked = record.get("pane_dispatch_id")
    return (isinstance(linked, str) and bool(linked)
            and isinstance(instance, str) and bool(instance) and linked != instance)


class ResultPassOwnership(Enum):
    CURRENT = "the result belongs to the current pass"
    OTHER_PASS = "the result belongs to another pass or run folder"
    OUTSIDE_PASS = "the result does not belong to a pass"
    UNKNOWN = "the result's pass ownership is unknown"


def result_pass_ownership(record: RunRecord, worker: staff.StaffRow, run: Path,
                          passed: dict[str, object]) -> ResultPassOwnership:
    """Use the same ownership decision for submission and restore."""
    if record["staff_id"] != worker.id or Path(record["session_dir"]).resolve() != run.resolve():
        return ResultPassOwnership.OTHER_PASS
    instance = passed.get("instance_id")
    linked = record.get("pane_dispatch_id")
    if isinstance(linked, str):
        if linked == "":
            return ResultPassOwnership.OUTSIDE_PASS
        if isinstance(instance, str) and instance:
            return ResultPassOwnership.CURRENT if linked == instance else ResultPassOwnership.OTHER_PASS
        return ResultPassOwnership.UNKNOWN
    if "pane_dispatch_id" in record:
        return ResultPassOwnership.UNKNOWN
    active = passed.get("status") in ("active", "refused")
    if not instance and not active:
        return ResultPassOwnership.OUTSIDE_PASS
    started = passed.get("started_at")
    if (isinstance(instance, str) and instance and active
            and isinstance(started, (int, float)) and not isinstance(started, bool)
            and 0 <= started <= record["started_at"] < math.inf):
        return ResultPassOwnership.CURRENT
    return ResultPassOwnership.UNKNOWN


@dataclass(frozen=True)
class ResultSubmissionPass:
    """The assignment and stage checked before input."""

    instance: str
    completion: str

    def __post_init__(self) -> None:
        if not self.instance or not self.completion:
            raise ValueError("result receipt requires a pass and completion id")


class UnknownResultStage(Enum):
    UNKNOWN = "the worker stage is unknown"


class ResultWithoutPass(Enum):
    OUTSIDE = "the result was submitted outside a pass"


def result_submission_pass(passed: dict[str, object]) -> ResultSubmissionPass | ResultWithoutPass | UnknownResultStage:
    from scripts.direct import pass_record

    instance = passed.get("instance_id")
    if not isinstance(instance, str) or not instance:
        return ResultWithoutPass.OUTSIDE
    completion = pass_record.dispatch_delivery(passed).completion_id
    return ResultSubmissionPass(instance, completion) if completion else UnknownResultStage.UNKNOWN


class ParkedAssignmentAcceptance(Enum):
    VERIFIED = "the park record proves assignment acceptance"
    UNRECORDED = "check the assignment transcript"


class MissingWorkerResultReceipt(Enum):
    ABSENT = "the record has no receipt"
    MALFORMED = "the record has an invalid receipt"


@dataclass(frozen=True)
class WorkerResultReceipt:
    """A detached result input confirmed in the selected worker transcript."""

    run_id: str
    session_dir: Path
    worker: staff.StaffId
    session: staff.StaffId
    pane: str
    terminal: str
    assignment: ResultSubmissionPass | ResultWithoutPass
    cursor: worker_panes.TranscriptCursor
    input_digest: str
    park_acceptance: ParkedAssignmentAcceptance

    def to_json(self) -> dict[str, object]:
        assignment = self.assignment
        return {
            "run_id": self.run_id, "session_dir": str(self.session_dir),
            "worker": self.worker, "session": self.session, "pane": self.pane, "terminal": self.terminal,
            "pass_id": assignment.instance if isinstance(assignment, ResultSubmissionPass) else "",
            "completion_id": assignment.completion if isinstance(assignment, ResultSubmissionPass) else "",
            "cursor": asdict(self.cursor), "input_digest": self.input_digest,
            "park_accepted": self.park_acceptance is ParkedAssignmentAcceptance.VERIFIED,
        }

    def identifies(self, record: RunRecord, worker: staff.StaffRow, run: Path) -> bool:
        link = record.get("pane_dispatch_id")
        instance = self.assignment.instance if isinstance(self.assignment, ResultSubmissionPass) else ""
        return (
            self.run_id == record["run_id"] and self.worker == record["staff_id"] == worker.id
            and self.session_dir == Path(record["session_dir"]).resolve() == run.resolve()
            and self.session == worker.current_session
            and ("pane_dispatch_id" not in record or link == instance)
        )


def read_worker_result_receipt(record: Mapping[str, object]) -> WorkerResultReceipt | MissingWorkerResultReceipt:
    from scripts.herdr import worker_panes

    if "worker_result_receipt" not in record:
        return MissingWorkerResultReceipt.ABSENT
    raw = record["worker_result_receipt"]
    if not isinstance(raw, dict):
        return MissingWorkerResultReceipt.MALFORMED
    match cast(dict[str, object], raw):
        case {
            "run_id": str(run_id), "session_dir": str(directory), "worker": str(worker),
            "session": str(session), "pane": str(pane), "terminal": str(terminal), "pass_id": str(instance),
            "completion_id": str(completion), "cursor": cursor_fields, "input_digest": str(digest),
            "park_accepted": bool(parked),
        } if all((run_id, directory, worker, session, pane, terminal)) and bool(instance) == bool(completion) and re.fullmatch(r"[a-f0-9]{64}", digest):
            cursor = worker_panes.TranscriptCursor.from_json(cursor_fields)
            if isinstance(cursor, worker_panes.TranscriptCursor) and Path(directory).is_absolute():
                assignment = ResultSubmissionPass(instance, completion) if instance else ResultWithoutPass.OUTSIDE
                acceptance = ParkedAssignmentAcceptance.VERIFIED if parked else ParkedAssignmentAcceptance.UNRECORDED
                return WorkerResultReceipt(run_id, Path(directory), staff.StaffId(worker),
                                           staff.StaffId(session), pane, terminal, assignment, cursor, digest, acceptance)
        case _:
            pass
    return MissingWorkerResultReceipt.MALFORMED


class NoCurrentResultDelivery(Enum):
    NONE = "no relevant result delivery"


@dataclass(frozen=True)
class UncertainResultDelivery:
    reason: str


def restore_result_evidence(
    run: Path, pane: worker_panes.OpenWorker | worker_panes.ClosedWorker | worker_panes.StoppedWorker,
    passed: dict[str, object], membership: staff.StaffSnapshot,
) -> WorkerResultReceipt | UncertainResultDelivery | NoCurrentResultDelivery:
    """Read current result evidence while restore holds the worker lock."""
    from scripts.herdr import herdr_types, worker_panes
    from scripts.message import send

    instance = passed.get("instance_id")
    if not isinstance(instance, str) or not instance or passed.get("status") not in ("active", "refused"):
        return NoCurrentResultDelivery.NONE
    current = result_submission_pass(passed)
    worker = pane.row
    confirmed: WorkerResultReceipt | NoCurrentResultDelivery = NoCurrentResultDelivery.NONE
    for path, record in _worker_run_objects(run / DETACHED_RUNS_DIR, worker.id):
        receipt = read_worker_result_receipt(record)
        if (record.get("staff_id") != worker.id and _record_owner(path) != worker.id
                and not (isinstance(receipt, WorkerResultReceipt) and receipt.worker == worker.id)):
            continue
        delivery = record.get("delivery", "")
        link = record.get("pane_dispatch_id")
        claims_input = (receipt is not MissingWorkerResultReceipt.ABSENT
                        or isinstance(delivery, str) and delivery.startswith(
                            ("sent: worker ", "already delivered: ", UNCONFIRMED_WORKER_RESULT)))
        uncertain = UncertainResultDelivery(f"Detached run {path.stem}: the result delivery is uncertain")
        if not _is_run_record(record):
            if claims_input and (not isinstance(link, str) or link == instance
                                 or isinstance(receipt, WorkerResultReceipt)
                                 and isinstance(receipt.assignment, ResultSubmissionPass)
                                 and receipt.assignment.instance == instance):
                return uncertain
            continue
        if record["status"] != "finished":
            continue
        delivery = record.get("delivery", "")
        saved_pass = receipt.assignment if isinstance(receipt, WorkerResultReceipt) else ResultWithoutPass.OUTSIDE
        if isinstance(link, str) and link != instance:
            linked_to_saved_pass = link == (saved_pass.instance if isinstance(saved_pass, ResultSubmissionPass) else "")
            if linked_to_saved_pass or not isinstance(receipt, WorkerResultReceipt):
                continue
        if isinstance(receipt, WorkerResultReceipt):
            if "pane_dispatch_id" not in record and (
                not isinstance(saved_pass, ResultSubmissionPass) or saved_pass.instance != instance
            ):
                continue
            if (isinstance(current, ResultSubmissionPass) and isinstance(saved_pass, ResultSubmissionPass)
                    and saved_pass.instance == instance and saved_pass != current):
                continue
        has_pane = isinstance(pane, (worker_panes.OpenWorker, worker_panes.StoppedWorker))
        if (isinstance(receipt, WorkerResultReceipt) and saved_pass == current
                and path.name == f"{record['run_id']}.json" and receipt.identifies(record, worker, run)
                and delivery.startswith("sent: worker ")):
            try:
                same_input = send.worker_result_input_matches(worker, membership, receipt.run_id,
                                                              receipt.cursor, receipt.input_digest)
                if same_input and (not has_pane or (receipt.pane, receipt.terminal) == (pane.pane, pane.terminal)):
                    confirmed = receipt
                    continue
            except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure):
                pass
        ownership = result_pass_ownership(record, worker, run, passed)
        if (claims_input or ownership is ResultPassOwnership.UNKNOWN and (
                _worker_result_pending(delivery) or not delivery and record.get("completion_pending"))):
            return uncertain
    return confirmed


def worker_results(runs: Path, worker: staff.StaffRow, *, since: float, pane_dispatch_id: str = "",
                   include_legacy_pass: bool = False,
                   ) -> tuple[dict[str, WorkerRunReceipt], set[str]]:
    """Find results and pending recoveries for this pass."""
    results: dict[str, WorkerRunReceipt] = {}
    other_passes: set[str] = set()
    for record in _registered_runs(runs, worker.id):
        if record.get("staff_id") != worker.id:
            continue
        if result_is_from_another_pass(record, pane_dispatch_id):
            other_passes.add(record["run_id"])
            continue
        link = record.get("pane_dispatch_id")
        linked = bool(pane_dispatch_id) and link == pane_dispatch_id
        legacy = include_legacy_pass and not isinstance(link, str) and record["started_at"] >= since
        if not (linked or legacy):
            continue
        if record["status"] == "running":
            path = runs / f"{record['run_id']}.json"
            if not _run_is_pending(path, dict(record), lambda pid: detached_run_process.supervises(pid, path)):
                results[record["run_id"]] = WorkerRunReceipt(
                    record, RunFinishTimeUnknown.UNRECORDED, WorkerResultDeferred.REAP_REQUIRED)
            continue
        finished = run_finish_time(record)
        delivery = record.get("delivery", "")
        stop_fallback = _stop_fallback_delivered(record, delivery)
        if delivery == COVERED_BY_DONE:
            results[record["run_id"]] = WorkerRunReceipt(record, finished, WorkerResultCovered.BY_DONE)
        elif not delivery or _worker_result_pending(delivery):
            results[record["run_id"]] = WorkerRunReceipt(record, finished, WorkerResultDeferred.PENDING)
        elif record["done_message_file"] and delivery == _success_only_delivery(command_result(record)):
            results[record["run_id"]] = WorkerRunReceipt(record, finished, WorkerResultDeliverySuppressed.SUCCESS_ONLY)
        elif stop_fallback or delivery.startswith((
                f"sent: worker {record['worker']} at ", f"sent: worker {worker.name} at ", "already delivered: ")):
            results[record["run_id"]] = WorkerRunReceipt(
                record, finished, record.get("delivered_at", DeliveryTimeUnknown.UNRECORDED),
                requires_done_after_finish=stop_fallback)
    return results, other_passes


@dataclass(frozen=True)
class LiveRunReadError:
    path: Path
    error: OSError | ValueError
    staff_ids: frozenset[staff.StaffId] = frozenset()

    @override
    def __str__(self) -> str:
        return f"{self.path}: {self.error}"


@dataclass(frozen=True)
class LiveRunEvidence:
    runs: tuple[RunRecord, ...]
    read_errors: tuple[LiveRunReadError, ...]

    def require_readable(self) -> list[RunRecord]:
        if self.read_errors:
            raise self.read_errors[0].error
        return list(self.runs)

    def report_errors(self) -> None:
        for error in self.read_errors:
            unit_runs.report_anomaly(" ".join(f"anomaly: {error}".splitlines()))


def live_runs(runs: Path, worker: str, *, identity: str = "") -> list[RunRecord]:
    """Return runs that still block completion."""
    def owns(raw: dict[str, object]) -> bool:
        return raw.get("staff_id") == identity if identity and raw.get("staff_id") else raw.get("worker") == worker

    evidence = _live_runs(runs, owns, frozenset({staff.StaffId(identity)}) if identity else frozenset())
    return evidence.require_readable()


@contextlib.contextmanager
def prepared_run_admission(run: Path) -> Generator[bool]:
    """Exclude detached starts while a prepared run's pointer is cleared."""
    runs = run / DETACHED_RUNS_DIR
    runs.mkdir(exist_ok=True)
    directory = os.open(runs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(".start.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o666, dir_fd=directory)
        with os.fdopen(descriptor, "a") as admission:
            try:
                fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
    finally:
        os.close(directory)


def run_execution_blocker(run: Path, *, admission_lock_held: bool = False) -> unit_runs.RunExecutionState:
    """Read command execution after dispatch admission ends."""
    runs = run / DETACHED_RUNS_DIR
    if admission_lock_held:
        return _detached_execution_blocker(runs)
    try:
        with (runs / ".start.lock").open("r") as admission:
            try:
                fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return unit_runs.RunExecutionBlocker("a detached command is starting")
            return _detached_execution_blocker(runs)
    except FileNotFoundError:
        return _detached_execution_blocker(runs)


def _detached_execution_blocker(runs: Path) -> unit_runs.RunExecutionBlocker | unit_runs.NoRunExecutionBlocker:
    for path in _run_record_paths(runs):
        raw = worker_lifecycle.read_json_object(path)
        if not raw and not path.exists():
            continue
        status = raw.get("status")
        if status not in ("running", "finished"):
            return unit_runs.RunExecutionBlocker(f"{path.name}: the detached command state is unavailable")
        pid = _integer(raw.get("pid"))
        if ((status == "running" and not _orphaned(path, raw, time.time()))
                or (raw.get("stop_pass_requested") and not _command_has_ended(raw, pid))):
            return unit_runs.RunExecutionBlocker(f"detached run {path.stem} can still be running")
    return unit_runs.NoRunExecutionBlocker.IDLE


def live_runs_for_staff(runs: Path, identities: frozenset[staff.StaffId]) -> list[RunRecord]:
    """Read live records for fixed staff identities, including renamed members."""
    evidence = live_run_evidence_for_staff(runs, identities)
    return evidence.require_readable()


def live_run_evidence_for_staff(runs: Path, identities: frozenset[staff.StaffId]) -> LiveRunEvidence:
    """Retain live runs and read errors so callers can distinguish idle from unknown."""
    return _live_runs(runs, lambda raw: isinstance(identity := raw.get("staff_id"), str) and identity in identities,
                      identities)


def live_runs_in_folders(folders: Mapping[Path, frozenset[staff.StaffId]]) -> list[RunRecord]:
    """Read runs that still block completion in the selected folders."""
    return live_run_evidence_in_folders(folders).require_readable()


def live_run_evidence_in_folders(folders: Mapping[Path, frozenset[staff.StaffId]], *,
                                 staff_names: Mapping[staff.StaffId, str] | None = None) -> LiveRunEvidence:
    """Read each live record once and identify the workers affected by read errors."""
    if not folders:
        return LiveRunEvidence((), ())
    supervisors = detached_run_process.supervisor_records()
    named: dict[Path, set[Path]] = {}
    found: list[RunRecord] = []
    errors: list[LiveRunReadError] = []
    slice_units: set[str] = set()
    if not isinstance(supervisors, detached_run_process.ProcessTableUnavailable):
        for path in supervisors:
            if path.parent.name == DETACHED_RUNS_DIR and path.parent.parent in folders:
                named.setdefault(path.parent.parent, set()).add(path)
    if sys.platform == "linux" and not isinstance(supervisors, detached_run_process.ProcessTableUnavailable):
        # Older launchers saved the slice identity before they wrote an index.
        directory = _user_run_slice_directory()
        try:
            with os.scandir(directory) as entries:
                slice_units = {entry.name for entry in entries if entry.name.endswith(".slice")}
        except FileNotFoundError:
            pass
        except OSError as error:
            affected = frozenset(member for identities in folders.values() for member in identities)
            errors.append(LiveRunReadError(directory, error, affected))
    for folder, identities in folders.items():
        directory = folder / DETACHED_RUNS_DIR
        try:
            paths = named.get(folder, set()) | _run_candidates(
                directory, scan_records=isinstance(supervisors, detached_run_process.ProcessTableUnavailable),
                slice_units=slice_units)
            evidence = _read_live_runs(
                sorted(paths), lambda raw: isinstance(identity := raw.get("staff_id"), str) and identity in identities,
                detached_run_process.supervises if isinstance(supervisors, detached_run_process.ProcessTableUnavailable)
                else lambda pid, path: pid in supervisors.get(path, ()),
                identities, staff_names=staff_names, supervised_paths=named.get(folder, set()))
        except OSError as error:
            errors.append(LiveRunReadError(directory, error, identities))
            continue
        found.extend(evidence.runs)
        errors.extend(evidence.read_errors)
    return LiveRunEvidence(tuple(found), tuple(errors))


if TYPE_CHECKING:
    _accounts_run_evidence: Callable[..., RunEvidence] = live_run_evidence_in_folders


def _recover_indexed_stop(path: Path, raw: dict[str, object]) -> None:
    if raw.get("stop_pass_requested") is not True and path.with_suffix(PENDING_STOP_SUFFIX).exists():
        raw.update(_read_stop_fields(path.with_suffix(CONFIRMED_STOP_SUFFIX), raw.get("run_id")))


def _run_is_pending(path: Path, raw: dict[str, object], supervisor_owns: Callable[[int], bool]) -> bool:
    pid = _integer(raw.get("pid"))
    # A saved slice stays pending until its processes end, also after the record finishes.
    if _has_saved_cgroup(raw) and not _command_has_ended(raw, pid):
        return True
    if raw.get("status") != "running":
        return False
    if supervisor_owns(pid):
        return True
    _recover_indexed_stop(path, raw)
    return raw.get("stop_pass_requested") is True and not _command_has_ended(raw, pid)


def _live_runs(runs: Path, owns: Callable[[dict[str, object]], bool],
               identities: frozenset[staff.StaffId]) -> LiveRunEvidence:
    try:
        paths = _run_candidates(runs, scan_records=True)
    except OSError as error:
        return LiveRunEvidence((), (LiveRunReadError(runs, error, identities),))
    return _read_live_runs(sorted(paths), owns, detached_run_process.supervises, identities)


def _read_live_runs(paths: Sequence[Path], owns: Callable[[dict[str, object]], bool],
                    supervises: Callable[[int, Path], bool], identities: frozenset[staff.StaffId], *,
                    staff_names: Mapping[staff.StaffId, str] | None = None,
                    supervised_paths: Collection[Path] = ()) -> LiveRunEvidence:
    found: list[RunRecord] = []
    errors: list[LiveRunReadError] = []
    for path in paths:
        identity = staff.StaffId(_record_owner(path))
        if identity and identities and identity not in identities:
            continue
        affected = frozenset({identity}) if identity else identities
        try:
            raw = worker_lifecycle.read_json_object(path)
            if not raw and not path.exists():
                if any(path.with_suffix(suffix).exists() for suffix in _RUN_INDEX_SUFFIXES):
                    raise worker_lifecycle.WorkerStateUnreadable(path, "the indexed run record is missing")
                if staff_names is not None and path in supervised_paths:
                    raise worker_lifecycle.WorkerStateUnreadable(path, "the live supervisor record is missing")
                continue
        except (OSError, ValueError) as error:
            errors.append(LiveRunReadError(path, error,
                                           identities if staff_names is not None and path in supervised_paths else affected))
            continue
        owner = raw.get("staff_id")
        name = raw.get("worker")
        legacy_owners: tuple[staff.StaffId, ...] = ()
        if staff_names is not None and not identity and owner in (None, "") and isinstance(name, str) and name:
            legacy_owners = tuple(member for member in identities if staff_names.get(member) == name)
            if not legacy_owners:
                continue
        if isinstance(owner, str) and owner and not identity:
            affected = frozenset({staff.StaffId(owner)})
        try:
            if staff_names is not None and (identity or not owner or not isinstance(owner, str) or owner in identities):
                if (not (isinstance(owner, str) and owner or legacy_owners)
                        or raw.get("status") not in ("running", "finished") or type(raw.get("pid")) is not int
                        or identity and (owner != identity or not _is_run_record(dict(raw)) or f"{raw.get('run_id')}.json" != path.name)):
                    raise worker_lifecycle.WorkerStateUnreadable(path, "invalid detached run record")
            if (legacy_owners or owns(raw)) and _run_is_pending(path, raw, lambda pid: supervises(pid, path)):
                if legacy_owners:
                    found.extend(cast(RunRecord, cast(object, {**raw, "staff_id": member})) for member in legacy_owners)
                else:
                    found.append(cast(RunRecord, cast(object, raw)))
        except (OSError, ValueError) as error:
            errors.append(LiveRunReadError(path, error, affected))
    return LiveRunEvidence(tuple(found), tuple(errors))


def resume_context(runs: Path, worker: str) -> str:
    """Name live runs and their persistent result files for a resumed worker."""
    lines: list[str] = []
    for record in live_runs(runs, worker):
        result = runs / f"{record['run_id']}.json"
        command = shlex.join(["cat", "--", str(result), record["log"]])
        lines.append(f"Detached run {record['run_id']} remains live. "
                     + f"Result: {result}; log: {record['log']}. Read it with: `{command}`. "
                     + "Its result follows this worker after the move.")
    return "\n".join(lines)


def _start_refusal(run: Path) -> str:
    path = run / unit_runs.STATE_FILE
    try:
        state = recorder_state.read_state(path)
    except FileNotFoundError:
        if path.exists(follow_symlinks=False):
            return "the recorder state is unavailable"
    except (OSError, ValueError, recorder_state.OldRecorderState) as error:
        return str(error)
    else:
        if unit_runs.terminal_run(state):
            return "the run has ended dispatch admission"
    return ""


SCOPE_REFUSAL = ("detached_runs: Start with systemd-run --scope or systemd-run --user --scope. "
                 + "Use the exact option names in that order.")


def _command_program_index(command: list[str]) -> int:
    index = 0
    while index < len(command) and Path(command[index]).name == "env":
        index += 1
        while index < len(command) and command[index] in ("-u", "--unset"):
            index += 2
        if index < len(command) and command[index].startswith("-"):
            break
        while index < len(command) and "=" in command[index]:
            index += 1
    return index


def _uses_unsupported_systemd_run(command: list[str]) -> bool:
    """Require an exact scope prefix when systemd-run is the program."""
    index = _command_program_index(command)
    if index < len(command) and command[index].startswith("-"):
        return any("systemd-run" in argument for argument in command[index:])
    return (index < len(command) and Path(command[index]).name == "systemd-run"
            and command[index + 1:index + 2] != ["--scope"]
            and command[index + 1:index + 3] != ["--user", "--scope"])


def _place_command_in_run_slice(command: list[str]) -> list[str]:
    """Keep a direct systemd-run scope wrapper inside the caller's run slice."""
    index = _command_program_index(command)
    if index >= len(command) or Path(command[index]).name != "systemd-run":
        return command
    args = command[index + 1:]
    if args[:1] == ["--scope"]:
        args = ["--user", *args]
    if args[:2] != ["--user", "--scope"]:
        return command
    return [*command[:index + 1], *args[:2], "--slice-inherit", *args[2:]]


def command_start(args: argparse.Namespace) -> int:
    command = [str(part) for part in cast(list[object], getattr(args, "command", []) or [])]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("detached_runs: start needs a command after --", file=sys.stderr)
        return 2
    if _uses_unsupported_systemd_run(command):
        print(SCOPE_REFUSAL, file=sys.stderr)
        return 2
    session_dir = str(getattr(args, "session_dir", "") or "")
    requested_name = str(getattr(args, "to", "") or "")
    environment = dict(os.environ)
    caller = caller_from_environment(environment)
    target: PaneRunSeat | DirectorCaller | UnregisteredRunSeat
    if isinstance(caller, PaneCaller):
        if requested_name and requested_name != caller.name:
            print(f"detached_runs: --to {requested_name} ignored; run belongs to worker {caller.name}", file=sys.stderr)
        session_dir = caller.session_dir
        target = PaneRunSeat(caller.name, caller.staff_id)
    elif isinstance(caller, DirectorCaller) and session_dir and not requested_name:
        target = caller
    else:
        if bool(session_dir) != bool(requested_name):
            print("detached_runs: --session-dir and --to go together", file=sys.stderr)
            return 2
        if not session_dir:
            return NOT_A_DETACHED_WORKER
        target = _run_seat(session_dir, requested_name)
        if isinstance(target, UnregisteredRunSeat):
            print(f"detached_runs: {target.name}: worker is not registered; started nothing", file=sys.stderr)
            return 2
    name = target.name
    identity = target.staff_id
    session_dir = str(Path(session_dir).resolve())
    label = str(getattr(args, "label", "") or "") or " ".join(command)
    kind = cast(DetachedRunKind, args.kind)
    done_file = str(getattr(args, "done_message_file", "") or "")
    director_dir = str(getattr(args, "director_dir", "") or "") or session_dir
    runs = _runs_dir(session_dir).resolve()
    runs.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    tree_key = treekey.tree_key(cwd) or ""
    reaped = OrphanReapResult()
    try:
        with contextlib.ExitStack() as locks:
            if isinstance(target, PaneRunSeat):
                from scripts.herdr import worker_panes

                locks.enter_context(worker_panes.worker_id_lock(target.staff_id, blocking=True))
                if isinstance(caller, PaneCaller):
                    current = caller_from_environment(environment)
                    unchanged = (isinstance(current, PaneCaller) and current.staff_id == identity
                                 and Path(current.session_dir).resolve() == Path(session_dir))
                else:
                    membership = staff.local_snapshot()
                    try:
                        owner = worker_admission.parent(membership, run=Path(session_dir))
                    except worker_admission.RunOwnerUnavailable as error:
                        print(f"detached_runs: {error}; started nothing", file=sys.stderr)
                        return 2
                    member = membership.by_id(identity)
                    unchanged = member is not None and member.kind == "worker" and member.reports_to(owner.id)
                if not unchanged:
                    print(f"detached_runs: {name}: worker changed before admission; started nothing", file=sys.stderr)
                    return 2
            admission = locks.enter_context((runs / ".start.lock").open("a"))
            fcntl.flock(admission, fcntl.LOCK_EX)
            if refusal := _start_refusal(Path(session_dir)):
                print(f"detached_runs: {session_dir}: {refusal}", file=sys.stderr)
                return 2
            reaped = _reap_orphans_locked(runs)
            if twin := live_twin(runs, identity, command, tree_key):
                started = time.strftime("%H:%M:%S", time.localtime(twin["started_at"]))
                print(f"run {twin['run_id']} is already running for {name} on these files (started {started}, "
                      + f"log {twin['log']}); started nothing; its result arrives as your next message")
                return 0
            if legacy := _ambiguous_legacy_twin(runs, name, command, tree_key):
                print(f"detached_runs: {legacy}: a matching run has no owner staff ID; started nothing", file=sys.stderr)
                return 2
            try:
                limit = conf.read("detached_runs").whole("live_run_limit")
            except conf.Unusable as error:
                print(f"detached_runs: {error}", file=sys.stderr)
                return 2
            active = live_runs(runs, name, identity=identity)
            if len(active) >= limit:
                details = "\n".join(f"  {run['run_id']}: {run['label']}" for run in active)
                print(f"detached_runs: live-run limit {limit} reached for {name}; started nothing.\n"
                      + f"Live runs:\n{details}\n"
                      + "Wait for a run's result; it arrives as your next message. "
                      + "Or put more files in one run.", file=sys.stderr)
                return LIVE_RUN_LIMIT_REACHED
            run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(2)}"
            if isinstance(target, PaneRunSeat):
                run_id += f"@{identity}"
            record_path = runs / f"{run_id}.json"
            log_path = runs / f"{run_id}.log"
            record: RunRecord = {
                "run_id": run_id, "status": "running", "session_dir": session_dir, "worker": name,
                "staff_id": identity, "label": label, "command": command,
                "kind": kind.value,
                "log": str(log_path), "started_at": time.time(), "pid": 0,
                "director_dir": director_dir, "done_message_file": done_file, "tree_key": tree_key,
            }
            from scripts.direct import progress_history

            if isinstance(target, DirectorCaller):
                record["pane_dispatch_id"] = ""
            else:
                dispatch = progress_history.active_dispatch(identity, Path(session_dir))
                if isinstance(dispatch, progress_history.ActiveDispatch):
                    record["pane_dispatch_id"] = dispatch.instance_id
                elif isinstance(dispatch, progress_history.NoActiveDispatch):
                    record["pane_dispatch_id"] = ""
                else:
                    record["pane_dispatch_id"] = {"unavailable": dispatch.reason}
            with record_lock(record_path):
                write_text_atomic(record_path, json.dumps(record, indent=2, sort_keys=True))
            with log_path.open("ab") as log:
                supervisor = subprocess.Popen(
                    [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "supervise", str(record_path)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, cwd=cwd,
                )
            record["pid"] = supervisor.pid
            _ = _write_record(record_path, record)
    finally:
        reaped.complete()
    _ = prune_finished(runs)
    print(f"run {run_id} started detached for {name} (pid {supervisor.pid}, log {log_path}); "
          + "its result arrives as your next message")
    return 0


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _cut_line(line: str) -> str:
    limit = conf.read("detached_runs").whole("result_line_chars")
    if len(line) <= limit:
        return line
    return f"{line[:limit]} [cut: {len(line) - limit} more characters]"


def _tail(path: Path, count: int, budget: int) -> list[str]:
    """Keep the latest lines within the message budget. Collapse repeated memory waits."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    collapsed: list[str] = []
    for line in lines:
        if (collapsed and line.startswith("still waiting for memory")
                and collapsed[-1].startswith("still waiting for memory")):
            collapsed[-1] = line
        else:
            collapsed.append(line)
    kept: list[str] = []
    total = 0
    for line in map(_cut_line, reversed(collapsed[-count:])):
        total += len(line) + 1
        if total > budget:
            break
        kept.append(line)
    return kept[::-1]


@dataclass(frozen=True)
class CompletedCommand:
    code: int


@dataclass(frozen=True)
class SupervisorLost:
    """The supervisor disappeared before it reported an exit status."""


CommandResult = CompletedCommand | SupervisorLost


def command_result(record: RunRecord) -> CommandResult:
    code = record.get("exit_code")
    return CompletedCommand(code) if isinstance(code, int) else SupervisorLost()


def _exit_words(result: CommandResult) -> str:
    return f"exit {result.code}" if isinstance(result, CompletedCommand) else "no exit status"


def _success_only_delivery(result: CommandResult) -> str:
    return f"nothing delivered: {_exit_words(result)} with a done message"


def result_text(record: RunRecord, result: CommandResult) -> str:
    exit_code = result.code if isinstance(result, CompletedCommand) else result
    stop_reason = record.get("stop_reason", "")
    if record.get("scope_error"):
        verdict = f"FAIL (cleanup; {_exit_words(result)})"
    elif stop_reason:
        verdict = f"STOPPED ({_exit_words(result)})"
    elif isinstance(exit_code, SupervisorLost):
        verdict = ORPHAN_VERDICT
    elif exit_code == 0:
        verdict = "PASS (exit 0)"
    elif exit_code == SUPERSEDED_STATUS:
        verdict = "SUPERSEDED (exit 76)"
    elif exit_code == TREE_CHANGED_STATUS:
        verdict = "CANCELLED (exit 77)"
    else:
        verdict = f"FAIL (exit {exit_code})"
    heading = "Run ended with no result" if isinstance(exit_code, SupervisorLost) else "Run finished"
    lines = [f"{heading}: ", f"Result: {verdict}", f"Log: {record['log']}"]
    if stop_reason:
        lines.append(f"Stop reason: {stop_reason}")
    if record.get("slice_leftovers"):
        lines.append("Cleanup stopped the processes that remained in the run slice.")
    if error := record.get("scope_error"):
        lines.append(f"Cleanup error: {error}")
    if exit_code == SUPERSEDED_STATUS:
        lines.append(SUPERSEDED_INSTRUCTION)
    elif exit_code == TREE_CHANGED_STATUS:
        lines.append(TREE_CHANGED_INSTRUCTION)
    cancellation = ""
    if exit_code == TREE_CHANGED_STATUS:
        # Keep the cancellation instruction when cleanup moves it outside the tail.
        try:
            with Path(record["log"]).open(encoding="utf-8", errors="replace") as log:
                cancellation = next((line.rstrip("\n") for line in log
                                     if line.startswith("verify.sh: cancelled — ")), "")
        except OSError:
            cancellation = ""
        if cancellation:
            lines.extend(["", cancellation])
    header = "\n".join(lines)
    limits = conf.read("detached_runs")
    tail_heading = f"\n\nLast log lines (lines over {limits.whole('result_line_chars')} characters are cut):\n"
    tail = _tail(Path(record["log"]), RESULT_TAIL_LINES,
                 limits.whole("result_text_chars") - len(header) - len(tail_heading))
    if cancellation:
        tail = [line for line in tail if line != _cut_line(cancellation)]
    body = tail_heading + "\n".join(tail) if tail else ""
    label_budget = max(0, limits.whole("result_text_chars") - len(header) - len(body))
    lines[0] += record["label"][:label_budget]
    header = "\n".join(lines)
    return header + body


@contextlib.contextmanager
def _delivery_lock(path: Path) -> Generator[None]:
    with _delivery_lock_path(path.parent, path.stem).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class _WorkerPassDeliveryBlocked(RuntimeError):
    """Stop pane input and save the delivery status."""


def _result_received(member: staff.StaffRow, membership: staff.StaffSnapshot, marker: str) -> bool:
    from scripts.message import send

    return send.WorkerResultProbe(marker).received(member, membership)


def _unconfirmed_result_received(record: RunRecord, membership: staff.StaffSnapshot, delivery: str) -> bool:
    member = _run_owner(record, membership)
    if isinstance(member, RunOwnerUnavailable):
        if not record.get("self_stop") or membership.by_id(staff.StaffId(record["staff_id"])) is not None:
            return False
        state_path = Path(record["director_dir"] or record["session_dir"]) / unit_runs.STATE_FILE
        with contextlib.suppress(OSError, ValueError):
            director = unit_runs.run_owner(worker_lifecycle.read_json_object(state_path), membership)
            if isinstance(director, staff.StaffRow):
                member = director
        if isinstance(member, RunOwnerUnavailable):
            return False
    marker = f"Detached run: {record['run_id']}"
    with contextlib.suppress(OSError, ValueError):
        if _result_received(member, membership, marker):
            return True
    if member.kind == "worker" and delivery.startswith(UNCONFIRMED_RESULT_PUSH):
        holder = membership.parent_of(member)
        with contextlib.suppress(OSError, ValueError):
            return holder is not None and _result_received(holder, membership, marker)
    return False


@contextlib.contextmanager
def _keep_send_error(path: Path) -> Generator[None]:
    """Save the error if the send may have started."""
    try:
        yield
    except _WorkerPassDeliveryBlocked:
        raise
    except (Exception, SystemExit) as error:
        saved = _read_record(path).get("delivery", "")
        detail = " ".join(str(error).splitlines()) or type(error).__name__
        if saved.startswith(UNCONFIRMED_WORKER_RESULT):
            _ = _save_delivery(path, f"{saved}: {detail}", if_current=saved)
        raise


class WorkerResultInput(Enum):
    TYPE = "submit the result to an idle worker"
    TURN_ENDED = "wait for the pane to become idle after the turn ends"


class WorkerResultTarget(Enum):
    PANE = "the worker pane can receive the result"
    PARKED = "the worker must resume before delivery"
    DIRECTOR = "the phase interrupted the owning pass"


@dataclass(frozen=True)
class RequestNewDone:
    completion_id: str


class SendResultAlone(Enum):
    SEND = "send the result without a DONE instruction"


class ParkedWorkerDelivery(Enum):
    DEFER = "leave pane restore to the worker event"


class AcquireWorkerInputLock(Enum):
    ACQUIRE = "acquire the worker lock before recording input"


class HeldWorkerInputLock(Protocol):
    def owns(self, row: staff.StaffRow) -> bool: ...


def _delivery_lock_path(runs: Path, run_id: str) -> Path:
    return runs / f".{run_id}.delivery.lock"


@contextlib.contextmanager
def guard_worker_park(run: Path, run_ids: tuple[str, ...]) -> Generator[bool]:
    """Keep results from reaching the pane while it closes."""
    with contextlib.ExitStack() as locks:
        for run_id in run_ids:
            if not run_id or Path(run_id).name != run_id:
                yield False
                return
            lock = locks.enter_context(_delivery_lock_path(run / DETACHED_RUNS_DIR, run_id).open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


def result_message(record: RunRecord, result: CommandResult) -> str:
    done_file = record["done_message_file"]
    if done_file and result == CompletedCommand(0) and not record.get("stop_reason"):
        try:
            return Path(done_file).read_text(encoding="utf-8")
        except OSError as error:
            return f"Run finished: {record['label']}\nResult: PASS (exit 0)\n(done message {done_file} unreadable: {error})"
    return result_text(record, result)


def _worker_result_message(record: RunRecord, result: CommandResult) -> str:
    return f"Detached run: {record['run_id']}\n" + result_message(record, result)


def confirmed_worker_results(runs: Path, worker: staff.StaffRow, ids: Collection[str]) -> Generator[RunRecord]:
    """Read finished results with a receipt for a pane submission."""
    for run_id in ids:
        if not run_id or Path(run_id).name != run_id:
            continue
        record = worker_lifecycle.read_json_object(runs / f"{run_id}.json")
        if (_is_run_record(record) and record["run_id"] == run_id and record["staff_id"] == worker.id
                and record["status"] == "finished" and Path(record["session_dir"]).resolve() == runs.parent.resolve()
                and "worker_result_receipt" in record):
            yield record


def deliver(record: RunRecord, result: CommandResult) -> str:
    delivery = _deliver(record, result)
    return delivery.delivery if isinstance(delivery, ConfirmedPaneResult) else delivery


def _deliver(record: RunRecord, result: CommandResult, *,
             worker_input: WorkerResultInput = WorkerResultInput.TYPE,
             resume_parked: Callable[[], bool] | ParkedWorkerDelivery = ParkedWorkerDelivery.DEFER,
             input_lock: HeldWorkerInputLock | AcquireWorkerInputLock = AcquireWorkerInputLock.ACQUIRE,
             ) -> str | ConfirmedPaneResult:
    """Deliver the command result to its current worker or director."""
    runs = _runs_dir(record["session_dir"])
    path = runs / f"{record['run_id']}.json"
    self_stop = record.get("self_stop") is True
    exit_code = result.code if isinstance(result, CompletedCommand) else result
    done_file = "" if record.get("slice_leftovers") or record.get("scope_error") else record["done_message_file"]
    if done_file and exit_code not in (0, SUPERSEDED_STATUS, TREE_CHANGED_STATUS) and not record.get("stop_reason"):
        return _save_delivery(path, _success_only_delivery(result))
    from scripts.message import send

    marker = f"Detached run: {record['run_id']}"

    def save_push(sent: send.Result, *, interrupted: bool = False) -> str:
        detail = f"{sent.outcome}: {sent.detail}"
        if isinstance(sent, (send.UncertainRelayDelivery, send.WorkerMessageUnconfirmed)):
            detail = UNCONFIRMED_RESULT_PUSH + detail
        elif sent.outcome == "refused" or isinstance(sent, send.RefusedRelayRequest):
            detail = FAILED_RESULT_PUSH + detail
        elif sent.outcome in ("failed", "retry", "pending", "held"):
            detail = UNCONFIRMED_RESULT_PUSH + detail
        elif interrupted:
            detail = INTERRUPTED_PASS_DELIVERY + detail
        return _save_delivery(path, detail)

    def save_deferral(detail: str) -> str:
        return _save_delivery(path, saved if saved.startswith((FAILED_RESULT_PUSH, FAILED_WORKER_SUBMISSION)) else detail)

    with _delivery_lock(path), contextlib.ExitStack() as locks:
        with record_lock(path):
            current_record = _read_record(path)
            saved = current_record.get("delivery", "")
            if "worker_result_receipt" in current_record and saved and not _worker_result_pending(saved):
                return f"already delivered: {record['run_id']}"
        unconfirmed = saved.startswith(UNCONFIRMED_WORKER_RESULT)
        try:
            membership = staff.snapshot()
        except staff.StaffUnreadable as error:
            if unconfirmed or (saved and not _worker_result_pending(saved)):
                return saved
            return _save_delivery(path, FAILED_RESULT_PUSH + str(error))
        member = _run_owner(record, membership)
        if (unconfirmed and (isinstance(member, RunOwnerUnavailable) or member.kind != "worker")
                and _unconfirmed_result_received(record, membership, saved)):
            return _save_delivery(path, f"already delivered: {record['run_id']}")
        if isinstance(member, RunOwnerUnavailable) and (
                not self_stop or membership.by_id(staff.StaffId(record["staff_id"])) is not None):
            if _result_push_failed(saved):
                return saved
            print(f"anomaly: {member.reason}", file=sys.stderr)
            return _save_delivery(path, "finished: worker retired")
        checkpoint = UNCONFIRMED_RESULT_PUSH + "the send did not record an outcome"
        if _result_push_failed(saved):
            checkpoint += f" after {saved}"
        if isinstance(member, RunOwnerUnavailable) or member.kind != "worker":
            if unconfirmed or (saved and not _worker_result_pending(saved)):
                return saved
            if isinstance(member, RunOwnerUnavailable):
                try:
                    member = worker_admission.parent(membership, run=Path(record["director_dir"] or record["session_dir"]))
                except (worker_admission.RunOwnerUnavailable, staff.StaffUnreadable) as error:
                    if isinstance(error, staff.StaffUnreadable) or record.get("ended") == "supervisor gone":
                        return _save_delivery(path, FAILED_RESULT_PUSH + f"director unavailable: {error}")
                    return save_deferral(DEFERRED_WORKER_RESULT + f"director unavailable: {error}")
            options = send.parse(["--to", str(member.id), "--from", "detached-runs",
                                  "--timeout", str(send_budget.seconds()), "--text",
                                  _worker_result_message(record, result)])
            _ = _save_delivery(path, checkpoint)
            with _keep_send_error(path), staff.supplied_snapshot(membership):
                sent = send.send(options)
            return save_push(sent)
        if saved and not _worker_result_pending(saved):
            return saved
        worker = member
        pane_checkpoint = checkpoint if saved.startswith(FAILED_RESULT_PUSH) else WORKER_INPUT_CHECKPOINT

        def read_pass(selected: staff.StaffRow, selected_membership: staff.StaffSnapshot
                      ) -> tuple[ResultPassOwnership, dict[str, object]]:
            from scripts.direct import recorder_state
            from scripts.herdr import worker_panes

            if record.get("pane_dispatch_id") == "":
                return ResultPassOwnership.OUTSIDE_PASS, {}
            try:
                run = worker_panes.worker_run(selected, record=selected_membership)
                passed = worker_panes.worker_pass(selected, run, record=selected_membership)
                ownership = result_pass_ownership(record, selected, run, passed)
            except (OSError, ValueError, recorder_state.OldRecorderState) as error:
                raise _WorkerPassDeliveryBlocked(
                    DEFERRED_WORKER_RESULT + f"worker pass is unavailable: {error}") from error
            return ownership, passed

        def require_owned_pass(ownership: ResultPassOwnership) -> None:
            if ownership is ResultPassOwnership.OTHER_PASS:
                raise _WorkerPassDeliveryBlocked(OTHER_PASS_DELIVERY)
            if ownership is ResultPassOwnership.UNKNOWN:
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + ownership.value)

        def check_pass(selected: staff.StaffRow, ownership: ResultPassOwnership,
                       passed: dict[str, object],
                       stage: ResultSubmissionPass | ResultWithoutPass | UnknownResultStage,
                       ) -> tuple[WorkerResultTarget, RequestNewDone | SendResultAlone]:
            from scripts.direct import progress_history

            if ownership is ResultPassOwnership.OUTSIDE_PASS:
                return WorkerResultTarget.PANE, SendResultAlone.SEND
            read_coverage = cast(Callable[[staff.StaffRow, dict[str, object], RunRecord],
                ResultDoneCoverage | ResultTranscriptUnknown],
                import_module("scripts.production.worker").result_done_coverage)
            coverage = read_coverage(selected, passed, record)
            if not isinstance(coverage, str):
                raise _WorkerPassDeliveryBlocked(
                    DEFERRED_WORKER_RESULT + f"worker transcript is unavailable: {coverage.reason}")
            if coverage == ResultDoneCoverage.COVERED:
                raise _WorkerPassDeliveryBlocked(COVERED_BY_DONE)
            if isinstance(stage, UnknownResultStage):
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + stage.value)
            if (_cancels_pass(record) and record.get("pane_dispatch_id") == passed.get("instance_id")
                    and passed.get("status") == "interrupted"):
                return WorkerResultTarget.DIRECTOR, SendResultAlone.SEND
            parked = progress_history.parked_worker_pass(passed)
            if parked and isinstance(resume_parked, ParkedWorkerDelivery):
                raise _WorkerPassDeliveryBlocked(PARKED_WORKER_RESULT)
            target = WorkerResultTarget.PARKED if parked else WorkerResultTarget.PANE
            if (coverage is ResultDoneCoverage.NEEDS_DELIVERY and isinstance(stage, ResultSubmissionPass)
                    and passed.get("status") in ("active", "refused")):
                return target, RequestNewDone(stage.completion)
            return target, SendResultAlone.SEND

        park_acceptance = ParkedAssignmentAcceptance.UNRECORDED

        def validate_transport(*, pane_input: bool = False) -> dict[str, object]:
            from scripts.herdr import worker_panes

            if isinstance(input_lock, AcquireWorkerInputLock):
                try:
                    _ = locks.enter_context(worker_panes.worker_id_lock(worker.id))
                except BlockingIOError as error:
                    raise _WorkerPassDeliveryBlocked(
                        DEFERRED_WORKER_RESULT + "the worker lock is held") from error
            try:
                current_membership = staff.local_snapshot()
            except staff.StaffUnreadable as error:
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + str(error)) from error
            current = current_membership.by_id(worker.id)
            if (current is None or current.kind != "worker" or current.current_session != worker.current_session
                    or current.parent != worker.parent or current.name != worker.name):
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the worker session changed")
            if not isinstance(input_lock, AcquireWorkerInputLock) and not input_lock.owns(current):
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the worker lock changed")
            holder = membership.parent_of(worker)
            current_holder = current_membership.parent_of(current)
            if holder is None or current_holder is None or current_holder.current_session != holder.current_session:
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the worker holder changed")
            current_ownership, current_pass = read_pass(current, current_membership)
            if current_ownership is not ownership or current_pass.get("instance_id") != passed.get("instance_id"):
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the worker pass changed")
            if current_ownership is ResultPassOwnership.OUTSIDE_PASS:
                return current_pass
            current_stage = result_submission_pass(current_pass)
            if current_stage != assignment:
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the worker stage changed")
            current_target, current_instruction = check_pass(current, current_ownership, current_pass, current_stage)
            if (current_target is WorkerResultTarget.DIRECTOR) != (target is WorkerResultTarget.DIRECTOR):
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the result destination changed")
            if pane_input and current_instruction != reply_instruction:
                raise _WorkerPassDeliveryBlocked(DEFERRED_WORKER_RESULT + "the worker DONE requirement changed")
            return current_pass

        def before_input() -> None:
            nonlocal park_acceptance
            from scripts.direct import progress_history

            current_pass = validate_transport(pane_input=True)
            parked_launch = progress_history.parked_worker_launch(current_pass)
            if (isinstance(assignment, ResultSubmissionPass)
                    and current_pass.get("worker_id") == worker.id
                    and isinstance(parked_launch, progress_history.ParkedWorkerLaunch)
                    and parked_launch.thread == worker.current_session):
                park_acceptance = ParkedAssignmentAcceptance.VERIFIED
            _ = _save_delivery(path, pane_checkpoint)

        def before_holder() -> None:
            _ = validate_transport()
            _ = _save_delivery(path, checkpoint)

        request = send.WorkerResultSubmission(wait_for_idle=worker_input == WorkerResultInput.TURN_ENDED,
            worker_only=self_stop, before_input=before_input, before_holder=before_holder)
        reason = ""
        try:
            ownership, passed = read_pass(worker, membership)
            require_owned_pass(ownership)
            assignment = (result_submission_pass(passed) if ownership is ResultPassOwnership.CURRENT
                          else ResultWithoutPass.OUTSIDE)
            if unconfirmed:
                if _unconfirmed_result_received(record, membership, saved):
                    return _save_delivery(path, f"already delivered: {record['run_id']}")
            else:
                try:
                    if _result_received(worker, membership, marker):
                        return _save_delivery(path, f"already delivered: {record['run_id']}")
                except (OSError, ValueError) as error:
                    reason = f"worker transcript is unavailable: {error}"
            target, reply_instruction = check_pass(worker, ownership, passed, assignment)
            if unconfirmed:
                return saved
            if target is WorkerResultTarget.DIRECTOR:
                reason = target.value
            if target is WorkerResultTarget.PARKED and not isinstance(resume_parked, ParkedWorkerDelivery):
                if not resume_parked():
                    return save_deferral(DEFERRED_WORKER_RESULT + "the parked worker could not resume")
                membership = staff.local_snapshot()
                resumed = membership.by_id(worker.id)
                if resumed is None:
                    return save_deferral(DEFERRED_WORKER_RESULT + "the resumed worker is unavailable")
                worker = resumed
                request = replace(request, worker_only=True)
            text = _worker_result_message(record, result)
            pane_text = ""
            if isinstance(reply_instruction, RequestNewDone):
                separator = "" if text.endswith("\n") else "\n"
                pane_text = (f"{text}{separator}End your final reply with this line alone: "
                             f"DONE {reply_instruction.completion_id}")
            timeout = send_budget.seconds()
            with _keep_send_error(path):
                delivery = send.deliver_worker_text(worker, membership, text,
                    sender="detached-runs", timeout=timeout, reason=reason, receipt=request, review_text=pane_text)
                if isinstance(delivery, send.BlockedWorkerResult):
                    if not _cancels_pass(record):
                        _ = validate_transport()
                        return _defer_blocked_worker_result(path, delivery)
                    before_holder()
                    delivery = delivery.send_to_holder(delivery.detail, text)
        except _WorkerPassDeliveryBlocked as blocked:
            status = str(blocked)
            if unconfirmed:
                # DONE can settle the pass. Keep the uncertain delivery for the alert.
                return COVERED_BY_DONE if status == COVERED_BY_DONE else saved
            if status in (OTHER_PASS_DELIVERY, COVERED_BY_DONE):
                return _save_delivery(path, status)
            return save_deferral(status)
        if isinstance(delivery, send.WorkerResultNotSubmitted):
            return save_deferral(FAILED_WORKER_SUBMISSION + delivery.detail)
        if isinstance(delivery, send.WorkerResultUnconfirmed):
            prefix = UNCONFIRMED_RESULT_PUSH if saved.startswith(FAILED_RESULT_PUSH) else UNCONFIRMED_WORKER_RESULT
            return _save_delivery(path, prefix + delivery.detail)
        if delivery.outcome == "retry":
            return save_deferral(DEFERRED_WORKER_RESULT + delivery.detail)
        if isinstance(delivery, send.WorkerResultSubmitted):
            if isinstance(assignment, UnknownResultStage):
                return _save_delivery(path, UNCONFIRMED_WORKER_RESULT + assignment.value)
            receipt = WorkerResultReceipt(record["run_id"], Path(record["session_dir"]).resolve(),
                                          delivery.worker, delivery.session, delivery.pane, delivery.terminal, assignment, delivery.cursor,
                                          delivery.input_digest, park_acceptance)
            submission = ConfirmedPaneResult(ownership, receipt, delivery.detail)
            _ = _save_delivery(path, submission.delivery, worker_result_receipt=receipt)
            return submission
        return save_push(delivery, interrupted=target is WorkerResultTarget.DIRECTOR)


def _defer_blocked_worker_result(path: Path, delivery: send.BlockedWorkerResult) -> str:
    """Keep the result pending and send a separate notice to the holder."""
    from scripts.message import send

    deferred = DEFERRED_WORKER_RESULT + delivery.detail
    with record_lock(path):
        record = _read_record(path)
        saved = record.get("delivery", "")
        if not _result_push_failed(saved):
            saved = deferred
            record["delivery"] = saved
        record["completion_pending"] = True
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
    if record.get("blocked_notice_accepted"):
        return saved
    label = send.one_line(record["label"], limit=80)
    sent = delivery.send_to_holder(f"needs input. The result for {label} waits.", str(path))
    if sent.outcome in ("sent", "queued", "skipped", "kept"):
        with record_lock(path):
            record = _read_record(path)
            record["blocked_notice_accepted"] = True
            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
    else:
        print(f"detached_runs: {record['run_id']}: blocked worker notice {sent.outcome}: {sent.detail}", file=sys.stderr)
    return saved


class KeepWorkerResultReceipt(Enum):
    EXISTING = "retain the saved receipt"


def _save_delivery(path: Path, delivery: str, *, if_current: str | None = None,
                   worker_result_receipt: WorkerResultReceipt | KeepWorkerResultReceipt = KeepWorkerResultReceipt.EXISTING,
                   ) -> str:
    with record_lock(path):
        record = _read_record(path)
        current = record.get("delivery", "")
        if if_current is not None and if_current != current:
            return current
        record["delivery"] = delivery
        if isinstance(worker_result_receipt, WorkerResultReceipt):
            record["worker_result_receipt"] = worker_result_receipt.to_json()
        if (delivery not in (COVERED_BY_DONE, OTHER_PASS_DELIVERY)
                and not _worker_result_pending(delivery) and "delivered_at" not in record):
            record["delivered_at"] = time.time()
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
    return delivery


class WorkerResultReplay(Enum):
    NONE = "no result needs submission"
    RETRY = "result delivery is unresolved"
    DUPLICATE = "the transcript prevented duplicate input"


@dataclass(frozen=True)
class ConfirmedPaneResult:
    """The record contains a receipt for this pane submission."""

    ownership: ResultPassOwnership
    receipt: WorkerResultReceipt
    destination: str

    @property
    def delivery(self) -> str:
        return f"sent: {self.destination}"


def submit_finished_worker_result(run: Path, worker: staff.StaffRow, *,
                                  worker_input: WorkerResultInput,
                                  resume_parked: Callable[[], bool] | ParkedWorkerDelivery = ParkedWorkerDelivery.DEFER,
                                  input_lock: HeldWorkerInputLock | AcquireWorkerInputLock = AcquireWorkerInputLock.ACQUIRE,
                                  ) -> WorkerResultReplay | ConfirmedPaneResult:
    """Submit one waiting result. The next turn can receive the next result."""
    from scripts.herdr import worker_panes

    passed = worker_panes.worker_pass(worker, run)
    for record in _registered_runs(run / DETACHED_RUNS_DIR, worker.id):
        if (record["status"] != "finished" or record["staff_id"] != worker.id
                or Path(record["session_dir"]).resolve() != run.resolve()
                or not _worker_result_pending(record.get("delivery", ""))):
            continue
        ownership = result_pass_ownership(record, worker, run, passed)
        if _unconfirmed_pass_result(record) or (
                ownership is ResultPassOwnership.OTHER_PASS and _result_push_failed(record.get("delivery", ""))):
            continue
        delivery = _deliver(record, command_result(record), worker_input=worker_input,
                            resume_parked=resume_parked, input_lock=input_lock)
        if ownership is ResultPassOwnership.OTHER_PASS:
            continue
        if isinstance(delivery, ConfirmedPaneResult):
            return delivery
        if _worker_result_pending(delivery):
            return WorkerResultReplay.RETRY
        if delivery.startswith("already delivered: ") and not isinstance(resume_parked, ParkedWorkerDelivery):
            return WorkerResultReplay.DUPLICATE
    return WorkerResultReplay.NONE


def _alert_failed_push(path: Path, *, check_receipt: bool = False) -> None:
    """Send the alert and record its outcome under the delivery lock."""
    from scripts.message import send

    with _delivery_lock(path):
        record = _read_record(path)
        if not _needs_push_retry(record):
            return
        delivery = record.get("delivery", "")
        state = ("Result delivery is unconfirmed. The result may have arrived."
                 if delivery.startswith(UNCONFIRMED_WORKER_RESULT) else "Result delivery failed.")
        text = (f"Detached run {record['run_id']}. {state}\n"
                f"Run: {record['label']}\n"
                f"Worker: {record['worker']}\n"
                f"Result: {_exit_words(command_result(record))}\n"
                f"Delivery: {delivery}")
        try:
            membership = staff.snapshot()
            if (check_receipt and delivery.startswith(UNCONFIRMED_WORKER_RESULT)
                    and _unconfirmed_result_received(record, membership, delivery)):
                _ = _save_delivery(path, f"already delivered: {record['run_id']}")
                return
            state_path = Path(record["director_dir"] or record["session_dir"]) / unit_runs.STATE_FILE
            lookup_error = ""
            try:
                fields = worker_lifecycle.read_json_object(state_path)
            except worker_lifecycle.WorkerStateUnreadable as error:
                fields = {}
                lookup_error = str(error)
            owner = unit_runs.run_owner(fields, membership)
            director = owner if isinstance(owner, staff.StaffRow) else membership.owner_of(record["staff_id"])
            if director is None or director.kind not in ("unit", "showrunner"):
                raise worker_admission.RunOwnerUnavailable(lookup_error or f"{state_path}: no director staff row")
            with staff.supplied_snapshot(membership):
                alert = send.send(send.parse(["--to", str(director.id), "--from", "detached-runs",
                                              "--timeout", str(send_budget.seconds()), "--text", text]))
            if alert.outcome not in ("sent", "queued", "skipped"):
                raise RuntimeError(f"{alert.outcome}: {alert.detail}")
        except (Exception, SystemExit) as error:
            detail = " ".join(str(error).splitlines())
            print(f"anomaly: {record['run_id']}: could not alert the director: {detail}", file=sys.stderr, flush=True)
        with record_lock(path):
            record = _read_record(path)
            record["push_alert_attempted"] = True
            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))


def _retry_failed_push(path: Path) -> None:
    """Retry a pending result after the supervisor releases the completion lock."""
    settings = conf.read("detached_runs")
    limit = settings.whole("result_push_retries")
    delay = settings.number("result_push_retry_seconds")
    while True:
        with record_lock(path):
            record = _read_record(path)
            if not _needs_push_retry(record):
                return
            retries = record.get("push_retries", 0)
            if retries >= limit:
                break
            record["push_retries"] = retries + 1
            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        time.sleep(delay)
        record = _read_record(path)
        if not _needs_push_retry(record):
            return
        try:
            _ = deliver(record, command_result(record))
        except (Exception, SystemExit) as error:
            detail = " ".join(str(error).splitlines())
            print(f"detached_runs: {record['run_id']}: result retry failed: {detail}", file=sys.stderr)
    _alert_failed_push(path)


def command_deliver(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", ""))).resolve()
    record = _read_record(record_path)
    has_slice = _has_saved_cgroup(record)
    if has_slice:
        with _scope_stop_lock(record_path, blocking=True):
            record = _read_record(record_path)
            if not _command_has_ended(record, record["pid"]):
                print(f"detached_runs: {record['run_id']}: the run slice has no confirmed end", file=sys.stderr)
                return 1
            if record.get("pending_hold_stop_requests") and (run_slice := record.get("command_slice")):
                _confirm_slice_end(record_path, run_slice)
                record = _read_record(record_path)
    if record.get("stop_pass_requested") and record["status"] != "finished" and not record.get("scope_error"):
        if "exit_code" not in record:
            print(f"detached_runs: {record['run_id']}: the stopped command has no confirmed exit", file=sys.stderr)
            return 1
        if not has_slice and not _stopped_group_has_ended(record):
            print(f"detached_runs: {record['run_id']}: the command group has no confirmed end", file=sys.stderr)
            return 1
    if _cancels_pass(record) and not _result_push_failed(record.get("delivery", "")):
        return int(_finish_record(record_path, record) == CompletionReplay.RETRY)
    delivery = deliver(record, command_result(record))
    if _result_push_failed(delivery):
        print(f"detached_runs: {record['run_id']}: {delivery}", file=sys.stderr)
        return 1
    if _cancels_pass(record):
        return int(_finish_record(record_path, record) == CompletionReplay.RETRY)
    return 0


def deliver_in_fresh_process(record_path: Path) -> int:
    """Import delivery code after the recorded command ends."""
    script = Path(__file__).resolve()
    launcher = str(script.parents[1] / "lib/py")
    try:
        completed = subprocess.run(
            [launcher, str(script), "deliver", str(record_path)],
            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", check=False,
        )
    except OSError as error:
        if cast(object, error.filename) == launcher and not _cancels_pass(_read_record(record_path)):
            _ = _save_delivery(record_path, FAILED_RESULT_PUSH + str(error), if_current="")
        print(f"detached_runs: delivery process did not start: {error}", file=sys.stderr)
        return 1
    if completed.stderr:
        _ = sys.stderr.write(completed.stderr)
        if completed.returncode and not _cancels_pass(_read_record(record_path)):
            detail = completed.stderr.strip().splitlines()
            if detail:
                _ = _save_delivery(record_path, FAILED_RESULT_PUSH + detail[-1], if_current="")
    return completed.returncode


def _command_slice_unit(run_id: str) -> str:
    slice_id = run_id.partition("@")[0].replace("-", "")
    return f"builds-detached-{slice_id}.slice"


def _command_scope_unit(kind: str, run_id: str) -> process_stop.ScopeUnit:
    scope_id = run_id.partition("@")[0]
    return process_stop.ScopeUnit(_command_slice_unit(run_id), f"detached-{kind}-{scope_id}.scope")


def scope_prefix(log: Path, environment: dict[str, str], *,
                 kind: DetachedRunKind = DetachedRunKind.DETECT) -> list[str]:
    """Start the command in its own slice when a scope is available."""
    runtime = environment.get("XDG_RUNTIME_DIR", "")
    if (not process_stop.supported() or environment.get("BUILDLOG_SCOPE", "1") == "0" or not runtime
            or not Path(runtime, "systemd", "private").is_socket() or shutil.which("systemd-run") is None):
        return []
    scope = _command_scope_unit(kind, log.stem)
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
            *process_stop.scope_slice_args(scope.slice, environment={}), f"--unit={scope.unit}",
            "--", sys.executable, "-IS", "-c", SCOPE_SCRIPT, str(Path(__file__).resolve().parents[2]),
            scope.slice, scope.unit]


def _run(command: list[str], environment: dict[str, str], log: Path) -> int:
    """Publish the command identity and group, then run the command in its own session.

    A stop leaves the supervisor alive to record and deliver the result.
    The gate input stays open until the command ends.
    The supervisor saves the slice cleanup intent before it releases the watcher.
    """
    script = Path(__file__).resolve()
    try:
        with log.open("ab") as output:
            child = subprocess.Popen(["/bin/sh", "-c", COMMAND_GATE, "detached-command",
                                      str(script.parents[1] / "lib/py"), str(script), *command],
                                     env=environment, start_new_session=True, bufsize=0,
                                     stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
    except OSError as exc:
        with log.open("a", encoding="utf-8") as output:
            _ = output.write(f"detached_runs: command did not start: {exc}\n")
        return 127

    assert child.stdin is not None
    path = Path(environment["UNIT_DETACHED_RECORD"])
    with child.stdin as gate:
        started = process_start.process_start(child.pid)
        if not isinstance(started, process_start.Started):
            with log.open("a", encoding="utf-8") as output:
                _ = output.write("detached_runs: command identity is unavailable; command did not start\n")
            # The gate shell reads the end of its input and exits 125.
            gate.close()
        else:
            with record_lock(path):
                record = _read_record(path)
                record.update(command_pid=child.pid, command_group=os.getpgid(child.pid),
                              command_started=started.start)
                supervisor = process_start.process_start(os.getpid())
                if isinstance(supervisor, process_start.Started):
                    record["supervisor_started"] = supervisor.start
                write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
            _send_gate_line(gate, b"start\n")
        exit_code = _exit_status(child.wait())
        if not gate.closed:
            _ = _checkpoint_command_exit(path, exit_code, scoped_only=True)
            _send_gate_line(gate, b"done\n")
    return exit_code


def _checkpoint_command_exit(path: Path, exit_code: int, *, scoped_only: bool = False) -> RunRecord:
    """Save the exit status and any cleanup that must finish before delivery."""
    with record_lock(path):
        record = _read_record(path)
        if record["status"] == "finished" or (scoped_only and not _has_saved_cgroup(record)):
            return record
        updated = record.copy()
        updated["exit_code"] = exit_code
        if _has_saved_cgroup(record):
            updated["slice_cleanup_pending"] = True
            _ = updated.pop("finished_at", None)
        if updated != record:
            write_text_atomic(path, json.dumps(updated, indent=2, sort_keys=True))
        return updated


def _save_finish_time(path: Path) -> RunRecord:
    """Save the finish time after cleanup and before delivery, so that a later DONE covers the run."""
    with record_lock(path):
        record = _read_record(path)
        if (record["status"] != "running" or "finished_at" in record
                or record.get("scope_error") or record.get("slice_cleanup_pending")):
            return record
        record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        return record


def _send_gate_line(gate: IO[bytes], line: bytes) -> None:
    """A group stop can end the gate shell and its watcher before they read."""
    with contextlib.suppress(BrokenPipeError):
        _ = gate.write(line)


class CompletionSourceLoader(SourceFileLoader):
    """Load the source that existed before the command ran."""

    def __init__(self, name: str, path: Path, source: bytes) -> None:
        super().__init__(name, str(path))
        self.source: bytes = source

    @override
    def get_code(self, fullname: str) -> CodeType:
        return compile(self.source, self.get_filename(fullname), "exec", dont_inherit=True)


class CompletionSources(MetaPathFinder):
    """Keep deferred imports on the same source as the completion handler."""

    def __init__(self, root: Path) -> None:
        self.modules: dict[str, CompletionSourceLoader] = {}
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).with_suffix("").parts
            module = ".".join(("scripts", *(parts[:-1] if path.stem == "__init__" else parts)))
            if module not in sys.modules and not path.stem.startswith("test_"):
                self.modules[module] = CompletionSourceLoader(module, path, path.read_bytes())

    @override
    def find_spec(self, fullname: str, path: Sequence[str] | None = None,
                  target: ModuleType | None = None) -> ModuleSpec | None:
        loader = self.modules.get(fullname)
        return spec_from_file_location(fullname, loader.path, loader=loader) if loader is not None else None


@contextlib.contextmanager
def _completion_sources(root: Path) -> Generator[None]:
    # Keep module paths intact for code that uses __file__ to find data.
    sources = CompletionSources(root)
    sys.meta_path.insert(0, sources)
    try:
        for module in ("scripts.herdr.turn_end", "scripts.direct.assign", "scripts.message.send"):
            _ = import_module(module)
        yield
    finally:
        sys.meta_path.remove(sources)


def _finish_command_slice(path: Path, run_slice: process_stop.RunSliceIdentity) -> None:
    grace, kill_grace = _scope_stop_graces()
    with _scope_stop_lock(path, blocking=True):
        record = _read_record(path)
        if record.get("command_slice") != run_slice:
            raise process_stop.SliceStopFailed("the saved run slice changed during cleanup")
        committed = bool(record.get("stop_pass_requested"))
        if not (committed or record.get("slice_cleanup_pending")):
            return
        ended = (process_stop.slice_has_ended(run_slice) if committed
                 else process_stop.slice_ends_by(run_slice, time.monotonic() + grace))
        _finish_slice_stop(path, run_slice, ended=ended, grace=grace, kill_grace=kill_grace, cleanup=not committed)


def command_supervise(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", "")))
    with (record_path.parent / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        record = _read_record(record_path)
        if record["pid"] == 0:
            record["pid"] = os.getpid()
            record = _write_record(record_path, record)
    environment = dict(os.environ)
    _ = environment.pop(process_stop.DETACHED_SLICE_ENV, None)
    environment["PYTHONPATH"] = startup_pythonpath(environment.get("PYTHONPATH", ""))
    environment.update({
        "UNIT_DETACHED_RUN": "1",
        "UNIT_RUN_DIR": record["session_dir"],
        "UNIT_TEAM_ROLE": record["worker"].rsplit("-", 1)[-1],
        "UNIT_DETACHED_RECORD": str(record_path.resolve()),
        "BUILDLOG_OWNER_STAFF_ID": record["staff_id"],
    })
    log = Path(record["log"])
    try:
        kind = DetachedRunKind(record.get("kind", DetachedRunKind.DETECT))
    except ValueError:
        kind = DetachedRunKind.DETECT
    prefix = scope_prefix(log, environment, kind=kind)
    with _completion_sources(Path(__file__).resolve().parents[1]):
        command = _place_command_in_run_slice(record["command"]) if prefix else record["command"]
        exit_code = _run(prefix + command, environment, log)
        with record_lock(record_path):
            record = _recover_staged_pane_stop(record_path, _read_record(record_path))
            retry_without_scope = bool(prefix) and "command_slice" not in record and not record.get("stop_reason")
        if retry_without_scope:
            exit_code = _run(record["command"], environment, log)
            with record_lock(record_path):
                record = _recover_staged_pane_stop(record_path, _read_record(record_path))
        # Deliver before marking finished: a launcher that reads the run as finished closes its inbox
        # unless the result is already held.
        record = _checkpoint_command_exit(record_path, exit_code)
        if run_slice := record.get("command_slice"):
            try:
                _finish_command_slice(record_path, run_slice)
            except (OSError, ValueError) as error:
                print(f"detached_runs: {record['run_id']}: slice cleanup failed: {error}", file=sys.stderr)
                return 1
        elif record.get("stop_pass_requested") and not _stopped_group_has_ended(record):
            grace, kill_grace = _scope_stop_graces()
            deadline = time.monotonic() + grace + kill_grace
            while not _stopped_group_has_ended(record):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record["scope_error"] = "the command group did not end within the stop grace"
                    record = _write_record(record_path, record)
                    break
                time.sleep(min(0.25, remaining))
        record = _save_finish_time(record_path)
        delivery_exit = deliver_in_fresh_process(record_path)
        record = _read_record(record_path)
        cancelled = _cancels_pass(record)
        if cancelled:
            if delivery_exit == 0 and record["status"] == "finished":
                return 0
            if record["status"] == "running" and not _stopped_group_has_ended(record):
                return 1
        elif delivery_exit != 0:
            # The delivery process saves a checkpoint before it starts a send.
            _ = _save_delivery(
                record_path, FAILED_RESULT_PUSH + f"delivery process exited {delivery_exit}", if_current="")
        completion = _finish_record(record_path, record)
        saved = worker_lifecycle.read_json_object(record_path)
        if _is_run_record(saved) and _needs_push_retry(saved):
            _retry_failed_push(record_path)
            saved = worker_lifecycle.read_json_object(record_path)
            if _is_run_record(saved) and _needs_completion(saved):
                completion = _finish_record(record_path, saved)
        if _result_push_failed(str(saved.get("delivery", ""))):
            return 1
        if record.get("scope_error"):
            return 1
        if cancelled and completion == CompletionReplay.RETRY:
            return 1
    return 0


def _wait_for_group(command: CommandGroup, deadline: float) -> None:
    """Wait for the saved command group within the stop grace."""
    budget = process_start.ProcessReadDeadline(deadline)
    while time.monotonic() < deadline:
        leader = _saved_process(command.group, command.started, budget=budget)
        if isinstance(leader, SavedProcessIdReused):
            return
        if isinstance(leader, (process_start.Gone, process_start.Zombie)):
            if not _group_is_live(command.group, budget=budget):
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _stop_watched_group(path: Path, watched: CommandGroup) -> None:
    """Send TERM to the group that the watcher just left."""
    group_is_live = _group_is_live(watched.group)

    def verify_group() -> CommandGroup | CommandGroupUnverified:
        record = worker_lifecycle.read_json_object(path)
        if record.get("status") != "running" or _has_saved_cgroup(record) or _recorded_group(record) != watched:
            return CommandGroupUnverified()
        leader = _saved_process(watched.group, watched.started)
        if isinstance(leader, SavedProcessIdReused):
            return CommandGroupUnverified()
        if isinstance(leader, process_start.Unreadable) and not group_is_live:
            return CommandGroupUnverified()
        return watched

    with contextlib.suppress(ProcessLookupError):
        _ = stop_with_reason(path, SUPERVISOR_GONE_REASON, verify_group=verify_group,
                             signal_group=process_group.send_process_group)


def command_supervisor_gone(args: argparse.Namespace) -> int:
    """Stop the saved scope or command group after the supervisor pipe closes."""
    path = Path(str(getattr(args, "record", "")))
    use_pidfd = process_stop.supported()
    watched_group = None
    # The watcher inherits the group when the command starts.
    # Its membership proves the group even if the leader has exited.
    if not use_pidfd and (command := command_group(path)) is not None and os.getpgrp() == command.group:
        watched_group = command
    # Leave the group before TERM.
    os.setsid()

    def supervisor_is_gone() -> bool:
        record = _read_record(path)
        return not _has_saved_cgroup(record) or not detached_run_process.supervises(record["pid"], path)

    try:
        if use_pidfd:
            _ = stop_command(path, SUPERVISOR_GONE_REASON, check=supervisor_is_gone,
                             stop_pass=False, retry_slice_read=True)
        elif watched_group is not None:
            _stop_watched_group(path, watched_group)
    except OSError as error:
        print(f"detached_runs: {path.stem}: stop failed: {error}", file=sys.stderr)
        return 1
    record = _read_record(path)
    group_stop = not use_pidfd and bool(record.get("stop_reason"))
    if group_stop and not _has_saved_cgroup(record) and (command := _recorded_group(record)) is not None:
        grace, _ = _scope_stop_graces()
        _wait_for_group(command, time.monotonic() + grace)
    elif record.get("scope_error") and not _has_saved_cgroup(record):
        grace, _ = _scope_stop_graces()
        deadline = time.monotonic() + grace
        budget = process_start.ProcessReadDeadline(deadline)
        while time.monotonic() < deadline and not _command_has_ended(record, record["pid"], budget=budget):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
    _ = reap_orphans(str(path.resolve().parent.parent))
    return 0


def command_record_exit(args: argparse.Namespace) -> int:
    """Save the command status before its orphan record is completed."""
    path = Path(str(getattr(args, "record", "")))
    caller = int(getattr(args, "pid", 0)) or os.getppid()
    with record_lock(path):
        record = _read_record(path)
        if record["status"] != "running" or record.get("command_pid") != caller:
            return 0
        started = _saved_process(caller, record.get("command_started"))
        if not isinstance(started, process_start.Started):
            return 0
        record["exit_code"] = int(getattr(args, "exit_code", 0))
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
    return 0


def command_stop(args: argparse.Namespace) -> int:
    path = Path(str(getattr(args, "record", ""))).resolve()
    try:
        if stop_command(path, str(getattr(args, "reason", ""))):
            print(f"stopped {path.stem}: its result comes from the supervisor")
            return 0
    except OSError as error:
        print(f"detached_runs: {path.stem}: stop failed: {error}", file=sys.stderr)
        return 1
    raw = worker_lifecycle.read_json_object(path)
    if error := raw.get("scope_error"):
        print(f"detached_runs: {path.stem}: stop unconfirmed: {error}", file=sys.stderr)
        return 1
    _recover_indexed_stop(path, raw)
    if raw.get("stop_pass_requested") is True:
        print(f"{path.stem} already has a stop request", file=sys.stderr)
        return 1
    print(f"{path.stem} is not running or its process identity changed; nothing stopped", file=sys.stderr)
    return 1


def command_reap(args: argparse.Namespace) -> int:
    for run_id in reap_orphans(str(getattr(args, "session_dir", "")), str(getattr(args, "worker", "") or "")):
        print(f"run {run_id} ended: its supervisor is gone")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    start = verbs.add_parser("start", help="run a command detached unless the caller already has the same run live")
    _ = start.add_argument("--session-dir", default="")
    _ = start.add_argument("--to", default="")
    _ = start.add_argument("--director-dir", default="", help="the run folder whose director hears results")
    _ = start.add_argument("--label", default="")
    _ = start.add_argument("--kind", type=DetachedRunKind, choices=list(DetachedRunKind), default=DetachedRunKind.DETECT)
    _ = start.add_argument("--done-message-file", default="")
    _ = start.add_argument("command", nargs=argparse.REMAINDER)
    start.set_defaults(handler=command_start)
    supervise = verbs.add_parser("supervise", help="run one recorded command and deliver its result")
    _ = supervise.add_argument("record")
    supervise.set_defaults(handler=command_supervise)
    gone = verbs.add_parser("supervisor-gone", help="stop and report a run whose supervisor ended first")
    _ = gone.add_argument("record")
    gone.set_defaults(handler=command_supervisor_gone)
    record_exit = verbs.add_parser("record-exit", help="save a command status without completing its run")
    _ = record_exit.add_argument("--pid", type=int, default=0)
    _ = record_exit.add_argument("record")
    _ = record_exit.add_argument("exit_code", type=int, choices=range(256))
    record_exit.set_defaults(handler=command_record_exit)
    delivery = verbs.add_parser("deliver", help="deliver one recorded result from the current code tree")
    _ = delivery.add_argument("record")
    delivery.set_defaults(handler=command_deliver)
    stop = verbs.add_parser("stop", help="stop a running command; its supervisor delivers the stopped result")
    _ = stop.add_argument("--reason", required=True, help="the Stop reason line of the result")
    _ = stop.add_argument("record")
    stop.set_defaults(handler=command_stop)
    reap = verbs.add_parser("reap", help="end the running records whose supervisor is gone")
    _ = reap.add_argument("--session-dir", required=True)
    _ = reap.add_argument("--worker", default="")
    reap.set_defaults(handler=command_reap)
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", getattr(args, "handler"))
    return handler(args)


if __name__ == "__main__":
    if sys.argv[1:2] == ["supervise"]:
        for number in CANCELLATION_SIGNALS:
            _ = signal.signal(number, signal.SIG_DFL)
    sys.exit(main())
