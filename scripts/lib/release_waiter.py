"""Wait for a change in hold or admission directories, with inotify or interval polling."""
from __future__ import annotations

from contextlib import AbstractContextManager
import errno
import os
from pathlib import Path
import select
import time
from typing import final, override

from scripts.lib import inotify


RELEASE_CHANGES = (inotify.IN_CLOSE_WRITE | inotify.IN_MOVED_FROM | inotify.IN_MOVED_TO
                   | inotify.IN_CREATE | inotify.IN_DELETE | inotify.IN_DELETE_SELF
                   | inotify.IN_MOVE_SELF)


@final
class LocalReleaseEvents(AbstractContextManager["LocalReleaseEvents"]):
    """Own Linux directory watches for one local hold wait."""

    def __init__(self, holders: Path, release: Path, mask: int = RELEASE_CHANGES,
                 *, wake_files: tuple[Path, ...] = ()) -> None:
        self.descriptor = inotify.open_watch()
        self.mask = mask
        self.holders = holders
        self.directories = (holders, release)
        self.wake_files = wake_files
        self.watched: set[Path] = set()
        self.watch_paths: dict[int, Path] = {}

    def arm(self) -> None:
        # Watch existing parents to detect new directories before reading state.
        paths = set(self.directories)
        for directory in self.directories:
            if directory.is_dir():
                paths.update(path for path in directory.iterdir() if path.is_dir())
        for directory in (*self.directories, *(path.parent for path in self.wake_files)):
            paths.add(directory)
            parent = directory.parent
            while not parent.is_dir():
                parent = parent.parent
            paths.add(parent)
        for path in paths - self.watched:
            if not path.is_dir():
                continue
            try:
                watch = inotify.add_watch(self.descriptor, path, self.mask)
            except OSError:
                if not path.is_dir():
                    continue
                raise
            self.watched.add(path)
            self.watch_paths[watch] = path

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = max(0, deadline - time.monotonic())
            readable, _, _ = select.select([self.descriptor], [], [], remaining)
            if not readable:
                return
            changed = False
            for event in inotify.read_events(self.descriptor):
                watch, mask, name = event.watch, event.mask, event.name
                if mask & inotify.IN_Q_OVERFLOW:
                    changed = True
                    continue
                if watch not in self.watch_paths:
                    continue
                directory = self.watch_paths[watch]
                if mask & inotify.WATCH_REMOVED:  # Subscribe to the replacement directory.
                    self.watched.discard(directory)
                    changed = True
                if not name or name.startswith("."):
                    continue
                path = directory / name
                if (directory == self.holders or name in {"current", "cycle.json"}
                        or path in self.directories or path in self.wake_files or mask & inotify.IN_ISDIR):
                    changed = True
            if changed or time.monotonic() >= deadline:
                return

    @override
    def __exit__(self, *_arguments: object) -> None:
        os.close(self.descriptor)


@final
class IntervalReleasePolling(AbstractContextManager["IntervalReleasePolling"]):
    """Preserve the caller's retry interval on platforms without inotify."""

    def arm(self) -> None:
        pass

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    @override
    def __exit__(self, *_arguments: object) -> None:
        pass


def local_release_waiter(holders: Path, release: Path,
                         mask: int = RELEASE_CHANGES, *,
                         wake_files: tuple[Path, ...] = ()) -> LocalReleaseEvents | IntervalReleasePolling:
    try:
        return LocalReleaseEvents(holders, release, mask, wake_files=wake_files)
    except AttributeError:
        # macOS libc has no inotify symbols.
        return IntervalReleasePolling()
    except OSError as error:
        if error.errno not in (errno.ENOSYS, errno.ENOTSUP):
            raise
        return IntervalReleasePolling()
