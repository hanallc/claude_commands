"""Read the owner of a gate checkout, a registered copy, or a staff worktree."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re

from scripts.agents.remote import local_machine
from scripts.lib import copy_record, scratch
from scripts.production import staff

OWNER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")


class InvalidCopyOwner(ValueError):
    """The name does not meet the rules for a copy owner."""


@dataclass(frozen=True)
class ActiveGate:
    """A gate holds the checkout until its caller exits the context."""

    tree: Path
    source: Path
    owner: str


ACTIVE_GATES: ContextVar[tuple[ActiveGate, ...]] = ContextVar("active_gate_checkouts", default=())


def checked_owner(owner: str) -> str:
    """Return a valid copy owner name."""
    if not OWNER_PATTERN.fullmatch(owner):
        raise InvalidCopyOwner(f"{owner!r}: owner must match [a-z0-9][a-z0-9_-]*")
    return owner


def recorded_owner(source: Path, *, intent: scratch.RemovalIntentAccess) -> str:
    """Read the active gate owner, the copy owner, or the staff ID in that order."""
    for active in reversed(ACTIVE_GATES.get()):
        if active.tree == source.resolve():
            return active.owner
    record = scratch.task_of(source, intent=intent)
    if isinstance(record, copy_record.Registered):
        owner = record.fields.get("owner_unit")
        if isinstance(owner, str):
            return checked_owner(owner)
    try:
        member = staff.local_snapshot().by_worktree(local_machine(), source.resolve())
        if member is not None:
            return str(member.id)
    except staff.StaffUnreadable:
        pass
    return ""
