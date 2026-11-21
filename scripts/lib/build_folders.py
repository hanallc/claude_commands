#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Name temporary build folders and preserve their caller's lifetime."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import json
import os
import errno
import re
import secrets
import signal
import stat
from enum import Enum
from pathlib import Path
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Literal, cast, override

from scripts.lib import build_folder_lock, git_files
from scripts.lib.boot_identity import boot_identity

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"
BUSY = ".claude-busy"
PRIVATE_RUN_ROOT = "CLAUDE_PRIVATE_RUN_ROOT"
SUPERVISED_RUN_ROOT = "CLAUDE_SUPERVISED_RUN_ROOT"
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


def private_run_root() -> Path | None:
    value = os.environ.get(PRIVATE_RUN_ROOT)
    return Path(value) if value else None


def _scoped_temporary_parent(parent: Path) -> Path:
    root = private_run_root()
    if root is not None and not parent.resolve().is_relative_to(root):
        return root
    return parent


def tmpfs_temporary_parent() -> Path:
    """Keep nested work under the caller's tmpfs folder."""
    root = Path(_settings().text("roots.gate"))
    inherited = inherited_temporary_parent().resolve()
    return inherited if inherited.is_relative_to(root) else root


def _parent(parent: BuildFolderParent) -> Path:
    if isinstance(parent, InheritedTemporaryParent):
        return inherited_temporary_parent()
    if isinstance(parent, SystemTemporaryParent):
        return build_folder_lock.system_temporary_parent()
    if isinstance(parent, UnitRunParent):
        from scripts.lib import unit_runs

        return unit_runs.new_run_root()
    return parent.path


def _check_name(name: str) -> None:
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("a build folder name must be one path component")


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
        _check_name(spec.repository)
        return spec.parent / f"{spec.repository}-slot{spec.slot}"
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
    return path


_locks: dict[int, Path] = {}
# A folder's lock file, lease record, or unfinished lease record.
_METADATA = re.compile(r"(.+)\.(?:lock|lease\.json(?:\.[0-9a-f]{16}\.tmp)?)")


def _probe_timing(*, wait: bool = True) -> build_folder_lock.ProbeTiming:
    if not wait:
        return build_folder_lock.ProbeTiming(wait_seconds=0.0, retry_seconds=0.0)
    settings = _settings()
    return build_folder_lock.ProbeTiming(settings.number("locks.probe_wait_seconds"),
                                        settings.number("locks.probe_retry_seconds"))


def _take_lock(descriptor: int, mode: int, *, wait: bool = True) -> bool:
    return build_folder_lock.take_lock(descriptor, mode, _probe_timing(wait=wait))


def build_folder_state(path: Path) -> Literal["held", "free"]:
    """Check the lock with the host configuration."""
    return build_folder_lock.probe(path, path.parent, _probe_timing)


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
    return spec.kind in ("standing", "editable", "mirror", "target", "shell-cache", "legacy-stable", "slot")


FolderCreationNotice = Callable[[Path, os.stat_result], None] | Literal["untracked"]


def create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest, *,
                        on_create: FolderCreationNotice = "untracked") -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    spec = _request_spec(original)
    if not _stable_folder(spec):
        spec = replace(spec, parent=_scoped_temporary_parent(spec.parent))
    with ExitStack() as source_locks:
        descriptors: tuple[int, ...] = ()
        if isinstance(original, LinkedEditableRepository):
            descriptors = source_locks.enter_context(_linked_source_locks(original.source))
        return _create_build_folder(request, spec, descriptors, on_create)


def _create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest,
                         spec: BuildFolderSpec, source_descriptors: tuple[int, ...],
                         on_create: FolderCreationNotice) -> Path:
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
        while True:
            allocation = (replace(spec, work=_new_suffix(spec.suffix_length, _settings()))
                          if spec.kind != "slot" and (spec.showrunner or spec.unit) and not spec.work else spec)
            path = (original.path.expanduser().absolute()
                    if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository))
                    else build_folder_path(allocation))
            try:
                previous_parent = path.parent.stat()
            except FileNotFoundError:
                previous_parent = None
            with ExitStack() as allocation_scope:
                _ = allocation_scope.enter_context(build_folder_lock.require_mutation_lock(path))
                if previous_parent is not None and not os.path.samestat(previous_parent, path.parent.stat()):
                    raise FileNotFoundError(errno.ENOENT, "the allocation parent changed", str(path.parent))
                directory = allocation_scope.enter_context(build_folder_lock.folder_parent(path, spec.parent,
                    create=stable or not isinstance(original, UniqueBuildFolder)))
                descriptor = build_folder_lock.open_lock(path, directory, create=True)
                made_inode: os.stat_result | None = None
                new_folder = False
                new_branch_start = ""
                try:
                    mode = fcntl.LOCK_SH if stable and spec.kind != "slot" and not editable_repository else fcntl.LOCK_EX
                    timing = _probe_timing(wait=spec.kind != "slot")
                    if not build_folder_lock.take_lock(descriptor, mode, timing):
                        if renamed:
                            continue
                        raise BlockingIOError(errno.EWOULDBLOCK, "build folder is held", str(path))
                    if not build_folder_lock.lock_current(path, directory, descriptor):
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
                            if not isinstance(_remove_leased_folder(
                                    path, directory, None, temporary=False, expected=existing,
                                    permissions=RemovalPermissions.REPAIR_OWNED_DIRECTORIES), Removed):
                                raise BlockingIOError(errno.EWOULDBLOCK, "cannot repair an incomplete slot", str(path))
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
                        if made_inode is not None and callable(on_create):
                            on_create(path, made_inode)
                        return path
                    if new_folder and isinstance(spec.slot_checkout, SlotCheckout):
                        import subprocess

                        _ = subprocess.run(["git", "-C", str(spec.slot_checkout.source), "worktree", "add", "--detach",
                                            str(path), spec.slot_checkout.commit], check=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            pass_fds=(descriptor, *held_build_folder_descriptors(), *source_descriptors),
                                           timeout=repository_git_timeout())
                        _write_lease(path, directory, allocation)
                    if made_inode is not None and callable(on_create):
                        on_create(path, made_inode)
                    os.set_inheritable(descriptor, True)
                    _locks[descriptor] = path
                    if isinstance(request, _TemporaryFolderRequest):
                        parent_fd = os.dup(directory)
                        _ = request.cleanup.callback(os.close, parent_fd)
                        folder_fd = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                        _ = request.cleanup.callback(os.close, folder_fd)
                        lease_fd = build_folder_lock.open_lock(path, parent_fd, create=False)
                        _ = request.cleanup.callback(os.close, lease_fd)
                        run_root = os.environ.get(SUPERVISED_RUN_ROOT)
                        supervised = bool(run_root and path.resolve().is_relative_to(run_root))
                        handle = AllocatedFolder(path, build_folder_lock.OpenLock.read(parent_fd),
                                                 folder_fd, lease_fd, os.getpid(), supervised)
                        _ = request.cleanup.callback(_finish_temporary, handle, descriptor=descriptor,
                                                     temporary=not stable,
                                                     timing=_probe_timing() if spec.kind == "slot" else timing)
                    return path
                except BaseException:
                    _ = _locks.pop(descriptor, None)
                    if (made_inode is not None or (new_folder and not stable)) and _take_lock(descriptor, fcntl.LOCK_EX):
                        cleanup = None if made_inode is not None else lambda: None
                        permissions = (RemovalPermissions.PRESERVE if made_inode is None else
                                       RemovalPermissions.REPAIR_OWNED_DIRECTORIES)
                        _ = _remove_leased_folder(path, directory, cleanup, temporary=True, expected=made_inode,
                                                  permissions=permissions)
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


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class AllocatedFolder:
    """The scope owner retains the allocated inode and its parent."""

    path: Path
    parent: build_folder_lock.OpenLock
    folder: int
    lease: int
    owner_pid: int
    supervised: bool


def _named_folder_matches(parent: int, name: str, folder: int) -> bool:
    try:
        return os.path.samestat(os.fstat(folder), os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        return False


def _repair_directory(descriptor: int, *, path_descriptor: bool = False) -> None:
    info = os.fstat(descriptor)
    required = stat.S_IRWXU
    if info.st_uid == os.geteuid() and info.st_mode & required != required:
        mode = stat.S_IMODE(info.st_mode) | required
        if path_descriptor:
            os.chmod(f"/proc/self/fd/{descriptor}", mode)
        else:
            os.fchmod(descriptor, mode)


@contextmanager
def _open_folder(parent: int, name: str, *, repair: bool) -> Generator[int]:
    with ExitStack() as stack:
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        except PermissionError:
            if not repair:
                raise
            path_flag = cast(int, getattr(os, "O_PATH", 0))
            open_flag = path_flag or cast(int, getattr(os, "O_EVTONLY", 0))
            if not open_flag:
                raise
            repair_fd = os.open(name, open_flag | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent)
            _ = stack.callback(os.close, repair_fd)
            if not _named_folder_matches(parent, name, repair_fd):
                raise FileNotFoundError(name)
            _repair_directory(repair_fd, path_descriptor=bool(path_flag))
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        _ = stack.callback(os.close, descriptor)
        if not _named_folder_matches(parent, name, descriptor):
            raise FileNotFoundError(name)
        yield descriptor


def _remove_folder(folder: int, *, repair: bool) -> None:
    """Remove contents relative to the verified folder."""
    if repair:
        _repair_directory(folder)
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                with _open_folder(folder, entry.name, repair=repair) as child:
                    _remove_folder(child, repair=repair)
                    if not _named_folder_matches(folder, entry.name, child):
                        raise FileNotFoundError(entry.name)
                    os.rmdir(entry.name, dir_fd=folder)
            else:
                os.unlink(entry.name, dir_fd=folder)


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
class Removed:
    """The original name and required metadata are absent."""


@dataclass(frozen=True)
class BlockedByLiveLease:
    path: Path


@dataclass(frozen=True)
class FolderChanged:
    path: Path


@dataclass(frozen=True)
class UnsafeChild:
    path: Path
    reason: str


RemovalRefusal = BlockedByLiveLease | FolderChanged | UnsafeChild
RemovalAttempt = Removed | RemovalRefusal


class RemovalPermissions(Enum):
    PRESERVE = "preserve directory permissions"
    REPAIR_OWNED_DIRECTORIES = "restore access to directories owned by this user"


@dataclass(frozen=True)
class RefusedRemovalLease:
    """The lock or namespace prevents removal."""

    reason: RemovalRefusal


@dataclass(frozen=True)
class UnreservedRemovalFolder:
    """The folder has no lock file to hold."""


SidecarIdentity = tuple[int, int, int]


@dataclass(frozen=True)
class HeldFolderRemovalLease:
    """The descriptor reserves the folder name until lock removal."""

    folder: Path
    directory: int
    descriptor: int

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

_INCOMPLETE_CLEANUP_MARKER = b"foreground cleanup incomplete\n"


def mark_cleanup_incomplete(descriptor: int) -> None:
    """Mark the lock before the supervisor releases it."""
    _ = os.pwrite(descriptor, _INCOMPLETE_CLEANUP_MARKER, 0)


class ForegroundMarker(Enum):
    """Reserve a byte for each level of foreground supervision."""

    COMMAND = len(_INCOMPLETE_CLEANUP_MARKER)
    COORDINATED_COMMAND = len(_INCOMPLETE_CLEANUP_MARKER) + 1

    def begin(self, descriptor: int) -> None:
        if cleanup_incomplete(descriptor, active=self):
            raise BlockingIOError(errno.EBUSY, "foreground cleanup is incomplete")
        self._write(descriptor, b"1")

    def finish(self, descriptor: int) -> None:
        self._write(descriptor, b"\0")

    def _write(self, descriptor: int, value: bytes) -> None:
        if os.pwrite(descriptor, value, self.value) != len(value):
            raise OSError(errno.EIO, "could not write foreground cleanup state")
        os.fsync(descriptor)


def cleanup_incomplete(descriptor: int, *, active: ForegroundMarker = ForegroundMarker.COMMAND) -> bool:
    """Read the marker while the caller holds the cleanup lock."""
    marker = os.pread(descriptor, len(_INCOMPLETE_CLEANUP_MARKER), 0)
    if marker and _INCOMPLETE_CLEANUP_MARKER.startswith(marker):
        return True
    for slot in ForegroundMarker:
        if slot is ForegroundMarker.COMMAND and active is ForegroundMarker.COORDINATED_COMMAND:
            continue
        if os.pread(descriptor, 1, slot.value) == b"1":
            return True
    return False


class _RemovalLockState(Enum):
    ACQUIRED = "the lease permits removal"
    HELD = "another holder has the lease"
    REPLACED = "the lock no longer names this lease"
    UNSAFE = "the lease cannot be checked or cleanup is incomplete"


RemovalProbe = bool | build_folder_lock.ProbeTiming


def _take_removal_lock(path: Path, directory: int, descriptor: int, *, wait: RemovalProbe) -> _RemovalLockState:
    timing = _probe_timing(wait=wait) if isinstance(wait, bool) else wait
    if not build_folder_lock.take_lock(descriptor, fcntl.LOCK_EX, timing):
        if not build_folder_lock.lock_current(path, directory, descriptor):
            return _RemovalLockState.REPLACED
        return _RemovalLockState.HELD
    if not build_folder_lock.lock_current(path, directory, descriptor):
        return _RemovalLockState.REPLACED
    if cleanup_incomplete(descriptor):
        return _RemovalLockState.UNSAFE
    return _RemovalLockState.ACQUIRED


@dataclass(frozen=True)
class _PendingRemoval(BlockedByLiveLease):
    """The cleanup owner holds a descriptor for the blocking lease."""

    descriptor: int

    def join(self) -> None:
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)


def _hold_free_lease(path: Path, directory: int, stack: ExitStack, *, wait: RemovalProbe = False,
                     create: bool = False, unpaired: bool = False, pending: ExitStack | None = None,
                     held_locks: Mapping[tuple[int, int], int] | None = None) -> FolderRemovalLease:
    """Hold a free lease until the stack closes."""
    try:
        held = None
        if held_locks:
            info = os.stat(path.name + ".lock", dir_fd=directory, follow_symlinks=False)
            held = held_locks.get((info.st_dev, info.st_ino))
        if held is not None:
            descriptor = os.dup(held)
        elif unpaired:
            descriptor = os.open(path.name + ".lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        else:
            descriptor = build_folder_lock.open_lock(path, directory, create=create)
    except FileNotFoundError:
        return UnreservedRemovalFolder()
    except (PermissionError, ValueError) as error:
        return RefusedRemovalLease(UnsafeChild(path, str(error)))
    except OSError as error:
        if unpaired and error.errno == errno.ELOOP:
            return UnreservedRemovalFolder()
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            return RefusedRemovalLease(UnsafeChild(path, str(error)))
        raise
    _ = stack.callback(os.close, descriptor)
    if unpaired or held is not None:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return (UnreservedRemovalFolder() if unpaired else
                    RefusedRemovalLease(UnsafeChild(path, "the lock is not a regular file")))
        if info.st_nlink > 1:
            return RefusedRemovalLease(UnsafeChild(path, "the lock has more than one link"))
    return _reserve_removal_lease(path, directory, descriptor, stack, wait=wait, pending=pending)


def _reserve_removal_lease(path: Path, directory: int, descriptor: int, stack: ExitStack, *,
                           wait: RemovalProbe = False, pending: ExitStack | None = None) -> FolderRemovalLease:
    state = _take_removal_lock(path, directory, descriptor, wait=wait)
    if state is _RemovalLockState.HELD:
        if pending is None:
            return RefusedRemovalLease(BlockedByLiveLease(path))
        blocking = os.dup(descriptor)
        _ = pending.callback(os.close, blocking)
        return RefusedRemovalLease(_PendingRemoval(path, blocking))
    if state is _RemovalLockState.REPLACED:
        return RefusedRemovalLease(FolderChanged(path))
    if state is _RemovalLockState.UNSAFE:
        return RefusedRemovalLease(UnsafeChild(path, state.value))
    _ = stack.callback(_locks.pop, descriptor, None)
    _locks[descriptor] = path
    return HeldFolderRemovalLease(path, directory, descriptor)


@dataclass(frozen=True)
class DescendantLeasesHeld:
    """The scan checked all descendant leases and keeps them in the caller's stack."""


DescendantLeaseScan = DescendantLeasesHeld | RemovalRefusal


def _hold_child_leases(path: Path, directory: int, stack: ExitStack, *, repair: bool,
                       pending: ExitStack | None = None,
                       held_locks: Mapping[tuple[int, int], int] | None = None) -> DescendantLeaseScan:
    """Check descendant leases relative to the verified folder without waiting."""
    try:
        if repair:
            _repair_directory(directory)
        with os.scandir(directory) as contents:
            entries = {entry.name: entry.is_dir(follow_symlinks=False) for entry in contents}
        for name, is_directory in entries.items():
            stem = name.removesuffix(".lock")
            if stem and stem != name:
                unpaired = not entries.get(stem, False) and stem + ".lease.json" not in entries
                if is_directory and unpaired:
                    continue
                result = _hold_free_lease(path / stem, directory, stack, unpaired=unpaired, pending=pending,
                                          held_locks=held_locks)
                if isinstance(result, RefusedRemovalLease):
                    return result.reason
        for name, is_directory in entries.items():
            if is_directory:
                with _open_folder(directory, name, repair=repair) as child:
                    result = _hold_child_leases(path / name, child, stack, repair=repair, pending=pending,
                                                held_locks=held_locks)
                    if not isinstance(result, DescendantLeasesHeld):
                        return result
    except FileNotFoundError:
        return FolderChanged(path)
    except (PermissionError, NotADirectoryError) as error:
        return UnsafeChild(path, str(error))
    return DescendantLeasesHeld()


def _scan_folder(path: Path, directory: int, stack: ExitStack) -> DescendantLeaseScan:
    try:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            return DescendantLeasesHeld()
        with _open_folder(directory, path.name, repair=False) as folder:
            return _hold_child_leases(path, folder, stack, repair=False)
    except FileNotFoundError:
        return FolderChanged(path) if _folder_exists(path, directory) else DescendantLeasesHeld()
    except (PermissionError, NotADirectoryError) as error:
        return UnsafeChild(path, str(error))


@contextmanager
def hold_descendant_leases(path: Path) -> Generator[DescendantLeaseScan]:
    """Hold the descendant leases of a folder whose root lease is already held."""
    with ExitStack() as stack:
        namespace = stack.enter_context(build_folder_lock.mutation_lock(path))
        if isinstance(namespace, build_folder_lock.ParentChanged):
            scan: DescendantLeaseScan = FolderChanged(namespace.path)
        else:
            try:
                parent = stack.enter_context(build_folder_lock.folder_parent(path, path.parent, create=False))
            except FileNotFoundError:
                scan = FolderChanged(path)
            else:
                scan = _scan_folder(path, parent, stack)
        if not isinstance(scan, DescendantLeasesHeld):
            stack.close()
        yield scan


@dataclass(frozen=True)
class CargoOutputAbsent:
    """The Cargo directory was absent during reservation."""

    def remove(self) -> Removed:
        return Removed()


@dataclass(frozen=True)
class ReservedCargoOutput:
    """The reservation keeps the Cargo namespace and all of its leases locked."""

    path: Path
    parent: build_folder_lock.OpenLock
    folder: int

    def remove(self) -> RemovalAttempt:
        try:
            self.parent.identity.require_name(self.path.parent.resolve())
        except FileNotFoundError:
            return FolderChanged(self.path)
        return _remove_locked_tree(self.path, self.parent.descriptor, self.folder, repair=False)


def _reserve_cargo_output(path: Path, stack: ExitStack, alongside: Sequence[Path],
                          held_locks: Sequence[int],
                          ) -> ReservedCargoOutput | CargoOutputAbsent | RemovalRefusal:
    try:
        namespace = stack.enter_context(build_folder_lock.mutation_lock(
            path, *alongside, wait=False, keep_shared_parent=not alongside))
        if isinstance(namespace, build_folder_lock.ParentChanged):
            return FolderChanged(namespace.path)
        try:
            parent = stack.enter_context(build_folder_lock.folder_parent(path, path.parent, create=False))
        except FileNotFoundError:
            return CargoOutputAbsent()
        parent_lock = build_folder_lock.OpenLock.read(parent)
        parent_lock.identity.require_name(path.parent.resolve())
        lease = _hold_free_lease(path, parent, stack)
        if isinstance(lease, RefusedRemovalLease):
            return lease.reason
        if not _folder_exists(path, parent):
            return CargoOutputAbsent()
        folder = stack.enter_context(_open_folder(parent, path.name, repair=False))
        known_locks: dict[tuple[int, int], int] = {}
        for descriptor in held_locks:
            info = os.fstat(descriptor)
            known_locks[info.st_dev, info.st_ino] = descriptor
        scan = _hold_child_leases(path, folder, stack, repair=False, held_locks=known_locks)
        return ReservedCargoOutput(path, parent_lock, folder) if isinstance(scan, DescendantLeasesHeld) else scan
    except FileNotFoundError:
        return FolderChanged(path)
    except (build_folder_lock.MutationLockBusy, PermissionError, NotADirectoryError, ValueError) as error:
        return UnsafeChild(path, str(error))


@contextmanager
def reserve_cargo_output(path: Path, *, alongside: Sequence[Path] = (), held_locks: Sequence[int] = (),
                         ) -> Generator[ReservedCargoOutput | CargoOutputAbsent | RemovalRefusal]:
    """Hold the Cargo root and descendant leases until removal ends."""
    with ExitStack() as stack:
        reservation = _reserve_cargo_output(path, stack, alongside, held_locks)
        if isinstance(reservation, (BlockedByLiveLease, FolderChanged, UnsafeChild)):
            stack.close()
        yield reservation


@dataclass(frozen=True)
class LeaseProbePassed:
    """The operation can check the lease under the namespace lock."""


def _probe_removal_lease(path: Path, wait: RemovalProbe, *, handle: AllocatedFolder | None = None,
                         pending: ExitStack | None = None) -> LeaseProbePassed | RefusedRemovalLease:
    """Apply the saved wait before taking the mutation lock."""
    if wait is False:
        return LeaseProbePassed()
    with ExitStack() as probe:
        if handle is not None:
            _ = probe.callback(fcntl.flock, handle.lease, fcntl.LOCK_UN)
            result = _reserve_removal_lease(path, handle.parent.descriptor, handle.lease, probe, wait=wait, pending=pending)
        else:
            try:
                parent = probe.enter_context(build_folder_lock.folder_parent(path, path.parent, create=False))
            except FileNotFoundError:
                return LeaseProbePassed()
            result = _hold_free_lease(path, parent, probe, wait=wait)
        return result if isinstance(result, RefusedRemovalLease) else LeaseProbePassed()


@contextmanager
def folder_removal_lease(path: Path, *, children: bool = False, wait: bool = True,
                         contents_only: bool = False) -> Generator[FolderRemovalLease]:
    """Hold the collector's namespace and lease locks through removal."""
    metadata = _METADATA.fullmatch(path.name)
    folder = path.with_name(metadata[1]) if metadata else path
    probe = _probe_removal_lease(folder, wait)
    if isinstance(probe, RefusedRemovalLease):
        yield probe
        return
    with ExitStack() as stack:
        try:
            namespace = stack.enter_context(build_folder_lock.mutation_lock(
                folder, wait=wait, shared_parent_only=contents_only))
            if isinstance(namespace, build_folder_lock.ParentChanged):
                lease: FolderRemovalLease = RefusedRemovalLease(FolderChanged(namespace.path))
            else:
                directory = stack.enter_context(build_folder_lock.folder_parent(folder, folder.parent, create=False))
                lease = _hold_free_lease(folder, directory, stack)
                if not isinstance(lease, RefusedRemovalLease):
                    if metadata is not None:
                        lease = UnreservedRemovalFolder()
                    elif children:
                        if not os.path.samestat(os.fstat(directory), folder.parent.stat()):
                            lease = RefusedRemovalLease(FolderChanged(folder))
                        else:
                            result = _scan_folder(folder, directory, stack)
                            if not isinstance(result, DescendantLeasesHeld):
                                lease = RefusedRemovalLease(result)
        except FileNotFoundError:
            lease = UnreservedRemovalFolder()
        except (build_folder_lock.MutationLockBusy, PermissionError, NotADirectoryError, ValueError) as error:
            lease = RefusedRemovalLease(UnsafeChild(folder, str(error)))
        if isinstance(lease, RefusedRemovalLease):
            stack.close()
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
def leased_build_folder(spec: BuildFolderSpec | UniqueBuildFolder, *,
                        on_create: FolderCreationNotice = "untracked") -> Generator[Path]:
    """Hold the maker lease while a persistent folder is in use."""
    path = create_build_folder(spec, on_create=on_create)
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
            with build_folder_lock.folder_parent(path, spec.parent, create=False) as directory:
                _write_lease(path, directory, spec, released=True)
        finally:
            _release_build_folder(path)


def report_removal_refusal(result: RemovalRefusal) -> None:
    print(f"build folders: {removal_refusal_detail(result)}", file=sys.stderr)


def removal_refusal_detail(result: RemovalRefusal) -> str:
    reason = (result.reason if isinstance(result, UnsafeChild) else
              "its lease is held" if isinstance(result, BlockedByLiveLease) else "its identity changed")
    return f"kept {result.path} because {reason}"


def attempt_folder_removal(path: Path, cleanup: Callable[[], object] | None = None, *, temporary: bool = True,
                           retained_parent: build_folder_lock.OpenLock | None = None, wait: RemovalProbe = True,
                           handle: AllocatedFolder | None = None,
                           expected: os.stat_result | None = None,
                           permissions: RemovalPermissions = RemovalPermissions.PRESERVE,
                           pending: ExitStack | None = None) -> RemovalAttempt:
    """Remove only the verified folder under its namespace lock."""
    probe = _probe_removal_lease(path, wait, handle=handle, pending=pending)
    if isinstance(probe, RefusedRemovalLease):
        return probe.reason
    parent = handle.parent if handle is not None else retained_parent
    with ExitStack() as locks:
        try:
            namespace = locks.enter_context(build_folder_lock.mutation_lock(
                path, wait=wait is not False, retained_parent=parent))
        except build_folder_lock.MutationLockBusy as error:
            return UnsafeChild(path, str(error))
        if isinstance(namespace, build_folder_lock.ParentChanged):
            return FolderChanged(namespace.path)
        try:
            if parent is None:
                directory = locks.enter_context(build_folder_lock.folder_parent(path, path.parent, create=False))
            else:
                directory = parent.descriptor
        except FileNotFoundError:
            return FolderChanged(path) if parent is not None else _finish_removal(path, cleanup)
        if handle is None:
            try:
                checked = build_folder_lock.OpenLock.read(directory) if parent is None else parent
                checked.identity.require_name(path.parent.resolve())
            except OSError:
                return FolderChanged(path)
        if handle is not None and _folder_exists(path, directory):
            if not _named_folder_matches(directory, path.name, handle.folder):
                return FolderChanged(path)
        if expected is not None:
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return Removed()
            if not os.path.samestat(current, expected):
                return FolderChanged(path)
        if handle is not None:
            _ = locks.callback(fcntl.flock, handle.lease, fcntl.LOCK_UN)
            lease = _reserve_removal_lease(path, directory, handle.lease, locks, pending=pending)
        else:
            lease = _hold_free_lease(path, directory, locks, create=True)
        if isinstance(lease, RefusedRemovalLease):
            return lease.reason
        if isinstance(lease, UnreservedRemovalFolder):
            return FolderChanged(path)
        return _remove_leased_folder(path, directory, cleanup, temporary=temporary, handle=handle,
                                    expected=expected, permissions=permissions, pending=pending)


def _finish_removal(path: Path, cleanup: Callable[[], object] | None, *,
                    directory: int = -1, temporary: bool = True) -> RemovalAttempt:
    if cleanup is not None:
        try:
            if cleanup() is False:
                return UnsafeChild(path, "the cleanup callback refused removal")
        except FileNotFoundError:
            if temporary and directory >= 0 and not _folder_exists(path, directory):
                _unlink_metadata(path, directory)
            raise
    if directory < 0:
        try:
            _ = path.lstat()
        except FileNotFoundError:
            return Removed()
        return FolderChanged(path)
    if _folder_exists(path, directory):
        return FolderChanged(path)
    if temporary:
        _unlink_metadata(path, directory)
    return Removed()


def _remove_locked_tree(path: Path, directory: int, folder: int, *, repair: bool) -> RemovalAttempt:
    try:
        if not _named_folder_matches(directory, path.name, folder):
            return FolderChanged(path)
        _remove_folder(folder, repair=repair)
        if not _named_folder_matches(directory, path.name, folder):
            return FolderChanged(path)
        os.rmdir(path.name, dir_fd=directory)
    except FileNotFoundError:
        return FolderChanged(path)
    except (PermissionError, NotADirectoryError) as error:
        return UnsafeChild(path, str(error))
    return Removed()


def _remove_leased_folder(path: Path, directory: int, cleanup: Callable[[], object] | None, *,
                          temporary: bool, permissions: RemovalPermissions, handle: AllocatedFolder | None = None,
                          expected: os.stat_result | None = None,
                          pending: ExitStack | None = None) -> RemovalAttempt:
    repair = permissions is RemovalPermissions.REPAIR_OWNED_DIRECTORIES
    with ExitStack() as children:
        if _folder_exists(path, directory):
            try:
                folder = (handle.folder if handle is not None else
                          children.enter_context(_open_folder(directory, path.name, repair=repair)))
                if (not _named_folder_matches(directory, path.name, folder)
                        or expected is not None and not os.path.samestat(expected, os.fstat(folder))):
                    return FolderChanged(path)
                scan = _hold_child_leases(path, folder, children, repair=repair, pending=pending)
                if not isinstance(scan, DescendantLeasesHeld):
                    return scan
                if cleanup is None:
                    result = _remove_locked_tree(path, directory, folder, repair=repair)
                    if not isinstance(result, Removed):
                        return result
            except FileNotFoundError:
                return FolderChanged(path)
            except (PermissionError, NotADirectoryError) as error:
                return UnsafeChild(path, str(error))
        return _finish_removal(path, cleanup, directory=directory, temporary=temporary)


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
    with ExitStack() as stack:
        try:
            namespace = stack.enter_context(build_folder_lock.mutation_lock(path, wait=False))
            if isinstance(namespace, build_folder_lock.ParentChanged):
                return FolderPairRelease.LOCK_HELD
            directory = stack.enter_context(build_folder_lock.folder_parent(path, Path(path.anchor), create=False))
        except build_folder_lock.MutationLockBusy:
            return FolderPairRelease.LOCK_HELD
        except FileNotFoundError:
            return FolderPairRelease.PARENT_ABSENT
        if _folder_exists(path, directory):
            return FolderPairRelease.FOLDER_PRESENT
        lock_name = path.name + ".lock"
        names = (path.name + ".lease.json", lock_name)
        present = {name for name in names if _folder_exists(path.with_name(name), directory)}
        if not present:
            return FolderPairRelease.PAIR_ABSENT
        lease = _hold_free_lease(path, directory, stack, wait=False, create=True)
        if not isinstance(lease, HeldFolderRemovalLease):
            return FolderPairRelease.LOCK_HELD
        removed = lease.unlink_matching_sidecars(lease.sidecar_identities(), reject_parent_links=True)
        if present <= removed and lock_name in removed:
            return FolderPairRelease.RELEASED
        if removed:
            return FolderPairRelease.PARTIAL
        if _folder_exists(path, directory):
            return FolderPairRelease.FOLDER_PRESENT
        return FolderPairRelease.PAIR_KEPT


def _finish_temporary(handle: AllocatedFolder, *, descriptor: int, temporary: bool,
                      timing: build_folder_lock.ProbeTiming) -> None:
    if _locks.get(descriptor) != handle.path:
        return
    _release_build_folder(handle.path, descriptor=descriptor)
    if os.getpid() != handle.owner_pid:
        return
    if handle.path in _locks.values():
        return
    while True:
        with ExitStack() as pending:
            result = attempt_folder_removal(handle.path, temporary=temporary, wait=timing,
                                            handle=handle, permissions=RemovalPermissions.REPAIR_OWNED_DIRECTORIES,
                                            pending=pending)
            if not isinstance(result, _PendingRemoval):
                break
            if handle.supervised:
                # The supervisor keeps the tree until it reaps the holder.
                return
            result.join()
    if not isinstance(result, Removed):
        report_removal_refusal(result)


def _remove_free(path: Path, parent: build_folder_lock.OpenLock, *, wait: bool) -> RemovalAttempt:
    """Remove a temporary allocation under its leases."""
    result = attempt_folder_removal(path, retained_parent=parent, wait=wait)
    if not isinstance(result, Removed):
        report_removal_refusal(result)
    return result


def sweep_temporary_build_folders(parent: Path, *, wait: bool = True) -> None:
    """Remove free allocations in a parent reserved for temporary maker folders.
    With wait False, a held folder costs one lock try, not the probe wait."""
    try:
        with build_folder_lock.folder_parent(parent / "entry", parent, create=False) as directory:
            parent_lock = build_folder_lock.OpenLock.read(directory)
            names: set[str] = set()
            for entry in os.listdir(directory):
                match = _METADATA.fullmatch(entry)
                if match is not None:
                    names.add(match[1])
            for name in sorted(names):
                _ = _remove_free(parent / name, parent_lock, wait=wait)
    except FileNotFoundError:
        pass


@contextmanager
def temporary_build_folder(request: UniqueBuildFolder | BuildFolderSpec) -> Generator[Path]:
    """Remove private work after the owner finishes its lease holders."""
    with ExitStack() as cleanup:
        yield create_build_folder(_TemporaryFolderRequest(request, cleanup))


def publish_build_folder(source: Path, destination: Path) -> None:
    """Move a folder while both names are locked."""
    with build_folder_lock.require_mutation_lock(source, destination):
        os.replace(source, destination)


def _claim_tool_folder(tool: str, parent: Path | None, allocate: Callable[[BuildFolderSpec], Path], *,
                       owner_pid: int = 0) -> Path:
    """Allocate <parent>/<tool>, or <tool>-2 and up while another run holds a name.

    A folder whose lock is free and whose lease names an ended process is a stopped run's leftover:
    it is removed and its name reused. Any other folder is passed over.
    """
    root = _scoped_temporary_parent(parent if parent is not None else inherited_temporary_parent())
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
            with build_folder_lock.folder_parent(path, root, create=False) as directory:
                leftover = _lease_owner_ended(path, directory) and isinstance(
                    _remove_free(path, build_folder_lock.OpenLock.read(directory), wait=False), Removed)
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
        yield _claim_tool_folder(tool, parent, lambda spec: cleanup.enter_context(
            temporary_build_folder(spec)))


OWNER_MARKER = ".build-folder-owner.json"


def run_build_command(command: Sequence[str], *, path: Path, environment: Mapping[str, str],
                      pass_fds: Sequence[int]) -> int:
    """Keep the caller until the command and its descendants stop."""
    import subprocess
    from scripts.lib.descendants import adopt_descendants

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
            from scripts.direct.group_cleanup import group_has_live_members, stop_group

            # A descendant outside the group still holds the slot if it inherits the lock descriptor.
            _ = stop_group(child.pid, signal.SIGKILL)
            while group_has_live_members(child.pid):
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
            _release_build_folder(path)
            result = attempt_folder_removal(path)
            if isinstance(result, Removed):
                return 0
            report_removal_refusal(result)
            return 1
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
