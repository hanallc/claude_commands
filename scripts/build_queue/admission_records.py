"""Shared admission records, configuration and wait phrases."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum, StrEnum
from hashlib import sha256
import json
import math
import os
import re
from threading import Thread
from types import MappingProxyType
from typing import Callable, Literal, NotRequired, TypedDict, assert_never, cast
from zoneinfo import ZoneInfo

import sys
import tempfile
import time
from pathlib import Path


from scripts.build_queue import execution_identity
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import placement, snapshot_codec
from scripts.build_queue.admission_jobs import (GATE_JOB_NOT_RECORDED, VERIFY_VERBS as VERIFY_VERBS,
    GateJobNotRecorded, NotVerifyCall as NotVerifyCall, StepJob, VerifyCall as VerifyCall,
    VerifyStep as VerifyStep, VerifyVerb as VerifyVerb)
from scripts.build_queue.scope_activity import (EmptyBuildScope, IdleService, LiveBuildRun, NoLiveBuildRun,
    NotServiceSlice, RunningServiceJob, ScopeProcesses, ServiceJobState)

from scripts.build_targets import target_config
from scripts.build_targets.target_config import LocalRequired, StepRoute
from scripts.buildlog import parse, store
from scripts.lib import conf
from scripts.lib.atomic_file import sync_folder
from scripts.lib import process_start
from scripts.lib.proc_text import read_proc_text
from scripts.production.staff import StaffId
from scripts.lib.test_phase import (CallRecord as CallRecord, NoCallRecord as NoCallRecord,
                                   TestPhaseChargeUse as TestPhaseChargeUse)

GIB = 1 << 30
DIRECT_CARGO = LocalRequired("a direct cargo command runs where it starts")


class PlacementWaitReason(StrEnum):
    MEMORY = "memory"
    SLOT = "slot"
    FOLDER = "folder"
    ROUTING = "routing"
    HOLD = "hold"
    REMOTE_START = "remote_start"
    AWS_REFUSAL_RETRY = "aws_refusal_retry"
    HANA_RECOVERY = "hana_recovery"


@dataclass(frozen=True)
class NoPlacementWait:
    """The call has no open wait."""


NO_PLACEMENT_WAIT = NoPlacementWait()


@dataclass(frozen=True)
class PlacementWaitKey:
    call_id: str
    episode: str
    attempt: str = ""


@dataclass(frozen=True)
class OpenPlacementWait:
    episode: str
    reason: PlacementWaitReason
    started_at: float
    interval_id: str
    attempt: str = ""


class PlacementWaitRow(TypedDict):
    kind: Literal["placement_wait_s"]
    interval_id: str
    call_id: str
    placement_episode: str
    reason: PlacementWaitReason
    started_at: float
    ended_at: float
    placement_wait_s: float


@dataclass(frozen=True)
class PlacementWaitOwner:
    pid: int
    start_time: str


@dataclass(frozen=True)
class UntrackedWaitOwner:
    """The transition does not change the owner of the call."""


UNTRACKED_WAIT_OWNER = UntrackedWaitOwner()


class PlacementWaitAction(StrEnum):
    REGISTER = "register"
    CONTINUE = "continue"
    ROUTING_IF_IDLE = "routing_if_idle"
    CHANGE = "change"
    CLOSE = "close"
    CLOSE_ATTEMPT = "close_attempt"
    COMMAND = "command"
    STARTED_ATTEMPT = "started_attempt"
    FINISH = "finish"
    FINISH_CONTINUED = "finish_continued"


WAIT_ACTION_ORDER = {action: position for position, action in enumerate(PlacementWaitAction)}


@dataclass(frozen=True)
class PlacementWaitTransition:
    episode: str
    action: PlacementWaitAction
    reason: PlacementWaitReason | NoPlacementWait
    only: PlacementWaitReason | NoPlacementWait
    at: float
    interval_id: str
    observed_at: float
    attempt: str = ""


@dataclass(frozen=True)
class PreparationTimeUnrecorded:
    """The journal lacks a call start or a preparation end."""


@dataclass(frozen=True)
class CallPreparation:
    started_at: float
    ended_at: float


@dataclass(frozen=True)
class PlacementWaitJournal:
    rows: tuple[PlacementWaitRow, ...]
    preparation: CallPreparation | PreparationTimeUnrecorded


@dataclass(frozen=True)
class OwnerLastSeen:
    at: float


class OwnerObservationNotRecorded(Enum):
    MISSING = "missing"


class OrphanWaitEnd(Enum):
    LAST_RECORD = "last_record"


@dataclass(frozen=True)
class WaitOwnerStillTracked:
    """The owner is live or its identity is unknown."""


WAIT_OWNER_STILL_TRACKED = WaitOwnerStillTracked()
WAIT_RECORD_SEPARATOR = b"\x1e"
PLACEMENT_WAIT_DIRECTORY = "placement-waits"
PLACEMENT_WAIT_JOURNAL_SUFFIX = ".journal"


def _placement_wait_path(root: Path, call_id: str) -> Path:
    return root / PLACEMENT_WAIT_DIRECTORY / sha256(call_id.encode()).hexdigest()


def buildlog_record_paths(root: Path) -> Iterator[Path]:
    """Find build log files. The placement journal has its own reader."""
    return (path for path in root.glob("*/*.jsonl") if path.parent.name != PLACEMENT_WAIT_DIRECTORY)


def read_buildlog_records(root: Path) -> Iterator[dict[str, object]]:
    """Read the rows from the build log files."""
    for path in buildlog_record_paths(root):
        for line in path.read_bytes().splitlines():
            yield _wait_object(cast(object, json.loads(line)))


def _placement_wait_owner_path(path: Path, *, continued: bool = False) -> Path:
    suffix = ".continued.owner" if continued else ".owner"
    return path.parent / "active" / path.with_suffix(suffix).name


def verify_run_call_id(pid: int, start_time: str) -> str:
    """Link the preparation command and the verify command to one call."""
    return f"v-run-{pid}-{start_time}"


def _wait_number(value: object) -> float:
    return cast(float, snapshot_codec.parse_snapshot_value(value, float))


def _wait_text(value: object) -> str:
    return cast(str, snapshot_codec.parse_snapshot_value(value, str))


def _wait_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid placement wait record")
    return cast(dict[str, object], value)


def _read_wait_object(data: bytes) -> dict[str, object]:
    return _wait_object(cast(object, json.loads(data)))


def placement_wait_frames(path: Path) -> Iterator[bytes]:
    """Read complete frames from a placement wait journal."""
    with path.open("rb") as source:
        for line in source:
            if line.endswith(b"\n") and line != b"\n":
                # A new frame starts after an incomplete append.
                yield line.rsplit(WAIT_RECORD_SEPARATOR, 1)[-1]


def _call_wait_frames(path: Path) -> Iterator[bytes]:
    for suffix in (".jsonl", PLACEMENT_WAIT_JOURNAL_SUFFIX):
        with suppress(FileNotFoundError):
            yield from placement_wait_frames(path.with_suffix(suffix))


def _wait_interval_id(episode: str, reason: PlacementWaitReason, started_at: float) -> str:
    identity = json.dumps((episode, reason, float(started_at).hex()), separators=(",", ":"))
    return sha256(identity.encode()).hexdigest()


def _decode_open_wait(value: object) -> OpenPlacementWait | NoPlacementWait:
    if value is None:
        return NO_PLACEMENT_WAIT
    fields = _wait_object(value)
    if fields.get("kind") == "NoPlacementWait" or fields.get("reason") == "local_setup":
        return NO_PLACEMENT_WAIT
    episode = _wait_text(fields["episode"])
    reason = PlacementWaitReason(_wait_text(fields["reason"]))
    started_at = _wait_number(fields["started_at"])
    interval_id = _wait_text(fields["interval_id"]) if "interval_id" in fields else _wait_interval_id(episode, reason, started_at)
    return OpenPlacementWait(episode, reason, started_at, interval_id)


def _decode_wait_row(call_id: str, value: object) -> PlacementWaitRow:
    fields = _wait_object(value)
    episode = _wait_text(fields["placement_episode"])
    reason = PlacementWaitReason(_wait_text(fields["reason"]))
    started_at = _wait_number(fields["started_at"])
    interval_id = _wait_text(fields["interval_id"]) if "interval_id" in fields else _wait_interval_id(episode, reason, started_at)
    return {"kind": "placement_wait_s", "call_id": call_id, "interval_id": interval_id,
            "placement_episode": episode, "reason": reason, "started_at": started_at,
            "ended_at": _wait_number(fields["ended_at"]), "placement_wait_s": _wait_number(fields["placement_wait_s"])}


def _transition_order(event: PlacementWaitTransition) -> tuple[float, int, str]:
    return event.at, WAIT_ACTION_ORDER[event.action], event.interval_id


def _decode_transition(fields: dict[str, object]) -> PlacementWaitTransition:
    reason = fields["reason"]
    action_name = _wait_text(fields["action"])
    action = PlacementWaitAction.REGISTER if action_name == "setup" else PlacementWaitAction(action_name)
    only = fields.get("only", reason if action is PlacementWaitAction.CLOSE else None)
    return PlacementWaitTransition(_wait_text(fields["episode"]), action,
        PlacementWaitReason(_wait_text(reason)) if reason not in (None, "local_setup") else NO_PLACEMENT_WAIT,
        PlacementWaitReason(_wait_text(only)) if only not in (None, "local_setup") else NO_PLACEMENT_WAIT,
        _wait_number(fields["at"]), _wait_text(fields["interval_id"]), _wait_number(fields["observed_at"]),
        _wait_text(fields.get("attempt", "")))


def _close_wait(call_id: str, active: OpenPlacementWait, at: float) -> PlacementWaitRow:
    return {"kind": "placement_wait_s", "call_id": call_id, "interval_id": active.interval_id,
            "placement_episode": active.episode, "reason": active.reason, "started_at": active.started_at,
            "ended_at": at, "placement_wait_s": at - active.started_at}


def _wait_rows_order(row: PlacementWaitRow) -> tuple[float, float, str]:
    return row["started_at"], row["ended_at"], row["interval_id"]


def _read_placement_wait_journal(call_id: str) -> PlacementWaitJournal:
    path = _placement_wait_path(store.root(), call_id)
    rows: dict[str, PlacementWaitRow] = {}
    events: dict[str, PlacementWaitTransition] = {}
    active: OpenPlacementWait | NoPlacementWait = NO_PLACEMENT_WAIT
    last_recorded_at = 0.0
    started_at = math.inf
    command_at = math.inf
    finished_at = math.inf
    finished = False
    orphan_ends: dict[bool, OwnerLastSeen | OrphanWaitEnd] = {}
    diagnosed = False

    def note(error: Exception) -> None:
        nonlocal diagnosed
        if not diagnosed:
            print(f"buildlog: invalid placement wait for {call_id}: {error}", file=sys.stderr)
            diagnosed = True

    def add_legacy(value: object) -> None:
        nonlocal last_recorded_at, started_at
        fields = _wait_object(value)
        if fields["reason"] == "local_setup":
            started_at = min(started_at, _wait_number(fields["started_at"]))
            last_recorded_at = max(last_recorded_at, _wait_number(fields["ended_at"]))
        else:
            row = _decode_wait_row(call_id, fields)
            add(row)
            last_recorded_at = max(last_recorded_at, row["ended_at"])

    def add(row: PlacementWaitRow) -> None:
        saved = rows.get(row["interval_id"])
        if saved is None or _wait_rows_order(row) < _wait_rows_order(saved):
            rows[row["interval_id"]] = row

    try:
        older = _read_wait_object(path.with_suffix(".json").read_bytes())
        active = _decode_open_wait(older["active"])
        finished = older["finished"] is True
        if isinstance(active, OpenPlacementWait):
            last_recorded_at = active.started_at
        for value in cast(list[object], older["rows"]):
            try:
                add_legacy(value)
            except (ValueError, TypeError, KeyError) as error:
                note(error)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, KeyError) as error:
        note(error)

    legacy_snapshot = (last_recorded_at, finished, b"")
    for data in _call_wait_frames(path):
        try:
            value = _read_wait_object(data)
            if value.get("kind") == "transition":
                event = _decode_transition(value)
                saved = events.get(event.interval_id)
                if saved is None or event.observed_at > saved.observed_at:
                    events[event.interval_id] = event
            elif value.get("kind") == "orphan":
                detached = value.get("continued") is True
                seen = value["last_seen_at"]
                if seen is not None:
                    end = OwnerLastSeen(_wait_number(seen))
                    previous = orphan_ends.get(detached)
                    if not isinstance(previous, OwnerLastSeen) or end.at > previous.at:
                        orphan_ends[detached] = end
                else:
                    _ = orphan_ends.setdefault(detached, OrphanWaitEnd.LAST_RECORD)
            else:
                snapshot_order = (_wait_number(value["at"]), value["finished"] is True, data)
                legacy_active = _decode_open_wait(value["active"])
                if "closed" in value:
                    add_legacy(value["closed"])
                if snapshot_order > legacy_snapshot:
                    legacy_snapshot = snapshot_order
                    snapshot_at, finished, _ = snapshot_order
                    last_recorded_at = max(last_recorded_at, snapshot_at)
                    active = legacy_active
        except (ValueError, TypeError, KeyError) as error:
            note(error)

    legacy_at = last_recorded_at
    if finished:
        finished_at = legacy_at
    closed_attempts: set[tuple[str, str]] = set()
    late_attempts: set[tuple[str, str]] = set()
    continued = any(event.action in (PlacementWaitAction.CONTINUE, PlacementWaitAction.FINISH_CONTINUED)
                    for event in events.values())
    for event in sorted(events.values(), key=_transition_order):
        if event.action in (PlacementWaitAction.REGISTER, PlacementWaitAction.CONTINUE):
            started_at = min(started_at, event.at)
            last_recorded_at = max(last_recorded_at, event.observed_at)
            continue
        if event.action is PlacementWaitAction.FINISH and continued:
            continue
        if event.at < legacy_at:
            continue
        attempt = (event.episode, event.attempt)
        closes_attempt = event.action in (PlacementWaitAction.CLOSE_ATTEMPT, PlacementWaitAction.STARTED_ATTEMPT)
        if attempt in closed_attempts:
            if not closes_attempt and attempt not in late_attempts:
                identity = f"{call_id}/{event.episode}/{event.attempt}"
                print(f"buildlog: ignored placement wait after attempt close for {identity}", file=sys.stderr)
                late_attempts.add(attempt)
            continue
        if finished:
            continue
        if closes_attempt:
            closed_attempts.add(attempt)

        last_recorded_at = max(last_recorded_at, event.observed_at)
        if event.action in (PlacementWaitAction.COMMAND, PlacementWaitAction.STARTED_ATTEMPT):
            command_at = min(command_at, event.at)
        if isinstance(event.only, PlacementWaitReason) and (
                not isinstance(active, OpenPlacementWait) or active.reason is not event.only):
            continue
        if event.action is PlacementWaitAction.ROUTING_IF_IDLE and isinstance(active, OpenPlacementWait):
            continue
        if closes_attempt or event.action is PlacementWaitAction.CLOSE:
            if not isinstance(active, OpenPlacementWait) or active.episode != event.episode:
                continue
            if active.attempt != event.attempt and (not closes_attempt or active.attempt):
                continue
        if isinstance(active, OpenPlacementWait):
            if active.episode == event.episode and active.reason is event.reason:
                if active.attempt == event.attempt:
                    continue
                if not active.attempt:
                    active = replace(active, attempt=event.attempt)
                    continue
            add(_close_wait(call_id, active, event.at))
        active = NO_PLACEMENT_WAIT
        if event.action in (PlacementWaitAction.FINISH, PlacementWaitAction.FINISH_CONTINUED):
            finished = True
            finished_at = event.at
        elif isinstance(event.reason, PlacementWaitReason):
            active = OpenPlacementWait(event.episode, event.reason, event.at, event.interval_id, event.attempt)

    orphan_end = orphan_ends.get(continued)
    if not finished and orphan_end is not None:
        finished_at = orphan_end.at if isinstance(orphan_end, OwnerLastSeen) else last_recorded_at
        if math.isfinite(command_at):
            finished_at = max(finished_at, command_at)
        if isinstance(active, OpenPlacementWait):
            add(_close_wait(call_id, active, max(finished_at, active.started_at)))
    ended_at = min(command_at, finished_at)
    preparation = (CallPreparation(started_at, max(started_at, ended_at))
                   if math.isfinite(started_at) and math.isfinite(ended_at) else PreparationTimeUnrecorded())
    return PlacementWaitJournal(tuple(sorted(rows.values(), key=_wait_rows_order)), preparation)


def read_placement_waits(call_id: str) -> tuple[PlacementWaitRow, ...]:
    """Read complete named waits in time order."""
    return _read_placement_wait_journal(call_id).rows


def placement_wait_totals(call_id: str) -> dict[str, float]:
    """Report the named waits and remaining time before the first command."""
    journal = _read_placement_wait_journal(call_id)
    totals: dict[str, float] = {}
    preparation = journal.preparation
    if isinstance(preparation, PreparationTimeUnrecorded):
        for row in journal.rows:
            reason = row["reason"]
            totals[reason] = totals.get(reason, 0.0) + row["placement_wait_s"]
        return totals

    cursor = preparation.started_at
    for row in journal.rows:
        started = max(cursor, row["started_at"])
        ended = min(preparation.ended_at, row["ended_at"])
        if ended > started:
            reason = row["reason"]
            totals[reason] = totals.get(reason, 0.0) + (ended - started)
            cursor = ended
    totals["local_setup"] = (preparation.ended_at - preparation.started_at) - sum(totals.values())
    return totals


def _append_wait_event(path: Path, value: dict[str, object], *, durable: bool = False) -> None:
    data = WAIT_RECORD_SEPARATOR + json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix(PLACEMENT_WAIT_JOURNAL_SUFFIX), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.write(descriptor, data) != len(data):
            raise OSError("short placement wait write")
        if durable:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if durable:
        sync_folder(path.parent)
        sync_folder(path.parent.parent)


def _record_wait_transition(path: Path, episode: str, action: PlacementWaitAction,
                            reason: PlacementWaitReason | NoPlacementWait, at: float, observed_at: float,
                            *, only: PlacementWaitReason | NoPlacementWait = NO_PLACEMENT_WAIT,
                            attempt: str = "", durable: bool = False) -> None:
    cause = reason if isinstance(reason, PlacementWaitReason) else None
    required = only if isinstance(only, PlacementWaitReason) else None
    identity = json.dumps((episode, attempt, action, cause, required, float(at).hex()), separators=(",", ":"))
    interval_id = sha256(identity.encode()).hexdigest()
    _append_wait_event(path, {"kind": "transition", "episode": episode, "action": action, "reason": cause,
                              "only": required, "at": at, "observed_at": observed_at, "interval_id": interval_id,
                              "attempt": attempt}, durable=durable)


def append_placement_wait(key: PlacementWaitKey, reason: PlacementWaitReason | NoPlacementWait,
                          at: float, *, root: Path, begin_routing: bool = False,
                          only: PlacementWaitReason | NoPlacementWait = NO_PLACEMENT_WAIT,
                          command_started: bool = False) -> None:
    """Append a wait transition and report write errors to the caller."""
    if not key.call_id:
        return
    action = PlacementWaitAction.CHANGE
    if isinstance(reason, NoPlacementWait):
        if command_started:
            action = PlacementWaitAction.STARTED_ATTEMPT
        else:
            action = PlacementWaitAction.CLOSE_ATTEMPT if key.attempt else PlacementWaitAction.CLOSE
    elif begin_routing:
        action = PlacementWaitAction.ROUTING_IF_IDLE
    _record_wait_transition(_placement_wait_path(root, key.call_id), key.episode, action, reason, at, at,
                            only=only, attempt=key.attempt)


def record_placement_wait(key: PlacementWaitKey, reason: PlacementWaitReason | NoPlacementWait,
                          at: float, *, root: Path | None = None, begin_routing: bool = False,
                          only: PlacementWaitReason | NoPlacementWait = NO_PLACEMENT_WAIT) -> None:
    """Record a wait transition at the time it occurs."""
    if not key.call_id:
        return
    try:
        append_placement_wait(key, reason, at, root=store.root() if root is None else root,
                              begin_routing=begin_routing, only=only)
    except (OSError, ValueError) as error:
        print(f"buildlog: cannot record placement wait for {key.call_id}: {error}", file=sys.stderr)


def register_placement_waits(call_id: str, owner: PlacementWaitOwner | UntrackedWaitOwner,
                          started_at: float, *, entered_at: float,
                          owner_seen: OwnerLastSeen | OwnerObservationNotRecorded = OwnerObservationNotRecorded.MISSING,
                          continued: bool = False) -> None:
    """Record the original start or transfer the call to its detached owner."""
    path = _placement_wait_path(store.root(), call_id)
    try:
        if isinstance(owner, PlacementWaitOwner):
            owner_path = _placement_wait_owner_path(path, continued=continued)
            owner_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=owner_path.parent, prefix=f".wait-owner.{os.getpid()}.",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    _ = stream.write(json.dumps({"call_id": call_id, "pid": owner.pid, "start_time": owner.start_time,
                        "seen_at": owner_seen.at if isinstance(owner_seen, OwnerLastSeen) else None}))
                    stream.close()
                    os.replace(temporary, owner_path)
                finally:
                    temporary.unlink(missing_ok=True)
        action = PlacementWaitAction.CONTINUE if continued else PlacementWaitAction.REGISTER
        _record_wait_transition(path, "", action, NO_PLACEMENT_WAIT, started_at, entered_at)
    except (OSError, ValueError) as error:
        print(f"buildlog: cannot start placement waits for {call_id}: {error}", file=sys.stderr)


def stamp_verify_command(call_id: str, at: float, observed_at: float) -> None:
    """Try to write the start of a command that ran."""
    try:
        path = _placement_wait_path(store.root(), call_id)
        _record_wait_transition(path, "", PlacementWaitAction.COMMAND, NO_PLACEMENT_WAIT, at, observed_at, durable=True)
    except (OSError, ValueError) as error:
        with suppress(OSError, ValueError):
            message = f"buildlog: cannot record command start for {call_id}: {error}\n"
            _ = os.write(sys.stderr.fileno(), message.encode(errors="replace"))


def record_started_verify_command(call_id: str, pid: int) -> None:
    """Read the start time before reaping the child and write it in a daemon thread."""
    observed_at = time.time()
    at = observed_at
    if hasattr(time, "CLOCK_BOOTTIME"):
        identity = process_start.process_start(pid)
        if not isinstance(identity, (process_start.Started, process_start.Zombie)):
            return
        try:
            uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
            at = observed_at - uptime + int(identity.start) / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError):
            return
    with suppress(RuntimeError):
        Thread(target=stamp_verify_command, args=(call_id, at, observed_at), daemon=True).start()


def finish_placement_waits(call_id: str, at: float, *, continued: bool = False) -> None:
    """Record the time the call ends."""
    try:
        path = _placement_wait_path(store.root(), call_id)
        action = PlacementWaitAction.FINISH_CONTINUED if continued else PlacementWaitAction.FINISH
        _record_wait_transition(path, "", action, NO_PLACEMENT_WAIT, at, at)
        _placement_wait_owner_path(path, continued=continued).unlink(missing_ok=True)
        if continued:
            _placement_wait_owner_path(path).unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        print(f"buildlog: cannot finish placement waits for {call_id}: {error}", file=sys.stderr)


def _read_wait_owner(path: Path) -> tuple[str, PlacementWaitOwner, OwnerLastSeen | OwnerObservationNotRecorded]:
    value = _read_wait_object(path.read_bytes())
    seen = value.get("seen_at")
    return _wait_text(value["call_id"]), PlacementWaitOwner(
        cast(int, snapshot_codec.parse_snapshot_value(value["pid"], int)), _wait_text(value["start_time"])), (
        OwnerLastSeen(_wait_number(seen)) if seen is not None else OwnerObservationNotRecorded.MISSING)


def _remove_orphaned_owner_temps(directory: Path) -> None:
    for path in directory.glob(".wait-owner.*.tmp"):
        match = re.fullmatch(r"\.wait-owner\.([1-9][0-9]*)\.[^.]+\.tmp", path.name)
        if match is None:
            continue
        try:
            try:
                os.kill(int(match[1]), 0)
            except ProcessLookupError:
                path.unlink(missing_ok=True)
        except (OSError, OverflowError):
            continue


def close_orphaned_placement_waits(root: Path,
                                  observe: Callable[[PlacementWaitOwner, OwnerLastSeen | OwnerObservationNotRecorded],
                                      OwnerLastSeen | OrphanWaitEnd | WaitOwnerStillTracked]) -> set[PlacementWaitOwner]:
    """Observe each registered owner and record the end of each orphan."""
    owners: set[PlacementWaitOwner] = set()
    directory = root / "placement-waits/active"
    _remove_orphaned_owner_temps(directory)
    for path in directory.glob("*.owner"):
        try:
            call_id, owner, registered = _read_wait_owner(path)
            journal = _placement_wait_path(root, call_id)
            continued = path == _placement_wait_owner_path(journal, continued=True)
            if not continued and _placement_wait_owner_path(journal, continued=True).exists():
                path.unlink(missing_ok=True)
                continue
            owners.add(owner)
            end = observe(owner, registered)
            if not isinstance(end, WaitOwnerStillTracked):
                at = end.at if isinstance(end, OwnerLastSeen) else None
                identity = json.dumps((owner.pid, owner.start_time, at), separators=(",", ":"))
                _append_wait_event(journal, {"kind": "orphan", "last_seen_at": at, "continued": continued,
                    "interval_id": sha256(identity.encode()).hexdigest()})
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as error:
            print(f"buildlog: cannot check placement wait at {path}: {error}", file=sys.stderr)
    return owners


@dataclass(frozen=True)
class UnregisteredTarget:
    """The target name has no registration."""


def effective_target(name: str, settings: placement.ProviderSettings, *,
                     configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                     ) -> target_config.Target | UnregisteredTarget:
    """Select the registered target with the settings supplied by the request."""
    for target in target_config.read_targets(selected=name, purpose=target_config.RegistryRead.PLACEMENT,
                                              configuration=configuration):
        return ssh_target_settings(target, settings) if isinstance(target, target_config.SshTarget) else target
    return UnregisteredTarget()


def ssh_target_settings(target: target_config.SshTarget, settings: placement.ProviderSettings) -> target_config.SshTarget:
    """Apply the settings supplied by the request to an SSH target."""
    if isinstance(settings, placement.ExplicitProviderSettings):
        return replace(target, settings=settings.path)
    return target


@dataclass(frozen=True)
class RefusalSettings:
    """The digest identifies the settings read immediately before a prelaunch refusal."""
    digest: str


@dataclass(frozen=True)
class NoRefusalSettings:
    """The report supplies no settings digest."""


@dataclass(frozen=True)
class RemoteStartHoldUnrecorded:
    """The assignment has no recorded hold for a remote start."""


@dataclass(frozen=True)
class RemoteStartHold:
    """Keep the local place until this deadline while a remote start is pending."""

    deadline: float


@dataclass(frozen=True)
class RemoteStartHoldEnded:
    """The remote assignment no longer keeps a place on natedev."""


@dataclass(frozen=True)
class SavedPlacementAssignment:
    """The saved assignment holds its settings generation and dispatch state."""
    assignment: placement.PlacementAssignment
    state: placement.AssignmentState = placement.CurrentAssignment()
    permission: placement.LaunchPermission = placement.PermissionAvailable()
    provider: placement.ProviderClaimReference | placement.NoProviderClaim = placement.NoProviderClaim()
    holder: placement.ClaimHolderIdentity | placement.ClaimHolderUnconfirmed = placement.ClaimHolderUnconfirmed("not consumed")
    retry: placement.RetryDecision = placement.DoNotRetry("attempt is not settled")
    terminal_report: placement.TerminalAttemptReport | placement.NoTerminalReport = placement.NoTerminalReport()
    assigned_settings_generation: placement.AssignedSettingsGeneration | placement.UnknownAssignedSettingsGeneration = (
        placement.UnknownAssignedSettingsGeneration())
    refusal_settings: RefusalSettings | NoRefusalSettings = NoRefusalSettings()
    remote_start_hold: RemoteStartHold | RemoteStartHoldUnrecorded | RemoteStartHoldEnded = RemoteStartHoldUnrecorded()

# Historical multiplier for saved dev bases from the Phase 24 replay.
DEV_HEADROOM = 4.2034275337646125

TestScope = Literal["full", "filtered"]


@dataclass(frozen=True)
class AdmissionTiming:
    """The timing limits control admission starts and estimate updates."""
    recovery_start_seconds: int
    estimate_refresh_seconds: int
    slot_gap_seconds: int
    fresh_test_seconds: float


@dataclass(frozen=True)
class PressurePolicy:
    """The limits define when memory pressure delays admission."""
    full_avg10_limit: float
    some_avg10_limit: float


@dataclass(frozen=True)
class PeakHold:
    """The durations define how long each step kind retains its peak charge."""
    default_seconds: int
    by_kind: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))

    def seconds_for(self, kind: str) -> int:
        return self.by_kind.get(kind, self.default_seconds)


@dataclass(frozen=True)
class GpuTestSlots:
    """Limit the local test steps of GPU repositories that run at once on this host."""
    slots: int
    repositories: frozenset[str]

    def holds(self, repository: str, step: str) -> bool:
        return repository in self.repositories and step in GPU_TEST_STEPS


# These steps run test binaries; a GPU repository's tests open render apps.
GPU_TEST_STEPS = frozenset({"nextest", "test", "t", "targeted-test", "merge-gate"})
NO_GPU_TESTS = GpuTestSlots(1, frozenset())


@dataclass(frozen=True)
class AdmissionPolicy:
    """The policy holds memory, pressure, timing, and GPU limits for admission."""
    timing: AdmissionTiming
    pressure: PressurePolicy
    peak_hold: PeakHold
    safety_bytes: int = 3 * GIB
    progress_lookahead_percent: float = field(kw_only=True)
    agent_swap_share_percent: int = field(default=100, kw_only=True)
    gpu_tests: GpuTestSlots = field(default=NO_GPU_TESTS, kw_only=True)


@dataclass(frozen=True)
class RepositoryIdentity:
    """The identity holds the repository key and current and legacy names."""
    key: str
    name: str
    legacy_name: str = ""


@dataclass(frozen=True)
class UnknownRepository:
    """The repository identity is unknown."""

Repository = RepositoryIdentity | UnknownRepository

LegacyNeedSource = Literal["measured", "buildlog", "pooled", "fallback"]

NeedSource = str


@dataclass(frozen=True)
class NoFloor:
    """No failed peak sets a minimum memory estimate."""


@dataclass(frozen=True)
class FailedPeakFloor:
    """The failed run supplies a minimum peak and its end time."""
    peak: int
    ended_at: str


@dataclass(frozen=True)
class NoHeadroom:
    """The memory estimate has no added headroom."""


@dataclass(frozen=True)
class ReleaseHeadroom:
    """The factor adds headroom to a release memory estimate."""
    factor: float


@dataclass(frozen=True)
class LegacyDevMultiplierHeadroom:
    """The multiplier recorded before the dev size rule."""

    factor: float = DEV_HEADROOM


@dataclass(frozen=True)
class DevSizeHeadroom:
    """The factor and byte margin add headroom to a development memory estimate."""
    factor: float
    margin_bytes: int

HeadroomRule = ReleaseHeadroom | LegacyDevMultiplierHeadroom | DevSizeHeadroom | NoHeadroom


@dataclass(frozen=True)
class CapNotRecorded:
    """The memory cap was not recorded."""


@dataclass(frozen=True)
class UnderBudget:
    """The memory estimate does not require a budget cap."""


@dataclass(frozen=True)
class CappedAtBudget:
    """The original estimate exceeds the memory budget cap."""
    uncapped: int
    cap: int | CapNotRecorded


@dataclass(frozen=True)
class ShapeRankedPeak:
    """The estimate uses a ranked shape peak with its floor, headroom, and cap."""
    runs: int
    share_percent: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: HeadroomRule = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeLargest:
    """The estimate uses the largest shape peak with its floor, headroom, and cap."""
    runs: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: HeadroomRule = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeFallback:
    """The shape estimate uses a fallback with its budget cap."""
    cap: CappedAtBudget | UnderBudget = UnderBudget()

ShapeNeedBasis = ShapeRankedPeak | ShapeLargest | ShapeFallback

NeedBasis = LegacyNeedSource | ShapeNeedBasis


@dataclass(frozen=True)
class BasisNotRecorded:
    """The basis of the memory estimate was not recorded."""

TestPhaseNeedForm = Literal["same_width", "wider_unscaled", "scaled_up"]


@dataclass(frozen=True)
class TestPhaseNeed:
    """The test memory estimate holds its widths, sample count, scaling form, and margin."""
    bytes: int
    runs: int
    width: int
    recorded_width: int
    form: TestPhaseNeedForm
    margin: float

FinalOutcomeName = Literal["completed", "failed", "killed", "owner_gone"]


class QueueLifeIdentity(TypedDict):
    pid: int
    start_time: str
    since: float
    session_id: str
    session_name: str
    step: str
    folder: str
    unit: NotRequired[str]


class FinalizationIntent(TypedDict):
    reservation_id: str
    peak: int
    scoped: bool
    outcome: FinalOutcomeName
    ended_at: str
    end_kind: NotRequired[Literal["step_end", "token_return"]]
    history_start: NotRequired[int]
    queue_identity: NotRequired[QueueLifeIdentity]
    status: NotRequired[int]
    test_phase_peak_bytes: NotRequired[int]
    test_phase_started_at: NotRequired[float]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]


@dataclass(frozen=True)
class RequestedTestWidth:
    """The caller requests this positive number of test threads."""
    threads: int

    def __post_init__(self) -> None:
        if self.threads <= 0:
            raise ValueError("test width must be positive")


@dataclass(frozen=True)
class UnspecifiedTestWidth:
    """The test command does not specify a thread count."""


@dataclass(frozen=True)
class NoTestWidth:
    """The command has no test thread count."""

TestWidth = RequestedTestWidth | UnspecifiedTestWidth | NoTestWidth

@dataclass(frozen=True)
class StepCallRecord(CallRecord):
    """The verify step has its own journal start."""
    step_started_at: float


Call = CallRecord | StepCallRecord | NoCallRecord


@dataclass(frozen=True)
class CallerRun:
    """The caller names this run; the pass captures whether it still lives."""

    run: LiveBuildRun | NoLiveBuildRun


@dataclass(frozen=True)
class FoundRun:
    """The process ancestry supplies this run without a caller claim."""

    run: LiveBuildRun | NoLiveBuildRun


class Reservation(TypedDict):
    pid: int
    start_time: str
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_threads: TestWidth
    call_id: Call
    repo_identity: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    shape: NotRequired[str]
    reservation_id: NotRequired[str]
    scoped: NotRequired[bool]
    finalization: NotRequired[FinalizationIntent]
    queue_waiter: NotRequired[Waiter]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    folder: NotRequired[str]
    unit: NotRequired[str]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]
    build_run: NotRequired[LiveBuildRun | NoLiveBuildRun]
    run_origin: NotRequired[CallerRun | FoundRun]
    slot_staff: NotRequired[StaffId | NoEndedStepStaff]
    job: NotRequired[StepJob]


@dataclass(frozen=True)
class ExpectedPeak:
    """The expected memory peak holds its source, sample count, and estimate basis."""
    bytes: int
    source: NeedSource
    count: int
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()

    def __post_init__(self) -> None:
        if self.source == "shape" and not isinstance(self.basis, (ShapeRankedPeak, ShapeLargest, ShapeFallback)):
            raise ValueError("a shape estimate needs its shape basis")
        if isinstance(self.basis, BasisNotRecorded):
            if self.source in ("measured", "buildlog", "pooled", "fallback"):
                object.__setattr__(self, "basis", self.source)


@dataclass(frozen=True)
class PromisedMemory:
    """The step promises this number of memory bytes."""
    bytes: int


@dataclass(frozen=True)
class PeakTimeUnknown:
    """The time of the memory peak is unknown."""


@dataclass(frozen=True)
class NoPeakRecorded:
    """The step has no recorded memory peak."""


@dataclass(frozen=True)
class RampPromise:
    """The memory promise covers the growth toward the step peak."""
    bytes: int


@dataclass(frozen=True)
class LifetimePromise:
    """The memory promise remains in effect for the step lifetime."""
    bytes: int


@dataclass(frozen=True)
class NoPromise:
    """The step has no memory promise."""

MemoryPromise = RampPromise | LifetimePromise | NoPromise

TEST_PHASE_MARGIN_FALLBACK = 1.2


@dataclass(frozen=True)
class PeakSoFar:
    """The observed peak holds its size and the times of peak activity."""
    bytes: int
    peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    last_near_peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    old_sampler: bool = False
    near_peak_is_fresh: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class PeakDecayed:
    """The record holds the peak after decay, the original peak, and the decay times."""
    bytes: int
    peak_bytes: int
    last_near_peak_at: float
    decayed_at: float
    old_sampler: bool
    margin: float = field(default=TEST_PHASE_MARGIN_FALLBACK, kw_only=True)
    sampled: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class TestPhaseStarted:
    """The test phase holds its start time, prior peak, and memory need."""
    started_at: float
    peak_so_far: int
    need: TestPhaseNeed


@dataclass(frozen=True)
class PeakFileUnreadable:
    """The unreadable peak file retains the promised byte count as a floor."""
    bytes: int

PeakHistory = NoPeakRecorded | PeakSoFar | PeakDecayed | PeakFileUnreadable | TestPhaseStarted


@dataclass(frozen=True)
class PidNotRecorded:
    """The process identifier was not recorded."""


@dataclass(frozen=True)
class ProcessAnonymousUse:
    """Retain one process's resident swap-backed use or its unavailable reading."""
    pid: int
    use: AnonymousRunUse | RunUseUnavailable


@dataclass(frozen=True)
class NoCompilerSeen:
    """Both snapshots agree that no compiler is active."""


@dataclass(frozen=True)
class CompilerSeen:
    """Both snapshots identify an active compiler."""


@dataclass(frozen=True)
class CompilerEvidenceIncomplete:
    """The compiler evidence cannot support a reduction in need."""
    reason: str


CompilerEvidence = NoCompilerSeen | CompilerSeen | CompilerEvidenceIncomplete


@dataclass(frozen=True)
class CompilerEvidenceNotRecorded:
    """This memory record has no compiler evidence."""


@dataclass(frozen=True)
class ScopeSource:
    """The memory reading comes from this scope and its process members."""
    directory: Path
    members: frozenset[int]
    compiler_evidence: CompilerEvidence | CompilerEvidenceNotRecorded = field(
        default_factory=CompilerEvidenceNotRecorded, kw_only=True, compare=False)


@dataclass(frozen=True)
class ProcessSource:
    """The memory reading comes from these individual processes."""
    processes: tuple[ProcessAnonymousUse, ...]
    memory_read_incomplete: bool = field(default=False, kw_only=True)
    compiler_evidence: CompilerEvidence | CompilerEvidenceNotRecorded = field(
        default_factory=CompilerEvidenceNotRecorded, kw_only=True, compare=False)


@dataclass(frozen=True)
class NoSource:
    """The memory reading has no recorded source."""

TestNeedReason = Literal["compiler", "compiler evidence unavailable", "near-peak sample",
                         "peak evidence unavailable", "post-decay sample unavailable", "peak after decay"]


@dataclass(frozen=True)
class TestNeedOverride:
    """The test evidence selects this need over the progress window."""
    bytes: int
    reason: TestNeedReason


@dataclass(frozen=True)
class NoTestNeedOverride:
    """Test evidence leaves the existing calculation in effect."""


@dataclass(frozen=True)
class MeasuredMemory:
    """The step holds measured memory, swap, promises, and peak history."""
    bytes: int
    anonymous: AnonymousRunUse | RunUseUnavailable = field(default_factory=lambda: RunUseUnavailable("swap-backed use unavailable"), kw_only=True)
    anonymous_source: ScopeSource | ProcessSource | NoSource = field(default_factory=NoSource, kw_only=True)
    promise: MemoryPromise = NoPromise()
    # Swapped bytes stay separate from resident swap-backed use.
    swap_bytes: int = 0
    observed_peak: int = 0
    # An existing peak file could not be read; its promise remains a floor.
    unknown_peak_promise: int = 0
    peak_history: PeakHistory = NoPeakRecorded()
    compiler_evidence: CompilerEvidence | CompilerEvidenceNotRecorded = field(
        default_factory=CompilerEvidenceNotRecorded, kw_only=True)


@dataclass(frozen=True)
class ProgressAdjustedMemory(MeasuredMemory):
    """The measured memory includes progress evidence and the remaining memory need."""
    counter: progress.ProgressCounter | progress.NoCounter = field(kw_only=True)
    window: progress.WindowPeak | progress.NoMatchingProfile = field(kw_only=True)
    remaining_need: int = field(kw_only=True)
    test_need_override: TestNeedOverride | NoTestNeedOverride = field(default_factory=NoTestNeedOverride, kw_only=True)


@dataclass(frozen=True)
class CompilerJob:
    """The compilers that sccache runs for one step scope, which the scope's step owns."""
    scope: Path
    use: AnonymousRunUse | RunUseUnavailable


@dataclass(frozen=True)
class CompilerServiceMemory(MeasuredMemory):
    """Compiler charge, without a build reservation or run-alone veto.

    The bytes leave out the jobs: each job counts in the step that names its scope.
    """

    job_state: IdleService = IdleService()
    pid: int | PidNotRecorded = field(kw_only=True)
    jobs: tuple[CompilerJob, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class ServiceJobStateNotRecorded:
    """The service job state was not recorded."""


@dataclass(frozen=True)
class UnownedSliceMemory(MeasuredMemory):
    """The memory reading describes a scope without an owned reservation."""
    diagnostic: str = ""
    cgroup: str = ""
    owner: str = ""
    activity: ScopeProcesses | EmptyBuildScope = EmptyBuildScope()
    pid: int | PidNotRecorded = field(kw_only=True)
    # Keep this wire union stable for pinned clients.
    job_state: RunningServiceJob | IdleService | NotServiceSlice = field(
        default=NotServiceSlice(), metadata={"acquisition_unused": True})
    # New service states belong in this optional field.
    service_job_state: ServiceJobState | ServiceJobStateNotRecorded = field(
        default_factory=ServiceJobStateNotRecorded, kw_only=True, metadata={"acquisition_unused": True})


def recorded_service_state(memory: CompilerServiceMemory | UnownedSliceMemory) -> ServiceJobState:
    """Read the current state, with the legacy state for older snapshots."""
    if isinstance(memory, UnownedSliceMemory) and not isinstance(memory.service_job_state, ServiceJobStateNotRecorded):
        return memory.service_job_state
    return memory.job_state


@dataclass(frozen=True)
class GoneMemory:
    """The step no longer has a live memory owner."""


@dataclass(frozen=True)
class RemoteMemoryUnavailable:
    """The reason explains why remote memory cannot be read."""
    reason: str


StepMemory = PromisedMemory | MeasuredMemory | ProgressAdjustedMemory | CompilerServiceMemory | UnownedSliceMemory | GoneMemory | RemoteMemoryUnavailable


@dataclass(frozen=True)
class NoPressureHold:
    """Memory pressure does not require an admission hold."""


@dataclass(frozen=True)
class PressureUnavailable:
    """The reason explains why memory pressure cannot be read."""
    reason: str


@dataclass(frozen=True)
class PressureConditionNotRecorded:
    """The pressure condition was not recorded."""


@dataclass(frozen=True)
class MemoryPressure:
    """The pressure reading holds averages, swap use, and the next permitted start time."""
    full_avg10: float
    some_avg10: float = 0.0
    swap_used_percent: float = 0.0
    next_start_at: float = 0.0
    condition: Literal["full", "some"] | PressureConditionNotRecorded = PressureConditionNotRecorded()


@dataclass(frozen=True)
class UnknownHostTotal:
    """The total host memory is unknown."""


@dataclass(frozen=True)
class HostMemory:
    """The host reading holds available memory, total memory, and swap use."""
    available: int
    total: int | UnknownHostTotal
    swap_total: int = 0
    swap_free: int = 0
    build_and_ci_swap: int | ReadingUnavailable = field(default=0, kw_only=True)
    swap_in_window_bytes: int = field(default=0, kw_only=True)

Pressure = NoPressureHold | MemoryPressure | PressureUnavailable

NO_PRESSURE_HOLD = NoPressureHold()


class Waiter(TypedDict):
    pid: int
    start_time: str
    build_run: NotRequired[LiveBuildRun | NoLiveBuildRun]
    need: int
    since: float
    order_since: NotRequired[float]
    label: NotRequired[str]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    unit: NotRequired[str]
    step: NotRequired[str]
    folder: NotRequired[str]
    token_left_at: NotRequired[float]
    token_back_at: NotRequired[float]
    priority_since: NotRequired[float]
    estimated_at: NotRequired[float]
    build_history_current: NotRequired[bool]
    fresh_test: NotRequired[bool]
    repo_identity: NotRequired[str]
    repository_name: NotRequired[str]
    legacy_name: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    need_source: NotRequired[NeedSource]
    need_runs: NotRequired[int]
    need_basis: NotRequired[dict[str, object]]
    test_threads: NotRequired[int | None]
    # Requests can record a route. Direct commands without a reason use DIRECT_CARGO.
    route: NotRequired[StepRoute]
    verify: NotRequired[VerifyCall]
    job: NotRequired[StepJob]


@dataclass(frozen=True)
class PriorityUnit:
    """The unit holds this position in the priority order."""
    unit: str
    position: int = 0


@dataclass(frozen=True)
class NormalOrder:
    """The waiter follows the normal queue order."""


class RankedWaiter(Waiter):
    order: PriorityUnit | NormalOrder


def waiter_priority(row: Waiter) -> PriorityUnit | NormalOrder:
    order = cast(Mapping[str, object], row).get("order")
    return order if isinstance(order, (PriorityUnit, NormalOrder)) else NormalOrder()


@dataclass(frozen=True)
class TokenWaitingWaiter:
    """The waiter retains the time when it left to wait for the Cargo token."""
    left_at: float


@dataclass(frozen=True)
class SettledPriority:
    """The queue priority has no pending change."""


@dataclass(frozen=True)
class PendingPriorityTransition:
    """The pending priority change holds its action, time, unit, and actor."""
    id: str
    action: Literal["set", "clear"]
    at: float
    month_row_end: int = 0
    unit: str = ""
    why: str = ""
    set_by: str = ""


class PriorityEntry(TypedDict):
    unit: str
    why: str
    set_at: float
    set_by: str


@dataclass(frozen=True)
class QueuePriority:
    """The queue priority holds ordered entries and any pending change."""
    entries: tuple[PriorityEntry, ...]
    transition: SettledPriority | PendingPriorityTransition = SettledPriority()


def upgrade_queue_priority(value: dict[str, object]) -> dict[str, object]:
    """Read an older priority without changing its stored record."""
    if "entries" in value or not all(key in value for key in ("unit", "why", "set_at", "set_by")):
        return value
    entry = {key: value[key] for key in ("unit", "why", "set_at", "set_by")}
    transition = value.get("transition", {"kind": "SettledPriority"})
    clearing = False
    if isinstance(transition, dict):
        old = cast(dict[str, object], transition)
        clearing = old.get("action") == "clear"
        if old.get("kind") == "PendingPriorityTransition":
            transition = {**old, "unit": entry["unit"], "why": entry["why"], "set_by": entry["set_by"]}
    return {"kind": "QueuePriority", "entries": [] if clearing else [entry], "transition": transition}


@dataclass(frozen=True)
class NoQueuePriority:
    """The queue has no unit priority."""

NO_QUEUE_PRIORITY = NoQueuePriority()


def queue_command_actor() -> str:
    """Identify the user command without a session-role check."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "user"


@dataclass(frozen=True)
class AnonymousRunUse:
    """Count resident anon plus shmem bytes, without swap; the codec uses this name."""
    bytes: int


@dataclass(frozen=True)
class RunUseUnavailable:
    """The reason explains why the run memory use is unavailable."""
    reason: str


@dataclass(frozen=True)
class NoEndedStepStaff:
    """The ended step has no recorded staff owner."""


@dataclass(frozen=True)
class EndedStepFactNotRecorded:
    """The reservation did not record this fact about the ended step."""


@dataclass(frozen=True)
class EndedStepFacts:
    """The reservation's work and staff facts survive until the hold ends."""

    step: str | EndedStepFactNotRecorded
    package: str | PackageNotRecorded
    project: str | EndedStepFactNotRecorded
    folder: str | EndedStepFactNotRecorded
    staff_id: StaffId | NoEndedStepStaff
    job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED


@dataclass(frozen=True)
class HoldPredatesStepFacts:
    """The saved hold records its memory but has no facts about the ended step."""


@dataclass(frozen=True)
class BetweenStepsSlot:
    """The run retains memory between steps until the slot expires."""
    run: LiveBuildRun
    reservation: str
    bytes: int
    expires_at: float
    identity: EndedStepFacts | HoldPredatesStepFacts


@dataclass(frozen=True)
class ContinuationSlot:
    """The owner continues within a memory slot retained between steps."""
    slot: BetweenStepsSlot
    owner: str

RunSlot = BetweenStepsSlot | ContinuationSlot


@dataclass(frozen=True)
class RunMemory:
    """The run holds its owner, memory need, measured use, and retained bytes."""
    owner: str
    need: int
    use: AnonymousRunUse | RunUseUnavailable
    held: int = 0
    run: LiveBuildRun | NoLiveBuildRun = field(default_factory=NoLiveBuildRun, kw_only=True)


@dataclass(frozen=True)
class RunKeepAside:
    """The run retains this number of bytes for later growth."""
    memory: RunMemory
    bytes: int


@dataclass(frozen=True)
class CapUnavailable:
    """These runs prevent calculation of the memory cap."""
    runs: tuple[RunMemory, ...]


@dataclass(frozen=True)
class MemoryRoom:
    """The memory budget holds available bytes, charges, reserves, and remaining room."""
    available: int
    safety: int
    used: int
    kept: int
    signed_room: int
    cap: int | CapUnavailable
    runs: tuple[RunKeepAside, ...]
    total: int | UnknownHostTotal = field(default_factory=UnknownHostTotal, kw_only=True)
    ci_use: int = field(default=0, kw_only=True)
    agent_swap_demand: int = field(default=0, kw_only=True)

    @property
    def cap_use(self) -> int:
        """Return the swap-backed use and CI use shown inside the cap."""
        return self.used + self.ci_use

    @property
    def room(self) -> int:
        return max(0, self.signed_room)

    @property
    def shortfall(self) -> int:
        return max(0, -self.signed_room)


@dataclass(frozen=True)
class Fits:
    """The memory need fits within the available room."""


@dataclass(frozen=True)
class StartsAlone:
    """The step can start under the rule for a step that runs alone."""


@dataclass(frozen=True)
class WithinSlot:
    """The step fits within its retained memory slot."""


@dataclass(frozen=True)
class DoesNotFit:
    """The memory need exceeds the available room."""

StartBasis = Fits | StartsAlone | WithinSlot | DoesNotFit


@dataclass(frozen=True)
class RoomCheck:
    """The check compares memory need with room and records the decision basis."""
    need: int
    room: int
    passes: bool
    basis: StartBasis = field(kw_only=True)


@dataclass(frozen=True)
class NoOrderHold:
    """The queue order does not delay this step."""


@dataclass(frozen=True)
class HeldBehindHead:
    """This waiter at the queue head delays the step."""
    waiter: Waiter


@dataclass(frozen=True)
class GpuSlotFree:
    """A GPU test slot is available."""


@dataclass(frozen=True)
class GpuSlotWait:
    """A GPU test step waits until a running GPU test step ends."""
    used: int
    slots: int


@dataclass(frozen=True)
class AdmissionDecision:
    """The decision holds admission state, memory charges, and reasons for a delay."""
    state: Literal["admit", "hold"]
    threshold: int
    promised: int
    reserve: int
    measured: int
    pressure: Pressure = NO_PRESSURE_HOLD
    charged: int = 0
    free_passes: bool = True
    order_hold: NoOrderHold | HeldBehindHead = NoOrderHold()
    starts_at: float = 0.0
    reused_slot: bool = False
    room: RoomCheck = RoomCheck(0, 0, True, basis=Fits())
    gpu: GpuSlotFree | GpuSlotWait = GpuSlotFree()


@dataclass(frozen=True)
class StartSpacingWait:
    """The step waits until this start time to preserve spacing."""
    at: float


@dataclass(frozen=True)
class NeedsMemory:
    """The memory wait holds the need, free bytes, threshold, growth, and reserve."""
    need: int = 0
    free: int = 0
    threshold: int = 0
    growth: int = 0
    reserve: int = 0


@dataclass(frozen=True)
class BehindHead:
    """The waiter holds the identity and join time of the step ahead."""
    head_name: str
    joined_at: float
    head_session_id: str


@dataclass(frozen=True)
class ReadyForAdmission:
    """The step is ready for the next admission check."""


@dataclass(frozen=True)
class ReadingUnavailable:
    """The reason explains why the memory reading is unavailable."""
    reason: str = "memory reading unavailable"


@dataclass(frozen=True)
class EstimateExpired:
    """The estimate has expired since this recorded estimate time."""
    estimated_at: float


@dataclass(frozen=True)
class NatedevHeld:
    """A CI step waits for the release of a natedev hold."""
    holder: str
    purpose: str


@dataclass(frozen=True)
class LandingWait:
    """Natemerge's merge test holds this step on natedev."""


LANDING_WAIT_TEXT = (
    "Waits for natedev: natemerge's integrate job runs there, and no other target can take this test now."
)


HeadCause = (MemoryPressure | PressureUnavailable | ReadingUnavailable | StartSpacingWait | NeedsMemory | EstimateExpired
             | BehindHead | GpuSlotWait | ReadyForAdmission | LandingWait | placement.CargoFolderWait)


@dataclass(frozen=True)
class HeadAdmissionStatus:
    """The queue head status holds its decision, memory need, and cause."""
    decision: AdmissionDecision
    need: int
    available: int
    cause: HeadCause = field(kw_only=True)

WaitReason = (StartSpacingWait | PressureUnavailable | EstimateExpired | NeedsMemory | BehindHead | GpuSlotWait
              | HeadAdmissionStatus | ReadyForAdmission | ReadingUnavailable | MemoryPressure | TokenWaitingWaiter
              | NatedevHeld | LandingWait | placement.CargoFolderWait)


def held_text(wait: NatedevHeld) -> str:
    return f"natedev is held by {wait.holder} for {wait.purpose}; waiting for release"


def gpu_slot_text(wait: GpuSlotWait) -> str:
    return f"waiting for the GPU test slot ({wait.used} of {wait.slots} in use)"


def head_wait_text(reason: HeadAdmissionStatus, zone: ZoneInfo) -> str:
    cause = reason.cause
    if isinstance(cause, LandingWait):
        return "head: " + LANDING_WAIT_TEXT
    if isinstance(cause, (PressureUnavailable, ReadingUnavailable)):
        return "head: " + cause.reason
    if isinstance(cause, MemoryPressure):
        return "head: " + pressure_text(cause)
    if isinstance(cause, StartSpacingWait):
        return "head: next start at " + datetime.fromtimestamp(cause.at, zone).strftime("%H:%M:%S")
    if isinstance(cause, EstimateExpired):
        return "head: estimate expired"
    if isinstance(cause, BehindHead):
        return "head: behind " + cause.head_name
    if isinstance(cause, GpuSlotWait):
        return "head: " + gpu_slot_text(cause)
    if isinstance(cause, ReadyForAdmission):
        return "head: ready for admission"
    if isinstance(cause, placement.CargoFolderWait):
        return "head: " + placement.CARGO_FOLDER_WAIT_REASON
    return f"head: needs {reason.need / GIB:.1f} GiB; room {max(0, reason.decision.room.room) / GIB:.1f} GiB"


@dataclass(frozen=True)
class PackageNotRecorded:
    """The queue record predates the package field. An empty package means workspace scope."""


@dataclass(frozen=True)
class WaitingStep:
    """The waiting step holds its caller, memory need, wait reason, and run identity."""
    pid: int
    session_id: str
    session_name: str
    step: str
    folder: str
    since: float
    need: int
    reason: WaitReason
    unit: str = ""
    project: str = ""
    run: LiveBuildRun | NoLiveBuildRun = field(default=NoLiveBuildRun(), kw_only=True)
    package: str | PackageNotRecorded = field(default=PackageNotRecorded(), kw_only=True)
    route: StepRoute = field(default=DIRECT_CARGO, kw_only=True)
    verify: VerifyStep = field(default=NotVerifyCall(), kw_only=True)
    job: StepJob | GateJobNotRecorded = field(default=GATE_JOB_NOT_RECORDED, kw_only=True)


@dataclass(frozen=True)
class RunningStep:
    """The running step holds its caller, memory charge, and execution evidence."""
    session_id: str
    session_name: str
    step: str
    folder: str
    promise: int
    measured: StepMemory
    pid: int | PidNotRecorded = field(kw_only=True)
    run: LiveBuildRun | NoLiveBuildRun = NoLiveBuildRun()
    started_at: float = 0.0
    unit: str = ""
    project: str = ""
    execution: execution_identity.ExecutionEvidence = execution_identity.ExecutionNotStarted()
    assignment_host: str = ""
    mac_placement: str = field(default="", kw_only=True)
    package: str | PackageNotRecorded = field(default=PackageNotRecorded(), kw_only=True)
    route: StepRoute = field(default=DIRECT_CARGO, kw_only=True)
    verify: VerifyStep = field(default=NotVerifyCall(), kw_only=True)
    job: StepJob | GateJobNotRecorded = field(default=GATE_JOB_NOT_RECORDED, kw_only=True)


def execution_host(step: RunningStep, local_host: str) -> str:
    evidence = step.execution
    if isinstance(evidence, execution_identity.ObservedExecution):
        identity = evidence.identity
        return identity.host if isinstance(identity, execution_identity.ProcessExecution) else identity.target
    return step.assignment_host or local_host


def execution_group(step: RunningStep, fallback: str) -> str:
    evidence = step.execution
    if isinstance(evidence, execution_identity.ObservedExecution):
        identity = evidence.identity
        if isinstance(identity, execution_identity.ProcessExecution):
            return repr((identity.host, identity.generation, identity.pid, identity.start_time))
        return repr((identity.target, identity.generation, identity.instance, identity.attempt))
    if step.assignment_host:
        return repr((step.assignment_host, fallback))
    return run_owner(step.run.pid, step.run.start_time) if isinstance(step.run, LiveBuildRun) else fallback


@dataclass(frozen=True)
class QueueView:
    """The queue view holds waiting steps, running steps, and memory totals at one time."""
    waiting: tuple[WaitingStep, ...]
    running: tuple[RunningStep, ...]
    available: int
    promised: int
    reserve: int
    now: float


@dataclass(frozen=True)
class HostAvailable:
    """The host summary holds available memory, total memory, reserve, and pressure."""
    available: int
    total: int | UnknownHostTotal
    reserve: int
    pressure: Pressure


@dataclass(frozen=True)
class AdmissionTimeNotRecorded:
    """The admission time was not recorded."""


@dataclass(frozen=True)
class ExplainedRunningStep:
    """The running step explanation holds admission time and promised and charged memory."""
    step: RunningStep
    admitted_at: float | AdmissionTimeNotRecorded
    promise: int
    measured: int
    charged: int
    owner: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class QueueMemoryTotals:
    """The totals hold charged memory, measured memory, and expected growth."""
    charged: int
    measured: int
    growth: int


@dataclass(frozen=True)
class WidthNotRecorded:
    """The test thread count was not recorded."""


@dataclass(frozen=True)
class NeedSourceNotRecorded:
    """The source of the memory need was not recorded."""


@dataclass(frozen=True)
class RecordedNeed:
    """The saved estimate holds its source, history, test selection, width, and basis."""
    source: NeedSource
    runs: int
    history_days: int
    scope: TestScope
    package: str
    filters: tuple[str, ...]
    test_threads: int | WidthNotRecorded
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()


@dataclass(frozen=True)
class FreeCheck:
    """The check compares free memory with the need, reserve, growth, and threshold."""
    available: int
    need: int
    reserve: int
    growth: int
    threshold: int
    passes: bool


@dataclass(frozen=True)
class HeadExplanation:
    """The queue head explanation holds its estimate, admission checks, priority, and cause."""
    waiter: WaitingStep
    need_source: RecordedNeed | NeedSourceNotRecorded
    decision: AdmissionDecision
    room: RoomCheck
    free: FreeCheck
    has_priority: bool
    cause: HeadCause = field(kw_only=True)


@dataclass(frozen=True)
class NoWaiter:
    """The queue has no waiter to explain."""


@dataclass(frozen=True)
class TokenWaitersOnly:
    """Rows retain their places, but all are waiting for the cargo token."""


@dataclass(frozen=True)
class LineWaiter:
    """The waiter holds its admission decision and memory checks for the queue line."""
    waiter: WaitingStep
    fits_now: bool
    decision: AdmissionDecision
    room: RoomCheck
    free: FreeCheck


@dataclass(frozen=True)
class WaitingLine:
    """The line holds waiter checks and summary counts, sizes, and times."""
    steps: tuple[LineWaiter, ...]
    count: int
    fitting: int
    smallest_need: int
    largest_need: int
    oldest_since: float


@dataclass(frozen=True)
class ReadyToStart:
    """The start spacing permits a step to start now."""


@dataclass(frozen=True)
class NextStartAt:
    """The start spacing permits the next step at this time."""
    at: float

StartSpacing = ReadyToStart | NextStartAt


@dataclass(frozen=True)
class QueueExplanation:
    """The explanation holds queue evidence, policy, decisions, and memory checks."""
    now: float
    host: HostAvailable | ReadingUnavailable
    running: tuple[ExplainedRunningStep, ...]
    totals: QueueMemoryTotals
    head: HeadExplanation | TokenWaitersOnly | NoWaiter
    line: WaitingLine
    decided_from: QueueInputs
    policy: AdmissionPolicy = field(kw_only=True)
    ordered: tuple[RankedWaiter, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    spacing: StartSpacing = ReadyToStart()
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    room: MemoryRoom | ReadingUnavailable = ReadingUnavailable()


@dataclass(frozen=True)
class QueueInputs:
    """Keep queue evidence separate from the admission policy."""
    ordered: Sequence[tuple[Path, Waiter]]
    running: tuple[RunningStep, ...]
    reservations: tuple[Reservation, ...]
    host: HostMemory | ReadingUnavailable
    pressure: Pressure
    spacing: StartSpacing
    now: float
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    blockers_for: frozenset[str] = frozenset()
    waiter_orders: dict[str, PriorityUnit | NormalOrder] = field(default_factory=dict, kw_only=True)
    slots: tuple[RunSlot, ...] = field(default=(), kw_only=True)
    cargo_folders: dict[str, placement.CargoFolder] = field(default_factory=dict, kw_only=True)
    occupied_cargo_folders: frozenset[placement.CargoFolder] = field(default=frozenset(), kw_only=True)
    pending_remote_starts: frozenset[str] = field(default=frozenset(), kw_only=True)
    local_waits: dict[str, placement.EvidenceNeeded] = field(default_factory=dict, kw_only=True)
    landing_waiters: frozenset[str] = field(default=frozenset(), kw_only=True)
    cloud_route_waiters: frozenset[str] = field(default=frozenset(), kw_only=True)
    cloud_route_waiters_past_deadline: frozenset[str] = field(default=frozenset(), kw_only=True)

    def __post_init__(self) -> None:
        orders = {waiter_owner(row): (waiter_priority(row) if isinstance(row.get("order"), (PriorityUnit, NormalOrder))
                                     else self.waiter_orders.get(waiter_owner(row), NormalOrder()))
                  for _, row in self.ordered}
        object.__setattr__(self, "waiter_orders", orders)
        object.__setattr__(self, "ordered", [(path, stored_waiter(row)) for path, row in self.ordered])


@dataclass(frozen=True)
class QueueRead:
    """The queue reading holds its view, explanation, ordered waiters, and diagnostics."""
    view: QueueView
    explanation: QueueExplanation
    ordered: list[tuple[Path, RankedWaiter]]
    diagnostics: tuple[ConfigurationDiagnostic, ...] = ()


@dataclass(frozen=True)
class NoBuildHistoryGeneration:
    """The build history generation was not recorded."""


@dataclass(frozen=True)
class BuildHistoryGeneration:
    """The value identifies the build history generation used for an estimate."""
    value: int


@dataclass(frozen=True)
class StepProfile:
    """The step profile holds repository identity, memory estimate, and test scope."""
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak
    repository: Repository = UnknownRepository()
    scope: TestScope = "full"
    estimated_at: float = 0.0
    build_history_generation: BuildHistoryGeneration | NoBuildHistoryGeneration = NoBuildHistoryGeneration()


@dataclass(frozen=True)
class InferFromArgv:
    """The caller requests inference from the command arguments."""

INFER_FROM_ARGV = InferFromArgv()


@dataclass(frozen=True)
class MeasuredProgramPeak:
    """The program peak holds measured bytes and the number of runs."""
    bytes: int
    runs: int


@dataclass(frozen=True)
class FirstProgramPeak:
    """The first program run uses this initial memory estimate."""
    bytes: int


@dataclass(frozen=True)
class HistoryPeakSizing:
    """The sizing holds the initial peak, current estimate, and estimate time."""
    first_peak_bytes: int
    estimate: MeasuredProgramPeak | FirstProgramPeak
    estimated_at: float


@dataclass(frozen=True)
class ProgramProfile:
    """The program profile holds its repository, worktree, and memory sizing."""
    program: str
    repo: str
    worktree: str
    sizing: HistoryPeakSizing
    receive_route_notices: bool = field(default=False, kw_only=True)

    @property
    def step(self) -> str:
        return self.program

    @property
    def estimated_at(self) -> float:
        return self.sizing.estimated_at

    @property
    def peak(self) -> ExpectedPeak:
        estimate = self.sizing.estimate
        return (ExpectedPeak(estimate.bytes, "measured", estimate.runs)
                if isinstance(estimate, MeasuredProgramPeak) else ExpectedPeak(estimate.bytes, "fallback", 0))

    @property
    def repository(self) -> UnknownRepository:
        return UnknownRepository()

    @property
    def scope(self) -> TestScope:
        return "full"


@dataclass(frozen=True)
class ConfigurationDiagnostic:
    """The diagnostic describes a configuration problem."""
    diagnostic: str

_warned_configuration_diagnostics: set[ConfigurationDiagnostic] = set()


def warn_configuration_diagnostics(diagnostics: tuple[ConfigurationDiagnostic, ...]) -> None:
    for diagnostic in diagnostics:
        if diagnostic not in _warned_configuration_diagnostics:
            _warned_configuration_diagnostics.add(diagnostic)
            print(diagnostic.diagnostic, file=sys.stderr)

@dataclass(frozen=True)
class MemoryConfig:
    """The configuration holds admission policy, service timings, and memory estimate settings."""
    policy: AdmissionPolicy
    ci_memory_slice: str
    ci_label: str = field(kw_only=True)
    test_phase_margin: float
    dev_size_factor: float
    dev_size_margin_bytes: int
    release_headroom: float
    running_pass_seconds: float
    waiting_pass_seconds: float
    min_pass_gap_seconds: float
    wait_reply_seconds: float
    query_timeout_seconds: float
    replay_retention_seconds: float
    ended_placement_seconds: float
    reconnect_seconds: float
    silence_seconds: float
    outage_notice_seconds: float
    reading_alert_seconds: float
    unreachable_limit_seconds: float
    source_check_seconds: float
    source_settle_seconds: float
    request_max_bytes: int
    program_first_peaks: Mapping[str, int]
    program_sample_seconds: float
    program_stop_seconds: float
    peak_wave_seconds: float
    diagnostics: tuple[ConfigurationDiagnostic, ...] = field(default=(), compare=False)


def _gib_bytes(value: float) -> int:
    return max(1, int(value * GIB))


def _ci_slice_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*\.slice", value) is None:
        raise ValueError("expected a systemd slice name")
    return value


def _ci_label(value: str) -> str:
    try:
        _ = value.format(runner="runner", job="job")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("expected a CI label with only {runner} and {job} fields") from error
    return value


def configuration_diagnostics(messages: Iterable[str]) -> tuple[ConfigurationDiagnostic, ...]:
    return tuple(ConfigurationDiagnostic(message) for message in dict.fromkeys(messages))


def read_memory_config(*, report: bool = True) -> MemoryConfig:
    """Resolve the live configuration for one admission decision."""
    with conf.diagnostics(report=False) as messages:
        settings_read = conf.read("build_admission")
        keys = settings_read.settings
        values = keys if isinstance(keys, dict) else settings_read.values()

        def value[Value](key: str, convert: Callable[[str], Value]) -> Value:
            result = settings_read.origin(key, convert)
            if isinstance(result, conf.UnavailableValue):
                raise conf.Unusable(f"{result.reason}; no usable committed value")
            if isinstance(result, conf.RecoveredValue):
                messages.append(f"warning: {result.reason}")
            return result.value

        def number(key: str) -> float:
            return value(key, float)

        def whole(key: str) -> int:
            return value(key, int)

        def test_margin() -> float:
            def valid_margin(raw: str) -> float:
                margin = float(raw)
                if not math.isfinite(margin) or margin < 1:
                    raise ValueError("expected a finite margin of at least 1")
                return margin

            result = settings_read.origin("test_phase_margin", valid_margin)
            if isinstance(result, (conf.UnavailableValue, conf.RecoveredValue)):
                messages.append(f"warning: {result.reason}; using test_phase_margin={TEST_PHASE_MARGIN_FALLBACK}")
                return TEST_PHASE_MARGIN_FALLBACK
            return result.value

        def gib(key: str) -> int:
            return value(key, lambda raw: _gib_bytes(float(raw)))

        try:
            running_interval = number("running_pass_seconds")
            timing = AdmissionTiming(*(whole(key) for key in (
                "recovery_start_seconds", "estimate_refresh_seconds", "slot_gap_seconds")), fresh_test_seconds=running_interval)
            pressure = PressurePolicy(*(number(key) for key in (
                "full_pressure_limit", "some_pressure_limit")))
            kinds = {key.removeprefix("peak_hold_seconds."): whole(key)
                     for key in values if key.startswith("peak_hold_seconds.")}
            first_peaks = {key.removeprefix("program_first_peak_gib."): gib(key)
                           for key in values if key.startswith("program_first_peak_gib.")}
            if "cargo" not in first_peaks:
                first_peaks["cargo"] = gib("program_first_peak_gib.cargo")
            config = MemoryConfig(
                policy=AdmissionPolicy(timing, pressure, PeakHold(whole("peak_hold_seconds"), kinds), gib("safety_gib"),
                                       progress_lookahead_percent=number("progress_lookahead_percent"),
                                       agent_swap_share_percent=whole("agent_swap_share_percent"),
                                       gpu_tests=GpuTestSlots(whole("gpu_test_slots"),
                                                              value("gpu_test_repositories", lambda raw: frozenset(raw.split())))),
                ci_memory_slice=value("ci_memory_slice", _ci_slice_name),
                ci_label=value("ci_label", _ci_label),
                test_phase_margin=test_margin(),
                dev_size_factor=number("dev_size_factor"),
                dev_size_margin_bytes=value("dev_size_margin_gib", lambda raw: math.ceil(float(raw) * GIB)),
                release_headroom=number("release_headroom"),
                running_pass_seconds=running_interval, waiting_pass_seconds=number("waiting_pass_seconds"),
                min_pass_gap_seconds=number("min_pass_gap_seconds"),
                wait_reply_seconds=number("wait_reply_seconds"), query_timeout_seconds=number("query_timeout_seconds"),
                replay_retention_seconds=number("replay_retention_seconds"),
                ended_placement_seconds=number("ended_placement_seconds"),
                reconnect_seconds=number("reconnect_seconds"), silence_seconds=number("silence_seconds"),
                outage_notice_seconds=number("outage_notice_seconds"),
                reading_alert_seconds=number("reading_alert_seconds"),
                unreachable_limit_seconds=number("unreachable_limit_seconds"),
                source_check_seconds=number("source_check_seconds"),
                source_settle_seconds=number("source_settle_seconds"),
                request_max_bytes=whole("request_max_bytes"), program_first_peaks=first_peaks,
                program_sample_seconds=number("program_sample_seconds"), program_stop_seconds=number("program_stop_seconds"),
                peak_wave_seconds=number("peak_wave_seconds"))
        finally:
            diagnostics = configuration_diagnostics(messages)
    config = replace(config, diagnostics=diagnostics)
    if report:
        warn_configuration_diagnostics(diagnostics)
    return config

test_threads = parse.test_threads


def process_start_time(pid: int, proc: str | Path = "/proc") -> str | None:
    root = Path(proc) if isinstance(proc, str) else proc
    try:
        stat = read_proc_text(root / str(pid) / "stat")
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19] if fields[0] not in ("Z", "X") else None
    except (OSError, IndexError):
        return None


def format_percent(value: float, *, complete: bool = True) -> str:
    """Round down to one decimal place without showing an early completion."""
    tenths = math.floor(value * 10)
    return f"{(tenths if complete else min(tenths, 999)) / 10:.1f}%"


def pressure_text(pressure: MemoryPressure) -> str:
    if isinstance(pressure.condition, PressureConditionNotRecorded):
        return (f"memory pressure: condition not recorded by service; full avg10 {pressure.full_avg10:.1f}; "
                f"some avg10 {pressure.some_avg10:.1f}; swap {format_percent(pressure.swap_used_percent)} used")
    if pressure.condition == "some":
        return f"memory pressure: some avg10 {pressure.some_avg10:.1f}"
    return f"memory pressure: full avg10 {pressure.full_avg10:.1f}"


def waiter_route(row: Waiter) -> StepRoute:
    """Return the placement route, or keep a direct command where it starts."""
    return row.get("route", DIRECT_CARGO)


def direct_cargo_route() -> LocalRequired:
    """Keep the caller's reason when a direct command must run locally."""
    reason = os.environ.get("BUILD_QUEUE_LOCAL_REASON", "")
    return LocalRequired(reason) if reason else DIRECT_CARGO


def stored_waiter(row: Waiter) -> Waiter:
    """Keep only waiter fields in durable rows and published rows."""
    return cast(Waiter, cast(object, {name: value for name, value in row.items() if name in Waiter.__annotations__}))


@dataclass(frozen=True)
class FreshTest:
    """A matching completed test build proves that the test targets are fresh."""

BUILDLOG_UNIT = "BUILDLOG_UNIT"


def waiter_identity(step: str, worktree: str) -> WaiterIdentity:
    from scripts.production import staff

    session = staff.caller_identity()
    identity: WaiterIdentity = {"session_id": session, "session_name": os.environ.get("CLAUDE_SESSION_NAME", ""),
                                "step": step, "folder": os.getcwd() if worktree else ""}
    if unit := os.environ.get(BUILDLOG_UNIT):
        identity["unit"] = unit
    return identity


class WaiterIdentity(TypedDict):
    unit: NotRequired[str]
    session_id: str
    session_name: str
    step: str
    folder: str


def waiter_owner(row: Waiter) -> str:
    return run_owner(row["pid"], row["start_time"])


def run_owner(pid: int, start_time: str) -> str:
    """Use one key for a captured process identity."""
    return f"{pid}-{start_time}"


@dataclass(frozen=True)
class SessionHolder:
    """The wait holder is the session with this identifier and name."""
    session_id: str
    session_name: str


@dataclass(frozen=True)
class UnitHolder:
    """The wait holder is this unit."""
    unit: str


@dataclass(frozen=True)
class NoHolder:
    """The wait has no identified holder."""


WaitHolder = SessionHolder | UnitHolder | NoHolder


@dataclass(frozen=True)
class AdmissionWaitSnapshot:
    """The wait snapshot holds the decision, estimate, running work, and queue head reason."""
    decision: AdmissionDecision
    peak: ExpectedPeak
    running: tuple[RunningStep, ...]
    available: int
    repo: str
    step: str
    policy: AdmissionPolicy
    diagnostics: tuple[ConfigurationDiagnostic, ...]
    head_need: int = 0
    head_label: str = ""
    head_reason: WaitReason = ReadyForAdmission()
    holder: WaitHolder = field(default_factory=NoHolder)


def eligible_head_position(waiting: Sequence[WaitingStep]) -> int:
    """Position of the first row eligible for the head rule; requires an eligible row."""
    return next(place for place, step in enumerate(waiting, 1)
                if not isinstance(step.reason, TokenWaitingWaiter))


def behind_place(reason: BehindHead, waiting: Sequence[WaitingStep]) -> int:
    """Find the captured position of the step that holds this waiter."""
    return next((place for place, step in enumerate(waiting, 1)
                 if step.session_id == reason.head_session_id and step.since == reason.joined_at), 0)


def reason_phrase(reason: WaitReason, waiting: Sequence[WaitingStep]) -> str:
    match reason:
        case TokenWaitingWaiter():
            return "waiting for the cargo token"
        case EstimateExpired():
            return "estimate expired"
        case ReadingUnavailable():
            return reason.reason
        case NatedevHeld():
            return held_text(reason)
        case LandingWait():
            return LANDING_WAIT_TEXT
        case MemoryPressure():
            return "memory pressure"
        case PressureUnavailable():
            return reason.reason
        case StartSpacingWait():
            return "another step just started; the queue waits a few seconds between starts"
        case NeedsMemory():
            return "free memory"
        case placement.CargoFolderWait():
            return placement.CARGO_FOLDER_WAIT_REASON
        case GpuSlotWait():
            return "GPU test slot"
        case BehindHead():
            place = behind_place(reason, waiting)
            return f"behind #{place}" if place else "behind the first step"
        case ReadyForAdmission():
            return "starts at the next check"
        case HeadAdmissionStatus():
            return (f"#{eligible_head_position(waiting)}'s turn" if isinstance(reason.cause, ReadyForAdmission)
                    else reason_phrase(reason.cause, waiting))
        case _:
            assert_never(reason)
