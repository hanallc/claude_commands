"""Keep a placed step in its scope until all its processes exit."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum, auto
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
DOC_HOLDER_START_TIMEOUT_SECONDS = 10
DOC_HOLDER_READY = b"1"


@dataclass(frozen=True)
class OwnedDocCheckoutLease:
    pipe_writer: int


class NoOwnedDocCheckoutLease(Enum):
    NOT_REQUIRED = auto()


class UnboundedScopeWait(Enum):
    UNTIL_EMPTY = auto()


# The scope shell publishes its path before this check. A dead owner permits no start.
CHECK_SCOPE_OWNER = r'''
live_owner() {
    read -r stat < "/proc/$1/stat" || return 1
    set -- "$2" ${stat##*) }
    [ "$2" != Z ] && [ "$2" != X ] && [ "$1" = "${21}" ]
}
read -r scope < "$1" && [ "${scope##*/}" = "$2" ] && [ -r "$scope/cgroup.events" ] || exit 125
live_owner "$3" "$4" && live_owner "$5" "$6" && live_owner "$7" "$8" || exit 125
shift 8
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


class ScopePublication(Enum):
    UNPUBLISHED = auto()


def published_scope(sidecar: Path, unit: str) -> Path | ScopePublication:
    """Read the scope path that the launcher publishes before Cargo starts."""
    try:
        name = sidecar.read_text().strip()
    except FileNotFoundError:
        return ScopePublication.UNPUBLISHED
    if not name:
        return ScopePublication.UNPUBLISHED
    scope = Path(name)
    if not scope.is_absolute() or scope.name != unit:
        raise ValueError(f"invalid build scope: {scope}")
    return scope


def open_scope_events(scope: Path) -> int | None:
    try:
        return os.open(scope / "cgroup.events", os.O_RDONLY)
    except FileNotFoundError:
        if scope.exists():
            raise
        return None


def wait_for_empty_scope(scope: Path, events: int, *, timeout: int | UnboundedScopeWait) -> None:
    ready = select.poll()
    ready.register(events, select.POLLPRI | select.POLLERR)
    deadline = time.monotonic() + timeout if isinstance(timeout, int) else None
    while scope_populated(events):
        milliseconds = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"build scope {scope.name} is still populated")
            milliseconds = round(remaining * 1000)
        _ = ready.poll(milliseconds)


def drain_scope(scope: Path) -> None:
    """Stop all scope members and confirm exit before release."""
    events = open_scope_events(scope)
    if events is None:
        return
    try:
        if not scope_populated(events):
            return
        try:
            _ = (scope / "cgroup.kill").write_text("1\n")
        except OSError:
            if scope_populated(events):
                raise
            return
        wait_for_empty_scope(scope, events, timeout=SCOPE_STOP_TIMEOUT_SECONDS)
    finally:
        os.close(events)


def hold_doc_checkout(lease: OwnedDocCheckoutLease, supervisor: int, sidecar: Path, unit: str) -> None:
    """Keep the lease until the supervisor exits and the scope empties."""
    _ = os.fstat(lease.pipe_writer)
    with ExitStack() as cleanup:
        supervisor_exit = open_pidfd(supervisor, cleanup)
        _ = os.write(sys.stdout.fileno(), DOC_HOLDER_READY)
        _ = select.select([supervisor_exit], [], [])
    while True:
        try:
            scope = published_scope(sidecar, unit)
            if isinstance(scope, ScopePublication):
                return
            events = open_scope_events(scope)
            if events is None:
                return
            try:
                wait_for_empty_scope(scope, events, timeout=UnboundedScopeWait.UNTIL_EMPTY)
            finally:
                os.close(events)
            return
        except (OSError, ValueError):
            # Keep the lease if the kernel cannot yet confirm exit.
            time.sleep(1)


def start_doc_checkout_holder(lease: OwnedDocCheckoutLease, sidecar: Path, unit: str) -> None:
    holder = subprocess.Popen(
        [sys.executable, __file__, "--hold-doc-checkout", str(lease.pipe_writer), str(os.getpid()),
         str(sidecar), unit],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        pass_fds=(lease.pipe_writer,), start_new_session=True,
    )
    assert holder.stdout is not None
    with holder.stdout as ready:
        readable, _, _ = select.select([ready], [], [], DOC_HOLDER_START_TIMEOUT_SECONDS)
        if readable and os.read(ready.fileno(), len(DOC_HOLDER_READY)) == DOC_HOLDER_READY:
            return
    holder.kill()
    _ = holder.wait(timeout=DOC_HOLDER_START_TIMEOUT_SECONDS)
    raise RuntimeError("the doc checkout lease holder did not confirm ownership")


def run_scoped_step(caller: int, leader: int, release_state: Path, peak: str,
                    scope_shell: str, command: list[str],
                    doc_lease: OwnedDocCheckoutLease | NoOwnedDocCheckoutLease) -> int:
    # The supervisor and the scope survive the exit of the step shell.
    os.setpgid(0, 0)
    supervisor = os.getpid()
    unit = f"verify-step-{supervisor}.scope"
    sidecar = Path(peak + ".cgroup")
    with release_state.open("r+b", buffering=0) as release, ExitStack() as cleanup:
        fcntl.flock(release, fcntl.LOCK_EX)
        if release.read(1):
            return 125
        caller_start = process_start_time(caller)
        leader_start = process_start_time(leader)
        supervisor_start = process_start_time(supervisor)
        if caller_start is None or leader_start is None or supervisor_start is None:
            return 125
        owners = select.poll()
        owners.register(open_pidfd(caller, cleanup), select.POLLIN)
        owners.register(open_pidfd(leader, cleanup), select.POLLIN)
        if owners.poll(0):
            return 125
        if isinstance(doc_lease, OwnedDocCheckoutLease):
            try:
                start_doc_checkout_holder(doc_lease, sidecar, unit)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"verify.sh: cannot hold the doc checkout before launch: {error}", file=sys.stderr)
                return 125
        # Keep this state if the scope cannot be proved empty.
        _ = release.write(b"scope may be populated\n")
        try:
            process = subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
                 "--slice=builds.slice", f"--unit={unit}",
                 "--",
                 "/bin/sh", "-c", 'exec 3>&1; ' + scope_shell, peak, "/bin/sh", "-c", CHECK_SCOPE_OWNER,
                 "verify-scope-owner", str(sidecar), unit, str(caller), caller_start, str(leader), leader_start,
                 str(supervisor), supervisor_start, *command],
                stderr=subprocess.DEVNULL, process_group=0,
            )
            try:
                owners.register(open_pidfd(process.pid, cleanup), select.POLLIN)
                _ = owners.poll()
            except ProcessLookupError:
                pass
            finally:
                scope = published_scope(sidecar, unit)
                if isinstance(scope, ScopePublication):
                    # The owner check prevents Cargo from starting after the step shell exits.
                    if process.poll() is None:
                        process.kill()
                    _ = process.wait(timeout=SCOPE_STOP_TIMEOUT_SECONDS)
                    scope = published_scope(sidecar, unit)
                if isinstance(scope, Path):
                    drain_scope(scope)
                status = process.wait(timeout=SCOPE_STOP_TIMEOUT_SECONDS)
                _ = release.truncate(0)
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
    if len(argv) == 5 and argv[0] == "--hold-doc-checkout":
        _, lease_writer, supervisor, sidecar, unit = argv
        hold_doc_checkout(OwnedDocCheckoutLease(int(lease_writer)), int(supervisor), Path(sidecar), unit)
        return 0
    caller, leader, release_state, peak, scope_shell, *command = argv
    try:
        lease = os.environ.pop("VERIFY_DOC_LEASE_FD", "")
        doc_lease = OwnedDocCheckoutLease(int(lease)) if lease else NoOwnedDocCheckoutLease.NOT_REQUIRED
        return run_scoped_step(int(caller), int(leader), Path(release_state), peak, scope_shell, command, doc_lease)
    except (OSError, ValueError) as error:
        print(f"verify.sh: cannot finish the local build scope ({error})", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
