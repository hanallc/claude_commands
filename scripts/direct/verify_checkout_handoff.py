"""Hold inherited checkout descriptors for the scope supervisor."""

from array import array
from contextlib import ExitStack
from enum import Enum
import os
import secrets
import select
import socket
import struct
import sys


class CheckoutHandoff(Enum):
    SOURCE_CHANNEL = "COPIES_RUN_BIND_FD"
    PREPARED_LOCK = "BUILDLOG_PREPARED_COPY_LOCK_FD"


def send_checkout_descriptors(channel: socket.socket, message: bytes, descriptors: list[int]) -> None:
    # Use sendmsg so MSG_DONTWAIT applies to the send on Python 3.13.
    rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", descriptors))]
    _ = channel.sendmsg([message], rights, socket.MSG_DONTWAIT)


def inherited_checkout_handoffs() -> dict[CheckoutHandoff, int]:
    handoffs: dict[CheckoutHandoff, int] = {}
    for kind in CheckoutHandoff:
        value = os.environ.pop(kind.value, "")
        if value.isascii() and value.isdecimal():
            descriptor = int(value)
            _ = os.fstat(descriptor)
            handoffs[kind] = descriptor
    return handoffs


def hold_checkout_handoffs(owner: int) -> None:
    handoffs = inherited_checkout_handoffs()
    names = "\n".join(kind.value for kind in handoffs).encode("ascii")
    descriptors = list(handoffs.values())
    address = "verify-checkout-" + secrets.token_hex(16)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener, ExitStack() as cleanup:
        owner_fd = os.pidfd_open(owner)
        _ = cleanup.callback(os.close, owner_fd)
        listener.bind("\0" + address)
        listener.listen(1)
        print(address, flush=True)
        while True:
            ready, _, _ = select.select([owner_fd, listener], [], [])
            if owner_fd in ready:
                return
            connection = listener.accept()[0]
            with connection:
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", credentials)
                if uid == os.getuid():
                    try:
                        send_checkout_descriptors(connection, names, descriptors)
                    except OSError:
                        # A supervisor can exit before it receives the descriptors.
                        continue


def receive_checkout_handoffs(channel: socket.socket, cleanup: ExitStack) -> dict[CheckoutHandoff, int]:
    descriptors = array("i")
    names, control, flags = channel.recvmsg(
        128, socket.CMSG_SPACE(len(CheckoutHandoff) * descriptors.itemsize), socket.MSG_CMSG_CLOEXEC)[:3]
    for level, kind, data in control:
        if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            descriptors.frombytes(data[:len(data) - len(data) % descriptors.itemsize])
    for descriptor in descriptors:
        _ = cleanup.callback(os.close, descriptor)
    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(names.splitlines()) != len(descriptors):
        raise ValueError("invalid checkout descriptor handoff")
    return {CheckoutHandoff(name.decode("ascii")): descriptor
            for name, descriptor in zip(names.splitlines(), descriptors, strict=True)}


if __name__ == "__main__":
    try:
        hold_checkout_handoffs(int(sys.argv[1]))
    except (OSError, ValueError) as error:
        raise SystemExit(f"verify.sh: cannot hold the checkout descriptors ({error})") from None
