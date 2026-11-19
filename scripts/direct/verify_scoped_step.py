"""Keep a placed step in its scope until all its processes exit."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum, auto
import errno
import fcntl
import math
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from scripts.build_queue.admission_records import process_start_time
from scripts.direct.verify_checkout_handoff import CheckoutHandoff, receive_checkout_handoffs, send_checkout_descriptors
from scripts.lib import build_folders, conf, inotify


SCOPE_STOP_TIMEOUT_SECONDS = 10
# poll() takes a signed 32-bit timeout in milliseconds.
POLL_MAX_MILLISECONDS = (1 << 31) - 1


@dataclass(frozen=True)
class InheritedSlotLease:
    path: Path
    lease_owner: int
    lock_fd: int


@dataclass(frozen=True)
class CheckoutOwnerChannel:
    lease_owner: int
    request_fd: int
    reply_fd: int


@dataclass(frozen=True)
class SourceCopyLeaseChannel:
    lease_owner: int
    request_fd: int


CheckoutScopeLease = InheritedSlotLease | CheckoutOwnerChannel | SourceCopyLeaseChannel


def checkout_scope_leases(owners: select.poll, deadline: float, cleanup: ExitStack) -> tuple[CheckoutScopeLease, ...]:
    """Collect each copy lease that the command can use."""
    leases: list[CheckoutScopeLease] = []
    address = os.environ.pop("VERIFY_CHECKOUT_CHANNEL", "")
    handoffs: dict[CheckoutHandoff, int] = {}
    if address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
            channel.setblocking(False)
            channel.connect("\0" + address)
            descriptor = channel.fileno()
            owners.register(descriptor, select.POLLIN)
            try:
                wait_for_checkout_channel(owners, descriptor, deadline, "the checkout descriptor holder did not reply")
                handoffs = receive_checkout_handoffs(channel, cleanup)
            finally:
                owners.unregister(descriptor)
    if os.environ.get("COPIES_RUN_PID") and os.environ.get("COPIES_STEP_SOURCE"):
        if CheckoutHandoff.SOURCE_CHANNEL not in handoffs:
            raise ValueError("the source copy has no scope binding channel")
        leases.append(SourceCopyLeaseChannel(int(os.environ["COPIES_RUN_PID"]), handoffs[CheckoutHandoff.SOURCE_CHANNEL]))

    owner_pid = os.environ.pop("VERIFY_CHECKOUT_OWNER", "")
    request_fd = os.environ.pop("DOC_COPY_LEASE_FD", "")
    reply_fd = os.environ.pop("DOC_COPY_REPLY_FD", "")
    if owner_pid:
        if request_fd:
            leases.append(CheckoutOwnerChannel(int(owner_pid), int(request_fd), int(reply_fd)))
        else:
            if CheckoutHandoff.PREPARED_LOCK not in handoffs:
                raise ValueError("the prepared checkout has no inherited lease")
            leases.append(InheritedSlotLease(Path(os.environ["BUILDLOG_PREPARED_COPY"]), int(owner_pid),
                                             handoffs[CheckoutHandoff.PREPARED_LOCK]))
    return tuple(leases)


# The scope shell publishes its path before this check. A dead owner permits no start.
CHECK_SCOPE_OWNER = r'''
live_owner() {
    read -r stat < "/proc/$1/stat" || return 1
    set -- "$2" ${stat##*) }
    [ "$2" != Z ] && [ "$2" != X ] && [ "$1" = "${21}" ]
}
read -r scope < "$1" && [ "${scope##*/}" = "$2" ] && [ -r "$scope/cgroup.events" ] || exit 125
if [ "$9" = bind-checkout ]; then
    IFS= read -r permission && [ "$permission" = launch ] || exit 125
    exec </dev/null
fi
live_owner "$3" "$4" && live_owner "$5" "$6" && live_owner "$7" "$8" || exit 125
shift 9
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


def wait_for_empty_scope(scope: Path, events: int, *, timeout: int) -> None:
    ready = select.poll()
    ready.register(events, select.POLLPRI | select.POLLERR)
    deadline = time.monotonic() + timeout
    while scope_populated(events):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"build scope {scope.name} is still populated")
        _ = ready.poll(round(remaining * 1000))


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


def poll_checkout_until(owners: select.poll, deadline: float) -> list[tuple[int, int]]:
    """Wait for a checkout event or the end of the publication deadline."""
    while (remaining := deadline - time.monotonic()) > 0:
        milliseconds = math.ceil(min(remaining * 1000, POLL_MAX_MILLISECONDS))
        if events := owners.poll(milliseconds):
            return events
    return []


def wait_for_checkout_scope(owners: select.poll, watch: int, sidecar: Path, unit: str, deadline: float) -> Path:
    """Read the published scope while Cargo waits for its checkout lease."""
    owners.register(watch, select.POLLIN)
    try:
        scope = published_scope(sidecar, unit)
        while True:
            if isinstance(scope, Path):
                return scope
            ready = poll_checkout_until(owners, deadline)
            if not ready:
                raise TimeoutError("the checkout scope publication timed out")
            if any(descriptor != watch for descriptor, _ in ready):
                raise ValueError("the checkout scope owner exited before launch")
            events = inotify.read_events(watch)
            if any(event.mask & inotify.WATCH_REMOVED for event in events):
                raise ValueError("the checkout scope publication folder disappeared")
            if any(event.name == sidecar.name or event.mask & inotify.IN_Q_OVERFLOW for event in events):
                scope = published_scope(sidecar, unit)
    finally:
        owners.unregister(watch)


def wait_for_checkout_channel(owners: select.poll, channel: int, deadline: float, timeout_reason: str) -> None:
    ready = poll_checkout_until(owners, deadline)
    if not ready or time.monotonic() >= deadline:
        raise TimeoutError(timeout_reason)
    if any(descriptor != channel for descriptor, _ in ready):
        raise ValueError("a scope owner exited before launch")


def wait_for_checkout_confirmation(owners: select.poll, reply: int, deadline: float) -> None:
    expected = b"bound\n"
    confirmation = b""
    owners.register(reply, select.POLLIN)
    try:
        while len(confirmation) < len(expected) and b"\n" not in confirmation:
            wait_for_checkout_channel(owners, reply, deadline, "the copy owner did not confirm the checkout scope")
            chunk = os.read(reply, len(expected) - len(confirmation))
            if not chunk:
                break
            confirmation += chunk
        if confirmation != expected:
            raise ValueError("the copy owner did not bind the checkout scope")
    finally:
        owners.unregister(reply)


def bind_source_copy_scope(checkout: SourceCopyLeaseChannel, request: bytes,
                           owners: select.poll, deadline: float) -> None:
    # Each scope gets its own reply, even when commands share a source copy.
    reader, writer = socket.socketpair()
    with reader, writer:
        with socket.socket(fileno=os.dup(checkout.request_fd)) as channel:
            descriptor = channel.fileno()
            owners.register(descriptor, select.POLLOUT)
            try:
                wait_for_checkout_channel(
                    owners, descriptor, deadline, "the source copy channel did not accept the checkout scope")
                try:
                    send_checkout_descriptors(channel, request, [writer.fileno()])
                except BlockingIOError as error:
                    raise ValueError("the source copy channel filled before the checkout scope was sent") from error
            finally:
                owners.unregister(descriptor)
        writer.close()
        wait_for_checkout_confirmation(owners, reader.fileno(), deadline)


def bind_checkout_scope(checkout: CheckoutScopeLease, scope: Path, owners: select.poll, deadline: float) -> None:
    owner_start = process_start_time(checkout.lease_owner)
    if owner_start is None:
        raise ValueError("the checkout lease owner exited before scope publication")
    if isinstance(checkout, SourceCopyLeaseChannel):
        bind_source_copy_scope(checkout, os.fsencode(scope) + b"\n", owners, deadline)
    elif isinstance(checkout, CheckoutOwnerChannel):
        _ = os.write(checkout.request_fd, os.fsencode(scope) + b"\n")
        wait_for_checkout_confirmation(owners, checkout.reply_fd, deadline)
    else:
        build_folders.write_checkout_lease(
            checkout.path, build_folders.CheckoutScopeBinding(scope, checkout.lease_owner),
            checkout.lock_fd)
    if process_start_time(checkout.lease_owner) != owner_start:
        raise ValueError("the checkout lease owner exited during scope publication")


def run_scoped_step(caller: int, leader: int, release_state: Path, peak: str,
                    scope_shell: str, command: list[str]) -> int:
    # The supervisor and the scope survive the exit of the step shell.
    supervisor = os.getpid()
    unit = f"verify-step-{supervisor}.scope"
    sidecar = Path(peak + ".cgroup")
    with release_state.open("r+b", buffering=0) as release, ExitStack() as cleanup:
        fcntl.flock(release, fcntl.LOCK_EX)
        scope: Path | ScopePublication = ScopePublication.UNPUBLISHED
        try:
            if release.read(1):
                raise ValueError("the scope launch is closed")
            # Keep this state if the scope cannot be proved empty.
            _ = release.write(b"scope may be populated\n")
            os.setpgid(0, 0)
            caller_start = process_start_time(caller)
            leader_start = process_start_time(leader)
            supervisor_start = process_start_time(supervisor)
            if caller_start is None or leader_start is None or supervisor_start is None:
                raise ValueError("a scope owner exited before launch")
            owners = select.poll()
            owners.register(open_pidfd(caller, cleanup), select.POLLIN)
            owners.register(open_pidfd(leader, cleanup), select.POLLIN)
            if owners.poll(0):
                raise ValueError("a scope owner exited before launch")
            watch = None
            deadline = 0.0
            if os.environ.get("VERIFY_CHECKOUT_CHANNEL") or os.environ.get("VERIFY_CHECKOUT_OWNER"):
                seconds = conf.read_or_repository("direct").number("VERIFY_SCOPE_PUBLICATION_SECONDS")
                deadline = time.monotonic() + seconds
            leases = checkout_scope_leases(owners, deadline, cleanup)
            lease_owners = {lease.lease_owner for lease in leases} - {caller, leader, supervisor}
            lease_owner_fds = tuple(open_pidfd(owner, cleanup) for owner in lease_owners)
            if leases:
                for descriptor in lease_owner_fds:
                    owners.register(descriptor, select.POLLIN)
                watch = inotify.open_watch()
                _ = cleanup.callback(os.close, watch)
                _ = inotify.add_watch(watch, sidecar.parent, inotify.IN_CLOSE_WRITE | inotify.IN_MOVED_TO
                                      | inotify.IN_DELETE_SELF | inotify.IN_MOVE_SELF)
            process = subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
                 "--slice=builds.slice", f"--unit={unit}",
                 "--",
                 "/bin/sh", "-c", 'exec 3>&1; ' + scope_shell, peak, "/bin/sh", "-c", CHECK_SCOPE_OWNER,
                 "verify-scope-owner", str(sidecar), unit, str(caller), caller_start, str(leader), leader_start,
                 str(supervisor), supervisor_start,
                 "bind-checkout" if leases else "no-checkout",
                 *command],
                stdin=subprocess.PIPE if leases else None,
                stderr=subprocess.DEVNULL, process_group=0,
            )
            try:
                owners.register(open_pidfd(process.pid, cleanup), select.POLLIN)
                if leases and watch is not None:
                    scope = wait_for_checkout_scope(owners, watch, sidecar, unit, deadline)
                    for lease in leases:
                        bind_checkout_scope(lease, scope, owners, deadline)
                    if owners.poll(0):
                        raise ValueError("a scope owner exited before launch")
                    assert process.stdin is not None
                    with process.stdin as permission:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("the checkout scope publication timed out")
                        _ = os.write(permission.fileno(), b"launch\n")
                    # The scope lease protects the checkout after its owner exits.
                    for descriptor in lease_owner_fds:
                        owners.unregister(descriptor)
                _ = owners.poll()
            except ProcessLookupError:
                pass
            finally:
                if process.stdin is not None:
                    process.stdin.close()
                if isinstance(scope, ScopePublication):
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
            if isinstance(scope, ScopePublication):
                raise ValueError("the local step did not enter its build scope")
            if status == 125:
                raise ValueError("the local build scope returned status 125")
            _ = release.truncate(0)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # The step's log readers can die before the scope stops.
            reason = f"cannot finish the local build scope: {error}"
            if isinstance(scope, Path):
                try:
                    with sidecar.open("x") as publication:
                        _ = publication.write(f"{scope}\n")
                except FileExistsError:
                    pass
                except OSError as publication_error:
                    reason += f". Cannot restore the reservation scope: {publication_error}"
            _ = release.seek(0)
            _ = release.write(f"{reason}\n".encode())
            _ = release.truncate()
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
