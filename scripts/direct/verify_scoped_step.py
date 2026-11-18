"""Keep a placed step in its scope until all its processes exit."""

from __future__ import annotations

from contextlib import ExitStack
import errno
import fcntl
import os
from pathlib import Path
import select
import subprocess
import sys
import time

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.build_queue.admission_records import process_start_time


SCOPE_STOP_TIMEOUT_SECONDS = 10


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
        fields = os.read(events, 4096).splitlines()
        if b"populated 1" in fields:
            return True
        if b"populated 0" in fields:
            return False
        raise ValueError("cgroup.events has no populated state")
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
            result = subprocess.run(["systemctl", "--user", "kill", "--signal=KILL", "--kill-whom=all",
                                     scope.name], stderr=subprocess.PIPE, text=True, check=False,
                                    timeout=SCOPE_STOP_TIMEOUT_SECONDS)
            if result.returncode:
                detail = result.stderr.strip() or f"systemctl exited with status {result.returncode}"
                raise OSError(f"cannot stop scope {scope.name}: {detail}")
            ready = select.poll()
            ready.register(events, select.POLLPRI | select.POLLERR)
            deadline = time.monotonic() + SCOPE_STOP_TIMEOUT_SECONDS
            while scope_populated(events):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"build scope {scope.name} is still populated")
                _ = ready.poll(round(remaining * 1000))
    finally:
        os.close(events)


def copy_scope_output(descriptor: int) -> bool:
    try:
        data = os.read(descriptor, 65536)
    except BlockingIOError:
        return False
    if not data:
        return False
    _ = sys.stdout.buffer.write(data)
    _ = sys.stdout.buffer.flush()
    return True


def watch_scope(owners: select.poll, descriptor: int) -> None:
    _ = owners.register(descriptor, select.POLLIN)
    while (ready := owners.poll()) and all(fd == descriptor for fd, _ in ready):
        if not copy_scope_output(descriptor):
            owners.unregister(descriptor)


def run_scoped_step(caller: int, leader: int, release_state: Path, peak: str,
                    scope_shell: str, command: list[str]) -> int:
    # The supervisor survives a stop of the step group. The launcher stays in it.
    os.setpgid(0, 0)
    unit = f"verify-step-{os.getpid()}.scope"
    sidecar = Path(peak + ".cgroup")
    with release_state.open("r+b", buffering=0) as release, ExitStack() as cleanup:
        fcntl.flock(release, fcntl.LOCK_EX)
        if release.read(1):
            return 125
        caller_start = process_start_time(caller)
        leader_start = process_start_time(leader)
        if caller_start is None or leader_start is None:
            return 125
        owners = select.poll()
        owners.register(open_pidfd(caller, cleanup), select.POLLIN)
        owners.register(open_pidfd(leader, cleanup), select.POLLIN)
        if owners.poll(0):
            return 125
        # Keep this state if the scope cannot be proved empty.
        _ = release.write(b"scope may be populated\n")
        try:
            process = subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
                 "--slice=builds.slice", f"--unit={unit}", "--",
                 "/bin/sh", "-c", 'exec 3>&1; ' + scope_shell, peak, "/bin/sh", "-c", CHECK_SCOPE_OWNER,
                 "verify-scope-owner", str(sidecar), unit, str(caller), caller_start, str(leader), leader_start,
                 *command],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, process_group=leader,
            )
            assert process.stdout is not None
            # The scope cannot hold verify's output stream open after a failed kill.
            with process.stdout as output:
                descriptor = output.fileno()
                os.set_blocking(descriptor, False)
                try:
                    owners.register(open_pidfd(process.pid, cleanup), select.POLLIN)
                    watch_scope(owners, descriptor)
                except (ProcessLookupError, BrokenPipeError):
                    pass
                finally:
                    if process.poll() is None:
                        process.kill()
                    status = process.wait(timeout=SCOPE_STOP_TIMEOUT_SECONDS)
                    drain_scope(sidecar)
                    _ = release.truncate(0)
                while copy_scope_output(descriptor):
                    pass
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # The step's log readers can die before the scope stops.
            if release.seek(0, os.SEEK_END):
                _ = release.seek(0)
                _ = release.write(f"cannot finish the local build scope: {error}\n".encode())
                _ = release.truncate()
            return 125
    if not sidecar.is_file():
        print("verify.sh: the local step did not enter its build scope", file=sys.stderr)
        return 125
    return status if status >= 0 else 128 - status


def main(argv: list[str]) -> int:
    caller, leader, release_state, peak, scope_shell, *command = argv
    try:
        return run_scoped_step(int(caller), int(leader), Path(release_state), peak, scope_shell, command)
    except (OSError, ValueError) as error:
        print(f"verify.sh: cannot finish the local build scope ({error})", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
