"""Select a named folder for each session.

The folder holds TMPDIR, scratch folders and the unit's run folders.
The `.session.json` record names the owner and the latest session ID.
The `.id` index links session IDs to folder names within each root.
The launcher sets TMPDIR and CLAUDE_SESSION_FOLDER for the session-start hook.
The former named root keeps live sessions and open runs.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from scripts.production import staff

ROOT_ENV = "SESSION_TEMP_ROOT"
# Tests can select a private former named root.
PREVIOUS_ROOT_ENV = "SESSION_PREVIOUS_ROOT"
# Tests can select a private legacy root for folders named by session ID.
FORMER_ROOT_ENV = "SESSION_FORMER_ROOT"
FORMER_ROOT = Path("/tmp/claude/session")
# Set by a launcher to the folder name it made; read by the session-start hook.
FOLDER_ENV = "CLAUDE_SESSION_FOLDER"
RECORD = ".session.json"
INDEX = ".id"
LOCK = ".lock"
# The run root in a session folder (scripts/lib/unit_runs.py); no other folder takes this name.
RUN = "run"
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Member:
    """The staff member that owns a session folder."""

    id: str
    name: str


class FolderRecord(TypedDict):
    owner: str
    session_id: str


def configured_roots() -> tuple[Path, ...]:
    """Read the current root first and the former named root when configured."""
    from scripts.lib import conf

    current = os.environ.get(ROOT_ENV)
    previous = os.environ.get(PREVIOUS_ROOT_ENV)
    if current:
        root = Path(current)
    else:
        settings = conf.read_or_repository("build_folders")
        root = Path(settings.text("roots.session")).expanduser()
        if previous is None:
            previous = settings.values("roots.session_former").get("roots.session_former")
    return tuple(dict.fromkeys((root, Path(previous).expanduser()))) if previous else (root,)


def configured_root() -> Path:
    """Read the configured destination for new session folders."""
    return configured_roots()[0]


def available_root(root: Path) -> Path | None:
    """Return the root when the session can write to it."""
    return root if root.is_dir() and os.access(root, os.W_OK | os.X_OK) else None


def session_root() -> Path | None:
    """Find the current root when it can take folders."""
    return available_root(configured_root())


def allocation_root(root: Path) -> Path | None:
    """Create a missing configured root when a session needs it."""
    if not os.environ.get(ROOT_ENV) and not root.is_dir():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return available_root(root)


def former_root() -> Path:
    return Path(os.environ.get(FORMER_ROOT_ENV) or FORMER_ROOT)


def former_folder(session_id: str) -> Path:
    return former_root() / session_id


def folder_name(member: Member | None, session_id: str, cwd: str = "") -> str:
    """The member's name, else the working folder's name, as one safe path component."""
    shown = member.name if member is not None else Path(cwd).name
    return UNSAFE.sub("-", shown).strip(".-") or f"session-{session_id[:6]}"


def member_for(row: staff.StaffRow, record: staff.StaffSnapshot) -> Member:
    """The row as a folder owner: a worker's name starts with its unit's name (demo-impl)."""
    from scripts.production import staff

    name = row.name
    parent = record.by_id(row.parent.id) if row.kind == "worker" and isinstance(row.parent, staff.ReportsTo) else None
    if parent is not None and not name.startswith(f"{parent.name}-"):
        name = f"{parent.name}-{name}"
    return Member(str(row.id), name)


def member_of(session_id: str) -> Member | None:
    """The staff member whose current session this is, from this machine's record only."""
    from scripts.production import staff

    try:
        record = staff.local_snapshot()
        row = record.by_current_session(session_id)
    except (OSError, ValueError):
        return None
    return member_for(row, record) if row is not None else None


def held_by_another(record: FolderRecord | None, session_id: str) -> bool:
    """Whether a live session other than this one holds the folder; unknown counts as live."""
    if record is None or not record["session_id"] or record["session_id"] == session_id:
        return False
    from scripts.message import session_records

    try:
        observed = session_records.session_liveness(record["session_id"])
    except (OSError, ValueError):
        return True
    return not isinstance(observed, session_records.SessionNotLive)


def read_record(folder: Path) -> FolderRecord | None:
    try:
        raw = cast(object, json.loads((folder / RECORD).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    fields = cast(dict[str, object], raw)
    owner, session_id = fields.get("owner"), fields.get("session_id")
    if not isinstance(owner, str) or not isinstance(session_id, str):
        return None
    return FolderRecord(owner=owner, session_id=session_id)


def write_record(folder: Path, record: FolderRecord) -> None:
    temporary = folder / f"{RECORD}.{os.getpid()}"
    _ = temporary.write_text(json.dumps(record) + "\n", encoding="utf-8")
    os.replace(temporary, folder / RECORD)


@contextmanager
def root_lock(root: Path) -> Generator[None]:
    with (root / LOCK).open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def folder_for(root: Path, session_id: str) -> Path | None:
    """The folder the index names for this session, when it still exists."""
    try:
        name = os.readlink(root / INDEX / session_id)
    except OSError:
        return None
    if name in ("", ".", "..") or "/" in name:
        return None
    folder = root / name
    return folder if not folder.is_symlink() and folder.is_dir() else None


def link(root: Path, session_id: str, folder: Path) -> None:
    index = root / INDEX
    index.mkdir(exist_ok=True)
    temporary = index / f".{session_id}.{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(folder.name)
    os.replace(temporary, index / session_id)


def select_folder(root: Path, base: str, session_id: str) -> Path:
    """Return the first available folder name without changing any records."""
    number = 1
    while True:
        folder = root / (base if number == 1 else f"{base}-{number}")
        if not os.path.lexists(folder):
            return folder
        if not folder.is_symlink() and folder.is_dir() and not held_by_another(read_record(folder), session_id):
            return folder
        number += 1


def claim(folder: Path, owner: str, session_id: str) -> Path:
    """Create or take over the selected folder. Hold the root lock."""
    if not os.path.lexists(folder):
        folder.mkdir(mode=0o700)
    elif folder.is_symlink() or not folder.is_dir():
        raise FileExistsError(f"{folder}: selected session folder changed")
    record = FolderRecord(owner=owner, session_id=session_id)
    if read_record(folder) != record:
        write_record(folder, record)
    return folder


def holds_run(folder: Path, now: float, record: staff.StaffSnapshot) -> bool:
    """Check the staff pointers and the heartbeat for runs in this folder."""
    from scripts.production import staff

    unit_runs = import_module("scripts.lib.unit_runs")
    live_run_folders = cast(Callable[[float, tuple[Path, ...]], list[Path]], unit_runs.live_run_folders)
    runs = (folder / RUN).resolve()
    return (any(isinstance(row.run, staff.OpenRun) and row.run.path.resolve().is_relative_to(runs)
                for row in record.rows)
            or bool(live_run_folders(now, (runs,))))


def claim_at_start(root: Path, former_roots: tuple[Path, ...], owner: str, base: str, session_id: str) -> Path:
    """Claim the new folder or retain a former folder with an open run. Hold the current root lock."""
    from scripts.production import staff

    folder = select_folder(root, base, session_id)
    current = read_record(folder)
    if current is None or current["owner"] != owner:
        for former in former_roots:
            if not former.is_dir() or former.samefile(root):
                continue
            with root_lock(former):
                for candidate in sorted(former.iterdir()):
                    if candidate.name.startswith(".") or candidate.is_symlink() or not candidate.is_dir():
                        continue
                    holder = read_record(candidate)
                    if holder is None or holder["owner"] != owner or held_by_another(holder, session_id):
                        continue
                    if not (candidate / RUN).is_dir():
                        continue
                    try:
                        keep = holds_run(candidate, time.time(), staff.local_snapshot())
                    except (OSError, ValueError):
                        keep = True
                    if keep:
                        claimed = claim(candidate, owner, session_id)
                        if session_id:
                            link(former, session_id, claimed)
                        return claimed
    claimed = claim(folder, owner, session_id)
    if session_id:
        link(root, session_id, claimed)
    return claimed


def launch_folder(member: Member, session_id: str = "") -> Path | None:
    """Find or create the member's folder before the launcher sets TMPDIR."""
    current, *former_roots = configured_roots()
    root = allocation_root(current)
    if root is None:
        return None
    with root_lock(root):
        folder = folder_for(root, session_id) if session_id else None
        if folder is None:
            folder = claim_at_start(root, tuple(former_roots), member.id, folder_name(member, session_id), session_id)
    return folder


def adopted(root: Path) -> Path | None:
    """The folder a launcher made for this session: TMPDIR names it and FOLDER_ENV matches."""
    name, temporary = os.environ.get(FOLDER_ENV, ""), os.environ.get("TMPDIR", "")
    if not name or not temporary or Path(temporary) != root / name or read_record(root / name) is None:
        return None
    return root / name


def session_folder(session_id: str, cwd: str = "", *, starting: bool = False) -> Path | None:
    """Find or make this session's folder. Evaluate the former folder's runs at startup."""
    current, *former_roots = configured_roots()
    found = folder_for(current, session_id)
    temp = os.environ.get("TMPDIR", "")
    previous_owner = None
    for former in former_roots:
        previous = folder_for(former, session_id)
        if previous is not None:
            holder = read_record(previous)
            if holder is not None and holder["session_id"] == session_id:
                if temp == str(previous) or (found is None and not starting):
                    return previous
                previous_owner = Member(holder["owner"], previous.name)
        launched = adopted(former)
        if launched is not None:
            with root_lock(former):
                holder = read_record(launched)
                if holder is not None and holder["session_id"] in ("", session_id):
                    folder = claim(launched, holder["owner"], session_id)
                    link(former, session_id, folder)
                    return folder
    if found is not None:
        return found
    root = allocation_root(current)
    if root is None:
        return None
    with root_lock(root):
        found = folder_for(root, session_id)
        if found is not None:
            return found
        launched = adopted(root)
        if launched is not None:
            record = read_record(launched)
            holder = record["owner"] if record is not None else session_id
            folder = claim(launched, holder, session_id)
            link(root, session_id, folder)
        else:
            owner = member_of(session_id) or previous_owner
            holder = owner.id if owner is not None else session_id
            folder = claim_at_start(root, tuple(former_roots), holder, folder_name(owner, session_id, cwd), session_id)
    return folder


def caller_folder() -> Path | None:
    """The calling session's folder, made when absent; None with no session id or no session root."""
    session_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    return session_folder(session_id) if session_id else None


def restore(session_id: str) -> None:
    """Make this session's folder again when something removed it; an error changes nothing."""
    try:
        current, *former_roots = configured_roots()
        for root in (current, *former_roots):
            index = root / INDEX / session_id
            if not index.is_symlink():
                continue
            if folder_for(root, session_id) is not None:
                return
            with root_lock(root):
                folder = root / os.readlink(index)
                if root != current and os.environ.get("TMPDIR") != str(folder):
                    continue
                if folder.parent == root and not os.path.lexists(folder):
                    owner = member_of(session_id)
                    _ = claim(folder, owner.id if owner is not None else session_id, session_id)
            return
        folder = former_folder(session_id)
        if not os.path.lexists(folder):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        pass
