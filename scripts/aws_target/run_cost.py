"""Measure finished cloud gate runs and their billed cost by machine type and kind."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

from scripts.aws_target import ledger
from scripts.aws_target.measurement import number

KINDS = (('merge-gate', 'merge gates'), ('targeted-test', 'targeted tests'))


@dataclass(frozen=True)
class FinishedGateRun:
    instance_type: str
    # Empty for rows written before launches recorded a region.
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


def gate_runs(book: ledger.CostLedger, start: datetime, end: datetime) -> GateRuns:
    """Each finished run in [start, end) with its launch, and the fallbacks in the same window."""
    rows = book.launches()
    launched = {ledger.text(row, 'attempt'): row for row in rows if row['result'] == 'launched' and 'mode' in row}
    runs: list[FinishedGateRun] = []
    fallbacks: list[tuple[datetime, dict[str, object]]] = []
    omitted = 0
    for row in rows:
        at = datetime.fromtimestamp(float(str(row['time'])), UTC)
        if not start <= at < end:
            continue
        if row['result'] == 'fallback':
            fallbacks.append((at, row))
        if 'mode' not in row:
            omitted += 1
        elif row['result'] == 'finished':
            if ledger.text(row, 'attempt') not in launched:
                omitted += 1
                continue
            launch = launched[ledger.text(row, 'attempt')]
            region = str(row.get('region', launch.get('region', '')))
            runs.append(FinishedGateRun(ledger.text(row, 'instance_type'), region, ledger.text(row, 'mode'),
                                        at.date().isoformat(), number(row['gate_s']), number(row['time']) - number(launch['time'])))
    return GateRuns(runs, omitted, fallbacks)


def billed_allocations(book: ledger.CostLedger, runs: list[FinishedGateRun], start: datetime,
                       end: datetime) -> dict[tuple[str, str, str], Decimal]:
    """Split each billed day's Spot cost of a type between its run kinds by box time."""
    costs: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    billed_days: set[str] = set()
    for reading in book.read_bills():
        if not (start.date().isoformat() <= reading.day < end.date().isoformat() and ledger.is_billed_day(reading)):
            continue
        billed_days.add(reading.day)
        for usage in reading.usage_types:
            size = usage.usage_type.partition('SpotUsage:')[2]
            if size:
                costs[size, reading.day] += usage.usd
    groups: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for run in runs:
        groups[run.instance_type, run.day, run.mode] += run.box_s
    allocations: dict[tuple[str, str, str], Decimal] = {}
    for (size, at, mode), seconds in groups.items():
        if at in billed_days:
            total = sum((groups.get((size, at, kind), Decimal(0)) for kind, _label in KINDS), Decimal(0))
            allocations[size, at, mode] = costs[size, at] * seconds / total
    return allocations


def billed_runs(runs: list[FinishedGateRun], allocations: dict[tuple[str, str, str], Decimal]) -> list[FinishedGateRun]:
    return [run for run in runs if (run.instance_type, run.day, run.mode) in allocations]


def billed_cost_per_run(runs: list[FinishedGateRun], allocations: dict[tuple[str, str, str], Decimal]) -> Decimal:
    groups = {(run.instance_type, run.day, run.mode) for run in runs}
    return sum((allocations[key] for key in groups), Decimal(0)) / len(runs)


def median_run_time(seconds: list[Decimal]) -> str:
    if not seconds:
        return '—'
    minutes, remainder = divmod(int(median(seconds)), 60)
    return f'{minutes}m {remainder}s'
