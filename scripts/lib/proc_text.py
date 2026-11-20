"""Read text from proc files that can contain invalid UTF-8."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
import re


MOUNT_TABLE = Path("/proc/self/mountinfo")
MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def decode_proc_text(data: bytes) -> str:
    """Replace invalid UTF-8 in proc text."""
    return data.decode("utf-8", errors="replace")


def read_proc_text(path: Path) -> str:
    """Replace invalid UTF-8 and let file errors reach the caller."""
    return decode_proc_text(path.read_bytes())


@dataclass(frozen=True)
class Mount:
    """Identify a filesystem and the paths it maps."""

    device: str
    root: PurePath
    point: PurePath


def read_mounts(table: Path = MOUNT_TABLE) -> tuple[Mount, ...]:
    """Read mounts in table order."""
    try:
        lines = table.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    def path(text: str) -> PurePath:
        return PurePath(MOUNT_ESCAPE.sub(lambda match: chr(int(match[1], 8)), text))

    return tuple(Mount(fields[2], path(fields[3]), path(fields[4]))
                 for fields in map(str.split, lines) if len(fields) > 4)
