"""Start a queued program in a private test scope."""
from pathlib import Path
import sys
from unittest import mock

from scripts.lib import atomic_file
from scripts.build_queue import program_start


def private_scope_start() -> None:
    """Use the real startup rule with a private kernel path at its input boundary."""
    private_slice, action, sidecar, holder, start, record, *argv = sys.argv[1:]
    if action != "--scope-start":
        raise AssertionError("the private scope wrapper received another action")
    cgroup_path = Path("/proc/self/cgroup")
    actual = cgroup_path.read_text()
    if not any(private_slice in Path(line[3:]).parts for line in actual.splitlines() if line.startswith("0::")):
        raise AssertionError("the program did not enter the private test slice")
    read_text = Path.read_text
    publish = atomic_file.atomic_sample

    def kernel_path(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == cgroup_path:
            return actual.replace("0::/", "0::/builds.slice/")
        return read_text(path, encoding=encoding, errors=errors)

    def publish_actual_path(path: Path, value: str) -> None:
        publish(path, value.replace("/sys/fs/cgroup/builds.slice/", "/sys/fs/cgroup/", 1))

    with mock.patch.object(Path, "read_text", new=kernel_path), \
         mock.patch.object(atomic_file, "atomic_sample", side_effect=publish_actual_path):
        program_start.scope_start(Path(sidecar), int(holder), start, argv, record=Path(record))

