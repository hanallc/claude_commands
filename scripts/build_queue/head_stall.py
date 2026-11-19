"""Alert once when the queue head waits while holders keep memory and nothing starts."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import cast

from scripts.build_queue import admission_records as records
from scripts.build_queue.queue_evidence import gib
from scripts.build_queue.queue_snapshot import QueueSnapshot, read_queue_snapshot
from scripts.buildlog import store
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import memory_admit as gate

SCRIPTS = Path(__file__).resolve().parents[1]
SENDER = "head-stall watch"
TIMES_TAIL_BYTES = 1 << 20
PROC = gate.PROC


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
class Holder:
    step: str
    folder: str
    pid: int | None
    promise: int
    used: int | records.RunUseUnavailable


@dataclass(frozen=True)
class HeadStall:
    """The head waited at least the limit, steps hold memory, and no step started in that time."""
    key: str
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
    running = tuple(step for step in inputs.running if step.promise > 0)
    if not inputs.ordered or not running:
        return NoStall()
    _, head = inputs.ordered[0]
    waited = now - head["since"]
    last_start = max(admitted, *(step.started_at for step in running))
    quiet = now - last_start
    if waited < limit or quiet < limit:
        return NoStall()
    holders = tuple(Holder(step.step, step.folder, step.pid if isinstance(step.pid, int) else None, step.promise,
                           attributed.use.bytes if isinstance(attributed.use, records.AnonymousRunUse) else attributed.use)
                    for _, step, attributed in gate.attributed_step_uses(inputs) if step.promise > 0)
    # A start ends the stall, so the head and the last start name one stall.
    return HeadStall(f"{head['pid']}:{head['start_time']}:{int(last_start)}", head.get("step", "step"), head.get("folder", ""),
                     head["pid"], head["need"], waited, quiet, holders, _compiler_scan(inputs.running))


def current_stall(paths: StallPaths, now: float, limit: float, freshest: float) -> HeadStall | NoStall:
    snapshot = read_queue_snapshot(paths.admission)
    if not isinstance(snapshot, QueueSnapshot) or now - snapshot.written_at > freshest:
        return NoStall()
    return find_stall(snapshot, last_admission(paths.admission / "queue_times.jsonl"), now, limit)


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
        pid = f"pid {holder.pid}" if holder.pid is not None else "pid unknown"
        lines.append(f"- {holder.step} in {_folder_name(holder.folder)} ({pid}): promise {gib(holder.promise)}, uses {used}")
    lines.append("The head starts only when a holder ends. Check each holder that uses much less than its promise.")
    return "\n".join(lines)


def user_body(stall: HeadStall) -> str:
    count = len(stall.holders)
    return (f"The next build has waited {stall.waited / 60:.0f} min for {gib(stall.need)}. "
            + f"No build started for {stall.quiet / 60:.0f} min. "
            + f"{count} {'build holds' if count == 1 else 'builds hold'} the memory it needs.")


def _run(command: list[str], text: str = "") -> bool:
    from scripts.message import send_budget
    try:
        result = subprocess.run(command, input=text, text=True, capture_output=True,
                                timeout=send_budget.seconds(), check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"head-stall watch: send failed: {error}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"head-stall watch: send failed: {result.stderr.strip() or result.returncode}", file=sys.stderr)
    return result.returncode == 0


def send_alert(stall: HeadStall, run: Callable[[list[str], str], bool] = _run) -> bool:
    """Send to builds and to the user; true when either one took the alert."""
    from scripts.message.session_send import session_send_argv
    from scripts.notify import push_texts

    to_builds = run(session_send_argv("builds", sender=SENDER), builds_text(stall))
    push = push_texts.user_send_argv(push_texts.render("alert.blocked", sender=SENDER, title="Build queue stalled",
        action="Ask builds to check the queue holders", body=user_body(stall)), key=f"head-stall-{stall.key}")
    push.extend(["--clears-when", shlex.join([str(SCRIPTS / "lib/py"), str(Path(__file__).resolve()),
                                              "cleared", "--head", stall.key])])
    to_user = run(push, "")
    return to_builds or to_user


def _alerted(state: Path) -> str:
    try:
        raw = cast(object, json.loads(state.read_text()))
    except (OSError, ValueError):
        return ""
    value = cast(dict[str, object], raw).get("alerted") if isinstance(raw, dict) else None
    return value if isinstance(value, str) else ""


def watch_once(paths: StallPaths, now: float, run: Callable[[list[str], str], bool] = _run) -> HeadStall | NoStall:
    """Alert once for each stall; a new stall after a step starts alerts again."""
    limit, freshest = configured_limits()
    stall = current_stall(paths, now, limit, freshest)
    if isinstance(stall, HeadStall) and stall.key != _alerted(paths.state) and send_alert(stall, run):
        write_text_atomic(paths.state, json.dumps({"alerted": stall.key, "at": now}) + "\n")
    return stall


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cleared = commands.add_parser("cleared", help="exit 0 when the named head no longer stalls")
    _ = cleared.add_argument("--head", required=True)
    arguments = parser.parse_args(argv)
    limit, freshest = configured_limits()
    stall = current_stall(default_paths(), time.time(), limit, freshest)
    return 0 if not isinstance(stall, HeadStall) or stall.key != cast(str, arguments.head) else 1


if __name__ == "__main__":
    sys.exit(main())
