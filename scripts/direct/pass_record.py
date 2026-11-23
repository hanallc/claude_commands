#!/usr/bin/env python3
"""Record launcher checks and pass events with the shared state core."""
from __future__ import annotations
if not __package__:
    try:
        with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
    except FileNotFoundError:
        pass

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from functools import cache
from pathlib import Path
from typing import Literal, NamedTuple, NoReturn, TypedDict, cast

from scripts.agents import remote
from scripts.direct import recorder_state
from scripts.lib import conf_reads, transcripts, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import session_records
from scripts.production import staff

def now_epoch() -> float:
    configured = unit_runs.environ("NOW_EPOCH")
    if configured:
        return float(configured)
    return time.time()


def _director_family(row: staff.StaffRow) -> str:
    """Read the current director's native transcript evidence."""
    extra_homes = (row.home.path,) if isinstance(row.home, staff.SessionHome) else ()
    for home in transcripts.codex_transcript_homes(extra_homes):
        for folder in ("sessions", "archived_sessions"):
            if transcripts.transcript_paths(home / folder, f"**/*{row.current_session}.jsonl"):
                return "codex"
    return "claude" if session_records.transcript_locations(row.current_session) else "unknown"


def staff_paths(fields: dict[str, object], row: staff.StaffRow) -> None:
    """Resolve recorded plan paths through the owner's current worktree."""
    previous = string(fields.get("working_dir"))
    for key in ("plan_doc", "project_plan_doc"):
        value = string(fields.get(key))
        if previous and value.startswith(previous + "/"):
            fields[key] = str(Path(value).relative_to(previous))
    for key in ("working_dir", "worktree"):
        if key in fields:
            del fields[key]
    if not isinstance(row.worktree, staff.WorktreeUnfilled):
        fields.update(working_dir=str(row.worktree.path), worktree=row.worktree.path.name)


class WorktreeBranchAtHead(NamedTuple):
    """The branch name remains valid for this HEAD text."""

    head: str
    name: str


# Each state read resolves the branch; one git start for each HEAD keeps reads cheap.
_KNOWN_BRANCHES: dict[Path, WorktreeBranchAtHead] = {}


def _head_text(worktree: Path) -> str | None:
    """Return the HEAD file text of a worktree root, or None when no file names its branch."""
    marker = worktree / ".git"
    try:
        if marker.is_dir():
            git_dir = marker
        else:
            pointer = marker.read_text(encoding="utf-8")
            if not pointer.startswith("gitdir: "):
                return None
            git_dir = worktree / pointer.removeprefix("gitdir: ").strip()
        head = (git_dir / "HEAD").read_text(encoding="utf-8")
    except OSError:
        return None
    # A reftable repository keeps this fixed stub in HEAD whatever the branch.
    return None if head.startswith("ref: refs/heads/.invalid") else head


def _worktree_branch(worktree: Path) -> str:
    """Return the current branch, or detached@<commit>, and run git again only when HEAD changes."""
    head = _head_text(worktree)
    known = _KNOWN_BRANCHES.get(worktree)
    if head is not None and known is not None and known.head == head:
        return known.name
    branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                            capture_output=True, text=True, check=False)
    name = branch.stdout.strip()
    succeeded = branch.returncode == 0
    if succeeded and not name:
        commit = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=False)
        name = f"detached@{commit.stdout.strip() or 'unknown'}"
        succeeded = commit.returncode == 0
    # A failed git run keeps nothing, so the next read runs git again.
    if head is not None and succeeded:
        _KNOWN_BRANCHES[worktree] = WorktreeBranchAtHead(head, name)
    return name


def staff_directory(fields: dict[str, object], record: staff.StaffSnapshot, *,
                    pass_agents: bool = True) -> dict[str, object]:
    """Resolve current state through staff and preserve recorded events.

    pass_agents=False leaves each pass as recorded and finds no worker backend.
    """
    if fields.get("event_type"):
        return fields
    row = unit_runs.run_owner(fields, record)
    if isinstance(row, unit_runs.RunOwnerUnrecorded):
        return fields
    staff_paths(fields, row)
    if not isinstance(row.worktree, staff.WorktreeUnfilled):
        if not fields.get("event_type"):
            fields["branch"] = _worktree_branch(row.worktree.path)
    if not fields.get("event_type"):
        main = fields.get("main_agent")
        identity = dict(cast(dict[str, object], main)) if isinstance(main, dict) else {}
        identity.update(session_id=row.current_session, family=_director_family(row))
        fields["main_agent"] = identity
        passes = fields.get("pass")
        if pass_agents and isinstance(passes, dict):
            for value in cast(dict[str, object], passes).values():
                if not isinstance(value, dict):
                    continue
                passed = cast(dict[str, object], value)
                worker = record.by_id(staff.StaffId(string(passed.get("worker_id"))))
                if worker is None or worker.kind != "worker" or worker.parent != staff.ReportsTo(row.id):
                    continue
                passed["worker_name"] = worker.name
                called = passed.get("called_agent")
                agent = dict(cast(dict[str, object], called)) if isinstance(called, dict) else {}
                backend = record.backends.get(worker.id)
                agent.update(session_id=worker.current_session, family=backend.family if backend is not None else "unknown")
                passed["called_agent"] = agent
    return fields


SCHEMA_VERSION = 1


# The shared history spine, read by scripts/history/history.py. Kept separate
# from SCHEMA_VERSION, which versions this recorder's own fields: a reader has
# to tell "worker changed its events" apart from "the shared format changed",
# and one number cannot say both once other skills write to the same spine.
SPINE_VERSION = 1


SKILL_NAME = "unit-history"


# Where the early reviewer lives between its launch and the ready sentinel that
# releases its real pass. A pass opened before the sentinel would file a verdict
# on a final diff that does not exist yet, so an early launch has nowhere to be
# recorded and the stage table showed only the implementer -- exactly during the
# window where the reader most needs to see that two agents are running. This
# marker is presentation-only: it opens no pass, and `findings.py` counts pass
# events, so convergence never sees it.
ARMED_REVIEW_KEY = "early_review"


# A phase closed by finish-phase takes no more passes, its plan review's included.
CLOSED_PHASE_RULE = ("A finished phase takes no more passes: run its plan review before its finish-phase "
                     + "(docs/direct/phase_end.md, step 3 before step 5).")


class AgentIdentity(TypedDict):
    family: str
    model: str
    effort: str
    session_id: str


@dataclass(frozen=True)
class KnownMainIdentity:
    """A transcript supplies the director's model and effort."""

    identity: AgentIdentity


@dataclass(frozen=True)
class MainIdentityUnavailable:
    """No readable transcript supplies a model; detection must try its fallback."""


# direct.conf, whose digest a pass records. `CONF_DIR` overrides the
# path so the tests read a fixture rather than the machine's live settings.

# A pass already records the agent identity it resolved to, so the digest covers
# what that identity cannot: the assignment tables that produced it and the
# worker settings the run obeyed. Stamped on every pass_started, it lets a
# later query group passes by the configuration in force, which is what makes a
# comparison survive interleaved runs and a conf edited between them.
CONFIG_DIGEST_LENGTH = 12


def _config_path() -> Path:
    return Path(os.environ.get("CONF_DIR") or "~/.claude/config").expanduser() / "direct.conf"


DEFAULT_MODELS_CONFIG_PATH = Path("~/.claude/config/models.conf").expanduser()


def inherited_registry_text(encoded: str) -> str:
    """Read the exact inherited registry text without opening a publication path."""
    match cast(object, json.loads(encoded)):
        case [str(), str() as text]:
            return text
        case _:
            raise ValueError("invalid model configuration; start a new operation")


@cache
def config_digest() -> str:
    """Hash the launch registry and direct settings once per process.

    Return empty text when either source is unavailable. Telemetry must not stop a run.
    """
    try:
        encoded = os.environ.get("MODELS_LAUNCH_CONFIGURATION", "")
        text = (inherited_registry_text(encoded) if encoded else conf_reads.read_text(
            Path(os.environ["MODELS_CONFIG_FILE"]) if os.environ.get("MODELS_CONFIG_FILE")
            else DEFAULT_MODELS_CONFIG_PATH))
        registry = text.encode("utf-8")
        conf_reads.record(_config_path().stem)
        contents = registry + _config_path().read_bytes()
    except (OSError, ValueError, UnicodeError):
        return ""
    return hashlib.sha256(contents).hexdigest()[:CONFIG_DIGEST_LENGTH]


PASS_OWNER_ENV = "UNIT_PASS_OWNER"


PASS_OWNER_TOKEN = "launcher"


PASS_OWNERSHIP_RULE = (
    "Pass lifecycle belongs to the launchers. assign.py and the "
    "review.sh it runs record pass_started and pass_finished. findings.py gate "
    "counts these passes for convergence. Manual pass records can falsely stop "
    "a run. Launch the work through assign.sh. Exception: if the unit director "
    "killed a launcher, close its open pass with "
    "'finish-pass --status canceled --orphaned-launcher'."
)


def _launcher_owned() -> bool:
    return (os.environ.get(PASS_OWNER_ENV) or os.environ.get("PLAN_DELEGATE_PASS_OWNER")) == PASS_OWNER_TOKEN


def iso_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds")


def json_object(text: str) -> dict[str, object] | None:
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast(dict[str, object], parsed)


def object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def string(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


def integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    return default


def arg_string(args: argparse.Namespace, name: str, default: str = "") -> str:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    return string(value, default)


def arg_integer(args: argparse.Namespace, name: str, default: int = 0) -> int:
    value: object = getattr(args, name)  # pyright: ignore[reportAny]
    return integer(value, default)


def argument_session_dir(args: argparse.Namespace) -> Path:
    value = arg_string(args, "session_dir")
    if not value:
        raise SystemExit("--session-dir is required")
    return Path(value).expanduser().resolve()


def state_path(session_dir: Path) -> Path:
    return session_dir / recorder_state.STATE_FILENAME


def refuse_old_state(session_dir: Path) -> None:
    """Check the old shape and leave other read errors to the caller."""
    with suppress(OSError, ValueError):
        _ = recorder_state.read_state(state_path(session_dir))


def recorder_lock_path(session_dir: Path) -> Path:
    path = session_dir / ".progress_history.lock"
    if not path.exists():
        refuse_old_state(session_dir)
    return path


def _stored_fields(fields: dict[str, object]) -> dict[str, object]:
    """Keep plan paths relative to the recorded folder."""
    stored = dict(fields)
    directory = string(fields.get("working_dir"))
    for key in ("plan_doc", "project_plan_doc"):
        value = fields.get(key)
        if directory and isinstance(value, str) and value.startswith(directory + "/"):
            stored[key] = str(Path(value).relative_to(directory))
    return stored


def read_state(session_dir: Path, *, record: staff.StaffSnapshot | None = None,
                resolve_staff: bool = True, pass_agents: bool = True) -> dict[str, object]:
    path = state_path(session_dir)
    try:
        state = recorder_state.read_state(path)
    except OSError as error:
        raise SystemExit(f"Unable to read progress state {path}: {error}") from error
    except ValueError as error:
        raise SystemExit(f"Invalid progress state: {path}") from error
    if not resolve_staff:
        return state
    return staff_directory(state, staff.initial_snapshot() if record is None else record,
                                        pass_agents=pass_agents)


def write_state(session_dir: Path, state: dict[str, object]) -> None:
    stored = _stored_fields(state)
    if state.get(unit_runs.OWNER_FIELD):
        for key in ("production", "unit", "branch", "working_dir", "worktree", "owner_session", "director_session"):
            _ = stored.pop(key, None)
        stored["main_agent"] = {key: value for key, value in (object_dict(state.get("main_agent")) or {}).items()
                                if key not in ("family", "session_id")}
    stored["pass"] = {slot: {**{key: value for key, value in passed.items()
                               if key != "worker_name" or not passed.get("worker_id")}, **({
        "called_agent": {key: value for key, value in (object_dict(passed.get("called_agent")) or {}).items()
                         if key not in ("family", "session_id")}
    } if passed.get("worker_id") else {})} for slot, passed in pass_slots(state).items()}
    write_text_atomic(
        state_path(session_dir), json.dumps(stored, indent=2, sort_keys=True) + "\n",
    )


def append_event(state: dict[str, object], event: dict[str, object]) -> None:
    history_file_value = string(state.get("history_file"))
    if not history_file_value:
        raise SystemExit("Progress state has no history_file")
    history_file = Path(history_file_value)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_stored_fields(event), separators=(",", ":"), sort_keys=True) + "\n"
    with history_file.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _ = handle.write(line)
        handle.flush()
        # Disposable fixtures may omit disk durability; production syncs by default.
        if (os.environ.get("UNIT_SYNC_WRITES") or os.environ.get("PLAN_DELEGATE_SYNC_WRITES", "1")) != "0":
            os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _main_identity_from_codex(session_id: str) -> KnownMainIdentity | MainIdentityUnavailable:
    session_root = Path.home() / ".codex" / "sessions"
    matches = list(session_root.rglob(f"*{session_id}.jsonl"))
    if not matches:
        return MainIdentityUnavailable()
    model = ""
    effort = ""
    try:
        with matches[0].open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                event = json_object(line)
                if event is None or event.get("type") != "turn_context":
                    continue
                payload = object_dict(event.get("payload"))
                if payload is None:
                    continue
                candidate_model = string(payload.get("model"))
                candidate_effort = string(payload.get("effort"))
                if candidate_model:
                    model = candidate_model
                if candidate_effort:
                    effort = candidate_effort
    except OSError:
        return MainIdentityUnavailable()
    if not model:
        return MainIdentityUnavailable()
    return KnownMainIdentity(AgentIdentity(
        family="codex",
        model=model,
        effort=effort or "unset",
        session_id=session_id,
    ))


def _main_identity_from_claude(session_id: str) -> KnownMainIdentity | MainIdentityUnavailable:
    project_root = Path.home() / ".claude" / "projects"
    matches = list(project_root.rglob(f"{session_id}.jsonl"))
    if not matches:
        return MainIdentityUnavailable()
    model = ""
    effort = ""
    try:
        with matches[0].open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                event = json_object(line)
                if event is None or event.get("type") != "assistant":
                    continue
                message = object_dict(event.get("message"))
                candidate_model = string(message.get("model")) if message else ""
                candidate_effort = string(event.get("effort"))
                if candidate_model:
                    model = candidate_model
                if candidate_effort:
                    effort = candidate_effort
    except OSError:
        return MainIdentityUnavailable()
    if not model:
        return MainIdentityUnavailable()
    return KnownMainIdentity(AgentIdentity(
        family="claude",
        model=model,
        effort=effort or "unset",
        session_id=session_id,
    ))


def detect_main_identity(args: argparse.Namespace) -> AgentIdentity:
    explicit_model = arg_string(args, "main_model")
    explicit_family = arg_string(args, "main_family")
    explicit_effort = arg_string(args, "main_effort", "unset")
    explicit_session = arg_string(args, "main_session_id")
    if explicit_model:
        return AgentIdentity(
            family=explicit_family or "unknown",
            model=explicit_model,
            effort=explicit_effort or "unset",
            session_id=explicit_session,
        )

    codex_session = os.environ.get("CODEX_THREAD_ID", "")
    if codex_session:
        identity = _main_identity_from_codex(codex_session)
        if isinstance(identity, KnownMainIdentity):
            return identity.identity

    claude_session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if claude_session:
        identity = _main_identity_from_claude(claude_session)
        if isinstance(identity, KnownMainIdentity):
            return identity.identity

    return AgentIdentity(
        family=explicit_family or "unknown",
        model="unknown",
        effort=explicit_effort or "unset",
        session_id=explicit_session or codex_session or claude_session,
    )


def refresh_main_identity(session_dir: Path, state: dict[str, object]) -> None:
    """Refresh model and effort from the director's current transcript.

    Staff supplies the director for a staffed run.
    An unstaffed helper keeps its recorded session.
    Unknown detection preserves the recorded settings.
    """
    detected = detect_main_identity(
        argparse.Namespace(
            main_model="",
            main_family="",
            main_effort="",
            main_session_id="",
        )
    )
    if detected["model"] == "unknown":
        return
    owner = unit_runs.director(session_dir)
    if not owner and not state.get(unit_runs.OWNER_FIELD):
        owner = string((object_dict(state.get("main_agent")) or {}).get("session_id"))
    if owner and detected["session_id"] != owner:
        return
    state["main_agent"] = detected


# assign.py exports the worker slot before it records a pass.
# Each launcher records its pass under that slot.
# The slot stays fixed when the worker's role changes on the board.
TEAM_SLOT_ENV = "UNIT_TEAM_ROLE"


def _team_slot() -> str:
    """Return the recording worker's slot.

    Earlier passes and recorders that omit a slot use the empty slot.
    Keep them separate from a launcher that records the `impl` slot.
    """
    return os.environ.get(TEAM_SLOT_ENV) or os.environ.get("PLAN_DELEGATE_TEAM_ROLE", "")


def record_slot(record: dict[str, object]) -> str:
    """The worker stamped on a pass record, under whichever key wrote it.

    The field was `team_role` for a day, until the value turned out to name the
    worker rather than the work. A run in flight across that change holds the old
    key on a pass it has already opened, and must not lose its worker halfway
    through.
    """
    if "team_slot" in record:
        return string(record.get("team_slot"))
    return string(record.get("team_role"))


def pass_slots(state: dict[str, object]) -> dict[str, dict[str, object]]:
    """Every pass this phase has recorded, keyed by the slot that opened it.

    Returns the records state itself holds, so a caller that changes one changes
    state. State written before the phase team existed holds a single pass
    object instead of a map; it reads as the entry for its own worker,
    which is how a run already in flight keeps its open pass. Migration happens
    in memory and reaches disk only when something else writes state -- a run is
    never rewritten for the change alone.
    """
    raw = object_dict(state.get("pass"))
    slots: dict[str, dict[str, object]] = {}
    if raw is not None and "instance_id" in raw:
        slots[record_slot(raw)] = raw
    elif raw is not None:
        for slot, value in raw.items():
            record = object_dict(value)
            if record is not None:
                slots[slot] = record
    state["pass"] = slots
    return slots


def open_passes(state: dict[str, object]) -> dict[str, dict[str, object]]:
    """The slots whose pass is still running, keyed the same way."""
    return {
        slot: record
        for slot, record in pass_slots(state).items()
        if string(record.get("status")) in ("active", "refused")
    }


def worker_launcher(command: list[str], name: str) -> list[str]:
    """Resolve saved target arguments through the current worker name."""
    result = list(command)
    for index, part in enumerate(command):
        if part in ("--to", "--new-worker") and index + 1 < len(command):
            result[index:index + 2] = ["--to", name]
        elif part.startswith(("--to=", "--new-worker=")):
            result[index] = "--to=" + name
    return result


def pass_phase_instance(record: dict[str, object]) -> str:
    """The instance id of the phase a pass was opened in; empty for a pass older than the stamp."""
    phase = object_dict(record.get("phase"))
    return string(phase.get("instance_id")) if phase is not None else ""


def phase_stamp(phase: dict[str, object]) -> dict[str, object]:
    """The fields a pass carries to name the phase it was opened in."""
    return {
        "instance_id": string(phase.get("instance_id")),
        "id": string(phase.get("id")),
        "title": string(phase.get("title")),
        "started_at": number(phase.get("started_at")),
    }


def open_phase_named(state: dict[str, object], phase_id: str) -> dict[str, object] | None:
    return next((phase for phase in recorder_state.open_phases(state) if phase.get("id") == phase_id), None)


def optional_arg(args: argparse.Namespace, name: str) -> str:
    """An option callers added after the recorder's own internal calls were written."""
    value: object = getattr(args, name, "")
    return value if isinstance(value, str) else ""


def phase_passes(state: dict[str, object], instance_id: str) -> dict[str, dict[str, object]]:
    return {slot: passed for slot, passed in open_passes(state).items()
            if pass_phase_instance(passed) == instance_id}


def reporting_pass(state: dict[str, object], phase: dict[str, object]) -> dict[str, object] | None:
    """Select the caller's pass or the first open pass in this phase."""
    opened = phase_passes(state, string(phase.get("instance_id")))
    own = opened.get(_team_slot())
    if own is not None:
        return own
    return min(opened.values(), key=lambda passed: (number(passed.get("started_at")), record_slot(passed)),
               default=None)


def record_event(
    state: dict[str, object],
    event_type: str,
    now: float,
    pass_record: dict[str, object] | None = None,
    phase_record: dict[str, object] | None = None,
    activity_record: dict[str, object] | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "spine_version": SPINE_VERSION,
        "skill": SKILL_NAME,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "timestamp": iso_time(now),
        "timestamp_epoch": now,
        "run_id": string(state.get("run_id")),
        "working_dir": string(state.get("working_dir")),
        "worktree": string(state.get("worktree")),
        "branch": string(state.get("branch")),
        "plan_doc": string(state.get("plan_doc")),
        "run_started_at": number(state.get("run_started_at")),
        "project_started_at": number(state.get("project_started_at")),
        "project_start_source": string(state.get("project_start_source")),
        "project_plan_doc": string(state.get("project_plan_doc")),
        "main_agent": state.get("main_agent", {}),
    }
    if state.get(unit_runs.OWNER_FIELD):
        event[unit_runs.OWNER_FIELD] = state[unit_runs.OWNER_FIELD]
    current_pass = pass_record
    phase = phase_record
    if phase is None and current_pass is not None:
        phase = object_dict(current_pass.get("phase"))
    if phase is not None:
        event.update(
            {
                "phase_instance_id": string(phase.get("instance_id")),
                "phase_id": string(phase.get("id")),
                "phase_title": string(phase.get("title")),
                "phase_started_at": number(phase.get("started_at")),
            }
        )
    # Only the window that is open right now identifies an event. A finished
    # pass stays in state, so stamping it unconditionally attributed every
    # activity -- and every progress report made during one -- to whichever
    # worker happened to run before it. A pass lifecycle event names its own
    # record instead: several slots are open at once, and the launcher that
    # closes a stale pass is not always the one that opened it.
    if current_pass is not None and string(current_pass.get("status")) in ("active", "refused"):
        event.update(
            {
                "pass_instance_id": string(current_pass.get("instance_id")),
                "pass_kind": string(current_pass.get("kind")),
                "fix_pass": integer(current_pass.get("fix_pass")),
                "pass_activity": string(current_pass.get("activity")),
                "pass_started_at": number(current_pass.get("started_at")),
                "called_agent": current_pass.get("called_agent", {}),
                "called_task": string(current_pass.get("called_task")),
                "worker_name": string(current_pass.get("worker_name")),
                "worker_id": string(current_pass.get("worker_id")),
                "launcher_command": current_pass.get("launcher_command", []),
            }
        )
    activity = activity_record
    if activity is not None and string(activity.get("status")) == "active":
        event.update(
            {
                "activity_instance_id": string(activity.get("instance_id")),
                "activity_label": string(activity.get("label")),
                "activity_text": string(activity.get("activity")),
                "activity_started_at": number(activity.get("started_at")),
            }
        )
    return event


def close_slot_pass(
    session_dir: Path,
    state: dict[str, object],
    slot: str,
    status: str,
    now: float,
) -> bool:
    """Close one slot's open pass, reporting whether there was one to close.

    One slot's and no other's: a launcher that closes a peer's window ends a
    pass whose agent is still working, and the ledger then describes a phase
    that never happened.
    """
    current_pass = open_passes(state).get(slot)
    if current_pass is None:
        return False
    event = record_event(state, "pass_finished", now, current_pass)
    event["status"] = status
    event["reply_file"] = dispatch_delivery(current_pass).reply_file
    # Taken from the pass, not from the environment. A stale pass is closed by
    # whichever launcher starts the next one in that worker, and reading the
    # environment here would stamp the closer's worker onto the opener's work.
    event["team_slot"] = record_slot(current_pass)
    event["pass_elapsed_seconds"] = max(
        0,
        int(now - number(current_pass.get("started_at"), now)),
    )
    append_event(state, event)
    current_pass["status"] = status
    current_pass["finished_at"] = now
    write_state(session_dir, state)
    return True


class LedgerRepairCause(TypedDict):
    """The repair batch that still authorizes work before closure review finishes."""

    round: int
    covered: list[str]


class LaunchCheck(TypedDict):
    """A launch checked against its open phase and prompt before any pass starts."""

    phase_id: str
    phase_title: str
    prompt_phase_line: Literal["matched", "absent"]
    fix_cause: str | LedgerRepairCause


def _launch_refused(
    state: dict[str, object], args: argparse.Namespace, reason: str,
) -> NoReturn:
    event = record_event(state, "launch_refused", now_epoch(), phase_record={})
    event.update(reason=reason, pass_kind=arg_string(args, "pass_kind"),
                 phase_id=optional_arg(args, "phase_id"), prompt_file=optional_arg(args, "prompt_file"),
                 lens=optional_arg(args, "lens"))
    append_event(state, event)
    raise SystemExit(reason)


def _launch_check(
    args: argparse.Namespace, state: dict[str, object], phase: dict[str, object], session_dir: Path,
) -> LaunchCheck:
    """Check prompt identity and a fix's cause before changing any open pass."""
    prompt_phase_line: Literal["matched", "absent"] = "absent"
    prompt_file = optional_arg(args, "prompt_file")
    if prompt_file:
        try:
            text = Path(prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as error:
            _launch_refused(state, args, f"Unable to read prompt file {prompt_file}: {error}")
        for line in text.splitlines():
            named = re.match(r"^Phase (\S+) — (.*)$", line)
            if named is None:
                continue
            if named.group(1) != string(phase.get("id")) or named.group(2).strip() != string(phase.get("title")).strip():
                _launch_refused(state, args, f"Stale prompt phase: {line}; open phase is Phase {phase.get('id')} — {phase.get('title')}")
            prompt_phase_line = "matched"
            break
    fix_cause: str | LedgerRepairCause = optional_arg(args, "fix_cause").strip()
    if arg_string(args, "pass_kind") == "fix":
        instance = string(phase.get("instance_id"))
        for path in (session_dir / "findings_state.json", session_dir / f"findings_state-{instance}.json"):
            try:
                ledger = json_object(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except OSError as error:
                _launch_refused(state, args, f"Unable to read findings ledger {path}: {error}")
            if ledger is None:
                _launch_refused(state, args, f"Invalid findings ledger: {path}")
            if string(ledger.get("phase_instance_id")) != instance:
                continue
            rounds: object = ledger.get("rounds")
            if isinstance(rounds, list) and rounds:
                repair = object_dict(cast(list[object], rounds)[-1])
                if repair is not None:
                    covered = string_list(repair.get("covered"))
                    findings = object_dict(ledger.get("findings")) or {}
                    pending = any(
                        string((object_dict(findings.get(finding_id)) or {}).get("state")) == "fixed_pending_review"
                        for finding_id in covered
                    )
                    outcome = string(repair.get("outcome"))
                    if outcome == "in_flight" or (outcome == "landed" and pending):
                        fix_cause = LedgerRepairCause(round=integer(repair.get("round")), covered=covered)
            break
        if not fix_cause:
            _launch_refused(state, args, "Fix launch has no ledger round in flight or awaiting closure; give --fix-cause naming the failed gate or defect")
    return {"phase_id": string(phase.get("id")), "phase_title": string(phase.get("title")),
            "prompt_phase_line": prompt_phase_line, "fix_cause": fix_cause}


def require_launch_admission(args: argparse.Namespace, state: dict[str, object], session_dir: Path) -> None:
    """Check whether the run still permits this slot to launch."""
    if unit_runs.terminal_run(state):
        raise SystemExit(f"{session_dir}: the run has ended dispatch admission")
    helper = string(state.get(unit_runs.PRIVATE_HELPER_SLOT))
    slot = optional_arg(args, "team_slot") if hasattr(args, "team_slot") else _team_slot()
    if helper and slot != helper:
        raise SystemExit(f"{session_dir}: only {helper} can dispatch in this private helper run")


def _checked_launch(
    args: argparse.Namespace, state: dict[str, object], session_dir: Path,
) -> tuple[dict[str, object], LaunchCheck]:
    """Validate a named open phase and its launch without changing any pass."""
    require_launch_admission(args, state, session_dir)
    phase_id = optional_arg(args, "phase_id")
    if not phase_id:
        _ = recorder_state.only_open_phase(state)
        _launch_refused(state, args, "Missing phase: --phase-id is required for a recorded run")
    phase = open_phase_named(state, phase_id)
    if phase is None:
        open_ids = ", ".join(recorder_state.open_phase_ids(state)) or "none"
        _launch_refused(state, args, f"Phase {phase_id} is not open; open phases: {open_ids}. {CLOSED_PHASE_RULE}")
    return phase, _launch_check(args, state, phase, session_dir)


def _check_launch(args: argparse.Namespace) -> None:
    session_dir = argument_session_dir(args)
    try:
        state = read_state(session_dir)
    except staff.StaffUnreadable as error:
        print(f"staff: {arg_string(args, 'pass_kind')}: not opened: {error}", file=sys.stderr)
        raise SystemExit(3) from error
    phase, launch_check = _checked_launch(args, state, session_dir)
    if optional_arg(args, "lens") == "ux":
        event = record_event(state, "launch_checked", now_epoch(), phase_record=phase)
        event.update(launch_check=launch_check, pass_kind=arg_string(args, "pass_kind"),
                     lens="ux", prompt_file=optional_arg(args, "prompt_file"))
        append_event(state, event)


@dataclass(frozen=True)
class MatchedDispatch:
    """The saved dispatch matches the requested assignment."""

    worker_name: str
    instance_id: str


@dataclass(frozen=True)
class ContinueDispatchStage(MatchedDispatch):
    """Submit the saved next stage of the same pass."""


@dataclass(frozen=True)
class NoMatchedDispatch:
    """No saved dispatch matches the requested assignment."""


@dataclass(frozen=True)
class ActiveDispatchPromptConflict:
    """The same assignment names an active pass with different prompt content."""

    worker_name: str
    instance_id: str


class DispatchPromptRefused(RuntimeError):
    """The locked recorder refuses a changed prompt for an active pass."""

    def __init__(self, conflict: ActiveDispatchPromptConflict) -> None:
        self.conflict: ActiveDispatchPromptConflict = conflict
        super().__init__(f"Active pass {conflict.instance_id} has different prompt content")


def dispatch_match(active: dict[str, object] | None, phase: dict[str, object],
                    fields: dict[str, object], launcher_command: list[str], *,
                    dispatch_owned: bool,
                    ) -> MatchedDispatch | NoMatchedDispatch | ActiveDispatchPromptConflict:
    if active is None or pass_phase_instance(active) != string(phase.get("instance_id")):
        return NoMatchedDispatch()
    if active.get("kind", "") != fields.get("kind", ""):
        return NoMatchedDispatch()
    saved_command = active.get("launcher_command")
    current_command: object = (worker_launcher(cast(list[str], saved_command), string(active.get("worker_name")))
                       if isinstance(saved_command, list) and active.get("worker_id") and active.get("worker_name")
                       else cast(object, saved_command))
    same_launcher = bool(launcher_command) and (launcher_command == saved_command or (
        active.get("status") in ("active", "refused") and launcher_command == current_command))
    stage_two = False
    if dispatch_owned and fields.get("prompt_file") != active.get("prompt_file"):
        delivery = dispatch_delivery(active)
        prompt = json.dumps(fields.get("prompt_file"))
        instance = string(active.get("instance_id"))
        stage_two = (same_launcher and active.get("status") in ("active", "refused") and active.get("kind") == "review"
                     and bool(instance and delivery.early_ready) and delivery.completion_id == f"{instance}-2"
                     and delivery.line.startswith("Stage 2: ")
                     and f"Read the unchanged prompt file {prompt}. " in delivery.line)
        if not stage_two:
            return NoMatchedDispatch()
    if dispatch_owned and not stage_two:
        try:
            digest = hashlib.sha256(Path(string(fields.get("prompt_file"))).read_bytes()).hexdigest()
        except OSError:
            return NoMatchedDispatch()
        if active.get("prompt_digest") != digest:
            if active.get("status") == "active":
                return ActiveDispatchPromptConflict(string(active.get("worker_name")), string(active.get("instance_id")))
            return NoMatchedDispatch()
    if launcher_command and saved_command and not same_launcher:
        return NoMatchedDispatch()
    if not all(active.get(key, "") == value for key, value in fields.items()
               if key != "prompt_file" or not stage_two):
        return NoMatchedDispatch()
    if active.get("status") not in ("active", "refused") and not (
            dispatch_owned and active.get("status") == "completed" and same_launcher):
        return NoMatchedDispatch()
    matched = ContinueDispatchStage if stage_two else MatchedDispatch
    return matched(string(active.get("worker_name")), string(active.get("instance_id")))


class VacantDispatchSlot(Enum):
    EMPTY = "the slot has no recorded pass"


@dataclass(frozen=True)
class DispatchPredecessor:
    instance_id: str
    phase_instance_id: str
    completion_id: str


@dataclass(frozen=True)
class NewAssignment:
    """Open a new pass after the selected predecessor."""


@dataclass(frozen=True)
class DispatchSelection:
    slot: str
    phase_id: str
    phase_instance_id: str
    predecessor: DispatchPredecessor | VacantDispatchSlot
    action: MatchedDispatch | NewAssignment


class SelectDispatchAtRecorder(Enum):
    SELECT = "select the pass under the recorder lock"


class DispatchSelectionChanged(ValueError):
    """The slot or phase no longer matches the worker's selection."""


def dispatch_predecessor(passes: dict[str, dict[str, object]], slot: str) -> DispatchPredecessor | VacantDispatchSlot:
    if slot not in passes:
        return VacantDispatchSlot.EMPTY
    passed = passes[slot]
    return DispatchPredecessor(string(passed.get("instance_id")), pass_phase_instance(passed),
                               dispatch_delivery(passed).completion_id)


def validate_dispatch_selection(state: dict[str, object], selection: DispatchSelection,
                                slot: str, phase: str) -> None:
    current_phase = open_phase_named(state, phase)
    if (slot != selection.slot or phase != selection.phase_id
            or string((current_phase or {}).get("instance_id")) != selection.phase_instance_id
            or dispatch_predecessor(pass_slots(state), slot) != selection.predecessor):
        raise DispatchSelectionChanged(f"Slot {slot} or phase {phase} changed during dispatch")


def start_pass(args: argparse.Namespace,
               selection: DispatchSelection | SelectDispatchAtRecorder = SelectDispatchAtRecorder.SELECT) -> str:
    if not _launcher_owned() and not getattr(args, "dispatch_owned", False):
        raise SystemExit(f"start-pass is the launcher's to record. {PASS_OWNERSHIP_RULE}")
    session_dir = argument_session_dir(args)
    pass_kind = arg_string(args, "pass_kind")
    fix_pass = arg_integer(args, "fix_pass")
    called_agent = AgentIdentity(
        family=arg_string(args, "called_family", "unknown"),
        model=arg_string(args, "called_model", "unknown"),
        effort=arg_string(args, "called_effort", "unset") or "unset",
        session_id="",
    )
    team_slot = optional_arg(args, "team_slot") if hasattr(args, "team_slot") else _team_slot()
    membership = staff.initial_snapshot()
    state = read_state(session_dir, record=membership)
    worker = membership.by_id(staff.StaffId(optional_arg(args, "worker_id")))
    inferred_owner = False
    if not optional_arg(args, "worker_id") and (name := optional_arg(args, "worker_name")):
        owner = unit_runs.run_owner(state, membership)
        if isinstance(owner, staff.StaffRow):
            worker = next((row for row in membership.find(kind="worker", parent=owner.id)
                           if name in (row.name, row.id)), None)
    if worker is not None and worker.kind == "worker" and isinstance(worker.parent, staff.ReportsTo):
        args.worker_id = worker.id
        args.worker_name = worker.name
        if not state.get(unit_runs.OWNER_FIELD):
            state[unit_runs.OWNER_FIELD] = worker.parent.id
            inferred_owner = True
    launcher_command = _launcher_arguments(optional_arg(args, "launcher_command"))
    if isinstance(selection, DispatchSelection):
        validate_dispatch_selection(state, selection, team_slot, optional_arg(args, "phase_id"))
    # Name the phase when more than one phase is open.
    phase, launch_check = _checked_launch(args, state, session_dir)
    launcher_pid = arg_integer(args, "launcher_pid", os.getppid())
    early_id = optional_arg(args, "early_review_pass_id")
    dispatch_fields: dict[str, object] = {
        "early_review_pass_id": early_id,
        "kind": pass_kind,
        "fix_pass": fix_pass,
        "called_task": arg_string(args, "called_task"),
        "worker_id": optional_arg(args, "worker_id"),
        "prompt_file": optional_arg(args, "prompt_file"),
        "dispatch_kind": optional_arg(args, "dispatch_kind"),
        "lens": optional_arg(args, "lens"),
    }
    active = pass_slots(state).get(team_slot)
    match = dispatch_match(active, phase, dispatch_fields, launcher_command,
                           dispatch_owned=bool(getattr(args, "dispatch_owned", False)))
    if isinstance(match, ActiveDispatchPromptConflict):
        raise DispatchPromptRefused(match)
    if isinstance(selection, DispatchSelection):
        action = selection.action
        if isinstance(action, MatchedDispatch) and (match != action or active is None
                or active.get("worker_id") != dispatch_fields["worker_id"]):
            raise DispatchSelectionChanged(f"Pass {action.instance_id} changed during dispatch")
        match = action
    status_file = optional_arg(args, "assign_status_file")
    if status_file:
        status_file = str(worker_artifact(Path(status_file), os.environ.get(TEAM_SLOT_ENV, team_slot), team_slot))
    if (active is not None and isinstance(match, MatchedDispatch)
            and (not getattr(args, "print_pass_id", False) or active.get("launcher_pid") == launcher_pid)):
        changed = inferred_owner or active.get("launcher_pid") != launcher_pid
        active["launcher_pid"] = launcher_pid
        if launcher_command and not active.get("launcher_command"):
            active["launcher_command"] = launcher_command
            changed = True
        if (status_file and active.get("status") in ("active", "refused") and not active.get("assign_status_file")
                and not dispatch_delivery(active).line and isinstance(hook_completion(active), NoHookCompletion)):
            active["assign_status_file"] = status_file
            changed = True
        if changed:
            write_state(session_dir, state)
        return match.instance_id
    if early_id:
        armed = object_dict(phase.get(ARMED_REVIEW_KEY))
        if pass_kind != "review" or armed is None or armed.get("pass_instance_id") != early_id:
            raise SystemExit("The open phase has no early review reserved for this pass")
    now = now_epoch()
    owner_id = state.get(unit_runs.OWNER_FIELD)
    # A different dispatch replaces only this slot's pass.
    _ = close_slot_pass(session_dir, state, team_slot, "interrupted", now)
    state = read_state(session_dir, record=membership)
    if owner_id:
        state[unit_runs.OWNER_FIELD] = owner_id
    refresh_main_identity(session_dir, state)
    proposed: object = getattr(args, "new_pass_instance_id", None)
    instance_id = early_id or str(proposed if isinstance(proposed, uuid.UUID) else uuid.uuid4())
    current_pass: dict[str, object] = {
        **dispatch_fields,
        "instance_id": instance_id,
        "activity": arg_string(args, "activity"),
        "started_at": now,
        "called_task": arg_string(args, "called_task"),
        "worker_name": optional_arg(args, "worker_name"),
        "worker_id": optional_arg(args, "worker_id"),
        "launcher_command": launcher_command,
        "launcher_pid": launcher_pid,
        "called_agent": called_agent,
        "team_slot": team_slot,
        "phase": phase_stamp(phase),
        "status": "active",
    }
    if getattr(args, "dispatch_owned", False):
        current_pass["prompt_digest"] = hashlib.sha256(Path(optional_arg(args, "prompt_file")).read_bytes()).hexdigest()
    if status_file:
        current_pass["assign_status_file"] = status_file
    slots = pass_slots(state)
    if previous := slots.get(team_slot):
        retain_replaced_pass(state, previous)
    slots[team_slot] = current_pass
    write_state(session_dir, state)
    worker_id = staff.StaffId(optional_arg(args, "worker_id"))
    if worker_id and pass_kind in staff.WORKER_ROLES:
        role = staff.KnownWorkerRole(pass_kind)
        try:
            row = staff.initial_snapshot().by_id(worker_id)
            if row is None:
                print(f"anomaly: {worker_id}: worker role has no staff row", file=sys.stderr)
            elif row.role != role:
                _ = staff.set_worker_role(worker_id, role)
        except (staff.StaffUnreadable, staff.StaffNotFound) as error:
            print(f"anomaly: {worker_id}: {error}", file=sys.stderr)
    event = record_event(state, "pass_started", now, current_pass)
    event["launch_check"] = launch_check
    event["config_digest"] = config_digest()
    event["team_slot"] = team_slot
    append_event(state, event)
    if status_file:
        write_text_atomic(Path(status_file), "running\n")
    if early_id:
        opened = open_phase_named(state, string(phase.get("id")))
        if opened is not None:
            _ = clear_armed_review(session_dir, state, opened, "adopted", now, passed=current_pass)
    return instance_id


def _start_pass_command(args: argparse.Namespace) -> None:
    instance_id = start_pass(args)
    if getattr(args, "print_pass_id", False):
        print(instance_id)


def _launcher_arguments(text: str) -> list[str]:
    """Read the launcher's saved argument list without shell parsing."""
    if not text:
        return []
    try:
        value = cast(object, json.loads(text))
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid --launcher-command: {error}") from error
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise SystemExit("--launcher-command requires a nonempty JSON list of strings")
    return cast(list[str], value)


@dataclass(frozen=True)
class CompletionActions:
    """The launcher's artifact paths and actions survive submission."""

    summary_file: str = ""
    status_file: str = ""
    report_file: str = ""
    style_directory: str = ""
    end_worker: bool = False
    held_role: str = ""
    resolve_fix: bool = False
    review_pass: int = 1
    finish_phase: str = ""
    finish_run: bool = False


@dataclass(frozen=True)
class DispatchDelivery:
    """The hook uses the exact prompt and destinations saved before submission."""

    line: str
    completion_id: str
    reply_file: str
    early_ready: str = ""
    actions: CompletionActions = CompletionActions()


def dispatch_delivery(passed: dict[str, object]) -> DispatchDelivery:
    fields = object_dict(passed.get("delivery")) or {}
    actions = object_dict(fields.get("actions")) or {}
    return DispatchDelivery(
        line=string(fields.get("line")), completion_id=string(fields.get("completion_id")),
        reply_file=string(fields.get("reply_file")), early_ready=string(fields.get("early_ready")),
        actions=CompletionActions(
            summary_file=string(actions.get("summary_file")),
            status_file=string(passed.get("assign_status_file")) or string(actions.get("status_file")),
            report_file=string(actions.get("report_file")), style_directory=string(actions.get("style_directory")),
            end_worker=actions.get("end_worker") is True, held_role=string(actions.get("held_role")),
            resolve_fix=actions.get("resolve_fix") is True, review_pass=int(number(actions.get("review_pass"), 1)),
            finish_phase=string(actions.get("finish_phase")), finish_run=actions.get("finish_run") is True),
    )


def worker_artifact(path: Path, slot: str, worker: str) -> Path:
    """Keep artifacts with the worker selected for the recorded slot."""
    return path.with_name(worker + path.name.removeprefix(slot)) if path.name.startswith(slot + "_") else path


class StoppedReplyDisposition(StrEnum):
    PENDING = "pending"
    SAVED = "saved"
    ENDED_WITHOUT_DONE = "ended without DONE"


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
    stopped_reply: StoppedReplyDisposition = StoppedReplyDisposition.PENDING
    pending_diagnostics: tuple[str, ...] = ()


class NoHookCompletion(Enum):
    UNRECORDED = "no terminal outcome recorded"


def hook_completion(passed: dict[str, object]) -> HookCompletion | NoHookCompletion:
    fields = object_dict(passed.get("hook_completion"))
    if fields is None:
        return NoHookCompletion.UNRECORDED
    details = fields.get("details", [])
    disposition = next((value for value in StoppedReplyDisposition if value.value == fields.get("stopped_reply")),
                       StoppedReplyDisposition.PENDING)
    return HookCompletion(string(fields.get("outcome")), string(fields.get("reply_file")),
        tuple(str(item) for item in cast(list[object], details)) if isinstance(details, list) else (),
        fields.get("actions_done") is True, fields.get("pane_closed") is True,
        fields.get("role_released") is True, fields.get("finished") is True, fields.get("recording_done") is True,
        string(fields.get("sender_native_id")), disposition, tuple(string_list(fields.get("pending_diagnostics"))))


def retained_passes(state: dict[str, object]) -> dict[str, dict[str, object]]:
    records = object_dict(state.get("finishing_passes")) or {}
    return {instance: passed for instance, value in records.items()
            if (passed := object_dict(value)) is not None}


def retain_replaced_pass(state: dict[str, object], passed: dict[str, object]) -> None:
    """Keep the pass available until its completion actions finish."""
    instance = string(passed.get("instance_id"))
    completion = hook_completion(passed)
    if isinstance(completion, HookCompletion):
        pending = not completion.finished
    else:
        delivery = dispatch_delivery(passed)
        pending = (delivery.actions.finish_run or bool(delivery.line)
                   or (passed.get("dispatch_kind") == "astra" and passed.get("kind") == "astra-reviewer"))
    if instance and pending:
        retained = retained_passes(state)
        retained[instance] = passed
        state["finishing_passes"] = retained


def _finish_pass(args: argparse.Namespace) -> None:
    session_dir = argument_session_dir(args)
    status = arg_string(args, "status")
    orphaned = bool(getattr(args, "orphaned_launcher", False))
    if not _launcher_owned():
        if not orphaned:
            raise SystemExit(f"finish-pass is the launcher's to record. {PASS_OWNERSHIP_RULE}")
        if status != "canceled":
            raise SystemExit(
                "--orphaned-launcher closes the pass of a launcher the unit director "
                + f"killed, so it takes --status canceled, not {status}"
            )
    # The launcher that finishes is the launcher that started, so the slot comes
    # from the environment here rather than from the record. Resolving it any
    # other way would let one member close a peer's pass by being the only one
    # still running.
    slot = _team_slot()
    state = read_state(session_dir)
    if instance_id := optional_arg(args, "pass_instance_id"):
        if open_passes(state).get(slot, {}).get("instance_id") != instance_id:
            return
    closed = close_slot_pass(session_dir, state, slot, status, now_epoch())
    if orphaned and not closed:
        raise SystemExit(
            "No pass is open in this slot, so no launcher was orphaned and "
            + "nothing was recorded"
        )


def clear_armed_review(
    session_dir: Path,
    state: dict[str, object],
    phase: dict[str, object],
    reason: str,
    now: float,
    *,
    passed: dict[str, object] | None = None,
) -> bool:
    armed = object_dict(phase.get(ARMED_REVIEW_KEY))
    if armed is None:
        return False
    phase[ARMED_REVIEW_KEY] = None
    write_state(session_dir, state)
    event = record_event(state, "early_review_disarmed", now, passed, phase_record=phase)
    event.update(
        {
            "early_review_instance_id": string(armed.get("instance_id")),
            "reason": reason,
            "early_review_elapsed_seconds": max(
                0,
                int(now - number(armed.get("started_at"), now)),
            ),
        }
    )
    append_event(state, event)
    return True


def add_identity_options(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--main-family", default="")
    _ = parser.add_argument("--main-model", default="")
    _ = parser.add_argument("--main-effort", default="unset")
    _ = parser.add_argument("--main-session-id", default="")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record and aggregate durable unit progress events."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_launch = subparsers.add_parser("check-launch")
    _ = check_launch.add_argument("--session-dir", required=True)
    _ = check_launch.add_argument("--pass-kind", choices=staff.WORKER_ROLES, required=True)
    _ = check_launch.add_argument("--phase-id", "--phase", default="")
    _ = check_launch.add_argument("--prompt-file", default="")
    _ = check_launch.add_argument("--fix-cause", default="")
    _ = check_launch.add_argument("--lens", default="")
    check_launch.set_defaults(handler=_check_launch)

    start_pass_parser = subparsers.add_parser("start-pass")
    _ = start_pass_parser.add_argument("--session-dir", required=True)
    _ = start_pass_parser.add_argument("--pass-kind", choices=staff.WORKER_ROLES, required=True)
    _ = start_pass_parser.add_argument("--launcher-command", default="", help="JSON list of launcher arguments for recovery")
    _ = start_pass_parser.add_argument("--launcher-pid", type=int, default=os.getppid())
    _ = start_pass_parser.add_argument("--fix-pass", type=int, default=0)
    _ = start_pass_parser.add_argument("--activity", required=True)
    _ = start_pass_parser.add_argument("--called-task", required=True)
    _ = start_pass_parser.add_argument("--worker-name", default="", help="the worker this pass was launched to")
    _ = start_pass_parser.add_argument("--worker-id", default="", help="the worker's staff id")
    _ = start_pass_parser.add_argument("--called-family", required=True)
    _ = start_pass_parser.add_argument("--called-model", required=True)
    _ = start_pass_parser.add_argument("--called-effort", default="unset")
    _ = start_pass_parser.add_argument("--phase-id", "--phase", default="",
                                       help="the open phase this pass belongs to; required for recorded runs")
    _ = start_pass_parser.add_argument("--prompt-file", default="")
    _ = start_pass_parser.add_argument("--fix-cause", default="")
    _ = start_pass_parser.add_argument("--early-review-pass-id", default="")
    _ = start_pass_parser.add_argument("--print-pass-id", action="store_true")
    start_pass_parser.set_defaults(handler=_start_pass_command)

    finish_pass = subparsers.add_parser("finish-pass")
    _ = finish_pass.add_argument("--session-dir", required=True)
    _ = finish_pass.add_argument("--pass-instance-id", default="")
    _ = finish_pass.add_argument(
        "--status",
        choices=("completed", "error", "canceled", "interrupted"),
        required=True,
    )
    _ = finish_pass.add_argument(
        "--orphaned-launcher",
        action="store_true",
        help="close this slot's open pass when the unit director killed its "
        + "launcher (--status canceled only)",
    )
    # Earlier shell launchers passed this option before 2026-10-15 18:00 EDT.
    # Accept and ignore it for those recorded commands.
    _ = finish_pass.add_argument(
        "--agent-awake-seconds",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    finish_pass.set_defaults(handler=_finish_pass)

    return parser


def resolve_run_owner(session_dir: Path, state: dict[str, object], record: staff.StaffSnapshot, *,
                      starting_director: str = "") -> staff.StaffRow | unit_runs.RunOwnerUnrecorded:
    folder = session_dir.resolve()
    owners = [row for row in record.rows if row.kind in ("unit", "showrunner")
              and isinstance(row.run, staff.OpenRun) and row.run.path.resolve() == folder]
    if len(owners) > 1:
        raise ValueError(f"{session_dir}: more than one staff member owns the run")
    saved = unit_runs.run_owner(state, record)
    if owners:
        if isinstance(saved, staff.StaffRow) and saved.id != owners[0].id:
            raise ValueError(f"{session_dir}: saved owner {saved.name} conflicts with the run pointer of {owners[0].name}")
        return owners[0]
    if isinstance(saved, staff.StaffRow) or not starting_director:
        return saved
    caller = record.owner_of(starting_director)
    return caller if caller is not None and caller.kind in ("unit", "showrunner") else saved


def read_run_owner(session_dir: Path, *, starting_director: str = "") -> staff.StaffRow | unit_runs.RunOwnerUnrecorded:
    """Resolve the run owner before either caller writes an artifact."""
    try:
        state = recorder_state.read_state(state_path(session_dir))
    except FileNotFoundError:
        state = {}
    owner = resolve_run_owner(session_dir, state, staff.initial_snapshot(), starting_director=starting_director)
    if isinstance(owner, staff.StaffRow) and owner.kind == "showrunner" and isinstance(
            owner.doc, (staff.LiveDoc, staff.WrappedDoc)):
        if owner.doc.machine != remote.local_machine():
            raise ValueError(f"{owner.name}: the staff document is on {owner.doc.machine}")
    return owner


def require_ordinary_review_run(session_dir: Path) -> None:
    """Refuse the alternate launcher before any artifacts exist."""
    owner = read_run_owner(session_dir)
    if isinstance(owner, staff.StaffRow) and owner.kind == "showrunner" and isinstance(
            owner.doc, (staff.LiveDoc, staff.WrappedDoc)):
        if staff.declares_merge_checkout(owner.doc.path.read_text(encoding="utf-8").splitlines()):
            raise ValueError(f"{owner.name}: enrolled showrunners must use the assigned review launcher")


def main(argv: Sequence[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    handler = cast(Callable[[argparse.Namespace], None], args.handler)
    session_dir = argument_session_dir(args)
    try:
        require_ordinary_review_run(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)
        with recorder_lock_path(session_dir).open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                handler(args)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError, recorder_state.OldRecorderState, staff.StaffRefused) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
