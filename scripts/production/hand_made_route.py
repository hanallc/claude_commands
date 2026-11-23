"""The parts of a hand made run that hand_made.py and each machine route share.

hand_made.py sends one placement request with a place in the line of each named machine. The queue gives
the run to one machine, and the route of that machine runs the program there. ROUTES in hand_made.py is
the one place that adds a machine.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import BinaryIO, Protocol, TextIO, Unpack

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.queue_place import HeldReservation, PopenArguments

# The only variables that the program gets: no other variable can make it load code that the check does not read.
PROGRAM_VARIABLES = frozenset({"HOME", "USER", "LOGNAME", "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM",
                               "TZ", "TMPDIR", "XDG_RUNTIME_DIR", "SHELL", "NIX_PATH", "LOCALE_ARCHIVE",
                               "SSL_CERT_FILE", "NIX_SSL_CERT_FILE"})

# The answer of the queue that gives the run to one machine.
Placed = wire.LocalPlacementGranted | wire.Assignment


def discard(output: BinaryIO | TextIO) -> None:
    """Point the descriptor of an output that failed at /dev/null; /dev/null takes the bytes it keeps at exit."""
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            _ = os.dup2(null, output.fileno())
        finally:
            os.close(null)
    except OSError:
        pass


def note(text: str) -> None:
    """Write one line to stderr; a closed stderr loses the line and does not end the attempt."""
    try:
        print(f"hand made: {text}", file=sys.stderr, flush=True)
    except OSError:
        discard(sys.stderr)


class Refused(Exception):
    """The attempt ends before the run; the message names the cause."""


class MachineFailed(Exception):
    """The machine failed before the program gave a status, and no work of the run is left on it.

    The run goes back to the other named machines.
    """


@dataclass(frozen=True)
class Program:
    """The command that the check passed, the file that the run starts on natedev, and each checked file."""
    argv: tuple[str, ...]
    repo: Path
    start: Path | None
    # Each checked file with its sha256 at the check.
    checked: tuple[tuple[Path, str], ...]


class Output(Protocol):
    """The log of the row and the caller, which get the output as it comes."""

    def write(self, chunk: bytes) -> None: ...


class ProgramPlace(Protocol):
    """A held natedev queue place that starts one program in its scope."""

    @property
    def peak_bytes(self) -> int: ...

    def popen(self, argv: Sequence[str], **popen_arguments: Unpack[PopenArguments]) -> subprocess.Popen[bytes]: ...


class MachineRun(Protocol):
    """A held place on one machine that runs the program once."""

    @property
    def peak_bytes(self) -> int | None: ...

    @property
    def started_at(self) -> float | None:
        """The time that the program started, or None when it did not start."""
        ...

    def run(self, output: Output) -> int:
        """Run the program and return its status; MachineFailed when the machine gave no status."""
        ...


class Line(Protocol):
    """The placement request of one attempt, with a place in the line of each named machine."""

    @property
    def folder(self) -> Path:
        """The folder of the attempt; it ends with the attempt."""
        ...

    def wait(self) -> Placed:
        """Wait until the queue gives the run to a named machine; Refused when the queue ends the run."""
        ...

    def local_place(self, grant: HeldReservation) -> AbstractContextManager[ProgramPlace]:
        """The natedev queue place of a local grant."""
        ...

    def close(self, placed: Placed, status: int | None, reason: str) -> None:
        """Give the place back to the queue: with the program status, or with no status and the reason."""
        ...

    def give_back(self, placed: wire.Assignment, reason: str) -> None:
        """Close the attempt on a failed machine, and keep the place in the lines of the other machines."""
        ...


# A route holds a place on its machine; at its end it closes the place, unless it raised MachineFailed.
Route = Callable[[Line, Placed, Program], AbstractContextManager[MachineRun]]


def no_refusal(program: Program) -> str:
    del program
    return ""


@dataclass(frozen=True)
class Machine:
    """The route of a machine, and why the machine cannot take a program (no text when it can)."""
    route: Route
    refusal: Callable[[Program], str] = no_refusal
