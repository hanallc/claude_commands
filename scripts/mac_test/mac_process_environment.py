"""Read the Mac process environment for Builds and claim settlement."""

import ctypes
from dataclasses import dataclass
import inspect
import os
import re
import shlex
import struct
import subprocess
import sys


@dataclass(frozen=True)
class ProcessEnvironment:
    """The fields from a complete environment read."""

    fields: dict[str, str]


@dataclass(frozen=True)
class ProcessEnvironmentUnreadable:
    """The reader cannot read or parse the process environment."""

    reason: str


@dataclass(frozen=True)
class ProcessExited:
    """The process no longer has a PID in the kernel."""


def read_process_environment(pid: int) -> ProcessEnvironment | ProcessEnvironmentUnreadable:
    """Read Cargo paths, CI labels and the placement value."""
    if sys.platform != "darwin":
        return ProcessEnvironmentUnreadable("the platform has no Mac process environment reader")
    try:
        kernel = ctypes.CDLL(None, use_errno=True)
        kernel.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        kernel.sysctl.restype = ctypes.c_int
        # CTL_KERN and KERN_PROCARGS2 read the process argument buffer.
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if kernel.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 <= size.value <= 2097152:
            return ProcessEnvironmentUnreadable("the process argument size is unavailable")
        data = ctypes.create_string_buffer(size.value)
        if kernel.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
            return ProcessEnvironmentUnreadable("the process arguments are unavailable")
        return cargo_environment(data.raw[:size.value])
    except (OSError, ValueError, AttributeError):
        return ProcessEnvironmentUnreadable("the process argument read failed")


def observe_process_environment(pid: int) -> ProcessEnvironment | ProcessExited | ProcessEnvironmentUnreadable:
    """Check for exit after an unsuccessful environment read."""
    observed = read_process_environment(pid)
    if isinstance(observed, ProcessEnvironmentUnreadable):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProcessExited()
        except OSError:
            return ProcessEnvironmentUnreadable("the process environment and exit are unconfirmed")
    return observed


def cargo_environment(data: bytes) -> ProcessEnvironment | ProcessEnvironmentUnreadable:
    """Parse the environment from the Mac process argument buffer."""
    invalid = ProcessEnvironmentUnreadable("the process argument buffer is invalid")
    if len(data) < 4:
        return invalid
    argc = int(struct.unpack_from("i", data)[0])
    path_end = data.find(b"\0", 4)
    if argc < 1 or path_end <= 4:
        return invalid
    start = path_end + 1
    while start < len(data) and data[start] == 0:
        start += 1
    fields = data[start:].split(b"\0")
    if len(fields) <= argc or fields[-1]:
        return invalid
    selected: dict[str, str] = {}
    terminated = False
    # split adds a final empty item for the last string's NUL. Skip that item.
    for field in fields[argc:-1]:
        if not field:
            terminated = True
            break
        key, separator, value = field.partition(b"=")
        if not key or not separator:
            return invalid
        # XNU adds this field after the environment, even when alignment adds no padding.
        if key == b"commpage_pfz" and re.fullmatch(rb"0x[0-9a-f]+", value):
            terminated = True
        if key in (b"CARGO_TARGET_DIR", b"CARGO_BUILD_TARGET_DIR", b"CARGO_HOME", b"HOME",
                   b"RUNNER_NAME", b"GITHUB_JOB", b"MAC_PLACEMENT"):
            selected[os.fsdecode(key)] = os.fsdecode(value)
    return ProcessEnvironment(selected) if terminated else invalid


def print_placement_pids(placement: str) -> int:
    """Print matching process IDs after all live processes of this user were checked."""
    uid = os.getuid()
    rows = subprocess.check_output(["ps", "-axo", "pid=,uid=,stat="], text=True)
    matches: list[int] = []
    listed = False
    for row in rows.splitlines():
        pid, owner, state = row.split()
        if int(owner) != uid or state.startswith("Z"):
            continue
        listed = True
        observed = observe_process_environment(int(pid))
        if isinstance(observed, ProcessExited):
            continue
        if isinstance(observed, ProcessEnvironmentUnreadable):
            print(f"cannot check Mac process {pid}: {observed.reason}", file=sys.stderr)
            return 1
        if observed.fields.get("MAC_PLACEMENT") == placement:
            matches.append(int(pid))
    if not listed:
        print("the Mac process list contains no live processes for the SSH user", file=sys.stderr)
        return 1
    for match in matches:
        print(match)
    return 0


def placement_pids_command(episode: str, attempt: str) -> str:
    """Build the remote process check for one exact placement."""
    source = inspect.getsource(sys.modules[__name__])
    program = source + "\nraise SystemExit(print_placement_pids(sys.argv[1]))\n"
    return shlex.join(["python3", "-c", program, f"{episode}/{attempt}"])
