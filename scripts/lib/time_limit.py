"""Run a command under a time limit that holds after the caller ends.

Usage: python3 -m scripts.lib.time_limit LIMIT_SECONDS GRACE_SECONDS COMMAND...

Start this runner in its own session, so it outlives its caller. The command runs
in its own process group. At the limit the group gets TERM, so the command can give
back what it holds. After the grace, KILL ends each process that is left. The Mac
has no coreutils timeout, so this runner does that work on both hosts.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
import signal
import subprocess
import sys

from scripts.lib import process_group

TIMED_OUT = 124


def run(limit: float, grace: float, command: Sequence[str]) -> int:
    """Run the command; return its exit status, or TIMED_OUT when the limit ended it."""
    with subprocess.Popen(command, start_new_session=True) as child:
        try:
            status = child.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            pass
        else:
            return status if status >= 0 else 128 - status
        with suppress(ProcessLookupError):
            process_group.send_process_group(child.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            _ = child.wait(timeout=grace)
        # KILL also ends each process that ignored TERM after the command itself ended.
        with suppress(ProcessLookupError):
            process_group.send_process_group(child.pid, signal.SIGKILL)
        _ = child.wait()
    return TIMED_OUT


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3:
        print("usage: time_limit.py LIMIT_SECONDS GRACE_SECONDS COMMAND...", file=sys.stderr)
        return 2
    return run(float(argv[0]), float(argv[1]), argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
