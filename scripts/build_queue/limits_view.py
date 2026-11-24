"""Read the published records for the Build limits section."""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
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
from scripts.build_queue.queue_wait import public_queue_text
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport, HanaGpuNextestTransport
from scripts.buildlog import charts, machine_facts, repository, store
from scripts.lib import conf, git_files
from scripts.lint.memory_admit import gpu_test_reservation, repository_run_owner, reserved_repository_runs
from scripts.mac_test import claim_state, eligibility, mac_test

@dataclass(frozen=True)
class WaitWords:
    one: str
    many: str


WORDS: Mapping[str | type[object], str | WaitWords] = {
    "title": "Build limits",
    "table": "Build limits by repository and machine",
    "repository": "Repository",
    "local": "natedev",
    "mac": "Mac",
    "aws": "AWS",
    "not_available": "Not available",
    "count": "{used} of {limit}",
    "limit": "Limit {limit}",
    "limit_set": "Limit set",
    "no_limit": "No limit",
    "running": "{count} running",
    "occupied": "{count} in use",
    "kept": "{count} kept (leftover of a stopped run)",
    "kept_many": "{count} kept (leftovers of stopped runs)",
    "other_running": "{count} other running",
    "merge_above": "A merge run above the limit",
    "policy_key": "{name} run limit",
    "repository_run_limit": "Repository run limit",
    "uses_cloud": "Uses the AWS places",
    "memory": "Memory for builds",
    "gpu": "GPU tests",
    "gpu_limit": "The GPU limit applies to tests for this repository on natedev.",
    "admission_restart": "The admission service needs a restart.",
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
    "on": "On",
    "held": "Held",
    "held_until": "Held until {time}",
    "unreachable": "Unreachable",
    "legend_run": "A run that is in progress",
    "legend_kept": "A place kept by a stopped run",
    "legend_free": "A free place",
    "legend_line": "The limit",
    "legend_wait": "A run that waits",
    "facts_link": "link {ms} ms",
    "facts_load": "load {load}/{cpus}",
    "facts_load_unreadable": "load unreadable",
    "facts_battery_full": "battery 100% charged · not in use",
    "facts_battery_mains": "battery {percent}% · not in use",
    "facts_battery_in_use": "battery {percent}% · in use",
    "facts_battery_unreadable": "battery unreadable",
    "facts_unreachable": "link unreachable",
    "facts_no_box": "no box running",
    "facts_box_running": "box running · no link or load reading",
    "facts_failed": "facts unreadable",
    "facts_missing": "facts not sampled",
    "facts_stale": "facts not sampled since {time}",
    records.TokenWaitingWaiter: WaitWords("1 run waits for permission to continue its Cargo work.",
        "{count} runs wait for permission to continue their Cargo work."),
    records.EstimateExpired: WaitWords("1 run waits because the queue needs a new memory estimate before it can start.",
        "{count} runs wait because the queue needs new memory estimates before they can start."),
    records.ReadingUnavailable: WaitWords("1 run waits because the queue cannot check whether it can start.",
        "{count} runs wait because the queue cannot check whether they can start."),
    records.MachineChoiceWait: WaitWords("1 run waits for a machine choice.", "{count} runs wait for a machine choice."),
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
    kept: int = 0
    policy: CellPolicy = CellPolicy.UNRESOLVED
    runs: list[str] = field(default_factory=list[str])
    merge_above: list[str] = field(default_factory=list[str])
    other: list[str] = field(default_factory=list[str])
    waits: WaitGroups = field(default_factory=dict[str, WaitGroup])
    note: str = ""
    show_marks: bool = True
    verify_used: int = 0
    gpu: Metric | None = None


@dataclass
class RepositoryRow:
    name: str
    quiet: bool = True
    natedev: Cell = field(default_factory=Cell)
    mac: Cell = field(default_factory=Cell)
    aws: Cell = field(default_factory=Cell)


@dataclass(frozen=True)
class MachineLimits:
    state: str
    shared: tuple[Metric, ...]
    waits: WaitGroups
    unattributed: int = 0
    unattributed_note: str = ""
    facts: str = ""
    unattributed_kept: int = 0


@dataclass(frozen=True)
class BuildLimits:
    natedev: MachineLimits
    mac: MachineLimits
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
    source_names: Mapping[str, set[str]] = field(default_factory=dict[str, set[str]])
    mac_scopes: Mapping[str, str] = field(default_factory=dict[str, str])

    def resolve(self, identity: str, folder: str) -> str:
        """Use the recorded origin or the checkout's own origin."""
        return recorded_origin(identity) or self.resolved.get(identity) or self.resolved.get(folder, "")

    def refresh(self, folders: Iterable[str], *, mac_folders: Iterable[str] = ()) -> Origins:
        """Read each checkout config and reuse values for unchanged files."""
        found: dict[Path, OriginReading] = {}
        resolved: dict[str, str] = {}
        source_names: dict[str, set[str]] = {}
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
                    if entry.remote:
                        source_names.setdefault(entry.remote, set()).add(entry.config.parent.parent.name)
                    break
                path = entry.remote
        mac_scopes = {folder: name for folder in mac_folders
                      if Path(folder).is_absolute() and (name := repository.repo_name(folder))}
        return Origins(found, resolved, source_names, mac_scopes)


@dataclass(frozen=True)
class CheckoutUse:
    folder: str
    owner: wire.Owner
    reservation: str = ""


@dataclass(frozen=True)
class WaitingCheckout:
    folder: str
    pid: int
    since: float
    may_wait_on_mac: bool = False


def waiting_cause(step: records.WaitingStep) -> records.WaitingStepReason:
    return step.reason.cause if isinstance(step.reason, records.HeadAdmissionStatus) else step.reason


def active_checkouts(reading: records.QueueRead) -> frozenset[CheckoutUse | WaitingCheckout]:
    occupancy = reading.explanation.local_occupancy
    reservations = occupancy.reservations if occupancy is not None else reading.explanation.decided_from.reservations
    checkouts: set[CheckoutUse | WaitingCheckout] = {
        CheckoutUse(item["worktree"], wire.Owner(item["pid"], item["start_time"]),
                    item.get("reservation_id", item["admitted_at"]))
        for item in reservations if item["worktree"]
    }
    checkouts.update(WaitingCheckout(step.folder, step.pid, step.since,
                         isinstance(step.route, (target_config.MacOnly, target_config.PinnedTarget)))
                     for step in reading.view.waiting if step.folder)
    if isinstance(reading, wire.CloudQueueRead):
        checkouts.update(CheckoutUse(row.identity.folder, row.owner) for row in reading.cloud if row.identity.folder)
    return frozenset(checkouts)


def refresh_queue_origins(origins: Origins, checkouts: frozenset[CheckoutUse | WaitingCheckout]) -> Origins:
    return origins.refresh({use.folder for use in checkouts},
        mac_folders={use.folder for use in checkouts if isinstance(use, WaitingCheckout) and use.may_wait_on_mac})


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
    hana: tuple[HanaCapacity, ...] | ReadingState = ReadingState.OMITTED
    aws_targets: frozenset[str] = field(default_factory=frozenset[str])
    origins: Origins = field(default_factory=Origins)


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
    hana: tuple[HanaRoute, ...] | Literal[ReadingState.UNAVAILABLE] = ()
    targets: frozenset[str] = field(default_factory=frozenset[str])


def aws_routes() -> AwsRoutes | ReadingState:
    try:
        settings = conf.read("build_targets")
        targets_read = target_config.read_targets(configuration=settings, purpose=target_config.RegistryRead.PLACEMENT)
        pinned = settings.value("pinned_targets", str, default="", allow_blank=True)
    except (OSError, ValueError):
        return ReadingState.UNAVAILABLE
    configured = {target.name: target for target in targets_read}
    route_read_failed = False
    for name in filter(None, (value.strip() for value in pinned.split(","))):
        if name not in configured:
            try:
                configured.update((target.name, target) for target in target_config.read_targets(
                    selected=name, configuration=settings, purpose=target_config.RegistryRead.PLACEMENT))
            except (OSError, ValueError):
                route_read_failed = True
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
            route_read_failed = True
    return AwsRoutes(ReadingState.UNAVAILABLE if route_read_failed else tuple(hana), frozenset(aws))


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
    hana_routes = routes if isinstance(routes, ReadingState) else routes.hana
    hana = (hana_routes if isinstance(hana_routes, ReadingState) else
            tuple(HanaCapacity(route.settings.repository_key, route.settings.max_steps,
                               occupancy if isinstance(occupancy, ReadingState)
                               else occupancy.get(route, ReadingState.OMITTED)) for route in hana_routes))
    spend_metric = Metric("money", None, None, "USD", unreadable=money is ReadingState.UNAVAILABLE)
    return Background(history, spend_metric if isinstance(money, ReadingState) else money,
                      cloud_slots, hana, aws.targets,
                      Origins() if isinstance(origins, ReadingState) else origins)


def background_once(now: float, reading: records.QueueRead) -> Background:
    """Read the same sources for a one-time page."""
    routes = safe_read(aws_routes)
    hana_routes = routes.hana if isinstance(routes, AwsRoutes) else routes
    occupancy = (safe_read(lambda: read_hana_occupancy(hana_routes))
                 if isinstance(hana_routes, tuple) else ReadingState.OMITTED)
    return background(safe_read(lambda: read_history(now)), safe_read(lambda: spending(now)),
                      safe_read(read_cloud_slots), routes, occupancy,
                      safe_read(lambda: refresh_queue_origins(Origins(), active_checkouts(reading))))


type RefreshKey = date | frozenset[CheckoutUse | WaitingCheckout] | tuple[HanaRoute, ...] | targets.TargetSnapshot


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
            return refresh_queue_origins(cache, checkouts)

        day = datetime.fromtimestamp(wall, UTC).date()
        routes = self.routes.current(now, self.token_interval, aws_routes)
        hana_routes = routes.hana if isinstance(routes, AwsRoutes) else routes
        occupancy = (self.hana_occupancy.current(now, self.token_interval,
                      lambda: read_hana_occupancy(hana_routes), key=hana_routes)
                     if isinstance(hana_routes, tuple) and hana_routes else ReadingState.OMITTED)
        return background(
            self.history.current(now, self.report_interval, lambda: read_history(wall)),
            self.money.current(now, self.report_interval, lambda: spending(wall), key=day),
            self.cloud_slots.current(now, self.report_interval, read_cloud_slots),
            routes, occupancy,
            self.origins.current(now, self.token_interval, read_origins, key=checkouts))


class MacClaimUse(Enum):
    """A working claim holds a live run; a kept claim holds the leftovers of a stopped run."""
    WORKING = "working"
    KEPT = "kept"


@dataclass(frozen=True)
class MacClaim:
    scope: claim_state.ClaimScope
    limit: int
    working_session: str
    use: MacClaimUse = MacClaimUse.WORKING


@dataclass(frozen=True)
class MacHeading:
    """The Mac claims without a repository row.

    Each one counts in the limit. `kept` counts the kept claims in `unattributed`.
    """
    unattributed: int = 0
    kept: int = 0
    note: str = ""


@dataclass(frozen=True)
class MacReading:
    claims: tuple[MacClaim, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict[str, int])
    claims_readable: bool = False


def mac_snapshot(snapshots: target_availability.Snapshots) -> targets.TargetSnapshot | None:
    if isinstance(snapshots, tuple):
        return next((snapshot for snapshot in snapshots
                     if isinstance(snapshot.target, target_config.SshTarget) and snapshot.target.platform == "macos"), None)
    return None


def read_mac_origins(now: float, earlier: Origins) -> Origins | ReadingState:
    """Find checkout names for the Mac policy from recent build steps."""
    def read_paths(connection: sqlite3.Connection) -> tuple[str, ...]:
        rows = cast(Iterable[tuple[object]], connection.execute(
            """SELECT DISTINCT repo_path FROM steps WHERE started_at >= ? AND started_at <= ?""",
            (store.utc_iso(now - HISTORY_SECONDS), store.utc_iso(now))))
        return tuple(path for path, in rows if isinstance(path, str) and path.startswith("/"))

    paths = charts.read_index(read_paths)
    return ReadingState.UNAVAILABLE if isinstance(paths, charts.IndexUnavailable) else earlier.refresh(paths)


def read_mac(snapshot: targets.TargetSnapshot, origins: Origins) -> MacReading:
    """Read claim and policy facts for one configured Mac."""
    target = snapshot.target
    assert isinstance(target, target_config.SshTarget)
    state = snapshot.states[0]
    paths = claim_state.StatePaths(target_config.ssh_target_state_directory(target.name), target.name,
                                  registry_selected=True, platform=target.platform)
    recorded = snapshot.claims
    if not recorded and isinstance(state, targets.StateUnreadable):
        # Read claims separately when the snapshot cannot identify the failed read.
        try:
            recorded = claim_state.read_claims(paths)
        except (OSError, UnicodeError, claim_state.StateReadError):
            return MacReading()
    live: dict[claim_state.ClaimIdentity, claim_state.HeldClaim] = {}
    for claim in recorded:
        identity = claim_state.claim_identity(claim.record)
        if identity not in live and mac_test.run_is_live(claim.record):
            live[identity] = claim
    claims: list[MacClaim] = []
    for claim in live.values():
        record = claim.record
        scope = claim.scope
        if isinstance(scope, claim_state.RepositoryScope):
            # The Mac admits a checkout under a scope that uses only its name.
            scope = claim_state.RepositoryScope(scope.name)
        # Each claim here is live, so a claim that is not kept holds work.
        kept = mac_test.run_is_kept(record)
        session = record["submitting_session"] if not record.get("cleanup_pending") and not kept else ""
        use = MacClaimUse.KEPT if kept else MacClaimUse.WORKING
        claims.append(MacClaim(scope, mac_test.slot_capacity(paths, scope), session, use))
    configured: dict[str, int] = {}
    if origins.source_names:
        settings = conf.read(target.settings)
        for name in {name for names in origins.source_names.values() for name in names}:
            try:
                policy = eligibility.linux_only_policy(settings, eligibility.KnownRepository(name), Path(), "", "check")
                if isinstance(policy, eligibility.KnownRepository):
                    configured[name] = mac_test.slot_capacity(paths, claim_state.RepositoryScope(name), configuration=settings)
            except conf.Unusable:
                continue
    return MacReading(tuple(claims), configured, claims_readable=True)


@final
class MacRefresh:
    def __init__(self, report_interval: float, feed_interval: float) -> None:
        self.report_interval = report_interval
        self.feed_interval = feed_interval
        self.origins = SourceCache[Origins]()
        self.facts = SourceCache[MacReading]()

    def current(self, snapshots: target_availability.Snapshots, now: float, wall: float,
                *, once: bool) -> MacReading | None:
        snapshot = mac_snapshot(snapshots)
        if snapshot is None:
            return None

        def read_origins() -> Origins | ReadingState:
            earlier = self.origins.reusable()
            return read_mac_origins(wall, earlier if isinstance(earlier, Origins) else Origins())

        origins = safe_read(read_origins) if once else self.origins.current(now, self.report_interval, read_origins)

        def read_facts() -> MacReading:
            return read_mac(snapshot, origins if isinstance(origins, Origins) else Origins())

        facts = safe_read(read_facts) if once else self.facts.current(now, self.feed_interval, read_facts, key=snapshot)
        return facts if isinstance(facts, MacReading) else None


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
        else:
            name = cloud_names.get(entry.owner, "")
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


def wait_words(reason: records.WaitingStepReason | wire.CloudSlotWait) -> WaitWords:
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


@dataclass(frozen=True)
class LocalPolicyWait:
    reason: records.RunLimitWait
    repository: str
    name: str


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


def mac_state(snapshots: target_availability.Snapshots, zone: tzinfo) -> str:
    if isinstance(snapshots, target_availability.SnapshotsUnreadable):
        return PAGE_WORDS["not_available"]
    snapshot = mac_snapshot(snapshots)
    if snapshot is None:
        return ""
    state = snapshot.states[0]
    if isinstance(state, targets.Held):
        if isinstance(state.end, targets.ExpiresAt):
            return PAGE_WORDS["held_until"].format(time=targets.clock(state.end.when, zone))
        return PAGE_WORDS["held"]
    if isinstance(state, targets.StateUnreadable):
        return PAGE_WORDS["not_available"]
    return PAGE_WORDS["on"] if isinstance(state, (targets.Free, targets.Running)) else ""


def mac_row_name(scope: str) -> str:
    return "claude_commands" if scope == ".claude" else clean_label(scope)


def kept_count(claims: Sequence[MacClaim]) -> int:
    return sum(1 for claim in claims if claim.use is MacClaimUse.KEPT)


def place_mac_counts(mac: MacReading, rows: dict[str, RepositoryRow], names: Mapping[str, str]) -> MacHeading:
    """Put each confirmed claim in its repository row or in the heading."""
    by_row: dict[str, list[MacClaim]] = {}
    unattributed: list[MacClaim] = []
    show_free = mac.claims_readable
    missing_scope = False
    for claim in mac.claims:
        if isinstance(claim.scope, claim_state.RepositoryScope):
            name = mac_row_name(claim.scope.name)
            if not name:
                unattributed.append(claim)
                show_free = False
                missing_scope = True
                continue
            by_row.setdefault(name, []).append(claim)
        else:
            unattributed.append(claim)
            show_free = False

    limits_by_row: dict[str, dict[str, int]] = {}
    for scope, limit in mac.limits.items():
        if name := mac_row_name(scope):
            limits_by_row.setdefault(name, {})[scope] = limit

    for name, claims in by_row.items():
        if name not in rows:
            rows[name] = RepositoryRow(name)
        row = rows[name]
        row.quiet = False
        row.mac.used = len(claims)
        row.mac.kept = kept_count(claims)
        row.mac.runs.extend(label for claim in claims
                            if claim.working_session and (label := clean_label(names.get(claim.working_session, ""))))
        first = claims[0]
        if (show_free and isinstance(first.scope, claim_state.RepositoryScope)
                and not (limits_by_row.get(name, {}).keys() - {first.scope.name}) and len(claims) <= first.limit
                and all(claim.scope == first.scope and claim.limit == first.limit for claim in claims)):
            row.mac.limit = first.limit
            row.mac.policy = CellPolicy.CONFIGURED

    if show_free:
        for name, scopes in limits_by_row.items():
            if name in rows and name not in by_row and len(scopes) == 1:
                limit = next(iter(scopes.values()))
                cell = rows[name].mac
                cell.used, cell.limit, cell.policy = 0, limit, CellPolicy.CONFIGURED
    return MacHeading(len(unattributed), kept_count(unattributed),
                      "A Mac claim has no repository name that this page can show." if missing_scope else "")


def facts_line(saved: machine_facts.SavedFacts | machine_facts.FactsUnread, machine: machine_facts.Machine, now: float,
               zone: tzinfo) -> str:
    """One line of link delay, load and battery under a machine heading."""
    if isinstance(saved, machine_facts.FactsUnread) or machine not in saved["machines"]:
        return PAGE_WORDS["facts_missing"]
    if now - saved["written"] > machine_facts.STALE_SECONDS:
        return PAGE_WORDS["facts_stale"].format(time=datetime.fromtimestamp(saved["written"], zone).strftime("%H:%M"))
    facts = saved["machines"][machine]
    state = facts["state"]
    if state != "read" and state != "load_unreadable":
        return PAGE_WORDS["facts_" + state]
    parts: list[str] = []
    link_ms = facts["link_ms"]
    if link_ms is not None:
        parts.append(PAGE_WORDS["facts_link"].format(ms="<1" if link_ms < 1 else f"{link_ms:.0f}"))
    load, cpus = facts["load"], facts["cpus"]
    parts.append(PAGE_WORDS["facts_load"].format(load=f"{load:.1f}", cpus=cpus)
                 if state == "read" and load is not None and cpus is not None else PAGE_WORDS["facts_load_unreadable"])
    battery = facts["battery"]
    if battery == "unreadable":
        parts.append(PAGE_WORDS["facts_battery_unreadable"])
    elif battery != "no_battery":
        # On mains the machine does not use its battery; a full charge says so.
        word = ("facts_battery_in_use" if not battery["mains"]
                else "facts_battery_full" if battery["percent"] >= 100 else "facts_battery_mains")
        parts.append(PAGE_WORDS[word].format(percent=battery["percent"]))
    return " · ".join(parts)


def build_limits(reading: records.QueueRead, snapshots: target_availability.Snapshots,
                 waits: Sequence[VisibleWait], cloud_names: Mapping[wire.Owner, str],
                 names: Mapping[str, str], saved: Background, zone: tzinfo, *,
                 mac: MacReading | None = None,
                 facts: machine_facts.SavedFacts | machine_facts.FactsUnread = machine_facts.FactsUnread.VALUE) -> BuildLimits:
    explanation = reading.explanation
    occupancy = explanation.local_occupancy
    inputs = replace(explanation.decided_from,
        reservations=occupancy.reservations if occupancy is not None else (),
        verify_run_owners=occupancy.verify_run_owners if occupancy is not None else {})
    local_note = PAGE_WORDS["admission_restart"] if occupancy is None else ""
    policy = explanation.policy.repository_runs
    local = inputs.local_machine
    prefix = local + "."
    local_limits = {key.removeprefix(prefix): limit for key, limit in policy.by_machine_and_repository.items()
                    if key.startswith(prefix)}
    origins = saved.origins
    history = saved.history if isinstance(saved.history, frozenset) else ()
    rows: dict[str, RepositoryRow] = {}
    local_waits: WaitGroups = {}
    mac_waits: WaitGroups = {}
    aws_waits: WaitGroups = {}
    unplaced: WaitGroups = {}
    metric_waits: dict[str, WaitGroups] = {}
    keys_by_row: dict[str, set[str]] = {}
    unresolved_policy = False
    run_labels: dict[tuple[str, str], tuple[str, str]] = {}
    gpu = explanation.policy.gpu_tests
    gpu_used = sum(gpu_test_reservation(item, gpu) for item in inputs.reservations)
    mac_targets = {"mac"}
    if isinstance(snapshots, tuple):
        mac_targets.update(snapshot.target.name for snapshot in snapshots if snapshot.target.platform == "macos")

    def row(name: str) -> RepositoryRow:
        if name not in rows:
            rows[name] = RepositoryRow(name)
        return rows[name]

    def observe_local(origin: str, repository_key: str = "") -> str:
        nonlocal unresolved_policy
        name = short_name(origin)
        if name:
            keys = keys_by_row.setdefault(name, set())
            if repository_key in local_limits:
                keys.add(repository_key)
            _ = row(name)
        elif repository_key in local_limits:
            # The checkout can match any row, even one with a known key.
            unresolved_policy = True
        return name

    def remote_wait(machine: str) -> WaitGroups:
        return aws_waits if machine in saved.aws_targets else unplaced

    for key in local_limits:
        origin = recorded_origin(key)
        name = short_name(origin) if origin else policy_label(key)
        if name:
            keys_by_row.setdefault(name, set()).add(key)
            if inputs.verify_run_owners.get(key):
                _ = row(name)

    for origin in history:
        _ = observe_local(origin)

    repository_runs = reserved_repository_runs(inputs, policy)
    seen = {owner for key in local_limits for owner in repository_runs.get(key, ())}
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
        recorded_unit = reservation.get("unit", "") or (waiter.get("unit", "") if waiter is not None else "")
        session = waiter.get("session_id", "") if waiter is not None else reservation.get("session_id", "")
        session_name = waiter.get("session_name", "") if waiter is not None else reservation.get("session_name", "")
        repository_name = waiter.get("repository_name", reservation["repo"]) if waiter is not None else reservation["repo"]
        name = observe_local(origin, repository_name)
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

    policy_wait_indices: dict[str, int] = {}
    local_policy_waits: list[LocalPolicyWait] = []
    waiting_origins = {(waiter["pid"], waiter["since"]): origin for _, waiter in reading.ordered
                       if (origin := recorded_origin(waiter.get("repo_identity", "")))}

    def record_wait(destination: WaitGroups, reason: records.WaitingStepReason | wire.CloudSlotWait, name: str) -> None:
        # Keep each policy group separate without publishing private keys.
        policy_index = (policy_wait_indices.setdefault(reason.repository, len(policy_wait_indices) + 1)
                        if isinstance(reason, records.RunLimitWait) else 0)
        add_wait(destination, wait_words(reason), name, policy_index=policy_index)

    for visible in waits:
        entry = visible.source
        if isinstance(entry, wire.CloudRow):
            if isinstance(entry.wait, wire.CloudSlotWait):
                name = short_name(origins.resolve("", entry.identity.folder))
                destination = row(name).aws.waits if name else metric_waits.setdefault("cloud", {})
                record_wait(destination, entry.wait, visible.name)
            continue
        step = entry.step
        pinned = step.route.target if isinstance(step.route, target_config.PinnedTarget) else local
        reason = waiting_cause(step)
        recorded = waiting_origins.get((step.pid, step.since), "")
        checkout = origins.resolve("", step.folder)
        origin = "" if recorded and checkout and recorded != checkout else recorded or checkout
        name = short_name(origin or recorded_origin(step.project)) or policy_label(step.project)
        machine = reason.machine if isinstance(reason, records.RunLimitWait) else pinned
        if isinstance(reason, records.RunLimitWait) and machine == local:
            if not origin and not (recorded and checkout) and name and step.project == reason.repository:
                _ = row(name)
            else:
                name = observe_local(origin, reason.repository)
            local_policy_waits.append(LocalPolicyWait(reason, name, visible.name))
            continue
        if (machine in mac_targets or (not isinstance(reason, records.RunLimitWait)
                                       and isinstance(step.route, target_config.MacOnly))):
            scope = reason.repository if isinstance(reason, records.RunLimitWait) else origins.mac_scopes.get(step.folder, "")
            mac_name = mac_row_name(scope)
            destination = row(mac_name).mac.waits if mac_name else mac_waits
        elif machine != local:
            destination = row(name).aws.waits if name and machine in saved.aws_targets else remote_wait(machine)
        elif isinstance(reason, records.GpuSlotWait):
            if name:
                destination = row(name).natedev.waits
            elif len(gpu.repositories) == 1 and (gpu_name := policy_label(next(iter(gpu.repositories)))):
                destination = row(gpu_name).natedev.waits
            else:
                destination = local_waits
        elif name:
            destination = row(name).natedev.waits
        elif isinstance(reason, (records.BuildSpaceLow, records.BuildSpaceUnreadable)):
            destination = metric_waits.setdefault("disk", {})
        elif isinstance(reason, (records.NeedsMemory, records.MemoryPressure, records.PressureUnavailable)):
            destination = metric_waits.setdefault("memory", {})
        else:
            destination = local_waits
        record_wait(destination, reason, visible.name)

    bindings = Counter(key for keys in keys_by_row.values() for key in keys)
    bound_rows = {key: name for name, keys in keys_by_row.items() for key in keys if bindings[key] == 1}
    for waiting in local_policy_waits:
        name = waiting.repository or bound_rows.get(waiting.reason.repository, "")
        destination = row(name).natedev.waits if name and not unresolved_policy else local_waits
        record_wait(destination, waiting.reason, waiting.name)

    cloud_slots = saved.cloud_slots
    cloud_used = cloud_waiting = 0
    cloud_origin_missing = False
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
            name = short_name(origin)
            if not name:
                cloud_origin_missing = True
                continue
            cell = row(name).aws
            if isinstance(saved.hana, ReadingState):
                continue
            cell.policy = CellPolicy.NO_LIMIT
            cell.other_used += 1
            label = clean_label(cloud_names.get(cloud.owner, ""), cloud.identity.unit, name)
            if label:
                cell.other.append(label)
            cell.note = PAGE_WORDS["uses_cloud"]

    # Every Hana target uses the same AWS lifecycle controller.
    hana = () if isinstance(saved.hana, ReadingState) else tuple(dict.fromkeys(saved.hana))
    for capacity in hana:
        if name := short_name(recorded_origin(capacity.repository)):
            cell = row(name).aws
            if len(hana) != 1:
                cell.policy = CellPolicy.UNRESOLVED
                continue
            cell.policy = CellPolicy.CONFIGURED
            cell.limit = capacity.limit
            cell.used = capacity.used if isinstance(capacity.used, int) else None
            if capacity.used is ReadingState.UNAVAILABLE:
                cell.note = PAGE_WORDS["not_available"]

    shared: list[Metric] = []
    for repository_key, limit in local_limits.items():
        owners = repository_runs.get(repository_key, set[str]())
        verify_used = len(inputs.verify_run_owners.get(repository_key, ()))
        used = len(owners)
        count = None if occupancy is None else used
        runs: list[str] = []
        merge_above: list[str] = []
        for owner in sorted(owners):
            label, recorded_unit = run_labels.get((repository_key, owner), ("", ""))
            if used - limit > len(merge_above) and recorded_unit and recorded_unit == policy.exempt_unit:
                merge_above.append(label)
            elif label:
                runs.append(label)
        name = bound_rows.get(repository_key, "")
        if unresolved_policy or name not in rows or len(keys_by_row[name]) > 1:
            key_label = policy_label(repository_key)
            label = PAGE_WORDS["policy_key"].format(name=key_label) if key_label else PAGE_WORDS["repository_run_limit"]
            shared.append(Metric(label, count, limit, exempt_runs=len(merge_above)))
            continue
        cell = rows[name].natedev
        cell.limit, cell.used = limit, count
        cell.verify_used = verify_used
        cell.runs, cell.merge_above = runs, merge_above

    for key in sorted(gpu.repositories):
        if name := bound_rows.get(key) or short_name(recorded_origin(key)) or policy_label(key):
            row(name).natedev.gpu = Metric("gpu", gpu_used if occupancy is not None else None, gpu.slots)

    for name, item in rows.items():
        if name in keys_by_row and not unresolved_policy:
            item.natedev.policy = CellPolicy.CONFIGURED if keys_by_row[name] else CellPolicy.NO_LIMIT
        if unresolved_policy and item.natedev.other_used:
            unattributed += item.natedev.other_used
            item.natedev.other_used = 0
            item.natedev.other.clear()
        for cell in (item.natedev, item.mac, item.aws):
            if cell.policy is CellPolicy.NO_LIMIT:
                cell.runs.extend(cell.other)
                cell.used = cell.other_used
                cell.other.clear()
                cell.other_used = 0
            if cell.used or cell.limit is not None or cell.waits or cell.runs or cell.other_used or cell.gpu:
                item.quiet = False
        if unattributed and item.natedev.policy is CellPolicy.NO_LIMIT:
            item.natedev.used = None
            item.natedev.runs.clear()
        if local_note:
            item.natedev.used = None
            item.natedev.note = local_note
        if cloud_origin_missing:
            item.aws.used = None
            item.aws.runs.clear()
            item.aws.other_used = 0
            item.aws.other.clear()
            item.aws.show_marks = item.aws.limit is None

    mac_heading = place_mac_counts(mac, rows, names) if mac is not None else MacHeading()
    room = explanation.room
    memory_used = room.cap_use / records.GIB if isinstance(room, records.MemoryRoom) else None
    memory_cap = room.cap / records.GIB if isinstance(room, records.MemoryRoom) and isinstance(room.cap, int) else None
    limits = [Metric("memory", memory_used, memory_cap, "GiB", waits=metric_waits.get("memory", {}),
                     unreadable=isinstance(room, records.ReadingUnavailable))]
    disk = inputs.build_space
    disk_waits = metric_waits.get("disk", {})
    if isinstance(disk, records.BuildSpaceLow):
        limits.append(Metric("disk", disk.free_gib, disk.floor_gib, "GiB", "minimum", disk_waits))
    elif disk_waits:
        limits.append(Metric("disk", None, None, waits=disk_waits))
    aws_active = cloud_used or any(isinstance(capacity.used, int) and capacity.used for capacity in hana)
    aws_state = (PAGE_WORDS["in_use"] if aws_active else
                 PAGE_WORDS["not_available"] if saved.hana is ReadingState.UNAVAILABLE else "")
    cloud = Metric("cloud", cloud_used if isinstance(cloud_slots, int) else None,
                   cloud_slots if isinstance(cloud_slots, int) else None, waits=metric_waits.get("cloud", {}),
                   unreadable=cloud_slots is ReadingState.UNAVAILABLE, reserved_waiting=cloud_waiting)
    first_rows = {"claude_commands": 0, "hana": 1}

    def facts_text(machine: machine_facts.Machine) -> str:
        return facts_line(facts, machine, explanation.now, zone)

    return BuildLimits(MachineLimits(local_state(snapshots, local, bool(inputs.reservations), zone),
                                    (*limits, *shared), local_waits, unattributed, local_note, facts_text("natedev")),
                       MachineLimits(mac_state(snapshots, zone), (), mac_waits, mac_heading.unattributed,
                                     mac_heading.note, facts_text("mac"), unattributed_kept=mac_heading.kept),
                       MachineLimits(aws_state, (cloud, saved.aws_money), aws_waits, facts=facts_text("aws")),
                       tuple(sorted(rows.values(), key=lambda item: (first_rows.get(item.name, len(first_rows)),
                                                                     item.name.casefold(), item.name))),
                       saved.history is ReadingState.UNAVAILABLE, unplaced)
