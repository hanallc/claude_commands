"""Record config reads in one private log for a test module."""
from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass
import os
from contextlib import contextmanager
import re
from threading import local
from typing import final, override

_open = os.open
_write = os.write
_close = os.close

CONF_READ_LOG = "CONF_READ_LOG"
CONF_SHIFT_READ_LOG = "CONF_SHIFT_READ_LOG"
SHIFT_MARK = "# test config shift: "
_shift_read_log = os.environ.get(CONF_SHIFT_READ_LOG, "")
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


def shifted_original(mark: str, value: str) -> str:
    if not mark.startswith(SHIFT_MARK):
        return ""
    original, _, shifted = mark.removeprefix(SHIFT_MARK).partition(" ")
    return original if shifted == value else ""


def set_shift_read_log(destination: str) -> str:
    """Select the log in this process and in its child processes."""
    global _shift_read_log
    previous, _shift_read_log = _shift_read_log, destination
    if destination:
        os.environ[CONF_SHIFT_READ_LOG] = destination
    else:
        _ = os.environ.pop(CONF_SHIFT_READ_LOG, None)
    return previous


def record_shift(filename: str, key: str, original: str, shifted: str) -> None:
    destination = _shift_read_log
    if not destination or _thread_recording.excluded:
        return
    try:
        descriptor = _open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            _ = _write(descriptor, f"{filename}:{key}: {original} -> {shifted}\n".encode())
        finally:
            _close(descriptor)
    except OSError:
        pass


@dataclass(frozen=True)
class ShiftedValue:
    """A marker links the current value to its committed value."""

    key: str
    original: str
    value: str
    line: int


def shifted_values(contents: str) -> Iterator[ShiftedValue]:
    """Find marked settings whose current value agrees with the marker."""
    if SHIFT_MARK not in contents:
        return
    previous = before_limit = ""
    for index, line in enumerate(contents.splitlines()):
        if previous.startswith(("# whole:", "# range:")) and before_limit.startswith(SHIFT_MARK):
            key, separator, value = line.partition("=")
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if separator and (original := shifted_original(before_limit, value)):
                yield ShiftedValue(key.strip(), original, value, index)
        before_limit, previous = previous, line.strip()


def shift_reads_enabled() -> bool:
    return bool(_shift_read_log)


def shift_child_environment() -> dict[str, str]:
    """Preserve the active shift log when a child gets a private environment."""
    return {CONF_SHIFT_READ_LOG: _shift_read_log} if _shift_read_log else {}


@dataclass(frozen=True)
class ShiftedConfigKey:
    """The marked value and the filename that supplied it."""

    filename: str
    shift: ShiftedValue

    def record(self) -> None:
        record_shift(self.filename, self.shift.key, self.shift.original, self.shift.value)


@final
class ShiftTracedValues(Mapping[str, str]):
    """Record raw config values when a caller uses a marked key."""

    def __init__(self, values: Mapping[str, str], shifts: Mapping[str, ShiftedConfigKey]) -> None:
        self._values = values
        self._shifts = shifts

    @override
    def __getitem__(self, key: str) -> str:
        value = self._values[key]
        if (origin := self._shifts.get(key)) is not None and origin.shift.value == value.strip():
            origin.record()
        return value

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    @override
    def __len__(self) -> int:
        return len(self._values)


def shift_origins(path: os.PathLike[str], contents: str) -> dict[str, ShiftedConfigKey]:
    """Find the source and committed value for each marked key."""
    if not shift_reads_enabled():
        return {}
    name = os.path.basename(path)
    return {shift.key: ShiftedConfigKey(name, shift) for shift in shifted_values(contents)}


def trace_values(path: os.PathLike[str], contents: str, values: Mapping[str, str]) -> Mapping[str, str]:
    """Attach read logging to a raw config dictionary during a test."""
    shifts = shift_origins(path, contents)
    return ShiftTracedValues(values, shifts) if shifts else values


def read_text(path: str | os.PathLike[str]) -> str:
    """Read raw config text and record its file stem."""
    record(os.path.basename(path).removesuffix(".conf"))
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
