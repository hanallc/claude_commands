"""Records of the checkpoint lands that run now, and the result of each land.

`merge_checkpoint.py` writes one record while it holds a production's merge lock, and removes it at exit.
A record whose process ended is not a running land; the next read removes it.
It also adds one result row for each checkpoint it lands or stops.
The units board reads the records to tell a running checkpoint from a queued one, and the results to say how a land ended.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from collections.abc import Collection, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start


if TYPE_CHECKING:
    from scripts.build_hold.build_hold import ProcessIdentity


# The results file keeps only this many newest rows.
KEPT_RESULTS = 300


class LandRecord(TypedDict):
    """A running land; owners[i] is the "unit phase" of commits[i]. An older record has no owners."""

    pid: int
    start: str
    commits: list[str]
    owners: NotRequired[list[str]]


RunnerState = Literal["taking", "empty"]


class RunnerLease(TypedDict):
    pid: int
    start: str
    state: RunnerState


@dataclass(frozen=True)
class LiveLand:
    """A live process has a land record."""


@dataclass(frozen=True)
class RunnerTaking:
    """A live runner took an item and has yet to find the line empty."""


@dataclass(frozen=True)
class LandingOn:
    reason: LiveLand | RunnerTaking


@dataclass(frozen=True)
class LandingOff:
    """No live land or runner lease holds the queue."""


class LandResult(TypedDict):
    commit: str
    production: str
    unit: str
    phase: str
    landed: bool
    step: str
    state: str
    detail: str
    time: str


def state_dir() -> Path:
    """The merge gate state folder; `merge_gate.gate_state_dir` returns this one."""
    return Path(os.environ.get("MERGE_GATE_STATE_DIR", "~/.local/state/merge-gate")).expanduser()


def lands_dir() -> Path:
    return state_dir() / "lands"


def runner_path() -> Path:
    return state_dir() / "runner.json"


def results_path() -> Path:
    return state_dir() / "land_results.jsonl"


@contextmanager
def landing(commits: Sequence[str], owners: Sequence[str] = ()) -> Generator[None]:
    """Record the commits of this land, and the "unit phase" that owns each, while the caller runs it."""
    identity = process_start(os.getpid())
    if not isinstance(identity, Started):
        yield
        return
    path = lands_dir() / f"{os.getpid()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record: LandRecord = {"pid": os.getpid(), "start": identity.start, "commits": list(commits), "owners": list(owners)}
    write_text_atomic(path, json.dumps(record) + "\n")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class NoLandRecord:
    """The path has no valid land record."""


def _read_land(path: Path) -> LandRecord | NoLandRecord:
    try:
        parsed = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return NoLandRecord()
    if not isinstance(parsed, dict):
        return NoLandRecord()
    row = cast(dict[str, object], parsed)
    pid, start = row.get("pid"), row.get("start")
    commits, owners = row.get("commits"), row.get("owners", [])
    if (type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start
            or not isinstance(commits, list) or not isinstance(owners, list)):
        return NoLandRecord()
    if (all(isinstance(commit, str) for commit in cast(list[object], commits))
            and all(isinstance(owner, str) for owner in cast(list[object], owners))):
        return cast(LandRecord, cast(object, row))
    return NoLandRecord()


def _land_records() -> Iterator[tuple[Path, LandRecord]]:
    try:
        paths = sorted(lands_dir().glob("*.json"))
    except (OSError, RuntimeError, ValueError):
        return
    for path in paths:
        record = _read_land(path)
        if not isinstance(record, NoLandRecord):
            yield path, record


@dataclass(frozen=True)
class NoNatemergeLand:
    """The lineage has no verified natemerge land."""


def natemerge_land(proc: Path, lineage: frozenset[ProcessIdentity]) -> ProcessIdentity | NoNatemergeLand:
    """Find the live land in the caller lineage and check its session in staff."""
    from scripts.build_hold.build_hold import process_start_time
    from scripts.build_queue.scope_activity import environment_values
    from scripts.lint.memory_admit import is_natemerge, queue_staff_chain
    from scripts.production import staff

    folder = lands_dir()
    for ancestor in sorted(lineage, key=lambda item: (int(item.start_time), item.pid), reverse=True):
        record = _read_land(folder / f"{ancestor.pid}.json")
        if isinstance(record, NoLandRecord) or (record["pid"], record["start"]) != (ancestor.pid, ancestor.start_time):
            continue
        values = environment_values(proc / str(ancestor.pid) / "environ", ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"))
        session = values.get("CLAUDE_CODE_SESSION_ID") or values.get("CODEX_THREAD_ID", "")
        if not session or process_start_time(ancestor.pid, proc) != ancestor.start_time:
            return NoNatemergeLand()
        try:
            members = staff.local_snapshot()
        except staff.StaffUnreadable:
            return NoNatemergeLand()
        if any(map(is_natemerge, queue_staff_chain(members, session))):
            return ancestor
        return NoNatemergeLand()
    return NoNatemergeLand()


def landing_active() -> LandingOn | LandingOff:
    """Read the landing signal. Leave all records on disk."""
    for _, record in _land_records():
        if process_start(record["pid"]) == Started(record["start"]):
            return LandingOn(LiveLand())
    try:
        parsed = cast(object, json.loads(runner_path().read_text(encoding="utf-8")))
    except (OSError, RuntimeError, ValueError):
        return LandingOff()
    if not isinstance(parsed, dict):
        return LandingOff()
    lease = cast(dict[str, object], parsed)
    if lease.get("state") != "taking":
        return LandingOff()
    pid, start = lease.get("pid"), lease.get("start")
    if type(pid) is int and pid > 0 and isinstance(start, str) and start and process_start(pid) == Started(start):
        return LandingOn(RunnerTaking())
    return LandingOff()


def running_lands() -> dict[str, str]:
    """Return the commits and owners of live lands. Remove the records of lands that ended."""
    commits: dict[str, str] = {}
    for path, record in _land_records():
        owner = process_start(record["pid"])
        if owner == Started(record["start"]):
            owners = record.get("owners", [])
            commits.update((commit, owners[index] if index < len(owners) else "")
                           for index, commit in enumerate(record["commits"]))
        elif not isinstance(owner, Unreadable):
            path.unlink(missing_ok=True)
    return commits


def holds(running: Collection[str], commit: str) -> bool:
    """True when a running land names this commit; a land can name a short hash."""
    return any(commit.startswith(listed) or listed.startswith(commit) for listed in running if listed and commit)


def record_result(*, commit: str, production: str, unit: str, phase: str,
                  landed: bool, step: str = "", state: str = "", detail: str = "") -> None:
    """Add the result of one checkpoint land, and keep only the newest rows."""
    row: LandResult = {"commit": commit, "production": production, "unit": unit, "phase": phase, "landed": landed,
                       "step": step, "state": state, "detail": detail,
                       "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(path.with_name(path.name + ".lock")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        lines = [*lines, json.dumps(row)][-KEPT_RESULTS:]
        write_text_atomic(path, "\n".join(lines) + "\n")


def last_results() -> dict[str, LandResult]:
    """The newest result of each commit; a bad row is not a result."""
    results: dict[str, LandResult] = {}
    try:
        lines = results_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return results
    for line in lines:
        try:
            row = cast(LandResult, json.loads(line))
            commit = cast(object, row["commit"])
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(commit, str) and commit:
            results[commit] = row
    return results


def result_for(results: dict[str, LandResult], commit: str) -> LandResult | None:
    """The newest result whose commit names this commit; a result can name a short hash."""
    found = [row for listed, row in results.items() if holds(frozenset({listed}), commit)]
    return max(found, key=lambda row: row["time"]) if found else None
