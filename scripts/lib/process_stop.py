"""Stop an exact Linux process lifetime or its dedicated command tree."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, suppress
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


class _ScopeRemoved(FileNotFoundError):
    """The kernel removed the empty scope tree."""


@dataclass(frozen=True)
class ScopeStopReceipt:
    term_reached: int


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
    if (not path.is_absolute() or ".." in path.parts or path.name != scope.unit
            or not scope.unit.endswith(".scope") or path.parent.name != scope.slice):
        raise ValueError("the command did not enter its dedicated scope")
    directory = _open_directory(CGROUP_ROOT / path.relative_to("/"))
    try:
        if pid not in _scope_pids(directory):
            raise ProcessLookupError("the launcher is outside its scope")
        return _directory_identity(scope.unit, path, directory)
    finally:
        os.close(directory)


def publish_command_scope(record: Path, scope: ScopeUnit) -> None:
    """Save the launcher's scope before it execs the command."""
    with record.with_suffix(".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fields = cast(dict[str, object], json.loads(record.read_text()))
        if fields.get("run_id") != record.stem or fields.get("status") != "running" or fields.get("stop_reason"):
            raise ValueError("the run is no longer starting")
        fields["command_scope"] = command_scope(os.getpid(), scope)
        atomic_file.write_text_atomic(record, json.dumps(fields, indent=2, sort_keys=True))


def launch_in_scope(record: Path, scope: ScopeUnit, command: list[str]) -> int:
    try:
        publish_command_scope(record, scope)
    except (OSError, ValueError) as error:
        print(f"detached_runs: command scope did not start: {error}", file=sys.stderr)
        return 125
    try:
        os.execvp(command[0], command)
    except OSError as error:
        print(f"detached_runs: command did not start: {error}", file=sys.stderr)
        return 127


def legacy_command_scope(leader: ProcessLifetime, unit: ScopeUnit) -> ScopeIdentity | None:
    """Verify an old scope through a member of the original command group."""
    with suppress(OSError):
        with pin_process(leader):
            for member in (leader, *_members(leader.pid)):
                with suppress(OSError, ValueError):
                    with pin_process(member):
                        scope = command_scope(member.pid, unit)
                        _require_live(member)
                        _require_live(leader)
                        return scope
    return None


def moved_command_scope(leader: ProcessLifetime, original: ScopeIdentity) -> ScopeIdentity | None:
    """Verify a new scope under the same user manager."""
    with suppress(OSError, ValueError):
        with pin_process(leader):
            current = _cgroup_path(leader.pid)
            previous = Path(original["path"])
            manager = f"user@{os.getuid()}.service"
            owner = next((parent for parent in previous.parents if parent.name == manager), None)
            if (owner is None or not current.is_relative_to(owner) or current == previous
                    or ".." in current.parts or not current.name.endswith(".scope")):
                return None
            scope = command_scope(leader.pid, ScopeUnit(current.parent.name, current.name))
            _require_live(leader)
            return scope
    return None


def _open_saved_scope(scope: ScopeIdentity) -> int:
    path = Path(scope["path"])
    if (not path.is_absolute() or ".." in path.parts or path.name != scope["unit"]
            or not path.name.endswith(".scope")):
        raise ScopeStopFailed("cannot open command scope: invalid command scope path")
    try:
        directory = _open_directory(CGROUP_ROOT / path.relative_to("/"))
    except FileNotFoundError as error:
        raise _ScopeRemoved from error
    except OSError as error:
        raise ScopeStopFailed(f"cannot open command scope: {error}") from error
    if _directory_identity(scope["unit"], path, directory) != scope:
        os.close(directory)
        raise ScopeStopFailed("cannot open command scope: the directory identity changed")
    return directory


def _check_saved_scope(scope: ScopeIdentity) -> None:
    os.close(_open_saved_scope(scope))


def scope_has_ended(scope: ScopeIdentity) -> bool:
    """Use the kernel event for the full scope tree."""
    with suppress(_ScopeRemoved), _scope_events(scope) as (_, descriptor):
        return _scope_is_empty(scope, descriptor)
    return True


@contextmanager
def _scope_events(scope: ScopeIdentity) -> Generator[tuple[int, int]]:
    directory = _open_saved_scope(scope)
    try:
        try:
            descriptor = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except OSError as error:
            _check_saved_scope(scope)
            raise ScopeStopFailed(f"cannot read cgroup.events: {error}") from error
        try:
            yield directory, descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _scope_is_empty(scope: ScopeIdentity, descriptor: int) -> bool:
    try:
        lines = os.pread(descriptor, 4096, 0).splitlines()
        if b"populated 0" in lines:
            return True
        if b"populated 1" in lines:
            return False
        raise ValueError("no populated field in cgroup.events")
    except (OSError, ValueError) as error:
        _check_saved_scope(scope)
        raise ScopeStopFailed(f"cannot read cgroup.events: {error}") from error


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


def _term_scope(directory: int, scope: ScopeIdentity, deadline: float) -> int:
    seen: set[ProcessLifetime] = set()
    reached = 0
    while time.monotonic() < deadline:
        try:
            members = _scope_processes(directory) - seen
        except (OSError, ValueError):
            break
        if not members:
            break
        for process in members:
            if time.monotonic() >= deadline:
                return reached
            try:
                with pin_process(process) as descriptor:
                    if _in_scope(process, scope):
                        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                        reached += 1
            except OSError:
                continue
        seen.update(members)
    return reached


def scope_ends_by(scope: ScopeIdentity, deadline: float) -> bool:
    with suppress(_ScopeRemoved), _scope_events(scope) as (_, descriptor):
        return _scope_ends_on(scope, descriptor, deadline)
    return True


def _wait_scope_event(descriptor: int, seconds: float) -> None:
    events = select.poll()
    events.register(descriptor, select.POLLPRI)
    _ = events.poll(seconds * 1000)


def _scope_ends_on(scope: ScopeIdentity, descriptor: int, deadline: float) -> bool:
    while not _scope_is_empty(scope, descriptor):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _wait_scope_event(descriptor, remaining)
    return True


def scope_member_pids(scope: ScopeIdentity) -> list[int]:
    """List the live process IDs in the scope tree."""
    with suppress(_ScopeRemoved):
        directory = _open_saved_scope(scope)
        try:
            return sorted({process.pid for process in _scope_processes(directory)})
        except OSError:
            _check_saved_scope(scope)
            raise
        finally:
            os.close(directory)
    return []


def stop_scope(
    scope: ScopeIdentity, grace: float, kill_grace: float, *,
    on_term: Callable[[int], None] | None = None,
) -> ScopeStopReceipt:
    """Report the TERM count before the exit wait."""
    return stop_scopes((scope,), grace, kill_grace, on_term=on_term)


@dataclass(frozen=True)
class _ScopeAwaitingExit:
    scope: ScopeIdentity
    directory: int
    events: int
    term_reached: int
    term_deadline: float


def stop_scopes(
    scopes: tuple[ScopeIdentity, ...], grace: float, kill_grace: float, *,
    on_term: Callable[[int], None] | None = None,
) -> ScopeStopReceipt:
    """Report one total after all TERM sends and before any exit wait."""
    pending: list[_ScopeAwaitingExit] = []
    errors: list[tuple[str, ScopeStopFailed]] = []
    with ExitStack() as opened:
        for scope in scopes:
            deadline = time.monotonic() + grace
            try:
                directory, events = opened.enter_context(_scope_events(scope))
                if not _scope_is_empty(scope, events):
                    reached = _term_scope(directory, scope, deadline)
                    pending.append(_ScopeAwaitingExit(scope, directory, events, reached, deadline))
            except _ScopeRemoved:
                continue
            except ScopeStopFailed as error:
                errors.append((scope["unit"], error))
        total = sum(item.term_reached for item in pending)
        try:
            if on_term is not None:
                on_term(total)
        finally:
            for item in pending:
                try:
                    _finish_scope_stop(item, kill_grace, require_term=on_term is not None)
                except _ScopeRemoved:
                    continue
                except ScopeStopFailed as error:
                    errors.append((item.scope["unit"], error))
    if errors:
        if len(scopes) == 1:
            raise errors[0][1]
        raise ScopeStopFailed("; ".join(f"{unit}: {error}" for unit, error in errors))
    return ScopeStopReceipt(total)


def _finish_scope_stop(item: _ScopeAwaitingExit, kill_grace: float, *, require_term: bool) -> None:
    if _scope_ends_on(item.scope, item.events, item.term_deadline):
        return
    if require_term and not item.term_reached:
        raise ScopeStopFailed("no TERM signal reached a member of the populated scope")
    try:
        descriptor = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=item.directory)
        try:
            _ = os.write(descriptor, b"1\n")
        finally:
            os.close(descriptor)
    except OSError as error:
        if _scope_is_empty(item.scope, item.events):
            return
        detail = "" if item.term_reached else "no TERM signal reached a member. "
        raise ScopeStopFailed(f"{detail}cgroup.kill failed: {error}") from error
    if not _scope_ends_on(item.scope, item.events, time.monotonic() + kill_grace):
        raise ScopeStopFailed("the scope remains populated after KILL")


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
