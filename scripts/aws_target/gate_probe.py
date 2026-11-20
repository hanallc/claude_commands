"""Save and read the verdicts from the last gate setup probe."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import ClassVar, cast

from scripts.aws_target import ledger, remote
from scripts.lib.atomic_file import sync_folder, write_bytes_atomic

FILE_NAME = 'gate-probe.json'
LABEL = 'gate-setup probe'
_VERSION = 1


class AccountCheckName(StrEnum):
    CALLER_IDENTITY = 'get-caller-identity'
    ACCOUNT_CHARGES = 'get-cost-and-usage'
    CREDENTIAL_EXPORT = 'export-credentials'


class RegionalCheckName(StrEnum):
    INSTANCES = 'describe-instances'
    OFFERINGS = 'describe-instance-type-offerings'
    SPOT_PRICES = 'describe-spot-price-history'
    SPOT_QUOTA = 'get-service-quota'
    TERMINATE = 'terminate-instances'
    TERMINATION_SIMULATION = 'termination'
    PUT_RESULT = 'put-object'
    LIST_RESULT = 'list-objects-v2'
    GET_RESULT = 'get-object'
    PUT_INPUT = 'put-object inputs'
    GET_INPUT = 'get-object inputs'
    MULTIPART_INPUT = 'create/abort-multipart-upload'


class InstanceTypeCheckName(StrEnum):
    LAUNCH = 'run-instances'


@dataclass(frozen=True)
class AccountCheck:
    name: AccountCheckName

    @property
    def operation(self) -> str:
        return self.name.value


def _validate_region(region: str) -> None:
    if not re.fullmatch(r'[a-z]{2,5}(?:-[a-z]+)+-[1-9][0-9]*', region):
        raise ValueError('Invalid gate probe region')


@dataclass(frozen=True)
class RegionalCheck:
    name: RegionalCheckName
    region: str

    def __post_init__(self) -> None:
        _validate_region(self.region)

    @property
    def operation(self) -> str:
        if self.name is RegionalCheckName.TERMINATION_SIMULATION:
            return f'{self.name} {self.region}'
        return self.name.value


@dataclass(frozen=True)
class GpuLaunchCheck:
    name: ClassVar[InstanceTypeCheckName] = InstanceTypeCheckName.LAUNCH
    region: str
    instance_type: str

    def __post_init__(self) -> None:
        _validate_region(self.region)
        if not re.fullmatch(r'[a-z][a-z0-9-]*\.[a-z0-9-]+', self.instance_type):
            raise ValueError('Invalid gate probe instance type')

    @property
    def operation(self) -> str:
        return f'{self.name} {self.instance_type}'


GateCheck = AccountCheck | RegionalCheck | GpuLaunchCheck


class ProbeVerdict(StrEnum):
    PASS = 'pass'
    FAIL = 'fail'
    UNVERIFIED = 'unverified'


class ProbeReason(StrEnum):
    OK = 'ok'
    DRY_RUN = 'dry-run'
    IAM_SIMULATION = 'iam-simulation'
    CHECK_FAILED = 'check-failed'
    SCALER_IDENTITY_MISMATCH = 'scaler-identity-mismatch'
    NO_OFFERED_SUBNET = 'no-offered-subnet'
    SIMULATOR_ACCESS_DENIED = 'simulator-access-denied'


_SUCCESS_DETAILS = {
    ProbeReason.OK: 'OK',
    ProbeReason.DRY_RUN: 'DryRunOperation',
    ProbeReason.IAM_SIMULATION: 'IAM simulation',
}
_REASONS = {
    ProbeVerdict.PASS: frozenset(_SUCCESS_DETAILS),
    ProbeVerdict.FAIL: frozenset((ProbeReason.CHECK_FAILED, ProbeReason.SCALER_IDENTITY_MISMATCH)),
    ProbeVerdict.UNVERIFIED: frozenset((ProbeReason.NO_OFFERED_SUBNET, ProbeReason.SIMULATOR_ACCESS_DENIED)),
}


def format_verdict(operation: str, verdict: ProbeVerdict, reason: ProbeReason, *,
                   failure_code: str = '', show_success_code: bool = True) -> str:
    if verdict is ProbeVerdict.UNVERIFIED:
        report = f'{operation} unverified.'
        if reason is ProbeReason.NO_OFFERED_SUBNET:
            report += ' No saved subnet offers this type.'
        return report
    if reason is ProbeReason.SCALER_IDENTITY_MISMATCH:
        return f'{operation}: fail. The scaler profile does not match the stack user.'
    code = failure_code
    if verdict is ProbeVerdict.PASS and show_success_code:
        code = _SUCCESS_DETAILS[reason]
    return f'{operation}: {verdict}' + (f' ({code})' if code else '')


@dataclass(frozen=True)
class ProbeCheck:
    check: GateCheck
    verdict: ProbeVerdict
    reason: ProbeReason

    def __post_init__(self) -> None:
        if self.reason not in _REASONS[self.verdict]:
            raise ValueError('Invalid gate probe reason')

    def report(self) -> str:
        code = self.reason.value if self.verdict is ProbeVerdict.FAIL else ''
        return format_verdict(self.check.operation, self.verdict, self.reason, failure_code=code)

    def encode(self) -> dict[str, object]:
        row: dict[str, object] = {'name': self.check.name.value, 'verdict': self.verdict.value,
                                  'reason': self.reason.value}
        if isinstance(self.check, (RegionalCheck, GpuLaunchCheck)):
            row['region'] = self.check.region
        if isinstance(self.check, GpuLaunchCheck):
            row['instance_type'] = self.check.instance_type
        return row

    @staticmethod
    def decode(row: dict[str, object]) -> ProbeCheck:
        name = ledger.text(row, 'name')
        verdict = ProbeVerdict(ledger.text(row, 'verdict'))
        reason = ProbeReason(ledger.text(row, 'reason'))
        fields = row.keys() - {'name', 'verdict', 'reason'}
        check: GateCheck
        if fields == {'region', 'instance_type'} and name == InstanceTypeCheckName.LAUNCH:
            check = GpuLaunchCheck(ledger.text(row, 'region'), ledger.text(row, 'instance_type'))
        elif fields == {'region'}:
            check = RegionalCheck(RegionalCheckName(name), ledger.text(row, 'region'))
        elif not fields:
            check = AccountCheck(AccountCheckName(name))
        else:
            raise ValueError('Invalid gate probe check')
        return ProbeCheck(check, verdict, reason)


@dataclass(frozen=True)
class GateProbeRecord:
    deployment_id: str
    ended_at: datetime
    checks: tuple[ProbeCheck, ...]


class ProbeRecordUnavailable(StrEnum):
    ABSENT = 'no probe record'
    CORRUPT = 'corrupt probe record'
    UNKNOWN_VERSION = 'unknown probe record version'
    OTHER_DEPLOYMENT = 'probe record is from another deployment'
    NO_DEPLOYMENT = 'no readable gate deployment record'


def invalidate_record(directory: Path) -> None:
    """Remove the old probe record before setup can change the deployment."""
    (directory / FILE_NAME).unlink(missing_ok=True)
    sync_folder(directory)


def save_record(directory: Path, record: GateProbeRecord) -> None:
    """Publish private evidence when the probe finishes."""
    document = {'version': _VERSION, 'deployment_id': record.deployment_id,
                'ended_at': record.ended_at.isoformat(), 'checks': [check.encode() for check in record.checks]}
    write_bytes_atomic(directory / FILE_NAME, (json.dumps(document, sort_keys=True) + '\n').encode(), mode=0o600)


def _check_deployment_scope(checks: tuple[ProbeCheck, ...], deployment: dict[str, object]) -> None:
    region_rows = ledger.rows(deployment, 'regions') if 'regions' in deployment else ()
    regions = {ledger.text(row, 'region') for row in region_rows}
    instance_types = deployment.get('gpu_instance_types', [])
    if not isinstance(instance_types, list):
        raise ValueError('Invalid gate deployment instance types')
    allowed_types = cast(list[object], instance_types)
    for row in checks:
        check = row.check
        if isinstance(check, (RegionalCheck, GpuLaunchCheck)) and check.region not in regions:
            raise ValueError('The gate probe region differs from the deployment')
        if isinstance(check, GpuLaunchCheck) and check.instance_type not in allowed_types:
            raise ValueError('The gate probe instance type differs from the deployment')


def read_record(directory: Path) -> GateProbeRecord | ProbeRecordUnavailable:
    """Read gate probe evidence for the current deployment only."""
    try:
        document = remote.read_record(directory / FILE_NAME)
        version = document.get('version')
        if type(version) is not int:
            return ProbeRecordUnavailable.CORRUPT
        if version != _VERSION:
            return ProbeRecordUnavailable.UNKNOWN_VERSION
        if document.keys() != {'version', 'deployment_id', 'ended_at', 'checks'}:
            return ProbeRecordUnavailable.CORRUPT
        deployment_id = ledger.text(document, 'deployment_id')
        ended_at = datetime.fromisoformat(ledger.text(document, 'ended_at'))
        checks = tuple(ProbeCheck.decode(row) for row in ledger.rows(document, 'checks'))
        if ended_at.utcoffset() != timedelta(0) or not checks:
            return ProbeRecordUnavailable.CORRUPT
    except FileNotFoundError:
        return ProbeRecordUnavailable.ABSENT
    except (OSError, ValueError):
        return ProbeRecordUnavailable.CORRUPT

    try:
        deployment = remote.read_record(directory / 'gate-deployment.json')
        current_id = ledger.text(deployment, 'deployment_id')
    except (OSError, ValueError):
        return ProbeRecordUnavailable.NO_DEPLOYMENT
    if current_id != deployment_id:
        return ProbeRecordUnavailable.OTHER_DEPLOYMENT
    try:
        _check_deployment_scope(checks, deployment)
    except ValueError:
        return ProbeRecordUnavailable.CORRUPT
    return GateProbeRecord(deployment_id, ended_at, checks)


def print_status(directory: Path, *, now: datetime) -> None:
    record = read_record(directory)
    if isinstance(record, ProbeRecordUnavailable):
        print(record.value)
        return
    age = max(0, int((now - record.ended_at).total_seconds()))
    print(f'probe-status: record age {age} s')
    for check in record.checks:
        print(f'{LABEL}: {check.report()}')
