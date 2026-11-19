"""Bind a checkout lease while the copy owner holds its lock."""

from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import secrets
import select
import socket
import struct
from threading import Thread

from scripts.build_queue.admission_records import process_start_time
from scripts.lib import build_folders


@dataclass(frozen=True)
class OwnedCheckoutLease:
    path: Path
    lock: int
    kind: build_folders.CheckoutLeaseKind


@dataclass(frozen=True)
class CheckoutProcess:
    pid: int
    start: str

    @classmethod
    def live(cls, pid: int) -> "CheckoutProcess":
        start = process_start_time(pid)
        if start is None:
            raise ProcessLookupError(pid)
        return cls(pid, start)

    @classmethod
    def parse(cls, value: str) -> "CheckoutProcess":
        pid, start = value.split(":")
        if not (pid.isascii() and pid.isdecimal() and start.isascii() and start.isdecimal()):
            raise ValueError("invalid checkout process identity")
        number = int(pid)
        if not 0 < number < 2**31:
            raise ValueError("invalid checkout process PID")
        return cls(number, start)

    def live_now(self) -> bool:
        return process_start_time(self.pid) == self.start


def checkout_peer(channel: socket.socket) -> CheckoutProcess:
    credentials = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    peer, user, _ = struct.unpack("3i", credentials)
    if user != os.getuid():
        raise ValueError("the checkout peer has a different user")
    return CheckoutProcess.live(peer)


@dataclass(frozen=True)
class CheckoutBindingChannel:
    owner: CheckoutProcess
    address: str

    def encode(self) -> bytes:
        return f"{self.owner.pid}:{self.owner.start}:{self.address}\n".encode("ascii")

    @classmethod
    def parse(cls, value: str) -> "CheckoutBindingChannel":
        process, address = value.rsplit(":", 1)
        if not address.startswith("copies-scope-"):
            raise ValueError("invalid checkout scope address")
        return cls(CheckoutProcess.parse(process), address)


@dataclass
class LaunchedSupervisors:
    identities: set[CheckoutProcess] = field(default_factory=set[CheckoutProcess])
    pending: bytes = b""

    def record(self, identity: CheckoutProcess) -> None:
        self.identities = {peer for peer in self.identities if peer.live_now()}
        if identity.live_now():
            self.identities.add(identity)

    def receive(self, requests: socket.socket) -> bool:
        """Read registrations before accepting a scope connection."""
        while True:
            try:
                incoming = requests.recv(4096, socket.MSG_DONTWAIT)
            except BlockingIOError:
                return True
            if not incoming:
                return False
            lines = (self.pending + incoming).split(b"\n")
            self.pending = lines.pop()
            if len(self.pending) > 64:
                return False
            for line in lines:
                with suppress(ValueError):
                    self.record(CheckoutProcess.parse(line.decode("ascii")))


def confirm_scope(connection: socket.socket, peer: CheckoutProcess, lease: OwnedCheckoutLease) -> None:
    request, _, flags = connection.recvmsg(4096, 0, socket.MSG_DONTWAIT)[:3]
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        return
    path = Path(os.fsdecode(request.removesuffix(b"\n")))
    if not path.is_absolute() or path.name != f"verify-step-{peer.pid}.scope" or not peer.live_now():
        return
    binding = build_folders.CheckoutScopeBinding(path, os.getpid(), lease.kind)
    build_folders.write_checkout_lease(lease.path, binding, lease.lock)
    _ = connection.send(b"bound\n", socket.MSG_DONTWAIT)


def serve_checkout_bindings(listener: socket.socket, stop: socket.socket, lease: OwnedCheckoutLease,
                            supervisors: LaunchedSupervisors, requests: socket.socket | None) -> None:
    ready = select.poll()
    ready.register(stop, select.POLLIN)
    ready.register(listener, select.POLLIN)
    if requests is not None:
        ready.register(requests, select.POLLIN)
    clients: dict[int, tuple[socket.socket, CheckoutProcess]] = {}
    try:
        while True:
            events = ready.poll()
            if any(descriptor == stop.fileno() for descriptor, _ in events):
                return
            # Verify writes the registration before its supervisor connects.
            if requests is not None and not supervisors.receive(requests):
                ready.unregister(requests)
                requests = None
            for descriptor, _ in events:
                if descriptor == listener.fileno():
                    try:
                        connection = listener.accept()[0]
                    except BlockingIOError:
                        continue
                    try:
                        peer = checkout_peer(connection)
                        if peer in supervisors.identities:
                            supervisors.identities.remove(peer)
                            clients[connection.fileno()] = connection, peer
                            ready.register(connection, select.POLLIN)
                            continue
                    except (OSError, ValueError):
                        pass
                    connection.close()
                elif descriptor in clients:
                    connection, peer = clients.pop(descriptor)
                    ready.unregister(descriptor)
                    with connection, suppress(OSError, ValueError):
                        confirm_scope(connection, peer, lease)
    finally:
        for connection, _ in clients.values():
            connection.close()


@contextmanager
def checkout_scope_listener() -> Generator[tuple[socket.socket, CheckoutBindingChannel]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        address = "copies-scope-" + secrets.token_hex(16)
        listener.bind("\0" + address)
        listener.listen()
        listener.setblocking(False)
        yield listener, CheckoutBindingChannel(CheckoutProcess.live(os.getpid()), address)


@contextmanager
def checkout_scope_channel(listener: socket.socket, lease: OwnedCheckoutLease, supervisors: LaunchedSupervisors,
                           requests: socket.socket | None = None) -> Generator[None]:
    """Serve scope requests in the existing copy owner."""
    stop, worker_end = socket.socketpair()
    with stop, worker_end:
        worker = Thread(target=serve_checkout_bindings, args=(listener, worker_end, lease, supervisors, requests))
        worker.start()
        try:
            yield
        finally:
            stop.close()
            worker.join()


@dataclass(frozen=True)
class CopyScopeRegistration:
    requests: int
    endpoint: int

    def handoff(self) -> str:
        return f"{self.requests}:{self.endpoint}"

    def descriptors(self) -> tuple[int, int]:
        return self.requests, self.endpoint


@contextmanager
def checkout_binding_registration(lease: OwnedCheckoutLease) -> Generator[CopyScopeRegistration]:
    """Give the launch its own registration socket and the owner's identity."""
    verify, owner = socket.socketpair()
    with verify, owner, checkout_scope_listener() as (listener, channel):
        endpoint = os.memfd_create("checkout-scope-owner", os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
        try:
            _ = os.write(endpoint, channel.encode())
            seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
            _ = fcntl.fcntl(endpoint, fcntl.F_ADD_SEALS, seals)
            with checkout_scope_channel(listener, lease, LaunchedSupervisors(), owner):
                yield CopyScopeRegistration(verify.fileno(), endpoint)
        finally:
            os.close(endpoint)
