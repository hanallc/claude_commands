"""Stop a process group and check when its live members exit."""

from __future__ import annotations

from collections.abc import Callable
import signal
import subprocess
import time

from scripts.lib.process_group import send_process_group


def signal_and_resume(send: Callable[[int], None], signum: int) -> bool:
    """Resume a stopped process after it receives a signal."""
    try:
        send(signum)
    except ProcessLookupError:
        return False
    if signum != signal.SIGKILL:
        try:
            send(signal.SIGCONT)
        except ProcessLookupError:
            pass
    return True


def stop_group(group: int, signum: int) -> bool:
    """Signal a process group and let its stopped members process the signal."""
    return signal_and_resume(lambda number: send_process_group(group, number), signum)


def group_has_live_members(group: int, *, timeout: float | None = None) -> bool:
    try:
        send_process_group(group, 0)
    except ProcessLookupError:
        return False
    rows = subprocess.check_output(["ps", "-A", "-o", "pgid=", "-o", "stat="], text=True, timeout=timeout)
    return any(int(fields[0]) == group and not fields[1].startswith("Z")
               for line in rows.splitlines() if len(fields := line.split()) == 2)


def wait_for_group_exit(group: int, *, deadline: float, interval: float) -> bool:
    """Prove that the group has no live members before the deadline."""
    while group_has_live_members(group, timeout=max(0, deadline - time.monotonic())):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))
    return True
