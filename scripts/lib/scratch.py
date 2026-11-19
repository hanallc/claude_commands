#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Scratch folders that belong to a task, and go when the task ends.

Agents made loose folders in /tmp and ~/rust (repo copies, repros, test trees) and nothing removed
them. Sessions run for weeks, so a session end is no cleanup point (user, 2026-10-26/27). Each
scratch folder belongs to a task, and the thing that ends the task removes it:

    scratch.py new [--task <task> | --until <when>] [--name <word>] [--visible]   make one; prints its path
    scratch.py new --worker [--name <word>]   make a worker folder under TMPDIR; prints its path
    scratch.py adopt <path> [--task <task> | --until <when>] [--name <word>] [--visible]   move a loose folder in
    scratch.py hold <path> [--task <task> | --until <when>] --reason <why>   keep a folder where it is
    scratch.py release <path>    drop a hold
    scratch.py renew <path>      mark it changed
    scratch.py hand-over <path> --to <unit> --task <task>  replace owner and task without moving it
    scratch.py end --task <task>  remove every folder and held folder of the task
    scratch.py list              each folder and hold, its task and its last change

A task, and what ends it:

  * `run:<run id>`: end_session.sh, at the run's end;
  * `phase:<unit>/<phase>`: merge_checkpoint.py, when the phase's checkpoint merges;
  * `gate:<id>`: the gate's caller, with `scratch.py end`, when the gate ends;
  * `gate:worker-<staff id>`: the worker's assignment; the worker close path ends it;
  * `landed:<unit>/<branch>`: the branch is in its recorded land target and all work is saved;
  * `row:<showrunner>-C<n>`: change_review.py verdict;
  * `until:<YYYY-MM-DDTHHMMZ>/<id>`: the hourly temporary sweep ends the task when its time passes.
    `--until` takes `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` in the user's zone.
    Each call makes a random id with scratch.conf suffix_length characters.
    With no task, a folder gets a separate deadline task for scratch.conf default_life_seconds from now.
    Old deadline tasks without an id remain valid.

A folder of a caller with a session folder (scripts/lib/session_folder.py) lives there as `<word>`, else
`<kind>-<name>` from its task, else `scratch`, with `-2`, `-3` and so on when the name is taken (user
decision 6, 2026-11-15). Its path is in the HOLDS file with its task, so `end` finds it; an until:
folder is not held and goes with its session's other content. Any other folder lives in ROOT
(build_folders.conf roots.scratch) as `<kind>-<name>-<word>-<suffix>`. An until:
task's folder is `<word>-<suffix>`, or `scratch-<suffix>` with no word; its record keeps the time.
scratch.conf suffix_length sets the suffix length.
Worker folders live under TMPDIR as `<showrunner>-<unit>-<worker>-<suffix>[-<word>]`.
The holds record indexes their paths, so task removal does not scan TMPDIR.
`--visible` (rows only) puts it in VISIBLE_ROOT
as `<showrunner>-C<n>-<word>`, where cargo-port lists it. Each holds copy_record.RECORD with its task, maker and
times. The hourly tmp sweep (scripts/lint/tmp_sweep.py) keeps a folder in ROOT while its task is
open, and removes it when its until: time passes, or after 7 idle days whatever its task, since an
ender can fail. A row's folder waits for its verdict, and VISIBLE_ROOT is never swept. A git
worktree removal also removes its registration in the source repository.
Removal writes versioned deletion intent to `.scratch-removing-<folder>.json` beside the source.
The intent preserves the task and source evidence across interruptions.
A retry compares ref file metadata with the intent and checks other file timestamps.
The retry validates the structure of Git metadata before it can resume removal.
A retry checks changed, intact sources for unsaved work.
A retry keeps changed, partly deleted sources and names the changed file.
Completion removes Cargo recovery evidence before task recovery evidence.

A hold keeps a folder that must stay where it is (a repo the user checks a change in). Its record
(task, maker, reason) is in the machine's HOLDS file, not in the folder, where a repo would show it
as uncommitted work. `end` removes a held folder with the task's other folders, unless a repo in
it holds work that is on no remote (unsaved_work): that hold stays, marked ended, for the folder
audit to name to its maker. The scratch guard hook
(scripts/hooks/pre-tool-use-scratch-guard.py) refuses a loose folder in /tmp, ~/rust and the
other roots.

Copies live at <roots.copies>/<pair>/<copy>. Their records also name source and copy commits,
target mode, seed, borrowed git directory, owner and land target. Open phases and borrowed parents
stay; an unreadable record, phase state or land target needs repair. Shared .claude-busy locks
keep a copy while work runs or waits. Removal holds an exclusive lock through deletion.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

if __name__ == "__main__":
    import sys as _sys
    from scripts.lib import scratch as _scratch

    _sys.exit(_scratch.main(_sys.argv[1:]))

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from pathlib import Path, PurePath
from threading import get_ident
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast
from urllib.parse import unquote_to_bytes

from scripts.lib import cargo_output, conf, copy_record, git_files, session_folder, time_limit  # noqa: E402
from scripts.lib.conf import UseConfiguredValue

from scripts.lib.build_folders import (  # noqa: E402
    BuildFolderSpec, FolderRemovalLease, HeldFolderRemovalLease, RefusedRemovalLease, build_folder_path, collector_lease,
    folder_removal_lease, held_build_folder_descriptors, inherited_temporary_parent, open_busy_mark, require_no_removal_mark,
    BUSY as BUSY, REMOVAL_RECORD_PREFIX as REMOVAL_RECORD_PREFIX, REMOVAL_RECORD_SUFFIX as REMOVAL_RECORD_SUFFIX,
    removal_mark as removal_mark,
)
from scripts.lib.user_zone import user_zone  # noqa: E402

if TYPE_CHECKING:
    from scripts.disks import measure

VISIBLE_ROOT_ENV = "SCRATCH_VISIBLE_ROOT"
HOLDS_ENV = "SCRATCH_HOLDS"
UNTIL_FORMAT = "%Y-%m-%dT%H%MZ"
ROW_NAME = re.compile(r"[A-Za-z0-9_-]+-C[0-9]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
BUSY_REASON = "busy: a lease or .claude-busy lock is held"
RECORD_LOCKS: ContextVar[frozenset[tuple[int, int]]] = ContextVar("scratch_record_locks", default=frozenset())
RECORD_LOCK_FDS: ContextVar[frozenset[int]] = ContextVar("scratch_record_lock_fds", default=frozenset())
REMOVAL_VERSION = 1


class BusyMarkOwnership(Enum):
    """Use a private scope for one lock acquisition."""
    LOCAL = "local"


@dataclass
class BusyMarkScope:
    """Track the descriptors that this scope owns across related acquisitions."""
    held: dict[Path, int] = field(default_factory=dict)
    created: set[Path] = field(default_factory=set)


@dataclass
class _HeldRemovalLock:
    """Permit reentry only in the owning process and thread while the locks remain held."""
    path: Path
    process: int
    thread: int
    lease: FolderRemovalLease
    marks: BusyMarkScope
    active: bool = True


_REMOVAL_LOCKS: ContextVar[tuple[_HeldRemovalLock, ...]] = ContextVar("scratch_removal_locks", default=())
USE_STAMP = ".lint-sweep-used"
CACHE_TAG = "CACHEDIR.TAG"
CACHE_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
CARGO_TAG_LINE = "# This file is a cache directory tag created by cargo."
STYLE_TARGET_NAME = "style-sync-target"
STYLE_LOCK_NAME = "sync.lock"


@dataclass(frozen=True)
class StaleRemoval:
    """The deletion intent belongs to a previous copy at this path."""


@dataclass(frozen=True)
class Live:
    why: str


@dataclass(frozen=True)
class LandedRemoteCheckNeeded(Live):
    """The branch landed. Check the remote to verify the remaining work."""


@dataclass(frozen=True)
class Ended:
    evidence: str
    at: float = 0.0


@dataclass(frozen=True)
class Unknown:
    reason: str


def mark_cargo_cache(target: Path) -> None:
    """Tag a lasting Cargo cache and refresh its one use stamp."""
    from scripts.lib.atomic_file import write_text_atomic

    target.mkdir(parents=True, exist_ok=True)
    tag = target / CACHE_TAG
    if not tag.exists():
        write_text_atomic(tag, CACHE_TAG_SIGNATURE + "\n" + CARGO_TAG_LINE + "\n")
    (target / USE_STAMP).touch()


def atomic_record(path: Path, fields: object, *, sync: bool = False) -> None:
    # A killed writer may leave its stage file. Keep copy stages in Git metadata,
    # where they cannot look like unsaved user work on the next run.
    git = path.parent / ".git"
    staging = git if path.name == copy_record.RECORD and git.is_dir() and not git.is_symlink() else path.parent
    write_scratch_text(path, staging, json.dumps(fields) + "\n")
    if sync:
        from scripts.lib.atomic_file import sync_folder

        sync_folder(path.parent)


def write_scratch_text(path: Path, staging: Path, text: str) -> None:
    with coordinate_removals(wait_for_hold_lock=False) as removal:
        temporary = staging / f".{path.name}.{secrets.token_hex(8)}"
        removal.check_paths((path, temporary))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                _ = output.write(text)
                output.flush()
                os.fsync(output.fileno())
            removal.replace(temporary, path)
        finally:
            delete_entry(temporary, RemovalKind.FILE, missing_ok=True)


def exclude_copy_metadata(path: Path, *, additional: tuple[str, ...] = ()) -> None:
    """Exclude generated copy metadata from untracked files in Git status."""
    if not (path / ".git").is_dir() or (path / ".git").is_symlink():
        return
    exclude = path / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = exclude.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    patterns = [f"/{name}" for name in (copy_record.RECORD, BUSY, *additional) if f"/{name}" not in text.splitlines()]
    if patterns:
        separator = "\n" if text and not text.endswith("\n") else ""
        write_scratch_text(exclude, exclude.parent, text + separator + "\n".join(patterns) + "\n")


def register_copy(path: Path, *, task: str, owner_unit: str, source_path: str, source_commit: str,
                  copy_commit: str, target_mode: str, seed_source: str, borrows_from: str,
                  land_target: str = "", run_dir: str = "", phase_id: str = "", crate_run: str = "",
                  repository_identity: str = "") -> None:
    scratch_settings = conf.read("scratch")
    if copy_record.copy_folder(path) != path.resolve() or path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path}: a copy must be a folder at {copy_record.copies_root()}/<pair>/<copy>")
    def registered(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        fields: dict[str, object] = dict(owner_unit=owner_unit, source_path=source_path,
            source_commit=source_commit, copy_commit=copy_commit, target_mode=target_mode, seed_source=seed_source,
            borrows_from=borrows_from, repository_identity=repository_identity,
            maker=maker(), created=time.time(), renewed=time.time())
        if isinstance(record, copy_record.Registered):
            fields["created"] = record.fields.get("created", fields["created"])
        linked_run = record.fields.get("crate_run", "") if isinstance(record, copy_record.Registered) else ""
        if crate_run and linked_run and linked_run != crate_run:
            raise ScratchError(f"{path}: crate run link changed")
        if crate_run or linked_run:
            fields["crate_run"] = crate_run or linked_run
        return fields_for_task(fields, task, run_dir=run_dir, phase_id=phase_id, land_target=land_target)

    _ = update_record(path, registered, scratch_settings=scratch_settings)


@dataclass(frozen=True)
class GitDirectory:
    checkout: git_files.Checkout


@dataclass(frozen=True)
class NoGitDirectory:
    pass


@dataclass(frozen=True)
class UnreadableGitDirectory:
    reason: str


GitDirectoryState = GitDirectory | NoGitDirectory | git_files.MissingReferencedGitDirectory | UnreadableGitDirectory


def git_directory(repo: Path) -> GitDirectoryState:
    """Read the Git directory of a checkout without following a linked .git entry."""
    dot_git = repo / ".git"
    try:
        if not stat.S_ISDIR(repo.stat().st_mode):
            return NoGitDirectory()
        if dot_git.is_symlink():
            return UnreadableGitDirectory(f"{dot_git}: symbolic link")
        checkout = git_files.at(repo)
        if not checkout.git_dir.is_relative_to(checkout.toplevel):
            # Changes to external metadata do not change the files in the checkout.
            _ = git_files.head(checkout)
        else:
            git_files.check_internal_head(checkout)
        return GitDirectory(checkout)
    except (FileNotFoundError, NotADirectoryError, git_files.NotACheckout):
        return NoGitDirectory()
    except git_files.MissingReferencedGitDirectory as error:
        return error
    except (OSError, git_files.UnreadableGitFiles) as error:
        return UnreadableGitDirectory(str(error))


def busy_path(path: Path) -> Path:
    """Keep a checkout's busy mark in its Git directory."""
    git = git_directory(path)
    if isinstance(git, GitDirectory):
        return git.checkout.git_dir / BUSY
    if isinstance(git, UnreadableGitDirectory):
        raise OSError(git.reason)
    if isinstance(git, git_files.MissingReferencedGitDirectory):
        if not (removal_mark(path).is_file() or empty_lost_worktree(path, git)):
            raise OSError(f"{path}: cannot locate Git metadata for its busy lock")
    return path / BUSY


@contextmanager
def copy_lock(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[None]:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    folder = copy_record.copy_folder(path)
    if folder is None:
        record = task_of(path)
        if not isinstance(record, copy_record.Registered) or "owner_unit" not in record.fields:
            yield
            return
        folder = path
    mark = busy_path(folder)
    lock = open_busy_mark(mark, create=True)
    try:
        fcntl.flock(lock, fcntl.LOCK_SH)
        with copy_record_lock(folder, scratch_settings=scratch_settings):
            # A remover can unlink the mark while this holder waits on its old inode.
            # Recheck after the record lock too: deletion can finish while it waits.
            held = os.fstat(lock)
            try:
                present = mark.stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ScratchError(f"{folder}: copy was removed before its busy lock was acquired") from error
            if (held.st_dev, held.st_ino) != (present.st_dev, present.st_ino):
                raise ScratchError(f"{folder}: copy changed before its busy lock was acquired")
            pending = pending_removal(folder, scratch_settings=scratch_settings)
            if isinstance(pending, copy_record.UnreadableRecord):
                raise ScratchError(pending.reason)
            if isinstance(pending, copy_record.Registered):
                raise ScratchError(f"{folder}: removal has already started; finish cleanup first")
            require_no_removal_mark(folder.resolve().parent)
            exclude_copy_metadata(folder)
        yield
    finally:
        os.close(lock)


@contextmanager
def copy_record_lock(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[None]:
    """Serialize record changes and deletion on the existing directory, without creating a file."""
    with directory_record_lock(path, wait=True, scratch_settings=scratch_settings):
        yield


@contextmanager
def directory_record_lock(path: Path, *, wait: bool,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[bool]:
    """Lock the directory still at path; readers can decline to wait on another copy."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    attempts = scratch_settings.whole("record_lock_attempts")
    for attempt in range(attempts):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise ScratchError(f"{path}: copy was removed before its record lock was acquired") from error
        try:
            directory = os.fstat(descriptor)
            identity = (directory.st_dev, directory.st_ino)
            reentered = identity in RECORD_LOCKS.get()
            if not reentered:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                except BlockingIOError:
                    if wait:
                        raise
                    yield False
                    return
            # Waiting on a removed inode does not lock a new copy at the same path.
            # Close that descriptor before retrying, including reentrant acquisitions.
            locked = os.fstat(descriptor)
            try:
                present = path.lstat()
            except FileNotFoundError as error:
                raise ScratchError(f"{path}: copy was removed before its record lock was acquired") from error
            if (locked.st_dev, locked.st_ino) != (present.st_dev, present.st_ino):
                if attempt == attempts - 1:
                    raise ScratchError(f"{path}: copy kept changing while its record lock was acquired; retry")
                continue
            record_token = RECORD_LOCKS.set(RECORD_LOCKS.get() | {identity})
            fd_token = RECORD_LOCK_FDS.set(RECORD_LOCK_FDS.get() | {descriptor})
            try:
                yield True
            finally:
                RECORD_LOCK_FDS.reset(fd_token)
                RECORD_LOCKS.reset(record_token)
            return
        finally:
            os.close(descriptor)


def _held_removal_lock(path: Path) -> _HeldRemovalLock | None:
    return next((held for held in _REMOVAL_LOCKS.get()
                 if held.active and held.path == path.absolute()
                 and held.process == os.getpid() and held.thread == get_ident()), None)


@contextmanager
def removal_lock(path: Path) -> Generator[bool]:
    """Claim the lease locks and the busy mark, even when absent, and hold them through removal.
    A held lease on the folder or on any folder below it is busy, and the busy mark is then
    not created."""
    key = path.absolute()
    if _held_removal_lock(key) is not None:
        yield True
        return
    with folder_removal_lease(path, children=True, wait=not removal_mark(path).is_file()) as lease:
        free = not isinstance(lease, RefusedRemovalLease)
        marks = BusyMarkScope()
        with busy_locks(path, create=free, scope=marks) as idle:
            if not free or not idle:
                yield False
                return
            held = _HeldRemovalLock(key, os.getpid(), get_ident(), lease, marks)
            token = _REMOVAL_LOCKS.set((*_REMOVAL_LOCKS.get(), held))
            try:
                yield True
            finally:
                held.active = False
                _REMOVAL_LOCKS.reset(token)


@contextmanager
def judgment_lock(path: Path, *, scope: BusyMarkScope | BusyMarkOwnership = BusyMarkOwnership.LOCAL) -> Generator[bool]:
    """Check the folder's lease lock and existing busy marks without creating metadata during a scan or dry run.
    Leases below the folder are checked only at removal, so status reads do not walk the tree."""
    with collector_lease(path) as free, busy_locks(path, create=False, scope=scope) as idle:
        yield free and idle


@contextmanager
def busy_locks(path: Path, *, create: bool, inherited_fds: list[int] | None = None,
               wait: bool = False, scope: BusyMarkScope | BusyMarkOwnership = BusyMarkOwnership.LOCAL) -> Generator[bool]:
    """Hold busy marks; optionally export live descriptors or wait for their release.

    Exported descriptors belong to this context. A child can retain the same
    flock with subprocess pass_fds; callers must not close them themselves.
    """
    scope = BusyMarkScope() if scope is BusyMarkOwnership.LOCAL else scope
    held: dict[Path, int] = {}
    created: set[Path] = set()
    acquired = True
    try:
        try:
            folder = copy_record.copy_folder(path)
            owners = dict.fromkeys((path, path.parent, *((folder,) if folder is not None else ())))
            marks: dict[Path, bool] = {}
            for owner in owners:
                mark = busy_path(owner)
                marks[mark] = create and owner in (path, folder) and owner.is_dir() and not owner.is_symlink()
                # Locks left by older callers still protect their work. Never create them in a checkout.
                _ = marks.setdefault(owner / BUSY, False)
            for mark, creating in marks.items():
                if mark in scope.held:
                    continue
                if not creating and not os.path.lexists(mark):
                    continue
                try:
                    fd = open_busy_mark(mark, create=False)
                except FileNotFoundError:
                    if not creating:
                        continue
                    try:
                        fd = open_busy_mark(mark, create=True, exclusive=True)
                    except FileExistsError:
                        fd = open_busy_mark(mark, create=False)
                    else:
                        created.add(mark)
                held[mark] = fd
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError:
            acquired = False
        if acquired:
            scope.held.update(held)
            scope.created.update(created)
            if inherited_fds is not None:
                inherited_fds.extend(held.values())
        yield acquired
    finally:
        scope.created.difference_update(created)
        for mark, fd in held.items():
            _ = scope.held.pop(mark, None)
            os.close(fd)


def hand_over(path: Path, owner: str, task: str, *, run_dir: str = "", land_target: str = "",
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if not WORD.fullmatch(owner):
        raise ScratchError(f"{owner!r}: invalid owner unit")
    validate_phase_task(checked_task(task, False))

    def transferred(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered):
            raise ScratchError(f"{path}: no readable copy record")
        return fields_for_task({**record.fields, "owner_unit": owner}, task, run_dir=run_dir, land_target=land_target)

    with (locked_holds(scratch_settings=scratch_settings) as found,
          copy_lock(path, scratch_settings=scratch_settings),
          copy_record_lock(path, scratch_settings=scratch_settings)):
        current = task_of(path, scratch_settings=scratch_settings)
        held = found.get(str(path.resolve()))
        transfer_hold = held is not None and isinstance(current, copy_record.Registered) and (
            held["task"] == current.task or current.fields.get("test_hana_clone") is True)
        _ = update_record(path, transferred, ending_task=held["task"] if transfer_hold and held is not None else None,
                          scratch_settings=scratch_settings)
        if transfer_hold and held is not None:
            held.update(task=task, ended=0.0)
            write_holds(found, scratch_settings=scratch_settings)


def fields_for_task(fields: dict[str, object], task: str, *, run_dir: str = "", phase_id: str = "",
                    land_target: str = "") -> dict[str, object]:
    """Replace every field whose meaning is scoped to a task, in one place."""
    match = checked_task(task, False)
    result = {key: value for key, value in fields.items()
              if key not in ("task", "run_dir", "phase_id", "land_target", "ended", "ended_task")}
    result["task"] = task
    if match["kind"] == "phase":
        unit, separator, phase = match["name"].partition("/")
        if not separator or not unit or not phase or phase_id and phase_id != phase:
            raise ScratchError(f"{task!r}: phase_id must match the task's phase")
        result.update(run_dir=run_dir, phase_id=phase)
    if match["kind"] == "landed" or land_target:
        # Registration may record a future land target before the task is a landed rule.
        result["land_target"] = land_target or "origin/main"
    return result


def update_record(path: Path, change: Callable[[copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord], dict[str, object]],
                  *, removal: bool = False, ending_task: str | None = None,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered:
    """The only record-write path: read, change and publish under the directory lock."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    with copy_record_lock(path, scratch_settings=scratch_settings):
        pending = pending_removal(path, scratch_settings=scratch_settings) if copy_record.copy_folder(path) == path.resolve() else copy_record.NotRegistered()
        if isinstance(pending, copy_record.UnreadableRecord):
            raise ScratchError(pending.reason)
        if isinstance(pending, copy_record.Registered) and not removal:
            raise ScratchError(f"{path}: removal has already started; finish cleanup first")
        record = task_of(path, scratch_settings=scratch_settings)
        fields = change(record)
        task = fields.get("task")
        if not isinstance(task, str):
            raise ScratchError(f"{path}: record needs a task")
        _ = checked_task(task, False)
        registered_copy = copy_record.copy_folder(path) == path.resolve() or "owner_unit" in fields
        if registered_copy:
            directory = path.stat()
            identity = record.fields.get("copy_id") if isinstance(record, copy_record.Registered) else ""
            fields.update(copy_id=identity if isinstance(identity, str) and identity else secrets.token_hex(16),
                          copy_device=directory.st_dev, copy_inode=directory.st_ino)
            if removal:
                fields["copy_path"] = str(path.resolve())
        destination = removal_mark(path) if removal else path / copy_record.RECORD
        owner = record.task if isinstance(record, copy_record.Registered) else task
        with coordinate_removals(scratch_settings=scratch_settings, wait_for_hold_lock=False,
                                 ending_task=ending_task if ending_task is not None else owner):
            if registered_copy and not removal:
                exclude_copy_metadata(path)
            atomic_record(destination, fields)
        return copy_record.Registered(task, fields)


class ScratchError(Exception):
    pass


class ScratchHoldsUnreadable(ScratchError):
    """The hold file cannot confirm which folders are protected."""


@dataclass(frozen=True)
class Folder:
    path: Path
    task: str
    changed: float


class Hold(TypedDict):
    task: str
    maker: str
    reason: str
    made: float
    ended: float


@dataclass(frozen=True)
class FilesystemLocation:
    device: str
    path: PurePath

    @classmethod
    def at(cls, path: Path, mounts: Sequence[measure.Mount]) -> FilesystemLocation:
        mount = next((mount for mount in mounts if path.is_relative_to(mount.point)), None)
        if mount is None:
            raise ScratchHoldsUnreadable(f"cannot identify the mount for {path}")
        return cls(mount.device, mount.root / path.relative_to(mount.point))

    def overlaps(self, other: FilesystemLocation) -> bool:
        return self.device == other.device and (
            self.path.is_relative_to(other.path) or other.path.is_relative_to(self.path))


@dataclass(frozen=True)
class HeldScratchFolders:
    """Keep each held folder, its contents, and any folder that contains it."""

    held: frozenset[tuple[int, int]]
    containers: frozenset[tuple[int, int]]
    locations: tuple[FilesystemLocation, ...]
    mounts: tuple[measure.Mount, ...]

    @staticmethod
    def identity(path: Path, *, follow_symlinks: bool = True) -> Collection[tuple[int, int]]:
        try:
            info = path.stat(follow_symlinks=follow_symlinks)
        except (FileNotFoundError, NotADirectoryError):
            return ()
        except OSError as error:
            raise ScratchHoldsUnreadable(f"cannot check scratch holds at {path}: {error}") from error
        return ((info.st_dev, info.st_ino),)

    @classmethod
    def from_holds(cls, holds: Mapping[str, Hold], *, ending_task: str | None = None,
                   mounts: Sequence[measure.Mount] | None = None) -> HeldScratchFolders:
        held: set[tuple[int, int]] = set()
        parents: set[Path] = set()
        paths: list[Path] = []
        for name, record in holds.items():
            if record["task"] == ending_task:
                continue
            path = Path(name)
            identities = cls.identity(path, follow_symlinks=False)
            if identities:
                held.update(identities)
                parents.update(path.parents)
                paths.append(path)
        visible: tuple[measure.Mount, ...] = ()
        if paths and sys.platform == "linux":
            from scripts.disks import measure

            entries = measure.read_mounts() if mounts is None else mounts
            if not entries:
                raise ScratchHoldsUnreadable("cannot read the mount table for scratch holds")
            # Prefer the deepest mount. The last mount at a point is visible.
            visible = tuple(sorted(reversed(entries), key=lambda mount: len(mount.point.parts), reverse=True))
        locations = tuple(FilesystemLocation.at(path, visible) for path in paths) if visible else ()
        return cls(frozenset(held), frozenset(key for path in parents for key in cls.identity(path)), locations, visible)

    def protects_location(self, path: Path) -> bool:
        if not self.locations:
            return False
        location = FilesystemLocation.at(path, self.mounts)
        return any(held.overlaps(location) for held in self.locations)

    def protects(self, path: Path) -> bool:
        if not self.held:
            return False
        identities = self.identity(path, follow_symlinks=False)
        return (self.protects_location(path) or not self.held.isdisjoint(identities) or not self.containers.isdisjoint(identities)
                or any(not self.held.isdisjoint(self.identity(parent)) for parent in path.parents))


HELD_FOLDER_REASON = "a scratch hold keeps this folder"


class RemovalRefused(OSError):
    """A scratch hold can prevent removal."""


class MountRemovalRefused(RemovalRefused):
    """A mount point prevents tree removal."""


def mount_identity(fd: int) -> int:
    """Identify a mount even when a bind mount has the same device."""
    if sys.platform == "linux":
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            key, _, value = line.partition(":")
            if key == "mnt_id":
                return int(value)
        raise MountRemovalRefused("cannot read the directory mount identity")
    return os.fstat(fd).st_dev


class RemovalKind(Enum):
    FILE = "file"
    EMPTY_DIRECTORY = "empty directory"
    TREE = "tree"


@dataclass(frozen=True)
class AnchoredRemoval:
    """Name an entry relative to its open parent directory."""

    parent: int
    name: str

    def __post_init__(self) -> None:
        if self.name in ("", ".", "..") or "/" in self.name:
            raise ValueError(f"{self.name!r}: expected one directory entry name")


def resolved_removal_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Include each path and its resolved target."""
    return tuple(dict.fromkeys(candidate for path in paths
                               for candidate in (path.absolute(), path.resolve())))


_TREE_ROOT = Path()


class TreeRemovalProof(Protocol):
    """Verify a source entry against its published copy before deletion."""

    def check(self, source: int, path: Path, info: os.stat_result, mount: int) -> None: ...
    def check_parent_metadata(self, fd: int, path: Path) -> None: ...


@dataclass(frozen=True)
class CoordinatedRemoval:
    """Check and remove entries while the scratch hold lock is held."""

    holds: Mapping[str, Hold]
    folders: HeldScratchFolders
    ending_task: str | None

    def check_paths(self, paths: Iterable[Path]) -> None:
        """Check all paths under the caller's hold lock."""
        if self.folders.held:
            self.check_held_paths(resolved_removal_paths(paths))

    def check_held_paths(self, paths: tuple[Path, ...]) -> None:
        if any(self.folders.protects(path) for path in paths):
            raise RemovalRefused(HELD_FOLDER_REASON)

    def check(self, entry: Path | AnchoredRemoval) -> None:
        if isinstance(entry, Path):
            self.check_paths((entry,))
        elif self.protects_anchored(entry):
            raise RemovalRefused(HELD_FOLDER_REASON)

    @staticmethod
    def check_tree(path: Path, *, mounts: Sequence[measure.Mount] | None = None) -> None:
        """Refuse a tree with a mount point before removing any entry."""
        if not path.is_symlink() and path.is_dir():
            CoordinatedRemoval.check_mounts(resolved_removal_paths((path,)), mounts=mounts)

    @staticmethod
    def check_mounts(paths: tuple[Path, ...], *, mounts: Sequence[measure.Mount] | None = None) -> None:
        if not paths:
            return
        if sys.platform == "linux":
            from scripts.disks import measure

            entries = measure.read_mounts() if mounts is None else mounts
            if not entries:
                raise MountRemovalRefused(f"{paths[0]}: cannot read the mount table")
            for mount in entries:
                if any(mount.point.is_relative_to(path) for path in paths):
                    raise MountRemovalRefused(f"tree contains a mount point at {mount.point}")
            return

        def unreadable(error: OSError) -> None:
            raise MountRemovalRefused(f"cannot check mount points: {error}") from error

        for path in paths:
            if os.path.ismount(path):
                raise MountRemovalRefused(f"tree contains a mount point at {path}")
            if path.is_symlink() or not path.is_dir():
                continue
            for folder, children, _ in os.walk(path, onerror=unreadable):
                for name in children:
                    candidate = Path(folder) / name
                    if os.path.ismount(candidate):
                        raise MountRemovalRefused(f"tree contains a mount point at {candidate}")

    def check_worktree_prune(self, git_dirs: Iterable[Path], *, records: Iterable[Path] = ()) -> None:
        """Check all paths that a prune can change."""
        sources = tuple(git_dirs)
        self.check_prune_paths((*records, *sources, *(source / "worktrees" for source in sources)))

    def check_prune_paths(self, paths: Iterable[Path]) -> None:
        checked = resolved_removal_paths(paths)
        self.check_held_paths(checked)
        self.check_mounts(checked)

    @contextmanager
    def open_worktree_registrations(self, common: Path) -> Generator[tuple[Path, int | None]]:
        given = common / "worktrees"
        worktrees = given.resolve()
        self.check_prune_paths((common, given, worktrees))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as opened:
            try:
                parent = os.open(worktrees.parent, flags)
                _ = opened.callback(os.close, parent)
                fd = os.open(worktrees.name, flags, dir_fd=parent)
            except FileNotFoundError:
                yield worktrees, None
                return
            _ = opened.callback(os.close, fd)
            if mount_identity(fd) != mount_identity(parent):
                raise MountRemovalRefused(f"tree contains a mount point at {worktrees}")
            self.check(AnchoredRemoval(parent, worktrees.name))
            yield worktrees, fd

    def remove_worktree_registration(self, registration: WorktreeRegistration) -> bool:
        """Resume removal of the recorded Git registration."""
        metadata = registration.path
        with self.open_worktree_registrations(registration.common) as (worktrees, fd):
            if fd is None:
                return True
            if metadata.parent != worktrees:
                raise RemovalRefused(f"{metadata}: worktree registration parent changed")
            try:
                info = os.stat(metadata.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if directory_id(info) != registration.identity:
                raise RemovalRefused(f"{metadata}: worktree registration changed")
            self.delete_at(AnchoredRemoval(fd, metadata.name), expected=info, repair_permissions=True, sync=True)
        return True

    def prune_worktrees(self, common: Path) -> None:
        """Ask Git which registrations to prune. Delete them through the coordinator."""
        command = ["git", "--git-dir", str(common), "worktree", "prune", "--dry-run"]
        with self.open_worktree_registrations(common) as (worktrees, fd):
            if fd is None:
                _ = subprocess.run(command, capture_output=True, check=True, timeout=60)
                return
            listed = {name: os.stat(name, dir_fd=fd, follow_symlinks=False) for name in os.listdir(fd)}
            if any("\n" in name for name in listed):
                raise RemovalRefused(f"{worktrees}: Git cannot report these names without ambiguity")
            report = subprocess.run(command, capture_output=True, check=True, timeout=60,
                                    env={**os.environ, "LC_ALL": "C"})
            if not os.path.samestat(os.fstat(fd), (common / "worktrees").stat()):
                raise RemovalRefused(f"{worktrees}: directory changed during prune")
            selected: list[str] = []
            for line in os.fsdecode(report.stdout + report.stderr).split("\n"):
                if not line:
                    continue
                matches = [name for name in listed if line.startswith(f"Removing worktrees/{name}: ")]
                if len(matches) != 1:
                    raise RemovalRefused(f"{worktrees}: cannot identify the registration from Git")
                selected.append(matches[0])
            for name in dict.fromkeys(selected):
                self.delete_at(AnchoredRemoval(fd, name), expected=listed[name], repair_permissions=True)

    def protects_anchored(self, entry: AnchoredRemoval) -> bool:
        if not self.folders.held:
            return False
        try:
            info = os.stat(entry.name, dir_fd=entry.parent, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if self.folders.locations:
            parent = Path(os.readlink(f"/proc/self/fd/{entry.parent}"))
            if not parent.is_absolute() or not os.path.samestat(parent.stat(), os.fstat(entry.parent)):
                raise ScratchHoldsUnreadable(f"cannot identify the directory for {entry.name}")
            if self.folders.protects_location(parent / entry.name):
                return True
        if info is not None:
            identity = (info.st_dev, info.st_ino)
            if identity in self.folders.held or identity in self.folders.containers:
                return True
        with ExitStack() as parents:
            current = entry.parent
            while True:
                info = os.fstat(current)
                if (info.st_dev, info.st_ino) in self.folders.held:
                    return True
                parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
                _ = parents.callback(os.close, parent)
                if os.path.samestat(info, os.fstat(parent)):
                    return False
                current = parent

    def delete(self, entry: Path | AnchoredRemoval, kind: RemovalKind, *, repair_permissions: bool = True,
               record_last: bool = False) -> None:
        self.check(entry)
        path, parent = (entry, None) if isinstance(entry, Path) else (entry.name, entry.parent)
        if kind is RemovalKind.EMPTY_DIRECTORY:
            os.rmdir(path, dir_fd=parent)
        elif kind is RemovalKind.FILE:
            os.unlink(path, dir_fd=parent)
        elif kind is RemovalKind.TREE:
            if isinstance(entry, AnchoredRemoval):
                self.delete_at(entry, repair_permissions=repair_permissions, record_last=record_last)
                return
            directory = os.open(entry.parent.resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self.check_tree(entry)
                self.delete_at(AnchoredRemoval(directory, entry.name),
                               repair_permissions=repair_permissions, record_last=record_last)
            finally:
                os.close(directory)

    def delete_at(self, entry: AnchoredRemoval, *, repair_permissions: bool, record_last: bool = False,
                  expected: os.stat_result | None = None, proof: TreeRemovalProof | None = None,
                  sync: bool = False) -> None:
        self.check(entry)
        info = os.stat(entry.name, dir_fd=entry.parent, follow_symlinks=False)
        if expected is not None and not os.path.samestat(info, expected):
            raise RemovalRefused(f"{entry.name}: entry changed before deletion")
        if stat.S_ISDIR(info.st_mode):
            self.delete_directory(entry, mount_identity(entry.parent), info, repair_permissions,
                                  record_last=record_last, proof=proof, sync=sync)
        else:
            if proof is not None:
                raise RemovalRefused(f"{entry.name}: directory changed before deletion")
            self.delete(entry, RemovalKind.FILE)

    def delete_directory(self, entry: AnchoredRemoval, mount: int, expected: os.stat_result,
                         repair_permissions: bool, *, record_last: bool = False,
                         proof: TreeRemovalProof | None = None, sync: bool = False,
                         relative: Path = _TREE_ROOT) -> None:
        """Open each directory without following links. Check its mount before descent."""
        parent, name = entry.parent, entry.name
        display = relative if relative.parts else Path(name)
        flags = os.O_DIRECTORY | os.O_NOFOLLOW
        if proof is not None and sys.platform == "linux":
            flags |= os.O_NOATIME
        with ExitStack() as opened:
            try:
                fd = os.open(name, flags | os.O_RDONLY, dir_fd=parent)
            except PermissionError:
                if not repair_permissions or sys.platform != "linux":
                    raise
                pinned = os.open(name, flags | os.O_PATH, dir_fd=parent)
                _ = opened.callback(os.close, pinned)
                if mount_identity(pinned) != mount:
                    raise MountRemovalRefused(f"tree contains a mount point at {display}") from None
                if not os.path.samestat(os.fstat(pinned), expected):
                    raise RemovalRefused(f"{display}: directory changed before deletion") from None
                os.chmod(f"/proc/self/fd/{pinned}", 0o700)
                fd = os.open(".", os.O_DIRECTORY | os.O_RDONLY, dir_fd=pinned)
            _ = opened.callback(os.close, fd)
            if mount_identity(fd) != mount:
                raise MountRemovalRefused(f"tree contains a mount point at {display}")
            if not os.path.samestat(os.fstat(fd), expected):
                raise RemovalRefused(f"{display}: directory changed before deletion")
            proof_parent = parent if relative.parts else fd
            if proof is not None:
                proof.check(proof_parent, relative, os.fstat(fd), mount)
            if repair_permissions and not os.access(".", os.W_OK | os.X_OK, dir_fd=fd):
                os.fchmod(fd, 0o700)
            children = os.listdir(fd)
            if record_last or proof is not None:
                children.sort(key=lambda child: (record_last and child == copy_record.RECORD, child))
            for child in children:
                path = relative / child
                try:
                    info = os.stat(child, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        self.delete_directory(AnchoredRemoval(fd, child), mount, info, repair_permissions,
                                              proof=proof, sync=sync, relative=path)
                    else:
                        if proof is not None:
                            proof.check(fd, path, info, mount)
                            proof.check_parent_metadata(fd, relative)
                        os.unlink(child, dir_fd=fd)
                except FileNotFoundError:
                    if proof is not None:
                        raise
            if proof is not None:
                proof.check(proof_parent, relative, os.fstat(fd), mount)
                if relative.parts:
                    proof.check_parent_metadata(parent, relative.parent)
            if not os.path.samestat(os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise RemovalRefused(f"{display}: directory changed during removal")
            os.rmdir(name, dir_fd=parent)
            if sync:
                os.fsync(parent)

    def replace(self, source: Path | AnchoredRemoval, target: Path | AnchoredRemoval) -> None:
        """Check both entries before replacing a file."""
        self.check(source)
        self.check(target)
        if isinstance(source, Path) and isinstance(target, Path):
            os.replace(source, target)
            return
        source_path, source_parent = (source, None) if isinstance(source, Path) else (source.name, source.parent)
        target_path, target_parent = (target, None) if isinstance(target, Path) else (target.name, target.parent)
        os.replace(source_path, target_path, src_dir_fd=source_parent, dst_dir_fd=target_parent)

    def rename_new(self, source: AnchoredRemoval, target: AnchoredRemoval) -> None:
        """Rename an entry under the hold lock and refuse an existing destination."""
        self.check(source)
        self.check(target)
        library = ctypes.CDLL(None, use_errno=True)
        try:
            if sys.platform == "darwin":
                rename = cast(Callable[[int, bytes, int, bytes, int], int], library.renameatx_np)
                flag = 4  # RENAME_EXCL
            elif sys.platform == "linux":
                rename = cast(Callable[[int, bytes, int, bytes, int], int], library.renameat2)
                flag = 1  # RENAME_NOREPLACE
            else:
                raise RemovalRefused("this platform does not support an exclusive rename")
        except AttributeError as error:
            raise RemovalRefused("this platform does not support an exclusive rename") from error
        if rename(source.parent, os.fsencode(source.name), target.parent, os.fsencode(target.name), flag) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), source.name, None, target.name)


_ACTIVE_REMOVAL: ContextVar[CoordinatedRemoval | None] = ContextVar("scratch_coordinated_removal", default=None)


@dataclass(frozen=True)
class _LockedHoldFile:
    path: Path
    records: dict[str, Hold]
    settings: conf.ConfigRead


_LOCKED_HOLD_FILE: ContextVar[_LockedHoldFile | None] = ContextVar("scratch_locked_hold_file", default=None)


@contextmanager
def coordinate_removals(*, dry_run: bool = False, wait_for_hold_lock: bool = True, ending_task: str | None = None,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[CoordinatedRemoval]:
    """Use the scratch hold lock from the first hold check through deletion."""
    active = _ACTIVE_REMOVAL.get()
    if active is not None and (ending_task is None or ending_task == active.ending_task):
        yield active
        return
    with ExitStack() as locks:
        locked = _LOCKED_HOLD_FILE.get()
        if isinstance(scratch_settings, UseConfiguredValue) and locked is not None:
            ledger = locked.path
        else:
            if isinstance(scratch_settings, UseConfiguredValue):
                scratch_settings = conf.read("scratch")
            ledger = holds_file(scratch_settings=scratch_settings).absolute()
        sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
        sweep.refuse_live_test_roots((ledger,))
        if locked is not None and locked.path == ledger:
            holds = locked.records
        else:
            holds = (read_holds(scratch_settings=scratch_settings) if dry_run else locks.enter_context(
                locked_holds(scratch_settings=scratch_settings, wait=wait_for_hold_lock)))
        removal = CoordinatedRemoval(holds, HeldScratchFolders.from_holds(holds, ending_task=ending_task), ending_task)
        if dry_run:
            yield removal
            return
        token = _ACTIVE_REMOVAL.set(removal)
        try:
            yield removal
        finally:
            _ACTIVE_REMOVAL.reset(token)


def delete_entry(entry: Path | AnchoredRemoval, kind: RemovalKind, *, missing_ok: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """Delete an entry through the shared hold check."""
    if isinstance(entry, Path):
        sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
        sweep.refuse_live_test_roots((entry,))
        if missing_ok and not os.path.lexists(entry):
            return
    try:
        with coordinate_removals(scratch_settings=scratch_settings) as removal:
            removal.delete(entry, kind)
    except FileNotFoundError:
        if not missing_ok:
            raise


def replace_entry(source: AnchoredRemoval, target: AnchoredRemoval) -> None:
    with coordinate_removals(wait_for_hold_lock=False) as removal:
        removal.replace(source, target)


def visible_root(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return Path(os.environ.get(VISIBLE_ROOT_ENV) or scratch_settings.text("visible_root")).expanduser()


def random_suffix(scratch_settings: conf.ConfigRead) -> str:
    length = scratch_settings.whole("suffix_length")
    return secrets.token_hex(length)[:length]


def until_task(moment: float, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    stamp = datetime.fromtimestamp(moment, timezone.utc).strftime(UNTIL_FORMAT)
    return f"until:{stamp}/{random_suffix(scratch_settings)}"


def until_stamp(name: str) -> str:
    return name.partition("/")[0]


def parsed_until(text: str) -> str:
    """The until: task for `YYYY-MM-DD` (its end) or `YYYY-MM-DD HH:MM`, in the user's zone."""
    for form, day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            moment = datetime.strptime(text.strip(), form).replace(tzinfo=user_zone())
        except ValueError:
            continue
        return until_task(moment.timestamp() + (24 * 3600 if day else 0))
    raise ScratchError(f"{text!r}: --until takes YYYY-MM-DD or YYYY-MM-DD HH:MM")


def task_end(task: str, now: float | None = None) -> Live | Ended | Unknown:
    """End evidence for a deadline, or the reason its external end is still needed."""
    match = copy_record.TASK.fullmatch(task)
    if match is None:
        return Unknown(f"invalid task: {task}")
    if match["kind"] != "until":
        return Live(f"task {task} has not ended")
    try:
        at = datetime.strptime(until_stamp(match["name"]), UNTIL_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return Unknown(f"invalid task deadline: {task}")
    return Ended(f"deadline {task} passed", at) if at <= (time.time() if now is None else now) else Live(f"task {task} has not ended")


def ended(task: str, now: float) -> bool:
    return isinstance(task_end(task, now), Ended)


def checked_task(task: str, visible: bool) -> re.Match[str]:
    match = copy_record.TASK.fullmatch(task)
    if match is None:
        raise ScratchError(f"{task!r} is no task: use run:<id>, phase:<unit>/<phase>, gate:<id>, "
                           + "landed:<unit>/<branch>, row:<showrunner>-C<n> or --until <when>")
    if match["kind"] == "row" and not ROW_NAME.fullmatch(match["name"]):
        raise ScratchError(f"{task!r}: a row task is row:<showrunner>-C<n>")
    if match["kind"] == "until" and isinstance(task_end(task), Unknown):
        raise ScratchError(f"{task!r}: an until task is until:YYYY-MM-DDTHHMMZ/<id>; use --until")
    if visible and match["kind"] != "row":
        raise ScratchError("--visible is for a change review row's repro only")
    return match


def registered_plan(unit: str) -> Path:
    """Resolve the plan from the unit's staff parent and production record."""
    from scripts.direct import phase_table
    from scripts.production import proposals_table, staff, unit_lookup
    from scripts.production.production_doc import Refusal

    try:
        parent = unit_lookup.unit_parent(unit, record=staff.snapshot())
        value = cast(object, json.loads(phase_table.show_production(Path(parent.doc))))
    except (OSError, UnicodeError, ValueError, Refusal) as error:
        raise ScratchError(f"{unit}: cannot resolve registered plan: {error}") from error
    if not isinstance(value, dict):
        raise ScratchError(f"{unit}: production plan records are not an object")
    records = cast(dict[str, object], value)
    plans = proposals_table.unit_plan_paths(records)
    if unit not in plans:
        record = records.get(unit)
        cause = cast(dict[str, object], record).get("unavailable") if isinstance(record, dict) else None
        raise ScratchError(f"{unit}: no registered plan in production {parent.doc}: "
                           + (str(cause) if cause else "no staff row or plan"))
    return plans[unit]


def validate_phase_task(match: re.Match[str]) -> None:
    """Validate a new phase assignment, without restricting legacy task cleanup."""
    if match["kind"] != "phase":
        return
    from scripts.direct import plan_history

    unit, separator, phase = match["name"].partition("/")
    hint = "use a phase id from the unit's plan, gate:<id> or --until <when>"
    if not separator or not phase:
        raise ScratchError(f"{match[0]!r}: a phase task needs phase:<unit>/<phase>; {hint}")
    try:
        plan = registered_plan(unit)
    except ScratchError as error:
        raise ScratchError(f"{match[0]!r}: {error}; {hint}") from error
    try:
        phases = plan_history.plan_phases(plan)
    except (OSError, UnicodeError) as error:
        raise ScratchError(f"{unit}: cannot read registered plan {plan}: {error}; {hint}") from error
    if not any(row["id"] == phase for row in phases):
        raise ScratchError(f"{match[0]!r}: phase {phase!r} is not in registered plan {plan}; {hint}")


def target(task: str, word: str, visible: bool, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """The new folder's path for the task; never one that exists."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    match = checked_task(task, visible)
    validate_phase_task(match)
    if word and not WORD.fullmatch(word):
        raise ScratchError(f"{word!r}: a name is letters, digits, - and _")
    if visible:
        path = visible_root(scratch_settings=scratch_settings) / "-".join(part for part in (match["name"], word) if part)
        if os.path.lexists(path):
            raise ScratchError(f"{path} exists")
        return path
    # The record keeps an until: task's time, so its folder name holds only the word.
    slug = "" if match["kind"] == "until" else f"{match['kind']}-{match['name'].replace('/', '-')}"
    folder = session_folder.caller_folder()
    if folder is not None:
        return free_name(folder, word or slug or "scratch")
    suffix = random_suffix(scratch_settings)
    purpose = "-".join(part for part in (slug, word) if part) or "scratch"
    return build_folder_path(BuildFolderSpec(kind="scratch", parent=copy_record.root(), suffix_length=len(suffix),
                                            purpose=purpose, suffix=suffix))


def free_name(folder: Path, name: str) -> Path:
    """The first of `name`, `name-2`, `name-3` ... that is free in the session folder; `run` is never free."""
    number = 1
    while True:
        path = folder / (name if number == 1 else f"{name}-{number}")
        if path.name != session_folder.RUN and not os.path.lexists(path):
            return path
        number += 1


def in_session_folder(path: Path) -> bool:
    return path.parent.parent in session_folder.configured_roots()


def held_in_session(path: Path, task: str) -> bool:
    """Whether a new folder is in a session folder and has a task that ends it there."""
    return in_session_folder(path) and checked_task(task, False)["kind"] != "until"


def index_in_session(path: Path, task: str, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """Hold a session folder's task folder, so `end` finds it without a scan."""
    if held_in_session(path, task):
        _ = hold(path, task, "scratch folder in the session folder", scratch_settings=scratch_settings)


def maker() -> str:
    from scripts.production import staff
    current = staff.runtime_id()
    try:
        row = staff.by_current_session(current) if current else None
    except staff.StaffAbsent:
        return ""
    return row.id if row is not None else ""


def task_or_default(task: str, now: float, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return task or until_task(now + scratch_settings.whole("default_life_seconds"), scratch_settings=scratch_settings)


def write_record(path: Path, task: str, now: float, *,
    throwaway: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    _ = update_record(path, lambda record: fields_for_task(
        {**(record.fields if isinstance(record, copy_record.Registered) else {}),
         "maker": maker(), "created": now, "renewed": now, "throwaway": throwaway}, task), scratch_settings=scratch_settings)


def new(task: str = "", word: str = "", visible: bool = False, now: float | None = None, *,
    throwaway: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    while True:
        path = target(task, word, visible, scratch_settings=scratch_settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()
            break
        except FileExistsError:
            # Another call took the free name in the session folder first; take the next one.
            if not in_session_folder(path):
                raise
    try:
        write_record(path, task, moment, throwaway=throwaway, scratch_settings=scratch_settings)
        index_in_session(path, task, scratch_settings=scratch_settings)
    except BaseException:
        with suppress(OSError, ScratchHoldsUnreadable):
            delete_entry(path, RemovalKind.TREE, missing_ok=True, scratch_settings=scratch_settings)
        raise
    return path


def worker_task(staff_id: str) -> str:
    """Name the task that owns one worker's assignment folders."""
    return f"gate:worker-{staff_id}"


def new_worker(word: str = "", *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """Create a private folder for the caller's worker assignment."""
    from scripts.production import staff

    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    record = staff.snapshot()
    worker = record.by_current_session(staff.runtime_id())
    if worker is None or worker.kind != "worker" or not isinstance(worker.parent, staff.ReportsTo):
        raise ScratchError("--worker requires the caller's worker staff row")
    unit = record.by_id(worker.parent.id)
    if unit is None or unit.kind != "unit" or not isinstance(unit.parent, staff.ReportsTo):
        raise ScratchError("--worker requires the worker's unit staff row")
    showrunner = record.by_id(unit.parent.id)
    if showrunner is None or showrunner.kind != "showrunner":
        raise ScratchError("--worker requires the unit's showrunner staff row")
    parts = (showrunner.name, unit.name, worker.name)
    if any(not WORD.fullmatch(part) for part in (*parts, *([word] if word else []))):
        raise ScratchError("--worker names must contain letters, digits, - and _")
    parent = inherited_temporary_parent().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / "-".join((*parts, random_suffix(scratch_settings), *([word] if word else [])))
    path.mkdir()
    try:
        task = worker_task(worker.id)
        write_record(path, task, time.time(), throwaway=True, scratch_settings=scratch_settings)
        _ = hold(path, task, "worker assignment folder", scratch_settings=scratch_settings)
    except BaseException:
        with suppress(OSError, ScratchHoldsUnreadable):
            delete_entry(path, RemovalKind.TREE, missing_ok=True, scratch_settings=scratch_settings)
        raise
    return path


def adopt(source: Path, task: str, word: str = "", visible: bool = False, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """Rename a loose folder or copy it before removal across filesystems."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    sweep.refuse_live_test_roots((source,))
    source = source.parent.resolve() / source.name
    original_operation = recover_folder_operations((source,), scratch_settings=scratch_settings).get(source)
    if original_operation is not None:
        return original_operation
    try:
        original = source.lstat()
    except FileNotFoundError as error:
        raise ScratchError(f"{source} is no folder") from error
    if not stat.S_ISDIR(original.st_mode):
        raise ScratchError(f"{source} is no folder")
    source = source.resolve()
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    path = target(task, word, visible, scratch_settings=scratch_settings)
    sweep.refuse_live_test_roots((path,))
    destination = path.resolve()
    if destination.is_relative_to(source):
        raise ScratchError(f"{path}: destination is inside {source}")
    mounts: tuple[measure.Mount, ...] | None = None
    if sys.platform == "linux":
        from scripts.disks import measure

        mounts = tuple(sorted(reversed(measure.read_mounts()), key=lambda mount: len(mount.point.parts), reverse=True))
        if FilesystemLocation.at(source, mounts).overlaps(FilesystemLocation.at(destination, mounts)):
            raise ScratchError(f"{path}: destination overlaps {source}")
    with coordinate_removals(scratch_settings=scratch_settings) as removal:
        _ = recover_folder_operations((path,), scratch_settings=scratch_settings)
        removal.check_paths((source, path))
        removal.check_tree(source, mounts=mounts)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not rename_adoption(removal, source, path, original, task, moment, scratch_settings):
            copy_adoption(removal, source, path, original, task, moment, scratch_settings)
    return path


def copy_adoption(removal: CoordinatedRemoval, source: Path, path: Path, original: os.stat_result,
                  task: str, now: float, scratch_settings: conf.ConfigRead) -> None:
    """Hide and check the source before copying it to a staging folder."""
    nonce = secrets.token_hex(16)
    hidden = source.with_name(f".scratch-operation-{nonce}")
    stage = path.with_name(f".scratch-operation-{nonce}")
    operation = FolderOperation(OperationStep.COPY, source, directory_id(original), hidden=hidden,
                                destination=path, stage=stage, task=task, made=now)
    journal = operation_journal(source, scratch_settings)
    journal.save(operation)
    try:
        rename_operation_entry(removal, source, hidden, operation.source_id)
        require_quiescent(hidden)
        before = adoption_stamps(hidden)
        stage.mkdir(mode=0o700)
        operation = replace(operation, stage_id=directory_id(stage.lstat()))
        journal.save(operation)
        _ = shutil.copytree(hidden, stage, symlinks=True, dirs_exist_ok=True)
        write_record(stage, task, now, scratch_settings=scratch_settings)
        if adoption_stamps(hidden) != before:
            raise RemovalRefused(f"{source}: source changed during the copy")
        require_quiescent(hidden)
        operation = replace(operation, step=OperationStep.PUBLISH)
        journal.save(operation)
        rename_operation_entry(removal, stage, path, operation.stage_id)
        _ = recover_folder_operation(removal, journal, operation, scratch_settings)
    except BaseException:
        if operation.step is OperationStep.COPY:
            _ = recover_folder_operation(removal, journal, operation, scratch_settings)
        raise


def rename_adoption(removal: CoordinatedRemoval, source: Path, path: Path, original: os.stat_result,
                    task: str, now: float, scratch_settings: conf.ConfigRead) -> bool:
    """Rename and record an adoption. Report when the rename needs a copy."""
    operation = FolderOperation(OperationStep.RENAME, source, directory_id(original), destination=path,
                                task=task, made=now)
    journal = operation_journal(source, scratch_settings)
    journal.save(operation)
    try:
        rename_operation_entry(removal, source, path, operation.source_id)
    except OSError as error:
        if error.errno == errno.EXDEV:
            journal.clear(removal)
            return False
        raise
    _ = recover_folder_operation(removal, journal, operation, scratch_settings)
    return True


def adoption_stamps(source: Path) -> dict[Path, tuple[int, int, int]]:
    """Detect changed entries before removing the source of an adoption."""
    info = source.lstat()
    stamps = {path.relative_to(source): (entry.status.st_dev, entry.status.st_ino, entry.status.st_ctime_ns)
              for path, entry in removal_entries(source).items()}
    stamps[_TREE_ROOT] = (info.st_mode, info.st_uid, info.st_mtime_ns)
    return stamps


type DirectoryId = tuple[int, int]


def directory_id(info: os.stat_result) -> DirectoryId:
    if not stat.S_ISDIR(info.st_mode):
        raise RemovalRefused("expected a directory")
    return info.st_dev, info.st_ino


def operation_entry(path: Path) -> DirectoryId | None:
    try:
        return directory_id(path.lstat())
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class WorktreeRegistration:
    common: Path
    path: Path
    identity: DirectoryId

    @classmethod
    def of(cls, checkout: git_files.Checkout) -> WorktreeRegistration | None:
        common, metadata = checkout.common_dir, checkout.git_dir
        if metadata.parent != (common / "worktrees").resolve():
            return None
        try:
            backlink = (metadata / "gitdir").read_text().rstrip("\r\n")
        except FileNotFoundError:
            return None
        if not backlink or (metadata / backlink).resolve() != checkout.toplevel / ".git":
            return None
        return cls(common, metadata, directory_id(checkout.git_dir_status))


class OperationStep(Enum):
    RENAME = "rename"
    COPY = "copy"
    PUBLISH = "publish"
    HIDE_FOR_REMOVAL = "hide-for-removal"
    DELETE = "delete"


@dataclass(frozen=True)
class FolderOperation:
    step: OperationStep
    source: Path
    source_id: DirectoryId
    hidden: Path | None = None
    destination: Path | None = None
    stage: Path | None = None
    stage_id: DirectoryId | None = None
    task: str = ""
    made: float = 0.0
    registration: WorktreeRegistration | None = None

    def paths(self) -> tuple[Path, ...]:
        paths = (self.source, self.hidden, self.destination, self.stage)
        return tuple(path for path in paths if path is not None)


def operation_path(value: object) -> Path:
    if isinstance(value, str):
        path = Path(value)
        if path.is_absolute() and path.name and ".." not in path.parts:
            return path
    raise ValueError("invalid operation path")


def operation_id(value: object) -> DirectoryId:
    if isinstance(value, list):
        pair = cast(list[object], value)
        if len(pair) == 2 and isinstance(pair[0], int) and isinstance(pair[1], int):
            return pair[0], pair[1]
    raise ValueError("invalid directory identity")


@dataclass(frozen=True)
class OperationJournal:
    path: Path

    def save(self, operation: FolderOperation) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        registration = operation.registration
        atomic_record(self.path, {
            "step": operation.step.value, "source": str(operation.source), "source_id": operation.source_id,
            "hidden": str(operation.hidden) if operation.hidden else None,
            "destination": str(operation.destination) if operation.destination else None,
            "stage": str(operation.stage) if operation.stage else None, "stage_id": operation.stage_id,
            "task": operation.task, "made": operation.made,
            "registration": [str(registration.common), str(registration.path), registration.identity] if registration else None,
        }, sync=True)

    def read(self) -> FolderOperation:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("expected a regular file")
                value = cast(object, json.load(stream))
            if not isinstance(value, dict):
                raise ValueError("expected an operation record")
            fields = cast(dict[str, object], value)
            source = operation_path(fields.get("source"))
            if self.path.name != operation_journal_name(source):
                raise ValueError("record name does not match its source")
            hidden, destination, stage = (operation_path(fields[key]) if fields.get(key) is not None else None
                                          for key in ("hidden", "destination", "stage"))
            if hidden is not None and (hidden.parent != source.parent or not hidden.name.startswith(".scratch-operation-")):
                raise ValueError("invalid hidden source")
            if stage is not None and (destination is None or stage.parent != destination.parent
                                      or not stage.name.startswith(".scratch-operation-")):
                raise ValueError("invalid staging folder")
            step = OperationStep(fields["step"])
            task, made = fields["task"], fields["made"]
            if not isinstance(task, str) or not isinstance(made, (float, int)):
                raise ValueError("invalid task record")
            raw = fields.get("registration")
            registration = None
            if raw is not None:
                if not isinstance(raw, list) or len(raw) != 3:
                    raise ValueError("invalid Git registration")
                parts = cast(list[object], raw)
                registration = WorktreeRegistration(operation_path(parts[0]), operation_path(parts[1]), operation_id(parts[2]))
            stage_id = operation_id(fields["stage_id"]) if fields.get("stage_id") is not None else None
            if step is OperationStep.RENAME:
                valid = destination is not None and hidden is None and stage is None and registration is None
            elif step in (OperationStep.COPY, OperationStep.PUBLISH):
                valid = (hidden is not None and destination is not None and stage is not None and registration is None
                         and (step is OperationStep.COPY or stage_id is not None))
            else:
                valid = hidden is not None and destination is None and stage is None
            if not valid or len({source, hidden, destination, stage} - {None}) != sum(
                    path is not None for path in (source, hidden, destination, stage)):
                raise ValueError("invalid operation paths")
            return FolderOperation(step, source, operation_id(fields.get("source_id")),
                                   hidden, destination, stage, stage_id, task, float(made), registration)
        except (OSError, ValueError, KeyError) as error:
            raise RemovalRefused(f"{self.path}: cannot read folder operation: {error}") from error

    def clear(self, removal: CoordinatedRemoval) -> None:
        from scripts.lib.atomic_file import sync_folder

        removal.delete(self.path, RemovalKind.FILE)
        sync_folder(self.path.parent)


def operation_journal_name(source: Path) -> str:
    return hashlib.sha256(os.fsencode(source)).hexdigest() + ".json"


def operation_journal_root(settings: conf.ConfigRead) -> Path:
    root = holds_file(scratch_settings=settings).parent / "folder-operations"
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    sweep.refuse_live_test_roots((root,))
    return root


def operation_journal(source: Path, settings: conf.ConfigRead) -> OperationJournal:
    return OperationJournal(operation_journal_root(settings) / operation_journal_name(source))


def rename_operation_entry(removal: CoordinatedRemoval, source: Path, destination: Path,
                           expected: DirectoryId | None) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as parents:
        old_parent = os.open(source.parent, flags)
        _ = parents.callback(os.close, old_parent)
        new_parent = os.open(destination.parent, flags)
        _ = parents.callback(os.close, new_parent)
        old = AnchoredRemoval(old_parent, source.name)
        if directory_id(os.stat(old.name, dir_fd=old.parent, follow_symlinks=False)) != expected:
            raise RemovalRefused(f"{source}: directory changed before rename")
        removal.rename_new(old, AnchoredRemoval(new_parent, destination.name))
        os.fsync(old_parent)
        os.fsync(new_parent)


def require_quiescent(path: Path) -> None:
    """Refuse a source that a process can still use."""
    lock_fds = RECORD_LOCK_FDS.get() | frozenset(held_build_folder_descriptors())
    for held in _REMOVAL_LOCKS.get():
        if held.active and held.process == os.getpid() and held.thread == get_ident():
            lock_fds |= frozenset(held.marks.held.values())
            if isinstance(held.lease, HeldFolderRemovalLease):
                lock_fds |= {held.lease.descriptor, held.lease.directory}
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    sweep.require_quiescent(path, lock_fds)


def recover_folder_operation(removal: CoordinatedRemoval, journal: OperationJournal, operation: FolderOperation,
                             settings: conf.ConfigRead, *, cleanup: Callable[[WorktreeRegistration], bool] | None = None,
) -> Path | None:
    """Restore an unpublished source or finish a published operation."""
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    sweep.refuse_live_test_roots(operation.paths())
    removal.check_paths(operation.paths())
    source, hidden, destination, stage = operation.source, operation.hidden, operation.destination, operation.stage
    original = operation_entry(source)
    saved = operation_entry(hidden) if hidden is not None else None
    if saved is not None and saved != operation.source_id:
        raise RemovalRefused(f"{hidden}: hidden source identity changed")
    if operation.step is OperationStep.DELETE:
        if original is not None:
            raise RemovalRefused(f"{source}: source name reappeared during removal")
        if hidden is not None and saved is not None:
            require_quiescent(hidden)
            removal.delete(hidden, RemovalKind.TREE)
            from scripts.lib.atomic_file import sync_folder

            sync_folder(hidden.parent)
        if operation.registration is not None:
            finish = cleanup or removal.remove_worktree_registration
            if not finish(operation.registration) or operation_entry(operation.registration.path) is not None:
                raise RemovalRefused(f"{operation.registration.path}: Git registration cleanup is incomplete")
        journal.clear(removal)
        return None
    published_id = operation.source_id if operation.step is OperationStep.RENAME else operation.stage_id
    published = (operation.step in (OperationStep.RENAME, OperationStep.PUBLISH)
                 and destination is not None and published_id is not None
                 and operation_entry(destination) == published_id)
    if published and destination is not None:
        if original is not None:
            raise RemovalRefused(f"{source}: source name reappeared during adoption")
        if operation.step is OperationStep.RENAME:
            write_record(destination, operation.task, operation.made, scratch_settings=settings)
        index_in_session(destination, operation.task, scratch_settings=settings)
        if hidden is not None and saved is not None:
            require_quiescent(hidden)
            removal.delete(hidden, RemovalKind.TREE)
            from scripts.lib.atomic_file import sync_folder

            sync_folder(hidden.parent)
        journal.clear(removal)
        return destination
    if saved is not None and hidden is not None:
        if original is not None:
            raise RemovalRefused(f"{source}: cannot restore the original source name")
        rename_operation_entry(removal, hidden, source, operation.source_id)
    elif original != operation.source_id:
        raise RemovalRefused(f"{source}: cannot find the original source")
    if stage is not None and (staged := operation_entry(stage)) is not None:
        if operation.stage_id is not None and operation.stage_id != staged:
            raise RemovalRefused(f"{stage}: staging identity changed")
        removal.delete(stage, RemovalKind.TREE)
    journal.clear(removal)
    return None


def recover_folder_operations(paths: Iterable[Path], *, dry_run: bool = False,
                              scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> dict[Path, Path]:
    """Recover recorded operations that overlap the requested paths."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    root = operation_journal_root(scratch_settings)
    if not os.path.lexists(root):
        return {}
    if root.is_symlink() or not root.is_dir():
        raise RemovalRefused(f"{root}: expected an operation journal directory")
    scope = resolved_removal_paths(paths)
    completed: dict[Path, Path] = {}
    with coordinate_removals(scratch_settings=scratch_settings, dry_run=dry_run, wait_for_hold_lock=False) as removal:
        for record in sorted(root.glob("*.json")):
            journal = OperationJournal(record)
            operation = journal.read()
            if not any(path.is_relative_to(base) or base.is_relative_to(path)
                       for path in operation.paths() for base in scope):
                continue
            if dry_run:
                raise RemovalRefused(f"{operation.source}: a folder operation needs recovery")
            result = recover_folder_operation(removal, journal, operation, scratch_settings)
            if result is not None:
                completed[operation.source] = result
    return completed


def remove_journaled_tree(folder: Path, checkout: git_files.Checkout | None, *,
                          cleanup: Callable[[WorktreeRegistration], bool] | None = None,
                          scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """Hide a tree, check process use, and record deletion before it starts."""
    if isinstance(scratch_settings, UseConfiguredValue):
        locked = _LOCKED_HOLD_FILE.get()
        scratch_settings = conf.read("scratch") if locked is None else locked.settings
    folder = folder.parent.resolve() / folder.name
    with coordinate_removals(scratch_settings=scratch_settings, wait_for_hold_lock=False) as removal:
        _ = recover_folder_operations((folder,), scratch_settings=scratch_settings)
        info = operation_entry(folder)
        if info is None:
            return
        registration = WorktreeRegistration.of(checkout) if checkout is not None else None
        if checkout is not None:
            removal.check_worktree_prune((checkout.common_dir,))
        removal.check_tree(folder)
        hidden = folder.with_name(f".scratch-operation-{secrets.token_hex(16)}")
        operation = FolderOperation(OperationStep.HIDE_FOR_REMOVAL, folder, info, hidden=hidden,
                                    registration=registration)
        journal = operation_journal(folder, scratch_settings)
        journal.save(operation)
        try:
            rename_operation_entry(removal, folder, hidden, info)
            require_quiescent(hidden)
        except BaseException:
            _ = recover_folder_operation(removal, journal, operation, scratch_settings)
            raise
        operation = replace(operation, step=OperationStep.DELETE)
        journal.save(operation)
        _ = recover_folder_operation(removal, journal, operation, scratch_settings, cleanup=cleanup)


def removal_target(mark: Path) -> Path | None:
    """The folder a removal mark names; None when mark is no removal mark."""
    name = mark.name
    if not name.startswith(REMOVAL_RECORD_PREFIX) or not name.endswith(REMOVAL_RECORD_SUFFIX):
        return None
    folder = name[len(REMOVAL_RECORD_PREFIX):-len(REMOVAL_RECORD_SUFFIX)]
    return mark.with_name(folder) if folder else None


def marked_folders(entries: Iterable[Path]) -> Iterator[Path]:
    """Each folder whose removal mark is among entries, also when the folder is already gone."""
    for mark in entries:
        target = removal_target(mark)
        if target is not None and mark.is_file():
            yield target


def folder_record(path: Path) -> Path:
    """The file that holds path's task: its own record, else its removal mark."""
    record = path / copy_record.RECORD
    return record if record.is_file() else removal_mark(path)


def pending_removal(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
    """Read deletion intent and check the identity of its source."""
    record = checked_removal(path)
    if not isinstance(record, StaleRemoval):
        return record
    # A lifetime scan may already own a different copy's record lock. Never wait
    # on a sibling merely to discard an old deletion intent.
    with directory_record_lock(path, wait=False, scratch_settings=scratch_settings) as acquired:
        if acquired:
            current = checked_removal(path)
            if not isinstance(current, StaleRemoval):
                return current
            if cargo_removal_mark(path).exists():
                return copy_record.UnreadableRecord(f"{path}: previous copy needs Cargo recovery before path reuse")
            try:
                finish_removal(path, source_replaced=True, scratch_settings=scratch_settings)
            except (OSError, ScratchHoldsUnreadable) as error:
                return copy_record.UnreadableRecord(f"{path}: cannot remove the stale removal record: {error}")
    return copy_record.NotRegistered()


def checked_removal(path: Path) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord | StaleRemoval:
    record = copy_record.read_record(removal_mark(path))
    if not isinstance(record, copy_record.Registered):
        return record
    version = record.fields.get("removal_version")
    if version is not None and (type(version) is not int or version != REMOVAL_VERSION):
        return copy_record.UnreadableRecord(f"repair removal record {removal_mark(path)}: unsupported removal version")
    if version is None and copy_record.copy_folder(path) != path.resolve():
        return record
    if record.fields.get("copy_path") != str(path.resolve()):
        return copy_record.UnreadableRecord(f"repair removal record {removal_mark(path)}: copy path differs")
    try:
        directory = path.stat()
    except FileNotFoundError:
        return record
    except OSError as error:
        return copy_record.UnreadableRecord(f"repair copy identity {path}: {error}")
    device, inode = record.fields.get("copy_device"), record.fields.get("copy_inode")
    if not isinstance(device, int) or not isinstance(inode, int):
        return copy_record.UnreadableRecord(f"repair removal record {removal_mark(path)}: copy identity is missing")
    present = copy_record.read_record(path / copy_record.RECORD)
    different_record = (isinstance(present, copy_record.Registered) and bool(present.fields.get("copy_id"))
                        and present.fields.get("copy_id") != record.fields.get("copy_id"))
    if (device, inode) != (directory.st_dev, directory.st_ino) or different_record:
        return StaleRemoval()
    return record


def task_of(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
    """Read folder task evidence; plain files have no task record."""
    if os.path.isfile(path):
        return copy_record.NotRegistered()
    record = copy_record.read_record(path / copy_record.RECORD)
    return pending_removal(path, scratch_settings=scratch_settings) if isinstance(record, copy_record.NotRegistered) else record


def is_throwaway(path: Path) -> bool:
    """Identify a folder that code creates without edits from a person."""
    record = task_of(path)
    return isinstance(record, copy_record.Registered) and record.fields.get("throwaway") is True


def pending_copy_paths(base: Path) -> Iterator[Path]:
    if not base.exists():
        return
    for pair in base.iterdir():
        if pair.is_dir() and not pair.is_symlink():
            yield from marked_folders(pair.iterdir())


def copy_paths() -> Iterator[Path]:
    """Each pair/copy without following links. An unreadable registry fails closed."""
    base = copy_record.copies_root()
    if not base.exists():
        return
    for pair in base.iterdir():
        if pair.is_dir() and not pair.is_symlink():
            for path in pair.iterdir():
                if path.is_dir() and not path.is_symlink():
                    yield path


def registered_copy_paths() -> Iterator[Path]:
    """List native copies and adopted copies from the existing holds."""
    paths = set(copy_paths())
    for held in holds():
        path = Path(held)
        record = task_of(path)
        if (isinstance(record, copy_record.UnreadableRecord)
                or isinstance(record, copy_record.Registered) and "borrows_from" in record.fields):
            paths.add(path)
    yield from sorted(paths)


class UnregisteredBorrowCandidate(Enum):
    POSSIBLE_BORROWER = "possible borrower"
    WORKTREE_SUBFOLDER = "worktree subfolder"


def _unregistered_borrow_candidate(path: Path) -> UnregisteredBorrowCandidate:
    """Check the pair and candidate for Git entries without reading their targets."""
    pair = path.parent
    if pair.parent != copy_record.copies_root():
        return UnregisteredBorrowCandidate.POSSIBLE_BORROWER
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as descriptors:
        pair_fd = os.open(pair, flags)
        _ = descriptors.callback(os.close, pair_fd)
        candidate_fd = os.open(path.name, flags, dir_fd=pair_fd)
        _ = descriptors.callback(os.close, candidate_fd)
        try:
            _ = os.stat(".git", dir_fd=pair_fd, follow_symlinks=False)
        except FileNotFoundError:
            return UnregisteredBorrowCandidate.POSSIBLE_BORROWER
        for name in (".git", "HEAD", "objects", "commondir"):
            try:
                _ = os.stat(name, dir_fd=candidate_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            return UnregisteredBorrowCandidate.POSSIBLE_BORROWER
    return UnregisteredBorrowCandidate.WORKTREE_SUBFOLDER


def borrow_lifetime(path: Path, *, scratch_settings: conf.ConfigRead) -> Live | Ended | Unknown:
    """Check the copy registry for borrowers of this folder's Git objects."""
    protection: Live | Ended | Unknown = Ended("no copy borrows from it")
    try:
        for child in registered_copy_paths():
            try:
                mode = child.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                protection = Unknown(f"{child}: cannot check borrowed objects through a link")
                break
            if child.resolve() == path.resolve():
                continue
            other = task_of(child, scratch_settings=scratch_settings)
            if isinstance(other, copy_record.NotRegistered):
                if _unregistered_borrow_candidate(child) is UnregisteredBorrowCandidate.WORKTREE_SUBFOLDER:
                    continue
                protection = Unknown(f"repair record {child / copy_record.RECORD}: cannot rule out borrowed objects")
                break
            if isinstance(other, copy_record.UnreadableRecord):
                protection = Unknown(other.reason + "; cannot rule out borrowed objects")
                break
            borrowed = other.fields.get("borrows_from", "")
            if not isinstance(borrowed, str):
                protection = Unknown(f"repair record {child / copy_record.RECORD}: invalid borrows_from")
                break
            if borrowed and Path(borrowed).resolve() == (path / ".git").resolve():
                protection = Live(f"borrowed: {child} still uses its git objects")
                break
    except OSError as error:
        protection = Unknown(f"repair copies registry {copy_record.copies_root()}: {error}")
    return protection


@dataclass(frozen=True)
class DescendantLocksHeld:
    """The descendant checks passed. The supplied stack holds the locks."""

    checkouts: tuple[Path, ...]


def lock_descendant_clones(path: Path, locks: ExitStack, scope: BusyMarkScope, *, create: bool,
                           scratch_settings: conf.ConfigRead) -> DescendantLocksHeld | KeptFolder:
    """Hold descendant busy marks and check each clone for borrowers."""
    def unreadable(error: OSError) -> None:
        raise error

    clones: list[Path] = []
    try:
        for top, directories, files, _ in os.fwalk(path, onerror=unreadable, follow_symlinks=False):
            if ".git" in directories:
                directories.remove(".git")
                checkout = True
            else:
                checkout = ".git" in files
            child = Path(top)
            if child == path:
                continue
            if not checkout and (copy_record.RECORD in files or copy_record.RECORD in directories):
                record = copy_record.read_record(child / copy_record.RECORD)
                if isinstance(record, copy_record.UnreadableRecord):
                    return KeptFolder(path, record.reason)
                checkout = isinstance(record, copy_record.Registered) and record.fields.get("test_hana_clone") is True
            if checkout:
                if not locks.enter_context(busy_locks(child, create=create, scope=scope)):
                    return KeptFolder(path, f"{child}: {BUSY_REASON}")
                clones.append(child)
    except OSError as error:
        return KeptFolder(path, f"cannot check descendant clones: {error}")

    for child in clones:
        state = borrow_lifetime(child, scratch_settings=scratch_settings)
        if not isinstance(state, Ended):
            return KeptFolder(path, f"{child}: {state.why if isinstance(state, Live) else state.reason}")
    return DescendantLocksHeld(tuple(clones))


def record_lifetime(path: Path, now: float, ending_task: str = "", *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Live | Ended | Unknown:
    """Read end evidence while the caller holds any required removal lock."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    removal = pending_removal(path, scratch_settings=scratch_settings)
    if isinstance(removal, copy_record.UnreadableRecord):
        return Unknown(removal.reason)
    record = task_of(path, scratch_settings=scratch_settings)
    if isinstance(record, copy_record.UnreadableRecord):
        return Unknown(record.reason)
    if isinstance(record, copy_record.NotRegistered):
        return Unknown(f"repair record {path / copy_record.RECORD}: copy is not registered")
    protection = borrow_lifetime(path, scratch_settings=scratch_settings)
    if isinstance(removal, copy_record.Registered):
        return Ended(f"deletion intent for task {removal.task}") if isinstance(protection, Ended) else protection
    if record.task.startswith("phase:"):
        run, phase = record.fields.get("run_dir"), record.fields.get("phase_id")
        if not isinstance(run, str) or not run or not isinstance(phase, str) or not phase:
            return Unknown(f"repair record {path / copy_record.RECORD}: phase needs run_dir and phase_id")
        progress = Path(run) / "progress_history_state.json"
        try:
            raw = cast(object, json.loads(progress.read_text(encoding="utf-8")))
            if not isinstance(raw, dict):
                raise ValueError("expected progress object")
            state = cast(dict[str, object], raw)
            current = state.get("phase")
            parked = state.get("open_phases", [])
            if not isinstance(current, dict) or not isinstance(parked, list):
                raise ValueError("invalid phase state")
            phases: list[object] = [cast(object, current), *cast(list[object], parked)]
            for item in phases:
                if not isinstance(item, dict):
                    raise ValueError("invalid phase record")
                fields = cast(dict[str, object], item)
                if str(fields.get("id")) == phase:
                    status = fields.get("status")
                    if status == "active" or item in parked:
                        return Live(f"open phase: {phase} in {run}")
                    # finish-phase's --status choices in progress_history.py; no shared constant exists.
                    if status not in ("completed", "stopped", "error", "finished", "complete", "ended"):
                        raise ValueError("unreadable phase status")
                    return Ended(f"phase {phase} ended in {progress}") if isinstance(protection, Ended) else protection
            if record.task == ending_task or task_has_end_evidence(record):
                return Ended(f"task {record.task} ended; phase is no longer open in {progress}") if isinstance(protection, Ended) else protection
            raise ValueError(f"phase {phase} has no end record")
        except (OSError, ValueError, UnicodeError) as error:
            return Unknown(f"repair progress file {progress}: {error}")
    if record.task.startswith("landed:"):
        branch = record.task.split("/", 1)[-1]
        target = record.fields.get("land_target", "origin/main") or "origin/main"
        if not isinstance(target, str):
            return Unknown(f"repair land target in {path / copy_record.RECORD}")
        source = record.fields.get("source_path")
        if not isinstance(source, str) or not source:
            return Unknown(f"repair source checkout in {path / copy_record.RECORD}: source_path is missing")
        source_path = Path(source)
        try:
            if not source_path.is_dir():
                return Unknown(f"repair source checkout {source_path}: folder is missing or unreadable")
            for ref in ("origin/" + branch, target):
                result = _git(source_path, "rev-parse", "--verify", ref + "^{commit}")
                if result.returncode:
                    return Unknown(f"repair land target {ref} in source checkout {source_path}: {result.stderr.strip()}")
            ancestor = _git(source_path, "merge-base", "--is-ancestor", "origin/" + branch, target)
        except OSError as error:
            return Unknown(f"repair source checkout {source_path}: {error}")
        if ancestor.returncode == 1:
            return Live(f"not landed: origin/{branch} is not in {target}")
        if ancestor.returncode:
            return Unknown(f"repair land target {target} in source checkout {source_path}: {ancestor.stderr.strip()}")
        work = _removal_work(path, scratch_settings, publication=RemoteInspection.LOCAL_ONLY)
        if isinstance(work, RemoteKeptFolder):
            return LandedRemoteCheckNeeded(work.reason) if isinstance(protection, Ended) else protection
        if isinstance(work, KeptFolder):
            return Unknown(work.reason) if "git cannot" in work.reason else Live("not landed: " + work.reason)
        return Ended(f"origin/{branch} landed in {target}") if isinstance(protection, Ended) else protection
    if record.task == ending_task or task_has_end_evidence(record):
        return Ended(f"task {record.task} ended") if isinstance(protection, Ended) else protection
    deadline = task_end(record.task, now)
    return protection if isinstance(deadline, Ended) and not isinstance(protection, Ended) else deadline


def task_has_end_evidence(record: copy_record.Registered) -> bool:
    at = record.fields.get("ended", 0)
    return record.fields.get("ended_task") == record.task and isinstance(at, (int, float)) and at > 0


def lifetime(path: Path, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Live | Ended | Unknown:
    with judgment_lock(path) as idle:
        if not idle:
            return Live(BUSY_REASON)
    return record_lifetime(path, time.time() if now is None else now, scratch_settings=scratch_settings)


def folders(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[Folder]:
    """Read scratch roots and the recorded paths of worker folders."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    candidates: dict[Path, None] = {}
    for base in (copy_record.root(), visible_root(scratch_settings=scratch_settings)):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        listed = {path for path in entries if path.is_dir()} | set(marked_folders(entries))
        candidates.update(dict.fromkeys(sorted(listed)))
    candidates.update(dict.fromkeys((*copy_paths(), *pending_copy_paths(copy_record.copies_root()))))
    candidates.update(dict.fromkeys(Path(path) for path, held in holds(scratch_settings=scratch_settings).items()
                                    if held["task"].startswith("gate:worker-")))
    found: list[Folder] = []
    for path in candidates:
        record = task_of(path, scratch_settings=scratch_settings) if not path.is_symlink() else copy_record.NotRegistered()
        if isinstance(record, copy_record.Registered):
            try:
                modified = folder_record(path).stat().st_mtime
            except FileNotFoundError:
                continue
            found.append(Folder(path, record.task, modified))
    return found


def renew(path: Path, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    moment = time.time() if now is None else now

    def renewed(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered):
            raise ScratchError(f"{path} is no scratch folder")
        return {**record.fields, "renewed": moment}

    with copy_record_lock(path, scratch_settings=scratch_settings):
        _ = update_record(path, renewed, scratch_settings=scratch_settings)
        os.utime(path / copy_record.RECORD, (moment, moment))


@dataclass(frozen=True)
class SavedCargoOutput:
    """Output ownership saved before a checkout can disappear."""
    roots: cargo_output.CargoOutputRoots
    other_checkouts: tuple[Path, ...]
    shared_output_roots: tuple[Path, ...] = ()
    saved_at: float = 0.0

    def borrows_build_directory(self) -> bool:
        return any(self.roots.build_directory.is_relative_to(root) for root in self.shared_output_roots)


# folder_audit.py --clean-cargo-output exits with this status when its guard keeps the output.
CARGO_OUTPUT_KEPT_STATUS = 3


class CargoOutputKept(OSError):
    """The guard kept a checkout's Cargo output, so the checkout stays for a later sweep."""


def cargo_removal_mark(path: Path) -> Path:
    return path.parent / f".scratch-cargo-{path.name}.json"


def cargo_checkout_peers(workspace: Path) -> tuple[Path, ...]:
    """Read related worktrees and registered scratch checkouts before source removal."""
    peers: set[Path] = set()
    result = _git(workspace, "worktree", "list", "--porcelain")
    if result.returncode == 0:
        peers.update(Path(line.removeprefix("worktree ")).resolve()
                     for line in result.stdout.splitlines() if line.startswith("worktree "))
    for folder in folders():
        peers.update(repo.resolve() for repo in repos(folder.path))
    peers.update(repo.resolve() for held in holds() for repo in repos(Path(held)))
    return tuple(sorted(peers - {workspace}))


def save_cargo_outputs(path: Path) -> tuple[SavedCargoOutput, ...]:
    """Save the mapping outside the deletion tree. Keep the first mapping on retries."""
    path = path.resolve()
    mark = cargo_removal_mark(path)
    if mark.exists():
        try:
            raw = cast(object, json.loads(mark.read_text()))
        except (ValueError, UnicodeError) as error:
            raise OSError(f"invalid Cargo removal record: {mark}: {error}") from error
        if not isinstance(raw, list):
            raise OSError(f"invalid Cargo removal record: {mark}")
        saved: list[SavedCargoOutput] = []
        for item in cast(list[object], raw):
            if not isinstance(item, dict):
                raise OSError(f"invalid Cargo removal record: {mark}")
            fields = cast(dict[str, object], item)
            values = [fields.get(key) for key in ("workspace_root", "target_directory", "build_directory")]
            peers = fields.get("other_checkouts")
            if (not all(isinstance(value, str) and Path(value).is_absolute() for value in values)
                    or not isinstance(peers, list) or not all(isinstance(peer, str) and Path(peer).is_absolute()
                                                                           for peer in cast(list[object], peers))):
                raise OSError(f"invalid Cargo removal record: {mark}")
            roots = cargo_output.CargoOutputRoots(*(Path(cast(str, value)).resolve() for value in values))
            if not roots.workspace_root.is_relative_to(path):
                raise OSError(f"Cargo workspace is outside removal tree: {mark}")
            saved_at = fields.get("saved_at", 0.0)
            if not isinstance(saved_at, (int, float)):
                raise OSError(f"invalid Cargo removal record: {mark}")
            target_link = roots.workspace_root / "target"
            legacy_shared = target_link.resolve() if target_link.is_symlink() else roots.target_directory
            shared = fields.get("shared_output_roots", [str(legacy_shared)]
                                if fields.get("shared_target") is True else [])
            if (not isinstance(shared, list)
                    or not all(isinstance(root, str) and Path(root).is_absolute()
                               for root in cast(list[object], shared))):
                raise OSError(f"invalid Cargo removal record: {mark}")
            saved.append(SavedCargoOutput(roots, tuple(Path(peer) for peer in cast(list[str], peers)),
                                          tuple(Path(root).resolve() for root in cast(list[str], shared)), float(saved_at)))
        return tuple(saved)
    found: list[SavedCargoOutput] = []
    checkouts = set(repos(path))
    if (path / "Cargo.toml").is_file():
        checkouts.add(path)
    for checkout in sorted(checkouts):
        if not (checkout / "Cargo.toml").is_file():
            continue
        roots = cargo_output.read_cargo_output_roots(checkout, options=("--locked",))
        if isinstance(roots, cargo_output.CargoOutputUnavailable):
            print(f"scratch: cannot read Cargo output for {checkout}: {roots.reason}", file=sys.stderr)
            continue
        if roots.workspace_root.is_relative_to(path) and roots not in [item.roots for item in found]:
            found.append(SavedCargoOutput(roots, cargo_checkout_peers(roots.workspace_root),
                                          ((checkout / "target").resolve(),) if (checkout / "target").is_symlink() else (),
                                          time.time()))
    if found:
        atomic_record(mark, [{"workspace_root": str(item.roots.workspace_root),
                             "target_directory": str(item.roots.target_directory),
                             "build_directory": str(item.roots.build_directory),
                             "other_checkouts": [str(peer) for peer in item.other_checkouts],
                             "shared_output_roots": [str(root) for root in item.shared_output_roots],
                             "saved_at": item.saved_at} for item in found])
    return tuple(found)


def has_external_cargo_output(path: Path, saved: tuple[SavedCargoOutput, ...]) -> bool:
    root = path.resolve()
    return any(not item.roots.build_directory.is_relative_to(root) and item.roots.build_directory.exists()
               for item in saved)


def check_cargo_cleanup(removal: CoordinatedRemoval, path: Path, saved: tuple[SavedCargoOutput, ...],
                        local_targets: Iterable[Path] = ()) -> None:
    """Check every directory before Cargo cleanup can delete any output."""
    directories = {path, *local_targets}
    for item in saved:
        roots = item.roots
        directories.update((roots.workspace_root, roots.target_directory, roots.build_directory))
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    paths = tuple(sorted(directories))
    sweep.refuse_live_test_roots(paths)
    try:
        removal.check_paths(paths)
    except RemovalRefused as error:
        raise CargoOutputKept(f"{path}: {error}") from error


class _CargoCleanup(Protocol):
    def clean_external_cargo_output(self, path: Path, saved: tuple[SavedCargoOutput, ...], attempt_started: float,
                                   removal: CoordinatedRemoval) -> None: ...


def clean_cargo_outputs(path: Path, saved: tuple[SavedCargoOutput, ...]) -> None:
    """Run the same Cargo cleanup guard with or without an existing hold lock."""
    if not has_external_cargo_output(path, saved):
        return
    removal = _ACTIVE_REMOVAL.get()
    if removal is not None:
        audit = cast(_CargoCleanup, cast(object, import_module("scripts.lint.folder_audit")))
        try:
            audit.clean_external_cargo_output(path.resolve(), saved, time.time(), removal)
        except (OSError, ValueError) as error:
            raise CargoOutputKept(f"Cargo output kept: {error}") from error
        return
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "lint" / "folder_audit.py"),
                             "--clean-cargo-output", str(path.resolve())],
                            capture_output=True, text=True, check=False)
    if result.returncode == CARGO_OUTPUT_KEPT_STATUS:
        raise CargoOutputKept(result.stderr.strip())
    if result.returncode:
        raise OSError(result.stderr.strip() or "Cargo output cleanup failed")
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)


@dataclass(frozen=True)
class TaskRemoval:
    """End the selected task while preserving copy lifetime and ownership checks."""
    task: str
    now: float
    require_task: bool = True


@dataclass(frozen=True)
class SelectedCopyRemoval:
    """Remove the exact selected copy after its owner validates the saved work."""
    expected: copy_record.Registered
    now: float
    validate: Callable[[], None]


@dataclass(frozen=True)
class FolderRemoval:
    """Remove a folder whose caller has established its lifetime."""


FOLDER_REMOVAL = FolderRemoval()


@dataclass(frozen=True)
class EmptyFolderRemoval:
    """Remove a directory only if it is still empty at deletion."""


@dataclass(frozen=True)
class ExpiredFolderRemoval:
    """Recheck a copy's expired lifetime before a sweep removes its folder."""
    now: float


class SourceDeletion(Enum):
    """Select the existing source deletion method after all safety checks pass."""
    STANDARD = "standard"
    SWEEP = "sweep"
    RUN = "run"


class _GitWorkAssessment(Protocol):
    """Read the sweep's saved-work assessment without importing its dependency graph."""
    @property
    def name(self) -> str: ...
    @property
    def value(self) -> str: ...


class _SweepRemoval(Protocol):
    """Use the existing sweep checks and deletion method at the module boundary."""
    def git_work(self, path: Path, *, verified_repos: tuple[Path, ...]) -> _GitWorkAssessment: ...
    def delete_tree(self, path: str) -> None: ...
    def refuse_live_test_roots(self, roots: tuple[Path, ...]) -> None: ...
    def require_quiescent(self, path: Path, lock_fds: frozenset[int]) -> None: ...


class _WorkspaceRescue(Protocol):
    """Save workspace work through its existing owner before the coordinator deletes source."""
    def rescue_workspace(self, tree: Path, record: copy_record.Registered) -> None: ...


class _RunSourceRemoval(Protocol):
    """Preserve the run cleanup's worktree removal and source pruning method."""
    def remove_tree(self, folder: Path) -> None: ...


class RepositoryRemovalEvidence(TypedDict):
    """Retain repository locations and references independently of the source tree."""
    path: str
    head: str
    references: list[str]


def removal_source_files(path: Path) -> dict[str, int]:
    """Read every regular file's ctime, including files inside Git directories."""
    root = path.resolve()
    return removal_regular_files(root, removal_entries(root))


def removal_entries(root: Path) -> dict[Path, git_files.RemovalEntry]:
    try:
        return git_files.scan_removal_tree(root)
    except git_files.UnreadableGitFiles as error:
        raise OSError(str(error)) from error


def removal_regular_files(root: Path, entries: Mapping[Path, git_files.RemovalEntry]) -> dict[str, int]:
    return {str(entry.path.relative_to(root)): entry.status.st_ctime_ns
            for entry in entries.values() if stat.S_ISREG(entry.status.st_mode)}


def removal_checkouts(path: Path, descendants: tuple[Path, ...]) -> tuple[git_files.Checkout, ...]:
    checkouts: list[git_files.Checkout] = []
    for folder in (path, *descendants):
        git = git_directory(folder)
        if isinstance(git, UnreadableGitDirectory):
            raise OSError(git.reason)
        if isinstance(git, GitDirectory):
            checkouts.append(git.checkout)
    return tuple(checkouts)


def removal_ref_entries(entries: Mapping[Path, git_files.RemovalEntry],
                        checkouts: tuple[git_files.Checkout, ...]) -> Iterator[git_files.RemovalEntry]:
    directories = {directory for checkout in checkouts for directory in (checkout.git_dir, checkout.common_dir)}
    directories.update(path for path, entry in entries.items()
                       if path.name == ".git" and stat.S_ISDIR(entry.status.st_mode))
    if not directories:
        return
    files = {directory / name for directory in directories for name in ("HEAD", "packed-refs")}
    prefixes = tuple(str(directory / name) + os.sep for directory in directories for name in ("refs", "reftable"))
    for path, entry in entries.items():
        if not stat.S_ISDIR(entry.status.st_mode) and (path in files or str(path).startswith(prefixes)):
            yield entry


def removal_ref_stat(entry: git_files.RemovalEntry) -> tuple[int, int, int]:
    """Identify Git's replacement of a ref file without reading the file."""
    status = entry.status
    return status.st_ino, status.st_size, status.st_mtime_ns


def changed_removal_ref(root: Path, refs: Iterable[git_files.RemovalEntry], recorded: object,
                        legacy_mtime: int | None) -> Path | None:
    if recorded is None:
        # A legacy mark and its refs use filesystem timestamps.
        # An old versioned mark cannot compare its wall time with ref ctime.
        return min((entry.path for entry in refs
                    if legacy_mtime is None or entry.status.st_ctime_ns >= legacy_mtime), default=None)
    if not isinstance(recorded, dict):
        raise OSError(f"{root}: invalid removal ref evidence")
    previous = cast(dict[str, object], recorded)
    return min((entry.path for entry in refs
                if previous.get(str(entry.path.relative_to(root))) != list(removal_ref_stat(entry))), default=None)


def empty_lost_worktree(path: Path, git: GitDirectoryState) -> bool:
    """Check that a worktree with a missing Git directory has no source files."""
    return (isinstance(git, (NoGitDirectory, git_files.MissingReferencedGitDirectory))
            and (path / ".git").is_file()
            and set(removal_source_files(path)).issubset({".git", copy_record.RECORD, BUSY}))


class RemovalRetry(Enum):
    """Select the work check from changes since the removal intent."""
    UNCHANGED = "unchanged"
    RECHECK = "recheck"


def repository_removal_evidence(path: Path, *, scratch_settings: conf.ConfigRead) -> list[RepositoryRemovalEvidence]:
    """Record repository locations and references before source deletion can change them."""
    evidence: list[RepositoryRemovalEvidence] = []
    for repo in repos(path, scratch_settings=scratch_settings):
        references = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        if references.returncode:
            raise ScratchError(f"{repo}: cannot record Git references before removal")
        head = _git(repo, "rev-parse", "--verify", "HEAD")
        evidence.append(RepositoryRemovalEvidence(path=str(repo.relative_to(path)),
                        head=head.stdout.strip() if head.returncode == 0 else "",
                        references=references.stdout.splitlines()))
    return evidence


def recovery_state(path: Path, pending: copy_record.Registered, scope: BusyMarkScope, *,
                   scratch_settings: conf.ConfigRead, descendant_checkouts: tuple[Path, ...] = ()) -> RemovalRetry | KeptFolder:
    """Resume unchanged sources and check changed sources before another deletion attempt."""
    mark_mtime = removal_mark(path).stat().st_mtime_ns
    intent = pending.fields.get("removal_intent_ns", mark_mtime)
    if type(intent) is not int:
        return KeptFolder(path, "invalid removal intent time")
    # A changed file can share the mark's timestamp and still come after the intent.
    source_cutoff = min(intent, mark_mtime)
    versioned = "removal_version" in pending.fields
    regular_cutoff = source_cutoff if versioned else source_cutoff + 1
    saved_files = pending.fields.get("removal_files")
    previous = (git_files.RecordedRemovalFiles(frozenset(name for name in cast(list[object], saved_files)
                                                         if isinstance(name, str)))
                if pending.fields.get("removal_version") == REMOVAL_VERSION and isinstance(saved_files, list)
                else git_files.LegacyRemovalFiles())
    removal_root = path.resolve()
    try:
        checkouts = removal_checkouts(path, descendant_checkouts)
        git_files.check_internal_removal_entries(checkouts, previous, removal_root=removal_root)
        entries = git_files.scan_removal_tree(removal_root, checkouts=checkouts, previous=previous)
        refs = {entry.path: entry for entry in removal_ref_entries(entries, checkouts)}
        legacy_mtime = None if versioned else mark_mtime
        recorded_refs = pending.fields.get("removal_ref_files")
        ref_change = changed_removal_ref(removal_root, refs.values(), recorded_refs, legacy_mtime)
        if ref_change is not None:
            change = "may have changed" if recorded_refs is None else "changed"
            return ChangedRemovalRef(path, f"ambiguous source: {ref_change} {change} after removal intent")
        busy_marks = {removal_root / mark.relative_to(path): os.fstat(scope.held[mark])
                      for mark in scope.created if mark.is_relative_to(path)}
    except (OSError, git_files.UnreadableGitFiles) as error:
        return KeptFolder(path, str(error))

    # Exempt only the busy mark that the retry created and still holds.
    changed = min((entry.path for entry in entries.values()
                   if entry.path not in refs
                   and ((stat.S_ISREG(entry.status.st_mode) and entry.status.st_ctime_ns >= regular_cutoff)
                        or (stat.S_ISLNK(entry.status.st_mode) and entry.status.st_ctime_ns >= source_cutoff))
                   and (entry.path not in busy_marks or not os.path.samestat(entry.status, busy_marks[entry.path]))),
                  default=RemovalRetry.UNCHANGED)
    if changed is RemovalRetry.UNCHANGED:
        return RemovalRetry.UNCHANGED
    reason = f"ambiguous source: {changed} changed after removal intent"
    files = {str(entry.path.relative_to(removal_root)) for entry in entries.values()
             if stat.S_ISREG(entry.status.st_mode)}
    found = repos(path, scratch_settings=scratch_settings)
    if pending.fields.get("removal_version") == REMOVAL_VERSION:
        raw = pending.fields.get("removal_repositories")
        if not isinstance(raw, list):
            return KeptFolder(path, reason + "; repository evidence is missing")
        expected: set[str] = set()
        for item in cast(list[object], raw):
            if not isinstance(item, dict):
                return KeptFolder(path, reason + "; repository evidence is invalid")
            relative = cast(dict[str, object], item).get("path")
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                return KeptFolder(path, reason + "; repository evidence is invalid")
            expected.add(relative)
        if {str(repo.relative_to(path)) for repo in found} != expected:
            return KeptFolder(path, reason + "; repository locations changed")
        saved_files = pending.fields.get("removal_files")
        if isinstance(saved_files, list) and any(name not in files for name in cast(list[object], saved_files)
                                                if isinstance(name, str) and ".git" not in Path(name).parts):
            return KeptFolder(path, reason + "; source files are missing")
    else:
        # Old marks cannot identify a checkout whose Git directory disappeared.
        metadata = {copy_record.RECORD, BUSY, *verified_mirror_metadata(path)}
        if any(str(entry.path.relative_to(removal_root)) not in metadata
               and not any(entry.path.is_relative_to(repo.resolve()) for repo in found)
               for entry in entries.values() if stat.S_ISREG(entry.status.st_mode) or stat.S_ISLNK(entry.status.st_mode)):
            return KeptFolder(path, reason + "; legacy source has no complete repository evidence")
    for repo in found:
        if not isinstance(git_directory(repo), GitDirectory):
            return KeptFolder(path, reason + f"; Git metadata is incomplete in {repo}")
        if _git(repo, "fsck", "--connectivity-only", "--no-reflogs").returncode:
            return KeptFolder(path, reason + f"; Git objects are incomplete in {repo}")
    return RemovalRetry.RECHECK


@dataclass(frozen=True)
class KeptFolder:
    """A folder that cleanup retains for the stated reason."""

    path: Path
    reason: str


@dataclass(frozen=True)
class ChangedRemovalRef(KeptFolder):
    """The retry must keep a folder whose Git ref changed."""


@dataclass(frozen=True)
class RemoteKeptFolder(KeptFolder):
    """A folder with work that requires remote publication evidence."""


@dataclass(frozen=True)
class LocalRemoteKeptFolder(RemoteKeptFolder):
    """The local remote cannot supply independent evidence for removal."""


@dataclass(frozen=True)
class RemoteStorageUnproven(LocalRemoteKeptFolder):
    """The local remote's repository or object storage could not be verified."""


@dataclass(frozen=True)
class RemoteInsideRemovalTree(LocalRemoteKeptFolder):
    """Removal would delete a local repository that could supply publication evidence."""


@dataclass(frozen=True)
class RemovalWorkSaved:
    """The work check found no unsaved work in these repositories."""

    repositories: tuple[Path, ...] = ()


class RemovalWorkCheck(Enum):
    STANDARD = "standard"
    LANDED = "landed"


def check_removal_work(path: Path, request: TaskRemoval | SelectedCopyRemoval | FolderRemoval | ExpiredFolderRemoval,
                       scratch_settings: conf.ConfigRead, *, publication: PublicationCheck,
                       check: RemovalWorkCheck) -> RemovalWorkSaved | KeptFolder:
    """Check current work under the removal locks."""
    if not isinstance(request, ExpiredFolderRemoval):
        return _removal_work(path, scratch_settings, publication=publication)
    verified_repos: tuple[Path, ...] = ()
    if check is RemovalWorkCheck.LANDED:
        work = _removal_work(path, scratch_settings, publication=publication)
        if isinstance(work, KeptFolder):
            return work
        verified_repos = work.repositories
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    work = sweep.git_work(path, verified_repos=verified_repos)
    return RemovalWorkSaved() if work.name == "SAVED" else KeptFolder(path, work.value)


def finish_removal(path: Path, *, cargo_cleaned: bool = False, source_replaced: bool = False,
                   ending_task: str | None = None,
                   scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> None:
    """Finish Cargo recovery before discarding the task's deletion intent."""
    if source_replaced:
        if cargo_removal_mark(path).exists():
            raise ScratchError(f"{path}: previous copy needs Cargo recovery before path reuse")
        with coordinate_removals(scratch_settings=scratch_settings, wait_for_hold_lock=False):
            delete_entry(removal_mark(path), RemovalKind.FILE, missing_ok=True)
        return
    if os.path.lexists(path):
        raise ScratchError(f"{path}: source still exists; removal completion refused")
    with coordinate_removals(scratch_settings=scratch_settings, wait_for_hold_lock=False,
                             ending_task=ending_task) as removal:
        if not cargo_cleaned:
            saved = save_cargo_outputs(path)
            check_cargo_cleanup(removal, path, saved)
            clean_cargo_outputs(path, saved)
        delete_entry(cargo_removal_mark(path), RemovalKind.FILE, missing_ok=True)
        delete_entry(removal_mark(path), RemovalKind.FILE, missing_ok=True)


def remove_folder(path: Path,
                  request: TaskRemoval | SelectedCopyRemoval | FolderRemoval | EmptyFolderRemoval | ExpiredFolderRemoval = FOLDER_REMOVAL, *,
                  source_deletion: SourceDeletion = SourceDeletion.STANDARD, dry_run: bool = False,
                  ending_task: str | None = None,
                  scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> RemovedCopy | KeptFolder:
    """Own removal locks, safety checks, preparation, deletion intent, source deletion, and completion."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    path = path.absolute()
    if isinstance(request, TaskRemoval):
        ending_task = request.task
    elif isinstance(request, SelectedCopyRemoval):
        ending_task = request.expected.task
    sweep = cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep")))
    sweep.refuse_live_test_roots((path,))
    _ = recover_folder_operations((path,), dry_run=dry_run, scratch_settings=scratch_settings)
    with ExitStack() as locks:
        CoordinatedRemoval.check_tree(path)
        if source_deletion is SourceDeletion.SWEEP:
            try:
                removal = locks.enter_context(coordinate_removals(dry_run=dry_run, ending_task=ending_task,
                                                                 scratch_settings=scratch_settings))
                removal.check(path)
            except (ScratchHoldsUnreadable, RemovalRefused) as error:
                return KeptFolder(path, str(error))
            except OSError as error:
                return KeptFolder(path, f"cannot check scratch holds: {error}")
        if isinstance(request, EmptyFolderRemoval):
            try:
                if dry_run:
                    if path.is_symlink() or any(path.iterdir()):
                        return KeptFolder(path, "the path is not an empty directory")
                else:
                    with coordinate_removals(scratch_settings=scratch_settings, ending_task=ending_task) as removal:
                        removal.delete(path, RemovalKind.EMPTY_DIRECTORY)
            except FileNotFoundError:
                pass
            except (OSError, ScratchHoldsUnreadable) as error:
                return KeptFolder(path, str(error))
            return RemovedCopy()
        scope = BusyMarkScope()
        lock = judgment_lock(path, scope=scope) if dry_run else removal_lock(path)
        idle = locks.enter_context(lock)
        if not idle:
            pending = pending_removal(path, scratch_settings=scratch_settings)
            if isinstance(pending, copy_record.Registered) and path.is_dir():
                recovery = recovery_state(path, pending, scope, scratch_settings=scratch_settings)
                if isinstance(recovery, KeptFolder):
                    return recovery
            git = git_directory(path)
            if isinstance(git, UnreadableGitDirectory):
                return KeptFolder(path, git.reason)
            if isinstance(git, git_files.MissingReferencedGitDirectory):
                return KeptFolder(path, f"ambiguous source: Git metadata is incomplete in {path}")
            if isinstance(request, TaskRemoval) and not dry_run and path.is_dir() and isinstance(pending, copy_record.NotRegistered):
                with copy_record_lock(path, scratch_settings=scratch_settings):
                    record = task_of(path, scratch_settings=scratch_settings)
                    if (isinstance(record, copy_record.Registered) and record.task == request.task
                            and not task_has_end_evidence(record)
                            and (copy_record.copy_folder(path) == path.resolve() or record.fields.get("test_hana_clone") is True)):
                        fields = {**record.fields, "ended": request.now, "ended_task": request.task}
                        _ = update_record(path, lambda current: fields, ending_task=request.task,
                                          scratch_settings=scratch_settings)
            return KeptFolder(path, BUSY_REASON)
        publication = RemoteInspection.LOCAL_ONLY if dry_run else RemovalAdvertisements(path, scratch_settings)
        present = os.path.lexists(path)
        record_lock = copy_record_lock(path, scratch_settings=scratch_settings) if path.is_dir() and not path.is_symlink() else nullcontext()
        with record_lock, ExitStack() as descendants:
            pending = pending_removal(path, scratch_settings=scratch_settings)
            if isinstance(pending, copy_record.UnreadableRecord):
                return KeptFolder(path, pending.reason)
            record = task_of(path, scratch_settings=scratch_settings)
            if isinstance(record, copy_record.UnreadableRecord):
                return KeptFolder(path, record.reason)
            selected = isinstance(request, SelectedCopyRemoval)
            if isinstance(request, SelectedCopyRemoval):
                if not isinstance(record, copy_record.Registered) or record.fields != request.expected.fields:
                    raise ScratchError(f"{path}: owner, task or copy identity changed; retry")
                if present and ("copy_device" in record.fields or "copy_inode" in record.fields):
                    directory = path.stat()
                    if (record.fields.get("copy_device"), record.fields.get("copy_inode")) != (directory.st_dev, directory.st_ino):
                        raise ScratchError(f"{path}: copy identity changed; retry")
            if isinstance(request, TaskRemoval):
                if isinstance(record, copy_record.Registered):
                    if request.require_task and record.task != request.task:
                        return KeptFolder(path, f"task changed to {record.task}")
                elif copy_record.copy_folder(path) == path.resolve() or not present:
                    return KeptFolder(path, "the copy has no task record")
            if not present:
                if isinstance(request, SelectedCopyRemoval):
                    request.validate()
                if not dry_run:
                    finish_removal(path, ending_task=ending_task, scratch_settings=scratch_settings)
                return RemovedCopy()
            if isinstance(request, SelectedCopyRemoval):
                request.validate()
            held = _held_removal_lock(path)
            if held is not None:
                scope = held.marks
            descendant_checkouts: tuple[Path, ...] = ()
            if path.is_dir() and not path.is_symlink():
                checked = lock_descendant_clones(path, descendants, scope, create=not dry_run,
                                                 scratch_settings=scratch_settings)
                if isinstance(checked, KeptFolder):
                    return checked
                descendant_checkouts = checked.checkouts
            recovery = RemovalRetry.RECHECK
            if isinstance(pending, copy_record.Registered):
                recovery = recovery_state(path, pending, scope, scratch_settings=scratch_settings,
                                          descendant_checkouts=descendant_checkouts)
                if isinstance(recovery, KeptFolder) and not isinstance(recovery, ChangedRemovalRef):
                    return recovery
            git = git_directory(path)
            if isinstance(git, UnreadableGitDirectory):
                return KeptFolder(path, git.reason)
            empty_run_worktree = source_deletion is SourceDeletion.RUN and empty_lost_worktree(path, git)
            task_request = TaskRemoval(request.expected.task, request.now) if isinstance(request, SelectedCopyRemoval) else request
            copy = (copy_record.copy_folder(path) == path.resolve()
                    or isinstance(record, copy_record.Registered) and record.fields.get("test_hana_clone") is True)
            if isinstance(task_request, TaskRemoval) and isinstance(record, copy_record.Registered):
                if (copy and record.task == task_request.task and not dry_run and isinstance(pending, copy_record.NotRegistered)
                        and not task_has_end_evidence(record)):
                    original_fields = record.fields
                    record = update_record(path, lambda current: {**original_fields, "ended": task_request.now,
                                           "ended_task": task_request.task}, ending_task=task_request.task,
                                           scratch_settings=scratch_settings)
            check = RemovalWorkCheck.STANDARD
            if (isinstance(task_request, TaskRemoval) and isinstance(record, copy_record.Registered) and copy
                    or isinstance(task_request, ExpiredFolderRemoval) and copy_record.copy_folder(path) == path.resolve()):
                state = record_lifetime(path, task_request.now, scratch_settings=scratch_settings)
                if isinstance(state, LandedRemoteCheckNeeded) and isinstance(publication, RemovalAdvertisements):
                    check = RemovalWorkCheck.LANDED
                elif not isinstance(state, Ended):
                    return KeptFolder(path, state.why if isinstance(state, Live) else state.reason)
            if (check is RemovalWorkCheck.LANDED or
                    ((recovery is RemovalRetry.RECHECK or isinstance(recovery, ChangedRemovalRef)) and not empty_run_worktree
                     and (not is_throwaway(path) or isinstance(pending, copy_record.Registered)))):
                work = check_removal_work(path, request, scratch_settings, publication=publication, check=check)
                if isinstance(recovery, ChangedRemovalRef):
                    # Keep the ref even when Git finds that its commit is published.
                    return work if isinstance(work, KeptFolder) else recovery
                if isinstance(work, LocalRemoteKeptFolder) or (check is RemovalWorkCheck.LANDED and isinstance(work, KeptFolder)):
                    return work
                rescue = (isinstance(pending, copy_record.NotRegistered)
                          and isinstance(record, copy_record.Registered)
                          and (selected or "crate_run" in record.fields))
                if rescue and not dry_run and isinstance(record, copy_record.Registered):
                    copies = cast(_WorkspaceRescue, cast(object, import_module("scripts.copies.copies")))
                    copies.rescue_workspace(path, record)
                if isinstance(work, KeptFolder) and not (rescue and not selected):
                    return work
            if isinstance(recovery, ChangedRemovalRef):
                return recovery
            if dry_run:
                return RemovedCopy()
            # The checkout locks are held. Refuse a busy hold lock to avoid a lock cycle.
            removal = locks.enter_context(coordinate_removals(scratch_settings=scratch_settings,
                                                             wait_for_hold_lock=False, ending_task=ending_task))
            source_is_tree = not path.is_symlink() and path.is_dir()
            if source_deletion is not SourceDeletion.SWEEP and isinstance(git, GitDirectory) and (path / ".git").is_file():
                removal.check_worktree_prune((git.checkout.common_dir,))
            saved = save_cargo_outputs(path) if source_is_tree else ()
            check_cargo_cleanup(removal, path, saved)
            if isinstance(record, copy_record.Registered):
                trust_folder = record.fields.get("claude_trust_folder")
                if isinstance(trust_folder, str):
                    from scripts.accounts import accounts

                    trust = accounts.set_claude_folder_trust(Path(trust_folder), trusted=False)
                    if isinstance(trust, accounts.FolderTrustFailed):
                        raise ScratchError("cannot clear Claude trust: " + "; ".join(trust.reasons))
            if not source_is_tree:
                if source_deletion is SourceDeletion.SWEEP:
                    cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep"))).delete_tree(str(path))
                else:
                    removal.delete(path, RemovalKind.FILE)
                return RemovedCopy()
            lease = (held.lease if isinstance(task_request, TaskRemoval) and isinstance(record, copy_record.Registered)
                     and record.fields.get("test_hana_clone") is True and held is not None
                     and isinstance(held.lease, HeldFolderRemovalLease) and held.lease.folder == path else None)
            sidecars = lease.sidecar_identities() if lease is not None else {}
            removal.check_paths(path.parent / name for name in sidecars)
            has_cargo_manifest = isinstance(pending, copy_record.Registered) and any(
                (folder / "Cargo.toml").is_file() for folder in (path, *descendant_checkouts))
            clean_cargo_outputs(path, saved)
            if isinstance(pending, copy_record.Registered) and (has_cargo_manifest or saved):
                # Cargo preparation can change the source without finding any Cargo outputs.
                recovery = recovery_state(path, pending, scope, scratch_settings=scratch_settings,
                                          descendant_checkouts=descendant_checkouts)
                if (recovery is RemovalRetry.RECHECK or isinstance(recovery, ChangedRemovalRef)) and not empty_run_worktree:
                    work = check_removal_work(path, request, scratch_settings, publication=publication, check=check)
                    if isinstance(work, KeptFolder):
                        return work
                    if recovery is RemovalRetry.RECHECK:
                        recovery = recovery_state(path, pending, scope, scratch_settings=scratch_settings,
                                                  descendant_checkouts=descendant_checkouts)
                if isinstance(recovery, KeptFolder):
                    return recovery
            if isinstance(pending, copy_record.NotRegistered):
                directory = path.stat()
                fields: dict[str, object] = dict(record.fields) if isinstance(record, copy_record.Registered) else {
                    "task": task_request.task if isinstance(task_request, TaskRemoval) else "gate:folder-removal"}
                repositories = [] if empty_run_worktree else repository_removal_evidence(path, scratch_settings=scratch_settings)
                root = path.resolve()
                entries = removal_entries(root)
                checkouts = removal_checkouts(path, descendant_checkouts)
                references = {str(entry.path.relative_to(root)): removal_ref_stat(entry)
                              for entry in removal_ref_entries(entries, checkouts)}
                fields.update(removal_version=REMOVAL_VERSION, copy_path=str(path.resolve()),
                              copy_device=directory.st_dev, copy_inode=directory.st_ino,
                              removal_repositories=repositories,
                              removal_files=sorted(removal_regular_files(root, entries)), removal_ref_files=references,
                              removal_intent_ns=time.time_ns())
                atomic_record(removal_mark(path), fields, sync=True)
            if source_deletion is SourceDeletion.SWEEP:
                cast(_SweepRemoval, cast(object, import_module("scripts.lint.tmp_sweep"))).delete_tree(str(path))
            elif source_deletion is SourceDeletion.RUN:
                cast(_RunSourceRemoval, cast(object, import_module("scripts.direct.remove_run_scratch"))).remove_tree(path)
            else:
                remove(path)
            finish_removal(path, cargo_cleaned=True)
            if lease is not None:
                lease.unlink_sidecars(sidecars)
            return RemovedCopy()


def remove_with_cargo_output(path: Path) -> None:
    """Use the coordinator for source removal and Cargo recovery."""
    remove_marked(path)


def remove(path: Path) -> None:
    """Delete the source under the coordinator's locks and leave its task record until last."""
    with coordinate_removals(wait_for_hold_lock=False) as removal:
        checkout = git_files.at(path) if not path.is_symlink() and (path / ".git").is_file() else None
        if checkout is not None:
            remove_journaled_tree(path, checkout)
        else:
            removal.delete(path, RemovalKind.TREE, repair_permissions=False, record_last=True)


def remove_marked(path: Path, *, source_deletion: SourceDeletion = SourceDeletion.STANDARD,
                  scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> None:
    """Remove one folder through the coordinator or report why the folder stays."""
    result = remove_folder(path, source_deletion=source_deletion, scratch_settings=scratch_settings)
    if isinstance(result, KeptFolder):
        raise ScratchError(f"{path}: kept: {result.reason}")


def clear_finished_copy_removals(base: Path, *, skip: Collection[Path] = (),
    source_deletion: SourceDeletion = SourceDeletion.STANDARD,
    ending_task: str | None = None,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[KeptFolder]:
    """Complete absent sources through the same coordinator used for task cleanup."""
    kept: list[KeptFolder] = []
    for path in sorted(pending_copy_paths(base)):
        if path not in skip and not os.path.lexists(path):
            try:
                result = remove_folder(path, source_deletion=source_deletion, ending_task=ending_task,
                                       scratch_settings=scratch_settings)
            except (ScratchError, OSError) as error:
                print(f"scratch: kept {path}: {error}", file=sys.stderr)
                kept.append(KeptFolder(path, str(error)))
                continue
            if isinstance(result, KeptFolder):
                print(f"scratch: kept {path}: {result.reason}", file=sys.stderr)
                kept.append(result)
    return kept


def holds_file(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return Path(os.environ.get(HOLDS_ENV) or scratch_settings.text("holds_file")).expanduser()


def holds(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[str, Hold]:
    """Read holds for reports that can omit an unreadable hold file."""
    try:
        return read_holds(scratch_settings=scratch_settings)
    except ScratchHoldsUnreadable:
        return {}


def read_holds(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> dict[str, Hold]:
    """Read all holds or raise an error. An absent file has no holds."""
    path = holds_file(scratch_settings=scratch_settings)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ScratchHoldsUnreadable(f"scratch hold file {path} must be a regular file")
            with os.fdopen(descriptor, encoding="utf-8", closefd=False) as source:
                raw = cast(object, json.load(source))
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError):
        raise ScratchHoldsUnreadable(f"cannot read scratch hold file {path}") from None
    if not isinstance(raw, dict):
        raise ScratchHoldsUnreadable(f"invalid scratch hold file {path}")
    found: dict[str, Hold] = {}
    for name, value in cast(dict[str, object], raw).items():
        if isinstance(value, dict):
            fields = cast(dict[str, object], value)
            task, made, end_at = fields.get("task"), fields.get("made"), fields.get("ended", 0.0)
            maker = fields.get("maker", "")
            if (os.path.isabs(name) and ".." not in Path(name).parts and isinstance(task, str)
                    and isinstance(maker, str) and isinstance(made, (int, float))
                    and isinstance(end_at, (int, float)) and end_at >= 0):
                found[name] = Hold(task=task, maker=maker, reason=str(fields.get("reason", "")),
                                   made=float(made), ended=float(end_at))
                continue
        raise ScratchHoldsUnreadable(f"invalid scratch hold for {name} in {path}")
    return found


def write_holds(found: dict[str, Hold], *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    path = holds_file(scratch_settings=scratch_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}")
    _ = staged.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    _ = staged.replace(path)


@contextmanager
def locked_holds(*,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    wait: bool = True,
) -> Generator[dict[str, Hold]]:
    """Keep concurrent task changes from replacing each other's folder records."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    path = holds_file(scratch_settings=scratch_settings).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        records = read_holds(scratch_settings=scratch_settings)
        token = _LOCKED_HOLD_FILE.set(_LockedHoldFile(path, records, scratch_settings))
        try:
            yield records
        finally:
            _LOCKED_HOLD_FILE.reset(token)


def hold(path: Path, task: str, reason: str, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    """Hold an existing folder for `task`; return the task."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if not reason.strip():
        raise ScratchError("a hold needs --reason")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    _ = checked_task(task, False)
    locked = _LOCKED_HOLD_FILE.get()
    access = (nullcontext(locked.records) if locked is not None
              and locked.path == holds_file(scratch_settings=scratch_settings).absolute()
              else locked_holds(scratch_settings=scratch_settings))
    with access as found:
        if path.is_symlink() or not path.is_dir():
            raise ScratchError(f"{path} is no folder")
        name = os.path.realpath(path)
        record = Hold(task=task, maker=maker(), reason=reason.strip(), made=moment, ended=0.0)
        write_holds({**found, name: record}, scratch_settings=scratch_settings)
        found[name] = record
    return task


def release(path: Path, *, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> bool:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    with locked_holds(scratch_settings=scratch_settings) as found:
        if found.pop(os.path.realpath(path), None) is None:
            return False
        write_holds(found, scratch_settings=scratch_settings)
    return True


def _git_read_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_TRACE")}
    env.update(GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    return env


def _git(repo: Path, *arguments: str, input: str | None = None,
         timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *arguments], input=input, capture_output=True, text=True,
                          errors="surrogateescape", check=False, env=_git_read_environment(), timeout=timeout)


def repos(path: Path, *, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[Path]:
    """Find repositories to the configured depth. Exclude configured folder names and symbolic links."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    return _repos(path, scratch_settings.whole("repo_depth"), scratch_settings.words("repo_skip"))


def _repos(path: Path, depth: int, skipped: tuple[str, ...]) -> list[Path]:
    if os.path.lexists(path / ".git"):
        return [path]
    if depth == 0:
        return []
    found: list[Path] = []
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name not in skipped and entry.is_dir() and not entry.is_symlink():
            found.extend(_repos(entry, depth - 1, skipped))
    return found


def verified_mirror_metadata(repo: Path) -> tuple[str, ...]:
    """Return generated filenames only when the mirror record, session, and marker agree."""
    from scripts.mac_test import floor_claims

    try:
        if any((repo / name).is_symlink() or not (repo / name).is_file()
               for name in floor_claims.MIRROR_METADATA):
            return ()
        raw = cast(object, json.loads((repo / floor_claims.MIRROR_RECORD).read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            return ()
        fields = cast(dict[str, object], raw)
        if fields.get("kind") != "remote-mirror" or any(
                not isinstance(fields.get(key), str) or not fields[key] for key in ("owner", "repo", "session")):
            return ()
        if (repo / floor_claims.MIRROR_SESSION).read_text(encoding="utf-8").strip() != fields["session"]:
            return ()
        if (repo / floor_claims.MIRROR_MARK).read_text(encoding="utf-8") != "registered remote mirror\n":
            return ()
    except (OSError, ValueError):
        return ()
    return floor_claims.MIRROR_METADATA


class RemoteInspection(Enum):
    """Select whether removal can ask a remote for its current tips."""

    LOCAL_ONLY = "local-only"
    ADVERTISE = "advertise"


@dataclass(frozen=True)
class RemotePublicationUnproven:
    """Removal needs remote evidence or the remote evidence was insufficient."""

    reason: str


@dataclass(frozen=True)
class RepositoryWorkKept:
    """The repository has work to keep, or Git cannot verify its work."""

    reason: str


@dataclass(frozen=True)
class AdvertisedIds:
    """Object IDs from the named remotes during this removal attempt."""

    ids: frozenset[str]


@dataclass
class RemovalAdvertisements:
    """Reuse only the remote evidence while the removal locks are held."""

    root: Path
    settings: conf.ConfigRead
    by_repository: dict[tuple[int, int], AdvertisedIds] = field(default_factory=dict)

    def read(self, repo: Path, remotes: list[str]) -> AdvertisedIds | RemotePublicationUnproven | LocalRemoteKeptFolder:
        try:
            directory = (repo / ".git").stat()
            identity = (directory.st_dev, directory.st_ino)
            if identity in self.by_repository:
                return self.by_repository[identity]
            result = _read_advertisements(self.root, repo, remotes, self.settings.whole("removal_remote_timeout_seconds"))
        except (OSError, ValueError, conf.Unusable) as error:
            return RemotePublicationUnproven(f"git cannot check remote publication: {error}")
        if isinstance(result, AdvertisedIds):
            self.by_repository[identity] = result
        return result


PublicationCheck = RemovalAdvertisements | Literal[RemoteInspection.LOCAL_ONLY]


def _local_remote_paths(repo: Path, url: str) -> Iterator[Path]:
    """Find paths Git can use for a local URL."""
    if url.startswith("file://"):
        _, separator, path = os.fsdecode(unquote_to_bytes(url[len("file://"):])).partition("/")
        if not separator:
            raise ValueError("file URL has no path")
        url = path if path.startswith("~") else "/" + path
    elif re.match(r"^[^/]+:", url):
        return
    location = repo.resolve() / Path(url).expanduser()
    yield location
    yield location / ".git"
    if location.name:
        suffixed = location.with_name(location.name + ".git")
        yield suffixed
        yield suffixed / ".git"


class RemoteStorage(Enum):
    """Describe whether a named remote can supply independent storage."""

    INDEPENDENT = "independent"
    ABSENT = "absent"


class _RemovalTreeDependency(ValueError):
    """A path used by a local remote lies inside the removal tree."""


def _remote_storage_path(path: Path, root: Path) -> Path:
    """Check the path and its resolved location."""
    location = Path(os.path.normpath(path))
    if location.is_relative_to(root):
        raise _RemovalTreeDependency(str(location))
    resolved = path.resolve(strict=True)
    if resolved.is_relative_to(root):
        raise _RemovalTreeDependency(str(resolved))
    return resolved


def _read_alternate_paths(objects: Path, root: Path, deadline: float) -> Iterator[Path]:
    alternates = objects / "info/alternates"
    try:
        path = _remote_storage_path(alternates, root)
    except FileNotFoundError:
        try:
            _ = alternates.lstat()
        except FileNotFoundError:
            return
        raise
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{path} is not a regular file")
        while line := source.readline(65536):
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("inspect local object storage", 0)
            if len(line) == 65536 or b"\0" in line:
                raise ValueError(f"{path} has an invalid alternate")
            if line != b"\n" and not line.startswith(b"#"):
                yield objects / os.fsdecode(line.removesuffix(b"\n"))


def _check_remote_objects(objects: Path, root: Path, deadline: float) -> None:
    pending = [objects]
    visited: set[Path] = set()
    while pending:
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("inspect local object storage", 0)
        folder = _remote_storage_path(pending.pop(), root)
        if folder in visited:
            continue
        if not folder.is_dir():
            raise ValueError(f"{folder} is not an object directory")
        visited.add(folder)
        pending.extend(_read_alternate_paths(folder, root, deadline))


def _check_local_remote(repo: Path, locations: tuple[Path, ...], root: Path, deadline: float) -> RemoteStorage:
    """Verify the Git folders and object directories that a local URL can use."""
    found = False
    checked = False
    for candidate in locations:
        try:
            resolved = _remote_storage_path(candidate, root)
        except FileNotFoundError:
            try:
                _ = candidate.lstat()
            except FileNotFoundError:
                continue
            raise
        found = True
        git_dir_result = _git(repo, "rev-parse", "--resolve-git-dir", str(candidate),
                              timeout=max(0, deadline - time.monotonic()))
        if git_dir_result.returncode:
            if resolved.is_file() or (resolved / "HEAD").exists():
                raise ValueError(f"cannot resolve the Git directory at {candidate}")
            continue
        git_dir = _remote_storage_path(repo / git_dir_result.stdout.removesuffix("\n"), root)
        directories = _git(repo, "--git-dir", str(git_dir), "rev-parse", "--path-format=absolute",
                           "--git-common-dir", "--git-path", "objects", timeout=max(0, deadline - time.monotonic()))
        paths = directories.stdout.removesuffix("\n").split("\n")
        if directories.returncode or len(paths) != 2 or any(not Path(path).is_absolute() for path in paths):
            raise ValueError(f"cannot resolve the common and object directories at {git_dir}")
        _ = _remote_storage_path(Path(paths[0]), root)
        _check_remote_objects(Path(paths[1]), root, deadline)
        checked = True
    if found and not checked:
        raise ValueError("the URL gives no readable Git repository")
    return RemoteStorage.INDEPENDENT if checked else RemoteStorage.ABSENT


def _read_advertisements(root: Path, repo: Path, remotes: list[str], timeout: int
                        ) -> AdvertisedIds | RemotePublicationUnproven | LocalRemoteKeptFolder:
    """Read all named remotes under one deadline."""
    deadline = time.monotonic() + timeout
    env = _git_read_environment()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="false", SSH_ASKPASS="false", SSH_ASKPASS_REQUIRE="never")
    removal_root = root.resolve()
    absent: set[str] = set()
    ids: set[str] = set()
    for remote in remotes:
        unverified = f"{repo}: cannot verify local remote {remote!r} storage"
        if deadline <= time.monotonic():
            return RemoteStorageUnproven(root, f"{unverified}: inspection timed out (limit {timeout} s)")
        try:
            if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
                ssh = _git(repo, "config", "--get", "core.sshCommand", timeout=max(0, deadline - time.monotonic()))
                if ssh.returncode == 1:
                    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
                elif ssh.returncode:
                    return RemotePublicationUnproven("git cannot read the configured SSH command")
            configured = _git(repo, "ls-remote", "--get-url", "--", remote,
                              timeout=max(0, deadline - time.monotonic()))
            url = configured.stdout.removesuffix("\n")
            if configured.returncode or not url:
                return RemoteStorageUnproven(root, f"{unverified}: cannot resolve its URL")
            locations = tuple(_local_remote_paths(repo, url))
            if locations:
                if any(env.get(key) for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                                                "GIT_ALTERNATE_OBJECT_DIRECTORIES")):
                    raise ValueError("Git environment overrides local repository storage")
                storage = _check_local_remote(repo, locations, removal_root, deadline)
                if storage is RemoteStorage.ABSENT:
                    absent.add(remote)
        except subprocess.TimeoutExpired:
            return RemoteStorageUnproven(root, f"{unverified}: inspection timed out (limit {timeout} s)")
        except _RemovalTreeDependency as error:
            return RemoteInsideRemovalTree(root, f"{repo}: local remote {remote!r} points inside the removal tree: {error}")
        except (OSError, RuntimeError, ValueError) as error:
            return RemoteStorageUnproven(root, f"{unverified}: {error}")
    for remote in remotes:
        failure = f"git cannot read remote {remote!r}"
        timed_out = RemotePublicationUnproven(f"{failure}: ls-remote timed out (limit {timeout} s)")
        if deadline <= time.monotonic():
            return timed_out
        query = time_limit.capture(["git", "-C", str(repo.absolute()), "ls-remote", "--", remote],
                                   deadline=deadline, env=env)
        if query.returncode == time_limit.TIMED_OUT:
            return timed_out
        if query.returncode:
            lines = query.stderr.strip().splitlines()
            detail = next((line for line in lines if line.startswith(("fatal:", "error:", "remote: error:"))),
                          lines[0] if lines else f"Git exited {query.returncode}")
            return RemotePublicationUnproven(f"{failure}: {detail}")
        if remote in absent:
            return RemoteStorageUnproven(root, f"{repo}: cannot verify local remote {remote!r} storage: URL was absent")
        for line in query.stdout.splitlines():
            oid, separator, ref = line.partition("\t")
            if not separator or not ref or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                return RemotePublicationUnproven(f"{failure}: Git advertised an invalid ref")
            ids.add(oid)
    return AdvertisedIds(frozenset(ids))


def _work_at_advertised_tips(repo: Path, advertised: AdvertisedIds) -> RemovalWorkSaved | RemotePublicationUnproven:
    """Check current refs against advertised IDs that exist locally."""
    object_ids = sorted(advertised.ids)
    objects = _git(repo, "cat-file", "--batch-check=%(objecttype)", input="".join(f"{oid}\n" for oid in object_ids))
    kinds = objects.stdout.splitlines()
    if objects.returncode or len(kinds) != len(object_ids):
        return RemotePublicationUnproven("git cannot check which advertised objects exist locally")
    excluded: list[str] = []
    missing = False
    for oid, kind in zip(object_ids, kinds, strict=True):
        if kind in ("commit", "tag", "tree", "blob"):
            excluded.append(f"^{oid}\n")
        elif kind == f"{oid} missing":
            missing = True
        else:
            return RemotePublicationUnproven("git cannot check which advertised objects exist locally")
    remaining = _git(repo, "rev-list", "-n1", "HEAD", "--branches", "--tags", "--stdin", input="".join(excluded))
    if remaining.returncode:
        return RemotePublicationUnproven("git cannot check its commits against the advertised tips")
    if not remaining.stdout.strip():
        return RemovalWorkSaved()
    reason = "it has commits that are on no remote"
    if missing:
        reason = "git cannot verify publication: advertised tips are missing locally and known tips do not cover all commits"
    return RemotePublicationUnproven(reason)


def _repo_problem(repo: Path, *, publication: PublicationCheck
                  ) -> RemovalWorkSaved | RepositoryWorkKept | RemotePublicationUnproven | LocalRemoteKeptFolder:
    """Find work that removal could lose. Check only the linked worktree's files and HEAD."""
    linked = (repo / ".git").is_file()
    status = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    if status.returncode:
        return RepositoryWorkKept(f"git cannot read it ({(status.stderr.strip().splitlines() or ['no message'])[0]})")
    changes = status.stdout.splitlines()
    record = copy_record.read_record(repo / copy_record.RECORD)
    if isinstance(record, copy_record.Registered):
        metadata: tuple[str, ...] = (copy_record.RECORD,)
        if record.fields.get("test_hana_clone") is True:
            metadata += verified_mirror_metadata(repo)
        generated = {f"?? {name}" for name in metadata}
        changes = [line for line in changes if line not in generated]
    if changes:
        return RepositoryWorkKept("it has uncommitted changes")
    head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    if head.returncode:
        return RemovalWorkSaved()
    if not linked and not _git(repo, "rev-parse", "--verify", "--quiet", "refs/stash").returncode:
        return RepositoryWorkKept("it has a stash")
    remotes: list[str] = []
    if not linked:
        configured = _git(repo, "remote")
        if configured.returncode:
            return RepositoryWorkKept("git cannot list its remotes")
        remotes = configured.stdout.splitlines()
        if not remotes:
            return RepositoryWorkKept("it has commits and no remote")
    tips = ("HEAD", "--not", "--branches", "--remotes") if linked else ("HEAD", "--branches", "--tags", "--not", "--remotes")
    unpushed = _git(repo, "rev-list", "-n1", *tips)
    if unpushed.returncode:
        return RepositoryWorkKept("git cannot list its commits")
    if not unpushed.stdout.strip():
        return RemovalWorkSaved()
    if remotes:
        if not isinstance(publication, RemovalAdvertisements):
            return RemotePublicationUnproven("a remote check is needed to verify publication")
        advertised = publication.read(repo, remotes)
        if not isinstance(advertised, AdvertisedIds):
            return advertised
        return _work_at_advertised_tips(repo, advertised)
    return RepositoryWorkKept("it has commits that are on no remote")


def repo_problem(repo: Path) -> str | None:
    """Give the reason to keep a repository, or None when its work is saved."""
    problem = _repo_problem(repo, publication=RemoteInspection.LOCAL_ONLY)
    return None if isinstance(problem, RemovalWorkSaved) else problem.reason


def _removal_work(path: Path, scratch_settings: conf.ConfigRead, *,
                  publication: PublicationCheck) -> RemovalWorkSaved | KeptFolder:
    repositories = tuple(repos(path, scratch_settings=scratch_settings))
    for repo in repositories:
        problem = _repo_problem(repo, publication=publication)
        if isinstance(problem, RemotePublicationUnproven):
            return RemoteKeptFolder(path, f"{repo}: {problem.reason}")
        if isinstance(problem, LocalRemoteKeptFolder):
            return problem
        if isinstance(problem, RepositoryWorkKept):
            return KeptFolder(path, f"{repo}: {problem.reason}")
    return RemovalWorkSaved(repositories)


def unsaved_work(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str | None:
    """Give the first reason to keep a repository in this folder."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    work = _removal_work(path, scratch_settings, publication=RemoteInspection.LOCAL_ONLY)
    return work.reason if isinstance(work, KeptFolder) else None


class TaskCleanup(list[Path]):
    """List removed paths for existing callers and report retained folders separately."""

    def __init__(self) -> None:
        super().__init__()
        self.kept: list[KeptFolder] = []


def end(task: str, now: float | None = None, *, keep: Callable[[Path], bool] = lambda _: False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> TaskCleanup:
    """Remove task folders and report each retained folder with its reason."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    _ = checked_task(task, False)
    removed = TaskCleanup()
    kept: dict[Path, str] = {}
    moment = time.time() if now is None else now

    def selected(path: Path) -> bool:
        if keep(path):
            _ = kept.setdefault(path, "selected by the keep rule")
            return False
        return True

    attempted: dict[Path, Path] = {}

    def remove_task_folder(path: Path, *, require_task: bool = True) -> bool:
        key = path.parent.resolve() / path.name
        if key in attempted:
            return attempted[key] not in kept
        attempted[key] = path
        try:
            outcome = remove_folder(path, TaskRemoval(task, moment, require_task), scratch_settings=scratch_settings)
            if isinstance(outcome, KeptFolder):
                kept[path] = outcome.reason
                return False
        except (ScratchError, OSError) as error:
            kept[path] = str(error)
            return False
        _ = kept.pop(path, None)
        removed.append(path)
        return True

    for folder in folders(scratch_settings=scratch_settings):
        if folder.task == task and selected(folder.path):
            _ = remove_task_folder(folder.path)
    with locked_holds(scratch_settings=scratch_settings) as found:
        changed = False
        for path, record in list(found.items()):
            if record["task"] != task or not selected(Path(path)):
                continue
            changed = True
            held = Path(path)
            copies = Path(os.path.realpath(copy_record.copies_root()))
            if copies.is_relative_to(held.resolve()) or held.parent == copies:
                for copy in list(copy_paths()):
                    if copy.is_relative_to(held) and selected(copy):
                        _ = remove_task_folder(copy, require_task=False)
                try:
                    unfinished = clear_finished_copy_removals(copies, skip=attempted.keys(), ending_task=task,
                                                              scratch_settings=scratch_settings)
                except (ScratchError, OSError) as error:
                    kept[held] = str(error)
                    record["ended"] = moment
                    continue
                for retained in unfinished:
                    _ = kept.setdefault(retained.path, retained.reason)
                    attempted[retained.path] = retained.path
                if any(key.is_relative_to(held) and path in kept and not os.path.lexists(key)
                       for key, path in attempted.items()):
                    kept[held] = "the container has unfinished copy removals"
                    record["ended"] = moment
                    continue
                _ = remove_empty_copy_pairs(copies, source_deletion=SourceDeletion.STANDARD,
                                            ending_task=task, scratch_settings=scratch_settings)
                # A container hold never takes recursive deletion through the copies root.
                if held != copies and held.exists():
                    outcome = remove_folder(held, EmptyFolderRemoval(), ending_task=task, scratch_settings=scratch_settings)
                    if isinstance(outcome, KeptFolder):
                        kept[held] = outcome.reason
                        record["ended"] = moment
                        continue
                if held == copies and any(copy_paths()):
                    kept[held] = "the container has retained copies"
                    record["ended"] = moment
                    continue
                del found[path]
                continue
            needs_removal = (held in attempted or os.path.lexists(held)
                             or removal_mark(held).exists() or cargo_removal_mark(held).exists())
            if needs_removal and not remove_task_folder(held):
                record["ended"] = moment
                continue
            del found[path]
        if changed:
            write_holds(found, scratch_settings=scratch_settings)
    removed.kept = [KeptFolder(path, reason) for path, reason in kept.items()]
    return removed


def end_selected_copy(path: Path, expected: copy_record.Registered, now: float,
                      *, validate: Callable[[], None]) -> bool:
    """Validate and remove the selected copy through the coordinator."""
    result = remove_folder(path, SelectedCopyRemoval(expected, now, validate))
    if isinstance(result, KeptFolder) and result.reason != BUSY_REASON:
        raise ScratchError(f"{path}: kept: {result.reason}")
    return isinstance(result, RemovedCopy)


def end_copy(path: Path, task: str, now: float, *, require_task: bool = True,
             scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> bool:
    return isinstance(remove_task_copy(path, task, now, require_task=require_task,
                                     scratch_settings=scratch_settings), RemovedCopy)


@dataclass(frozen=True)
class RemovedCopy:
    """The source and both removal marks are removed."""


def remove_task_copy(path: Path, task: str, now: float, *, require_task: bool = True,
                     scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> RemovedCopy | KeptFolder:
    return remove_folder(path, TaskRemoval(task, now, require_task), scratch_settings=scratch_settings)


def end_copy_locked(path: Path, task: str, now: float, *, require_task: bool, idle: bool,
                    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> bool:
    return isinstance(remove_task_copy_locked(path, task, now, require_task=require_task, idle=idle,
                                             scratch_settings=scratch_settings), RemovedCopy)


def remove_task_copy_locked(path: Path, task: str, now: float, *, require_task: bool, idle: bool,
                            scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> RemovedCopy | KeptFolder:
    """Keep the compatibility entry point while the coordinator owns the actual locks."""
    if not idle:
        return KeptFolder(path, BUSY_REASON)
    return remove_task_copy(path, task, now, require_task=require_task, scratch_settings=scratch_settings)


def remove_empty_copy_pairs(base: Path, *, source_deletion: SourceDeletion,
                            ending_task: str | None = None, scratch_settings: conf.ConfigRead) -> list[KeptFolder]:
    """Send empty copy pairs to the removal coordinator."""
    kept: list[KeptFolder] = []
    if not base.is_dir():
        return kept
    for pair in base.iterdir():
        try:
            if pair.is_symlink() or not pair.is_dir() or any(pair.iterdir()):
                continue
        except OSError:
            continue
        result = remove_folder(pair, EmptyFolderRemoval(), source_deletion=source_deletion,
                               ending_task=ending_task, scratch_settings=scratch_settings)
        if isinstance(result, KeptFolder):
            kept.append(result)
    return kept


def chosen_task(arguments: argparse.Namespace) -> str:
    """The --task given, the until: task of --until, or "" for the default."""
    until = cast(str, arguments.until)
    return parsed_until(until) if until else cast(str, arguments.task)


def main(argv: list[str]) -> int:
    scratch_settings = conf.read("scratch")
    parser = argparse.ArgumentParser(prog="scratch.py", description="Scratch folders that belong to a task.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "adopt", "hold"):
        command = commands.add_parser(name)
        if name != "new":
            _ = command.add_argument("path")
        ends = command.add_mutually_exclusive_group()
        _ = ends.add_argument("--task", default="")
        _ = ends.add_argument("--until", default="")
        if name == "new":
            _ = ends.add_argument("--worker", action="store_true")
        if name == "hold":
            _ = command.add_argument("--reason", required=True)
        else:
            _ = command.add_argument("--name", default="")
            _ = command.add_argument("--visible", action="store_true")
    for action in ("cargo-save", "cargo-clean"):
        _ = commands.add_parser(action).add_argument("path")
    _ = commands.add_parser("release").add_argument("path")
    _ = commands.add_parser("renew").add_argument("path")
    _ = commands.add_parser("end").add_argument("--task", required=True)
    _ = commands.add_parser("list")
    handover = commands.add_parser("hand-over")
    _ = handover.add_argument("path")
    _ = handover.add_argument("--to", required=True)
    _ = handover.add_argument("--task", required=True)
    _ = handover.add_argument("--run-dir", default="")
    _ = handover.add_argument("--land-target", default="")
    arguments = parser.parse_args(argv)
    command = cast(str, arguments.command)
    try:
        if command in ("cargo-save", "cargo-clean"):
            path = Path(cast(str, arguments.path)).resolve()
            saved = save_cargo_outputs(path)
            if command == "cargo-clean":
                clean_cargo_outputs(path, saved)
                delete_entry(cargo_removal_mark(path), RemovalKind.FILE, missing_ok=True, scratch_settings=scratch_settings)
        elif command == "new":
            if cast(bool, arguments.worker):
                if cast(bool, arguments.visible):
                    raise ScratchError("--worker does not take --visible")
                print(new_worker(cast(str, arguments.name), scratch_settings=scratch_settings))
            else:
                print(new(chosen_task(arguments), cast(str, arguments.name), cast(bool, arguments.visible), scratch_settings=scratch_settings))
        elif command == "adopt":
            print(adopt(Path(cast(str, arguments.path)), chosen_task(arguments), cast(str, arguments.name),
                        cast(bool, arguments.visible), scratch_settings=scratch_settings))
        elif command == "hold":
            path = Path(cast(str, arguments.path))
            print(f"held {os.path.realpath(path)} for {hold(path, chosen_task(arguments), cast(str, arguments.reason), scratch_settings=scratch_settings)}")
        elif command == "release":
            path = Path(cast(str, arguments.path))
            print(f"released {path}" if release(path, scratch_settings=scratch_settings) else f"{path} has no hold")
        elif command == "renew":
            renew(Path(cast(str, arguments.path)), scratch_settings=scratch_settings)
        elif command == "hand-over":
            path = Path(cast(str, arguments.path))
            hand_over(path, cast(str, arguments.to), cast(str, arguments.task),
                      run_dir=cast(str, arguments.run_dir), land_target=cast(str, arguments.land_target), scratch_settings=scratch_settings)
            print(path)
        elif command == "end":
            result = end(cast(str, arguments.task), scratch_settings=scratch_settings)
            for path in result:
                print(f"removed {path}")
            for folder in result.kept:
                print(f"scratch: kept {folder.path}: {folder.reason}", file=sys.stderr)
            if result.kept:
                return 1
        else:
            now = time.time()
            for folder in folders(scratch_settings=scratch_settings):
                print(f"{folder.path}\t{folder.task}\t{(now - folder.changed) / 3600:.1f} h since a change")
            for path, record in sorted(holds(scratch_settings=scratch_settings).items()):
                state = "ended, unsaved work" if record["ended"] else "held"
                print(f"{path}\t{record['task']}\t{state}: {record['reason']}")
    except (ScratchError, OSError) as error:
        print(f"scratch: {error}", file=sys.stderr)
        return 2
    return 0
