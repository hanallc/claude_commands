"""Placement decisions keep submitting owners separate from remote executions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
import uuid

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.mac_test.claim_state import ClaimScope

SubmittingSession = OwningSession | NoOwningSession


@dataclass(frozen=True)
class SubmittingOwner:
    """The owner identity holds the submitting host, process identifier, and start time."""
    host: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class ClaimHolderIdentity:
    """The claim holder identity holds its host, process identifier, and start time."""
    host: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class ClaimHolderUnconfirmed:
    """The reason explains why the claim holder is unconfirmed."""
    reason: str


@dataclass(frozen=True)
class TokenReadinessUnchecked:
    """The caller has not checked whether the local token is ready."""


@dataclass(frozen=True)
class TokenUnavailable:
    """The required local token is unavailable."""


@dataclass(frozen=True)
class TokenRequired:
    """Local execution requires a token with this readiness state."""
    readiness: TokenReadinessUnchecked | TokenUnavailable = TokenUnavailable()


@dataclass(frozen=True)
class TokenHeld:
    """The caller holds the token required for local execution."""


@dataclass(frozen=True)
class TokenNotRequired:
    """Local execution does not require a token."""


LocalExecutionPrerequisite = TokenRequired | TokenHeld | TokenNotRequired


@dataclass(frozen=True)
class NormalStep:
    """The placement serves a normal step."""


@dataclass(frozen=True)
class SpeculativeCopy:
    """The speculative copy holds its parent episode and original call time."""
    parent_episode: str
    call_started: float


PlacementPurpose = NormalStep | SpeculativeCopy


@dataclass(frozen=True)
class AttemptRunning:
    """The execution attempt is still running."""


@dataclass(frozen=True)
class AttemptCompleted:
    """The execution attempt completed with this exit status."""
    status: int


@dataclass(frozen=True)
class PrelaunchRefused:
    """The reason explains why the attempt was refused before launch."""
    reason: str


@dataclass(frozen=True)
class AttemptDeclined:
    """The reason explains why the attempt was declined."""
    reason: str


@dataclass(frozen=True)
class AttemptCancelled:
    """The reason explains why the attempt was cancelled."""
    reason: str


@dataclass(frozen=True)
class AttemptOutcomeUnavailable:
    """The reason explains why the attempt outcome is unavailable."""
    reason: str


AttemptOutcome = AttemptRunning | AttemptCompleted | PrelaunchRefused | AttemptDeclined | AttemptCancelled | AttemptOutcomeUnavailable
TerminalAttemptOutcome = AttemptCompleted | PrelaunchRefused | AttemptDeclined | AttemptCancelled


@dataclass(frozen=True)
class RetryAt:
    """The attempt can retry at this deadline."""
    deadline: float


@dataclass(frozen=True)
class DoNotRetry:
    """The reason explains why the attempt must not retry."""
    reason: str


RetryDecision = RetryAt | DoNotRetry


@dataclass(frozen=True)
class DispatchPathSettled:
    """The dispatch path has settled."""


@dataclass(frozen=True)
class DispatchPathPending:
    """The reason explains why the dispatch path remains pending."""
    reason: str


DispatchPathState = DispatchPathSettled | DispatchPathPending


@dataclass(frozen=True)
class NoTerminalReport:
    """The attempt has no final report."""


@dataclass(frozen=True)
class TerminalAttemptReport:
    """The final report holds the outcome, dispatch path state, and receipt time."""
    outcome: TerminalAttemptOutcome
    path: DispatchPathState
    received_at: float


@dataclass(frozen=True)
class ConfiguredOrder:
    """Host selection follows the configured order."""


@dataclass(frozen=True)
class PinnedHost:
    """Host selection requires this target."""
    target: str


@dataclass(frozen=True)
class MacOnlyHost:
    """Host selection requires a Mac target."""


@dataclass(frozen=True)
class ScalerGateOrder:
    """Host selection follows the scaler gate order."""


HostSelection = ConfiguredOrder | PinnedHost | MacOnlyHost | ScalerGateOrder


@dataclass(frozen=True)
class AllowedHost:
    """The host is eligible for this step."""


@dataclass(frozen=True)
class FixedExclusion:
    """The reason excludes the host independently of temporary resource state."""
    reason: str


@dataclass(frozen=True)
class TemporaryExclusion:
    """The reason temporarily excludes the host."""
    reason: str


@dataclass(frozen=True)
class EvidenceNeeded:
    """The reason describes the evidence needed to judge host eligibility."""
    reason: str


HostEligibility = AllowedHost | FixedExclusion | TemporaryExclusion | EvidenceNeeded


@dataclass(frozen=True)
class PlacementDestination:
    """The destination holds its provider, target, host, claim scope, and slot."""
    provider: Literal["local", "mac", "ssh", "scaler"]
    target: str
    host: str
    scope: ClaimScope
    slot: int = 0


@dataclass(frozen=True)
class ConfiguredProviderSettings:
    """The provider uses its configured settings."""


@dataclass(frozen=True)
class ExplicitProviderSettings:
    """The provider reads settings from this explicit path."""
    path: Path


ProviderSettings = ConfiguredProviderSettings | ExplicitProviderSettings


@dataclass(frozen=True)
class PlacementCandidate:
    """The candidate holds a destination, eligibility, and provider settings."""
    destination: PlacementDestination
    eligibility: AllowedHost | FixedExclusion = AllowedHost()
    settings: ProviderSettings = ConfiguredProviderSettings()


@dataclass(frozen=True)
class SourceKeyKnown:
    """The key identifies the source tree for placement."""
    key: str


@dataclass(frozen=True)
class SourceKeyUnavailable:
    """The reason explains why the source tree key is unavailable."""
    reason: str


SourceIdentity = SourceKeyKnown | SourceKeyUnavailable


StepCommandKind = Literal["check", "lint", "doc", "test", "build", "example", "final"]
MacRunKind = Literal["shot", "install", "probe", "run"]
CommandKind = StepCommandKind | MacRunKind
STEP_COMMAND_KINDS: tuple[StepCommandKind, ...] = ("check", "lint", "doc", "test", "build", "example", "final")
MAC_RUN_KINDS: tuple[MacRunKind, ...] = ("shot", "install", "probe", "run")


@dataclass(frozen=True)
class CommandKindNotSent:
    """An older client sends no command kind, so the service reads it from the recorded step."""


@dataclass(frozen=True)
class PlacementConstraints:
    """The constraints hold source identity, command, caller, candidates, and selection rules."""
    source_key: SourceIdentity
    command: tuple[str, ...]
    repository: str
    package: str
    session: SubmittingSession
    candidates: tuple[PlacementCandidate, ...]
    selection: HostSelection = ConfiguredOrder()
    command_kind: CommandKind | CommandKindNotSent = CommandKindNotSent()


@dataclass(frozen=True)
class PlacementAssignment:
    """The assignment binds an attempt to its destination, source, and submitting owner."""
    episode: str
    attempt: str
    destination: PlacementDestination
    source_key: SourceIdentity
    owner: SubmittingOwner
    session: SubmittingSession
    since: float


@dataclass(frozen=True)
class CurrentAssignment:
    """The placement assignment is current."""


@dataclass(frozen=True)
class ClosedAssignment:
    """The reason explains why the assignment closed."""
    reason: str


AssignmentState = CurrentAssignment | ClosedAssignment


@dataclass(frozen=True)
class PermissionAvailable:
    """The assignment retains permission to launch."""


@dataclass(frozen=True)
class PermissionConsumed:
    """The launch permission was consumed at this time."""
    consumed_at: float


LaunchPermission = PermissionAvailable | PermissionConsumed


@dataclass(frozen=True)
class ProviderClaimReference:
    """The reference identifies a provider claim for one episode and attempt."""
    provider: Literal["local", "mac", "ssh", "scaler"]
    target: str
    scope: ClaimScope
    claim_id: str
    episode: str
    attempt: str


@dataclass(frozen=True)
class NoProviderClaim:
    """The assignment has no provider claim."""


@dataclass(frozen=True)
class AdoptedProviderClaim:
    """The adopted claim holds its holder, session, host, and generation."""
    reference: ProviderClaimReference
    holder: ClaimHolderIdentity | ClaimHolderUnconfirmed
    session: SubmittingSession
    host: str
    generation: str


@dataclass(frozen=True)
class RetainedCleanupClaim:
    """The claim remains retained for cleanup for this reason."""
    reference: ProviderClaimReference
    reason: str


@dataclass(frozen=True)
class ReservedHost:
    """The reserved host holds its placement assignment and adopted claim."""
    assignment: PlacementAssignment
    claim: AdoptedProviderClaim


@dataclass(frozen=True)
class ProcessExecution:
    """The execution identity holds a host, generation, process identifier, and start time."""
    host: str
    generation: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class CloudExecution:
    """The cloud execution identity holds a target, generation, instance, and attempt."""
    target: str
    generation: str
    instance: str
    attempt: str


ExecutionIdentity = ProcessExecution | CloudExecution


@dataclass(frozen=True)
class ExecutionNotStarted:
    """Execution has not started."""


@dataclass(frozen=True)
class ObservedExecution:
    """The evidence identifies an observed execution."""
    identity: ExecutionIdentity


@dataclass(frozen=True)
class ExecutionEvidenceUnavailable:
    """The reason and time describe unavailable execution evidence."""
    reason: str
    observed_at: float


ExecutionEvidence = ExecutionNotStarted | ObservedExecution | ExecutionEvidenceUnavailable


@dataclass(frozen=True)
class NoLocalHoldOwner:
    """Remote host evidence does not depend on a local caller lineage."""


@dataclass(frozen=True)
class HostEvidenceKey:
    """The key identifies host evidence for a command, caller, destination, and configuration."""
    destination: PlacementDestination
    session: SubmittingSession
    command: tuple[str, ...]
    configuration_generation: str
    repository: str = ""
    package: str = ""
    step: str = ""
    settings: ProviderSettings = ConfiguredProviderSettings()
    local_hold_owner: SubmittingOwner | NoLocalHoldOwner = NoLocalHoldOwner()
    command_kind: CommandKind | CommandKindNotSent = CommandKindNotSent()


@dataclass(frozen=True)
class ResourceHolderSession:
    """This session holds the required resource."""
    session_id: str


@dataclass(frozen=True)
class ResourceHolderUnknown:
    """The resource holder is unknown."""


@dataclass(frozen=True)
class ResourceHolderUnit:
    """This unit holds the required resource."""
    unit: str


ResourceHolder = ResourceHolderSession | ResourceHolderUnit | ResourceHolderUnknown


@dataclass(frozen=True)
class BusyProviderDiagnostic:
    """The diagnostic holds the busy target, its activity, and its start time."""
    target: str
    what: str
    since: str


@dataclass(frozen=True)
class ProviderRetryNotice:
    """The notice holds the target, retry reason, and delay."""
    target: str
    reason: str
    delay_seconds: float


@dataclass(frozen=True)
class NoProviderDiagnostic:
    """The provider has no busy diagnostic."""


NO_PROVIDER_DIAGNOSTIC = NoProviderDiagnostic()


@dataclass(frozen=True)
class FreshHostEvidence:
    """The current evidence holds eligibility, claims, executions, and resource diagnostics."""
    key: HostEvidenceKey
    observed_at: float
    generation: str
    eligibility: HostEligibility
    claims: tuple[AdoptedProviderClaim, ...] = ()
    cleanup_claims: tuple[RetainedCleanupClaim, ...] = ()
    executions: tuple[tuple[ProviderClaimReference, ExecutionEvidence], ...] = ()
    holder: ResourceHolder = ResourceHolderUnknown()
    provider_diagnostic: BusyProviderDiagnostic | NoProviderDiagnostic = NO_PROVIDER_DIAGNOSTIC


@dataclass(frozen=True)
class StaleHostEvidence:
    """The stale evidence holds its key, observation time, and reason."""
    key: HostEvidenceKey
    observed_at: float
    reason: str


@dataclass(frozen=True)
class HostEvidenceUnavailable:
    """The unavailable evidence holds its key, observation time, and reason."""
    key: HostEvidenceKey
    observed_at: float
    reason: str


HostEvidence = FreshHostEvidence | StaleHostEvidence | HostEvidenceUnavailable


@dataclass(frozen=True)
class CandidateEligibility:
    """The result pairs a placement destination with its eligibility."""
    destination: PlacementDestination
    eligibility: HostEligibility


@dataclass(frozen=True)
class UnknownAssignment:
    """The episode and attempt have no known assignment."""
    episode: str
    attempt: str


@dataclass(frozen=True)
class NoLaunchPermission:
    """The reason explains why the assignment has no launch permission."""
    reason: str


def attempt_key(episode: str, attempt: str) -> str:
    """Length framing keeps different episode and attempt pairs distinct."""
    return f"{len(episode)}:{episode}{attempt}"


def assigned_claim_id(episode: str, attempt: str) -> str:
    """The provider claim id of one assigned attempt."""
    return uuid.uuid5(uuid.NAMESPACE_URL, attempt_key(episode, attempt)).hex


def retry_decision(destination: PlacementDestination, reason: str, now: float) -> RetryDecision:
    """Keep the existing remote decline delays separate from evidence age."""
    if reason in ("no_tests", "off", "repo", "linux_only", "local_flag", "command"):
        return DoNotRetry(reason)
    delay = 180 if destination.provider == "mac" and reason not in ("busy", "blocked", "disk") else 0
    return RetryAt(now + delay)


def copy_order(purpose: SpeculativeCopy, episode: str) -> tuple[float, str]:
    return purpose.call_started, episode
