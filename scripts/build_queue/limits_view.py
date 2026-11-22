"""Prepare the Build limits section from recorded queue and target state."""
from __future__ import annotations

import ast
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from scripts.aws_target import cloud_gate, config as aws_config, hana_box, hana_placement, ledger, spend
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement, queue_view, target_availability
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.slot_wait import public_queue_text
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport, HanaGpuNextestTransport
from scripts.buildlog import charts, repository, store
from scripts.lib import conf, git_files
from scripts.lib.build_slots import slot_cargo_root
from scripts.lint.memory_admit import gpu_test_reservation, repository_run_owner
from scripts.mac_test import claim_state, mac_test
from scripts.production import gate

Machine = Literal["natedev", "mac", "aws"]
HISTORY_SECONDS = 30 * 24 * 60 * 60
NO_AWS_HOSTS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Metric:
    label: str
    used: float | None
    limit: float | None
    unit: Literal["", "GiB", "USD"] = ""
    qualifier: str = ""


@dataclass(frozen=True)
class MachineLimits:
    state: str
    shared: tuple[Metric, ...] = ()
    waits: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


@dataclass
class Cell:
    available: bool | None = True
    limit: int | None = None
    used: int | None = 0
    runs: list[str] = field(default_factory=list[str])
    exempt: list[str] = field(default_factory=list[str])
    other: list[str] = field(default_factory=list[str])
    other_note: str = ""
    waits: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    budgets: tuple[Metric, ...] = ()
    note: str = ""


@dataclass
class RepositoryRow:
    name: str
    natedev: Cell = field(default_factory=Cell)
    mac: Cell = field(default_factory=Cell)
    aws: Cell = field(default_factory=Cell)
    unplaced: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def cell(self, machine: Machine) -> Cell:
        match machine:
            case "natedev":
                return self.natedev
            case "mac":
                return self.mac
            case "aws":
                return self.aws


@dataclass(frozen=True)
class BuildLimits:
    natedev: MachineLimits
    mac: MachineLimits
    aws: MachineLimits
    repositories: tuple[RepositoryRow, ...]
    history_unavailable: bool
    unplaced: dict[str, list[str]]
    history_stale: bool = False


def recorded_origin(identity: str) -> str:
    if not identity.startswith("https://"):
        return ""
    try:
        key = repository.remote_identity(identity, "")
    except ValueError:
        return ""
    return key if key.startswith("https://") and key.count("/") > 2 else ""


def origin_name(origin: str) -> str:
    return origin.rsplit("/", 1)[-1]


def origin_host_and_name(origin: str) -> tuple[str, str]:
    return origin.split("/")[2], origin_name(origin)


@dataclass
class Origins:
    """Cache checkout origins for one background interval."""

    folders: dict[str, str] = field(default_factory=dict[str, str])
    common: dict[Path, str] = field(default_factory=dict[Path, str])
    aliases: dict[str, str] = field(default_factory=dict[str, str])
    identities: dict[str, str] = field(default_factory=dict[str, str])
    active_folders: set[str] = field(default_factory=set[str])

    def key(self, identity: str) -> str:
        if identity not in self.identities:
            self.identities[identity] = recorded_origin(identity)
        return self.identities[identity]

    def canonical(self, origin: str) -> str:
        seen: set[str] = set()
        while origin in self.aliases and origin not in seen:
            seen.add(origin)
            origin = self.aliases[origin]
        return min(seen) if origin in seen else origin

    def resolve(self, identity: str, folder: str = "", *, read_checkout: bool = False) -> str:
        if key := self.key(identity):
            return self.canonical(key)
        if identity.startswith("/"):
            folder = identity
        if not Path(folder).is_absolute():
            return ""
        if read_checkout:
            if folder not in self.folders:
                self.folders[folder] = self.read(folder)
        else:
            self.active_folders.add(folder)
        return self.canonical(self.folders.get(folder, ""))

    def read(self, folder: str) -> str:
        try:
            checkout = git_files.locate(Path(folder))
        except (git_files.GitFilesError, OSError):
            return ""
        if checkout.common_dir not in self.common:
            self.common[checkout.common_dir] = ""
            remote = repository.output(["git", "config", "--includes", "--file", str(checkout.common_dir / "config"),
                                        "--get", "remote.origin.url"], str(checkout.toplevel), repository.GIT_TIMEOUT_S)
            if remote and remote.strip():
                try:
                    key = repository.remote_identity(remote.strip(), str(checkout.toplevel))
                    self.common[checkout.common_dir] = self.read(key) if key.startswith("/") else recorded_origin(key)
                except ValueError:
                    pass
        return self.common[checkout.common_dir]


@dataclass(frozen=True)
class RecentRepositories:
    machines: dict[str, set[str]]
    checkouts: dict[str, Path]
    origins: Origins = field(default_factory=Origins)


@dataclass(frozen=True)
class Background:
    history: RecentRepositories | charts.IndexUnavailable
    aws_money: Metric
    hana_money: Metric
    cloud_routes: dict[str, bool] = field(default_factory=dict[str, bool])
    origins: Origins = field(default_factory=Origins)
    history_stale: bool = False


def recent_steps(connection: sqlite3.Connection, now: float,
                 aws_hosts: frozenset[str] = NO_AWS_HOSTS, *, origins: Origins | None = None) -> RecentRepositories:
    """Identify repositories from origins or readable source checkouts."""
    select = """SELECT DISTINCT repo_identity, repo_path, target, host FROM steps
                WHERE started_at >= ? AND started_at <= ?"""
    rows = cast(Iterable[tuple[object, object, object, object]],
                connection.execute(select, (store.utc_iso(now - HISTORY_SECONDS), store.utc_iso(now))))
    machines: dict[str, set[str]] = {}
    checkouts: dict[str, Path] = {}
    checkout_origins: dict[str, set[str]] = {}
    origins = origins if origins is not None else Origins()
    for identity, repo_path, target, host in rows:
        key = identity if isinstance(identity, str) else ""
        folder = key if key.startswith("/") else repo_path if isinstance(repo_path, str) else ""
        origin = origins.resolve(key, folder, read_checkout=True)
        if not origin:
            continue
        if Path(folder).is_absolute():
            if origins.key(key):
                checkout_origins.setdefault(folder, set()).add(origin)
            if origin not in checkouts and (Path(folder) / ".git").exists():
                checkouts[origin] = Path(folder)
        machine = target if isinstance(target, str) and target else "aws" if host in aws_hosts else str(host)
        machines.setdefault(origin, set()).add(machine)
    # Prefer a checkout that has recorded both the old URL and the current URL.
    for folder, recorded in checkout_origins.items():
        if len(recorded) < 2:
            continue
        current = origins.resolve("", folder, read_checkout=True)
        if current in recorded:
            for origin in recorded - {current}:
                if origin_host_and_name(current) == origin_host_and_name(origin):
                    origins.aliases[origin] = current
    for origin, candidate in checkouts.items():
        current = origins.resolve("", str(candidate), read_checkout=True)
        if current and current != origin and origin_host_and_name(current) == origin_host_and_name(origin):
            _ = origins.aliases.setdefault(origin, current)
    canonical_machines: dict[str, set[str]] = {}
    canonical_checkouts: dict[str, Path] = {}
    for origin, used in machines.items():
        canonical_machines.setdefault(origins.canonical(origin), set()).update(used)
    for origin, candidate in checkouts.items():
        _ = canonical_checkouts.setdefault(origins.canonical(origin), candidate)
    return RecentRepositories(canonical_machines, canonical_checkouts, origins)


def aws_gate_routes(history: RecentRepositories, deployed: bool) -> dict[str, bool]:
    """Check the wire mode in the recorded checkout with bounded Git reads."""
    if not deployed:
        return {}
    routes: dict[str, bool] = {}
    mode = gate.SuiteMode.GATE.wire
    for origin, checkout in history.checkouts.items():
        for name in ("gate_routes.py", "gate.py", "merge_gate.py"):
            path = f"scripts/production/{name}"
            if not (checkout / path).is_file():
                continue
            source = repository.output(["git", "show", f"HEAD:{path}"], str(checkout), repository.GIT_TIMEOUT_S)
            if source is None:
                continue
            try:
                suite = next((node for node in ast.parse(source).body
                              if isinstance(node, ast.ClassDef) and node.name == "SuiteMode"), None)
            except SyntaxError:
                break
            if suite is not None:
                routes[origin] = any(
                    isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant)
                    and node.value.value == mode
                    or isinstance(node, ast.FunctionDef) and node.name == "_missing_"
                    and any(isinstance(value, ast.Constant) and value.value == mode for value in ast.walk(node))
                    for node in suite.body)
                break
    return routes


def background(now: float, active_folders: Sequence[str] = ()) -> Background:
    """Refresh the index and the local cost ledger at the report interval."""
    aws_hosts: frozenset[str] = NO_AWS_HOSTS
    try:
        registry = conf.read("build_targets")
        aliases = registry.value("sync.hosts.aws", str, default="", allow_blank=True)
        aws_hosts = frozenset(alias.strip() for alias in aliases.split(",") if alias.strip())
    except conf.Unusable:
        pass
    origins = Origins()
    try:
        history = charts.read_index(lambda connection: recent_steps(connection, now, aws_hosts, origins=origins))
    except (OSError, conf.Unusable) as error:
        history = charts.IndexUnavailable(str(error))
    for folder in active_folders:
        _ = origins.resolve("", folder, read_checkout=True)
    directory = target_config.ssh_target_state_directory("aws")
    routes = aws_gate_routes(history, (directory / "gate-deployment.json").is_file()
                             ) if isinstance(history, RecentRepositories) else {}
    instant = int(now)
    today = datetime.fromtimestamp(instant, UTC).date().isoformat()
    account = conf.read("aws_target")
    aws_limit = None
    hana_limit = None
    aws_used = None
    hana_used = None
    qualifier = ""
    try:
        aws_limit = float(account.value("daily_limit_usd", aws_config.money, recover_invalid=False))
        shown = spend.read_spend(account.source, directory, instant, today)
        if isinstance(shown, spend.Spend):
            day = next((item for item in shown.days if item.day == today), None)
            if day is not None:
                aws_used = float(day.usd)
                qualifier = "bill" if day.source == "AWS bill" else "estimate"
    except (ValueError, OSError, KeyError, ArithmeticError):
        pass
    try:
        hana = conf.read("hana_aws")
        hana_limit = float(hana.value("daily_budget_usd", conf.decimal, recover_invalid=False))
        settings = aws_config.read_settings(account)
        state = ledger.CostLedger(directory).read_snapshot()
        amounts, _ = ledger.spending_periods(ledger.hana_intervals(state, instant, settings.box_minimum_seconds), ZoneInfo("UTC"))
        hana_used = float(amounts[today]) if today in amounts else None
    except (ValueError, OSError, KeyError, ArithmeticError):
        pass
    return Background(history, Metric("Money today", aws_used, aws_limit, "USD", qualifier),
                      Metric("Money today", hana_used, hana_limit, "USD", "estimate"), routes,
                      origins)


def target_state(snapshot: targets.TargetSnapshot | None, zone: tzinfo) -> str:
    if snapshot is None:
        return "State unavailable"
    states = snapshot.states
    hold = next((state for state in states if isinstance(state, targets.Held)), None)
    if hold is not None:
        if isinstance(hold.end, (targets.ExpiresAt, targets.ReleaseEstimated)):
            return f"Held until {targets.clock(hold.end.when, zone)}"
        return "Blocked" if isinstance(snapshot.target, target_config.SshTarget) else "Held"
    if any(isinstance(state, targets.Unreachable) for state in states):
        return "Unreachable"
    if any(isinstance(state, targets.StateUnreadable) for state in states):
        return "State unavailable"
    return "In use" if any(isinstance(state, targets.Running) for state in states) else "Free"


def local_limits(reading: records.QueueRead, memory: Metric, folders: conf.ConfigRead) -> tuple[Metric, ...]:
    explanation = reading.explanation
    inputs = explanation.decided_from
    gpu = explanation.policy.gpu_tests
    metrics = [memory]
    if gpu.repositories:
        used = sum(gpu_test_reservation(row, gpu) for row in inputs.reservations)
        metrics.append(Metric("GPU tests", used, gpu.slots, qualifier=", ".join(sorted(gpu.repositories))))
    try:
        settings = conf.read("build_admission")
        floor = settings.value("free_floor_gib", conf.positive_whole, default=0)
    except conf.Unusable:
        floor = 0
    if floor:
        free = None
        if isinstance(inputs.build_space, records.BuildSpaceLow):
            free = inputs.build_space.free_gib
        elif isinstance(inputs.build_space, records.NoBuildSpaceHold):
            try:
                disk = os.statvfs(slot_cargo_root(folders))
                free = disk.f_bavail * disk.f_frsize // records.GIB
            except (OSError, conf.Unusable):
                pass
        metrics.append(Metric("Free disk", free, floor, "GiB", "minimum"))
    return tuple(metrics)


@dataclass(frozen=True)
class MacPolicy:
    repositories: frozenset[str]
    overrides: frozenset[str]
    settings: conf.ConfigRead


def mac_policy(snapshot: targets.TargetSnapshot | None) -> MacPolicy | None:
    if snapshot is None or not isinstance(snapshot.target, target_config.SshTarget):
        return None
    settings = conf.read(snapshot.target.settings)
    try:
        values = settings.values()
        allowed = frozenset(key.removeprefix("linux_only.") for key in values if key.startswith("linux_only."))
        overrides = frozenset(key.removeprefix("mac_budget_gib.") for key in values
                              if key.startswith("mac_budget_gib."))
        return MacPolicy(allowed if settings.value("offload", conf.on_off) else frozenset(), overrides, settings)
    except conf.Unusable:
        return None


def mac_budget(policy: MacPolicy, names: set[str]) -> Metric:
    settings = policy.settings
    try:
        configured = settings.values(*(f"mac_budget_gib.{name}" for name in sorted(names)))
        override = next(iter(configured), "")
        maximum = settings.number(override or "mac_budget_gib")
    except conf.Unusable:
        maximum = None
    return Metric("Build folder", None, maximum, "GiB", "maximum")


@dataclass(frozen=True)
class HanaRoute:
    repository: str
    host: str
    maximum: int
    lifecycle: hana_box.HanaLifecycleEvidence | hana_box.HanaUnprovisioned | None


def hana_route(snapshots: Sequence[targets.TargetSnapshot]) -> HanaRoute | None:
    for snapshot in snapshots:
        target = snapshot.target
        if not isinstance(target, target_config.SshTarget) or target.platform != "linux":
            continue
        try:
            transport = target_config.read_execution_transport(target)
            if not isinstance(transport, HanaControlTransport):
                continue
            route = hana_placement.read_settings(conf.read(transport.settings))
            if not isinstance(route, hana_placement.EnabledHanaRoute):
                continue
        except (OSError, ValueError):
            continue
        lifecycle = None
        try:
            lifecycle = hana_placement.controller_for(transport, read_only=True).read_saved_lifecycle()
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return HanaRoute(route.repository_key, transport.host, route.max_steps, lifecycle)
    return None


@dataclass(frozen=True)
class VisibleWait:
    source: queue_view.WaitingBuild | wire.CloudRow
    name: str


def visible_waits(model: queue_view.QueueModel, local_names: Mapping[int, str],
                  cloud_names: Mapping[wire.Owner, str]) -> tuple[VisibleWait, ...]:
    """Select the named queue rows once in display order."""
    waits: dict[tuple[int, float] | str | wire.Owner, VisibleWait] = {}
    for entry in queue_view.waiting_builds(model):
        if isinstance(entry, queue_view.WaitingBuild):
            step = entry.step
            key = step.episode or (step.pid, step.since)
            name = local_names.get(entry.position)
        elif isinstance(entry, wire.CloudRow):
            key = entry.owner
            name = cloud_names.get(entry.owner)
        else:
            continue
        if name is not None:
            _ = waits.setdefault(key, VisibleWait(entry, name))
    return tuple(waits.values())


@dataclass(frozen=True)
class QueueNames:
    waits: tuple[VisibleWait, ...]
    cloud: Mapping[wire.Owner, str]
    sessions: Mapping[str, str]


def wait_cause(step: records.WaitingStep) -> records.WaitReason:
    return step.reason.cause if isinstance(step.reason, records.HeadAdmissionStatus) else step.reason


def machine_for_target(target: str, local: str, mac: str, aws: frozenset[str]) -> Machine | None:
    if target == local:
        return "natedev"
    if target == mac:
        return "mac"
    if target == "aws" or target in aws:
        return "aws"
    return None


def aws_placement_targets(snapshots: Sequence[targets.TargetSnapshot]) -> frozenset[str]:
    names: set[str] = set()
    for snapshot in snapshots:
        target = snapshot.target
        if not isinstance(target, target_config.SshTarget) or target.platform != "linux":
            continue
        try:
            transport = target_config.read_execution_transport(target)
        except (OSError, ValueError):
            continue
        if isinstance(transport, (HanaControlTransport, HanaGpuNextestTransport)):
            names.add(target.name)
    return frozenset(names)


def placement_reason(reason: str) -> str:
    match reason:
        case "busy":
            return "Limit full"
        case "blocked" | "recovery":
            return "Machine held"
        case "disk":
            return "Disk floor"
        case "memory":
            return "Memory"
        case "hana_daily_budget_usd" | "daily_limit_usd" | "monthly_limit_usd":
            return "Money limit full"
        case "starting":
            return "Starting"
        case "evidence":
            return "Placement evidence pending"
        case "pending":
            return "Placement pending"
        case "retry deadline has not passed":
            return "Waiting to retry"
        case _:
            return public_queue_text(reason) or "Placement pending"


def queue_wait(step: records.WaitingStep, cause: records.WaitReason, local_target: str,
               mac_target: str, aws_targets: frozenset[str]) -> tuple[Machine | None, str]:
    if (block := step.placement_block) is not None:
        machine: Machine | None = ("mac" if block.provider == "mac" else
                                   "natedev" if block.provider == "local" else
                                   machine_for_target(block.target, local_target, mac_target, aws_targets))
        return machine, placement_reason(block.reason)
    if step.placement_pending or isinstance(cause, records.ReadyForAdmission):
        route = step.route
        machine = ("mac" if isinstance(route, target_config.MacOnly) else
                   machine_for_target(route.target, local_target, mac_target, aws_targets)
                   if isinstance(route, target_config.PinnedTarget) else None)
        return machine, "Evidence pending"
    if isinstance(cause, (records.EstimateExpired, records.PressureUnavailable, records.ReadingUnavailable)):
        return "natedev", "Evidence pending"
    if isinstance(cause, records.NatedevHeld):
        return "natedev", "Machine held"
    if isinstance(cause, records.BuildSpaceHold):
        return "natedev", "Disk floor"
    if isinstance(cause, records.GpuSlotWait):
        return "natedev", "GPU limit full"
    if isinstance(cause, records.RunLimitWait):
        return "natedev", "Run limit full"
    if isinstance(cause, placement.CargoFolderWait):
        return "natedev", "Build folder in use"
    route = step.route
    if isinstance(route, target_config.MacOnly):
        return "mac", "Placement pending"
    if isinstance(route, target_config.PinnedTarget) and route.target != local_target:
        return machine_for_target(route.target, local_target, mac_target, aws_targets), "Placement pending"
    if isinstance(cause, records.BehindHead):
        if cause.head_name == "remote startup":
            return None, "Waiting for remote startup"
        return "natedev", records.behind_queue_text(public_queue_text(cause.head_name) or "the first step")
    if isinstance(cause, (records.NeedsMemory, records.MemoryPressure)):
        return "natedev", "Memory"
    if isinstance(cause, records.TokenWaitingWaiter):
        return "natedev", "Cargo token"
    return "natedev", "Waiting in queue"


def with_mac_claim_waits(reading: records.QueueRead, snapshots: target_availability.Snapshots) -> records.QueueRead:
    """Show claim waits once, including calls that did not ask for placement."""
    waiting = list(reading.view.waiting)
    found = snapshots if isinstance(snapshots, tuple) else ()
    matched = {step.episode: index for index, step in enumerate(waiting) if step.episode}
    callers = {(step.pid, step.caller_start, step.folder): index for index, step in enumerate(waiting)
               if not step.episode and step.caller_start}
    for snapshot in found:
        target = snapshot.target
        if not isinstance(target, target_config.SshTarget) or target.platform != "macos":
            continue
        directory = target_config.ssh_target_state_directory(target.name)
        for wait in claim_state.read_claim_waits(claim_state.StatePaths(directory, target.name)):
            block = records.PlacementBlock("mac", target.name, wait.reason, wait.episode)
            caller = (wait.caller, wait.caller_start, wait.folder)
            index = matched.get(wait.episode) if wait.episode else callers.get(caller)
            if index is not None:
                waiting[index] = replace(waiting[index], placement_block=block, placement_pending=False)
                continue
            if wait.episode:
                matched[wait.episode] = len(waiting)
            else:
                callers[caller] = len(waiting)
            waiting.append(records.WaitingStep(wait.caller, wait.session, wait.name, wait.name,
                wait.folder, wait.since, 0, records.ReadyForAdmission(), placement_block=block,
                placement_episode=wait.episode, caller_start=wait.caller_start))
    return replace(reading, view=replace(reading.view, waiting=tuple(waiting)))


def claim_name(record: claim_state.RunRecord, names: QueueNames) -> str:
    return names.sessions.get(record.get("holder_session", ""), "") or record["what"]


def build_limits(reading: records.QueueRead, snapshots: target_availability.Snapshots, names: QueueNames,
                 memory: Metric, folders: conf.ConfigRead, saved: Background, zone: tzinfo,
                 mac_runs: Sequence[tuple[queue_view.MacBuild, str]], mac_disk_free: float | None = None) -> BuildLimits:
    explanation = reading.explanation
    inputs = explanation.decided_from
    policy = explanation.policy.repository_runs
    found = snapshots if isinstance(snapshots, tuple) else ()
    mac = next((snap for snap in found if snap.target.platform == "macos"), None)
    local = next((snap for snap in found if isinstance(snap.target, target_config.LocalTarget)), None)
    mac_name = mac.target.name if mac is not None else "mac"
    aws_targets = aws_placement_targets(found)
    mac_settings = mac_policy(mac)
    hana = hana_route(found)
    history = saved.history.machines if isinstance(saved.history, RecentRepositories) else {}
    rows = {origin: RepositoryRow(origin_name(origin)) for origin in history}
    shared: dict[str, dict[str, list[str]]] = {machine: {} for machine in ("natedev", "mac", "aws")}
    unplaced: dict[str, list[str]] = {}
    origins = saved.origins
    mac_claims = mac.claims if mac is not None else ()
    mac_claims_unavailable = mac is None or any(isinstance(state, targets.StateUnreadable)
                                                for state in mac.states)
    whole_mac = False
    claimed: set[str] = set()
    repositories_with_claims: set[str] = set()
    cloud_repositories: set[str] = set()
    local_runs: set[str] = set()

    def row(origin: str) -> RepositoryRow:
        if origin not in rows:
            rows[origin] = RepositoryRow(origin_name(origin) or "Repository not recorded")
        return rows[origin]

    def known_origin(name: str) -> str:
        if name == ".claude":
            name = "claude_commands"
        matches = [origin for origin, current in rows.items() if current.name == name]
        return matches[0] if len(matches) == 1 else ""

    def record_wait(origin: str, machine: Machine | None, reason: str, name: str) -> None:
        if machine is None:
            waits = row(origin).unplaced if origin else unplaced
        elif origin:
            waits = row(origin).cell(machine).waits
        else:
            waits = shared[machine]
        waits.setdefault(reason, []).append(name)

    for reservation in sorted(inputs.reservations, key=lambda item: item["step"] not in records.REPOSITORY_RUN_STEPS):
        owner = repository_run_owner(reservation)
        if owner in local_runs:
            continue
        local_runs.add(owner)
        waiter = reservation.get("queue_waiter", {})
        name = waiter.get("repository_name") or reservation["repo"]
        project = origins.resolve(waiter.get("repo_identity", reservation["repo"]), reservation["worktree"])
        if not project:
            continue
        cell = row(project).natedev
        cell.limit = policy.by_machine_and_repository.get(f"{inputs.local_machine}.{origin_name(project)}")
        session = waiter.get("session_id", "")
        unit = waiter.get("unit") or names.sessions.get(session, "")
        label = unit or public_queue_text(waiter.get("session_name", "")) or reservation["step"]
        limited = policy.for_step(inputs.local_machine, name, reservation["step"])
        if limited and policy.exempt_unit and (unit == policy.exempt_unit
                                                or records.run_owner(reservation["pid"], reservation["start_time"]) in inputs.run_limit_exempt_owners):
            cell.exempt.append(label)
        elif cell.limit is not None and not limited:
            cell.other.append(label)
        else:
            cell.runs.append(label)

    mac_placements: dict[str, list[tuple[queue_view.MacBuild, str]]] = {}
    for build, label in mac_runs:
        if build.gate_label is queue_view.MacGateLabel.PLACEMENT and build.gate_placement:
            mac_placements.setdefault(build.gate_placement, []).append((build, label))

    for claim in mac_claims:
        record = claim.record
        kept = mac_test.run_is_kept(record)
        if not kept and not mac_test.run_is_live(record):
            continue
        key = f'{record["episode"]}/{record["attempt"]}'
        claimed.add(key)
        matching = mac_placements.get(key, ())
        if isinstance(claim.scope, claim_state.WholeMacScope):
            whole_mac = True
            for build, label in matching:
                project = origins.resolve("", build.folder) or known_origin(build.repository)
                row(project).mac.runs.append(label)
                repositories_with_claims.add(project)
            continue
        project = origins.resolve(claim.scope.key, record["worktree"])
        if not project and matching:
            project = known_origin(claim.scope.name)
        if not project and not matching:
            continue
        cell = row(project).mac
        cell.used = (cell.used or 0) + 1
        repositories_with_claims.add(project)
        if kept:
            cell.runs.extend(label for _, label in matching)
            cell.note = "Claim held for cleanup"
        else:
            cell.runs.append(claim_name(record, names))
    for build, label in mac_runs:
        matched = build.gate_label is queue_view.MacGateLabel.PLACEMENT and build.gate_placement in claimed
        if not matched and (origin := known_origin(build.repository)):
            cell = row(origin).mac
            cell.other.append(label)
            cell.other_note = "Claim status unavailable" if mac_claims_unavailable else "No matching claim"

    switch = cloud_gate.launch_switch(conf.read("cloud_gate"))
    aws_state = ("On" if switch is cloud_gate.CloudGateSwitch.ON or hana is not None else
                 "Off" if switch is cloud_gate.CloudGateSwitch.OFF else "State unavailable")
    cloud_limit = None
    cloud_used = 0
    try:
        cloud_limit = conf.read("build_admission").whole("cloud_gate_slots")
    except conf.Unusable:
        pass
    if isinstance(reading, wire.CloudQueueRead):
        cloud_limit = reading.cloud_slots or cloud_limit
        for cloud in reading.cloud:
            project = origins.resolve(cloud.repo_identity, cloud.identity.folder)
            if project:
                cloud_repositories.add(project)
            label = names.cloud.get(cloud.owner) or cloud.identity.unit or cloud.identity.step
            if isinstance(cloud.wait, wire.CloudSlotWait):
                cloud_used += isinstance(cloud.wait.state, ReservedCloudSlot)
            else:
                cloud_used += 1
                if project:
                    row(project).aws.runs.append(label)

    order = reading.ordered
    for visible in names.waits:
        entry = visible.source
        if isinstance(entry, wire.CloudRow):
            if isinstance(entry.wait, wire.CloudSlotWait):
                reason = ("Waiting for launch" if isinstance(entry.wait.state, ReservedCloudSlot) else
                          "Gate limit full" if entry.wait.used >= entry.wait.slots else "Waiting for cloud slots")
                record_wait("", "aws", reason, visible.name)
            continue
        step = entry.step
        item = order[entry.position - 1][1] if entry.position <= len(order) else None
        key = item.get("repo_identity", "") if item is not None and item.get("folder") == step.folder else ""
        project = origins.resolve(key, step.folder)
        cause = wait_cause(step)
        machine, reason = queue_wait(step, cause, inputs.local_machine, mac_name, aws_targets)
        if isinstance(cause, records.RunLimitWait) and reason == "Run limit full":
            if cause.repository != origin_name(project):
                project = known_origin(cause.repository)
            if not project:
                reason = f"{cause.repository} run limit full"
        elif (reason == "Machine held"
              or machine == "natedev" and reason in ("Memory", "Disk floor", "GPU limit full", "Cargo token")
              or machine == "mac" and (reason == "Disk floor" or reason == "Limit full" and whole_mac)):
            project = ""
        record_wait(project, machine, reason, visible.name)

    if hana is not None and (origin := origins.resolve(hana.repository)) in rows:
        cell = row(origin).aws
        cell.limit, cell.budgets = hana.maximum, (saved.hana_money,)
        cell.used = None if hana.lifecycle is None else 0
        if isinstance(hana.lifecycle, hana_box.HanaLifecycleEvidence):
            cell.used = len(hana.lifecycle.claims)
            running = [step for step in reading.view.running if step.assignment_host == hana.host]
            labels = [step.unit or names.sessions.get(step.session_id) or step.step for step in running]
            active = sum(claim.active for claim in hana.lifecycle.claims)
            cell.runs.extend(labels[index] if index < len(labels) else "Hana step" for index in range(active))
            pending = cell.used - active
            if pending:
                cell.note = "Claim held for startup or cleanup"

    for origin, current in rows.items():
        name = current.name
        current.natedev.limit = policy.by_machine_and_repository.get(f"{inputs.local_machine}.{name}")
        current.natedev.used = len(current.natedev.runs) + len(current.natedev.exempt)
        mac_names = {name, ".claude"} if name == "claude_commands" else {name}
        mac_allowed = (bool(mac_names & mac_settings.repositories) if mac_settings is not None else None)
        cell = current.mac
        if origin in repositories_with_claims or cell.other or cell.waits:
            mac_allowed = True
        cell.available = mac_allowed if not cell.waits else True
        if mac_settings is not None and (mac_allowed or mac_names & mac_settings.overrides):
            cell.budgets = (mac_budget(mac_settings, mac_names),)
        if mac_allowed:
            cell.limit = 1
            if whole_mac:
                cell.used = max(1, cell.used or 0)
                cell.note = "Whole Mac claimed"
            elif mac_claims_unavailable:
                cell.used = None
        elif cell.waits:
            cell.used = None
        cell = current.aws
        has_cloud = origin in cloud_repositories or saved.cloud_routes.get(origin) is True
        if cell.limit is None:
            cell.available = bool(cell.waits or cell.runs or has_cloud and switch is not cloud_gate.CloudGateSwitch.OFF)
            cell.used = len(cell.runs)
            if has_cloud:
                cell.note = "Uses shared cloud gate slots"
    shown_names = Counter(current.name for current in rows.values())
    for origin, current in rows.items():
        if shown_names[current.name] > 1:
            current.name = origin.removeprefix("https://")
    ordered = tuple(sorted(rows.values(), key=lambda item: item.name.casefold()))
    mac_shared: tuple[Metric, ...] = ()
    if mac is not None and isinstance(mac.target, target_config.SshTarget):
        try:
            floor = conf.read(mac.target.settings).number("free_floor_gib")
            mac_shared = (Metric("Free disk", mac_disk_free, floor, "GiB", "minimum"),)
        except conf.Unusable:
            pass
    return BuildLimits(MachineLimits(target_state(local, zone), local_limits(reading, memory, folders), shared["natedev"]),
                       MachineLimits(target_state(mac, zone), mac_shared, shared["mac"]),
                       MachineLimits(aws_state, (Metric("Cloud gates", cloud_used, cloud_limit), saved.aws_money),
                                     shared["aws"]),
                       ordered, isinstance(saved.history, charts.IndexUnavailable), unplaced, saved.history_stale)
