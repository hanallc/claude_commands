"""Enforce the conversation-pause hook deadline without a signal handler."""

from __future__ import annotations

import os
import selectors
import signal
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, NoReturn

__all__ = ["pause_state_root", "print_hook_error", "run_hook_with_budget"]

_HOOK_BUDGET_SECONDS = 5


class HookBudgetSpent(Exception):
    """The hook exceeded its time budget."""


def pause_state_root() -> Path:
    return Path(os.environ.get("CONVERSATION_PAUSE_STATE_DIR")
                or Path.home() / ".local/state/conversation-pause")


def print_hook_error(error: Exception) -> None:
    lines = str(error).splitlines()
    detail = lines[0] if lines else ""
    print(f"conversation-pause: {type(error).__name__}: {detail}", file=sys.stderr)


def _exit_hook_process(run: Callable[[], int]) -> NoReturn:
    status = 1
    try:
        status = run()
    except Exception as error:
        print_hook_error(error)
        status = 0
    finally:
        try:
            _ = sys.stdout.flush()
            _ = sys.stderr.flush()
        finally:
            os._exit(status)


def _signal_worker(pid: int) -> None:
    # Stop the worker even if it has yet to create its process group.
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)

    # Load the shared check only when the supervisor stops a worker.
    from scripts.lib.process_group import send_process_group

    with suppress(ProcessLookupError):
        send_process_group(pid, signal.SIGKILL)


def _watch_worker(pid: int, owner: BinaryIO, completion: BinaryIO, deadline: float, budget: int) -> int:
    finished = False
    try:
        with selectors.DefaultSelector() as selector:
            _ = selector.register(owner, selectors.EVENT_READ)
            _ = selector.register(completion, selectors.EVENT_READ)
            ready = {key.fd for key, _events in selector.select(max(0, deadline - time.monotonic()))}
            finished = completion.fileno() in ready and owner.fileno() not in ready
    finally:
        if not finished:
            _signal_worker(pid)
        _, status = os.waitpid(pid, 0)
        if not finished:
            # The worker's temporary files contain the supervisor and worker PIDs.
            root = pause_state_root() / "scheduled-prompts"
            for temporary in root.glob(f".scheduled-prompts-{os.getpid()}-{pid}-*"):
                temporary.unlink(missing_ok=True)
    if not ready:
        raise HookBudgetSpent(f"gave up after {budget} seconds")
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def _supervise_hook(work: Callable[[], None], owner: BinaryIO, deadline: float, budget: int) -> int:
    # The supervisor must receive owner EOF even if the caller's group ends.
    _ = os.setsid()
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as completion, os.fdopen(writer, "wb") as running:
        pid = os.fork()
        if pid == 0:
            owner.close()
            completion.close()

            def run() -> int:
                os.setpgid(0, 0)
                work()
                return 0

            _exit_hook_process(run)
        running.close()
        return _watch_worker(pid, owner, completion, deadline, budget)


def run_hook_with_budget(work: Callable[[], None]) -> None:
    """Supervise hook work until it finishes, reaches the deadline, or loses its owner."""
    budget = int(os.environ.get("CONVERSATION_PAUSE_HOOK_BUDGET", _HOOK_BUDGET_SECONDS))
    if budget == 0:
        work()
        return

    deadline = time.monotonic() + budget
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as owner, os.fdopen(writer, "wb") as owned:
        pid = os.fork()
        if pid == 0:
            owned.close()
            _exit_hook_process(lambda: _supervise_hook(work, owner, deadline, budget))
        owner.close()
        try:
            _, status = os.waitpid(pid, 0)
        except BaseException:
            owned.close()
            with suppress(ChildProcessError):
                _ = os.waitpid(pid, 0)
            raise
    code = os.waitstatus_to_exitcode(status)
    if code:
        raise SystemExit(code if code >= 0 else 128 - code)
