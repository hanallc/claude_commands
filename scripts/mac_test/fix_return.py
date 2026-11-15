#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Bring the files of a Mac fix pass back into the worktree that the Mac copied.

offload.py lists the mirror files that differ from the worktree, fetches them
to a staging folder, and calls apply_fix. apply_fix writes them only when the
worktree still has the tree key that the copy sent.

A roll-forward record in the git folder makes the return all or nothing:

1. The record lists the temporary siblings, in state staging.
2. Every fixed file goes to its temporary sibling.
3. The record changes to state commit.
4. The renames and removals run, then the record is deleted.

A stop leaves the record. The next verify.sh run calls `fix_return.py finish`
before its first step. A staging record removes its siblings, so the tree is
unchanged. A commit record finishes the renames, so the tree has every fix.

Usage: fix_return.py finish --repo-root DIR
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Literal, TypedDict, cast


@dataclass(frozen=True)
class FixApplied:
    """The worktree received the changed files and lost the removed files."""

    changed: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class FixNone:
    """The Mac fix pass changed no file."""


@dataclass(frozen=True)
class FixRefused:
    """The worktree changed after the copy, so no file was written."""

    reason: str


@dataclass(frozen=True)
class FixFailed:
    """The changed files could not be listed, fetched or written."""

    reason: str


@dataclass(frozen=True)
class FixNotRun:
    """The run made no fix pass, or its fix pass did not pass."""


FixResult = FixApplied | FixNone | FixRefused | FixFailed | FixNotRun
FIX_NOT_RUN = FixNotRun()


@dataclass(frozen=True)
class MirrorChanges:
    """Repository-relative files that the fix pass changed, added or removed."""

    changed: tuple[str, ...]
    removed: tuple[str, ...]


# rsync --itemize-changes: the update type, the file type, then nine attribute letters.
ITEMIZED_LINE = re.compile(r"([<>ch.*])([fdLDS])([^ ]{9}) (.+)")
DELETED_LINE = re.compile(r"\*deleting +(.+)")


def fix_word(result: FixResult) -> str:
    if isinstance(result, FixApplied):
        return "applied"
    if isinstance(result, FixNone):
        return "none"
    if isinstance(result, FixRefused):
        return "refused"
    if isinstance(result, FixFailed):
        return "failed"
    return "not_run"


def fix_passed(result: FixResult) -> bool:
    """A fix pass passes only when the worktree now holds its files."""
    return isinstance(result, (FixApplied, FixNone))


def parse_itemized(output: str) -> MirrorChanges | FixFailed:
    """Read a dry-run listing from the mirror to the worktree.

    A file counts as changed when it is new or its content differs (-c). A
    change of mode alone does not count. A directory line has no content.
    """
    changed: list[str] = []
    removed: list[str] = []
    for line in output.splitlines():
        if deleted := DELETED_LINE.fullmatch(line):
            path = deleted.group(1)
            if not path.endswith("/"):
                removed.append(path)
            continue
        itemized = ITEMIZED_LINE.fullmatch(line)
        if itemized is None:
            continue
        update, kind, attributes, path = itemized.groups()
        if kind == "d":
            continue
        if kind != "f":
            return FixFailed(f"the fix pass changed {path}, which is not a regular file")
        if update == ">" and (attributes[:2] == "++" or attributes[0] == "c" or attributes[1] == "s"):
            changed.append(path)
    return MirrorChanges(tuple(changed), tuple(removed))


def without_ignored(repo_root: Path, changes: MirrorChanges) -> MirrorChanges | FixFailed:
    """Drop the paths that git ignores, such as files the Mac build wrote."""
    paths = (*changes.changed, *changes.removed)
    if not paths:
        return changes
    try:
        listed = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "--stdin", "-z"],
            input="\0".join(paths) + "\0", capture_output=True, text=True, check=False,
        )
    except OSError as error:
        return FixFailed(f"git check-ignore: {error}")
    # check-ignore exits 1 when no path is ignored.
    if listed.returncode not in (0, 1):
        return FixFailed("git check-ignore failed: " + listed.stderr.strip())
    ignored = {path for path in listed.stdout.split("\0") if path}
    return MirrorChanges(tuple(path for path in changes.changed if path not in ignored),
                         tuple(path for path in changes.removed if path not in ignored))


def worktree_path(repo_root: Path, relative: str) -> Path:
    """Resolve a mirror path in the worktree; refuse a path that leaves it."""
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise OSError(f"{relative} is not inside the worktree")
    path = repo_root / relative
    if path.is_symlink() or not path.parent.resolve().is_relative_to(repo_root.resolve()):
        raise OSError(f"{relative} is not inside the worktree")
    return path


RECORD_NAME = "mac-fix.json"
LOCK_NAME = "mac-fix.lock"
TEMPORARY_MARK = ".mac-fix-"


class FixRecord(TypedDict):
    """The roll-forward record: repository-relative paths only."""

    state: Literal["staging", "commit"]
    renames: list[tuple[str, str]]
    removals: list[str]


def git_folder_path(repo_root: Path, name: str) -> Path:
    """Resolve a file in the worktree's own git folder."""
    listed = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--git-path", name],
                            capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        raise OSError("git rev-parse --git-path failed: " + listed.stderr.strip())
    return repo_root / listed.stdout.strip()


@contextmanager
def record_lock(repo_root: Path) -> Generator[Path, None, None]:
    """Hold the worktree's fix lock; yield the record path."""
    lock_path = git_folder_path(repo_root, LOCK_NAME)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield git_folder_path(repo_root, RECORD_NAME)


def write_record(path: Path, record: FixRecord) -> None:
    """Replace the record in one rename, with its data and its folder on disk."""
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8") as output:
        _ = output.write(json.dumps(record))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    folder = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def read_record(path: Path) -> FixRecord:
    record = cast(FixRecord, json.loads(path.read_text(encoding="utf-8")))
    if record["state"] not in ("staging", "commit"):
        raise OSError(f"{path} has an unknown state")
    return record


def stage_file(source: Path, temporary: Path, destination: Path) -> None:
    """Write the staged content to the temporary sibling, with the mode of the file it replaces."""
    mode = destination.stat().st_mode if destination.exists() else source.stat().st_mode
    with temporary.open("xb") as output:
        _ = output.write(source.read_bytes())
    os.chmod(temporary, mode & 0o7777)


def finish_record(repo_root: Path, path: Path) -> str:
    """Complete the record at path; the caller holds the lock. Return what was done."""
    if not path.exists():
        return ""
    record = read_record(path)
    renames = [(worktree_path(repo_root, temporary), worktree_path(repo_root, destination))
               for temporary, destination in record["renames"]]
    if record["state"] == "staging":
        for temporary, _ in renames:
            temporary.unlink(missing_ok=True)
        path.unlink()
        return f"removed {len(renames)} staged file(s) of a stopped Mac fix; the worktree is unchanged"
    for temporary, destination in renames:
        if temporary.exists():
            os.replace(temporary, destination)
    for relative in record["removals"]:
        worktree_path(repo_root, relative).unlink(missing_ok=True)
    path.unlink()
    return f"finished a stopped Mac fix: {len(renames)} file(s) written, {len(record['removals'])} removed"


def finish(repo_root: Path) -> str:
    """Complete a stopped fix return in this worktree, if one is left."""
    with record_lock(repo_root) as path:
        return finish_record(repo_root, path)


def apply_fix(repo_root: Path, staging: Path, changes: MirrorChanges,
              worktree_unchanged: Callable[[], bool]) -> FixResult:
    """Write the staged files when the worktree still has the sent tree key."""
    if not worktree_unchanged():
        return FixRefused("the worktree changed after the copy to the Mac")
    try:
        token = secrets.token_hex(4)
        renames = [(str(Path(relative).with_name(f".{Path(relative).name}{TEMPORARY_MARK}{token}")), relative)
                   for relative in changes.changed]
        targets = [(staging / destination, worktree_path(repo_root, temporary), worktree_path(repo_root, destination))
                   for temporary, destination in renames]
        for relative in changes.removed:
            _ = worktree_path(repo_root, relative)
        for source, _, _ in targets:
            if not source.is_file() or source.is_symlink():
                return FixFailed(f"the staged copy of {source.relative_to(staging)} is missing")
        with record_lock(repo_root) as path:
            record = FixRecord(state="staging", renames=renames, removals=list(changes.removed))
            write_record(path, record)
            try:
                for source, temporary, destination in targets:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    stage_file(source, temporary, destination)
            except OSError:
                _ = finish_record(repo_root, path)
                raise
            write_record(path, FixRecord(state="commit", renames=renames, removals=list(changes.removed)))
            _ = finish_record(repo_root, path)
    except OSError as error:
        return FixFailed(str(error))
    return FixApplied(changes.changed, changes.removed)


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="fix_return.py")
    commands = parser.add_subparsers(dest="command", required=True)
    finishing = commands.add_parser("finish", help="complete a stopped Mac fix return")
    _ = finishing.add_argument("--repo-root", required=True)
    parsed = parser.parse_args(arguments)
    try:
        done = finish(Path(cast(str, parsed.repo_root)))
    except (OSError, ValueError, KeyError) as error:
        print(f"fix_return.py: the stopped Mac fix could not be finished: {error}", file=sys.stderr)
        return 2
    if done:
        print(f"verify.sh: {done}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
