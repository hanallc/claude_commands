"""One folder per Claude session in the session root, named for the session's shown name.

The session root is build_folders.conf roots.session (/session on natedev, ~/session on the Mac).
A session folder holds the session's TMPDIR, its Claude Code scratchpad (claude-<uid>), its tool
folders, its scratch folders (scripts/lib/scratch.py) and, in `run`, a unit's run folders. The folder name is the staff member's name (`verify`, `builds`);
a worker adds its unit (`demo-impl`); a session with no staff row takes its working folder's name.
A name gets `-2`, `-3` and so on only while a live session holds the plain name (user decision 2,
2026-11-15).

The name, not the conversation, keeps the folder: a new conversation of the same member uses the
same folder, so a unit's run folder stays in place. `.session.json` in the folder names the owner
and the latest session id. `<root>/.id/<session id>` is a link to the folder name, so a hook finds
its session's folder with one readlink.

A launcher that knows the member makes the folder first and starts Claude Code with TMPDIR set to
it and CLAUDE_SESSION_FOLDER set to its name; the session-start hook then adopts that folder.

Until the session root exists (before the nixos lines are live), sessions keep the former root,
/tmp/claude/session/<session id>.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from scripts.production import staff

ROOT_ENV = "SESSION_TEMP_ROOT"
# Tests point the former root elsewhere.
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


def configured_root() -> Path:
    """The session root from the environment or build_folders.conf, not yet checked."""
    from_environment = os.environ.get(ROOT_ENV)
    if from_environment:
        return Path(from_environment)
    from scripts.lib import conf

    return Path(conf.read_or_repository("build_folders").text("roots.session")).expanduser()


def session_root() -> Path | None:
    """The session root when it can take folders, else None (the former root applies)."""
    root = configured_root()
    return root if root.is_dir() and os.access(root, os.W_OK | os.X_OK) else None


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
    folder = root / name
    return folder if "/" not in name and folder.is_dir() else None


def link(root: Path, session_id: str, folder: Path) -> None:
    index = root / INDEX
    index.mkdir(exist_ok=True)
    temporary = index / f".{session_id}.{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(folder.name)
    os.replace(temporary, index / session_id)


def claim(root: Path, owner: str, base: str, session_id: str) -> Path:
    """The first name no other live session holds, made or taken over. Hold the root lock."""
    number = 1
    while True:
        folder = root / (base if number == 1 else f"{base}-{number}")
        if not os.path.lexists(folder):
            folder.mkdir(mode=0o700)
            write_record(folder, FolderRecord(owner=owner, session_id=session_id))
            return folder
        record = read_record(folder)
        if folder.is_dir() and not folder.is_symlink() and not held_by_another(record, session_id):
            if record != FolderRecord(owner=owner, session_id=session_id):
                write_record(folder, FolderRecord(owner=owner, session_id=session_id))
            return folder
        number += 1


def launch_folder(member: Member, session_id: str = "") -> Path | None:
    """The member's folder for a launch, made when absent; None before the session root exists."""
    root = session_root()
    if root is None:
        return None
    with root_lock(root):
        folder = claim(root, member.id, folder_name(member, session_id), session_id)
        if session_id:
            link(root, session_id, folder)
    return folder


def adopted(root: Path) -> Path | None:
    """The folder a launcher made for this session: TMPDIR names it and FOLDER_ENV matches."""
    name, temporary = os.environ.get(FOLDER_ENV, ""), os.environ.get("TMPDIR", "")
    if not name or not temporary or Path(temporary) != root / name or read_record(root / name) is None:
        return None
    return root / name


def session_folder(session_id: str, cwd: str = "") -> Path | None:
    """Find or make this session's folder; None until the session root exists."""
    root = session_root()
    if root is None:
        return None
    found = folder_for(root, session_id)
    if found is not None:
        return found
    with root_lock(root):
        launched = adopted(root)
        if launched is not None:
            record = read_record(launched)
            holder = record["owner"] if record is not None else session_id
            write_record(launched, FolderRecord(owner=holder, session_id=session_id))
            folder = launched
        else:
            owner = member_of(session_id)
            folder = claim(root, owner.id if owner is not None else session_id,
                           folder_name(owner, session_id, cwd), session_id)
        link(root, session_id, folder)
    return folder


def caller_folder() -> Path | None:
    """The calling session's folder, made when absent; None with no session id or no session root."""
    session_id = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    return session_folder(session_id) if session_id else None


def restore(session_id: str) -> None:
    """Make this session's folder again when something removed it; an error changes nothing."""
    try:
        root = session_root()
        index = root / INDEX / session_id if root is not None else None
        if root is not None and index is not None and os.path.lexists(index):
            if folder_for(root, session_id) is None:
                with root_lock(root):
                    folder = root / os.readlink(index)
                    if folder.parent == root and not os.path.lexists(folder):
                        owner = member_of(session_id)
                        folder.mkdir(mode=0o700)
                        write_record(folder, FolderRecord(owner=owner.id if owner is not None else session_id,
                                                          session_id=session_id))
            return
        folder = former_folder(session_id)
        if not os.path.lexists(folder):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        pass
