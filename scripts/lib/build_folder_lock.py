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


@contextmanager
def folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
    """Open the parent and take shared claim locks before creating directories."""
    with ExitStack() as scope:
        if create:
            folders = cast(_BuildFolderCreation, cast(object, import_module("scripts.lib.build_folders")))
            _ = scope.enter_context(folders.build_folder_creation(path))
        yield scope.enter_context(open_folder_parent(path, root, create=create))


@contextmanager
def open_folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
    """Open without following links. The caller guards any creation."""
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


def lock_current(path: Path, directory: int, descriptor: int) -> bool:
    try:
        return os.path.samestat(os.fstat(descriptor), os.stat(path.name + ".lock", dir_fd=directory,
                                                            follow_symlinks=False))
    except FileNotFoundError:
        return False


@contextmanager
def hold_named_lock(path: Path, root: Path, *, mode: int, create: bool,
                    busy: BlockingIOError, changed: Exception,
                    timing: ProbeTiming = NO_WAIT) -> Generator[int | None]:
    """Hold the current lock inode within the specified wait time."""
    with ExitStack() as scope:
        try:
            directory = scope.enter_context(open_folder_parent(path, root, create=create))
            descriptor = open_lock(path, directory, create=create)
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        _ = scope.callback(os.close, descriptor)
        if not take_lock(descriptor, mode, timing):
            raise busy
        if not lock_current(path, directory, descriptor):
            raise changed
        yield descriptor


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
