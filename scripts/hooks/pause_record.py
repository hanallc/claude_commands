"""Read and update conversation pause records."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import cast

from scripts.hooks import hook_budget
from scripts.hooks.hook_budget import (
    record_path as record_path,
    scheduled_prompts_path as scheduled_prompts_path,
)

@dataclass(frozen=True)
class ScheduledTaskPrompt:
    id: str
    prompt: str
    recurring: bool


@dataclass(frozen=True)
class Replying:
    user_wrote_at: int


@dataclass(frozen=True)
class Quiet:
    user_wrote_at: int
    reply_ended_at: int


@dataclass(frozen=True)
class QuestionPending:
    due_at: int


@dataclass(frozen=True)
class QuestionNotRead:
    pass


@dataclass(frozen=True)
class QuestionRead:
    replies_ended: int


QuestionReading = QuestionNotRead | QuestionRead


@dataclass(frozen=True)
class Asked:
    asked_at: int
    reading: QuestionReading


@dataclass(frozen=True)
class KeptOff:
    pass


@dataclass(frozen=True)
class Returned:
    returned_at: int


PausePhase = Replying | Quiet | QuestionPending | Asked | KeptOff | Returned


@dataclass(frozen=True)
class PauseRecord:
    session_id: str
    instances: tuple[str, ...]
    phase: PausePhase


class NoPauseRecord(Enum):
    ABSENT = "no pause record"


RecordLookup = PauseRecord | NoPauseRecord


def now_epoch() -> int:
    override = os.environ.get("CONVERSATION_PAUSE_NOW_EPOCH")
    return int(override) if override is not None else int(time.time())


def record_scheduled_prompts(session_id: str, prompts: tuple[ScheduledTaskPrompt, ...]) -> None:
    """Retain missing one-shot tasks until their prompts arrive."""
    current_ids = {task.id for task in prompts}
    pending = tuple(task for task in read_scheduled_task_prompts(session_id)
                    if not task.recurring and task.id not in current_ids)
    write_scheduled_task_prompts(session_id, prompts + pending)


def write_scheduled_task_prompts(session_id: str, prompts: tuple[ScheduledTaskPrompt, ...]) -> None:
    destination = scheduled_prompts_path(session_id)
    if not prompts:
        destination.unlink(missing_ok=True)
        return
    root = destination.parent
    root.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=root,
            prefix=f".scheduled-prompts-{os.getppid()}-{os.getpid()}-",
            delete=False,
            encoding="utf-8",
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump([{"id": task.id, "prompt": task.prompt, "recurring": task.recurring}
                       for task in prompts], temporary)
            _ = temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_scheduled_task_prompts(session_id: str) -> tuple[ScheduledTaskPrompt, ...]:
    try:
        raw = cast(object, json.loads(
            scheduled_prompts_path(session_id).read_text(encoding="utf-8")
        ))
    except (OSError, UnicodeError, ValueError):
        return ()
    if not isinstance(raw, list):
        return ()
    items = cast(list[object], raw)
    prompts: list[ScheduledTaskPrompt] = []
    for item in items:
        if not isinstance(item, dict):
            return ()
        fields = cast(dict[str, object], item)
        task_id, prompt, recurring = fields.get("id"), fields.get("prompt"), fields.get("recurring")
        if not isinstance(task_id, str) or not isinstance(prompt, str) or not isinstance(recurring, bool):
            return ()
        prompts.append(ScheduledTaskPrompt(task_id, prompt, recurring))
    return tuple(prompts)


def read_scheduled_prompts(session_id: str) -> tuple[str, ...]:
    return tuple(task.prompt for task in read_scheduled_task_prompts(session_id))


def _phase_json(phase: PausePhase) -> dict[str, object]:
    if isinstance(phase, Replying):
        return {"kind": "replying", "user_wrote_at": phase.user_wrote_at}
    if isinstance(phase, Quiet):
        return {"kind": "quiet", "user_wrote_at": phase.user_wrote_at,
                "reply_ended_at": phase.reply_ended_at}
    if isinstance(phase, QuestionPending):
        return {"kind": "question_pending", "due_at": phase.due_at}
    if isinstance(phase, Asked):
        reading: dict[str, object]
        if isinstance(phase.reading, QuestionNotRead):
            reading = {"kind": "not_read"}
        else:
            reading = {"kind": "read", "replies_ended": phase.reading.replies_ended}
        return {
            "kind": "asked",
            "asked_at": phase.asked_at,
            "reading": reading,
        }
    if isinstance(phase, KeptOff):
        return {"kind": "kept_off"}
    return {"kind": "returned", "returned_at": phase.returned_at}


def phase_kind(phase: PausePhase) -> str:
    return cast(str, _phase_json(phase)["kind"])


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {field}")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"invalid {field}")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"invalid {field}")
    return tuple(cast(list[str], items))


def _question_reading_from_json(value: object) -> QuestionReading:
    if not isinstance(value, dict):
        raise ValueError("invalid reading")
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind == "not_read":
        return QuestionNotRead()
    if kind == "read":
        return QuestionRead(_integer(fields.get("replies_ended"), "replies_ended"))
    raise ValueError("invalid reading kind")


def _phase_from_json(value: object) -> PausePhase:
    if not isinstance(value, dict):
        raise ValueError("invalid phase")
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind == "replying":
        return Replying(_integer(fields.get("user_wrote_at"), "user_wrote_at"))
    if kind == "quiet":
        return Quiet(_integer(fields.get("user_wrote_at"), "user_wrote_at"),
                     _integer(fields.get("reply_ended_at"), "reply_ended_at"))
    if kind == "question_pending":
        return QuestionPending(_integer(fields.get("due_at"), "due_at"))
    if kind == "asked":
        reading = (
            _question_reading_from_json(fields.get("reading"))
            if "reading" in fields
            else QuestionRead(_integer(fields.get("replies_ended_since_question", 1),
                                       "replies_ended_since_question"))
        )
        return Asked(
            _integer(fields.get("asked_at"), "asked_at"),
            reading,
        )
    if kind == "kept_off":
        return KeptOff()
    if kind == "returned":
        return Returned(_integer(fields.get("returned_at"), "returned_at"))
    raise ValueError("invalid phase kind")


def read_record(path: Path) -> PauseRecord:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid conversation pause record: {path}")
    fields = cast(dict[str, object], raw)
    session_id = fields.get("session_id")
    if not isinstance(session_id, str):
        raise ValueError(f"invalid conversation pause record: {path}")
    try:
        # A record written while reply footers existed also has `footers`, which nothing reads now.
        return PauseRecord(session_id, _string_tuple(fields["instances"], "instances"),
                           _phase_from_json(fields["phase"]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid conversation pause record: {path}") from error


def write_record(record: PauseRecord) -> None:
    root = hook_budget.pause_state_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = record_path(record.session_id)
    payload: dict[str, object] = {
        "session_id": record.session_id,
        "instances": list(record.instances),
        "phase": _phase_json(record.phase),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=root, prefix=".conversation-pause-", delete=False,
                                         encoding="utf-8") as temporary:
            json.dump(payload, temporary)
            _ = temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def record_lock() -> Generator[None, None, None]:
    root = hook_budget.pause_state_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def lookup_record(session_id: str) -> RecordLookup:
    path = record_path(session_id)
    return read_record(path) if path.exists() else NoPauseRecord.ABSENT


def mark_reply_ended(session_id: str, now: int) -> None:
    with record_lock():
        current = lookup_record(session_id)
        if not isinstance(current, PauseRecord):
            return
        phase = current.phase
        if isinstance(phase, Replying):
            write_record(replace(
                current,
                phase=Quiet(phase.user_wrote_at, now),
            ))
        elif isinstance(phase, Asked) and isinstance(phase.reading, QuestionRead):
            write_record(replace(
                current,
                phase=replace(
                    phase,
                    reading=replace(
                        phase.reading,
                        replies_ended=phase.reading.replies_ended + 1,
                    ),
                ),
            ))
