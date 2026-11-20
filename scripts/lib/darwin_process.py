"""Read process information from Darwin libproc."""

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
import errno
from functools import cache
from typing import cast, final


# Darwin ABI values.
PROC_PIDTBSDINFO = 3
SSTOP = 4
SZOMB = 5
MICROSECONDS_PER_SECOND = 1_000_000


@dataclass(frozen=True)
class DarwinProcessInfo:
    pid: int
    parent_pid: int
    started_us: int
    status: int
    terminal_device: int


class DarwinProcessInfoUnavailable(OSError):
    """The native read did not supply valid process information."""


@final
class _DarwinProcessBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]
    pbi_status: int = 0
    pbi_pid: int = 0
    pbi_ppid: int = 0
    e_tdev: int = 0
    pbi_start_tvsec: int = 0
    pbi_start_tvusec: int = 0


_NativeProcessRead = Callable[[int, int, int, object, int], int]


@cache
def libproc() -> ctypes.CDLL:
    """Load libproc on the first native call."""
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


@cache
def _proc_pidinfo() -> _NativeProcessRead:
    return cast(_NativeProcessRead, ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
        ctypes.c_void_p, ctypes.c_int, use_errno=True)(("proc_pidinfo", libproc())))


def read_process_info(pid: int, *, include_zombies: bool = False) -> DarwinProcessInfo:
    info = _DarwinProcessBSDInfo()
    size = ctypes.sizeof(info)
    try:
        read = _proc_pidinfo()
        _ = ctypes.set_errno(0)
        count = read(pid, PROC_PIDTBSDINFO, int(include_zombies), ctypes.byref(info), size)
    except (OSError, AttributeError, OverflowError, ctypes.ArgumentError) as error:
        code = error.errno if isinstance(error, OSError) else errno.EIO
        if isinstance(error, ProcessLookupError):
            code = errno.ESRCH
        raise DarwinProcessInfoUnavailable(code, f"process {pid}: libproc read failed") from error
    if count != size:
        raise DarwinProcessInfoUnavailable(ctypes.get_errno(), f"process {pid}: libproc returned a short read")
    if pid <= 0 or info.pbi_pid != pid:
        raise DarwinProcessInfoUnavailable(errno.EIO, f"process {pid}: libproc returned an invalid PID")
    if info.pbi_start_tvsec <= 0 or not 0 <= info.pbi_start_tvusec < MICROSECONDS_PER_SECOND:
        raise DarwinProcessInfoUnavailable(errno.EIO, f"process {pid}: libproc returned an invalid start time")
    started_us = info.pbi_start_tvsec * MICROSECONDS_PER_SECOND + info.pbi_start_tvusec
    return DarwinProcessInfo(pid, info.pbi_ppid, started_us, info.pbi_status, info.e_tdev)
