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


def read_hana_start_acceptance(directory: Path) -> dict[int, HanaStartAcceptance | HanaStartAccepted]:
    """Read the acceptance of each Hana start in the provider journal."""
    starts: dict[int, HanaStartAcceptance | HanaStartAccepted] = {}
    for path in (directory / 'hana' / 'provider').glob('*.json'):
        try:
            record = read_record(path)
        except (OSError, ValueError):
            continue
        action, generation = record.get('action'), record.get('generation')
        if type(generation) is not int or generation < 0:
            continue
        if action == 'bootstrap' or action == 'start' or (action is None and record.get('cancelled') is True):
            _ = starts.setdefault(generation, HanaStartAcceptance.AWAITING)
        at = record.get('accepted_at')
        if (start_was_dispatched(record) and mutation_accepted(record)
                and type(at) is int and at >= 0):
            previous = starts[generation]
            if not isinstance(previous, HanaStartAccepted) or at < previous.at:
                starts[generation] = HanaStartAccepted(at)
    return starts
