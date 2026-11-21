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
import time

from scripts.direct.group_cleanup import group_has_live_members, wait_for_group_exit
from scripts.direct.group_cleanup import signal_and_resume as signal_and_resume, stop_group as stop_group
from scripts.lib import build_folder_lock, conf
from scripts.lib.build_folders import (
    ForegroundMarker, Removed, attempt_folder_removal, cleanup_incomplete,
    mark_cleanup_incomplete as mark_cleanup_incomplete, report_removal_refusal,
)


class NoCleanupLease(Enum):
    NONE = auto()


class CleanupLeaseUse(Enum):
    KEEP_IN_SUPERVISOR = "keep"
    SHARE_WITH_COORDINATOR = "share"


class CommandLifetime(Enum):
    FOREGROUND = "foreground"
    GROUP = "group"
    VERIFY_STEP = "verify-step"


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
        if owns_group:
            if ended and group_has_live_members(child.pid, timeout=max(0, deadline - time.monotonic())):
                _ = stop_group(child.pid, signal.SIGKILL)
            if not wait_for_group_exit(child.pid, deadline=deadline, interval=cleanup_timing().retry_seconds):
                print(f"foreground cleanup is incomplete for group {child.pid}", file=sys.stderr)
                return 2
        if lifetime is CommandLifetime.VERIFY_STEP:
            pending = Path(os.environ["VERIFY_BATCH_PLACEMENT_PENDING_FILE"])
            try:
                _ = pending.lstat()
            except FileNotFoundError:
                pass
            else:
                print("foreground cleanup is incomplete for the placement client", file=sys.stderr)
                return 2
        marker.finish(lease)
    except subprocess.TimeoutExpired:
        return 2
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"could not finish foreground cleanup for {command[0]}: {error}", file=sys.stderr)
        return 2
    return status if status >= 0 else 128 - status


def main(argv: Sequence[str]) -> int:
    command, *arguments = argv
    if command == "run":
        owner, group, lease = map(int, arguments[:3])
        return run_command(arguments[6:], owner, group, lease, CleanupLeaseUse(arguments[4]),
                           marker=ForegroundMarker[arguments[3]], lifetime=CommandLifetime(arguments[5]))
    if command == "remove":
        result = attempt_folder_removal(Path(arguments[0]), wait=cleanup_timing())
        if isinstance(result, Removed):
            return 0
        report_removal_refusal(result)
        return 2
    if command in ("cancel-group", "group-exit"):
        group = int(arguments[0])
        try:
            if command == "cancel-group":
                signum = int(arguments[1]) if len(arguments) > 1 else signal.SIGTERM
                _ = stop_group(group, signum)
                if "--wait" not in arguments[2:]:
                    return 0
            timing = cleanup_timing()
            if wait_for_group_exit(group, deadline=time.monotonic() + timing.wait_seconds,
                                   interval=timing.retry_seconds):
                return 0
            print(f"process group {group} still has live members", file=sys.stderr)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            print(f"could not clean up process group {group}: {error}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
