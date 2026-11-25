#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Manage scratch folders and record when their tasks end.

Each folder belongs to a task. The end command keeps the folder for a manual removal list.

    scratch.py new [--task <task> | --until <when>] [--name <word>]   make one; prints its path
    scratch.py new --worker [--name <word>]   make a worker folder under TMPDIR; prints its path
    scratch.py adopt <path> [--task <task> | --until <when>] [--name <word>]   move a loose folder in
    scratch.py hold <path> [--task <task> | --until <when>] --reason <why>   keep a folder where it is
    scratch.py release <path>    drop a hold
    scratch.py renew <path>      mark it changed
    scratch.py hand-over <path> --to <unit> --task <task>  replace owner and task without moving it
    scratch.py end --task <task>  record task completion and list the retained folders
    scratch.py list              each folder and hold, its task and its last change

A task, and what ends it:

  * `run:<run id>`: end_session.sh, at the run's end;
  * `phase:<unit>/<phase>`: merge_checkpoint.py, when the phase's checkpoint merges;
  * `gate:<id>`: the gate's caller, with `scratch.py end`, when the gate ends;
  * `gate:worker-<staff id>`: the worker's assignment; the worker close path ends it;
  * `landed:<unit>/<branch>`: the branch is in its recorded land target and all work is saved;
  * `row:<showrunner>-C<n>`: change_review.py verdict;
  * `until:<YYYY-MM-DDTHHMMZ>/<id>`: the deadline ends the task.
    The hourly sweep lists eligible folders for vetted manual removal.
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
The holds record lists their paths, so task end can find them.
Each folder holds copy_record.RECORD with its task, maker and
times. The hourly sweep (scripts/lint/tmp_sweep.py) checks each folder in ROOT.
After its until: deadline or 7 idle days, the sweep lists it for vetted manual removal if the other checks pass.
A row's folder waits for its verdict. A git
worktree goes through `git worktree remove`, so its source repo keeps no stale entry.
Removal writes versioned deletion intent to `.scratch-removing-<folder>.json` beside the source.
The intent preserves the task and source evidence across interruptions.
A retry compares ref file metadata with the intent and checks other file timestamps.
The retry validates the structure of Git metadata before it can resume removal.
A retry checks changed, intact sources for unsaved work.
A retry keeps changed, partly deleted sources and names the changed file.
Completion removes Cargo recovery evidence before task recovery evidence.

A hold protects a folder at its current path.
The machine's HOLDS file records the task, maker, and reason.
Task end marks the hold ended and keeps the folder.
The scratch guard hook refuses loose folders in /tmp, ~/rust, and other configured roots.
The hook is in scripts/hooks/pre-tool-use-scratch-guard.py.

Legacy copy records also name source and copy commits,
target mode, seed, borrowed git directory, owner and land target. Open phases and borrowed parents
stay; an unreadable record, phase state or land target needs repair. Shared .claude-busy locks
keep a copy while work runs or waits. Removal holds an exclusive lock through deletion.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from pathlib import Path
from threading import get_ident
from typing import Literal, Protocol, TypedDict, cast
from urllib.parse import unquote_to_bytes

from scripts.lib import build_folder_lock, build_folders, cargo_output, conf, copy_record, file_lock, git_files, removal_guard, session_folder, time_limit  # noqa: E402
from scripts.direct import recorder_state  # noqa: E402
from scripts.lib.conf import UseConfiguredValue

from scripts.lib.build_folders import (  # noqa: E402
    BuildFolderSpec, FolderRemovalLease, HeldFolderRemovalLease, RefusedRemovalLease, build_folder_path, collector_lease,
    folder_removal_lease, inherited_temporary_parent, open_busy_mark, require_no_removal_mark,
    BUSY as BUSY, REMOVAL_RECORD_PREFIX as REMOVAL_RECORD_PREFIX, REMOVAL_RECORD_SUFFIX as REMOVAL_RECORD_SUFFIX,
    removal_mark as removal_mark,
)
from scripts.lib.user_zone import user_zone  # noqa: E402

HOLDS_ENV = "SCRATCH_HOLDS"
UNTIL_FORMAT = "%Y-%m-%dT%H%MZ"
ROW_NAME = re.compile(r"[A-Za-z0-9_-]+-C[0-9]+")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
BUSY_REASON = "busy: a lease or .claude-busy lock is held"
RECORD_LOCKS: ContextVar[frozenset[tuple[int, int]]] = ContextVar("scratch_record_locks", default=frozenset())
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
    staged: build_folders.CheckedRemovalTree | None = None

    def in_this_thread(self) -> bool:
        return self.active and self.process == os.getpid() and self.thread == get_ident()


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

    with build_folders.build_folder_creation(target):
        target.mkdir(parents=True, exist_ok=True)
        tag = target / CACHE_TAG
        if not tag.exists():
            write_text_atomic(tag, CACHE_TAG_SIGNATURE + "\n" + CARGO_TAG_LINE + "\n")
        (target / USE_STAMP).touch()


def atomic_record(path: Path, fields: object) -> None:
    # A killed writer may leave its stage file. Keep copy stages in Git metadata,
    # where they cannot look like unsaved user work on the next run.
    git = path.parent / ".git"
    staging = git if path.name == copy_record.RECORD and git.is_dir() and not git.is_symlink() else path.parent
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=staging,
                                     prefix=f".{path.name}.", delete=False) as output:
        temporary = Path(output.name)
        try:
            _ = output.write(json.dumps(fields) + "\n")
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


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
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=exclude.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                _ = output.write(text + ("\n" if text and not text.endswith("\n") else "") + "\n".join(patterns) + "\n")
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, exclude)
            finally:
                temporary.unlink(missing_ok=True)


def register_copy(path: Path, *, task: str, owner_unit: str, source_path: str, source_commit: str,
                  copy_commit: str, target_mode: str, seed_source: str, borrows_from: str,
                  land_target: str = "", run_dir: str = "", phase_id: str = "", crate_run: str = "",
                  repository_identity: str = "") -> None:
    scratch_settings = conf.read("scratch")
    if not copy_record.is_legacy_copy_slot(path.resolve()) or path.is_symlink() or not path.is_dir():
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


def lock_copy_descriptor(descriptor: int, operation: int, *, wait: bool = True,
                         deadline: float | None = None) -> None:
    """Acquire a copy lock within the given deadline when it has one."""
    try:
        file_lock.flock(descriptor, operation, wait=wait, deadline=deadline)
    except file_lock.LockDeadlineExceeded as error:
        raise CopyLockTimeout("the scratch copy lock is still held") from error


@contextmanager
def copy_lock(path: Path, *, deadline: float | None = None,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[None]:
    """Hold a numbered copy lease or the busy mark of a legacy copy."""
    if build_folders.hidden_removal_path(path.resolve()):
        raise ScratchError(f"{path}: removal has already started; finish cleanup first")
    numbered = copy_record.numbered_copy_checkout(path)
    if isinstance(numbered, Path):
        with build_folders.numbered_copy_use_lease(numbered) as lease:
            if isinstance(lease, build_folders.NumberedCopyUseRefused):
                raise ScratchError(lease.reason)
            yield
        return
    with ExitStack() as claim:
        try:
            descriptors = claim.enter_context(build_folders.build_folder_use(path))
        except build_folders.BuildFolderClaimHeld as error:
            raise ScratchError(str(error)) from error
        for descriptor in descriptors:
            _ = claim.enter_context(build_folders.inherit_build_lock(descriptor))
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
            lock_copy_descriptor(lock, fcntl.LOCK_SH, deadline=deadline)
            with copy_record_lock(folder, deadline=deadline, scratch_settings=scratch_settings):
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
def copy_record_lock(path: Path, *, deadline: float | None = None,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Generator[None]:
    """Serialize record changes and deletion on the existing directory, without creating a file."""
    if copy_record.is_numbered_copy_path(path):
        raise ScratchError(f"{path}: the copy lease owns this record")
    with directory_record_lock(path, wait=True, deadline=deadline, scratch_settings=scratch_settings):
        yield


@contextmanager
def directory_record_lock(path: Path, *, wait: bool, deadline: float | None = None,
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
                    lock_copy_descriptor(descriptor, fcntl.LOCK_EX, wait=wait, deadline=deadline)
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
            if reentered:
                yield True
                return
            token = RECORD_LOCKS.set(RECORD_LOCKS.get() | {identity})
            try:
                yield True
            finally:
                RECORD_LOCKS.reset(token)
            return
        finally:
            os.close(descriptor)


def _held_removal_lock(path: Path) -> _HeldRemovalLock | None:
    return next((held for held in _REMOVAL_LOCKS.get()
                 if held.in_this_thread() and held.path == path.absolute()), None)


@contextmanager
def removal_lock(path: Path, *, reserve_name: bool = False) -> Generator[bool]:
    """Claim the lease locks and the busy mark, even when absent, and hold them through removal.
    A held lease on the folder or on any folder below it is busy, and the busy mark is then
    not created."""
    key = path.absolute()
    if _held_removal_lock(key) is not None:
        yield True
        return
    with folder_removal_lease(path, children=True, wait=not removal_mark(path).is_file(),
                              create=reserve_name) as lease:
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


class BusyMarkHeld(Exception):
    def __init__(self, lock: Path) -> None:
        super().__init__(str(lock))
        self.lock: Path = lock


@contextmanager
def busy_locks(path: Path, *, create: bool, inherited_fds: list[int] | None = None,
               wait: bool = False, scope: BusyMarkScope | BusyMarkOwnership = BusyMarkOwnership.LOCAL,
               raise_if_held: bool = False) -> Generator[bool]:
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
                if not creating:
                    try:
                        _ = mark.lstat()
                    except (FileNotFoundError, NotADirectoryError):
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
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                except BlockingIOError as error:
                    if raise_if_held:
                        raise BusyMarkHeld(mark) from error
                    raise
                if not os.path.samestat(os.fstat(fd), mark.stat(follow_symlinks=False)):
                    raise OSError(f"{mark}: the busy mark changed")
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
    validate_phase_task(checked_task(task))

    def transferred(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered):
            raise ScratchError(f"{path}: no readable copy record")
        return fields_for_task({**record.fields, "owner_unit": owner}, task, run_dir=run_dir, land_target=land_target)

    with (locked_holds(scratch_settings=scratch_settings) as found,
          copy_lock(path, scratch_settings=scratch_settings),
          copy_record_lock(path, scratch_settings=scratch_settings)):
        current = task_of(path, scratch_settings=scratch_settings)
        updated = update_record(path, transferred, scratch_settings=scratch_settings)
        held = found.get(str(path.resolve()))
        if held is not None and isinstance(current, copy_record.Registered) and (
                held["task"] == current.task or updated.fields.get("test_hana_clone") is True):
            held.update(task=task, ended=0.0)
            write_holds(found, scratch_settings=scratch_settings)


def fields_for_task(fields: dict[str, object], task: str, *, run_dir: str = "", phase_id: str = "",
                    land_target: str = "") -> dict[str, object]:
    """Replace every field whose meaning is scoped to a task, in one place."""
    match = checked_task(task)
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
                  *, removal: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> copy_record.Registered:
    """Read, change and publish a legacy task record under the directory lock."""
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
        _ = checked_task(task)
        if copy_record.copy_folder(path) == path.resolve() or "owner_unit" in fields:
            directory = path.stat()
            identity = record.fields.get("copy_id") if isinstance(record, copy_record.Registered) else ""
            fields.update(copy_id=identity if isinstance(identity, str) and identity else secrets.token_hex(16),
                          copy_device=directory.st_dev, copy_inode=directory.st_ino)
            if removal:
                fields["copy_path"] = str(path.resolve())
            else:
                exclude_copy_metadata(path)
        destination = removal_mark(path) if removal else path / copy_record.RECORD
        atomic_record(destination, fields)
        return copy_record.Registered(task, fields)


class ScratchError(Exception):
    pass


class CopyLockTimeout(ScratchError):
    pass


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


def checked_task(task: str) -> re.Match[str]:
    match = copy_record.TASK.fullmatch(task)
    if match is None:
        raise ScratchError(f"{task!r} is no task: use run:<id>, phase:<unit>/<phase>, gate:<id>, "
                           + "landed:<unit>/<branch>, row:<showrunner>-C<n> or --until <when>")
    if match["kind"] == "row" and not ROW_NAME.fullmatch(match["name"]):
        raise ScratchError(f"{task!r}: a row task is row:<showrunner>-C<n>")
    if match["kind"] == "until" and isinstance(task_end(task), Unknown):
        raise ScratchError(f"{task!r}: an until task is until:YYYY-MM-DDTHHMMZ/<id>; use --until")
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


def target(task: str, word: str, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """The new folder's path for the task; never one that exists."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    match = checked_task(task)
    validate_phase_task(match)
    if word and not WORD.fullmatch(word):
        raise ScratchError(f"{word!r}: a name is letters, digits, - and _")
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
    root = session_folder.session_root()
    return root is not None and path.parent.parent == root


def held_in_session(path: Path, task: str) -> bool:
    """Whether a new folder is in a session folder and has a task that ends it there."""
    return in_session_folder(path) and checked_task(task)["kind"] != "until"


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


def new(task: str = "", word: str = "", now: float | None = None, *,
    throwaway: bool = False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    while True:
        path = target(task, word, scratch_settings=scratch_settings)
        with build_folders.build_folder_creation(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.mkdir()
            except FileExistsError:
                if in_session_folder(path):
                    continue
                raise
            try:
                write_record(path, task, moment, throwaway=throwaway, scratch_settings=scratch_settings)
                index_in_session(path, task, scratch_settings=scratch_settings)
            except BaseException:
                with suppress(OSError):
                    _ = build_folders.remove_build_folder(path, wait=False)
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
    path = parent / "-".join((*parts, random_suffix(scratch_settings), *([word] if word else [])))
    with build_folders.build_folder_creation(path):
        parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        try:
            task = worker_task(worker.id)
            write_record(path, task, time.time(), throwaway=True, scratch_settings=scratch_settings)
            _ = hold(path, task, "worker assignment folder", scratch_settings=scratch_settings)
        except BaseException:
            _ = build_folders.remove_build_folder(path, wait=False)
            raise
    return path


def adopt(source: Path, task: str, word: str = "", now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Path:
    """Move a loose folder into a scratch root and record its task."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if source.is_symlink() or not source.is_dir():
        raise ScratchError(f"{source} is no folder")
    if protected_build_path(source) or os.path.lexists(source / ".git"):
        raise ScratchError(f"{source}: use worktrees.py make <repo> <unit> <short-name> for editable work")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    path = target(task, word, scratch_settings=scratch_settings)
    with build_folders.build_folder_creation(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.move(str(source), str(path))
        write_record(path, task, moment, scratch_settings=scratch_settings)
        index_in_session(path, task, scratch_settings=scratch_settings)
    return path


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


class RemovalIntentAccess(Enum):
    """Select whether a stale removal intent may be cleared."""

    RECOVER = "recover"
    READ_ONLY = "read only"


def pending_removal(path: Path, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    intent: RemovalIntentAccess = RemovalIntentAccess.RECOVER,
) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
    """Read deletion intent and check the identity of its source."""
    record = checked_removal(path)
    if not isinstance(record, StaleRemoval):
        return record
    if intent is RemovalIntentAccess.READ_ONLY:
        return copy_record.UnreadableRecord(f"{path}: the saved removal intent belongs to an earlier folder")
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
                finish_removal(path, source_replaced=True)
            except ScratchError as error:
                return copy_record.UnreadableRecord(str(error))
        else:
            current = copy_record.read_record(removal_mark(path))
            if isinstance(current, copy_record.Registered) and "removal_stage" in current.fields:
                return copy_record.UnreadableRecord(f"{path}: previous copy needs staging recovery before path reuse")
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
    intent: RemovalIntentAccess = RemovalIntentAccess.RECOVER,
) -> copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord:
    """Read folder task evidence; plain files have no task record."""
    if os.path.isfile(path) or copy_record.is_numbered_copy_path(path):
        return copy_record.NotRegistered()
    record = copy_record.read_record(path / copy_record.RECORD)
    return pending_removal(path, scratch_settings=scratch_settings, intent=intent) if isinstance(record, copy_record.NotRegistered) else record


def is_throwaway(path: Path, *, intent: RemovalIntentAccess = RemovalIntentAccess.RECOVER) -> bool:
    """Identify a folder that code creates without edits from a person."""
    record = task_of(path, intent=intent)
    return isinstance(record, copy_record.Registered) and record.fields.get("throwaway") is True


def protected_build_path(path: Path) -> bool:
    """Keep managed build and source paths outside legacy scratch removal."""
    try:
        locations = (path.parent.resolve() / path.name, path.resolve())
    except (OSError, RuntimeError):
        return True
    build = copy_record.build_root()
    workspace = copy_record.worktree_root()
    for location in locations:
        if location.is_relative_to(build):
            scratch_root = copy_record.root().resolve()
            if not (scratch_root != build and location.is_relative_to(scratch_root)
                    or location.is_relative_to(build / "session")):
                return True
        if location.is_relative_to(workspace):
            parts = location.relative_to(workspace).parts
            if len(parts) < 2 or copy_record.copy_folder(workspace.joinpath(*parts[:2])) is None:
                return True
    return False


def is_managed_worktree(path: Path) -> bool:
    """Check for a checkout path in the configured source layout."""
    root = copy_record.worktree_root()
    return path.is_relative_to(root) and len(path.relative_to(root).parts) == 3


def _raise_copy_scan_error(_path: Path, error: OSError) -> None:
    raise error


def _legacy_copy_entries(folder: Path, *,
                         unreadable: Callable[[Path, OSError], None] = _raise_copy_scan_error) -> Iterator[Path]:
    """Read a legacy copy folder. Let its caller decide how to handle access errors."""
    try:
        yield from folder.iterdir()
    except FileNotFoundError:
        return
    except OSError as error:
        unreadable(folder, error)


class LegacyPairScan(Enum):
    CLEANUP = "find legacy copy records"
    BORROWERS = "find legacy copy records and possible Git borrowers"


GIT_BORROWER_ENTRIES = (".git", "HEAD", "objects", "commondir")


def _entry_exists(path: Path) -> bool:
    """Check the entry without following a symbolic link."""
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return False
    return True


def _legacy_pair_entry(entry: Path, scan: LegacyPairScan) -> bool:
    try:
        mode = entry.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        return removal_target(entry) is not None
    return (_entry_exists(entry / copy_record.RECORD)
            or scan is LegacyPairScan.BORROWERS
            and any(_entry_exists(entry / name) for name in GIT_BORROWER_ENTRIES))


def legacy_copy_pairs(base: Path, *, scan: LegacyPairScan = LegacyPairScan.CLEANUP,
                      unreadable: Callable[[Path, OSError], None] = _raise_copy_scan_error) -> Iterator[Path]:
    """Find legacy copy pairs in the shared or dedicated root."""
    try:
        if not stat.S_ISDIR(base.lstat().st_mode) or base.resolve().is_relative_to(copy_record.build_root()):
            return
    except FileNotFoundError:
        return
    except OSError as error:
        unreadable(base, error)
        return
    shared_source_root = base == copy_record.worktree_root()
    for pair in _legacy_copy_entries(base, unreadable=unreadable):
        try:
            if not stat.S_ISDIR(pair.lstat().st_mode):
                continue
            if shared_source_root and not any(_legacy_pair_entry(entry, scan)
                    for entry in _legacy_copy_entries(pair, unreadable=unreadable)):
                continue
        except FileNotFoundError:
            continue
        except OSError as error:
            unreadable(pair, error)
            continue
        yield pair


def pending_copy_paths(base: Path, *,
                       unreadable: Callable[[Path, OSError], None] = _raise_copy_scan_error) -> Iterator[Path]:
    for pair in legacy_copy_pairs(base, unreadable=unreadable):
        try:
            yield from marked_folders(_legacy_copy_entries(pair, unreadable=unreadable))
        except OSError as error:
            unreadable(pair, error)


def copy_paths(*, scan: LegacyPairScan = LegacyPairScan.CLEANUP) -> Iterator[Path]:
    """Each pair/copy without following links. An unreadable registry fails closed."""
    base = copy_record.copies_root()
    for pair in legacy_copy_pairs(base, scan=scan):
        for path in _legacy_copy_entries(pair):
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode):
                yield path


def registered_copy_paths() -> Iterator[Path]:
    """List copies that can have legacy borrow records."""
    paths = set(copy_paths(scan=LegacyPairScan.BORROWERS))
    for held in holds():
        path = Path(held)
        record = task_of(path)
        if (isinstance(record, copy_record.UnreadableRecord)
                or isinstance(record, copy_record.Registered) and "borrows_from" in record.fields):
            paths.add(path)
    yield from sorted(paths)


def numbered_source_lifetime(path: Path) -> Live | Ended | Unknown:
    """Check whether removing this folder would remove a numbered copy's Git source."""
    deadline = time.monotonic() + build_folders.repository_git_timeout()
    index = BorrowerIndex(tuple(_numbered_borrowers({}, deadline)), "")
    return index.lifetime(path, object_roots=(path,))


def _borrower_git_paths(path: Path, registry: Path, on_link: Callable[[Path], None]) -> tuple[Path, Path] | None:
    """Locate the Git directory and its common object store."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as descriptors:
        if path.parent.parent == registry:
            parent = os.open(path.parent, flags)
            _ = descriptors.callback(os.close, parent)
            candidate = os.open(path.name, flags, dir_fd=parent)
        else:
            candidate = os.open(path, flags)
        _ = descriptors.callback(os.close, candidate)
        for name in GIT_BORROWER_ENTRIES:
            try:
                _ = os.stat(name, dir_fd=candidate, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if name == ".git":
                checkout = git_files.at(path, on_link=on_link)
                return checkout.git_dir, checkout.common_dir / "objects"
            return path.resolve(), git_files.objects_in_git_directory(path, on_link=on_link)
    return None


def _borrowable_objects(roots: tuple[Path, ...], *, check_descendants: bool) -> bool:
    """Check whether an unreadable borrower could name objects in these folders."""
    def unreadable(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    for root in roots:
        if ((root / ".git/objects").is_dir() or (root / "objects").is_dir()
                or root.name == "objects" and root.is_dir()):
            return True
        if check_descendants:
            for _, folders, _ in os.walk(root, onerror=unreadable, followlinks=False):
                if "objects" in folders:
                    return True
    return False


@dataclass(frozen=True)
class _Borrower:
    path: Path
    stores: frozenset[Path]
    files: frozenset[Path]
    unreadable: str
    git_links: frozenset[Path] = frozenset()
    numbered: bool = False
    git_directory: Path | None = None


def _numbered_borrowers(cache: dict[Path, git_files.AlternateEvidence], deadline: float) -> Iterator[_Borrower]:
    """Read the Git paths and object stores that numbered copies use."""
    graphs: dict[Path, tuple[set[Path], set[Path], str]] = {}
    for record in build_folders.numbered_copy_records():
        if isinstance(record, build_folders.UnreadableNumberedCopySource):
            reason = f"{record.path}: cannot check numbered copy source: {record.reason}"
            yield _Borrower(record.path, frozenset(), frozenset(), reason, numbered=True)
            continue
        copy = record.copy if isinstance(record, build_folders.NumberedCopyRemovalPending) else record
        stores = {copy.source, copy.git_directory, copy.common_directory}
        objects = copy.common_directory / "objects"
        graph = graphs.get(objects)
        if graph is None:
            graph = graphs[objects] = git_files.object_stores(objects, cache, deadline)
        alternates, files, unreadable = graph
        links: set[Path] = set()
        try:
            _ = git_files.at(copy.source, on_link=links.add)
            if isinstance(record, build_folders.NumberedCopySource):
                _ = git_files.at(copy.path, on_link=links.add)
        except (OSError, ValueError, RuntimeError) as error:
            unreadable = unreadable or str(error)
        reason = f"{copy.path}: cannot check numbered copy source: {unreadable}" if unreadable else ""
        yield _Borrower(copy.path, frozenset(stores | alternates), frozenset(files), reason,
                        git_links=frozenset(links), numbered=True)


@dataclass(frozen=True)
class BorrowerIndex:
    """The object stores and unreadable evidence from one borrower scan."""

    borrowers: tuple[_Borrower, ...]
    unreadable: str

    def lifetime(self, path: Path, *, object_roots: tuple[Path, ...] = ()) -> Live | Ended | Unknown:
        """Check whether the scan found a reference to the folder's Git objects."""
        root = path.resolve()
        roots = tuple(folder.resolve() for folder in object_roots) or (root,)
        exact = (root / ".git/objects", root / "objects")
        unreadable = self.unreadable
        unreadable_numbered = ""
        for borrower in self.borrowers:
            if any(borrower.path.is_relative_to(folder) for folder in roots):
                continue
            if any(link.is_relative_to(folder) for link in borrower.git_links for folder in roots):
                return Live(f"borrowed: {borrower.path} still uses a Git directory link in {path}")
            matches = (any(store.is_relative_to(folder) for store in borrower.stores for folder in roots)
                       if object_roots else any(store in borrower.stores for store in exact))
            if matches:
                reason = "its Git source" if borrower.numbered else "its git objects"
                return Live(f"borrowed: {borrower.path} still uses {reason}")
            if borrower.git_directory is not None and any(borrower.git_directory.is_relative_to(folder) for folder in roots):
                return Live(f"borrowed: {borrower.path} still uses a Git directory in {path}")
            if any(file.is_relative_to(folder) for file in borrower.files for folder in roots):
                return Live(f"borrowed: {borrower.path} still uses an alternates file in {path}")
            if borrower.numbered:
                unreadable_numbered = unreadable_numbered or borrower.unreadable
            else:
                unreadable = unreadable or borrower.unreadable
        if unreadable_numbered:
            return Unknown(unreadable_numbered)
        if unreadable:
            try:
                if not _borrowable_objects(roots, check_descendants=bool(object_roots)):
                    return Ended("the folder has no Git objects")
            except OSError:
                pass
            return Unknown(f"cannot rule out a borrower of Git objects in {path}: {unreadable}")
        return Ended("no copy borrows from it")


def _removed_default_store(path: Path, objects: Path) -> bool:
    """Check that a missing local store belongs to an idle checkout with a removal record."""
    if objects != path / ".git/objects":
        return False
    with suppress(FileNotFoundError):
        _ = objects.lstat()
        return False
    with collector_lease(path, wait=False) as free, busy_locks(path, create=False) as idle:
        if not free or not idle:
            return False
        with suppress(FileNotFoundError):
            _ = objects.lstat()
            return False
        removal = checked_removal(path)
        return isinstance(removal, copy_record.Registered) and (
            removal.fields.get("removal_version") == REMOVAL_VERSION or copy_record.copy_folder(path) == path)


def borrower_index(*, scratch_settings: conf.ConfigRead,
                   intent: RemovalIntentAccess = RemovalIntentAccess.READ_ONLY,
                   exclude: tuple[Path, ...] = (), include_numbered: bool = False) -> BorrowerIndex:
    """Read borrower records and their object stores."""
    registry = copy_record.copies_root()
    borrowers: list[_Borrower] = []
    cache: dict[Path, git_files.AlternateEvidence] = {}
    deadline = time.monotonic() + build_folders.repository_git_timeout()

    def inspect(child: Path) -> _Borrower:
        unreadable = ""
        stores: set[Path] = set()
        files: set[Path] = set()
        links: set[Path] = set()
        git_directory: Path | None = None
        try:
            try:
                mode = child.lstat().st_mode
            except FileNotFoundError:
                return _Borrower(child, frozenset(), frozenset(), "")
            if stat.S_ISLNK(mode):
                return _Borrower(child, frozenset(), frozenset(), f"{child}: cannot check borrowed objects through a link")
            if not stat.S_ISDIR(mode):
                return _Borrower(child, frozenset(), frozenset(), "")
            record = task_of(child, scratch_settings=scratch_settings, intent=intent)
            if isinstance(record, copy_record.UnreadableRecord):
                unreadable = record.reason
            if isinstance(record, copy_record.Registered):
                borrowed = record.fields.get("borrows_from", "")
                if not isinstance(borrowed, str) or borrowed and not Path(borrowed).is_absolute():
                    unreadable = f"repair record {child / copy_record.RECORD}: invalid borrows_from"
                elif borrowed:
                    store = Path(borrowed)
                    if store.name != "objects":
                        store /= "objects"
                    stores.add(Path(os.path.normpath(store)))
                    stores.add(store.resolve())
            git_paths = _borrower_git_paths(child, registry, links.add)
            if git_paths is not None:
                git_directory, objects = git_paths
                if git_directory != objects.parent or not _removed_default_store(child, objects):
                    alternates, files, reason = git_files.object_stores(objects, cache, deadline)
                    stores.update(alternates)
                    if reason:
                        unreadable = unreadable or f"{child}: {reason}"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            unreadable = unreadable or f"{child}: {error}"
        return _Borrower(child, frozenset(stores), frozenset(files), unreadable,
                         git_links=frozenset(links), git_directory=git_directory)

    unreadable = ""
    try:
        paths = set(copy_paths(scan=LegacyPairScan.BORROWERS))
        paths.update(path for path in _legacy_copy_entries(copy_record.root().resolve())
                     if _legacy_pair_entry(path, LegacyPairScan.BORROWERS))
        paths.update(Path(held) for held in holds(scratch_settings=scratch_settings))
        for path in sorted(paths):
            if any(path.is_relative_to(folder) for folder in exclude):
                continue
            evidence = inspect(path)
            if evidence.stores or evidence.git_links or evidence.git_directory is not None or evidence.unreadable:
                borrowers.append(evidence)
    except (OSError, ValueError, RuntimeError) as error:
        unreadable = f"repair copies registry {registry}: {error}"
    if include_numbered:
        borrowers.extend(_numbered_borrowers(cache, deadline))
    return BorrowerIndex(tuple(borrowers), unreadable)


def build_folder_borrowers() -> BorrowerIndex:
    """Read the scratch and numbered copies that can borrow from build folders."""
    return borrower_index(scratch_settings=conf.read("scratch"), include_numbered=True)


def build_folder_has_borrowers(path: Path) -> bool:
    """Keep the folder when the scan finds a borrower or cannot rule one out."""
    return not isinstance(build_folder_borrowers().lifetime(path, object_roots=(path,)), Ended)


def borrow_lifetime(path: Path, *, scratch_settings: conf.ConfigRead,
                    intent: RemovalIntentAccess = RemovalIntentAccess.RECOVER,
                    object_roots: tuple[Path, ...] = ()) -> Live | Ended | Unknown:
    """Check for references to the Git objects that the removal would delete."""
    roots = tuple(folder.resolve() for folder in object_roots or (path,))
    index = borrower_index(scratch_settings=scratch_settings, intent=intent, exclude=roots)
    return index.lifetime(path, object_roots=object_roots)


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
    intent: RemovalIntentAccess = RemovalIntentAccess.RECOVER,
) -> Live | Ended | Unknown:
    """Read end evidence while the caller holds any required removal lock."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    removal = pending_removal(path, scratch_settings=scratch_settings, intent=intent)
    if isinstance(removal, copy_record.UnreadableRecord):
        return Unknown(removal.reason)
    record = task_of(path, scratch_settings=scratch_settings, intent=intent)
    if isinstance(record, copy_record.UnreadableRecord):
        return Unknown(record.reason)
    if isinstance(record, copy_record.NotRegistered):
        return Unknown(f"repair record {path / copy_record.RECORD}: copy is not registered")
    protection = borrow_lifetime(path, scratch_settings=scratch_settings, intent=intent)
    if isinstance(removal, copy_record.Registered):
        return Ended(f"deletion intent for task {removal.task}") if isinstance(protection, Ended) else protection
    if record.task.startswith("phase:"):
        run, phase = record.fields.get("run_dir"), record.fields.get("phase_id")
        if not isinstance(run, str) or not run or not isinstance(phase, str) or not phase:
            return Unknown(f"repair record {path / copy_record.RECORD}: phase needs run_dir and phase_id")
        progress = Path(run) / recorder_state.STATE_FILENAME
        try:
            state = recorder_state.read_state(progress)
            current = state.get("phase")
            opened = state.get("open_phases", [])
            if (current is not None and not isinstance(current, dict)) or not isinstance(opened, list):
                raise ValueError("invalid phase state")
            for item in cast(list[object], opened):
                if not isinstance(item, dict):
                    raise ValueError("invalid phase record")
                if cast(dict[str, object], item).get("id") == phase:
                    return Live(f"open phase: {phase} in {run}")
            if isinstance(current, dict):
                fields = cast(dict[str, object], current)
                if fields.get("id") == phase:
                    if fields.get("status") not in ("completed", "stopped", "error", "finished", "complete", "ended"):
                        raise ValueError("unreadable phase status")
                    return Ended(f"phase {phase} ended in {progress}") if isinstance(protection, Ended) else protection
            if record.task == ending_task or task_has_end_evidence(record):
                return Ended(f"task {record.task} ended; phase is no longer open in {progress}") if isinstance(protection, Ended) else protection
            raise ValueError(f"phase {phase} has no end record")
        except recorder_state.OldRecorderState as error:
            return Unknown(str(error))
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
    intent: RemovalIntentAccess = RemovalIntentAccess.RECOVER,
) -> Live | Ended | Unknown:
    with judgment_lock(path) as idle:
        if not idle:
            return Live(BUSY_REASON)
    return record_lifetime(path, time.time() if now is None else now, scratch_settings=scratch_settings, intent=intent)


def folders(*, scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[Folder]:
    """Read scratch roots and the recorded paths of worker folders."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    candidates: dict[Path, None] = {}
    try:
        entries = sorted(copy_record.root().iterdir())
    except OSError:
        entries = []
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
    found = [SavedCargoOutput(roots, cargo_checkout_peers(roots.workspace_root),
                             ((checkout / "target").resolve(),) if (checkout / "target").is_symlink() else (),
                             time.time())
             for checkout, roots in cargo_workspace_outputs(path) if roots.workspace_root.is_relative_to(path)]
    if found:
        atomic_record(mark, [{"workspace_root": str(item.roots.workspace_root),
                             "target_directory": str(item.roots.target_directory),
                             "build_directory": str(item.roots.build_directory),
                             "other_checkouts": [str(peer) for peer in item.other_checkouts],
                             "shared_output_roots": [str(root) for root in item.shared_output_roots],
                             "saved_at": item.saved_at} for item in found])
    return tuple(found)


def cargo_workspace_outputs(path: Path) -> Iterator[tuple[Path, cargo_output.CargoOutputRoots]]:
    """Read Cargo output paths from checkouts inside the source folder."""
    found: set[cargo_output.CargoOutputRoots] = set()
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
        if roots not in found:
            found.add(roots)
            yield checkout, roots


def report_retained_cargo_outputs(path: Path) -> bool:
    """Report external Cargo folders and return whether Cargo reported any output paths."""
    root = path.resolve()
    seen = False
    kept: set[Path] = set()
    for _, roots in cargo_workspace_outputs(root):
        seen = True
        for output in (roots.target_directory, roots.build_directory):
            if not output.is_relative_to(root) and output.is_dir():
                kept.add(output)
    for output in sorted(kept):
        report_retained_task_folder(output, "Cargo output is outside the crate workspace")
    return seen


def clean_cargo_outputs(path: Path, saved: tuple[SavedCargoOutput, ...], *,
                        source: removal_guard.Identity | None = None) -> None:
    """List external outputs for review without running a deletion process."""
    if source is not None and removal_guard.identity(path.lstat()) != source:
        raise CargoOutputKept(f"{path}: source identity changed")
    plan = removal_guard.RemovalList(removal_guard.Tool.CARGO)
    for item in saved:
        folder = item.roots.build_directory
        if folder.is_relative_to(path) or item.borrows_build_directory() or not folder.exists():
            continue
        _ = plan.add_manual(removal_guard.Tool.CARGO, folder, path, 0, "external Cargo output")
    if source is not None and removal_guard.identity(path.lstat()) != source:
        raise CargoOutputKept(f"{path}: source identity changed")
    if plan.rows:
        plan.report()


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
class ExpiredFolderRemoval:
    """Recheck an expired copy before the sweep lists it for vetted manual removal."""
    now: float


class SourceDeletion(Enum):
    """Select the source deletion method and its scope."""
    STANDARD = "standard"
    SWEEP = "sweep"
    RUN = "run"
    CRATE_WORKSPACE_ONLY = "crate_workspace_only"


class _GitWorkAssessment(Protocol):
    """Read the sweep's saved-work assessment without importing its dependency graph."""
    @property
    def name(self) -> str: ...
    @property
    def value(self) -> str: ...


class _SweepRemoval(Protocol):
    """Use the sweep's saved-work check."""
    def git_work(self, path: Path, *, verified_repos: tuple[Path, ...]) -> _GitWorkAssessment: ...


class _WorkspaceRescue(Protocol):
    """Save workspace work through its existing owner before the coordinator deletes source."""
    def rescue_workspace(self, tree: Path, record: copy_record.Registered) -> None: ...


class _RunSourceRemoval(Protocol):
    """Preserve the run cleanup's Git timeout and registration retry."""
    def git(self, common: Path, *arguments: str) -> bool: ...


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


@dataclass(frozen=True)
class WorktreeRemoval:
    """Identify the Git registration of the source to remove."""

    git_dir: Path
    device: int
    inode: int
    run_cleanup: bool

    @classmethod
    def read(cls, fields: dict[str, object]) -> WorktreeRemoval | None:
        if "worktree_removal" not in fields:
            return None
        value = fields["worktree_removal"]
        if isinstance(value, dict):
            saved = cast(dict[str, object], value)
            name, device, inode, run_cleanup = (saved.get(key) for key in ("git_dir", "device", "inode", "run_cleanup"))
            if isinstance(name, str) and type(device) is int and type(inode) is int and isinstance(run_cleanup, bool):
                git_dir = Path(name)
                if git_dir.is_absolute() and ".." not in git_dir.parts and git_dir.parent.name == "worktrees":
                    return cls(git_dir, device, inode, run_cleanup)
        raise ScratchError("invalid Git registration in the removal intent")

    def fields(self) -> dict[str, object]:
        return {"git_dir": str(self.git_dir), "device": self.device, "inode": self.inode,
                "run_cleanup": self.run_cleanup}

    def exists(self) -> bool:
        try:
            current = self.git_dir.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (self.device, self.inode):
            raise ScratchError(f"{self.git_dir}: the saved Git registration changed")
        return True

    def remove(self, path: Path, parent: int) -> None:
        if not self.exists():
            return
        if not build_folder_lock.parent_current(path, parent) or os.path.lexists(path):
            raise ScratchError(f"{path}: the original path is not free for Git registration cleanup")
        common = self.git_dir.parent.parent
        if self.run_cleanup:
            run = cast(_RunSourceRemoval, cast(object, import_module("scripts.direct.remove_run_scratch")))
            if not run.git(common, "remove", "--force", "--force", str(path)):
                _ = run.git(common, "prune")
        else:
            _remove_worktree_registration(path, common)
        if self.exists():
            raise ScratchError(f"{path}: the Git registration could not be removed")


def finish_worktree_removal(path: Path, registration: WorktreeRemoval, *, source_replaced: bool) -> None:
    if not registration.exists():
        return
    if source_replaced:
        raise ScratchError(f"{path}: previous copy needs Git registration cleanup before path reuse")
    held = _held_removal_lock(path)
    if held is not None and isinstance(held.lease, HeldFolderRemovalLease):
        if not build_folder_lock.lock_current(path, held.lease.directory, held.lease.descriptor):
            raise ScratchError(f"{path}: the source lease changed")
        registration.remove(path, held.lease.directory)
        return
    with folder_removal_lease(path, wait=False, create=True, transient_lock=True) as lease:
        if not isinstance(lease, HeldFolderRemovalLease):
            raise ScratchError(f"{path}: the source lease is unavailable for Git registration cleanup")
        registration.remove(path, lease.directory)


def finish_removal(path: Path, *, cargo_cleaned: bool = False, source_replaced: bool = False) -> None:
    """Finish staged source, Git and Cargo cleanup before clearing the intent."""
    if not source_replaced and os.path.lexists(path):
        raise ScratchError(f"{path}: source still exists; removal completion refused")
    pending = copy_record.read_record(removal_mark(path))
    if isinstance(pending, copy_record.UnreadableRecord):
        raise ScratchError(pending.reason)
    if isinstance(pending, copy_record.Registered):
        try:
            stage = build_folders.StagedRemoval.read(pending.fields)
        except ValueError as error:
            raise ScratchError(f"{path}: {error}") from error
        if stage is not None and not stage.recover(path):
            raise ScratchError(f"{path}: the staged source could not be removed")
        registration = WorktreeRemoval.read(pending.fields)
        if registration is not None:
            finish_worktree_removal(path, registration, source_replaced=source_replaced)
    if source_replaced:
        if cargo_removal_mark(path).exists():
            raise ScratchError(f"{path}: previous copy needs Cargo recovery before path reuse")
        removal_mark(path).unlink(missing_ok=True)
        return
    if not cargo_cleaned:
        saved = save_cargo_outputs(path)
        clean_cargo_outputs(path, saved)
    cargo_removal_mark(path).unlink(missing_ok=True)
    removal_mark(path).unlink(missing_ok=True)


def remove_numbered_folder(path: Path, *, dry_run: bool) -> RemovedCopy | KeptFolder:
    """Keep a numbered checkout for manual review."""
    _ = dry_run
    copy = build_folders.numbered_copy_record(path)
    if isinstance(copy, build_folders.UnreadableNumberedCopySource):
        return KeptFolder(path, copy.reason)
    if isinstance(copy, build_folders.NoNumberedCopySource) and not os.path.lexists(path):
        return RemovedCopy()
    reason = "the numbered copy requires manual review"
    report_retained_task_folder(path, reason)
    return KeptFolder(path, reason)


def delete_sweep_tree(path: str) -> None:
    """Remove a source owned by the legacy folder audit."""
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        for _, _, _, descriptor in os.fwalk(path, follow_symlinks=False):
            os.fchmod(descriptor, 0o700)
        shutil.rmtree(path)


@dataclass(frozen=True)
class FinishAbsentRemoval:
    """Complete a saved removal only while its source is absent."""


def remove_folder(path: Path, request: TaskRemoval | SelectedCopyRemoval | FolderRemoval | ExpiredFolderRemoval | FinishAbsentRemoval = FOLDER_REMOVAL, *,
                  source_deletion: SourceDeletion = SourceDeletion.STANDARD, dry_run: bool = False,
                  scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
                  prepare_staged: Callable[[], None] | None = None,
) -> RemovedCopy | KeptFolder:
    """Own removal locks, safety checks, preparation, deletion intent, source deletion, and completion."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    path = path.absolute()
    numbered = copy_record.numbered_copy_checkout(path)
    if isinstance(numbered, Path):
        if isinstance(request, TaskRemoval):
            return KeptFolder(path, NUMBERED_COPY_HOLD_REFUSAL)
        if (source_deletion is not SourceDeletion.STANDARD or path.is_symlink()
                or numbered.resolve() != path.resolve()
                or isinstance(request, (SelectedCopyRemoval, ExpiredFolderRemoval))):
            return KeptFolder(path, "the numbered copy owner handles removal")
        return remove_numbered_folder(numbered, dry_run=dry_run)
    if protected_build_path(path):
        return KeptFolder(path, "the build folder owner handles removal")
    scope = BusyMarkScope()
    lock = judgment_lock(path, scope=scope) if dry_run else removal_lock(path)
    with lock as idle:
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
                        _ = update_record(path, lambda current: fields, scratch_settings=scratch_settings)
            return KeptFolder(path, BUSY_REASON)
        publication = RemoteInspection.LOCAL_ONLY if dry_run else RemovalAdvertisements(path, scratch_settings)
        present = os.path.lexists(path)
        record_lock = copy_record_lock(path, scratch_settings=scratch_settings) if path.is_dir() and not path.is_symlink() else nullcontext()
        with record_lock, ExitStack() as descendants:
            if isinstance(request, FinishAbsentRemoval) and os.path.lexists(path):
                return KeptFolder(path, "source exists again")
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
                if not dry_run and source_deletion is not SourceDeletion.CRATE_WORKSPACE_ONLY:
                    finish_removal(path)
                return RemovedCopy()
            if isinstance(request, FinishAbsentRemoval):
                return KeptFolder(path, "source exists again")
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
            if (isinstance(git, (GitDirectory, git_files.MissingReferencedGitDirectory))
                    or descendant_checkouts or (path / "objects").is_dir()):
                protection = numbered_source_lifetime(path)
                if not isinstance(protection, Ended):
                    return KeptFolder(path, protection.why if isinstance(protection, Live) else protection.reason)
            empty_run_worktree = source_deletion is SourceDeletion.RUN and empty_lost_worktree(path, git)
            task_request = TaskRemoval(request.expected.task, request.now) if isinstance(request, SelectedCopyRemoval) else request
            copy = (copy_record.copy_folder(path) == path.resolve()
                    or isinstance(record, copy_record.Registered) and record.fields.get("test_hana_clone") is True)
            if isinstance(task_request, TaskRemoval) and isinstance(record, copy_record.Registered):
                if (copy and record.task == task_request.task and not dry_run and isinstance(pending, copy_record.NotRegistered)
                        and not task_has_end_evidence(record)):
                    original_fields = record.fields
                    record = update_record(path, lambda current: {**original_fields, "ended": task_request.now,
                                           "ended_task": task_request.task}, scratch_settings=scratch_settings)
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
            if isinstance(record, copy_record.Registered):
                trust_folder = record.fields.get("claude_trust_folder")
                if isinstance(trust_folder, str):
                    from scripts.accounts import accounts

                    trust = accounts.set_claude_folder_trust(Path(trust_folder), trusted=False)
                    if isinstance(trust, accounts.FolderTrustFailed):
                        raise ScratchError("cannot clear Claude trust: " + "; ".join(trust.reasons))
            if path.is_symlink() or not path.is_dir():
                path.unlink()
                return RemovedCopy()
            lease = (held.lease if isinstance(task_request, TaskRemoval) and isinstance(record, copy_record.Registered)
                     and record.fields.get("test_hana_clone") is True and held is not None
                     and isinstance(held.lease, HeldFolderRemovalLease) and held.lease.folder == path else None)
            sidecars = lease.sidecar_identities() if lease is not None else {}
            has_cargo_manifest = isinstance(pending, copy_record.Registered) and any(
                (folder / "Cargo.toml").is_file() for folder in (path, *descendant_checkouts))
            source = removal_guard.identity(path.lstat())
            if source_deletion is SourceDeletion.CRATE_WORKSPACE_ONLY:
                has_cargo_output = report_retained_cargo_outputs(path)
            else:
                saved = save_cargo_outputs(path)
                if removal_guard.identity(path.lstat()) != source:
                    return KeptFolder(path, "source identity changed before Cargo output review")
                clean_cargo_outputs(path, saved)
                has_cargo_output = bool(saved)
            if isinstance(pending, copy_record.Registered) and (has_cargo_manifest or has_cargo_output):
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
                atomic_record(removal_mark(path), fields)
            else:
                fields = dict(pending.fields)
            if removal_guard.identity(path.lstat()) != source:
                return KeptFolder(path, "source identity changed before removal")
            if held is None or not isinstance(held.lease, build_folders.FreeFolderRemovalLease):
                return KeptFolder(path, BUSY_REASON)
            registration = None
            if isinstance(git, GitDirectory) and git.checkout.git_dir.parent == git.checkout.common_dir / "worktrees":
                status = git.checkout.git_dir_status
                registration = WorktreeRemoval(git.checkout.git_dir, status.st_dev, status.st_ino,
                                               source_deletion is SourceDeletion.RUN)
                fields["worktree_removal"] = registration.fields()

            def record_stage(staged: build_folders.CheckedRemovalTree) -> None:
                if not build_folder_lock.parent_current(staged.path, staged.parent):
                    raise ScratchError(f"{path}: the source parent changed")
                stage = build_folders.StagedRemoval.from_tree(staged)
                atomic_record(removal_mark(path), {**fields, **stage.fields()})

            def delete(staged: build_folders.CheckedRemovalTree) -> None:
                if not build_folder_lock.parent_current(staged.path, staged.parent):
                    raise ScratchError(f"{path}: the source parent changed")
                if prepare_staged is not None:
                    prepare_staged()
                if source_deletion in (SourceDeletion.SWEEP, SourceDeletion.RUN):
                    staged.remove(repair_access=True)
                else:
                    held.staged = staged
                    try:
                        remove(staged.path)
                    finally:
                        held.staged = None
                if registration is not None:
                    registration.remove(path, staged.parent)

            expected = path.lstat()
            if removal_guard.identity(expected) != source:
                return KeptFolder(path, "source identity changed before removal")
            if not held.lease.remove_tree(path, expected, wait=False, repair_access=True,
                                          delete=delete, before_stage=record_stage):
                return KeptFolder(path, BUSY_REASON)
            finish_removal(path, cargo_cleaned=True)
            if lease is not None:
                lease.unlink_sidecars(sidecars)
            return RemovedCopy()


def remove_with_cargo_output(path: Path, *, prepare_staged: Callable[[], None] | None = None) -> None:
    """Remove the source and its Cargo outputs. Prepare the staged tree before deletion."""
    remove_marked(path, prepare_staged=prepare_staged)


def _remove_worktree_registration(path: Path, registration: Path) -> None:
    _ = subprocess.run(["git", "--git-dir", str(registration), "worktree", "remove",
                        "--force", "--force", str(path)], capture_output=True, check=False)


def remove(source: Path) -> None:
    """Delete the checked source and leave its task record until last."""
    if build_folders.hidden_removal_path(source):
        for held in _REMOVAL_LOCKS.get():
            staged = held.staged
            if held.in_this_thread() and staged is not None and staged.path == source:
                staged.remove(repair_access=True, metadata_last=(copy_record.RECORD,))
                return
        raise ScratchError(f"{source}: the staged source has no removal lease in this thread")
    with build_folder_lock.folder_parent(source, source.parent, create=False) as parent:
        try:
            info = os.stat(source.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(source.name, dir_fd=parent)
            return
        git = git_directory(source) if (source / ".git").is_file() else NoGitDirectory()
        build_folders.CheckedRemovalTree(source, parent, info).remove(metadata_last=(copy_record.RECORD,))
        if isinstance(git, GitDirectory) and not os.path.lexists(source):
            _remove_worktree_registration(source, git.checkout.common_dir)


def remove_marked(path: Path, *, source_deletion: SourceDeletion = SourceDeletion.STANDARD,
                  scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
                  prepare_staged: Callable[[], None] | None = None) -> None:
    """Remove one folder through the coordinator or report why the folder stays."""
    result = remove_folder(path, source_deletion=source_deletion, scratch_settings=scratch_settings,
                           prepare_staged=prepare_staged)
    if isinstance(result, KeptFolder):
        raise ScratchError(f"{path}: kept: {result.reason}")


def clear_finished_copy_removals(base: Path, *, skip: Collection[Path] = (),
    unreadable: Callable[[Path, OSError], None] = _raise_copy_scan_error,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[KeptFolder]:
    """Finish legacy copy removals whose checkout is absent."""
    kept: list[KeptFolder] = []
    for path in sorted(pending_copy_paths(base, unreadable=unreadable)):
        if path not in skip and not os.path.lexists(path):
            try:
                result = remove_folder(path, scratch_settings=scratch_settings)
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
    """Every hold on this machine, by the held folder's real path."""
    path = holds_file(scratch_settings=scratch_settings)
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    found: dict[str, Hold] = {}
    for path, value in cast(dict[str, object], raw).items():
        if isinstance(value, dict):
            fields = cast(dict[str, object], value)
            task, made, end_at = fields.get("task"), fields.get("made"), fields.get("ended", 0.0)
            if isinstance(task, str) and isinstance(made, (int, float)) and isinstance(end_at, (int, float)):
                found[path] = Hold(task=task, maker=str(fields.get("maker", "")), reason=str(fields.get("reason", "")),
                                   made=float(made), ended=float(end_at))
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
) -> Generator[dict[str, Hold]]:
    """Keep concurrent task changes from replacing each other's folder records."""
    path = holds_file(scratch_settings=scratch_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield holds(scratch_settings=scratch_settings)


NUMBERED_COPY_HOLD_REFUSAL = "the numbered copy lease owns its lifetime and does not permit task holds"


def hold(path: Path, task: str, reason: str, now: float | None = None, *,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    """Hold an existing folder for `task`; return the task."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    if copy_record.is_numbered_copy_path(path):
        raise ScratchError(f"{path}: {NUMBERED_COPY_HOLD_REFUSAL}")
    if path.is_symlink() or not path.is_dir():
        raise ScratchError(f"{path} is no folder")
    if not reason.strip():
        raise ScratchError("a hold needs --reason")
    moment = time.time() if now is None else now
    task = task_or_default(task, moment, scratch_settings=scratch_settings)
    _ = checked_task(task)
    with locked_holds(scratch_settings=scratch_settings) as found:
        found[os.path.realpath(path)] = Hold(task=task, maker=maker(), reason=reason.strip(), made=moment, ended=0.0)
        write_holds(found, scratch_settings=scratch_settings)
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


def _check_remote_objects(objects: Path, root: Path, deadline: float) -> None:
    stores, files, unreadable = git_files.object_stores(objects, {}, deadline)
    for dependency in sorted(stores | files):
        if dependency.is_relative_to(root):
            raise _RemovalTreeDependency(str(dependency))
    if unreadable:
        raise ValueError(unreadable)


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


def retained_folder_exists(path: Path) -> bool:
    """Keep a path in the report unless lstat confirms it is absent."""
    try:
        _ = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


_RETAINED_TASK_FOLDER_PREFIX = "kept for a manual removal list: "
_RETAINED_TASK_FOLDER_REASON = " (scratch, "


def retained_task_folder_notice(path: Path, reason: str) -> str:
    return f"{_RETAINED_TASK_FOLDER_PREFIX}{path}{_RETAINED_TASK_FOLDER_REASON}{reason})"


def is_retained_task_folder_notice(detail: str) -> bool:
    """Recognize the format of a saved scratch notice."""
    return (detail.startswith(_RETAINED_TASK_FOLDER_PREFIX)
            and _RETAINED_TASK_FOLDER_REASON in detail and detail.endswith(")"))


def report_retained_task_folder(path: Path, reason: str) -> None:
    print(retained_task_folder_notice(path, reason), file=sys.stderr)


def record_task_end(path: Path, task: str, moment: float, *,
                    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> None:
    """Mark the current task ended in an existing folder record."""
    def mark_ended(record: copy_record.Registered | copy_record.NotRegistered | copy_record.UnreadableRecord) -> dict[str, object]:
        if not isinstance(record, copy_record.Registered) or record.task != task:
            raise ScratchError(f"{path}: the folder task changed")
        return {**record.fields, "ended": moment, "ended_task": task}

    with copy_record_lock(path, scratch_settings=scratch_settings):
        current = copy_record.read_record(path / copy_record.RECORD)
        if not isinstance(current, copy_record.Registered) or current.task != task:
            raise ScratchError(f"{path}: the folder task changed")
        if not task_has_end_evidence(current):
            _ = update_record(path, mark_ended, scratch_settings=scratch_settings)


def end(task: str, now: float | None = None, *, keep: Callable[[Path], bool] = lambda _: False,
    scratch_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[KeptFolder]:
    """Record task completion and keep its folders for manual removal."""
    if isinstance(scratch_settings, UseConfiguredValue):
        scratch_settings = conf.read("scratch")
    _ = checked_task(task)
    kept: list[KeptFolder] = []
    moment = time.time() if now is None else now
    paths = {folder.path.resolve(): folder.path for folder in folders(scratch_settings=scratch_settings)
             if folder.task == task}
    with locked_holds(scratch_settings=scratch_settings) as found:
        task_holds = {Path(name): record for name, record in found.items() if record["task"] == task}
        for path in task_holds:
            _ = paths.setdefault(path, path)
        protected = {path for path in paths.values() if keep(path)}
        changed = False
        for path, record in task_holds.items():
            if paths[path] not in protected and not record["ended"]:
                record["ended"] = moment
                changed = True
        if changed:
            write_holds(found, scratch_settings=scratch_settings)

    for path in paths.values():
        if not retained_folder_exists(path):
            continue
        reason = "selected by the keep rule" if path in protected else f"task {task} ended"
        try:
            if path not in protected and not path.is_symlink():
                current = copy_record.read_record(path / copy_record.RECORD)
                if isinstance(current, copy_record.Registered) and current.task == task:
                    record_task_end(path, task, moment, scratch_settings=scratch_settings)
        except (ScratchError, OSError) as error:
            reason = " ".join(str(error).splitlines())
        report_retained_task_folder(path, reason)
        kept.append(KeptFolder(path, reason))
    return kept


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
            if protected_build_path(path):
                raise ScratchError(f"{path}: the build folder owner handles Cargo output")
            saved = save_cargo_outputs(path)
            if command == "cargo-clean":
                clean_cargo_outputs(path, saved)
                cargo_removal_mark(path).unlink(missing_ok=True)
        elif command == "new":
            if cast(bool, arguments.worker):
                print(new_worker(cast(str, arguments.name), scratch_settings=scratch_settings))
            else:
                print(new(chosen_task(arguments), cast(str, arguments.name), scratch_settings=scratch_settings))
        elif command == "adopt":
            print(adopt(Path(cast(str, arguments.path)), chosen_task(arguments), cast(str, arguments.name),
                        scratch_settings=scratch_settings))
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
            _ = end(cast(str, arguments.task), scratch_settings=scratch_settings)
        else:
            now = time.time()
            for folder in folders(scratch_settings=scratch_settings):
                print(f"{folder.path}\t{folder.task}\t{(now - folder.changed) / 3600:.1f} h since a change")
            for path, record in sorted(holds(scratch_settings=scratch_settings).items()):
                state = "ended" if record["ended"] else "held"
                print(f"{path}\t{record['task']}\t{state}: {record['reason']}")
    except (ScratchError, OSError) as error:
        print(f"scratch: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
