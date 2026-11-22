"""Read the published records for the Build limits section."""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, tzinfo
from enum import Enum
from threading import Thread
from pathlib import Path
from typing import Literal, cast, final
from urllib.parse import urlsplit

from scripts.aws_target import config as aws_config, hana_box, hana_placement, spend
from scripts.build_queue import admission_protocol as wire, admission_records as records
from scripts.build_queue import placement, queue_view, target_availability
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.slot_wait import public_queue_text
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport, HanaGpuNextestTransport
from scripts.buildlog import charts, repository, store
from scripts.lib import conf, git_files
from scripts.lint.memory_admit import gpu_test_reservation, repository_run_owner, reserved_repository_runs
from scripts.mac_test import claim_state

WORDS: Mapping[str | type[object], str] = {
    "title": "Build limits",
    "table": "Build limits by repository and machine",
    "repository": "Repository",
    "local": "natedev",
    "aws": "AWS",
    "not_available": "Not available",
    "count": "{used} of {limit}",
    "limit": "Limit {limit}",
    "no_limit": "No limit",
    "running": "{count} running",
    "other_running": "{count} other running",
    "merge_above": "A merge run above the limit",
    "policy_key": "{name} run limit",
    "repository_run_limit": "Repository run limit",
    "uses_cloud": "Uses the AWS places",
    "memory": "Memory for builds",
    "gpu": "GPU tests",
    "disk": "Free disk",
    "cloud": "Places",
    "money": "Money today",
    "minimum_value": "{used} · {limit} minimum",
    "gib_value": "{value} GiB",
    "money_value": "${value}",
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
    "run_limit_full": "The repository run limit is full.",
    records.BehindHead: "This run waits behind an earlier step in the queue.",
    "remote_start": "This run waits while a step starts on another machine.",
    records.ReadyForAdmission: "The queue can start this run at its next check.",
    wire.CloudSlotWait: "This run waits for a place on AWS.",
    ReservedCloudSlot: "This run has a place on AWS and waits for its machine to start.",
}
PAGE_WORDS = {key: text for key, text in WORDS.items() if isinstance(key, str)}

HISTORY_SECONDS = 30 * 24 * 60 * 60


class ReadingState(Enum):
    OMITTED = "omitted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Metric:
    label: str
    used: float | None
    limit: float | None
    unit: Literal["", "GiB", "USD"] = ""
    qualifier: str = ""
    waits: Mapping[str, list[str]] = field(default_factory=dict[str, list[str]])
    unreadable: bool = False
    reserved_waiting: int = 0


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
    aws: MachineLimits
    repositories: tuple[RepositoryRow, ...]
    history_unavailable: bool
    unplaced: dict[str, list[str]]
    words: Mapping[str, str] = field(default_factory=lambda: PAGE_WORDS)


def recorded_origin(identity: str) -> str:
    if not identity.startswith("https://"):
        return ""
    try:
        parsed = urlsplit(identity)
        return repository.remote_identity(identity, "") if parsed.hostname and parsed.path.strip("/") else ""
    except ValueError:
        return ""


@dataclass(frozen=True)
class OriginReading:
    checkout: Path
    config: Path
    device: int
    inode: int
    modified_ns: int
    remote: str | Path


def checkout_origin(folder: Path, earlier: OriginReading | None, *,
                    local_remote: bool) -> OriginReading | None:
    try:
        checkout = git_files.at(folder) if local_remote else git_files.locate(folder)
        config = checkout.common_dir / "config"
        status = config.stat()
        if (earlier is not None and earlier.checkout == checkout.toplevel
                and earlier.config == config and earlier.device == status.st_dev
                and earlier.inode == status.st_ino and earlier.modified_ns == status.st_mtime_ns):
            return earlier
        value = repository.output(
            ["git", "config", "--no-includes", "--file", str(config), "--get", "remote.origin.url"],
            str(folder), repository.GIT_TIMEOUT_S)
        if value is None:
            return None
        remote = value.strip()
        identity = repository.remote_identity(remote, str(folder)) if remote else ""
        destination: str | Path
        if identity.startswith("/"):
            local_path = urlsplit(remote).path if remote.startswith("file:") else remote
            destination = (folder / Path(local_path).expanduser()).resolve()
        else:
            destination = recorded_origin(identity)
        return OriginReading(checkout.toplevel, config, status.st_dev, status.st_ino, status.st_mtime_ns, destination)
    except (OSError, ValueError, RuntimeError):
        return None


@dataclass(frozen=True)
class Origins:
    folders: Mapping[Path, OriginReading] = field(default_factory=dict[Path, OriginReading])
    resolved: Mapping[str, str] = field(default_factory=dict[str, str])

    def resolve(self, identity: str, folder: str) -> str:
        """Use the recorded origin or the checkout's own origin."""
        return recorded_origin(identity) or self.resolved.get(identity) or self.resolved.get(folder, "")

    def refresh(self, folders: Iterable[str]) -> Origins:
        """Read each checkout config and reuse values for unchanged files."""
        found: dict[Path, OriginReading] = {}
        resolved: dict[str, str] = {}
        for folder in folders:
            path = Path(folder)
            if not path.is_absolute():
                continue
            for depth in range(3):
                entry = found.get(path)
                if entry is None:
                    entry = checkout_origin(path, self.folders.get(path), local_remote=depth > 0)
                    if entry is None:
                        break
                    found[path] = entry
                if depth and path != entry.checkout:
                    break
                if isinstance(entry.remote, str):
                    resolved[folder] = entry.remote
                    break
                path = entry.remote
        return Origins(found, resolved)


@dataclass(frozen=True)
class CheckoutUse:
    folder: str
    owner: wire.Owner
    reservation: str = ""


def active_checkouts(reading: records.QueueRead) -> frozenset[CheckoutUse]:
    checkouts = {CheckoutUse(item["worktree"], wire.Owner(item["pid"], item["start_time"]),
                             item.get("reservation_id", item["admitted_at"]))
                 for item in reading.explanation.decided_from.reservations if item["worktree"]}
    if isinstance(reading, wire.CloudQueueRead):
        checkouts.update(CheckoutUse(row.identity.folder, row.owner) for row in reading.cloud if row.identity.folder)
    return frozenset(checkouts)


def recent_steps(connection: sqlite3.Connection, now: float) -> frozenset[str]:
    """Collect the origins recorded by build steps in the last 30 days."""
    rows = cast(Iterable[tuple[object]], connection.execute(
        """SELECT DISTINCT repo_identity FROM steps WHERE started_at >= ? AND started_at <= ?""",
        (store.utc_iso(now - HISTORY_SECONDS), store.utc_iso(now))))
    return frozenset(origin for (identity,) in rows
                     if isinstance(identity, str) and (origin := recorded_origin(identity)))


def read_history(now: float) -> frozenset[str] | ReadingState:
    history = charts.read_index(lambda connection: recent_steps(connection, now))
    return ReadingState.UNAVAILABLE if isinstance(history, charts.IndexUnavailable) else history


@dataclass(frozen=True)
class HanaCapacity:
    repository: str
    limit: int
    used: int | ReadingState


@dataclass(frozen=True)
class Background:
    history: frozenset[str] | ReadingState
    aws_money: Metric
    cloud_slots: int | ReadingState = ReadingState.OMITTED
    hana: tuple[HanaCapacity, ...] = ()
    aws_targets: frozenset[str] = field(default_factory=frozenset[str])
    origins: Origins = field(default_factory=Origins)
    aws_routes_unavailable: bool = False


def hana_use(target: str, transport: HanaControlTransport, policy: hana_placement.EnabledHanaRoute) -> int | ReadingState:
    """Count claims when both reads agree on the same work and host boot."""
    try:
        controller = hana_placement.controller_for(transport, read_only=True)
        paths = claim_state.state_paths(target)
        lifecycle = controller.read_saved_lifecycle()
        claims = claim_state.read_claims(paths)
        if lifecycle != controller.read_saved_lifecycle() or claims != claim_state.read_claims(paths):
            return ReadingState.OMITTED
    except (OSError, ValueError, KeyError, TypeError, claim_state.StateReadError):
        return ReadingState.UNAVAILABLE
    if not isinstance(lifecycle, hana_box.HanaLifecycleEvidence):
        return ReadingState.OMITTED
    recorded = {claim.execution for claim in lifecycle.claims}
    if not recorded and not claims:
        return 0
    if not isinstance(lifecycle.boot, hana_box.VerifiedBoot):
        return ReadingState.OMITTED
    generation = hana_placement.generation_text(hana_placement.HanaHostGeneration(
        lifecycle.deployment.deployment, lifecycle.boot.start_token, lifecycle.boot.boot_id))
    matched: set[str] = set()
    for claim in claims:
        if (not isinstance(claim.scope, claim_state.RepositoryScope) or claim.scope.key != policy.repository_key
                or claim.record.get("remote_host") != transport.host
                or claim.record.get("instance_generation") != generation):
            return ReadingState.OMITTED
        matched.add(claim.record["claim_id"])
    return len(matched) if matched == recorded else ReadingState.OMITTED


@dataclass(frozen=True)
class HanaRoute:
    target: str
    transport: HanaControlTransport
    settings: hana_placement.EnabledHanaRoute


@dataclass(frozen=True)
class AwsRoutes:
    hana: tuple[HanaRoute, ...] = ()
    targets: frozenset[str] = field(default_factory=frozenset[str])


def aws_routes() -> AwsRoutes | ReadingState:
    try:
        settings = conf.read("build_targets")
        targets_read = target_config.read_targets(configuration=settings, purpose=target_config.RegistryRead.PLACEMENT)
        pinned = settings.value("pinned_targets", str, default="", allow_blank=True)
    except (OSError, ValueError):
        return ReadingState.UNAVAILABLE
    configured = {target.name: target for target in targets_read}
    for name in filter(None, (value.strip() for value in pinned.split(","))):
        if name not in configured:
            try:
                configured.update((target.name, target) for target in target_config.read_targets(
                    selected=name, configuration=settings, purpose=target_config.RegistryRead.PLACEMENT))
            except (OSError, ValueError):
                continue
    hana: list[HanaRoute] = []
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
                    hana.append(HanaRoute(target.name, transport, route))
        except (OSError, ValueError, KeyError):
            continue
    return AwsRoutes(tuple(hana), frozenset(aws))


def read_hana_occupancy(routes: tuple[HanaRoute, ...]) -> dict[HanaRoute, int | ReadingState]:
    return {route: hana_use(route.target, route.transport, route.settings) for route in routes}


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


def read_cloud_slots() -> int:
    return conf.read("build_admission").whole("cloud_gate_slots")


def safe_read[T](read: Callable[[], T]) -> T | ReadingState:
    try:
        return read()
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error):
        return ReadingState.UNAVAILABLE


def background(history: frozenset[str] | ReadingState, money: Metric | ReadingState,
               cloud_slots: int | ReadingState, routes: AwsRoutes | ReadingState,
               occupancy: Mapping[HanaRoute, int | ReadingState] | ReadingState,
               origins: Origins | ReadingState) -> Background:
    """Keep the facts available from each independent source."""
    aws = AwsRoutes() if isinstance(routes, ReadingState) else routes
    hana = tuple(HanaCapacity(route.settings.repository_key, route.settings.max_steps,
                             occupancy if isinstance(occupancy, ReadingState)
                             else occupancy.get(route, ReadingState.OMITTED)) for route in aws.hana)
    spend_metric = Metric("money", None, None, "USD", unreadable=money is ReadingState.UNAVAILABLE)
    return Background(history, spend_metric if isinstance(money, ReadingState) else money,
                      cloud_slots, hana, aws.targets,
                      Origins() if isinstance(origins, ReadingState) else origins,
                      routes is ReadingState.UNAVAILABLE)


def background_once(now: float, reading: records.QueueRead) -> Background:
    """Read the same sources for a one-time page."""
    routes = safe_read(aws_routes)
    occupancy = (safe_read(lambda: read_hana_occupancy(routes.hana))
                 if isinstance(routes, AwsRoutes) else ReadingState.OMITTED)
    return background(safe_read(lambda: read_history(now)), safe_read(lambda: spending(now)),
                      safe_read(read_cloud_slots), routes, occupancy,
                      safe_read(lambda: Origins().refresh({use.folder for use in active_checkouts(reading)})))


type RefreshKey = date | frozenset[CheckoutUse] | tuple[HanaRoute, ...]


@dataclass(frozen=True)
class SourceSample[T]:
    value: T | ReadingState
    started_at: float
    key: RefreshKey | None


class SourceCache[T]:
    def __init__(self) -> None:
        self.sample: SourceSample[T] | None = None
        self.pending: Future[SourceSample[T]] | None = None

    def refresh(self, result: Future[SourceSample[T]], read: Callable[[], T | ReadingState],
                started_at: float, key: RefreshKey | None) -> None:
        result.set_result(SourceSample(safe_read(read), started_at, key))

    def reusable(self) -> T | ReadingState:
        return ReadingState.OMITTED if self.sample is None else self.sample.value

    def current(self, now: float, interval: float, read: Callable[[], T | ReadingState], *,
                key: RefreshKey | None = None) -> T | ReadingState:
        if self.pending is not None and self.pending.done():
            self.sample = self.pending.result()
            self.pending = None
        same_key = self.sample is not None and self.sample.key == key
        age = now - self.sample.started_at if self.sample is not None else float("inf")
        if self.pending is None and (not same_key or age >= interval):
            result: Future[SourceSample[T]] = Future()
            self.pending = result
            Thread(target=self.refresh, args=(result, read, now, key),
                   name="build-limits-refresh", daemon=True).start()
        if same_key and age < interval * 2:
            return self.reusable()
        return ReadingState.OMITTED


@final
class LimitsRefresh:
    def __init__(self, report_interval: float, token_interval: float) -> None:
        self.report_interval = report_interval
        self.token_interval = token_interval
        self.history = SourceCache[frozenset[str]]()
        self.money = SourceCache[Metric]()
        self.cloud_slots = SourceCache[int]()
        self.routes = SourceCache[AwsRoutes]()
        self.hana_occupancy = SourceCache[dict[HanaRoute, int | ReadingState]]()
        self.origins = SourceCache[Origins]()

    def current(self, reading: records.QueueRead, now: float, wall: float) -> Background:
        checkouts = active_checkouts(reading)

        def read_origins() -> Origins:
            previous = self.origins.reusable()
            cache = previous if isinstance(previous, Origins) else Origins()
            return cache.refresh({use.folder for use in checkouts})

        day = datetime.fromtimestamp(wall, UTC).date()
        routes = self.routes.current(now, self.token_interval, aws_routes)
        occupancy = (self.hana_occupancy.current(now, self.token_interval,
                      lambda: read_hana_occupancy(routes.hana), key=routes.hana)
                     if isinstance(routes, AwsRoutes) and routes.hana else ReadingState.OMITTED)
        return background(
            self.history.current(now, self.report_interval, lambda: read_history(wall)),
            self.money.current(now, self.report_interval, lambda: spending(wall), key=day),
            self.cloud_slots.current(now, self.report_interval, read_cloud_slots),
            routes, occupancy,
            self.origins.current(now, self.token_interval, read_origins, key=checkouts))


@dataclass(frozen=True)
class VisibleWait:
    source: queue_view.WaitingBuild | wire.CloudRow | wire.RepositorySlotRow
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
            name = entry.identity.unit or entry.identity.name
        waits.append(VisibleWait(entry, clean_label(name)))
    return tuple(waits)


def clean_label(*candidates: str) -> str:
    """Keep a usable label without exposing a path."""
    return next((label for candidate in candidates if (label := public_queue_text(candidate))
                 and "/" not in label and "\\" not in label), "")


def short_name(origin: str) -> str:
    return clean_label(origin.rsplit("/", 1)[-1])


def policy_name(key: str) -> str:
    """Use a configured short name when the key is a name."""
    return clean_label(key) if ":" not in key and key != store.UNKNOWN_IDENTITY else ""


def wait_text(reason: records.WaitReason | wire.CloudSlotWait, origin: str = "") -> str:
    if isinstance(reason, records.HeadAdmissionStatus):
        reason = reason.cause
    if isinstance(reason, records.RunLimitWait):
        name = short_name(origin) or policy_name(reason.repository)
        machine = clean_label(reason.machine)
        if name and machine:
            return WORDS[records.RunLimitWait].format(repository=name, machine=machine)
        return WORDS["run_limit_full"]
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


def build_limits(reading: records.QueueRead, snapshots: target_availability.Snapshots,
                 waits: Sequence[VisibleWait], cloud_names: Mapping[wire.Owner, str],
                 names: Mapping[str, str], saved: Background, zone: tzinfo) -> BuildLimits:
    explanation = reading.explanation
    inputs = explanation.decided_from
    policy = explanation.policy.repository_runs
    local = inputs.local_machine
    prefix = local + "."
    local_limits = {key.removeprefix(prefix): limit for key, limit in policy.by_machine_and_repository.items()
                    if key.startswith(prefix)}
    origins = saved.origins
    history = saved.history if isinstance(saved.history, frozenset) else ()
    rows: dict[str, RepositoryRow] = {}
    local_waits: dict[str, list[str]] = {}
    aws_waits: dict[str, list[str]] = {}
    unplaced: dict[str, list[str]] = {}
    metric_waits: dict[str, dict[str, list[str]]] = {}
    policy_rows: dict[str, set[str]] = {}
    run_labels: dict[tuple[str, str], tuple[str, str]] = {}
    mac_targets = (frozenset(snapshot.target.name for snapshot in snapshots if snapshot.target.platform == "macos")
                   if isinstance(snapshots, tuple) else frozenset[str]())

    def row(origin: str) -> RepositoryRow:
        if origin not in rows:
            name = short_name(origin)
            rows[origin] = RepositoryRow(name, natedev=Cell(has_limit_key=name in local_limits or origin in local_limits))
        return rows[origin]

    def remote_wait(machine: str) -> dict[str, list[str]]:
        return aws_waits if machine in saved.aws_targets else unplaced

    for origin in history:
        if short_name(origin):
            _ = row(origin)

    occupancy = reserved_repository_runs(inputs, policy)
    seen = {owner for owners in occupancy.values() for owner in owners}
    limited_owners = frozenset(seen)
    # Use the same admission rule to identify the key of each reservation.
    single_reservation = replace(inputs, ordered=(), reservations=())
    unattributed = 0
    for reservation in inputs.reservations:
        owner = repository_run_owner(reservation)
        waiter = reservation.get("queue_waiter")
        waiting_origin = recorded_origin(waiter.get("repo_identity", "")) if waiter is not None else ""
        reserved_origin = recorded_origin(reservation.get("repo_identity", ""))
        origin = ""
        if not (waiting_origin and reserved_origin and waiting_origin != reserved_origin):
            identity = waiting_origin or reserved_origin or recorded_origin(reservation["repo"])
            origin = origins.resolve(identity, reservation["worktree"])
            if not short_name(origin):
                origin = ""
        recorded_unit = reservation.get("unit", "") or (waiter.get("unit", "") if waiter is not None else "")
        session = waiter.get("session_id", "") if waiter is not None else reservation.get("session_id", "")
        session_name = waiter.get("session_name", "") if waiter is not None else reservation.get("session_name", "")
        if owner in limited_owners:
            if reservation["step"] in records.REPOSITORY_RUN_STEPS:
                keys = reserved_repository_runs(replace(single_reservation, reservations=(reservation,)), policy)
                for key in keys:
                    policy_rows.setdefault(key, set()).add(origin)
                    if origin:
                        row(origin).natedev.has_limit_key = True
                    _ = run_labels.setdefault((key, owner),
                        (clean_label(recorded_unit, names.get(session, ""), session_name), recorded_unit))
            continue
        if owner in seen:
            continue
        seen.add(owner)
        if not origin:
            unattributed += 1
            continue
        cell = row(origin).natedev
        cell.other_used += 1
        if label := clean_label(recorded_unit, names.get(session, ""), session_name, short_name(origin)):
            cell.other.append(label)

    bindings = Counter(origin for bound in policy_rows.values() for origin in bound)
    bound_origins = {
        key: origin for key, bound in policy_rows.items() if len(bound) == 1
        for origin in bound if origin and bindings[origin] == 1
    }
    full_waits: set[str] = set()
    for visible in waits:
        entry = visible.source
        if isinstance(entry, wire.CloudRow):
            if isinstance(entry.wait, wire.CloudSlotWait):
                metric_waits.setdefault("cloud", {}).setdefault(wait_text(entry.wait), []).append(visible.name)
            continue
        if isinstance(entry, wire.RepositorySlotRow):
            if entry.machine not in mac_targets:
                destination = local_waits if entry.machine == local else remote_wait(entry.machine)
                destination.setdefault("", []).append(visible.name)
            continue
        step = entry.step
        pinned = step.route.target if isinstance(step.route, target_config.PinnedTarget) else local
        if isinstance(step.route, target_config.MacOnly) or pinned in mac_targets:
            continue
        reason = step.reason.cause if isinstance(step.reason, records.HeadAdmissionStatus) else step.reason
        origin = ""
        if pinned != local:
            destination = remote_wait(pinned)
        elif isinstance(reason, records.RunLimitWait) and reason.machine == local:
            if reason.used >= reason.limit:
                full_waits.add(reason.repository)
            origin = bound_origins.get(reason.repository, "")
            destination = row(origin).natedev.waits if origin else local_waits
        elif isinstance(reason, (records.BuildSpaceLow, records.BuildSpaceUnreadable)):
            destination = metric_waits.setdefault("disk", {})
        elif isinstance(reason, records.GpuSlotWait):
            destination = metric_waits.setdefault("gpu", {})
        elif isinstance(reason, (records.NeedsMemory, records.MemoryPressure, records.PressureUnavailable)):
            destination = metric_waits.setdefault("memory", {})
        else:
            destination = local_waits
        destination.setdefault(wait_text(step.reason, origin), []).append(visible.name)

    shared: list[Metric] = []
    for repository_key, limit in local_limits.items():
        bound = policy_rows.get(repository_key, set[str]())
        owners = occupancy.get(repository_key, set[str]())
        used = len(owners)
        if used < limit and repository_key in full_waits:
            continue
        origin = bound_origins.get(repository_key, "")
        if not origin:
            labels = {short_name(candidate) for candidate in bound if candidate}
            name = next(iter(labels)) if len(labels) == 1 else policy_name(repository_key)
            label = WORDS["policy_key"].format(name=name) if name else WORDS["repository_run_limit"]
            shared.append(Metric(label, used, limit))
            continue
        cell = row(origin).natedev
        cell.limit, cell.used = limit, used
        for owner in sorted(owners):
            label, recorded_unit = run_labels.get((repository_key, owner), ("", ""))
            if used > limit and recorded_unit and recorded_unit == policy.exempt_unit:
                cell.merge_above.append(label)
            elif label:
                cell.runs.append(label)

    cloud_slots = saved.cloud_slots
    cloud_used = cloud_waiting = 0
    if isinstance(reading, wire.CloudQueueRead) and reading.cloud:
        cloud_slots = reading.cloud_slots
        for cloud in reading.cloud:
            if isinstance(cloud.wait, wire.CloudSlotWait):
                if isinstance(cloud.wait.state, ReservedCloudSlot):
                    cloud_used += 1
                    cloud_waiting += 1
                continue
            cloud_used += 1
            origin = origins.resolve("", cloud.identity.folder)
            if short_name(origin):
                cell = row(origin).aws
                cell.other_used += 1
                label = clean_label(cloud_names.get(cloud.owner, ""), cloud.identity.unit, short_name(origin))
                if label:
                    cell.other.append(label)
                cell.note = WORDS["uses_cloud"]

    for hana in saved.hana:
        origin = recorded_origin(hana.repository)
        if short_name(origin):
            cell = row(origin).aws
            cell.limit = hana.limit
            cell.other.clear()
            if isinstance(hana.used, int):
                cell.used = hana.used
            elif hana.used is ReadingState.UNAVAILABLE:
                cell.note = WORDS["not_available"]

    names_by_origin = {origin: (item.name, urlsplit(origin).path.strip("/").rpartition("/")[0])
                       for origin, item in rows.items()}
    names_count = Counter(name for name, _ in names_by_origin.values())
    owners_count = Counter(names_by_origin.values())
    for origin, item in rows.items():
        name, owner = names_by_origin[origin]
        if names_count[name] > 1:
            item.owner = owner
        if owners_count[name, owner] > 1:
            item.owner = " · ".join(filter(None, (owner, urlsplit(origin).netloc)))
        for cell in (item.natedev, item.aws):
            if cell.limit is None:
                cell.runs.extend(cell.other)
                cell.used = (cell.used or 0) + cell.other_used
                cell.other.clear()
                cell.other_used = 0
            if cell.used or cell.limit is not None or cell.waits or cell.runs or cell.other_used:
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
    disk_waits = metric_waits.get("disk", {})
    if isinstance(disk, records.BuildSpaceLow):
        limits.append(Metric("disk", disk.free_gib, disk.floor_gib, "GiB", "minimum", disk_waits))
    elif disk_waits:
        limits.append(Metric("disk", None, None, waits=disk_waits))
    aws_active = cloud_used or any(isinstance(hana.used, int) and hana.used for hana in saved.hana)
    aws_state = WORDS["in_use"] if aws_active else WORDS["not_available"] if saved.aws_routes_unavailable else ""
    cloud = Metric("cloud", cloud_used if isinstance(cloud_slots, int) else None,
                   cloud_slots if isinstance(cloud_slots, int) else None, waits=metric_waits.get("cloud", {}),
                   unreadable=cloud_slots is ReadingState.UNAVAILABLE, reserved_waiting=cloud_waiting)
    return BuildLimits(MachineLimits(local_state(snapshots, local, bool(inputs.reservations), zone),
                                    (*limits, *shared), local_waits, unattributed),
                       MachineLimits(aws_state, (cloud, saved.aws_money), aws_waits),
                       tuple(sorted(rows.values(), key=lambda item: (item.quiet, item.name.casefold(), item.owner.casefold()))),
                       saved.history is ReadingState.UNAVAILABLE, unplaced)
