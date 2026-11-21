"""Share exact Hana attempt and result identities without AWS credentials."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import sys
from typing import cast

from scripts.aws_target.hana_generation import HanaHostGeneration

RESULT_MAX_BYTES = 32 * 1024 * 1024
RESULT_UPLOAD_TIMEOUT = 'HanaResultUploadTimedOut'
RESULT_UPLOAD_FAILED = 'HanaResultUploadFailed'


class HanaResultDownloadTimedOut(RuntimeError):
    """The sender did not receive a result within its transfer limit."""

    def __init__(self, timeout_s: int) -> None:
        super().__init__(f'HanaResultDownloadTimedOut: The result download exceeded {timeout_s} s '
                         + '(result_transfer_timeout_s)')


@dataclass(frozen=True)
class HanaAttemptKey:
    episode: str
    attempt: str
    claim_id: str
    generation: HanaHostGeneration


@dataclass(frozen=True)
class LogicalStepLink:
    call_id: str
    episode: str


@dataclass(frozen=True)
class MeasuredPeak:
    bytes: int


@dataclass(frozen=True)
class PeakUnavailable:
    reason: str


@dataclass(frozen=True)
class HanaCommandResult:
    key: HanaAttemptKey
    source_key: str
    commit: str
    toolchain: str
    host: str
    argv: tuple[str, ...]
    slot: int
    status: int
    stdout: bytes
    stderr: bytes
    started_at: float
    finished_at: float
    execution_seconds: float
    memory: MeasuredPeak | PeakUnavailable


@dataclass(frozen=True)
class HanaResultExpectation:
    key: HanaAttemptKey
    source_key: str
    commit: str
    toolchain: str
    host: str
    argv: tuple[str, ...]
    slot: int


@dataclass(frozen=True)
class AcceptedHanaResult:
    result: HanaCommandResult


@dataclass(frozen=True)
class RejectedHanaResult:
    reason: str


HanaResultDecision = AcceptedHanaResult | RejectedHanaResult


@dataclass(frozen=True)
class HanaAttemptCharge:
    key: HanaAttemptKey
    link: LogicalStepLink
    uploaded_bytes: int
    downloaded_bytes: int
    requests: int
    started_at: float
    finished_at: float
    ledger_references: tuple[str, ...]


def encode_attempt_key(key: HanaAttemptKey) -> dict[str, object]:
    """Encode the exact attempt at the JSON boundary."""
    return {'episode': key.episode, 'attempt': key.attempt, 'claim_id': key.claim_id,
            'generation': {'deployment': key.generation.deployment,
                           'start_token': key.generation.start_token, 'boot_id': key.generation.boot_id}}


def decode_attempt_key(value: object) -> HanaAttemptKey:
    """Decode the shared identity without creating another generation type."""
    row = object_fields(value)
    generation = object_fields(row.get('generation'))
    return HanaAttemptKey(string_field(row, 'episode'), string_field(row, 'attempt'), string_field(row, 'claim_id'),
        HanaHostGeneration(string_field(generation, 'deployment'), string_field(generation, 'start_token'),
                           string_field(generation, 'boot_id')))


def encode_result(result: HanaCommandResult) -> bytes:
    """Encode the result and preserve both output byte streams."""
    content = json.dumps({'version': 1, 'key': encode_attempt_key(result.key),
        'source_key': result.source_key, 'commit': result.commit, 'toolchain': result.toolchain,
        'host': result.host, 'argv': result.argv, 'slot': result.slot, 'status': result.status,
        'stdout': base64.b64encode(result.stdout).decode('ascii'),
        'stderr': base64.b64encode(result.stderr).decode('ascii'), 'started_at': result.started_at,
        'finished_at': result.finished_at, 'execution_seconds': result.execution_seconds,
        'memory': ({'bytes': result.memory.bytes} if isinstance(result.memory, MeasuredPeak)
                   else {'reason': result.memory.reason})}, separators=(',', ':'), allow_nan=False).encode()
    if len(content) > RESULT_MAX_BYTES:
        raise ValueError('Hana result exceeds the transport limit')
    return content


def decode_result(content: bytes) -> HanaCommandResult:
    """Decode the guest result at the transport boundary."""
    if len(content) > RESULT_MAX_BYTES:
        raise ValueError('Hana result exceeds the transport limit')
    row = object_fields(cast(object, json.loads(content)))
    if type(row.get('version')) is not int or row['version'] != 1:
        raise ValueError('Unsupported Hana result version')
    argv = row['argv']
    if not isinstance(argv, list) or not argv or any(not isinstance(word, str) or '\0' in word for word in cast(list[object], argv)):
        raise ValueError('Invalid Hana result command')
    memory = object_fields(row['memory'])
    peak = MeasuredPeak(integer_field(memory, 'bytes')) if 'bytes' in memory else PeakUnavailable(string_field(memory, 'reason'))
    result = HanaCommandResult(decode_attempt_key(row['key']), string_field(row, 'source_key'),
        string_field(row, 'commit'), string_field(row, 'toolchain'), string_field(row, 'host'),
        tuple(cast(list[str], argv)), integer_field(row, 'slot'), integer_field(row, 'status'),
        base64.b64decode(string_field(row, 'stdout', empty=True), validate=True),
        base64.b64decode(string_field(row, 'stderr', empty=True), validate=True),
        seconds_field(row, 'started_at'), seconds_field(row, 'finished_at'), seconds_field(row, 'execution_seconds'), peak)
    if result.finished_at < result.started_at:
        raise ValueError('Hana result ends before it starts')
    return result


def validate_result(result: HanaCommandResult, expected: HanaResultExpectation,
                    current_source_key: str, *, fenced: bool = False) -> HanaResultDecision:
    """Check the current attempt and source before any result publication."""
    if fenced:
        return RejectedHanaResult('The Hana attempt is fenced')
    if result.key != expected.key:
        return RejectedHanaResult('The Hana attempt or host generation changed')
    for name, actual, requested in (('source_key', result.source_key, expected.source_key),
            ('commit', result.commit, expected.commit), ('toolchain', result.toolchain, expected.toolchain),
            ('host', result.host, expected.host), ('argv', result.argv, expected.argv), ('slot', result.slot, expected.slot)):
        if actual != requested:
            return RejectedHanaResult(f'The Hana result {name} differs from the request')
    if current_source_key != expected.source_key:
        return RejectedHanaResult('The current source key changed during execution or transfer')
    return AcceptedHanaResult(result)


def attempt_record_id(key: HanaAttemptKey) -> str:
    """Use one stable row identity for delivery and recorder recovery."""
    return 'hana-' + hashlib.sha256(json.dumps(encode_attempt_key(key), sort_keys=True).encode()).hexdigest()


def linked_step_fields(key: HanaAttemptKey, link: LogicalStepLink, role: str, final: bool) -> dict[str, object]:
    """Keep execution role separate from final-result selection."""
    if link.episode != key.episode or role not in ('remote', 'fallback'):
        raise ValueError('Invalid Hana step link or role')
    generation = json.dumps(encode_attempt_key(key)['generation'], sort_keys=True, separators=(',', ':'))
    return {'step_link': json.dumps([link.call_id, link.episode], separators=(',', ':')),
            'placement_episode': key.episode, 'placement_attempt': key.attempt,
            'execution_generation': generation, 'attempt_role': role, 'is_final_result': final}


def encode_attempt_charge(charge: HanaAttemptCharge) -> dict[str, object]:
    return {'key': encode_attempt_key(charge.key), 'call_id': charge.link.call_id,
            'episode': charge.link.episode, 'uploaded_bytes': charge.uploaded_bytes,
            'downloaded_bytes': charge.downloaded_bytes, 'requests': charge.requests,
            'started_at': charge.started_at, 'finished_at': charge.finished_at,
            'ledger_references': list(charge.ledger_references)}


def decode_attempt_charge(value: object) -> HanaAttemptCharge:
    row = object_fields(value)
    references = row['ledger_references']
    if not isinstance(references, list) or any(not isinstance(item, str) for item in cast(list[object], references)):
        raise ValueError('Invalid Hana ledger references')
    key = decode_attempt_key(row['key'])
    link = LogicalStepLink(string_field(row, 'call_id'), string_field(row, 'episode'))
    if link.episode != key.episode:
        raise ValueError('Hana charge has a different episode')
    charge = HanaAttemptCharge(key, link, integer_field(row, 'uploaded_bytes'), integer_field(row, 'downloaded_bytes'),
        integer_field(row, 'requests'), seconds_field(row, 'started_at'), seconds_field(row, 'finished_at'),
        tuple(cast(list[str], references)))
    if charge.finished_at < charge.started_at:
        raise ValueError('Hana charge ends before it starts')
    return charge


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in cast(dict[object, object], value)):
        raise ValueError('Hana record must be an object')
    return cast(dict[str, object], value)


def string_field(row: dict[str, object], name: str, *, empty: bool = False) -> str:
    value = row.get(name)
    if not isinstance(value, str) or '\0' in value or (not empty and not value):
        raise ValueError(f'Invalid Hana field: {name}')
    return value


def integer_field(row: dict[str, object], name: str) -> int:
    value = row.get(name)
    if type(value) is not int or value < 0:
        raise ValueError(f'Invalid Hana integer: {name}')
    return value


def seconds_field(row: dict[str, object], name: str) -> float:
    value = row.get(name)
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0 <= value <= sys.float_info.max:
        raise ValueError(f'Invalid Hana time: {name}')
    return float(value)
