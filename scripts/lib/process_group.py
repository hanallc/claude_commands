"""Validate process-group ids before sending a signal."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress

STOP_WAIT_SECONDS = 10


def check_process_group(pid: object) -> int:
    """Refuse special ids, non-integers and the caller's own group."""
    if type(pid) is not int:
        raise ValueError("process group pid must be a real int")
    if pid <= 1:
        raise ValueError("process group pid must be greater than 1")
    if pid == os.getpgrp():
        raise ValueError("refusing to signal the caller's own process group")
    return pid


def send_process_group(pid: object, signum: int) -> None:
    """Send a signal to a checked group."""
    _send_checked_group(check_process_group(pid), signum)


def terminate_owned_process_group() -> None:
    """Send SIGTERM to the group that the caller leads."""
    pid = os.getpid()
    if pid <= 1 or pid != os.getpgrp():
        raise ValueError("the caller must lead its process group")
    _send_checked_group(pid, signal.SIGTERM)


def run_process_group(command: Sequence[str], *, timeout: float,
                      env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command in its own group and stop the whole group when it returns or fails.

    A browser leaves helper processes after its own exit or a timeout.
    """
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env=env, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        finally:
            stop_process_group(process)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def stop_process_group(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """Stop the group that a `start_new_session` child leads, then close its pipes."""
    try:
        with suppress(ProcessLookupError):
            send_process_group(process.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            _ = process.wait(timeout=STOP_WAIT_SECONDS)
        # The leader can exit before its child processes.
        with suppress(ProcessLookupError):
            send_process_group(process.pid, signal.SIGKILL)
        _ = process.wait(timeout=STOP_WAIT_SECONDS)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _send_checked_group(group: int, signum: int) -> None:
    os.killpg(group, signum)


def main(argv: Sequence[str]) -> int:
    """CLI: process_group.py PID SIGNAL (TERM, SIGTERM or a signal number)."""
    if len(argv) != 2:
        print("usage: process_group.py PID SIGNAL", file=sys.stderr)
        return 2
    try:
        pid = int(argv[0])
        name = argv[1]
        if name.isdecimal():
            signum = int(name)
        else:
            signum = signal.Signals[name if name.startswith("SIG") else "SIG" + name].value
        send_process_group(pid, signum)
    except ProcessLookupError:
        return 3
    except (ValueError, KeyError, OSError) as error:
        print(f"process_group: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
