"""Read a process start without losing the reason for an absent identity."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
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


@dataclass(frozen=True)
class ProcessReadDeadline:
    """Check this monotonic deadline before a read and use it to limit the ps timeout."""

    at: float


class StandardProcessReadBudget:
    """Use the standard process read timeout."""


if TYPE_CHECKING:
    ProcessReadBudget = StandardProcessReadBudget | ProcessReadDeadline
else:
    ProcessReadBudget = object

STANDARD_PROCESS_READ_BUDGET = StandardProcessReadBudget()


def ps_read_timeout(budget: ProcessReadBudget) -> float:
    """Limit the standard ps timeout to the time that remains."""
    cap = 5.0
    return min(cap, budget.at - time.monotonic()) if isinstance(budget, ProcessReadDeadline) else cap


def process_start(pid: int, *, budget: ProcessReadBudget = STANDARD_PROCESS_READ_BUDGET) -> ProcessStart:
    """Check the budget before reading the start and use it to limit the ps timeout."""
    timeout = ps_read_timeout(budget)
    if timeout <= 0:
        return Unreadable("the process read reached its deadline")
    if sys.platform == "darwin":
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
    return proc_table_start(pid, Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")))


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
