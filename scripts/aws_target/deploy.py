#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Prepare local evidence and deploy resources only through an explicit user command."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import base64
from configparser import ConfigParser
from io import StringIO
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from enum import StrEnum
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from statistics import median
import subprocess
import sys
import tempfile
import threading
import time
from typing import ClassVar, cast, final
from uuid import uuid4


from scripts.aws_target.controller_lock import HanaCommandAccess, account_stack_lock, hana_command_lock
from scripts.aws_target import admin_secrets, aws_test_identity, box, hana_box, hana_image, hana_replacement, hana_replacement_record, hana_spend, ledger, plan_identity, plan_run, remote, retained, spot_end, teardown
from scripts.aws_target.aws_cli import aws_error_code
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, execute_command, fetch_account_charges, fetch_spot_prices, spot_quota, largest_gate_vcpus as largest_gate_vcpus, snapshot as read_price_snapshot
from scripts.aws_target.hana_provider import AwsHanaProvider
from scripts.aws_target.config import BudgetNotificationAddress, DeploymentSettings, money, read_budget_notification_address, deployment_record, gate_deployment, gate_regions, read_deployment_settings, read_settings
from scripts.lib import conf
from scripts.build_targets import target_config
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic
from scripts.lib.file_lock import LockResult

TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/plans/builds-aws/cloudformation.json'
HANA_TEMPLATE = TEMPLATE.with_name('hana-cloudformation.json')
QUOTA_PLAN_AUTH_FAILURES = frozenset(('InvalidClientTokenId', 'UnrecognizedClientException',
                                      'AuthFailure', 'SignatureDoesNotMatch'))
PROBE_DENIED_CODES = frozenset(('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation',
                               'Client.UnauthorizedOperation', 'Forbidden', '401', '403'))
HANA_SETUP_VERSION = 2
HANA_DATA_DEVICE = '/dev/sdf'
HANA_DOWN_FIRST = 'Run deploy.py down --workload hana --approve-destroy first.'
HANA_SPOT_REFUSALS = frozenset(('InsufficientInstanceCapacity', 'SpotMaxPriceTooLow', 'Unsupported',
                               'MaxSpotInstanceCountExceeded'))
HANA_CREATE_REFUSALS = frozenset((
    'AccessDenied', 'AccessDeniedException', 'AuthFailure', 'UnauthorizedOperation',
    'ClientInvalidParameterValue', 'InvalidParameter', 'InvalidParameterCombination', 'InvalidParameterValue',
    'MissingParameter', 'ValidationError',
    'InvalidAMIID.NotFound', 'InvalidSubnetID.NotFound', 'InvalidGroup.NotFound',
    'InsufficientFreeAddressesInSubnet',
    'VolumeLimitExceeded', 'VolumeTypeNotAvailableInZone', 'UnknownVolumeType',
    'UnsupportedOperation', 'TagPolicyViolation',
))


def _operation_failure(operation: str, error: Exception) -> str:
    code = aws_error_code(error) if isinstance(error, AwsUnavailable) else ''
    return f'{operation}: fail' + (f' ({code})' if code else '')


@dataclass(frozen=True)
class ScalerProbe:
    operation: str
    call: Callable[[AwsCli], object]
    success_code: str = 'OK'


@dataclass(frozen=True)
class ScalerProbeUnverified:
    reason: str = ''


@dataclass(frozen=True)
class ScalerProbePassed:
    pass


@dataclass(frozen=True)
class ScalerProbeFailure:
    operation: str
    code: str

    def next_step(self) -> str:
        if self.code in PROBE_DENIED_CODES or self.code in ('AuthFailure', 'TerminationGrantDenied'):
            return 'Check the gate scaler permissions and repeat approved setup.'
        if ((self.operation, self.code) == ('get-cost-and-usage', 'LimitExceededException')
                or self.code.startswith(('Throttl', 'TooManyRequests', 'RequestTimeout'))
                or self.code in ('TimeoutExpired', 'TimeoutError', 'RequestLimitExceeded', 'SlowDown', '429',
                                 'InternalError', 'InternalFailure', 'ServiceUnavailable')
                or re.fullmatch(r'5[0-9]{2}', self.code)):
            return 'Run approved setup again.'
        return 'Check the failed operation and repeat approved setup.'


class TerminationGrantDenied(ValueError):
    pass


class ScalerIdentityMismatch(ValueError):
    pass


def run_scaler_probes(label: str, api: AwsCli, probes: Iterable[ScalerProbe], *,
                      show_codes: bool = False) -> tuple[ScalerProbeFailure, ...]:
    failed: list[ScalerProbeFailure] = []
    for probe in probes:
        try:
            result = probe.call(api)
        except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            code = aws_error_code(error, allow_status=show_codes) if isinstance(error, AwsUnavailable) else ''
            code = code or (type(error).__name__ if show_codes else '')
            failed.append(ScalerProbeFailure(probe.operation, code))
            if isinstance(error, ScalerIdentityMismatch):
                report = f'{probe.operation}: fail. The scaler profile does not match the stack user.'
            else:
                report = f'{probe.operation}: fail' + (f' ({code})' if code else '')
        else:
            if isinstance(result, ScalerProbeUnverified):
                report = f'{probe.operation} unverified.'
                if result.reason:
                    report += f' {result.reason}'
            else:
                report = f'{probe.operation}: pass'
                if show_codes:
                    report += f' ({probe.success_code})'
        print(f'{label}: {report}', flush=True)
    return tuple(failed)


def put_probe_object(api: AwsCli, bucket: str, key: str, content: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix='scaler-probe-') as folder:
        body = Path(folder) / 'probe.json'
        _ = body.write_bytes(content)
        _ = api.call('put-object', '--bucket', bucket, '--key', key, '--body', str(body), service='s3api')


def get_probe_object(api: AwsCli, bucket: str, key: str, content: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix='scaler-probe-') as folder:
        body = Path(folder) / 'probe.json'
        _ = api.call('get-object', '--bucket', bucket, '--key', key, str(body), service='s3api')
        if body.read_bytes() != content:
            raise ValueError('The probe object differs from the value sent')


@contextmanager
def step(command: str, text: str, settings: Path, *, hint: str = '') -> Generator[None]:
    """Show progress until the operation ends."""
    interval = conf.read(settings).number('deploy_progress_interval_s')
    stream = sys.stdout
    started = time.monotonic()
    stopped = threading.Event()
    start = f'{text} ({hint})' if hint else text
    print(f'{command}: {start}', file=stream, flush=True)

    def heartbeat() -> None:
        while not stopped.wait(interval):
            minutes = int((time.monotonic() - started) / 60)
            print(f'{command}: still {text}, {minutes} min', file=stream, flush=True)

    worker = threading.Thread(target=heartbeat, name='deployment-progress', daemon=True)
    worker.start()
    completed = False
    try:
        yield
        completed = True
    finally:
        stopped.set()
        worker.join()
        elapsed = time.monotonic() - started
        outcome = 'done in' if completed else 'failed after'
        print(f'{command}: {text} {outcome} {elapsed:.0f} s', file=stream, flush=True)


class DeploymentRefused(ValueError):
    """Current evidence does not permit a resource request."""


class HanaControlRefused(DeploymentRefused):
    """Another command holds exclusive control of Hana."""

    def __init__(self, command: str) -> None:
        super().__init__(f'{command}: Another command is using the Hana resources. '
                         + 'Run the same command again when that command ends.')


class HanaSetupTimingRefused(DeploymentRefused):
    """The settings do not give Hana enough time to stop."""


@dataclass(frozen=True)
class HanaSetupTiming:
    idle_wait_s: int
    heartbeat_s: int
    stop_wait_s: int
    stop_poll_s: int

    @classmethod
    def read(cls, workload: conf.ConfigRead) -> HanaSetupTiming:
        idle_wait = workload.whole('setup_idle_wait_timeout_s')
        heartbeat = workload.whole('setup_idle_heartbeat_s')
        warm_idle = workload.whole('idle_stop_seconds')
        stop_wait = workload.whole('stop_wait_timeout_s')
        stop_poll = workload.whole('stop_poll_seconds')
        if min(idle_wait, heartbeat, warm_idle, stop_wait, stop_poll) <= 0:
            raise HanaSetupTimingRefused('Hana setup requires positive idle and stop intervals.')
        if idle_wait <= warm_idle + stop_wait:
            raise HanaSetupTimingRefused(
                'setup_idle_wait_timeout_s must exceed idle_stop_seconds plus stop_wait_timeout_s.')
        return cls(idle_wait, heartbeat, stop_wait, stop_poll)


class HanaCreateRolledBack(AwsUnavailable):
    """The create failed and cleanup removed its resources."""


@dataclass(frozen=True)
class HanaSetupChange:
    """Allow the stack changes needed for setup."""


class HanaBoxOwnership(StrEnum):
    STACK = 'stack'
    DIRECT = 'direct'


@dataclass(frozen=True)
class HanaPolicyUpdate:
    resource_outputs: dict[str, str]
    ownership: HanaBoxOwnership = HanaBoxOwnership.DIRECT
    only_scaler: bool = False


@dataclass(frozen=True)
class HanaPolicyStack:
    settings: DeploymentSettings
    outputs: dict[str, str]
    parameters: dict[str, str]
    template: dict[str, object]
    scope: HanaPolicyUpdate
    identity: hana_box.DeploymentIdentity


HANA_SETUP_CHANGE = HanaSetupChange()


class GateRegionOperation(StrEnum):
    NETWORK = 'network'
    STACK = 'stack'


@dataclass
class GateRegionTimings:
    """Keep successful regional durations for the next deployment estimate."""

    _LABELS: ClassVar[dict[GateRegionOperation, str]] = {
        GateRegionOperation.NETWORK: 'preparing the network and image',
        GateRegionOperation.STACK: 'updating the gate stack',
    }
    _path: Path
    _seconds: dict[GateRegionOperation, dict[str, float]]

    @classmethod
    def read(cls, directory: Path) -> GateRegionTimings:
        path = directory / 'gate-region-times.json'
        try:
            saved = remote.read_record(path)
            if saved.get('version') != 1:
                raise ValueError('Unsupported regional timing version')
            operations = ledger.mapping(saved['seconds'])
            seconds: dict[GateRegionOperation, dict[str, float]] = {}
            for operation in GateRegionOperation:
                if operation not in operations:
                    continue
                seconds[operation] = {}
                for region, value in ledger.mapping(operations[operation]).items():
                    if (isinstance(value, bool) or not isinstance(value, (int, float))
                            or not math.isfinite(value) or value < 0):
                        raise ValueError('Invalid regional duration')
                    seconds[operation][region] = float(value)
            return cls(path, seconds)
        except (OSError, ValueError, KeyError, OverflowError):
            return cls(path, {})

    def announce(self, operation: GateRegionOperation, regions: Sequence[str]) -> None:
        if not regions:
            return
        saved = self._seconds.get(operation, {})
        if saved:
            typical = median(saved.values())
            total = sum(saved.get(region, typical) for region in regions)
            estimate = f'expected total {total:.0f} s from saved regional times'
        else:
            estimate = 'expected total is not yet measured'
        print(f'cloud-gate: {self._LABELS[operation]} in {len(regions)} regions: {estimate}', flush=True)

    @contextmanager
    def measure(self, operation: GateRegionOperation, region: str, position: int, total: int,
                settings: Path) -> Generator[None]:
        started = time.monotonic()
        with step(f'cloud-gate: {region}', self._LABELS[operation], settings, hint=f'{position} of {total}'):
            yield
        self._seconds.setdefault(operation, {})[region] = time.monotonic() - started
        record = json.dumps({'version': 1, 'seconds': self._seconds}, sort_keys=True) + '\n'
        try:
            write_text_atomic(self._path, record)
        except OSError:
            print(f'cloud-gate: warning: could not write {self._path.name}', flush=True)


@dataclass
class RegionalStackDigests:
    """Store evidence for regional stacks that completed deployment."""

    path: Path
    entries: dict[str, object]
    _warned: bool = False

    @classmethod
    def read(cls, directory: Path) -> RegionalStackDigests:
        cache = cls(directory / 'regional-stack-digests.json', {})
        try:
            entries = remote.read_record(cache.path)
            for value in entries.values():
                entry = ledger.mapping(value)
                if entry.keys() != {'digest', 'StackId', 'LastUpdatedTime'}:
                    raise ValueError('Invalid regional stack digest fields')
                if not re.fullmatch(r'[0-9a-f]{64}', ledger.text(entry, 'digest')):
                    raise ValueError('Invalid regional stack digest')
                _ = ledger.text(entry, 'StackId')
                if entry['LastUpdatedTime'] is not None:
                    _ = ledger.text(entry, 'LastUpdatedTime')
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            cache._warn()
        else:
            cache.entries = entries
        return cache

    def save(self) -> None:
        try:
            write_text_atomic(self.path, json.dumps(self.entries, sort_keys=True) + '\n', new_file_mode=0o600)
        except OSError:
            self._warn()

    def _warn(self) -> None:
        if not self._warned:
            print(f'cloud-gate: warning: {self.path.name} is unavailable', flush=True)
            self._warned = True


def regional_stack_digest(digest: str, stack: dict[str, object]) -> dict[str, object]:
    """Bind the digest to a complete stack with a known identity."""
    status = stack.get('StackStatus')
    stack_id = stack.get('StackId')
    updated = stack.get('LastUpdatedTime')
    if status not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE') or not isinstance(stack_id, str) or not stack_id:
        return {}
    # CloudFormation omits LastUpdatedTime until the first update.
    if (updated is None and status == 'CREATE_COMPLETE') or (isinstance(updated, str) and updated):
        return {'digest': digest, 'StackId': stack_id, 'LastUpdatedTime': updated}
    return {}


def quota_targets(home: str) -> tuple[tuple[tuple[str, ...], str, int, str], ...]:
    """Share the gate regions and quota codes between requests and reports.

    A region with only a price read has no gate stack, so a quota request there buys nothing."""
    values = conf.read_or_repository('cloud_gate')
    regions = gate_regions(home, values)
    return (
        (regions, 'L-34B43A08', values.whole('spot_quota_request_vcpus'), 'Spot'),
        (regions, 'L-3819A6DF', values.whole('gpu_spot_quota_request_vcpus'), 'G and VT Spot'),
    )


def quota_timestamp(value: object) -> float:
    """Convert AWS timestamps to seconds for request ordering."""
    if value is None:
        return float('-inf')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    else:
        raise ValueError('The quota request date is invalid')
    return moment.timestamp()


def _quota_request_age(created: float, now: float) -> str:
    """Show the age in the largest whole unit."""
    if not math.isfinite(created):
        return 'age unknown'
    elapsed = max(0, int(now - created))
    for unit, seconds in (('d', 86400), ('h', 3600), ('m', 60)):
        if elapsed >= seconds:
            return f'{elapsed // seconds}{unit} old'
    return f'{elapsed}s old'


class _KnownQuotaRequestStatus(StrEnum):
    PENDING = 'PENDING'
    CASE_OPENED = 'CASE_OPENED'
    IN_PROGRESS = 'IN_PROGRESS'
    APPROVED = 'APPROVED'
    DENIED = 'DENIED'
    CASE_CLOSED = 'CASE_CLOSED'
    NOT_APPROVED = 'NOT_APPROVED'
    INVALID_REQUEST = 'INVALID_REQUEST'


CLOSED_QUOTA_REQUEST_STATUSES: frozenset[str] = frozenset((
    _KnownQuotaRequestStatus.APPROVED, _KnownQuotaRequestStatus.DENIED, _KnownQuotaRequestStatus.CASE_CLOSED,
    _KnownQuotaRequestStatus.NOT_APPROVED, _KnownQuotaRequestStatus.INVALID_REQUEST,
))


@dataclass(frozen=True)
class _UnknownQuotaRequestStatus:
    value: str


def _quota_request_status(value: str) -> _KnownQuotaRequestStatus | _UnknownQuotaRequestStatus:
    try:
        return _KnownQuotaRequestStatus(value)
    except ValueError:
        return _UnknownQuotaRequestStatus(value)


@dataclass(frozen=True)
class _QuotaRequest:
    created_at: float
    requested_vcpus: Decimal
    status: _KnownQuotaRequestStatus | _UnknownQuotaRequestStatus


def request_spot_quota(api: AwsCli, code: str, desired: int, label: str) -> bool:
    """Request one regional quota unless its value is sufficient or a request is open."""
    region = api.region
    try:
        quota = spot_quota(api, code)
        if quota >= desired:
            print(f'{region}: {label} quota {quota.normalize():f} vCPUs; no request is needed for {desired} vCPUs')
            return True
        history = api.call('list-requested-service-quota-change-history-by-quota',
            '--service-code', 'ec2', '--quota-code', code, service='service-quotas')
        opened = [row for row in ledger.rows(history, 'RequestedQuotas') if ledger.text(row, 'Status') not in CLOSED_QUOTA_REQUEST_STATUSES]
        if opened:
            for row in opened:
                status = ledger.text(row, 'Status')
                value = conf.decimal(str(row['DesiredValue']))
                print(f'{region}: open {label} quota request {value.normalize():f} vCPUs; status {status}')
            return True
        reply = api.call('request-service-quota-increase', '--service-code', 'ec2',
            '--quota-code', code, '--desired-value', str(desired), service='service-quotas')
        status = ledger.text(ledger.mapping(reply['RequestedQuota']), 'Status')
        print(f'{region}: {label} quota request {desired} vCPUs; status {status}')
        return status not in {'DENIED', 'NOT_APPROVED', 'INVALID_REQUEST'}
    except (admin_secrets.SecretsRefused, DeploymentRefused):
        raise
    except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
        match = re.search(r'An error occurred \(([A-Za-z][A-Za-z0-9]*)\) when calling ', str(error))
        reason = match[1] if match else type(error).__name__
        print(f'{region}: {label} quota request failed: {reason}')
        return False


@dataclass(frozen=True)
class RegionalNetwork:
    region: str
    vpc: str
    subnets: list[dict[str, object]]
    image: str


@dataclass(frozen=True)
class PreparedDeployment:
    at: int
    fingerprint: str
    parameters: dict[str, str]


@dataclass(frozen=True)
class HanaSpotCandidate:
    instance_type: str
    zone: str
    subnet: str
    usd_per_hour: Decimal

    @property
    def description(self) -> str:
        return f'{self.instance_type} in {self.zone}'


@dataclass(frozen=True)
class PendingHanaLaunch:
    instance_type: str
    zone: str
    subnet: str
    token: str

    def matches(self, candidate: HanaSpotCandidate) -> bool:
        return (self.instance_type, self.zone, self.subnet) == (candidate.instance_type, candidate.zone, candidate.subnet)

    @classmethod
    def decode(cls, value: object) -> PendingHanaLaunch:
        row = ledger.mapping(value)
        return cls(ledger.text(row, 'instance_type'), ledger.text(row, 'zone'),
                   ledger.text(row, 'subnet'), ledger.text(row, 'token'))

    def encode(self) -> dict[str, str]:
        return {'instance_type': self.instance_type, 'zone': self.zone, 'subnet': self.subnet, 'token': self.token}


def _save_hana_setup(path: Path, saved: dict[str, object]) -> None:
    write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')


def _save_hana_setup_image(path: Path, saved: dict[str, object], registered: hana_image.RegisteredImage) -> None:
    saved.update(image=registered.image, guest_digest=registered.digest.value)
    _save_hana_setup(path, saved)


def _hana_parameter_hash(parameters: dict[str, str]) -> str:
    return sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()


def _hana_setup_pending(saved: dict[str, object]) -> bool:
    return any(key in saved for key in ('pending_launch', 'pending_volume', 'stop_pending'))


def _hana_setup_ownership(saved: dict[str, object]) -> HanaBoxOwnership:
    version = saved.get('version')
    generation = saved.get('generation')
    resource_fields = ('instance', 'spot_request', 'data_volume', 'bucket')
    text_fields = (*resource_fields, 'deployment', 'region', 'stack', 'parameters_sha256', 'template_sha256')
    if (type(version) is int and version == 1 and saved.keys() - {'image', 'guest_digest'} == {*text_fields, 'version', 'requested', 'generation'}
            and ('image' not in saved or isinstance(saved['image'], str) and saved['image'])
            and saved['requested'] is True and type(generation) is int and generation >= 0
            and all(isinstance(saved[key], str) and saved[key] for key in text_fields)):
        return HanaBoxOwnership.STACK
    if (type(version) is not int or version != HANA_SETUP_VERSION) and any(
            key in saved for key in (*resource_fields, 'launch_template', 'security_group', 'pending_launch', 'pending_volume')):
        raise DeploymentRefused('The saved Hana setup uses stack-owned resources. ' + HANA_DOWN_FIRST)
    return HanaBoxOwnership.DIRECT


def _print_hana_placement(workload: conf.ConfigRead) -> None:
    enabled = workload.text('enabled')
    if enabled not in ('on', 'off'):
        raise ValueError('enabled must be on or off')
    state = 'enabled' if enabled == 'on' else 'disabled'
    print(f'Hana placement is {state}. This command does not change it.')


def _hana_lifecycle_idle(registration: dict[str, object]) -> bool:
    operation = registration.get('operation', {})
    observing = (isinstance(operation, dict)
                 and cast(dict[str, object], operation).get('action') == hana_box.Action.OBSERVE)
    return (registration.get('state') == hana_box.Lifecycle.IDLE_STOPPED and registration.get('claims') == []
            and registration.get('start', {}) is None and (operation is None or observing))


def _hana_wait_status(registration: dict[str, object], lock_busy: bool) -> str:
    state = registration.get('state')
    holder = 'unknown'
    claims = registration.get('claims')
    if claims == []:
        holder = 'none'
    elif isinstance(claims, list) and claims:
        claim = cast(list[object], claims)[0]
        if isinstance(claim, dict):
            holder = 'local process' if isinstance(cast(dict[str, object], claim).get('owner'), dict) else 'unrecorded'
    status = f'state {state if state in tuple(hana_box.Lifecycle) else "unavailable"}, claim holder {holder}'
    return status + (', controller lock busy' if lock_busy else '')


def deployment_parameters(path: Path, settings: DeploymentSettings,
                          notification: BudgetNotificationAddress, deployment_id: str) -> dict[str, str]:
    spending = read_settings(path)
    return {
        'VpcId': settings.vpc_id, 'NotificationEmail': notification.value, 'RootDeviceName': settings.root_device_name,
        'MonthlyLimitUsd': str(spending.monthly_limit_usd),
        'NoticeLowPercent': str(settings.notice_low), 'NoticeHighPercent': str(settings.notice_high),
        'StopPercent': str(settings.stop_percent), 'CostTagKey': spending.cost_explorer_tag_key,
        'CostTagValue': spending.cost_explorer_tag_value, 'ScalerUserName': settings.scaler_user_name,
        'DeploymentId': deployment_id,
    }


def fingerprint(settings: DeploymentSettings, parameters: dict[str, str], template: bytes) -> str:
    content = json.dumps({'region': settings.region, 'profile': settings.profile,
                          'stack': settings.stack_name, 'owner': settings.ami_owner_id,
                          'release': settings.nixos_release, 'parameters': parameters}, sort_keys=True)
    return sha256(template + content.encode()).hexdigest()


@final
class Deployment:
    def __init__(self, settings_path: Path, directory: Path, output: Path, *,
                 execute: AwsExecutor = execute_command, clock: Callable[[], float] = time.time,
                 pause: Callable[[float], None] = time.sleep,
                 notify: Callable[[ledger.LimitEvent], bool] = ledger.send_limit_event,
                 mode: plan_run.ApprovedSetup | plan_run.PlanRun = plan_run.APPROVED_SETUP) -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.output = output
        self.execute = execute
        self.clock = clock
        self.pause = pause
        self.notify = notify
        self.mode = mode
        self.admin: admin_secrets.AdminSecretSession = admin_secrets.AdminSecretSession(settings_path)

    def _deployment_id(self) -> str:
        settings = read_deployment_settings(self.settings_path)
        saved = deployment_record(self.directory)
        if saved.get('stack') and saved['stack'] != settings.stack_name:
            if not saved.get('replacement_deployment_id'):
                saved['replacement_deployment_id'] = uuid4().hex
                write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            return ledger.text(saved, 'replacement_deployment_id')
        if not saved.get('deployment_id'):
            saved.update(deployment_id=uuid4().hex)
            _ = saved.setdefault('stack', settings.stack_name)
            _ = saved.setdefault('region', settings.region)
            _ = saved.setdefault('outputs', {})
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
        return ledger.text(saved, 'deployment_id')

    def _parameters(self, settings: DeploymentSettings, notification: BudgetNotificationAddress) -> dict[str, str]:
        deployment_id = self._deployment_id()
        saved = deployment_record(self.directory)
        parameters = deployment_parameters(self.settings_path, settings, notification, deployment_id)
        parameters['BudgetName'] = str(saved.get('budget_name', '')) if saved.get('preserve_budget_name') else ''
        return parameters

    def _reserve_stack(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        api = self._api(settings)
        saved = deployment_record(self.directory)
        account = ledger.text(api.call('get-caller-identity', service='sts'), 'Account')
        if saved.get('account_id', account) != account or saved.get('region') != settings.region:
            raise DeploymentRefused('the deployment account or region differs')
        stacks: tuple[dict[str, object], ...]
        try:
            stacks = ledger.rows(api.call('describe-stacks', '--stack-name', settings.stack_name, service='cloudformation'), 'Stacks')
        except AwsUnavailable as error:
            if 'ValidationError' not in str(error) or 'does not exist' not in str(error):
                raise
            stacks = ()
        if stacks and stacks[0].get('StackStatus') != 'REVIEW_IN_PROGRESS':
            if saved.get('stack_id') != stacks[0].get('StackId'):
                raise DeploymentRefused('the stack lacks the saved immutable identity')
            return
        if stacks and saved.get('stack_id') and saved.get('stack_id') == stacks[0].get('StackId'):
            return
        if saved.get('stack') != settings.stack_name:
            for key in ('scaler_user', 'stack_id', 'budget_name', 'replacement_deployment_id', 'preserve_budget_name'):
                _ = saved.pop(key, None)
        saved.update(account_id=account, region=settings.region, stack=settings.stack_name,
                     deployment_id=parameters['DeploymentId'])
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
        change_name = 'identity-' + parameters['DeploymentId']
        try:
            reply = api.call('describe-change-set', '--stack-name', settings.stack_name,
                             '--change-set-name', change_name, service='cloudformation')
        except AwsUnavailable as error:
            if 'does not exist' not in str(error) and 'ChangeSetNotFound' not in str(error):
                raise
            reply = {}
        if reply:
            values = {ledger.text(row, 'ParameterKey'): row.get('ParameterValue') for row in ledger.rows(reply, 'Parameters')}
            stack_id = ledger.text(reply, 'StackId')
            if (values.get('DeploymentId') != parameters['DeploymentId'] or not stack_id.startswith(
                    f'arn:aws:cloudformation:{settings.region}:{account}:stack/{settings.stack_name}/')):
                raise DeploymentRefused('the reserved change set differs from the saved deployment identity')
        reserved = bool(reply)
        started = self.clock()
        try:
            if not reply:
                # A CREATE change set reserves the StackId before any resource can exist.
                reply = api.call('create-change-set', '--stack-name', settings.stack_name,
                                 '--change-set-name', change_name, '--change-set-type', 'CREATE',
                                 '--template-body', 'file://' + str(self.output / 'template.json'),
                                 '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps([
                                     {'ParameterKey': key, 'ParameterValue': value} for key, value in
                                     parameters.items()]), service='cloudformation')
                reserved = True
            saved['stack_id'] = ledger.text(reply, 'StackId')
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            _ = api.call('wait', 'change-set-create-complete', '--stack-name', ledger.text(saved, 'stack_id'),
                         '--change-set-name', change_name, service='cloudformation',
                         timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        except (AwsUnavailable, subprocess.SubprocessError) as error:
            stack_id = str(saved.get('stack_id') or settings.stack_name)
            try:
                reply = api.call('describe-change-set', '--stack-name', stack_id,
                                 '--change-set-name', change_name, service='cloudformation')
            except (AwsUnavailable, subprocess.SubprocessError):
                pass
            failure = self._stack_failure(api, stack_id, started, error, reply, parameters['NotificationEmail'])
            leftover = self._discard_change_set(api, stack_id, change_name) if reserved else ''
            raise AwsUnavailable(str(failure) + leftover) from None

    def _save_scaler_identity(self, settings: DeploymentSettings) -> None:
        saved = deployment_record(self.directory)
        api = self._api(settings)
        resource = ledger.mapping(api.call('describe-stack-resource', '--stack-name', ledger.text(saved, 'stack_id'),
                                           '--logical-resource-id', 'ScalerUser', service='cloudformation').get('StackResourceDetail'))
        name = ledger.text(resource, 'PhysicalResourceId')
        user = ledger.mapping(api.call('get-user', '--user-name', name, service='iam').get('User'))
        identity = {'name': name, 'arn': ledger.text(user, 'Arn'), 'user_id': ledger.text(user, 'UserId')}
        if saved.get('scaler_user', identity) != identity:
            raise DeploymentRefused('the scaler IAM identity differs from the deployment record')
        saved['scaler_user'] = identity
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    def _guard(self) -> ledger.SpendingAllowed:
        decision = ledger.spending_guard(self.settings_path, self.directory, 'deployment',
                                         clock=self.clock, notify=self.notify)
        if not isinstance(decision, ledger.SpendingAllowed):
            raise DeploymentRefused(decision.reason)
        if not ledger.CostLedger(self.directory).decision_is_current(decision):
            raise DeploymentRefused('the deployment guard has no durable confirmation')
        return decision

    def _api(self, settings: DeploymentSettings, *, single_attempt: bool = False) -> AwsCli:
        if isinstance(self.mode, plan_run.PlanRun):
            return self.mode.api(settings.region, settings.timeout_s)
        if single_attempt:
            return AwsCli(settings.profile, settings.region, settings.timeout_s,
                          self.admin.executor(self.execute, single_attempt=True))

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            offset = arguments.index('--region') + 2
            if tuple(arguments[offset:offset + 2]) in (('ec2', 'run-instances'), ('ec2', 'create-volume')):
                # Give each saved intent one EC2 attempt per call.
                env = dict(env or {}, AWS_MAX_ATTEMPTS='1')
            return self.execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)

        return AwsCli(settings.profile, settings.region, settings.timeout_s, self.admin.executor(execute))

    def _print_spot_quota(self, api: AwsCli, largest: int) -> None:
        """Show the regional gate quota without stopping deployment on a failed read."""
        try:
            quota = spot_quota(api)
        except (admin_secrets.SecretsRefused, DeploymentRefused):
            raise
        except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            print(f'{api.region}: Spot vCPU quota unknown: {self.admin.redact(str(error))}')
            return
        print(f'{api.region}: Spot quota {quota.normalize():f} vCPUs')
        if largest == 0:
            print(f'{api.region}: gate vCPU count is unknown')
        elif quota < largest:
            print(f'{api.region}: cannot run one gate ({largest} vCPUs); ask AWS for a Spot vCPU quota increase in this region.')


    def _load(self, notification: BudgetNotificationAddress
              ) -> tuple[DeploymentSettings, dict[str, str]]:
        settings = read_deployment_settings(self.settings_path)
        parameters = self._parameters(settings, notification)
        self._before_request(settings, parameters)
        return settings, parameters

    def _before_request(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        _ = self._guard()
        self._confirm_preparation(settings, parameters)

    def prepare_hana(self) -> PreparedDeployment:
        """Save local preparation without accessing AWS or changing placement."""
        admin_secrets.refuse_plain_admin(self.settings_path)
        with self._require_hana_control('prepare --workload hana'):
            self._refuse_hana_replacement()
            settings, parameters, template = self._hana_inputs()
            decision = self._guard()
            prepared = self._hana_output()._write_preparation(settings, parameters, template, decision)
            path = self.directory / 'hana' / 'setup.json'
            if not path.exists():
                _save_hana_setup(path, {'version': HANA_SETUP_VERSION, 'deployment': parameters['DeploymentId'],
                                       'stack': settings.stack_name, 'region': settings.region})
            return prepared

    @staticmethod
    def _print_hana_setup(workload: conf.ConfigRead, limits: hana_spend.PermitSettings) -> None:
        print(f'Hana plan: separate stack; {limits.instance_type}; persistent Spot; interruption stop')
        print(f'Hana plan: root {limits.root_gib} GiB; data {limits.data_gib} GiB; compute ceiling ${limits.spot_max_usd_per_hour}/hour')
        print('Hana resources: instance, retained data disk, launch template, security group, private control bucket, scaler policy')
        _print_hana_placement(workload)

    def hana_setup(self, *, approve_resources: bool = False) -> bool:
        """Deploy one retained Spot box and confirm its stopped state before registration."""
        workload = conf.read_or_repository('hana_aws')
        limits = hana_spend.read_settings(workload)
        if not approve_resources and isinstance(self.mode, plan_run.ApprovedSetup):
            self._print_hana_setup(workload, limits)
            return False
        if isinstance(self.mode, plan_run.PlanRun):
            return self._plan_hana_replacement(workload, limits)
        timing = HanaSetupTiming.read(workload)
        self._authorize(True)
        stop_timeout, stop_interval = timing.stop_wait_s, timing.stop_poll_s
        with self._hana_setup_lock(timing):
            if self._hana_root_replacement(workload, limits):
                return True
            self._refuse_hana_replacement()
            self._print_hana_setup(workload, limits)
            child = self._hana_output()
            path = self.directory / 'hana' / 'setup.json'
            saved = remote.read_record(path) if path.exists() else {}
            ownership = _hana_setup_ownership(saved)
            if saved.get('instance') and _hana_setup_pending(saved):
                registered_for_resume = self._require_current_hana_registration(workload)
                configured_image = workload.values('ami_id').get('ami_id', '')
                if configured_image and configured_image != registered_for_resume.image:
                    raise DeploymentRefused('The configured Hana image differs from the registration. Review the saved deployment.')
            if ownership is HanaBoxOwnership.STACK:
                return self._update_stack_owned_hana(path, saved, workload, limits)
            settings, parameters, template = self._hana_inputs(workload)
            if not saved:
                prepared = cast(object, json.loads((child.output / 'parameters.json').read_text()))
                prepared_parameters = {ledger.text(row, 'ParameterKey'): row.get('ParameterValue')
                                       for row in ledger.rows({'parameters': prepared}, 'parameters')}
                parameters['DeploymentId'] = ledger.text(prepared_parameters, 'DeploymentId')
            resume_stop = saved.get('requested') is True and saved.get('stop_pending') is True and bool(saved.get('instance'))
            child.confirm_preparation_evidence(settings, parameters, template, allow_expired=resume_stop)
            book = ledger.CostLedger(self.directory)
            spending = hana_spend.HanaSpending(book, self.settings_path, limits)
            outputs: dict[str, str] = {}
            if saved.get('requested'):
                saved_settings = replace(settings, region=ledger.text(saved, 'region'))
                outputs = child._stack(saved_settings, require_outputs=True)
                if 'Instance' in outputs or 'DataVolume' in outputs:
                    raise DeploymentRefused('The existing Hana stack owns the instance or data disk. ' + HANA_DOWN_FIRST)
                if outputs:
                    self._refuse_hana_stack_instance(self._api(saved_settings), saved_settings.stack_name)
                if not outputs:
                    if any(key in saved for key in ('instance', 'data_volume', 'bucket', 'pending_launch', 'pending_volume')):
                        raise DeploymentRefused('The Hana stack is missing and the setup record names resources or pending requests. '
                                                + 'Run down --workload hana --approve-destroy before setup.')
                    try:
                        saved_api = self._api(saved_settings)
                        started = self._hana_setup_started(book, saved)
                        kept = retained.deleted_since(saved_api, saved_settings.stack_name, started)
                        if kept is None:
                            raise AwsUnavailable('No deleted stack matches the saved setup.')
                        _ = spot_end.end_boxes(saved_api, ('HanaDeployment', ledger.text(saved, 'deployment')))
                        retained.delete(saved_api, kept, wait_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
                        self._clear_hana_setup(spending, path, saved)
                    except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError, spot_end.SpotEndIncomplete) as error:
                        raise DeploymentRefused(f'The saved Hana setup remains. {error}\n'
                                                + 'Run down --workload hana --approve-destroy before setup.') from None
                    print('hana-setup: cleared the saved setup for the missing stack')
                    saved = {}
            template_hash = sha256(template).hexdigest()
            parameter_hash = _hana_parameter_hash(parameters)
            stack_parameters = {key: value for key, value in parameters.items()
                                if key not in ('InstanceType', 'AvailabilityZone', 'SubnetId', 'DataGiB',
                                               'FallbackInstanceTypes')}
            if 'generation' in saved and saved.get('template_sha256') != template_hash:
                if saved.get('region') != settings.region or saved.get('parameters_sha256') != parameter_hash:
                    raise DeploymentRefused('The approved Hana parameters or region changed. Review the saved deployment.')
                if not outputs:
                    raise DeploymentRefused('The approved Hana template changed; review the saved deployment')
                for key in ('instance', 'data_volume', 'bucket', 'launch_template', 'security_group'):
                    if not saved.get(key):
                        raise DeploymentRefused(f'The saved Hana setup lacks {key}. Complete approved setup first.')
                output_names = ('ControlBucket', 'LaunchTemplate', 'LaunchTemplateVersion', 'SecurityGroup')
                launch = f"LaunchTemplateId={outputs.get('LaunchTemplate')},Version={outputs.get('LaunchTemplateVersion')}"
                if (not all(outputs.get(key) for key in output_names)
                        or outputs['ControlBucket'] != saved['bucket'] or launch != saved['launch_template']
                        or outputs['SecurityGroup'] != saved['security_group']):
                    raise DeploymentRefused('Hana resource identities changed. Review the saved deployment.')
                resources = {key: outputs[key] for key in output_names}
                api = self._api(settings)
                identity = self._idle_hana_policy_identity(api, path, saved)
                provider = self._hana_provider()
                print('hana-setup probe: before the policy update')
                _ = self._probe_hana_scaler(provider, identity)
                with step('hana-setup', 'updating the Hana IAM policy', self.settings_path):
                    self._hana_stack_change(api, settings, child.output / 'template.json',
                                            stack_parameters, keep='', scope=HanaPolicyUpdate(resources))
                saved['template_sha256'] = template_hash
                write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')
                print('Hana policy update complete')
                print('hana-setup probe: after the policy update')
                return self._probe_hana_scaler(provider, identity)
            _ = self._guard()
            api = self._api(settings)
            generation = ledger.instant(saved, 'generation') if 'generation' in saved else 0
            update_ceiling = False
            if 'generation' in saved:
                if saved.get('region') != settings.region:
                    raise DeploymentRefused('The approved Hana region changed. Review the saved deployment.')
                if saved.get('parameters_sha256') != parameter_hash:
                    if (not outputs or saved.get('version') != HANA_SETUP_VERSION
                            or any(key in saved for key in ('instance', 'pending_launch', 'data_volume', 'pending_volume'))):
                        raise DeploymentRefused('The approved Hana parameters changed. Review the saved deployment.')
                    previous_ceiling = str(saved.get('spot_max_price', '')) or next(
                        (str(row.usd_per_hour) for row in book.read_snapshot().hana_compute if row.generation == generation), '')
                    if (not previous_ceiling or saved.get('parameters_sha256')
                            != _hana_parameter_hash(parameters | {'SpotMaxPrice': previous_ceiling})):
                        raise DeploymentRefused('Only the Spot ceiling can change on the saved Hana stack.')
                    saved['spot_max_price'] = previous_ceiling
                    update_ceiling = True
            with step('hana-setup', 'checking the image and subnet', self.settings_path):
                self._validate_inputs(settings)
                candidates = (() if saved.get('instance') else self._hana_spot_candidates(api, settings, limits, workload))
            launch_deadline = 0
            if not saved.get('instance'):
                if update_ceiling:
                    spending.close_setup(int(self.clock()), generation)
                exposure = self._reserve_hana_setup(spending, saved, generation)
                generation, launch_deadline = exposure.generation, exposure.expires
                saved.update(version=HANA_SETUP_VERSION, deployment=parameters['DeploymentId'], stack=settings.stack_name,
                             region=settings.region, generation=generation, template_sha256=template_hash)
                if not update_ceiling:
                    saved.update(parameters_sha256=parameter_hash, spot_max_price=parameters['SpotMaxPrice'])
                _save_hana_setup(path, saved)
            if not outputs or update_ceiling:
                if saved.get('instance'):
                    raise DeploymentRefused('The saved Hana instance has no stack outputs. ' + HANA_DOWN_FIRST)
                saved['requested'] = True
                _save_hana_setup(path, saved)
                with step('hana-setup', 'updating the separate Hana stack', self.settings_path):
                    try:
                        self._hana_stack_change(api, settings, child.output / 'template.json', stack_parameters, keep='')
                    except HanaCreateRolledBack as error:
                        try:
                            self._clear_hana_setup(spending, path, saved)
                        except (OSError, ValueError) as cleanup_error:
                            raise AwsUnavailable(f'{error}\nThe saved Hana setup remains: {cleanup_error}') from None
                        raise
                outputs = child._stack(settings)
            if not all(outputs.get(key) for key in ('ControlBucket', 'LaunchTemplate', 'LaunchTemplateVersion', 'SecurityGroup')):
                raise DeploymentRefused('The Hana stack did not report its launch resources.')
            bucket = outputs['ControlBucket']
            launch = 'LaunchTemplateId=' + outputs['LaunchTemplate'] + ',Version=' + outputs['LaunchTemplateVersion']
            group = outputs['SecurityGroup']
            identities = {'bucket': bucket, 'launch_template': launch, 'security_group': group}
            expected = {key: str(saved.get(key, value)) for key, value in identities.items()}
            if update_ceiling and expected['launch_template'].split(',Version=', 1)[0] == launch.split(',Version=', 1)[0]:
                expected['launch_template'] = launch
            if identities != expected:
                raise DeploymentRefused('Hana stack outputs changed. Review replacement before setup.')
            saved.update(identities, parameters_sha256=parameter_hash, spot_max_price=parameters['SpotMaxPrice'])
            _save_hana_setup(path, saved)
            if not saved.get('instance'):
                self._launch_hana_instance(api, path, saved, candidates, spending, launch_deadline)
            instance = ledger.text(saved, 'instance')
            spending.record_setup_instance_type(generation, ledger.text(saved, 'instance_type'))
            stack_timeout = conf.read(self.settings_path).whole('stack_wait_timeout_s')
            _ = api.call('wait', 'instance-exists', '--instance-ids', instance, timeout_s=stack_timeout)
            reply = api.call('describe-instances', '--instance-ids', instance)
            instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
            if len(instances) != 1 or instances[0].get('InstanceId') != instance:
                raise DeploymentRefused('Hana instance confirmation differs')
            registered = hana_image.saved(self.directory)
            if isinstance(registered, hana_image.RegisteredImage) and registered.image == settings.ami_id:
                if instances[0].get('ImageId') != registered.image:
                    raise DeploymentRefused('Setup could not confirm the Hana instance image. Review the saved deployment.')
                saved['guest_digest'] = registered.digest.value
            request = ledger.text(instances[0], 'SpotInstanceRequestId')
            if saved.get('spot_request', request) != request:
                raise DeploymentRefused('The Hana Spot request differs from the saved request.')
            saved['spot_request'] = request
            try:
                requests = ledger.rows(api.call('describe-spot-instance-requests', '--spot-instance-request-ids', request),
                                       'SpotInstanceRequests')
            except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                raise AwsUnavailable(_operation_failure('describe-spot-instance-requests', error)) from None
            if (len(requests) != 1 or requests[0].get('InstanceId') != instance
                    or requests[0].get('Type') != 'persistent'
                    or requests[0].get('InstanceInterruptionBehavior') != 'stop'):
                raise DeploymentRefused('Hana requires a persistent Spot request with stop behavior')
            if 'pending_launch' in saved:
                del saved['pending_launch']
            _save_hana_setup(path, saved)
            self._attach_hana_data(api, path, saved, limits.data_gib, stack_timeout, stop_interval)
            saved['stop_pending'] = True
            _save_hana_setup(path, saved)
            with step('hana-setup', 'confirming the idle stop', self.settings_path):
                observed = api.observe(instance, int(self.clock()))
                if observed.state not in ('stopped', 'stopping'):
                    _ = api.call('stop-instances', '--instance-ids', instance)
                for started in self._hana_check_times(stop_timeout, stop_interval):
                    confirmed = api.observe(instance, int(self.clock()))
                    elapsed = int(time.monotonic() - started)
                    print(f'waiting for the Hana box to stop: {confirmed.state}, {elapsed} s of {stop_timeout} s',
                          flush=True)
                    if confirmed.state == 'stopped':
                        break
                else:
                    raise DeploymentRefused('The Hana box is not yet stopped. '
                                            + 'Run deploy.py hana-setup --approve-resources again to resume setup.')
                spending.reconcile(int(self.clock()), 'stopped', generation=ledger.instant(saved, 'generation'))
                del saved['stop_pending']
                _save_hana_setup(path, saved)
            provider = self._hana_provider()
            identity = hana_box.DeploymentIdentity(parameters['DeploymentId'], instance, request, settings.region)
            hana_box.HanaController(self.directory, self.settings_path, conf.path('hana_aws'), provider,
                clock=self.clock).register(identity)
            print('Hana setup complete. The box is stopped.')
            _print_hana_placement(workload)
            return self._probe_hana_scaler(provider, identity)

    @contextmanager
    def _require_account_stack(self, command: str) -> Generator[None]:
        with account_stack_lock(self.directory) as access:
            if access is LockResult.BUSY:
                raise DeploymentRefused(f'{command}: Another command is using the AWS account stack. '
                                        + 'Run the same command again when that command ends.')
            yield

    @contextmanager
    def _require_hana_control(self, command: str) -> Generator[None]:
        with hana_command_lock(self.directory) as access:
            if access is not HanaCommandAccess.ACQUIRED:
                raise HanaControlRefused(command)
            yield

    @contextmanager
    def _hana_setup_lock(self, timing: HanaSetupTiming) -> Generator[None]:
        lifecycle = self.directory / 'hana' / 'lifecycle.json'
        status = 'state unavailable, claim holder unknown'
        waiting = False

        def is_idle() -> bool:
            nonlocal status
            if hana_replacement_record.held(self.directory):
                return True
            try:
                registration = remote.read_record(lifecycle)
            except FileNotFoundError:
                return True
            status = _hana_wait_status(registration, False)
            return _hana_lifecycle_idle(registration)

        timeout = timing.idle_wait_s
        for started in self._hana_check_times(timeout, timing.heartbeat_s, check_at_deadline=True):
            with hana_command_lock(self.directory, ready=is_idle, wait_for_controller=False) as access:
                if access is HanaCommandAccess.ACQUIRED:
                    if waiting:
                        print('hana-setup: Hana is idle-stopped. Setup holds the box.', flush=True)
                    yield
                    return
                if access is HanaCommandAccess.RESOURCES_HELD:
                    raise HanaControlRefused('hana-setup')
                if access is HanaCommandAccess.CONTROLLER_BUSY:
                    try:
                        registration = remote.read_record(lifecycle)
                    except FileNotFoundError:
                        registration = {}
                    status = _hana_wait_status(registration, True)
            if not waiting:
                print(f'hana-setup: waiting up to {timeout} s for Hana to become idle ({status}).', flush=True)
                waiting = True
            else:
                elapsed = int(time.monotonic() - started)
                print(f'hana-setup: still waiting for Hana ({status}, {elapsed} s of {timeout} s).', flush=True)
        raise DeploymentRefused(f'The Hana lifecycle is still busy after {timeout} s ({status}). '
                                + 'Run hana-setup again when the box is idle.')

    def _plan_hana_replacement(self, workload: conf.ConfigRead, limits: hana_spend.PermitSettings) -> bool:
        with hana_command_lock(self.directory, wait_for_controller=False, existing_controller=True) as access:
            if access is HanaCommandAccess.RESOURCES_HELD:
                raise HanaControlRefused('hana-setup --plan')
            if access is HanaCommandAccess.CONTROLLER_BUSY:
                raise DeploymentRefused('The Hana controller holds the lock. '
                                        + 'Run the same command again when the controller releases it.')
            if access is HanaCommandAccess.CONTROLLER_MISSING:
                if not (self.directory / 'hana' / 'setup.json').is_file():
                    raise plan_run.PlanRefused('Hana has no saved setup. Run deploy.py prepare --workload hana.')
                raise DeploymentRefused('The Hana controller lock is missing. Review the saved deployment.')
            return self._hana_root_replacement(workload, limits)

    def _refuse_hana_replacement(self) -> None:
        if hana_replacement_record.held(self.directory):
            raise DeploymentRefused(hana_replacement_record.HELD)

    def _require_current_hana_registration(self, configuration: conf.ConfigRead) -> hana_image.RegisteredImage:
        registered = hana_image.saved(self.directory)
        if not isinstance(registered, hana_image.RegisteredImage):
            raise DeploymentRefused('Hana has no verified image registration. ' + hana_replacement.REGISTER_IMAGE)
        if not hana_image.guest_digest_is_current(registered.digest, configuration):
            raise DeploymentRefused('The registered Hana image is older than the current guest files. '
                                    + 'Build the Hana image from the current input. ' + hana_replacement.REGISTER_IMAGE)
        return registered

    def _hana_root_replacement(self, workload: conf.ConfigRead, limits: hana_spend.PermitSettings) -> bool:
        """Check the registered image and converge on the same box."""
        path = self.directory / 'hana' / 'setup.json'
        plan = self.mode
        if not path.is_file():
            if isinstance(plan, plan_run.PlanRun):
                raise plan_run.PlanRefused('Hana has no saved setup. Run deploy.py prepare --workload hana.')
            return False
        try:
            attempt = hana_replacement_record.read(self.directory)
            saved = remote.read_record(path)
            has_instance = bool(saved.get('instance'))
            pending = _hana_setup_pending(saved)
            if not has_instance or (pending and isinstance(plan, plan_run.ApprovedSetup)):
                if isinstance(plan, plan_run.PlanRun):
                    raise plan_run.unsupported('Hana setup has pending instance or volume work')
                if isinstance(attempt, hana_replacement_record.NoReplacement):
                    return False
                raise DeploymentRefused('The saved Hana setup changed during replacement.')
            if isinstance(plan, plan_run.PlanRun):
                self._print_hana_image_status(saved, workload)
            registered = self._require_current_hana_registration(workload)
            if pending:
                raise plan_run.unsupported('Hana setup has pending instance or volume work')
            if isinstance(attempt, hana_replacement_record.ReplacementAttempt) and (
                    attempt.image != registered.image or attempt.guest_digest != registered.digest.value):
                raise DeploymentRefused('The Hana replacement inputs differ. Restore the approved registration.')
            ownership = _hana_setup_ownership(saved)
            box = hana_replacement.BoxIdentity.read(saved)
            identity = hana_box.DeploymentIdentity(box.deployment, box.instance, box.spot_request, box.region)
            account = read_deployment_settings(self.settings_path)
            stack = workload.text('stack_name')
            if stack == account.stack_name or saved.get('stack') != stack or box.region != limits.region:
                raise DeploymentRefused('The Hana stack or region differs. Review the saved deployment.')
            timeout = workload.whole('aws_timeout_s')
            settings = replace(account, stack_name=stack, region=box.region, timeout_s=timeout)
            api = self._api(settings)
            if isinstance(plan, plan_run.PlanRun):
                self._check_credentials()
                plan.confirm_account(limits.region, timeout)
            if isinstance(attempt, hana_replacement_record.NoReplacement):
                self._require_idle_hana_lifecycle(path, identity)
            root = hana_replacement.RootReplacement(self.directory, api, box)
            preflight = root.preflight(registered, saved, limits.root_gib, attempt)
            if isinstance(attempt, hana_replacement_record.NoReplacement):
                if preflight.current:
                    if isinstance(plan, plan_run.ApprovedSetup):
                        if saved.get('image') != registered.image or saved.get('guest_digest') != registered.digest.value:
                            _save_hana_setup_image(path, saved, registered)
                        self._print_hana_image_status(saved, workload)
                    return True
                view = self._hana_policy_stack(api, settings, saved, ownership)
                policy = hana_replacement.scaler_policy(
                    self._hana_scaler_policy(), identity.instance, registered.image, preflight.image.snapshot)
                if isinstance(plan, plan_run.PlanRun):
                    self._preview_hana_policy(api, view, policy, self._hana_provider(), identity)
                else:
                    _ = self._install_hana_policy(api, view, policy)
                    attempt = hana_replacement.new_attempt(self.directory, registered, preflight)
            if isinstance(plan, plan_run.PlanRun):
                hana_replacement.print_plan(attempt)
                return True

            def publish(proof: hana_replacement_record.ReplacementAttempt, stopped: bool) -> None:
                current = remote.read_record(path)
                if hana_replacement.BoxIdentity.read(current) != box:
                    raise DeploymentRefused('The Hana setup changed during replacement. ' + hana_replacement.RESUME)
                _save_hana_setup_image(path, current, registered)
                hana_box.record_replacement_stop(path.with_name('lifecycle.json'), proof.generation,
                                                 int(self.clock()), stopped=stopped)

            assert isinstance(attempt, hana_replacement_record.ReplacementAttempt)
            spending = hana_spend.HanaSpending(ledger.CostLedger(self.directory), self.settings_path, limits)
            run_timeout = min(workload.whole('stop_wait_timeout_s'), limits.permit_max_seconds)
            checks = self._hana_check_times(run_timeout, workload.whole('stop_poll_seconds'))
            root = hana_replacement.RootReplacement(self.directory, self._hana_provider().api.in_region(box.region), box)
            root.apply(attempt, spending, self.clock, run_timeout, checks, publish)
            return True
        except (hana_image.ImageRefused, hana_replacement.ReplacementRefused, plan_run.PlanRefused,
                plan_identity.PlanIdentityRefused, DeploymentRefused):
            raise
        except (OSError, ValueError, KeyError, TypeError, AwsUnavailable, subprocess.SubprocessError) as error:
            code = aws_error_code(error) if isinstance(error, AwsUnavailable) else ''
            detail = f' ({code})' if code else ''
            raise DeploymentRefused('Could not verify Hana root replacement' + detail + '. ' + hana_replacement.RESUME) from None

    @staticmethod
    def _print_hana_image_status(saved: dict[str, object], configuration: conf.ConfigRead) -> None:
        current = hana_image.setup_image_is_current(saved, configuration)
        print('Hana image is current.' if current else hana_box.HANA_IMAGE_STALE)

    @staticmethod
    def _hana_scaler_policy() -> dict[str, object]:
        current = ledger.mapping(cast(object, json.loads(HANA_TEMPLATE.read_bytes())))
        return ledger.mapping(ledger.mapping(current['Resources'])['ScalerPolicy'])

    def _hana_policy_stack(self, api: AwsCli, settings: DeploymentSettings, saved: dict[str, object],
                           ownership: HanaBoxOwnership, *, check_idle: bool = False) -> HanaPolicyStack:
        stacks = ledger.rows(api.call('describe-stacks', '--stack-name', settings.stack_name, service='cloudformation'), 'Stacks')
        if len(stacks) != 1:
            raise DeploymentRefused('Expected one Hana stack for the policy update.')
        stack = stacks[0]
        outputs = self._stack_outputs(stack, require_outputs=True)
        if ownership == HanaBoxOwnership.STACK:
            fields = {'Instance': 'instance', 'DataVolume': 'data_volume', 'ControlBucket': 'bucket'}
            matches = all(outputs.get(output) == saved[key] for output, key in fields.items())
        else:
            launch = f"LaunchTemplateId={outputs.get('LaunchTemplate')},Version={outputs.get('LaunchTemplateVersion')}"
            matches = (outputs.get('ControlBucket') == saved.get('bucket') and launch == saved.get('launch_template')
                       and outputs.get('SecurityGroup') == saved.get('security_group')
                       and 'Instance' not in outputs and 'DataVolume' not in outputs)
        if not matches or not outputs.get('LaunchTemplate'):
            raise DeploymentRefused('Hana resource identities changed. Review the saved deployment.')
        resource_names = (('Instance', 'DataVolume', 'ControlBucket', 'LaunchTemplate')
                          if ownership == HanaBoxOwnership.STACK else
                          ('ControlBucket', 'LaunchTemplate', 'LaunchTemplateVersion', 'SecurityGroup'))
        resources = {key: outputs[key] for key in resource_names}
        parameter_rows = ledger.rows(stack, 'Parameters')
        parameters = {ledger.text(row, 'ParameterKey'): ledger.text(row, 'ParameterValue') for row in parameter_rows}
        if len(parameters) != len(parameter_rows) or parameters.get('DeploymentId') != saved.get('deployment'):
            raise DeploymentRefused('The live Hana stack parameters differ from the saved deployment.')
        if parameters.get('ScalerUser') != settings.scaler_user_name:
            raise DeploymentRefused('The live Hana stack scaler user differs from the approved configuration.')
        if check_idle:
            identity = self._idle_hana_policy_identity(api, self.directory / 'hana' / 'setup.json', saved)
        else:
            identity = hana_box.DeploymentIdentity(*(ledger.text(saved, key) for key in
                                                     ('deployment', 'instance', 'spot_request', 'region')))
        stack_id = ledger.text(stack, 'StackId')
        reply = api.call('get-template', '--stack-name', stack_id, '--template-stage', 'Original', service='cloudformation')
        body = reply.get('TemplateBody')
        template = ledger.mapping(cast(object, json.loads(body)) if isinstance(body, str) else body)
        live = ledger.mapping(template['Resources'])
        resource_types = {'ScalerPolicy': 'AWS::IAM::ManagedPolicy'}
        if ownership == HanaBoxOwnership.STACK:
            resource_types.update(Instance='AWS::EC2::Instance', DataVolume='AWS::EC2::Volume',
                                  DataAttachment='AWS::EC2::VolumeAttachment')
        elif any(key in live for key in ('Instance', 'DataVolume', 'DataAttachment')):
            raise DeploymentRefused('The live template owns Hana resources that the setup marks as direct-owned.')
        if (ledger.mapping(template['Parameters']).keys() != parameters.keys()
                or any(ledger.mapping(live.get(key, {})).get('Type') != kind for key, kind in resource_types.items())):
            raise DeploymentRefused('The live template does not match the saved Hana setup.')
        return HanaPolicyStack(replace(settings, stack_name=stack_id), outputs, parameters, template,
                               HanaPolicyUpdate(resources, ownership, only_scaler=not check_idle), identity)

    def _install_hana_policy(self, api: AwsCli, view: HanaPolicyStack, policy: dict[str, object]) -> bytes:
        live = ledger.mapping(view.template['Resources'])
        needs_update = live['ScalerPolicy'] != policy
        live['ScalerPolicy'] = policy
        content = json.dumps(view.template, sort_keys=True).encode()
        if needs_update:
            with tempfile.TemporaryDirectory(prefix='hana-policy-') as folder:
                change_template = Path(folder) / 'template.json'
                _ = change_template.write_bytes(content)
                with step('hana-setup', 'updating the Hana IAM policy', self.settings_path):
                    self._hana_stack_change(api, view.settings, change_template, view.parameters, keep='', scope=view.scope)
        return content

    def _preview_hana_policy(self, api: AwsCli, view: HanaPolicyStack, policy: dict[str, object],
                             provider: AwsHanaProvider, identity: hana_box.DeploymentIdentity) -> None:
        plan = self.mode
        assert isinstance(plan, plan_run.PlanRun)
        bucket = view.outputs['ControlBucket']
        scaler = plan.scaler(provider.api.in_region(identity.region), view.settings.scaler_user_name)
        replacement = plan.scaler_policy(api, view.settings.stack_name, policy, view.parameters, bucket)
        bucket_policy = plan.bucket_policy(identity.region, view.template, view.parameters, bucket)
        plan.probe_scaler(api, scaler, replacement, identity.instance, identity.deployment, bucket, bucket_policy)
        plan.probe_hana_replacement(api, scaler, replacement)
        if ledger.mapping(view.template['Resources'])['ScalerPolicy'] == policy:
            print('The Hana scaler policy is current.')
        else:
            print('plan IAM: Modify ScalerPolicy AWS::IAM::ManagedPolicy replacement=False', flush=True)

    def _update_stack_owned_hana(self, path: Path, saved: dict[str, object], workload: conf.ConfigRead,
                                 limits: hana_spend.PermitSettings) -> bool:
        """Update the scaler policy for a stopped stack-owned box."""
        account = read_deployment_settings(self.settings_path)
        stack_name = workload.text('stack_name')
        if stack_name == account.stack_name or saved.get('stack') != stack_name or saved.get('region') != limits.region:
            raise DeploymentRefused('The saved Hana stack or region differs from the workload.')
        settings = replace(account, stack_name=stack_name, region=limits.region, timeout_s=workload.whole('aws_timeout_s'))
        api = self._api(settings)
        view = self._hana_policy_stack(api, settings, saved, HanaBoxOwnership.STACK, check_idle=True)
        identity = view.identity
        policy = self._hana_scaler_policy()
        needs_update = ledger.mapping(view.template['Resources'])['ScalerPolicy'] != policy
        provider = self._hana_provider()
        if needs_update:
            print('hana-setup probe: before the policy update')
            _ = self._probe_hana_scaler(provider, identity)
        else:
            print('The Hana scaler policy is current.')
        content = self._install_hana_policy(api, view, policy)
        template_hash = sha256(content).hexdigest()
        if saved.get('template_sha256') != template_hash:
            saved['template_sha256'] = template_hash
            _save_hana_setup(path, saved)
        if needs_update:
            print('hana-setup probe: after the policy update')
        return self._probe_hana_scaler(provider, identity)

    @staticmethod
    def _require_idle_hana_lifecycle(path: Path, identity: hana_box.DeploymentIdentity) -> None:
        try:
            hana_box.require_idle_setup(path.with_name('lifecycle.json'), identity)
        except ValueError as error:
            raise DeploymentRefused(str(error)) from None

    def _idle_hana_policy_identity(self, api: AwsCli, path: Path, saved: dict[str, object]) -> hana_box.DeploymentIdentity:
        box = hana_replacement.BoxIdentity.read(saved)
        identity = hana_box.DeploymentIdentity(box.deployment, box.instance, box.spot_request, box.region)
        self._require_idle_hana_lifecycle(path, identity)
        root = hana_replacement.RootReplacement(self.directory, api, box)
        try:
            _ = root.require_idle(root.instance())
        except hana_replacement.ReplacementRefused as error:
            raise DeploymentRefused(str(error)) from None
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            raise DeploymentRefused('Setup could not confirm the Hana instance is idle. Run hana-setup again when the box is idle.') from None
        return identity

    def _hana_provider(self) -> AwsHanaProvider:
        return AwsHanaProvider.from_settings(conf.path('aws_target'), self.directory, conf.path('hana_aws'),
            execute=self.execute, credentials_file=admin_secrets.scaler_service_credentials_file)

    @staticmethod
    def _probe_hana_scaler(provider: AwsHanaProvider, identity: hana_box.DeploymentIdentity) -> bool:
        attribute = ('--instance-id', identity.instance, '--attribute', 'userData')
        user_data = ''

        def read_user_data(api: AwsCli) -> None:
            nonlocal user_data
            reply = api.call('describe-instance-attribute', *attribute)
            value = ledger.mapping(reply.get('UserData', {})).get('Value', '')
            if not isinstance(value, str):
                raise ValueError('Invalid user data value')
            user_data = value

        def check_user_data_write(api: AwsCli) -> None:
            with tempfile.TemporaryDirectory(prefix='hana-probe-') as folder:
                # Use an empty value for the dry run if the read failed.
                data = Path(folder) / 'user-data'
                _ = data.write_text(user_data)
                api.ec2_dry_run('modify-instance-attribute', *attribute, '--value', f'file://{data}')

        key = f'control/{identity.deployment}/setup-probe.json'
        content = b'{"probe": "hana-setup"}'
        return not run_scaler_probes('hana-setup probe', provider.api.in_region(identity.region), [
            ScalerProbe('export-credentials', lambda _api: provider.signing_key()),
            ScalerProbe('describe-instances', lambda api: api.call('describe-instances', '--instance-ids', identity.instance)),
            ScalerProbe('describe-spot-instance-requests', lambda api: api.call('describe-spot-instance-requests',
                '--spot-instance-request-ids', identity.spot_request)),
            ScalerProbe('describe-instance-attribute', read_user_data),
            ScalerProbe('modify-instance-attribute', check_user_data_write),
            ScalerProbe('start-instances', lambda api: api.ec2_dry_run('start-instances', '--instance-ids', identity.instance)),
            ScalerProbe('stop-instances', lambda api: api.ec2_dry_run('stop-instances', '--instance-ids', identity.instance)),
            ScalerProbe('put-object', lambda api: put_probe_object(api, provider.bucket, key, content)),
            ScalerProbe('get-object', lambda api: get_probe_object(api, provider.bucket, key, content)),
        ])

    def _hana_spot_candidates(self, api: AwsCli, settings: DeploymentSettings,
                              limits: hana_spend.PermitSettings, workload: conf.ConfigRead) -> tuple[HanaSpotCandidate, ...]:
        try:
            margin = conf.decimal(workload.text('spot_ceiling_margin', recover_invalid=False))
        except ValueError as error:
            raise DeploymentRefused(f'Hana setup requires a positive spot_ceiling_margin. {error}') from error
        if margin <= 0:
            raise DeploymentRefused('Hana setup requires a positive spot_ceiling_margin.')
        sizes = (limits.instance_type, *limits.fallback_instance_types)
        primary = settings.availability_zone
        subnets = {primary: settings.subnet_id}
        reply = api.call('describe-subnets', '--filters', 'Name=vpc-id,Values=' + settings.vpc_id)
        for row in sorted(ledger.rows(reply, 'Subnets'), key=lambda subnet: ledger.text(subnet, 'SubnetId')):
            zone = ledger.text(row, 'AvailabilityZone')
            if (zone != primary and zone.startswith(api.region) and row.get('VpcId') == settings.vpc_id
                    and row.get('State') == 'available' and row.get('DefaultForAz') is True):
                _ = subnets.setdefault(zone, ledger.text(row, 'SubnetId'))
        try:
            rows = fetch_spot_prices(api, sizes, int(self.clock()))
        except (AwsUnavailable, OSError, ValueError, subprocess.SubprocessError) as error:
            raise DeploymentRefused(f'Could not read Spot prices for Hana instance types in {api.region}.') from error
        prices = {(row.instance_type, row.zone): row.usd_per_hour for row in rows}
        ceiling = limits.spot_max_usd_per_hour
        available: list[HanaSpotCandidate] = []
        above_ceiling: list[HanaSpotCandidate] = []
        for size in sizes:
            for zone, subnet in subnets.items():
                if (size, zone) not in prices:
                    print(f'hana-setup: skip {size} in {zone}. AWS lists no Spot price.')
                    continue
                candidate = HanaSpotCandidate(size, zone, subnet, prices[size, zone])
                if candidate.usd_per_hour > ceiling:
                    print(f'hana-setup: skip {candidate.description}. '
                          + f'{candidate.usd_per_hour:f} USD/h exceeds the {ceiling:f} USD/h ceiling.')
                    above_ceiling.append(candidate)
                else:
                    available.append(candidate)
        if available:
            return tuple(sorted(available, key=lambda candidate: (
                (candidate.instance_type, candidate.zone) != (limits.instance_type, primary),
                candidate.usd_per_hour)))
        if above_ceiling:
            cheapest = min(above_ceiling, key=lambda candidate: candidate.usd_per_hour)
            suggested = format((cheapest.usd_per_hour * (1 + margin)).quantize(
                Decimal('0.01'), rounding=ROUND_CEILING), 'f')
            ceiling_key = workload.effective_key('spot_max_usd_per_hour')
            details = (f'No Hana Spot candidate meets the {ceiling:f} USD/h ceiling.\n'
                + f'The cheapest candidate is {cheapest.description} at {cheapest.usd_per_hour:f} USD/h.\n')
            try:
                _ = conf.editable_line('hana_aws', ceiling_key)
                conf.check('hana_aws', ceiling_key, suggested)
            except conf.Unusable as error:
                raise DeploymentRefused(f'{details}The suggested ceiling is {suggested} USD/hour.\n{error}') from error
            raise DeploymentRefused(details
                + f'1. Run /tune hana_aws {ceiling_key} {suggested}\n'
                + '2. Run deploy.py prepare --workload hana\n'
                + '3. Run deploy.py hana-setup --approve-resources')
        raise DeploymentRefused(f'AWS lists no Spot prices for Hana instance types in the eligible subnets of {api.region}.')

    def _reserve_hana_setup(self, spending: hana_spend.HanaSpending[hana_spend.PermitSettings],
                            saved: dict[str, object], generation: int) -> ledger.HanaComputeExposure:
        now = int(self.clock())
        for row in spending.book.read_snapshot().hana_compute:
            if row.generation == generation and isinstance(row.state, ledger.HanaExposureOpen):
                if now < row.expires:
                    return row
                if not saved.get('pending_launch'):
                    spending.close_setup(now, generation)
                break
        if saved.get('pending_launch'):
            raise DeploymentRefused('The pending Hana launch has no active reservation. ' + HANA_DOWN_FIRST)
        reservation = spending.reserve_setup(now, now + spending.settings.permit_max_seconds)
        if isinstance(reservation, hana_spend.PermitRefused):
            raise DeploymentRefused(reservation.reason)
        return reservation.exposure

    def _launch_hana_instance(self, api: AwsCli, path: Path, saved: dict[str, object],
                              candidates: tuple[HanaSpotCandidate, ...],
                              spending: hana_spend.HanaSpending[hana_spend.PermitSettings], deadline: int) -> None:
        if 'pending_launch' in saved:
            pending = PendingHanaLaunch.decode(saved['pending_launch'])
            first = next((candidate for candidate in candidates if pending.matches(candidate)), None)
            if first is None:
                raise DeploymentRefused('The pending Hana launch is no longer an eligible candidate. ' + HANA_DOWN_FIRST)
            candidates = (first, *(candidate for candidate in candidates if candidate != first))
        failures: list[str] = []
        ceiling = spending.settings.spot_max_usd_per_hour
        for number, candidate in enumerate(candidates, 1):
            if int(self.clock()) >= deadline:
                raise DeploymentRefused('The Hana setup reservation expired before the next launch.')
            retrying = 'pending_launch' in saved
            token = PendingHanaLaunch.decode(saved['pending_launch']).token if retrying else uuid4().hex
            saved['pending_launch'] = PendingHanaLaunch(
                candidate.instance_type, candidate.zone, candidate.subnet, token).encode()
            _save_hana_setup(path, saved)
            print(f'attempt {number} of {len(candidates)}: {candidate.description} at '
                  + f'{candidate.usd_per_hour:f} USD/h (ceiling {ceiling:f})', flush=True)
            try:
                reply = api.call('run-instances', '--launch-template', ledger.text(saved, 'launch_template'),
                    '--instance-type', candidate.instance_type, '--subnet-id', candidate.subnet,
                    '--security-group-ids', ledger.text(saved, 'security_group'), '--associate-public-ip-address',
                    '--count', '1', '--client-token', token)
            except AwsUnavailable as error:
                reason = ' '.join(str(error).split())
                code = aws_error_code(error, first_attempt_of='RunInstances')
                spot_refusal = code in HANA_SPOT_REFUSALS or (not code and (
                    'no spot capacity' in reason.lower()
                    or 'lower than the minimum required spot request fulfillment price' in reason.lower()))
                message = f'attempt {number} failed: {reason}'
                if not retrying and (spot_refusal or code in HANA_CREATE_REFUSALS):
                    del saved['pending_launch']
                    _save_hana_setup(path, saved)
                if retrying or not spot_refusal:
                    raise DeploymentRefused(message) from error
                print(message, flush=True)
                failures.append(f'{candidate.description}: {message}')
                continue
            instances = ledger.rows(reply, 'Instances')
            if len(instances) != 1:
                raise DeploymentRefused('AWS did not identify one instance for the Hana launch.')
            launched = instances[0]
            saved.update(instance=ledger.text(launched, 'InstanceId'), instance_type=candidate.instance_type,
                         zone=candidate.zone, subnet=candidate.subnet)
            if launched.get('SpotInstanceRequestId'):
                saved['spot_request'] = ledger.text(launched, 'SpotInstanceRequestId')
            _save_hana_setup(path, saved)
            return
        spending.close_setup(int(self.clock()), ledger.instant(saved, 'generation'))
        raise DeploymentRefused('No Hana Spot candidate launched. The stack is ready for another setup run.\n'
                                + '\n'.join(failures))

    def _hana_check_times(self, timeout: int, interval: int, *, check_at_deadline: bool = False) -> Generator[float]:
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            yield started
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.pause(min(interval, remaining))
        if check_at_deadline:
            yield started

    def _attach_hana_data(self, api: AwsCli, path: Path, saved: dict[str, object],
                          size: int, timeout: int, interval: int) -> None:
        instance, zone = ledger.text(saved, 'instance'), ledger.text(saved, 'zone')
        if saved.get('data_volume'):
            volume = ledger.text(saved, 'data_volume')
            if 'pending_volume' not in saved:
                volumes = ledger.rows(api.call('describe-volumes', '--volume-ids', volume), 'Volumes')
                if len(volumes) != 1 or volumes[0].get('VolumeId') != volume or volumes[0].get('AvailabilityZone') != zone:
                    raise DeploymentRefused('The Hana data volume differs from the saved volume.')
                attachments = ledger.rows(volumes[0], 'Attachments')
                if attachments:
                    if (len(attachments) != 1 or attachments[0].get('InstanceId') != instance
                            or attachments[0].get('Device') != HANA_DATA_DEVICE
                            or attachments[0].get('State') not in ('attaching', 'attached')):
                        raise DeploymentRefused('The Hana data volume attachment differs from the saved instance.')
                    _ = api.call('wait', 'volume-in-use', '--volume-ids', volume, timeout_s=timeout)
                    return
        else:
            identity = ledger.text(saved, 'deployment')
            retrying = 'pending_volume' in saved
            token = (ledger.text(saved, 'pending_volume') if retrying
                     else sha256(f'hana-data:{identity}:{instance}'.encode()).hexdigest())
            saved['pending_volume'] = token
            _save_hana_setup(path, saved)
            tags = {'HanaDeployment': identity, 'Workload': 'hana', 'HanaData': 'true', 'HanaVolumeToken': token}
            try:
                reply = api.call('create-volume', '--availability-zone', zone, '--size', str(size), '--volume-type', 'gp3',
                    '--encrypted', '--client-token', token, '--tag-specifications', json.dumps(
                        [{'ResourceType': 'volume', 'Tags': [{'Key': key, 'Value': value} for key, value in tags.items()]}]))
            except AwsUnavailable as error:
                if not retrying and aws_error_code(error, first_attempt_of='CreateVolume') in HANA_CREATE_REFUSALS:
                    del saved['pending_volume']
                    _save_hana_setup(path, saved)
                raise
            volume = ledger.text(reply, 'VolumeId')
            saved['data_volume'] = volume
            _save_hana_setup(path, saved)
        _ = api.call('wait', 'volume-available', '--volume-ids', volume, timeout_s=timeout)
        if 'pending_volume' in saved:
            del saved['pending_volume']
            _save_hana_setup(path, saved)
        for _started in self._hana_check_times(timeout, interval):
            state = api.observe(instance, int(self.clock())).state
            if state in ('running', 'stopped'):
                break
            if state not in ('pending', 'stopping'):
                raise DeploymentRefused(f'Cannot attach the Hana data disk while the instance is {state}.')
        else:
            raise DeploymentRefused('The Hana instance is not ready for its data disk. Run approved setup again.')
        _ = api.call('attach-volume', '--volume-id', volume, '--instance-id', instance, '--device', HANA_DATA_DEVICE)
        _ = api.call('wait', 'volume-in-use', '--volume-ids', volume, timeout_s=timeout)

    @staticmethod
    def _hana_setup_started(book: ledger.CostLedger, saved: dict[str, object]) -> int:
        generation = ledger.instant(saved, 'generation')
        for row in book.read_snapshot().hana_compute:
            if row.generation == generation:
                return row.at
        raise DeploymentRefused('The saved Hana setup has no reservation time.')

    def _clear_hana_setup(self, spending: hana_spend.HanaSpending[hana_spend.PermitSettings],
                          path: Path, saved: dict[str, object]) -> None:
        generation = ledger.instant(saved, 'generation')
        now = int(self.clock())
        spending.reconcile(now, 'terminated', generation=generation)
        if any(row.generation == generation and isinstance(row.state, ledger.HanaExposureOpen)
               for row in spending.book.read_snapshot().hana_compute):
            raise DeploymentRefused('The Hana setup reservation is still open. Repeat approved setup.')
        spending.confirm_storage_deleted(now)
        path.unlink()

    def hana_image(self, result: Path, *, approve_resources: bool = False) -> bool:
        """Upload the built Hana image to one snapshot, register it, and save the image and network identity."""
        built = hana_image.read_built(result)
        workload = conf.read_or_repository('hana_aws')
        print(f'Hana image plan: {built.name}; {built.logical_bytes / 2**30:.1f} GiB; boot {built.boot_mode}')
        print('Hana image resources: one EBS snapshot and one registered image; no instance')
        _print_hana_placement(workload)
        if not approve_resources:
            return False
        self._authorize(True)
        limits = hana_spend.read_settings(workload)
        settings = replace(read_deployment_settings(self.settings_path), region=limits.region,
                           timeout_s=workload.whole('aws_timeout_s'))
        zone = workload.text('availability_zone')
        upload_timeout = workload.whole('image_upload_timeout_s')
        wait_interval = max(workload.whole('image_wait_interval_s'),
                            (upload_timeout + hana_image.COLDSNAP_MAX_WAIT_ATTEMPTS - 1)
                            // hana_image.COLDSNAP_MAX_WAIT_ATTEMPTS)
        wait_attempts = upload_timeout // wait_interval
        with self._require_hana_control('hana-image'):
            self._refuse_hana_replacement()
            digest = hana_image.artifact_digest(built.store, self.execute, upload_timeout)
            description = 'Hana guest digest ' + digest.value
            api = self._api(settings)
            _ = self._guard()
            subnets = [row for row in gate_subnets(api, settings.vpc_id) if row['zone'] == zone]
            if not subnets:
                raise DeploymentRefused(f'The gate VPC has no available public subnet in {zone}')
            images = ledger.rows(api.call('describe-images', '--owners', 'self',
                                          '--filters', 'Name=name,Values=' + built.name), 'Images')
            if not images:
                tag = conf.read(self.settings_path)
                cost_tag = 'Key=%s,Value=%s' % (tag.text('cost_explorer_tag_key'), tag.text('cost_explorer_tag_value'))
                try:
                    pending = hana_image.saved(self.directory)
                except hana_image.RegistrationDigestRefused:
                    pending = hana_image.NoRegisteredImage()
                resume = isinstance(pending, hana_image.PendingImageRegistration) and pending.name == built.name
                if not resume:
                    snapshots = ledger.rows(api.call('describe-snapshots', '--owner-ids', 'self', '--filters',
                                                     'Name=description,Values=' + built.name), 'Snapshots')
                    if snapshots:
                        states = ', '.join(ledger.text(row, 'State') for row in snapshots)
                        if len(snapshots) != 1 or snapshots[0].get('State') not in ('completed', 'pending'):
                            raise DeploymentRefused(
                                f'Hana snapshot adoption refused: {len(snapshots)} matching snapshots; states: {states}')
                        pending = hana_image.PendingImageRegistration(built.name, ledger.text(snapshots[0], 'SnapshotId'))
                        _ = hana_image.save(self.directory, pending)
                        resume = True
                raw = self.output / 'hana' / 'image.raw'
                try:
                    if not resume:
                        raw.parent.mkdir(parents=True, exist_ok=True)
                        with step('hana-image', 'converting the image to a raw disk', self.settings_path):
                            _ = self.execute(['qemu-img', 'convert', '-f', built.source_format, '-O', 'raw',
                                              str(built.file), str(raw)], capture_output=True, text=True,
                                             timeout=upload_timeout, check=True)
                    tool = self.execute(['nix', 'build', '--no-link', '--print-out-paths',
                                         workload.text('image_upload_tool')], capture_output=True, text=True,
                                        timeout=upload_timeout, check=True).stdout.strip().splitlines()[-1]
                    if resume and isinstance(pending, hana_image.PendingImageRegistration):
                        snapshot = pending.snapshot
                    else:
                        with step('hana-image', 'uploading the snapshot', self.settings_path, hint='can take several minutes'):
                            upload = self.admin.executor(self.execute)(
                                [tool + '/bin/coldsnap', '--region', settings.region, 'upload', '--no-progress',
                                 '--omit-zero-blocks', '--description', built.name, '--tag', cost_tag, str(raw)],
                                capture_output=True, text=True, timeout=upload_timeout, check=False)
                        if upload.returncode or not upload.stdout.strip():
                            raise DeploymentRefused('the Hana snapshot upload failed: ' + upload.stderr.strip())
                        snapshot = upload.stdout.strip().splitlines()[-1]
                        _ = hana_image.save(self.directory, hana_image.PendingImageRegistration(built.name, snapshot))
                finally:
                    raw.unlink(missing_ok=True)
                with step('hana-image', 'waiting for the snapshot', self.settings_path):
                    waited = self.admin.executor(self.execute)(
                        [tool + '/bin/coldsnap', '--region', settings.region, 'wait',
                         '--seconds-between-attempts', str(wait_interval),
                         '--max-attempts', str(wait_attempts), snapshot],
                        capture_output=True, text=True, timeout=upload_timeout, check=False)
                    if waited.returncode:
                        raise DeploymentRefused('the Hana snapshot wait failed: ' + waited.stderr.strip())
                with step('hana-image', 'registering the image', self.settings_path):
                    image = ledger.text(api.call(
                        'register-image', '--name', built.name, '--description', description, '--architecture', 'x86_64',
                        '--virtualization-type', 'hvm', '--ena-support', '--sriov-net-support', 'simple',
                        '--boot-mode', built.boot_mode, '--imds-support', 'v2.0',
                        '--root-device-name', settings.root_device_name, '--block-device-mappings',
                        f'DeviceName={settings.root_device_name},Ebs={{SnapshotId={snapshot},VolumeType=gp3,DeleteOnTermination=true}}',
                        '--tag-specifications', f'ResourceType=image,Tags=[{{{cost_tag}}}]'), 'ImageId')
                    _ = api.call('wait', 'image-available', '--image-ids', image, timeout_s=upload_timeout)
                images = ledger.rows(api.call('describe-images', '--image-ids', image), 'Images')
            if len(images) != 1 or images[0].get('State') != 'available':
                raise DeploymentRefused('the registered Hana image is not available')
            if images[0].get('Description') != description:
                raise DeploymentRefused('The registered Hana image lacks the matching guest digest. Build a fresh Hana image.')
            path = hana_image.save(self.directory, hana_image.RegisteredImage(
                built.name, ledger.text(images[0], 'ImageId'), ledger.text(images[0], 'OwnerId'),
                settings.vpc_id, str(subnets[0]['id']), digest))
        print(f'Hana image ready. The record is in {path}.')
        return True

    @staticmethod
    def _refuse_hana_stack_instance(api: AwsCli, stack: str) -> None:
        reply = api.call('describe-stack-resources', '--stack-name', stack, service='cloudformation')
        if any(row.get('LogicalResourceId') == 'Instance' and row.get('ResourceType') == 'AWS::EC2::Instance'
               for row in ledger.rows(reply, 'StackResources')):
            raise DeploymentRefused('The existing Hana stack owns the instance. ' + HANA_DOWN_FIRST)

    @staticmethod
    def _hana_parameters_match(reply: dict[str, object], approved: dict[str, str], *, allow_previous: bool = False) -> bool:
        try:
            rows = ledger.rows(reply, 'Parameters')
            values = {}
            for row in rows:
                key = ledger.text(row, 'ParameterKey')
                value = row.get('ParameterValue')
                if allow_previous and 'ParameterValue' not in row and row.get('UsePreviousValue') is True:
                    value = approved.get(key)
                values[key] = value
            return len(rows) == len(approved) and values == approved
        except ValueError:
            return False

    def _hana_stack_change(self, api: AwsCli, settings: DeploymentSettings, template: Path, parameters: dict[str, str],
                           *, keep: str, scope: HanaSetupChange | HanaPolicyUpdate = HANA_SETUP_CHANGE) -> None:
        """Change the Hana stack without replacing its box or disk."""
        timeout = conf.read(self.settings_path).whole('stack_wait_timeout_s')
        started = self.clock()
        try:
            stacks = ledger.rows(api.call('describe-stacks', '--stack-name', settings.stack_name,
                                          service='cloudformation'), 'Stacks')
        except AwsUnavailable as error:
            if 'does not exist' not in str(error):
                raise
            stacks = ()
        kind = 'UPDATE' if stacks else 'CREATE'
        stack_target = settings.stack_name
        policy_only = isinstance(scope, HanaPolicyUpdate)
        stack_owned = isinstance(scope, HanaPolicyUpdate) and scope.ownership is HanaBoxOwnership.STACK
        only_scaler = isinstance(scope, HanaPolicyUpdate) and (stack_owned or scope.only_scaler)
        if policy_only:
            if not stacks:
                raise DeploymentRefused('The Hana stack is absent. The policy update requires an existing stack.')
            if len(stacks) != 1:
                raise DeploymentRefused('Expected one Hana stack for the policy update.')
            stack = stacks[0]
            outputs = self._stack_outputs(stack, require_outputs=True)
            if any(outputs.get(key) != value for key, value in scope.resource_outputs.items()):
                raise DeploymentRefused('Hana resource identities changed. Review the saved deployment.')
            if not self._hana_parameters_match(stack, parameters):
                raise DeploymentRefused('The live Hana stack parameters differ from the approved parameters.')
            stack_target = ledger.text(stack, 'StackId')
            if stack_owned and stack_target != settings.stack_name:
                raise DeploymentRefused('The live Hana stack ID differs from the stack selected for the policy update.')
        else:
            for stack in stacks:
                if 'Outputs' in stack and any(row.get('OutputKey') in ('Instance', 'DataVolume')
                                             for row in ledger.rows(stack, 'Outputs')):
                    raise DeploymentRefused('The existing Hana stack owns the instance or data disk. ' + HANA_DOWN_FIRST)
            if stacks:
                self._refuse_hana_stack_instance(api, settings.stack_name)
        values: list[dict[str, object]] = [
            {'ParameterKey': key, 'UsePreviousValue': True} if policy_only
            else {'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()
        ]
        def validate(change: dict[str, object], wait_error: Exception | None = None) -> bool:
            reason = str(change.get('StatusReason', '')).lower()
            no_changes = change.get('Status') == 'FAILED' and any(
                text in reason for text in ("didn't contain changes", 'no changes'))
            if (only_scaler and no_changes and not change.get('NextToken')
                    and change.get('Changes', []) == []):
                print('The Hana scaler policy is current.')
                return False
            if policy_only and change.get('Status') == 'FAILED':
                raise DeploymentRefused('CloudFormation failed to create the Hana policy change set.')
            if no_changes:
                return False
            if wait_error is not None:
                raise wait_error
            if change.get('Status') != 'CREATE_COMPLETE':
                raise AwsUnavailable('the Hana change set is not complete')
            resources = tuple(ledger.mapping(item.get('ResourceChange')) for item in ledger.rows(change, 'Changes'))
            replaced = [ledger.text(resource, 'LogicalResourceId') for resource in resources
                        if resource.get('ResourceType') in ('AWS::EC2::Instance', 'AWS::EC2::Volume', 'AWS::EC2::VolumeAttachment')
                        and (resource.get('Action') == 'Remove' or resource.get('Replacement') in ('True', 'Conditional'))]
            if replaced:
                refusal = 'hana-setup refuses replacement: ' + ', '.join(replaced)
                if not policy_only:
                    refusal += '; run deploy.py down --workload hana --approve-destroy first'
                raise DeploymentRefused(refusal)
            if policy_only and change.get('NextToken'):
                raise DeploymentRefused('The Hana policy change set has more than one page.')
            if policy_only and (not resources or only_scaler and len(resources) != 1):
                raise DeploymentRefused('The Hana policy change set must contain exactly one scaler policy change.'
                                         if resources else 'The Hana policy change set has no changes.')
            if policy_only and any(
                    resource.get('Action') != 'Modify'
                    or resource.get('Replacement') != 'False'
                    or (only_scaler and (resource.get('LogicalResourceId') != 'ScalerPolicy'
                                        or resource.get('ResourceType') != 'AWS::IAM::ManagedPolicy'))
                    or resource.get('ResourceType') not in ('AWS::IAM::ManagedPolicy', 'AWS::IAM::Policy',
                        'AWS::IAM::RolePolicy', 'AWS::IAM::UserPolicy', 'AWS::IAM::GroupPolicy')
                    for resource in resources):
                raise DeploymentRefused('The Hana change set must contain only IAM policy modifications without replacement.')
            if policy_only and not self._hana_parameters_match(change, parameters, allow_previous=only_scaler):
                raise DeploymentRefused('The Hana policy change set parameters differ from the approved parameters.')
            return True

        if isinstance(self.mode, plan_run.PlanRun):
            raise plan_run.unsupported('Hana plan uses read-only policy simulation')

        name = 'hana-' + uuid4().hex
        execution_requested = False
        try:
            _ = api.call('create-change-set', '--stack-name', stack_target, '--change-set-name', name,
                         '--change-set-type', kind, '--template-body', 'file://' + str(template),
                         '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(values),
                         service='cloudformation')
            wait_error: Exception | None = None
            try:
                _ = api.call('wait', 'change-set-create-complete', '--stack-name', stack_target,
                             '--change-set-name', name, service='cloudformation', timeout_s=timeout)
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                wait_error = error
            change = api.call('describe-change-set', '--stack-name', stack_target, '--change-set-name', name,
                              *(['--no-paginate'] if policy_only else []), service='cloudformation')
            if not validate(change, wait_error):
                _ = self._discard_change_set(api, stack_target, name)
                return
            execute_api = self._api(settings, single_attempt=True) if policy_only else api
            execution_requested = True
            try:
                _ = execute_api.call('execute-change-set', '--stack-name', stack_target, '--change-set-name', name,
                                     service='cloudformation')
            except AwsUnavailable as error:
                if policy_only and aws_error_code(error, first_attempt_of='ExecuteChangeSet') in (
                        'AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation', 'ValidationError',
                        'ChangeSetNotFound', 'ChangeSetNotFoundException', 'InvalidChangeSetStatus',
                        'InsufficientCapabilities', 'InsufficientCapabilitiesException'):
                    execution_requested = False
                raise
            _ = api.call('wait', 'stack-create-complete' if kind == 'CREATE' else 'stack-update-complete',
                         '--stack-name', stack_target, service='cloudformation', timeout_s=timeout)
            if stack_owned:
                print('Hana policy update complete')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            if policy_only or isinstance(error, DeploymentRefused):
                leftover = self._discard_change_set(api, stack_target, name) if not execution_requested else ''
                if isinstance(error, ValueError):
                    raise DeploymentRefused(str(error) + leftover) from None
                raise AwsUnavailable(str(error) + leftover) from None
            if not isinstance(error, (AwsUnavailable, subprocess.SubprocessError)):
                raise
            tag = ('HanaDeployment', parameters['DeploymentId'])
            lines = [str(error), *failed_stack_events(api, settings.stack_name, started)]
            try:
                stack_id = ''
                if kind == 'CREATE':
                    stacks = ledger.rows(api.call('describe-stacks', '--stack-name', settings.stack_name,
                                                  service='cloudformation'), 'Stacks')
                    if len(stacks) != 1 or stacks[0].get('StackStatus') not in ('ROLLBACK_COMPLETE', 'ROLLBACK_FAILED'):
                        raise AwsUnavailable('CloudFormation did not confirm a terminal rollback. The saved setup remains.')
                    stack_id = ledger.text(stacks[0], 'StackId')
                ended = spot_end.end_boxes(api, tag, keep=keep)
                if stack_id:
                    _ = api.call('delete-stack', '--stack-name', stack_id, service='cloudformation')
                    _ = api.call('wait', 'stack-delete-complete', '--stack-name', stack_id,
                                 service='cloudformation', timeout_s=timeout)
                    kept = retained.skipped_since(api, stack_id, started)
                else:
                    kept = retained.skipped_since(api, settings.stack_name, started, missing_ok=True)
                retained.delete(api, kept, wait_s=timeout)
            except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError, spot_end.SpotEndIncomplete) as end_error:
                lines.append(f'The failed Hana change did not remove what it made. {end_error}')
                raise AwsUnavailable('\n'.join(lines)) from None
            lines.append(f'the failed Hana change ended {ended} Spot boxes and their requests, '
                         + f'and deleted {len(kept.volumes)} disks and {len(kept.buckets)} buckets')
            if kind == 'CREATE':
                raise HanaCreateRolledBack('\n'.join(lines)) from None
            raise AwsUnavailable('\n'.join(lines)) from None

    def hana_down(self, *, approve_destroy: bool = False) -> bool:
        """Delete Hana resources and close their charges."""
        print('down --workload hana cancels the Spot request and terminates the Hana instance.')
        print('It deletes the data disk, stack, and control bucket. This deletes their data.')
        if not approve_destroy:
            print('Run deploy.py down --workload hana --approve-destroy to remove the Hana box.')
            return True
        self._check_credentials()
        path = self.directory / 'hana' / 'setup.json'
        with self._require_hana_control('hana-down'):
            self._refuse_hana_replacement()
            if not path.exists():
                print('down --workload hana: no Hana deployment is saved')
                return True
            saved = remote.read_record(path)
            workload = conf.read_or_repository('hana_aws')
            settings, _, _ = self._hana_inputs(workload)
            if 'region' in saved:
                settings = replace(settings, region=ledger.text(saved, 'region'))
            api = self._api(settings)
            timeout = conf.read(self.settings_path).whole('stack_wait_timeout_s')
            tag = ('HanaDeployment', ledger.text(saved, 'deployment'))
            direct = saved.get('version') == HANA_SETUP_VERSION
            if direct and any(key in saved for key in ('pending_launch', 'pending_volume')):
                lookup_timeout = workload.whole('intent_lookup_timeout_s')
                interval = workload.whole('stop_poll_seconds')
                if min(lookup_timeout, interval) <= 0:
                    raise DeploymentRefused('Hana teardown requires positive values for intent_lookup_timeout_s and stop_poll_seconds.')
                self._resolve_hana_intents(api, path, saved, lookup_timeout, interval)
            volumes: frozenset[str] = self._hana_data_volumes(api, saved) if direct else frozenset()
            kept = retained.in_stack(api, settings.stack_name) | retained.Retained(
                frozenset((ledger.text(saved, 'data_volume'),)) if not direct and saved.get('data_volume') else frozenset(),
                frozenset((ledger.text(saved, 'bucket'),)) if saved.get('bucket') else frozenset())
            book = ledger.CostLedger(self.directory)
            started = self._hana_setup_started(book, saved) if saved.get('requested') and not saved.get('instance') else None
            with step('hana-down', 'ending the Hana Spot boxes', self.settings_path):
                ended = (self._end_saved_hana_boxes(api, tag, saved, timeout) if direct
                         else spot_end.end_boxes(api, tag, wait_s=timeout))
                if not ended:
                    print('hana-down: no live Hana instance remains.')
            if direct:
                with step('hana-down', 'deleting the Hana data disk', self.settings_path):
                    retained.delete(api, retained.Retained(volumes), wait_s=timeout)
            with step('hana-down', 'deleting the Hana stack', self.settings_path, hint='can take several minutes'):
                _ = api.call('delete-stack', '--stack-name', settings.stack_name, service='cloudformation')
                _ = api.call('wait', 'stack-delete-complete', '--stack-name', settings.stack_name,
                             service='cloudformation', timeout_s=timeout)
            if not direct:
                with step('hana-down', 'checking for Spot boxes that the stack kept', self.settings_path):
                    ended += spot_end.end_boxes(api, tag, wait_s=timeout)
            with step('hana-down', 'deleting the Hana data disk and control bucket', self.settings_path):
                if started is not None:
                    deleted = retained.deleted_since(api, settings.stack_name, started)
                    if deleted is None:
                        print('down --workload hana: no deleted Hana stack since setup started')
                    else:
                        kept |= deleted
                retained.delete(api, kept, wait_s=timeout)
            spending = hana_spend.HanaSpending(book, self.settings_path, hana_spend.read_settings(workload))
            now = int(self.clock())
            compute = book.read_snapshot().hana_compute if book.path.exists() else []
            if compute:
                spending.reconcile(now, 'terminated', generation=max(row.generation for row in compute))
            spending.confirm_storage_deleted(now)
            for name in ('lifecycle.json', 'setup.json'):
                (self.directory / 'hana' / name).unlink(missing_ok=True)
        print(f'down --workload hana: done; {ended} Spot boxes ended; '
              + f'{len(volumes | kept.volumes)} disks and {len(kept.buckets)} buckets deleted')
        return True

    def _resolve_hana_intents(self, api: AwsCli, path: Path, saved: dict[str, object], timeout: int, interval: int) -> None:
        if 'pending_launch' in saved:
            pending = PendingHanaLaunch.decode(saved['pending_launch'])
            for _started in self._hana_check_times(timeout, interval):
                reply = api.call('describe-instances', '--filters', f'Name=client-token,Values={pending.token}')
                instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
                if len(instances) > 1:
                    raise DeploymentRefused('AWS returned multiple instances for the pending Hana launch. Teardown stopped.')
                if not instances or not instances[0].get('SpotInstanceRequestId'):
                    continue
                request = ledger.text(instances[0], 'SpotInstanceRequestId')
                try:
                    requests = ledger.rows(api.call('describe-spot-instance-requests', '--spot-instance-request-ids', request),
                                           'SpotInstanceRequests')
                except AwsUnavailable as error:
                    if 'InvalidSpotInstanceRequestID.NotFound' not in str(error):
                        raise
                    continue
                if not any(row.get('SpotInstanceRequestId') == request for row in requests):
                    continue
                saved.update(instance=ledger.text(instances[0], 'InstanceId'), spot_request=request,
                             instance_type=pending.instance_type, zone=pending.zone, subnet=pending.subnet)
                del saved['pending_launch']
                _save_hana_setup(path, saved)
                break
            else:
                raise DeploymentRefused('AWS did not confirm the pending Hana launch. Run approved teardown again.')
        if 'pending_volume' in saved:
            token = ledger.text(saved, 'pending_volume')
            deployment = ledger.text(saved, 'deployment')
            for _started in self._hana_check_times(timeout, interval):
                reply = api.call('describe-volumes', '--filters', f'Name=tag:HanaVolumeToken,Values={token}',
                                 f'Name=tag:HanaDeployment,Values={deployment}')
                volumes = ledger.rows(reply, 'Volumes')
                if len(volumes) > 1:
                    raise DeploymentRefused('AWS returned multiple disks for the pending Hana disk creation. Teardown stopped.')
                if not volumes or volumes[0].get('State') == 'creating':
                    continue
                volume = ledger.text(volumes[0], 'VolumeId')
                if saved.get('data_volume', volume) != volume:
                    raise DeploymentRefused('The pending Hana disk differs from the saved disk. Teardown stopped.')
                saved['data_volume'] = volume
                del saved['pending_volume']
                _save_hana_setup(path, saved)
                break
            else:
                raise DeploymentRefused('AWS did not confirm the pending Hana disk creation. Run approved teardown again.')

    @staticmethod
    def _end_saved_hana_boxes(api: AwsCli, tag: tuple[str, str], saved: dict[str, object], timeout: int) -> int:
        request = ledger.text(saved, 'spot_request') if saved.get('spot_request') else ''
        if not saved.get('instance'):
            print('hana-down: no saved instance ID. Checking the deployment tags.')
        if not request:
            print('hana-down: no saved Spot request ID. Checking the instance and deployment tags.')
            if saved.get('instance'):
                instance = ledger.text(saved, 'instance')
                reply = api.call('describe-instances', '--filters', f'Name=instance-id,Values={instance}')
                for reservation in ledger.rows(reply, 'Reservations'):
                    for row in ledger.rows(reservation, 'Instances'):
                        if row.get('InstanceId') == instance and row.get('SpotInstanceRequestId'):
                            request = ledger.text(row, 'SpotInstanceRequestId')
        if request:
            try:
                rows = ledger.rows(api.call('describe-spot-instance-requests', '--spot-instance-request-ids', request),
                                   'SpotInstanceRequests')
                if any(row.get('SpotInstanceRequestId') == request and row.get('State') in spot_end.LIVE_REQUEST_STATES
                       for row in rows):
                    _ = api.call('cancel-spot-instance-requests', '--spot-instance-request-ids', request)
                else:
                    print('hana-down: the saved Spot request is already closed or gone.')
            except AwsUnavailable as error:
                if 'InvalidSpotInstanceRequestID.NotFound' not in str(error):
                    raise
                print('hana-down: the saved Spot request is already gone.')
        ended = spot_end.end_boxes(api, tag, wait_s=timeout)
        if not request:
            return ended
        # Check the exact request too. Its replacement instances can lack deployment tags.
        reply = api.call('describe-instances', '--filters', f'Name=spot-instance-request-id,Values={request}',
                         'Name=instance-state-name,Values=' + spot_end.LIVE_BOX_STATES)
        remaining = {ledger.text(row, 'InstanceId') for group in ledger.rows(reply, 'Reservations')
                     for row in ledger.rows(group, 'Instances') if row.get('SpotInstanceRequestId') == request}
        if saved.get('instance'):
            instance = ledger.text(saved, 'instance')
            reply = api.call('describe-instances', '--filters', f'Name=instance-id,Values={instance}',
                             'Name=instance-state-name,Values=' + spot_end.LIVE_BOX_STATES)
            if any(row.get('InstanceId') == instance for group in ledger.rows(reply, 'Reservations')
                   for row in ledger.rows(group, 'Instances')):
                remaining.add(instance)
        if remaining:
            instances = sorted(remaining)
            _ = api.call('terminate-instances', '--instance-ids', *instances)
            _ = api.call('wait', 'instance-terminated', '--instance-ids', *instances, timeout_s=timeout)
        try:
            rows = ledger.rows(api.call('describe-spot-instance-requests', '--spot-instance-request-ids', request),
                               'SpotInstanceRequests')
        except AwsUnavailable as error:
            if 'InvalidSpotInstanceRequestID.NotFound' not in str(error):
                raise
            rows = ()
        if any(row.get('SpotInstanceRequestId') == request and row.get('State') in spot_end.LIVE_REQUEST_STATES
               for row in rows):
            raise spot_end.SpotEndIncomplete('The saved Hana Spot request remains open.')
        return ended + len(remaining)

    @staticmethod
    def _hana_data_volumes(api: AwsCli, saved: dict[str, object]) -> frozenset[str]:
        if saved.get('data_volume'):
            return frozenset((ledger.text(saved, 'data_volume'),))
        print('hana-down: no saved data disk ID.')
        if not saved.get('instance'):
            return frozenset()
        # Recover a disk when AWS created it before setup saved the ID.
        reply = api.call('describe-volumes', '--filters', 'Name=tag:HanaData,Values=true',
                         'Name=tag:HanaDeployment,Values=' + ledger.text(saved, 'deployment'))
        return frozenset(ledger.text(row, 'VolumeId') for row in ledger.rows(reply, 'Volumes'))

    def _hana_inputs(self, workload: conf.ConfigRead | None = None) -> tuple[DeploymentSettings, dict[str, str], bytes]:
        """Share the existing deployment validation and use only Hana sizes and prices."""
        if workload is None:
            workload = conf.read_or_repository('hana_aws')
        limits = hana_spend.read_settings(workload)
        # Empty identity values are allowed here; text() refuses an empty value.
        present = workload.values('ami_id', 'ami_owner_id', 'vpc_id', 'subnet_id')
        names = {key: workload.text(key) for key in ('stack_name', 'availability_zone')}
        names.update({key: present.get(key, '') for key in ('ami_id', 'ami_owner_id', 'vpc_id', 'subnet_id')})
        registered = hana_image.saved(self.directory)
        if isinstance(registered, hana_image.RegisteredImage):
            # Configured values win; the saved registration keeps cloud identities out of the repository.
            for key, value in (('ami_id', registered.image), ('ami_owner_id', registered.owner),
                               ('vpc_id', registered.vpc), ('subnet_id', registered.subnet)):
                names[key] = names[key] or value
        if not all(names.values()):
            raise DeploymentRefused('Configure the reviewed Hana image, owner, VPC, and subnet before prepare')
        settings = replace(read_deployment_settings(self.settings_path), **names,
            region=limits.region, timeout_s=workload.whole('aws_timeout_s'))
        if settings.stack_name == conf.read(self.settings_path).text('stack_name'):
            raise DeploymentRefused('The Hana stack must differ from the gate stack')
        if not settings.availability_zone.startswith(settings.region):
            raise DeploymentRefused('The Hana zone must belong to its region')
        identity_path = self.directory / 'hana' / 'setup.json'
        saved = remote.read_record(identity_path) if identity_path.exists() else {}
        identity = ledger.text(saved, 'deployment') if saved else uuid4().hex
        if saved and saved.get('stack') != settings.stack_name:
            raise DeploymentRefused('The saved Hana stack differs; review replacement before setup')
        parameters = {
            'DeploymentId': identity, 'Image': settings.ami_id, 'InstanceType': limits.instance_type,
            'FallbackInstanceTypes': ','.join(limits.fallback_instance_types),
            'AvailabilityZone': settings.availability_zone, 'VpcId': settings.vpc_id, 'SubnetId': settings.subnet_id,
            'RootDevice': settings.root_device_name, 'RootGiB': str(limits.root_gib), 'DataGiB': str(limits.data_gib),
            'SpotMaxPrice': str(limits.spot_max_usd_per_hour), 'ScalerUser': settings.scaler_user_name,
        }
        return settings, parameters, HANA_TEMPLATE.read_bytes()

    def _hana_output(self) -> Deployment:
        """Use separate preparation files with the existing deployment helper."""
        child = Deployment(self.settings_path, self.directory, self.output / 'hana',
            execute=self.execute, clock=self.clock, pause=self.pause, notify=self.notify)
        child.admin = self.admin
        return child

    def _write_preparation(self, settings: DeploymentSettings, parameters: dict[str, str], template: bytes,
                           decision: ledger.SpendingAllowed, resources: tuple[ledger.PricedResource, ...] = ()) -> PreparedDeployment:
        prepared = PreparedDeployment(int(self.clock()), fingerprint(settings, parameters, template), parameters)
        self.output.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.output / 'template.json', template.decode())
        write_text_atomic(self.output / 'parameters.json', json.dumps([
            {'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()
        ], sort_keys=True) + '\n')
        write_text_atomic(self.output / 'preflight.json', json.dumps({
            'at': prepared.at, 'fingerprint': prepared.fingerprint,
            'decision': ledger.decision_record(decision),
            'inventory': [{'name': resource.name, 'usd_per_month': str(resource.usd_per_month)} for resource in resources],
        }, sort_keys=True) + '\n')
        return prepared

    def prepare(self) -> PreparedDeployment:
        admin_secrets.refuse_plain_admin(self.settings_path)
        with self._require_hana_control('prepare'):
            settings = read_deployment_settings(self.settings_path)
            now = int(self.clock())
            notification = read_budget_notification_address(self.settings_path)
            parameters = self._parameters(settings, notification)
            book = ledger.CostLedger(self.directory)
            state = book.read_snapshot() if book.path.exists() else ledger.LedgerEvidence()
            resources = state.inventories[-1].resources if state.inventories else ()
            if not state.inventories:
                book.record_inventory(ledger.ResourceInventory(now, resources, 'Budget foundation', builder_removed=True))
            decision = self._guard()
            template = TEMPLATE.read_bytes()
            return self._write_preparation(settings, parameters, template, decision, resources)

    def _stack(self, settings: DeploymentSettings, *, require_outputs: bool = False) -> dict[str, str]:
        try:
            row = self._api(settings).call('describe-stacks', '--stack-name', settings.stack_name, service='cloudformation')
        except AwsUnavailable as error:
            if 'does not exist' in str(error) and 'ValidationError' in str(error):
                return {}
            raise
        stacks = ledger.rows(row, 'Stacks')
        if len(stacks) != 1:
            raise DeploymentRefused('expected one stack')
        stack = stacks[0]
        saved = deployment_record(self.directory)
        if (saved.get('stack_id') and saved.get('stack') == settings.stack_name
                and stack.get('StackId') != saved['stack_id']):
            raise DeploymentRefused('the live StackId differs from the deployment record')
        return self._stack_outputs(stack, require_outputs=require_outputs)

    @staticmethod
    def _stack_outputs(stack: dict[str, object], *, require_outputs: bool) -> dict[str, str]:
        status = ledger.text(stack, 'StackStatus')
        if status == 'REVIEW_IN_PROGRESS' and not require_outputs:
            return {}
        if status not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
            raise DeploymentRefused(f'reconcile the stack before deployment: {status}')
        outputs: dict[str, str] = {}
        for item in ledger.rows(stack, 'Outputs'):
            value = item.get('OutputValue')
            if not isinstance(value, str):
                raise DeploymentRefused('stack output values must contain text')
            outputs[ledger.text(item, 'OutputKey')] = value
        if require_outputs and not outputs:
            raise DeploymentRefused('The existing Hana stack has no outputs. Reconcile it before setup.')
        return outputs

    def _validate_inputs(self, settings: DeploymentSettings) -> None:
        reply = self._api(settings).call('describe-images', '--image-ids', settings.ami_id,
                           '--owners', settings.ami_owner_id)
        images = ledger.rows(reply, 'Images')
        if len(images) != 1:
            raise DeploymentRefused('the selected official NixOS image is unavailable')
        image = images[0]
        if (image.get('ImageId') != settings.ami_id or image.get('OwnerId') != settings.ami_owner_id
                or image.get('Architecture') != 'x86_64' or image.get('State') != 'available'
                or 'nixos' not in str(image.get('Name', '')).lower()
                or settings.nixos_release not in str(image.get('Name', ''))
                or image.get('RootDeviceName') != settings.root_device_name):
            raise DeploymentRefused('the image identity, release, architecture or root device does not match')
        subnet = self._api(settings).call('describe-subnets', '--subnet-ids', settings.subnet_id)
        rows = ledger.rows(subnet, 'Subnets')
        if len(rows) != 1 or rows[0].get('VpcId') != settings.vpc_id or rows[0].get('AvailabilityZone') != settings.availability_zone:
            raise DeploymentRefused('the subnet does not match the VPC and availability zone')

    @staticmethod
    def _discard_change_set(api: AwsCli, stack_id: str, name: str) -> str:
        try:
            _ = api.call('delete-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
        except (ValueError, OSError, AwsUnavailable, subprocess.SubprocessError):
            return '\nunexecuted change set remains: ' + name
        return ''

    def _stack_failure(self, api: AwsCli, stack_id: str, started: float,
                       error: Exception, change: dict[str, object], notification: str) -> AwsUnavailable:
        lines = [str(error)]
        if change.get('StatusReason'):
            lines.append(str(change['StatusReason']))
        lines.extend(failed_stack_events(api, stack_id, started))
        try:
            stacks = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id, service='cloudformation'), 'Stacks')
            for stack in stacks:
                status = ledger.text(stack, 'StackStatus')
                lines.append('stack status: ' + status)
                if status == 'UPDATE_ROLLBACK_COMPLETE':
                    lines.append('the stack rolled back; its resources did not change')
        except (ValueError, AwsUnavailable, subprocess.SubprocessError):
            lines.append('stack status is unavailable')
        return AwsUnavailable('\n'.join(lines).replace(notification, '<notification address>'))

    def _update_stack(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        api = self._api(settings)
        stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
        _ = self._stack(settings)
        notification = parameters.get('NotificationEmail') or '<notification address>'
        values: list[dict[str, object]] = [
            {'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()
        ]
        if (self.directory / 'gate-deployment.json').exists():
            template = ledger.mapping(cast(object, json.loads(TEMPLATE.read_text())))
            values.extend({'ParameterKey': key, 'UsePreviousValue': True}
                          for key in ledger.mapping(template['Parameters']) if key.startswith('Gate') or key == 'EnableGates')
        name = 'update-' + uuid4().hex
        started = self.clock()
        change: dict[str, object] = {}
        created = False
        executed = False
        try:
            _ = api.call('create-change-set', '--stack-name', stack_id, '--change-set-name', name,
                         '--change-set-type', 'UPDATE', '--template-body', 'file://' + str(self.output / 'template.json'),
                         '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(values), service='cloudformation')
            created = True
            wait_error: Exception | None = None
            try:
                _ = api.call('wait', 'change-set-create-complete', '--stack-name', stack_id,
                             '--change-set-name', name, service='cloudformation',
                             timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                wait_error = error
            change = api.call('describe-change-set', '--stack-name', stack_id, '--change-set-name', name,
                              service='cloudformation')
            reason = str(change.get('StatusReason', ''))
            if change.get('Status') == 'FAILED' and any(text in reason.lower() for text in (
                    "didn't contain changes", 'no updates are to be performed', 'no changes')):
                _ = api.call('delete-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
                return
            if wait_error is not None:
                raise wait_error
            if change.get('Status') != 'CREATE_COMPLETE':
                raise AwsUnavailable('the update change set is not complete')
            protected = {
                'AWS::EC2::Instance', 'AWS::EC2::Volume', 'AWS::EC2::VolumeAttachment',
                'AWS::EC2::NetworkInterface', 'AWS::EC2::NetworkInterfaceAttachment',
                'AWS::EC2::EIP', 'AWS::EC2::EIPAssociation', 'AWS::Budgets::Budget',
            }
            replacements: list[str] = []
            for item in ledger.rows(change, 'Changes'):
                resource = ledger.mapping(item.get('ResourceChange'))
                if resource.get('ResourceType') in protected and (resource.get('Action') == 'Remove'
                        or resource.get('Replacement') in ('True', 'Conditional')):
                    replacements.append(f"{ledger.text(resource, 'LogicalResourceId')} ({ledger.text(resource, 'ResourceType')})")
            if replacements:
                raise DeploymentRefused('up refuses replacement: ' + ', '.join(replacements)
                                        + '; run deploy.py down --approve-destroy, then deploy.py up --approve-resources')
            self._before_request(settings, parameters)
            _ = api.call('execute-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
            executed = True
            _ = api.call('wait', 'stack-update-complete', '--stack-name', stack_id, service='cloudformation',
                         timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        except (ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            if not change:
                try:
                    change = api.call('describe-change-set', '--stack-name', stack_id,
                                      '--change-set-name', name, service='cloudformation')
                    created = True
                except (AwsUnavailable, subprocess.SubprocessError):
                    pass
            leftover = self._discard_change_set(api, stack_id, name) if created and not executed else ''
            if isinstance(error, ValueError):
                raise DeploymentRefused((str(error) + leftover).replace(notification, '<notification address>')) from None
            failure = self._stack_failure(api, stack_id, started, error, change, notification)
            raise AwsUnavailable(str(failure) + leftover) from None


    def _deploy(self, settings: DeploymentSettings, parameters: dict[str, str], ) -> None:
        self._before_request(settings, parameters)
        self._reserve_stack(settings, parameters)
        live = self._stack(settings)
        try:
            self._confirm_preparation(settings, parameters, renew=True)
            self._before_request(settings, parameters)
        except (ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            if not live:
                stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
                leftover = self._discard_change_set(self._api(settings), stack_id, 'identity-' + parameters['DeploymentId'])
                if leftover:
                    reason = (str(error) + leftover).replace(parameters['NotificationEmail'], '<notification address>')
                    if isinstance(error, ValueError):
                        raise DeploymentRefused(reason) from None
                    raise AwsUnavailable(reason) from None
            raise
        if live:
            self._update_stack(settings, parameters)
        else:
            api = self._api(settings)
            started = self.clock()
            try:
                _ = api.call('deploy',
                    '--stack-name', ledger.text(deployment_record(self.directory), 'stack_id'),
                    '--template-file', str(self.output / 'template.json'),
                    '--capabilities', 'CAPABILITY_NAMED_IAM', '--no-fail-on-empty-changeset',
                    '--parameter-overrides', *[f'{key}={value}' for key, value in parameters.items()],
                    service='cloudformation')
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                raise self._stack_failure(api, ledger.text(deployment_record(self.directory), 'stack_id'),
                                          started, error, {}, parameters['NotificationEmail']) from None
        self._save_scaler_identity(settings)
        outputs = self._stack(settings)
        saved = deployment_record(self.directory)
        saved['outputs'] = {**ledger.mapping(saved.get('outputs', {})), **outputs}
        if outputs.get('BudgetName'):
            saved['budget_name'] = outputs['BudgetName']
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    def _confirm_preparation(self, settings: DeploymentSettings, parameters: dict[str, str], *, renew: bool = False) -> None:
        current = read_deployment_settings(self.settings_path)
        notification = BudgetNotificationAddress(parameters['NotificationEmail'])
        current_parameters = self._parameters(current, notification)
        self.confirm_preparation_evidence(settings, parameters, TEMPLATE.read_bytes(), renew=renew,
            current_fingerprint=fingerprint(current, current_parameters, TEMPLATE.read_bytes()))

    def confirm_preparation_evidence(self, settings: DeploymentSettings, parameters: dict[str, str], template: bytes,
                                    *, renew: bool = False, current_fingerprint: str = '', allow_expired: bool = False) -> None:
        """Verify saved preparation before either workload can create resources."""
        preflight = remote.read_record(self.output / 'preflight.json')
        at = ledger.instant(preflight, 'at')
        expected = current_fingerprint or fingerprint(settings, parameters, template)
        age = int(self.clock()) - at
        if ((not renew and (age < 0 or (not allow_expired and age > settings.preflight_max_age_s)))
                or preflight.get('fingerprint') != expected
                or fingerprint(settings, parameters, template) != expected
                or (self.output / 'template.json').read_bytes() != template):
            raise DeploymentRefused('deployment preparation expired or its inputs changed; run prepare again')
        if renew:
            preflight['at'] = int(self.clock())
            write_text_atomic(self.output / 'preflight.json', json.dumps(preflight, sort_keys=True) + '\n')

    def _authorize(self, approve_resources: bool) -> None:
        if isinstance(self.mode, plan_run.PlanRun):
            raise plan_run.unsupported('this command requires approved setup')
        if not approve_resources:
            raise DeploymentRefused('the user must pass --approve-resources at their terminal')
        self._check_credentials()

    def _check_credentials(self) -> None:
        admin_secrets.refuse_plain_admin(self.settings_path)
        try:
            mode = admin_secrets.credentials_file().stat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise OSError
        except OSError:
            raise DeploymentRefused('credential_file must name a private mode 0600 file') from None


    def apply(self, *, approve_resources: bool = False) -> dict[str, str]:
        self._authorize(approve_resources)
        with self._require_account_stack('apply'), self._require_hana_control('apply'):
            settings, parameters = self._load(read_budget_notification_address(self.settings_path))
            stack = self._stack(settings)
            previous_path = self.directory / 'deployment.json'
            if previous_path.exists() and ledger.mapping(remote.read_record(previous_path).get('outputs', {})):
                previous = remote.read_record(previous_path)
                if previous.get('region') != settings.region:
                    raise DeploymentRefused('preserve the region of the saved deployment')
                if previous.get('stack') != settings.stack_name:
                    if self._stack(replace(settings, stack_name=ledger.text(previous, 'stack'))):
                        raise DeploymentRefused('delete the old stack before replacement')
                elif not stack:
                    raise DeploymentRefused('use a new stack_name to preserve the retained Budget')
            self._validate_inputs(settings)
            if not stack:
                self._deploy(settings, parameters)
                stack = self._stack(settings)
                if not stack.get('BudgetName'):
                    raise DeploymentRefused('the Budget foundation has no confirmed evidence')
                self._confirm_preparation(settings, parameters, renew=True)
            self._deploy(settings, parameters)
            outputs = self._stack(settings)
            saved = deployment_record(self.directory)
            saved['fingerprint'] = fingerprint(settings, parameters, TEMPLATE.read_bytes())
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            self.scaler_key()
            return outputs


    def _scaler_api(self) -> AwsCli:
        settings = read_deployment_settings(self.settings_path)
        profile = conf.read(self.settings_path).text('credentials_profile')
        return AwsCli(profile, settings.region, settings.timeout_s, self.execute)

    def _scaler_present(self) -> bool:
        try:
            reply = self._scaler_api().call('get-caller-identity', service='sts')
            user = read_deployment_settings(self.settings_path).scaler_user_name
            return ledger.text(reply, 'Arn').endswith('user/' + user)
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            return False


    def scaler_key(self) -> None:
        settings = read_deployment_settings(self.settings_path)
        api = self._api(settings)
        credential_file = admin_secrets.credentials_file()
        previous = credential_file.read_bytes()
        key_id = ''
        checkpoint = self.directory / 'scaler-key.json'
        credentials = ConfigParser(interpolation=None)
        _ = credentials.read_string(previous.decode())
        profile = conf.read(self.settings_path).text('credentials_profile')
        if checkpoint.exists():
            saved_key = ledger.text(remote.read_record(checkpoint), 'access_key_id')
            if credentials.get(profile, 'aws_access_key_id', fallback='') == saved_key and self._scaler_present():
                self._remove_old_scaler_keys(api, settings.scaler_user_name, saved_key)
                checkpoint.unlink()
                return
            _ = api.call('delete-access-key', '--user-name', settings.scaler_user_name,
                         '--access-key-id', saved_key, service='iam')
            checkpoint.unlink()
        try:
            reply = api.call('create-access-key', '--user-name', settings.scaler_user_name, service='iam')
            key = ledger.mapping(reply.get('AccessKey'))
            key_id = ledger.text(key, 'AccessKeyId')
            secret = ledger.text(key, 'SecretAccessKey')
            write_text_atomic(checkpoint, json.dumps({'access_key_id': key_id}) + '\n', new_file_mode=0o600)
            if not credentials.has_section(profile):
                credentials.add_section(profile)
            credentials.set(profile, 'aws_access_key_id', key_id)
            credentials.set(profile, 'aws_secret_access_key', secret)
            content = StringIO()
            credentials.write(content)
            write_bytes_atomic(credential_file, content.getvalue().encode(), mode=0o600)
            attempts = conf.read(self.settings_path).whole('scaler_key_check_attempts')
            for attempt in range(attempts):
                if self._scaler_present():
                    break
                if attempt + 1 < attempts:
                    time.sleep(conf.read(self.settings_path).number('scaler_key_check_delay_s'))
            else:
                raise DeploymentRefused('the new scaler key did not confirm its IAM user')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            try:
                if key_id:
                    _ = api.call('delete-access-key', '--user-name', settings.scaler_user_name,
                                 '--access-key-id', key_id, service='iam')
            except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
                raise DeploymentRefused('scaler key setup failed; remove the unconfirmed key through IAM') from None
            finally:
                write_bytes_atomic(credential_file, previous, mode=0o600)
            checkpoint.unlink(missing_ok=True)
            raise DeploymentRefused('scaler key setup failed; the new key was removed') from None
        self._remove_old_scaler_keys(api, settings.scaler_user_name, key_id)
        checkpoint.unlink()

    def _remove_old_scaler_keys(self, api: AwsCli, user: str, key_id: str) -> None:
        try:
            reply = api.call('list-access-keys', '--user-name', user, service='iam')
            for old in ledger.rows(reply, 'AccessKeyMetadata'):
                old_id = ledger.text(old, 'AccessKeyId')
                if old_id != key_id:
                    _ = api.call('delete-access-key', '--user-name', user,
                                 '--access-key-id', old_id, service='iam')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            raise DeploymentRefused('the scaler key works; IAM could not remove every old key') from None


    def remove_images(self, *, approve_destroy: bool = False) -> bool:
        """Boot gates from the official image and delete the saved gate and former builder images."""
        if not approve_destroy:
            print(teardown.IMAGES_DRY_RUN)
            return True
        self._check_credentials()
        settings = read_deployment_settings(self.settings_path)
        with self._require_account_stack('remove-images'):
            api = self._api(settings)
            deployment = gate_deployment(self.directory / 'gate-deployment.json', settings.region)
            home = ledger.rows(deployment, 'regions')[0]
            stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
            body = api.call('get-template', '--stack-name', stack_id, service='cloudformation')['TemplateBody']
            template_body = ledger.mapping(cast(object, json.loads(body))) if isinstance(body, str) else ledger.mapping(body)
            image_policy = ledger.mapping(ledger.mapping(ledger.mapping(
                ledger.mapping(template_body['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
            statements = [row for row in ledger.rows(image_policy, 'Statement') if row.get('Sid') != 'GateImageTransition']
            template = self.output / 'gate-image-template.json'
            self.output.mkdir(parents=True, exist_ok=True)
            if home.get('image') != settings.ami_id:
                # Keep the old image launchable until gate-deployment.json names the official image.
                image_policy['Statement'] = [*statements, {
                    'Sid': 'GateImageTransition', 'Effect': 'Allow', 'Action': ['ec2:RunInstances'],
                    'Resource': {'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/'
                                 + ledger.text(home, 'image')}}]
                write_text_atomic(template, json.dumps(template_body))
                stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                            service='cloudformation'), 'Stacks')[0]
                changes: list[dict[str, object]] = [
                    {'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                    for row in ledger.rows(stack, 'Parameters') if row['ParameterKey'] != 'GateImage']
                changes.append({'ParameterKey': 'GateImage', 'ParameterValue': settings.ami_id})
                with step('remove-images', 'switching gates to the official image', self.settings_path,
                          hint='can take several minutes'):
                    self._gate_change(api, stack_id, template, changes)
                home.update(image=settings.ami_id, version=self._stack(settings)['GateLaunchVersion'])
                write_text_atomic(self.directory / 'gate-deployment.json', json.dumps(deployment, sort_keys=True) + '\n')
            if image_policy.get('Statement') != statements:
                image_policy['Statement'] = statements
                write_text_atomic(template, json.dumps(template_body))
                stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                            service='cloudformation'), 'Stacks')[0]
                changes = [{'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                           for row in ledger.rows(stack, 'Parameters')]
                with step('remove-images', 'removing the old image permission', self.settings_path,
                          hint='can take several minutes'):
                    self._gate_change(api, stack_id, template, changes)
            with step('remove-images', 'deleting the gate image and its snapshot', self.settings_path):
                removed = teardown.remove_gate_images(api, self.directory, self.clock)
            with step('remove-images', 'deleting the former builder image and its snapshot', self.settings_path):
                removed = teardown.remove_builder_image(api, self.directory) or removed
        print('remove-images: done; gates boot from the official image and no saved image remains' if removed
              else 'remove-images: nothing to remove; gates boot from the official image')
        return True

    def _gate_change(self, api: AwsCli, stack_id: str, template: Path, changes: list[dict[str, object]]) -> None:
        name = 'gate-' + uuid4().hex
        executed = False
        try:
            print('cloud-gate: preparing the stack change (about 1 minute)', flush=True)
            _ = api.call('create-change-set', '--stack-name', stack_id, '--change-set-name', name,
                         '--change-set-type', 'UPDATE', '--template-body', 'file://' + str(template),
                         '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(changes),
                         service='cloudformation')
            try:
                _ = api.call('wait', 'change-set-create-complete', '--stack-name', stack_id,
                             '--change-set-name', name, service='cloudformation',
                             timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
            except AwsUnavailable:
                pass
            change = api.call('describe-change-set', '--stack-name', stack_id,
                              '--change-set-name', name, service='cloudformation')
            reason = str(change.get('StatusReason', '')).lower()
            empty = change.get('Status') == 'FAILED' and any(text in reason for text in (
                "didn't contain changes", 'no updates are to be performed', 'no changes'))
            if not empty:
                if change.get('Status') != 'CREATE_COMPLETE':
                    raise DeploymentRefused(f"The gate change set is {change.get('Status')}: {change.get('StatusReason', 'no reason given')}")
                for item in ledger.rows(change, 'Changes'):
                    resource = ledger.mapping(item['ResourceChange'])
                    if not ledger.text(resource, 'LogicalResourceId').startswith('Gate'):
                        raise DeploymentRefused('The gate change set changes a Rust resource')
                    print(f"cloud-gate: {resource.get('Action', 'Change')} {ledger.text(resource, 'LogicalResourceId')}", flush=True)
                _ = api.call('execute-change-set', '--stack-name', stack_id, '--change-set-name', name,
                             service='cloudformation')
                executed = True
                print('cloud-gate: applying the change (several minutes)', flush=True)
                try:
                    _ = api.call('wait', 'stack-update-complete', '--stack-name', stack_id, service='cloudformation',
                                 timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
                except AwsUnavailable:
                    events = ledger.rows(api.call('describe-stack-events', '--stack-name', stack_id,
                                                  '--max-items', '60', service='cloudformation'), 'StackEvents')
                    for event in reversed(events):
                        if str(event.get('ResourceStatus', '')).endswith('_FAILED'):
                            failure = event.get('ResourceStatusReason', 'no reason given')
                            print(f"cloud-gate: {event.get('LogicalResourceId')} {event.get('ResourceStatus')}: {failure}", flush=True)
                    raise
        finally:
            if not executed:
                _ = self._discard_change_set(api, stack_id, name)

    def regional_image(self, api: AwsCli, settings: DeploymentSettings, region: str,
                       *, home_image: dict[str, object] | None = None) -> str:
        """Find the matching release or retain a copied image for recovery."""
        target = api.in_region(region)
        source = home_image
        if source is None:
            sources = ledger.rows(api.call('describe-images', '--image-ids', settings.ami_id), 'Images')
            if isinstance(self.mode, plan_run.PlanRun) and not sources:
                raise DeploymentRefused('EC2 did not return the configured home gate image.')
            source = sources[0]
        images = ledger.rows(target.call('describe-images', '--owners', ledger.text(source, 'OwnerId'),
            '--filters', 'Name=name,Values=' + ledger.text(source, 'Name'), 'Name=state,Values=available'), 'Images')
        if images:
            return ledger.text(images[0], 'ImageId')
        path = self.directory / 'regional-images.json'
        saved = remote.read_record(path) if path.exists() else {}
        identity = region + ':' + settings.ami_id
        if isinstance(self.mode, plan_run.PlanRun):
            if identity not in saved:
                raise plan_run.unsupported('the regional gate image needs an EC2 copy')
            image = ledger.text(ledger.mapping(saved[identity]), 'image')
            matches = ledger.rows(target.call('describe-images', '--image-ids', image), 'Images')
            if len(matches) != 1 or matches[0].get('ImageId') != image or matches[0].get('State') != 'available':
                raise plan_run.unsupported('the saved regional gate image is unavailable')
            return image
        if identity not in saved:
            copied = target.call('copy-image', '--source-region', settings.region, '--source-image-id', settings.ami_id,
                '--name', ledger.text(source, 'Name'), '--client-token', sha256(identity.encode()).hexdigest())
            saved[identity] = {'region': region, 'image': ledger.text(copied, 'ImageId')}
            write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')
        image = ledger.text(ledger.mapping(saved[identity]), 'image')
        _ = target.call('wait', 'image-available', '--image-ids', image,
                       timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        return image

    def regional_network(self, api: AwsCli, settings: DeploymentSettings, region: str,
                         *, home_image: dict[str, object] | None = None) -> RegionalNetwork:
        """Read the default VPC and public subnets of a gate region, and find or copy its gate image."""
        target = api.in_region(region)
        vpcs = ledger.rows(target.call('describe-vpcs', '--filters', 'Name=is-default,Values=true'), 'Vpcs')
        if not vpcs:
            raise DeploymentRefused(region + ': no default VPC is available')
        vpc = ledger.text(vpcs[0], 'VpcId')
        try:
            subnets = gate_subnets(target, vpc)
        except DeploymentRefused as error:
            raise DeploymentRefused(region + ': ' + str(error)) from error
        return RegionalNetwork(region, vpc, subnets, self.regional_image(api, settings, region, home_image=home_image))

    def _regional_gate_values(self, settings: DeploymentSettings, parameters: dict[str, str],
                              network: RegionalNetwork, deployment: str, profile: str) -> dict[str, str]:
        return {**parameters, 'GateImage': network.image, 'VpcId': network.vpc,
            'DeploymentId': deployment, 'CostTagKey': read_settings(self.settings_path).cost_explorer_tag_key,
            'CostTagValue': read_settings(self.settings_path).cost_explorer_tag_value,
            'RootDeviceName': settings.root_device_name, 'HomeGateProfileArn': profile}

    def regional_gate_stack(self, api: AwsCli, settings: DeploymentSettings, home: dict[str, str],
                            parameters: dict[str, str], network: RegionalNetwork,
                            digests: RegionalStackDigests) -> dict[str, object]:
        """Deploy only gate resources in the target region; the home stack's scaler policies cover every gate region."""
        region = network.region
        target = api.in_region(region)
        deployment = ledger.text(deployment_record(self.directory), 'deployment_id')
        values = self._regional_gate_values(settings, parameters, network, deployment, home['GateProfileArn'])
        definition = region_gate_template()
        template_text = json.dumps(definition, sort_keys=True) + '\n'
        definitions = ledger.mapping(definition['Parameters'])
        values = {key: value for key, value in values.items() if key in definitions}
        name = settings.stack_name + '-gates-' + region
        digest = sha256(json.dumps({
            'template': template_text, 'name': name, 'parameters': values,
        }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        stack_path = self.directory / 'regional-stacks.json'
        stacks = remote.read_record(stack_path) if stack_path.exists() else {}
        stacks[region] = name
        write_text_atomic(stack_path, json.dumps(stacks) + '\n')
        stack = describe_regional_stack(target, name)
        status = stack.get('StackStatus')
        unchanged = digests.entries.get(region) == regional_stack_digest(digest, stack)
        if unchanged:
            print(f'cloud-gate: {region}: gate stack unchanged; skipped', flush=True)
        else:
            if region in digests.entries:
                # A deploy that stops before it saves the new digest must run again.
                del digests.entries[region]
                digests.save()
            wait_s = conf.read(self.settings_path).whole('stack_wait_timeout_s')
            if status == 'ROLLBACK_COMPLETE':
                print(f'cloud-gate: {region}: removing the gate stack whose first create failed')
                _ = target.call('delete-stack', '--stack-name', name, service='cloudformation')
                _ = target.call('wait', 'stack-delete-complete', '--stack-name', name, service='cloudformation', timeout_s=wait_s)
            started = self.clock()
            try:
                with tempfile.TemporaryDirectory(prefix='gate-region-') as folder:
                    template = Path(folder) / 'template.json'
                    _ = template.write_text(template_text, encoding='utf-8')
                    _ = target.call('deploy', '--stack-name', name, '--template-file', str(template),
                        '--capabilities', 'CAPABILITY_NAMED_IAM', '--no-fail-on-empty-changeset', '--parameter-overrides',
                        *[key + '=' + value for key, value in values.items()], service='cloudformation', timeout_s=wait_s)
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                raise AwsUnavailable('\n'.join([f'{region}: {error}', *failed_stack_events(target, name, started)])) from error
            stack = ledger.rows(target.call('describe-stacks', '--stack-name', name, service='cloudformation'), 'Stacks')[0]
        outputs = {ledger.text(row, 'OutputKey'): ledger.text(row, 'OutputValue') for row in ledger.rows(stack, 'Outputs')}
        result: dict[str, object] = {'region': region, 'image': network.image, 'subnets': network.subnets, 'template': outputs['GateLaunchTemplate'],
                'version': outputs['GateLaunchVersion'], 'security_group': outputs['GateSecurityGroup'], 'stack': name}
        if not unchanged:
            entry = regional_stack_digest(digest, stack)
            if entry:
                digests.entries[region] = entry
                digests.save()
        return result

    def prices(self) -> None:
        """Print regional Spot prices without resource identities."""
        settings = read_deployment_settings(self.settings_path)
        api = self._api(settings)
        from scripts.aws_target import cloud_gate
        values = cloud_gate.settings()
        configured_types = values.words('instance_types')
        largest = largest_gate_vcpus(configured_types)
        sizes = tuple(dict.fromkeys([*configured_types,
                                    'r7a.8xlarge', 'm8a.8xlarge', 'c8a.8xlarge', *values.words('gpu_instance_types')]))
        regions = tuple(dict.fromkeys([*gate_regions(settings.region), *values.words('spot_candidate_regions')]))
        _ = self.admin.get()
        print('region type zone price')
        for region in regions:
            target = api.in_region(region)
            self._print_spot_quota(target, largest)
            try:
                quota = spot_quota(target, 'L-3819A6DF')
                print(f'{region}: G and VT Spot quota {quota.normalize():f} vCPUs')
            except (admin_secrets.SecretsRefused, DeploymentRefused):
                raise
            except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                code = re.search(r'An error occurred \(([A-Za-z][A-Za-z0-9]*)\) when calling ', str(error))
                reason = code[1] if code else type(error).__name__
                print(f'{region}: G and VT Spot quota unknown: {reason}')
            zones = [ledger.text(row, 'ZoneName') for row in ledger.rows(target.call('describe-availability-zones',
                '--filters', 'Name=state,Values=available'), 'AvailabilityZones')]
            prices = fetch_spot_prices(target, sizes, int(self.clock()))
            latest = {(row.instance_type, row.zone): row.usd_per_hour for row in prices}
            for size in sizes:
                for zone in sorted(zones):
                    print(region, size, zone, str(latest.get((size, zone), 'unavailable')))

    def quota_request(self) -> bool:
        """Request both regional Spot quotas without duplicating open requests."""
        settings = read_deployment_settings(self.settings_path)
        api = self._api(settings)
        success = True
        for regions, code, desired, label in quota_targets(settings.region):
            for region in regions:
                if not request_spot_quota(api.in_region(region), code, desired, label):
                    success = False
        return success

    def quota_status(self) -> bool:
        """Read regional Spot quotas with the plan identity."""
        from scripts.aws_target import gpu_size_trial
        plan = plan_run.PlanRun(plan_identity.plan_credentials, self.execute, self.pause)
        _ = plan.credential
        settings = read_deployment_settings(self.settings_path)
        api = plan.api(settings.region, settings.timeout_s)
        needed = gpu_size_trial.required_vcpus(gpu_size_trial.settings())
        now = self.clock()
        regional: dict[str, list[tuple[str, str, int]]] = {}
        for regions, code, desired, label in quota_targets(settings.region):
            for region in dict.fromkeys(regions):
                regional.setdefault(region, []).append((code, 'Standard Spot' if label == 'Spot' else label, desired))
        success = True
        ready: list[str] = []
        opened: list[str] = []
        unknown: list[str] = []
        unknown_quota: list[str] = []
        lines: list[str] = []
        for region, quotas in regional.items():
            target = api.in_region(region)
            lines.append(region)
            for code, name, desired in quotas:
                applied: Decimal | None = None
                try:
                    applied = spot_quota(target, code)
                    lines.append(f'  {name}: {applied.normalize():f} vCPUs applied, {desired} vCPUs needed')
                    if code == 'L-3819A6DF' and applied >= needed:
                        ready.append(region)
                    history = target.call('list-requested-service-quota-change-history-by-quota',
                        '--service-code', 'ec2', '--quota-code', code, service='service-quotas')
                    requests: list[_QuotaRequest] = []
                    for request in ledger.rows(history, 'RequestedQuotas'):
                        created_value = request.get('Created')
                        created = quota_timestamp(request.get('LastUpdated') if created_value is None else created_value)
                        asked = conf.decimal(str(request['DesiredValue'])).normalize()
                        status = _quota_request_status(ledger.text(request, 'Status'))
                        requests.append(_QuotaRequest(created, asked, status))
                    requests.sort(key=lambda request: request.created_at, reverse=True)
                    if requests:
                        newest = requests[0]
                        age = _quota_request_age(newest.created_at, now)
                        lines.append(f'    Newest request: {newest.status.value} for {newest.requested_vcpus:f} vCPUs ({age}).')
                    else:
                        lines.append('    No requests.')
                    for item in requests:
                        if item.status.value not in CLOSED_QUOTA_REQUEST_STATUSES:
                            opened.append(f'{region} {name} {item.requested_vcpus:f} vCPUs ({item.status.value})')
                except (KeyError, OSError, OverflowError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                    code_text = aws_error_code(error) if isinstance(error, AwsUnavailable) else ''
                    if code_text in QUOTA_PLAN_AUTH_FAILURES:
                        raise plan_identity.PlanIdentityRefused(
                            'AWS rejected the plan key. Run deploy.py plan-identity --check.') from None
                    reason = code_text or type(error).__name__
                    if applied is None:
                        lines.append(f'  {name}: applied quota unknown, {desired} vCPUs needed')
                        if code == 'L-3819A6DF':
                            unknown_quota.append(region)
                    lines.append('    Quota status failed: ' + reason)
                    unknown.append(region + ' ' + name)
                    success = False
            lines.append('')
        fits = ', '.join(ready)
        if unknown_quota:
            fits += (', ' if fits else '') + 'unknown for ' + ', '.join(unknown_quota)
        elif not fits:
            fits = 'no region has enough applied quota'
        requests_text = 'unknown' if unknown else ', '.join(opened) or 'none'
        incomplete = '; request history unavailable: ' + ', '.join(unknown) if unknown else ''
        lines.append(f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; '
                     + 'open requests: ' + requests_text + incomplete)
        print('\n'.join(lines), flush=True)
        return success

    def gpu_size_trial(self, folder: Path, *, approve_resources: bool = False) -> bool:
        """Check terminal approval and start the scaler service."""
        from scripts.aws_target import cloud_gate, gpu_size_trial
        self._authorize(approve_resources)
        limits = gpu_size_trial.settings()
        timeout = (limits.lifetime_minutes * 60 * (len(limits.spot_max_usd_per_hour) + 2)
                   + cloud_gate.settings().number('termination_timeout_s') * len(limits.spot_max_usd_per_hour))
        def execute(arguments: Sequence[str]) -> int:
            return self.execute(arguments, capture_output=False, text=True, timeout=timeout, check=False).returncode
        return gpu_size_trial.start_service(folder, self.directory.resolve(),
                                            self.settings_path.resolve(), execute) == 0

    def gate_plan(self) -> None:
        """Preview the approved gate update in configured region order."""
        if not isinstance(self.mode, plan_run.PlanRun):
            raise DeploymentRefused('The gate plan requires the plan identity.')
        with self._require_account_stack('up --workload merge-gates --plan'):
            self._gate_plan_locked(self.mode)

    def _gate_plan_locked(self, plan: plan_run.PlanRun) -> None:
        from scripts.aws_target import cloud_gate, gpu_size_trial

        settings = read_deployment_settings(self.settings_path)
        values = cloud_gate.settings()
        regions = gate_regions(settings.region, values)
        api = plan.api(settings.region, settings.timeout_s)
        failures: dict[str, str] = {}
        results: dict[str, str] = {}
        networks: dict[str, RegionalNetwork] = {}
        try:
            self._check_credentials()
            plan.confirm_account(api.region, api.timeout_s)
            owner = plan.credential.owner
            saved = deployment_record(self.directory)
            stack = plan.gate_stack(api, settings.stack_name)
            status = ledger.text(stack, 'StackStatus')
            if status == 'REVIEW_IN_PROGRESS':
                raise plan_run.unsupported('the home gate stack is in REVIEW_IN_PROGRESS')
            stack_id = ledger.text(stack, 'StackId')
            if (saved.get('stack') != settings.stack_name or saved.get('region') != settings.region
                    or saved.get('account_id') != owner.account or saved.get('stack_id') != stack_id):
                raise DeploymentRefused('The home gate stack differs from the saved deployment identity.')
            outputs = self._stack_outputs(stack, require_outputs=False)
            deployment = ledger.text(saved, 'deployment_id')
            live_parameters = {ledger.text(row, 'ParameterKey'): str(row.get('ParameterValue', ''))
                               for row in ledger.rows(stack, 'Parameters')}
            if (not re.fullmatch(r'[a-f0-9]{32}', deployment)
                    or live_parameters.get('DeploymentId') != deployment
                    or live_parameters.get('VpcId') != settings.vpc_id
                    or live_parameters.get('ScalerUserName') != settings.scaler_user_name
                    or outputs.get('ScalerUserName') != settings.scaler_user_name):
                raise DeploymentRefused('The home gate deployment parameters differ from the saved identity.')
            scaler_api = self._gate_scaler_api(settings, values.number('api_timeout_s'))
            _ = gpu_size_trial.gpu_signing_key(scaler_api)
            scaler = scaler_api.call('get-caller-identity', service='sts')
            principal = owner.iam('user/' + settings.scaler_user_name)
            local_scaler = ledger.mapping(saved.get('scaler_user', {}))
            if (scaler.get('Account') != owner.account or scaler.get('Arn') != principal
                    or local_scaler.get('name') != settings.scaler_user_name or local_scaler.get('arn') != principal
                    or local_scaler.get('user_id') != ledger.text(scaler, 'UserId')):
                raise DeploymentRefused('STS did not confirm the saved gate scaler user.')
            reply = api.call('get-template', '--stack-name', stack_id, '--template-stage', 'Original',
                             service='cloudformation')
            body = reply['TemplateBody']
            live = ledger.mapping(cast(object, json.loads(body)) if isinstance(body, str) else body)
            live_resources = ledger.mapping(live['Resources'])
            source = TEMPLATE.read_bytes()
            template = ledger.mapping(cast(object, json.loads(source)))
            definitions = ledger.mapping(template['Parameters'])
            spending = read_settings(self.settings_path)
            gpu_types = sorted(gpu_size_trial.settings().spot_max_usd_per_hour.keys()
                               | set(values.words('hana_gpu_instance_types')))
            wait_s = conf.read(self.settings_path).whole('stack_wait_timeout_s')
            print('plan guard: home gate stack and scaler credentials passed', flush=True)
            images = ledger.rows(api.call('describe-images', '--image-ids', settings.ami_id), 'Images')
            if len(images) != 1 or images[0].get('ImageId') != settings.ami_id:
                raise DeploymentRefused('EC2 did not return the configured home gate image.')
            home_image = images[0]
        except plan_run.PLAN_ERRORS as error:
            for region in regions:
                print(f'plan gate: {region}: refused: {plan_run.safe_reason(error)}', flush=True)
            raise DeploymentRefused('one or more gate regions failed') from None

        for region in regions:
            try:
                if region == settings.region:
                    networks[region] = RegionalNetwork(region, settings.vpc_id, gate_subnets(api, settings.vpc_id),
                                                       settings.ami_id)
                else:
                    networks[region] = self.regional_network(api, settings, region, home_image=home_image)
            except plan_run.PLAN_ERRORS as error:
                failures[region] = plan_run.safe_reason(error)

        try:
            parameters = gate_parameters(settings, values, owner.partition, owner.account,
                                         [networks[region] for region in regions[1:] if region in networks], gpu_types)
            proposed = live_parameters | parameters
            regional_template = region_gate_template()
            regional_source = json.dumps(regional_template, sort_keys=True).encode()
            regional_definitions = ledger.mapping(regional_template['Parameters'])
            if failures and settings.region not in failures:
                failures[settings.region] = 'the home gate policy needs the image and VPC of every gate region'
            documents, shared = plan.gate_policy_documents(api.region, settings.scaler_user_name, template, proposed)
            for key, value in (('GateBucket', shared.bucket), ('GateProfileArn', shared.profile), ('GateRoleArn', shared.role)):
                if key in outputs and outputs[key] != value:
                    raise DeploymentRefused('The home gate outputs differ from the proposed resource identities.')

        except plan_run.PLAN_ERRORS as error:
            for region in regions:
                reason = failures.get(region, plan_run.safe_reason(error))
                print(f'plan gate: {region}: refused: {reason}', flush=True)
            raise DeploymentRefused('one or more gate regions failed') from None

        home_policy_ids = tuple(logical_id for logical_id in plan_run.GATE_SCALER_POLICY_IDS
                                if live_parameters.get('EnableGates') == 'true' and logical_id in live_resources)
        displaced = {settings.region: plan_run.GateDisplacedPolicies(stack_id, home_policy_ids)}
        covered_regions: set[str] = set()
        pending_probes: dict[str, plan_run.GateLaunchProbe] = {}
        interrupted = False
        for region in regions:
            if region in failures:
                continue
            try:
                target = api.in_region(region)
                network = networks[region]
                if region == settings.region:
                    changes = gate_update_values(stack, parameters, definitions)
                    with plan.gate_update(target, stack_id, source, changes, wait_s) as preview:
                        displaced[region] = plan_run.GateDisplacedPolicies(
                            stack_id, home_policy_ids + preview.displaced_policy_ids)
                        change_kind = preview.summary
                    regional_outputs = outputs
                else:
                    name = settings.stack_name + '-gates-' + region
                    regional = plan.gate_stack(target, name)
                    if regional.get('StackStatus') == 'ROLLBACK_COMPLETE':
                        change_kind = 'permissions passed; approved setup must recreate the rolled-back stack'
                        regional_outputs = {}
                    else:
                        regional_outputs = self._stack_outputs(regional, require_outputs=False)
                        if regional.get('StackStatus') == 'REVIEW_IN_PROGRESS':
                            raise plan_run.unsupported('the regional gate stack is in REVIEW_IN_PROGRESS')
                        regional_parameters = self._regional_gate_values(settings, parameters, network, deployment,
                                                                         shared.profile)
                        changes = gate_update_values(regional, {key: value for key, value in regional_parameters.items()
                                                                if key in regional_definitions}, regional_definitions)
                        regional_id = ledger.text(regional, 'StackId')
                        with plan.gate_update(target, regional_id, regional_source, changes, wait_s) as preview:
                            displaced[region] = plan_run.GateDisplacedPolicies(regional_id, preview.displaced_policy_ids)
                            change_kind = preview.summary

                pending_probes[region] = plan_run.GateLaunchProbe(deployment, network.image, network.vpc,
                    ledger.text(network.subnets[0], 'id'), regional_outputs.get('GateLaunchTemplate', 'lt-00000000000000000'),
                    regional_outputs.get('GateSecurityGroup', 'sg-00000000000000000'), tuple(gpu_types),
                    {'GateAttempt': 'setup-probe', 'GpuSizeTrial': 'setup-probe', 'ExpiresAt': '0',
                     spending.cost_explorer_tag_key: spending.cost_explorer_tag_value},
                    gpu_size_trial.GPU_TRIAL_ARTIFACT_PREFIX + 'setup-probe.json',
                    gpu_size_trial.GPU_TRIAL_REQUEST_PREFIX + 'setup-probe.json')
                results[region] = change_kind
                covered_regions.add(region)
            except plan_run.GateStackAbsent as error:
                covered_regions.add(region)
                failures[region] = plan_run.safe_reason(error)
            except KeyboardInterrupt:
                covered_regions.discard(region)
                failures[region] = 'The gate preview was interrupted.'
                interrupted = True
                break
            except plan_run.PLAN_ERRORS as error:
                failures[region] = plan_run.safe_reason(error)

        try:
            exclusions = plan.gate_policy_exclusions(api, regions, covered_regions, displaced)
        except plan_run.PlanRefused as error:
            for region in regions:
                _ = failures.setdefault(region, plan_run.safe_reason(error))
        else:
            for region, launch in pending_probes.items():
                try:
                    plan.probe_gate_scaler(api.in_region(region), principal, exclusions, documents, shared, launch,
                                           home=region == settings.region)
                except plan_run.PLAN_ERRORS as error:
                    failures[region] = plan_run.safe_reason(error)

        for region in regions:
            result = f'refused: {failures[region]}' if region in failures else f'pass ({results[region]})'
            print(f'plan gate: {region}: {result}', flush=True)
        if interrupted:
            raise KeyboardInterrupt
        if failures:
            raise DeploymentRefused('one or more gate regions failed')

    def gate_up(self, *, approve_resources: bool = False) -> bool:
        """Add the gate resources and preserve every Rust parameter."""
        from scripts.aws_target import aws_trial, cloud_gate, gpu_size_trial
        self._authorize(approve_resources)
        with self._require_account_stack('up --workload merge-gates'):
            settings = read_deployment_settings(self.settings_path)
            regions = gate_regions(settings.region)
            api = self._api(settings)
            saved = deployment_record(self.directory)
            stack_id = ledger.text(saved, 'stack_id')
            _ = self._stack(settings)
            stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                       service='cloudformation'), 'Stacks')[0]
            subnets = gate_subnets(api, settings.vpc_id)
            revision = aws_trial.host_nixpkgs_revision()
            account = ledger.text(api.call('get-caller-identity', service='sts'), 'Account')
            partition = stack_id.split(':')[1]
            values = cloud_gate.settings()
            # The home scaler policies cover every gate region, so the scaler user's policy count stays fixed.
            timings = GateRegionTimings.read(self.directory)
            additional = regions[1:]
            timings.announce(GateRegionOperation.NETWORK, additional)
            networks: list[RegionalNetwork] = []
            for position, region in enumerate(additional, 1):
                with timings.measure(GateRegionOperation.NETWORK, region, position, len(additional), self.settings_path):
                    networks.append(self.regional_network(api, settings, region))
            comparison_caps = gpu_size_trial.settings().spot_max_usd_per_hour
            gpu_types = sorted(comparison_caps.keys() | set(values.words('hana_gpu_instance_types')))
            gpu_caps = comparison_caps | gpu_size_trial.price_caps(values.text('hana_gpu_spot_max_usd_per_hour'))
            parameters = gate_parameters(settings, values, partition, account, networks, gpu_types)
            book = ledger.CostLedger(self.directory)
            inventory = book.read_snapshot().inventories
            resources = tuple(resource for resource in inventory[-1].resources if resource.name != 'gate-shared') if inventory else ()
            book.record_inventory(ledger.ResourceInventory(int(self.clock()), resources + (
                ledger.PricedResource('gate-shared', money(str(values.number('shared_usd_per_month')))),), 'gate deployment',
                builder_removed=bool(inventory and inventory[-1].builder_removed)))
            home_template = TEMPLATE.read_bytes()
            known = ledger.mapping(ledger.mapping(cast(object, json.loads(home_template)))['Parameters'])
            changes = gate_update_values(stack, parameters, known)
            with tempfile.TemporaryDirectory(prefix='gate-home-') as folder:
                template = Path(folder) / 'template.json'
                _ = template.write_bytes(home_template)
                with step('up', 'updating the stack', self.settings_path, hint='can take several minutes'):
                    self._gate_change(api, stack_id, template, changes)
            outputs = self._stack(settings)
            deployment_id = ledger.text(saved, 'deployment_id')
            home: dict[str, object] = {'region': settings.region, 'image': settings.ami_id, 'subnets': subnets,
                'template': outputs['GateLaunchTemplate'], 'version': outputs['GateLaunchVersion'],
                'security_group': outputs['GateSecurityGroup']}
            manifest: dict[str, object] = {
                'deployment_id': deployment_id, 'revision': revision,
                'instance_types': values.words('instance_types'),
                'gpu_instance_types': gpu_types,
                'bucket': outputs['GateBucket'], 'artifact_prefix': 'artifacts/' + outputs['GateRoleId'] + ':',
                'regions': [home],
            }
            cloud_gate.save(self.directory / 'gate-deployment.json', manifest)
            regional = list(ledger.rows(manifest, 'regions'))
            timings.announce(GateRegionOperation.STACK, additional)
            digests = RegionalStackDigests.read(self.directory)
            for position, network in enumerate(networks, 1):
                with timings.measure(GateRegionOperation.STACK, network.region, position, len(networks), self.settings_path):
                    regional.append(self.regional_gate_stack(api, settings, outputs, parameters, network, digests))
                    manifest['regions'] = regional
                    cloud_gate.save(self.directory / 'gate-deployment.json', manifest)
            print('cloud-gate: deployed')
            self._fill_prices()
            scaler_arn = f"arn:{partition}:iam::{account}:user/{outputs['ScalerUserName']}"
            return self._probe_gate_gpu(settings, values, manifest, gpu_caps, admin_api=api,
                                        scaler_arn=scaler_arn, partition=partition, account_id=account)

    def _gate_scaler_api(self, settings: DeploymentSettings, timeout: float) -> AwsCli:
        # Use the GPU worker's profile and credential file without inherited admin credentials.
        environment = {key: value for key, value in os.environ.items() if not key.startswith('AWS_')}
        environment.update(AWS_SHARED_CREDENTIALS_FILE=str(admin_secrets.credentials_file()),
                           AWS_EC2_METADATA_DISABLED='true')

        def scaler(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                   check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = env
            return self.execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check,
                                env=environment)

        account = conf.read(self.settings_path)
        return AwsCli(account.text('credentials_profile'), settings.region, timeout, scaler)

    def _probe_gate_gpu(self, settings: DeploymentSettings, values: conf.ConfigRead, manifest: dict[str, object],
                        caps: dict[str, Decimal], *, admin_api: AwsCli, scaler_arn: str,
                        partition: str, account_id: str) -> bool:
        from scripts.aws_target import gpu_size_trial

        api = self._gate_scaler_api(settings, values.number('api_timeout_s'))
        regions = ledger.rows(manifest, 'regions')
        home = regions[0]
        candidates = set(values.words('spot_candidate_regions'))
        subnets = ledger.rows(home, 'subnets')
        types = gpu_size_trial.string_list(manifest['gpu_instance_types'])
        bucket = ledger.text(manifest, 'bucket')
        deployment_id = ledger.text(manifest, 'deployment_id')
        now = int(self.clock())
        started = datetime.fromtimestamp(now, UTC).isoformat()
        utc_day = now - now % 86400
        nonce = uuid4().hex
        key = gpu_size_trial.GPU_TRIAL_ARTIFACT_PREFIX + 'setup-probe.json'
        input_key = gpu_size_trial.GPU_TRIAL_REQUEST_PREFIX + 'setup-probe.json'
        content = json.dumps({'probe': nonce}).encode()
        spending = read_settings(self.settings_path)
        termination_targets: list[str] = []
        offered_zones: dict[str, set[str]] = {}
        label = 'gate-setup probe'
        retry_interval = values.whole('gpu_setup_dry_run_retry_interval_s')
        simulation_access_denied = False

        def verify_scaler_identity(api: AwsCli) -> None:
            reply = api.call('get-caller-identity', service='sts')
            if reply.get('Arn') != scaler_arn:
                raise ScalerIdentityMismatch

        def describe_instances(api: AwsCli) -> None:
            reply = api.call('describe-instances', '--filters', *gpu_size_trial.GPU_SPOT_FILTERS)
            if reply.get('NextToken'):
                raise ValueError('The GPU Spot instance list is incomplete')
            for reservation in ledger.rows(reply, 'Reservations'):
                for instance in ledger.rows(reservation, 'Instances'):
                    tags = {row['Key']: row['Value'] for row in
                            ledger.rows({'Tags': instance.get('Tags', [])}, 'Tags')}
                    if (tags.get('GateDeployment') == deployment_id and tags.get('Workload') == 'cloud-claude-gate'
                            and tags.get('GpuSizeTrial')):
                        termination_targets.append(ledger.text(instance, 'InstanceId'))

        def describe_offerings(api: AwsCli) -> None:
            reply = api.call('describe-instance-type-offerings', '--location-type', 'availability-zone',
                             '--filters', 'Name=instance-type,Values=' + ','.join(types))
            for row in ledger.rows(reply, 'InstanceTypeOfferings'):
                offered_zones.setdefault(ledger.text(row, 'InstanceType'), set()).add(ledger.text(row, 'Location'))

        def launch_probe(api: AwsCli, size: str, deadline: float) -> ScalerProbePassed | ScalerProbeUnverified:
            zones = offered_zones.get(size, set())
            subnet = next((row for row in subnets if ledger.text(row, 'zone') in zones), None)
            if subnet is None:
                return ScalerProbeUnverified('No saved subnet offers this type.')
            launch = gpu_size_trial.GpuSpotLaunch(deployment_id, nonce, f'{nonce}-{size}', size, caps[size],
                now + 60 * values.whole('hana_gpu_lifetime_minutes'), values.whole('gpu_size_trial_root_gib'),
                base64.b64encode(b'#!/bin/sh\nexit 0\n').decode())
            arguments = json.dumps(launch.arguments(home, subnet, settings.root_device_name, spending))
            remaining = deadline - time.monotonic()
            timeout = min(api.timeout_s, remaining) if remaining > 0 else api.timeout_s
            while True:
                try:
                    api.ec2_dry_run('run-instances', '--cli-input-json', arguments, timeout_s=timeout)
                    return ScalerProbePassed()
                except AwsUnavailable as error:
                    code = aws_error_code(error, operation='RunInstances', allow_status=True)
                    if code not in PROBE_DENIED_CODES or deadline - time.monotonic() <= retry_interval:
                        raise
                    print(f'{label}: run-instances {size}: {code}. Retry in {retry_interval} s.', flush=True)
                    self.pause(retry_interval)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    timeout = min(api.timeout_s, remaining)

        def simulate_termination(region: str) -> ScalerProbePassed | ScalerProbeUnverified:
            nonlocal simulation_access_denied
            action = 'ec2:TerminateInstances'
            instance_arn = f'arn:{partition}:ec2:{region}:{account_id}:instance/i-00000000000000000'
            context = [{'ContextKeyName': key, 'ContextKeyType': 'string', 'ContextKeyValues': [value]}
                       for key, value in (('ec2:ResourceTag/GateDeployment', deployment_id),
                                          ('ec2:ResourceTag/Workload', 'cloud-claude-gate'),
                                          ('aws:RequestedRegion', region))]
            try:
                reply = admin_api.call('simulate-principal-policy', '--policy-source-arn', scaler_arn,
                    '--action-names', action, '--resource-arns', instance_arn,
                    '--context-entries', json.dumps(context), '--no-paginate', service='iam')
            except AwsUnavailable as error:
                if aws_error_code(error, operation='SimulatePrincipalPolicy', allow_status=True) not in PROBE_DENIED_CODES:
                    raise
                simulation_access_denied = True
                return ScalerProbeUnverified()
            incomplete = 'The termination simulation result is incomplete'
            results = ledger.rows(reply, 'EvaluationResults')
            if reply.get('IsTruncated') or len(results) != 1 or results[0].get('EvalActionName') != action:
                raise ValueError(incomplete)
            result = results[0]
            decision_key = 'EvalDecision'
            if 'ResourceSpecificResults' in result:
                matches = [row for row in ledger.rows(result, 'ResourceSpecificResults')
                           if row.get('EvalResourceName') == instance_arn]
                if len(matches) != 1:
                    raise ValueError(incomplete)
                result = matches[0]
                decision_key = 'EvalResourceDecision'
            elif result.get('EvalResourceName') != instance_arn:
                raise ValueError(incomplete)
            decision = result.get(decision_key)
            if decision in ('implicitDeny', 'explicitDeny'):
                raise TerminationGrantDenied('The simulator denied termination for the scaler')
            if decision != 'allowed' or result.get('MissingContextValues'):
                raise ValueError(incomplete)
            return ScalerProbePassed()

        def list_result_probe(api: AwsCli) -> None:
            if not gpu_size_trial.gpu_object_exists(api, bucket, key):
                raise ValueError('The probe object does not appear in the list')

        def probe_multipart(api: AwsCli) -> None:
            location = ('--bucket', bucket, '--key', input_key)
            reply = api.call('create-multipart-upload', *location, service='s3api')
            _ = api.call('abort-multipart-upload', *location, '--upload-id', ledger.text(reply, 'UploadId'), service='s3api')

        def probes() -> Iterable[ScalerProbe]:
            yield ScalerProbe('describe-instances', describe_instances)
            yield ScalerProbe('describe-instance-type-offerings', describe_offerings)
            yield ScalerProbe('describe-spot-price-history', lambda api: api.call('describe-spot-price-history',
                '--instance-types', *types, '--product-descriptions', 'Linux/UNIX', '--start-time', started))
            yield ScalerProbe('get-service-quota', lambda api: spot_quota(api, 'L-3819A6DF'))
            yield ScalerProbe('get-cost-and-usage', lambda api: fetch_account_charges(
                api, utc_day - 86400, utc_day, now, spending.cost_explorer_read_usd))
            deadline = time.monotonic() + values.whole('gpu_setup_dry_run_retry_timeout_s')
            for size in types:
                yield ScalerProbe(f'run-instances {size}',
                                  lambda api, size=size: launch_probe(api, size, deadline), 'DryRunOperation')
            if termination_targets:
                yield ScalerProbe('terminate-instances', lambda api: api.ec2_dry_run(
                    'terminate-instances', '--instance-ids', termination_targets[0]), 'DryRunOperation')
            for row in regions:
                region = ledger.text(row, 'region')
                if region in candidates and row.get('subnets'):
                    yield ScalerProbe(f'termination {region}', lambda _api, region=region: simulate_termination(region),
                                      'IAM simulation')
            yield ScalerProbe('export-credentials', gpu_size_trial.gpu_signing_key)
            yield ScalerProbe('put-object', lambda api: put_probe_object(api, bucket, key, content))
            yield ScalerProbe('list-objects-v2', list_result_probe)
            yield ScalerProbe('get-object', lambda api: get_probe_object(api, bucket, key, content))
            yield ScalerProbe('put-object inputs', lambda api: put_probe_object(api, bucket, input_key, content))
            yield ScalerProbe('get-object inputs', lambda api: get_probe_object(api, bucket, input_key, content))
            yield ScalerProbe('create/abort-multipart-upload', probe_multipart)

        failed = run_scaler_probes(label, api, (ScalerProbe('get-caller-identity', verify_scaler_identity),),
                                   show_codes=True)
        if failed and failed[0].code == ScalerIdentityMismatch.__name__:
            return False
        if not failed:
            failed = run_scaler_probes(label, api, probes(), show_codes=True)
        if simulation_access_denied:
            print('gate-setup: The administrator cannot call iam:SimulatePrincipalPolicy.', flush=True)
        for next_step in dict.fromkeys(failure.next_step() for failure in failed):
            print(f'gate-setup: {next_step}', flush=True)
        return not failed

    def _fill_prices(self) -> None:
        """Read Spot prices and quotas once the stack allows it; the hourly refresh continues."""
        configuration = conf.read(self.settings_path)
        attempts = configuration.whole('deploy_price_fill_attempts')
        for attempt in range(1, attempts + 1):
            try:
                # The user sees the reasons here; the hourly refresh sends the alert if they remain.
                status = read_price_snapshot(self.settings_path, self.directory, execute=self.execute,
                                             clock=self.clock, notify=lambda _key, _push, _timeout: False, force=True)
            except (OSError, ValueError) as error:
                print(f'cloud-gate: the Spot price read did not run: {type(error).__name__}: '
                      + admin_secrets.redact(str(error)), flush=True)
                break
            if status == 0:
                print('cloud-gate: Spot prices and quotas read', flush=True)
                return
            if attempt < attempts:
                self.pause(configuration.number('deploy_price_fill_pause_s'))
        print('cloud-gate: deployed, but the Spot price read failed for the regions above. '
              + 'The hourly refresh tries again.', flush=True)

    def down(self, *, approve_destroy: bool = False) -> bool:
        if not approve_destroy:
            print(teardown.DRY_RUN)
            return True
        self._check_credentials()
        settings = read_deployment_settings(self.settings_path)
        with (self._require_account_stack('down'), self._require_hana_control('down'),
              step('down', 'removing local and cloud resources', self.settings_path)):
            return teardown.TargetRemoval(self.settings_path, self.directory, self.output, self.admin,
                self._api(settings), self.execute, tailnet=box.tailnet_request, clock=self.clock).run()


def gate_parameters(settings: DeploymentSettings, values: conf.ConfigRead, partition: str, account: str,
                    networks: list[RegionalNetwork], gpu_types: list[str]) -> dict[str, str]:
    return {
        'EnableGates': 'true', 'GateImage': settings.ami_id,
        'GateVpcs': ','.join([f'arn:{partition}:ec2:{settings.region}:{account}:vpc/{settings.vpc_id}',
                              *(f'arn:{partition}:ec2:{network.region}:{account}:vpc/{network.vpc}' for network in networks)]),
        'GateRegionImages': ','.join(f'arn:{partition}:ec2:{network.region}::image/{network.image}' for network in networks),
        'GateInstanceTypes': ','.join(values.words('instance_types')),
        'GateGpuInstanceTypes': ','.join(gpu_types),
        'GateRootGiB': str(values.whole('root_gib')),
        'GateSpotMaxPrice': str(values.number('spot_max_usd_per_hour')),
        'GateArtifactDays': str(values.whole('artifact_days')), 'GateLogDays': str(values.whole('log_days')),
        'GateExpiryMinutes': str(values.whole('expiry_schedule_minutes')),
        'GateExpiryTimeout': str(values.whole('expiry_timeout_s')),
        'GateExpiryMemory': str(values.whole('expiry_memory_mib')),
    }


def gate_update_values(stack: dict[str, object], parameters: dict[str, str],
                       definitions: dict[str, object]) -> list[dict[str, object]]:
    # Preserve the values of parameters that the template still uses.
    changes: list[dict[str, object]] = [
        {'ParameterKey': key, 'UsePreviousValue': True}
        for row in ledger.rows(stack, 'Parameters')
        if (key := ledger.text(row, 'ParameterKey')) not in parameters and key in definitions
    ]
    changes.extend({'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items())
    return changes


def region_gate_template() -> dict[str, object]:
    """Reuse the home gate bodies without regional copies of global resources."""
    source = ledger.mapping(cast(object, json.loads(TEMPLATE.read_text())))
    # No scaler policy: the scaler user may hold 10 managed policies, and the budget block needs one of them.
    names = ('GateSecurityGroup', 'GateLaunchTemplate', 'GateExpiryRole',
             'GateExpiryFunction', 'GateExpiryLog', 'GateExpirySchedule', 'GateExpiryPermission')
    resources = ledger.mapping(source['Resources'])
    def rewrite(value: object) -> object:
        if isinstance(value, str):
            # IAM role, Lambda and schedule names allow 64 characters; the shorter expiry prefix fits a 19-character region.
            return (value.replace('cloud-claude-gate-${DeploymentId}-expiry', 'gate-${DeploymentId}-${AWS::Region}-expiry')
                    .replace('cloud-claude-gate-${DeploymentId}', 'cloud-gate-${DeploymentId}-${AWS::Region}'))
        if isinstance(value, list):
            return [rewrite(item) for item in cast(list[object], value)]
        if isinstance(value, dict):
            row = ledger.mapping(cast(object, value))
            if row.get('Fn::GetAtt') == ['GateProfile', 'Arn']:
                return {'Ref': 'HomeGateProfileArn'}
            if row.get('Fn::GetAtt') == ['GateRole', 'Arn']:
                return {'Ref': 'HomeGateRoleArn'}
            if row.get('Ref') == 'ScalerUser':
                return {'Ref': 'ScalerUserName'}
            return {key: rewrite(item) for key, item in row.items() if not (key == 'Condition' and item == 'Gates')}
        return value
    regional = {name: rewrite(resources[name]) for name in names}
    outputs = {key: rewrite(value) for key, value in ledger.mapping(source['Outputs']).items()
               if key in ('GateLaunchTemplate', 'GateLaunchVersion', 'GateSecurityGroup')}
    parameters = ledger.mapping(source['Parameters'])
    used = referenced_names([regional, outputs, ledger.mapping(source['Conditions'])['GateEveryMinute']])
    definitions: dict[str, object] = {key: parameters[key] for key in parameters if key in used}
    definitions.update({key: {'Type': 'String'} for key in ('HomeGateProfileArn', 'HomeGateRoleArn', 'ScalerUserName') if key in used})
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Parameters': definitions,
            'Conditions': {'GateEveryMinute': ledger.mapping(source['Conditions'])['GateEveryMinute']},
            'Resources': regional, 'Outputs': outputs}


def referenced_names(value: object) -> set[str]:
    """The names that Ref and Fn::Sub in a template part refer to."""
    if isinstance(value, str):
        return {found.group(1) for found in re.finditer(r'\$\{([A-Za-z0-9]+)', value)}
    if isinstance(value, list):
        return set[str]().union(*(referenced_names(item) for item in cast(list[object], value)))
    if isinstance(value, dict):
        row = ledger.mapping(cast(object, value))
        reference = row.get('Ref')
        names = {reference} if isinstance(reference, str) else set[str]()
        return names.union(*(referenced_names(item) for item in row.values()))
    return set[str]()


def failed_stack_events(api: AwsCli, stack: str, started: float) -> list[str]:
    """One line for each resource event of the stack that failed at or after started."""
    try:
        events = api.call('describe-stack-events', '--stack-name', stack, service='cloudformation')
        lines: list[str] = []
        for event in ledger.rows(events, 'StackEvents'):
            stamp = datetime.fromisoformat(ledger.text(event, 'Timestamp').replace('Z', '+00:00')).timestamp()
            status = ledger.text(event, 'ResourceStatus')
            if stamp >= started and status.endswith('_FAILED'):
                lines.append(f"{ledger.text(event, 'LogicalResourceId')} ({ledger.text(event, 'ResourceType')}): "
                             + f"{status}: {event.get('ResourceStatusReason', '')}")
        return lines
    except (ValueError, OSError, AwsUnavailable, subprocess.SubprocessError):
        return ['stack events are unavailable']


def describe_regional_stack(api: AwsCli, stack: str) -> dict[str, object]:
    """Read the stack status and outputs. Return an empty record for a missing stack."""
    try:
        stacks = ledger.rows(api.call('describe-stacks', '--stack-name', stack, service='cloudformation'), 'Stacks')
    except AwsUnavailable as error:
        if 'does not exist' in str(error):
            return {}
        raise
    return stacks[0] if stacks else {}


def gate_subnets(api: AwsCli, vpc: str) -> list[dict[str, object]]:
    """Choose one public subnet in each available zone."""
    tables = ledger.rows(api.call('describe-route-tables', '--filters', 'Name=vpc-id,Values=' + vpc), 'RouteTables')
    main: dict[str, object] = {'Routes': []}
    assigned: dict[str, dict[str, object]] = {}
    for table in tables:
        for association in ledger.rows(table, 'Associations'):
            if association.get('Main') is True:
                main = table
            if association.get('SubnetId'):
                assigned[ledger.text(association, 'SubnetId')] = table
    zones: dict[str, dict[str, object]] = {}
    reply = api.call('describe-subnets', '--filters', 'Name=vpc-id,Values=' + vpc)
    for subnet in sorted(ledger.rows(reply, 'Subnets'), key=lambda row: ledger.text(row, 'SubnetId')):
        table = assigned.get(ledger.text(subnet, 'SubnetId'), main)
        public = any(route.get('DestinationCidrBlock') == '0.0.0.0/0' and route.get('State') == 'active'
                     and str(route.get('GatewayId', '')).startswith('igw-') for route in ledger.rows(table, 'Routes'))
        if public and subnet.get('State') == 'available':
            zone = ledger.text(subnet, 'AvailabilityZone')
            _ = zones.setdefault(zone, {'zone': zone, 'id': ledger.text(subnet, 'SubnetId')})
    if not zones:
        raise DeploymentRefused('The gate VPC has no available public subnet')
    return [zones[zone] for zone in sorted(zones)]


class DeploymentArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.workload: str = 'rust'
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = target_config.ssh_target_state_directory('aws')
        self.output: Path = Path.home() / '.local/state/aws-build/deployment'
        self.approve_resources: bool = False
        self.approve_destroy: bool = False
        self.plan: bool = False
        self.check: bool = False
        self.trial_action: str | None = None
        self.trial_folder: Path = Path()
        self.image: Path | None = None


def run_setup_plan(args: DeploymentArguments) -> int:
    try:
        workloads = ('rust', 'merge-gates', 'hana') if args.command == 'hana-setup' else ('rust', 'merge-gates')
        if (args.command not in ('up', 'hana-setup')
                or args.workload not in workloads
                or args.command == 'up' and args.workload != 'merge-gates'
                or args.trial_action or args.approve_resources or args.approve_destroy):
            raise DeploymentRefused('Use --plan with hana-setup or up --workload merge-gates without approval flags.')
        plan = plan_run.PlanRun(plan_identity.plan_credentials, execute_command, time.sleep)
        subject = Deployment(args.settings, args.directory, args.output, execute=execute_command, mode=plan)
        if args.command == 'up':
            subject.gate_plan()
        elif not subject.hana_setup():
            raise DeploymentRefused('Setup did not pass every check.')
    except KeyboardInterrupt:
        print('plan run: refused: interrupted', flush=True)
        return 1
    except (KeyError, OSError, TypeError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
        print('plan run: refused: ' + plan_run.safe_reason(error), flush=True)
        return 1
    print('plan run: pass', flush=True)
    return 0


def main() -> int:
    if sys.argv[1:2] == ['aws-trial']:
        from scripts.aws_target import aws_trial
        os.environ['AWS_SHARED_CREDENTIALS_FILE'] = str(admin_secrets.credentials_file())
        settings_path = conf.path('aws_target')
        with step('aws-trial', 'running the trial', settings_path):
            return aws_trial.main(sys.argv[2:], settings_path, target_config.ssh_target_state_directory('aws'))
    parser = argparse.ArgumentParser(description=__doc__, epilog=aws_test_identity.CONSOLE_SETUP)
    _ = parser.add_argument('command', choices=('gpu-size-trial', 'secrets', 'up', 'down', 'remove-images', 'prices', 'quota-request', 'quota-status', 'prepare', 'hana-setup', 'hana-image', 'apply', 'plan-identity', 'test-identity'),
                            help='Use quota-status to read Spot quotas with the plan key. Use quota-request to request increases with the admin key.')
    _ = parser.add_argument('trial_action', nargs='?', choices=('run',))
    _ = parser.add_argument('--trial-folder', type=Path)
    _ = parser.add_argument('--image', type=Path)
    _ = parser.add_argument('--workload', choices=('merge-gates', 'hana', 'plan-identity'), default='merge-gates')
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--output', type=Path)
    _ = parser.add_argument('--approve-resources', action='store_true')
    _ = parser.add_argument('--approve-destroy', action='store_true')
    _ = parser.add_argument('--plan', action='store_true', help='Check setup with the AWS plan identity.')
    _ = parser.add_argument('--check', action='store_true', help='Verify the saved plan or test identity without a prompt.')
    args = parser.parse_args(namespace=DeploymentArguments())
    if args.command == 'test-identity':
        if args.plan:
            parser.error('Use test-identity without --plan.')
        return aws_test_identity.command(args.settings, check=args.check)
    if args.check:
        if args.command != 'plan-identity' or args.plan:
            parser.error('Use --check with plan-identity or test-identity.')
        return plan_identity.command_check(args.settings)
    if args.plan:
        return run_setup_plan(args)
    deploy = Deployment(args.settings, args.directory, args.output)
    try:
        if args.workload == 'plan-identity' and args.command != 'down':
            raise DeploymentRefused('Use --workload plan-identity only with down.')
        if args.command == 'plan-identity' or args.workload == 'plan-identity':
            if args.command == 'plan-identity':
                plan_identity.command_create(args.settings, approve_resources=args.approve_resources)
            else:
                plan_identity.command_down(args.settings, approve_destroy=args.approve_destroy)
            return 0
        if args.command == 'quota-status':
            try:
                return 0 if deploy.quota_status() else 1
            except plan_identity.PlanIdentityRefused as error:
                print('quota-status: ' + plan_run.safe_reason(error), file=sys.stderr)
                return 1
        os.environ['AWS_SHARED_CREDENTIALS_FILE'] = str(admin_secrets.credentials_file())
        if args.command == 'gpu-size-trial':
            if args.trial_action != 'run' or args.trial_folder == Path():
                raise DeploymentRefused('Use gpu-size-trial run --trial-folder DIR --approve-resources')
            return 0 if deploy.gpu_size_trial(args.trial_folder, approve_resources=args.approve_resources) else 1
        if args.command == 'secrets':
            with step('secrets', 'saving encrypted credentials', args.settings):
                admin_secrets.configure(args.settings)
        elif args.command == 'up':
            return 0 if deploy.gate_up(approve_resources=args.approve_resources) else 1
        elif args.command == 'down':
            if args.workload == 'hana':
                return 0 if deploy.hana_down(approve_destroy=args.approve_destroy) else 1
            return 0 if deploy.down(approve_destroy=args.approve_destroy) else 1
        elif args.command == 'remove-images':
            return 0 if deploy.remove_images(approve_destroy=args.approve_destroy) else 1
        elif args.command == 'prices':
            deploy.prices()
        elif args.command == 'quota-request':
            return 0 if deploy.quota_request() else 1
        elif args.command == 'hana-image':
            if args.image is None:
                raise DeploymentRefused('Use hana-image --image RESULT [--approve-resources]')
            return 0 if deploy.hana_image(args.image, approve_resources=args.approve_resources) else 1
        elif args.command == 'hana-setup':
            try:
                return 0 if deploy.hana_setup(approve_resources=args.approve_resources) else 1
            except KeyboardInterrupt:
                print('hana-setup: interrupted', file=sys.stderr)
                return 130
        elif args.command == 'prepare':
            prepared = deploy.prepare_hana() if args.workload == 'hana' else deploy.prepare()
            print(json.dumps({'at': prepared.at, 'fingerprint': prepared.fingerprint, 'output': str(args.output)}))
        else:
            print(json.dumps(deploy.apply(approve_resources=args.approve_resources), sort_keys=True))
        return 0
    except hana_replacement.ReplacementRefused as error:
        print(deploy.admin.redact(str(error)), file=sys.stderr)
        return 1
    except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
        print('aws_deploy: ' + deploy.admin.redact(str(error)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
