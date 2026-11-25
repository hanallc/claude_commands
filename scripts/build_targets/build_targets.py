#!/usr/bin/env python3
"""Show the recorded state of each configured build target."""

from __future__ import annotations

from typing import TYPE_CHECKING
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import io
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, nullcontext, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol, TypedDict, cast, final, get_args

from scripts.build_queue import execution_identity
from scripts.build_hold import owning_session as session_owner
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_hold import override_guard
from scripts.mac_test import claim_state
from scripts.build_targets.execution_transport import HanaGpuNextestTransport
from scripts.lib import conf
from scripts.lib.display_text import display_field
from scripts.build_queue.placement import (
    MAC_RUN_KINDS, CommandKind, ConfiguredProviderSettings, ExplicitProviderSettings, ProviderSettings,
)

CONFIGURED_PROVIDER_SETTINGS = ConfiguredProviderSettings()


if TYPE_CHECKING:
    from scripts.mac_test import offload
    from scripts.build_hold import build_hold
    from scripts.build_queue.placement import SubmittingSession
    from scripts.build_queue import placement
    from scripts.mac_test import eligibility, mac_test
    from scripts.message import roster
    from scripts.build_targets.targets import (
        Held, KeptClaim, RunningJob, TargetState,
    )


class CliArguments(argparse.Namespace):
    """Arguments accepted by the build-target status command."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.probe: bool = False
        self.call_id: str = ""
        self.pid: int = 0
        self.signal_name: str = "TERM"
        self.repo_root: str = ""
        self.package: str = ""
        self.kind: str = ""
        self.local_reason: str = ""
        self.leave_hook: str = ""
        self.caller: str = ""
        self.reservation_file: str = ""
        self.target: str = ""
        self.new_step: bool = False
        self.recovery_fd: int = -1
        self.probe_marks_file: str = ""
        self.recover_only: bool = False
        self.no_probe: bool = False
        self.mac_only: bool = False
        self.remote_kinds_file: str = ""
        self.session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION
        self.claim_identity_file: str = ""
        self.claim_id: str = ""
        self.episode: str = ""
        self.attempt: str = ""
        self.assignment_file: str = ""
        self.result_file: str = ""
        self.argv: list[str] = []


def positive_pid(value: str) -> int:
    pid = int(value)
    if pid <= 0:
        raise argparse.ArgumentTypeError("must be a positive PID")
    return pid


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    reserve = commands.add_parser("reserve", aliases=["recover-reservation"])
    _ = reserve.add_argument("--repo-root", required=True)
    _ = reserve.add_argument("--package", default="")
    _ = reserve.add_argument("--kind", required=True)
    _ = reserve.add_argument("--pid", type=positive_pid, required=True)
    _ = reserve.add_argument("--reservation-file", required=True)
    _ = reserve.add_argument("--claim-identity-file", default="")
    _ = reserve.add_argument("--assignment-file", default="")
    _ = reserve.add_argument("--target", default="")
    _ = reserve.add_argument("--recover-only", action="store_true")
    _ = reserve.add_argument("--new-step", action="store_true")
    gpu = commands.add_parser("run-hana-gpu")
    _ = gpu.add_argument("--assignment-file", required=True)
    _ = gpu.add_argument("--repo-root", required=True)
    _ = gpu.add_argument("--package", required=True)
    _ = gpu.add_argument("--result-file", required=True)
    _ = gpu.add_argument("argv", nargs=argparse.REMAINDER)
    stop_gpu = commands.add_parser("stop-hana-gpu")
    _ = stop_gpu.add_argument("--assignment-file", required=True)
    info = commands.add_parser("assigned-info")
    _ = info.add_argument("--assignment-file", required=True)
    recovery = commands.add_parser("recover-probe", help=argparse.SUPPRESS)
    _ = recovery.add_argument("--repo-root", required=True)
    _ = recovery.add_argument("--package", default="")
    _ = recovery.add_argument("--kind", required=True)
    _ = recovery.add_argument("--target", required=True)
    _ = recovery.add_argument("--recovery-fd", type=int, required=True)
    release = commands.add_parser("release-wait")
    _ = release.add_argument("--pid", type=positive_pid, required=True)
    for field in ("claim-id", "episode", "attempt"):
        _ = release.add_argument("--" + field, default="")

    def parsed_session(value: str) -> SubmittingSession:
        return OwningSession(value) if value else NoOwningSession()

    _ = release.add_argument("--session", type=parsed_session, default=claim_state.CALLING_SESSION)
    _ = commands.add_parser("submitting-session")
    source = commands.add_parser("source-key")
    _ = source.add_argument("--repo-root", required=True)
    backoff = commands.add_parser("probe-backoff")
    _ = backoff.add_argument("--target", required=True)
    _ = backoff.add_argument("--no-probe", action="store_true")
    status = commands.add_parser("status")
    _ = status.add_argument("--probe", action="store_true")
    _ = commands.add_parser("setup-mac-linux", help="Repair the state root for the Mac Linux gate on natedev")
    eligible = commands.add_parser("eligible", aliases=["constraints"])
    _ = eligible.add_argument("--repo-root", required=True)
    _ = eligible.add_argument("--package", default="")
    _ = eligible.add_argument("--kind", choices=("check", "lint", "doc", "test", "build", "example", "final"), default="")
    _ = eligible.add_argument("--local", dest="local_reason", default="")
    _ = eligible.add_argument("--target", default="")
    _ = eligible.add_argument("--mac", dest="mac_only", action="store_true")
    _ = eligible.add_argument("--remote-kinds-file", default="")
    _ = eligible.add_argument("--new-step", action="store_true")
    _ = eligible.add_argument("--probe-marks-file", default="")
    register = commands.add_parser("register-local")
    _ = register.add_argument("--call-id", required=True)
    _ = register.add_argument("--pid", type=positive_pid, required=True)
    waiter = commands.add_parser("wait-local")
    _ = waiter.add_argument("--call-id", required=True)
    _ = waiter.add_argument("--pid", type=positive_pid, default=0)
    _ = waiter.add_argument("--leave-hook", default="")
    _ = waiter.add_argument("--caller", default="")
    check = commands.add_parser("check-local")
    _ = check.add_argument("--caller", default="")
    for local_command in (register, waiter, check):
        _ = local_command.add_argument("--session", type=parsed_session, default=claim_state.CALLING_SESSION)
    interrupt = commands.add_parser("interrupt-local")
    _ = interrupt.add_argument("--call-id", required=True)
    _ = interrupt.add_argument("--signal", dest="signal_name", choices=("INT", "TERM"), default="TERM")
    return root


from scripts.build_targets.target_config import (
    ANY_ELIGIBLE_TARGET,
    ConfigError,
    EligibleTarget,
    ExcludedTarget,
    LocalTarget,
    LocalRequired,
    Routing,
    RegistryRead,
    SshTarget,
    WRITES_TREE,
    read_targets,
)
from scripts.direct.step_selection import Pinned, ROUTED, TargetSelection


class ActiveWaitProcess(TypedDict):
    call_id: str
    pid: int
    proc_start: str


def target_state(target: LocalTarget | SshTarget) -> TargetState:
    """Load the state reader only when the command reads target state."""
    from scripts.build_targets.targets import target_state as read_target_state

    if isinstance(target, SshTarget):
        from scripts.aws_target import hana_placement
        transport = hana_placement.read_execution_transport(target)
        if isinstance(transport, HanaGpuNextestTransport):
            from scripts.build_targets.targets import Unreachable

            setup = hana_placement.gpu_setup(transport)
            if isinstance(setup, hana_placement.HanaStepRefused):
                return Unreachable(setup.reason)
        if isinstance(transport, hana_placement.HanaControlTransport):
            from scripts.build_targets.targets import Free, StateUnreadable, Unreachable
            from scripts.aws_target import hana_box
            controller = hana_placement.controller_for(transport, read_only=True)
            snapshot = controller.inspect()
            if isinstance(snapshot, hana_placement.HanaLifecycleUnavailable):
                return StateUnreadable(snapshot.reason)
            if isinstance(snapshot, hana_placement.HanaUnprovisioned):
                return Unreachable('Hana is unprovisioned')
            if isinstance(snapshot, hana_placement.HanaResourcesHeld):
                return Unreachable('Hana is busy or recovering')
            if not controller.image_is_current(snapshot.deployment):
                return Unreachable(hana_box.HANA_IMAGE_STALE)
            if snapshot.claims or snapshot.state not in (hana_box.Lifecycle.READY, hana_box.Lifecycle.IDLE_STOPPED):
                return Unreachable('Hana is busy or recovering')
            return Free()
    return read_target_state(target)


def local_time(instant: datetime) -> str:
    from scripts.lib.user_zone import user_zone

    return instant.astimezone(user_zone()).strftime("%H:%M %Z")


def unit_label_suffix(state: RunningJob | Held, labels: Mapping[RunningJob | Held | KeptClaim, roster.UnitLabels]) -> str:
    identity = labels.get(state)
    if identity is None:
        return ""
    return f" (showrunner {display_field(identity.showrunner)}, unit {display_field(identity.unit)})"


_NO_UNIT_LABELS: Mapping[RunningJob | Held | KeptClaim, roster.UnitLabels] = {}


def kept_claims_suffix(kept: tuple[KeptClaim, ...],
                       labels: Mapping[RunningJob | Held | KeptClaim, roster.UnitLabels]) -> str:
    """Name each kept claim with its owner: the holder session name when known, else the repository."""
    from scripts.build_targets.targets import kept_claim_text
    from scripts.lib.user_zone import user_zone

    zone = user_zone()
    return "".join(" · " + display_field(kept_claim_text(claim, labels[claim].session if claim in labels else "", zone))
                   for claim in kept)


def running_job_text(job: RunningJob,
                     labels: Mapping[RunningJob | Held | KeptClaim, roster.UnitLabels] = _NO_UNIT_LABELS) -> str:
    from scripts.lib.user_zone import user_zone
    from scripts.build_targets.targets import IdleSince

    text = f"running {display_field(job.what)} since {local_time(job.since)}"
    text += unit_label_suffix(job, labels)
    if isinstance(job.idle, IdleSince):
        text += f", idle since {job.idle.when.astimezone(user_zone()).strftime('%H:%M')}"
    return text


def format_state(name: str, state: TargetState,
                 labels: Mapping[RunningJob | Held | KeptClaim, roster.UnitLabels] = _NO_UNIT_LABELS) -> str:
    from scripts.build_targets.targets import (
        ExpiresAt, Free, ReleaseEstimated, ReleaseOverdue, Running, StateUnreadable, Unreachable,
    )

    name = display_field(name)
    if isinstance(state, Free):
        return f"{name}: free" + kept_claims_suffix(state.kept_claims, labels)
    if isinstance(state, Running):
        return (f"{name}: " + "; ".join(running_job_text(job, labels) for job in state.jobs)
                + kept_claims_suffix(state.kept_claims, labels))
    if isinstance(state, Unreachable):
        return f"{name}: unreachable — {display_field(state.reason)}"
    if isinstance(state, StateUnreadable):
        return f"{name}: state unreadable — {display_field(state.reason)}"
    since = f" since {local_time(state.since)}" if isinstance(state.since, datetime) else ", start not recorded"
    line = f"{name}: held by {display_field(state.holder)}{since}"
    line += unit_label_suffix(state, labels)
    if isinstance(state.end, ExpiresAt):
        line += f" until {local_time(state.end.when)}"
    elif isinstance(state.end, ReleaseEstimated):
        line += f" release expected {local_time(state.end.when)}"
    elif isinstance(state.end, ReleaseOverdue):
        line += f" release overdue since {local_time(state.end.when)}"
    reason = display_field(state.reason)
    return (f"{line} — {reason}" if reason else line) + kept_claims_suffix(state.kept_claims, labels)


def probe_target(target: SshTarget) -> TargetState:
    from scripts.build_targets.targets import Unreachable
    from scripts.mac_test import offload

    config = ssh_target_config(target)
    if isinstance(config, ExcludedTarget):
        return target_state(target)
    result = offload.probe_reachability(offload.ProcessController(), config)
    if isinstance(result, offload.UnreachableHost):
        return Unreachable(result.reason)
    return target_state(target)


def command_status(probe: bool = False) -> int:


    from scripts.buildlog import mac_idle
    from scripts.lib.user_zone import user_zone
    from scripts.message import roster
    from scripts.build_targets.targets import Free, Held, HolderSession, Running

    try:
        targets = read_targets()
    except ConfigError as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2
    states: list[tuple[str, TargetState]] = []
    holders: dict[RunningJob | Held | KeptClaim, str] = {}
    for target in targets:
        state = (
            probe_target(target)
            if probe and isinstance(target, SshTarget)
            else target_state(target)
        )
        states.append((target.name, state))
        jobs = ((*state.jobs, *state.kept_claims) if isinstance(state, Running) else state.kept_claims
                if isinstance(state, Free) else (state, *state.kept_claims) if isinstance(state, Held) else ())
        for job in jobs:
            if isinstance(job.holder_session, HolderSession):
                holders[job] = job.holder_session.session_id
    try:
        identities = roster.unit_labels_for(holders.values(), os.environ) if holders else {}
    except Exception:
        identities = {}
    labels = {state: identities[session_id] for state, session_id in holders.items() if session_id in identities}
    alert = mac_idle.view_alert_line(time.time(), user_zone())
    if alert is not None:
        print(f"\033[1;31m{alert}\033[0m" if sys.stdout.isatty() else alert)
    for name, state in states:
        print(format_state(name, state, labels))
    return 0


def command_eligible(
    repo_root: Path, package: str, routing: Routing = ANY_ELIGIBLE_TARGET
) -> int:
    from scripts.build_targets.targets import target_eligibility

    try:
        eligibility = target_eligibility(repo_root, package, routing)
    except (ConfigError, OSError, UnicodeError) as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2
    for result in eligibility:
        print_eligible_target(result)
    return 0


def print_eligible_target(result: EligibleTarget | ExcludedTarget) -> None:
    target = result.target
    disposition = "eligible" if isinstance(result, EligibleTarget) else "excluded"
    reason = "-" if isinstance(result, EligibleTarget) else display_field(result.reason)
    settings = str(target.settings) if isinstance(target, SshTarget) else "-"
    platform = target.platform if isinstance(target, SshTarget) else "-"
    print(f"{target.name}\t{disposition}\t{reason}\t{settings}\t{platform}")


StepKind = Literal["check", "lint", "doc", "test", "build", "example", "final"]


@dataclass(frozen=True)
class AnyTarget:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


def step_routing_rule(kind: CommandKind, routing: Routing = ANY_ELIGIBLE_TARGET,
                      selection: TargetSelection = ROUTED, *, writes_tree: bool = False
                      ) -> AnyTarget | NatedevOnly:
    """Choose the run location for each step before target admission."""
    if writes_tree:
        return NatedevOnly(WRITES_TREE.reason)
    if isinstance(routing, LocalRequired):
        return NatedevOnly(routing.reason)
    if isinstance(selection, Pinned):
        return AnyTarget()
    if kind == "final":
        return NatedevOnly("full gate runs on natedev")
    return AnyTarget()


# The command kind of a hand made run. The Mac takes it as a Mac run, and the guest line takes it as a gate step.
HAND_MADE_KIND: CommandKind = "run"


def mac_run_bypasses_repository_policy(target: LocalTarget | SshTarget, kind: str) -> bool:
    """A Mac run has no repository step policy."""
    return isinstance(target, SshTarget) and target.platform == "macos" and kind in MAC_RUN_KINDS


def fixed_placement_constraints(
    repo_root: Path, package: str, kind: CommandKind, command: tuple[str, ...],
    session: SubmittingSession, source_key: str | execution_identity.SourceIdentity, routing: Routing = ANY_ELIGIBLE_TARGET,
    selection: TargetSelection = ROUTED, *, writes_tree: bool = False,
    provider_settings: ProviderSettings = CONFIGURED_PROVIDER_SETTINGS,
) -> placement.PlacementConstraints:
    """Keep dynamic host conditions out of the submitted candidate set."""
    from scripts.build_queue import placement
    from scripts.buildlog import store
    from scripts.mac_test import eligibility

    rule = step_routing_rule(kind, routing, selection, writes_tree=writes_tree)
    if writes_tree:
        selection = ROUTED
    repository = eligibility.repository_identity(repo_root)
    candidates: list[placement.PlacementCandidate] = []
    selected = selection.target if isinstance(selection, Pinned) and isinstance(rule, AnyTarget) else ""
    with conf.report_key_changes():
        for target in read_targets(selected=selected, purpose=RegistryRead.PLACEMENT):
            if isinstance(target, LocalTarget):
                destination = placement.PlacementDestination("local", target.name, store.host_name(), claim_state.WHOLE_MAC)
                candidates.append(placement.PlacementCandidate(destination))
                continue
            settings: ProviderSettings = CONFIGURED_PROVIDER_SETTINGS
            if isinstance(provider_settings, ExplicitProviderSettings) and isinstance(selection, Pinned) and target.name == selection.target:
                target = replace(target, settings=provider_settings.path)
                settings = provider_settings
            candidates.extend(fixed_remote_candidates(
                target, settings, repository, repo_root, package, kind, command, rule, selection=selection))
    source = execution_identity.SourceKeyKnown(source_key) if isinstance(source_key, str) else source_key
    return placement.PlacementConstraints(source, command, str(repo_root), package, session, tuple(candidates),
        placement.PinnedHost(selection.target) if isinstance(selection, Pinned) else placement.ConfiguredOrder(),
        command_kind=kind)


def fixed_remote_candidates(
    target: SshTarget, settings: ProviderSettings, repository: eligibility.RepositoryIdentity,
    repo_root: Path, package: str, kind: CommandKind, command: tuple[str, ...], rule: AnyTarget | NatedevOnly,
    *, selection: TargetSelection,
) -> tuple[placement.PlacementCandidate, ...]:
    """Read the remote keys that determine the fixed candidate set."""
    from scripts.aws_target import hana_placement, hana_placement_adapter
    from scripts.build_queue import placement
    from scripts.build_targets import target_config
    from scripts.mac_test import eligibility

    if target.name == eligibility.MAC_LINUX_TARGET:
        return (fixed_mac_linux_candidate(target, settings, repo_root, kind, command, rule),)

    provider: Literal["mac", "ssh"] = "mac" if target.platform == "macos" else "ssh"
    policy: placement.AllowedHost | placement.FixedExclusion = placement.AllowedHost()
    try:
        configuration = conf.read(target.settings)
        transport = target_config.read_execution_transport(target, configuration=configuration)
        if isinstance(transport, HanaGpuNextestTransport):
            scope = (claim_state.RepositoryScope(repository.name, repository.key)
                     if isinstance(repository, eligibility.KnownRepository) else claim_state.WHOLE_MAC)
            if isinstance(rule, NatedevOnly):
                policy = placement.FixedExclusion(rule.reason)
            elif not isinstance(selection, Pinned) or selection.target != target.name:
                policy = placement.FixedExclusion('This target requires --target hana-gpu')
            else:
                policy = hana_placement_adapter.gpu_step(transport,
                    repository.key if isinstance(repository, eligibility.KnownRepository) else '',
                    package, kind, command, configuration=configuration)
            destination = placement.PlacementDestination('ssh', target.name, transport.host, scope, 1)
            return (placement.PlacementCandidate(destination, policy, settings),)
        if isinstance(transport, hana_placement.HanaControlTransport):
            scope = (claim_state.RepositoryScope(repository.name, repository.key)
                     if isinstance(repository, eligibility.KnownRepository) else claim_state.WHOLE_MAC)
            route = hana_placement_adapter.configured_route(configuration, str(repo_root), kind)
            if isinstance(rule, NatedevOnly):
                policy = placement.FixedExclusion(rule.reason)
            elif isinstance(route, hana_placement.HanaStepRefused):
                policy = placement.FixedExclusion(route.reason)
            elif command:
                judgment = hana_placement.classify_command('check' if kind == 'check' else 'clippy', kind, command)
                if isinstance(judgment, hana_placement.HanaStepRefused):
                    policy = placement.FixedExclusion('command')
            destination = placement.PlacementDestination('ssh', target.name, transport.host, scope, 1)
            return (placement.PlacementCandidate(destination, policy, settings),)

        paths = claim_state.state_paths(target.name)
        if paths.platform == "macos":
            identity = claim_state.mac_repository_identity(str(repo_root))
            if isinstance(identity, claim_state.MacMirrorIdentity):
                scope = claim_state.RepositoryScope(identity.mirror_name, identity.key)
                repository = eligibility.KnownRepository(identity.mirror_name, identity.key)
            else:
                scope = claim_state.WHOLE_MAC
                repository = eligibility.UNKNOWN_REPOSITORY
        else:
            scope = claim_state.target_claim_scope(paths, str(repo_root))
        host = configuration.text("host")
        if isinstance(rule, NatedevOnly):
            policy = placement.FixedExclusion(rule.reason)
        elif (selection == ROUTED and target_config.uses_legacy_mac_layout(target.platform)
                and isinstance(repository, eligibility.KnownRepository)
                and kind in configuration.value(f"offload_exclude.{repository.name}",
                                                eligibility.comma_values, default=set(), allow_blank=True)):
            policy = placement.FixedExclusion("offload_exclude")
        elif not mac_run_bypasses_repository_policy(target, kind):
            answer = eligibility.configured_step_policy(configuration, target.platform, repository, repo_root, package, kind)
            if isinstance(answer, eligibility.IneligibleMacStep):
                policy = placement.FixedExclusion(answer.reason)
    except conf.Unusable:
        destination = placement.PlacementDestination(provider, target.name, target.name, claim_state.WHOLE_MAC)
        return (placement.PlacementCandidate(destination, placement.FixedExclusion("state unreadable"), settings),)

    destination = placement.PlacementDestination(provider, target.name, host, scope,
                                                 1 if target.platform == "linux" else 0)
    return (placement.PlacementCandidate(destination, policy, settings),)


def fixed_mac_linux_candidate(target: SshTarget, settings: ProviderSettings, repo_root: Path, kind: CommandKind,
                               command: tuple[str, ...], rule: AnyTarget | NatedevOnly
                               ) -> placement.PlacementCandidate:
    """Name the single guest slot for a supported gate or a hand made run (kind HAND_MADE_KIND)."""
    from scripts.build_queue import placement
    from scripts.build_queue.admission_jobs import StepJob
    from scripts.build_targets import mac_linux
    from scripts.lib import build_folders
    from scripts.lib.build_steps import gate_job_for_command

    host = target.name
    policy: placement.AllowedHost | placement.FixedExclusion
    job = gate_job_for_command(command)
    if isinstance(rule, NatedevOnly):
        policy = placement.FixedExclusion(rule.reason)
    elif kind != HAND_MADE_KIND and (not isinstance(job, StepJob) or job.step not in (
            "test-affected", "test-all", "test-modules", "test-nightly", "integrate")):
        policy = placement.FixedExclusion("command")
    else:
        try:
            if build_folders.repo_word(repo_root) == "claude":
                host = mac_linux.read_settings(conf.read("build_targets")).ssh_hops[1]
                policy = placement.AllowedHost()
            else:
                policy = placement.FixedExclusion("repo")
        except build_folders.InvalidFolderForm:
            policy = placement.FixedExclusion("repo")
        except (conf.Unusable, mac_linux.GuestUnavailable):
            policy = placement.FixedExclusion("mac_linux_settings_unreadable")
    destination = placement.PlacementDestination("ssh", target.name, host, claim_state.RepositoryScope("claude"), 1)
    return placement.PlacementCandidate(destination, policy, settings)


def command_constraints(args: CliArguments) -> int:
    """Print fixed candidates without probing or selecting a host."""
    from scripts.mac_test import eligibility
    from scripts.build_queue import placement
    from scripts.lib.atomic_file import write_text_atomic
    from scripts.build_targets import target_config

    if args.target and args.target != "natedev":
        try:
            with conf.report_key_changes():
                _ = target_config.configured_ssh_target(args.target)
        except ConfigError as error:
            print(f"build targets: {error}", file=sys.stderr)
            return 2

    session = claim_state.submitting_session(args.session)
    routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
    selection = Pinned(args.target or "mac") if args.target or args.mac_only else ROUTED
    rule = step_routing_rule(cast(StepKind, args.kind), routing, selection)
    constraints = fixed_placement_constraints(Path(args.repo_root).resolve(), args.package,
        cast(StepKind, args.kind), (), session, execution_identity.SourceKeyUnavailable("source is not submitted yet"), routing, selection)
    print("any_target" if isinstance(rule, AnyTarget) else f"natedev_only {rule.reason}")
    selected_name = selection.target if isinstance(selection, Pinned) and isinstance(rule, AnyTarget) else ""
    shown: set[str] = set()
    with conf.report_key_changes():
        targets = {target.name: target for target in read_targets(
            selected=selected_name, purpose=RegistryRead.PLACEMENT)}
    for candidate in constraints.candidates:
        destination = candidate.destination
        if destination.target in shown:
            continue
        shown.add(destination.target)
        target = targets.get(destination.target)
        if target is None:
            continue
        if args.mac_only and (not isinstance(target, SshTarget) or target.platform != "macos"):
            continue
        reason = (candidate.eligibility.reason if isinstance(candidate.eligibility, placement.FixedExclusion)
                  else "pinned to another target" if isinstance(selection, Pinned) and target.name != selection.target else "")
        print_eligible_target(ExcludedTarget(target, reason) if reason else EligibleTarget(target))
    if args.remote_kinds_file:
        write_text_atomic(Path(args.remote_kinds_file), "".join(
            f"{remote_verb(cast(StepKind, step))}\t{step}\n" for step in eligibility.remote_step_kinds(args.kind)))
    return 0


def remote_verb(kind: StepKind) -> eligibility.MacVerb:

    from scripts.mac_test import eligibility

    step = eligibility.remote_step_kinds(kind)[0]
    return cast(eligibility.MacVerb, "nextest" if step == "test" else "clippy" if step in ("lint", "example") else step)


@dataclass(frozen=True)
class KindNotOffloaded:
    """The recorded step has no Mac command."""


def recorded_step_kind(step: str) -> StepKind | KindNotOffloaded:
    """Map a queue step name, such as nextest or mend, to its routing kind."""
    from scripts.mac_test import eligibility

    verbs = cast(tuple[eligibility.MacVerb, ...], get_args(eligibility.MacVerb))
    for verb in verbs:
        if eligibility.step_kind(verb) == step:
            return cast(StepKind, eligibility.verb_kind(verb))
    return KindNotOffloaded()


def step_policy(target: LocalTarget | SshTarget, repo_root: Path, package: str, kind: StepKind, *,
                backoff: eligibility.BackoffState | eligibility.ReadBackoff,
                configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
                ) -> eligibility.KnownRepository | ExcludedTarget:
    """Read the Mac policy for one step: repository, linux_only packages and backoff. Run no remote command."""
    from scripts.mac_test import eligibility

    config = (configuration if isinstance(configuration, conf.ConfigRead)
              else eligibility.read_target_settings(target.settings) if isinstance(target, SshTarget) else eligibility.read_target_settings())
    name = target.name if isinstance(target, SshTarget) else "mac"
    first, *rest = eligibility.remote_step_kinds(kind)
    repository = eligibility.mac_step_eligibility(config, repo_root, package,
        remote_verb(cast(StepKind, first)), name, kind=first, backoff=backoff)
    if isinstance(repository, eligibility.IneligibleMacStep):
        return ExcludedTarget(target, repository.reason)
    for step in rest:
        policy = eligibility.mac_step_eligibility(config, repo_root, package,
            remote_verb(cast(StepKind, step)), name, kind=step, backoff=backoff)
        if isinstance(policy, eligibility.IneligibleMacStep):
            return ExcludedTarget(target, policy.reason)
    return repository


def start_probe_recovery(target: SshTarget, repo_root: Path, package: str, kind: StepKind) -> None:
    """Start one recovery probe without delaying the eligibility answer."""

    from scripts.mac_test import eligibility

    directory = eligibility.state_directory(target.name)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "recovery-probe.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        arguments = [str(Path(__file__).parents[1] / "lib/py"), str(Path(__file__)), "recover-probe",
                     "--target", target.name, "--repo-root", str(repo_root), "--package", package,
                     "--kind", kind, "--recovery-fd", str(lock.fileno())]
        os.set_inheritable(lock.fileno(), True)
        try:
            with open(os.devnull, "r+") as output:
                pid = os.posix_spawn(arguments[0], arguments, dict(os.environ), setsid=True,
                    file_actions=tuple((os.POSIX_SPAWN_DUP2, output.fileno(), fd) for fd in (0, 1, 2)))
        finally:
            os.set_inheritable(lock.fileno(), False)
        threading.Thread(target=os.waitpid, args=(pid, 0), name="probe-recovery-reaper", daemon=True).start()


def command_recover_probe(args: CliArguments) -> int:
    """Copy scripts and probe under the normal target claim."""

    from scripts.build_targets import target_config
    from scripts.copies import copies
    from scripts.mac_test import eligibility, mac_test, offload

    with os.fdopen(args.recovery_fd):
        target = target_config.configured_ssh_target(args.target)
        if target.platform == "linux":
            print(eligibility.LINUX_SSH_OFFLOAD_REFUSED, file=sys.stderr)
            return 1
        config = ssh_target_config(target)
        if isinstance(config, ExcludedTarget):
            return 0
        mirror = claim_state.mac_repository_identity(args.repo_root)
        if isinstance(mirror, claim_state.WholeMacScope):
            return 0
        mac_repository = mirror.claim_repository()
        if not isinstance(mac_repository, claim_state.MacRepositoryIdentity):
            return 0
        repository = eligibility.KnownRepository(mac_repository.mirror_name, mac_repository.key)
        paths = mac_test.state_paths(target.name)
        if mac_test.free_now(paths, target.settings, Path(args.repo_root), mac_repository=mac_repository) != "bad":
            return 0
        request = offload.OffloadRequest(
            Path(args.repo_root), args.package, f"probe-{os.getpid()}", offload.NoResultFile(), (),
            owner="verify", holder_session=offload.holder_session(), target=target.name,
            verb=remote_verb(cast(StepKind, args.kind)), kind=args.kind)
        controller = offload.ProcessController()
        try:
            with offload.interrupt_handlers(controller):
                with ExitStack() as leases:
                    try:
                        leases.enter_context(copies.remote_lease(offload.request_mirror(config, request, repository)))
                    except copies.BusyRemoteMirror:
                        return 0
                    claim = offload.claim_for_run(controller, config, request, repository,
                        mac_repository=mac_repository)
                    if not isinstance(claim, offload.FreshMacClaim):
                        return 0
                    try:
                        saved = controller.mac_repository
                        if not isinstance(saved, claim_state.MacRepositoryIdentity):
                            return 0
                        if saved.mirror_name != repository.name:
                            return 0
                        repository = eligibility.KnownRepository(saved.mirror_name, saved.key)
                        if not offload.copy_tracked_scripts(controller, config):
                            return 0
                        selected = replace(request, slot=claim.slot)
                        copied = offload.copy_tree_with_key(controller, config, selected, repository)
                        if isinstance(copied, offload.TreeCopyFailed):
                            return 0
                        _ = offload.probe_mac(controller, config, repository, target.name, claim.slot)
                    finally:
                        _ = offload.release_claim(controller, target.name)
        except offload.OffloadInterrupted as interrupted:
            return 128 + interrupted.signal_number

    return 0


def command_probe_backoff(name: str, *, probe: bool) -> int:
    """Exit 0 when the target has no retry backoff or answers one probe; exit 1 otherwise."""
    from scripts.build_targets import target_config
    from scripts.mac_test import eligibility, offload

    if not isinstance(eligibility.read_backoff(time.time(), name), eligibility.ActiveBackoff):
        return 0
    if not probe:
        return 1
    config = ssh_target_config(target_config.configured_ssh_target(name))
    # A target without an offload probe keeps its backoff until its own route clears it.
    if isinstance(config, ExcludedTarget):
        return 1
    probe_started_ns = time.time_ns()
    if isinstance(offload.probe_reachability(offload.ProcessController(), config), offload.UnreachableHost):
        return 1
    offload.record_reachable(name, probe_started_ns=probe_started_ns)
    return int(isinstance(eligibility.read_backoff(time.time(), name), eligibility.ActiveBackoff))


@dataclass(frozen=True)
class CapacityNotRead:
    """The direct eligibility caller has not read local capacity."""


CAPACITY_NOT_READ = CapacityNotRead()


@dataclass(frozen=True)
class MacDiskRecoveryPending(ExcludedTarget):
    """The disk reading excludes the Mac while its sweep runs."""

    disk: offload.MacDiskShort


@dataclass(frozen=True)
class MacDiskSweep:
    """Start a sweep for this disk shortage."""

    disk: offload.MacDiskShort
    run: Callable[[], None]


@dataclass(frozen=True)
class MacDiskRecoveryQuery:
    """Read the recovery state without starting a sweep."""


@dataclass(frozen=True)
class MacDiskRecoveryActive:
    """The sweep keeps the Mac excluded."""

    disk: offload.MacDiskShort


@dataclass(frozen=True)
class MacDiskRecoveryInactive:
    """The host has no active sweep."""


type MacDiskRecoveryRequest = MacDiskSweep | MacDiskRecoveryQuery
type MacDiskRecoveryState = MacDiskRecoveryActive | MacDiskRecoveryInactive
type RequestMacDiskRecovery = Callable[[str, MacDiskRecoveryRequest], MacDiskRecoveryState]


def _sweep_mac_disk(controller: offload.ProcessController,
                    config: eligibility.OffloadConfig | eligibility.SshProbeSettings,
                    repository: eligibility.KnownRepository, probe: offload.ProbeSnapshot, name: str
                    ) -> offload.MacDiskReady | offload.SweepShort | offload.SweepFailed:
    from scripts.mac_test import offload

    result = offload.ensure_mac_disk_floor(controller, config, repository, probe, name)
    if isinstance(result, offload.SweepFailed):
        print(f"build targets: Mac disk exclusion: {result.cause}", file=sys.stderr)
    return result


def mac_disk_recovery_eligibility(target: LocalTarget | SshTarget,
                                  config: eligibility.OffloadConfig | eligibility.SshProbeSettings,
                                  repository: eligibility.KnownRepository, probe: offload.ProbeSnapshot,
                                  request_disk_recovery: RequestMacDiskRecovery) -> EligibleTarget | ExcludedTarget:
    """Apply the disk floor to a Mac reading and request a sweep if necessary."""
    from scripts.mac_test import offload

    reading = offload.check_mac_disk_floor(probe, offload.mac_disk_floor(config))

    def recover_disk() -> None:
        name = target.name if isinstance(target, SshTarget) else "mac"
        _ = _sweep_mac_disk(offload.ProcessController(), config, repository, probe, name)

    request = MacDiskSweep(reading, recover_disk) if isinstance(reading, offload.MacDiskShort) else MacDiskRecoveryQuery()
    recovery = request_disk_recovery(config.host, request)
    if isinstance(recovery, MacDiskRecoveryActive):
        return MacDiskRecoveryPending(target, "disk", recovery.disk)
    return EligibleTarget(target) if isinstance(request, MacDiskRecoveryQuery) else ExcludedTarget(target, "disk")


def mac_target_eligibility(target: LocalTarget | SshTarget, repo_root: Path,
                           package: str, kind: StepKind, routing: Routing, *,
                           answer: mac_test.FreeNowAnswer | CapacityNotRead = CAPACITY_NOT_READ,
                           configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
                           session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                           mac_scope: claim_state.ClaimScope | claim_state.ResolveMacRepository = claim_state.RESOLVE_MAC_REPOSITORY,
                           recover_mac_backoff: bool = False,
                           request_disk_recovery: RequestMacDiskRecovery | None = None,
                           ) -> EligibleTarget | ExcludedTarget:

    from scripts.mac_test import eligibility, mac_test

    session = claim_state.submitting_session(session)
    if isinstance(routing, LocalRequired):
        return ExcludedTarget(target, routing.reason)
    try:
        name = target.name if isinstance(target, SshTarget) else "mac"
        backoff = eligibility.NO_BACKOFF if recover_mac_backoff else eligibility.READ_BACKOFF
        repository = step_policy(target, repo_root, package, kind, backoff=backoff, configuration=configuration)
        if isinstance(repository, ExcludedTarget):
            return repository
        # An excluded step reads no probe settings.
        config = (eligibility.probe_settings(configuration) if isinstance(configuration, conf.ConfigRead)
                  else eligibility.read_config(target.settings) if isinstance(target, SshTarget) else eligibility.read_config())
        if isinstance(answer, CapacityNotRead):
            settings = config.source if isinstance(config, eligibility.OffloadConfig) else config.configuration.source
            answer = mac_test.free_now(mac_test.state_paths(name), settings, repo_root, session=session,
                                      configuration=configuration, mac_scope=mac_scope,
                                      recover_mac_backoff=recover_mac_backoff)
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, conf.Unusable) and isinstance(configuration, conf.ConfigRead):
            raise
        return ExcludedTarget(target, f"state unreadable: {error}")
    if answer in ("blocked", "backoff"):
        return ExcludedTarget(target, answer)
    if answer == "bad" and not recover_mac_backoff:
        if request_disk_recovery is not None:
            recovery = request_disk_recovery(config.host, MacDiskRecoveryQuery())
            if isinstance(recovery, MacDiskRecoveryActive):
                return MacDiskRecoveryPending(target, "disk", recovery.disk)
        return EligibleTarget(target)
    # Only an eligible step pays for the offload runner's import.
    from scripts.mac_test import offload

    try:
        controller = offload.ProcessController()
        probe = offload.probe_mac(controller, config, repository, name)
        if isinstance(probe, offload.UnresolvedSlotStartup):
            return ExcludedTarget(target, "busy")
        if isinstance(eligibility.read_backoff(time.time(), name), eligibility.ActiveBackoff):
            return ExcludedTarget(target, "backoff")
        if isinstance(probe, offload.UnavailableProbe):
            delay = (config.unreachable_backoff_s if isinstance(config, eligibility.OffloadConfig)
                     else config.configuration.number("unreachable_backoff_s"))
            offload.write_backoff(time.time() + delay, name, wait=not recover_mac_backoff)
            return ExcludedTarget(target, "unreachable")

        if request_disk_recovery is not None:
            return mac_disk_recovery_eligibility(target, config, repository, probe, request_disk_recovery)

        handlers = (offload.interrupt_handlers(controller)
                    if threading.current_thread() is threading.main_thread() else nullcontext())
        with handlers:
            disk = _sweep_mac_disk(controller, config, repository, probe, name)
        if not isinstance(disk, offload.MacDiskReady):
            return ExcludedTarget(target, "disk")
    except offload.OffloadInterrupted as interrupted:
        raise SystemExit(128 + interrupted.signal_number) from None
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, conf.Unusable) and isinstance(configuration, conf.ConfigRead):
            raise
        return ExcludedTarget(target, f"state unreadable: {error}")
    return EligibleTarget(target)


def ssh_target_eligibility(target: SshTarget, repo_root: Path, package: str,
                           kind: StepKind, routing: Routing, *, selection: TargetSelection = ROUTED, new_step: bool = False,
                           configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
                           session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                           mac_scope: claim_state.ClaimScope | claim_state.ResolveMacRepository = claim_state.RESOLVE_MAC_REPOSITORY,
                           recover_mac_backoff: bool = False,
                           request_disk_recovery: RequestMacDiskRecovery | None = None,
                           ) -> EligibleTarget | ExcludedTarget:
    """Read SSH target policy and capacity without evaluating a Linux development shell."""

    from scripts.mac_test import eligibility, mac_test

    session = claim_state.submitting_session(session)
    from scripts.aws_target import hana_placement, hana_placement_adapter
    from scripts.build_targets import target_config

    settings = conf.read(target.settings) if isinstance(configuration, conf.UseConfiguredValue) else configuration
    transport = target_config.read_execution_transport(target, configuration=settings)
    if isinstance(transport, HanaGpuNextestTransport):
        from scripts.build_queue.placement import FixedExclusion

        if isinstance(routing, LocalRequired):
            return ExcludedTarget(target, routing.reason)
        if not isinstance(selection, Pinned) or selection.target != target.name:
            return ExcludedTarget(target, 'This target requires --target hana-gpu')
        identity = eligibility.repository_identity(repo_root)
        policy = hana_placement_adapter.gpu_step(transport,
            identity.key if isinstance(identity, eligibility.KnownRepository) else '',
            package, kind, (), configuration=settings)
        return ExcludedTarget(target, policy.reason) if isinstance(policy, FixedExclusion) else EligibleTarget(target)
    if isinstance(transport, hana_placement.HanaControlTransport):
        if isinstance(routing, LocalRequired):
            return ExcludedTarget(target, routing.reason)
        policy = hana_placement.fixed_policy(transport, str(repo_root), kind)
        if isinstance(policy, hana_placement.HanaStepRefused):
            return ExcludedTarget(target, policy.reason)
        controller = hana_placement.controller_for(transport, read_only=True)
        snapshot = controller.inspect()
        if isinstance(snapshot, hana_placement.HanaUnprovisioned):
            return ExcludedTarget(target, 'unprovisioned')
        if isinstance(snapshot, hana_placement.HanaLifecycleUnavailable):
            return ExcludedTarget(target, 'state unreadable')
        if isinstance(snapshot, hana_placement.HanaResourcesHeld):
            return ExcludedTarget(target, 'busy')
        from scripts.aws_target import hana_box
        if not controller.image_is_current(snapshot.deployment):
            return ExcludedTarget(target, hana_box.HANA_IMAGE_STALE)
        if snapshot.claims:
            return ExcludedTarget(target, 'busy')
        if snapshot.state not in (hana_box.Lifecycle.READY, hana_box.Lifecycle.IDLE_STOPPED):
            return ExcludedTarget(target, 'starting' if snapshot.state == hana_box.Lifecycle.STARTING else 'recovery')
        return EligibleTarget(target)
    if target.platform != "macos":
        reason = routing.reason if isinstance(routing, LocalRequired) else eligibility.LINUX_SSH_OFFLOAD_REFUSED
        return ExcludedTarget(target, reason)
    answer = mac_test.free_now(mac_test.state_paths(target.name), target.settings, repo_root, session=session,
                             configuration=configuration, mac_scope=mac_scope,
                             recover_mac_backoff=recover_mac_backoff)
    if new_step and not recover_mac_backoff and not isinstance(selection, Pinned) and answer == "bad":
        start_probe_recovery(target, repo_root, package, kind)
    return mac_target_eligibility(target, repo_root, package, kind, routing, answer=answer,
                                  configuration=configuration, session=session, mac_scope=mac_scope,
                                  recover_mac_backoff=recover_mac_backoff,
                                  request_disk_recovery=request_disk_recovery)


def ssh_target_config(target: SshTarget) -> eligibility.OffloadConfig | ExcludedTarget:
    """Read execution settings for a registered SSH target."""
    from scripts.mac_test import eligibility
    from scripts.aws_target import hana_placement

    if target.name == eligibility.MAC_LINUX_TARGET:
        return ExcludedTarget(target, 'The Mac Linux guest requires an exact queue assignment')
    if isinstance(hana_placement.read_execution_transport(target), (hana_placement.HanaControlTransport, HanaGpuNextestTransport)):
        return ExcludedTarget(target, 'Hana requires an exact queue assignment')

    return eligibility.read_config(target.settings)


def command_pinned_eligible(args: CliArguments, routing: Routing) -> int:
    """Use normal eligibility, but wait for stopped AWS capacity before its reachability check."""

    from scripts.build_targets import target_config

    try:
        if args.target == "natedev":
            local = local_target()
            print("natedev_only pinned to natedev")
            print_eligible_target(EligibleTarget(local))
            return 0
        target = target_config.configured_ssh_target(args.target)
        kind = cast(StepKind, args.kind)
        result = ssh_target_eligibility(target, Path(args.repo_root), args.package, kind, routing,
                                        selection=Pinned(target.name), new_step=args.new_step)
        if isinstance(result, ExcludedTarget) and result.reason not in ("busy", "blocked", "starting"):
            raise ValueError(f"target {target.name} refused: {result.reason}")
        print("any_target")
        print_eligible_target(result)
        return 0
    except (ConfigError, OSError, UnicodeError, ValueError) as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2


def wait_claim_identity(paths: mac_test.StatePaths, repo_root: str, pid: int, *,
                        session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                        expected: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY,
                        mac_scope: claim_state.ClaimScope = claim_state.WHOLE_MAC,
                        cleanup_only: bool = False,
                        ) -> claim_state.ClaimIdentity | claim_state.NoClaimIdentity:
    """Find the exact reservation for this process and session."""
    from scripts.lib import process_start as process_identity
    from scripts.mac_test import mac_test

    session = claim_state.submitting_session(session)
    start = process_identity.process_start(pid)
    if not isinstance(start, process_identity.Started):
        return claim_state.NO_CLAIM_IDENTITY
    try:
        worktree = claim_state.claim_worktree_path(repo_root)
        block = mac_test.blocking_claim_block(paths, session)
        claims = [claim for claim in mac_test.read_claims(paths)
                  if claim.record["pid"] == pid and claim.record["proc_start"] == start.start
                  and "wait_released" not in claim.record
                  and claim.record["submitting_session"] == claim_state.session_id(session)
                  and ("remote_host" not in claim.record or claim.record.get("cleanup_pending"))
                  and (not cleanup_only or claim.record.get("cleanup_pending"))
                  and (not claim.record.get("cleanup_pending") or claim_state.cleanup_scope_is_known(paths, claim))
                  and (isinstance(block, mac_test.NoMacBlock) or "remote_host" in claim.record)]
        if paths.platform == "macos":
            claims = [claim for claim in claims
                      if (isinstance(expected, claim_state.NoClaimIdentity)
                          or claim_state.claim_identity(claim.record) == expected)
                      and (isinstance(mac_scope, claim_state.WholeMacScope)
                           or claim.matches_mac_assignment_scope(mac_scope))
                      and (claim_state.recorded_worktree_matches(claim.record["worktree"], worktree)
                           or (claim.record.get("cleanup_pending")
                               and (isinstance(expected, claim_state.ClaimIdentity)
                                    or not claim.record.get("repo_key"))))]
            return claim_state.claim_identity(claims[0].record) if len(claims) == 1 else claim_state.NO_CLAIM_IDENTITY
        scope = mac_test.target_claim_scope(paths, str(worktree))
        return next((claim_state.claim_identity(claim.record) for claim in claims
                     if mac_test.wait_claim_matches(paths, claim, scope, worktree)), claim_state.NO_CLAIM_IDENTITY)
    except (OSError, mac_test.StateReadError):
        return claim_state.NO_CLAIM_IDENTITY


def published_wait_identity(path: str, session: SubmittingSession) -> claim_state.ClaimIdentity | claim_state.NoClaimIdentity:
    if not path:
        return claim_state.NO_CLAIM_IDENTITY
    try:
        claim_id, episode, attempt, saved_session = Path(path).read_text().removesuffix("\n").split("\t")
    except FileNotFoundError:
        return claim_state.NO_CLAIM_IDENTITY
    if not claim_id or not episode or not attempt or saved_session != claim_state.session_id(session):
        raise ValueError("the saved reservation identity does not match the wait")
    return claim_state.ClaimIdentity(claim_id, episode, attempt)


def cleanup_refused_claims(target: SshTarget, args: CliArguments, *, session: SubmittingSession) -> None:
    """Recover retained cleanup ownership without granting permission to execute."""

    from scripts.copies import copies
    from scripts.lib import conf, process_start as process_identity
    from scripts.mac_test import eligibility, mac_test, offload

    paths = mac_test.state_paths(target.name)
    scope = mac_test.target_claim_scope(paths, args.repo_root)
    if paths.platform == "linux" and not isinstance(scope, mac_test.RepositoryScope):
        return
    start = process_identity.process_start(args.pid)
    if not isinstance(start, process_identity.Started):
        return
    with mac_test.locked(paths.lock):
        claims = tuple(claim for claim in mac_test.read_claims(paths)
                       if mac_test.cleanup_claim_matches(paths, claim, scope)
                       and "remote_host" in claim.record
                       and (mac_test.recoverable_kept_claim(claim.record)
                            or (claim.record.get("cleanup_pending") and claim.record["pid"] == args.pid
                                and claim.record["proc_start"] == start.start)))
    if not claims:
        return
    try:
        config = eligibility.read_connection_settings(target.settings)
    except conf.Unusable as error:
        print(f"build targets: {target.name}: cleanup settings unavailable: {error}", file=sys.stderr)
        return
    owner = mac_test.CliArguments()
    owner.pid = args.pid
    for claim in claims:
        record = claim.record
        if "remote_host" not in record:
            continue
        if paths.platform == "macos":
            mirror_number = 1
            saved = claim_state.saved_mac_repository(record)
            lease = offload.mac_cleanup_lease(saved, record["remote_host"], target.name)
            mirror = offload.mac_cleanup_scope(saved)
        else:
            cleanup_scope = claim.scope if isinstance(claim.scope, claim_state.RepositoryScope) else scope
            if not isinstance(cleanup_scope, claim_state.RepositoryScope):
                continue
            _, mirror_number = claim_state.legacy_linux_claim_name(claim.path)
            mirror = eligibility.KnownRepository(cleanup_scope.name, cleanup_scope.key)
            lease = copies.remote_lease(copies.RemoteMirror("", copies.SelectedRepository(mirror.name),
                record["remote_host"], offload.remote_mirror(mirror, target.name, mirror_number), ""))
        with ExitStack() as cleanup:
            try:
                cleanup.enter_context(lease)
            except copies.BusyRemoteMirror:
                continue
            except (OSError, conf.Unusable, copies.CopyError) as error:
                print(f"build targets: {target.name}: cleanup lease unavailable: {error}", file=sys.stderr)
                continue
            try:
                with mac_test.locked(paths.lock):
                    if mac_test.read_run(claim.path) != record:
                        continue
                    attempt = mac_test.take_over_kept_claim(owner, claim, record, session=session)
                    if not isinstance(attempt, mac_test.ClaimFinished) or attempt.status != 13:
                        continue
                checked = offload.check_leftovers(config, record["remote_host"], mirror, target.name,
                                                  mirror_number)
                if isinstance(checked, offload.LeftoversEnded):
                    release = mac_test.CliArguments()
                    release.pid = args.pid
                    release.claim_id = record["claim_id"]
                    release.episode = record["episode"]
                    release.attempt = record["attempt"]
                    release.settings = str(target.settings)
                    _ = mac_test.command_release(release, paths)
            except copies.BusyRemoteMirror:
                continue
            except (conf.Unusable, copies.CopyError) as error:
                print(f"build targets: {target.name}: cleanup settings unavailable: {error}", file=sys.stderr)


def publish_wait_reservation(args: CliArguments, target: SshTarget,
                             identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity,
                             session: SubmittingSession) -> None:
    """Publish the target and the exact identity for release and handoff."""
    from scripts.lib.atomic_file import write_text_atomic

    write_text_atomic(Path(args.reservation_file), f"{target.name}\t{target.settings}\n")
    if args.claim_identity_file and isinstance(identity, claim_state.ClaimIdentity):
        write_text_atomic(Path(args.claim_identity_file),
                          f"{identity.claim_id}\t{identity.episode}\t{identity.attempt}\t{claim_state.session_id(session)}\n")


def reserve_assigned(args: CliArguments) -> int:
    """Claim the target and repository in the saved assignment."""
    from scripts.build_queue import admission_protocol as wire, placement
    from scripts.mac_test import mac_test
    from scripts.build_targets import target_config

    saved = wire.parse_reply(Path(args.assignment_file).read_text())
    if not isinstance(saved, wire.Assignment):
        raise ValueError("reservation requires a remote assignment")
    assignment = saved.assignment
    target = target_config.configured_ssh_target(assignment.destination.target)
    from scripts.aws_target import hana_placement
    transport = target_config.read_execution_transport(target)
    if isinstance(transport, hana_placement.HanaControlTransport):
        from scripts.buildlog import store
        from scripts.lib import process_start
        owner = process_start.process_start(args.pid)
        if not isinstance(owner, process_start.Started):
            return 78
        holder = placement.ClaimHolderIdentity(store.host_name(), args.pid, owner.start)
        checked = hana_placement.validate_assignment(assignment, holder)
        if isinstance(checked, hana_placement.HanaClaimRefused):
            return 1
        # F1 commits lifecycle demand after this process-bound reservation handoff.
        identity = claim_state.ClaimIdentity(execution_identity.assigned_claim_id(assignment.episode, assignment.attempt),
            assignment.episode, assignment.attempt)
        publish_wait_reservation(args, target, identity, assignment.session)
        return 0
    paths = mac_test.state_paths(target.name)
    expected = claim_state.ClaimIdentity(execution_identity.assigned_claim_id(assignment.episode, assignment.attempt),
                                        assignment.episode, assignment.attempt)
    published = (published_wait_identity(args.claim_identity_file, assignment.session)
                 if paths.platform == "macos" and not args.recover_only and Path(args.reservation_file).is_file()
                 else claim_state.NO_CLAIM_IDENTITY)
    if args.recover_only or (isinstance(published, claim_state.ClaimIdentity) and published != expected):
        from scripts.lib import process_start

        try:
            with mac_test.locked(paths.lock):
                owner = process_start.process_start(args.pid)
                if isinstance(owner, process_start.Unreadable):
                    return 78
                scope = assignment.destination.scope
                matches = [held for held in mac_test.read_claims(paths)
                    if ((held.matches_mac_assignment_scope(scope) or (held.record.get("cleanup_pending")
                            and mac_test.cleanup_claim_matches(paths, held, scope)))
                        if paths.platform == "macos" else held.scope == scope)
                    and held.record["pid"] == args.pid
                    and "wait_released" not in held.record
                    and (not held.record.get("cleanup_pending") or claim_state.cleanup_scope_is_known(paths, held))
                    and owner == process_start.Started(held.record["proc_start"])
                    and held.record["submitting_session"] == claim_state.session_id(assignment.session)
                    and ((args.recover_only and claim_state.claim_identity(held.record) == expected)
                         or ("remote_host" in held.record and held.record.get("cleanup_pending")
                             and (args.recover_only or claim_state.claim_identity(held.record) == published)))]
                if not matches and args.recover_only:
                    return 1
                if len(matches) > 1:
                    return 78
                if matches:
                    held = matches[0]
                    publish_wait_reservation(args, target, claim_state.claim_identity(held.record), assignment.session)
                    return 13 if "remote_host" in held.record else 0
        except (OSError, ValueError, mac_test.StateReadError):
            return 78
    identity = wait_claim_identity(paths, args.repo_root, args.pid, session=assignment.session,
                                   expected=expected, mac_scope=assignment.destination.scope)
    if (isinstance(identity, claim_state.ClaimIdentity)
            and (identity.episode, identity.attempt) == (assignment.episode, assignment.attempt)):
        publish_wait_reservation(args, target, identity, assignment.session)
        return 0
    claim = mac_test.CliArguments()
    claim.pid, claim.target, claim.repo_root = args.pid, target.name, args.repo_root
    claim.package, claim.verb, claim.settings = args.package, remote_verb(cast(StepKind, args.kind)), str(target.settings)
    claim.identity = True
    with redirect_stdout(io.StringIO()) as output:
        status = mac_test.command_claim_wait(claim, paths, kind=args.kind,
                                             session=assignment.session, assignment=assignment)
    identity = claim_state.reply_claim_identity(output.getvalue())
    if status not in (0, 13):
        return status
    publish_wait_reservation(args, target, identity, assignment.session)
    return status


def command_reserve(args: CliArguments) -> int:
    """Reserve before a queue leave; the wait process owns the reservation."""

    from scripts.build_targets import target_config

    if args.assignment_file:
        return reserve_assigned(args)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    destination = Path(args.reservation_file)
    if destination.exists():
        name, separator, settings = destination.read_text().strip().partition("\t")
        if separator and (not args.target or name == args.target):
            target = target_config.configured_ssh_target(name)
            if settings != str(target.settings):
                destination.unlink()
                return 1
            config = ssh_target_config(target)
            if not isinstance(config, ExcludedTarget):
                if config.enabled:
                    from scripts.mac_test import mac_test

                    paths = mac_test.state_paths(name)
                    expected = (published_wait_identity(args.claim_identity_file, session) if paths.platform == "macos"
                                else claim_state.NO_CLAIM_IDENTITY)
                    identity = wait_claim_identity(paths, args.repo_root, args.pid, session=session, expected=expected)
                    if isinstance(identity, claim_state.ClaimIdentity):
                        publish_wait_reservation(args, target, identity, session)
                        return 0
        destination.unlink(missing_ok=True)
    if args.kind not in ("test", "check", "doc", "lint", "example", "build", "final"):
        return 1
    busy_mac = False
    for target in read_targets():
        if not isinstance(target, SshTarget) or (args.target and target.name != args.target):
            continue
        from scripts.mac_test import eligibility, mac_test, offload

        verb = remote_verb(args.kind)
        config = ssh_target_config(target)
        if isinstance(config, ExcludedTarget):
            if config.reason != "off":
                cleanup_refused_claims(target, args, session=session)
            continue
        if not config.enabled:
            continue
        paths = mac_test.state_paths(target.name)
        mac_in_backoff = (paths.platform == "macos"
                          and isinstance(eligibility.read_backoff(time.time(), target.name), eligibility.ActiveBackoff))
        identity = wait_claim_identity(paths, args.repo_root, args.pid, session=session, cleanup_only=mac_in_backoff)
        if paths.platform == "macos" and isinstance(identity, claim_state.ClaimIdentity):
            publish_wait_reservation(args, target, identity, session)
            return 0
        if mac_in_backoff:
            continue
        if isinstance(identity, claim_state.NoClaimIdentity):
            answer = mac_test.free_now(paths, target.settings, Path(args.repo_root), session=session)
            if answer not in ("free", "busy") and not (answer == "bad" and args.new_step):
                continue
        policy = eligibility.mac_step_eligibility(config, Path(args.repo_root), args.package, verb, target.name, kind=args.kind)
        if isinstance(policy, eligibility.IneligibleMacStep):
            continue
        if isinstance(identity, claim_state.ClaimIdentity):
            publish_wait_reservation(args, target, identity, session)
            return 0
        if args.recover_only:
            scope = mac_test.target_claim_scope(paths, args.repo_root)
            with mac_test.locked(paths.lock):
                retained = any(mac_test.cleanup_claim_matches(paths, claim, scope)
                               and mac_test.recoverable_kept_claim(claim.record)
                               for claim in mac_test.read_claims(paths))
            if not retained:
                busy_mac = busy_mac or target.platform == "macos"
                continue
        if isinstance(offload.probe_reachability(offload.ProcessController(), config), offload.UnreachableHost):
            continue
        claim = mac_test.CliArguments()
        claim.pid, claim.target, claim.repo_root = args.pid, target.name, args.repo_root
        claim.settings, claim.package, claim.verb = str(target.settings), args.package, verb
        claim.identity = True
        with redirect_stdout(io.StringIO()) as output:
            status = mac_test.command_claim_wait(claim, paths, kind=args.kind, session=session)
        identity = claim_state.reply_claim_identity(output.getvalue())
        if status not in (0, 13):
            if status == 11 and target.platform == "macos":
                busy_mac = True
            continue
        try:
            publish_wait_reservation(args, target, identity, session)
        except BaseException:
            _ = command_release_wait(args.pid, identity=identity, session=session)
            raise
        return 0
    return 11 if busy_mac else 1


def command_release_wait(pid: int, *,
                         identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY,
                         session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:
    """Release the process-bound reservation and revoke its retry permission."""
    from scripts.lib import process_start as process_identity

    if isinstance(identity, claim_state.NoClaimIdentity) or not all((identity.claim_id, identity.episode, identity.attempt)):
        return 0
    session = claim_state.submitting_session(session)
    session_id = claim_state.session_id(session)
    start = process_identity.process_start(pid)
    located = False
    unreadable: claim_state.UnlocatedWaitReservation | None = None
    for target in read_targets():
        if not isinstance(target, SshTarget):
            continue
        from scripts.build_targets import target_config

        if not target_config.ssh_target_state_directory(target.name).exists():
            continue
        paths = claim_state.state_paths(target.name)
        fenced: list[claim_state.ProcessBoundClaim] = []
        removable: list[Path] = []
        with claim_state.locked(paths.lock):
            release_start = start.start if isinstance(start, process_identity.Started) else None
            permission: claim_state.RetiredMacReservation | claim_state.NoSavedRetryPermission
            permission = claim_state.NO_SAVED_RETRY_PERMISSION
            reservation: Path | claim_state.UnrecordedReservationPath = claim_state.UnrecordedReservationPath()
            matched = False
            try:
                removal = claim_state.read_wait_removal(claim_state.released_wait_path(paths, identity))
                if isinstance(removal, claim_state.StateFileReadFailed):
                    print(removal.diagnostic, file=sys.stderr)
                    return 1
                if isinstance(removal, claim_state.WaiterProcessIdentity):
                    located = True
                    continue
                if paths.platform == "macos":
                    retired = claim_state.read_retired_mac_reservation(paths, identity)
                    if (isinstance(retired, claim_state.RetiredMacReservation)
                            and retired.pid == pid and retired.submitting_session == session_id
                            and (release_start is None or retired.proc_start == release_start)):
                        permission = retired
                        # An interrupted retirement can leave both the proof and the original reservation.
                        release_start = retired.proc_start
                        reservation = retired.reservation
                if release_start is not None or (paths.platform == "macos" and isinstance(start, process_identity.Unreadable)):
                    if isinstance(reservation, claim_state.UnrecordedReservationPath):
                        location = claim_state.read_wait_location(paths, identity)
                        if isinstance(location, claim_state.WaitReservationLocation):
                            if (location.pid != pid or location.submitting_session != session_id
                                    or (release_start is not None and location.proc_start != release_start)):
                                located = True
                                continue
                            reservation = location.path
                    if isinstance(reservation, Path):
                        located = True
                    for saved in claim_state.read_wait_reservations(paths, identity, reservation):
                        path, record = saved.path, saved.record
                        if record["pid"] != pid or record["submitting_session"] != session_id:
                            continue
                        if release_start is not None and record["proc_start"] != release_start:
                            continue
                        located = True
                        matched = True
                        if (isinstance(reservation, claim_state.UnrecordedReservationPath)
                                or isinstance(permission, claim_state.RetiredMacReservation)):
                            claim_state.save_wait_location(paths, identity, claim_state.WaitReservationLocation.for_claim(path, record))
                        # A sweep must not restore permission after release removes it.
                        if ((release_start is None or isinstance(permission, claim_state.RetiredMacReservation))
                                and "wait_released" not in record):
                            claim_state.mark_wait_released(path, saved.original_fields)
                        if release_start is None:
                            continue
                        if claim_state.claim_pending_start(paths, path, record).exists():
                            fenced.append(record)
                        else:
                            removable.append(path)
                if isinstance(permission, claim_state.RetiredMacReservation):
                    if matched:
                        permission.path.unlink(missing_ok=True)
                    else:
                        claim_state.record_wait_removal(paths, identity, permission.path)
                for path in removable:
                    claim_state.record_wait_removal(paths, identity, path)
            except claim_state.UnlocatedWaitReservation as error:
                if unreadable is None:
                    unreadable = error
            except (claim_state.StateReadError, OSError) as error:
                print(str(error), file=sys.stderr)
                return 1
        for record in fenced:
            from scripts.mac_test import mac_test

            release = mac_test.CliArguments()
            release.pid, release.target, release.settings = pid, target.name, str(target.settings)
            release.claim_id = record["claim_id"]
            release.episode, release.attempt = record["episode"], record["attempt"]
            status = mac_test.command_release(release, paths, finish_wait=True)
            if status != 0:
                return status
    if not located and unreadable is not None:
        print(str(unreadable), file=sys.stderr)
        return 1
    return 0


def command_kind_eligible(repo_root: Path, package: str, kind: StepKind, routing: Routing,
                          *, mac_only: bool = False, new_step: bool = False) -> int:
    decision = step_routing_rule(kind, routing, Pinned("mac") if mac_only else ROUTED)
    try:
        eligibility = tuple(
            ExcludedTarget(target, decision.reason) if isinstance(target, SshTarget) and isinstance(decision, NatedevOnly) and not new_step
            else ssh_target_eligibility(target, repo_root, package, kind,
                LocalRequired(decision.reason) if isinstance(decision, NatedevOnly) else routing,
                selection=Pinned(target.name) if mac_only else ROUTED, new_step=new_step)
            if isinstance(target, SshTarget) else EligibleTarget(target)
            for target in read_targets()
            if not mac_only or isinstance(target, SshTarget) and target.platform == "macos"
        )
    except (ConfigError, OSError, UnicodeError, ValueError) as error:
        print(f"natedev_only state unreadable: {display_field(str(error))}")
        return 0
    print("any_target" if isinstance(decision, AnyTarget)
          else f"natedev_only {display_field(decision.reason)}")
    for result in eligibility:
        print_eligible_target(result)
    return 0


def wait_clock() -> datetime:

    from scripts.build_hold import build_hold

    supplied = os.environ.get("BUILD_TARGETS_NOW", "")
    return build_hold.aware_instant(supplied) if supplied else datetime.now(timezone.utc)


@final
class NoActiveLocalHoldWait:
    """No wait owns a release-wait mechanism in this execution context."""

    def wait(self, _seconds: float) -> None:
        raise RuntimeError("local hold waiting requires an active wait")


class LocalHoldWait(Protocol):
    """A hold wait owns and closes its notification or polling mechanism."""

    def arm(self) -> None: ...
    def wait(self, seconds: float) -> None: ...
    def __enter__(self) -> "LocalHoldWait": ...
    def __exit__(self, *_arguments: object) -> None: ...


LOCAL_HOLD_WAIT: ContextVar[LocalHoldWait | NoActiveLocalHoldWait] = ContextVar(
    "local_hold_wait", default=NoActiveLocalHoldWait())


def wait_sleep(seconds: float) -> None:
    # Keep the existing wait boundary; its owner selects notifications or polling.
    LOCAL_HOLD_WAIT.get().wait(seconds)


def local_target() -> LocalTarget:
    for target in read_targets():
        if target.name == "natedev" and isinstance(target, LocalTarget):
            return target
    raise ConfigError("targets: natedev local target is required")


# wait-local's status when its leave hook found another target free: the call
# leaves natedev's queue and runs there. It is invoke.sh's LINT_LEFT_QUEUE_STATUS.
LEFT_QUEUE_STATUS = 75


def leave_hook_says_free(leave_hook: str) -> bool:
    """Run the caller's leave hook (mac_test.py free-now); status 0 means free."""
    import subprocess

    if not leave_hook:
        return False
    try:
        return subprocess.run(["/bin/sh", "-c", leave_hook], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False).returncode == 0
    except OSError:
        return False


VERIFY_SPEAKER = "verify.sh"


def speaker(caller: str) -> str:
    """The name the wait messages start with: verify.sh, or lint and the caller that ran it."""
    if not caller:
        return VERIFY_SPEAKER
    return "lint" if caller == "lint" else f"lint ({display_field(caller)})"


def wait_diagnostic(reason: str, caller: str = "") -> str:
    return f"{speaker(caller)}: natedev's hold state cannot be read ({display_field(reason)}); not starting."


@dataclass(frozen=True)
class NoBlockingHold:
    """No hold in force prevents this session from starting."""


@dataclass(frozen=True)
class ConfirmedCallerLineage:
    """The caller identity matches the checked process lineage."""

    processes: frozenset[build_hold.ProcessIdentity]


@dataclass(frozen=True)
class UnavailableCallerLineage:
    """The caller lineage cannot grant permission through a keep list."""

    reason: str


CallerLineage = ConfirmedCallerLineage | UnavailableCallerLineage


def read_caller_lineage(proc_root: Path, pid: int, start_time: str) -> CallerLineage:
    """Read the lineage only while the checked caller identity matches."""
    from scripts.build_hold import build_hold

    if not start_time or build_hold.process_start_time(pid, proc_root) != start_time:
        return UnavailableCallerLineage("the caller start time is unavailable or differs")
    processes = build_hold.caller_processes(proc_root, pid)
    if (build_hold.ProcessIdentity(pid, start_time) not in processes
            or build_hold.process_start_time(pid, proc_root) != start_time):
        return UnavailableCallerLineage("the caller lineage is unavailable or its identity changed")
    return ConfirmedCallerLineage(processes)


def current_caller_lineage() -> CallerLineage:
    """Read the current process lineage for a local hold decision."""
    from scripts.build_hold import build_hold

    pid = os.getpid()
    return read_caller_lineage(build_hold.PROC_ROOT, pid, build_hold.process_start_time(pid, build_hold.PROC_ROOT))


def hold_waited_on(holds: build_hold.NatedevHolds,
                   owner: OwningSession | NoOwningSession,
                   directory: Path, caller_lineage: CallerLineage) -> build_hold.Holder | NoBlockingHold:
    """Choose the oldest hold that applies to the caller and belongs to another session."""
    return blocking_hold(holds, own_hold_key(owner, directory), caller_lineage)


def own_hold_key(owner: OwningSession | NoOwningSession, directory: Path) -> str:
    """Name the hold file of the owner's session, or give an empty name."""
    from scripts.build_hold import build_hold

    if isinstance(owner, OwningSession):
        try:
            return build_hold.holder_path(directory, build_hold.holder_of(owner.session_id).key).name
        except ValueError:
            pass
    return ""


def blocking_hold(holds: build_hold.NatedevHolds, own_key: str,
                  caller_lineage: CallerLineage) -> build_hold.Holder | NoBlockingHold:
    """Choose the oldest hold that applies to the caller, other than the hold of its own session."""
    for holder in holds.in_force:
        kept = (isinstance(caller_lineage, ConfirmedCallerLineage)
                and bool(caller_lineage.processes.intersection(holder.keep)))
        if holder.key != own_key and not kept:
            return holder
    return NoBlockingHold()


def caller_waits_on_hold(holds: build_hold.NatedevHolds, directory: Path) -> bool:
    """Check whether a hold makes the calling session wait."""
    from scripts.build_hold import build_hold

    return isinstance(hold_waited_on(holds, session_owner.owning_session(), directory,
                                    current_caller_lineage()), build_hold.Holder)


def command_register_local(_call_id: str, *, pid: int,
                           session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:
    """Check the local hold without waiting or writing a call record."""
    _ = pid
    return command_check_local("verify", session=session)


def tell_wait_holder(holder: build_hold.Holder, line: str, caller: str) -> None:
    """Tell the holder about unresolved ownership without ending the wait."""
    from scripts.build_hold import build_hold

    result = build_hold.send_holder_message(holder.key, line)
    if isinstance(result, build_hold.HolderMessageFailed):
        print(f"{speaker(caller)}: could not tell {display_field(holder.name)}: {display_field(result.reason)}",
              file=sys.stderr, flush=True)


def command_wait_local(call_id: str, *, pid: int = 0, leave_hook: str = "", caller: str = "",
                       session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:
    """Wait until no hold applies. An empty caller is verify.sh; any other caller is a lint step."""

    from scripts.build_hold import build_hold
    from scripts.build_targets.targets import Held, StateUnreadable
    from scripts.lib.release_waiter import local_release_waiter

    _ = pid
    if not leave_hook and not caller:
        leave_hook = os.environ.get("BUILDLOG_MAC_IDLE_HOOK", "")
    name = speaker(caller)
    # verify.sh runs a test on the Mac when it can; a lint step only waits.
    waiting = "; waiting (tests that can run on the Mac keep running there)." if not caller else "; waiting for the release."
    started = wait_clock()
    announced = False
    escalated = False
    polled = False
    waiter_file = os.environ.get("BUILDLOG_HOLD_WAITER_FILE", "")
    previous_handlers: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
    notifications = ExitStack()

    def interrupted(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        if waiter_file:
            waiter: ActiveWaitProcess = {"call_id": call_id, "pid": os.getpid(),
                                         "proc_start": build_hold.process_start_time(os.getpid())}
            temporary = Path(waiter_file).with_suffix(".tmp")
            _ = temporary.write_text(json.dumps(waiter))
            os.replace(temporary, waiter_file)
        target = local_target()
        release_events = notifications.enter_context(local_release_waiter(
            build_hold.holder_directory(), build_hold.release_directory()))
        context = LOCAL_HOLD_WAIT.set(release_events)
        _ = notifications.callback(LOCAL_HOLD_WAIT.reset, context)
        interval = max(0.001, float(os.environ.get("BUILD_TARGETS_POLL_S", "15")))
        escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
        while True:
            release_events.arm()
            state = target_state(target)
            if isinstance(state, StateUnreadable):
                print(wait_diagnostic(state.reason, caller), file=sys.stderr, flush=True)
                return 12
            directory = build_hold.holder_directory()
            holds = build_hold.natedev_holds(directory)
            if not holds.in_force:
                return 0
            session = claim_state.submitting_session(session)
            blocking = hold_waited_on(holds, session, directory, current_caller_lineage())
            if isinstance(blocking, NoBlockingHold):
                return 0
            if not isinstance(state, Held):
                print(wait_diagnostic("holder file exists; target state disagrees", caller), file=sys.stderr, flush=True)
                return 12
            if not announced:
                announced = True
                print(f"{name}: natedev is held by {display_field(blocking.name)} for "
                      + f"{display_field(blocking.purpose)}{waiting}",
                      file=sys.stderr, flush=True)
                resolution = session_owner.run_resolution()
                if isinstance(resolution, session_owner.RunNotInStaff):
                    line = (f"{name}: build has no run pointer ({display_field(str(resolution.run))}); "
                            + f"waits on {display_field(blocking.name)} hold for {display_field(blocking.purpose)}.")
                elif isinstance(session, NoOwningSession):
                    line = (f"{name}: build has no session; waits on {display_field(blocking.name)} hold "
                            + f"for {display_field(blocking.purpose)}.")
                else:
                    line = ""
                if line:
                    print(line, file=sys.stderr, flush=True)
                    tell_wait_holder(blocking, line, caller)
            # A held natedev cannot start this step, so a free Mac wins and the
            # call can leave without an allocation. As in memory_gate.sh, the
            # hook runs from the second poll on: a Mac that declines after
            # free-now said free costs one poll interval, never a tight loop.
            if polled and leave_hook_says_free(leave_hook):
                print(f"{name}: the Mac is free: leaving natedev's hold wait to run there.",
                      file=sys.stderr, flush=True)
                return LEFT_QUEUE_STATUS
            if not escalated and (wait_clock() - blocking.since).total_seconds() >= escalation:
                print(f"{name}: natedev has been held for 2 h by {display_field(blocking.name)}; ask them.",
                      file=sys.stderr, flush=True)
                escalated = True
            wait_sleep(interval)
            polled = True
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error), caller), file=sys.stderr, flush=True)
        return 12
    finally:
        notifications.close()
        for signum, handler in previous_handlers.items():
            _ = signal.signal(signum, handler)
        output = os.environ.get("BUILDLOG_HOLD_WAIT_FILE", "")
        if output:
            with Path(output).open("a") as elapsed:
                seconds = max(0.0, (wait_clock() - started).total_seconds()) if announced else 0.0
                _ = elapsed.write(f"{seconds:.6f}\n")
        if waiter_file:
            Path(waiter_file).unlink(missing_ok=True)


def command_interrupt_local(call_id: str, signal_name: str) -> int:

    from scripts.build_hold import build_hold

    waiter_file = os.environ.get("BUILDLOG_HOLD_WAITER_FILE", "")
    if not waiter_file:
        return 0
    path = Path(waiter_file)
    try:
        record = cast(object, json.loads(path.read_text()))
        if not isinstance(record, dict):
            return 12
        waiter = cast(dict[str, object], record)
        pid = waiter.get("pid")
        identity = waiter.get("proc_start")
        if (waiter.get("call_id") != call_id or not isinstance(pid, int) or pid <= 0
            or not isinstance(identity, str) or not identity
            or build_hold.process_start_time(pid) != identity):
            return 0
        os.kill(pid, signal.SIGINT if signal_name == "INT" else signal.SIGTERM)
        deadline = time.monotonic() + 2
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return 0
    except (FileNotFoundError, ProcessLookupError):
        return 0
    except (OSError, ValueError):
        return 12


def command_check_local(caller: str = "", *,
                        session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:

    from scripts.build_hold import build_hold
    from scripts.build_targets.targets import Held, StateUnreadable

    try:
        state = target_state(local_target())
        output = sys.stderr if caller else sys.stdout
        if isinstance(state, StateUnreadable):
            print(wait_diagnostic(state.reason, "" if caller == "verify" else caller), file=output)
            return 12
        directory = build_hold.holder_directory()
        holds = build_hold.natedev_holds(directory)
        if not holds.in_force:
            return 0
        owner = claim_state.submitting_session(session)
        blocking = hold_waited_on(holds, owner, directory, current_caller_lineage())
        if isinstance(blocking, NoBlockingHold):
            return 0
        if not isinstance(state, Held):
            print(wait_diagnostic("holder file exists; target state disagrees", caller), file=output)
            return 12
        if caller:
            label = speaker("" if caller == "verify" else caller)
            print(f"{label}: natedev is held by {display_field(blocking.name)} for {display_field(blocking.purpose)}.", file=output)
            escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
            if (wait_clock() - blocking.since).total_seconds() >= escalation:
                print(f"{label}: natedev has been held for 2 h by {display_field(blocking.name)}; ask them.", file=output)
        else:
            if isinstance(owner, OwningSession):
                _ = build_hold.register_wait(owner.session_id)
            print(f"natedev is held by {display_field(blocking.name)} for {display_field(blocking.purpose)}; launch after the release.")
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error), "" if caller == "verify" else caller), file=sys.stderr if caller else sys.stdout)
        return 12


def command_eligibility(args: CliArguments) -> int:
    """Apply eligibility flags without loading them for other commands."""

    from scripts.lib.atomic_file import write_text_atomic
    if args.probe_marks_file:
        from scripts.mac_test import eligibility

        repository = eligibility.repository_identity(Path(args.repo_root))
        marks: list[str] = []
        for target in read_targets():
            if isinstance(target, SshTarget):
                marks.append(str(eligibility.state_directory(target.name) / "failed_probe"))
                if isinstance(repository, eligibility.KnownRepository):
                    marks.append(str(eligibility.linux_version_decline_path(target.name, repository.name)))
        write_text_atomic(Path(args.probe_marks_file), "".join(path + "\n" for path in marks))
    if args.remote_kinds_file:
        from scripts.mac_test import eligibility

        write_text_atomic(Path(args.remote_kinds_file), "".join(
            f"{remote_verb(cast(StepKind, step))}\t{step}\n"
            for step in eligibility.remote_step_kinds(args.kind)))
    routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
    if args.target:
        return command_pinned_eligible(args, routing)
    if args.kind:
        return command_kind_eligible(Path(args.repo_root).resolve(), args.package, cast(StepKind, args.kind), routing, mac_only=args.mac_only, new_step=args.new_step)
    return command_eligible(Path(args.repo_root).resolve(), args.package, routing)


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "build_targets/build_targets.py")
    try:
        from scripts.build_targets import target_config

        args = parser().parse_args(namespace=CliArguments())
        if args.command == "setup-mac-linux":
            root = target_config.build_targets_state_root()
            try:
                target_config.provision_mac_linux_state_root(root)
            except OSError as error:
                print(f"setup-mac-linux: Cannot prepare {root}: {error}", file=sys.stderr)
                return 1
            return 0
        if args.command == "recover-probe":
            return command_recover_probe(args)
        if args.command == "probe-backoff":
            return command_probe_backoff(args.target, probe=not args.no_probe)
        if args.command == "source-key":
            from scripts.buildlog import treekey

            print(treekey.tree_key(args.repo_root) or "")
            return 0
        if args.command == "submitting-session":
            print(claim_state.session_id(claim_state.submitting_session(args.session)))
            return 0
        if args.command == "stop-hana-gpu":
            from importlib import import_module
            from scripts.build_queue import admission_wire as wire

            saved = wire.read_assignment(Path(args.assignment_file))
            if not isinstance(saved, wire.Assignment) or saved.assignment.destination.target != "hana-gpu":
                raise ValueError("A Hana GPU assignment is required to stop its service")
            stop_service = cast(Callable[[str], int], getattr(
                import_module("scripts.aws_target.gpu_size_trial"), "stop_assigned_gpu_service"))
            return stop_service(execution_identity.assigned_claim_id(saved.assignment.episode, saved.assignment.attempt))
        if args.command == "run-hana-gpu":
            from scripts.aws_target import hana_placement
            from scripts.build_queue import admission_wire as wire

            saved = wire.read_assignment(Path(args.assignment_file))
            if not isinstance(saved, wire.Assignment) or args.argv[:1] != ["--"]:
                raise ValueError("The Hana GPU runner requires a remote assignment and a command")
            return hana_placement.run_gpu_assignment(saved.assignment, Path(args.repo_root), args.package,
                                      tuple(args.argv[1:]), Path(args.result_file))
        if args.command == "assigned-info":
            from scripts.build_queue import admission_protocol as wire

            saved = wire.parse_reply(Path(args.assignment_file).read_text())
            if not isinstance(saved, (wire.Assignment, wire.LocalPlacementGranted)):
                raise wire.AdmissionUnavailable("file contains no assigned attempt")
            destination = saved.assignment.destination
            if destination.provider == "local":
                print("-\tlinux\t" + claim_state.session_id(saved.assignment.session))
            else:
                target = target_config.configured_ssh_target(destination.target)
                print(f"{target.settings}\t{target.platform}\t{claim_state.session_id(saved.assignment.session)}")
            return 0
        if args.command in ("reserve", "recover-reservation"):
            if args.command == "recover-reservation":
                args.recover_only = True
            return command_reserve(args)
        if args.command in ("release-wait", "eligible", "constraints"):
            from scripts.build_targets import target_config

            with target_config.command_configuration():
                if args.command == "release-wait":
                    return command_release_wait(args.pid, identity=claim_state.ClaimIdentity(
                        args.claim_id, args.episode, args.attempt), session=args.session)
                return command_constraints(args) if args.command == "constraints" else command_eligibility(args)
        if args.command == "register-local":
            return command_register_local(args.call_id, pid=args.pid, session=args.session)
        if args.command == "wait-local":
            return command_wait_local(args.call_id, pid=args.pid, leave_hook=args.leave_hook, caller=args.caller, session=args.session)
        if args.command == "check-local":
            return command_check_local(args.caller, session=args.session)
        if args.command == "interrupt-local":
            return command_interrupt_local(args.call_id, args.signal_name)
        return command_status(args.probe)
    except override_guard.SharedStateWriteRefused as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
