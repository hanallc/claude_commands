"""Read saved AWS deployment records."""
from __future__ import annotations

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
    ACCEPTED = 'accepted'


def mutation_accepted(record: dict[str, object]) -> bool:
    return record.get('accepted') is True


def read_hana_start_acceptance(directory: Path) -> dict[int, HanaStartAcceptance]:
    """Read the acceptance of each Hana start in the provider journal."""
    starts: dict[int, HanaStartAcceptance] = {}
    for path in (directory / 'hana' / 'provider').glob('*.json'):
        try:
            record = read_record(path)
        except (OSError, ValueError):
            continue
        action, generation = record.get('action'), record.get('generation')
        if type(generation) is not int or action not in ('bootstrap', 'start'):
            continue
        if action == 'start' and mutation_accepted(record):
            starts[generation] = HanaStartAcceptance.ACCEPTED
        else:
            _ = starts.setdefault(generation, HanaStartAcceptance.AWAITING)
    return starts
