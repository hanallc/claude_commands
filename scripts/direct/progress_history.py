#!/usr/bin/env python3
"""Durable unit progress events, headers, and calibration statistics."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import io
import itertools
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Generator, Iterable, Mapping
from contextlib import contextmanager, redirect_stderr
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Literal, NoReturn, Protocol, TypedDict, cast

from scripts.agents import worker_lifecycle  # noqa: E402
from scripts.direct import pass_record as pass_recorder
from scripts.direct import plan_history, recorder_state  # noqa: E402
from scripts.direct.recorder_state import OPEN_PHASES_KEY as OPEN_PHASES_KEY  # noqa: E402
from scripts.lib import git_files, unit_runs
from scripts.lib.atomic_file import write_text_atomic  # noqa: E402
from scripts.lib.intervals import merged_spans  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.production import session_staff, staff, unit_config  # noqa: E402
# The name events carried before the rename: every history written before it, and a run still open.
FORMER_SKILL_NAME = "plan-delegate"
MIN_CALIBRATION_SAMPLES = 5
# How long a stretch with no pass or activity open still counts as phase work: the unit
# director reviews, gates and writes the next prompt between dispatches. Past it the phase
# waited, and an overnight wait counted in full turned a phase with an hour left into one
# finishing the next evening (herdr-trial, 2026-10-15).
IDLE_GRACE_SECONDS = 30 * 60
# A worker finished this long ago drops out of the report's worker lines.
FINISHED_WORKER_SECONDS = 10 * 60
# How far apart two entered percentages may sit and still calibrate each other.
# The entered percent is itself off by about plan_history.DEFAULT_PERCENT_SPREAD, so history
# ten points either side describes the same moment of a phase as well as history
# at the exact number does. An exact match handed each tick its own handful of
# reports: four ticks entering 78, 82, 84 and 86 drew suggestions of 40, 20, 55
# and 15, and the ETA built on them moved by more than nine hours between ticks.
CALIBRATION_WINDOW = 10
# The state that describes the current phase alone. A phase a newer one
# displaces keeps its own copy under PARKED_KEY, and gets it back when the newer
# one finishes and it is current again.
PHASE_SCOPED_KEYS = (
    pass_recorder.ARMED_REVIEW_KEY,
    "phase_last_percent",
    "phase_percent_started_at",
    "last_percent",
    "percent_started_at",
    "pending_calibration",
)
PARKED_KEY = "parked"
PROJECT_STARTED_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+\*\*Project started:\*\*[ \t]*(?P<value>.+?)[ \t]*$",
    re.MULTILINE,
)
PROJECT_PATTERN = re.compile(
    r"^[ \t]*-[ \t]+\*\*Project:\*\*[^\n]*$",
    re.MULTILINE,
)

# A percentage is an estimate; a stage is a fact. These ceilings keep an
# optimistic estimate from claiming a gate that has not run — the failure mode
# was a phase sitting at 99% while its review loop was still unconverged.
PROGRESS_CAPS: dict[str, int] = {
    "implementation": 75,
    "initial_review": 85,
    "open_findings": 90,
    "closure": 95,
    "checkpoint": 98,
    "complete": 100,
}
PROJECT_CAP_BEFORE_COMPLETE = 99

WAIT_KINDS = (
    "WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
    "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity",
)

HOLDER_WAIT_KINDS = ("WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "ReadyWithoutCapacity")
# A phase does no work while it waits on the user or a supplier, so active phase time leaves these waits out.
PAUSE_WAIT_KINDS = ("WaitingForUser", "WaitingForSupplier")


class CalibrationSample(TypedDict):
    percent: int
    raw_percent: int
    cap_stage: str
    suggested_percent: int
    decision_source: str
    override_reason: str
    pass_kind: str
    main_model: str
    main_effort: str
    called_model: str
    called_effort: str
    hold_seconds: int
    unchanged_before_report_seconds: int
    remaining_at_report_seconds: int
    temporal_percent: float
    raw_bias_percentage_points: float
    suggested_bias_percentage_points: float
    reported_bias_percentage_points: float
    implied_total_error_seconds: float


class ProjectTiming(TypedDict):
    started_at: float
    source: str
    plan_doc: str


class StageWindow(TypedDict):
    """One launcher pass or one unit-director activity.

    Two tables read these. `timeline` renders one row apiece, which is the
    provenance view. The progress report groups them by `fix_round` and lays the
    workers out as columns, which is why `slot` is carried here rather than
    recovered later: a window with no worker is a unit-director activity, and it is
    the absence of a worker that tells the two apart.
    """

    instance_id: str
    slot: str
    kind: str
    fix_round: int
    label: str
    # What the window actually ran, for an activity: the `--activity` text the
    # unit director opened it with ("test hana"). Empty for passes, whose work is
    # described by kind and worker. The gate notes under the round table are the
    # one reader of it -- the label alone renders a block of verification
    # commands as identical rows.
    text: str
    started_at: float
    elapsed: int
    status: str
    result: str
    worker: str
    main: str


class FindingTally(TypedDict):
    opened: int
    landed: int
    accepted: int
    still_open: int
    reopened: int
# models_config.sh reads the same env override, so a launcher resolving against
# one table can never be stamped with the digest of another.


class ContractFileHash(TypedDict):
    state: Literal["Recorded"]
    sha256: str


class ContractFileUnavailable(TypedDict):
    state: Literal["Missing", "NotRecorded"]
    reason: str


class ContractMainHead(TypedDict):
    state: Literal["Recorded"]
    commit: str


class ContractHeadUnavailable(TypedDict):
    state: Literal["NotRecorded"]
    reason: str


class ContractRevision(TypedDict):
    main_head: ContractMainHead | ContractHeadUnavailable
    direct_md: ContractFileHash | ContractFileUnavailable
    files: dict[str, ContractFileHash | ContractFileUnavailable]


def _contract_file(path: Path) -> ContractFileHash | ContractFileUnavailable:
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return ContractFileUnavailable(state="Missing", reason=f"Missing file: {path}")
    except OSError as error:
        return ContractFileUnavailable(state="NotRecorded", reason=str(error))
    return ContractFileHash(state="Recorded", sha256=hashlib.sha256(contents).hexdigest())


def _contract_revision() -> ContractRevision:
    """Read the deployed main checkout afresh at each phase boundary."""
    from scripts.direct.tag_inventory import contract_table_paths

    root = Path.home() / ".claude"
    head: ContractMainHead | ContractHeadUnavailable
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        head = ContractMainHead(state="Recorded", commit=result.stdout.strip())
    except (OSError, subprocess.SubprocessError) as error:
        head = ContractHeadUnavailable(state="NotRecorded", reason=str(error))
    direct = root / "commands/unit/direct.md"
    files: dict[str, ContractFileHash | ContractFileUnavailable] = {}
    try:
        contents = direct.read_bytes()
        direct_hash: ContractFileHash | ContractFileUnavailable = ContractFileHash(
            state="Recorded", sha256=hashlib.sha256(contents).hexdigest(),
        )
        for path in contract_table_paths(contents.decode("utf-8")):
            files[path] = _contract_file(root / path)
    except FileNotFoundError:
        direct_hash = ContractFileUnavailable(state="Missing", reason=f"Missing file: {direct}")
    except (OSError, UnicodeError) as error:
        direct_hash = ContractFileUnavailable(state="NotRecorded", reason=str(error))
    return ContractRevision(main_head=head, direct_md=direct_hash, files=files)


def _refuse_eta(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _local_eta_epoch(value: str, option: str) -> float:
    zone = user_zone()
    if re.fullmatch(r"\d{2}:\d{2}", value):
        today = datetime.fromtimestamp(pass_recorder.now_epoch(), zone)
        value = f"{today:%Y-%m-%d}T{value}"
    layout = "%Y-%m-%dT%H:%M"
    try:
        parsed = datetime.strptime(value, layout)
    except ValueError:
        _refuse_eta(f"{option} must use YYYY-MM-DDTHH:MM")
    if parsed.strftime(layout) != value:
        _refuse_eta(f"{option} must use YYYY-MM-DDTHH:MM")
    epoch = parsed.replace(tzinfo=zone).timestamp()
    if datetime.fromtimestamp(epoch, zone).strftime(layout) != value:
        _refuse_eta(f"{option} is a time the local clock skips")
    return epoch


def set_unit(session_dir: Path, production: str, unit: str) -> None:
    """Persist unit identity without changing the run's history or workers."""
    if any(not value or value in (".", "..") or "/" in value or "\\" in value
           for value in (production, unit)):
        raise ValueError("invalid production or unit identity")
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        worker_lifecycle.acquire_flock(lock, mode=fcntl.LOCK_EX)
        state = pass_recorder.read_state(session_dir)
        _set_owner(state, production, unit)
        pass_recorder.write_state(session_dir, state)


def _set_unit(args: argparse.Namespace) -> None:
    # main already holds this lock; the public API takes it for other callers.
    state = pass_recorder.read_state(pass_recorder.argument_session_dir(args))
    production, unit = pass_recorder.arg_string(args, "production"), pass_recorder.arg_string(args, "unit")
    if any(not value or value in (".", "..") or "/" in value or "\\" in value
           for value in (production, unit)):
        raise SystemExit("invalid production or unit identity")
    _set_owner(state, production, unit)
    pass_recorder.write_state(pass_recorder.argument_session_dir(args), state)


def _set_owner(state: dict[str, object], production: str, unit: str) -> None:
    """Resolve the command's names once and store the fixed unit id."""
    if state.get(unit_runs.OWNER_FIELD):
        return
    record = staff.initial_snapshot()
    parents = {row.id for row in record.find(kind="showrunner")
               if row.name == production}
    matches = [row for row in record.find(kind="unit")
               if isinstance(row.parent, staff.ReportsTo) and row.parent.id in parents and row.name == unit]
    if len(matches) != 1:
        raise ValueError(f"{production}/{unit}: no unique unit staff row")
    state[unit_runs.OWNER_FIELD] = matches[0].id
    _ = state.pop("production", None)
    _ = state.pop("unit", None)


def move_run(_history_file: Path, _old: Path, _new: Path, _new_branch: str) -> list[Path]:
    """Staff supplies the current folder; historical records remain unchanged."""
    return []


def _refresh_phase_table(session_dir: Path) -> None:
    """Rewrite a production unit's phase note without affecting the recorder."""
    try:
        state = pass_recorder.read_state(session_dir)
        plan_doc = pass_recorder.string(state.get("project_plan_doc")) or pass_recorder.string(
            state.get("plan_doc")
        )
        if not plan_doc:
            return
        plan_path = plan_history.resolve_plan_path(
            Path(pass_recorder.string(state.get("working_dir"))),
            plan_doc,
        )
        if isinstance(plan_history.plan_member(plan_path), session_staff.NoSuchMember):
            return
    except (OSError, UnicodeError, SystemExit) as error:
        print(f"phase table not written: {error}", file=sys.stderr)
        return
    from scripts.direct import phase_table

    # Without a vault the refresh writes nothing; skip its process start.
    if not phase_table.vault_present():
        return

    command = [
        sys.executable,
        str(Path(__file__).with_name("phase_table.py")),
        "refresh",
        "--session-dir",
        str(session_dir),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"phase table not written: {error}", file=sys.stderr)
        return
    if result.returncode == 0:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return
    reason = " ".join(result.stderr.split()) or f"exit status {result.returncode}"
    print(f"phase table not written: {reason}", file=sys.stderr)


def _git_value(working_dir: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=working_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _iso_epoch(value: str, context: str) -> float:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SystemExit(
            f"Malformed Project started timestamp in {context}: {value}"
        ) from error
    if parsed.tzinfo is None:
        raise SystemExit(
            f"Project started timestamp must include a timezone in {context}: {value}"
        )
    return parsed.timestamp()


def _count_plan_phases(plan_path: Path) -> dict[str, object]:
    """Count a plan's phases by heading, classifying every form.

    Returns done/todo/total plus any heading this could not classify. An
    unclassifiable heading is reported, never counted by default: a miscount here
    silently corrupts every project percentage the run reports.
    """
    try:
        phases, duplicates = plan_history.plan_phase_details(plan_path)
    except OSError:
        return {"available": False, "reason": f"unable to read {plan_path}"}
    done = sum(phase["done"] for phase in phases)
    todo = len(phases) - done
    total = done + todo
    if total == 0:
        return {"available": False, "reason": f"no phase headings in {plan_path}"}
    return {
        "available": True,
        "done": done,
        "todo": todo,
        "total": total,
        "order": [phase["id"] for phase in phases],
        "duplicate_ids": sorted(set(duplicates)),
    }


def _plan_derived_project_percent(
    counts: dict[str, object], phase_percent: int
) -> int | None:
    """Project completion from phase counts, crediting the phase in flight.

    The active phase contributes its own percentage as a fraction of one phase,
    so a project clock advances during a long phase instead of stepping only at
    checkpoints.
    """
    if not counts.get("available"):
        return None
    done = counts.get("done")
    total = counts.get("total")
    if not isinstance(done, int) or not isinstance(total, int) or total <= 0:
        return None
    completed = done + max(0, min(100, phase_percent)) / 100
    return max(0, min(100, round(100 * completed / total)))


def _read_plan_project_start(plan_path: Path) -> float | None:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to read plan document {plan_path}: {error}") from error
    matches = list(PROJECT_STARTED_PATTERN.finditer(text))
    if len(matches) > 1:
        raise SystemExit(f"Plan document has multiple Project started fields: {plan_path}")
    if not matches:
        return None
    return _iso_epoch(matches[0].group("value"), str(plan_path))


def _bullet_end(text: str, first_line_end: int) -> int:
    """Offset just past a Markdown bullet, continuation lines included.

    A Delegation Context bullet routinely wraps across several lines, and its
    sub-bullets are indented under it. Inserting after only the first line would
    split the bullet in half and orphan its remainder.
    """
    end = first_line_end
    while end < len(text) and text[end] == "\n":
        line_start = end + 1
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        if not line.strip() or not line[:1].isspace():
            break
        end = line_end
    return end


def _persist_plan_project_start(plan_path: Path, value: str) -> None:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to read plan document {plan_path}: {error}") from error
    project_match = PROJECT_PATTERN.search(text)
    if project_match is None:
        raise SystemExit(f"Plan document has no Delegation Context Project field: {plan_path}")
    insert_at = _bullet_end(text, project_match.end())
    updated = (
        text[:insert_at]
        + f"\n- **Project started:** {value}"
        + text[insert_at:]
    )
    try:
        _ = plan_path.write_text(updated, encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Unable to write Project started to {plan_path}: {error}") from error


def _explicit_plan_timing(
    working_dir: Path,
    plan_doc: str,
    now: float,
) -> ProjectTiming:
    plan_path = plan_history.resolve_plan_path(working_dir, plan_doc)
    existing = _read_plan_project_start(plan_path)
    if existing is not None:
        return ProjectTiming(
            started_at=existing,
            source="plan_field",
            plan_doc=str(plan_path),
        )

    # The plan's own first checkpoint, never the plan file's first commit. A
    # phased plan is routinely born as the previous plan's `-next.md` backlog,
    # committed by that project's very first checkpoint — so dating a project
    # from the file's birth measures its predecessor's whole run as this one's
    # elapsed time, and the ETA extrapolates from a rate nobody worked at.
    # <CheckpointCommit/> writes `checkpoint(<plan-slug>):` on every phase, and
    # the closing paren keeps `github-runners` from matching
    # `github-runners-next`.
    checkpoint_times = _git_value(
        working_dir,
        "log",
        "--fixed-strings",
        f"--grep=checkpoint({plan_path.stem}):",
        "--format=%cI",
    )
    committed = [line.strip() for line in checkpoint_times.splitlines() if line.strip()]
    if committed:
        value = committed[-1]
        started_at = _iso_epoch(value, f"First checkpoint for {plan_path}")
        source = "plan_first_checkpoint"
    else:
        value = pass_recorder.iso_time(now)
        started_at = now
        source = "plan_run"
    _persist_plan_project_start(plan_path, value)
    return ProjectTiming(
        started_at=started_at,
        source=source,
        plan_doc=str(plan_path),
    )


def _historical_project_timing(
    working_dir: Path,
    branch: str,
) -> ProjectTiming | None:
    runs_dir = plan_history.history_root() / "runs"
    latest_event: dict[str, object] | None = None
    latest_started_at = -1.0
    try:
        history_paths = runs_dir.glob("*.jsonl")
    except OSError:
        return None
    record = staff.initial_snapshot()
    for history_path in history_paths:
        event = plan_history.run_started_event(history_path)
        if event is None:
            continue
        if not pass_recorder.string(event.get("working_dir")):
            owner = unit_runs.run_owner(event, record)
            if isinstance(owner, staff.StaffRow) and not isinstance(owner.worktree, staff.WorktreeUnfilled):
                event = {**event, "working_dir": str(owner.worktree.path)}
        if (
            pass_recorder.string(event.get("working_dir")) != str(working_dir)
            or pass_recorder.string(event.get("branch")) != branch
        ):
            continue
        project_plan_doc = pass_recorder.string(event.get("project_plan_doc")) or pass_recorder.string(
            event.get("plan_doc")
        )
        if not project_plan_doc:
            continue
        run_started_at = pass_recorder.number(event.get("run_started_at"), -1.0)
        if run_started_at > latest_started_at:
            latest_event = event
            latest_started_at = run_started_at
    if latest_event is None:
        return None

    project_plan_doc = pass_recorder.string(latest_event.get("project_plan_doc")) or pass_recorder.string(
        latest_event.get("plan_doc")
    )
    plan_path = plan_history.resolve_plan_path(working_dir, project_plan_doc)
    if plan_path.is_file():
        existing = _read_plan_project_start(plan_path)
        if existing is not None:
            return ProjectTiming(
                started_at=existing,
                source="history_plan_field",
                plan_doc=str(plan_path),
            )
    return ProjectTiming(
        started_at=pass_recorder.number(latest_event.get("project_started_at")),
        source="history_recorded",
        plan_doc=str(plan_path),
    )


def _resolve_project_timing(
    working_dir: Path,
    branch: str,
    plan_doc: str,
    now: float,
) -> ProjectTiming:
    if plan_doc:
        return _explicit_plan_timing(working_dir, plan_doc, now)
    historical = _historical_project_timing(working_dir, branch)
    if historical is not None:
        return historical
    return ProjectTiming(started_at=now, source="run_started", plan_doc="")


def _ensure_project_timing(
    session_dir: Path,
    state: dict[str, object],
    now: float,
) -> dict[str, object]:
    if pass_recorder.string(state.get("project_start_source")):
        return state
    working_dir = Path(pass_recorder.string(state.get("working_dir"))).resolve()
    timing = _resolve_project_timing(
        working_dir,
        pass_recorder.string(state.get("branch")),
        pass_recorder.string(state.get("plan_doc")),
        now,
    )
    state["project_started_at"] = timing["started_at"]
    state["project_start_source"] = timing["source"]
    state["project_plan_doc"] = timing["plan_doc"]
    pass_recorder.write_state(session_dir, state)
    return state


@dataclass(frozen=True)
class ActiveDispatch:
    instance_id: str
    launcher_command: tuple[str, ...]


@dataclass(frozen=True)
class NoActiveDispatch:
    """The worker has no active pass."""


@dataclass(frozen=True)
class ActiveDispatchUnavailable:
    reason: str


def active_dispatch(worker_id: staff.StaffId, run: Path) -> ActiveDispatch | NoActiveDispatch | ActiveDispatchUnavailable:
    """Read the active pass through the worker's fixed staff id."""
    try:
        record = staff.snapshot()
        row = record.by_id(worker_id)
        if row is None or row.kind != "worker":
            return ActiveDispatchUnavailable(f"worker {worker_id}: staff row is unavailable")
        state = worker_lifecycle.read_json_object(pass_recorder.state_path(run))
        owner = unit_runs.run_owner(state, record)
        if not isinstance(owner, staff.StaffRow) or row.parent != staff.ReportsTo(owner.id):
            return NoActiveDispatch()
        matches = [item for item in pass_recorder.open_passes(state).values() if item.get("worker_id") == worker_id]
        if not matches:
            return NoActiveDispatch()
        if len(matches) != 1:
            return ActiveDispatchUnavailable(f"worker {worker_id}: multiple active passes")
        instance = pass_recorder.string(matches[0].get("instance_id"))
        command = matches[0].get("launcher_command")
        if not instance or not isinstance(command, list) or not command or any(
                not isinstance(part, str) for part in cast(list[object], command)):
            return ActiveDispatchUnavailable(f"worker {worker_id}: active pass identity or launcher command is unavailable")
        return ActiveDispatch(instance, tuple(pass_recorder.worker_launcher(cast(list[str], command), row.name)))
    except FileNotFoundError:
        return NoActiveDispatch()
    except (OSError, ValueError, worker_lifecycle.WorkerStateUnreadable) as error:
        return ActiveDispatchUnavailable(f"worker {worker_id}: {error}")


def reply_completed(run: Path, reply: Path) -> bool:
    """Read a saved reply only after the hook completes its recorded pass."""
    state = pass_recorder.read_state(run)
    return any(item.get("status") == "completed" and
               Path(pass_recorder.dispatch_delivery(item).reply_file) == reply
               for item in pass_recorder.pass_slots(state).values())


class DispatchDetailNotRecorded(Enum):
    """The dispatch has no unique value for this detail."""

    UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class RecordedDispatch:
    role: str
    phase_id: str | DispatchDetailNotRecorded
    lens: str | DispatchDetailNotRecorded
    paths: str | DispatchDetailNotRecorded
    instance_id: str | DispatchDetailNotRecorded = DispatchDetailNotRecorded.UNRECORDED
    status: str | DispatchDetailNotRecorded = DispatchDetailNotRecorded.UNRECORDED


@dataclass(frozen=True)
class NoDispatchRecorded:
    """The worker has no recorded dispatch."""


@dataclass
class DispatchReads:
    record: staff.StaffSnapshot
    states: dict[Path, dict[str, object]]
    events: dict[Path, list[dict[str, object]]]


class ReadDispatches(Enum):
    CURRENT = "read dispatch history at use"


_dispatch_reads: ContextVar[DispatchReads | ReadDispatches] = ContextVar("dispatch_reads", default=ReadDispatches.CURRENT)


@contextmanager
def dispatch_reads(record: staff.StaffSnapshot, runs: Iterable[Path]) -> Generator[None]:
    """Share history reads within one command, without retaining them afterward."""
    states: dict[Path, dict[str, object]] = {}
    for run in runs:
        path = run / unit_runs.STATE_FILE
        try:
            states[path] = pass_recorder.json_object(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError):
            continue
    token = _dispatch_reads.set(DispatchReads(record, states, {}))
    try:
        yield
    finally:
        _dispatch_reads.reset(token)


def last_dispatch(worker_id: staff.StaffId) -> RecordedDispatch | NoDispatchRecorded:
    """Read the newest open or finished dispatch across the staff parent's runs."""
    from scripts.direct import phase_reservation

    reads = _dispatch_reads.get()
    if isinstance(reads, ReadDispatches):
        try:
            membership = staff.local_snapshot()
        except staff.StaffUnreadable:
            return NoDispatchRecorded()
        runs = [path.parent for root in unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT"))
                for path in root.glob(f"*/{unit_runs.STATE_FILE}")]
        if private := unit_runs.environ("RUN_DIR"):
            runs.append(Path(private))
        with dispatch_reads(membership, dict.fromkeys(runs)):
            return last_dispatch(worker_id)
    membership = reads.record
    worker = membership.by_id(worker_id)
    if worker is None or worker.kind != "worker":
        return NoDispatchRecorded()
    parent = membership.parent_of(worker)
    records: list[tuple[Path, dict[str, object]]] = []
    for path, state in reads.states.items():
        try:
            owner = unit_runs.run_owner(state, membership)
        except (OSError, UnicodeError):
            continue
        if owner != parent:
            continue
        if path not in reads.events:
            reads.events[path] = _run_events(state, record=membership)
        passes = [*pass_recorder.pass_slots(state).values(), *(
                {**event, "instance_id": event.get("pass_instance_id"), "kind": event.get("pass_kind"),
                 "started_at": event.get("pass_started_at"), "finished_at": event.get("timestamp_epoch"),
                 "phase": {"id": event.get("phase_id")}}
                for event in reads.events[path] if event.get("event_type") == "pass_finished")]
        records.extend((path.parent, record) for record in passes
                       if pass_recorder.string(record.get("status")) in {"active", "refused", "completed", "error", "canceled", "interrupted"}
                       and record.get("worker_id") == worker_id)
    run, latest = max(records, key=lambda item: pass_recorder.number(item[1].get("finished_at"), pass_recorder.number(item[1].get("started_at"))),
                      default=(Path(), dict[str, object]()))
    role = pass_recorder.string(latest.get("kind"))
    if not role:
        return NoDispatchRecorded()
    phases = {pass_recorder.string((pass_recorder.object_dict(item[1].get("phase")) or {}).get("id")).strip() for item in records}
    missing = DispatchDetailNotRecorded.UNRECORDED
    phase = next(iter(phases)) if len(phases) == 1 and "" not in phases else missing
    lens = pass_recorder.string(latest.get("lens"))
    paths = pass_recorder.string(latest.get("paths")) or (
        str(phase_reservation.phase_directory(run, phase) / phase_reservation.FILES_FILE)
        if isinstance(phase, str) and role != "review" else missing)
    return RecordedDispatch(role, phase, lens or missing, paths,
                            pass_recorder.string(latest.get("instance_id")) or missing, pass_recorder.string(latest.get("status")) or missing)


def _park_current(state: dict[str, object], current: dict[str, object]) -> None:
    """Move the current phase among the older open ones, with its own state.

    Its open passes stay in the slot map, where their launchers close them; a
    pass opened before passes carried a phase gains this one's stamp, so the
    phase that finishes later cannot claim it.
    """
    for record in pass_recorder.open_passes(state).values():
        if "phase" not in record:
            record["phase"] = pass_recorder.phase_stamp(current)
    current[PARKED_KEY] = {key: state.get(key) for key in PHASE_SCOPED_KEYS}
    pass_recorder.parked_phases(state).append(current)


def _report_unclosed_phase(state: dict[str, object], phase: dict[str, object]) -> None:
    """Name a phase set aside with no open pass: its director never closed it."""
    identity = resolve_production_unit(state)
    unit = identity.unit if isinstance(identity, ProductionUnitIdentity) else f"unit not recorded ({identity.reason})"
    started = datetime.fromtimestamp(pass_recorder.number(phase.get("started_at")), user_zone())
    print(f"anomaly: start-phase: {unit}: phase {pass_recorder.string(phase.get('id'))} set aside unclosed, "
          + f"started {started:%Y-%m-%d %H:%M %Z}", file=sys.stderr)


def _unpark_newest(state: dict[str, object]) -> None:
    """Make the most recently started older open phase current again."""
    parked = pass_recorder.parked_phases(state)
    if not parked:
        return
    phase = parked.pop()
    saved = pass_recorder.object_dict(phase.pop(PARKED_KEY, None)) or {}
    for key in PHASE_SCOPED_KEYS:
        state[key] = saved.get(key)
    state["phase"] = phase


def _optional_epoch(args: argparse.Namespace, name: str, option: str, now: float) -> float:
    """A past moment an option names, for a phase recorded after the fact; now when absent."""
    value = pass_recorder.optional_arg(args, name)
    if not value:
        return now
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise SystemExit(f"{option} must be an ISO 8601 time with a zone; got {value}") from error
    if parsed.tzinfo is None:
        raise SystemExit(f"{option} must be an ISO 8601 time with a zone; got {value}")
    moment = parsed.timestamp()
    if moment > now:
        raise SystemExit(f"{option} {value} is in the future")
    return moment


def _close_active_activity(
    session_dir: Path,
    state: dict[str, object],
    status: str,
    result: str,
    now: float,
) -> None:
    activity = pass_recorder.object_dict(state.get("activity"))
    if activity is None or pass_recorder.string(activity.get("status")) != "active":
        return
    event = pass_recorder.record_event(state, "activity_finished", now)
    event["status"] = status
    event["activity_result"] = result
    event["activity_elapsed_seconds"] = max(
        0,
        int(now - pass_recorder.number(activity.get("started_at"), now)),
    )
    pass_recorder.append_event(state, event)
    activity["status"] = status
    activity["result"] = result
    activity["finished_at"] = now
    pass_recorder.write_state(session_dir, state)


def _start_activity(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    if not unit_runs.run_is_open(session_dir):
        raise SystemExit("Cannot start an activity: the run is finished")
    if pass_recorder.object_dict(state.get("phase")) is None:
        raise SystemExit("Start a phase before starting an activity")
    now = pass_recorder.now_epoch()
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = pass_recorder.read_state(session_dir)
    pass_recorder.refresh_main_identity(session_dir, state)
    activity: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "label": pass_recorder.arg_string(args, "label", "Work") or "Work",
        "activity": pass_recorder.arg_string(args, "activity"),
        "started_at": now,
        "status": "active",
    }
    state["activity"] = activity
    pass_recorder.write_state(session_dir, state)
    pass_recorder.append_event(state, pass_recorder.record_event(state, "activity_started", now))


def _finish_activity(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    status = pass_recorder.arg_string(args, "status", "completed") or "completed"
    _close_active_activity(
        session_dir,
        state,
        status,
        pass_recorder.arg_string(args, "result"),
        pass_recorder.now_epoch(),
    )


def _claim_run_pointer(owner: staff.StaffRow, session_dir: Path) -> None:
    """Publish the pointer for a unit run folder."""
    if unit_runs.is_unit_run(session_dir):
        _ = staff.set_run(owner.id, staff.OpenRun(session_dir))


def _assign_unit_plan(member: staff.StaffRow | unit_runs.RunOwnerUnrecorded | None, working_dir: Path, plan_doc: str) -> None:
    """Name the plan in a unit's `.unit.toml`; other owners have no unit config."""
    if isinstance(member, staff.StaffRow) and member.kind == "unit":
        unit_config.set_plan(working_dir, plan_history.resolve_plan_path(working_dir, plan_doc).relative_to(working_dir))


def _record_plan(session_dir: Path, state: dict[str, object], plan_doc: str) -> dict[str, object]:
    """Point a reused run at the plan its start-run names; the same plan changes nothing."""
    working_dir = Path(pass_recorder.string(state.get("working_dir"))).resolve()
    recorded = pass_recorder.string(state.get("project_plan_doc")) or pass_recorder.string(state.get("plan_doc"))
    wanted = plan_history.resolve_plan_path(working_dir, plan_doc)
    if recorded and plan_history.resolve_plan_path(working_dir, recorded) == wanted:
        return state
    _assign_unit_plan(unit_runs.run_owner(state, staff.initial_snapshot()), working_dir, plan_doc)
    timing = _explicit_plan_timing(working_dir, plan_doc, pass_recorder.now_epoch())
    state["plan_doc"] = plan_doc
    state["project_started_at"] = timing["started_at"]
    state["project_start_source"] = timing["source"]
    state["project_plan_doc"] = timing["plan_doc"]
    pass_recorder.write_state(session_dir, state)
    return state


def _start_run(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    existing = pass_recorder.state_path(session_dir)
    if existing.exists():
        state = _ensure_project_timing(
            session_dir,
            pass_recorder.read_state(session_dir),
            pass_recorder.now_epoch(),
        )
        if plan_doc := pass_recorder.arg_string(args, "plan_doc"):
            state = _record_plan(session_dir, state, plan_doc)
        print(pass_recorder.string(state.get("history_file"), str(existing)))
        return

    working_dir_value = pass_recorder.arg_string(args, "working_dir")
    if not working_dir_value:
        raise SystemExit("--working-dir is required")
    main_agent = pass_recorder.detect_main_identity(args)
    if main_agent["model"] == "unknown":
        raise SystemExit(
            "Unable to detect the unit director's model and effort; pass explicit --main-* values"
        )
    record = staff.initial_snapshot()
    owner = record.owner_of(main_agent["session_id"])
    owner = owner if owner is not None and owner.kind in ("unit", "showrunner") else None
    current = pass_recorder.staff_directory({unit_runs.OWNER_FIELD: owner.id} if owner is not None else {}, record)
    working_dir_value = pass_recorder.string(current.get("working_dir")) or working_dir_value
    working_dir = Path(working_dir_value).expanduser().resolve()
    now = pass_recorder.now_epoch()
    run_id = session_dir.name
    branch = _git_value(working_dir, "branch", "--show-current")
    if not branch:
        short_hash = _git_value(working_dir, "rev-parse", "--short", "HEAD")
        branch = f"detached@{short_hash or 'unknown'}"
    plan_doc = pass_recorder.arg_string(args, "plan_doc")
    if plan_doc:
        _assign_unit_plan(record.by_member_reference(main_agent["session_id"]), working_dir, plan_doc)
    project_timing = _resolve_project_timing(working_dir, branch, plan_doc, now)
    history_file = plan_history.history_root() / "runs" / f"{run_id}.jsonl"
    state: dict[str, object] = {
        "schema_version": pass_recorder.SCHEMA_VERSION,
        "run_id": run_id,
        "history_file": str(history_file),
        "working_dir": str(working_dir),
        "worktree": working_dir.name,
        "branch": branch,
        "plan_doc": plan_doc,
        "run_started_at": now,
        "project_started_at": project_timing["started_at"],
        "project_start_source": project_timing["source"],
        "project_plan_doc": project_timing["plan_doc"],
        "main_agent": main_agent,
        "phase": None,
        OPEN_PHASES_KEY: [],
        "pass": {},
        "project_last_percent": None,
        "project_percent_started_at": None,
        "phase_last_percent": None,
        "phase_percent_started_at": None,
        "last_percent": None,
        "percent_started_at": None,
        "pending_calibration": None,
    }
    if owner is not None and owner.kind in ("unit", "showrunner"):
        state[unit_runs.OWNER_FIELD] = owner.id
        _claim_run_pointer(owner, session_dir)
    pass_recorder.write_state(session_dir, state)
    pass_recorder.append_event(state, pass_recorder.record_event(state, "run_started", now))
    print(history_file)


def _work_order_metrics(work_order_file: str) -> dict[str, object]:
    """Measure a phase's Work Order so phase size can be correlated with outcome.

    Recorded only. Thresholds belong upstream in `/plan:to_phased_plan`, and
    setting them from one bad phase is a threshold with no evidence behind it —
    this is how the data arrives.
    """
    if not work_order_file:
        return {}
    try:
        text = Path(work_order_file).expanduser().read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    targets: set[str] = set()
    quoted_spans = cast(list[str], re.findall(r"`([^`\n]+)`", text))
    for quoted in quoted_spans:
        candidate = quoted.strip()
        if "/" in candidate or re.search(r"\.[A-Za-z0-9]{1,5}$", candidate):
            targets.add(candidate)
    return {
        "work_order_lines": sum(1 for line in lines if line.strip()),
        "work_order_words": len(text.split()),
        "work_order_file_targets": len(targets),
        "work_order_top_level_bullets": sum(
            1 for line in lines if re.match(r"^[-*] ", line)
        ),
    }


def _start_phase(args: argparse.Namespace) -> None:
    from scripts.direct import phase_reservation

    session_dir = pass_recorder.argument_session_dir(args)
    record = staff.initial_snapshot()
    state = pass_recorder.read_state(session_dir, record=record)
    now = pass_recorder.now_epoch()
    phase_id = pass_recorder.arg_string(args, "phase_id", "ad hoc")
    phase_dir = phase_reservation.phase_directory(session_dir, phase_id)
    # An unchanged retry keeps the open phase, regardless of the caller's worktree.
    open_phase = pass_recorder.open_phase_named(state, phase_id)
    if open_phase is not None:
        same_title = pass_recorder.arg_string(args, "phase_title", "Ad hoc work") == pass_recorder.string(open_phase.get("title"))
        same_start = (not pass_recorder.optional_arg(args, "started_at")
                      or _optional_epoch(args, "started_at", "--started-at", now)
                      == pass_recorder.number(open_phase.get("started_at")))
        if same_title and same_start:
            _finish_phase_start(session_dir, state, open_phase)
            return
        worktree = phase_reservation.phase_worktree(phase_dir)
        root = phase_reservation.repository_root(Path.cwd())
        if root is not None and root.resolve() != worktree.root.resolve():
            raise SystemExit(f"Phase {phase_id} is already open in {worktree.root}; "
                             + f"finish it before restarting in {root}.")
        return
    # The phase count reads the plan's headings, so a phase the plan lacks makes
    # every later report disagree with its own phase number.
    missing_from = _phase_missing_from_plan(state, phase_id)
    if missing_from is not None:
        raise SystemExit(
            f"Phase {phase_id} has no heading in {missing_from}. Add "
            + f"`### Phase {phase_id} — <title>  · status: todo` under ## Phases, then start it."
        )
    started_at = _optional_epoch(args, "started_at", "--started-at", now)
    # A new instance gets its worktree and baselines from its first dispatch, not the run's working_dir.
    try:
        phase_reservation.reset_for_start(phase_dir, phase_id)
        _ = phase_reservation.restore(phase_dir, phase_id)
    except phase_reservation.StateRefused as refused:
        raise SystemExit(str(refused)) from refused
    if not unit_runs.run_is_open(session_dir, record=record) or "status" in state:
        previous_status = pass_recorder.string(state.get("status"))
        _ = state.pop("status", None)
        _ = state.pop("finished_at", None)
        reopened = pass_recorder.record_event(state, "reopened", now)
        reopened.update({"previous_status": previous_status, "cause": "start-phase", "phase_id": phase_id})
        pass_recorder.append_event(state, reopened)
        pass_recorder.write_state(session_dir, state)
        row = unit_runs.run_owner(state, record)
        if isinstance(row, staff.StaffRow):
            _claim_run_pointer(row, session_dir)
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = pass_recorder.read_state(session_dir, record=record)
    active = pass_recorder.object_dict(state.get("phase"))
    if active is not None and pass_recorder.string(active.get("status")) == "active":
        _park_current(state, active)
        if not any(pass_recorder.string((pass_recorder.object_dict(record.get("phase")) or {}).get("instance_id"))
                   == pass_recorder.string(active.get("instance_id")) for record in pass_recorder.open_passes(state).values()):
            _report_unclosed_phase(state, active)
    else:
        state["pass"] = {}
    phase: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "id": phase_id,
        "title": pass_recorder.arg_string(args, "phase_title", "Ad hoc work"),
        "started_at": started_at,
        "status": "active",
    }
    state["phase"] = phase
    state[pass_recorder.ARMED_REVIEW_KEY] = None
    state["phase_last_percent"] = None
    state["phase_percent_started_at"] = None
    state["last_percent"] = None
    state["percent_started_at"] = None
    state["pending_calibration"] = None
    event = pass_recorder.record_event(state, "phase_started", started_at)
    production, unit = _phase_plan_names(state)
    source = PhaseSourceIdentity(production, unit, _phase_repository(pass_recorder.string(state.get("working_dir"))))
    event.update(production=_phase_metadata_fields(source.production),
                 unit=_phase_metadata_fields(source.unit),
                 repository=_phase_metadata_fields(source.repository))
    event["contract_revision"] = _contract_revision()
    event["wait_coverage"] = {"state": "Recorded", "version": 1}
    if started_at != now:
        event["recorded_at"] = pass_recorder.iso_time(now)
        event["backfilled"] = True
    event.update(_work_order_metrics(pass_recorder.arg_string(args, "work_order_file")))
    phase["start_event"] = event
    pass_recorder.write_state(session_dir, state)
    _finish_phase_start(session_dir, state, phase)


def _finish_phase_start(session_dir: Path, state: dict[str, object], phase: dict[str, object]) -> None:
    """Publish a saved phase start once, including after a stopped state write."""
    events = _run_events(state)
    if not any(event.get("event_type") == "phase_started"
               and event.get("phase_instance_id") == phase.get("instance_id") for event in events):
        event = pass_recorder.object_dict(phase.get("start_event"))
        if event is None:
            event = pass_recorder.record_event(state, "phase_started", pass_recorder.number(phase.get("started_at")), phase_record=phase)
        pass_recorder.append_event(state, event)
    run_start = next((index + 1 for index in range(len(events) - 1, -1, -1)
                      if events[index].get("event_type") == "run_finished"), 0)
    for wait in _waits_by_id(events[run_start:]).values():
        if (wait.get("event_type") == "wait_started" and wait.get("wait_scope") == "unit"
                and wait.get("target_work") == phase.get("id")):
            _end_wait(state, wait, pass_recorder.now_epoch(), unclosed=False)
    if "start_event" in phase:
        del phase["start_event"]
        pass_recorder.write_state(session_dir, state)
    _refresh_phase_table(session_dir)


def launch_slot(session_dir: Path, slot: str, launcher_command: list[str]) -> str:
    """Name the slot whose pass this launcher command started, or the given slot."""
    if not pass_recorder.state_path(session_dir).is_file():
        return slot
    return _launch_slot(pass_recorder.pass_slots(pass_recorder.read_state(session_dir)), slot, launcher_command)


def _launch_slot(passes: dict[str, dict[str, object]], slot: str, launcher_command: list[str]) -> str:
    return next((name for name, passed in passes.items()
                 if launcher_command and (passed.get("launcher_command") == launcher_command or (
                     passed.get("status") in ("active", "refused")
                     and passed.get("worker_id") and passed.get("worker_name")
                     and isinstance(saved := passed.get("launcher_command"), list)
                     and pass_recorder.worker_launcher(cast(list[str], saved), pass_recorder.string(passed.get("worker_name"))) == launcher_command))), slot)


def dispatch_worker(session_dir: Path, *, slot: str, phase: str, role: staff.WorkerRole,
                    prompt: Path, launcher_command: list[str],
                    ) -> pass_recorder.MatchedDispatch | pass_recorder.NoMatchedDispatch | pass_recorder.ActiveDispatchPromptConflict:
    """Find the worker for this dispatch retry without changing recorder state."""
    if not pass_recorder.state_path(session_dir).is_file():
        return pass_recorder.NoMatchedDispatch()
    state = pass_recorder.read_state(session_dir)
    passes = pass_recorder.pass_slots(state)
    slot = _launch_slot(passes, slot, launcher_command)
    recorded_phase = pass_recorder.open_phase_named(state, phase)
    if recorded_phase is None:
        recorded_phase = pass_recorder.object_dict(passes.get(slot, {}).get("phase"))
        if recorded_phase is None or pass_recorder.string(recorded_phase.get("id")) != phase:
            return pass_recorder.NoMatchedDispatch()
    return pass_recorder.dispatch_match(
        passes.get(slot), recorded_phase,
        {"kind": role, "prompt_file": str(prompt), "launcher_command": launcher_command},
        launcher_command, dispatch_owned=True,
    )


@dataclass(frozen=True)
class RecordedDispatchRun:
    directory: Path


class DispatchRunNotRecorded(Enum):
    MISSING = "the worker has no dispatch state link"


def _dispatch_state_links() -> Path:
    return staff.record_path().parent / "dispatches"


def _dispatch_state_link(worker_id: staff.StaffId) -> Path:
    return _dispatch_state_links() / str(worker_id)


def recorded_dispatch_run(worker_id: staff.StaffId) -> RecordedDispatchRun | DispatchRunNotRecorded:
    """Find the existing pass state without searching the run root."""
    try:
        path = _dispatch_state_link(worker_id).resolve(strict=True)
    except FileNotFoundError:
        return DispatchRunNotRecorded.MISSING
    return RecordedDispatchRun(path.parent)


def recorded_dispatch_runs() -> dict[staff.StaffId, RecordedDispatchRun]:
    """Map each linked worker to its pass state run with one read of the link folder."""
    try:
        with os.scandir(_dispatch_state_links()) as entries:
            links = {entry.name: (entry.path, os.readlink(entry.path)) for entry in entries if entry.is_symlink()}
    except FileNotFoundError:
        return {}
    # Many workers link to one run; resolve each target once.
    runs: dict[str, RecordedDispatchRun | DispatchRunNotRecorded] = {}
    found: dict[staff.StaffId, RecordedDispatchRun] = {}
    for name, (path, target) in links.items():
        if target not in runs:
            try:
                runs[target] = RecordedDispatchRun(Path(path).resolve(strict=True).parent)
            except FileNotFoundError:
                runs[target] = DispatchRunNotRecorded.MISSING
        if isinstance(run := runs[target], RecordedDispatchRun):
            found[staff.StaffId(name)] = run
    return found


def _link_dispatch_state(worker_id: staff.StaffId, session_dir: Path) -> None:
    link = _dispatch_state_link(worker_id)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{worker_id}.{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(pass_recorder.state_path(session_dir).resolve())
        _ = temporary.replace(link)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CalledAgent:
    """The family, model and effort a dispatched worker runs; `unknown` and `unset` mean not recorded."""

    family: str
    model: str
    effort: str


CALLED_AGENT_NOT_RECORDED = CalledAgent("unknown", "unknown", "unset")


def begin_dispatch(session_dir: Path, *, slot: str, phase: str, role: staff.WorkerRole,
                   worker_name: str, worker_id: staff.StaffId, launcher_command: list[str], prompt: Path, kind: str, lens: str,
                   fix_cause: str = "", fix_pass: int = 0, activity: str = "",
                   called: CalledAgent = CALLED_AGENT_NOT_RECORDED) -> str:
    """Record a published worker's dispatch and return its durable pass id."""
    if role not in staff.WORKER_ROLES:
        raise ValueError(f"Unknown worker role: {role}")
    args = argparse.Namespace(
        session_dir=str(session_dir), team_slot=slot, phase_id=phase, pass_kind=role, launcher_pid=os.getpid(),
        worker_name=worker_name, worker_id=worker_id, launcher_command=json.dumps(launcher_command) if launcher_command else "",
        prompt_file=str(prompt), activity=activity or f"{role} dispatch", called_task=f"worker.{role}",
        called_family=called.family, called_model=called.model, called_effort=called.effort,
        fix_pass=fix_pass, fix_cause=fix_cause, dispatch_kind=kind, lens=lens, dispatch_owned=True,
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            pass_recorder.start_pass(args)
            _link_dispatch_state(worker_id, session_dir)
            return pass_recorder.string(pass_recorder.pass_slots(pass_recorder.read_state(session_dir))[slot]["instance_id"])
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def complete_dispatch(session_dir: Path, slot: str, instance_id: str, *,
                      status: Literal["completed", "error"] = "completed") -> None:
    """Record completion for the exact dispatch, including DONE after a launcher error."""
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = pass_recorder.read_state(session_dir)
            opened = pass_recorder.pass_slots(state).get(slot)
            if opened is not None and opened.get("instance_id") == instance_id:
                if opened.get("status") == "error" and status == "completed":
                    # Close the corrected pass through the existing recorder event.
                    opened["status"] = "active"
                _ = pass_recorder.close_slot_pass(session_dir, state, slot, status, pass_recorder.now_epoch())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


NO_COMPLETION_ACTIONS = pass_recorder.CompletionActions()


def _update_dispatch(session_dir: Path, instance_id: str, key: str, value: object) -> None:
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = pass_recorder.read_state(session_dir)
        for passed in pass_recorder.pass_slots(state).values():
            if passed.get("instance_id") == instance_id:
                if passed.get(key) != value:
                    passed[key] = value
                    pass_recorder.write_state(session_dir, state)
                return


def worker_artifact(path: Path, slot: str, worker: str) -> Path:
    """Keep artifacts with the worker selected for the recorded slot."""
    return path.with_name(worker + path.name.removeprefix(slot)) if path.name.startswith(slot + "_") else path


def save_dispatch_delivery(session_dir: Path, instance_id: str, delivery: pass_recorder.DispatchDelivery) -> None:
    _update_dispatch(session_dir, instance_id, "delivery", asdict(delivery))


@dataclass(frozen=True)
class HookCompletion:
    """Keep each unfinished completion step available to the next event."""

    outcome: str
    reply_file: str
    details: tuple[str, ...] = ()
    actions_done: bool = False
    pane_closed: bool = False
    role_released: bool = False
    finished: bool = False
    recording_done: bool = False
    sender_native_id: str = ""


class NoHookCompletion(Enum):
    UNRECORDED = "no terminal outcome recorded"


def hook_completion(passed: dict[str, object]) -> HookCompletion | NoHookCompletion:
    fields = pass_recorder.object_dict(passed.get("hook_completion"))
    if fields is None:
        return NoHookCompletion.UNRECORDED
    details = fields.get("details", [])
    return HookCompletion(pass_recorder.string(fields.get("outcome")), pass_recorder.string(fields.get("reply_file")),
        tuple(str(item) for item in cast(list[object], details)) if isinstance(details, list) else (),
        fields.get("actions_done") is True, fields.get("pane_closed") is True,
        fields.get("role_released") is True, fields.get("finished") is True, fields.get("recording_done") is True,
        pass_recorder.string(fields.get("sender_native_id")))


def save_hook_completion(run: Path, instance_id: str, completion: HookCompletion) -> None:
    _update_dispatch(run, instance_id, "hook_completion", asdict(completion))


def dispatch_completed(run: Path, slot: str, instance_id: str) -> bool:
    passed = pass_recorder.pass_slots(pass_recorder.read_state(run)).get(slot, {})
    return passed.get("instance_id") == instance_id and passed.get("status") == "completed"


def record_dispatch_notice(session_dir: Path, instance_id: str, notice: str) -> None:
    """Record a notice only after its message succeeds."""
    _update_dispatch(session_dir, instance_id, "hook_notice", notice)


def record_quota_refusal(session_dir: Path, instance_id: str, refusal_id: str) -> None:
    """Record a reported quota refusal so the timer reports it once."""
    _update_dispatch(session_dir, instance_id, "quota_refusal", refusal_id)


def mark_pass_refused(session_dir: Path, worker_name: str) -> None:
    """Mark the open pass without closing it or changing its dispatch id."""
    if not pass_recorder.state_path(session_dir).is_file():
        return
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        record = staff.initial_snapshot()
        state = pass_recorder.read_state(session_dir, record=record)
        owner = unit_runs.run_owner(state, record)
        workers = record.find(kind="worker", parent=owner.id) if isinstance(owner, staff.StaffRow) else ()
        worker = next((row for row in workers if row.name == worker_name), None)
        for passed in pass_recorder.open_passes(state).values():
            if worker is not None and passed.get("worker_id") == worker.id and passed.get("status") != "refused":
                passed["status"] = "refused"
                pass_recorder.write_state(session_dir, state)
                pass_recorder.append_event(state, pass_recorder.record_event(state, "pass_refused", pass_recorder.now_epoch(), passed))


def cancel_worker_passes(session_dir: Path, worker_id: staff.StaffId) -> int:
    """End each open pass of a stopped worker as canceled. Return the number of passes ended."""
    if not pass_recorder.state_path(session_dir).is_file():
        return 0
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = pass_recorder.read_state(session_dir)
        slots = [slot for slot, passed in pass_recorder.open_passes(state).items() if passed.get("worker_id") == worker_id]
        return sum(pass_recorder.close_slot_pass(session_dir, state, slot, "canceled", pass_recorder.now_epoch()) for slot in slots)


def _resolve_worker_agent(task: str) -> pass_recorder.AgentIdentity:
    """Ask the shared agent registry which agent a task resolves to.

    The early reviewer is armed by the unit director in the same turn it launches
    `review.sh`, so the launcher has not written its resolved identity yet and
    waiting for it would be a poll. Resolving through the same bash entry point
    the launcher uses gives the Worker cell the same answer with no race.
    """
    resolver = Path(__file__).resolve().parent.parent / "agents" / "models_config.sh"
    unknown = pass_recorder.AgentIdentity(family="", model="", effort="", session_id="")
    if not resolver.is_file():
        return unknown
    try:
        result = subprocess.run(
            ["bash", "-c", 'source "$1" && agents_resolve_print "$2"', "_", str(resolver), task],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return unknown
    if result.returncode != 0:
        return unknown
    fields = dict(
        part.split("=", 1)
        for part in result.stdout.strip().split(" ")
        if "=" in part
    )
    return pass_recorder.AgentIdentity(
        family=fields.get("family", ""),
        model=fields.get("agent", ""),
        effort=fields.get("effort", "") or "unset",
        session_id="",
    )


def _arm_review(args: argparse.Namespace) -> None:
    """Record that a blind reviewer is running alongside the open pass.

    Deliberately not launcher-owned: no pass is opened and no pass event is
    written, so nothing convergence counts can be forged from here. The marker
    only earns the reviewer a row in the stage table while the writer it
    overlaps is still going.
    """
    from scripts.direct import phase_reservation

    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    phase_id = pass_recorder.arg_string(args, "phase_id")
    phase = pass_recorder.open_phase_named(state, phase_id) if phase_id else pass_recorder.object_dict(state.get("phase"))
    if phase is None or pass_recorder.string(phase.get("status")) != "active":
        raise SystemExit(f"Phase {phase_id} is not open" if phase_id else "Start a phase before arming an early review")
    if pass_recorder.reporting_pass(state) is None:
        raise SystemExit(
            "An early review overlaps a running dispatch, so a pass must be open. "
            + "Without an open pass, launch it through assign.sh and let it record its own pass."
        )
    now = pass_recorder.now_epoch()
    called_task = pass_recorder.arg_string(args, "called_task", "direct.review") or "direct.review"
    lens = pass_recorder.arg_string(args, "lens")
    suffix = f"_{lens}" if lens else ""
    worker = pass_recorder.arg_string(args, "worker") or "review"
    # `review.sh --phase` writes its files in the phase's directory, so the marker names the same one.
    artifact_dir = phase_reservation.phase_directory(session_dir, phase_id) if phase_id else session_dir
    armed: dict[str, object] = {
        "instance_id": str(uuid.uuid4()),
        "kind": "review",
        "phase_instance_id": pass_recorder.string(phase.get("instance_id")),
        "activity": pass_recorder.arg_string(args, "activity"),
        "started_at": now,
        "called_task": called_task,
        "called_agent": _resolve_worker_agent(called_task),
        # Named reviewers use their worker prefix. Legacy reviewers use review.
        "status_file": str(artifact_dir / f"{worker}_status{suffix}"),
        "pid_file": str(artifact_dir / f"{worker}_pid{suffix}"),
        "status": "armed",
    }
    state[pass_recorder.ARMED_REVIEW_KEY] = armed
    pass_recorder.write_state(session_dir, state)
    event = pass_recorder.record_event(state, "early_review_armed", now)
    event.update(
        {
            "early_review_instance_id": pass_recorder.string(armed.get("instance_id")),
            "early_review_activity": pass_recorder.string(armed.get("activity")),
        }
    )
    pass_recorder.append_event(state, event)


def _disarm_review(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    _ = pass_recorder.clear_armed_review(
        session_dir,
        state,
        pass_recorder.arg_string(args, "reason", "cleared") or "cleared",
        pass_recorder.now_epoch(),
    )


def load_events(event_types: Collection[str] = (), *,
                record: staff.StaffSnapshot | None = None) -> tuple[list[dict[str, object]], int]:
    events: list[dict[str, object]] = []
    if record is None:
        record = staff.initial_snapshot()
    ignored = 0
    runs_dir = plan_history.history_root() / "runs"
    wanted = re.compile(r'"event_type"\s*:\s*"(?:' + "|".join(map(re.escape, event_types)) + r')"')
    if not runs_dir.exists():
        return events, ignored
    for path in sorted(runs_dir.glob("*.jsonl")):
        try:
            with path.open(encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                lines = handle.read().splitlines()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            ignored += 1
            continue
        for line in lines:
            if event_types and not wanted.search(line):
                continue
            event = pass_recorder.json_object(line)
            if event is None or pass_recorder.integer(event.get("schema_version")) != pass_recorder.SCHEMA_VERSION:
                ignored += 1
                continue
            if not event_types or event.get("event_type") in event_types:
                events.append(pass_recorder.staff_directory(event, record))
    return events, ignored


def events_for_worktrees(worktrees: tuple[Path, ...], event_types: frozenset[str]) -> list[dict[str, object]]:
    """Read selected records once for the units' current staff worktrees."""
    directories = {str(worktree.resolve()) for worktree in worktrees}
    events = load_events(event_types)[0]
    record = staff.initial_snapshot() if any(not event.get("working_dir") for event in events) else None
    selected: list[dict[str, object]] = []
    for event in events:
        if not event.get("working_dir") and record is not None:
            current = pass_recorder.staff_directory({key: value for key, value in event.items() if key != "event_type"}, record)
            event = {**event, **{key: current[key] for key in ("working_dir", "worktree") if key in current}}
        if event.get("working_dir") in directories:
            selected.append(event)
    return sorted(selected, key=lambda event: pass_recorder.number(event.get("timestamp_epoch")))


def _agent_fields(event: dict[str, object], key: str) -> tuple[str, str]:
    identity = pass_recorder.object_dict(event.get(key))
    if identity is None:
        return "", ""
    return pass_recorder.string(identity.get("model")), pass_recorder.string(identity.get("effort"))


def _calibration_samples(events: list[dict[str, object]]) -> list[CalibrationSample]:
    phase_finishes: dict[str, float] = {}
    progress_by_phase: dict[str, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        phase_instance_id = pass_recorder.string(event.get("phase_instance_id"))
        if not phase_instance_id:
            continue
        event_type = pass_recorder.string(event.get("event_type"))
        if event_type == "phase_finished" and pass_recorder.string(event.get("status")) == "completed":
            phase_finishes[phase_instance_id] = pass_recorder.number(event.get("timestamp_epoch"))
        elif event_type == "progress_reported":
            progress_by_phase[phase_instance_id].append(event)

    samples: list[CalibrationSample] = []
    for phase_instance_id, reports in progress_by_phase.items():
        phase_finished_at = phase_finishes.get(phase_instance_id)
        if phase_finished_at is None:
            continue
        phase_events = [event for event in events if pass_recorder.string(event.get("phase_instance_id")) == phase_instance_id]
        reports.sort(key=lambda event: pass_recorder.number(event.get("timestamp_epoch")))
        index = 0
        while index < len(reports):
            first = reports[index]
            percent = pass_recorder.integer(first.get("percent"))
            next_index = index + 1
            while next_index < len(reports) and pass_recorder.integer(reports[next_index].get("percent")) == percent:
                next_index += 1
            streak_ended_at = (
                pass_recorder.number(reports[next_index].get("timestamp_epoch"))
                if next_index < len(reports)
                else phase_finished_at
            )
            streak_started_at = pass_recorder.number(first.get("timestamp_epoch"))
            seen_raw_percentages: set[int] = set()
            for report in reports[index:next_index]:
                raw_percent = pass_recorder.integer(report.get("raw_percent"), percent)
                if raw_percent in seen_raw_percentages:
                    continue
                seen_raw_percentages.add(raw_percent)
                reported_at = pass_recorder.number(report.get("timestamp_epoch"))
                phase_started_at = pass_recorder.number(report.get("phase_started_at"))
                if phase_finished_at <= phase_started_at or reported_at < phase_started_at:
                    continue
                phase_duration = _work_seconds(phase_events, phase_instance_id, phase_started_at, phase_finished_at)
                phase_elapsed = _work_seconds(phase_events, phase_instance_id, phase_started_at, reported_at)
                if phase_duration <= 0:
                    continue
                temporal_percent = max(0.0, min(100.0, 100.0 * phase_elapsed / phase_duration))
                suggested_percent = pass_recorder.integer(report.get("suggested_percent"), raw_percent)
                main_model, main_effort = _agent_fields(report, "main_agent")
                called_model, called_effort = _agent_fields(report, "called_agent")
                implied_total = (
                    phase_elapsed / (percent / 100.0) if percent > 0 else phase_duration
                )
                samples.append(
                    CalibrationSample(
                        percent=percent,
                        raw_percent=raw_percent,
                        cap_stage=pass_recorder.string(report.get("cap_stage")),
                        suggested_percent=suggested_percent,
                        decision_source=pass_recorder.string(report.get("decision_source"), "legacy"),
                        override_reason=pass_recorder.string(report.get("override_reason")),
                        pass_kind=pass_recorder.string(report.get("pass_kind")),
                        main_model=main_model,
                        main_effort=main_effort,
                        called_model=called_model,
                        called_effort=called_effort,
                        hold_seconds=max(0, int(streak_ended_at - streak_started_at)),
                        unchanged_before_report_seconds=max(
                            0,
                            int(reported_at - streak_started_at),
                        ),
                        remaining_at_report_seconds=max(
                            0,
                            int(phase_finished_at - reported_at),
                        ),
                        temporal_percent=temporal_percent,
                        raw_bias_percentage_points=raw_percent - temporal_percent,
                        suggested_bias_percentage_points=suggested_percent - temporal_percent,
                        reported_bias_percentage_points=percent - temporal_percent,
                        implied_total_error_seconds=implied_total - phase_duration,
                    )
                )
            index = next_index
    return samples


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _round_metric(value: float) -> float:
    return round(value, 2)


def _sample_metrics(samples: list[CalibrationSample], current_hold: int) -> dict[str, object]:
    import statistics

    if not samples:
        return {
            "sample_count": 0,
            "comparable_after_current_hold_count": 0,
            "decision_source_counts": {},
            "override_sample_count": 0,
        }
    decision_source_counts: dict[str, int] = defaultdict(int)
    for sample in samples:
        decision_source_counts[sample["decision_source"]] += 1
    survivors = [sample for sample in samples if sample["hold_seconds"] >= current_hold]
    remaining_after_hold = [
        max(
            0.0,
            sample["remaining_at_report_seconds"]
            + sample["unchanged_before_report_seconds"]
            - current_hold,
        )
        for sample in survivors
    ]
    return {
        "sample_count": len(samples),
        "completed_raw_estimate_sample_count": len(samples),
        "median_unchanged_seconds": int(statistics.median(sample["hold_seconds"] for sample in samples)),
        "p75_unchanged_seconds": int(_percentile([float(sample["hold_seconds"]) for sample in samples], 0.75)),
        "p90_unchanged_seconds": int(_percentile([float(sample["hold_seconds"]) for sample in samples], 0.90)),
        "median_remaining_at_report_seconds": int(
            statistics.median(sample["remaining_at_report_seconds"] for sample in samples)
        ),
        "median_temporal_percent_at_report": _round_metric(
            statistics.median(sample["temporal_percent"] for sample in samples)
        ),
        "median_reported_percent": _round_metric(
            statistics.median(sample["percent"] for sample in samples)
        ),
        "median_suggested_percent": _round_metric(
            statistics.median(sample["suggested_percent"] for sample in samples)
        ),
        "median_raw_bias_percentage_points": _round_metric(
            statistics.median(sample["raw_bias_percentage_points"] for sample in samples)
        ),
        "median_reported_bias_percentage_points": _round_metric(
            statistics.median(sample["reported_bias_percentage_points"] for sample in samples)
        ),
        "median_raw_absolute_error_percentage_points": _round_metric(
            statistics.median(abs(sample["raw_bias_percentage_points"]) for sample in samples)
        ),
        "median_suggested_absolute_error_percentage_points": _round_metric(
            statistics.median(
                abs(sample["suggested_bias_percentage_points"]) for sample in samples
            )
        ),
        "median_reported_absolute_error_percentage_points": _round_metric(
            statistics.median(abs(sample["reported_bias_percentage_points"]) for sample in samples)
        ),
        "median_suggested_improvement_percentage_points": _round_metric(
            statistics.median(
                abs(sample["raw_bias_percentage_points"])
                - abs(sample["suggested_bias_percentage_points"])
                for sample in samples
            )
        ),
        "median_reported_improvement_percentage_points": _round_metric(
            statistics.median(
                abs(sample["raw_bias_percentage_points"])
                - abs(sample["reported_bias_percentage_points"])
                for sample in samples
            )
        ),
        "decision_source_counts": dict(sorted(decision_source_counts.items())),
        "override_sample_count": sum(
            sample["decision_source"] == "override" for sample in samples
        ),
        "median_implied_total_error_seconds": int(
            statistics.median(sample["implied_total_error_seconds"] for sample in samples)
        ),
        "comparable_after_current_hold_count": len(survivors),
        "median_remaining_after_current_hold_seconds": (
            int(statistics.median(remaining_after_hold)) if remaining_after_hold else None
        ),
    }


def _calibration_scopes(
    samples: list[CalibrationSample],
    state: dict[str, object],
    cap_stage: str,
) -> list[tuple[str, list[CalibrationSample]]]:
    """Every history a suggestion may draw on, narrowest first.

    The stage is a fact and the percent is an estimate, so a named stage narrows
    every identity scope and the whole history sits behind them as the widest.
    An 86 entered during implementation and an 86 entered during closure sit
    hours apart, and pooling them is how one tick drew a suggestion of 55 between
    neighbours of 20 and 15. With no stage named the identity scopes are the
    whole ladder.
    """
    # Match on the open window, the same rule `_event` records by. A finished
    # pass left in state would otherwise match this activity's report against
    # samples from a worker that is no longer running.
    current_pass = pass_recorder.reporting_pass(state) or {}
    main_agent = pass_recorder.object_dict(state.get("main_agent")) or {}
    called_agent = pass_recorder.object_dict(current_pass.get("called_agent")) or {}
    pass_kind = pass_recorder.string(current_pass.get("kind"))
    main_model = pass_recorder.string(main_agent.get("model"))
    main_effort = pass_recorder.string(main_agent.get("effort"))
    called_model = pass_recorder.string(called_agent.get("model"))
    called_effort = pass_recorder.string(called_agent.get("effort"))

    identity: list[tuple[str, Callable[[CalibrationSample], bool]]] = [
        (
            "main_called_pass",
            lambda sample: sample["pass_kind"] == pass_kind
            and sample["main_model"] == main_model
            and sample["main_effort"] == main_effort
            and sample["called_model"] == called_model
            and sample["called_effort"] == called_effort,
        ),
        (
            "called_pass",
            lambda sample: sample["pass_kind"] == pass_kind
            and sample["called_model"] == called_model
            and sample["called_effort"] == called_effort,
        ),
        ("pass", lambda sample: sample["pass_kind"] == pass_kind),
        ("all_models_and_passes", lambda _sample: True),
    ]
    if not cap_stage:
        return [
            (name, [sample for sample in samples if matches(sample)])
            for name, matches in identity
        ]
    staged = [sample for sample in samples if sample["cap_stage"] == cap_stage]
    return [
        *(
            (f"stage_{name}", [sample for sample in staged if matches(sample)])
            for name, matches in identity
        ),
        ("all_stages", samples),
    ]


def _window_weight(sample: CalibrationSample, candidate_percent: int) -> float:
    """How far one report speaks for the entered percent: fully at the same
    number, fading to nothing at the edge of CALIBRATION_WINDOW."""
    distance = abs(sample["raw_percent"] - candidate_percent)
    return max(0.0, 1.0 - distance / CALIBRATION_WINDOW)


def _window_totals(
    samples: list[CalibrationSample],
    candidate_percent: int,
) -> tuple[float, float]:
    """A scope's weight around the entered percent, and its weighted bias sum."""
    weight = 0.0
    weighted_bias = 0.0
    for sample in samples:
        sample_weight = _window_weight(sample, candidate_percent)
        weight += sample_weight
        weighted_bias += sample_weight * sample["raw_bias_percentage_points"]
    return weight, weighted_bias


def _smoothed_bias(
    scopes: list[tuple[str, list[CalibrationSample]]],
    candidate_percent: int,
) -> tuple[float, float]:
    """The bias the history around this percent shows, and the widest scope's weight.

    Each narrower scope leans on the one around it until it holds
    MIN_CALIBRATION_SAMPLES of weight of its own, so the answer slides from the
    broad history to the matching one as samples accumulate. Picking one scope
    and one exact percent outright made the answer jump wherever either choice
    flipped; this blend moves only as far as the entered percent does. The
    widest scope's weight decides whether history says anything at all.
    """
    *narrower, (_, widest) = scopes
    support, weighted_bias = _window_totals(widest, candidate_percent)
    bias = weighted_bias / support if support > 0 else 0.0
    for _, scoped in reversed(narrower):
        weight, weighted_bias = _window_totals(scoped, candidate_percent)
        bias = (weighted_bias + MIN_CALIBRATION_SAMPLES * bias) / (
            weight + MIN_CALIBRATION_SAMPLES
        )
    return bias, support


def _matching_scope(
    scopes: list[tuple[str, list[CalibrationSample]]],
    candidate_percent: int,
) -> tuple[str, list[CalibrationSample]]:
    """The narrowest scope that clears the sample floor on its own weight, cut to
    its reports inside the window: the history the suggestion leans on most, and
    the one the metrics beside it describe."""
    window = f"within_{CALIBRATION_WINDOW}_percentage_points"
    for name, scoped in scopes:
        weight, _ = _window_totals(scoped, candidate_percent)
        if weight >= MIN_CALIBRATION_SAMPLES:
            return f"{window}:{name}", [
                sample for sample in scoped if _window_weight(sample, candidate_percent) > 0
            ]
    _, widest = scopes[-1]
    return f"{window}:insufficient", [
        sample for sample in widest if _window_weight(sample, candidate_percent) > 0
    ]


def _current_hold_seconds(state: dict[str, object], candidate_percent: int, now: float) -> int:
    last_percent = pass_recorder.integer(
        state.get("phase_last_percent"),
        pass_recorder.integer(state.get("last_percent"), -1),
    )
    if last_percent != candidate_percent:
        return 0
    started_at = pass_recorder.number(
        state.get("phase_percent_started_at"),
        pass_recorder.number(state.get("percent_started_at"), now),
    )
    return max(0, int(now - started_at))


def _calibrate(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    now = pass_recorder.now_epoch()
    candidate = pass_recorder.arg_integer(args, "candidate_percent")
    if not 0 <= candidate <= 100:
        raise SystemExit("--candidate-percent must be between 0 and 100")
    cap_stage = pass_recorder.arg_string(args, "cap_stage")
    events, ignored = load_events()
    scopes = _calibration_scopes(_calibration_samples(events), state, cap_stage)
    scope, samples = _matching_scope(scopes, candidate)
    bias, support = _smoothed_bias(scopes, candidate)
    apply_suggestion = support >= MIN_CALIBRATION_SAMPLES
    suggestion = candidate
    if apply_suggestion:
        corrected = max(5.0, min(95.0, candidate - bias))
        suggestion = int(5 * round(corrected / 5.0))
    current_hold = _current_hold_seconds(state, suggestion, now)
    metrics = _sample_metrics(samples, current_hold)
    calibration: dict[str, object] = {
        "calibration_id": str(uuid.uuid4()),
        "candidate_percent": candidate,
        "cap_stage": cap_stage,
        "suggested_percent": suggestion,
        "smoothed_bias_percentage_points": _round_metric(bias),
        "apply_suggestion": apply_suggestion,
        "minimum_samples": MIN_CALIBRATION_SAMPLES,
        "scope": scope,
        "current_unchanged_seconds": current_hold,
        "ignored_history_rows": ignored,
        **metrics,
    }
    state["pending_calibration"] = calibration
    pass_recorder.write_state(session_dir, state)
    print(json.dumps(calibration, indent=2, sort_keys=True))


def _format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if days:
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} {hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def _pass_display(current_pass: dict[str, object]) -> str:
    # An activity is unit-director work with no convergence meaning, so it carries a
    # plain label instead of a pass kind. findings.py counts passes, never these.
    label = pass_recorder.string(current_pass.get("label"))
    if label:
        return label
    pass_kind = pass_recorder.string(current_pass.get("kind"))
    if pass_kind == "fix":
        return f"Fix {pass_recorder.integer(current_pass.get('fix_pass'))}"
    return {
        "impl": "Impl",
        "review": "Review",
        "test": "Test",
        # `arch` is retired, not renamed: escalation is an agent tier, not a kind
        # of work, and `called_task` already records it for 156 fix passes.
        # Four back-corpus passes still spell it, and title-casing renders those.
    }.get(pass_kind, pass_kind.title() or "Pass")


def _progress_decision(
    calibration: dict[str, object] | None,
    raw_percent: int,
    reported_percent: int,
    override_reason: str,
    scope: str,
    cap_stage: str,
) -> dict[str, object]:
    reason_option = (
        "--override-reason" if scope == "legacy" else f"--{scope}-override-reason"
    )
    if calibration is None:
        suggested_percent = raw_percent
        apply_suggestion = False
        historical_bias: float | None = None
    else:
        candidate_percent = pass_recorder.integer(calibration.get("candidate_percent"), raw_percent)
        if candidate_percent != raw_percent:
            raise SystemExit(
                f"The pending {scope} calibration candidate does not match its raw percent; "
                + "run calibrate again"
            )
        # A suggestion drawn from one stage's history says nothing about another's.
        calibrated_stage = pass_recorder.string(calibration.get("cap_stage"))
        if calibrated_stage and calibrated_stage != cap_stage:
            raise SystemExit(
                f"The pending {scope} calibration was drawn for stage {calibrated_stage}, "
                + f"not {cap_stage or 'none'}; run calibrate again with the same --cap-stage"
            )
        suggested_percent = pass_recorder.integer(calibration.get("suggested_percent"), raw_percent)
        apply_suggestion = calibration.get("apply_suggestion") is True
        bias_value = calibration.get("smoothed_bias_percentage_points")
        historical_bias = (
            pass_recorder.number(bias_value) if isinstance(bias_value, int | float) else None
        )

    if apply_suggestion and reported_percent == suggested_percent:
        decision_source = "calibrated"
    elif not apply_suggestion and reported_percent == raw_percent:
        decision_source = "raw"
    else:
        decision_source = "override"

    cleaned_reason = override_reason.strip()
    if decision_source == "override" and not cleaned_reason:
        raise SystemExit(
            f"{reason_option} is required when the reported percentage differs "
            + "from the applicable raw or calibrated value"
        )
    if decision_source != "override" and cleaned_reason:
        raise SystemExit(f"{reason_option} is valid only for an override decision")

    return {
        "decision_source": decision_source,
        "override_reason": cleaned_reason,
        "suggested_percent": suggested_percent,
        "historical_bias_percentage_points": historical_bias,
        "suggested_adjustment_percentage_points": suggested_percent - raw_percent,
        "reported_adjustment_percentage_points": reported_percent - raw_percent,
    }


def _assessment_timing(
    state: dict[str, object],
    scope: str,
    percent: int,
    now: float,
) -> tuple[float, int]:
    last_key = f"{scope}_last_percent"
    started_key = f"{scope}_percent_started_at"
    if pass_recorder.integer(state.get(last_key), -1) == percent:
        started_at = pass_recorder.number(state.get(started_key), now)
        return started_at, max(0, int(now - started_at))
    state[last_key] = percent
    state[started_key] = now
    return now, 0


def _prefixed_decision(scope: str, decision: dict[str, object]) -> dict[str, object]:
    return {f"{scope}_{key}": value for key, value in decision.items()}


def _work_seconds(
    events: list[dict[str, object]], phase_instance_id: str, started_at: float, until: float
) -> int:
    """The phase's working time from `started_at` to `until`, idle stretches left out.

    A pass or an activity open is work. A stretch with none open counts up to
    `IDLE_GRACE_SECONDS`, the unit director's own work between dispatches; the rest of
    it is waiting, and an ETA extended from a clock that ran through a night quotes
    that night again for the work left.
    """
    opened: dict[str, float] = {}
    windows: list[tuple[float, float]] = []
    for event in events:
        if pass_recorder.string(event.get("phase_instance_id")) != phase_instance_id:
            continue
        event_type = pass_recorder.string(event.get("event_type"))
        key = pass_recorder.string(event.get("pass_instance_id" if event_type.startswith("pass_") else "activity_instance_id"))
        at = min(until, max(started_at, pass_recorder.number(event.get("timestamp_epoch"))))
        if event_type in ("pass_started", "activity_started") and key:
            opened[key] = at
        elif event_type in ("pass_finished", "activity_finished") and key in opened:
            windows.append((opened.pop(key), at))
    windows.extend((start, until) for start in opened.values())
    if not windows:
        # History with no windows says nothing about idleness, so the wall clock stands.
        return max(0, int(until - started_at))
    work, idle_from = 0.0, started_at
    for start, end in merged_spans(windows):
        work += min(start - idle_from, IDLE_GRACE_SECONDS) + end - start
        idle_from = end
    if until > idle_from:
        work += min(until - idle_from, IDLE_GRACE_SECONDS)
    return int(work)


def _clock_line(now: float) -> str:
    """Anchor the durations above to wall-clock time.

    Every other number in the header is a duration, which says how long but
    never when. This line is the one place a reader can tell whether the report
    they are looking at is current.
    """
    return f"**now {datetime.fromtimestamp(now, user_zone()):%Y-%m-%d %H:%M:%S}**"


def _moment_stamp(epoch: float, now: float) -> str:
    """A local clock time, dated only when it falls on a day other than `now`'s."""
    moment = datetime.fromtimestamp(epoch, user_zone())
    if moment.date() == datetime.fromtimestamp(now, user_zone()).date():
        return f"{moment:%H:%M:%S}"
    return f"{moment:%Y-%m-%d %H:%M:%S}"


STAGE_HEADERS: tuple[str, ...] = (
    "Stage",
    "Main",
    "Worker",
    "Start",
    "Elapsed",
    "Result",
)
# The two slots a phase always runs, in the order they are reported: the workers
# that write. A slot is a fixed identity; the role it is doing is what the cell
# shows, so a `test` slot opened as a writer reads "impl" under Agent 2.
ROUND_SLOTS: tuple[str, ...] = ("impl", "test")
# The worker the broad review's craft lens records under. No writer sits there,
# so it is a third column only when a stage the table shows assigned it, and a
# table without one keeps the two columns it always had.
REVIEW_SLOT = "review"
# The statuses that mean a window is being worked right now. Narrower than the
# set `_window_span` runs to the clock: a window left `open` is one whose finish
# was never recorded, so its launcher is gone and its span is merely unbounded.
# Reading that as work is the mistake the worker states exist to stop making.
LIVE_WINDOW_STATUSES: tuple[str, ...] = ("running", "armed")
# A pass kind's display name, where the two differ. `arch` is deliberately
# absent: it is retired rather than renamed, and the handful of back-corpus
# passes carrying it fall through to `.title()` unchanged.
STAGE_KIND_LABELS: dict[str, str] = {"impl": "Impl", "test": "Test"}
SUMMARY_HEADERS: tuple[str, ...] = ("Scope", "%", "Elapsed", "ETA", "Unchanged")
# Every summary column holding a magnitude or a time, named rather than indexed
# so the alignment follows a column that moves. The two scope labels and the
# phase tally read as words and stay left.
RIGHT_ALIGNED_SUMMARY_COLUMNS = frozenset({"%", "Elapsed", "ETA", "ETA low", "ETA high"})
# How much of a worker's last board line the note under the table carries. The
# board caps a post far higher than a terminal line can hold, and a wrapped
# narration costs the reader the three-line scan the note exists to give.
BOARD_NOTE_CHARS = 72
# assign.py prefixes its final `done` and `blocked` board lines with this text.
LAUNCHER_PREFIX = "launcher:"
# What a worker's own narration says when it is alive but not working: held up on
# a peer's edit, on a token, on a gate. Nothing in the pass record separates that
# from work -- the process is up and the window is open either way -- so the last
# board line is the only source there is, and these are the words the workers use.
WAITING_PHRASES: tuple[str, ...] = ("waiting", "wait for", "blocked on", "standing by")
# Where a window that sits in no round is drawn when its kind names a worker. A
# lone reviewer between rounds -- the closure review, the broad review of a
# solo run -- is doing review work and reads in the `test` column, where the
# adversary lens sits; a row of dashes would hide it. A unit-director activity
# names no worker and keeps them.
LONE_WORKERS: dict[str, str] = {"impl": "impl", "fix": "impl", "test": "test", "review": "test"}
# How many stages the round table draws. A long phase runs dozens -- ten repair
# rounds and eleven verifications in the one that prompted the cap -- and a
# table that tall is scrolled past rather than read. What a reader acts on is
# the last few; the rest is history, and `timeline` still renders it in full.
ROUND_TABLE_STAGES = 3
# How close together two role movements are before they are one movement of the
# team. Workers converge in sequence -- each worker's `board.sh role` call landing
# a second or two apart -- and a boundary each would put a near-empty row on
# every call. The cost is small: a movement inside the window describes its row
# from the row's start, at most this many seconds early, which is invisible at
# the minute granularity the table prints.
ROLE_WAVE_SECONDS = 30.0
# The label the director opens an activity with while the UX helper judges the
# phase's shots; review-trial counts that time as review time.
UX_REVIEW_ACTIVITY_LABEL = "UX review"
# What findings.py's `--lens both` stands for: the two lenses the broad review
# ran before `craft` joined them.
LEGACY_BOTH_LENS = "both"
LEGACY_BOTH_LENSES: tuple[str, ...] = ("adversary", "contract")


def _run_events(state: dict[str, object], *,
                record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL
                ) -> list[dict[str, object]]:
    """Every event this run recorded, oldest first.

    Read only this run for its phase table.
    The read cost depends on this run's history.
    """
    history_file = pass_recorder.string(state.get("history_file"))
    if not history_file:
        return []
    try:
        with Path(history_file).open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            lines = handle.read().splitlines()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return []
    events: list[dict[str, object]] = []
    record = staff.initial_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
    for line in lines:
        event = pass_recorder.json_object(line)
        if event is not None and pass_recorder.integer(event.get("schema_version")) == pass_recorder.SCHEMA_VERSION:
            events.append(pass_recorder.staff_directory(event, record))
    return events


def _agent_display(model: str, effort: str) -> str:
    if not model:
        return ""
    return model if not effort or effort == "unset" else f"{model} {effort}"


def _clock_stamp(epoch: float) -> str:
    return f"{datetime.fromtimestamp(epoch, user_zone()):%H:%M:%S}"


def _started_window(event: dict[str, object], event_type: str) -> StageWindow:
    if event_type == "pass_started":
        model, effort = _agent_fields(event, "called_agent")
        instance_id = pass_recorder.string(event.get("pass_instance_id"))
        kind = pass_recorder.string(event.get("pass_kind"))
        label = ""
        started_at = pass_recorder.number(event.get("pass_started_at"))
    else:
        # An activity is the unit director working directly, so it has no worker
        # and the Worker cell stays empty rather than repeating the unit director.
        model, effort = "", ""
        instance_id = pass_recorder.string(event.get("activity_instance_id"))
        kind = "activity"
        label = pass_recorder.string(event.get("activity_label"), "Work")
        started_at = pass_recorder.number(event.get("activity_started_at"))
    main_model, main_effort = _agent_fields(event, "main_agent")
    return StageWindow(
        instance_id=instance_id,
        # An activity is the unit director, which sits in no worker. Passes recorded
        # before the field existed also read empty, and land in the same place:
        # a row of their own rather than a worker's column.
        slot="" if event_type != "pass_started" else pass_recorder.record_slot(event),
        kind=kind,
        fix_round=pass_recorder.integer(event.get("fix_pass")),
        label=label,
        text=pass_recorder.string(event.get("activity_text")) if kind == "activity" else "",
        started_at=started_at or pass_recorder.number(event.get("timestamp_epoch")),
        elapsed=0,
        status="open",
        result="",
        worker=_agent_display(model, effort),
        main=_agent_display(main_model, main_effort),
    )


def _running_window(
    current: dict[str, object],
    slot: str,
    kind: str,
    label: str,
    worker: str,
    main: str,
    fallback_id: str,
    now: float,
    text: str = "",
) -> StageWindow:
    started_at = pass_recorder.number(current.get("started_at"), now)
    return StageWindow(
        instance_id=pass_recorder.string(current.get("instance_id")) or fallback_id,
        slot=slot,
        kind=kind,
        fix_round=pass_recorder.integer(current.get("fix_pass")),
        label=label,
        text=text,
        started_at=started_at,
        elapsed=max(0, int(now - started_at)),
        status="running",
        result="running",
        worker=worker,
        main=main,
    )


def _live_windows(state: dict[str, object], now: float) -> list[StageWindow]:
    """The windows running right now, read from state rather than the events.

    Their finishing events do not exist yet, and a session upgraded mid-phase
    has an activity whose start event predates activity identity, so state is
    the only source that always knows what is running. A phase team runs a
    launcher per slot and each opens its own pass, so this is a row apiece; the
    unit director's activity is the running window only when no pass is open at
    all, which is what makes verification a row and never a second one beside a
    dispatch.
    """
    main = _agent_display(*_agent_fields(state, "main_agent"))
    open_passes = pass_recorder.open_passes(state)
    if open_passes:
        return [
            _running_window(
                current,
                slot,
                pass_recorder.string(current.get("kind")),
                "",
                _agent_display(*_agent_fields(current, "called_agent")),
                main,
                f"live:{slot}",
                now,
            )
            for slot, current in sorted(open_passes.items())
        ]
    activity = pass_recorder.object_dict(state.get("activity"))
    if activity is None or pass_recorder.string(activity.get("status")) != "active":
        return []
    return [
        _running_window(
            activity,
            "",
            "activity",
            pass_recorder.string(activity.get("label"), "Work"),
            "",
            main,
            "live",
            now,
            text=pass_recorder.string(activity.get("activity")),
        )
    ]


def _launcher_file_written_since(path_value: str, epoch: float) -> str | None:
    """A launcher-written file's contents, or None when it predates `epoch`."""
    if not path_value:
        return None
    path = Path(path_value)
    try:
        if path.stat().st_mtime <= epoch:
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _early_reviewer_working(armed: dict[str, object]) -> bool:
    """Whether the launcher behind an armed row is still doing the review.

    Two files `review.sh` writes, because neither covers the other. Its status
    catches a reviewer that failed before delivery and left the marker standing;
    its pid catches a kill, which writes no status at all.

    Both are believed only once written after the arm. At launch each still
    holds the previous pass's value, and reading that would retire the row
    before this reviewer had drawn breath -- the same stale-artifact trap the
    early launch itself has to clear before it starts.
    """
    started_at = pass_recorder.number(armed.get("started_at"))
    status = _launcher_file_written_since(pass_recorder.string(armed.get("status_file")), started_at)
    if status is not None and status != "reviewing":
        return False
    pid = _launcher_file_written_since(pass_recorder.string(armed.get("pid_file")), started_at)
    if pid is None or not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _armed_window(
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
) -> StageWindow | None:
    """The early reviewer's row, running beside the pass it overlaps.

    Rendered from the marker rather than an event, and dropped the moment the
    launcher that owns it is gone: a marker outliving a killed reviewer would
    show a phantom agent working, which is worse than showing nothing.
    """
    armed = pass_recorder.object_dict(state.get(pass_recorder.ARMED_REVIEW_KEY))
    if armed is None or pass_recorder.string(armed.get("status")) != "armed":
        return None
    if pass_recorder.string(armed.get("phase_instance_id")) != phase_instance_id:
        return None
    if not _early_reviewer_working(armed):
        return None
    model, effort = _agent_fields(armed, "called_agent")
    main_model, main_effort = _agent_fields(state, "main_agent")
    started_at = pass_recorder.number(armed.get("started_at"), now)
    return StageWindow(
        instance_id=pass_recorder.string(armed.get("instance_id")) or "armed",
        # The early reviewer occupies the `test` worker before its pass exists --
        # where the adversary lens sits and where a lone closure reviewer reads
        # -- so it claims that column. Its round is left at zero and resolved
        # against the phase in `_round_windows`: the marker is written before
        # the round it reads is known, and the writer running beside it is what
        # defines it.
        slot="test",
        kind="review",
        fix_round=0,
        label="",
        text="",
        started_at=started_at,
        elapsed=max(0, int(now - started_at)),
        status="armed",
        result="running (early)",
        worker=_agent_display(model, effort),
        main=_agent_display(main_model, main_effort),
    )


def _phase_windows(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool,
) -> list[StageWindow]:
    windows: dict[str, StageWindow] = {}
    for event in events:
        if pass_recorder.string(event.get("phase_instance_id")) != phase_instance_id:
            continue
        event_type = pass_recorder.string(event.get("event_type"))
        if event_type in ("pass_started", "activity_started"):
            window = _started_window(event, event_type)
            if window["instance_id"]:
                windows[window["instance_id"]] = window
            continue
        if event_type not in ("pass_finished", "activity_finished"):
            continue
        finished_pass = event_type == "pass_finished"
        key = pass_recorder.string(
            event.get("pass_instance_id" if finished_pass else "activity_instance_id")
        )
        window = windows.get(key)
        if window is None:
            continue
        window["status"] = pass_recorder.string(event.get("status"), "completed")
        window["result"] = pass_recorder.string(event.get("activity_result"))
        window["elapsed"] = pass_recorder.integer(
            event.get(
                "pass_elapsed_seconds" if finished_pass else "activity_elapsed_seconds"
            )
        )
    if include_live:
        for live in _live_windows(state, now):
            started = windows.get(live["instance_id"])
            if started is not None and started["text"]:
                # Every progress tick rewrites the state activity's text with
                # its --activity narration, so the live record no longer says
                # what the window ran. The start event does, and keeps saying
                # it for as long as the window is open.
                live["text"] = started["text"]
            windows[live["instance_id"]] = live
        # A phase team runs a launcher per slot, so several passes are open at
        # once and each has a row above. The armed marker adds one more: an
        # early-launched reviewer reading the diff a writer beside it is still
        # producing, which has no pass of its own until the ready sentinel.
        armed = _armed_window(state, phase_instance_id, now)
        if armed is not None:
            windows[armed["instance_id"]] = armed
    return sorted(windows.values(), key=lambda window: window["started_at"])


def _stage_labels(windows: list[StageWindow]) -> list[str]:
    """Name each window by kind, and a review by the pass whose diff it reads.

    A review row answers "which one" far better as `Impl Review` or `Review Fix
    2` than as an ordinal the reader has to count rows to place, because a
    review always follows the pass it judges. Activities are transparent here:
    verification sits between a fix and its review without breaking the pair. A
    review with no unclaimed pass behind it -- the closure review after
    verification -- keeps the ordinal, which is all that is true of it. A fix
    carries the round the ledger dispatched, so its number is the one
    convergence counts, and the review naming it repeats that number.
    """
    counts: dict[str, int] = {}
    labels: list[str] = []
    reviewed: tuple[str, str] | None = None
    for window in windows:
        kind = window["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        index = counts[kind]
        if kind == "activity":
            labels.append(window["label"] or "Work")
        elif kind == "review":
            if reviewed is None:
                labels.append(f"Review {index}")
            elif reviewed[0] == "fix":
                labels.append(f"Review {reviewed[1]}")
            else:
                labels.append(f"{reviewed[1]} Review")
            reviewed = None
        else:
            if kind == "fix":
                name = f"Fix {window['fix_round'] or index}"
            else:
                base = STAGE_KIND_LABELS.get(kind) or kind.title() or "Pass"
                name = base if index == 1 else f"{base} {index}"
            reviewed = (kind, name)
            labels.append(name)
    return labels


def _finding_tally(
    findings: list[dict[str, object]],
    lower: float,
    upper: float,
) -> FindingTally:
    """What the ledger recorded between one window opening and the next.

    Findings are opened, dispatched, and settled by the unit director in the gap
    after a window closes, so the interval that starts at a window and ends at
    its successor is what attributes them to the pass that produced them.

    This is the `timeline` view's attribution and it only holds where passes run
    one at a time. A phase team opens two at once, and slicing by timestamp
    then gives the first a window under a second wide and hands the whole
    phase to whichever registered last -- so the progress report groups by
    `_round_tally` instead, on a round the ledger already stamps.
    """
    return _tallied(
        event
        for event in findings
        if lower <= pass_recorder.number(event.get("timestamp_epoch")) < upper
    )


def _round_tally(findings: list[dict[str, object]], round_number: int) -> FindingTally:
    """What the ledger recorded for one repair round.

    Exact where a time slice can only estimate: `findings.py` stamps `round` on
    every event it writes, so a finding names the round it belongs to rather
    than being inferred from when it happened. Events predating the field are
    left out entirely instead of defaulting into round zero, which would credit
    the impl round with the whole back corpus.
    """
    return _tallied(
        event
        for event in findings
        if "round" in event and pass_recorder.integer(event.get("round")) == round_number
    )


def _tallied(findings: Iterable[dict[str, object]]) -> FindingTally:
    """Count one already-selected set of finding events."""
    tally = FindingTally(opened=0, landed=0, accepted=0, still_open=0, reopened=0)
    for event in findings:
        event_type = pass_recorder.string(event.get("event_type"))
        if event_type == "finding_opened":
            tally["opened"] += 1
        elif event_type == "finding_batch_landed":
            landed = event.get("landed")
            if isinstance(landed, list):
                tally["landed"] += len(cast(list[object], landed))
        elif event_type == "finding_verdict":
            verdict = pass_recorder.string(event.get("verdict"))
            if verdict == "accepted":
                tally["accepted"] += 1
            elif verdict == "still_open":
                tally["still_open"] += 1
            elif verdict == "reopened":
                tally["reopened"] += 1
    return tally


def _window_result(window: StageWindow, tally: FindingTally) -> str:
    status = window["status"]
    if status in ("running", "armed"):
        return window["result"] or "running"
    if status not in ("completed", "open"):
        return status
    if window["kind"] == "activity":
        return window["result"] or ("open" if status == "open" else "done")
    if status == "open":
        return "open"
    if window["kind"] == "fix":
        return f"{tally['landed']} landed" if tally["landed"] else "done"
    if window["kind"] == "review":
        unresolved = tally["still_open"] + tally["reopened"]
        if tally["accepted"] or unresolved:
            parts = [f"{tally['accepted']} fixed"]
            if unresolved:
                parts.append(f"{unresolved} open")
            if tally["opened"]:
                parts.append(f"{tally['opened']} new")
            return ", ".join(parts)
        return f"{tally['opened']} found" if tally["opened"] else "clean"
    return "done"


class BoardActivity(TypedDict):
    """The latest board line under a worker, and the worker's own last words."""

    at: float
    kind: str
    message: str
    # The worker's latest line that the launcher did not write, empty when the
    # worker has said nothing itself since it was launched. Carried beside
    # `message` because the launcher's `done` lands after the worker's final
    # narration, and a note that read only the latest line reported every
    # finished worker with the same boilerplate.
    own: str


class RoleChange(TypedDict):
    """One moment a slot took a role, as the coordination board recorded it."""

    at: float
    slot: str
    role: str
    # `register` or `handoff`. Only a handoff is a movement: the launchers
    # register a second or two apart, and treating that stagger as role
    # changes splits a round into a row per launch plus the real one.
    kind: str


def _board_stamp(line: str) -> float | None:
    """The epoch a board line was written, or None if it carries no timestamp."""
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\b", line)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return None


def _board_role_changes(session_dir: Path) -> list[RoleChange]:
    """Every role each slot has held this run, in the order it took them.

    Roles move during a phase and the movement is the point: a writer that
    finishes early takes a slice of the tests, and at the end both converge on
    review. The board is what knows -- `board.sh
    role` stamps a `role=<name>` field on each `handoff` so it reads back exactly
    rather than being inferred from prose, and the launcher stamps the same field
    on its `register` line so a slot reports a role from second zero.

    The whole history rather than the latest entry, because a round that changed
    composition part way through has to render as more than one row: collapsing
    to the current answer would report the last shape as though it had held all
    along.
    """
    changes: list[RoleChange] = []
    try:
        text = (session_dir / "board.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return changes
    for line in text.splitlines():
        match = re.search(r"\[([^\]]+)\] (register|handoff): (.*)$", line)
        if match is None:
            continue
        role = re.search(r"\brole=(\w+)", match.group(3))
        at = _board_stamp(line)
        if role is None or at is None:
            continue
        changes.append(
            RoleChange(
                at=at,
                slot=match.group(1),
                role=role.group(1),
                kind=match.group(2),
            )
        )
    return sorted(changes, key=lambda change: change["at"])


def _launcher_authored(kind: str, message: str) -> bool:
    """Identify board lines that assign.py records.

    The launcher records `register` lines and prefixes its final lines with `launcher:`.
    """
    return kind == "register" or message.startswith(LAUNCHER_PREFIX)


def _board_activity(session_dir: Path) -> dict[str, BoardActivity]:
    """The most recent line under each worker on the board, keyed by slot.

    A worker that recorded no pass reaches the table with a role and no duration,
    and a role on its own cannot tell a worker deep in its work from one that
    stopped an hour ago. The board can. Every worker posts what it is about to
    do before doing it, attributed to its slot, so the latest line and its age
    answer the question a pass window alone cannot -- for a worker that has one as
    much as for a worker that does not, since an open window says only that the
    launcher is still waiting.

    The latest line of any kind, not just `status`: a worker whose last act was a
    handoff or a `blocked` is exactly the worker a reader needs to hear about, and
    filtering to narration would render it as silent.

    Beside it, the worker's own last words. The launcher posts `done` when the
    worker exits, which is after the worker's final narration, so the latest line
    of every finished worker is the launcher's boilerplate and the narration the
    reader wanted sits one line up. A `register` clears them: it opens a new
    occupancy of the worker, and words from the previous one do not describe it.
    """
    activity: dict[str, BoardActivity] = {}
    try:
        text = (session_dir / "board.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return activity
    for line in text.splitlines():
        match = re.search(r"\[([^\]]+)\] (\w+): (.*)$", line)
        at = _board_stamp(line)
        if match is None or at is None:
            continue
        slot, kind, message = match.group(1), match.group(2), match.group(3).strip()
        previous = activity.get(slot)
        if kind == "register":
            own = ""
        elif _launcher_authored(kind, message):
            own = previous["own"] if previous is not None else ""
        else:
            own = message
        activity[slot] = BoardActivity(at=at, kind=kind, message=message, own=own)
    return activity


def _board_phrase(said: BoardActivity) -> str:
    """One worker's last board line, cut to fit a terminal row.

    A `status` is narration and reads as itself. Any other kind is named,
    because the kind is then the news -- `blocked` and `done` change what the
    reader should do next, where a bare message would look like more work in
    progress. The words are the worker's own wherever it has any: the launcher's
    `done` supplies the kind and the worker's last narration supplies what it was
    doing when it finished, which is what the reader asked. A leading `role=`
    field is dropped: the role it announces is already in that worker's column,
    and repeating it here spends the line.
    """
    message = said["own"] or said["message"].removeprefix(LAUNCHER_PREFIX)
    message = re.sub(r"^\s*role=\S+\s*", "", message).strip()
    body = message if said["kind"] == "status" else f"{said['kind']}: {message}"
    body = body.strip() or said["kind"]
    if len(body) > BOARD_NOTE_CHARS:
        body = body[: BOARD_NOTE_CHARS - 1].rstrip() + "\u2026"
    return body


def _roles_over(changes: list[RoleChange], upper: float) -> dict[str, str]:
    """What each slot was doing across one stretch of a round.

    Any entry before the stretch ends describes it from the start. For a
    register that is the roll-call reasoning: the launchers land a second
    or two apart, and reading them strictly would show a worker idle while the
    third works. For a handoff it holds because `_round_segments` opens a
    boundary at every movement wave: a handoff inside this stretch is either
    the movement that opened it or a member of that movement's wave, seconds
    behind the boundary, and both belong to the whole stretch.
    """
    roles: dict[str, str] = {}
    for change in changes:
        if change["at"] < upper:
            roles[change["slot"]] = change["role"]
    return roles


def _role_movements(changes: list[RoleChange]) -> list[RoleChange]:
    """The handoffs that changed what their slot was doing.

    Workers re-announce the role they already hold -- an opening `board.sh role`
    seconds after the launcher registered the same role, or narration routed
    through `role` because it is the command that takes a note. Neither moves
    the team, and a row split on one shows columns identical to the row
    above with a fresh clock: noise with a timestamp. A movement is a handoff
    whose role differs from the one its slot was last reported doing, by any
    entry kind -- a register opens an occupancy with a role, and re-stating that
    role is no more a movement than re-stating a handoff's.
    """
    current: dict[str, str] = {}
    movements: list[RoleChange] = []
    for change in changes:
        if change["kind"] == "handoff" and current.get(change["slot"]) != change["role"]:
            movements.append(change)
        current[change["slot"]] = change["role"]
    return movements


def _compact_duration(seconds: int) -> str:
    """A duration narrow enough to share a cell with a role word.

    `_format_duration`'s `HH:MM:SS` is right for a column of its own, where the
    colons align and the reader compares magnitudes down the column. Two of
    these sit side by side in one row instead, each behind a role, so the unit
    is spelled and the leading zero components are dropped.
    """
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def _round_result(tally: FindingTally, running: bool) -> str:
    """A round's outcome: what it settled, what it left, what it turned up."""
    unresolved = tally["still_open"] + tally["reopened"]
    parts: list[str] = []
    if tally["accepted"]:
        parts.append(f"{tally['accepted']} fixed")
    if unresolved:
        parts.append(f"{unresolved} open")
    if tally["opened"]:
        # "found" alone on the impl round, where nothing had been found before;
        # "new" once a round has settled something, because there the count is
        # what this round added on top rather than the phase total.
        parts.append(f"{tally['opened']} new" if parts else f"{tally['opened']} found")
    if parts:
        return ", ".join(parts)
    return "running" if running else "clean"


def _gate_report(
    label: str,
    block: list[StageWindow],
    now: float,
) -> tuple[str, list[str]]:
    """One verification block's Result cell, and the gate notes under the table.

    A block is a run of consecutive unit-director activities sharing a label: the
    phase gates, run one command at a time. Each distinct command is one gate,
    and a window repeating an earlier command is the same gate run again after
    a repair -- one line telling fail-then-pass is the story, where a row per
    command rendered a healthy retry as a stretch of near-identical rows the
    reader could tell apart only by result.
    """
    gates: dict[str, list[StageWindow]] = {}
    for window in block:
        gates.setdefault(window["text"] or window["label"], []).append(window)
    lines: list[str] = []
    failing = reran = 0
    live_position = 0
    for position, (text, runs) in enumerate(gates.items(), start=1):
        last = runs[-1]
        earlier_fails = sum(
            1 for run in runs[:-1] if run["status"] not in LIVE_WINDOW_STATUSES
        )
        retried = (
            f"failed {earlier_fails}x"
            if earlier_fails > 1
            else f"{_compact_duration(runs[0]['elapsed'])} failed"
        )
        if last["status"] in LIVE_WINDOW_STATUSES:
            live_position = position
            duration = _compact_duration(max(0, int(now - last["started_at"])))
            if earlier_fails:
                lines.append(f"  - ✗→▸ {text} · {retried} · rerunning {duration}")
            else:
                lines.append(f"  - ▸ {text} · running {duration}")
        elif last["status"] == "completed":
            duration = _compact_duration(last["elapsed"])
            if earlier_fails:
                reran += 1
                lines.append(f"  - ✗→✓ {text} · {retried} · passed on rerun {duration}")
            else:
                lines.append(f"  - ✓ {text} · {duration}")
        else:
            failing += 1
            lines.append(f"  - ✗ {text} · {_compact_duration(last['elapsed'])} failed")
    count = len(gates)
    header = f"- **{label}** (unit director) · {count} gate{'s' if count > 1 else ''}:"
    if live_position:
        summary = f"gate {live_position} running"
    else:
        parts: list[str] = []
        if failing:
            parts.append(f"{failing} failing")
        if reran:
            parts.append(f"{reran} failed, reran clean")
        summary = ", ".join(parts) or (f"{count} pass" if count > 1 else "pass")
    return summary, [header, *lines]


def _round_of(window: StageWindow, open_round: int) -> int:
    """The round a window belongs to.

    An armed reviewer is the one window that cannot answer for itself: the
    marker is written before the round it will read has a number, so it takes
    the round of the writer it is running beside.
    """
    return open_round if window["status"] == "armed" else window["fix_round"]


class RoundEntry(TypedDict):
    """One row of the progress report: a round of workers, or a lone window."""

    label: str
    started_at: float
    # The round these workers worked, and None for a window that sits in no round:
    # a unit-director activity, or a pass from a run that recorded no worker at all.
    # The distinction picks the row's finding attribution, which is the whole
    # reason the two shapes stay separable rather than being forced into one.
    number: int | None
    members: list[StageWindow]


def _round_entries(windows: list[StageWindow], labels: list[str]) -> list[RoundEntry]:
    """The phase's rows: one per round, plus one per window that has no round.

    A round is the unit that advances -- it is dispatched, it lands, and the
    ledger stamps it on every finding it produces -- so it is the row, and the
    workers working it read across as columns. Everything else keeps a row
    of its own: verification is the unit director between rounds, in no worker and in
    no round, and a solo run records no worker at all, so both render one window
    per row exactly as they did before the phase team existed.
    """
    rounds = {
        window["fix_round"]
        for window in windows
        if window["slot"] and window["status"] != "armed"
    }
    open_round = max(rounds, default=0)
    grouped: dict[int, list[StageWindow]] = defaultdict(list)
    entries: list[RoundEntry] = []
    for index, window in enumerate(windows):
        # An armed reviewer joins the round it reads only when a round exists to
        # join. In a solo run it is the only assigned window there is, and it
        # belongs beside the writer in a row of its own, as it always rendered.
        lone = not window["slot"] or (window["status"] == "armed" and not rounds)
        if lone:
            entries.append(
                RoundEntry(
                    label=labels[index],
                    started_at=window["started_at"],
                    number=None,
                    members=[window],
                )
            )
            continue
        grouped[_round_of(window, open_round)].append(window)
    for number, members in grouped.items():
        # Round zero is whatever the phase opened with -- an implementation, or a
        # review of work that arrived already written -- so it is named for the
        # kind its workers actually ran. Every later round is a repair round, and
        # carries the number convergence counts.
        if number:
            label = f"Fix {number}"
        else:
            kinds = {member["kind"] for member in members}
            opening = next((k for k in ("impl", "fix", "test", "review") if k in kinds), "")
            label = STAGE_KIND_LABELS.get(opening) or opening.title() or "Impl"
        entries.append(
            RoundEntry(
                label=label,
                started_at=min(member["started_at"] for member in members),
                number=number,
                members=members,
            )
        )
    return sorted(entries, key=lambda entry: entry["started_at"])


def _stage_groups(entries: list[RoundEntry]) -> list[list[RoundEntry]]:
    """The phase's stages: consecutive entries sharing a label are one stage.

    Only a window in no round repeats a label -- a round carries its number --
    and those come in runs: the four verifications a repair round triggers land
    seconds apart and are one stage of the phase, not four. Counting them singly
    would let a run of them fill the whole table and push the repair work that
    produced them off the bottom.
    """
    groups: list[list[RoundEntry]] = []
    for entry in entries:
        if groups and groups[-1][0]["label"] == entry["label"]:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def _earlier_note(elided: list[list[RoundEntry]]) -> str:
    """The one line standing in for the stages the table does not draw.

    Without it a phase eleven hours and twenty stages deep opens on `Fix 9` and
    reads as one that has barely started, and the reader cannot tell whether the
    missing rows are a truncation or an absence.
    """
    if not elided:
        return ""
    first, last = elided[0][0]["label"], elided[-1][-1]["label"]
    span = first if first == last else f"{first} through {last}"
    stages = "stage" if len(elided) == 1 else "stages"
    return f"*Earlier: {len(elided)} {stages} not shown - {span}.*"


def _window_span(window: StageWindow, now: float) -> tuple[float, float]:
    """When a window opened and when it closed, an open one running to now."""
    started_at = window["started_at"]
    if window["status"] in ("running", "armed", "open"):
        return started_at, max(now, started_at)
    return started_at, started_at + window["elapsed"]


def _round_segments(
    entry: RoundEntry,
    changes: list[RoleChange],
    now: float,
) -> list[tuple[float, float, dict[str, str]]]:
    """A round split at every moment its workers changed what they were doing.

    A round is not one shape held to the end. Workers get recruited across as the
    work moves -- one writing while the other tests, then both writing, then
    both converging on review -- and a single row would report whichever shape happened to be last
    as though it had held throughout. Each segment is a stretch over which no
    worker changed role, so a row per segment tells the reader what the team was
    when, which is what the board records and nothing else preserves.
    """
    start = entry["started_at"]
    end = max(
        (_window_span(member, now)[1] for member in entry["members"]),
        default=max(now, start),
    )
    end = max(end, start)
    # Only a movement splits: a register is a launcher opening its slot, and a
    # handoff that re-states the role its slot already holds moves nothing, so
    # splitting on either repeats the row above with a fresh clock. A movement
    # exactly at the round's start opens it rather than splitting it, and one
    # at or past its end belongs to the round that follows.
    # A worker re-occupied is the other way a round moves on, and the one no
    # handoff marks: a blind reviewer is a fresh session that takes the chair
    # after the writer's has exited, so nothing posts a movement between them.
    # One boundary per wave rather than per movement -- workers converge seconds
    # apart, and a boundary each would put a near-empty row on every one, so a
    # candidate within ROLE_WAVE_SECONDS of the boundary before it joins that
    # boundary's row instead of opening its own.
    occupancies = _slot_occupancies(entry["members"])
    depth = max((len(windows) for windows in occupancies.values()), default=0)
    waves = {
        min(
            windows[index]["started_at"]
            for windows in occupancies.values()
            if len(windows) > index
        )
        for index in range(1, depth)
    }
    # A worker outside the phase's opening impl/test pair joins the round when its
    # first occupancy begins. Treat that as a reassigning workers wave just like the
    # original craft-review worker, so `review2` and `fix2` do not appear to have
    # worked from the writers' start time.
    for slot, assigned in occupancies.items():
        if slot not in ROUND_SLOTS and assigned:
            waves.add(assigned[0]["started_at"])
    candidates = sorted({change["at"] for change in _role_movements(changes)} | waves)
    boundaries = [start]
    for at in candidates:
        if start < at < end and at - boundaries[-1] >= ROLE_WAVE_SECONDS:
            boundaries.append(at)
    segments: list[tuple[float, float, dict[str, str]]] = []
    for index, lower in enumerate(boundaries):
        upper = boundaries[index + 1] if index + 1 < len(boundaries) else end
        segments.append((lower, upper, _roles_over(changes, upper)))
    return segments


def _slot_occupancies(members: list[StageWindow]) -> dict[str, list[StageWindow]]:
    """Each worker's windows in one round, oldest first.

    A worker sits down more than once in a round wherever the round runs a stage
    after the one it opened with: the writers finish, and the reviewers that
    read what they wrote take the same chairs. Keeping one
    member per worker reported whichever occupancy happened to be last as though
    it had held the round from the start.
    """
    occupancies: dict[str, list[StageWindow]] = defaultdict(list)
    for member in members:
        occupancies[member["slot"]].append(member)
    for windows in occupancies.values():
        windows.sort(key=lambda window: window["started_at"])
    return occupancies


def _worker_is_waiting(said: BoardActivity | None) -> bool:
    """Whether a worker's last board line says it is held up rather than working."""
    if said is None or said["message"].startswith(LAUNCHER_PREFIX):
        # A launcher writes one line only as its worker exits, and a worker that has
        # exited is not waiting on anything.
        return False
    if said["kind"] == "blocked":
        return True
    text = (said["own"] or said["message"]).lower()
    return any(phrase in text for phrase in WAITING_PHRASES)


def _worker_state(member: StageWindow, live: bool, said: BoardActivity | None) -> str:
    """What this worker is doing at the end of the stretch its cell covers.

    The duration cannot say it. An open window and a closed one both render a
    number, and a number that stopped growing looks exactly like one still
    growing, so the reader's first question -- is anyone still working -- is the
    one the row could not answer.

    `done` is every stretch that is already over, which is every row but the
    last of a live round. Inside that last row three answers separate. A worker
    whose window has closed is `idle`: finished with what it was given while the
    round it belongs to is not, so it is a worker available for more work rather
    than a phase that ended. A worker whose window is open is working, unless its
    own narration says it is held up on a peer, a token, or a gate -- which the
    records cannot see, and which is `waiting`. Both workers reading `running`
    while one sits on the other is the picture this exists to correct.
    """
    if not live:
        return "done"
    if member["status"] not in LIVE_WINDOW_STATUSES:
        return "idle"
    return "waiting" if _worker_is_waiting(said) else "running"


def _round_cells(
    members: list[StageWindow],
    roles: dict[str, str],
    lower: float,
    upper: float,
    now: float,
    live: bool,
    activity: dict[str, BoardActivity],
    slots: tuple[str, ...],
) -> list[str]:
    """A cell per worker in `slots`: its role over this stretch, how long, and its state."""
    occupancies = _slot_occupancies(members)
    cells: list[str] = []
    for slot in slots:
        # The occupancy this stretch falls in: the last one that had begun by
        # the time the stretch ended.
        assigned = occupancies.get(slot, [])
        member = next(
            (window for window in reversed(assigned) if window["started_at"] < upper),
            None,
        )
        if member is None:
            # A worker that recorded no pass: it was launched without a pass
            # kind, or it died before `start-pass`. The board still knows its
            # role, and knows it after the round closes -- the old reading gated
            # this on the round running, so a finished round rendered its other
            # workers as "-" and read as a solo pass. The duration is what is
            # genuinely unknown here; the worker is not.
            cells.append(roles.get(slot) or "-")
            continue
        # The board's role wins wherever it has one, for this stretch rather than
        # for now: it is the only source that tracks a worker recruited across, and
        # reading it per segment is what keeps an earlier row describing the team
        # that was. A worker whose board never spoke reports the kind it recorded.
        # Only for the worker's opening occupancy, though: a later one is a
        # different session in the same chair, and the role the last occupant
        # left behind describes work that is already over.
        role = (roles.get(slot) or member["kind"]) if member is assigned[0] else member["kind"]
        opened, closed = _window_span(member, now)
        overlap = int(min(closed, upper) - max(opened, lower))
        cells.append(
            " ".join(
                word
                for word in (
                    role,
                    _compact_duration(overlap) if overlap > 0 else "",
                    _worker_state(member, live, activity.get(slot)),
                )
                if word
            )
        )
    return cells


def _lone_cells(
    window: StageWindow,
    activity: dict[str, BoardActivity],
    slots: tuple[str, ...],
) -> list[str]:
    """The worker cells of a window in no round: its kind's worker filled, or none."""
    cells = ["-"] * len(slots)
    worker = LONE_WORKERS.get(window["kind"])
    if worker is not None:
        # One worker, so there is no round to outlive: it is either running or
        # over, and `idle` -- a worker free while its round continues -- cannot
        # arise here.
        live = window["status"] in LIVE_WINDOW_STATUSES
        said = activity.get(window["slot"]) if window["slot"] else None
        cells[slots.index(worker)] = " ".join(
            (
                window["kind"],
                _compact_duration(window["elapsed"]),
                _worker_state(window, live, said),
            )
        )
    return cells


class RoundTable(TypedDict):
    """The rows the round table draws, and the note for the ones it drops."""

    rows: list[list[str]]
    earlier: str
    # The gate notes for the newest collapsed verification block: its header
    # line and one line per gate. Empty when the shown stages hold no such
    # block. Only the newest block's -- an earlier one keeps its row and its
    # tally, but its gate-by-gate story is history the reader is not acting on.
    gates: list[str]
    # The workers the rows have cells for, in column order: ROUND_SLOTS, and the
    # review worker after them when a shown stage assigned it.
    slots: tuple[str, ...]


def _round_headers(slots: tuple[str, ...]) -> list[str]:
    """The round table's header, with a column for each worker in `slots`."""
    return [
        "Stage",
        "Start",
        "Elapsed",
        *(f"Agent {index}" for index, _slot in enumerate(slots, start=1)),
        "Result",
    ]


def _reported_slots(
    shown: list[list[RoundEntry]],
    windows: list[StageWindow],
    changes: list[RoleChange],
) -> tuple[str, ...]:
    """The worker columns this phase needs, in first-seen order after impl/test.

    The writers' two always, so a phase without a craft review keeps the table
    it always drew. The original `review` worker retains its prior visibility
    rule when it only recorded a pass: it appears while a shown stage workers it,
    keeping existing impl/test/review tables byte-identical. Every other worker
    that recorded a phase pass, plus every worker with a board handoff, gets a
    column ordered by the first of those records.
    """
    shown_assigned = {
        member["slot"]
        for group in shown
        for entry in group
        for member in entry["members"]
    }
    appearances: list[tuple[float, str]] = []
    for window in windows:
        slot = window["slot"]
        if not slot or slot in ROUND_SLOTS:
            continue
        if slot == REVIEW_SLOT and REVIEW_SLOT not in shown_assigned:
            continue
        appearances.append((window["started_at"], slot))
    appearances.extend(
        (change["at"], change["slot"])
        for change in changes
        if change["kind"] == "handoff" and change["slot"] not in ROUND_SLOTS
    )
    extra: list[str] = []
    for _at, slot in sorted(appearances, key=lambda appearance: appearance[0]):
        if slot not in extra:
            extra.append(slot)
    return (*ROUND_SLOTS, *extra)


def _round_table(
    windows: list[StageWindow],
    labels: list[str],
    findings: list[dict[str, object]],
    changes: list[RoleChange],
    activity: dict[str, BoardActivity],
    now: float,
) -> RoundTable:
    """The last ROUND_TABLE_STAGES stages, each split wherever its workers moved.

    A stage keeps every row it owns: a repair round that recruited two workers
    across renders the two rows that say so. The cap counts stages rather than
    rows because a row is not a unit a reader asks for -- "what happened in the
    last few stages" is the question, and a stage answers it whole.
    """
    # A window with no round is attributed the way the stage table always did
    # it, by the interval running to whatever opened next. That slice is wrong
    # for concurrent workers, which is why a round does not use it -- but it is
    # exactly right for the sequential windows that still reach this branch.
    # Its worker cells come from its kind: a lone review sits under the test worker.
    uppers = {
        window["instance_id"]: (
            windows[index + 1]["started_at"]
            if index + 1 < len(windows)
            else max(now, window["started_at"]) + 1.0
        )
        for index, window in enumerate(windows)
    }
    groups = _stage_groups(_round_entries(windows, labels))
    shown = groups[-ROUND_TABLE_STAGES:]
    slots = _reported_slots(shown, windows, changes)
    rows: list[list[str]] = []
    gate_lines: list[str] = []
    for group in shown:
        # A stage of two or more unit-director activities is a verification block:
        # the gates of the phase, run one command at a time. One row for the
        # block, with the gate-by-gate story -- which command, what happened,
        # retries folded into their gate -- in the notes under the table, where
        # a row per command told the reader nothing but pass/error.
        if len(group) > 1 and all(
            entry["number"] is None and entry["members"][0]["kind"] == "activity"
            for entry in group
        ):
            block = [entry["members"][0] for entry in group]
            summary, notes = _gate_report(group[0]["label"], block, now)
            block_start = block[0]["started_at"]
            block_end = max(_window_span(window, now)[1] for window in block)
            rows.append(
                [
                    group[0]["label"],
                    _clock_stamp(block_start),
                    _compact_duration(int(block_end - block_start)),
                    *(["-"] * len(slots)),
                    summary,
                ]
            )
            gate_lines = notes
            continue
        for entry in group:
            members, started_at = entry["members"], entry["started_at"]
            running = any(member["status"] in LIVE_WINDOW_STATUSES for member in members)
            if entry["number"] is None:
                lone = members[0]
                tally = _finding_tally(findings, started_at, uppers[lone["instance_id"]])
                rows.append(
                    [
                        entry["label"],
                        _clock_stamp(started_at),
                        _compact_duration(lone["elapsed"]),
                        *_lone_cells(lone, activity, slots),
                        _window_result(lone, tally),
                    ]
                )
                continue
            segments = _round_segments(entry, changes, now)
            previous_roles: list[str] = []
            for index, (lower, upper, roles) in enumerate(segments):
                last = index + 1 == len(segments)
                # Only the closing row of a live round is the present. Every
                # earlier segment is a stretch that ended, whatever its workers
                # are doing now.
                cells = _round_cells(
                    members, roles, lower, upper, now, running and last, activity, slots
                )
                # A continuation row exists because the team moved, and the
                # reader should not have to diff two rows of cells to find
                # where. The row's result cell says so in the table's own
                # column terms, from the rendered cells rather than the board
                # -- what it reports is exactly what changed on screen, whether
                # a handoff or a reassigned chair caused it.
                worker_roles = [cell.split()[0] for cell in cells]
                moved = ", ".join(
                    f"Agent {position} → {role}"
                    for position, (_slot, before, role) in enumerate(
                        zip(slots, previous_roles, worker_roles), start=1
                    )
                    if role != before and role != "-"
                )
                previous_roles = worker_roles
                # The result belongs to the round, which is the granularity the
                # ledger stamps, so it sits on the row that closes it, after
                # the movement that opened the row if one did.
                outcome = (
                    _round_result(_round_tally(findings, entry["number"]), running)
                    if last
                    else ""
                )
                # The round label names the round once. A continuation row is
                # the same round with its workers moved, and repeating the name
                # there would read as a second round rather than as the team
                # having moved.
                rows.append(
                    [
                        entry["label"] if index == 0 else "",
                        _clock_stamp(lower),
                        _compact_duration(int(upper - lower)),
                        *cells,
                        "; ".join(part for part in (moved, outcome) if part),
                    ]
                )
    return RoundTable(
        rows=rows,
        earlier=_earlier_note(groups[: len(groups) - len(shown)]),
        gates=gate_lines,
        slots=slots,
    )


def _current_worker_windows(windows: list[StageWindow]) -> dict[str, StageWindow]:
    """Each worker's live window, otherwise its most recent finished window."""
    current: dict[str, StageWindow] = {}
    for window in windows:
        slot = window["slot"]
        if not slot:
            continue
        previous = current.get(slot)
        if previous is None or (
            window["status"] in LIVE_WINDOW_STATUSES, window["started_at"]
        ) >= (previous["status"] in LIVE_WINDOW_STATUSES, previous["started_at"]):
            current[slot] = window
    return current


def _current_worker_cells(
    windows: list[StageWindow],
    labels: list[str],
    changes: list[RoleChange],
    activity: dict[str, BoardActivity],
    slots: tuple[str, ...],
    now: float,
) -> tuple[str, ...]:
    """Current worker details, independent of the director's latest activity."""
    current = _current_worker_windows(windows)
    roles = _roles_over(changes, now + 1)
    live_round = {
        member["instance_id"]: any(peer["status"] in LIVE_WINDOW_STATUSES for peer in entry["members"])
        for entry in _round_entries(windows, labels)
        for member in entry["members"]
    }
    cells: list[str] = []
    for slot in slots:
        window = current.get(slot)
        if window is None:
            cells.append(roles.get(slot) or "-")
            continue
        cells.append(" ".join((
            roles.get(slot) or window["kind"],
            _compact_duration(window["elapsed"]),
            _worker_state(window, live_round[window["instance_id"]], activity.get(slot)),
        )))
    return tuple(cells)


def _worker_turn_windows(session_dir: Path, windows: list[StageWindow]) -> list[StageWindow]:
    """Show accepted settings for the current staff workers and keep historical windows intact."""
    try:
        membership = staff.local_snapshot()
        workers = {row.id for row in session_staff.run_workers(pass_recorder.read_state(session_dir, record=membership), membership)}
    except (OSError, ValueError, worker_lifecycle.WorkerStateUnreadable):
        return [{**window, "worker": "unavailable"} for window in windows]
    models: dict[str, str] = {}
    for path in session_dir.glob("impl_worker_*.json"):
        try:
            identity = worker_lifecycle.read_worker_state(path)
        except (OSError, ValueError, worker_lifecycle.WorkerStateUnreadable):
            continue
        if identity.get("staff_id") not in workers:
            continue
        settings = worker_lifecycle.recorded_turn_settings(identity)
        slot = pass_recorder.string(identity.get("slot"))
        if settings.model and slot:
            models[slot] = _agent_display(settings.model, settings.effort)
    result: list[StageWindow] = []
    for window in windows:
        current = window.copy()
        if current["slot"] in models:
            current["worker"] = models[current["slot"]]
        result.append(current)
    return result


def _worker_note(
    windows: list[StageWindow],
    activity: dict[str, BoardActivity],
    now: float,
    slots: tuple[str, ...],
    worker_cells: tuple[str, ...] = (),
) -> list[str]:
    """A line per worker the table has a column for: its agent, and its last words.

    Which model is sitting in which worker is the thing a reader cannot recover
    from the table and asks about first when a worker runs long -- and it is not
    the only thing the table cannot say. A duration is the age of an open pass
    window, which grows whether the worker is working or wedged, and a worker that
    recorded no pass shows a role and nothing else. The board line closes both
    gaps: what the worker said last, and how long ago, which is the difference
    between working and stalled.

    It goes under the table rather than into it -- one answer per worker, not per
    row, and a column repeating it down every row would crowd out the roles. The
    unit director is left out; the reader is the unit director. So is a worker the
    table draws no column for: the review worker's board line outlives the phase
    that assigned it, and a line with no column above it names nothing on screen.
    """
    model_windows = list(_current_worker_windows(windows).values()) if worker_cells else windows
    live = {
        window["slot"]: window["worker"]
        for window in model_windows
        if window["slot"] and (worker_cells or window["status"] in LIVE_WINDOW_STATUSES)
    }
    ended = {window["slot"]: window["started_at"] + window["elapsed"] for window in model_windows if window["slot"]}
    lines: list[str] = []
    for position, slot in enumerate(slots, start=1):
        agent, said = live.get(slot), activity.get(slot)
        if agent is None and said is None:
            continue
        # A worker whose stretch ended long ago is not news, and its row crowds out the live ones.
        if (worker_cells and worker_cells[position - 1].endswith(" done")
                and now - max(ended.get(slot, 0.0), said["at"] if said else 0.0) > FINISHED_WORKER_SECONDS):
            continue
        if said is None:
            # Launched and yet to narrate. Naming that is the useful answer: a
            # worker short of its first line is either still starting or gone
            # before it began, and neither should read to the reader as work.
            detail = "no board line yet"
        else:
            age = _compact_duration(max(int(now - said["at"]), 0))
            detail = f"{age} ago \u00b7 {_board_phrase(said)}"
        lines.append(
            f"- **Agent {position}** ({slot})"
            + (f" {worker_cells[position - 1]} \u00b7" if worker_cells else "")
            + f"{f' {agent}' if agent else ''} \u00b7 {detail}"
        )
    return lines

def _stage_table(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool = True,
) -> tuple[list[StageWindow], list[str], list[list[str]]]:
    """The phase's windows, their labels, and the rows rendered from both.

    Callers that only print take the rows; the progress header also needs the
    label of one particular window, which it can no longer find by position now
    that a second row can be running.
    """
    windows = _phase_windows(events, state, phase_instance_id, now, include_live)
    findings = [
        event
        for event in events
        if pass_recorder.string(event.get("phase_instance_id")) == phase_instance_id
        and pass_recorder.string(event.get("event_type")).startswith("finding_")
    ]
    labels = _stage_labels(windows)
    rows: list[list[str]] = []
    for index, window in enumerate(windows):
        upper = (
            windows[index + 1]["started_at"]
            if index + 1 < len(windows)
            else max(now, window["started_at"]) + 1.0
        )
        rows.append(
            [
                labels[index],
                window["main"],
                window["worker"],
                _clock_stamp(window["started_at"]),
                _format_duration(window["elapsed"]),
                _window_result(window, _finding_tally(findings, window["started_at"], upper)),
            ]
        )
    return windows, labels, rows


def _stage_rows(
    events: list[dict[str, object]],
    state: dict[str, object],
    phase_instance_id: str,
    now: float,
    include_live: bool = True,
) -> list[list[str]]:
    return _stage_table(events, state, phase_instance_id, now, include_live)[2]


def _render_table(
    headers: list[str],
    rows: list[list[str]],
    numeric: set[int],
) -> list[str]:
    """Pad a Markdown table so its columns line up as plain text as well."""
    # Three is the narrowest column a Markdown rule can still describe: `--:`
    # keeps the `%` column's alignment marker legal where `:` alone is not.
    widths = [max(len(header), 3) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def rendered(cells: list[str]) -> str:
        padded = [
            cell.rjust(widths[index]) if index in numeric else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        ]
        return "| " + " | ".join(padded) + " |"

    rule = (
        "| "
        + " | ".join(
            "-" * (width - 1) + ":" if index in numeric else "-" * width
            for index, width in enumerate(widths)
        )
        + " |"
    )
    return [rendered(headers), rule, *(rendered(row) for row in rows)]


def _arrival_label(arrival_epoch: float, now: float) -> str:
    """A point in time named the way a person would say it aloud on `now`'s day."""
    arrival = datetime.fromtimestamp(arrival_epoch, user_zone())
    days = (arrival.date() - datetime.fromtimestamp(now, user_zone()).date()).days
    if days == 0:
        return f"today {arrival:%H:%M}"
    if days == 1:
        return f"tomorrow {arrival:%H:%M}"
    return f"{arrival:%Y-%m-%d %H:%M}"


def _eta_cell(percent: int, elapsed: int, as_of: float, now: float, floor: int = 0) -> str:
    """When the work is expected to land, as a clock time rather than a duration.

    A remaining duration has to be added to the current time by hand every time
    it is read, and the answer changes with every report. An arrival stamp is
    that addition already done, and it says the same thing tomorrow morning.
    `elapsed` is measured at `as_of`, which a report between windows sets to
    its last recorded moment; the day is still named from `now`.
    """
    eta = plan_history.eta_seconds(percent, elapsed)
    return "" if eta is None else _arrival_label(as_of + max(eta, floor), now)


def _format_offset(seconds: int) -> str:
    """A swing, as hours and minutes, however many hours that turns out to be.

    `_format_duration` breaks past a day into a `<days> HH:MM:SS` phrase, which
    reads well for an elapsed clock and badly for the two numbers whose whole
    job is to be compared at a glance. One unbroken hours field renders a swing
    of forty minutes and a swing of forty hours the same way.
    """
    hours, remainder = divmod(max(0, seconds), 3600)
    return f"{hours:02d}:{remainder // 60:02d}"


def _eta_band_cells(
    percent: int,
    elapsed: int,
    as_of: float,
    now: float,
    spread: float,
    floor: plan_history.EtaBand | None = None,
) -> tuple[str, str]:
    """The best and worst arrival the percentage beside it still allows.

    The ETA extends the current rate from a percentage that is an estimate, so
    every hour it predicts inherits that estimate's error. Moving the percentage
    a plausible distance each way and re-running the same projection turns that
    error into the two times it actually implies. The band is asymmetric on
    purpose: at 20% a few points of optimism cost far more hours than the same
    few points of pessimism save, and a symmetric band would hide exactly that.

    Each cell carries its own distance from the ETA, so the swing reads off the
    row without subtracting two clock times by hand.

    Neither end is allowed past double or half the reported progress, whatever
    the spread says. A measured error wider than the percentage itself would
    otherwise push the pessimistic end down to 1% and quote an arrival ninety-nine
    times the elapsed clock — a number no reader can use and none should trust.
    """
    band = plan_history.eta_band_seconds(percent, elapsed, spread)
    if isinstance(band, plan_history.EtaProjectionUnavailable):
        return "", ""
    if floor is not None:
        band = plan_history.EtaBand(
            remaining=max(band.remaining, floor.remaining),
            earliest=max(band.earliest, floor.earliest),
            latest=max(band.latest, floor.latest),
        )
    return (
        f"{_arrival_label(as_of + band.earliest, now)} "
        + f"(-{_format_offset(band.remaining - band.earliest)})",
        f"{_arrival_label(as_of + band.latest, now)} "
        + f"(+{_format_offset(band.latest - band.remaining)})",
    )


def _unchanged_cell(seconds: int) -> str:
    return _format_duration(seconds) if seconds > 0 else ""


def _scope_line(state: dict[str, object]) -> str:
    """Where the run is working: the worktree, and the branch when it differs.

    How far through the plan the run is belongs to the Phases column, and the
    phase in flight is named by its id in the Phase row. The line carries no
    position of its own: a plan with deferred earlier phases or retired ids
    puts the active phase at an ordinal that matches neither number.
    """
    worktree = pass_recorder.string(state.get("worktree"))
    branch = pass_recorder.string(state.get("branch"))
    if not branch or branch == worktree:
        return f"**{worktree}**"
    return f"**{worktree} - {branch}**"


def _timeline(args: argparse.Namespace) -> None:
    """Render the stage table for one phase, or for every phase of the run.

    The progress header only ever shows the phase in flight. This answers the
    questions asked after the fact -- how many fix passes, how long each review
    took -- without reading the raw event stream by hand.
    """
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    now = pass_recorder.now_epoch()
    events = _run_events(state)
    wanted = pass_recorder.arg_string(args, "phase")
    started: list[tuple[str, str, str, float]] = []
    finished: dict[str, tuple[str, int]] = {}
    for event in events:
        event_type = pass_recorder.string(event.get("event_type"))
        if event_type == "phase_started":
            started.append(
                (
                    pass_recorder.string(event.get("phase_instance_id")),
                    pass_recorder.string(event.get("phase_id"), "ad hoc"),
                    pass_recorder.string(event.get("phase_title"), "Ad hoc work"),
                    pass_recorder.number(
                        event.get("phase_started_at"),
                        pass_recorder.number(event.get("timestamp_epoch")),
                    ),
                )
            )
        elif event_type == "phase_finished":
            finished[pass_recorder.string(event.get("phase_instance_id"))] = (
                pass_recorder.string(event.get("status"), "completed"),
                pass_recorder.integer(event.get("phase_elapsed_seconds")),
            )
    lines = [
        f"**{pass_recorder.string(state.get('worktree'))} - {pass_recorder.string(state.get('branch'))}**",
    ]
    matched = 0
    for instance_id, phase_id, title, started_at in started:
        if wanted and phase_id != wanted:
            continue
        matched += 1
        live = instance_id not in finished
        status, elapsed = finished.get(
            instance_id,
            ("running", max(0, int(now - started_at))),
        )
        heading = (
            f"Phase {phase_id}: {title} - elapsed {_format_duration(elapsed)}"
            + f" - {status}"
        )
        lines.extend(
            [
                "",
                f"**{heading}**",
                "",
                *_render_table(
                    list(STAGE_HEADERS),
                    _stage_rows(events, state, instance_id, now, include_live=live),
                    set(),
                ),
            ]
        )
    if matched == 0:
        raise SystemExit(
            f"This run recorded no phase {wanted}"
            if wanted
            else "This run has recorded no phase yet"
        )
    print("\n".join(lines))


def _phase_count(args: argparse.Namespace) -> None:
    """Report a plan's phase counts and the project percent they imply.

    Standalone query for the same numbers `progress` derives, so a plan's
    completion can be checked without an active phase and pass.
    """
    plan_doc = pass_recorder.arg_string(args, "plan_doc")
    counts = _count_plan_phases(Path(plan_doc).expanduser().resolve())
    phase_percent = pass_recorder.arg_integer(args, "phase_percent", 0)
    payload = dict(counts)
    payload["project_percent"] = _plan_derived_project_percent(counts, phase_percent)
    print(json.dumps(payload, indent=2, sort_keys=True))


# What a report that describes no live window adds. The recorder knows what its
# own records say and nothing about the processes behind them: a live run once
# had two workers with passes recorded `error` while their workers were still
# posting to the board, and reading the closed passes as a finished dispatch
# would have routed a healthy round through the abandon path.
WORKERS_UNSEEN = (
    "These records say nothing about whether the workers are alive: a pass can be "
    + "closed while its worker still runs. Before concluding anything, check "
    + "impl_status_<slot>, recent board posts, and whether the launcher has exited."
)


class ScopeClock(TypedDict):
    """One summary row's numbers: the percentage and the clocks beside it."""

    percent: int
    elapsed: int
    unchanged: int


class RecordedReport(TypedDict):
    """The summary table's numbers as the phase's last `progress` call left them."""

    at: float
    # What `at` is, in the words the as-of line prints.
    source: str
    project: ScopeClock
    phase: ScopeClock
    calibration: dict[str, object] | None


@dataclass(frozen=True)
class ReportedPassWindow:
    """A progress report that measures a live launcher pass."""

    record: dict[str, object]


@dataclass(frozen=True)
class ReportedActivityWindow:
    """A progress report that describes a live unit-director activity."""

    record: dict[str, object]


@dataclass(frozen=True)
class NoReportedWindow:
    """A progress report made between live windows."""


def _state_plan_phase_counts(state: dict[str, object]) -> dict[str, object]:
    """Count phase headings after resolving the plan against the run folder."""
    plan_doc = pass_recorder.string(state.get("project_plan_doc")) or pass_recorder.string(state.get("plan_doc"))
    if plan_doc:
        candidate = plan_history.resolve_plan_path(Path(pass_recorder.string(state.get("working_dir"))), plan_doc)
        return _with_unlisted_phase(_count_plan_phases(candidate), state, candidate)
    return {"available": False, "reason": "no plan doc"}


def _run_plan_path(state: dict[str, object]) -> Path | None:
    """The plan this run's phase ids come from, which the project plan may not be."""
    plan_doc = pass_recorder.string(state.get("plan_doc"))
    if not plan_doc:
        return None
    return plan_history.resolve_plan_path(Path(pass_recorder.string(state.get("working_dir")) or str(Path.cwd())), plan_doc)


def _with_unlisted_phase(counts: dict[str, object], state: dict[str, object], counted: Path) -> dict[str, object]:
    """Add a numbered run phase when the plan has no heading for it."""
    phase = pass_recorder.object_dict(state.get("phase"))
    phase_id = pass_recorder.string(phase.get("id")) if phase is not None else ""
    order = counts.get("order")
    if (phase is None or re.fullmatch(plan_history.PHASE_ID, phase_id) is None or not isinstance(order, list)
            or _run_plan_path(state) != counted or _plan_lists_phase(counted, phase_id)):
        return counts
    finished = pass_recorder.string(phase.get("status")) == "completed"
    done, todo = pass_recorder.integer(counts.get("done")), pass_recorder.integer(counts.get("todo"))
    return {
        **counts,
        "done": done + int(finished),
        "todo": todo + int(not finished),
        "total": done + todo + 1,
        "order": [*order, phase_id],
    }


def _plan_lists_phase(plan: Path, phase_id: str) -> bool:
    """Return whether a numbered heading names `phase_id`."""
    try:
        phases, _ = plan_history.plan_phase_details(plan)
    except OSError:
        return False
    return any(phase_id == phase["id"] for phase in phases)


def _phase_missing_from_plan(state: dict[str, object], phase_id: str) -> Path | None:
    """Return the run's plan when it lacks the requested numbered phase."""
    if re.fullmatch(plan_history.PHASE_ID, phase_id) is None:
        return None
    plan = _run_plan_path(state)
    if plan is None:
        return None
    listed = _count_plan_phases(plan).get("available") is not True or _plan_lists_phase(plan, phase_id)
    return None if listed else plan


def _reported_window(
    state: dict[str, object],
) -> ReportedPassWindow | ReportedActivityWindow | NoReportedWindow:
    """The live window a report describes, or the state between windows.

    A launcher's pass when one is open, and otherwise the unit director's
    activity. Both render the same line under the round table; only a pass
    carries convergence meaning. Which pass, when a phase team has two of them
    open, is `_reporting_pass`; the round table is where the other is visible.
    """
    current_pass = pass_recorder.reporting_pass(state)
    if current_pass is not None:
        return ReportedPassWindow(current_pass)
    activity = pass_recorder.object_dict(state.get("activity"))
    if activity is not None and pass_recorder.string(activity.get("status")) == "active":
        return ReportedActivityWindow(activity)
    return NoReportedWindow()


def _scope_row(label: str, clock: ScopeClock, as_of: float, now: float, floor: int = 0) -> list[str]:
    return [
        label,
        str(clock["percent"]),
        _format_duration(clock["elapsed"]),
        _eta_cell(clock["percent"], clock["elapsed"], as_of, now, floor),
        _unchanged_cell(clock["unchanged"]),
    ]


def _summary_table(
    phase_id: str,
    project: ScopeClock,
    phase: ScopeClock,
    plan_phase_counts: dict[str, object],
    phase_spread: float,
    as_of: float,
    now: float,
    state: dict[str, object],
) -> list[str]:
    """The clocks table: a project row and a phase row.

    Percentages and elapsed clocks are measured at `as_of`. The project ETA
    follows the current plan chain; every arrival is named from `now`'s day.
    """
    headers = list(SUMMARY_HEADERS)
    project_row = [
        "Project", str(project["percent"]), _format_duration(project["elapsed"]),
        "—", _unchanged_cell(project["unchanged"]),
    ]
    band = plan_history.eta_band_seconds(phase["percent"], phase["elapsed"], phase_spread)
    phase_swing = (None if isinstance(band, plan_history.EtaProjectionUnavailable)
                   else (band.remaining - band.earliest, band.latest - band.remaining))
    project_eta, project_low, project_high = _project_chain_eta(state, now, phase_swing)
    project_row[3] = project_eta
    phase_row = _scope_row(f"Phase {phase_id}", phase, as_of, now)
    if plan_phase_counts.get("available") is True:
        headers.append("Phases")
        done = pass_recorder.integer(plan_phase_counts.get("done"))
        total = pass_recorder.integer(plan_phase_counts.get("total"))
        project_row.append(f"{done} of {total} done")
        phase_row.append("")
    headers.extend(("ETA low", "ETA high"))
    project_row.extend((project_low, project_high))
    phase_row.extend(
        _eta_band_cells(phase["percent"], phase["elapsed"], as_of, now, phase_spread)
    )
    return _render_table(
        headers,
        [project_row, phase_row],
        {index for index, header in enumerate(headers) if header in RIGHT_ALIGNED_SUMMARY_COLUMNS},
    )


def _project_chain_eta(
    state: dict[str, object], now: float, phase_swing: tuple[int, int] | None,
) -> tuple[str, str, str]:
    """Render the same plan finish used by the phase table and vault note.

    The low and high shift the finish by the open phase's swing: its projected
    band, else the range stated with its ETA, else `phase_swing`, the band of
    the report's Phase row. So a stated ETA with no range still gets the
    Phase row's band (user, 2026-10-26). A finished plan's low and high are
    its recorded finish.
    """
    from scripts.direct import phase_table

    plan_doc = pass_recorder.string(state.get("project_plan_doc")) or pass_recorder.string(state.get("plan_doc"))
    if not plan_doc:
        return "—", "—", "—"
    working_dir = Path(pass_recorder.string(state.get("working_dir")) or str(Path.cwd()))
    try:
        record = phase_table.build_plan(plan_history.resolve_plan_path(working_dir, plan_doc))
    except phase_table.NoPlan:
        return "—", "—", "—"
    finish = record.plan_finish
    if not isinstance(finish, phase_table.FinishedAt | phase_table.PredictedFinish):
        return "—", "—", "—"
    target = finish.at.timestamp()
    eta_cell = _arrival_label(target, now)
    current = record.current
    eta = current.eta if isinstance(current, phase_table.OpenPhase) else None
    if isinstance(finish, phase_table.FinishedAt):
        low, high = 0.0, 0.0
    elif isinstance(eta, phase_table.ProjectedEta):
        low = (eta.time - eta.earliest).total_seconds()
        high = (eta.latest - eta.time).total_seconds()
    elif isinstance(eta, phase_table.StatedEta) and isinstance(eta.range, phase_table.EtaRange):
        low = (eta.time - eta.range.earliest).total_seconds()
        high = (eta.range.latest - eta.time).total_seconds()
    elif phase_swing is not None:
        low, high = float(phase_swing[0]), float(phase_swing[1])
    else:
        return eta_cell, "—", "—"
    return (
        eta_cell,
        f"{_arrival_label(target - low, now)} (-{_format_offset(int(low))})",
        f"{_arrival_label(target + high, now)} (+{_format_offset(int(high))})",
    )


def _phase_eta_line(session_dir: Path, phase_number: str) -> str:
    """Describe the phase's promises, never its advanced schedule end."""
    from scripts.direct import phase_table

    try:
        record = phase_table.build(session_dir)
    except (phase_table.NoPlan, phase_table.NoState):
        return "ETA unavailable"
    now = pass_recorder.now_epoch()
    zone = user_zone()

    def promise_time(target: datetime) -> str:
        local = target.astimezone(zone)
        return local.strftime("%H:%M" if local.date() == datetime.fromtimestamp(now, zone).date()
                              else "%a %d %b %H:%M")

    candidates = [*record.phases]
    if isinstance(record.current, phase_table.OpenPhase):
        candidates = [record.current, *candidates]
    for row in candidates:
        if row.phase != phase_number:
            continue
        if not row.eta_trail:
            return "ETA unavailable"
        target = row.eta_trail[-1][0]
        first = row.eta_trail[0][0]
        details: list[str] = []
        if target.timestamp() < now and not isinstance(row, phase_table.DonePhase):
            details.append("passed")
        if row.moves:
            minutes = int((target - first).total_seconds() / 60)
            hours, remainder = divmod(abs(minutes), 60)
            details.extend((
                f"first {promise_time(first)}",
                f"{'+' if minutes >= 0 else '-'}{hours}:{remainder:02d}",
                f"moved {row.moves}\u00d7",
            ))
        else:
            details.append("on its first ETA")
        if row.fix_done or row.fix_running:
            fixes = (
                f"{row.fix_done} fix round{'s' if row.fix_done != 1 else ''}"
                if row.fix_done else "1 fix round running"
            )
            if row.fix_running and row.fix_done:
                fixes += " + 1 running"
            details.append(fixes)
        return f"ETA {promise_time(target)} ({', '.join(details)})"
    return "ETA unavailable"


def _phase_section(
    session_dir: Path,
    state: dict[str, object],
    phase: dict[str, object],
    events: list[dict[str, object]],
    now: float,
    tables: bool = True,
    include_heading: bool = True,
) -> tuple[list[str], list[StageWindow], list[str]]:
    """The phase heading, its round table and the notes under it.

    Opens with a blank line and closes every block with one. Also returns the
    windows and labels the table was built from, which the caller's closing
    line reads.
    """
    phase_instance_id = pass_recorder.string(phase.get("instance_id"))
    stage_windows, stage_labels, _ = _stage_table(events, state, phase_instance_id, now)
    # One row per round, one column per worker. A round is what advances and
    # what the ledger stamps on its findings, so it is the row that can carry
    # a Start, an Elapsed, and a Result that all mean something; the
    # workers working it read across. Per-pass provenance -- which model ran
    # where, and a review overlapping the writer as its own row -- is the
    # `timeline` view, which still renders one row per window, and so is the
    # history behind the last few stages this table keeps.
    phase_findings = [
        event
        for event in events
        if pass_recorder.string(event.get("phase_instance_id")) == phase_instance_id
        and pass_recorder.string(event.get("event_type")).startswith("finding_")
    ]
    board_activity = _board_activity(session_dir)
    role_changes = _board_role_changes(session_dir)
    round_table = _round_table(
        stage_windows,
        stage_labels,
        phase_findings,
        role_changes,
        board_activity,
        now,
    )
    slots = round_table["slots"]
    # A phase that has opened no window yet still draws its table: one row
    # saying so, never a header with nothing under it.
    rows = round_table["rows"] or [["-", "-", "-", *(["-"] * len(slots)), "no stage yet"]]
    worker_note = _worker_note(
        _worker_turn_windows(session_dir, stage_windows), board_activity, now, slots,
        _current_worker_cells(stage_windows, stage_labels, role_changes, board_activity, slots, now)
        if not tables else (),
    )
    title = pass_recorder.string(phase.get("title"), "Ad hoc work")
    lines = ([
        "",
        f"**Phase {pass_recorder.string(phase.get('id'), 'ad hoc')}: {title}**",
        "",
        _phase_eta_line(session_dir, pass_recorder.string(phase.get("id"))),
        "",
    ] if include_heading else []) + [
        *([round_table["earlier"], ""] if tables and round_table["earlier"] else []),
        *([*_render_table(_round_headers(slots), rows, set()), ""] if tables else []),
        *([*round_table["gates"], ""] if round_table["gates"] else []),
        *([*worker_note, ""] if worker_note else []),
    ]
    return lines, stage_windows, stage_labels


def _reported_label(
    windows: list[StageWindow],
    labels: list[str],
    current_pass: dict[str, object],
) -> str:
    """The round-table label of the window this report describes.

    The sentence beneath the table is what a fixed-width Result column cannot
    carry, and it belongs to the window this report is about. That is not
    simply the last row: an early-launched reviewer runs beside the writer and
    starts later, so position would name the reviewer and describe it with the
    writer's activity.
    """
    reported_id = pass_recorder.string(current_pass.get("instance_id"))
    return next(
        (
            labels[index]
            for index, window in enumerate(windows)
            if window["instance_id"] == reported_id
        ),
        labels[-1] if labels else _pass_display(current_pass),
    )


def _recorded_report(
    state: dict[str, object],
    phase: dict[str, object],
    events: list[dict[str, object]],
    plan_phase_counts: dict[str, object],
    now: float,
) -> RecordedReport:
    """The summary numbers the phase's last `progress` call recorded.

    Read from that call's event, the one record carrying the moment it was made
    and the calibration it used. A phase not reported yet reads as of its own
    start: no phase progress, and the project where the plan's phase count puts
    it, held below 100 because a phase is still active.
    """
    instance_id = pass_recorder.string(phase.get("instance_id"))
    last = next(
        (
            event
            for event in reversed(events)
            if pass_recorder.string(event.get("event_type")) == "progress_reported"
            and pass_recorder.string(event.get("phase_instance_id")) == instance_id
        ),
        None,
    )
    fields: dict[str, object] = last if last is not None else {}
    at = pass_recorder.number(fields.get("timestamp_epoch"), pass_recorder.number(phase.get("started_at"), now))
    phase_percent = pass_recorder.integer(fields.get("phase_percent"), pass_recorder.integer(fields.get("percent")))
    derived = _plan_derived_project_percent(plan_phase_counts, phase_percent)
    project_fallback = (
        pass_recorder.integer(state.get("project_last_percent"))
        if derived is None
        else min(derived, PROJECT_CAP_BEFORE_COMPLETE)
    )
    return RecordedReport(
        at=at,
        source=(
            "the last progress report"
            if last is not None
            else "the phase start, before any progress report"
        ),
        project=ScopeClock(
            percent=pass_recorder.integer(fields.get("project_percent"), project_fallback),
            elapsed=max(0, int(at - pass_recorder.number(state.get("project_started_at"), at))),
            unchanged=pass_recorder.integer(fields.get("project_same_percent_elapsed_seconds")),
        ),
        phase=ScopeClock(
            percent=phase_percent,
            elapsed=pass_recorder.integer(
                fields.get("phase_elapsed_seconds"), max(0, int(at - pass_recorder.number(phase.get("started_at"), at)))
            ),
            unchanged=pass_recorder.integer(fields.get("phase_same_percent_elapsed_seconds")),
        ),
        calibration=pass_recorder.object_dict(fields.get("phase_calibration")),
    )


def _print_last_recorded(
    session_dir: Path,
    state: dict[str, object],
    phase: dict[str, object],
    now: float,
    reported_window: ReportedActivityWindow | NoReportedWindow,
    tables: bool = False,
) -> None:
    """Report the last recorded clocks without assessing progress again.

    Between windows -- the reviews closed, the repair writers not launched yet
    -- there is nothing live to measure. The same is true of an activity opened
    after its phase closed: it belongs in the round table, but does not revise
    the closed phase's assessment. The summary table keeps the last recorded
    clocks. With --tables, the round table shows the phase's current windows.
    It writes no event, window or state, so pass counts, convergence and
    calibration never see it.
    """
    events = _run_events(state)
    plan_phase_counts = _state_plan_phase_counts(state)
    recorded = _recorded_report(state, phase, events, plan_phase_counts, now)
    section, stage_windows, stage_labels = _phase_section(
        session_dir, state, phase, events, now, tables=tables
    )
    if isinstance(reported_window, ReportedActivityWindow):
        activity = reported_window.record
        reported_label = _reported_label(stage_windows, stage_labels, activity)
        window_line = f"▸ **{reported_label} - {pass_recorder.string(activity.get('activity'))}**"
    else:
        window_line = f"No pass or activity is open. {WORKERS_UNSEEN}"
    lines = [
        _scope_line(state),
        "",
        f"*Percentages and clocks as of {_moment_stamp(recorded['at'], now)}, "
        + f"{recorded['source']}.*",
        "",
        *_summary_table(
            pass_recorder.string(phase.get("id"), "ad hoc"),
            recorded["project"],
            recorded["phase"],
            plan_phase_counts,
            plan_history.percent_spread(recorded["calibration"]),
            recorded["at"],
            now,
            state,
        ),
        *section,
        window_line,
        _clock_line(now),
    ]
    print("\n".join(lines))


def _report_progress(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    tables = getattr(args, "tables", False) is True
    now = pass_recorder.now_epoch()
    state = _ensure_project_timing(session_dir, pass_recorder.read_state(session_dir), now)
    phase = pass_recorder.object_dict(state.get("phase"))
    reported_window = _reported_window(state)
    if phase is None:
        raise SystemExit(f"No active phase to report: phase missing. {WORKERS_UNSEEN}")
    if pass_recorder.string(phase.get("status")) != "active":
        if isinstance(reported_window, ReportedActivityWindow):
            _print_last_recorded(
                session_dir, state, phase, now, reported_window, tables=tables
            )
            return
        phase_status = pass_recorder.string(phase.get("status")) or "missing"
        raise SystemExit(f"No active phase to report: phase {phase_status}. {WORKERS_UNSEEN}")
    if isinstance(reported_window, NoReportedWindow):
        _print_last_recorded(
            session_dir, state, phase, now, reported_window, tables=tables
        )
        return
    current_pass = reported_window.record
    legacy_raw_percent = pass_recorder.arg_integer(args, "raw_percent", -1)
    legacy_percent = pass_recorder.arg_integer(args, "percent", -1)
    project_raw_percent = pass_recorder.arg_integer(args, "project_raw_percent", -1)
    project_percent = pass_recorder.arg_integer(args, "project_percent", -1)
    phase_raw_percent = pass_recorder.arg_integer(args, "phase_raw_percent", -1)
    phase_percent = pass_recorder.arg_integer(args, "phase_percent", -1)
    uses_dual_layout = any(
        value >= 0
        for value in (
            project_raw_percent,
            project_percent,
            phase_raw_percent,
            phase_percent,
        )
    )
    if uses_dual_layout:
        if legacy_raw_percent >= 0 or legacy_percent >= 0:
            raise SystemExit("Do not combine legacy and project/phase percent options")
        if not all(
            0 <= value <= 100
            for value in (
                project_raw_percent,
                project_percent,
                phase_raw_percent,
                phase_percent,
            )
        ):
            raise SystemExit("All project and phase percent values are required")
    else:
        if not 0 <= legacy_raw_percent <= 100 or not 0 <= legacy_percent <= 100:
            raise SystemExit("--raw-percent and --percent are required for legacy calls")
        phase_raw_percent = legacy_raw_percent
        phase_percent = legacy_percent

    if not 0 <= phase_raw_percent <= 100 or not 0 <= phase_percent <= 100:
        raise SystemExit("Percent values must be between 0 and 100")
    cap_stage = pass_recorder.arg_string(args, "cap_stage")
    phase_calibration = pass_recorder.object_dict(state.get("pending_calibration"))
    phase_override_reason = (
        pass_recorder.arg_string(args, "phase_override_reason")
        if uses_dual_layout
        else pass_recorder.arg_string(args, "override_reason")
    )
    phase_decision = _progress_decision(
        phase_calibration,
        phase_raw_percent,
        phase_percent,
        phase_override_reason,
        "phase" if uses_dual_layout else "legacy",
        cap_stage,
    )
    # The project clock is derived, never estimated. An agent eyeballing phase
    # headings misses the archived ones; counting them here removes the judgment
    # call entirely, and a supplied --project-percent becomes advisory.
    plan_phase_counts = _state_plan_phase_counts(state)
    project_percent_source = "supplied"
    derived_project_percent = _plan_derived_project_percent(
        plan_phase_counts, phase_percent
    )
    if uses_dual_layout and derived_project_percent is not None:
        project_percent_source = "plan_phase_count"
        project_raw_percent = derived_project_percent
        project_percent = derived_project_percent

    project_decision: dict[str, object] | None = None
    if uses_dual_layout:
        project_decision = _progress_decision(
            None,
            project_raw_percent,
            project_percent,
            pass_recorder.arg_string(args, "project_override_reason"),
            "project",
            cap_stage,
        )
    if uses_dual_layout and not cap_stage:
        raise SystemExit(
            "--cap-stage is required: name the gate this phase has actually reached ("
            + ", ".join(sorted(PROGRESS_CAPS))
            + ")"
        )
    phase_uncapped_percent = phase_percent
    project_uncapped_percent = project_percent
    if cap_stage:
        phase_percent = min(phase_percent, PROGRESS_CAPS.get(cap_stage, 100))
        if cap_stage != "complete":
            project_percent = min(project_percent, PROJECT_CAP_BEFORE_COMPLETE)

    activity = pass_recorder.arg_string(args, "activity")
    if activity:
        current_pass["activity"] = activity

    phase_percent_started_at, phase_unchanged_seconds = _assessment_timing(
        state,
        "phase",
        phase_percent,
        now,
    )
    state["last_percent"] = phase_percent
    state["percent_started_at"] = phase_percent_started_at
    project_percent_started_at = now
    project_unchanged_seconds = 0
    if uses_dual_layout:
        project_percent_started_at, project_unchanged_seconds = _assessment_timing(
            state,
            "project",
            project_percent,
            now,
        )

    phase_elapsed = _work_seconds(
        _run_events(state), pass_recorder.string(phase.get("instance_id")), pass_recorder.number(phase.get("started_at"), now), now
    )
    pass_elapsed = max(0, int(now - pass_recorder.number(current_pass.get("started_at"), now)))
    total_elapsed = max(0, int(now - pass_recorder.number(state.get("project_started_at"), now)))
    event = pass_recorder.record_event(state, "progress_reported", now)
    event.update(
        {
            "raw_percent": phase_raw_percent,
            "percent": phase_percent,
            "same_percent_started_at": phase_percent_started_at,
            "same_percent_elapsed_seconds": phase_unchanged_seconds,
            "phase_raw_percent": phase_raw_percent,
            "phase_percent": phase_percent,
            "phase_same_percent_started_at": phase_percent_started_at,
            "phase_same_percent_elapsed_seconds": phase_unchanged_seconds,
            "phase_elapsed_seconds": phase_elapsed,
            "pass_elapsed_seconds": pass_elapsed,
            "total_elapsed_seconds": total_elapsed,
            "cap_stage": cap_stage,
            "phase_uncapped_percent": phase_uncapped_percent,
            "phase_percent_capped_by": (
                cap_stage if phase_percent < phase_uncapped_percent else ""
            ),
            "calibration": phase_calibration,
            "phase_calibration": phase_calibration,
            **phase_decision,
            **_prefixed_decision("phase", phase_decision),
        }
    )
    if uses_dual_layout and project_decision is not None:
        event.update(
            {
                "project_raw_percent": project_raw_percent,
                "project_percent": project_percent,
                "project_percent_source": project_percent_source,
                "project_plan_phase_counts": plan_phase_counts,
                "project_uncapped_percent": project_uncapped_percent,
                "project_same_percent_started_at": project_percent_started_at,
                "project_same_percent_elapsed_seconds": project_unchanged_seconds,
                "project_calibration": None,
                **_prefixed_decision("project", project_decision),
            }
        )
    pass_recorder.append_event(state, event)
    state["pending_calibration"] = None
    # The reported window is the object state already holds, so the activity
    # edit above has landed either way; a pass is written back through its slot,
    # never over the map that holds every slot's.
    if isinstance(reported_window, ReportedActivityWindow):
        state["activity"] = current_pass
    pass_recorder.write_state(session_dir, state)

    phase_id = pass_recorder.string(phase.get("id"), "ad hoc")
    section, stage_windows, stage_labels = _phase_section(
        session_dir, state, phase, _run_events(state), now, tables=tables,
        include_heading=uses_dual_layout,
    )
    reported_label = _reported_label(stage_windows, stage_labels, current_pass)
    display_project_percent = project_percent if uses_dual_layout else (
        derived_project_percent if derived_project_percent is not None else phase_percent
    )
    summary = _summary_table(
        phase_id,
        ScopeClock(
            percent=display_project_percent,
            elapsed=total_elapsed,
            unchanged=project_unchanged_seconds,
        ),
        ScopeClock(
            percent=phase_percent,
            elapsed=phase_elapsed,
            unchanged=phase_unchanged_seconds,
        ),
        plan_phase_counts,
        plan_history.percent_spread(phase_calibration),
        now,
        now,
        state,
    ) if uses_dual_layout or tables else []
    if uses_dual_layout:
        lines = [
            _scope_line(state),
            "",
            *summary,
            *section,
            f"▸ **{reported_label} - {pass_recorder.string(current_pass.get('activity'))}**",
        ]
    else:
        legacy_progress_line = f"**{phase_percent}% complete"
        if phase_unchanged_seconds > 0:
            legacy_progress_line += f" - unchanged for {_format_duration(phase_unchanged_seconds)}"
        lines = [
            f"**{pass_recorder.string(state.get('worktree'))} - {pass_recorder.string(state.get('branch'))}**",
            f"**Phase {phase_id}: {pass_recorder.string(phase.get('title'), 'Ad hoc work')} "
            + f"- elapsed {_format_duration(phase_elapsed)}**",
            _phase_eta_line(session_dir, phase_id),
            f"**{_pass_display(current_pass)} - {pass_recorder.string(current_pass.get('activity'))} "
            + f"- elapsed {_format_duration(pass_elapsed)}**",
            legacy_progress_line + "**",
            f"**Total elapsed {_format_duration(total_elapsed)}**",
            *summary,
            *(section if tables else []),
        ]
    lines.append(_clock_line(now))
    print("\n".join(lines))


def _progress(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    _report_progress(args)
    _refresh_phase_table(session_dir)


def _eta(args: argparse.Namespace) -> None:
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    phase = pass_recorder.object_dict(state.get("phase"))
    phase_id = pass_recorder.optional_arg(args, "phase_id").strip()
    if not phase_id:
        if phase is None or pass_recorder.string(phase.get("status")) != "active":
            _refuse_eta("An active phase is required to record an ETA")
        phase_id = pass_recorder.string(phase.get("id"))
    basis = pass_recorder.arg_string(args, "basis").strip()
    if not basis:
        _refuse_eta("--basis must not be blank")

    now = pass_recorder.now_epoch()
    eta_at = _local_eta_epoch(pass_recorder.arg_string(args, "time"), "--time")
    if eta_at < now:
        _refuse_eta("--time must not be in the past")

    earliest_text = pass_recorder.arg_string(args, "earliest")
    latest_text = pass_recorder.arg_string(args, "latest")
    if bool(earliest_text) != bool(latest_text):
        _refuse_eta("--earliest and --latest must be supplied together")

    event = pass_recorder.record_event(state, "eta_stated", now)
    event.update({"eta_at": eta_at, "basis": basis, "eta_phase_id": phase_id})
    if earliest_text and latest_text:
        earliest_at = _local_eta_epoch(earliest_text, "--earliest")
        latest_at = _local_eta_epoch(latest_text, "--latest")
        if earliest_at > eta_at:
            _refuse_eta("--earliest must not be after --time")
        if latest_at < eta_at:
            _refuse_eta("--latest must not be before --time")
        event.update(
            {
                "eta_earliest_at": earliest_at,
                "eta_latest_at": latest_at,
            }
        )

    pass_recorder.append_event(state, event)
    _refresh_phase_table(session_dir)
    local_eta = datetime.fromtimestamp(eta_at, user_zone())
    print(f"ETA recorded: {local_eta:%H:%M %Z}")


def _waits_by_id(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    waits: dict[str, dict[str, object]] = {}
    for event in events:
        if event.get("event_type") in ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded"):
            wait_id = pass_recorder.string(event.get("wait_id"))
            if wait_id:
                waits[wait_id] = event
    return waits


@dataclass(frozen=True)
class SessionHolder:
    session_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("holder session must not be blank")


@dataclass(frozen=True)
class UnitHolder:
    unit: str

    def __post_init__(self) -> None:
        if not self.unit.strip():
            raise ValueError("holder unit must not be blank")


@dataclass(frozen=True)
class HolderNotNamed:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("unnamed holder needs a reason")


@dataclass(frozen=True)
class MalformedHolder:
    raw_value: object
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("malformed holder needs a reason")

    def to_fields(self) -> dict[str, object]:
        return {"state": "MalformedHolder", "reason": self.reason, "raw_value": self.raw_value}


def _validate_holder_schema(fields: dict[str, object], allowed: set[str], state: str) -> None:
    unknown = fields.keys() - allowed
    if unknown:
        raise ValueError(f"{state} has unknown fields: {', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class NamedHolder:
    resource_kind: Literal["account", "slot", "memory"]
    resource: str
    holder: SessionHolder | UnitHolder

    def __post_init__(self) -> None:
        if self.resource_kind not in ("account", "slot", "memory") or not self.resource.strip():
            raise ValueError("holder needs a resource kind and a nonblank resource")

    @classmethod
    def from_fields(cls, fields: dict[str, object]) -> NamedHolder:
        _validate_holder_schema(fields, {"state", "resource_kind", "resource", "holder"}, "NamedHolder")
        kind = fields.get("resource_kind")
        resource = fields.get("resource")
        holder = pass_recorder.object_dict(fields.get("holder")) or {}
        if (fields.get("state") != "NamedHolder" or not isinstance(resource, str)
                or kind not in ("account", "slot", "memory")):
            raise ValueError("invalid named holder fields")
        if holder.get("state") == "SessionHolder":
            _validate_holder_schema(holder, {"state", "session_id"}, "SessionHolder")
            owner = SessionHolder(pass_recorder.string(holder.get("session_id")))
        elif holder.get("state") == "UnitHolder":
            _validate_holder_schema(holder, {"state", "unit"}, "UnitHolder")
            owner = UnitHolder(pass_recorder.string(holder.get("unit")))
        else:
            raise ValueError("holder must name a session or unit")
        # Narrow the wire value to the domain's three resource kinds.
        if kind == "account":
            return cls("account", resource, owner)
        if kind == "slot":
            return cls("slot", resource, owner)
        return cls("memory", resource, owner)


def _holder_from_fields(fields: dict[str, object]) -> NamedHolder | HolderNotNamed:
    if fields.get("state") == "HolderNotNamed":
        _validate_holder_schema(fields, {"state", "reason"}, "HolderNotNamed")
        return HolderNotNamed(pass_recorder.string(fields.get("reason")))
    return NamedHolder.from_fields(fields)


def _holder_fields(holder: NamedHolder | HolderNotNamed) -> dict[str, object]:
    if isinstance(holder, HolderNotNamed):
        return {"state": "HolderNotNamed", "reason": holder.reason}
    owner = ({"state": "SessionHolder", "session_id": holder.holder.session_id}
             if isinstance(holder.holder, SessionHolder) else
             {"state": "UnitHolder", "unit": holder.holder.unit})
    return {"state": "NamedHolder", "resource_kind": holder.resource_kind,
            "resource": holder.resource, "holder": owner}


def _refuse_holder(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _holder_from_args(args: argparse.Namespace, kind: str) -> NamedHolder | HolderNotNamed:
    flags = ("holder_resource_kind", "holder_resource", "holder_session", "holder_unit")
    supplied = {flag for flag in flags if hasattr(args, flag)}
    if not supplied:
        return HolderNotNamed("producer named no holder")
    if kind not in HOLDER_WAIT_KINDS:
        _refuse_holder(f"{kind} does not accept holder flags")
    if supplied not in ({"holder_resource_kind", "holder_resource", "holder_session"},
                         {"holder_resource_kind", "holder_resource", "holder_unit"}):
        _refuse_holder("holder flags require a resource kind, resource and exactly one session or unit")
    owner = ({"state": "SessionHolder", "session_id": pass_recorder.arg_string(args, "holder_session")}
             if "holder_session" in supplied else
             {"state": "UnitHolder", "unit": pass_recorder.arg_string(args, "holder_unit")})
    try:
        return NamedHolder.from_fields({"state": "NamedHolder",
            "resource_kind": pass_recorder.arg_string(args, "holder_resource_kind"),
            "resource": pass_recorder.arg_string(args, "holder_resource"), "holder": owner})
    except ValueError as error:
        _refuse_holder(str(error))


def _wait_holder(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    # A holder event copies its recorded wait; it reads only the history file.
    state = pass_recorder.read_state(pass_recorder.argument_session_dir(args), resolve_staff=False)
    wait_id = pass_recorder.arg_string(args, "wait_id")
    events = _run_events(state)
    wait = _waits_by_id(events).get(wait_id)
    if wait is None or wait.get("event_type") != "wait_started":
        _refuse_holder(f"Wait {wait_id} is not open in this run")
    kind = pass_recorder.string(wait.get("wait_kind"))
    if kind not in HOLDER_WAIT_KINDS:
        _refuse_holder(f"{kind} does not accept holders")
    holder = _holder_from_args(args, kind)
    now = pass_recorder.now_epoch()
    last_seen = max((pass_recorder.number(event.get("timestamp_epoch")) for event in events
                     if event.get("wait_id") == wait_id), default=pass_recorder.number(wait.get("wait_started_epoch")))
    if now < last_seen:
        _refuse_holder("Cannot record a holder before the wait or its last holder observation")
    event = dict(wait)
    event.update(event_id=str(uuid.uuid4()), event_type="wait_holder_seen",
                 timestamp=pass_recorder.iso_time(now), timestamp_epoch=now, holder=_holder_fields(holder))
    pass_recorder.append_event(state, event)


def _add_holder_arguments(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--holder-resource-kind", choices=("account", "slot", "memory"), default=argparse.SUPPRESS)
    _ = parser.add_argument("--holder-resource", default=argparse.SUPPRESS)
    owner = parser.add_mutually_exclusive_group()
    _ = owner.add_argument("--holder-session", default=argparse.SUPPRESS)
    _ = owner.add_argument("--holder-unit", default=argparse.SUPPRESS)


@dataclass(frozen=True)
class WaitPhaseResolved:
    phase: dict[str, object]


@dataclass(frozen=True)
class WaitPhaseNotRecorded:
    reason: str


@dataclass(frozen=True)
class ResolvedWorkerPass:
    phase: dict[str, object]
    pass_instance_id: str
    worker_name: str
    team_slot: str


@dataclass(frozen=True)
class UnresolvedWaitAttribution:
    reason: str
    candidates: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ResolvedCheckpointPhase:
    identity: dict[str, object]


@dataclass(frozen=True)
class ProductionHandoff:
    production: str
    checkpoint_commit: str


@dataclass(frozen=True)
class NamedSupplier:
    address: str


@dataclass(frozen=True)
class NoNamedSupplier:
    reason: str


@dataclass(frozen=True)
class ProductionUnitIdentity:
    production: str
    unit: str


@dataclass(frozen=True)
class ProductionNotRecorded:
    reason: str


def resolve_production_unit(state: Mapping[str, object], *, resolver: session_staff.RowResolver | None = None) -> ProductionUnitIdentity | ProductionNotRecorded:
    """Read the production and unit names from the current staff row."""
    try:
        resolver = resolver if resolver is not None else session_staff.RowResolver(staff.snapshot())
        record = resolver.record
        owner = unit_runs.run_owner(state, record)
        row = owner if isinstance(owner, staff.StaffRow) else None
        if row is None or row.kind != "unit":
            plan = pass_recorder.string(state.get("project_plan_doc")) or pass_recorder.string(state.get("plan_doc"))
            working_dir = pass_recorder.string(state.get("working_dir"))
            if plan and working_dir:
                try:
                    # Read the artifact only to report a file failure.
                    _ = (Path(working_dir).expanduser() / plan).read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    return ProductionNotRecorded(f"phase plan cannot be read: {error}")
            return ProductionNotRecorded("the run has no matching unit staff row")
        parent = record.parent_of(row)
        if parent is None:
            return ProductionNotRecorded(f"{row.name}: the staff parent is unavailable")
        return ProductionUnitIdentity(parent.name, row.name)
    except (OSError, ValueError) as error:
        return ProductionNotRecorded(str(error))


@dataclass(frozen=True)
class RecordedPhaseMetadata:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("recorded phase metadata must not be blank")


@dataclass(frozen=True)
class PhaseMetadataNotRecorded:
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("missing phase metadata needs a reason")


@dataclass(frozen=True)
class PhaseSourceIdentity:
    production: RecordedPhaseMetadata | PhaseMetadataNotRecorded
    unit: RecordedPhaseMetadata | PhaseMetadataNotRecorded
    repository: RecordedPhaseMetadata | PhaseMetadataNotRecorded


def _phase_metadata_fields(value: RecordedPhaseMetadata | PhaseMetadataNotRecorded) -> dict[str, str]:
    if isinstance(value, RecordedPhaseMetadata):
        return {"state": "Recorded", "value": value.value}
    return {"state": "NotRecorded", "reason": value.reason}


def _recorded_phase_metadata(value: object, reason: str) -> RecordedPhaseMetadata | PhaseMetadataNotRecorded:
    # Accept earlier string metadata as well as the tagged event fields.
    if isinstance(value, str) and value.strip():
        return RecordedPhaseMetadata(value)
    fields = pass_recorder.object_dict(value) or {}
    if fields.get("state") == "Recorded" and pass_recorder.string(fields.get("value")).strip():
        return RecordedPhaseMetadata(pass_recorder.string(fields.get("value")))
    if fields.get("state") == "NotRecorded" and pass_recorder.string(fields.get("reason")).strip():
        return PhaseMetadataNotRecorded(pass_recorder.string(fields.get("reason")))
    return PhaseMetadataNotRecorded(reason)


def _phase_repository(working_dir: str) -> RecordedPhaseMetadata | PhaseMetadataNotRecorded:
    if not working_dir:
        return PhaseMetadataNotRecorded("phase has no working directory")
    worktree = Path(working_dir).expanduser()
    if not worktree.is_dir():
        return PhaseMetadataNotRecorded("phase working directory is unavailable")
    try:
        repository = git_files.locate(worktree).common_dir
    except git_files.GitFilesError:
        return PhaseMetadataNotRecorded("repository common directory is unavailable")
    return RecordedPhaseMetadata(repository.parent.name if repository.name == ".git" else repository.name)


def _phase_plan_names(state: Mapping[str, object]) -> tuple[
    RecordedPhaseMetadata | PhaseMetadataNotRecorded, RecordedPhaseMetadata | PhaseMetadataNotRecorded
]:
    identity = resolve_production_unit(state)
    if isinstance(identity, ProductionNotRecorded):
        missing = PhaseMetadataNotRecorded(identity.reason)
        return missing, missing
    return RecordedPhaseMetadata(identity.production), RecordedPhaseMetadata(identity.unit)


def _phase_source_identity(state: Mapping[str, object]) -> PhaseSourceIdentity:
    production, unit = _phase_plan_names(state)
    return PhaseSourceIdentity(production, unit, _phase_repository(pass_recorder.string(state.get("working_dir"))))


def _phase_identity(state: dict[str, object], phase: dict[str, object]) -> dict[str, object]:
    return {"run_id": pass_recorder.string(state.get("run_id")), "phase_id": pass_recorder.string(phase.get("id")),
            "phase_instance_id": pass_recorder.string(phase.get("instance_id"))}


def resolve_worker_pass(state: dict[str, object], thread_id: str) -> ResolvedWorkerPass | UnresolvedWaitAttribution:
    """Select the current staff worker and its recorded dispatch reference."""
    phases = [*pass_recorder.parked_phases(state), pass_recorder.object_dict(state.get("phase")) or {}]
    candidates: list[dict[str, object]] = [{"run_id": pass_recorder.string(state.get("run_id")),
                   "phase_instance_id": pass_recorder.pass_phase_instance(passed),
                   "phase_id": pass_recorder.string((pass_recorder.object_dict(passed.get("phase")) or {}).get("id")),
                   "pass_instance_id": pass_recorder.string(passed.get("instance_id")),
                   "worker_id": pass_recorder.string(passed.get("worker_id")),
                   "team_slot": pass_recorder.string(passed.get("team_slot"))}
                  for passed in pass_recorder.open_passes(state).values()]
    if not candidates:
        candidates = [_phase_identity(state, phase) for phase in phases if phase.get("status") == "active"]
    try:
        membership = staff.local_snapshot()
        worker = next((row for row in session_staff.run_workers(state, membership)
                       if thread_id and row.current_session == thread_id), None)
    except (OSError, ValueError) as error:
        return UnresolvedWaitAttribution(f"worker staff unavailable: {error}", tuple(candidates))
    if worker is None:
        return UnresolvedWaitAttribution("worker thread has no matching staff membership", tuple(candidates))
    matching = [candidate for candidate in candidates if candidate.get("worker_id") == worker.id]
    if len(matching) != 1:
        return UnresolvedWaitAttribution(f"worker {worker.name} has {len(matching)} active pass matches",
                                         tuple(matching or candidates))
    candidate = matching[0]
    no_phase: dict[str, object] = {}
    phase = next((phase for phase in phases if phase.get("instance_id") == candidate["phase_instance_id"]
                  and phase.get("status") == "active"), no_phase)
    if not phase or not candidate["pass_instance_id"]:
        return UnresolvedWaitAttribution("worker pass has no active phase or pass id", (candidate,))
    return ResolvedWorkerPass(phase, pass_recorder.string(candidate["pass_instance_id"]), worker.name,
                              pass_recorder.string(candidate["team_slot"]))


def _wait_phase(state: dict[str, object], args: argparse.Namespace) -> (
    WaitPhaseResolved | WaitPhaseNotRecorded | ResolvedWorkerPass | UnresolvedWaitAttribution
):
    """Resolve the caller's pass now, or an explicitly named historical instance."""
    slot = pass_recorder.optional_arg(args, "slot")
    instance_id = pass_recorder.optional_arg(args, "phase_instance_id")
    phase_id = pass_recorder.optional_arg(args, "phase_id")
    if hasattr(args, "worker_thread_id"):
        return resolve_worker_pass(state, pass_recorder.optional_arg(args, "worker_thread_id"))
    if hasattr(args, "director") and cast(bool, args.director):
        phases = [phase for phase in [*pass_recorder.parked_phases(state), pass_recorder.object_dict(state.get("phase")) or {}]
                  if phase.get("status") == "active"]
        if len(phases) == 1:
            return WaitPhaseResolved(phases[0])
        return UnresolvedWaitAttribution(f"director has {len(phases)} active phase candidates",
                                         tuple(_phase_identity(state, phase) for phase in phases))
    if hasattr(args, "slot"):
        current_pass = pass_recorder.open_passes(state).get(slot)
        if current_pass is None:
            return WaitPhaseNotRecorded(f"slot {slot} has no open pass")
        instance_id = pass_recorder.pass_phase_instance(current_pass)
        if not instance_id:
            return WaitPhaseNotRecorded(f"slot {slot} open pass has no phase instance")
        # An open pass can only belong to an open phase. Never substitute the
        # current phase when a reused server carries an older environment.
        candidates = [*pass_recorder.parked_phases(state), pass_recorder.object_dict(state.get("phase")) or {}]
        for phase in candidates:
            if phase.get("instance_id") == instance_id and phase.get("status") == "active":
                return WaitPhaseResolved(phase)
        return WaitPhaseNotRecorded(f"slot {slot} open pass names no open phase")
    if hasattr(args, "phase_instance_id"):
        if not instance_id:
            return WaitPhaseNotRecorded("phase instance selector is blank")
        for event in reversed(_run_events(state)):
            if event.get("event_type") == "phase_started" and event.get("phase_instance_id") == instance_id:
                return WaitPhaseResolved({"instance_id": instance_id, "id": event.get("phase_id"),
                                          "title": event.get("phase_title"),
                                          "started_at": event.get("timestamp_epoch")})
        return WaitPhaseNotRecorded(f"phase instance {instance_id} has no start record in this run")
    if pass_recorder.optional_arg(args, "missing_reason") and not hasattr(args, "phase_id"):
        return WaitPhaseNotRecorded("missing wait has no phase selector")
    phase = pass_recorder.open_phase_named(state, phase_id) if hasattr(args, "phase_id") else pass_recorder.object_dict(state.get("phase"))
    if phase is not None and phase.get("status") == "active" and unit_runs.run_is_open(pass_recorder.argument_session_dir(args)):
        return WaitPhaseResolved(phase)
    return WaitPhaseNotRecorded(f"phase {phase_id or '(current)'} is not open")


def _start_wait(args: argparse.Namespace) -> None:
    if cast(bool, args.probe) or os.environ.get(PROBE_ENV) == "1":
        return
    # A wait event names its phase, so it carries no pass and needs no worker backend.
    state = pass_recorder.read_state(pass_recorder.argument_session_dir(args), pass_agents=False)
    result = _record_wait(state, args)
    if isinstance(result, StartedWait):
        print(result.wait_id)


@dataclass(frozen=True)
class StartedWait:
    wait_id: str


@dataclass(frozen=True)
class WaitNotRecorded:
    wait_id: str


@dataclass(frozen=True)
class UnitWaitTarget:
    """The work and actor named by a unit wait."""

    target: str
    by: str


def _record_wait(state: dict[str, object], args: argparse.Namespace) -> StartedWait | WaitNotRecorded:
    target = pass_recorder.optional_arg(args, "target")
    if hasattr(args, "target"):
        for option in ("target", "by", "reason"):
            if not pass_recorder.optional_arg(args, option).strip():
                raise SystemExit(f"start-wait --target requires --{option}")
    holder = _holder_from_args(args, pass_recorder.arg_string(args, "kind"))
    resolution = UnitWaitTarget(target, pass_recorder.optional_arg(args, "by")) if target else _wait_phase(state, args)
    owner = pass_recorder.arg_string(args, "owner_session") or pass_recorder.string((pass_recorder.object_dict(state.get("main_agent")) or {}).get("session_id"))
    missing_reason = "" if isinstance(resolution, UnitWaitTarget) else pass_recorder.optional_arg(args, "missing_reason")
    if isinstance(resolution, (WaitPhaseNotRecorded, UnresolvedWaitAttribution)):
        missing_reason = "; ".join(part for part in (missing_reason, resolution.reason) if part)
    if not owner and not isinstance(resolution, UnitWaitTarget):
        missing_reason = "; ".join(part for part in (missing_reason, "wait owner session not recorded") if part)
    now = pass_recorder.now_epoch()
    # An empty explicit phase prevents _event from borrowing a reporting pass
    # or the current phase when attribution failed.
    event = pass_recorder.record_event(state, "wait_not_recorded" if missing_reason else "wait_started", now,
                   phase_record=resolution.phase if isinstance(resolution, (WaitPhaseResolved, ResolvedWorkerPass)) else {})
    wait_id = str(uuid.uuid4())
    event.update({"wait_id": wait_id, "wait_kind": pass_recorder.arg_string(args, "kind"),
                  "wait_reason": pass_recorder.arg_string(args, "reason"),
                  "unit": pass_recorder.arg_string(args, "unit") or pass_recorder.string(state.get("worktree")),
                  "owner_session": owner, "wait_started_at": pass_recorder.iso_time(now),
                  "wait_started_epoch": now, "wait_end": {"state": "Open"},
                  "holder": _holder_fields(holder)})
    if declaration := pass_recorder.optional_arg(args, "declaration_id"):
        event["declaration_id"] = declaration
    if hasattr(args, "expected_supplier"):
        event["expected_supplier"] = cast(object, args.expected_supplier)
    if isinstance(resolution, UnitWaitTarget):
        event.update(wait_scope="unit", target_work=resolution.target, wait_by=resolution.by,
                     wait_attribution={"state": "UnitTarget"})
    elif isinstance(resolution, ResolvedWorkerPass):
        event.update(pass_instance_id=resolution.pass_instance_id, worker_name=resolution.worker_name,
                     team_slot=resolution.team_slot, wait_attribution={"state": "WorkerPass"})
    elif isinstance(resolution, UnresolvedWaitAttribution):
        event["wait_attribution"] = {"state": "UnresolvedAttribution", "reason": resolution.reason,
                                     "candidates": list(resolution.candidates)}
    else:
        event["wait_attribution"] = {"state": "PhaseInstance" if isinstance(resolution, WaitPhaseResolved)
                                     else "UnresolvedAttribution", "candidates": [],
                                     "reason": resolution.reason if isinstance(resolution, WaitPhaseNotRecorded) else "explicit phase attribution"}
    supplier = pass_recorder.object_dict(event.get("expected_supplier")) or {}
    if (not isinstance(resolution, UnitWaitTarget) and event["wait_kind"] == "WaitingForSupplier"
            and supplier.get("state") == "NoNamedSupplier"):
        event["wait_attribution"] = {"state": "UnresolvedSupplier", "reason": supplier.get("reason"),
                                     "candidates": [{key: event[key] for key in
                                                     ("run_id", "phase_id", "phase_instance_id") if key in event}]}
    if missing_reason:
        event.update(wait_record_state={"state": "NotRecorded", "reason": missing_reason},
                     wait_end={"state": "NotRecorded"},
                     requested_slot=pass_recorder.optional_arg(args, "slot"),
                     requested_phase_id=pass_recorder.optional_arg(args, "phase_id"),
                     requested_phase_instance_id=pass_recorder.optional_arg(args, "phase_instance_id"))
    pass_recorder.append_event(state, event)
    return WaitNotRecorded(wait_id) if missing_reason else StartedWait(wait_id)


def _end_wait(state: dict[str, object], wait: dict[str, object], now: float, *, unclosed: bool) -> None:
    event = dict(wait)
    event.update({"event_id": str(uuid.uuid4()), "event_type": "wait_unclosed" if unclosed else "wait_finished",
                  "timestamp": pass_recorder.iso_time(now), "timestamp_epoch": now,
                  "wait_end": {"state": "Unclosed"} if unclosed else
                  {"state": "Finished", "at": pass_recorder.iso_time(now), "epoch": now}})
    pass_recorder.append_event(state, event)


def _finish_wait(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    # A finish event copies its recorded wait; it reads only the history file.
    state = pass_recorder.read_state(pass_recorder.argument_session_dir(args), resolve_staff=False)
    wait_id = pass_recorder.arg_string(args, "wait_id")
    wait = _waits_by_id(_run_events(state)).get(wait_id)
    if wait is None:
        raise SystemExit(f"Wait {wait_id} was not recorded in this run")
    if wait.get("event_type") != "wait_started":
        return
    now = pass_recorder.now_epoch()
    if now < pass_recorder.number(wait.get("wait_started_epoch")):
        raise SystemExit("Cannot finish a wait before its start")
    _end_wait(state, wait, now, unclosed=False)


def _checkpoint(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    state = pass_recorder.read_state(pass_recorder.argument_session_dir(args))
    commit = pass_recorder.arg_string(args, "commit")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise SystemExit("checkpoint requires a full commit hash")
    phase = pass_recorder.open_phase_named(state, pass_recorder.arg_string(args, "phase"))
    if phase is None:
        raise SystemExit("checkpoint requires the named open phase")
    events = _run_events(state)
    for event in events:
        if (event.get("event_type") == "checkpoint" and event.get("checkpoint_commit") == commit
                and event.get("phase_instance_id") == phase.get("instance_id")):
            return
    event = pass_recorder.record_event(state, "checkpoint", pass_recorder.now_epoch(), phase_record=phase)
    event.update(checkpoint_commit=commit, session_dir=str(pass_recorder.argument_session_dir(args)))
    pass_recorder.append_event(state, event)


def resolve_checkpoint(commit: str, working_dir: Path, phase_id: str) -> (
    ResolvedCheckpointPhase | UnresolvedWaitAttribution
):
    """Match a full checkpoint, including finished instances, across durable runs."""
    events, ignored = load_events()
    phases = [event for event in events if event.get("event_type") == "phase_started"
              and pass_recorder.string(event.get("working_dir"))
              and Path(pass_recorder.string(event.get("working_dir"))).resolve() == working_dir.resolve()
              and event.get("phase_id") == phase_id]
    matches = [event for event in events if event.get("event_type") == "checkpoint"
               and event.get("checkpoint_commit") == commit]
    valid = [event for event in matches if any(
        (event.get("run_id"), event.get("phase_instance_id"), event.get("working_dir")) ==
        (phase.get("run_id"), phase.get("phase_instance_id"), phase.get("working_dir")) for phase in phases)]
    if len(matches) == len(valid) == 1 and not ignored:
        return ResolvedCheckpointPhase(valid[0])
    candidates = list({(pass_recorder.string(event.get("run_id")), pass_recorder.string(event.get("phase_instance_id"))): event
                       for event in [*phases, *matches]}.values())
    identities = tuple({key: event[key] for key in
                        ("run_id", "phase_id", "phase_instance_id", "session_dir", "working_dir", "checkpoint_commit")
                        if key in event} for event in candidates)
    reason = (f"checkpoint {commit} has {len(matches)} records and {len(valid)} valid phase matches"
              + (f"; {ignored} unreadable history rows" if ignored else ""))
    return UnresolvedWaitAttribution(reason, identities)


def _external_wait_file() -> Path:
    return plan_history.history_root() / "wait_attribution.jsonl"


def _external_wait_events() -> list[dict[str, object]]:
    # This reader creates no file or parent directory.
    return _run_events({"history_file": str(_external_wait_file())})


@contextmanager
def _external_wait_lock() -> Generator[None, None, None]:
    path = _external_wait_file().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _start_external_wait(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    with _external_wait_lock():
        _record_external_wait(args)


def _record_external_wait(args: argparse.Namespace) -> None:
    commit = pass_recorder.arg_string(args, "commit")
    attribution = (ProductionHandoff(pass_recorder.arg_string(args, "production"), commit)
                   if pass_recorder.arg_string(args, "command") == "start-handoff-wait" else
                   resolve_checkpoint(commit, Path(pass_recorder.arg_string(args, "working_dir")), pass_recorder.arg_string(args, "phase_id")))
    now = pass_recorder.now_epoch()
    owner = (pass_recorder.arg_string(args, "owner_session") or os.environ.get("CLAUDE_CODE_SESSION_ID")
             or os.environ.get("CODEX_THREAD_ID", ""))
    if not owner and isinstance(attribution, ResolvedCheckpointPhase):
        member = unit_runs.run_owner(attribution.identity, staff.initial_snapshot())
        owner = member.current_session if isinstance(member, staff.StaffRow) else ""
    event: dict[str, object] = {"schema_version": pass_recorder.SCHEMA_VERSION, "event_id": str(uuid.uuid4()),
        "event_type": "wait_started", "wait_id": str(uuid.uuid4()), "timestamp_epoch": now,
        "timestamp": pass_recorder.iso_time(now), "wait_started_at": pass_recorder.iso_time(now), "wait_started_epoch": now,
        "wait_kind": "WaitingForMergeGate", "wait_reason": pass_recorder.arg_string(args, "reason"),
        "owner_session": owner, "unit": pass_recorder.arg_string(args, "unit"),
        "checkpoint_commit": commit, "wait_end": {"state": "Open"}}
    if isinstance(attribution, ResolvedCheckpointPhase):
        event.update({key: attribution.identity[key] for key in
                      ("run_id", "phase_id", "phase_instance_id", "working_dir", "session_dir")})
        event["wait_attribution"] = {"state": "CheckpointPhase", "reason": "one checkpoint phase match",
                                     "candidates": [attribution.identity]}
    elif isinstance(attribution, ProductionHandoff):
        event["production"] = attribution.production
        event["wait_attribution"] = {"state": "ProductionHandoff", "reason": "child production checkpoint has no owning phase",
                                     "candidates": []}
    else:
        event.update(event_type="wait_not_recorded", wait_end={"state": "NotRecorded"},
                     wait_record_state={"state": "NotRecorded", "reason": attribution.reason},
                     wait_attribution={"state": "UnresolvedAttribution", "reason": attribution.reason,
                                       "candidates": list(attribution.candidates)})
    pass_recorder.append_event({"history_file": str(_external_wait_file())}, event)
    print(event["wait_id"])


def _finish_external_wait(args: argparse.Namespace) -> None:
    if os.environ.get(PROBE_ENV) == "1":
        return
    path = _external_wait_file()
    with _external_wait_lock():
        wait = _waits_by_id(_external_wait_events()).get(pass_recorder.arg_string(args, "wait_id"))
        if wait is None:
            raise SystemExit("External wait was not recorded")
        if wait.get("event_type") == "wait_started":
            now = pass_recorder.now_epoch()
            if now < pass_recorder.number(wait.get("wait_started_epoch")):
                raise SystemExit("Cannot finish a wait before its start")
            _end_wait({"history_file": str(path)}, wait, now, unclosed=False)


def _showrunner_supplier(state: dict[str, object]) -> NamedSupplier | NoNamedSupplier:
    try:
        resolver = session_staff.RowResolver(staff.snapshot())
        identity = resolve_production_unit(state, resolver=resolver)
        if isinstance(identity, ProductionNotRecorded):
            return NoNamedSupplier(identity.reason)
        address = _supplier_address("showrunner:" + identity.production, resolver=resolver)
    except (OSError, ValueError) as error:
        return NoNamedSupplier(f"showrunner address unavailable: {error}")
    if isinstance(address, NoNamedSupplier):
        return address
    return NamedSupplier("showrunner:" + identity.production)


def _supplier_fields(supplier: NamedSupplier | NoNamedSupplier) -> dict[str, object]:
    if isinstance(supplier, NamedSupplier):
        return {"state": "NamedSupplier", "address": supplier.address}
    return {"state": "NoNamedSupplier", "reason": supplier.reason}


def _turn_supplier(state: dict[str, object], reason: str) -> NamedSupplier | NoNamedSupplier:
    """The supplier a blocked turn names, else the run's showrunner, which relays pauses and their end."""
    match = re.search(r"blocked: waiting on ((?:claude-|codex-|worker:|session:|codex:|uds:)[^\s,;]+)", reason, re.IGNORECASE)
    if match:
        return NamedSupplier(match[1])
    if re.search(r"\bpeer\b", reason, re.IGNORECASE):
        return NoNamedSupplier("wait names no supplier address")
    return _showrunner_supplier(state)


def _supplier_address(identity: str, *, resolver: session_staff.RowResolver | None = None) -> NamedSupplier | NoNamedSupplier:
    """Resolve the supplier alias at arrival, including a restarted showrunner."""
    if identity.startswith("showrunner:"):
        from scripts.message import sessions

        resolver = resolver if resolver is not None else session_staff.RowResolver(staff.snapshot())
        production = identity.removeprefix("showrunner:")
        records = {item["sessionId"]: item for item in sessions.live_sessions()}
        for row in staff.active(resolver.record, "showrunner"):
            if row.name == production and row.current_session in records:
                return NamedSupplier("uds:" + records[row.current_session]["messagingSocketPath"])
        return NoNamedSupplier("showrunner has no current address")
    if identity.startswith(("uds:/", "codex:")):
        return NamedSupplier(identity)
    if identity.startswith("worker:"):
        run_id, separator, reference = identity.partition(":")[2].partition("/")
        if not separator or run_id in ("", ".", "..") or "/" in reference:
            return NoNamedSupplier("worker alias has no run and worker identity")
        membership = resolver.record if resolver is not None else staff.local_snapshot()
        worker = membership.by_id(staff.StaffId(reference))
        if worker is None or worker.kind != "worker" or not isinstance(worker.parent, staff.ReportsTo):
            return NoNamedSupplier("worker alias has no current staff worker")
        for root in unit_runs.run_roots(os.environ.get("UNIT_RUN_ROOT")):
            try:
                fields = worker_lifecycle.read_json_object(root / run_id / unit_runs.STATE_FILE)
                owner = unit_runs.run_owner(fields, membership)
                if isinstance(owner, staff.StaffRow) and worker.parent == staff.ReportsTo(owner.id):
                    return NamedSupplier("codex:" + worker.current_session)
            except (OSError, ValueError):
                continue
        return NoNamedSupplier("worker alias has no matching staff parent")
    from scripts.message import sessions

    records = sessions.live_sessions()
    try:
        membership = resolver.record if resolver is not None else staff.local_snapshot()
    except staff.StaffUnreadable as error:
        return NoNamedSupplier(f"supplier staff is unavailable: {error}")
    reference = identity.removeprefix("session:")
    owner = membership.by_member_reference(reference)
    owners = [owner] if owner is not None else [row for row in membership.rows if row.name == identity]
    if len(owners) > 1:
        return NoNamedSupplier(f"supplier alias has {len(owners)} live addresses")
    if owners:
        reference = str(owners[0].current_session)
    else:
        try:
            _ = staff.native_id(reference)
        except staff.StaffRefused:
            return NoNamedSupplier("supplier alias has 0 live addresses")
    matches = [record for record in records if record["sessionId"] == reference]
    if len(matches) != 1:
        return NoNamedSupplier(f"supplier alias has {len(matches)} live addresses")
    return NamedSupplier("uds:" + matches[0]["messagingSocketPath"])


class SupplierMessageRoute(Enum):
    DIRECT_REPLY = "direct supplier reply"
    POSTMASTER_REPLY = "verified postmaster reply"


class SupplierMessageRejection(Enum):
    INVALID_ORIGIN = "invalid message origin"
    SUPPLIER_UNAVAILABLE = "supplier address unavailable"
    OUTER_SENDER_NOT_POSTMASTER = "outer sender is not the postmaster"
    INVALID_FORWARD = "invalid postmaster forward"
    NO_RECORD = "no valid reply record at the forward time"
    SEVERAL_RECORDS = "several matching reply records"
    SOCKET_MISMATCH = "reply socket is not the supplier current address"
    BODY_MISMATCH = "reply body does not match the forward"
    NOT_A_TARGET = "receiving session is not an intended target"


@dataclass(frozen=True)
class VerifiedSupplierMessage:
    body: str
    route: SupplierMessageRoute = SupplierMessageRoute.DIRECT_REPLY


@dataclass(frozen=True)
class UnverifiedSupplierMessage:
    rejection: SupplierMessageRejection

    @property
    def reason(self) -> str:
        return self.rejection.value


@dataclass(frozen=True)
class PostmasterReplyRecord:
    at_ms: int
    socket: str
    name: str
    session_id: str
    senders: tuple[str, ...]
    body: str
    targets: tuple[str, ...]

    @classmethod
    def from_fields(cls, fields: dict[str, object]) -> PostmasterReplyRecord:
        """Validate the postmaster schema at the JSON boundary."""
        def session_names(value: object) -> tuple[str, ...]:
            if not isinstance(value, list):
                raise ValueError("reply session names must be a list")
            names: list[str] = []
            for item in cast(list[object], value):
                if not isinstance(item, str) or not item:
                    raise ValueError("reply session names must be nonempty strings")
                names.append(item)
            return tuple(names)

        at_ms = fields.get("at_ms")
        socket = fields.get("socket")
        name = fields.get("name")
        session_id = fields.get("session_id")
        body = fields.get("body")
        if (type(at_ms) is not int or not 0 <= at_ms <= 9007199254740991
                or not isinstance(socket, str) or not socket.startswith("uds:/")
                or not isinstance(name, str) or not name
                or not isinstance(session_id, str) or not session_id
                or not isinstance(body, str)):
            raise ValueError("invalid postmaster reply fields")
        senders = session_names(fields.get("senders"))
        if not senders:
            raise ValueError("reply must name a sender")
        return cls(at_ms, socket, name, session_id, senders, body,
                   session_names(fields.get("targets")))


def _read_postmaster_reply_record(path: Path) -> PostmasterReplyRecord:
    fields = pass_recorder.json_object(path.read_text(encoding="utf-8"))
    if fields is None:
        raise ValueError("reply record is not a JSON object")
    return PostmasterReplyRecord.from_fields(fields)


def _supplier_message(identity: str, prompt: str, owner_session: str) -> VerifiedSupplierMessage | UnverifiedSupplierMessage:
    """Verify direct origin, or the postmaster's pre-forward origin and target record."""
    wrapper = re.fullmatch(r'\s*<cross-session-message\b([^>]*)>(.*)</cross-session-message>\s*', prompt, re.DOTALL)
    if wrapper is None:
        return UnverifiedSupplierMessage(SupplierMessageRejection.INVALID_ORIGIN)
    pairs = cast(list[tuple[str, str]], re.findall(r'([\w-]+)="([^"]*)"', wrapper[1]))
    attributes = dict(pairs)
    if len(pairs) != len(attributes):
        return UnverifiedSupplierMessage(SupplierMessageRejection.INVALID_ORIGIN)
    address = _supplier_address(identity)
    if isinstance(address, NoNamedSupplier):
        return UnverifiedSupplierMessage(SupplierMessageRejection.SUPPLIER_UNAVAILABLE)
    body = wrapper[2]
    origin = attributes.get("from", "")
    if origin == address.address:
        return VerifiedSupplierMessage(body.strip())

    # Remove only the transport's enclosing line breaks. The supplier body,
    # including its leading/trailing whitespace, must match the record exactly.
    if body.startswith("\r\n"):
        body = body.removeprefix("\r\n").removesuffix("\r\n")
    elif body.startswith("\n"):
        body = body.removeprefix("\n").removesuffix("\n")
    forward = re.fullmatch(
        r'Message from [^\r\n]+ \(their answer to [^\r\n]+, passed on by postmaster\) '
        + r'\[([0-9]+)-[0-9]+\]:\n\n(.*)', body, re.DOTALL)
    if forward is None:
        return UnverifiedSupplierMessage(SupplierMessageRejection.INVALID_FORWARD)

    # Resolve the outer sender before reading reply records. A name in the
    # message, or a reply record alone, cannot establish the forward's origin.
    postmaster = _supplier_address("postmaster")
    outer = _supplier_address(origin) if origin.startswith("session:") else NamedSupplier(origin)
    if (not isinstance(postmaster, NamedSupplier) or not isinstance(outer, NamedSupplier)
            or outer.address != postmaster.address):
        return UnverifiedSupplierMessage(SupplierMessageRejection.OUTER_SENDER_NOT_POSTMASTER)

    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    records: list[PostmasterReplyRecord] = []
    if root.is_absolute():
        # replyCount names files; forwardCount names deliveries. They need not agree.
        for path in (root / "message/postmaster/replies").glob(f"{forward[1]}-*.json"):
            if path.is_symlink() or re.fullmatch(r'[0-9]+-[0-9]+\.json', path.name) is None:
                continue
            try:
                record = _read_postmaster_reply_record(path)
            except (OSError, ValueError):
                continue
            if str(record.at_ms) == forward[1]:
                records.append(record)
    if not records:
        return UnverifiedSupplierMessage(SupplierMessageRejection.NO_RECORD)
    records = [record for record in records if record.socket == address.address]
    if not records:
        return UnverifiedSupplierMessage(SupplierMessageRejection.SOCKET_MISMATCH)
    records = [record for record in records if record.body == forward[2]]
    if not records:
        return UnverifiedSupplierMessage(SupplierMessageRejection.BODY_MISMATCH)
    records = [record for record in records if owner_session in record.targets]
    if not records:
        return UnverifiedSupplierMessage(SupplierMessageRejection.NOT_A_TARGET)
    if len(records) != 1:
        return UnverifiedSupplierMessage(SupplierMessageRejection.SEVERAL_RECORDS)
    # The write-once reply record's targets establish the intended recipients.
    return VerifiedSupplierMessage(forward[2], SupplierMessageRoute.POSTMASTER_REPLY)


def _turn_waits_for_marker(marker: dict[str, object], waits: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    """Find the declared wait even when its marker lacks the event's wait id."""
    declaration = pass_recorder.string(marker.get("declaration_id"))
    if declaration:
        return [wait for wait in waits.values() if wait.get("declaration_id") == declaration]
    previous = waits.get(pass_recorder.string(marker.get("wait_id")))
    return [previous] if previous is not None else []


def record_turn_wait(session_dir: Path, owner_session: str, final_message: str) -> bool:
    """Start a director's declared user/supplier wait once, before the stop."""
    if os.environ.get(PROBE_ENV) == "1":
        return False
    lines = final_message.strip().splitlines()
    if not lines:
        return False
    reason = lines[-1].strip().removeprefix("— ")
    line = reason.lower()
    if line.startswith(("decision:", "gate:")):
        kind = "WaitingForUser"
    elif line.startswith("blocked: waiting on ") or (line.startswith("blocked:") and re.search(r"\bpause[ds]?\b", line)):
        # A block names its supplier or a pause; any other block is the director's own work and wakes it.
        kind = "WaitingForSupplier"
    else:
        return False
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = pass_recorder.read_state(session_dir)
        supplier = _turn_supplier(state, reason) if kind == "WaitingForSupplier" else _showrunner_supplier(state)
        if kind == "WaitingForSupplier" and isinstance(supplier, NoNamedSupplier) and re.search(r"\bpeer\b", line) is None:
            # A block that names no supplier, in a run with no showrunner, waits on the user.
            kind = "WaitingForUser"
        supplier_fields = _supplier_fields(supplier)
        marker_path = session_dir / "turn_wait.json"
        try:
            marker = pass_recorder.json_object(marker_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            marker = {}
        phase = pass_recorder.object_dict(state.get("phase")) or {}
        identity: dict[str, object] = {"owner_session": owner_session, "wait_kind": kind,
                                       "expected_supplier": supplier_fields,
                                       "phase_instance_id": pass_recorder.string(phase.get("instance_id"))}
        waits = _waits_by_id(_run_events(state))
        same_declaration = all(marker.get(key) == value for key, value in identity.items())
        declaration = pass_recorder.string(marker.get("declaration_id")) if same_declaration else ""
        matching = _turn_waits_for_marker(marker, waits) if declaration else []
        recovered = next((wait for wait in matching if wait.get("event_type") == "wait_started"), None)
        previous = waits.get(pass_recorder.string(marker.get("wait_id")))
        if recovered is None and not marker.get("declaration_id") and previous is not None:
            if previous.get("event_type") == "wait_started" and all(previous.get(key) == value for key, value in identity.items()):
                return True
        if recovered is not None:
            write_text_atomic(marker_path, json.dumps({**marker, "wait_id": recovered["wait_id"]}) + "\n")
            return True
        if previous is not None and previous.get("event_type") == "wait_started":
            _end_wait(state, previous, pass_recorder.now_epoch(), unclosed=True)
        if not declaration or matching:
            declaration = str(uuid.uuid4())
        marker = {**identity, "declaration_id": declaration}
        write_text_atomic(marker_path, json.dumps(marker) + "\n")
        args = argparse.Namespace(session_dir=str(session_dir), missing_reason="", kind=kind, reason=reason,
                                  owner_session=owner_session, unit="", expected_supplier=supplier_fields,
                                  declaration_id=declaration)
        result = _record_wait(state, args)
        write_text_atomic(marker_path, json.dumps({**marker, "wait_id": result.wait_id}) + "\n")
    return True


def finish_turn_wait(session_dir: Path, owner_session: str, *, source: str, prompt: str) -> None:
    """Finish only for typed input or a message from the recorded supplier."""
    if os.environ.get(PROBE_ENV) == "1" or not (session_dir / "turn_wait.json").exists():
        return
    with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        marker_path = session_dir / "turn_wait.json"
        try:
            marker = pass_recorder.json_object(marker_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return
        if marker.get("owner_session") != owner_session:
            return
        state = pass_recorder.read_state(session_dir)
        waits = [wait for wait in _turn_waits_for_marker(marker, _waits_by_id(_run_events(state)))
                 if wait.get("event_type") == "wait_started"]
        if not waits:
            return
        wait = waits[0]
        supplier = pass_recorder.object_dict(wait.get("expected_supplier")) or {}
        message = (_supplier_message(pass_recorder.string(supplier.get("address")), prompt, owner_session)
                   if source == "PEER" and supplier.get("state") == "NamedSupplier"
                   else UnverifiedSupplierMessage(SupplierMessageRejection.INVALID_ORIGIN))
        from_supplier = isinstance(message, VerifiedSupplierMessage)
        user_wait = wait.get("wait_kind") == "WaitingForUser"
        body = message.body if isinstance(message, VerifiedSupplierMessage) else ""
        body = re.sub(r'^Message from [^\n]+ via postmaster: [^\n]* \[[a-fA-F0-9]{8}\]\r?\n\r?\n', "", body, count=1)
        relay = from_supplier and body.lstrip().startswith("From the user (via the showrunner):")
        if not ((user_wait and (source == "TYPED" or relay)) or (not user_wait and from_supplier)):
            return
        _finish_wait(argparse.Namespace(session_dir=str(session_dir), wait_id=pass_recorder.string(wait.get("wait_id"))))
        marker_path.unlink()


def write_merge_receipt(*, project: str, phase_id: str, merge_commit: str,
                        source_checkpoint: str, target_branch: str, merged_at: float,
                        issue: str = "") -> dict[str, object]:
    """Record a pushed merge once; callers must not call this for a rolled-back merge."""
    if not all((project, merge_commit, source_checkpoint, target_branch)) or not (phase_id or issue):
        raise ValueError("A merge receipt needs its project, phase or issue, both commits, and target branch")
    path = plan_history.history_root() / "merge_receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.seek(0)
        for line in handle:
            receipt = pass_recorder.json_object(line)
            if receipt is not None and (receipt.get("project"), receipt.get("target_branch"), receipt.get("merge_commit")) == (project, target_branch, merge_commit):
                return receipt
        record: dict[str, object] = {"event_type": "merge_receipt", "schema_version": pass_recorder.SCHEMA_VERSION,
                   "project": project, "issue": issue, "phase_id": phase_id,
                   "merge_commit": merge_commit, "source_checkpoint": source_checkpoint,
                   "target_branch": target_branch, "merged_at": pass_recorder.iso_time(merged_at),
                   "timestamp_epoch": merged_at}
        _ = handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        if (os.environ.get("UNIT_SYNC_WRITES") or os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1")) != "0":
            os.fsync(handle.fileno())
    return record


def _history_window(args: argparse.Namespace) -> tuple[float, float]:
    bounds: list[float] = []
    for name, default in (("since", 0.0), ("until", pass_recorder.now_epoch())):
        text = pass_recorder.arg_string(args, name)
        try:
            parsed = datetime.fromisoformat(text) if text else None
        except ValueError as error:
            raise SystemExit(f"--{name} must be an ISO 8601 time") from error
        bounds.append(default if parsed is None else
                      (parsed if parsed.tzinfo else parsed.replace(tzinfo=user_zone())).timestamp())
    since, until = bounds
    if since >= until:
        raise SystemExit("--since must be before --until")
    return since, until


def _attribution_candidates(wait: dict[str, object]) -> list[dict[str, object]]:
    value = (pass_recorder.object_dict(wait.get("wait_attribution")) or {}).get("candidates")
    return [record for item in cast(list[object], value) if (record := pass_recorder.object_dict(item)) is not None] if isinstance(value, list) else []


def _wait_holder_history(events: list[dict[str, object]]) -> dict[tuple[str, str], list[dict[str, object]]]:
    histories: dict[tuple[str, str], list[dict[str, object]]] = {}
    for event in events:
        event_type = event.get("event_type")
        terminal = event_type in ("wait_finished", "wait_unclosed")
        if event_type not in ("wait_started", "wait_not_recorded", "wait_holder_seen") and not terminal:
            continue
        if terminal and "holder" not in event:
            continue
        raw_value = event.get("holder")
        fields = pass_recorder.object_dict(raw_value)
        if "holder" not in event:
            holder_fields = _holder_fields(HolderNotNamed("recorded before holders"))
        elif fields is None:
            holder_fields = MalformedHolder(raw_value, "holder must be an object").to_fields()
        else:
            try:
                holder_fields = _holder_fields(_holder_from_fields(fields))
            except ValueError as error:
                holder_fields = MalformedHolder(raw_value, f"invalid holder record: {error}").to_fields()
        key = (pass_recorder.string(event.get("run_id")), pass_recorder.string(event.get("wait_id")))
        history = histories.setdefault(key, [])
        # Terminal rows copy the start holder, not later holder observations.
        # Keep new terminal evidence without repeating a copied observation.
        if terminal and any(observation["holder"] == holder_fields for observation in history):
            continue
        epoch = pass_recorder.number(event.get("timestamp_epoch"))
        history.append({"at": pass_recorder.iso_time(epoch), "epoch": epoch, "holder": holder_fields})
    return histories


def _counted_uncertain_reasons(waits: Iterable[dict[str, object]]) -> str:
    counts: dict[str, int] = {}
    for wait in waits:
        reason = pass_recorder.string((pass_recorder.object_dict(wait.get("wait_attribution")) or {}).get("reason"))
        counts[reason] = counts.get(reason, 0) + 1
    return "; ".join(f"{reason} ({count} times)" if count > 1 else reason
                     for reason, count in counts.items())


def _read_waits(args: argparse.Namespace) -> None:
    since, until = _history_window(args)
    session = pass_recorder.arg_string(args, "session_dir")
    ignored = 0
    if session:
        events = _run_events(pass_recorder.read_state(pass_recorder.argument_session_dir(args), resolve_staff=False))
        run_ids = {event.get("run_id") for event in events}
        events.extend(wait for wait in _external_wait_events() if wait.get("run_id") in run_ids or any(
            candidate.get("run_id") in run_ids for candidate in
            _attribution_candidates(wait)))
    else:
        events, ignored = load_events()
        events.extend(_external_wait_events())
    without_id = [event for event in events
                  if event.get("event_type") in ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded")
                  and not pass_recorder.string(event.get("wait_id"))]
    ignored += len(without_id)
    histories = _wait_holder_history(events)
    records: list[dict[str, object]] = []
    for wait in _waits_by_id(events).values():
        key = (pass_recorder.string(wait.get("run_id")), pass_recorder.string(wait.get("wait_id")))
        epoch = pass_recorder.number(wait.get("wait_started_epoch"))
        holders = histories.get(key, [{"at": pass_recorder.iso_time(epoch), "epoch": epoch,
                                      "holder": _holder_fields(HolderNotNamed("recorded before holders"))}])
        records.append({**wait, "holders": holders})
    selected = [wait for wait in records if since <= pass_recorder.number(wait.get("wait_started_epoch")) < until]
    missing = [wait for wait in selected if wait.get("event_type") == "wait_not_recorded"]
    waits = [wait for wait in selected if wait.get("event_type") != "wait_not_recorded"]
    uncertain = [wait for wait in selected if (pass_recorder.object_dict(wait.get("wait_attribution")) or {}).get("state")
                 in ("UnresolvedAttribution", "UnresolvedSupplier")]
    handoffs = [wait for wait in waits if (pass_recorder.object_dict(wait.get("wait_attribution")) or {}).get("state") == "ProductionHandoff"]
    phases: list[dict[str, object]] = []
    old_phase_n = 0
    sources: dict[str, PhaseSourceIdentity] = {}
    for event in events:
        if event.get("event_type") != "phase_started" or not since <= pass_recorder.number(event.get("timestamp_epoch")) < until:
            continue
        own = [wait for wait in waits if (wait.get("run_id"), wait.get("phase_instance_id")) ==
               (event.get("run_id"), event.get("phase_instance_id"))]
        own_missing = [wait for wait in missing if (wait.get("run_id"), wait.get("phase_instance_id")) ==
                       (event.get("run_id"), event.get("phase_instance_id"))]
        own_without_id = sum((row.get("run_id"), row.get("phase_instance_id")) ==
                             (event.get("run_id"), event.get("phase_instance_id")) for row in without_id)
        recorded = (pass_recorder.object_dict(event.get("wait_coverage")) or {}).get("state") == "Recorded"
        own_uncertain = [wait for wait in uncertain if any(
            (candidate.get("run_id"), candidate.get("phase_instance_id")) ==
            (event.get("run_id"), event.get("phase_instance_id")) for candidate in _attribution_candidates(wait))]
        old_phase_n += not recorded
        phase: dict[str, object] = {"run_id": event.get("run_id"), "phase_id": event.get("phase_id"),
                                    "phase_instance_id": event.get("phase_instance_id"),
                                    "unit": event.get("worktree"),
                                    "state": "Covered" if recorded and not own_missing else "HistoryNotRecorded"}
        plan = pass_recorder.string(event.get("project_plan_doc")) or pass_recorder.string(event.get("plan_doc"))
        phase["plan_doc"] = _phase_metadata_fields(_recorded_phase_metadata(plan, "phase has no plan document"))
        if "production" not in event or "repository" not in event:
            run_id = pass_recorder.string(event.get("run_id"))
            if run_id not in sources:
                sources[run_id] = _phase_source_identity(event)
            source = sources[run_id]
            phase["production"] = _phase_metadata_fields(source.production)
            phase["repository"] = _phase_metadata_fields(source.repository)
        for field in ("production", "repository"):
            if field in event:
                metadata = _recorded_phase_metadata(event[field], f"phase has no {field}")
                phase[field] = _phase_metadata_fields(metadata)
        if own_missing:
            phase.update(reason="wait emission has a missing record", wait_missing_n=len(own_missing),
                         recorded_wait_n=len(own))
        elif recorded:
            phase["wait_n"] = len(own)
        else:
            phase["reason"] = "phase start has no wait coverage record"
        phase["candidates"] = []
        if recorded and not own_missing:
            phase["reason"] = "wait coverage recorded"
        if own_uncertain:
            _ = phase.pop("wait_n", None)
            phase.update(state="UncertainCoverage", recorded_wait_n=len(own),
                         reason=_counted_uncertain_reasons(own_uncertain),
                         candidates=[candidate for wait in own_uncertain for candidate in _attribution_candidates(wait)],
                         attribution_miss_ids=[wait["wait_id"] for wait in own_uncertain])
        if own_without_id:
            _ = phase.pop("wait_n", None)
            phase.update(state="HistoryNotRecorded", wait_without_id_n=own_without_id,
                         recorded_wait_n=len(own))
            if not own_missing:
                phase["reason"] = "wait record has no wait id"
        phases.append(phase)
    print(json.dumps({"since": pass_recorder.iso_time(since), "until": pass_recorder.iso_time(until),
                      "selection": "phase starts and wait starts in [since, until)",
                      "covered_n": sum(phase["state"] == "Covered" for phase in phases),
                      "missing_n": old_phase_n + len(missing),
                      "missing_phase_n": sum(phase["state"] == "HistoryNotRecorded" for phase in phases),
                      "missing_record_n": len(missing),
                      "attribution_miss_n": len(uncertain),
                      "uncertain_phase_n": sum(phase["state"] == "UncertainCoverage" for phase in phases),
                      "production_handoff_n": len(handoffs), "handoffs": handoffs,
                      "ignored_n": ignored, "wait_without_id_n": len(without_id),
                      "phases": phases, "waits": waits, "missing": missing}, sort_keys=True))


def _read_receipts(args: argparse.Namespace) -> None:
    since, until = _history_window(args)
    path = plan_history.history_root() / "merge_receipts.jsonl"
    receipts: list[dict[str, object]] = []
    problems: list[str] = []
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            for line in handle:
                receipt = pass_recorder.json_object(line)
                if receipt is None:
                    problems.append("NotRecorded: invalid receipt record")
                elif receipt.get("event_type") == "merge_receipt":
                    receipts.append(receipt)
    except FileNotFoundError:
        pass
    except OSError as error:
        problems.append(f"NotRecorded: {error}")
    selected = [receipt for receipt in receipts if since <= pass_recorder.number(receipt.get("timestamp_epoch")) < until
                and all(not pass_recorder.arg_string(args, option) or receipt.get(option) == pass_recorder.arg_string(args, option)
                        for option in ("project", "target_branch", "merge_commit"))]
    expected = pass_recorder.arg_string(args, "merge_commit")
    missing: list[dict[str, str]] = ([{"state": "ReceiptMissing", "merge_commit": expected}]
                                    if expected and not selected else [])
    print(json.dumps({"since": pass_recorder.iso_time(since), "until": pass_recorder.iso_time(until),
                      "covered_n": len(selected), "missing_n": len(missing),
                      "receipts": selected, "missing": missing, "problems": problems}, sort_keys=True))


@dataclass(frozen=True)
class InterruptedWorkerPasses:
    """Close these workers after the recorder releases the progress lock."""

    worker_ids: tuple[staff.StaffId, ...] = ()


class KeptWorkerPane(Protocol):
    @property
    def blocker(self) -> str: ...


class FailedWorkerPaneClose(Protocol):
    @property
    def reason(self) -> str: ...


def _close_interrupted_workers(passes: InterruptedWorkerPasses) -> None:
    from scripts.herdr import herdr_types

    module = import_module("scripts.herdr.worker_panes")
    close = cast(Callable[[staff.StaffId, staff.WorkerName], object], module.close)
    closed = cast(type[object], module.Closed)
    kept = cast(type[object], module.Kept)

    for identity in passes.worker_ids:
        try:
            record = staff.snapshot()
            row = record.by_id(identity)
            if row is None or row.kind != "worker":
                continue
            parent = record.parent_of(row)
            if parent is None:
                continue
            with redirect_stderr(io.StringIO()):
                result = close(parent.id, staff.WorkerName.parse(row.name))
            if isinstance(result, closed):
                continue
            reason = (cast(KeptWorkerPane, result).blocker if isinstance(result, kept)
                      else cast(FailedWorkerPaneClose, result).reason)
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError,
                staff.StaffRefused, staff.StaffUnreadable) as error:
            reason = str(error)
        print(f"anomaly: finish-phase: worker {identity}: pane retained: {' '.join(reason.splitlines())}", file=sys.stderr)


def _paused_seconds(waits: Iterable[dict[str, object]], instance_id: str,
                    started_at: float, finished_at: float, now: float) -> float:
    """The time inside the phase that its user and supplier waits cover, counted once where they overlap.

    An unclosed wait ends when it was marked unclosed; a wait still open ends now.
    """
    spans: list[tuple[float, float]] = []
    for wait in waits:
        if wait.get("phase_instance_id") != instance_id or wait.get("wait_kind") not in PAUSE_WAIT_KINDS:
            continue
        match wait.get("event_type"):
            case "wait_started":
                end = now
            case "wait_finished":
                end = pass_recorder.number((pass_recorder.object_dict(wait.get("wait_end")) or {}).get("epoch"), pass_recorder.number(wait.get("timestamp_epoch"), now))
            case "wait_unclosed":
                end = pass_recorder.number(wait.get("timestamp_epoch"), now)
            case _:
                continue
        begin = max(started_at, pass_recorder.number(wait.get("wait_started_epoch"), end))
        end = min(finished_at, end)
        if end > begin:
            spans.append((begin, end))
    total, reached = 0.0, started_at
    for begin, end in sorted(spans):
        if end > reached:
            total += end - max(begin, reached)
            reached = end
    return total


def _finish_phase(args: argparse.Namespace) -> InterruptedWorkerPasses:
    # The run lock is already held. External starts use this same history lock,
    # through the cleanup scan and publication of phase_finished.
    with _external_wait_lock():
        return _finish_phase_with_wait_lock(args)


def _finish_phase_with_wait_lock(args: argparse.Namespace) -> InterruptedWorkerPasses:
    """Close one open phase: the one --phase-id names, else the current one.

    Only that phase's own passes close with it; another open phase keeps its
    workers. When the current phase closes, the most recently started older open
    phase becomes current again.
    """
    from scripts.direct import phase_reservation

    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    phase_id = pass_recorder.optional_arg(args, "phase_id")
    current = pass_recorder.object_dict(state.get("phase"))
    current_open = current is not None and pass_recorder.string(current.get("status")) == "active"
    if phase_id:
        phase = pass_recorder.open_phase_named(state, phase_id)
        if phase is None:
            open_ids = ", ".join(recorder_state.open_phase_ids(state)) or "none"
            raise SystemExit(f"Phase {phase_id} is not open; open phases: {open_ids}")
    elif current is not None and current_open:
        phase = current
    else:
        return InterruptedWorkerPasses()
    is_current = phase is current
    now = pass_recorder.now_epoch()
    finished_at = _optional_epoch(args, "finished_at", "--finished-at", now)
    started_at = pass_recorder.number(phase.get("started_at"), finished_at)
    if finished_at < started_at:
        raise SystemExit(
            f"--finished-at is before Phase {pass_recorder.string(phase.get('id'))} started "
            + f"({pass_recorder.iso_time(started_at)})"
        )
    instance_id = pass_recorder.string(phase.get("instance_id"))
    run_waits = list(_waits_by_id(_run_events(state)).values())
    external_waits = [wait for wait in _waits_by_id(_external_wait_events()).values()
                      if wait.get("run_id") == state.get("run_id")]
    paused = _paused_seconds(itertools.chain(run_waits, external_waits), instance_id, started_at, finished_at, now)
    for wait in run_waits:
        if wait.get("phase_instance_id") == instance_id and wait.get("event_type") == "wait_started":
            _end_wait(state, wait, now, unclosed=True)
    for wait in external_waits:
        if wait.get("phase_instance_id") == instance_id and wait.get("event_type") == "wait_started":
            _end_wait({"history_file": str(_external_wait_file())}, wait, now, unclosed=True)
    # Every window this phase left open, one per team member: the launchers that
    # would have closed them are gone. A pass from before passes carried their
    # phase belongs to the current one.
    interrupted: list[staff.StaffId] = []
    for slot, record in sorted(pass_recorder.open_passes(state).items()):
        owner = pass_recorder.pass_phase_instance(record)
        if owner == instance_id or (is_current and not owner):
            if pass_recorder.close_slot_pass(session_dir, state, slot, "interrupted", now):
                identity = pass_recorder.string(record.get("worker_id"))
                if identity:
                    interrupted.append(staff.StaffId(identity))
    if is_current:
        _close_active_activity(session_dir, state, "interrupted", "", now)
        _ = pass_recorder.clear_armed_review(session_dir, state, "phase ended", now)
    status = pass_recorder.arg_string(args, "status")
    event = pass_recorder.record_event(state, "phase_finished", finished_at, phase_record=phase)
    event["contract_revision"] = _contract_revision()
    event["status"] = status
    event["phase_elapsed_seconds"] = max(0, int(finished_at - started_at))
    event["phase_active_seconds"] = max(0, int(finished_at - started_at - paused))
    if finished_at != now:
        event["recorded_at"] = pass_recorder.iso_time(now)
        event["backfilled"] = True
    pass_recorder.append_event(state, event)
    phase["status"] = status
    phase["finished_at"] = finished_at
    if is_current:
        _unpark_newest(state)
    else:
        state[OPEN_PHASES_KEY] = [
            record for record in pass_recorder.parked_phases(state) if record is not phase
        ]
    pass_recorder.write_state(session_dir, state)
    # A completed phase's state that owns no reservation ends with it. Active and a pending
    # release stay: only the checkpoint and its confirmed release clear them.
    if status == "completed":
        phase_dir = phase_reservation.phase_directory(session_dir, pass_recorder.string(phase.get("id")))
        state_kind = pass_recorder.string(phase_reservation.read_state(phase_dir).get("kind"))
        if state_kind in phase_reservation.BOARD_STATES.values():
            _ = phase_reservation.clear(phase_dir)
    _refresh_phase_table(session_dir)
    return InterruptedWorkerPasses(tuple(dict.fromkeys(interrupted)))


def _drop_phase(args: argparse.Namespace) -> InterruptedWorkerPasses:
    """Close an open phase and record why its attempt leaves the board."""
    reason = pass_recorder.arg_string(args, "reason").strip()
    if not reason:
        raise SystemExit("Cannot drop a phase: --reason is empty")
    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    phase_id = pass_recorder.arg_string(args, "phase_id")
    passes = InterruptedWorkerPasses()
    if pass_recorder.open_phase_named(state, phase_id) is not None:
        passes = _finish_phase(argparse.Namespace(session_dir=str(session_dir), phase_id=phase_id, status="stopped"))
        args.interrupted_worker_passes = passes
        state = pass_recorder.read_state(session_dir)
    events = _run_events(state)
    finished = next((event for event in reversed(events)
                     if event.get("event_type") == "phase_finished" and event.get("phase_id") == phase_id), None)
    if finished is None:
        raise SystemExit(f"Cannot drop Phase {phase_id}: no open or finished instance")
    if finished.get("status") == "completed":
        raise SystemExit(f"Cannot drop Phase {phase_id}: the newest instance completed")
    instance = pass_recorder.string(finished.get("phase_instance_id"))
    previous = next((event for event in reversed(events)
                     if event.get("event_type") == "phase_dropped" and event.get("phase_instance_id") == instance), None)
    if previous is not None and previous.get("reason") == reason:
        return passes
    phase = {"id": phase_id, "instance_id": instance, "title": finished.get("phase_title", ""),
             "started_at": finished.get("phase_started_at", 0)}
    event = pass_recorder.record_event(state, "phase_dropped", pass_recorder.now_epoch(), phase_record=phase)
    event["reason"] = reason
    pass_recorder.append_event(state, event)
    _refresh_phase_table(session_dir)
    return passes


def _clear_run_pointer(session_dir: Path, *, record: staff.StaffSnapshot) -> None:
    folder = session_dir.resolve()
    for row in record.rows:
        if isinstance(row.run, staff.OpenRun) and row.run.path.resolve() == folder:
            _ = staff.clear_run(row.id, session_dir)


def _finish_run(args: argparse.Namespace) -> InterruptedWorkerPasses:
    session_dir = pass_recorder.argument_session_dir(args)
    record = staff.initial_snapshot()
    state = pass_recorder.read_state(session_dir, record=record)
    now = pass_recorder.now_epoch()
    run_status = pass_recorder.arg_string(args, "status")
    interrupted: list[staff.StaffId] = []
    # Every open phase, newest first: closing the current one makes the next
    # older one current.
    while recorder_state.open_phase_ids(state):
        finish_args = argparse.Namespace(session_dir=str(session_dir), status=run_status)
        interrupted.extend(_finish_phase(finish_args).worker_ids)
        state = pass_recorder.read_state(session_dir, record=record)
    passes = InterruptedWorkerPasses(tuple(dict.fromkeys(interrupted)))
    if not unit_runs.run_is_open(session_dir, record=record):
        _clear_run_pointer(session_dir, record=record)
        return passes
    # A repeated finish clears a restored pointer without recording the outcome again.
    if worker_lifecycle.finished_run_status(str(session_dir)):
        _clear_run_pointer(session_dir, record=record)
        return passes
    _close_active_activity(session_dir, state, "interrupted", "", now)
    state = pass_recorder.read_state(session_dir, record=record)
    event = pass_recorder.record_event(state, "run_finished", now)
    event["status"] = run_status
    event["run_elapsed_seconds"] = max(
        0,
        int(now - pass_recorder.number(state.get("run_started_at"), now)),
    )
    event["total_elapsed_seconds"] = max(
        0,
        int(now - pass_recorder.number(state.get("project_started_at"), now)),
    )
    pass_recorder.append_event(state, event)
    state["status"] = run_status
    state["finished_at"] = now
    pass_recorder.write_state(session_dir, state)
    _clear_run_pointer(session_dir, record=record)
    _refresh_phase_table(session_dir)
    return passes


def _finding_lenses(event: dict[str, object]) -> set[str]:
    """Read a finding's lenses, with `both` read as its pair."""
    lens = pass_recorder.string(event.get("lens"))
    if lens == LEGACY_BOTH_LENS:
        return set(LEGACY_BOTH_LENSES)
    return {part.strip() for part in lens.split(",") if part.strip()}


SEPARATE_TRIAL_LENSES = ("ux",)


def write_review_turn(session_dir: Path, metadata: Mapping[str, object], *,
                      reviewer: str, thread_id: str, lens: str, pass_number: int,
                      started_at: int, status: str, raised: int, details: Mapping[str, object]) -> None:
    """Store one review result without replacing another result from the same second."""
    record: dict[str, object] = {
        "arm": "own", "unit": metadata.get("unit", ""), "phase": metadata.get("phase", ""),
        "phase_instance_id": metadata.get("phase_instance_id", ""),
        "history_file": metadata.get("history_file", ""),
        "reviewer": reviewer, "thread_id": thread_id, "lens": lens,
        "kind": "ux" if lens == "ux" else "broad" if pass_number == 1 and lens else "closure",
        "pass": pass_number, "started_at": started_at, "finished_at": time.time(),
        "status": status, "raised": raised, **details,
    }
    root = plan_history.history_root() / "review_turns" / session_dir.name
    root.mkdir(parents=True, exist_ok=True)
    for sequence in itertools.count():
        try:
            output = (root / f"{started_at}-{os.getpid()}-{sequence}.json").open("x", encoding="utf-8")
        except FileExistsError:
            continue
        with output:
            json.dump(record, output, ensure_ascii=False)
            _ = output.write("\n")
        return


def _review_trial(args: argparse.Namespace) -> None:
    """Print the phase's findings and review minutes for the checkpoint notice.

    A named phase selects its open instance or its latest completed instance.
    An unnamed command selects the current phase, including a completed phase.
    Only the selected instance contributes to the line.
    Its ledger supplies the findings and dispatched rounds, including routed and closed findings.
    Review-worker minutes count finished review passes and UX review activities.
    UX check minutes count `UX review` activities.
    UX repair minutes count fix passes whose dispatched batch includes a UX finding.
    Each time rounds to the nearest whole minute.
    """
    from scripts.direct import findings

    session_dir = pass_recorder.argument_session_dir(args)
    state = pass_recorder.read_state(session_dir)
    phase_id = pass_recorder.optional_arg(args, "phase_id")
    if phase_id:
        phase = pass_recorder.open_phase_named(state, phase_id)
        if phase is None:
            phase = next(
                (
                    {"instance_id": pass_recorder.string(event.get("phase_instance_id"))}
                    for event in reversed(_run_events(state))
                    if pass_recorder.string(event.get("event_type")) == "phase_started"
                    and pass_recorder.string(event.get("phase_id")) == phase_id
                ),
                None,
            )
        if phase is None:
            raise SystemExit(f"This run never started Phase {phase_id}")
    else:
        phase = pass_recorder.object_dict(state.get("phase"))
    instance_id = pass_recorder.string(phase.get("instance_id")) if phase is not None else ""
    if not instance_id:
        raise SystemExit(
            f"No phase recorded in {pass_recorder.state_path(session_dir)}: review-trial reports "
            + "the phase start-phase opened, and this run has not opened one"
        )
    ledger = findings.read_phase_ledger(session_dir, instance_id, phase_id, pass_recorder.now_epoch())
    ux_finding_ids: set[str] = set()
    code_finding_ids: set[str] = set()
    for finding_id, value in (pass_recorder.object_dict(ledger.get("findings")) or {}).items():
        entry = pass_recorder.object_dict(value)
        if entry is None:
            continue
        lenses = _finding_lenses(entry)
        if "ux" in lenses:
            ux_finding_ids.add(finding_id)
        if not lenses or lenses.difference(SEPARATE_TRIAL_LENSES):
            code_finding_ids.add(finding_id)
    review_seconds = 0
    ux_check_seconds = 0
    fix_passes: list[dict[str, object]] = []
    events = _run_events(state)
    finished_passes = {pass_recorder.string(event.get("pass_instance_id")): event for event in events
                       if event.get("event_type") == "pass_finished"}
    for event in events:
        if pass_recorder.string(event.get("phase_instance_id")) != instance_id:
            continue
        event_type = pass_recorder.string(event.get("event_type"))
        if event_type == "pass_finished" and event is not finished_passes[pass_recorder.string(event.get("pass_instance_id"))]:
            continue
        if event_type == "pass_finished" and pass_recorder.string(event.get("pass_kind")) == "review":
            review_seconds += pass_recorder.integer(event.get("pass_elapsed_seconds"))
        elif event_type == "pass_finished" and pass_recorder.string(event.get("pass_kind")) == "fix":
            fix_passes.append(event)
        elif (
            event_type == "activity_finished"
            and pass_recorder.string(event.get("activity_label")) == UX_REVIEW_ACTIVITY_LABEL
        ):
            elapsed = pass_recorder.integer(event.get("activity_elapsed_seconds"))
            review_seconds += elapsed
            ux_check_seconds += elapsed
    # A fix pass carries the round the ledger dispatched (`fix_pass`), and the
    # dispatch names the findings that round covers.
    rounds = ledger.get("rounds")
    dispatched = cast(list[object], rounds) if isinstance(rounds, list) else []
    ux_rounds = {
        pass_recorder.integer(event.get("round"))
        for value in dispatched
        if (event := pass_recorder.object_dict(value)) is not None
        and ux_finding_ids.intersection(pass_recorder.string_list(event.get("covered")))
    }
    ux_repair_seconds = sum(
        pass_recorder.integer(event.get("pass_elapsed_seconds"))
        for event in fix_passes
        if pass_recorder.integer(event.get("fix_pass")) in ux_rounds
    )
    print(
        f"review trial: ux {len(ux_finding_ids)} findings, code {len(code_finding_ids)} findings, "
        + f"review-worker minutes {(review_seconds + 30) // 60}, "
        + f"ux check minutes {(ux_check_seconds + 30) // 60}, "
        + f"ux repair minutes {(ux_repair_seconds + 30) // 60}"
    )


def _aggregate(args: argparse.Namespace) -> None:
    events, ignored = load_events()
    samples = _calibration_samples(events)
    requested_percent = pass_recorder.arg_integer(args, "percent", -1)
    grouped: dict[tuple[int, str], list[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        if requested_percent >= 0 and sample["raw_percent"] != requested_percent:
            continue
        grouped[(sample["raw_percent"], sample["pass_kind"])].append(sample)
    groups: list[dict[str, object]] = []
    for (raw_percent, pass_kind), group_samples in sorted(grouped.items()):
        groups.append(
            {
                "raw_percent": raw_percent,
                "pass_kind": pass_kind,
                **_sample_metrics(group_samples, 0),
            }
        )
    output = {
        "schema_version": pass_recorder.SCHEMA_VERSION,
        "generated_at": pass_recorder.iso_time(pass_recorder.now_epoch()),
        "history_root": str(plan_history.history_root()),
        "completed_raw_estimate_samples": len(samples),
        "ignored_history_rows": ignored,
        "groups": groups,
    }
    print(json.dumps(output, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record and aggregate durable unit progress events."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    identity = subparsers.add_parser("set-unit")
    _ = identity.add_argument("--session-dir", required=True)
    _ = identity.add_argument("--production", required=True)
    _ = identity.add_argument("--unit", required=True)
    identity.set_defaults(handler=_set_unit)

    start_run = subparsers.add_parser("start-run")
    _ = start_run.add_argument("--session-dir", required=True)
    _ = start_run.add_argument("--working-dir", required=True)
    _ = start_run.add_argument("--plan-doc", default="")
    pass_recorder.add_identity_options(start_run)
    start_run.set_defaults(handler=_start_run)

    start_phase = subparsers.add_parser("start-phase")
    _ = start_phase.add_argument("--session-dir", required=True)
    _ = start_phase.add_argument("--phase-id", required=True)
    _ = start_phase.add_argument("--phase-title", required=True)
    _ = start_phase.add_argument("--work-order-file", default="")
    _ = start_phase.add_argument("--started-at", default="")
    start_phase.set_defaults(handler=_start_phase)

    start_wait = subparsers.add_parser("start-wait")
    _ = start_wait.add_argument("--session-dir", required=True)
    _ = start_wait.add_argument("--kind", choices=WAIT_KINDS, required=True)
    wait_phase = start_wait.add_mutually_exclusive_group()
    _ = wait_phase.add_argument("--phase-id", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--slot", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--worker-thread-id", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--director", action="store_true")
    _ = wait_phase.add_argument("--phase-instance-id", default=argparse.SUPPRESS)
    _ = wait_phase.add_argument("--target", default=argparse.SUPPRESS)
    _ = start_wait.add_argument("--by", default="")
    _ = start_wait.add_argument("--missing-reason", default="")
    _ = start_wait.add_argument("--reason", default="")
    _ = start_wait.add_argument("--owner-session", default="")
    _ = start_wait.add_argument("--unit", default="")
    _ = start_wait.add_argument("--probe", action="store_true")
    _add_holder_arguments(start_wait)
    start_wait.set_defaults(handler=_start_wait)

    wait_holder = subparsers.add_parser("wait-holder")
    _ = wait_holder.add_argument("--session-dir", required=True)
    _ = wait_holder.add_argument("--wait-id", required=True)
    _add_holder_arguments(wait_holder)
    wait_holder.set_defaults(handler=_wait_holder)

    finish_wait = subparsers.add_parser("finish-wait")
    _ = finish_wait.add_argument("--session-dir", required=True)
    _ = finish_wait.add_argument("--wait-id", required=True)
    finish_wait.set_defaults(handler=_finish_wait)

    checkpoint = subparsers.add_parser("checkpoint")
    _ = checkpoint.add_argument("--session-dir", required=True)
    _ = checkpoint.add_argument("--phase", required=True)
    _ = checkpoint.add_argument("--commit", required=True)
    checkpoint.set_defaults(handler=_checkpoint)

    for verb in ("start-checkpoint-wait", "start-handoff-wait"):
        external_wait = subparsers.add_parser(verb)
        _ = external_wait.add_argument("--commit", required=True)
        _ = external_wait.add_argument("--owner-session", default="")
        _ = external_wait.add_argument("--unit", default="")
        _ = external_wait.add_argument("--reason", default="")
        if verb == "start-checkpoint-wait":
            _ = external_wait.add_argument("--working-dir", required=True)
            _ = external_wait.add_argument("--phase-id", required=True)
        else:
            _ = external_wait.add_argument("--production", required=True)
        external_wait.set_defaults(handler=_start_external_wait)

    finish_external = subparsers.add_parser("finish-external-wait")
    _ = finish_external.add_argument("--wait-id", required=True)
    finish_external.set_defaults(handler=_finish_external_wait)

    waits = subparsers.add_parser("waits")
    _ = waits.add_argument("--session-dir", default="")
    _ = waits.add_argument("--since", default="")
    _ = waits.add_argument("--until", default="")
    waits.set_defaults(handler=_read_waits)

    receipts = subparsers.add_parser("receipts")
    _ = receipts.add_argument("--since", default="")
    _ = receipts.add_argument("--until", default="")
    _ = receipts.add_argument("--merge-commit", default="")
    _ = receipts.add_argument("--project", default="")
    _ = receipts.add_argument("--target-branch", default="")
    receipts.set_defaults(handler=_read_receipts)

    start_activity = subparsers.add_parser("start-activity")
    _ = start_activity.add_argument("--session-dir", required=True)
    _ = start_activity.add_argument("--label", required=True)
    _ = start_activity.add_argument("--activity", required=True)
    start_activity.set_defaults(handler=_start_activity)

    finish_activity = subparsers.add_parser("finish-activity")
    _ = finish_activity.add_argument("--session-dir", required=True)
    _ = finish_activity.add_argument(
        "--status",
        choices=("completed", "error", "canceled", "interrupted"),
        default="completed",
    )
    _ = finish_activity.add_argument(
        "--result",
        default="",
        help="short outcome for the stage table, such as pass, clean, or no change",
    )
    finish_activity.set_defaults(handler=_finish_activity)

    arm_review = subparsers.add_parser(
        "arm-review",
        help="show an early-launched reviewer as a running row beside the passes "
        + "already open",
    )
    _ = arm_review.add_argument("--session-dir", required=True)
    _ = arm_review.add_argument("--activity", required=True)
    _ = arm_review.add_argument("--called-task", default="direct.review")
    _ = arm_review.add_argument("--lens", default="")
    _ = arm_review.add_argument("--worker", default="", help="the named assign.sh reviewer; omit for review.sh")
    _ = arm_review.add_argument("--phase-id", default="", help="the open phase the review belongs to, as review.sh --phase names it")
    arm_review.set_defaults(handler=_arm_review)

    disarm_review = subparsers.add_parser(
        "disarm-review",
        help="drop the early reviewer's row when its launch is canceled or discarded",
    )
    _ = disarm_review.add_argument("--session-dir", required=True)
    _ = disarm_review.add_argument("--reason", default="cleared")
    disarm_review.set_defaults(handler=_disarm_review)

    calibrate = subparsers.add_parser("calibrate")
    _ = calibrate.add_argument("--session-dir", required=True)
    _ = calibrate.add_argument("--candidate-percent", type=int, required=True)
    _ = calibrate.add_argument("--cap-stage", choices=tuple(PROGRESS_CAPS), default="")
    calibrate.set_defaults(handler=_calibrate)

    progress = subparsers.add_parser("progress")
    _ = progress.add_argument("--session-dir", required=True)
    _ = progress.add_argument("--tables", action="store_true", help="include the round table")
    _ = progress.add_argument("--raw-percent", type=int, default=-1)
    _ = progress.add_argument("--percent", type=int, default=-1)
    _ = progress.add_argument("--project-raw-percent", type=int, default=-1)
    _ = progress.add_argument("--project-percent", type=int, default=-1)
    _ = progress.add_argument("--phase-raw-percent", type=int, default=-1)
    _ = progress.add_argument("--phase-percent", type=int, default=-1)
    _ = progress.add_argument("--activity", required=True)
    _ = progress.add_argument("--cap-stage", choices=tuple(PROGRESS_CAPS), default="")
    _ = progress.add_argument("--override-reason", default="")
    _ = progress.add_argument("--project-override-reason", default="")
    _ = progress.add_argument("--phase-override-reason", default="")
    progress.set_defaults(handler=_progress)

    eta = subparsers.add_parser("eta")
    _ = eta.add_argument("--session-dir", required=True)
    _ = eta.add_argument("--phase-id", default="")
    _ = eta.add_argument("--time", required=True)
    _ = eta.add_argument("--earliest", default="")
    _ = eta.add_argument("--latest", default="")
    _ = eta.add_argument("--basis", required=True)
    eta.set_defaults(handler=_eta)

    timeline = subparsers.add_parser("timeline")
    _ = timeline.add_argument("--session-dir", required=True)
    _ = timeline.add_argument(
        "--phase",
        default="",
        help="restrict the table to this phase identifier; omit for every phase",
    )
    timeline.set_defaults(handler=_timeline)

    phase_count = subparsers.add_parser("phase-count")
    _ = phase_count.add_argument("--plan-doc", required=True)
    _ = phase_count.add_argument("--phase-percent", type=int, default=0)
    phase_count.set_defaults(handler=_phase_count)

    finish_phase = subparsers.add_parser("finish-phase")
    _ = finish_phase.add_argument("--session-dir", required=True)
    _ = finish_phase.add_argument(
        "--status",
        choices=("completed", "stopped", "error"),
        required=True,
    )
    _ = finish_phase.add_argument("--phase-id", default="")
    _ = finish_phase.add_argument("--finished-at", default="")
    finish_phase.set_defaults(handler=_finish_phase)

    drop_phase = subparsers.add_parser("drop-phase")
    _ = drop_phase.add_argument("--session-dir", required=True)
    _ = drop_phase.add_argument("--phase-id", required=True)
    _ = drop_phase.add_argument("--reason", required=True)
    drop_phase.set_defaults(handler=_drop_phase)

    finish_run = subparsers.add_parser("finish-run")
    _ = finish_run.add_argument("--session-dir", required=True)
    _ = finish_run.add_argument(
        "--status",
        choices=("completed", "stopped", "error"),
        required=True,
    )
    finish_run.set_defaults(handler=_finish_run)

    review_trial = subparsers.add_parser(
        "review-trial",
        help="one line for the checkpoint notice: the phase's ux and craft finding "
        + "counts and its review-worker minutes",
    )
    _ = review_trial.add_argument("--session-dir", required=True)
    _ = review_trial.add_argument("--phase-id", default="")
    review_trial.set_defaults(handler=_review_trial)

    aggregate = subparsers.add_parser("aggregate")
    _ = aggregate.add_argument("--percent", type=int, default=-1)
    aggregate.set_defaults(handler=_aggregate)
    return parser


def main() -> None:
    # Remove this deploy path on 2026-11-23.
    if sys.argv[1:2] and sys.argv[1] in ("check-launch", "start-pass", "finish-pass"):
        pass_recorder.main()
        return
    args = _build_parser().parse_args()
    handler_value: object = getattr(args, "handler")  # pyright: ignore[reportAny]
    if not callable(handler_value):
        raise SystemExit("No command handler selected")
    handler = cast(Callable[[argparse.Namespace], object], handler_value)
    if pass_recorder.arg_string(args, "command") in ("waits", "receipts"):
        _ = handler(args)
        return
    session_value: object = getattr(args, "session_dir", "")
    session_text = pass_recorder.string(session_value)
    if not session_text:
        _ = handler(args)
        return
    session_dir = Path(session_text).expanduser().resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    result: object = None
    try:
        with (session_dir / ".progress_history.lock").open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                result = handler(args)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        passes: object = getattr(args, "interrupted_worker_passes", result)
        if isinstance(passes, InterruptedWorkerPasses):
            _close_interrupted_workers(passes)


if __name__ == "__main__":
    main()
