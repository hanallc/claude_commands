"""Prepare the Build limits section from recorded queue and target state."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from scripts.aws_target import config as aws_config, ledger, spend
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement, queue_view, snapshot_codec, target_availability
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.slot_wait import public_queue_text
from scripts.build_targets import target_config, targets
from scripts.buildlog import charts, repository, store
from scripts.lib import conf, git_files
from scripts.lint.memory_admit import gpu_test_reservation, repository_run_owner, reserved_repository_runs

Machine = Literal["natedev", "aws"]
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
    waits: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    budgets: tuple[Metric, ...] = ()
    note: str = ""


@dataclass
class RepositoryRow:
    name: str
    natedev: Cell = field(default_factory=Cell)
    aws: Cell = field(default_factory=Cell)
    unplaced: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


@dataclass(frozen=True)
class BuildLimits:
    natedev: MachineLimits
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
    policy_names: dict[str, set[str]] = field(default_factory=dict[str, set[str]])


@dataclass(frozen=True)
class Background:
    history: RecentRepositories | charts.IndexUnavailable
    aws_money: Metric
    hana_money: Metric
    origins: Origins = field(default_factory=Origins)
    history_stale: bool = False


def recent_steps(connection: sqlite3.Connection, now: float,
                 aws_hosts: frozenset[str] = NO_AWS_HOSTS, *, origins: Origins | None = None) -> RecentRepositories:
    """Identify repositories from origins or readable source checkouts."""
    select = """SELECT DISTINCT repo_identity, repo_path, target, host, repo FROM steps
                WHERE started_at >= ? AND started_at <= ?"""
    rows = cast(Iterable[tuple[object, object, object, object, object]],
                connection.execute(select, (store.utc_iso(now - HISTORY_SECONDS), store.utc_iso(now))))
    machines: dict[str, set[str]] = {}
    checkouts: dict[str, Path] = {}
    checkout_origins: dict[str, set[str]] = {}
    policy_names: dict[str, set[str]] = {}
    origins = origins if origins is not None else Origins()
    for identity, repo_path, target, host, repo_name in rows:
        key = identity if isinstance(identity, str) else ""
        folder = key if key.startswith("/") else repo_path if isinstance(repo_path, str) else ""
        origin = origins.resolve(key, folder, read_checkout=True)
        if not origin:
            continue
        if Path(folder).is_absolute():
            if origins.key(key):
                checkout_origins.setdefault(folder, set()).add(origin)
            if (Path(folder) / ".git").exists():
                _ = origins.resolve("", folder, read_checkout=True)
                _ = checkouts.setdefault(origin, Path(folder))
        machine = target if isinstance(target, str) and target else "aws" if host in aws_hosts else str(host)
        machines.setdefault(origin, set()).add(machine)
        if isinstance(repo_name, str) and repo_name:
            policy_names.setdefault(origin, set()).add(repo_name)
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
    canonical_names: dict[str, set[str]] = {}
    for origin, used in machines.items():
        canonical_machines.setdefault(origins.canonical(origin), set()).update(used)
    for origin, candidate in checkouts.items():
        _ = canonical_checkouts.setdefault(origins.canonical(origin), candidate)
    for origin, names in policy_names.items():
        canonical_names.setdefault(origins.canonical(origin), set()).update(names)
    return RecentRepositories(canonical_machines, canonical_checkouts, origins, canonical_names)


def read_facts(directory: Path, observed_at: float) -> records.LimitsFacts | None:
    """Use only facts for the queue pass that the page displays."""
    try:
        raw = cast(object, json.loads((directory / records.LIMITS_SNAPSHOT).read_text()))
        if not isinstance(raw, dict):
            return None
        envelope = cast(dict[str, object], raw)
        facts = cast(records.LimitsFacts, snapshot_codec.parse_snapshot_value(envelope["limits_facts"], records.LimitsFacts))
    except (OSError, KeyError, ValueError, TypeError, RecursionError, OverflowError):
        return None
    return facts if facts.observed_at == observed_at else None


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
                      Metric("Money today", hana_used, hana_limit, "USD", "estimate"),
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


def local_limits(reading: records.QueueRead, memory: Metric, facts: records.LimitsFacts | None) -> tuple[Metric, ...]:
    explanation = reading.explanation
    inputs = explanation.decided_from
    gpu = explanation.policy.gpu_tests
    metrics = [memory]
    if gpu.repositories:
        used = sum(gpu_test_reservation(row, gpu) for row in inputs.reservations)
        metrics.append(Metric("GPU tests", used, gpu.slots, qualifier=", ".join(sorted(gpu.repositories))))
    if facts is not None and facts.disk_floor is not None:
        metrics.append(Metric("Free disk", facts.disk_free, facts.disk_floor, "GiB", "minimum"))
    return tuple(metrics)


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
            key = (step.pid, step.since)
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


def machine_for_target(target: str, local: str, aws: frozenset[str]) -> Machine | None:
    if target == local:
        return "natedev"
    if target == "aws" or target in aws:
        return "aws"
    return None


def placement_reason(reason: str) -> str:
    match reason:
        case "busy":
            return "Busy"
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


def queue_wait(wait: records.LimitsWait, cause: records.WaitReason, local_target: str,
               facts: records.LimitsFacts) -> tuple[Machine | None, str] | None:
    step = wait.step
    aws_targets = facts.aws_targets
    if isinstance(step.route, target_config.MacOnly) or (
            isinstance(step.route, target_config.PinnedTarget) and step.route.target in facts.mac_targets):
        return None
    block = next((block for block in wait.blocks if block.provider != "mac" and block.target not in facts.mac_targets), None)
    if block is not None:
        machine = ("natedev" if block.provider == "local" else
                   machine_for_target(block.target, local_target, aws_targets))
        label = "Limit full" if block.reason == "busy" else placement_reason(block.reason)
        return machine, f"{block.target} · {label}" if machine is None and block.target else label
    if wait.blocks:
        return None
    if wait.pending:
        route = step.route
        machine = machine_for_target(route.target, local_target, aws_targets) if isinstance(route, target_config.PinnedTarget) else None
        if isinstance(route, target_config.PinnedTarget) and machine is None:
            return None, f"{route.target} · Evidence pending"
        return machine, "Evidence pending"
    if isinstance(cause, records.ReadyForAdmission):
        return "natedev", "Ready for admission"
    if isinstance(cause, records.StartSpacingWait):
        return "natedev", "Waiting for start spacing"
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
    if isinstance(route, target_config.PinnedTarget) and route.target != local_target:
        machine = machine_for_target(route.target, local_target, aws_targets)
        return machine, "Placement pending" if machine else f"{route.target} · Placement pending"
    if isinstance(cause, records.BehindHead):
        if cause.head_name == "remote startup":
            return "natedev", "Waiting for remote startup"
        return "natedev", records.behind_queue_text(public_queue_text(cause.head_name) or "the first step")
    if isinstance(cause, (records.NeedsMemory, records.MemoryPressure)):
        return "natedev", "Memory"
    if isinstance(cause, records.TokenWaitingWaiter):
        return "natedev", "Cargo token"
    return "natedev", "Waiting in queue"


def build_limits(reading: records.QueueRead, snapshots: target_availability.Snapshots, names: QueueNames,
                 memory: Metric, facts: records.LimitsFacts | None, saved: Background, zone: tzinfo) -> BuildLimits:
    explanation = reading.explanation
    inputs = explanation.decided_from
    if facts is None:
        disk = inputs.build_space
        cloud_slots = reading.cloud_slots if isinstance(reading, wire.CloudQueueRead) else None
        facts = records.LimitsFacts(explanation.now, tuple(
            records.LimitsWait(step, position, records.RepositoryIdentity(row.get("repo_identity", ""),
                row.get("repository_name", ""))) for position, (step, (_, row)) in
            enumerate(zip(reading.view.waiting, reading.ordered), 1)), (), cloud_slots,
            disk.floor_gib if isinstance(disk, records.BuildSpaceLow) else None,
            disk.free_gib if isinstance(disk, records.BuildSpaceLow) else None, frozenset(), frozenset())
    policy = explanation.policy.repository_runs
    found = snapshots if isinstance(snapshots, tuple) else ()
    local = next((snap for snap in found if isinstance(snap.target, target_config.LocalTarget)), None)
    history = saved.history.machines if isinstance(saved.history, RecentRepositories) else {}
    rows = {origin: RepositoryRow(origin_name(origin)) for origin in history}
    shared: dict[str, dict[str, list[str]]] = {machine: {} for machine in ("natedev", "aws")}
    unplaced: dict[str, list[str]] = {}
    origins = saved.origins
    cloud_repositories: set[str] = set()
    local_runs: set[str] = set()
    local_repositories: dict[str, str] = {}
    local_occupancy = reserved_repository_runs(inputs, policy)

    def row(origin: str) -> RepositoryRow:
        if origin not in rows:
            rows[origin] = RepositoryRow(origin_name(origin))
        return rows[origin]

    for reservation in sorted(inputs.reservations, key=lambda item: item["step"] not in records.REPOSITORY_RUN_STEPS):
        owner = repository_run_owner(reservation)
        if owner in local_runs:
            continue
        local_runs.add(owner)
        waiter = reservation.get("queue_waiter", {})
        name = waiter.get("repository_name", reservation["repo"])
        project = origins.resolve(waiter.get("repo_identity", reservation["repo"]), reservation["worktree"])
        if not project:
            continue
        cell = row(project).natedev
        repository_name = local_repositories.setdefault(project, name)
        cell.limit = policy.by_machine_and_repository.get(f"{inputs.local_machine}.{repository_name}")
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

    cloud_limit = facts.cloud_slots
    cloud_used = 0
    cloud_facts = {wire.Owner(cloud.pid, cloud.start_time): cloud for cloud in facts.cloud}
    wait_names = {entry.position: visible.name for visible in names.waits
                  if isinstance(entry := visible.source, queue_view.WaitingBuild)}
    cloud_wait_names = {entry.owner: visible.name for visible in names.waits
                        if isinstance(entry := visible.source, wire.CloudRow)}
    if isinstance(reading, wire.CloudQueueRead):
        for cloud in reading.cloud:
            recorded = cloud_facts.get(cloud.owner)
            project = origins.resolve(recorded.origin, cloud.identity.folder) if recorded is not None else ""
            if project:
                cloud_repositories.add(project)
            label = names.cloud.get(cloud.owner) or cloud.identity.unit or cloud.identity.step
            if isinstance(cloud.wait, wire.CloudSlotWait):
                cloud_used += isinstance(cloud.wait.state, ReservedCloudSlot)
                if cloud.owner in cloud_wait_names:
                    reason = recorded.reason if recorded is not None and recorded.reason else "Evidence pending"
                    shared["aws"].setdefault(reason, []).append(cloud_wait_names[cloud.owner])
            else:
                cloud_used += 1
                if project:
                    row(project).aws.runs.append(label)

    seen: set[tuple[int, float]] = set()
    for wait in facts.waits:
        step = wait.step
        if (step.pid, step.since) in seen:
            continue
        if wait.position and wait.position not in wait_names:
            continue
        cause = wait_cause(step)
        assigned = queue_wait(wait, cause, inputs.local_machine, facts)
        if assigned is None:
            continue
        seen.add((step.pid, step.since))
        machine, reason = assigned
        key = wait.repository.key
        project = (origins.resolve(key) if isinstance(cause, records.RunLimitWait)
                   else origins.resolve(key, step.folder))
        if project and wait.repository.name and machine == "natedev":
            _ = local_repositories.setdefault(project, wait.repository.name)
        if isinstance(cause, records.RunLimitWait) and reason == "Run limit full" and not project:
            reason = f"{cause.repository} run limit full"
        elif (reason == "Machine held" or machine == "natedev" and reason in (
                "Memory", "Disk floor", "GPU limit full", "Cargo token", "Waiting for remote startup", "Waiting for start spacing")):
            project = ""
        if machine is None:
            waits = row(project).unplaced if project else unplaced
        elif project:
            current = row(project)
            waits = current.natedev.waits if machine == "natedev" else current.aws.waits
        else:
            waits = shared[machine]
        label = wait_names.get(wait.position) or step.unit or names.sessions.get(step.session_id) or step.step
        waits.setdefault(reason, []).append(label)

    for hana in facts.hana:
        evidence = hana.evidence
        if origin := origins.resolve(evidence.repository):
            cell = row(origin).aws
            cell.limit, cell.budgets = evidence.limit, (saved.hana_money,)
            cell.used = len(evidence.claim_ids)
            cell.runs.extend(hana.runs)
            if cell.used > len(hana.runs):
                cell.note = "Claim held for startup or cleanup"

    historical_names = saved.history.policy_names if isinstance(saved.history, RecentRepositories) else {}
    for origin, current in rows.items():
        matches = {name for name in historical_names.get(origin, set())
                   if f"{inputs.local_machine}.{name}" in policy.by_machine_and_repository}
        if len(matches) == 1:
            _ = local_repositories.setdefault(origin, next(iter(matches)))
        else:
            _ = local_repositories.setdefault(origin, current.name)
    shared_local_limits = Counter(local_repositories.get(origin, current.name) for origin, current in rows.items())
    for origin, current in rows.items():
        name = local_repositories.get(origin, current.name)
        current.natedev.limit = policy.by_machine_and_repository.get(f"{inputs.local_machine}.{name}")
        if current.natedev.limit is not None:
            current.natedev.used = len(local_occupancy.get(name, ()))
            if shared_local_limits[name] > 1:
                current.natedev.note = f"Shared {name} run limit"
        else:
            current.natedev.used = len(current.natedev.runs)
        cell = current.aws
        if cell.limit is None:
            has_cloud = origin in cloud_repositories
            cell.available = True if cell.waits or cell.runs or has_cloud else None
            cell.used = len(cell.runs) if cell.runs or has_cloud else None
            if has_cloud:
                cell.note = "Uses shared cloud gate slots"
    aws_state = ("In use" if cloud_used or any(current.aws.used for current in rows.values()) else
                 "Free" if any(current.aws.limit is not None for current in rows.values()) else "State unavailable")
    shown_names = Counter(current.name for current in rows.values())
    for origin, current in rows.items():
        if shown_names[current.name] > 1:
            current.name = origin.removeprefix("https://")
    ordered = tuple(sorted(rows.values(), key=lambda item: item.name.casefold()))
    return BuildLimits(MachineLimits(target_state(local, zone), local_limits(reading, memory, facts), shared["natedev"]),
                       MachineLimits(aws_state, (Metric("Cloud gates", cloud_used if cloud_limit is not None else None, cloud_limit), saved.aws_money),
                                     shared["aws"]),
                       ordered, isinstance(saved.history, charts.IndexUnavailable), unplaced, saved.history_stale)
