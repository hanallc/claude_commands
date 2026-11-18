"""Count transfer bytes without loading build-log synchronization."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from typing import TypedDict


class TransferBytes(TypedDict):
    operation: str
    sent_bytes: int
    received_bytes: int
    complete: bool


@dataclass
class TransferLedger:
    """Application bytes for each SSH or rsync exchange."""

    transfers: list[TransferBytes] = field(default_factory=list)

    def observe(self, operation: str, arguments: list[str], stdout: str, stderr: str) -> None:
        if arguments[0] == "rsync":
            sent = re.search(r"Total bytes sent:\s*([\d,]+)", stdout)
            received = re.search(r"Total bytes received:\s*([\d,]+)", stdout)
            summary = re.search(r"sent ([\d,]+) bytes\s+received ([\d,]+) bytes", stdout)
            complete = bool((sent and received) or summary)
            sent_bytes = int(sent[1].replace(",", "")) if sent else int(summary[1].replace(",", "")) if summary else 0
            received_bytes = int(received[1].replace(",", "")) if received else int(summary[2].replace(",", "")) if summary else 0
        elif arguments[0] == "ssh":
            sent_bytes = len(arguments[-1].encode("utf-8"))
            received_bytes = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
            complete = True
        else:
            return
        self.transfers.append({"operation": operation, "sent_bytes": sent_bytes,
                               "received_bytes": received_bytes, "complete": complete})

    def observe_bytes(self, operation: str, arguments: list[str], stdout: bytes, stderr: bytes) -> None:
        self.observe(operation, arguments, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))
        if arguments[0] == "ssh":
            self.transfers[-1]["received_bytes"] = len(stdout) + len(stderr)


@dataclass(frozen=True)
class NoRunTransfers:
    """No run receives the exchange measurements."""


active_transfers: ContextVar[TransferLedger | NoRunTransfers] = ContextVar("active_transfers", default=NoRunTransfers())


@contextmanager
def measure_transfers(ledger: TransferLedger) -> Generator[None]:
    token = active_transfers.set(ledger)
    try:
        yield
    finally:
        active_transfers.reset(token)
