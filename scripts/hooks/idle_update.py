#!/usr/bin/env python3
"""Tell an idle producer, showrunner or unit session which short form its text to nate takes.

The prompt hook calls `typed` on each prompt nate types in the session and `context` on every other prompt.
The rule is in `docs/idle_update.md`. The limits are in `config/idle_update.conf`, read at use.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from scripts.hooks import conversation_pause
from scripts.lib import conf
from scripts.production import staff

ROLES: frozenset[staff.Kind] = frozenset({"producer", "showrunner", "unit"})
RULE = "~/.claude/docs/idle_update.md"


class IdleRecord(TypedDict):
    typed_at: float
    updated_at: float | None


@dataclass(frozen=True)
class Limits:
    after_seconds: int
    renew_seconds: int


def limits() -> Limits:
    settings = conf.read("idle_update")
    return Limits(settings.whole("after_minutes") * 60, settings.whole("renew_minutes") * 60)


def has_role(record: staff.StaffSnapshot) -> bool:
    return any(row.kind in ROLES for row in record.rows)


def state_dir() -> Path:
    """Keep the records beside the conversation pause records, so one test override covers both."""
    return conversation_pause.state_root() / "idle-update"


def _record_path(session_id: str) -> Path:
    return state_dir() / f"{session_id}.json"


def _read(session_id: str) -> IdleRecord | None:
    try:
        value = cast(object, json.loads(_record_path(session_id).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    fields = cast(dict[str, object], value)
    typed_at, updated_at = fields.get("typed_at"), fields.get("updated_at")
    if not isinstance(typed_at, int | float) or not (updated_at is None or isinstance(updated_at, int | float)):
        return None
    return {"typed_at": float(typed_at), "updated_at": None if updated_at is None else float(updated_at)}


def _write(session_id: str, record: IdleRecord) -> None:
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=f".{session_id}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            _ = handle.write(json.dumps(record))
        os.replace(temporary, _record_path(session_id))
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def typed(session_id: str, now: float) -> None:
    """Record that nate typed in the session; idle update mode ends."""
    _write(session_id, {"typed_at": now, "updated_at": None})


def context(session_id: str, now: float, limit: Limits) -> str:
    """Return the hook context for a prompt nate did not type, or an empty text outside the mode."""
    record = _read(session_id)
    if record is None:
        # No typed time yet: start the count at this prompt.
        typed(session_id, now)
        return ""
    idle = now - record["typed_at"]
    if idle < limit.after_seconds:
        return ""
    head = (f"Idle update mode: nate typed nothing in this session for {int(idle // 60)} min. "
            "Keep doing all your work. ")
    updated = record["updated_at"]
    if updated is not None and now - updated < limit.renew_seconds:
        due = int((updated + limit.renew_seconds - now) // 60) + 1
        return head + f'End this turn with "—" alone. The next update is due in {due} min. Rule: {RULE}.'
    _write(session_id, {**record, "updated_at": now})
    if updated is None:
        return head + ("Your text to nate this turn is one short update: what you last had on deck with him, "
                       "in a few words, and the question whether he is willing to look at it. "
                       f'Show it in full only after he says yes. With nothing on deck, end with "—" alone. Rule: {RULE}.')
    return head + ("Your text to nate this turn is one short update with only the news since your last update, "
                   "and the question whether he is willing to look at what you have on deck. "
                   f'With no news, end with "—" alone. Rule: {RULE}.')
