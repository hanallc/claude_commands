"""Select the default build targets for an unassigned gate."""
from dataclasses import dataclass
from pathlib import Path

from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import execution_identity, placement
from scripts.build_queue.admission_client import OuterReservation, outer_reservation, placement_constraint_builder
from scripts.build_targets import target_config
from scripts.direct.step_selection import Pinned
from scripts.lib import build_folders, conf
from scripts.lib.build_steps import (
    OtherCommand, StepJob, command_job, gate_natedev_only_flag, gate_target_kind, parse_command_program,
)
from scripts.mac_test.claim_state import WHOLE_MAC


@dataclass(frozen=True)
class NoEnabledTarget:
    """No enabled target can run the requested Claude gate."""

    reason: str = "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf."


def uses_parent_reservation(command: tuple[str, ...]) -> bool:
    """Keep a nested named module under its parent reservation."""
    job = command_job(command)
    return (isinstance(job, StepJob) and job.step == "test-modules"
            and isinstance(outer_reservation(match_cpu_pool=True), OuterReservation))


def scaler_enabled(registry: conf.ConfigRead, target: target_config.ScalerTarget) -> bool:
    """Check both scaler switches before reading provider evidence."""
    return (registry.value(f"enabled.{target.name}", conf.on_off, default=True, recover_invalid=False)
            and conf.read(target.settings).value("enabled", conf.on_off, default=False, recover_invalid=False))


def scaler_candidate(registry: conf.ConfigRead, target: target_config.ScalerTarget) -> placement.PlacementCandidate:
    destination = placement.PlacementDestination("scaler", target.name, target.name, WHOLE_MAC)
    return placement.PlacementCandidate(destination,
        placement.AllowedHost() if scaler_enabled(registry, target) else placement.FixedExclusion("off"),
        placement.ExplicitProviderSettings(target.settings))


def configured_scaler_gate(registry: conf.ConfigRead, name: str) -> placement.PlacementCandidate:
    """Read the named scaler in the current registry."""
    for target in target_config.read_registered_targets(selected=name,
            purpose=target_config.RegistryRead.PLACEMENT, configuration=registry):
        if isinstance(target, target_config.ScalerTarget):
            return scaler_candidate(registry, target)
    raise conf.Unusable(f"The scaler {name} is absent from the target registry")


def natedev_only_flag(command: tuple[str, ...]) -> str | None:
    program = parse_command_program(command)
    return None if isinstance(program, OtherCommand) else gate_natedev_only_flag(program[1])


def is_claude_gate_source(repo_root: Path) -> bool:
    try:
        return build_folders.repo_word(repo_root) == "claude"
    except build_folders.InvalidFolderForm:
        return False


def eligible_gate_targets(repo_root: Path, command: tuple[str, ...]) -> tuple[str, ...] | NoEnabledTarget:
    """Select the enabled targets that can run a Claude gate."""
    if natedev_only_flag(command) is not None or not is_claude_gate_source(repo_root):
        return ()
    registry = conf.read("build_targets")
    builder = placement_constraint_builder()
    names: list[str] = []
    for target in target_config.read_registered_targets(purpose=target_config.RegistryRead.PLACEMENT, configuration=registry):
        if isinstance(target, target_config.ScalerTarget):
            if scaler_enabled(registry, target):
                names.append(target.name)
            continue
        kind = gate_target_kind(target.name)
        if kind is None:
            continue
        enabled = registry.value(f"enabled.{target.name}", conf.on_off,
                                 default=kind != "mac-linux", recover_invalid=False)
        if not enabled:
            continue
        fixed = builder(repo_root, "", "final", command, NoOwningSession(),
            execution_identity.SourceKeyUnavailable("candidate check"), target_config.ANY_ELIGIBLE_TARGET,
            Pinned(target.name), writes_tree=False)
        if any(candidate.destination.target == target.name and isinstance(candidate.eligibility, placement.AllowedHost)
               for candidate in fixed.candidates):
            names.append(target.name)
    return tuple(names) if names else NoEnabledTarget()
