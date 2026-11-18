"""Dispatch one assigned Hana command and retain its execution until settlement."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import TYPE_CHECKING, Protocol, cast

from scripts.aws_target.hana_guest import CommandAdmissionRefusalCode, WorkPermit
from scripts.aws_target import hana_result, hana_source
from scripts.aws_target.hana_result import HanaAttemptKey, LogicalStepLink
from scripts.aws_target.hana_source import SourceManifest
from scripts.build_queue import placement

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from scripts.aws_target import hana_box, hana_placement
    from scripts.build_queue import admission_protocol as wire
    from scripts.buildlog.sync import TransferBytes

CONTROL_MAX_BYTES = 65536
COMMAND_RESULT_PENDING = 'The command result is pending'
_MISSING_BASIS_PREFIX = 'MissingBasis: '


@dataclass(frozen=True)
class HanaExecutionRequest:
    """The request binds exact source and command to one assigned slot."""
    key: HanaAttemptKey
    manifest: SourceManifest
    slot: int
    source_key: placement.SourceKeyKnown
    argv: tuple[str, ...]
    link: LogicalStepLink


@dataclass(frozen=True)
class HanaExecutionAccess:
    """Scoped URLs let root publish observations and results without AWS credentials."""
    observation_url: str
    result_url: str
    expires_at: float
    output_max_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class HanaCommandEnvelope:
    """The command carries the current acknowledged lifecycle permission."""
    request: HanaExecutionRequest
    permission: WorkPermit
    access: HanaExecutionAccess


@dataclass(frozen=True)
class HanaFenceEnvelope:
    """The fence prevents command acceptance for this exact attempt and slot."""
    key: HanaAttemptKey
    slot: int
    access: HanaExecutionAccess


@dataclass(frozen=True)
class HanaSourceUpdateEnvelope:
    """A complete source bundle repairs the original capture without command authority."""
    key: HanaAttemptKey
    slot: int
    manifest: SourceManifest
    access: HanaExecutionAccess


HanaExecutionEnvelope = HanaCommandEnvelope | HanaFenceEnvelope | HanaSourceUpdateEnvelope


@dataclass(frozen=True)
class NoCommandAdmissionRefusalCode:
    """The observation has no typed command admission refusal."""


@dataclass(frozen=True)
class HanaCommandObservation:
    """The guest reports acceptance, fencing, termination, and slot lease release."""
    key: HanaAttemptKey
    slot: int
    accepted: bool
    fenced: bool
    terminated: bool
    lease_free: bool
    observed_at: float
    evidence: placement.ExecutionEvidence
    source_updated: bool = False
    refusal_code: CommandAdmissionRefusalCode | NoCommandAdmissionRefusalCode = NoCommandAdmissionRefusalCode()


def encode_envelope(envelope: HanaExecutionEnvelope) -> bytes:
    """Encode a bounded version 1 execute or fence document."""
    if isinstance(envelope, HanaCommandEnvelope):
        document: dict[str, object] = {'version': 1, 'action': 'execute', 'request': request_fields(envelope.request),
                    'permission': asdict(envelope.permission), 'access': asdict(envelope.access)}
    elif isinstance(envelope, HanaSourceUpdateEnvelope):
        document = {'version': 1, 'action': 'source-update', 'key': hana_result.encode_attempt_key(envelope.key),
                    'slot': envelope.slot, 'manifest': asdict(envelope.manifest), 'access': asdict(envelope.access)}
    else:
        document = {'version': 1, 'action': 'fence', 'key': hana_result.encode_attempt_key(envelope.key),
                    'slot': envelope.slot, 'access': asdict(envelope.access)}
    content = _encode(document)
    _ = decode_envelope(content)
    return content


def decode_envelope(content: bytes) -> HanaExecutionEnvelope:
    """Decode a bounded document at the guest transport boundary."""
    row = _document(content)
    access_row = hana_result.object_fields(row['access'])
    access = HanaExecutionAccess(_signed_url(access_row, 'observation_url'), _signed_url(access_row, 'result_url'),
        hana_result.seconds_field(access_row, 'expires_at'), _positive_integer(access_row, 'output_max_bytes'))
    if access.output_max_bytes > 16 * 1024 * 1024:
        raise ValueError('The Hana output limit exceeds the protocol bound')
    if row.get('action') == 'execute':
        request = decode_request(_encode(hana_result.object_fields(row['request'])))
        permit_row = hana_result.object_fields(row['permission'])
        generation = hana_result.string_field(permit_row, 'generation')
        if not generation.isascii() or not generation.isdecimal() or int(generation) < 1:
            raise ValueError('The Hana permit generation is invalid')
        permit = WorkPermit(hana_result.seconds_field(permit_row, 'expires_at'), generation,
                            hana_result.string_field(permit_row, 'boot_id'))
        if permit.boot_id != request.key.generation.boot_id:
            raise ValueError('The Hana command permit belongs to another boot')
        return HanaCommandEnvelope(request, permit, access)
    if row.get('action') == 'source-update':
        manifest = _decode_manifest(row['manifest'])
        if manifest.basis_commits:
            raise ValueError('A Hana source update requires a complete bundle')
        return HanaSourceUpdateEnvelope(hana_result.decode_attempt_key(row['key']), _positive_integer(row, 'slot'), manifest, access)
    if row.get('action') != 'fence':
        raise ValueError('The Hana command action is invalid')
    return HanaFenceEnvelope(hana_result.decode_attempt_key(row['key']), _positive_integer(row, 'slot'), access)


def encode_observation(observation: HanaCommandObservation) -> bytes:
    """Encode one observation with its exact attempt and slot."""
    evidence = _evidence_fields(observation.evidence)
    if isinstance(observation.refusal_code, CommandAdmissionRefusalCode):
        evidence['refusal_code'] = observation.refusal_code.value
    return _encode({'version': 1, 'key': hana_result.encode_attempt_key(observation.key), 'slot': observation.slot,
        'accepted': observation.accepted, 'fenced': observation.fenced, 'terminated': observation.terminated,
        'lease_free': observation.lease_free, 'observed_at': observation.observed_at,
        'evidence': evidence, 'source_updated': observation.source_updated})


def decode_observation(content: bytes) -> HanaCommandObservation:
    """Reject malformed observations before the controller uses their evidence."""
    row = _document(content)
    flags: list[bool] = []
    for name in ('accepted', 'fenced', 'terminated', 'lease_free', 'source_updated'):
        value = row.get(name)
        if not isinstance(value, bool):
            raise ValueError('The Hana observation flag is invalid: ' + name)
        flags.append(value)
    evidence_row = hana_result.object_fields(row['evidence'])
    refusal_code = (CommandAdmissionRefusalCode(hana_result.string_field(evidence_row, 'refusal_code'))
                    if 'refusal_code' in evidence_row else NoCommandAdmissionRefusalCode())
    observation = HanaCommandObservation(hana_result.decode_attempt_key(row['key']), _positive_integer(row, 'slot'),
        flags[0], flags[1], flags[2], flags[3], hana_result.seconds_field(row, 'observed_at'),
        _decode_evidence(evidence_row), flags[4], refusal_code)
    if observation.accepted and isinstance(observation.evidence, placement.ExecutionNotStarted):
        raise ValueError('An accepted Hana command needs execution evidence')
    if observation.source_updated and not observation.accepted:
        raise ValueError('A Hana source update needs the original accepted attempt')
    if isinstance(refusal_code, CommandAdmissionRefusalCode) and not isinstance(
            command_admission_refusal(observation), placement.HanaCommandAdmissionRefused):
        raise ValueError('A Hana command admission refusal needs confirmed departure and a cause')
    return observation


def request_fields(request: HanaExecutionRequest) -> dict[str, object]:
    """Keep the command binding at the transport boundary."""
    return {'version': 1, 'key': hana_result.encode_attempt_key(request.key), 'manifest': asdict(request.manifest),
            'slot': request.slot, 'source_key': {'key': request.source_key.key}, 'argv': request.argv,
            'link': asdict(request.link)}


def encode_request(request: HanaExecutionRequest) -> bytes:
    """Give the runner source and command data without publication URLs."""
    content = _encode(request_fields(request))
    _ = decode_request(content)
    return content


def decode_request(content: bytes) -> HanaExecutionRequest:
    """Decode one command request without importing the controller or provider."""
    row = _document(content)
    manifest = _decode_manifest(row['manifest'])
    key = hana_result.decode_attempt_key(row['key'])
    source = placement.SourceKeyKnown(hana_result.string_field(hana_result.object_fields(row['source_key']), 'key'))
    link_row = hana_result.object_fields(row['link'])
    link = LogicalStepLink(hana_result.string_field(link_row, 'call_id'), hana_result.string_field(link_row, 'episode'))
    argv = row.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(word, str) or '\0' in word for word in cast(list[object], argv)):
        raise ValueError('The Hana command arguments are invalid')
    request = HanaExecutionRequest(key, manifest, _positive_integer(row, 'slot'), source, tuple(cast(list[str], argv)), link)
    if (request.argv != manifest.argv or key.claim_id != placement.assigned_claim_id(key.episode, key.attempt)
            or link.episode != key.episode):
        raise ValueError('The Hana source, command, or attempt binding differs')
    return request


def _decode_manifest(value: object) -> SourceManifest:
    # The guest applies its configured source limits again before materialization.
    return hana_source.decode_manifest(_encode(hana_result.object_fields(value)),
        hana_source.SourceLimits(1073741824, 120, 60, ('RUSTFLAGS', 'RUSTDOCFLAGS', 'CARGO_INCREMENTAL', 'RUST_BACKTRACE')))


def _encode(document: dict[str, object]) -> bytes:
    content = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(content) > CONTROL_MAX_BYTES:
        raise ValueError('The Hana control document exceeds its protocol bound')
    return content


def _document(content: bytes) -> dict[str, object]:
    if len(content) > CONTROL_MAX_BYTES:
        raise ValueError('The Hana control document exceeds its protocol bound')
    row = hana_result.object_fields(cast(object, json.loads(content)))
    if type(row.get('version')) is not int or row['version'] != 1:
        raise ValueError('The Hana control version is invalid')
    return row


def _positive_integer(row: dict[str, object], name: str) -> int:
    value = hana_result.integer_field(row, name)
    if value < 1:
        raise ValueError('The Hana integer must be positive: ' + name)
    return value


def _signed_url(row: dict[str, object], name: str) -> str:
    from urllib.parse import urlsplit

    value = hana_result.string_field(row, name)
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or not parts.hostname.endswith('.amazonaws.com')
            or '.s3.' not in parts.hostname or parts.username or parts.password or parts.fragment):
        raise ValueError('The Hana publication URL is invalid')
    return value


def _evidence_fields(evidence: placement.ExecutionEvidence) -> dict[str, object]:
    if isinstance(evidence, placement.ExecutionNotStarted):
        return {'kind': 'not-started'}
    if isinstance(evidence, placement.ExecutionEvidenceUnavailable):
        return {'kind': 'unavailable', 'reason': evidence.reason, 'observed_at': evidence.observed_at}
    return {'kind': 'observed', 'identity': asdict(evidence.identity),
            'identity_kind': 'process' if isinstance(evidence.identity, placement.ProcessExecution) else 'cloud'}


def _decode_evidence(value: object) -> placement.ExecutionEvidence:
    row = hana_result.object_fields(value)
    if row.get('kind') == 'not-started':
        return placement.ExecutionNotStarted()
    if row.get('kind') == 'unavailable':
        return placement.ExecutionEvidenceUnavailable(hana_result.string_field(row, 'reason'),
                                                     hana_result.seconds_field(row, 'observed_at'))
    if row.get('kind') != 'observed':
        raise ValueError('The Hana execution evidence is invalid')
    identity = hana_result.object_fields(row['identity'])
    text = hana_result.string_field
    if row.get('identity_kind') == 'process':
        return placement.ObservedExecution(placement.ProcessExecution(text(identity, 'host'), text(identity, 'generation'),
            _positive_integer(identity, 'pid'), text(identity, 'start_time')))
    if row.get('identity_kind') != 'cloud':
        raise ValueError('The Hana execution identity is invalid')
    return placement.ObservedExecution(placement.CloudExecution(text(identity, 'target'), text(identity, 'generation'),
        text(identity, 'instance'), text(identity, 'attempt')))


@dataclass(frozen=True)
class ExecutionPreparationRefused:
    reason: str


@dataclass(frozen=True)
class DispatchPrepared:
    key: HanaAttemptKey
    evidence: placement.ExecutionEvidence


@dataclass(frozen=True)
class DispatchSent:
    key: HanaAttemptKey
    evidence: placement.ExecutionEvidence


@dataclass(frozen=True)
class DispatchAcknowledged:
    key: HanaAttemptKey
    evidence: placement.ExecutionEvidence


@dataclass(frozen=True)
class DispatchFenced:
    key: HanaAttemptKey
    evidence: placement.ExecutionEvidence


@dataclass(frozen=True)
class DispatchSettled:
    key: HanaAttemptKey
    evidence: placement.ExecutionEvidence


HanaDispatchState = DispatchPrepared | DispatchSent | DispatchAcknowledged | DispatchFenced | DispatchSettled


@dataclass(frozen=True)
class HanaSettled:
    outcome: placement.TerminalAttemptOutcome
    evidence: placement.ExecutionEvidence


@dataclass(frozen=True)
class HanaGuestEndedWithoutResult(HanaSettled):
    """The guest confirmed departure and the sender has no accepted compiler result."""
    outcome: placement.AttemptDeclined
    evidence: placement.ExecutionEvidenceUnavailable


@dataclass(frozen=True)
class HanaAdmissionRefusalSettled(HanaSettled):
    """The guest confirmed that the command did not start."""
    outcome: placement.HanaCommandAdmissionRefused
    evidence: placement.ExecutionNotStarted


@dataclass(frozen=True)
class HanaSettlementPending:
    reason: str


@dataclass(frozen=True)
class HanaGuestEndedClaimRetained(HanaSettlementPending):
    """The guest confirmed departure and the lifecycle claim still needs release."""


HanaSettlement = HanaSettled | HanaSettlementPending


class ExecutionObjects(Protocol):
    """Keep executable publication separate from retriable observations and fences."""
    def execution_access(self, key: HanaAttemptKey) -> HanaExecutionAccess: ...
    def publish_execution(self, envelope: HanaExecutionEnvelope) -> None: ...
    def observe_execution(self, key: HanaAttemptKey) -> HanaCommandObservation: ...
    def execution_result(self, key: HanaAttemptKey) -> bytes: ...


class SourceCapture(Protocol):
    """The source sender retains ownership of capture and source publication."""
    def prepare(self, source: Path, argv: tuple[str, ...], *, environment: Mapping[str, str],
                features: tuple[str, ...] = ()) -> SourceManifest: ...


def execution_path(directory: Path, execution: str) -> Path:
    import hashlib

    return directory / 'hana' / 'executions' / (hashlib.sha256(execution.encode()).hexdigest() + '.json')


def execution_guard_path(directory: Path, execution: str) -> Path:
    return execution_path(directory, execution).with_suffix('.guard')


def _read_journal(directory: Path, execution: str) -> dict[str, object]:
    from scripts.aws_target.remote import read_record

    row = read_record(execution_path(directory, execution))
    if row.get('kind') == 'unpublished':
        if row.get('claim_id') != execution or not hana_result.string_field(row, 'episode') or not hana_result.string_field(row, 'attempt'):
            raise ValueError('The Hana prelaunch closure is invalid')
        claim_id = hana_result.string_field(row, 'claim_id')
    else:
        request = decode_request(_encode(hana_result.object_fields(row['request'])))
        claim_id = request.key.claim_id
    if (row.get('version') != 1 or claim_id != execution
            or row.get('phase') not in ('prepared', 'sent', 'acknowledged', 'fenced', 'settled')
            or type(row.get('publication_attempted')) is not bool or type(row.get('command_settled')) is not bool):
        raise ValueError('The Hana execution journal is invalid')
    _ = _decode_evidence(row['evidence'])
    if row['phase'] == 'settled':
        if row['command_settled'] is not True:
            raise ValueError('The settled Hana journal has no command closure')
        _ = _terminal(row)
    return row


def _save_journal(directory: Path, execution: str, row: dict[str, object]) -> None:
    from scripts.lib.atomic_file import write_text_atomic

    write_text_atomic(execution_path(directory, execution), json.dumps(row, sort_keys=True, allow_nan=False) + '\n',
                      new_file_mode=0o600)


def _journal_lock(directory: Path, execution: str):
    """Acquire only the attempt lock during transport waits."""
    from scripts.aws_target.controller_lock import control_lock

    return control_lock(execution_path(directory, execution).with_suffix('.lock'))


def execution_command_settled(directory: Path, execution: str) -> bool:
    """Require a durable closed command path before lifecycle release."""
    try:
        return _read_journal(directory, execution).get('command_settled') is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def claim_dispatch_pending(directory: Path, reference: placement.ProviderClaimReference) -> bool:
    """Retain missing, unreadable, or incomplete dispatches after local holder death."""
    try:
        row = _read_journal(directory, reference.claim_id)
        return (_journal_attempt(row) != (reference.episode, reference.attempt)
                or row['phase'] != 'settled')
    except (OSError, ValueError, KeyError, TypeError):
        return True


def claim_admission_refused(directory: Path, reference: placement.ProviderClaimReference) -> bool:
    """Check that the exact claim has a durable command admission refusal."""
    try:
        row = _read_journal(directory, reference.claim_id)
        return (row['phase'] == 'settled' and _journal_attempt(row) == (reference.episode, reference.attempt)
                and isinstance(_settled_dispatch(row), HanaAdmissionRefusalSettled))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def read_claim_execution(directory: Path, reference: placement.ProviderClaimReference) -> placement.ExecutionEvidence:
    """Supply durable remote evidence without using the local holder as proof of departure."""
    import time

    try:
        row = _read_journal(directory, reference.claim_id)
        if _journal_attempt(row) != (reference.episode, reference.attempt):
            raise ValueError('The Hana execution journal belongs to another attempt')
        return _decode_evidence(row['evidence'])
    except (OSError, ValueError, KeyError, TypeError):
        return placement.ExecutionEvidenceUnavailable('The Hana execution journal is unavailable', time.time())


def _journal_attempt(row: dict[str, object]) -> tuple[str, str]:
    if row.get('kind') == 'unpublished':
        return hana_result.string_field(row, 'episode'), hana_result.string_field(row, 'attempt')
    request = decode_request(_encode(hana_result.object_fields(row['request'])))
    return request.key.episode, request.key.attempt


def settle_unpublished_claim(directory: Path, reference: placement.ProviderClaimReference, *,
                             release_claim: Callable[[], bool], reason: str, cancelled: bool = False) -> HanaSettlement:
    """Close a claim that never created an executable request without inventing a boot identity."""
    execution = reference.claim_id
    try:
        with _journal_lock(directory, execution):
            if execution_path(directory, execution).exists():
                row = _read_journal(directory, execution)
                if row.get('kind') != 'unpublished' or _journal_attempt(row) != (reference.episode, reference.attempt):
                    return HanaSettlementPending('The Hana attempt already has a command dispatch')
            else:
                row: dict[str, object] = {'version': 1, 'kind': 'unpublished', 'claim_id': execution,
                    'episode': reference.episode, 'attempt': reference.attempt, 'phase': 'fenced',
                    'evidence': _evidence_fields(placement.ExecutionNotStarted()), 'publication_attempted': False,
                    'command_settled': True, 'outcome': {'kind': 'cancelled' if cancelled else 'prelaunch', 'reason': reason}}
                _save_journal(directory, execution, row)
        if not release_claim():
            return HanaSettlementPending('The Hana lifecycle start or claim remains unresolved')
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            row['phase'] = 'settled'
            _save_journal(directory, execution, row)
            return HanaSettled(_terminal(row), placement.ExecutionNotStarted())
    except (OSError, ValueError, RuntimeError, ConnectionError, KeyError, TypeError):
        return HanaSettlementPending('The Hana prelaunch claim settlement is unavailable')


@dataclass(frozen=True)
class HanaObservationUnavailable:
    reason: str


@dataclass(frozen=True)
class NoMissingBasis:
    """The observation has no valid request for complete captured source."""


def missing_basis(observation: HanaCommandObservation, request: HanaExecutionRequest) -> hana_source.MissingBasis | NoMissingBasis:
    """Decode the guest diagnostic without accepting a different repository or basis."""
    evidence = observation.evidence
    if not isinstance(evidence, placement.ExecutionEvidenceUnavailable) or not evidence.reason.startswith(_MISSING_BASIS_PREFIX):
        return NoMissingBasis()
    try:
        diagnostic = hana_result.object_fields(cast(object, json.loads(evidence.reason.removeprefix(_MISSING_BASIS_PREFIX))))
        commits = diagnostic.get('commits')
        if diagnostic.get('repository') != request.manifest.repository or not isinstance(commits, list) or not commits:
            return NoMissingBasis()
        values = cast(list[object], commits)
        if not all(isinstance(value, str) and value in request.manifest.basis_commits for value in values):
            return NoMissingBasis()
        return hana_source.MissingBasis(request.manifest.repository, tuple(cast(list[str], values)))
    except (ValueError, KeyError, TypeError):
        return NoMissingBasis()


def dispatch_publication_attempted(directory: Path, request: HanaExecutionRequest) -> bool:
    return _read_journal(directory, request.key.claim_id)['publication_attempted'] is True


def _checked_observation(row: dict[str, object], request: HanaExecutionRequest
                         ) -> HanaCommandObservation | HanaObservationUnavailable:
    try:
        observation = decode_observation(_encode(hana_result.object_fields(row['observation'])))
        if observation.key != request.key or observation.slot != request.slot:
            raise ValueError('The Hana observation belongs to another dispatch')
        return observation
    except (OSError, ValueError, KeyError, TypeError):
        return HanaObservationUnavailable('The Hana command has no checked observation')


def read_dispatch_observation(directory: Path, request: HanaExecutionRequest) -> HanaCommandObservation | HanaObservationUnavailable:
    try:
        return _checked_observation(_read_journal(directory, request.key.claim_id), request)
    except (OSError, ValueError, KeyError, TypeError):
        return HanaObservationUnavailable('The Hana command has no checked observation')


def prepare_execution(adopted: hana_placement.HanaClaimAdopted, sender: SourceCapture,
                      source: Path, argv: tuple[str, ...], link: LogicalStepLink, *,
                      current_source_key: Callable[[], placement.SourceIdentity],
                      environment: dict[str, str], features: tuple[str, ...] = ()) -> HanaExecutionRequest | ExecutionPreparationRefused:
    """Capture exact source and bind it to the checked assignment before permission consumption."""
    from scripts.aws_target.hana_placement import generation_text

    assignment = adopted.reserved.assignment
    claim = adopted.reserved.claim
    expected = placement.assigned_claim_id(assignment.episode, assignment.attempt)
    if (claim.reference.claim_id != expected or claim.reference.episode != assignment.episode
            or claim.reference.attempt != assignment.attempt or claim.reference.target != assignment.destination.target
            or claim.reference.scope != assignment.destination.scope or claim.session != assignment.session
            or claim.generation != generation_text(adopted.generation)
            or not isinstance(claim.holder, placement.ClaimHolderIdentity) or assignment.destination.slot < 1
            or not isinstance(assignment.source_key, placement.SourceKeyKnown)
            or current_source_key() != assignment.source_key or link.episode != assignment.episode):
        return ExecutionPreparationRefused('The Hana assignment binding is invalid')
    if not judgment_command(argv):
        return ExecutionPreparationRefused('The Hana target permits only check and clippy judgments')
    manifest = sender.prepare(source, argv, environment=environment, features=features)
    if current_source_key() != assignment.source_key:
        return ExecutionPreparationRefused('The live source changed during capture')
    return HanaExecutionRequest(HanaAttemptKey(assignment.episode, assignment.attempt, expected, adopted.generation),
        manifest, assignment.destination.slot, assignment.source_key, argv, link)


def judgment_command(argv: tuple[str, ...]) -> bool:
    """Refuse wrappers, repairs, and commands that return build artifacts."""
    if len(argv) < 2 or argv[:2] not in (('cargo', 'check'), ('cargo', 'clippy')):
        return False
    denied = ('--fix', '--target-dir', '--manifest-path', '--config', '--out-dir', '--build-plan', '--message-format')
    return not any(word.split('=', 1)[0] in denied or '\0' in word for word in argv[2:])


def _state(row: dict[str, object], key: HanaAttemptKey) -> HanaDispatchState:
    evidence = _decode_evidence(row['evidence'])
    states = {'prepared': DispatchPrepared, 'sent': DispatchSent, 'acknowledged': DispatchAcknowledged,
              'fenced': DispatchFenced, 'settled': DispatchSettled}
    return states[cast(str, row['phase'])](key, evidence)


def dispatch_execution(directory: Path, request: HanaExecutionRequest, adopted: hana_placement.HanaClaimAdopted,
                       objects: ExecutionObjects, *,
                       consume: Callable[[wire.ConsumePermission], wire.FirstAuthorization | wire.DispatchStatus],
                       current_permission: Callable[[], hana_box.CommandPermission | hana_box.ClaimRefused],
                       current_source_key: Callable[[], placement.SourceIdentity]) -> HanaDispatchState:
    """Persist the dispatch before consuming permission and publish its command at most once."""
    from importlib import import_module
    from scripts.build_queue import admission_protocol as wire
    from uuid import uuid4

    execution = request.key.claim_id
    assignment = adopted.reserved.assignment
    if (request.key != HanaAttemptKey(assignment.episode, assignment.attempt,
            placement.assigned_claim_id(assignment.episode, assignment.attempt), adopted.generation)
            or request.slot != assignment.destination.slot or request.source_key != assignment.source_key
            or adopted.reserved.claim.reference.claim_id != execution or not judgment_command(request.argv)):
        raise ValueError('The Hana dispatch differs from the adopted assignment')
    with _journal_lock(directory, execution):
        if execution_path(directory, execution).exists():
            return _state(_read_journal(directory, execution), request.key)
        _ = encode_request(request)
        row: dict[str, object] = {'version': 1, 'request': request_fields(request), 'phase': 'prepared',
            'evidence': _evidence_fields(placement.ExecutionNotStarted()), 'publication_attempted': False,
            'command_settled': False}
        _save_journal(directory, execution, row)
        row['phase'] = 'sent'
        _save_journal(directory, execution, row)
    if current_source_key() != request.source_key:
        return DispatchSent(request.key, placement.ExecutionNotStarted())
    holder = adopted.reserved.claim.holder
    if not isinstance(holder, placement.ClaimHolderIdentity):
        return DispatchSent(request.key, placement.ExecutionNotStarted())
    try:
        authorization = consume(wire.ConsumePermission(uuid4().hex, adopted.reserved.assignment,
                                                       adopted.reserved.claim.reference, holder))
        if (not isinstance(authorization, wire.FirstAuthorization) or authorization.assignment != adopted.reserved.assignment
                or authorization.claim != adopted.reserved.claim.reference):
            return DispatchSent(request.key, placement.ExecutionNotStarted())
        permission = current_permission()
        permission_type = cast('type[hana_box.CommandPermission]', import_module('scripts.aws_target.hana_box').CommandPermission)
        if (not isinstance(permission, permission_type) or permission.execution != execution
                or permission.boot.boot_id != request.key.generation.boot_id
                or permission.boot.start_token != request.key.generation.start_token
                or current_source_key() != request.source_key):
            return DispatchSent(request.key, placement.ExecutionNotStarted())
        envelope = HanaCommandEnvelope(request, WorkPermit(permission.expires, str(permission.generation),
                                       permission.boot.boot_id), objects.execution_access(request.key))
        _ = encode_envelope(envelope)
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            if row['phase'] != 'sent' or row['publication_attempted']:
                return _state(row, request.key)
            row['publication_attempted'] = True
            import time
            row['evidence'] = _evidence_fields(placement.ExecutionEvidenceUnavailable(
                'The Hana executable publication has no acknowledgement', time.time()))
            _save_journal(directory, execution, row)
        objects.publish_execution(envelope)
    except (OSError, ValueError, RuntimeError, ConnectionError):
        # The saved publication identity remains authoritative after a lost reply.
        pass
    with _journal_lock(directory, execution):
        return _state(_read_journal(directory, execution), request.key)


def observe_execution(directory: Path, request: HanaExecutionRequest, objects: ExecutionObjects) -> placement.ExecutionEvidence:
    """Observe the same dispatch without creating another executable publication."""
    import time

    try:
        observation = objects.observe_execution(request.key)
        if observation.key != request.key or observation.slot != request.slot:
            raise ValueError('The Hana observation belongs to another attempt or slot')
        with _journal_lock(directory, request.key.claim_id):
            row = _read_journal(directory, request.key.claim_id)
            if row['phase'] == 'settled':
                return _decode_evidence(row['evidence'])
            row['observation'] = hana_result.object_fields(cast(object, json.loads(encode_observation(observation))))
            row['evidence'] = _evidence_fields(observation.evidence)
            if observation.accepted and row['phase'] == 'sent':
                row['phase'] = 'acknowledged'
            _save_journal(directory, request.key.claim_id, row)
        return observation.evidence
    except (OSError, ValueError, RuntimeError, ConnectionError):
        return placement.ExecutionEvidenceUnavailable('The Hana command observation is unavailable', time.time())


def publish_source_update(directory: Path, request: HanaExecutionRequest, manifest: SourceManifest,
                          objects: ExecutionObjects) -> bool:
    """Repair the captured source once without consuming permission or publishing another command."""
    original = request.manifest
    if (manifest.basis_commits or (manifest.repository, manifest.commit, manifest.argv, manifest.features,
            manifest.environment, manifest.toolchain) != (original.repository, original.commit, original.argv,
            original.features, original.environment, original.toolchain)):
        raise ValueError('The Hana source update changes the captured source or command')
    envelope = HanaSourceUpdateEnvelope(request.key, request.slot, manifest, objects.execution_access(request.key))
    _ = encode_envelope(envelope)
    with _journal_lock(directory, request.key.claim_id):
        row = _read_journal(directory, request.key.claim_id)
        if row['phase'] not in ('sent', 'acknowledged') or row.get('source_update_attempted') is True:
            return False
        if row['publication_attempted'] is not True:
            raise ValueError('The Hana source update has no original command dispatch')
        observation = read_dispatch_observation(directory, request)
        if (not isinstance(observation, HanaCommandObservation) or not observation.accepted
                or not observation.terminated or not observation.lease_free
                or isinstance(missing_basis(observation, request), NoMissingBasis)):
            raise ValueError('The Hana source update has no settled MissingBasis refusal')
        row['source_update_attempted'] = True
        row['source_update_manifest'] = asdict(manifest)
        _save_journal(directory, request.key.claim_id, row)
    objects.publish_execution(envelope)
    return True


def accept_result(directory: Path, request: HanaExecutionRequest, content: bytes, host: str,
                  current_source_key: Callable[[], placement.SourceIdentity]) -> hana_result.HanaResultDecision:
    """Persist one checked terminal result before the recorder or verify adapter uses it."""
    import base64

    result = hana_result.decode_result(content)
    expected = hana_result.HanaResultExpectation(request.key, request.source_key.key, request.manifest.commit,
        request.manifest.toolchain, host, request.argv, request.slot)
    with _journal_lock(directory, request.key.claim_id):
        row = _read_journal(directory, request.key.claim_id)
        live = current_source_key()
        encoded = base64.b64encode(hana_result.encode_result(result)).decode('ascii')
        replay = row.get('accepted_result') == encoded and row.get('phase') == 'settled' and _terminal(row) == placement.AttemptCompleted(result.status)
        decision = hana_result.validate_result(result, expected,
            live.key if isinstance(live, placement.SourceKeyKnown) else '', fenced=row['phase'] in ('fenced', 'settled') and not replay)
        if isinstance(decision, hana_result.RejectedHanaResult):
            return decision
        encoded = base64.b64encode(hana_result.encode_result(decision.result)).decode('ascii')
        if 'accepted_result' in row and row['accepted_result'] != encoded:
            return hana_result.RejectedHanaResult('The Hana attempt already has another terminal result')
        row['accepted_result'] = encoded
        _save_journal(directory, request.key.claim_id, row)
        return decision


def replay_accepted_result(directory: Path, request: HanaExecutionRequest, host: str,
                           current_source_key: Callable[[], placement.SourceIdentity]) -> hana_result.HanaResultDecision:
    """Recover the saved result without another command or another terminal decision."""
    import base64

    row = _read_journal(directory, request.key.claim_id)
    encoded = row.get('accepted_result')
    if not isinstance(encoded, str):
        return hana_result.RejectedHanaResult('The Hana dispatch has no accepted terminal result')
    return accept_result(directory, request, base64.b64decode(encoded, validate=True), host, current_source_key)


def _terminal(row: dict[str, object]) -> placement.TerminalAttemptOutcome:
    outcome = hana_result.object_fields(row['outcome'])
    if outcome.get('kind') == 'completed':
        return placement.AttemptCompleted(hana_result.integer_field(outcome, 'status'))
    reason = hana_result.string_field(outcome, 'reason')
    if outcome.get('kind') == 'cancelled':
        return placement.AttemptCancelled(reason)
    if outcome.get('kind') == 'prelaunch':
        return placement.PrelaunchRefused(reason)
    if outcome.get('kind') == placement.HANA_COMMAND_ADMISSION_REFUSED:
        return placement.HanaCommandAdmissionRefused(reason)
    if outcome.get('kind') not in ('declined', 'guest-ended'):
        raise ValueError('The Hana settlement outcome is invalid')
    return placement.AttemptDeclined(reason)


def _settled_dispatch(row: dict[str, object]) -> HanaSettled:
    outcome = _terminal(row)
    evidence = _decode_evidence(row['evidence'])
    if isinstance(outcome, placement.HanaCommandAdmissionRefused):
        if not isinstance(evidence, placement.ExecutionNotStarted):
            raise ValueError('The Hana admission refusal has invalid evidence')
        return HanaAdmissionRefusalSettled(outcome, evidence)
    if hana_result.object_fields(row['outcome']).get('kind') == 'guest-ended':
        if (not isinstance(outcome, placement.AttemptDeclined)
                or not isinstance(evidence, placement.ExecutionEvidenceUnavailable)):
            raise ValueError('The Hana guest departure has invalid evidence')
        return HanaGuestEndedWithoutResult(outcome, evidence)
    return HanaSettled(outcome, evidence)


@dataclass(frozen=True)
class NoCommandAdmissionRefusal:
    """The observation does not prove a command admission refusal."""


def command_admission_refusal(observation: HanaCommandObservation
                              ) -> placement.HanaCommandAdmissionRefused | NoCommandAdmissionRefusal:
    """Classify a command admission refusal after the guest confirms departure."""
    evidence = observation.evidence
    if (not observation.accepted or not observation.terminated or not observation.lease_free
            or observation.source_updated or not isinstance(evidence, placement.ExecutionEvidenceUnavailable)):
        return NoCommandAdmissionRefusal()
    # The old guest image sends only this exact reason for an admission refusal.
    if (isinstance(observation.refusal_code, CommandAdmissionRefusalCode)
            or evidence.reason == 'The data mount and a fresh permit are required'):
        return placement.HanaCommandAdmissionRefused(' '.join(evidence.reason.splitlines()))
    return NoCommandAdmissionRefusal()


def settle_execution(directory: Path, request: HanaExecutionRequest, objects: ExecutionObjects, *,
                     release_claim: Callable[[], bool], reason: str, cancelled: bool = False) -> HanaSettlement:
    """Require exact command closure before lifecycle release."""
    import base64

    execution = request.key.claim_id
    try:
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            if row['phase'] == 'settled':
                return _settled_dispatch(row)
            row['phase'] = 'fenced'
            _save_journal(directory, execution, row)
            published = row['publication_attempted'] is True
            command_settled = row['command_settled'] is True
        guest_ended = False
        refusal: placement.HanaCommandAdmissionRefused | NoCommandAdmissionRefusal = NoCommandAdmissionRefusal()
        if published and not command_settled:
            observation = _checked_observation(row, request)
            if (not cancelled and 'accepted_result' not in row and row.get('source_update_attempted') is not True
                    and isinstance(observation, HanaCommandObservation) and observation.accepted
                    and observation.terminated and observation.lease_free and not observation.source_updated
                    and isinstance(observation.evidence, placement.ExecutionEvidenceUnavailable)):
                # The guest records acceptance before launch and refuses duplicate delivery.
                evidence = observation.evidence
                guest_ended = True
                refusal = command_admission_refusal(observation)
            else:
                fence = HanaFenceEnvelope(request.key, request.slot, objects.execution_access(request.key))
                objects.publish_execution(fence)
                observation = objects.observe_execution(request.key)
                if (observation.key != request.key or observation.slot != request.slot or not observation.fenced
                        or not observation.lease_free or observation.accepted and not observation.terminated):
                    return HanaSettlementPending('The Hana fence, command termination, or lease release remains unconfirmed')
                evidence = observation.evidence
        else:
            evidence = _decode_evidence(row['evidence'])
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            if row['phase'] == 'settled':
                return _settled_dispatch(row)
            if 'outcome' not in row:
                if cancelled:
                    outcome: dict[str, object] = {'kind': 'cancelled', 'reason': reason}
                elif 'accepted_result' in row:
                    result = hana_result.decode_result(base64.b64decode(cast(str, row['accepted_result']), validate=True))
                    outcome = {'kind': 'completed', 'status': result.status}
                elif isinstance(refusal, placement.HanaCommandAdmissionRefused):
                    outcome = {'kind': placement.HANA_COMMAND_ADMISSION_REFUSED, 'reason': refusal.reason}
                    evidence = placement.ExecutionNotStarted()
                    row['phase'] = 'settled'
                elif guest_ended and isinstance(evidence, placement.ExecutionEvidenceUnavailable):
                    cause = ' '.join(evidence.reason.splitlines())
                    if cause == COMMAND_RESULT_PENDING or cause.startswith(_MISSING_BASIS_PREFIX):
                        cause = ' '.join(reason.splitlines())
                    outcome = {'kind': 'guest-ended', 'reason': cause}
                else:
                    outcome = {'kind': 'declined' if published else 'prelaunch', 'reason': reason}
                row['outcome'] = outcome
            row['command_settled'] = True
            row['evidence'] = _evidence_fields(evidence)
            _save_journal(directory, execution, row)
            if row['phase'] == 'settled':
                # The controller settles the claim after the local holder exits.
                return _settled_dispatch(row)
        if not release_claim():
            retained = _settled_dispatch(row)
            if isinstance(retained, HanaGuestEndedWithoutResult):
                return HanaGuestEndedClaimRetained(retained.outcome.reason)
            return HanaSettlementPending('The exact Hana lifecycle claim remains retained')
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            row['phase'] = 'settled'
            _save_journal(directory, execution, row)
            return _settled_dispatch(row)
    except (OSError, ValueError, RuntimeError, ConnectionError, KeyError, TypeError):
        return HanaSettlementPending('The Hana dispatch or cleanup observation is unavailable')


def reject_settled_result(directory: Path, request: HanaExecutionRequest, reason: str) -> HanaSettlement:
    """Save a final result rejection before the queue can receive a completed outcome."""
    try:
        with _journal_lock(directory, request.key.claim_id):
            row = _read_journal(directory, request.key.claim_id)
            if (row['phase'] != 'settled'
                    or decode_request(_encode(hana_result.object_fields(row['request']))) != request):
                return HanaSettlementPending('The Hana result rejection has no exact settled dispatch')
            if isinstance(_terminal(row), placement.AttemptCompleted):
                row['outcome'] = {'kind': 'declined', 'reason': reason}
                _save_journal(directory, request.key.claim_id, row)
            return _settled_dispatch(row)
    except (OSError, ValueError, KeyError, TypeError):
        return HanaSettlementPending('The Hana result rejection could not be saved')


def record_attempt_charge(directory: Path, request: HanaExecutionRequest, transfers: list[TransferBytes],
                          started_at: float, finished_at: float) -> None:
    """Persist measured transfer links without creating another dollar total."""
    from scripts.aws_target.ledger import CostLedger

    book = CostLedger(directory)
    with _journal_lock(directory, request.key.claim_id):
        row = _read_journal(directory, request.key.claim_id)
        if 'charge' in row:
            charge = hana_result.decode_attempt_charge(row['charge'])
        else:
            snapshot = book.read_snapshot()
            references = tuple('hana_compute:' + str(exposure.generation) for exposure in snapshot.hana_compute
                if exposure.at <= finished_at and exposure.expires >= started_at)
            charge = hana_result.HanaAttemptCharge(request.key, request.link,
                sum(transfer['sent_bytes'] for transfer in transfers),
                sum(transfer['received_bytes'] for transfer in transfers), len(transfers), started_at, finished_at, references)
            row['charge'] = hana_result.encode_attempt_charge(charge)
            _save_journal(directory, request.key.claim_id, row)
    book.record_hana_attempt(charge)
