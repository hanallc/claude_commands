"""Save a person's priority changes for the next admission pass."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
from typing import BinaryIO, cast

from scripts.build_hold import override_guard
from scripts.build_queue import admission_protocol as wire, admission_records as records
from scripts.build_queue import snapshot_codec as codec
from scripts.buildlog import store
from scripts.lib.atomic_file import sync_folder

JOURNAL_NAME = "person_actions.jsonl"


@dataclass(frozen=True)
class SetBump:
    step_key: str
    from_place: int = 0


@dataclass(frozen=True)
class ClearBump:
    """Clear one unit, or all units when the record names no unit."""


@dataclass(frozen=True)
class Bump:
    id: str
    action: SetBump | ClearBump
    unit: str
    actor: str
    written_at: float
    reason: str


@dataclass(frozen=True)
class BumpNotApplied:
    unit: str
    reason: str


@dataclass(frozen=True)
class AppliedBump:
    step_key: str
    owner: wire.Owner
    since: float
    who: str
    from_place: int
    to_place: int
    at: float


@dataclass(frozen=True)
class BumpProgress:
    offset: int = 0
    refusals: tuple[BumpNotApplied, ...] = ()
    moves: dict[str, AppliedBump] = field(default_factory=dict)


class UnknownBumpUnit(ValueError):
    pass


def _saved_state(directory: Path) -> tuple[BumpProgress, dict[str, object]]:
    try:
        state = cast(dict[str, object], json.loads((directory / "state.json").read_bytes()))
    except FileNotFoundError:
        return BumpProgress(), {}
    progress = cast(BumpProgress, codec.parse_snapshot_value(state["bumps"], BumpProgress)) if "bumps" in state else BumpProgress()
    return progress, state


def _entries(stream: BinaryIO, offset: int) -> Iterator[tuple[int, Bump]]:
    if offset < 0 or offset > os.fstat(stream.fileno()).st_size:
        raise ValueError("invalid saved bump offset")
    _ = stream.seek(offset)
    while (line := stream.readline()).endswith(b"\n"):
        yield stream.tell(), wire.parse_record(line.decode(), Bump)


def pending_bumps(directory: Path, offset: int) -> Iterator[tuple[int, Bump]]:
    try:
        stream = (directory / JOURNAL_NAME).open("rb")
    except FileNotFoundError:
        return
    with stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        # Release the lock before the service applies or persists any change.
        pending = tuple(_entries(stream, offset))
    yield from pending


def bump_progress(directory: Path) -> BumpProgress:
    return _saved_state(directory)[0] if (directory / JOURNAL_NAME).exists() else BumpProgress()


def bump_status(directory: Path, progress: BumpProgress | None = None) -> tuple[Bump | BumpNotApplied, ...]:
    if progress is None:
        progress = bump_progress(directory)
    return (*progress.refusals, *(bump for _, bump in pending_bumps(directory, progress.offset)))


def _can_clear(directory: Path, stream: BinaryIO, unit: str) -> bool:
    progress, state = _saved_state(directory)
    value = state.get("priority")
    active = False
    if isinstance(value, dict):
        saved = records.upgrade_queue_priority(cast(dict[str, object], value))
    else:
        saved = {}
    if saved.get("kind") == "QueuePriority":
        priority = cast(records.QueuePriority, codec.parse_snapshot_value(saved, records.QueuePriority))
        active = any(entry["unit"] == unit for entry in priority.entries)
    for _, bump in _entries(stream, progress.offset):
        if bump.unit == unit:
            active = isinstance(bump.action, SetBump)
        elif isinstance(bump.action, ClearBump) and not bump.unit:
            active = False
    return active


def save_bump(directory: Path, bump: Bump) -> None:
    path = directory / JOURNAL_NAME
    override_guard.require_shared_state_write(Path(__file__), "build_queue/person_actions.py", path)
    with store.locked_append(path) as stream:
        if isinstance(bump.action, ClearBump) and bump.unit and not _can_clear(directory, stream, bump.unit):
            raise UnknownBumpUnit(f"unknown unit {bump.unit}; the queue priority did not change")
        store.append_locked(stream, codec.snapshot_value(bump))
    sync_folder(directory)
