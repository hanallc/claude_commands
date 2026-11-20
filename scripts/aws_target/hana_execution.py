"""Dispatch one assigned Hana command and retain its execution until settlement."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import time
from typing import TYPE_CHECKING, Protocol, cast

from scripts.build_queue import execution_identity
from scripts.aws_target.hana_guest import WorkPermit
from scripts.aws_target.hana_placement import HanaClaimRelease, HanaClaimReleaseRefused
from scripts.aws_target import hana_protocol, hana_result, hana_source
from scripts.aws_target.hana_result import HanaAttemptKey, LogicalStepLink
from scripts.build_queue import placement

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import ExitStack
    from pathlib import Path
    from scripts.aws_target import hana_box, hana_placement
    from scripts.build_queue import admission_protocol as wire
    from scripts.buildlog.sync import TransferBytes

_MISSING_BASIS_PREFIX = 'MissingBasis: '


@dataclass(frozen=True)
class ExecutionPreparationRefused:
    reason: str


@dataclass(frozen=True)
class DispatchSent:
    key: HanaAttemptKey
    evidence: execution_identity.ExecutionEvidence


@dataclass(frozen=True)
class DispatchAcknowledged:
    key: HanaAttemptKey
    evidence: execution_identity.ExecutionEvidence


@dataclass(frozen=True)
class DispatchFenced:
    key: HanaAttemptKey
    evidence: execution_identity.ExecutionEvidence


@dataclass(frozen=True)
class DispatchSettled:
    key: HanaAttemptKey
    evidence: execution_identity.ExecutionEvidence


HanaDispatchState = DispatchSent | DispatchAcknowledged | DispatchFenced | DispatchSettled


@dataclass(frozen=True)
class HanaSettled:
    outcome: placement.TerminalAttemptOutcome
    evidence: execution_identity.ExecutionEvidence


@dataclass(frozen=True)
class HanaGuestEndedWithoutResult(HanaSettled):
    """The guest confirmed departure and the sender has no accepted compiler result."""
    outcome: placement.AttemptDeclined
    evidence: execution_identity.ExecutionEvidenceUnavailable


@dataclass(frozen=True)
class HanaAdmissionRefusalSettled(HanaSettled):
    """The admission check confirmed that the command did not start."""
    outcome: execution_identity.HanaCommandAdmissionRefused
    evidence: execution_identity.ExecutionNotStarted


@dataclass(frozen=True)
class HanaSettlementPending:
    reason: str


@dataclass(frozen=True)
class HanaCleanupPending(HanaSettlementPending):
    """The journal confirms the command outcome and cleanup remains pending."""
    command: HanaSettled


HanaSettlement = HanaSettled | HanaSettlementPending


@dataclass(frozen=True)
class FenceAcknowledgementWindow:
    """The monotonic deadline and poll interval for the exact fence acknowledgement."""
    deadline: float
    poll_seconds: int


@dataclass(frozen=True)
class SavedTerminalProofOnly:
    """Settle with journal evidence and no provider access."""


class ExecutionObjects(Protocol):
    """Keep executable publication separate from retriable observations and fences."""
    def boot_control_deadline(self, key: HanaAttemptKey) -> int: ...
    def execution_access(self, key: HanaAttemptKey) -> hana_protocol.HanaExecutionAccess: ...
    def publish_execution(self, content: bytes) -> None: ...
    def observe_execution(self, key: HanaAttemptKey, *, timeout_s: float | None = None) -> hana_protocol.HanaExecutionObservation: ...
    def execution_result(self, key: HanaAttemptKey) -> bytes: ...


class SourceCapture(Protocol):
    """Stage the source and supply its upload operation."""
    def prepare(self, source: Path, argv: tuple[str, ...], *, environment: Mapping[str, str],
                staging: ExitStack, features: tuple[str, ...] = ()) -> tuple[hana_protocol.SourceManifest, Callable[[], None]]: ...


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
        claim_id = _journal_request(row).key.claim_id
    if (row.get('version') != 1 or claim_id != execution
            or row.get('phase') not in ('prepared', 'sent', 'acknowledged', 'fenced', 'settled')
            or type(row.get('publication_attempted')) is not bool or type(row.get('command_settled')) is not bool):
        raise ValueError('The Hana execution journal is invalid')
    _ = hana_protocol.decode_evidence(row['evidence'])
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
    return isinstance(read_claim_admission_refusal(directory, reference, require_release=True),
                      execution_identity.HanaCommandAdmissionRefused)


def read_claim_admission_refusal(directory: Path, reference: placement.ProviderClaimReference, *,
                                 require_release: bool
                                 ) -> execution_identity.HanaCommandAdmissionRefused | hana_protocol.NoCommandAdmissionRefusal:
    """Read the command refusal and check claim release when required."""
    try:
        row = _read_journal(directory, reference.claim_id)
        if (row['command_settled'] is True and (not require_release or row['phase'] == 'settled')
                and _journal_attempt(row) == (reference.episode, reference.attempt)):
            closed = _settled_dispatch(row)
            if isinstance(closed, HanaAdmissionRefusalSettled):
                return closed.outcome
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return hana_protocol.NO_COMMAND_ADMISSION_REFUSAL


def read_claim_execution(directory: Path, reference: placement.ProviderClaimReference) -> execution_identity.ExecutionEvidence:
    """Supply durable remote evidence without using the local holder as proof of departure."""
    import time

    try:
        row = _read_journal(directory, reference.claim_id)
        if _journal_attempt(row) != (reference.episode, reference.attempt):
            raise ValueError('The Hana execution journal belongs to another attempt')
        return hana_protocol.decode_evidence(row['evidence'])
    except (OSError, ValueError, KeyError, TypeError):
        return execution_identity.ExecutionEvidenceUnavailable('The Hana execution journal is unavailable', time.time())


def _journal_attempt(row: dict[str, object]) -> tuple[str, str]:
    if row.get('kind') == 'unpublished':
        return hana_result.string_field(row, 'episode'), hana_result.string_field(row, 'attempt')
    request = _journal_request(row)
    return request.key.episode, request.key.attempt


def _journal_request(row: dict[str, object]) -> hana_protocol.HanaExecutionRequest:
    return hana_protocol.decode_request(hana_protocol.encode_document(hana_result.object_fields(row['request'])))


def settle_unpublished_claim(directory: Path, reference: placement.ProviderClaimReference, *,
                             release_claim: Callable[[], HanaClaimRelease], reason: str | execution_identity.HanaCommandAdmissionRefused,
                             cancelled: bool = False) -> HanaSettlement:
    """Close an unpublished claim or resume release of a confirmed admission refusal."""
    execution = reference.claim_id
    try:
        with _journal_lock(directory, execution):
            if execution_path(directory, execution).exists():
                row = _read_journal(directory, execution)
                if (_journal_attempt(row) != (reference.episode, reference.attempt)
                        or row.get('kind') != 'unpublished' and (row['command_settled'] is not True
                            or not isinstance(_terminal(row), execution_identity.HanaCommandAdmissionRefused))):
                    return HanaSettlementPending('The Hana attempt already has a command dispatch')
            else:
                refusal = isinstance(reason, execution_identity.HanaCommandAdmissionRefused) and not cancelled
                kind = 'hana_command_admission_refused' if refusal else 'cancelled' if cancelled else 'prelaunch'
                detail = reason if isinstance(reason, str) else reason.reason
                row: dict[str, object] = {'version': 1, 'kind': 'unpublished', 'claim_id': execution,
                    'episode': reference.episode, 'attempt': reference.attempt, 'phase': 'fenced',
                    'evidence': hana_protocol.encode_evidence(execution_identity.ExecutionNotStarted()), 'publication_attempted': False,
                    'command_settled': True, 'outcome': {'kind': kind, 'reason': detail}}
                _save_journal(directory, execution, row)
            if row['phase'] == 'settled':
                return _settled_dispatch(row)
        released = release_claim()
        if isinstance(released, HanaClaimReleaseRefused):
            return HanaCleanupPending(released.reason, _settled_dispatch(row))
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            row['phase'] = 'settled'
            _save_journal(directory, execution, row)
            return _settled_dispatch(row)
    except (OSError, ValueError, RuntimeError, ConnectionError, KeyError, TypeError):
        return HanaSettlementPending('The Hana prelaunch claim settlement is unavailable')


@dataclass(frozen=True)
class HanaObservationUnavailable:
    reason: str


@dataclass(frozen=True)
class NoMissingBasis:
    """The observation has no valid request for complete captured source."""


def missing_basis(observation: hana_protocol.HanaCommandObservation, request: hana_protocol.HanaExecutionRequest) -> hana_source.MissingBasis | NoMissingBasis:
    """Decode the guest diagnostic without accepting a different repository or basis."""
    evidence = observation.evidence
    if (not isinstance(evidence, execution_identity.ExecutionEvidenceUnavailable)
            or not evidence.reason.startswith(_MISSING_BASIS_PREFIX)):
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


def dispatch_publication_attempted(directory: Path, request: hana_protocol.HanaExecutionRequest) -> bool:
    return _read_journal(directory, request.key.claim_id)['publication_attempted'] is True


def _checked_observation(row: dict[str, object], request: hana_protocol.HanaExecutionRequest
                         ) -> hana_protocol.HanaExecutionObservation | HanaObservationUnavailable:
    try:
        observation = hana_protocol.decode_observation(hana_protocol.encode_document(hana_result.object_fields(row['observation'])))
        if observation.key != request.key or observation.slot != request.slot:
            raise ValueError('The Hana observation belongs to another dispatch')
        return observation
    except (OSError, ValueError, KeyError, TypeError):
        return HanaObservationUnavailable('The Hana command has no checked observation')


def read_dispatch_observation(directory: Path, request: hana_protocol.HanaExecutionRequest) -> hana_protocol.HanaExecutionObservation | HanaObservationUnavailable:
    try:
        return _checked_observation(_read_journal(directory, request.key.claim_id), request)
    except (OSError, ValueError, KeyError, TypeError):
        return HanaObservationUnavailable('The Hana command has no checked observation')


def dispatch_claim_boot_matches(directory: Path, reference: placement.ProviderClaimReference, generation: str) -> bool:
    """Require a published dispatch and its retained claim to name the same boot."""
    from scripts.aws_target.hana_placement import generation_text

    try:
        row = _read_journal(directory, reference.claim_id)
        if row['publication_attempted'] is False:
            return True
        request = _journal_request(row)
        return ((request.key.episode, request.key.attempt) == (reference.episode, reference.attempt)
                and generation == generation_text(request.key.generation)
                and isinstance(_checked_observation(row, request), hana_protocol.HanaExecutionObservation))
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prepare_execution(adopted: hana_placement.HanaClaimAdopted, sender: SourceCapture,
                      source: Path, argv: tuple[str, ...], link: LogicalStepLink, *,
                      current_source_key: Callable[[], execution_identity.SourceIdentity],
                      environment: dict[str, str], staging: ExitStack, features: tuple[str, ...] = ()
                      ) -> (tuple[hana_protocol.HanaExecutionRequest, Callable[[], None]]
                            | ExecutionPreparationRefused | execution_identity.HanaCommandAdmissionRefused):
    """Capture exact source and bind it to the checked assignment before permission consumption."""
    from scripts.aws_target.hana_placement import generation_text

    assignment = adopted.reserved.assignment
    claim = adopted.reserved.claim
    expected = execution_identity.assigned_claim_id(assignment.episode, assignment.attempt)
    if (claim.reference.claim_id != expected or claim.reference.episode != assignment.episode
            or claim.reference.attempt != assignment.attempt or claim.reference.target != assignment.destination.target
            or claim.reference.scope != assignment.destination.scope or claim.session != assignment.session
            or claim.generation != generation_text(adopted.generation)
            or not isinstance(claim.holder, placement.ClaimHolderIdentity) or assignment.destination.slot < 1
            or not isinstance(assignment.source_key, execution_identity.SourceKeyKnown) or link.episode != assignment.episode):
        return execution_identity.HanaCommandAdmissionRefused('The Hana assignment binding is invalid')
    if current_source_key() != assignment.source_key:
        return ExecutionPreparationRefused('The live source changed before capture')
    if not judgment_command(argv):
        return execution_identity.HanaCommandAdmissionRefused('The Hana target permits only check and clippy judgments')
    manifest, upload = sender.prepare(source, argv, environment=environment, staging=staging, features=features)
    if current_source_key() != assignment.source_key:
        return ExecutionPreparationRefused('The live source changed during capture')
    request = hana_protocol.HanaExecutionRequest(HanaAttemptKey(assignment.episode, assignment.attempt, expected, adopted.generation),
        manifest, assignment.destination.slot, assignment.source_key, argv, link)
    return request, upload


def judgment_command(argv: tuple[str, ...]) -> bool:
    """Refuse wrappers, repairs, and commands that return build artifacts."""
    if len(argv) < 2 or argv[:2] not in (('cargo', 'check'), ('cargo', 'clippy')):
        return False
    denied = ('--fix', '--target-dir', '--manifest-path', '--config', '--out-dir', '--build-plan', '--message-format')
    return not any(word.split('=', 1)[0] in denied or '\0' in word for word in argv[2:])


def _state(row: dict[str, object], key: HanaAttemptKey) -> HanaDispatchState | execution_identity.HanaCommandAdmissionRefused:
    if row['phase'] in ('prepared', 'sent') and row['publication_attempted'] is False:
        return execution_identity.HanaCommandAdmissionRefused('The saved Hana dispatch ended before a publication attempt')
    evidence = hana_protocol.decode_evidence(row['evidence'])
    states = {'sent': DispatchSent, 'acknowledged': DispatchAcknowledged,
              'fenced': DispatchFenced, 'settled': DispatchSettled}
    return states[cast(str, row['phase'])](key, evidence)


def dispatch_execution(directory: Path, request: hana_protocol.HanaExecutionRequest, adopted: hana_placement.HanaClaimAdopted,
                       objects: ExecutionObjects, *,
                       upload_source: Callable[[], None],
                       consume: Callable[[wire.ConsumePermission], wire.FirstAuthorization | wire.DispatchStatus],
                       current_permission: Callable[[], hana_box.CommandPermission | hana_box.ClaimRefused],
                       current_source_key: Callable[[], execution_identity.SourceIdentity]
                       ) -> HanaDispatchState | execution_identity.HanaCommandAdmissionRefused:
    """Persist the dispatch before consuming permission and publish its command at most once."""
    from importlib import import_module
    from scripts.build_queue import admission_protocol as wire
    from uuid import uuid4

    execution = request.key.claim_id
    assignment = adopted.reserved.assignment
    if (request.key != HanaAttemptKey(assignment.episode, assignment.attempt,
            execution_identity.assigned_claim_id(assignment.episode, assignment.attempt), adopted.generation)
            or request.slot != assignment.destination.slot or request.source_key != assignment.source_key
            or adopted.reserved.claim.reference.claim_id != execution or not judgment_command(request.argv)):
        return execution_identity.HanaCommandAdmissionRefused('The Hana dispatch differs from the adopted assignment')
    try:
        hana_protocol.check_command_request(request)
    except (ValueError, RuntimeError, KeyError, TypeError) as error:
        cause = ' '.join(str(error).splitlines())
        return execution_identity.HanaCommandAdmissionRefused(f'The Hana command could not be prepared: {cause}')

    permission_type = cast('type[hana_box.CommandPermission]', import_module('scripts.aws_target.hana_box').CommandPermission)

    def permit_for_claim() -> WorkPermit | execution_identity.HanaCommandAdmissionRefused:
        permission = current_permission()
        if not isinstance(permission, permission_type):
            reason = cast('hana_box.ClaimRefused', permission).reason
            return execution_identity.HanaCommandAdmissionRefused(' '.join(reason.splitlines()))
        if (permission.execution != execution or permission.boot.boot_id != request.key.generation.boot_id
                or permission.boot.start_token != request.key.generation.start_token):
            return execution_identity.HanaCommandAdmissionRefused('The Hana command permission differs from the adopted claim')
        return WorkPermit(permission.expires, str(permission.generation), permission.boot.boot_id)

    def journal_refusal(error: Exception) -> execution_identity.HanaCommandAdmissionRefused:
        cause = ' '.join(str(error).splitlines()) or type(error).__name__
        return execution_identity.HanaCommandAdmissionRefused(f'The Hana dispatch journal is unavailable: {cause}')

    try:
        with _journal_lock(directory, execution):
            if execution_path(directory, execution).exists():
                return _state(_read_journal(directory, execution), request.key)
            row: dict[str, object] = {'version': 1, 'request': hana_protocol.request_fields(request), 'phase': 'prepared',
                'evidence': hana_protocol.encode_evidence(execution_identity.ExecutionNotStarted()), 'publication_attempted': False,
                'command_settled': False}
            _save_journal(directory, execution, row)
            row['phase'] = 'sent'
            _save_journal(directory, execution, row)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        return journal_refusal(error)
    if current_source_key() != request.source_key:
        return execution_identity.HanaCommandAdmissionRefused('The live source key no longer matches before Hana authorization')
    holder = adopted.reserved.claim.holder
    if not isinstance(holder, placement.ClaimHolderIdentity):
        return execution_identity.HanaCommandAdmissionRefused('The Hana claim has no confirmed holder')
    try:
        authorization = consume(wire.ConsumePermission(uuid4().hex, assignment, adopted.reserved.claim.reference, holder))
    except (OSError, ValueError, RuntimeError) as error:
        cause = ' '.join(str(error).splitlines()) or type(error).__name__
        return execution_identity.HanaCommandAdmissionRefused(f'Hana authorization failed: {cause}')
    if isinstance(authorization, wire.DispatchStatus):
        cause = ' '.join(authorization.reason.splitlines()) or 'The admission service returned status without a first authorization'
        return execution_identity.HanaCommandAdmissionRefused(cause)
    if authorization.assignment != assignment or authorization.claim != adopted.reserved.claim.reference:
        return execution_identity.HanaCommandAdmissionRefused('The Hana authorization differs from the adopted assignment or claim')
    try:
        permit = permit_for_claim()
        if isinstance(permit, execution_identity.HanaCommandAdmissionRefused):
            return permit
        if current_source_key() != request.source_key:
            return execution_identity.HanaCommandAdmissionRefused('The live source key no longer matches before Hana upload')
        access = objects.execution_access(request.key)
        content = hana_protocol.encode_envelope(hana_protocol.HanaCommandEnvelope(request, permit, access))
        upload_source()
        current = permit_for_claim()
        if isinstance(current, execution_identity.HanaCommandAdmissionRefused):
            return current
        if current_source_key() != request.source_key:
            return execution_identity.HanaCommandAdmissionRefused('The live source key no longer matches after Hana upload')
        if current != permit:
            content = hana_protocol.encode_envelope(hana_protocol.HanaCommandEnvelope(request, current, access))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        cause = ' '.join(str(error).splitlines()) or type(error).__name__
        return execution_identity.HanaCommandAdmissionRefused(f'The Hana command could not be prepared: {cause}')
    try:
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            if row['phase'] != 'sent' or row['publication_attempted']:
                return _state(row, request.key)
            row['publication_attempted'] = True
            row['evidence'] = hana_protocol.encode_evidence(execution_identity.ExecutionEvidenceUnavailable(
                'The Hana executable publication has no acknowledgement', time.time()))
            _save_journal(directory, execution, row)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        return journal_refusal(error)
    try:
        objects.publish_execution(content)
    except (OSError, ValueError, RuntimeError):
        # The saved publication identity remains authoritative after a lost reply.
        pass
    with _journal_lock(directory, execution):
        return _state(_read_journal(directory, execution), request.key)


def observe_execution(directory: Path, request: hana_protocol.HanaExecutionRequest, objects: ExecutionObjects) -> execution_identity.ExecutionEvidence:
    """Observe the same dispatch without creating another executable publication."""
    try:
        observation = objects.observe_execution(request.key)
        if observation.key != request.key or observation.slot != request.slot:
            raise ValueError('The Hana observation belongs to another attempt or slot')
        with _journal_lock(directory, request.key.claim_id):
            row = _read_journal(directory, request.key.claim_id)
            if row['command_settled'] is True:
                return hana_protocol.decode_evidence(row['evidence'])
            row['observation'] = hana_result.object_fields(cast(object, json.loads(hana_protocol.encode_observation(observation))))
            row['evidence'] = hana_protocol.encode_evidence(observation.evidence)
            if observation.accepted and row['phase'] == 'sent':
                row['phase'] = 'acknowledged'
            _save_journal(directory, request.key.claim_id, row)
        return observation.evidence
    except (OSError, ValueError, RuntimeError, ConnectionError):
        return execution_identity.ExecutionEvidenceUnavailable('The Hana command observation is unavailable', time.time())


def publish_source_update(directory: Path, request: hana_protocol.HanaExecutionRequest,
                          manifest: hana_protocol.SourceManifest, objects: ExecutionObjects, *,
                          upload_source: Callable[[], None]) -> bool:
    """Repair the captured source once without consuming permission or publishing another command."""
    original = request.manifest
    if manifest != replace(original, bundle_sha256=manifest.bundle_sha256,
                           bundle_url=manifest.bundle_url, basis_commits=()):
        raise ValueError('The Hana source update changes the captured source or command')
    envelope = hana_protocol.HanaSourceUpdateEnvelope(request.key, request.slot, manifest, objects.execution_access(request.key))
    content = hana_protocol.encode_envelope(envelope)
    with _journal_lock(directory, request.key.claim_id):
        row = _read_journal(directory, request.key.claim_id)
        if row['phase'] not in ('sent', 'acknowledged') or row.get('source_update_attempted') is True:
            return False
        if row['publication_attempted'] is not True:
            raise ValueError('The Hana source update has no original command dispatch')
        observation = read_dispatch_observation(directory, request)
        if (not isinstance(observation, hana_protocol.HanaCommandObservation) or not observation.accepted
                or not observation.terminated or not observation.lease_free
                or isinstance(missing_basis(observation, request), NoMissingBasis)):
            raise ValueError('The Hana source update has no settled MissingBasis refusal')
        upload_source()
        row['source_update_attempted'] = True
        row['source_update_manifest'] = asdict(manifest)
        _save_journal(directory, request.key.claim_id, row)
    objects.publish_execution(content)
    return True


def accept_result(directory: Path, request: hana_protocol.HanaExecutionRequest, content: bytes, host: str,
                  current_source_key: Callable[[], execution_identity.SourceIdentity]) -> hana_result.HanaResultDecision:
    """Persist one checked terminal result before the recorder or verify adapter uses it."""
    import base64

    result = hana_result.decode_result(content)
    expected = hana_result.HanaResultExpectation(request.key, request.source_key.key, request.manifest.commit,
        request.manifest.toolchain, host, request.argv, request.slot)
    with _journal_lock(directory, request.key.claim_id):
        row = _read_journal(directory, request.key.claim_id)
        if _journal_request(row) != request:
            return hana_result.RejectedHanaResult('The Hana journal differs from the dispatch')
        encoded = base64.b64encode(hana_result.encode_result(result)).decode('ascii')
        if 'accepted_result' in row:
            if row['accepted_result'] != encoded:
                return hana_result.RejectedHanaResult('The Hana attempt already has another terminal result')
            return hana_result.AcceptedHanaResult(result)
        live = current_source_key()
        decision = hana_result.validate_result(result, expected,
            live.key if isinstance(live, execution_identity.SourceKeyKnown) else '', fenced=row['phase'] in ('fenced', 'settled'))
        if isinstance(decision, hana_result.RejectedHanaResult):
            return decision
        row['accepted_result'] = encoded
        _save_journal(directory, request.key.claim_id, row)
        return decision


def replay_accepted_result(directory: Path, request: hana_protocol.HanaExecutionRequest) -> hana_result.HanaResultDecision:
    """Read the saved result decision for this exact dispatch."""
    row = _read_journal(directory, request.key.claim_id)
    if _journal_request(row) != request:
        return hana_result.RejectedHanaResult('The Hana journal differs from the dispatch')
    return _accepted_result(row)


def _accepted_result(row: dict[str, object]) -> hana_result.HanaResultDecision:
    import base64

    encoded = row.get('accepted_result')
    if not isinstance(encoded, str):
        return hana_result.RejectedHanaResult('The Hana dispatch has no accepted terminal result')
    return hana_result.AcceptedHanaResult(hana_result.decode_result(base64.b64decode(encoded, validate=True)))


def _terminal(row: dict[str, object]) -> placement.TerminalAttemptOutcome:
    outcome = hana_result.object_fields(row['outcome'])
    if outcome.get('kind') == 'completed':
        return placement.AttemptCompleted(hana_result.integer_field(outcome, 'status'))
    reason = hana_result.string_field(outcome, 'reason')
    if outcome.get('kind') == 'cancelled':
        return placement.AttemptCancelled(reason)
    if outcome.get('kind') == 'prelaunch':
        return placement.PrelaunchRefused(reason)
    if outcome.get('kind') == 'hana_command_admission_refused':
        return execution_identity.HanaCommandAdmissionRefused(reason)
    if outcome.get('kind') not in ('declined', 'guest-ended'):
        raise ValueError('The Hana settlement outcome is invalid')
    return placement.AttemptDeclined(reason)


def _settled_dispatch(row: dict[str, object]) -> HanaSettled:
    outcome = _terminal(row)
    evidence = hana_protocol.decode_evidence(row['evidence'])
    if isinstance(outcome, execution_identity.HanaCommandAdmissionRefused):
        if not isinstance(evidence, execution_identity.ExecutionNotStarted):
            raise ValueError('The Hana admission refusal has invalid evidence')
        return HanaAdmissionRefusalSettled(outcome, evidence)
    if hana_result.object_fields(row['outcome']).get('kind') == 'guest-ended':
        if (not isinstance(outcome, placement.AttemptDeclined)
                or not isinstance(evidence, execution_identity.ExecutionEvidenceUnavailable)):
            raise ValueError('The Hana guest departure has invalid evidence')
        return HanaGuestEndedWithoutResult(outcome, evidence)
    return HanaSettled(outcome, evidence)


def _finish_execution_settlement(directory: Path, request: hana_protocol.HanaExecutionRequest) -> HanaSettlement:
    """Record claim release after the journal confirms command settlement."""
    execution = request.key.claim_id
    with _journal_lock(directory, execution):
        row = _read_journal(directory, execution)
        if row['command_settled'] is not True or _journal_request(row) != request:
            return HanaSettlementPending('The Hana journal has no exact command settlement')
        settled = _settled_dispatch(row)
        if row['phase'] != 'settled':
            row['phase'] = 'settled'
            _save_journal(directory, execution, row)
        return settled


def replay_execution_settlement(directory: Path, assignment: placement.PlacementAssignment, *,
                                retained_generation: str | placement.NoProviderClaim,
                                release_claim: Callable[[], HanaClaimRelease]) -> HanaSettlement:
    """Recover the exact assigned dispatch from local proof."""
    from scripts.aws_target.hana_placement import generation_text

    reference = placement.assigned_claim_reference(assignment)
    try:
        row = _read_journal(directory, reference.claim_id)
        request = _journal_request(row)
        if ((request.key.episode, request.key.attempt) != (reference.episode, reference.attempt)
                or request.source_key != assignment.source_key or request.slot != assignment.destination.slot):
            return HanaSettlementPending('The Hana journal differs from the assignment')
        if isinstance(retained_generation, placement.NoProviderClaim) and row['command_settled'] is True:
            settled = _finish_execution_settlement(directory, request)
        else:
            if not isinstance(_checked_observation(row, request), hana_protocol.HanaExecutionObservation):
                return HanaSettlementPending('The Hana journal has no observation for this boot')
            if isinstance(retained_generation, str) and retained_generation != generation_text(request.key.generation):
                return HanaSettlementPending('The Hana claim belongs to a different boot')
            if isinstance(retained_generation, placement.NoProviderClaim):
                return HanaSettlementPending('The Hana claim is missing before command settlement')
            settled = settle_execution(directory, request, SavedTerminalProofOnly(), release_claim=release_claim,
                reason='The Hana command has no accepted result', fence_window=FenceAcknowledgementWindow(0, 1))
    except (OSError, ValueError, KeyError, TypeError):
        return HanaSettlementPending('The assigned Hana dispatch journal is unavailable')
    return settled


def _fence_acknowledged(observation: hana_protocol.HanaExecutionObservation) -> bool:
    return observation.fenced and observation.lease_free and (not observation.accepted or observation.terminated)


def _await_fence_acknowledgement(request: hana_protocol.HanaExecutionRequest, objects: ExecutionObjects,
                                window: FenceAcknowledgementWindow) -> hana_protocol.HanaExecutionObservation | HanaSettlementPending:
    while (remaining := window.deadline - time.monotonic()) > 0:
        try:
            observation = objects.observe_execution(request.key, timeout_s=remaining)
            if (observation.key == request.key and observation.slot == request.slot and _fence_acknowledged(observation)
                    and time.monotonic() <= window.deadline):
                return observation
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            # A later read can confirm the fence after a failed read.
            pass
        remaining = window.deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(window.poll_seconds, remaining))
    return HanaSettlementPending('The Hana fence, command termination, or lease release remains unconfirmed')


def settle_execution(directory: Path, request: hana_protocol.HanaExecutionRequest,
                     objects: ExecutionObjects | SavedTerminalProofOnly, *,
                     release_claim: Callable[[], HanaClaimRelease], reason: str, fence_window: FenceAcknowledgementWindow,
                     cancelled: bool = False, result_transfer_lost: bool = False,
                     admission_refusal: execution_identity.HanaCommandAdmissionRefused | hana_protocol.NoCommandAdmissionRefusal
                     = hana_protocol.NO_COMMAND_ADMISSION_REFUSAL) -> HanaSettlement:
    """Require exact command closure before lifecycle release."""
    unavailable = 'The Hana dispatch or cleanup observation is unavailable'
    command: HanaSettled | HanaSettlementPending = HanaSettlementPending(unavailable)

    def pending(detail: str) -> HanaSettlementPending:
        return HanaCleanupPending(detail, command) if isinstance(command, HanaSettled) else HanaSettlementPending(detail)

    execution = request.key.claim_id
    try:
        guest_ended = False
        refusal = admission_refusal
        fence_needed = False
        fence_observation: hana_protocol.HanaExecutionObservation | None = None
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            if (isinstance(objects, SavedTerminalProofOnly) and (_journal_request(row) != request
                    or not isinstance(_checked_observation(row, request), hana_protocol.HanaExecutionObservation))):
                return HanaSettlementPending('The Hana journal differs from the dispatch')
            if row['phase'] == 'settled':
                return _settled_dispatch(row)
            resume_fence = row['phase'] == 'fenced'
            published = row['publication_attempted'] is True
            command_settled = row['command_settled'] is True
            evidence = hana_protocol.decode_evidence(row['evidence'])
            accepted = _accepted_result(row)
            if command_settled:
                command = _settled_dispatch(row)
            elif isinstance(accepted, hana_result.AcceptedHanaResult):
                command = HanaSettled(placement.AttemptCompleted(accepted.result.status), evidence)
            if published and not command_settled:
                observation = _checked_observation(row, request)
                if isinstance(observation, hana_protocol.HanaExecutionObservation):
                    reported_refusal = hana_protocol.command_admission_refusal(observation)
                    if isinstance(reported_refusal, execution_identity.HanaCommandAdmissionRefused):
                        refusal = reported_refusal
                if (not cancelled and isinstance(accepted, hana_result.RejectedHanaResult)
                        and isinstance(observation, hana_protocol.ConfirmedCommandAdmissionRefusal)):
                    evidence = observation.evidence
                elif (not cancelled and isinstance(accepted, hana_result.RejectedHanaResult) and row.get('source_update_attempted') is not True
                        and isinstance(observation, hana_protocol.HanaCommandObservation) and observation.accepted
                        and observation.terminated and observation.lease_free and not observation.source_updated
                        and isinstance(observation.evidence, execution_identity.ExecutionEvidenceUnavailable)
                        and (isinstance(refusal, execution_identity.HanaCommandAdmissionRefused)
                             or not (resume_fence or result_transfer_lost or isinstance(objects, SavedTerminalProofOnly)))):
                    # The guest records acceptance before launch and refuses duplicate delivery.
                    evidence = observation.evidence
                    guest_ended = True
                elif (isinstance(objects, SavedTerminalProofOnly)
                        and isinstance(observation, hana_protocol.HanaExecutionObservation)
                        and _fence_acknowledged(observation)):
                    evidence = observation.evidence
                else:
                    fence_needed = True
            if (isinstance(objects, SavedTerminalProofOnly) and not command_settled
                    and (not published or fence_needed or 'outcome' in row)):
                return pending('The Hana journal has no exact terminal proof')
            row['phase'] = 'fenced'
            _save_journal(directory, execution, row)
        if fence_needed and not isinstance(objects, SavedTerminalProofOnly):
            fence = hana_protocol.HanaFenceEnvelope(request.key, request.slot, objects.execution_access(request.key))
            try:
                objects.publish_execution(hana_protocol.encode_envelope(fence))
            except (OSError, ValueError, RuntimeError):
                # An earlier PUT attempt can have saved the fence before the final error.
                pass
            observation = _await_fence_acknowledgement(request, objects, fence_window)
            if isinstance(observation, HanaSettlementPending):
                return pending(observation.reason)
            fence_observation = observation
            evidence = observation.evidence
            reported_refusal = hana_protocol.command_admission_refusal(observation)
            if isinstance(reported_refusal, execution_identity.HanaCommandAdmissionRefused):
                refusal = reported_refusal
        with _journal_lock(directory, execution):
            row = _read_journal(directory, execution)
            if row['phase'] == 'settled':
                return _settled_dispatch(row)
            if fence_observation is not None:
                saved_observation = _checked_observation(row, request)
                if isinstance(saved_observation, hana_protocol.HanaExecutionObservation):
                    timing: hana_protocol.HanaCommandTiming = {**saved_observation.timing, **fence_observation.timing}
                    fence_observation = replace(fence_observation, timing=timing)
                row['observation'] = hana_result.object_fields(cast(object,
                    json.loads(hana_protocol.encode_observation(fence_observation))))
            if 'outcome' not in row:
                if isinstance(command, HanaSettled) and isinstance(command.outcome, placement.AttemptCompleted):
                    outcome: dict[str, object] = {'kind': 'completed', 'status': command.outcome.status}
                elif cancelled:
                    outcome = {'kind': 'cancelled', 'reason': reason}
                elif isinstance(refusal, execution_identity.HanaCommandAdmissionRefused):
                    outcome = {'kind': 'hana_command_admission_refused', 'reason': refusal.reason}
                    evidence = execution_identity.ExecutionNotStarted()
                elif guest_ended and isinstance(evidence, execution_identity.ExecutionEvidenceUnavailable):
                    cause = ' '.join(evidence.reason.splitlines())
                    if cause == hana_protocol.COMMAND_RESULT_PENDING or cause.startswith(_MISSING_BASIS_PREFIX):
                        cause = ' '.join(reason.splitlines())
                    outcome = {'kind': 'guest-ended', 'reason': cause}
                else:
                    outcome = {'kind': 'declined' if published else 'prelaunch', 'reason': reason}
                row['outcome'] = outcome
            row['command_settled'] = True
            row['evidence'] = hana_protocol.encode_evidence(evidence)
            _save_journal(directory, execution, row)
            command = _settled_dispatch(row)
    except (OSError, ValueError, RuntimeError, ConnectionError, KeyError, TypeError):
        return pending(unavailable)
    try:
        released = release_claim()
        if isinstance(released, HanaClaimReleaseRefused):
            return pending(released.reason)
        finished = _finish_execution_settlement(directory, request)
        return pending(finished.reason) if isinstance(finished, HanaSettlementPending) else finished
    except (OSError, ValueError, RuntimeError, ConnectionError, KeyError, TypeError) as error:
        return pending(f'The Hana claim release or journal update failed ({type(error).__name__})')


def record_attempt_charge(directory: Path, request: hana_protocol.HanaExecutionRequest, transfers: list[TransferBytes],
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
