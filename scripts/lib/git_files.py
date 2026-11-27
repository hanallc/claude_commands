"""Read Git facts that live in plain files, with no Git start.

This is the one reader of `.git`, `commondir` and `HEAD`. A `.git` folder is
the Git folder. A `.git` file names the Git folder on its `gitdir:` line, and
that folder's `commondir` file names the shared Git folder.
"""
from __future__ import annotations

import codecs
import errno
import os
import re
import stat
import subprocess
import time
from collections import deque
from collections.abc import Callable, Generator
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


GIT_STORAGE_REASON = "the folder contains Git storage"


def is_git_directory(folders: set[str], files: set[str]) -> bool:
    """Recognize a bare Git directory or the directory for a linked checkout."""
    return "HEAD" in files and ("objects" in folders or "commondir" in files)


def has_git_storage(folder: Path, folders: set[str], files: set[str]) -> bool:
    """Check the names and types in one directory for Git storage."""
    if ".git" in folders or ".git" in files or is_git_directory(folders, files):
        return True
    if {"info", "pack"} <= folders or (folder.name == "info" and "alternates" in files):
        return True
    if folder.name == "pack":
        return any(re.fullmatch(r"pack-(?:[0-9a-f]{40}|[0-9a-f]{64})\.pack", name) for name in files)
    return len(folder.name) == 2 and bool(re.fullmatch(r"[0-9a-f]{2}", folder.name)) and any(
        re.fullmatch(r"[0-9a-f]{38}|[0-9a-f]{62}", name) for name in files)


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
            missing: type[UnreadableGitFiles] = UnreadableGitFiles,
            on_link: Callable[[Path], None] | None = None) -> _ResolvedGitDirectory:
    if on_link is not None:
        _ = _path_links(path, on_link)
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


def _common_directory(git_dir: _ResolvedGitDirectory, on_link: Callable[[Path], None] | None) -> _ResolvedGitDirectory:
    common = git_dir.path / "commondir"
    if on_link is not None:
        _ = _path_links(common, on_link)
    try:
        location = _read_entry(common).strip()
    except FileNotFoundError:
        return git_dir
    if not location:
        raise UnreadableGitFiles(common, "empty commondir path")
    return _folder(git_dir.path / location, common, on_link=on_link)


def objects_in_git_directory(git_dir: Path, *, on_link: Callable[[Path], None] | None = None) -> Path:
    """Find the object directory for a bare or separate Git directory."""
    return _common_directory(_folder(git_dir, git_dir, on_link=on_link), on_link).path / "objects"


def _git_file_location(path: Path, entry: str) -> str:
    prefix, separator, location = entry.strip().partition(":")
    if not separator or prefix != "gitdir" or not location.strip():
        raise UnreadableGitFiles(path, "expected a gitdir: line")
    return location.strip()


def worktree_back_pointer(registration: Path, *, parent: int) -> Path:
    """Read the checkout that a worktree registration names."""
    pointer = registration / "gitdir"
    directory = os.open(registration.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        descriptor = os.open("gitdir", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UnreadableGitFiles(pointer, "expected a regular gitdir file")
            with os.fdopen(descriptor, encoding="utf-8", errors="surrogateescape", closefd=False) as stream:
                target = stream.read().removesuffix("\n")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)
    if not target:
        raise UnreadableGitFiles(pointer, "empty gitdir path")
    return (registration / target).resolve()


def at(toplevel: Path, *, on_link: Callable[[Path], None] | None = None) -> Checkout:
    """Read the checkout whose `.git` entry is in this folder, with no search of parent folders."""
    dot_git = toplevel / ".git"
    if on_link is not None:
        _ = _path_links(dot_git, on_link)
    try:
        entry = _read_entry(dot_git, allow_directory=True)
    except FileNotFoundError as error:
        raise NotACheckout(f"{toplevel}: no .git entry") from error
    if isinstance(entry, _ResolvedGitDirectory):
        git_dir = entry
    else:
        location = _git_file_location(dot_git, entry)
        git_dir = _folder(toplevel / location, dot_git, missing=MissingReferencedGitDirectory, on_link=on_link)
    common_dir = _common_directory(git_dir, on_link)
    return Checkout(toplevel.resolve(), git_dir.path, common_dir.path, git_dir.status, common_dir.status)


def locate(folder: Path, *, on_link: Callable[[Path], None] | None = None) -> Checkout:
    """Find the checkout that holds the folder, as `git -C <folder>` finds it."""
    if on_link is not None:
        _ = _path_links(folder.expanduser(), on_link)
    start = folder.expanduser().resolve()
    if not start.is_dir():
        raise NotACheckout(f"{folder}: not a folder")
    for toplevel in (start, *start.parents):
        try:
            return at(toplevel, on_link=on_link)
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


def _path_links(path: Path, on_link: Callable[[Path], None]) -> Path:
    """Resolve the path and record every link that the lookup uses."""
    anchor, *parts = path.absolute().parts
    current = Path(anchor)
    pending = deque(parts)
    followed = 0
    while pending:
        part = pending.popleft()
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        try:
            target = Path(os.readlink(candidate))
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOENT, errno.ENOTDIR):
                raise
            current = candidate
            continue
        on_link(candidate)
        followed += 1
        if followed > 40:
            raise ValueError(f"{path}: too many symbolic links")
        parts = list(target.parts)
        if target.is_absolute():
            current = Path(parts.pop(0))
        pending.extendleft(reversed(parts))
    return current


@dataclass(frozen=True)
class AlternateEvidence:
    paths: tuple[Path, ...]
    files: frozenset[Path]
    unreadable: str


def object_stores(objects: Path, cache: dict[Path, AlternateEvidence], deadline: float, *,
                  hold: Callable[[Path], None] | None = None) -> tuple[set[Path], set[Path], str]:
    """Find object stores and the files that they need. Hold paths before opening the alternates file."""
    pending = [objects]
    visited: set[Path] = set()
    stores: set[Path] = set()
    files: set[Path] = set()
    unreadable = ""
    while pending:
        if time.monotonic() >= deadline:
            return stores, files, unreadable or "the borrowed object scan timed out"
        store = pending.pop()
        stores.add(Path(os.path.abspath(store)))
        links: set[Path] = set()
        try:
            folder = _path_links(store, links.add)
        except (OSError, ValueError) as error:
            files.update(links)
            unreadable = unreadable or str(error)
            continue
        stores.add(folder)
        files.update(links)
        if hold is not None:
            for path in (store, folder, *sorted(links)):
                hold(path)
        if folder in visited:
            continue
        visited.add(folder)
        evidence = cache.get(folder)
        if evidence is None:
            evidence = cache[folder] = _read_alternates(folder, deadline, hold)
        pending.extend(evidence.paths)
        files.update(evidence.files)
        unreadable = unreadable or evidence.unreadable
    return stores, files, unreadable


def _read_alternates(objects: Path, deadline: float, hold: Callable[[Path], None] | None) -> AlternateEvidence:
    alternates = objects / "info/alternates"
    files: set[Path] = set()
    paths: list[Path] = []
    reason = ""
    try:
        path = _path_links(alternates, files.add)
        if os.path.lexists(alternates):
            files.update((alternates, path))
    except (OSError, ValueError) as error:
        return AlternateEvidence((), frozenset(files), str(error))
    if hold is not None:
        for dependency in sorted(files):
            hold(dependency)
    try:
        if not objects.is_dir():
            raise ValueError(f"{objects} is not an object directory")
        paths.extend(_read_alternate_paths(objects, alternates, path, deadline))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        reason = str(error)
    return AlternateEvidence(tuple(paths), frozenset(files), reason)


def _read_alternate_paths(objects: Path, alternates: Path, path: Path, deadline: float) -> Generator[Path]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError as error:
        try:
            _ = alternates.lstat()
        except FileNotFoundError:
            if alternates.parent.is_symlink():
                _ = alternates.parent.resolve(strict=True)
            return
        raise ValueError(f"{alternates}: cannot open {path}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path} is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            while line := source.readline(65536):
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("inspect local object storage", 0)
                if len(line) == 65536 or b"\0" in line:
                    raise ValueError(f"{path} has an invalid alternate")
                if line != b"\n" and not line.startswith(b"#"):
                    name = line.removesuffix(b"\n")
                    if name.startswith(b'"'):
                        if not re.fullmatch(rb'"(?:[^"\\]|\\(?:[abfnrtv"\\]|[0-3][0-7]{2}))*"', name):
                            raise ValueError(f"{path} has an invalid quoted alternate")
                        name, _ = codecs.escape_decode(name[1:-1])
                        if not name or b"\0" in name:
                            raise ValueError(f"{path} has an invalid alternate")
                    yield objects / os.fsdecode(name)
    finally:
        os.close(descriptor)
