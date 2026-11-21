"""Read saved AWS deployment records."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import cast

def read_record(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise ValueError(f'{path.name}: expected an evidence record')
    return cast(dict[str, object], value)


# These EC2 replies reject StartInstances before it can start the instance.
START_REFUSAL_CODES = frozenset((
    'Blocked', 'OptInRequired', 'UnauthorizedOperation', 'Client.UnauthorizedOperation',
    'AccessDenied', 'AccessDeniedException', 'AuthFailure', 'InsufficientInstanceCapacity',
    'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded',
))


def start_was_refused(record: dict[str, object]) -> bool:
    """Read a definite refusal from the dispatched START record."""
    code = record.get('start_refusal_code')
    return (record.get('action') == 'start' and record.get('dispatched') is True
            and isinstance(code, str) and code in START_REFUSAL_CODES
            and record.get('accepted') is not True and 'accepted_at' not in record
            and 'observed_active_at' not in record)


@dataclass(frozen=True)
class HanaStartJournal:
    refused_generations: frozenset[int] = frozenset()
    possible_starts: tuple[int, ...] = ()

    def unstarted_exposures(self, exposure_generations: Sequence[int]) -> frozenset[int]:
        """Find refusals with no possible start in the same exposure."""
        unstarted: set[int] = set()
        previous = 0
        for generation in sorted(exposure_generations):
            # Each closed exposure ends a range of permit generations.
            first = bisect_right(self.possible_starts, previous)
            if (generation in self.refused_generations
                    and (first == len(self.possible_starts) or self.possible_starts[first] > generation)):
                unstarted.add(generation)
            previous = generation
        return frozenset(unstarted)


def hana_start_journal(records: Sequence[dict[str, object]]) -> HanaStartJournal:
    """Read definite refusals and possible starts from the captured records."""
    refused: set[int] = set()
    possible: set[int] = set()
    for record in records:
        action = record.get('action')
        generation = record.get('generation')
        if start_was_refused(record):
            if type(generation) is int and generation > 0:
                refused.add(generation)
            continue
        if action not in ('bootstrap', 'stop', 'observe') and (
                action == 'start' or record.get('dispatched') is True or 'dispatched_at' in record
                or record.get('accepted') is True or 'accepted_at' in record or 'observed_active_at' in record):
            if type(generation) is not int or generation < 1:
                return HanaStartJournal()
            possible.add(generation)
    return HanaStartJournal(frozenset(refused), tuple(sorted(possible)))


def read_hana_start_journal(directory: Path) -> HanaStartJournal:
    """Read definite refusals and possible starts."""
    try:
        paths = (directory / 'hana' / 'provider').iterdir()
        return hana_start_journal(tuple(read_record(path) for path in paths if path.suffix == '.json'))
    except (OSError, ValueError):
        return HanaStartJournal()
