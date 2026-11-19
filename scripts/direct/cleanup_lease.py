"""Keep a cleanup lease through a foreground command's exit."""

from __future__ import annotations

if __name__ == "__main__":
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum, auto
import fcntl
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

from scripts.lib import build_folder_lock, conf, process_group


class NoCleanupLease(Enum):
    NONE = auto()


@dataclass(frozen=True)
class ForegroundCleanupLease:
    descriptor: int

    def run(self, command: Sequence[str], *, cwd: Path, environment: Mapping[str, str],
            stdin: int | None = None, pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[bytes]:
        """Hold the lease in a supervisor that can see its caller stop."""
        arguments = [sys.executable, str(Path(__file__).resolve()), "run",
                     str(os.getpid()), str(os.getpgrp()), str(self.descriptor), *command]
        # The supervisor survives cancellation. The command joins the caller's group.
        return subprocess.run(arguments, cwd=cwd, env=environment, stdin=stdin,
                              pass_fds=(*pass_fds, self.descriptor), process_group=0, check=False)


def cleanup_seconds() -> float:
    return conf.read_or_repository("build_admission").number("program_stop_seconds")


def acquire_cleanup(descriptor: int) -> bool:
    """Acquire the lease within the stop limit and check for incomplete cleanup."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        retry = conf.read_or_repository("build_folders").number("locks.probe_retry_seconds")
        if not build_folder_lock.take_lock(
            descriptor, fcntl.LOCK_EX, build_folder_lock.ProbeTiming(cleanup_seconds(), retry),
        ):
            return False
    return os.fstat(descriptor).st_size == 0


def command_ended_before_owner(command: int, owner: int) -> bool:
    """Wait for the command or its owner to exit."""
    if os.getppid() != owner:
        return False
    if sys.platform == "darwin":
        queue = select.kqueue()
        try:
            for pid in (command, owner):
                try:
                    event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                          flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
                    _ = queue.control([event], 0)
                except ProcessLookupError:
                    return pid == command
            return any(event.ident == command for event in queue.control(None, 2))
        finally:
            queue.close()
    with ExitStack() as watches:
        try:
            command_fd = watches.enter_context(os.fdopen(os.pidfd_open(command), "rb"))
        except ProcessLookupError:
            return True
        try:
            owner_fd = watches.enter_context(os.fdopen(os.pidfd_open(owner), "rb"))
        except ProcessLookupError:
            return False
        return command_fd in select.select([command_fd, owner_fd], [], [])[0]


def run_command(command: Sequence[str], owner: int, group: int, lease: int) -> int:
    if os.getppid() != owner:
        return 128 + signal.SIGTERM
    # The command and its descendants never receive the cleanup descriptor.
    os.set_inheritable(lease, False)
    try:
        child = subprocess.Popen(command, process_group=group)
    except OSError as error:
        print(f"could not start {command[0]}: {error}", file=sys.stderr)
        return 2
    try:
        if command_ended_before_owner(child.pid, owner):
            status = child.wait()
        else:
            try:
                child.terminate()
                child.send_signal(signal.SIGCONT)
            except ProcessLookupError:
                pass
            status = child.wait(timeout=cleanup_seconds())
    except subprocess.TimeoutExpired:
        # Keep the scope if the command can still use its files.
        mark_cleanup_incomplete(lease)
        return 2
    except (OSError, ValueError) as error:
        mark_cleanup_incomplete(lease)
        print(f"could not check foreground command {child.pid}: {error}", file=sys.stderr)
        return 2
    return status if status >= 0 else 128 - status


def mark_cleanup_incomplete(descriptor: int) -> None:
    _ = os.write(descriptor, b"foreground cleanup incomplete\n")


def cancel_group(group: int) -> None:
    try:
        process_group.send_process_group(group, signal.SIGTERM)
        process_group.send_process_group(group, signal.SIGCONT)
    except ProcessLookupError:
        pass


def main(argv: Sequence[str]) -> int:
    command, *arguments = argv
    if command == "run":
        owner, group, lease = map(int, arguments[:3])
        return run_command(arguments[3:], owner, group, lease)
    if command == "acquire":
        return 0 if acquire_cleanup(int(arguments[0])) else 2
    if command == "cancel-group":
        cancel_group(int(arguments[0]))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
