"""Verify and stop the local run behind a Builds row."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import time
from typing import TypedDict, cast

from scripts.build_queue import admission_client, admission_records, execution_identity
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.buildlog import store
from scripts.buildlog.parse import CARGO_BUILD_VERBS
from scripts.lib import conf, process_start
from scripts.lib.build_steps import GATE_PROGRAMS, command_program, live_process_arguments
from scripts.lib.user_zone import user_zone
from scripts.lint import memory_admit
from scripts.message import send_budget, session_send
from scripts.production import staff


PATH = "/console/build-kill"
PROC = Path("/proc")
CARGO_PROGRAMS = frozenset({"cargo", "cargo-nextest", "cargo-tile-real"})
BUILD_PROGRAMS = CARGO_PROGRAMS | {"verify.sh"}
BUILD_STEPS = CARGO_BUILD_VERBS | {"cargo", "cargo-nextest", "fmt"}


@dataclass(frozen=True)
class KillCandidate:
    """One current reservation that can have a verified stop route."""

    holder_pid: int
    holder_start: str
    row_pid: int
    row_start: str
    session: str
    step: str
    admitted_at: str
    sidecar: str
    verify_call: bool = False

    @property
    def reference(self) -> str:
        fields = (self.holder_pid, self.holder_start, self.row_pid, self.row_start,
                  self.session, self.step, self.admitted_at)
        return sha256(json.dumps(fields).encode()).hexdigest()


@dataclass(frozen=True)
class NoKillCandidate:
    """The row cannot have a stop route."""


def candidate(step: admission_records.RunningStep, reservation: admission_records.Reservation,
              machine: str, *, ci: bool = False) -> KillCandidate | NoKillCandidate:
    """Use the recorded local process and caller, independent of the display label."""
    if (machine != "natedev" or ci or step.ci_runner or step.mac_placement or not step.session_id
            or admission_records.execution_host(step, machine) != machine
            or not memory_admit.running_has_reservation(step)
            or not isinstance(step.measured, admission_records.MeasuredMemory)
            or isinstance(step.execution, execution_identity.ObservedExecution)
            and not isinstance(step.execution.identity, execution_identity.ProcessExecution)
            or not step.step.isprintable()
            or step.pid != reservation["pid"] or reservation["pid"] <= 1
            or not reservation["start_time"].isdecimal()
            or step.step in GATE_PROGRAMS or isinstance(step.job, StepJob)
            or not (step.step in BUILD_STEPS or isinstance(step.verify, admission_records.VerifyCall))):
        return NoKillCandidate()
    run = step.run
    row_pid, row_start = ((run.pid, run.start_time) if isinstance(run, LiveBuildRun)
                          else (reservation["pid"], reservation["start_time"]))
    return KillCandidate(reservation["pid"], reservation["start_time"], row_pid, row_start,
                         step.session_id, step.step, reservation["admitted_at"], reservation["sidecar"],
                         isinstance(step.verify, admission_records.VerifyCall))


@dataclass(frozen=True)
class ProgramScope:
    """A distinct systemd scope whose live members belong to the queue holder."""

    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class DetachedCommand:
    """A supervised command group that contains the recorded run."""

    record: Path
    group: int
    started: str
    supervisor: int
    supervisor_start: str


@dataclass(frozen=True)
class VerifyRun:
    """The exact Verify shell that owns the run and its step scopes."""

    pid: int
    start: str


StopRoute = ProgramScope | DetachedCommand | VerifyRun


@dataclass(frozen=True)
class NoStopRoute:
    """The run has no verified route for stopping its processes."""


@dataclass(frozen=True)
class KillTarget:
    candidate: KillCandidate
    route: StopRoute
    recipient: staff.StaffId
    starter: str
    recipient_name: str
    recipient_role: str


def live(pid: int, started: str) -> bool:
    return process_start.proc_table_start(pid, PROC) == process_start.Started(started)


def ended(pid: int, started: str) -> bool:
    state = process_start.proc_table_start(pid, PROC)
    return isinstance(state, (process_start.Gone, process_start.Zombie)) or (
        isinstance(state, process_start.Started) and state.start != started)


def program(pid: int, processes: dict[int, memory_admit.ProcessIdentity]) -> str:
    args = live_process_arguments(PROC / str(pid), processes[pid].start)
    return command_program(args) if isinstance(args, list) else ""


def program_scope(run: KillCandidate, processes: dict[int, memory_admit.ProcessIdentity],
                  descendants: set[int]) -> ProgramScope | NoStopRoute:
    if not run.sidecar:
        return NoStopRoute()
    try:
        saved_scope = Path(run.sidecar).read_text().strip()
        if not saved_scope:
            return NoStopRoute()
        scope = Path(saved_scope).resolve(strict=True)
        if (scope.parent != memory_admit.builds_slice_cgroup().resolve(strict=True)
                or not re.fullmatch(r"run-[a-zA-Z0-9]+\.scope", scope.name)):
            return NoStopRoute()
        memory_admit.check_memory_reading(scope / "cgroup.events")
        identity = scope.stat()
        members = memory_admit.scope_members(scope)
        running = members & processes.keys()
        if (run.holder_pid in members or not running or not members <= descendants
                or not any(program(pid, processes) in BUILD_PROGRAMS for pid in running)):
            return NoStopRoute()
    except (OSError, ValueError):
        return NoStopRoute()
    return ProgramScope(scope, identity.st_dev, identity.st_ino)


def detached_command(run: KillCandidate, recipient: staff.StaffId,
                     processes: dict[int, memory_admit.ProcessIdentity],
                     descendants: set[int]) -> DetachedCommand | NoStopRoute:
    from scripts.agents import detached_run_process, detached_runs, worker_lifecycle

    if not callable(getattr(detached_runs, "stop_verified_command", None)):
        return NoStopRoute()
    pid = run.holder_pid
    while pid in processes:
        parent = processes[pid].parent
        try:
            record_entry = next((part for part in (PROC / str(pid) / "environ").read_bytes().split(b"\0")
                                 if part.startswith(b"UNIT_DETACHED_RECORD=")), b"")
        except OSError:
            return NoStopRoute()
        if record_entry:
            path = Path(os.fsdecode(record_entry.partition(b"=")[2]))
            if not path.is_absolute() or path.parent.name != detached_runs.DETACHED_RUNS_DIR:
                return NoStopRoute()
            try:
                raw = worker_lifecycle.read_json_object(path)
                group = detached_runs.command_group(path)
            except (OSError, ValueError):
                return NoStopRoute()
            if (group is not None and group.group == pid and group.started == processes[pid].start
                    and raw.get("command_pid") == pid and raw.get("staff_id") == recipient
                    and isinstance(supervisor := raw.get("pid"), int) and supervisor == parent
                    and isinstance(start := raw.get("supervisor_started"), str)
                    and live(supervisor, start)
                    and detached_run_process.supervisor_match(supervisor, path) is True
                    and detached_build(raw.get("command"), program(pid, processes))
                    and all(in_group(child, pid) for child in descendants)):
                return DetachedCommand(path, pid, group.started, supervisor, start)
        if parent not in processes or int(processes[parent].start) > int(processes[pid].start):
            break
        pid = parent
    return NoStopRoute()


def detached_build(command: object, leader: str) -> bool:
    if not isinstance(command, list):
        return False
    arguments = cast(list[object], command)
    if not all(isinstance(value, str) for value in arguments):
        return False
    recorded = command_program(cast(list[str], arguments))
    return recorded in BUILD_PROGRAMS | {"lint"} and leader in (recorded, "systemd-run")


def in_group(pid: int, group: int) -> bool:
    try:
        return os.getpgid(pid) == group
    except ProcessLookupError:
        return False


def stop_route(run: KillCandidate, recipient: staff.StaffId,
               processes: dict[int, memory_admit.ProcessIdentity]) -> StopRoute | NoStopRoute:
    """Require a live Cargo or Verify process with a dedicated stop boundary."""
    if not live(run.holder_pid, run.holder_start) or not live(run.row_pid, run.row_start):
        return NoStopRoute()
    descendants = memory_admit.process_tree({run.holder_pid: run.holder_start}, processes)
    row_tree = memory_admit.process_tree({run.row_pid: run.row_start}, processes)
    if run.holder_pid not in row_tree:
        return NoStopRoute()
    ancestor = run.holder_pid
    verify_shell = 0
    while ancestor in processes:
        name = program(ancestor, processes)
        if name in {"validate_ci.sh", "ci_cargo_front.py"}:
            return NoStopRoute()
        if name == "verify.sh":
            verify_shell = ancestor
        parent = processes[ancestor].parent
        if parent not in processes or int(processes[parent].start) > int(processes[ancestor].start):
            break
        ancestor = parent
    if verify_shell:
        return VerifyRun(run.row_pid, run.row_start) if verify_shell == run.row_pid else NoStopRoute()
    if run.verify_call:
        return NoStopRoute()
    if isinstance(scope := program_scope(run, processes, descendants), ProgramScope):
        return scope
    return detached_command(run, recipient, processes, row_tree)


def live_targets() -> dict[str, KillTarget]:
    machine = store.host_name()
    if machine != "natedev":
        return {}
    reading = admission_client.view()
    explanation = reading.explanation
    steps = (entry.step for entry in explanation.running if memory_admit.running_has_reservation(entry.step))
    candidates = [run for step, reservation in zip(steps, explanation.decided_from.reservations, strict=True)
                  if isinstance(run := candidate(step, reservation, machine), KillCandidate)]
    if not candidates:
        return {}
    members = staff.snapshot()
    counts = Counter(run.reference for run in candidates)
    processes = memory_admit.process_table(PROC)
    result: dict[str, KillTarget] = {}
    for run in candidates:
        if counts[run.reference] != 1:
            continue
        caller = members.by_current_session(run.session)
        if caller is None or sum(row.current_session == run.session for row in members.rows) != 1:
            continue
        route = stop_route(run, caller.id, processes)
        if not isinstance(route, NoStopRoute):
            role = "unit director" if caller.kind == "unit" else caller.kind
            parent = members.parent_of(caller)
            starter = f"{caller.name}, a {role}"
            if caller.kind == "worker" and parent is not None:
                starter += f" of {parent.name}"
            result[run.reference] = KillTarget(run, route, caller.id, starter, caller.name, role)
    return result


class StopState(StrEnum):
    STOPPING = "Stopping"
    STOPPED = "Stopped"
    UNCONFIRMED = "Stop unconfirmed"


class DeliveryState(StrEnum):
    PENDING = ""
    SENT = "Message sent"
    QUEUED = "Message queued"
    FAILED = "Message failed"


class KillOffer(TypedDict):
    handle: str
    pid: int
    started_by: str
    recipient_name: str
    step: str
    message: str


class KillResult(TypedDict):
    reference: str
    step: str
    pid: int
    stop: StopState
    delivery: DeliveryState
    time: str
    recipient_name: str
    recipient_role: str


@dataclass(frozen=True)
class PendingNotice:
    """Keep the words until the stop can be confirmed."""

    result: KillResult
    recipient: staff.StaffId
    message: str


@dataclass(frozen=True)
class StopFinished:
    result: KillResult
    expires: float


def notification(run: KillCandidate, at: datetime) -> str:
    return f'nate killed your run "{run.step}" (pid {run.row_pid}) from the Builds page at {at:%H:%M %Z}'


def scope_empty(route: ProgramScope) -> bool:
    memory_admit.check_memory_reading(route.path / "cgroup.events")
    try:
        descriptor = os.open(route.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    try:
        identity = os.fstat(descriptor)
        if (identity.st_dev, identity.st_ino) != (route.device, route.inode):
            return False
        events = os.open("cgroup.events", os.O_RDONLY, dir_fd=descriptor)
        with os.fdopen(events, "rb") as evidence:
            return b"populated 0" in evidence.read().splitlines()
    except FileNotFoundError:
        return not route.path.exists()
    finally:
        os.close(descriptor)


StopConfirmation = Callable[[], bool]


def stop_program_scope(route: ProgramScope, run: KillCandidate, seconds: float) -> StopConfirmation | NoStopRoute:
    """Ask the user manager to stop the verified scope."""
    processes = memory_admit.process_table(PROC)
    descendants = memory_admit.process_tree({run.holder_pid: run.holder_start}, processes)
    if not live(run.row_pid, run.row_start) or program_scope(run, processes, descendants) != route:
        return NoStopRoute()
    try:
        result = subprocess.run(["systemctl", "--user", "stop", route.path.name],
                                capture_output=True, check=False, timeout=seconds)
    except subprocess.TimeoutExpired:
        # The manager can finish its stop after the client exits.
        return lambda: scope_empty(route)
    if result.returncode:
        return NoStopRoute()
    return lambda: scope_empty(route)


def member_scope(pid: int) -> ProgramScope | NoStopRoute:
    """Find a scope in builds.slice from a verified member's kernel record."""
    for line in (PROC / str(pid) / "cgroup").read_text().splitlines():
        if line.startswith("0::"):
            path = Path("/sys/fs/cgroup", line[3:].lstrip("/"))
            root = memory_admit.builds_slice_cgroup()
            for scope in (path, *path.parents):
                if scope.is_relative_to(root) and scope.suffix == ".scope":
                    memory_admit.check_memory_reading(scope / "cgroup.events")
                    info = scope.stat()
                    return ProgramScope(scope, info.st_dev, info.st_ino)
    return NoStopRoute()


@dataclass
class RunCleanup:
    """Keep each observed descendant and scope until cleanup can be confirmed."""

    pid: int
    start: str
    members: dict[int, str] = field(default_factory=dict[int, str])
    scopes: set[ProgramScope] = field(default_factory=set[ProgramScope])
    incomplete: bool = False

    def sample(self) -> None:
        processes = memory_admit.process_table(PROC)
        roots = {self.pid: self.start, **self.members}
        for pid in memory_admit.process_tree(roots, processes) - {self.pid}:
            start = processes[pid].start
            if not live(pid, start):
                continue
            self.members[pid] = start
            try:
                scope = member_scope(pid)
                if isinstance(scope, ProgramScope) and live(pid, start):
                    self.scopes.add(scope)
            except OSError:
                if live(pid, start):
                    self.incomplete = True

    def complete(self) -> bool:
        self.sample()
        return (not self.incomplete and ended(self.pid, self.start)
                and all(ended(pid, start) for pid, start in self.members.items())
                and all(scope_empty(scope) for scope in self.scopes))


def stop_verify(route: VerifyRun, run: KillCandidate) -> StopConfirmation | NoStopRoute:
    """Signal the Verify shell through its pidfd so its exit trap can run."""
    descriptor = os.pidfd_open(route.pid)
    try:
        argv = live_process_arguments(PROC / str(route.pid), route.start)
        if not isinstance(argv, list) or command_program(argv) != "verify.sh":
            return NoStopRoute()
        cleanup = RunCleanup(route.pid, route.start)
        cleanup.sample()
        if run.sidecar:
            table = memory_admit.process_table(PROC)
            descendants = memory_admit.process_tree({run.holder_pid: run.holder_start}, table)
            scope = program_scope(run, table, descendants)
            if isinstance(scope, ProgramScope):
                cleanup.scopes.add(scope)
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        return cleanup.complete
    finally:
        os.close(descriptor)


def stop_detached(route: DetachedCommand, run: KillCandidate, reason: str) -> StopConfirmation | NoStopRoute:
    """Ask the supervisor to stop the exact command named in the handle."""
    from scripts.agents import detached_runs, worker_lifecycle

    stop = cast(Callable[[Path, str, detached_runs.CommandGroup], bool] | None,
                getattr(detached_runs, "stop_verified_command", None))
    if stop is None or not live(run.holder_pid, run.holder_start) or not live(run.row_pid, run.row_start):
        return NoStopRoute()
    cleanup = RunCleanup(route.group, route.started)
    cleanup.sample()
    if not stop(route.record, reason, detached_runs.CommandGroup(route.group, route.started)):
        return NoStopRoute()

    def complete() -> bool:
        raw = worker_lifecycle.read_json_object(route.record)
        return (raw.get("status") == "finished" and raw.get("stop_reason") == reason
                and ended(run.holder_pid, run.holder_start) and ended(run.row_pid, run.row_start)
                and cleanup.complete())

    return complete


def observe(confirmation: StopConfirmation, seconds: float, sample_seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if confirmation():
                return True
        except (OSError, ValueError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(remaining, sample_seconds))


def deliver(recipient: staff.StaffId, message: str) -> DeliveryState:
    response = subprocess.run(session_send.send_argv("--to", f"staff:{recipient}", "--from", "nate",
                                                     "--text", message),
                              capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                              timeout=send_budget.seconds(), check=False)
    if response.returncode == 0 and response.stdout.startswith("SENT:"):
        return DeliveryState.SENT
    if response.returncode == 1 and response.stdout.startswith("QUEUED:"):
        return DeliveryState.QUEUED
    return DeliveryState.FAILED


class KillActions:
    """Issue handles and retain pending notices and stop results."""

    def __init__(self) -> None:
        self._key: bytes = secrets.token_bytes(32)
        self._lock: threading.Lock = threading.Lock()
        self._pending: dict[str, PendingNotice] = {}
        self._finished: dict[str, StopFinished] = {}

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def handle(self, target: KillTarget) -> str:
        data = repr((target.candidate.reference, target.route, target.recipient)).encode()
        return hmac.new(self._key, data, sha256).hexdigest()

    def _expire(self) -> None:
        now = time.monotonic()
        for reference, finished in tuple(self._finished.items()):
            if finished.expires <= now:
                del self._finished[reference]

    def options(self) -> tuple[dict[str, KillOffer], list[KillResult]]:
        targets = live_targets()
        now = datetime.now(user_zone())
        with self._lock:
            self._expire()
            offers: dict[str, KillOffer] = {
                reference: {"handle": self.handle(target), "pid": target.candidate.row_pid,
                            "started_by": target.starter, "recipient_name": target.recipient_name,
                            "step": target.candidate.step, "message": notification(target.candidate, now)}
                for reference, target in targets.items()
                if reference not in self._pending and reference not in self._finished}
            return offers, [*(entry.result for entry in self._pending.values()),
                            *(entry.result for entry in self._finished.values())]

    def _finish(self, notice: PendingNotice, stopped: bool, retention: float) -> KillResult:
        result: KillResult = {**notice.result, "stop": StopState.UNCONFIRMED}
        if stopped:
            delivery = DeliveryState.FAILED
            try:
                delivery = deliver(notice.recipient, notice.message)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            result = {**result, "stop": StopState.STOPPED, "delivery": delivery}
        with self._lock:
            reference = result["reference"]
            del self._pending[reference]
            self._finished[reference] = StopFinished(result, time.monotonic() + retention)
        return result

    def _finish_later(self, notice: PendingNotice, confirmation: StopConfirmation,
                      seconds: float, sample: float, retention: float) -> None:
        _ = self._finish(notice, observe(confirmation, seconds, sample), retention)

    def stop(self, reference: str, handle: str, words: str) -> KillResult | NoStopRoute:
        target = live_targets().get(reference)
        if target is None or not hmac.compare_digest(self.handle(target), handle):
            return NoStopRoute()
        settings = conf.read_or_repository("review_links")
        seconds = settings.number("kill_stop_seconds")
        sample = settings.number("kill_sample_seconds")
        retention = settings.number("kill_result_seconds")
        late_seconds = settings.number("kill_late_confirmation_seconds")
        run = target.candidate
        at = datetime.now(user_zone())
        reason = notification(run, at)
        result: KillResult = {"reference": reference, "step": run.step, "pid": run.row_pid,
                              "stop": StopState.STOPPING, "delivery": DeliveryState.PENDING,
                              "time": at.strftime("%H:%M %Z"), "recipient_name": target.recipient_name,
                              "recipient_role": target.recipient_role}
        notice = PendingNotice(result, target.recipient, f"{reason}\n{words}" if words else reason)
        with self._lock:
            self._expire()
            if reference in self._pending or reference in self._finished:
                return NoStopRoute()
            self._pending[reference] = notice
        try:
            route = target.route
            if isinstance(route, VerifyRun):
                confirmation = stop_verify(route, run)
            elif isinstance(route, ProgramScope):
                confirmation = stop_program_scope(route, run, seconds)
            else:
                confirmation = stop_detached(route, run, reason)
        except (OSError, ValueError, subprocess.SubprocessError):
            return self._finish(notice, False, retention)
        if isinstance(confirmation, NoStopRoute):
            return self._finish(notice, False, retention)
        if observe(confirmation, seconds, sample):
            return self._finish(notice, True, retention)
        notice = replace(notice, result={**result, "stop": StopState.UNCONFIRMED})
        with self._lock:
            self._pending[reference] = notice
        threading.Thread(target=self._finish_later, args=(notice, confirmation, late_seconds, sample, retention),
                         daemon=True).start()
        return notice.result
