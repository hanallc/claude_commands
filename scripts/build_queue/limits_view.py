"""Read the published records for the Build limits section."""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal, cast

from scripts.aws_target import config as aws_config, hana_box, hana_placement, spend
from scripts.build_queue import admission_protocol as wire, admission_records as records, execution_identity
from scripts.build_queue import placement, queue_view, target_availability
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.slot_wait import public_queue_text
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport, HanaGpuNextestTransport
from scripts.buildlog import charts, repository, store
from scripts.lib import conf
from scripts.lint.memory_admit import gpu_test_reservation, repository_run_owner, reserved_repository_runs
from scripts.mac_test import claim_state

WORDS = {
    "title": "Build limits",
    "table": "Build limits by repository and machine",
    "repository": "Repository",
    "local": "natedev",
    "aws": "AWS",
    "unknown_repository": "Repository not recorded",
    "unknown": "Unavailable",
    "unknown_state": "State unavailable",
    "unknown_route": "Route unknown",
    "unknown_limit": "Run limit unknown",
    "unknown_reason": "Reason unavailable",
    "unknown_name": "Name unavailable",
    "unknown_remote_waits": "Other AWS waits do not appear in the queue reading.",
    "unknown_machine": "Machine unknown",
    "use_unknown": "Use unavailable · limit {limit}",
    "count": "{used} of {limit}",
    "no_limit": "No separate run limit",
    "unlimited_runs": "{used} running · no separate run limit",
    "other_runs": "{used} other running",
    "exempt_runs": "Exempt merge runs",
    "above_limit": "Above the limit",
    "policy_key": "{name} (run limit)",
    "shared_policy": "Shared {name} run limit",
    "uses_cloud": "Uses shared cloud gate places",
    "occupied": "Place occupied",
    "stale_history": "Build history is stale.",
    "missing_history": "Build history unavailable.",
    "memory": "Memory for builds",
    "gpu": "GPU tests",
    "disk": "Free disk",
    "cloud": "Cloud gate places",
    "money": "Money today",
    "minimum_value": "{used} · {limit} minimum",
    "bill": "bill",
    "estimate": "estimate",
    "free": "Free",
    "in_use": "In use",
    "held": "Held",
    "held_until": "Held until {time}",
    "unreachable": "Unreachable",
    "waiting": "{count} waiting",
    "legend_run": "A run that is in progress",
    "legend_free": "A free place",
    "legend_line": "The limit",
    "legend_merge": "An exempt merge run",
    "legend_wait": "A run that waits",
    "token": "The run waits for permission to continue its Cargo work.",
    "expired": "The queue needs a new memory estimate before this run can start.",
    "unreadable": "The queue cannot check whether this run can start.",
    "hold": "This run waits for the hold on natedev to end.",
    "pressure": "This run waits while other work on the machine needs memory.",
    "pressure_unknown": "The queue cannot check whether other work on the machine needs memory.",
    "spacing": "This run waits briefly because another step just started.",
    "disk_unknown": "The queue cannot check how much space is free on the build disk.",
    "disk_low": "The build disk has less free space than the queue requires.",
    "cargo_folder": "This run waits for another build to release the same build folder.",
    "needs_memory": "This run needs more free memory to start.",
    "gpu_full": "All GPU test places are in use.",
    "run_full": "The run limit for {repository} on {machine} is full.",
    "order": "This run waits behind an earlier step in the queue.",
    "remote_start": "This run waits while a step starts on another machine.",
    "ready": "The queue can start this run at its next check.",
    "cloud_wait": "This run waits for a place on AWS.",
    "cloud_reserved": "This run has a place on AWS and waits for its machine to start.",
}

HISTORY_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class Metric:
    label: str
    used: float | None
    limit: float | None
    unit: Literal["", "GiB", "USD"] = ""
    qualifier: str = ""
    waits: Mapping[str, list[str]] = field(default_factory=dict[str, list[str]])


@dataclass
class Cell:
    limit: int | None = None
    used: int | None = None
    no_limit: bool = False
    runs: list[str] = field(default_factory=list[str])
    exempt: list[str] = field(default_factory=list[str])
    other: list[str] = field(default_factory=list[str])
    waits: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    note: str = ""


@dataclass
class RepositoryRow:
    name: str
    natedev: Cell = field(default_factory=Cell)
    aws: Cell = field(default_factory=Cell)


@dataclass(frozen=True)
class MachineLimits:
    state: str
    shared: tuple[Metric, ...]
    waits: dict[str, list[str]]


@dataclass(frozen=True)
class BuildLimits:
    natedev: MachineLimits
    aws: MachineLimits
    repositories: tuple[RepositoryRow, ...]
    history_unavailable: bool
    history_stale: bool
    unplaced: dict[str, list[str]]
    words: Mapping[str, str] = field(default_factory=lambda: WORDS)


def recorded_origin(identity: str) -> str:
    if not identity.startswith("https://"):
        return ""
    try:
        return repository.remote_identity(identity, "")
    except ValueError:
        return ""


@dataclass
class Origins:
    folders: dict[str, str] = field(default_factory=dict[str, str])
    pending: set[str] = field(default_factory=set[str])

    def resolve(self, identity: str, folder: str, *, read_checkout: bool = False) -> str:
        if origin := recorded_origin(identity):
            return origin
        if identity.startswith("/"):
            folder = identity
        if not Path(folder).is_absolute():
            return ""
        if folder not in self.folders:
            if not read_checkout:
                self.pending.add(folder)
                return ""
            value = repository.repository_key(folder) if Path(folder).is_dir() else ""
            self.folders[folder] = recorded_origin(value)
        return self.folders[folder]


def recent_steps(connection: sqlite3.Connection, now: float, origins: Origins) -> dict[str, str]:
    """Keep recent repository names when the index has no usable origin."""
    rows = cast(Iterable[tuple[object, object, object]], connection.execute(
        """SELECT DISTINCT repo_identity, repo_path, repo FROM steps WHERE started_at >= ? AND started_at <= ?""",
        (store.utc_iso(now - HISTORY_SECONDS), store.utc_iso(now))))
    history: dict[str, str] = {}
    for identity, path, name in rows:
        key = identity if isinstance(identity, str) else ""
        label = name if isinstance(name, str) and name else WORDS["unknown_repository"]
        origin = origins.resolve(key, path if isinstance(path, str) else "", read_checkout=True)
        if origin:
            history[origin] = origin.rsplit("/", 1)[-1]
        else:
            local_key = key if key and not key.startswith("/") and key != store.UNKNOWN_IDENTITY else label
            history["history:" + local_key] = label
    return history


@dataclass(frozen=True)
class HanaCapacity:
    repository: str
    host: str
    limit: int
    used: int | None


@dataclass(frozen=True)
class Background:
    history: dict[str, str] | charts.IndexUnavailable
    aws_money: Metric
    cloud_slots: int | None = None
    disk_floor: int | None = None
    hana: tuple[HanaCapacity, ...] = ()
    aws_targets: frozenset[str] = field(default_factory=frozenset[str])
    origins: Origins = field(default_factory=Origins)
    history_stale: bool = False


def hana_use(target: str, transport: HanaControlTransport, policy: hana_placement.EnabledHanaRoute) -> int | None:
    """Count claims when the lifecycle and target records identify the same work."""
    try:
        controller = hana_placement.controller_for(transport, read_only=True)
        lifecycle = controller.read_saved_lifecycle()
        claims = claim_state.read_claims(claim_state.state_paths(target))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(lifecycle, hana_box.HanaLifecycleEvidence):
        return None
    recorded = {claim.execution for claim in lifecycle.claims}
    if not recorded and not claims:
        return 0
    if not isinstance(lifecycle.boot, hana_box.VerifiedBoot):
        return None
    generation = hana_placement.generation_text(hana_placement.HanaHostGeneration(
        lifecycle.deployment.deployment, lifecycle.boot.start_token, lifecycle.boot.boot_id))
    matched: set[str] = set()
    for claim in claims:
        if (not isinstance(claim.scope, claim_state.RepositoryScope) or claim.scope.key != policy.repository_key
                or claim.record.get("remote_host") != transport.host
                or claim.record.get("instance_generation") != generation):
            return None
        matched.add(claim.record["claim_id"])
    return len(matched) if matched == recorded else None


def aws_routes() -> tuple[tuple[HanaCapacity, ...], frozenset[str]]:
    try:
        settings = conf.read("build_targets")
        targets_read = target_config.read_targets(configuration=settings, purpose=target_config.RegistryRead.PLACEMENT)
        pinned = settings.value("pinned_targets", str, default="", allow_blank=True)
    except (OSError, ValueError):
        return (), frozenset()
    configured = {target.name: target for target in targets_read}
    for name in filter(None, (value.strip() for value in pinned.split(","))):
        if name not in configured:
            try:
                configured.update((target.name, target) for target in target_config.read_targets(
                    selected=name, configuration=settings, purpose=target_config.RegistryRead.PLACEMENT))
            except (OSError, ValueError):
                continue
    hana: list[HanaCapacity] = []
    aws: set[str] = set()
    for target in configured.values():
        if not isinstance(target, target_config.SshTarget) or target.platform != "linux":
            continue
        try:
            transport = target_config.read_execution_transport(target)
            if isinstance(transport, (HanaControlTransport, HanaGpuNextestTransport)):
                aws.add(target.name)
            if isinstance(transport, HanaControlTransport):
                route = hana_placement.read_settings(conf.read(transport.settings))
                if isinstance(route, hana_placement.EnabledHanaRoute):
                    hana.append(HanaCapacity(route.repository_key, transport.host, route.max_steps,
                                            hana_use(target.name, transport, route)))
        except (OSError, ValueError, KeyError):
            continue
    return tuple(hana), frozenset(aws)


def spending(now: float) -> Metric:
    used = limit = None
    qualifier = ""
    try:
        account = conf.read("aws_target")
        limit = float(account.value("daily_limit_usd", aws_config.money, recover_invalid=False))
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        directory = target_config.ssh_target_state_directory("aws")
        shown = spend.read_spend(account.source, directory, int(now), today)
        if isinstance(shown, spend.Spend):
            day = next((item for item in shown.days if item.day == today), None)
            if day is not None:
                used = float(day.usd)
                qualifier = "bill" if day.source == "AWS bill" else "estimate"
    except (OSError, ValueError, KeyError, ArithmeticError):
        pass
    return Metric("money", used, limit, "USD", qualifier)


def background(now: float, active_folders: Sequence[str] = ()) -> Background:
    """Read saved sources at the report interval."""
    origins = Origins()
    try:
        history = charts.read_index(lambda connection: recent_steps(connection, now, origins))
    except (OSError, ValueError) as error:
        history = charts.IndexUnavailable(str(error))
    for folder in active_folders:
        _ = origins.resolve("", folder, read_checkout=True)
    settings = conf.read("build_admission")
    try:
        cloud_slots = settings.whole("cloud_gate_slots")
    except conf.Unusable:
        cloud_slots = None
    try:
        disk_floor = settings.whole("free_floor_gib")
    except conf.Unusable:
        disk_floor = None
    hana, aws = aws_routes()
    return Background(history, spending(now), cloud_slots, disk_floor, hana, aws, origins)


@dataclass(frozen=True)
class VisibleWait:
    source: queue_view.WaitingBuild | wire.CloudRow
    name: str


def visible_waits(model: queue_view.QueueModel, local_names: Mapping[int, str],
                  cloud_names: Mapping[wire.Owner, str]) -> tuple[VisibleWait, ...]:
    """Use the same waits and order as the existing list."""
    waits: list[VisibleWait] = []
    for entry in queue_view.waiting_builds(model):
        if isinstance(entry, queue_view.WaitingBuild):
            name = local_names.get(entry.position)
        elif isinstance(entry, wire.CloudRow):
            name = cloud_names.get(entry.owner)
        else:
            continue
        if name is not None:
            waits.append(VisibleWait(entry, name))
    return tuple(waits)


WAIT_WORDS: Mapping[type[object], str] = {
    records.TokenWaitingWaiter: "token",
    records.EstimateExpired: "expired",
    records.ReadingUnavailable: "unreadable",
    records.NatedevHeld: "hold",
    records.MemoryPressure: "pressure",
    records.PressureUnavailable: "pressure_unknown",
    records.StartSpacingWait: "spacing",
    records.BuildSpaceUnreadable: "disk_unknown",
    records.BuildSpaceLow: "disk_low",
    placement.CargoFolderWait: "cargo_folder",
    records.NeedsMemory: "needs_memory",
    records.GpuSlotWait: "gpu_full",
    records.BehindHead: "order",
    records.ReadyForAdmission: "ready",
    wire.CloudSlotWait: "cloud_wait",
    ReservedCloudSlot: "cloud_reserved",
}


def wait_text(reason: records.WaitReason | wire.CloudSlotWait) -> str:
    if isinstance(reason, records.HeadAdmissionStatus):
        reason = reason.cause
    if isinstance(reason, records.RunLimitWait):
        return WORDS["run_full"].format(repository=reason.repository, machine=reason.machine)
    if isinstance(reason, records.BehindHead) and reason.head_name == "remote startup":
        return WORDS["remote_start"]
    if isinstance(reason, wire.CloudSlotWait) and isinstance(reason.state, ReservedCloudSlot):
        return WORDS[WAIT_WORDS[ReservedCloudSlot]]
    return WORDS[WAIT_WORDS.get(type(reason), "unknown_reason")]


def local_state(snapshots: target_availability.Snapshots, machine: str, active: bool, zone: tzinfo) -> str:
    if isinstance(snapshots, tuple):
        for snapshot in snapshots:
            if snapshot.target.name != machine or not isinstance(snapshot.target, target_config.LocalTarget):
                continue
            for state in snapshot.states:
                if isinstance(state, targets.Held):
                    if isinstance(state.end, (targets.ExpiresAt, targets.ReleaseEstimated)):
                        return WORDS["held_until"].format(time=targets.clock(state.end.when, zone))
                    return WORDS["held"]
                if isinstance(state, targets.Unreachable):
                    return WORDS["unreachable"]
                if isinstance(state, targets.StateUnreadable):
                    return WORDS["unknown_state"]
            if active or any(isinstance(state, targets.Running) for state in snapshot.states):
                return WORDS["in_use"]
            return WORDS["free"] if any(isinstance(state, targets.Free) for state in snapshot.states) else WORDS["unknown_state"]
    return WORDS["in_use"] if active else WORDS["unknown_state"]


def build_limits(reading: records.QueueRead, snapshots: target_availability.Snapshots,
                 waits: Sequence[VisibleWait], cloud_names: Mapping[wire.Owner, str],
                 names: Mapping[str, str], saved: Background, zone: tzinfo) -> BuildLimits:
    explanation = reading.explanation
    inputs = explanation.decided_from
    policy = explanation.policy.repository_runs
    local = inputs.local_machine
    origins = saved.origins
    history = saved.history if isinstance(saved.history, dict) else {}
    rows = {key: RepositoryRow(name) for key, name in history.items()}
    local_waits: dict[str, list[str]] = {}
    aws_waits: dict[str, list[str]] = {}
    unplaced: dict[str, list[str]] = {}
    metric_waits: dict[str, dict[str, list[str]]] = {}
    policy_rows: dict[str, set[str]] = {}

    def row(key: str) -> RepositoryRow:
        if key not in rows:
            rows[key] = RepositoryRow(key.rsplit("/", 1)[-1] if key.startswith("https://") else WORDS["unknown_repository"])
        return rows[key]

    prefix = local + "."
    seen: set[str] = set()
    for reservation in sorted(inputs.reservations, key=lambda item: item["step"] not in records.REPOSITORY_RUN_STEPS):
        owner = repository_run_owner(reservation)
        if owner in seen:
            continue
        seen.add(owner)
        waiter = reservation.get("queue_waiter")
        step = reservation["step"]
        repo = reservation["repo"] if waiter is None else waiter.get("repository_name", reservation["repo"])
        identity = reservation.get("repo_identity") or recorded_origin(reservation["repo"])
        if waiter is not None:
            identity = waiter.get("repo_identity") or identity
        key = origins.resolve(identity, reservation["worktree"])
        limit = policy.for_step(local, repo, step)
        if key and limit:
            policy_rows.setdefault(repo, set()).add(key)
        if not key:
            key = "policy:" + repo if limit else ""
        unit = (waiter.get("unit") or names.get(waiter.get("session_id", ""), "")) if waiter is not None else ""
        unit = unit or reservation.get("unit", "")
        session_name = waiter.get("session_name", "") if waiter is not None else reservation.get("session_name", "")
        label = unit or public_queue_text(session_name) or step or WORDS["unknown_name"]
        cell = row(key).natedev
        if key and repo and prefix + repo not in policy.by_machine_and_repository:
            cell.no_limit = True
        exempt = policy.exempt_unit and (unit == policy.exempt_unit
            or records.run_owner(reservation["pid"], reservation["start_time"]) in inputs.run_limit_exempt_owners)
        if limit and exempt:
            cell.exempt.append(label)
        elif limit:
            cell.runs.append(label)
        else:
            cell.other.append(label)
    ordered = {(item["pid"], item["since"]): item for _, item in reading.ordered}
    for visible in waits:
        entry = visible.source
        if isinstance(entry, wire.CloudRow):
            if isinstance(entry.wait, wire.CloudSlotWait):
                metric_waits.setdefault("cloud", {}).setdefault(wait_text(entry.wait), []).append(visible.name)
            continue
        step = entry.step
        reason = step.reason.cause if isinstance(step.reason, records.HeadAdmissionStatus) else step.reason
        if isinstance(reason, records.RunLimitWait) and reason.machine == local:
            waiter = ordered.get((step.pid, step.since))
            key = origins.resolve(waiter.get("repo_identity", "") if waiter is not None else "", step.folder)
            if key:
                policy_rows.setdefault(reason.repository, set()).add(key)
            cell = row(key or "policy:" + reason.repository).natedev
            destination = cell.waits
        elif isinstance(step.route, target_config.PinnedTarget) and step.route.target in saved.aws_targets:
            destination = aws_waits
        elif (isinstance(step.route, target_config.MacOnly)
              or isinstance(step.route, target_config.PinnedTarget) and step.route.target != local):
            destination = unplaced
        elif isinstance(reason, (records.BuildSpaceLow, records.BuildSpaceUnreadable)):
            destination = metric_waits.setdefault("disk", {})
        elif isinstance(reason, records.GpuSlotWait):
            destination = metric_waits.setdefault("gpu", {})
        elif isinstance(reason, (records.NeedsMemory, records.MemoryPressure, records.PressureUnavailable)):
            destination = metric_waits.setdefault("memory", {})
        else:
            destination = local_waits
        destination.setdefault(wait_text(step.reason), []).append(visible.name)

    occupancy = reserved_repository_runs(inputs, policy)
    ambiguous = Counter(origin for bound in policy_rows.values() for origin in bound)
    for key, limit in policy.by_machine_and_repository.items():
        if not key.startswith(prefix):
            continue
        name = key.removeprefix(prefix)
        bound = {origin for origin in policy_rows.get(name, ()) if ambiguous[origin] == 1}
        policy_key = "policy:" + name
        if not bound or policy_key in rows:
            bound = {*bound, policy_key}
            row(policy_key).name = WORDS["policy_key"].format(name=name)
        for origin in bound:
            cell = rows[origin].natedev
            cell.limit = limit
            cell.used = len(occupancy.get(name, ()))
            if len(bound) > 1:
                cell.note = WORDS["shared_policy"].format(name=name)

    cloud_slots = saved.cloud_slots
    cloud_used = 0
    if isinstance(reading, wire.CloudQueueRead) and reading.cloud:
        cloud_slots = reading.cloud_slots
        for cloud in reading.cloud:
            if isinstance(cloud.wait, wire.CloudSlotWait):
                cloud_used += isinstance(cloud.wait.state, ReservedCloudSlot)
                continue
            cloud_used += 1
            cell = row(origins.resolve("", cloud.identity.folder)).aws
            cell.runs.append(cloud_names.get(cloud.owner) or cloud.identity.unit or cloud.identity.step or WORDS["unknown_name"])
            cell.note = WORDS["uses_cloud"]

    for hana in saved.hana:
        if origin := origins.resolve(hana.repository, ""):
            cell = row(origin).aws
            cell.limit, cell.used = hana.limit, hana.used
            for run in reading.view.running:
                if (run.assignment_host == hana.host and run.started_at
                        and isinstance(run.execution, execution_identity.ObservedExecution)
                        and origins.resolve("", run.folder) == origin):
                    cell.runs.append(run.unit or names.get(run.session_id) or run.step)
            if hana.used is not None and hana.used > len(cell.runs):
                cell.note = WORDS["occupied"]

    for item in rows.values():
        if item.natedev.limit is None:
            item.natedev.runs.extend(item.natedev.other)
            item.natedev.other.clear()
            item.natedev.used = len(item.natedev.runs)
    counts = Counter(item.name for item in rows.values())
    for key, item in rows.items():
        if counts[item.name] > 1 and key.startswith("https://"):
            item.name = key.removeprefix("https://")

    room = explanation.room
    memory_used = room.cap_use / records.GIB if isinstance(room, records.MemoryRoom) else None
    memory_cap = room.cap / records.GIB if isinstance(room, records.MemoryRoom) and isinstance(room.cap, int) else None
    limits = [Metric("memory", memory_used, memory_cap, "GiB", waits=metric_waits.get("memory", {}))]
    gpu = explanation.policy.gpu_tests
    if gpu.repositories or "gpu" in metric_waits:
        used = sum(gpu_test_reservation(item, gpu) for item in inputs.reservations) if gpu.repositories else None
        limits.append(Metric("gpu", used, gpu.slots if gpu.repositories else None, waits=metric_waits.get("gpu", {})))
    disk = inputs.build_space
    limits.append(Metric("disk", disk.free_gib if isinstance(disk, records.BuildSpaceLow) else None,
                         disk.floor_gib if isinstance(disk, records.BuildSpaceLow) else saved.disk_floor, "GiB", "minimum", metric_waits.get("disk", {})))
    aws_state = "in_use" if cloud_used or any(hana.used for hana in saved.hana) else "unknown_state"
    return BuildLimits(MachineLimits(local_state(snapshots, local, bool(inputs.reservations), zone), tuple(limits), local_waits),
                       MachineLimits(WORDS[aws_state],
                           (Metric("cloud", cloud_used, cloud_slots, waits=metric_waits.get("cloud", {})), saved.aws_money), aws_waits),
                       tuple(sorted(rows.values(), key=lambda item: item.name.casefold())),
                       isinstance(saved.history, charts.IndexUnavailable), saved.history_stale, unplaced)
