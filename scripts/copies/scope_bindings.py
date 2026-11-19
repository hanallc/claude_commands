"""Bind a checkout lease while the copy owner holds its lock."""

from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import select
import socket
import struct
from threading import Thread

from scripts.lib import build_folders
from scripts.lib.build_steps import process_arguments, verify_command, VerifyCommand
from scripts.lib.proc_text import read_proc_text


@dataclass(frozen=True)
class OwnedCheckoutLease:
    path: Path
    lock: int
    kind: build_folders.CheckoutLeaseKind


@dataclass(frozen=True)
class VerifyLaunch:
    pid: int


@dataclass(frozen=True)
class CopyOwnerLaunch:
    pid: int


CheckoutRequester = VerifyLaunch | CopyOwnerLaunch


def live_parent(process: Path) -> int:
    fields = read_proc_text(process / "stat").rsplit(") ", 1)[1].split()
    if fields[0] in ("X", "Z"):
        raise ProcessLookupError(process)
    return int(fields[1])


def supervisor_of_checkout(peer: int, requester: CheckoutRequester) -> bool:
    """Require a supervisor launched by a Verify shell in the owner's process tree."""
    process = Path("/proc") / str(peer)
    if not Path(os.readlink(process / "exe")).name.startswith(("python", "pypy")):
        return False
    arguments = process_arguments(process)
    for index, argument in enumerate(arguments[:-1]):
        if Path(argument).name == "verify_scoped_step.py":
            break
    else:
        return False
    caller = int(arguments[index + 1])
    if not 0 < caller < 2**31 or caller == peer:
        return False
    if isinstance(requester, VerifyLaunch) and caller != requester.pid:
        return False
    verify = Path("/proc") / str(caller)
    verify_arguments = process_arguments(verify)
    if not isinstance(verify_command(verify_arguments), VerifyCommand):
        return False
    verify_script = next(word for word in verify_arguments if Path(word).name == "verify.sh")
    expected = (verify / "cwd" / verify_script).resolve().with_name("verify_scoped_step.py")
    if Path(argument).resolve() != expected:
        return False
    shell = os.readlink(verify / "exe")
    if Path(shell).name != "bash":
        return False

    # The launch may pass through Bash subshells in the step pipeline.
    parent = live_parent(process)
    while parent > 1 and parent != caller:
        branch = Path("/proc") / str(parent)
        if process_arguments(branch) != verify_arguments or os.readlink(branch / "exe") != shell:
            return False
        parent = live_parent(branch)
    if parent != caller:
        return False
    if isinstance(requester, VerifyLaunch):
        return True
    parent = caller
    while parent > 1:
        if parent == requester.pid:
            return True
        parent = live_parent(Path("/proc") / str(parent))
    return False


def confirm_scope(connection: socket.socket, peer: int, lease: OwnedCheckoutLease, requester: CheckoutRequester) -> None:
    request, _, flags = connection.recvmsg(4096, 0, socket.MSG_DONTWAIT)[:3]
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        return
    path = Path(os.fsdecode(request.removesuffix(b"\n")))
    if not path.is_absolute() or path.name != f"verify-step-{peer}.scope":
        return
    if supervisor_of_checkout(peer, requester):
        binding = build_folders.CheckoutScopeBinding(path, os.getpid(), lease.kind)
        build_folders.write_checkout_lease(lease.path, binding, lease.lock)
        _ = connection.send(b"bound\n", socket.MSG_DONTWAIT)


def serve_checkout_bindings(listener: socket.socket, stop: socket.socket, lease: OwnedCheckoutLease,
                            requester: CheckoutRequester) -> None:
    ready = select.poll()
    ready.register(stop, select.POLLIN)
    ready.register(listener, select.POLLIN)
    clients: dict[int, tuple[socket.socket, int]] = {}
    try:
        while True:
            events = ready.poll()
            if any(descriptor == stop.fileno() for descriptor, _ in events):
                return
            for descriptor, _ in events:
                if descriptor == listener.fileno():
                    try:
                        connection = listener.accept()[0]
                    except BlockingIOError:
                        continue
                    try:
                        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                        peer, user, _ = struct.unpack("3i", credentials)
                        if user == os.getuid() and supervisor_of_checkout(peer, requester):
                            clients[connection.fileno()] = connection, peer
                            ready.register(connection, select.POLLIN)
                            continue
                    except (OSError, ValueError, IndexError):
                        pass
                    connection.close()
                else:
                    connection, peer = clients.pop(descriptor)
                    ready.unregister(descriptor)
                    with connection, suppress(OSError, ValueError, IndexError):
                        confirm_scope(connection, peer, lease, requester)
    finally:
        for connection, _ in clients.values():
            connection.close()


@contextmanager
def checkout_scope_channel(lease: OwnedCheckoutLease, requester: CheckoutRequester) -> Generator[str]:
    """Serve scope requests in the existing copy owner."""
    stop, worker_end = socket.socketpair()
    with stop, worker_end, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        address = "copies-scope-" + secrets.token_hex(16)
        listener.bind("\0" + address)
        listener.listen()
        listener.setblocking(False)
        worker = Thread(target=serve_checkout_bindings, args=(listener, worker_end, lease, requester))
        worker.start()
        try:
            yield address
        finally:
            stop.close()
            worker.join()
