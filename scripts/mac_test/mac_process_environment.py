"""Share the Mac process environment reader across SSH probes."""

import shlex


MAC_PROCESS_ENVIRONMENT = r'''
import ctypes
import os
import struct
import sys


def process_environment(pid):
    """Read only Cargo paths, CI labels and the queue placement from the Mac environment."""
    if sys.platform != "darwin":
        return {}
    try:
        kernel = ctypes.CDLL(None, use_errno=True)
        kernel.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        kernel.sysctl.restype = ctypes.c_int
        # sysinfo uses CTL_KERN and KERN_PROCARGS2 for the same process observation.
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if kernel.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 <= size.value <= 2097152:
            return {}
        data = ctypes.create_string_buffer(size.value)
        if kernel.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
            return {}
        return cargo_environment(data.raw[:size.value])
    except (OSError, ValueError, AttributeError):
        return {}


def cargo_environment(data):
    """Keep only Cargo paths, CI labels and the queue placement from the environment."""
    if len(data) < 4:
        return {}
    argc = struct.unpack_from("i", data)[0]
    if argc < 1:
        return {}
    start = data.find(b"\0", 4)
    if start < 0:
        return {}
    start += 1
    while start < len(data) and data[start] == 0:
        start += 1
    fields = data[start:].split(b"\0")
    selected = {}
    for field in fields[argc:]:
        if not field:
            break
        key, separator, value = field.partition(b"=")
        if separator and key in (b"CARGO_TARGET_DIR", b"CARGO_BUILD_TARGET_DIR", b"CARGO_HOME", b"HOME",
                                 b"RUNNER_NAME", b"GITHUB_JOB", b"MAC_PLACEMENT"):
            selected[os.fsdecode(key)] = os.fsdecode(value)
    return selected
'''


def placement_pids_command(episode: str, attempt: str) -> str:
    """Print the user's processes that carry the exact placement identity."""
    program = MAC_PROCESS_ENVIRONMENT + r'''
import subprocess


placement = sys.argv[1]
uid = os.getuid()
rows = subprocess.check_output(["ps", "-axo", "pid=,uid="], text=True)
for row in rows.splitlines():
    pid, owner = map(int, row.split())
    if owner == uid and process_environment(pid).get("MAC_PLACEMENT") == placement:
        print(pid)
'''
    return shlex.join(["python3", "-c", program, f"{episode}/{attempt}"])
