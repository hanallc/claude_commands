#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Name build folders and preserve the caller's use of temporary folders."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Collection, Container, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import ctypes
import fcntl
import json
import os
import errno
import re
import secrets
import signal
import stat
import subprocess
from enum import Enum, auto
from importlib import import_module
from pathlib import Path
import sys
import tempfile
from threading import Lock, Thread, current_thread
import time
from typing import TYPE_CHECKING, Literal, Protocol, cast, final, overload, override

from urllib.parse import urlsplit

from scripts.lib import build_folder_lock, git_files, process_start
from scripts.lib.boot_identity import boot_identity
from scripts.lib.conf import UseConfiguredValue

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead

TEST_RUN_FOLDER_ENV = "DEVTOOLS_TEST_RUN_FOLDER"

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"
_TEMPORARY_BUILD_NAME = re.compile(rf"p-(?:{MKDTEMP_SUFFIX_PATTERN}|{LEGACY_MKTEMP_SUFFIX_PATTERN})")
BUSY = ".claude-busy"
REMOVAL_RECORD_PREFIX = ".scratch-removing-"
REMOVAL_RECORD_SUFFIX = ".json"


def removal_mark(path: Path) -> Path:
    """Locate the deletion intent beside the source."""
    return path.parent / f"{REMOVAL_RECORD_PREFIX}{path.name}{REMOVAL_RECORD_SUFFIX}"


def open_busy_mark(mark: Path, *, create: bool, exclusive: bool = False) -> int:
    """Return a descriptor for a regular busy mark. The caller closes it."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | (os.O_EXCL if exclusive else 0)
    descriptor = os.open(mark, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "busy mark must be a regular file", str(mark))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def hidden_removal_path(path: Path) -> bool:
    """Return whether the path is inside a tree staged for removal."""
    return any(_HIDDEN_REMOVAL.fullmatch(part) is not None for part in path.parts)


def _reject_staged_allocation(path: Path) -> None:
    if hidden_removal_path(path):
        raise ValueError(f"{path}: the build folder is staged for removal")


def allocation_parent_current(path: Path, directory: int) -> bool:
    """Check the parent after marking the child lock that a removal scan sees."""
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        return False
    _reject_staged_allocation(parent / path.name)
    return build_folder_lock.parent_current(parent / path.name, directory)


def require_no_removal_mark(path: Path) -> None:
    """Refuse to use a source with a deletion intent on it or an ancestor."""
    for folder in (path, *path.parents):
        try:
            _ = removal_mark(folder).lstat()
        except FileNotFoundError:
            continue
        raise BlockingIOError(errno.EWOULDBLOCK, "source removal has already started", str(folder))


class _SourceObjectCache(dict[Path, git_files.AlternateEvidence]):
    """Identify callbacks that occur while the scan reads an alternates file."""

    last_lookup: Path | None = None

    @overload
    def get(self, key: Path, default: None = None, /) -> git_files.AlternateEvidence | None: ...

    @overload
    def get(self, key: Path, default: git_files.AlternateEvidence, /) -> git_files.AlternateEvidence: ...

    @overload
    def get[Default](self, key: Path, default: Default, /) -> git_files.AlternateEvidence | Default: ...

    @override
    def get[Default](self, key: Path, default: Default | None = None, /) -> git_files.AlternateEvidence | Default | None:
        self.last_lookup = key
        return super().get(key, default)


def _hold_git_source_claims(source: Path, stack: ExitStack) -> tuple[set[int], tuple[Path, ...]]:
    descriptors = set(stack.enter_context(build_folder_use(source)))
    root = build_root().resolve()
    checked: set[Path] = set()

    def hold(path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        if absolute.is_relative_to(root) and absolute not in checked:
            descriptors.update(stack.enter_context(build_folder_use(absolute)))
            checked.add(absolute)

    if (source / "HEAD").is_file() and (source / "objects").is_dir():
        folders = (git_files.objects_in_git_directory(source, on_link=hold).parent, source)
    else:
        try:
            checkout = git_files.locate(source, on_link=hold)
        except git_files.NotACheckout:
            return descriptors, ()
        folders = checkout.common_dir, checkout.git_dir
    descriptors.update(stack.enter_context(build_folder_use(*folders)))
    cache = _SourceObjectCache()

    def hold_object_path(path: Path) -> None:
        # The scan caches each store after it reads the alternates file.
        if cache:
            try:
                mode = path.stat().st_mode
            except (FileNotFoundError, NotADirectoryError):
                mode = None
            if mode is None or (cache.last_lookup in cache and not stat.S_ISDIR(mode)):
                _ = cache.setdefault(path, git_files.AlternateEvidence((), frozenset(), ""))
                return
        hold(path)

    _, _, unreadable = git_files.object_stores(folders[0] / "objects", cache,
        time.monotonic() + repository_git_timeout(), hold=hold_object_path)
    if unreadable:
        raise OSError(errno.EIO, f"cannot verify Git object storage: {unreadable}", str(source))
    return descriptors, folders


@contextmanager
def git_source_claims(source: Path) -> Generator[tuple[int, ...]]:
    """Hold the build folders that a Git source uses."""
    with ExitStack() as stack:
        descriptors, _ = _hold_git_source_claims(source.expanduser().absolute(), stack)
        yield tuple(sorted(descriptors))


@contextmanager
def linked_source_locks(source: Path) -> Generator[tuple[int, ...]]:
    """Hold the Git source while Git creates a worktree or shared clone."""
    source = source.expanduser().absolute()
    require_no_removal_mark(source.resolve())
    with ExitStack() as stack:
        descriptors, folders = _hold_git_source_claims(source, stack)
        for folder in dict.fromkeys(folders):
            mark = folder / BUSY
            descriptor = open_busy_mark(mark, create=True)
            _ = stack.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            descriptors.add(descriptor)
            require_no_removal_mark(folder)
            locked, present = os.fstat(descriptor), mark.lstat()
            if (locked.st_dev, locked.st_ino) != (present.st_dev, present.st_ino):
                raise BlockingIOError(errno.EWOULDBLOCK, "source busy mark changed", str(mark))
        require_no_removal_mark(source.resolve())
        yield tuple(sorted(descriptors))


class FolderPrefixes(Mapping[str, str]):
    """Read legacy caller prefixes from the current configuration."""

    @override
    def __getitem__(self, key: str) -> str:
        return _settings().text(f"prefixes.{key}")

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(key.removeprefix("prefixes.") for key in _settings().values() if key.startswith("prefixes."))

    @override
    def __len__(self) -> int:
        return len([key for key in _settings().values() if key.startswith("prefixes.")])


FOLDER_PREFIXES: Mapping[str, str] = FolderPrefixes()


@dataclass(frozen=True)
class InheritedTemporaryParent:
    """Use the caller's temporary parent."""


@dataclass(frozen=True)
class ExplicitBuildParent:
    """The path specifies the parent for a build folder."""
    path: Path


@dataclass(frozen=True)
class SystemTemporaryParent:
    """Use the short system temporary parent."""


@dataclass(frozen=True)
class UnitRunParent:
    """Use the director's session run root, else the unit root (unit_runs.new_run_root)."""


BuildFolderParent = InheritedTemporaryParent | ExplicitBuildParent | SystemTemporaryParent | UnitRunParent


@dataclass(frozen=True)
class UniqueBuildFolder:
    """The request holds the prefix and parent for a unique build folder."""
    prefix: str
    parent: BuildFolderParent = InheritedTemporaryParent()


@dataclass(frozen=True)
class _NoGateBootIdentity:
    """The gate has no boot identity to publish."""


@dataclass(frozen=True)
class _GateSuiteOwner:
    """Identify the process that requests a gate suite folder."""

    pid: int
    start: process_start.ProcessStart
    boot: str | _NoGateBootIdentity

    @classmethod
    def current(cls) -> _GateSuiteOwner:
        pid = os.getpid()
        start = process_start.process_start(pid)
        if sys.platform == "darwin":
            return cls(pid, start, _NoGateBootIdentity())
        try:
            return cls(pid, start, boot_identity())
        except (OSError, ValueError):
            return cls(pid, start, _NoGateBootIdentity())

    def publish(self, path: Path, directory: int, folder: os.stat_result) -> None:
        fields = _read_folder_lease(path, directory)
        fields.update(pid=self.pid, device=folder.st_dev, inode=folder.st_ino)
        if isinstance(self.start, process_start.Started):
            fields["process_start"] = self.start.start
        if isinstance(self.boot, str):
            fields["boot_id"] = self.boot
        _publish_lease(path, directory, fields)


@final
@dataclass(frozen=True)
class GateSuiteFolder(UniqueBuildFolder):
    """Read the owner identity before allocating a gate suite folder."""

    owner: _GateSuiteOwner = field(default_factory=_GateSuiteOwner.current, init=False)


@dataclass(frozen=True)
class StableOwnerFolder:
    """The request holds the name, parent, and mode for an owner folder."""
    name: str
    parent: BuildFolderParent
    mode: int = 0o777


@dataclass(frozen=True)
class NewWorktreeBranch:
    """Create the named branch from its start point."""
    name: str
    start_point: str


@dataclass(frozen=True)
class ExistingWorktreeBranch:
    """Use an existing branch without resetting it."""
    name: str


@dataclass(frozen=True)
class LinkedEditableRepository:
    """Create a persistent linked worktree at the exact caller path."""
    path: Path
    source: Path
    branch: NewWorktreeBranch | ExistingWorktreeBranch
    allow_empty: bool = False


@dataclass(frozen=True)
class RemoteEditableRepository:
    """Clone a remote ref at the exact caller path with the requested depth."""
    path: Path
    url: str
    ref: str
    depth: int


EditableRepositoryRequest = LinkedEditableRepository | RemoteEditableRepository


def inherited_temporary_parent() -> Path:
    """Read the caller's current temporary parent."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def configured_gate_root() -> Path:
    """Return the configured root for gate temporary folders."""
    return Path(_settings().text("roots.gate"))


def tmpfs_temporary_parent() -> Path:
    """Keep nested work under the caller's tmpfs folder."""
    root = configured_gate_root()
    inherited = inherited_temporary_parent().resolve()
    return inherited if inherited.is_relative_to(root) else root


def _parent(parent: BuildFolderParent) -> Path:
    if isinstance(parent, InheritedTemporaryParent):
        return inherited_temporary_parent()
    if isinstance(parent, SystemTemporaryParent):
        return Path(_settings().text("roots.system"))
    if isinstance(parent, UnitRunParent):
        from scripts.lib import unit_runs

        return unit_runs.new_run_root()
    return parent.path


def _check_name(name: str) -> None:
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("a build folder name must be one path component")


BuildFolderKind = Literal["temporary", "gate", "standing", "editable", "main", "mirror", "target",
                          "scratch", "legacy-unique", "legacy-stable", "tool", "copy"]


@dataclass(frozen=True)
class CopyCheckout:
    """The checkout holds the source repository and required commit for a numbered copy."""
    source: Path
    commit: str


class CopySourceBusy(BlockingIOError):
    """The source lock prevents creation of a numbered copy checkout."""


@dataclass(frozen=True)
class NoCopyCheckout:
    """The folder does not contain a numbered copy checkout."""


@dataclass(frozen=True, kw_only=True)
class BuildFolderSpec:
    """The specification holds the folder kind, parent, name parts, and checkout source."""
    kind: BuildFolderKind
    parent: Path
    suffix_length: int
    repository: str = ""
    owner: str = ""
    purpose: str = ""
    number: int = 0
    suffix: str = ""
    showrunner: str = ""
    unit: str = ""
    work: str = ""
    # The process the lease names; 0 names the allocating process.
    owner_pid: int = 0
    copy_checkout: CopyCheckout | NoCopyCheckout = NoCopyCheckout()
    machine: str = ""


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read_or_repository("build_folders")


class InvalidFolderForm(ValueError):
    """A value cannot identify a folder in the build layout."""


@final
@dataclass(frozen=True)
class WorktreeOutput:
    """The build output of a named worktree."""

    repo: str
    unit: str
    short_name: str


@final
@dataclass(frozen=True)
class BuildCopy:
    """A numbered test checkout and its build output."""

    repo: str
    unit: str
    number: int


@final
@dataclass(frozen=True)
class MainOutput:
    """The build output of a main checkout."""

    repo: str


@final
@dataclass(frozen=True)
class CiOutput:
    """The folder of a numbered CI runner."""

    repo: str
    number: int


FolderForm = WorktreeOutput | BuildCopy | MainOutput | CiOutput
BuildFolderRequest = UniqueBuildFolder | StableOwnerFolder | EditableRepositoryRequest | MainOutput


@final
@dataclass(frozen=True)
class OutsideFolderLayout:
    """The resolved path is outside the build root."""


@final
@dataclass(frozen=True)
class InvalidFolderComponents:
    """The path has no valid form in the build layout."""

    reason: str


@final
@dataclass(frozen=True)
class UnverifiedFolderPath:
    """The operating system could not verify the path."""

    error: OSError


ParsedFolder = FolderForm | OutsideFolderLayout | InvalidFolderComponents | UnverifiedFolderPath
_DECIMAL_NAME = re.compile(r"[0-9]+")
_METADATA = re.compile(r"(.+)\.(?:lock|lease\.json(?:\.[0-9a-f]{16}\.tmp)?)")
_HIDDEN_REMOVAL = re.compile(r"\.removing-[0-9]+-[0-9a-f]{16}")
_UNIT_LOCKS = ".unit-locks"
_CLAIM_LOCKS = ".build-claim-locks"
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 4
_unit_lock_claims: set[tuple[int, Path]] = set()
_unit_lock_claims_guard = Lock()
_shared_folder_claims: ContextVar[Mapping[Path, int]] = ContextVar("shared_folder_claims", default={})


def metadata_folder_name(name: str) -> str:
    """Return the folder name for a sidecar, or the unchanged name."""
    match = _METADATA.fullmatch(name)
    return match[1] if match else name


def _form_name(name: str) -> str:
    if not name:
        raise InvalidFolderForm("a build folder name must not be empty")
    if metadata_folder_name(name) != name or name in (_UNIT_LOCKS, _CLAIM_LOCKS):
        raise InvalidFolderForm(f"{name!r} is reserved for build folder metadata")
    try:
        _check_name(name)
    except ValueError as error:
        raise InvalidFolderForm(str(error)) from error
    return name


def _unit_name(unit: str) -> str:
    if unit in ("main", "ci"):
        raise InvalidFolderForm(f"{unit!r} is a reserved unit name")
    return _form_name(unit)


def _short_name(name: str) -> str:
    if _DECIMAL_NAME.fullmatch(name):
        raise InvalidFolderForm("a short name must contain a character other than a digit")
    return _form_name(name)


def _folder_number(number: int) -> str:
    if isinstance(number, bool) or number < 1:
        raise InvalidFolderForm("a folder number must be a positive integer")
    try:
        return f"{number:d}"
    except ValueError as error:
        raise InvalidFolderForm("the folder number is too large") from error


def _parse_number(name: str) -> int:
    if not _DECIMAL_NAME.fullmatch(name) or name.startswith("0"):
        raise InvalidFolderForm("a folder number must use decimal digits without a leading zero")
    return int(name)


def build_root(*, settings: ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    """Return the configured root for the local build folders."""
    if isinstance(settings, UseConfiguredValue):
        settings = _settings()
    return Path(settings.text("roots.build")).expanduser()


def repo_word(repository: Path) -> str:
    """Read the repository name from origin and apply its configured alias."""
    try:
        origin = subprocess.run(
            ["git", "-C", str(repository), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
            timeout=repository_git_timeout(),
        ).stdout.rstrip("\n")
        if "://" in origin:
            location = urlsplit(origin).path
        else:
            location = re.sub(r"^(?:[^/:]+@)?(?:\[[^\]]+\]|[^/:]+):", "", origin, count=1)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise InvalidFolderForm(f"{repository}: cannot read the origin URL") from error
    word = _form_name(location.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
    key = f"repo_word.{word}"
    return _form_name(_settings().values(key).get(key, word))


def worktree_path(repo: str, unit: str, short_name: str) -> Path:
    """Return the source path for a named worktree."""
    root = Path(_settings().text("roots.workspace")).expanduser()
    return root / _form_name(repo) / _unit_name(unit) / _short_name(short_name)


def output_path(repo: str, unit: str, short_name: str) -> Path:
    """Return the build output path for a named worktree."""
    return unit_folder(repo, unit) / _short_name(short_name)


def main_output_path(repo: str) -> Path:
    """Return the build output path for the main checkout."""
    return build_root() / _form_name(repo) / "main"


def _ci_folder(repo: str) -> Path:
    return build_root() / _form_name(repo) / "ci"


def ci_path(repo: str, number: int) -> Path:
    """Return the path for a numbered CI runner."""
    return _ci_folder(repo) / _folder_number(number)


def ci_folders(repo: str) -> list[tuple[int, Path]]:
    """List existing CI runner folders in runner number order."""
    parent = _ci_folder(repo)
    try:
        numbers = copy_numbers(parent)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [(number, parent / _folder_number(number)) for number in sorted(numbers) if numbers[number]]


def copy_path(repo: str, unit: str, number: int) -> Path:
    """Return the path for a numbered test copy."""
    return unit_folder(repo, unit) / _folder_number(number)


def unit_folder(repo: str, unit: str, *, root: Path | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    """Return the parent of the unit's build outputs and copies."""
    base = root if isinstance(root, Path) else build_root()
    return base / _form_name(repo) / _unit_name(unit)


@contextmanager
def unit_allocation_lock(folder: Path, *, exclusive: bool, root: Path | None = None,
                         create: bool = True, require_existing: bool = False) -> Generator[None]:
    """Reserve the unit name after its output folder moves or is removed."""
    root = build_root() if root is None else root
    lock = root / _UNIT_LOCKS / folder.relative_to(root)
    claim = (os.getpid(), root.resolve() / lock.relative_to(root))
    blocked = f"{folder}: the unit allocation lock is held. Rerun when idle"
    with _unit_lock_claims_guard:
        if claim in _unit_lock_claims:
            raise BlockingIOError(blocked)
        _unit_lock_claims.add(claim)
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        with build_folder_lock.hold_named_lock(lock, root, mode=mode, create=create, allocation_check=allocation_parent_current,
                busy=BlockingIOError(blocked),
                changed=ValueError(f"{folder}: the unit lock changed. Rerun when idle")) as descriptor:
            if descriptor is None and require_existing:
                raise FileNotFoundError(f"{folder}: no unit allocation lock")
            yield
    finally:
        with _unit_lock_claims_guard:
            _unit_lock_claims.remove(claim)


def _resolve_form_path(path: Path) -> Path:
    """Resolve links and permit paths for folders that do not exist yet."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve()


class BuildFolderClaimHeld(BlockingIOError):
    """A build folder claim prevents access."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path: Path = path


def _parent_lease_lock(path: Path, root: Path, *, publish: bool, create: bool) -> AbstractContextManager[int | None]:
    # A parent lease can change whether a nested temporary folder is a claim candidate.
    return build_folder_lock.hold_named_lock(root / _CLAIM_LOCKS / path.name / ".nested-claims", root,
        mode=fcntl.LOCK_EX if publish else fcntl.LOCK_SH, create=create,
        busy=BuildFolderClaimHeld(path, "the nested build folder claim lock is held"),
        changed=BuildFolderClaimHeld(path, "the nested build folder claim lock changed"),
        timing=_probe_timing(wait=True) if publish else build_folder_lock.NO_WAIT)


@contextmanager
def build_folder_claim_lock(path: Path, root: Path, *, exclusive: bool, create: bool = True) -> Generator[int | None]:
    """Lock the name outside the folder so removal cannot release the lock."""
    lock = root / _CLAIM_LOCKS / path.relative_to(root)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with ExitStack() as locks:
        if (exclusive and path.parent.parent == root and is_temporary_build_folder(path, require_lease=False)
                and is_temporary_build_folder(path.parent, require_lease=False)):
            _ = locks.enter_context(_parent_lease_lock(path.parent, root, publish=False, create=create))
        yield locks.enter_context(build_folder_lock.hold_named_lock(lock, root, mode=mode, create=create,
            busy=BuildFolderClaimHeld(path, "the build folder claim lock is held"),
            changed=BuildFolderClaimHeld(path, "the build folder claim lock changed")))


def _claim_locations(path: Path, root: Path) -> set[Path]:
    return {location for location in (Path(os.path.abspath(path)), path.resolve())
            if location.is_relative_to(root) and location != root}


@contextmanager
def build_folder_use(*paths: Path) -> Generator[tuple[int, ...]]:
    """Take shared claim locks for numbered copies and temporary folders that contain the paths."""
    root = build_root().resolve()
    folders: set[Path] = set()
    for path in paths:
        for location in _claim_locations(path, root):
            parts = location.relative_to(root).parts
            for depth in (1, 2):
                if len(parts) >= depth and _TEMPORARY_BUILD_NAME.fullmatch(parts[depth - 1]):
                    folders.add(root.joinpath(*parts[:depth]))
            if len(parts) >= 3:
                numbered = root.joinpath(*parts[:3])
                if isinstance(parse_resolved_form(numbered, root), BuildCopy):
                    folders.add(numbered)
    with _shared_claims(folders, root) as claims:
        yield claims


@contextmanager
def _shared_claims(folders: set[Path], root: Path) -> Generator[tuple[int, ...]]:
    if not folders:
        yield ()
        return
    with ExitStack() as stack:
        claims = dict(_shared_folder_claims.get())
        for folder in sorted(folders.difference(claims)):
            descriptor = stack.enter_context(build_folder_claim_lock(folder, root, exclusive=False))
            assert descriptor is not None
            claims[folder] = descriptor
        token = _shared_folder_claims.set(claims)
        try:
            yield tuple(claims[folder] for folder in sorted(folders))
        finally:
            _shared_folder_claims.reset(token)


@contextmanager
def build_folder_creation(path: Path, *, publish_lease: bool = False) -> Generator[tuple[int, ...]]:
    """Take shared claim locks before creating a build folder or any parent."""
    root = build_root().resolve()
    locations = _claim_locations(path, root)
    folders = {folder for location in locations for folder in (location, *location.parents)
               if folder.is_relative_to(root) and folder != root}
    with _shared_claims(folders, root) as claims, ExitStack() as locks:
        if publish_lease:
            for location in sorted(locations):
                if location.parent == root and is_temporary_build_folder(location, require_lease=False):
                    _ = locks.enter_context(_parent_lease_lock(location, root, publish=True, create=True))
        yield claims


def parse_form(path: Path) -> ParsedFolder:
    """Read the form or identify why the path has no verified form."""
    if metadata_folder_name(path.name) != path.name:
        return InvalidFolderComponents("the path names build folder metadata")
    root = build_root()
    try:
        resolved = _resolve_form_path(path)
        base = _resolve_form_path(root)
    except OSError as error:
        return UnverifiedFolderPath(error)
    except RuntimeError as error:
        return UnverifiedFolderPath(OSError(errno.ELOOP, str(error), str(path)))
    return parse_resolved_form(resolved, base)


def parse_resolved_form(path: Path, build_root: Path) -> ParsedFolder:
    """Read the folder form from a resolved path and build root."""
    if not path.is_relative_to(build_root):
        return OutsideFolderLayout()
    try:
        match path.relative_to(build_root).parts:
            case (repo, "main"):
                return MainOutput(_form_name(repo))
            case (repo, "ci", number):
                return CiOutput(_form_name(repo), _parse_number(number))
            case (repo, unit, name):
                _ = _form_name(repo)
                _ = _unit_name(unit)
                if _DECIMAL_NAME.fullmatch(name):
                    return BuildCopy(repo, unit, _parse_number(name))
                return WorktreeOutput(repo, unit, _short_name(name))
            case _:
                return InvalidFolderComponents("the path has the wrong number of components")
    except ValueError as error:
        return InvalidFolderComponents(str(error))


def test_runs_parent(checkout: Path) -> Path:
    """Select a test parent from the checkout output or the caller's private temporary folder."""
    target = checkout / "target"
    output = None
    if isinstance(parse_form(checkout), BuildCopy):
        if not target.is_symlink():
            output = target
    elif target.is_symlink():
        with suppress(OSError, RuntimeError):
            linked_output = target.resolve(strict=True)
            if linked_output.is_dir() and isinstance(parse_form(linked_output), (WorktreeOutput, MainOutput)):
                output = linked_output
    if output is not None:
        parent = output / "test-runs"
        if parent.is_symlink():
            raise InvalidFolderForm(f"{parent}: a model test folder must not be a link")
        return parent

    from scripts.lib import session_folder, unit_runs

    inherited = inherited_temporary_parent().resolve()
    parent = inherited.parent if str(inherited) == os.environ.get(TEST_RUN_FOLDER_ENV) else inherited / "test-runs"
    build = build_root().resolve()
    protected = (
        build / "test-runs",
        session_folder.configured_root().resolve(),
        session_folder.former_root().resolve(),
        unit_runs.RUN_ROOT.resolve(),
        unit_runs.FORMER_RUN_ROOT.resolve(),
    )
    resolved = parent.resolve()
    if inherited == build or any(folder.is_relative_to(root) for root in protected for folder in (inherited, resolved)):
        raise InvalidFolderForm(f"{checkout}: test runs from {inherited} require an assigned build output")
    return parent


def repository_git_timeout() -> float:
    """Keep the copies Git deadline when the maker creates a numbered copy."""
    from scripts.lib import conf

    return conf.read_or_repository("copies").number("git_timeout_seconds")


def repository_key_for(name: str, identity: str) -> str:
    """Append the configured identity hash to a readable repository name."""
    from hashlib import sha256

    return f"{name}-{sha256(os.fsencode(identity)).hexdigest()[:_settings().whole('names.repository_key_length')]}"


def repository_key(repository: Path) -> str:
    """Distinguish repositories with equal basenames using their canonical paths."""
    canonical = repository.resolve()
    return repository_key_for(canonical.name, str(canonical))


def _new_suffix(length: int, settings: ConfigRead) -> str:
    if length <= 0:
        raise ValueError("a unique build folder needs a suffix")
    characters = settings.text("names.suffix_characters")
    if not characters or any(not (char.isascii() and (char.isalnum() or char == "_")) for char in characters):
        raise ValueError("suffix characters must be ASCII letters, digits, or underscores")
    return "".join(secrets.choice(characters) for _ in range(length))


def build_folder_path(spec: BuildFolderSpec) -> Path:
    """Apply the configured name rule without creating a folder."""
    if spec.kind == "main":
        return spec.parent / _form_name(spec.repository) / "main"
    settings = _settings()
    if spec.kind == "copy":
        return copy_path(spec.repository, spec.unit, spec.number)
    for component in (spec.repository, spec.owner, spec.purpose, spec.suffix, spec.showrunner, spec.unit, spec.work):
        _check_name(component)
    if spec.suffix_length < 0:
        raise ValueError("a suffix length cannot be negative")
    suffix = spec.suffix
    named = bool(spec.showrunner or spec.unit or spec.work)
    if named and not (spec.showrunner and spec.unit and spec.purpose):
        raise ValueError("a named build folder needs showrunner, unit, and purpose")
    if not suffix and ((named and not spec.work) or
                       (not named and spec.kind in ("temporary", "gate", "scratch", "legacy-unique"))):
        suffix = _new_suffix(spec.suffix_length, settings)
    if named:
        pattern = "{showrunner}-{unit}/{work}-{purpose}"
    elif spec.kind == "editable":
        pattern = settings.text("patterns.editable")
    elif spec.kind == "mirror":
        pattern = "{repository}/{owner}-s{number}"
    else:
        pattern = settings.text(f"patterns.{spec.kind}")
    for field, value in (("repository", spec.repository), ("owner", spec.owner), ("purpose", spec.purpose)):
        if "{" + field + "}" in pattern and not value and spec.kind != "legacy-unique":
            raise ValueError(f"{spec.kind} needs {field}")
    relative = pattern.format(
        repository=spec.repository, owner=spec.owner, purpose=spec.purpose, suffix=suffix, number=spec.number,
        showrunner=spec.showrunner, unit=spec.unit, work=spec.work or suffix)
    if not relative or Path(relative).is_absolute() or any(part in (".", "..") for part in relative.split("/")):
        raise ValueError("a build folder pattern must stay below its parent")
    path = spec.parent / relative
    if not path.is_relative_to(spec.parent) or path == spec.parent:
        raise ValueError("a build folder must stay below its parent")
    return path


_locks: dict[int, Path] = {}
_lock_owners: dict[int, Thread] = {}
_inherited_build_locks: ContextVar[tuple[int, ...]] = ContextVar('inherited_build_locks', default=())

def _probe_timing(*, wait: bool = True, settings: ConfigRead | None = None) -> build_folder_lock.ProbeTiming:
    settings = _settings() if settings is None else settings
    return build_folder_lock.ProbeTiming(settings.number("locks.probe_wait_seconds") if wait else 0.0,
                                        settings.number("locks.probe_retry_seconds"))


def _take_lock(descriptor: int, mode: int, *, wait: bool = True) -> bool:
    return build_folder_lock.take_lock(descriptor, mode, _probe_timing(wait=wait))


COPY_KEEP_ENV = "COPIES_KEEP_FILE"


def copy_keep_file(path: Path) -> Path:
    """Return the path where the child can record a reason to keep the copy."""
    return path.with_name(path.name + ".keep")


def _copy_is_kept(path: Path, directory: int | None = None) -> bool:
    marker = copy_keep_file(path)
    try:
        info = os.stat(marker if directory is None else marker.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 0


def _kept_build_copy(path: Path, directory: int | None = None) -> bool:
    return (_DECIMAL_NAME.fullmatch(path.name) is not None and _copy_is_kept(path, directory)
            and isinstance(parse_form(path), BuildCopy))


BuildFolderState = Literal["held", "free", "kept"]


def build_folder_state(path: Path) -> BuildFolderState:
    """Check the lock and the numbered copy keep file."""
    state = build_folder_lock.probe(path, path.parent, _probe_timing)
    if state == "free" and _kept_build_copy(path):
        return "kept"
    return state


def build_folder_lock_fd(path: Path) -> int:
    """Return the held descriptor for subprocess pass_fds."""
    return next(descriptor for descriptor, candidate in reversed(_locks.items()) if candidate == path)


def held_build_folder_descriptors() -> tuple[int, ...]:
    """Pass inherited locks and locks that this thread holds to child processes."""
    owner = current_thread()
    owned = (descriptor for descriptor, thread in _lock_owners.copy().items() if thread is owner)
    return (*_inherited_build_locks.get(), *owned)


def _register_build_lock(descriptor: int, path: Path) -> None:
    _locks[descriptor] = path
    _lock_owners[descriptor] = current_thread()


def _forget_build_lock(descriptor: int) -> None:
    _ = _lock_owners.pop(descriptor, None)
    _ = _locks.pop(descriptor, None)


@contextmanager
def inherit_build_lock(descriptor: int) -> Generator[None]:
    """Pass an inherited build lock to child processes."""
    token = _inherited_build_locks.set((*_inherited_build_locks.get(), descriptor))
    try:
        yield
    finally:
        _inherited_build_locks.reset(token)


def _release_build_folder(path: Path, *, descriptor: int | None = None) -> None:
    if descriptor is None:
        try:
            descriptor = build_folder_lock_fd(path)
        except StopIteration:
            return
    if _locks.get(descriptor) != path:
        return
    try:
        _forget_build_lock(descriptor)
    finally:
        # Closing preserves a child's inherited hold on the same lock.
        os.close(descriptor)


COPY_USE_STAMP = ".lint-sweep-used"


def _mac_mount_points() -> tuple[Path, ...]:
    import ctypes

    @final
    class _MacMount(ctypes.Structure):
        """The 64-bit macOS statfs structure."""

        _fields_ = [
            ("block_size", ctypes.c_uint32),
            ("io_size", ctypes.c_int32),
            ("blocks", ctypes.c_uint64),
            ("blocks_free", ctypes.c_uint64),
            ("blocks_available", ctypes.c_uint64),
            ("files", ctypes.c_uint64),
            ("files_free", ctypes.c_uint64),
            ("filesystem_id", ctypes.c_int32 * 2),
            ("owner", ctypes.c_uint32),
            ("filesystem_type", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("subtype", ctypes.c_uint32),
            ("type_name", ctypes.c_char * 16),
            ("mount_point", ctypes.c_char * 1024),
            ("source", ctypes.c_char * 1024),
            ("reserved", ctypes.c_uint32 * 8),
        ]

    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    symbol = "getmntinfo_r_np$INODE64" if os.uname().machine == "x86_64" else "getmntinfo_r_np"
    try:
        read = system[symbol]
    except AttributeError as error:
        raise OSError("the macOS mount table reader is unavailable") from error
    mounts = ctypes.POINTER(_MacMount)()
    read.argtypes = [ctypes.POINTER(type(mounts)), ctypes.c_int]
    read.restype = ctypes.c_int
    count = cast(int, read(ctypes.byref(mounts), 2))  # MNT_NOWAIT
    if count <= 0:
        raise OSError(ctypes.get_errno(), "cannot read the macOS mount table")
    free = system["free"]
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    try:
        return tuple(Path(os.fsdecode(cast(bytes, mounts[index].mount_point))) for index in range(count))
    finally:
        _ = cast(None, free(mounts))


@dataclass(frozen=True)
class LinuxMount:
    mount_id: int
    device: bytes
    root: Path
    point: Path


def _decode_mount_path(value: bytes) -> Path:
    return Path(os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), value)))


def linux_mounts() -> tuple[LinuxMount, ...]:
    mounts: list[LinuxMount] = []
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        fields = line.split()
        if len(fields) < 10 or b"-" not in fields[6:]:
            raise ValueError("invalid Linux mount table entry")
        root, point = map(_decode_mount_path, fields[3:5])
        if not root.is_absolute() or not point.is_absolute():
            raise ValueError("invalid Linux mount path")
        mounts.append(LinuxMount(int(fields[0]), fields[2], root, point))
    return tuple(mounts)


def _linux_mount_points() -> tuple[Path, ...]:
    return tuple(mount.point for mount in linux_mounts())


def may_remove_build_path(path: Path) -> bool:
    """Keep the build root and repository folders owned by another user."""
    physical = path.resolve()
    root = build_root().resolve()
    if (root.is_relative_to(physical) or physical == Path("/build")
            or physical.is_relative_to(root / _UNIT_LOCKS) or physical.is_relative_to(root / _CLAIM_LOCKS)):
        return False
    if physical.parent in (root, Path("/build")):
        try:
            return physical.stat().st_uid == os.geteuid()
        except FileNotFoundError:
            return False
    return True


def may_remove_folder_tree(*paths: Path) -> bool:
    """Check that the mount table has no mount at or below a removal path."""
    try:
        if sys.platform == "darwin":
            mounts = _mac_mount_points()
        elif sys.platform == "linux":
            mounts = _linux_mount_points()
        else:
            return False
        if Path("/") not in mounts or any(not point.is_absolute() for point in mounts):
            return False
        for path in paths:
            physical = path.resolve()
            if path.is_mount() or any(point.is_relative_to(physical) for point in mounts):
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def mark_folder_use(path: Path) -> None:
    """Update the use stamp while the caller holds the folder lock."""
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(COPY_USE_STAMP, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"{path}: invalid use stamp")
            os.utime(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def lease_path(path: Path) -> Path:
    """Return the lease path beside the copy."""
    return path.with_name(path.name + ".lease.json")


def is_temporary_build_folder(path: Path, *, require_lease: bool = True,
                              directory_entries: Container[str] | None = None) -> bool:
    """Check the temporary build name and, by default, its lease record."""
    if not _TEMPORARY_BUILD_NAME.fullmatch(path.name):
        return False
    if not require_lease:
        return True
    lease = lease_path(path)
    return lease.name in directory_entries if directory_entries is not None else os.path.lexists(lease)


def _temporary_claim_location(path: Path, root: Path | None = None) -> Path | None:
    if is_temporary_build_folder(path):
        canonical = path.parent.resolve() / path.name
        root = build_root().resolve() if root is None else root
        if root in (canonical.parent, canonical.parent.parent):
            return canonical
    return None


def _read_folder_lease(path: Path, directory: int, *, allow_empty: bool = False) -> dict[str, object]:
    sidecar = lease_path(path)
    descriptor = os.open(sidecar.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, encoding="utf-8") as record:
        info = os.fstat(record.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ValueError(f"{sidecar}: invalid lease file")
        if allow_empty and info.st_size == 0:
            return {}
        fields = cast(object, json.load(record))
    if not isinstance(fields, dict):
        raise ValueError(f"{sidecar}: invalid lease record")
    return cast(dict[str, object], fields)


def folder_lease_fields(path: Path) -> dict[str, object]:
    """Read lease fields for a status display."""
    with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
        return _read_folder_lease(path, directory)


def gate_folder_lease_fields(path: Path, root: Path) -> dict[str, object]:
    """Read a lease under the gate root without acquiring a removal lock."""
    with build_folder_lock.folder_parent(path, root, create=False) as directory:
        return _read_folder_lease(path, directory)


@dataclass(frozen=True)
class CopyDirectoryIdentity:
    """The device and inode of a copy directory or its Git directory."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, status: os.stat_result) -> CopyDirectoryIdentity:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError("expected a directory")
        return cls(status.st_dev, status.st_ino)


@dataclass(frozen=True)
class NumberedCopySource:
    """The source commit and directory identities for one numbered copy."""

    path: Path
    source: Path
    commit: str
    checkout_identity: CopyDirectoryIdentity
    git_directory: Path
    git_identity: CopyDirectoryIdentity
    common_directory: Path
    common_identity: CopyDirectoryIdentity


@dataclass(frozen=True)
class NumberedCopyRemovalPending:
    """The copy owner has started deleting this exact copy."""

    copy: NumberedCopySource


@dataclass(frozen=True)
class NoNumberedCopySource:
    """There is no numbered copy source record at this path."""


@dataclass(frozen=True)
class UnreadableNumberedCopySource:
    """The copy source record cannot establish the checkout's identity."""

    path: Path
    reason: str


NumberedSourceEvidence = NumberedCopySource | NumberedCopyRemovalPending | UnreadableNumberedCopySource
NumberedCopyRecord = NumberedSourceEvidence | NoNumberedCopySource


_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CARGO_REMOVAL = "cargo_removal"


def _record_numbered_copy_path(path: Path) -> BuildCopy:
    form = parse_form(path)
    if isinstance(form, UnverifiedFolderPath):
        raise form.error
    if not isinstance(form, BuildCopy):
        raise ValueError(f"{path}: expected a numbered copy")
    if path.absolute() != copy_path(form.repo, form.unit, form.number).absolute():
        raise ValueError(f"{path}: expected the numbered copy's configured path")
    return form


def _recorded_directory(fields: Mapping[str, object], key: str) -> CopyDirectoryIdentity:
    device, inode = fields.get(key + "_device"), fields.get(key + "_inode")
    if type(device) is not int or type(inode) is not int:
        raise ValueError(f"missing {key} identity")
    return CopyDirectoryIdentity(device, inode)


def _recorded_path(fields: dict[str, object], key: str) -> Path:
    value = fields.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"missing {key} path")
    return Path(value)


def _copy_git_checkout(path: Path, common: Path, common_identity: CopyDirectoryIdentity) -> git_files.Checkout:
    checkout = git_files.at(path)
    if (not stat.S_ISREG((path / ".git").lstat().st_mode)
            or checkout.common_dir != common
            or checkout.git_dir.parent != common / "worktrees"
            or common.is_relative_to(checkout.toplevel)
            or CopyDirectoryIdentity.from_stat(checkout.common_dir_status) != common_identity):
        raise ValueError("copy Git directory identity changed")
    pointer = checkout.git_dir / "gitdir"
    descriptor = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("copy Git back-pointer is not a regular file")
        target = stream.read().strip()
    if not target or (checkout.git_dir / target).resolve() != checkout.toplevel / ".git":
        raise ValueError("copy Git back-pointer names a different checkout")
    return checkout


def _copy_identity_fields(fields: dict[str, object], copy: NumberedCopySource) -> None:
    fields.update(copy_path=str(copy.path.absolute()), source_path=str(copy.source),
                  copy_device=copy.checkout_identity.device, copy_inode=copy.checkout_identity.inode,
                  git_directory=str(copy.git_directory), git_device=copy.git_identity.device,
                  git_inode=copy.git_identity.inode, common_directory=str(copy.common_directory),
                  common_device=copy.common_identity.device, common_inode=copy.common_identity.inode)


def _numbered_copy_record(path: Path, form: BuildCopy, directory: int) -> NumberedCopyRecord:
    try:
        fields = _read_folder_lease(path, directory)
    except FileNotFoundError:
        return NoNumberedCopySource()
    return _validate_numbered_copy_record(path, form, directory, fields)


def _validate_numbered_copy_record(path: Path, form: BuildCopy, directory: int,
                                   fields: dict[str, object]) -> NumberedCopyRecord:
    if "source_path" not in fields and "removing" not in fields:
        return NoNumberedCopySource()
    commit = fields.get("commit")
    if (fields.get("unit") != form.unit or fields.get("repository") != form.repo
            or not isinstance(commit, str) or not _COMMIT_ID.fullmatch(commit)
            or _recorded_path(fields, "copy_path") != path.absolute()):
        raise ValueError("copy source record has the wrong path or commit")
    source = _recorded_path(fields, "source_path")
    identity = _recorded_directory(fields, "copy")
    common = _recorded_path(fields, "common_directory")
    common_identity = _recorded_directory(fields, "common")
    removing = fields.get("removing") is True
    if removing:
        git_directory = _recorded_path(fields, "git_directory")
        if git_directory.parent != common / "worktrees" or common.is_relative_to(path.absolute()):
            raise ValueError("copy Git directory is outside the source repository")
        record = NumberedCopySource(path, source, commit, identity, git_directory,
                                    _recorded_directory(fields, "git"), common, common_identity)
        return NumberedCopyRemovalPending(record)
    if identity != CopyDirectoryIdentity.from_stat(os.stat(path.name, dir_fd=directory, follow_symlinks=False)):
        raise ValueError("copy directory identity changed")
    checkout = _copy_git_checkout(path, common, common_identity)
    if git_files.at(source).common_dir != common:
        raise ValueError("copy source changed")
    git_identity = CopyDirectoryIdentity.from_stat(checkout.git_dir_status)
    if fields.get("ready") is not False or "git_directory" in fields:
        if (checkout.git_dir != _recorded_path(fields, "git_directory")
                or git_identity != _recorded_directory(fields, "git")):
            raise ValueError("copy Git directory identity changed")
    return NumberedCopySource(path, source, commit, identity, checkout.git_dir, git_identity, common, common_identity)


def numbered_copy_record(path: Path) -> NumberedCopyRecord:
    """Read the source or removal record for an exact numbered copy path."""
    try:
        form = _record_numbered_copy_path(path)
        with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
            return _numbered_copy_record(path, form, directory)
    except FileNotFoundError as error:
        # The lease can remain after an interruption removes the checkout.
        if not os.path.lexists(lease_path(path)):
            return NoNumberedCopySource()
        return UnreadableNumberedCopySource(path, str(error))
    except (OSError, ValueError, RuntimeError) as error:
        return UnreadableNumberedCopySource(path, str(error))


class NumberedCopyEnvrc(Enum):
    """The numbered copy requires byte proof for its envrc."""

    OUTSIDE_COPY = auto()
    TRUSTED = auto()
    NEEDS_APPROVAL = auto()


def numbered_copy_envrc(envrc: Path, *, trusted_source: Callable[[Path], bool]) -> NumberedCopyEnvrc:
    """Compare an envrc with the recorded commit in a trusted source checkout."""
    try:
        root = build_root()
        candidate = Path(os.path.abspath(envrc))
        physical_root = root.resolve()
        for directory, base in ((candidate.parent, root.absolute()),
                                (candidate.parent.resolve(), physical_root)):
            if not directory.is_relative_to(base):
                continue
            parts = directory.relative_to(base).parts
            if len(parts) >= 3 and parts[1] not in ("main", "ci") and re.fullmatch(r"[0-9]+", parts[2]):
                parent = root.joinpath(*parts[:3])
                break
        else:
            return NumberedCopyEnvrc.OUTSIDE_COPY
        exact = physical_root / parent.relative_to(root)
        if (candidate.name != ".envrc" or candidate.parent.resolve() != exact
                or len(parts) != 3):
            return NumberedCopyEnvrc.NEEDS_APPROVAL
        record = numbered_copy_record(parent)
        if not isinstance(record, NumberedCopySource) or not trusted_source(record.source):
            return NumberedCopyEnvrc.NEEDS_APPROVAL
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            identity = os.fstat(stream.fileno())
            if not stat.S_ISREG(identity.st_mode):
                return NumberedCopyEnvrc.NEEDS_APPROVAL
            command = ["git", "--git-dir", str(record.common_directory)]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            timeout = repository_git_timeout()
            entry = subprocess.run([*command, "ls-tree", record.commit, "--", ".envrc"],
                cwd=record.source, env=environment, capture_output=True, check=True, timeout=timeout)
            if not entry.stdout.startswith((b"100644 blob ", b"100755 blob ")):
                return NumberedCopyEnvrc.NEEDS_APPROVAL
            expected = subprocess.run(
                [*command, "cat-file", "blob", f"{record.commit}:.envrc"],
                cwd=record.source, env=environment, capture_output=True, check=True, timeout=timeout)
            if stream.read(len(expected.stdout) + 1) != expected.stdout:
                return NumberedCopyEnvrc.NEEDS_APPROVAL
            current = candidate.lstat()
            if (not os.path.samestat(identity, current) or identity.st_ctime_ns != current.st_ctime_ns
                    or identity.st_mtime_ns != current.st_mtime_ns
                    or record != numbered_copy_record(parent)):
                return NumberedCopyEnvrc.NEEDS_APPROVAL
        return NumberedCopyEnvrc.TRUSTED
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return NumberedCopyEnvrc.NEEDS_APPROVAL


def _copy_creation_fields(path: Path, info: os.stat_result, source: Path, fields: dict[str, object]) -> None:
    repository = git_files.at(source)
    fields.update(copy_path=str(path.absolute()), source_path=str(source),
                  copy_device=info.st_dev, copy_inode=info.st_ino,
                  common_directory=str(repository.common_dir),
                  common_device=repository.common_dir_status.st_dev, common_inode=repository.common_dir_status.st_ino)


def _prepare_copy_reuse(path: Path, directory: int, spec: BuildFolderSpec, fields: dict[str, object]) -> bool:
    form = BuildCopy(spec.repository, spec.unit, spec.number)
    if _copy_is_kept(path, directory):
        raise FileExistsError(f"{path}: numbered copy is kept")
    assert isinstance(spec.copy_checkout, CopyCheckout)
    try:
        lease = _read_folder_lease(path, directory)
        record = _validate_numbered_copy_record(path, form, directory, lease)
        if (not isinstance(record, NumberedCopySource) or record.source != spec.copy_checkout.source
                or not may_remove_folder_tree(path, record.git_directory)):
            raise ValueError("copy has no matching source record")
        _recover_copy_cargo(path, directory, lease)
        _copy_identity_fields(fields, record)
        fields["commit"] = record.commit
        return True
    except git_files.NotACheckout as error:
        try:
            lease = _read_folder_lease(path, directory)
            repository = git_files.at(spec.copy_checkout.source)
            if (lease.get("ready") is False and "git_directory" not in lease
                    and _recorded_path(lease, "source_path") == spec.copy_checkout.source
                    and _recorded_path(lease, "common_directory") == repository.common_dir
                    and _recorded_directory(lease, "common") == CopyDirectoryIdentity.from_stat(repository.common_dir_status)
                    and not any(path.iterdir())
                    and may_remove_folder_tree(path)):
                return False
        except (OSError, ValueError, RuntimeError) as unreadable:
            raise FileExistsError(f"{path}: incomplete copy cannot be verified") from unreadable
        raise FileExistsError(f"{path}: incomplete copy retained") from error
    except (OSError, ValueError, RuntimeError) as error:
        raise FileExistsError(f"{path}: copy identity could not be verified") from error


def _current_copy_lock(path: Path, directory: int) -> None:
    try:
        descriptor = build_folder_lock_fd(path)
    except StopIteration:
        raise ValueError(f"{path}: the numbered copy lock is required") from None
    if not build_folder_lock.lock_current(path, directory, descriptor):
        raise ValueError(f"{path}: the numbered copy lock changed")


def _recover_copy_cargo(path: Path, directory: int, fields: Mapping[str, object]) -> None:
    if _CARGO_REMOVAL not in fields:
        return
    raw = fields[_CARGO_REMOVAL]
    if not isinstance(raw, dict) or (stage := StagedRemoval.read(cast(dict[str, object], raw))) is None:
        raise ValueError(f"{path}: invalid Cargo removal record")
    target = path / "target"
    copy_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        identity = CopyDirectoryIdentity.from_stat(os.fstat(copy_fd))
        if identity != _recorded_directory(fields, "copy") or not build_folder_lock.parent_current(target, copy_fd):
            raise ValueError(f"{target}: the numbered copy changed")
        if not stage.recover(target, dir_fd=copy_fd):
            raise FileExistsError(f"{target}: the staged Cargo output could not be removed")
        released = _release_removed_folder_at(target, copy_fd, reject_parent_links=False)
        if released not in (FolderPairRelease.RELEASED, FolderPairRelease.PAIR_ABSENT, FolderPairRelease.FOLDER_PRESENT):
            raise FileExistsError(f"{target}: the Cargo removal lock could not be released")
    finally:
        os.close(copy_fd)


def reset_numbered_copy_cargo(path: Path) -> bool:
    """Remove Cargo output and record its stage under the numbered copy lock."""
    root = build_root()
    with build_folder_lock.folder_parent(path, root, create=False) as directory:
        _current_copy_lock(path, directory)
        fields = _read_folder_lease(path, directory)
        identity = _recorded_directory(fields, "copy")
        target = path / "target"
        _recover_copy_cargo(path, directory, fields)

        def record_stage(tree: CheckedRemovalTree) -> None:
            if (CopyDirectoryIdentity.from_stat(os.fstat(tree.parent)) != identity
                    or not build_folder_lock.parent_current(target, tree.parent)):
                raise ValueError(f"{target}: the numbered copy changed")
            fields[_CARGO_REMOVAL] = StagedRemoval.from_tree(tree).fields()
            _publish_lease(path, directory, fields)

        if not remove_build_folder(target, wait=False, before_stage=record_stage):
            return False
        if _CARGO_REMOVAL in fields:
            del fields[_CARGO_REMOVAL]
            _publish_lease(path, directory, fields)
        return True


def record_numbered_copy_source(path: Path, source: Path, commit: str) -> None:
    """Publish the prepared commit while the maker holds the numbered copy lock."""
    form = _record_numbered_copy_path(path)
    with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
        _current_copy_lock(path, directory)
        record = _numbered_copy_record(path, form, directory)
        if not isinstance(record, NumberedCopySource) or record.source != source.resolve(strict=True):
            raise ValueError(f"{path}: the prepared checkout does not match the lease")
        head = git_files.head(git_files.at(path))
        if not _COMMIT_ID.fullmatch(commit) or not isinstance(head, git_files.DetachedHead) or head.commit != commit:
            raise ValueError(f"{path}: the prepared checkout does not match the requested commit")
        fields = _read_folder_lease(path, directory)
        _copy_identity_fields(fields, record)
        fields.update(commit=commit, ready=True)
        _publish_lease(path, directory, fields)

def mark_numbered_copy_removal(copy: NumberedCopySource) -> None:
    """Record removal intent while the remover holds the numbered copy lock."""
    path = copy.path
    form = _record_numbered_copy_path(path)
    with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
        _current_copy_lock(path, directory)
        if _numbered_copy_record(path, form, directory) != copy:
            raise ValueError(f"{path}: the numbered copy source changed")
        fields = _read_folder_lease(path, directory)
        _copy_identity_fields(fields, copy)
        fields["removing"] = True
        _publish_lease(path, directory, fields)


NUMBERED_COPY_LOCK_ENV = "NUMBERED_COPY_LOCK_FD"


@dataclass(frozen=True)
class NumberedCopyUseLease:
    """The reader holds the copy and can pass its descriptor to a child."""

    source: NumberedCopySource
    pass_fds: tuple[int, ...]

    @property
    def environment(self) -> Mapping[str, str]:
        return {NUMBERED_COPY_LOCK_ENV: str(self.pass_fds[0])}


@dataclass(frozen=True)
class NumberedCopyUseRefused:
    """The reader cannot hold this numbered copy."""

    reason: str


@contextmanager
def numbered_copy_use_lease(path: Path) -> Generator[NumberedCopyUseLease | NumberedCopyUseRefused]:
    """Hold a free copy for reading or reuse the inherited owner lock."""
    with ExitStack() as stack:
        try:
            form = _record_numbered_copy_path(path)
            claims = stack.enter_context(build_folder_use(path))
            directory = stack.enter_context(build_folder_lock.folder_parent(path, build_root(), create=False))
            inherited = next((str(fd) for fd, held in _locks.items() if held == path),
                             os.environ.get(NUMBERED_COPY_LOCK_ENV, ""))
            descriptor = build_folder_lock.open_lock(path, directory, create=False)
            _ = stack.callback(os.close, descriptor)
            has_inherited_lock = False
            if inherited.isdecimal():
                with suppress(OSError, OverflowError):
                    if build_folder_lock.lock_current(path, directory, int(inherited)):
                        descriptor = os.dup(int(inherited))
                        _ = stack.callback(os.close, descriptor)
                        has_inherited_lock = True
            if not _take_lock(descriptor, fcntl.LOCK_EX, wait=False) or not build_folder_lock.lock_current(path, directory, descriptor):
                raise BlockingIOError(f"{path}: numbered copy is busy")
            record = _numbered_copy_record(path, form, directory)
            if (not isinstance(record, NumberedCopySource)
                    or (not has_inherited_lock and _copy_is_kept(path, directory))
                    or _read_folder_lease(path, directory).get("ready") is not True):
                raise ValueError(f"{path}: no available numbered copy source")
            result = NumberedCopyUseLease(record, (descriptor, *claims))
        except (OSError, ValueError, RuntimeError) as error:
            yield NumberedCopyUseRefused(str(error))
        else:
            yield result


@dataclass(frozen=True)
class KnownFolderLease:
    """A held copy has a complete owner record."""

    path: Path
    pid: int
    process_start: str
    start_time: datetime
    unit: str
    repository: str
    commit: str
    last_use: datetime


@dataclass(frozen=True)
class UnknownFolderLease:
    """A copy or its parent has an unknown lock state or owner."""

    path: Path
    reason: str


HeldFolderLease = KnownFolderLease | UnknownFolderLease


def _lease_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing lease time")
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("lease time has no time zone")
    return date


def _copy_owner_record(path: Path, form: BuildCopy, fields: dict[str, object], *, held: bool) -> KnownFolderLease:
    pid = fields.get("pid")
    identity = fields.get("process_start")
    commit = fields.get("commit")
    if (type(pid) is not int or pid <= 0 or not isinstance(identity, str) or not identity
            or not isinstance(commit, str) or not _COMMIT_ID.fullmatch(commit)
            or fields.get("unit") != form.unit or fields.get("repository") != form.repo
            or type(fields.get("released")) is not bool or (held and fields["released"] is True)):
        raise ValueError("incomplete owner record")
    return KnownFolderLease(path, pid, identity, _lease_time(fields.get("start_time")),
                            form.unit, form.repo, commit, _lease_time(fields.get("last_use")))


USER_REMOVAL_ROOTS = build_folder_lock.USER_REMOVAL_ROOTS
BUILD_ROOT_EXCEPTIONS = USER_REMOVAL_ROOTS | frozenset({
    _UNIT_LOCKS, _CLAIM_LOCKS, "sccache", "session", "lost+found", "old-cargo", "old-paths", "old-claude",
})


def excluded_build_root_child(name: str) -> bool:
    """Identify root entries that the copy inventories must skip."""
    return name in BUILD_ROOT_EXCEPTIONS or name.startswith("old-") or name in ("cargo", "paths", "hana-ci")


def copy_numbers(parent: Path) -> dict[int, bool]:
    """List copy numbers and whether each has a directory."""
    numbers: dict[int, bool] = {}
    with os.scandir(parent) as entries:
        for entry in entries:
            name = metadata_folder_name(entry.name)
            try:
                number = _parse_number(name)
            except ValueError:
                continue
            numbers[number] = numbers.get(number, False) or (name == entry.name and entry.is_dir(follow_symlinks=False))
    return numbers


def _copy_leases_in_unit(unit: Path, root: Path, timing: build_folder_lock.ProbeTiming) -> Iterator[HeldFolderLease]:
    try:
        numbers = copy_numbers(unit)
    except OSError as error:
        yield UnknownFolderLease(unit, str(error))
        return
    for number in sorted(numbers):
        path = unit / str(number)
        try:
            form = BuildCopy(unit.parent.name, unit.name, number)
            with build_folder_lock.folder_parent(path, root, create=False) as directory:
                descriptor = build_folder_lock.open_lock(path, directory, create=False)
                try:
                    free = build_folder_lock.take_lock(descriptor, fcntl.LOCK_EX, timing)
                    if not build_folder_lock.lock_current(path, directory, descriptor):
                        raise ValueError("lock changed during the read")
                    if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                        raise ValueError("copy is not a directory")
                    fields = _read_folder_lease(path, directory)
                    owner = _copy_owner_record(path, form, fields, held=not free)
                    if free and (fields.get("ready") is not True or not isinstance(
                            _validate_numbered_copy_record(path, form, directory, fields), NumberedCopySource)):
                        raise ValueError("copy has no prepared source record")
                finally:
                    os.close(descriptor)
            if not free:
                yield owner
        except (OSError, ValueError, RuntimeError) as error:
            yield UnknownFolderLease(path, str(error))


def copy_unit_folders(root: Path, *,
                      exclude_root_child: Callable[[str], bool] = BUILD_ROOT_EXCEPTIONS.__contains__
                      ) -> Iterator[Path | UnknownFolderLease]:
    """List unit directories or report the directory whose state is unreadable."""
    try:
        repositories = list(root.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        yield UnknownFolderLease(root, str(error))
        return
    for repo in repositories:
        if exclude_root_child(repo.name) or is_temporary_build_folder(repo):
            continue
        try:
            if not stat.S_ISDIR(repo.lstat().st_mode):
                continue
            units = list(repo.iterdir())
        except OSError as error:
            yield UnknownFolderLease(repo, str(error))
            continue
        for unit in units:
            if unit.name in ("ci", "main") or is_temporary_build_folder(unit):
                continue
            try:
                mode = unit.lstat().st_mode
            except OSError as error:
                yield UnknownFolderLease(unit, str(error))
                continue
            if stat.S_ISDIR(mode):
                yield unit


def held_folder_leases() -> Iterator[HeldFolderLease]:
    """List held copies and paths whose lease state is unknown."""
    settings = _settings()
    root = Path(settings.text("roots.build")).expanduser()
    timing = _probe_timing(wait=False, settings=settings)
    for unit in copy_unit_folders(root):
        if isinstance(unit, UnknownFolderLease):
            yield unit
        else:
            yield from _copy_leases_in_unit(unit, root, timing)


def copy_folders(root: Path) -> Iterator[tuple[Path, BuildCopy]]:
    """List existing copy directories for the disk reports."""
    for unit in copy_unit_folders(root):
        if isinstance(unit, UnknownFolderLease):
            continue
        try:
            numbers = copy_numbers(unit)
        except OSError:
            continue
        for number, exists in numbers.items():
            if exists:
                yield unit / str(number), BuildCopy(unit.parent.name, unit.name, number)


@dataclass(frozen=True)
class KeptCopy:
    path: Path
    reason: str
    kept_since: datetime


@dataclass(frozen=True)
class Clear:
    pass


@dataclass(frozen=True)
class NotClear:
    reason: str


@dataclass(frozen=True)
class CheckFailed:
    reason: str


KeptCopyCheck = Clear | NotClear | CheckFailed


class CheckoutProcessScan(Protocol):
    def check_checkout(self, checkout: Path, /) -> KeptCopyCheck: ...


@contextmanager
def _open_copy_directory(path: str, parent: int) -> Generator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _kept_copy_metadata(path: Path, info: os.stat_result, *, list_unreadable: bool) -> KeptCopy:
    reason = ""
    regular = stat.S_ISREG(info.st_mode)
    if not regular or info.st_nlink != 1:
        reason = f"{copy_keep_file(path)}: the keep file must be a regular file with one link"
        if not list_unreadable or not regular or info.st_nlink < 2:
            raise ValueError(reason)
    elif info.st_size == 0:
        raise ValueError(f"{copy_keep_file(path)}: the keep file is empty")
    return KeptCopy(path, reason, datetime.fromtimestamp(info.st_mtime, timezone.utc))


def _read_kept_copy(path: Path, directory: int, *, list_unreadable: bool = False) -> KeptCopy:
    marker = copy_keep_file(path)
    info = os.stat(marker.name, dir_fd=directory, follow_symlinks=False)
    row = _kept_copy_metadata(path, info, list_unreadable=list_unreadable)
    if row.reason:
        return row
    try:
        descriptor = os.open(marker.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        if not list_unreadable:
            raise
        info = os.stat(marker.name, dir_fd=directory, follow_symlinks=False)
        row = _kept_copy_metadata(path, info, list_unreadable=True)
        return replace(row, reason=row.reason or f"the keep file could not be read: {error}")
    with os.fdopen(descriptor, encoding="utf-8") as reason:
        row = _kept_copy_metadata(path, os.fstat(reason.fileno()), list_unreadable=list_unreadable)
        if row.reason:
            return row
        try:
            text = reason.read().strip()
        except (OSError, UnicodeDecodeError) as error:
            if not list_unreadable:
                raise
            text = f"the keep file could not be read: {error}"
        return replace(row, reason=text)


def _kept_copies_in_unit(unit: Path, directory: int) -> Iterator[KeptCopy]:
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".keep") or not entry.is_file(follow_symlinks=False):
                continue
            name = entry.name.removesuffix(".keep")
            with suppress(OSError, ValueError, OverflowError):
                path = unit / _folder_number(_parse_number(name))
                if stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                    yield _read_kept_copy(path, directory, list_unreadable=True)


def _kept_copy_directories(parent: Path, directory: int, *, repositories: bool = False) -> Iterator[str]:
    with os.scandir(directory) as entries:
        children = {entry.name: entry for entry in entries}
        for name, entry in children.items():
            if repositories and excluded_build_root_child(name):
                continue
            if is_temporary_build_folder(parent / name, directory_entries=children):
                continue
            if entry.is_dir(follow_symlinks=False):
                yield name


def kept_build_copies(root: Path) -> list[KeptCopy]:
    """List numbered copies with keep files under this build root."""
    root = Path(os.path.abspath(root))
    kept: list[KeptCopy] = []
    with suppress(OSError), build_folder_lock.open_folder_parent(root / "1", Path(root.anchor), create=False) as directory:
        for repo in _kept_copy_directories(root, directory, repositories=True):
            with suppress(OSError), _open_copy_directory(repo, directory) as repository:
                for name in _kept_copy_directories(root / repo, repository):
                    unit = root / repo / name
                    if not isinstance(parse_resolved_form(unit / "1", root), BuildCopy):
                        continue
                    with suppress(OSError), _open_copy_directory(name, repository) as unit_directory:
                        kept.extend(_kept_copies_in_unit(unit, unit_directory))
    return sorted(kept, key=lambda copy: copy.path)


def recheck_kept_copy(copy: KeptCopy, scan: CheckoutProcessScan) -> KeptCopyCheck:
    """Check a kept copy against the supplied scan while holding its folder lock."""
    path = copy.path
    try:
        with build_folder_lock.folder_parent(path, path.parents[2], create=False) as directory:
            descriptor = build_folder_lock.open_lock(path, directory, create=False)
            try:
                if not build_folder_lock.take_lock(descriptor, fcntl.LOCK_EX, build_folder_lock.NO_WAIT):
                    return CheckFailed("the folder lock is held")
                if not build_folder_lock.lock_current(path, directory, descriptor):
                    return CheckFailed("the folder lock file changed")
                folder = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(folder.st_mode) or _read_kept_copy(path, directory) != copy:
                    return CheckFailed("the kept copy changed")
                if not build_folder_lock.parent_current(path, directory):
                    return CheckFailed("the kept copy parent changed")
                return scan.check_checkout(path)
            finally:
                os.close(descriptor)
    except (OSError, ValueError, RuntimeError, OverflowError, IndexError) as error:
        return CheckFailed(str(error))


def numbered_copy_records() -> Iterator[NumberedSourceEvidence]:
    """List numbered copy records, including interrupted removals and unreadable entries."""
    for unit in copy_unit_folders(build_root()):
        if isinstance(unit, UnknownFolderLease):
            yield UnreadableNumberedCopySource(unit.path, unit.reason)
            continue
        try:
            numbers = copy_numbers(unit)
        except OSError as error:
            yield UnreadableNumberedCopySource(unit, str(error))
            continue
        for number in sorted(numbers):
            path = unit / str(number)
            record = numbered_copy_record(path)
            if not isinstance(record, NoNumberedCopySource):
                yield record
            elif numbers[number] and any(os.path.lexists(entry) for entry in (
                    lease_path(path), path.with_suffix(".lock"), path / ".git")):
                yield UnreadableNumberedCopySource(path, "numbered copy has no source record")


def _write_lease(path: Path, directory: int, spec: BuildFolderSpec, *, released: bool = False,
                 ready: bool = True, copy_fields: Iterable[tuple[str, object]] = ()) -> None:
    now = datetime.now(timezone.utc).isoformat()
    record: dict[str, object] = {"showrunner": spec.showrunner, "unit": spec.unit or spec.owner,
              "work": spec.work, "purpose": spec.purpose, "pid": spec.owner_pid or os.getpid(),
              "start_time": now}
    if spec.kind == "copy" and isinstance(spec.copy_checkout, CopyCheckout):
        if released:
            record = _read_folder_lease(path, directory)
        else:
            started = process_start.process_start(spec.owner_pid or os.getpid())
            if isinstance(started, (process_start.Started, process_start.Zombie)):
                record["process_start"] = started.start
        record["released"] = released and not _copy_is_kept(path, directory)
        record.update(machine=spec.machine, repository=spec.repository, last_use=now)
        if not released:
            record.update(commit=spec.copy_checkout.commit, ready=ready)
            record.update(copy_fields)
    _publish_lease(path, directory, record)


def _publish_lease(path: Path, directory: int, record: Mapping[str, object]) -> None:
    name = path.name + ".lease.json"
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise OSError(errno.ELOOP, "a build folder lease cannot be a symbolic link", name)
        # A concurrent os.replace can unlink the lease after this lookup, so no link is not a fault.
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink > 1:
            raise ValueError("a build folder lease must be a regular file with one link")
    text = json.dumps(record) + "\n"
    temporary = name + "." + secrets.token_hex(8) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "w") as output:
            _ = output.write(text)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class _TemporaryFolderRequest:
    """The temporary folder request retains its cleanup stack."""
    request: UniqueBuildFolder | BuildFolderSpec
    cleanup: ExitStack


def _request_spec(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest) -> BuildFolderSpec:
    if isinstance(request, _TemporaryFolderRequest):
        return _request_spec(request.request)
    if isinstance(request, BuildFolderSpec):
        return request
    if isinstance(request, MainOutput):
        return BuildFolderSpec(kind="main", parent=build_root(), repository=request.repo, suffix_length=0)
    if isinstance(request, (LinkedEditableRepository, RemoteEditableRepository)):
        path = request.path.expanduser().absolute()
        if isinstance(request, RemoteEditableRepository) and request.depth < 1:
            raise ValueError("an editable clone needs a positive depth")
        return BuildFolderSpec(kind="editable", parent=path.parent, purpose=path.name, suffix_length=0)
    else:
        stable = isinstance(request, StableOwnerFolder)
        name = request.name if isinstance(request, StableOwnerFolder) else request.prefix
        _check_name(name)
        if stable and not name:
            raise ValueError("a stable build folder needs an owner name")
        spec = BuildFolderSpec(kind="legacy-stable" if stable else "legacy-unique", parent=_parent(request.parent),
                               purpose=name, suffix_length=_settings().whole("names.suffix_length"))
        return spec


def _stable_folder(spec: BuildFolderSpec) -> bool:
    return spec.kind in ("standing", "editable", "main", "mirror", "target", "legacy-stable", "copy")


FolderCreationNotice = Callable[[Path, os.stat_result], None] | Literal["untracked"]


def create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest, *,
                        on_create: FolderCreationNotice = "untracked") -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    spec = _request_spec(original)
    with ExitStack() as source_locks:
        descriptors: tuple[int, ...] = ()
        if isinstance(original, LinkedEditableRepository):
            descriptors = source_locks.enter_context(linked_source_locks(original.source))
        return _create_build_folder(request, spec, descriptors, on_create)


def _create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest,
                         spec: BuildFolderSpec, source_descriptors: tuple[int, ...],
                         on_create: FolderCreationNotice) -> Path:
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    if isinstance(original, LinkedEditableRepository) and isinstance(original.branch, ExistingWorktreeBranch):

        branch = subprocess.run(["git", "-C", str(original.source), "show-ref", "--verify", "--quiet",
                                 f"refs/heads/{original.branch.name}"], capture_output=True,
                                 timeout=repository_git_timeout(), pass_fds=source_descriptors)
        if branch.returncode:
            raise ValueError(f"existing worktree branch {original.branch.name!r} does not exist in {original.source}")
    editable_repository = isinstance(original, (LinkedEditableRepository, RemoteEditableRepository))
    stable = _stable_folder(spec)
    # Only a name with a random part gets a new name on a clash; a tool name is fixed per request.
    renamed = not stable and not (spec.suffix or spec.work) and spec.kind != "tool"
    descriptor = -1
    try:
        with ExitStack() as allocation_scope, ExitStack() as folder_use:
            parents = request.cleanup if isinstance(request, _TemporaryFolderRequest) else allocation_scope
            directories: dict[Path, int] = {}
            retry_parent = False
            allocation = spec
            while True:
                if not retry_parent:
                    allocation = (replace(spec, work=_new_suffix(spec.suffix_length, _settings()))
                                  if spec.kind != "copy" and (spec.showrunner or spec.unit)
                                  and not spec.work else spec)
                path = (original.path.expanduser().absolute()
                        if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository))
                        else build_folder_path(allocation))
                _reject_staged_allocation(path)
                folder_use.close()
                claims = folder_use.enter_context(build_folder_creation(path, publish_lease=True))
                directory = directories.get(path.parent)
                if directory is None:
                    directory = parents.enter_context(build_folder_lock.open_folder_parent(path, spec.parent,
                        create=stable or not isinstance(original, UniqueBuildFolder)))
                    directories[path.parent] = directory
                try:
                    descriptor = build_folder_lock.open_lock(path, directory, create=True)
                except FileNotFoundError:
                    if not retry_parent and not build_folder_lock.parent_current(path, directory):
                        del directories[path.parent]
                        retry_parent = True
                        continue
                    if spec.kind != "copy" or retry_parent:
                        raise
                    del directories[path.parent]
                    continue
                made_inode: os.stat_result | None = None
                allocated: os.stat_result | None = None
                new_folder = False
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_SH if stable and spec.kind != "copy" and not editable_repository else fcntl.LOCK_EX,
                                      wait=spec.kind != "copy" and not isinstance(original, UniqueBuildFolder)):
                        if renamed:
                            continue
                        raise BlockingIOError(errno.EWOULDBLOCK, "build folder is held", str(path))
                    build_folder_lock.mark_lock(descriptor)
                    if not allocation_parent_current(path, directory):
                        if retry_parent:
                            raise FileNotFoundError(errno.ENOENT, "the build folder parent changed", str(path.parent))
                        del directories[path.parent]
                        retry_parent = True
                        continue
                    retry_parent = False
                    if not build_folder_lock.lock_current(path, directory, descriptor):
                        if spec.kind == "copy":
                            del directories[path.parent]
                        continue
                    try:
                        existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        if editable_repository:
                            if not (isinstance(original, LinkedEditableRepository) and original.allow_empty
                                    and _empty_editable_destination(path, directory, existing)):
                                raise FileExistsError(errno.EEXIST, "editable repository destination is occupied", str(path))
                        if not stable:
                            if not renamed:
                                raise FileExistsError(errno.EEXIST, "build folder already exists", str(path))
                            continue
                        if not stat.S_ISDIR(existing.st_mode):
                            raise FileExistsError(errno.EEXIST, "build folder is not a directory", str(path))
                    copy_fields: dict[str, object] = {}
                    checkout_ready = False
                    if spec.kind == "copy" and isinstance(spec.copy_checkout, CopyCheckout):
                        if existing is not None:
                            checkout_ready = _prepare_copy_reuse(path, directory, spec, copy_fields)
                        else:
                            try:
                                lease = _read_folder_lease(path, directory)
                            except FileNotFoundError:
                                lease = {}
                            except (OSError, ValueError) as error:
                                raise FileExistsError(f"{path}: copy lease cannot be read") from error
                            if "source_path" in lease or "removing" in lease or _copy_is_kept(path, directory):
                                raise FileExistsError(f"{path}: a recorded copy checkout is missing")
                        if existing is None or not checkout_ready:
                            try:
                                source_descriptors = allocation_scope.enter_context(linked_source_locks(spec.copy_checkout.source))
                            except BlockingIOError as error:
                                raise CopySourceBusy(errno.EWOULDBLOCK, f"copy source is busy: {error}",
                                                     str(spec.copy_checkout.source)) from error
                    new_folder = existing is None
                    if spec.kind != "copy":
                        _write_lease(path, directory, allocation)
                    if existing is None:
                        mode = original.mode if isinstance(original, StableOwnerFolder) else 0o777 if stable and spec.kind != "main" else 0o700
                        try:
                            os.mkdir(path.name, mode=mode, dir_fd=directory)
                            made_inode = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        except FileExistsError:
                            if not stable or editable_repository or spec.kind == "copy":
                                raise
                            if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                                raise
                    allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if isinstance(original, GateSuiteFolder):
                        if made_inode is None or not stat.S_ISDIR(allocated.st_mode) or not os.path.samestat(made_inode, allocated):
                            raise OSError(errno.ESTALE, "the gate suite folder changed during allocation", str(path))
                        original.owner.publish(path, directory, allocated)
                    if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository)):

                        if isinstance(original, LinkedEditableRepository):
                            command = ["git", "-C", str(original.source), "worktree", "add", str(path), original.branch.name]
                        else:
                            command = ["git", "clone", "--depth", str(original.depth), "--branch", original.ref,
                                       "--", original.url, str(path)]
                        try:
                            if isinstance(original, LinkedEditableRepository) and isinstance(original.branch, NewWorktreeBranch):
                                _ = subprocess.run(["git", "-C", str(original.source), "branch", "--",
                                                    original.branch.name, original.branch.start_point], check=True,
                                                    capture_output=True, text=True, timeout=repository_git_timeout(),
                                                    pass_fds=(*source_descriptors, *claims))
                            _ = subprocess.run(command, check=True, capture_output=True, text=True,
                                               timeout=repository_git_timeout(), pass_fds=(*source_descriptors, *claims))
                        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                            detail: str | bytes = (str(error) if isinstance(error, OSError)
                                                   else cast(str | bytes, error.stderr or str(error)))
                            if isinstance(detail, bytes):
                                detail = detail.decode(errors="replace")
                            raise OSError(errno.EIO, f"git repository creation failed: {detail.strip()}", str(path)) from error
                        if made_inode is not None and callable(on_create):
                            on_create(path, made_inode)
                        return path
                    if spec.kind == "copy" and isinstance(spec.copy_checkout, CopyCheckout):
                        if not checkout_ready:
                            _copy_creation_fields(path, allocated, spec.copy_checkout.source, copy_fields)
                        _write_lease(path, directory, allocation, ready=False, copy_fields=copy_fields.items())
                    if (spec.kind == "copy" and isinstance(spec.copy_checkout, CopyCheckout)
                            and (new_folder or not checkout_ready)):
                        _ = subprocess.run(["git", "-C", str(spec.copy_checkout.source), "worktree", "add", "--detach",
                                            str(path), spec.copy_checkout.commit], check=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            pass_fds=tuple(dict.fromkeys((descriptor, *held_build_folder_descriptors(),
                                                                         *source_descriptors, *claims))),
                                           timeout=repository_git_timeout())
                        form = BuildCopy(spec.repository, spec.unit, spec.number)
                        record = _numbered_copy_record(path, form, directory)
                        if not isinstance(record, NumberedCopySource):
                            raise ValueError(f"{path}: copy registration has no source record")
                        fields = _read_folder_lease(path, directory)
                        _copy_identity_fields(fields, record)
                        _publish_lease(path, directory, fields)
                    if made_inode is not None and callable(on_create):
                        on_create(path, made_inode)
                    os.set_inheritable(descriptor, True)
                    _register_build_lock(descriptor, path)
                    if isinstance(request, _TemporaryFolderRequest):
                        _ = request.cleanup.callback(_finish_temporary, path, directory, allocated,
                                                     descriptor=descriptor, temporary=not stable)
                    return path
                except BaseException:
                    _forget_build_lock(descriptor)
                    if isinstance(original, LinkedEditableRepository) or spec.kind == "copy":
                        if allocated is not None:
                            print(f"kept for a manual removal list: {path} (build_folders, checkout setup failed)",
                                  file=sys.stderr)
                    elif ((made_inode is not None or (new_folder and not stable))
                          and _take_lock(descriptor, fcntl.LOCK_EX)):
                        _remove_failed_allocation(path, directory, made_inode)
                    raise
                finally:
                    if descriptor not in _locks:
                        closing = descriptor
                        descriptor = -1
                        os.close(closing)
    except BaseException:
        if descriptor >= 0:
            _release_build_folder(_locks[descriptor], descriptor=descriptor)
        raise


def _empty_editable_destination(path: Path, parent: int, expected: os.stat_result) -> bool:
    if not stat.S_ISDIR(expected.st_mode):
        return False
    descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        current = os.fstat(descriptor)
        return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino) and not os.listdir(descriptor)
    finally:
        os.close(descriptor)


def _remove_failed_allocation(path: Path, directory: int, allocated: os.stat_result | None) -> None:
    if allocated is not None:
        _ = _remove_folder(path, directory, allocated, repair_access=True)
    if not _folder_exists(path, directory):
        _unlink_metadata(path, directory)


class RemovalTreeChanged(OSError):
    """The removal name no longer identifies the checked folder."""


def _same_removal_tree(path: Path, directory: int, expected: os.stat_result) -> bool:
    try:
        return os.path.samestat(expected, os.stat(path.name, dir_fd=directory, follow_symlinks=False))
    except FileNotFoundError:
        return False


def _delete_renamed_folder(path: Path, directory: int, expected: os.stat_result, *,
                           repair_access: bool, metadata_last: Collection[str] = ()) -> None:
    """Delete contents through the checked directory descriptor."""
    with ExitStack() as opened:
        descriptor = _open_removal_directory(path, directory, expected, opened, repair_access=repair_access)
        inode = os.fstat(descriptor)
        permissions = stat.S_IMODE(inode.st_mode)
        if repair_access and inode.st_uid == os.geteuid() and permissions & 0o300 != 0o300:
            _ = opened.callback(os.fchmod, descriptor, permissions)
            os.fchmod(descriptor, permissions | 0o300)
        entries = os.listdir(descriptor)
        if metadata_last:
            entries.sort(key=lambda name: name in metadata_last)
        for name in entries:
            try:
                child = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(child.st_mode):
                _delete_renamed_folder(path / name, descriptor, child, repair_access=repair_access)
            else:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=descriptor)
        if not _same_removal_tree(path, directory, expected):
            raise RemovalTreeChanged(f"{path}: the removal folder changed before rmdir")
        os.rmdir(path.name, dir_fd=directory)


def _rename_without_replacing(directory: int, source: str, destination: str) -> None:
    """Rename entries in one parent and refuse an occupied destination."""
    library = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "linux":
            rename = cast(Callable[[int, bytes, int, bytes, int], int], library.renameat2)
            status = rename(directory, os.fsencode(source), directory, os.fsencode(destination), _RENAME_NOREPLACE)
        elif sys.platform == "darwin":
            rename = cast(Callable[[int, bytes, int, bytes, int], int], library.renameatx_np)
            status = rename(directory, os.fsencode(source), directory, os.fsencode(destination), _RENAME_EXCL)
        else:
            raise AttributeError("exclusive rename")
    except AttributeError as error:
        raise OSError(errno.ENOTSUP, "exclusive rename is unavailable", source) from error
    if status != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), source, None, destination)


def _restore_removal(path: Path, hidden: Path, directory: int) -> None:
    try:
        _rename_without_replacing(directory, hidden.name, path.name)
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"kept for a manual removal list: {hidden} (build_folders, restore to {path} failed: {error})",
              file=sys.stderr)


@dataclass(frozen=True)
class CheckedRemovalTree:
    """Keep the parent descriptor and the checked identity for deletion."""

    path: Path
    parent: int
    info: os.stat_result

    def remove(self, *, repair_access: bool = False, metadata_last: Collection[str] = ()) -> None:
        _delete_renamed_folder(self.path, self.parent, self.info,
                               repair_access=repair_access, metadata_last=metadata_last)


RemovalTreeAction = Callable[[CheckedRemovalTree], None]


@dataclass(frozen=True)
class StagedRemoval:
    """Identify a removal stage independently of the source path."""

    name: str
    device: int
    inode: int

    @classmethod
    def from_tree(cls, tree: CheckedRemovalTree) -> StagedRemoval:
        return cls(tree.path.name, tree.info.st_dev, tree.info.st_ino)

    @classmethod
    def read(cls, fields: Mapping[str, object]) -> StagedRemoval | None:
        if "removal_stage" not in fields:
            return None
        name = fields.get("removal_stage")
        device, inode = fields.get("copy_device"), fields.get("copy_inode")
        if not isinstance(name, str) or type(device) is not int or type(inode) is not int:
            raise ValueError("invalid removal stage")
        return cls(name, device, inode)

    def fields(self) -> dict[str, object]:
        return {"removal_stage": self.name, "copy_device": self.device, "copy_inode": self.inode}

    def recover(self, source: Path, *, dir_fd: int | None = None) -> bool:
        return recover_staged_folder(source, self.name, self.device, self.inode, dir_fd=dir_fd)


def _staging_identity(info: os.stat_result) -> dict[str, object]:
    return {"device": info.st_dev, "inode": info.st_ino}


def _remove_folder(path: Path, directory: int, allocated: os.stat_result, *,
                   wait: bool = True, repair_access: bool = False,
                   child_locks: tuple[int, ...] = (), delete: RemovalTreeAction | None = None,
                   before_stage: RemovalTreeAction | None = None, review_temporary: bool = False) -> bool:
    """Rename the checked tree, check for new leases, and delete it."""
    if not stat.S_ISDIR(allocated.st_mode) or not may_remove_folder_tree(path):
        return False
    hidden = path.with_name(f".removing-{os.getpid()}-{secrets.token_hex(8)}")
    held = {(inode.st_dev, inode.st_ino) for inode in map(os.fstat, child_locks)}
    review_root = path.absolute() if review_temporary else None
    with ExitStack() as leases:
        try:
            refusal = _hold_child_leases(path, directory, leases, wait=wait, held=held,
                                         repair_access=repair_access, review_root=review_root)
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except (OSError, ValueError):
            return False
        if isinstance(refusal, RefusedRemovalLease) or not os.path.samestat(current, allocated):
            return False
        if not stat.S_ISDIR(current.st_mode) or not may_remove_build_path(path):
            return False
        stage = _hold_free_lease(hidden, directory, leases, wait=False, create=True, check_parent=False, mark=True)
        if not isinstance(stage, HeldFolderRemovalLease):
            return False
        moved = False
        removed = False
        keep_staging_record = False
        try:
            tree = CheckedRemovalTree(hidden, directory, allocated)
            _publish_lease(hidden, directory, _staging_identity(allocated))
            if before_stage is not None:
                before_stage(tree)
            try:
                _rename_without_replacing(directory, path.name, hidden.name)
            except (FileExistsError, FileNotFoundError):
                return False
            moved = True
            try:
                renamed = os.stat(hidden.name, dir_fd=directory, follow_symlinks=False)
                if not os.path.samestat(renamed, allocated) or not may_remove_folder_tree(hidden):
                    return False
                refusal = _hold_child_leases(
                    hidden, directory, leases, wait=False, held=held, repair_access=repair_access,
                    review_root=review_root)
            except (OSError, ValueError):
                return False
            if isinstance(refusal, RefusedRemovalLease):
                return False
            try:
                if delete is None:
                    tree.remove(repair_access=repair_access)
                else:
                    delete(tree)
            except OSError:
                keep_staging_record = True
                raise
            removed = not _folder_exists(hidden, directory)
            return removed
        finally:
            if moved and not removed and _same_removal_tree(hidden, directory, allocated):
                _restore_removal(path, hidden, directory)
                keep_staging_record = False
            if (not keep_staging_record and not _folder_exists(hidden, directory)
                    and build_folder_lock.lock_current(hidden, directory, stage.descriptor)):
                _unlink_metadata(hidden, directory)


def _folder_exists(path: Path, directory: int) -> bool:
    try:
        _ = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _unlink_metadata(path: Path, directory: int) -> None:
    temporary = re.compile(re.escape(path.name) + r"\.lease\.json\.[0-9a-f]{16}\.tmp")
    for name in os.listdir(directory):
        if temporary.fullmatch(name):
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
    for suffix in (".lease.json", ".lock"):
        try:
            os.unlink(path.name + suffix, dir_fd=directory)
        except FileNotFoundError:
            pass


def temporary_allocation_entries(folder: Path) -> tuple[Path, ...]:
    """List the folder and its lease files that are still present."""
    try:
        return tuple(sorted(entry for entry in folder.parent.iterdir() if entry == folder or (
            (metadata := _METADATA.fullmatch(entry.name)) is not None and metadata[1] == folder.name)))
    except FileNotFoundError:
        return ()


def _parent_has_symlink(parent: Path) -> bool:
    absolute = parent.absolute()
    # A link before '..' can lead to the same parent as the absolute path.
    return (Path(os.path.abspath(absolute)) != absolute.resolve()
            or any(component.is_symlink() for component in (absolute, *absolute.parents)))


@dataclass(frozen=True)
class RefusedRemovalLease:
    """A lock or keep file prevents removal."""


@dataclass(frozen=True)
class KeptCopyRemovalLease(RefusedRemovalLease):
    """A keep file prevents removal of the numbered copy."""


@dataclass(frozen=True)
class RemovalLockHeld(RefusedRemovalLease):
    lock: Path


@dataclass(frozen=True, kw_only=True)
class FreeFolderRemovalLease:
    """Hold any child locks found during the lease scan."""

    child_locks: tuple[int, ...] = field(default=(), repr=False, compare=False)

    def remove_tree(self, path: Path, expected: os.stat_result, *, wait: bool = True,
                    repair_access: bool = False, delete: RemovalTreeAction | None = None,
                    before_stage: RemovalTreeAction | None = None) -> bool:
        """Remove the same tree while retaining the child locks."""
        if isinstance(self, HeldFolderRemovalLease):
            if (self.folder != path or _locks.get(self.descriptor) != path
                    or not build_folder_lock.lock_current(path, self.directory, self.descriptor)
                    or not build_folder_lock.parent_current(path, self.directory)):
                return False
            return _remove_folder(path, self.directory, expected, wait=wait, repair_access=repair_access,
                                  child_locks=self.child_locks, delete=delete, before_stage=before_stage)
        return remove_build_folder(path, wait=wait, expected=expected, repair_access=repair_access,
                                   child_locks=self.child_locks, delete=delete, transient_lock=True,
                                   before_stage=before_stage)


@dataclass(frozen=True)
class UnreservedRemovalFolder(FreeFolderRemovalLease):
    """The folder has no lock file to hold."""


SidecarIdentity = tuple[int, int, int]


@dataclass(frozen=True)
class HeldFolderRemovalLease(FreeFolderRemovalLease):
    """The descriptor reserves the folder name until lock removal."""

    folder: Path
    directory: int
    descriptor: int
    created: bool = field(default=False, repr=False, compare=False, kw_only=True)

    def _sidecar_identity(self, name: str) -> SidecarIdentity | None:
        try:
            inode = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        except OSError:
            return None
        if stat.S_ISREG(inode.st_mode) and inode.st_nlink == 1:
            return inode.st_dev, inode.st_ino, inode.st_ctime_ns
        return None

    def sidecar_identities(self) -> dict[str, SidecarIdentity]:
        """Read the identities for this removal while the lock is held."""
        try:
            lock = os.fstat(self.descriptor)
        except OSError:
            return {}
        lock_name = self.folder.name + ".lock"
        lock_identity = (lock.st_dev, lock.st_ino, lock.st_ctime_ns)
        if self._sidecar_identity(lock_name) != lock_identity:
            return {}
        lease_name = self.folder.name + ".lease.json"
        lease_identity = self._sidecar_identity(lease_name)
        identities = {lease_name: lease_identity} if lease_identity is not None else {}
        identities[lock_name] = lock_identity
        return identities

    def owner_state(self, *, allow_released: bool = False) -> LeaseOwnerState:
        """Read the state of the recorded owner."""
        return _lease_owner_state(self.folder, self.directory, allow_released=allow_released)

    def _source_removed(self) -> bool:
        try:
            return (os.path.samestat(os.fstat(self.directory), self.folder.parent.stat())
                    and not _folder_exists(self.folder, self.directory))
        except OSError:
            return False

    def unlink_sidecars(self, expected: dict[str, SidecarIdentity]) -> None:
        """Remove the original sidecars after the source and marks are gone."""
        _ = self.unlink_matching_sidecars(expected)

    def unlink_matching_sidecars(self, expected: dict[str, SidecarIdentity], *,
                                 reject_parent_links: bool = False) -> set[str]:
        """Remove sidecars with matching identities and return their names."""
        removed: set[str] = set()
        lock_name = self.folder.name + ".lock"
        lock_identity = expected.get(lock_name)
        if lock_identity is None:
            return removed
        for name in (self.folder.name + ".lease.json", lock_name):
            identity = expected.get(name)
            with suppress(OSError):
                if (identity is not None and self._source_removed()
                        and (not reject_parent_links or not _parent_has_symlink(self.folder.parent))
                        and self._sidecar_identity(name) == identity
                        and (name == lock_name or self._sidecar_identity(lock_name) == lock_identity)):
                    os.unlink(name, dir_fd=self.directory)
                    removed.add(name)
        return removed


FolderRemovalLease = RefusedRemovalLease | UnreservedRemovalFolder | HeldFolderRemovalLease


class BuildFolderParentChanged(FileNotFoundError):
    """The parent path stopped naming the open directory."""


def _hold_free_lease(path: Path, directory: int, stack: ExitStack, *, wait: bool = True,
                     create: bool = False, check_parent: bool = True,
                     transient_lock: bool = False, mark: bool = False) -> FolderRemovalLease:
    """Hold the lock if it is free. The stack owns the descriptor."""
    if create and check_parent and mark:
        _reject_staged_allocation(path)
    created = False
    try:
        if transient_lock:
            try:
                descriptor = build_folder_lock.open_lock(path, directory, create=True, exclusive=True)
                created = True
            except FileExistsError:
                descriptor = build_folder_lock.open_lock(path, directory, create=False)
        else:
            descriptor = build_folder_lock.open_lock(path, directory, create=create)
    except FileNotFoundError:
        if create and check_parent and not build_folder_lock.parent_current(path, directory):
            raise BuildFolderParentChanged(str(path.parent)) from None
        if _kept_build_copy(path, directory):
            return KeptCopyRemovalLease()
        return UnreservedRemovalFolder()
    with ExitStack() as opened:
        _ = opened.callback(os.close, descriptor)
        acquired = _take_lock(descriptor, fcntl.LOCK_EX, wait=wait)
        if acquired and (mark or created):
            build_folder_lock.mark_lock(descriptor)
        if create and check_parent:
            parent_check = allocation_parent_current if mark else build_folder_lock.parent_current
            if not parent_check(path, directory):
                raise BuildFolderParentChanged(str(path.parent))
        if not build_folder_lock.lock_current(path, directory, descriptor):
            return RefusedRemovalLease()
        if not acquired:
            return RemovalLockHeld(path.with_name(path.name + ".lock"))
        lease = HeldFolderRemovalLease(path, directory, descriptor, created=created)
        if _kept_build_copy(path, directory):
            if created:
                _unlink_transient_lock(lease)
            return KeptCopyRemovalLease()
        _ = opened.callback(_forget_build_lock, descriptor)
        _register_build_lock(descriptor, path)
        _ = stack.enter_context(opened.pop_all())
    return lease


EXTERNAL_LINK_REASON = "the folder contains a symbolic link that can point outside the folder"
_LOOSE_OBJECT_HASH = re.compile(r"[0-9a-fA-F]+")


class TemporaryFolderReview(RuntimeError):
    """The temporary folder requires review before removal."""


def _link_can_leave_folder(root: Path, folder: Path, target: Path) -> bool:
    if target.is_absolute():
        if not target.is_relative_to(root):
            return True
        target = target.relative_to(root)
        folder = root
    depth = len(folder.relative_to(root).parts)
    entered = False
    for part in target.parts:
        if part == "..":
            # A preceding component can be a symbolic link.
            if entered or not depth:
                return True
            depth -= 1
        else:
            entered = True
    return False


def _check_removal_directory(path: Path, parent: int, directory: int, expected: os.stat_result) -> None:
    from scripts.lib.removal_guard import Refused, mount_id

    if not os.path.samestat(os.fstat(directory), expected):
        raise RemovalTreeChanged(f"{path}: the removal folder changed before open")
    try:
        same_mount = (expected.st_dev == os.fstat(parent).st_dev
                      and mount_id(directory) == mount_id(parent))
    except Refused as error:
        raise RemovalTreeChanged(f"{path}: cannot check the removal mount") from error
    if not same_mount:
        raise RemovalTreeChanged(f"{path}: the removal crosses a mount")


def _open_removal_directory(path: Path, parent: int, expected: os.stat_result, opened: ExitStack, *,
                            repair_access: bool) -> int:
    permissions = stat.S_IMODE(expected.st_mode)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if repair_access and expected.st_uid == os.geteuid() and permissions & 0o500 != 0o500:
        if sys.platform == "linux":
            # Check and repair the same directory, even if its name changes.
            handle = os.open(path.name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            _ = opened.callback(os.close, handle)
            _check_removal_directory(path, parent, handle, expected)
            entry = Path(f"/proc/self/fd/{handle}")
            _ = opened.callback(entry.chmod, permissions)
            entry.chmod(permissions | 0o500)
        else:
            if (expected.st_dev != os.fstat(parent).st_dev or not _same_removal_tree(path, parent, expected)
                    or not may_remove_folder_tree(path)):
                raise RemovalTreeChanged(f"{path}: cannot check the removal folder before access repair")

            def restore() -> None:
                if _same_removal_tree(path, parent, expected):
                    os.chmod(path.name, permissions, dir_fd=parent, follow_symlinks=False)

            _ = opened.callback(restore)
            os.chmod(path.name, permissions | 0o500, dir_fd=parent, follow_symlinks=False)
    directory = os.open(path.name, flags, dir_fd=parent)
    _ = opened.callback(os.close, directory)
    _check_removal_directory(path, parent, directory, expected)
    return directory


def hold_child_leases(path: Path, stack: ExitStack, *, wait: bool = True, check_owners: bool = False,
                      review_temporary: bool = False,
                      inspect: Callable[[Path, set[str], set[str]], None] | None = None) -> RefusedRemovalLease | None:
    """Check child leases and inspect a folder before a claim."""
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return _hold_child_leases(path, parent, stack, wait=wait, check_owners=check_owners,
                                  review_root=path.absolute() if review_temporary else None, inspect=inspect)
    finally:
        os.close(parent)


def _hold_child_leases(path: Path, parent: int, stack: ExitStack, *, wait: bool = True,
                       probe_fds: list[int] | None = None, held: set[tuple[int, int]] | None = None,
                       repair_access: bool = False, check_owners: bool = False,
                       review_root: Path | None = None,
                       inspect: Callable[[Path, set[str], set[str]], None] | None = None) -> RefusedRemovalLease | None:
    """Hold child leases through the open parent. Skip inodes already held."""
    scan_root = path

    def visit(path: Path, parent: int) -> RefusedRemovalLease | None:
        try:
            expected = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(expected.st_mode):
            return None
        with ExitStack() as opened:
            try:
                directory = _open_removal_directory(path, parent, expected, opened, repair_access=repair_access)
            except FileNotFoundError:
                return None
            folders: set[str] = set()
            files: set[str] = set()
            links: set[str] = set()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.endswith(".keep") and _kept_build_copy(
                            path / entry.name.removesuffix(".keep"), directory):
                        return KeptCopyRemovalLease()
                    if entry.is_dir(follow_symlinks=False):
                        folders.add(entry.name)
                    elif entry.is_symlink():
                        links.add(entry.name)
                    else:
                        files.add(entry.name)
            entries = files | links
            children = folders | links
            if review_root is not None:
                original = review_root / path.relative_to(scan_root)
                if ("pack" in folders or git_files.has_git_storage(original, folders, entries)
                        or any(len(name) in (38, 40, 62, 64) and _LOOSE_OBJECT_HASH.fullmatch(name) for name in files)):
                    raise TemporaryFolderReview(git_files.GIT_STORAGE_REASON)
                for name in links:
                    target = Path(os.readlink(name, dir_fd=directory))
                    if _link_can_leave_folder(review_root, original, target):
                        raise TemporaryFolderReview(EXTERNAL_LINK_REASON)
            if inspect is not None:
                inspect(path, children, entries)
            for name in entries:
                stem = name.removesuffix(".lock")
                if not stem or stem == name:
                    continue
                paired = stem in children or stem + ".lease.json" in entries
                try:
                    lock = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    # Older allocations wrote an empty lock beside a folder or lease record.
                    legacy = paired and lock.st_size == 0
                    if not (legacy or (check_owners and paired)
                            or build_folder_lock.is_marked_lock(name, directory, lock)):
                        continue
                except FileNotFoundError:
                    continue
                key = (lock.st_dev, lock.st_ino)
                if held is not None and key in held:
                    continue
                lease = _hold_free_lease(path / stem, directory, stack, wait=wait)
                if isinstance(lease, RefusedRemovalLease):
                    return lease
                if isinstance(lease, HeldFolderRemovalLease):
                    if held is not None:
                        locked = os.fstat(lease.descriptor)
                        held.add((locked.st_dev, locked.st_ino))
                    if probe_fds is not None:
                        probe_fds.append(lease.descriptor)
            if check_owners:
                for name in children:
                    child = path / name
                    if name + ".lease.json" in entries and _lease_owner_state(child, directory) is not LeaseOwnerState.ENDED:
                        return RemovalLockHeld(lease_path(child))
            for child in folders:
                refusal = visit(path / child, directory)
                if refusal is not None:
                    return refusal
        return None

    return visit(path, parent)


@contextmanager
def folder_removal_lease(path: Path, *, children: bool = False, wait: bool = True,
                         create: bool = False, root: Path | None = None,
                         probe_fds: list[int] | None = None,
                         transient_lock: bool = False) -> Generator[FolderRemovalLease]:
    """Hold the collector's locks and return the reservation for a folder."""
    metadata = _METADATA.fullmatch(path.name)
    folder = path.with_name(metadata[1]) if metadata else path
    with ExitStack() as stack:
        try:
            for attempt in range(2):
                directory = stack.enter_context(build_folder_lock.folder_parent(
                    folder, root or folder.parent, create=create and root is not None))
                try:
                    lease = _hold_free_lease(folder, directory, stack, wait=wait, create=create,
                                             mark=create and not transient_lock, transient_lock=transient_lock)
                except BuildFolderParentChanged:
                    if attempt == 1:
                        raise
                else:
                    break
            else:
                raise AssertionError("the lease attempt must resolve")
            if isinstance(lease, HeldFolderRemovalLease) and lease.created:
                _ = stack.callback(_unlink_transient_lock, lease)
            if probe_fds is not None:
                probe_fds.append(directory)
                if isinstance(lease, HeldFolderRemovalLease):
                    probe_fds.append(lease.descriptor)
            if not isinstance(lease, RefusedRemovalLease):
                if metadata is not None:
                    lease = UnreservedRemovalFolder()
                elif children and not build_folder_lock.parent_current(folder, directory):
                    lease = RefusedRemovalLease()
                elif children:
                    child_locks: list[int] = []
                    with suppress(FileNotFoundError):
                        refusal = _hold_child_leases(folder, directory, stack, wait=wait, probe_fds=child_locks)
                        lease = refusal if refusal is not None else replace(lease, child_locks=tuple(child_locks))
                    if probe_fds is not None:
                        probe_fds.extend(child_locks)
        except BuildFolderParentChanged:
            lease = RefusedRemovalLease()
        except FileNotFoundError:
            lease = UnreservedRemovalFolder()
        except (OSError, ValueError):
            lease = RefusedRemovalLease()
        yield lease


@contextmanager
def collector_lease(path: Path, *, children: bool = False, wait: bool = True) -> Generator[bool]:
    """Hold a managed folder's lease lock while a collector judges and removes it.

    Yield False when an owner holds the lease. A folder without a lock file has no lease.
    Use build_folder_lock_fd while the collector holds the lock.
    A lock file or lease record is judged by its folder's lease. With children, also hold every lease below the folder; a held one, or an unreadable
    folder, is busy.
    """
    with folder_removal_lease(path, children=children, wait=wait) as lease:
        yield not isinstance(lease, RefusedRemovalLease)


@contextmanager
def leased_build_folder(spec: BuildFolderSpec | UniqueBuildFolder | MainOutput, *,
                        on_create: FolderCreationNotice = "untracked") -> Generator[Path]:
    """Hold the maker lease while a persistent folder is in use."""
    path = create_build_folder(spec, on_create=on_create)
    try:
        yield path
    finally:
        _release_build_folder(path)


@contextmanager
def copy_build_folder(spec: BuildFolderSpec) -> Generator[Path]:
    """Keep the checkout locked and record its last use before release."""
    path = create_build_folder(spec)
    try:
        yield path
    finally:
        try:
            if spec.kind == "copy":
                mark_folder_use(path)
            with build_folder_lock.folder_parent(path, spec.parent, create=False) as directory:
                if spec.kind == "copy" and not _copy_is_kept(path, directory):
                    with suppress(FileNotFoundError):
                        os.unlink(copy_keep_file(path).name, dir_fd=directory)
                _write_lease(path, directory, spec, released=True)
        finally:
            _release_build_folder(path)


def _unlink_transient_lock(lease: HeldFolderRemovalLease) -> None:
    if build_folder_lock.lock_current(lease.folder, lease.directory, lease.descriptor):
        os.unlink(lease.folder.name + ".lock", dir_fd=lease.directory)


class _FolderRemoval(Enum):
    REMOVED = "removed the folder"
    PROTECTED = "the folder is protected"
    LOCK_HELD = "the folder lock is held"
    LOCK_CHANGED = "the folder lock file changed"
    REVIEW_ONLY = "the folder requires review"
    CLEANUP_KEPT = "the cleanup kept the folder"
    FOLDER_PRESENT = "the folder still exists after cleanup"


def remove_build_folder(path: Path, *, temporary: bool = True, dir_fd: int | None = None,
                        wait: bool = True, expected: os.stat_result | None = None,
                        repair_access: bool = False, prepare: Callable[[], bool] | None = None,
                        child_locks: tuple[int, ...] = (), delete: RemovalTreeAction | None = None,
                        transient_lock: bool = False, before_stage: RemovalTreeAction | None = None) -> bool:
    """Remove a free folder. Run optional preparation at its original path."""
    return _remove_build_folder(path, temporary=temporary, dir_fd=dir_fd, wait=wait, expected=expected,
                                repair_access=repair_access, prepare=prepare, child_locks=child_locks,
                                delete=delete, transient_lock=transient_lock, before_stage=before_stage) is _FolderRemoval.REMOVED


def _remove_build_folder(path: Path, *, temporary: bool = True, dir_fd: int | None = None,
                         wait: bool = True, expected: os.stat_result | None = None,
                         repair_access: bool = False, prepare: Callable[[], bool] | None = None,
                         child_locks: tuple[int, ...] = (), delete: RemovalTreeAction | None = None,
                         transient_lock: bool = False,
                         before_stage: RemovalTreeAction | None = None,
                         owner_finished: bool = False) -> _FolderRemoval:
    if not may_remove_build_path(path):
        return _FolderRemoval.PROTECTED
    for _attempt in range(2):
        with ExitStack() as parents:
            try:
                directory = dir_fd if dir_fd is not None else parents.enter_context(
                    build_folder_lock.folder_parent(path, path.parent, create=False))
            except FileNotFoundError:
                if expected is None and (prepare is None or prepare() is not False):
                    return _FolderRemoval.REMOVED
                return _FolderRemoval.CLEANUP_KEPT
            try:
                lease = _hold_free_lease(path, directory, parents, wait=wait, create=True,
                                         check_parent=dir_fd is None, transient_lock=transient_lock)
            except BuildFolderParentChanged:
                continue
            if isinstance(lease, RemovalLockHeld):
                return _FolderRemoval.LOCK_HELD
            if isinstance(lease, KeptCopyRemovalLease):
                return _FolderRemoval.PROTECTED
            if not isinstance(lease, HeldFolderRemovalLease):
                return _FolderRemoval.LOCK_CHANGED
            if lease.created:
                _ = parents.callback(_unlink_transient_lock, lease)
            try:
                allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                if temporary:
                    _unlink_metadata(path, directory)
                if expected is None and (prepare is None or prepare() is not False):
                    return _FolderRemoval.REMOVED
                return _FolderRemoval.CLEANUP_KEPT
            if expected is not None and not os.path.samestat(expected, allocated):
                return _FolderRemoval.CLEANUP_KEPT
            if prepare is not None and (not may_remove_folder_tree(path) or not prepare()):
                return _FolderRemoval.CLEANUP_KEPT
            review_temporary = (_temporary_claim_location(path) is not None
                                and (owner_finished or _lease_owner_state(path, directory) is LeaseOwnerState.ENDED))
            try:
                if not _remove_folder(path, directory, allocated, wait=wait, repair_access=repair_access,
                                      child_locks=child_locks, delete=delete, before_stage=before_stage,
                                      review_temporary=review_temporary):
                    return _FolderRemoval.CLEANUP_KEPT
            except TemporaryFolderReview:
                return _FolderRemoval.REVIEW_ONLY
            if _folder_exists(path, directory):
                return _FolderRemoval.FOLDER_PRESENT
            if temporary:
                _unlink_metadata(path, directory)
            return _FolderRemoval.REMOVED
    return _FolderRemoval.LOCK_CHANGED


def remove_folder(path: Path) -> None:
    """Remove a free folder and repair its permissions when needed."""
    removal = _remove_build_folder(path, repair_access=True)
    if removal is not _FolderRemoval.REMOVED:
        raise PermissionError(f"{path}: {removal.value}")


def expired_folder(path: Path, *, cutoff: float) -> bool:
    """Check an old use stamp while the folder is free."""
    form = parse_form(path)
    if not isinstance(form, (WorktreeOutput, MainOutput, BuildCopy)):
        return False
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(build_folder_lock.folder_parent(path, build_root(), create=False))
            lease = _hold_free_lease(path, directory, stack, wait=False, create=False)
            return (not isinstance(lease, RefusedRemovalLease)
                    and _folder_has_expired(path, directory, cutoff, form))
        except FileNotFoundError:
            return False


def _folder_has_expired(path: Path, directory: int, cutoff: float, form: FolderForm) -> bool:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    folder = os.open(path.name, flags, dir_fd=directory)
    try:
        info = os.fstat(folder)
        if info.st_dev != os.fstat(directory).st_dev:
            return False
        try:
            stamp = os.stat(COPY_USE_STAMP, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            if not isinstance(form, BuildCopy):
                return False
            try:
                fields = _read_folder_lease(path, directory)
                return (fields.get("ready") is False
                        and _lease_time(fields.get("last_use")).timestamp() <= cutoff
                        and isinstance(_numbered_copy_record(path, form, directory), NumberedCopySource))
            except (OSError, ValueError, RuntimeError):
                return False
        return stat.S_ISREG(stamp.st_mode) and stamp.st_mtime <= cutoff
    finally:
        os.close(folder)


class FolderPairRelease(Enum):
    RELEASED = "released the build folder pair"
    PAIR_ABSENT = "the build folder pair is absent"
    PARTIAL = "only part of the build folder pair was released"
    PAIR_KEPT = "the lease record and lock could not be removed"
    FOLDER_PRESENT = "the folder path still exists"
    LOCK_HELD = "the build folder lock is held or changed"
    PARENT_ABSENT = "the parent folder is absent"
    PARENT_LINK = "the parent path contains a symbolic link"


def release_removed_folder(path: Path) -> FolderPairRelease:
    """Release the lease and lock after another operation removes the folder."""
    path = path.absolute()
    if _parent_has_symlink(path.parent):
        return FolderPairRelease.PARENT_LINK
    for _attempt in range(2):
        with ExitStack() as parents:
            try:
                directory = parents.enter_context(build_folder_lock.folder_parent(path, Path(path.anchor), create=False))
            except FileNotFoundError:
                return FolderPairRelease.PARENT_ABSENT
            try:
                return _release_removed_folder_at(path, directory, reject_parent_links=True)
            except BuildFolderParentChanged:
                continue
    return FolderPairRelease.LOCK_HELD


def _release_removed_folder_at(path: Path, directory: int, *, reject_parent_links: bool) -> FolderPairRelease:
    if not build_folder_lock.parent_current(path, directory):
        raise BuildFolderParentChanged(str(path.parent))
    if _folder_exists(path, directory):
        return FolderPairRelease.FOLDER_PRESENT
    lock_name = path.name + ".lock"
    names = (path.name + ".lease.json", lock_name)
    present = {name for name in names if _folder_exists(path.with_name(name), directory)}
    if not present:
        return FolderPairRelease.PAIR_ABSENT
    with ExitStack() as locks:
        lease = _hold_free_lease(path, directory, locks, wait=False, create=True)
        if not isinstance(lease, HeldFolderRemovalLease):
            return FolderPairRelease.LOCK_HELD
        removed = lease.unlink_matching_sidecars(lease.sidecar_identities(), reject_parent_links=reject_parent_links)
        if present <= removed and lock_name in removed:
            return FolderPairRelease.RELEASED
        if removed:
            return FolderPairRelease.PARTIAL
        if _folder_exists(path, directory):
            return FolderPairRelease.FOLDER_PRESENT
        return FolderPairRelease.PAIR_KEPT


class _ScratchBorrowerCheck(Protocol):
    def build_folder_has_borrowers(self, path: Path) -> bool: ...


def _finish_temporary(path: Path, directory: int, allocated: os.stat_result, *,
                      descriptor: int, temporary: bool) -> None:
    if _locks.get(descriptor) != path:
        return
    _release_build_folder(path, descriptor=descriptor)
    with ExitStack() as claim:
        root = build_root().resolve()
        canonical = _temporary_claim_location(path, root)
        if canonical is not None:
            try:
                _ = claim.enter_context(build_folder_claim_lock(canonical, root, exclusive=True))
            except BuildFolderClaimHeld:
                return

        def prepare() -> bool:
            if canonical is None:
                return True
            scratch = cast(_ScratchBorrowerCheck, cast(object, import_module("scripts.lib.scratch")))
            return not scratch.build_folder_has_borrowers(canonical)

        _ = _remove_build_folder(path, temporary=temporary, dir_fd=directory, expected=allocated,
                                 repair_access=True, prepare=prepare, owner_finished=True)


def _remove_free(path: Path, directory: int, *, wait: bool) -> bool:
    """Remove a folder and its records when the folder and its children are free."""
    return remove_build_folder(path, dir_fd=directory, wait=wait)


def recover_staged_folder(source: Path, name: str, device: int, inode: int, *, dir_fd: int | None = None) -> bool:
    """Resolve the staging folder recorded in the source removal intent."""
    if _HIDDEN_REMOVAL.fullmatch(name) is None or name == source.name:
        return False
    path = source.with_name(name)
    if dir_fd is not None:
        return _recover_staged_folder(path, dir_fd, wait=False, identity=(device, inode))
    with ExitStack() as parents:
        try:
            directory = parents.enter_context(build_folder_lock.folder_parent(path, source.parent, create=False))
        except FileNotFoundError:
            return False
        return _recover_staged_folder(path, directory, wait=False, identity=(device, inode))


def _recover_staged_folder(path: Path, directory: int, *, wait: bool,
                           identity: tuple[int, int] | None = None) -> bool:
    """Hold the staging lock through deletion of the matching folder."""
    with ExitStack() as leases:
        original: os.stat_result | None = None
        try:
            stage = _hold_free_lease(path, directory, leases, wait=wait)
            if isinstance(stage, UnreservedRemovalFolder):
                return not _folder_exists(path, directory) and not _folder_exists(lease_path(path), directory)
            if not isinstance(stage, HeldFolderRemovalLease):
                return False
            lock_name = path.name + ".lock"
            lock = os.fstat(stage.descriptor)
            if (lock.st_uid != os.geteuid() or not build_folder_lock.is_marked_lock(lock_name, directory, lock)
                    or not build_folder_lock.parent_current(path, directory) or not may_remove_build_path(path)):
                return False
            if _folder_exists(path, directory):
                original = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(original.st_mode) or not may_remove_folder_tree(path):
                    return False
                if identity is not None and identity != (original.st_dev, original.st_ino):
                    return False
                try:
                    recorded = _read_folder_lease(path, directory, allow_empty=True)
                except FileNotFoundError:
                    recorded = {}
                if not recorded and identity is None:
                    _publish_lease(path, directory, _staging_identity(original))
                elif recorded != _staging_identity(original):
                    return False
                refusal = _hold_child_leases(path, directory, leases, wait=wait, repair_access=True)
                if isinstance(refusal, RefusedRemovalLease):
                    return False
        except (OSError, ValueError):
            return False
        if original is not None:
            try:
                CheckedRemovalTree(path, directory, original).remove(repair_access=True)
            except RemovalTreeChanged:
                return False
        if (_folder_exists(path, directory)
                or not build_folder_lock.lock_current(path, directory, stage.descriptor)):
            return False
        _unlink_metadata(path, directory)
        return True


def sweep_temporary_build_folders(parent: Path, *, wait: bool = True) -> None:
    """Remove free allocations in a parent reserved for temporary maker folders.
    With wait False, a held folder costs one lock try, not the probe wait."""
    try:
        with build_folder_lock.folder_parent(parent / "entry", parent, create=False) as directory:
            names: set[str] = set()
            for entry in os.listdir(directory):
                match = _METADATA.fullmatch(entry)
                if match is None:
                    continue
                name = match[1]
                if _HIDDEN_REMOVAL.fullmatch(name) is None or entry == name + ".lock":
                    names.add(name)
            for name in sorted(names):
                path = parent / name
                if _HIDDEN_REMOVAL.fullmatch(name) is not None:
                    _ = _recover_staged_folder(path, directory, wait=wait)
                else:
                    _ = _remove_free(path, directory, wait=wait)
    except FileNotFoundError:
        pass


@contextmanager
def temporary_build_folder(request: UniqueBuildFolder | BuildFolderSpec) -> Generator[Path]:
    """Remove private work after the caller ends, including interrupted callers."""
    with ExitStack() as cleanup:
        yield create_build_folder(_TemporaryFolderRequest(request, cleanup))


def _claim_tool_folder(tool: str, parent: Path | None, allocate: Callable[[BuildFolderSpec], Path], *,
                       owner_pid: int = 0) -> Path:
    """Allocate <parent>/<tool>, or <tool>-2 and up while another run holds a name.

    A folder whose lock is free and whose lease names an ended process is a stopped run's leftover:
    it is removed and its name reused. Any other folder is passed over.
    """
    root = parent if parent is not None else inherited_temporary_parent()
    number = 1
    while True:
        spec = BuildFolderSpec(kind="tool", parent=root, purpose=tool, suffix=f"-{number}" if number > 1 else "",
                               suffix_length=0, owner_pid=owner_pid)
        try:
            return allocate(spec)
        except BuildFolderClaimHeld:
            raise
        except BlockingIOError:
            number += 1
        except FileExistsError:
            path = build_folder_path(spec)
            with build_folder_lock.folder_parent(path, root, create=False) as directory:
                leftover = remove_build_folder(path, dir_fd=directory, wait=False,
                                               prepare=lambda: _lease_owner_state(path, directory) is LeaseOwnerState.ENDED)
            if not leftover:
                number += 1


class LeaseOwnerState(Enum):
    ENDED = "ended"
    ACTIVE = "active"
    UNKNOWN = "unknown"


def _lease_owner_state(path: Path, directory: int, *, allow_released: bool = False) -> LeaseOwnerState:
    """Check the recorded owner PID. Accept a release only when the caller permits it."""
    try:
        fields = _read_folder_lease(path, directory)
    except (OSError, ValueError):
        return LeaseOwnerState.UNKNOWN
    if allow_released and fields.get("released") is True:
        return LeaseOwnerState.ENDED
    pid = fields.get("pid")
    if type(pid) is not int or pid <= 0:
        return LeaseOwnerState.UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return LeaseOwnerState.ENDED
    except PermissionError:
        return LeaseOwnerState.ACTIVE
    except (OSError, OverflowError):
        return LeaseOwnerState.UNKNOWN
    return LeaseOwnerState.ACTIVE


@contextmanager
def tool_folder(tool: str, parent: Path | None = None) -> Generator[Path]:
    """Give one tool run its own folder in the session folder, and remove it when the run ends.

    The parent is the caller's temporary parent unless the caller names one.
    """
    with ExitStack() as cleanup:
        yield _claim_tool_folder(tool, parent, lambda spec: cleanup.enter_context(temporary_build_folder(spec)))


def run_build_command(command: Sequence[str], *, path: Path, environment: Mapping[str, str],
                      pass_fds: Sequence[int]) -> int:
    """Keep the caller until the command and its descendants stop."""
    from scripts.lib.descendants import adopt_descendants
    from scripts.lib.process_group import send_process_group

    if not command:
        raise ValueError("run needs a command after --")
    pid = os.getpid()
    if sys.platform == "darwin":
        child = subprocess.Popen(command, cwd=path, env=environment,
                                 pass_fds=pass_fds, start_new_session=True)
    else:
        _ = adopt_descendants()
        child = subprocess.Popen(command, cwd=path, env=environment,
                                 pass_fds=pass_fds, process_group=os.getpgrp())
    try:
        status = child.wait()
    finally:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        _ = child.wait()
        if sys.platform == "darwin":
            # A descendant outside the group still holds the folder if it inherits the lock descriptor.
            try:
                send_process_group(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            while True:
                try:
                    send_process_group(child.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
        else:
            while True:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
                for descendant in children:
                    try:
                        os.kill(int(descendant), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    _ = os.waitpid(-1, 0)
                except ChildProcessError:
                    break
    return status if status >= 0 else 128 - status


def run_build(owner: str, name: str, command: Sequence[str], *, showrunner: str = "", unit: str = "",
              work: str = "") -> int:
    """Keep the folder until the command and its descendants stop."""
    import json

    from scripts.lib.removal_guard import OWNER_MARKER

    settings = _settings()
    root = Path(os.path.expandvars(settings.text("roots.temporary").replace("${TMPDIR}", str(inherited_temporary_parent())))).expanduser().resolve()
    spec = BuildFolderSpec(kind="temporary", parent=root, repository=repository_key(Path.cwd()),
                           owner=owner, purpose=name, suffix_length=settings.whole("names.suffix_length"),
                           showrunner=showrunner, unit=unit, work=work)

    with temporary_build_folder(spec) as path:
        pid = os.getpid()
        start_time = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        information = path.stat()
        marker = {"owner": owner, "pid": pid, "start_time": start_time, "boot_id": boot_identity(),
                  "copy_path": str(path.resolve()), "copy_device": information.st_dev, "copy_inode": information.st_ino}
        _ = (path / OWNER_MARKER).write_text(json.dumps(marker) + "\n")
        descriptor = build_folder_lock_fd(path)
        return run_build_command(command, path=path,
                                 environment={**os.environ, "BUILD_FOLDER": str(path),
                                              "BUILD_FOLDER_LOCK_FD": str(descriptor)},
                                 pass_fds=(descriptor,))


def main(argv: Sequence[str]) -> int:
    # verify_helper.py runs main in its long-lived process. A lock kept past the call loses its
    # owner when the helper reloads this module, and a later remove cannot take that lock.
    # Thus each call releases the locks it took, as a process that exits does.
    held = set(_locks.items())
    try:
        return _command(argv)
    finally:
        for descriptor, path in list(_locks.items()):
            if (descriptor, path) not in held:
                _release_build_folder(path, descriptor=descriptor)


def _command(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    unique = commands.add_parser("unique")
    prefix = unique.add_mutually_exclusive_group(required=True)
    _ = prefix.add_argument("--prefix")
    _ = prefix.add_argument("--kind", choices=tuple(FOLDER_PREFIXES))
    _ = unique.add_argument("--owner", default="")
    unique_parent = unique.add_mutually_exclusive_group()
    _ = unique_parent.add_argument("--parent", type=Path)
    _ = unique_parent.add_argument("--system-temp", action="store_true")
    stable = commands.add_parser("stable")
    stable_destination = stable.add_mutually_exclusive_group()
    _ = stable_destination.add_argument("--path", type=Path)
    _ = stable_destination.add_argument("--run-temp", type=Path)
    _ = stable.add_argument("--name", default="")
    stable_parent = stable.add_mutually_exclusive_group()
    _ = stable_parent.add_argument("--parent", type=Path)
    _ = stable_parent.add_argument("--unit-root", action="store_true")
    editable = commands.add_parser("editable-repository")
    _ = editable.add_argument("--path", type=Path, required=True)
    source = editable.add_mutually_exclusive_group(required=True)
    _ = source.add_argument("--source", type=Path)
    _ = source.add_argument("--url")
    branch = editable.add_mutually_exclusive_group()
    _ = branch.add_argument("--new-branch")
    _ = branch.add_argument("--existing-branch")
    _ = editable.add_argument("--start-point")
    _ = editable.add_argument("--ref")
    _ = editable.add_argument("--depth", type=int)
    tool = commands.add_parser("tool")
    _ = tool.add_argument("--tool", required=True)
    _ = tool.add_argument("--parent", type=Path)
    _ = tool.add_argument("--owner-pid", type=int, required=True)
    remove = commands.add_parser("remove")
    _ = remove.add_argument("path", type=Path)
    run_temp = commands.add_parser("run-temp-path")
    _ = run_temp.add_argument("run", type=Path)
    run = commands.add_parser("run")
    _ = run.add_argument("--owner", default="")
    _ = run.add_argument("--showrunner", default="")
    _ = run.add_argument("--unit", default="")
    _ = run.add_argument("--work", default="")
    _ = run.add_argument("--name", required=True)
    _ = run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    request: BuildFolderRequest
    command = cast(str, args.command)
    try:
        if command == "editable-repository":
            path = cast(Path, args.path)
            source_path = cast(Path | None, args.source)
            new_branch = cast(str | None, args.new_branch)
            existing_branch = cast(str | None, args.existing_branch)
            start = cast(str | None, args.start_point)
            ref = cast(str | None, args.ref)
            depth = cast(int | None, args.depth)
            if source_path is not None:
                if ref is not None or depth is not None:
                    raise ValueError("a linked worktree does not use --ref or --depth")
                selection: NewWorktreeBranch | ExistingWorktreeBranch
                if new_branch is not None and start is not None:
                    selection = NewWorktreeBranch(new_branch, start)
                elif existing_branch is not None and start is None:
                    selection = ExistingWorktreeBranch(existing_branch)
                else:
                    raise ValueError("a linked worktree needs --new-branch and --start-point, or --existing-branch")
                request = LinkedEditableRepository(path, source_path, selection)
            else:
                if new_branch is not None or existing_branch is not None or start is not None:
                    raise ValueError("a remote clone does not use worktree branch flags")
                if ref is None or depth is None:
                    raise ValueError("a remote clone needs --ref and --depth")
                request = RemoteEditableRepository(path, cast(str, args.url), ref, depth)
            print(create_build_folder(request))
            return 0
        if command == "run":
            arguments = cast(list[str], args.argv)
            owner = cast(str, args.owner) or cast(str, args.unit)
            if not owner:
                raise ValueError("run needs --owner or --unit")
            return run_build(owner, cast(str, args.name), arguments[1:] if arguments[:1] == ["--"] else arguments,
                             showrunner=cast(str, args.showrunner), unit=cast(str, args.unit), work=cast(str, args.work))
        if command == "tool":
            print(_claim_tool_folder(cast(str, args.tool), cast(Path | None, args.parent), create_build_folder,
                                     owner_pid=cast(int, args.owner_pid)))
            return 0
        if command == "remove":
            path = cast(Path, args.path)
            if not may_remove_build_path(path):
                raise PermissionError(f"{path}: retain the protected build folder")
            _release_build_folder(path)
            removal = _remove_build_folder(path)
            if removal is not _FolderRemoval.REMOVED:
                print(f"build folders: {path}: {removal.value}", file=sys.stderr)
                return 1
            return 0
        if command == "run-temp-path":
            from scripts.lib import unit_runs

            print(unit_runs.run_temp_path(cast(Path, args.run)))
            return 0
        if command == "stable":
            path = cast(Path | None, args.path)
            run = cast(Path | None, args.run_temp)
            if run is not None:
                from scripts.lib import unit_runs

                path = unit_runs.run_temp_path(run)
            if path is not None:
                request = StableOwnerFolder(path.name, ExplicitBuildParent(path.parent))
            else:
                parent: BuildFolderParent = UnitRunParent() if cast(bool, args.unit_root) else ExplicitBuildParent(cast(Path, args.parent))
                if not cast(bool, args.unit_root) and cast(Path | None, args.parent) is None:
                    raise ValueError("a stable folder needs --path, --run-temp, or a parent and name")
                request = StableOwnerFolder(cast(str, args.name), parent)
        else:
            unique_choice: BuildFolderParent = InheritedTemporaryParent()
            explicit_parent = cast(Path | None, args.parent)
            if cast(bool, args.system_temp):
                unique_choice = SystemTemporaryParent()
            elif explicit_parent is not None:
                unique_choice = ExplicitBuildParent(explicit_parent)
            kind = cast(str | None, args.kind)
            name = FOLDER_PREFIXES[kind].format(owner=cast(str, args.owner)) if kind else cast(str, args.prefix)
            request = UniqueBuildFolder(name, unique_choice)
        print(create_build_folder(request))
    except (OSError, ValueError) as error:
        print(f"build folders: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
