#!/usr/bin/env python3
"""Read fast-track patches and record their review results."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Literal, TypedDict, cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail


class Refused(Exception):
    """The requested patch must not be pushed."""


class PushRecord(TypedDict):
    patch_sha256: str
    unit: str
    commit: str
    why: str
    time: str
    review: str
    repo: str
    paths: list[str]
    review_pass: str


class PushIntent(TypedDict):
    record: PushRecord
    worktree_head: str
    worktree_tree: str
    index_tree: str


class PushAttempt(TypedDict):
    restore: PushIntent
    base: str
    production: str
    publication: Literal["prepared", "published", "not_published"]
    restored: bool
    restore_closed: Literal["", "ancestry", "explicit"]
    promoted: bool


def state_file() -> Path:
    return Path(os.environ.get("FAST_TRACK_STATE_DIR", str(Path.home() / ".local/state/fast-track"))) / "records.jsonl"


@contextmanager
def record_lock() -> Generator[None]:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def push_record(value: object, path: Path) -> PushRecord:
    if not isinstance(value, dict):
        raise Refused(f"invalid fast-track record in {path}")
    fields = cast(dict[str, object], value)
    for key in ("patch_sha256", "unit", "commit", "why", "time", "review"):
        if key not in fields or not isinstance(fields[key], str):
            raise Refused(f"invalid fast-track record key {key} in {path}")
    # Older review rows contain only the public review fields.
    for key in ("repo", "review_pass"):
        if key not in fields:
            fields[key] = ""
    if "paths" not in fields:
        fields["paths"] = []
    if (not isinstance(fields["repo"], str) or not isinstance(fields["review_pass"], str)
            or not isinstance(fields["paths"], list)
            or any(not isinstance(item, str) for item in cast(list[object], fields["paths"]))
            or fields["review"] not in ("pending", "reviewed")):
        raise Refused(f"invalid fast-track record in {path}")
    return cast(PushRecord, cast(object, fields))


def records() -> list[PushRecord]:
    path = state_file()
    if not path.exists():
        return []
    return [push_record(cast(object, json.loads(line)), path) for line in path.read_text().splitlines()]


def read_attempt(path: Path) -> PushAttempt:
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise Refused(f"invalid fast-track attempt in {path}")
    fields = cast(dict[str, object], value)
    snapshot = fields["restore"]
    if not isinstance(snapshot, dict):
        raise Refused(f"invalid restore snapshot in {path}")
    restore = cast(dict[str, object], snapshot)
    row = push_record(restore["record"], path)
    restore["record"] = row
    if "restore_closed" not in fields:
        fields["restore_closed"] = ""
    if (any(not isinstance(restore[key], str) for key in ("worktree_head", "worktree_tree", "index_tree"))
            or any(not isinstance(fields[key], str) for key in ("base", "production"))
            or fields["publication"] not in ("prepared", "published", "not_published")
            or fields["restore_closed"] not in ("", "ancestry", "explicit")
            or any(not isinstance(fields[key], bool) for key in ("restored", "promoted"))):
        raise Refused(f"invalid fast-track attempt in {path}")
    return cast(PushAttempt, cast(object, fields))


def attempts() -> list[tuple[Path, PushAttempt]]:
    folder = state_file().parent / "attempts"
    return [(path, read_attempt(path)) for path in sorted(folder.glob("*.json"))]


def save_attempt(path: Path, attempt: PushAttempt) -> None:
    write_text_atomic(path, json.dumps(attempt) + "\n")


def save_records(rows: list[PushRecord]) -> None:
    write_text_atomic(state_file(), "".join(json.dumps(row) + "\n" for row in rows))


def snapshot_pending(unit: str, show_commit: Callable[[str], str]) -> None:
    with record_lock():
        rows = records()
    write_pending(unit, rows, show_commit)


def write_pending(unit: str, rows: list[PushRecord], show_commit: Callable[[str], str]) -> None:
    for row in rows:
        if row["unit"] == unit and row["review"] == "pending":
            print(f"\n## Fast-track {row['commit']} — {row['why']}\n")
            print(show_commit(row["commit"]))


def mark_reviewed(unit: str, commits: list[str], review_pass: str) -> None:
    if not commits or not review_pass:
        raise Refused("--mark-reviewed requires --commit and --pass")
    with record_lock():
        rows = records()
        for row in rows:
            if row["unit"] == unit and row["commit"] in commits and row["review"] == "pending":
                row["review"] = "reviewed"
                row["review_pass"] = review_pass
        save_records(rows)
        for path, attempt in attempts():
            row = attempt["restore"]["record"]
            if row["unit"] == unit and row["commit"] in commits and row["review"] == "pending":
                row["review"] = "reviewed"
                row["review_pass"] = review_pass
                save_attempt(path, attempt)


def git(*arguments: str) -> str:
    result = subprocess.run(["git", *arguments], env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"},
                            check=False, capture_output=True, text=True)
    if result.returncode:
        raise Refused(failure_detail(result))
    return result.stdout


def review_pending(unit: str) -> None:
    with record_lock():
        if next(state_file().with_name("attempts").glob("*.json"), None) is None:
            rows = records()
        else:
            rows = None
    if rows is None:
        # Recover published attempts through the fast-track command.
        command = str(Path(__file__).with_name("fast_track.py"))
        os.execv(sys.executable, [sys.executable, command, "--pending", "--unit", unit])
    write_pending(unit, rows, lambda commit: git("show", "--format=fuller", "--binary", commit))


@dataclass
class ReviewArguments(argparse.Namespace):
    unit: str = ""
    pending: bool = False
    review_pass: str = ""
    commit: list[str] = field(default_factory=list)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--unit", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    _ = mode.add_argument("--pending", action="store_true")
    _ = mode.add_argument("--mark-reviewed", action="store_true")
    _ = parser.add_argument("--pass", dest="review_pass", default="")
    _ = parser.add_argument("--commit", action="append", default=[], type=str)
    args = parser.parse_args(namespace=ReviewArguments())
    try:
        _ = git("rev-parse", "--show-toplevel")
        if args.pending:
            review_pending(args.unit)
        else:
            mark_reviewed(args.unit, args.commit, args.review_pass)
    except (Refused, OSError, ValueError, KeyError) as error:
        print(f"fast-track: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
