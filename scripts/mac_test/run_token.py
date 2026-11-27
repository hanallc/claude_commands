"""Identify Mac run processes by their exact environment token."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import ctypes
from dataclasses import dataclass
import errno
from functools import cache, partial
import json
import os
import shlex
import signal
import sys
import time
from typing import Literal, cast

from scripts.mac_test.claim_state import ClaimIdentity


TOKEN_NAME = "MAC_RUN_TOKEN"
TOKEN_ENTRY_PREFIX = f"{TOKEN_NAME}=".encode("ascii")
KILLED_PREFIX = "leftover_killed "
PROC_UID_ONLY = 4
PROC_PIDTBSDINFO = 3
PROC_PIDUNIQIDENTIFIERINFO = 17
PROC_FLAG_LP64 = 0x10
SZOMB = 5
Mode = Literal["observe", "kill", "probe"]


class InvalidRunToken(ValueError):
    """A claim identity cannot form an unambiguous run token."""


def token(identity: ClaimIdentity) -> str:
    for name, component in (("episode", identity.episode), ("attempt", identity.attempt)):
        if any(char in "/=\0" or char.isspace() for char in component):
            raise InvalidRunToken(f"the Mac run token {name} cannot contain '/', '=', NUL, or whitespace")
    return f"{identity.episode}/{identity.attempt}"


def command(mode: Mode, groups: Sequence[Sequence[str]]) -> str:
    program = "from scripts.mac_test.run_token import main; raise SystemExit(main())"
    return '"$HOME/.claude/scripts/lib/py" -c ' + shlex.join([program, mode, json.dumps(groups)])


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    unique_id: int
    version: int


NO_PROCESS_IDS: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Process:
    identity: ProcessIdentity
    tokens: frozenset[str]
    command: str


@dataclass(frozen=True)
class ProcessTable:
    processes: tuple[Process, ...]
    unreadable_pids: frozenset[int] = NO_PROCESS_IDS

    def select(self, tokens: Sequence[str]) -> tuple[Process, ...]:
        selected = (process for process in self.processes if not process.tokens.isdisjoint(tokens))
        return tuple(sorted(selected, key=lambda process: process.identity.pid))


@cache
def _libproc() -> ctypes.CDLL:
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def _user_pids() -> list[int]:
    list_pids = cast(Callable[[int, int, object, int], int], _libproc().proc_listpids)
    uid = os.getuid()
    _ = ctypes.set_errno(0)
    size = list_pids(PROC_UID_ONLY, uid, None, 0)
    if size <= 0:
        raise OSError(ctypes.get_errno(), "cannot list Mac user processes")
    # Allow for processes that start after the size query.
    buffer = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 64))()
    size = ctypes.sizeof(buffer)
    used = list_pids(PROC_UID_ONLY, uid, buffer, size)
    if used <= 0 or used >= size:
        raise OSError(ctypes.get_errno(), "cannot read the Mac user process list")
    pids = cast(list[int], buffer[:used // ctypes.sizeof(ctypes.c_int)])
    return [pid for pid in pids if pid > 0]


def _process_info(pid: int, kind: int, info: ctypes.Structure) -> bool:
    read_info = cast(Callable[[int, int, int, object, int], int], _libproc().proc_pidinfo)
    _ = ctypes.set_errno(0)
    if read_info(pid, kind, 1, ctypes.byref(info), ctypes.sizeof(info)) == ctypes.sizeof(info):
        return True
    error = ctypes.get_errno()
    if error in (errno.ESRCH, errno.ENOENT):
        return False
    raise OSError(error, "cannot read the Mac process identity")


def _run_environment(payload: bytes, pointer_width: int) -> tuple[frozenset[str], str]:
    count = int.from_bytes(payload[:4], sys.byteorder, signed=True)
    executable, separator, rest = payload[4:].partition(b"\0")
    # XNU aligns argv relative to the executable path after the argc header.
    padding = (-len(executable) - 1) % pointer_width
    words = rest[padding:].split(b"\0")
    if not (count > 0 and executable and separator and payload.endswith(b"\0")
            and rest[:padding] == bytes(padding) and len(words) > count):
        raise ValueError("the Mac process argument record is malformed")
    entries = [word for word in words[count:] if word]
    if not words[0] or not entries:
        raise ValueError("the Mac process environment is unavailable")
    # The kernel strings after the environment do not use the run token name.
    tokens = frozenset(os.fsdecode(entry.removeprefix(TOKEN_ENTRY_PREFIX))
                       for entry in entries if entry.startswith(TOKEN_ENTRY_PREFIX))
    return tokens, os.fsdecode(b" ".join(words[:count]))[:200]


def mac_processes() -> ProcessTable:
    """Read exact environment entries for the current user's processes."""
    from scripts.message.darwin_test_processes import ProcessIdentityInfo, ProcessStateInfo
    from scripts.whoami.account import kern_procargs2

    processes: list[Process] = []
    unreadable: set[int] = set()
    for pid in _user_pids():
        if pid == os.getpid():
            continue
        state = ProcessStateInfo()
        before = ProcessIdentityInfo()
        after = ProcessIdentityInfo()
        try:
            if not _process_info(pid, PROC_PIDUNIQIDENTIFIERINFO, before):
                continue
            if (not _process_info(pid, PROC_PIDTBSDINFO, state) or cast(int, state.status) == SZOMB
                    or cast(int, state.real_uid) != os.getuid() or cast(int, state.uid) != os.geteuid()):
                continue
            pointer_width = 8 if cast(int, state.flags) & PROC_FLAG_LP64 else 4
            tokens, command_text = _run_environment(kern_procargs2(pid), pointer_width)
            if not _process_info(pid, PROC_PIDUNIQIDENTIFIERINFO, after):
                continue
        except (OSError, ValueError):
            try:
                if not _process_info(pid, PROC_PIDTBSDINFO, state) or cast(int, state.status) == SZOMB:
                    continue
            except OSError:
                pass
            unreadable.add(pid)
            continue
        identity = ProcessIdentity(pid, cast(int, after.unique_id), cast(int, after.version))
        if identity != ProcessIdentity(pid, cast(int, before.unique_id), cast(int, before.version)):
            unreadable.add(pid)
            continue
        processes.append(Process(identity, tokens, command_text))
    return ProcessTable(tuple(processes), frozenset(unreadable))


def signal_process(identity: ProcessIdentity, signum: int) -> bool:
    """Signal the process only if the kernel still recognizes its PID version."""
    send = cast(Callable[[object, int], int], _libproc().proc_signal_with_audittoken)
    audit = (ctypes.c_uint32 * 8)()
    audit[5], audit[7] = identity.pid, identity.version
    return send(ctypes.byref(audit), signum) == 0


def report(mode: Mode, groups: Sequence[Sequence[str]], *,
           read: Callable[[], ProcessTable] = mac_processes,
           send: Callable[[ProcessIdentity, int], bool] = signal_process,
           pause: Callable[[float], None] = time.sleep,
           write: Callable[[str], None] = print) -> int:
    if not any(groups):
        return 0
    table = read()
    if mode == "probe":
        if not table.unreadable_pids:
            for index, tokens in enumerate(groups):
                for process in table.select(tokens):
                    write(f"{index} {process.identity.pid}")
        return int(bool(table.unreadable_pids))

    tokens = groups[0]
    signaled: dict[int, Process] = {}
    unresolved = bool(table.unreadable_pids)
    for signum in (signal.SIGTERM, signal.SIGKILL) if mode == "kill" else ():
        selected = table.select(tokens)
        if not selected:
            break
        for process in selected:
            if send(process.identity, signum):
                signaled[process.identity.unique_id] = process
        if signum == signal.SIGTERM:
            pause(1)
        table = read()
        unresolved = unresolved or bool(table.unreadable_pids)
        live = {process.identity.unique_id for process in table.processes}
        for key, process in tuple(signaled.items()):
            if key not in live and process.identity.pid not in table.unreadable_pids:
                command_text = "".join(char if char.isprintable() else " " for char in process.command[:200])
                write(f"{KILLED_PREFIX}pid={process.identity.pid} command={command_text}")
                del signaled[key]
    for process in table.select(tokens):
        write(str(process.identity.pid))
    return int(unresolved)


def main() -> int:
    mode = sys.argv[1]
    if mode not in ("observe", "kill", "probe"):
        return 2
    groups = cast(list[list[str]], json.loads(sys.argv[2]))
    try:
        return report(mode, groups, write=partial(print, flush=True))
    except OSError as error:
        print(f"Mac run process check failed: {error}", file=sys.stderr)
        return 1
