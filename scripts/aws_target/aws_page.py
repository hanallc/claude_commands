"""Publish the AWS prices page: the /aws_prices tables as data for the admin console."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import TypedDict

from scripts.aws_target import spot_table
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.review_links import review_link

PAGE_NAME = 'aws_prices.html'


class TableData(TypedDict):
    title: str
    headers: list[str]
    right: list[int]
    rows: list[list[str]]
    region_axis: spot_table.RegionAxis
    # Rankings of [row, column] cells from the lowest price up: one per type, and one per vCPU count.
    type_ranks: list[list[list[list[int]]]]
    group_ranks: list[list[list[list[int]]]]
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
    # The summary for each candidate estimate, cheapest first; the page shows the first in the chosen regions.
    estimates: list[EstimateData]
    no_estimate: str
    refused: TableData
    launched: TableData


class PageData(TypedDict):
    read_at: int
    # Every region a table shows, for the region filter.
    regions: list[str]
    snapshot: list[SnapshotLine]
    prices: list[TableData]
    quotas: list[TableData]
    runs: list[RunCostData]
    hours: TableData
    hours_note: str


def ranking_data(rankings: tuple[spot_table.Ranking, ...]) -> list[list[list[list[int]]]]:
    return [[[list(cell) for cell in sorted(ties)] for ties in ranking] for ranking in rankings]


def table_data(table: spot_table.Table) -> TableData:
    return {'title': table.title, 'headers': list(table.headers), 'right': sorted(table.right),
            'rows': [list(row) for row in table.rows], 'region_axis': table.region_axis,
            'type_ranks': ranking_data(table.type_ranks), 'group_ranks': ranking_data(table.group_ranks),
            'not_sold': [list(cell) for cell in sorted(table.not_sold)]}


def page_data(tables: spot_table.SpotTables, now: int) -> PageData:
    # The billed hours table can add a region that is no longer a candidate.
    regions = dict.fromkeys((*(read.region for read in tables.regions), *tables.hours.headers[1:]))
    return {
        'read_at': now,
        'regions': list(regions),
        'snapshot': [{'region': region, 'text': text} for region, text in tables.snapshot_rows],
        'prices': [table_data(table) for table in tables.prices],
        'quotas': [table_data(table) for table in tables.quotas],
        'runs': [{'scope': costs.scope.name, 'label': costs.scope.label, 'explanation': costs.explanation,
                  'tables': [table_data(table) for table in costs.tables],
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
    data = page_data(spot_table.spot_tables(directory, now, settings_path=settings_path), now)
    source = Path(__file__).with_name(PAGE_NAME).read_text(encoding='utf-8')
    # A '</' in a cell must not end the script element.
    page = source.replace('__AWS_PRICES_DATA__', json.dumps(data).replace('</', '<\\/'))
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
        raise FileNotFoundError(f'AWS prices page is not available: {page}')
    return url


class Arguments(argparse.Namespace):
    command: str = 'link'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Publish the AWS prices page, or print its link.')
    _ = parser.add_argument('command', choices=('publish', 'link'))
    if parser.parse_args(argv, namespace=Arguments()).command == 'publish':
        _ = publish_configured()
    print(page_url())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
