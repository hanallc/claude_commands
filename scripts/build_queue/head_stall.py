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
    step: str
    folder: str
    pid: int | records.PidNotRecorded
    promise: int
    used: int | records.RunUseUnavailable
    held_for: float | HolderAdmissionUnavailable


@dataclass(frozen=True)
class HeadStall:
    """The head reached the wait and quiet limits while steps hold memory."""
    last_memory_start: float
    step: str
    folder: str
    pid: int
    need: int
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


@dataclass(frozen=True)
class QueueActivity:
    last_memory_admission: float = 0.0
    last_landing_pass: float = 0.0


def default_paths() -> StallPaths:
    return StallPaths(store.root() / "admission", store.root() / "head_stall.json")


def configured_limits() -> tuple[float, float]:
    """The stall limit and the oldest usable snapshot age, in seconds."""
    settings = conf.read("build_admission")
    return settings.number("head_stall_alert_minutes") * 60, settings.number("silence_seconds")


def last_admission(times: Path) -> float:
    """Find the last start that added a memory promise."""
    return queue_activity(times, records.NoWaiter()).last_memory_admission


def queue_activity(times: Path, head: records.Waiter | records.NoWaiter) -> QueueActivity:
    """Read the last admission with memory need and the last pass of this head."""
    try:
        with times.open("rb") as stream:
            size = stream.seek(0, 2)
            _ = stream.seek(max(0, size - TIMES_TAIL_BYTES))
            lines = stream.read().decode(errors="replace").splitlines()
    except OSError:
        return QueueActivity()
    last_landing_pass = 0.0
    for line in reversed(lines):
        if '"kind":"admitted"' not in line and '"kind":"landing_pass"' not in line:
            continue
        try:
            row = cast(object, json.loads(line))
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        fields = cast(dict[str, object], row)
        if fields.get("kind") == "admitted":
            value, need = fields.get("admitted_at"), fields.get("need")
            if isinstance(value, (int, float)) and isinstance(need, int) and need > 0:
                return QueueActivity(float(value), last_landing_pass)
        elif (not isinstance(head, records.NoWaiter) and fields.get("pid") == head["pid"]
                and fields.get("start_time") == head["start_time"] and fields.get("since") == head["since"]):
            passed_at = fields.get("passed_at")
            if isinstance(passed_at, (int, float)):
                last_landing_pass = max(last_landing_pass, float(passed_at))
    return QueueActivity(last_landing_pass=last_landing_pass)


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


def find_stall(snapshot: QueueSnapshot, last_memory_admission: float, now: float, limit: float, *,
               last_landing_pass: float = 0.0) -> HeadStall | NoStall:
    inputs = snapshot.inputs
    running = tuple(step for step in inputs.running if step.promise > 0)
    if not inputs.ordered or not running:
        return NoStall()
    _, head = inputs.ordered[0]
    waited = now - head["since"]
    last_memory_start = max(last_memory_admission, *(step.started_at for step in running))
    quiet_since = max(last_memory_start, last_landing_pass)
    quiet = now - quiet_since
    if waited < limit or quiet < limit:
        return NoStall()
    holders = tuple(Holder(step.step, step.folder, step.pid, step.promise,
                           attributed.use.bytes if isinstance(attributed.use, records.AnonymousRunUse) else attributed.use,
                           max(0.0, now - step.started_at) if step.started_at > 0 else HolderAdmissionUnavailable())
                    for _, step, attributed in gate.attributed_step_uses(inputs) if step.promise > 0)
    return HeadStall(last_memory_start, head.get("step", "step"), head.get("folder", ""),
                     head["pid"], head["need"], waited, quiet, holders, _compiler_scan(inputs.running))


def current_stall(paths: StallPaths, now: float, limit: float, freshest: float) -> HeadStall | NoStall:
    snapshot = read_queue_snapshot(paths.admission)
    if not isinstance(snapshot, QueueSnapshot) or now - snapshot.written_at > freshest:
        return NoStall()
    if not snapshot.inputs.ordered:
        return NoStall()
    _, head = snapshot.inputs.ordered[0]
    activity = queue_activity(paths.admission / "queue_times.jsonl", head)
    return find_stall(snapshot, activity.last_memory_admission, now, limit, last_landing_pass=activity.last_landing_pass)


def _folder_name(folder: str) -> str:
    return folder.rstrip("/").rpartition("/")[2] or "unknown folder"


def builds_text(stall: HeadStall) -> str:
    lines = [f"The build queue head has waited {stall.waited / 60:.0f} min. No step started for {stall.quiet / 60:.0f} min.",
             f"Head: {stall.step} in {_folder_name(stall.folder)} (pid {stall.pid}) needs {gib(stall.need)}."]
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


def _already_alerted(value: float | str, last_memory_start: float) -> bool:
    if isinstance(value, str):
        # The previous key ended with the last start in whole seconds.
        _, separator, second = value.rpartition(":")
        return bool(separator) and second.isdecimal() and int(last_memory_start) <= int(second)
    return last_memory_start <= value


def send_alert(stall: HeadStall, state: Path, now: float, run: Callable[[list[str], str], bool]) -> None:
    """Record each accepted alert before sending to the next recipient."""
    from scripts.message.session_send import session_send_argv

    delivered = _delivered_stalls(state)
    text = builds_text(stall)
    write_errors: list[OSError] = []
    for recipient in ALERT_RECIPIENTS:
        if recipient in delivered and _already_alerted(delivered[recipient], stall.last_memory_start):
            continue
        command = session_send_argv(recipient, sender=SENDER)
        command.extend(["--key", f"head-stall-{stall.last_memory_start}"])
        if run(command, text):
            delivered[recipient] = stall.last_memory_start
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
