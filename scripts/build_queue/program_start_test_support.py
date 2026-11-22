"""Start a queued program in a private test scope."""
from pathlib import Path
import sys
from unittest import mock

from scripts.build_queue import program_start


def private_scope_start() -> None:
    """Use the real startup rule with a private kernel path at its input boundary."""
    private_slice, *argv = sys.argv[1:]
    if argv[:1] != ["--scope-start"]:
        raise AssertionError("the private scope wrapper received another action")
    lines = Path("/proc/self/cgroup").read_text().splitlines()
    scope = next(Path(line[4:]) for line in lines if line.startswith("0::/"))
    if private_slice not in scope.parts:
        raise AssertionError("the program did not enter the private test slice")
    with mock.patch.object(program_start, "program_scope", return_value=Path("/sys/fs/cgroup") / scope):
        raise SystemExit(program_start.main(argv))
