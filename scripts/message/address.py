"""Recognize reserved message addresses."""

USER = "user"
STAFF_PREFIX = "staff:"
SESSION_PREFIX = "session:"
WORKER_PREFIX = "worker:"
SOCKET_PREFIX = "uds:"
CODEX_PREFIX = "codex:"


def reserved_recipient(name: str) -> bool:
    """Check whether a name uses address syntax."""
    name = name.casefold()
    return name == USER or name.startswith((STAFF_PREFIX, SESSION_PREFIX, WORKER_PREFIX, SOCKET_PREFIX, CODEX_PREFIX))
