"""Read saved AWS deployment records."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import cast


def read_record(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise ValueError(f'{path.name}: expected an evidence record')
    return cast(dict[str, object], value)


class HanaStartAcceptance(Enum):
    LEGACY = 'legacy'
    AWAITING = 'awaiting'


@dataclass(frozen=True)
class HanaStartAccepted:
    at: int


def mutation_accepted(record: dict[str, object]) -> bool:
    return record.get('accepted') is True


def start_was_dispatched(record: dict[str, object]) -> bool:
    return record.get('action') == 'start' and record.get('dispatched') is True


@dataclass(frozen=True)
class HanaStartJournal:
    starts: dict[int, HanaStartAcceptance | HanaStartAccepted]
    dispatched_starts: set[int]
    actionless_cancellations: set[int]


def read_hana_start_journal(directory: Path) -> HanaStartJournal:
    """Read start evidence and legacy cancellations from the provider journal."""
    starts: dict[int, HanaStartAcceptance | HanaStartAccepted] = {}
    dispatched_starts: set[int] = set()
    actionless_cancellations: set[int] = set()
    for path in (directory / 'hana' / 'provider').glob('*.json'):
        try:
            record = read_record(path)
        except (OSError, ValueError):
            continue
        action, generation = record.get('action'), record.get('generation')
        if type(generation) is not int or generation < 0:
            continue
        if action == 'bootstrap' or action == 'start':
            _ = starts.setdefault(generation, HanaStartAcceptance.AWAITING)
        elif action is None and record.get('cancelled') is True:
            actionless_cancellations.add(generation)
        if not start_was_dispatched(record):
            continue
        dispatched_starts.add(generation)
        at = record.get('accepted_at')
        if mutation_accepted(record) and type(at) is int and at >= 0:
            previous = starts[generation]
            if not isinstance(previous, HanaStartAccepted) or at < previous.at:
                starts[generation] = HanaStartAccepted(at)
    return HanaStartJournal(starts, dispatched_starts, actionless_cancellations)
