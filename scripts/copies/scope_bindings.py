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
from scripts.lib import build_folders, conf


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


def checkout_peer_pid(channel: socket.socket) -> int:
    credentials = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    peer, user, _ = struct.unpack("3i", credentials)
    if user != os.getuid():
        raise ValueError("the checkout peer has a different user")
    return peer


def checkout_peer(channel: socket.socket) -> CheckoutProcess:
    return CheckoutProcess.live(checkout_peer_pid(channel))


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


CHECKOUT_TOKEN_BYTES = 64


def checkout_scope_publication_seconds() -> float:
    return conf.read_or_repository("direct").number("VERIFY_SCOPE_PUBLICATION_SECONDS")


@dataclass(frozen=True)
class CheckoutBindToken:
    value: bytes

    @classmethod
    def parse(cls, data: bytes) -> "CheckoutBindToken":
        if len(data) != CHECKOUT_TOKEN_BYTES or not all(byte in b"0123456789abcdef" for byte in data):
            raise ValueError("invalid checkout binding token")
        return cls(data)


@dataclass
class CheckoutBindTokens:
    unspent: set[bytes] = field(default_factory=set[bytes])
    registrations: int = 0

    def issue(self) -> CheckoutBindToken:
        self.registrations += 1
        # The counter prevents reuse even if the random part repeats.
        value = f"{self.registrations:016x}{secrets.token_hex(24)}".encode("ascii")
        token = CheckoutBindToken.parse(value)
        self.unspent.add(token.value)
        return token

    def consume(self, value: bytes) -> bool:
        if value not in self.unspent:
            return False
        self.unspent.remove(value)
        return True


@dataclass(frozen=True)
class CheckoutTokenRegistration:
    requests: socket.socket
    replies: socket.socket

    def issue(self, tokens: CheckoutBindTokens) -> bool:
        request = self.requests.recv(16, socket.MSG_DONTWAIT)
        if not request:
            return False
        if request == b"issue\n":
            token = tokens.issue()
            try:
                _ = self.replies.send(token.value, socket.MSG_DONTWAIT)
            except OSError:
                _ = tokens.consume(token.value)
        return True


def confirm_scope(connection: socket.socket, peer: int, lease: OwnedCheckoutLease, tokens: CheckoutBindTokens) -> None:
    request, _, flags = connection.recvmsg(4096, 0, socket.MSG_DONTWAIT)[:3]
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
        return
    token, _, scope = request.partition(b"\n")
    if not tokens.consume(token):
        _ = connection.send(b"refused\n", socket.MSG_DONTWAIT)
        return
    path = Path(os.fsdecode(scope.removesuffix(b"\n")))
    if not path.is_absolute() or path.name != f"verify-step-{peer}.scope":
        return
    binding = build_folders.CheckoutScopeBinding(path, os.getpid(), lease.kind)
    build_folders.write_checkout_lease(lease.path, binding, lease.lock)
    _ = connection.send(b"bound\n", socket.MSG_DONTWAIT)


def serve_checkout_bindings(listener: socket.socket, stop: socket.socket, lease: OwnedCheckoutLease,
                            tokens: CheckoutBindTokens, registration: CheckoutTokenRegistration | None) -> None:
    ready = select.poll()
    ready.register(stop, select.POLLIN)
    ready.register(listener, select.POLLIN)
    if registration is not None:
        ready.register(registration.requests, select.POLLIN)
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
                        peer = checkout_peer_pid(connection)
                        clients[connection.fileno()] = connection, peer
                        ready.register(connection, select.POLLIN)
                    except (OSError, ValueError):
                        connection.close()
                elif registration is not None and descriptor == registration.requests.fileno():
                    if not registration.issue(tokens):
                        ready.unregister(descriptor)
                elif descriptor in clients:
                    connection, peer = clients.pop(descriptor)
                    ready.unregister(descriptor)
                    with connection, suppress(OSError, ValueError):
                        confirm_scope(connection, peer, lease, tokens)
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
def checkout_scope_channel(listener: socket.socket, lease: OwnedCheckoutLease, tokens: CheckoutBindTokens,
                           registration: CheckoutTokenRegistration | None = None) -> Generator[None]:
    """Serve scope requests in the existing copy owner."""
    stop, worker_end = socket.socketpair()
    with stop, worker_end:
        worker = Thread(target=serve_checkout_bindings, args=(listener, worker_end, lease, tokens, registration))
        worker.start()
        try:
            yield
        finally:
            stop.close()
            worker.join()


@dataclass(frozen=True)
class CopyScopeRegistration:
    requests: int
    replies: int
    endpoint: int

    def handoff(self) -> str:
        return f"{self.requests}:{self.replies}:{self.endpoint}"

    def descriptors(self) -> tuple[int, int, int]:
        return self.requests, self.replies, self.endpoint


@contextmanager
def checkout_binding_registration(lease: OwnedCheckoutLease) -> Generator[CopyScopeRegistration]:
    """Give the caller private channels for token registration."""
    verify_requests, owner_requests = socket.socketpair(type=socket.SOCK_SEQPACKET)
    with verify_requests, owner_requests, checkout_scope_listener() as (listener, channel):
        verify_replies, owner_replies = socket.socketpair(type=socket.SOCK_SEQPACKET)
        with verify_replies, owner_replies:
            endpoint = os.memfd_create("checkout-scope-owner", os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
            try:
                # Use milliseconds within the poll interval limit.
                seconds = min(checkout_scope_publication_seconds(), (2 ** 31 - 1) / 1000)
                _ = os.write(endpoint, channel.encode() + f"{int(seconds * 1000)}\n".encode("ascii"))
                seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
                _ = fcntl.fcntl(endpoint, fcntl.F_ADD_SEALS, seals)
                registration = CheckoutTokenRegistration(owner_requests, owner_replies)
                with checkout_scope_channel(listener, lease, CheckoutBindTokens(), registration):
                    yield CopyScopeRegistration(verify_requests.fileno(), verify_replies.fileno(), endpoint)
            finally:
                os.close(endpoint)
