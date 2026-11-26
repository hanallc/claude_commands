"""Owner proof for a message lookup."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum

from scripts.herdr.place import HerdrPlace


@dataclass(frozen=True)
class ProvedMessageOwner:
    conversation: str
    pid: int
    start: str


class StrictOwnerProof(Enum):
    REQUIRED = "strict proof required"


type MessageOwnerProof = ProvedMessageOwner | StrictOwnerProof

@dataclass
class MessageLookup:
    owner: MessageOwnerProof
    starting_panes: set[HerdrPlace] = field(default_factory=set)


active_lookup: ContextVar[MessageLookup | None] = ContextVar("herdr_message_lookup", default=None)


@contextmanager
def message_lookup(owner: MessageOwnerProof) -> Generator[MessageLookup]:
    """Set the owner proof for one message lookup."""
    lookup = MessageLookup(owner)
    token = active_lookup.set(lookup)
    try:
        yield lookup
    finally:
        active_lookup.reset(token)
