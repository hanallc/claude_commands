"""Record config reads in one private log for a test module."""
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from threading import local

_open = os.open
_write = os.write
_close = os.close

CONF_READ_LOG = "CONF_READ_LOG"
# The log that ModuleReadLog sets for this process. None reads CONF_READ_LOG at each record,
# because verify_helper.py runs many commands in one process.
_read_log: str | None = None


class _ThreadReadRecording(local):
    def __init__(self) -> None:
        self.excluded: bool = False


_thread_recording = _ThreadReadRecording()


@contextmanager
def without_recording() -> Generator[None]:
    """Exclude config reads in the current thread for this call."""
    previous = _thread_recording.excluded
    _thread_recording.excluded = True
    try:
        yield
    finally:
        _thread_recording.excluded = previous


def record(stem: str) -> None:
    log = os.environ.get(CONF_READ_LOG, "") if _read_log is None else _read_log
    if not log or _thread_recording.excluded:
        return
    descriptor = _open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _ = _write(descriptor, (stem + "\n").encode("utf-8"))
    finally:
        _close(descriptor)


def read_text(path: str | os.PathLike[str]) -> str:
    """Read raw config text and record its file stem."""
    source = path if isinstance(path, str) else path.__fspath__()
    record(os.path.basename(source).removesuffix(".conf"))
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ModuleReadLog:
    """Set the read log for one module and its child processes."""

    def __init__(self, destination: str | os.PathLike[str]) -> None:
        self.destination: str = os.fspath(destination)
        self.previous_log: str | None = None
        self.previous_environment: str | None = None

    def __enter__(self) -> None:
        global _read_log
        self.previous_log = _read_log
        self.previous_environment = os.environ.get(CONF_READ_LOG)
        _read_log = self.destination
        os.environ[CONF_READ_LOG] = _read_log

    def __exit__(self, *_exception: object) -> None:
        global _read_log
        _read_log = self.previous_log
        if self.previous_environment:
            os.environ[CONF_READ_LOG] = self.previous_environment
        else:
            _ = os.environ.pop(CONF_READ_LOG, None)


def record_reads(destination: str | os.PathLike[str]) -> ModuleReadLog:
    return ModuleReadLog(destination)
