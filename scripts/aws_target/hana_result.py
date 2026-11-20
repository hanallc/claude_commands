"""Share exact Hana attempt and result identities without AWS credentials."""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
import hashlib
import json
import sys
from typing import Literal, cast, overload

from scripts.aws_target.hana_generation import HanaHostGeneration
from scripts.lib.strict_json import DuplicateJsonKey, require_finite_json_numbers, unique_json_fields

RESULT_MAX_BYTES = 32 * 1024 * 1024
RESULT_UPLOAD_TIMEOUT = 'HanaResultUploadTimedOut'
RESULT_UPLOAD_FAILED = 'HanaResultUploadFailed'


class HanaResultDownloadTimedOut(RuntimeError):
    """The sender did not receive a result within its transfer limit."""

    def __init__(self, timeout_s: int) -> None:
        super().__init__(f'HanaResultDownloadTimedOut: The result download exceeded {timeout_s} s '
                         + '(result_transfer_timeout_s)')


class HanaFixDeltaLimitExceeded(ValueError):
    """The result exceeds the fixed encoded size bound."""


@dataclass(frozen=True)
class HanaFixPermitted:
    """Permit a fix result or its complete lint skip."""


@dataclass(frozen=True)
class HanaJudgmentOnly:
    """Permit a judgment or its complete lint skip."""


HanaResultDeltaPolicy = HanaFixPermitted | HanaJudgmentOnly


class _LegacyResultPolicy(Enum):
    JUDGMENT_RESULT = 'version 1 judgment result only'


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
class _HanaCommandFields:
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
class HanaCommandResult(_HanaCommandFields):
    """Keep the untagged judgment result."""


HanaJudgmentResult = HanaCommandResult
LintCheck = Literal['mend', 'clippy']
FixFileMode = Literal['100644', '100755']

_FIX_COMMAND_CHECKS: dict[tuple[str, ...], frozenset[LintCheck]] = {
    ('lint', 'mend-fix'): frozenset(('mend', 'clippy')),
    ('lint', 'clippy-fix'): frozenset(('clippy',)),
}


@dataclass(frozen=True)
class FixDeltaAdd:
    path: str
    data: bytes
    mode: FixFileMode


@dataclass(frozen=True)
class FixDeltaReplace:
    path: str
    data: bytes


@dataclass(frozen=True)
class FixDeltaRemove:
    path: str


FixDeltaEntry = FixDeltaAdd | FixDeltaReplace | FixDeltaRemove


@dataclass(frozen=True)
class HanaFixEmpty(_HanaCommandFields):
    skipped_checks: list[LintCheck]


@dataclass(frozen=True)
class HanaFixDelta(_HanaCommandFields):
    skipped_checks: list[LintCheck]
    entries: list[FixDeltaEntry]


@dataclass(frozen=True)
class HanaFixFailed(_HanaCommandFields):
    skipped_checks: list[LintCheck]
    fix_status: int
    reason: str


@dataclass(frozen=True)
class HanaLintSkipped(_HanaCommandFields):
    skipped_checks: list[LintCheck]
    reason: str


HanaGuestResult = HanaJudgmentResult | HanaFixEmpty | HanaFixDelta | HanaFixFailed | HanaLintSkipped


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


_COMMAND_FIELDS = frozenset(('version', 'key', 'source_key', 'commit', 'toolchain', 'host', 'argv',
                            'slot', 'status', 'stdout', 'stderr', 'started_at', 'finished_at',
                            'execution_seconds', 'memory'))
_FIX_FIELDS: dict[str, frozenset[str]] = {
    'HanaFixEmpty': frozenset(),
    'HanaFixDelta': frozenset(('entries',)),
    'HanaFixFailed': frozenset(('fix_status', 'reason')),
    'HanaLintSkipped': frozenset(('reason',)),
}


def _result_document(row: dict[str, object]) -> bytes:
    content = json.dumps(row, separators=(',', ':'), allow_nan=False).encode('utf-8')
    _check_transport_size(content)
    return content


def _checked_result_fields(value: object) -> dict[str, object]:
    row = object_fields(value)
    try:
        require_finite_json_numbers(row)
    except RecursionError as error:
        raise ValueError('Invalid Hana result document') from error
    return row


def _check_transport_size(content: bytes) -> None:
    if len(content) > RESULT_MAX_BYTES:
        raise HanaFixDeltaLimitExceeded('Hana result exceeds the 32 MiB encoded bound')


def _require_fields(row: dict[str, object], expected: frozenset[str], record: str) -> None:
    if row.keys() != expected:
        raise ValueError(f'Invalid Hana {record} fields')


def _base64_text(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _base64_field(row: dict[str, object], name: str) -> bytes:
    return base64.b64decode(string_field(row, name, empty=True), validate=True)


def _fix_failure_status(value: object) -> int:
    if type(value) is not int or value == 0:
        raise ValueError('Invalid Hana fix failure status')
    return value


def _requested_checks(argv: tuple[str, ...], policy: HanaResultDeltaPolicy) -> frozenset[LintCheck]:
    """Identify the checks from the command name in the result."""
    command = argv[:2]
    if isinstance(policy, HanaJudgmentOnly) and command == ('cargo', 'clippy'):
        return frozenset(('clippy',))
    if isinstance(policy, HanaFixPermitted) and command in _FIX_COMMAND_CHECKS:
        return _FIX_COMMAND_CHECKS[command]
    raise ValueError('The Hana result command differs from its policy')


def _validate_result_form(result: HanaGuestResult, policy: HanaResultDeltaPolicy | _LegacyResultPolicy) -> None:
    if isinstance(result, HanaCommandResult):
        if isinstance(policy, HanaFixPermitted) or result.argv[:2] in _FIX_COMMAND_CHECKS:
            raise ValueError('A Hana fix requires a fix result')
        return
    if isinstance(policy, _LegacyResultPolicy):
        raise ValueError('A tagged Hana result requires an explicit policy')
    if isinstance(policy, HanaJudgmentOnly) and not isinstance(result, HanaLintSkipped):
        raise ValueError('A Hana judgment cannot return a fix result')
    requested = _requested_checks(result.argv, policy)
    skipped = frozenset(result.skipped_checks)
    if len(skipped) != len(result.skipped_checks) or not skipped <= requested:
        raise ValueError('The Hana skipped checks differ from the requested checks')
    status = result.status
    if isinstance(result, HanaLintSkipped):
        if status != 0 or skipped != requested:
            raise ValueError('A complete Hana skip requires zero status and every requested check')
        _ = string_field({'reason': result.reason}, 'reason')
        return
    if skipped == requested:
        raise ValueError('A complete Hana skip requires the HanaLintSkipped tag')
    if isinstance(result, HanaFixFailed):
        fix_status = _fix_failure_status(result.fix_status)
        if status != 0 and fix_status != status:
            raise ValueError('The Hana fix failure status differs from the command status')
        _ = string_field({'reason': result.reason}, 'reason')
    elif status != 0:
        raise ValueError('A successful Hana fix requires zero status')


def _checked_command_fields(row: dict[str, object], stdout: object, stderr: object) -> _HanaCommandFields:
    argv = row['argv']
    if not isinstance(argv, list) or not argv or any(
            not isinstance(word, str) or '\0' in word for word in cast(list[object], argv)):
        raise ValueError('Invalid Hana result command')
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise ValueError('Hana result output streams must be bytes')
    memory = object_fields(row['memory'])
    if memory.keys() == {'bytes'}:
        peak = MeasuredPeak(integer_field(memory, 'bytes'))
    elif memory.keys() == {'reason'}:
        peak = PeakUnavailable(string_field(memory, 'reason'))
    else:
        raise ValueError('Invalid Hana result memory fields')
    slot = integer_field(row, 'slot')
    status = integer_field(row, 'status')
    started = seconds_field(row, 'started_at')
    finished = seconds_field(row, 'finished_at')
    if finished < started:
        raise ValueError('Hana result ends before it starts')
    return _HanaCommandFields(
        key=decode_attempt_key(row['key']),
        source_key=string_field(row, 'source_key'),
        commit=string_field(row, 'commit'),
        toolchain=string_field(row, 'toolchain'),
        host=string_field(row, 'host'),
        argv=tuple(cast(list[str], argv)),
        slot=slot,
        status=status,
        stdout=stdout,
        stderr=stderr,
        started_at=started,
        finished_at=finished,
        execution_seconds=seconds_field(row, 'execution_seconds'),
        memory=peak,
    )


def _checked_addition_mode(mode: str) -> FixFileMode:
    if mode == '100644' or mode == '100755':
        return mode
    raise ValueError('Invalid Hana fix addition mode')


def _validate_delta(entries: list[FixDeltaEntry]) -> None:
    # The existing judgment image does not ship this module.
    from scripts.lib.fix_delta_paths import validate_fix_delta_paths

    if not entries:
        raise ValueError('A Hana fix delta requires at least one entry')
    _ = validate_fix_delta_paths([entry.path for entry in entries])


def _encode_entry(entry: FixDeltaEntry) -> dict[str, object]:
    if isinstance(entry, FixDeltaAdd):
        mode = _checked_addition_mode(entry.mode)
        return {'kind': 'add', 'path': entry.path, 'data': _base64_text(entry.data), 'mode': mode}
    if isinstance(entry, FixDeltaReplace):
        return {'kind': 'replace', 'path': entry.path, 'data': _base64_text(entry.data)}
    return {'kind': 'remove', 'path': entry.path}


def _decode_entry(value: object) -> FixDeltaEntry:
    row = object_fields(value)
    kind = string_field(row, 'kind')
    path = string_field(row, 'path')
    if kind == 'add':
        _require_fields(row, frozenset(('kind', 'path', 'data', 'mode')), 'addition')
        mode = _checked_addition_mode(string_field(row, 'mode'))
        return FixDeltaAdd(path, _base64_field(row, 'data'), mode)
    if kind == 'replace':
        _require_fields(row, frozenset(('kind', 'path', 'data')), 'replacement')
        return FixDeltaReplace(path, _base64_field(row, 'data'))
    if kind == 'remove':
        _require_fields(row, frozenset(('kind', 'path')), 'removal')
        return FixDeltaRemove(path)
    raise ValueError('Unknown Hana fix delta entry kind')


@overload
def encode_result(result: HanaJudgmentResult) -> bytes: ...


@overload
def encode_result(result: HanaGuestResult, *, delta_policy: HanaResultDeltaPolicy) -> bytes: ...


def encode_result(result: HanaGuestResult, *,
                  delta_policy: HanaResultDeltaPolicy | _LegacyResultPolicy = _LegacyResultPolicy.JUDGMENT_RESULT
                  ) -> bytes:
    """Encode the result and preserve both output byte streams."""
    row: dict[str, object] = {'version': 1, 'key': encode_attempt_key(result.key),
        'source_key': result.source_key, 'commit': result.commit, 'toolchain': result.toolchain,
        'host': result.host, 'argv': list(result.argv), 'slot': result.slot, 'status': result.status,
        'stdout': '', 'stderr': '', 'started_at': result.started_at,
        'finished_at': result.finished_at, 'execution_seconds': result.execution_seconds,
        'memory': ({'bytes': result.memory.bytes} if isinstance(result.memory, MeasuredPeak)
                   else {'reason': result.memory.reason})}
    _ = _checked_command_fields(row, result.stdout, result.stderr)
    _validate_result_form(result, delta_policy)
    row.update(stdout=_base64_text(result.stdout), stderr=_base64_text(result.stderr))
    if not isinstance(result, HanaCommandResult):
        row.update(version=2, kind=type(result).__name__, skipped_checks=result.skipped_checks)
        if isinstance(result, HanaFixDelta):
            _validate_delta(result.entries)
            row['entries'] = [_encode_entry(entry) for entry in result.entries]
        if isinstance(result, HanaFixFailed):
            row['fix_status'] = result.fix_status
        if isinstance(result, (HanaFixFailed, HanaLintSkipped)):
            row['reason'] = result.reason
    return _result_document(row)


@overload
def decode_result(content: bytes) -> HanaJudgmentResult: ...


@overload
def decode_result(content: bytes, *, delta_policy: HanaResultDeltaPolicy) -> HanaGuestResult: ...


def decode_result(content: bytes, *,
                  delta_policy: HanaResultDeltaPolicy | _LegacyResultPolicy = _LegacyResultPolicy.JUDGMENT_RESULT
                  ) -> HanaGuestResult:
    """Decode the guest result at the transport boundary."""
    _check_transport_size(content)
    try:
        document = cast(object, json.loads(content.decode('utf-8'), object_pairs_hook=unique_json_fields))
    except DuplicateJsonKey as error:
        raise ValueError(f'Duplicate Hana result field: {error.key}') from error
    except RecursionError as error:
        raise ValueError('Invalid Hana result document') from error
    return _decode_result_row(_checked_result_fields(document), delta_policy)


@overload
def decode_result_fields(value: object) -> HanaJudgmentResult: ...


@overload
def decode_result_fields(value: object, *, delta_policy: HanaResultDeltaPolicy) -> HanaGuestResult: ...


def decode_result_fields(value: object, *,
                         delta_policy: HanaResultDeltaPolicy | _LegacyResultPolicy = _LegacyResultPolicy.JUDGMENT_RESULT
                         ) -> HanaGuestResult:
    """Check the compact size and decode an embedded result."""
    row = _checked_result_fields(value)
    try:
        _ = _result_document(row)
    except (TypeError, RecursionError) as error:
        raise ValueError('Invalid Hana result document') from error
    return _decode_result_row(row, delta_policy)


def _decode_result_row(row: dict[str, object], policy: HanaResultDeltaPolicy | _LegacyResultPolicy) -> HanaGuestResult:
    version = row.get('version')
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported Hana result version')
    if version == 1:
        _require_fields(row, _COMMAND_FIELDS, 'result')
        kind = ''
    else:
        if isinstance(policy, _LegacyResultPolicy):
            raise ValueError('A tagged Hana result requires an explicit policy')
        kind = string_field(row, 'kind')
        if kind not in _FIX_FIELDS:
            raise ValueError('Unknown Hana result kind')
        _require_fields(row, _COMMAND_FIELDS | {'kind', 'skipped_checks'} | _FIX_FIELDS[kind], 'result')
    command = _checked_command_fields(row, _base64_field(row, 'stdout'), _base64_field(row, 'stderr'))
    result: HanaGuestResult
    if version == 1:
        result = _result_from_command(HanaCommandResult, command)
    else:
        checks = row['skipped_checks']
        if not isinstance(checks, list) or any(check not in ('mend', 'clippy') for check in cast(list[object], checks)):
            raise ValueError('Invalid Hana skipped checks')
        skipped = cast(list[LintCheck], checks)
        if kind == 'HanaFixEmpty':
            result = _result_from_command(partial(HanaFixEmpty, skipped_checks=skipped), command)
        elif kind == 'HanaFixFailed':
            result = _result_from_command(partial(HanaFixFailed, skipped_checks=skipped,
                fix_status=_fix_failure_status(row['fix_status']), reason=string_field(row, 'reason')), command)
        elif kind == 'HanaLintSkipped':
            result = _result_from_command(partial(HanaLintSkipped, skipped_checks=skipped,
                reason=string_field(row, 'reason')), command)
        else:
            entries = row['entries']
            if not isinstance(entries, list):
                raise ValueError('Invalid Hana fix delta entries')
            delta = [_decode_entry(entry) for entry in cast(list[object], entries)]
            result = _result_from_command(partial(HanaFixDelta, skipped_checks=skipped, entries=delta), command)
            _validate_delta(result.entries)
    _validate_result_form(result, policy)
    return result


def _result_from_command[T: _HanaCommandFields](factory: Callable[..., T], command: _HanaCommandFields) -> T:
    # Each result constructor inherits the common fields from the same dataclass.
    return factory(**vars(command))


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
