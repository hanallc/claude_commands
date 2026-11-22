"""Verify and stop the local run behind a Builds row."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import fcntl
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Literal, TypedDict, cast

from scripts.agents import detached_run_process, detached_runs, worker_lifecycle
from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.buildlog import store
from scripts.buildlog.parse import CARGO_BUILD_VERBS
from scripts.lib import conf, process_start, process_stop
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.build_steps import GATE_PROGRAMS, command_program, live_process_arguments
from scripts.lib.user_zone import user_zone
from scripts.lint import memory_admit
from scripts.message import send_budget, session_send
from scripts.production import staff


PATH = "/console/build-kill"
PROC = Path("/proc")
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
                         step.session_id, step.step, reservation["admitted_at"])


@dataclass(frozen=True)
class DetachedCommand:
    """A supervised command group that contains the recorded run."""

    record: Path
    group: int
    started: str
    supervisor: int
    supervisor_start: str
    scope: process_stop.ScopeIdentity


@dataclass(frozen=True)
class NoStopRoute:
    """The run has no verified route for stopping its processes."""


@dataclass(frozen=True)
class KillTarget:
    candidate: KillCandidate
    route: DetachedCommand
    recipient: staff.StaffId
    starter: str
    recipient_name: str
    recipient_role: str


def live(pid: int, started: str) -> bool:
    return process_start.proc_table_start(pid, PROC) == process_start.Started(started)


def program(pid: int, processes: dict[int, memory_admit.ProcessIdentity]) -> str:
    args = live_process_arguments(PROC / str(pid), processes[pid].start)
    return command_program(args) if isinstance(args, list) else ""


def detached_launch_scope(value: object) -> process_stop.ScopeIdentity | None:
    """Decode the scope that the detached launcher saved at startup."""
    if process_stop.is_scope_identity(value):
        path = Path(value["path"])
        unit = value["unit"]
        if (value["device"] >= 0 and value["inode"] > 0
                and path.is_absolute() and ".." not in path.parts
                and path.parent.name == "builds-detached.slice" and path.name == unit
                and unit.endswith(".scope")):
            return value
    return None


def detached_command(run: KillCandidate, recipient: staff.StaffId,
                     processes: dict[int, memory_admit.ProcessIdentity],
                     descendants: set[int]) -> DetachedCommand | NoStopRoute:
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
            if (scope is not None
                    and group is not None and group.group == pid and group.started == processes[pid].start
                    and raw.get("command_pid") == pid and raw.get("staff_id") == recipient
                    and isinstance(supervisor := raw.get("pid"), int) and supervisor == parent
                    and isinstance(start := raw.get("supervisor_started"), str)
                    and live(supervisor, start)
                    and detached_run_process.supervisor_match(supervisor, path) is True
                    and detached_build(raw.get("command"), program(pid, processes))
                    and all(in_group(child, pid) for child in descendants)):
                try:
                    unit = process_stop.ScopeUnit("builds-detached.slice", scope["unit"])
                    with process_stop.pin_process(process_stop.ProcessLifetime(run.holder_pid, run.holder_start)):
                        if (process_stop.command_scope(run.holder_pid, unit) == scope
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
               processes: dict[int, memory_admit.ProcessIdentity]) -> DetachedCommand | NoStopRoute:
    """Require a live Cargo process with a dedicated stop boundary."""
    if not process_stop.supported():
        return NoStopRoute()
    if not live(run.holder_pid, run.holder_start) or not live(run.row_pid, run.row_start):
        return NoStopRoute()
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
    ALREADY_ENDED = "Run already ended"


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
    stop: Literal[StopState.STOPPING, StopState.STOPPED, StopState.UNCONFIRMED, StopState.ALREADY_ENDED]
    delivery: Literal[DeliveryState.PENDING, DeliveryState.SENT, DeliveryState.QUEUED, DeliveryState.FAILED]
    time: str
    recipient_name: str
    recipient_role: str


@dataclass(frozen=True)
class StopRequested:
    """The action records the request before it sends TERM."""


@dataclass(frozen=True)
class SendingNotice:
    """The run ended after acceptance. The notice is pending."""


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
    stage: StopRequested | SendingNotice | FinishedStop
    route: DetachedCommand


def notification(run: KillCandidate, at: datetime) -> str:
    return f'nate killed your run "{run.step}" (pid {run.row_pid}) from the Builds page at {at:%H:%M %Z}'


@dataclass(frozen=True)
class RunningDetachedRun:
    scope_end_proved: bool


@dataclass(frozen=True)
class EndedDetachedRun:
    """The record shows completion or was pruned."""


def detached_run_state(route: DetachedCommand, recipient: staff.StaffId) -> RunningDetachedRun | EndedDetachedRun:
    try:
        raw = cast(object, json.loads(route.record.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return EndedDetachedRun()
    if not isinstance(raw, dict):
        raise ValueError("the detached run record is not an object")
    record = cast(dict[str, object], raw)
    if (record.get("run_id") != route.record.stem or record.get("staff_id") != recipient
            or detached_launch_scope(record.get("command_scope")) != route.scope):
        raise ValueError("the detached run record changed identity")
    if "finished_at" in record or record.get("status") != "running":
        return EndedDetachedRun()
    return RunningDetachedRun(record.get("scope_end_proved") is True)


def stop_verified_scope(target: KillTarget, reason: str) -> bool:
    """Check the live route again before the detached helper stops it."""
    def route_matches() -> bool:
        return stop_route(target.candidate, target.recipient, memory_admit.process_table(PROC)) == target.route

    if not route_matches():
        return False
    return detached_runs.stop_command(target.route.record, reason, check=route_matches)


def resume_stop(action: SavedKillAction) -> bool:
    """Request the stop again against the same saved run record."""
    return detached_runs.stop_command(
        action.route.record, action.message.partition("\n")[0],
        check=lambda: isinstance(detached_run_state(action.route, action.recipient), RunningDetachedRun),
    )


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


@dataclass(frozen=True)
class DetachedKillOffer:
    target: KillTarget
    expires_at: float


class KillActions:
    """Persist each stop, recover its notice, and publish recent results."""

    def __init__(self, root: Path) -> None:
        self._folder: Path = root / ".review-link-build-kill"
        self._key: bytes = secrets.token_bytes(32)
        self._lock: threading.Lock = threading.Lock()
        self._actions: dict[str, SavedKillAction] = {}
        self._offers: dict[str, DetachedKillOffer] = {}

    def handle(self, target: KillTarget) -> str:
        data = repr((target.candidate.reference, target.route, target.recipient)).encode()
        return hmac.new(self._key, data, sha256).hexdigest()

    def _cache(self, action: SavedKillAction) -> None:
        with self._lock:
            self._actions[action.result["reference"]] = action

    def _read(self, reference: str) -> SavedKillAction:
        text = (self._folder / f"{reference}.json").read_text()
        action = admission_wire.parse_record(text, SavedKillAction)
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
            self._offers = {reference: offer for reference, offer in self._offers.items() if offer.expires_at > now}
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
            if reference not in actions
        }
        if offers:
            deadline = now + conf.read_or_repository("review_links").number("kill_result_seconds")
            with self._lock:
                self._offers.update({reference: DetachedKillOffer(targets[reference], deadline) for reference in offers})
        return offers, [action.result for action in actions.values()]

    def _finish(self, action: SavedKillAction, result: KillResult, retention: float) -> SavedKillAction:
        finished = replace(action, result=result, stage=FinishedStop(time.time() + retention))
        self._save(finished)
        return finished

    def _advance(
        self, action: SavedKillAction, retention: float, request_stop: Callable[[SavedKillAction], bool],
    ) -> SavedKillAction:
        """Use the run record to advance to the notice."""
        if isinstance(action.stage, StopRequested):
            state = detached_run_state(action.route, action.recipient)
            if isinstance(state, RunningDetachedRun) and not state.scope_end_proved:
                try:
                    accepted = request_stop(action)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print(f"review-links build kill {action.run.reference}: {error}", file=sys.stderr)
                    accepted = False
                state = detached_run_state(action.route, action.recipient)
                if (isinstance(state, RunningDetachedRun) and not accepted
                        and action.result["stop"] != StopState.UNCONFIRMED):
                    action = replace(action, result={**action.result, "stop": StopState.UNCONFIRMED})
                    self._save(action)
            if isinstance(state, RunningDetachedRun):
                return action
            action = replace(action, stage=SendingNotice(), result={**action.result, "stop": StopState.STOPPED})
            self._save(action)
        if isinstance(action.stage, SendingNotice):
            try:
                delivery = deliver(action)
            except (OSError, ValueError, subprocess.SubprocessError):
                return action
            if isinstance(delivery, RetryNotice):
                return action
            return self._finish(action, {**action.result, "delivery": delivery}, retention)
        return action

    def _watch(self, reference: str, sample: float, retention: float) -> None:
        while True:
            try:
                with self._record_lock(reference):
                    try:
                        action = self._read(reference)
                    except FileNotFoundError:
                        return
                    action = self._advance(action, retention, resume_stop)
                    if isinstance(action.stage, FinishedStop):
                        self._cache(action)
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
        if re.fullmatch(r"[0-9a-f]{64}", reference) is None:
            return NoStopRoute()
        with self._lock:
            offered = self._offers.get(reference)
        if offered is None or offered.expires_at <= time.time():
            return NoStopRoute()
        target = offered.target
        if not hmac.compare_digest(self.handle(target), handle):
            return NoStopRoute()
        settings = conf.read_or_repository("review_links")
        retention = settings.number("kill_result_seconds")
        run = target.candidate
        with self._record_lock(reference):
            try:
                previous = self._read(reference)
            except FileNotFoundError:
                pass
            else:
                if not isinstance(previous.stage, FinishedStop) or previous.stage.visible_until > time.time():
                    return NoStopRoute()
            state = detached_run_state(target.route, target.recipient)
            if isinstance(state, RunningDetachedRun):
                current = live_targets().get(reference)
                if (current is None or self.handle(current) != handle) and isinstance(
                        detached_run_state(target.route, target.recipient), RunningDetachedRun):
                    return NoStopRoute()
            at = datetime.now(user_zone())
            message = notification(run, at)
            result: KillResult = {"reference": reference, "step": run.step, "pid": run.row_pid,
                                  "stop": StopState.STOPPING, "delivery": DeliveryState.PENDING,
                                  "time": at.strftime("%H:%M %Z"), "recipient_name": target.recipient_name,
                                  "recipient_role": target.recipient_role}
            body = f"{message}\n{words}" if words else message
            action = SavedKillAction(secrets.token_hex(16), run, result, target.recipient, body, StopRequested(),
                                     target.route)
            if isinstance(state, EndedDetachedRun):
                return self._finish(action, {**result, "stop": StopState.ALREADY_ENDED}, retention).result
            self._save(action)
            try:
                action = self._advance(action, retention, lambda _action: stop_verified_scope(target, message))
            except (OSError, ValueError) as error:
                print(f"review-links build kill {reference}: {error}", file=sys.stderr)
                action = self._read(reference)
            if isinstance(action.stage, FinishedStop):
                return action.result
        self._start_watch(reference, settings)
        return action.result
