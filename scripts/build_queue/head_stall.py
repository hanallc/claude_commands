"""Alert once when the queue head waits while holders keep memory and nothing starts."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal, cast

from scripts.build_queue import admission_records as records
from scripts.build_queue.queue_evidence import gib
from scripts.build_queue.queue_snapshot import QueueSnapshot, read_queue_snapshot
from scripts.buildlog import store
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import memory_admit as gate

SENDER = "head-stall watch"
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
    step: str
    folder: str
    pid: int | records.PidNotRecorded
    promise: int
    used: int | records.RunUseUnavailable
    held_for: float | HolderAdmissionUnavailable


@dataclass(frozen=True)
class HeadStall:
    """The head waited at least the limit, steps hold memory, and no step started in that time."""
    last_start: float
    step: str
    folder: str
    pid: int
    need: int
    waited: float
    quiet: float
    holders: tuple[Holder, ...]
    compiler_scan: CompilerScan
    build_space: records.BuildSpaceHold | records.NoBuildSpaceHold = records.NoBuildSpaceHold()


@dataclass(frozen=True)
class NoStall:
    pass


@dataclass(frozen=True)
class StallPaths:
    admission: Path
    state: Path


def default_paths() -> StallPaths:
    return StallPaths(store.root() / "admission", store.root() / "head_stall.json")


def last_admission(times: Path, settings: conf.ConfigRead) -> float:
    """The newest admitted time of a step with memory need in the tail of queue_times.jsonl; 0 when none.

    The service admits gate jobs with zero need. These admissions free no memory for the head.
    """
    try:
        with times.open("rb") as stream:
            size = stream.seek(0, 2)
            _ = stream.seek(max(0, size - settings.whole("head_stall.times_tail_bytes")))
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
    running = tuple(step for step in inputs.running if step.promise > 0)
    ordered = inputs.ordered
    disk_hold = inputs.build_space
    if isinstance(disk_hold, records.BuildSpaceHold):
        local = gate.first_queued_waiter((row for _, row in ordered if gate.can_use_natedev(row)
            and records.waiter_owner(row) not in inputs.pending_remote_starts), frozenset())
        if not isinstance(local, gate.QueuedWaiter):
            return NoStall()
        head = local.waiter
    else:
        if not ordered or not running:
            return NoStall()
        _, head = ordered[0]
    waited = now - head["since"]
    last_start = max((admitted, *(step.started_at for step in running))) or head["since"]
    quiet = now - last_start
    if waited < limit or quiet < limit:
        return NoStall()
    holders = tuple(Holder(step.step, step.folder, step.pid, step.promise,
                           attributed.use.bytes if isinstance(attributed.use, records.AnonymousRunUse) else attributed.use,
                           max(0.0, now - step.started_at) if step.started_at > 0 else HolderAdmissionUnavailable())
                    for _, step, attributed in gate.attributed_step_uses(inputs) if step.promise > 0)
    return HeadStall(last_start, head.get("step", "step"), head.get("folder", ""),
                     head["pid"], head["need"], waited, quiet, holders, _compiler_scan(inputs.running), disk_hold)


def current_stall(paths: StallPaths, now: float) -> HeadStall | NoStall:
    settings = conf.read("build_admission")
    limit = settings.number("head_stall_alert_minutes") * 60
    freshest = settings.number("silence_seconds")
    snapshot = read_queue_snapshot(paths.admission)
    if not isinstance(snapshot, QueueSnapshot) or now - snapshot.written_at > freshest:
        return NoStall()
    admitted = last_admission(paths.admission / "queue_times.jsonl", settings)
    return find_stall(snapshot, admitted, now, limit)


def _folder_name(folder: str) -> str:
    return folder.rstrip("/").rpartition("/")[2] or "unknown folder"


def builds_text(stall: HeadStall) -> str:
    lines = [f"The build queue head has waited {stall.waited / 60:.0f} min. No step started for {stall.quiet / 60:.0f} min.",
             f"Head: {stall.step} in {_folder_name(stall.folder)} (pid {stall.pid}) needs {gib(stall.need)}."]
    if isinstance(stall.build_space, records.BuildSpaceHold):
        lines.append(records.build_space_text(stall.build_space) + ".")
        return "\n".join(lines)
    scan = stall.compiler_scan
    if isinstance(scan, UnreadableCompilers):
        compilers = "compiler" if scan.count == 1 else "compilers"
        lines.append(f"sccache: {scan.count} {compilers} not readable; uses can be low")
    elif isinstance(scan, CompilerScanCountUnavailable):
        lines.append("sccache: compiler scan incomplete; uses can be low")
    lines.append("Holders:")
    for holder in stall.holders:
        used = gib(holder.used) if isinstance(holder.used, int) else "unknown"
        pid = f"pid {holder.pid}" if isinstance(holder.pid, int) else "pid unknown"
        duration = f"{holder.held_for / 60:.0f} min" if isinstance(holder.held_for, float) else "unknown"
        lines.append(f"- {holder.step} in {_folder_name(holder.folder)} ({pid}): "
                     + f"promise {gib(holder.promise)}, uses {used}, held for {duration}")
    lines.append("The head starts only when a holder ends. Check each holder that uses much less than its promise.")
    return "\n".join(lines)


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
    text = builds_text(stall)
    write_errors: list[OSError] = []
    for recipient in ALERT_RECIPIENTS:
        if recipient in delivered and _already_alerted(delivered[recipient], stall.last_start):
            continue
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
    stall = current_stall(paths, now)
    if isinstance(stall, HeadStall):
        send_alert(stall, paths.state, now, run)
    return stall
