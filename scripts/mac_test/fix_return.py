"""Bring the files of a Mac fix pass back into the worktree that the Mac copied.

offload.py lists the mirror files that differ from the worktree, fetches them
to a staging folder, and calls apply_fix. apply_fix writes them only when the
worktree still has the tree key that the copy sent.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile


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


def replace_file(source: Path, destination: Path) -> None:
    """Write the staged content in one rename and keep the mode of a file the worktree has."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = destination.stat().st_mode if destination.exists() else source.stat().st_mode
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.mac-fix-")
    try:
        with os.fdopen(descriptor, "wb") as output:
            _ = output.write(source.read_bytes())
        os.chmod(temporary, mode & 0o7777)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def apply_fix(repo_root: Path, staging: Path, changes: MirrorChanges,
              worktree_unchanged: Callable[[], bool]) -> FixResult:
    """Write the staged files when the worktree still has the sent tree key."""
    if not worktree_unchanged():
        return FixRefused("the worktree changed after the copy to the Mac")
    try:
        targets = [(staging / relative, worktree_path(repo_root, relative)) for relative in changes.changed]
        removals = [worktree_path(repo_root, relative) for relative in changes.removed]
        for source, _ in targets:
            if not source.is_file() or source.is_symlink():
                return FixFailed(f"the staged copy of {source.relative_to(staging)} is missing")
        for source, destination in targets:
            replace_file(source, destination)
        for path in removals:
            path.unlink(missing_ok=True)
    except OSError as error:
        return FixFailed(str(error))
    return FixApplied(changes.changed, changes.removed)
