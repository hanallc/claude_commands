"""Stop an exact Linux process lifetime or its dedicated command tree."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sys
import time
from typing import TypeGuard, TypedDict, cast

from scripts.lib import atomic_file, process_start


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


class RunSliceIdentity(TypedDict):
    """The directory identity of one detached run slice."""

    unit: str
    path: str
    device: int
    inode: int


def is_slice_identity(value: object) -> TypeGuard[RunSliceIdentity]:
    match value:
        case {"unit": str(), "path": str(), "device": int(device), "inode": int(inode)}:
            return type(device) is int and type(inode) is int
        case _:
            return False


class SliceStopFailed(OSError):
    """The stop could not confirm an empty run slice."""


class _SliceRemoved(FileNotFoundError):
    """The kernel removed the empty run slice."""


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


def _directory_identity(unit: str, path: Path, directory: int) -> RunSliceIdentity:
    stat = os.fstat(directory)
    return {"unit": unit, "path": str(path), "device": stat.st_dev, "inode": stat.st_ino}


def _open_directory(path: Path | str, *, parent: int | None = None) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _read_cgroup_file(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


DETACHED_SLICE_ENV = "DETACHED_RUN_SLICE"


def scope_slice_args(default: str = "", *, environment: Mapping[str, str] = os.environ) -> list[str]:
    """Use the detached run slice or the caller's usual slice."""
    name = environment.get(DETACHED_SLICE_ENV) or default
    return [f"--slice={name}"] if name else []


def _is_run_slice_path(path: Path) -> bool:
    prefix = "builds-detached-"
    suffix = path.stem.removeprefix(prefix)
    return (path.anchor == "/" and ".." not in path.parts and "\x00" not in str(path) and path.suffix == ".slice"
            and path.name.startswith(prefix) and suffix.isascii() and suffix.isalnum()
            and path.parent.name == "builds-detached.slice" and path.parent.parent.name == "builds.slice")


def command_slice(pid: int, scope: ScopeUnit) -> RunSliceIdentity:
    """Identify the run slice while the launcher is in its command scope."""
    path = _cgroup_path(pid)
    if (path.name != scope.unit or path.suffix != ".scope" or path.parent.name != scope.slice
            or not _is_run_slice_path(path.parent)):
        raise ValueError("the command did not enter its dedicated slice")
    directory = _open_directory(CGROUP_ROOT / path.parent.relative_to("/"))
    try:
        child = _open_directory(path.name, parent=directory)
        try:
            if pid not in _cgroup_pids(child):
                raise ProcessLookupError("the launcher is outside its command scope")
        finally:
            os.close(child)
        return _directory_identity(scope.slice, path.parent, directory)
    finally:
        os.close(directory)


def publish_run_slice(record: Path, scope: ScopeUnit) -> None:
    """Save the run slice before it execs the command."""
    with record.with_suffix(".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fields = cast(dict[str, object], json.loads(record.read_text()))
        if fields.get("run_id") != record.stem or fields.get("status") != "running" or fields.get("stop_reason"):
            raise ValueError("the run is no longer starting")
        fields["command_slice"] = command_slice(os.getpid(), scope)
        atomic_file.write_text_atomic(record, json.dumps(fields, indent=2, sort_keys=True))


def launch_in_slice(record: Path, scope: ScopeUnit, command: list[str]) -> int:
    try:
        publish_run_slice(record, scope)
    except (OSError, ValueError) as error:
        print(f"detached_runs: command scope did not start: {error}", file=sys.stderr)
        return 125
    try:
        os.environ[DETACHED_SLICE_ENV] = scope.slice
        os.execvp(command[0], command)
    except OSError as error:
        print(f"detached_runs: command did not start: {error}", file=sys.stderr)
        return 127


def _open_saved_slice(scope: RunSliceIdentity) -> int:
    path = Path(scope["path"])
    if path.name != scope["unit"] or not _is_run_slice_path(path):
        raise SliceStopFailed("cannot open run slice: invalid run slice path")
    try:
        directory = _open_directory(CGROUP_ROOT / path.relative_to("/"))
    except FileNotFoundError as error:
        raise _SliceRemoved from error
    except OSError as error:
        raise SliceStopFailed(f"cannot open run slice: {error}") from error
    if _directory_identity(scope["unit"], path, directory) != scope:
        os.close(directory)
        raise SliceStopFailed("cannot open run slice: the directory identity changed")
    return directory


def _check_saved_slice(scope: RunSliceIdentity) -> None:
    os.close(_open_saved_slice(scope))


def slice_has_ended(scope: RunSliceIdentity) -> bool:
    """Use the kernel event for the full run slice."""
    with suppress(_SliceRemoved), _slice_events(scope) as (_, descriptor):
        return _slice_is_empty(scope, descriptor)
    return True


@contextmanager
def _slice_events(scope: RunSliceIdentity) -> Generator[tuple[int, int]]:
    directory = _open_saved_slice(scope)
    try:
        try:
            descriptor = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except OSError as error:
            _check_saved_slice(scope)
            raise SliceStopFailed(f"cannot read cgroup.events: {error}") from error
        try:
            yield directory, descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _slice_is_empty(scope: RunSliceIdentity, descriptor: int) -> bool:
    try:
        lines = os.pread(descriptor, 4096, 0).splitlines()
        if b"populated 0" in lines:
            return True
        if b"populated 1" in lines:
            return False
        raise ValueError("no populated field in cgroup.events")
    except (OSError, ValueError) as error:
        _check_saved_slice(scope)
        raise SliceStopFailed(f"cannot read cgroup.events: {error}") from error


def _cgroup_pids(directory: int) -> set[int]:
    return {int(pid) for pid in _read_cgroup_file(directory, "cgroup.procs").split()}


def _cgroup_processes(directory: int) -> set[ProcessLifetime]:
    processes: set[ProcessLifetime] = set()
    for pid in _cgroup_pids(directory):
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
            processes.update(_cgroup_processes(child))
        except FileNotFoundError:
            pass
        finally:
            os.close(child)
    return processes


def _in_slice(process: ProcessLifetime, scope: RunSliceIdentity) -> bool:
    with suppress(FileNotFoundError, ProcessLookupError):
        path = _cgroup_path(process.pid)
        return path.is_relative_to(scope["path"])
    return False


def _term_slice(directory: int, scope: RunSliceIdentity) -> bool:
    try:
        members = _cgroup_processes(directory)
    except (OSError, ValueError):
        # The event file still controls escalation when the member scan fails.
        _check_saved_slice(scope)
        return False
    signaled = False
    for process in members:
        try:
            with pin_process(process) as descriptor:
                if _in_slice(process, scope):
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                    signaled = True
        except OSError:
            continue
    return signaled


def slice_ends_by(scope: RunSliceIdentity, deadline: float) -> bool:
    with suppress(_SliceRemoved), _slice_events(scope) as (_, descriptor):
        return _slice_ends_on(scope, descriptor, deadline)
    return True


def _wait_slice_event(descriptor: int, seconds: float) -> None:
    events = select.poll()
    events.register(descriptor, select.POLLPRI)
    _ = events.poll(seconds * 1000)


def _slice_ends_on(scope: RunSliceIdentity, descriptor: int, deadline: float) -> bool:
    while not _slice_is_empty(scope, descriptor):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _wait_slice_event(descriptor, remaining)
    return True


def stop_slice(
    run_slice: RunSliceIdentity, grace: float, kill_grace: float, *,
    on_effect: Callable[[], None] = lambda: None,
) -> None:
    """Send TERM, then kill the slice if it stays populated through the grace period."""
    if not supported():
        raise SliceStopFailed("this system does not support pidfd stop")
    with suppress(_SliceRemoved), _slice_events(run_slice) as (directory, events):
        if _slice_is_empty(run_slice, events):
            return
        signaled = _term_slice(directory, run_slice)
        if signaled:
            on_effect()
        if _slice_ends_on(run_slice, events, time.monotonic() + grace):
            return
        try:
            descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _ = os.write(descriptor, b"1\n")
            finally:
                os.close(descriptor)
        except OSError as error:
            if _slice_is_empty(run_slice, events):
                return
            raise SliceStopFailed(f"cgroup.kill failed: {error}") from error
        if not signaled:
            on_effect()
        if not _slice_ends_on(run_slice, events, time.monotonic() + kill_grace):
            raise SliceStopFailed("the slice remains populated after KILL")


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
