"""Copy locations and record reading, kept as a leaf because nix_env imports it.
scratch.py reaches herdr, which imports nix_env; only the standard library and conf belong here.
build_folders.conf roots.scratch and roots.copies set the two roots.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT_ENV = "SCRATCH_ROOT"
# natedev's roots before /build. The sweeps drain them by the usual task rules.
FORMER_ROOT = Path("/tmp/claude/scratch")
FORMER_COPIES_ROOT = Path("/tmp/claude/copies")
COPIES_ENV = "SCRATCH_COPIES_ROOT"
RECORD = ".scratch.json"
TEMPORARY_GATE_RECORD = ".gate-copy.json"
TASK = re.compile(r"(?P<kind>run|phase|row|gate|until|landed):(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_./-]+)?)")


@dataclass(frozen=True)
class Registered:
    """The copy registration holds its task and saved fields."""
    task: str
    fields: dict[str, object]


@dataclass(frozen=True)
class NotRegistered:
    """The copy has no registration record."""


@dataclass(frozen=True)
class UnreadableRecord:
    """The reason explains why the copy registration record cannot be read."""
    reason: str


# A sweep asks for a root once per entry; a parse costs 0.35 ms, a stat a few microseconds.
_ROOTS: dict[str, tuple[tuple[str, int, int], str]] = {}


def _configured_root(key: str) -> str:
    from scripts.lib import conf

    live = conf.path("build_folders")
    try:
        status = live.stat()
        stamp = (str(live), status.st_mtime_ns, status.st_size)
    except FileNotFoundError:
        stamp = (str(live), -1, -1)
    cached = _ROOTS.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    value = conf.read_or_repository("build_folders").text(key)
    _ROOTS[key] = (stamp, value)
    return value


def root() -> Path:
    return Path(os.environ.get(ROOT_ENV) or _configured_root("roots.scratch")).expanduser()


def copies_root() -> Path:
    # A SCRATCH_ROOT override keeps its copies beside it.
    configured = (os.environ.get(COPIES_ENV) or (str(root().parent / "copies") if os.environ.get(ROOT_ENV)
                                                 else _configured_root("roots.copies")))
    return Path(os.path.realpath(Path(configured).expanduser()))


def copy_folder(path: Path) -> Path | None:
    """The copy that contains path; convert the path boundary to no copy here."""
    base = copies_root()
    resolved = path.resolve()
    if resolved.is_relative_to(base):
        parts = resolved.relative_to(base).parts
        if len(parts) >= 2:
            return base / parts[0] / parts[1]
    return None


def record_path(folder: Path) -> Path | None:
    """Find the record for an editable copy, a temporary gate, or a task clone."""
    folder = folder.resolve()
    if copy_folder(folder) == folder:
        return folder / RECORD
    if folder.name == "checkout":
        path = folder.parent / TEMPORARY_GATE_RECORD
        record = read_record(path)
        if isinstance(record, Registered) and record.task.startswith("gate:"):
            return path
    if folder.parent == root().resolve():
        path = folder / RECORD
        record = read_record(path)
        if isinstance(record, Registered) and record.fields.get("test_hana_clone") is True:
            return path
    return None


def read_record(path: Path) -> Registered | NotRegistered | UnreadableRecord:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NotRegistered()
    except (OSError, UnicodeError, ValueError) as error:
        return UnreadableRecord(f"repair record {path}: {error}")
    fields = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    task = fields.get("task")
    if not isinstance(task, str) or not TASK.fullmatch(task):
        return UnreadableRecord(f"repair record {path}: invalid task")
    return Registered(task, fields)


@dataclass(frozen=True)
class AllRepositories:
    """Remove copies from every repository."""


@dataclass(frozen=True)
class SelectedRepository:
    """Remove copies from one repository identity."""

    name: str


RepositoryScope = AllRepositories | SelectedRepository


ALL_REPOSITORIES = AllRepositories()
