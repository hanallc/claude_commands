"""Publish the spend, prices, quotas and run data for the AWS console page."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import defaultdict
from collections.abc import Generator, Sequence, Set
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from html import escape
from pathlib import Path
from statistics import median
from typing import Literal, NotRequired, TypedDict

from scripts.aws_target import cloud_gate, config as aws_config, console_help, ledger, quota_requests, run_cost, spend as aws_spend, spot_table
from scripts.build_targets import target_config
from scripts.disks.disks_page import shared_step_tip
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.file_lock import LockResult
from scripts.review_links import review_link

PAGE_NAME = ledger.SPEND_PAGE_SOURCE.name
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


class RefusedBillData(TypedDict):
    days: list[str]
    reason: str


class SpendData(TypedDict):
    # Empty when the spend is available.
    unavailable: str
    # Today, yesterday and the month to date, in that order.
    tiles: list[SpendTile]
    forecast: str
    refused_bills: list[RefusedBillData]


class NormalLaunchRefusal(StrEnum):
    PRICE = 'SpotMaxPriceTooLow'
    SPOT_QUOTA = 'MaxSpotInstanceCountExceeded'
    VCPU_QUOTA = 'VcpuLimitExceeded'
    INSTANCE_CAPACITY = 'InsufficientInstanceCapacity'
    SPOT_CAPACITY = 'InsufficientSpotCapacity'


class OtherLaunchRefusal(StrEnum):
    UNKNOWN = 'unknown code'
    BLOCKED = 'Blocked'
    AUTH_FAILURE = 'AuthFailure'
    INVALID_CLIENT_TOKEN = 'InvalidClientTokenId'
    UNRECOGNIZED_CLIENT = 'UnrecognizedClientException'
    BAD_SIGNATURE = 'SignatureDoesNotMatch'
    EXPIRED_TOKEN = 'ExpiredToken'
    EXPIRED_TOKEN_EXCEPTION = 'ExpiredTokenException'
    UNAUTHORIZED_OPERATION = 'UnauthorizedOperation'
    ACCESS_DENIED = 'AccessDenied'
    ACCESS_DENIED_EXCEPTION = 'AccessDeniedException'
    UNSUPPORTED = 'Unsupported'
    INVALID_INSTANCE_TYPE = 'InvalidInstanceType'
    INSTANCE_TYPE_NOT_FOUND = 'InvalidInstanceType.NotFound'
    REQUEST_LIMIT = 'RequestLimitExceeded'
    REQUEST_TIMEOUT = 'RequestTimeout'
    SERVICE_UNAVAILABLE = 'ServiceUnavailable'
    INTERNAL_ERROR = 'InternalError'
    INTERNAL_FAILURE = 'InternalFailure'
    SERVER_INTERNAL = 'ServerInternal'
    UNAVAILABLE = 'Unavailable'


LAUNCH_REFUSAL_REASONS = {
    OtherLaunchRefusal.BLOCKED: 'The account is blocked.',
    OtherLaunchRefusal.AUTH_FAILURE: 'AWS could not authenticate the launch credentials.',
    OtherLaunchRefusal.INVALID_CLIENT_TOKEN: 'AWS rejected the launch credentials.',
    OtherLaunchRefusal.UNRECOGNIZED_CLIENT: 'AWS rejected the launch credentials.',
    OtherLaunchRefusal.BAD_SIGNATURE: 'AWS rejected the request signature.',
    OtherLaunchRefusal.EXPIRED_TOKEN: 'The launch credentials expired.',
    OtherLaunchRefusal.EXPIRED_TOKEN_EXCEPTION: 'The launch credentials expired.',
}


class LaunchRowEvidence(Enum):
    LAUNCHED = 'launched'
    NO_ATTEMPT = 'no attempt'


def classify_launch_row(
    row: dict[str, object], requested_attempts: Set[str],
) -> LaunchRowEvidence | NormalLaunchRefusal | OtherLaunchRefusal:
    """A fallback counts only when its ID identifies an attempt without a launch request."""
    result = row.get('result')
    match result:
        case 'launched':
            return LaunchRowEvidence.LAUNCHED
        case 'refused' | 'fallback':
            if result == 'fallback':
                attempt = row.get('attempt')
                if not isinstance(attempt, str) or not attempt or attempt in requested_attempts:
                    return LaunchRowEvidence.NO_ATTEMPT
            code = row.get('error_code')
            if code in NormalLaunchRefusal:
                return NormalLaunchRefusal(code)
            if code in OtherLaunchRefusal:
                return OtherLaunchRefusal(code)
            # A fallback can contain a local reason, such as the end of a capacity search.
            return OtherLaunchRefusal.UNKNOWN if result == 'refused' else LaunchRowEvidence.NO_ATTEMPT
        case 'finished':
            return LaunchRowEvidence.NO_ATTEMPT
        case _:
            return LaunchRowEvidence.NO_ATTEMPT


class CloudGatesOff(TypedDict):
    kind: Literal['off']


class InvalidCloudGateSetting(TypedDict):
    kind: Literal['invalid']


class NoLaunchEvidence(TypedDict):
    kind: Literal['no-evidence']


class LaunchesWorking(TypedDict):
    kind: Literal['working']
    last_good_at: float
    latest_attempt_at: float


class LaunchesRefused(TypedDict):
    kind: Literal['refused']
    code: OtherLaunchRefusal
    reason: str
    first_at: float
    latest_attempt_at: float
    count: int
    last_good_at: NotRequired[float]


type LaunchState = CloudGatesOff | InvalidCloudGateSetting | NoLaunchEvidence | LaunchesWorking | LaunchesRefused


class QuotaRequestColumns(TypedDict):
    wanted: str
    request: QuotaRequestOutcome | Literal['not measured', '—']
    age: str
    detail: NotRequired[str]
    note: NotRequired[str]
    history_missing: NotRequired[Literal[True]]


class GpuQuotaSummary(TypedDict):
    target_vcpus: int
    regions_by_outcome: dict[QuotaRequestOutcome, list[str]]


class QuotaHistoryData(TypedDict):
    standard: dict[str, QuotaRequestColumns]
    gpu: dict[str, QuotaRequestColumns]
    gpu_summary: GpuQuotaSummary


HoursDifferenceKind = Literal['difference', 'no-difference']


class HoursDifferenceKinds(TypedDict):
    by_tenths: dict[str, HoursDifferenceKind]
    default: HoursDifferenceKind


HOURS_DIFFERENCE_KINDS: HoursDifferenceKinds = {'by_tenths': {'0': 'no-difference'}, 'default': 'difference'}


class ComparedHoursRowData(TypedDict):
    size: str
    region: str
    billed_tenths: int
    recorded_tenths: int
    difference_kind: HoursDifferenceKind
    first_utc_day: str
    last_utc_day: str
    hana_reserved_tenths: NotRequired[int]
    limit_result: Literal['within_limit', 'higher_bill', 'lower_bill']


class HoursComparisonData(TypedDict):
    kind: Literal['comparison']
    rows: list[ComparedHoursRowData]
    limit_percent: str
    open_row_limit: int
    difference_kinds: HoursDifferenceKinds


class HoursComparisonUnavailableData(TypedDict):
    kind: Literal['unavailable']
    reason: str


class PageData(TypedDict):
    read_at: int
    render_seconds: float
    launch_state: LaunchState
    spend: SpendData
    settled_notes: list[str]
    # Every region a table shows, for the region filter.
    regions: list[str]
    snapshot: list[SnapshotLine]
    prices: list[TableRowsData]
    placement_scores: TableData
    quotas: list[TableRowsData]
    quota_history: QuotaHistoryData
    cached_spot_status: dict[str, Literal['read failed', 'old']]
    runs: list[RunCostData]
    hours: HoursComparisonData | HoursComparisonUnavailableData


def table_rows_data(table: spot_table.Table) -> TableRowsData:
    return {'headers': list(table.headers), 'right': sorted(table.right),
            'rows': [list(row) for row in table.rows], 'region_axis': table.region_axis,
            'values': [[float(value) if value is not None else None for value in row] for row in table.values],
            'not_sold': [list(cell) for cell in sorted(table.not_sold)]}


def table_data(table: spot_table.Table) -> TableData:
    return {'title': table.title, **table_rows_data(table)}


def hours_row_data(row: spot_table.ComparedMachineHoursRow) -> ComparedHoursRowData:
    check = row.check
    billed = check.billed_hours
    limit_result: Literal['within_limit', 'higher_bill', 'lower_bill'] = 'within_limit'
    if check.hours_gap_over:
        limit_result = 'higher_bill' if billed > check.box_hours else 'lower_bill'
    billed_tenths = round(billed * 10)
    recorded_tenths = round(check.box_hours * 10)
    difference_kind = HOURS_DIFFERENCE_KINDS['by_tenths'].get(
        str(billed_tenths - recorded_tenths), HOURS_DIFFERENCE_KINDS['default'])
    compared: ComparedHoursRowData = {
        'size': row.size, 'region': row.region, 'limit_result': limit_result,
        'billed_tenths': billed_tenths, 'recorded_tenths': recorded_tenths, 'difference_kind': difference_kind,
        'first_utc_day': check.first_utc_day.isoformat(), 'last_utc_day': check.last_utc_day.isoformat(),
    }
    if billed >= check.box_hours and check.hana_reserved_hours > 0:
        compared['hana_reserved_tenths'] = round(check.hana_reserved_hours * 10)
    return compared


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


def gate_time_price_table(comparisons: Sequence[run_cost.GateTimeComparison], gate_name: str) -> spot_table.Table:
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
    return spot_table.Table(f'32-vCPU {gate_name} break-even', headers, frozenset((2, 3, 4, 6, 7, 8, 9)), rows, 'rows')


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
        return {'unavailable': f'AWS spend unavailable: {spend.reason}.', 'tiles': [],
                'forecast': '', 'refused_bills': []}
    today = datetime.fromtimestamp(now, UTC).date()
    yesterday = today - timedelta(days=1)
    tiles = [day_tile('Today', today.isoformat(), spend, True),
             day_tile('Yesterday', yesterday.isoformat(), spend, True)]
    month = aws_spend.month_spend(spend.days, today, pace_days)
    shown_estimates = {row.day for row in spend.days if row.source != 'AWS bill' and (
        row.day in (today.isoformat(), yesterday.isoformat())
        or (month is not None and row.day.startswith(f'{today:%Y-%m}')))}
    refused: list[RefusedBillData] = [
        {'days': labels, 'reason': record.reason.value}
        for record in spend.refused_bills
        if (labels := [day.isoformat() for day in record.days if day.isoformat() in shown_estimates])]
    if month is None:
        tiles.append({'label': 'Month to date', 'usd': '—', 'note': f'UTC {today:%Y-%m}: no billed day yet'})
        return {'unavailable': '', 'tiles': tiles, 'forecast': '', 'refused_bills': refused}
    tiles.append({'label': 'Month to date', 'usd': f'${month.to_date:.2f}',
                  'note': f'UTC {today:%Y-%m}, of the ${spend.monthly_limit:.2f} monthly limit.'})
    forecast = (f'Month forecast: ${month.forecast:.2f} at ${month.pace:.2f} a day, the average of the last '
                + f'{month.billed_days} billed days. 32-CPU gate compute is {month.gate_share:.0%} of the spend on those days.')
    return {'unavailable': '', 'tiles': tiles, 'forecast': forecast, 'refused_bills': refused}


def utc_spend_data(report: aws_spend.UtcSpendReport) -> SpendData:
    """Show the captured UTC amounts and their sources."""
    account, evidence = report.account, report.evidence
    if isinstance(account, ledger.SpendEvidenceUnavailable):
        return {'unavailable': f'AWS spend unavailable: {account.reason}.', 'tiles': [],
                'forecast': '', 'refused_bills': []}
    today = datetime.fromtimestamp(evidence.at, UTC).date()
    yesterday = today - timedelta(days=1)
    days = {row.day: row for row in account.days}
    tiles: list[SpendTile] = []
    for label, day in (('Today', today), ('Yesterday', yesterday)):
        row = days.get(day)
        if isinstance(row, ledger.UtcDaySpend):
            limit = f'${evidence.settings.daily_limit_usd:.2f}'
            note = f'UTC {day}, {aws_spend.utc_day_label(row)} of the {limit} daily limit. {row.gate_runs} gate runs.'
            tiles.append({'label': label, 'usd': f'${row.usd:.2f}', 'note': note})
        else:
            reason = row.reason if isinstance(row, ledger.UtcDayWithoutEvidence) else 'no spend evidence'
            tiles.append({'label': label, 'usd': '—', 'note': f'UTC {day}. {reason}.'})
    month = aws_spend.utc_month_to_date(account, today)
    has_amount = any(isinstance(day, ledger.UtcDaySpend) for day in month.days)
    tiles.append({'label': 'Month to date', 'usd': f'${month.usd:.2f}' if has_amount else '—',
                  'note': f'UTC {today:%Y-%m}, of the ${evidence.settings.monthly_limit_usd:.2f} monthly limit.'
                  if has_amount else f'UTC {today:%Y-%m}. No spend evidence.'})
    pace_days = evidence.forecast_pace_days
    if isinstance(pace_days, conf.UnavailableValue):
        raise conf.Unusable(pace_days.reason)
    forecast = aws_spend.utc_month_forecast(account, today, pace_days)
    forecast_line = ''
    if isinstance(forecast, aws_spend.UtcMonthForecast):
        forecast_line = (f'{forecast.display_line()} '
                         f'32-CPU gate compute is {forecast.gate_share:.0%} of the spend on those days.')
    estimate_dates = {row.day for row in (*month.days, days.get(yesterday))
                      if isinstance(row, ledger.UtcDaySpend) and isinstance(row.source, ledger.EstimatedUtcDaySpend)}
    refused: list[RefusedBillData] = [
        {'days': labels, 'reason': record.reason.value}
        for record in evidence.refused_bills
        if (labels := [day.isoformat() for day in record.days if day in estimate_dates])]
    return {'unavailable': '', 'tiles': tiles, 'forecast': forecast_line, 'refused_bills': refused}


def launch_state(records: Sequence[dict[str, object]], gate_switch: cloud_gate.CloudGateSetting) -> LaunchState:
    """Show the switch state or the latest launch evidence."""
    if gate_switch is cloud_gate.CloudGateSwitch.OFF:
        return {'kind': 'off'}
    if isinstance(gate_switch, cloud_gate.InvalidCloudGateSwitch):
        return {'kind': 'invalid'}
    requested_attempts = {attempt for row in records if row.get('result') in ('launched', 'refused')
                          and isinstance(attempt := row.get('attempt'), str) and attempt}
    counted_refusals: set[str] = set()
    state: LaunchState = {'kind': 'no-evidence'}
    for row in sorted(records, key=lambda row: float(str(row['time']))):
        evidence = classify_launch_row(row, requested_attempts)
        at = float(str(row['time']))
        if evidence is LaunchRowEvidence.LAUNCHED:
            state = {'kind': 'working', 'last_good_at': at, 'latest_attempt_at': at}
            counted_refusals.clear()
        elif isinstance(evidence, NormalLaunchRefusal):
            if state['kind'] != 'no-evidence':
                state['latest_attempt_at'] = at
        elif isinstance(evidence, OtherLaunchRefusal):
            attempt = row.get('attempt')
            count = state['count'] if state['kind'] == 'refused' else 0
            if not isinstance(attempt, str) or attempt not in counted_refusals:
                count += 1
                if isinstance(attempt, str) and attempt:
                    counted_refusals.add(attempt)
            refused: LaunchesRefused = {
                'kind': 'refused', 'code': evidence, 'latest_attempt_at': at,
                'reason': LAUNCH_REFUSAL_REASONS.get(evidence, 'The launch could not proceed.'),
                'first_at': state['first_at'] if state['kind'] == 'refused' else at,
                'count': count,
            }
            if state['kind'] == 'working':
                refused['last_good_at'] = state['last_good_at']
            elif state['kind'] == 'refused' and 'last_good_at' in state:
                refused['last_good_at'] = state['last_good_at']
            state = refused
    return state


class QuotaRequestOutcome(StrEnum):
    GRANTED = 'granted'
    OPEN = 'open'
    CLOSED_NOT_GRANTED = 'closed, not granted'
    CLOSED_QUOTA_UNKNOWN = 'closed, quota unknown'
    STATUS_UNKNOWN = 'status unknown'


class AppliedQuotaUnavailable(Enum):
    NOT_MEASURED = 'not measured'


def quota_request_outcome(request: quota_requests.QuotaRequest, applied: Decimal | AppliedQuotaUnavailable,
                          wanted: Decimal) -> QuotaRequestOutcome:
    if isinstance(applied, Decimal) and applied >= wanted:
        return QuotaRequestOutcome.GRANTED
    if request.status in quota_requests.CLOSED_QUOTA_REQUEST_STATUSES:
        return (QuotaRequestOutcome.CLOSED_NOT_GRANTED if isinstance(applied, Decimal)
                else QuotaRequestOutcome.CLOSED_QUOTA_UNKNOWN)
    if request.status is quota_requests.QuotaRequestStatus.UNKNOWN:
        return QuotaRequestOutcome.STATUS_UNKNOWN
    return QuotaRequestOutcome.OPEN


def quota_request_age(request: quota_requests.QuotaRequest, now: int) -> str:
    stamp = request.created_at if isinstance(request.created_at, datetime) else request.updated_at
    if not isinstance(stamp, datetime) or (seconds := now - stamp.timestamp()) < 0:
        return 'age unknown'
    hours = int(seconds // 3600)
    if hours >= 24:
        days = hours // 24
        return f'{days} day' + ('s' if days != 1 else '')
    if hours == 0:
        return 'less than 1 hour'
    return f'{hours} hour' + ('s' if hours != 1 else '')


def quota_request_note(reason: str) -> str:
    if reason.startswith('The AWS plan key is locked.'):
        return 'The plan identity is locked. Run github-warmup on natedev.'
    if reason.startswith('The AWS plan key is missing.'):
        return 'The plan identity is missing.'
    if reason == 'The quota request history deadline expired.':
        return 'The quota request history read timed out.'
    return 'The page could not measure quota request history.'


def quota_request_columns(reading: quota_requests.QuotaRequestReading,
                          applied: Decimal | AppliedQuotaUnavailable, now: int) -> QuotaRequestColumns:
    if isinstance(reading, quota_requests.QuotaRequest):
        wanted = reading.requested_vcpus
        outcome = quota_request_outcome(reading, applied, wanted)
        columns: QuotaRequestColumns = {'wanted': f'{wanted:f}', 'request': outcome, 'age': quota_request_age(reading, now)}
        if outcome in (QuotaRequestOutcome.CLOSED_QUOTA_UNKNOWN, QuotaRequestOutcome.STATUS_UNKNOWN):
            columns.update(request='not measured', detail=outcome)
        return columns
    unavailable: QuotaRequestColumns = {'wanted': '—', 'request': 'not measured', 'age': '—'}
    if isinstance(reading, quota_requests.NoQuotaRequest):
        unavailable['request'] = '—'
    elif isinstance(reading, quota_requests.QuotaRequestReadFailed):
        unavailable['detail'] = 'request read failed'
    elif reading.reason == 'The snapshot has no quota request history for this region.':
        unavailable['history_missing'] = True
    else:
        unavailable['note'] = quota_request_note(reading.reason)
    return unavailable


def quota_history_data(tables: spot_table.SpotTables, now: int) -> QuotaHistoryData:
    standard, gpu = (
        {row[0]: value if isinstance(value := numbers[1], Decimal) else AppliedQuotaUnavailable.NOT_MEASURED
         for row, numbers in zip(table.rows, table.values, strict=True)}
        for table in tables.quotas
    )
    regions_by_outcome: dict[QuotaRequestOutcome, list[str]] = {
        QuotaRequestOutcome.GRANTED: [],
        QuotaRequestOutcome.OPEN: [],
        QuotaRequestOutcome.CLOSED_NOT_GRANTED: [],
    }
    needed = Decimal(tables.gpu_quota_needed)
    data: QuotaHistoryData = {'standard': {}, 'gpu': {},
                             'gpu_summary': {'target_vcpus': tables.gpu_quota_needed,
                                             'regions_by_outcome': regions_by_outcome}}
    for history in tables.quota_requests:
        region = history.region
        applied = gpu[region]
        request = history.gpu
        data['standard'][region] = quota_request_columns(history.standard, standard[region], now)
        data['gpu'][region] = quota_request_columns(request, applied, now)
        if isinstance(applied, Decimal) and applied >= needed:
            regions_by_outcome[QuotaRequestOutcome.GRANTED].append(region)
        elif isinstance(request, quota_requests.QuotaRequest) and request.requested_vcpus >= needed:
            outcome = quota_request_outcome(request, applied, needed)
            if outcome in regions_by_outcome:
                regions_by_outcome[outcome].append(region)
    return data


def page_data(tables: spot_table.SpotTables, spend: SpendData, now: int,
              placement_readings: Sequence[ledger.SpotPlacementReading] = (), *,
              hours_table_open_row_limit: int,
              launch_records: Sequence[dict[str, object]] = (), settled_notes: Sequence[str] = (),
              gate_switch: cloud_gate.CloudGateSetting = cloud_gate.CloudGateSwitch.ON) -> PageData:
    # The billed hours table can add a region that is no longer a candidate.
    placement = placement_score_table(tuple(read.region for read in tables.regions), placement_readings, now)
    regions = {row[0] for row in placement.rows} | tables.hours.checked_regions
    hours: HoursComparisonData | HoursComparisonUnavailableData
    if isinstance(tables.hours, spot_table.HoursComparison):
        percent = format(tables.hours.max_gap_ratio, '%').removesuffix('%')
        if '.' in percent:
            percent = percent.rstrip('0').rstrip('.')
        hours = {
            'kind': 'comparison', 'limit_percent': f'{percent}%',
            'rows': [hours_row_data(row) for row in sorted(
                tables.hours.rows, key=lambda row: row.check.box_hours - row.check.billed_hours)],
            'open_row_limit': hours_table_open_row_limit,
            'difference_kinds': HOURS_DIFFERENCE_KINDS,
        }
    else:
        hours = {'kind': 'unavailable', 'reason': tables.hours.reason}
    return {
        'read_at': now,
        'render_seconds': 0.0,
        'launch_state': launch_state(launch_records, gate_switch),
        'spend': spend,
        'settled_notes': list(settled_notes),
        'regions': sorted(regions, key=region_filter_key),
        'snapshot': [{'region': region, 'text': text} for region, text in tables.snapshot_rows],
        'prices': [table_rows_data(table) for table in tables.prices],
        'placement_scores': table_data(placement),
        'quotas': [table_rows_data(table) for table in tables.quotas],
        'quota_history': quota_history_data(tables, now),
        'cached_spot_status': {read.region: 'read failed' if read.failure else 'old'
                               for read in tables.regions if read.failure or read.stale},
        'runs': [{'scope': costs.scope.name, 'label': costs.scope.label, 'explanation': costs.explanation,
                  'tables': [table_data(without_prices(table)) for table in costs.tables],
                  'prices': [table_rows_data(costs.gate_prices)],
                  'estimates': [{'region': entry.region, 'summary': costs.summary_of(entry)} for entry in costs.estimates],
                  'no_estimate': costs.summary_of(None),
                  'refused': table_data(costs.refused), 'launched': table_data(costs.launched)}
                 for costs in tables.runs],
        'hours': hours,
    }


def output_folder() -> Path:
    return Path(os.path.expandvars(conf.read('aws_page').text('output_folder'))).expanduser()


def quota_help() -> str:
    """Render the shared instructions with a link to the case list."""
    link = escape(console_help.CASE_LIST_URL)
    steps: list[str] = []
    for step in console_help.QUOTA_CASE_STEPS:
        text = escape(step).replace(link, f'<a href="{link}">{link}</a>')
        steps.append(f'<li>{text}</li>')
    return '<ol>' + ''.join(steps) + f'</ol>\n<p>{escape(console_help.WRONG_ACCOUNT)}</p>'


def settled_amount_notes(watched: Sequence[ledger.WatchedChargeDay]) -> tuple[str, ...]:
    unchanged = 0
    changes: list[str] = []
    for day in watched:
        settlement = day.settlement
        if isinstance(settlement, ledger.SettledChargeUnchanged):
            unchanged += 1
        elif isinstance(settlement, ledger.SettledChargeChanged):
            changes.append(settlement.display_line(day.day))
    settled = unchanged + len(changes)
    if not settled:
        return ()
    checked = datetime.fromtimestamp(max(day.checked_at for day in watched), UTC)
    summary = (f'Settled AWS amounts: {unchanged} of {settled} did not change after they settled '
               + f'(checked {checked:%b} {checked.day}).')
    return (summary, *changes)


@dataclass(frozen=True)
class PageAbsent:
    """The destination has no published page."""


@dataclass(frozen=True)
class LegacyPageRevision:
    stamp: ledger.SpendFileStamp


@dataclass(frozen=True)
class PublishedPageRevision:
    at: int
    source_digest: str
    stamp: ledger.SpendFileStamp


class PagePublicationDeferred(RuntimeError):
    """The sources do not support the first publication yet."""


def published_revision(path: Path) -> PageAbsent | LegacyPageRevision | PublishedPageRevision:
    try:
        with path.open('rb') as page:
            info = os.fstat(page.fileno())
            stamp = ledger.SpendFileStamp(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            _ = page.seek(max(0, info.st_size - 128))
            footer = page.read()
    except FileNotFoundError:
        return PageAbsent()
    metadata = re.search(rb'\n<!-- aws-spend-revision ([0-9]{1,20}) ([0-9a-f]{64}) -->\n$', footer)
    if metadata is None:
        return LegacyPageRevision(stamp)
    return PublishedPageRevision(int(metadata[1]), metadata[2].decode('ascii'), stamp)


def render_report_page(directory: Path, report: aws_spend.UtcSpendReport, started: float) -> str:
    evidence = report.evidence
    inputs = evidence.page_inputs
    if isinstance(inputs, ledger.SpendPageInputsNotCaptured):
        raise ledger.LedgerUnavailable('page capture requires the page inputs')
    now, values, configuration = evidence.at, evidence.gate_configuration, evidence.account_configuration
    spend = utc_spend_data(report)
    watched = report.account.settlement_notes if isinstance(report.account, ledger.UtcAccountSpend) else ()
    launches = evidence.launches
    gate_pricing = run_cost.gate_pricing_from(evidence, launches)
    tables = spot_table.spot_tables_from(directory, now, values, configuration, gate_pricing=gate_pricing,
                                         evidence=evidence)
    data = page_data(tables, spend, now, evidence.placement_scores, launch_records=launches,
                     hours_table_open_row_limit=configuration.whole('hours_table_open_row_limit'),
                     settled_notes=settled_amount_notes(watched), gate_switch=cloud_gate.launch_switch(values))
    comparisons = run_cost.merge_gate_comparisons(directory, now, values, configuration, gate_pricing=gate_pricing)
    gate_time_price = table_data(gate_time_price_table(comparisons, inputs.step_names.shown('merge-gate')))
    regions = sorted(set(data['regions']).union(row[0] for row in gate_time_price['rows']), key=region_filter_key)
    gate_time_data = {'table': gate_time_price, 'regions': regions}
    tip = shared_step_tip(inputs.step_tip_template)
    data['render_seconds'] = time.perf_counter() - started
    # Escape closing tags inside JSON values.
    return (inputs.frame_template
            .replace('__STEP_TIP_STYLE__', tip.style, 1)
            .replace('__AWS_QUOTA_HELP__', quota_help(), 1)
            .replace('__AWS_PRICES_DATA__', json.dumps(data).replace('</', '<\\/'))
            .replace('__AWS_GATE_TIME_DATA__', json.dumps(gate_time_data).replace('</', '<\\/')))


@contextmanager
def _page_publication_lock(folder: Path) -> Generator[LockResult]:
    """Serialize page replacement with or without an account ledger."""
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield LockResult.BUSY
        else:
            yield LockResult.ACQUIRED
    finally:
        os.close(descriptor)


def publish(directory: Path, now: int, folder: Path, *, settings_path: Path | None = None) -> Path:
    """Replace the page while the captured sources and the page revision match."""
    started = time.perf_counter()
    page_path = folder / PAGE_NAME
    account_path = settings_path if settings_path is not None else aws_config.account_settings_path()
    reason = 'The AWS page revision changed during publication.'
    for attempt in range(aws_spend.REPORT_ATTEMPTS):
        before = published_revision(page_path)
        report = aws_spend.read_utc_spend(directory, now, settings_path=account_path,
                                          purpose=ledger.SpendCapturePurpose.PAGE_PUBLICATION)
        if isinstance(report, ledger.SpendEvidenceUnavailable):
            reason = report.reason
            continue
        evidence = report.evidence
        digest = hashlib.sha256(repr((evidence.revision.sources, evidence.revision.configurations)).encode()).hexdigest()
        page = render_report_page(directory, report, started)
        with ledger.verify_spend_revision(directory, evidence) as verified:
            if isinstance(verified, ledger.SpendEvidenceUnavailable):
                reason = verified.reason
                continue
            with _page_publication_lock(folder) as publication:
                if publication is LockResult.BUSY:
                    reason = 'Another publisher holds the AWS page folder lock.'
                    continue
                current = published_revision(page_path)
                if current != before:
                    continue
                if isinstance(current, PublishedPageRevision) and (
                    current.at > evidence.at or (attempt > 0 and (current.at, current.source_digest) == (evidence.at, digest))
                ):
                    return page_path
                write_text_atomic(page_path, page + f'\n<!-- aws-spend-revision {evidence.at} {digest} -->\n', new_file_mode=0o644)
                return page_path
    if isinstance(published_revision(page_path), PageAbsent):
        raise PagePublicationDeferred(reason)
    return page_path


def publish_configured() -> int:
    """Publish the page from the AWS target state folder; the hourly job calls this."""
    try:
        _ = publish(target_config.ssh_target_state_directory('aws'), int(time.time()), output_folder(),
                    settings_path=aws_config.account_settings_path())
    except PagePublicationDeferred as error:
        print(f'aws_page: {error}', file=sys.stderr)
        return 1
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
        if status := publish_configured():
            return status
    print(page_url())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
