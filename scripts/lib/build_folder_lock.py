"""Check the exact lock inode for a build folder."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import errno
import fcntl
import os
from pathlib import Path
import stat
import sys
from threading import local
import time
from typing import Literal, cast


class MutationLockBusy(BlockingIOError):
    """Another operation holds the namespace lock."""


@dataclass(frozen=True)
class MutationLockHeld:
    """The operation holds the namespace locks for the requested paths."""


@dataclass(frozen=True)
class ParentChanged:
    """A parent lost its name or identity during lock acquisition."""

    path: Path


@dataclass(frozen=True)
class FileIdentity:
    """Identify a filesystem object by its device and inode."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> FileIdentity:
        return cls(info.st_dev, info.st_ino)

    def matches_name(self, path: Path) -> bool:
        return self == self.from_stat(path.lstat())

    def require_name(self, path: Path) -> None:
        """Refuse a missing name or a name that has a different inode."""
        if not self.matches_name(path):
            raise FileNotFoundError(errno.ENOENT, "the mutation parent changed", str(path))


@dataclass(frozen=True)
class OpenLock:
    """Keep the identity selected on an open lock descriptor."""

    descriptor: int
    identity: FileIdentity

    @classmethod
    def read(cls, descriptor: int) -> OpenLock:
        return cls(descriptor, FileIdentity.from_stat(os.fstat(descriptor)))


@dataclass(frozen=True)
class _MutationCoverage:
    """The current thread holds shared ancestors and exclusive mutation parents."""

    owner: int
    shared: dict[Path, OpenLock]
    exclusive: dict[Path, OpenLock]
    guarded_ancestors: dict[Path, FileIdentity]


class _MutationScope(local):
    coverage: _MutationCoverage = _MutationCoverage(0, {}, {}, {})


_mutation_scope = _MutationScope()


def system_temporary_parent() -> Path:
    from scripts.lib import conf

    settings = conf.read_or_repository("build_folders")
    # Older private configurations can omit the system root.
    return Path(replace(settings, repository_allowed=True).text("roots.system"))


def _retained_path(path: Path, parent: OpenLock) -> Path:
    """Find the current name of a retained parent for namespace exclusion."""
    try:
        directory = path.parent.resolve()
        parent.identity.require_name(directory)
        return directory / path.name
    except OSError:
        pass
    try:
        if sys.platform == "linux":
            directory = Path(os.readlink(f"/proc/self/fd/{parent.descriptor}"))
        else:
            get_path = cast(int, getattr(fcntl, "F_GETPATH", 0))
            directory = Path()
            if get_path:
                buffer = bytes(os.fpathconf(parent.descriptor, "PC_PATH_MAX"))
                raw = fcntl.fcntl(parent.descriptor, get_path, buffer)
                directory = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
        if directory.is_absolute():
            parent.identity.require_name(directory)
            return directory / path.name
    except OSError:
        pass
    raise FileNotFoundError(errno.ENOENT, "the retained mutation parent has no current name", str(path.parent))


def _existing_parent(path: Path, select: Callable[[Path], OpenLock], missing: set[Path]) -> Path:
    for parent in path.parents:
        try:
            _ = select(parent)
        except FileNotFoundError:
            missing.add(parent)
            continue
        return parent
    raise FileNotFoundError(errno.ENOENT, "the mutation parent is absent", str(path))


def _close_namespace_lock(descriptor: int, owner: int) -> None:
    try:
        if os.getpid() == owner:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def mutation_lock(*paths: Path, wait: bool = True,
                  retained_parent: OpenLock | None = None,
                  keep_shared_parent: bool = False,
                  shared_parent_only: bool = False) -> Generator[MutationLockHeld | ParentChanged]:
    """Lock the existing parent of each mutation and share its ancestor locks."""
    owner = os.getpid()
    previous = _mutation_scope.coverage
    held = previous if previous.owner == owner else _MutationCoverage(owner, {}, {}, {})
    flags = 0 if wait and not (held.shared or held.exclusive) else fcntl.LOCK_NB
    borrowed: dict[Path, OpenLock] = {}
    selected: dict[Path, OpenLock] = {}
    guarded_ancestors: dict[Path, FileIdentity] = {}
    missing: set[Path] = set()
    held_locks = held.shared | held.exclusive

    def locations() -> set[Path]:
        if retained_parent is not None:
            return {_retained_path(path.absolute(), retained_parent) for path in paths}
        return {path.parent.resolve() / path.name for path in paths}

    with ExitStack() as stack:
        def select(directory: Path) -> OpenLock:
            if directory in held_locks:
                lock = held_locks[directory]
                lock.identity.require_name(directory)
                borrowed[directory] = lock
                return lock
            if directory not in selected:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                _ = stack.callback(_close_namespace_lock, descriptor, owner)
                selected[directory] = OpenLock.read(descriptor)
            return selected[directory]

        try:
            current = locations()
            parents: set[Path] = set()
            for path in sorted(current):
                for ancestor in path.parents:
                    if ancestor in held_locks:
                        _ = select(ancestor)
                    if ancestor in held.guarded_ancestors:
                        identity = held.guarded_ancestors[ancestor]
                        identity.require_name(ancestor)
                        guarded_ancestors[ancestor] = identity
                if any(ancestor in held.exclusive for ancestor in path.parents):
                    continue
                parent = _existing_parent(path, select, missing)
                if retained_parent is not None and select(parent).identity != retained_parent.identity:
                    raise FileNotFoundError(errno.ENOENT, "the retained mutation parent changed", str(parent))
                # The shared parent protects the root lease. Lock the tree against new child leases.
                if keep_shared_parent and parent == path.parent and parent in held.shared:
                    _ = select(path)
                    parent = path
                parents.add(parent)
            exclusive: set[Path] = set() if shared_parent_only else {parent for parent in parents if not any(
                ancestor in parents or ancestor in held.exclusive for ancestor in parent.parents
            )} - held.exclusive.keys()
            shared_roots = parents if shared_parent_only else exclusive
            shared = {ancestor for parent in shared_roots for ancestor in parent.parents}
            if shared_parent_only:
                shared.update(parents)
            shared.difference_update(held_locks, exclusive)
            for directory in sorted(shared):
                try:
                    _ = select(directory)
                except PermissionError:
                    info = directory.lstat()
                    if not stat.S_ISDIR(info.st_mode):
                        raise NotADirectoryError(errno.ENOTDIR, "the mutation ancestor changed", str(directory))
                    guard = next((parent for parent in directory.parents
                                  if parent in selected or parent in held_locks), None)
                    if guard is None:
                        raise
                    if guard in held.shared:
                        # The shared lock protects this name against removal and permission repair.
                        # Use it only while permissions also prohibit changes inside the directory.
                        if os.access(directory, os.W_OK):
                            raise
                    elif guard not in held.exclusive:
                        exclusive.add(guard)
                    guarded_ancestors[directory] = FileIdentity.from_stat(info)
            shared.difference_update(exclusive, guarded_ancestors)
            for directory in sorted(shared | exclusive):
                if directory in exclusive and directory in held.shared:
                    raise BlockingIOError(errno.EWOULDBLOCK, "cannot upgrade a held shared namespace lock")
                lock = select(directory)
                mode = fcntl.LOCK_EX if directory in exclusive else fcntl.LOCK_SH
                fcntl.flock(lock.descriptor, mode | flags)
            for directory, lock in (borrowed | selected).items():
                lock.identity.require_name(directory)
            for directory, identity in guarded_ancestors.items():
                identity.require_name(directory)
            if current != locations() or any(os.path.lexists(parent) for parent in missing):
                raise FileNotFoundError(errno.ENOENT, "the mutation parent changed", str(min(current).parent))
        except BlockingIOError as error:
            raise MutationLockBusy(errno.EWOULDBLOCK, "the build folder namespace is busy", str(min(paths))) from error
        except (FileNotFoundError, NotADirectoryError) as error:
            stack.close()
            filename = cast(str | bytes | None, error.filename)
            yield ParentChanged(Path(os.fsdecode(filename)) if filename is not None else min(paths).parent)
            return
        _mutation_scope.coverage = _MutationCoverage(owner,
            held.shared | {directory: selected[directory] for directory in shared},
            held.exclusive | {directory: selected[directory] for directory in exclusive},
            held.guarded_ancestors | guarded_ancestors)
        try:
            yield MutationLockHeld()
        finally:
            _mutation_scope.coverage = previous


@contextmanager
def require_mutation_lock(*paths: Path) -> Generator[None]:
    """Refuse a mutation if its parent changes during lock acquisition."""
    with mutation_lock(*paths) as result:
        if isinstance(result, ParentChanged):
            raise FileNotFoundError(errno.ENOENT, "the mutation parent changed", str(result.path))
        yield


@contextmanager
def folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
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


def open_lock(path: Path, directory: int, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path.name + ".lock", flags, 0o600, dir_fd=directory)
    try:
        inode = os.fstat(descriptor)
        # Another process can remove the lock after this open.
        # lock_current refuses a lock with no link.
        if not stat.S_ISREG(inode.st_mode) or inode.st_nlink > 1:
            raise ValueError("a build folder lock must be a regular file with one link")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True)
class ProbeTiming:
    wait_seconds: float
    retry_seconds: float


def take_lock(descriptor: int, mode: int, timing: ProbeTiming) -> bool:
    """Try the lock until the probe wait ends."""
    deadline = time.monotonic() + timing.wait_seconds
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(timing.retry_seconds, remaining))


def lock_current(path: Path, directory: int, descriptor: int) -> bool:
    try:
        return os.path.samestat(os.fstat(descriptor), os.stat(path.name + ".lock", dir_fd=directory,
                                                            follow_symlinks=False))
    except FileNotFoundError:
        return False


def probe(path: Path, root: Path, timing: Callable[[], ProbeTiming]) -> Literal["held", "free"]:
    """Check the lock without using the lease record or session state."""
    try:
        with folder_parent(path, root, create=False) as directory:
            while True:
                descriptor = open_lock(path, directory, create=False)
                try:
                    if not take_lock(descriptor, fcntl.LOCK_EX, timing()):
                        if lock_current(path, directory, descriptor):
                            return "held"
                        continue
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    if lock_current(path, directory, descriptor):
                        return "free"
                finally:
                    os.close(descriptor)
    except FileNotFoundError:
        return "free"
