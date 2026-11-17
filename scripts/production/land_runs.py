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
from collections.abc import Collection, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start


# The results file keeps only this many newest rows.
KEPT_RESULTS = 300
DEFAULT_STATE_DIR = "~/.local/state/gate"


class GateStateMoveRequired(Exception):
    """The default gate state still requires the deploy move."""


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


def default_state_dir() -> Path:
    """Return the default gate state folder after the deploy move."""
    directory = Path(DEFAULT_STATE_DIR).expanduser()
    # Chunk 6 removes this guard and the old folder path.
    old = Path("~/.local/state/merge-gate").expanduser()
    if os.path.lexists(old) and not os.path.lexists(directory):
        raise GateStateMoveRequired(
            "Gate state move required: set /build:hold drain natedev, then run "
            + "/tmp/claude/scratch/natest-probe/p89/move_gate_state.py")
    return directory


def state_dir() -> Path:
    """Return the selected gate state folder."""
    selected = os.environ.get("GATE_STATE_DIR")
    return default_state_dir() if selected is None else Path(selected).expanduser()


def lands_dir() -> Path:
    return state_dir() / "lands"


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


def running_lands() -> dict[str, str]:
    """The commits of every land whose process still runs, each with its "unit phase" or "".

    It removes the record of a land that ended.
    """
    commits: dict[str, str] = {}
    try:
        paths = sorted(lands_dir().glob("*.json"))
    except OSError:
        return {}
    for path in paths:
        try:
            record = cast(LandRecord, json.loads(path.read_text()))
            pid, start, listed, owners = record["pid"], record["start"], record["commits"], record.get("owners", [])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        owner = process_start(pid)
        if owner == Started(start):
            commits.update((commit, owners[index] if index < len(owners) else "") for index, commit in enumerate(listed))
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
