#!/usr/bin/env python3
"""Run a worker command detached and deliver its result through the native worker route.

Start writes a run record and returns after it starts the supervisor.
The supervisor runs the command and delivers its result to the current worker.
A unit director or showrunner names its run folder and receives its result through send.py.
Reap finishes records whose supervisor disappeared and delivers their results.
The command runs in its own session; the record names its process group.
`stop` signals that group, and the supervisor records the exit status and delivers the result.
A watcher in the command group holds the supervisor pipe. If the supervisor ends first, the watcher
stops the command group and delivers the STOPPED result.
Exit 3 means that the caller is neither a registered worker nor a director with a run folder.
"""

# Each platform check needs only the suppression for the other platform's exit watch.
# pyright: reportUnnecessaryTypeIgnoreComment=false

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
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, StrEnum
from importlib import import_module
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, SourceFileLoader
from importlib.util import spec_from_file_location
from pathlib import Path
from types import CodeType, ModuleType
from typing import IO, TYPE_CHECKING, Literal, NotRequired, TypedDict, TypeGuard, cast, override

from scripts.agents import detached_run_process, worker_lifecycle
from scripts.buildlog import treekey
from scripts.lib import atomic_file, conf, process_group, process_start, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import send_budget
from scripts.production import staff, worker_admission

if TYPE_CHECKING:
    from scripts.accounts.session_state import RunEvidence

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
# On natedev the command runs in builds-detached.slice, a child of builds.slice.
# The parent slice's memory limit and CPU weight also apply to the detached command.
# The scope writes the marker before the command starts.
# A missing marker after the scope exits proves that the command did not start there.
# The command then runs without a scope. BUILDLOG_SCOPE=0 disables the scope, as it does for invoke.sh.
SCOPE_SCRIPT = ': > "$0" || exit 125; exec "$@"'
# The gate shell keeps its PID across exec and waits for identity publication.
# Its watcher reads the supervisor pipe. An end of input without "done" means that the supervisor ended
# before the command: a kill of any kind or a crash. The watcher then runs `supervisor-gone`.
COMMAND_GATE = ('read -r ready || exit 125; watcher_python=$1 watcher_script=$2; shift 2; '
                + '(read -r end; [ "$end" = done ] || exec "$watcher_python" "$watcher_script" supervisor-gone '
                + '"$UNIT_DETACHED_RECORD") <&0 & exec 0</dev/null; exec "$@"')
SUPERVISOR_GONE_REASON = "the supervisor ended before the command, so its watcher stopped the command"
CONFIRMED_STOP_SUFFIX = ".stop-confirmed"
STAGED_STOP_SUFFIX = ".stop-staged"
STOPPED_GROUP_CHECK_SECONDS = 0.25
LEADER_IDENTITY_TIMEOUT_SECONDS = 5.0


class PaneDispatchUnavailable(TypedDict):
    unavailable: str


class StopFields(TypedDict, total=False):
    stop_reason: str
    stop_requested_at: float
    stop_pass_requested: Literal[True]
    self_stop: Literal[True]


class DetachedRunKind(StrEnum):
    DETECT = "detect"
    PYTHON_TESTS = "python-tests"
    OTHER = "other"


class RunRecord(StopFields):
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
    director_dir: str
    done_message_file: str
    # The tree key of cwd at start, "" when unknown; records before 2026-10-21 have none.
    tree_key: NotRequired[str]
    exit_code: NotRequired[int]
    stop_escalated: NotRequired[Literal[True]]
    stop_group_end: NotRequired[Literal["confirmed"]]
    finished_at: NotRequired[str]
    delivery: NotRequired[str]
    delivered_at: NotRequired[float]
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


def _worker_reference(session_dir: str, name: str) -> str:
    member = worker_admission.member(Path(session_dir), name)
    return str(member.id) if member is not None else ""


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
        if "stop_reason" in current:
            fields["stop_reason"] = current["stop_reason"]
        else:
            _ = fields.pop("stop_reason", "")
        if "stop_requested_at" in current:
            fields["stop_requested_at"] = current["stop_requested_at"]
        else:
            _ = fields.pop("stop_requested_at", None)
        if "stop_pass_requested" in current:
            fields["stop_pass_requested"] = current["stop_pass_requested"]
        else:
            _ = fields.pop("stop_pass_requested", False)
        if "self_stop" in current:
            fields["self_stop"] = current["self_stop"]
        else:
            _ = fields.pop("self_stop", False)
        if not _is_run_record(fields):
            raise SystemExit(f"detached_runs: {path} update is not a run record")
        write_text_atomic(path, json.dumps(fields, indent=2, sort_keys=True))
        if fields["status"] == "finished":
            path.with_suffix(PENDING_STOP_SUFFIX).unlink(missing_ok=True)
        return fields


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
                                        "delivery", "ended", "stop_reason"))
                    and ("delivered_at" not in raw or type(raw["delivered_at"]) in (int, float))
                    and ("stop_requested_at" not in raw or _is_stop_time(raw["stop_requested_at"]))
                    and ("stop_group_end" not in raw or raw["stop_group_end"] == "confirmed")
                    and ("completion_pending" not in raw or isinstance(raw["completion_pending"], bool))
                    and all(key not in raw or raw[key] is True
                            for key in ("stop_pass_requested", "self_stop", "stop_escalated",
                                        "orphan_result_superseded", "push_alert_attempted"))
                    and ("pane_dispatch_id" not in raw or isinstance(pane_dispatch, str)
                         or (isinstance(pane_dispatch, dict)
                             and isinstance(cast(dict[str, object], pane_dispatch).get("unavailable"), str))))
        case _:
            return False


def _is_stop_time(value: object) -> TypeGuard[float]:
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _read_stop_fields(path: Path, run_id: object) -> StopFields:
    saved = worker_lifecycle.read_json_object(path)
    if not saved:
        return {}
    reason = saved.get("stop_reason")
    if (not isinstance(run_id, str) or saved.get("run_id") != run_id or not isinstance(reason, str)
            or any(key in saved and saved[key] is not True for key in ("stop_pass_requested", "self_stop"))):
        raise worker_lifecycle.WorkerStateUnreadable(path, "invalid stop fields")
    fields: StopFields = {"stop_reason": reason}
    if "stop_requested_at" in saved:
        requested_at = saved["stop_requested_at"]
        if not _is_stop_time(requested_at):
            raise worker_lifecycle.WorkerStateUnreadable(path, "invalid stop request time")
        fields["stop_requested_at"] = requested_at
    if saved.get("stop_pass_requested") is True:
        fields["stop_pass_requested"] = True
    if saved.get("self_stop") is True:
        fields["self_stop"] = True
    return fields


def _read_record(path: Path) -> RunRecord:
    raw = worker_lifecycle.read_json_object(path)
    if not _is_run_record(raw):
        raise SystemExit(f"detached_runs: {path} is not a run record")
    if path.name != f"{raw['run_id']}.json":
        raise SystemExit(f"detached_runs: {path} does not match run ID {raw['run_id']}")
    confirmed = _read_stop_fields(path.with_suffix(CONFIRMED_STOP_SUFFIX), raw["run_id"])
    return {**raw, **confirmed} if confirmed else raw


def _recover_staged_stop(path: Path, record: RunRecord) -> RunRecord:
    """Recover the stop request under the record lock after the command exits."""
    if record.get("stop_pass_requested") or not path.with_suffix(PENDING_STOP_SUFFIX).exists():
        return record
    staged = path.with_suffix(STAGED_STOP_SUFFIX)
    fields = _read_stop_fields(staged, record["run_id"])
    if not fields or ("stop_reason" in record and not fields.get("stop_pass_requested")):
        return record
    _ = staged.replace(path.with_suffix(CONFIRMED_STOP_SUFFIX))
    atomic_file.sync_folder(path.parent)
    return {**record, **fields}


def stop_with_reason(
    path: Path, reason: str, *, verify_group: Callable[[], CommandGroup | CommandGroupUnverified],
    signal_group: Callable[[int, int], None], stop_pass: bool = False, self_stop: bool = False,
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
        # Save the request time before TERM so recovery keeps the same deadline.
        requested_at = record["stop_requested_at"] if "stop_requested_at" in record else time.monotonic()
        stopping = {**record, "stop_reason": reason, "stop_requested_at": requested_at}
        pending_stop = path.with_suffix(PENDING_STOP_SUFFIX)
        was_indexed = pending_stop.exists()
        pending_stop.touch(exist_ok=True)
        if stop_pass:
            stopping["stop_pass_requested"] = True
            if self_stop:
                stopping["self_stop"] = True
        signaled = False
        try:
            try:
                write_text_atomic(staged, json.dumps(stopping, indent=2, sort_keys=True),
                                  new_file_mode=path.stat().st_mode & 0o777)
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
                if record["status"] == "finished" or (not was_indexed and not signaled):
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
    group, started = record.get("command_group"), record.get("command_started")
    if (record.get("status") == "running" and isinstance(group, int) and group > 0
            and isinstance(started, str) and started):
        return CommandGroup(group, started)
    return None


def stop_command(path: Path, reason: str) -> bool:
    """Save the reason and TERM the command group while the supervisor and command leader are current."""
    record = worker_lifecycle.read_json_object(path)
    supervisor = _integer(record.get("pid"))

    def verify_group() -> CommandGroup | CommandGroupUnverified:
        command = command_group(path)
        if (command is None or detached_run_process.supervisor_match(supervisor, path) is not True
                or process_start.process_start(command.group) != process_start.Started(command.started)):
            return CommandGroupUnverified()
        return command

    try:
        return stop_with_reason(path, reason, verify_group=verify_group,
                                signal_group=process_group.send_process_group, stop_pass=True,
                                self_stop=_stop_caller_is_owner(record.get("staff_id"), os.environ))
    except ProcessLookupError:
        return False


def seat_name(session_dir: str, name: str) -> str:
    """Resolve the current staff worker through its active recorder slot."""
    run = Path(session_dir)
    membership = staff.local_snapshot()
    owner = worker_admission.parent(membership, run=run)
    state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
    passes = state.get("pass")
    current = cast(dict[str, object], passes).get(name) if isinstance(passes, dict) else None
    member = None
    if isinstance(current, dict):
        dispatch = cast(dict[str, object], current)
        if dispatch.get("status") == "active":
            name = str(dispatch.get("worker_name", "")) or name
            member = membership.by_id(staff.StaffId(str(dispatch.get("worker_id", ""))))
    if member is None:
        member = worker_admission.find_worker(membership, owner.id, name)
    return member.name if member is not None and member.kind == "worker" and member.reports_to(owner.id) else name


def _integer(value: object) -> int:
    return value if type(value) is int else 0


def _number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _group_is_live(group: int) -> bool:
    """Keep the run pending while its group has live processes or cannot be read."""
    try:
        timeout = conf.read("detached_runs").whole("STOP_GROUP_SCAN_TIMEOUT_SECONDS")
        result = subprocess.run(["ps", "-A", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, check=True, timeout=timeout)
    except (conf.Unusable, OSError, UnicodeError, subprocess.SubprocessError):
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


def _saved_process(pid: object, started: object) -> process_start.ProcessStart | SavedProcessIdReused:
    """Read the saved process identity and identify a reused PID."""
    if not isinstance(started, str) or not started or _integer(pid) <= 0:
        return process_start.Gone()
    observed = process_start.process_start(_integer(pid))
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
    started = _number(raw.get("started_at"), now)
    return now - started >= DETACHED_RUN_START_GRACE_SECS


def _command_has_ended(raw: Mapping[str, object], pid: int) -> bool:
    if raw.get("stop_group_end") == "confirmed":
        return True
    command = _saved_process(raw.get("command_pid"), raw.get("command_started"))
    if isinstance(command, (process_start.Started, process_start.Unreadable)):
        return False
    group = _integer(raw.get("command_group"))
    if group > 0:
        leader = group == _integer(raw.get("command_pid"))
        if leader and isinstance(command, SavedProcessIdReused):
            return True
        if not _group_is_live(group):
            return True
        # The group ID can be reused after the last member exits.
        return leader and isinstance(_saved_process(group, raw.get("command_started")), SavedProcessIdReused)
    supervisor = _saved_process(pid, raw.get("supervisor_started"))
    owns_group = isinstance(supervisor, (process_start.Started, process_start.Zombie))
    if not owns_group and isinstance(command, process_start.Zombie):
        try:
            owns_group = os.getpgid(_integer(raw.get("command_pid"))) == pid
        except ProcessLookupError:
            pass
        except OSError:
            return False
    return not owns_group or not _group_is_live(pid)


def _stopped_group_has_ended(record: RunRecord) -> bool:
    return _integer(record.get("command_group")) > 0 and _command_has_ended(record, record["pid"])


def _cancels_pass(record: RunRecord) -> bool:
    return bool(record.get("stop_pass_requested") and not record.get("self_stop"))


def _retired_worker_result(record: RunRecord) -> bool:
    if record.get("self_stop") and _worker_result_pending(record.get("delivery", "")):
        with contextlib.suppress(staff.StaffUnreadable):
            return staff.local_snapshot().by_id(staff.StaffId(record["staff_id"])) is None
    return False


SUPERSEDED_ORPHAN_DELIVERY = "nothing delivered: the supervisor is gone and a newer run of the same command answers"
OTHER_PASS_DELIVERY = "nothing delivered: the result belongs to a different pane pass"


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
                        pending = _replay_completion(path, current) == CompletionReplay.RETRY
                if pending:
                    _alert_failed_push(path, check_receipt=True)
            except (SystemExit, worker_lifecycle.WorkerStateUnreadable) as error:
                print(" ".join(str(error).splitlines()), file=sys.stderr)


def reap_orphans(session_dir: str, worker: str = "") -> list[str]:
    """Recover results after the command and owned group processes end.

    Deliver the saved command status when available.
    A claim file prevents duplicate delivery.
    """
    runs = _runs_dir(session_dir)
    if not runs.is_dir():
        return []
    with (runs / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        result = _reap_orphans_locked(runs, worker)
    result.complete()
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
        if not _orphaned(path, raw, now):
            continue
        claim = path.with_suffix(".reap")
        try:
            os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        try:
            with record_lock(path):
                record = _read_record(path)
                if record["status"] != "running":
                    continue
                record = _recover_staged_stop(path, record)
                try:
                    Path(record["log"]).with_suffix(".scope").unlink(missing_ok=True)
                except OSError as error:
                    print(f"detached_runs: {path}: could not remove the scope marker: {error}", file=sys.stderr)
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
            _ = record.setdefault("finished_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            record = _write_record(path, record)
            result.queue_completion(path, record)
            result.run_ids.append(record["run_id"])
        except (SystemExit, worker_lifecycle.WorkerStateUnreadable) as error:
            print(" ".join(str(error).splitlines()), file=sys.stderr)
        finally:
            claim.unlink(missing_ok=True)
    return result


class CompletionReplay(StrEnum):
    HANDLED = "the completion event needs no replay"
    RETRY = "the completion event needs another read"


class PaneStopDeferred(TypedDict):
    """The owning pane pass still needs cancellation."""

    reason: str


def _finish_record(path: Path, record: RunRecord) -> CompletionReplay:
    """Publish the finished run and claim its pending completion."""
    with path.with_suffix(".completion.lock").open("a") as claim:
        fcntl.flock(claim, fcntl.LOCK_EX)
        current = _read_record(path)
        if current["status"] == "finished":
            if current.get("completion_pending") is True:
                return _replay_completion(path, current)
            if (current.get("completion_pending") is False
                    or (current.get("delivery") and not _cancels_pass(current))):
                return CompletionReplay.HANDLED
        record["completion_pending"] = True
        record = _write_record(path, record)
        return _replay_completion(path, record)


def _replay_completion(path: Path, record: RunRecord) -> CompletionReplay:
    """Keep an unresolved completion available for the next event."""
    if _result_push_failed(record.get("delivery", "")) and not _cancels_pass(record):
        return CompletionReplay.RETRY
    try:
        if _cancels_pass(record):
            if _complete_stopped_run(path, record) == CompletionReplay.RETRY:
                return CompletionReplay.RETRY
        elif _retired_worker_result(record):
            if _result_push_failed(deliver(record, command_result(record))):
                return CompletionReplay.RETRY
        else:
            if record.get("delivery") == ORPHAN_RESULT_PENDING:
                if _worker_result_pending(deliver(record, command_result(record))):
                    return CompletionReplay.RETRY
            if _complete_worker_dispatch(record) == CompletionReplay.RETRY:
                return CompletionReplay.RETRY
    except (Exception, SystemExit) as error:
        detail = " ".join(str(error).splitlines())
        with contextlib.suppress(SystemExit, worker_lifecycle.WorkerStateUnreadable):
            _ = _save_delivery(path, FAILED_RESULT_PUSH + detail, if_current=ORPHAN_RESULT_PENDING)
        print(f"anomaly: {record['run_id']}: completion event failed: {detail}", file=sys.stderr)
        return CompletionReplay.RETRY
    _clear_completion_pending(path)
    return CompletionReplay.HANDLED


def _complete_stopped_run(path: Path, record: RunRecord) -> CompletionReplay:
    """End the proved pane pass or deliver the ordinary stopped result."""
    from scripts.herdr import worker_panes

    run = Path(record["session_dir"])
    runs = _runs_dir(record["session_dir"])

    @contextlib.contextmanager
    def guard_ended() -> Generator[worker_panes.CloseAllowed | worker_panes.Kept | PaneStopDeferred]:
        # The caller holds the worker lock before it takes admission and the recorder lock.
        with (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            if not _stopped_group_has_ended(record):
                yield PaneStopDeferred(reason="the command group has no confirmed end")
                return
            now = time.time()
            for other in _run_record_paths(runs):
                if other == path:
                    continue
                raw = worker_lifecycle.read_json_object(other)
                if raw.get("staff_id") != record["staff_id"]:
                    continue
                if raw.get("status") == "running" and not _orphaned(other, raw, now) or (
                        raw.get("status") == "finished" and raw.get("stop_pass_requested") is True
                        and not _command_has_ended(raw, _integer(raw.get("pid")))):
                    yield worker_panes.Kept(f"another detached run can still be live: {other.stem}")
                    return
            yield worker_panes.CloseAllowed()

    stop_pass = cast(Callable[[Path, RunRecord, Callable[
        [], contextlib.AbstractContextManager[worker_panes.CloseAllowed | worker_panes.Kept | PaneStopDeferred]]],
        worker_panes.StoppedRunPassCompletion | worker_panes.Kept | worker_panes.NoOwningPanePass | PaneStopDeferred],
        import_module("scripts.herdr.turn_end").stop_pass)
    result = stop_pass(run, record, guard_ended)
    if isinstance(result, worker_panes.StoppedRunPassCompletion):
        _ = result.send_notice()
        _ = _save_delivery(path, f"finished: the pane pass {result.outcome}")
        return CompletionReplay.HANDLED
    if isinstance(result, dict):
        print(f"detached_runs: {record['run_id']}: pane cancellation is pending: {result['reason']}", file=sys.stderr)
        return CompletionReplay.RETRY
    if isinstance(result, worker_panes.Kept):
        print(f"detached_runs: {record['run_id']}: kept the pane pass: {result.blocker}", file=sys.stderr)
    if record.get("orphan_result_superseded"):
        _ = _save_delivery(path, SUPERSEDED_ORPHAN_DELIVERY)
        return CompletionReplay.HANDLED
    delivery = deliver(record, command_result(record))
    return CompletionReplay.RETRY if _worker_result_pending(delivery) else CompletionReplay.HANDLED


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


def _complete_worker_dispatch(record: RunRecord) -> CompletionReplay:
    """Recheck the pass after the finished record becomes visible."""
    member = _run_owner(record, staff.local_snapshot())
    if isinstance(member, staff.StaffRow) and member.kind == "worker":
        complete = cast(Callable[[staff.StaffId], CompletionReplay], import_module("scripts.herdr.turn_end").run_finished)
        return complete(member.id)
    return CompletionReplay.HANDLED


# Keep the record lock at its path until the record is gone.
_RUN_FILES = ("{}.log", "{}.scope", "{}.reap", "{}.completion.lock", ".{}.delivery.lock", "{}.stop-staged",
              "{}" + CONFIRMED_STOP_SUFFIX, "{}" + PENDING_STOP_SUFFIX,
              "{}.json", "{}.record.lock")


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
    """
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
                    or current.get("completion_pending") or _worker_result_pending(str(current.get("delivery", "")))):
                continue
            for name in _RUN_FILES:
                (runs / name.format(run_id)).unlink(missing_ok=True)
        removed.append(run_id)
    return removed


def _run_record_paths(runs: Path) -> list[Path]:
    """List run records; only an absent folder counts as empty."""
    try:
        with os.scandir(runs) as entries:
            return sorted(runs / entry.name for entry in entries if entry.name.endswith(".json"))
    except FileNotFoundError:
        return []


def live_twin(runs: Path, worker: str, command: list[str], tree_key: str) -> RunRecord | None:
    """A pending run for the same worker, command, folder and tree key.
    After an interrupt on 2026-10-21, hana_catalyst-inspectors started its lint run three times
    on one tree; each run took memory, the cargo token and build time."""
    if not tree_key:
        return None
    for path in _run_record_paths(runs):
        raw = worker_lifecycle.read_json_object(path)
        if (raw.get("worker") == worker and raw.get("command") == command and raw.get("tree_key") == tree_key
                and _run_is_pending(path, raw, lambda pid: detached_run_process.supervises(pid, path))):
            return cast(RunRecord, cast(object, raw))
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


def _registered_runs(runs: Path, identity: staff.StaffId) -> Generator[RunRecord]:
    for path in _run_record_paths(runs):
        if (owner := _record_owner(path)) and owner != identity:
            continue
        raw = worker_lifecycle.read_json_object(path)
        if _is_run_record(raw) and path.name == f"{raw['run_id']}.json":
            yield raw


class DeliveryTimeUnknown(Enum):
    UNRECORDED = "the run does not record when the worker received the result"


class WorkerResultDeferred(Enum):
    PENDING = "a finished run result still needs delivery"


DEFERRED_WORKER_RESULT = "deferred: "
ORPHAN_RESULT_PENDING = DEFERRED_WORKER_RESULT + "the supervisor ended before delivery"
FAILED_WORKER_SUBMISSION = "submission failed: "
UNCONFIRMED_WORKER_RESULT = "unconfirmed: "
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


def _needs_completion(record: RunRecord) -> bool:
    if _cancels_pass(record) and record.get("completion_pending"):
        return True
    return (not _result_push_failed(record.get("delivery", ""))
            and bool(record.get("completion_pending") or _retired_worker_result(record)))


def result_is_from_another_pass(record: Mapping[str, object], instance: object) -> bool:
    """Require two known pass IDs before rejecting a result."""
    linked = record.get("pane_dispatch_id")
    return (isinstance(linked, str) and bool(linked)
            and isinstance(instance, str) and bool(instance) and linked != instance)


def worker_results(runs: Path, worker: staff.StaffRow, *, since: float, pane_dispatch_id: str = "",
                   include_legacy_pass: bool = False,
                   ) -> tuple[dict[str, float | DeliveryTimeUnknown | WorkerResultDeferred], set[str]]:
    """Find finished results and identify results from other passes."""
    results: dict[str, float | DeliveryTimeUnknown | WorkerResultDeferred] = {}
    other_passes: set[str] = set()
    for record in _registered_runs(runs, worker.id):
        if record.get("staff_id") != worker.id:
            continue
        if result_is_from_another_pass(record, pane_dispatch_id):
            other_passes.add(record["run_id"])
            continue
        if record["started_at"] < since or record["status"] != "finished":
            continue
        if pane_dispatch_id and not include_legacy_pass and record.get("pane_dispatch_id") != pane_dispatch_id:
            continue
        if _worker_result_pending(record.get("delivery", "")):
            results[record["run_id"]] = WorkerResultDeferred.PENDING
        elif record.get("delivery", "").startswith((f"sent: worker {record['worker']} at ", f"sent: worker {worker.name} at ", "already delivered: ")):
            results[record["run_id"]] = record.get("delivered_at", DeliveryTimeUnknown.UNRECORDED)
    return results, other_passes


@dataclass(frozen=True)
class LiveRunReadError:
    path: Path
    error: OSError | ValueError
    staff_ids: frozenset[staff.StaffId] = frozenset()

    @override
    def __str__(self) -> str:
        if isinstance(self.error, worker_lifecycle.WorkerStateUnreadable):
            return str(self.error)
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
    """Read candidate records once and identify workers affected by read errors."""
    if not folders:
        return LiveRunEvidence((), ())
    supervisors = detached_run_process.supervisor_records()
    named: dict[Path, set[Path]] = {}
    if not isinstance(supervisors, detached_run_process.ProcessTableUnavailable):
        for path in supervisors:
            if path.parent.name == DETACHED_RUNS_DIR and path.parent.parent in folders:
                named.setdefault(path.parent.parent, set()).add(path)
    found: list[RunRecord] = []
    errors: list[LiveRunReadError] = []
    for folder, identities in folders.items():
        directory = folder / DETACHED_RUNS_DIR
        try:
            # Older stops can have a saved reason without a live supervisor or a stop marker.
            paths = set(_run_record_paths(directory)) | named.get(folder, set())
            paths.update(marker.with_suffix(".json") for marker in directory.glob("*" + PENDING_STOP_SUFFIX))
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
    # A stop can outlive its supervisor. The delivery process or reaper proves that its group ended.
    if raw.get("status") != "running":
        return False
    _recover_indexed_stop(path, raw)
    return ("stop_reason" in raw or raw.get("stop_pass_requested") is True
            or path.with_suffix(PENDING_STOP_SUFFIX).exists() or supervisor_owns(_integer(raw.get("pid"))))


def _live_runs(runs: Path, owns: Callable[[dict[str, object]], bool],
               identities: frozenset[staff.StaffId]) -> LiveRunEvidence:
    try:
        paths = _run_record_paths(runs)
    except OSError as error:
        return LiveRunEvidence((), (LiveRunReadError(runs, error, identities),))
    return _read_live_runs(paths, owns, detached_run_process.supervises, identities)


def _valid_live_record(path: Path, raw: dict[str, object]) -> bool:
    record = raw
    if not _record_owner(path) and "staff_id" not in raw:
        # Older records saved the worker name without a staff ID.
        record = {**raw, "staff_id": ""}
    if _is_run_record(record):
        return path.name == f"{record['run_id']}.json"
    # The old stop format has exactly these four fields.
    match raw:
        case {"worker": str(name), "pid": int(pid), "status": "running", "stop_reason": str()}:
            return len(raw) == 4 and bool(name) and not isinstance(pid, bool)
        case _:
            return False


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
                if path in supervised_paths:
                    raise worker_lifecycle.WorkerStateUnreadable(path, "the live supervisor record is missing")
                continue
            if staff_names is not None and not _valid_live_record(path, raw):
                raise worker_lifecycle.WorkerStateUnreadable(path, "invalid detached run record")
            owner = raw.get("staff_id")
            if staff_names is not None and identity and owner != identity:
                raise worker_lifecycle.WorkerStateUnreadable(path, "detached run owner does not match the filename")
            legacy_owners = (tuple(member for member in identities if staff_names.get(member) == raw.get("worker"))
                             if staff_names is not None and not owner else ())
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


SCOPE_REFUSAL = ("detached_runs: Start with systemd-run --scope or systemd-run --user --scope. "
                 + "Use the exact option names in that order.")


def _uses_unsupported_systemd_run(command: list[str]) -> bool:
    """Require an exact scope prefix when systemd-run is the program."""
    index = 0
    while index < len(command) and Path(command[index]).name == "env":
        index += 1
        while index < len(command) and command[index] in ("-u", "--unset"):
            index += 2
        if index < len(command) and command[index].startswith("-"):
            return any("systemd-run" in argument for argument in command[index:])
        while index < len(command) and "=" in command[index]:
            index += 1
    return (index < len(command) and Path(command[index]).name == "systemd-run"
            and command[index + 1:index + 2] != ["--scope"]
            and command[index + 1:index + 3] != ["--user", "--scope"])


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
    name = str(getattr(args, "to", "") or "")
    caller = caller_from_environment(dict(os.environ))
    reference = ""
    if isinstance(caller, PaneCaller):
        if name and name != caller.name:
            print(f"detached_runs: --to {name} ignored; run belongs to worker {caller.name}", file=sys.stderr)
        session_dir, name = caller.session_dir, caller.name
        reference = caller.staff_id
    elif isinstance(caller, DirectorCaller) and session_dir and not name:
        name, reference = caller.name, caller.staff_id
    else:
        if bool(session_dir) != bool(name):
            print("detached_runs: --session-dir and --to go together", file=sys.stderr)
            return 2
        if not session_dir:
            return NOT_A_DETACHED_WORKER
        name = seat_name(session_dir, name)
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
        with (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            reaped = _reap_orphans_locked(runs)
            if twin := live_twin(runs, name, command, tree_key):
                started = time.strftime("%H:%M:%S", time.localtime(twin["started_at"]))
                print(f"run {twin['run_id']} is already running for {name} on these files (started {started}, "
                      + f"log {twin['log']}); started nothing; its result arrives as your next message")
                return 0
            try:
                limit = conf.read("detached_runs").whole("LIVE_RUN_LIMIT")
            except conf.Unusable as error:
                print(f"detached_runs: {error}", file=sys.stderr)
                return 2
            active = live_runs(runs, name)
            if len(active) >= limit:
                details = "\n".join(f"  {run['run_id']}: {run['label']}" for run in active)
                print(f"detached_runs: live-run limit {limit} reached for {name}; started nothing.\n"
                      + f"Live runs:\n{details}\n"
                      + "Wait for a run's result; it arrives as your next message. "
                      + "Or put more files in one run.", file=sys.stderr)
                return LIVE_RUN_LIMIT_REACHED
            run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(2)}"
            identity = reference or _worker_reference(session_dir, name)
            if identity and not isinstance(caller, DirectorCaller):
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

            if isinstance(caller, DirectorCaller) and reference:
                record["pane_dispatch_id"] = ""
            elif record["staff_id"]:
                dispatch = progress_history.active_dispatch(staff.StaffId(record["staff_id"]), Path(session_dir))
                if isinstance(dispatch, progress_history.ActiveDispatch):
                    record["pane_dispatch_id"] = dispatch.instance_id
                elif isinstance(dispatch, progress_history.NoActiveDispatch):
                    record["pane_dispatch_id"] = ""
                else:
                    record["pane_dispatch_id"] = {"unavailable": dispatch.reason}
            else:
                record["pane_dispatch_id"] = {"unavailable": "the worker staff row is unavailable"}
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
    limit = conf.read("detached_runs").whole("RESULT_LINE_CHARS")
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


def _was_stopped(record: RunRecord) -> bool:
    return "stop_reason" in record or record.get("stop_escalated") is True


def stop_signal_line(record: RunRecord) -> str:
    return "Stop signal: SIGKILL" if record.get("stop_escalated") else ""


def result_text(record: RunRecord, result: CommandResult) -> str:
    exit_code = result.code if isinstance(result, CompletedCommand) else result
    stop_reason = record.get("stop_reason", "")
    if _was_stopped(record):
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
    if signal_line := stop_signal_line(record):
        lines.append(signal_line)
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
    tail_heading = f"\n\nLast log lines (lines over {limits.whole('RESULT_LINE_CHARS')} characters are cut):\n"
    tail = _tail(Path(record["log"]), RESULT_TAIL_LINES,
                 limits.whole("RESULT_TEXT_CHARS") - len(header) - len(tail_heading))
    if cancellation:
        tail = [line for line in tail if line != _cut_line(cancellation)]
    body = tail_heading + "\n".join(tail) if tail else ""
    label_budget = max(0, limits.whole("RESULT_TEXT_CHARS") - len(header) - len(body))
    lines[0] += record["label"][:label_budget]
    header = "\n".join(lines)
    return header + body


@contextlib.contextmanager
def _delivery_lock(path: Path) -> Generator[None]:
    with path.with_name(f".{path.stem}.delivery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class _WorkerPassDeliveryBlocked(RuntimeError):
    """Stop pane input and save the delivery status."""


def _result_received(member: staff.StaffRow, membership: staff.StaffSnapshot, marker: str) -> bool:
    from scripts.message import send

    return send.WorkerResultReceipt(marker).received(member, membership)


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
def _result_send_checkpoint(path: Path, checkpoint: str) -> Generator[None]:
    """Save the checkpoint before sending and keep any send error with it."""
    _ = _save_delivery(path, checkpoint)
    try:
        yield
    except (Exception, SystemExit) as error:
        saved = _read_record(path).get("delivery", "")
        detail = " ".join(str(error).splitlines()) or type(error).__name__
        if saved.startswith(UNCONFIRMED_WORKER_RESULT):
            _ = _save_delivery(path, f"{saved}: {detail}", if_current=saved)
        raise


class WorkerResultInput(Enum):
    TYPE = "submit the result to an idle worker"
    TURN_ENDED = "wait for the pane to become idle after the turn ends"


def deliver(record: RunRecord, result: CommandResult, *,
            worker_input: WorkerResultInput = WorkerResultInput.TYPE) -> str:
    """Deliver the command result to its current worker or director."""
    runs = _runs_dir(record["session_dir"])
    path = runs / f"{record['run_id']}.json"
    self_stop = record.get("self_stop") is True
    exit_code = result.code if isinstance(result, CompletedCommand) else result
    done_file = record["done_message_file"]
    stopped = _was_stopped(record)
    if done_file and exit_code not in (0, SUPERSEDED_STATUS, TREE_CHANGED_STATUS) and not stopped:
        return _save_delivery(path, f"nothing delivered: {_exit_words(result)} with a done message")
    if done_file and exit_code == 0 and not stopped:
        try:
            text = Path(done_file).read_text(encoding="utf-8")
        except OSError as exc:
            text = f"Run finished: {record['label']}\nResult: PASS (exit 0)\n(done message {done_file} unreadable: {exc})"
    else:
        text = result_text(record, result)
    from scripts.message import send

    marker = f"Detached run: {record['run_id']}"

    def save_push(sent: send.Result) -> str:
        detail = f"{sent.outcome}: {sent.detail}"
        if isinstance(sent, (send.UncertainRelayDelivery, send.WorkerMessageUnconfirmed)):
            detail = UNCONFIRMED_RESULT_PUSH + detail
        elif sent.outcome == "refused" or isinstance(sent, send.RefusedRelayRequest):
            detail = FAILED_RESULT_PUSH + detail
        elif sent.outcome in ("failed", "retry", "pending", "held"):
            detail = UNCONFIRMED_RESULT_PUSH + detail
        return _save_delivery(path, detail)

    def save_deferral(detail: str) -> str:
        return _save_delivery(path, saved if saved.startswith((FAILED_RESULT_PUSH, FAILED_WORKER_SUBMISSION)) else detail)

    with _delivery_lock(path):
        saved = _read_record(path).get("delivery", "")
        unconfirmed = saved.startswith(UNCONFIRMED_WORKER_RESULT)
        try:
            membership = staff.snapshot()
        except staff.StaffUnreadable as error:
            if unconfirmed or (saved and not _worker_result_pending(saved)):
                return saved
            return _save_delivery(path, FAILED_RESULT_PUSH + str(error))
        if unconfirmed and _unconfirmed_result_received(record, membership, saved):
            return _save_delivery(path, f"already delivered: {record['run_id']}")
        member = _run_owner(record, membership)
        if unconfirmed and (isinstance(member, RunOwnerUnavailable) or member.kind != "worker"):
            return saved
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
            if saved and not _worker_result_pending(saved):
                return saved
            if isinstance(member, RunOwnerUnavailable):
                try:
                    member = worker_admission.parent(membership, run=Path(record["director_dir"] or record["session_dir"]))
                except (worker_admission.RunOwnerUnavailable, staff.StaffUnreadable) as error:
                    if isinstance(error, staff.StaffUnreadable) or record.get("ended") == "supervisor gone":
                        return _save_delivery(path, FAILED_RESULT_PUSH + f"director unavailable: {error}")
                    return save_deferral(DEFERRED_WORKER_RESULT + f"director unavailable: {error}")
            options = send.parse(["--to", str(member.id), "--from", "detached-runs",
                                  "--timeout", str(send_budget.seconds()), "--text", marker + "\n" + text])
            with _result_send_checkpoint(path, checkpoint), staff.supplied_snapshot(membership):
                sent = send.send(options)
            return save_push(sent)
        reason = ""
        if not unconfirmed:
            try:
                if _result_received(member, membership, marker):
                    return _save_delivery(path, f"already delivered: {record['run_id']}")
            except (OSError, ValueError) as error:
                reason = f"worker transcript is unavailable: {error}"
        if saved and not _worker_result_pending(saved):
            return saved
        worker = member
        pane_checkpoint = checkpoint if saved.startswith(FAILED_RESULT_PUSH) else (
            UNCONFIRMED_WORKER_RESULT + "the prompt may have sent input")

        def check_pass() -> None:
            linked = record.get("pane_dispatch_id")
            if isinstance(linked, str) and linked:
                from scripts.direct import recorder_state
                from scripts.herdr import worker_panes

                try:
                    run = worker_panes.worker_run(worker, record=membership)
                    instance = worker_panes.worker_pass(worker, run, record=membership).get("instance_id")
                except (OSError, ValueError, recorder_state.OldRecorderState) as error:
                    raise _WorkerPassDeliveryBlocked(
                        DEFERRED_WORKER_RESULT + f"worker pass is unavailable: {error}") from error
                if result_is_from_another_pass(record, instance):
                    raise _WorkerPassDeliveryBlocked(OTHER_PASS_DELIVERY)

        def before_input() -> None:
            check_pass()
            _ = _save_delivery(path, pane_checkpoint)

        receipt = send.WorkerResultReceipt(marker, wait_for_idle=worker_input == WorkerResultInput.TURN_ENDED,
            worker_only=self_stop, before_input=before_input)
        try:
            check_pass()
            if unconfirmed:
                return saved
            with _result_send_checkpoint(path, checkpoint):
                delivery = send.deliver_worker_text(worker, membership, marker + "\n" + text,
                    sender="detached-runs", timeout=send_budget.seconds(), reason=reason, receipt=receipt)
        except _WorkerPassDeliveryBlocked as blocked:
            if unconfirmed:
                return saved
            status = str(blocked)
            if status == OTHER_PASS_DELIVERY:
                return _save_delivery(path, status)
            return save_deferral(status)
        if isinstance(delivery, send.WorkerResultNotSubmitted):
            return save_deferral(FAILED_WORKER_SUBMISSION + delivery.detail)
        if isinstance(delivery, send.WorkerResultUnconfirmed):
            prefix = UNCONFIRMED_RESULT_PUSH if saved.startswith(FAILED_RESULT_PUSH) else UNCONFIRMED_WORKER_RESULT
            return _save_delivery(path, prefix + delivery.detail)
        if delivery.outcome == "retry":
            return save_deferral(DEFERRED_WORKER_RESULT + delivery.detail)
        return save_push(delivery)


def _save_delivery(path: Path, delivery: str, *, if_current: str | None = None) -> str:
    with record_lock(path):
        record = _read_record(path)
        current = record.get("delivery", "")
        if if_current is not None and if_current != current:
            return current
        record["delivery"] = delivery
        if not _worker_result_pending(delivery) and "delivered_at" not in record:
            record["delivered_at"] = time.time()
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
    return delivery


class WorkerResultReplay(Enum):
    NONE = "no result needs submission"
    SUBMITTED = "the worker accepted the result"
    RETRY = "the worker has not accepted the result"


def submit_finished_worker_result(run: Path, worker: staff.StaffRow, *,
                                  worker_input: WorkerResultInput) -> WorkerResultReplay:
    """Submit one waiting result. The next turn can receive the next result."""
    runs = run / DETACHED_RUNS_DIR
    for record in _registered_runs(runs, worker.id):
        if (record["status"] != "finished" or record.get("staff_id") != worker.id
                or (_cancels_pass(record) and record.get("completion_pending"))
                or not _worker_result_pending(record.get("delivery", ""))):
            continue
        delivery = deliver(record, command_result(record), worker_input=worker_input)
        if _worker_result_pending(delivery):
            return WorkerResultReplay.RETRY
        if delivery.startswith(f"sent: worker {worker.name} at "):
            return WorkerResultReplay.SUBMITTED
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
    limit = settings.whole("RESULT_PUSH_RETRIES")
    delay = settings.number("RESULT_PUSH_RETRY_SECONDS")
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
            if _cancels_pass(record):
                _ = _finish_record(path, record)
            else:
                _ = deliver(record, command_result(record))
        except (Exception, SystemExit) as error:
            detail = " ".join(str(error).splitlines())
            print(f"detached_runs: {record['run_id']}: result retry failed: {detail}", file=sys.stderr)
    _alert_failed_push(path)


def command_deliver(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", ""))).resolve()
    record = _read_record(record_path)
    published_stop = _was_stopped(record) and _integer(record.get("command_group")) > 0
    if (published_stop or record.get("stop_pass_requested")) and record["status"] != "finished":
        if "exit_code" not in record:
            print(f"detached_runs: {record['run_id']}: the stopped command has no confirmed exit", file=sys.stderr)
            return 1
        if not _stopped_group_has_ended(record):
            print(f"detached_runs: {record['run_id']}: the command group has no confirmed end", file=sys.stderr)
            return 1
    if _cancels_pass(record):
        if record["status"] != "finished":
            record["status"] = "finished"
            _ = record.setdefault("finished_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return 0 if _finish_record(record_path, record) == CompletionReplay.HANDLED else 1
    delivery = deliver(record, command_result(record))
    if _result_push_failed(delivery):
        print(f"detached_runs: {record['run_id']}: {delivery}", file=sys.stderr)
        return 1
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


def scope_prefix(marker: Path, environment: dict[str, str], *,
                 kind: DetachedRunKind = DetachedRunKind.DETECT) -> list[str]:
    """Start the command in builds-detached.slice when a scope is available."""
    runtime = environment.get("XDG_RUNTIME_DIR", "")
    if (environment.get("BUILDLOG_SCOPE", "1") == "0" or not runtime
            or not Path(runtime, "systemd", "private").is_socket() or shutil.which("systemd-run") is None):
        return []
    run_id = marker.stem.partition("@")[0]
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
            "--slice=builds-detached.slice", f"--unit=detached-{kind}-{run_id}.scope",
            "--", "/bin/sh", "-c", SCOPE_SCRIPT, str(marker)]


def _child_has_exited(pid: int, saved_start: str) -> bool:
    """Observe exit without reaping the process."""
    if not hasattr(os, "waitid"):
        observed = _saved_process(pid, saved_start)
        if isinstance(observed, (process_start.Gone, SavedProcessIdReused)):
            raise ChildProcessError(pid)
        return isinstance(observed, process_start.Zombie)
    status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    if status is None:
        return False
    if status.si_code == os.CLD_STOPPED:
        # macOS can report a stopped child with WEXITED. Consume only that event.
        _ = os.waitid(os.P_PID, pid, os.WSTOPPED | os.WNOHANG)
    return status.si_code in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED)


def _run(command: list[str], environment: dict[str, str], log: Path) -> int:
    """Publish the command identity and group, then run the command in its own session.

    A stop signals the command group only, so the supervisor stays alive to record and deliver.
    The gate input stays open until the command ends. Then the supervisor writes "done" to the watcher.
    """
    settings = conf.read("detached_runs")
    grace = settings.whole("STOP_GRACE_SECONDS")
    alert_after = settings.whole("STOP_GROUP_ALERT_SECONDS")
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
    with child.stdin as gate:
        started = process_start.process_start(child.pid)
        if not isinstance(started, process_start.Started):
            with log.open("a", encoding="utf-8") as output:
                _ = output.write("detached_runs: command identity is unavailable; command did not start\n")
            # The gate shell reads the end of its input and exits 125.
            gate.close()
            return _exit_status(child.wait())
        path = Path(environment["UNIT_DETACHED_RECORD"])
        try:
            if _child_has_exited(child.pid, started.start):
                return _exit_status(child.wait())
            with record_lock(path):
                record = _read_record(path)
                record.update(command_pid=child.pid, command_group=os.getpgid(child.pid),
                              command_started=started.start)
                supervisor = process_start.process_start(os.getpid())
                if isinstance(supervisor, process_start.Started):
                    record["supervisor_started"] = supervisor.start
                write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
                _send_gate_line(gate, b"start\n")
            return _exit_status(_wait_for_command(child, gate, path, grace, alert_after,
                                                  lambda: _child_has_exited(child.pid, started.start)))
        except (ChildProcessError, ProcessLookupError):
            _send_gate_line(gate, b"done\n")
            raise SystemExit(f"detached_runs: {path}: the command leader was already reaped") from None


def _wait_for_command(child: subprocess.Popen[bytes], gate: IO[bytes], path: Path, grace: float,
                      alert_after: float, leader_exited: Callable[[], bool]) -> int:
    """Escalate a stop before observing exit. Reap only after the group ends."""
    last_unstopped_check = time.monotonic()
    leader_has_exited = False
    while True:
        delay = STOPPED_GROUP_CHECK_SECONDS
        with record_lock(path):
            record = _read_record(path)
            now = time.monotonic()
            if not _was_stopped(record):
                last_unstopped_check = now
                leader_has_exited = leader_exited()
                if leader_has_exited:
                    record = _recover_staged_stop(path, record)
                    if not _was_stopped(record):
                        _send_gate_line(gate, b"done\n")
                        return child.wait()
            if _was_stopped(record):
                remaining = record.get("stop_requested_at", last_unstopped_check) + grace - now
                if remaining <= 0:
                    if record.get("command_pid") != child.pid or record.get("command_group") != child.pid:
                        _send_gate_line(gate, b"done\n")
                        raise SystemExit(f"detached_runs: {path}: the command group identity changed")
                    if not record.get("stop_escalated"):
                        with contextlib.suppress(ProcessLookupError):
                            process_group.send_process_group(child.pid, signal.SIGKILL)
                            record["stop_escalated"] = True
                            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
                    break
                delay = min(delay, remaining)
        time.sleep(delay)

    alert_at = time.monotonic() + alert_after
    alerted = False
    if leader_has_exited:
        _send_gate_line(gate, b"done\n")
    while True:
        if not leader_has_exited and leader_exited():
            leader_has_exited = True
            _send_gate_line(gate, b"done\n")
        if leader_has_exited and not _group_is_live(child.pid):
            break
        if not alerted and time.monotonic() >= alert_at:
            unit_runs.report_anomaly(f"anomaly: {path}: the command group end is still unconfirmed after escalation")
            alerted = True
        time.sleep(STOPPED_GROUP_CHECK_SECONDS)
    returncode = child.wait()
    with record_lock(path):
        record = _recover_staged_stop(path, _read_record(path))
        record["stop_group_end"] = "confirmed"
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
    return returncode


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


def command_supervise(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", "")))
    with (record_path.parent / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        record = _read_record(record_path)
        if record["pid"] == 0:
            record["pid"] = os.getpid()
            record = _write_record(record_path, record)
    environment = dict(os.environ)
    from scripts.lib.python_startup.sitecustomize import inherited_paths

    paths = inherited_paths(environment.get("PYTHONPATH", ""))
    if paths:
        environment["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        _ = environment.pop("PYTHONPATH", None)
    environment.update({
        "UNIT_DETACHED_RUN": "1",
        "UNIT_RUN_DIR": record["session_dir"],
        "UNIT_TEAM_ROLE": record["worker"].rsplit("-", 1)[-1],
        "UNIT_DETACHED_RECORD": str(record_path.resolve()),
        "BUILDLOG_OWNER_STAFF_ID": record["staff_id"],
    })
    log = Path(record["log"])
    marker = log.with_suffix(".scope")
    try:
        kind = DetachedRunKind(record.get("kind", DetachedRunKind.DETECT))
    except ValueError:
        kind = DetachedRunKind.DETECT
    prefix = scope_prefix(marker, environment, kind=kind)
    with _completion_sources(Path(__file__).resolve().parents[1]):
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            with log.open("a", encoding="utf-8") as output:
                _ = output.write("detached_runs: the supervisor cannot retain the leader with SIGCHLD ignored\n")
            exit_code = 125
            prefix = []
        else:
            exit_code = _run(prefix + record["command"], environment, log)
        with record_lock(record_path):
            record = _recover_staged_stop(record_path, _read_record(record_path))
            retry_without_scope = bool(prefix) and not marker.exists() and not _was_stopped(record)
        if retry_without_scope:
            exit_code = _run(record["command"], environment, log)
            with record_lock(record_path):
                record = _recover_staged_stop(record_path, _read_record(record_path))
        marker.unlink(missing_ok=True)
        # Deliver before marking finished: a launcher that reads the run as finished closes its inbox
        # unless the result is already held.
        finished_at = record.get("finished_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        record.update(exit_code=exit_code, finished_at=finished_at)
        record = _write_record(record_path, record)
        # Other members of the command group can remain after its leader exits.
        while record.get("stop_pass_requested") and not _stopped_group_has_ended(record):
            time.sleep(STOPPED_GROUP_CHECK_SECONDS)
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
        record.update(status="finished", exit_code=exit_code, finished_at=finished_at)
        completion = _finish_record(record_path, record)
        saved = worker_lifecycle.read_json_object(record_path)
        if _is_run_record(saved) and _needs_push_retry(saved):
            _retry_failed_push(record_path)
            saved = worker_lifecycle.read_json_object(record_path)
            if _is_run_record(saved) and _needs_completion(saved):
                completion = _finish_record(record_path, saved)
        if _result_push_failed(str(saved.get("delivery", ""))):
            return 1
        if cancelled and completion != CompletionReplay.HANDLED:
            return 1
    return 0


def command_supervisor_gone(args: argparse.Namespace) -> int:
    """Stop the command group of a run whose supervisor ended, then deliver its STOPPED result.

    The gate watcher runs this when the supervisor pipe closes without "done".
    """
    path = Path(str(getattr(args, "record", "")))
    # Leave the command group, so its TERM spares this watcher.
    os.setsid()
    command = command_group(path)
    if command is None:
        return 0
    # POSIX reuses no group ID while the group has members, so a live group is this command's.
    # A command that ended before its supervisor needs no stop, only the reap.
    if _group_is_live(command.group):
        with record_lock(path):
            record = worker_lifecycle.read_json_object(path)
            if (not _is_run_record(record) or path.name != f"{record['run_id']}.json"
                    or record["status"] != "running"):
                return 0
            path.with_suffix(PENDING_STOP_SUFFIX).touch(exist_ok=True)
            _ = record.setdefault("stop_reason", SUPERVISOR_GONE_REASON)
            write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        with contextlib.suppress(ProcessLookupError):
            process_group.send_process_group(command.group, signal.SIGTERM)
        _wait_for_leader(command)
    _ = reap_orphans(str(path.resolve().parent.parent))
    return 0


def _wait_for_leader(command: CommandGroup) -> None:
    """Wait for the leader to save its exit status and exit."""
    with contextlib.suppress(ProcessLookupError):
        if sys.platform == "darwin":
            with contextlib.closing(select.kqueue()) as watcher:  # pyright: ignore[reportUnreachable]
                event = select.kevent(command.group, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                      select.KQ_NOTE_EXIT)
                _ = watcher.control([event], 0)
                _wait_for_leader_exit(command, lambda timeout: bool(watcher.control(None, 1, timeout)))
        else:
            leader = os.pidfd_open(command.group)  # pyright: ignore[reportUnreachable]
            try:
                _wait_for_leader_exit(command, lambda timeout: bool(select.select([leader], [], [], timeout)[0]))
            finally:
                os.close(leader)


def _wait_for_leader_exit(command: CommandGroup, wait_for_exit: Callable[[float | None], bool]) -> None:
    """Check the identity after the exit watch starts, so PID reuse cannot delay the reap."""
    deadline = time.monotonic() + LEADER_IDENTITY_TIMEOUT_SECONDS
    while True:
        identity = _saved_process(command.group, command.started)
        if isinstance(identity, process_start.Started):
            _ = wait_for_exit(None)
            return
        if not isinstance(identity, process_start.Unreadable):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            unit_runs.report_anomaly(f"anomaly: detached command leader {command.group}: the start identity is unreadable")
            return
        if wait_for_exit(min(remaining, STOPPED_GROUP_CHECK_SECONDS)):
            return


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
    if stop_command(path, str(getattr(args, "reason", ""))):
        print(f"stopped {path.stem}: its result comes from the supervisor")
        return 0
    raw = worker_lifecycle.read_json_object(path)
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


def _stop_reason(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("the stop reason must contain text")
    return value


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
    _ = stop.add_argument("--reason", required=True, type=_stop_reason, help="the Stop reason line of the result")
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
    sys.exit(main())
