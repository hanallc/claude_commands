"""Share Hana request, observation, and runner outcome formats."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import ClassVar, Literal, TypedDict, cast
from urllib.parse import urlsplit

from scripts.aws_target import hana_result
from scripts.aws_target.hana_guest import CommandAdmissionRefusalCode, WorkPermit
from scripts.aws_target.hana_result import HanaAttemptKey, LogicalStepLink, decode_result, encode_result
from scripts.build_queue import execution_identity


CONTROL_MAX_BYTES = 65536
# Reserve space for the wrapper, the permit, and both signed publication URLs.
COMMAND_ENVELOPE_RESERVE_BYTES = 16 * 1024
COMMAND_RESULT_PENDING = 'The command result is pending'
RUNTIME_ABI = 3
REPOSITORY = 'hana'
COPY_UNIT = 'aws-hana'
COPY_MAX_KEY = 'hana.copies.max'
COPY_MAX_BYTES_KEY = 'hana.copies.max_bytes'


def main_checkout(home: Path) -> Path:
    """Select the guest's main checkout."""
    return home / 'rust' / REPOSITORY


def fence_settlement_seconds(control_timeout_s: int, poll_seconds: int) -> int:
    """Reserve time for sender requests and the guest's next fence pass."""
    # Allow three sender requests, the rest of the current guest pass, and the next fence reply.
    return 16 * control_timeout_s + poll_seconds


class SourceRefused(ValueError):
    """The source cannot satisfy the requested identity or transport bounds."""


@dataclass(frozen=True)
class SourceManifest:
    version: int
    repository: str
    commit: str
    bundle_sha256: str
    bundle_url: str = field(repr=False)
    basis_commits: tuple[str, ...]
    argv: tuple[str, ...]
    features: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    toolchain: str
    runtime_abi: int = field(kw_only=True)

    def encode(self) -> bytes:
        """Encode the immutable request within the control protocol bound."""
        content = json.dumps(asdict(self), sort_keys=True, separators=(',', ':')).encode()
        if len(content) > CONTROL_MAX_BYTES:
            raise SourceRefused('ManifestTooLarge: source manifest exceeds the control limit')
        return content


@dataclass(frozen=True)
class SourceLimits:
    git_timeout_s: int
    allowed_environment: tuple[str, ...]


@dataclass(frozen=True)
class MissingBasis:
    """The guest lacks commits needed to apply an incremental bundle."""
    repository: str
    commits: tuple[str, ...]


def validate_manifest(manifest: SourceManifest, limits: SourceLimits) -> None:
    """Reject unsafe paths, revisions, URLs, and inherited environment values."""
    _ = manifest.encode()
    if (manifest.version != 1 or type(manifest.runtime_abi) is not int or manifest.runtime_abi < 0
            or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]*', manifest.repository)
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', manifest.commit)
            or any(not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) for value in manifest.basis_commits)
            or not re.fullmatch(r'[0-9a-f]{64}', manifest.bundle_sha256)
            or not re.fullmatch(r'[0-9a-f]{64}', manifest.toolchain)
            or not manifest.argv or any('\0' in word for word in (*manifest.argv, *manifest.features))):
        raise SourceRefused('InvalidManifest: source identity or command is invalid')
    names = [name for name, _ in manifest.environment]
    if (len(set(names)) != len(names) or any(name not in limits.allowed_environment
            or name in ('HOME', 'PATH', 'CARGO_HOME', 'RUSTUP_HOME', 'CARGO_TARGET_DIR')
            or any(word in name.upper() for word in ('AWS', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL'))
            or '\0' in value for name, value in manifest.environment)):
        raise SourceRefused('EnvironmentRefused: caller environment is not allowed')
    parts = urlsplit(manifest.bundle_url)
    if (parts.scheme != 'https' or not parts.hostname or not parts.hostname.endswith('.amazonaws.com')
            or '.s3.' not in parts.hostname or parts.username or parts.password or parts.fragment):
        raise SourceRefused('InvalidSourceURL: source needs a signed S3 GET URL')


def decode_manifest(content: bytes, limits: SourceLimits) -> SourceManifest:
    """Check JSON types before admitting a source request."""
    if len(content) > CONTROL_MAX_BYTES:
        raise SourceRefused('ManifestTooLarge: source manifest exceeds the control limit')
    raw = cast(object, json.loads(content))
    if not isinstance(raw, dict):
        raise SourceRefused('InvalidManifest: source manifest must be an object')
    row = cast(dict[str, object], raw)
    def text(key: str) -> str:
        value = row.get(key)
        if not isinstance(value, str):
            raise SourceRefused('InvalidManifest: missing string ' + key)
        return value
    def words(key: str) -> tuple[str, ...]:
        values = row.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in cast(list[object], values)):
            raise SourceRefused('InvalidManifest: missing string list ' + key)
        return tuple(cast(list[str], values))
    environment = row.get('environment')
    if not isinstance(environment, list):
        raise SourceRefused('InvalidManifest: missing environment list')
    pairs: list[tuple[str, str]] = []
    for value in cast(list[object], environment):
        if not isinstance(value, list):
            raise SourceRefused('InvalidManifest: environment entry must be a pair')
        pair = cast(list[object], value)
        if len(pair) != 2 or not all(isinstance(item, str) for item in pair):
            raise SourceRefused('InvalidManifest: environment entry must contain two strings')
        pairs.append((cast(str, pair[0]), cast(str, pair[1])))
    if type(row.get('version')) is not int:
        raise SourceRefused('InvalidManifest: version must be an integer')
    # Journals from before per-run payloads have no runtime ABI.
    runtime_abi = row.get('runtime_abi', 0)
    if type(runtime_abi) is not int:
        raise SourceRefused('InvalidManifest: runtime_abi must be an integer')
    manifest = SourceManifest(cast(int, row['version']), text('repository'), text('commit'), text('bundle_sha256'),
        text('bundle_url'), words('basis_commits'), words('argv'), words('features'), tuple(pairs), text('toolchain'),
        runtime_abi=runtime_abi)
    validate_manifest(manifest, limits)
    return manifest


@dataclass(frozen=True)
class SourceIdentity:
    repository: str
    commit: str
    toolchain: str


@dataclass(frozen=True)
class HanaExecutionRequest:
    """The request binds exact source and command to one assigned copy."""
    key: HanaAttemptKey
    manifest: SourceManifest
    copy_number: int
    source_key: execution_identity.SourceKeyKnown
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
    """The fence prevents command acceptance for this exact attempt and copy number."""
    key: HanaAttemptKey
    copy_number: int
    access: HanaExecutionAccess


@dataclass(frozen=True)
class HanaSourceUpdateEnvelope:
    """A complete source bundle repairs the original capture without command authority."""
    key: HanaAttemptKey
    copy_number: int
    manifest: SourceManifest
    access: HanaExecutionAccess


HanaExecutionEnvelope = HanaCommandEnvelope | HanaFenceEnvelope | HanaSourceUpdateEnvelope


class HanaCommandTiming(TypedDict, total=False):
    """The guest records when it first confirms each command stage."""

    unit_exited_at: float
    unit_exit_observed_at: float
    lease_free_at: float
    upload_started_at: float


def decode_command_timing(value: object) -> HanaCommandTiming:
    row = hana_result.object_fields(value)
    timing: HanaCommandTiming = {}
    if 'unit_exited_at' in row:
        timing['unit_exited_at'] = hana_result.seconds_field(row, 'unit_exited_at')
    if 'unit_exit_observed_at' in row:
        timing['unit_exit_observed_at'] = hana_result.seconds_field(row, 'unit_exit_observed_at')
    if 'lease_free_at' in row:
        timing['lease_free_at'] = hana_result.seconds_field(row, 'lease_free_at')
    if 'upload_started_at' in row:
        timing['upload_started_at'] = hana_result.seconds_field(row, 'upload_started_at')
    return timing


@dataclass(frozen=True)
class HanaCommandObservation:
    """The guest reports acceptance, fencing, termination, and copy lease release."""
    key: HanaAttemptKey
    copy_number: int
    accepted: bool
    fenced: bool
    terminated: bool
    lease_free: bool
    observed_at: float
    evidence: execution_identity.ExecutionEvidence
    source_updated: bool = False
    timing: HanaCommandTiming = field(default_factory=HanaCommandTiming)


@dataclass(frozen=True)
class ConfirmedCommandAdmissionRefusal:
    """The guest reports a pre-command refusal after it confirms departure."""

    key: HanaAttemptKey
    copy_number: int
    fenced: bool
    observed_at: float
    reason: str
    refusal_code: CommandAdmissionRefusalCode
    timing: HanaCommandTiming = field(default_factory=HanaCommandTiming)

    accepted: ClassVar[Literal[True]] = True
    terminated: ClassVar[Literal[True]] = True
    lease_free: ClassVar[Literal[True]] = True
    source_updated: ClassVar[Literal[False]] = False

    @property
    def evidence(self) -> execution_identity.ExecutionEvidenceUnavailable:
        return execution_identity.ExecutionEvidenceUnavailable(self.reason, self.observed_at)


HanaExecutionObservation = HanaCommandObservation | ConfirmedCommandAdmissionRefusal


@dataclass(frozen=True)
class NoCommandAdmissionRefusal:
    """The observation does not prove a command admission refusal."""


NO_COMMAND_ADMISSION_REFUSAL = NoCommandAdmissionRefusal()


def command_admission_refusal(observation: HanaExecutionObservation
                              ) -> execution_identity.HanaCommandAdmissionRefused | NoCommandAdmissionRefusal:
    """Classify a command admission refusal after the guest confirms departure."""
    if isinstance(observation, ConfirmedCommandAdmissionRefusal):
        return execution_identity.HanaCommandAdmissionRefused(' '.join(observation.reason.splitlines()))
    evidence = observation.evidence
    if (not observation.accepted or not observation.terminated or not observation.lease_free
            or observation.source_updated or not isinstance(evidence, execution_identity.ExecutionEvidenceUnavailable)):
        return NoCommandAdmissionRefusal()
    # The old guest image sends only this exact reason for an admission refusal.
    if evidence.reason == 'The data mount and a fresh permit are required':
        return execution_identity.HanaCommandAdmissionRefused(' '.join(evidence.reason.splitlines()))
    return NoCommandAdmissionRefusal()


def encode_envelope(envelope: HanaExecutionEnvelope) -> bytes:
    """Encode a bounded version 1 execute or fence document."""
    if isinstance(envelope, HanaCommandEnvelope):
        document: dict[str, object] = {'version': 1, 'action': 'execute', 'request': request_fields(envelope.request),
                    'permission': asdict(envelope.permission), 'access': asdict(envelope.access)}
    elif isinstance(envelope, HanaSourceUpdateEnvelope):
        document = {'version': 1, 'action': 'source-update', 'key': hana_result.encode_attempt_key(envelope.key),
                    'copy_number': envelope.copy_number, 'manifest': asdict(envelope.manifest), 'access': asdict(envelope.access)}
    else:
        document = {'version': 1, 'action': 'fence', 'key': hana_result.encode_attempt_key(envelope.key),
                    'copy_number': envelope.copy_number, 'access': asdict(envelope.access)}
    content = encode_document(document)
    _ = decode_envelope(content)
    return content


def decode_envelope(content: bytes) -> HanaExecutionEnvelope:
    """Decode a bounded document at the guest transport boundary."""
    row = _document(content)
    access_row = hana_result.object_fields(row.get('access'))
    access = HanaExecutionAccess(_signed_url(access_row, 'observation_url'), _signed_url(access_row, 'result_url'),
        hana_result.seconds_field(access_row, 'expires_at'), _positive_integer(access_row, 'output_max_bytes'))
    if access.output_max_bytes > 16 * 1024 * 1024:
        raise ValueError('The Hana output limit exceeds the protocol bound')
    if row.get('action') == 'execute':
        request = decode_request(encode_document(hana_result.object_fields(row.get('request'))))
        permit_row = hana_result.object_fields(row.get('permission'))
        generation = hana_result.string_field(permit_row, 'generation')
        if not generation.isascii() or not generation.isdecimal() or int(generation) < 1:
            raise ValueError('The Hana permit generation is invalid')
        permit = WorkPermit(hana_result.seconds_field(permit_row, 'expires_at'), generation,
                            hana_result.string_field(permit_row, 'boot_id'))
        if permit.boot_id != request.key.generation.boot_id:
            raise ValueError('The Hana command permit belongs to another boot')
        return HanaCommandEnvelope(request, permit, access)
    if row.get('action') == 'source-update':
        manifest = _decode_manifest(row.get('manifest'))
        if manifest.basis_commits:
            raise ValueError('A Hana source update requires a complete bundle')
        return HanaSourceUpdateEnvelope(hana_result.decode_attempt_key(row.get('key')),
                                        _positive_integer(row, 'copy_number'), manifest, access)
    if row.get('action') != 'fence':
        raise ValueError('The Hana command action is invalid')
    return HanaFenceEnvelope(hana_result.decode_attempt_key(row.get('key')), _positive_integer(row, 'copy_number'), access)


def encode_observation(observation: HanaExecutionObservation) -> bytes:
    """Encode one observation with its exact attempt and copy number."""
    evidence = encode_evidence(observation.evidence)
    if isinstance(observation, ConfirmedCommandAdmissionRefusal):
        evidence['refusal_code'] = observation.refusal_code.value
    row: dict[str, object] = {'version': 1, 'key': hana_result.encode_attempt_key(observation.key),
        'copy_number': observation.copy_number, 'accepted': observation.accepted,
        'fenced': observation.fenced, 'terminated': observation.terminated,
        'lease_free': observation.lease_free, 'observed_at': observation.observed_at,
        'evidence': evidence, 'source_updated': observation.source_updated}
    if observation.timing:
        row['timing'] = observation.timing
    return encode_document(row)


def decode_observation(content: bytes) -> HanaExecutionObservation:
    """Reject malformed observations before the controller uses their evidence."""
    row = _document(content)
    flags: list[bool] = []
    for name in ('accepted', 'fenced', 'terminated', 'lease_free', 'source_updated'):
        value = row.get(name)
        if not isinstance(value, bool):
            raise ValueError('The Hana observation flag is invalid: ' + name)
        flags.append(value)
    evidence_row = hana_result.object_fields(row['evidence'])
    key = hana_result.decode_attempt_key(row['key'])
    copy_number = _positive_integer(row, 'copy_number')
    observed_at = hana_result.seconds_field(row, 'observed_at')
    evidence = decode_evidence(evidence_row)
    timing = decode_command_timing(row['timing']) if 'timing' in row else HanaCommandTiming()
    accepted, fenced, terminated, lease_free, source_updated = flags
    if 'refusal_code' in evidence_row:
        code = CommandAdmissionRefusalCode(hana_result.string_field(evidence_row, 'refusal_code'))
        if (not (accepted and terminated and lease_free) or source_updated
                or not isinstance(evidence, execution_identity.ExecutionEvidenceUnavailable)):
            raise ValueError('A Hana command admission refusal needs confirmed departure and a cause')
        return ConfirmedCommandAdmissionRefusal(key, copy_number, fenced, observed_at, evidence.reason, code, timing)
    observation = HanaCommandObservation(key, copy_number, accepted, fenced, terminated, lease_free,
                                         observed_at, evidence, source_updated, timing)
    if accepted and isinstance(evidence, execution_identity.ExecutionNotStarted):
        raise ValueError('An accepted Hana command needs execution evidence')
    if source_updated and not accepted:
        raise ValueError('A Hana source update needs the original accepted attempt')
    return observation


def request_fields(request: HanaExecutionRequest) -> dict[str, object]:
    """Keep the command binding at the transport boundary."""
    return {'version': 1, 'key': hana_result.encode_attempt_key(request.key), 'manifest': asdict(request.manifest),
            'copy_number': request.copy_number, 'source_key': {'key': request.source_key.key}, 'argv': request.argv,
            'link': asdict(request.link)}


def encode_request(request: HanaExecutionRequest) -> bytes:
    """Give the runner source and command data without publication URLs."""
    content = encode_document(request_fields(request))
    _ = decode_request(content)
    return content


def check_command_request(request: HanaExecutionRequest) -> None:
    """Check the request binding and reserve space for the command envelope."""
    if len(encode_request(request)) + COMMAND_ENVELOPE_RESERVE_BYTES > CONTROL_MAX_BYTES:
        raise ValueError('The Hana control document exceeds its protocol bound')


def decode_request(content: bytes) -> HanaExecutionRequest:
    """Decode one command request without importing the controller or provider."""
    row = _document(content)
    manifest = _decode_manifest(row.get('manifest'))
    key = hana_result.decode_attempt_key(row.get('key'))
    source = execution_identity.SourceKeyKnown(hana_result.string_field(hana_result.object_fields(row.get('source_key')), 'key'))
    link_row = hana_result.object_fields(row.get('link'))
    link = LogicalStepLink(hana_result.string_field(link_row, 'call_id'), hana_result.string_field(link_row, 'episode'))
    argv = row.get('argv')
    if not isinstance(argv, list) or not argv or any(not isinstance(word, str) or '\0' in word for word in cast(list[object], argv)):
        raise ValueError('The Hana command arguments are invalid')
    request = HanaExecutionRequest(key, manifest, _positive_integer(row, 'copy_number'), source,
                                   tuple(cast(list[str], argv)), link)
    if (request.argv != manifest.argv or key.claim_id != execution_identity.assigned_claim_id(key.episode, key.attempt)
            or link.episode != key.episode):
        raise ValueError('The Hana source, command, or attempt binding differs')
    return request


def _decode_manifest(value: object) -> SourceManifest:
    # The guest applies its configured source limits again before materialization.
    return decode_manifest(encode_document(hana_result.object_fields(value)),
        SourceLimits(60, ('RUSTFLAGS', 'RUSTDOCFLAGS', 'CARGO_INCREMENTAL', 'RUST_BACKTRACE')))


def encode_document(document: dict[str, object]) -> bytes:
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


def encode_evidence(evidence: execution_identity.ExecutionEvidence) -> dict[str, object]:
    if isinstance(evidence, execution_identity.ExecutionNotStarted):
        return {'kind': 'not-started'}
    if isinstance(evidence, execution_identity.ExecutionEvidenceUnavailable):
        return {'kind': 'unavailable', 'reason': evidence.reason, 'observed_at': evidence.observed_at}
    return {'kind': 'observed', 'identity': asdict(evidence.identity),
            'identity_kind': 'process' if isinstance(evidence.identity, execution_identity.ProcessExecution) else 'cloud'}


def decode_evidence(value: object) -> execution_identity.ExecutionEvidence:
    row = hana_result.object_fields(value)
    if row.get('kind') == 'not-started':
        return execution_identity.ExecutionNotStarted()
    if row.get('kind') == 'unavailable':
        return execution_identity.ExecutionEvidenceUnavailable(hana_result.string_field(row, 'reason'),
                                                     hana_result.seconds_field(row, 'observed_at'))
    if row.get('kind') != 'observed':
        raise ValueError('The Hana execution evidence is invalid')
    identity = hana_result.object_fields(row['identity'])
    text = hana_result.string_field
    if row.get('identity_kind') == 'process':
        return execution_identity.ObservedExecution(execution_identity.ProcessExecution(text(identity, 'host'), text(identity, 'generation'),
            _positive_integer(identity, 'pid'), text(identity, 'start_time')))
    if row.get('identity_kind') != 'cloud':
        raise ValueError('The Hana execution identity is invalid')
    return execution_identity.ObservedExecution(execution_identity.CloudExecution(text(identity, 'target'), text(identity, 'generation'),
        text(identity, 'instance'), text(identity, 'attempt')))


@dataclass(frozen=True)
class HanaCommandFinished:
    result: hana_result.HanaCommandResult


@dataclass(frozen=True)
class HanaExecutionRefused:
    reason: str


@dataclass(frozen=True)
class HanaExecutionLost:
    reason: str


HanaRunOutcome = HanaCommandFinished | HanaExecutionRefused | HanaExecutionLost


@dataclass(frozen=True)
class HanaSourceUpdated:
    identity: SourceIdentity


@dataclass(frozen=True)
class HanaSourceUpdateRefused:
    reason: str


HanaSourceUpdateOutcome = HanaSourceUpdated | HanaSourceUpdateRefused


def encode_outcome(outcome: HanaRunOutcome) -> bytes:
    if isinstance(outcome, HanaCommandFinished):
        return b'{"finished":' + encode_result(outcome.result) + b'}'
    kind = 'refused' if isinstance(outcome, HanaExecutionRefused) else 'lost'
    return json.dumps({kind: outcome.reason}, separators=(',', ':')).encode()


def encode_source_outcome(outcome: HanaSourceUpdateOutcome) -> bytes:
    if isinstance(outcome, HanaSourceUpdated):
        return json.dumps({'source_updated': asdict(outcome.identity)}, separators=(',', ':')).encode()
    return json.dumps({'refused': outcome.reason}, separators=(',', ':')).encode()


def decode_source_outcome(content: bytes) -> HanaSourceUpdateOutcome:
    from scripts.aws_target.hana_result import object_fields, string_field

    row = object_fields(cast(object, json.loads(content)))
    if set(row) == {'source_updated'}:
        identity = object_fields(row['source_updated'])
        return HanaSourceUpdated(SourceIdentity(string_field(identity, 'repository'),
            string_field(identity, 'commit'), string_field(identity, 'toolchain')))
    if set(row) == {'refused'}:
        return HanaSourceUpdateRefused(string_field(row, 'refused'))
    raise ValueError('The source update outcome is invalid')


def decode_outcome(content: bytes) -> HanaRunOutcome:
    from scripts.aws_target.hana_result import object_fields, string_field

    row = object_fields(cast(object, json.loads(content)))
    if set(row) == {'finished'}:
        return HanaCommandFinished(decode_result(json.dumps(row['finished']).encode()))
    if set(row) == {'refused'}:
        return HanaExecutionRefused(string_field(row, 'refused'))
    if set(row) == {'lost'}:
        return HanaExecutionLost(string_field(row, 'lost'))
    raise ValueError('The runner outcome is invalid')
