"""Check the exact lock inode for a build folder."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
from importlib import import_module
import os
from pathlib import Path
import stat
import time
from typing import Literal, Protocol, cast


class _BuildFolderCreation(Protocol):
    def build_folder_creation(self, path: Path, /) -> AbstractContextManager[tuple[int, ...]]: ...


USER_REMOVAL_ROOTS = frozenset({"slot"})
_LOCK_MARK = b"build-folder-lock\n"


def _open_parent(path: Path, root: Path, *, create: bool) -> int:
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
        return directory
    except BaseException:
        os.close(directory)
        raise


@contextmanager
def folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
    """Hold shared claim locks before creating parent directories."""
    with ExitStack() as scope:
        if create:
            folders = cast(_BuildFolderCreation, cast(object, import_module("scripts.lib.build_folders")))
            _ = scope.enter_context(folders.build_folder_creation(path))
        yield scope.enter_context(open_folder_parent(path, root, create=create))


@contextmanager
def open_folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
    """Open each parent without following links. Retry once if creation races with a move."""
    try:
        directory = _open_parent(path, root, create=create)
    except FileNotFoundError:
        if not create:
            raise
        directory = _open_parent(path, root, create=True)
    try:
        yield directory
    finally:
        os.close(directory)


def open_lock(path: Path, directory: int, *, create: bool, exclusive: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | (os.O_EXCL if exclusive else 0)
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


def mark_lock(descriptor: int) -> None:
    """Mark the acquired lock before the allocator checks its parent."""
    size = os.fstat(descriptor).st_size
    if size:
        if size != len(_LOCK_MARK) or os.pread(descriptor, len(_LOCK_MARK), 0) != _LOCK_MARK:
            raise ValueError("a build folder lock must be empty or marked")
    elif os.pwrite(descriptor, _LOCK_MARK, 0) != len(_LOCK_MARK):
        raise OSError("could not mark a build folder lock")


def is_marked_lock(name: str, directory: int, inode: os.stat_result) -> bool:
    """Identify a protocol lock before its folder or lease record exists."""
    if not stat.S_ISREG(inode.st_mode) or inode.st_size != len(_LOCK_MARK):
        return False
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        return os.path.samestat(inode, os.fstat(descriptor)) and os.read(descriptor, len(_LOCK_MARK)) == _LOCK_MARK
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class ProbeTiming:
    wait_seconds: float
    retry_seconds: float


NO_WAIT = ProbeTiming(0, 0)


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


def parent_current(path: Path, directory: int) -> bool:
    """A checked child lock exists before any later rename, so the second scan sees it.
    An allocator abandons the child lock when the parent check fails."""
    try:
        return os.path.samestat(os.stat(path.parent), os.fstat(directory))
    except FileNotFoundError:
        return False


def lock_current(path: Path, directory: int, descriptor: int) -> bool:
    try:
        return os.path.samestat(os.fstat(descriptor), os.stat(path.name + ".lock", dir_fd=directory,
                                                            follow_symlinks=False))
    except FileNotFoundError:
        return False


@contextmanager
def hold_named_lock(path: Path, root: Path, *, mode: int, create: bool,
                    busy: BlockingIOError, changed: Exception, timing: ProbeTiming = NO_WAIT,
                    allocation_check: Callable[[Path, int], bool] | None = None) -> Generator[int | None]:
    """Hold the current lock inode within the specified wait time."""
    for _attempt in range(2):
        with ExitStack() as scope:
            directory = None
            try:
                directory = scope.enter_context(open_folder_parent(path, root, create=create))
                descriptor = open_lock(path, directory, create=create)
            except FileNotFoundError:
                if create:
                    if allocation_check is not None and directory is not None and not parent_current(path, directory):
                        continue
                    raise
                yield None
                return
            _ = scope.callback(os.close, descriptor)
            if not take_lock(descriptor, mode, timing):
                raise busy
            if allocation_check is not None:
                if create:
                    mark_lock(descriptor)
                if not allocation_check(path, directory):
                    continue
            if not lock_current(path, directory, descriptor):
                break
            yield descriptor
            return
    raise changed


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
