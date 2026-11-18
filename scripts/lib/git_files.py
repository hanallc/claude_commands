"""Read Git facts that live in plain files, with no Git start.

This is the one reader of `.git`, `commondir` and `HEAD`. A `.git` folder is
the Git folder. A `.git` file names the Git folder on its `gitdir:` line, and
that folder's `commondir` file names the shared Git folder.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

_BRANCH_PREFIX = "ref: refs/heads/"


class GitFilesError(ValueError):
    """The Git files give no checkout."""


class NotACheckout(GitFilesError):
    """No `.git` entry marks the folder as a checkout."""


class UnreadableGitFiles(GitFilesError):
    """A `.git` entry exists, but it or the files it names cannot be read."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path: Path = path
        self.reason: str = reason


class MissingReferencedGitDirectory(UnreadableGitFiles):
    """The directory that a gitdir: line names is missing."""


@dataclass(frozen=True)
class Checkout:
    """The folders of one checkout, resolved, as `git rev-parse` prints them in absolute form."""

    toplevel: Path
    git_dir: Path
    common_dir: Path


@dataclass(frozen=True)
class OnBranch:
    """The name identifies the checked out Git branch."""
    name: str


@dataclass(frozen=True)
class DetachedHead:
    """HEAD names a commit, or holds a ref outside refs/heads."""

    commit: str


@overload
def _read_entry(path: Path, *, allow_directory: Literal[False] = False) -> str: ...


@overload
def _read_entry(path: Path, *, allow_directory: Literal[True]) -> str | Path: ...


def _read_entry(path: Path, *, allow_directory: bool = False) -> str | Path:
    """Read a metadata line or return the directory path for a .git folder."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError as error:
        try:
            _ = path.lstat()
        except FileNotFoundError:
            raise error
        except OSError as inspection_error:
            raise UnreadableGitFiles(path, f"cannot inspect: {inspection_error}") from inspection_error
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    except OSError as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    try:
        mode = os.fstat(descriptor).st_mode
        if allow_directory and stat.S_ISDIR(mode):
            return _folder(path, path)
        if not stat.S_ISREG(mode):
            raise UnreadableGitFiles(path, "expected a regular file")
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as source:
            return source.read().strip()
    except (OSError, UnicodeError) as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    finally:
        os.close(descriptor)


def _folder(path: Path, named_by: Path, *,
            missing: type[UnreadableGitFiles] = UnreadableGitFiles) -> Path:
    try:
        found = path.resolve(strict=True)
        mode = found.stat().st_mode
    except FileNotFoundError as error:
        raise missing(named_by, f"{path} is missing") from error
    except (OSError, RuntimeError, ValueError) as error:
        raise UnreadableGitFiles(named_by, f"cannot resolve {path}: {error}") from error
    if not stat.S_ISDIR(mode):
        raise UnreadableGitFiles(named_by, f"{found} is not a folder")
    return found


def at(toplevel: Path) -> Checkout:
    """Read the checkout whose `.git` entry is in this folder, with no search of parent folders."""
    dot_git = toplevel / ".git"
    try:
        entry = _read_entry(dot_git, allow_directory=True)
    except FileNotFoundError as error:
        raise NotACheckout(f"{toplevel}: no .git entry") from error
    if isinstance(entry, Path):
        git_dir = entry
    else:
        location = entry.removeprefix("gitdir:").strip()
        if not entry.startswith("gitdir:") or not location:
            raise UnreadableGitFiles(dot_git, "expected a gitdir: line")
        git_dir = _folder(toplevel / location, dot_git, missing=MissingReferencedGitDirectory)
    common = git_dir / "commondir"
    try:
        location = _read_entry(common)
    except FileNotFoundError:
        return Checkout(toplevel.resolve(), git_dir, git_dir)
    if not location:
        raise UnreadableGitFiles(common, "empty commondir path")
    return Checkout(toplevel.resolve(), git_dir, _folder(git_dir / location, common))


def locate(folder: Path) -> Checkout:
    """Find the checkout that holds the folder, as `git -C <folder>` finds it."""
    start = folder.expanduser().resolve()
    if not start.is_dir():
        raise NotACheckout(f"{folder}: not a folder")
    for toplevel in (start, *start.parents):
        try:
            return at(toplevel)
        except NotACheckout:
            continue
    raise NotACheckout(f"{folder}: not in a Git checkout")


def head(checkout: Checkout) -> OnBranch | DetachedHead:
    """Read HEAD in the checkout's own Git folder."""
    path = checkout.git_dir / "HEAD"
    try:
        text = _read_entry(path)
    except FileNotFoundError as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    return OnBranch(text.removeprefix(_BRANCH_PREFIX)) if text.startswith(_BRANCH_PREFIX) else DetachedHead(text)


def current_branch(folder: Path) -> str:
    """The checked-out branch, or "" for a detached HEAD, as `git branch --show-current` prints it."""
    found = head(locate(folder))
    return found.name if isinstance(found, OnBranch) else ""
