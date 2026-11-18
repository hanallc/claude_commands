#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Name temporary build folders and preserve their caller's lifetime."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import fcntl
import json
import os
import errno
import re
import secrets
import shutil
import signal
import stat
from pathlib import Path
import sys
import tempfile
from threading import get_ident
import time
from typing import TYPE_CHECKING, Literal, cast, override

from scripts.lib import git_files
from scripts.lib.boot_identity import boot_identity

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"
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


def require_no_removal_mark(path: Path) -> None:
    """Refuse to use a source with a deletion intent on it or an ancestor."""
    for folder in (path, *path.parents):
        try:
            _ = removal_mark(folder).lstat()
        except FileNotFoundError:
            continue
        raise BlockingIOError(errno.EWOULDBLOCK, "source removal has already started", str(folder))


@contextmanager
def _linked_source_locks(source: Path) -> Generator[tuple[int, ...]]:
    """Hold the checkout and its shared Git folder while Git creates a link."""
    source = source.expanduser().resolve()
    require_no_removal_mark(source)
    try:
        checkout = git_files.locate(source)
    except git_files.NotACheckout:
        yield ()
        return
    with ExitStack() as stack:
        descriptors: list[int] = []
        for folder in dict.fromkeys((checkout.common_dir, checkout.git_dir)):
            mark = folder / BUSY
            descriptor = open_busy_mark(mark, create=True)
            _ = stack.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            descriptors.append(descriptor)
            require_no_removal_mark(folder)
            locked, present = os.fstat(descriptor), mark.lstat()
            if (locked.st_dev, locked.st_ino) != (present.st_dev, present.st_ino):
                raise BlockingIOError(errno.EWOULDBLOCK, "source busy mark changed", str(mark))
        require_no_removal_mark(source)
        yield tuple(descriptors)


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


@dataclass(frozen=True)
class RemoteEditableRepository:
    """Clone a remote ref at the exact caller path with the requested depth."""
    path: Path
    url: str
    ref: str
    depth: int


EditableRepositoryRequest = LinkedEditableRepository | RemoteEditableRepository
BuildFolderRequest = UniqueBuildFolder | StableOwnerFolder | EditableRepositoryRequest


def inherited_temporary_parent() -> Path:
    """Read the caller's current temporary parent."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def tmpfs_temporary_parent() -> Path:
    """Keep nested work under the caller's tmpfs folder."""
    root = Path(_settings().text("roots.gate"))
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


def _check_name_component(name: str) -> None:
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("a build folder name must be one path component")


def _check_name(name: str) -> None:
    if not isinstance(classify_removal_target(name), FolderTarget):
        raise ValueError(f"{name!r}: invalid folder name or name reserved for lease sidecars")


BuildFolderKind = Literal["temporary", "gate", "standing", "editable", "mirror", "target", "shell-cache",
                          "scratch", "legacy-unique", "legacy-stable", "tool", "slot"]


@dataclass(frozen=True)
class SlotCheckout:
    """The checkout holds the source repository and required commit for a slot."""
    source: Path
    commit: str


class SlotSourceBusy(BlockingIOError):
    """The source lock prevents creation of a slot checkout."""


@dataclass(frozen=True)
class NoSlotCheckout:
    """The folder does not contain a repository slot."""


@dataclass(frozen=True, kw_only=True)
class BuildFolderSpec:
    """The specification holds the folder kind, parent, name parts, and checkout source."""
    kind: BuildFolderKind
    parent: Path
    suffix_length: int
    repository: str = ""
    owner: str = ""
    purpose: str = ""
    slot: int = 0
    suffix: str = ""
    showrunner: str = ""
    unit: str = ""
    work: str = ""
    # The process the lease names; 0 names the allocating process.
    owner_pid: int = 0
    slot_checkout: SlotCheckout | NoSlotCheckout = NoSlotCheckout()
    machine: str = ""


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read_or_repository("build_folders")


def repository_git_timeout() -> float:
    """Keep the copies Git deadline when the maker creates a slot."""
    from scripts.lib import conf

    path = Path(os.environ.get("COPIES_CONFIG") or Path(__file__).resolve().parents[2] / "config/copies.conf")
    return float(conf.read(path.expanduser()).values()["git_timeout_seconds"])


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
    settings = _settings()
    if spec.kind == "slot":
        _check_name_component(spec.repository)
        name = f"{spec.repository}-slot{spec.slot}"
        _check_name(name)
        return spec.parent / name
    for component in (spec.repository, spec.owner, spec.purpose, spec.suffix, spec.showrunner, spec.unit, spec.work):
        _check_name_component(component)
    if spec.suffix_length < 0:
        raise ValueError("a suffix length cannot be negative")
    suffix = spec.suffix
    named = bool(spec.showrunner or spec.unit or spec.work)
    if named and not (spec.showrunner and spec.unit and spec.purpose):
        raise ValueError("a named build folder needs showrunner, unit, and purpose")
    if not suffix and ((named and not spec.work) or
                       (not named and spec.kind in ("temporary", "gate", "scratch", "legacy-unique"))):
        suffix = _new_suffix(spec.suffix_length, settings)
    pattern = settings.text("patterns.named" if named else f"patterns.{spec.kind}")
    for field, value in (("repository", spec.repository), ("owner", spec.owner), ("purpose", spec.purpose)):
        if "{" + field + "}" in pattern and not value and spec.kind != "legacy-unique":
            raise ValueError(f"{spec.kind} needs {field}")
    relative = pattern.format(
        repository=spec.repository, owner=spec.owner, purpose=spec.purpose, suffix=suffix, slot=spec.slot,
        showrunner=spec.showrunner, unit=spec.unit, work=spec.work or suffix)
    if not relative or Path(relative).is_absolute() or any(part in (".", "..") for part in relative.split("/")):
        raise ValueError("a build folder pattern must stay below its parent")
    path = spec.parent / relative
    if not path.is_relative_to(spec.parent) or path == spec.parent:
        raise ValueError("a build folder must stay below its parent")
    _check_name(path.name)
    return path


@contextmanager
def _folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
    """Open each parent below the root without following directory links."""
    canonical = root.resolve()
    if create:
        canonical.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(canonical, flags)
    try:
        for component in path.parent.relative_to(root).parts:
            if create:
                try:
                    os.mkdir(component, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=directory)
            previous = directory
            directory = child
            os.close(previous)
        yield directory
    finally:
        os.close(directory)


_locks: dict[int, Path] = {}
# Match a sidecar name relative to an explicit folder name.
LEASE_SIDECAR_NAME = re.compile(r"(.+)\.(?:lock|lease\.json(?:\.[0-9a-f]{16}\.tmp)?)")
CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _open_lock(path: Path, directory: int, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path.name + ".lock", flags, 0o600, dir_fd=directory)
    try:
        inode = os.fstat(descriptor)
        # Another process can remove the lock after this open; _lock_current refuses a lock with no link.
        if not stat.S_ISREG(inode.st_mode) or inode.st_nlink > 1:
            raise ValueError("a build folder lock must be a regular file with one link")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _take_lock(descriptor: int, mode: int, *, wait: bool = True) -> bool:
    """Try the lock until the configured probe wait ends, or once when wait is False."""
    settings = _settings()
    deadline = time.monotonic() + (settings.number("locks.probe_wait_seconds") if wait else 0.0)
    retry = settings.number("locks.probe_retry_seconds")
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(retry, remaining))


def _lock_current(path: Path, directory: int, descriptor: int) -> bool:
    try:
        return os.path.samestat(os.fstat(descriptor), os.stat(path.name + ".lock", dir_fd=directory,
                                                            follow_symlinks=False))
    except FileNotFoundError:
        return False


def build_folder_state(path: Path) -> Literal["held", "free"]:
    """Check the lock without using the lease record or session state."""
    try:
        with _folder_parent(path, path.parent, create=False) as directory:
            while True:
                descriptor = _open_lock(path, directory, create=False)
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_EX):
                        if _lock_current(path, directory, descriptor):
                            return "held"
                        continue
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    if _lock_current(path, directory, descriptor):
                        return "free"
                finally:
                    os.close(descriptor)
    except FileNotFoundError:
        return "free"


def build_folder_lock_fd(path: Path) -> int:
    """Return the held descriptor for subprocess pass_fds."""
    return next(descriptor for descriptor, candidate in reversed(_locks.items()) if candidate == path)


def held_build_folder_descriptors() -> tuple[int, ...]:
    """Retain all maker leases in source and command subprocesses."""
    return tuple(_locks)


def _release_build_folder(path: Path, *, descriptor: int | None = None) -> None:
    if descriptor is None:
        try:
            descriptor = build_folder_lock_fd(path)
        except StopIteration:
            return
    if _locks.get(descriptor) != path:
        return
    try:
        _ = _locks.pop(descriptor, None)
    finally:
        # Closing preserves a child's inherited hold on the same lock.
        os.close(descriptor)


def _write_lease(path: Path, directory: int, spec: BuildFolderSpec, *, released: bool = False,
                 ready: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    record = {"showrunner": spec.showrunner, "unit": spec.unit or spec.owner,
              "work": spec.work, "purpose": spec.purpose, "pid": spec.owner_pid or os.getpid(),
              "start_time": now}
    if spec.kind == "slot" and isinstance(spec.slot_checkout, SlotCheckout):
        if released:
            record = cast(dict[str, str | int], json.loads(path.with_name(path.name + ".lease.json").read_text()))
        record.update(machine=spec.machine, repository=spec.repository, slot=spec.slot,
                      commit=spec.slot_checkout.commit, last_use=now, ready=ready)
    record.update(kind=spec.kind, folder=path.name)
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
    if isinstance(request, (LinkedEditableRepository, RemoteEditableRepository)):
        path = request.path.expanduser().absolute()
        if not path.name:
            raise ValueError("an editable repository needs a destination name")
        _check_name(path.name)
        if isinstance(request, RemoteEditableRepository) and request.depth < 1:
            raise ValueError("an editable clone needs a positive depth")
        return BuildFolderSpec(kind="editable", parent=path.parent, purpose=path.name, suffix_length=0)
    else:
        stable = isinstance(request, StableOwnerFolder)
        name = request.name if isinstance(request, StableOwnerFolder) else request.prefix
        _check_name_component(name)
        if stable and not name:
            raise ValueError("a stable build folder needs an owner name")
        spec = BuildFolderSpec(kind="legacy-stable" if stable else "legacy-unique", parent=_parent(request.parent),
                               purpose=name, suffix_length=_settings().whole("names.suffix_length"))
        return spec


def _stable_folder(spec: BuildFolderSpec) -> bool:
    return spec.kind in ("standing", "editable", "mirror", "target", "shell-cache", "legacy-stable", "slot")


def create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest) -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    spec = _request_spec(original)
    with ExitStack() as source_locks:
        descriptors: tuple[int, ...] = ()
        if isinstance(original, LinkedEditableRepository):
            descriptors = source_locks.enter_context(_linked_source_locks(original.source))
        return _create_build_folder(request, spec, descriptors)


def _create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest,
                         spec: BuildFolderSpec, source_descriptors: tuple[int, ...]) -> Path:
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    if isinstance(original, LinkedEditableRepository) and isinstance(original.branch, ExistingWorktreeBranch):
        import subprocess

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
        with ExitStack() as allocation_scope:
            parents = request.cleanup if isinstance(request, _TemporaryFolderRequest) else allocation_scope
            directories: dict[Path, int] = {}
            while True:
                allocation = (replace(spec, work=_new_suffix(spec.suffix_length, _settings()))
                              if spec.kind != "slot" and (spec.showrunner or spec.unit) and not spec.work else spec)
                path = (original.path.expanduser().absolute()
                        if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository))
                        else build_folder_path(allocation))
                directory = directories.get(path.parent)
                if directory is None:
                    directory = parents.enter_context(_folder_parent(path, spec.parent,
                        create=stable or not isinstance(original, UniqueBuildFolder)))
                    directories[path.parent] = directory
                descriptor = _open_lock(path, directory, create=True)
                made_inode: os.stat_result | None = None
                new_folder = False
                new_branch_start = ""
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_SH if stable and spec.kind != "slot" and not editable_repository else fcntl.LOCK_EX,
                                      wait=spec.kind != "slot"):
                        if renamed:
                            continue
                        raise BlockingIOError(errno.EWOULDBLOCK, "build folder is held", str(path))
                    if not _lock_current(path, directory, descriptor):
                        continue
                    try:
                        existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        if editable_repository:
                            raise FileExistsError(errno.EEXIST, "editable repository destination is occupied", str(path))
                        if not stable:
                            if not renamed:
                                raise FileExistsError(errno.EEXIST, "build folder already exists", str(path))
                            continue
                        if not stat.S_ISDIR(existing.st_mode):
                            raise FileExistsError(errno.EEXIST, "build folder is not a directory", str(path))
                    if isinstance(spec.slot_checkout, SlotCheckout):
                        import subprocess

                        try:
                            lease = cast(dict[str, object], json.loads(
                                path.with_name(path.name + ".lease.json").read_text()))
                        except FileNotFoundError:
                            lease = {}
                        if existing is None or lease.get("ready") is not True:
                            try:
                                source_descriptors = allocation_scope.enter_context(_linked_source_locks(spec.slot_checkout.source))
                            except BlockingIOError as error:
                                raise SlotSourceBusy(errno.EWOULDBLOCK, f"slot source is busy: {error}",
                                                     str(spec.slot_checkout.source)) from error
                        if existing is not None and lease.get("ready") is not True:
                            if (path / ".git").exists():
                                from scripts.lib import git_rescue

                                checkout = git_files.at(path)
                                if checkout.common_dir != git_files.at(spec.slot_checkout.source).common_dir:
                                    raise ValueError(f"{path}: incomplete slot owns a different Git repository; retained")
                                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                                _ = git_rescue.rescue(path, f"{path.name}-recovery-{stamp}", generated=[path / "target"],
                                                     pass_fds=(descriptor, *held_build_folder_descriptors(), *source_descriptors))
                            _ = subprocess.run(["git", "-C", str(spec.slot_checkout.source), "worktree", "prune", "--expire", "now"],
                                               check=True, capture_output=True,
                                               pass_fds=(descriptor, *held_build_folder_descriptors(), *source_descriptors),
                                               timeout=repository_git_timeout())
                            _remove_folder(path, directory, existing)
                            _ = subprocess.run(["git", "-C", str(spec.slot_checkout.source), "worktree", "prune", "--expire", "now"],
                                               check=True, capture_output=True,
                                               pass_fds=(descriptor, *held_build_folder_descriptors(), *source_descriptors),
                                               timeout=repository_git_timeout())
                            existing = None
                    new_folder = existing is None
                    if isinstance(spec.slot_checkout, SlotCheckout):
                        _write_lease(path, directory, allocation, ready=not new_folder)
                    else:
                        _write_lease(path, directory, allocation)
                    if existing is None:
                        mode = request.mode if isinstance(request, StableOwnerFolder) else 0o777 if stable else 0o700
                        try:
                            os.mkdir(path.name, mode=mode, dir_fd=directory)
                            made_inode = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        except FileExistsError:
                            if not stable or editable_repository:
                                raise
                            if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                                raise
                    allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository)):
                        import subprocess

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
                                                    pass_fds=source_descriptors)
                                start = subprocess.run(["git", "-C", str(original.source), "rev-parse", "--verify",
                                                        f"refs/heads/{original.branch.name}"], check=True,
                                                       capture_output=True, text=True, timeout=repository_git_timeout(),
                                                       pass_fds=source_descriptors)
                                new_branch_start = start.stdout.strip()
                            _ = subprocess.run(command, check=True, capture_output=True, text=True,
                                               timeout=repository_git_timeout(), pass_fds=source_descriptors)
                        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                            detail: str | bytes = (str(error) if isinstance(error, OSError)
                                                   else cast(str | bytes, error.stderr or str(error)))
                            if isinstance(detail, bytes):
                                detail = detail.decode(errors="replace")
                            raise OSError(errno.EIO, f"git repository creation failed: {detail.strip()}", str(path)) from error
                        return path
                    if new_folder and isinstance(spec.slot_checkout, SlotCheckout):
                        import subprocess

                        _ = subprocess.run(["git", "-C", str(spec.slot_checkout.source), "worktree", "add", "--detach",
                                            str(path), spec.slot_checkout.commit], check=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            pass_fds=(descriptor, *held_build_folder_descriptors(), *source_descriptors),
                                           timeout=repository_git_timeout())
                        _write_lease(path, directory, allocation)
                    os.set_inheritable(descriptor, True)
                    _locks[descriptor] = path
                    if isinstance(request, _TemporaryFolderRequest):
                        _ = request.cleanup.callback(_finish_temporary, path, directory, allocated,
                                                     descriptor=descriptor, temporary=not stable)
                    return path
                except BaseException:
                    _ = _locks.pop(descriptor, None)
                    if (made_inode is not None or (new_folder and not stable)) and _take_lock(descriptor, fcntl.LOCK_EX):
                        _remove_failed_allocation(HeldFolderRemovalLease(path, directory, descriptor), made_inode)
                        if isinstance(original, LinkedEditableRepository):
                            import subprocess

                            cleanup_commands = [["git", "-C", str(original.source), "worktree", "prune", "--expire", "now"]]
                            if new_branch_start and isinstance(original.branch, NewWorktreeBranch):
                                # The expected value protects a branch that changed after creation.
                                cleanup_commands.append(["git", "-C", str(original.source), "update-ref", "-d",
                                                         f"refs/heads/{original.branch.name}", new_branch_start])
                            for cleanup_command in cleanup_commands:
                                try:
                                    _ = subprocess.run(cleanup_command, capture_output=True,
                                                       timeout=repository_git_timeout(), pass_fds=source_descriptors)
                                except (OSError, subprocess.TimeoutExpired):
                                    # Keep the creation error when a cleanup command fails.
                                    pass
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


def _remove_failed_allocation(lease: HeldFolderRemovalLease, allocated: os.stat_result | None) -> None:
    path, directory = lease.folder, lease.directory
    if allocated is not None:
        _remove_folder(path, directory, allocated)
    if not _folder_exists(path, directory):
        sidecars = lease.allocation_sidecars(temporary=True)
        if isinstance(sidecars, VerifiedRemovalSidecars):
            cleanup = lease.unlink_sidecars(sidecars)
            if isinstance(cleanup, CleanupIncomplete):
                print(f"build folders: {cleanup.reason}", file=sys.stderr)


def _remove_folder(path: Path, directory: int, allocated: os.stat_result) -> None:
    def repair(function: object, name: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(Path(name).parent, 0o700, dir_fd=directory)
        if not stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            os.chmod(name, 0o700, dir_fd=directory, follow_symlinks=False)
        if function is os.open:
            shutil.rmtree(name, dir_fd=directory, onexc=repair)
        else:
            _ = cast(Callable[..., object], function)(name, dir_fd=directory)

    try:
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if os.path.samestat(current, allocated):
        shutil.rmtree(path.name, dir_fd=directory, onexc=repair)


def _folder_exists(path: Path, directory: int) -> bool:
    try:
        _ = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


@dataclass(frozen=True)
class RefusedRemovalLease:
    """A busy or unreadable lock prevents removal."""

    reason: str = "the build folder lock is held"


@dataclass(frozen=True)
class UnreservedRemovalFolder:
    """The folder has no lock file to hold."""


SidecarIdentity = tuple[int, int, int]


@dataclass(frozen=True)
class VerifiedRemovalSidecars:
    """The exact sidecar names and identities to remove."""

    entries: tuple[tuple[str, SidecarIdentity], ...] = ()


@dataclass(frozen=True)
class RemovalSidecarConflict:
    """A name or file conflicts with the expected lease group."""

    name: str


@dataclass(frozen=True)
class FolderTarget:
    """The name can identify a folder."""

    name: str


@dataclass(frozen=True)
class UnverifiedSidecarName:
    """The name matches a sidecar pattern."""

    name: str
    folder: str


@dataclass(frozen=True)
class VerifiedSidecarGroup:
    """The sidecars have ownership evidence for this folder."""

    folder: str
    sidecars: VerifiedRemovalSidecars


class SidecarOwnership(Enum):
    DISCOVERY = "a lease record identifies the folder"
    GATE = "a gate record identifies the folder"
    ALLOCATION = "the caller identifies a reserved allocation"


@dataclass(frozen=True)
class AllocationSidecars:
    """Use the sidecars of a folder from a trusted allocation path."""


ALLOCATION_SIDECARS = AllocationSidecars()


def removal_sidecar_identity(status: os.stat_result) -> SidecarIdentity:
    return status.st_dev, status.st_ino, status.st_ctime_ns


def read_removal_sidecars(directory: int, names: Iterable[str]) -> VerifiedRemovalSidecars | RemovalSidecarConflict:
    """Read each selected regular file through the parent descriptor."""
    entries: list[tuple[str, SidecarIdentity]] = []
    for name in sorted(names):
        try:
            status = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError:
            return RemovalSidecarConflict(name)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            return RemovalSidecarConflict(name)
        entries.append((name, removal_sidecar_identity(status)))
    return VerifiedRemovalSidecars(tuple(entries))


def lease_record_identity(name: str, folder: str, directory: int, ownership: SidecarOwnership
                          ) -> SidecarIdentity | RemovalSidecarConflict:
    """Read ownership evidence from a regular lease record."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, encoding="utf-8") as contents:
            status = os.fstat(contents.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                return RemovalSidecarConflict(name)
            raw = cast(object, json.loads(contents.read(65537)))
    except (OSError, ValueError):
        return RemovalSidecarConflict(name)
    if not isinstance(raw, dict):
        return RemovalSidecarConflict(name)
    record = cast(dict[str, object], raw)
    if ownership is SidecarOwnership.GATE:
        owned = record.get("folder") == folder and record.get("kind") in ("gate", "standing")
    elif "folder" in record or "kind" in record:
        owned = record.get("folder") == folder and record.get("kind") in (
            "temporary", "gate", "standing", "editable", "mirror", "target", "shell-cache",
            "scratch", "legacy-unique", "legacy-stable", "tool", "slot")
    else:
        owned = (type(record.get("pid")) is int
                 and all(isinstance(record.get(field), str)
                         for field in ("showrunner", "unit", "work", "purpose", "start_time")))
    return removal_sidecar_identity(status) if owned else RemovalSidecarConflict(name)


def classify_removal_target(
    name: str, *, directory: int | None = None, sidecars: Iterable[str] = (),
    ownership: SidecarOwnership = SidecarOwnership.DISCOVERY,
) -> FolderTarget | UnverifiedSidecarName | VerifiedSidecarGroup | RemovalSidecarConflict:
    """Classify a name or verify a group in an open parent."""
    if name in ("", ".", "..") or "/" in name or CONTROL_CHARACTER.search(name):
        return RemovalSidecarConflict(name)
    match = LEASE_SIDECAR_NAME.fullmatch(name)
    if match is not None:
        folder = match[1]
        if folder in (".", "..") or LEASE_SIDECAR_NAME.fullmatch(folder):
            return RemovalSidecarConflict(name)
        return UnverifiedSidecarName(name, folder)
    names = tuple(sidecars)
    if not names:
        return FolderTarget(name)
    if directory is None:
        return RemovalSidecarConflict(name)
    for sidecar in names:
        candidate = classify_removal_target(sidecar)
        if not isinstance(candidate, UnverifiedSidecarName) or candidate.folder != name:
            return RemovalSidecarConflict(sidecar)
    verified = read_removal_sidecars(directory, names)
    if isinstance(verified, RemovalSidecarConflict):
        return verified
    if ownership is not SidecarOwnership.ALLOCATION:
        record_name = name + ".lease.json"
        entries = dict(verified.entries)
        identity = lease_record_identity(record_name, name, directory, ownership)
        if isinstance(identity, RemovalSidecarConflict) or entries.get(record_name) != identity:
            return RemovalSidecarConflict(record_name)
        if name + ".lock" not in entries:
            return RemovalSidecarConflict(name + ".lock")
    return VerifiedSidecarGroup(name, verified)


def sidecar_names_for(folder: str, names: Iterable[str]) -> tuple[str, ...]:
    """List names that can belong to the specified folder."""
    return tuple(sorted(name for name in names
                        if isinstance(candidate := classify_removal_target(name), UnverifiedSidecarName)
                        and candidate.folder == folder))


class RemovalPathChanged(OSError):
    """The visible parent or lock no longer identifies the held folder."""


@dataclass(frozen=True)
class FolderRemovalGuard:
    folder: Path
    parent_descriptor: int | None
    lock_fds: tuple[int, ...]

    def check_path(self) -> None:
        """Verify the visible path before a removal step."""
        if self.parent_descriptor is None:
            if not os.path.lexists(self.folder.parent):
                return
        else:
            try:
                parent = self.folder.parent.stat()
                if (os.path.samestat(parent, os.fstat(self.parent_descriptor))
                        and _lock_current(self.folder, self.parent_descriptor, self.lock_fds[0])):
                    return
            except OSError:
                pass
        raise RemovalPathChanged(f"{self.folder}: the removal parent or lock changed")


@dataclass(frozen=True)
class CleanupCompleted:
    """The callback completed all removal steps."""


@dataclass(frozen=True)
class CleanupIncomplete:
    """The callback could not complete all removal steps."""

    reason: str = "folder cleanup is incomplete"


FolderCleanup = Callable[[FolderRemovalGuard], CleanupCompleted | CleanupIncomplete]


@dataclass(frozen=True)
class HeldFolderRemovalLease:
    """The descriptor reserves the folder name until lock removal."""

    folder: Path
    directory: int
    descriptor: int

    def guard(self) -> FolderRemovalGuard:
        """Provide the path check and descriptors for removal steps."""
        return FolderRemovalGuard(self.folder, self.directory, (self.descriptor,))

    def remove(
        self, cleanup: FolderCleanup, *, sidecars: VerifiedRemovalSidecars | AllocationSidecars = ALLOCATION_SIDECARS,
        temporary: bool = True,
    ) -> RemovedBuildFolder | RefusedRemovalLease | IncompleteBuildFolderRemoval:
        """Run cleanup that uses the visible path under this lock."""
        guard = self.guard()

        def verified_cleanup() -> CleanupCompleted | CleanupIncomplete:
            guard.check_path()
            outcome = cleanup(guard)
            guard.check_path()
            return outcome

        selected = self.allocation_sidecars(temporary=temporary) if isinstance(sidecars, AllocationSidecars) else sidecars
        if isinstance(selected, RemovalSidecarConflict):
            return RefusedRemovalLease(f"{self.folder}: cannot verify {selected.name}")
        return _remove_from_locked_parent(self, verified_cleanup, sidecars=selected)

    def allocation_sidecars(self, *, temporary: bool) -> VerifiedRemovalSidecars | RemovalSidecarConflict:
        """Select sidecars for a folder identified by its allocation path."""
        if not temporary:
            return VerifiedRemovalSidecars()
        return self.group_sidecars(sidecar_names_for(self.folder.name, os.listdir(self.directory)),
                                   SidecarOwnership.ALLOCATION)

    def group_sidecars(self, names: Iterable[str], ownership: SidecarOwnership
                       ) -> VerifiedRemovalSidecars | RemovalSidecarConflict:
        """Verify the selected sidecars and their held lock."""
        result = classify_removal_target(self.folder.name, directory=self.directory, sidecars=names, ownership=ownership)
        if isinstance(result, VerifiedSidecarGroup):
            self.check_sidecars(result.sidecars)
            return result.sidecars
        return result if isinstance(result, RemovalSidecarConflict) else RemovalSidecarConflict(self.folder.name)

    def check_sidecars(self, sidecars: VerifiedRemovalSidecars) -> None:
        """Check that the selected files still have their verified identities."""
        lock_name = self.folder.name + ".lock"
        lock_identity = removal_sidecar_identity(os.fstat(self.descriptor))
        for name, identity in sidecars.entries:
            match = classify_removal_target(name)
            if (not isinstance(match, UnverifiedSidecarName) or match.folder != self.folder.name
                    or self._sidecar_identity(name) != identity
                    or name == lock_name and identity != lock_identity):
                raise RemovalPathChanged(f"{self.folder}: sidecar changed: {name}")

    def _sidecar_identity(self, name: str) -> SidecarIdentity | None:
        try:
            inode = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        except OSError:
            return None
        if stat.S_ISREG(inode.st_mode) and inode.st_nlink == 1:
            return removal_sidecar_identity(inode)
        return None

    def unlink_sidecars(self, expected: VerifiedRemovalSidecars) -> CleanupCompleted | CleanupIncomplete:
        """Remove the interrupted records, canonical record, and lock in order."""
        self.check_sidecars(expected)
        lock_name = self.folder.name + ".lock"
        record_name = self.folder.name + ".lease.json"
        for name, identity in sorted(expected.entries, key=lambda entry: (entry[0] == lock_name, entry[0] == record_name)):
            if _folder_exists(self.folder, self.directory) or not _lock_current(self.folder, self.directory, self.descriptor):
                raise RemovalPathChanged(f"{self.folder}: the folder or lock changed")
            self.check_sidecars(VerifiedRemovalSidecars(((name, identity),)))
            try:
                os.unlink(name, dir_fd=self.directory)
            except OSError as error:
                return CleanupIncomplete(f"{self.folder}: cannot remove {name}: {error}")
        return CleanupCompleted()


FolderRemovalLease = RefusedRemovalLease | UnreservedRemovalFolder | HeldFolderRemovalLease


_COLLECTOR_LEASES: ContextVar[tuple[tuple[int, int, HeldFolderRemovalLease], ...]] = ContextVar(
    "build_folder_collector_leases", default=())


def current_folder_removal_lease(path: Path) -> HeldFolderRemovalLease | UnreservedRemovalFolder:
    """Find the lease that the current collector already holds for this folder."""
    path = path.absolute()
    for process, thread, lease in reversed(_COLLECTOR_LEASES.get()):
        if (process == os.getpid() and thread == get_ident() and lease.folder.absolute() == path
                and _locks.get(lease.descriptor) == lease.folder):
            return lease
    return UnreservedRemovalFolder()


def _hold_free_lease(path: Path, directory: int, stack: ExitStack, *, wait: bool = True,
                     create: bool = False) -> FolderRemovalLease:
    """Hold the lock if it is free. The stack owns the descriptor."""
    try:
        descriptor = _open_lock(path, directory, create=create)
    except FileNotFoundError:
        return UnreservedRemovalFolder()
    _ = stack.callback(os.close, descriptor)
    if not _take_lock(descriptor, fcntl.LOCK_EX, wait=wait) or not _lock_current(path, directory, descriptor):
        return RefusedRemovalLease()
    _ = stack.callback(_locks.pop, descriptor, None)
    _locks[descriptor] = path
    return HeldFolderRemovalLease(path, directory, descriptor)


def _hold_child_leases(path: Path, stack: ExitStack, *, wait: bool = True) -> CleanupCompleted | RefusedRemovalLease:
    """Hold child locks next to folders or lease records."""
    def unreadable(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    for top, folders, files, directory in os.fwalk(path, follow_symlinks=False, onerror=unreadable):
        directories = set(folders)
        children = directories.copy()
        entries = directories.union(files)
        for name in entries:
            candidate = classify_removal_target(name)
            if name in directories and not isinstance(candidate, FolderTarget):
                return RefusedRemovalLease(f"{Path(top) / name}: conflicting sidecar name")
            if isinstance(candidate, UnverifiedSidecarName) and name != candidate.folder + ".lock":
                children.add(candidate.folder)
        for child in children:
            if child + ".lock" not in entries:
                continue
            lease = _hold_free_lease(Path(top) / child, directory, stack, wait=wait)
            if not isinstance(lease, HeldFolderRemovalLease):
                return RefusedRemovalLease(f"{Path(top) / child}: cannot hold the child lease")
    return CleanupCompleted()


@contextmanager
def folder_removal_lease(path: Path, *, children: bool = False, wait: bool = True,
                         create: bool = False, dir_fd: int | None = None) -> Generator[FolderRemovalLease]:
    """Hold the collector's locks for the specified folder."""
    if not isinstance(classify_removal_target(path.name), FolderTarget):
        yield RefusedRemovalLease(f"{path}: the removal target is reserved for lease sidecars")
        return
    with ExitStack() as stack:
        try:
            directory = dir_fd if dir_fd is not None else stack.enter_context(
                _folder_parent(path, path.parent, create=False))
            lease = _hold_free_lease(path, directory, stack, wait=wait, create=create)
            if not isinstance(lease, RefusedRemovalLease) and children:
                with suppress(FileNotFoundError):
                    children_lease = _hold_child_leases(path, stack, wait=wait)
                    if isinstance(children_lease, RefusedRemovalLease):
                        lease = children_lease
        except FileNotFoundError:
            lease = UnreservedRemovalFolder()
        except (OSError, ValueError):
            lease = RefusedRemovalLease()
        if isinstance(lease, HeldFolderRemovalLease):
            entry = (os.getpid(), get_ident(), lease)
            token = _COLLECTOR_LEASES.set((*_COLLECTOR_LEASES.get(), entry))
            try:
                yield lease
            finally:
                _COLLECTOR_LEASES.reset(token)
        else:
            yield lease


@contextmanager
def collector_lease(path: Path, *, children: bool = False, wait: bool = True) -> Generator[bool]:
    """Hold a managed folder's lease lock while a collector judges and removes it.

    Yield False when an owner holds the lease. A folder without a lock file has no lease.
    Use build_folder_lock_fd while the collector holds the lock.
    With children, also hold each lease below the folder.
    A held lease or an unreadable folder is busy.
    """
    with folder_removal_lease(path, children=children, wait=wait) as lease:
        yield not isinstance(lease, RefusedRemovalLease)


@contextmanager
def leased_build_folder(spec: BuildFolderSpec) -> Generator[Path]:
    """Hold the maker lease while a persistent folder is in use."""
    path = create_build_folder(spec)
    try:
        yield path
    finally:
        _release_build_folder(path)


@contextmanager
def slot_build_folder(spec: BuildFolderSpec) -> Generator[Path]:
    """Keep a slot and update its last use before releasing its lock."""
    path = create_build_folder(spec)
    try:
        yield path
    finally:
        try:
            with _folder_parent(path, spec.parent, create=False) as directory:
                _write_lease(path, directory, spec, released=True)
        finally:
            _release_build_folder(path)


@dataclass(frozen=True)
class RemovedBuildFolder:
    """Cleanup succeeded and the folder is absent."""


@dataclass(frozen=True)
class IncompleteBuildFolderRemoval:
    """Cleanup failed or left the folder in place."""

    reason: str = "folder cleanup is incomplete"


def _remove_from_locked_parent(
    lease: HeldFolderRemovalLease, cleanup: Callable[[], CleanupCompleted | CleanupIncomplete], *,
    sidecars: VerifiedRemovalSidecars,
) -> RemovedBuildFolder | RefusedRemovalLease | IncompleteBuildFolderRemoval:
    """Remove only the selected files through the held parent descriptor."""
    path, directory, descriptor = lease.folder, lease.directory, lease.descriptor
    if not _lock_current(path, directory, descriptor):
        return RefusedRemovalLease()
    try:
        lease.check_sidecars(sidecars)
        outcome = cleanup()
        if isinstance(outcome, CleanupIncomplete):
            return IncompleteBuildFolderRemoval(outcome.reason)
        if _folder_exists(path, directory):
            return IncompleteBuildFolderRemoval()
        if not _lock_current(path, directory, descriptor):
            return RefusedRemovalLease()
        outcome = lease.unlink_sidecars(sidecars)
        if isinstance(outcome, CleanupIncomplete):
            return IncompleteBuildFolderRemoval(outcome.reason)
    except RemovalPathChanged as error:
        return RefusedRemovalLease(str(error))
    return RemovedBuildFolder()


def remove_folder_with_lease(path: Path, cleanup: FolderCleanup, *, temporary: bool = True,
                             dir_fd: int | None = None, wait: bool = True
                             ) -> RemovedBuildFolder | RefusedRemovalLease | IncompleteBuildFolderRemoval:
    """Run cleanup with the folder lock held. Pass the lock to any removal subprocess."""
    if not isinstance(classify_removal_target(path.name), FolderTarget):
        return RefusedRemovalLease(f"{path}: the removal target is reserved for lease sidecars")
    with ExitStack() as parents:
        try:
            directory = dir_fd if dir_fd is not None else parents.enter_context(
                _folder_parent(path, path.parent, create=False))
        except FileNotFoundError:
            # An absent parent has no folder or metadata to lock.
            guard = FolderRemovalGuard(path, None, ())
            try:
                guard.check_path()
                outcome = cleanup(guard)
                guard.check_path()
            except RemovalPathChanged as error:
                return RefusedRemovalLease(str(error))
            return IncompleteBuildFolderRemoval(outcome.reason) if isinstance(outcome, CleanupIncomplete) else RemovedBuildFolder()
        descriptor = _open_lock(path, directory, create=True)
        try:
            if not _take_lock(descriptor, fcntl.LOCK_EX, wait=wait):
                return RefusedRemovalLease()
            return HeldFolderRemovalLease(path, directory, descriptor).remove(cleanup, temporary=temporary)
        finally:
            os.close(descriptor)


def remove_build_folder(path: Path, cleanup: Callable[[], bool | None], *, temporary: bool = True,
                        dir_fd: int | None = None, wait: bool = True) -> bool:
    """Run cleanup under the free folder's lock, then remove temporary metadata."""
    def compatible_cleanup(_guard: FolderRemovalGuard) -> CleanupCompleted | CleanupIncomplete:
        return CleanupIncomplete() if cleanup() is False else CleanupCompleted()

    return isinstance(remove_folder_with_lease(path, compatible_cleanup, temporary=temporary,
                                               dir_fd=dir_fd, wait=wait), RemovedBuildFolder)


def _finish_temporary(path: Path, directory: int, allocated: os.stat_result, *,
                      descriptor: int, temporary: bool) -> None:
    if _locks.get(descriptor) != path:
        return
    _release_build_folder(path, descriptor=descriptor)

    def cleanup() -> CleanupCompleted:
        _remove_folder(path, directory, allocated)
        return CleanupCompleted()

    with ExitStack() as locks:
        lease = _hold_free_lease(path, directory, locks)
        if isinstance(lease, HeldFolderRemovalLease):
            sidecars = lease.allocation_sidecars(temporary=temporary)
            if isinstance(sidecars, VerifiedRemovalSidecars):
                result = _remove_from_locked_parent(lease, cleanup, sidecars=sidecars)
                if isinstance(result, IncompleteBuildFolderRemoval):
                    print(f"build folders: {result.reason}", file=sys.stderr)


def _remove_free(path: Path, directory: int, *, wait: bool) -> bool:
    """Remove a folder and its records when no process holds its lock or a lease below it."""
    def cleanup(guard: FolderRemovalGuard) -> CleanupCompleted | CleanupIncomplete:
        try:
            allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return CleanupCompleted()
        with ExitStack() as children:
            # A held lease below a free folder, or an unreadable folder below it, keeps the folder.
            try:
                child_leases = _hold_child_leases(path, children, wait=wait)
                if isinstance(child_leases, RefusedRemovalLease):
                    print(f"build folders: {child_leases.reason}", file=sys.stderr)
                    return CleanupIncomplete(child_leases.reason)
            except OSError:
                return CleanupIncomplete()
            guard.check_path()
            _remove_folder(path, directory, allocated)
        return CleanupCompleted()

    return isinstance(remove_folder_with_lease(path, cleanup, dir_fd=directory, wait=wait), RemovedBuildFolder)


def sweep_temporary_build_folders(parent: Path, *, wait: bool = True) -> None:
    """Remove free allocations in a parent reserved for temporary maker folders.
    With wait False, a held folder costs one lock try, not the probe wait."""
    try:
        with _folder_parent(parent / "entry", parent, create=False) as directory:
            names: set[str] = set()
            for entry in os.listdir(directory):
                candidate = classify_removal_target(entry)
                if isinstance(candidate, UnverifiedSidecarName):
                    names.add(candidate.folder)
                elif isinstance(candidate, RemovalSidecarConflict):
                    print(f"build folders: conflicting sidecar name: {parent / entry}", file=sys.stderr)
            for name in sorted(names):
                _ = _remove_free(parent / name, directory, wait=wait)
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
        except BlockingIOError:
            number += 1
        except FileExistsError:
            path = build_folder_path(spec)
            with _folder_parent(path, root, create=False) as directory:
                leftover = _lease_owner_ended(path, directory) and _remove_free(path, directory, wait=False)
            if not leftover:
                number += 1


def _lease_owner_ended(path: Path, directory: int) -> bool:
    """True when the lease names a process that no longer runs. An unreadable lease is not proof."""
    try:
        descriptor = os.open(path.name + ".lease.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return False
    with os.fdopen(descriptor) as lease:
        try:
            record = cast(object, json.load(lease))
        except ValueError:
            return False
    pid = cast(dict[str, object], record).get("pid") if isinstance(record, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@contextmanager
def tool_folder(tool: str, parent: Path | None = None) -> Generator[Path]:
    """Give one tool run its own folder in the session folder, and remove it when the run ends.

    The parent is the caller's temporary parent unless the caller names one.
    """
    with ExitStack() as cleanup:
        yield _claim_tool_folder(tool, parent, lambda spec: cleanup.enter_context(temporary_build_folder(spec)))


OWNER_MARKER = ".build-folder-owner.json"


def run_build_command(command: Sequence[str], *, path: Path, environment: Mapping[str, str],
                      pass_fds: Sequence[int]) -> int:
    """Keep the caller until the command and its descendants stop."""
    import subprocess
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
            # A descendant outside the group still holds the slot if it inherits the lock descriptor.
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

    settings = _settings()
    root = Path(os.path.expandvars(settings.text("roots.temporary").replace("${TMPDIR}", str(inherited_temporary_parent())))).expanduser().resolve()
    spec = BuildFolderSpec(kind="temporary", parent=root, repository=repository_key(Path.cwd()),
                           owner=owner, purpose=name, suffix_length=settings.whole("names.suffix_length"),
                           showrunner=showrunner, unit=unit, work=work)

    with temporary_build_folder(spec) as path:
        pid = os.getpid()
        start_time = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        marker = {"owner": owner, "pid": pid, "start_time": start_time,
                  "boot_id": boot_identity()}
        _ = (path / OWNER_MARKER).write_text(json.dumps(marker) + "\n")
        descriptor = build_folder_lock_fd(path)
        return run_build_command(command, path=path,
                                 environment={**os.environ, "BUILD_FOLDER": str(path),
                                              "BUILD_FOLDER_LOCK_FD": str(descriptor)},
                                 pass_fds=(descriptor,))


def main(argv: Sequence[str]) -> int:
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
    remove_with = commands.add_parser("remove-with-command")
    _ = remove_with.add_argument("path", type=Path)
    _ = remove_with.add_argument("argv", nargs=argparse.REMAINDER)
    check_removal = commands.add_parser("check-removal-path")
    _ = check_removal.add_argument("path", type=Path)
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
            _release_build_folder(path)
            def cleanup() -> None:
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    pass
            return 0 if remove_build_folder(path, cleanup) else 1
        if command == "remove-with-command":
            import subprocess

            path = cast(Path, args.path).absolute()
            arguments = cast(list[str], args.argv)
            if not arguments:
                raise ValueError("remove-with-command requires a removal command")

            def run_removal(guard: FolderRemovalGuard) -> CleanupCompleted:
                parent = guard.parent_descriptor
                descriptors = () if parent is None else (parent, *guard.lock_fds)
                environment = {**os.environ, "BUILD_FOLDER_REMOVAL_FDS": " ".join(map(str, descriptors))}
                _ = subprocess.run(arguments, check=True, pass_fds=descriptors, env=environment)
                return CleanupCompleted()

            try:
                removal = remove_folder_with_lease(path, run_removal)
            except subprocess.CalledProcessError as error:
                return error.returncode if error.returncode > 0 else 128 - error.returncode
            if isinstance(removal, RefusedRemovalLease):
                print(f"Error: {removal.reason}; the worktree stays: {path}")
                return 1
            if isinstance(removal, IncompleteBuildFolderRemoval):
                if path.exists():
                    print(f"Error: Worktree path still exists after cleanup: {path}")
                else:
                    print(f"Error: Worktree cleanup is incomplete: {path}: {removal.reason}")
                return 1
            return 0
        if command == "check-removal-path":
            path = cast(Path, args.path).absolute()
            descriptors = os.environ.get("BUILD_FOLDER_REMOVAL_FDS")
            if descriptors is None:
                raise ValueError("check-removal-path requires a removal command")
            if descriptors:
                parent_fd, lock_fd = map(int, descriptors.split())
                guard = FolderRemovalGuard(path, parent_fd, (lock_fd,))
            else:
                guard = FolderRemovalGuard(path, None, ())
            guard.check_path()
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
