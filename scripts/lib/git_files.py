"""Read Git facts that live in plain files, with no Git start.

This is the one reader of `.git`, `commondir` and `HEAD`. A `.git` folder is
the Git folder. A `.git` file names the Git folder on its `gitdir:` line, and
that folder's `commondir` file names the shared Git folder.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, overload

_BRANCH_PREFIX = "ref: refs/heads/"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK


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
    git_dir_status: os.stat_result = field(compare=False, repr=False)
    common_dir_status: os.stat_result = field(compare=False, repr=False)


@dataclass(frozen=True)
class _ResolvedGitDirectory:
    path: Path
    status: os.stat_result


@dataclass(frozen=True)
class OnBranch:
    """The name identifies the checked out Git branch."""
    name: str


@dataclass(frozen=True)
class DetachedHead:
    """HEAD names a commit, or holds a ref outside refs/heads."""

    commit: str


@dataclass(frozen=True)
class RecordedRemovalFiles:
    """The removal intent lists the regular files relative to the removal path."""

    relative_paths: frozenset[str]


@dataclass(frozen=True)
class LegacyRemovalFiles:
    """The removal intent gives no list of the regular files."""


LEGACY_REMOVAL_FILES = LegacyRemovalFiles()


@dataclass(frozen=True)
class _AbsentGitEntry:
    """The Git path has no directory entry."""


def _entry_status(path: Path) -> os.stat_result | _AbsentGitEntry:
    try:
        return path.lstat()
    except FileNotFoundError:
        return _AbsentGitEntry()
    except OSError as error:
        raise UnreadableGitFiles(path, f"cannot inspect: {error}") from error


@overload
def _read_entry(path: Path, *, allow_directory: Literal[False] = False, errors: str = "strict") -> str: ...


@overload
def _read_entry(path: Path, *, allow_directory: Literal[True], errors: str = "strict") -> str | _ResolvedGitDirectory: ...


def _read_entry(path: Path, *, allow_directory: bool = False, errors: str = "strict") -> str | _ResolvedGitDirectory:
    """Read metadata or return the directory path for a .git folder."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError as error:
        if isinstance(_entry_status(path), _AbsentGitEntry):
            raise error
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    except OSError as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    try:
        status = os.fstat(descriptor)
        if allow_directory and stat.S_ISDIR(status.st_mode):
            directory = _folder(path, path)
            if not os.path.samestat(status, directory.status):
                raise UnreadableGitFiles(path, "directory changed during inspection")
            return directory
        if not stat.S_ISREG(status.st_mode):
            raise UnreadableGitFiles(path, "expected a regular file")
        with os.fdopen(descriptor, encoding="utf-8", errors=errors, closefd=False) as source:
            return source.read()
    except (OSError, UnicodeError) as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    finally:
        os.close(descriptor)


def _folder(path: Path, named_by: Path, *,
            missing: type[UnreadableGitFiles] = UnreadableGitFiles) -> _ResolvedGitDirectory:
    try:
        found = path.resolve(strict=True)
        status = found.lstat()
    except FileNotFoundError as error:
        raise missing(named_by, f"{path} is missing") from error
    except (OSError, RuntimeError, ValueError) as error:
        raise UnreadableGitFiles(named_by, f"cannot resolve {path}: {error}") from error
    if not stat.S_ISDIR(status.st_mode):
        raise UnreadableGitFiles(named_by, f"{found} is not a folder")
    return _ResolvedGitDirectory(found, status)


def at(toplevel: Path) -> Checkout:
    """Read the checkout whose `.git` entry is in this folder, with no search of parent folders."""
    dot_git = toplevel / ".git"
    try:
        entry = _read_entry(dot_git, allow_directory=True)
    except FileNotFoundError as error:
        raise NotACheckout(f"{toplevel}: no .git entry") from error
    if isinstance(entry, _ResolvedGitDirectory):
        git_dir = entry
    else:
        entry = entry.strip()
        location = entry.removeprefix("gitdir:").strip()
        if not entry.startswith("gitdir:") or not location:
            raise UnreadableGitFiles(dot_git, "expected a gitdir: line")
        git_dir = _folder(toplevel / location, dot_git, missing=MissingReferencedGitDirectory)
    common = git_dir.path / "commondir"
    try:
        location = _read_entry(common).strip()
    except FileNotFoundError:
        return Checkout(toplevel.resolve(), git_dir.path, git_dir.path, git_dir.status, git_dir.status)
    if not location:
        raise UnreadableGitFiles(common, "empty commondir path")
    common_dir = _folder(git_dir.path / location, common)
    return Checkout(toplevel.resolve(), git_dir.path, common_dir.path, git_dir.status, common_dir.status)


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
        text = _read_entry(path).strip()
    except FileNotFoundError as error:
        raise UnreadableGitFiles(path, f"cannot read: {error}") from error
    return OnBranch(text.removeprefix(_BRANCH_PREFIX)) if text.startswith(_BRANCH_PREFIX) else DetachedHead(text)


def check_internal_head(checkout: Checkout) -> None:
    """Check HEAD if an interrupted removal left its entry."""
    if not isinstance(_entry_status(checkout.git_dir / "HEAD"), _AbsentGitEntry):
        _ = head(checkout)


def _require_regular(path: Path, mode: int, parent: int) -> None:
    if stat.S_ISLNK(mode):
        try:
            mode = os.stat(path.name, dir_fd=parent).st_mode
        except OSError as error:
            raise UnreadableGitFiles(path, f"cannot inspect: {error}") from error
    if not stat.S_ISREG(mode):
        raise UnreadableGitFiles(path, "expected a regular file")


def _packed_ref_names(path: Path) -> frozenset[str]:
    try:
        packed = _read_entry(path, errors="surrogateescape")
    except FileNotFoundError:
        return frozenset()
    return frozenset(line.partition(" ")[2] for line in packed.split("\n") if not line.startswith(("#", "^")))


@contextmanager
def _open_inspected_directory(folder: Path, inspected: os.stat_result, *, parent: int | None) -> Generator[int]:
    name = folder if parent is None else folder.name
    if not stat.S_ISDIR(inspected.st_mode):
        raise UnreadableGitFiles(folder, "expected a directory")
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        try:
            if not os.path.samestat(inspected, os.fstat(descriptor)):
                raise UnreadableGitFiles(folder, "directory changed before open")
            yield descriptor
            present = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not os.path.samestat(inspected, present):
                raise UnreadableGitFiles(folder, "directory changed during inspection")
        finally:
            os.close(descriptor)
    except OSError as error:
        raise UnreadableGitFiles(folder, f"cannot inspect directory: {error}") from error


def _check_symbolic_ref(path: Path, parent: int, inspected: os.stat_result) -> None:
    try:
        target = os.readlink(path.name, dir_fd=parent)
        present = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        raise UnreadableGitFiles(path, f"cannot read symbolic ref: {error}") from error
    if (not target.startswith("refs/") or target.endswith(".") or ".." in target or "@{" in target
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in target.split("/"))
            or any(ord(char) <= 32 or ord(char) == 127 or char in "~^:?*[\\" for char in target)):
        raise UnreadableGitFiles(path, "expected a symbolic ref target under refs/")
    if not os.path.samestat(inspected, present) or inspected.st_ctime_ns != present.st_ctime_ns:
        raise UnreadableGitFiles(path, "symbolic ref changed during inspection")


@dataclass(frozen=True)
class RemovalEntry:
    """The path and status observed without following symbolic links."""

    path: Path
    status: os.stat_result


class RemovalScan(Enum):
    """Select the directories that the scan reads."""

    METADATA = "metadata"
    SOURCE = "source"


def _walk_removal_entries(root: Path, *, inspected: os.stat_result,
                          scope: RemovalScan, metadata_paths: set[Path], refs: set[Path]) -> Generator[tuple[RemovalEntry, int]]:
    """Yield each entry with its parent descriptor open."""

    def visit(folder: Path, status: os.stat_result, parent: int | None, in_refs: bool) -> Generator[tuple[RemovalEntry, int]]:
        with _open_inspected_directory(folder, status, parent=parent) as descriptor:
            with os.scandir(descriptor) as entries:
                for child in entries:
                    path = folder / child.name
                    seen = child.stat(follow_symlinks=False)
                    yield RemovalEntry(path, seen), descriptor
                    ref_tree = in_refs or path in refs
                    if stat.S_ISDIR(seen.st_mode) and (scope is RemovalScan.SOURCE or ref_tree or path in metadata_paths):
                        yield from visit(path, seen, descriptor, ref_tree)

    yield from visit(root, inspected, None, False)


def scan_removal_tree(root: Path, *, checkouts: tuple[Checkout, ...] = (),
                      previous: RecordedRemovalFiles | LegacyRemovalFiles = LEGACY_REMOVAL_FILES,
                      scope: RemovalScan = RemovalScan.SOURCE) -> dict[Path, RemovalEntry]:
    """Observe source entries and validate the Git metadata inside the removal tree."""
    directories: dict[Path, os.stat_result] = {}
    regular: set[Path] = set()
    git_entries: set[Path] = set()
    refs: dict[Path, Path] = {}
    for checkout in checkouts:
        git_dir, common_dir = checkout.git_dir, checkout.common_dir
        git_entries.add(checkout.toplevel / ".git")
        if git_dir.is_relative_to(root):
            directories[git_dir] = checkout.git_dir_status
            regular.update(git_dir / name for name in ("HEAD", "index", "commondir"))
        if common_dir.is_relative_to(root):
            directories[common_dir] = checkout.common_dir_status
            regular.add(common_dir / "packed-refs")
            refs[common_dir / "refs"] = common_dir

    metadata_paths = {folder for path in (*directories, *git_entries) for folder in (path, *path.parents)
                      if folder.is_relative_to(root)}
    ref_directories: dict[Path, Path] = {}
    empty_refs: set[Path] = set()
    found: dict[Path, RemovalEntry] = {}
    try:
        root_status = root.lstat()
        if root in directories and not os.path.samestat(root_status, directories[root]):
            raise UnreadableGitFiles(root, "directory changed before open")
        with closing(_walk_removal_entries(root, inspected=root_status, scope=scope,
                                           metadata_paths=metadata_paths, refs=set(refs))) as entries:
            for entry, parent in entries:
                path, status = entry.path, entry.status
                found[path] = entry
                mode = status.st_mode
                if path in directories and not os.path.samestat(status, directories[path]):
                    raise UnreadableGitFiles(path, "directory changed before open")
                if path in git_entries and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise UnreadableGitFiles(path, "expected a Git file or directory")
                if path in regular:
                    _require_regular(path, mode, parent)
                if path in refs:
                    if not stat.S_ISDIR(mode):
                        raise UnreadableGitFiles(path, "expected a directory")
                    ref_directories[path] = refs[path]
                    empty_refs.add(path)
                elif path.parent in ref_directories:
                    empty_refs.discard(path.parent)
                    if stat.S_ISDIR(mode):
                        if (isinstance(previous, RecordedRemovalFiles)
                                and str(path.relative_to(root)) in previous.relative_paths):
                            raise UnreadableGitFiles(path, "expected a regular file")
                        ref_directories[path] = ref_directories[path.parent]
                        empty_refs.add(path)
                    elif stat.S_ISLNK(mode):
                        _check_symbolic_ref(path, parent, status)
                    elif not stat.S_ISREG(mode):
                        raise UnreadableGitFiles(path, "expected a regular file")
    except OSError as error:
        raise UnreadableGitFiles(root, f"cannot inspect tree: {error}") from error

    for directory in directories.keys() - found.keys() - {root}:
        raise UnreadableGitFiles(directory, "directory disappeared during inspection")
    if isinstance(previous, LegacyRemovalFiles):
        packed_by_common_dir: dict[Path, frozenset[str]] = {}
        for folder in sorted(empty_refs):
            common = ref_directories[folder]
            name = folder.relative_to(common)
            if str(name) not in {"refs", "refs/heads", "refs/tags", "refs/remotes"}:
                raise UnreadableGitFiles(folder, "the legacy intent cannot distinguish this empty directory from a replaced ref")
            if common not in packed_by_common_dir:
                packed_by_common_dir[common] = _packed_ref_names(common / "packed-refs")
            log = _entry_status(common / "logs" / name)
            if str(name) in packed_by_common_dir[common] or (isinstance(log, os.stat_result) and stat.S_ISREG(log.st_mode)):
                raise UnreadableGitFiles(folder, "ref evidence requires a regular file here")
    return found


def check_internal_removal_entries(checkouts: tuple[Checkout, ...], previous: RecordedRemovalFiles | LegacyRemovalFiles, *,
                                   removal_root: Path) -> None:
    """Validate the Git metadata inside the removal tree."""
    if checkouts:
        _ = scan_removal_tree(removal_root, checkouts=checkouts, previous=previous, scope=RemovalScan.METADATA)


def current_branch(folder: Path) -> str:
    """The checked-out branch, or "" for a detached HEAD, as `git branch --show-current` prints it."""
    found = head(locate(folder))
    return found.name if isinstance(found, OnBranch) else ""
