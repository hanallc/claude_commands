"""Store the open quota requests from the deployment commands."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import cast

from scripts.aws_target import ledger
from scripts.aws_target.controller_lock import control_lock
from scripts.lib import conf, user_zone
from scripts.lib.atomic_file import write_text_atomic

RECORD_PATH = Path('quota-requests') / 'requests.json'
CLOSED_STATUSES = frozenset(('APPROVED', 'DENIED', 'CLOSED', 'CASE_CLOSED', 'NOT_APPROVED', 'INVALID_REQUEST'))

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
        updated_at = datetime.fromtimestamp(timestamp(date), UTC).isoformat() if date is not None else ''
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


def shown_time(value: object) -> str:
    """Format a quota timestamp in the user's zone."""
    if value is None or value == '':
        return '-'
    return datetime.fromtimestamp(timestamp(value), UTC).astimezone(user_zone.user_zone()).strftime('%Y-%m-%d %H:%M:%S %Z')


def read(directory: Path) -> QuotaRequestSnapshot:
    """Read the saved requests. Raise FileNotFoundError if no snapshot exists."""
    document = ledger.mapping(cast(object, json.loads((directory / RECORD_PATH).read_text(encoding='utf-8'))))
    requests: list[OpenQuotaRequest] = []
    for row in ledger.rows(document, 'requests'):
        status = ledger.text(row, 'status')
        if status in CLOSED_STATUSES:
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
    requests.extend(row for rows in histories.values() for row in rows if row.status not in CLOSED_STATUSES)
    document = {'read_at': read_at, 'requests': [asdict(row) for row in requests]}
    write_text_atomic(path, json.dumps(document) + '\n', new_file_mode=0o600)


def command_lock(directory: Path) -> AbstractContextManager[None]:
    """Serialize quota commands from the first AWS read through the save."""
    return control_lock(directory / RECORD_PATH.parent)
