"""The owner of a checkout: a gate that holds it, its copy record, or the staff member that holds it.

This module imports no build_queue module, so the queue can read an owner with no import cycle.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re

from scripts.lib import copy_record, scratch

OWNER = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class ActiveGate:
    """A gate holds this slot through its caller's context."""

    tree: Path
    source: Path
    owner: str


ACTIVE_GATES: ContextVar[tuple[ActiveGate, ...]] = ContextVar("active_gate_checkouts", default=())


class CopyError(Exception):
    """An invalid request or an unavailable checkout."""

    exit_code: int = 2


def checked_owner(owner: str) -> str:
    if not OWNER.fullmatch(owner):
        raise CopyError(f"{owner!r}: owner must match [a-z0-9][a-z0-9_-]*")
    return owner


def recorded_owner(source: Path, *,
                   intent: scratch.RemovalIntentAccess = scratch.RemovalIntentAccess.RECOVER) -> str:
    """Use the standing checkout owner, copy owner or staff owner; empty when none is recorded."""
    for active in reversed(ACTIVE_GATES.get()):
        if active.tree == source.resolve():
            return active.owner
    record = scratch.task_of(source, intent=intent)
    if isinstance(record, copy_record.Registered):
        owner = record.fields.get("owner_unit")
        if isinstance(owner, str):
            return checked_owner(owner)
    from scripts.production import staff
    from scripts.agents.remote import local_machine
    try:
        member = staff.local_snapshot().by_worktree(local_machine(), source.resolve())
        if member is not None:
            return str(member.id)
    except staff.StaffUnreadable:
        pass
    return ""
