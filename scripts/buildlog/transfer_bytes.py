"""The byte count of one SSH, rsync or object transfer; claim records and the build log share it."""
from __future__ import annotations

from typing import TypedDict


class TransferBytes(TypedDict):
    operation: str
    sent_bytes: int
    received_bytes: int
    complete: bool
