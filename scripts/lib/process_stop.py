"""Stop an exact Linux process lifetime or its dedicated command tree."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import sys
from typing import TypeGuard, TypedDict, cast

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


class ScopeIdentity(TypedDict):
    """A scope directory that a live command member verified."""

    path: str
    device: int
    inode: int


def _is_scope_identity(value: object) -> bool:
    match value:
        case {"path": str(), "device": int(device), "inode": int(inode)}:
            return type(device) is int and type(inode) is int
        case _:
            return False


def scope_identities(value: object) -> TypeGuard[list[ScopeIdentity]]:
    return isinstance(value, list) and all(_is_scope_identity(scope) for scope in cast(list[object], value))


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


def _open_scope(path: Path, cleanup: ExitStack) -> int:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    _ = cleanup.callback(os.close, directory)
    return directory


def _verify_scope(directory: int, anchor: int, anchor_fd: int) -> None:
    poller = select.poll()
    poller.register(anchor_fd, select.POLLIN)
    if anchor not in _scope_members(directory) or poller.poll(0):
        raise ProcessLookupError("the process is outside the open scope")


def _scope_identity(path: Path, directory: int) -> ScopeIdentity:
    stat = os.fstat(directory)
    return {"path": str(path.relative_to(CGROUP_ROOT)), "device": stat.st_dev, "inode": stat.st_ino}


def _open_saved_scope(scope: ScopeIdentity, cleanup: ExitStack) -> int:
    relative = Path(scope["path"])
    if relative == Path(".") or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid saved scope path")
    path = CGROUP_ROOT / relative
    directory = _open_scope(path, cleanup)
    if _scope_identity(path, directory) != scope:
        raise FileNotFoundError("the original scope no longer exists")
    return directory


def scopes_have_ended(scopes: list[ScopeIdentity]) -> bool:
    """Confirm that every saved scope is empty or gone."""
    for scope in scopes:
        try:
            with ExitStack() as cleanup:
                if _scope_members(_open_saved_scope(scope, cleanup)):
                    return False
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return False
    return True


def _kill_scope(directory: int) -> None:
    descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        _ = os.write(descriptor, b"1\n")
    finally:
        os.close(descriptor)


def _send_signal(descriptor: int, number: int) -> None:
    try:
        signal.pidfd_send_signal(descriptor, number)
    except ProcessLookupError:
        pass
    except OSError as error:
        print(f"process stop: cannot signal a command member: {error}", file=sys.stderr)


def _signal_scope(directory: int, number: int, group: dict[int, int], signaled: set[int]) -> bool:
    """Send one signal to each scope member, including new members."""
    members = _scope_members(directory)
    if not members:
        return False
    if number == signal.SIGKILL:
        try:
            _kill_scope(directory)
        except OSError as error:
            print(f"process stop: cannot stop the command scope: {error}", file=sys.stderr)
        else:
            signaled.update(members)
            return True

    seen: set[int] = set()
    while new_members := members - seen:
        with ExitStack() as cleanup:
            pinned = {pid: group[pid] for pid in new_members - signaled if pid in group}
            for pid in new_members - pinned.keys() - signaled:
                start = process_start.proc_table_start(pid, PROC)
                if isinstance(start, process_start.Started):
                    with suppress(ProcessLookupError):
                        pinned[pid] = cleanup.enter_context(pin_process(ProcessLifetime(pid, start.start)))
            # Check membership after opening the pidfds.
            current = _scope_members(directory)
            for pid in pinned.keys() & current:
                _send_signal(pinned[pid], number)
                signaled.add(pid)
        seen.update(new_members)
        members = _scope_members(directory)
    return True


def stop_saved_scopes(scopes: list[ScopeIdentity], number: int) -> bool:
    """Signal saved scopes while their directory identities still match."""
    found = False
    signaled: set[int] = set()
    for scope in scopes:
        try:
            with ExitStack() as cleanup:
                directory = _open_saved_scope(scope, cleanup)
                found = _signal_scope(directory, number, {}, signaled) or found
        except FileNotFoundError:
            continue
    return found


def stop_command_tree(
    leader: ProcessLifetime, scope: ScopeUnit | NoScopeUnit, number: int,
    *, remember_scopes: Callable[[list[ScopeIdentity]], None] | None = None,
) -> None:
    """Save the verified scopes before sending signals."""
    with ExitStack() as cleanup:
        leader_fd = cleanup.enter_context(pin_process(leader))
        descriptors = {leader.pid: leader_fd}
        for process in _members(leader.pid):
            if process.pid in descriptors:
                continue
            with suppress(ProcessLookupError):
                descriptors[process.pid] = cleanup.enter_context(pin_process(process))
        _require_live(leader)
        scopes: dict[Path, int] = {}
        for pid, descriptor in descriptors.items():
            try:
                if isinstance(scope, ScopeUnit) and isinstance(path := _scope_path(pid, scope), Path):
                    if path not in scopes:
                        directory = _open_scope(path, cleanup)
                        _verify_scope(directory, pid, descriptor)
                        scopes[path] = directory
            except (ProcessLookupError, FileNotFoundError):
                continue
            except OSError as error:
                print(f"process stop: cannot open the command scope: {error}", file=sys.stderr)
        if scopes and remember_scopes is not None:
            remember_scopes([_scope_identity(path, directory) for path, directory in scopes.items()])
        _send_signal(leader_fd, number)
        signaled = {leader.pid}
        for directory in scopes.values():
            try:
                _ = _signal_scope(directory, number, descriptors, signaled)
            except FileNotFoundError:
                continue
            except OSError as error:
                print(f"process stop: cannot read the command scope: {error}", file=sys.stderr)
        for pid, descriptor in descriptors.items():
            if pid not in signaled:
                _send_signal(descriptor, number)
