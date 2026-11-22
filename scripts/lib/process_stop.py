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


def _scope_path(process: ProcessLifetime, scope: ScopeUnit) -> Path | NoScopeUnit:
    """Find the candidate scope in the process's cgroup record."""
    try:
        for line in (PROC / str(process.pid) / "cgroup").read_bytes().splitlines():
            if line.startswith(b"0::"):
                path = Path(os.fsdecode(line[3:]))
                if (path.is_absolute() and ".." not in path.parts
                        and path.name == scope.unit and path.parent.name == scope.slice):
                    return CGROUP_ROOT / path.relative_to("/")
    except (ProcessLookupError, FileNotFoundError):
        pass
    return NoScopeUnit()


def _open_scope_kill(path: Path, process: ProcessLifetime, descriptor: int) -> int:
    """Verify membership in the open directory before opening its kill file."""
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        members = os.open("cgroup.procs", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(members, "rb") as stream:
            if str(process.pid).encode("ascii") not in stream.read().split():
                raise ProcessLookupError("the process is outside the open scope")
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        if poller.poll(0):
            raise ProcessLookupError("the process exited during scope verification")
        return os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


def stop_command_tree(leader: ProcessLifetime, scope: ScopeUnit | NoScopeUnit, number: int) -> None:
    """Signal by pidfd and stop remaining members of the owned scope."""
    with ExitStack() as cleanup:
        leader_fd = cleanup.enter_context(pin_process(leader))
        descriptors: dict[int, int] = {}
        scoped: set[int] = set()
        kill_files: dict[Path, int] = {}
        for process in (leader, *_members(leader.pid)):
            if process.pid in descriptors:
                continue
            try:
                descriptor = (leader_fd if process == leader
                              else cleanup.enter_context(pin_process(process)))
            except ProcessLookupError:
                continue
            descriptors[process.pid] = descriptor
            try:
                if isinstance(scope, ScopeUnit) and isinstance(path := _scope_path(process, scope), Path):
                    if path not in kill_files:
                        kill_file = _open_scope_kill(path, process, descriptor)
                        _ = cleanup.callback(os.close, kill_file)
                        kill_files[path] = kill_file
                    scoped.add(process.pid)
            except (ProcessLookupError, FileNotFoundError):
                continue
            except OSError as error:
                print(f"process stop: cannot open the command scope: {error}", file=sys.stderr)
        _require_live(leader)
        signal.pidfd_send_signal(leader_fd, number)
        for kill_file in kill_files.values():
            try:
                _ = os.write(kill_file, b"1\n")
            except OSError as error:
                print(f"process stop: cannot stop the command scope: {error}", file=sys.stderr)
                scoped.clear()
        for pid, descriptor in descriptors.items():
            if descriptor != leader_fd and pid not in scoped:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, number)
