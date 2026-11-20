"""Read and store the state of regional Spot quota requests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from scripts.aws_target import ledger
from scripts.aws_target.aws_cli import AwsCli
from scripts.lib import conf


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
    CASE_CLOSED = 'CASE_CLOSED'
    NOT_APPROVED = 'NOT_APPROVED'
    INVALID_REQUEST = 'INVALID_REQUEST'
    UNKNOWN = 'unknown'


CLOSED_QUOTA_REQUEST_STATUSES: frozenset[str] = frozenset((
    QuotaRequestStatus.APPROVED, QuotaRequestStatus.DENIED, QuotaRequestStatus.CASE_CLOSED,
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
