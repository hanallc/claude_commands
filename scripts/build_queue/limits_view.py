"""Read the published records for the Build limits section."""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

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
from scripts.mac_test import claim_state, eligibility, mac_test

WORDS: Mapping[str | type[object], str] = {
    "title": "Build limits",
    "table": "Build limits by repository and machine",
    "repository": "Repository",
    "local": "natedev",
    "mac": "Mac",
    "aws": "AWS",
    "not_available": "Not available",
    "count": "{used} of {limit}",
    "limit": "Limit {limit}",
    "no_limit": "No limit",
    "running": "{count} running",
    "occupied": "{count} in use",
    "other_running": "{count} other running",
    "merge_above": "A merge run above the limit",
    "policy_key": "{name} run limit",
    "shared_policy": "Shared {name} run limit",
    "uses_cloud": "Uses the AWS places",
    "stale_history": "Build history is stale.",
    "memory": "Memory for builds",
    "gpu": "GPU tests",
    "disk": "Free disk",
    "cloud": "Places",
    "money": "Money today",
    "minimum_value": "{used} · {limit} minimum",
    "minimum_only": "{limit} minimum",
    "gib_value": "{value} GiB",
    "money_value": "${value}",
    "bill": "bill",
    "estimate": "estimate",
    "free": "Free",
    "in_use": "In use",
    "on": "On",
    "held": "Held",
    "held_until": "Held until {time}",
    "unreachable": "Unreachable",
    "waiting": "{count} waiting",
    "legend_run": "A run that is in progress",
    "legend_free": "A free place",
    "legend_line": "The limit",
    "legend_wait": "A run that waits",
    records.TokenWaitingWaiter: "The run waits for permission to continue its Cargo work.",
    records.EstimateExpired: "The queue needs a new memory estimate before this run can start.",
    records.ReadingUnavailable: "The queue cannot check whether this run can start.",
    records.NatedevHeld: "This run waits for the hold on natedev to end.",
    records.MemoryPressure: "This run waits while other work on the machine needs memory.",
    records.PressureUnavailable: "The queue cannot check whether other work on the machine needs memory.",
    records.StartSpacingWait: "This run waits briefly because another step just started.",
    records.BuildSpaceUnreadable: "The queue cannot check how much space is free on the build disk.",
    records.BuildSpaceLow: "The build disk has less free space than the queue requires.",
    placement.CargoFolderWait: "This run waits for another build to release the same build folder.",
    records.NeedsMemory: "This run needs more free memory to start.",
    records.GpuSlotWait: "All GPU test places are in use.",
    records.RunLimitWait: "The run limit for {repository} on {machine} is full.",
    records.BehindHead: "This run waits behind an earlier step in the queue.",
    "remote_start": "This run waits while a step starts on another machine.",
    records.ReadyForAdmission: "The queue can start this run at its next check.",
    wire.CloudSlotWait: "This run waits for a place on AWS.",
    ReservedCloudSlot: "This run has a place on AWS and waits for its machine to start.",
}
PAGE_WORDS = {key: text for key, text in WORDS.items() if isinstance(key, str)}

HISTORY_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class Metric:
    label: str
    used: float | None
    limit: float | None
    unit: Literal["", "GiB", "USD"] = ""
    qualifier: str = ""
    waits: Mapping[str, list[str]] = field(default_factory=dict[str, list[str]])
    unreadable: bool = False


@dataclass
class Cell:
    limit: int | None = None
    used: int | None = None
    other_used: int = 0
    has_limit_key: bool = False
    runs: list[str] = field(default_factory=list[str])
    merge_above: list[str] = field(default_factory=list[str])
    other: list[str] = field(default_factory=list[str])
    waits: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    note: str = ""


@dataclass
class RepositoryRow:
    name: str
    owner: str = ""
    quiet: bool = True
    natedev: Cell = field(default_factory=Cell)
    mac: Cell = field(default_factory=Cell)
    aws: Cell = field(default_factory=Cell)


@dataclass(frozen=True)
class MachineLimits:
    state: str
    shared: tuple[Metric, ...]
    waits: dict[str, list[str]]
    unattributed: int = 0


@dataclass(frozen=True)
class BuildLimits:
    natedev: MachineLimits
    mac: MachineLimits
    aws: MachineLimits
    repositories: tuple[RepositoryRow, ...]
    history_unavailable: bool
    history_stale: bool
    unplaced: dict[str, list[str]]
    words: Mapping[str, str] = field(default_factory=lambda: PAGE_WORDS)


@dataclass(frozen=True)
class MacClaim:
    scope: claim_state.ClaimScope
    limit: int
    key: str
    folder: str
    working_session: str


@dataclass(frozen=True)
class MacReading:
    target: str
    state: targets.TargetState | None
    claims: tuple[MacClaim, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict[str, int])


def read_mac(snapshots: target_availability.Snapshots, names: Iterable[str]) -> MacReading | None:
    """Resolve Mac claims and repository limits before the feed uses them."""
    if isinstance(snapshots, target_availability.SnapshotsUnreadable):
        return None
    for snapshot in snapshots:
        target = snapshot.target
        if isinstance(target, target_config.SshTarget) and target.platform == "macos":
            break
    else:
        return None
    state = snapshot.states[0]
    if isinstance(state, targets.StateUnreadable):
        return MacReading(target.name, state)
    paths = claim_state.StatePaths(target_config.ssh_target_state_directory(target.name), target.name,
                                  registry_selected=True, platform=target.platform)
    claims: dict[claim_state.ClaimIdentity, MacClaim] = {}
    for claim in snapshot.claims:
        record = claim.record
        identity = claim_state.claim_identity(record)
        if identity in claims or not mac_test.run_is_live(record):
            continue
        # The legacy Mac admits a checkout with a name-only scope.
        scope = (claim_state.RepositoryScope(claim.scope.name)
                 if isinstance(claim.scope, claim_state.RepositoryScope) else claim.scope)
        session = record["submitting_session"] if mac_test.run_holds_work(record) else ""
        claims[identity] = MacClaim(scope, mac_test.slot_capacity(paths, scope),
                                    record.get("repo_key", ""), record["worktree"], session)
    settings = conf.read(target.settings)
    limits: dict[str, int] = {}
    for name in names:
        # A check reads the repository rule without testing workspace packages.
        try:
            policy = eligibility.linux_only_policy(settings, eligibility.KnownRepository(name), Path(), "", "check")
            if isinstance(policy, eligibility.KnownRepository):
                limits[name] = mac_test.slot_capacity(paths, claim_state.RepositoryScope(name), configuration=settings)
        except conf.Unusable:
            continue
    return MacReading(target.name, state, tuple(claims.values()), limits)


def recorded_origin(identity: str) -> str:
    if not identity.startswith("https://"):
        return ""
    try:
        parsed = urlsplit(identity)
        return repository.remote_identity(identity, "") if parsed.hostname and parsed.path.strip("/") else ""
    except ValueError:
        return ""


@dataclass
class Origins:
    folders: dict[str, str] = field(default_factory=dict[str, str])
    active_folders: set[str] = field(default_factory=set[str])
    sources: dict[str, str] = field(default_factory=dict[str, str])
    names: dict[str, set[str]] = field(default_factory=dict[str, set[str]])

    def resolve(self, identity: str, folder: str) -> str:
        """Use a recorded origin or an origin from the last refresh."""
        if origin := recorded_origin(identity):
            return origin
        if identity.startswith("/"):
            self.active_folders.add(identity)
            if origin := self.folders.get(identity):
                return origin
        if not folder.startswith("/"):
            return ""
        self.active_folders.add(folder)
        return self.folders.get(folder, "")

    def read(self, folder: str) -> str:
        """Cache the origin of a checkout and its source Git folder."""
        if not folder.startswith("/"):
            return ""
        if folder in self.folders:
            return self.folders[folder]
        self.folders[folder] = ""
        if not Path(folder).is_dir() or (common := repository.git_common_dir(folder)) is None:
            return ""
        source = repository.source_git_dir(common)
        if source not in self.sources:
            remote = repository.output(
                ["git", "config", "--includes", "--file", str(Path(source) / "config"), "--get", "remote.origin.url"],
                folder, repository.GIT_TIMEOUT_S)
            try:
                self.sources[source] = recorded_origin(repository.remote_identity(remote.strip(), folder)) if remote else ""
            except ValueError:
                self.sources[source] = ""
            if (origin := self.sources[source]) and (name := repository.repo_name(folder)):
                self.names.setdefault(origin, set()).add(name)
        self.folders[folder] = self.sources[source]
        return self.folders[folder]


def recent_steps(connection: sqlite3.Connection, now: float, origins: Origins) -> frozenset[str]:
    """Collect the source origins of the build steps in the last 30 days."""
    rows = cast(Iterable[tuple[object, object]], connection.execute(
        """SELECT DISTINCT repo_identity, repo_path FROM steps WHERE started_at >= ? AND started_at <= ?""",
        (store.utc_iso(now - HISTORY_SECONDS), store.utc_iso(now))))
    history: set[str] = set()
    for identity, path in rows:
        key = identity if isinstance(identity, str) else ""
        folder = path if isinstance(path, str) else ""
        origin = recorded_origin(key) or origins.read(key)
        checkout_origin = origins.read(folder)
        if origin or checkout_origin:
            history.add(origin or checkout_origin)
    return frozenset(history)


@dataclass(frozen=True)
class HanaCapacity:
    repository: str
    host: str
    limit: int
    used: int | None


@dataclass(frozen=True)
class Background:
    history: frozenset[str] | charts.IndexUnavailable
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
    unreadable = True
    try:
        account = conf.read("aws_target")
        limit = float(account.value("daily_limit_usd", aws_config.money, recover_invalid=False))
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        directory = target_config.ssh_target_state_directory("aws")
        shown = spend.read_spend(account.source, directory, int(now), today)
        if isinstance(shown, spend.Spend):
            unreadable = False
            day = next((item for item in shown.days if item.day == today), None)
            if day is not None:
                used = float(day.usd)
                qualifier = "bill" if day.source == "AWS bill" else "estimate"
    except (OSError, ValueError, KeyError, ArithmeticError):
        pass
    return Metric("money", used, limit, "USD", qualifier, unreadable=unreadable)


def background(now: float, active_folders: Sequence[str] = ()) -> Background:
    """Read saved sources at the report interval."""
    origins = Origins()
    try:
        history = charts.read_index(lambda connection: recent_steps(connection, now, origins))
    except (OSError, ValueError) as error:
        history = charts.IndexUnavailable(str(error))
    for folder in active_folders:
        _ = origins.read(folder)
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
            name = local_names.get(entry.position, "")
        elif isinstance(entry, wire.CloudRow):
            name = cloud_names.get(entry.owner, "")
        else:
            continue
        waits.append(VisibleWait(entry, name))
    return tuple(waits)


def wait_text(reason: records.WaitReason | wire.CloudSlotWait) -> str:
    if isinstance(reason, records.HeadAdmissionStatus):
        reason = reason.cause
    if isinstance(reason, records.RunLimitWait):
        return WORDS[records.RunLimitWait].format(repository=reason.repository, machine=reason.machine)
    if isinstance(reason, records.BehindHead) and reason.head_name == "remote startup":
        return WORDS["remote_start"]
    if isinstance(reason, wire.CloudSlotWait) and isinstance(reason.state, ReservedCloudSlot):
        return WORDS[ReservedCloudSlot]
    return WORDS.get(type(reason), "")


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
                    return WORDS["not_available"]
            if active or any(isinstance(state, targets.Running) for state in snapshot.states):
                return WORDS["in_use"]
            return WORDS["free"] if any(isinstance(state, targets.Free) for state in snapshot.states) else ""
    if active:
        return WORDS["in_use"]
    return WORDS["not_available"] if isinstance(snapshots, target_availability.SnapshotsUnreadable) else ""


def mac_state(mac: MacReading | None, snapshots: target_availability.Snapshots, zone: tzinfo) -> str:
    if mac is None:
        return WORDS["not_available"] if isinstance(snapshots, target_availability.SnapshotsUnreadable) else ""
    state = mac.state
    if isinstance(state, targets.Held):
        if isinstance(state.end, targets.ExpiresAt):
            return WORDS["held_until"].format(time=targets.clock(state.end.when, zone))
        return WORDS["held"]
    if isinstance(state, targets.StateUnreadable):
        return WORDS["not_available"]
    return WORDS["on"] if isinstance(state, (targets.Free, targets.Running)) else ""


def place_mac_counts(mac: MacReading, rows: Sequence[tuple[str, RepositoryRow]],
                     claims: Mapping[claim_state.ClaimScope, Sequence[tuple[str, MacClaim]]],
                     origins: Origins, names: Mapping[str, str]) -> None:
    positions = {origin: index for index, (origin, _) in enumerate(rows)}
    for group in claims.values():
        known_claims = [(origin, claim) for origin, claim in group if origin]
        if not known_claims:
            continue
        owner = min((origin for origin, _ in known_claims), key=positions.__getitem__)
        cell = rows[positions[owner]][1].mac
        cell.used = (cell.used or 0) + len(known_claims)
        cell.limit = (cell.limit or 0) + known_claims[0][1].limit
        cell.runs.extend(name for _, claim in known_claims
                         if claim.working_session and (name := names.get(claim.working_session)))

    used_scopes = {scope.name for scope in claims if isinstance(scope, claim_state.RepositoryScope)}
    for origin, row in rows:
        if row.mac.used:
            continue
        configured = origins.names.get(origin, set()) & mac.limits.keys()
        if len(configured) == 1:
            name, = configured
            if name not in used_scopes:
                row.mac.used, row.mac.limit = 0, mac.limits[name]
                used_scopes.add(name)


def build_limits(reading: records.QueueRead, snapshots: target_availability.Snapshots,
                 waits: Sequence[VisibleWait], cloud_names: Mapping[wire.Owner, str],
                 names: Mapping[str, str], saved: Background, zone: tzinfo, *,
                 mac: MacReading | None = None) -> BuildLimits:
    explanation = reading.explanation
    inputs = explanation.decided_from
    policy = explanation.policy.repository_runs
    local = inputs.local_machine
    origins = saved.origins
    history = saved.history if isinstance(saved.history, frozenset) else ()
    rows: dict[str, RepositoryRow] = {}
    local_waits: dict[str, list[str]] = {}
    mac_waits: dict[str, list[str]] = {}
    aws_waits: dict[str, list[str]] = {}
    unplaced: dict[str, list[str]] = {}
    metric_waits: dict[str, dict[str, list[str]]] = {}
    policy_rows: dict[str, set[str]] = {}
    unbound_waits: dict[str, dict[str, list[str]]] = {}
    mac_claims: dict[claim_state.ClaimScope, list[tuple[str, MacClaim]]] = {}

    def row(origin: str) -> RepositoryRow:
        if origin not in rows:
            rows[origin] = RepositoryRow(origin.rsplit("/", 1)[-1])
        return rows[origin]

    for origin in history:
        _ = row(origin)
    mac_unattributed = 0
    if mac is not None:
        for claim in mac.claims:
            origin = origins.resolve(claim.key, claim.folder)
            mac_claims.setdefault(claim.scope, []).append((origin, claim))
            if origin:
                _ = row(origin)
            else:
                mac_unattributed += 1
    prefix = local + "."
    seen: set[str] = set()
    unattributed = 0
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
        origin = origins.resolve(identity, reservation["worktree"])
        if not origin:
            unattributed += 1
            continue
        cell = row(origin).natedev
        if prefix + repo in policy.by_machine_and_repository:
            cell.has_limit_key = True
            policy_rows.setdefault(repo, set()).add(origin)
        recorded_unit = reservation.get("unit", "") or (waiter.get("unit", "") if waiter is not None else "")
        session = waiter.get("session_id", "") if waiter is not None else reservation.get("session_id", "")
        session_name = waiter.get("session_name", "") if waiter is not None else reservation.get("session_name", "")
        label = recorded_unit or names.get(session) or public_queue_text(session_name) or step
        if policy.for_step(local, repo, step):
            cell.used = (cell.used or 0) + 1
            if label:
                cell.runs.append(label)
            if recorded_unit and recorded_unit == policy.exempt_unit:
                cell.merge_above.append(recorded_unit)
        else:
            cell.other_used += 1
            if label:
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
        if (isinstance(step.route, target_config.MacOnly)
                or mac is not None and isinstance(step.route, target_config.PinnedTarget)
                and step.route.target == mac.target):
            destination = mac_waits
        elif isinstance(reason, records.RunLimitWait) and reason.machine == local:
            waiter = ordered.get((step.pid, step.since))
            key = origins.resolve(waiter.get("repo_identity", "") if waiter is not None else "", step.folder)
            if key:
                policy_rows.setdefault(reason.repository, set()).add(key)
                cell = row(key).natedev
                cell.has_limit_key = True
                destination = cell.waits
            else:
                destination = unbound_waits.setdefault(reason.repository, {})
        elif isinstance(step.route, target_config.PinnedTarget) and step.route.target in saved.aws_targets:
            destination = aws_waits
        elif isinstance(step.route, target_config.PinnedTarget) and step.route.target != local:
            destination = unplaced
        elif isinstance(reason, (records.BuildSpaceLow, records.BuildSpaceUnreadable)):
            destination = metric_waits.setdefault("disk", {})
        elif isinstance(reason, records.GpuSlotWait):
            destination = metric_waits.setdefault("gpu", {})
        elif isinstance(reason, (records.NeedsMemory, records.MemoryPressure, records.PressureUnavailable)):
            destination = metric_waits.setdefault("memory", {})
        else:
            destination = local_waits
        destination.setdefault(wait_text(reason), []).append(visible.name)

    occupancy = reserved_repository_runs(inputs, policy)
    ambiguous = Counter(origin for bound in policy_rows.values() for origin in bound)
    unbound: list[Metric] = []
    for key, limit in policy.by_machine_and_repository.items():
        if not key.startswith(prefix):
            continue
        name = key.removeprefix(prefix)
        bound = {origin for origin in policy_rows.get(name, ()) if ambiguous[origin] == 1}
        used = len(occupancy.get(name, ()))
        waits_without_origin = unbound_waits.pop(name, {})
        if not bound or waits_without_origin:
            unbound.append(Metric(WORDS["policy_key"].format(name=name), used, limit, waits=waits_without_origin))
        for origin in bound:
            cell = rows[origin].natedev
            cell.limit = limit
            cell.used = used
            if len(bound) > 1:
                cell.note = WORDS["shared_policy"].format(name=name)
            if used <= limit:
                cell.merge_above.clear()
            for unit in cell.merge_above:
                cell.runs.remove(unit)
    for group in unbound_waits.values():
        for text, waiting in group.items():
            local_waits.setdefault(text, []).extend(waiting)

    cloud_slots = saved.cloud_slots
    cloud_used = 0
    cloud_recorded = isinstance(reading, wire.CloudQueueRead) and bool(reading.cloud)
    if isinstance(reading, wire.CloudQueueRead) and reading.cloud:
        cloud_slots = reading.cloud_slots
        for cloud in reading.cloud:
            if isinstance(cloud.wait, wire.CloudSlotWait):
                cloud_used += isinstance(cloud.wait.state, ReservedCloudSlot)
                continue
            cloud_used += 1
            if origin := origins.resolve("", cloud.identity.folder):
                cell = row(origin).aws
                cell.other_used += 1
                label = cloud_names.get(cloud.owner) or cloud.identity.unit or cloud.identity.step
                if label:
                    cell.other.append(label)
                cell.note = WORDS["uses_cloud"]

    aws_unattributed: set[int] = set()
    for hana in saved.hana:
        if origin := origins.resolve(hana.repository, ""):
            cell = row(origin).aws
            cell.limit, cell.used = hana.limit, hana.used
            for index, run in enumerate(reading.view.running):
                if (run.assignment_host != hana.host or not run.started_at
                        or not isinstance(run.execution, execution_identity.ObservedExecution)):
                    continue
                run_origin = origins.resolve("", run.folder)
                if not run_origin:
                    aws_unattributed.add(index)
                if run_origin == origin and (label := run.unit or names.get(run.session_id) or run.step):
                    cell.runs.append(label)

    counts = Counter(item.name for item in rows.values())
    for origin, item in rows.items():
        if counts[item.name] > 1:
            namespace = urlsplit(origin).path.strip("/").split("/")[:-1]
            item.owner = "/".join(namespace)
        for cell in (item.natedev, item.aws):
            if cell.limit is None:
                cell.runs.extend(cell.other)
                cell.used = (cell.used or 0) + cell.other_used
                cell.other.clear()
                cell.other_used = 0
                cell.merge_above.clear()
            if cell.used or cell.limit is not None or cell.waits or cell.runs or cell.other_used:
                item.quiet = False

    def page_order(item: RepositoryRow) -> tuple[bool, str, str]:
        return item.quiet, item.name.casefold(), item.owner.casefold()

    if mac is not None and mac.state is not None and not isinstance(mac.state, targets.StateUnreadable):
        place_mac_counts(mac, sorted(rows.items(), key=lambda pair: page_order(pair[1])), mac_claims, origins, names)
        for item in rows.values():
            if item.mac.used:
                item.quiet = False

    room = explanation.room
    memory_used = room.cap_use / records.GIB if isinstance(room, records.MemoryRoom) else None
    memory_cap = room.cap / records.GIB if isinstance(room, records.MemoryRoom) and isinstance(room.cap, int) else None
    limits = [Metric("memory", memory_used, memory_cap, "GiB", waits=metric_waits.get("memory", {}),
                     unreadable=isinstance(room, records.ReadingUnavailable))]
    gpu = explanation.policy.gpu_tests
    if gpu.repositories or "gpu" in metric_waits:
        used = sum(gpu_test_reservation(item, gpu) for item in inputs.reservations) if gpu.repositories else None
        limits.append(Metric("gpu", used, gpu.slots if gpu.repositories else None, waits=metric_waits.get("gpu", {})))
    disk = inputs.build_space
    limits.append(Metric("disk", disk.free_gib if isinstance(disk, records.BuildSpaceLow) else None,
                         disk.floor_gib if isinstance(disk, records.BuildSpaceLow) else saved.disk_floor, "GiB", "minimum", metric_waits.get("disk", {}),
                         unreadable=isinstance(disk, records.BuildSpaceUnreadable)))
    aws_state = WORDS["in_use"] if cloud_used or any(hana.used for hana in saved.hana) else ""
    cloud = Metric("cloud", cloud_used, cloud_slots, waits=metric_waits.get("cloud", {}),
                   unreadable=cloud_slots is None and not cloud_recorded)
    return BuildLimits(MachineLimits(local_state(snapshots, local, bool(inputs.reservations), zone),
                                    (*limits, *unbound), local_waits, unattributed),
                       MachineLimits(mac_state(mac, snapshots, zone), (), mac_waits, mac_unattributed),
                       MachineLimits(aws_state, (cloud, saved.aws_money), aws_waits, len(aws_unattributed)),
                       tuple(sorted(rows.values(), key=page_order)),
                       isinstance(saved.history, charts.IndexUnavailable), saved.history_stale, unplaced)
