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


@dataclass(frozen=True)
class HanaStartJournal:
    cancelled_generations: frozenset[int] = frozenset()
    possible_starts: tuple[int, ...] = ()

    def cancelled_exposures(self, exposure_generations: Sequence[int]) -> frozenset[int]:
        """Find cancellations with no possible start in the same exposure."""
        cancelled: set[int] = set()
        previous = 0
        for generation in sorted(exposure_generations):
            # Each closed exposure ends a range of permit generations.
            first = bisect_right(self.possible_starts, previous)
            if (generation in self.cancelled_generations
                    and (first == len(self.possible_starts) or self.possible_starts[first] > generation)):
                cancelled.add(generation)
            previous = generation
        return frozenset(cancelled)


def read_hana_start_journal(directory: Path) -> HanaStartJournal:
    """Read proof of cancellation and evidence that can keep an exposure chargeable."""
    cancelled: set[int] = set()
    possible: set[int] = set()
    try:
        paths = tuple((directory / 'hana' / 'provider').iterdir())
    except OSError:
        return HanaStartJournal()
    for path in paths:
        if path.suffix != '.json':
            continue
        try:
            record = read_record(path)
        except (OSError, ValueError):
            return HanaStartJournal()
        action = record.get('action')
        generation = record.get('generation')
        if action not in ('bootstrap', 'stop', 'observe') and (
                record.get('dispatched') is True or 'dispatched_at' in record
                or record.get('accepted') is True or 'accepted_at' in record):
            if type(generation) is not int or generation < 1:
                return HanaStartJournal()
            possible.add(generation)
        if (action in (None, 'start', 'bootstrap') and record.get('cancelled') is True
                and type(generation) is int and generation > 0):
            cancelled.add(generation)
    return HanaStartJournal(frozenset(cancelled), tuple(sorted(possible)))
