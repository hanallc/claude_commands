"""Read the local Spot snapshot and the run ledger into tables, and format them for a terminal."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import textwrap

from scripts.aws_target import ledger, run_cost
from scripts.aws_target.aws_target import SnapshotFailure, largest_gate_vcpus, read_snapshot, snapshot_failures
from scripts.aws_target.terminal_table import aligned_table
from scripts.lib import conf


@dataclass(frozen=True)
class Table:
    title: str
    headers: tuple[str, ...]
    # The indexes of the columns that hold numbers.
    right: frozenset[int]
    rows: tuple[tuple[str, ...], ...]


def minutes_ago(seconds: int) -> str:
    return f'{seconds // 60} min ago'


def runs_text(count: int) -> str:
    return f'{count} run' + ('' if count == 1 else 's')


@dataclass(frozen=True)
class RegionRead:
    """The state of one region's snapshot."""
    region: str
    # Seconds since the last good read; None when no read was ever taken.
    age_s: int | None
    stale: bool
    # The last failed read and its age in seconds.
    failure: SnapshotFailure | None
    failure_age_s: int

    @property
    def note(self) -> str:
        """The short mark beside each value of the region."""
        if self.failure:
            return 'read failed'
        if self.age_s is None:
            return 'no snapshot'
        return f'old {self.age_s // 60} min' if self.stale else ''

    @property
    def detail(self) -> str:
        """The full sentence about the region's snapshot."""
        if self.failure:
            failure = self.failure
            stamp = datetime.fromtimestamp(failure.at, UTC).isoformat()
            text = (f'last read failed at {stamp} ({minutes_ago(self.failure_age_s)}): '
                    f'{failure.read}: {failure.error}: {failure.reason or "no error detail"}')
            return text + (f'; last good read {minutes_ago(self.age_s)}' if self.age_s is not None else '')
        if self.age_s is None:
            return 'no snapshot was ever taken'
        return f'read {minutes_ago(self.age_s)}' + (' (stale)' if self.stale else '')


@dataclass(frozen=True)
class Cheapest:
    usd: Decimal
    size: str
    region: str


@dataclass(frozen=True)
class RunCosts:
    """The cost per run of one scope: one table per type."""
    scope: run_cost.RunScope
    explanation: str
    tables: tuple[Table, ...]
    cheapest: Cheapest | None

    @property
    def summary(self) -> str:
        if self.cheapest is None:
            return f'Cheapest {self.scope.noun}: no estimate; the snapshot has no price or the ledger has no runs'
        cheapest = self.cheapest
        return f'Cheapest {self.scope.noun}: {cheapest.size} in {cheapest.region}, {cheapest.usd:.4f} USD (estimate)'


@dataclass(frozen=True)
class SpotTables:
    regions: tuple[RegionRead, ...]
    prices: Table
    quotas: Table
    # One entry for each scope in run_cost.SCOPES, in that order.
    runs: tuple[RunCosts, ...]

    @property
    def snapshot_lines(self) -> tuple[str, ...]:
        """One line when every region has the same snapshot state and minute; else one line per region."""
        details = {read.detail for read in self.regions}
        if len(details) == 1 and not any(read.failure for read in self.regions):
            return (f'Snapshot {details.pop()}',)
        return tuple(f'{read.region}: {read.detail}' for read in self.regions)


def spot_tables(directory: Path, now: int, *, settings_path: Path | None = None) -> SpotTables:
    """Read prices, quotas, run costs, and the reason for each unavailable value."""
    values = conf.read_or_repository('cloud_gate')
    configuration = conf.read(settings_path) if settings_path else conf.read_or_repository('aws_target')
    regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
    standard = tuple(dict.fromkeys((*values.words('instance_types'), *values.words('price_compare_instance_types'))))
    gpu = values.words('gpu_instance_types')
    sizes = tuple(dict.fromkeys((*standard, *gpu)))
    document = read_snapshot(directory)
    readings = {ledger.text(row, 'region'): row for row in ledger.rows(document, 'regions')} if 'regions' in document else {}
    failures = {row.region: row for row in snapshot_failures(document)}
    minimum: dict[tuple[str, str], Decimal] = {}
    for row in ledger.rows(document, 'prices'):
        key = ledger.text(row, 'region'), ledger.text(row, 'instance_type')
        price = conf.decimal(ledger.text(row, 'usd_per_hour'))
        minimum[key] = min(minimum.get(key, price), price)
    reads: list[RegionRead] = []
    for region in regions:
        reading = readings.get(region)
        failure = failures.get(region)
        age = now - ledger.instant(reading, 'read_at') if reading else None
        stale = age is not None and (age < 0 or age > configuration.whole('price_max_age_seconds'))
        reads.append(RegionRead(region, age, stale, failure, now - failure.at if failure else 0))
    notes = {read.region: read.note for read in reads}

    def price_cell(size: str, region: str) -> str:
        price = minimum.get((region, size))
        note = notes[region]
        return f'{price:.4f}' + (f' [{note}]' if note else '') if price is not None else note or 'not sold here'

    prices = Table('Spot prices (USD/h; lowest zone)', ('type', *regions), frozenset(range(1, len(regions) + 1)),
                   tuple((size, *(price_cell(size, region) for region in regions)) for size in sizes))
    fits = tuple((size, largest_gate_vcpus((size,)), 'gpu_vcpus' if size in gpu else 'standard_vcpus') for size in sizes)
    quota_rows: list[tuple[str, ...]] = []
    for region in regions:
        reading = readings.get(region)
        note = notes[region]
        suffix = f' [{note}]' if note else ''
        quota_rows.append((region, *(f'{conf.decimal(ledger.text(reading, field)).normalize():f} vCPUs{suffix}'
                                     if reading else note for field in ('standard_vcpus', 'gpu_vcpus')),
                           *(f'{int(conf.decimal(ledger.text(reading, field)) // vcpus)} boxes{suffix}'
                             if reading and vcpus else note or 'unknown vCPU count' for _size, vcpus, field in fits)))
    quotas = Table('Spot quotas and boxes that fit', ('region', 'Standard quota', 'G and VT quota',
                                                      *(f'{size} ({vcpus or "?"} vCPUs)' for size, vcpus, _field in fits)),
                   frozenset(range(1, 3 + len(fits))), tuple(quota_rows))
    return SpotTables(tuple(reads), prices, quotas, run_costs(
        directory, now, standard, frozenset(values.words('instance_types')), regions, minimum,
        configuration.whole('run_cost_days'), configuration.text('region')))


def run_costs(directory: Path, now: int, sizes: tuple[str, ...], candidates: frozenset[str], regions: tuple[str, ...],
              minimum: dict[tuple[str, str], Decimal], days: int, home: str) -> tuple[RunCosts, ...]:
    """For each scope, one table per type and one row per region: price, run time, expected and paid cost of a run."""
    end = datetime.fromtimestamp(now, UTC)
    start = end - timedelta(days=days)
    book = ledger.CostLedger(directory)
    rows = book.launches()
    runs = run_cost.runs_from_rows(rows, start, end, home).runs
    evidence = run_cost.run_evidence(rows, start, end, home)
    bills = run_cost.spot_bills(book, start, end)
    paid_hourly = run_cost.paid_per_hour(bills)
    allocations = run_cost.billed_allocations(bills, runs)
    values = conf.read_or_repository('cloud_gate')
    wait_cost = conf.decimal(values.text('gate_wait_usd_per_minute'))
    default_seconds = Decimal(values.whole('lifetime_minutes') * 60)
    scopes: list[RunCosts] = []
    for scope in run_cost.SCOPES:
        explanation = (f'USD per {scope.noun}, last {days} days. Estimate: lowest zone Spot price x median box '
                       + f'time (boot included), plus refusal wait at {wait_cost} USD/min '
                       + f'({evidence.refusal_wait_s:.0f} s each). Paid: the AWS bill for the type and region, '
                       + 'split between runs by box time. Refused: capacity and quota refusals.')
        cheapest: Cheapest | None = None
        headers = ('region', 'Spot USD/h', scope.noun, 'estimate', 'paid USD/h', 'paid USD/run', 'refused', 'launched')
        tables: list[Table] = []
        for size in sizes:
            table: list[tuple[str, ...]] = []
            for region in regions:
                price = minimum.get((region, size))
                seconds, own = evidence.box_seconds(size, region, scope.modes)
                duration = (f'{run_cost.median_run_time(seconds)} ({runs_text(len(seconds))}'
                            + ('' if own else ' in other regions') + ')') if seconds else 'no runs'
                estimate = (evidence.expected_usd(price, size, region, scope.modes, wait_cost, default_seconds)
                            if price is not None and seconds else None)
                # Placement picks only the instance_types, so only they can be the cheapest.
                if estimate is not None and size in candidates and (cheapest is None or estimate < cheapest.usd):
                    cheapest = Cheapest(estimate, size, region)
                here = [run for run in runs if (run.instance_type, run.region) == (size, region)]
                scoped = [run for run in here if run.mode in scope.modes]
                # Allocations split each bill between both kinds, so only the share of the scope's kinds counts here.
                billed = run_cost.billed_runs(scoped, allocations)
                paid = paid_hourly.get((size, region))
                _refusals, refused, launched = evidence.refusals_per_launch(size, region, scope.modes)
                table.append((region, f'{price:.4f}' if price is not None else 'no price', duration,
                              f'{estimate:.4f}' if estimate is not None else '—',
                              f'{paid:.4f}' if paid is not None else unbilled(len(here), len(here)),
                              f'{run_cost.billed_cost_per_run(billed, allocations):.4f} ({runs_text(len(billed))})' if billed
                              else unbilled(len(scoped), len(here), scope), str(refused), str(launched)))
            tables.append(Table(size, headers, frozenset(range(1, len(headers))), tuple(table)))
        scopes.append(RunCosts(scope, explanation, tuple(tables), cheapest))
    return tuple(scopes)


def unbilled(scoped: int, here: int, scope: run_cost.RunScope | None = None) -> str:
    """The paid cell without a bill: runs wait for their day's bill, only other kinds ran, or nothing ran."""
    if scoped:
        return f'bill due ({runs_text(scoped)})'
    return f'no {scope.noun} runs' if here and scope else 'no runs'


def split_table(table: Table, width: int) -> list[str]:
    """Draw the first column and as many equal columns as fit the width, then the next columns below."""
    columns = table.headers[1:]
    label_width = max(len(table.headers[0]), *(len(row[0]) for row in table.rows))
    column_width = max(8, *(len(header) for header in columns), *(len(cell) for row in table.rows for cell in row[1:]))
    count = max(1, (width - label_width) // (column_width + 2))
    lines = [table.title]
    for start in range(0, len(columns), count):
        indexes = range(1 + start, 1 + min(start + count, len(columns)))
        lines.extend(aligned_table([table.headers[0], *(table.headers[index] for index in indexes)],
                                   [[row[0], *(row[index] for index in indexes)] for row in table.rows],
                                   right=frozenset(position for position, index in enumerate(indexes, 1) if index in table.right),
                                   minimum_widths=[label_width, *(column_width for _ in indexes)]))
        lines.append('')
    return lines


def render(directory: Path, now: int, *, width: int | None = None, settings_path: Path | None = None) -> str:
    """Show prices, quotas, merge gate costs, and the reason for each unavailable value."""
    tables = spot_tables(directory, now, settings_path=settings_path)
    terminal_width = width if width is not None else shutil.get_terminal_size().columns
    lines = [*split_table(tables.prices, terminal_width), *split_table(tables.quotas, terminal_width), *tables.snapshot_lines]
    merge_gates = tables.runs[0]
    lines.extend(('', *textwrap.wrap(merge_gates.explanation, terminal_width)))
    for table in merge_gates.tables:
        lines.extend(('', table.title, *('  ' + line for line in aligned_table(table.headers, table.rows, right=table.right))))
    lines.extend(('', merge_gates.summary))
    return '\n'.join(lines)
