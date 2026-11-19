"""Read Mac process arguments for Builds and claim settlement."""

import ctypes
from dataclasses import dataclass
from enum import Enum
import inspect
import os
import shlex
import struct
import subprocess
import sys


@dataclass(frozen=True)
class ProcessArgumentStrings:
    """The strings from a kernel response that fits in the supplied buffer."""

    arguments: tuple[bytes, ...]
    environment_and_metadata: tuple[bytes, ...]

    def environment_fields(self) -> dict[bytes, bytes]:
        """Read named fields before any padding after the environment."""
        fields: dict[bytes, bytes] = {}
        for field in self.environment_and_metadata:
            if not field:
                break
            key, separator, value = field.partition(b"=")
            if not key or not separator:
                return {}
            fields[key] = value
        return fields


@dataclass(frozen=True)
class ProcessInspectionUnavailable:
    """The reader lacks enough information to check the process."""

    reason: str


class PlacementObservation(Enum):
    FOUND = "the response contains the marker"
    NOT_FOUND = "the full response contains no marker"
    EXITED = "the kernel no longer has the PID"


def read_process_arguments(pid: int) -> ProcessArgumentStrings | ProcessInspectionUnavailable:
    """Read the Mac process argument buffer with space to detect a short copy."""
    if sys.platform != "darwin":
        return ProcessInspectionUnavailable("the platform has no Mac process argument reader")
    try:
        kernel = ctypes.CDLL(None, use_errno=True)
        kernel.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        kernel.sysctl.restype = ctypes.c_int
        # CTL_KERN and KERN_PROCARGS2 read the process argument buffer.
        mib = (ctypes.c_int * 3)(1, 49, pid)
        size = ctypes.c_size_t()
        if kernel.sysctl(mib, 3, None, ctypes.byref(size), None, 0) or not 4 <= size.value <= 2097152:
            return ProcessInspectionUnavailable("the process argument size is unavailable")
        # XNU can fill a buffer with a short copy and still report success.
        capacity = size.value + 1
        data = ctypes.create_string_buffer(capacity)
        size.value = capacity
        if kernel.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
            return ProcessInspectionUnavailable("the process arguments are unavailable")
        if size.value >= capacity:
            return ProcessInspectionUnavailable("the process argument buffer filled its capacity")
        return parse_process_arguments(data.raw[:size.value])
    except (OSError, ValueError, AttributeError):
        return ProcessInspectionUnavailable("the process argument read failed")


def observe_process_placement(pid: int, marker: bytes) -> PlacementObservation | ProcessInspectionUnavailable:
    """Check all strings for the exact marker, or confirm process exit."""
    observed = read_process_arguments(pid)
    if isinstance(observed, ProcessArgumentStrings):
        if marker in observed.arguments or marker in observed.environment_and_metadata:
            return PlacementObservation.FOUND
        if any(observed.environment_and_metadata):
            return PlacementObservation.NOT_FOUND
        observed = ProcessInspectionUnavailable("the response contains no content after argv")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return PlacementObservation.EXITED
    except OSError:
        return ProcessInspectionUnavailable("the process environment and exit are unconfirmed")
    return observed


def parse_process_arguments(data: bytes) -> ProcessArgumentStrings | ProcessInspectionUnavailable:
    """Separate argv from the remaining strings without splitting environment and metadata."""
    invalid = ProcessInspectionUnavailable("the process argument buffer is invalid")
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
    return ProcessArgumentStrings(tuple(fields[:argc]), tuple(fields[argc:-1]))


def print_placement_pids(placement: str) -> int:
    """Print matching process IDs after all live processes of this user were checked."""
    uid = os.getuid()
    rows = subprocess.check_output(["ps", "-axo", "pid=,uid=,stat="], text=True)
    marker = os.fsencode("MAC_PLACEMENT=" + placement)
    matches: list[int] = []
    listed = False
    for row in rows.splitlines():
        pid, owner, state = row.split()
        if int(owner) != uid or state.startswith("Z"):
            continue
        listed = True
        observed = observe_process_placement(int(pid), marker)
        if isinstance(observed, ProcessInspectionUnavailable):
            print(f"cannot check Mac process {pid}: {observed.reason}", file=sys.stderr)
            return 1
        if observed is PlacementObservation.FOUND:
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
