"""Measure finished cloud gate runs and their billed cost by machine type and kind."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

from scripts.aws_target import ledger
from scripts.aws_target.measurement import number

KINDS = (('merge-gate', 'merge gates'), ('targeted-test', 'targeted tests'))


@dataclass(frozen=True)
class RunScope:
    """The run kinds that one cost table counts."""
    name: str
    # The filter label on the AWS prices page.
    label: str
    # One run of this scope, as the table headers and sentences name it.
    noun: str
    modes: tuple[str, ...]


SCOPES = (RunScope('merge-gate', 'Merge gates', 'merge gate', ('merge-gate',)),
          RunScope('targeted-test', 'Targeted runs', 'targeted test', ('targeted-test',)),
          RunScope('both', 'Both', 'run', tuple(mode for mode, _label in KINDS)))

# AWS had no Spot box for this type and zone at this price.
ZONE_REFUSALS = frozenset({'InsufficientInstanceCapacity', 'InsufficientSpotCapacity', 'SpotMaxPriceTooLow'})
# The region has no Spot quota room.
QUOTA_REFUSALS = frozenset({'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded'})


@dataclass(frozen=True)
class FinishedGateRun:
    instance_type: str
    # Rows written before launches recorded a region ran in the home region.
    region: str
    mode: str
    day: str
    test_s: Decimal
    # From the launch row to the finished row: boot, test and the box stop.
    box_s: Decimal


@dataclass(frozen=True)
class GateRuns:
    runs: list[FinishedGateRun]
    omitted: int
    fallbacks: list[tuple[datetime, dict[str, object]]]


def gate_runs(book: ledger.CostLedger, start: datetime, end: datetime, home: str) -> GateRuns:
    """Each finished run in [start, end) with its launch, and the fallbacks in the same window."""
    return runs_from_rows(book.launches(), start, end, home)


def runs_from_rows(rows: Sequence[dict[str, object]], start: datetime, end: datetime, home: str) -> GateRuns:
    launched = {str(row.get('attempt', '')): row for row in rows if row.get('result') == 'launched' and 'mode' in row}
    runs: list[FinishedGateRun] = []
    fallbacks: list[tuple[datetime, dict[str, object]]] = []
    omitted = 0
    for row in rows:
        at = datetime.fromtimestamp(float(str(row['time'])), UTC)
        if not start <= at < end:
            continue
        if row.get('result') == 'fallback':
            fallbacks.append((at, row))
        if 'mode' not in row:
            omitted += 1
        elif row.get('result') == 'finished':
            launch = launched.get(str(row.get('attempt', '')))
            size, mode = row.get('instance_type'), row.get('mode')
            # Placement reads this too, so a row it cannot use is counted, not raised.
            if launch is None or not isinstance(size, str) or not size or not isinstance(mode, str):
                omitted += 1
                continue
            region = str(row.get('region') or launch.get('region') or home)
            runs.append(FinishedGateRun(size, region, mode, at.date().isoformat(), number(row['gate_s']),
                                        number(row['time']) - number(launch['time'])))
    return GateRuns(runs, omitted, fallbacks)


BilledKey = tuple[str, str, str, str]


def spot_bills(book: ledger.CostLedger, start: datetime, end: datetime) -> dict[tuple[str, str, str], tuple[Decimal, Decimal]]:
    """The billed Spot USD and box hours for each type, region and day in [start, end)."""
    bills: dict[tuple[str, str, str], tuple[Decimal, Decimal]] = {}
    for reading in book.read_bills():
        if not (start.date().isoformat() <= reading.day < end.date().isoformat() and ledger.is_billed_day(reading)):
            continue
        for usage in reading.usage_types:
            size = usage.usage_type.partition('SpotUsage:')[2]
            region = ledger.usage_region(usage.usage_type, 'SpotUsage:')
            if size and region is not None:
                usd, hours = bills.get((size, region, reading.day), (Decimal(0), Decimal(0)))
                bills[size, region, reading.day] = usd + usage.usd, hours + usage.quantity
    return bills


def paid_per_hour(bills: dict[tuple[str, str, str], tuple[Decimal, Decimal]]) -> dict[tuple[str, str], Decimal]:
    """The billed Spot USD per box hour of each type and region."""
    totals: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for (size, region, _day), (usd, hours) in bills.items():
        paid, used = totals.get((size, region), (Decimal(0), Decimal(0)))
        totals[size, region] = paid + usd, used + hours
    return {key: usd / hours for key, (usd, hours) in totals.items() if hours > 0}


def billed_allocations(bills: dict[tuple[str, str, str], tuple[Decimal, Decimal]],
                       runs: list[FinishedGateRun]) -> dict[BilledKey, Decimal]:
    """Split each billed day's Spot cost of a type in a region between its run kinds by box time."""
    groups: dict[BilledKey, Decimal] = defaultdict(Decimal)
    for run in runs:
        groups[run.instance_type, run.region, run.day, run.mode] += run.box_s
    allocations: dict[BilledKey, Decimal] = {}
    for (size, region, at, mode), seconds in groups.items():
        if (size, region, at) in bills:
            total = sum((groups.get((size, region, at, kind), Decimal(0)) for kind, _label in KINDS), Decimal(0))
            allocations[size, region, at, mode] = bills[size, region, at][0] * seconds / total
    return allocations


def billed_runs(runs: list[FinishedGateRun], allocations: dict[BilledKey, Decimal]) -> list[FinishedGateRun]:
    return [run for run in runs if (run.instance_type, run.region, run.day, run.mode) in allocations]


def billed_cost_per_run(runs: list[FinishedGateRun], allocations: dict[BilledKey, Decimal]) -> Decimal:
    groups = {(run.instance_type, run.region, run.day, run.mode) for run in runs}
    return sum((allocations[key] for key in groups), Decimal(0)) / len(runs)


def median_run_time(seconds: list[Decimal]) -> str:
    if not seconds:
        return '—'
    minutes, remainder = divmod(int(median(seconds)), 60)
    return f'{minutes}m {remainder}s'


@dataclass(frozen=True)
class RunEvidence:
    """Box times and refusals by type and region, for the expected cost of one run."""
    by_region: dict[tuple[str, str, str], list[Decimal]]
    by_type: dict[tuple[str, str], list[Decimal]]
    by_mode: dict[str, list[Decimal]]
    refused: dict[tuple[str, str], int]
    launched: dict[tuple[str, str], int]
    # Median seconds from a refused launch request to the next request of the same attempt.
    refusal_wait_s: Decimal

    def box_seconds(self, size: str, region: str, modes: tuple[str, ...]) -> tuple[list[Decimal], bool]:
        """The region's own box times, else the type's times in all regions (False)."""
        own = [time for mode in modes for time in self.by_region.get((size, region, mode), [])]
        return (own, True) if own else ([time for mode in modes for time in self.by_type.get((size, mode), [])], False)

    def refusals_per_launch(self, size: str, region: str) -> tuple[Decimal | None, int, int]:
        """Refused requests for each launched box, with the counts it comes from."""
        refused, launched = self.refused.get((size, region), 0), self.launched.get((size, region), 0)
        if not refused + launched:
            return None, 0, 0
        return Decimal(refused) / max(launched, 1), refused, launched

    def expected_usd(self, price: Decimal, size: str, region: str, modes: tuple[str, ...], wait_usd_per_minute: Decimal,
                     default_seconds: Decimal) -> Decimal:
        """Spot price for the median box time, plus the wait cost of the expected refusals first."""
        seconds, _own = self.box_seconds(size, region, modes)
        seconds = seconds or [time for mode in modes for time in self.by_mode.get(mode, [])]
        box = Decimal(median(seconds)) if seconds else default_seconds
        refusals = self.refusals_per_launch(size, region)[0] or Decimal(0)
        return price * box / 3600 + refusals * self.refusal_wait_s * wait_usd_per_minute / 60


def run_evidence(rows: Sequence[dict[str, object]], start: datetime, end: datetime, home: str) -> RunEvidence:
    by_region: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    by_type: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    by_mode: dict[str, list[Decimal]] = defaultdict(list)
    for run in runs_from_rows(rows, start, end, home).runs:
        by_region[run.instance_type, run.region, run.mode].append(run.box_s)
        by_type[run.instance_type, run.mode].append(run.box_s)
        by_mode[run.mode].append(run.box_s)
    refused: dict[tuple[str, str], int] = defaultdict(int)
    launched: dict[tuple[str, str], int] = defaultdict(int)
    attempts: dict[str, list[tuple[Decimal, str]]] = defaultdict(list)
    low, high = Decimal(str(start.timestamp())), Decimal(str(end.timestamp()))
    for row in rows:
        at = number(row['time'])
        if not low <= at < high:
            continue
        result = row.get('result')
        attempts[str(row.get('attempt', ''))].append((at, str(result)))
        key = str(row.get('instance_type', '')), str(row.get('region') or home)
        if result == 'launched':
            launched[key] += 1
        elif result == 'refused' and row.get('error_code') in ZONE_REFUSALS | QUOTA_REFUSALS:
            refused[key] += 1
    waits: list[Decimal] = []
    for events in attempts.values():
        ordered = sorted(events)
        waits.extend(after[0] - before[0] for before, after in zip(ordered, ordered[1:]) if before[1] == 'refused')
    return RunEvidence(dict(by_region), dict(by_type), dict(by_mode), dict(refused), dict(launched),
                       Decimal(median(waits)) if waits else Decimal(0))
