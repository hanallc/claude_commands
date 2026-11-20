"""Identify test processes and signal their audit tokens on macOS."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import os
import time
from typing import cast, final

from scripts.lib.darwin_process import DarwinProcessInfoUnavailable, SSTOP, SZOMB, libproc, read_process_info
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

# These values are Darwin ABI selectors, not test policy.
PROC_UID_ONLY = 4
PROC_PPID_ONLY = 6
PROC_PIDUNIQIDENTIFIERINFO = 17
CTL_KERN = 1
KERN_PROCARGS2 = 49


@final
class ProcessIdentityInfo(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_ubyte * 16), ("unique_id", ctypes.c_uint64),
                ("parent_unique_id", ctypes.c_uint64), ("version", ctypes.c_int32),
                ("parent_version", ctypes.c_int32), ("reserved", ctypes.c_uint64 * 2)]


@dataclass(frozen=True)
class OwnedProcessIdentity:
    pid: int
    unique_id: int
    version: int


def identity(pid: int) -> OwnedProcessIdentity | None:
    library = libproc()
    info = ProcessIdentityInfo()
    size = cast(int, library.proc_pidinfo(pid, PROC_PIDUNIQIDENTIFIERINFO, 1,
                                         ctypes.byref(info), ctypes.sizeof(info)))
    if size != ctypes.sizeof(info):
        error = ctypes.get_errno()
        if error in (0, errno.ESRCH, errno.ENOENT):
            return None
        raise OSError(error, "cannot read test process identity")
    return OwnedProcessIdentity(pid, cast(int, info.unique_id), cast(int, info.version))


def list_processes(selector: int, value: int) -> list[int]:
    library = libproc()
    size = cast(int, library.proc_listpids(selector, value, None, 0))
    if size == 0:
        return []
    if size < 0:
        raise OSError(ctypes.get_errno(), "cannot list test processes")
    buffer = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int)))()
    used = cast(int, library.proc_listpids(selector, value, buffer, size))
    if used == 0:
        return []
    if used < 0:
        raise OSError(ctypes.get_errno(), "cannot list test processes")
    pids = cast(list[int], buffer[:used // ctypes.sizeof(ctypes.c_int)])
    return [pid for pid in pids if pid > 0]


def user_processes() -> list[int]:
    return list_processes(PROC_UID_ONLY, os.getuid())


def child_processes() -> set[int]:
    return set(list_processes(PROC_PPID_ONLY, os.getpid()))


def has_owner(pid: int, token: str) -> bool:
    library = ctypes.CDLL(None, use_errno=True)
    capacity = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(capacity))
    result = cast(int, library.sysctlbyname(b"kern.argmax", ctypes.byref(capacity),
                                          ctypes.byref(size), None, 0))
    if result:
        raise OSError(ctypes.get_errno(), "cannot read the argument buffer size")
    buffer = ctypes.create_string_buffer(capacity.value)
    size.value = capacity.value
    selector = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    result = cast(int, library.sysctl(selector, len(selector), buffer, ctypes.byref(size), None, 0))
    if result:
        if ctypes.get_errno() in (errno.ESRCH, errno.EINVAL, errno.EPERM):
            return False
        raise OSError(ctypes.get_errno(), "cannot read test process ownership")
    prefix = b"MESSAGE_TEST_PROCESS_OWNERS="
    return any(value.startswith(prefix) and token.encode() in value[len(prefix):].split(b":")
               for value in buffer.raw[:size.value].split(b"\0"))


def signal_identity(owned: OwnedProcessIdentity, signum: int) -> None:
    """Let the kernel reject a reused PID or an identity changed by exec."""
    deadline = time.monotonic() + TEST_HANG_GUARD_SECS
    while True:
        current = identity(owned.pid)
        if current is None or current.unique_id != owned.unique_id:
            return
        token = (ctypes.c_uint32 * 8)()
        token[5], token[7] = current.pid, current.version
        library = libproc()
        error = cast(int, library.proc_signal_with_audittoken(ctypes.byref(token), signum))
        if error == 0:
            return
        if error != errno.ESRCH:
            raise OSError(error, "cannot signal an owned test process")
        after = identity(owned.pid)
        if after is None or after.unique_id != owned.unique_id:
            return
        if after.version == current.version:
            return  # The same process can remain visible as a zombie.
        if time.monotonic() >= deadline:
            raise AssertionError(f"test process {owned.pid} did not stop changing its executable")


def process_state(owned: OwnedProcessIdentity) -> str:
    try:
        current = identity(owned.pid)
        if current is None or current.unique_id != owned.unique_id:
            return ""
        info = read_process_info(owned.pid, include_zombies=True)
        after = identity(owned.pid)
        if after is None or after.unique_id != owned.unique_id:
            return ""
    except DarwinProcessInfoUnavailable as error:
        if error.errno not in (0, errno.ESRCH, errno.ENOENT):
            raise
        return ""
    except ProcessLookupError:
        return ""
    return "T" if info.status == SSTOP else "Z" if info.status == SZOMB else "R"
