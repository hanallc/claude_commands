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
    OtherCommand, StepJob, command_job, gate_natedev_only_flag, parse_command_program,
)
from scripts.mac_test.claim_state import WHOLE_MAC


@dataclass(frozen=True)
class NoEnabledTarget:
    """No enabled target can run the requested Claude gate."""

    reason: str = "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf."


def aws_gate_command(repo: Path, command: tuple[str, ...]) -> bool:
    """Allow AWS for the canonical gate script in a supported mode."""
    program = parse_command_program(command, keep_path=True)
    if isinstance(program, OtherCommand) or Path(command[0]).name in {"bash", "sh"}:
        return False
    root = repo.resolve()
    path = root / program[0]
    canonical = root / "scripts/production/gate.py"
    arguments = program[1]
    return (path == canonical and path.resolve() == canonical
            and "--nightly" not in arguments and gate_natedev_only_flag(arguments) is None
            and not any(word == "--module" or word.startswith("--module=") for word in arguments))


def scaler_gate_candidate(registry: conf.ConfigRead, target: target_config.ScalerTarget) -> placement.PlacementCandidate:
    """Use the registry switch for this scaler."""
    enabled = registry.value(f"enabled.{target.name}", conf.on_off, default=True, recover_invalid=False)
    destination = placement.PlacementDestination("scaler", target.name, target.name, WHOLE_MAC)
    eligibility = placement.AllowedHost() if enabled else placement.FixedExclusion("off")
    return placement.PlacementCandidate(destination, eligibility, placement.ExplicitProviderSettings(target.settings))


def configured_scaler_gate(registry: conf.ConfigRead, name: str) -> placement.PlacementCandidate:
    """Read the named scaler in the current registry."""
    for target in target_config.read_registered_targets(selected=name,
            purpose=target_config.RegistryRead.PLACEMENT, configuration=registry):
        if isinstance(target, target_config.ScalerTarget):
            return scaler_gate_candidate(registry, target)
    raise conf.Unusable(f"The scaler {name} is absent from the target registry")


def eligible_gate_targets(repo_root: Path, command: tuple[str, ...]) -> tuple[str, ...] | NoEnabledTarget:
    """Select the enabled targets that can run a Claude gate."""
    program = parse_command_program(command)
    if not isinstance(program, OtherCommand) and gate_natedev_only_flag(program[1]):
        return ()
    job = command_job(command)
    if (isinstance(job, StepJob) and job.step == "test-modules"
            and isinstance(outer_reservation(match_cpu_pool=True), OuterReservation)):
        return ()
    try:
        if build_folders.repo_word(repo_root) != "claude":
            return ()
    except build_folders.InvalidFolderForm:
        return ()
    registry = conf.read("build_targets")
    builder = placement_constraint_builder()
    names: list[str] = []
    for target in target_config.read_registered_targets(purpose=target_config.RegistryRead.PLACEMENT, configuration=registry):
        if isinstance(target, target_config.ScalerTarget):
            if (aws_gate_command(repo_root, command)
                    and isinstance(scaler_gate_candidate(registry, target).eligibility, placement.AllowedHost)):
                names.append(target.name)
            continue
        if target.name not in ("natedev", "mac-linux"):
            continue
        enabled = registry.value(f"enabled.{target.name}", conf.on_off,
                                 default=target.name != "mac-linux", recover_invalid=False)
        if not enabled:
            continue
        fixed = builder(repo_root, "", "final", command, NoOwningSession(),
            execution_identity.SourceKeyUnavailable("candidate check"), target_config.ANY_ELIGIBLE_TARGET,
            Pinned(target.name), writes_tree=False)
        if any(candidate.destination.target == target.name and isinstance(candidate.eligibility, placement.AllowedHost)
               for candidate in fixed.candidates):
            names.append(target.name)
    return tuple(names) if names else NoEnabledTarget()
