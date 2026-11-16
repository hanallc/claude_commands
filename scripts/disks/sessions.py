"""Read each session folder's size, held folders and state for the Disks page."""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePath
from typing import Literal, TypedDict

from scripts.disks import measure
from scripts.disks.measure import DisksData, Mount
from scripts.lib import scratch, session_folder
from scripts.message import session_records

State = Literal["running", "ended", "unknown"]


class HeldFolder(TypedDict):
    name: str
    task: str
    # The task ended, and unsaved work keeps the folder.
    ended: bool


class SessionRow(TypedDict):
    name: str
    size: int | None
    held: list[HeldFolder]
    state: State


class SessionReport(TypedDict):
    root: str
    sessions: list[SessionRow]


def session_states(registry: session_records.RegistryRead) -> Callable[[str], State]:
    """Each session's state from one read of the session registry."""
    def state(session_id: str) -> State:
        observed = session_records.session_liveness(session_id, registry)
        if isinstance(observed, session_records.LiveSessionProcess):
            return "running"
        return "ended" if isinstance(observed, session_records.SessionNotLive) else "unknown"

    return state


def held_folders(folder: Path, holds: Mapping[str, scratch.Hold]) -> list[HeldFolder]:
    """The folders that a hold keeps in a session folder, by their path in it."""
    real = PurePath(os.path.realpath(folder))
    return sorted(({"name": str(PurePath(path).relative_to(real)), "task": hold["task"], "ended": hold["ended"] > 0}
                   for path, hold in holds.items() if real in PurePath(path).parents),
                  key=lambda held: held["name"])


def read_sessions(root: Path, data: DisksData | None, holds: Mapping[str, scratch.Hold], mounts: Sequence[Mount],
                  state: Callable[[str], State]) -> SessionReport:
    """Each session folder under root, largest first, with its held folders and whether its session runs."""
    rows: list[SessionRow] = []
    folders: list[Path] = sorted(root.iterdir()) if root.is_dir() else []
    for folder in folders:
        if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
            continue
        record = session_folder.read_record(folder)
        rows.append({"name": folder.name, "size": measure.tree_size(data, measure.drive_path(folder, mounts)),
                     "held": held_folders(folder, holds),
                     "state": state(record["session_id"]) if record and record["session_id"] else "unknown"})
    rows.sort(key=lambda row: (-(row["size"] or 0), row["name"]))
    return {"root": str(root), "sessions": rows}


def session_report() -> SessionReport | None:
    """The session folders on this machine, or None until the session root exists."""
    root = session_folder.session_root()
    if root is None:
        return None
    return read_sessions(root, measure.read_data(), scratch.holds(), measure.read_mounts(),
                         session_states(session_records.read_registry()))
