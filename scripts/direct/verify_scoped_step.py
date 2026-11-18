"""Keep a placed step in its scope until all its processes exit."""

from __future__ import annotations

from contextlib import ExitStack
import errno
import os
from pathlib import Path
import select
import subprocess
import sys

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.build_queue.admission_records import process_start_time


# The scope shell publishes its path before this check. A dead owner permits no start.
CHECK_SCOPE_OWNER = r'''
live_owner() {
    read -r stat < "/proc/$1/stat" || return 1
    set -- "$2" ${stat##*) }
    [ "$2" != Z ] && [ "$2" != X ] && [ "$1" = "${21}" ]
}
read -r scope < "$1" && [ "${scope##*/}" = "$2" ] && [ -r "$scope/cgroup.events" ] || exit 125
live_owner "$3" "$4" && live_owner "$5" "$6" || exit 125
shift 6
exec "$@"
'''


def open_pidfd(pid: int, cleanup: ExitStack) -> int:
    descriptor = os.pidfd_open(pid)
    _ = cleanup.callback(os.close, descriptor)
    return descriptor


def scope_populated(events: int) -> bool:
    try:
        _ = os.lseek(events, 0, os.SEEK_SET)
        return b"populated 1" in os.read(events, 4096).splitlines()
    except OSError as error:
        if error.errno == errno.ENODEV:
            return False
        raise


def drain_scope(sidecar: Path) -> None:
    """Stop any remaining members before the shell releases the grant."""
    try:
        scope = Path(sidecar.read_text().strip())
        events = os.open(scope / "cgroup.events", os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        if scope_populated(events):
            try:
                _ = subprocess.run(["systemctl", "--user", "kill", "--signal=KILL", "--kill-whom=all",
                                    scope.name], check=False)
            except OSError as error:
                print(f"verify.sh: cannot stop the build scope ({error})", file=sys.stderr)
            ready = select.poll()
            ready.register(events, select.POLLPRI | select.POLLERR)
            while scope_populated(events):
                _ = ready.poll()
    finally:
        os.close(events)


def run_scoped_step(caller: int, leader: int, peak: str, scope_shell: str, command: list[str]) -> int:
    caller_start = process_start_time(caller)
    leader_start = process_start_time(leader)
    if caller_start is None or leader_start is None:
        return 125

    # Keep the launcher in the step group. The supervisor must survive a stop of that group.
    os.setpgid(0, 0)
    unit = f"verify-step-{os.getpid()}.scope"
    sidecar = Path(peak + ".cgroup")
    with ExitStack() as cleanup:
        owners = select.poll()
        owners.register(open_pidfd(caller, cleanup), select.POLLIN)
        owners.register(open_pidfd(leader, cleanup), select.POLLIN)
        if owners.poll(0):
            return 125
        with subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
             "--slice=builds.slice", f"--unit={unit}", "--",
             "/bin/sh", "-c", scope_shell, peak, "/bin/sh", "-c", CHECK_SCOPE_OWNER,
             "verify-scope-owner", str(sidecar), unit, str(caller), caller_start, str(leader), leader_start,
             *command],
            stderr=subprocess.DEVNULL, pass_fds=(3,), process_group=leader,
        ) as process:
            try:
                owners.register(open_pidfd(process.pid, cleanup), select.POLLIN)
                _ = owners.poll()
            finally:
                if process.poll() is None:
                    process.kill()
                status = process.wait()
                drain_scope(sidecar)
    if not sidecar.is_file():
        print("verify.sh: the local step did not enter its build scope", file=sys.stderr)
        return 125
    return status if status >= 0 else 128 - status


def main(argv: list[str]) -> int:
    caller, leader, peak, scope_shell, *command = argv
    try:
        return run_scoped_step(int(caller), int(leader), peak, scope_shell, command)
    except (OSError, ValueError) as error:
        print(f"verify.sh: cannot run the local step in its build scope ({error})", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
