"""Stop an exact Linux process lifetime or its dedicated command tree."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import sys

from scripts.lib import process_start


PROC = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True)
class ProcessLifetime:
    pid: int
    start: str


@dataclass(frozen=True)
class ScopeUnit:
    """A systemd scope name and its parent slice."""

    slice: str
    unit: str


@dataclass(frozen=True)
class NoScopeUnit:
    """The caller has no dedicated scope to stop."""


@dataclass(frozen=True)
class _PinnedScope:
    """An open scope directory and its verified member pidfds."""

    directory: int
    members: dict[int, int]


def supported() -> bool:
    return sys.platform == "linux" and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")


def _require_live(process: ProcessLifetime) -> None:
    if process_start.proc_table_start(process.pid, PROC) != process_start.Started(process.start):
        raise ProcessLookupError(f"the lifetime of pid {process.pid} changed")


@contextmanager
def pin_process(process: ProcessLifetime) -> Generator[int]:
    """Open a pidfd and then confirm the exact live process."""
    if not supported():
        raise OSError("this system does not support pidfd stop")
    if process.pid <= 1:
        raise ProcessLookupError("the command PID is invalid")
    descriptor = os.pidfd_open(process.pid)
    try:
        _require_live(process)
        yield descriptor
    finally:
        os.close(descriptor)


def _members(leader_pid: int) -> list[ProcessLifetime]:
    """Read the IDs and start identities in one stat read per process."""
    members: list[ProcessLifetime] = []
    for entry in PROC.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
            if (int(fields[2]) == int(fields[3]) == leader_pid and fields[19].isdigit()
                    and fields[0] not in (b"Z", b"X", b"x")):
                members.append(ProcessLifetime(int(entry.name), fields[19].decode("ascii")))
        except (OSError, ValueError, IndexError):
            continue
    return members


def _scope_path(pid: int, scope: ScopeUnit) -> Path | NoScopeUnit:
    """Find the candidate scope in the process's cgroup record."""
    try:
        for line in (PROC / str(pid) / "cgroup").read_bytes().splitlines():
            if line.startswith(b"0::"):
                path = Path(os.fsdecode(line[3:]))
                if (path.is_absolute() and ".." not in path.parts
                        and path.name == scope.unit and path.parent.name == scope.slice):
                    return CGROUP_ROOT / path.relative_to("/")
    except (ProcessLookupError, FileNotFoundError):
        pass
    return NoScopeUnit()


def _scope_members(directory: int) -> set[int]:
    descriptor = os.open("cgroup.procs", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        return {int(pid) for pid in stream.read().split()}


def _pin_scope(path: Path, anchor: int, anchor_fd: int, group: dict[int, int],
               cleanup: ExitStack) -> _PinnedScope:
    """Verify the anchor and pin the members of the open scope."""
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _ = cleanup.callback(os.close, directory)
    pids = _scope_members(directory)
    if anchor not in pids:
        raise ProcessLookupError("the process is outside the open scope")
    members = {pid: group[pid] for pid in pids if pid in group}
    for pid in pids - members.keys():
        start = process_start.proc_table_start(pid, PROC)
        if isinstance(start, process_start.Started):
            with suppress(ProcessLookupError):
                members[pid] = cleanup.enter_context(pin_process(ProcessLifetime(pid, start.start)))
    # A PID can change owners between the membership read and the pidfd open.
    current_members = _scope_members(directory)
    poller = select.poll()
    poller.register(anchor_fd, select.POLLIN)
    if anchor not in current_members or poller.poll(0):
        raise ProcessLookupError("the process left during scope verification")
    return _PinnedScope(directory, {pid: fd for pid, fd in members.items() if pid in current_members})


def _kill_scope(scope: _PinnedScope) -> None:
    descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=scope.directory)
    try:
        _ = os.write(descriptor, b"1\n")
    finally:
        os.close(descriptor)


def stop_command_tree(leader: ProcessLifetime, scope: ScopeUnit | NoScopeUnit, number: int) -> None:
    """Signal by pidfd and stop remaining members of the owned scope."""
    with ExitStack() as cleanup:
        leader_fd = cleanup.enter_context(pin_process(leader))
        descriptors = {leader.pid: leader_fd}
        for process in _members(leader.pid):
            if process.pid in descriptors:
                continue
            with suppress(ProcessLookupError):
                descriptors[process.pid] = cleanup.enter_context(pin_process(process))
        scopes: dict[Path, _PinnedScope] = {}
        for pid, descriptor in descriptors.items():
            try:
                if isinstance(scope, ScopeUnit) and isinstance(path := _scope_path(pid, scope), Path):
                    if path not in scopes:
                        scopes[path] = _pin_scope(path, pid, descriptor, descriptors, cleanup)
            except (ProcessLookupError, FileNotFoundError):
                continue
            except OSError as error:
                print(f"process stop: cannot open the command scope: {error}", file=sys.stderr)
        _require_live(leader)
        signal.pidfd_send_signal(leader_fd, number)
        signaled = {leader.pid}
        for pinned in scopes.values():
            descriptors.update(pinned.members)
            if number == signal.SIGKILL:
                try:
                    _kill_scope(pinned)
                except OSError as error:
                    print(f"process stop: cannot stop the command scope: {error}", file=sys.stderr)
                else:
                    signaled.update(pinned.members)
        for pid, descriptor in descriptors.items():
            if pid not in signaled:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, number)
