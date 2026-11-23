"""Run the planner's Cargo commands through shared admission."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

from scripts.build_queue import queue_place
from scripts.copies import copies


class StyleAdmissionStopped(SystemExit):
    """Stop the command when the queue refuses admission."""

    def __init__(self) -> None:
        super().__init__(2)



@contextmanager
def style_program_place(arguments: Sequence[str], repo: Path) -> Generator[queue_place.QueuePlace]:
    """Request a place for the Cargo command."""
    try:
        with queue_place.queue_place(arguments, repo, batch=True) as place:
            yield place
    except queue_place.QueuePlaceRefused as error:
        print(f"style admission: {error}", file=sys.stderr)
        raise StyleAdmissionStopped() from error


def cargo_output(repo: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Capture Cargo output while the shared queue owns the program lifetime."""
    with style_program_place(arguments, repo) as place:
        process = place.popen(arguments, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=copies.COMMAND_LOCKS.get())
        try:
            output, errors = process.communicate()
            return subprocess.CompletedProcess(arguments, process.wait(), output.decode(), errors.decode())
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
