"""Run a command under a time limit that holds after the caller ends.

Usage: python3 -m scripts.lib.time_limit LIMIT_SECONDS GRACE_SECONDS COMMAND...
Capture: python3 -m scripts.lib.time_limit DEADLINE GRACE_SECONDS --capture-until COMMAND...

Start this runner in its own session, so it outlives its caller. The command runs
in its own process group. At the limit the group gets TERM, so the command can give
back what it holds. After the grace, KILL ends each process that is left. The Mac
has no coreutils timeout, so this runner does that work on both hosts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from enum import Enum
from pathlib import Path
from threading import Thread
import signal
import subprocess
import sys
import time

from scripts.lib import process_group

TIMED_OUT = 124


class CommandOutput(Enum):
    INHERIT = "inherit"
    CAPTURE = "capture"


def capture(command: Sequence[str], *, deadline: float, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Read the runner's output until the deadline. Let the runner stop its group."""
    arguments = [sys.executable, "-m", "scripts.lib.time_limit", str(deadline), "0", "--capture-until", *command]
    runner = subprocess.Popen(arguments, cwd=Path(__file__).resolve().parents[2], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True,
                              text=True, errors="replace")
    assert runner.stdout is not None and runner.stderr is not None
    try:
        stdout, stderr = runner.communicate(timeout=max(0, deadline - time.monotonic()))
        return subprocess.CompletedProcess(command, runner.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, TIMED_OUT, "", "")
    finally:
        # The runner owns the group deadline even if this caller closes its pipes.
        runner.stdout.close()
        runner.stderr.close()
        if runner.returncode is None:
            Thread(target=runner.wait, daemon=True).start()


def run(deadline: float, grace: float, command: Sequence[str], *,
        output: CommandOutput = CommandOutput.INHERIT) -> int:
    """Return the command status or TIMED_OUT when the deadline ends its group."""
    if deadline <= time.monotonic():
        return TIMED_OUT
    pipe = subprocess.PIPE if output is CommandOutput.CAPTURE else None
    with subprocess.Popen(command, stdout=pipe, stderr=pipe, start_new_session=True) as child:
        try:
            try:
                stdout, stderr = child.communicate(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process_group.send_process_group(child.pid, signal.SIGTERM)
                with suppress(subprocess.TimeoutExpired):
                    _ = child.communicate(timeout=grace)
                return TIMED_OUT
        finally:
            # The direct command can exit while descendants still use its group.
            with suppress(ProcessLookupError):
                process_group.send_process_group(child.pid, signal.SIGKILL)
            _ = child.wait()
        if output is CommandOutput.CAPTURE:
            _ = sys.stdout.buffer.write(stdout)
            _ = sys.stderr.buffer.write(stderr)
        return child.returncode if child.returncode >= 0 else 128 - child.returncode


def main(argv: Sequence[str]) -> int:
    captured = len(argv) > 2 and argv[2] == "--capture-until"
    command = argv[3:] if captured else argv[2:]
    if not command:
        print("usage: time_limit.py LIMIT_SECONDS GRACE_SECONDS COMMAND...", file=sys.stderr)
        return 2
    deadline = float(argv[0]) if captured else time.monotonic() + float(argv[0])
    return run(deadline, float(argv[1]), command,
               output=CommandOutput.CAPTURE if captured else CommandOutput.INHERIT)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
