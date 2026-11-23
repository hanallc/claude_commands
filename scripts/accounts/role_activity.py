"""Keep 24 hours of busy session counts for each role, for the Roles board line."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from scripts.lib import atomic_file, file_lock

if TYPE_CHECKING:
    from scripts.accounts import accounts

WINDOW = timedelta(hours=24)


class _SampleDocument(TypedDict):
    at: str
    busy: dict[str, int]


@dataclass(frozen=True)
class RoleBusySample:
    at: datetime
    busy: Mapping[str, int]


def history_path() -> Path:
    return Path.home() / ".local/state/accounts/role_activity.jsonl"


def _lock_path() -> Path:
    return history_path().with_suffix(".lock")


def _parse(line: str) -> RoleBusySample | None:
    try:
        document = cast(object, json.loads(line))
        if not isinstance(document, dict):
            return None
        fields = cast(dict[str, object], document)
        at, busy = fields.get("at"), fields.get("busy")
        if not isinstance(at, str) or not isinstance(busy, dict):
            return None
        counts = cast(dict[object, object], busy)
        parsed = datetime.fromisoformat(at)
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    if not all(isinstance(role, str) and isinstance(count, int) and count >= 0 for role, count in counts.items()):
        return None
    return RoleBusySample(parsed, cast(dict[str, int], counts))


def _line(sample: RoleBusySample) -> str:
    return json.dumps(_SampleDocument(at=sample.at.isoformat(timespec="seconds"), busy=dict(sample.busy)),
                      sort_keys=True) + "\n"


def _samples(text: str) -> list[RoleBusySample]:
    return [sample for line in text.splitlines() if (sample := _parse(line)) is not None]


def record(busy: Mapping[str, int], now: datetime) -> None:
    """Append one sample and drop the samples older than the window."""
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(_lock_path()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        # A sample after now stays: a clock that steps back loses no history.
        kept = [sample for sample in _samples(text) if now - WINDOW <= sample.at]
        kept.append(RoleBusySample(now, dict(busy)))
        atomic_file.write_text_atomic(path, "".join(_line(sample) for sample in kept))


def read(now: datetime) -> list[RoleBusySample]:
    """The samples of the last 24 hours, oldest first; a missing or unreadable file gives none."""
    try:
        text = history_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted((sample for sample in _samples(text) if now - WINDOW <= sample.at <= now), key=lambda sample: sample.at)


def busy_by_role(rows: Iterable[tuple[Iterable[str], Iterable[object]]]) -> dict[str, int]:
    """Sum the busy sessions of each role over the accounts; a role with an unknown count is left out."""
    from scripts.accounts import accounts

    totals: dict[str, int] = {}
    unknown: set[str] = set()
    for roles, counts in rows:
        for role, count in zip(roles, counts):
            if isinstance(count, accounts.RoleSessionCounts):
                totals[role] = totals.get(role, 0) + count.busy
            elif isinstance(count, accounts.UnknownRoleCounts):
                unknown.add(role)
    return {role: busy for role, busy in totals.items() if role not in unknown}


def sample(reading: accounts.StatusReading, now: datetime) -> None:
    """Record the busy counts of the same reading the Roles board shows; a role with an unknown count is left out."""
    record(busy_by_role((row.roles, row.role_counts) for row in reading.rows), now)
