#!/usr/bin/env python3
"""Publish queued program ownership and execute the program."""
from __future__ import annotations
if not __package__:
    try:
        with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
    except FileNotFoundError:
        pass

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
import os
from pathlib import Path
import signal
import sys

from scripts.lib import atomic_file
from scripts.lib.process_start import Started, process_start
from scripts.build_queue.admission_wire import Owner, encode_record


STEP_SCOPE_ENV = "BUILDLOG_STEP_SCOPE"
# The --scope-start word of a program scope that a reservation pays for.
RESERVED_WORD = "--reserved"


class ScopeReservation(Enum):
    """Whether a reservation pays for the memory of the program scope."""
    # The program names its scope as a queued step does: the cargo wrapper starts cargo at once in that scope,
    # and memory_admit charges the compilers that sccache runs for the scope to the reservation.
    HELD = auto()
    # No reservation pays for the scope, so its cargo calls take the queue path and its compilers stay global.
    UNRESERVED = auto()

    def arguments(self) -> list[str]:
        """The --scope-start words that select this value."""
        return [RESERVED_WORD] if self is ScopeReservation.HELD else []


class ProgramStartRefused(Exception):
    """The program cannot publish its ownership or enter its scope."""


@dataclass(frozen=True)
class ProgramStart:
    """The record links the program and its queue holder before execution."""
    holder: Owner
    program: Owner
    scope: str


def program_start(record: Path, holder: Owner, argv: Sequence[str], *, scope: str = "") -> None:
    """Publish ownership before execution and reject a dead holder."""
    if process_start(holder.pid) != Started(holder.start_time):
        raise ProgramStartRefused("the queue place holder exited before program startup")
    start = process_start(os.getpid())
    if not isinstance(start, Started):
        raise ProgramStartRefused("the program has no live process identity")
    atomic_file.write_text_atomic(record, encode_record(ProgramStart(holder, Owner(os.getpid(), start.start), scope)))
    if process_start(holder.pid) != Started(holder.start_time):
        record.unlink(missing_ok=True)
        raise ProgramStartRefused("the queue place holder exited during program publication")
    _ = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
    os.execvp(argv[0], list(argv))


def scope_start(sidecar: Path, holder_pid: int, holder_start: str, argv: Sequence[str], *, record: Path,
                reservation: ScopeReservation) -> None:
    """Publish the scope and confirm its holder before the program starts."""
    if process_start(holder_pid) != Started(holder_start):
        raise ProgramStartRefused("the queue place holder exited before scope startup")
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::") and "/builds.slice/" in line:
            scope = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            atomic_file.atomic_sample(sidecar, str(scope) + "\n")
            if process_start(holder_pid) != Started(holder_start):
                raise ProgramStartRefused("the queue place holder exited during scope publication")
            if reservation is ScopeReservation.HELD:
                os.environ[STEP_SCOPE_ENV] = str(scope)
            program_start(record, Owner(holder_pid, holder_start), argv, scope=str(scope))
    raise ProgramStartRefused("the program did not enter its memory scope")


def main(argv: list[str] | None = None) -> int:
    """Start the program after publishing its ownership or memory scope."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--scope-start"]:
        # A run that loaded queue_place.py before the reservation word gives no word: its scope stays unnamed.
        reservation = ScopeReservation.HELD if argv[1:2] == [RESERVED_WORD] else ScopeReservation.UNRESERVED
        words = argv[1 + len(reservation.arguments()):]
        if len(words) >= 4:
            scope_start(Path(words[0]), int(words[1]), words[2], words[4:], record=Path(words[3]),
                        reservation=reservation)
            return 0
    if len(argv) >= 5 and argv[0] == "--program-start":
        program_start(Path(argv[1]), Owner(int(argv[2]), argv[3]), argv[4:])
        return 0
    print("program start: expected --program-start or --scope-start arguments", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
