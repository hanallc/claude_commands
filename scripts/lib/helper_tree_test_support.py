"""Copy selected helper folders into a private fixture home."""

from __future__ import annotations

from functools import cache
from pathlib import Path
import os
import shutil
from time import perf_counter
from typing import NamedTuple

from scripts.lib.checkout_test_support import runtime_files


SOURCE = Path(__file__).resolve().parents[2]
COPY_HELPERS = (
    "scripts/lib", "scripts/buildlog", "scripts/copies", "scripts/mac_test", "scripts/message",
    "scripts/production", "scripts/agents", "scripts/direct", "scripts/hooks", "scripts/lint", "scripts/herdr",
    "scripts/build_hold", "scripts/build_queue", "scripts/build_targets", "scripts/test_times",
)
VERIFY_HELPERS = (*COPY_HELPERS, "scripts/whoami", "scripts/notify")


class CopyTime(NamedTuple):
    seconds: float
    files: int
    bytes: int


COPY_TIMES: list[CopyTime] = []


@cache
def helper_files(folders: tuple[str, ...]) -> tuple[Path, ...]:
    """Use the shared runtime catalog for the selected folders."""
    selected = tuple(Path(name) for name in ("scripts/lib", *folders))
    return tuple(relative for relative in runtime_files()
                 if any(relative.is_relative_to(folder) for folder in selected))


def copy_helpers(home: Path, *folders: str) -> Path:
    """Copy helper folders without replacing the fixture's existing stubs."""
    began = perf_counter()
    installed = home / ".claude"
    installed.mkdir(parents=True, exist_ok=True)
    count = size = 0
    files = helper_files(tuple(folders))
    selected_files = frozenset(files)
    for relative in files:
        destination = installed / relative
        if destination.is_symlink():
            target = Path(os.readlink(destination))
            if target.is_absolute() or not destination.resolve().is_relative_to(installed):
                raise ValueError(f"The fixture helper path leaves the copied tree: {destination}")
            continue
        for parent in destination.parents:
            if parent == home:
                break
            if parent.is_symlink():
                raise ValueError(f"The fixture helper path is a link: {parent}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            source = SOURCE / relative
            target = Path(os.readlink(source)) if source.is_symlink() else None
            resolved = source.resolve()
            inside = resolved.is_relative_to(SOURCE) and resolved.relative_to(SOURCE) in selected_files
            if target is not None and not target.is_absolute() and inside:
                destination.symlink_to(target)
            else:
                _ = shutil.copy2(source, destination)
            count += 1
            size += destination.lstat().st_size
    COPY_TIMES.append(CopyTime(perf_counter() - began, count, size))
    return installed / "scripts"

