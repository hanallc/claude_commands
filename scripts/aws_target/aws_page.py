"""Publish the AWS page: the /aws_spend totals and the spot_table tables as data for the admin console."""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from scripts.aws_target import config as aws_config, spend as aws_spend, spot_table
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.review_links import console_theme, page_frame, review_link

PAGE_NAME = 'aws_prices.html'
# The user's order for the region filter, 2026-11-15: the groups in this order, then each group A to Z.
REGION_GROUPS = ('us', 'eu', 'ap', 'ca', 'sa')


def region_filter_key(region: str) -> tuple[int, str]:
    """Sort a region into its group, then by name; a region outside the groups comes last."""
    group = region.split('-', 1)[0]
    return (REGION_GROUPS.index(group) if group in REGION_GROUPS else len(REGION_GROUPS), region)


class TableData(TypedDict):
    title: str
    headers: list[str]
    right: list[int]
    rows: list[list[str]]
    region_axis: spot_table.RegionAxis
    # The number of each cell for the color scale, row by row; null for a cell without a number.
    values: list[list[float | None]]
    # [row, column] cells of a type that the region does not sell.
    not_sold: list[list[int]]


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
    # The estimate of each type and region: the price tables of the Cost per gate view.
    prices: list[TableData]
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
    spend: SpendData
    # Every region a table shows, for the region filter.
    regions: list[str]
    snapshot: list[SnapshotLine]
    prices: list[TableData]
    quotas: list[TableData]
    runs: list[RunCostData]
    hours: TableData
    hours_note: str


def table_data(table: spot_table.Table) -> TableData:
    return {'title': table.title, 'headers': list(table.headers), 'right': sorted(table.right),
            'rows': [list(row) for row in table.rows], 'region_axis': table.region_axis,
            'values': [[float(value) if value is not None else None for value in row] for row in table.values],
            'not_sold': [list(cell) for cell in sorted(table.not_sold)]}


def without_prices(table: spot_table.Table) -> spot_table.Table:
    """A run table without the Spot price and estimate columns, which the page's price tables show."""
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
    """Today, yesterday and the month to date, as /aws_spend shows them."""
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


def page_data(tables: spot_table.SpotTables, spend: SpendData, now: int) -> PageData:
    # The billed hours table can add a region that is no longer a candidate.
    regions = dict.fromkeys((*(read.region for read in tables.regions), *tables.hours.headers[1:]))
    return {
        'read_at': now,
        'spend': spend,
        'regions': sorted(regions, key=region_filter_key),
        'snapshot': [{'region': region, 'text': text} for region, text in tables.snapshot_rows],
        'prices': [table_data(table) for table in tables.prices],
        'quotas': [table_data(table) for table in tables.quotas],
        'runs': [{'scope': costs.scope.name, 'label': costs.scope.label, 'explanation': costs.explanation,
                  'tables': [table_data(without_prices(table)) for table in costs.tables],
                  'prices': [table_data(table) for table in costs.prices],
                  'estimates': [{'region': entry.region, 'summary': costs.summary_of(entry)} for entry in costs.estimates],
                  'no_estimate': costs.summary_of(None),
                  'refused': table_data(costs.refused), 'launched': table_data(costs.launched)}
                 for costs in tables.runs],
        'hours': table_data(tables.hours),
        'hours_note': tables.hours_note,
    }


def output_folder() -> Path:
    return Path(os.path.expandvars(conf.read('aws_page').text('output_folder'))).expanduser()


def publish(directory: Path, now: int, folder: Path, *, settings_path: Path | None = None) -> Path:
    """Write the page with the tables of the local snapshot and run ledger."""
    configuration = conf.read(settings_path) if settings_path else conf.read_or_repository('aws_target')
    today = datetime.fromtimestamp(now, UTC).date().isoformat()
    read = aws_spend.read_spend(settings_path or conf.path('aws_target'), directory, now, today)
    spend = spend_data(read, now, configuration.whole('spend_pace_days'))
    data = page_data(spot_table.spot_tables(directory, now, settings_path=settings_path), spend, now)
    source = console_theme.with_theme(Path(__file__).with_name(PAGE_NAME).read_text(encoding='utf-8'))
    # A '</' in a cell must not end the script element.
    page = (source.replace('__PAGE_FRAME_STYLE__', page_frame.STYLE)
            .replace('__AWS_PRICES_DATA__', json.dumps(data).replace('</', '<\\/')))
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
