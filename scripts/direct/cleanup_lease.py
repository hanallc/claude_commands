"""Keep a cleanup lease through a foreground command's exit."""

from __future__ import annotations

if __name__ == "__main__":
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})

from collections.abc import Callable, Mapping, Sequence
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
import time

from scripts.lib import build_folder_lock, conf, process_group
from scripts.lib.build_folders import (
    ForegroundMarker, cleanup_incomplete, mark_cleanup_incomplete as mark_cleanup_incomplete, remove_build_folder,
)


class NoCleanupLease(Enum):
    NONE = auto()


class CleanupLeaseUse(Enum):
    KEEP_IN_SUPERVISOR = "keep"
    SHARE_WITH_COORDINATOR = "share"


class CommandLifetime(Enum):
    FOREGROUND = "foreground"
    GROUP = "group"


@dataclass(frozen=True)
class ForegroundCleanupLease:
    descriptor: int
    marker: ForegroundMarker = ForegroundMarker.COMMAND

    def run(self, command: Sequence[str], *, cwd: Path, environment: Mapping[str, str],
            stdin: int | None = None, retain_fds: tuple[int, ...] = (),
            use: CleanupLeaseUse = CleanupLeaseUse.KEEP_IN_SUPERVISOR,
            lifetime: CommandLifetime = CommandLifetime.FOREGROUND,
            stdout: int | None = None) -> subprocess.CompletedProcess[bytes]:
        """Hold the lease in a supervisor that can see its caller stop."""
        arguments = [sys.executable, str(Path(__file__).resolve()), "run",
                     str(os.getpid()), str(os.getpgrp()), str(self.descriptor), self.marker.name,
                     use.value, lifetime.value, *command]
        # The supervisor stays outside the caller's group.
        return subprocess.run(arguments, cwd=cwd, env=environment, stdin=stdin, stdout=stdout,
                              pass_fds=(*retain_fds, self.descriptor), process_group=0, check=False)


def cleanup_seconds() -> float:
    return conf.read_or_repository("build_admission").number("program_stop_seconds")


def cleanup_timing() -> build_folder_lock.ProbeTiming:
    interval = conf.read_or_repository("build_folders").number("locks.probe_retry_seconds")
    return build_folder_lock.ProbeTiming(cleanup_seconds(), interval)


def acquire_cleanup(descriptor: int) -> bool:
    """Acquire the lease within the stop limit and check for incomplete cleanup."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not build_folder_lock.take_lock(descriptor, fcntl.LOCK_EX, cleanup_timing()):
            return False
    return not cleanup_incomplete(descriptor)


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


def group_has_live_members(group: int, *, timeout: float) -> bool:
    try:
        process_group.send_process_group(group, 0)
    except ProcessLookupError:
        return False
    rows = subprocess.check_output(["ps", "-A", "-o", "pgid=", "-o", "stat="], text=True, timeout=timeout)
    return any(int(fields[0]) == group and not fields[1].startswith("Z")
               for line in rows.splitlines() if len(fields := line.split()) == 2)


def run_command(command: Sequence[str], owner: int, group: int, lease: int, use: CleanupLeaseUse,
                *, marker: ForegroundMarker = ForegroundMarker.COMMAND,
                lifetime: CommandLifetime = CommandLifetime.FOREGROUND) -> int:
    if os.getppid() != owner:
        return 128 + signal.SIGTERM
    try:
        marker.begin(lease)
    except OSError as error:
        print(f"could not record foreground cleanup before starting {command[0]}: {error}", file=sys.stderr)
        return 2
    # Only another cleanup coordinator receives the descriptor.
    pass_fds = (lease,) if use is CleanupLeaseUse.SHARE_WITH_COORDINATOR else ()
    owns_group = lifetime is CommandLifetime.GROUP
    try:
        try:
            child = subprocess.Popen(command, process_group=0 if owns_group else group, pass_fds=pass_fds)
        except OSError as error:
            marker.finish(lease)
            print(f"could not start {command[0]}: {error}", file=sys.stderr)
            return 2
        ended = command_ended_before_owner(child.pid, owner)
        deadline = time.monotonic() + cleanup_seconds() if owns_group or not ended else 0
        if not ended:
            if owns_group:
                # The tree-key reader and Git have no shutdown work.
                _ = stop_group(child.pid, signal.SIGKILL)
            else:
                _ = signal_and_resume(child.send_signal, signal.SIGTERM)
        status = child.wait(timeout=max(0, deadline - time.monotonic()) if deadline else None)
        if owns_group and group_has_live_members(child.pid, timeout=max(0, deadline - time.monotonic())):
            if ended:
                _ = stop_group(child.pid, signal.SIGKILL)
            if group_has_live_members(child.pid, timeout=max(0, deadline - time.monotonic())):
                print(f"foreground cleanup is incomplete for group {child.pid}", file=sys.stderr)
                return 2
        marker.finish(lease)
    except subprocess.TimeoutExpired:
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"could not finish foreground cleanup for {command[0]}: {error}", file=sys.stderr)
        return 2
    return status if status >= 0 else 128 - status


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
    return signal_and_resume(lambda number: process_group.send_process_group(group, number), signum)


def main(argv: Sequence[str]) -> int:
    command, *arguments = argv
    if command == "run":
        owner, group, lease = map(int, arguments[:3])
        return run_command(arguments[6:], owner, group, lease, CleanupLeaseUse(arguments[4]),
                           marker=ForegroundMarker[arguments[3]], lifetime=CommandLifetime(arguments[5]))
    if command == "remove":
        return 0 if remove_build_folder(Path(arguments[0]), wait=cleanup_timing()) else 2
    if command == "cancel-group":
        group = int(arguments[0])
        signum = int(arguments[1]) if len(arguments) > 1 else signal.SIGTERM
        try:
            _ = stop_group(group, signum)
        except (ValueError, OSError) as error:
            print(f"could not stop process group {group}: {error}", file=sys.stderr)
            return 2
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
