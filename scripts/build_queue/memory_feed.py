#!/usr/bin/env python3
"""Publish the build memory page and its current queue reading."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict
from zoneinfo import ZoneInfo


class CodeRootArguments(argparse.Namespace):
    code_root: Path = Path(__file__).resolve().parents[2]


installed = CodeRootArguments()
if __name__ == "__main__":
    bootstrap = argparse.ArgumentParser(add_help=False)
    _ = bootstrap.add_argument("--code-root", type=Path, default=Path(__file__).resolve().parents[2])
    installed, _remaining = bootstrap.parse_known_args(namespace=installed)

FEED_IMPORT_STARTED_AT_NS = time.time_ns()

from scripts.build_queue import admission_code


# Preserve the schema check across the imports below.
ADMISSION_VERSION_AT_IMPORT = admission_code.admission_code_version(installed.code_root / "scripts")

from scripts.buildlog import charts, mac_idle_alert, parse
from scripts.buildlog.store import host_name
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue import placement
from scripts.build_queue.queue_evidence import ci_amount as ci_bytes_text, room_text, keep_aside_text, service_activity_text, step_labels
from scripts.build_queue import admission_client, admission_protocol as wire, head_facts, queue_evidence, queue_view
from scripts.build_queue.queue_lines import age, plain, step_text
from scripts.build_queue.queue_view import reason_phrase
from scripts.lib.build_steps import StepNames, read_step_names
from scripts.build_queue.scope_activity import LiveBuildRun, NotServiceSlice, RunningServiceJob, ScopeProcesses
from scripts.build_queue import admission_records, target_availability
from scripts.build_targets import target_config
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels
from scripts.lib import build_slots, conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.review_links import console_theme, page_frame, review_link

if TYPE_CHECKING:
    from scripts.mac_test import claim_state

# These function-level imports also belong to the fixed source list.
FUNCTION_IMPORT_MODULES = (
    "scripts.buildlog.record", "scripts.buildlog.holds", "scripts.build_targets.target_config",
    "scripts.production.production_doc", "scripts.build_hold.override_guard",
    "scripts.production.change_routing", "scripts.mac_test.offload",
    "scripts.build_hold.build_hold", "scripts.mac_test.mac_test",
)


def feed_code_paths() -> tuple[str, ...]:
    """Freeze the feed modules and startup files after the imports finish."""
    for name in FUNCTION_IMPORT_MODULES:
        _ = importlib.import_module(name)
    source_root = Path(__file__).resolve().parents[2]
    paths = {"scripts/" + relative for relative in admission_code.ADMISSION_CODE_PATHS}
    from scripts.lib.python_startup.sitecustomize import STARTUP_FILES

    paths.update((*STARTUP_FILES, "scripts/build_queue/memory_live.html", "config/memory_feed.conf"))
    for module in tuple(sys.modules.values()):
        source = getattr(module, "__file__", None)
        if isinstance(source, str) and source.endswith(".py"):
            path = Path(source).resolve()
            if path.is_relative_to(source_root):
                paths.add(path.relative_to(source_root).as_posix())
    return tuple(sorted(paths))


FEED_CODE_PATHS = feed_code_paths()


def installed_feed_code_version(code_root: Path) -> str:
    """Hash the fixed feed sources and startup files in the installed copy."""
    digest = hashlib.sha256()
    for relative in FEED_CODE_PATHS:
        try:
            content = (code_root / relative).read_bytes()
        except OSError:
            return ""
        digest.update(relative.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def loaded_feed_code_version(code_root: Path) -> str:
    """Reject an installation that changes while the feed imports its modules."""
    if admission_code.admission_code_version(code_root / "scripts") != ADMISSION_VERSION_AT_IMPORT:
        return ""
    version = installed_feed_code_version(code_root)
    try:
        if any((code_root / relative).stat().st_mtime_ns > FEED_IMPORT_STARTED_AT_NS for relative in FEED_CODE_PATHS):
            return ""
    except OSError:
        return ""
    return version


LOADED_FEED_CODE_VERSION = loaded_feed_code_version(installed.code_root)


GIB = 1024 ** 3
CI_CGROUP = Path("/sys/fs/cgroup/hana.slice/hana-ci.slice")
PROC = Path("/proc")
SYS_BLOCK = Path("/sys/block")
CGROUP = CI_CGROUP.parents[1]
SWAP_NAMES = {
    "agents": "agent sessions ({count})", "apps": "other apps: {app}",
    "desktop": "desktop session", "system": "system services", "ci": "hana CI",
    "ungrouped": "Unclassified groups", "unattributed": "Not attributed", "total": "All swap used",
    "difference": "Reading difference", "smaller": "{count} smaller groups", "between": "between steps",
    "compiler": "compiler cache",
    "unavailable": "Swap is unreadable: {path}",
    "unattributed_detail": "Pool swap minus the named cgroup readings.",
    "difference_detail": "The named cgroup readings exceed the pool swap.",
    "host": "SwapTotal minus SwapFree from {path}",
    "recorded": "SwapTotal minus SwapFree in the recorded admission reading",
}
SwapCategory = Literal["agents", "desktop", "system", "ci", ""]
SWAP_OWNER_SLICES: dict[str, SwapCategory] = {"app-agents.slice": "agents", "session.slice": "desktop",
                                              "system.slice": "system", "hana.slice": "ci", "hana-ci.slice": "ci"}
SwapPool = Literal["outside", "builds_ci"]
SWAP_POOLS: tuple[SwapPool, ...] = ("outside", "builds_ci")
SWAP_RETURN_NOTE = "It comes back into RAM when they run, so the queue holds RAM for it."
BUILDS_CI_SLICES = frozenset({"builds.slice", "hana.slice", "hana-ci.slice"})
SwapRowKind = Literal["owner", "ungrouped", "unattributed", "difference", "smaller", "total"]


class MachineColors(TypedDict):
    natedev: str
    mac: str
    aws: str


class ThemeColors(TypedDict):
    remote_memory: str
    machines: MachineColors


class PageColors(TypedDict):
    light: ThemeColors
    dark: ThemeColors


@dataclass(frozen=True)
class FeedConfiguration:
    output_folder: Path
    interval_seconds: float
    history_days: int
    swap_rows: int
    report_refresh_seconds: int
    colors: PageColors
    other_rows: int


def configured_output_folder(settings: conf.ConfigRead) -> Path:
    """Expand the configured page folder."""
    return Path(os.path.expandvars(settings.text("output_folder"))).expanduser()


def read_configuration(settings: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> FeedConfiguration:
    """Read the page settings from one configuration."""
    if isinstance(settings, conf.UseConfiguredValue):
        settings = conf.read("memory_feed")

    def color(key: str) -> str:
        value = settings.text(key)
        if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
            raise ValueError(f"{key}: expected six hexadecimal digits")
        return "#" + value

    def theme(name: str) -> ThemeColors:
        return {"remote_memory": color(f"remote_memory_{name}"), "machines": {
            "natedev": color(f"machine_natedev_{name}"), "mac": color(f"machine_mac_{name}"),
            "aws": color(f"machine_aws_{name}")}}

    return FeedConfiguration(configured_output_folder(settings),
                             settings.number("interval_seconds"), settings.whole("history_days"),
                             settings.whole("swap_rows"), settings.whole("report_refresh_seconds"),
                             {"light": theme("light"), "dark": theme("dark")}, settings.whole("other_rows"))


class PageArguments(CodeRootArguments):
    once: bool = False
    command: Literal["feed", "link"] = "feed"


def page_arguments(argv: Sequence[str]) -> PageArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--once", action="store_true", help="Write one reading and exit.")
    _ = parser.add_argument("--code-root", type=Path, default=PageArguments.code_root,
                            help="Read the installed admission code from this repository.")
    commands = parser.add_subparsers(dest="command")
    _ = commands.add_parser("link", help="Print the build memory page link.")
    parser.set_defaults(command="feed")
    return parser.parse_args(argv, namespace=PageArguments())


def page_url() -> str:
    """Return the configured page URL without publishing a reading."""
    folder = configured_output_folder(conf.read("memory_feed")).resolve()
    page = folder / "memory_live.html"
    url = review_link.snapshot_url(review_link.review_root().resolve(), page)
    if not page.is_file():
        raise FileNotFoundError(f"Builds page is not available: {page}")
    return url


def page_link() -> int:
    """Print the configured page URL without publishing a reading."""
    try:
        url = page_url()
    except review_link.SNAPSHOT_ERRORS as error:
        print(f"Build page link: {error}")
        return 1
    print(url)
    return 0


def publish_page(configuration: FeedConfiguration) -> None:
    """Publish the repository page with the configured refresh interval."""
    source = console_theme.with_theme(Path(__file__).with_name("memory_live.html").read_text(encoding="utf-8"))
    page = (source.replace("__MEMORY_FEED_INTERVAL_SECONDS__", json.dumps(configuration.interval_seconds))
            .replace("__PAGE_FRAME_STYLE__", page_frame.STYLE))
    write_text_atomic(configuration.output_folder / "memory_live.html", page, new_file_mode=0o644)


def gib(value: object) -> float:
    return float(value) / GIB if isinstance(value, (int, float)) else 0.0


class GroupPart(TypedDict):
    who: str
    pid: int | admission_records.PidNotRecorded
    gib: float
    age: str


class LeftoverProcess(TypedDict):
    pid: int | admission_records.PidNotRecorded
    program: str


class LeftoverScope(TypedDict):
    owner: str
    age: str
    used: float
    members: list[LeftoverProcess]


class CIRunnerMemory(TypedDict):
    unit: str
    name: str
    used: float
    pid: int | admission_records.PidNotRecorded


class BusyCIRunner(CIRunnerMemory):
    state: Literal["busy"]
    started_at: float
    job: str


class IdleCIRunner(CIRunnerMemory):
    state: Literal["idle"]


class UnreadableCIRunner(TypedDict):
    unit: str
    name: str
    state: Literal["unreadable"]
    diagnostic: str


CIRunner = BusyCIRunner | IdleCIRunner | UnreadableCIRunner


@dataclass(frozen=True)
class RunnerProcess:
    pid: int
    parent: int
    started_at: float
    program: str
    command: tuple[str, ...]


class NoProgress(TypedDict):
    kind: Literal["NoProgress"]
    reason: str


class TokenWaitProgress(TypedDict):
    kind: Literal["TokenWaitProgress"]
    stage: str
    tokens: int


class RowProgress(TypedDict):
    kind: Literal["RowProgress"]
    phase: Literal["building", "testing"]
    done: int
    total: int
    percent: float
    unit: Literal["modules", "units", "tests"]


class RoomRow(TypedDict):
    label: str
    gib: float


class HeadRoom(TypedDict):
    """The first waiting step's room, one term to a row, then its need."""
    rows: list[RoomRow]
    note: str


class RowQueueNotes(TypedDict):
    head_room: NotRequired[HeadRoom]
    priority_text: NotRequired[str]
    ci_label: NotRequired[str]
    runs_on: NotRequired[str]
    mac_state: NotRequired[str]
    runner: NotRequired[unit_labels.Runner]
    worker: NotRequired[str]


class PageOwnerLabels(TypedDict):
    showrunner: str
    unit: str
    ci_label: str
    runner: unit_labels.Runner
    worker: str


def local_runs_on(mac: target_availability.TargetFit | target_availability.NoMacTarget) -> str:
    """Name the targets that can take a natedev queue step now: natedev, and the Mac when its routes allow it."""
    if isinstance(mac, target_availability.Allowed):
        return "natedev or Mac"
    if isinstance(mac, target_availability.FitUnreadable):
        return "natedev only (Mac state unknown)"
    return "natedev only"


# A run that asks for natedev: the plain words of the local caller route.
RUN_ASKS_TEXT = "the run asks for it"


def step_runs_on(route: target_config.StepRoute, routes: target_availability.MacRoutes, step: str, folder: str,
                 package: str | admission_records.PackageNotRecorded) -> str:
    """Name the targets that can take a natedev queue step: a route that limits the step first, else the Mac routes."""
    if isinstance(route, target_config.LocalRequired):
        return f"natedev only ({RUN_ASKS_TEXT if route == target_config.LOCAL_CALLER else route.reason})"
    if mac_only_route(route, routes):
        return "Mac only"
    if isinstance(route, target_config.PinnedTarget):
        return route.target + " only"
    return local_runs_on(routes.fit(step, folder, package))


class TargetNotes(TypedDict):
    runs_on: str
    mac_state: NotRequired[str]


def target_notes(route: target_config.StepRoute, routes: target_availability.MacRoutes, step: str, folder: str,
                 package: str | admission_records.PackageNotRecorded) -> TargetNotes:
    """The step's "can run on" text, and for a Mac-only step what holds the Mac, for every row that shows them."""
    notes: TargetNotes = {"runs_on": step_runs_on(route, routes, step, folder, package)}
    if mac_only_route(route, routes) and (holds := routes.holds(folder)):
        notes["mac_state"] = holds
    return notes


NO_TARGET_NOTES: TargetNotes = {"runs_on": ""}


def mac_only_route(route: target_config.StepRoute, routes: target_availability.MacRoutes) -> bool:
    """A Mac-only step, or a step pinned to the Mac target."""
    return isinstance(route, target_config.MacOnly) or (isinstance(route, target_config.PinnedTarget)
                                                        and routes.is_mac(route.target))


CLOUD_RUNS_ON = "AWS (natedev only when AWS cannot start the gate)"


def head_room(check: admission_records.RoomCheck,
              room: admission_records.MemoryRoom | admission_records.ReadingUnavailable) -> HeadRoom:
    """Show the first waiting step's room as free memory less each term, then its need."""
    rows: list[RoomRow] = []
    if isinstance(room, admission_records.MemoryRoom):
        rows.extend({"label": ("− " if index else "") + label, "gib": gib(amount)}
                    for index, (label, amount) in enumerate(queue_evidence.head_room_values(check, room)))
    rows.append({"label": "= Free", "gib": gib(max(0, check.room))})
    rows.append({"label": "need", "gib": gib(check.need)})
    swap = (f"Other programs have {gib(room.agent_swap_demand):.1f} GiB of memory in swap. {SWAP_RETURN_NOTE}"
            if isinstance(room, admission_records.MemoryRoom) and room.agent_swap_demand else "")
    basis = ("It can start alone, because no build runs." if isinstance(check.basis, admission_records.StartsAlone) else
            "It fits in the memory it already holds." if isinstance(check.basis, admission_records.WithinSlot) else
            "The user's admit does not count reserved memory." if isinstance(check.basis, admission_records.OverrideRoom) else
            "Free memory reading unavailable: " + room.reason if isinstance(room, admission_records.ReadingUnavailable) else "")
    return {"rows": rows, "note": " ".join(filter(None, (swap, basis)))}


def page_owner_labels(identity: unit_labels.UnitLabels, *, ci_label: str = "") -> PageOwnerLabels:
    """Name the showrunner, unit and runner of a row; keep CI labels separate from staff ownership."""
    runner = unit_labels.Runner.UNKNOWN if ci_label else identity.runner
    return {"showrunner": "" if ci_label else identity.owner_text,
            "unit": "" if ci_label else identity.unit, "ci_label": ci_label, "runner": runner,
            "worker": identity.session if runner is unit_labels.Runner.WORKER else ""}


def ci_step_label(runner: str, job: str) -> str:
    """Omit missing fields from the configured CI label."""
    label = conf.read_or_repository("build_admission").text("ci_label").format(runner=runner, job=job)
    return " · ".join(" ".join(part.split()) for part in label.split(" · ") if part.strip())


class RunRow(RowQueueNotes):
    admitted: NotRequired[bool]
    machine: NotRequired[str]
    vcpus: NotRequired[int | queue_view.UnknownCpuCount]
    slot: NotRequired[int | Literal["—"]]
    age: NotRequired[str]
    between_steps: NotRequired[bool]
    name: str
    showrunner: str
    unit: str
    progress: RowProgress | TokenWaitProgress | NoProgress
    state: str
    pid: int | admission_records.PidNotRecorded
    detail: str
    used: float | admission_records.RunUseUnavailable
    counted: float | admission_records.RunUseUnavailable
    reserved: float
    started_at: float
    queued: bool
    keep_text: str
    group: str
    ci: bool
    parts: list[GroupPart]
    ci_runners: NotRequired[list[CIRunner]]


class SwapMember(TypedDict):
    name: str
    bytes: int


class SwapRow(TypedDict):
    name: str
    detail: str
    swap: float
    bytes: int
    kind: SwapRowKind
    pool: SwapPool | Literal["all"]
    category: SwapCategory
    members: list[SwapMember]
    owner_count: int
    showrunner: NotRequired[str]
    unit: NotRequired[str]
    ci_label: NotRequired[str]


@dataclass(frozen=True)
class SwapOwner:
    name: str
    detail: str
    identity: unit_labels.UnitLabels


@dataclass(frozen=True)
class SwapUnavailable:
    reason: str


@dataclass(frozen=True)
class SwapReading:
    used: int
    total: int
    builds_ci: int
    rows: list[SwapRow]
    files_read: int


class WaitRow(RowQueueNotes):
    name: str
    showrunner: str
    unit: str
    pid: int
    detail: str
    need: float
    since: float
    reason: str
    fits: bool


class MacBuildRow(RowQueueNotes):
    kind: Literal["mac-build"]
    name: str
    repository: str
    gate_label: str
    showrunner: str
    unit: str
    pid: int
    machine: Literal["mac"]
    started_at: float
    read_at: float
    used: float | queue_view.MacMemoryUnavailable
    stage: queue_view.MacBuildStage
    progress: RowProgress | NoProgress
    vcpus: int | queue_view.MacJobsUnavailable
    slot: int | Literal["—"]


class MacReadingUnavailableRow(RowQueueNotes):
    kind: Literal["mac-unavailable"]
    name: Literal["Mac builds"]
    unit: Literal["—"]
    pid: admission_records.PidNotRecorded
    machine: Literal["mac"]
    started_at: float
    read_at: float
    used: queue_view.MacMemoryUnavailable
    stage: queue_view.MacStageUnavailable
    vcpus: queue_view.MacJobsUnavailable
    status: str


class CloudWorkRow(RowQueueNotes):
    kind: Literal["cloud"]
    name: str
    showrunner: str
    unit: str
    pid: int | admission_records.PidNotRecorded
    detail: str
    machine: Literal["aws", ""]
    vcpus: int | queue_view.UnknownCpuCount
    state: str
    status: str
    progress: RowProgress | NoProgress
    stale: bool
    late_age: str


class CloudRunRow(CloudWorkRow):
    started_at: float


class CloudWaitRow(CloudWorkRow):
    since: float
    reason: str


@dataclass(frozen=True)
class PressureNotRecorded:
    reason: str


class PressureValues(TypedDict):
    some: float | PressureNotRecorded
    full: float | PressureNotRecorded


class PressureLimits(TypedDict):
    some: float
    full: float


class ReportCost(TypedDict):
    machine: str
    cause: str
    recent: dict[str, object]
    window: dict[str, object]
    action: str
    owner: str


@dataclass(frozen=True)
class ReportSections:
    charts: dict[str, object]
    top: tuple[ReportCost, ...]
    start: str
    end: str
    recent_days: int
    zone: str


class ReportNotRead(Enum):
    VALUE = "not read"


class SwapTotals(TypedDict):
    used_bytes: int
    capacity_bytes: int
    builds_ci_bytes: int
    outside_bytes: int
    reserve_share_percent: int


class MemoryPage(TypedDict):
    colors: PageColors
    ok: bool
    read_at: float
    total: float
    available: float
    cap: float | admission_records.CapUnavailable
    floor: float
    cap_text: str
    shortfall: float
    used: float
    reserved: float
    taken: float
    room: float
    room_source: Literal["free"]
    limit: str
    ram: RamBlocks
    memory_status: MemoryStatus
    ci_name: str
    ci_ram: float
    waiting: int
    smallest_need: float
    pressure: str
    swap: SwapTotals
    walk: list[RunRow | CloudRunRow | MacBuildRow | MacReadingUnavailableRow]
    waiters: list[WaitRow | CloudWaitRow]
    swap_by: list[SwapRow]
    priority_text: str
    queued_priority_units: list[str]
    unmatched_priority_text: str
    mac_idle_alert: str
    targets: list[target_availability.TargetRow]
    cloud_slots_used: int
    cloud_slots: int
    psi: PressureValues
    limits: PressureLimits
    report: ReportSections | charts.IndexUnavailable
    swap_rows: int
    leftovers: list[LeftoverScope]


class RetainedReading(MemoryPage):
    read_error: str
    error_at: float


@dataclass(frozen=True)
class NoGoodReading:
    pass


class PageUnavailable(TypedDict):
    ok: bool
    error: str
    read_at: float


def cargo_job(command: tuple[str, ...]) -> str:
    """Return the job word from a Cargo command."""
    for index, argument in enumerate(command):
        if Path(argument).name not in ("cargo", "cargo-tile-real", "cargo-nextest"):
            continue
        subcommand = parse.cargo_subcommand(command[index + 1:])
        if subcommand in ("test", "nextest"):
            return "tests"
        if subcommand == "build" and any(flag in command for flag in ("--release", "-r")):
            return "release build"
        if subcommand:
            return subcommand
    return ""


def read_ci_runner(unit: Path, proc: Path) -> CIRunner:
    name = unit.name.removeprefix("github-runner-").removesuffix(".service")
    try:
        used = gib(gate.slice_working_set(unit).bytes)
        boot = float(next(line.split()[1] for line in (proc / "stat").read_text().splitlines()
                          if line.startswith("btime ")))
        ticks = int(os.sysconf("SC_CLK_TCK"))
        members: set[int] = set()
        pending = [unit]
        while pending:
            scope = pending.pop()
            members.update(int(value) for value in (scope / "cgroup.procs").read_text().split())
            pending.extend(child for child in scope.iterdir() if child.is_dir())
        processes: dict[int, RunnerProcess] = {}
        for pid in sorted(members):
            try:
                stat = (proc / str(pid) / "stat").read_text()
                fields = stat.rsplit(") ", 1)[1].split()
                if fields[0] in ("Z", "X"):
                    continue
                command = tuple(part for part in (proc / str(pid) / "cmdline").read_bytes()
                                .decode(errors="replace").split("\0") if part)
                processes[pid] = RunnerProcess(pid, int(fields[1]), boot + int(fields[19]) / ticks,
                                               stat.split("(", 1)[1].rsplit(") ", 1)[0], command)
            except FileNotFoundError:
                continue
        workers = sorted((p for p in processes.values() if p.program == "Runner.Worker"),
                         key=lambda p: (p.started_at, p.pid))
        if workers:
            worker = workers[0]
            jobs: list[tuple[float, str]] = []
            for process in processes.values():
                job = cargo_job(process.command)
                if not job:
                    continue
                ancestor = process.pid
                seen: set[int] = set()
                while ancestor in processes and ancestor not in seen:
                    if ancestor == worker.pid:
                        jobs.append((process.started_at, job))
                        break
                    seen.add(ancestor)
                    ancestor = processes[ancestor].parent
            return {"unit": unit.name, "name": name, "state": "busy", "used": used,
                    "pid": worker.pid, "started_at": worker.started_at,
                    "job": min(jobs)[1] if jobs else "CI job"}
        listeners = sorted(p.pid for p in processes.values() if p.program == "Runner.Listener")
        return {"unit": unit.name, "name": name, "state": "idle", "used": used,
                "pid": listeners[0] if listeners else admission_records.PidNotRecorded()}
    except (OSError, ValueError, IndexError, StopIteration) as error:
        return {"unit": unit.name, "name": name, "state": "unreadable", "diagnostic": str(error)}


def job_age(started_at: float, now: float) -> str:
    minutes = max(0, int((now - started_at) / 60 + 0.5))
    if minutes < 1:
        return "under 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h {minutes % 60} min" if hours < 48 else f"{hours // 24} days"


def ci_amount(value: float) -> str:
    """Show the shared CI text for an amount in GiB."""
    return ci_bytes_text(round(value * admission_records.GIB))


def build_ci_row(used: float, now: float,
                 cgroup: Path = CI_CGROUP, proc: Path = PROC, *, admitted: bool = False) -> RunRow:
    runners: list[CIRunner]
    try:
        units = sorted(child for child in cgroup.iterdir()
                       if child.name.startswith("github-runner-") and child.name.endswith(".service"))
        runners = [read_ci_runner(unit, proc) for unit in units]
    except OSError as error:
        runners = [{"unit": cgroup.name, "name": "hana CI",
                                    "state": "unreadable", "diagnostic": str(error)}]
    busy = [runner for runner in runners if runner["state"] == "busy"]
    busy.sort(key=lambda runner: runner["started_at"])
    detail = (f"{len(busy)} of {len(runners)} runners busy · {job_age(busy[0]['started_at'], now)}"
              if busy else "idle")
    if any(runner["state"] == "unreadable" for runner in runners):
        detail = detail + " · runner not readable" if busy else "runner not readable"
    lines: list[str] = []
    for runner in runners:
        if runner["state"] == "unreadable":
            lines.append(f"{runner['name']} · not readable")
        elif runner["state"] == "busy":
            lines.append(f"{runner['name']} · {runner['job']} · {job_age(runner['started_at'], now)} · {ci_amount(runner['used'])}")
        else:
            lines.append(f"{runner['name']} · idle · {ci_amount(runner['used'])}")
    lines.append(f"hana CI uses {ci_amount(used)} now. This use is part of the work inside the cap.")
    idle = [runner for runner in runners if runner["state"] == "idle" and isinstance(runner["pid"], int)]
    pid = busy[0]["pid"] if busy else idle[0]["pid"] if idle else admission_records.PidNotRecorded()
    return {"name": read_step_names().for_owner(busy[0]["job"] if busy else "job", "", unit_labels.OwnerRole.UNKNOWN,
                                               ci=True, current_name="hana CI"),
            "showrunner": "", "unit": "", "ci_label": ci_step_label(busy[0]["name"] if busy else "",
                                                                       busy[0]["job"] if busy else ""),
            "state": detail if not busy else "",
            "pid": pid, "detail": detail, "used": used,
            "counted": 0.0, "reserved": 0.0, "started_at": busy[0]["started_at"] if busy else 0,
            "queued": False, "keep_text": "\n".join(lines), "group": "", "ci": True, "parts": [],
            "ci_runners": runners, "admitted": admitted,
            "progress": {"kind": "NoProgress", "reason": "no admission counter"}}


def row_progress(steps: Sequence[admission_records.RunningStep]) -> RowProgress | TokenWaitProgress | NoProgress:
    """Use the newest step counter or token wait from the admission reading."""
    reason = "no admission counter"
    phase: Literal["building", "testing"]
    unit: Literal["modules", "units", "tests"]
    for step in sorted(steps, key=lambda step: step.started_at, reverse=True):
        counter = queue_view.step_counter(step)
        if isinstance(counter, progress.NoCounter):
            if isinstance(counter.token_wait, progress.TokenWait):
                return {"kind": "TokenWaitProgress", "stage": progress.TOKEN_WAIT_REASON,
                        "tokens": counter.token_wait.tokens}
            reason = counter.reason
            continue
        if counter.phase == "modules" or counter.phase.startswith("suites"):
            phase, unit = "testing", "modules"
        elif counter.phase.startswith("test:"):
            phase, unit = "testing", "tests"
        elif counter.phase.startswith("build:"):
            phase, unit = "building", "units"
        else:
            reason = f"unknown counter phase: {counter.phase}"
            continue
        return {"kind": "RowProgress", "phase": phase, "done": counter.done,
                "total": counter.total, "percent": counter.percent, "unit": unit}
    return {"kind": "NoProgress", "reason": reason}


def feed_step_labels(step: admission_records.RunningStep | admission_records.WaitingStep,
                     labels: Mapping[str, unit_labels.UnitLabels], runners: frozenset[str]) -> unit_labels.UnitLabels:
    """Use the CI runner record instead of an absent staff owner."""
    if step.unit in runners:
        return unit_labels.UnitLabels("hana CI", "hana CI", step.unit)
    return step_labels(step, labels)


def mac_build_units(builds: Sequence[queue_view.MacBuild],
                    mac_claims: Sequence[claim_state.HeldClaim]) -> Mapping[int, unit_labels.UnitLabels]:
    """Use a Mac claim's session, from the page's one target reading, to resolve its staff unit."""
    from scripts.copies import copies
    from scripts.mac_test import claim_state

    if not builds:
        return {}
    held_claims = [claim for claim in mac_claims
                   if claim.record.get("remote_host", "mac").rsplit("@", 1)[-1] == "mac"]
    claims = [claim.record for claim in held_claims]
    leases = [lease for lease in copies.held_remote_mirrors()
              if lease.identity.host.rsplit("@", 1)[-1] == "mac"]
    labels = unit_labels.unit_labels_for([claim["submitting_session"] for claim in claims], os.environ)

    def contains(folder: str, project: str) -> bool:
        folder = folder.removeprefix("/private")
        project = project.removeprefix("/private")
        if folder.startswith("~/"):
            folder = folder[1:]
            return project.endswith(folder) or folder + "/" in project
        if not Path(folder).is_absolute():
            return project.endswith("/" + folder) or "/" + folder + "/" in project
        return Path(project).is_relative_to(Path(folder))

    result: dict[int, unit_labels.UnitLabels] = {}
    for build in builds:
        sessions = {lease.session for lease in leases if contains(lease.identity.path, build.folder)}
        if build.session:
            sessions.add(build.session)
        matches = {labels[claim["submitting_session"]] for claim in claims
                   if claim["submitting_session"] in labels and
                   (not build.session or build.session == claim["submitting_session"]) and
                   (contains(claim["worktree"], build.folder) or
                    claim["submitting_session"] in sessions and
                    claim.get("repo_name", "") == build.repository)}
        if len(matches) == 1:
            result[build.pid] = next(iter(matches))
        elif (not matches and not sessions and build.origin is queue_view.MacBuildOrigin.NON_CI and
              not any(contains(claim["worktree"], build.folder) for claim in claims)):
            repository_claims = [claim for claim in held_claims
                                 if isinstance(claim.scope, claim_state.RepositoryScope) and
                                 claim.scope.name == build.repository]
            if len(repository_claims) == 1:
                session = repository_claims[0].record["submitting_session"]
                if session in labels:
                    result[build.pid] = labels[session]
    return result


def build_slot_number(folder: str, machine: str, roots: Mapping[str, str], *, home: str) -> int | Literal["—"]:
    """Read the slot number from a direct child of the machine's configured root."""
    root = build_slots.slot_root(roots, machine)
    if not root or not folder:
        return "—"
    uses_home = root == "~" or root.startswith("~/") or re.search(r"\$(?:HOME\b|\{HOME\})", root)
    if uses_home and not home:
        return "—"
    root = re.sub(r"\$(?:HOME\b|\{HOME\})", lambda _: home, root)
    if root == "~" or root.startswith("~/"):
        root = home + root[1:]
    # Compare the Mac root unresolved; the page cannot resolve Mac paths locally.
    configured = Path(os.path.realpath(root) if machine == host_name() else os.path.normpath(root))
    run_folder = Path(os.path.normpath(folder))
    match = re.fullmatch(r".+-slot([1-9][0-9]*)", run_folder.name)
    return int(match[1]) if match and run_folder.parent == configured else "—"


def mac_work_row(build: queue_view.MacBuild, reading: queue_view.MacBuildReading,
                 identity: unit_labels.UnitLabels, *, names: StepNames,
                 slot_roots: Mapping[str, str], slot_machine: str) -> MacBuildRow:
    """Keep Mac memory and its stage separate from natedev admission values."""
    label = ci_step_label(build.ci_runner, build.ci_job) if build.origin is queue_view.MacBuildOrigin.CI else ""
    gate = queue_view.gate_label_text(build)
    # A placed run is the normal case, so its name leaves out the placement id; the other labels mark an exception.
    shown_gate = "" if build.gate_label == queue_view.MacGateLabel.PLACEMENT else gate
    return {"kind": "mac-build",
            "name": queue_view.mac_step_name(build, names=names, identity=identity) + (" · " + shown_gate if shown_gate else ""),
            **page_owner_labels(identity, ci_label=label), "gate_label": gate, "repository": build.repository,
            "pid": build.pid, "machine": "mac", "started_at": build.started_at, "read_at": reading.read_at,
            "used": gib(build.resident_bytes) if isinstance(build.resident_bytes, int) else build.resident_bytes,
            "stage": build.stage, "vcpus": queue_view.mac_command_jobs(build.command),
            "slot": build_slot_number(build.folder, slot_machine, slot_roots, home=reading.home),
            "progress": ({"kind": "RowProgress", "phase": "testing" if build.counter.phase.startswith("test:") else "building",
                          "done": build.counter.done, "total": build.counter.total, "percent": build.counter.percent,
                          "unit": "tests" if build.counter.phase.startswith("test:") else "units"}
                         if isinstance(build.counter, progress.ProgressCounter) else
                         {"kind": "NoProgress", "reason": build.counter.reason})}


def cloud_row_progress(counter: progress.ProgressCounter | progress.NoCounter) -> RowProgress | NoProgress:
    if isinstance(counter, progress.NoCounter):
        return {"kind": "NoProgress", "reason": counter.reason}
    return {"kind": "RowProgress", "phase": "testing", "done": counter.done,
            "total": counter.total, "percent": counter.percent, "unit": "modules"}


def run_keep_text(hold: admission_records.RunKeepAside, steps: Sequence[admission_records.RunningStep]) -> str:
    lines = [keep_aside_text(hold)]
    for step in steps:
        detail = gate.progress_text(step.measured)
        if detail:
            lines.append(f"{step.step}: {detail}")
    return "\n".join(lines)


def read_stalls() -> PressureValues:
    """Read the current ten-second memory stall averages."""
    path = PROC / "pressure/memory"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        missing = PressureNotRecorded(f"Pressure is unreadable: {path}")
        return {"some": missing, "full": missing}
    values: PressureValues = {"some": PressureNotRecorded(f"Pressure is unreadable: {path}"),
                              "full": PressureNotRecorded(f"Pressure is unreadable: {path}")}
    for line in lines:
        fields = line.split()
        if not fields or fields[0] not in ("some", "full"):
            continue
        name = fields[0]
        for field in fields[1:]:
            if field.startswith("avg10="):
                try:
                    values[name] = float(field.removeprefix("avg10="))
                except ValueError:
                    values[name] = PressureNotRecorded(f"Pressure is unreadable: {path}")
    return values


def report_reading(days: int) -> ReportSections | charts.IndexUnavailable:
    """Read both report sections from one provider result."""
    try:
        recent_days = conf.read("buildlog").whole("TOP_COST_RECENT_DAYS")
        if days < recent_days:
            raise conf.Unusable(f"history_days={days} must be at least TOP_COST_RECENT_DAYS={recent_days}")
        report = charts.page_data(days, n=3)
        if isinstance(report, charts.IndexUnavailable):
            return report
        top = report.top
        rows: list[ReportCost] = []
        if not isinstance(top.rows, charts.NoDataYet):
            for row in top.rows:
                window: dict[str, object] = ({"state": "matches recent"} if isinstance(row.window, charts.WindowMatchesRecent)
                          else charts.cost_json(row.window.value, 3600))
                rows.append({"machine": row.machine, "cause": row.cause,
                             "recent": charts.cost_json(row.recent, 3600 * top.recent_days),
                             "window": window, "action": row.action.text, "owner": row.action.owner})
        zone = user_zone()
        return ReportSections(charts.chart_data(report.charts), tuple(rows), top.start.astimezone(zone).isoformat(),
                              top.end.astimezone(zone).isoformat(), top.recent_days, str(zone))
    except (OSError, ValueError, RuntimeError) as error:
        return charts.IndexUnavailable(str(error))


def swap_row(name: str, detail: str, amount: int, pool: SwapPool | Literal["all"], kind: SwapRowKind = "owner",
             category: SwapCategory = "") -> SwapRow:
    return {"name": name, "detail": detail, "swap": gib(amount), "bytes": amount, "kind": kind,
            "pool": pool, "category": category, "members": [], "owner_count": 0}


def swap_pool(path: Path) -> SwapPool:
    """Place a cgroup in the builds and CI pool or outside it, by its slice membership."""
    return "builds_ci" if BUILDS_CI_SLICES.intersection(path.relative_to(CGROUP).parts) else "outside"


class SwapReadingPass:
    """Read each selected cgroup counter once for one host reading."""

    def __init__(self, owners: Mapping[Path, SwapOwner]) -> None:
        self.owners: Mapping[Path, SwapOwner] = owners
        self.owner_ancestors: set[Path] = {parent for owner in owners for parent in owner.parents}
        self.counters: dict[Path, int | SwapUnavailable] = {}
        self.rows: dict[str, SwapRow] = {}
        self.agent_count: int = 0

    def counter(self, path: Path) -> int | SwapUnavailable:
        if path not in self.counters:
            try:
                self.counters[path] = int((path / "memory.swap.current").read_text())
            except (OSError, ValueError):
                self.counters[path] = SwapUnavailable(SWAP_NAMES["unavailable"].format(path=path / "memory.swap.current"))
        return self.counters[path]

    def children(self, path: Path) -> list[Path]:
        try:
            with os.scandir(path) as entries:
                return sorted(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def add(self, key: str, name: str, path: Path, detail: str, amount: int,
            kind: Literal["owner", "ungrouped"] = "owner", category: SwapCategory = "") -> None:
        if not amount:
            return
        pool = swap_pool(path)
        key = f"{key}:{pool}" if kind == "ungrouped" else key
        if key in self.rows:
            row = self.rows[key]
            row["bytes"] += amount
            row["swap"] = gib(row["bytes"])
            row["detail"] += "; " + detail
            return
        self.rows[key] = swap_row(name, detail, amount, pool, kind, category)
        owner = self.owners.get(Path(key))
        if owner is not None:
            identity = page_owner_labels(owner.identity)
            self.rows[key]["showrunner"] = identity["showrunner"]
            self.rows[key]["unit"] = identity["unit"]
            self.rows[key]["owner_count"] = 1
        elif key == "ci":
            self.rows[key]["ci_label"] = SWAP_NAMES["ci"]

    def visit(self, path: Path) -> int:
        counter = self.counter(path)
        nested_owner = path in self.owner_ancestors
        category = SWAP_OWNER_SLICES.get(path.name, "")
        if path.name.startswith("session-") and path.name.endswith(".scope"):
            category = "desktop"
        leaf = path in self.owners or bool(category) or path.parent.name in ("app.slice", "background.slice")
        children = self.children(path) if not leaf or nested_owner or category == "agents" else []
        charged = 0
        shown = str(path.relative_to(CGROUP))
        if category == "agents":
            self.agent_count += sum(child.name.endswith(".scope") for child in children)
        if path in self.owners and not nested_owner:
            owner = self.owners[path]
            if isinstance(counter, int):
                self.add(str(path), owner.name, path, owner.detail + " · " + shown, counter)
        elif category and not nested_owner:
            if isinstance(counter, int):
                self.add(category, SWAP_NAMES[category], path, shown, counter, category=category)
        elif path.parent.name in ("app.slice", "background.slice") and not nested_owner:
            app = program_name(path)
            if isinstance(counter, int):
                self.add(str(path), SWAP_NAMES["apps"].format(app=app), path, shown, counter)
        elif children:
            charged = sum(self.visit(child) for child in children)
            if isinstance(counter, int):
                if path in self.owners:
                    owner = self.owners[path]
                    self.add(str(path), owner.name, path, owner.detail, counter - charged)
                elif category:
                    self.add(category, SWAP_NAMES[category], path, shown, counter - charged, category=category)
                else:
                    self.add("ungrouped", SWAP_NAMES["ungrouped"], path, shown, counter - charged, "ungrouped")
        elif isinstance(counter, int):
            self.add(str(path), path.name.removesuffix(".scope").removesuffix(".service").removesuffix(".slice"),
                     path, shown, counter)
        return counter if isinstance(counter, int) else charged


@dataclass(frozen=True)
class RecordedHostSwap:
    """Hold the host swap totals the admission decision used."""
    used: int
    capacity: int
    builds_ci: int


@dataclass(frozen=True)
class LiveHostSwap:
    """Read the host swap now; the builds and CI pool is its own group readings."""


LIVE_HOST_SWAP = LiveHostSwap()


def pool_rows(rows: Sequence[SwapRow], ungrouped: Sequence[SwapRow], pool: SwapPool, pool_bytes: int | LiveHostSwap,
              row_limit: int) -> list[SwapRow]:
    """Show one pool's largest groups, its smaller groups and its own reading difference."""
    main = [row for row in rows if row["category"]]
    others = [row for row in rows if not row["category"]]
    count = max(0, row_limit - len(main))
    visible = sorted(main + others[:count], key=lambda row: row["bytes"], reverse=True)
    smaller = others[count:]
    if smaller:
        smaller_row = swap_row(SWAP_NAMES["smaller"].format(count=len(smaller)),
            "; ".join(f"{row['name']}: {row['bytes']} B" for row in smaller),
            sum(row["bytes"] for row in smaller), pool, "smaller")
        smaller_row["members"] = [{"name": row["name"], "bytes": row["bytes"]} for row in smaller]
        smaller_row["owner_count"] = sum(row["owner_count"] for row in smaller)
        visible.append(smaller_row)
    visible.extend(ungrouped)
    difference = 0 if isinstance(pool_bytes, LiveHostSwap) else pool_bytes - sum(row["bytes"] for row in visible)
    if difference > 0:
        visible.append(swap_row(SWAP_NAMES["unattributed"], SWAP_NAMES["unattributed_detail"], difference, pool,
                                "unattributed"))
    elif difference < 0:
        visible.append(swap_row(SWAP_NAMES["difference"], SWAP_NAMES["difference_detail"], difference, pool,
                                "difference"))
    return visible


def read_swap(steps: Sequence[admission_records.RunningStep], labels: Mapping[str, unit_labels.UnitLabels],
              row_limit: int, names: StepNames, host: RecordedHostSwap | LiveHostSwap = LIVE_HOST_SWAP
              ) -> SwapReading | SwapUnavailable:
    """Account for host swap in two pools, with disjoint cgroup owners and an explicit difference in each pool."""
    owners: dict[Path, SwapOwner] = {}
    for step in steps:
        memory = step.measured
        if not isinstance(memory, admission_records.MeasuredMemory):
            continue
        source = memory.anonymous_source
        path: Path
        if isinstance(source, admission_records.ScopeSource):
            path = source.directory
        elif isinstance(memory, admission_records.UnownedSliceMemory) and memory.cgroup:
            path = Path(memory.cgroup)
        else:
            continue
        if path.is_relative_to(CGROUP) and path.name not in SWAP_OWNER_SLICES:
            identity = step_labels(step, labels)
            name, detail = step_text(step, names, labels=labels), identity.session
            if step.step == SWAP_NAMES["between"]:
                name, detail = identity.session or str(path.name), SWAP_NAMES["between"]
            elif isinstance(memory, admission_records.UnownedSliceMemory):
                name = identity.session or memory.owner
            elif isinstance(memory, admission_records.CompilerServiceMemory):
                name = SWAP_NAMES["compiler"]
            _ = owners.setdefault(path, SwapOwner(name, detail, identity))
    reading = SwapReadingPass(owners)
    for child in reading.children(CGROUP):
        _ = reading.visit(child)
    files_read = len(reading.counters)
    if isinstance(host, LiveHostSwap):
        path = PROC / "meminfo"
        try:
            values = {fields[0].removesuffix(":"): int(fields[1]) * 1024
                      for line in path.read_text().splitlines() if (fields := line.split()) and fields[0] in ("SwapTotal:", "SwapFree:")}
            capacity = values["SwapTotal"]
            used = capacity - values["SwapFree"]
        except (OSError, ValueError, KeyError, IndexError):
            return SwapUnavailable(SWAP_NAMES["unavailable"].format(path=path))
        files_read += 1
        builds_ci = sum(row["bytes"] for row in reading.rows.values() if row["pool"] == "builds_ci")
        pool_bytes: dict[SwapPool, int | LiveHostSwap] = {"outside": used - builds_ci, "builds_ci": host}
        source = SWAP_NAMES["host"].format(path=path)
    else:
        used, capacity, builds_ci = host.used, host.capacity, host.builds_ci
        pool_bytes = {"outside": used - builds_ci, "builds_ci": builds_ci}
        source = SWAP_NAMES["recorded"]
    if "agents" in reading.rows:
        reading.rows["agents"]["name"] = SWAP_NAMES["agents"].format(count=reading.agent_count)
        reading.rows["agents"]["owner_count"] = reading.agent_count
    rows = sorted((row for row in reading.rows.values() if row["kind"] == "owner"),
                  key=lambda row: row["bytes"], reverse=True)
    visible = [row for pool in SWAP_POOLS
               for row in pool_rows([row for row in rows if row["pool"] == pool],
                                    [row for row in reading.rows.values() if row["kind"] == "ungrouped" and row["pool"] == pool],
                                    pool, pool_bytes[pool], row_limit)]
    visible.append(swap_row(SWAP_NAMES["total"], source, used, "all", "total"))
    return SwapReading(used, capacity, builds_ci, visible, files_read)


def swap_text(rows: Sequence[SwapRow]) -> str:
    """Show the same swap rows and Total as the page."""
    return "\n".join(f"{row['name']}: {row['bytes']} B ({row['swap']:.6f} GiB) · {row['detail']}" for row in rows)


class ProgramRam(TypedDict):
    id: str
    name: str
    bytes: int


RamReading = int | admission_records.ReadingUnavailable


class RamBlocks(TypedDict):
    """Give each bar block one byte amount, from the recorded room and one attribution reading."""
    capture_time: float
    attribution_read_at: float
    total_bytes: RamReading
    available_bytes: int
    safety_target_bytes: int
    swap_target_bytes: int
    build_target_bytes: int
    room_bytes: int
    shortfall_bytes: int
    safety_covered_bytes: int
    swap_covered_bytes: int
    build_covered_bytes: int
    safety_missing_bytes: int
    swap_missing_bytes: int
    build_missing_bytes: int
    queued_builds_bytes: int
    build_tools_bytes: int
    rust_analyzer_bytes: int
    sccache_bytes: int
    other_build_tools_bytes: int
    # The CI use that the queue cap counts: swap-backed use.
    ci_bytes: int
    # What builds may use: build tools, queued builds, CI, build reserved and free.
    cap_bytes: int
    agents_bytes: RamReading
    desktop_bytes: RamReading
    desktop_protection_bytes: RamReading
    programs: list[ProgramRam]
    other_programs_bytes: int
    smaller_programs_bytes: int
    smaller_program_count: int
    kernel_bytes: RamReading
    compressed_swap_bytes: RamReading
    kernel_structures_bytes: RamReading
    kernel_counters_bytes: RamReading
    kernel_no_counter_bytes: int
    cache_bytes: RamReading
    unattributed_bytes: int
    missing: list[str]
    agreement: Literal["agree", "disagree"]


MemoryStatusCode = Literal["unavailable", "gpu_slot", "pressure", "shortfall", "no_room", "one_running_memory_wait", "none"]


class MemoryStatus(TypedDict):
    code: MemoryStatusCode
    active_local_build_count: int
    next_need_bytes: int
    check_room_bytes: int


RAM_NAMES = {
    "system": "system services", "background": "background services", "sessions": "login sessions",
    "agents": "Agents", "desktop": "Desktop programs", "kernel": "Kernel, measured", "cache": "File and kernel cache",
}
RUST_ANALYZER = "rust-analyzer"
KERNEL_FIELDS = ("SUnreclaim", "KernelStack", "PageTables")
KERNEL_COUNTER_FIELDS = ("SecPageTables", "Percpu", "VmallocUsed")
# MemTotal less these fields and the kernel parts leaves the kernel memory that no counter names.
COUNTED_FIELDS = ("MemFree", "Active(anon)", "Inactive(anon)", "Active(file)", "Inactive(file)",
                  "Unevictable", "KReclaimable")
CACHE_FIELDS = ("Buffers", "Cached", "Shmem", "KReclaimable", "MemAvailable", "MemFree")


def scope_program(path: Path) -> str:
    """Name a run scope by its program only when every member runs that program."""
    try:
        programs = {(PROC / pid / "comm").read_text().strip() for pid in (path / "cgroup.procs").read_text().split()}
    except OSError:
        programs = set[str]()
    if len(programs) != 1:
        return path.name.removesuffix(".scope")
    # A Nix wrapper runs as ".ghostty-wrapped"; its comm is cut to 15 bytes.
    return re.sub(r"^\.(.+)-wrap[a-z]*$", r"\1", programs.pop()) or path.name


def program_name(path: Path) -> str:
    """Name an app cgroup by its program: app-com.google.Chrome-41.scope gives Chrome."""
    name = path.name.removeprefix("app-").removesuffix(".scope").removesuffix(".service").removesuffix(".slice")
    if name.startswith("run-"):
        return scope_program(path)
    name = re.sub(r"(@.*|-[0-9]+|-[0-9a-f]{16,})$", "", name.replace("\\x2d", "-")).removeprefix("google-")
    return name.rsplit(".", 1)[-1] or path.name


def swap_backed_bytes(path: Path) -> int:
    """Read the anonymous plus shared memory of one cgroup: memory that only swap can free."""
    gate.check_memory_reading(path / "memory.stat")
    fields = dict(line.split() for line in (path / "memory.stat").read_text().splitlines())
    return int(fields["anon"]) + int(fields["shmem"])


def other_programs() -> dict[str, int]:
    """Read the programs outside builds, CI, agents and the desktop, by name, each cgroup charge once."""
    user = gate.user_manager_cgroup()
    groups: list[tuple[str, Path]] = [(program_name(child), child) for child in sorted((user / "app.slice").glob("*"))
                                      if child.is_dir() and child.name != "app-agents.slice"]
    groups += [(RAM_NAMES["background"], user / "background.slice"), (RAM_NAMES["system"], CGROUP / "system.slice"),
               (RAM_NAMES["system"], CGROUP / "init.scope")]
    groups += [(RAM_NAMES["sessions"], scope) for scope in sorted(user.parent.glob("session-*.scope"))]
    programs: dict[str, int] = {}
    for name, path in groups:
        try:
            amount = swap_backed_bytes(path)
        except (OSError, ValueError, KeyError):
            continue
        # Chrome and chrome are one program: join names that differ only in case, and keep the capitalized one.
        key = next((known for known in programs if known.lower() == name.lower()), name)
        shown = key if key != key.lower() else name
        programs[shown] = programs.pop(key, 0) + amount
    return programs


def meminfo_bytes() -> dict[str, int] | admission_records.ReadingUnavailable:
    """Read /proc/meminfo once, in bytes."""
    path = PROC / "meminfo"
    try:
        return {fields[0].removesuffix(":"): int(fields[1]) * 1024
                for line in path.read_text().splitlines() if (fields := line.split()) and len(fields) >= 2}
    except (OSError, ValueError) as error:
        return admission_records.ReadingUnavailable(f"{path}: {error}")


def meminfo_fields(values: Mapping[str, int] | admission_records.ReadingUnavailable,
                   names: Sequence[str]) -> Mapping[str, int] | admission_records.ReadingUnavailable:
    if isinstance(values, admission_records.ReadingUnavailable):
        return values
    absent = [name for name in names if name not in values]
    return admission_records.ReadingUnavailable("meminfo lacks " + ", ".join(absent)) if absent else values


def kernel_reading(values: Mapping[str, int] | admission_records.ReadingUnavailable) -> RamReading:
    """Measure the kernel structures in RAM: unreclaimable slab, kernel stacks and page tables."""
    fields = meminfo_fields(values, KERNEL_FIELDS)
    if isinstance(fields, admission_records.ReadingUnavailable):
        return fields
    return sum(fields[name] for name in KERNEL_FIELDS)


def compressed_swap_reading() -> RamReading:
    """Read the RAM that holds compressed swap: mem_used_total, field 3 of each zram device mm_stat."""
    total = 0
    for path in sorted(SYS_BLOCK.glob("zram*/mm_stat")):
        try:
            total += int(path.read_text().split()[2])
        except (OSError, ValueError, IndexError) as error:
            return admission_records.ReadingUnavailable(f"{path}: {error}")
    return total


def memmap_reading() -> RamReading:
    """Read the page descriptors allocated after boot, from /proc/vmstat nr_memmap_pages."""
    path = PROC / "vmstat"
    try:
        pages = dict(line.split() for line in path.read_text().splitlines())["nr_memmap_pages"]
        return int(pages) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, KeyError) as error:
        return admission_records.ReadingUnavailable(f"{path}: {error!r}")


def kernel_counters_reading(values: Mapping[str, int] | admission_records.ReadingUnavailable) -> RamReading:
    """Measure the other counted kernel memory: page descriptors, secondary page tables, per-CPU and vmalloc."""
    fields = meminfo_fields(values, (*KERNEL_COUNTER_FIELDS, "KernelStack"))
    if isinstance(fields, admission_records.ReadingUnavailable):
        return fields
    memmap = memmap_reading()
    if isinstance(memmap, admission_records.ReadingUnavailable):
        return memmap
    # With VMAP_STACK the kernel stacks are vmalloc pages; KernelStack already counts them.
    vmalloc = max(0, fields["VmallocUsed"] - fields["KernelStack"])
    return memmap + fields["SecPageTables"] + fields["Percpu"] + vmalloc


@dataclass(frozen=True)
class KernelRam:
    """Hold the three measured kernel parts and the kernel memory that no counter names."""
    compressed_swap: RamReading
    structures: RamReading
    counters: RamReading
    no_counter: int

    @property
    def total(self) -> RamReading:
        parts = (self.compressed_swap, self.structures, self.counters)
        unavailable = next((part for part in parts if isinstance(part, admission_records.ReadingUnavailable)), None)
        return unavailable if unavailable is not None else sum(known_bytes(part) for part in parts)


def kernel_ram(values: Mapping[str, int] | admission_records.ReadingUnavailable) -> KernelRam:
    """Measure the kernel parts; without every reading, the no-counter amount is 0."""
    structures, counters, compressed = kernel_reading(values), kernel_counters_reading(values), compressed_swap_reading()
    fields = meminfo_fields(values, ("MemTotal", *COUNTED_FIELDS))
    no_counter = 0
    if isinstance(fields, Mapping) and all(isinstance(part, int) for part in (structures, counters, compressed)):
        counted = sum(fields[name] for name in COUNTED_FIELDS)
        no_counter = max(0, fields["MemTotal"] - counted - known_bytes(structures) - known_bytes(counters)
                         - known_bytes(compressed))
    return KernelRam(compressed, structures, counters, no_counter)


def cache_reading(values: Mapping[str, int] | admission_records.ReadingUnavailable) -> RamReading:
    """Estimate the file and kernel cache outside available RAM; a negative estimate stays negative."""
    fields = meminfo_fields(values, CACHE_FIELDS)
    if isinstance(fields, admission_records.ReadingUnavailable):
        return fields
    pool = fields["Buffers"] + fields["Cached"] - fields["Shmem"] + fields["KReclaimable"]
    return pool - max(0, fields["MemAvailable"] - fields["MemFree"])


def slice_program_bytes(reading: admission_records.MeasuredMemory | admission_records.ReadingUnavailable) -> RamReading:
    """Take the anonymous plus shared memory of one slice reading."""
    if isinstance(reading, admission_records.ReadingUnavailable):
        return reading
    if isinstance(reading.anonymous, admission_records.AnonymousRunUse):
        return reading.anonymous.bytes
    return admission_records.ReadingUnavailable(reading.anonymous.reason)


@dataclass(frozen=True)
class BuildToolRam:
    """Hold the builds.slice program RAM that no queued build holds."""
    rust_analyzer: int
    sccache: int
    other: int


def build_tool_ram(running: Sequence[admission_records.ExplainedRunningStep], queued_use: int) -> BuildToolRam:
    """Read the builds.slice program RAM that no queued build holds, on the same basis as the queued builds.

    rust-analyzer and the compiler cache have their own lines. Other build tools is the rest of builds.slice
    (the slice reading minus the queued use and the groups that another line or block counts), plus each
    recorded tool group outside builds.slice.
    """
    builds = gate.builds_slice_cgroup()
    rust_analyzer = sccache = outside_tools = named_inside = 0
    for entry in running:
        memory = entry.step.measured
        if isinstance(memory, admission_records.CompilerServiceMemory):
            path = builds / "sccache.service"
        elif isinstance(memory, admission_records.UnownedSliceMemory) and memory.cgroup and not memory.diagnostic:
            path = Path(memory.cgroup)
        else:
            continue
        try:
            amount = swap_backed_bytes(path)
        except (OSError, ValueError, KeyError):
            # The group ended after the capture: it holds no RAM now.
            continue
        inside = path.is_relative_to(builds)
        if isinstance(memory, admission_records.CompilerServiceMemory):
            sccache += amount
        elif (not isinstance(admission_records.recorded_service_state(memory), NotServiceSlice)
              or entry.step.step == SWAP_NAMES["between"]):
            # A CI service counts in hana CI, and a run between steps counts in its queued hold.
            pass
        elif isinstance(memory.activity, ScopeProcesses) and memory.activity.program == RUST_ANALYZER:
            rust_analyzer += amount
        elif not inside:
            outside_tools += amount
            continue
        else:
            continue
        named_inside += amount if inside else 0
    try:
        slice_bytes = swap_backed_bytes(builds)
    except (OSError, ValueError, KeyError):
        slice_bytes = queued_use + named_inside
    return BuildToolRam(rust_analyzer, sccache, max(0, slice_bytes - queued_use - named_inside) + outside_tools)


def known_bytes(value: RamReading) -> int:
    return value if isinstance(value, int) else 0


def ram_blocks(room: admission_records.MemoryRoom, tools: BuildToolRam, capture_time: float,
               program_rows: int) -> RamBlocks:
    """Give each bar block one amount: covered reserves, program RAM, the kernel, the cache and the rest.

    Build tools, queued builds, CI, reserved memory and free memory add to the queue cap.
    """
    available, safety, swap, build = room.available, room.safety, room.agent_swap_demand, room.kept
    safety_covered = min(safety, available)
    swap_covered = min(swap, max(0, available - safety))
    build_covered = min(build, max(0, available - safety - swap))
    agents = slice_program_bytes(gate.agents_memory())
    desktop = slice_program_bytes(gate.desktop_memory())
    values = meminfo_bytes()
    kernel, cache = kernel_ram(values), cache_reading(values)
    ranked = sorted(((name, amount) for name, amount in other_programs().items() if amount > 0),
                    key=lambda item: item[1], reverse=True)
    smaller = ranked[program_rows:]
    other_programs_bytes = sum(amount for _, amount in ranked)
    build_tools = tools.rust_analyzer + tools.sccache + tools.other
    total: RamReading = (room.total if isinstance(room.total, int)
                         else admission_records.ReadingUnavailable("total RAM is not recorded"))
    named = (room.used + build_tools + room.ci_use + known_bytes(agents) + known_bytes(desktop) + other_programs_bytes
             + known_bytes(kernel.total) + known_bytes(cache))
    unattributed = total - available - named if isinstance(total, int) else 0
    readings = ((RAM_NAMES["agents"], agents), (RAM_NAMES["desktop"], desktop),
                (RAM_NAMES["kernel"], kernel.total), (RAM_NAMES["cache"], cache))
    missing = [f"{name}: {value.reason}" for name, value in readings
               if isinstance(value, admission_records.ReadingUnavailable)]
    return {
        "capture_time": capture_time, "attribution_read_at": time.time(),
        "total_bytes": total, "available_bytes": available,
        "safety_target_bytes": safety, "swap_target_bytes": swap, "build_target_bytes": build,
        "room_bytes": room.room, "shortfall_bytes": room.shortfall,
        "safety_covered_bytes": safety_covered, "swap_covered_bytes": swap_covered, "build_covered_bytes": build_covered,
        "safety_missing_bytes": safety - safety_covered, "swap_missing_bytes": swap - swap_covered,
        "build_missing_bytes": build - build_covered,
        "queued_builds_bytes": room.used, "build_tools_bytes": build_tools, "rust_analyzer_bytes": tools.rust_analyzer,
        "sccache_bytes": tools.sccache, "other_build_tools_bytes": tools.other, "ci_bytes": room.ci_use,
        "cap_bytes": room.used + build_tools + room.ci_use + build_covered + room.room,
        "agents_bytes": agents, "desktop_bytes": desktop, "desktop_protection_bytes": gate.desktop_guarantee(),
        "programs": [{"id": name.lower(), "name": name, "bytes": amount} for name, amount in ranked[:program_rows]],
        "other_programs_bytes": other_programs_bytes,
        "smaller_programs_bytes": sum(amount for _, amount in smaller), "smaller_program_count": len(smaller),
        "kernel_bytes": kernel.total, "compressed_swap_bytes": kernel.compressed_swap,
        "kernel_structures_bytes": kernel.structures, "kernel_counters_bytes": kernel.counters,
        "kernel_no_counter_bytes": min(kernel.no_counter, max(0, unattributed)), "cache_bytes": cache,
        "unattributed_bytes": unattributed, "missing": missing,
        "agreement": "disagree" if unattributed < 0 or known_bytes(cache) < 0 else "agree",
    }


def memory_status(explanation: admission_records.QueueExplanation, room: admission_records.MemoryRoom,
                  active_local_builds: int) -> MemoryStatus:
    """Give the one reason the next start waits, from the recorded admission result."""
    head = explanation.head
    pressure = explanation.decided_from.pressure
    check = head.room if isinstance(head, admission_records.HeadExplanation) else admission_records.RoomCheck(
        0, room.room, True, basis=admission_records.Fits())
    code: MemoryStatusCode = "none"
    if (isinstance(pressure, admission_records.PressureUnavailable)
            or isinstance(head, admission_records.HeadExplanation)
            and isinstance(head.cause, (admission_records.ReadingUnavailable, admission_records.PressureUnavailable))):
        code = "unavailable"
    elif isinstance(pressure, admission_records.MemoryPressure):
        code = "pressure"
    elif room.room == 0:
        code = "shortfall" if room.shortfall > 0 else "no_room"
    elif isinstance(head, admission_records.HeadExplanation) and isinstance(head.cause, admission_records.GpuSlotWait):
        code = "gpu_slot"
    elif (active_local_builds == 1 and isinstance(head, admission_records.HeadExplanation)
          and isinstance(head.cause, admission_records.NeedsMemory) and not check.passes
          and explanation.line.fitting == 0):
        code = "one_running_memory_wait"
    return {"code": code, "active_local_build_count": active_local_builds,
            "next_need_bytes": check.need, "check_room_bytes": check.room}


def reading(configuration: FeedConfiguration | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
            report: ReportSections | charts.IndexUnavailable | ReportNotRead = ReportNotRead.VALUE) -> MemoryPage:
    if isinstance(configuration, conf.UseConfiguredValue):
        configuration = read_configuration()
    names = read_step_names()
    parsed = admission_client.view()
    explanation = parsed.explanation
    captured = explanation.room
    host = explanation.decided_from.host
    if not isinstance(captured, admission_records.MemoryRoom) or not isinstance(host, admission_records.HostMemory):
        raise RuntimeError("recorded room unavailable")
    snapshots = target_availability.read_snapshots()
    slot_roots = conf.read_or_repository("build_folders").values()
    mac_reading = queue_view.read_mac_builds()
    mac_machine = conf.read_or_repository("machines").words("mac.hosts")[0]
    slot_homes = {mac_machine: mac_reading.home, host_name(): str(Path.home())}
    labels = unit_labels.unit_labels_for(
        [*(step.session_id for step in (*parsed.view.running, *parsed.view.waiting)),
         *target_availability.holder_sessions(snapshots)], os.environ)
    zone = user_zone()
    routes = target_availability.MacRoutes(snapshots, labels, zone)
    ci_rows: dict[str, RunRow] = {}
    for entry in explanation.running:
        memory = entry.step.measured
        if isinstance(memory, admission_records.UnownedSliceMemory) and not isinstance(admission_records.recorded_service_state(memory), NotServiceSlice):
            ci_rows[memory.cgroup] = build_ci_row(gib(memory.bytes), explanation.now,
                Path(memory.cgroup) if memory.cgroup else CI_CGROUP,
                admitted=isinstance(admission_records.recorded_service_state(memory), RunningServiceJob))
    ci_runners = frozenset(runner["name"] for row in ci_rows.values() for runner in row.get("ci_runners", []))
    leftovers: list[LeftoverScope] = []
    for step, memory, activity in queue_view.leftover_processes(parsed.view.running):
        identity = labels.get(activity.session_id)
        owner = identity.session if identity is not None else activity.session_name or "unlabelled session"
        scope_members: list[LeftoverProcess] = [{"pid": member.pid, "program": member.program} for member in activity.members]
        if not scope_members:
            scope_members = [{"pid": step.pid, "program": activity.program}]
        leftovers.append({"owner": owner, "age": queue_view.leftover_age(age(activity.started_at, explanation.now))
                          if activity.started_at else "not recorded", "used": gib(memory.bytes), "members": scope_members})
    ram = ram_blocks(captured, build_tool_ram(explanation.running, captured.used),
                     explanation.now, configuration.other_rows)
    cap = gib(ram["cap_bytes"]) if isinstance(captured.cap, int) else captured.cap
    safety = gib(captured.safety)
    used = gib(captured.used)
    total = gib(host.total) if isinstance(host.total, int) else used + gib(captured.available)
    reserved = gib(sum(run.bytes for run in captured.runs))
    ci_ram, ci_name = gib(captured.ci_use), "hana CI"
    by_owner: dict[str, admission_records.RunningStep] = {}
    steps_by_owner: dict[str, list[admission_records.RunningStep]] = {}
    reserved_steps = (entry for entry in explanation.running if gate.running_has_reservation(entry.step))
    for reservation, entry in zip(explanation.decided_from.reservations, reserved_steps, strict=True):
        owner = gate.reservation_owner(reservation, entry.step)
        by_owner[owner] = entry.step
        steps_by_owner.setdefault(owner, []).append(entry.step)
    run_steps = {admission_records.run_owner(entry.step.run.pid, entry.step.run.start_time): entry.step
                 for entry in explanation.running if isinstance(entry.step.run, LiveBuildRun)}
    walk: list[RunRow] = []
    for hold in captured.runs:
        step = by_owner.get(hold.memory.owner)
        metadata = step if step is not None else run_steps.get(hold.memory.owner)
        identity = feed_step_labels(metadata, labels, ci_runners) if metadata is not None else unit_labels.UnitLabels("")
        unit = identity.unit
        ci_label = metadata.session_name if metadata is not None and metadata.unit in ci_runners else ""
        name = names.for_owner(step.step, unit if unit != "—" else "", identity.role,
            ci=bool(ci_label), current_name=step_text(step, names, labels=labels)) if step is not None else identity.session or hold.memory.owner
        detail = unit if step is not None else SWAP_NAMES["between"]
        use = gib(hold.memory.use.bytes) if isinstance(hold.memory.use, admission_records.AnonymousRunUse) else hold.memory.use
        run = hold.memory.run
        pid = (metadata.run.pid if isinstance(metadata.run, LiveBuildRun) else metadata.pid) if metadata is not None else (
            run.pid if isinstance(run, LiveBuildRun) else admission_records.PidNotRecorded())
        walk.append({"name": name, **page_owner_labels(identity, ci_label=ci_label), "between_steps": step is None,
                     "state": "", "pid": pid, "detail": detail, "used": use,
                     "slot": build_slot_number(metadata.folder, admission_records.execution_host(metadata, host_name()), slot_roots,
                        home=slot_homes.get(admission_records.execution_host(metadata, host_name()), ""))
                        if metadata is not None else "—",
                     "counted": use + gib(hold.bytes) if isinstance(use, float) else use, "reserved": gib(hold.bytes),
                     "started_at": metadata.started_at if metadata is not None else 0,
                     "progress": row_progress(steps_by_owner.get(hold.memory.owner, ())),
                     "queued": True, "keep_text": run_keep_text(hold, steps_by_owner.get(hold.memory.owner, ())), "group": "", "ci": False, "parts": [],
                     **(target_notes(step.route, routes, step.step, step.folder, step.package)
                        if step is not None and not ci_label else NO_TARGET_NOTES)})
    groups: dict[str, list[RunRow]] = {}
    remote_groups: set[str] = set()
    for entry in explanation.running:
        step = entry.step
        memory = step.measured
        identity = feed_step_labels(step, labels, ci_runners)
        name, detail = step_text(step, names, labels=labels), identity.unit
        unit, state = detail, ""
        if isinstance(memory, admission_records.RemoteMemoryUnavailable):
            key = admission_records.execution_group(step, entry.owner)
            if key in remote_groups:
                continue
            remote_groups.add(key)
            remote_row: RunRow = {"name": name, "showrunner": identity.showrunner, "unit": unit,
                "state": "running" if isinstance(step.execution, placement.ObservedExecution) else "uncertain",
                "pid": step.pid, "detail": detail, "used": admission_records.RunUseUnavailable(memory.reason),
                "counted": 0.0, "reserved": 0.0, "started_at": step.started_at, "progress": row_progress((step,)),
                "queued": False, "keep_text": memory.reason, "group": "", "ci": False, "parts": [],
                "machine": admission_records.execution_host(step, host_name()), "vcpus": queue_view.UnknownCpuCount(),
                "slot": build_slot_number(step.folder, admission_records.execution_host(step, host_name()), slot_roots,
                    home=slot_homes.get(admission_records.execution_host(step, host_name()), ""))}
            walk.append(remote_row)
            continue
        if isinstance(memory, (admission_records.UnownedSliceMemory, admission_records.CompilerServiceMemory)):
            service = (isinstance(memory, admission_records.CompilerServiceMemory)
                       or not isinstance(admission_records.recorded_service_state(memory), NotServiceSlice) or bool(memory.diagnostic))
            if service:
                name = memory.owner if isinstance(memory, admission_records.UnownedSliceMemory) else "compiler cache"
                activity = service_activity_text(memory, explanation.now)
                state = activity
                unit = memory.owner if isinstance(memory, admission_records.UnownedSliceMemory) else "sccache.service"
                detail = activity if isinstance(memory, admission_records.UnownedSliceMemory) else "sccache.service · " + activity
                if isinstance(memory, admission_records.UnownedSliceMemory):
                    ci_name = "hana CI"
                    description = memory.diagnostic
                else:
                    description = "Memory the compiler cache holds. The queue does not start it."
            else:
                description = "Memory these processes hold. The queue did not start them."
                if isinstance(memory, admission_records.UnownedSliceMemory) and isinstance(memory.activity, ScopeProcesses):
                    name = memory.activity.program
            row: RunRow = {"name": name, "showrunner": "", "unit": unit, "state": state, "pid": (step.run.pid if isinstance(step.run, LiveBuildRun) else step.pid), "detail": detail, "used": gib(memory.bytes),
                           "counted": 0.0, "reserved": 0.0, "started_at": step.started_at,
                           "age": queue_view.leftover_age(age(step.started_at, explanation.now)) if step.started_at else "not recorded",
                           "progress": row_progress((step,)),
                           "queued": False, "keep_text": description, "group": "", "ci": False, "parts": []}
            if service and isinstance(memory, admission_records.UnownedSliceMemory):
                row = ci_rows[memory.cgroup] if memory.cgroup in ci_rows else build_ci_row(gib(memory.bytes), explanation.now,
                                   Path(memory.cgroup) if memory.cgroup else CI_CGROUP,
                                   admitted=isinstance(admission_records.recorded_service_state(memory), RunningServiceJob))
                if memory.diagnostic:
                    row["keep_text"] += "\n" + memory.diagnostic
                name, detail = row["name"], row["detail"]
            if not service and isinstance(memory, admission_records.UnownedSliceMemory) and isinstance(memory.activity, ScopeProcesses) and memory.bytes < GIB // 2:
                program = memory.activity.program
                group = "Codex servers" if program in ("codex", "codex-real") else "unowned processes"
                row["detail"] = " · ".join(part for part in (detail, program) if part)
                groups.setdefault(group, []).append(row)
            else:
                walk.append(row)
    walk.sort(key=lambda row: row["started_at"])
    for name, members in groups.items():
        amount = sum(row["used"] for row in members if isinstance(row["used"], float))
        walk.append({"name": f"{name} ({len(members)})", "showrunner": "", "unit": "", "state": "", "pid": admission_records.PidNotRecorded(), "detail": "each under 0.5 GiB",
                     "used": amount, "counted": 0.0, "reserved": 0.0, "started_at": 0,
                     "queued": False, "keep_text": "Memory these processes hold. The queue did not start them.",
                     "progress": {"kind": "NoProgress", "reason": "group has no admission counter"},
                     "group": name, "ci": False, "parts": [{"who": row["detail"], "pid": row["pid"],
                         "gib": row["used"], "age": row.get("age", "")}
                         for row in sorted(members, key=lambda row: gib(row["used"]), reverse=True)
                         if isinstance(row["used"], float)]})
    machine = host_name()
    vcpus = os.cpu_count()
    for row in walk:
        _ = row.setdefault("machine", machine)
        _ = row.setdefault("vcpus", vcpus if vcpus is not None else queue_view.UnknownCpuCount())
    waiters: list[WaitRow] = []
    checks = {row.waiter.pid: row.room for row in explanation.line.steps}
    if isinstance(explanation.head, admission_records.HeadExplanation):
        checks[explanation.head.waiter.pid] = explanation.head.room
    for waiter in parsed.view.waiting:
        check = checks.get(waiter.pid)
        need = check.need if check is not None else waiter.need
        identity = feed_step_labels(waiter, labels, ci_runners)
        reason = reason_phrase(waiter.reason, parsed.view.waiting)
        if isinstance(waiter.reason, admission_records.BehindHead):
            reason += ": the first step in line gets memory first"
        ci_label = waiter.session_name if waiter.unit in ci_runners else ""
        waiters.append({"name": names.for_owner(waiter.step, identity.unit if identity.unit != "—" else "", identity.role,
                            ci=bool(ci_label), current_name=step_text(waiter, names, labels=labels)),
                        **page_owner_labels(identity, ci_label=ci_label),
                        "pid": (waiter.run.pid if isinstance(waiter.run, LiveBuildRun) else waiter.pid), "detail": "" if ci_label else identity.unit,
                        "need": gib(waiter.need), "since": waiter.since,
                        "reason": reason,
                        "fits": need <= captured.signed_room,
                        **(NO_TARGET_NOTES if ci_label else target_notes(
                            waiter.route, routes, waiter.step, waiter.folder, waiter.package))})
    work_rows: list[RunRow | CloudRunRow | MacBuildRow | MacReadingUnavailableRow] = list(walk)
    mac_status = queue_view.mac_reading_status(mac_reading, explanation.now)
    if mac_status:
        work_rows.append({"kind": "mac-unavailable", "name": "Mac builds", "unit": "—",
            "pid": admission_records.PidNotRecorded(), "machine": "mac", "started_at": 0,
            "read_at": mac_reading.read_at, "used": queue_view.MacMemoryUnavailable(),
            "stage": queue_view.MacStageUnavailable(), "vcpus": queue_view.MacJobsUnavailable(), "status": mac_status})
    else:
        mac_units = mac_build_units(mac_reading.builds, target_availability.mac_claims(snapshots))
        work_rows.extend(mac_work_row(build, mac_reading, mac_units.get(build.pid, unit_labels.UnitLabels("")),
                                     names=names, slot_roots=slot_roots, slot_machine=mac_machine)
                         for build in mac_reading.builds)
    work_rows.sort(key=lambda row: row["started_at"])
    waiting_rows: list[WaitRow | CloudWaitRow] = list(waiters)
    model = queue_view.QueueModel(
        parsed, labels, admission_records.NoWaiter(), head_facts.PaceUnavailable(), ZoneInfo("UTC"),
        queue_view.HostLoadUnavailable(), admission_records.NoWaiter(), names=names)
    cloud_slots_used = 0
    cloud_slots = 0
    if isinstance(parsed, wire.CloudQueueRead):
        cloud_slots = parsed.cloud_slots
        rows = queue_view.cloud_rows(model)
        cloud_slots_used = sum(isinstance(row.wait, admission_records.ReadyForAdmission) for row in rows)
        interval = queue_view.cloud_push_interval()
        cloud_waiters: dict[wire.Owner, CloudWaitRow] = {}
        for cloud in rows:
            information = queue_view.cloud_work(cloud, explanation.now, parsed.cloud_gate_stale_pushes, interval)
            identity = queue_view.cloud_step_labels(cloud, model)
            values: CloudWorkRow = {"kind": "cloud", "name": queue_view.cloud_step_name(cloud, model),
                **page_owner_labels(identity), "pid": cloud.owner.pid,
                "detail": identity.unit, "machine": "" if isinstance(cloud.wait, wire.CloudSlotWait) else "aws",
                "vcpus": information.vcpus,
                "state": information.state, "status": plain([queue_view.cloud_page_status(cloud, information)]),
                "progress": cloud_row_progress(cloud.counter),
                "stale": isinstance(information.update, queue_view.CloudUpdateLate)
                         and not isinstance(cloud.wait, wire.CloudSlotWait),
                "late_age": queue_view.cloud_update_age(information.update.seconds)
                    if isinstance(information.update, queue_view.CloudUpdateLate)
                    and not isinstance(cloud.wait, wire.CloudSlotWait) else ""}
            if isinstance(cloud.wait, wire.CloudSlotWait):
                cloud_waiters[cloud.owner] = {**values, "since": cloud.since, "reason": cloud.wait.text,
                    "runs_on": CLOUD_RUNS_ON}
            else:
                work_rows.append({**values, "started_at": cloud.started_at or cloud.since})
        work_rows.sort(key=lambda row: row["started_at"])
        waiting_rows = [cloud_waiters[entry.owner] if isinstance(entry, wire.CloudRow) else waiters[entry.position - 1]
                        for entry in queue_view.waiting_builds(model)]
    unmatched_priority_text = ""
    queued_priority_units: list[str] = []
    if isinstance(explanation.priority, admission_records.QueuePriority):
        priorities = {entry["unit"]: f"queue priority: {entry['unit']} ({entry['why']})"
                      for entry in explanation.priority.entries}
        queued_priority_units = list(dict.fromkeys(row["unit"] for row in waiting_rows if row["unit"] in priorities))
        matched_units: set[str] = set()
        for work_row in (*work_rows, *waiting_rows):
            if work_row["unit"] in priorities:
                work_row["priority_text"] = priorities[work_row["unit"]]
                matched_units.add(work_row["unit"])
        unmatched_priority_text = "\n".join(text for unit, text in priorities.items() if unit not in matched_units)
    if waiting_rows:
        head_pid = waiting_rows[0]["pid"]
        if isinstance(explanation.head, admission_records.HeadExplanation):
            head = explanation.head.waiter
            head_pid = head.run.pid if isinstance(head.run, LiveBuildRun) else head.pid
        for waiting_row in waiting_rows:
            if waiting_row["pid"] == head_pid:
                if isinstance(explanation.head, admission_records.HeadExplanation) and explanation.head.decision.state == "hold":
                    waiting_row["head_room"] = head_room(explanation.head.room, explanation.room)
                break
    pressure = explanation.decided_from.pressure
    policy = explanation.policy.pressure
    # Without the recorded builds and CI swap, the page reads host swap now instead of failing.
    swap_host = (LIVE_HOST_SWAP if isinstance(host.build_and_ci_swap, admission_records.ReadingUnavailable)
                 else RecordedHostSwap(host.swap_total - host.swap_free, host.swap_total, host.build_and_ci_swap))
    swap = read_swap(parsed.view.running, labels, configuration.swap_rows, names, swap_host)
    if isinstance(swap, SwapUnavailable):
        raise RuntimeError(swap.reason)
    pressure_values = read_stalls()
    pressure_label = "none"
    if isinstance(pressure, admission_records.MemoryPressure):
        pressure_label = admission_records.pressure_text(pressure)
    elif isinstance(pressure, admission_records.PressureUnavailable):
        pressure_label = "unavailable: " + pressure.reason
    return {
        "colors": configuration.colors,
        "ok": True, "read_at": explanation.now, "total": total, "available": gib(captured.available),
        "cap": cap, "floor": safety, "cap_text": room_text(captured),
        "shortfall": gib(captured.shortfall), "used": used, "reserved": reserved,
        "taken": gib(ram["cap_bytes"] - captured.room), "room": gib(captured.room), "room_source": "free",
        "limit": "room and head protection", "ram": ram,
        "memory_status": memory_status(explanation, captured, len(by_owner)),
        "ci_name": ci_name, "ci_ram": ci_ram,
        "waiting": len(waiting_rows), "smallest_need": min((step.need / GIB for step in parsed.view.waiting), default=0.0),
        "pressure": pressure_label,
        "swap": {"used_bytes": swap.used, "capacity_bytes": swap.total, "builds_ci_bytes": swap.builds_ci,
                 "outside_bytes": swap.used - swap.builds_ci,
                 "reserve_share_percent": explanation.policy.agent_swap_share_percent},
        "walk": work_rows, "waiters": waiting_rows, "swap_by": swap.rows, "psi": pressure_values,
        "priority_text": head_facts.priority_text(explanation.priority),
        "queued_priority_units": queued_priority_units,
        "unmatched_priority_text": unmatched_priority_text,
        "mac_idle_alert": "\n".join(text for text in (
            mac_idle_alert.alert_line(mac_idle_alert.state_dir() / "alert.json", time.time(), zone,
                block=mac_idle_alert.BlockText.OMITTED if routes.blocked() else mac_idle_alert.BlockText.SHOWN) or "",
            *(f"Mac unplaced run: pid {run.pid}; {run.command}" for run in mac_reading.unplaced_runs)) if text),
        "targets": target_availability.availability_rows(snapshots, labels, zone),
        "cloud_slots_used": cloud_slots_used, "cloud_slots": cloud_slots,
        "limits": {"some": policy.some_avg10_limit, "full": policy.full_avg10_limit},
        "report": report_reading(configuration.history_days) if isinstance(report, ReportNotRead) else report,
        "swap_rows": configuration.swap_rows, "leftovers": leftovers,
    }


def page_json(payload: MemoryPage | RetainedReading | PageUnavailable) -> str:
    """Preserve named unavailable readings at the JSON boundary."""
    return json.dumps(codec.snapshot_value(payload))


def notify_unplaced_mac_runs(reading: queue_view.MacBuildReading, notified: set[tuple[int, float]]) -> None:
    """Send builds one message with all new unplaced processes."""
    from scripts.buildlog import mac_idle

    if reading.error:
        return
    new_runs = tuple(run for run in reading.unplaced_runs if (run.pid, run.started_at) not in notified)
    if not new_runs:
        return
    text = "Mac runs have no queue placement:\n" + "\n".join(
        f"pid {run.pid}; {run.command}" for run in new_runs)
    text += "\nUse mac_queue_run.py for agent and SSH commands."
    keys = [f"mac-unplaced-{run.pid}-{run.started_at!r}" for run in new_runs]
    key = ";".join(keys)
    if mac_idle.notify_showrunner(text, identity=mac_idle.OutageMessage(int(new_runs[0].started_at), reading.read_at, key)):
        notified.update((run.pid, run.started_at) for run in new_runs)


def main(argv: Sequence[str]) -> int:
    args = page_arguments(argv)
    if args.command == "link":
        return page_link()
    configuration = read_configuration()
    publish_page(configuration)
    target = configuration.output_folder / "memory.json"
    last_good: MemoryPage | NoGoodReading = NoGoodReading()
    report: ReportSections | charts.IndexUnavailable | ReportNotRead = ReportNotRead.VALUE
    next_report_at = 0.0
    next_mac_at = 0.0
    notified_mac_runs: set[tuple[int, float]] = set()
    while True:
        if installed_feed_code_version(args.code_root) != LOADED_FEED_CODE_VERSION:
            print("The installed feed code changed. Restart the feed.", file=sys.stderr)
            return 1
        now = time.monotonic()
        if isinstance(report, ReportNotRead) or now >= next_report_at:
            report = report_reading(configuration.history_days)
            next_report_at = now + configuration.report_refresh_seconds
        try:
            if now >= next_mac_at:
                queue_view.refresh_mac_builds()
                if not args.once:
                    # A one-time proof run reads the Mac but sends no alert.
                    notify_unplaced_mac_runs(queue_view.read_mac_builds(), notified_mac_runs)
                next_mac_at = now + queue_view.mac_activity_refresh_seconds()
            payload: MemoryPage | RetainedReading | PageUnavailable = reading(configuration, report)
            last_good = payload
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            if isinstance(last_good, NoGoodReading):
                payload = {"ok": False, "error": str(error), "read_at": time.time()}
            else:
                payload = {**last_good, "read_error": str(error), "error_at": time.time()}
        write_text_atomic(target, page_json(payload), new_file_mode=0o644)
        if args.once:
            return 0
        time.sleep(configuration.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
