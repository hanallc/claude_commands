"""Shared admission records, configuration and wait phrases."""
from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
import math
import os
import re
from types import MappingProxyType
from typing import Callable, Literal, NamedTuple, NotRequired, TypedDict, assert_never, cast
from zoneinfo import ZoneInfo

import sys
from pathlib import Path


from scripts.build_queue import execution_identity
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import placement
from scripts.build_queue.admission_jobs import (GATE_JOB_NOT_RECORDED, HAND_MADE, VERIFY_VERBS as VERIFY_VERBS,
    GateJobNotRecorded, NotVerifyCall as NotVerifyCall, StepJob, VerifyCall as VerifyCall,
    VerifyStep as VerifyStep, VerifyVerb as VerifyVerb)
from scripts.build_queue.snapshot_codec import select_queue_name
from scripts.build_queue.scope_activity import (EmptyBuildScope, IdleService, LiveBuildRun, NoLiveBuildRun,
    NotServiceSlice, RunningServiceJob, ScopeProcesses, ServiceJobState)

from scripts.build_targets import target_config
from scripts.build_targets.target_config import LocalRequired, StepRoute
from scripts.buildlog import parse
from scripts.lib import conf
from scripts.lib.proc_text import read_proc_text
from scripts.lib.process_stat import PROC_ROOT, process_fields
from scripts.lib.showrunner_state import state_root
from scripts.production import staff
from scripts.production.staff import StaffId
from scripts.lib.test_phase import (CallRecord as CallRecord, NoCallRecord as NoCallRecord,
                                   TestPhaseChargeUse as TestPhaseChargeUse)

GIB = 1 << 30
DIRECT_CARGO = LocalRequired("a direct cargo command runs where it starts")


@dataclass(frozen=True)
class UnregisteredTarget:
    """The target name has no registration."""


def effective_target(name: str, settings: placement.ProviderSettings, *,
                     configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                     ) -> target_config.Target | UnregisteredTarget:
    """Select the registered target with the settings supplied by the request."""
    for target in target_config.read_targets(selected=name, purpose=target_config.RegistryRead.PLACEMENT,
                                              configuration=configuration):
        return ssh_target_settings(target, settings) if isinstance(target, target_config.SshTarget) else target
    return UnregisteredTarget()


def ssh_target_settings(target: target_config.SshTarget, settings: placement.ProviderSettings) -> target_config.SshTarget:
    """Apply the settings supplied by the request to an SSH target."""
    if isinstance(settings, placement.ExplicitProviderSettings):
        return replace(target, settings=settings.path)
    return target


@dataclass(frozen=True)
class RefusalSettings:
    """The digest identifies the settings read immediately before a prelaunch refusal."""
    digest: str


@dataclass(frozen=True)
class NoRefusalSettings:
    """The report supplies no settings digest."""


@dataclass(frozen=True)
class RemoteStartHoldUnrecorded:
    """The assignment has no recorded hold for a remote start."""


@dataclass(frozen=True)
class RemoteStartHold:
    """Keep the local place until this deadline while a remote start is pending."""

    deadline: float


@dataclass(frozen=True)
class RemoteStartHoldEnded:
    """The remote assignment no longer keeps a place on natedev."""


@dataclass(frozen=True)
class SavedPlacementAssignment:
    """The saved assignment holds its settings generation and dispatch state."""
    assignment: placement.PlacementAssignment
    state: placement.AssignmentState = placement.CurrentAssignment()
    permission: placement.LaunchPermission = placement.PermissionAvailable()
    provider: placement.ProviderClaimReference | placement.NoProviderClaim = placement.NoProviderClaim()
    holder: placement.ClaimHolderIdentity | placement.ClaimHolderUnconfirmed = placement.ClaimHolderUnconfirmed("not consumed")
    retry: placement.RetryDecision = placement.DoNotRetry("attempt is not settled")
    terminal_report: placement.TerminalAttemptReport | placement.NoTerminalReport = placement.NoTerminalReport()
    assigned_settings_generation: placement.AssignedSettingsGeneration | placement.UnknownAssignedSettingsGeneration = (
        placement.UnknownAssignedSettingsGeneration())
    refusal_settings: RefusalSettings | NoRefusalSettings = NoRefusalSettings()
    remote_start_hold: RemoteStartHold | RemoteStartHoldUnrecorded | RemoteStartHoldEnded = RemoteStartHoldUnrecorded()
    placement_candidates: tuple[placement.CandidateEligibility, ...] = ()

# Historical multiplier for saved dev bases from the Phase 24 replay.
DEV_HEADROOM = 4.2034275337646125

TestScope = Literal["full", "filtered"]


@dataclass(frozen=True)
class AdmissionTiming:
    """The timing limits control admission starts and estimate updates."""
    recovery_start_seconds: int
    estimate_refresh_seconds: int
    slot_gap_seconds: int
    fresh_test_seconds: float


@dataclass(frozen=True)
class PressurePolicy:
    """The limits define when memory pressure delays admission."""
    full_avg10_limit: float
    some_avg10_limit: float


@dataclass(frozen=True)
class PeakHold:
    """The durations define how long each step kind retains its peak charge."""
    default_seconds: int
    by_kind: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))

    def seconds_for(self, kind: str) -> int:
        return self.by_kind.get(kind, self.default_seconds)


@dataclass(frozen=True)
class GpuTestSlots:
    """Limit the local test steps of GPU repositories that run at once on this host."""
    slots: int
    repositories: frozenset[str]

    def holds(self, repository: str, step: str) -> bool:
        return repository in self.repositories and step in GPU_TEST_STEPS


# These steps run test binaries; a GPU repository's tests open render apps.
GPU_TEST_STEPS = frozenset({"nextest", "test", "t", "test-affected", "gate"})
NO_GPU_TESTS = GpuTestSlots(1, frozenset())
# A hand made run counts in the run limit of its machine and repository, as a gate does.
REPOSITORY_GATE_STEPS = frozenset({"gate", "test-affected", HAND_MADE.step})
RUN_LIMIT_EXEMPT_KEY = "max_runs.exempt_unit"


@dataclass(frozen=True)
class NoRepositoryRunLimit:
    """The repository has no run limit for this admission."""


@dataclass(frozen=True)
class RepositoryRunLimit:
    """Hold a new run when this many runs already use the machine."""
    maximum: int

    def __post_init__(self) -> None:
        if self.maximum < 0:
            raise ValueError("expected a non-negative run limit")


@dataclass(frozen=True)
class RepositoryRunLimits:
    """Limit concurrent local runs by machine and repository."""
    by_machine_and_repository: Mapping[str, int] = field(default_factory=dict[str, int])
    exempt_unit: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_machine_and_repository", MappingProxyType(dict(self.by_machine_and_repository)))

    def for_step(self, machine: str, repository: str, step: str) -> RepositoryRunLimit | NoRepositoryRunLimit:
        if step not in REPOSITORY_GATE_STEPS:
            return NoRepositoryRunLimit()
        return self.for_repository(machine, repository)

    def for_repository(self, machine: str, repository: str) -> RepositoryRunLimit | NoRepositoryRunLimit:
        key = f"{machine}.{repository}"
        if key not in self.by_machine_and_repository:
            return NoRepositoryRunLimit()
        return RepositoryRunLimit(self.by_machine_and_repository[key])


@dataclass(frozen=True)
class AdmissionPolicy:
    """Set memory, pressure, timing, GPU, and repository run limits."""
    timing: AdmissionTiming
    pressure: PressurePolicy
    peak_hold: PeakHold
    safety_bytes: int = 3 * GIB
    progress_lookahead_percent: float = field(kw_only=True)
    agent_swap_share_percent: int = field(default=100, kw_only=True)
    gpu_tests: GpuTestSlots = field(default=NO_GPU_TESTS, kw_only=True)
    repository_runs: RepositoryRunLimits = field(default_factory=RepositoryRunLimits, kw_only=True)


@dataclass(frozen=True)
class RepositoryIdentity:
    """The identity holds the repository key and current and legacy names."""
    key: str
    name: str
    legacy_name: str = ""


@dataclass(frozen=True)
class UnknownRepository:
    """The repository identity is unknown."""

Repository = RepositoryIdentity | UnknownRepository

LegacyNeedSource = Literal["measured", "buildlog", "pooled", "fallback"]

NeedSource = str


@dataclass(frozen=True)
class NoFloor:
    """No failed peak sets a minimum memory estimate."""


@dataclass(frozen=True)
class FailedPeakFloor:
    """The failed run supplies a minimum peak and its end time."""
    peak: int
    ended_at: str


@dataclass(frozen=True)
class NoHeadroom:
    """The memory estimate has no added headroom."""


@dataclass(frozen=True)
class ReleaseHeadroom:
    """The factor adds headroom to a release memory estimate."""
    factor: float


@dataclass(frozen=True)
class LegacyDevMultiplierHeadroom:
    """The multiplier recorded before the dev size rule."""

    factor: float = DEV_HEADROOM


@dataclass(frozen=True)
class DevSizeHeadroom:
    """The factor and byte margin add headroom to a development memory estimate."""
    factor: float
    margin_bytes: int

HeadroomRule = ReleaseHeadroom | LegacyDevMultiplierHeadroom | DevSizeHeadroom | NoHeadroom


@dataclass(frozen=True)
class CapNotRecorded:
    """The memory cap was not recorded."""


@dataclass(frozen=True)
class UnderBudget:
    """The memory estimate does not require a budget cap."""


@dataclass(frozen=True)
class CappedAtBudget:
    """The original estimate exceeds the memory budget cap."""
    uncapped: int
    cap: int | CapNotRecorded


@dataclass(frozen=True)
class ShapeRankedPeak:
    """The estimate uses a ranked shape peak with its floor, headroom, and cap."""
    runs: int
    share_percent: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: HeadroomRule = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeLargest:
    """The estimate uses the largest shape peak with its floor, headroom, and cap."""
    runs: int
    floor: FailedPeakFloor | NoFloor = NoFloor()
    factor: HeadroomRule = NoHeadroom()
    cap: CappedAtBudget | UnderBudget = UnderBudget()


@dataclass(frozen=True)
class ShapeFallback:
    """The shape estimate uses a fallback with its budget cap."""
    cap: CappedAtBudget | UnderBudget = UnderBudget()

ShapeNeedBasis = ShapeRankedPeak | ShapeLargest | ShapeFallback

NeedBasis = LegacyNeedSource | ShapeNeedBasis


@dataclass(frozen=True)
class BasisNotRecorded:
    """The basis of the memory estimate was not recorded."""

TestPhaseNeedForm = Literal["same_width", "wider_unscaled", "scaled_up"]


@dataclass(frozen=True)
class TestPhaseNeed:
    """The test memory estimate holds its widths, sample count, scaling form, and margin."""
    bytes: int
    runs: int
    width: int
    recorded_width: int
    form: TestPhaseNeedForm
    margin: float

FinalOutcomeName = Literal["completed", "failed", "killed", "owner_gone"]


class QueueLifeIdentity(TypedDict):
    pid: int
    start_time: str
    since: float
    session_id: str
    session_name: str
    step: str
    folder: str
    unit: NotRequired[str]


class FinalizationIntent(TypedDict):
    reservation_id: str
    peak: int
    scoped: bool
    outcome: FinalOutcomeName
    ended_at: str
    end_kind: NotRequired[Literal["step_end", "token_return"]]
    history_start: NotRequired[int]
    queue_identity: NotRequired[QueueLifeIdentity]
    status: NotRequired[int]
    test_phase_peak_bytes: NotRequired[int]
    test_phase_started_at: NotRequired[float]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]


@dataclass(frozen=True)
class RequestedTestWidth:
    """The caller requests this positive number of test threads."""
    threads: int

    def __post_init__(self) -> None:
        if self.threads <= 0:
            raise ValueError("test width must be positive")


@dataclass(frozen=True)
class UnspecifiedTestWidth:
    """The test command does not specify a thread count."""


@dataclass(frozen=True)
class NoTestWidth:
    """The command has no test thread count."""

TestWidth = RequestedTestWidth | UnspecifiedTestWidth | NoTestWidth

Call = CallRecord | NoCallRecord


@dataclass(frozen=True)
class CallerRun:
    """The caller names this run; the pass captures whether it still lives."""

    run: LiveBuildRun | NoLiveBuildRun


@dataclass(frozen=True)
class FoundRun:
    """The process ancestry supplies this run without a caller claim."""

    run: LiveBuildRun | NoLiveBuildRun


class Reservation(TypedDict):
    pid: int
    start_time: str
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_threads: TestWidth
    call_id: Call
    repo_identity: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    shape: NotRequired[str]
    reservation_id: NotRequired[str]
    scoped: NotRequired[bool]
    finalization: NotRequired[FinalizationIntent]
    queue_waiter: NotRequired[Waiter]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    folder: NotRequired[str]
    unit: NotRequired[str]
    test_phase_need: NotRequired[dict[str, object]]
    test_phase_charge_use: NotRequired[TestPhaseChargeUse]
    build_run: NotRequired[LiveBuildRun | NoLiveBuildRun]
    run_origin: NotRequired[CallerRun | FoundRun]
    slot_staff: NotRequired[StaffId | NoEndedStepStaff]
    job: NotRequired[StepJob]


@dataclass(frozen=True)
class ExpectedPeak:
    """The expected memory peak holds its source, sample count, and estimate basis."""
    bytes: int
    source: NeedSource
    count: int
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()

    def __post_init__(self) -> None:
        if self.source == "shape" and not isinstance(self.basis, (ShapeRankedPeak, ShapeLargest, ShapeFallback)):
            raise ValueError("a shape estimate needs its shape basis")
        if isinstance(self.basis, BasisNotRecorded):
            if self.source in ("measured", "buildlog", "pooled", "fallback"):
                object.__setattr__(self, "basis", self.source)


@dataclass(frozen=True)
class PromisedMemory:
    """The step promises this number of memory bytes."""
    bytes: int


@dataclass(frozen=True)
class PeakTimeUnknown:
    """The time of the memory peak is unknown."""


@dataclass(frozen=True)
class NoPeakRecorded:
    """The step has no recorded memory peak."""


@dataclass(frozen=True)
class RampPromise:
    """The memory promise covers the growth toward the step peak."""
    bytes: int


@dataclass(frozen=True)
class LifetimePromise:
    """The memory promise remains in effect for the step lifetime."""
    bytes: int


@dataclass(frozen=True)
class NoPromise:
    """The step has no memory promise."""

MemoryPromise = RampPromise | LifetimePromise | NoPromise


@dataclass(frozen=True)
class PeakSoFar:
    """The observed peak holds its size and the times of peak activity."""
    bytes: int
    peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    last_near_peak_at: float | PeakTimeUnknown = PeakTimeUnknown()
    old_sampler: bool = False


@dataclass(frozen=True)
class PeakDecayed:
    """The reduced memory charge retains the original peak and decay times."""
    bytes: int
    peak_bytes: int
    last_near_peak_at: float
    decayed_at: float
    old_sampler: bool


@dataclass(frozen=True)
class TestPhaseStarted:
    """The test phase holds its start time, prior peak, and memory need."""
    started_at: float
    peak_so_far: int
    need: TestPhaseNeed


@dataclass(frozen=True)
class PeakFileUnreadable:
    """The unreadable peak file retains the promised byte count as a floor."""
    bytes: int

PeakHistory = NoPeakRecorded | PeakSoFar | PeakDecayed | PeakFileUnreadable | TestPhaseStarted


@dataclass(frozen=True)
class PidNotRecorded:
    """The process identifier was not recorded."""


@dataclass(frozen=True)
class ProcessAnonymousUse:
    """Retain one process's resident swap-backed use or its unavailable reading."""
    pid: int
    use: AnonymousRunUse | RunUseUnavailable


@dataclass(frozen=True)
class ScopeSource:
    """The memory reading comes from this scope and its process members."""
    directory: Path
    members: frozenset[int]


@dataclass(frozen=True)
class ProcessSource:
    """The memory reading comes from these individual processes."""
    processes: tuple[ProcessAnonymousUse, ...]


@dataclass(frozen=True)
class NoSource:
    """The memory reading has no recorded source."""


@dataclass(frozen=True)
class MeasuredMemory:
    """The step holds measured memory, swap, promises, and peak history."""
    bytes: int
    anonymous: AnonymousRunUse | RunUseUnavailable = field(default_factory=lambda: RunUseUnavailable("swap-backed use unavailable"), kw_only=True)
    anonymous_source: ScopeSource | ProcessSource | NoSource = field(default_factory=NoSource, kw_only=True)
    promise: MemoryPromise = NoPromise()
    # Swapped bytes stay separate from resident swap-backed use.
    swap_bytes: int = 0
    observed_peak: int = 0
    # An existing peak file could not be read; its promise remains a floor.
    unknown_peak_promise: int = 0
    peak_history: PeakHistory = NoPeakRecorded()


@dataclass(frozen=True)
class ProgressAdjustedMemory(MeasuredMemory):
    """The measured memory includes progress evidence and the remaining memory need."""
    counter: progress.ProgressCounter | progress.NoCounter = field(kw_only=True)
    window: progress.WindowPeak | progress.NoMatchingProfile = field(kw_only=True)
    remaining_need: int = field(kw_only=True)


@dataclass(frozen=True)
class CompilerJob:
    """The compilers that sccache runs for one step scope, which the scope's step owns."""
    scope: Path
    use: AnonymousRunUse | RunUseUnavailable


@dataclass(frozen=True)
class CompilerServiceMemory(MeasuredMemory):
    """Compiler charge, without a build reservation or run-alone veto.

    The bytes leave out the jobs: each job counts in the step that names its scope.
    """

    job_state: IdleService = IdleService()
    pid: int | PidNotRecorded = field(kw_only=True)
    jobs: tuple[CompilerJob, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class ServiceJobStateNotRecorded:
    """The service job state was not recorded."""


@dataclass(frozen=True)
class UnownedSliceMemory(MeasuredMemory):
    """The memory reading describes a scope without an owned reservation."""
    diagnostic: str = ""
    cgroup: str = ""
    owner: str = ""
    activity: ScopeProcesses | EmptyBuildScope = EmptyBuildScope()
    pid: int | PidNotRecorded = field(kw_only=True)
    # Keep this wire union stable for pinned clients.
    job_state: RunningServiceJob | IdleService | NotServiceSlice = field(
        default=NotServiceSlice(), metadata={"acquisition_unused": True})
    # New service states belong in this optional field.
    service_job_state: ServiceJobState | ServiceJobStateNotRecorded = field(
        default_factory=ServiceJobStateNotRecorded, kw_only=True, metadata={"acquisition_unused": True})


def recorded_service_state(memory: CompilerServiceMemory | UnownedSliceMemory) -> ServiceJobState:
    """Read the current state, with the legacy state for older snapshots."""
    if isinstance(memory, UnownedSliceMemory) and not isinstance(memory.service_job_state, ServiceJobStateNotRecorded):
        return memory.service_job_state
    return memory.job_state


@dataclass(frozen=True)
class GoneMemory:
    """The step no longer has a live memory owner."""


@dataclass(frozen=True)
class RemoteMemoryUnavailable:
    """The reason explains why remote memory cannot be read."""
    reason: str


StepMemory = PromisedMemory | MeasuredMemory | ProgressAdjustedMemory | CompilerServiceMemory | UnownedSliceMemory | GoneMemory | RemoteMemoryUnavailable


@dataclass(frozen=True)
class NoPressureHold:
    """Memory pressure does not require an admission hold."""


@dataclass(frozen=True)
class PressureUnavailable:
    """The reason explains why memory pressure cannot be read."""
    reason: str


@dataclass(frozen=True)
class PressureConditionNotRecorded:
    """The pressure condition was not recorded."""


@dataclass(frozen=True)
class MemoryPressure:
    """The pressure reading holds averages, swap use, and the next permitted start time."""
    full_avg10: float
    some_avg10: float = 0.0
    swap_used_percent: float = 0.0
    next_start_at: float = 0.0
    condition: Literal["full", "some"] | PressureConditionNotRecorded = PressureConditionNotRecorded()


@dataclass(frozen=True)
class UnknownHostTotal:
    """The total host memory is unknown."""


@dataclass(frozen=True)
class HostMemory:
    """The host reading holds available memory, total memory, and swap use."""
    available: int
    total: int | UnknownHostTotal
    swap_total: int = 0
    swap_free: int = 0
    build_and_ci_swap: int | ReadingUnavailable = field(default=0, kw_only=True)
    swap_in_window_bytes: int = field(default=0, kw_only=True)

Pressure = NoPressureHold | MemoryPressure | PressureUnavailable

NO_PRESSURE_HOLD = NoPressureHold()


class Waiter(TypedDict):
    pid: int
    start_time: str
    build_run: NotRequired[LiveBuildRun | NoLiveBuildRun]
    need: int
    since: float
    order_since: NotRequired[float]
    move_id: NotRequired[str]
    label: NotRequired[str]
    session_id: NotRequired[str]
    session_name: NotRequired[str]
    unit: NotRequired[str]
    ci_runner: NotRequired[str]
    step: NotRequired[str]
    folder: NotRequired[str]
    token_left_at: NotRequired[float]
    token_back_at: NotRequired[float]
    priority_since: NotRequired[float]
    estimated_at: NotRequired[float]
    build_history_current: NotRequired[bool]
    fresh_test: NotRequired[bool]
    repo_identity: NotRequired[str]
    repository_name: NotRequired[str]
    legacy_name: NotRequired[str]
    test_scope: NotRequired[TestScope]
    package: NotRequired[str]
    filters: NotRequired[list[str]]
    need_source: NotRequired[NeedSource]
    need_runs: NotRequired[int]
    need_basis: NotRequired[dict[str, object]]
    test_threads: NotRequired[int | None]
    # Requests can record a route. Direct commands without a reason use DIRECT_CARGO.
    route: NotRequired[StepRoute]
    verify: NotRequired[VerifyCall]
    job: NotRequired[StepJob]
    # The caller's own text for the work, such as the mac_queue_run --what text.
    what: NotRequired[str]


@dataclass(frozen=True)
class PriorityUnit:
    """The unit holds this position in the priority order."""
    unit: str
    position: int = 0


@dataclass(frozen=True)
class NormalOrder:
    """The waiter follows the normal queue order."""


class RankedWaiter(Waiter):
    order: PriorityUnit | NormalOrder


def waiter_priority(row: Waiter) -> PriorityUnit | NormalOrder:
    order = cast(Mapping[str, object], row).get("order")
    return order if isinstance(order, (PriorityUnit, NormalOrder)) else NormalOrder()


@dataclass(frozen=True)
class TokenWaitingWaiter:
    """The waiter retains the time when it left to wait for the Cargo token."""
    left_at: float


@dataclass(frozen=True)
class SettledPriority:
    """The queue priority has no pending change."""


@dataclass(frozen=True)
class PendingPriorityTransition:
    """The pending priority change holds its action, time, unit, and actor."""
    id: str
    action: Literal["set", "clear"]
    at: float
    month_row_end: int = 0
    unit: str = ""
    why: str = ""
    set_by: str = ""


class PriorityEntry(TypedDict):
    unit: str
    why: str
    set_at: float
    set_by: str


@dataclass(frozen=True)
class QueuePriority:
    """The queue priority holds ordered entries and any pending change."""
    entries: tuple[PriorityEntry, ...]
    transition: SettledPriority | PendingPriorityTransition = SettledPriority()


def upgrade_queue_priority(value: dict[str, object]) -> dict[str, object]:
    """Read an older priority without changing its stored record."""
    if "entries" in value or not all(key in value for key in ("unit", "why", "set_at", "set_by")):
        return value
    entry = {key: value[key] for key in ("unit", "why", "set_at", "set_by")}
    transition = value.get("transition", {"kind": "SettledPriority"})
    clearing = False
    if isinstance(transition, dict):
        old = cast(dict[str, object], transition)
        clearing = old.get("action") == "clear"
        if old.get("kind") == "PendingPriorityTransition":
            transition = {**old, "unit": entry["unit"], "why": entry["why"], "set_by": entry["set_by"]}
    return {"kind": "QueuePriority", "entries": [] if clearing else [entry], "transition": transition}


@dataclass(frozen=True)
class NoQueuePriority:
    """The queue has no unit priority."""

NO_QUEUE_PRIORITY = NoQueuePriority()


def queue_command_actor() -> str:
    """Identify the user command without a session-role check."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "user"


@dataclass(frozen=True)
class AnonymousRunUse:
    """Count resident anon plus shmem bytes, without swap; the codec uses this name."""
    bytes: int


@dataclass(frozen=True)
class RunUseUnavailable:
    """The reason explains why the run memory use is unavailable."""
    reason: str


@dataclass(frozen=True)
class NoEndedStepStaff:
    """The ended step has no recorded staff owner."""


@dataclass(frozen=True)
class EndedStepFactNotRecorded:
    """The reservation did not record this fact about the ended step."""


@dataclass(frozen=True)
class EndedStepFacts:
    """The reservation's caller, work, and staff facts survive until the hold ends."""

    step: str | EndedStepFactNotRecorded
    package: str | PackageNotRecorded
    project: str | EndedStepFactNotRecorded
    folder: str | EndedStepFactNotRecorded
    staff_id: StaffId | NoEndedStepStaff
    job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED
    caller_name: str | EndedStepFactNotRecorded = field(default_factory=EndedStepFactNotRecorded, kw_only=True)


@dataclass(frozen=True)
class HoldPredatesStepFacts:
    """The saved hold records its memory but has no facts about the ended step."""


@dataclass(frozen=True)
class BetweenStepsSlot:
    """The run retains memory between steps until the slot expires."""
    run: LiveBuildRun
    reservation: str
    bytes: int
    expires_at: float
    identity: EndedStepFacts | HoldPredatesStepFacts


@dataclass(frozen=True)
class ContinuationSlot:
    """The owner continues within a memory slot retained between steps."""
    slot: BetweenStepsSlot
    owner: str

RunSlot = BetweenStepsSlot | ContinuationSlot


@dataclass(frozen=True)
class RunMemory:
    """The run holds its owner, memory need, measured use, and retained bytes."""
    owner: str
    need: int
    use: AnonymousRunUse | RunUseUnavailable
    held: int = 0
    run: LiveBuildRun | NoLiveBuildRun = field(default_factory=NoLiveBuildRun, kw_only=True)


@dataclass(frozen=True)
class RunKeepAside:
    """The run retains this number of bytes for later growth."""
    memory: RunMemory
    bytes: int


@dataclass(frozen=True)
class CapUnavailable:
    """These runs prevent calculation of the memory cap."""
    runs: tuple[RunMemory, ...]


@dataclass(frozen=True)
class MemoryRoom:
    """The memory budget holds available bytes, charges, reserves, and remaining room."""
    available: int
    safety: int
    used: int
    kept: int
    signed_room: int
    cap: int | CapUnavailable
    runs: tuple[RunKeepAside, ...]
    total: int | UnknownHostTotal = field(default_factory=UnknownHostTotal, kw_only=True)
    ci_use: int = field(default=0, kw_only=True)
    agent_swap_demand: int = field(default=0, kw_only=True)

    @property
    def cap_use(self) -> int:
        """Return the swap-backed use and CI use shown inside the cap."""
        return self.used + self.ci_use

    @property
    def room(self) -> int:
        return max(0, self.signed_room)

    @property
    def shortfall(self) -> int:
        return max(0, -self.signed_room)


@dataclass(frozen=True)
class Fits:
    """The memory need fits within the available room."""


@dataclass(frozen=True)
class StartsAlone:
    """The step can start under the rule for a step that runs alone."""


@dataclass(frozen=True)
class WithinSlot:
    """The step fits within its retained memory slot."""


@dataclass(frozen=True)
class DoesNotFit:
    """The memory need exceeds the available room."""

StartBasis = Fits | StartsAlone | WithinSlot | DoesNotFit


@dataclass(frozen=True)
class RoomCheck:
    """The check compares memory need with room and records the decision basis."""
    need: int
    room: int
    passes: bool
    basis: StartBasis = field(kw_only=True)


@dataclass(frozen=True)
class NoOrderHold:
    """The queue order does not delay this step."""


@dataclass(frozen=True)
class HeldBehindHead:
    """This waiter at the queue head delays the step."""
    waiter: Waiter


@dataclass(frozen=True)
class GpuSlotFree:
    """A GPU test slot is available."""


@dataclass(frozen=True)
class GpuSlotWait:
    """A GPU test step waits until a running GPU test step ends."""
    used: int
    slots: int


@dataclass(frozen=True)
class RunLimitFree:
    """The repository run limit permits this step."""


@dataclass(frozen=True)
class RunLimitWait:
    """The step waits for room under its repository run limit."""
    used: int
    limit: int
    repository: str
    machine: str


@dataclass(frozen=True)
class AdmissionDecision:
    """The decision holds admission state, memory charges, and reasons for a delay."""
    state: Literal["admit", "hold"]
    threshold: int
    promised: int
    reserve: int
    measured: int
    pressure: Pressure = NO_PRESSURE_HOLD
    charged: int = 0
    free_passes: bool = True
    order_hold: NoOrderHold | HeldBehindHead = NoOrderHold()
    starts_at: float = 0.0
    reused_slot: bool = False
    room: RoomCheck = RoomCheck(0, 0, True, basis=Fits())
    gpu: GpuSlotFree | GpuSlotWait = GpuSlotFree()
    run_limit: RunLimitFree | RunLimitWait = RunLimitFree()


@dataclass(frozen=True)
class StartSpacingWait:
    """The step waits until this start time to preserve spacing."""
    at: float


@dataclass(frozen=True)
class BuildSpaceLow:
    free_gib: int
    floor_gib: int


@dataclass(frozen=True)
class BuildSpaceUnreadable:
    error: str


BuildSpaceHold = BuildSpaceLow | BuildSpaceUnreadable


@dataclass(frozen=True)
class NoBuildSpaceHold:
    """The pass has no disk space hold."""


@dataclass(frozen=True)
class NeedsMemory:
    """The memory wait holds the need, free bytes, threshold, growth, and reserve."""
    need: int = 0
    free: int = 0
    threshold: int = 0
    growth: int = 0
    reserve: int = 0


@dataclass(frozen=True)
class BehindHead:
    """The waiter holds the identity and join time of the step ahead."""
    head_name: str
    joined_at: float
    head_session_id: str


@dataclass(frozen=True)
class ReadyForAdmission:
    """The step is ready for the next admission check."""


@dataclass(frozen=True)
class ReadingUnavailable:
    """The reason explains why the memory reading is unavailable."""
    reason: str = "memory reading unavailable"


@dataclass(frozen=True)
class EstimateExpired:
    """The estimate has expired since this recorded estimate time."""
    estimated_at: float


@dataclass(frozen=True)
class NatedevHeld:
    """A step waits for the release of a natedev hold."""
    holder: str
    purpose: str

HeadCause = (MemoryPressure | PressureUnavailable | ReadingUnavailable | StartSpacingWait | NeedsMemory | EstimateExpired
             | BehindHead | GpuSlotWait | RunLimitWait | ReadyForAdmission | BuildSpaceHold | placement.CargoFolderWait)


@dataclass(frozen=True)
class HeadAdmissionStatus:
    """The queue head status holds its decision, memory need, and cause."""
    decision: AdmissionDecision
    need: int
    available: int
    cause: HeadCause = field(kw_only=True)

WaitReason = (StartSpacingWait | PressureUnavailable | EstimateExpired | NeedsMemory | BehindHead | GpuSlotWait
              | RunLimitWait | HeadAdmissionStatus | ReadyForAdmission | ReadingUnavailable | MemoryPressure | TokenWaitingWaiter
              | NatedevHeld | BuildSpaceHold | placement.CargoFolderWait)


def held_text(wait: NatedevHeld) -> str:
    if wait.holder and wait.purpose:
        return f"natedev is held by {wait.holder} for {wait.purpose}; waiting for release"
    return f"{wait.holder} holds natedev" if wait.holder else "natedev is held"


def build_space_text(wait: BuildSpaceHold) -> str:
    if isinstance(wait, BuildSpaceUnreadable):
        return f"cannot read /build free space: {wait.error}"
    return f"/build has {wait.free_gib} GiB free, below the {wait.floor_gib} GiB floor"


def gpu_slot_text(wait: GpuSlotWait) -> str:
    return f"waiting for the GPU test slot ({wait.used} of {wait.slots} in use)"


def run_limit_text(wait: RunLimitWait) -> str:
    return f"{wait.used} of {wait.limit} {wait.repository} runs on {wait.machine}"


def head_wait_text(reason: HeadAdmissionStatus, zone: ZoneInfo) -> str:
    cause = reason.cause
    if isinstance(cause, (PressureUnavailable, ReadingUnavailable)):
        return "head: " + cause.reason
    if isinstance(cause, MemoryPressure):
        return "head: " + pressure_text(cause)
    if isinstance(cause, BuildSpaceHold):
        return "head: " + build_space_text(cause)
    if isinstance(cause, StartSpacingWait):
        return "head: next start at " + datetime.fromtimestamp(cause.at, zone).strftime("%H:%M:%S")
    if isinstance(cause, EstimateExpired):
        return "head: estimate expired"
    if isinstance(cause, BehindHead):
        return "head: behind " + select_queue_name(cause.head_name)
    if isinstance(cause, GpuSlotWait):
        return "head: " + gpu_slot_text(cause)
    if isinstance(cause, RunLimitWait):
        return "head: " + run_limit_text(cause)
    if isinstance(cause, ReadyForAdmission):
        return "head: ready for admission"
    if isinstance(cause, placement.CargoFolderWait):
        return "head: " + placement.CARGO_FOLDER_WAIT_REASON
    return f"head: needs {reason.need / GIB:.1f} GiB; room {max(0, reason.decision.room.room) / GIB:.1f} GiB"


@dataclass(frozen=True)
class PackageNotRecorded:
    """The queue record predates the package field. An empty package means workspace scope."""


@dataclass(frozen=True)
class QueueMoveMember:
    """Identify a queue row and show if it can move."""
    row_id: str
    movable: bool = field(default=True, kw_only=True)


@dataclass(frozen=True)
class QueueMoveLine:
    """Keep the move revision and the order of the queue rows."""
    revision: str
    members: tuple[QueueMoveMember, ...]


@dataclass(frozen=True)
class QueueMoveUnavailable:
    """The service did not publish an order that supports moves."""


@dataclass(frozen=True)
class WaitingStep:
    """The waiting step holds its caller, memory need, wait reason, and run identity."""
    pid: int
    session_id: str
    session_name: str
    step: str
    folder: str
    since: float
    need: int
    reason: WaitReason
    unit: str = ""
    project: str = ""
    run: LiveBuildRun | NoLiveBuildRun = field(default=NoLiveBuildRun(), kw_only=True)
    package: str | PackageNotRecorded = field(default=PackageNotRecorded(), kw_only=True)
    route: StepRoute = field(default=DIRECT_CARGO, kw_only=True)
    verify: VerifyStep = field(default=NotVerifyCall(), kw_only=True)
    job: StepJob | GateJobNotRecorded = field(default=GATE_JOB_NOT_RECORDED, kw_only=True)
    ci_runner: str = field(default="", kw_only=True)
    placement_candidates: tuple[placement.CandidateEligibility, ...] = field(default=(), kw_only=True)
    placement_targets: tuple[str, ...] = field(default=(), kw_only=True)
    speculative_copy: bool = field(default=False, kw_only=True)
    what: str = field(default="", kw_only=True)


class ConfirmedGuestRun(TypedDict):
    """Identify a confirmed Mac Linux attempt in a queue reading."""

    target: Literal["mac-linux"]
    attempt: str


@dataclass(frozen=True)
class RunningStep:
    """The running step holds its caller, memory charge, and execution evidence."""
    session_id: str
    session_name: str
    step: str
    folder: str
    promise: int
    measured: StepMemory
    pid: int | PidNotRecorded = field(kw_only=True)
    run: LiveBuildRun | NoLiveBuildRun = NoLiveBuildRun()
    started_at: float = 0.0
    unit: str = ""
    project: str = ""
    execution: execution_identity.ExecutionEvidence = execution_identity.ExecutionNotStarted()
    assignment_host: str = ""
    assignment_target: str = field(default="", kw_only=True)
    placement_candidates: tuple[placement.CandidateEligibility, ...] = field(default=(), kw_only=True)
    confirmed_guest_run: ConfirmedGuestRun | None = field(default=None, kw_only=True)
    mac_placement: str = field(default="", kw_only=True)
    package: str | PackageNotRecorded = field(default=PackageNotRecorded(), kw_only=True)
    route: StepRoute = field(default=DIRECT_CARGO, kw_only=True)
    verify: VerifyStep = field(default=NotVerifyCall(), kw_only=True)
    job: StepJob | GateJobNotRecorded = field(default=GATE_JOB_NOT_RECORDED, kw_only=True)
    ci_runner: str = field(default="", kw_only=True)
    what: str = field(default="", kw_only=True)


def execution_host(step: RunningStep, local_host: str) -> str:
    evidence = step.execution
    if isinstance(evidence, execution_identity.ObservedExecution):
        identity = evidence.identity
        return identity.host if isinstance(identity, execution_identity.ProcessExecution) else identity.target
    return step.assignment_host or local_host


def execution_group(step: RunningStep, fallback: str) -> str:
    evidence = step.execution
    if isinstance(evidence, execution_identity.ObservedExecution):
        identity = evidence.identity
        if isinstance(identity, execution_identity.ProcessExecution):
            return repr((identity.host, identity.generation, identity.pid, identity.start_time))
        return repr((identity.target, identity.generation, identity.instance, identity.attempt))
    if step.assignment_host:
        return repr((step.assignment_host, fallback))
    return run_owner(step.run.pid, step.run.start_time) if isinstance(step.run, LiveBuildRun) else fallback


@dataclass(frozen=True)
class QueueView:
    """The queue view holds waiting steps, running steps, and memory totals at one time."""
    waiting: tuple[WaitingStep, ...]
    running: tuple[RunningStep, ...]
    available: int
    promised: int
    reserve: int
    now: float
    move_line: QueueMoveLine | QueueMoveUnavailable = field(default=QueueMoveUnavailable(), kw_only=True)


@dataclass(frozen=True)
class HostAvailable:
    """The host summary holds available memory, total memory, reserve, and pressure."""
    available: int
    total: int | UnknownHostTotal
    reserve: int
    pressure: Pressure


@dataclass(frozen=True)
class AdmissionTimeNotRecorded:
    """The admission time was not recorded."""


@dataclass(frozen=True)
class ExplainedRunningStep:
    """The running step explanation holds admission time and promised and charged memory."""
    step: RunningStep
    admitted_at: float | AdmissionTimeNotRecorded
    promise: int
    measured: int
    charged: int
    owner: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class QueueMemoryTotals:
    """The totals hold charged memory, measured memory, and expected growth."""
    charged: int
    measured: int
    growth: int


@dataclass(frozen=True)
class WidthNotRecorded:
    """The test thread count was not recorded."""


@dataclass(frozen=True)
class NeedSourceNotRecorded:
    """The source of the memory need was not recorded."""


@dataclass(frozen=True)
class RecordedNeed:
    """The saved estimate holds its source, history, test selection, width, and basis."""
    source: NeedSource
    runs: int
    history_days: int
    scope: TestScope
    package: str
    filters: tuple[str, ...]
    test_threads: int | WidthNotRecorded
    basis: NeedBasis | BasisNotRecorded = BasisNotRecorded()


@dataclass(frozen=True)
class FreeCheck:
    """The check compares free memory with the need, reserve, growth, and threshold."""
    available: int
    need: int
    reserve: int
    growth: int
    threshold: int
    passes: bool


@dataclass(frozen=True)
class HeadExplanation:
    """The queue head explanation holds its estimate, admission checks, priority, and cause."""
    waiter: WaitingStep
    need_source: RecordedNeed | NeedSourceNotRecorded
    decision: AdmissionDecision
    room: RoomCheck
    free: FreeCheck
    has_priority: bool
    cause: HeadCause = field(kw_only=True)


@dataclass(frozen=True)
class NoWaiter:
    """The queue has no waiter to explain."""


@dataclass(frozen=True)
class TokenWaitersOnly:
    """Rows retain their places, but all are waiting for the cargo token."""


@dataclass(frozen=True)
class LineWaiter:
    """The waiter holds its admission decision and memory checks for the queue line."""
    waiter: WaitingStep
    fits_now: bool
    decision: AdmissionDecision
    room: RoomCheck
    free: FreeCheck


@dataclass(frozen=True)
class WaitingLine:
    """The line holds waiter checks and summary counts, sizes, and times."""
    steps: tuple[LineWaiter, ...]
    count: int
    fitting: int
    smallest_need: int
    largest_need: int
    oldest_since: float


@dataclass(frozen=True)
class ReadyToStart:
    """The start spacing permits a step to start now."""


@dataclass(frozen=True)
class NextStartAt:
    """The start spacing permits the next step at this time."""
    at: float

StartSpacing = ReadyToStart | NextStartAt


@dataclass(frozen=True)
class LocalAdmissionOccupancy:
    """The service captures these reservations and owners after it grants starts."""

    reservations: tuple[Reservation, ...]
    verify_run_owners: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class QueueExplanation:
    """The explanation holds queue evidence, policy, decisions, and memory checks."""
    now: float
    host: HostAvailable | ReadingUnavailable
    running: tuple[ExplainedRunningStep, ...]
    totals: QueueMemoryTotals
    head: HeadExplanation | TokenWaitersOnly | NoWaiter
    line: WaitingLine
    decided_from: QueueInputs
    policy: AdmissionPolicy = field(kw_only=True)
    ordered: tuple[RankedWaiter, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    spacing: StartSpacing = ReadyToStart()
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    room: MemoryRoom | ReadingUnavailable = ReadingUnavailable()
    local_occupancy: LocalAdmissionOccupancy | None = field(
        default=None, kw_only=True, metadata={"acquisition_unused": True})


@dataclass(frozen=True)
class QueueInputs:
    """Keep queue evidence separate from the admission policy."""
    ordered: Sequence[tuple[Path, Waiter]]
    running: tuple[RunningStep, ...]
    reservations: tuple[Reservation, ...]
    host: HostMemory | ReadingUnavailable
    pressure: Pressure
    spacing: StartSpacing
    now: float
    priority: QueuePriority | NoQueuePriority = NO_QUEUE_PRIORITY
    blockers_for: frozenset[str] = frozenset()
    waiter_orders: dict[str, PriorityUnit | NormalOrder] = field(default_factory=dict, kw_only=True)
    order_captured: bool = field(default=False, kw_only=True)
    slots: tuple[RunSlot, ...] = field(default=(), kw_only=True)
    cargo_folders: dict[str, placement.CargoFolder] = field(default_factory=dict, kw_only=True)
    occupied_cargo_folders: frozenset[placement.CargoFolder] = field(default=frozenset(), kw_only=True)
    pending_remote_starts: frozenset[str] = field(default=frozenset(), kw_only=True)
    local_waits: dict[str, placement.EvidenceNeeded] = field(default_factory=dict, kw_only=True)
    build_space: BuildSpaceHold | NoBuildSpaceHold = field(default=NoBuildSpaceHold(), kw_only=True)
    local_machine: str = field(default="natedev", kw_only=True)
    run_limit_exempt_owners: frozenset[str] = field(default=frozenset(), kw_only=True)
    verify_run_owners: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]], kw_only=True)
    verify_slot_requests: Mapping[str, str] = field(default_factory=dict[str, str], kw_only=True)
    placement_candidates: dict[str, tuple[placement.CandidateEligibility, ...]] = field(
        default_factory=dict[str, tuple[placement.CandidateEligibility, ...]], kw_only=True)

    def __post_init__(self) -> None:
        orders = {waiter_owner(row): (waiter_priority(row) if isinstance(row.get("order"), (PriorityUnit, NormalOrder))
                                     else self.waiter_orders.get(waiter_owner(row), NormalOrder()))
                  for _, row in self.ordered}
        object.__setattr__(self, "waiter_orders", orders)
        object.__setattr__(self, "ordered", [(path, stored_waiter(row)) for path, row in self.ordered])


@dataclass(frozen=True)
class QueueRead:
    """The queue reading holds its view, explanation, ordered waiters, and diagnostics."""
    view: QueueView
    explanation: QueueExplanation
    ordered: list[tuple[Path, RankedWaiter]]
    diagnostics: tuple[ConfigurationDiagnostic, ...] = ()


@dataclass(frozen=True)
class NoBuildHistoryGeneration:
    """The build history generation was not recorded."""


@dataclass(frozen=True)
class BuildHistoryGeneration:
    """The value identifies the build history generation used for an estimate."""
    value: int


@dataclass(frozen=True)
class StepProfile:
    """The step profile holds repository identity, memory estimate, and test scope."""
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak
    repository: Repository = UnknownRepository()
    scope: TestScope = "full"
    estimated_at: float = 0.0
    build_history_generation: BuildHistoryGeneration | NoBuildHistoryGeneration = NoBuildHistoryGeneration()


@dataclass(frozen=True)
class InferFromArgv:
    """The caller requests inference from the command arguments."""

INFER_FROM_ARGV = InferFromArgv()


@dataclass(frozen=True)
class MeasuredProgramPeak:
    """The program peak holds measured bytes and the number of runs."""
    bytes: int
    runs: int


@dataclass(frozen=True)
class FirstProgramPeak:
    """The first program run uses this initial memory estimate."""
    bytes: int


@dataclass(frozen=True)
class HistoryPeakSizing:
    """The sizing holds the initial peak, current estimate, and estimate time."""
    first_peak_bytes: int
    estimate: MeasuredProgramPeak | FirstProgramPeak
    estimated_at: float


@dataclass(frozen=True)
class ProgramProfile:
    """The program profile holds its repository, worktree, and memory sizing."""
    program: str
    repo: str
    worktree: str
    sizing: HistoryPeakSizing

    @property
    def step(self) -> str:
        return self.program

    @property
    def estimated_at(self) -> float:
        return self.sizing.estimated_at

    @property
    def peak(self) -> ExpectedPeak:
        estimate = self.sizing.estimate
        return (ExpectedPeak(estimate.bytes, "measured", estimate.runs)
                if isinstance(estimate, MeasuredProgramPeak) else ExpectedPeak(estimate.bytes, "fallback", 0))

    @property
    def repository(self) -> UnknownRepository:
        return UnknownRepository()

    @property
    def scope(self) -> TestScope:
        return "full"


@dataclass(frozen=True)
class ConfigurationDiagnostic:
    """The diagnostic describes a configuration problem."""
    diagnostic: str

_warned_configuration_diagnostics: set[ConfigurationDiagnostic] = set()


def warn_configuration_diagnostics(diagnostics: tuple[ConfigurationDiagnostic, ...]) -> None:
    for diagnostic in diagnostics:
        if diagnostic not in _warned_configuration_diagnostics:
            _warned_configuration_diagnostics.add(diagnostic)
            print(diagnostic.diagnostic, file=sys.stderr)

TEST_PHASE_MARGIN_FALLBACK = 1.2


@dataclass(frozen=True)
class MemoryConfig:
    """The configuration holds admission policy, service timings, and memory estimate settings."""
    policy: AdmissionPolicy
    ci_memory_slice: str
    ci_label: str = field(kw_only=True)
    test_phase_margin: float
    dev_size_factor: float
    dev_size_margin_bytes: int
    release_headroom: float
    running_pass_seconds: float
    waiting_pass_seconds: float
    min_pass_gap_seconds: float
    wait_reply_seconds: float
    query_timeout_seconds: float
    person_query_timeout_seconds: float
    replay_retention_seconds: float
    ended_placement_seconds: float
    reconnect_seconds: float
    silence_seconds: float
    outage_notice_seconds: float
    reading_alert_seconds: float
    unreachable_limit_seconds: float
    source_check_seconds: float
    source_settle_seconds: float
    request_max_bytes: int
    program_first_peaks: Mapping[str, int]
    program_sample_seconds: float
    program_stop_seconds: float
    peak_wave_seconds: float
    free_floor_gib: int | None = field(default=None, kw_only=True)
    diagnostics: tuple[ConfigurationDiagnostic, ...] = field(default=(), compare=False)


def _gib_bytes(value: float) -> int:
    return max(1, int(value * GIB))


def _ci_slice_name(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*\.slice", value) is None:
        raise ValueError("expected a systemd slice name")
    return value


def _ci_label(value: str) -> str:
    try:
        _ = value.format(runner="runner", job="job")
    except (KeyError, IndexError, AttributeError, TypeError, ValueError) as error:
        raise ValueError("expected a CI label with only {runner} and {job} fields") from error
    return value


def configuration_diagnostics(messages: Iterable[str]) -> tuple[ConfigurationDiagnostic, ...]:
    return tuple(ConfigurationDiagnostic(message) for message in dict.fromkeys(messages))


def _repository_run_maximum(raw: str) -> int:
    if re.fullmatch(r"[+-]?[0-9]+", raw) is None:
        raise ValueError("expected a whole number")
    return RepositoryRunLimit(int(raw)).maximum


def read_memory_config(*, report: bool = True) -> MemoryConfig:
    """Resolve the live configuration for one admission decision."""
    with conf.diagnostics(report=False) as messages:
        settings_read = conf.read("build_admission")
        keys = settings_read.settings
        values = keys if isinstance(keys, dict) else settings_read.values()

        def value[Value](key: str, convert: Callable[[str], Value], *, use_file_limits: bool = True) -> Value:
            result = settings_read.origin(key, convert, use_file_limits=use_file_limits)
            if isinstance(result, conf.UnavailableValue):
                raise conf.Unusable(f"{result.reason}; no usable committed value")
            if isinstance(result, conf.RecoveredValue):
                messages.append(f"warning: {result.reason}")
            return result.value

        def number(key: str) -> float:
            return value(key, float)

        def whole(key: str) -> int:
            return value(key, int)

        def test_margin() -> float:
            def valid_margin(raw: str) -> float:
                margin = float(raw)
                if not math.isfinite(margin) or margin < 1:
                    raise ValueError("expected a finite margin of at least 1")
                return margin

            result = settings_read.origin("test_phase_margin", valid_margin)
            if isinstance(result, (conf.UnavailableValue, conf.RecoveredValue)):
                messages.append(f"warning: {result.reason}; using test_phase_margin={TEST_PHASE_MARGIN_FALLBACK}")
                return TEST_PHASE_MARGIN_FALLBACK
            return result.value

        def gib(key: str) -> int:
            return value(key, lambda raw: _gib_bytes(float(raw)))

        try:
            running_interval = number("running_pass_seconds")
            timing = AdmissionTiming(*(whole(key) for key in (
                "recovery_start_seconds", "estimate_refresh_seconds", "slot_gap_seconds")), fresh_test_seconds=running_interval)
            pressure = PressurePolicy(*(number(key) for key in (
                "full_pressure_limit", "some_pressure_limit")))
            kinds = {key.removeprefix("peak_hold_seconds."): whole(key)
                     for key in values if key.startswith("peak_hold_seconds.")}
            first_peaks = {key.removeprefix("program_first_peak_gib."): gib(key)
                           for key in values if key.startswith("program_first_peak_gib.")}
            # The admission policy also accepts zero for keys with older file limits.
            run_limits = {
                key.removeprefix("max_runs."): value(key, _repository_run_maximum, use_file_limits=False)
                for key in values if key.startswith("max_runs.") and key != RUN_LIMIT_EXEMPT_KEY
            }
            exempt_unit = value(RUN_LIMIT_EXEMPT_KEY, str) if run_limits else ""
            if "cargo" not in first_peaks:
                first_peaks["cargo"] = gib("program_first_peak_gib.cargo")
            config = MemoryConfig(
                policy=AdmissionPolicy(timing, pressure, PeakHold(whole("peak_hold_seconds"), kinds), gib("safety_gib"),
                                       progress_lookahead_percent=number("progress_lookahead_percent"),
                                       agent_swap_share_percent=whole("agent_swap_share_percent"),
                                       gpu_tests=GpuTestSlots(whole("gpu_test_slots"),
                                                              value("gpu_test_repositories", lambda raw: frozenset(raw.split()))),
                                       repository_runs=RepositoryRunLimits(run_limits, exempt_unit)),
                ci_memory_slice=value("ci_memory_slice", _ci_slice_name),
                ci_label=value("ci_label", _ci_label),
                test_phase_margin=test_margin(),
                dev_size_factor=number("dev_size_factor"),
                dev_size_margin_bytes=value("dev_size_margin_gib", lambda raw: math.ceil(float(raw) * GIB)),
                release_headroom=number("release_headroom"),
                running_pass_seconds=running_interval, waiting_pass_seconds=number("waiting_pass_seconds"),
                min_pass_gap_seconds=number("min_pass_gap_seconds"),
                wait_reply_seconds=number("wait_reply_seconds"), query_timeout_seconds=number("query_timeout_seconds"),
                person_query_timeout_seconds=number("person_query_timeout_seconds"),
                replay_retention_seconds=number("replay_retention_seconds"),
                ended_placement_seconds=number("ended_placement_seconds"),
                reconnect_seconds=number("reconnect_seconds"), silence_seconds=number("silence_seconds"),
                outage_notice_seconds=number("outage_notice_seconds"),
                reading_alert_seconds=number("reading_alert_seconds"),
                unreachable_limit_seconds=number("unreachable_limit_seconds"),
                source_check_seconds=number("source_check_seconds"),
                source_settle_seconds=number("source_settle_seconds"),
                request_max_bytes=whole("request_max_bytes"), program_first_peaks=first_peaks,
                program_sample_seconds=number("program_sample_seconds"), program_stop_seconds=number("program_stop_seconds"),
                peak_wave_seconds=number("peak_wave_seconds"),
                free_floor_gib=whole("free_floor_gib")
                    if settings_read.effective_key("free_floor_gib") in values else None)
        finally:
            diagnostics = configuration_diagnostics(messages)
    config = replace(config, diagnostics=diagnostics)
    if report:
        warn_configuration_diagnostics(diagnostics)
    return config

test_threads = parse.test_threads


def process_start_time(pid: int) -> str | None:
    try:
        stat = read_proc_text(Path("/proc") / str(pid) / "stat")
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19] if fields[0] not in ("Z", "X") else None
    except (OSError, IndexError):
        return None


def format_percent(value: float, *, complete: bool = True) -> str:
    """Round down to one decimal place without showing an early completion."""
    tenths = math.floor(value * 10)
    return f"{(tenths if complete else min(tenths, 999)) / 10:.1f}%"


def pressure_text(pressure: MemoryPressure) -> str:
    if isinstance(pressure.condition, PressureConditionNotRecorded):
        return (f"memory pressure: condition not recorded by service; full avg10 {pressure.full_avg10:.1f}; "
                f"some avg10 {pressure.some_avg10:.1f}; swap {format_percent(pressure.swap_used_percent)} used")
    if pressure.condition == "some":
        return f"memory pressure: some avg10 {pressure.some_avg10:.1f}"
    return f"memory pressure: full avg10 {pressure.full_avg10:.1f}"


def waiter_route(row: Waiter) -> StepRoute:
    """Return the placement route, or keep a direct command where it starts."""
    return row.get("route", DIRECT_CARGO)


def direct_cargo_route() -> LocalRequired:
    """Keep the caller's reason when a direct command must run locally."""
    reason = os.environ.get("BUILD_QUEUE_LOCAL_REASON", "")
    return LocalRequired(reason) if reason else DIRECT_CARGO


def stored_waiter(row: Waiter) -> Waiter:
    """Keep only waiter fields in durable rows and published rows."""
    return cast(Waiter, cast(object, {name: value for name, value in row.items() if name in Waiter.__annotations__}))


@dataclass(frozen=True)
class FreshTest:
    """A matching completed test build proves that the test targets are fresh."""

BUILDLOG_UNIT = "BUILDLOG_UNIT"


@dataclass(frozen=True)
class QueueCaller:
    session_id: str
    name: str
    unit: str


@dataclass(frozen=True)
class StaffLookupSkipped:
    """The caller has no staff reference to resolve."""


NO_STAFF_LOOKUP = StaffLookupSkipped()
type QueueStaff = staff.StaffSnapshot | staff.StaffUnreadable | StaffLookupSkipped


def queue_staff() -> QueueStaff:
    try:
        return staff.local_snapshot()
    except staff.StaffUnreadable as error:
        return error


def queue_caller(session_id: str, staff_id: str, exported_name: str, session_folder: str,
                 *, members: QueueStaff = NO_STAFF_LOOKUP, unit: str = "") -> QueueCaller:
    """Resolve the caller name and its unit for queue priority."""
    reference = session_id or staff_id
    staff_name = ""
    if isinstance(members, staff.StaffSnapshot) and reference and (member := members.by_member_reference(reference)):
        if not unit:
            owner = members.parent_of(member) if member.kind == "worker" else member
            unit = owner.name if owner is not None and owner.kind == "unit" else ""
        session_id = session_id or member.current_session
        staff_name = member.name
    name = select_queue_name(staff_name, exported_name, Path(session_folder).name)
    return QueueCaller(session_id, name, unit)


SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".zsh", ".bash"})
# Python and shell options whose value is the next argument.
VALUE_OPTIONS = frozenset({"-X", "-W", "-o", "+o", "-O", "+O"})
AGENT_COMMANDS = frozenset({"claude", "codex"})
FAMILY_LIMIT = 64


class FamilyProcess(NamedTuple):
    """One process of a caller family, the script file that it runs, and the name of its cgroup unit."""
    argv: tuple[str, ...]
    tty: int
    script: Path | None
    unit: str


def run_script(argv: Sequence[str]) -> Path | None:
    """Return the script file that a shell or Python command runs: its first argument that is not an option."""
    arguments = iter(argv[1:])
    for argument in arguments:
        if argument in VALUE_OPTIONS:
            _ = next(arguments, "")
        elif not argument.startswith(("-", "+")):
            path = Path(argument)
            return path if path.suffix in SCRIPT_SUFFIXES and not any(c.isspace() for c in argument) else None
    return None


@dataclass(frozen=True)
class TerminalOwner:
    """The user started the work: at a terminal, in a login session, or in a desktop app."""
    name: str = "terminal"


TERMINAL_OWNER = TerminalOwner()


@dataclass(frozen=True)
class StaffOwner:
    """A script under the state root of a staff member started the work."""
    member_id: str
    name: str


@dataclass(frozen=True)
class StateSessionOwner:
    """A script under the state root of an id that no staff member has; the name is the short id."""
    name: str


@dataclass(frozen=True)
class OwnerNotGiven:
    """The request does not say who started it; the program names the work that asks."""
    program: str
    unit: str = ""


type FamilyOwner = TerminalOwner | StaffOwner | StateSessionOwner | OwnerNotGiven
# The user's own scopes: a desktop app (app-*.scope) and a login session (session-*.scope).
USER_SCOPE_PREFIXES = ("app-", "session-")


def cgroup_unit(pid: int, proc_root: Path) -> str:
    """The name of the cgroup unit of a process, such as `run-p1-i2.service`; empty when it cannot be read."""
    try:
        cgroup = read_proc_text(proc_root / str(pid) / "cgroup")
    except OSError:
        return ""
    return next((Path(line[3:]).name for line in cgroup.splitlines() if line.startswith("0::")), "")


def service_owner(process: FamilyProcess, pid: int, proc_root: Path, scripts: Sequence[Path]) -> OwnerNotGiven:
    """Name the systemd service that started the work."""
    if not (process.unit.startswith("run-") and process.unit.endswith(".service")):
        return OwnerNotGiven(f"systemd · {process.unit}")
    # systemd-run generates the unit name, so the script or the folder names the work.
    if scripts:
        return OwnerNotGiven(f"systemd · {scripts[-1].name}")
    try:
        return OwnerNotGiven(f"systemd · {Path(os.readlink(proc_root / str(pid) / 'cwd')).name or process.unit}")
    except OSError:
        return OwnerNotGiven(f"systemd · {process.unit}")


def family_owner(pid: int, proc_root: Path) -> FamilyOwner:
    """Find the owner of a process that has no session and no staff reference.

    Read the process and its parents once, up to the first claude or codex process.
    A read error stops the read, and the next rule applies.
    A script under the showrunner state root gives its staff member, or else the short session id.
    A terminal with no agent above it gives TERMINAL_OWNER: the user.
    A family with a process in a systemd service does not say who started it.
    Otherwise a desktop app scope or a login session scope with no agent above it gives TERMINAL_OWNER.
    Every other family, such as an agent family with no session, does not say who started it.
    """
    family: list[FamilyProcess] = []
    under_agent = False
    current = pid
    while current > 0 and len(family) < FAMILY_LIMIT:
        folder = proc_root / str(current)
        try:
            fields = process_fields(current, proc_root)
            tty, parent = int(fields[4]), int(fields[1])
            argv = tuple(part for part in read_proc_text(folder / "cmdline").split("\0") if part)
        except (OSError, IndexError, ValueError):
            break
        if argv and Path(argv[0]).name in AGENT_COMMANDS:
            under_agent = True
            break
        script = run_script(argv)
        if script is not None and not script.is_absolute():
            try:
                script = Path(os.readlink(folder / "cwd")) / script
            except OSError:
                pass
        family.append(FamilyProcess(argv, tty, script, cgroup_unit(current, proc_root)))
        current = parent
    if not family:
        # macOS has no /proc. The caller is the user's terminal.
        return TERMINAL_OWNER
    scripts = [process.script for process in family if process.script is not None]
    state = state_root()
    for script in scripts:
        if script.is_relative_to(state) and len(parts := script.relative_to(state).parts) > 1:
            members = queue_staff()
            member = members.by_member_reference(parts[0]) if isinstance(members, staff.StaffSnapshot) else None
            return StaffOwner(member.id, member.name) if member else StateSessionOwner(parts[0][:8])
    if not under_agent and any(process.tty for process in family):
        return TERMINAL_OWNER
    # A timer or another service can start a scope; a service anywhere in the family means no person started it.
    service = next((process for process in family if process.unit.endswith(".service")), None)
    if service is not None:
        return service_owner(service, pid, proc_root, scripts)
    own = family[0].unit
    if not under_agent and own.startswith(USER_SCOPE_PREFIXES) and own.endswith(".scope"):
        # A desktop app, such as Zed, or a login session started the work: the user.
        return TERMINAL_OWNER
    if scripts:
        return OwnerNotGiven(f"script · {scripts[-1].name}")
    return OwnerNotGiven(f"script · {Path(family[0].argv[0]).name if family[0].argv else 'unknown program'}")


def request_owner(worktree: str) -> QueueCaller | OwnerNotGiven:
    """Find who started a request, in this order: a session, a staff reference, a session name,
    a state-root script or a terminal, and last the recorded owner of the worktree."""
    session = staff.caller_identity()
    reference = os.environ.get("BUILDLOG_OWNER_STAFF_ID", "")
    exported = os.environ.get("CLAUDE_SESSION_NAME", "")
    unit = os.environ.get(BUILDLOG_UNIT, "")
    if session or reference or exported:
        return queue_caller(session, reference, exported, os.environ.get("CLAUDE_SESSION_FOLDER", ""),
            members=queue_staff() if session or reference else NO_STAFF_LOOKUP, unit=unit)
    family = family_owner(os.getpid(), PROC_ROOT)
    if isinstance(family, StaffOwner):
        return queue_caller("", family.member_id, family.name, "", members=queue_staff(), unit=unit)
    if isinstance(family, (TerminalOwner, StateSessionOwner)):
        return QueueCaller("", family.name, unit)
    owner = folder_owner(Path(worktree)) if worktree and Path(worktree).is_absolute() else ""
    if not owner:
        return replace(family, unit=unit)
    members = queue_staff()
    if isinstance(members, staff.StaffSnapshot) and members.by_member_reference(owner):
        return queue_caller("", owner, "", "", members=members, unit=unit)
    return QueueCaller("", owner, unit)


def folder_owner(worktree: Path) -> str:
    """The owner that copies records for a worktree: a gate, a copy, or the staff member that holds it."""
    from scripts.copies import copies
    from scripts.lib import scratch

    try:
        # The queue request reads the owner only; it never clears a stale removal intent.
        return copies.recorded_owner(worktree, intent=scratch.RemovalIntentAccess.READ_ONLY)
    except (copies.CopyError, OSError):
        return ""


OWNER_VARIABLES = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "BUILDLOG_OWNER_STAFF_ID", "CLAUDE_SESSION_NAME")


def owner_given(environment: Mapping[str, str]) -> bool:
    """True when the environment has a variable that says who started the work."""
    return any(environment.get(name) for name in OWNER_VARIABLES)


def named_owner(environment: Mapping[str, str], name: str) -> dict[str, str]:
    """CLAUDE_SESSION_NAME set to this name when the environment says no owner; otherwise nothing."""
    return {} if owner_given(environment) else {"CLAUDE_SESSION_NAME": name}


def owner_environment() -> dict[str, str]:
    """The owner variables for work that leaves this process family, such as a systemd unit.

    A caller with no owner variable passes the owner that its process family gives.
    A staff member goes in BUILDLOG_OWNER_STAFF_ID, so the staff link stays."""
    given = {name: os.environ[name] for name in (*OWNER_VARIABLES, BUILDLOG_UNIT) if os.environ.get(name)}
    if owner_given(given):
        return given
    family = family_owner(os.getpid(), PROC_ROOT)
    if isinstance(family, StaffOwner):
        return {**given, "BUILDLOG_OWNER_STAFF_ID": family.member_id}
    return given if isinstance(family, OwnerNotGiven) else {**given, **named_owner(given, family.name)}


@contextmanager
def default_owner(reference: str) -> Generator[None]:
    """Name this owner for the queue requests of this process and its children when the caller names none.

    The name goes in CLAUDE_SESSION_NAME, and a staff unit also goes in BUILDLOG_UNIT when it is not set.
    The owner never goes in BUILDLOG_OWNER_STAFF_ID: a hold sends its restart notice to that staff member,
    and this owner did not start the run."""
    if not reference or owner_given(os.environ):
        yield
        return
    members = queue_staff()
    caller = (queue_caller("", reference, reference, "", members=members)
              if isinstance(members, staff.StaffSnapshot) else QueueCaller("", reference, ""))
    names = {"CLAUDE_SESSION_NAME": caller.name}
    if caller.unit and not os.environ.get(BUILDLOG_UNIT):
        names[BUILDLOG_UNIT] = caller.unit
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                _ = os.environ.pop(name, None)
            else:
                os.environ[name] = value


def waiter_identity(step: str, worktree: str, owner: QueueCaller | OwnerNotGiven) -> WaiterIdentity:
    """The stored parts of a request: who started it, and the step that says what it does."""
    caller = owner if isinstance(owner, QueueCaller) else QueueCaller("", owner.program, owner.unit)
    identity: WaiterIdentity = {"session_id": caller.session_id, "session_name": caller.name,
        "step": step, "folder": os.getcwd() if worktree else ""}
    if caller.unit:
        identity["unit"] = caller.unit
    return identity


class WaiterIdentity(TypedDict):
    unit: NotRequired[str]
    session_id: str
    session_name: str
    step: str
    folder: str


def waiter_owner(row: Waiter) -> str:
    return run_owner(row["pid"], row["start_time"])


def run_owner(pid: int, start_time: str) -> str:
    """Use one key for a captured process identity."""
    return f"{pid}-{start_time}"


@dataclass(frozen=True)
class SessionHolder:
    """The wait holder is the session with this identifier and name."""
    session_id: str
    session_name: str


@dataclass(frozen=True)
class UnitHolder:
    """The wait holder is this unit."""
    unit: str


@dataclass(frozen=True)
class NoHolder:
    """The wait has no identified holder."""


WaitHolder = SessionHolder | UnitHolder | NoHolder


@dataclass(frozen=True)
class AdmissionWaitSnapshot:
    """The wait snapshot holds the decision, estimate, running work, and queue head reason."""
    decision: AdmissionDecision
    peak: ExpectedPeak
    running: tuple[RunningStep, ...]
    available: int
    repo: str
    step: str
    policy: AdmissionPolicy
    diagnostics: tuple[ConfigurationDiagnostic, ...]
    head_need: int = 0
    head_label: str = ""
    head_reason: WaitReason = ReadyForAdmission()
    holder: WaitHolder = field(default_factory=NoHolder)


def eligible_head_position(waiting: Sequence[WaitingStep]) -> int:
    """Position of the first row eligible for the head rule; requires an eligible row."""
    return next(place for place, step in enumerate(waiting, 1)
                if not isinstance(step.reason, TokenWaitingWaiter))


def behind_place(reason: BehindHead, waiting: Sequence[WaitingStep]) -> int:
    """Find the captured position of the step that holds this waiter."""
    return next((place for place, step in enumerate(waiting, 1)
                 if step.session_id == reason.head_session_id and step.since == reason.joined_at), 0)


def reason_phrase(reason: WaitReason, waiting: Sequence[WaitingStep]) -> str:
    match reason:
        case TokenWaitingWaiter():
            return "waiting for the cargo token"
        case EstimateExpired():
            return "estimate expired"
        case ReadingUnavailable():
            return reason.reason
        case NatedevHeld():
            return held_text(reason)
        case MemoryPressure():
            return "memory pressure"
        case PressureUnavailable():
            return reason.reason
        case StartSpacingWait():
            return "another step just started; the queue waits a few seconds between starts"
        case BuildSpaceLow() | BuildSpaceUnreadable():
            return build_space_text(reason)
        case NeedsMemory():
            return "free memory"
        case placement.CargoFolderWait():
            return placement.CARGO_FOLDER_WAIT_REASON
        case GpuSlotWait():
            return "GPU test slot"
        case RunLimitWait():
            return run_limit_text(reason)
        case BehindHead():
            place = behind_place(reason, waiting)
            return f"behind #{place}" if place else "behind the first step"
        case ReadyForAdmission():
            return "starts at the next check"
        case HeadAdmissionStatus():
            return (f"#{eligible_head_position(waiting)}'s turn" if isinstance(reason.cause, ReadyForAdmission)
                    else reason_phrase(reason.cause, waiting))
        case _:
            assert_never(reason)
