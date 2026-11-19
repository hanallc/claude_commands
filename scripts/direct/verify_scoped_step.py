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
from scripts.copies.scope_bindings import (
    CHECKOUT_TOKEN_BYTES, CheckoutBindingChannel, CheckoutBindToken, checkout_peer, checkout_scope_publication_seconds,
)
from scripts.lib import inotify


SCOPE_STOP_TIMEOUT_SECONDS = 10
# poll() takes a signed 32-bit timeout in milliseconds.
POLL_MAX_MILLISECONDS = (1 << 31) - 1


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


def read_checkout_line(owners: select.poll, reply: int, deadline: float, limit: int, timeout_reason: str,
                       *, read_size: int | None = None) -> bytes:
    line = b""
    chunk_size = limit if read_size is None else read_size
    owners.register(reply, select.POLLIN)
    try:
        while len(line) < limit and b"\n" not in line:
            wait_for_checkout_channel(owners, reply, deadline, timeout_reason)
            chunk = os.read(reply, min(chunk_size, limit - len(line)))
            if not chunk:
                break
            line += chunk
    finally:
        owners.unregister(reply)
    return line


def wait_for_checkout_confirmation(owners: select.poll, reply: int, deadline: float) -> None:
    expected = b"bound\n"
    confirmation = read_checkout_line(
        owners, reply, deadline, len(expected), "the copy owner did not confirm the checkout scope",
    )
    if confirmation != expected:
        raise ValueError("the copy owner did not bind the checkout scope")


def send_checkout_scope(channel: socket.socket, request: bytes, owners: select.poll, deadline: float) -> None:
    descriptor = channel.fileno()
    owners.register(descriptor, select.POLLOUT)
    try:
        wait_for_checkout_channel(owners, descriptor, deadline, "the copy owner did not accept the checkout scope")
        try:
            _ = channel.send(request, socket.MSG_DONTWAIT)
        except BlockingIOError as error:
            raise ValueError("the copy channel filled before the checkout scope was sent") from error
    finally:
        owners.unregister(descriptor)


@dataclass(frozen=True)
class AuthorizedCheckoutBinding:
    endpoint: CheckoutBindingChannel
    token: CheckoutBindToken


def read_checkout_token(owners: select.poll, pipe: int, deadline: float) -> CheckoutBindToken:
    line = read_checkout_line(
        owners, pipe, deadline, CHECKOUT_TOKEN_BYTES + 1, "the copy owner did not send a checkout binding token",
    )
    if not line.endswith(b"\n"):
        raise ValueError("incomplete checkout binding token")
    return CheckoutBindToken.parse(line[:-1])


class SlotCheckout(Enum):
    NOT_USED = auto()


@dataclass(frozen=True)
class SlotCheckoutReply:
    descriptor: int
    owner_pid: int


def read_slot_command(owners: select.poll, reply: int, deadline: float) -> list[str]:
    timeout_reason = "the copy owner did not send the slot command"
    # Read the count without consuming any bytes from the first word.
    header = read_checkout_line(owners, reply, deadline, 20, timeout_reason, read_size=1)
    count_bytes = header[:-1]
    if not header.endswith(b"\n") or not count_bytes.isdigit() or count_bytes.startswith(b"0"):
        raise ValueError("invalid slot command argument count")
    count = int(count_bytes)
    words = bytearray()
    remaining = count
    limit = os.sysconf("SC_ARG_MAX")
    owners.register(reply, select.POLLIN)
    try:
        while remaining > 0 and len(words) < limit:
            wait_for_checkout_channel(owners, reply, deadline, timeout_reason)
            chunk = os.read(reply, min(65536, limit - len(words)))
            if not chunk:
                break
            words.extend(chunk)
            remaining -= chunk.count(b"\0")
    finally:
        owners.unregister(reply)
    if remaining > 0:
        raise ValueError("incomplete slot command")
    return [os.fsdecode(word) for word in bytes(words).split(b"\0", count)[:count]]


def read_slot_checkout(owners: select.poll, slot: SlotCheckoutReply,
                       deadline: float) -> tuple[AuthorizedCheckoutBinding, list[str]]:
    with os.fdopen(slot.descriptor, "rb", buffering=0) as pipe:
        reply = pipe.fileno()
        line = read_checkout_line(owners, reply, deadline, 256, "the copy owner did not send a slot binding", read_size=1)
        if not line.endswith(b"\n"):
            raise ValueError("incomplete slot binding")
        endpoint = CheckoutBindingChannel.parse(line[:-1].decode("ascii"))
        if endpoint.owner.pid != slot.owner_pid or not endpoint.owner.live_now():
            raise ValueError("the slot binding does not name its live copy owner")
        token = read_checkout_token(owners, reply, deadline)
        return AuthorizedCheckoutBinding(endpoint, token), read_slot_command(owners, reply, deadline)


def bind_checkout_scope(checkout: AuthorizedCheckoutBinding, scope: Path, owners: select.poll, deadline: float) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
        channel.setblocking(False)
        channel.connect("\0" + checkout.endpoint.address)
        if checkout_peer(channel) != checkout.endpoint.owner:
            raise ValueError("the checkout scope peer is not the registered copy owner")
        send_checkout_scope(channel, checkout.token.value + b"\n" + os.fsencode(scope) + b"\n", owners, deadline)
        wait_for_checkout_confirmation(owners, channel.fileno(), deadline)


def run_scoped_step(caller: int, leader: int, release_state: Path, peak: str,
                    scope_shell: str, command: list[str], endpoints: tuple[CheckoutBindingChannel, ...],
                    registration_started: float | None, slot: SlotCheckoutReply | SlotCheckout) -> int:
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
            leases: tuple[AuthorizedCheckoutBinding, ...] = ()
            lease_owner_fds: tuple[int, ...] = ()
            if endpoints or isinstance(slot, SlotCheckoutReply):
                seconds = checkout_scope_publication_seconds()
                if registration_started is not None:
                    seconds -= time.clock_gettime(time.CLOCK_BOOTTIME) - registration_started
                if seconds <= 0:
                    raise TimeoutError("the checkout scope publication timed out")
                deadline = time.monotonic() + seconds
                if any(not endpoint.owner.live_now() for endpoint in endpoints):
                    raise ValueError("a checkout owner exited before launch")
                lease_owners = {endpoint.owner.pid for endpoint in endpoints}
                if isinstance(slot, SlotCheckoutReply):
                    lease_owners.add(slot.owner_pid)
                lease_owner_fds = tuple(open_pidfd(owner, cleanup) for owner in lease_owners - {caller, leader, supervisor})
                for descriptor in lease_owner_fds:
                    owners.register(descriptor, select.POLLIN)
                if endpoints:
                    with sys.stdin.buffer as pipe:
                        leases = tuple(AuthorizedCheckoutBinding(endpoint, read_checkout_token(owners, pipe.fileno(), deadline))
                                       for endpoint in endpoints)
                if isinstance(slot, SlotCheckoutReply):
                    slot_lease, command = read_slot_checkout(owners, slot, deadline)
                    leases += (slot_lease,)
                if time.monotonic() >= deadline:
                    raise TimeoutError("the checkout scope publication timed out")
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
        registration_started = None
        if command[:1] == ["--checkout-registration-start"]:
            registration_started = float(command[1])
            if not math.isfinite(registration_started) or registration_started < 0:
                raise ValueError("invalid checkout registration start")
            del command[:2]
        endpoints: list[CheckoutBindingChannel] = []
        while command[:1] == ["--checkout-binding"]:
            endpoints.append(CheckoutBindingChannel.parse(command[1]))
            del command[:2]
        slot: SlotCheckoutReply | SlotCheckout = SlotCheckout.NOT_USED
        if command[:1] == ["--checkout-slot"]:
            slot = SlotCheckoutReply(int(command[1]), int(command[2]))
            del command[:3]
        return run_scoped_step(
            int(caller), int(leader), Path(release_state), peak, scope_shell, command, tuple(endpoints),
            registration_started, slot,
        )
    except (OSError, ValueError) as error:
        print(f"verify.sh: cannot finish the local build scope ({error})", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
