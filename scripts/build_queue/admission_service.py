"""The single admission loop owns reservations and durable queue state."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections import deque
from collections.abc import Generator, Mapping
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
import errno
import heapq
import json
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum, auto
from functools import cache
import os
from pathlib import Path
import select
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from types import MappingProxyType
from typing import Callable, Protocol, cast, override

from scripts.build_queue import execution_identity
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue import admission_wire as status_wire
from scripts.lib import conf, inotify
from scripts.lib.boot_identity import boot_identity
from scripts.lib.proc_text import read_proc_text
from scripts.build_queue import admission_code
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import placement
from scripts.build_queue import step_estimator
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import cloud_progress
from scripts.build_queue import queue_snapshot
from scripts.build_targets import target_config
from scripts.build_targets.execution_transport import ExecutionTransport, HanaGpuNextestTransport
from scripts.build_queue.admission_records import AdmissionWaitSnapshot, WaitHolder, reason_phrase
from scripts.build_queue.system_clock import SystemClock as SystemClock
from scripts.lint import memory_admit as gate
from scripts.build_hold import override_guard
from scripts.production import staff

from scripts.buildlog import repository, store
from scripts.lib import test_phase
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.lib.build_steps import gate_job_from_process

from scripts.build_queue.scope_activity import (
    IdleService, LiveBuildRun, NoLiveBuildRun, NotServiceSlice, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob,
    ScopeActivityReader, ScopeProcesses,
)
from scripts.lint.build_shape import build_shape, recorded_build_shape
from scripts.lib.atomic_file import atomic_sample, write_text_atomic
from scripts.accounts import outage_alerts




@dataclass
class ProviderConfiguration:
    """Reuse provider settings during one admission pass."""

    transports: dict[target_config.SshTarget, ExecutionTransport] = field(default_factory=dict)
    settings: dict[str | Path, conf.ConfigRead] = field(default_factory=dict)
    files: dict[Path, conf.ConfigurationBytes | OSError] = field(default_factory=dict)
    targets: dict[str, target_config.Target | records.UnregisteredTarget | conf.Unusable] = field(default_factory=dict)
    generations: dict[tuple[str, placement.ProviderSettings], str] = field(default_factory=dict)

    def registered_target(self, name: str) -> target_config.Target | records.UnregisteredTarget:
        if name not in self.targets:
            try:
                self.targets[name] = records.effective_target(name, placement.ConfiguredProviderSettings(),
                    configuration=self.read("build_targets"))
            except conf.Unusable as error:
                self.targets[name] = error
        target = self.targets[name]
        if isinstance(target, conf.Unusable):
            raise target
        return target

    def target(self, name: str, settings: placement.ProviderSettings
               ) -> target_config.Target | records.UnregisteredTarget:
        target = self.registered_target(name)
        return records.ssh_target_settings(target, settings) if isinstance(target, target_config.SshTarget) else target

    def transport(self, target: target_config.SshTarget) -> ExecutionTransport:
        if target not in self.transports:
            self.transports[target] = target_config.read_execution_transport(target, configuration=self.read(target.settings))
        return self.transports[target]

    def read(self, file: str | Path) -> conf.ConfigRead:
        if file not in self.settings:
            self.settings[file] = conf.read(file, load=self.snapshot)
        return self.settings[file]

    def snapshot(self, path: Path) -> conf.ConfigurationBytes:
        if path not in self.files:
            try:
                self.files[path] = conf.read_bytes(path)
            except OSError as error:
                self.files[path] = error
        contents = self.files[path]
        if isinstance(contents, OSError):
            raise contents
        return contents

    def contents(self, path: Path) -> bytes:
        return self.snapshot(path).data

    def generation(self, name: str, settings: placement.ProviderSettings) -> str:
        key = name, settings
        if key not in self.generations:
            try:
                target = self.registered_target(name)
            except conf.Unusable:
                # The probe reports the registry error for this target.
                target = records.UnregisteredTarget()
            targets = (target,) if isinstance(target, target_config.SshTarget) else ()
            self.generations[key] = status_wire.placement_configuration_digest(name, settings,
                targets=targets, transport_for=self.transport, read_bytes=self.contents)
        return self.generations[key]


_PASS_PROVIDER_CONFIGURATION: ContextVar[ProviderConfiguration] = ContextVar("pass_provider_configuration")


def _provider_configuration() -> ProviderConfiguration:
    current = _PASS_PROVIDER_CONFIGURATION.get(None)
    return ProviderConfiguration() if current is None else current


CARGO_CAPTURE_ROOT = Path("/tmp/cargo-tile")
READING_ALERT_KEY = "build-admission-reading"

PlacementEvidenceKeys = dict[tuple[str, placement.PlacementDestination], placement.HostEvidenceKey | placement.EvidenceNeeded]


@dataclass(frozen=True)
class NoReadingHold:
    """No unreadable reading currently holds a queued start."""


@dataclass(frozen=True)
class UnreadableReadingHold:
    since: float
    path: Path
    reason: str


@dataclass(frozen=True)
class NoReadingAlertDelivery:
    """No reading alert sender has started."""


@dataclass(frozen=True)
class ReadingAlertDelivery:
    thread: threading.Thread


def deliver_reading_alert(claimed: outage_alerts.AlertClaim, hold: UnreadableReadingHold, elapsed: float) -> None:
    """Send the claimed alert outside the admission pass."""
    from scripts.message import send_budget, send_result
    from scripts.message.session_send import session_send_argv

    delivered = False
    try:
        settings = conf.read("lint")
        command = session_send_argv("natedev", sender="build-admission", lint_settings=settings)
        message = (f"Build admission: {hold.path} is unreadable.\nReason: {hold.reason}\n"
                   f"This reading has held build starts for {elapsed:g} seconds.\n"
                   "Restore the reading on natedev. Admission holds starts until the reading works.")
        result = subprocess.run(command, input=message, text=True, capture_output=True,
                                timeout=send_budget.seconds(settings.number("sweep_sender_timeout_seconds")))
        if result.returncode in (send_result.EXIT["sent"], send_result.EXIT["queued"]):
            outage_alerts.delivered(claimed)
            delivered = True
        else:
            print(f"memory admission: reading alert failed: {result.stderr.strip()}", file=sys.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"memory admission: reading alert failed: {error}", file=sys.stderr)
    finally:
        if not delivered:
            outage_alerts.release(claimed)


@contextmanager
def retry_cleanup(path: Path) -> Generator[None]:
    """Keep failed cleanup records for the next pass."""
    try:
        yield
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"memory admission: cannot finish cleanup {path}: {error}; will retry", file=sys.stderr)


def _live_run(run: wire.RunIdentity) -> LiveBuildRun:
    return LiveBuildRun(run.pid, run.start_time, 0.0)


class Clock(Protocol):
    def now(self) -> float: ...
    def monotonic(self) -> float: ...


class Readings(Protocol):
    def host(self) -> records.HostMemory | records.ReadingUnavailable: ...
    def pressure(self, host: records.HostMemory, policy: records.AdmissionPolicy) -> records.Pressure: ...
    def build_space(self, floor: int) -> records.BuildSpaceHold | records.NoBuildSpaceHold: ...
    def compilers(self) -> list[records.StepMemory]: ...
    def reservation(self, path: Path, row: records.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path, policy: records.AdmissionPolicy) -> records.StepMemory: ...


class SystemReadings:
    def __init__(self) -> None:
        self.swap_in: gate.SwapInHistory = gate.SwapInHistory()

    def host(self) -> records.HostMemory | records.ReadingUnavailable:
        try:
            host = gate.read_meminfo(Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo")))
            window = conf.read("build_admission").whole("agent_swap_window_seconds")
            returning = self.swap_in.read(Path(os.environ.get("BUILDLOG_VMSTAT", "/proc/vmstat")), time.monotonic(), window)
            return replace(host, swap_in_window_bytes=returning) if isinstance(returning, int) else returning
        except (OSError, ValueError, conf.Unusable) as error:
            return records.ReadingUnavailable(str(error) or "memory reading unavailable")

    def pressure(self, host: records.HostMemory, policy: records.AdmissionPolicy) -> records.Pressure:
        return gate.read_pressure(host, policy=policy)

    def build_space(self, floor: int) -> records.BuildSpaceHold | records.NoBuildSpaceHold:
        from scripts.lib.build_slots import slot_cargo_root

        try:
            root = slot_cargo_root(conf.read("build_folders"))
            reading = os.statvfs(root)
        except (OSError, conf.Unusable) as error:
            hold = records.BuildSpaceUnreadable(str(error))
            print(f"memory admission: {records.build_space_text(hold)}", file=sys.stderr)
            return hold
        free = reading.f_bavail * reading.f_frsize // records.GIB
        return records.BuildSpaceLow(free, floor) if free < floor else records.NoBuildSpaceHold()

    def compilers(self) -> list[records.StepMemory]:
        return gate.sccache_memory()

    def reservation(self, path: Path, row: records.Reservation, now: datetime,
                    processes: dict[int, gate.ProcessIdentity], proc: Path, policy: records.AdmissionPolicy) -> records.StepMemory:
        return gate.reservation_memory(path, row, now, processes, proc, policy)


@dataclass(frozen=True)
class LiveOwnerIdentity:
    start_time: str


@dataclass(frozen=True)
class OwnerProcessGone:
    pass


@dataclass(frozen=True)
class OwnerProcessUnreadable:
    reason: str


OwnerStartReading = LiveOwnerIdentity | OwnerProcessGone | OwnerProcessUnreadable


def owner_may_be_live(reading: OwnerStartReading, start_time: str) -> bool:
    """Keep a known owner until a reading confirms that it ended."""
    return isinstance(reading, OwnerProcessUnreadable) or reading == LiveOwnerIdentity(start_time)


class Proc(Protocol):
    @property
    def path(self) -> Path: ...
    def processes(self) -> dict[int, gate.ProcessIdentity]: ...
    def owner_start(self, pid: int) -> OwnerStartReading: ...
    def pidfd(self, pid: int) -> int: ...
    def lifetime(self, row: records.Reservation, owner_live: bool
                 ) -> gate.StepLifetime: ...


class SystemProc:
    @property
    def path(self) -> Path:
        return Path(os.environ.get("BUILDLOG_PROC", "/proc"))

    def processes(self) -> dict[int, gate.ProcessIdentity]:
        return gate.process_table(self.path)

    def owner_start(self, pid: int) -> OwnerStartReading:
        if sys.platform == "darwin":
            from scripts.lib import process_start

            reading = process_start.process_start(pid)
            if isinstance(reading, process_start.Started):
                return LiveOwnerIdentity(reading.start)
            if isinstance(reading, process_start.Unreadable):
                return OwnerProcessUnreadable(reading.reason)
            return OwnerProcessGone()
        if pid <= 0:
            return OwnerProcessUnreadable("invalid process id")
        root = self.path
        if "BUILDLOG_PROC" not in os.environ and (verify_root := os.environ.get("VERIFY_PROC_ROOT")) is not None:
            root = Path(verify_root)
        try:
            stat = read_proc_text(root / str(pid) / "stat")
        except (FileNotFoundError, ProcessLookupError):
            return OwnerProcessGone()
        except OSError as error:
            return OwnerProcessUnreadable(str(error))
        _, separator, rest = stat.rpartition(")")
        fields = rest.split()
        if (not separator or len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdigit()
                or fields[0] not in ("R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I")):
            return OwnerProcessUnreadable("invalid process stat fields")
        return OwnerProcessGone() if fields[0] in ("Z", "X") else LiveOwnerIdentity(fields[19])

    def pidfd(self, pid: int) -> int:
        return os.pidfd_open(pid)

    def lifetime(self, row: records.Reservation, owner_live: bool
                 ) -> gate.StepLifetime:
        return gate.reservation_lifetime(row, owner_live)


@dataclass(frozen=True)
class OrderNotStarted:
    pass


@dataclass(frozen=True)
class TokenNotLeft:
    pass


def matches_build_estimate(repo: str, repo_identity: str, shape: str,
                           profile: records.StepProfile, estimate_shape: str) -> bool:
    return (step_estimator.history_matches_repository({"repo": repo, "repo_identity": repo_identity}, profile.repository)
            and (not shape or not estimate_shape
                 or recorded_build_shape(shape) == recorded_build_shape(estimate_shape)))


@dataclass(frozen=True)
class WaiterPlace:
    owner: wire.Owner
    since: float
    episode: str
    priority_since: float | OrderNotStarted = OrderNotStarted()
    token_left_at: float | TokenNotLeft = TokenNotLeft()
    order_since: float | OrderNotStarted = field(default_factory=OrderNotStarted, kw_only=True)
    queue_identity: records.QueueLifeIdentity = field(kw_only=True)


def waiter_place(row: records.Waiter, episode: str) -> WaiterPlace:
    """Keep a logical identity with every active place."""
    return WaiterPlace(wire.Owner(row["pid"], row["start_time"]), row["since"], episode,
        row.get("priority_since", OrderNotStarted()), row.get("token_left_at", TokenNotLeft()),
        order_since=row.get("order_since", row["since"]), queue_identity=gate.queue_life_identity(row))


@dataclass(frozen=True)
class NotEnding:
    pass


@dataclass(frozen=True)
class ReleaseStatus:
    request_id: str
    status: int


@dataclass(frozen=True)
class OwnerExited:
    pass


@dataclass(frozen=True)
class NoFinalizationIntent:
    pass


@dataclass(frozen=True)
class TokenWaiting:
    request_id: str
    left_at: float


@dataclass(frozen=True)
class ReservationRecord:
    """Service-owned reservation: width and call absence have domain names."""
    episode: str
    path: str
    owner: wire.Owner
    need: int
    repo: str
    step: str
    worktree: str
    admitted_at: str
    sidecar: str
    test_width: records.TestWidth
    call: records.Call
    reservation_id: str
    queue_waiter: records.Waiter
    repo_identity: str = ""
    test_scope: records.TestScope = "full"
    package: str = ""
    filters: tuple[str, ...] = ()
    shape: str = ""
    test_phase_need: records.TestPhaseNeed | gate.NoTestPhaseNeed = gate.NoTestPhaseNeed("not_recorded")
    ending: NotEnding | ReleaseStatus | OwnerExited | TokenWaiting = NotEnding()
    finalization: records.FinalizationIntent | NoFinalizationIntent = NoFinalizationIntent()
    charge_use: test_phase.TestPhaseChargeUse | test_phase.NoTestPhaseChargeUse = test_phase.NoTestPhaseChargeUse()
    run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
    place: wire.LocalPlace | wire.CloudPlace = wire.LocalPlace()
    slot_staff: staff.StaffId | records.NoEndedStepStaff = records.NoEndedStepStaff()
    cargo_folder: placement.CargoFolder | placement.NoCargoFolder = placement.NoCargoFolder()
    gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED
    cloud_launch_seen: bool = False


def admitted_gate_job(request: wire.AdmitRequest, proc: Path) -> StepJob | GateJobNotRecorded:
    """Keep the request's gate job or read the local caller's command."""
    if isinstance(request.identity.job, StepJob):
        return request.identity.job
    step = request.profile.step
    if isinstance(request.place, wire.LocalPlace) and isinstance(request.run, wire.RunIdentity):
        job = gate_job_from_process(step, proc / str(request.owner.pid), start_time=request.owner.start_time)
        if isinstance(job, StepJob):
            return job
    return GATE_JOB_NOT_RECORDED


def step_staff(row: records.Waiter, host: str) -> staff.StaffId | records.NoEndedStepStaff:
    """Resolve the fixed staff ID while the caller's step is live."""
    try:
        members = staff.local_snapshot()
    except staff.StaffUnreadable:
        return records.NoEndedStepStaff()
    reference = row.get("session_id", "")
    member = members.by_member_reference(reference) if reference else None
    folder = Path(row.get("folder", ""))
    if member is None and folder.is_absolute():
        member = members.unit_by_worktree(host, folder) or members.by_worktree(host, folder)
    return member.id if member is not None else records.NoEndedStepStaff()


def reservation_row(record: ReservationRecord) -> records.Reservation:
    """Project service state into the shared reservation record."""
    row: records.Reservation = {
        "pid": record.owner.pid, "start_time": record.owner.start_time, "need": record.need,
        "repo": record.repo, "step": record.step, "worktree": record.worktree,
        "admitted_at": record.admitted_at, "sidecar": record.sidecar,
        "test_threads": record.test_width, "call_id": record.call,
        "reservation_id": record.reservation_id, "queue_waiter": record.queue_waiter,
        "scoped": bool(record.sidecar) and isinstance(record.place, wire.LocalPlace), "test_scope": record.test_scope,
        "package": record.package, "filters": list(record.filters),
    }
    if isinstance(record.run, wire.RunIdentity):
        row["build_run"] = _live_run(record.run)
        row["run_origin"] = records.CallerRun(row["build_run"])
        row["slot_staff"] = record.slot_staff
    if isinstance(record.gate_job, StepJob):
        row["job"] = record.gate_job
    if record.repo_identity:
        row["repo_identity"] = record.repo_identity
    if record.shape:
        row["shape"] = record.shape
    if not isinstance(record.test_phase_need, gate.NoTestPhaseNeed) or record.test_phase_need.reason != "not_recorded":
        row["test_phase_need"] = gate.test_phase_need_record(record.test_phase_need)
    if not isinstance(record.finalization, NoFinalizationIntent):
        row["finalization"] = record.finalization
    if not isinstance(record.charge_use, test_phase.NoTestPhaseChargeUse):
        row["test_phase_charge_use"] = record.charge_use
    return row


@cache
def gate_worktree_repository_name(worktree: str) -> str | records.UnknownRepository:
    """Read the Git repository name for this gate worktree."""
    try:
        key = repository.repository_key(worktree)
        if key == store.UNKNOWN_IDENTITY or key.startswith(repository.MANIFEST_IDENTITY_PREFIX):
            return records.UnknownRepository()
        if key.startswith("git-root:"):
            return repository.repo_name(worktree) or records.UnknownRepository()
        return key.rsplit("/", 1)[-1] or records.UnknownRepository()
    except (OSError, ValueError):
        return records.UnknownRepository()


def gate_repository_name(step: str, repository_name: str, worktree: str) -> str:
    """Resolve the Git repository for a gate with a legacy program profile."""
    if step in records.REPOSITORY_RUN_STEPS and Path(repository_name).is_absolute() and Path(worktree).is_absolute():
        name = gate_worktree_repository_name(worktree)
        if isinstance(name, str):
            return name
    return repository_name


@dataclass(frozen=True)
class LiveGrant:
    pass


@dataclass(frozen=True)
class ReplayEntry:
    reply: wire.Reply
    ended_at: float | LiveGrant
    reservation: str = ""


class ReplayLedger(dict[str, ReplayEntry]):
    """Index live replies and expiry without scanning ended replies each pass."""

    def __init__(self, entries: dict[str, ReplayEntry]) -> None:
        super().__init__()
        self.revision: int = 0
        self.live: set[str] = set()
        self.ended: list[tuple[float, int, str]] = []
        for key, entry in entries.items():
            self[key] = entry

    @override
    def __setitem__(self, key: str, entry: ReplayEntry) -> None:
        if self.get(key) is entry:
            return
        super().__setitem__(key, entry)
        self.revision += 1
        if isinstance(entry.ended_at, LiveGrant):
            self.live.add(key)
        else:
            self.live.discard(key)
            heapq.heappush(self.ended, (entry.ended_at, self.revision, key))

    @override
    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self.live.discard(key)
        self.revision += 1

    def expire(self, cutoff: float, protected_keys: set[str], reservations: set[str]) -> None:
        retained: list[tuple[float, int, str]] = []
        while self.ended and self.ended[0][0] <= cutoff:
            at, revision, key = heapq.heappop(self.ended)
            entry = self.get(key)
            if entry is None or entry.ended_at != at:
                continue
            if key in protected_keys or entry.reservation in reservations:
                retained.append((at, revision, key))
            else:
                del self[key]
        for item in retained:
            heapq.heappush(self.ended, item)


@dataclass(frozen=True)
class PendingAdmission:
    episode: str
    need_source: records.NeedSource
    is_head: bool
    wait_seconds: float


@dataclass(frozen=True)
class PendingLeft:
    identity: records.QueueLifeIdentity
    episode: str
    reason: gate.LeftReason
    ended_at: float


@dataclass(frozen=True)
class PendingCloudGroup:
    """Keep the group's place until admission or coordinator exit."""
    target: str
    group: status_wire.CloudGroup


@dataclass(frozen=True)
class ServiceState:
    reservations: dict[str, ReservationRecord]
    places: dict[str, WaiterPlace]
    ledger: dict[str, ReplayEntry]
    spacing: records.StartSpacing
    priority: records.QueuePriority | records.NoQueuePriority = records.NO_QUEUE_PRIORITY
    build_history_generation: int = 0

    run_places: dict[str, wire.RunPlace] = field(default_factory=dict)
    caller_versions: dict[str, str] = field(default_factory=dict)
    lifecycle_outbox: tuple[PendingAdmission | PendingLeft, ...] = ()
    placement_requests: dict[str, wire.PlacementRequest] = field(default_factory=dict)
    assignments: dict[str, records.SavedPlacementAssignment] = field(default_factory=dict)
    current_assignments: dict[str, str] = field(default_factory=dict)
    pending_cloud_groups: dict[str, PendingCloudGroup] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemReadingPlan:
    """The reader owns these records until it publishes the next reading."""
    config: records.MemoryConfig
    reservations: tuple[ReservationRecord, ...]
    runs: tuple[wire.RunPlace, ...]
    waiting: bool


@dataclass(frozen=True)
class MissingSample:
    """The sampler has not written this file."""


@dataclass(frozen=True)
class UnreadableSample:
    """The sampler file could not be read."""


@dataclass(frozen=True)
class SampleFile:
    text: str | MissingSample | UnreadableSample
    modified_at: float | records.PeakTimeUnknown


def sample_file(path: Path) -> SampleFile:
    try:
        text: str | MissingSample | UnreadableSample = path.read_text()
    except FileNotFoundError:
        text = MissingSample()
    except (OSError, UnicodeError):
        text = UnreadableSample()
    try:
        modified: float | records.PeakTimeUnknown = path.stat().st_mtime
    except OSError:
        modified = records.PeakTimeUnknown()
    return SampleFile(text, modified)


@dataclass(frozen=True)
class ReservationEvidence:
    """Physical memory and sampler observations without a policy charge."""
    memory: records.StepMemory
    peak: SampleFile
    wave: SampleFile
    phase: test_phase.ObservedTestPhase | test_phase.UnobservedTestPhase
    scope: str


def raw_reservation_memory(record: ReservationRecord, row: records.Reservation,
                           lifetime: gate.StepLifetime, processes: dict[int, gate.ProcessIdentity],
                           proc: Path) -> records.StepMemory:
    _ = record
    if isinstance(lifetime, gate.EndedStep):
        return records.GoneMemory()
    if isinstance(lifetime, gate.PopulatedStepScope):
        try:
            return gate.process_memory(lifetime, proc)
        except FileNotFoundError:
            identity = processes.get(row["pid"])
            if identity is None or identity.start != row["start_time"]:
                return records.GoneMemory()
    return gate.tree_memory(row["pid"], row["start_time"], processes, proc)


def reservation_charge(evidence: ReservationEvidence, row: records.Reservation,
                       now: float, policy: records.AdmissionPolicy) -> records.StepMemory:
    """Apply the current reservation and policy to unchanged physical evidence."""
    memory = gate.step_promise(row, evidence.memory)
    phase = gate.test_phase_history(row, memory, evidence.phase)
    if isinstance(memory, records.MeasuredMemory) and isinstance(phase, records.TestPhaseStarted):
        return replace(memory, peak_history=phase)
    try:
        if isinstance(evidence.peak.text, UnreadableSample):
            raise ValueError("unreadable peak")
        peak = 0 if isinstance(evidence.peak.text, MissingSample) else int(evidence.peak.text)
        if peak < 0:
            raise ValueError("negative peak")
    except ValueError:
        return (replace(memory, unknown_peak_promise=row["need"], peak_history=records.PeakFileUnreadable(row["need"]))
                if isinstance(memory, records.MeasuredMemory) else memory)
    last_near_peak = now
    peak_at = evidence.peak.modified_at if peak > 0 else records.PeakTimeUnknown()
    peak_time: float | records.PeakTimeUnknown = records.PeakTimeUnknown()
    old_sampler = False
    if isinstance(evidence.wave.text, MissingSample) and peak > 0:
        old_sampler = True
        if isinstance(peak_at, float):
            last_near_peak = peak_at
            peak_time = peak_at
    elif isinstance(evidence.wave.text, str):
        try:
            sampled, recorded = map(int, evidence.wave.text.split())
            if sampled == peak and recorded >= 0:
                last_near_peak = recorded
                peak_time = recorded
        except ValueError:
            pass
    history: records.PeakHistory = (records.PeakSoFar(peak, peak_at, peak_time, old_sampler)
                                if peak > 0 else records.NoPeakRecorded())
    hold = policy.peak_hold.seconds_for(row.get("step", ""))
    if not gate.release_build(row) and now - last_near_peak >= hold:
        history = records.PeakDecayed(0, peak, last_near_peak, last_near_peak + hold, old_sampler)
        peak = 0
    if isinstance(memory, records.MeasuredMemory):
        return replace(memory, observed_peak=peak, peak_history=history)
    if isinstance(memory, records.PromisedMemory):
        return records.PromisedMemory(max(memory.bytes, peak))
    return memory


@dataclass(frozen=True)
class SystemEvidence:
    read_at: float
    started_monotonic: float
    processes: Mapping[int, gate.ProcessIdentity]
    lifetimes: Mapping[str, gate.StepLifetime]
    reservations: Mapping[str, ReservationEvidence]
    # The reader thread is done with this reader when it publishes the reading; only the pass uses it after that.
    activity: ScopeActivityReader = field(compare=False)
    unowned: tuple[records.UnownedSliceMemory, ...]
    scope_members: Mapping[str, frozenset[int]]
    scope_process_memory: Mapping[int, records.MeasuredMemory]
    scope_workers: Mapping[str, frozenset[int]]
    between: tuple[records.RunningStep, ...]
    host: records.HostMemory | records.ReadingUnavailable
    pressure: records.Pressure
    compilers: tuple[records.StepMemory, ...]
    reading_identity: object = field(default_factory=object, init=False, compare=False, repr=False)

    def build_run(self, pid: int) -> LiveBuildRun | NoLiveBuildRun:
        """Find one owner's build run from this reading's process table."""
        return self.activity.run(pid)


def read_system(plan: SystemReadingPlan, readings: Readings, proc: Proc, clock: Clock, *,
                reservation_reader: Callable[[ReservationRecord, records.Reservation, gate.StepLifetime,
                    dict[int, gate.ProcessIdentity], Path], records.StepMemory] = raw_reservation_memory) -> SystemEvidence:
    """Read operating-system evidence without access to mutable service state."""
    started_monotonic = clock.monotonic()
    now = clock.now()
    processes = proc.processes()
    activity = ScopeActivityReader(processes, proc.path,
        tuple(LiveBuildRun(place.run.pid, place.run.start_time, place.since) for place in plan.runs))
    lifetimes: dict[str, gate.StepLifetime] = {}
    reservations: dict[str, ReservationEvidence] = {}
    for record in plan.reservations:
        row = reservation_row(record)
        live = owner_may_be_live(proc.owner_start(record.owner.pid), record.owner.start_time)
        lifetime = proc.lifetime(row, live)
        lifetimes[record.episode] = lifetime
        memory = reservation_reader(record, row, lifetime, processes, proc.path)
        if isinstance(memory, records.MeasuredMemory):
            memory = replace(memory, promise=records.NoPromise(), observed_peak=0,
                             unknown_peak_promise=0, peak_history=records.NoPeakRecorded())
        scope = ""
        if record.sidecar:
            try:
                scope = Path(record.sidecar).read_text().strip()
            except OSError:
                pass
        reservations[record.episode] = ReservationEvidence(memory,
            sample_file(Path(record.path).with_suffix(".max")),
            sample_file(Path(record.path).with_suffix(".wave")), gate.observed_test_phase(row), scope)
    # Capture every scope before the loop applies current reservation ownership.
    unowned = gate.unowned_slice_memory([], processes, proc.path,
        activity_reader=activity, ci_memory_slice=plan.config.ci_memory_slice)
    has_caller = plan.runs or any(isinstance(row.activity, ScopeProcesses) and row.activity.session_id for row in unowned)
    staff_members = records.queue_staff() if has_caller else records.NO_STAFF_LOOKUP

    def scope_caller(evidence: ScopeProcesses, unit: str = "") -> records.QueueCaller:
        return records.queue_caller(evidence.session_id, "", evidence.session_name, "", evidence.folder,
                                    members=staff_members, unit=unit)

    unowned = tuple(replace(row, activity=replace(evidence, session_name=scope_caller(evidence).name))
                    if isinstance(evidence := row.activity, ScopeProcesses) else row for row in unowned)
    members: dict[str, frozenset[int]] = {}
    workers: dict[str, frozenset[int]] = {}
    for reading in unowned:
        if not reading.cgroup:
            continue
        try:
            members[reading.cgroup] = frozenset(gate.scope_members(Path(reading.cgroup)))
        except (OSError, ValueError):
            members[reading.cgroup] = frozenset()
        if not isinstance(records.recorded_service_state(reading), NotServiceSlice):
            programs = gate.member_programs(members[reading.cgroup].intersection(processes), proc.path)
            workers[reading.cgroup] = frozenset(pid for pid, name in programs.items() if name == "Runner.Worker")
    scope_process_memory = {pid: gate.process_memory({pid}, proc.path)
                            for pid in {member for group in members.values() for member in group}}
    between: list[records.RunningStep] = []
    represented = {(row.activity.run.pid, row.activity.run.start_time) for row in unowned
                   if isinstance(row.activity, ScopeProcesses) and isinstance(row.activity.run, LiveBuildRun)}
    represented.update((record.run.pid, record.run.start_time) for record in plan.reservations
                       if isinstance(record.run, wire.RunIdentity))
    for place in plan.runs:
        run = place.run
        if (run.pid, run.start_time) in represented:
            continue
        identity = processes.get(run.pid)
        if identity is None or identity.start != run.start_time:
            continue
        evidence = activity.scope({run.pid})
        if not isinstance(evidence, ScopeProcesses):
            continue
        live_run = LiveBuildRun(run.pid, run.start_time, activity.started_at(run.pid) or place.since)
        measured = gate.tree_memory(run.pid, run.start_time, processes, proc.path)
        if isinstance(measured, records.GoneMemory):
            continue
        caller = scope_caller(evidence, place.unit)
        memory = records.UnownedSliceMemory(measured.bytes, swap_bytes=measured.swap_bytes,
            pid=run.pid, activity=replace(evidence, run=live_run, session_name=caller.name))
        between.append(records.RunningStep(caller.session_id, caller.name, "between steps",
            evidence.folder, 0, memory, live_run, live_run.started_at, pid=run.pid, unit=caller.unit))
    host = readings.host()
    # Retain all pressure values so a pending policy can use the same reading.
    pressure_policy = replace(plan.config.policy,
        pressure=replace(plan.config.policy.pressure, full_avg10_limit=-1))
    pressure = readings.pressure(host if isinstance(host, records.HostMemory) else
                                 records.HostMemory(0, records.UnknownHostTotal()), pressure_policy)
    return SystemEvidence(now, started_monotonic, MappingProxyType(processes),
        MappingProxyType(lifetimes), MappingProxyType(reservations), activity,
        tuple(unowned), MappingProxyType(members), MappingProxyType(scope_process_memory), MappingProxyType(workers),
        tuple(between), host, pressure, tuple(readings.compilers()))


@dataclass(frozen=True)
class NoReadingFailure:
    """The reader has no exception for the loop."""


class SystemReader(Protocol):
    @property
    def latest(self) -> SystemEvidence: ...
    def update(self, plan: SystemReadingPlan) -> None: ...
    def request_read(self) -> None: ...
    def raise_failure(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class NoSystemReader:
    """Direct callers collect evidence before a pass; the serving loop uses a reader."""


class OperatingSystemReader:
    """One thread publishes complete readings at the existing pass cadence."""

    def __init__(self, plan: SystemReadingPlan, initial: SystemEvidence,
                 readings: Readings, proc: Proc, clock: Clock, wake: int) -> None:
        self.plan: SystemReadingPlan = plan
        self.reading: SystemEvidence = initial
        self.readings: Readings = readings
        self.proc: Proc = proc
        self.clock: Clock = clock
        self.wake: int = wake
        self.lock: threading.Lock = threading.Lock()
        self.stopped: threading.Event = threading.Event()
        self.requested: threading.Event = threading.Event()
        self.failure: Exception | NoReadingFailure = NoReadingFailure()
        self.thread: threading.Thread = threading.Thread(target=self._read, name="admission-readings", daemon=True)
        self.thread.start()

    @property
    def latest(self) -> SystemEvidence:
        with self.lock:
            return self.reading

    def update(self, plan: SystemReadingPlan) -> None:
        with self.lock:
            faster = plan.waiting and not self.plan.waiting
            self.plan = plan
            if faster:
                self.requested.set()

    def request_read(self) -> None:
        self.requested.set()

    def raise_failure(self) -> None:
        with self.lock:
            failure, self.failure = self.failure, NoReadingFailure()
        if isinstance(failure, Exception):
            raise failure

    def _read(self) -> None:
        while not self.stopped.is_set():
            with self.lock:
                plan = self.plan
            interval = plan.config.waiting_pass_seconds if plan.waiting else plan.config.running_pass_seconds
            _ = self.requested.wait(interval)
            self.requested.clear()
            if self.stopped.is_set():
                return
            with self.lock:
                plan = self.plan
            try:
                reading = read_system(plan, self.readings, self.proc, self.clock)
            except Exception as error:
                with self.lock:
                    self.failure = error
            else:
                with self.lock:
                    self.reading = reading
            os.eventfd_write(self.wake, 1)

    def close(self) -> None:
        self.stopped.set()
        self.requested.set()
        self.thread.join()


@dataclass(frozen=True)
class DispatchEvidenceProbe:
    """The probe reads dispatch ownership for one saved assignment outside the service loop."""
    key: placement.HostEvidenceKey
    saved: records.SavedPlacementAssignment
    read: Callable[[], placement.DispatchPathState | wire.ExecutionReport]


@dataclass(frozen=True)
class DispatchEvidence:
    """The reading applies only to the saved assignment that requested it."""
    saved: records.SavedPlacementAssignment
    result: placement.DispatchPathState | wire.ExecutionReport
    observed_at: float


class HostEvidenceWorker:
    """One target reader publishes evidence without blocking the service loop."""

    def __init__(self, key: placement.HostEvidenceKey,
                 probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence],
                 clock: Clock, wake: int, interval: float) -> None:
        self.key: placement.HostEvidenceKey = key
        self.probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] = probe
        self.clock: Clock = clock
        self.wake: int = wake
        self.interval: float = interval
        self.lock: threading.Lock = threading.Lock()
        self.observations: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        self.dispatch_probes: dict[str, DispatchEvidenceProbe] = {}
        self.dispatch_observations: dict[str, DispatchEvidence] = {}
        self.due: dict[placement.HostEvidenceKey, float] = {key: 0.0}
        self.subscriptions: dict[placement.HostEvidenceKey, object] = {key: object()}
        self.stopped: threading.Event = threading.Event()
        self.requested: threading.Event = threading.Event()
        self.thread: threading.Thread = threading.Thread(target=self._read, name="placement-evidence-" + key.destination.target, daemon=True)
        self.thread.start()

    def evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        with self.lock:
            return self.observations.get(key, placement.HostEvidenceUnavailable(key, self.clock.now(), "not read"))

    def publish(self, evidence: placement.HostEvidence) -> None:
        with self.lock:
            self.observations[evidence.key] = evidence

    def dispatch_pending(self, probe: DispatchEvidenceProbe) -> bool:
        """Request a worker reading and retain ownership until the exact reading arrives."""
        assignment = probe.saved.assignment
        attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        with self.lock:
            previous = self.dispatch_probes.get(attempt)
            if previous is None or previous.saved != probe.saved:
                self.dispatch_probes[attempt] = probe
                self.due[probe.key] = float('-inf')
                self.requested.set()
            observed = self.dispatch_observations.get(attempt)
            return (observed is None or observed.saved != probe.saved
                    or time.monotonic() - observed.observed_at > self.interval
                    or not isinstance(observed.result, placement.DispatchPathSettled))

    def recovered_dispatch(self, saved: records.SavedPlacementAssignment) -> wire.ExecutionReport | placement.NoTerminalReport:
        """Read the settled report for the same saved assignment."""
        assignment = saved.assignment
        attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        with self.lock:
            observed = self.dispatch_observations.get(attempt)
            if observed is not None and observed.saved == saved and isinstance(observed.result, wire.ExecutionReport):
                return observed.result
        return placement.NoTerminalReport()

    def request(self, key: placement.HostEvidenceKey) -> None:
        with self.lock:
            if key in self.due:
                return
            self.due[key] = 0.0
            self.subscriptions[key] = object()
        self.requested.set()

    def expedite(self, key: placement.HostEvidenceKey) -> None:
        """Probe this key before every other due key."""
        with self.lock:
            if key not in self.due:
                return
            self.due[key] = float("-inf")
        self.requested.set()

    def remove(self, key: placement.HostEvidenceKey) -> None:
        with self.lock:
            _ = self.due.pop(key, None)
            _ = self.subscriptions.pop(key, None)
            _ = self.observations.pop(key, None)
            for attempt, probe in tuple(self.dispatch_probes.items()):
                if probe.key == key:
                    del self.dispatch_probes[attempt]
                    _ = self.dispatch_observations.pop(attempt, None)
        self.requested.set()

    def _next_ready(self, now: float) -> tuple[placement.HostEvidenceKey, object] | float | None:
        """Return the earliest due key and its subscription, or the delay until a key is due.

        Each turn probes one key, so an expedited key waits for one probe at most.
        """
        ready = min(((due, order, key) for order, (key, due) in enumerate(self.due.items()) if due <= now),
                    default=None)
        if ready is not None:
            return ready[2], self.subscriptions[ready[2]]
        return max(0.0, min(self.due.values()) - now) if self.due else None

    def _read(self) -> None:
        while not self.stopped.is_set():
            self.requested.clear()
            with self.lock:
                if self.stopped.is_set():
                    return
                ready = self._next_ready(time.monotonic())
            if not isinstance(ready, tuple):
                _ = self.requested.wait(ready)
            elif not self._probe(*ready):
                return

    def _probe(self, key: placement.HostEvidenceKey, subscription: object) -> bool:
        """Probe one key and publish its evidence. Return False when the worker stops."""
        observed_at = self.clock.now()
        try:
            evidence = self.probe(key)
            if evidence.key != key:
                raise ValueError("provider evidence has a different policy key")
            if isinstance(evidence, placement.FreshHostEvidence):
                evidence = replace(evidence, observed_at=min(evidence.observed_at, observed_at))
        except Exception as error:
            evidence = placement.HostEvidenceUnavailable(key, self.clock.now(), str(error))
        with self.lock:
            dispatches = tuple((attempt, probe) for attempt, probe in self.dispatch_probes.items() if probe.key == key)
        dispatches_read: list[tuple[str, DispatchEvidenceProbe, DispatchEvidence]] = []
        for attempt, probe in dispatches:
            try:
                result = probe.read()
            except Exception:
                result = placement.DispatchPathPending('The dispatch evidence is unavailable')
            dispatches_read.append((attempt, probe, DispatchEvidence(probe.saved, result, time.monotonic())))
        with self.lock:
            if self.subscriptions.get(key) is not subscription:
                return True
            self.observations[key] = evidence
            for attempt, probe, observed in dispatches_read:
                if self.dispatch_probes.get(attempt) is probe:
                    self.dispatch_observations[attempt] = observed
            self.due[key] = time.monotonic() + self.interval
            if self.stopped.is_set():
                return False
            try:
                os.eventfd_write(self.wake, 1)
            except OSError:
                return False
        return True

    def close(self) -> None:
        with self.lock:
            self.stopped.set()
        self.requested.set()


def same_remote_resource(first: placement.PlacementDestination, second: placement.PlacementDestination) -> bool:
    """Return whether two remote destinations share one target and an overlapping scope."""
    from scripts.mac_test.mac_test import scopes_overlap

    return (first.provider == second.provider and first.target == second.target
            and scopes_overlap(first.scope, second.scope))


@dataclass(frozen=True)
class PlacementHold:
    """An older waiting request that permits a remote destination but has no current free reading for it."""

    episode: str
    destination: placement.PlacementDestination
    key: placement.HostEvidenceKey
    observed_at: float


@dataclass(frozen=True)
class HostEvidenceReader:
    """Retain each policy key while one target worker owns the probes."""

    key: placement.HostEvidenceKey
    worker: HostEvidenceWorker
    timeout: float

    @property
    def latest(self) -> placement.HostEvidence:
        return self.worker.evidence(self.key)

    @property
    def thread(self) -> threading.Thread:
        return self.worker.thread

    def publish(self, evidence: placement.HostEvidence) -> None:
        self.worker.publish(evidence)

    def close(self) -> None:
        self.worker.close()


@dataclass(frozen=True)
class DurableRecordEncoding:
    value: object
    encoded: str
    key: str


def durable_copy(value: object) -> object:
    """Retain a comparison value even when a nested record can change."""
    if is_dataclass(value) and not isinstance(value, type):
        return replace(value, **{member.name: durable_copy(cast(object, getattr(value, member.name)))
                                  for member in fields(value) if member.init})
    if isinstance(value, Mapping):
        return {key: durable_copy(item) for key, item in cast(Mapping[str, object], value).items()}
    if isinstance(value, tuple):
        return tuple(durable_copy(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, list):
        return [durable_copy(item) for item in cast(list[object], value)]
    return value


@dataclass
class PublishedReplies:
    view: wire.ViewReply
    explain: wire.ExplainReply
    view_bytes: bytes = b""
    explain_bytes: bytes = b""
    mine_bytes: bytes = b""


@dataclass(frozen=True)
class NoConnection:
    pass


NO_CONNECTION = NoConnection()


@dataclass(frozen=True)
class NoRequest:
    pass


@dataclass(frozen=True)
class NoSubmittedPlacementRequest:
    """No submitted request remains for the assignment's provider evidence."""


@dataclass(frozen=True)
class NoWaitReply:
    pass


NO_WAIT_REPLY = NoWaitReply()


@dataclass(frozen=True)
class SourcesUnchanged:
    pass


@dataclass(frozen=True)
class SourceMissing:
    pass


@dataclass(frozen=True)
class OwnerWatch:
    owner: wire.Owner


@dataclass(frozen=True)
class ScopeWatch:
    path: Path
    episode: str


@dataclass
class Connection:
    socket: socket.socket
    received: bytes = b""
    sending: bytes = b""
    request: wire.Request | NoRequest = NoRequest()
    finished: bool = False
    version: int = 1


@dataclass(frozen=True)
class ConnectionReadiness:
    """These events belong to one socket registration."""
    connection: Connection
    events: int


@dataclass(frozen=True)
class PendingCoordinatorEvidence:
    requested_at: float


@dataclass(frozen=True)
class DeferredOwnerRequest:
    request: wire.AdmitRequest | wire.LaunchRequest | wire.ServiceEstimateRequest
    connection: Connection
    arrived_monotonic: float


@dataclass(frozen=True)
class ParkedPlacement:
    """A placement request whose reply waits for the next pass."""
    request: wire.PlacementRequest
    connection: Connection


@dataclass(frozen=True)
class AdmissionPassInputs:
    """Keep the local inputs and the queue order that includes held CI waiters."""
    local: records.QueueInputs
    queue_order: list[tuple[Path, records.RankedWaiter]]


QueueChange = wire.PrioritySetRequest | wire.PriorityClearRequest


@dataclass
class OrderedQueueChange:
    request: QueueChange
    connections: list[Connection]


@dataclass(frozen=True)
class NoPendingPriorityRoster:
    """No priority change waits for a roster read."""


@dataclass(frozen=True)
class PendingPriorityRoster:
    change: OrderedQueueChange
    result: Future[Mapping[str, gate.UnitMembership]]


def read_priority_roster(result: Future[Mapping[str, gate.UnitMembership]], wake: int) -> None:
    """Read the roster without changing service state."""
    try:
        try:
            result.set_result(gate.priority_labels(gate.RosterUnits()))
        except Exception as error:
            result.set_exception(error)
        os.eventfd_write(wake, 1)
    finally:
        os.close(wake)


@dataclass(frozen=True)
class NoContinuationClaim:
    """This waiter has not claimed a continuation slot."""


class WaiterClaimDisposition(Enum):
    RELEASE = auto()
    RETURN = auto()
    TRANSFER = auto()


@dataclass
class WaitingAdmission:
    request: wire.AdmitRequest | wire.LaunchRequest
    row: records.Waiter
    connection: Connection | NoConnection
    last_reply: wire.Wait | NoWaitReply = NoWaitReply()
    last_sent: float = float("-inf")
    continuation_claim: records.ContinuationSlot | NoContinuationClaim = NoContinuationClaim()


@dataclass(frozen=True)
class NoPass:
    pass


def _no_fault(_stage: str) -> None:
    pass


def _place_key(owner: wire.Owner, since: float) -> str:
    return f"{records.run_owner(owner.pid, owner.start_time)}:{since:.17g}"


def _refreshes(first: wire.Request | NoRequest, request: wire.Request) -> bool:
    """A new estimate for the same episode keeps its connection, so the waiter keeps its place."""
    same_kind = ((isinstance(first, wire.AdmitRequest) and isinstance(request, wire.AdmitRequest))
                 or (isinstance(first, wire.LaunchRequest) and isinstance(request, wire.LaunchRequest))
                 or (isinstance(first, wire.ServiceEstimateRequest) and isinstance(request, wire.ServiceEstimateRequest)))
    return (same_kind and isinstance(first, (wire.AdmitRequest, wire.LaunchRequest, wire.ServiceEstimateRequest))
            and isinstance(request, (wire.AdmitRequest, wire.LaunchRequest, wire.ServiceEstimateRequest))
            and (request.episode, request.owner, request.since) == (first.episode, first.owner, first.since))


# systemd passes the first inherited socket at this descriptor.
LISTEN_FD = 3
ADMISSION_SOURCE_RELOAD = "BUILD_ADMISSION_SOURCE_RELOAD"


def _notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect("\0" + address[1:] if address.startswith("@") else address)
            _ = notifier.send(message.encode())


def finalization_outcome(ending: ReleaseStatus | OwnerExited | NotEnding | TokenWaiting
                         ) -> tuple[step_estimator.FinalOutcome, int | step_estimator.OutcomeNotRecorded, gate.StepEnd | gate.TokenReturn]:
    if isinstance(ending, TokenWaiting):
        return step_estimator.Completed(), 0, gate.TokenReturn()
    if isinstance(ending, ReleaseStatus):
        return (step_estimator.Completed() if ending.status == 0 else
                step_estimator.Killed() if ending.status >= 128 else step_estimator.Failed()), ending.status, gate.StepEnd()
    return step_estimator.OwnerGone(), gate.OUTCOME_NOT_RECORDED, gate.StepEnd()


@dataclass(frozen=True)
class ServiceStateWrites:
    """Validate the service directory and each mutation target before publication."""
    directory: Path

    def require(self, *paths: Path) -> None:
        targets = paths or (self.directory, self.directory / "state.json",
                            self.directory / "queue.json", self.directory / "progress_profiles")
        override_guard.require_shared_state_write(
            Path(__file__), "build_queue/admission_service.py", *targets)

    def register(self, record: ReservationRecord) -> None:
        """Check each saved or new reservation before it enters service state."""
        path = Path(record.path)
        targets = [path, path.with_suffix(".max"), path.with_suffix(".wave"),
                   path.parent / "anon_peaks.jsonl", path.parent / "queue_times.jsonl"]
        if record.sidecar:
            sidecar = Path(record.sidecar)
            targets.extend((sidecar, progress.progress_path(sidecar, "json"), progress.progress_path(sidecar, "log"),
                            Path(str(sidecar) + ".cloud.json")))
        self.require(*targets)


@dataclass(frozen=True)
class RegisteredServiceWrites:
    """The service checked its reservations when they entered state."""
    writes: ServiceStateWrites


class StateIntents:
    """Persist finalization before history; remove the entry only after history."""
    def __init__(self, reservations: dict[str, ReservationRecord],
                 advance_generation: Callable[[], None], episode: str,
                 persist: Callable[[], None], fault: Callable[[str], None],
                 places: dict[str, WaiterPlace], writes: ServiceStateWrites | RegisteredServiceWrites) -> None:
        boundary = writes.writes if isinstance(writes, RegisteredServiceWrites) else writes
        selected = (reservations[episode],) if isinstance(writes, RegisteredServiceWrites) else reservations.values()
        boundary.require(*(Path(record.path) for record in selected),
                         *(Path(record.sidecar) for record in selected if record.sidecar))
        self.writes: ServiceStateWrites = boundary
        self.reservations: dict[str, ReservationRecord] = reservations
        self.places: dict[str, WaiterPlace] = places
        self.advance_generation: Callable[[], None] = advance_generation
        self.episode: str = episode
        self.persist: Callable[[], None] = persist
        self.fault: Callable[[str], None] = fault

    def _require_record(self) -> None:
        record = self.reservations[self.episode]
        paths = (Path(record.path), Path(record.sidecar)) if record.sidecar else (Path(record.path),)
        self.writes.require(*paths)

    def save(self, row: records.Reservation, intent: records.FinalizationIntent) -> None:
        self._require_record()
        record = self.reservations[self.episode]
        if record.reservation_id != row.get("reservation_id"):
            raise ValueError("finalization reservation changed")
        self.reservations[self.episode] = replace(record, finalization=intent)
        self.persist()
        self.fault("after_finalization_intent")

    def published(self, row: records.Reservation) -> None:
        self._require_record()
        if row["step"] == "build":
            self.advance_generation()

    def clear(self, row: records.Reservation) -> None:
        self._require_record()
        if self.reservations[self.episode].reservation_id != row.get("reservation_id"):
            raise ValueError("finalization reservation changed")
        self.fault("after_history_append")
        record = self.reservations[self.episode]
        if not isinstance(record.ending, TokenWaiting):
            _ = self.places.pop(_place_key(record.owner, record.queue_waiter["since"]), None)
        del self.reservations[self.episode]
        self.persist()


@dataclass(frozen=True)
class ServiceEstimatedAdmission(wire.AdmitRequest):
    """Keep the runner separate from the unit used for priority."""
    runner: str = field(kw_only=True)


def wait_line_name(waiting: WaitingAdmission) -> str:
    """Name a queued build from its recorded owner and step."""
    row = waiting.row
    return codec.wait_line_name(row.get("unit", ""), row.get("session_name", ""),
                                      row.get("folder", ""), row.get("step", ""))


def placement_order_reason(request: wire.PlacementRequest) -> str:
    identity = request.identity
    name = codec.wait_line_name(identity.unit, identity.name, identity.folder, identity.step)
    return records.behind_queue_text(name)


@cache
def loaded_git_revision(source_root: Path) -> str:
    """Read the source revision once per process."""
    try:
        return subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def peer_uid(connection: Connection | NoConnection) -> int:
    """Read the account before any replay or state lookup."""
    if isinstance(connection, NoConnection):
        return os.getuid()
    credentials = connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", credentials)[1]


def process_uid(pid: int, proc: Path) -> int | OwnerProcessGone | OwnerProcessUnreadable:
    try:
        for line in read_proc_text(proc / str(pid) / "status").splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        return OwnerProcessGone()
    except (OSError, ValueError, IndexError) as error:
        return OwnerProcessUnreadable(str(error))
    return OwnerProcessUnreadable("the process status has no UID")


def episode_constraints(first: placement.PlacementConstraints,
                        later: placement.PlacementConstraints) -> placement.PlacementConstraints:
    """Keep the constraints of an episode's first request, so a later land or a busy target cannot change them.

    A request from an older client has no command kind. A newer client of the same episode adds it.
    """
    if isinstance(first.command_kind, placement.CommandKindNotSent):
        return replace(first, command_kind=later.command_kind)
    return first


class AdmissionServer:
    def __init__(self, state_dir: Path, clock: Clock, readings: Readings, proc: Proc, *,
                 fault: Callable[[str], None] = _no_fault, socket_path: Path | None = None,
                 capture_root: Path = CARGO_CAPTURE_ROOT,
                 host_probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] | None = None) -> None:
        self.writes: ServiceStateWrites = ServiceStateWrites(state_dir)
        self.writes.require()
        self.state_dir: Path = state_dir
        self.clock: Clock = clock
        self.readings: Readings = readings
        self.proc: Proc = proc
        self.fault: Callable[[str], None] = fault
        self.socket_path: Path = socket_path if socket_path is not None else wire.admission_socket_path()
        self.waiters: dict[str, WaitingAdmission] = {}
        self.pending_cloud_groups: dict[str, PendingCloudGroup] = {}
        self.reservations: dict[str, ReservationRecord] = {}
        self.lifecycle_outbox: tuple[PendingAdmission | PendingLeft, ...] = ()
        self.places: dict[str, WaiterPlace] = {}
        self.run_places: dict[str, wire.RunPlace] = {}
        self.build_history_generation: int = 0
        self.ledger: ReplayLedger = ReplayLedger({})
        self.placement_requests: dict[str, wire.PlacementRequest] = {}
        self.pending_coordinators: dict[placement.SubmittingOwner, PendingCoordinatorEvidence] = {}
        self.assignments: dict[str, records.SavedPlacementAssignment] = {}
        self.current_assignments: dict[str, str] = {}
        self.execution_evidence: dict[str, execution_identity.ExecutionEvidence] = {}
        self.prerequisites: dict[str, placement.LocalExecutionPrerequisite] = {}
        self.placement_eligibility: dict[str, tuple[placement.CandidateEligibility, ...]] = {}
        self.active_placement_waits: set[str] = set()
        self.placement_resource_waits: dict[str, records.PlacementBlock] = {}
        self.local_hold_holders: dict[str, placement.ResourceHolder] = {}
        self.host_probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] = (
            host_probe if host_probe is not None else self._read_host_evidence)
        self.host_readers: dict[placement.HostEvidenceKey, HostEvidenceReader] = {}
        self.host_targets: dict[tuple[str, str], HostEvidenceWorker] = {}
        self.background_local_evidence: bool = host_probe is None
        self.host_observations: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        self.host_requests: dict[placement.HostEvidenceKey, wire.PlacementRequest] = {}
        self.placement_host_keys: dict[tuple[str, placement.PlacementDestination], placement.HostEvidenceKey] = {}
        # Start time of each hold that keeps a remote destination for an older waiting request.
        self.placement_holds: dict[tuple[str, placement.PlacementDestination], float] = {}
        # The reading that answered each ended hold. That reading never starts another hold.
        self.placement_hold_answers: dict[tuple[str, placement.PlacementDestination], float] = {}
        self.spacing: records.StartSpacing = records.ReadyToStart()
        self.last_inputs: records.QueueInputs | NoPass = NoPass()
        self.last_result: gate.PassResult | NoPass = NoPass()
        self.last_policy: records.AdmissionPolicy | NoPass = NoPass()
        self.reading_hold: UnreadableReadingHold | NoReadingHold = NoReadingHold()
        self.reading_alert_delivery: ReadingAlertDelivery | NoReadingAlertDelivery = NoReadingAlertDelivery()
        self.reading_alert_needs_clear: bool = True
        self.outage_alert_needs_clear: bool = True
        self.listener_started_at: float = float("inf")
        self.configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration = gate.UnusableAdmissionConfiguration("configuration not read")
        self.last_usable_configuration: records.MemoryConfig | NoPass = NoPass()
        self.caller_versions: dict[str, str] = {}
        self.slots: dict[str, records.RunSlot] = {}
        self.progress_history: progress.ProfileHistory = progress.ProfileHistory(state_dir / "progress_profiles")
        self.progress_captures: progress.CaptureReader = progress.CaptureReader(capture_root)
        try:
            self.boot_id: str = boot_identity(proc.path)
        except (OSError, ValueError):
            self.boot_id = ""
        self.host: str = store.host_name()
        self.pass_stale: bool = False
        self.pending_configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration | NoPass = NoPass()
        self.priority: records.QueuePriority | records.NoQueuePriority = records.NO_QUEUE_PRIORITY
        self.pending_priority: PendingPriorityRoster | NoPendingPriorityRoster = NoPendingPriorityRoster()
        self.queue_changes: deque[OrderedQueueChange] = deque()
        self.last_diagnostics: tuple[records.ConfigurationDiagnostic, ...] = ()
        self._pending_replies: dict[str, list[wire.Reply]] = {}
        self._persisted: str = ""
        self.durable_encodings: dict[tuple[str, str], DurableRecordEncoding] = {}
        self.published: PublishedReplies | NoPass = NoPass()
        self.system_reader: SystemReader | NoSystemReader = NoSystemReader()
        self.system_evidence: SystemEvidence | NoPass = NoPass()
        self.owner_confirmation_reading: object = object()
        self.owner_confirmations: dict[int, OwnerStartReading] = {}
        self.reader_requested: bool = False
        self.deferred_requests: list[DeferredOwnerRequest] = []
        self.parked_placements: list[ParkedPlacement] = []
        self.last_pass_started: float = float("-inf")
        self.receiving_requests: bool = False
        self.connections: dict[int, Connection] = {}
        self.watchers: dict[int, OwnerWatch | ScopeWatch] = {}
        self.epoll: select.epoll = select.epoll()
        self.priority_events: int = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
        self.epoll.register(self.priority_events, select.EPOLLIN)
        self.reading_events: int = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
        self.epoll.register(self.reading_events, select.EPOLLIN)
        self.listener: socket.socket | NoConnection = NoConnection()
        self.inherited_listener: bool = False
        # A source change under systemd runs the new code in this process and keeps the socket.
        self.replace_in_place: bool = False
        self.stopped: bool = False
        self.closed: bool = False
        self.next_pass: float = float("inf")
        self.next_progress_sweep: float = float("-inf")
        self.next_source_check: float = float("inf")
        # The service watches each loaded module under this folder and stops when one changes.
        self.source_root: Path = Path(__file__).resolve().parents[1]
        self.source_module_count: int = len(sys.modules)
        self.sources: dict[Path, tuple[int, int] | SourceMissing] = {
            path: self._stamp(path) for path in self._source_paths()}
        self.source_change_at: float | SourcesUnchanged = SourcesUnchanged()
        self.changed_sources: set[Path] = set()
        state_dir.mkdir(parents=True, exist_ok=True)

        self.config_path: Path = conf.path("build_admission")
        self.config_events: int = inotify.open_watch()
        self.config_watch: int = -1
        self.config_parent: Path = self.config_path.parent
        self.config_parent_identity: tuple[int, int] = (-1, -1)
        self.config_event_name: str = ""
        self.next_config_watch_retry: float = float("inf")
        self._arm_config_watch()
        self.epoll.register(self.config_events, select.EPOLLIN)

        self._restore()
        _ = self.run_pass()

    def _finish_priority(self) -> None:
        if isinstance(self.priority, records.NoQueuePriority):
            return
        transition = self.priority.transition
        if isinstance(transition, records.SettledPriority):
            return
        self.writes.require(store.root())
        gate.finish_priority_transition(self.priority)
        self.priority = (records.QueuePriority(self.priority.entries) if self.priority.entries
                         else records.NO_QUEUE_PRIORITY)
        self.persist()


    def _restore(self) -> None:
        path = self.state_dir / "state.json"
        if path.exists():
            self._persisted = path.read_text()
            state = parse_service_state(self._persisted, warn_missing_folders=True)
            members = (records.queue_staff() if state.reservations or state.places or state.placement_requests
                       else records.NO_STAFF_LOOKUP)
            for episode, record in state.reservations.items():
                row = record.queue_waiter
                if not row.get("ci_runner"):
                    caller = self._stored_caller(row.get("session_id", ""), row.get("session_name", ""),
                        row.get("folder") or record.worktree, row.get("unit", ""), members=members)
                    record = replace(record, queue_waiter={**row, "session_name": caller.name, "unit": caller.unit})
                    state.reservations[episode] = record
                self.writes.register(record)
            for key, place in state.places.items():
                identity = place.queue_identity
                caller = self._stored_caller(identity["session_id"], identity["session_name"],
                                             identity["folder"], identity.get("unit", ""), members=members)
                state.places[key] = replace(place, queue_identity={
                    **identity, "session_name": caller.name, "unit": caller.unit})
            self.reservations, self.places = state.reservations, state.places
            self.ledger = ReplayLedger(state.ledger)
            self.spacing, self.priority = state.spacing, state.priority
            self.build_history_generation = state.build_history_generation
            self.run_places = state.run_places
            self.lifecycle_outbox = state.lifecycle_outbox
            self.caller_versions = state.caller_versions
            self.placement_requests = {
                key: replace(request, identity=self._named_identity(self._placement_identity(request), members=members))
                for key, request in state.placement_requests.items()}
            self.assignments = state.assignments
            self.current_assignments = state.current_assignments
            self.pending_cloud_groups = state.pending_cloud_groups
        self.persist()
        for episode, record in list(self.reservations.items()):
            if not self._end_if_finished(episode, record):
                lifetime = self._lifetime(record)
                self._watch_owner(record.owner)
                self._watch_scope(lifetime, episode)
        self._prune_places()
        self._finish_priority()
        for request in self.placement_requests.values():
            self.prerequisites[request.episode] = (placement.TokenNotRequired()
                if isinstance(request.prerequisite, placement.TokenNotRequired) else placement.TokenRequired())
            if request.owner.host == self.host and self._live(wire.Owner(request.owner.pid, request.owner.start_time)):
                self._watch_owner(request.owner)
        for holder in self._claim_holders():
            if self._live(holder):
                self._watch_owner(holder)
        for pending in self.pending_cloud_groups.values():
            coordinator = pending.group.coordinator
            if self.proc.owner_start(coordinator.pid) == LiveOwnerIdentity(coordinator.start_time):
                self._watch_owner(coordinator)

    def _end_if_finished(self, episode: str, record: ReservationRecord) -> bool:
        lifetime = self._lifetime(record)
        if (not isinstance(record.finalization, NoFinalizationIntent)
                or not isinstance(record.ending, NotEnding) or isinstance(lifetime, gate.EndedStep)):
            with retry_cleanup(Path(record.path)):
                self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
            return episode not in self.reservations
        return False

    def _live_reservation_ids(self) -> set[str]:
        return {record.reservation_id for record in self.reservations.values()}

    def _inputs_with_current_slots(self, inputs: records.QueueInputs) -> records.QueueInputs:
        self._prune_runs()
        self._claim_slots()
        remaining = gate.inputs_after_reservations_end(inputs, self._live_reservation_ids())
        occupied = self._occupied_local_cargo_folders()
        local_owners = {records.waiter_owner(row) for _, row in remaining.ordered}
        folders = {owner: request.constraints.cargo_folder
                   for episode, waiting in self.waiters.items()
                   if (owner := records.waiter_owner(waiting.row)) in local_owners
                   and (request := self.placement_requests.get(episode)) is not None
                   and isinstance(request.constraints.cargo_folder, placement.CargoFolder)}
        if occupied:
            for key, slot in list(self.slots.items()):
                if isinstance(slot, records.ContinuationSlot) and folders.get(slot.owner) in occupied:
                    del self.slots[key]
        return replace(remaining, slots=tuple(self.slots.values()), cargo_folders=folders,
                       occupied_cargo_folders=occupied)

    def persist(self) -> None:
        now = self.clock.now()
        live_callers: dict[str, str] = {}
        for key, version in self.caller_versions.items():
            pid, separator, start_time = key.partition("-")
            if separator and pid.isdecimal() and self._live(wire.Owner(int(pid), start_time)):
                live_callers[key] = version
        self.caller_versions = live_callers
        live_reservations = self._live_reservation_ids()
        live_dispatches = {"dispatch:" + key for key, saved in self.assignments.items()
                           if isinstance(saved.state, placement.CurrentAssignment)}
        protected_keys = self.reservations.keys() | live_dispatches
        for key in tuple(self.ledger.live):
            entry = self.ledger[key]
            if key not in protected_keys and entry.reservation not in live_reservations:
                self.ledger[key] = replace(entry, ended_at=now)
        if not isinstance(self.last_usable_configuration, NoPass):
            self.ledger.expire(now - self.last_usable_configuration.replay_retention_seconds,
                               protected_keys, live_reservations)
            self._prune_ended_placements(now - self.last_usable_configuration.ended_placement_seconds)
        state = ServiceState(self.reservations, self.places, self.ledger, self.spacing, self.priority,
            self.build_history_generation, self.run_places, self.caller_versions, self.lifecycle_outbox,
            self.placement_requests, self.assignments, self.current_assignments, self.pending_cloud_groups)
        encodings: dict[tuple[str, str], DurableRecordEncoding] = {}
        changed = False

        def encoded_record(name: str, key: str, value: object) -> DurableRecordEncoding:
            nonlocal changed
            previous = self.durable_encodings.get((name, key))
            if previous is None or previous.value != value:
                changed = True
                previous = DurableRecordEncoding(durable_copy(value), wire.encode_record(value),
                    previous.key if previous is not None else json.dumps(key, ensure_ascii=False))
            encodings[name, key] = previous
            return previous

        parts: list[tuple[str, str]] = []
        for member in fields(state):
            value = cast(object, getattr(state, member.name))
            if member.name == "ledger":
                previous = self.durable_encodings.get(("ledger", ""))
                if previous is None or previous.value != self.ledger.revision:
                    changed = True
                    previous = DurableRecordEncoding(self.ledger.revision,
                        wire.encode_record(dict(self.ledger)), "")
                encodings["ledger", ""] = previous
                encoded_value = previous.encoded
            elif isinstance(value, dict):
                records = (encoded_record(member.name, key, item)
                           for key, item in cast(dict[str, object], value).items())
                encoded_value = "{" + ",".join(record.key + ":" + record.encoded for record in records) + "}"
            else:
                encoded_value = encoded_record(member.name, "", value).encoded
            parts.append((member.name, encoded_value))
        changed = changed or encodings.keys() != self.durable_encodings.keys()
        if changed:
            encoded = '{"kind":"ServiceState",' + ",".join(json.dumps(name) + ":" + value for name, value in parts) + "}"
            if encoded != self._persisted:
                write_text_atomic(self.state_dir / "state.json", encoded)
                self._persisted = encoded
                self.fault("after_state_write")
        self.durable_encodings = encodings
        self._drain_lifecycle()

    def _unwatch(self, descriptor: int) -> None:
        _ = self.watchers.pop(descriptor, None)
        try:
            self.epoll.unregister(descriptor)
        except (OSError, FileNotFoundError):
            pass
        os.close(descriptor)

    def _watch_owner(self, owner: wire.Owner | placement.SubmittingOwner) -> None:
        if isinstance(owner, placement.SubmittingOwner):
            if owner.host != self.host:
                return
            owner = wire.Owner(owner.pid, owner.start_time)
        if any(isinstance(watch, OwnerWatch) and watch.owner == owner for watch in self.watchers.values()):
            return
        try:
            descriptor = self.proc.pidfd(owner.pid)
        except (OSError, ProcessLookupError):
            return
        try:
            self.epoll.register(descriptor, select.EPOLLIN)
        except OSError:
            os.close(descriptor)
            return
        self.watchers[descriptor] = OwnerWatch(owner)

    def _watch_scope(self, lifetime: gate.StepLifetime,
                     episode: str) -> None:
        if not isinstance(self.system_reader, NoSystemReader) or not isinstance(lifetime, gate.PopulatedStepScope):
            return
        path = lifetime.directory / "cgroup.events"
        for descriptor, watch in list(self.watchers.items()):
            if isinstance(watch, ScopeWatch) and watch.episode == episode:
                if watch.path == path:
                    return
                self._unwatch(descriptor)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            self.epoll.register(descriptor, select.EPOLLPRI | select.EPOLLERR)
            self.watchers[descriptor] = ScopeWatch(path, episode)
            _ = os.read(descriptor, 4096)
        except OSError:
            self._unwatch(descriptor)

    def _prune_watches(self) -> None:
        owners = {record.owner for record in self.reservations.values()}
        owners.update(waiting.request.owner for waiting in self.waiters.values())
        owners.update(place.owner for place in self.places.values())
        owners.update(wire.Owner(place.run.pid, place.run.start_time) for place in self.run_places.values())
        owners.update(wire.Owner(request.owner.pid, request.owner.start_time)
                      for request in self.placement_requests.values() if request.owner.host == self.host)
        owners.update(self._claim_holders())
        owners.update(pending.group.coordinator for pending in self.pending_cloud_groups.values())
        for descriptor, watch in list(self.watchers.items()):
            if (isinstance(watch, OwnerWatch) and watch.owner not in owners
                    or isinstance(watch, ScopeWatch) and watch.episode not in self.reservations):
                self._unwatch(descriptor)

    def _ci_hold_reasons(self) -> dict[str, records.NatedevHeld | records.ReadingUnavailable]:
        """Apply the existing hold policy to legacy CI admission requests."""
        waiting = {episode: item for episode, item in self.waiters.items()
                   if isinstance(item.request, ServiceEstimatedAdmission)}
        if not waiting:
            return {}
        from scripts.build_hold import build_hold
        from scripts.build_targets import build_targets

        try:
            directory = build_hold.holder_directory()
            holds = build_hold.natedev_holds(directory)
        except OSError as error:
            return {episode: records.ReadingUnavailable(f"natedev hold state is unreadable: {error}") for episode in waiting}
        reasons: dict[str, records.NatedevHeld | records.ReadingUnavailable] = {}
        for episode, item in waiting.items():
            owner = item.request.owner
            lineage = build_targets.read_caller_lineage(self.proc.path, owner.pid, owner.start_time)
            blocking = build_targets.hold_waited_on(holds, NoOwningSession(), directory, lineage)
            if isinstance(blocking, build_hold.Holder):
                reasons[episode] = records.NatedevHeld(blocking.name, blocking.purpose)
        return reasons

    def _ordered_waiters(self) -> list[tuple[Path, records.RankedWaiter]]:
        rows: list[tuple[Path, records.RankedWaiter]] = []
        for waiting in self.waiters.values():
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace):
                continue
            row = gate.rank_waiter(waiting.row, records.NormalOrder())
            row["order_since"] = row.get("order_since", row["since"])
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.run, wire.RunIdentity):
                place = self.run_places.get(records.run_owner(waiting.request.run.pid, waiting.request.run.start_time))
                if place is not None:
                    row["order_since"] = min(place.since, row["order_since"])
            rows.append((self.state_dir / records.waiter_owner(row), row))
        for request in self.placement_requests.values():
            if request.episode in self.waiters or not self._placement_waiting(request):
                continue
            row = gate.rank_waiter(self._placement_row(request), records.NormalOrder())
            rows.append((self.state_dir / records.waiter_owner(row), row))
        return sorted(rows, key=lambda item: gate.waiter_order(item[1]))

    @staticmethod
    def _priority_name(row: records.Waiter) -> str:
        return row.get("ci_runner") or row.get("unit", "")

    def _queue_order(self) -> tuple[list[tuple[Path, records.RankedWaiter]], records.QueuePriority | records.NoQueuePriority]:
        rows = self._ordered_waiters()
        if isinstance(self.priority, records.NoQueuePriority):
            return rows, self.priority
        # Match CI priority by runner. Keep the runner out of the published unit field.
        ci_waiters: dict[Path, records.RankedWaiter] = {}
        sessions: list[str] = []
        ranking_rows: list[tuple[Path, records.Waiter]] = []
        for path, row in rows:
            name = self._priority_name(row)
            if name != row.get("unit", ""):
                ci_waiters[path] = row
                row = row.copy()
                row["unit"] = name
            elif not name:
                sessions.append(row.get("session_id", ""))
            ranking_rows.append((path, row))
        labels: Mapping[str, gate.UnitMembership] = gate.priority_labels(sessions) if sessions else {}
        ranked, priority = gate.apply_queue_priority(ranking_rows, gate.QueueOrderContext(self.priority, labels))
        return [(path, gate.rank_waiter(ci_waiters[path], records.waiter_priority(row))
                 if path in ci_waiters else row) for path, row in ranked], priority


    def _prune_runs(self) -> None:
        now = self.clock.now()
        for key, slot in list(self.slots.items()):
            if (not self._run_alive(gate.between_steps(slot).run)
                    or not gate.slot_in_force(slot, now)):
                del self.slots[key]
        for key, place in list(self.run_places.items()):
            owner = wire.Owner(place.run.pid, place.run.start_time)
            if self._live(owner):
                self._watch_owner(owner)
            else:
                del self.run_places[key]

    def _free_claim(self, owner: str) -> None:
        for key, slot in list(self.slots.items()):
            if isinstance(slot, records.ContinuationSlot) and slot.owner == owner:
                if gate.slot_in_force(slot.slot, self.clock.now()) and self._run_alive(slot.slot.run):
                    self.slots[key] = slot.slot
                else:
                    del self.slots[key]

    def _claim_slots(self) -> None:
        claimed = {slot.owner for slot in self.slots.values() if isinstance(slot, records.ContinuationSlot)}
        for waiting in sorted(self.waiters.values(), key=lambda item: gate.waiter_order(item.row)):
            request = waiting.request
            saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
            if saved is not None and isinstance(saved.remote_start_hold, records.RemoteStartHold):
                continue
            placement_request = self.placement_requests.get(request.episode)
            if placement_request is not None and isinstance(placement_request.purpose, placement.SpeculativeCopy):
                continue
            if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
                continue
            if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
                continue
            owner = records.waiter_owner(waiting.row)
            if owner in claimed or isinstance(gate.waiter_state(waiting.row), records.TokenWaitingWaiter):
                continue
            candidates = [(key, slot) for key, slot in self.slots.items()
                          if isinstance(slot, records.BetweenStepsSlot) and slot.run.pid == request.run.pid
                          and slot.run.start_time == request.run.start_time]
            if not candidates:
                continue
            key, slot = min(candidates, key=lambda item: (item[1].expires_at, item[0]))
            claim = records.ContinuationSlot(slot, owner)
            self.slots[key] = claim
            waiting.continuation_claim = claim
            claimed.add(owner)

    def _run_place(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace) -> None:
        if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
            return
        if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
            return
        run = request.run
        key = records.run_owner(run.pid, run.start_time)
        if key not in self.run_places and self._run_alive(run):
            self.run_places[key] = wire.RunPlace(run, place.since, request.owner, request.identity.unit)

    def _known_owner(self, owner: wire.Owner) -> bool:
        return (any(isinstance(watch, OwnerWatch) and watch.owner == owner for watch in self.watchers.values())
                or any(place.owner == owner for place in self.places.values())
                or any(waiter.request.owner == owner for waiter in self.waiters.values()))

    def _confirm_omitted_owner(self, pid: int, evidence: SystemEvidence) -> OwnerStartReading:
        if self.owner_confirmation_reading is not evidence.reading_identity:
            self.owner_confirmation_reading = evidence.reading_identity
            self.owner_confirmations.clear()
        if pid not in self.owner_confirmations:
            self.owner_confirmations[pid] = self.proc.owner_start(pid)
        return self.owner_confirmations[pid]

    def _live(self, owner: wire.Owner) -> bool:
        if isinstance(self.system_reader, NoSystemReader):
            reading = self.proc.owner_start(owner.pid)
            if isinstance(reading, OwnerProcessUnreadable):
                return self._known_owner(owner)
            return reading == LiveOwnerIdentity(owner.start_time)
        evidence = self.system_reader.latest
        identity = evidence.processes.get(owner.pid)
        if identity is not None:
            return identity.start == owner.start_time
        return self._known_owner(owner) and owner_may_be_live(
            self._confirm_omitted_owner(owner.pid, evidence), owner.start_time)

    def _account(self, owner: wire.Owner | placement.SubmittingOwner | wire.RunIdentity
                 ) -> int | OwnerProcessGone | OwnerProcessUnreadable:
        if not isinstance(self.system_reader, NoSystemReader):
            evidence = self.system_reader.latest
            if owner.pid not in evidence.processes:
                if not self._known_owner(wire.Owner(owner.pid, owner.start_time)):
                    return OwnerProcessUnreadable("the owner is missing from the process reading")
                reading = self._confirm_omitted_owner(owner.pid, evidence)
                if isinstance(reading, (OwnerProcessGone, OwnerProcessUnreadable)):
                    return reading
                if reading.start_time != owner.start_time:
                    return OwnerProcessGone()
        return process_uid(owner.pid, self.proc.path)

    def _run_alive(self, run: wire.RunIdentity | LiveBuildRun) -> bool:
        return self._live(wire.Owner(run.pid, run.start_time))

    def _prune_places(self, owner_exit: OwnerWatch | None = None) -> None:
        for key, place in list(self.places.items()):
            ended = owner_exit is not None and owner_exit.owner == place.owner
            if (ended or not self._live(place.owner)) and not any(
                    record.owner == place.owner for record in self.reservations.values()):
                self._record_left(place.episode, place.queue_identity, "owner_gone")
                del self.places[key]
        self.persist()

    def _row(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace | records.NoWaiter) -> records.Waiter:
        profile, identity = request.profile, request.identity
        since = request.since if isinstance(place, records.NoWaiter) else place.since
        row: records.Waiter = {
            "pid": request.owner.pid, "start_time": request.owner.start_time,
            "need": (0 if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
                     else profile.peak.bytes), "since": since,
            "order_since": (place.order_since if isinstance(place, WaiterPlace)
                            and not isinstance(place.order_since, OrderNotStarted) else since),
            "session_id": identity.session_id, "session_name": identity.name,
            "step": identity.step, "folder": identity.folder, "unit": identity.unit,
            "estimated_at": profile.estimated_at or self.clock.now(),
            "fresh_test": isinstance(request, wire.AdmitRequest) and request.fresh_test,
            "need_source": profile.peak.source, "need_runs": profile.peak.count,
            "need_basis": gate.need_basis_record(profile.peak.basis), "test_scope": profile.scope,
            "repository_name": profile.repo,
            "route": request.route if isinstance(request, wire.AdmitRequest) else records.DIRECT_CARGO,
        }
        if isinstance(identity.verify, records.VerifyCall):
            row["verify"] = identity.verify
        if isinstance(identity.job, StepJob):
            row["job"] = identity.job
        if isinstance(request, ServiceEstimatedAdmission):
            row["ci_runner"] = request.runner
        if isinstance(place, WaiterPlace) and not isinstance(place.priority_since, OrderNotStarted):
            row["priority_since"] = place.priority_since
        if isinstance(place, WaiterPlace) and not isinstance(place.token_left_at, TokenNotLeft):
            row["token_left_at"] = place.token_left_at
            if isinstance(request, wire.AdmitRequest) and request.token_back_at:
                row["token_back_at"] = request.token_back_at
        if isinstance(profile.repository, records.RepositoryIdentity):
            row["repo_identity"] = profile.repository.key
            row["repository_name"] = profile.repository.name or profile.repo
            row["legacy_name"] = profile.repository.legacy_name
        elif isinstance(profile, records.ProgramProfile):
            row["repository_name"] = gate_repository_name(profile.step, profile.repo, profile.worktree)
        if isinstance(request, wire.AdmitRequest):
            row["package"] = step_estimator.test_package(list(request.argv))
            row["filters"] = list(step_estimator.test_filters(list(request.argv)))
            if isinstance(request.test_width, records.RequestedTestWidth):
                row["test_threads"] = request.test_width.threads
        return row

    def _advance_build_history(self) -> None:
        self.build_history_generation += 1
        self.persist()

    def _build_estimate_expired(self, request: wire.AdmitRequest | wire.LaunchRequest) -> bool:
        profile = request.profile
        if not isinstance(profile, records.StepProfile) or profile.step != "build":
            return False
        generation = profile.build_history_generation
        if not isinstance(generation, records.BuildHistoryGeneration) or generation.value != self.build_history_generation:
            return True
        shape = build_shape(list(request.argv)).key if isinstance(request, wire.AdmitRequest) else ""
        return any(record.step == "build" and isinstance(record.finalization, dict)
                   and matches_build_estimate(record.repo, record.repo_identity, record.shape, profile, shape)
                   for record in self.reservations.values())

    def _reading_plan(self, config: records.MemoryConfig) -> SystemReadingPlan:
        return SystemReadingPlan(config, tuple(replace(record, queue_waiter=record.queue_waiter.copy())
            for record in self.reservations.values() if isinstance(record.place, wire.LocalPlace)),
            tuple(self.run_places.values()), bool(self._ordered_waiters()))

    def _lifetime(self, record: ReservationRecord) -> gate.StepLifetime:
        if isinstance(record.place, wire.CloudPlace):
            live = self._live(record.owner)
            if isinstance(record.run, wire.RunIdentity):
                live = live and self._run_alive(record.run)
            return gate.LiveOwnerProcess() if live else gate.EndedStep()
        if not isinstance(self.system_reader, NoSystemReader):
            return self.system_reader.latest.lifetimes.get(record.episode, gate.LiveOwnerProcess())
        return self.proc.lifetime(reservation_row(record), self._live(record.owner))

    def _inputs(self, config: records.MemoryConfig, profile_rank: int, profile_share: int,
                sweep_seconds: float) -> AdmissionPassInputs:
        now = self.clock.now()
        plan = self._reading_plan(config)
        if isinstance(self.system_reader, NoSystemReader):
            def direct_memory(record: ReservationRecord, row: records.Reservation, lifetime: gate.StepLifetime,
                              processes: dict[int, gate.ProcessIdentity], proc: Path) -> records.StepMemory:
                return (records.GoneMemory() if isinstance(lifetime, gate.EndedStep) else
                    self.readings.reservation(Path(record.path), row, datetime.fromtimestamp(now, UTC),
                                              processes, proc, config.policy))
            evidence = read_system(plan, self.readings, self.proc, self.clock, reservation_reader=direct_memory)
        else:
            self.system_reader.raise_failure()
            self.system_reader.update(plan)
            evidence = self.system_reader.latest
        self.system_evidence = evidence
        memories: list[tuple[records.Reservation, records.StepMemory]] = []
        for episode, record in list(self.reservations.items()):
            if isinstance(record.place, wire.CloudPlace):
                _ = self._end_if_finished(episode, record)
                continue
            if isinstance(record.finalization, dict) or not isinstance(record.ending, NotEnding):
                with retry_cleanup(Path(record.path)):
                    self._end(episode, record.ending)
                if episode not in self.reservations:
                    continue
            row = reservation_row(record)
            if "build_run" not in row:
                row["build_run"] = evidence.build_run(record.owner.pid)
                row["run_origin"] = records.FoundRun(row["build_run"])
            elif isinstance(record.run, wire.RunIdentity) and not self._run_alive(record.run):
                row["run_origin"] = records.CallerRun(NoLiveBuildRun())
            captured = evidence.reservations.get(episode)
            memory = (reservation_charge(captured, row, now, config.policy) if captured is not None
                      else records.PromisedMemory(record.need))
            if isinstance(memory, records.GoneMemory):
                with retry_cleanup(Path(record.path)):
                    self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
                continue
            repository_name = record.queue_waiter.get("repository_name", record.repo)
            resolved_name = gate_repository_name(record.step, repository_name, record.worktree)
            if resolved_name != repository_name:
                row["queue_waiter"] = {**record.queue_waiter, "repository_name": resolved_name}
            if record.episode.startswith("uid:") and isinstance(memory, records.MeasuredMemory):
                if isinstance(memory.anonymous, records.AnonymousRunUse):
                    self._sample_runner(record, memory.anonymous.bytes, now)
            memories.append((row, memory))
            self._watch_scope(evidence.lifetimes.get(episode, gate.LiveOwnerProcess()), episode)
        for waiting in self.waiters.values():
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace):
                continue
            waiting.row["build_history_current"] = not self._build_estimate_expired(waiting.request)
            request = waiting.request
            if "build_run" not in waiting.row:
                waiting.row["build_run"] = (_live_run(request.run)
                    if isinstance(request, wire.AdmitRequest) and isinstance(request.run, wire.RunIdentity)
                    else evidence.build_run(request.owner.pid))
        pressure = evidence.pressure
        if isinstance(pressure, records.MemoryPressure):
            pressure = gate.pressure_from_readings(pressure.full_avg10, pressure.some_avg10,
                                                  pressure.swap_used_percent, config.policy)
        readings = gate.QueueSnapshotReadings(dict(evidence.processes), {}, evidence.host,
            pressure, self.spacing, list(evidence.compilers), policy=config.policy)
        ordered, priority = self._queue_order()
        unowned = self._unowned_memory(evidence, [row for row, _memory in memories])
        pending_remote_starts = frozenset(records.waiter_owner(waiting.row)
            for episode, key in self.current_assignments.items()
            if (waiting := self.waiters.get(episode)) is not None
            and isinstance(self.assignments[key].remote_start_hold, records.RemoteStartHold))
        local_owners: set[str] = set()
        local_waits: dict[str, placement.EvidenceNeeded] = {}
        for episode, waiting in self.waiters.items():
            owner = records.waiter_owner(waiting.row)
            request = self.placement_requests.get(episode)
            eligibility = placement.AllowedHost() if request is None else self._local_placement_eligibility(request)
            if isinstance(eligibility, placement.EvidenceNeeded):
                local_waits[owner] = eligibility
            if (isinstance(eligibility, (placement.AllowedHost, placement.EvidenceNeeded))
                    or owner in pending_remote_starts and eligibility != placement.TemporaryExclusion("blocked")):
                local_owners.add(owner)
        local_rows = [(path, row) for path, row in ordered if records.waiter_owner(row) in local_owners]
        limits = config.policy.repository_runs
        sessions = list(dict.fromkeys(session for _, row in local_rows
            if not row.get("unit") and (session := row.get("session_id", ""))
            and limits.for_step(self.host, row.get("repository_name", ""), row.get("step", ""))))
        labels: Mapping[str, gate.UnitMembership] = gate.priority_labels(sessions) if sessions else {}
        exempt = frozenset(records.waiter_owner(row) for _, row in local_rows
            if (session := row.get("session_id", "")) in labels
            and gate.queue_unit(row.get("unit", ""), session, labels) == limits.exempt_unit)
        inputs = gate.build_queue_inputs(local_rows, memories, readings, now, priority, unowned)
        inputs = replace(inputs, pending_remote_starts=pending_remote_starts & local_owners, local_waits=local_waits,
                         local_machine=self.host, run_limit_exempt_owners=exempt)
        represented = {(step.run.pid, step.run.start_time) for step in inputs.running
                       if isinstance(step.run, LiveBuildRun)}
        between = tuple(step for step in evidence.between
                        if isinstance(step.run, LiveBuildRun) and (step.run.pid, step.run.start_time) not in represented)
        if between:
            inputs = replace(inputs, running=(*inputs.running, *between))
        if self.clock.monotonic() >= self.next_progress_sweep:
            self.progress_history.prune(now - step_estimator.HISTORY_DAYS * 86400, sweep_seconds,
                rank=profile_rank, share_percent=profile_share)
            self.next_progress_sweep = self.clock.monotonic() + config.running_pass_seconds
        local = self._progress_inputs(self._inputs_with_current_slots(inputs), dict(evidence.processes),
                                      config.policy, profile_rank, profile_share)
        return AdmissionPassInputs(local, ordered)

    def _unowned_memory(self, evidence: SystemEvidence, rows: list[records.Reservation]
                        ) -> tuple[records.UnownedSliceMemory, ...]:
        owned: set[str] = set()
        scopes: dict[str, str] = {}
        scope_memory: dict[Path, records.MeasuredMemory] = {}
        active = {row.get("reservation_id", "") for row in rows}
        for record in self.reservations.values():
            if isinstance(record.place, wire.CloudPlace) and record.sidecar:
                try:
                    scope = Path(record.sidecar).read_text().strip()
                except (OSError, UnicodeError):
                    scope = ""
                if scope:
                    owned.add(scope)
            captured = evidence.reservations.get(record.episode)
            if captured is not None and captured.scope:
                owned.add(captured.scope)
                scopes[record.reservation_id] = captured.scope
                if record.reservation_id in active and isinstance(captured.memory, records.MeasuredMemory):
                    scope_memory[Path(captured.scope)] = captured.memory
        roots = {row["pid"]: row["start_time"] for row in rows if not scopes.get(row.get("reservation_id", ""))}
        tree_pids = gate.process_tree(roots, dict(evidence.processes))
        result: list[records.UnownedSliceMemory] = []
        for memory in evidence.unowned:
            state = records.recorded_service_state(memory)
            required = not isinstance(state, NotServiceSlice)
            if not required and memory.cgroup in owned:
                continue
            scoped = [scope_memory[scope] for scope in gate.owned_scope_roots(scope_memory, Path(memory.cgroup))] if required else []
            scoped_pids = {pid for reading in scoped if isinstance(reading.anonymous_source, records.ScopeSource)
                           for pid in reading.anonymous_source.members}
            duplicate = [evidence.scope_process_memory[pid]
                         for pid in (evidence.scope_members.get(memory.cgroup, frozenset()) & tree_pids) - scoped_pids
                         if pid in evidence.scope_process_memory]
            use = sum(item.anonymous.bytes if required and isinstance(item.anonymous, records.AnonymousRunUse)
                      else item.bytes for item in duplicate)
            use += sum(item.anonymous.bytes for item in scoped if isinstance(item.anonymous, records.AnonymousRunUse))
            workers = evidence.scope_workers.get(memory.cgroup, frozenset())
            job = (gate.service_job_state(workers, dict(evidence.processes), tree_pids | scoped_pids,
                    [waiting.row for waiting in self.waiters.values()],
                    state.started_at if isinstance(state,
                        (RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob)) else 0.0)
                   if required else state)
            legacy_job = memory.job_state
            if workers and all(gate.process_tree({pid: evidence.processes[pid].start},
                               dict(evidence.processes)).intersection(tree_pids | scoped_pids) for pid in workers):
                legacy_job = IdleService()
            anonymous = gate.swap_backed_use_less(memory.anonymous,
                                                  (item.anonymous for item in (*duplicate, *scoped)))
            result.append(replace(memory, bytes=max(0, memory.bytes - use), anonymous=anonymous,
                                  job_state=legacy_job, service_job_state=job,
                                  swap_bytes=max(0, memory.swap_bytes - sum(item.swap_bytes for item in (*duplicate, *scoped)))))
        return tuple(result)

    def _sample_runner(self, record: ReservationRecord, use: int, now: float) -> None:
        maximum = Path(record.path).with_suffix(".max")
        try:
            peak = int(maximum.read_text())
        except (OSError, ValueError):
            peak = 0
        runner_peak = 0
        if record.sidecar:
            try:
                runner_peak = int(Path(record.sidecar).with_suffix(".max").read_text())
            except (OSError, ValueError):
                pass
        maximum_use = max(peak, use, runner_peak)
        if maximum_use > peak:
            atomic_sample(maximum, f"{maximum_use}\n")
        wave = Path(record.path).with_suffix(".wave")
        if use > 0 and use >= maximum_use / 2:
            atomic_sample(wave, f"{maximum_use} {int(now)}\n")
        elif runner_peak > 0 and runner_peak == maximum_use:
            try:
                sampled, at = map(int, Path(record.sidecar).with_suffix(".wave").read_text().split())
                if sampled == maximum_use:
                    atomic_sample(wave, f"{sampled} {at}\n")
            except (OSError, ValueError):
                pass

    def _progress_inputs(self, inputs: records.QueueInputs, processes: dict[int, gate.ProcessIdentity],
                         policy: records.AdmissionPolicy, profile_rank: int, profile_share: int) -> records.QueueInputs:
        """Capture counters and profile samples on the service loop thread."""
        attributed = iter(gate.attributed_step_uses(inputs))
        running: list[records.RunningStep] = []
        cutoff = inputs.now - step_estimator.HISTORY_DAYS * 86400
        for step in inputs.running:
            if not gate.running_has_reservation(step):
                running.append(step)
                continue
            row, step, use = next(attributed)
            waiter = row.get("queue_waiter")
            if waiter is not None and "verify" in waiter:
                step = replace(step, verify=waiter["verify"])
            if row["step"] not in gate.PROGRESS_STEPS:
                running.append(step)
                continue
            memory = step.measured
            if not isinstance(memory, records.MeasuredMemory):
                running.append(step)
                continue
            sidecar = Path(row["sidecar"])
            counter = progress.read_counter(sidecar) if row["sidecar"] else progress.NoCounter()
            if isinstance(counter, progress.NoCounter) and row["sidecar"] and row["step"] != "gate":
                counter = self.progress_captures.log_counter(progress.progress_path(sidecar, "log"))
            if (isinstance(counter, progress.NoCounter) and isinstance(counter.token_wait, progress.NoTokenWait)
                    and row["step"] != "gate" and self.boot_id):
                pids = gate.process_tree({row["pid"]: row["start_time"]}, processes)
                counter = self.progress_captures.counter({pid: processes[pid].start for pid in pids}, self.boot_id)
            key = gate.reservation_profile_key(row, self.host)
            profile = self.progress_history.matching(key, cutoff, rank=profile_rank, share_percent=profile_share)
            adjusted = gate.progress_memory(row, memory, use, counter, profile, policy)
            running.append(replace(step, measured=adjusted))
            if use.owns_source and isinstance(use.use, records.AnonymousRunUse) and "reservation_id" in row:
                source = memory.anonymous_source
                if isinstance(source, records.ScopeSource):
                    reading_source: progress.ReadingSource = progress.ScopeReading(source.directory)
                    pids = tuple(sorted(source.members))
                elif isinstance(source, records.ProcessSource):
                    pids = tuple(item.pid for item in source.processes)
                    reading_source = progress.ProcessTreeReading(pids)
                else:
                    pids = ()
                    reading_source = progress.ReadingSourceNotRecorded("reading source unavailable")
                self.progress_history.observe(row["reservation_id"], key, counter, use.use.bytes, inputs.now,
                    reading_source=reading_source,
                    read_processes=lambda pids=pids, source=reading_source:
                        progress.PeakProcessesNotRecorded("reading source unavailable")
                        if isinstance(source, progress.ReadingSourceNotRecorded) else progress.read_processes(pids, self.proc.path))
        self.progress_captures.end_pass()
        return replace(inputs, running=tuple(running))

    def run_pass(self) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        return self._run_pass(self._take_configuration())

    def _read_configuration(self) -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
        configuration = gate.read_admission_configuration()
        if isinstance(configuration, gate.UsableAdmissionConfiguration):
            self.last_usable_configuration = configuration.config
        return configuration

    def _take_configuration(self) -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
        configuration = self.pending_configuration
        self.pending_configuration = NoPass()
        return self._read_configuration() if isinstance(configuration, NoPass) else configuration


    def _check_reading_alert(self, inputs: records.QueueInputs, result: gate.PassResult, config: records.MemoryConfig) -> None:
        """Measure unreadable holds with the service clock and start at most one sender."""
        if isinstance(inputs.host, records.ReadingUnavailable):
            path = Path(os.environ.get("BUILDLOG_MEMINFO", "/proc/meminfo"))
            reason = inputs.host.reason
        elif isinstance(inputs.pressure, records.PressureUnavailable):
            path = Path(os.environ.get("BUILDLOG_MEMORY_PRESSURE", "/proc/pressure/memory"))
            reason = inputs.pressure.reason
        else:
            self.reading_hold = NoReadingHold()
            if self.reading_alert_needs_clear:
                try:
                    self.reading_alert_needs_clear = not outage_alerts.clear(READING_ALERT_KEY, self.clock.now())
                except (OSError, ValueError) as error:
                    print(f"memory admission: cannot clear reading alert: {error}", file=sys.stderr)
            return
        self.reading_alert_needs_clear = True
        held = any(row.decision.state == "hold" and not isinstance(row.reason, records.TokenWaitingWaiter)
                   and not isinstance(row.decision.room.basis, records.WithinSlot)
                   for row in result.rows)
        if not held:
            self.reading_hold = NoReadingHold()
            return
        since = self.reading_hold.since if isinstance(self.reading_hold, UnreadableReadingHold) else self.clock.monotonic()
        self.reading_hold = UnreadableReadingHold(since, path, reason)
        elapsed = self.clock.monotonic() - since
        if elapsed < config.reading_alert_seconds:
            return
        if isinstance(self.reading_alert_delivery, ReadingAlertDelivery) and self.reading_alert_delivery.thread.is_alive():
            return
        try:
            claimed = outage_alerts.claim(READING_ALERT_KEY)
            if not isinstance(claimed, outage_alerts.AlertClaim):
                return
            thread = threading.Thread(target=deliver_reading_alert, args=(claimed, self.reading_hold, elapsed),
                                      name="admission-reading-alert", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                outage_alerts.release(claimed)
                raise
            self.reading_alert_delivery = ReadingAlertDelivery(thread)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"memory admission: cannot start reading alert: {error}", file=sys.stderr)

    def _pass_allowed_at(self) -> float:
        """Passes start at least min_pass_gap_seconds apart, so a burst of requests shares one pass."""
        config = self.last_usable_configuration
        gap = config.min_pass_gap_seconds if isinstance(config, records.MemoryConfig) else 0
        return self.last_pass_started + gap

    def _pass_gap_open(self) -> bool:
        return self.clock.monotonic() >= self._pass_allowed_at()

    def _pass_when_open(self, configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                        ) -> bool:
        """Run a pass now when the gap allows it. Otherwise keep the configuration and mark a pass due."""
        if self._pass_gap_open():
            _ = self._run_pass(configuration)
            return True
        if isinstance(self.pending_configuration, NoPass):
            self.pending_configuration = configuration
        self.next_pass = min(self.next_pass, self.clock.monotonic())
        return False

    def _run_pass(self, configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                  ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        token = _PASS_PROVIDER_CONFIGURATION.set(ProviderConfiguration())
        try:
            with target_config.command_configuration():
                result = self._pass(configuration)
                self._answer_parked_placements()
                return result
        finally:
            _PASS_PROVIDER_CONFIGURATION.reset(token)

    def _answer_parked_placements(self) -> None:
        parked, self.parked_placements = self.parked_placements, []
        for item in parked:
            if not item.connection.finished and item.connection.socket.fileno() >= 0:
                self._send(item.connection, self._placement_reply(item.request,
                    self.local_hold_holders.get(item.request.episode, placement.ResourceHolderUnknown())))

    def _pass(self, configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
              ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        self.last_pass_started = self.clock.monotonic()
        self._prune_runs()
        self.configuration = configuration
        self.pass_stale = False
        if isinstance(self.configuration, gate.UnusableAdmissionConfiguration):
            self.local_hold_holders.clear()
            self.reading_hold = NoReadingHold()
            self.next_pass = self.next_config_watch_retry
            self.next_source_check = (self.clock.monotonic() + self.last_usable_configuration.source_check_seconds
                                      if isinstance(self.last_usable_configuration, records.MemoryConfig) else float("inf"))
            self.last_inputs = self.last_result = self.last_policy = NoPass()
            self.published = NoPass()
            self.last_diagnostics = ()
            for episode, record in list(self.reservations.items()):
                if self._end_if_finished(episode, record):
                    self._prune_places()
            self.persist()
            (self.state_dir / "queue.json").unlink(missing_ok=True)
            for episode, waiting in tuple(self.waiters.items()):
                if self.waiters.get(episode) is not waiting:
                    continue
                refusal = wire.Refused(self.configuration.reason)
                if isinstance(waiting.connection, Connection):
                    if self._detach_admission(waiting.connection, refusal):
                        self._prune_watches()
                else:
                    self._deliver(waiting, refusal)
            return self.configuration
        config = self.configuration.config
        self.last_usable_configuration = config
        self.next_source_check = min(self.next_source_check,
                                     self.clock.monotonic() + config.source_check_seconds)
        policy = config.policy
        self.last_diagnostics = config.diagnostics
        evidence_keys: PlacementEvidenceKeys = {}
        self._settle_attempt_reports(evidence_keys)
        remote_start_hold_seconds = self.configuration.remote_start_hold_seconds
        self._prepare_placements(evidence_keys, remote_start_hold_seconds)
        captured = self._inputs(config, self.configuration.progress_profile_rank, self.configuration.peak_history_share_percent,
                                self.configuration.progress_profile_sweep_seconds)
        inputs = captured.local
        if self.host == "natedev" and (floor := config.free_floor_gib) is not None:
            inputs = replace(inputs, build_space=self.readings.build_space(floor))
        ci_holds = self._ci_hold_reasons()
        held_owners = {records.waiter_owner(self.waiters[episode].row) for episode in ci_holds}
        if held_owners:
            inputs = replace(inputs, ordered=[(path, row) for path, row in inputs.ordered
                                             if records.waiter_owner(row) not in held_owners])
        inputs = self._placement_pass(inputs, policy, remote_start_hold_seconds)
        result = gate.admission_pass(inputs, inputs.now, policy)
        for reservation in result.policy.returned_claims:
            del self.slots[reservation]
        self._check_reading_alert(inputs, result, config)
        self.last_inputs, self.last_result, self.last_policy = inputs, result, policy
        cloud_started = self._cloud_pass(inputs, self.configuration)
        by_owner = {records.waiter_owner(waiting.row): episode for episode, waiting in self.waiters.items()}
        head = result.policy.queued_head
        head_label = (wait_line_name(self.waiters[by_owner[records.waiter_owner(head.waiter)]])
                      if isinstance(head, gate.QueuedHead) else "")
        head_need = head.waiter["need"] if isinstance(head, gate.QueuedHead) else 0
        starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                        key=lambda row: cast(gate.ProposedAdmission, row.admission).rank)
        for decision in starts:
            episode = by_owner[decision.owner]
            waiting = self.waiters.get(episode)
            if waiting is None:
                continue
            cargo_folder = inputs.cargo_folders.get(decision.owner, placement.NoCargoFolder())
            placement_request = self.placement_requests.get(episode)
            if placement_request is not None:
                candidate = next(candidate for candidate in self.placement_eligibility[episode]
                    if candidate.destination.provider == "local" and isinstance(candidate.eligibility, placement.AllowedHost))
                assignment = self._new_assignment(placement_request, candidate.destination, remote_start_hold_seconds)
                key = execution_identity.attempt_key(episode, assignment.attempt)
                self.assignments[key] = replace(self.assignments[key], permission=placement.PermissionConsumed(inputs.now))
                if isinstance(cargo_folder, placement.CargoFolder):
                    for slot_key, slot in list(self.slots.items()):
                        if isinstance(slot, records.ContinuationSlot) and inputs.cargo_folders.get(slot.owner) == cargo_folder:
                            del self.slots[slot_key]
            proposed = cast(gate.ProposedAdmission, decision.admission)
            self.spacing = proposed.spacing
            self._admit([waiting], config, self.configuration.peak_history_share_percent, cargo_folder=cargo_folder)
        self.persist()
        waiting_steps = tuple(records.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
            row.get("step", ""), row.get("folder", ""), row["since"], row["need"], decision.reason, row.get("unit", ""))
            for (_, row), decision in zip(inputs.ordered, result.rows, strict=True))
        positions = {row.owner: position for position, row in enumerate(result.rows, 1)}
        for position, decision in enumerate(result.rows, 1):
            episode = by_owner[decision.owner]
            if episode in self.waiters:
                waiting = self.waiters[episode]
                reason = decision.reason
                cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
                order = decision.decision.order_hold
                text = (f"behind #{positions[records.waiter_owner(order.waiter)]}"
                        if isinstance(cause, records.BehindHead) and isinstance(order, records.HeldBehindHead)
                        else reason_phrase(reason, waiting_steps))
                reply = wire.Wait(reason, position, head_label, text,
                    self._wait_snapshot(waiting, decision.decision, inputs, config, reason, decision.holder,
                                        head_label, head_need))
                if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                    self._deliver(waiting, reply)
                    waiting.last_reply, waiting.last_sent = reply, inputs.now
        for position, (episode, reason) in enumerate(ci_holds.items(), len(result.rows) + 1):
            waiting = self.waiters[episode]
            decision = records.AdmissionDecision("hold", waiting.row["need"], 0, 0, 0, free_passes=False)
            reply = wire.Wait(reason, position, "", reason_phrase(reason, ()),
                self._wait_snapshot(waiting, decision, inputs, config, reason, records.NoHolder(), head_label, head_need))
            if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                self._deliver(waiting, reply)
                waiting.last_reply, waiting.last_sent = reply, inputs.now
        interval = (config.waiting_pass_seconds if self.waiters or any(self._placement_waiting(request)
                    for request in self.placement_requests.values()) else config.running_pass_seconds)
        self.next_pass = self.clock.monotonic() + interval
        if starts or cloud_started:
            self.next_pass = self.clock.monotonic()
        remaining = {owner for owner, episode in by_owner.items() if episode in self.waiters}
        local_rows = ((path, gate.rank_waiter(row, inputs.waiter_orders[owner])) for path, row in inputs.ordered
                      if (owner := records.waiter_owner(row)) in remaining)
        held_rows = ((path, row) for path, row in captured.queue_order
                     if (owner := records.waiter_owner(row)) in held_owners and owner in remaining)
        ordered = list(heapq.merge(local_rows, held_rows, key=lambda item: gate.waiter_order(item[1])))
        try:
            queue_snapshot.write_queue_snapshot(self.state_dir,
                replace(inputs, ordered=ordered, spacing=self.spacing,
                        slots=tuple(slot for slot in inputs.slots
                            if gate.between_steps(slot).reservation not in result.policy.returned_claims)), policy)
        except OSError:
            # An old snapshot expires without changing the admission result.
            pass
        self._publish_replies()
        if self.reader_requested:
            self.start_reader()
        if not isinstance(self.system_reader, NoSystemReader):
            self.system_reader.update(self._reading_plan(config))
        return result

    def _register_cloud_group(self, place: wire.CloudPlace, group: status_wire.CloudGroup
                              ) -> wire.Refused | None:
        pending = PendingCloudGroup(place.target, group)
        previous = self.pending_cloud_groups.get(group.key)
        if previous is not None:
            if previous != pending:
                return wire.Refused("cloud group members disagree on the group")
            return None
        if any(isinstance(record.place, wire.CloudPlace) and isinstance(record.place.group, status_wire.CloudGroup)
               and record.place.group.key == group.key for record in self.reservations.values()):
            return wire.Refused("cloud group already has reservations")
        self.pending_cloud_groups[group.key] = pending
        if self.proc.owner_start(group.coordinator.pid) == LiveOwnerIdentity(group.coordinator.start_time):
            self._watch_owner(group.coordinator)
        self.persist()
        return None

    @staticmethod
    def _cloud_order(waiting: WaitingAdmission) -> tuple[float, int, str]:
        request = waiting.request
        assert isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
        return status_wire.cloud_queue_order(request.place, waiting.row["since"])

    def _cloud_waiters(self) -> list[WaitingAdmission]:
        return sorted((waiting for waiting in self.waiters.values()
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace)),
            key=self._cloud_order)

    def _cloud_groups(self, waiting_cloud: list[WaitingAdmission], slots: int
                      ) -> dict[str, list[WaitingAdmission]]:
        groups: dict[str, list[WaitingAdmission]] = {}
        for waiting in waiting_cloud:
            request = waiting.request
            assert isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
            if isinstance(group := request.place.group, status_wire.CloudGroup):
                if not owner_may_be_live(self.proc.owner_start(request.owner.pid), request.owner.start_time):
                    self._remove_waiter(waiting)
                    continue
                groups.setdefault(group.key, []).append(waiting)
        refused: list[tuple[WaitingAdmission, wire.Refused]] = []
        removed = False
        for key, pending in tuple(self.pending_cloud_groups.items()):
            group = pending.group
            coordinator = group.coordinator
            members = groups.get(key, [])
            reason = ""
            if not owner_may_be_live(self.proc.owner_start(coordinator.pid), coordinator.start_time):
                reason = "cloud group coordinator no longer exists"
            elif group.size > slots:
                reason = f"cloud group needs {group.size} slots but only {slots} are configured"
            elif len(members) > group.size:
                reason = "cloud group has more members than slots"
            if reason:
                reply = wire.Refused(reason)
                del self.pending_cloud_groups[key]
                _ = groups.pop(key, None)
                removed = True
                for member in members:
                    self.ledger[member.request.episode] = ReplayEntry(reply, self.clock.now())
                    refused.append((member, reply))
        if removed:
            self.persist()
            for waiting, reply in refused:
                self._remove_waiter(waiting, reply)
            self._prune_watches()
        return groups

    def _cloud_pass(self, inputs: records.QueueInputs, configuration: gate.UsableAdmissionConfiguration) -> bool:
        waiting_cloud = self._cloud_waiters()
        if not waiting_cloud and not self.pending_cloud_groups:
            return False
        config = configuration.config
        slots = configuration.cloud_gate_slots
        groups = self._cloud_groups(waiting_cloud, slots)
        disconnected = min(((pending.group.started_at, 0, key)
            for key, pending in self.pending_cloud_groups.items() if key not in groups),
            default=(float("inf"), 0, ""))
        used = sum(isinstance(record.place, wire.CloudPlace) for record in self.reservations.values())
        started = False
        blocked = False
        position = 0
        for waiting in waiting_cloud:
            request = waiting.request
            if self.waiters.get(request.episode) is not waiting:
                continue
            assert isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
            group = request.place.group
            members = groups[group.key] if isinstance(group, status_wire.CloudGroup) else [waiting]
            size = group.size if isinstance(group, status_wire.CloudGroup) else 1
            if not blocked:
                if self._cloud_order(waiting) < disconnected and len(members) == size and used + size <= slots:
                    self._admit(members, config, configuration.peak_history_share_percent, cargo_folder=placement.NoCargoFolder())
                    used += size
                    started = True
                    continue
                blocked = True
            position += 1
            reason = wire.CloudSlotWait(used, slots)
            snapshot = AdmissionWaitSnapshot(records.AdmissionDecision("hold", 0, 0, 0, 0),
                replace(waiting.request.profile.peak, bytes=0), (), 0,
                waiting.request.profile.repo, waiting.request.profile.step, config.policy, config.diagnostics)
            reply = wire.Wait(reason, position, "", reason.text, snapshot)
            if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                self._deliver(waiting, reply)
                waiting.last_reply, waiting.last_sent = reply, inputs.now
        return started

    def _cloud_reading(self, reading: records.QueueRead, configuration: gate.UsableAdmissionConfiguration
                       ) -> records.QueueRead | wire.CloudQueueRead:
        rows: list[wire.CloudRow] = []
        reservations = [record for record in self.reservations.values() if isinstance(record.place, wire.CloudPlace)]
        used, slots = len(reservations), configuration.cloud_gate_slots
        recorded_launch = False
        for record in reservations:
            place = record.place
            assert isinstance(place, wire.CloudPlace)
            waiter = record.queue_waiter
            identity = wire.Identity(waiter.get("session_id", ""), waiter.get("session_name", ""),
                record.step, record.worktree, waiter.get("unit", ""), job=record.gate_job,
                verify=waiter.get("verify", records.NotVerifyCall()))
            sidecar = Path(record.sidecar)
            started_at = step_estimator.epoch(record.admitted_at)
            current = cloud_progress.read(sidecar) if record.sidecar else cloud_progress.NoCloudProgress()
            if isinstance(current, cloud_progress.NoCloudProgress) or current.updated_at < started_at:
                current = cloud_progress.NoCloudProgress()
            elif not record.cloud_launch_seen:
                record = replace(record, cloud_launch_seen=True)
                self.reservations[record.episode] = record
                recorded_launch = True
            if record.cloud_launch_seen:
                counter = progress.read_counter(sidecar) if record.sidecar else progress.NoCounter()
                wait_state = records.ReadyForAdmission()
            else:
                counter = progress.NoCounter()
                wait_state = wire.CloudSlotWait(used, slots, status_wire.ReservedCloudSlot())
            rows.append(wire.CloudRow(record.owner, identity, place, waiter["since"], started_at,
                current, counter, wait_state, repo_identity=waiter.get("repo_identity", "")))
        if recorded_launch:
            self.persist()
        waiting_cloud = self._cloud_waiters()
        if not rows and not waiting_cloud:
            return reading
        for waiting in waiting_cloud:
            request = waiting.request
            assert isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
            sidecar = Path(request.sidecar)
            rows.append(wire.CloudRow(request.owner, request.identity, request.place, waiting.row["since"], 0.0,
                cloud_progress.read(sidecar) if request.sidecar else cloud_progress.NoCloudProgress(),
                progress.read_counter(sidecar) if request.sidecar else progress.NoCounter(), wire.CloudSlotWait(used, slots),
                repo_identity=waiting.row.get("repo_identity", "")))
        return wire.CloudQueueRead(reading.view, reading.explanation, reading.ordered, reading.diagnostics,
            tuple(rows), slots, cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes)

    def _placement_wait_reading(self, reading: records.QueueRead) -> records.QueueRead:
        if not self.placement_requests:
            return reading
        waiting: list[records.WaitingStep] = []
        ordered: list[tuple[Path, records.RankedWaiter]] = []
        shown: set[str] = set()
        pending_starts = reading.explanation.decided_from.pending_remote_starts
        for step, (path, row) in zip(reading.view.waiting, reading.ordered, strict=True):
            episode = row.get("placement_episode", "")
            request = self.placement_requests.get(episode)
            if request is not None:
                shown.add(episode)
                saved = self.assignments.get(self.current_assignments.get(episode, ""))
                if saved is not None and saved.assignment.destination.provider != "local":
                    destination = saved.assignment.destination
                    if records.waiter_owner(row) not in pending_starts or destination.provider == "mac":
                        continue
                    decision = records.PlacementBlock(destination.provider, destination.target, "starting", episode)
                else:
                    candidates = self.placement_eligibility.get(episode, ())
                    if saved is None and self._placement_refusal(request, candidates) is not None:
                        continue
                    decision = self._placement_wait_report(request, step.reason)
                    if decision is None:
                        continue
                step = replace(step, placement_block=decision if isinstance(decision, records.PlacementBlock) else None,
                               placement_pending=isinstance(decision, wire.PlacementEvidenceWait),
                               placement_episode=episode)
            waiting.append(step)
            ordered.append((path, row))
        remote = (request for episode in self.active_placement_waits - shown - self.current_assignments.keys()
                  if (request := self.placement_requests.get(episode)) is not None)
        for request in sorted(remote, key=lambda item: item.since):
            candidates = self.placement_eligibility.get(request.episode, ())
            if self._placement_refusal(request, candidates) is not None:
                continue
            decision = self._placement_wait_report(request, None)
            if decision is None:
                continue
            row = self._placement_row(request)
            identity = request.identity
            if not row.get("folder"):
                row["folder"] = request.profile.worktree
            waiting.append(records.WaitingStep(row["pid"], identity.session_id, identity.name,
                identity.step or request.profile.step, row.get("folder", ""),
                row["since"], row["need"], records.ReadyForAdmission(), identity.unit,
                project=gate.step_project(request.profile.repo), route=request.route, verify=identity.verify,
                job=identity.job, placement_block=decision if isinstance(decision, records.PlacementBlock) else None,
                placement_pending=isinstance(decision, wire.PlacementEvidenceWait),
                placement_episode=request.episode))
            ordered.append((self.state_dir / records.waiter_owner(row), gate.rank_waiter(row, records.NormalOrder())))
        return replace(reading, view=replace(reading.view, waiting=tuple(waiting)), ordered=ordered)

    def _placement_refusal(self, request: wire.PlacementRequest,
                           candidates: tuple[placement.CandidateEligibility, ...]) -> str | None:
        """Return the refusal for the current candidate readings."""
        selection = request.constraints.selection
        if isinstance(selection, placement.MacOnlyHost):
            refusal = next((candidate.eligibility.reason for candidate in candidates
                if candidate.destination.target == "mac"
                and isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion))
                and candidate.eligibility.reason != "busy"), "")
            if refusal:
                return refusal
        if isinstance(selection, placement.PinnedHost):
            selected = tuple(candidate for candidate in candidates if candidate.destination.target == selection.target)
            if selected and all(isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion))
                    and isinstance(placement.retry_decision(candidate.destination, candidate.eligibility.reason, self.clock.now()),
                                 placement.DoNotRetry) for candidate in selected):
                return "; ".join(candidate.eligibility.reason for candidate in selected
                    if isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion)))
        if candidates and all(isinstance(candidate.eligibility, placement.FixedExclusion) for candidate in candidates):
            return "; ".join(candidate.eligibility.reason for candidate in candidates
                            if isinstance(candidate.eligibility, placement.FixedExclusion))
        return None

    def _placement_wait_report(self, request: wire.PlacementRequest, reason: records.WaitReason | None
                                 ) -> records.WaitReason | records.PlacementBlock | wire.PlacementEvidenceWait | None:
        """Record the resource that holds the request."""
        candidates = self.placement_eligibility.get(request.episode, ())
        cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
        if reason is not None and (
                isinstance(cause, (placement.CargoFolderWait, records.BuildSpaceHold, records.EstimateExpired))
                or any(candidate.destination.provider == "local"
                       and isinstance(candidate.eligibility, placement.AllowedHost) for candidate in candidates)):
            return reason
        if (block := self.placement_resource_waits.get(request.episode)) is not None:
            return block
        possible = [candidate for candidate in candidates if not isinstance(candidate.eligibility, placement.FixedExclusion)]
        # Prefer the local hold. Otherwise use the first blocked route when all routes have a known blocker.
        candidate = next((item for item in possible if item.destination.provider == "local"
                          and isinstance(item.eligibility, placement.TemporaryExclusion)
                          and item.eligibility.reason in ("blocked", "busy", "starting")), None)
        if candidate is None and possible and all(
                isinstance(item.eligibility, placement.TemporaryExclusion) for item in possible):
            candidate = possible[0]
        if candidate is not None and isinstance(candidate.eligibility, placement.TemporaryExclusion):
            destination = candidate.destination
            if destination.provider == "mac":
                return None
            return records.PlacementBlock(destination.provider, destination.target, candidate.eligibility.reason,
                                          request.episode)
        if any(item.destination.provider != "mac" for item in possible) or not candidates:
            return wire.PlacementEvidenceWait()
        return None

    def _placement_reading(self, reading: records.QueueRead) -> records.QueueRead:
        remote: list[records.RunningStep] = []
        for key, saved in self.assignments.items():
            assignment = saved.assignment
            if assignment.destination.provider == "local":
                continue
            request = self.placement_requests.get(assignment.episode)
            if request is None:
                continue
            evidence = self.execution_evidence.get(key, execution_identity.ExecutionEvidenceUnavailable(
                "execution evidence is unavailable", self.clock.now()))
            policy_key = self.placement_host_keys.get((request.episode, assignment.destination))
            host_evidence = self.host_evidence(policy_key) if policy_key is not None else None
            retained = False
            if isinstance(host_evidence, placement.FreshHostEvidence):
                retained = any(claim.reference.episode == assignment.episode and claim.reference.attempt == assignment.attempt
                    for claim in (*host_evidence.claims, *host_evidence.cleanup_claims))
                if not isinstance(evidence, execution_identity.ObservedExecution):
                    evidence = next((execution for reference, execution in host_evidence.executions
                        if reference == saved.provider), evidence)
            if not isinstance(saved.state, placement.CurrentAssignment) and not retained:
                continue
            identity = request.identity
            pid: int | records.PidNotRecorded = records.PidNotRecorded()
            if isinstance(evidence, execution_identity.ObservedExecution) and isinstance(evidence.identity, execution_identity.ProcessExecution):
                pid = evidence.identity.pid
            remote.append(records.RunningStep(identity.session_id, identity.name, identity.step or request.profile.step,
                identity.folder or request.profile.worktree, 0, records.RemoteMemoryUnavailable("remote memory is unavailable"),
                pid=pid, started_at=saved.permission.consumed_at if isinstance(saved.permission, placement.PermissionConsumed) else 0,
                unit=identity.unit, project=gate.step_project(request.profile.repo), execution=evidence,
                assignment_host=assignment.destination.host, verify=identity.verify,
                mac_placement=f"{assignment.episode}/{assignment.attempt}"
                    if assignment.destination.provider == "mac" else ""))
        if not remote:
            return reading
        explained = tuple(records.ExplainedRunningStep(step, records.AdmissionTimeNotRecorded(), 0, 0, 0,
            owner=records.execution_group(step, str(index))) for index, step in enumerate(remote))
        return replace(reading, view=replace(reading.view, running=(*reading.view.running, *remote)),
            explanation=replace(reading.explanation, running=(*reading.explanation.running, *explained)))

    def _publish_replies(self) -> None:
        if (not isinstance(self.configuration, gate.UsableAdmissionConfiguration)
                or not isinstance(self.last_inputs, records.QueueInputs)
                or not isinstance(self.last_policy, records.AdmissionPolicy)
                or not isinstance(self.last_result, gate.PassResult)):
            return
        reading = replace(gate.queue_read_from_pass(self.last_inputs, self.last_policy, self.last_result),
                          diagnostics=self.last_diagnostics)
        waiting = tuple(replace(step, verify=row["verify"]) if "verify" in row else step
                        for step, (_, row) in zip(reading.view.waiting, reading.ordered, strict=True))
        reading = replace(reading, view=replace(reading.view, waiting=waiting))
        ci_holds = self._ci_hold_reasons()
        if ci_holds:
            held_steps = tuple(records.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
                row.get("step", ""), row.get("folder", ""), row["since"], row["need"], reason,
                row.get("unit", ""), route=records.waiter_route(row), job=row.get("job", GATE_JOB_NOT_RECORDED),
                ci_runner=row.get("ci_runner", ""), verify=row.get("verify", records.NotVerifyCall()))
                for episode, reason in ci_holds.items() for row in (self.waiters[episode].row,))
            reading = replace(reading, view=replace(reading.view, waiting=(*reading.view.waiting, *held_steps)),
                ordered=[*reading.ordered, *((self.state_dir / records.waiter_owner(self.waiters[episode].row),
                    gate.rank_waiter(self.waiters[episode].row, records.NormalOrder())) for episode in ci_holds)])
        reading = self._placement_wait_reading(self._placement_reading(reading))
        reading = self._cloud_reading(reading, self.configuration)
        view = wire.ViewReply(reading)
        explain = wire.ExplainReply(reading.explanation, self.last_diagnostics)
        self.published = PublishedReplies(view, explain)

    def reply_bytes(self, reply: wire.Reply, *, version: int = wire.VERSION) -> bytes:
        if version != wire.VERSION:
            return wire.encode_reply(reply, version=version).encode()
        published = self.published
        if isinstance(published, PublishedReplies):
            if reply is published.view:
                if not published.view_bytes:
                    published.view_bytes = wire.encode_reply(reply).encode()
                return published.view_bytes
            if reply is published.explain:
                if not published.explain_bytes:
                    published.explain_bytes = wire.encode_reply(reply).encode()
                return published.explain_bytes
            if isinstance(reply, wire.MineReply) and reply.reading is published.view.reading:
                if not published.mine_bytes:
                    published.mine_bytes = self.reply_bytes(published.view).replace(
                        b'"type":"view","data":{"kind":"ViewReply",',
                        b'"type":"mine","data":{"kind":"MineReply",', 1)
                return published.mine_bytes
        return wire.encode_reply(reply).encode()


    def _save_place(self, waiting: WaitingAdmission) -> None:
        row, request = waiting.row, waiting.request
        if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
            return
        key = _place_key(request.owner, row["since"])
        previous = self.places.get(key)
        place = waiter_place(row, request.episode)
        self.places[key] = replace(place, queue_identity=previous.queue_identity) if previous is not None else place


    def _wait_snapshot(self, waiting: WaitingAdmission, decision: records.AdmissionDecision,
                       inputs: records.QueueInputs, config: records.MemoryConfig,
                       reason: records.WaitReason, holder: WaitHolder, head_label: str,
                       head_need: int) -> AdmissionWaitSnapshot:
        return AdmissionWaitSnapshot(decision, waiting.request.profile.peak, inputs.running,
            inputs.host.available if isinstance(inputs.host, records.HostMemory) else 0,
            waiting.request.profile.repo, waiting.request.profile.step, config.policy, config.diagnostics,
            head_need if not isinstance(decision.order_hold, records.NoOrderHold) else 0, head_label, reason, holder)

    def _drain_lifecycle(self) -> None:
        """Publish saved lifecycle facts after each durable state transition."""
        while self.lifecycle_outbox:
            pending = self.lifecycle_outbox[0]
            if isinstance(pending, PendingAdmission):
                record = self.reservations[pending.episode]
                admitted_at = datetime.fromisoformat(record.admitted_at).timestamp()
                event: gate.AdmittedEvent = {
                    **gate.lifecycle_event(gate.queue_life_identity(record.queue_waiter), "admitted", record.reservation_id),
                        "reservation_id": record.reservation_id, "repo": record.repo,
                        "admitted_at": admitted_at, "need": record.need, "need_source": pending.need_source,
                        "is_head": pending.is_head,
                        "wait_seconds": pending.wait_seconds,
                }
                gate.append_queue_time(self.state_dir, cast(dict[str, object], codec.snapshot_value(event)), admitted_at)
                self.fault("after_admitted_append")
            else:
                left: gate.LeftEvent = {
                    **gate.lifecycle_event(pending.identity, "left", pending.episode + ":" + pending.reason),
                    "reason": pending.reason, "ended_at": pending.ended_at,
                }
                gate.append_queue_time(self.state_dir, cast(dict[str, object], codec.snapshot_value(left)), pending.ended_at)
                self.fault("after_left_append")
            self.lifecycle_outbox = self.lifecycle_outbox[1:]

    def _record_left(self, episode: str, identity: records.QueueLifeIdentity, reason: gate.LeftReason) -> None:
        self.lifecycle_outbox = (*self.lifecycle_outbox, PendingLeft(identity, episode, reason, self.clock.now()))

    def _freeze_test_phase_need(self, record: ReservationRecord, config: records.MemoryConfig,
                                share_percent: int) -> ReservationRecord:
        if record.step != "nextest":
            return record
        need = gate.test_phase_need(reservation_row(record), self.host,
            datetime.fromtimestamp(self.clock.now(), UTC), self.state_dir,
            history_path=self.state_dir / "anon_peaks.jsonl", config=config, share_percent=share_percent)
        return replace(record, test_phase_need=need)

    def _prepare_admission(self, waiting: WaitingAdmission, now: float, config: records.MemoryConfig,
                           share_percent: int, cargo_folder: placement.CargoFolder | placement.NoCargoFolder
                           ) -> ReservationRecord | wire.LaunchAdmitted:
        request, profile = waiting.request, waiting.request.profile
        if isinstance(request, wire.LaunchRequest):
            return wire.LaunchAdmitted(profile.peak.bytes, profile.peak.source)
        stem = gate.reservation_stem(self.state_dir, request.owner.pid, request.owner.start_time, int(now * 1_000_000_000))
        # The episode disambiguates grants at the same clock tick after release.
        while any(record.path == str(stem) for record in self.reservations.values()):
            stem = stem.with_name(stem.name + "0")
        if profile.step == "build":
            shape = build_shape(list(request.argv)).key
        elif profile.step in {"gate", "merge-gate"}:
            shape = gate.merge_gate_shape(request.argv)
        else:
            shape = ""
        cloud = isinstance(request.place, wire.CloudPlace)
        record = ReservationRecord(request.episode, str(stem), request.owner, 0 if cloud else profile.peak.bytes,
            profile.repo, profile.step, profile.worktree, store.utc_iso(now), request.sidecar,
            request.test_width, request.call, stem.name, records.stored_waiter(waiting.row),
            profile.repository.key if isinstance(profile.repository, records.RepositoryIdentity) else "",
            profile.scope, step_estimator.test_package(list(request.argv)), tuple(step_estimator.test_filters(list(request.argv))),
            shape, run=request.run, place=request.place, cargo_folder=cargo_folder,
            gate_job=admitted_gate_job(request, self.proc.path))
        self.writes.register(record)
        if cloud:
            return record
        if isinstance(request.run, wire.RunIdentity):
            record = replace(record, slot_staff=step_staff(record.queue_waiter, self.host))
        return self._freeze_test_phase_need(record, config, share_percent)

    def _admit(self, waiters: list[WaitingAdmission], config: records.MemoryConfig, share_percent: int, *,
               cargo_folder: placement.CargoFolder | placement.NoCargoFolder) -> None:
        now = self.clock.now()
        prepared = [self._prepare_admission(waiting, now, config, share_percent, cargo_folder) for waiting in waiters]
        replies: list[wire.Admitted | wire.LaunchAdmitted] = []
        for waiting, admission in zip(waiters, prepared, strict=True):
            request = waiting.request
            if isinstance(admission, wire.LaunchAdmitted):
                reply: wire.Admitted | wire.LaunchAdmitted = admission
                self.ledger[request.episode] = ReplayEntry(reply, now)
            else:
                result = self.last_result
                candidate = result.policy.queued_head if isinstance(result, gate.PassResult) else gate.NoQueuedHead()
                source = request.profile.peak.source
                self.lifecycle_outbox = (*self.lifecycle_outbox, PendingAdmission(request.episode, source,
                    isinstance(candidate, gate.QueuedHead)
                        and records.waiter_owner(candidate.waiter) == records.waiter_owner(waiting.row),
                    max(0.0, now - waiting.row["since"])))
                self.reservations[request.episode] = admission
                for key, slot in list(self.slots.items()):
                    if isinstance(slot, records.ContinuationSlot) and slot.owner == records.waiter_owner(waiting.row):
                        del self.slots[key]
                stem = Path(admission.path)
                reply = wire.Admitted(stem.name, admission.need, source, str(stem.with_suffix(".max")),
                    str(stem.with_suffix(".wave")), {} if isinstance(admission.place, wire.CloudPlace)
                    else gate.sampling_context(reservation_row(admission)),
                    wire.CleanupTiming(config.silence_seconds, config.reconnect_seconds))
                self.ledger[request.episode] = ReplayEntry(reply, LiveGrant())
                self._watch_owner(request.owner)
            self._save_place(waiting)
            replies.append(reply)
        first = waiters[0].request
        if (isinstance(first, wire.AdmitRequest) and isinstance(first.place, wire.CloudPlace)
                and isinstance(group := first.place.group, status_wire.CloudGroup)):
            del self.pending_cloud_groups[group.key]
            self._prune_watches()
        # Save every grant before sending any reply for the group.
        self.persist()
        for waiting, reply in zip(waiters, replies, strict=True):
            self._remove_waiter(waiting, reply)

    def _end(self, episode: str, ending: ReleaseStatus | OwnerExited | NotEnding | TokenWaiting) -> None:
        record = self.reservations[episode]
        record = replace(record, ending=ending)
        self.reservations[episode] = record
        self.persist()
        row = reservation_row(record)
        if isinstance(ending, (OwnerExited, NotEnding)):
            lifetime = self._lifetime(record)
            if isinstance(lifetime, gate.PopulatedStepScope):
                self._watch_scope(lifetime, episode)
                return
        ended_at = (step_estimator.epoch(record.finalization["ended_at"])
                    if isinstance(record.finalization, dict) else self.clock.now())
        if record.episode.startswith("uid:"):
            self._sample_runner(record, 0, ended_at)
        self.progress_history.finish(record.reservation_id, ended_at, ended_at - step_estimator.HISTORY_DAYS * 86400)
        if record.sidecar:
            self.progress_captures.remove(Path(record.sidecar))
        path = Path(record.path)
        outcome, status, end_kind = finalization_outcome(ending)
        if isinstance(ending, TokenWaiting):
            row_waiter = record.queue_waiter.copy()
            row_waiter["token_left_at"] = ending.left_at
            self.places[_place_key(record.owner, row_waiter["since"])] = waiter_place(row_waiter, record.episode)
        _ = gate.finalize_reservation(path, row, outcome, datetime.fromtimestamp(ended_at, UTC), status,
            intents=StateIntents(self.reservations, self._advance_build_history, episode, self.persist, self.fault,
                                self.places, RegisteredServiceWrites(self.writes)),
            end_kind=end_kind)
        if (isinstance(record.place, wire.LocalPlace) and isinstance(record.run, wire.RunIdentity)
                and self._run_alive(record.run)
                and isinstance(self.last_usable_configuration, records.MemoryConfig)):
            if isinstance(record.slot_staff, records.NoEndedStepStaff):
                row["slot_staff"] = step_staff(record.queue_waiter, self.host)
            slot = gate.ended_step_slot(row, ended_at, self.last_usable_configuration.policy)
            if isinstance(slot, records.BetweenStepsSlot):
                self.slots[slot.reservation] = slot
        self._prune_watches()
        self._prune_host_evidence()
        for key, entry in list(self.ledger.items()):
            if entry.reservation == record.reservation_id:
                self.ledger[key] = replace(entry, ended_at=self.clock.now())
        previous = self.ledger.get(episode)
        if previous is not None:
            self.ledger[episode] = replace(previous, ended_at=self.clock.now())
        if isinstance(ending, (ReleaseStatus, TokenWaiting)):
            self.ledger[ending.request_id] = ReplayEntry(wire.Ok(), self.clock.now())
        self.persist()

    @staticmethod
    def _placement_settings(request: wire.PlacementRequest,
                            destination: placement.PlacementDestination) -> placement.ProviderSettings:
        return next((candidate.settings for candidate in request.constraints.candidates
                     if candidate.destination == destination), placement.ConfiguredProviderSettings())

    def host_evidence_key(self, request: wire.PlacementRequest,
                          destination: placement.PlacementDestination) -> placement.HostEvidenceKey:
        settings = self._placement_settings(request, destination)
        with conf.report_key_changes():
            generation = _provider_configuration().generation(destination.target, settings)
        return placement.HostEvidenceKey(destination, request.constraints.session,
            request.constraints.command, generation, request.constraints.repository,
            request.constraints.package, request.profile.step, settings,
            request.owner if destination.provider == "local" else placement.NoLocalHoldOwner(),
            request.constraints.command_kind)

    def _host_probe_timeout(self, key: placement.HostEvidenceKey) -> float:
        from scripts.aws_target import hana_placement

        with conf.report_key_changes():
            configuration = _provider_configuration()
            target = configuration.target(key.destination.target, key.settings)
            if isinstance(target, target_config.SshTarget):
                settings = configuration.read(target.settings)
                if key.destination.provider == "ssh":
                    transport = configuration.transport(target)
                    if isinstance(transport, hana_placement.HanaControlTransport):
                        return settings.value("aws_timeout_s", conf.positive_whole_seconds)
                    if isinstance(transport, HanaGpuNextestTransport):
                        return settings.number("api_timeout_s")
                return settings.number("probe_timeout_s")
            file = key.settings.path if isinstance(key.settings, placement.ExplicitProviderSettings) else "mac_test"
            return configuration.read(file).number("probe_timeout_s")

    def publish_host_evidence(self, evidence: placement.HostEvidence) -> None:
        self.host_observations[evidence.key] = evidence
        self.pass_stale = True
        reader = self.host_readers.get(evidence.key)
        if reader is not None:
            reader.publish(evidence)
        try:
            os.eventfd_write(self.reading_events, 1)
        except OSError:
            pass

    def host_evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        reader = self.host_readers.get(key)
        evidence = (reader.latest if reader is not None else self.host_observations.get(key,
                    placement.HostEvidenceUnavailable(key, self.clock.now(), "not read")))
        if isinstance(evidence, placement.FreshHostEvidence):
            try:
                lifetime = self._evidence_lifetime(key)
            except (OSError, ValueError) as error:
                return placement.HostEvidenceUnavailable(key, evidence.observed_at, str(error))
            if self.clock.now() - evidence.observed_at > lifetime:
                return placement.StaleHostEvidence(key, evidence.observed_at, "provider evidence expired")
        return evidence

    def _refresh_host_observations(self) -> None:
        """Apply worker evidence on the loop thread and invalidate queue read snapshots."""
        for key, reader in self.host_readers.items():
            evidence = reader.latest
            if evidence != self.host_observations.get(key):
                self.host_observations[key] = evidence
                self.pass_stale = True

    def _evidence_lifetime(self, key: placement.HostEvidenceKey) -> float:
        """Return how long one probe can take: a waiting pass plus the probe time limit."""
        reader = self.host_readers.get(key)
        timeout = (0.0 if key.destination.provider == "local" else
                   reader.timeout if reader is not None else self._host_probe_timeout(key))
        return self._waiting_pass_interval() + timeout

    def _waiting_pass_interval(self) -> float:
        configuration = self.last_usable_configuration
        return configuration.waiting_pass_seconds if isinstance(configuration, records.MemoryConfig) else 0.0

    def _request_host_evidence(self, request: wire.PlacementRequest,
                               destination: placement.PlacementDestination, keys: PlacementEvidenceKeys | None = None
                               ) -> placement.HostEvidence | placement.EvidenceNeeded:
        key = self._placement_evidence_key(request, destination, keys)
        if isinstance(key, placement.EvidenceNeeded):
            _ = self.placement_host_keys.pop((request.episode, destination), None)
            return key
        with conf.report_key_changes():
            try:
                return self._request_target_evidence(request, key)
            except conf.Unusable as error:
                return placement.HostEvidenceUnavailable(key, self.clock.now(), str(error))

    @staticmethod
    def _hana_memory_eligibility(request: wire.PlacementRequest, settings: conf.ConfigRead
                                 ) -> placement.AllowedHost | placement.TemporaryExclusion:
        if request.profile.peak.bytes > settings.value('step_memory_gib', conf.positive_whole) * 1024 ** 3:
            return placement.TemporaryExclusion('memory')
        return placement.AllowedHost()

    def _request_target_evidence(self, request: wire.PlacementRequest,
                                 key: placement.HostEvidenceKey) -> placement.HostEvidence:
        destination = key.destination
        self.placement_host_keys[(request.episode, destination)] = key
        if destination.provider == 'ssh':
            from scripts.aws_target import hana_placement

            configuration = _provider_configuration()
            target = configuration.target(destination.target, key.settings)
            if isinstance(target, target_config.SshTarget):
                transport = configuration.transport(target)
                if isinstance(transport, hana_placement.HanaControlTransport):
                    memory = self._hana_memory_eligibility(request, configuration.read(transport.settings))
                    if isinstance(memory, placement.TemporaryExclusion):
                        return placement.FreshHostEvidence(key, self.clock.now(), '', memory)
        if destination.provider == "local" and not self.background_local_evidence:
            try:
                return self.host_probe(key)
            except Exception as error:
                return placement.HostEvidenceUnavailable(key, self.clock.now(), str(error))
        self.host_requests[key] = request
        current = self.host_evidence(key)
        if isinstance(current, placement.FreshHostEvidence):
            return current
        if isinstance(self.last_usable_configuration, records.MemoryConfig):
            _ = self._host_evidence_reader(key, self.last_usable_configuration.waiting_pass_seconds)
        return self.host_evidence(key)

    def _placement_evidence_key(self, request: wire.PlacementRequest,
                                destination: placement.PlacementDestination, keys: PlacementEvidenceKeys | None = None
                                ) -> placement.HostEvidenceKey | placement.EvidenceNeeded:
        identity = (request.episode, destination)
        if keys is not None and identity in keys:
            return keys[identity]
        try:
            key: placement.HostEvidenceKey | placement.EvidenceNeeded = self.host_evidence_key(request, destination)
        except (OSError, ValueError) as error:
            key = placement.EvidenceNeeded(str(error))
        if keys is not None:
            keys[identity] = key
        return key

    def _host_evidence_reader(self, key: placement.HostEvidenceKey, interval: float) -> HostEvidenceReader:
        """Keep host and dispatch readings on the same target worker."""
        if key not in self.host_readers:
            timeout = 0.0 if key.destination.provider == "local" else self._host_probe_timeout(key)
            previous = self.host_observations.get(key)
            target = (key.destination.provider, key.destination.target)
            reader = self.host_targets.get(target)
            if reader is None:
                reader = HostEvidenceWorker(key, lambda current: self.host_probe(current), self.clock,
                    self.reading_events, interval)
                self.host_targets[target] = reader
            else:
                reader.request(key)
            self.host_readers[key] = HostEvidenceReader(key, reader, timeout)
            if previous is not None:
                reader.publish(previous)
        return self.host_readers[key]

    def read_provider_evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        """Read provider evidence without replacing the transport hook."""
        return self._read_host_evidence(key)

    def _read_host_evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        """Limit an unusable setting to the affected target."""
        with conf.report_key_changes():
            try:
                return self._read_target_evidence(key)
            except conf.Unusable as error:
                return placement.HostEvidenceUnavailable(key, self.clock.now(), str(error))

    def _read_target_evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        """Read local hold policy or remote provider claims."""
        from scripts.build_targets import build_targets
        from scripts.mac_test import claim_state, mac_test
        from scripts.mac_test.eligibility import recorded_step_kind
        from scripts.build_hold import build_hold

        destination = key.destination
        if destination.provider == "local":
            holds = build_hold.natedev_holds(build_hold.holder_directory())
            owner = key.local_hold_owner
            lineage = (build_targets.read_caller_lineage(self.proc.path, owner.pid, owner.start_time)
                       if isinstance(owner, placement.SubmittingOwner) and owner.host == self.host
                       else build_targets.UnavailableCallerLineage("the local caller identity is unavailable"))
            blocking = build_targets.hold_waited_on(holds, key.session, build_hold.holder_directory(), lineage)
            local_eligibility: placement.HostEligibility = (placement.AllowedHost()
                if isinstance(blocking, build_targets.NoBlockingHold) else placement.TemporaryExclusion("blocked"))
            holder: placement.ResourceHolder = placement.ResourceHolderUnknown()
            if not isinstance(blocking, build_targets.NoBlockingHold):
                try:
                    _ = uuid.UUID(blocking.key)
                except ValueError:
                    holder = placement.ResourceHolderUnit(blocking.name)
                else:
                    holder = placement.ResourceHolderSession(blocking.key)
            return placement.FreshHostEvidence(key, self.clock.now(), "", local_eligibility, holder=holder)
        request = self.host_requests[key]
        if destination.provider not in ("mac", "ssh"):
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "provider uses its existing admission path")
        target = records.effective_target(destination.target, key.settings)
        if isinstance(target, records.UnregisteredTarget):
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "target is no longer configured")
        configuration = conf.read(target.settings if isinstance(target, target_config.SshTarget) else "mac_test",
                                  load=conf.read_bytes)
        if isinstance(target, target_config.SshTarget):
            from scripts.aws_target import hana_placement

            transport = target_config.read_execution_transport(target, configuration=configuration)
            if isinstance(transport, HanaGpuNextestTransport):
                from scripts.aws_target.hana_placement_adapter import read_gpu_evidence

                return read_gpu_evidence(transport, key, request, configuration=configuration)
            if isinstance(transport, hana_placement.HanaControlTransport):
                return hana_placement.read_evidence(key, request, transport=transport, workload=configuration)
        sent = request.constraints.command_kind
        kind = recorded_step_kind(request.profile.step) if isinstance(sent, placement.CommandKindNotSent) else sent
        eligibility: placement.HostEligibility
        if build_targets.mac_run_bypasses_repository_policy(target, kind):
            eligibility = placement.AllowedHost()
        elif kind not in placement.STEP_COMMAND_KINDS:
            # The target can never run this command, so a pinned step refuses at once.
            return placement.FreshHostEvidence(key, self.clock.now(), "", placement.FixedExclusion("command"))
        else:
            if isinstance(target, target_config.SshTarget):
                answer = build_targets.ssh_target_eligibility(target, Path(request.constraints.repository),
                    request.constraints.package, kind, target_config.ANY_ELIGIBLE_TARGET,
                    new_step=True, session=key.session, configuration=configuration)
            else:
                answer = build_targets.mac_target_eligibility(target, Path(request.constraints.repository),
                    request.constraints.package, kind, target_config.ANY_ELIGIBLE_TARGET, session=key.session,
                    configuration=configuration)
            eligibility = (placement.TemporaryExclusion(answer.reason)
                if isinstance(answer, target_config.ExcludedTarget) else placement.AllowedHost())
        paths = claim_state.state_paths(destination.target)
        # Routing keeps a busy Mac eligible so a step can wait for it. Placement must wait too,
        # or each pass makes a new attempt that the Mac refuses as busy.
        if (isinstance(eligibility, placement.AllowedHost) and isinstance(target, target_config.SshTarget)
                and target.platform == "macos"
                and mac_test.free_now(paths, target.settings, Path(request.constraints.repository),
                                      session=key.session, configuration=configuration) == "busy"):
            eligibility = placement.TemporaryExclusion("busy")
        adopted: list[placement.AdoptedProviderClaim] = []
        cleanup: list[placement.RetainedCleanupClaim] = []
        executions: list[tuple[placement.ProviderClaimReference, execution_identity.ExecutionEvidence]] = []
        with claim_state.locked(paths.lock):
            held = claim_state.read_claims(paths)
        holder = placement.ResourceHolderUnknown()
        diagnostic: placement.BusyProviderDiagnostic | placement.NoProviderDiagnostic = placement.NO_PROVIDER_DIAGNOSTIC
        scope = claim_state.target_claim_scope(paths, request.constraints.repository)
        for claim in held:
            if (mac_test.scopes_overlap(claim.scope, scope)
                    and mac_test.run_holds_work(claim.record)):
                diagnostic = placement.BusyProviderDiagnostic(destination.target, claim.record["what"], claim.record["since"])
                session_id = claim.record.get("holder_session", "")
                if session_id and session_id != claim_state.session_id(key.session):
                    holder = placement.ResourceHolderSession(session_id)
                    break
        generation = ""
        for claim in held:
            record = claim.record
            if "remote_host" not in record:
                continue
            reference = placement.ProviderClaimReference(destination.provider, destination.target, claim.scope,
                record["claim_id"], record["episode"], record["attempt"])
            session_id = record["submitting_session"]
            session = OwningSession(session_id) if session_id else NoOwningSession()
            if record.get("cleanup_pending", False):
                cleanup.append(placement.RetainedCleanupClaim(reference, "provider cleanup remains unresolved"))
                executions.append((reference, execution_identity.ExecutionEvidenceUnavailable("provider cleanup remains unresolved", self.clock.now())))
                continue
            adopted.append(placement.AdoptedProviderClaim(reference,
                placement.ClaimHolderIdentity(self.host, record["pid"], record["proc_start"]),
                session, record["remote_host"], record.get("instance_generation", "")))
            executions.append((reference, self._observe_provider_execution(request, destination, reference,
                record["remote_host"], record.get("instance_generation", ""), configuration)))
        return placement.FreshHostEvidence(key, self.clock.now(), generation, eligibility, tuple(adopted), tuple(cleanup), tuple(executions), holder, diagnostic)

    def _observe_provider_execution(self, request: wire.PlacementRequest, destination: placement.PlacementDestination,
                                    reference: placement.ProviderClaimReference, host: str, generation: str,
                                    config: conf.ConfigRead) -> execution_identity.ExecutionEvidence:
        """Read existing mirror process evidence without invoking provider cleanup."""
        from scripts.mac_test import eligibility, offload, claim_state

        if isinstance(eligibility.read_backoff(time.time(), destination.target), eligibility.ActiveBackoff):
            return execution_identity.ExecutionEvidenceUnavailable("provider retry backoff is active", self.clock.now())
        repository = eligibility.repository_identity(Path(request.constraints.repository))
        if not isinstance(repository, eligibility.KnownRepository):
            return execution_identity.ExecutionEvidenceUnavailable("repository execution scope is unavailable", self.clock.now())
        scope = reference.scope
        slot = scope.slot if isinstance(scope, claim_state.RepositoryScope) and scope.slot else destination.slot or 1
        script = offload.mirror_selector_script(repository, destination.target, slot) + r'''
placement_pids=$(select_leftovers) || exit 1
for placement_pid in $placement_pids; do
    placement_start=$(ps -p "$placement_pid" -o lstart=) || continue
    [ -n "$placement_start" ] || continue
    printf '%s\t%s\n' "$placement_pid" "$placement_start"
done
'''
        probe = eligibility.SshProbeSettings(config)
        options, timeout = offload.ssh_options(probe), probe.probe_timeout_s
        try:
            result = subprocess.run(["ssh", *options, host, "sh -c " + shlex.quote(script)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                check=False, timeout=timeout)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    pid, separator, start = line.partition("\t")
                    if separator and pid.isdecimal() and int(pid) > 0 and start.strip():
                        return execution_identity.ObservedExecution(execution_identity.ProcessExecution(host, generation, int(pid), start.strip()))
                return execution_identity.ExecutionEvidenceUnavailable("remote execution remains unresolved", self.clock.now())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        offload.write_backoff(time.time() + config.number("unreachable_backoff_s"), destination.target)
        return execution_identity.ExecutionEvidenceUnavailable("remote execution remains unresolved", self.clock.now())

    def _prune_ended_placements(self, cutoff: float) -> None:
        """Remove the request and closed attempts of an episode whose owner ended before the cutoff."""
        last: dict[str, float] = {episode: request.since for episode, request in self.placement_requests.items()}
        for saved in self.assignments.values():
            episode = saved.assignment.episode
            report = saved.terminal_report
            at = report.received_at if isinstance(report, placement.TerminalAttemptReport) else saved.assignment.since
            last[episode] = max(last.get(episode, at), at)
        owners = {saved.assignment.episode: saved.assignment.owner for saved in self.assignments.values()}
        owners.update((episode, request.owner) for episode, request in self.placement_requests.items())
        ended = {episode for episode, at in last.items()
                 if at < cutoff and episode not in self.current_assignments
                 and not self._placement_owner_live(owners[episode])}
        if not ended:
            return
        self.assignments = {key: saved for key, saved in self.assignments.items()
                            if saved.assignment.episode not in ended}
        for episode in ended:
            _ = self.placement_requests.pop(episode, None)
            _ = self.prerequisites.pop(episode, None)
            _ = self.placement_eligibility.pop(episode, None)
            self._remove_waiter(self.waiters.get(episode))

    def _placement_owner_live(self, owner: placement.SubmittingOwner) -> bool:
        return owner.host == self.host and self._live(wire.Owner(owner.pid, owner.start_time))

    def _saved_assignment(self, episode: str, attempt: str
                          ) -> records.SavedPlacementAssignment | status_wire.UnknownPlacementStatus:
        return self.assignments.get(execution_identity.attempt_key(episode, attempt),
            status_wire.UnknownPlacementStatus(episode, attempt))

    def _provider_release_evidence(self, saved: records.SavedPlacementAssignment, keys: PlacementEvidenceKeys | None = None
                                   ) -> placement.HostEvidence | placement.EvidenceNeeded | NoSubmittedPlacementRequest:
        """Read evidence for the submitted request under the current configuration."""
        submitted = self.placement_requests.get(saved.assignment.episode, NoSubmittedPlacementRequest())
        if isinstance(submitted, NoSubmittedPlacementRequest):
            return submitted
        key = self._placement_evidence_key(submitted, saved.assignment.destination, keys)
        return key if isinstance(key, placement.EvidenceNeeded) else self.host_evidence(key)

    def _assignment_state(self, saved: records.SavedPlacementAssignment) -> placement.AssignmentState:
        """Project placement lifetime without changing records or requesting evidence."""
        if isinstance(saved.state, placement.ClosedAssignment):
            return saved.state
        assignment = saved.assignment
        if assignment.destination.provider == "local":
            replay = self.ledger.get(assignment.episode)
            reservation = self.reservations.get(assignment.episode)
            if (replay is None or not isinstance(replay.reply, wire.Admitted)
                    or reservation is None or not isinstance(reservation.ending, NotEnding)
                    or reservation.reservation_id != replay.reply.reservation):
                return placement.ClosedAssignment("local reservation has ended")
            if isinstance(reservation.run, wire.RunIdentity) and not self._run_alive(reservation.run):
                return placement.ClosedAssignment("local run has ended")
            if isinstance(self._lifetime(reservation), gate.EndedStep):
                return placement.ClosedAssignment("local execution has ended")
        elif self._provider_claim_released(saved, self._provider_release_evidence(saved)):
            return placement.ClosedAssignment("provider claim released")
        return saved.state

    def _provider_claim_released(self, saved: records.SavedPlacementAssignment,
                                 evidence: placement.HostEvidence | placement.EvidenceNeeded | NoSubmittedPlacementRequest) -> bool:
        """Confirm claim release under the same rules for status, replay, and settlement."""
        assignment = saved.assignment
        if (not isinstance(saved.state, placement.CurrentAssignment)
                or self.current_assignments.get(assignment.episode) != execution_identity.attempt_key(assignment.episode, assignment.attempt)
                or assignment.episode not in self.placement_requests):
            return False
        if self._claim_holder_ended(saved):
            return True
        if (not isinstance(evidence, placement.FreshHostEvidence)
                or any(claim.reference == saved.provider for claim in (*evidence.claims, *evidence.cleanup_claims))):
            return False
        report = saved.terminal_report
        if isinstance(report, placement.TerminalAttemptReport):
            return (isinstance(report.path, placement.DispatchPathSettled)
                and isinstance(report.outcome, placement.AttemptDeclined)
                and evidence.observed_at >= report.received_at)
        return ((self._ended_claim_holder(saved) or self._unconfirmed_holder_owner_ended(saved))
            and isinstance(saved.permission, placement.PermissionConsumed)
            and evidence.observed_at > saved.permission.consumed_at
            and not self._start_pending(saved))

    def _claim_holder_ended(self, saved: records.SavedPlacementAssignment) -> bool:
        """A provider claim whose holder and submitting owner ended frees its assignment.

        The Mac can keep the claim after its holder ends. The next claim for the scope takes it
        over and checks for leftovers, so the old assignment holds nothing. A live owner can still
        send a terminal report, and a pending start can still launch, so both keep it current.
        """
        return (self._ended_claim_holder(saved)
            and not self._placement_owner_live(saved.assignment.owner)
            and not self._start_pending(saved))

    def _ended_claim_holder(self, saved: records.SavedPlacementAssignment) -> bool:
        return (saved.assignment.destination.provider != "local"
                and isinstance(saved.permission, placement.PermissionConsumed)
                and isinstance(saved.provider, placement.ProviderClaimReference)
                and isinstance(saved.holder, placement.ClaimHolderIdentity)
                and saved.holder.host == self.host
                and not self._live(wire.Owner(saved.holder.pid, saved.holder.start_time)))

    def _unconfirmed_holder_owner_ended(self, saved: records.SavedPlacementAssignment) -> bool:
        """A claim with no confirmed holder frees its assignment after its submitting owner ends.

        No holder process can be checked, so only fresh evidence without the claim releases it.
        Without this rule, the assignment stays current with no process behind it.
        """
        return (saved.assignment.destination.provider != "local"
                and isinstance(saved.provider, placement.ProviderClaimReference)
                and isinstance(saved.holder, placement.ClaimHolderUnconfirmed)
                and not self._placement_owner_live(saved.assignment.owner))

    def _start_pending(self, saved: records.SavedPlacementAssignment) -> bool:
        from scripts.mac_test import claim_state, offload

        provider = saved.provider
        from scripts.aws_target import hana_placement
        if saved.assignment.destination.provider == 'ssh':
            try:
                destination = saved.assignment.destination
                submitted = self.placement_requests.get(saved.assignment.episode)
                settings = (placement.ConfiguredProviderSettings() if submitted is None
                            else self._placement_settings(submitted, destination))
                with conf.report_key_changes():
                    configuration = _provider_configuration()
                    target = configuration.target(destination.target, settings)
                    if not isinstance(target, target_config.SshTarget):
                        return True
                    transport = configuration.transport(target)
                if isinstance(transport, HanaGpuNextestTransport):
                    from scripts.aws_target.hana_placement_adapter import gpu_claim_pending

                    return gpu_claim_pending(destination.target,
                        execution_identity.assigned_claim_id(saved.assignment.episode, saved.assignment.attempt))
                if isinstance(transport, hana_placement.HanaControlTransport):
                    if submitted is None or not isinstance(self.last_usable_configuration, records.MemoryConfig):
                        return True
                    reference = placement.assigned_claim_reference(saved.assignment)

                    def read_dispatch() -> placement.DispatchPathState | wire.ExecutionReport:
                        from scripts.aws_target.hana_execution import HanaSettled, claim_dispatch_pending
                        from scripts.aws_target.hana_placement_adapter import recover_assigned_execution

                        controller = hana_placement.controller_for(transport, read_only=True)
                        report = saved.terminal_report
                        if (isinstance(saved.permission, placement.PermissionConsumed)
                                and isinstance(saved.holder, placement.ClaimHolderIdentity)
                                and (isinstance(report, placement.NoTerminalReport)
                                     or isinstance(report.path, placement.DispatchPathPending))):
                            if saved.provider != reference or destination.host != transport.host:
                                return placement.DispatchPathPending('The Hana claim differs from the assignment')
                            recovered = recover_assigned_execution(saved.assignment, saved.holder, controller)
                            if isinstance(recovered, HanaSettled):
                                outcome = report.outcome if isinstance(report, placement.TerminalAttemptReport) else recovered.outcome
                                evidence = (execution_identity.ExecutionNotStarted()
                                    if isinstance(outcome, (placement.PrelaunchRefused, execution_identity.HanaCommandAdmissionRefused))
                                    else recovered.evidence)
                                return wire.ExecutionReport('hana-recovery:' + reference.claim_id, saved.assignment,
                                    reference, evidence, outcome, placement.DispatchPathSettled())
                            return placement.DispatchPathPending(recovered.reason)
                        if (controller.execution_bound(reference.claim_id)
                                and claim_dispatch_pending(controller.directory, reference)):
                            return placement.DispatchPathPending('The Hana dispatch remains pending')
                        return placement.DispatchPathSettled()

                    key = self.host_evidence_key(submitted, destination)
                    self.host_requests[key] = submitted
                    self.placement_host_keys[(submitted.episode, destination)] = key
                    reader = self._host_evidence_reader(key, self.last_usable_configuration.waiting_pass_seconds)
                    return reader.worker.dispatch_pending(DispatchEvidenceProbe(key, saved, read_dispatch))
            except (OSError, ValueError):
                return True
        if not isinstance(provider, placement.ProviderClaimReference):
            return False
        identity = claim_state.ClaimIdentity(provider.claim_id, provider.episode, provider.attempt)
        with conf.report_key_changes():
            try:
                return offload.pending_start_path(saved.assignment.destination.target, identity).exists()
            except conf.Unusable:
                return True

    def _recovered_hana_dispatch(self, saved: records.SavedPlacementAssignment) -> wire.ExecutionReport | placement.NoTerminalReport:
        if self._ended_claim_holder(saved):
            _ = self._start_pending(saved)
            assignment = saved.assignment
            key = self.placement_host_keys.get((assignment.episode, assignment.destination))
            if key is not None and (reader := self.host_readers.get(key)) is not None:
                return reader.worker.recovered_dispatch(saved)
        return placement.NoTerminalReport()

    def _claim_holders(self) -> set[wire.Owner]:
        """The local holders of current provider claims, which the service watches."""
        return {wire.Owner(saved.holder.pid, saved.holder.start_time)
                for key in self.current_assignments.values()
                if (saved := self.assignments.get(key)) is not None
                and isinstance(saved.state, placement.CurrentAssignment)
                and isinstance(saved.holder, placement.ClaimHolderIdentity) and saved.holder.host == self.host}

    def _placement_status(self, assignment: placement.PlacementAssignment, reason: str = "", *,
                          check_lifetime: bool = True) -> wire.DispatchStatus:
        saved = self._saved_assignment(assignment.episode, assignment.attempt)
        if isinstance(saved, status_wire.UnknownPlacementStatus):
            return wire.DispatchStatus(placement.UnknownAssignment(assignment.episode, assignment.attempt),
                placement.ClosedAssignment("unknown attempt"), placement.NoLaunchPermission("unknown attempt"), reason)
        state = self._assignment_state(saved) if check_lifetime else saved.state
        return wire.DispatchStatus(saved.assignment, state, saved.permission, reason, saved.retry)

    def _placement_waiting(self, request: wire.PlacementRequest) -> bool:
        if not self._placement_owner_live(request.owner) or request.episode in self.current_assignments:
            return False
        return not any(saved.assignment.episode == request.episode
            and isinstance(saved.state, placement.ClosedAssignment)
            and saved.state.reason in ("completed", "cancelled") for saved in self.assignments.values())

    def _placement_admission(self, request: wire.PlacementRequest) -> wire.AdmitRequest | wire.LaunchRequest:
        identity = request.identity
        owner = wire.Owner(request.owner.pid, request.owner.start_time)
        local = request.local
        if isinstance(local, wire.LocalAdmissionUnavailable):
            return wire.LaunchRequest(request.episode, owner, request.since, identity, request.profile)
        return wire.AdmitRequest(request.episode, owner, local.argv, local.cwd, request.since, local.sidecar,
            identity, local.call, local.test_width, request.profile, local.fresh_test, local.token_back_at, request.run)

    def _placement_row(self, request: wire.PlacementRequest) -> records.Waiter:
        admission = self._placement_admission(request)
        place = self.places.get(_place_key(admission.owner, admission.since), records.NoWaiter())
        row = self._row(admission, place)
        if isinstance(request.run, wire.RunIdentity):
            run_place = self.run_places.get(records.run_owner(request.run.pid, request.run.start_time))
            if run_place is not None:
                row["order_since"] = min(run_place.since, row.get("order_since", row["since"]))
            row["build_run"] = _live_run(request.run)
        row["route"] = request.route
        row["placement_episode"] = request.episode
        prerequisite = self.prerequisites.get(request.episode, placement.TokenRequired())
        if isinstance(prerequisite, placement.TokenRequired):
            row["token_left_at"] = row.get("token_left_at", request.since)
            _ = row.pop("token_back_at", None)
        else:
            _ = row.pop("token_left_at", None)
            _ = row.pop("token_back_at", None)
        return row

    def _target_retry(self, request: wire.PlacementRequest,
                      destination: placement.PlacementDestination, keys: PlacementEvidenceKeys
                      ) -> placement.RetryDecision | placement.EvidenceNeeded:
        previous = [saved for saved in self.assignments.values()
            if saved.assignment.episode == request.episode and saved.assignment.destination == destination
            and isinstance(saved.state, placement.ClosedAssignment)]
        if not previous:
            return placement.RetryAt(0)
        saved = previous[-1]
        retry = saved.retry
        if (isinstance(retry, placement.DoNotRetry) and retry.reason in placement.SETTINGS_REFUSAL_REASONS
                and saved.state == placement.ClosedAssignment(retry.reason)):
            generation = (saved.refusal_settings if isinstance(saved.refusal_settings, records.RefusalSettings)
                          else saved.assigned_settings_generation)
            key = self._placement_evidence_key(request, destination, keys)
            if isinstance(key, placement.EvidenceNeeded):
                return key
            if (isinstance(generation, placement.UnknownAssignedSettingsGeneration)
                    or generation.digest != key.configuration_generation):
                return placement.RetryAt(0)
        return retry

    def _candidate_eligibility(self, request: wire.PlacementRequest, keys: PlacementEvidenceKeys
                               ) -> tuple[tuple[placement.CandidateEligibility, ...], placement.ResourceHolder]:
        result: list[placement.CandidateEligibility] = []
        local_holder: placement.ResourceHolder = placement.ResourceHolderUnknown()
        for candidate in request.constraints.candidates:
            destination = candidate.destination
            permitted: placement.HostEligibility = candidate.eligibility
            selection = request.constraints.selection
            retry = self._target_retry(request, destination, keys)
            if isinstance(selection, placement.PinnedHost) and selection.target != destination.target:
                permitted = placement.FixedExclusion("another target is pinned")
            elif isinstance(selection, placement.MacOnlyHost) and destination.target != "mac":
                permitted = placement.FixedExclusion("only the Mac is selected")
            elif isinstance(retry, placement.EvidenceNeeded):
                permitted = retry
            elif isinstance(retry, placement.DoNotRetry):
                permitted = placement.FixedExclusion(retry.reason)
            elif retry.deadline > self.clock.now():
                permitted = placement.TemporaryExclusion("retry deadline has not passed")
            if isinstance(permitted, placement.AllowedHost):
                evidence = self._request_host_evidence(request, destination, keys)
                if destination.provider == "local" and isinstance(evidence, placement.FreshHostEvidence):
                    local_holder = evidence.holder
                permitted = (evidence.eligibility if isinstance(evidence, placement.FreshHostEvidence)
                             else placement.EvidenceNeeded(evidence.reason))
                if isinstance(evidence, placement.FreshHostEvidence) and isinstance(permitted, placement.AllowedHost):
                    from scripts.mac_test.mac_test import scopes_overlap

                    retained = tuple((attempt.provider, report.received_at) for attempt in self.assignments.values()
                        for report in (attempt.terminal_report,)
                        if isinstance(attempt.state, placement.ClosedAssignment)
                        and isinstance(report, placement.TerminalAttemptReport)
                        and isinstance(report.outcome, (placement.PrelaunchRefused, execution_identity.HanaCommandAdmissionRefused))
                        and isinstance(attempt.provider, placement.ProviderClaimReference)
                        and attempt.provider.provider == destination.provider and attempt.provider.target == destination.target
                        and scopes_overlap(attempt.provider.scope, destination.scope))
                    if any(scopes_overlap(claim.reference.scope, destination.scope)
                            and (isinstance(claim, placement.RetainedCleanupClaim)
                                 or any(claim.reference == item[0] for item in retained))
                            for claim in (*evidence.cleanup_claims, *evidence.claims)):
                        permitted = placement.TemporaryExclusion("busy")
                    elif evidence.observed_at < max((item[1] for item in retained), default=float("-inf")):
                        permitted = placement.EvidenceNeeded("provider evidence predates the retained claim report")
            result.append(placement.CandidateEligibility(destination, permitted))
        return tuple(result), local_holder

    def _local_placement_eligibility(self, request: wire.PlacementRequest) -> placement.HostEligibility:
        if isinstance(request.local, wire.LocalAdmissionContext) and isinstance(request.purpose, placement.NormalStep):
            return next((candidate.eligibility for candidate in self.placement_eligibility.get(request.episode, ())
                         if candidate.destination.provider == "local"),
                        placement.FixedExclusion("no local candidate"))
        return placement.FixedExclusion("local admission is unavailable")

    def _can_hold_local_place(self, request: wire.PlacementRequest) -> bool:
        selection, route = request.constraints.selection, request.route
        return (isinstance(request.local, wire.LocalAdmissionContext)
            and isinstance(request.purpose, placement.NormalStep)
            and not isinstance(selection, placement.MacOnlyHost)
            and not isinstance(route, target_config.MacOnly)
            and any(candidate.destination.provider == "local"
                    and isinstance(candidate.eligibility, placement.AllowedHost)
                    and (not isinstance(selection, placement.PinnedHost) or selection.target == candidate.destination.target)
                    and (not isinstance(route, target_config.PinnedTarget) or route.target == candidate.destination.target)
                    for candidate in request.constraints.candidates))

    def _release_unclaimed_remote_slots(self, request: wire.PlacementRequest) -> None:
        """Release the run's unclaimed slots when remote placement begins without a hold."""
        run = request.run
        if not isinstance(run, wire.RunIdentity):
            return
        for key, slot in list(self.slots.items()):
            if isinstance(slot, records.BetweenStepsSlot) and (slot.run.pid, slot.run.start_time) == (run.pid, run.start_time):
                del self.slots[key]

    def _end_remote_start_hold(self, request: wire.PlacementRequest,
                               saved: records.SavedPlacementAssignment) -> records.SavedPlacementAssignment:
        hold = saved.remote_start_hold
        if (isinstance(hold, records.RemoteStartHold)
                or (isinstance(hold, records.RemoteStartHoldUnrecorded)
                    and saved.assignment.destination.provider != "local" and self._can_hold_local_place(request))):
            return replace(saved, remote_start_hold=records.RemoteStartHoldEnded())
        return saved

    def _update_remote_start_hold(self, request: wire.PlacementRequest, key: str,
                                  saved: records.SavedPlacementAssignment, remote_start_hold_seconds: int) -> bool:
        if (saved.assignment.destination.provider == "local"
                or not isinstance(saved.state, placement.CurrentAssignment)
                or isinstance(saved.remote_start_hold, records.RemoteStartHoldEnded)):
            return False
        if not self._can_hold_local_place(request):
            self.assignments[key] = self._end_remote_start_hold(request, saved)
            return False
        if isinstance(saved.permission, placement.PermissionConsumed):
            evidence = self.execution_evidence.get(key)
            host_key = self.placement_host_keys.get((request.episode, saved.assignment.destination))
            host_evidence = self.host_evidence(host_key) if host_key is not None else None
            if (isinstance(evidence, execution_identity.ObservedExecution)
                    or isinstance(host_evidence, placement.FreshHostEvidence)
                    and any(reference == saved.provider and isinstance(execution, execution_identity.ObservedExecution)
                            for reference, execution in host_evidence.executions)):
                self.assignments[key] = self._end_remote_start_hold(request, saved)
                return False
        hold = saved.remote_start_hold
        now = self.clock.now()
        if isinstance(hold, records.RemoteStartHoldUnrecorded):
            hold = records.RemoteStartHold(now + remote_start_hold_seconds)
            self.assignments[key] = replace(saved, remote_start_hold=hold)
        if now < hold.deadline:
            return True
        self.assignments[key] = self._end_remote_start_hold(request, saved)
        return False

    def _prune_host_evidence(self) -> None:
        active = {episode for episode, request in self.placement_requests.items()
                  if episode in self.current_assignments or self._placement_waiting(request)}
        self.placement_host_keys = {pair: key for pair, key in self.placement_host_keys.items()
                                    if pair[0] in active
                                    and (pair[1].provider != "local" or pair[0] in self.waiters
                                         or pair[0] not in self.current_assignments)}
        needed = set(self.placement_host_keys.values())
        for key in set(self.host_readers) - needed:
            reader = self.host_readers.pop(key)
            reader.worker.remove(key)
            _ = self.host_observations.pop(key, None)
        for key in self.host_requests.keys() - self.host_readers.keys():
            if key not in needed or not isinstance(self.host_observations.get(key), placement.FreshHostEvidence):
                _ = self.host_requests.pop(key, None)

    def _prepare_placements(self, keys: PlacementEvidenceKeys, remote_start_hold_seconds: int) -> None:
        """Refresh provider evidence and prepare waiting placement rows on the loop thread."""
        self._refresh_host_observations()
        # Keep the current holder label without retaining local process evidence.
        self.local_hold_holders.clear()
        self.active_placement_waits.clear()
        for saved in self.assignments.values():
            if saved.assignment.destination.provider == "local":
                continue
            submitted = self.placement_requests.get(saved.assignment.episode)
            if submitted is not None and (submitted.episode in self.current_assignments
                    or self._placement_waiting(submitted)):
                _ = self._request_host_evidence(submitted, saved.assignment.destination, keys)
        prepared: set[str] = set()
        for request in tuple(self.placement_requests.values()):
            if self.placement_requests.get(request.episode) is not request:
                continue
            key = self.current_assignments.get(request.episode, "")
            saved = self.assignments.get(key)
            if not self._placement_owner_live(request.owner):
                if saved is not None:
                    if isinstance(saved.permission, placement.PermissionAvailable) and not self._start_pending(saved):
                        self._close_placement(saved, "cancelled", placement.DoNotRetry("coordinator ended"))
                    else:
                        self.assignments[key] = self._end_remote_start_hold(request, saved)
                continue
            pending_remote = saved is not None and self._update_remote_start_hold(
                request, key, saved, remote_start_hold_seconds)
            if not pending_remote and not self._placement_waiting(request):
                continue
            self.active_placement_waits.add(request.episode)
            candidates, local_holder = self._candidate_eligibility(request, keys)
            self.placement_eligibility[request.episode] = candidates
            self.local_hold_holders[request.episode] = local_holder
            admission = self._placement_admission(request)
            row = self._placement_row(request)
            if isinstance(admission, wire.AdmitRequest):
                prepared.add(request.episode)
                previous = self.waiters.get(request.episode)
                claim = previous.continuation_claim if previous is not None else NoContinuationClaim()
                if previous is not None and isinstance(previous.connection, Connection):
                    self._remove_waiter(previous, claim_disposition=WaiterClaimDisposition.TRANSFER)
                waiting = WaitingAdmission(admission, row, NO_CONNECTION, continuation_claim=claim)
                self.waiters[request.episode] = waiting
                self._save_place(waiting)
                self._run_place(admission, waiter_place(row, request.episode))
        for episode, waiting in tuple(self.waiters.items()):
            if episode in self.placement_requests and episode not in prepared:
                self._remove_waiter(waiting)
        self._prune_host_evidence()

    def _new_assignment(self, request: wire.PlacementRequest,
                        destination: placement.PlacementDestination, remote_start_hold_seconds: int
                        ) -> placement.PlacementAssignment:
        assignment = placement.PlacementAssignment(request.episode, uuid.uuid4().hex, destination,
            request.constraints.source_key, request.owner, request.constraints.session, request.since)
        key = execution_identity.attempt_key(request.episode, assignment.attempt)
        evidence_key = self.placement_host_keys[(request.episode, destination)]
        hold = (records.RemoteStartHold(self.clock.now() + remote_start_hold_seconds)
                if destination.provider != "local" and self._can_hold_local_place(request)
                else records.RemoteStartHoldUnrecorded())
        self.assignments[key] = records.SavedPlacementAssignment(assignment,
            assigned_settings_generation=placement.AssignedSettingsGeneration(evidence_key.configuration_generation),
            remote_start_hold=hold)
        self.current_assignments[request.episode] = key
        return assignment

    def _placement_reply(self, request: wire.PlacementRequest, local_holder: placement.ResourceHolder
                         ) -> wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus:
        diagnostics = tuple(evidence.provider_diagnostic
            for candidate in self.placement_eligibility.get(request.episode, ())
            if candidate.destination.provider != "local" and isinstance(candidate.eligibility, placement.TemporaryExclusion)
            and candidate.eligibility.reason == "busy"
            if (key := self.placement_host_keys.get((request.episode, candidate.destination))) is not None
            for evidence in (self.host_evidence(key),)
            if isinstance(evidence, placement.FreshHostEvidence)
            and isinstance(evidence.provider_diagnostic, placement.BusyProviderDiagnostic))
        disk_exclusions = tuple(dict.fromkeys(candidate.destination.target
            for candidate in self.placement_eligibility.get(request.episode, ())
            if candidate.destination.provider == "mac"
            and isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion))
            and candidate.eligibility.reason == "disk"))
        delayed: dict[str, placement.ProviderRetryNotice] = {}
        for candidate in self.placement_eligibility.get(request.episode, ()):
            if (candidate.destination.provider == 'ssh' and isinstance(candidate.eligibility, placement.TemporaryExclusion)
                    and candidate.eligibility.reason in ('starting', 'blocked', 'recovery')):
                from scripts.aws_target import hana_placement
                with conf.report_key_changes():
                    try:
                        settings = self._placement_settings(request, candidate.destination)
                        configuration = _provider_configuration()
                        target = configuration.target(candidate.destination.target, settings)
                        if not isinstance(target, target_config.SshTarget):
                            continue
                        transport = configuration.transport(target)
                    except conf.Unusable:
                        continue
                if isinstance(transport, hana_placement.HanaControlTransport):
                    interval = (self.last_usable_configuration.waiting_pass_seconds
                        if isinstance(self.last_usable_configuration, records.MemoryConfig) else 1.0)
                    delayed[target.name] = placement.ProviderRetryNotice(target.name, candidate.eligibility.reason, interval)
        for attempt in self.assignments.values():
            report = attempt.terminal_report
            if (attempt.assignment.episode == request.episode and isinstance(attempt.state, placement.ClosedAssignment)
                    and isinstance(attempt.retry, placement.RetryAt) and attempt.retry.deadline > self.clock.now()
                    and isinstance(report, placement.TerminalAttemptReport)
                    and attempt.retry.deadline > report.received_at):
                target = attempt.assignment.destination.target
                delayed[target] = placement.ProviderRetryNotice(target, attempt.state.reason,
                    attempt.retry.deadline - report.received_at)
        retry_notices = tuple(delayed.values())
        stale_hana = (any(isinstance(candidate.eligibility, placement.FixedExclusion)
                          and candidate.eligibility.reason == placement.HANA_IMAGE_STALE
                          for candidate in self.placement_eligibility.get(request.episode, ()))
                      and not any(attempt.assignment.episode == request.episode
                                  and attempt.retry == placement.DoNotRetry(placement.HANA_IMAGE_STALE)
                                  for attempt in self.assignments.values()))
        saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
        if saved is not None:
            state = self._assignment_state(saved)
            if isinstance(state, placement.ClosedAssignment) and saved.assignment.destination.provider == "local":
                return self._placement_status(saved.assignment, state.reason)
            if isinstance(state, placement.ClosedAssignment):
                # A released provider claim is final only after a pass settles the attempt and its retry.
                self.next_pass = min(self.next_pass, self.clock.monotonic())
                return wire.PlacementWait(request.episode, request.since, 1,
                    self.placement_eligibility.get(request.episode, ()),
                    provider_diagnostics=diagnostics, mac_disk_exclusions=disk_exclusions,
                    retry_notices=retry_notices)
            if saved.assignment.destination.provider == "local":
                replay = self.ledger[request.episode]
                assert isinstance(replay.reply, wire.Admitted)
                return wire.LocalPlacementGranted(saved.assignment, replay.reply, diagnostics, disk_exclusions, stale_hana)
            if isinstance(saved.permission, placement.PermissionConsumed):
                return wire.PlacementWait(request.episode, request.since, 1,
                    self.placement_eligibility.get(request.episode, ()),
                    provider_diagnostics=diagnostics, mac_disk_exclusions=disk_exclusions,
                    retry_notices=retry_notices)
            return wire.Assignment(saved.assignment, diagnostics, disk_exclusions, stale_hana)
        terminal = [saved for saved in self.assignments.values() if saved.assignment.episode == request.episode
                    and isinstance(saved.state, placement.ClosedAssignment) and saved.state.reason in ("completed", "cancelled")]
        if terminal:
            return self._placement_status(terminal[-1].assignment)
        candidates = self.placement_eligibility.get(request.episode, ())
        if (refusal := self._placement_refusal(request, candidates)) is not None:
            return wire.DispatchStatus(placement.UnknownAssignment(request.episode, ""),
                placement.ClosedAssignment(refusal), placement.NoLaunchPermission(refusal),
                refusal, placement.DoNotRetry(refusal))
        ordered, _priority = self._queue_order()
        owner = records.waiter_owner(self._placement_row(request))
        position = next((index for index, (_, row) in enumerate(ordered, 1)
                         if records.waiter_owner(row) == owner), len(ordered) + 1)
        waiting = self.waiters.get(request.episode)
        if waiting is not None and isinstance(waiting.last_reply, wire.Wait):
            reason = waiting.last_reply.reason
            cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
            if isinstance(cause, placement.CargoFolderWait | records.BuildSpaceHold | records.EstimateExpired):
                return wire.PlacementWait(request.episode, request.since, position, candidates,
                    waiting.last_reply, diagnostics, disk_exclusions, retry_notices)
        if any(isinstance(candidate.eligibility, placement.EvidenceNeeded) for candidate in candidates):
            return wire.PlacementWait(request.episode, request.since, position, candidates, provider_diagnostics=diagnostics, mac_disk_exclusions=disk_exclusions, retry_notices=retry_notices)
        blocked = [candidate for candidate in candidates
            if isinstance(candidate.eligibility, placement.TemporaryExclusion)
            and candidate.eligibility.reason in ("blocked", "busy", "starting")]
        blocked.sort(key=lambda candidate: candidate.destination.provider != "local")
        local_possible = any(candidate.destination.provider == "local"
            and isinstance(candidate.eligibility, (placement.AllowedHost, placement.EvidenceNeeded))
            for candidate in candidates)
        if blocked and (blocked[0].destination.provider == "local" or not local_possible):
            candidate = blocked[0]
            if candidate.destination.provider == "local":
                holder = local_holder
            else:
                key = self.placement_host_keys.get((request.episode, candidate.destination))
                evidence = self.host_evidence(key) if key is not None else None
                holder = (evidence.holder if isinstance(evidence, placement.FreshHostEvidence)
                          else placement.ResourceHolderUnknown())
            assert isinstance(candidate.eligibility, placement.TemporaryExclusion)
            return wire.PlacementWait(request.episode, request.since, position, candidates,
                wire.PlacementResourceWait(candidate.destination.target, candidate.eligibility.reason, holder), diagnostics, disk_exclusions, retry_notices)
        if waiting is not None and isinstance(waiting.last_reply, wire.Wait):
            return wire.PlacementWait(request.episode, request.since, position,
                self.placement_eligibility.get(request.episode, ()), waiting.last_reply, diagnostics, disk_exclusions, retry_notices)
        return wire.PlacementWait(request.episode, request.since, position,
            self.placement_eligibility.get(request.episode, ()), provider_diagnostics=diagnostics, mac_disk_exclusions=disk_exclusions, retry_notices=retry_notices)

    def _occupied_local_cargo_folders(self) -> frozenset[placement.CargoFolder]:
        """Keep each local folder occupied until its reservation ends."""
        # Automatic cleanup waits for owner exit and an empty or absent scope.
        # ReleaseRequest and TokenWaitRequest skip the scope check. The caller must stop Cargo first.
        # Verify drains the scope before a normal release.
        return frozenset(record.cargo_folder for record in self.reservations.values()
                         if isinstance(record.place, wire.LocalPlace)
                         and isinstance(record.cargo_folder, placement.CargoFolder))

    def _placement_pass(self, inputs: records.QueueInputs, policy: records.AdmissionPolicy,
                        remote_start_hold_seconds: int) -> records.QueueInputs:
        """Keep pending remote starts in the local order until their holds end."""
        self.placement_resource_waits.clear()
        if not any(self._placement_waiting(request) for request in self.placement_requests.values()):
            self.placement_holds = {}
            self.placement_hold_answers = {}
            return inputs
        owners = {records.waiter_owner(self._placement_row(request)): request
                  for request in self.placement_requests.values() if self._placement_waiting(request)}
        local_occupied = inputs.occupied_cargo_folders
        preview_ordered: list[tuple[Path, records.Waiter]] = []
        for path, row in inputs.ordered:
            request = owners.get(records.waiter_owner(row))
            prerequisite = (self.prerequisites.get(request.episode) if request is not None
                            else placement.TokenNotRequired())
            if (isinstance(prerequisite, placement.TokenRequired)
                    and isinstance(prerequisite.readiness, placement.TokenReadinessUnchecked)):
                row = row.copy()
                if "token_left_at" in row:
                    del row["token_left_at"]
                if "token_back_at" in row:
                    del row["token_back_at"]
            preview_ordered.append((path, row))
        preview = gate.admission_pass(replace(inputs, ordered=preview_ordered), inputs.now, policy)
        local_starts = {row.owner for row in preview.rows if isinstance(row.admission, gate.ProposedAdmission)}
        ordered, _priority = self._queue_order()
        copies = tuple(request for request in owners.values() if isinstance(request.purpose, placement.SpeculativeCopy))
        held_copy_destinations = [saved.assignment.destination
            for episode, key in self.current_assignments.items()
            if (saved := self.assignments.get(key)) is not None
            and isinstance(saved.state, placement.CurrentAssignment)
            and (submitted := self.placement_requests.get(episode)) is not None
            and isinstance(submitted.purpose, placement.SpeculativeCopy)]
        removed: set[str] = set()
        pending_remote_starts = set(inputs.pending_remote_starts)
        cargo_folders = dict(inputs.cargo_folders)
        held: list[PlacementHold] = []
        taken: list[placement.PlacementDestination] = []
        holds: dict[tuple[str, placement.PlacementDestination], float] = {}
        answers: dict[tuple[str, placement.PlacementDestination], float] = {}
        for _, row in ordered:
            owner = records.waiter_owner(row)
            request = owners.get(owner)
            if request is None:
                continue
            prerequisite = self.prerequisites.get(request.episode)
            initial_readiness_unchecked = (isinstance(prerequisite, placement.TokenRequired)
                and isinstance(prerequisite.readiness, placement.TokenReadinessUnchecked))
            waiting_for: list[PlacementHold] = []
            for candidate in self.placement_eligibility.get(request.episode, ()):
                destination = candidate.destination
                if destination.provider == "local" and request.constraints.cargo_folder in local_occupied:
                    continue
                if (initial_readiness_unchecked and destination.provider == "local"
                        and isinstance(inputs.build_space, records.NoBuildSpaceHold)
                        and isinstance(candidate.eligibility, placement.EvidenceNeeded)):
                    break
                if not isinstance(candidate.eligibility, placement.AllowedHost):
                    hold = self._placement_hold(request, candidate, answers)
                    if hold is not None:
                        waiting_for.append(hold)
                    continue
                if isinstance(request.purpose, placement.SpeculativeCopy):
                    if candidate.destination in held_copy_destinations:
                        continue
                    competing = [copy for copy in copies if any(
                        choice.destination == candidate.destination and isinstance(choice.eligibility, placement.AllowedHost)
                        for choice in self.placement_eligibility.get(copy.episode, ()))]
                    newest = max(competing, key=lambda copy: placement.copy_order(
                        cast(placement.SpeculativeCopy, copy.purpose), copy.episode))
                    if newest.episode != request.episode:
                        continue
                if destination.provider == "local":
                    if owner in local_starts:
                        break
                    continue
                if destination.provider == "scaler":
                    continue
                if (blocking_start := next((other for other in taken if same_remote_resource(other, destination)), None)) is not None:
                    self._record_placement_block(request, blocking_start, "busy")
                    continue
                if self._held_for_older(request, destination, held, holds):
                    continue
                assignment = self._new_assignment(request, destination, remote_start_hold_seconds)
                taken.append(destination)
                if isinstance(request.purpose, placement.SpeculativeCopy):
                    held_copy_destinations.append(candidate.destination)
                saved = self.assignments[execution_identity.attempt_key(assignment.episode, assignment.attempt)]
                if isinstance(saved.remote_start_hold, records.RemoteStartHold):
                    if self._local_placement_eligibility(request) != placement.TemporaryExclusion("blocked"):
                        pending_remote_starts.add(owner)
                        folder = request.constraints.cargo_folder
                        if isinstance(folder, placement.CargoFolder):
                            cargo_folders[owner] = folder
                else:
                    self._remove_waiter(self.waiters.get(request.episode))
                    if isinstance(request.purpose, placement.NormalStep):
                        self._release_unclaimed_remote_slots(request)
                    removed.add(owner)
                break
            else:
                for hold in waiting_for:
                    held.append(hold)
                    start = self.placement_holds.get((hold.episode, hold.destination))
                    # A hold lasts until the next probe of the older request; it never starts again on the same reading.
                    if start is not None and hold.observed_at < start:
                        holds[(hold.episode, hold.destination)] = start
        self.placement_holds = holds
        self.placement_hold_answers = answers
        local_order = (inputs.waiter_orders.keys() | pending_remote_starts) - removed
        return replace(inputs, ordered=[(path, row) for path, row in ordered
            if records.waiter_owner(row) in local_order], slots=tuple(self.slots.values()), cargo_folders=cargo_folders,
            pending_remote_starts=frozenset(pending_remote_starts))

    def _record_placement_block(self, request: wire.PlacementRequest,
                                destination: placement.PlacementDestination, reason: str) -> None:
        if destination.provider != "mac":
            _ = self.placement_resource_waits.setdefault(request.episode,
                records.PlacementBlock(destination.provider, destination.target, reason, request.episode))

    def _placement_hold(self, request: wire.PlacementRequest, candidate: placement.CandidateEligibility,
                        answers: dict[tuple[str, placement.PlacementDestination], float]) -> PlacementHold | None:
        """Return the hold that keeps a remote destination for a waiting request that can use it now.

        The request can use it when its last reading allowed it or said busy, or when it has no reading.
        A reading that answered an earlier hold starts no new hold: the request cannot use the destination now.
        """
        destination = candidate.destination
        eligibility = candidate.eligibility
        key = self.placement_host_keys.get((request.episode, destination))
        if (key is None or not isinstance(request.purpose, placement.NormalStep)
                or destination.provider in ("local", "scaler")
                or not (isinstance(eligibility, placement.EvidenceNeeded)
                        or isinstance(eligibility, placement.TemporaryExclusion) and eligibility.reason == "busy")):
            return None
        reader = self.host_readers.get(key)
        if reader is None:
            return None
        latest = reader.latest
        if (isinstance(latest, placement.FreshHostEvidence) and not isinstance(latest.eligibility, placement.AllowedHost)
                and not (isinstance(latest.eligibility, placement.TemporaryExclusion) and latest.eligibility.reason == "busy")):
            return None
        pair = (request.episode, destination)
        observed_at = self.host_evidence(key).observed_at
        start = self.placement_holds.get(pair)
        answer = observed_at if start is not None and observed_at >= start else self.placement_hold_answers.get(pair)
        if answer is not None:
            answers[pair] = answer
        if answer == observed_at:
            return None
        return PlacementHold(request.episode, destination, key, observed_at)

    def _held_for_older(self, request: wire.PlacementRequest, destination: placement.PlacementDestination,
                        held: list[PlacementHold], holds: dict[tuple[str, placement.PlacementDestination], float]) -> bool:
        """Keep a destination for an older request whose last reading predates this request's reading.

        The older request's key is probed next. The hold ends when that probe answers, or after one probe time.
        """
        key = self.placement_host_keys.get((request.episode, destination))
        observed_at = self.host_evidence(key).observed_at if key is not None else self.clock.now()
        now = self.clock.now()
        for hold in held:
            if not same_remote_resource(hold.destination, destination) or hold.observed_at >= observed_at:
                continue
            reader = self.host_readers.get(hold.key)
            if reader is None:
                continue
            start = holds.get((hold.episode, hold.destination))
            if start is None:
                start = now
                holds[(hold.episode, hold.destination)] = start
                reader.worker.expedite(hold.key)
            if now - start <= self._waiting_pass_interval() + reader.timeout:
                if (older := self.placement_requests.get(hold.episode)) is not None:
                    self._record_placement_block(request, hold.destination, placement_order_reason(older))
                return True
        return False

    def _consume_permission(self, request: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
        assignment = request.assignment
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        saved = self.assignments.get(key)
        if saved is None:
            return self._placement_status(assignment, "unknown attempt")
        if saved.assignment != assignment:
            return self._placement_status(assignment, "the request differs from the saved assignment")
        if isinstance(saved.state, placement.ClosedAssignment):
            return self._placement_status(assignment, f"the attempt is closed: {saved.state.reason}")
        if self.current_assignments.get(assignment.episode) != key:
            return self._placement_status(assignment, "the attempt is no longer current")
        if isinstance(saved.permission, placement.PermissionConsumed):
            return self._placement_status(assignment, "launch permission was already consumed")
        if not isinstance(saved.terminal_report, placement.NoTerminalReport):
            return self._placement_status(assignment, 'the attempt has a terminal report')
        submitted = self.placement_requests.get(assignment.episode)
        if (submitted is None or submitted.constraints.source_key != assignment.source_key
                or submitted.owner != assignment.owner or not self._placement_owner_live(assignment.owner)):
            return self._placement_status(assignment, "assignment owner or source is no longer valid")
        reference = request.claim
        destination = assignment.destination
        if (not reference.claim_id or reference.episode != assignment.episode or reference.attempt != assignment.attempt
                or reference.provider != destination.provider or reference.target != destination.target
                or reference.scope != destination.scope):
            return self._placement_status(assignment, "claim does not match the assignment")
        if not isinstance(request.holder, placement.ClaimHolderIdentity) or not self._holder_matches(assignment.owner, request.holder):
            return self._placement_status(assignment, "claim holder is not a live coordinator descendant")
        from scripts.aws_target import hana_placement
        from scripts.mac_test.claim_state import RepositoryScope
        if isinstance(destination.scope, RepositoryScope) and destination.scope.slot != destination.slot and destination.provider == "ssh":
            return self._placement_status(assignment, "assigned scope and slot disagree")
        transport: ExecutionTransport | None = None
        if destination.provider == "ssh":
            with conf.report_key_changes():
                try:
                    configuration = _provider_configuration()
                    target = configuration.target(destination.target, self._placement_settings(submitted, destination))
                    if isinstance(target, target_config.SshTarget):
                        transport = configuration.transport(target)
                        if isinstance(transport, hana_placement.HanaControlTransport):
                            memory = self._hana_memory_eligibility(submitted, configuration.read(transport.settings))
                            if isinstance(memory, placement.TemporaryExclusion):
                                return self._placement_status(assignment, f'Hana admission refused: {memory.reason}')
                except (OSError, ValueError) as error:
                    return self._placement_status(assignment, f"adoption evidence is unavailable: {error}")
        if isinstance(transport, hana_placement.HanaControlTransport):
            from scripts.aws_target.hana_placement_adapter import read_assigned_adoption

            adopted = read_assigned_adoption(assignment, request.holder, transport)
            if isinstance(adopted, hana_placement.HanaClaimRefused):
                return self._placement_status(assignment, adopted.reason)
            if adopted.reference != reference:
                return self._placement_status(assignment, "the exact claim is not adopted")
        else:
            evidence = self._request_host_evidence(submitted, destination)
            if not isinstance(evidence, placement.FreshHostEvidence):
                if destination.target == 'hana-gpu':
                    return self._placement_status(assignment, 'adoption evidence is unavailable or stale')
                state = "stale" if isinstance(evidence, placement.StaleHostEvidence) else "unavailable"
                return self._placement_status(assignment, f"adoption evidence is {state}: {evidence.reason}")
            if destination.target == 'hana-gpu' and not isinstance(evidence.eligibility, placement.AllowedHost):
                return self._placement_status(assignment, 'The Hana GPU step is no longer eligible')
            if not any(claim.reference == reference and claim.holder == request.holder
                       and claim.session == assignment.session and claim.host == destination.host
                       and claim.generation == evidence.generation for claim in evidence.claims):
                return self._placement_status(assignment, "the exact claim is not adopted")
        saved = replace(saved, permission=placement.PermissionConsumed(self.clock.now()), provider=reference, holder=request.holder)
        self.assignments[key] = saved
        self._watch_owner(wire.Owner(request.holder.pid, request.holder.start_time))
        status = wire.DispatchStatus(assignment, saved.state, saved.permission)
        self.ledger["dispatch:" + key] = ReplayEntry(status, LiveGrant())
        self.persist()
        return wire.FirstAuthorization(assignment, reference)

    def _holder_matches(self, owner: placement.SubmittingOwner, holder: placement.ClaimHolderIdentity) -> bool:
        if holder.host != self.host or not self._live(wire.Owner(holder.pid, holder.start_time)):
            return False
        if holder.pid == owner.pid and holder.start_time == owner.start_time:
            return True
        processes = (dict(self.system_reader.latest.processes) if not isinstance(self.system_reader, NoSystemReader)
                     else self.proc.processes())
        return holder.pid in gate.process_tree({owner.pid: owner.start_time}, processes)

    def _close_placement(self, saved: records.SavedPlacementAssignment, reason: str,
                         retry: placement.RetryDecision) -> None:
        assignment = saved.assignment
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        self._remove_waiter(self.waiters.get(assignment.episode))
        if self.current_assignments.get(assignment.episode) == key:
            request = self.placement_requests.get(assignment.episode)
            if request is not None:
                saved = self._end_remote_start_hold(request, saved)
            del self.current_assignments[assignment.episode]
        self.assignments[key] = replace(saved, state=placement.ClosedAssignment(reason), retry=retry)
        self._prune_host_evidence()
        self.persist()
        self.next_pass = self.clock.monotonic()

    def _settle_hana_prelaunch(self, saved: records.SavedPlacementAssignment,
                               report: placement.TerminalAttemptReport) -> bool:
        """Close a Hana attempt when its journal confirms release before launch."""
        assignment = saved.assignment
        if (assignment.destination.provider != "ssh"
                or not isinstance(report.outcome, (placement.PrelaunchRefused, placement.AttemptCancelled,
                                                     execution_identity.HanaCommandAdmissionRefused))
                or not isinstance(saved.permission, placement.PermissionAvailable)):
            return False
        from scripts.aws_target import hana_placement
        from scripts.aws_target.hana_execution import claim_dispatch_pending, read_claim_execution

        try:
            target = target_config.configured_ssh_target(assignment.destination.target)
            transport = _provider_configuration().transport(target)
            if not isinstance(transport, hana_placement.HanaControlTransport):
                return False
            directory = hana_placement.controller_for(transport, read_only=True).directory
        except (OSError, ValueError):
            return False
        reference = placement.assigned_claim_reference(assignment)
        # The atomic journal proves settlement without a controller lock or a provider probe.
        if (claim_dispatch_pending(directory, reference)
                or not isinstance(read_claim_execution(directory, reference), execution_identity.ExecutionNotStarted)):
            return False
        settled = replace(report, path=placement.DispatchPathSettled())
        reason = report.outcome.reason
        state_reason = "cancelled" if isinstance(report.outcome, placement.AttemptCancelled) else reason
        self._close_placement(replace(saved, terminal_report=settled), state_reason, placement.DoNotRetry(reason))
        return True

    def _settle_attempt_reports(self, keys: PlacementEvidenceKeys) -> None:
        for key, saved in tuple(self.assignments.items()):
            assignment = saved.assignment
            if self.current_assignments.get(assignment.episode) != key:
                continue
            report = saved.terminal_report
            if (isinstance(report, placement.TerminalAttemptReport)
                    and isinstance(report.path, placement.DispatchPathPending)
                    and self._settle_hana_prelaunch(saved, report)):
                continue
            if (isinstance(report, placement.TerminalAttemptReport)
                    and isinstance(report.path, placement.DispatchPathPending)
                    and isinstance(report.outcome, (placement.PrelaunchRefused, placement.AttemptCancelled))
                    and (isinstance(saved.provider, placement.NoProviderClaim)
                        or isinstance(report.outcome, placement.PrelaunchRefused)
                        and (saved.provider.episode, saved.provider.attempt) != (assignment.episode, assignment.attempt))
                    and isinstance(saved.permission, placement.PermissionAvailable) and not self._start_pending(saved)):
                report = replace(report, path=placement.DispatchPathSettled())
                saved = replace(saved, terminal_report=report)
            if (isinstance(report, placement.TerminalAttemptReport) and isinstance(report.path, placement.DispatchPathSettled)
                    and not isinstance(report.outcome, placement.AttemptDeclined)):
                self._close_for_report(saved, report)
                continue
            if assignment.destination.provider == "local":
                # A coordinator that ends before its execution report leaves the local assignment current.
                state = self._assignment_state(saved)
                if isinstance(state, placement.ClosedAssignment) and not self._placement_owner_live(assignment.owner):
                    self._close_placement(saved, state.reason, placement.DoNotRetry("coordinator ended"))
                continue
            submitted = self.placement_requests.get(assignment.episode)
            if submitted is None:
                continue
            if isinstance(recovered := self._recovered_hana_dispatch(saved), wire.ExecutionReport):
                _ = self._handle_placement(recovered)
                if self.current_assignments.get(assignment.episode) == key:
                    # The worker already confirmed release for the exact settled decline.
                    updated = self.assignments[key]
                    terminal = updated.terminal_report
                    if (isinstance(terminal, placement.TerminalAttemptReport)
                            and isinstance(terminal.path, placement.DispatchPathSettled)
                            and terminal.outcome == recovered.outcome):
                        self._close_for_report(updated, terminal)
                continue
            if not self._claim_holder_ended(saved):
                _ = self._request_host_evidence(submitted, assignment.destination, keys)
                if not self._provider_claim_released(saved, self._provider_release_evidence(saved, keys)):
                    continue
            if isinstance(report, placement.NoTerminalReport):
                report = placement.TerminalAttemptReport(placement.AttemptDeclined("lost"),
                    placement.DispatchPathSettled(), self.clock.now())
                saved = replace(saved, terminal_report=report)
            self._close_for_report(saved, report)

    def _close_for_report(self, saved: records.SavedPlacementAssignment, report: placement.TerminalAttemptReport) -> None:
        outcome = report.outcome
        if isinstance(outcome, placement.AttemptCompleted):
            self._close_placement(saved, "completed", placement.DoNotRetry("command result is final"))
        elif isinstance(outcome, placement.AttemptCancelled):
            self._close_placement(saved, "cancelled", placement.DoNotRetry(outcome.reason))
        elif isinstance(outcome, execution_identity.HanaCommandAdmissionRefused):
            self._close_placement(saved, outcome.reason,
                placement.retry_decision(saved.assignment.destination, outcome, report.received_at))
        elif not self._settle_hana_prelaunch(saved, report):
            self._close_placement(saved, outcome.reason,
                saved.retry if isinstance(saved.retry, placement.RetryAt) else
                placement.retry_decision(saved.assignment.destination, outcome.reason, report.received_at))

    def _handle_placement(self, request: wire.PlacementRequest | wire.ConsumePermission | wire.ClaimFailure | wire.ExecutionReport
                          | wire.PrerequisiteReport | wire.CancelPlacement,
                          connection: Connection | NoConnection = NO_CONNECTION) -> list[wire.Reply]:
        if isinstance(request, wire.ConsumePermission):
            return [self._consume_permission(request)]
        if isinstance(request, (wire.PrerequisiteReport, wire.CancelPlacement)):
            submitted = self.placement_requests.get(request.episode)
            if submitted is None:
                return [wire.Refused(wire.PLACEMENT_REQUEST_MISSING_REASON)]
            if submitted.owner != request.owner:
                return [wire.Refused("placement coordinator does not match")]
            if isinstance(request, wire.PrerequisiteReport):
                if not self._placement_owner_live(request.owner):
                    return [wire.Refused("placement coordinator ended")]
                if self.prerequisites.get(request.episode) == request.prerequisite:
                    return [wire.Ok()]
                self.prerequisites[request.episode] = request.prerequisite
            else:
                saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
                if saved is not None and (isinstance(saved.permission, placement.PermissionAvailable)
                        or saved.assignment.destination.provider == "local"):
                    if saved.assignment.destination.provider == "local" and request.episode in self.reservations:
                        reservation = self.reservations[request.episode]
                        with retry_cleanup(Path(reservation.path)):
                            self._end(request.episode, OwnerExited())
                    report = saved.terminal_report
                    if (self._start_pending(saved) or (isinstance(report, placement.TerminalAttemptReport)
                            and isinstance(report.path, placement.DispatchPathPending))):
                        key = execution_identity.attempt_key(saved.assignment.episode, saved.assignment.attempt)
                        self.assignments[key] = replace(saved, terminal_report=placement.TerminalAttemptReport(
                            placement.AttemptCancelled(request.reason), placement.DispatchPathPending('provider settlement remains pending'),
                            self.clock.now()))
                    else:
                        self._close_placement(saved, "cancelled", placement.DoNotRetry(request.reason))
                elif saved is None:
                    _ = self.placement_requests.pop(request.episode, None)
                    self._remove_waiter(self.waiters.get(request.episode),
                                        claim_disposition=WaiterClaimDisposition.RETURN)
                self._prune_host_evidence()
            self.persist()
            self.next_pass = self.clock.monotonic()
            return [wire.Ok()]
        if isinstance(request, wire.PlacementRequest):
            if not self._placement_owner_live(request.owner):
                if request.owner.host == self.host and not isinstance(self.system_reader, NoSystemReader):
                    pending = self.pending_coordinators.get(request.owner)
                    if pending is None:
                        self.pending_coordinators[request.owner] = PendingCoordinatorEvidence(self.clock.monotonic())
                        self.system_reader.request_read()
                        return [wire.PlacementWait(request.episode, request.since, 0, ())]
                    if self.system_reader.latest.started_monotonic < pending.requested_at:
                        return [wire.PlacementWait(request.episode, request.since, 0, ())]
                if request.owner in self.pending_coordinators:
                    del self.pending_coordinators[request.owner]
                return [wire.Refused("submitting owner is not live on the coordinator")]
            if request.owner in self.pending_coordinators:
                del self.pending_coordinators[request.owner]
            previous = self.placement_requests.get(request.episode)
            if previous is not None and (previous.owner != request.owner or previous.since != request.since
                    or previous.run != request.run or previous.purpose != request.purpose):
                return [wire.Refused("placement episode changed its owner, start, run or purpose")]
            if previous is not None:
                request = replace(request, constraints=episode_constraints(previous.constraints, request.constraints))
            elif (isinstance(folder := request.constraints.cargo_folder, placement.CargoFolder)
                    and any(candidate.destination.provider == "local" for candidate in request.constraints.candidates)):
                try:
                    canonical = placement.CargoFolder(folder.path.resolve())
                except (OSError, ValueError) as error:
                    return [wire.Refused(f"cannot resolve the Cargo folder: {error}")]
                request = replace(request, constraints=replace(request.constraints, cargo_folder=canonical))
            if (isinstance(connection, Connection) and previous == request
                    and not isinstance(self.system_reader, NoSystemReader)
                    and self.system_reader.latest.started_monotonic <= self.last_pass_started
                    and isinstance(self.pending_configuration, NoPass) and not self.pass_stale
                    and isinstance(self.last_result, gate.PassResult)
                    and self.clock.monotonic() < self.next_pass):
                return [self._placement_reply(request,
                    self.local_hold_holders.get(request.episode, placement.ResourceHolderUnknown()))]
            self.placement_requests[request.episode] = request
            if previous is None:
                self.prerequisites[request.episode] = request.prerequisite
            self._watch_owner(request.owner)
            self.persist()
            if isinstance(connection, Connection) and not self._pass_gap_open():
                self.parked_placements.append(ParkedPlacement(request, connection))
                self.next_pass = min(self.next_pass, self.clock.monotonic())
                return []
            _ = self.run_pass()
            return [self._placement_reply(request,
                self.local_hold_holders.get(request.episode, placement.ResourceHolderUnknown()))]
        assignment = request.assignment
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        saved = self.assignments.get(key)
        if saved is None or saved.assignment != assignment:
            return [self._placement_status(assignment, check_lifetime=False)]
        if isinstance(request, wire.ClaimFailure):
            if (self.current_assignments.get(assignment.episode) == key
                    and isinstance(saved.permission, placement.PermissionAvailable)
                    and isinstance(saved.state, placement.CurrentAssignment)):
                if isinstance(saved.terminal_report, placement.TerminalAttemptReport):
                    return [self._placement_status(assignment, 'the attempt already has a terminal report', check_lifetime=False)]
                if isinstance(request.claim, placement.ProviderClaimReference):
                    from scripts.mac_test.mac_test import scopes_overlap

                    destination = assignment.destination
                    if (request.claim.provider != destination.provider or request.claim.target != destination.target
                            or not scopes_overlap(request.claim.scope, destination.scope)):
                        return [self._placement_status(assignment, "retained claim does not match the assigned provider scope",
                            check_lifetime=False)]
                terminal = placement.TerminalAttemptReport(placement.PrelaunchRefused(request.reason),
                    placement.DispatchPathPending('provider settlement remains pending'), self.clock.now())
                retained = request.claim if isinstance(request.claim, placement.ProviderClaimReference) else saved.provider
                refused = replace(saved, terminal_report=terminal, provider=retained,
                                  refusal_settings=request.refusal_settings)
                if not self._settle_hana_prelaunch(refused, terminal):
                    retry = placement.retry_decision(assignment.destination, request.reason, terminal.received_at)
                    if self._start_pending(saved):
                        self.assignments[key] = replace(refused, retry=retry)
                        self.persist()
                        self.next_pass = self.clock.monotonic()
                    else:
                        terminal = replace(terminal, path=placement.DispatchPathSettled())
                        self._close_placement(replace(refused, terminal_report=terminal), request.reason, retry)
        elif (isinstance(saved.permission, placement.PermissionConsumed) and saved.provider == request.claim
                or isinstance(saved.permission, placement.PermissionAvailable)
                and (isinstance(request.outcome, (placement.PrelaunchRefused, execution_identity.HanaCommandAdmissionRefused))
                    or isinstance(request.outcome, placement.AttemptCancelled)
                    and isinstance(saved.terminal_report, placement.TerminalAttemptReport)
                    and saved.terminal_report.outcome == request.outcome)
                and isinstance(request.claim, placement.ProviderClaimReference)
                and request.claim.episode == assignment.episode and request.claim.attempt == assignment.attempt
                and request.claim.provider == assignment.destination.provider
                and request.claim.target == assignment.destination.target and request.claim.scope == assignment.destination.scope):
            self.execution_evidence[key] = request.evidence
            outcome = request.outcome
            if (isinstance(outcome, (placement.AttemptCompleted, placement.AttemptDeclined, placement.AttemptCancelled))
                    or isinstance(outcome, (placement.PrelaunchRefused, execution_identity.HanaCommandAdmissionRefused))
                    and isinstance(request.evidence, execution_identity.ExecutionNotStarted)):
                previous = saved.terminal_report
                if isinstance(previous, placement.NoTerminalReport):
                    terminal = placement.TerminalAttemptReport(outcome, request.path, self.clock.now())
                    retry = saved.retry
                    if isinstance(outcome, execution_identity.HanaCommandAdmissionRefused):
                        retry = placement.DoNotRetry(outcome.reason)
                    elif isinstance(outcome, placement.PrelaunchRefused) and not isinstance(retry, placement.RetryAt):
                        retry = placement.retry_decision(assignment.destination, outcome.reason, terminal.received_at)
                    self.assignments[key] = replace(saved, terminal_report=terminal, provider=request.claim, retry=retry)
                elif previous.outcome == outcome:
                    terminal = (replace(previous, path=request.path)
                        if isinstance(request.path, placement.DispatchPathSettled) else previous)
                    self.assignments[key] = replace(saved, provider=request.claim, terminal_report=terminal)
            saved = self.assignments[key]
            report = saved.terminal_report
            if (self.current_assignments.get(assignment.episode) == key
                    and isinstance(report, placement.TerminalAttemptReport)
                    and isinstance(report.path, placement.DispatchPathSettled)
                    and not isinstance(report.outcome, placement.AttemptDeclined)):
                self._close_for_report(saved, report)
            else:
                self.persist()
                self.next_pass = self.clock.monotonic()
        self._publish_replies()
        return [self._placement_status(assignment, check_lifetime=False)]

    def _authorize(self, request: wire.Request, uid: int) -> str:
        if isinstance(request, status_wire.PlacementStatusRequest):
            return ""
        if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ClaimFailure, wire.ExecutionReport,
                                wire.PrerequisiteReport, wire.CancelPlacement)):
            owner = request.owner if isinstance(request, (wire.PlacementRequest, wire.PrerequisiteReport, wire.CancelPlacement)) else request.assignment.owner
            episode = request.episode if isinstance(request, (wire.PlacementRequest, wire.PrerequisiteReport, wire.CancelPlacement)) else request.assignment.episode
            if uid != os.getuid() and (not episode.startswith(f"uid:{uid}:") or self._account(owner) != uid):
                return "placement belongs to another account"
            return ""
        if uid == os.getuid():
            return ""
        if isinstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest)):
            return "queue priority and head admission require the service account"
        if isinstance(request, (wire.BuildHistoryRequest, wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            return ""
        key = (request.episode if isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest, wire.RepositorySlotRequest))
               else request.request_id)
        if not key.startswith(f"uid:{uid}:"):
            return "request belongs to another account"
        if isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest, wire.RepositorySlotRequest)):
            if self._account(request.owner) != uid or not self._live(request.owner):
                return "owner belongs to another account or no longer exists"
            if isinstance(request, wire.AdmitRequest) and isinstance(request.run, wire.RunIdentity):
                if self._account(request.run) != uid or not self._run_alive(request.run):
                    return "run belongs to another account or no longer exists"
            return ""
        if key in self.ledger:
            return ""
        record = next((record for record in self.reservations.values()
                       if record.reservation_id == request.reservation), None)
        if record is None or not record.episode.startswith(f"uid:{uid}:"):
            return "reservation belongs to another account"
        return ""

    def _runner_reply(self, reply: wire.Reply) -> wire.Reply:
        if not isinstance(reply, wire.Admitted):
            return reply
        config = records.read_memory_config()
        return wire.RunnerAdmitted(reply.reservation, reply.need, reply.source, reply.max_path,
            reply.wave_path, reply.sampling_context, reply.cleanup_timing,
            execution=wire.RunnerExecutionTiming(config.program_sample_seconds,
                config.program_stop_seconds, config.peak_wave_seconds))

    def _service_estimate(self, request: wire.ServiceEstimateRequest,
                          configuration: gate.UsableAdmissionConfiguration) -> wire.AdmitRequest:
        config = configuration.config
        estimate = step_estimator.step_estimate(list(request.argv), request.cwd, records.INFER_FROM_ARGV,
            lambda: datetime.fromtimestamp(self.clock.now(), UTC), self.state_dir.parent, config.policy,
            first_peak_bytes=config.program_first_peaks["cargo"],
            share_percent=configuration.peak_history_share_percent,
            build_history_generation=lambda: records.BuildHistoryGeneration(self.build_history_generation),
            location=step_estimator.RepositoryLocation(request.repository, request.worktree))
        label = codec.ci_label(config.ci_label, request.runner, request.job)
        identity = wire.Identity("", label, estimate.profile.step, request.worktree)
        return ServiceEstimatedAdmission(request.episode, request.owner, request.argv, request.cwd, request.since,
            request.sidecar, identity, records.NoCallRecord(), request.test_width, estimate.profile,
            isinstance(estimate.cache, records.FreshTest), route=request.route, code_version=request.code_version,
            runner=request.runner)

    @staticmethod
    def _stored_caller(session: str, name: str, folder: str, unit: str, *,
                       members: records.QueueStaff) -> records.QueueCaller:
        caller = records.queue_caller(session, "", "", "", folder, members=members, unit=unit)
        return replace(caller, name=codec.resolve_queue_caller(stored_caller=name, staff_caller=caller.name))

    def _named_identity(self, identity: wire.Identity, *, members: records.QueueStaff, folder: str = "") -> wire.Identity:
        caller = self._stored_caller(identity.session_id, identity.name, identity.folder or folder, identity.unit,
                                     members=members)
        return replace(identity, name=caller.name, unit=caller.unit)

    @staticmethod
    def _placement_identity(request: wire.PlacementRequest) -> wire.Identity:
        identity = request.identity
        session = request.constraints.session
        return replace(identity, session_id=identity.session_id or (
            session.session_id if isinstance(session, OwningSession) else ""),
            step=identity.step or request.profile.step, folder=identity.folder or request.profile.worktree)

    def handle_request(self, request: wire.Request, connection: Connection | NoConnection = NO_CONNECTION
                       ) -> list[wire.Reply]:
        started, cpu_started = self.clock.monotonic(), time.thread_time()
        try:
            return self._handle_request(request, connection)
        finally:
            self._log_request_timing(request, started, cpu_started)

    def _log_request_timing(self, request: wire.Request, started: float, cpu_started: float) -> None:
        try:
            with conf.diagnostics(report=False) as messages:
                limit = conf.read("build_admission").number("request_slow_seconds")
        except conf.Unusable as error:
            records.warn_configuration_diagnostics((records.ConfigurationDiagnostic(str(error)),))
        else:
            records.warn_configuration_diagnostics(records.configuration_diagnostics(messages))
            wall, cpu = self.clock.monotonic() - started, time.thread_time() - cpu_started
            if wall > limit:
                print(f"admission request {type(request).__name__}: wall={wall:.3f}s cpu={cpu:.3f}s",
                      file=sys.stderr, flush=True)

    def _handle_request(self, request: wire.Request, connection: Connection | NoConnection
                        ) -> list[wire.Reply]:
        refusal = self._authorize(request, peer_uid(connection))
        if refusal:
            return [wire.Refused(refusal)]
        if isinstance(request, status_wire.PlacementStatusRequest):
            saved = self._saved_assignment(request.episode, request.attempt)
            status = (saved if isinstance(saved, status_wire.UnknownPlacementStatus) else
                status_wire.KnownPlacementStatus(saved.assignment, self._assignment_state(saved), saved.provider))
            return [status_wire.PlacementStatusReply(status)]
        if isinstance(request, wire.RepositorySlotRequest):
            return [wire.Refused("Repository copy capacity no longer requires admission")]
        if isinstance(connection, Connection):
            self._record_caller_version(request, connection, persist_change=not isinstance(request, wire.ConsumePermission))
        if isinstance(request, wire.PlacementRequest):
            identity = self._placement_identity(request)
            members = records.queue_staff() if identity.session_id else records.NO_STAFF_LOOKUP
            request = replace(request, identity=self._named_identity(identity, members=members))
        if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ClaimFailure, wire.ExecutionReport,
                                wire.PrerequisiteReport, wire.CancelPlacement)):
            replies = self._handle_placement(request, connection)
            if (isinstance(request, wire.ConsumePermission) and isinstance(connection, Connection)
                    and not any(isinstance(reply, wire.FirstAuthorization) for reply in replies)):
                self.persist()
            return replies
        if isinstance(request, wire.ServiceEstimateRequest):
            replay = self.ledger.get(request.episode)
            if replay is not None:
                return [self._runner_reply(replay.reply)]
            configuration = self._take_configuration()
            if isinstance(configuration, gate.UnusableAdmissionConfiguration):
                _ = self._run_pass(configuration)
                return [wire.Refused(configuration.reason)]
            request = self._service_estimate(request, configuration)
            self.pending_configuration = configuration
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) and request.code_version:
            self.caller_versions[records.run_owner(request.owner.pid, request.owner.start_time)] = request.code_version
        if isinstance(request, wire.BuildHistoryRequest):
            return [wire.BuildHistoryReply(self.build_history_generation)]
        if isinstance(request, (wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            if not isinstance(self.pending_configuration, NoPass):
                _ = self.run_pass()
            elif self.pass_stale and isinstance(self.last_inputs, records.QueueInputs):
                if isinstance(self.last_policy, records.AdmissionPolicy):
                    self.last_inputs = self._inputs_with_current_slots(self.last_inputs)
                    self.last_result = gate.admission_pass(self.last_inputs, self.last_inputs.now, self.last_policy)
                self.pass_stale = False
                self._publish_replies()
            if isinstance(self.configuration, gate.UnusableAdmissionConfiguration):
                return [wire.Refused(self.configuration.reason)]
            if (not isinstance(self.last_inputs, records.QueueInputs)
                    or not isinstance(self.last_policy, records.AdmissionPolicy)):
                raise RuntimeError("service has no pass")
            published = self.published
            if not isinstance(published, PublishedReplies):
                raise RuntimeError("service has no published pass")
            if isinstance(request, wire.ExplainRequest):
                return [published.explain]
            if isinstance(request, wire.ViewRequest):
                return [published.view]
            return [wire.MineReply(published.view.reading)]
        key = request.episode if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) else request.request_id
        replay = self.ledger.get(key)
        if replay is not None:
            return [replay.reply]
        if isinstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest)):
            return self._submit_queue_change(request, connection)
        configuration = (self.configuration if isinstance(request, (wire.ReleaseRequest, wire.TokenWaitRequest))
                         else self._take_configuration())
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            if not self._live(request.owner):
                return self._mutation_reply(key, wire.Refused("owner no longer exists"))
            if any(record.owner == request.owner for record in self.reservations.values()):
                return self._mutation_reply(key, wire.Refused("owner already has a reservation"))
            if any(waiting.request.owner == request.owner and episode != key
                   for episode, waiting in self.waiters.items()):
                return self._mutation_reply(key, wire.Refused("owner already has an admission episode"))
            if (isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
                    and isinstance(group := request.place.group, status_wire.CloudGroup)):
                if (refused := self._register_cloud_group(request.place, group)) is not None:
                    return self._mutation_reply(key, refused)
            if not isinstance(request, ServiceEstimatedAdmission):
                folder = request.cwd if isinstance(request, wire.AdmitRequest) else request.profile.worktree
                members = records.queue_staff() if request.identity.session_id else records.NO_STAFF_LOOKUP
                request = replace(request, identity=self._named_identity(request.identity, members=members, folder=folder))
            self._pending_replies[key] = []
            place_key = _place_key(request.owner, request.since)
            row = self._row(request, self.places.get(place_key, records.NoWaiter()))
            place = waiter_place(row, key)
            self._run_place(request, place)
            previous = self.waiters.get(key)
            claim = previous.continuation_claim if previous is not None else NoContinuationClaim()
            if previous is not None and previous.connection is not connection:
                self._remove_waiter(previous, claim_disposition=WaiterClaimDisposition.TRANSFER)
            self.waiters[key] = WaitingAdmission(request, row, connection, continuation_claim=claim)
            self._save_place(self.waiters[key])
            self._watch_owner(request.owner)
            if (isinstance(request, wire.AdmitRequest) and request.token_back_at
                    and not isinstance(place.token_left_at, TokenNotLeft)):
                gate.append_queue_time(self.state_dir, {"kind": "token_gap", "episode": key,
                    **gate.queue_life_identity(self.waiters[key].row),
                    "token_left_at": place.token_left_at, "token_back_at": request.token_back_at}, self.clock.now())
        elif isinstance(request, wire.ReleaseRequest):
            episode = next((episode for episode, record in self.reservations.items() if record.reservation_id == request.reservation), None)
            if episode is not None:
                self._end(episode, ReleaseStatus(key, request.status))
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        elif isinstance(request, wire.WidthRequest):
            if request.width <= 0:
                return self._mutation_reply(key, wire.Refused("test width must be positive"), request.reservation)
            for episode, record in list(self.reservations.items()):
                if record.reservation_id == request.reservation:
                    updated = replace(record, test_width=records.RequestedTestWidth(request.width),
                                      test_phase_need=gate.NoTestPhaseNeed("not_recorded"))
                    if (isinstance(record.place, wire.LocalPlace)
                            and isinstance(configuration, gate.UsableAdmissionConfiguration)):
                        updated = self._freeze_test_phase_need(updated, configuration.config,
                                                               configuration.peak_history_share_percent)
                    self.reservations[episode] = updated
            live = any(record.reservation_id == request.reservation for record in self.reservations.values())
            self.ledger[key] = ReplayEntry(wire.Ok(), LiveGrant() if live else self.clock.now(), request.reservation)
        else:
            for episode, record in list(self.reservations.items()):
                if record.reservation_id == request.reservation:
                    self._end(episode, TokenWaiting(key, self.clock.now()))
            self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        if isinstance(request, (wire.ReleaseRequest, wire.TokenWaitRequest)):
            self.persist()
            if (isinstance(self.last_inputs, records.QueueInputs)
                    and isinstance(self.last_policy, records.AdmissionPolicy)):
                self.last_inputs = self._inputs_with_current_slots(self.last_inputs)
                self.pass_stale = True
            self.next_pass = self.clock.monotonic()
            return [self.ledger[key].reply]
        if isinstance(connection, Connection):
            # The pass delivers admission replies on the connection, now or at the next pass.
            ran = self._pass_when_open(configuration)
        else:
            _ = self._run_pass(configuration)
            ran = True
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)):
            replies = self._pending_replies.pop(key, [])
            return replies if ran else []
        return [self.ledger[key].reply]

    def _submit_queue_change(self, request: QueueChange,
                             connection: Connection | NoConnection) -> list[wire.Reply]:
        connections = [connection] if isinstance(connection, Connection) else []
        if isinstance(self.pending_priority, PendingPriorityRoster):
            for change in (self.pending_priority.change, *self.queue_changes):
                if change.request.request_id == request.request_id:
                    for client in connections:
                        if all(client is not previous for previous in change.connections):
                            change.connections.append(client)
                    return []
            self.queue_changes.append(OrderedQueueChange(request, connections))
            return []
        if isinstance(request, wire.PrioritySetRequest) and request.unit.strip():
            result: Future[Mapping[str, gate.UnitMembership]] = Future()
            self.pending_priority = PendingPriorityRoster(OrderedQueueChange(request, connections), result)
            threading.Thread(target=read_priority_roster, args=(result, os.dup(self.priority_events)),
                             daemon=True, name="priority-roster").start()
            return []
        return self._apply_queue_change(request, {})

    def _finish_priority_roster(self) -> None:
        pending = self.pending_priority
        if not isinstance(pending, PendingPriorityRoster) or not pending.result.done():
            return
        self.pending_priority = NoPendingPriorityRoster()
        self._complete_queue_change(pending.change,
            lambda: self._apply_queue_change(pending.change.request, pending.result.result()))
        self._continue_queue_changes()

    def _continue_queue_changes(self) -> None:
        while self.queue_changes and isinstance(self.pending_priority, NoPendingPriorityRoster):
            change = self.queue_changes.popleft()
            self._complete_queue_change(change,
                lambda: self._submit_queue_change(change.request, NO_CONNECTION))
            if isinstance(self.pending_priority, PendingPriorityRoster):
                self.pending_priority.change.connections.extend(change.connections)

    def _complete_queue_change(self, change: OrderedQueueChange,
                               apply: Callable[[], list[wire.Reply]]) -> None:
        try:
            replies = apply()
        except (ValueError, TypeError) as error:
            for connection in change.connections:
                if connection.socket.fileno() >= 0:
                    self._refuse_connection(connection, str(error))
        else:
            self._reply_to_queue_change(change, replies)

    def _reply_to_queue_change(self, change: OrderedQueueChange, replies: list[wire.Reply]) -> None:
        for connection in change.connections:
            if connection.socket.fileno() >= 0:
                for reply in replies:
                    self._send(connection, reply)

    def _apply_queue_change(self, request: QueueChange,
                            labels: Mapping[str, gate.UnitMembership]) -> list[wire.Reply]:
        key = request.request_id
        configuration = self._take_configuration()
        if isinstance(request, wire.PrioritySetRequest):
            if not request.unit.strip():
                return self._mutation_reply(key, wire.Refused("unit is required"))
            entries = self.priority.entries if isinstance(self.priority, records.QueuePriority) else ()
            if any(entry["unit"] == request.unit for entry in entries):
                return self._mutation_reply(key, wire.Ok())
            rows = [waiting.row for waiting in self.waiters.values()]
            rows.extend(record.queue_waiter for record in self.reservations.values())
            recorded_names = [(row.get("session_id", ""), name) for row in rows if (name := self._priority_name(row))]
            recorded_sessions = {session for session, _ in recorded_names}
            known_names = {label.unit for session, label in labels.items() if session not in recorded_sessions}
            known_names.update(name for _, name in recorded_names)
            if request.unit == "—" or request.unit not in known_names:
                return self._mutation_reply(key, wire.Refused(
                    f"unknown unit {request.unit}; the queue priority did not change"))
            changed = True
            entry: records.PriorityEntry = {"unit": request.unit, "why": request.why,
                "set_at": self.clock.now(), "set_by": request.actor}
            entries = (*entries, entry)
            unit, why, actor = request.unit, request.why, request.actor
        else:
            entries = self.priority.entries if isinstance(self.priority, records.QueuePriority) else ()
            if request.unit and not any(entry["unit"] == request.unit for entry in entries):
                return self._mutation_reply(key, wire.Refused(
                    f"unknown unit {request.unit}; the queue priority did not change"))
            removed = tuple(entry for entry in entries if not request.unit or entry["unit"] == request.unit)
            changed = bool(removed)
            entries = tuple(entry for entry in entries if request.unit and entry["unit"] != request.unit)
            unit = request.unit or ", ".join(entry["unit"] for entry in removed)
            why, actor = ", ".join(entry["why"] for entry in removed), request.actor
        at = self.clock.now()
        month_row_end = store.read_last_whole_row_end(store.host_file(self.host, at))
        if not changed:
            return self._mutation_reply(key, wire.Ok())
        transition = records.PendingPriorityTransition(key,
            "set" if isinstance(request, wire.PrioritySetRequest) else "clear", at,
            month_row_end, unit, why, actor)
        self.priority = records.QueuePriority(entries, transition)
        self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        self.persist()
        self._finish_priority()
        _ = self._run_pass(configuration)
        return [self.ledger[key].reply]

    def _mutation_reply(self, key: str, reply: wire.Ok | wire.Refused, reservation: str = "") -> list[wire.Reply]:
        live = any(record.reservation_id == reservation for record in self.reservations.values())
        self.ledger[key] = ReplayEntry(reply, LiveGrant() if live else self.clock.now(), reservation)
        self.persist()
        return [reply]

    def _deliver(self, waiting: WaitingAdmission, reply: wire.Reply) -> None:
        if isinstance(waiting.request, ServiceEstimatedAdmission):
            reply = self._runner_reply(reply)
        episode = waiting.request.episode
        if episode in self._pending_replies:
            self._pending_replies[episode].append(reply)
        if isinstance(waiting.connection, Connection):
            self._send(waiting.connection, reply)

    def _remove_waiter(self, waiting: WaitingAdmission | None,
                       reply: wire.Reply | NoWaitReply = NO_WAIT_REPLY, *,
                       claim_disposition: WaiterClaimDisposition = WaiterClaimDisposition.RELEASE) -> None:
        if waiting is None:
            return
        episode = waiting.request.episode
        del self.waiters[episode]
        if claim_disposition is WaiterClaimDisposition.RETURN and episode not in self.current_assignments:
            self._free_claim(records.waiter_owner(waiting.row))
        elif claim_disposition is not WaiterClaimDisposition.TRANSFER:
            claim = waiting.continuation_claim
            if isinstance(claim, records.ContinuationSlot) and self.slots.get(claim.slot.reservation) is claim:
                del self.slots[claim.slot.reservation]
        if not isinstance(reply, NoWaitReply):
            self._deliver(waiting, reply)
        elif isinstance(waiting.connection, Connection):
            self._close_connection(waiting.connection)

    def _send(self, connection: Connection, reply: wire.Reply) -> None:
        if connection.finished:
            return
        connection.sending += self.reply_bytes(reply, version=connection.version)
        connection.finished = not isinstance(reply, (wire.Wait, wire.BuildHistoryReply))
        if connection.finished:
            self._drop_deferred_requests(connection)
        self.epoll.modify(connection.socket.fileno(), select.EPOLLOUT if connection.finished
                          else select.EPOLLIN | select.EPOLLOUT)

    def start(self) -> None:
        self.writes.require(self.socket_path)
        if not isinstance(self.listener, NoConnection):
            return
        inherited = int(os.environ.get("LISTEN_FDS") or "0")
        if inherited and int(os.environ.get("LISTEN_PID", str(os.getpid()))) == os.getpid():
            listener = socket.socket(fileno=LISTEN_FD)
            self.inherited_listener = True
        else:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            if self.socket_path.exists():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.setblocking(False)
                    try:
                        probe.connect(str(self.socket_path))
                    except OSError as error:
                        if error.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                            listener.close()
                            raise RuntimeError(f"cannot establish whether admission socket is live: {error}") from error
                    else:
                        listener.close()
                        raise RuntimeError(f"admission server already listening at {self.socket_path}")
                self.socket_path.unlink(missing_ok=True)
            previous_umask = os.umask(0o177)
            try:
                listener.bind(str(self.socket_path))
            except BaseException:
                listener.close()
                raise
            finally:
                _ = os.umask(previous_umask)
            listener.listen()
        listener.setblocking(False)
        self.listener = listener
        self.epoll.register(listener.fileno(), select.EPOLLIN)
        self.listener_started_at = self.clock.monotonic()
        if self.next_source_check == float("inf"):
            first_check = "after the first usable configuration"
        else:
            first_check = f"at monotonic {self.next_source_check:.3f}"
        print(f"memory admission: source watch checks {len(self.sources)} paths first {first_check}", file=sys.stderr)
        revision = loaded_git_revision(self.source_root)
        _notify("READY=1")
        if os.environ.pop(ADMISSION_SOURCE_RELOAD, ""):
            version = admission_code.reload_source_version(self.source_root)
            print(f"memory admission: reload ready, source hash {version or 'unavailable'}",
                  file=sys.stderr, flush=True)
        print(f"memory admission: ready with git revision {revision}", file=sys.stderr)

    def _drop_deferred_requests(self, connection: Connection) -> None:
        self.deferred_requests = [pending for pending in self.deferred_requests
                                  if pending.connection is not connection]
        self.parked_placements = [item for item in self.parked_placements if item.connection is not connection]

    def _disconnect(self, connection: Connection) -> None:
        detached = self._detach_admission(connection)
        self._close_connection(connection)
        if detached:
            _ = self._pass_when_open(self._take_configuration())

    def _close_connection(self, connection: Connection) -> None:
        self._drop_deferred_requests(connection)
        descriptor = connection.socket.fileno()
        if self.connections.get(descriptor) is not connection:
            return
        self.epoll.unregister(descriptor)
        del self.connections[descriptor]
        connection.socket.close()

    def _detach_admission(self, connection: Connection, reply: wire.Refused | NoWaitReply = NO_WAIT_REPLY) -> bool:
        """End this connection's eligibility before a later pass can grant it."""
        changed = False
        for episode, waiting in list(self.waiters.items()):
            if waiting.connection is connection:
                self._record_left(episode, gate.queue_life_identity(waiting.row), "cancelled")
                _ = self.places.pop(_place_key(waiting.request.owner, waiting.row["since"]), None)
                self._remove_waiter(waiting, reply, claim_disposition=WaiterClaimDisposition.RETURN)
                changed = True
        if changed:
            self.persist()
        return changed

    def _refuse_connection(self, connection: Connection, reason: str) -> None:
        self._drop_deferred_requests(connection)
        connection.received = b""
        connection.sending = b""
        reply = wire.Refused(reason)
        if not self._detach_admission(connection, reply):
            self._send(connection, reply)

    def _record_caller_version(self, request: wire.Request, connection: Connection,
                               *, persist_change: bool = True) -> None:
        if not request.code_version:
            return
        pid, uid, gid = struct.unpack("3i", connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        del uid, gid
        if isinstance(self.system_reader, NoSystemReader):
            identity = self.proc.owner_start(pid)
        else:
            process = self.system_reader.latest.processes.get(pid)
            identity = LiveOwnerIdentity(process.start) if process is not None else OwnerProcessGone()
        if isinstance(identity, LiveOwnerIdentity):
            self.caller_versions[records.run_owner(pid, identity.start_time)] = request.code_version
            if persist_change:
                self.persist()

    def _read_request(self, connection: Connection) -> None:
        config = (self.configuration if isinstance(self.pending_configuration, NoPass)
                  else self.pending_configuration)
        last = self.last_usable_configuration
        request_limit = last.request_max_bytes if isinstance(last, records.MemoryConfig) else wire.CLEANUP_REQUEST_MAX_BYTES
        limit = max(request_limit, wire.CLEANUP_REQUEST_MAX_BYTES)
        reason = (config.reason if isinstance(config, gate.UnusableAdmissionConfiguration) and isinstance(last, NoPass)
                  else f"admission request exceeds {limit} bytes")
        if len(connection.received) > limit:
            raise ValueError(reason)
        while not connection.finished:
            chunk = connection.socket.recv(min(65536, limit + 1 - len(connection.received)))
            if not chunk:
                self._disconnect(connection)
                return
            connection.received += chunk
            if len(connection.received) > limit:
                raise ValueError(reason)
            if b"\n" in connection.received:
                line, _, tail = connection.received.partition(b"\n")
                if tail:
                    raise ValueError("one request per connection")
                connection.version, request = wire.parse_request_envelope(line)
                cleanup = isinstance(request, (wire.ReleaseRequest, wire.TokenWaitRequest))
                if len(line) + 1 > (wire.CLEANUP_REQUEST_MAX_BYTES if cleanup else request_limit):
                    raise ValueError(reason)
                if not isinstance(request, wire.BuildHistoryRequest):
                    if not (isinstance(connection.request, NoRequest) or _refreshes(connection.request, request)):
                        raise ValueError("one request for admission per connection")
                    connection.request = request
                connection.received = b""
                if (not isinstance(self.system_reader, NoSystemReader)
                        and isinstance(request, (wire.AdmitRequest, wire.ServiceEstimateRequest, wire.LaunchRequest))
                        and ((process := self.system_reader.latest.processes.get(request.owner.pid)) is None
                             or process.start != request.owner.start_time)):
                    self.deferred_requests.append(DeferredOwnerRequest(request, connection, self.clock.monotonic()))
                    self.system_reader.request_read()
                    return
                replies = self.handle_request(request, connection)
                # Admission replies were delivered as part of the pass.
                for reply in replies:
                    if isinstance(request, wire.BuildHistoryRequest) or not connection.sending:
                        self._send(connection, reply)
                return
            if len(connection.received) > request_limit and wire.noncleanup_request_started(connection.received):
                raise ValueError(f"admission request exceeds {request_limit} bytes")

    def _connection_event(self, connection: Connection, events: int) -> None:
        try:
            if events & select.EPOLLIN and not connection.finished:
                self._read_request(connection)
                if connection.socket.fileno() < 0:
                    return
            # Send the reply before a later admission pass can delay it.
            if connection.sending and events & (select.EPOLLIN | select.EPOLLOUT):
                sent = connection.socket.send(connection.sending)
                connection.sending = connection.sending[sent:]
                if not connection.sending:
                    if connection.finished:
                        self._disconnect(connection)
                    else:
                        self.epoll.modify(connection.socket.fileno(), select.EPOLLIN)
            if events & (select.EPOLLHUP | select.EPOLLERR) and connection.socket.fileno() >= 0:
                self._disconnect(connection)
        except BlockingIOError:
            pass
        except (ValueError, TypeError) as error:
            self._refuse_connection(connection, str(error))
        except (BrokenPipeError, ConnectionResetError):
            self._disconnect(connection)

    def _source_paths(self) -> set[Path]:
        paths: set[Path] = set()
        for module in list(sys.modules.values()):
            filename = cast(object, getattr(module, "__file__", None))
            if isinstance(filename, str):
                path = Path(filename).resolve()
                if path.suffix == admission_code.RELOAD_SOURCE_SUFFIX and path.is_relative_to(self.source_root):
                    paths.add(path)
        return paths

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int] | SourceMissing:
        try:
            stat = path.stat()
            return stat.st_ino, stat.st_mtime_ns
        except FileNotFoundError:
            return SourceMissing()

    def _check_sources(self) -> None:
        configuration = self.last_usable_configuration
        if len(sys.modules) != self.source_module_count:
            # A function-level import loads its module after start. Watch it from the first check after it loads.
            self.source_module_count = len(sys.modules)
            for path in self._source_paths() - self.sources.keys():
                self.sources[path] = self._stamp(path)
        current = {path: self._stamp(path) for path in self.sources}
        now = self.clock.monotonic()
        if current != self.sources:
            changed_paths = sorted(path for path, stamp in current.items() if self.sources[path] != stamp)
            self.changed_sources.update(changed_paths)
            changed = ", ".join(map(str, changed_paths))
            print(f"memory admission: source watch detected changes at monotonic {now:.3f} in {changed}",
                  file=sys.stderr)
            self.sources = current
            self.source_change_at = now
        elif (isinstance(configuration, records.MemoryConfig)
              and not isinstance(self.source_change_at, SourcesUnchanged)
              and now - self.source_change_at >= configuration.source_settle_seconds):
            self.persist()
            self.replace_in_place = self.inherited_listener
            action = "reload" if self.replace_in_place else "stop"
            print(f"memory admission: source watch will {action} after the changes settled", file=sys.stderr)
            _notify(f"RELOADING=1\nMONOTONIC_USEC={time.monotonic_ns() // 1000}" if self.replace_in_place
                    else "STOPPING=1")
            self.stopped = True
        self.next_source_check = (now + configuration.source_check_seconds
                                  if isinstance(configuration, records.MemoryConfig) else float("inf"))

    def _arm_config_watch(self) -> None:
        old = self.config_watch
        for parent in self.config_path.parents:
            try:
                if not parent.is_dir():
                    continue
                identity = parent.stat()
                watch = inotify.add_watch(self.config_events, parent)
            except OSError:
                continue
            self.config_watch = watch
            self.config_parent = parent
            self.config_parent_identity = identity.st_dev, identity.st_ino
            self.config_event_name = self.config_path.relative_to(parent).parts[0]
            self.next_config_watch_retry = float("inf")
            if old >= 0 and old != watch:
                inotify.remove_watch(self.config_events, old)
            return
        if old >= 0:
            covers_path = False
            try:
                identity = self.config_parent.stat()
                covers_path = (identity.st_dev, identity.st_ino) == self.config_parent_identity
            except OSError:
                pass
            if not covers_path:
                inotify.remove_watch(self.config_events, old)
                self.config_watch = -1
        interval = (self.last_usable_configuration.source_check_seconds
                    if isinstance(self.last_usable_configuration, records.MemoryConfig)
                    else wire.PROTOCOL_CLEANUP_TIMING.reconnect_seconds)
        self.next_config_watch_retry = self.clock.monotonic() + interval
        self.next_pass = min(self.next_pass, self.next_config_watch_retry)

    def _configuration_changed(self) -> bool:
        changed = False
        for event in inotify.events(self.config_events):
            if event.mask & inotify.IN_Q_OVERFLOW:
                changed = True
            elif event.watch == self.config_watch:
                changed |= bool(event.mask & inotify.WATCH_REMOVED) or not event.name or event.name == self.config_event_name
                if event.mask & inotify.WATCH_REMOVED:
                    inotify.remove_watch(self.config_events, self.config_watch)
                    self.config_watch = -1
        if changed:
            self._arm_config_watch()
        return changed

    def _check_outage_alert(self) -> None:
        configuration = self.last_usable_configuration
        if (not self.outage_alert_needs_clear or not isinstance(configuration, records.MemoryConfig)
                or self.clock.monotonic() - self.listener_started_at < configuration.silence_seconds):
            return
        try:
            self.outage_alert_needs_clear = not outage_alerts.clear("build-admission", self.clock.now())
            if self.outage_alert_needs_clear:
                print("memory admission: cannot clear outage alert; will retry", file=sys.stderr)
        except (OSError, ValueError) as error:
            print(f"memory admission: cannot clear outage alert: {error}", file=sys.stderr)

    def run_once(self, timeout: float = 0) -> None:
        self.start()
        now = self.clock.monotonic()
        due = max(self.next_pass, self._pass_allowed_at())
        delay = min(timeout, max(0, min(due, self.next_source_check, self.next_config_watch_retry) - now))
        ready = self.epoll.poll(-1 if delay == float("inf") else delay)
        connections = {descriptor: ConnectionReadiness(connection, events) for descriptor, events in ready
                       if (connection := self.connections.get(descriptor)) is not None}
        watches = {descriptor: watch for descriptor, _events in ready
                   if (watch := self.watchers.get(descriptor)) is not None}
        if any(descriptor == self.priority_events for descriptor, _events in ready):
            _ = os.eventfd_read(self.priority_events)
        if any(descriptor == self.reading_events for descriptor, _events in ready):
            _ = os.eventfd_read(self.reading_events)
            self._refresh_host_observations()
            if not isinstance(self.system_reader, NoSystemReader):
                self.system_reader.raise_failure()
            self.next_pass = self.clock.monotonic()
            deferred, self.deferred_requests = self.deferred_requests, []
            for pending in deferred:
                connection = pending.connection
                if not connection.finished and self.connections.get(connection.socket.fileno()) is connection:
                    if (not isinstance(self.system_reader, NoSystemReader)
                            and self.system_reader.latest.started_monotonic < pending.arrived_monotonic):
                        self.deferred_requests.append(pending)
                        continue
                    for reply in self.handle_request(pending.request, connection):
                        if not connection.sending:
                            self._send(connection, reply)
        configuration_changed = (any(descriptor == self.config_events for descriptor, _events in ready)
                                 and self._configuration_changed())
        if self.clock.monotonic() >= self.next_config_watch_retry:
            self._arm_config_watch()
            configuration_changed = True
        if configuration_changed:
            self.pending_configuration = self._read_configuration()
            self.next_pass = self.clock.monotonic()
        # Drain ready requests before any expiry pass, including requests queued
        # on a connection accepted this turn. A silent peer cannot delay expiry.
        listener_ready = (not isinstance(self.listener, NoConnection)
                          and any(descriptor == self.listener.fileno() for descriptor, _events in ready))
        self.receiving_requests = True
        try:
            self._receive_requests(connections, listener_ready)
        finally:
            self.receiving_requests = False
        for descriptor, events in ready:
            connection_ready = connections.get(descriptor)
            watch = watches.get(descriptor)
            if connection_ready is not None and self.connections.get(descriptor) is connection_ready.connection:
                self._connection_event(connection_ready.connection, events & ~select.EPOLLIN)
            elif watch is not None and self.watchers.get(descriptor) is watch:
                if isinstance(watch, ScopeWatch):
                    try:
                        _ = os.lseek(descriptor, 0, os.SEEK_SET)
                        _ = os.read(descriptor, 4096)
                    except OSError as error:
                        if error.errno not in (errno.ENODEV, errno.ENOENT):
                            raise
                        self._unwatch(descriptor)
                else:
                    self._unwatch(descriptor)
                owner_exit = watch if isinstance(watch, OwnerWatch) else None
                self._prune_places(owner_exit)
                for waiting in tuple(self.waiters.values()):
                    owner = waiting.request.owner
                    if (owner_exit is not None and owner_exit.owner == owner) or not self._live(owner):
                        self._remove_waiter(waiting, claim_disposition=WaiterClaimDisposition.RETURN)
                self._prune_watches()
                self.next_pass = self.clock.monotonic()
        self._finish_priority_roster()
        if self.clock.monotonic() >= self.next_pass and self._pass_gap_open():
            _ = self.run_pass()
        if self.clock.monotonic() >= self.next_source_check:
            self._check_sources()
        self._check_outage_alert()
        _notify("WATCHDOG=1")

    def _receive_requests(self, ready: dict[int, ConnectionReadiness], listener_ready: bool) -> None:
        readable = [(descriptor, connection) for descriptor, connection in self.connections.items()
                    if (event := ready.get(descriptor)) is not None and event.connection is connection
                    and event.events & select.EPOLLIN]
        if listener_ready and not isinstance(self.listener, NoConnection):
            while True:
                try:
                    client = self.listener.accept()[0]
                except BlockingIOError:
                    break
                client.setblocking(False)
                descriptor = client.fileno()
                connection = Connection(client)
                self.connections[descriptor] = connection
                self.epoll.register(descriptor, select.EPOLLIN)
                readable.append((descriptor, connection))
        for descriptor, connection in readable:
            if self.connections.get(descriptor) is connection and not connection.finished:
                self._connection_event(connection, select.EPOLLIN)

    def start_reader(self) -> None:
        """Start the reader when a usable configuration exists."""
        self.reader_requested = True
        if isinstance(self.system_reader, NoSystemReader) and isinstance(self.last_usable_configuration, records.MemoryConfig):
            plan = self._reading_plan(self.last_usable_configuration)
            initial = self.system_evidence
            if not isinstance(initial, SystemEvidence):
                initial = read_system(plan, self.readings, self.proc, self.clock)
            self.system_reader = OperatingSystemReader(plan, initial, self.readings, self.proc, self.clock, self.reading_events)
            for descriptor, watch in list(self.watchers.items()):
                if isinstance(watch, ScopeWatch):
                    self._unwatch(descriptor)

    def serve_forever(self) -> None:
        self.start_reader()
        watchdog = int(os.environ.get("WATCHDOG_USEC", "0"))
        timeout = watchdog / 2_000_000 if watchdog > 0 else float("inf")
        while not self.stopped:
            self.run_once(timeout)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopped = True
        for worker in self.host_targets.values():
            worker.close()
        if not isinstance(self.system_reader, NoSystemReader):
            self.system_reader.close()
        os.close(self.reading_events)
        os.close(self.config_events)
        os.close(self.priority_events)
        self.deferred_requests.clear()
        self.parked_placements.clear()
        for connection in tuple(self.connections.values()):
            self._close_connection(connection)
        for descriptor in self.watchers:
            os.close(descriptor)
        self.watchers.clear()
        if not isinstance(self.listener, NoConnection):
            if self.replace_in_place:
                # The descriptor stays open for the replacement process.
                _ = self.listener.detach()
            else:
                self.listener.close()
                if not os.environ.get("LISTEN_FDS"):
                    self.socket_path.unlink(missing_ok=True)
        self.epoll.close()


# These scripts retain admission functions for the lifetime of their process.
ADMISSION_CALLERS = {
    "verify.sh": "direct/verify.sh",
    "invoke.sh": "lint/invoke.sh",
    "pre-tool-use-brp-launch-gate.sh": "hooks/pre-tool-use-brp-launch-gate.sh",
    "merge_gate.py": "production/merge_gate.py",
    "gate.py": "production/gate.py",
    "targeted_test.py": "production/targeted_test.py",
    "admission_client.py": "build_queue/admission_client.py",
    "queue_place.py": "build_queue/queue_place.py",
    "offload.py": "mac_test/offload.py",
    "copy_watch.py": "mac_test/copy_watch.py",
}

def read_state(directory: Path) -> ServiceState:
    return parse_service_state((directory / "state.json").read_text())


def parse_service_state(text: str, *, warn_missing_folders: bool = False) -> ServiceState:
    def check_placement_shape(value: object) -> None:
        if isinstance(value, list):
            for child in cast(list[object], value):
                check_placement_shape(child)
        elif isinstance(value, dict):
            row = cast(dict[str, object], value)
            required = ({"holder", "retry", "terminal_report"} if row.get("kind") == "SavedPlacementAssignment" else
                {"identity", "run", "local", "prerequisite", "purpose"} if row.get("kind") == "PlacementRequest" else set[str]())
            if not required.issubset(row):
                raise ValueError("stored placement records require external conversion")
            for child in row.values():
                check_placement_shape(child)

    raw_state = cast(dict[str, object], json.loads(text))
    check_placement_shape(raw_state)
    state = wire.parse_record(text, ServiceState)
    saved_reservations = cast(dict[str, dict[str, object]], raw_state["reservations"])
    legacy_episodes = {episode for episode, record in saved_reservations.items() if "cargo_folder" not in record}
    if not legacy_episodes:
        return state
    # A consumed local assignment proves that the service granted this owner a local start.
    local_grants = {(assignment.episode, assignment.owner.pid, assignment.owner.start_time)
                    for saved in state.assignments.values() for assignment in (saved.assignment,)
                    if assignment.destination.provider == "local"
                    and isinstance(saved.permission, placement.PermissionConsumed)}
    for episode in legacy_episodes:
        record = state.reservations[episode]
        if not isinstance(record.place, wire.LocalPlace):
            continue
        submitted = state.placement_requests.get(episode)
        grant = (episode, record.owner.pid, record.owner.start_time)
        if (submitted is not None and grant in local_grants
                and (submitted.owner.pid, submitted.owner.start_time) == (record.owner.pid, record.owner.start_time)):
            state.reservations[episode] = replace(record, cargo_folder=submitted.constraints.cargo_folder)
        elif warn_missing_folders:
            print(f"memory admission: cannot recover the Cargo folder for reservation {record.reservation_id}",
                  file=sys.stderr)
    return state


def state_check(directory: Path) -> int:
    """Read stored state as serve does, without changing it."""
    if not (directory / "state.json").exists():
        print("state-check: no state.json")
        return 0
    try:
        _ = read_state(directory)
    except ValueError as error:
        print(f"state-check: {error}", file=sys.stderr)
        return 2
    print("state-check: state.json reads")
    return 0


def _older_caller(pid: int, args: list[str], proc: Proc, scripts: dict[str, bytes],
                  versions: dict[str, str], current_version: str) -> bool:
    """Require matching entry content and a recorded version of the loaded admission code."""
    for argument in args:
        name = Path(argument).name
        if name not in scripts:
            continue
        candidate = Path(argument)
        if not candidate.is_absolute():
            candidate = proc.path / str(pid) / "cwd" / candidate
        try:
            if candidate.read_bytes() != scripts[name]:
                return True
        except OSError:
            return True
        identity = proc.owner_start(pid)
        if not isinstance(identity, LiveOwnerIdentity):
            return True
        version = versions.get(records.run_owner(pid, identity.start_time), "")
        if not version or not current_version or version != current_version:
            return True
    return False

def drain_check(directory: Path, *, service: bool, include_gate_callers: bool, proc: Proc) -> int:
    """Read admission owners and callers without changing admission state."""
    waiting: list[str] = []
    reserved: list[str] = []
    owners: list[wire.Owner] = []
    versions: dict[str, str] = {}
    if service:
        try:
            state = read_state(directory)
        except FileNotFoundError:
            state = ServiceState({}, {}, {}, records.ReadyToStart())
        versions = state.caller_versions
        rows = [(record.reservation_id, reservation_row(record)) for record in state.reservations.values()]
        owners = [place.owner for place in state.places.values()
                  if place.episode not in state.reservations]
    else:
        rows = [(path.name, row) for path, row in gate.reservation_rows(directory)]
        owners = []
        for path in directory.glob("*.waiting"):
            row = gate.read_waiter(path)
            owners.append(wire.Owner(row["pid"], row["start_time"]))
    for owner in owners:
        if proc.owner_start(owner.pid) == LiveOwnerIdentity(owner.start_time):
            waiting.append(records.run_owner(owner.pid, owner.start_time))
    for name, row in rows:
        live = proc.owner_start(row["pid"]) == LiveOwnerIdentity(row["start_time"])
        if not isinstance(proc.lifetime(row, live), gate.EndedStep):
            reserved.append(name)
    clients: list[str] = []
    names: set[str] = set(ADMISSION_CALLERS) if not service or include_gate_callers else set()
    if service:
        names.update(("admission_client.py", "queue_place.py"))
    scripts: dict[str, bytes] = {}
    root = Path(__file__).resolve().parents[1]
    for name in names:
        try:
            scripts[name] = (root / ADMISSION_CALLERS[name]).read_bytes()
        except OSError:
            scripts[name] = b""
    current_version = admission_code.admission_code_version(root)
    for pid in proc.processes():
        if pid == os.getpid():
            continue
        try:
            args = read_proc_text(proc.path / str(pid) / "cmdline").split("\0")
        except OSError:
            continue
        if _older_caller(pid, args, proc, scripts, versions, current_version):
            clients.append(str(pid))
    if not any((waiting, reserved, clients)):
        print("drained")
        return 0
    for label, members in (("waiters", waiting), ("reservations", reserved), ("clients", clients)):
        print(f"{label}: {len(members)}" + (": " + ", ".join(members[:5]) if members else ""))
    return 1


def export_priority(directory: Path) -> None:
    """Export the drained priority and retire the service state before rollback."""
    writes = ServiceStateWrites(directory)
    writes.require()
    if drain_check(directory, service=True, include_gate_callers=True, proc=SystemProc()):
        raise ValueError("service is not drained; priority was not exported")
    try:
        state = read_state(directory)
    except FileNotFoundError:
        return
    for record in state.reservations.values():
        writes.register(record)
    def persist() -> None:
        writes.require()
        write_text_atomic(directory / "state.json", wire.encode_record(state))
    def advance_generation() -> None:
        nonlocal state
        state = replace(state, build_history_generation=state.build_history_generation + 1)
        persist()
    for episode, record in list(state.reservations.items()):
        outcome, status, end_kind = finalization_outcome(record.ending)
        try:
            _ = gate.finalize_reservation(Path(record.path), reservation_row(record), outcome,
                datetime.now(UTC), status, intents=StateIntents(state.reservations,
                    advance_generation, episode, persist, _no_fault, state.places, writes),
                end_kind=end_kind)
        except Exception as error:
            raise ValueError(f"cannot finalize reservation {record.reservation_id}: {error}; service state kept") from error
    path = gate.queue_priority_path(directory)
    if isinstance(state.priority, records.NoQueuePriority):
        path.unlink(missing_ok=True)
    else:
        # The legacy reader uses the same domain codec without an envelope.
        write_text_atomic(path, wire.encode_record(state.priority))
    (directory / "state.json").unlink()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("serve", "drain-check", "export-priority", "state-check"))
    default_state_dir = store.root() / "admission"
    _ = parser.add_argument("--state-dir", type=Path, default=default_state_dir)
    _ = parser.add_argument("--service", action="store_true")
    class Arguments(argparse.Namespace):
        command: str = ""
        service: bool = False
        # argparse skips a default when the namespace already has the attribute.
        state_dir: Path = default_state_dir
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.command == "drain-check":
            return drain_check(args.state_dir, service=args.service,
                               include_gate_callers=args.state_dir == default_state_dir, proc=SystemProc())
        if args.command == "state-check":
            return state_check(args.state_dir)
        if args.command == "export-priority":
            export_priority(args.state_dir)
            return 0
        server = AdmissionServer(args.state_dir, SystemClock(), SystemReadings(), SystemProc())
        try:
            server.serve_forever()
        finally:
            server.close()
        if server.replace_in_place:
            # The new code takes the same socket without the systemd restart delay; a failed start restarts as before.
            os.set_inheritable(LISTEN_FD, True)
            os.environ[ADMISSION_SOURCE_RELOAD] = "1"
            changed = ", ".join(str(path) for path in sorted(server.changed_sources))
            print(f"memory admission: reload for changed source {changed}", file=sys.stderr, flush=True)
            os.execv(sys.executable, sys.orig_argv)
        return 0
    except override_guard.SharedStateWriteRefused as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
