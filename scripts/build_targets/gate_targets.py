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

PARENT_RESERVATION = "the gate uses its parent reservation"
TARGET_NOT_CONFIGURED = "the target is not configured"


@dataclass(frozen=True)
class NoEnabledTarget:
    """No enabled target can run the requested Claude gate."""

    reason: str = "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf."


def uses_parent_reservation(command: tuple[str, ...]) -> bool:
    """Keep a nested named module under its parent reservation."""
    job = command_job(command)
    return (isinstance(job, StepJob) and job.step == "test-modules"
            and isinstance(outer_reservation(match_cpu_pool=True), OuterReservation))


def scaler_gate_exclusion(registry: conf.ConfigRead, target: target_config.ScalerTarget) -> str:
    """Read the reason when a scaler switch is off."""
    if not registry.value(f"enabled.{target.name}", conf.on_off, default=True, recover_invalid=False):
        return "off"
    if not conf.read(target.settings).value("enabled", conf.on_off, default=False, recover_invalid=False):
        return placement.CLOUD_GATE_OFF_REASON
    return ""


def scaler_candidate(registry: conf.ConfigRead, target: target_config.ScalerTarget) -> placement.PlacementCandidate:
    destination = placement.PlacementDestination("scaler", target.name, target.name, WHOLE_MAC)
    reason = scaler_gate_exclusion(registry, target)
    return placement.PlacementCandidate(destination,
        placement.FixedExclusion(reason) if reason else placement.AllowedHost(),
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


def natedev_only_reason(flag: str) -> str:
    return f"gate.py {flag} runs only on natedev"


def caller_gate_targets(names: tuple[str, ...]) -> placement.GateTargets:
    machines = set(names)
    if "aws" not in machines and machines.difference(placement.GATE_MACHINES):
        try:
            configured = target_config.read_registered_targets(purpose=target_config.RegistryRead.PLACEMENT)
        except (OSError, conf.Unusable):
            configured = ()
        if any(isinstance(target, target_config.ScalerTarget) and target.name in machines for target in configured):
            machines.add("aws")
    return placement.GateTargets(names, tuple((machine, placement.GATE_CALLER_EXCLUSION)
        for machine in placement.GATE_MACHINES if machine not in machines), caller_supplied=True)


def is_claude_gate_source(repo_root: Path) -> bool:
    try:
        return build_folders.repo_word(repo_root) == "claude"
    except build_folders.InvalidFolderForm:
        return False


def eligible_gate_targets(repo_root: Path, command: tuple[str, ...]) -> placement.GateTargets | NoEnabledTarget:
    """Select the enabled targets that can run a Claude gate."""
    if (flag := natedev_only_flag(command)) is not None:
        return placement.GateTargets((), tuple((machine, natedev_only_reason(flag))
            for machine in placement.GATE_MACHINES if machine != "natedev"))
    if not is_claude_gate_source(repo_root):
        return placement.GateTargets((), tuple((machine, "repo") for machine in placement.GATE_MACHINES))
    registry = conf.read("build_targets")
    builder = placement_constraint_builder()
    names: list[str] = []
    eligible: set[str] = set()
    configured = target_config.read_registered_targets(purpose=target_config.RegistryRead.PLACEMENT,
                                                       configuration=registry)
    entries = {name.strip() for name in registry.text("targets").split(",")}
    reasons = {machine: placement.TARGET_READING_UNAVAILABLE if machine in entries else TARGET_NOT_CONFIGURED
               for machine in placement.GATE_MACHINES}
    for target in configured:
        if isinstance(target, target_config.ScalerTarget):
            if reason := scaler_gate_exclusion(registry, target):
                reasons["aws"] = reason
            else:
                names.append(target.name)
                eligible.add("aws")
            continue
        kind = gate_target_kind(target.name)
        if kind is None:
            continue
        enabled = registry.value(f"enabled.{target.name}", conf.on_off,
                                 default=kind != "mac-linux", recover_invalid=False)
        if not enabled:
            reasons[kind] = "off"
            continue
        fixed = builder(repo_root, "", "final", command, NoOwningSession(),
            execution_identity.SourceKeyUnavailable("candidate check"), target_config.ANY_ELIGIBLE_TARGET,
            Pinned(target.name), writes_tree=False)
        for candidate in fixed.candidates:
            if candidate.destination.target != target.name:
                continue
            if isinstance(candidate.eligibility, placement.AllowedHost):
                names.append(target.name)
                if kind != "aws":
                    eligible.add(kind)
                break
            reasons[kind] = candidate.eligibility.reason
    if not names:
        return NoEnabledTarget()
    return placement.GateTargets(tuple(names), tuple((machine, reason)
        for machine, reason in reasons.items() if machine not in eligible))
