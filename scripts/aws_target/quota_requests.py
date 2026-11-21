"""Read and store the state of regional Spot quota requests."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
from typing import cast

from scripts.aws_target import ledger
from scripts.aws_target.aws_cli import AwsCli
from scripts.aws_target.controller_lock import control_lock
from scripts.lib import conf, user_zone
from scripts.lib.atomic_file import write_text_atomic


RECORD_PATH = Path('quota-requests') / 'requests.json'
STANDARD_SPOT_QUOTA = 'L-34B43A08'
GPU_SPOT_QUOTA = 'L-3819A6DF'
# Match both Spot quota request limits in cloud_gate.conf.
MAX_REQUESTED_VCPUS = 10_000_000_000


class QuotaRequestStatus(StrEnum):
    PENDING = 'PENDING'
    CASE_OPENED = 'CASE_OPENED'
    IN_PROGRESS = 'IN_PROGRESS'
    APPROVED = 'APPROVED'
    DENIED = 'DENIED'
    CLOSED = 'CLOSED'
    CASE_CLOSED = 'CASE_CLOSED'
    NOT_APPROVED = 'NOT_APPROVED'
    INVALID_REQUEST = 'INVALID_REQUEST'
    UNKNOWN = 'unknown'


CLOSED_QUOTA_REQUEST_STATUSES: frozenset[str] = frozenset((
    QuotaRequestStatus.APPROVED, QuotaRequestStatus.DENIED, QuotaRequestStatus.CLOSED, QuotaRequestStatus.CASE_CLOSED,
    QuotaRequestStatus.NOT_APPROVED, QuotaRequestStatus.INVALID_REQUEST,
))


@dataclass(frozen=True)
class QuotaRequestTimeUnavailable:
    """AWS did not supply this time."""


def request_time(value: object) -> datetime | QuotaRequestTimeUnavailable:
    if value is None:
        return QuotaRequestTimeUnavailable()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if not isinstance(value, str):
        raise ValueError('The quota request date is invalid')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@dataclass(frozen=True)
class QuotaRequest:
    status: QuotaRequestStatus
    requested_vcpus: Decimal
    created_at: datetime | QuotaRequestTimeUnavailable
    updated_at: datetime | QuotaRequestTimeUnavailable

    @property
    def order_time(self) -> float:
        """Use the update time if AWS omits the creation time."""
        stamp = self.created_at if isinstance(self.created_at, datetime) else self.updated_at
        return stamp.timestamp() if isinstance(stamp, datetime) else float('-inf')


@dataclass(frozen=True)
class NoQuotaRequest:
    """AWS returned an empty request history."""


@dataclass(frozen=True)
class QuotaRequestsNotMeasured:
    reason: str


@dataclass(frozen=True)
class QuotaRequestReadFailed:
    reason: str


QuotaRequestReading = QuotaRequest | NoQuotaRequest | QuotaRequestsNotMeasured | QuotaRequestReadFailed


@dataclass(frozen=True)
class RegionalQuotaRequests:
    region: str
    standard: QuotaRequestReading
    gpu: QuotaRequestReading


def quota_request(status: str, requested_vcpus: Decimal, created: object, updated: object) -> QuotaRequest:
    try:
        request_status = QuotaRequestStatus(status)
    except ValueError:
        request_status = QuotaRequestStatus.UNKNOWN
    return QuotaRequest(request_status, requested_vcpus.normalize(), request_time(created), request_time(updated))


def _aws_requested_vcpus(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('The requested vCPU count must be a JSON number.')
    if not 1 <= value <= MAX_REQUESTED_VCPUS or value != int(value):
        raise ValueError('The requested vCPU count must be a whole number in range.')
    return Decimal(int(value))


def read_quota_request_rows(api: AwsCli, code: str) -> tuple[dict[str, object], ...]:
    """Read every page of request history in AWS response order."""
    history = api.call('list-requested-service-quota-change-history-by-quota',
                       '--service-code', 'ec2', '--quota-code', code, service='service-quotas')
    return ledger.rows(history, 'RequestedQuotas')


def read_quota_requests(api: AwsCli, code: str) -> tuple[QuotaRequest, ...]:
    return tuple(quota_request(ledger.text(row, 'Status'), _aws_requested_vcpus(row['DesiredValue']),
                              row.get('Created'), row.get('LastUpdated'))
                 for row in read_quota_request_rows(api, code))


def request_record(reading: QuotaRequestReading) -> dict[str, object]:
    if isinstance(reading, QuotaRequest):
        record: dict[str, object] = {'state': 'requested', 'status': reading.status.value,
                                     'requested_vcpus': format(reading.requested_vcpus, 'f')}
        if isinstance(reading.created_at, datetime):
            record['created_at'] = reading.created_at.isoformat()
        if isinstance(reading.updated_at, datetime):
            record['updated_at'] = reading.updated_at.isoformat()
        return record
    if isinstance(reading, NoQuotaRequest):
        return {'state': 'no_request'}
    if isinstance(reading, QuotaRequestReadFailed):
        return {'state': 'read_failed', 'reason': reading.reason}
    return {'state': 'not_measured', 'reason': reading.reason}


def request_reading(row: dict[str, object]) -> QuotaRequestReading:
    state = ledger.text(row, 'state')
    match state:
        case 'requested':
            return quota_request(ledger.text(row, 'status'), conf.decimal(str(row['requested_vcpus'])),
                                 row.get('created_at'), row.get('updated_at'))
        case 'no_request':
            return NoQuotaRequest()
        case 'read_failed':
            return QuotaRequestReadFailed(ledger.text(row, 'reason'))
        case 'not_measured':
            return QuotaRequestsNotMeasured(ledger.text(row, 'reason'))
        case _:
            raise ValueError('The quota request snapshot state is invalid')


def snapshot_quota_requests(document: dict[str, object], regions: tuple[str, ...]) -> tuple[RegionalQuotaRequests, ...]:
    """Return a typed reading for each region in the price table."""
    rows = ledger.rows(document, 'quota_requests') if 'quota_requests' in document else ()
    by_region = {ledger.text(row, 'region'): row for row in rows}
    missing = QuotaRequestsNotMeasured('The snapshot has no quota request history for this region.')
    return tuple(RegionalQuotaRequests(region, request_reading(ledger.mapping(row['standard'])),
                                       request_reading(ledger.mapping(row['gpu'])))
                 if (row := by_region.get(region)) is not None else RegionalQuotaRequests(region, missing, missing)
                 for region in regions)


type RegionalQuota = tuple[str, str]


@dataclass(frozen=True)
class OpenQuotaRequest:
    region: str
    quota_code: str
    quota_name: str
    asked_vcpus: str
    status: str
    # An empty value means AWS did not supply the case ID or time.
    case_id: str
    updated_at: str

    @property
    def quota(self) -> RegionalQuota:
        return self.region, self.quota_code

    @classmethod
    def from_reply(cls, region: str, code: str, name: str, status: str,
                   reply: Mapping[str, object]) -> OpenQuotaRequest:
        asked = format_asked_vcpus(reply['DesiredValue'])
        date = reply.get('LastUpdated') or reply.get('Created')
        moment = request_time(date)
        updated_at = moment.isoformat() if isinstance(moment, datetime) else ''
        return cls(region, code, str(reply.get('QuotaName') or name), asked, status,
                   str(reply.get('CaseId') or ''), updated_at)


@dataclass(frozen=True)
class QuotaRequestSnapshot:
    read_at: int
    requests: tuple[OpenQuotaRequest, ...]


def format_asked_vcpus(value: object) -> str:
    return f'{conf.decimal(str(value)).normalize():f}'


def timestamp(value: object) -> float:
    """Convert an AWS timestamp to seconds. Sort missing dates last."""
    moment = request_time(value)
    return moment.timestamp() if isinstance(moment, datetime) else float('-inf')


def shown_time(value: object) -> str:
    """Format a quota timestamp in the user's zone."""
    if value is None or value == '':
        return '-'
    moment = request_time(value)
    assert isinstance(moment, datetime)
    return moment.astimezone(user_zone.user_zone()).strftime('%Y-%m-%d %H:%M:%S %Z')


def read(directory: Path) -> QuotaRequestSnapshot:
    """Read the saved requests. Raise FileNotFoundError if no snapshot exists."""
    document = ledger.mapping(cast(object, json.loads((directory / RECORD_PATH).read_text(encoding='utf-8'))))
    requests: list[OpenQuotaRequest] = []
    for row in ledger.rows(document, 'requests'):
        status = ledger.text(row, 'status')
        if status in CLOSED_QUOTA_REQUEST_STATUSES:
            continue
        asked = format_asked_vcpus(ledger.text(row, 'asked_vcpus'))
        requests.append(OpenQuotaRequest(
            ledger.text(row, 'region'), ledger.text(row, 'quota_code'), ledger.text(row, 'quota_name'),
            asked, status, str(row.get('case_id') or ''), str(row.get('updated_at') or '')))
    return QuotaRequestSnapshot(ledger.instant(document, 'read_at'), tuple(requests))


def save(directory: Path, histories: Mapping[RegionalQuota, tuple[OpenQuotaRequest, ...]], read_at: int,
         *, complete: bool) -> None:
    """Replace the record atomically. Call with the quota command lock held."""
    if not histories and not complete:
        return
    path = directory / RECORD_PATH
    requests: list[OpenQuotaRequest] = []
    if not complete:
        try:
            previous = read(directory)
        except FileNotFoundError:
            pass
        else:
            requests.extend(row for row in previous.requests if row.quota not in histories)
            if requests:
                read_at = previous.read_at
    requests.extend(row for rows in histories.values() for row in rows if row.status not in CLOSED_QUOTA_REQUEST_STATUSES)
    document = {'read_at': read_at, 'requests': [asdict(row) for row in requests]}
    write_text_atomic(path, json.dumps(document) + '\n', new_file_mode=0o600)


def command_lock(directory: Path) -> AbstractContextManager[None]:
    """Serialize quota commands from the first AWS read through the save."""
    return control_lock(directory / RECORD_PATH.parent)
