"""Read text from proc files that can contain invalid UTF-8."""
from __future__ import annotations

from pathlib import Path


def decode_proc_text(data: bytes) -> str:
    """Replace invalid UTF-8 in proc text."""
    return data.decode("utf-8", errors="replace")


def read_proc_text(path: Path) -> str:
    """Replace invalid UTF-8 and let file errors reach the caller."""
    return decode_proc_text(path.read_bytes())
