"""Start a queued program in a private test scope."""
import os
from collections.abc import Sequence
from pathlib import Path
import sys
from unittest import mock

from scripts.lib import atomic_file
from scripts.build_queue import program_start
from scripts.build_queue.admission_wire import Owner


def private_scope_start() -> None:
    """Use the real startup rule with a private kernel path at its input boundary. The published sidecar, the
    exported step scope and the program record get the actual path back."""
    private_slice, *startup_argv = sys.argv[1:]
    if startup_argv[:1] != ["--scope-start"]:
        raise AssertionError("the private scope wrapper received another action")
    cgroup_path = Path("/proc/self/cgroup")
    actual = cgroup_path.read_text()
    if not any(private_slice in Path(line[3:]).parts for line in actual.splitlines() if line.startswith("0::")):
        raise AssertionError("the program did not enter the private test slice")
    read_text = Path.read_text
    publish = atomic_file.atomic_sample
    start_program = program_start.program_start

    def actual_path(value: str) -> str:
        return value.replace("/sys/fs/cgroup/builds.slice/", "/sys/fs/cgroup/", 1)

    def kernel_path(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == cgroup_path:
            return actual.replace("0::/", "0::/builds.slice/")
        return read_text(path, encoding=encoding, errors=errors)

    def publish_actual_path(path: Path, value: str) -> None:
        publish(path, actual_path(value))

    def start_in_actual_scope(record: Path, holder: Owner, argv: Sequence[str], *, scope: str = "") -> None:
        if program_start.STEP_SCOPE_ENV in os.environ:
            os.environ[program_start.STEP_SCOPE_ENV] = actual_path(os.environ[program_start.STEP_SCOPE_ENV])
        start_program(record, holder, argv, scope=actual_path(scope))

    with mock.patch.object(Path, "read_text", new=kernel_path), \
         mock.patch.object(atomic_file, "atomic_sample", side_effect=publish_actual_path), \
         mock.patch.object(program_start, "program_start", side_effect=start_in_actual_scope):
        raise SystemExit(program_start.main(startup_argv))
