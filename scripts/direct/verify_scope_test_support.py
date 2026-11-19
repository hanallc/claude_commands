"""Provide a scope launcher for fixtures that use fake Cargo."""

from datetime import datetime
import os
from pathlib import Path
import shlex
import shutil
import sys
from unittest import SkipTest, TestCase, mock
from scripts.build_queue import admission_records as records
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.lint import memory_admit as gate


def write_test_scope_launcher(directory: Path) -> None:
    manager = directory / "systemd-run"
    _ = manager.write_text(r'''#!/bin/sh
set -eu
for word in "$@"; do
    case "$word" in --unit=*) unit=${word#--unit=} ;; esac
    [ "$word" != -- ] || break
    shift
done
shift
peak=$4
shift 4
scope="$TMPDIR/$unit"
mkdir "$scope"
printf 'populated 0\n' > "$scope/cgroup.events"
printf '%s\n' "$scope" > "$peak.cgroup"
: > "$peak"
exec "$@" 2>&1
''')
    manager.chmod(0o755)


def write_real_scope_launcher(directory: Path) -> None:
    """Use the host user manager with a private fixture environment."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    launcher = shutil.which("systemd-run")
    if sys.platform != "linux" or not launcher or not runtime or not (Path(runtime) / "systemd/private").is_socket():
        raise SkipTest("build scopes require a systemd user manager")
    manager = directory / "systemd-run"
    _ = manager.write_text(f"""#!/bin/sh
export XDG_RUNTIME_DIR={shlex.quote(runtime)}
exec {shlex.quote(launcher)} "$@"
""")
    manager.chmod(0o755)


def use_fixture_cgroup_roots(test: TestCase) -> None:
    """Keep cgroup discovery private while a test restores the environment."""
    builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
    ci = Path(os.environ["BUILDLOG_CI_CGROUP"])
    _ = test.enterContext(mock.patch.object(gate, "builds_slice_cgroup", return_value=builds))
    _ = test.enterContext(mock.patch.object(gate, "systemd_slice_group", return_value=ci))


def use_fixture_scope_readings(test: TestCase, root: Path, admission: AdmissionFixture) -> None:
    """Permit membership reads from test scopes and supply private memory values."""
    guard = gate.check_memory_reading
    reservation = admission.readings.reservation

    scope_membership: set[Path] = set()

    def membership_only(path: Path) -> None:
        if path in scope_membership:
            return
        if path.name == "cgroup.procs":
            for sidecar in (root / "tmp").glob("buildlog.*.peak.cgroup"):
                try:
                    if sidecar.read_text().strip() == str(path.parent):
                        scope_membership.add(path)
                        return
                except FileNotFoundError:
                    pass
        guard(path)

    def scoped_memory(path: Path, row: records.Reservation, now: datetime,
                      processes: dict[int, gate.ProcessIdentity], proc: Path,
                      policy: records.AdmissionPolicy) -> records.StepMemory:
        if row["sidecar"]:
            return records.MeasuredMemory(records.GIB)
        return reservation(path, row, now, processes, proc, policy)

    # Read process membership from the test's scope. Supply memory separately.
    _ = test.enterContext(mock.patch.object(gate, "check_memory_reading", side_effect=membership_only))
    _ = test.enterContext(mock.patch.object(admission.readings, "reservation", side_effect=scoped_memory))
