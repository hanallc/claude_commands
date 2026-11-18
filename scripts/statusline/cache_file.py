"""Write the cache files for the status line rows."""

from __future__ import annotations

import os
from pathlib import Path


def watched(path: Path) -> Path:
    """Return the path or its nearest existing ancestor."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def write_cache(cache: Path, line: str, due: str, sources: list[Path], began: int, details: str = "") -> None:
    """Replace the cache with the answer, deadline, source paths, and optional result details."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    scratch = cache.with_name(f".{cache.name}.{os.getpid()}")
    named = dict.fromkeys(str(watched(path)) for path in sources)
    _ = scratch.write_text("\n".join([line, due, *named, *([details] if details else [])]) + "\n", encoding="utf-8")
    os.utime(scratch, ns=(began, began))
    _ = scratch.replace(cache)
