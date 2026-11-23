"""Keep the daily mean of the lowest zone Spot price of each type and region, from the hourly snapshot.

The run estimate can then compare each billed day with the Spot price of that day."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import cast

from scripts.aws_target import ledger
from scripts.lib.atomic_file import write_text_atomic

FILE_NAME = 'spot-price-days.json'

DayKey = tuple[str, str, str]


@dataclass(frozen=True)
class PriceDay:
    instance_type: str
    region: str
    # The UTC day of the snapshot reads.
    day: str
    usd_sum: Decimal
    reads: int

    @property
    def mean_usd_per_hour(self) -> Decimal:
        return self.usd_sum / self.reads


def read_days(directory: Path) -> dict[DayKey, PriceDay]:
    """The saved days by type, region and day; no file means no days."""
    path = directory / FILE_NAME
    if not path.exists():
        return {}
    document = ledger.mapping(cast(object, json.loads(path.read_text())))
    days: dict[DayKey, PriceDay] = {}
    for row in ledger.rows(document, 'days'):
        entry = PriceDay(ledger.text(row, 'instance_type'), ledger.text(row, 'region'), ledger.text(row, 'day'),
                         Decimal(ledger.text(row, 'usd_sum')), int(ledger.text(row, 'reads')))
        if entry.reads < 1:
            raise ValueError(f'{FILE_NAME}: {entry.instance_type} {entry.region} {entry.day} has no reads')
        days[entry.instance_type, entry.region, entry.day] = entry
    return days


def record(directory: Path, prices: Sequence[ledger.RegionalSpotPrice], now: int, keep_days: int) -> None:
    """Add one read of the lowest zone price of each type and region in prices, and drop days past keep_days."""
    today = datetime.fromtimestamp(now, UTC).date()
    lowest: dict[tuple[str, str], Decimal] = {}
    for row in prices:
        key = row.instance_type, row.region
        lowest[key] = min(lowest.get(key, row.usd_per_hour), row.usd_per_hour)
    days = read_days(directory)
    for (size, region), usd in lowest.items():
        old = days.get((size, region, today.isoformat()))
        days[size, region, today.isoformat()] = PriceDay(size, region, today.isoformat(),
                                                          (old.usd_sum if old else Decimal(0)) + usd,
                                                          (old.reads if old else 0) + 1)
    first = (today - timedelta(days=keep_days - 1)).isoformat()
    kept = sorted((entry for entry in days.values() if entry.day >= first),
                  key=lambda entry: (entry.day, entry.instance_type, entry.region))
    write_text_atomic(directory / FILE_NAME, json.dumps({'days': [
        {'instance_type': entry.instance_type, 'region': entry.region, 'day': entry.day,
         'usd_sum': str(entry.usd_sum), 'reads': str(entry.reads)} for entry in kept]}, sort_keys=True) + '\n')

