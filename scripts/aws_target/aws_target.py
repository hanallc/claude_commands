#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Reconcile Hana lifecycle state and refresh regional prices and account charges."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import re
import os
import subprocess
import sys
import time
from time import monotonic
from typing import cast, final

from scripts.aws_target import hana_spend, ledger, measurement, price_days, quota_requests, run_cost
from scripts.aws_target.aws_cli import aws_error_code
from scripts.aws_target.aws_cli import AwsCli as AwsCli, AwsExecutor as AwsExecutor, AwsUnavailable as AwsUnavailable, execute_command as execute_command
from scripts.aws_target.config import read_settings
from scripts.aws_target.controller_lock import control_lock as control_lock
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib import conf
from scripts.message import send_budget, send_result
from scripts.message.session_send import send_argv
from scripts.notify import push_texts
from scripts.lib.atomic_file import write_text_atomic


SNAPSHOT_ERROR_PATTERNS = (
    r'^An error occurred \(',
    r'^aws:\s*(?i:error|\[error\]):',
    r'^Invalid choice\b',
    r'^Parameter validation failed\b',
    r'^Unable to locate credentials\b',
    r'^Could not connect to the endpoint\b',
    r'^[A-Z][A-Za-z]*:\s',
)
SNAPSHOT_DUMP_LINE = re.compile(
    r'(?i)^(?:(?:request|response|headers?)\b|'
    + r'(?:[a-z0-9]+(?:-[a-z0-9]+)+|authorization|host|date|connection|cookie)\s*:|[{\[<])')
HOURS_GAP_RECIPIENT = 'staff:a898cb38-dbac-4751-b72b-d0b14ab9c38d'


def largest_gate_vcpus(instance_types: tuple[str, ...]) -> int:
    """Return the largest known count, or zero when all types have unknown counts."""
    return max((run_cost.gate_vcpus(name) for name in instance_types), default=0)


def spot_quota(api: AwsCli, code: str = quota_requests.STANDARD_SPOT_QUOTA) -> Decimal:
    """Read the applied regional Spot quota in vCPUs."""
    reply = api.call('get-service-quota', '--service-code', 'ec2',
                     '--quota-code', code, service='service-quotas')
    value = conf.decimal(str(ledger.mapping(reply['Quota'])['Value']))
    if not value.is_finite() or value < 0:
        raise ValueError('The Spot quota is invalid')
    return value


def fetch_spot_prices(api: AwsCli, sizes: Sequence[str], now: int) -> tuple[ledger.RegionalSpotPrice, ...]:
    """Read every page and select the latest price for each type and zone."""
    reply = api.call('describe-spot-price-history', '--instance-types', *sizes,
        '--product-descriptions', 'Linux/UNIX', '--start-time', datetime.fromtimestamp(now, UTC).isoformat())
    return ledger.regional_spot_prices(api.region, reply, sizes, now)


def read_snapshot(directory: Path) -> dict[str, object]:
    """Read the local price and quota snapshot without creating files."""
    try:
        return ledger.mapping(cast(object, json.loads((directory / 'spot-prices.json').read_text())))
    except FileNotFoundError:
        return {'prices': [], 'regions': []}


@dataclass(frozen=True)
class SnapshotFailure:
    region: str
    at: int
    read: str
    error: str
    reason: str


@dataclass(frozen=True)
class SnapshotFailureStreak:
    since: int
    notice_claimed: bool


@dataclass(frozen=True)
class SnapshotHealthy:
    """Every regional read succeeded."""


def snapshot_failures(document: dict[str, object]) -> tuple[SnapshotFailure, ...]:
    """Read the latest failed attempt for each region."""
    return tuple(SnapshotFailure(ledger.text(row, 'region'), ledger.instant(row, 'at'),
        ledger.text(row, 'read'), ledger.text(row, 'error'), str(row.get('reason', '')))
        for row in ledger.rows(document, 'failures')) if 'failures' in document else ()


def snapshot_failure_notice(key: str, push: push_texts.RenderedPush, timeout: float) -> bool:
    """Send a keyed user notice through the existing message interface."""
    try:
        result = subprocess.run(push_texts.user_send_argv(push, key=key),
                                capture_output=True, text=True, check=False, timeout=timeout)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def snapshot_permission_failure(failure: SnapshotFailure) -> bool:
    """Select permission failures that the gate deployment can repair."""
    return re.search(r'\b(?:AccessDenied(?:Exception)?|UnauthorizedOperation|not authorized)\b',
                     failure.error + ' ' + failure.reason, re.IGNORECASE) is not None


def snapshot_failure_push(failures: Sequence[SnapshotFailure]) -> push_texts.RenderedPush:
    """Name each regional permission error and its repair."""
    lines: list[str] = []
    for failure in failures:
        lines.append(f'AWS refused the {failure.region} {failure.read} read.')
    return push_texts.render('aws.snapshot_failed', failures='\n'.join(lines))


def gpu_price_sizes(values: conf.ConfigRead) -> tuple[str, ...]:
    """Include Hana GPU types when the price configuration has the new key."""
    hana = values.origin('hana_gpu_instance_types', str.split)
    return tuple(dict.fromkeys((*values.words('gpu_instance_types'),
                                *(hana.value if not isinstance(hana, conf.UnavailableValue) else ()))))


def gate_sizes(values: conf.ConfigRead) -> tuple[str, ...]:
    """Every type a cloud gate can launch or compare."""
    return tuple(dict.fromkeys((*values.words('instance_types'), *values.words('price_compare_instance_types'),
                                *gpu_price_sizes(values))))


def hours_check_scope(gate: conf.ConfigRead) -> run_cost.SpotHoursScope:
    sizes = gate_sizes(gate)
    try:
        configuration = conf.read_or_repository('hana_aws')
        region = configuration.text('region')
        primary = configuration.text('instance_type')
        hana = (primary, *hana_spend.read_fallback_instance_types(configuration, primary))
    except conf.Unusable:
        return run_cost.SpotHoursScope(sizes, {})
    return run_cost.SpotHoursScope(tuple(dict.fromkeys((*sizes, *hana))), {region: hana})


def spot_placement_requests(values: conf.ConfigRead) -> tuple[ledger.SpotPlacementRequest, ...]:
    """Use the same ordered batches for each hour of the collection."""
    regions = tuple(dict.fromkeys(values.words('regions')))
    batches = tuple(regions[start:start + 10] for start in range(0, len(regions), 10))
    gate_types = tuple(dict.fromkeys(values.words('instance_types')))
    gate_32_vcpu = tuple(size for size in gate_types if ledger.gate_vcpus(size) == 32)
    pair, all_types = ledger.SpotPlacementTypeSet.GATE_32_VCPU, ledger.SpotPlacementTypeSet.ALL_GATE_TYPES
    regional, zonal = ledger.SpotPlacementScope.REGION, ledger.SpotPlacementScope.ZONE
    shapes = (
        (regional, 8, pair, gate_32_vcpu),
        (regional, 8, all_types, gate_types),
        (zonal, 8, pair, gate_32_vcpu),
        (zonal, 8, all_types, gate_types),
        (regional, 20, all_types, gate_types),
    )
    return tuple(ledger.SpotPlacementRequest(scope, type_set, boxes, types, batch)
                 for scope, boxes, type_set, types in shapes if types for batch in batches)


def read_spot_placement(api: AwsCli, request: ledger.SpotPlacementRequest, now: int) -> ledger.SpotPlacementReading:
    """Keep the scores that AWS returns and record any failed request once."""
    zonal = request.scope is ledger.SpotPlacementScope.ZONE
    try:
        reply = api.call('get-spot-placement-scores', '--instance-types', *request.instance_types,
                         '--target-capacity', str(request.boxes), '--target-capacity-unit-type', 'units',
                         '--single-availability-zone' if zonal else '--no-single-availability-zone',
                         '--region-names', *request.regions)
        if reply.get('NextToken'):
            raise ValueError('The Spot placement score reply is incomplete')
        scores: dict[ledger.SpotPlacementLocation, ledger.SpotPlacementScore] = {}
        for row in ledger.rows(reply, 'SpotPlacementScores'):
            region = ledger.text(row, 'Region')
            if region not in request.regions:
                continue
            place = (ledger.SpotPlacementZone(region, ledger.text(row, 'AvailabilityZoneId')) if zonal
                     else ledger.SpotPlacementRegion(region))
            scores[place] = ledger.SpotPlacementScore(place, ledger.instant(row, 'Score'))
        return ledger.SpotPlacementSample(request, now, tuple(scores.values()))
    except AwsUnavailable as error:
        reason = aws_error_code(error, operation='GetSpotPlacementScores') or 'AWS request failed'
        return ledger.SpotPlacementFailure(request, now, reason)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return ledger.SpotPlacementFailure(request, now, type(error).__name__)


def collect_spot_placement(api: AwsCli, requests: Sequence[ledger.SpotPlacementRequest],
                           history: Sequence[ledger.SpotPlacementReading], now: int) -> tuple[ledger.SpotPlacementReading, ...]:
    """Refresh accepted shapes and add at most one shape after the initial shapes."""
    first = now - ledger.SPOT_PLACEMENT_RETENTION_SECONDS
    accepted = {row.request for row in history if isinstance(row, ledger.SpotPlacementSample) and first <= row.at <= now}
    recent = {row.request for row in history if now - 24 * 3600 < row.at <= now}
    shapes: dict[tuple[ledger.SpotPlacementScope, int, ledger.SpotPlacementTypeSet],
                 list[ledger.SpotPlacementRequest]] = {}
    for request in requests:
        shapes.setdefault((request.scope, request.boxes, request.type_set), []).append(request)
    scheduled: list[list[ledger.SpotPlacementRequest]] = []
    pending: list[list[ledger.SpotPlacementRequest]] = []
    initial_accepted = True
    for (scope, boxes, _), batches in shapes.items():
        if scope is ledger.SpotPlacementScope.REGION and boxes == 8:
            scheduled.append(batches)
            initial_accepted = initial_accepted and accepted.issuperset(batches)
        elif accepted.issuperset(batches):
            scheduled.append(batches)
        else:
            pending.append(batches)

    readings: list[ledger.SpotPlacementReading] = []
    can_start = True
    for batches in (*scheduled, *(pending[:1] if initial_accepted else ())):
        new = set(batches) - recent
        if not can_start and (new or batches in pending):
            continue
        if new and len(recent) + len(new) > ledger.SPOT_PLACEMENT_AWS_24_HOUR_CONFIGURATION_LIMIT:
            can_start = False
            continue
        recent.update(new)
        results = tuple(read_spot_placement(api, request, now) for request in batches)
        readings.extend(results)
        can_start = can_start and all(isinstance(result, ledger.SpotPlacementSample) for result in results)
    return tuple(readings)


def scaler_snapshot_api(configuration: conf.ConfigRead, credential_file: Path, execute: AwsExecutor,
                        *, single_attempt: bool = False) -> AwsCli:
    environment = {key: value for key, value in os.environ.items() if key not in (
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN',
        'AWS_CONFIG_FILE', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE')}
    environment['AWS_SHARED_CREDENTIALS_FILE'] = str(credential_file)
    environment['AWS_CONFIG_FILE'] = os.devnull
    environment['AWS_EC2_METADATA_DISABLED'] = 'true'
    if single_attempt:
        environment['AWS_MAX_ATTEMPTS'] = '1'

    def scaler_execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        _ = env
        return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=environment)

    return AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                  configuration.number('aws_timeout_s'), scaler_execute)


def placement_snapshot(settings_path: Path, directory: Path, *, execute: AwsExecutor = execute_command,
                       clock: Callable[[], float] = time.time) -> None:
    """Collect placement scores once per UTC hour, independent of prices."""
    from scripts.aws_target import admin_secrets

    try:
        credential_file = admin_secrets.credentials_file()
        if not credential_file.is_file():
            return
        requests = spot_placement_requests(conf.read_or_repository('cloud_gate'))
        api = scaler_snapshot_api(conf.read(settings_path), credential_file, execute, single_attempt=True)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'spot-placement-scores.lock').open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            now = int(clock())
            hour = str(now // 3600)
            _ = lock.seek(0)
            if lock.read().strip() == hour:
                return
            try:
                history = ledger.read_spot_placement_scores(directory)
            except (OSError, ValueError, KeyError) as error:
                print(f'aws_target: Spot placement history unreadable: {type(error).__name__}', file=sys.stderr)
                history = ()
            # Claim the hour before AWS calls so a restart does not repeat them.
            _ = lock.seek(0)
            _ = lock.truncate()
            _ = lock.write(hour)
            lock.flush()
            readings = collect_spot_placement(api, requests, history, now)
            ledger.write_spot_placement_scores(directory, (*history, *readings), now)
    except (OSError, ValueError, KeyError) as error:
        print(f'aws_target: Spot placement scores unavailable: {type(error).__name__}', file=sys.stderr)


def hours_gap_message(day: str, size: str, region: str, check: run_cost.HoursCheck, limit: Decimal) -> str:
    gap = (f'The gap is {check.hours_gap_ratio:.0%}.' if check.hours_gap_ratio is not None
           else 'The gap covers all billed hours.')
    reservation = ((f'The ledger also reserves {check.hana_reserved_hours:.2f} h of Hana box time in that region for that day.',)
                   if check.hana_reserved_hours else ())
    return '\n'.join((
        'AWS Spot hours differ from the cost ledger.',
        f'On {day} UTC, AWS billed {check.billed_hours or 0:.2f} h of {size} Spot in {region}.',
        f'The cost ledger records {check.box_hours:.2f} h for that size, region, and day.',
        *reservation,
        f'{gap} The limit is {limit:.0%}.',
        'File an investigation for this gap and add it to the list.',
    ))


def send_hours_gap(message: str, delivery_timeout: float) -> bool:
    """Check whether send.py confirms delivery or accepts the message for bob's queue."""
    command = send_argv('--to', HOURS_GAP_RECIPIENT, '--from', 'AWS hours check',
                        '--timeout', f'{delivery_timeout:g}', '--text', message)
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                timeout=send_budget.seconds(delivery_timeout))
    except (OSError, subprocess.SubprocessError):
        return False
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    return isinstance(outcome, send_result.ConfirmedSendOutcome) and outcome.header.outcome in ('sent', 'skipped', 'queued')


def hours_gap_notices(settings_path: Path, directory: Path, *, clock: Callable[[], float] = time.time) -> int:
    """Send bob one message per type, region, and UTC day above the gap limit."""
    configuration = conf.read(settings_path)
    limit = conf.decimal(configuration.text('billed_hours_gap_max_ratio'))
    end = datetime.fromtimestamp(clock(), UTC)
    days = configuration.whole('run_cost_days')
    start = end - timedelta(days=days)
    try:
        scope = hours_check_scope(conf.read_or_repository('cloud_gate'))
        checks = run_cost.day_hours_checks(ledger.CostLedger(directory), start, end, configuration.text('region'),
                                           configuration.text('instance_type'), scope, limit)
    except (ledger.LedgerUnavailable, OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        print(f'aws_target: hours check unavailable: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    path = directory / 'hours-gap-notices.json'
    saved = ledger.mapping(cast(object, json.loads(path.read_text()))).get('claimed') if path.exists() else None
    # A day before the window is never checked again, so its key is dropped.
    claimed = {key for key in (cast(list[object], saved) if isinstance(saved, list) else [])
               if isinstance(key, str) and key.rpartition(':')[2] >= start.date().isoformat()}
    def save() -> None:
        write_text_atomic(path, json.dumps({'claimed': sorted(claimed)}) + '\n')
    status = 0
    for (size, region, day), check in sorted(checks.items()):
        key = f'aws-hours-gap:{size}:{region}:{day}'
        if not check.hours_gap_over or key in claimed:
            continue
        message = hours_gap_message(day, size, region, check, limit)
        if send_hours_gap(message, configuration.number('snapshot_notification_timeout_s')):
            claimed.add(key)
            save()
        else:
            print(f'aws_target: hours gap notice for {size} in {region} on {day} remains pending', file=sys.stderr)
            status = 1
    save()
    return status


def read_snapshot_quota_requests(regions: tuple[str, ...], timeout: float,
                                 execute: AwsExecutor) -> tuple[quota_requests.RegionalQuotaRequests, ...]:
    """Collect independent quota request readings with the plan identity."""
    from scripts.aws_target import plan_identity, plan_run

    if not regions:
        return ()
    deadline = monotonic() + timeout
    missed_deadline = quota_requests.QuotaRequestsNotMeasured('The quota request history deadline expired.')
    plan = plan_run.PlanRun(plan_identity.plan_credentials, execute, time.sleep)

    def read(region: str, code: str) -> quota_requests.QuotaRequestReading:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return missed_deadline
        api = plan.api(region, remaining)
        try:
            requests = quota_requests.read_quota_requests(api, code)
            reading: quota_requests.QuotaRequestReading = max(requests, key=lambda request: request.order_time,
                                                              default=quota_requests.NoQuotaRequest())
        except Exception as error:
            code_text = aws_error_code(error) if isinstance(error, AwsUnavailable) else ''
            known_codes = {'AccessDenied', 'AccessDeniedException', 'NoSuchResourceException',
                           'ServiceException', 'ThrottlingException', 'TooManyRequestsException'}
            reading = quota_requests.QuotaRequestReadFailed(
                code_text if code_text in known_codes else 'QuotaHistoryUnavailable')
        return reading if monotonic() < deadline else missed_deadline

    workers = ThreadPoolExecutor(max_workers=8)
    try:
        try:
            _ = workers.submit(lambda: plan.credential).result(timeout=max(0, deadline - monotonic()))
        except TimeoutError:
            return tuple(quota_requests.RegionalQuotaRequests(region, missed_deadline, missed_deadline)
                         for region in regions)
        except plan_identity.PlanIdentityRefused as error:
            not_measured = quota_requests.QuotaRequestsNotMeasured(str(error))
            return tuple(quota_requests.RegionalQuotaRequests(region, not_measured, not_measured)
                         for region in regions)
        codes = (quota_requests.STANDARD_SPOT_QUOTA, quota_requests.GPU_SPOT_QUOTA)
        pending = {(region, code): workers.submit(read, region, code) for region in regions for code in codes}
        done, _ = wait(pending.values(), timeout=max(0, deadline - monotonic()))
        readings = {key: future.result() if future in done else missed_deadline for key, future in pending.items()}
        return tuple(quota_requests.RegionalQuotaRequests(
            region, readings[region, codes[0]], readings[region, codes[1]]) for region in regions)
    finally:
        workers.shutdown(wait=False, cancel_futures=True)


def snapshot(settings_path: Path, directory: Path, *, execute: AwsExecutor = execute_command,
             clock: Callable[[], float] = time.time,
             notify: Callable[[str, push_texts.RenderedPush, float], bool] = snapshot_failure_notice,
             force: bool = False) -> int:
    """Replace successful regional reads and preserve failed regional reads.

    force reads before the refresh interval ends and does not move the interval; deploy.py up uses it."""
    # Import here because administrator deployment also uses the shared read functions.
    from scripts.aws_target import admin_secrets

    configuration = conf.read(settings_path)
    values = conf.read_or_repository('cloud_gate')
    regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
    sizes = gate_sizes(values)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'spot-prices.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = int(clock())
        _ = lock.seek(0)
        try:
            last = int(lock.read().strip() or '0')
        except ValueError:
            last = 0
        if not force and last and 0 <= now - last < configuration.whole('price_refresh_seconds'):
            return 0
        api = scaler_snapshot_api(configuration, admin_secrets.credentials_file(), execute)
        document = read_snapshot(directory)
        prices = list(ledger.rows(document, 'prices'))
        readings = list(ledger.rows(document, 'regions')) if 'regions' in document else []
        reason_limit = configuration.whole('snapshot_error_reason_max_chars')
        failures = {row.region: row for row in snapshot_failures(document)}
        status = 0
        read_prices: list[ledger.RegionalSpotPrice] = []
        for region in regions:
            read_name = 'price'
            try:
                target = api.in_region(region)
                regional = fetch_spot_prices(target, sizes, now)
                read_name = 'Standard quota'
                standard = spot_quota(target)
                read_name = 'G and VT quota'
                gpu = spot_quota(target, quota_requests.GPU_SPOT_QUOTA)
                prices = [row for row in prices if row.get('region') != region]
                prices.extend(dict(region=row.region, zone=row.zone, instance_type=row.instance_type,
                    usd_per_hour=str(row.usd_per_hour), read_at=row.read_at) for row in regional)
                readings = [row for row in readings if row.get('region') != region]
                readings.append(dict(region=region, read_at=now, standard_vcpus=str(standard), gpu_vcpus=str(gpu)))
                read_prices.extend(regional)
                _ = failures.pop(region, None)
            except (AwsUnavailable, OSError, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError) as error:
                raw_lines: list[str] = []
                for raw_line in str(error).splitlines():
                    if SNAPSHOT_DUMP_LINE.match(raw_line.lstrip()):
                        break
                    raw_lines.append(raw_line)
                redacted = admin_secrets.redact('\n'.join(raw_lines))
                lines = [line.strip() for line in redacted.splitlines() if line.strip()]
                error_line = next((line for line in lines if any(re.match(pattern, line)
                    for pattern in SNAPSHOT_ERROR_PATTERNS)), lines[0] if lines else '')
                reason = admin_secrets.redact(' '.join(error_line.split()))[:reason_limit]
                print(f'aws_target: snapshot {region} {read_name} unavailable: {type(error).__name__}: {reason}', file=sys.stderr)
                failures[region] = SnapshotFailure(region, now, read_name, type(error).__name__, reason)
                status = 1
        current_failures = tuple(failures[region] for region in regions if region in failures)
        output: dict[str, object] = {'prices': prices, 'regions': readings, 'failures': [
            dict(region=row.region, at=row.at, read=row.read, error=row.error, reason=row.reason)
            for row in current_failures]}
        try:
            request_readings = read_snapshot_quota_requests(regions, api.timeout_s, execute)
        except Exception:
            unavailable = quota_requests.QuotaRequestReadFailed('QuotaHistoryUnavailable')
            request_readings = tuple(quota_requests.RegionalQuotaRequests(region, unavailable, unavailable)
                                     for region in regions)
        output['quota_requests'] = [
            {'region': row.region, 'standard': quota_requests.request_record(row.standard),
             'gpu': quota_requests.request_record(row.gpu)}
            for row in request_readings]
        # Success drops an unsent notice because no user action remains.
        streak: SnapshotFailureStreak | SnapshotHealthy = SnapshotHealthy()
        if current_failures:
            previous = ledger.mapping(document['failure_streak']) if 'failure_streak' in document else {}
            since = ledger.instant(previous, 'since') if previous else now
            notice_claimed = previous.get('notice_claimed', previous.get('notified')) is True
            streak = SnapshotFailureStreak(since, notice_claimed)
            output['failure_streak'] = {'since': streak.since, 'notice_claimed': streak.notice_claimed}
        write_text_atomic(directory / 'spot-prices.json', json.dumps(output, sort_keys=True) + '\n')
        try:
            price_days.record(directory, read_prices, now, configuration.whole('spot_price_days_keep'))
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            print(f'aws_target: daily Spot prices not saved: {type(error).__name__}: {error}', file=sys.stderr)
            status = 1
        access_failures = tuple(row for row in current_failures if snapshot_permission_failure(row))
        if access_failures and isinstance(streak, SnapshotFailureStreak) and not streak.notice_claimed:
            # Save the claim before delivery because note keys alone do not suppress repeats.
            output['failure_streak'] = {'since': streak.since, 'notice_claimed': True}
            write_text_atomic(directory / 'spot-prices.json', json.dumps(output, sort_keys=True) + '\n')
            if not notify(f'aws-spot-snapshot:{streak.since}', snapshot_failure_push(access_failures),
                          configuration.number('snapshot_notification_timeout_s')):
                output['failure_streak'] = {'since': streak.since, 'notice_claimed': False}
                write_text_atomic(directory / 'spot-prices.json', json.dumps(output, sort_keys=True) + '\n')
                print('aws_target: snapshot user notice remains pending', file=sys.stderr)
        # The stamp is the last scheduled read. A forced read leaves it, so the next hourly read still runs.
        if not force:
            _ = lock.seek(0)
            _ = lock.truncate()
            _ = lock.write(str(now))
        return status


USAGE_ONLY = json.dumps({'Not': {'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit', 'Refund']}}})


def fetch_account_charges(api: AwsCli, start: int, end: int, now: int,
                          read_usd: Decimal, *,
                          before_page: Callable[[], None] = lambda: None) -> tuple[ledger.ChargeRead, ...]:
    """Read daily account costs by usage type and count every API page."""
    period = f'Start={datetime.fromtimestamp(start, UTC):%Y-%m-%d},End={datetime.fromtimestamp(end, UTC):%Y-%m-%d}'
    days: dict[str, ledger.ChargeRead] = {}
    token = ''
    pages = 0
    while True:
        before_page()
        arguments = ['--time-period', period, '--granularity', 'DAILY', '--metrics', 'UnblendedCost', 'UsageQuantity',
                     '--group-by', 'Type=DIMENSION,Key=USAGE_TYPE', '--filter', USAGE_ONLY, '--no-paginate']
        if token:
            arguments.extend(('--next-page-token', token))
        reply = api.call('get-cost-and-usage', *arguments, service='ce')
        items = ledger.rows(reply, 'ResultsByTime')
        if not items:
            raise ValueError('Cost Explorer daily reply is unavailable')
        pages += 1
        for item in items:
            bounds = ledger.mapping(item['TimePeriod'])
            day = ledger.text(bounds, 'Start')
            usage: list[ledger.UsageTypeCost] = []
            for group in ledger.rows(item, 'Groups'):
                keys = cast(list[str], group['Keys'])
                metrics = ledger.mapping(group['Metrics'])
                cost = ledger.mapping(metrics['UnblendedCost'])
                quantity = ledger.mapping(metrics['UsageQuantity'])
                if cost.get('Unit') != 'USD':
                    raise ValueError('Cost Explorer requires USD')
                usage.append(ledger.UsageTypeCost(keys[0], conf.decimal(ledger.text(cost, 'Amount')),
                                                  conf.decimal(ledger.text(quantity, 'Amount')), ledger.text(quantity, 'Unit')))
            previous = days.get(day)
            combined: dict[str, ledger.UsageTypeCost] = {}
            for row in (*previous.usage_types, *usage) if previous else usage:
                old = combined.get(row.usage_type, ledger.UsageTypeCost(row.usage_type, Decimal(0)))
                combined[row.usage_type] = replace(row, usd=old.usd + row.usd, quantity=old.quantity + row.quantity)
            types = tuple(row for _, row in sorted(combined.items()))
            days[day] = ledger.ChargeRead(day, now,
                int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp()),
                int(datetime.fromisoformat(ledger.text(bounds, 'End')).replace(tzinfo=UTC).timestamp()),
                sum((row.usd for row in types), Decimal(0)), Decimal(0), Decimal(0), types,
                item['Estimated'] is True or bool(previous and previous.provisional))
        token = str(reply.get('NextPageToken', ''))
        if not token:
            break
    readings = tuple(days[day] for day in sorted(days))
    return (*readings[:-1], replace(readings[-1], query_usd=read_usd * pages))


@final
class AccountRefresh:
    def __init__(self, settings_path: Path, directory: Path, *,
                 execute: AwsExecutor = execute_command, clock: Callable[[], float] = time.time) -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.execute = execute
        self.clock = clock

    def tick(self) -> None:
        now = int(self.clock())
        configuration = conf.read(self.settings_path)
        api = AwsCli(configuration.text('credentials_profile'), configuration.text('region'),
                     configuration.number('aws_timeout_s'), self.execute)
        _ = self.refresh_charges(api, now)

    def refresh_charges(self, api: AwsCli, now: int) -> measurement.ChargeRead | measurement.MeasurementUnavailable:

        def fetch(start: int, end: int) -> tuple[ledger.ChargeRead, ...]:
            settings = read_settings(self.settings_path)
            return fetch_account_charges(api, start, end, now, settings.cost_explorer_read_usd)

        return measurement.read_charges(self.settings_path, self.directory, now, fetch)


def controller_tick(settings_path: Path, directory: Path, workload_path: Path, *,
                    lifecycle: Callable[[], object] | None = None, refresh: Callable[[], object] | None = None) -> int:
    """Recover lifecycle state before an independent account refresh attempt."""
    from scripts.aws_target.hana_box import HanaController
    from scripts.aws_target.hana_provider import AwsHanaProvider
    status = 0
    controller = HanaController(directory, settings_path, workload_path,
        AwsHanaProvider.from_settings(settings_path, directory, workload_path))
    for name, step in (('Hana lifecycle', controller.tick if lifecycle is None else lifecycle),
                       ('account refresh', AccountRefresh(settings_path, directory).tick if refresh is None else refresh)):
        try:
            _ = step()
        except (OSError, ValueError, RuntimeError, ArithmeticError, subprocess.SubprocessError) as error:
            detail = f'{type(error).__name__}: {error}' if name == 'Hana lifecycle' else type(error).__name__
            print(f'aws_target: {name} unavailable: {detail}', file=sys.stderr)
            status = 1
    return status


def serve(settings_path: Path, directory: Path, workload_path: Path, *,
          run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
          pause: Callable[[float], None] = time.sleep, keep_running: Callable[[], bool] = lambda: True) -> int:
    """Bound each tick in a child process and release all tick locks between attempts."""
    from scripts.aws_target.hana_box import read_settings as lifecycle_settings
    while keep_running():
        settings = lifecycle_settings(conf.read(workload_path))
        try:
            _ = run([sys.executable, str(Path(__file__).resolve()), 'tick', '--settings', str(settings_path),
                '--directory', str(directory), '--hana-settings', str(workload_path)],
                timeout=settings.controller_tick_timeout_s, check=False, text=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(f'aws_target: bounded tick failed: {type(error).__name__}', file=sys.stderr)
        pause(settings.tick_interval_s)
    return 0


def hourly(settings_path: Path, directory: Path) -> int:
    """Read prices and placement scores and check billed hours."""
    status = 0
    for label, operation in (('price snapshot', snapshot), ('hours-gap check', hours_gap_notices)):
        try:
            status = max(status, operation(settings_path, directory))
        except Exception as error:
            print(f'aws_target: {label} failed: {type(error).__name__}', file=sys.stderr)
            status = max(status, 1)
    try:
        placement_snapshot(settings_path, directory)
    except Exception as error:
        print(f'aws_target: placement snapshot failed: {type(error).__name__}', file=sys.stderr)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('tick', 'snapshot', 'serve', 'open-gates'))
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--hana-settings', type=Path)
    args = parser.parse_args(namespace=RefreshArguments())
    if args.command == 'open-gates':
        from scripts.aws_target.open_gates import report
        return report(args.directory, int(time.time()))
    if args.command == 'snapshot':
        return hourly(args.settings, args.directory)
    if args.command == 'serve':
        return serve(args.settings, args.directory, args.hana_settings)
    return controller_tick(args.settings, args.directory, args.hana_settings)


class RefreshArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = ssh_target_state_directory('aws')
        self.hana_settings: Path = conf.path('hana_aws')


if __name__ == '__main__':
    raise SystemExit(main())
