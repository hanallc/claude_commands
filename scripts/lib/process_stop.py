"""Stop an exact Linux process lifetime or its dedicated command tree."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import sys
import time
from typing import TypeGuard, TypedDict

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


class ScopeIdentity(TypedDict):
    """The directory identity of the command scope at launch."""

    unit: str
    path: str
    device: int
    inode: int


def is_scope_identity(value: object) -> TypeGuard[ScopeIdentity]:
    match value:
        case {"unit": str(), "path": str(), "device": int(device), "inode": int(inode)}:
            return type(device) is int and type(inode) is int
        case _:
            return False


class ScopeStopFailed(OSError):
    """The stop could not confirm an empty scope."""


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


def _cgroup_path(pid: int) -> Path:
    for line in (PROC / str(pid) / "cgroup").read_bytes().splitlines():
        if line.startswith(b"0::"):
            return Path(os.fsdecode(line[3:]))
    raise FileNotFoundError("the process has no cgroup v2 entry")


def _directory_identity(unit: str, path: Path, directory: int) -> ScopeIdentity:
    stat = os.fstat(directory)
    return {"unit": unit, "path": str(path), "device": stat.st_dev, "inode": stat.st_ino}


def _open_directory(path: Path | str, *, parent: int | None = None) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _read_cgroup_file(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def command_scope(pid: int, scope: ScopeUnit) -> ScopeIdentity:
    """Read the dedicated scope while the launcher is inside it."""
    path = _cgroup_path(pid)
    if not path.is_absolute() or ".." in path.parts or path.name != scope.unit or path.parent.name != scope.slice:
        raise ValueError("the command did not enter its dedicated scope")
    directory = _open_directory(CGROUP_ROOT / path.relative_to("/"))
    try:
        if pid not in _scope_pids(directory):
            raise ProcessLookupError("the launcher is outside its scope")
        return _directory_identity(scope.unit, path, directory)
    finally:
        os.close(directory)


def _open_saved_scope(scope: ScopeIdentity) -> int:
    path = Path(scope["path"])
    if (not path.is_absolute() or ".." in path.parts or path.name != scope["unit"]
            or path.parent.name != "builds-detached.slice" or not path.name.endswith(".scope")):
        raise ValueError("invalid command scope path")
    directory = _open_directory(CGROUP_ROOT / path.relative_to("/"))
    if _directory_identity(scope["unit"], path, directory) != scope:
        os.close(directory)
        raise FileNotFoundError("the original scope is gone")
    return directory


def scope_has_ended(scope: ScopeIdentity) -> bool:
    """Use the kernel event for the full scope tree."""
    try:
        directory = _open_saved_scope(scope)
    except FileNotFoundError:
        # The kernel removes a cgroup only after its tree is empty.
        return True
    except (OSError, ValueError):
        return False
    try:
        return b"populated 0" in _read_cgroup_file(directory, "cgroup.events").splitlines()
    except OSError:
        return False
    finally:
        os.close(directory)


def _scope_pids(directory: int) -> set[int]:
    return {int(pid) for pid in _read_cgroup_file(directory, "cgroup.procs").split()}


def _scope_processes(directory: int) -> set[ProcessLifetime]:
    processes: set[ProcessLifetime] = set()
    for pid in _scope_pids(directory):
        started = process_start.proc_table_start(pid, PROC)
        if isinstance(started, process_start.Started):
            processes.add(ProcessLifetime(pid, started.start))
    with os.scandir(directory) as entries:
        children = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
    for name in children:
        try:
            child = _open_directory(name, parent=directory)
        except (FileNotFoundError, NotADirectoryError):
            continue
        try:
            processes.update(_scope_processes(child))
        except FileNotFoundError:
            pass
        finally:
            os.close(child)
    return processes


def _in_scope(process: ProcessLifetime, scope: ScopeIdentity) -> bool:
    with suppress(FileNotFoundError, ProcessLookupError):
        path = _cgroup_path(process.pid)
        return path.is_relative_to(scope["path"])
    return False


def _term_scope(directory: int, scope: ScopeIdentity, deadline: float) -> bool:
    seen: set[ProcessLifetime] = set()
    reached = False
    while time.monotonic() < deadline:
        members = _scope_processes(directory) - seen
        if not members:
            break
        for process in members:
            if time.monotonic() >= deadline:
                return reached
            try:
                with pin_process(process) as descriptor:
                    if _in_scope(process, scope):
                        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                        reached = True
            except OSError:
                continue
        seen.update(members)
    return reached


def _scope_ends_by(scope: ScopeIdentity, deadline: float) -> bool:
    while not scope_has_ended(scope):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def stop_scope(scope: ScopeIdentity, grace: float, kill_grace: float) -> None:
    """Send TERM, then use cgroup.kill if the scope stays populated."""
    deadline = time.monotonic() + grace
    try:
        directory = _open_saved_scope(scope)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        raise ScopeStopFailed(str(error)) from error
    try:
        reached = False
        with suppress(OSError):
            reached = _term_scope(directory, scope, deadline)
        if _scope_ends_by(scope, deadline):
            return
        try:
            descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _ = os.write(descriptor, b"1\n")
            finally:
                os.close(descriptor)
        except OSError as error:
            if scope_has_ended(scope):
                return
            detail = "" if reached else "no TERM signal reached a member. "
            raise ScopeStopFailed(f"{detail}cgroup.kill failed: {error}") from error
        if not _scope_ends_by(scope, time.monotonic() + kill_grace):
            raise ScopeStopFailed("the scope remains populated after KILL")
    finally:
        os.close(directory)


def stop_group(leader: ProcessLifetime, number: int) -> None:
    """Signal the original group while the leader identity matches."""
    reached = False
    with pin_process(leader) as leader_fd:
        members = _members(leader.pid)
        _require_live(leader)
        for member in members:
            if member.pid == leader.pid:
                continue
            with suppress(ProcessLookupError, PermissionError):
                with pin_process(member) as descriptor:
                    signal.pidfd_send_signal(descriptor, number)
                    reached = True
        try:
            signal.pidfd_send_signal(leader_fd, number)
        except (ProcessLookupError, PermissionError):
            if not reached:
                raise
