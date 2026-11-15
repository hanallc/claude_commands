"""Records of the checkpoint lands that run now.

`merge_checkpoint.py` writes one record while it holds a production's merge lock, and removes it at exit.
A record whose process ended is not a running land; the next read removes it.
The units board reads the records to tell a running checkpoint from a queued one.
"""
from __future__ import annotations

import json
import os
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start


class LandRecord(TypedDict):
    pid: int
    start: str
    commits: list[str]


def state_dir() -> Path:
    """The merge gate state folder; `merge_gate.gate_state_dir` returns this one."""
    return Path(os.environ.get("MERGE_GATE_STATE_DIR", "~/.local/state/merge-gate")).expanduser()


def lands_dir() -> Path:
    return state_dir() / "lands"


@contextmanager
def landing(commits: Sequence[str]) -> Generator[None]:
    """Record the commits of this land while the caller runs it."""
    identity = process_start(os.getpid())
    if not isinstance(identity, Started):
        yield
        return
    path = lands_dir() / f"{os.getpid()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record: LandRecord = {"pid": os.getpid(), "start": identity.start, "commits": list(commits)}
    write_text_atomic(path, json.dumps(record) + "\n")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def running_commits() -> frozenset[str]:
    """The commits of every land whose process still runs; remove the record of a land that ended."""
    commits: set[str] = set()
    try:
        paths = sorted(lands_dir().glob("*.json"))
    except OSError:
        return frozenset()
    for path in paths:
        try:
            record = cast(LandRecord, json.loads(path.read_text()))
            pid, start, listed = record["pid"], record["start"], record["commits"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        owner = process_start(pid)
        if owner == Started(start):
            commits.update(listed)
        elif not isinstance(owner, Unreadable):
            path.unlink(missing_ok=True)
    return frozenset(commits)


def holds(running: frozenset[str], commit: str) -> bool:
    """True when a running land names this commit; a land can name a short hash."""
    return any(commit.startswith(listed) or listed.startswith(commit) for listed in running if listed and commit)
