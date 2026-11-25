"""Verify and stop the local run behind a Builds row."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Generator, Iterator
from contextlib import ExitStack, contextmanager
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
import signal
import subprocess
import sys
import threading
import time
from typing import Literal, NotRequired, TypedDict, cast

from scripts.agents import detached_run_process, detached_runs, worker_lifecycle
from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.buildlog import store
from scripts.buildlog.parse import CARGO_BUILD_VERBS
from scripts.lib import atomic_file, process_start, process_stop
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
class OutsideKillCandidate:
    row_pid: int
    row_start: str
    step: str

    @property
    def reference(self) -> str:
        return sha256(json.dumps(("outside", self.row_pid, self.row_start)).encode()).hexdigest()


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
    """A supervised command that owns the verified run slice."""

    record: Path
    group: int
    started: str
    supervisor: int
    supervisor_start: str
    run_slice: process_stop.RunSliceIdentity


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


@dataclass(frozen=True)
class OutsideKillTarget:
    candidate: OutsideKillCandidate
    recipient: staff.StaffId
    starter: str
    recipient_name: str
    recipient_role: str


def notice_recipient(members: staff.StaffSnapshot, caller: staff.StaffRow) -> tuple[staff.StaffId, str, str, str]:
    role = "unit director" if caller.kind == "unit" else caller.kind
    parent = members.parent_of(caller)
    starter = f"{caller.name}, a {role}"
    if caller.kind == "worker" and parent is not None:
        starter += f" of {parent.name}"
    return caller.id, starter, caller.name, role


@dataclass(frozen=True)
class OutsideProcessTree:
    boot_id: str
    processes: tuple[process_stop.ProcessLifetime, ...]


def boot_id() -> str:
    return (PROC / "sys/kernel/random/boot_id").read_text().strip()


def outside_process_tree(run: OutsideKillCandidate) -> OutsideProcessTree:
    root = process_stop.ProcessLifetime(run.row_pid, run.row_start)
    with process_stop.pin_process(root):
        processes = memory_admit.process_table(PROC)
        # Exclude links to a parent PID from a later process lifetime.
        processes = {pid: entry for pid, entry in processes.items()
                     if entry.parent not in processes or int(processes[entry.parent].start) <= int(entry.start)}
        tree = memory_admit.process_tree({root.pid: root.start}, processes)
        if root.pid not in tree or os.getpid() in tree or not live(root.pid, root.start):
            raise ProcessLookupError("the outside command changed")
        children = tuple(process_stop.ProcessLifetime(pid, processes[pid].start) for pid in sorted(tree - {root.pid}))
        return OutsideProcessTree(boot_id(), (root, *children))


def stop_outside_tree(tree: OutsideProcessTree) -> bool:
    """Signal only the saved process lifetimes. Return true when all have ended."""
    if tree.boot_id != boot_id():
        raise ProcessLookupError("the system restarted")
    with ExitStack() as pins:
        descriptors: list[int] = []
        for process in tree.processes:
            try:
                descriptors.append(pins.enter_context(process_stop.pin_process(process)))
            except ProcessLookupError:
                if isinstance(process_start.proc_table_start(process.pid, PROC), process_start.Unreadable):
                    raise
        for descriptor in descriptors:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                continue
    return not descriptors



def live(pid: int, started: str) -> bool:
    return process_start.proc_table_start(pid, PROC) == process_start.Started(started)


def program(pid: int, processes: dict[int, memory_admit.ProcessIdentity]) -> str:
    args = live_process_arguments(PROC / str(pid), processes[pid].start)
    return command_program(args) if isinstance(args, list) else ""


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
                if raw.get("completion_after_slice_cleanup") is not True:
                    return NoStopRoute()
                group = detached_runs.command_group(path)
            except (OSError, ValueError):
                return NoStopRoute()
            run_slice = raw.get("command_slice")
            if (process_stop.is_slice_identity(run_slice)
                    and group is not None and group.group == pid and group.started == processes[pid].start
                    and raw.get("command_pid") == pid and raw.get("staff_id") == recipient
                    and isinstance(supervisor := raw.get("pid"), int) and supervisor == parent
                    and isinstance(start := raw.get("supervisor_started"), str)
                    and live(supervisor, start)
                    and detached_run_process.supervisor_match(supervisor, path) is True
                    and detached_build(raw.get("command"), program(pid, processes))
                    and all(in_group(child, pid) for child in descendants)):
                try:
                    with process_stop.pin_process(process_stop.ProcessLifetime(run.holder_pid, run.holder_start)):
                        cgroup = (PROC / str(run.holder_pid) / "cgroup").read_text()
                        unit = next((Path(line[3:]).name for line in cgroup.splitlines() if line.startswith("0::/")), "")
                        member_scope = process_stop.ScopeUnit(run_slice["unit"], unit)
                        if (process_stop.command_slice(run.holder_pid, member_scope) == run_slice
                                and live(run.holder_pid, run.holder_start)):
                            return DetachedCommand(path, pid, group.started, supervisor, start, run_slice)
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
            result[run.reference] = KillTarget(run, route, *notice_recipient(members, caller))
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
    reason: NotRequired[str]


@dataclass(frozen=True)
class StopRequested:
    """The action records the request before it sends TERM."""


@dataclass(frozen=True)
class SendingNotice:
    """The run ended after acceptance. The notice is pending."""


@dataclass(frozen=True)
class FinishedStop:
    """The action is complete."""

    finished_at: float = 0


@dataclass(frozen=True)
class SavedKillAction:
    id: str
    run: KillCandidate | OutsideKillCandidate
    result: KillResult
    recipient: staff.StaffId
    message: str
    stage: StopRequested | SendingNotice | FinishedStop
    route: DetachedCommand | OutsideProcessTree


def notification(run: KillCandidate | OutsideKillCandidate, at: datetime) -> str:
    return f'nate killed your run "{run.step}" (pid {run.row_pid}) from the Builds page at {at:%H:%M %Z}'


@dataclass(frozen=True)
class RunningDetachedRun:
    """The record still shows an active run."""

    stop_committed: bool


@dataclass(frozen=True)
class EndedDetachedRun:
    """The record shows completion or was pruned."""

    stop_committed: bool


@dataclass(frozen=True)
class ChangedDetachedRun:
    """The record lacks the saved identity or the completion contract."""


def detached_run_state(
    route: DetachedCommand, recipient: staff.StaffId, *, notice_id: str = "",
) -> RunningDetachedRun | EndedDetachedRun | ChangedDetachedRun:
    try:
        raw = cast(object, json.loads(route.record.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return EndedDetachedRun(stop_committed=False)
    if not isinstance(raw, dict):
        return ChangedDetachedRun()
    record = cast(dict[str, object], raw)
    run_slice = record.get("command_slice")
    if (record.get("completion_after_slice_cleanup") is not True
            or record.get("run_id") != route.record.stem or record.get("staff_id") != recipient
            or not process_stop.is_slice_identity(run_slice) or run_slice != route.run_slice):
        return ChangedDetachedRun()
    committed = (bool(notice_id) and record.get("stop_notice_id") == notice_id
                 and record.get("stop_pass_requested") is True)
    if record.get("status") == "running":
        return RunningDetachedRun(stop_committed=committed)
    return EndedDetachedRun(stop_committed=committed)


def request_stop(action: SavedKillAction) -> bool:
    """Ask the detached run stop path to stop the saved command."""
    route, run = action.route, action.run
    if not isinstance(route, DetachedCommand) or not isinstance(run, KillCandidate):
        return False

    def can_stop() -> bool:
        state = detached_run_state(route, action.recipient, notice_id=action.id)
        return isinstance(state, RunningDetachedRun) and (
            state.stop_committed
            or stop_route(run, action.recipient, memory_admit.process_table(PROC)) == route)

    return detached_runs.stop_command(route.record, action.message.partition("\n")[0], check=can_stop,
                                      stop_notice_id=action.id)


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
class CachedKillOffer:
    target: KillTarget | OutsideKillTarget
    expires_at: float


class KillActions:
    """Persist each stop, recover its notice, and publish recent results."""

    def __init__(self, root: Path,
                 outside_targets: Callable[[], dict[str, OutsideKillTarget]] | None = None) -> None:
        self._outside_targets: Callable[[], dict[str, OutsideKillTarget]] | None = outside_targets
        self._folder: Path = root / ".review-link-build-kill"
        self._key: bytes = secrets.token_bytes(32)
        self._lock: threading.Lock = threading.Lock()
        self._actions: dict[str, SavedKillAction] = {}
        self._offers: dict[str, CachedKillOffer] = {}
        self._retry: set[str] = set()
        self._worker: threading.Thread | None = None

    def handle(self, target: KillTarget | OutsideKillTarget) -> str:
        identity = target.route if isinstance(target, KillTarget) else target.candidate
        data = repr((target.candidate.reference, identity, target.recipient)).encode()
        return hmac.new(self._key, data, sha256).hexdigest()

    def _cache(self, action: SavedKillAction) -> None:
        with self._lock:
            self._actions[action.run.reference] = action

    def _read(self, reference: str) -> SavedKillAction:
        text = (self._folder / f"{reference}.json").read_text()
        action = admission_wire.parse_record(text, SavedKillAction)
        if action.result["reference"] != reference or action.run.reference != reference:
            raise ValueError("the Builds stop record has a different run")
        if re.fullmatch(r"[0-9a-f]{32}", action.id) is None:
            raise ValueError("the Builds stop record has an invalid notice ID")
        return action

    def _save(self, action: SavedKillAction) -> None:
        reference = action.result["reference"]
        atomic_file.write_text_atomic(self._folder / f"{reference}.json", admission_wire.encode_record(action),
                                      new_file_mode=0o600)
        self._cache(action)

    def _notice_path(self, reference: str, notice_id: str) -> Path:
        return self._folder / f"{reference}.{notice_id}.notice"

    def _track_notice(self, action: SavedKillAction) -> None:
        assert isinstance(action.route, DetachedCommand)
        # Save the run path before the stop commit. The action file can disappear later.
        path = self._notice_path(action.run.reference, action.id)
        if not path.exists():
            atomic_file.write_text_atomic(path, str(action.route.record), new_file_mode=0o600)

    def _acknowledge_notice(self, record: Path, reference: str, notice_id: str) -> None:
        path = self._notice_path(reference, notice_id)
        if not path.exists():
            return
        try:
            # The atomic write syncs the file before replacement. Confirm the directory sync.
            atomic_file.sync_folder(self._folder)
            detached_runs.acknowledge_stop_notice(record, notice_id)
            path.unlink(missing_ok=True)
        except (OSError, ValueError, SystemExit) as error:
            print(f"review-links build kill {notice_id}: cannot acknowledge the stop notice: {error}", file=sys.stderr)

    def _release_notices(self, reference: str, active_id: str = "") -> None:
        for path in self._notice_files(reference):
            notice_id = path.stem.partition(".")[2]
            if notice_id != active_id:
                try:
                    record = Path(path.read_text())
                    if not record.is_absolute() or record.parent.name != detached_runs.DETACHED_RUNS_DIR:
                        raise ValueError("the Builds notice has an invalid run path")
                    self._acknowledge_notice(record, reference, notice_id)
                except (OSError, ValueError) as error:
                    print(f"review-links build kill {path}: {error}", file=sys.stderr)

    def _notice_files(self, reference: str) -> Iterator[Path]:
        return (path for path in self._folder.glob(f"{reference}.*.notice")
                if re.fullmatch(r"[0-9a-f]{32}", path.stem.partition(".")[2]) is not None)

    @contextmanager
    def _record_lock(self, reference: str) -> Generator[None]:
        self._folder.mkdir(exist_ok=True, mode=0o700)
        descriptor = os.open(self._folder / f"{reference}.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def options(self, refresh_seconds: float) -> tuple[dict[str, KillOffer], list[KillResult]]:
        now = time.time()
        cutoff = now - 2 * refresh_seconds
        with self._lock:
            for reference, action in tuple(self._actions.items()):
                if isinstance(action.stage, FinishedStop) and action.stage.finished_at <= cutoff:
                    del self._actions[reference]
            actions = dict(self._actions)
            self._offers = {reference: offer for reference, offer in self._offers.items() if offer.expires_at > now}
        targets: dict[str, KillTarget | OutsideKillTarget] = {}
        try:
            targets.update(live_targets())
            if self._outside_targets is not None:
                targets.update(self._outside_targets())
        except (OSError, ValueError, admission_wire.AdmissionUnavailable):
            pass
        now_in_zone = datetime.now(user_zone())
        offers: dict[str, KillOffer] = {
            reference: {"handle": self.handle(target), "pid": target.candidate.row_pid,
                        "started_by": target.starter, "recipient_name": target.recipient_name,
                        "step": target.candidate.step, "message": notification(target.candidate, now_in_zone)}
            for reference, target in targets.items()
            if reference not in actions or (
                isinstance(target, OutsideKillTarget) and actions[reference].result["stop"] == StopState.UNCONFIRMED)
        }
        if offers:
            deadline = now + 2 * refresh_seconds
            with self._lock:
                self._offers.update({reference: CachedKillOffer(targets[reference], deadline) for reference in offers})
        return offers, [action.result for action in actions.values()]

    def _finish(self, action: SavedKillAction, result: KillResult) -> SavedKillAction:
        finished = replace(action, result=result, stage=FinishedStop(time.time()))
        self._save(finished)
        if isinstance(action.stage, StopRequested) and isinstance(action.route, DetachedCommand):
            self._acknowledge_notice(action.route.record, action.run.reference, action.id)
        return finished

    def _advance(self, action: SavedKillAction, *, recover_orphan: bool = False) -> SavedKillAction:
        if isinstance(action.stage, StopRequested):
            route = action.route
            if isinstance(route, OutsideProcessTree):
                try:
                    ended = stop_outside_tree(route)
                except OSError as error:
                    print(f"review-links build kill {action.run.reference}: {error}", file=sys.stderr)
                    return self._finish(action, {**action.result, "stop": StopState.UNCONFIRMED,
                                                 "reason": "The command could not be stopped."})
                if ended:
                    action = self._stopped(action)
            else:
                action = self._advance_detached(action, route, recover_orphan=recover_orphan)
        if isinstance(action.stage, SendingNotice):
            if isinstance(action.route, DetachedCommand):
                self._acknowledge_notice(action.route.record, action.run.reference, action.id)
            try:
                delivery = deliver(action)
            except (OSError, ValueError, subprocess.SubprocessError):
                return action
            if isinstance(delivery, RetryNotice):
                return action
            return self._finish(action, {**action.result, "delivery": delivery})
        return action

    def _stopped(self, action: SavedKillAction) -> SavedKillAction:
        action = replace(action, stage=SendingNotice(), result={**action.result, "stop": StopState.STOPPED})
        self._save(action)
        return action

    def _advance_detached(self, action: SavedKillAction, route: DetachedCommand, *,
                          recover_orphan: bool) -> SavedKillAction:
        if action.result["stop"] != StopState.STOPPING:
            action = replace(action, result={**action.result, "stop": StopState.STOPPING})
            self._save(action)
        self._track_notice(action)
        state = detached_run_state(route, action.recipient, notice_id=action.id)
        if isinstance(state, RunningDetachedRun):
            retry = False
            try:
                _ = request_stop(action)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"review-links build kill {action.run.reference}: {error}", file=sys.stderr)
                retry = True
            state = detached_run_state(route, action.recipient, notice_id=action.id)
            if recover_orphan and isinstance(state, RunningDetachedRun) and state.stop_committed:
                detached_runs.reap_orphan(route.record)
                state = detached_run_state(route, action.recipient, notice_id=action.id)
            if isinstance(state, RunningDetachedRun):
                if not state.stop_committed and not retry:
                    return self._finish(action, {**action.result, "stop": StopState.UNCONFIRMED})
                return action
        if isinstance(state, ChangedDetachedRun):
            return self._finish(action, {**action.result, "stop": StopState.UNCONFIRMED})
        if not state.stop_committed:
            return self._finish(action, {**action.result, "stop": StopState.ALREADY_ENDED})
        if not process_stop.slice_has_ended(route.run_slice):
            return action
        return self._stopped(action)

    def _recover(self, references: tuple[str, ...]) -> None:
        for reference in references:
            try:
                with self._record_lock(reference):
                    try:
                        action = self._read(reference)
                    except FileNotFoundError:
                        self._release_notices(reference)
                    else:
                        if isinstance(action.stage, FinishedStop):
                            self._release_notices(reference)
                        else:
                            self._release_notices(reference, action.id)
                            action = self._advance(action, recover_orphan=True)
                            if not isinstance(action.stage, FinishedStop):
                                continue
                    if not any(self._notice_files(reference)):
                        with self._lock:
                            self._retry.discard(reference)
            except (OSError, ValueError) as error:
                print(f"review-links build kill {reference}: {error}", file=sys.stderr)

    def retry_pending(self) -> None:
        """Start one recovery pass from the server maintenance cycle."""
        with self._lock:
            if self._retry and (self._worker is None or not self._worker.is_alive()):
                self._worker = threading.Thread(target=self._recover, args=(tuple(self._retry),), daemon=True)
                self._worker.start()

    def resume(self) -> None:
        references = {path.stem for path in self._folder.glob("*.json")}
        references.update(path.name.partition(".")[0] for path in self._folder.glob("*.notice"))
        for reference in sorted(references):
            if re.fullmatch(r"[0-9a-f]{64}", reference) is None:
                continue
            try:
                with self._record_lock(reference):
                    active_id = ""
                    try:
                        action = self._read(reference)
                    except FileNotFoundError:
                        pass
                    else:
                        self._cache(action)
                        if not isinstance(action.stage, FinishedStop):
                            active_id = action.id
                    self._release_notices(reference, active_id)
                    if active_id or any(self._notice_files(reference)):
                        with self._lock:
                            self._retry.add(reference)
            except (OSError, ValueError) as error:
                print(f"review-links build kill {reference}: {error}", file=sys.stderr)
                continue
        self.retry_pending()

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
        run = target.candidate
        with self._record_lock(reference):
            try:
                previous = self._read(reference)
            except FileNotFoundError:
                pass
            else:
                if previous.result["stop"] != StopState.UNCONFIRMED or not isinstance(previous.stage, FinishedStop):
                    return NoStopRoute()
            if isinstance(target, OutsideKillTarget):
                current = self._outside_targets().get(reference) if self._outside_targets is not None else None
                if current is None or self.handle(current) != handle:
                    return NoStopRoute()
                try:
                    route = outside_process_tree(target.candidate)
                except ProcessLookupError:
                    return NoStopRoute()
                ended = False
            else:
                state = detached_run_state(target.route, target.recipient)
                if isinstance(state, ChangedDetachedRun):
                    return NoStopRoute()
                if isinstance(state, RunningDetachedRun):
                    current = live_targets().get(reference)
                    if current is None or self.handle(current) != handle:
                        state = detached_run_state(target.route, target.recipient)
                        if not isinstance(state, EndedDetachedRun):
                            return NoStopRoute()
                route = target.route
                ended = isinstance(state, EndedDetachedRun)
            at = datetime.now(user_zone())
            message = notification(run, at)
            result: KillResult = {"reference": reference, "step": run.step, "pid": run.row_pid,
                                  "stop": StopState.STOPPING, "delivery": DeliveryState.PENDING,
                                  "time": at.strftime("%H:%M %Z"), "recipient_name": target.recipient_name,
                                  "recipient_role": target.recipient_role}
            body = f"{message}\n{words}" if words else message
            action = SavedKillAction(secrets.token_hex(16), run, result, target.recipient, body, StopRequested(),
                                     route)
            if ended:
                return self._finish(action, {**result, "stop": StopState.ALREADY_ENDED}).result
            self._save(action)
            try:
                action = self._advance(action)
            except (OSError, ValueError) as error:
                print(f"review-links build kill {reference}: {error}", file=sys.stderr)
                action = self._read(reference)
            if not isinstance(action.stage, FinishedStop) or any(self._notice_files(reference)):
                with self._lock:
                    self._retry.add(reference)
            return action.result
