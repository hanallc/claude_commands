"""Read the local Spot snapshot and the run ledger into tables, and format them for a terminal."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
from statistics import median
import textwrap

from scripts.aws_target import ledger, run_cost
from scripts.aws_target.aws_target import SnapshotFailure, gate_sizes, largest_gate_vcpus, read_snapshot, snapshot_failures
from scripts.aws_target.terminal_table import aligned_table
from scripts.lib import conf


@dataclass(frozen=True)
class Table:
    title: str
    headers: tuple[str, ...]
    # The indexes of the columns that hold numbers.
    right: frozenset[int]
    rows: tuple[tuple[str, ...], ...]
    # The (row, column) cells with the lowest price of their row.
    row_lowest: frozenset[tuple[int, int]] = frozenset()
    # The (row, column) cells with the lowest price of all the types with the same vCPU count.
    group_lowest: frozenset[tuple[int, int]] = frozenset()


def minutes_ago(seconds: int) -> str:
    return f'{seconds // 60} min ago'


def run_minutes(seconds: list[Decimal]) -> str:
    """The median run time in minutes with one decimal: 450 s is 7.5."""
    return f'{median(seconds) / 60:.1f}' if seconds else '—'


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
    """The cost per run of one scope: one table per type, then the refused requests and launched boxes."""
    scope: run_cost.RunScope
    explanation: str
    tables: tuple[Table, ...]
    cheapest: Cheapest | None
    refused: Table
    launched: Table

    @property
    def summary(self) -> str:
        if self.cheapest is None:
            return f'Cheapest {self.scope.noun}: no estimate; the snapshot has no price or the ledger has no runs'
        cheapest = self.cheapest
        return f'Cheapest {self.scope.noun}: {cheapest.size} in {cheapest.region}, {cheapest.usd:.4f} USD (estimate)'


@dataclass(frozen=True)
class SpotTables:
    regions: tuple[RegionRead, ...]
    # The standard types, then the GPU types.
    prices: tuple[Table, ...]
    quotas: tuple[Table, ...]
    # One entry for each scope in run_cost.SCOPES, in that order.
    runs: tuple[RunCosts, ...]
    hours: Table
    hours_note: str

    @property
    def snapshot_lines(self) -> tuple[str, ...]:
        """One line when every region has the same snapshot state and minute; else one line per region."""
        details = {read.detail for read in self.regions}
        if len(details) == 1 and not any(read.failure for read in self.regions):
            return (f'Snapshot {details.pop()}',)
        return tuple(f'{read.region}: {read.detail}' for read in self.regions)


def spot_tables(directory: Path, now: int, *, settings_path: Path | None = None) -> SpotTables:
    """Read prices, quotas, run costs, billed hours, and the reason for each unavailable value."""
    values = conf.read_or_repository('cloud_gate')
    configuration = conf.read(settings_path) if settings_path else conf.read_or_repository('aws_target')
    regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
    standard = tuple(dict.fromkeys((*values.words('instance_types'), *values.words('price_compare_instance_types'))))
    gpu = tuple(size for size in dict.fromkeys(values.words('gpu_instance_types')) if size not in standard)
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
    days = configuration.whole('run_cost_days')
    home = configuration.text('region')
    hours, hours_note = billed_hours(directory, now, regions, gate_sizes(values), days, home,
                                     conf.decimal(configuration.text('billed_hours_gap_max_ratio')))
    return SpotTables(
        tuple(reads),
        (price_table('Standard Spot prices (USD/h; lowest zone)', standard, regions, minimum, notes),
         price_table('GPU Spot prices (USD/h; lowest zone)', gpu, regions, minimum, notes)),
        (quota_table('Standard Spot quotas and boxes that fit', 'Standard quota', 'standard_vcpus', standard, regions,
                     readings, notes),
         quota_table('GPU Spot quotas and boxes that fit', 'G and VT quota', 'gpu_vcpus', gpu, regions, readings, notes)),
        run_costs(directory, now, standard, frozenset(values.words('instance_types')), regions, minimum, days, home),
        hours, hours_note)


def price_table(title: str, sizes: tuple[str, ...], regions: tuple[str, ...], minimum: dict[tuple[str, str], Decimal],
                notes: Mapping[str, str]) -> Table:
    """One row per type and one column per region, with the lowest price of each row and of each vCPU count marked."""
    def cell(size: str, region: str) -> str:
        price = minimum.get((region, size))
        note = notes[region]
        return f'{price:.4f}' + (f' [{note}]' if note else '') if price is not None else note or 'no offer'

    prices = {(row, column): minimum[region, size] for row, size in enumerate(sizes)
              for column, region in enumerate(regions, 1) if (region, size) in minimum}
    groups = {row: largest_gate_vcpus((size,)) for row, size in enumerate(sizes)}
    return Table(title, ('type', *regions), frozenset(range(1, len(regions) + 1)),
                 tuple((size, *(cell(size, region) for region in regions)) for size in sizes),
                 lowest(prices, lambda place: place[0]), lowest(prices, lambda place: groups[place[0]]))


def lowest(prices: dict[tuple[int, int], Decimal], group: Callable[[tuple[int, int]], int]) -> frozenset[tuple[int, int]]:
    """The cells with the lowest price of their group; equal prices are all lowest."""
    least: dict[int, Decimal] = {}
    for place, price in prices.items():
        least[group(place)] = min(least.get(group(place), price), price)
    return frozenset(place for place, price in prices.items() if price == least[group(place)])


def quota_table(title: str, label: str, field: str, sizes: tuple[str, ...], regions: tuple[str, ...],
                readings: Mapping[str, dict[str, object]], notes: Mapping[str, str]) -> Table:
    """One row per region: the quota in vCPUs and the box count of each type that fits.

    The quotas come from the same read as the prices, so the snapshot age shows only with the prices."""
    fits = tuple((size, largest_gate_vcpus((size,))) for size in sizes)
    rows: list[tuple[str, ...]] = []
    for region in regions:
        reading = readings.get(region)
        note = notes[region]
        rows.append((region, f'{conf.decimal(ledger.text(reading, field)).normalize():f} vCPUs' if reading else note,
                     *(f'{int(conf.decimal(ledger.text(reading, field)) // vcpus)} boxes'
                       if reading and vcpus else note or 'unknown vCPU count' for _size, vcpus in fits)))
    return Table(title, ('region', label, *(f'{size} ({vcpus or "?"} vCPUs)' for size, vcpus in fits)),
                 frozenset(range(1, 2 + len(fits))), tuple(rows))


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
    counts = ('type', *regions)
    scopes: list[RunCosts] = []
    for scope in run_cost.SCOPES:
        explanation = (f'USD per {scope.noun}, last {days} days. Estimate: lowest zone Spot price x median box '
                       + f'time (boot included), plus refusal wait at {wait_cost} USD/min '
                       + f'({evidence.refusal_wait_s:.0f} s each). Paid: the AWS bill for the type and region, '
                       + 'split between runs by box time. Runs elsewhere: the type has no runs in the region, '
                       + 'so the time comes from the other regions.')
        cheapest: Cheapest | None = None
        headers = ('region', 'Spot USD/h', f'{scope.noun} (minutes)', 'runs', 'estimate', 'paid USD/h', 'paid USD/run',
                   'paid runs')
        tables: list[Table] = []
        refused_rows: list[tuple[str, ...]] = []
        launched_rows: list[tuple[str, ...]] = []
        for size in sizes:
            table: list[tuple[str, ...]] = []
            refused_cells: list[str] = []
            launched_cells: list[str] = []
            for region in regions:
                price = minimum.get((region, size))
                seconds, own = evidence.box_seconds(size, region, scope.modes)
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
                refused_cells.append(str(refused))
                launched_cells.append(str(launched))
                table.append((region, f'{price:.4f}' if price is not None else 'no price', run_minutes(seconds),
                              f'{len(seconds)}' + ('' if own or not seconds else ' elsewhere'),
                              f'{estimate:.4f}' if estimate is not None else '—',
                              f'{paid:.4f}' if paid is not None else unbilled(len(here)),
                              f'{run_cost.billed_cost_per_run(billed, allocations):.4f}' if billed
                              else unbilled(len(scoped)), str(len(billed))))
            tables.append(Table(size, headers, frozenset(range(1, len(headers))), tuple(table)))
            refused_rows.append((size, *refused_cells))
            launched_rows.append((size, *launched_cells))
        right = frozenset(range(1, len(counts)))
        scopes.append(RunCosts(scope, explanation, tuple(tables), cheapest,
                               Table(f'Refused {scope.noun} requests (capacity and quota)', counts, right, tuple(refused_rows)),
                               Table(f'Launched {scope.noun} boxes', counts, right, tuple(launched_rows))))
    return tuple(scopes)


def unbilled(runs: int) -> str:
    """The paid cell without a bill: runs wait for their day's bill, or no run counts here."""
    return 'bill due' if runs else 'no runs'


def billed_hours(directory: Path, now: int, regions: tuple[str, ...], sizes: tuple[str, ...], days: int, home: str,
                 max_ratio: Decimal) -> tuple[Table, str]:
    """The billed Spot hours against the recorded box hours of each type and region, and the note under them."""
    title = 'Billed hours / box hours'
    end = datetime.fromtimestamp(now, UTC)
    try:
        checks = run_cost.hours_checks(ledger.CostLedger(directory), end - timedelta(days=days), end, home, sizes, max_ratio)
    except ledger.LedgerUnavailable:
        return Table(title, ('type', *regions), frozenset(), ()), 'No billed hours: the cost ledger is not readable.'
    # A region that is no longer a candidate keeps its column while it has hours in the window.
    columns = (*regions, *sorted({region for _size, region in checks} - set(regions)))
    rows = tuple((size, *(hours_cell(checks.get((size, region))) for region in columns))
                 for size in sizes if any(key[0] == size for key in checks))
    note = (f'AWS billed Spot hours / recorded box hours, last {days} days, on the days with a bill. '
            + f'The gap in parentheses is the difference over the box hours. Over: the gap is above {max_ratio:.0%}. '
            + 'Not billed: no day has a bill yet.')
    return Table(title, ('type', *columns), frozenset(range(1, len(columns) + 1)), rows), note


def hours_cell(check: run_cost.HoursCheck | None) -> str:
    """For example 9.2/8.8 (4%); 'over' marks a gap above the limit."""
    if check is None:
        return '—'
    if check.billed_hours is None:
        return 'not billed'
    gap = f' ({check.hours_gap_ratio:.0%})' if check.hours_gap_ratio is not None else ''
    return f'{check.billed_hours:.1f}/{check.box_hours:.1f}{gap}' + (' over' if check.hours_gap_over else '')


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
    """Show prices, quotas, merge gate costs, billed hours, and the reason for each unavailable value."""
    tables = spot_tables(directory, now, settings_path=settings_path)
    terminal_width = width if width is not None else shutil.get_terminal_size().columns
    lines = [line for table in tables.prices for line in split_table(table, terminal_width)]
    lines.extend((*tables.snapshot_lines, ''))
    lines.extend(line for table in tables.quotas for line in split_table(table, terminal_width))
    merge_gates = tables.runs[0]
    lines.extend(textwrap.wrap(merge_gates.explanation, terminal_width))
    for table in merge_gates.tables:
        lines.extend(('', table.title, *('  ' + line for line in aligned_table(table.headers, table.rows, right=table.right))))
    lines.extend(('', merge_gates.summary, ''))
    lines.extend((*split_table(merge_gates.refused, terminal_width), *split_table(merge_gates.launched, terminal_width)))
    if tables.hours.rows:
        lines.extend(split_table(tables.hours, terminal_width))
    else:
        lines.append(tables.hours.title)
    lines.extend(textwrap.wrap(tables.hours_note, terminal_width))
    return '\n'.join(lines)
