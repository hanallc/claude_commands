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


@dataclass(frozen=True)
class NoEnabledTarget:
    """No enabled target can run the requested Claude gate."""

    reason: str = "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf."


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
    for target in target_config.read_targets(purpose=target_config.RegistryRead.PLACEMENT, configuration=registry):
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
