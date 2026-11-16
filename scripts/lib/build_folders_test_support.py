"""Find the folders that build_folders.tool_folder gives tool runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast


def _tool_name(name: str, tool: str) -> bool:
    """True for <tool> and for <tool>-2, <tool>-3 and up."""
    number = name.removeprefix(tool + "-")
    return name == tool or (number != name and number.isdigit())


def _lease_pid(folder: Path) -> object:
    try:
        record = cast(object, json.loads(folder.with_name(folder.name + ".lease.json").read_text()))
    except (OSError, ValueError):
        return None
    return cast(dict[str, object], record).get("pid") if isinstance(record, dict) else None


def tool_folders(parent: Path, tool: str, *, pid: int | None = None, nested: bool = False) -> list[Path]:
    """List the tool's run folders in parent, or at any depth when nested.

    A pid keeps only the folders whose lease names that process.
    """
    candidates = parent.glob(f"**/{tool}*") if nested else parent.glob(f"{tool}*")
    return [path for path in candidates
            if path.is_dir() and _tool_name(path.name, tool) and (pid is None or _lease_pid(path) == pid)]
