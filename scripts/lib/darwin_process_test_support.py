"""Supply a native process response to the shared Darwin probe."""

from collections.abc import Generator
from contextlib import contextmanager
import ctypes
from typing import cast
from unittest.mock import patch

from scripts.lib import darwin_process
from scripts.lib.darwin_process import _DarwinProcessBSDInfo, _NativeProcessRead  # pyright: ignore[reportPrivateUsage]


@contextmanager
def native_process_read(read: _NativeProcessRead) -> Generator[None]:
    with patch.object(darwin_process, "_proc_pidinfo", return_value=read):
        yield


def write_process_info(buffer: object, size: int, info: darwin_process.DarwinProcessInfo) -> int:
    assert size == ctypes.sizeof(_DarwinProcessBSDInfo)
    response = ctypes.cast(cast(ctypes.c_void_p, buffer), ctypes.POINTER(_DarwinProcessBSDInfo)).contents
    response.pbi_pid = info.pid
    response.pbi_ppid = info.parent_pid
    response.pbi_status = info.status
    response.e_tdev = info.terminal_device
    response.pbi_start_tvsec, response.pbi_start_tvusec = divmod(info.started_us, darwin_process.MICROSECONDS_PER_SECOND)
    return size
