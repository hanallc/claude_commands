"""Read a process start without losing the reason for an absent identity."""
from __future__ import annotations

from contextlib import closing
import math
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from typing import override
else:
    _Method = TypeVar("_Method")

    def override(method: _Method) -> _Method:
        return method


class Started:
    """A live process has this stored start identity."""

    def __init__(self, start: str) -> None:
        self.start: str = start

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Started and self.start == other.start


class Zombie:
    """An ended process still owns its PID and start identity."""

    def __init__(self, start: str) -> None:
        self.start: str = start

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Zombie and self.start == other.start


class Gone:
    """The process no longer exists."""

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Gone


class Unreadable:
    """The process can exist, but its start cannot be read."""

    def __init__(self, reason: str) -> None:
        self.reason: str = reason

    @override
    def __eq__(self, other: object) -> bool:
        return type(other) is Unreadable and self.reason == other.reason


if TYPE_CHECKING:
    ProcessStart = Started | Zombie | Gone | Unreadable
else:
    # Python 3.9 imports this module from board.sh. Annotations stay deferred.
    ProcessStart = object


def process_start_format() -> str:
    """Name the identity format used on this host."""
    return "darwin-lstart-utc-c-v1" if sys.platform == "darwin" else "linux-proc-stat22-v1"


def process_start(pid: int) -> ProcessStart:
    """Read the process start from the host."""
    if sys.platform == "darwin":
        return _darwin_process_start(pid, 5)
    return proc_table_start(pid, Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")))


def process_start_before(pid: int, deadline: float) -> ProcessStart:
    """Read a process start within an existing cleanup budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return Unreadable("process start deadline expired")
    if sys.platform == "darwin":
        return _darwin_process_start(pid, min(5, remaining))
    return process_start(pid)


def _darwin_process_start(pid: int, timeout: float) -> ProcessStart:
    if pid <= 0:
        return Unreadable("invalid process id")
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
            capture_output=True, text=True, timeout=timeout, check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        return Unreadable(str(error))
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return Gone()
    if result.returncode:
        return Unreadable(result.stderr.strip() or "ps failed")
    fields = result.stdout.strip().rsplit(None, 1)
    if (len(fields) != 2 or not re.fullmatch(
            r"[A-Z][a-z]{2} [A-Z][a-z]{2} +[0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}", fields[0])
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9+<>-]*", fields[1])):
        return Unreadable("invalid ps process identity")
    return Zombie(fields[0]) if fields[1].startswith("Z") else Started(fields[0])


def proc_table_start(pid: int, proc_root: Path) -> ProcessStart:
    """Read the process start from the specified Linux process table."""
    if pid <= 0:
        return Unreadable("invalid process id")
    try:
        path = proc_root / str(pid) / "stat"
        _, separator, rest = path.read_bytes().rpartition(b")")
        fields = rest.split()
        if not separator or len(fields) <= 19 or not fields[19].isdigit() or fields[0] not in (b"R", b"S", b"D", b"Z", b"T", b"t", b"X", b"x", b"K", b"W", b"P", b"I"):
            return Unreadable("invalid process stat fields")
        start = fields[19].decode("ascii")
        return Zombie(start) if fields[0] == b"Z" else Started(start)
    except (FileNotFoundError, ProcessLookupError):
        return Gone()
    except (OSError, UnicodeError) as error:
        return Unreadable(str(error))



class ProcessExited:
    """The observed process ended or its PID belongs to a later process."""


class ProcessExitTimedOut:
    """The deadline passed before the observer saw exit."""


def observe_process_exit(pid: int, deadline: float, *, expected_start: str = "") -> ProcessExited | ProcessExitTimedOut | Unreadable:
    """Observe exit without consuming a child's wait status."""
    def read_identity() -> Started | ProcessExited | ProcessExitTimedOut | Unreadable:
        identity = process_start_before(pid, deadline)
        if isinstance(identity, Unreadable) and time.monotonic() >= deadline:
            return ProcessExitTimedOut()
        return classify(identity)

    def classify(identity: ProcessStart) -> Started | ProcessExited | Unreadable:
        if isinstance(identity, Gone):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return ProcessExited()
            except OSError as error:
                return Unreadable(str(error))
            return Unreadable("process exists without a readable start")
        if isinstance(identity, Zombie):
            return ProcessExited()
        if isinstance(identity, Started) and expected_start and identity.start != expected_start:
            return ProcessExited()
        return identity

    observed = read_identity()
    if not isinstance(observed, Started):
        return observed

    def remaining() -> float | None:
        return None if math.isinf(deadline) else max(0, deadline - time.monotonic())

    try:
        if sys.platform == "darwin":
            with closing(select.kqueue()) as queue:
                event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                      flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                      fflags=select.KQ_NOTE_EXIT)
                _ = queue.control([event], 0, 0)
                identity = read_identity()
                if not isinstance(identity, Started):
                    return identity
                if identity.start != observed.start:
                    return ProcessExited()
                ready = bool(queue.control(None, 1, remaining()))
        else:
            descriptor = os.pidfd_open(pid)
            try:
                identity = read_identity()
                if not isinstance(identity, Started):
                    return identity
                if identity.start != observed.start:
                    return ProcessExited()
                ready = bool(select.select([descriptor], [], [], remaining())[0])
            finally:
                os.close(descriptor)
    except ProcessLookupError:
        return ProcessExited()
    except (AttributeError, OSError) as error:
        return Unreadable(str(error))
    return ProcessExited() if ready else ProcessExitTimedOut()
