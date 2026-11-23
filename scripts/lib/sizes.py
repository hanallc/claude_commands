"""Byte size constants and their GiB and MiB text forms."""
from __future__ import annotations

GIB = 1 << 30
MIB = 1 << 20


def gib(size: int) -> str:
    return f"{size / GIB:.1f} GiB"


def mib(size: int) -> str:
    return f"{size / MIB:.1f} MiB"
