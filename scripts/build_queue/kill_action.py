"""Verify and stop the local run behind a Builds row."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import fcntl
from hashlib import sha256
import hmac
from importlib import import_module
from inspect import signature
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Literal, Protocol, TypedDict, cast

from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, snapshot_codec
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.buildlog import store
from scripts.buildlog.parse import CARGO_BUILD_VERBS
from scripts.lib import conf, process_start
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.build_steps import GATE_PROGRAMS, command_program, live_process_arguments
from scripts.lib.user_zone import user_zone
from scripts.lint import memory_admit
from scripts.message import send_budget, session_send
from scripts.production import staff


PATH = "/console/build-kill"
PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
CARGO_PROGRAMS = frozenset({"cargo", "cargo-nextest", "cargo-tile-real"})
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
            or isinstance(step.verify, admission_records.VerifyCall) or step.step not in BUILD_STEPS):
        return NoKillCandidate()
    run = step.run
    row_pid, row_start = ((run.pid, run.start_time) if isinstance(run, LiveBuildRun)
                          else (reservation["pid"], reservation["start_time"]))
    return KillCandidate(reservation["pid"], reservation["start_time"], row_pid, row_start,
                         step.session_id, step.step, reservation["admitted_at"], reservation["sidecar"])


class ScopeIdentity(TypedDict):
    """The saved directory identity from the shared stop API."""

    unit: str
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class ProgramScope:
    """A Cargo scope with an exact live member."""

    scope: ScopeIdentity
    anchor_pid: int
    anchor_start: str


@dataclass(frozen=True)
class NoLaunchScope:
    """The record has no verified launch scope."""


@dataclass(frozen=True)
class DetachedCommand:
    """A supervised command group that contains the recorded run."""

    record: Path
    group: int
    started: str
    supervisor: int
    supervisor_start: str
    scope: ScopeIdentity


StopRoute = ProgramScope | DetachedCommand


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


def program(pid: int, processes: dict[int, memory_admit.ProcessIdentity]) -> str:
    args = live_process_arguments(PROC / str(pid), processes[pid].start)
    return command_program(args) if isinstance(args, list) else ""


class ScopeStopReceipt(Protocol):
    @property
    def term_reached(self) -> int: ...


class ProcessStopApi(Protocol):
    """The shared stop API becomes available when phase 44fh reaches main."""

    def supported(self) -> bool: ...
    def ProcessLifetime(self, pid: int, start: str, /) -> object: ...
    def ScopeUnit(self, slice_name: str, scope_name: str, /) -> object: ...
    def pin_process(self, process: object, /) -> AbstractContextManager[int]: ...
    def command_scope(self, pid: int, scope: object, /) -> ScopeIdentity: ...
    def stop_scope(self, scope: ScopeIdentity, grace: float, kill_grace: float, /) -> ScopeStopReceipt: ...
    def scope_has_ended(self, scope: ScopeIdentity, /) -> bool: ...


class DetachedStop(Protocol):
    def __call__(self, record: Path, reason: str, /, *, check: Callable[[], bool]) -> bool: ...


def detached_stop_api() -> DetachedStop | NoStopRoute:
    module = import_module("scripts.agents.detached_runs")
    stop = getattr(module, "stop_command", None)
    if not callable(stop) or "check" not in signature(stop).parameters:
        return NoStopRoute()
    return cast(DetachedStop, stop)


def process_stop_api() -> ProcessStopApi | NoStopRoute:
    try:
        module = cast(ProcessStopApi, cast(object, import_module("scripts.lib.process_stop")))
    except ModuleNotFoundError as error:
        if error.name != "scripts.lib.process_stop":
            raise
        return NoStopRoute()
    required = ("supported", "ProcessLifetime", "ScopeUnit", "pin_process",
                "command_scope", "stop_scope", "scope_has_ended")
    if not all(callable(getattr(module, name, None)) for name in required) or not module.supported():
        return NoStopRoute()
    annotations = cast(dict[str, object], getattr(module.stop_scope, "__annotations__", {}))
    receipt = cast(object, getattr(module, "ScopeStopReceipt", None))
    if receipt is None or annotations.get("return") not in (receipt, "ScopeStopReceipt"):
        return NoStopRoute()
    return NoStopRoute() if isinstance(detached_stop_api(), NoStopRoute) else module


def read_scope_file(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        return source.read()


@dataclass(frozen=True)
class ScopeNotPublished:
    """The run has no recorded program scope."""


def program_scope(api: ProcessStopApi, run: KillCandidate, processes: dict[int, memory_admit.ProcessIdentity],
                  descendants: set[int]) -> ProgramScope | ScopeNotPublished | NoStopRoute:
    if not run.sidecar:
        return ScopeNotPublished()
    try:
        saved_scope = Path(run.sidecar).read_text().strip()
        if not saved_scope:
            return ScopeNotPublished()
        scope = Path(saved_scope).resolve(strict=True)
        if (not scope.is_relative_to(CGROUP_ROOT)
                or scope.parent != memory_admit.builds_slice_cgroup().resolve(strict=True)
                or not re.fullmatch(r"run-[a-zA-Z0-9]+\.scope", scope.name)):
            return NoStopRoute()
        memory_admit.check_memory_reading(scope / "cgroup.procs")
        descriptor = os.open(scope, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            identity = os.fstat(descriptor)
            direct_members = {int(pid) for pid in read_scope_file(descriptor, "cgroup.procs").split()}
            members = memory_admit.scope_members(scope)
            if not members or members != direct_members or run.holder_pid in members or not members <= descendants:
                return NoStopRoute()
            cargo = {pid: processes[pid].start for pid in descendants if program(pid, processes) in CARGO_PROGRAMS}
            if not cargo or not memory_admit.process_tree(cargo, processes) <= members:
                return NoStopRoute()
            expected: ScopeIdentity = {"unit": scope.name, "path": "/" + str(scope.relative_to(CGROUP_ROOT)),
                                       "device": identity.st_dev, "inode": identity.st_ino}
            for pid, start in sorted(cargo.items()):
                try:
                    with api.pin_process(api.ProcessLifetime(pid, start)):
                        observed = api.command_scope(pid, api.ScopeUnit(scope.parent.name, scope.name))
                        if observed == expected and live(pid, start):
                            return ProgramScope(observed, pid, start)
                except (OSError, ValueError):
                    continue
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        pass
    return NoStopRoute()


def detached_launch_scope(value: object) -> ScopeIdentity | NoLaunchScope:
    """Decode the scope that the detached launcher saved at startup."""
    match value:
        case {"unit": str(unit), "path": str(name), "device": int(device), "inode": int(inode)}:
            path = Path(name)
            if (type(device) is int and device >= 0 and type(inode) is int and inode > 0
                    and path.is_absolute() and ".." not in path.parts
                    and path.parent.name == "builds-detached.slice" and path.name == unit
                    and unit.endswith(".scope")):
                return {"unit": unit, "path": name, "device": device, "inode": inode}
        case _:
            pass
    return NoLaunchScope()


def detached_command(api: ProcessStopApi, run: KillCandidate, recipient: staff.StaffId,
                     processes: dict[int, memory_admit.ProcessIdentity],
                     descendants: set[int]) -> DetachedCommand | NoStopRoute:
    from scripts.agents import detached_run_process, detached_runs, worker_lifecycle

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
            scope = detached_launch_scope(raw.get("command_scope"))
            if (not isinstance(scope, NoLaunchScope)
                    and group is not None and group.group == pid and group.started == processes[pid].start
                    and raw.get("command_pid") == pid and raw.get("staff_id") == recipient
                    and isinstance(supervisor := raw.get("pid"), int) and supervisor == parent
                    and isinstance(start := raw.get("supervisor_started"), str)
                    and live(supervisor, start)
                    and detached_run_process.supervisor_match(supervisor, path) is True
                    and detached_build(raw.get("command"), program(pid, processes))
                    and all(in_group(child, pid) for child in descendants)):
                try:
                    unit = api.ScopeUnit("builds-detached.slice", scope["unit"])
                    with api.pin_process(api.ProcessLifetime(run.holder_pid, run.holder_start)):
                        if (api.command_scope(run.holder_pid, unit) == scope
                                and live(run.holder_pid, run.holder_start)):
                            return DetachedCommand(path, pid, group.started, supervisor, start, scope)
                except (OSError, ValueError):
                    return NoStopRoute()
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
    return recorded in CARGO_PROGRAMS | {"lint"} and leader in (recorded, "systemd-run")


def in_group(pid: int, group: int) -> bool:
    try:
        return os.getpgid(pid) == group
    except ProcessLookupError:
        return False


def stop_route(run: KillCandidate, recipient: staff.StaffId,
               processes: dict[int, memory_admit.ProcessIdentity]) -> StopRoute | NoStopRoute:
    """Require a live Cargo process with a dedicated stop boundary."""
    api = process_stop_api()
    if isinstance(api, NoStopRoute):
        return NoStopRoute()
    if not live(run.holder_pid, run.holder_start) or not live(run.row_pid, run.row_start):
        return NoStopRoute()
    descendants = memory_admit.process_tree({run.holder_pid: run.holder_start}, processes)
    row_tree = memory_admit.process_tree({run.row_pid: run.row_start}, processes)
    if run.holder_pid not in row_tree:
        return NoStopRoute()
    ancestor = run.holder_pid
    while ancestor in processes:
        if program(ancestor, processes) in {"verify.sh", "validate_ci.sh", "ci_cargo_front.py"}:
            return NoStopRoute()
        parent = processes[ancestor].parent
        if parent not in processes or int(processes[parent].start) > int(processes[ancestor].start):
            break
        ancestor = parent
    scope = program_scope(api, run, processes, descendants)
    if isinstance(scope, ScopeNotPublished):
        return detached_command(api, run, recipient, processes, row_tree)
    return scope


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
    NOT_SENT = "Kill not sent; try again"


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
    stop: Literal[StopState.STOPPING, StopState.STOPPED, StopState.UNCONFIRMED, StopState.NOT_SENT]
    delivery: Literal[DeliveryState.PENDING, DeliveryState.SENT, DeliveryState.QUEUED, DeliveryState.FAILED]
    time: str
    recipient_name: str
    recipient_role: str


@dataclass(frozen=True)
class StopRequested:
    """The action records the request before it sends TERM."""


@dataclass(frozen=True)
class TermSent:
    """The stop route confirms TERM or an ended scope."""


@dataclass(frozen=True)
class SendingNotice:
    """The saved TERM receipt and empty scope allow the keyed notice."""

    term: TermSent


@dataclass(frozen=True)
class FinishedStop:
    """The action is complete. Its result stays visible until this time."""

    visible_until: float


@dataclass(frozen=True)
class SavedKillAction:
    id: str
    run: KillCandidate
    result: KillResult
    recipient: staff.StaffId
    message: str
    stage: StopRequested | TermSent | SendingNotice | FinishedStop
    scope: ScopeIdentity | NoLaunchScope = NoLaunchScope()


def notification(run: KillCandidate, at: datetime) -> str:
    return f'nate killed your run "{run.step}" (pid {run.row_pid}) from the Builds page at {at:%H:%M %Z}'


def stop_complete(action: SavedKillAction) -> bool:
    api = process_stop_api()
    return not isinstance(action.scope, NoLaunchScope) and not isinstance(api, NoStopRoute) and api.scope_has_ended(action.scope)


def stop_verified_scope(target: KillTarget, reason: str) -> bool:
    """Recheck the route before asking the shared helper to stop the saved scope."""
    api = process_stop_api()
    if isinstance(api, NoStopRoute):
        return False

    def route_matches() -> bool:
        return stop_route(target.candidate, target.recipient, memory_admit.process_table(PROC)) == target.route

    if not route_matches():
        return False
    route = target.route
    if isinstance(route, DetachedCommand):
        stop = detached_stop_api()
        return not isinstance(stop, NoStopRoute) and stop(route.record, reason, check=route_matches)
    settings = conf.read_or_repository("shutdown")
    receipt = api.stop_scope(route.scope, settings.whole("STOP_GRACE_SECONDS"),
                             settings.whole("KILL_GRACE_SECONDS"))
    return receipt.term_reached > 0 or api.scope_has_ended(route.scope)


@dataclass(frozen=True)
class RetryNotice:
    """The sender did not give a final delivery result."""


def deliver(action: SavedKillAction) -> DeliveryState | RetryNotice:
    from scripts.review_links.change_page import review_send_options

    options = review_send_options(f"builds-kill:{action.id}", f"staff:{action.recipient}")
    response = subprocess.run(session_send.send_argv("--to", f"staff:{action.recipient}", "--from", "nate",
                                                     *options, "--text", action.message),
                              capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL,
                              timeout=send_budget.seconds(), check=False)
    output = response.stdout
    if response.returncode == 0 and (output.startswith("SENT:") or
            output.startswith("SKIPPED:") and "postmaster confirmed delivery" in output):
        return DeliveryState.SENT
    if (response.returncode == 1 and output.startswith("QUEUED:")
            or response.returncode == 0 and output.startswith(("KEPT:", "SKIPPED:"))):
        return DeliveryState.QUEUED
    if response.returncode and any(line.startswith(("REFUSED:", "FAILED:", "HELD:"))
                                   for line in (output, response.stderr)):
        return DeliveryState.FAILED
    if response.returncode == 2 and response.stderr.startswith("send.py: "):
        return DeliveryState.FAILED
    return RetryNotice()


class KillActions:
    """Persist each stop, recover its notice, and publish recent results."""

    def __init__(self, root: Path) -> None:
        self._folder: Path = root / ".review-link-build-kill"
        self._key: bytes = secrets.token_bytes(32)
        self._lock: threading.Lock = threading.Lock()
        self._actions: dict[str, SavedKillAction] = {}

    def handle(self, target: KillTarget) -> str:
        data = repr((target.candidate.reference, target.route, target.recipient)).encode()
        return hmac.new(self._key, data, sha256).hexdigest()

    def _cache(self, action: SavedKillAction) -> None:
        with self._lock:
            self._actions[action.result["reference"]] = action

    def _read(self, reference: str) -> SavedKillAction:
        text = (self._folder / f"{reference}.json").read_text()
        raw = cast(object, json.loads(text))
        if not isinstance(raw, dict):
            raise ValueError("invalid Builds stop record")
        record = cast(dict[str, object], raw)
        if (record.get("stage") in ({"kind": "WatchingStop"}, {"kind": "SendingNotice"})
                or "scope" not in record and record.get("stage") == {"kind": "TermSent"}):
            record["stage"] = {"kind": "StopRequested"}
        action = cast(SavedKillAction, snapshot_codec.parse_snapshot_value(record, SavedKillAction))
        if action.result["reference"] != reference or action.run.reference != reference:
            raise ValueError("the Builds stop record has a different run")
        return action

    def _save(self, action: SavedKillAction) -> None:
        reference = action.result["reference"]
        write_text_atomic(self._folder / f"{reference}.json", admission_wire.encode_record(action), new_file_mode=0o600)
        self._cache(action)

    @contextmanager
    def _record_lock(self, reference: str) -> Generator[None]:
        self._folder.mkdir(exist_ok=True, mode=0o700)
        descriptor = os.open(self._folder / f"{reference}.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def options(self) -> tuple[dict[str, KillOffer], list[KillResult]]:
        with self._lock:
            now = time.time()
            for reference, action in tuple(self._actions.items()):
                if isinstance(action.stage, FinishedStop) and action.stage.visible_until <= now:
                    del self._actions[reference]
            actions = dict(self._actions)
        try:
            targets = live_targets()
        except (OSError, ValueError, admission_wire.AdmissionUnavailable):
            targets = {}
        now_in_zone = datetime.now(user_zone())
        offers: dict[str, KillOffer] = {
            reference: {"handle": self.handle(target), "pid": target.candidate.row_pid,
                        "started_by": target.starter, "recipient_name": target.recipient_name,
                        "step": target.candidate.step, "message": notification(target.candidate, now_in_zone)}
            for reference, target in targets.items()
            if reference not in actions or actions[reference].result["stop"] == StopState.NOT_SENT
        }
        return offers, [action.result for action in actions.values()]

    def _finish(self, action: SavedKillAction, result: KillResult, retention: float) -> SavedKillAction:
        finished = replace(action, result=result, stage=FinishedStop(time.time() + retention))
        self._save(finished)
        return finished

    def _advance(self, action: SavedKillAction, retention: float) -> SavedKillAction:
        """Record each step before proceeding. Recovery only checks or sends."""
        if isinstance(action.stage, StopRequested):
            return self._finish(action, {**action.result, "stop": StopState.NOT_SENT}, retention)
        if isinstance(action.stage, TermSent) and stop_complete(action):
            action = replace(action, stage=SendingNotice(action.stage))
            self._save(action)
        if isinstance(action.stage, SendingNotice):
            try:
                delivery = deliver(action)
            except (OSError, ValueError, subprocess.SubprocessError):
                return action
            if isinstance(delivery, RetryNotice):
                return action
            return self._finish(action, {**action.result, "stop": StopState.STOPPED, "delivery": delivery}, retention)
        return action

    def _watch(self, reference: str, sample: float, retention: float) -> None:
        while True:
            try:
                with self._record_lock(reference):
                    action = self._advance(self._read(reference), retention)
                    if isinstance(action.stage, FinishedStop):
                        self._cache(action)
                        return
            except FileNotFoundError:
                return
            except (OSError, ValueError) as error:
                print(f"review-links build kill {reference}: {error}", file=sys.stderr)
            time.sleep(sample)

    def _start_watch(self, reference: str, settings: conf.ConfigRead) -> None:
        threading.Thread(target=self._watch, args=(reference, settings.number("kill_pending_check_seconds"),
                         settings.number("kill_result_seconds")), daemon=True).start()

    def resume(self) -> None:
        for path in self._folder.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{64}", path.stem) is None:
                continue
            try:
                action = self._read(path.stem)
            except (OSError, ValueError) as error:
                print(f"review-links build kill {path}: {error}", file=sys.stderr)
                continue
            self._cache(action)
            if not isinstance(action.stage, FinishedStop):
                self._start_watch(path.stem, conf.read_or_repository("review_links"))

    def stop(self, reference: str, handle: str, words: str) -> KillResult | NoStopRoute:
        target = live_targets().get(reference)
        if target is None or not hmac.compare_digest(self.handle(target), handle):
            return NoStopRoute()
        if re.fullmatch(r"[0-9a-f]{64}", reference) is None:
            return NoStopRoute()
        settings = conf.read_or_repository("review_links")
        seconds = settings.number("kill_stop_seconds")
        sample = settings.number("kill_sample_seconds")
        retention = settings.number("kill_result_seconds")
        run = target.candidate
        at = datetime.now(user_zone())
        message = notification(run, at)
        identity = secrets.token_hex(16)
        result: KillResult = {"reference": reference, "step": run.step, "pid": run.row_pid,
                              "stop": StopState.STOPPING, "delivery": DeliveryState.PENDING,
                              "time": at.strftime("%H:%M %Z"), "recipient_name": target.recipient_name,
                              "recipient_role": target.recipient_role}
        body = f"{message}\n{words}" if words else message
        action = SavedKillAction(identity, run, result, target.recipient, body, StopRequested(), target.route.scope)
        with self._record_lock(reference):
            try:
                previous = self._read(reference)
            except FileNotFoundError:
                pass
            else:
                if not isinstance(previous.stage, FinishedStop):
                    return NoStopRoute()
                if previous.stage.visible_until > time.time() and previous.result["stop"] != StopState.NOT_SENT:
                    return NoStopRoute()
            self._save(action)
            try:
                started = stop_verified_scope(target, message)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"review-links build kill {reference}: {error}", file=sys.stderr)
                started = False
            if not started:
                return self._advance(action, retention).result
            action = replace(action, stage=TermSent())
            self._save(action)
            deadline = time.monotonic() + seconds
            while True:
                try:
                    action = self._advance(action, retention)
                except (OSError, ValueError):
                    break
                if isinstance(action.stage, FinishedStop):
                    return action.result
                remaining = deadline - time.monotonic()
                if remaining <= 0 or isinstance(action.stage, SendingNotice):
                    break
                time.sleep(min(remaining, sample))
            # A delivery write can fail after cleanup is saved. Resume the durable state.
            action = self._read(reference)
            if isinstance(action.stage, TermSent):
                action = replace(action, result={**result, "stop": StopState.UNCONFIRMED})
                self._save(action)
        self._start_watch(reference, settings)
        return action.result
