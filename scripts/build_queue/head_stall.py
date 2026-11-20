"""Alert once when the queue head waits while holders keep memory and nothing starts."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal, cast

from scripts.build_hold import build_hold
from scripts.build_queue import admission_client, admission_records as records
from scripts.build_queue.admission_wire import AdmissionUnavailable
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
type AlertDeliveries = dict[AlertRecipient, float | str]
ALERT_RECIPIENTS: tuple[AlertRecipient, ...] = ("builds", "bob")
DECISION_OUTAGE = "admission-decision"


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
    head: records.HeadExplanation
    room: records.RoomCheck | records.ReadingUnavailable
    waited: float
    quiet: float
    holders: tuple[Holder, ...]
    compiler_scan: CompilerScan


@dataclass(frozen=True)
class NoStall:
    pass


@dataclass(frozen=True)
class AdmissionDecisionUnreadable:
    reason: str
    waited: float


@dataclass(frozen=True)
class NoDecisionOutage:
    pass


@dataclass(frozen=True)
class WatchState:
    alerted: AlertDeliveries = field(default_factory=dict)
    outage_alerted: AlertDeliveries = field(default_factory=dict)
    unreadable_since: float | NoDecisionOutage = NoDecisionOutage()


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


def find_stall(decision: records.QueueExplanation, admitted: float, now: float, limit: float) -> HeadStall | NoStall:
    head = decision.head
    if not isinstance(head, records.HeadExplanation) or head.cause == records.ADMISSION_DECISION_PENDING:
        return NoStall()
    inputs = decision.decided_from
    last_running_start = max((step.started_at for step in inputs.running if step.promise > 0), default=0.0)
    last_start = max(admitted, last_running_start)
    quiet = now - last_start
    waited = now - head.waiter.since
    if quiet < limit or waited < limit:
        return NoStall()

    holders = memory_holders(decision, now)
    if not holders:
        return NoStall()
    head_room = head.room if isinstance(decision.room, records.MemoryRoom) else decision.room
    return HeadStall(last_start, head, head_room, waited, quiet, holders, _compiler_scan(inputs.running))


def memory_holders(decision: records.QueueExplanation, now: float) -> tuple[Holder, ...]:
    """List the runs with active reservations or memory held between steps."""
    inputs = decision.decided_from
    steps_by_owner: dict[str, list[records.RunningStep]] = {}
    reserved_steps = (step for step in inputs.running if gate.running_has_reservation(step))
    for row, step in zip(inputs.reservations, reserved_steps, strict=True):
        if step.promise <= 0:
            continue
        owner = gate.reservation_owner(row, step)
        steps_by_owner.setdefault(owner, []).append(step)
    room = decision.room
    if isinstance(room, records.MemoryRoom):
        charges = tuple((run.memory, run.bytes) for run in room.runs)
    else:
        if inputs.slots:
            start = gate.start_spacing_after(inputs.now, decision.policy)
            returned = gate.head_policy(inputs, inputs.now, decision.policy, start).returned_claims
            inputs = replace(inputs, slots=tuple(slot for slot in inputs.slots
                if gate.between_steps(slot).reservation not in returned))
        charges = tuple((memory, room) for memory in gate.run_memories(inputs))
    holders: list[Holder] = []
    for memory, keep_aside in charges:
        steps = steps_by_owner.get(memory.owner, [])
        if not steps and not memory.held:
            continue
        started_at = min((step.started_at for step in steps), default=0.0)
        held_for = max(0.0, now - started_at) if started_at > 0 else HolderAdmissionUnavailable()
        holders.append(Holder(tuple(steps), memory, keep_aside, held_for))
    return tuple(holders)


def current_stall(paths: StallPaths, now: float, limit: float, freshest: float
                  ) -> HeadStall | AdmissionDecisionUnreadable | NoStall:
    snapshot = read_queue_snapshot(paths.admission)
    if not isinstance(snapshot, QueueSnapshot) or now - snapshot.written_at > freshest:
        return NoStall()
    if not any(now - row["since"] >= limit for _, row in snapshot.inputs.ordered):
        return NoStall()
    # The snapshot includes CI waiters that the admission decision excludes.
    try:
        decision = admission_client.explain()
    except (AdmissionUnavailable, OSError, ValueError) as error:
        reason = " ".join(str(error).splitlines()) or type(error).__name__
    else:
        age = now - decision.now
        if age <= freshest:
            return find_stall(decision, last_admission(paths.admission / "queue_times.jsonl"), now, limit)
        reason = f"the admission decision is {age:.0f} s old"
    inputs = snapshot.inputs
    head = gate.first_queued_waiter((row for _, row in gate.ranked_queue_waiters(inputs)),
                                    inputs.pending_remote_starts)
    waited = now - head.waiter["since"] if isinstance(head, gate.QueuedWaiter) else 0.0
    return AdmissionDecisionUnreadable(reason, waited)


def _folder_name(folder: str) -> str:
    return folder.rstrip("/").rpartition("/")[2] or "unknown folder"


def _holder_step(step: records.RunningStep) -> str:
    pid = f"pid {step.pid}" if isinstance(step.pid, int) else "pid unknown"
    return f"{step.step} in {_folder_name(step.folder)} ({pid})"


def builds_text(stall: HeadStall) -> str:
    waiter = stall.head.waiter
    lines = [f"The build queue head has waited {stall.waited / 60:.0f} min. No step started for {stall.quiet / 60:.0f} min.",
             f"Head: {waiter.step or 'step'} in {_folder_name(waiter.folder)} (pid {waiter.pid}) needs {gib(waiter.need)}."]
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
    cause = stall.head.cause
    if isinstance(cause, records.NeedsMemory) and isinstance(room, records.RoomCheck):
        lines.append(f"Memory shortfall: {gib(max(0, room.need - room.room))}.")
    elif isinstance(cause, records.BehindHead) and isinstance(stall.head.decision.order_hold, records.NoOrderHold):
        # Admission uses BehindHead without an order hold for the head's own remote start.
        lines.append("Reason: waiting for its remote start.")
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


def _read_state(path: Path) -> WatchState:
    try:
        raw = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return WatchState()
    if not isinstance(raw, dict):
        return WatchState()
    fields = cast(dict[str, object], raw)
    saved = fields.get("alerted")
    # An old key covers builds only. It cannot confirm delivery to bob.
    values = cast(dict[str, object], saved) if isinstance(saved, dict) else {"builds": saved}
    outages = fields.get("outage_alerted")
    outage_values = cast(dict[str, object], outages) if isinstance(outages, dict) else {}
    alerted: AlertDeliveries = {}
    outage_alerted: AlertDeliveries = {}
    for recipient in ALERT_RECIPIENTS:
        value = values.get(recipient)
        outage = outage_values.get(recipient)
        if isinstance(value, dict):
            # Keep the latest delivery from the old format that stored one timestamp per head.
            heads = cast(dict[str, object], value)
            outage = heads.get(DECISION_OUTAGE, outage)
            value = max((at for key, at in heads.items() if key != DECISION_OUTAGE
                         and isinstance(at, (int, float)) and not isinstance(at, bool)), default=None)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            alerted[recipient] = value
        if isinstance(outage, (int, float)) and not isinstance(outage, bool):
            outage_alerted[recipient] = outage
    since = fields.get("unreadable_since")
    return WatchState(alerted, outage_alerted, float(since) if isinstance(since, (int, float))
                      and not isinstance(since, bool) else NoDecisionOutage())


def _write_state(path: Path, state: WatchState, now: float) -> None:
    fields: dict[str, object] = {"alerted": state.alerted, "at": now}
    if state.outage_alerted:
        fields["outage_alerted"] = state.outage_alerted
    if isinstance(state.unreadable_since, float):
        fields["unreadable_since"] = state.unreadable_since
    write_text_atomic(path, json.dumps(fields) + "\n")


def _already_alerted(value: float | str, event_at: float) -> bool:
    if isinstance(value, str):
        # The old key stored the last start in whole seconds.
        _, separator, second = value.rpartition(":")
        return bool(separator) and second.isdecimal() and len(second) <= 20 and int(event_at) <= int(second)
    return event_at <= value


def _send_alert(path: Path, state: WatchState, delivered: AlertDeliveries, now: float,
                key: str, event_at: float, text: Callable[[], str],
                run: Callable[[list[str], str], bool]) -> None:
    """Record each accepted alert before sending to the next recipient."""
    from scripts.message.session_send import session_send_argv

    pending: tuple[AlertRecipient, ...] = tuple(recipient for recipient in ALERT_RECIPIENTS
        if recipient not in delivered or not _already_alerted(delivered[recipient], event_at))
    if not pending:
        return
    report = text()
    write_errors: list[OSError] = []
    for recipient in pending:
        command = session_send_argv(recipient, sender=SENDER)
        command.extend(["--key", key])
        if run(command, report):
            delivered[recipient] = event_at
            try:
                _write_state(path, state, now)
            except OSError as error:
                write_errors.append(error)
    if write_errors:
        raise write_errors[0]


def watch_once(paths: StallPaths, now: float, run: Callable[[list[str], str], bool] = _run
               ) -> HeadStall | AdmissionDecisionUnreadable | NoStall:
    """Alert once per stall. Report prolonged failures to read the decision."""
    limit, freshest = configured_limits()
    stall = current_stall(paths, now, limit, freshest)
    state = _read_state(paths.state)
    if isinstance(stall, AdmissionDecisionUnreadable):
        print(f"head-stall watch: skipped sample: {stall.reason}", file=sys.stderr)
        since = state.unreadable_since
        if isinstance(since, NoDecisionOutage):
            _write_state(paths.state, replace(state, unreadable_since=now), now)
        elif now - since >= limit and stall.waited >= limit:
            _send_alert(paths.state, state, state.outage_alerted, now, f"head-stall-{DECISION_OUTAGE}-{since}", since,
                        lambda: "The head-stall check cannot read the admission decision. "
                        + f"The queue snapshot shows a head that has waited {stall.waited / 60:.0f} min.\n"
                        + stall.reason, run)
        return stall
    if isinstance(state.unreadable_since, float):
        state = replace(state, unreadable_since=NoDecisionOutage())
        _write_state(paths.state, state, now)
    if isinstance(stall, HeadStall):
        _send_alert(paths.state, state, state.alerted, now, f"head-stall-{stall.last_start}", stall.last_start,
                    lambda: _alert_text(stall, now), run)
    return stall
