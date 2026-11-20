"""Alert once when the queue head waits while holders keep memory and nothing starts."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal, cast

from scripts.build_hold import build_hold
from scripts.build_queue import admission_records as records
from scripts.build_queue.queue_evidence import gib
from scripts.build_queue.queue_snapshot import QueueSnapshot, read_queue_snapshot
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.buildlog import store
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import memory_admit as gate

SENDER = "head-stall watch"
TIMES_TAIL_BYTES = 1 << 20
PROC = gate.PROC

type AlertRecipient = Literal["builds", "bob"]
ALERT_RECIPIENTS: tuple[AlertRecipient, ...] = ("builds", "bob")


@dataclass(frozen=True)
class CompleteCompilerScan:
    """The capture has no unreadable compiler readings."""


@dataclass(frozen=True)
class UnreadableCompilers:
    count: int


@dataclass(frozen=True)
class CompilerScanCountUnavailable:
    """The alert cannot count the unreadable compilers."""


type CompilerScan = CompleteCompilerScan | UnreadableCompilers | CompilerScanCountUnavailable


@dataclass(frozen=True)
class HolderAdmissionUnavailable:
    """The snapshot has no admission time for the holder."""


@dataclass(frozen=True)
class Holder:
    steps: tuple[records.RunningStep, ...]
    memory: records.RunMemory
    keep_aside: int | records.ReadingUnavailable
    held_for: float | HolderAdmissionUnavailable


@dataclass(frozen=True)
class HeadStall:
    """The head waited at least the limit while reservations held memory and no step started."""
    last_start: float
    step: str
    folder: str
    pid: int
    need: int
    room: records.RoomCheck | records.ReadingUnavailable
    reason: records.WaitReason
    waited: float
    quiet: float
    holders: tuple[Holder, ...]
    compiler_scan: CompilerScan


@dataclass(frozen=True)
class NoStall:
    pass


@dataclass(frozen=True)
class StallPaths:
    admission: Path
    state: Path


def default_paths() -> StallPaths:
    return StallPaths(store.root() / "admission", store.root() / "head_stall.json")


def configured_limits() -> tuple[float, float]:
    """The stall limit and the oldest usable snapshot age, in seconds."""
    settings = conf.read("build_admission")
    return settings.number("head_stall_alert_minutes") * 60, settings.number("silence_seconds")


def last_admission(times: Path) -> float:
    """The newest admitted time of a step with memory need in the tail of queue_times.jsonl; 0 when none.

    A merge gate or targeted test is admitted with zero need every few minutes; it frees nothing for the head.
    """
    try:
        with times.open("rb") as stream:
            size = stream.seek(0, 2)
            _ = stream.seek(max(0, size - TIMES_TAIL_BYTES))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        return 0.0
    for line in reversed(lines):
        if '"kind":"admitted"' not in line:
            continue
        try:
            row = cast(object, json.loads(line))
        except ValueError:
            continue
        if isinstance(row, dict):
            fields = cast(dict[str, object], row)
            value, need = fields.get("admitted_at"), fields.get("need")
            if isinstance(value, (int, float)) and isinstance(need, int) and need > 0:
                return float(value)
    return 0.0


def _compiler_scan(running: tuple[records.RunningStep, ...]) -> CompilerScan:
    for step in running:
        memory = step.measured
        if not isinstance(memory, records.CompilerServiceMemory):
            continue
        source = memory.anonymous_source
        if isinstance(source, records.ScopeSource):
            if all(isinstance(job.use, records.AnonymousRunUse) for job in memory.jobs):
                return CompleteCompilerScan()
            members = source.members
        else:
            path = gate.builds_slice_cgroup() / "sccache.service" / "cgroup.procs"
            gate.check_memory_reading(path)
            try:
                members = frozenset(int(pid) for pid in path.read_text().split())
            except (OSError, ValueError):
                return CompilerScanCountUnavailable()
        unreadable = 0
        for pid in members:
            scope = gate.named_step_scope(pid, PROC)
            if isinstance(scope, records.ReadingUnavailable) and pid != memory.pid:
                unreadable += 1
            elif isinstance(scope, Path):
                use = gate.process_memory({pid}, PROC).anonymous
                if isinstance(use, records.RunUseUnavailable):
                    unreadable += 1
        # A compiler can end after the capture. A later read cannot prove that the capture was complete.
        return UnreadableCompilers(unreadable) if unreadable else CompilerScanCountUnavailable()
    return CompilerScanCountUnavailable()


def find_stall(snapshot: QueueSnapshot, admitted: float, now: float, limit: float) -> HeadStall | NoStall:
    inputs = snapshot.inputs
    if not inputs.ordered or not (inputs.reservations or inputs.slots):
        return NoStall()
    last_running_start = max((step.started_at for step in inputs.running if step.promise > 0), default=0.0)
    last_start = max(admitted, last_running_start)
    quiet = now - last_start
    if quiet < limit:
        return NoStall()

    result = gate.admission_pass(replace(inputs, blockers_for=frozenset()), inputs.now, snapshot.policy)
    candidate = result.policy.queued_head
    if isinstance(candidate, gate.NoQueuedHead):
        return NoStall()
    head = candidate.waiter
    waited = now - head["since"]
    if waited < limit:
        return NoStall()

    owner = records.waiter_owner(head)
    head_row = next(row for row in result.rows if row.owner == owner)
    room = result.policy.room
    head_room = head_row.decision.room if isinstance(room, records.MemoryRoom) else room
    holders = memory_holders(inputs, result.policy, now)
    if not holders:
        return NoStall()
    return HeadStall(last_start, head.get("step", "step"), head.get("folder", ""),
                     head["pid"], head["need"], head_room, head_row.reason, waited, quiet, holders,
                     _compiler_scan(inputs.running))


def memory_holders(inputs: records.QueueInputs, policy: gate.HeadPolicy, now: float) -> tuple[Holder, ...]:
    """List the runs with active reservations or memory held between steps."""
    steps_by_owner: dict[str, list[records.RunningStep]] = {}
    reserved_steps = (step for step in inputs.running if gate.running_has_reservation(step))
    for row, step in zip(inputs.reservations, reserved_steps, strict=True):
        if step.promise <= 0:
            continue
        owner = gate.reservation_owner(row, step)
        steps_by_owner.setdefault(owner, []).append(step)
    room = policy.room
    if isinstance(room, records.MemoryRoom):
        charges = tuple((run.memory, run.bytes) for run in room.runs)
    else:
        slots = tuple(slot for slot in inputs.slots if not isinstance(slot, records.ContinuationSlot)
                      or slot.slot.reservation not in policy.returned_claims)
        charges = tuple((memory, room) for memory in gate.run_memories(replace(inputs, slots=slots)))
    holders: list[Holder] = []
    for memory, keep_aside in charges:
        steps = steps_by_owner.get(memory.owner, [])
        if not steps and not memory.held:
            continue
        started_at = min((step.started_at for step in steps), default=0.0)
        held_for = max(0.0, now - started_at) if started_at > 0 else HolderAdmissionUnavailable()
        holders.append(Holder(tuple(steps), memory, keep_aside, held_for))
    return tuple(holders)


def current_stall(paths: StallPaths, now: float, limit: float, freshest: float) -> HeadStall | NoStall:
    snapshot = read_queue_snapshot(paths.admission)
    if not isinstance(snapshot, QueueSnapshot) or now - snapshot.written_at > freshest:
        return NoStall()
    return find_stall(snapshot, last_admission(paths.admission / "queue_times.jsonl"), now, limit)


def _folder_name(folder: str) -> str:
    return folder.rstrip("/").rpartition("/")[2] or "unknown folder"


def _holder_step(step: records.RunningStep) -> str:
    pid = f"pid {step.pid}" if isinstance(step.pid, int) else "pid unknown"
    return f"{step.step} in {_folder_name(step.folder)} ({pid})"


def builds_text(stall: HeadStall) -> str:
    lines = [f"The build queue head has waited {stall.waited / 60:.0f} min. No step started for {stall.quiet / 60:.0f} min.",
             f"Head: {stall.step} in {_folder_name(stall.folder)} (pid {stall.pid}) needs {gib(stall.need)}."]
    room = stall.room
    if isinstance(room, records.RoomCheck):
        lines.append(f"Admission room: {gib(room.room)} for {gib(room.need)} of need.")
    else:
        lines.append("Admission room: unavailable.")
    scan = stall.compiler_scan
    if isinstance(scan, UnreadableCompilers):
        compilers = "compiler" if scan.count == 1 else "compilers"
        lines.append(f"sccache: {scan.count} {compilers} not readable; uses can be low")
    elif isinstance(scan, CompilerScanCountUnavailable):
        lines.append("sccache: compiler scan incomplete; uses can be low")
    lines.append("Holders:")
    for holder in stall.holders:
        memory = holder.memory
        if not holder.steps:
            run = memory.run
            name = f"Run pid {run.pid}" if isinstance(run, LiveBuildRun) else memory.owner
            lines.append(f"- {name}: held between steps, keep-aside {gib(memory.held)}")
            continue
        used = gib(memory.use.bytes) if isinstance(memory.use, records.AnonymousRunUse) else "unknown"
        keep_aside = gib(holder.keep_aside) if isinstance(holder.keep_aside, int) else "unknown"
        original_grant = sum(step.promise for step in holder.steps)
        duration = f"{holder.held_for / 60:.0f} min" if isinstance(holder.held_for, float) else "unknown"
        steps = " + ".join(_holder_step(step) for step in holder.steps)
        held = f", {gib(memory.held)} held between steps" if memory.held else ""
        lines.append(f"- {steps}: effective need {gib(memory.need)}, keep-aside {keep_aside}, "
                     + f"uses {used}, original grant {gib(original_grant)}, held for {duration}{held}")
    reason = stall.reason
    cause = reason.cause if isinstance(reason, records.HeadAdmissionStatus) else reason
    if isinstance(cause, records.NeedsMemory) and isinstance(room, records.RoomCheck):
        lines.append(f"Memory shortfall: {gib(max(0, room.need - room.room))}.")
    else:
        lines.append(f"Reason: {records.reason_phrase(cause, ())}.")
    return "\n".join(lines)


def _alert_text(stall: HeadStall, now: float) -> str:
    instant = datetime.fromtimestamp(now, UTC)
    directory = build_hold.holder_directory()
    try:
        holds = build_hold.natedev_holds(directory)
    except OSError:
        return builds_text(stall)
    for holder in holds.in_force:
        if not build_hold.release_overdue(holder.release, instant):
            release_time = build_hold.release_eta_text(holder.release)
            return f"A build hold by {holder.name} is active for {holder.purpose}. The release time is {release_time}."
    return builds_text(stall)


def _run(command: list[str], text: str = "") -> bool:
    from scripts.message import send_budget, send_result
    try:
        result = subprocess.run(command, input=text, text=True, capture_output=True,
                                timeout=send_budget.seconds(), check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"head-stall watch: send failed: {error}", file=sys.stderr)
        return False
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        if outcome.header.outcome in {"sent", "skipped", "queued", "kept"}:
            return True
        print(f"head-stall watch: {outcome.header.outcome}: {outcome.header.detail}", file=sys.stderr)
    else:
        print(f"head-stall watch: send result unconfirmed: {result.stderr.strip() or result.returncode}", file=sys.stderr)
    return False


def _delivered_stalls(state: Path) -> dict[AlertRecipient, float | str]:
    try:
        raw = cast(object, json.loads(state.read_text()))
    except (OSError, ValueError):
        return {}
    saved = cast(dict[str, object], raw).get("alerted") if isinstance(raw, dict) else None
    # An old key covers builds only. It cannot confirm delivery to bob.
    values = cast(dict[str, object], saved) if isinstance(saved, dict) else {"builds": saved}
    delivered: dict[AlertRecipient, float | str] = {}
    for recipient in ALERT_RECIPIENTS:
        value = values.get(recipient)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            delivered[recipient] = value
    return delivered


def _already_alerted(value: float | str, last_start: float) -> bool:
    if isinstance(value, str):
        # The previous key ended with the last start in whole seconds.
        _, separator, second = value.rpartition(":")
        return bool(separator) and second.isdecimal() and int(last_start) <= int(second)
    return last_start <= value


def send_alert(stall: HeadStall, state: Path, now: float, run: Callable[[list[str], str], bool]) -> None:
    """Record each accepted alert before sending to the next recipient."""
    from scripts.message.session_send import session_send_argv

    delivered = _delivered_stalls(state)
    pending: tuple[AlertRecipient, ...] = tuple(recipient for recipient in ALERT_RECIPIENTS
        if recipient not in delivered or not _already_alerted(delivered[recipient], stall.last_start))
    if not pending:
        return
    text = _alert_text(stall, now)
    write_errors: list[OSError] = []
    for recipient in pending:
        command = session_send_argv(recipient, sender=SENDER)
        command.extend(["--key", f"head-stall-{stall.last_start}"])
        if run(command, text):
            delivered[recipient] = stall.last_start
            try:
                write_text_atomic(state, json.dumps({"alerted": delivered, "at": now}) + "\n")
            except OSError as error:
                write_errors.append(error)
    if write_errors:
        raise write_errors[0]


def watch_once(paths: StallPaths, now: float, run: Callable[[list[str], str], bool] = _run) -> HeadStall | NoStall:
    """Alert once for each stall. A new step start permits another alert."""
    limit, freshest = configured_limits()
    stall = current_stall(paths, now, limit, freshest)
    if isinstance(stall, HeadStall):
        send_alert(stall, paths.state, now, run)
    return stall
