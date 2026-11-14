"""Format the local Spot snapshot for a terminal."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import textwrap

from scripts.aws_target import ledger, run_cost
from scripts.aws_target.aws_target import largest_gate_vcpus, read_snapshot, snapshot_failures
from scripts.aws_target.terminal_table import aligned_table
from scripts.lib import conf


def render(directory: Path, now: int, *, width: int | None = None, settings_path: Path | None = None) -> str:
    """Show prices, quotas, and the reason for each unavailable value."""
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
    notes: dict[str, str] = {}
    details: dict[str, str] = {}
    for region in regions:
        reading = readings.get(region)
        failure = failures.get(region)
        age = now - ledger.instant(reading, 'read_at') if reading else 0
        if failure:
            stamp = datetime.fromtimestamp(failure.at, UTC).isoformat()
            notes[region] = 'read failed'
            details[region] = (f'last read failed at {stamp} (age {now - failure.at}s): '
                               f'{failure.read}: {failure.error}: {failure.reason or "no error detail"}')
            if reading:
                details[region] += f'; last good snapshot age {age}s'
        elif reading is None:
            notes[region] = 'no snapshot'
            details[region] = 'no snapshot was ever taken'
        elif age < 0 or age > configuration.whole('price_max_age_seconds'):
            notes[region] = f'old {age}s'
            details[region] = f'snapshot age {age}s (stale)'
        else:
            notes[region] = ''
            details[region] = f'snapshot age {age}s'
    cells: dict[tuple[str, str], str] = {}
    for size in sizes:
        for region in regions:
            price = minimum.get((region, size))
            note = notes[region]
            cells[size, region] = (f'{price:.4f}' + (f' [{note}]' if note else '') if price is not None
                                   else note or 'no offer')
    label_width = max(len('type'), *(len(size) for size in sizes))
    column_width = max(8, *(len(region) for region in regions), *(len(cell) for cell in cells.values()))
    terminal_width = width if width is not None else shutil.get_terminal_size().columns
    count = max(1, (terminal_width - label_width) // (column_width + 2))
    lines = ['Spot prices (USD/h; lowest zone)']
    for start in range(0, len(regions), count):
        columns = regions[start:start + count]
        rows = [[size, *(cells[size, region] for region in columns)] for size in sizes]
        lines.extend(aligned_table(['type', *columns], rows, right=frozenset(range(1, len(columns) + 1)),
                                  minimum_widths=[label_width, *(column_width for _ in columns)]))
        lines.append('')
    for region in regions:
        reading = readings.get(region)
        lines.append(f'{region}: {details[region]}')
        note = notes[region]
        suffix = f' [{note}]' if note else ''
        for name, field in (('Standard', 'standard_vcpus'), ('G and VT', 'gpu_vcpus')):
            quota = (f'{conf.decimal(ledger.text(reading, field)).normalize():f} vCPUs{suffix}'
                     if reading else note)
            lines.append(f'  {name} quota: {quota}')
        for size in sizes:
            vcpus = largest_gate_vcpus((size,))
            field = 'gpu_vcpus' if size in gpu else 'standard_vcpus'
            fit = (f'{int(conf.decimal(ledger.text(reading, field)) // vcpus)} boxes{suffix}'
                   if reading and vcpus else note or 'unknown vCPU count')
            lines.append(f'  {size} ({vcpus or "?"} vCPUs): {fit}')
    lines.extend(run_cost_lines(directory, now, standard, regions, minimum, configuration.whole('run_cost_days'),
                                terminal_width, configuration.text('region')))
    return '\n'.join(lines)


def run_cost_lines(directory: Path, now: int, sizes: tuple[str, ...], regions: tuple[str, ...],
                   minimum: dict[tuple[str, str], Decimal], days: int, width: int, home: str) -> list[str]:
    """One block per type and one line per region: price, run time, expected and paid cost of a merge gate."""
    end = datetime.fromtimestamp(now, UTC)
    start = end - timedelta(days=days)
    book = ledger.CostLedger(directory)
    rows = book.launches()
    mode = run_cost.KINDS[0][0]
    runs = run_cost.runs_from_rows(rows, start, end, home).runs
    evidence = run_cost.run_evidence(rows, start, end, home)
    bills = run_cost.spot_bills(book, start, end)
    paid_hourly = run_cost.paid_per_hour(bills)
    allocations = run_cost.billed_allocations(bills, runs)
    values = conf.read_or_repository('cloud_gate')
    wait_cost = conf.decimal(values.text('gate_wait_usd_per_minute'))
    default_seconds = Decimal(values.whole('lifetime_minutes') * 60)
    lines = ['', *textwrap.wrap(f'USD per merge gate, last {days} days. Estimate: lowest zone Spot price x median box '
                                + f'time (boot included), plus refusal wait at {wait_cost} USD/min '
                                + f'({evidence.refusal_wait_s:.0f} s each). Paid: the AWS bill for the type and region, '
                                + 'split between runs by box time. Refused: capacity and quota refusals.', width)]
    cheapest: tuple[Decimal, str, str] | None = None
    headers = ['region', 'Spot USD/h', 'merge gate', 'estimate', 'paid USD/h', 'paid USD/run', 'refused/launched']
    for size in sizes:
        table: list[list[str]] = []
        for region in regions:
            price = minimum.get((region, size))
            seconds, own = evidence.box_seconds(size, region, mode)
            duration = (f'{run_cost.median_run_time(seconds)} ({len(seconds)} runs'
                        + ('' if own else ' in other regions') + ')') if seconds else 'no runs'
            estimate = (evidence.expected_usd(price, size, region, mode, wait_cost, default_seconds)
                        if price is not None and seconds else None)
            if estimate is not None and (cheapest is None or estimate < cheapest[0]):
                cheapest = estimate, size, region
            # Allocations split each bill between both kinds, so only the merge gate share counts here.
            billed = run_cost.billed_runs([run for run in runs if (run.instance_type, run.region, run.mode)
                                           == (size, region, mode)], allocations)
            paid = paid_hourly.get((size, region))
            _refusals, refused, launched = evidence.refusals_per_launch(size, region)
            table.append([region, f'{price:.4f}' if price is not None else 'no price', duration,
                          f'{estimate:.4f}' if estimate is not None else '—',
                          f'{paid:.4f}' if paid is not None else 'not billed',
                          f'{run_cost.billed_cost_per_run(billed, allocations):.4f} ({len(billed)} runs)' if billed
                          else 'not billed', f'{refused}/{launched}'])
        lines.extend(('', size, *('  ' + line for line in aligned_table(headers, table))))
    lines.extend(('', f'Cheapest merge gate: {cheapest[1]} in {cheapest[2]}, {cheapest[0]:.4f} USD (estimate)'
                  if cheapest else 'Cheapest merge gate: no estimate; the snapshot has no price or the ledger has no runs'))
    return lines
