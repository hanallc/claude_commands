"""Copy locations and record reading, kept as a leaf because nix_env imports it.
Load build folder helpers inside the readers to avoid an import cycle through nix_env.
The legacy workspace root remains available for drain and resume.
"""
from __future__ import annotations

import json
import os
import re
import stat
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
SNAPSHOT_ORIGIN = "snapshot-origin.json"
"""The binding that `copies.py run` writes beside the snapshot checkout of a request."""
FULL_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
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
    configured = os.environ.get(COPIES_ENV) or _configured_root("roots.workspace")
    return Path(os.path.realpath(Path(configured).expanduser()))


def worktree_root() -> Path:
    """Return the configured source root."""
    return Path(_configured_root("roots.workspace")).expanduser().resolve()


def build_root() -> Path:
    """Return the configured build root for this host."""
    return Path(_configured_root("roots.build")).expanduser().resolve()


@dataclass(frozen=True)
class OutsideNumberedCopy:
    """The path is outside a numbered copy."""


def numbered_copy_checkout(path: Path) -> Path | OutsideNumberedCopy:
    """Return the configured checkout path for a numbered copy or one of its children."""
    from scripts.lib.build_folders import BuildCopy, UnverifiedFolderPath, copy_path, parse_form

    base = build_root()
    for location in (path.absolute(), Path(os.path.realpath(path))):
        if not location.is_relative_to(base):
            continue
        parts = location.relative_to(base).parts
        if len(parts) >= 3:
            form = parse_form(base.joinpath(*parts[:3]))
            if isinstance(form, UnverifiedFolderPath):
                raise form.error
            if isinstance(form, BuildCopy):
                return copy_path(form.repo, form.unit, form.number)
    return OutsideNumberedCopy()


def is_numbered_copy_path(path: Path) -> bool:
    """Check whether a path lies in a numbered copy."""
    return isinstance(numbered_copy_checkout(path), Path)


def is_legacy_copy_slot(path: Path) -> bool:
    """Check the exact slot for a caller that still writes legacy records."""
    return path.parent.parent == copies_root() and not is_numbered_copy_path(path)


def has_legacy_copy_evidence(path: Path) -> bool:
    """Keep unreadable legacy evidence visible to recovery."""
    from scripts.lib.build_folders import removal_mark

    return (os.path.lexists(removal_mark(path))
            or not path.is_symlink() and os.path.lexists(path / RECORD))


def copy_folder(path: Path) -> Path | None:
    """Return a legacy copy with record evidence, including an interrupted removal."""
    resolved = path.resolve()
    if is_numbered_copy_path(resolved):
        return None
    for base in (copies_root(), FORMER_COPIES_ROOT.resolve()):
        if not resolved.is_relative_to(base):
            continue
        parts = resolved.relative_to(base).parts
        if len(parts) >= 2:
            folder = base.joinpath(*parts[:2])
            if has_legacy_copy_evidence(folder):
                return folder
    return None


def record_path(folder: Path) -> Path | None:
    """Find the legacy task record for a copy."""
    folder = folder.resolve()
    if is_numbered_copy_path(folder):
        return None
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


def _private_folder(path: Path) -> bool:
    """Whether a real folder belongs to this user and no one else can write in it."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return status.st_uid == os.getuid() and not status.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _read_regular_json(path: Path) -> object:
    """Read JSON from a regular file without following a symbolic link."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("expected a regular file")
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as source:
            try:
                return cast(object, json.load(source))
            except RecursionError as error:
                raise ValueError("JSON nesting is too deep") from error
    finally:
        os.close(descriptor)


def read_record(path: Path) -> Registered | NotRegistered | UnreadableRecord:
    try:
        raw = _read_regular_json(path)
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
class RecordedCopySource:
    """The copy record names its source checkout and commit."""

    checkout: Path
    commit: str


@dataclass(frozen=True)
class NoRecordedCopySource:
    """The checkout has no available copy source record."""


def source_of(folder: Path) -> RecordedCopySource | NoRecordedCopySource | UnreadableRecord:
    """Read the source checkout and commit from the copy's own record."""
    from scripts.lib import build_folders

    folder = folder.resolve()
    numbered = numbered_copy_checkout(folder)
    if isinstance(numbered, Path):
        if numbered.resolve() != folder:
            return NoRecordedCopySource()
        record = build_folders.numbered_copy_record(numbered)
        if isinstance(record, build_folders.NumberedCopySource):
            return RecordedCopySource(record.source, record.commit)
        if isinstance(record, build_folders.UnreadableNumberedCopySource):
            return UnreadableRecord(record.reason)
        return NoRecordedCopySource()
    path = record_path(folder)
    if path is not None:
        legacy = read_record(path)
        if isinstance(legacy, UnreadableRecord):
            return legacy
        if isinstance(legacy, Registered):
            checkout, commit = legacy.fields.get("source_path"), legacy.fields.get("source_commit")
            if isinstance(checkout, str) and checkout and isinstance(commit, str) and commit:
                return RecordedCopySource(Path(checkout), commit)
            return UnreadableRecord(f"{path}: copy record has no source checkout or commit")
        return NoRecordedCopySource()
    return snapshot_source(folder)


def snapshot_source(folder: Path) -> RecordedCopySource | NoRecordedCopySource:
    """Read the repository and commit that a `copies.py run` snapshot comes from.

    The binding counts only in a private folder of this user, as the run's tool folder is.
    It names its own checkout and a full commit id, so it proves only that folder and commit.
    """
    try:
        if not _private_folder(folder.parent):
            return NoRecordedCopySource()
        raw = _read_regular_json(folder.parent / SNAPSHOT_ORIGIN)
    except (OSError, UnicodeError, ValueError):
        return NoRecordedCopySource()
    binding = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    repository, commit = binding.get("repository"), binding.get("source_commit")
    if (binding.get("checkout") != str(folder) or not isinstance(repository, str) or not repository
            or not isinstance(commit, str) or not FULL_COMMIT.fullmatch(commit)):
        return NoRecordedCopySource()
    return RecordedCopySource(Path(repository), commit)


@dataclass(frozen=True)
class AllRepositories:
    """Remove copies from every repository."""


@dataclass(frozen=True)
class SelectedRepository:
    """Remove copies from one repository identity."""

    name: str


RepositoryScope = AllRepositories | SelectedRepository


ALL_REPOSITORIES = AllRepositories()
