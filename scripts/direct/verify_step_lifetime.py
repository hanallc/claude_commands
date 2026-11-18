"""Stop a local build group if its verify caller dies."""

from __future__ import annotations

from contextlib import suppress
import os
import select
import signal
import sys

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.lib.process_group import send_process_group


_watchers: set[int] = set()


def watch_group(caller_fd: int, leader_fd: int, group: int) -> None:
    # The watch must survive its caller and hold no helper or output pipes.
    os.setsid()
    low, high = sorted((caller_fd, leader_fd))
    os.closerange(0, low)
    os.closerange(low + 1, high)
    os.closerange(high + 1, os.sysconf("SC_OPEN_MAX"))

    events = select.poll()
    events.register(caller_fd, select.POLLIN)
    events.register(leader_fd, select.POLLIN)
    ended = {descriptor for descriptor, _ in events.poll()}
    if caller_fd in ended and leader_fd not in ended:
        with suppress(ProcessLookupError):
            send_process_group(group, signal.SIGKILL)


def main(argv: list[str]) -> int:
    try:
        caller, group = map(int, argv)
        if caller <= 1 or group <= 1 or os.getpgid(group) != group:
            raise ValueError("invalid caller or local step group")
        for watcher in tuple(_watchers):
            pid, _ = os.waitpid(watcher, os.WNOHANG)
            if pid:
                _watchers.remove(watcher)

        caller_fd = os.pidfd_open(caller)
        try:
            leader_fd = os.pidfd_open(group)
            try:
                # Fork from verify_helper.py so each step shares its interpreter.
                child = os.fork()
                if child == 0:
                    try:
                        watch_group(caller_fd, leader_fd, group)
                    finally:
                        os._exit(0)
                _watchers.add(child)
            finally:
                os.close(leader_fd)
        finally:
            os.close(caller_fd)
    except (OSError, ValueError) as error:
        print(f"verify.sh: cannot watch the local step: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
