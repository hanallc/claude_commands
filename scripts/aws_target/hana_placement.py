"""Bind Hana lifecycle claims to the queue assignment without an SSH connection."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from scripts.build_targets.execution_transport import (
    ExecutionTransport as ExecutionTransport, SshCommandTransport as SshCommandTransport,
    HanaControlTransport as HanaControlTransport, HanaGpuNextestTransport,
)
from typing import TYPE_CHECKING, Protocol, cast
from scripts.aws_target.hana_generation import HanaHostGeneration as HanaHostGeneration

if TYPE_CHECKING:
    from scripts.aws_target import hana_box as box
    from scripts.aws_target.hana_box import (HanaLifecycleEvidence as HanaLifecycleEvidence,
        HanaUnprovisioned as HanaUnprovisioned, HanaLifecycleUnavailable as HanaLifecycleUnavailable)
    from scripts.build_queue import admission_protocol as wire, admission_records as records, placement
    from scripts.build_targets.target_config import SshTarget
    from scripts.lib import conf


@dataclass(frozen=True)
class DisabledHanaRoute:
    """Configuration grants no Hana placement or lifecycle claim."""


@dataclass(frozen=True)
class EnabledHanaRoute:
    repository_key: str
    kinds: tuple[placement.CommandKind, ...]
    max_steps: int
    free_floor_gib: int


HanaRouteSettings = DisabledHanaRoute | EnabledHanaRoute


@dataclass(frozen=True)
class HanaCheck:
    command: tuple[str, ...]
    recorded_step: str


@dataclass(frozen=True)
class HanaClippyJudgment:
    command: tuple[str, ...]
    recorded_step: str


@dataclass(frozen=True)
class HanaStepRefused:
    reason: str


HanaStepPolicy = HanaCheck | HanaClippyJudgment | HanaStepRefused


@dataclass(frozen=True)
class HanaClaimPending:
    reference: placement.ProviderClaimReference
    claim: box.WorkClaim
    deadline: int


@dataclass(frozen=True)
class HanaClaimAdopted:
    reserved: placement.ReservedHost
    generation: HanaHostGeneration


@dataclass(frozen=True)
class HanaClaimRefused:
    reason: str


@dataclass(frozen=True)
class HanaClaimRetained:
    reference: placement.ProviderClaimReference
    reason: str


HanaClaimResult = HanaClaimPending | HanaClaimAdopted | HanaClaimRefused | HanaClaimRetained


@dataclass(frozen=True)
class RemoteStepWaiter:
    """An exact assignment and live holder establish remote waiting ownership."""
    assignment: placement.PlacementAssignment
    holder: placement.ClaimHolderIdentity
    reference: placement.ProviderClaimReference



class _PlacementAdapter(Protocol):

    def read_execution_transport(self, target: SshTarget) -> ExecutionTransport:
        ...

    def read_settings(self, configuration: conf.ConfigRead) -> HanaRouteSettings:
        ...

    def classify_command(self, step: str, kind: str, command: tuple[str, ...]) -> HanaStepPolicy:
        ...

    def classify_step(self, profile: records.StepProfile | records.ProgramProfile, constraints: placement.PlacementConstraints) -> HanaStepPolicy:
        ...

    def controller_for(self, transport: HanaControlTransport, *, read_only: bool=False) -> box.HanaController:
        ...

    def validate_assignment(self, assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity) -> HanaControlTransport | HanaClaimRefused:
        ...

    def claim_assigned(self, assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity, controller: box.HanaController, *, worktree: str='') -> HanaClaimResult:
        ...

    def release_assigned(self, assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity, controller: box.HanaController) -> bool:
        ...

    def fixed_policy(self, transport: HanaControlTransport, repository: str, kind: str) -> HanaStepRefused | EnabledHanaRoute:
        ...

    def read_evidence(self, key: placement.HostEvidenceKey, request: wire.PlacementRequest) -> placement.HostEvidence:
        ...


    def gpu_setup(self, transport: HanaGpuNextestTransport) -> tuple[str, ...] | HanaStepRefused:
        ...

    def gpu_step(self, transport: HanaGpuNextestTransport, repository: str, package: str, kind: str,
                 command: tuple[str, ...]) -> placement.AllowedHost | placement.FixedExclusion:
        ...

    def run_gpu_assignment(self, assignment: placement.PlacementAssignment, repo: Path, package: str,
                           command: tuple[str, ...], result_file: Path) -> int:
        ...


def _adapter() -> _PlacementAdapter:
    from importlib import import_module
    return cast(_PlacementAdapter, cast(object, import_module('scripts.aws_target.hana_placement_adapter')))


def read_execution_transport(target: SshTarget) -> ExecutionTransport:
    """Select the configured transport before reading SSH settings."""
    return _adapter().read_execution_transport(target)


def read_settings(configuration: conf.ConfigRead) -> HanaRouteSettings:
    """Read the workload without reading credentials or deployment records."""
    return _adapter().read_settings(configuration)


def classify_command(step: str, kind: str, command: tuple[str, ...]) -> HanaStepPolicy:
    """Permit direct Cargo judgments without repair or command wrappers."""
    return _adapter().classify_command(step, kind, command)


def classify_step(profile: records.StepProfile | records.ProgramProfile, constraints: placement.PlacementConstraints) -> HanaStepPolicy:
    return _adapter().classify_step(profile, constraints)


def controller_for(transport: HanaControlTransport, *, read_only: bool=False) -> box.HanaController:
    """Use the existing account directory and lifecycle controller."""
    return _adapter().controller_for(transport, read_only=read_only)


def validate_assignment(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity) -> HanaControlTransport | HanaClaimRefused:
    """Check the configured destination, scope, source, and live local holder."""
    return _adapter().validate_assignment(assignment, holder)


def claim_assigned(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity, controller: box.HanaController, *, worktree: str='') -> HanaClaimResult:
    """Commit or inspect only the claim for this exact assignment."""
    return _adapter().claim_assigned(assignment, holder, controller, worktree=worktree)


def generation_text(generation: HanaHostGeneration) -> str:
    """Return the stable queue generation for one verified boot."""
    digest = hashlib.sha256()
    for value in (generation.deployment, generation.start_token, generation.boot_id):
        content = value.encode('utf-8')
        digest.update(len(content).to_bytes(8, 'big') + content)
    return digest.hexdigest()


def release_assigned(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity, controller: box.HanaController) -> bool:
    """Release the exact target claim after durable execution and lifecycle settlement."""
    return _adapter().release_assigned(assignment, holder, controller)


def fixed_policy(transport: HanaControlTransport, repository: str, kind: str) -> HanaStepRefused | EnabledHanaRoute:
    """Apply configuration and repository capabilities without lifecycle reads."""
    return _adapter().fixed_policy(transport, repository, kind)


def read_evidence(key: placement.HostEvidenceKey, request: wire.PlacementRequest) -> placement.HostEvidence:
    """Project existing lifecycle and execution records without provider mutations."""
    return _adapter().read_evidence(key, request)


def gpu_setup(transport: HanaGpuNextestTransport) -> tuple[str, ...] | HanaStepRefused:
    return _adapter().gpu_setup(transport)


def gpu_step(transport: HanaGpuNextestTransport, repository: str, package: str, kind: str,
             command: tuple[str, ...]) -> placement.AllowedHost | placement.FixedExclusion:
    return _adapter().gpu_step(transport, repository, package, kind, command)


def run_gpu_assignment(assignment: placement.PlacementAssignment, repo: Path, package: str,
                       command: tuple[str, ...], result_file: Path) -> int:
    return _adapter().run_gpu_assignment(assignment, repo, package, command, result_file)


def __getattr__(name: str) -> object:
    """Load lifecycle projection types only when a backend requests them."""
    if name in ('HanaLifecycleEvidence', 'HanaUnprovisioned', 'HanaLifecycleUnavailable'):
        from importlib import import_module
        return cast(object, getattr(import_module('scripts.aws_target.hana_box'), name))
    raise AttributeError(name)
