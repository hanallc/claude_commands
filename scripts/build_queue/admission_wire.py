"""Admission socket shapes and encoding without queue records at startup."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from scripts.build_hold.shared_state_defaults import DEFAULT_ADMISSION_SOCKET as DEFAULT_ADMISSION_SOCKET
from typing import TYPE_CHECKING, TypeVar, cast

from scripts.build_queue import snapshot_codec as codec
from scripts.build_targets import target_config

if TYPE_CHECKING:
    from scripts.build_queue import admission_records as records
    from scripts.build_queue import placement
else:
    class _DeferredAdmissionRecords:
        """Load domain types when an annotation or a default needs them."""

        def __getattr__(self, name: str) -> object:
            from scripts.build_queue import admission_records
            return cast(object, getattr(admission_records, name))

    records = _DeferredAdmissionRecords()

    class _DeferredPlacement:
        """Load placement types only for version 3 messages."""

        def __getattr__(self, name: str) -> object:
            from scripts.build_queue import placement
            return cast(object, getattr(placement, name))

    placement = _DeferredPlacement()

VERSION = 3


def _no_test_width() -> records.NoTestWidth:
    return records.NoTestWidth()


class AdmissionUnavailable(RuntimeError):
    """A bounded queue query has no answer."""

    def __init__(self, reason: str) -> None:
        self.reason: str = reason
        super().__init__(reason)


class UnsupportedVersion(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(f"client version {version}, service supports 1..{VERSION}")


@dataclass(frozen=True)
class Owner:
    """The owner identity holds a process identifier and start time."""
    pid: int
    start_time: str


@dataclass(frozen=True)
class RunIdentity:
    """The run identity holds a process identifier and start time."""
    pid: int
    start_time: str


@dataclass(frozen=True)
class NoRun:
    """The request has no named build run."""


@dataclass(frozen=True)
class LocalPlace:
    """The admission request runs locally."""


@dataclass(frozen=True)
class CloudPlace:
    """The admission request runs on this cloud target."""
    target: str


@dataclass(frozen=True)
class RunPlace:
    """The run placement holds its identity, first owner, start time, and unit."""
    run: RunIdentity
    since: float
    first_owner: Owner
    unit: str = ""


@dataclass(frozen=True)
class Identity:
    """The caller identity holds its session, step, folder, and unit."""
    session_id: str
    name: str
    step: str
    folder: str
    unit: str = ""


@dataclass(frozen=True, kw_only=True)
class CallerCode:
    """The request carries the caller code version."""
    code_version: str = ""


NO_RUN = NoRun()


@dataclass(frozen=True)
class AdmitRequest(CallerCode):
    """The request holds the command, caller, run, and memory profile for admission."""
    episode: str
    owner: Owner
    argv: tuple[str, ...]
    cwd: str
    since: float
    sidecar: str
    identity: Identity
    call: records.Call
    test_width: records.TestWidth
    profile: records.StepProfile | records.ProgramProfile
    fresh_test: bool
    token_back_at: float = 0.0
    run: RunIdentity | NoRun = NO_RUN
    place: LocalPlace | CloudPlace = LocalPlace()
    route: target_config.StepRoute = field(default_factory=lambda: records.DIRECT_CARGO, kw_only=True)


@dataclass(frozen=True)
class RepositorySlotRequest(CallerCode):
    """Wait for one attempt to acquire a repository slot."""
    episode: str
    owner: Owner
    since: float
    repository: str
    machine: str
    root: str
    maximum: int
    deadline: float
    identity: Identity


@dataclass(frozen=True)
class RepositorySlotWaiting:
    """The repository pool has no free slot for this waiter."""
    repository: str
    held: tuple[int, ...]
    maximum: int
    maximum_key: str

    @property
    def text(self) -> str:
        return f"waits for a free {self.repository} slot ({len(self.held)} of {self.maximum} in use)"

    @property
    def expired_text(self) -> str:
        held = ", ".join(f"{self.repository}-slot{number}" for number in self.held) or "none"
        return f"slots: wait ended for {self.repository}; held slots: {held}; maximum {self.maximum} ({self.maximum_key})"


@dataclass(frozen=True)
class RepositorySlotReady:
    """The first waiter can attempt to acquire a repository slot."""


@dataclass(frozen=True)
class RepositorySlotExpired:
    """The repository slot wait ended before capacity became available."""
    reason: str


@dataclass(frozen=True)
class ServiceEstimateRequest(CallerCode):
    """Ask the service to estimate a cargo call from its own history."""
    episode: str
    owner: Owner
    argv: tuple[str, ...]
    cwd: str
    since: float
    sidecar: str
    repository: records.Repository
    worktree: str
    runner: str
    job: str
    test_width: records.TestWidth = field(default_factory=_no_test_width)
    route: target_config.StepRoute = field(default_factory=lambda: records.DIRECT_CARGO, kw_only=True)


@dataclass(frozen=True)
class LaunchRequest(CallerCode):
    """The launch request holds the episode, owner, caller identity, and memory profile."""
    episode: str
    owner: Owner
    since: float
    identity: Identity
    profile: records.StepProfile | records.ProgramProfile


@dataclass(frozen=True)
class ReleaseRequest(CallerCode):
    """The request releases a reservation with its final status."""
    request_id: str
    reservation: str
    status: int


@dataclass(frozen=True)
class WidthRequest(CallerCode):
    """The request updates the test width of a reservation."""
    request_id: str
    reservation: str
    width: int


@dataclass(frozen=True)
class BuildHistoryRequest(CallerCode):
    """The request asks for the build history generation."""


@dataclass(frozen=True)
class ViewRequest(CallerCode):
    """The request asks for the queue view."""


@dataclass(frozen=True)
class ExplainRequest(CallerCode):
    """The request asks for the queue explanation."""


@dataclass(frozen=True)
class MineRequest(CallerCode):
    """The request asks for the queue reading for this session."""
    session_id: str


@dataclass(frozen=True)
class PrioritySetRequest(CallerCode):
    """The request sets queue priority for a unit with a reason and actor."""
    request_id: str
    unit: str
    why: str
    actor: str


@dataclass(frozen=True)
class PriorityClearRequest(CallerCode):
    """The request clears queue priority with an actor and optional unit."""
    request_id: str
    actor: str
    unit: str = ""


@dataclass(frozen=True)
class TokenWaitRequest(CallerCode):
    """The request marks a reservation as waiting for the Cargo token."""
    request_id: str
    reservation: str


@dataclass(frozen=True)
class CloudSlotWait:
    """The cloud wait holds the used slot count and slot limit."""
    used: int
    slots: int

    @property
    def text(self) -> str:
        return f"waits for a cloud slot: {self.used} of {self.slots} in use"


@dataclass(frozen=True)
class Wait:
    """The wait reply holds its reason, queue position, text, and admission snapshot."""
    reason: records.WaitReason | CloudSlotWait
    position: int
    head: str
    text: str
    snapshot: records.AdmissionWaitSnapshot


@dataclass(frozen=True)
class AcquisitionTiming:
    """Bound acquisition without reading the client account's configuration."""
    silence_seconds: float
    reconnect_seconds: float
    unreachable_limit_seconds: float
    wait_reply_seconds: float = 60.0


PROTOCOL_ACQUISITION_TIMING = AcquisitionTiming(120.0, 2.0, 300.0)


@dataclass(frozen=True)
class CleanupTiming:
    """The timing limits control silence detection and reconnection during cleanup."""
    silence_seconds: float
    reconnect_seconds: float


# These protocol intervals permit cleanup before any configuration is usable.
PROTOCOL_CLEANUP_TIMING = CleanupTiming(120.0, 2.0)
CLEANUP_STRING_MAX = 4096
CLEANUP_STATUS_MIN = -(2 ** 63)
CLEANUP_STATUS_MAX = 2 ** 63 - 1


@dataclass(frozen=True)
class Admitted:
    """The admission grant holds its reservation, memory need, and sampling settings."""
    reservation: str
    need: int
    source: str
    max_path: str
    wave_path: str
    sampling_context: dict[str, object]
    cleanup_timing: CleanupTiming = PROTOCOL_CLEANUP_TIMING


@dataclass(frozen=True)
class RunnerExecutionTiming:
    """The runner timings control program sampling, stop checks, and peak waves."""
    program_sample_seconds: float
    program_stop_seconds: float
    peak_wave_seconds: float


@dataclass(frozen=True)
class RunnerAdmitted(Admitted):
    """The runner admission grant includes execution timings."""
    execution: RunnerExecutionTiming = field(kw_only=True)


@dataclass(frozen=True)
class LaunchAdmitted:
    """The launch grant holds its memory need and estimate source."""
    need: int
    source: str


@dataclass(frozen=True)
class Refused:
    """The reply gives the reason for refusal."""
    reason: str


@dataclass(frozen=True)
class Ok:
    """The reply confirms that the request succeeded."""


@dataclass(frozen=True)
class BuildHistoryReply:
    """The reply holds the current build history generation."""
    generation: int


@dataclass(frozen=True)
class LocalAdmissionContext:
    """The context holds the local command, test width, and admission options."""
    argv: tuple[str, ...]
    cwd: str
    sidecar: str
    call: records.Call
    test_width: records.TestWidth
    fresh_test: bool
    token_back_at: float = 0.0


@dataclass(frozen=True)
class LocalAdmissionUnavailable:
    """The reason explains why local admission context is unavailable."""
    reason: str


@dataclass(frozen=True)
class PlacementRequest(CallerCode):
    """The request holds placement constraints, caller identity, and local prerequisites."""
    episode: str
    owner: placement.SubmittingOwner
    since: float
    profile: records.StepProfile | records.ProgramProfile
    constraints: placement.PlacementConstraints
    identity: Identity = Identity("", "", "", "")
    run: RunIdentity | NoRun = NO_RUN
    local: LocalAdmissionContext | LocalAdmissionUnavailable = LocalAdmissionUnavailable("not supplied")
    prerequisite: placement.LocalExecutionPrerequisite = field(default_factory=lambda: placement.TokenNotRequired())
    purpose: placement.PlacementPurpose = field(default_factory=lambda: placement.NormalStep())
    # Where the caller lets the step run. The queue stores it on the waiter for the Builds page.
    route: target_config.StepRoute = target_config.ANY_ELIGIBLE_TARGET


@dataclass(frozen=True)
class PlacementStatusRequest(CallerCode):
    """The request asks for the status of an episode and attempt."""
    episode: str
    attempt: str


@dataclass(frozen=True)
class KnownPlacementStatus:
    """The status holds a known assignment, its state, and provider claim."""
    assignment: placement.PlacementAssignment
    state: placement.AssignmentState
    claim: placement.ProviderClaimReference | placement.NoProviderClaim


@dataclass(frozen=True)
class UnknownPlacementStatus:
    """The requested episode and attempt have no known placement status."""
    episode: str
    attempt: str


@dataclass(frozen=True)
class PlacementStatusReply:
    """The reply holds the known or unknown placement status."""
    status: KnownPlacementStatus | UnknownPlacementStatus


@dataclass(frozen=True)
class PrerequisiteReport(CallerCode):
    """The report updates local execution prerequisites for an episode and owner."""
    request_id: str
    episode: str
    owner: placement.SubmittingOwner
    prerequisite: placement.LocalExecutionPrerequisite


@dataclass(frozen=True)
class CancelPlacement(CallerCode):
    """The request cancels an episode for its owner with a reason."""
    request_id: str
    episode: str
    owner: placement.SubmittingOwner
    reason: str


@dataclass(frozen=True)
class ClaimFailure(CallerCode):
    """The report holds an assignment, failed provider claim, and reason."""
    request_id: str
    assignment: placement.PlacementAssignment
    reason: str
    claim: placement.ProviderClaimReference | placement.NoProviderClaim = field(
        default_factory=lambda: placement.NoProviderClaim())


@dataclass(frozen=True)
class ConsumePermission(CallerCode):
    """The request consumes launch permission for an assignment and claim holder."""
    request_id: str
    assignment: placement.PlacementAssignment
    claim: placement.ProviderClaimReference
    holder: placement.ClaimHolderIdentity | placement.ClaimHolderUnconfirmed = field(
        default_factory=lambda: placement.ClaimHolderUnconfirmed("not supplied"))


@dataclass(frozen=True)
class ExecutionReport(CallerCode):
    """The report holds assignment, claim, execution evidence, outcome, and path state."""
    request_id: str
    assignment: placement.PlacementAssignment
    claim: placement.ProviderClaimReference | placement.NoProviderClaim
    evidence: placement.ExecutionEvidence
    outcome: placement.AttemptOutcome = field(default_factory=lambda: placement.AttemptOutcomeUnavailable("not supplied"))
    path: placement.DispatchPathState = field(default_factory=lambda: placement.DispatchPathPending("not confirmed"))


@dataclass(frozen=True)
class PlacementEvidenceWait:
    """Placement waits for host evidence."""


@dataclass(frozen=True)
class PlacementResourceWait:
    """Placement waits for this resource with a reason and holder."""
    resource: str
    reason: str
    holder: placement.ResourceHolder = field(default_factory=lambda: placement.ResourceHolderUnknown())


@dataclass(frozen=True)
class PlacementWait:
    """The wait holds candidate eligibility, queue position, and provider diagnostics."""
    episode: str
    since: float
    position: int
    eligibility: tuple[placement.CandidateEligibility, ...]
    wait: Wait | PlacementEvidenceWait | PlacementResourceWait = field(default_factory=PlacementEvidenceWait)
    provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()
    mac_disk_exclusions: tuple[str, ...] = ()
    retry_notices: tuple[placement.ProviderRetryNotice, ...] = ()


@dataclass(frozen=True)
class Assignment:
    """The reply holds a placement assignment and provider diagnostics."""
    assignment: placement.PlacementAssignment
    provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()
    mac_disk_exclusions: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalPlacementGranted:
    """The reply pairs a local assignment with its memory admission grant."""
    assignment: placement.PlacementAssignment
    grant: Admitted
    provider_diagnostics: tuple[placement.BusyProviderDiagnostic, ...] = ()
    mac_disk_exclusions: tuple[str, ...] = ()


@dataclass(frozen=True)
class FirstAuthorization:
    """The first launch authorization pairs an assignment with its provider claim."""
    assignment: placement.PlacementAssignment
    claim: placement.ProviderClaimReference


@dataclass(frozen=True)
class DispatchStatus:
    """The dispatch status holds assignment state, launch permission, and retry decision."""
    assignment: placement.PlacementAssignment | placement.UnknownAssignment
    state: placement.AssignmentState
    permission: placement.LaunchPermission | placement.NoLaunchPermission
    reason: str = ""
    retry: placement.RetryDecision = field(default_factory=lambda: placement.DoNotRetry("attempt is not settled"))


Request = (RepositorySlotRequest | ServiceEstimateRequest | AdmitRequest | LaunchRequest | ReleaseRequest | WidthRequest | ViewRequest
           | BuildHistoryRequest | ExplainRequest | MineRequest | PrioritySetRequest | PriorityClearRequest | TokenWaitRequest
           | PlacementRequest | PlacementStatusRequest | ClaimFailure | ConsumePermission | ExecutionReport | PrerequisiteReport | CancelPlacement)

Reply = (RepositorySlotWaiting | RepositorySlotReady | RepositorySlotExpired | Wait | RunnerAdmitted | Admitted | LaunchAdmitted | Refused | Ok | BuildHistoryReply
         | PlacementWait | Assignment | LocalPlacementGranted | FirstAuthorization | DispatchStatus | PlacementStatusReply)

_REQUESTS: dict[str, type] = {
    "repository_slot": RepositorySlotRequest,
    "service_estimate": ServiceEstimateRequest, "admit": AdmitRequest, "launch": LaunchRequest, "release": ReleaseRequest,
    "width": WidthRequest, "view": ViewRequest, "explain": ExplainRequest,
    "build_history": BuildHistoryRequest, "mine": MineRequest,
    "priority_set": PrioritySetRequest, "priority_clear": PriorityClearRequest, "token_wait": TokenWaitRequest,
    "placement": PlacementRequest, "placement_status": PlacementStatusRequest, "claim_failure": ClaimFailure,
    "consume_permission": ConsumePermission, "execution_report": ExecutionReport,
    "prerequisite": PrerequisiteReport, "cancel_placement": CancelPlacement,
}
REPLY_TYPES: dict[str, type] = {
    "repository_slot_waiting": RepositorySlotWaiting, "repository_slot_ready": RepositorySlotReady,
    "repository_slot_expired": RepositorySlotExpired,
    "wait": Wait, "runner_admitted": RunnerAdmitted, "admitted": Admitted, "launch_admitted": LaunchAdmitted,
    "build_history": BuildHistoryReply, "refused": Refused, "ok": Ok,
    "placement_wait": PlacementWait, "assignment": Assignment,
    "first_authorization": FirstAuthorization, "dispatch_status": DispatchStatus,
    "local_placement_granted": LocalPlacementGranted, "placement_status": PlacementStatusReply,
}
_PLACEMENT_TYPES = (PlacementStatusRequest, PlacementStatusReply, PlacementRequest, ClaimFailure, ConsumePermission, ExecutionReport, PrerequisiteReport, CancelPlacement,
                    PlacementWait, Assignment, LocalPlacementGranted, FirstAuthorization, DispatchStatus)
T = TypeVar("T")


def admission_socket_path() -> Path:
    from scripts.build_hold.override_guard import refuse_overrides
    refuse_overrides(Path(__file__).with_name("admission_protocol.py"), "build_queue/admission_protocol.py")
    return Path(os.environ.get("ADMISSION_SOCKET") or DEFAULT_ADMISSION_SOCKET)


def encode_record(value: object) -> str:
    """State and message payloads have exactly the domain codec's representation."""
    return json.dumps(codec.snapshot_value(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _read_wire_record(record: dict[str, object]) -> dict[str, object]:
    # Phase 33: older clients sent ShapeP90 with a fixed ten-percent share.
    if record.get("kind") == "ShapeP90":
        return {**record, "kind": "ShapeRankedPeak", "share_percent": 10}
    priority = record.get("priority")
    if isinstance(priority, dict):
        record["priority"] = records.upgrade_queue_priority(cast(dict[str, object], priority))
    return record


def _placement_wire_defaults(record: dict[str, object]) -> dict[str, object]:
    """Decode older placement messages into semantic recovery states."""
    record = _read_wire_record(record)
    if record.get("kind") == "AdoptedProviderClaim" and "holder" not in record:
        return {**record, "holder": {"kind": "ClaimHolderUnconfirmed", "reason": "older holder evidence"}}
    if record.get("kind") == "ExecutionReport" and isinstance(record.get("outcome"), str):
        return {**record, "outcome": {"kind": "AttemptOutcomeUnavailable", "reason": record["outcome"] or "older outcome"}}
    return record


def _write_wire_record(value: object) -> object:
    if isinstance(value, list):
        return [_write_wire_record(item) for item in cast(list[object], value)]
    if not isinstance(value, dict):
        return value
    record = {key: _write_wire_record(item) for key, item in cast(dict[str, object], value).items()}
    if (record.get("kind") in ("AdmitRequest", "ServiceEstimateRequest")
            and record.get("route") == codec.snapshot_value(records.DIRECT_CARGO)):
        del record["route"]
    # Phase 33: a config change introduces other shares after deployment of the new service.
    if record.get("kind") == "ShapeRankedPeak" and record.get("share_percent") == 10:
        record["kind"] = "ShapeP90"
        del record["share_percent"]
    return record


def parse_record(text: str, schema: type[T]) -> T:
    value = cast(object, json.loads(text, object_hook=_read_wire_record))
    if getattr(schema, "__name__", "") == "QueuePriority" and isinstance(value, dict):
        value = records.upgrade_queue_priority(cast(dict[str, object], value))
    return _parse_value(value, schema)


def _parse_value(value: object, schema: type[T]) -> T:
    return cast(T, codec.parse_snapshot_value(value, schema))


def encode_envelope(value: object, variants: dict[str, type], version: int = VERSION) -> str:
    name = next((name for name, schema in variants.items() if type(value) is schema), None)
    if name is None:
        raise ValueError("unknown admission message")
    if version < 3 and type(value) in _PLACEMENT_TYPES:
        raise ValueError("placement requires version 3")
    data = _write_wire_record(codec.snapshot_value(value))
    return encode_record({"v": version, "type": name, "data": data}) + "\n"


def parse_envelope(line: str | bytes, variants: dict[str, type]) -> tuple[int, object]:
    try:
        value = cast(object, json.loads(line, object_hook=_placement_wire_defaults))
        if not isinstance(value, dict):
            raise ValueError("invalid admission envelope")
        envelope = cast(dict[str, object], value)
        version = envelope.get("v")
        if type(version) is not int or not 1 <= version <= VERSION:
            raise UnsupportedVersion(version)
        name = envelope.get("type")
        if not isinstance(name, str) or name not in variants or set(envelope) != {"v", "type", "data"}:
            raise ValueError("invalid admission envelope")
        data = envelope["data"]
        if version < 3 and variants[name] in _PLACEMENT_TYPES:
            raise ValueError("placement requires version 3")
        return version, cast(object, _parse_value(data, variants[name]))
    except (ArithmeticError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("invalid admission message") from error


def _validate_cleanup(request: Request) -> None:
    if not isinstance(request, (ReleaseRequest, TokenWaitRequest)):
        return
    fields = (request.request_id, request.reservation, request.code_version)
    if any(len(value) > CLEANUP_STRING_MAX for value in fields):
        raise ValueError(f"cleanup string exceeds {CLEANUP_STRING_MAX} characters")
    if isinstance(request, ReleaseRequest) and not CLEANUP_STATUS_MIN <= request.status <= CLEANUP_STATUS_MAX:
        raise ValueError("cleanup status exceeds signed 64-bit range")


def encode_request(request: Request) -> str:
    _validate_cleanup(request)
    return encode_envelope(request, _REQUESTS)


def parse_request(line: str | bytes) -> Request:
    return parse_request_envelope(line)[1]


def parse_request_envelope(line: str | bytes) -> tuple[int, Request]:
    version, value = parse_envelope(line, _REQUESTS)
    request = cast(Request, value)
    _validate_cleanup(request)
    return version, request


def noncleanup_request_started(data: bytes) -> bool:
    """Identify a noncleanup header before its request body is complete."""
    source = data.decode("utf-8", errors="replace").lstrip()
    if not source.startswith("{"):
        return False
    decoder = json.JSONDecoder()
    rest = source[1:].lstrip()
    try:
        while rest:
            key, end = cast(tuple[object, int], decoder.raw_decode(rest))
            rest = rest[end:].lstrip()
            if not rest.startswith(":"):
                return False
            value, end = cast(tuple[object, int], decoder.raw_decode(rest[1:].lstrip()))
            if key == "type":
                return isinstance(value, str) and _REQUESTS.get(value) not in (ReleaseRequest, TokenWaitRequest)
            rest = rest[1:].lstrip()[end:].lstrip()
            if not rest.startswith(","):
                return False
            rest = rest[1:].lstrip()
    except ValueError:
        pass
    return False


def parse_reply(line: str | bytes, request: Request | None = None) -> Reply:
    reply = cast(Reply, parse_envelope(line, REPLY_TYPES)[1])
    if (isinstance(request, LaunchRequest) and isinstance(reply, Admitted)
            or isinstance(request, (AdmitRequest, ServiceEstimateRequest)) and isinstance(reply, LaunchAdmitted)):
        raise ValueError("admission outcome does not match request")
    return reply


def encode_reply(reply: Reply, *, version: int = VERSION) -> str:
    return encode_envelope(reply, REPLY_TYPES, version)


# JSON escapes one character into at most six ASCII bytes; include the newline.
CLEANUP_REQUEST_MAX_BYTES = max(len(encode_request(ReleaseRequest(
    "\0" * CLEANUP_STRING_MAX, "\0" * CLEANUP_STRING_MAX, status,
    code_version="\0" * CLEANUP_STRING_MAX)).encode())
    for status in (CLEANUP_STATUS_MIN, CLEANUP_STATUS_MAX))
