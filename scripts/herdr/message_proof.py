"""Owner proof for a message lookup."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ProvedMessageOwner:
    conversation: str
    pid: int
    start: str


class StrictOwnerProof(Enum):
    REQUIRED = "strict proof required"


type MessageOwnerProof = ProvedMessageOwner | StrictOwnerProof

message_owner: ContextVar[MessageOwnerProof] = ContextVar("herdr_message_owner", default=StrictOwnerProof.REQUIRED)


@contextmanager
def message_lookup(owner: MessageOwnerProof) -> Generator[None]:
    """Set the owner proof for one message lookup."""
    token = message_owner.set(owner)
    try:
        yield
    finally:
        message_owner.reset(token)
