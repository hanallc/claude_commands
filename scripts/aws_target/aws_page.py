"""Publish the spend, prices, quotas and run data for the AWS console page."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import defaultdict
from collections.abc import Sequence
import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from statistics import median
from typing import Literal, TypedDict

from scripts.aws_target import config as aws_config, console_help, ledger, run_cost, spend as aws_spend, spot_table
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.build_steps import read_step_names
from scripts.review_links import console_theme, page_frame, review_link

PAGE_NAME = 'aws_prices.html'
# The user's order for the region filter, 2026-11-15: the groups in this order, then each group A to Z.
REGION_GROUPS = ('us', 'eu', 'ap', 'ca', 'sa')


def region_filter_key(region: str) -> tuple[int, str]:
    """Sort a region into its group, then by name; a region outside the groups comes last."""
    group = region.split('-', 1)[0]
    return (REGION_GROUPS.index(group) if group in REGION_GROUPS else len(REGION_GROUPS), region)


class TableRowsData(TypedDict):
    headers: list[str]
    right: list[int]
    rows: list[list[str]]
    region_axis: spot_table.RegionAxis
    # The number of each cell for the color scale, row by row; null for a cell without a number.
    values: list[list[float | None]]
    # [row, column] cells of a type that the region does not sell.
    not_sold: list[list[int]]


class TableData(TableRowsData):
    title: str


class EstimateData(TypedDict):
    region: str
    summary: str


class SnapshotLine(TypedDict):
    # Empty when the line covers every region.
    region: str
    text: str


class RunCostData(TypedDict):
    scope: str
    label: str
    explanation: str
    tables: list[TableData]
    # Keep the standard Gate table at index 0 for pages that are already open.
    prices: list[TableRowsData]
    # The summary for each candidate estimate, cheapest first; the page shows the first in the chosen regions.
    estimates: list[EstimateData]
    no_estimate: str
    refused: TableData
    launched: TableData


class SpendTile(TypedDict):
    label: str
    usd: str
    note: str


class SpendData(TypedDict):
    # Empty when the spend is available.
    unavailable: str
    # Today, yesterday and the month to date, in that order.
    tiles: list[SpendTile]
    forecast: str


class PageData(TypedDict):
    read_at: int
    render_seconds: float
    spend: SpendData
    # Every region a table shows, for the region filter.
    regions: list[str]
    snapshot: list[SnapshotLine]
    prices: list[TableRowsData]
    placement_scores: TableData
    quotas: list[TableRowsData]
    cached_spot_status: dict[str, Literal['read failed', 'old']]
    runs: list[RunCostData]
    hours: TableData
    hours_note: str


def table_rows_data(table: spot_table.Table) -> TableRowsData:
    return {'headers': list(table.headers), 'right': sorted(table.right),
            'rows': [list(row) for row in table.rows], 'region_axis': table.region_axis,
            'values': [[float(value) if value is not None else None for value in row] for row in table.values],
            'not_sold': [list(cell) for cell in sorted(table.not_sold)]}


def table_data(table: spot_table.Table) -> TableData:
    return {'title': table.title, **table_rows_data(table)}


def placement_score_table(regions: Sequence[str], readings: Sequence[ledger.SpotPlacementReading],
                          now: int) -> spot_table.Table:
    columns = (
        (ledger.SpotPlacementTypeSet.GATE_32_VCPU, 8, '32 vCPU (8 boxes)'),
        (ledger.SpotPlacementTypeSet.ALL_GATE_TYPES, 8, 'All types (8 boxes)'),
        (ledger.SpotPlacementTypeSet.ALL_GATE_TYPES, 20, 'All types (20 boxes)'),
    )
    displayed = {(types, boxes) for types, boxes, _ in columns}
    first = now - ledger.SPOT_PLACEMENT_RETENTION_SECONDS
    locations: set[ledger.SpotPlacementLocation] = {ledger.SpotPlacementRegion(region) for region in regions}
    current_types: dict[ledger.SpotPlacementTypeSet, tuple[str, ...]] = {}
    attempts: dict[tuple[ledger.SpotPlacementScope, ledger.SpotPlacementTypeSet, int, str],
                   ledger.SpotPlacementReading] = {}
    scores: defaultdict[tuple[ledger.SpotPlacementLocation, ledger.SpotPlacementTypeSet, int],
                        list[tuple[int, int]]] = defaultdict(list)
    for reading in sorted(readings, key=lambda row: row.at, reverse=True):
        if not first <= reading.at <= now:
            continue
        request = reading.request
        if (request.type_set, request.boxes) not in displayed:
            continue
        if current_types.setdefault(request.type_set, request.instance_types) != request.instance_types:
            continue
        for region in request.regions:
            locations.add(ledger.SpotPlacementRegion(region))
            _ = attempts.setdefault((request.scope, request.type_set, request.boxes, region), reading)
        if isinstance(reading, ledger.SpotPlacementSample):
            for entry in reading.scores:
                locations.add(entry.location)
                scores[entry.location, request.type_set, request.boxes].append((reading.at, entry.score))

    def cell(location: ledger.SpotPlacementLocation, types: ledger.SpotPlacementTypeSet, boxes: int) -> str:
        zonal = isinstance(location, ledger.SpotPlacementZone)
        if zonal and boxes != 8:
            return 'Not sampled'
        scope = ledger.SpotPlacementScope.ZONE if zonal else ledger.SpotPlacementScope.REGION
        attempt = attempts.get((scope, types, boxes, location.region))
        observed = scores.get((location, types, boxes), [])
        if not observed:
            return f'Missing ({attempt.reason})' if isinstance(attempt, ledger.SpotPlacementFailure) else 'Missing'
        at, latest = observed[0]
        stale = now - at >= 2 * 3600 or (attempt is not None and attempt.at > at)
        prefix = 'Stale: ' if stale else ''
        return f'{prefix}{latest} / {median(score for _, score in observed):g} / {len(observed)}'

    rows = tuple((place.region, place.zone_id if isinstance(place, ledger.SpotPlacementZone) else 'Region',
                  *(cell(place, types, boxes) for types, boxes, _ in columns))
                 for place in sorted(locations, key=lambda place: (region_filter_key(place.region),
                                     place.zone_id if isinstance(place, ledger.SpotPlacementZone) else '')))
    headers = ('Region', 'Location', *(label for _, _, label in columns))
    return spot_table.Table('Spot placement scores', headers, frozenset(range(2, len(headers))), rows, 'rows')


def gate_time_price_table(comparisons: Sequence[run_cost.GateTimeComparison]) -> spot_table.Table:
    """Show the run costs and the time price that makes the 32-vCPU box cheaper."""
    def cells(estimate: run_cost.GateRunEstimate | run_cost.NoPricedGateVariant) -> tuple[str, str, str, str]:
        if isinstance(estimate, run_cost.NoPricedGateVariant):
            return '—', '—', '—', '—'
        minutes = estimate.median_minutes
        return (estimate.instance_type, f'{estimate.spot_usd_per_hour:.4f}',
                f'{minutes:.1f}' if isinstance(minutes, Decimal) else 'no measurement',
                f'{estimate.usd_per_run:.4f}')

    def break_even(row: run_cost.GateTimeComparison) -> str:
        result = run_cost.gate_break_even(row.small, row.large)
        if isinstance(result, run_cost.BreakEvenCondition):
            return result.value
        direction = 'more' if result.faster_32_vcpu else 'less'
        return f'{result.usd_per_minute:.4f} or {direction}'

    headers = ('Region', '16 vCPU type', '16 vCPU Spot (USD/h)', '16 vCPU median (min)', '16 vCPU run (USD)',
               '32 vCPU type', '32 vCPU Spot (USD/h)', '32 vCPU median (min)', '32 vCPU run (USD)',
               '32 vCPU break-even (USD/min)')
    rows = tuple((row.region, *cells(row.small), *cells(row.large), break_even(row))
                 for row in sorted(comparisons, key=lambda row: region_filter_key(row.region)))
    gate = read_step_names().shown('gate')
    return spot_table.Table(f'32-vCPU {gate} break-even', headers, frozenset((2, 3, 4, 6, 7, 8, 9)), rows, 'rows')


def without_prices(table: spot_table.Table) -> spot_table.Table:
    """Omit the Spot price and estimate that the Prices and quotas card shows."""
    keep = [index for index, header in enumerate(table.headers)
            if header not in (spot_table.SPOT_HEADER, spot_table.ESTIMATE_HEADER)]
    return replace(table, headers=tuple(table.headers[index] for index in keep),
                   right=frozenset(position for position, index in enumerate(keep) if index in table.right),
                   rows=tuple(tuple(row[index] for index in keep) for row in table.rows))


def day_tile(label: str, day: str, spend: aws_spend.Spend, limit: bool) -> SpendTile:
    found = next((row for row in spend.days if row.day == day), None)
    if found is None:
        return {'label': label, 'usd': '—', 'note': f'UTC {day}: no spend day yet'}
    source = 'estimate' if found.source != 'AWS bill' else 'provisional bill' if found.provisional_bill else 'bill'
    of_limit = f' of the ${spend.daily_limit:.2f} daily limit' if limit else ''
    return {'label': label, 'usd': f'${found.usd:.2f}', 'note': f'UTC {day}, {source}{of_limit}. {found.runs} gate runs.'}


def spend_data(spend: aws_spend.Spend | aws_config.MeasurementUnavailable, now: int, pace_days: int) -> SpendData:
    """Today, yesterday and the month to date, as the buildlog report shows them."""
    if isinstance(spend, aws_config.MeasurementUnavailable):
        return {'unavailable': f'AWS spend unavailable: {spend.reason}.', 'tiles': [], 'forecast': ''}
    today = datetime.fromtimestamp(now, UTC).date()
    tiles = [day_tile('Today', today.isoformat(), spend, True),
             day_tile('Yesterday', (today - timedelta(days=1)).isoformat(), spend, True)]
    month = aws_spend.month_spend(spend.days, today, pace_days)
    if month is None:
        tiles.append({'label': 'Month to date', 'usd': '—', 'note': f'UTC {today:%Y-%m}: no billed day yet'})
        return {'unavailable': '', 'tiles': tiles, 'forecast': ''}
    tiles.append({'label': 'Month to date', 'usd': f'${month.to_date:.2f}',
                  'note': f'UTC {today:%Y-%m}, of the ${spend.monthly_limit:.2f} monthly limit.'})
    forecast = (f'Month forecast: ${month.forecast:.2f} at ${month.pace:.2f} a day, the average of the last '
                + f'{month.billed_days} billed days. 32-CPU gate compute is {month.gate_share:.0%} of the spend on those days.')
    return {'unavailable': '', 'tiles': tiles, 'forecast': forecast}


def page_data(tables: spot_table.SpotTables, spend: SpendData, now: int,
              placement_readings: Sequence[ledger.SpotPlacementReading] = ()) -> PageData:
    # The billed hours table can add a region that is no longer a candidate.
    placement = placement_score_table(tuple(read.region for read in tables.regions), placement_readings, now)
    regions = dict.fromkeys((*(row[0] for row in placement.rows), *tables.hours.headers[1:]))
    return {
        'read_at': now,
        'render_seconds': 0.0,
        'spend': spend,
        'regions': sorted(regions, key=region_filter_key),
        'snapshot': [{'region': region, 'text': text} for region, text in tables.snapshot_rows],
        'prices': [table_rows_data(table) for table in tables.prices],
        'placement_scores': table_data(placement),
        'quotas': [table_rows_data(table) for table in tables.quotas],
        'cached_spot_status': {read.region: 'read failed' if read.failure else 'old'
                               for read in tables.regions if read.failure or read.stale},
        'runs': [{'scope': costs.scope.name, 'label': costs.scope.label, 'explanation': costs.explanation,
                  'tables': [table_data(without_prices(table)) for table in costs.tables],
                  'prices': [table_rows_data(costs.gate_prices)],
                  'estimates': [{'region': entry.region, 'summary': costs.summary_of(entry)} for entry in costs.estimates],
                  'no_estimate': costs.summary_of(None),
                  'refused': table_data(costs.refused), 'launched': table_data(costs.launched)}
                 for costs in tables.runs],
        'hours': table_data(tables.hours),
        'hours_note': tables.hours_note,
    }


def output_folder() -> Path:
    return Path(os.path.expandvars(conf.read('aws_page').text('output_folder'))).expanduser()


def quota_help() -> str:
    """Render the shared instructions with links to AWS settings and the case list."""
    steps: list[str] = []
    for step in console_help.QUOTA_CASE_STEPS:
        text = escape(step)
        for url in (console_help.SETTINGS_URL, console_help.CASE_LIST_URL):
            link = escape(url)
            text = text.replace(link, f'<a href="{link}">{link}</a>')
        steps.append(f'<li>{text}</li>')
    notes = ''.join(f'\n<p>{escape(note)}</p>' for note in (console_help.WRONG_ACCOUNT, console_help.PASSKEY_NOTE))
    return '<ol>' + ''.join(steps) + '</ol>' + notes


def publish(directory: Path, now: int, folder: Path, *, settings_path: Path | None = None) -> Path:
    """Write the page with the tables of the local snapshot and run ledger."""
    started = time.perf_counter()
    configuration = conf.read(settings_path) if settings_path else conf.read_or_repository('aws_target')
    today = datetime.fromtimestamp(now, UTC).date().isoformat()
    read = aws_spend.read_spend(settings_path or conf.path('aws_target'), directory, now, today)
    spend = spend_data(read, now, configuration.whole('spend_pace_days'))
    try:
        placement = ledger.read_spot_placement_scores(directory)
    except (OSError, ValueError, KeyError) as error:
        print(f'aws_page: Spot placement scores unavailable: {type(error).__name__}', file=sys.stderr)
        placement = ()
    values = conf.read_or_repository('cloud_gate')
    gate_pricing = run_cost.read_gate_pricing_inputs(directory, now, values, configuration)
    tables = spot_table.spot_tables(directory, now, settings_path=settings_path, gate_pricing=gate_pricing)
    data = page_data(tables, spend, now, placement)
    comparisons = run_cost.gate_comparisons(directory, now, values, configuration, gate_pricing=gate_pricing)
    gate_time_price = table_data(gate_time_price_table(comparisons))
    regions = sorted(set(data['regions']).union(row[0] for row in gate_time_price['rows']), key=region_filter_key)
    gate_time_data = {'table': gate_time_price, 'regions': regions}
    source = console_theme.with_theme(Path(__file__).with_name(PAGE_NAME).read_text(encoding='utf-8'))
    # A '</' in a cell must not end the script element.
    data['render_seconds'] = time.perf_counter() - started
    page = (page_frame.template(source, 'AWS', 'aws')
            .replace('__AWS_QUOTA_HELP__', quota_help(), 1)
            .replace('__AWS_PRICES_DATA__', json.dumps(data).replace('</', '<\\/'))
            .replace('__AWS_GATE_TIME_DATA__', json.dumps(gate_time_data).replace('</', '<\\/')))
    page_path = folder / PAGE_NAME
    folder.mkdir(parents=True, exist_ok=True)
    write_text_atomic(page_path, page, new_file_mode=0o644)
    return page_path


def publish_configured() -> int:
    """Publish the page from the AWS target state folder; the hourly job calls this."""
    from scripts.build_targets.target_config import ssh_target_state_directory

    _ = publish(ssh_target_state_directory('aws'), int(time.time()), output_folder(), settings_path=conf.path('aws_target'))
    return 0


def page_url() -> str:
    """Return the page URL for config/console.conf without publishing it."""
    page = output_folder().resolve() / PAGE_NAME
    url = review_link.snapshot_url(review_link.review_root().resolve(), page)
    if not page.is_file():
        raise FileNotFoundError(f'AWS page is not available: {page}')
    return url


class Arguments(argparse.Namespace):
    command: str = 'link'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Publish the AWS page, or print its link.')
    _ = parser.add_argument('command', choices=('publish', 'link'))
    if parser.parse_args(argv, namespace=Arguments()).command == 'publish':
        _ = publish_configured()
    print(page_url())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
