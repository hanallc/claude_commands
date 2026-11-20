"""Observe process exits in test fixtures."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
import os
import select
import sys

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@contextmanager
def exit_watch(pid: int) -> Generator[Callable[[], None]]:
    """Observe a process until the kernel reports its exit.

    Start the watch before requesting a stop. A peer socket can close before its process exits.
    """
    if sys.platform == "darwin":
        queue = select.kqueue()
        try:
            try:
                _ = queue.control([select.kevent(pid, select.KQ_FILTER_PROC,
                    select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)], 0)
            except ProcessLookupError:
                yield lambda: None
                return

            def wait_mac() -> None:
                assert queue.control(None, 1, TEST_HANG_GUARD_SECS), f"process {pid} did not exit"

            yield wait_mac
        finally:
            queue.close()
        return
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        yield lambda: None
        return
    try:
        def wait_linux() -> None:
            assert select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)[0], f"process {pid} did not exit"

        yield wait_linux
    finally:
        os.close(descriptor)
