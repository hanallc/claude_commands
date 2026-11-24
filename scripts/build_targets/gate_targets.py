"""Select the default build targets for an unassigned gate."""
from dataclasses import dataclass
from pathlib import Path

from scripts.aws_target.config import AwsOn, aws_switch, cloud_gate_off_reason
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import execution_identity, placement
from scripts.build_queue.admission_client import placement_constraint_builder
from scripts.build_targets import target_config
from scripts.direct.step_selection import Pinned
from scripts.lib import build_folders, conf
from scripts.lib.build_steps import OtherCommand, gate_natedev_only_flag, gate_target_kind, parse_command_program
from scripts.mac_test.claim_state import WHOLE_MAC


@dataclass(frozen=True)
class NoEnabledTarget:
    """No enabled target can run the requested Claude gate."""

    reason: str = "No enabled build target can run this Claude gate. Check enabled.* in build_targets.conf."


def aws_gate_command(repo: Path, command: tuple[str, ...]) -> bool:
    """Allow AWS for the canonical gate script in the checkout."""
    program = parse_command_program(command, keep_path=True)
    return (not isinstance(program, OtherCommand) and Path(command[0]).name not in {"bash", "sh"}
            and (repo / program[0]).resolve() == repo.resolve() / "scripts/production/gate.py"
            and "--nightly" not in program[1] and gate_natedev_only_flag(program[1]) is None)


def aws_gate_candidate() -> placement.PlacementCandidate:
    """Read the cloud gate switch for the AWS scaler."""
    path = conf.path("cloud_gate")
    destination = placement.PlacementDestination("scaler", "aws", "aws", WHOLE_MAC)
    settings = placement.ExplicitProviderSettings(path)
    switch = aws_switch(conf.read_or_repository("cloud_gate"))
    eligibility = (placement.AllowedHost() if isinstance(switch, AwsOn)
                   else placement.FixedExclusion(cloud_gate_off_reason(switch)))
    return placement.PlacementCandidate(destination, eligibility, settings)


def eligible_gate_targets(repo_root: Path, command: tuple[str, ...]) -> tuple[str, ...] | NoEnabledTarget:
    """Select the enabled targets that can run a Claude gate."""
    program = parse_command_program(command)
    if not isinstance(program, OtherCommand) and gate_natedev_only_flag(program[1]):
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
    if aws_gate_command(repo_root, command) and isinstance(aws_gate_candidate().eligibility, placement.AllowedHost):
        names.append("aws")
    return tuple(names) if names else NoEnabledTarget()
