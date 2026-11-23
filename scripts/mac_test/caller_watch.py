"""Watch the caller of a Mac queue run and report when the caller or the queue ends.

mac_queue_run.py starts this file with its own interpreter and `-I -S`. It imports only a few
fast standard library modules, so it starts fast on a loaded host.
"""
from __future__ import annotations

import select
import sys

CALLER_EXITED = 3


def watch_caller(caller: int, queue: int) -> int:
    """Report readiness and wait for a caller or queue exit."""
    watch = select.poll()
    try:
        watch.register(queue, select.POLLIN)
        watch.register(caller, select.POLLIN)
        if watch.poll(0):
            return 2
        print("ready", flush=True)
        events = dict(watch.poll())
        if queue in events:
            return 0
        if events.get(caller, 0) & (select.POLLIN | select.POLLHUP):
            return CALLER_EXITED
    except OSError as error:
        print(f"mac queue: caller watch failed: {error}", file=sys.stderr)
    return 2


def main() -> int:
    caller, queue = sys.argv[1:]
    return watch_caller(int(caller), int(queue))


if __name__ == "__main__":
    raise SystemExit(main())
