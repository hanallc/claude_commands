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
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start


# The results file keeps only this many newest rows.
KEPT_RESULTS = 300


class LandRecord(TypedDict):
    """A running land; owners[i] is the "unit phase" of commits[i]. An older record has no owners."""

    pid: int
    start: str
    commits: list[str]
    owners: NotRequired[list[str]]


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


def results_path() -> Path:
    return state_dir() / "land_results.jsonl"


@contextmanager
def landing(commits: Sequence[str], owners: Sequence[str] = ()) -> Generator[None]:
    """Record the commits of this land, and the "unit phase" that owns each, while the caller runs it."""
    identity = process_start(os.getpid())
    if not isinstance(identity, Started):
        raise OSError("cannot record the land process identity")
    path = lands_dir() / f"{os.getpid()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record: LandRecord = {"pid": os.getpid(), "start": identity.start, "commits": list(commits), "owners": list(owners)}
    write_text_atomic(path, json.dumps(record) + "\n")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _read_land(path: Path) -> LandRecord:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if isinstance(value, dict):
        record = cast(dict[str, object], value)
        pid, start, commits, owners = (record.get("pid"), record.get("start"),
                                       record.get("commits"), record.get("owners", []))
        if (type(pid) is int and pid > 0 and isinstance(start, str) and start
                and isinstance(commits, list) and all(isinstance(commit, str) for commit in cast(list[object], commits))
                and isinstance(owners, list) and all(isinstance(owner, str) for owner in cast(list[object], owners))):
            return LandRecord(pid=pid, start=start, commits=cast(list[str], commits), owners=cast(list[str], owners))
    raise ValueError("invalid land record")


def _active_lands() -> Iterator[LandRecord | Unreadable]:
    try:
        paths = sorted(path for path in lands_dir().iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        return
    except OSError as error:
        yield Unreadable(str(error))
        return
    for path in paths:
        try:
            record = _read_land(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            yield Unreadable(f"{path}: {error}")
            continue
        owner = process_start(record["pid"])
        if owner == Started(record["start"]):
            yield record
        elif isinstance(owner, Unreadable):
            yield owner
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def has_gap() -> bool:
    """Check that every recorded land has ended while registration is locked."""
    return not any(_active_lands())


def running_lands() -> dict[str, str]:
    """Show the commits and owners of the lands with a confirmed process identity."""
    commits: dict[str, str] = {}
    for record in _active_lands():
        if isinstance(record, Unreadable):
            continue
        owners = record.get("owners", [])
        commits.update((commit, owners[index] if index < len(owners) else "")
                       for index, commit in enumerate(record["commits"]))
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
