"""Read the local Spot snapshot and the run ledger into tables for the AWS page."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Literal

from scripts.aws_target import ledger, run_cost
from scripts.aws_target.aws_target import SnapshotFailure, gpu_price_sizes, hours_check_scope, largest_gate_vcpus, read_snapshot, snapshot_failures
from scripts.aws_target.quota_requests import RegionalQuotaRequests, snapshot_quota_requests
from scripts.lib import conf


# Where a table puts its regions: one region in each row (the first column), or in each column after the first.
RegionAxis = Literal['rows', 'columns']
# The run table columns that the page shows in its price tables, so the page leaves them out of the run tables.
SPOT_HEADER = 'Spot USD/h'
ESTIMATE_HEADER = 'estimate'


@dataclass(frozen=True)
class Table:
    title: str
    headers: tuple[str, ...]
    # The indexes of the columns that hold numbers.
    right: frozenset[int]
    rows: tuple[tuple[str, ...], ...]
    region_axis: RegionAxis
    # The number of each cell, row by row. Use None for a cell without a number.
    values: tuple[tuple[Decimal | None, ...], ...] = ()
    # The (row, column) cells of a type that the region does not sell; they read NOT_SOLD.
    not_sold: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True, kw_only=True)
class BilledHoursTable(Table):
    active_regions_by_size: Mapping[str, list[str]]
    note: str
    reserved_by_region: Mapping[str, str]


NOT_SOLD = '—'


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
    # The gate estimate of each standard type, with one row per region.
    gate_prices: Table
    # Estimates for deployed types, cheapest first. Equal estimates keep the table order.
    estimates: tuple[Cheapest, ...]
    refused: Table
    launched: Table

    @property
    def cheapest(self) -> Cheapest | None:
        return self.estimates[0] if self.estimates else None

    def summary_of(self, cheapest: Cheapest | None) -> str:
        if cheapest is None:
            return f'Cheapest {self.scope.noun}: no eligible type with a current price and run evidence.'
        return f'Cheapest {self.scope.noun}: {cheapest.size} in {cheapest.region}, {cheapest.usd:.4f} USD (estimate)'


@dataclass(frozen=True)
class SpotTables:
    regions: tuple[RegionRead, ...]
    # The standard types, then the GPU types.
    prices: tuple[Table, ...]
    quotas: tuple[Table, ...]
    # One entry for each scope in run_cost.SCOPES, in that order.
    runs: tuple[RunCosts, ...]
    hours: BilledHoursTable
    gpu_quota_needed: int
    quota_requests: tuple[RegionalQuotaRequests, ...]

    @property
    def snapshot_rows(self) -> tuple[tuple[str, str], ...]:
        """(region, line) pairs: one line with no region when every region has the same snapshot state and minute;
        else one line per region."""
        details = {read.detail for read in self.regions}
        if len(details) == 1 and not any(read.failure for read in self.regions):
            return (('', f'Snapshot {details.pop()}'),)
        return tuple((read.region, f'{read.region}: {read.detail}') for read in self.regions)


def spot_tables(directory: Path, now: int, *, settings_path: Path | None = None,
                gate_pricing: run_cost.GatePricingInputs | None = None) -> SpotTables:
    """Read prices, quotas, run costs, billed hours, and the reason for each unavailable value."""
    values = conf.read_or_repository('cloud_gate')
    configuration = conf.read(settings_path) if settings_path else conf.read_or_repository('aws_target')
    return spot_tables_from(directory, now, values, configuration, gate_pricing=gate_pricing)


def spot_tables_from(directory: Path, now: int, values: conf.ConfigRead, configuration: conf.ConfigRead, *,
                     gate_pricing: run_cost.GatePricingInputs | None = None,
                     evidence: ledger.SpendEvidenceAt | None = None) -> SpotTables:
    """Build the tables from the settings for this publication."""
    regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
    gate_types = values.words('instance_types')
    standard = tuple(dict.fromkeys((*gate_types, *values.words('price_compare_instance_types'))))
    gpu = tuple(size for size in gpu_price_sizes(values) if size not in standard)
    document = read_snapshot(directory) if evidence is None else evidence.spot_snapshot
    readings = {ledger.text(row, 'region'): row for row in ledger.rows(document, 'regions')} if 'regions' in document else {}
    failures = {row.region: row for row in snapshot_failures(document)}
    minimum: dict[tuple[str, str], Decimal] = {}
    for row in ledger.rows(document, 'prices'):
        key = ledger.text(row, 'region'), ledger.text(row, 'instance_type')
        price = conf.decimal(ledger.text(row, 'usd_per_hour'))
        minimum[key] = min(minimum.get(key, price), price)
    reads: list[RegionRead] = []
    max_age = configuration.whole('price_max_age_seconds')
    for region in regions:
        reading = readings.get(region)
        failure = failures.get(region)
        age = now - ledger.instant(reading, 'read_at') if reading else None
        stale = age is not None and (age < 0 or age > max_age)
        reads.append(RegionRead(region, age, stale, failure, now - failure.at if failure else 0))
    notes = {read.region: read.note for read in reads}
    not_sold = frozenset((region, size) for region in regions for size in (*standard, *gpu)
                         if (region, size) not in minimum and not notes[region])
    days = configuration.whole('run_cost_days')
    home = configuration.text('region')
    inputs = gate_pricing if gate_pricing is not None else run_cost.read_gate_pricing_inputs(
        directory, now, values, configuration)
    page = evidence.page_inputs if evidence is not None else None
    if isinstance(page, ledger.SpendPageInputsNotCaptured):
        raise ledger.LedgerUnavailable('page capture requires the Hana hours settings')
    hana = page.hana_hours if isinstance(page, ledger.SpendPageInputs) else None
    hours = billed_hours(directory, now, regions, hours_check_scope(values, hana=hana), days, home,
                         configuration.text('instance_type'), conf.decimal(configuration.text('billed_hours_gap_max_ratio')),
                         evidence=evidence)
    return SpotTables(
        tuple(reads),
        (price_table(standard, regions, minimum, notes, not_sold), price_table(gpu, regions, minimum, notes, not_sold)),
        (quota_table('Standard quota', 'standard_vcpus', standard, regions, readings, notes),
         quota_table('G and VT quota', 'gpu_vcpus', gpu, regions, readings, notes)),
        run_costs(directory, now, standard, regions, minimum, values, configuration, inputs, evidence=evidence),
        hours, values.whole('gpu_spot_quota_request_vcpus'), snapshot_quota_requests(document, regions))


def price_table(sizes: tuple[str, ...], regions: tuple[str, ...], minimum: Mapping[tuple[str, str], Decimal],
                notes: Mapping[str, str], not_sold: frozenset[tuple[str, str]]) -> Table:
    """One row per region and one column per type: the lowest zone Spot price."""
    def cell(region: str, size: str) -> str:
        price = minimum.get((region, size))
        note = notes[region]
        return f'{price:.4f}' + (f' [{note}]' if note else '') if price is not None else note or NOT_SOLD

    return region_type_table(sizes, regions, minimum, cell, not_sold)


def region_type_table(sizes: tuple[str, ...], regions: tuple[str, ...], numbers: Mapping[tuple[str, str], Decimal],
                      cell: Callable[[str, str], str], not_sold: frozenset[tuple[str, str]]) -> Table:
    """One row per region and one column per type, with the number of each (region, type) cell for the color scale."""
    return Table('', ('region', *sizes), frozenset(range(1, len(sizes) + 1)),
                 tuple((region, *(cell(region, size) for size in sizes)) for region in regions), 'rows',
                 tuple((None, *(numbers.get((region, size)) for size in sizes)) for region in regions),
                 frozenset((row, column) for row, region in enumerate(regions) for column, size in enumerate(sizes, 1)
                           if (region, size) in not_sold))


def quota_table(label: str, field: str, sizes: tuple[str, ...], regions: tuple[str, ...],
                readings: Mapping[str, dict[str, object]], notes: Mapping[str, str]) -> Table:
    """One row per region: the quota in vCPUs and the box count of each type that fits.

    The quotas come from the same read as the prices, so the snapshot age shows only with the prices."""
    fits = tuple((size, largest_gate_vcpus((size,))) for size in sizes)
    applied = {region: conf.decimal(ledger.text(readings[region], field)) for region in regions if region in readings}
    rows = tuple((region, f'{value.normalize():f} vCPUs' if (value := applied.get(region)) is not None else notes[region],
                  *(f'{int(value // vcpus)} boxes' if value is not None and vcpus
                    else notes[region] or 'unknown vCPU count' for _size, vcpus in fits))
                 for region in regions)
    values = tuple((None, applied.get(region), *(None for _size in sizes)) for region in regions)
    return Table('', ('region', label, *(f'{size} ({vcpus or "?"} vCPUs)' for size, vcpus in fits)),
                 frozenset(range(1, 2 + len(fits))), rows, 'rows', values)


def run_costs(directory: Path, now: int, sizes: tuple[str, ...], regions: tuple[str, ...],
              minimum: Mapping[tuple[str, str], Decimal], values: conf.ConfigRead,
              policy: conf.ConfigRead, gate_pricing: run_cost.GatePricingInputs, *,
              evidence: ledger.SpendEvidenceAt | None = None) -> tuple[RunCosts, ...]:
    """Build the run tables and gate estimates for standard types in each scope."""
    days = policy.whole('run_cost_days')
    home = policy.text('region')
    end = datetime.fromtimestamp(now, UTC)
    start = end - timedelta(days=days)
    if evidence is None:
        book = ledger.CostLedger(directory)
        history = run_cost.read_gate_run_history(book, start, end, home)
        bills = run_cost.spot_bills(book, start, end, bill_policy=run_cost.ValidateBillsAt(now))
    else:
        history = run_cost.gate_run_history_from(evidence.launches, start, end, home)
        bills = run_cost.bills_from_readings(run_cost.readings_in_window(evidence.validated_bills, start, end))
    paid_hourly = run_cost.paid_per_hour(bills)
    allocations = run_cost.billed_allocations(bills, history.runs)
    gate_minimum: dict[tuple[str, str], Decimal] = {}
    for variant, price in gate_pricing.prices.items():
        key = variant.region, variant.instance_type
        gate_minimum[key] = min(gate_minimum.get(key, price), price)
    max_gate_minutes = values.whole('max_gate_minutes')
    counts = ('type', *regions)
    scopes: list[RunCosts] = []
    for scope in run_cost.SCOPES:
        pricing = run_cost.GateRunPricing.from_config(values, gate_pricing.evidence, home, scope.modes)
        explanation = (f'USD per {scope.noun}, last {days} days. The estimate uses the lowest deployed zone Spot price '
                       + 'and the median box time, including boot. It adds transfer charges outside the home region. '
                       + f'Refusal wait ({gate_pricing.evidence.refusal_wait_s:.0f} s each) and extra regional wait cost '
                       + f'{pricing.wait_usd_per_minute} USD/min. Paid: the AWS bill for the type and region, '
                       + 'split between runs by box time. Runs elsewhere: the type has no runs in the region, '
                       + 'so the time comes from the other regions. Runs same vCPUs: the type has no runs, '
                       + 'so the time comes from the types with its vCPU count.')
        estimates: list[Cheapest] = []
        grid: dict[tuple[str, str], Decimal] = {}
        slow = run_cost.slow_gate_types(gate_pricing.deployed, gate_pricing.evidence, scope.modes, max_gate_minutes)
        for (region, size), price in gate_minimum.items():
            if (size, region) not in slow:
                estimate = pricing.estimate(price, size, region)
                if isinstance(estimate.median_minutes, Decimal):
                    grid[region, size] = estimate.usd_per_run
        headers = ('region', SPOT_HEADER, f'{scope.noun} (minutes)', 'runs', ESTIMATE_HEADER, 'paid USD/h', 'paid USD/run',
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
                seconds, source = history.evidence.box_seconds(size, region, scope.modes)
                estimate = grid.get((region, size))
                if estimate is not None:
                    estimates.append(Cheapest(estimate, size, region))
                here = [run for run in history.runs if (run.instance_type, run.region) == (size, region)]
                scoped = [run for run in here if run.mode in scope.modes]
                # Allocations split each bill between both kinds, so only the share of the scope's kinds counts here.
                billed = run_cost.billed_runs(scoped, allocations)
                paid = paid_hourly.get((size, region))
                _refusals, refused, launched = history.evidence.refusals_per_launch(size, region, scope.modes)
                refused_cells.append(str(refused))
                launched_cells.append(str(launched))
                table.append((region, f'{price:.4f}' if price is not None else 'no price', run_minutes(seconds),
                              f'{len(seconds)}' + (source.value if seconds else ''),
                              f'{estimate:.4f}' if estimate is not None else '—',
                              f'{paid:.4f}' if paid is not None else unbilled(len(here)),
                              paid_run_cell(billed, len(scoped) - len(billed), allocations), str(len(billed))))
            tables.append(Table(size, headers, frozenset(range(1, len(headers))), tuple(table), 'rows'))
            refused_rows.append((size, *refused_cells))
            launched_rows.append((size, *launched_cells))
        right = frozenset(range(1, len(counts)))

        def estimate_cell(region: str, size: str) -> str:
            if (size, region) in slow:
                return 'too slow'
            if (region, size) not in gate_minimum:
                return 'no current gate price'
            estimate = grid.get((region, size))
            return f'{estimate:.4f}' if estimate is not None else 'no runs'

        gate_prices = region_type_table(sizes, regions, grid, estimate_cell, frozenset())
        scopes.append(RunCosts(scope, explanation, tuple(tables), gate_prices, tuple(sorted(estimates, key=lambda entry: entry.usd)),
                               Table(f'Refused {scope.noun} requests (capacity and quota)', counts, right,
                                     tuple(refused_rows), 'columns'),
                               Table(f'Launched {scope.noun} boxes', counts, right, tuple(launched_rows), 'columns')))
    return tuple(scopes)


def unbilled(runs: int) -> str:
    """The paid cell without a bill: runs wait for their day's bill, or no run counts here."""
    return 'bill due' if runs else 'no runs'


def paid_run_cell(billed: list[run_cost.FinishedGateRun], due: int, allocations: dict[run_cost.BilledKey, Decimal]) -> str:
    """The paid USD per run and the count of runs whose bill is still due; a bill comes up to about a day late."""
    if not billed:
        return f'{due} bill due' if due else 'no runs'
    return f'{run_cost.billed_cost_per_run(billed, allocations):.4f}' + (f' ({due} bill due)' if due else '')


def billed_hours(directory: Path, now: int, regions: tuple[str, ...], scope: run_cost.SpotHoursScope, days: int, home: str,
                 builder: str, max_ratio: Decimal, *, evidence: ledger.SpendEvidenceAt | None = None) -> BilledHoursTable:
    """Show the billed Spot hours, box hours and regional Hana allowance."""
    title = 'Billed hours / box hours'
    end = datetime.fromtimestamp(now, UTC)
    start = end - timedelta(days=days)
    try:
        if evidence is None:
            checks = run_cost.hours_checks(ledger.CostLedger(directory), start, end, home, builder, scope,
                                           max_ratio, bill_policy=run_cost.ValidateBillsAt(now))
        elif evidence.has_saved_ledger:
            readings = run_cost.readings_in_window(evidence.validated_bills, start, end)
            daily = run_cost.day_hours_from(evidence.state, readings, start, end, home, builder, scope, max_ratio)
            checks = run_cost.sum_hours_checks(daily, max_ratio)
        else:
            raise ledger.LedgerUnavailable('the cost ledger is absent')
    except ledger.LedgerUnavailable:
        return BilledHoursTable(title, ('type', *regions), frozenset(), (), 'columns',
                                active_regions_by_size={}, note='No billed hours: the cost ledger is not readable.', reserved_by_region={})
    # A region that is no longer a candidate keeps its column while it has hours in the window.
    columns = (*regions, *sorted({region for _size, region in checks} - set(regions)))
    active_regions_by_size: defaultdict[str, list[str]] = defaultdict(list)
    for (size, region), check in checks.items():
        if check.billed_hours or check.box_hours:
            active_regions_by_size[size].append(region)
    rows = tuple((size, *(hours_cell(checks.get((size, region))) for region in columns))
                 for size in scope.sizes if size in active_regions_by_size)
    # Each eligible size in a region uses the same Hana allowance on billed days.
    reserved_by_region = {region: f'{check.hana_reserved_hours:.1f}' for (_size, region), check in checks.items()
                          if check.billed_hours is not None and check.hana_reserved_hours}
    percentage = format(max_ratio, '%').removesuffix('%')
    if '.' in percentage:
        percentage = percentage.rstrip('0').rstrip('.')
    note = (f'Each cell shows billed Spot hours / recorded box hours on billed days in the last {days} days. '
            + f'Over marks gaps above {percentage}% after the Hana allowance, which applies only to sizes Hana can use.')
    return BilledHoursTable(title, ('type', *columns), frozenset(range(1, len(columns) + 1)), rows, 'columns',
                            active_regions_by_size=active_regions_by_size, note=note, reserved_by_region=reserved_by_region)


def hours_cell(check: run_cost.HoursCheck | None) -> str:
    """For example 9.2/8.8 (4%); 'over' marks a gap above the limit."""
    if check is None:
        return '—'
    if check.billed_hours is None:
        return 'not billed'
    gap = f' ({check.hours_gap_ratio:.0%})' if check.hours_gap_ratio is not None else ''
    return f'{check.billed_hours:.1f}/{check.box_hours:.1f}{gap}' + (' over' if check.hours_gap_over else '')

