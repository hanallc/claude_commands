"""Read the published records for the Build limits section."""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from enum import Enum, StrEnum
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

@dataclass(frozen=True)
class WaitWords:
    one: str
    many: str


WORDS: Mapping[str | type[object], str | WaitWords] = {
    "title": "Build limits",
    "table": "Build limits by repository and machine",
    "repository": "Repository",
    "local": "natedev",
    "aws": "AWS",
    "not_available": "Not available",
    "count": "{used} of {limit}",
    "limit": "Limit {limit}",
    "limit_set": "Limit set",
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
    "legend_run": "A run that is in progress",
    "legend_free": "A free place",
    "legend_line": "The limit",
    "legend_wait": "A run that waits",
    records.TokenWaitingWaiter: WaitWords("1 run waits for permission to continue its Cargo work.",
        "{count} runs wait for permission to continue their Cargo work."),
    records.EstimateExpired: WaitWords("1 run waits because the queue needs a new memory estimate before it can start.",
        "{count} runs wait because the queue needs new memory estimates before they can start."),
    records.ReadingUnavailable: WaitWords("1 run waits because the queue cannot check whether it can start.",
        "{count} runs wait because the queue cannot check whether they can start."),
    records.NatedevHeld: WaitWords("1 run waits for the hold on natedev to end.",
        "{count} runs wait for the hold on natedev to end."),
    records.MemoryPressure: WaitWords("1 run waits while other work on the machine needs memory.",
        "{count} runs wait while other work on the machine needs memory."),
    records.PressureUnavailable: WaitWords("1 run waits because the queue cannot check whether other work needs memory.",
        "{count} runs wait because the queue cannot check whether other work needs memory."),
    records.StartSpacingWait: WaitWords("1 run waits briefly because another step just started.",
        "{count} runs wait briefly because another step just started."),
    records.BuildSpaceUnreadable: WaitWords("1 run waits because the queue cannot check how much space is free on the build disk.",
        "{count} runs wait because the queue cannot check how much space is free on the build disk."),
    records.BuildSpaceLow: WaitWords("1 run waits because the build disk has less free space than the queue requires.",
        "{count} runs wait because the build disk has less free space than the queue requires."),
    placement.CargoFolderWait: WaitWords("1 run waits for another build to release the same build folder.",
        "{count} runs wait for another build to release the same build folder."),
    records.NeedsMemory: WaitWords("1 run needs more free memory to start.", "{count} runs need more free memory to start."),
    records.GpuSlotWait: WaitWords("1 run waits because all GPU test places are in use.",
        "{count} runs wait because all GPU test places are in use."),
    records.RunLimitWait: WaitWords("1 run waits because the run limit for {repository} on {machine} is full.",
        "{count} runs wait because the run limit for {repository} on {machine} is full."),
    "run_limit_full": WaitWords("1 run waits because the repository run limit is full.",
        "{count} runs wait because the repository run limit is full."),
    records.BehindHead: WaitWords("1 run waits behind an earlier step in the queue.",
        "{count} runs wait behind an earlier step in the queue."),
    "remote_start": WaitWords("1 run waits while a step starts on another machine.",
        "{count} runs wait while a step starts on another machine."),
    records.ReadyForAdmission: WaitWords("1 run can start at the queue's next check.",
        "{count} runs can start at the queue's next check."),
    wire.CloudSlotWait: WaitWords("1 run waits for a place on AWS.", "{count} runs wait for a place on AWS."),
    ReservedCloudSlot: WaitWords("1 run has a place on AWS and waits for its machine to start.",
        "{count} runs have a place on AWS and wait for their machines to start."),
    "wait": WaitWords("1 run waits.", "{count} runs wait."),
}
PAGE_WORDS = {key: text for key, text in WORDS.items() if isinstance(key, str) and isinstance(text, str)}
WAIT_WORDS = {key: text for key, text in WORDS.items() if isinstance(text, WaitWords)}

HISTORY_SECONDS = 30 * 24 * 60 * 60


class ReadingState(Enum):
    OMITTED = "omitted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class WaitGroup:
    words: WaitWords
    names: list[str] = field(default_factory=list[str])


type WaitGroups = dict[str, WaitGroup]


@dataclass(frozen=True)
class Metric:
    label: str
    used: float | None
    limit: float | None
    unit: Literal["", "GiB", "USD"] = ""
    qualifier: str = ""
    waits: Mapping[str, WaitGroup] = field(default_factory=dict[str, WaitGroup])
    unreadable: bool = False
    reserved_waiting: int = 0
    exempt_runs: int = 0


class CellPolicy(StrEnum):
    UNRESOLVED = "unresolved"
    CONFIGURED = "configured"
    NO_LIMIT = "no_limit"


@dataclass
class Cell:
    limit: int | None = None
    used: int | None = None
    other_used: int = 0
    policy: CellPolicy = CellPolicy.UNRESOLVED
    runs: list[str] = field(default_factory=list[str])
    merge_above: list[str] = field(default_factory=list[str])
    other: list[str] = field(default_factory=list[str])
    waits: WaitGroups = field(default_factory=dict[str, WaitGroup])
    note: str = ""


@dataclass
class RepositoryRow:
    name: str
    quiet: bool = True
    natedev: Cell = field(default_factory=Cell)
    aws: Cell = field(default_factory=Cell)


@dataclass(frozen=True)
class MachineLimits:
    state: str
    shared: tuple[Metric, ...]
    waits: WaitGroups
    unattributed: int = 0


@dataclass(frozen=True)
class BuildLimits:
    natedev: MachineLimits
    aws: MachineLimits
    repositories: tuple[RepositoryRow, ...]
    history_unavailable: bool
    unplaced: WaitGroups
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


def policy_label(key: str) -> str:
    """Show the key when it is a name or a public origin."""
    if ":" not in key and key != store.UNKNOWN_IDENTITY:
        return clean_label(key)
    return recorded_origin(key).removeprefix("https://")


def wait_words(reason: records.WaitReason | wire.CloudSlotWait) -> WaitWords:
    if isinstance(reason, records.HeadAdmissionStatus):
        reason = reason.cause
    if isinstance(reason, records.RunLimitWait):
        name = policy_label(reason.repository)
        machine = clean_label(reason.machine)
        if name and machine:
            words = WAIT_WORDS[records.RunLimitWait]
            return WaitWords(words.one.format(repository=name, machine=machine),
                             words.many.format(repository=name, machine=machine, count="{count}"))
        return WAIT_WORDS["run_limit_full"]
    if isinstance(reason, records.BehindHead) and reason.head_name == "remote startup":
        return WAIT_WORDS["remote_start"]
    if isinstance(reason, wire.CloudSlotWait) and isinstance(reason.state, ReservedCloudSlot):
        return WAIT_WORDS[ReservedCloudSlot]
    return WAIT_WORDS.get(type(reason), WAIT_WORDS["wait"])


def add_wait(groups: WaitGroups, words: WaitWords, name: str, *, policy_index: int = 0) -> None:
    heading_key = f"{policy_index}:{words.one}" if policy_index else words.one
    groups.setdefault(heading_key, WaitGroup(words)).names.append(name)


def local_state(snapshots: target_availability.Snapshots, machine: str, active: bool, zone: tzinfo) -> str:
    if isinstance(snapshots, tuple):
        for snapshot in snapshots:
            if snapshot.target.name != machine or not isinstance(snapshot.target, target_config.LocalTarget):
                continue
            for state in snapshot.states:
                if isinstance(state, targets.Held):
                    if isinstance(state.end, (targets.ExpiresAt, targets.ReleaseEstimated)):
                        return PAGE_WORDS["held_until"].format(time=targets.clock(state.end.when, zone))
                    return PAGE_WORDS["held"]
                if isinstance(state, targets.Unreachable):
                    return PAGE_WORDS["unreachable"]
                if isinstance(state, targets.StateUnreadable):
                    return PAGE_WORDS["not_available"]
            if active or any(isinstance(state, targets.Running) for state in snapshot.states):
                return PAGE_WORDS["in_use"]
            return PAGE_WORDS["free"] if any(isinstance(state, targets.Free) for state in snapshot.states) else ""
    if active:
        return PAGE_WORDS["in_use"]
    return PAGE_WORDS["not_available"] if isinstance(snapshots, target_availability.SnapshotsUnreadable) else ""


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
    local_waits: WaitGroups = {}
    aws_waits: WaitGroups = {}
    unplaced: WaitGroups = {}
    metric_waits: dict[str, WaitGroups] = {}
    policy_rows: dict[str, set[str]] = {}
    run_labels: dict[tuple[str, str], tuple[str, str]] = {}
    mac_targets = (frozenset(snapshot.target.name for snapshot in snapshots if snapshot.target.platform == "macos")
                   if isinstance(snapshots, tuple) else frozenset[str]())

    def row(name: str) -> RepositoryRow:
        if name not in rows:
            rows[name] = RepositoryRow(name)
        return rows[name]

    def record_local_policy(name: str, origin: str, repository_name: str = "") -> None:
        cell = row(name).natedev
        if name in local_limits or origin in local_limits or repository_name in local_limits:
            cell.policy = CellPolicy.CONFIGURED
        elif cell.policy is CellPolicy.UNRESOLVED:
            cell.policy = CellPolicy.NO_LIMIT

    def remote_wait(machine: str) -> WaitGroups:
        return aws_waits if machine in saved.aws_targets else unplaced

    for origin in history:
        if name := short_name(origin):
            record_local_policy(name, origin)

    occupancy = reserved_repository_runs(inputs, policy)
    seen = {owner for owners in occupancy.values() for owner in owners}
    limited_owners = frozenset(seen)
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
        name = short_name(origin)
        recorded_unit = reservation.get("unit", "") or (waiter.get("unit", "") if waiter is not None else "")
        session = waiter.get("session_id", "") if waiter is not None else reservation.get("session_id", "")
        session_name = waiter.get("session_name", "") if waiter is not None else reservation.get("session_name", "")
        repository_name = waiter.get("repository_name", reservation["repo"]) if waiter is not None else reservation["repo"]
        if name:
            record_local_policy(name, origin, repository_name)
        if repository_name in local_limits:
            policy_rows.setdefault(repository_name, set()).add(name)
        if owner in limited_owners:
            if policy.for_step(local, repository_name, reservation["step"]):
                _ = run_labels.setdefault((repository_name, owner),
                    (clean_label(recorded_unit, names.get(session, ""), session_name), recorded_unit))
            continue
        if owner in seen:
            continue
        seen.add(owner)
        if not name:
            unattributed += 1
            continue
        cell = row(name).natedev
        cell.other_used += 1
        if label := clean_label(recorded_unit, names.get(session, ""), session_name, name):
            cell.other.append(label)

    bindings = Counter(name for bound in policy_rows.values() for name in bound if name)
    bound_rows = {key: name for key, bound in policy_rows.items() if len(bound) == 1 for name in bound if name}
    unresolved_policy = any("" in bound for bound in policy_rows.values())
    repository_wait_counts: dict[str, set[tuple[int, int]]] = {}
    policy_wait_indices: dict[str, int] = {}
    gpu_wait_counts: set[tuple[int, int]] = set()
    for visible in waits:
        entry = visible.source
        if isinstance(entry, wire.CloudRow):
            if isinstance(entry.wait, wire.CloudSlotWait):
                add_wait(metric_waits.setdefault("cloud", {}), wait_words(entry.wait), visible.name)
            continue
        if isinstance(entry, wire.RepositorySlotRow):
            if entry.machine not in mac_targets:
                destination = local_waits if entry.machine == local else remote_wait(entry.machine)
                add_wait(destination, WAIT_WORDS["wait"], visible.name)
            continue
        step = entry.step
        pinned = step.route.target if isinstance(step.route, target_config.PinnedTarget) else local
        if isinstance(step.route, target_config.MacOnly) or pinned in mac_targets:
            continue
        reason = step.reason.cause if isinstance(step.reason, records.HeadAdmissionStatus) else step.reason
        if pinned != local:
            destination = remote_wait(pinned)
        elif isinstance(reason, records.RunLimitWait) and reason.machine == local:
            repository_wait_counts.setdefault(reason.repository, set()).add((reason.used, reason.limit))
            name = bound_rows.get(reason.repository, "")
            destination = row(name).natedev.waits if name else local_waits
            if reason.repository in local_limits and step.folder and not origins.resolve("", step.folder):
                unresolved_policy = True
        elif isinstance(reason, (records.BuildSpaceLow, records.BuildSpaceUnreadable)):
            destination = metric_waits.setdefault("disk", {})
        elif isinstance(reason, records.GpuSlotWait):
            gpu_wait_counts.add((reason.used, reason.slots))
            destination = metric_waits.setdefault("gpu", {})
        elif isinstance(reason, (records.NeedsMemory, records.MemoryPressure, records.PressureUnavailable)):
            destination = metric_waits.setdefault("memory", {})
        else:
            destination = local_waits
        # Keep each policy group separate without publishing private keys.
        policy_index = (policy_wait_indices.setdefault(reason.repository, len(policy_wait_indices) + 1)
                        if isinstance(reason, records.RunLimitWait) else 0)
        add_wait(destination, wait_words(reason), visible.name, policy_index=policy_index)

    shared: list[Metric] = []
    for repository_key, limit in local_limits.items():
        owners = occupancy.get(repository_key, set[str]())
        used = len(owners)
        wait_counts = repository_wait_counts.get(repository_key)
        if wait_counts and wait_counts != {(used, limit)}:
            continue
        runs: list[str] = []
        merge_above: list[str] = []
        for owner in sorted(owners):
            label, recorded_unit = run_labels.get((repository_key, owner), ("", ""))
            if used > limit and recorded_unit and recorded_unit == policy.exempt_unit:
                merge_above.append(label)
            elif label:
                runs.append(label)
        if used - limit > len(merge_above):
            continue
        name = bound_rows.get(repository_key, "")
        if not name or bindings[name] > 1:
            key_label = policy_label(repository_key)
            label = PAGE_WORDS["policy_key"].format(name=key_label) if key_label else PAGE_WORDS["repository_run_limit"]
            shared.append(Metric(label, used, limit, exempt_runs=len(merge_above)))
            continue
        cell = row(name).natedev
        cell.limit, cell.used = limit, used
        cell.runs, cell.merge_above = runs, merge_above

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
            if name := short_name(origin):
                cell = row(name).aws
                cell.policy = CellPolicy.NO_LIMIT
                cell.other_used += 1
                label = clean_label(cloud_names.get(cloud.owner, ""), cloud.identity.unit, name)
                if label:
                    cell.other.append(label)
                cell.note = PAGE_WORDS["uses_cloud"]

    # Every Hana target uses the same AWS lifecycle controller.
    hana_by_name: dict[str, set[HanaCapacity]] = {}
    for capacity in saved.hana:
        if name := short_name(recorded_origin(capacity.repository)):
            hana_by_name.setdefault(name, set()).add(capacity)
    for name, capacities in hana_by_name.items():
        cell = row(name).aws
        if len(capacities) != 1:
            cell.policy = CellPolicy.UNRESOLVED
            continue
        capacity = next(iter(capacities))
        cell.policy = CellPolicy.CONFIGURED
        cell.limit = capacity.limit
        cell.used = capacity.used if isinstance(capacity.used, int) else None
        if capacity.used is ReadingState.UNAVAILABLE:
            cell.note = PAGE_WORDS["not_available"]

    for name, item in rows.items():
        if item.natedev.policy is CellPolicy.NO_LIMIT and unresolved_policy:
            item.natedev.policy = CellPolicy.UNRESOLVED
        for cell, has_limited_runs in ((item.natedev, name in bindings), (item.aws, False)):
            if cell.limit is None and not has_limited_runs and (cell is item.natedev or cell.other_used):
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
        gpu_used = sum(gpu_test_reservation(item, gpu) for item in inputs.reservations)
        counts_agree = not gpu_wait_counts or gpu_wait_counts == {(gpu_used, gpu.slots)}
        gpu_readable = bool(gpu.repositories) and counts_agree
        limits.append(Metric("gpu", gpu_used if gpu_readable else None, gpu.slots if gpu_readable else None,
                             waits=metric_waits.get("gpu", {})))
    disk = inputs.build_space
    disk_waits = metric_waits.get("disk", {})
    if isinstance(disk, records.BuildSpaceLow):
        limits.append(Metric("disk", disk.free_gib, disk.floor_gib, "GiB", "minimum", disk_waits))
    elif disk_waits:
        limits.append(Metric("disk", None, None, waits=disk_waits))
    aws_active = cloud_used or any(isinstance(hana.used, int) and hana.used for hana in saved.hana)
    aws_state = PAGE_WORDS["in_use"] if aws_active else PAGE_WORDS["not_available"] if saved.aws_routes_unavailable else ""
    cloud = Metric("cloud", cloud_used if isinstance(cloud_slots, int) else None,
                   cloud_slots if isinstance(cloud_slots, int) else None, waits=metric_waits.get("cloud", {}),
                   unreadable=cloud_slots is ReadingState.UNAVAILABLE, reserved_waiting=cloud_waiting)
    return BuildLimits(MachineLimits(local_state(snapshots, local, bool(inputs.reservations), zone),
                                    (*limits, *shared), local_waits, unattributed),
                       MachineLimits(aws_state, (cloud, saved.aws_money), aws_waits),
                       tuple(sorted(rows.values(), key=lambda item: (item.quiet, item.name.casefold()))),
                       saved.history is ReadingState.UNAVAILABLE, unplaced)
