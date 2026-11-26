"""Prove which process holds a lifecycle lock before sending a signal."""
from __future__ import annotations

import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path
from threading import Event
from typing import IO, cast
from uuid import uuid4

from scripts.lib import process_start, process_stop


@dataclass(frozen=True)
class HolderRecord:
    pid: int
    command: str
    start: str
    restart: bool = False
    restart_scope: str = ""

    @property
    def description(self) -> str:
        return f"pid {self.pid}: {self.command}"


@dataclass(frozen=True)
class ProvedLockHolder:
    record: HolderRecord
    lock_path: Path
    mode: str
    device: int
    inode: int


class HolderEnd(Enum):
    ENDED = "signal sent and exit confirmed"
    RELEASED = "holder released before the stop completed"
    UNCONFIRMED = "holder exit not confirmed"


_RESTART_OPERATION: ContextVar[str] = ContextVar("_RESTART_OPERATION", default="")


@contextmanager
def restart_operation() -> Generator[None]:
    token = _RESTART_OPERATION.set(uuid4().hex)
    try:
        yield
    finally:
        _RESTART_OPERATION.reset(token)


def own_fields() -> dict[str, object]:
    pid = os.getpid()
    start = process_start.process_start(pid)
    fields: dict[str, object] = {"pid": pid, "command": shlex.join(sys.argv),
                                "start": start.start if isinstance(start, process_start.Started) else ""}
    if scope := _RESTART_OPERATION.get():
        fields["restart"] = scope
    return fields


def _restart_command(command: str) -> bool:
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return any(Path(arg).name == "restart.py" or (Path(arg).name == "shutdown.py" and (
               args[index + 1:index + 2] in (["restart"], ["reboot"])
               or args[index + 1:index + 2] == ["stop"] and "--all" in args[index + 2:]))
               for index, arg in enumerate(args[:4]))


def read_fields(stream: IO[bytes]) -> dict[str, object]:
    try:
        size = os.fstat(stream.fileno()).st_size
        data = os.pread(stream.fileno(), size, 0).partition(b"\n")[0]
        value = cast(object, json.loads(data))
    except (OSError, ValueError, RecursionError):
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def holder_record(fields: dict[str, object]) -> HolderRecord | None:
    pid, command, start = fields.get("pid"), fields.get("command"), fields.get("start", "")
    if type(pid) is int and pid > 1 and isinstance(command, str) and command and isinstance(start, str):
        scope = fields.get("restart")
        scope = scope if isinstance(scope, str) else ""
        return HolderRecord(pid, command, start, bool(scope) or _restart_command(command), scope)
    return None


def read_record(stream: IO[bytes]) -> HolderRecord | None:
    return holder_record(read_fields(stream))


def _parent(pid: int) -> int:
    if sys.platform == "linux":
        fields = (process_stop.PROC / str(pid) / "stat").read_bytes().rpartition(b")")[2].split()
        return int(fields[1])
    result = subprocess.run(["ps", "-p", str(pid), "-o", "ppid="],
                            capture_output=True, text=True, timeout=2, check=True)
    return int(result.stdout)


def _outside_move(pid: int) -> bool:
    visited: set[int] = set()
    try:
        while pid > 1 and pid not in visited:
            if pid == os.getpid():
                return False
            visited.add(pid)
            pid = _parent(pid)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False
    return pid in (0, 1)


def _linux_locked_fd(entry: Path, target: os.stat_result, mode: str) -> bool:
    try:
        opened = entry.stat()
        if (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino):
            return False
        info = (entry.parent.with_name("fdinfo") / entry.name).read_text(encoding="ascii")
        flags = re.search(r"^flags:\s*([0-7]+)$", info, re.MULTILINE)
        if flags is None or (int(flags[1], 8) & os.O_ACCMODE) not in (os.O_WRONLY, os.O_RDWR):
            return False
        locked = re.search(rf"^lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+{mode}\s+-?\d+\s+([\da-f]+):([\da-f]+):(\d+)\s",
                           info, re.MULTILINE)
        return (locked is not None and
                (int(locked[1], 16), int(locked[2], 16), int(locked[3])) ==
                (os.major(target.st_dev), os.minor(target.st_dev), target.st_ino))
    except (OSError, UnicodeError):
        return False


def _holds_linux_lock(pid: int, target: os.stat_result, mode: str) -> bool:
    try:
        return any(_linux_locked_fd(entry, target, mode)
                   for entry in (process_stop.PROC / str(pid) / "fd").iterdir())
    except OSError:
        return False


def _linux_holders(target: os.stat_result, mode: str, records: dict[int, HolderRecord]) -> list[HolderRecord]:
    holders: list[HolderRecord] = []
    # Include any process that inherited a locked descriptor.
    pids = dict.fromkeys((*records, *(int(entry.name) for entry in process_stop.PROC.iterdir()
                                    if entry.name.isascii() and entry.name.isdecimal())))
    for pid in pids:
        folder = process_stop.PROC / str(pid)
        start = process_start.proc_table_start(pid, process_stop.PROC)
        if (isinstance(start, process_start.Started) and _holds_linux_lock(pid, target, mode)
                and _outside_move(pid) and process_start.proc_table_start(pid, process_stop.PROC) == start):
            saved = records.get(pid)
            if saved is not None and saved.start and saved.start != start.start:
                continue
            if saved is not None and saved.start == start.start:
                holders.append(saved)
                continue
            try:
                args = (folder / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                command = shlex.join(os.fsdecode(arg) for arg in args)
            except OSError:
                command = ""
            if command:
                holders.append(HolderRecord(pid, command, start.start, _restart_command(command)))
    return holders


def _mac_holders(path: Path, mode: str, records: dict[int, HolderRecord]) -> list[HolderRecord]:
    result = subprocess.run(["lsof", "-nP", "-Fpfalc", "--", str(path)],
                            capture_output=True, text=True, timeout=2, check=True)
    holders: dict[int, HolderRecord] = {}
    pid = 0
    access = lock = command = ""
    for line in [*result.stdout.splitlines(), "f"]:
        if line.startswith(("p", "f")):
            saved = records.get(pid)
            expected = "W" if mode == "WRITE" else "R"
            if access in ("u", "w") and (lock == expected or (not lock.strip() and saved is not None)):
                start = process_start.process_start(pid)
                if isinstance(start, process_start.Started) and _outside_move(pid):
                    if saved is not None and saved.start == start.start:
                        holders[pid] = saved
                    elif saved is None and lock == expected and command:
                        full = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                              capture_output=True, text=True, timeout=2, check=True).stdout.strip()
                        if full:
                            holders[pid] = HolderRecord(pid, full, start.start, _restart_command(full))
            access = lock = ""
        if line.startswith("p"):
            pid = int(line[1:])
            command = ""
        elif line.startswith("a"):
            access = line[1:]
        elif line.startswith("l"):
            lock = line[1:]
        elif line.startswith("c"):
            command = line[1:]
    return list(holders.values())


def prove(path: Path, records: list[HolderRecord], *, shared: bool = False) -> ProvedLockHolder | None:
    """Return one proved holder. A Mac record can identify a lock that lsof cannot show."""
    by_pid = {record.pid: record for record in records}
    mode = "READ" if shared else "WRITE"
    try:
        target = path.stat()
        holders = (_mac_holders(path, mode, by_pid) if sys.platform == "darwin"
                   else _linux_holders(target, mode, by_pid))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if shared:
        holders = [holder for holder in holders if by_pid.get(holder.pid) == holder]
    else:
        if any(record.restart for record in records):
            holders = [holder for holder in holders if by_pid.get(holder.pid) == holder]
        recorded = [holder for holder in holders if by_pid.get(holder.pid) == holder]
        if len(recorded) == 1:
            holders = recorded
    if len(holders) != 1:
        return None
    return ProvedLockHolder(holders[0], path, mode, target.st_dev, target.st_ino)


def _mac_alive(holder: HolderRecord) -> bool:
    start = process_start.process_start(holder.pid)
    if isinstance(start, process_start.Unreadable):
        raise OSError(start.reason)
    return start == process_start.Started(holder.start)


@dataclass(frozen=True)
class MacSignalTarget:
    proof: ProvedLockHolder
    recheck: Callable[[], ProvedLockHolder | None]


@dataclass(frozen=True)
class PidfdSignalTarget:
    proof: ProvedLockHolder
    pidfd: int
    recheck: Callable[[], ProvedLockHolder | None]


SignalTarget = MacSignalTarget | PidfdSignalTarget
SignalGuard = Callable[[ProvedLockHolder, Callable[[], None]], bool]


def _send(target: SignalTarget, sig: signal.Signals, send_if_waiting: SignalGuard | None) -> bool:
    proof = target.proof
    holder = proof.record
    if isinstance(target, MacSignalTarget):
        if target.recheck() != proof or not _mac_alive(holder):
            return False
        send = partial(os.kill, holder.pid, sig)
    else:
        if holder.restart and target.recheck() != proof:
            return False
        try:
            current = proof.lock_path.stat()
        except FileNotFoundError:
            return False
        if ((current.st_dev, current.st_ino) != (proof.device, proof.inode)
                or not _holds_linux_lock(holder.pid, current, proof.mode) or not _outside_move(holder.pid)):
            return False
        send = partial(signal.pidfd_send_signal, target.pidfd, sig)
    if send_if_waiting is not None:
        return send_if_waiting(proof, send)
    send()
    return True


def _exited(target: SignalTarget) -> bool:
    if isinstance(target, MacSignalTarget):
        return not _mac_alive(target.proof.record)
    ready = select.poll()
    ready.register(target.pidfd, select.POLLIN)
    return bool(ready.poll(0))


SignalGroup = tuple[SignalTarget, ...]
HolderOutcome = Callable[[ProvedLockHolder, HolderEnd], None]


def _wait_for_exits(pending: list[SignalGroup], report: HolderOutcome, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    wake = Event()
    while pending:
        for group in tuple(pending):
            try:
                if not _exited(group[0]):
                    continue
                outcome = HolderEnd.ENDED
            except OSError:
                outcome = HolderEnd.UNCONFIRMED
            for target in group:
                report(target.proof, outcome)
            pending.remove(group)
        remaining = deadline - time.monotonic()
        if not pending or remaining <= 0:
            return
        _ = wake.wait(min(0.05, remaining))


def end_all(proofs: Mapping[ProvedLockHolder, Callable[[], ProvedLockHolder | None]],
            seconds: float, *, publish: HolderOutcome, send_if_waiting: SignalGuard | None = None) -> None:
    """Send TERM to all proved holders before the shared grace period."""
    pending: list[SignalGroup] = []
    lifetimes: dict[process_stop.ProcessLifetime, list[ProvedLockHolder]] = {}
    for proof in proofs:
        holder = proof.record
        lifetime = process_stop.ProcessLifetime(holder.pid, holder.start)
        lifetimes.setdefault(lifetime, []).append(proof)
    with ExitStack() as pinned:
        for lifetime, held in lifetimes.items():
            try:
                if sys.platform == "darwin":
                    group: SignalGroup = tuple(MacSignalTarget(proof, proofs[proof]) for proof in held)
                else:
                    pidfd = pinned.enter_context(process_stop.pin_process(lifetime))
                    group = tuple(PidfdSignalTarget(proof, pidfd, proofs[proof]) for proof in held)
                if any(_send(target, signal.SIGTERM, send_if_waiting) for target in group):
                    pending.append(group)
                    continue
                outcome = HolderEnd.RELEASED
            except ProcessLookupError:
                outcome = HolderEnd.RELEASED
            except OSError:
                outcome = HolderEnd.UNCONFIRMED
            for proof in held:
                publish(proof, outcome)
        _wait_for_exits(pending, publish, seconds)
        for group in tuple(pending):
            try:
                if any(_send(target, signal.SIGKILL, send_if_waiting) for target in group):
                    continue
                outcome = HolderEnd.ENDED if _exited(group[0]) else HolderEnd.RELEASED
            except ProcessLookupError:
                outcome = HolderEnd.ENDED
            except OSError:
                outcome = HolderEnd.UNCONFIRMED
            for target in group:
                publish(target.proof, outcome)
            pending.remove(group)
        _wait_for_exits(pending, publish, seconds)
        for group in pending:
            for target in group:
                publish(target.proof, HolderEnd.UNCONFIRMED)
