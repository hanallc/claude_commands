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
import hashlib
import heapq
import json
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum, auto
from functools import cache
from importlib import import_module
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
from typing import TYPE_CHECKING, Callable, Literal, Protocol, cast, override

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
from scripts.build_queue import ci_job_start
from scripts.build_queue import placement
from scripts.build_queue import step_estimator
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import cloud_progress
from scripts.build_queue import person_actions, queue_snapshot
from scripts.build_targets import target_config
from scripts.build_targets.execution_transport import ExecutionTransport, HanaGpuNextestTransport
from scripts.build_queue.admission_records import AdmissionWaitSnapshot, WaitHolder, reason_phrase
from scripts.build_queue.system_clock import SystemClock as SystemClock
from scripts.lint import memory_admit as gate
from scripts.build_hold import override_guard
from scripts.production import staff
from scripts.production.gate_routes import SuiteMode

from scripts.buildlog import repository, store
from scripts.lib import test_phase
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
from scripts.lib.build_steps import gate_job_from_process

from scripts.build_queue.scope_activity import (
    IdleService, LiveBuildRun, NoLiveBuildRun, NotServiceSlice, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob,
    ScopeActivityReader, ScopeProcesses,
)
from scripts.lint.build_shape import build_shape, recorded_build_shape
from scripts.lib.atomic_file import atomic_sample, write_text_atomic
from scripts.accounts import outage_alerts

if TYPE_CHECKING:
    from scripts.aws_target import ledger as aws_ledger
    from scripts.mac_test.claim_state import RemoteJobClaim




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


class CloudGateProvider(Protocol):
    def availability(self, *, mode: SuiteMode) -> placement.Available | placement.Unavailable: ...

    def reconcile_dispatch(self, binding: aws_ledger.GateDispatchBinding) -> None: ...


def scaler_provider() -> CloudGateProvider:
    """Load the scaler adapter on the provider evidence worker."""
    return cast(CloudGateProvider, cast(object, import_module("scripts.aws_target.cloud_gate")))


def scaler_availability(mode: SuiteMode) -> placement.AllowedHost | placement.TemporaryExclusion:
    try:
        result = scaler_provider().availability(mode=mode)
    except AttributeError:
        return placement.TemporaryExclusion("AWS availability is unavailable")
    return placement.AllowedHost() if isinstance(result, placement.Available) else placement.TemporaryExclusion(result.reason)


class GuestSettlementProvider(Protocol):
    def settlement(self, episode: str, attempt: int, *,
                   legacy_holder_ended: bool = False) -> placement.Settled | placement.Pending: ...


def mac_linux_provider() -> GuestSettlementProvider:
    """Load the guest settlement provider on the evidence worker."""
    return cast(GuestSettlementProvider, cast(object, import_module("scripts.build_targets.mac_linux")))


def guest_settlement(episode: str, attempt: int, *, holds_slot: Callable[[], bool],
                     legacy_holder_ended: bool = False) -> placement.DispatchPathState:
    try:
        provider = mac_linux_provider()
        result = provider.settlement(episode, attempt, legacy_holder_ended=legacy_holder_ended)
    except AttributeError:
        return placement.DispatchPathPending("Mac Linux settlement is unavailable")
    if isinstance(result, placement.Pending):
        reconcile = cast(Callable[[str, int], None] | None, getattr(provider, "reconcile_unproven", None))
        if reconcile is not None and holds_slot():
            reconcile(episode, attempt)
    return placement.DispatchPathSettled() if isinstance(result, placement.Settled) else placement.DispatchPathPending(result.reason)


CARGO_CAPTURE_ROOT = Path("/tmp/cargo-tile")
READING_ALERT_KEY = "build-admission-reading"

PlacementEvidenceKeys = dict[tuple[str, placement.PlacementDestination], placement.HostEvidenceKey | placement.EvidenceNeeded]


@dataclass(frozen=True)
class RetryAfterAvailability:
    """The target needs an available reading taken after the decline."""
    declined_at: float


PLACEMENT_REASON_TEXT = {
    "mac_linux_off": "the guest is off",
    "mac_linux_unregistered": "the guest has no queue registration",
    "mac_linux_settings_unreadable": "the guest settings are unreadable or invalid",
    "mac_linux_backoff_unreadable": "the guest backoff record is unreadable or invalid",
    "mac_linux_backoff": "the guest is in retry backoff",
    "mac_linux_reading_required": "the queue needs a newer guest reading",
    MAC_LINUX_NO_ANSWER: "the Mac does not answer",
    "busy": "the target is busy",
    "off": "the target is off",
    "disk": "the target needs more disk space",
    "command": "the target does not support this command",
    "repo": "the target does not support this repository",
    "linux_only": "this step runs only on Linux",
    "offload_exclude": "the target excludes this step",
}


def confirmed_guest_run(assignment: placement.PlacementAssignment,
                        evidence: execution_identity.ExecutionEvidence) -> records.ConfirmedGuestRun | None:
    """Use execution evidence that matches the assigned guest and attempt."""
    destination = assignment.destination
    if (destination.target != "mac-linux" or destination.provider != "ssh"
            or not assignment.attempt or not isinstance(evidence, execution_identity.ObservedExecution)):
        return None
    identity = evidence.identity
    matches = (identity.host == destination.host and identity.pid > 0 and bool(identity.start_time)
               if isinstance(identity, execution_identity.ProcessExecution)
               else (identity.target, identity.attempt) == (destination.target, assignment.attempt))
    return {"target": destination.target, "attempt": assignment.attempt} if matches else None


@dataclass(frozen=True)
class MacLinuxReadyForProbe:
    """The guest is enabled and its backoff permits a reading."""

    registry: conf.ConfigRead
    failed_at: float


def mac_linux_backoff(now: float) -> MacLinuxReadyForProbe | placement.TemporaryExclusion:
    from scripts.mac_test import eligibility

    try:
        registry = _provider_configuration().read("build_targets")
        target = records.effective_target(eligibility.MAC_LINUX_TARGET,
            placement.ConfiguredProviderSettings(), configuration=registry)
        if not isinstance(target, target_config.SshTarget):
            return placement.TemporaryExclusion("mac_linux_unregistered")
        if target.platform != "linux":
            return placement.TemporaryExclusion("mac_linux_settings_unreadable")
        if not eligibility.mac_linux_enabled(registry):
            return placement.TemporaryExclusion("mac_linux_off")
    except (OSError, ValueError):
        return placement.TemporaryExclusion("mac_linux_settings_unreadable")
    try:
        backoff = eligibility.read_backoff(now, eligibility.MAC_LINUX_TARGET, strict=True)
        if isinstance(backoff, eligibility.ActiveBackoff):
            return placement.TemporaryExclusion("mac_linux_backoff")
        if isinstance(backoff, eligibility.UnreadableBackoff):
            return placement.TemporaryExclusion("mac_linux_backoff_unreadable")
        failed_at = eligibility.backoff_path(eligibility.MAC_LINUX_TARGET).stat().st_mtime
    except target_config.ConfigError:
        return placement.TemporaryExclusion("mac_linux_unregistered")
    except FileNotFoundError:
        failed_at = float("-inf")
    except OSError:
        return placement.TemporaryExclusion("mac_linux_backoff_unreadable")
    return MacLinuxReadyForProbe(registry, failed_at)


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


def send_admission_alert(recipient: str, message: str) -> bool:
    """Send an alert outside the admission pass."""
    from scripts.message import send_budget, send_result
    from scripts.message.session_send import session_send_argv

    try:
        settings = conf.read("lint")
        command = session_send_argv(recipient, sender="build-admission", lint_settings=settings)
        result = subprocess.run(command, input=message, text=True, capture_output=True,
                                timeout=send_budget.seconds(settings.number("sweep_sender_timeout_seconds")))
        if result.returncode in (send_result.EXIT["sent"], send_result.EXIT["queued"]):
            return True
        print(f"memory admission: alert to {recipient} failed: {result.stderr.strip()}", file=sys.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"memory admission: alert to {recipient} failed: {error}", file=sys.stderr)
    return False


def deliver_reading_alert(claimed: outage_alerts.AlertClaim, hold: UnreadableReadingHold, elapsed: float) -> None:
    """Send the claimed alert outside the admission pass."""
    message = (f"Build admission: {hold.path} is unreadable.\nReason: {hold.reason}\n"
               f"This reading has held build starts for {elapsed:g} seconds.\n"
               "Restore the reading on natedev. Admission holds starts until the reading works.")
    delivered = False
    try:
        if send_admission_alert("natedev", message):
            outage_alerts.delivered(claimed)
            delivered = True
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
        from scripts.lib.build_folders import build_root

        try:
            root = build_root(settings=conf.read("build_folders"))
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
OwnerProcessReading = gate.ProcessIdentity | OwnerProcessGone | OwnerProcessUnreadable


def linux_owner_process(pid: int, proc: Path) -> OwnerProcessReading:
    """Read the parent PID and start time from one process record."""
    if pid <= 0:
        return OwnerProcessUnreadable("invalid process id")
    try:
        stat = read_proc_text(proc / str(pid) / "stat")
    except (FileNotFoundError, ProcessLookupError):
        return OwnerProcessGone()
    except OSError as error:
        return OwnerProcessUnreadable(str(error))
    _, separator, rest = stat.rpartition(")")
    fields = rest.split()
    if (not separator or len(fields) <= 19 or not fields[19].isascii() or not fields[19].isdigit()
            or fields[0] not in ("R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I")):
        return OwnerProcessUnreadable("invalid process stat fields")
    if fields[0] in ("Z", "X"):
        return OwnerProcessGone()
    try:
        return gate.ProcessIdentity(int(fields[1]), fields[19])
    except ValueError:
        return OwnerProcessUnreadable("invalid parent process id")


def owner_may_be_live(reading: OwnerStartReading, start_time: str) -> bool:
    """Keep a known owner until a reading confirms that it ended."""
    return isinstance(reading, OwnerProcessUnreadable) or reading == LiveOwnerIdentity(start_time)


class Proc(Protocol):
    @property
    def path(self) -> Path: ...
    def processes(self) -> dict[int, gate.ProcessIdentity]: ...
    def owner_start(self, pid: int) -> OwnerStartReading: ...
    def owner_process(self, pid: int) -> OwnerProcessReading: ...
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
        process = self.owner_process(pid)
        return LiveOwnerIdentity(process.start) if isinstance(process, gate.ProcessIdentity) else process

    def owner_process(self, pid: int) -> OwnerProcessReading:
        root = self.path
        if "BUILDLOG_PROC" not in os.environ and (verify_root := os.environ.get("VERIFY_PROC_ROOT")) is not None:
            root = Path(verify_root)
        return linux_owner_process(pid, root)

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
    queue_identity: records.QueueLifeIdentity = field(kw_only=True)
    move_id: str = field(default="", kw_only=True)
    ci_runner: str = field(default="", kw_only=True)
    run: wire.RunIdentity | wire.NoRun = field(default=wire.NO_RUN, kw_only=True)
    # The saved job start keeps the place of a CI step through a service restart.
    ci_job_start: records.CiJobStart = field(default=records.NO_JOB_START, kw_only=True)


def waiter_place(row: records.Waiter, episode: str, *, run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
                 ) -> WaiterPlace:
    """Keep a logical identity with every active place."""
    return WaiterPlace(wire.Owner(row["pid"], row["start_time"]), row["since"], episode,
        row.get("priority_since", OrderNotStarted()), row.get("token_left_at", TokenNotLeft()),
        queue_identity=gate.queue_life_identity(row), move_id=row.get("move_id", ""),
        ci_runner=row.get("ci_runner", ""), run=run, ci_job_start=records.waiter_job_start(row))


@dataclass(frozen=True)
class NotEnding:
    pass


@dataclass(frozen=True)
class ReleaseStatus:
    request_id: str
    status: int


@dataclass(frozen=True)
class FirstStepHandOff:
    """The caller gave its first-step grant to its command, which sends its own first step; no step ended."""
    request_id: str


@dataclass(frozen=True)
class OwnerExited:
    pass


@dataclass(frozen=True)
class LocalGrantReturned:
    """The caller gave back its local placement grant."""


@dataclass(frozen=True)
class NoFinalizationIntent:
    pass


@dataclass(frozen=True)
class TokenWaiting:
    request_id: str
    left_at: float


ReservationEnding = NotEnding | ReleaseStatus | FirstStepHandOff | OwnerExited | LocalGrantReturned | TokenWaiting
# The caller ended the grant itself, with a status or at a handoff.
CallerRelease = ReleaseStatus | FirstStepHandOff


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
    ending: ReservationEnding = NotEnding()
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
    if step in records.REPOSITORY_GATE_STEPS and Path(repository_name).is_absolute() and Path(worktree).is_absolute():
        name = gate_worktree_repository_name(worktree)
        if isinstance(name, str):
            return name
    return repository_name


def profile_repository_name(profile: records.StepProfile | records.ProgramProfile) -> str:
    """Get the repository name used by the run limits."""
    if isinstance(profile.repository, records.RepositoryIdentity):
        return profile.repository.name or profile.repo
    if isinstance(profile, records.ProgramProfile):
        return gate_repository_name(profile.step, profile.repo, profile.worktree)
    return profile.repo


@dataclass(frozen=True)
class LiveGrant:
    pass


@dataclass(frozen=True)
class NoQueueUndoAnchor:
    """The replay entry has no saved queue anchor."""


@dataclass(frozen=True)
class ReplayEntry:
    reply: wire.Reply
    ended_at: float | LiveGrant
    reservation: str = ""
    undo_anchor: QueueAnchor | NoQueueUndoAnchor = NoQueueUndoAnchor()


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
class PendingVerifyGrants:
    """The reservations can still return the slot before execution."""
    reservations: frozenset[str]


@dataclass(frozen=True)
class UsedVerifySlot:
    """A local step used the slot, so the run keeps it until exit."""


@dataclass(frozen=True)
class QueueHeadAnchor:
    """Place a pinned row at the head of the full order."""


@dataclass(frozen=True)
class QueueRowAnchor:
    """Place a pinned row directly after this row."""

    row_id: str


QueueAnchor = QueueHeadAnchor | QueueRowAnchor


@dataclass(frozen=True)
class QueuePin:
    row_id: str
    anchor: QueueAnchor


QueuePins = tuple[QueuePin, ...]


def queue_anchor_at(order: list[str], index: int) -> QueueAnchor:
    return QueueRowAnchor(order[index - 1]) if index else QueueHeadAnchor()


def pinned_order(order: list[str], pins: QueuePins) -> list[str]:
    """Follow the pinned rows after each row in the main order."""
    if not pins:
        return order
    members = set(order)
    followers = {pin.anchor: pin.row_id for pin in pins if pin.row_id in members
                 and (isinstance(pin.anchor, QueueHeadAnchor) or pin.anchor.row_id in members)}
    pinned = set(followers.values())
    ordered: list[str] = []
    roots = (QueueHeadAnchor(), *(QueueRowAnchor(row_id) for row_id in order if row_id not in pinned))
    for anchor in roots:
        if isinstance(anchor, QueueRowAnchor):
            ordered.append(anchor.row_id)
        while anchor in followers:
            row_id = followers.pop(anchor)
            ordered.append(row_id)
            anchor = QueueRowAnchor(row_id)
    return ordered


@dataclass(frozen=True)
class VerifyRunSlot:
    """Hold one repository slot until the Verify process exits."""
    repository: str
    owner: wire.Owner
    use: PendingVerifyGrants | UsedVerifySlot = UsedVerifySlot()

    @property
    def run_key(self) -> str:
        return records.run_owner(self.owner.pid, self.owner.start_time)

    @property
    def key(self) -> str:
        return f"{self.run_key}:{self.repository}"


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
    queue_pins: QueuePins = ()
    bumps: person_actions.BumpProgress = field(default_factory=person_actions.BumpProgress)
    verify_run_slots: dict[str, VerifyRunSlot] = field(default_factory=dict, kw_only=True)
    held_places: tuple[records.HeldPlace, ...] = field(default=(), kw_only=True)
    placement_replacements: dict[str, ReplayEntry] = field(default_factory=dict, kw_only=True)


def saved_verify_run_slots(state: ServiceState) -> dict[str, VerifyRunSlot]:
    """Keep legacy slots only when the state records a local grant."""
    slots = {key: slot for key, slot in state.verify_run_slots.items() if key == slot.key}
    if len(slots) == len(state.verify_run_slots):
        return slots
    granted: set[VerifyRunSlot] = set()
    for record in state.reservations.values():
        if isinstance(record.place, wire.LocalPlace) and "verify" in record.queue_waiter:
            repository = record.queue_waiter.get("repository_name", record.repo)
            granted.add(VerifyRunSlot(repository, record.owner))
            if isinstance(record.run, wire.RunIdentity):
                granted.add(VerifyRunSlot(repository, wire.Owner(record.run.pid, record.run.start_time)))
    for saved in state.assignments.values():
        assignment = saved.assignment
        request = state.placement_requests.get(assignment.episode)
        if (assignment.destination.provider == "local" and isinstance(saved.permission, placement.PermissionConsumed)
                and request is not None and isinstance(request.identity.verify, records.VerifyCall)):
            repository = profile_repository_name(request.profile)
            granted.add(VerifyRunSlot(repository, wire.Owner(request.owner.pid, request.owner.start_time)))
            if isinstance(request.run, wire.RunIdentity):
                granted.add(VerifyRunSlot(repository, wire.Owner(request.run.pid, request.run.start_time)))
    slots.update((slot.key, slot) for slot in state.verify_run_slots.values() if slot in granted)
    return slots


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
            return gate.scope_memory(lifetime.directory, gate.ScopeMembershipPass())
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


def local_reservation_row(record: ReservationRecord, evidence: SystemEvidence) -> records.Reservation:
    row = reservation_row(record)
    if "build_run" not in row:
        row["build_run"] = evidence.build_run(record.owner.pid)
        row["run_origin"] = records.FoundRun(row["build_run"])
    return row


def resolve_reservation_repository(row: records.Reservation, record: ReservationRecord) -> None:
    repository_name = record.queue_waiter.get("repository_name", record.repo)
    resolved_name = gate_repository_name(record.step, repository_name, record.worktree)
    if resolved_name != repository_name:
        row["queue_waiter"] = {**record.queue_waiter, "repository_name": resolved_name}


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
    membership = gate.ScopeMembershipPass()
    unowned = gate.unowned_slice_memory([], processes, proc.path,
        activity_reader=activity, ci_memory_slice=plan.config.ci_memory_slice, membership=membership)
    has_caller = plan.runs or any(isinstance(row.activity, ScopeProcesses) and row.activity.session_id for row in unowned)
    staff_members = records.queue_staff() if has_caller else records.NO_STAFF_LOOKUP

    def scope_caller(evidence: ScopeProcesses, unit: str = "") -> records.QueueCaller:
        # A scope with no recorded caller gets its owner from the family of its oldest process.
        if evidence.session_name or evidence.session_id or not evidence.members:
            return records.queue_caller(evidence.session_id, "", evidence.session_name, "",
                                        members=staff_members, unit=unit)
        family = records.family_owner(evidence.members[0].pid, proc.path)
        if isinstance(family, records.StaffOwner):
            return records.queue_caller("", family.member_id, family.name, "", members=staff_members, unit=unit)
        name = family.program if isinstance(family, records.OwnerNotGiven) else family.name
        return records.queue_caller(evidence.session_id, "", name, "", members=staff_members, unit=unit)

    unowned = tuple(replace(row, activity=replace(evidence, session_name=scope_caller(evidence).name))
                    if isinstance(evidence := row.activity, ScopeProcesses) else row for row in unowned)
    members: dict[str, frozenset[int]] = {}
    workers: dict[str, frozenset[int]] = {}
    for reading in unowned:
        if not reading.cgroup or reading.diagnostic:
            continue
        members[reading.cgroup] = membership.read(Path(reading.cgroup))
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
        tree = gate.process_tree({run.pid: run.start_time}, processes)
        measured = gate.process_memory(tree - scope_process_memory.keys(), proc.path)
        cached = [scope_process_memory[pid] for pid in tree & scope_process_memory.keys()]
        used = measured.bytes + sum(item.bytes for item in cached)
        swap = measured.swap_bytes + sum(item.swap_bytes for item in cached)
        caller = scope_caller(evidence, place.unit)
        memory = records.UnownedSliceMemory(used, swap_bytes=swap,
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
    read: Callable[[], placement.DispatchPathState | wire.ExecutionReport] = field(compare=False)


@dataclass(frozen=True)
class DispatchEvidence:
    """The reading applies only to the saved assignment that requested it."""
    saved: records.SavedPlacementAssignment
    result: placement.DispatchPathState | wire.ExecutionReport
    observed_at: float
    observed_monotonic: float


@dataclass(frozen=True)
class NoHostObservation:
    """The worker has no observation for this key."""


@dataclass(frozen=True)
class HoldReadingNotSupplied:
    """The hold must read the worker's observation."""


HostObservation = placement.HostEvidence | NoHostObservation
HOLD_READING_NOT_SUPPLIED = HoldReadingNotSupplied()


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

    def observation(self, key: placement.HostEvidenceKey) -> HostObservation:
        with self.lock:
            observed = self.observations.get(key)
        return observed if observed is not None else NoHostObservation()

    def evidence(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        observed = self.observation(key)
        return (placement.HostEvidenceUnavailable(key, self.clock.now(), "not read")
                if isinstance(observed, NoHostObservation) else observed)

    def publish(self, evidence: placement.HostEvidence) -> None:
        with self.lock:
            self.observations[evidence.key] = evidence

    def dispatch_pending(self, probe: DispatchEvidenceProbe) -> bool:
        """Keep the attempt until the worker confirms settlement."""
        return self.dispatch_settled_at(probe) is None

    def dispatch_settled_at(self, probe: DispatchEvidenceProbe) -> float | None:
        """Request a reading and return the time of exact settlement."""
        assignment = probe.saved.assignment
        attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        with self.lock:
            previous = self.dispatch_probes.get(attempt)
            if previous != probe:
                self.dispatch_probes[attempt] = probe
                _ = self.dispatch_observations.pop(attempt, None)
                self.due[probe.key] = float('-inf')
                self.requested.set()
            return self._settlement_time(probe.saved, attempt)

    def observed_settlement_time(self, saved: records.SavedPlacementAssignment, attempt: str) -> float | None:
        with self.lock:
            return self._settlement_time(saved, attempt)

    def _settlement_time(self, saved: records.SavedPlacementAssignment, attempt: str) -> float | None:
        observed = self.dispatch_observations.get(attempt)
        if (observed is not None and observed.saved == saved
                and isinstance(observed.result, placement.DispatchPathSettled)
                and (is_mac_linux_guest(saved.assignment.destination)
                     or time.monotonic() - observed.observed_monotonic <= self.interval)):
            return observed.observed_at
        return None

    def recovered_dispatch(self, saved: records.SavedPlacementAssignment) -> wire.ExecutionReport | placement.NoTerminalReport:
        """Read the settled report for the same saved assignment."""
        assignment = saved.assignment
        attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        with self.lock:
            observed = self.dispatch_observations.get(attempt)
            if observed is not None and observed.saved == saved and isinstance(observed.result, wire.ExecutionReport):
                return observed.result
        return placement.NoTerminalReport()

    def forget_dispatch(self, attempt_key: str) -> None:
        """Remove the probe and reading for a closed attempt."""
        with self.lock:
            _ = self.dispatch_probes.pop(attempt_key, None)
            _ = self.dispatch_observations.pop(attempt_key, None)

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
            else:
                evidence = replace(evidence)
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
            dispatches_read.append((attempt, probe, DispatchEvidence(probe.saved, result, self.clock.now(), time.monotonic())))
        with self.lock:
            if self.subscriptions.get(key) is not subscription:
                return True
            self.observations[key] = evidence
            for attempt, probe, observed in dispatches_read:
                if (self.dispatch_probes.get(attempt) is probe
                        and (not is_mac_linux_guest(probe.saved.assignment.destination)
                             or self._settlement_time(probe.saved, attempt) is None)):
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
            and scopes_overlap(first.scope, second.scope, mac=first.provider == "mac"))


def _hold_exclusion(request: wire.PlacementRequest, destination: placement.PlacementDestination,
                    eligibility: placement.HostEligibility) -> bool:
    """Check if an exclusion permits one more remote probe."""
    if not isinstance(eligibility, placement.TemporaryExclusion):
        return False
    if eligibility.reason == "busy":
        return True
    if destination.provider != "mac" or destination.target != "mac" or eligibility.reason not in (
            "blocked", "backoff", "unreachable"):
        return False
    selection, route = request.constraints.selection, request.route
    if isinstance(selection, placement.PinnedHost):
        return selection.target == "mac"
    return isinstance(selection, placement.ConfiguredOrder) and (
        isinstance(route, target_config.MacOnly) or isinstance(route, target_config.PinnedTarget) and route.target == "mac")


@dataclass(frozen=True)
class PlacementHold:
    """A remote wait and the reading that can start or end its hold."""

    episode: str
    destination: placement.PlacementDestination
    key: placement.HostEvidenceKey
    observed_at: float
    reading: HostObservation
    answered: bool


@dataclass(frozen=True)
class PlacementHoldStart:
    started_at: float
    reading: HostObservation


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


@dataclass(frozen=True)
class PublishedPlacementWait:
    input_event_count: int
    request_digest: bytes
    reply: wire.PlacementWait


@dataclass
class PublishedReplies:
    view: wire.ViewReply
    explain: wire.ExplainReply
    wait_positions: dict[str, int]
    placement_waits: dict[str, PublishedPlacementWait]
    view_bytes: bytes
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
    placement_request_digest: bytes | None = None


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
    ranked_waiters: list[tuple[Path, records.RankedWaiter]]


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


BeforeLaunchRefusal = placement.PrelaunchRefused | execution_identity.HanaCommandAdmissionRefused
SCALER_SETTLEMENT_REQUESTED = placement.AttemptDeclined("scaler settlement requested")
GUEST_SETTLEMENT_REQUESTED = placement.AttemptDeclined("Mac Linux settlement requested")


def is_mac_linux_guest(destination: placement.PlacementDestination) -> bool:
    return destination.provider == "ssh" and destination.target == "mac-linux"


def _placement_history_applies(saved: records.SavedPlacementAssignment, request: wire.PlacementRequest) -> bool:
    """Keep a cancelled guest's retry decision with its coordinator after settlement."""
    assignment = saved.assignment
    return (assignment.episode == request.episode
            and (saved.state != placement.ClosedAssignment("cancelled")
                 or not is_mac_linux_guest(assignment.destination)
                 or not isinstance(saved.permission, placement.PermissionConsumed)
                 or assignment.owner == request.owner and assignment.since == request.since))


@dataclass(frozen=True)
class GuestWaitLimitUnavailable:
    """The configuration has no usable Mac Linux wait limit."""


def _guest_wait_limit() -> int | GuestWaitLimitUnavailable:
    try:
        with conf.report_key_changes():
            return target_config.unreachable_wait_seconds(_provider_configuration().read("build_targets"))
    except (OSError, conf.Unusable):
        return GuestWaitLimitUnavailable()


def _guest_wait_timeout(attempt: str, limit: int | GuestWaitLimitUnavailable) -> str:
    if isinstance(limit, GuestWaitLimitUnavailable):
        return f"The Mac Linux wait limit is unavailable for attempt {attempt}."
    return f"Mac Linux did not confirm the end of attempt {attempt} within {limit} seconds."


@dataclass(frozen=True)
class MacLinuxStateReady:
    root: Path


@dataclass(frozen=True)
class MacLinuxStateUnavailable:
    reason: str


def _holds_consumed_guest_slot(saved: records.SavedPlacementAssignment) -> bool:
    # A consumed guest holds the slot until the service records closure.
    return (isinstance(saved.state, placement.CurrentAssignment)
            and is_mac_linux_guest(saved.assignment.destination)
            and isinstance(saved.permission, placement.PermissionConsumed))


def _holds_scaler_slot(saved: records.SavedPlacementAssignment) -> bool:
    return isinstance(saved.state, placement.CurrentAssignment) and saved.assignment.destination.provider == "scaler"


@dataclass
class WaitingAdmission:
    request: wire.AdmitRequest | wire.LaunchRequest
    row: records.Waiter
    connection: Connection | NoConnection
    last_reply: wire.Wait | NoWaitReply = NoWaitReply()
    last_sent: float = float("-inf")
    continuation_claim: records.ContinuationSlot | NoContinuationClaim = NoContinuationClaim()


def without_owners(inputs: records.QueueInputs, owners: set[str]) -> records.QueueInputs:
    """Remove held waiters from the order that the admission pass may start."""
    if not owners:
        return inputs
    return replace(inputs, ordered=[(path, row) for path, row in inputs.ordered if records.waiter_owner(row) not in owners])


@dataclass(frozen=True)
class NoPass:
    pass


def _no_fault(_stage: str) -> None:
    pass


def _place_key(owner: wire.Owner | placement.SubmittingOwner, since: float) -> str:
    return f"{records.run_owner(owner.pid, owner.start_time)}:{since:.17g}"


@dataclass(frozen=True)
class RunStep:
    """One step of a run, as rule 20 counts it."""
    key: str
    # The step runs test binaries (records.TEST_BINARY_STEPS).
    tests: bool


def _run_step(owner: wire.Owner | placement.SubmittingOwner, since: float, step: str, scope: str) -> RunStep:
    """Name one step of a run: verify.sh sends every step with one owner and one arrival time.

    The local grant and the Mac start of one step give the same name, so a step that moves counts once.
    """
    return RunStep("\t".join((_place_key(owner, since), step, scope)), step in records.TEST_BINARY_STEPS)


def _local_step(record: ReservationRecord) -> RunStep:
    return _run_step(record.owner, record.queue_waiter["since"], record.step, record.test_scope)


def _placed_step(request: wire.PlacementRequest) -> RunStep:
    profile = request.profile
    scope = profile.scope if isinstance(profile, records.StepProfile) else "full"
    return _run_step(request.owner, request.since, profile.step, scope)


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


def finalization_outcome(ending: ReservationEnding
                         ) -> tuple[step_estimator.FinalOutcome, int | step_estimator.OutcomeNotRecorded,
                                    gate.StepEnd | gate.TokenReturn | gate.HandOff]:
    if isinstance(ending, TokenWaiting):
        return step_estimator.Completed(), 0, gate.TokenReturn()
    if isinstance(ending, FirstStepHandOff):
        # The step goes on under the command's own admission.
        return step_estimator.HandedOff(), gate.OUTCOME_NOT_RECORDED, gate.HandOff()
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
                 places: dict[str, WaiterPlace], writes: ServiceStateWrites | RegisteredServiceWrites, *,
                 retire_verify_grant: Callable[[str], None] | None = None) -> None:
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
        self.retire_verify_grant: Callable[[str], None] | None = retire_verify_grant

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
        key = _place_key(record.owner, record.queue_waiter["since"])
        place = self.places.get(key)
        # A placement release can precede a refusal before launch.
        awaiting_report = (isinstance(record.ending, CallerRelease) and place is not None
                           and place.episode == self.episode and bool(place.move_id))
        if not isinstance(record.ending, TokenWaiting) and not awaiting_report:
            _ = self.places.pop(key, None)
        del self.reservations[self.episode]
        if self.retire_verify_grant is not None:
            self.retire_verify_grant(record.reservation_id)
        self.persist()


@dataclass(frozen=True)
class ServiceEstimatedAdmission(wire.AdmitRequest):
    """Keep the runner separate from the unit used for priority."""
    runner: str = field(kw_only=True)
    job_start: records.CiJobStart = field(default=records.NO_JOB_START, kw_only=True)


def wait_line_name(waiting: WaitingAdmission) -> str:
    """Name a queued build from its recorded owner and step."""
    row = waiting.row
    what = codec.row_what(row.get("what", ""), row.get("step", ""))
    return codec.wait_line_name(row.get("unit", ""), row.get("session_name", ""), row.get("folder", ""), what)


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
                 host_probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] | None = None,
                 job_start_reader: Callable[[wire.Owner], records.CiJobStart] | None = None) -> None:
        state_dir = state_dir.resolve()
        self.writes: ServiceStateWrites = ServiceStateWrites(state_dir)
        self.writes.require()
        self.state_dir: Path = state_dir
        self.clock: Clock = clock
        self.readings: Readings = readings
        self.proc: Proc = proc
        # An older CI front sends no job start, so the service reads it from the runner process.
        self.job_start_reader: Callable[[wire.Owner], records.CiJobStart] = (job_start_reader if job_start_reader is not None
            else lambda owner: ci_job_start.read_queue_job_start(owner.pid, owner.start_time, proc.path))
        self.fault: Callable[[str], None] = fault
        self.socket_path: Path = socket_path if socket_path is not None else wire.admission_socket_path()
        self.waiters: dict[str, WaitingAdmission] = {}
        self.verify_run_slots: dict[str, VerifyRunSlot] = {}
        self.pending_cloud_groups: dict[str, PendingCloudGroup] = {}
        self.queue_pins: QueuePins = ()
        self.move_line: records.QueueMoveLine = records.QueueMoveLine(uuid.uuid4().hex, ())
        self.reservations: dict[str, ReservationRecord] = {}
        self.lifecycle_outbox: tuple[PendingAdmission | PendingLeft, ...] = ()
        self.places: dict[str, WaiterPlace] = {}
        self.run_places: dict[str, wire.RunPlace] = {}
        self.held_places: tuple[records.HeldPlace, ...] = ()
        self.build_history_generation: int = 0
        self.ledger: ReplayLedger = ReplayLedger({})
        self.placement_replacements: ReplayLedger = ReplayLedger({})
        self.placement_requests: dict[str, wire.PlacementRequest] = {}
        self.placement_request_digests: dict[str, bytes] = {}
        self.refused_mac_only: set[str] = set()
        self.input_event_count: int = 0
        self.pending_coordinators: dict[placement.SubmittingOwner, PendingCoordinatorEvidence] = {}
        self.assignments: dict[str, records.SavedPlacementAssignment] = {}
        self.current_assignments: dict[str, str] = {}
        self.mac_linux_state: MacLinuxStateReady | MacLinuxStateUnavailable = MacLinuxStateUnavailable(
            "The Mac Linux state root setup has not run.")
        self.manual_guest_settlement: set[str] = set()
        self.execution_evidence: dict[str, execution_identity.ExecutionEvidence] = {}
        self.prerequisites: dict[str, placement.LocalExecutionPrerequisite] = {}
        self.placement_eligibility: dict[str, tuple[placement.CandidateEligibility, ...]] = {}
        self.local_hold_holders: dict[str, placement.ResourceHolder] = {}
        self.host_probe: Callable[[placement.HostEvidenceKey], placement.HostEvidence] = (
            host_probe if host_probe is not None else self._read_host_evidence)
        self.host_readers: dict[placement.HostEvidenceKey, HostEvidenceReader] = {}
        self.host_targets: dict[tuple[str, str], HostEvidenceWorker] = {}
        self.mac_recovery_lock: threading.Lock = threading.Lock()
        self.mac_recovery_retry_at: dict[str, float] = {}
        self.background_local_evidence: bool = host_probe is None
        self.host_observations: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        self.host_requests: dict[placement.HostEvidenceKey, wire.PlacementRequest] = {}
        self.placement_host_keys: dict[tuple[str, placement.PlacementDestination], placement.HostEvidenceKey] = {}
        self.settlement_host_keys: dict[str, placement.HostEvidenceKey] = {}
        # Start time of each hold that keeps a remote destination for an older waiting request.
        self.placement_holds: dict[tuple[str, placement.PlacementDestination], PlacementHoldStart] = {}
        # The reading that answered each ended hold. That reading never starts another hold.
        self.placement_hold_answers: dict[tuple[str, placement.PlacementDestination], placement.HostEvidence] = {}
        self.spacing: records.StartSpacing = records.ReadyToStart()
        self.last_inputs: records.QueueInputs | NoPass = NoPass()
        self.last_result: gate.PassResult | NoPass = NoPass()
        self.last_policy: records.AdmissionPolicy | NoPass = NoPass()
        # The natedev hold of each held local waiter at the last pass.
        self.local_holds: dict[str, records.NatedevHeld | records.ReadingUnavailable] = {}
        self.reading_hold: UnreadableReadingHold | NoReadingHold = NoReadingHold()
        self.reading_alert_delivery: ReadingAlertDelivery | NoReadingAlertDelivery = NoReadingAlertDelivery()
        self.reading_alert_needs_clear: bool = True
        self.outage_alert_needs_clear: bool = True
        self.listener_started_at: float = float("inf")
        self.configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration = gate.UnusableAdmissionConfiguration("configuration not read")
        self.last_usable_configuration: records.MemoryConfig | NoPass = NoPass()
        self.request_slow_seconds: float = float("inf")
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
        self.bumps: person_actions.BumpProgress = person_actions.BumpProgress()
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
                                                 row.get("unit", ""), members=members)
                    record = replace(record, queue_waiter={**row, "session_name": caller.name, "unit": caller.unit})
                    state.reservations[episode] = record
                self.writes.register(record)
            for key, place in state.places.items():
                identity = place.queue_identity
                caller = self._stored_caller(identity["session_id"], identity["session_name"],
                                             identity.get("unit", ""), members=members)
                state.places[key] = replace(place, queue_identity={
                    **identity, "session_name": caller.name, "unit": caller.unit})
            self.reservations, self.places = state.reservations, state.places
            self.queue_pins = state.queue_pins
            self.ledger = ReplayLedger(state.ledger)
            self.placement_replacements = ReplayLedger(state.placement_replacements)
            self.spacing, self.priority = state.spacing, state.priority
            self.bumps = state.bumps
            self.build_history_generation = state.build_history_generation
            self.run_places = state.run_places
            self.held_places = state.held_places
            self.verify_run_slots = saved_verify_run_slots(state)
            self.lifecycle_outbox = state.lifecycle_outbox
            self.caller_versions = state.caller_versions
            self.placement_requests = {
                key: replace(request, identity=self._named_identity(self._placement_identity(request), members=members))
                for key, request in state.placement_requests.items()}
            self.assignments = state.assignments
            self.current_assignments = state.current_assignments
            self.pending_cloud_groups = state.pending_cloud_groups
            self._check_saved_remote_slots()
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

    def _check_saved_remote_slots(self) -> None:
        guests = {key: saved for key, saved in self.assignments.items() if _holds_consumed_guest_slot(saved)}
        self.manual_guest_settlement = {key for key, saved in guests.items()
            if len(guests) > 1 or self.current_assignments.get(saved.assignment.episode) != key}
        guest_episodes = {guests[key].assignment.episode for key in self.manual_guest_settlement}
        scalers = {key for key, saved in self.assignments.items() if _holds_scaler_slot(saved)
                   and (saved.assignment.episode in guest_episodes
                        or self.current_assignments.get(saved.assignment.episode) != key)}
        if not self.manual_guest_settlement and not scalers:
            return
        python = str(Path(__file__).resolve().parents[1] / "lib/py")
        steps: list[str] = []
        for key in sorted(self.manual_guest_settlement):
            saved = guests[key]
            assignment = saved.assignment
            command = [python, "-m", "scripts.build_targets.mac_linux", "--reconcile-end",
                       assignment.episode, str(placement.scaler_attempt_number(assignment.attempt))]
            steps.append(f"{key!r} (queue unit: run {shlex.join(command)})")
        for key in sorted(scalers):
            command = [python, "-m", "scripts.aws_target.cloud_gate", "--reconcile", "--directory",
                       str(target_config.ssh_target_state_directory("aws"))]
            steps.append(f"{key!r} (ledger-wide cleanup, queue unit: run {shlex.join(command)})")
        message = "memory admission: placement settlement requires manual recovery. " + ", ".join(steps)
        print(message, file=sys.stderr, flush=True)
        try:
            threading.Thread(target=send_admission_alert, args=("builds", message),
                             name="admission-settlement-alert", daemon=True).start()
        except RuntimeError as error:
            print(f"memory admission: cannot send the settlement alert: {error}", file=sys.stderr)

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

    def _inputs_with_current_slots(self, inputs: records.QueueInputs,
                                   ordered: list[tuple[Path, records.RankedWaiter]]) -> records.QueueInputs:
        self._claim_slots(ordered)
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
                       occupied_cargo_folders=occupied, verify_run_owners=self._verify_run_owners())

    def _refresh_pass_slots(self, inputs: records.QueueInputs,
                            ordered: list[tuple[Path, records.RankedWaiter]]) -> records.QueueInputs:
        waiting_owners = {records.waiter_owner(waiting.row) for waiting in self.waiters.values()}
        local_paths = {path for path, row in inputs.ordered if records.waiter_owner(row) in waiting_owners}
        inputs = replace(inputs, ordered=[(path, row) for path, row in ordered if path in local_paths])
        return self._inputs_with_current_slots(inputs, ordered)

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
            replay_cutoff = now - self.last_usable_configuration.replay_retention_seconds
            self.ledger.expire(replay_cutoff, protected_keys, live_reservations)
            self.placement_replacements.expire(replay_cutoff, set(), set())
            self._prune_ended_placements(now - self.last_usable_configuration.ended_placement_seconds)
        if self.bumps.moves:
            active = self.reservations.keys() | self.placement_requests.keys() | {place.episode for place in self.places.values()}
            if not self.bumps.moves.keys() <= active:
                self.bumps = replace(self.bumps, moves={episode: move for episode, move in self.bumps.moves.items()
                                                       if episode in active})
        state = ServiceState(self.reservations, self.places, self.ledger, self.spacing, self.priority,
            self.build_history_generation, self.run_places, self.caller_versions, self.lifecycle_outbox,
            self.placement_requests, self.assignments, self.current_assignments, self.pending_cloud_groups,
            queue_pins=self.queue_pins, bumps=self.bumps, verify_run_slots=self.verify_run_slots,
            held_places=self.held_places, placement_replacements=self.placement_replacements)
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
            if isinstance(value, ReplayLedger):
                previous = self.durable_encodings.get((member.name, ""))
                if previous is None or previous.value != value.revision:
                    changed = True
                    previous = DurableRecordEncoding(value.revision, wire.encode_record(dict(value)), "")
                encodings[member.name, ""] = previous
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
        owners.update(slot.owner for slot in self.verify_run_slots.values())
        owners.update(wire.Owner(request.owner.pid, request.owner.start_time)
                      for request in self.placement_requests.values() if request.owner.host == self.host)
        owners.update(self._claim_holders())
        owners.update(pending.group.coordinator for pending in self.pending_cloud_groups.values())
        for descriptor, watch in list(self.watchers.items()):
            if (isinstance(watch, OwnerWatch) and watch.owner not in owners
                    or isinstance(watch, ScopeWatch) and watch.episode not in self.reservations):
                self._unwatch(descriptor)

    def _local_hold_reasons(self) -> dict[str, records.NatedevHeld | records.ReadingUnavailable]:
        """Hold each local waiter that a natedev hold applies to, at each pass."""
        if self.host != "natedev":
            return {}
        waiting = {episode: item for episode, item in self.waiters.items()
                   if isinstance(item.request, wire.AdmitRequest) and isinstance(item.request.place, wire.LocalPlace)}
        if not waiting:
            return {}
        from scripts.build_hold import build_hold
        from scripts.build_targets import build_targets

        try:
            directory = build_hold.holder_directory()
            holds = build_hold.natedev_holds(directory)
        except OSError as error:
            # The enforcer stays the guard for other local steps while the hold state is unreadable.
            return {episode: records.ReadingUnavailable(f"natedev hold state is unreadable: {error}")
                    for episode, item in waiting.items() if isinstance(item.request, ServiceEstimatedAdmission)}
        if not holds.in_force:
            return {}
        reasons: dict[str, records.NatedevHeld | records.ReadingUnavailable] = {}
        own_keys: dict[OwningSession | NoOwningSession, str] = {}
        for episode, item in waiting.items():
            request = item.request
            if not isinstance(request, wire.AdmitRequest):
                continue
            session = request.session
            if isinstance(session, wire.SessionNotSent):
                # An older client names only its own session, in its queue row.
                session_id = item.row.get("session_id", "")
                session = OwningSession(session_id) if session_id else NoOwningSession()
            if session not in own_keys:
                try:
                    own_keys[session] = build_targets.own_hold_key(session, directory)
                except OSError:
                    # The staff record is unreadable: no hold is the step's own, so the step waits.
                    own_keys[session] = ""
            owner = request.owner
            lineage = build_targets.read_caller_lineage(self.proc.path, owner.pid, owner.start_time)
            blocking = build_targets.blocking_hold(holds, own_keys[session], lineage)
            if isinstance(blocking, build_hold.Holder):
                reasons[episode] = records.NatedevHeld(blocking.name, blocking.purpose)
        return reasons

    def _ordered_waiters(self) -> list[tuple[Path, records.RankedWaiter]]:
        """Collect the present queue rows."""
        rows: list[tuple[Path, records.RankedWaiter]] = []
        for waiting in self.waiters.values():
            if isinstance(waiting.request, wire.AdmitRequest) and isinstance(waiting.request.place, wire.CloudPlace):
                continue
            row = gate.rank_waiter(waiting.row, records.NormalOrder())
            run = waiting.request.run if isinstance(waiting.request, wire.AdmitRequest) else wire.NO_RUN
            row["order_since"] = self._order_since(row["since"], run, records.waiter_job_start(row))
            rows.append((self.state_dir / records.waiter_owner(row), row))
        for request in self.placement_requests.values():
            if request.episode in self.waiters or not self._placement_waiting(request):
                continue
            row = gate.rank_waiter(self._placement_row(request), records.NormalOrder())
            row["order_since"] = self._order_since(row["since"], request.run, records.waiter_job_start(row))
            rows.append((self.state_dir / records.waiter_owner(row), row))
        return rows

    @staticmethod
    def _priority_name(row: records.Waiter) -> str:
        return row.get("ci_runner") or row.get("unit", "")

    def _queue_order(self) -> list[tuple[Path, records.RankedWaiter]]:
        return self._visible_queue_order(self._full_queue_order(self._ranked_waiters()))

    def _ranked_waiters(self) -> list[tuple[Path, records.RankedWaiter]]:
        rows = self._ordered_waiters()
        present = {row.get("move_id") for _, row in rows}
        for place in self.places.values():
            if place.move_id and place.move_id not in present:
                grant = self.reservations.get(place.episode)
                row: records.Waiter = (grant.queue_waiter if grant is not None else
                    {**place.queue_identity, "need": 0, "ci_runner": place.ci_runner})
                if isinstance(place.ci_job_start, records.JobStart):
                    row = {**row, "ci_job_start": place.ci_job_start}
                ranked = gate.rank_waiter({**row, "move_id": place.move_id,
                    "order_since": self._order_since(place.since, place.run, place.ci_job_start)}, records.NormalOrder())
                key = records.waiter_owner(row) if grant is not None else place.move_id
                rows.append((self.state_dir / key, ranked))
        if isinstance(self.priority, records.QueuePriority):
            rows = self._rank_priority_waiters(rows, self.priority)
        return sorted(rows, key=lambda item: self._waiter_order(item[1]))

    def _waiter_run_place(self, row: records.Waiter) -> wire.RunPlace | None:
        place = self.places.get(_place_key(wire.Owner(row["pid"], row["start_time"]), row["since"]))
        if place is not None and isinstance(place.run, wire.RunIdentity):
            return self.run_places.get(records.run_owner(place.run.pid, place.run.start_time))
        return None

    def _waiter_order(self, row: records.RankedWaiter) -> tuple[int, float, float, int, float, int]:
        priority, order_since, since, pid = gate.waiter_order(row)
        place = self._waiter_run_place(row)
        if place is not None and order_since == place.since:
            # Keep the run rank and order its steps by arrival time.
            return priority, order_since, place.since, place.first_owner.pid, since, pid
        return priority, order_since, since, pid, since, pid

    def _full_queue_order(self, ranked: list[tuple[Path, records.RankedWaiter]]
                          ) -> list[tuple[Path, records.RankedWaiter]]:
        saved = {place.move_id for place in self.places.values() if place.move_id}
        rows = {row_id: (path, row) for path, row in ranked if (row_id := row.get("move_id")) in saved}
        return [rows[row_id] for row_id in pinned_order(list(rows), self.queue_pins)]

    def _visible_queue_order(self, ordered: list[tuple[Path, records.RankedWaiter]]
                             ) -> list[tuple[Path, records.RankedWaiter]]:
        visible = {waiting.row.get("move_id") for waiting in self.waiters.values()}
        reservations = self.last_inputs.reservations if isinstance(self.last_inputs, records.QueueInputs) else ()
        counted = {row.get("reservation_id") for row in reservations}
        for record in self.reservations.values():
            if record.reservation_id in counted:
                # The pass already shows this grant in Running.
                continue
            place = self.places.get(_place_key(record.owner, record.queue_waiter["since"]))
            if place is not None and place.episode == record.episode and place.move_id:
                visible.add(place.move_id)
        for request in self.placement_requests.values():
            if request.episode in self.waiters or not self._placement_waiting(request):
                continue
            place = self.places.get(_place_key(request.owner, request.since))
            if place is not None and place.episode == request.episode:
                visible.add(place.move_id)
        return [(path, row) for path, row in ordered if row.get("move_id") in visible]

    def _rank_priority_waiters(self, rows: list[tuple[Path, records.RankedWaiter]], priority: records.QueuePriority
                               ) -> list[tuple[Path, records.RankedWaiter]]:
        # Match CI priority by runner. Publish the current step's unit.
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
        ranked, _ = gate.apply_queue_priority(ranking_rows, gate.QueueOrderContext(priority, labels))
        return [(path, gate.rank_waiter(ci_waiters[path], records.waiter_priority(row))
                 if path in ci_waiters else row) for path, row in ranked]

    def _current_move_line(self, rows: list[tuple[Path, records.RankedWaiter]],
                           movable: set[str]) -> records.QueueMoveLine:
        kept, counts = records.kept_place_order(self.held_places, (row for _, row in rows), gate.waiter_order, self.priority)
        members: list[records.QueueMoveMember] = []
        for (_, row), count in zip(rows, counts, strict=True):
            row_id = row.get("move_id")
            assert row_id
            members.append(records.QueueMoveMember(row_id, movable=row_id in movable, kept_before=count))
        return self._revise_move_line(tuple(members), tuple(place.failed_run for place in kept))

    def _hidden_speculative_waits(self) -> set[str]:
        # A speculative copy still waits for placement while its parent holds local memory.
        return {place.move_id for request in self.placement_requests.values()
                if isinstance(request.purpose, placement.SpeculativeCopy)
                and (parent := self.reservations.get(request.purpose.parent_episode)) is not None
                and isinstance(parent.place, wire.LocalPlace)
                and (place := self.places.get(_place_key(request.owner, request.since))) is not None
                and place.episode == request.episode}

    def _published_queue_order(self, ordered: list[tuple[Path, records.RankedWaiter]], hidden: set[str]
                               ) -> list[tuple[Path, records.RankedWaiter]]:
        candidates = self.last_inputs.placement_candidates if isinstance(self.last_inputs, records.QueueInputs) else {}
        return [(path, row) for path, row in ordered if row.get("move_id") not in hidden
                or any(candidate.destination.target == "mac-linux"
                       for candidate in candidates.get(records.waiter_owner(row), ()))]

    def _revise_move_line(self, members: tuple[records.QueueMoveMember, ...],
                          kept_runs: tuple[str, ...]) -> records.QueueMoveLine:
        if members != self.move_line.members or kept_runs != self.move_line.kept_runs:
            self.move_line = records.QueueMoveLine(uuid.uuid4().hex, members, kept_runs=kept_runs)
        return self.move_line

    def _movable_wait_ids(self, *, check_owners: bool = False) -> set[str]:
        return {waiting.row.get("move_id", "") for episode, waiting in self.waiters.items()
            if episode not in self.current_assignments and waiting.row.get("move_id") and (not check_owners or
                self.proc.owner_start(waiting.request.owner.pid) == LiveOwnerIdentity(waiting.request.owner.start_time))
            and ((request := self.placement_requests.get(episode)) is None
                 or (request.owner.host == self.host
                     and isinstance(self._local_placement_eligibility(request),
                                    (placement.AllowedHost, placement.EvidenceNeeded))))}

    def _prune_runs(self) -> None:
        now = self.clock.now()
        for key, slot in list(self.slots.items()):
            if (not self._run_alive(gate.between_steps(slot).run)
                    or not gate.slot_in_force(slot, now)):
                del self.slots[key]
        held_before = self.held_places
        ended: list[wire.RunPlace] = []
        for key, place in list(self.run_places.items()):
            owner = wire.Owner(place.run.pid, place.run.start_time)
            if self._live(owner):
                self._watch_owner(owner)
            else:
                del self.run_places[key]
                ended.append(place)
        for place in ended:
            self._keep_place(place, now)
        self._end_held_places(now)
        # Save each held place change at once, so a restart writes no second end event.
        if self.held_places is not held_before:
            self.persist()

    def _free_claim(self, owner: str) -> None:
        for key, slot in list(self.slots.items()):
            if isinstance(slot, records.ContinuationSlot) and slot.owner == owner:
                if gate.slot_in_force(slot.slot, self.clock.now()) and self._run_alive(slot.slot.run):
                    self.slots[key] = slot.slot
                else:
                    del self.slots[key]

    def _prune_verify_run_slots(self) -> None:
        for key, slot in list(self.verify_run_slots.items()):
            if owner_may_be_live(self.proc.owner_start(slot.owner.pid), slot.owner.start_time):
                self._watch_owner(slot.owner)
            else:
                del self.verify_run_slots[key]

    def _verify_run_owners(self) -> dict[str, frozenset[str]]:
        owners: dict[str, set[str]] = {}
        for slot in self.verify_run_slots.values():
            owners.setdefault(slot.repository, set()).add(slot.run_key)
        return {repository: frozenset(runs) for repository, runs in owners.items()}

    def _finish_verify_grant(self, reservation: str, *, started: bool) -> None:
        for key, slot in self.verify_run_slots.items():
            if not isinstance(slot.use, PendingVerifyGrants) or reservation not in slot.use.reservations:
                continue
            remaining = slot.use.reservations - {reservation}
            if started:
                self.verify_run_slots[key] = replace(slot, use=UsedVerifySlot())
            elif remaining:
                self.verify_run_slots[key] = replace(slot, use=PendingVerifyGrants(remaining))
            else:
                del self.verify_run_slots[key]
            return

    def _verify_slot_owner(self, request: wire.AdmitRequest, repository: str) -> wire.Owner:
        owner, run = request.owner, request.run
        if VerifyRunSlot(repository, owner).key in self.verify_run_slots:
            return owner
        inherited = isinstance(run, wire.RunIdentity) and run.pid != owner.pid
        if not inherited and not any(slot.repository == repository for slot in self.verify_run_slots.values()):
            return owner
        child = self.proc.owner_process(owner.pid)
        if not isinstance(child, gate.ProcessIdentity) or child.start != owner.start_time:
            return owner
        lineage = {owner.pid: child}
        while child.parent > 0 and child.parent not in lineage:
            pid = child.parent
            parent = self.proc.owner_process(pid)
            if not isinstance(parent, gate.ProcessIdentity) or int(parent.start) > int(child.start):
                break
            lineage[pid] = parent
            ancestor = VerifyRunSlot(repository, wire.Owner(pid, parent.start))
            if (ancestor.key in self.verify_run_slots or
                    isinstance(run, wire.RunIdentity) and run == wire.RunIdentity(pid, parent.start)):
                # Check for process exit or reparenting during the walk.
                if all(self.proc.owner_process(pid) == identity for pid, identity in lineage.items()):
                    return ancestor.owner
                break
            child = parent
        return owner

    def _verify_slot_requests(self, inputs: records.QueueInputs, limits: records.RepositoryRunLimits
                              ) -> dict[str, VerifyRunSlot]:
        slots: dict[str, VerifyRunSlot] = {}
        for waiting in self.waiters.values():
            row, request = waiting.row, waiting.request
            owner = records.waiter_owner(row)
            if (owner not in inputs.waiter_orders or not isinstance(request, wire.AdmitRequest)
                    or not isinstance(request.place, wire.LocalPlace) or "verify" not in row):
                continue
            if limits.exempt_unit and (row.get("unit") == limits.exempt_unit or owner in inputs.run_limit_exempt_owners):
                continue
            repository = row.get("repository_name", "")
            slots[owner] = VerifyRunSlot(repository, self._verify_slot_owner(request, repository))
        return slots

    def _claim_slots(self, ordered: list[tuple[Path, records.RankedWaiter]]) -> None:
        held = self._held_owners()
        for waiting in self.waiters.values():
            # A held waiter gives back its slot, so the slot can expire during the hold.
            if records.waiter_owner(waiting.row) in held and isinstance(waiting.continuation_claim, records.ContinuationSlot):
                self._free_claim(records.waiter_owner(waiting.row))
                waiting.continuation_claim = NoContinuationClaim()
        claimed = {slot.owner for slot in self.slots.values() if isinstance(slot, records.ContinuationSlot)}
        waiters = {records.waiter_owner(item.row): item for item in self.waiters.values()}
        for _, row in ordered:
            owner = records.waiter_owner(row)
            waiting = waiters.get(owner)
            if waiting is None or owner in held or not gate.can_use_natedev(row):
                continue
            request = waiting.request
            saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
            if saved is not None and isinstance(saved.remote_start_hold, records.RemoteStartHold):
                continue
            placement_request = self.placement_requests.get(request.episode)
            if placement_request is not None and not isinstance(self._local_placement_eligibility(placement_request),
                                                                (placement.AllowedHost, placement.EvidenceNeeded)):
                continue
            if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
                continue
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

    def _held_owners(self) -> set[str]:
        return {records.waiter_owner(self.waiters[episode].row) for episode in self.local_holds if episode in self.waiters}

    def _run_place(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace,
                   machines: tuple[str, ...]) -> None:
        if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
            return
        if not isinstance(request, wire.AdmitRequest) or not isinstance(request.run, wire.RunIdentity):
            return
        run = request.run
        key = records.run_owner(run.pid, run.start_time)
        current = self.run_places.get(key)
        if current is not None:
            line = current.unit_place
            if isinstance(line, status_wire.UnitPlace) and not set(machines) <= set(line.machines):
                named = tuple(dict.fromkeys((*line.machines, *machines)))
                self.run_places[key] = replace(current, unit_place=replace(line, machines=named))
        elif self._run_alive(run):
            new = wire.RunPlace(run, place.since, request.owner, request.identity.unit)
            job = records.run_job_name(request.identity.job, request.identity.verify)
            self.run_places[key] = self._take_place(new, profile_repository_name(request.profile), job, machines)

    def _take_place(self, new: wire.RunPlace, repository: str, job: str,
                    machines: tuple[str, ...]) -> wire.RunPlace:
        """Rules 13 and 20: a new run takes the oldest place of the waiting run and the held place of its key."""
        config = self.last_usable_configuration
        exempt = config.policy.repository_runs.exempt_unit if isinstance(config, records.MemoryConfig) else ""
        if not new.unit or not repository or not job or new.unit == exempt:
            return new
        waiting = self._waiting_runs(new.unit, repository, job)
        key = (new.unit, repository, records.place_kind(job))
        held = [kept for kept in self.held_places
                if (kept.unit, kept.repository, records.place_kind(kept.job)) == key]
        self.held_places = tuple(kept for kept in self.held_places if kept not in held)
        took_place: status_wire.TookPlace = "failed" if held else "waiting" if waiting else ""
        line = status_wire.UnitPlace(repository, job, machines,
            replaces=max((0, *(line.replaces for _, line in waiting), *(kept.replaces for kept in held))),
            took_place=took_place)
        for place, older in waiting:
            self.run_places[records.run_owner(place.run.pid, place.run.start_time)] = replace(
                place, unit_place=replace(older, replaced_by=new.run))
            self._end_replaced_run(place.run)
        since = min((new.since, *(place.since for place, _ in waiting), *(kept.since for kept in held)))
        return replace(new, since=since, unit_place=line)

    def _waiting_runs(self, unit: str, repository: str,
                      job: str) -> list[tuple[wire.RunPlace, status_wire.UnitPlace]]:
        """Each live run of the key that has no grant and no newer run in its place."""
        assigned = {request.run for request in self.placement_requests.values()
                    if request.episode in self.current_assignments}
        return [(place, line) for place in self.run_places.values()
                if place.unit == unit and isinstance(line := place.unit_place, status_wire.UnitPlace)
                and line.repository == repository and records.place_kind(line.job) == records.place_kind(job)
                and line.reservations == 0
                and isinstance(line.replaced_by, status_wire.NotReplaced) and place.run not in assigned
                and self._live(wire.Owner(place.run.pid, place.run.start_time))]

    def _replaced(self, run: wire.RunIdentity | wire.NoRun) -> bool:
        if not isinstance(run, wire.RunIdentity):
            return False
        place = self.run_places.get(records.run_owner(run.pid, run.start_time))
        return (place is not None and isinstance(line := place.unit_place, status_wire.UnitPlace)
                and isinstance(line.replaced_by, wire.RunIdentity))

    def _end_replaced_run(self, run: wire.RunIdentity) -> None:
        """Answer each open request of a replaced run, and take its steps out of the line."""
        self._schedule_pass()
        refused = wire.Refused(status_wire.REPLACED_REASON)
        episodes = {episode for episode, request in self.placement_requests.items() if request.run == run}
        for episode, waiting in tuple(self.waiters.items()):
            if isinstance(waiting.request, wire.AdmitRequest) and waiting.request.run == run:
                episodes.add(episode)
        for episode in episodes:
            submitted = self.placement_requests.pop(episode, None)
            if submitted is not None:
                key = self._placement_replacement_key(episode, submitted.owner)
                self.placement_replacements[key] = ReplayEntry(refused, self.clock.now())
                _ = self.placement_request_digests.pop(episode, None)
                self.refused_mac_only.discard(episode)
                self._end_placement_rank(submitted)
            waiting = self.waiters.get(episode)
            if waiting is not None:
                self._end_move(waiting)
                self.ledger[episode] = ReplayEntry(refused, self.clock.now())
                self._remove_waiter(waiting, refused)
        parked = [item for item in self.parked_placements if item.request.episode in episodes]
        self.parked_placements = [item for item in self.parked_placements if item not in parked]
        for item in parked:
            self._send(item.connection, refused)

    @staticmethod
    def _placement_replacement_key(episode: str, owner: placement.SubmittingOwner) -> str:
        return "placement-replaced:" + json.dumps((episode, owner.host, owner.pid, owner.start_time))

    def _granted(self, run: wire.RunIdentity | wire.NoRun, step: RunStep) -> None:
        """Count a grant of the run. A new grant of its first step does not count again.

        The caller can give a grant back before its command runs and then wait again for the same step.
        The status of that give-back is not the result of the step, so the new grant clears it.
        """
        if not isinstance(run, wire.RunIdentity):
            return
        key = records.run_owner(run.pid, run.start_time)
        place = self.run_places.get(key)
        if place is None or not isinstance(line := place.unit_place, status_wire.UnitPlace):
            return
        if line.reservations == 0:
            line = replace(line, reservations=1, first_step=step.key)
        elif step.key == line.first_step:
            line = replace(line, first_status=status_wire.NO_FIRST_STATUS)
        else:
            line = replace(line, reservations=line.reservations + 1)
        if step.key == line.failed_step:
            line = replace(line, failed_step="")
        if step.tests:
            line = replace(line, tests_started=True)
        self.run_places[key] = replace(place, unit_place=line)

    def _released(self, run: wire.RunIdentity | wire.NoRun, step: RunStep, status: int) -> None:
        """Keep the status of the first step while it is the only step of the run.

        Also keep the latest step that failed before the tests started. A failed test step keeps no place.
        """
        if not isinstance(run, wire.RunIdentity):
            return
        key = records.run_owner(run.pid, run.start_time)
        place = self.run_places.get(key)
        if place is None or not isinstance(line := place.unit_place, status_wire.UnitPlace):
            return
        if (line.reservations == 1 and line.first_step == step.key
                and isinstance(line.first_status, status_wire.NoFirstStatus)):
            line = replace(line, first_status=status)
        if status != 0 and not step.tests and not line.tests_started:
            line = replace(line, failed_step=step.key)
        self.run_places[key] = replace(place, unit_place=line)

    def _handed_off(self, run: wire.RunIdentity | wire.NoRun, step: RunStep) -> None:
        """A grant that only held the place until the command sent its own first step is not a step.

        The caller does not call _released for a handoff, so its status is not the result of a step.
        """
        if not isinstance(run, wire.RunIdentity):
            return
        key = records.run_owner(run.pid, run.start_time)
        place = self.run_places.get(key)
        # Only a handoff before any other step is known: copies.py starts a new run for each command.
        if (place is not None and isinstance(line := place.unit_place, status_wire.UnitPlace)
                and line.reservations == 1 and line.first_step == step.key):
            self.run_places[key] = replace(place, unit_place=replace(line, reservations=0, first_step="",
                first_status=status_wire.NO_FIRST_STATUS, tests_started=False))

    def _copy_passed(self, run: wire.RunIdentity | wire.NoRun) -> None:
        """A passed copy is the result of its step.

        The copy watch then stops the local step, so the status of that local release is not the result.
        A copy has its own owner and arrival time, so it cannot name its step. A copy runs only while
        its local step holds a grant, so in a run of one step it is a copy of that step.
        The copy watch copies only a test step (cargo nextest), so its local release never sets failed_step.
        """
        if not isinstance(run, wire.RunIdentity):
            return
        key = records.run_owner(run.pid, run.start_time)
        place = self.run_places.get(key)
        if (place is not None and isinstance(line := place.unit_place, status_wire.UnitPlace)
                and line.reservations == 1):
            self.run_places[key] = replace(place, unit_place=replace(line, first_status=0))

    def _keep_place(self, ended: wire.RunPlace, now: float) -> None:
        """Rule 20: keep the place of a run that failed before its tests, or failed or ended with no result in its only step."""
        line = ended.unit_place
        rules = self.last_usable_configuration
        if (not isinstance(line, status_wire.UnitPlace) or not isinstance(rules, records.MemoryConfig)
                or not isinstance(limits := rules.replace_after_fail, records.ReplaceAfterFail)
                or isinstance(line.replaced_by, wire.RunIdentity) or line.replaces >= limits.max_times
                or not (line.failed_step or (line.reservations == 1 and line.first_status != 0))):
            return
        replaces = line.replaces + 1
        waiting = self._waiting_runs(ended.unit, line.repository, line.job)
        for place, older in waiting:
            self.run_places[records.run_owner(place.run.pid, place.run.start_time)] = replace(
                place, since=min(place.since, ended.since),
                unit_place=replace(older, replaces=max(older.replaces, replaces), took_place="failed"))
        if waiting:
            return
        key = (ended.unit, line.repository, records.place_kind(line.job))
        same = [kept for kept in self.held_places
                if (kept.unit, kept.repository, records.place_kind(kept.job)) == key]
        end: records.HeldPlaceEnd = ("no_result" if not line.failed_step
            and isinstance(line.first_status, status_wire.NoFirstStatus) else "failed")
        step_key = (line.failed_step or line.first_step) if end == "failed" else ""
        failed_step = step_key.split("\t", 2)[1] if "\t" in step_key else step_key
        held = records.HeldPlace(ended.unit, line.repository, line.job,
            min((ended.since, *(kept.since for kept in same))), now, now + limits.time_limit_seconds,
            records.run_owner(ended.run.pid, ended.run.start_time),
            line.machines, max((replaces, *(kept.replaces for kept in same))), end, failed_step=failed_step)
        self.held_places = (*(kept for kept in self.held_places if kept not in same), held)

    def _end_held_places(self, now: float) -> None:
        """A held place that no new run took ends at its time limit."""
        ended = [held for held in self.held_places if held.ends_at <= now]
        if not ended:
            return
        self.held_places = tuple(held for held in self.held_places if held not in ended)
        for held in ended:
            gate.append_queue_time(self.state_dir, {"kind": "held_place_ended", "unit": held.unit,
                "repository": held.repository, "job": held.job, "since": held.since, "failed_at": held.failed_at,
                "ends_at": held.ends_at, "failed_run": held.failed_run,
                "ended": held.ended}, now)

    def _order_since(self, since: float, run: wire.RunIdentity | wire.NoRun, job_start: records.CiJobStart) -> float:
        """Rank a step at the earliest of its arrival, the first step of its run, and the start of its CI job."""
        since = records.job_order_since(since, job_start)
        if isinstance(run, wire.RunIdentity):
            place = self.run_places.get(records.run_owner(run.pid, run.start_time))
            if place is not None:
                return min(place.since, since)
        return since

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
        if owner_exit is not None:
            owner = owner_exit.owner
            for key, slot in tuple(self.verify_run_slots.items()):
                if slot.owner == owner:
                    del self.verify_run_slots[key]
        for key, place in list(self.places.items()):
            ended = owner_exit is not None and owner_exit.owner == place.owner
            if (ended or not self._live(place.owner)) and not any(
                    record.owner == place.owner for record in self.reservations.values()):
                self._record_left(place.episode, place.queue_identity, "owner_gone")
                self._clear_wait_rank(place.owner, place.since, place.episode)
                del self.places[key]
        self.persist()

    def _row(self, request: wire.AdmitRequest | wire.LaunchRequest, place: WaiterPlace | records.NoWaiter) -> records.Waiter:
        profile, identity = request.profile, request.identity
        since = request.since if isinstance(place, records.NoWaiter) else place.since
        row: records.Waiter = {
            "pid": request.owner.pid, "start_time": request.owner.start_time,
            "need": (0 if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
                     else profile.peak.bytes), "since": since,
            "order_since": since,
            "session_id": identity.session_id, "session_name": identity.name,
            "step": identity.step, "folder": identity.folder, "unit": identity.unit,
            "estimated_at": profile.estimated_at or self.clock.now(),
            "fresh_test": isinstance(request, wire.AdmitRequest) and request.fresh_test,
            "need_source": profile.peak.source, "need_runs": profile.peak.count,
            "need_basis": gate.need_basis_record(profile.peak.basis), "test_scope": profile.scope,
            "repository_name": profile_repository_name(profile),
            "route": request.route if isinstance(request, wire.AdmitRequest) else records.DIRECT_CARGO,
        }
        if isinstance(place, WaiterPlace) and place.episode == request.episode and place.move_id:
            row["move_id"] = place.move_id
        if identity.what:
            row["what"] = identity.what
        if isinstance(identity.verify, records.VerifyCall):
            row["verify"] = identity.verify
        if isinstance(identity.job, StepJob):
            row["job"] = identity.job
        if isinstance(request, ServiceEstimatedAdmission):
            row["ci_runner"] = request.runner
            if isinstance(request.job_start, records.JobStart):
                row["ci_job_start"] = request.job_start
        if isinstance(place, WaiterPlace) and not isinstance(place.priority_since, OrderNotStarted):
            row["priority_since"] = place.priority_since
        if isinstance(place, WaiterPlace) and not isinstance(place.token_left_at, TokenNotLeft):
            row["token_left_at"] = place.token_left_at
            if isinstance(request, wire.AdmitRequest) and request.token_back_at:
                row["token_back_at"] = request.token_back_at
        if isinstance(profile.repository, records.RepositoryIdentity):
            row["repo_identity"] = profile.repository.key
            row["legacy_name"] = profile.repository.legacy_name
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
            row = local_reservation_row(record, evidence)
            if isinstance(record.run, wire.RunIdentity) and not self._run_alive(record.run):
                row["run_origin"] = records.CallerRun(NoLiveBuildRun())
            captured = evidence.reservations.get(episode)
            memory = (reservation_charge(captured, row, now, config.policy) if captured is not None
                      else records.PromisedMemory(record.need))
            if isinstance(memory, records.GoneMemory):
                with retry_cleanup(Path(record.path)):
                    self._end(episode, record.ending if not isinstance(record.ending, NotEnding) else OwnerExited())
                continue
            resolve_reservation_repository(row, record)
            lifetime = evidence.lifetimes.get(episode, gate.LiveOwnerProcess())
            unscoped_direct = (not record.sidecar and episode not in self.placement_requests
                               and episode in evidence.lifetimes)
            if isinstance(lifetime, gate.PopulatedStepScope) or unscoped_direct:
                self._clear_wait_rank(record.owner, record.queue_waiter["since"], episode)
            if record.episode.startswith("uid:") and isinstance(memory, records.MeasuredMemory):
                if isinstance(memory.anonymous, records.AnonymousRunUse):
                    self._sample_runner(record, memory.anonymous.bytes, now)
            memories.append((row, memory))
            self._watch_scope(lifetime, episode)
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
        self._prune_runs()
        ranked = self._ranked_waiters()
        ordered = self._visible_queue_order(self._full_queue_order(ranked))
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
            and ("verify" in row or isinstance(limits.for_step(
                self.host, row.get("repository_name", ""), row.get("step", "")), records.RepositoryRunLimit))))
        labels: Mapping[str, gate.UnitMembership] = gate.priority_labels(sessions) if sessions else {}
        exempt = frozenset(records.waiter_owner(row) for _, row in local_rows
            if (session := row.get("session_id", "")) in labels
            and gate.queue_unit(row.get("unit", ""), session, labels) == limits.exempt_unit)
        inputs = gate.build_queue_inputs(local_rows, memories, readings, now, self.priority, unowned)
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
        local = self._progress_inputs(self._inputs_with_current_slots(inputs, ordered), dict(evidence.processes),
                                      config.policy, profile_rank, profile_share)
        return AdmissionPassInputs(local, ordered, ranked)

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
            try:
                with conf.diagnostics(report=False) as messages:
                    self.request_slow_seconds = conf.read("build_admission").number("request_slow_seconds")
            except conf.Unusable as error:
                records.warn_configuration_diagnostics((records.ConfigurationDiagnostic(str(error)),))
            else:
                records.warn_configuration_diagnostics(records.configuration_diagnostics(messages))
        return configuration

    def _take_configuration(self) -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
        configuration = self.pending_configuration
        self.pending_configuration = NoPass()
        return self._read_configuration() if self._configuration_changed() or isinstance(configuration, NoPass) else configuration


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

    def _schedule_pass(self) -> None:
        """Invalidate placement replies and make a pass due."""
        self.input_event_count += 1
        self.next_pass = min(self.next_pass, self.clock.monotonic())

    def _pass_when_open(self, configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                        ) -> bool:
        """Run a pass now when the gap allows it. Otherwise keep the configuration and mark a pass due."""
        if self._pass_gap_open():
            _ = self._run_pass(configuration)
            return True
        if isinstance(self.pending_configuration, NoPass):
            self.pending_configuration = configuration
        self._schedule_pass()
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
                reply = self._placement_reply(item.request,
                    self.local_hold_holders.get(item.request.episode, placement.ResourceHolderUnknown()))
                self._send(item.connection, reply)

    def _pass(self, configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
              ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
        self.last_pass_started = self.clock.monotonic()
        self._apply_bumps()
        self._prune_runs()
        self._prune_verify_run_slots()
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
            (self.state_dir / queue_snapshot.VIEW_NAME).unlink(missing_ok=True)
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
        self.local_holds = local_holds = self._local_hold_reasons()
        held_owners = self._held_owners()
        captured = self._inputs(config, self.configuration.progress_profile_rank, self.configuration.peak_history_share_percent,
                                self.configuration.progress_profile_sweep_seconds)
        inputs = replace(captured.local, verify_run_owners=self._verify_run_owners())
        if self.host == "natedev" and (floor := config.free_floor_gib) is not None:
            inputs = replace(inputs, build_space=self.readings.build_space(floor))
        inputs = without_owners(inputs, held_owners)
        verify_slots = self._verify_slot_requests(inputs, policy.repository_runs)
        inputs = replace(inputs, verify_slot_requests={owner: slot.run_key for owner, slot in verify_slots.items()})
        # The placement pass rebuilds the local order from the queue order, so remove held waiters again.
        inputs = without_owners(self._placement_pass(
            inputs, captured.queue_order, policy, remote_start_hold_seconds, evidence_keys), held_owners)
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
        starts = [row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)]
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
            self._admit([waiting], config, self.configuration.peak_history_share_percent,
                        cargo_folder=cargo_folder, verify_slots=verify_slots)
        self.persist()
        visible_order = self._visible_queue_order(self._full_queue_order(captured.ranked_waiters))
        published = self._project_replies(visible_order)
        assert isinstance(published, PublishedReplies)
        waiting_steps = published.view.reading.view.waiting
        positions = published.wait_positions
        decisions = {decision.owner: decision for decision in result.rows}
        for owner, position in positions.items():
            episode = by_owner.get(owner, "")
            waiting = self.waiters.get(episode)
            if waiting is None:
                continue
            if (entry := decisions.get(owner)) is not None:
                reason, decision, holder = entry.reason, entry.decision, entry.holder
                label = head_label
            elif (held_reason := local_holds.get(episode)) is not None:
                reason, holder = held_reason, records.NoHolder()
                decision = records.AdmissionDecision("hold", waiting.row["need"], 0, 0, 0, free_passes=False)
                label = ""
            else:
                continue
            cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
            order = decision.order_hold
            blocker = (positions.get(records.waiter_owner(order.waiter), 0)
                       if isinstance(cause, records.BehindHead) and isinstance(order, records.HeldBehindHead) else 0)
            text = f"behind #{blocker}" if blocker else reason_phrase(reason, waiting_steps)
            reply = wire.Wait(reason, position, label, text,
                self._wait_snapshot(waiting, decision, inputs, config, reason, holder, head_label, head_need))
            if waiting.last_reply != reply or inputs.now - waiting.last_sent >= config.wait_reply_seconds:
                self._deliver(waiting, reply)
                waiting.last_reply, waiting.last_sent = reply, inputs.now
        self._publish_replies(published, placement_keys=evidence_keys)
        interval = (config.waiting_pass_seconds if self.waiters or any(self._placement_waiting(request)
                    for request in self.placement_requests.values()) else config.running_pass_seconds)
        self.next_pass = self.clock.monotonic() + interval
        if starts or cloud_started:
            self.next_pass = self.clock.monotonic()
        remaining = {owner for owner, episode in by_owner.items() if episode in self.waiters}
        snapshot_owners = (inputs.waiter_orders.keys() | held_owners) & remaining
        ordered = [(path, row) for path, row in visible_order if records.waiter_owner(row) in snapshot_owners]
        try:
            queue_snapshot.write_queue_snapshot(self.state_dir,
                replace(inputs, ordered=ordered, spacing=self.spacing,
                        slots=tuple(slot for slot in inputs.slots
                            if gate.between_steps(slot).reservation not in result.policy.returned_claims)), policy)
        except OSError:
            # An old snapshot expires without changing the admission result.
            pass
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

    def _cloud_slots_used(self) -> int:
        legacy = sum(isinstance(record.place, wire.CloudPlace) for record in self.reservations.values())
        placed = sum(_holds_scaler_slot(saved) for saved in self.assignments.values())
        return legacy + placed

    def _cloud_slots_full(self) -> bool:
        return (not isinstance(self.configuration, gate.UsableAdmissionConfiguration)
                or self._cloud_slots_used() >= self.configuration.cloud_gate_slots)

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
        used = self._cloud_slots_used()
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
                    self._admit(members, config, configuration.peak_history_share_percent,
                                cargo_folder=placement.NoCargoFolder(), verify_slots={})
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
        used, slots = self._cloud_slots_used(), configuration.cloud_gate_slots
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
                current, counter, wait_state))
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
                progress.read_counter(sidecar) if request.sidecar else progress.NoCounter(), wire.CloudSlotWait(used, slots)))
        return wire.CloudQueueRead(reading.view, reading.explanation, reading.ordered, reading.diagnostics,
            tuple(rows), slots, cloud_gate_stale_pushes=configuration.cloud_gate_stale_pushes)

    def _placement_wait_reading(self, reading: records.QueueRead) -> records.QueueRead:
        candidates = dict(reading.explanation.decided_from.placement_candidates)
        if not candidates:
            return reading
        published = {(step.pid, step.since): named
                     for step, (_, row) in zip(reading.view.waiting, reading.ordered, strict=True)
                     if (named := candidates.get(records.waiter_owner(row), ()))}

        def with_candidates(step: records.WaitingStep) -> records.WaitingStep:
            named = published.get((step.pid, step.since), ())
            return replace(step, placement_candidates=named) if named else step

        waiting = tuple(with_candidates(step) for step in reading.view.waiting)
        head, line = reading.explanation.head, reading.explanation.line
        if isinstance(head, records.HeadExplanation):
            head = replace(head, waiter=with_candidates(head.waiter))
        line = replace(line, steps=tuple(replace(entry, waiter=with_candidates(entry.waiter)) for entry in line.steps))
        return replace(reading, view=replace(reading.view, waiting=waiting),
                       explanation=replace(reading.explanation, head=head, line=line))

    def _local_placement_reading(self, reading: records.QueueRead) -> records.QueueRead:
        candidates: dict[str, tuple[placement.CandidateEligibility, ...]] = {}
        for episode, record in self.reservations.items():
            request = self.placement_requests.get(episode)
            assignment = self.assignments.get(self.current_assignments.get(episode, ""))
            if (request is not None and assignment is not None
                    and assignment.assignment.destination.provider == "local"):
                candidates[record.reservation_id] = assignment.placement_candidates or self._placement_candidate_reading(request)
        if not candidates:
            return reading
        reservations = iter(reading.explanation.decided_from.reservations)
        running: list[records.RunningStep] = []
        explained: list[records.ExplainedRunningStep] = []
        for step, entry in zip(reading.view.running, reading.explanation.running, strict=True):
            named = candidates.get(next(reservations).get("reservation_id", ""), ()) if gate.running_has_reservation(step) else ()
            running.append(replace(step, placement_candidates=named) if named else step)
            explained.append(replace(entry, step=replace(entry.step, placement_candidates=named)) if named else entry)
        return replace(reading, view=replace(reading.view, running=tuple(running)),
                       explanation=replace(reading.explanation, running=tuple(explained)))

    def _placement_reading(self, reading: records.QueueRead) -> records.QueueRead:
        reading = self._local_placement_reading(self._placement_wait_reading(reading))
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
            current = isinstance(saved.state, placement.CurrentAssignment)
            if not current and not retained:
                continue
            identity = request.identity
            guest = assignment.destination.target == "mac-linux"
            consumed = (current and self.current_assignments.get(assignment.episode) == key
                        and isinstance(saved.permission, placement.PermissionConsumed))
            confirmed = confirmed_guest_run(assignment, evidence) if consumed else None
            if (consumed and assignment.destination.target == "mac-linux"
                    and isinstance(saved.confirmed_guest_start, records.ConfirmedGuestStart)):
                confirmed = records.ConfirmedGuestRun(target=assignment.destination.target, attempt=assignment.attempt)
            local = request.local
            counter = (progress.read_counter(Path(local.sidecar))
                       if guest and consumed and isinstance(local, wire.LocalAdmissionContext) and local.sidecar
                       else progress.NoCounter())
            pid: int | records.PidNotRecorded = records.PidNotRecorded()
            if isinstance(evidence, execution_identity.ObservedExecution) and isinstance(evidence.identity, execution_identity.ProcessExecution):
                pid = evidence.identity.pid
            remote.append(records.RunningStep(identity.session_id, identity.name, identity.step or request.profile.step,
                identity.folder or request.profile.worktree, 0, records.RemoteMemoryUnavailable("remote memory is unavailable"),
                pid=pid, started_at=saved.permission.consumed_at if isinstance(saved.permission, placement.PermissionConsumed) else 0,
                unit=identity.unit, project=gate.step_project(request.profile.repo), execution=evidence,
                assignment_host=assignment.destination.host, verify=identity.verify,
                assignment_target=assignment.destination.target if guest else "",
                placement_candidates=saved.placement_candidates or self._placement_candidate_reading(request),
                confirmed_guest_run=confirmed, counter=counter,
                route=request.route if guest else records.DIRECT_CARGO, job=identity.job if guest else GATE_JOB_NOT_RECORDED,
                mac_placement=f"{assignment.episode}/{assignment.attempt}"
                    if (assignment.destination.provider == "mac" or confirmed is not None
                        or guest and consumed and assignment.episode in self.bumps.moves) else "",
                what=identity.what))
        if not remote:
            return reading
        explained = tuple(records.ExplainedRunningStep(step, records.AdmissionTimeNotRecorded(), 0, 0, 0,
            owner=records.execution_group(step, str(index))) for index, step in enumerate(remote))
        return replace(reading, view=replace(reading.view, running=(*reading.view.running, *remote)),
            explanation=replace(reading.explanation, running=(*reading.explanation.running, *explained)))

    def _local_occupancy(self, inputs: records.QueueInputs) -> records.LocalAdmissionOccupancy:
        captured = {row.get("reservation_id"): row for row in inputs.reservations}
        reservations: list[records.Reservation] = []
        for record in self.reservations.values():
            if isinstance(record.place, wire.CloudPlace):
                continue
            row = captured.get(record.reservation_id)
            if row is None:
                row = reservation_row(record)
                # Admission used the waiter owner when it granted this reservation.
                build_run = record.queue_waiter.get("build_run")
                if build_run is not None:
                    row["build_run"] = build_run
                resolve_reservation_repository(row, record)
            reservations.append(row)
        return records.LocalAdmissionOccupancy(tuple(reservations), self._verify_run_owners())

    def _project_replies(self, ordered: list[tuple[Path, records.RankedWaiter]]) -> PublishedReplies | NoPass:
        if (not isinstance(self.configuration, gate.UsableAdmissionConfiguration)
                or not isinstance(self.last_inputs, records.QueueInputs)
                or not isinstance(self.last_policy, records.AdmissionPolicy)
                or not isinstance(self.last_result, gate.PassResult)):
            return NoPass()
        hidden = self._hidden_speculative_waits()
        ordered = self._published_queue_order(ordered, hidden)
        reading = replace(gate.queue_read_from_pass(self.last_inputs, self.last_policy, self.last_result),
                          diagnostics=self.last_diagnostics)
        local = {path: replace(step, verify=row.get("verify", step.verify))
                 for (path, row), step in zip(reading.ordered, reading.view.waiting, strict=True)}
        held_reasons = {records.waiter_owner(self.waiters[episode].row): reason
                        for episode, reason in self.local_holds.items() if episode in self.waiters}
        routed = {place.move_id: request for request in self.placement_requests.values()
                  if (place := self.places.get(_place_key(request.owner, request.since))) is not None
                  and place.episode == request.episode}
        visible: list[records.WaitingStep] = []
        positions: dict[str, int] = {}
        took_places = self._took_places()
        for position, (path, row) in enumerate(ordered, 1):
            owner = records.waiter_owner(row)
            positions[owner] = position
            took_place = took_places.get(owner, "")
            row_id = row.get("move_id", "")
            restored = row_id in hidden
            if path in local and not restored:
                visible.append(replace(local[path], took_place=took_place) if took_place else local[path])
                continue
            if restored:
                visible.append(records.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
                    row.get("step", ""), row.get("folder", ""), row["since"], row["need"],
                    records.MachineChoiceWait(), row.get("unit", ""),
                    project=gate.step_project(row.get("repository_name", "")), run=row.get("build_run", NoLiveBuildRun()),
                    route=records.waiter_route(row), verify=row.get("verify", records.NotVerifyCall()),
                    job=row.get("job", GATE_JOB_NOT_RECORDED), what=row.get("what", "")))
                continue
            request = routed.get(row_id)
            candidates = self.placement_eligibility.get(request.episode, ()) if request is not None else ()
            targets = tuple(dict.fromkeys(item.destination.target for item in candidates
                if item.destination.provider != "local" and not isinstance(item.eligibility, placement.FixedExclusion)))
            reason = held_reasons.get(owner, records.ReadyForAdmission())
            if request is not None and self._local_placement_eligibility(request) == placement.TemporaryExclusion("blocked"):
                holder = self.local_hold_holders.get(request.episode)
                reason = records.NatedevHeld(holder.unit if isinstance(holder, placement.ResourceHolderUnit) else "", "")
            visible.append(records.WaitingStep(row["pid"], row.get("session_id", ""), row.get("session_name", ""),
                row.get("step", ""), row.get("folder", ""), row["since"], row["need"],
                reason, row.get("unit", ""), route=records.waiter_route(row),
                job=row.get("job", GATE_JOB_NOT_RECORDED), ci_runner=row.get("ci_runner", ""),
                ci_job_start=records.waiter_job_start(row),
                verify=row.get("verify", records.NotVerifyCall()), placement_targets=targets,
                speculative_copy=request is not None and isinstance(request.purpose, placement.SpeculativeCopy),
                took_place=took_place, what=row.get("what", "")))
        reading = replace(reading, ordered=ordered, view=replace(reading.view, waiting=tuple(visible)))
        reading = self._placement_reading(reading)
        move_line = self._current_move_line(reading.ordered, self._movable_wait_ids() - hidden)
        reading = replace(reading, view=replace(reading.view, move_line=move_line))
        reading = replace(self._cloud_reading(reading, self.configuration),
                          held_places=reading.number_held_places(self.held_places, gate.waiter_order, self.priority))
        explain = wire.ExplainReply(reading.explanation, self.last_diagnostics)
        occupancy = self._local_occupancy(self.last_inputs)
        view = wire.ViewReply(replace(reading, explanation=replace(reading.explanation, local_occupancy=occupancy)))
        view_bytes = wire.encode_reply(view).encode()
        waits = self.published.placement_waits if isinstance(self.published, PublishedReplies) else {}
        return PublishedReplies(view, explain, positions, waits, view_bytes)

    def _publish_replies(self, published: PublishedReplies | NoPass, *,
                         placement_keys: PlacementEvidenceKeys | None = None) -> None:
        if isinstance(published, NoPass):
            return
        self.published = published
        if placement_keys is not None:
            published.placement_waits = self._publish_placement_waits(placement_keys)
        assert isinstance(self.configuration, gate.UsableAdmissionConfiguration)
        config = self.configuration.config
        written_at = self.clock.now()
        longest_interval = max(config.running_pass_seconds, config.waiting_pass_seconds, config.min_pass_gap_seconds)
        try:
            queue_snapshot.write_view_snapshot(self.state_dir, written_at, written_at + longest_interval, published.view_bytes)
        except OSError:
            # Keep the last complete file when publication fails.
            pass

    def _publish_placement_waits(self, keys: PlacementEvidenceKeys) -> dict[str, PublishedPlacementWait]:
        waits: dict[str, PublishedPlacementWait] = {}
        for episode, request in self.placement_requests.items():
            digest = self.placement_request_digests.get(episode)
            if digest is None or not self._placement_waiting(request) or self._needs_mac_linux_state(request):
                continue
            reply = self._placement_reply(request,
                self.local_hold_holders.get(episode, placement.ResourceHolderUnknown()), keys, record_refusal=False)
            if isinstance(reply, wire.PlacementWait):
                waits[episode] = PublishedPlacementWait(self.input_event_count, digest, reply)
        return waits

    def _took_places(self) -> dict[str, str]:
        """Name each waiting step whose run took the place of an older run of its unit."""
        requests: list[tuple[wire.Owner | placement.SubmittingOwner, wire.RunIdentity | wire.NoRun]] = [
            (waiting.request.owner, waiting.request.run)
            for waiting in self.waiters.values() if isinstance(waiting.request, wire.AdmitRequest)]
        requests.extend((request.owner, request.run) for request in self.placement_requests.values())
        took: dict[str, str] = {}
        for owner, run in requests:
            if not isinstance(run, wire.RunIdentity):
                continue
            place = self.run_places.get(records.run_owner(run.pid, run.start_time))
            if place is not None and isinstance(line := place.unit_place, status_wire.UnitPlace) and line.took_place:
                took[records.run_owner(owner.pid, owner.start_time)] = line.took_place
        return took

    def reply_bytes(self, reply: wire.Reply, *, version: int = wire.VERSION) -> bytes:
        if version != wire.VERSION:
            return wire.encode_reply(reply, version=version).encode()
        published = self.published
        if isinstance(published, PublishedReplies):
            if reply is published.view:
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


    def _save_place(self, request: wire.AdmitRequest | wire.LaunchRequest | wire.PlacementRequest,
                    row: records.Waiter) -> None:
        if isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace):
            return
        key = _place_key(request.owner, row["since"])
        previous = self.places.get(key)
        run = request.run if isinstance(request, (wire.AdmitRequest, wire.PlacementRequest)) else wire.NO_RUN
        place = waiter_place(row, request.episode, run=run)
        if previous is not None:
            if previous.episode == request.episode:
                place = replace(place, queue_identity=previous.queue_identity)
            elif previous.move_id:
                self._depart_queue_id(previous.move_id)
        if not place.move_id:
            row["move_id"] = uuid.uuid4().hex
            place = replace(place, move_id=row["move_id"])
        self.places[key] = place

    def _set_queue_pins(self, pins: QueuePins) -> None:
        if pins != self.queue_pins:
            self.queue_pins = pins
            self.move_line = replace(self.move_line, revision=uuid.uuid4().hex)

    def _depart_queue_id(self, row_id: str) -> None:
        if not self.queue_pins:
            return
        pins = {pin.row_id: pin.anchor for pin in self.queue_pins}
        departed = QueueRowAnchor(row_id)
        anchor = pins.pop(row_id, None)
        if anchor is None:
            if departed not in pins.values():
                return
            order = [row.get("move_id", "") for _, row in self._full_queue_order(self._ranked_waiters())]
            index = order.index(row_id)
            anchor = queue_anchor_at(order, index)
        self._set_queue_pins(tuple(QueuePin(member, anchor if previous == departed else previous)
                                   for member, previous in pins.items()))

    def _end_move(self, waiting: WaitingAdmission) -> None:
        self._clear_wait_rank(waiting.request.owner, waiting.row["since"], waiting.request.episode)

    def _clear_wait_rank(self, owner: wire.Owner, since: float, episode: str) -> None:
        key = _place_key(owner, since)
        place = self.places.get(key)
        if place is not None and place.episode == episode and place.move_id:
            self._depart_queue_id(place.move_id)
            self.places[key] = replace(place, move_id="")
            if (waiting := self.waiters.get(episode)) is not None:
                _ = waiting.row.pop("move_id", None)

    def _end_placement_rank(self, request: wire.PlacementRequest) -> None:
        waiting = self.waiters.get(request.episode)
        if waiting is not None:
            self._end_move(waiting)
        else:
            self._clear_wait_rank(wire.Owner(request.owner.pid, request.owner.start_time), request.since,
                                  request.episode)


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
        queue_waiter = records.stored_waiter(waiting.row)
        _ = queue_waiter.pop("move_id", None)
        record = ReservationRecord(request.episode, str(stem), request.owner, 0 if cloud else profile.peak.bytes,
            profile.repo, profile.step, profile.worktree, store.utc_iso(now), request.sidecar,
            request.test_width, request.call, stem.name, queue_waiter,
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

    def _save_gate_rank(self, admission: ReservationRecord, move_id: str) -> None:
        run = admission.run
        row = admission.queue_waiter
        if (not move_id or not isinstance(run, wire.RunIdentity) or not isinstance(admission.place, wire.LocalPlace)
                or row.get("step") not in ("gate", "test-affected")):
            return
        key = records.run_owner(run.pid, run.start_time)
        rank = self.run_places.get(key)
        if rank is None or rank.first_owner != admission.owner or (rank.move_id and rank.repository):
            return
        line = rank.unit_place
        inherited = isinstance(line, status_wire.UnitPlace) and line.took_place != "" and line.reservations == 0
        if rank.since == row["since"] or inherited:
            self.run_places[key] = replace(rank, move_id=rank.move_id or move_id,
                                          repository=rank.repository or row.get("repository_name", admission.repo))

    def _gate_gap_start(self, admission: ReservationRecord, now: float) -> str | None:
        """Describe a local grant during an older gate's existing memory gap."""
        run = admission.run
        if not self.slots or not isinstance(run, wire.RunIdentity) or not isinstance(admission.place, wire.LocalPlace):
            return None
        current = self.run_places.get(records.run_owner(run.pid, run.start_time))
        if current is None:
            return None
        active = {record.run for record in self.reservations.values()}
        current_order = (current.since, current.first_owner.pid)
        older: wire.RunPlace | None = None
        for slot in self.slots.values():
            gap = gate.between_steps(slot)
            if gap.expires_at <= now:
                continue
            rank = self.run_places.get(records.run_owner(gap.run.pid, gap.run.start_time))
            if rank is None or not rank.move_id or not rank.repository or rank.run in active:
                continue
            order = (rank.since, rank.first_owner.pid)
            if order < current_order and (older is None or order < (older.since, older.first_owner.pid)):
                older = rank
        if older is None:
            return None
        return (f"memory admission: run {run.pid}:{run.start_time} started in the gate gap of "
                f"{older.run.pid}:{older.run.start_time} for {older.repository} on {self.host}")

    def _admit(self, waiters: list[WaitingAdmission], config: records.MemoryConfig, share_percent: int, *,
               cargo_folder: placement.CargoFolder | placement.NoCargoFolder,
               verify_slots: Mapping[str, VerifyRunSlot]) -> None:
        now = self.clock.now()
        prepared = [self._prepare_admission(waiting, now, config, share_percent, cargo_folder) for waiting in waiters]
        replies: list[wire.Admitted | wire.LaunchAdmitted] = []
        gap_starts: list[str] = []
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
                if (line := self._gate_gap_start(admission, now)) is not None:
                    gap_starts.append(line)
                self._save_gate_rank(admission, waiting.row.get("move_id", ""))
                self.reservations[request.episode] = admission
                if isinstance(request, wire.AdmitRequest) and isinstance(admission.place, wire.LocalPlace):
                    self._granted(request.run, _local_step(admission))
                slot = verify_slots.get(records.waiter_owner(waiting.row))
                if slot is not None:
                    current = self.verify_run_slots.get(slot.key)
                    if current is None:
                        self.verify_run_slots[slot.key] = replace(slot,
                            use=PendingVerifyGrants(frozenset({admission.reservation_id})))
                        self._watch_owner(slot.owner)
                    elif isinstance(current.use, PendingVerifyGrants):
                        self.verify_run_slots[slot.key] = replace(current,
                            use=PendingVerifyGrants(current.use.reservations | {admission.reservation_id}))
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
            self._save_place(request, waiting.row)
            if isinstance(admission, wire.LaunchAdmitted):
                self._end_move(waiting)
            replies.append(reply)
        first = waiters[0].request
        if (isinstance(first, wire.AdmitRequest) and isinstance(first.place, wire.CloudPlace)
                and isinstance(group := first.place.group, status_wire.CloudGroup)):
            del self.pending_cloud_groups[group.key]
            self._prune_watches()
        # Save every grant before sending any reply for the group.
        self.persist()
        for line in gap_starts:
            print(line, file=sys.stderr, flush=True)
        for waiting, reply in zip(waiters, replies, strict=True):
            self._remove_waiter(waiting, reply)

    def _end(self, episode: str, ending: ReservationEnding) -> None:
        record = self.reservations[episode]
        self.input_event_count += 1
        started = isinstance(ending, CallerRelease)
        if not started and "verify" in record.queue_waiter and record.sidecar:
            try:
                started = bool(Path(record.sidecar).read_text().strip())
            except FileNotFoundError:
                pass
        if started:
            self._finish_verify_grant(record.reservation_id, started=True)
        record = replace(record, ending=ending)
        self.reservations[episode] = record
        self.persist()
        row = reservation_row(record)
        if isinstance(ending, (OwnerExited, NotEnding, LocalGrantReturned)):
            lifetime = self._lifetime(record)
            if isinstance(lifetime, gate.PopulatedStepScope):
                self._finish_verify_grant(record.reservation_id, started=True)
                self.persist()
                self._watch_scope(lifetime, episode)
                return
            self._clear_wait_rank(record.owner, record.queue_waiter["since"], episode)
        elif isinstance(ending, CallerRelease) and episode not in self.placement_requests:
            self._clear_wait_rank(record.owner, record.queue_waiter["since"], episode)
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
            key = _place_key(record.owner, row_waiter["since"])
            place = self.places.get(key)
            if place is not None and place.episode == record.episode and place.move_id:
                row_waiter["move_id"] = place.move_id
            self.places[key] = waiter_place(row_waiter, record.episode, run=record.run)
        _ = gate.finalize_reservation(path, row, outcome, datetime.fromtimestamp(ended_at, UTC), status,
            intents=StateIntents(self.reservations, self._advance_build_history, episode, self.persist, self.fault,
                                self.places, RegisteredServiceWrites(self.writes),
                                retire_verify_grant=lambda reservation: self._finish_verify_grant(reservation, started=False)),
            end_kind=end_kind)
        if ((started or not isinstance(ending, LocalGrantReturned))
                and isinstance(record.place, wire.LocalPlace) and isinstance(record.run, wire.RunIdentity)
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
        if isinstance(ending, (ReleaseStatus, FirstStepHandOff, TokenWaiting)):
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
        if key.destination.provider in ("local", "scaler"):
            return 0.0
        if key.destination.provider == "ssh" and key.destination.target == "mac-linux":
            from scripts.build_targets.mac_linux import GUEST_REF_TIMEOUT_SECONDS

            return GUEST_REF_TIMEOUT_SECONDS
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
        self._schedule_pass()
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
        if is_mac_linux_guest(destination):
            return self._request_mac_linux_evidence(request, key)
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

    def _request_mac_linux_evidence(self, request: wire.PlacementRequest,
                                    key: placement.HostEvidenceKey) -> placement.FreshHostEvidence:
        now = self.clock.now()
        if any(_holds_consumed_guest_slot(saved) for saved in self.assignments.values()):
            return placement.FreshHostEvidence(key, now, "", placement.TemporaryExclusion("busy"))
        backoff = mac_linux_backoff(now)
        if isinstance(backoff, placement.TemporaryExclusion):
            return placement.FreshHostEvidence(key, now, "", backoff)
        self.host_requests[key] = request
        current = self.host_evidence(key)
        if (isinstance(current, placement.FreshHostEvidence) and current.observed_at > backoff.failed_at
                and (isinstance(current.eligibility, placement.AllowedHost)
                     or current.eligibility == placement.TemporaryExclusion("blocked"))):
            if isinstance(current.eligibility, placement.AllowedHost) and any(
                    is_mac_linux_guest(self.assignments[attempt].assignment.destination)
                    for episode, attempt in self.current_assignments.items() if episode != request.episode):
                return replace(current, eligibility=placement.TemporaryExclusion("busy"))
            return current
        if isinstance(self.last_usable_configuration, records.MemoryConfig):
            _ = self._host_evidence_reader(key, self.last_usable_configuration.waiting_pass_seconds)
        reason = (MAC_LINUX_NO_ANSWER if isinstance(current, placement.FreshHostEvidence)
                  and current.eligibility == placement.TemporaryExclusion(MAC_LINUX_NO_ANSWER)
                  else "mac_linux_reading_required")
        return placement.FreshHostEvidence(key, now, "", placement.TemporaryExclusion(reason))

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
            timeout = self._host_probe_timeout(key)
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

    def _begin_mac_backoff_probe(self, target: str) -> bool:
        """Allow one recovery probe per target in each waiting pass interval."""
        from scripts.mac_test import eligibility

        if not isinstance(eligibility.read_backoff(time.time(), target), eligibility.ActiveBackoff):
            return False
        with self.mac_recovery_lock:
            if self.clock.monotonic() < self.mac_recovery_retry_at.get(target, 0):
                return False
            self.mac_recovery_retry_at[target] = float("inf")
            return True

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
        if destination.provider == "scaler":
            from scripts.build_targets.gate_targets import configured_scaler_gate

            try:
                candidate = configured_scaler_gate(conf.read("build_targets"), destination.target)
                eligibility = (candidate.eligibility if (destination, key.settings) == (candidate.destination, candidate.settings)
                               else placement.FixedExclusion("The scaler differs from the configured target"))
            except conf.Unusable as error:
                eligibility = placement.FixedExclusion(str(error))
            if isinstance(eligibility, placement.AllowedHost):
                mode = SuiteMode.TARGETED if SuiteMode.step(key.step) == SuiteMode.TARGETED.value else SuiteMode.GATE
                eligibility = scaler_availability(mode)
            return placement.FreshHostEvidence(key, self.clock.now(), key.configuration_generation, eligibility)
        request = self.host_requests.get(key)
        if request is None:
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "the placement request is unavailable")
        if destination.provider not in ("mac", "ssh"):
            return placement.HostEvidenceUnavailable(key, self.clock.now(), "provider uses its existing admission path")
        if destination.provider == "ssh" and destination.target == "mac-linux":
            return self._read_mac_linux_guest(key)
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
                recover = target.platform == "macos" and self._begin_mac_backoff_probe(target.name)
                try:
                    answer = build_targets.ssh_target_eligibility(target, Path(request.constraints.repository),
                        request.constraints.package, kind, target_config.ANY_ELIGIBLE_TARGET, new_step=True,
                        session=key.session, configuration=configuration, mac_scope=destination.scope,
                        recover_mac_backoff=recover)
                finally:
                    if recover:
                        with self.mac_recovery_lock:
                            self.mac_recovery_retry_at[target.name] = self.clock.monotonic() + self._waiting_pass_interval()
            else:
                answer = build_targets.mac_target_eligibility(target, Path(request.constraints.repository),
                    request.constraints.package, kind, target_config.ANY_ELIGIBLE_TARGET, session=key.session,
                    configuration=configuration, mac_scope=destination.scope)
            eligibility = (placement.TemporaryExclusion(answer.reason)
                if isinstance(answer, target_config.ExcludedTarget) else placement.AllowedHost())
        paths = claim_state.state_paths(destination.target)
        with claim_state.locked(paths.lock):
            held, _ = claim_state.sweep_mac_replacements(paths, claim_state.read_claims(paths))
        # Routing keeps a busy or blocked Mac eligible so a step can wait for it. Placement must
        # wait too, or each pass makes a new attempt that the Mac refuses. A Mac run skips the
        # repository policy, so this check is the only one that sees a block from another session.
        # An unreadable block or claim record also waits: it can hide a block.
        if (isinstance(eligibility, placement.AllowedHost) and isinstance(target, target_config.SshTarget)
                and target.platform == "macos"):
            mac_answer = mac_test.free_now(paths, target.settings, Path(request.constraints.repository),
                                           session=key.session, configuration=configuration,
                                           mac_scope=destination.scope)
            if mac_answer in ("blocked", "busy", "state"):
                eligibility = placement.TemporaryExclusion(mac_answer)
        adopted: list[placement.AdoptedProviderClaim] = []
        cleanup: list[placement.RetainedCleanupClaim] = []
        executions: list[tuple[placement.ProviderClaimReference, execution_identity.ExecutionEvidence]] = []
        holder = placement.ResourceHolderUnknown()
        diagnostic: placement.BusyProviderDiagnostic | placement.NoProviderDiagnostic = placement.NO_PROVIDER_DIAGNOSTIC
        scope = destination.scope
        if paths.platform == "linux":
            scope = claim_state.target_claim_scope(paths, request.constraints.repository)
        for claim in held:
            if not mac_test.claim_blocks_scope(paths, claim, scope):
                continue
            if paths.platform == "macos" and (mac_test.run_is_kept(claim.record) or claim.record.get("cleanup_pending")):
                if isinstance(diagnostic, placement.NoProviderDiagnostic):
                    diagnostic = placement.BusyProviderDiagnostic(
                        destination.target, claim.record["what"] + " (kept for cleanup)", claim.record["since"])
                continue
            if mac_test.run_holds_work(claim.record):
                diagnostic = placement.BusyProviderDiagnostic(destination.target, claim.record["what"], claim.record["since"])
                session_id = claim.record.get("holder_session", "")
                if session_id and session_id != claim_state.session_id(key.session):
                    holder = placement.ResourceHolderSession(session_id)
                    break
        generation = ""
        for claim in held:
            record = claim.record
            if "remote_host" not in record or "replaced_reservation" in record:
                continue
            reference_scope = claim.scope
            if paths.platform == "macos":
                saved = self.assignments.get(execution_identity.attempt_key(record["episode"], record["attempt"]))
                if saved is not None:
                    assigned = saved.assignment.destination
                    if (assigned.provider == destination.provider and assigned.target == destination.target
                            and claim.matches_mac_assignment_scope(assigned.scope)):
                        reference_scope = assigned.scope
            reference = placement.ProviderClaimReference(destination.provider, destination.target, reference_scope,
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
                record, paths.platform, configuration)))
        return placement.FreshHostEvidence(key, self.clock.now(), generation, eligibility, tuple(adopted), tuple(cleanup), tuple(executions), holder, diagnostic)

    def _read_mac_linux_guest(self, key: placement.HostEvidenceKey) -> placement.FreshHostEvidence:
        from scripts.build_targets import mac_linux
        from scripts.mac_test import mac_test

        started_at = self.clock.now()
        backoff = mac_linux_backoff(started_at)
        if isinstance(backoff, placement.TemporaryExclusion):
            return placement.FreshHostEvidence(key, started_at, "", backoff)
        block = mac_test.blocking_claim_block(mac_test.state_paths(), key.session)
        if not isinstance(block, mac_test.NoMacBlock):
            return placement.FreshHostEvidence(key, started_at, "", placement.TemporaryExclusion("blocked"))
        try:
            _ = mac_linux.read_guest_main(mac_linux.read_settings(backoff.registry))
        except (mac_linux.GuestUnavailable, OSError, ValueError, subprocess.SubprocessError):
            return placement.FreshHostEvidence(key, started_at, "", placement.TemporaryExclusion(MAC_LINUX_NO_ANSWER))
        return placement.FreshHostEvidence(key, started_at, "", placement.AllowedHost())

    def _observe_provider_execution(self, request: wire.PlacementRequest, destination: placement.PlacementDestination,
                                    reference: placement.ProviderClaimReference, record: RemoteJobClaim,
                                    platform: Literal["linux", "macos"],
                                    config: conf.ConfigRead) -> execution_identity.ExecutionEvidence:
        """Read existing mirror process evidence without invoking provider cleanup."""
        from scripts.mac_test import eligibility, offload, claim_state

        if isinstance(eligibility.read_backoff(time.time(), destination.target), eligibility.ActiveBackoff):
            return execution_identity.ExecutionEvidenceUnavailable("provider retry backoff is active", self.clock.now())
        if platform == "macos":
            saved = claim_state.saved_mac_repository(record)
            if not isinstance(saved, claim_state.MacMirrorIdentity):
                return execution_identity.ExecutionEvidenceUnavailable("the claim has no saved Mac mirror", self.clock.now())
            repository = eligibility.KnownRepository(saved.mirror_name, saved.key)
        else:
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
        host = record["remote_host"]
        try:
            # A build target registry without a usable unreachable wait raises conf.Unusable, a ValueError.
            probe = eligibility.probe_settings(config, _provider_configuration().read("build_targets"))
            options = offload.ssh_options(probe)
            result = subprocess.run(["ssh", *options, host, "sh -c " + shlex.quote(script)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                check=False, timeout=probe.probe_timeout_s)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    pid, separator, start = line.partition("\t")
                    if separator and pid.isdecimal() and int(pid) > 0 and start.strip():
                        return execution_identity.ObservedExecution(execution_identity.ProcessExecution(
                            host, record.get("instance_generation", ""), int(pid), start.strip()))
                return execution_identity.ExecutionEvidenceUnavailable("remote execution remains unresolved", self.clock.now())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        offload.write_backoff(time.time() + config.number("unreachable_backoff_s"), destination.target)
        return execution_identity.ExecutionEvidenceUnavailable("remote execution remains unresolved", self.clock.now())

    def _prune_ended_placements(self, cutoff: float) -> None:
        """Remove the request and closed attempts of an episode whose owner ended before the cutoff."""
        last: dict[str, float] = {episode: request.since for episode, request in self.placement_requests.items()}
        active_remote: set[str] = set()
        for saved in self.assignments.values():
            episode = saved.assignment.episode
            report = saved.terminal_report
            at = report.received_at if isinstance(report, placement.TerminalAttemptReport) else saved.assignment.since
            last[episode] = max(last.get(episode, at), at)
            if _holds_consumed_guest_slot(saved) or _holds_scaler_slot(saved):
                active_remote.add(episode)
        owners = {saved.assignment.episode: saved.assignment.owner for saved in self.assignments.values()}
        owners.update((episode, request.owner) for episode, request in self.placement_requests.items())
        ended = {episode for episode, at in last.items()
                 if at < cutoff and episode not in self.current_assignments and episode not in active_remote
                 and not self._placement_owner_live(owners[episode])}
        if not ended:
            return
        self.input_event_count += 1
        self.assignments = {key: saved for key, saved in self.assignments.items()
                            if saved.assignment.episode not in ended}
        for episode in ended:
            if (request := self.placement_requests.get(episode)) is not None:
                self._end_placement_rank(request)
            _ = self.placement_requests.pop(episode, None)
            _ = self.placement_request_digests.pop(episode, None)
            self.refused_mac_only.discard(episode)
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

    def _assignment_state(self, saved: records.SavedPlacementAssignment, keys: PlacementEvidenceKeys | None = None
                          ) -> placement.AssignmentState:
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
        elif self._provider_claim_released(saved, self._provider_release_evidence(saved, keys)):
            return placement.ClosedAssignment("provider claim released")
        return saved.state

    def _provider_claim_released(self, saved: records.SavedPlacementAssignment,
                                 evidence: placement.HostEvidence | placement.EvidenceNeeded | NoSubmittedPlacementRequest) -> bool:
        """Confirm claim release under the same rules for status, replay, and settlement."""
        assignment = saved.assignment
        if (assignment.destination.provider == "scaler" or is_mac_linux_guest(assignment.destination)
                or not isinstance(saved.state, placement.CurrentAssignment)
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
        send a terminal report, and a pending start can still launch, so both keep it current until
        the owner asks again (`_recover_abandoned_attempt`).
        """
        return (not is_mac_linux_guest(saved.assignment.destination)
            and self._ended_claim_holder(saved)
            and not self._placement_owner_live(saved.assignment.owner)
            and not self._start_pending(saved))

    def _ended_claim_holder(self, saved: records.SavedPlacementAssignment) -> bool:
        return (saved.assignment.destination.provider != "local"
                and isinstance(saved.permission, placement.PermissionConsumed)
                and isinstance(saved.provider, placement.ProviderClaimReference)
                and isinstance(saved.holder, placement.ClaimHolderIdentity)
                and saved.holder.host == self.host
                and not self._live(wire.Owner(saved.holder.pid, saved.holder.start_time)))

    def _confirmed_local_holder_ended(self, holder: placement.ClaimHolderIdentity | placement.ClaimHolderUnconfirmed) -> bool:
        """Check the saved holder against a fresh process reading."""
        return (isinstance(holder, placement.ClaimHolderIdentity) and holder.host == self.host
                and not owner_may_be_live(self.proc.owner_start(holder.pid), holder.start_time))

    def _unconfirmed_holder_owner_ended(self, saved: records.SavedPlacementAssignment) -> bool:
        """A claim with no confirmed holder frees its assignment after its submitting owner ends.

        No holder process can be checked, so only fresh evidence without the claim releases it.
        Without this rule, the assignment stays current with no process behind it.
        """
        return (saved.assignment.destination.provider != "local"
                and isinstance(saved.provider, placement.ProviderClaimReference)
                and isinstance(saved.holder, placement.ClaimHolderUnconfirmed)
                and not self._placement_owner_live(saved.assignment.owner))

    def _recover_abandoned_attempt(self, request: wire.PlacementRequest) -> None:
        """Start settlement when the coordinator asks to replace a silent attempt."""
        for key, saved in self.assignments.items():
            assignment = saved.assignment
            if (assignment.episode == request.episode and assignment.owner == request.owner
                    and assignment.since == request.since and _holds_consumed_guest_slot(saved)
                    and isinstance(saved.provider, placement.ProviderClaimReference)
                    and isinstance(saved.terminal_report, placement.NoTerminalReport)
                    and self._confirmed_local_holder_ended(saved.holder)):
                _ = self._record_settlement_request(key, saved, GUEST_SETTLEMENT_REQUESTED)

        if request.episode not in self.placement_requests:
            return
        saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
        if (saved is not None and not is_mac_linux_guest(saved.assignment.destination)
                and isinstance(saved.terminal_report, placement.NoTerminalReport)
                and isinstance(saved.state, placement.CurrentAssignment)
                and self._ended_claim_holder(saved) and self._next_claim_takes_over(saved)):
            report = self._lost_report()
            self._close_for_report(replace(saved, terminal_report=report), report, persist_change=False)

    def _next_claim_takes_over(self, saved: records.SavedPlacementAssignment) -> bool:
        """A mac_test claim gives its kept claim and pending start to the next claim for its scope.

        A Hana claim does not: only recovery of the current attempt settles its started command.
        An unreadable target counts as no takeover, so the attempt stays current.
        """
        from scripts.aws_target import hana_placement

        destination = saved.assignment.destination
        if destination.provider != "ssh":
            return destination.provider == "mac"
        transport = self._ssh_transport(saved)
        return transport is not None and not isinstance(
            transport, (hana_placement.HanaControlTransport, HanaGpuNextestTransport))

    def _ssh_transport(self, saved: records.SavedPlacementAssignment) -> ExecutionTransport | None:
        """The transport of an ssh assignment's target; None when the target is not ssh or cannot be read."""
        destination = saved.assignment.destination
        submitted = self.placement_requests.get(saved.assignment.episode)
        settings = (placement.ConfiguredProviderSettings() if submitted is None
                    else self._placement_settings(submitted, destination))
        try:
            with conf.report_key_changes():
                configuration = _provider_configuration()
                target = configuration.target(destination.target, settings)
                return configuration.transport(target) if isinstance(target, target_config.SshTarget) else None
        except (OSError, ValueError):
            return None

    def _lost_report(self) -> placement.TerminalAttemptReport:
        return placement.TerminalAttemptReport(placement.AttemptDeclined("lost"), placement.DispatchPathSettled(),
                                               self.clock.now())

    def _start_pending(self, saved: records.SavedPlacementAssignment) -> bool:
        from scripts.aws_target import hana_placement
        destination = saved.assignment.destination
        if destination.provider == "scaler":
            return True
        marked_guest = is_mac_linux_guest(destination) and saved.guest_proof_required
        if destination.provider == 'ssh' and not marked_guest:
            try:
                submitted = self.placement_requests.get(saved.assignment.episode)
                transport = self._ssh_transport(saved)
                if transport is None:
                    return True
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
                                    if isinstance(outcome, BeforeLaunchRefusal)
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
        return self._pending_start_path_exists(saved, guest_state_path=marked_guest)

    def _pending_start_path_exists(self, saved: records.SavedPlacementAssignment, *, guest_state_path: bool) -> bool:
        from scripts.mac_test import claim_state

        provider = saved.provider
        if not isinstance(provider, placement.ProviderClaimReference):
            return False
        target = saved.assignment.destination.target
        identity = claim_state.ClaimIdentity(provider.claim_id, provider.episode, provider.attempt)
        with conf.report_key_changes():
            try:
                paths = (claim_state.StatePaths(target_config.ssh_target_state_directory(target), target, platform="linux")
                         if guest_state_path else claim_state.state_paths(target))
                return claim_state.pending_start_exists(paths, identity)
            except (OSError, conf.Unusable):
                return True

    def _guest_settlement_ready_at(self, saved: records.SavedPlacementAssignment,
                                   proved_at: float | None) -> float | None:
        if proved_at is None or self._pending_start_path_exists(saved, guest_state_path=True):
            return None
        # The service confirms marker clearance only when it checks the path.
        return max(proved_at, self.clock.now())

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
                for key, saved in self.assignments.items()
                if isinstance(saved.holder, placement.ClaimHolderIdentity) and saved.holder.host == self.host
                and (self.current_assignments.get(saved.assignment.episode) == key or _holds_consumed_guest_slot(saved))}

    def _placement_status(self, assignment: placement.PlacementAssignment, reason: str = "", *,
                          check_lifetime: bool = True) -> wire.DispatchStatus:
        saved = self._saved_assignment(assignment.episode, assignment.attempt)
        if isinstance(saved, status_wire.UnknownPlacementStatus):
            return wire.DispatchStatus(placement.UnknownAssignment(assignment.episode, assignment.attempt),
                placement.ClosedAssignment("unknown attempt"), placement.NoLaunchPermission("unknown attempt"), reason)
        state = self._assignment_state(saved) if check_lifetime else saved.state
        return wire.DispatchStatus(saved.assignment, state, saved.permission, reason, saved.retry)

    def _reused_guest_timeout(self, request: wire.PlacementRequest) -> str | None:
        submitted = self.placement_requests.get(request.episode)
        if submitted is None or submitted.owner != request.owner or submitted.since != request.since:
            return None
        assigned = self.assignments.get(self.current_assignments.get(request.episode, ""))
        if assigned is not None and assigned.assignment.owner == request.owner and assigned.assignment.since == request.since:
            return None
        blocking = [(key, saved) for key, saved in self.assignments.items()
                    if saved.assignment.episode == request.episode
                    and (saved.assignment.owner != request.owner or saved.assignment.since != request.since)
                    and is_mac_linux_guest(saved.assignment.destination)
                    and isinstance(saved.permission, placement.PermissionConsumed)]
        if not blocking:
            return None
        limit = _guest_wait_limit()
        deadline = submitted.since + limit if isinstance(limit, int) else submitted.since
        for key, saved in blocking:
            report = saved.terminal_report
            if isinstance(saved.state, placement.CurrentAssignment):
                now = self.clock.now()
                if now < deadline:
                    continue
                evidence_key = self.settlement_host_keys.get(key)
                reader = None if evidence_key is None else self.host_readers.get(evidence_key)
                proved_at = None if reader is None else reader.worker.observed_settlement_time(saved, key)
                ready_at = self._guest_settlement_ready_at(saved, proved_at)
                until = now if ready_at is None else ready_at
            elif (isinstance(report, placement.TerminalAttemptReport)
                  and isinstance(report.path, placement.DispatchPathSettled) and report.received_at >= request.since):
                until = report.received_at
            else:
                continue
            if until >= deadline:
                return _guest_wait_timeout(saved.assignment.attempt, limit)
        return None

    def _placement_waiting(self, request: wire.PlacementRequest) -> bool:
        if (not self._placement_owner_live(request.owner) or request.episode in self.current_assignments
                or request.episode in self.refused_mac_only
                or self._reused_guest_timeout(request) is not None):
            return False
        return not any(saved.assignment.episode == request.episode
            and (_holds_consumed_guest_slot(saved) or _holds_scaler_slot(saved)
                 or isinstance(saved.state, placement.ClosedAssignment)
                 and saved.state.reason in ("completed", "cancelled")
                 and _placement_history_applies(saved, request))
            for saved in self.assignments.values())

    def _placement_admission(self, request: wire.PlacementRequest) -> wire.AdmitRequest | wire.LaunchRequest:
        identity = request.identity
        owner = wire.Owner(request.owner.pid, request.owner.start_time)
        local = request.local
        if isinstance(local, wire.LocalAdmissionUnavailable):
            return wire.LaunchRequest(request.episode, owner, request.since, identity, request.profile)
        return wire.AdmitRequest(request.episode, owner, local.argv, local.cwd, request.since, local.sidecar,
            identity, local.call, local.test_width, request.profile, local.fresh_test, local.token_back_at, request.run,
            session=request.constraints.session)

    def _placement_row(self, request: wire.PlacementRequest) -> records.Waiter:
        admission = self._placement_admission(request)
        place = self.places.get(_place_key(admission.owner, admission.since), records.NoWaiter())
        row = self._row(admission, place)
        if isinstance(request.run, wire.RunIdentity):
            row["build_run"] = _live_run(request.run)
        row["route"] = request.route
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
                      ) -> placement.RetryDecision | placement.EvidenceNeeded | RetryAfterAvailability:
        saved = next((saved for saved in reversed(self.assignments.values())
            if saved.assignment.destination == destination and isinstance(saved.state, placement.ClosedAssignment)
            and _placement_history_applies(saved, request)), None)
        if saved is None:
            return placement.RetryAt(0)
        retry = saved.retry
        report = saved.terminal_report
        if (isinstance(retry, placement.RetryAt)
                and (destination.provider == "scaler" or is_mac_linux_guest(destination))
                and isinstance(report, placement.TerminalAttemptReport)
                and isinstance(report.outcome, placement.PrelaunchRefused)):
            return RetryAfterAvailability(report.received_at)
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

    def _candidate_permission(self, request: wire.PlacementRequest, candidate: placement.PlacementCandidate,
                              retry: placement.RetryDecision | placement.EvidenceNeeded | RetryAfterAvailability
                              ) -> placement.HostEligibility:
        destination = candidate.destination
        selection = request.constraints.selection
        if isinstance(selection, placement.PinnedHost) and selection.target != destination.target:
            return placement.FixedExclusion("another target is pinned")
        if isinstance(selection, placement.MacOnlyHost) and destination.target != "mac":
            return placement.FixedExclusion("only the Mac is selected")
        if isinstance(retry, placement.EvidenceNeeded):
            return retry
        if isinstance(retry, placement.DoNotRetry):
            return placement.FixedExclusion(retry.reason)
        if isinstance(retry, placement.RetryAt) and retry.deadline > self.clock.now():
            return placement.TemporaryExclusion("retry deadline has not passed")
        if (isinstance(candidate.eligibility, placement.AllowedHost) and is_mac_linux_guest(destination)
                and isinstance(state := self.mac_linux_state, MacLinuxStateUnavailable)):
            return placement.FixedExclusion(state.reason)
        return candidate.eligibility

    def _evidence_eligibility(self, destination: placement.PlacementDestination,
                              evidence: placement.HostEvidence | placement.EvidenceNeeded,
                              retry: placement.RetryDecision | placement.EvidenceNeeded | RetryAfterAvailability
                              ) -> placement.HostEligibility:
        if not isinstance(evidence, placement.FreshHostEvidence):
            return placement.EvidenceNeeded(evidence.reason)
        if not isinstance(evidence.eligibility, placement.AllowedHost):
            return evidence.eligibility
        if isinstance(retry, RetryAfterAvailability) and evidence.observed_at <= retry.declined_at:
            if isinstance(self.last_usable_configuration, records.MemoryConfig):
                _ = self._host_evidence_reader(evidence.key, self.last_usable_configuration.waiting_pass_seconds)
            return placement.EvidenceNeeded("provider availability predates the decline")
        if destination.provider == "scaler" and self._cloud_slots_full():
            return placement.TemporaryExclusion("cloud slots full")
        from scripts.mac_test.mac_test import scopes_overlap

        retained = tuple((attempt.provider, report.received_at) for attempt in self.assignments.values()
            for report in (attempt.terminal_report,)
            if isinstance(attempt.state, placement.ClosedAssignment)
            and isinstance(report, placement.TerminalAttemptReport)
            and isinstance(report.outcome, BeforeLaunchRefusal)
            and isinstance(attempt.provider, placement.ProviderClaimReference)
            and attempt.provider.provider == destination.provider and attempt.provider.target == destination.target
            and scopes_overlap(attempt.provider.scope, destination.scope, mac=destination.provider == "mac"))
        if any(scopes_overlap(claim.reference.scope, destination.scope, mac=destination.provider == "mac")
                and (isinstance(claim, placement.RetainedCleanupClaim)
                     or any(claim.reference == item[0] for item in retained))
                for claim in (*evidence.cleanup_claims, *evidence.claims)):
            return placement.TemporaryExclusion("busy")
        if evidence.observed_at < max((item[1] for item in retained), default=float("-inf")):
            return placement.EvidenceNeeded("provider evidence predates the retained claim report")
        return evidence.eligibility

    def _candidate_eligibility(self, request: wire.PlacementRequest, keys: PlacementEvidenceKeys
                               ) -> tuple[tuple[placement.CandidateEligibility, ...], placement.ResourceHolder]:
        result: list[placement.CandidateEligibility] = []
        local_holder: placement.ResourceHolder = placement.ResourceHolderUnknown()
        for candidate in request.constraints.candidates:
            destination = candidate.destination
            retry = self._target_retry(request, destination, keys)
            permitted = self._candidate_permission(request, candidate, retry)
            if isinstance(permitted, placement.AllowedHost):
                evidence = self._request_host_evidence(request, destination, keys)
                if destination.provider == "local" and isinstance(evidence, placement.FreshHostEvidence):
                    local_holder = evidence.holder
                permitted = self._evidence_eligibility(destination, evidence, retry)
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
                or not isinstance(saved.state, placement.CurrentAssignment)):
            return False
        if isinstance(saved.permission, placement.PermissionConsumed):
            evidence = self.execution_evidence.get(key)
            host_key = self.placement_host_keys.get((request.episode, saved.assignment.destination))
            host_evidence = self.host_evidence(host_key) if host_key is not None else None
            if (isinstance(saved.confirmed_guest_start, records.ConfirmedGuestStart)
                    or isinstance(evidence, execution_identity.ObservedExecution)
                    or isinstance(host_evidence, placement.FreshHostEvidence)
                    and any(reference == saved.provider and isinstance(execution, execution_identity.ObservedExecution)
                            for reference, execution in host_evidence.executions)):
                self._end_placement_rank(request)
                self.assignments[key] = self._end_remote_start_hold(request, saved)
                return False
            if _holds_consumed_guest_slot(saved):
                self.assignments[key] = self._end_remote_start_hold(request, saved)
                return False
        hold = saved.remote_start_hold
        if isinstance(hold, records.RemoteStartHoldEnded):
            return False
        if not self._can_hold_local_place(request):
            self.assignments[key] = self._end_remote_start_hold(request, saved)
            return False
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
        active.update(saved.assignment.episode for saved in self.assignments.values() if _holds_consumed_guest_slot(saved))
        self.placement_host_keys = {pair: key for pair, key in self.placement_host_keys.items()
                                    if pair[0] in active
                                    and (pair[1].provider != "local" or pair[0] in self.waiters
                                         or pair[0] not in self.current_assignments)}
        needed = set(self.placement_host_keys.values()) | set(self.settlement_host_keys.values())
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
                self._end_placement_rank(request)
                if saved is not None:
                    if (saved.assignment.destination.provider != "scaler"
                            and isinstance(saved.permission, placement.PermissionAvailable) and not self._start_pending(saved)):
                        self._close_placement(saved, "cancelled", placement.DoNotRetry("coordinator ended"))
                    else:
                        self.assignments[key] = self._end_remote_start_hold(request, saved)
                continue
            pending_remote = saved is not None and self._update_remote_start_hold(
                request, key, saved, remote_start_hold_seconds)
            if not pending_remote and not self._placement_waiting(request):
                continue
            candidates, local_holder = self._candidate_eligibility(request, keys)
            self.placement_eligibility[request.episode] = candidates
            self.local_hold_holders[request.episode] = local_holder
            admission = self._placement_admission(request)
            row = self._placement_row(request)
            self._save_place(request, row)
            if isinstance(admission, wire.AdmitRequest):
                prepared.add(request.episode)
                previous = self.waiters.get(request.episode)
                claim = previous.continuation_claim if previous is not None else NoContinuationClaim()
                if previous is not None and isinstance(previous.connection, Connection):
                    self._remove_waiter(previous, claim_disposition=WaiterClaimDisposition.TRANSFER)
                waiting = WaitingAdmission(admission, row, NO_CONNECTION, continuation_claim=claim)
                self.waiters[request.episode] = waiting
                self._run_place(admission, waiter_place(row, request.episode), tuple(dict.fromkeys(
                    candidate.destination.target for candidate in request.constraints.candidates
                    if not isinstance(candidate.eligibility, placement.FixedExclusion))))
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
        claim = (placement.assigned_claim_reference(assignment) if destination.provider == "scaler"
                 else placement.NoProviderClaim())
        self.assignments[key] = records.SavedPlacementAssignment(assignment, provider=claim,
            assigned_settings_generation=placement.AssignedSettingsGeneration(evidence_key.configuration_generation),
            remote_start_hold=hold, placement_candidates=self._placement_candidate_reading(request))
        self.current_assignments[request.episode] = key
        return assignment

    def _end_placement_wait(self, request: wire.PlacementRequest) -> None:
        episode = request.episode
        self._end_placement_rank(request)
        _ = self.local_hold_holders.pop(episode, None)
        for candidate in request.constraints.candidates:
            pair = (episode, candidate.destination)
            _ = self.placement_holds.pop(pair, None)
            _ = self.placement_hold_answers.pop(pair, None)
        self._remove_waiter(self.waiters.get(episode), claim_disposition=WaiterClaimDisposition.RETURN)

    def _discard_placement_request(self, request: wire.PlacementRequest) -> None:
        episode = request.episode
        self._end_placement_wait(request)
        self.refused_mac_only.discard(episode)
        _ = self.placement_requests.pop(episode, None)
        _ = self.placement_request_digests.pop(episode, None)
        _ = self.prerequisites.pop(episode, None)
        _ = self.placement_eligibility.pop(episode, None)

    def _aws_gate_spending(self) -> placement.AllowedHost | placement.TemporaryExclusion:
        """Include grants that the worker has yet to book."""
        from scripts.aws_target import ledger as aws_ledger
        from scripts.aws_target.config import account_settings_path

        try:
            directory = target_config.ssh_target_state_directory("aws").resolve()
            book = aws_ledger.CostLedger(directory)
            evidence = book.read_snapshot()
            booked = {row.attempt for row in evidence.gate_reservations if aws_ledger.gate_is_open(row)}
            values = conf.read_or_repository("cloud_gate")
            now = int(self.clock.now())
            offered = aws_ledger.gate_booking(values, "placement-preview", now, now + values.whole("lifetime_minutes") * 60)
            for key in self.current_assignments.values():
                assignment = self.assignments[key].assignment
                if assignment.destination.provider != "scaler":
                    continue
                binding = self._aws_gate_binding(assignment)
                journal = aws_ledger.GateDispatchJournal(binding.journal)
                try:
                    record = journal.read()
                except FileNotFoundError:
                    pass
                else:
                    if record.binding != binding:
                        raise ValueError("The AWS journal names a different binding")
                    if record.state != "prelaunch" or any(folder.name in booked for folder in record.folders):
                        continue
                evidence.gate_reservations.append(replace(offered, attempt=binding.claim.claim_id))
            reason = book.gate_booking_refusal(evidence, account_settings_path(), offered)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            return placement.TemporaryExclusion(f"AWS spending evidence is unavailable. {error}")
        return placement.TemporaryExclusion(reason) if reason else placement.AllowedHost()

    def _aws_gate_binding(self, assignment: placement.PlacementAssignment) -> aws_ledger.GateDispatchBinding:
        from scripts.aws_target import ledger as aws_ledger
        from scripts.aws_target.config import account_settings_path

        claim = placement.assigned_claim_reference(assignment)
        journal = self.state_dir / f"aws-gate-{claim.claim_id}.json"
        directory = target_config.ssh_target_state_directory("aws").resolve()
        return aws_ledger.GateDispatchBinding(claim, journal, directory, conf.path("build_targets").resolve(),
                                               account_settings_path().resolve())

    def _require_enabled_scaler(self, request: wire.PlacementRequest, destination: placement.PlacementDestination,
                                registry: Path) -> None:
        from scripts.build_targets.gate_targets import configured_scaler_gate

        candidate = configured_scaler_gate(conf.read(registry), destination.target)
        if not isinstance(candidate.eligibility, placement.AllowedHost):
            raise ValueError(f"The AWS scaler is {candidate.eligibility.reason}")
        if (candidate.destination, candidate.settings) != (destination, self._placement_settings(request, destination)):
            raise ValueError("The AWS scaler differs from the assigned target")

    def _publish_aws_gate_binding(self, request: wire.PlacementRequest,
                                  assignment: placement.PlacementAssignment) -> None:
        from scripts.aws_target import ledger as aws_ledger

        local = request.local
        if not isinstance(local, wire.LocalAdmissionContext) or not local.sidecar:
            raise ValueError("The AWS gate placement has no sidecar")
        binding = self._aws_gate_binding(assignment)
        self._require_enabled_scaler(request, assignment.destination, binding.registry)
        path = aws_ledger.gate_dispatch_binding_path(Path(local.sidecar))
        self.writes.require(path)
        binding.publish(path)

    def _placement_reply(self, request: wire.PlacementRequest, local_holder: placement.ResourceHolder,
                         keys: PlacementEvidenceKeys | None = None, *, record_refusal: bool = True
                         ) -> wire.Assignment | wire.LocalPlacementGranted | wire.PlacementWait | wire.DispatchStatus | wire.Refused:
        if (reason := self._reused_guest_timeout(request)) is not None:
            return wire.DispatchStatus(placement.UnknownAssignment(request.episode, ""),
                placement.ClosedAssignment(reason), placement.NoLaunchPermission(reason), reason, placement.DoNotRetry(reason))
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
        owner = records.run_owner(request.owner.pid, request.owner.start_time)
        position = self.published.wait_positions.get(owner, 0) if isinstance(self.published, PublishedReplies) else 0
        guest_waiting = False
        for key, guest in self.assignments.items():
            if guest.assignment.episode != request.episode or not is_mac_linux_guest(guest.assignment.destination):
                continue
            if _holds_consumed_guest_slot(guest):
                guest_waiting = True
                expired = self._expire_guest_replacement(guest)
                if expired is not guest:
                    self.assignments[key] = expired
                    self.persist()
                    self._schedule_pass()
                guest = expired
            report = guest.terminal_report
            if (guest.assignment.owner == request.owner and guest.assignment.since == request.since
                    and isinstance(guest.permission, placement.PermissionConsumed)
                    and isinstance(report, placement.TerminalAttemptReport)
                    and isinstance(report.outcome, placement.AttemptCancelled)):
                return self._placement_status(guest.assignment, report.outcome.reason, check_lifetime=False)
        if guest_waiting:
            return wire.PlacementWait(request.episode, request.since, position,
                self.placement_eligibility.get(request.episode, ()), provider_diagnostics=diagnostics,
                mac_disk_exclusions=disk_exclusions, retry_notices=retry_notices)
        saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
        if saved is not None:
            state = self._assignment_state(saved, keys)
            if isinstance(state, placement.ClosedAssignment) and saved.assignment.destination.provider == "local":
                return self._placement_status(saved.assignment, state.reason)
            if isinstance(state, placement.ClosedAssignment):
                # A released provider claim is final only after a pass settles the attempt and its retry.
                self._schedule_pass()
                return wire.PlacementWait(request.episode, request.since, position,
                    self.placement_eligibility.get(request.episode, ()),
                    provider_diagnostics=diagnostics, mac_disk_exclusions=disk_exclusions,
                    retry_notices=retry_notices)
            if saved.assignment.destination.provider == "local":
                replay = self.ledger[request.episode]
                assert isinstance(replay.reply, wire.Admitted)
                return wire.LocalPlacementGranted(saved.assignment, replay.reply, diagnostics, disk_exclusions, stale_hana)
            if (isinstance(saved.permission, placement.PermissionConsumed)
                    or saved.assignment.destination.provider == "scaler"
                    and isinstance(saved.terminal_report, placement.TerminalAttemptReport)):
                return wire.PlacementWait(request.episode, request.since, position,
                    self.placement_eligibility.get(request.episode, ()),
                    provider_diagnostics=diagnostics, mac_disk_exclusions=disk_exclusions,
                    retry_notices=retry_notices)
            if saved.assignment.destination.provider == "scaler":
                try:
                    self._publish_aws_gate_binding(request, saved.assignment)
                except (OSError, ValueError) as error:
                    reason = f"The AWS placement binding is unavailable. {error}"
                    self._close_placement(saved, "cancelled", placement.DoNotRetry(reason))
                    return self._placement_status(saved.assignment, reason, check_lifetime=False)
            if (is_mac_linux_guest(saved.assignment.destination)
                    and isinstance(setup := self.mac_linux_state, MacLinuxStateUnavailable)):
                return wire.Refused(setup.reason)
            return wire.Assignment(saved.assignment, diagnostics, disk_exclusions, stale_hana)
        terminal = [saved for saved in self.assignments.values() if isinstance(saved.state, placement.ClosedAssignment)
                    and saved.state.reason in ("completed", "cancelled")
                    and _placement_history_applies(saved, request)]
        if terminal:
            saved = terminal[-1]
            return self._placement_status(saved.assignment,
                saved.retry.reason if isinstance(saved.retry, placement.DoNotRetry) else "")
        candidates = self.placement_eligibility.get(request.episode, ())
        selection = request.constraints.selection
        if isinstance(selection, placement.MacOnlyHost):
            refusal = next((candidate.eligibility.reason for candidate in candidates
                if candidate.destination.target == "mac"
                and isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion))
                and candidate.eligibility.reason != "busy"), "")
            if refusal:
                refused = wire.DispatchStatus(placement.UnknownAssignment(request.episode, ""),
                    placement.ClosedAssignment(refusal), placement.NoLaunchPermission(refusal),
                    refusal, placement.DoNotRetry(refusal))
                if record_refusal and request.episode not in self.refused_mac_only:
                    self._end_placement_wait(request)
                    self.refused_mac_only.add(request.episode)
                    self._prune_host_evidence()
                    self._schedule_pass()
                    self.persist()
                    self._publish_replies(self._project_replies(self._queue_order()))
                return refused
        if isinstance(selection, placement.PinnedHost):
            selected = tuple(candidate for candidate in candidates if candidate.destination.target == selection.target)
            if selected and all(isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion))
                    and isinstance(placement.retry_decision(candidate.destination, candidate.eligibility.reason, self.clock.now()),
                        placement.DoNotRetry) for candidate in selected):
                reason = "; ".join(candidate.eligibility.reason for candidate in selected
                    if isinstance(candidate.eligibility, (placement.FixedExclusion, placement.TemporaryExclusion)))
                return wire.DispatchStatus(placement.UnknownAssignment(request.episode, ""), placement.ClosedAssignment(reason),
                    placement.NoLaunchPermission(reason), reason, placement.DoNotRetry(reason))
        if candidates and all(isinstance(candidate.eligibility, placement.FixedExclusion) for candidate in candidates):
            reason = "; ".join(candidate.eligibility.reason for candidate in candidates
                if isinstance(candidate.eligibility, placement.FixedExclusion))
            return wire.DispatchStatus(placement.UnknownAssignment(request.episode, ""), placement.ClosedAssignment(reason),
                placement.NoLaunchPermission(reason), reason, placement.DoNotRetry(reason))
        waiting = self.waiters.get(request.episode)
        wait = waiting.last_reply if waiting is not None else NO_WAIT_REPLY
        if isinstance(wait, wire.Wait):
            if wait.position != position:
                wait = replace(wait, position=position)
            reason = wait.reason
            cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
            if isinstance(cause, placement.CargoFolderWait | records.BuildSpaceHold | records.EstimateExpired | records.RunLimitWait):
                return wire.PlacementWait(request.episode, request.since, position, candidates,
                    wait, diagnostics, disk_exclusions, retry_notices)
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
        if isinstance(wait, wire.Wait):
            return wire.PlacementWait(request.episode, request.since, position,
                candidates, wait, diagnostics, disk_exclusions, retry_notices)
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

    def _placement_candidate_reading(self, request: wire.PlacementRequest) -> tuple[placement.CandidateEligibility, ...]:
        """Keep the named destinations in order and give their plain reasons."""
        candidates = self.placement_eligibility.get(request.episode)
        if candidates is None:
            candidates = tuple(placement.CandidateEligibility(candidate.destination,
                placement.EvidenceNeeded("the target reading is unavailable")
                    if isinstance(candidate.eligibility, placement.AllowedHost) else candidate.eligibility)
                for candidate in request.constraints.candidates)
        return tuple(replace(candidate, eligibility=replace(state, reason=reason))
                     if not isinstance(state := candidate.eligibility, placement.AllowedHost)
                     and (reason := PLACEMENT_REASON_TEXT.get(state.reason)) is not None else candidate
                     for candidate in candidates)

    def _placement_pass(self, inputs: records.QueueInputs, ordered: list[tuple[Path, records.RankedWaiter]],
                        policy: records.AdmissionPolicy,
                        remote_start_hold_seconds: int, keys: PlacementEvidenceKeys) -> records.QueueInputs:
        """Keep pending remote starts in the local order until their holds end."""
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
            unchecked = (isinstance(prerequisite, placement.TokenRequired)
                         and isinstance(prerequisite.readiness, placement.TokenReadinessUnchecked))
            if unchecked:
                row = row.copy()
                _ = row.pop("token_left_at", None)
                _ = row.pop("token_back_at", None)
            preview_ordered.append((path, row))
        preview = gate.admission_pass(replace(inputs, ordered=preview_ordered), inputs.now, policy)
        local_starts = {row.owner for row in preview.rows if isinstance(row.admission, gate.ProposedAdmission)}
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
        holds: dict[tuple[str, placement.PlacementDestination], PlacementHoldStart] = {}
        answers: dict[tuple[str, placement.PlacementDestination], placement.HostEvidence] = {}
        for _, row in ordered:
            owner = records.waiter_owner(row)
            request = owners.get(owner)
            if request is None:
                continue
            prerequisite = self.prerequisites.get(request.episode)
            initial_readiness_unchecked = (isinstance(prerequisite, placement.TokenRequired)
                and isinstance(prerequisite.readiness, placement.TokenReadinessUnchecked))
            waiting_for: list[PlacementHold] = []
            candidates = self.placement_eligibility.get(request.episode, ())
            for index, candidate in enumerate(candidates):
                destination = candidate.destination
                if destination.provider == "local" and request.constraints.cargo_folder in local_occupied:
                    continue
                if (initial_readiness_unchecked and destination.provider == "local"
                        and isinstance(inputs.build_space, records.NoBuildSpaceHold)
                        and isinstance(candidate.eligibility, placement.EvidenceNeeded)):
                    break
                if not isinstance(candidate.eligibility, placement.AllowedHost):
                    reading = self._mac_hold_reading(request, candidate)
                    if (isinstance(reading, placement.FreshHostEvidence)
                            and isinstance(reading.eligibility, placement.AllowedHost)
                            and self.clock.now() - reading.observed_at <= self._evidence_lifetime(reading.key)):
                        retry = self._target_retry(request, destination, keys)
                        if isinstance(self._candidate_permission(request, request.constraints.candidates[index], retry),
                                      placement.AllowedHost):
                            candidate = replace(candidate, eligibility=self._evidence_eligibility(destination, reading, retry))
                            candidates = (*candidates[:index], candidate, *candidates[index + 1:])
                            self.placement_eligibility[request.episode] = candidates
                    if not isinstance(candidate.eligibility, placement.AllowedHost):
                        hold = self._placement_hold(request, candidate, answers, reading=reading)
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
                    if self._cloud_slots_full():
                        continue
                    spending = self._aws_gate_spending()
                    if isinstance(spending, placement.TemporaryExclusion):
                        self.placement_eligibility[request.episode] = tuple(
                            replace(item, eligibility=spending) if item.destination == destination else item
                            for item in self.placement_eligibility[request.episode])
                        continue
                elif (any(same_remote_resource(other, destination) for other in taken)
                        or self._held_for_older(request, destination, held, holds)):
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
                    pair = (hold.episode, hold.destination)
                    if not hold.answered and (start := self.placement_holds.get(pair)) is not None:
                        holds[pair] = start
        self.placement_holds = holds
        self.placement_hold_answers = answers
        local_order = (inputs.waiter_orders.keys() | pending_remote_starts) - removed
        named = {owner: candidates for owner, request in owners.items()
                 if self._placement_waiting(request) and (candidates := self._placement_candidate_reading(request))}
        return replace(inputs, ordered=[(path, row) for path, row in ordered
            if records.waiter_owner(row) in local_order], slots=tuple(self.slots.values()), cargo_folders=cargo_folders,
            pending_remote_starts=frozenset(pending_remote_starts), placement_candidates=named)

    def _mac_hold_reading(self, request: wire.PlacementRequest,
                          candidate: placement.CandidateEligibility) -> HostObservation | HoldReadingNotSupplied:
        """Read the Mac evidence once for eligibility and the hold."""
        destination = candidate.destination
        if (destination.provider != "mac" or not isinstance(request.purpose, placement.NormalStep)
                or not (isinstance(candidate.eligibility, placement.EvidenceNeeded)
                        or _hold_exclusion(request, destination, candidate.eligibility))):
            return HOLD_READING_NOT_SUPPLIED
        key = self.placement_host_keys.get((request.episode, destination))
        reader = self.host_readers.get(key) if key is not None else None
        return reader.worker.observation(reader.key) if reader is not None else NoHostObservation()

    def _placement_hold(self, request: wire.PlacementRequest, candidate: placement.CandidateEligibility,
                        answers: dict[tuple[str, placement.PlacementDestination], placement.HostEvidence], *,
                        reading: HostObservation | HoldReadingNotSupplied = HOLD_READING_NOT_SUPPLIED
                        ) -> PlacementHold | None:
        """Keep a remote destination for one probe of an older request."""
        destination = candidate.destination
        eligibility = candidate.eligibility
        pair = (request.episode, destination)
        key = self.placement_host_keys.get(pair)
        if (key is None or not isinstance(request.purpose, placement.NormalStep)
                or destination.provider in ("local", "scaler")):
            return None
        reader = self.host_readers.get(key)
        if reader is None:
            return None
        latest = reader.worker.observation(key) if isinstance(reading, HoldReadingNotSupplied) else reading
        observed_at = self.clock.now() if isinstance(latest, NoHostObservation) else latest.observed_at
        start = self.placement_holds.get(pair)
        answer = self.placement_hold_answers.get(pair)
        mac = destination.provider == "mac"
        # A Mac probe can publish after the hold starts with an earlier observation time.
        if (start is not None and not isinstance(latest, NoHostObservation)
                and (latest is not start.reading if mac else observed_at >= start.started_at)):
            answer = latest
        answered = (answer is not None and
                    (answer is latest if mac else answer.observed_at == observed_at))
        # Keep a Mac answer in the order even when it cannot start a hold.
        if not (mac and answered):
            if not (isinstance(eligibility, placement.EvidenceNeeded)
                    or _hold_exclusion(request, destination, eligibility)):
                return None
            if (isinstance(latest, placement.FreshHostEvidence)
                    and not isinstance(latest.eligibility, placement.AllowedHost)
                    and not _hold_exclusion(request, destination, latest.eligibility)):
                return None
        if answer is not None:
            answers[pair] = answer
        if answered and not mac:
            return None
        return PlacementHold(request.episode, destination, key, observed_at, latest, answered)

    def _held_for_older(self, request: wire.PlacementRequest, destination: placement.PlacementDestination,
                        held: list[PlacementHold],
                        holds: dict[tuple[str, placement.PlacementDestination], PlacementHoldStart]) -> bool:
        """Hold the destination until the older request's probe answers or reaches its time limit."""
        key = self.placement_host_keys.get((request.episode, destination))
        observed_at = self.host_evidence(key).observed_at if key is not None else self.clock.now()
        now = self.clock.now()
        for hold in held:
            if not same_remote_resource(hold.destination, destination):
                continue
            if hold.answered or hold.observed_at >= observed_at:
                if destination.provider == "mac":
                    return False
                continue
            reader = self.host_readers.get(hold.key)
            if reader is None:
                continue
            pair = (hold.episode, hold.destination)
            start = holds.get(pair)
            if start is None:
                start = PlacementHoldStart(now, hold.reading)
                holds[pair] = start
                reader.worker.expedite(hold.key)
            active = now - start.started_at <= self._waiting_pass_interval() + reader.timeout
            if active or destination.provider == "mac":
                return active
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
        if (is_mac_linux_guest(assignment.destination)
                and isinstance(setup := self.mac_linux_state, MacLinuxStateUnavailable)):
            return replace(self._placement_status(assignment, setup.reason),
                           permission=placement.NoLaunchPermission(setup.reason))
        if is_mac_linux_guest(assignment.destination) and any(
                _holds_consumed_guest_slot(other) for other in self.assignments.values()):
            return self._placement_status(assignment, "the Mac Linux slot is busy")
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
        from scripts.build_queue import mac_run
        from scripts.build_targets import mac_linux
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
        if destination.provider == "scaler":
            from scripts.aws_target import ledger as aws_ledger

            binding = self._aws_gate_binding(assignment)
            if reference != binding.claim:
                return self._placement_status(assignment, "the exact AWS claim does not match")
            try:
                self._require_enabled_scaler(submitted, destination, binding.registry)
                record = aws_ledger.GateDispatchJournal(binding.journal).require_open(reference, request.holder)
                if record.binding != binding:
                    raise ValueError("The AWS journal names a different binding")
            except (OSError, ValueError) as error:
                return self._placement_status(assignment, f"AWS prelaunch evidence is unavailable. {error}")
        elif isinstance(transport, hana_placement.HanaControlTransport):
            from scripts.aws_target.hana_placement_adapter import read_assigned_adoption

            adopted = read_assigned_adoption(assignment, request.holder, transport)
            if isinstance(adopted, hana_placement.HanaClaimRefused):
                return self._placement_status(assignment, adopted.reason)
            if adopted.reference != reference:
                return self._placement_status(assignment, "the exact claim is not adopted")
        elif destination.target == mac_linux.TARGET:
            # The guest probe reads no claims. The gate holds the Mac claim of its guest run on this host.
            try:
                held = mac_run.claim_held(mac_linux.GUEST_CLAIM_TARGET, reference.claim_id,
                                          request.holder.pid, request.holder.start_time)
            except (OSError, ValueError) as error:
                return self._placement_status(assignment, f"adoption evidence is unavailable: {error}")
            if not held:
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
        saved = replace(saved, permission=placement.PermissionConsumed(self.clock.now()), provider=reference,
                        holder=request.holder, guest_proof_required=is_mac_linux_guest(destination) and request.guest_proof_required)
        self.assignments[key] = saved
        # A speculative copy repeats a step of the run, so it is not a step of its own.
        if destination.provider != "local" and not isinstance(submitted.purpose, placement.SpeculativeCopy):
            self._granted(submitted.run, _placed_step(submitted))
        self._watch_owner(wire.Owner(request.holder.pid, request.holder.start_time))
        status = wire.DispatchStatus(assignment, saved.state, saved.permission)
        self.ledger["dispatch:" + key] = ReplayEntry(status, LiveGrant())
        self._schedule_pass()
        self.persist()
        if destination.target == mac_linux.TARGET:
            self.pass_stale = True
        return wire.FirstAuthorization(assignment, reference)

    def _guest_took_no_run(self, saved: records.SavedPlacementAssignment, request: wire.ClaimFailure) -> bool:
        """A guest gate that consumed its launch permission took no run and ended.

        The no-run report can close a legacy attempt if no start is pending.
        A fresh process read checks the gate because the last system reading can still show it.
        """
        return (saved.assignment.destination.target == "mac-linux"
                and isinstance(saved.permission, placement.PermissionConsumed)
                and isinstance(request.claim, placement.NoProviderClaim) and request.reason == MAC_LINUX_NO_ANSWER
                and not saved.guest_execution_observed
                and isinstance(saved.confirmed_guest_start, records.GuestStartNotRecorded)
                and self._confirmed_local_holder_ended(saved.holder))

    def _holder_matches(self, owner: placement.SubmittingOwner, holder: placement.ClaimHolderIdentity) -> bool:
        if holder.host != self.host:
            return False
        processes = (dict(self.system_reader.latest.processes) if not isinstance(self.system_reader, NoSystemReader)
                     else self.proc.processes())
        listed = processes.get(holder.pid)
        if listed is None or listed.start != holder.start_time:
            # A holder that started after the last system reading is not in it, or its pid then named
            # another process. So read the processes now.
            processes = self.proc.processes()
            if self.proc.owner_start(holder.pid) != LiveOwnerIdentity(holder.start_time):
                return False
        elif not self._live(wire.Owner(holder.pid, holder.start_time)):
            return False
        if holder.pid == owner.pid and holder.start_time == owner.start_time:
            return True
        return holder.pid in gate.process_tree({owner.pid: owner.start_time}, processes)

    def _close_placement(self, saved: records.SavedPlacementAssignment, reason: str,
                         retry: placement.RetryDecision, *, persist_change: bool = True) -> None:
        self._schedule_pass()
        assignment = saved.assignment
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        current = self.current_assignments.get(assignment.episode) == key
        request = self.placement_requests.get(assignment.episode)
        if current:
            self._remove_waiter(self.waiters.get(assignment.episode))
            if request is not None:
                saved = self._end_remote_start_hold(request, saved)
                if reason in ("cancelled", "completed"):
                    self._end_placement_rank(request)
            del self.current_assignments[assignment.episode]
        self.assignments[key] = replace(saved, state=placement.ClosedAssignment(reason), retry=retry)
        self.manual_guest_settlement.discard(key)
        _ = self.settlement_host_keys.pop(key, None)
        destination = assignment.destination
        worker = self.host_targets.get((destination.provider, destination.target))
        if worker is not None:
            worker.forget_dispatch(key)
        if current and request is not None and self._placement_waiting(request):
            self._save_place(request, self._placement_row(request))
        self._prune_host_evidence()
        if persist_change:
            self.persist()

    def _settle_hana_prelaunch(self, saved: records.SavedPlacementAssignment,
                               report: placement.TerminalAttemptReport) -> bool:
        """Close a Hana attempt when its journal confirms release before launch."""
        assignment = saved.assignment
        if (assignment.destination.provider != "ssh"
                or not isinstance(report.outcome, BeforeLaunchRefusal | placement.AttemptCancelled)
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
            if _holds_consumed_guest_slot(saved):
                self._settle_guest_attempt(saved)
                continue
            if _holds_scaler_slot(saved):
                _ = self._settle_aws_dispatch(saved, persist_change=False)
                continue
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
                report = self._lost_report()
                saved = replace(saved, terminal_report=report)
            self._close_for_report(saved, report)

    def _settle_aws_dispatch(self, saved: records.SavedPlacementAssignment, *,
                             persist_change: bool = True) -> bool:
        """Close an AWS assignment when its journal proves settlement."""
        from scripts.aws_target import ledger as aws_ledger

        assignment = saved.assignment
        binding = self._aws_gate_binding(assignment)
        journal = aws_ledger.GateDispatchJournal(binding.journal)
        report = saved.terminal_report
        holder = saved.holder
        try:
            if isinstance(saved.permission, placement.PermissionAvailable):
                owner = assignment.owner
                if (isinstance(report, placement.NoTerminalReport)
                        and (owner.host != self.host
                             or owner_may_be_live(self.proc.owner_start(owner.pid), owner.start_time))):
                    return False
                try:
                    record = journal.read()
                except FileNotFoundError:
                    holder = placement.ClaimHolderIdentity(owner.host, owner.pid, owner.start_time)
                    journal.begin(binding, holder)
                else:
                    holder = record.holder
            elif saved.provider != binding.claim:
                return False
            if not isinstance(holder, placement.ClaimHolderIdentity) or holder.host != self.host:
                return False
            if (isinstance(report, placement.NoTerminalReport)
                    and owner_may_be_live(self.proc.owner_start(holder.pid), holder.start_time)):
                return False
            record = journal.read()
            if record.binding != binding:
                return False
        except (OSError, ValueError):
            return False
        settlement = journal.settle(binding.claim, holder)
        if isinstance(settlement, placement.DispatchPathPending):
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            settled_holder = holder

            def read_dispatch() -> placement.DispatchPathState:
                observed = journal.settle(binding.claim, settled_holder)
                if not isinstance(observed, placement.DispatchPathPending):
                    return placement.DispatchPathSettled()
                reconcile = getattr(scaler_provider(), "reconcile_dispatch", None)
                if callable(reconcile) and not self.closed and self.current_assignments.get(assignment.episode) == key:
                    _ = cast(Callable[[aws_ledger.GateDispatchBinding], None], reconcile)(binding)
                return observed

            probe = self._dispatch_settlement_probe(saved, read_dispatch)
            if probe is not None:
                _ = probe[0].dispatch_pending(probe[1])
            return False
        path = placement.DispatchPathSettled()
        if (isinstance(report, placement.TerminalAttemptReport)
                and (isinstance(report.outcome, placement.AttemptCompleted)
                     or settlement == "no launch" and isinstance(report.outcome, placement.PrelaunchRefused))):
            terminal = replace(report, path=path)
        elif (settlement == "no launch" and isinstance(report, placement.TerminalAttemptReport)
                and isinstance(report.outcome, placement.AttemptDeclined)):
            terminal = replace(report, outcome=placement.PrelaunchRefused(report.outcome.reason), path=path)
        else:
            terminal = placement.TerminalAttemptReport(placement.AttemptCancelled(
                "The AWS gate ended without a verdict" if settlement == "launch ended" else "no launch"),
                path, self.clock.now())
        self._close_for_report(replace(saved, terminal_report=terminal), terminal, persist_change=persist_change)
        return True

    def _request_settlement(self, request: wire.SettlementRequest) -> wire.Ok | wire.Refused:
        assignment = request.assignment
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        saved = self.assignments.get(key)
        scaler = assignment.destination.provider == "scaler"
        if (not (scaler or is_mac_linux_guest(assignment.destination))
                or saved is None or saved.assignment != assignment):
            return wire.Refused("settlement requires the exact scaler or Mac Linux assignment")
        if not isinstance(saved.permission, placement.PermissionConsumed):
            return wire.Refused("settlement requires consumed launch permission")
        if isinstance(saved.state, placement.ClosedAssignment):
            return wire.Ok()
        if self.current_assignments.get(assignment.episode) != key:
            return wire.Refused("the attempt is not current")
        outcome = SCALER_SETTLEMENT_REQUESTED if scaler else GUEST_SETTLEMENT_REQUESTED
        saved = self._record_settlement_request(key, saved, outcome)
        if scaler:
            _ = self._settle_aws_dispatch(saved)
        self._schedule_pass()
        return wire.Ok()

    def _record_settlement_request(self, key: str, saved: records.SavedPlacementAssignment,
                                   outcome: placement.AttemptDeclined) -> records.SavedPlacementAssignment:
        if not isinstance(saved.terminal_report, placement.NoTerminalReport):
            return saved
        terminal = placement.TerminalAttemptReport(outcome,
            placement.DispatchPathPending("launch settlement is pending"), self.clock.now())
        saved = replace(saved, terminal_report=terminal)
        self.assignments[key] = saved
        self.persist()
        self._schedule_pass()
        return saved

    def _settle_guest_attempt(self, saved: records.SavedPlacementAssignment) -> None:
        assignment = saved.assignment
        episode = assignment.episode
        attempt_key = execution_identity.attempt_key(episode, assignment.attempt)

        def holds_slot() -> bool:
            active = self.assignments.get(attempt_key)
            return (not self.closed and attempt_key not in self.manual_guest_settlement
                    and active is not None and _holds_consumed_guest_slot(active))

        def read_dispatch() -> placement.DispatchPathState:
            holder = saved.holder
            legacy_holder_ended = not saved.guest_proof_required and isinstance(holder, placement.ClaimHolderIdentity) and all(
                identity.host == self.host
                and not owner_may_be_live(self.proc.owner_start(identity.pid), identity.start_time)
                for identity in (holder, assignment.owner))
            return guest_settlement(episode, placement.scaler_attempt_number(assignment.attempt),
                holds_slot=holds_slot,
                legacy_holder_ended=legacy_holder_ended)

        probe = self._dispatch_settlement_probe(saved, read_dispatch)
        proved_at = None if probe is None else probe[0].dispatch_settled_at(probe[1])
        settled_at = self._guest_settlement_ready_at(saved, proved_at)
        if settled_at is None:
            self.assignments[attempt_key] = self._expire_guest_replacement(saved)
            return
        expired = self._expire_guest_replacement(saved, checked_at=settled_at)
        report = expired.terminal_report
        terminal = (replace(report, path=placement.DispatchPathSettled())
                    if isinstance(report, placement.TerminalAttemptReport) else self._lost_report())
        # Keep the settlement time for a reused submission after the worker forgets the attempt.
        submitted = self.placement_requests.get(episode)
        saved_terminal = (replace(terminal, received_at=settled_at) if submitted is not None
                          and (assignment.owner != submitted.owner or assignment.since != submitted.since) else terminal)
        self._close_for_report(replace(expired, terminal_report=saved_terminal), terminal, persist_change=False)

    def _expire_guest_replacement(self, saved: records.SavedPlacementAssignment, *,
                                  checked_at: float | None = None) -> records.SavedPlacementAssignment:
        report = saved.terminal_report
        if (not isinstance(report, placement.TerminalAttemptReport)
                or not isinstance(report.outcome, placement.AttemptDeclined | placement.PrelaunchRefused)):
            return saved
        limit = _guest_wait_limit()
        now = self.clock.now() if checked_at is None else checked_at
        if isinstance(limit, int) and now < report.received_at + limit:
            return saved
        reason = _guest_wait_timeout(saved.assignment.attempt, limit)
        return replace(saved, terminal_report=replace(report, outcome=placement.AttemptCancelled(reason)),
                       retry=placement.DoNotRetry(reason))

    def _dispatch_settlement_probe(self, saved: records.SavedPlacementAssignment,
                                   read: Callable[[], placement.DispatchPathState],
                                   ) -> tuple[HostEvidenceWorker, DispatchEvidenceProbe] | None:
        """Retain the attempt's probe until closure, even if its host evidence key changes."""
        if not isinstance(self.last_usable_configuration, records.MemoryConfig):
            return None
        assignment = saved.assignment
        attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        key = self.settlement_host_keys.get(attempt)
        if key is None:
            submitted = self.placement_requests.get(assignment.episode)
            evidence_key = self._placement_evidence_key(submitted, assignment.destination) if submitted is not None else None
            if isinstance(evidence_key, placement.HostEvidenceKey) and submitted is not None:
                key = evidence_key
                self.host_requests[key] = submitted
            else:
                key = placement.HostEvidenceKey(assignment.destination, assignment.session, (), "")
            self.settlement_host_keys[attempt] = key
        reader = self._host_evidence_reader(key, self.last_usable_configuration.waiting_pass_seconds)
        return reader.worker, DispatchEvidenceProbe(key, saved, read)

    def _close_for_report(self, saved: records.SavedPlacementAssignment, report: placement.TerminalAttemptReport, *,
                          persist_change: bool = True) -> None:
        outcome = report.outcome
        assignment = saved.assignment
        if (assignment.destination.provider == "local" and isinstance(outcome, placement.PrelaunchRefused)
                and (reservation := self.reservations.get(assignment.episode)) is not None):
            with retry_cleanup(Path(reservation.path)):
                self._end(assignment.episode, LocalGrantReturned())
            if assignment.episode in self.reservations:
                return
        if isinstance(outcome, placement.AttemptCompleted):
            submitted = self.placement_requests.get(assignment.episode)
            if (submitted is not None and assignment.destination.provider != "local"
                    and submitted.owner == assignment.owner and submitted.since == assignment.since):
                if not isinstance(submitted.purpose, placement.SpeculativeCopy):
                    self._released(submitted.run, _placed_step(submitted), outcome.status)
                elif outcome.status == 0:
                    self._copy_passed(submitted.run)
            self._close_placement(saved, "completed", placement.DoNotRetry("command result is final"),
                                  persist_change=persist_change)
        elif isinstance(outcome, placement.AttemptCancelled):
            self._close_placement(saved, "cancelled", placement.DoNotRetry(outcome.reason),
                                  persist_change=persist_change)
        elif isinstance(outcome, execution_identity.HanaCommandAdmissionRefused):
            self._close_placement(saved, outcome.reason,
                placement.retry_decision(saved.assignment.destination, outcome, report.received_at),
                persist_change=persist_change)
        elif not self._settle_hana_prelaunch(saved, report):
            self._close_placement(saved, outcome.reason,
                saved.retry if isinstance(saved.retry, placement.RetryAt) else
                placement.retry_decision(saved.assignment.destination, outcome.reason, report.received_at),
                persist_change=persist_change)

    def _handle_placement(self, request: wire.PlacementRequest | wire.ConsumePermission | wire.ClaimFailure | wire.ExecutionReport
                          | wire.PrerequisiteReport | wire.CancelPlacement,
                          connection: Connection | NoConnection = NO_CONNECTION) -> list[wire.Reply]:
        if isinstance(request, wire.ConsumePermission):
            return [self._consume_permission(request)]
        if isinstance(request, (wire.PrerequisiteReport, wire.CancelPlacement)):
            submitted = self.placement_requests.get(request.episode)
            if submitted is None:
                key = self._placement_replacement_key(request.episode, request.owner)
                replay = self.placement_replacements.get(key)
                config = self.last_usable_configuration
                if (replay is not None and not isinstance(replay.ended_at, LiveGrant) and isinstance(config, records.MemoryConfig)
                        and replay.ended_at > self.clock.now() - config.replay_retention_seconds):
                    return [replay.reply]
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
                if isinstance(connection, Connection):
                    self._record_caller_version(request, connection, persist_change=False)
                saved = self.assignments.get(self.current_assignments.get(request.episode, ""))
                if saved is not None and (isinstance(saved.permission, placement.PermissionAvailable)
                        or saved.assignment.destination.provider == "local"):
                    if saved.assignment.destination.provider == "local" and request.episode in self.reservations:
                        try:
                            self._end(request.episode, LocalGrantReturned())
                        except (OSError, ValueError, TypeError, KeyError) as error:
                            return [wire.Refused(f"cannot return the local grant: {error}")]
                    report = saved.terminal_report
                    if (self._start_pending(saved) or (isinstance(report, placement.TerminalAttemptReport)
                            and isinstance(report.path, placement.DispatchPathPending))):
                        key = execution_identity.attempt_key(saved.assignment.episode, saved.assignment.attempt)
                        self.assignments[key] = replace(saved, terminal_report=placement.TerminalAttemptReport(
                            placement.AttemptCancelled(request.reason), placement.DispatchPathPending('provider settlement remains pending'),
                            self.clock.now()))
                    else:
                        self._close_placement(saved, "cancelled", placement.DoNotRetry(request.reason), persist_change=False)
                elif saved is None:
                    self._discard_placement_request(submitted)
                self._prune_host_evidence()
            self._schedule_pass()
            self.persist()
            return [wire.Ok()]
        if isinstance(request, wire.PlacementRequest):
            digest = connection.placement_request_digest if isinstance(connection, Connection) else None
            if digest is None:
                digest = hashlib.sha256(wire.encode_request(request).encode().removesuffix(b"\n")).digest()
            _ = self._configuration_changed()
            if (isinstance(self.pending_configuration, NoPass)
                    and request.episode not in self.current_assignments
                    and request.episode not in self.reservations
                    and isinstance(self.published, PublishedReplies)
                    and (published := self.published.placement_waits.get(request.episode)) is not None
                    and published.input_event_count == self.input_event_count
                    and published.request_digest == digest):
                return [published.reply]
            if self._replaced(request.run):
                return [wire.Refused(status_wire.REPLACED_REASON)]
            previous = self.placement_requests.get(request.episode)
            identity = self._placement_identity(request)
            members = records.queue_staff() if identity.session_id else records.NO_STAFF_LOOKUP
            request = replace(request, identity=self._named_identity(identity, members=members))
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
            if previous is not None and (previous.owner != request.owner or previous.since != request.since
                    or previous.run != request.run or previous.purpose != request.purpose):
                return [wire.Refused("placement episode changed its owner, start, run or purpose")]
            if previous is not None:
                request = replace(request, constraints=episode_constraints(previous.constraints, request.constraints))
                if request.episode in self.refused_mac_only:
                    self.refused_mac_only.remove(request.episode)
                    _ = self.placement_eligibility.pop(request.episode, None)
            elif (isinstance(folder := request.constraints.cargo_folder, placement.CargoFolder)
                    and any(candidate.destination.provider == "local" for candidate in request.constraints.candidates)):
                try:
                    canonical = placement.CargoFolder(folder.path.resolve())
                except (OSError, ValueError) as error:
                    return [wire.Refused(f"cannot resolve the Cargo folder: {error}")]
                request = replace(request, constraints=replace(request.constraints, cargo_folder=canonical))
            self._recover_abandoned_attempt(request)
            if self._needs_mac_linux_state(request):
                setup = self._prepare_mac_linux_state()
                if setup != self.mac_linux_state:
                    self.mac_linux_state = setup
                    self.pass_stale = True
            self._schedule_pass()
            self.placement_requests[request.episode] = request
            self.placement_request_digests[request.episode] = digest
            if previous is None:
                self.prerequisites[request.episode] = request.prerequisite
            if self._placement_waiting(request):
                self._save_place(request, self._placement_row(request))
            self._watch_owner(request.owner)
            if isinstance(connection, Connection) and not self._pass_gap_open():
                self.persist()
                self.parked_placements.append(ParkedPlacement(request, connection))
                return []
            _ = self.run_pass()
            return [self._placement_reply(request,
                self.local_hold_holders.get(request.episode, placement.ResourceHolderUnknown()))]
        assignment = request.assignment
        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
        saved = self.assignments.get(key)
        if saved is None or saved.assignment != assignment:
            return [self._placement_status(assignment, check_lifetime=False)]
        guest = is_mac_linux_guest(assignment.destination)
        current = self.current_assignments.get(assignment.episode) == key
        if guest and not current:
            return [self._placement_status(assignment, "the attempt is no longer current", check_lifetime=False)]
        guest_holds_slot = _holds_consumed_guest_slot(saved)
        if (isinstance(request, wire.ExecutionReport) and assignment.destination.provider == "scaler"
                and request.claim != saved.provider):
            return [self._placement_status(assignment, "claim does not match the scaler assignment", check_lifetime=False)]
        if isinstance(request, wire.ClaimFailure):
            local_decline = (assignment.destination.provider == "local"
                             and isinstance(request.claim, placement.NoProviderClaim))
            if (current
                    and (local_decline or isinstance(saved.permission, placement.PermissionAvailable)
                         or self._guest_took_no_run(saved, request)
                         or assignment.destination.provider == "scaler"
                         and (request.claim == saved.provider or isinstance(request.claim, placement.NoProviderClaim)))
                    and isinstance(saved.state, placement.CurrentAssignment)):
                if isinstance(saved.terminal_report, placement.TerminalAttemptReport):
                    return [self._placement_status(assignment, 'the attempt already has a terminal report', check_lifetime=False)]
                if assignment.destination.provider == "scaler" and isinstance(request.claim, placement.ProviderClaimReference):
                    if request.claim != saved.provider:
                        return [self._placement_status(assignment, "claim does not match the scaler assignment",
                            check_lifetime=False)]
                elif isinstance(request.claim, placement.ProviderClaimReference):
                    from scripts.mac_test.mac_test import scopes_overlap

                    destination = assignment.destination
                    if (request.claim.provider != destination.provider or request.claim.target != destination.target
                            or not scopes_overlap(request.claim.scope, destination.scope, mac=destination.provider == "mac")):
                        return [self._placement_status(assignment, "retained claim does not match the assigned provider scope",
                            check_lifetime=False)]
                if isinstance(connection, Connection):
                    self._record_caller_version(request, connection, persist_change=False)
                terminal = placement.TerminalAttemptReport(placement.PrelaunchRefused(request.reason),
                    placement.DispatchPathPending('provider settlement remains pending'), self.clock.now())
                retained = request.claim if isinstance(request.claim, placement.ProviderClaimReference) else saved.provider
                refused = replace(saved, terminal_report=terminal, provider=retained,
                                  refusal_settings=request.refusal_settings)
                if guest or not self._settle_hana_prelaunch(refused, terminal):
                    retry = placement.retry_decision(assignment.destination, request.reason, terminal.received_at)
                    if assignment.destination.provider == "scaler":
                        refused = replace(refused, retry=retry)
                        self.assignments[key] = refused
                        if not self._settle_aws_dispatch(refused):
                            self.persist()
                        self._schedule_pass()
                    elif (guest_holds_slot and saved.guest_proof_required) or self._start_pending(saved):
                        self.assignments[key] = replace(refused, retry=retry)
                        self._schedule_pass()
                        self.persist()
                    else:
                        terminal = replace(terminal, path=placement.DispatchPathSettled())
                        refused = replace(refused, terminal_report=terminal, retry=retry)
                        if local_decline:
                            self._close_for_report(refused, terminal)
                        else:
                            self._close_placement(refused, request.reason, retry)
        elif (isinstance(saved.permission, placement.PermissionConsumed) and saved.provider == request.claim
                or isinstance(saved.permission, placement.PermissionAvailable)
                and (isinstance(request.outcome, BeforeLaunchRefusal)
                    or isinstance(request.outcome, placement.AttemptCancelled)
                    and isinstance(saved.terminal_report, placement.TerminalAttemptReport)
                    and saved.terminal_report.outcome == request.outcome)
                and isinstance(request.claim, placement.ProviderClaimReference)
                and request.claim.episode == assignment.episode and request.claim.attempt == assignment.attempt
                and request.claim.provider == assignment.destination.provider
                and request.claim.target == assignment.destination.target and request.claim.scope == assignment.destination.scope):
            self._schedule_pass()
            if isinstance(connection, Connection) and (assignment.destination.provider == "scaler" or guest):
                self._record_caller_version(request, connection, persist_change=False)
            if (assignment.destination.target != "mac-linux" or isinstance(saved.permission, placement.PermissionConsumed)
                    or not isinstance(request.evidence, execution_identity.ObservedExecution)):
                self.execution_evidence[key] = request.evidence
                if (assignment.destination.target == "mac-linux"
                        and isinstance(request.evidence, execution_identity.ObservedExecution)):
                    saved = replace(saved, guest_execution_observed=True)
                    self.assignments[key] = saved
            if (isinstance(saved.confirmed_guest_start, records.GuestStartNotRecorded)
                    and current
                    and isinstance(saved.state, placement.CurrentAssignment)
                    and isinstance(saved.permission, placement.PermissionConsumed)
                    and confirmed_guest_run(assignment, request.evidence) is not None):
                saved = replace(saved, confirmed_guest_start=records.ConfirmedGuestStart(self.clock.now()))
                self.assignments[key] = saved
            outcome = request.outcome
            if (isinstance(outcome, (placement.AttemptCompleted, placement.AttemptDeclined, placement.AttemptCancelled))
                    or isinstance(outcome, BeforeLaunchRefusal)
                    and isinstance(request.evidence, execution_identity.ExecutionNotStarted)):
                previous = saved.terminal_report
                path = (placement.DispatchPathPending("Mac Linux settlement is pending")
                        if guest_holds_slot else request.path)
                if (isinstance(previous, placement.NoTerminalReport)
                        or previous.outcome in (SCALER_SETTLEMENT_REQUESTED, GUEST_SETTLEMENT_REQUESTED)
                        and isinstance(previous.path, placement.DispatchPathPending)):
                    received_at = (previous.received_at if isinstance(previous, placement.TerminalAttemptReport)
                                   and previous.outcome == GUEST_SETTLEMENT_REQUESTED else self.clock.now())
                    terminal = placement.TerminalAttemptReport(outcome, path, received_at)
                    retry = saved.retry
                    if isinstance(outcome, execution_identity.HanaCommandAdmissionRefused):
                        retry = placement.DoNotRetry(outcome.reason)
                    elif isinstance(outcome, placement.PrelaunchRefused) and not isinstance(retry, placement.RetryAt):
                        retry = placement.retry_decision(assignment.destination, outcome.reason, terminal.received_at)
                    self.assignments[key] = replace(saved, terminal_report=terminal, provider=request.claim, retry=retry)
                elif previous.outcome == outcome:
                    terminal = (replace(previous, path=path)
                        if isinstance(path, placement.DispatchPathSettled) else previous)
                    self.assignments[key] = replace(saved, provider=request.claim, terminal_report=terminal)
            saved = self.assignments[key]
            report = saved.terminal_report
            if current and assignment.destination.provider == "scaler":
                if not self._settle_aws_dispatch(saved):
                    self.persist()
            elif (current and not guest_holds_slot
                    and isinstance(report, placement.TerminalAttemptReport)
                    and isinstance(report.path, placement.DispatchPathSettled)
                    and (assignment.destination.provider == "scaler" or guest
                         or not isinstance(report.outcome, placement.AttemptDeclined))):
                self._close_for_report(saved, report)
            else:
                self.persist()
        if not self.pass_stale:
            self._publish_replies(self._project_replies(self._queue_order()))
        return [self._placement_status(assignment, check_lifetime=False)]

    def _authorize(self, request: wire.Request, uid: int) -> str:
        if isinstance(request, status_wire.PlacementStatusRequest):
            return ""
        if isinstance(request, wire.VerifyRunSlotRequest):
            if uid != os.getuid() and process_uid(request.owner.pid, self.proc.path) != uid:
                return "Verify process belongs to another account or no longer exists"
            return ""
        if isinstance(request, (wire.PlacementRequest, wire.ConsumePermission, wire.ClaimFailure, wire.ExecutionReport, wire.SettlementRequest,
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
        if isinstance(request, (wire.QueueMoveRequest, wire.QueueUndoRequest)):
            return "queue moves require the service account"
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
            runner=request.runner, job_start=self._request_job_start(request))

    def _request_job_start(self, request: wire.ServiceEstimateRequest) -> records.CiJobStart:
        """Use the sent job start, or find it for a CI request from an older front that sent none.

        A job start saved with the place of the same episode is kept, so a later failed read cannot drop it.
        """
        if isinstance(request.job_start, records.JobStart) or not request.runner:
            return request.job_start
        place = self.places.get(_place_key(request.owner, request.since))
        if place is not None and place.episode == request.episode and isinstance(place.ci_job_start, records.JobStart):
            return place.ci_job_start
        return self.job_start_reader(request.owner)

    @staticmethod
    def _stored_caller(session: str, name: str, unit: str, *,
                       members: records.QueueStaff) -> records.QueueCaller:
        caller = records.queue_caller(session, "", "", "", members=members, unit=unit)
        return replace(caller, name=codec.resolve_queue_caller(stored_caller=name, staff_caller=caller.name))

    def _named_identity(self, identity: wire.Identity, *, members: records.QueueStaff) -> wire.Identity:
        caller = self._stored_caller(identity.session_id, identity.name, identity.unit, members=members)
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
        wall = self.clock.monotonic() - started
        if wall > self.request_slow_seconds:
            cpu = time.thread_time() - cpu_started
            print(f"admission request {type(request).__name__}: wall={wall:.3f}s cpu={cpu:.3f}s",
                  file=sys.stderr, flush=True)

    def _handle_request(self, request: wire.Request, connection: Connection | NoConnection
                        ) -> list[wire.Reply]:
        uid = peer_uid(connection)
        refusal = self._authorize(request, uid)
        if refusal:
            return [wire.Refused(refusal)]
        if isinstance(request, status_wire.PlacementStatusRequest):
            saved = self._saved_assignment(request.episode, request.attempt)
            if isinstance(saved, status_wire.UnknownPlacementStatus):
                return [status_wire.PlacementStatusReply(saved)]
            if request.include_permission:
                permission, report = saved.permission, saved.terminal_report
            else:
                permission, report = status_wire.LAUNCH_PERMISSION_NOT_REPORTED, placement.NoTerminalReport()
            status = status_wire.KnownPlacementStatus(saved.assignment, self._assignment_state(saved), saved.provider,
                                                      permission, report)
            return [status_wire.PlacementStatusReply(status)]
        if isinstance(request, wire.RepositorySlotRequest):
            return [wire.Refused("Repository copy capacity no longer requires admission")]
        if isinstance(request, wire.VerifyRunSlotRequest):
            # Older clients can still ask. Only a local memory grant takes a slot.
            return [wire.Ok()]
        if isinstance(request, wire.SettlementRequest):
            return [self._request_settlement(request)]
        provider_report = isinstance(request, wire.ExecutionReport) and (
            request.assignment.destination.provider == "scaler" or is_mac_linux_guest(request.assignment.destination))
        if (isinstance(connection, Connection) and not provider_report
                and not isinstance(request, (wire.ClaimFailure, wire.CancelPlacement))):
            self._record_caller_version(request, connection,
                persist_change=not isinstance(request, (wire.ConsumePermission, wire.PlacementRequest)))
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
            self.input_event_count += 1
            self.pending_configuration = configuration
        if isinstance(request, (wire.AdmitRequest, wire.LaunchRequest)) and request.code_version:
            self.caller_versions[records.run_owner(request.owner.pid, request.owner.start_time)] = request.code_version
        if isinstance(request, wire.BuildHistoryRequest):
            return [wire.BuildHistoryReply(self.build_history_generation)]
        if isinstance(request, (wire.ViewRequest, wire.ExplainRequest, wire.MineRequest)):
            if not isinstance(request, wire.ViewRequest):
                if not isinstance(self.pending_configuration, NoPass):
                    _ = self.run_pass()
                elif self.pass_stale and isinstance(self.last_inputs, records.QueueInputs):
                    self._prune_runs()
                    ordered = self._queue_order()
                    if isinstance(self.last_policy, records.AdmissionPolicy):
                        self.last_inputs = self._refresh_pass_slots(self.last_inputs, ordered)
                        self.last_result = gate.admission_pass(self.last_inputs, self.last_inputs.now, self.last_policy)
                    self.pass_stale = False
                    self._publish_replies(self._project_replies(ordered))
            if isinstance(self.configuration, gate.UnusableAdmissionConfiguration):
                return [wire.Refused(self.configuration.reason)]
            if (not isinstance(self.last_inputs, records.QueueInputs)
                    or not isinstance(self.last_policy, records.AdmissionPolicy)):
                raise RuntimeError("service has no pass")
            published = self.published
            if not isinstance(published, PublishedReplies):
                raise RuntimeError("service has no published pass")
            if isinstance(request, wire.ViewRequest):
                return [published.view]
            if isinstance(request, wire.ExplainRequest):
                return [published.explain]
            return [wire.MineReply(published.view.reading)]
        if isinstance(request, (wire.QueueMoveRequest, wire.QueueUndoRequest)):
            return self._move_queue(request)
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
            if isinstance(request, wire.AdmitRequest) and self._replaced(request.run):
                return self._mutation_reply(key, wire.Refused(status_wire.REPLACED_REASON))
            if (isinstance(request, wire.AdmitRequest) and isinstance(request.place, wire.CloudPlace)
                    and isinstance(group := request.place.group, status_wire.CloudGroup)):
                if (refused := self._register_cloud_group(request.place, group)) is not None:
                    return self._mutation_reply(key, refused)
            if not isinstance(request, ServiceEstimatedAdmission):
                members = records.queue_staff() if request.identity.session_id else records.NO_STAFF_LOOKUP
                request = replace(request, identity=self._named_identity(request.identity, members=members))
            self._pending_replies[key] = []
            place_key = _place_key(request.owner, request.since)
            row = self._row(request, self.places.get(place_key, records.NoWaiter()))
            place = waiter_place(row, key)
            self._run_place(request, place, (self.host,))
            previous = self.waiters.get(key)
            claim = previous.continuation_claim if previous is not None else NoContinuationClaim()
            if previous is not None and previous.connection is not connection:
                self._remove_waiter(previous, claim_disposition=WaiterClaimDisposition.TRANSFER)
            self.waiters[key] = WaitingAdmission(request, row, connection, continuation_claim=claim)
            self._save_place(request, row)
            self._watch_owner(request.owner)
            if (isinstance(request, wire.AdmitRequest) and request.token_back_at
                    and not isinstance(place.token_left_at, TokenNotLeft)):
                gate.append_queue_time(self.state_dir, {"kind": "token_gap", "episode": key,
                    **gate.queue_life_identity(self.waiters[key].row),
                    "token_left_at": place.token_left_at, "token_back_at": request.token_back_at}, self.clock.now())
        elif isinstance(request, wire.ReleaseRequest):
            episode = next((episode for episode, record in self.reservations.items() if record.reservation_id == request.reservation), None)
            if episode is not None:
                record = self.reservations[episode]
                # Before _end, so that its save keeps the change. The status of a handoff is not a step result.
                if request.ending == "handed_off":
                    self._handed_off(record.run, _local_step(record))
                else:
                    self._released(record.run, _local_step(record), request.status)
                self._end(episode, FirstStepHandOff(key) if request.ending == "handed_off"
                          else ReleaseStatus(key, request.status))
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
                self._prune_runs()
                ordered = self._queue_order()
                self.last_inputs = self._refresh_pass_slots(self.last_inputs, ordered)
                self.pass_stale = True
            self._schedule_pass()
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

    def _move_queue(self, request: wire.QueueMoveRequest | wire.QueueUndoRequest) -> list[wire.Reply]:
        key = "queue-move:" + request.request_id
        previous = self.ledger.get(key)
        if previous is not None:
            return [previous.reply]
        if isinstance(self.configuration, gate.UnusableAdmissionConfiguration):
            return [wire.Refused(self.configuration.reason)]
        full_order = self._full_queue_order(self._ranked_waiters())
        hidden = self._hidden_speculative_waits()
        rows = self._published_queue_order(self._visible_queue_order(full_order), hidden)
        line = self._current_move_line(rows, self._movable_wait_ids(check_owners=True) - hidden)
        result = self._move_destination(request, line, [row.get("move_id", "") for _, row in full_order])
        if isinstance(result, wire.QueueMoveConflict):
            return self._mutation_reply(key, result)
        reply, undo_anchor = result
        self.ledger[key] = ReplayEntry(reply, self.clock.now(), undo_anchor=undo_anchor)
        self.pass_stale = True
        self._schedule_pass()
        self.persist()
        self._publish_moved_queue(line, reply.line)
        return [reply]

    def _publish_moved_queue(self, previous: records.QueueMoveLine, line: records.QueueMoveLine) -> None:
        published = self.published
        if previous == line or not isinstance(published, PublishedReplies):
            return
        reading = published.view.reading
        if reading.view.move_line != previous:
            published.placement_waits.clear()
            return
        by_id = {row[1].get("move_id", ""): (row, step)
                 for row, step in zip(reading.ordered, reading.view.waiting, strict=True)}
        pairs = [by_id[member.row_id] for member in line.members]
        ordered = [row for row, _ in pairs]
        waiting = tuple(step for _, step in pairs)
        reading = replace(reading, ordered=ordered, view=replace(reading.view, waiting=waiting, move_line=line))
        reading = replace(reading,
                          held_places=reading.number_held_places(reading.held_places, gate.waiter_order, self.priority))
        wait_positions = {records.waiter_owner(row): position for position, (_, row) in enumerate(ordered, 1)
                          if records.waiter_owner(row) in published.wait_positions}
        view = wire.ViewReply(reading)
        self._publish_replies(replace(published, view=view, wait_positions=wait_positions, placement_waits={},
                                      view_bytes=wire.encode_reply(view).encode(), mine_bytes=b""))

    def _move_destination(self, request: wire.QueueMoveRequest | wire.QueueUndoRequest, line: records.QueueMoveLine,
                          full_order: list[str]) -> tuple[wire.QueueMoved, QueueAnchor] | wire.QueueMoveConflict:
        changed = wire.QueueMoveConflict(line, "The queue changed. Try the move again.")
        unavailable = wire.QueueMoveConflict(line, "Undo not available")
        by_id = {member.row_id: member for member in line.members}
        row_id = request.row_id
        moving = by_id.get(row_id)
        undo = isinstance(request, wire.QueueUndoRequest)
        if moving is None or not moving.movable or request.revision != line.revision:
            return unavailable if undo else changed

        visible_order = list(by_id)
        old_index = visible_order.index(row_id)
        del visible_order[old_index]
        full_index = full_order.index(row_id)
        previous_anchor = queue_anchor_at(full_order, full_index)
        if isinstance(request, wire.QueueUndoRequest):
            saved = self.ledger.get("queue-move:" + request.move_request_id)
            if (saved is None or not isinstance(saved.reply, wire.QueueMoved) or saved.reply.row_id != row_id
                    or isinstance(saved.undo_anchor, NoQueueUndoAnchor)):
                return unavailable
            destination = saved.undo_anchor
            if isinstance(destination, QueueRowAnchor) and destination.row_id not in full_order:
                return wire.QueueMoveConflict(line, "The old neighbor left the queue.")
        elif isinstance(request.destination, wire.QueueHeadDrop):
            destination = QueueHeadAnchor()
        else:
            drop = request.destination
            if drop.neighbor not in visible_order:
                return changed
            place = visible_order.index(drop.neighbor) + int(drop.side == "after")
            if place == old_index:
                return wire.QueueMoved(row_id, line, old_index + 1, old_index + 1), previous_anchor
            destination = QueueHeadAnchor() if place == 0 else drop

        self._move_pin(row_id, destination, full_order, full_index, previous_anchor)
        updated = self._revise_move_line(tuple(by_id[member] for member in full_order if member in by_id), line.kept_runs)
        new_index = next(index for index, member in enumerate(updated.members) if member.row_id == row_id)
        return wire.QueueMoved(row_id, updated, old_index + 1, new_index + 1), previous_anchor

    def _move_pin(self, row_id: str, destination: wire.QueueDrop | QueueAnchor, order: list[str],
                  old_index: int, old_anchor: QueueAnchor) -> None:
        pins = {pin.row_id: pin.anchor for pin in self.queue_pins}
        moved = QueueRowAnchor(row_id)
        del order[old_index]
        if old_index < len(order) and pins.get(order[old_index]) == moved:
            pins[order[old_index]] = old_anchor

        if isinstance(destination, QueueHeadAnchor):
            index = 0
        elif isinstance(destination, QueueRowAnchor):
            index = order.index(destination.row_id) + 1
        else:
            index = order.index(destination.neighbor) + int(destination.side == "after")
        anchor = queue_anchor_at(order, index)
        if index < len(order) and pins.get(order[index]) == anchor:
            pins[order[index]] = moved
        pins[row_id] = anchor
        order.insert(index, row_id)
        self._set_queue_pins(tuple(QueuePin(member, pins[member]) for member in order if member in pins))

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
        entries = self._priority_entries()
        if isinstance(request, wire.PrioritySetRequest):
            if not request.unit.strip():
                return self._mutation_reply(key, wire.Refused("unit is required"))
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
            action = "set"
            why = request.why
        else:
            if not request.unit and not entries:
                _ = self._priority_history_end(self.clock.now())
                return self._mutation_reply(key, wire.Ok())
            action = "clear"
            why = ", ".join(entry["why"] for entry in entries if not request.unit or entry["unit"] == request.unit)
        if refusal := self._change_priority(action, request.unit, why, request.actor, key, self.clock.now()):
            return self._mutation_reply(key, wire.Refused(refusal))
        self.ledger[key] = ReplayEntry(wire.Ok(), self.clock.now())
        self.persist()
        self._finish_priority()
        _ = self._run_pass(configuration)
        return [self.ledger[key].reply]

    def _priority_entries(self) -> tuple[records.PriorityEntry, ...]:
        return self.priority.entries if isinstance(self.priority, records.QueuePriority) else ()

    def _priority_history_end(self, at: float) -> int:
        return store.read_last_whole_row_end(store.host_file(self.host, at))

    def _change_priority(self, action: Literal["set", "clear"], unit: str, why: str,
                         actor: str, key: str, written_at: float) -> str:
        entries = self._priority_entries()
        if action == "set":
            if any(entry["unit"] == unit for entry in entries):
                return ""
            entry: records.PriorityEntry = {"unit": unit, "why": why, "set_at": written_at, "set_by": actor}
            entries = (*entries, entry)
        else:
            kept = tuple(entry for entry in entries if unit and entry["unit"] != unit)
            if kept == entries:
                return f"unknown unit {unit}; the queue priority did not change" if unit else ""
            unit = unit or ", ".join(entry["unit"] for entry in entries)
            entries = kept
        at = self.clock.now()
        month_row_end = self._priority_history_end(at)
        transition = records.PendingPriorityTransition(key, action, at, month_row_end, unit, why, actor)
        self.input_event_count += 1
        self.priority = records.QueuePriority(entries, transition)
        return ""

    def _bump_place(self, step_key: str, unit: str) -> WaiterPlace | str:
        # The saved place keeps the same key while a live waiter reconnects.
        for place in self.places.values():
            if not place.move_id or place.move_id != step_key:
                continue
            request = self.placement_requests.get(place.episode)
            live = self._placement_owner_live(request.owner) if request is not None else self._live(place.owner)
            if not live:
                break
            identity = place.queue_identity
            recorded_unit = identity.get("unit", "")
            session = identity["session_id"]
            if not recorded_unit and session:
                recorded_unit = gate.queue_unit(recorded_unit, session, gate.priority_labels([session]))
            return place if recorded_unit == unit else "the step no longer belongs to this unit"
        return "the step left the queue"

    def _apply_bumps(self) -> None:
        for offset, bump in person_actions.pending_bumps(self.state_dir, self.bumps.offset):
            self._finish_priority()
            action = bump.action
            place = self._bump_place(action.step_key, bump.unit) if isinstance(action, person_actions.SetBump) else None
            refusal = place if isinstance(place, str) else ""
            moves = self.bumps.moves
            if not refusal:
                previous = self.priority
                from_place = action.from_place if isinstance(action, person_actions.SetBump) else 0
                if isinstance(place, WaiterPlace) and not from_place:
                    from_place = next((index for index, (_, row) in enumerate(self._queue_order(), 1)
                                       if row.get("move_id") == place.move_id), 0)
                refusal = self._change_priority("set" if isinstance(action, person_actions.SetBump) else "clear",
                    bump.unit, bump.reason, bump.actor, bump.id, bump.written_at)
                if isinstance(place, WaiterPlace) and self.priority != previous and not refusal:
                    moves = {**moves, place.episode: person_actions.AppliedBump(
                        place.move_id, place.owner, place.since, bump.actor, from_place, 1, bump.written_at)}
            refusals = self.bumps.refusals
            if refusal:
                refusals = (*refusals, person_actions.BumpNotApplied(bump.unit, refusal))
            self.bumps = person_actions.BumpProgress(offset, refusals, moves)
            self.persist()
            self._finish_priority()

    def _mutation_reply(self, key: str, reply: wire.Ok | wire.Refused | wire.QueueMoveConflict,
                        reservation: str = "") -> list[wire.Reply]:
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

    def _needs_mac_linux_state(self, request: wire.PlacementRequest) -> bool:
        return isinstance(self.mac_linux_state, MacLinuxStateUnavailable) and any(
            is_mac_linux_guest(candidate.destination) and isinstance(candidate.eligibility, placement.AllowedHost)
            for candidate in request.constraints.candidates)

    def _prepare_mac_linux_state(self) -> MacLinuxStateReady | MacLinuxStateUnavailable:
        root: Path | None = None
        try:
            root = target_config.build_targets_state_root()
            self.writes.require(root)
            target_config.provision_mac_linux_state_root(root)
        except (OSError, RuntimeError) as error:
            path = root if root is not None else os.environ.get("BUILD_TARGETS_STATE_DIR") or "~/.local/state/build-targets"
            detail = " ".join(str(error).splitlines())
            return MacLinuxStateUnavailable(f"Cannot prepare the Mac Linux state root {path}: {detail}. "
                                            + "Run build_targets.py setup-mac-linux on natedev.")
        return MacLinuxStateReady(root)

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
        self.mac_linux_state = self._prepare_mac_linux_state()
        if isinstance(self.mac_linux_state, MacLinuxStateUnavailable):
            print(f"memory admission: {self.mac_linux_state.reason}", file=sys.stderr)
        else:
            print(f"memory admission: Mac Linux state root ready at {self.mac_linux_state.root}", file=sys.stderr)
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
                self._end_move(waiting)
                _ = self.places.pop(_place_key(waiting.request.owner, waiting.row["since"]), None)
                self._remove_waiter(waiting, reply, claim_disposition=WaiterClaimDisposition.RETURN)
                changed = True
        if changed:
            self._schedule_pass()
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
        if not isinstance(request, wire.CallerCode) or not request.code_version:
            return
        pid, uid, gid = struct.unpack("3i", connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        del uid, gid
        if isinstance(self.system_reader, NoSystemReader):
            identity = self.proc.owner_start(pid)
        else:
            process = self.system_reader.latest.processes.get(pid)
            identity = LiveOwnerIdentity(process.start) if process is not None else OwnerProcessGone()
        if isinstance(identity, LiveOwnerIdentity):
            caller = records.run_owner(pid, identity.start_time)
            if isinstance(request, wire.PlacementRequest) and self.caller_versions.get(caller) == request.code_version:
                return
            self.caller_versions[caller] = request.code_version
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
                if isinstance(request, wire.PlacementRequest):
                    connection.placement_request_digest = hashlib.sha256(line).digest()
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
            self.pending_configuration = NoPass()
            self._schedule_pass()
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
        # While steps wait, the effective pass interval is the larger of the waiting interval and the minimum pass gap.
        due = max(self.next_pass, self._pass_allowed_at())
        delay = min(timeout, max(0, min(due, self.next_source_check, self.next_config_watch_retry) - now))
        ready = self.epoll.poll(-1 if delay == float("inf") else delay)
        connections = {descriptor: ConnectionReadiness(connection, events) for descriptor, events in ready
                       if (connection := self.connections.get(descriptor)) is not None}
        watches = {descriptor: watch for descriptor, _events in ready
                   if (watch := self.watchers.get(descriptor)) is not None}
        # A due pass can find journal changes and expired holds without a reader event.
        if self.clock.monotonic() >= due:
            self.input_event_count += 1
        if any(descriptor == self.priority_events for descriptor, _events in ready):
            _ = os.eventfd_read(self.priority_events)
        if any(descriptor == self.reading_events for descriptor, _events in ready):
            _ = os.eventfd_read(self.reading_events)
            self._schedule_pass()
            self._refresh_host_observations()
            if not isinstance(self.system_reader, NoSystemReader):
                self.system_reader.raise_failure()
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
            self._schedule_pass()
        # These events can change placement after the loop reads ready requests.
        if watches or (isinstance(self.pending_priority, PendingPriorityRoster) and self.pending_priority.result.done()):
            self.input_event_count += 1
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
                self._schedule_pass()
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


def _valid_current_assignments(state: ServiceState) -> dict[str, str]:
    """Keep valid index entries and retain all assignment records."""
    valid: dict[str, str] = {}
    for episode, key in state.current_assignments.items():
        saved = state.assignments.get(key)
        if (saved is not None and saved.assignment.episode == episode
                and isinstance(saved.state, placement.CurrentAssignment)):
            valid[episode] = key
    return valid


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
    state = replace(state, current_assignments=_valid_current_assignments(state))
    for key in state.current_assignments.values():
        saved = state.assignments[key]
        report = saved.terminal_report
        if (is_mac_linux_guest(saved.assignment.destination) and isinstance(saved.permission, placement.PermissionAvailable)
                and isinstance(report, placement.TerminalAttemptReport) and report.outcome == GUEST_SETTLEMENT_REQUESTED):
            state.assignments[key] = replace(saved, terminal_report=placement.NoTerminalReport())
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
