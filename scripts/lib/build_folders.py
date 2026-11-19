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
import shutil
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
    settings = _settings()
    return build_folder_lock.ProbeTiming(settings.number("locks.probe_wait_seconds") if wait else 0.0,
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
                    directory = parents.enter_context(build_folder_lock.folder_parent(path, spec.parent,
                        create=stable or not isinstance(original, UniqueBuildFolder)))
                    directories[path.parent] = directory
                descriptor = build_folder_lock.open_lock(path, directory, create=True)
                made_inode: os.stat_result | None = None
                new_folder = False
                new_branch_start = ""
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_SH if stable and spec.kind != "slot" and not editable_repository else fcntl.LOCK_EX,
                                      wait=spec.kind != "slot"):
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
                        cleanup = None if made_inode is not None else lambda: None
                        _ = _remove_leased_folder(path, directory, cleanup, temporary=True, allocated=made_inode)
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


def _parent_has_symlink(parent: Path) -> bool:
    absolute = parent.absolute()
    # A link before '..' can lead to the same parent as the absolute path.
    return (Path(os.path.abspath(absolute)) != absolute.resolve()
            or any(component.is_symlink() for component in (absolute, *absolute.parents)))


@dataclass(frozen=True)
class RefusedRemovalLease:
    """A busy or unreadable lock prevents removal."""


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


def _take_removal_lock(path: Path, directory: int, descriptor: int, *,
                       wait: bool | build_folder_lock.ProbeTiming) -> bool:
    timing = _probe_timing(wait=wait) if isinstance(wait, bool) else wait
    return (build_folder_lock.take_lock(descriptor, fcntl.LOCK_EX, timing)
            and build_folder_lock.lock_current(path, directory, descriptor)
            and not cleanup_incomplete(descriptor))


def _hold_free_lease(path: Path, directory: int, stack: ExitStack, *, wait: bool = True,
                     create: bool = False) -> FolderRemovalLease:
    """Hold the lock if it is free. The stack owns the descriptor."""
    try:
        descriptor = build_folder_lock.open_lock(path, directory, create=create)
    except FileNotFoundError:
        return UnreservedRemovalFolder()
    _ = stack.callback(os.close, descriptor)
    if not _take_removal_lock(path, directory, descriptor, wait=wait):
        return RefusedRemovalLease()
    _ = stack.callback(_locks.pop, descriptor, None)
    _locks[descriptor] = path
    return HeldFolderRemovalLease(path, directory, descriptor)


def _hold_child_leases(path: Path, stack: ExitStack, *, dir_fd: int | None = None,
                       wait: bool = True, repair_permissions: bool = False) -> bool:
    """Hold the child leases without following links."""
    def unreadable(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    root = path.name if dir_fd is not None else str(path)
    try:
        info = os.stat(root, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if repair_permissions:
        _make_directory_readable(root, dir_fd, info)
    for top, folders, files, directory in os.fwalk(root, follow_symlinks=False, onerror=unreadable, dir_fd=dir_fd):
        if repair_permissions:
            for folder in folders:
                try:
                    info = os.stat(folder, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                _make_directory_readable(folder, directory, info)
        names = set(folders) | set(files)
        for name in files:
            stem = name.removesuffix(".lock")
            if stem == name:
                continue
            child = (path.parent / top if dir_fd is not None else Path(top)) / stem
            if stem not in folders and stem + ".lease.json" not in names:
                # An interrupted removal can leave a marker beside an absent folder.
                try:
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    if error.errno == errno.ELOOP:
                        continue
                    raise
                try:
                    if stat.S_ISREG(os.fstat(descriptor).st_mode) and cleanup_incomplete(descriptor):
                        return False
                finally:
                    os.close(descriptor)
                continue
            if isinstance(_hold_free_lease(child, directory, stack, wait=wait), RefusedRemovalLease):
                return False
    return True


def _make_directory_readable(name: str, directory: int | None, info: os.stat_result) -> None:
    required = stat.S_IRUSR | stat.S_IXUSR
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and info.st_mode & required != required:
        os.chmod(name, stat.S_IMODE(info.st_mode) | required, dir_fd=directory, follow_symlinks=False)


@contextmanager
def folder_removal_lease(path: Path, *, children: bool = False, wait: bool = True) -> Generator[FolderRemovalLease]:
    """Hold the collector's locks and return the reservation for a folder."""
    metadata = _METADATA.fullmatch(path.name)
    folder = path.with_name(metadata[1]) if metadata else path
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(build_folder_lock.folder_parent(folder, folder.parent, create=False))
            lease = _hold_free_lease(folder, directory, stack, wait=wait)
            if not isinstance(lease, RefusedRemovalLease):
                if metadata is not None:
                    lease = UnreservedRemovalFolder()
                elif children:
                    with suppress(FileNotFoundError):
                        if not _hold_child_leases(folder, stack):
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
            with build_folder_lock.folder_parent(path, spec.parent, create=False) as directory:
                _write_lease(path, directory, spec, released=True)
        finally:
            _release_build_folder(path)


def remove_build_folder(path: Path, cleanup: Callable[[], object] | None = None, *, temporary: bool = True,
                        dir_fd: int | None = None, wait: bool | build_folder_lock.ProbeTiming = True,
                        allocated: os.stat_result | None = None) -> bool:
    """Remove a folder only while its lease and all child leases are free."""
    with ExitStack() as locks:
        try:
            directory = dir_fd if dir_fd is not None else locks.enter_context(
                build_folder_lock.folder_parent(path, path.parent, create=False))
        except FileNotFoundError:
            return cleanup is None or cleanup() is not False
        try:
            descriptor = build_folder_lock.open_lock(path, directory, create=True)
            _ = locks.callback(os.close, descriptor)
            free = _take_removal_lock(path, directory, descriptor, wait=wait)
        except (OSError, ValueError):
            free = False
        if not free:
            print(f"build folders: kept {path}; its lease is held or incomplete", file=sys.stderr)
            return False
        return _remove_leased_folder(path, directory, cleanup, temporary=temporary, allocated=allocated)


def _remove_leased_folder(path: Path, directory: int, cleanup: Callable[[], object] | None, *,
                          temporary: bool, allocated: os.stat_result | None = None) -> bool:
    with ExitStack() as children:
        current = None
        try:
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if allocated is not None and current is not None and not os.path.samestat(allocated, current):
                return False
            free = _hold_child_leases(path, children, dir_fd=directory, wait=False,
                                      repair_permissions=allocated is not None)
        except (OSError, ValueError):
            free = False
        if not free:
            print(f"build folders: kept {path}; a child lease is held or incomplete", file=sys.stderr)
            return False
        if cleanup is None:
            if current is not None:
                _remove_folder(path, directory, current)
        elif cleanup() is False:
            return False
        if _folder_exists(path, directory):
            return False
        if temporary:
            _unlink_metadata(path, directory)
        return True


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
            directory = stack.enter_context(build_folder_lock.folder_parent(path, Path(path.anchor), create=False))
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


def _finish_temporary(path: Path, directory: int, allocated: os.stat_result, *,
                      descriptor: int, temporary: bool) -> None:
    if _locks.get(descriptor) != path:
        return
    _release_build_folder(path, descriptor=descriptor)
    _ = remove_build_folder(path, temporary=temporary, dir_fd=directory, allocated=allocated)


def _remove_free(path: Path, directory: int, *, wait: bool) -> bool:
    """Remove a temporary allocation under its leases."""
    return remove_build_folder(path, dir_fd=directory, wait=wait)


def sweep_temporary_build_folders(parent: Path, *, wait: bool = True) -> None:
    """Remove free allocations in a parent reserved for temporary maker folders.
    With wait False, a held folder costs one lock try, not the probe wait."""
    try:
        with build_folder_lock.folder_parent(parent / "entry", parent, create=False) as directory:
            names: set[str] = set()
            for entry in os.listdir(directory):
                match = _METADATA.fullmatch(entry)
                if match is not None:
                    names.add(match[1])
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
            with build_folder_lock.folder_parent(path, root, create=False) as directory:
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
            return 0 if remove_build_folder(path) else 1
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
