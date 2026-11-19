#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Bring the files of a lint fix pass back into the worktree that was copied.

Two fix passes run on a copy of the worktree:

1. The Mac fix pass. offload.py lists the mirror files that differ from the
   worktree, fetches them to a staging folder, and calls apply_fix.
2. The local fix pass in a repository that cargo-berth configures. verify.sh
   runs mend, clippy --fix, fmt and taplo on a private copy (`copy`), then
   brings the changes back (`return-copy`). A rewrite in place can write a path
   that another reservation holds, and berth records that as an incursion.

apply_fix writes the files only when the worktree is unchanged since the copy.
It reads `cargo-berth board --json` and writes only the paths that this
worktree's reservations hold. It reports each other fixed path and does not
write it. A path that another reservation holds is not a failure. A path that
no reservation holds fails the pass, with the cargo-berth check command.

A record in the git folder lets Verify resume an interrupted return.
Live return and recovery compare the same file versions before each write.
Each displaced file stays in its staging folder until the commit ends.
A conflict keeps the record and both versions for review.

Usage:
  fix_return.py finish --repo-root DIR
  fix_return.py copy --repo-root DIR --owner PID < cargo-metadata.json
  fix_return.py return-copy --repo-root DIR --folder DIR
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import fcntl
import filecmp
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Literal, TypedDict, cast

from scripts.lib import atomic_file, berth_board


@dataclass(frozen=True)
class Withheld:
    """A fixed path that this worktree does not hold, so the fix is not written.

    holder names the reservation that holds the path. It is empty when no
    reservation holds the path.
    """

    path: str
    holder: str


@dataclass(frozen=True)
class FixApplied:
    """The worktree received the held changed files and lost the held removed files."""

    changed: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class FixPartial:
    """The worktree received the fixes for its reservations."""

    changed: tuple[str, ...]
    removed: tuple[str, ...]
    withheld: tuple[Withheld, ...]

    def unheld(self) -> tuple[Withheld, ...]:
        """The withheld paths that no reservation holds; each one fails the pass."""
        return tuple(item for item in self.withheld if not item.holder)


@dataclass(frozen=True)
class FixNone:
    """The Mac fix pass changed no file."""


@dataclass(frozen=True)
class FixRefused:
    """The worktree changed before or during the return."""

    reason: str


@dataclass(frozen=True)
class FixFailed:
    """The changed files could not be listed, fetched or written."""

    reason: str


@dataclass(frozen=True)
class FixNotRun:
    """The run made no fix pass, or its fix pass did not pass."""


FixResult = FixApplied | FixPartial | FixNone | FixRefused | FixFailed | FixNotRun
FIX_NOT_RUN = FixNotRun()


@dataclass(frozen=True)
class MirrorChanges:
    """Repository-relative files that the fix pass changed, added or removed."""

    changed: tuple[str, ...]
    removed: tuple[str, ...]


# rsync --itemize-changes: the update type, the file type, then nine attribute letters.
ITEMIZED_LINE = re.compile(r"([<>ch.*])([fdLDS])([^ ]{9}) (.+)")
DELETED_LINE = re.compile(r"\*deleting +(.+)")


def fix_word(result: FixResult) -> str:
    if isinstance(result, FixApplied):
        return "applied"
    if isinstance(result, FixPartial):
        return "withheld" if result.unheld() else "partial"
    if isinstance(result, FixNone):
        return "none"
    if isinstance(result, FixRefused):
        return "refused"
    if isinstance(result, FixFailed):
        return "failed"
    return "not_run"


def local_return_succeeded(result: FixResult) -> bool:
    """Tell the local copy caller whether all fixes have a reservation."""
    return isinstance(result, (FixNone, FixApplied)) or (isinstance(result, FixPartial) and not result.unheld())


def withheld_lines(result: FixResult) -> list[str]:
    """One line for each fixed path that was not written."""
    if not isinstance(result, FixPartial):
        return []
    lines = [f"not written: {item.path} is held by reservation {item.holder}"
             for item in result.withheld if item.holder]
    unheld = [item.path for item in result.unheld()]
    if unheld:
        lines.append("not written: no reservation holds " + ", ".join(unheld)
                     + "; run `cargo-berth check " + " ".join(unheld) + "`, then run lint again")
    return lines


def parse_itemized(output: str) -> MirrorChanges | FixFailed:
    """Read a dry-run listing from the mirror to the worktree.

    A file counts as changed when it is new or its content differs (-c). A
    change of mode alone does not count. A directory line has no content.
    """
    changed: list[str] = []
    removed: list[str] = []
    for line in output.splitlines():
        if deleted := DELETED_LINE.fullmatch(line):
            path = deleted.group(1)
            if not path.endswith("/"):
                removed.append(path)
            continue
        itemized = ITEMIZED_LINE.fullmatch(line)
        if itemized is None:
            continue
        update, kind, attributes, path = itemized.groups()
        if kind == "d":
            continue
        if kind != "f":
            return FixFailed(f"the fix pass changed {path}, which is not a regular file")
        if update == ">" and (attributes[:2] == "++" or attributes[0] == "c" or attributes[1] == "s"):
            changed.append(path)
    return MirrorChanges(tuple(changed), tuple(removed))


def without_ignored(repo_root: Path, changes: MirrorChanges) -> MirrorChanges | FixFailed:
    """Drop the paths that git ignores, such as files the Mac build wrote."""
    paths = (*changes.changed, *changes.removed)
    if not paths:
        return changes
    try:
        listed = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "--stdin", "-z"],
            input="\0".join(paths) + "\0", capture_output=True, text=True, check=False,
        )
    except OSError as error:
        return FixFailed(f"git check-ignore: {error}")
    # check-ignore exits 1 when no path is ignored.
    if listed.returncode not in (0, 1):
        return FixFailed("git check-ignore failed: " + listed.stderr.strip())
    ignored = {path for path in listed.stdout.split("\0") if path}
    return MirrorChanges(tuple(path for path in changes.changed if path not in ignored),
                         tuple(path for path in changes.removed if path not in ignored))


def worktree_path(repo_root: Path, relative: str) -> Path:
    """Resolve a mirror path in the worktree; refuse a path that leaves it."""
    if not Path(relative).parts or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise OSError(f"{relative} is not inside the worktree")
    path = repo_root / relative
    if path.is_symlink() or not path.parent.resolve().is_relative_to(repo_root.resolve()):
        raise OSError(f"{relative} is not inside the worktree")
    return path


RECORD_NAME = "mac-fix.json"
LOCK_NAME = "mac-fix.lock"
TEMPORARY_MARK = ".mac-fix-"


class FileVersion(TypedDict):
    device: int
    inode: int
    mode: int
    modified_ns: int
    digest: str


PathVersion = FileVersion | Literal["absent"]


class StagingPath(TypedDict):
    destination: str
    folder: str


class CommitPath(StagingPath):
    original: PathVersion
    fixed: PathVersion


class StagingRecord(TypedDict):
    state: Literal["staging"]
    paths: list[StagingPath]


class CommitRecord(TypedDict):
    state: Literal["commit", "cleanup"]
    entries: list[CommitPath]


class LegacyFixRecord(TypedDict):
    state: Literal["staging", "commit"]
    renames: list[tuple[str, str]]
    removals: list[str]


FixRecord = StagingRecord | CommitRecord | LegacyFixRecord


class FixConflict(OSError):
    """A path changed during the return."""


class CommitProgress(Enum):
    PENDING = "pending"
    DISPLACED = "displaced"
    COMPLETE = "complete"


def git_folder_path(repo_root: Path, name: str) -> Path:
    """Resolve a file in the worktree's own git folder."""
    listed = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--git-path", name],
                            capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        raise OSError("git rev-parse --git-path failed: " + listed.stderr.strip())
    return repo_root / listed.stdout.strip()


@contextmanager
def record_lock(repo_root: Path) -> Generator[Path, None, None]:
    """Hold the worktree's fix lock; yield the record path."""
    lock_path = git_folder_path(repo_root, LOCK_NAME)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield git_folder_path(repo_root, RECORD_NAME)


def write_record(path: Path, record: FixRecord) -> None:
    """Replace the record in one rename, with its data and its folder on disk."""
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8") as output:
        _ = output.write(json.dumps(record))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    atomic_file.sync_folder(path.parent)


def read_record(path: Path) -> FixRecord:
    record = cast(FixRecord, json.loads(path.read_text(encoding="utf-8")))
    if record["state"] not in ("staging", "commit", "cleanup"):
        raise OSError(f"{path} has an unknown state")
    return record


def stage_file(source: Path, temporary: Path, destination: Path) -> None:
    """Stage the fix with the mode of the destination file."""
    mode = destination.stat().st_mode if destination.exists() else source.stat().st_mode
    with temporary.open("xb") as output:
        _ = output.write(source.read_bytes())
        output.flush()
        os.fchmod(output.fileno(), stat.S_IMODE(mode))
        os.fsync(output.fileno())


def file_version(path: Path) -> PathVersion:
    """Identify the file and its content across renames and hard links."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISREG(before.st_mode):
        raise FixConflict(f"{path} is not a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_ctime_ns):
            raise FixConflict(f"{path} changed while reading its version")
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_ctime_ns) != (before.st_dev, before.st_ino, before.st_ctime_ns):
            raise FixConflict(f"{path} changed while reading its version")
    return FileVersion(device=before.st_dev, inode=before.st_ino, mode=before.st_mode,
                       modified_ns=before.st_mtime_ns, digest=digest)


@dataclass(frozen=True)
class CommitFiles:
    """The live destination and its private staging folder."""

    destination: Path
    folder: Path

    @property
    def staged(self) -> Path:
        return self.folder / "fixed"

    @property
    def received(self) -> Path:
        return self.folder / "received"

    @property
    def displaced(self) -> Path:
        return self.folder / "original"

    def conflict(self) -> FixConflict:
        return FixConflict(f"{self.destination} changed during the fix return. Review the files in {self.folder}.")


def commit_files(repo_root: Path, entry: StagingPath) -> CommitFiles:
    destination = worktree_path(repo_root, entry["destination"])
    folder = worktree_path(repo_root, entry["folder"])
    if folder.parent != destination.parent or not folder.name.startswith(TEMPORARY_MARK):
        raise OSError(f"{folder} is not a fix staging folder for {destination}")
    return CommitFiles(destination, folder)


def checked_displacement(files: CommitFiles, original: PathVersion) -> PathVersion:
    """Restore a changed displaced file if its live path is empty."""
    try:
        displaced = file_version(files.displaced)
        if displaced != "absent" and displaced != original:
            raise files.conflict()
    except OSError:
        try:
            os.link(files.displaced, files.destination, follow_symlinks=False)
        except (FileExistsError, FileNotFoundError):
            pass
        else:
            atomic_file.sync_folder(files.destination.parent)
        raise
    return displaced


def commit_progress(files: CommitFiles, entry: CommitPath) -> CommitProgress:
    original = entry["original"]
    displaced = checked_displacement(files, original)
    live = file_version(files.destination)
    fixed = entry["fixed"]
    if file_version(files.staged) == fixed:
        if displaced == original and live == fixed:
            return CommitProgress.COMPLETE
        if live == original and displaced == "absent":
            return CommitProgress.PENDING
        if displaced == original:
            if live == "absent":
                return CommitProgress.DISPLACED
    raise files.conflict()


def commit_path(files: CommitFiles, entry: CommitPath) -> None:
    progress = commit_progress(files, entry)
    if progress == CommitProgress.COMPLETE:
        return
    if progress == CommitProgress.PENDING and entry["original"] != "absent":
        # Move the file before the comparison. An editor can save after the first check.
        os.replace(files.destination, files.displaced)
        atomic_file.sync_folder(files.folder)
        atomic_file.sync_folder(files.destination.parent)
        if checked_displacement(files, entry["original"]) != entry["original"]:
            raise files.conflict()
    if entry["fixed"] != "absent":
        # Publish only to an empty name. A new editor save must win.
        try:
            os.link(files.staged, files.destination)
        except FileExistsError as error:
            raise files.conflict() from error
        atomic_file.sync_folder(files.destination.parent)


def clear_record(path: Path) -> None:
    path.unlink()
    atomic_file.sync_folder(path.parent)


def clean_staging(files: CommitFiles) -> None:
    files.staged.unlink(missing_ok=True)
    files.received.unlink(missing_ok=True)
    try:
        files.folder.rmdir()
    except FileNotFoundError:
        return
    atomic_file.sync_folder(files.folder.parent)


def finish_record(repo_root: Path, path: Path) -> str:
    """Finish a record while the caller holds the fix lock."""
    if not path.exists():
        return ""
    record = read_record(path)
    if "renames" in record:
        if record["state"] == "commit":
            raise OSError(f"{path} has no expected file versions. Review the record and staged files.")
        for temporary, _ in record["renames"]:
            worktree_path(repo_root, temporary).unlink(missing_ok=True)
        clear_record(path)
        count = len(record["renames"])
        return f"removed {count} staged file(s) of a stopped Mac fix; the worktree is unchanged"
    if record["state"] == "staging":
        for entry in record["paths"]:
            clean_staging(commit_files(repo_root, entry))
        clear_record(path)
        return f"removed {len(record['paths'])} staged file(s) of a stopped Mac fix; the worktree is unchanged"

    entries: list[tuple[CommitFiles, CommitPath]] = []
    errors: list[OSError] = []
    for entry in record["entries"]:
        try:
            files = commit_files(repo_root, entry)
            if record["state"] == "commit":
                _ = commit_progress(files, entry)
            entries.append((files, entry))
        except OSError as error:
            # Check all displacements so another conflict cannot prevent restoration.
            errors.append(error)
    if errors:
        raise errors[0]

    if record["state"] == "commit":
        for files, entry in entries:
            commit_path(files, entry)
        for files, entry in entries:
            if commit_progress(files, entry) != CommitProgress.COMPLETE:
                raise files.conflict()
        write_record(path, CommitRecord(state="cleanup", entries=record["entries"]))
    for files, entry in entries:
        if checked_displacement(files, entry["original"]) != "absent":
            files.displaced.unlink()
        clean_staging(files)
    clear_record(path)
    written = sum(entry["fixed"] != "absent" for _, entry in entries)
    return f"finished a stopped Mac fix: {written} file(s) written, {len(entries) - written} removed"


def finish(repo_root: Path) -> str:
    """Complete a stopped fix return in this worktree, if one is left."""
    with record_lock(repo_root) as path:
        return finish_record(repo_root, path)


BoardReader = Callable[[Path], berth_board.Board | berth_board.BoardUnavailable]


def read_board(repo_root: Path) -> berth_board.Board | berth_board.BoardUnavailable:
    """Read the reservations. A host without cargo-berth has an unconfigured board.

    A repository with no cargo-berth ledger, such as a copies.py clone, also has
    an unconfigured board. No reservation can hold a path there, so the fix pass
    writes the worktree in place.
    """
    if shutil.which("cargo-berth") is None:
        return berth_board.Board((), configured=False)
    board = berth_board.read_board(repo_root)
    if isinstance(board, berth_board.BoardUnavailable) and board.ledger_missing:
        return berth_board.Board((), configured=False)
    return board


# cargo-berth's lifecycle stage for a reservation that protects a tip until it merges.
OUTSTANDING = "outstanding"


def in_head_history(repo_root: Path, commit: str) -> bool:
    """Whether commit is HEAD or an ancestor of HEAD; an unknown commit is not."""
    try:
        listed = subprocess.run(["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit, "HEAD"],
                                capture_output=True, check=False)
    except OSError:
        return False
    return listed.returncode == 0


def hold_filter(repo_root: Path, changes: MirrorChanges,
                board: berth_board.Board) -> tuple[MirrorChanges, tuple[Withheld, ...]]:
    """Keep the paths that this worktree holds; withhold every other path.

    A path that another worktree's blocking reservation holds is withheld with
    that reservation, unless this worktree's own reservation names the same
    file: berth then lets both hold it. A tree scope of this worktree does not
    win over another worktree's hold. An outstanding reservation whose protected tip is in this
    worktree's HEAD history holds nothing here: its work is already in this
    branch. A path that no blocking reservation holds is withheld with no
    holder. An unconfigured board keeps every path.
    """
    if not board.configured:
        return changes, ()
    root = repo_root.resolve()
    reservations = [(reservation, reservation.holder_worktree_root.resolve() == root)
                    for reservation in board.reservations()
                    if not (reservation.lifecycle == OUTSTANDING and reservation.protected_tip
                            and in_head_history(repo_root, reservation.protected_tip))]
    withheld: list[Withheld] = []

    def kept(paths: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        for path in paths:
            holders = [(reservation, own) for reservation, own in reservations if reservation.blocks(path)]
            foreign = [reservation for reservation, own in holders if not own]
            if any(own and reservation.names_file(path) for reservation, own in holders):
                result.append(path)
            elif foreign:
                withheld.append(Withheld(path, f"{foreign[0].reservation_id} ({foreign[0].holder_branch})"))
            elif holders:
                result.append(path)
            else:
                withheld.append(Withheld(path, ""))
        return tuple(result)

    return MirrorChanges(kept(changes.changed), kept(changes.removed)), tuple(withheld)


def apply_fix(repo_root: Path, staging: Path, changes: MirrorChanges,
              worktree_unchanged: Callable[[], bool], board_reader: BoardReader | None = None) -> FixResult:
    """Write the held staged files when the worktree is unchanged since the copy.

    board_reader defaults to read_board, looked up at each call.
    """
    if not worktree_unchanged():
        return FixRefused("the worktree changed after the copy")
    if not changes.changed and not changes.removed:
        return FixNone()
    board = (board_reader or read_board)(repo_root)
    if isinstance(board, berth_board.BoardUnavailable):
        return FixFailed(f"cargo-berth board: {board.reason}")
    changes, withheld = hold_filter(repo_root, changes, board)
    try:
        with record_lock(repo_root) as path:
            if path.exists():
                return FixFailed(f"review the unfinished fix return in {path}")
            token = secrets.token_hex(8)
            paths = [
                StagingPath(destination=relative, folder=str(Path(relative).with_name(
                    f"{TEMPORARY_MARK}{token}-{index}")))
                for index, relative in enumerate((*changes.changed, *changes.removed))
            ]
            originals: list[PathVersion] = [
                file_version(worktree_path(repo_root, item["destination"])) for item in paths
            ]
            for relative in changes.changed:
                source = staging / relative
                if not source.is_file() or source.is_symlink():
                    return FixFailed(f"the staged copy of {relative} is missing")
            if not worktree_unchanged():
                return FixRefused("the worktree changed after the copy")
            if paths:
                stage_and_commit(repo_root, staging, path, paths, originals, set(changes.changed))
    except FixConflict as error:
        return FixRefused(str(error))
    except OSError as error:
        return FixFailed(str(error))
    if withheld:
        return FixPartial(changes.changed, changes.removed, withheld)
    return FixApplied(changes.changed, changes.removed)


def stage_and_commit(repo_root: Path, staging: Path, path: Path, paths: list[StagingPath],
                     originals: list[PathVersion], changed: set[str]) -> None:
    """Stage each fix and pass the record to the shared commit code."""
    write_record(path, StagingRecord(state="staging", paths=paths))
    entries: list[CommitPath] = []
    try:
        for item, original in zip(paths, originals, strict=True):
            files = commit_files(repo_root, item)
            files.folder.parent.mkdir(parents=True, exist_ok=True)
            files.folder.mkdir(mode=0o700)
            if item["destination"] in changed:
                stage_file(staging / item["destination"], files.staged, files.destination)
                # A write through the published hard link must leave the received file intact.
                stage_file(files.staged, files.received, files.staged)
            fixed = file_version(files.staged)
            atomic_file.sync_folder(files.folder)
            atomic_file.sync_folder(files.folder.parent)
            entries.append(CommitPath(**item, original=original, fixed=fixed))
    except OSError:
        _ = finish_record(repo_root, path)
        raise
    write_record(path, CommitRecord(state="commit", entries=entries))
    _ = finish_record(repo_root, path)


COPY_NAME = "verify-lint-copy"
OWNER_NAME = "owner"
SENT_NAME = "sent.json"
TREE_NAME = "tree"
FileStat = tuple[int, int, int, int]


class CargoMetadata(TypedDict):
    workspace_root: str
    target_directory: str


@dataclass(frozen=True)
class CopyMade:
    """The private copy: the fix pass runs cargo with manifest and target."""

    folder: Path
    tree: Path
    manifest: Path
    target: Path


@dataclass(frozen=True)
class InPlace:
    """cargo-berth does not configure the repository, or it has no ledger, so the fix pass writes the worktree."""


def listed_files(repo_root: Path) -> list[str]:
    """The tracked and untracked files that git does not ignore, as the Mac mirror copies them."""
    listed = subprocess.run(["git", "-C", str(repo_root), "ls-files", "-z", "-co", "--exclude-standard"],
                            capture_output=True, check=False)
    if listed.returncode != 0:
        raise OSError("git ls-files failed: " + listed.stderr.decode(errors="replace").strip())
    return sorted({os.fsdecode(name) for name in listed.stdout.split(b"\0") if name})


def file_stat(path: Path) -> FileStat | None:
    """Size, modification time, inode and mode; None for a missing path or a folder."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(status.st_mode):
        return None
    return (status.st_size, status.st_mtime_ns, status.st_ino, status.st_mode)


def worktree_snapshot(repo_root: Path) -> dict[str, FileStat]:
    snapshot: dict[str, FileStat] = {}
    for relative in listed_files(repo_root):
        status = file_stat(repo_root / relative)
        if status is not None:
            snapshot[relative] = status
    return snapshot


def owner_alive(folder: Path, owner: int) -> bool:
    """True when another live process owns the copy folder."""
    try:
        pid = int((folder / OWNER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if pid == owner:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def copy_parent(target: Path) -> Path:
    """The folder for the copies: a folder in the cargo target folder that ends in target/package.

    Cargo looks up the folders for the workspace of a package, and it stops at a
    folder that ends in target/package, where `cargo package` checks its copies.
    Without that stop, a package that the copy's workspace excludes finds the
    worktree's own Cargo.toml, and cargo refuses it as a member that is not listed.
    """
    return (target if target.name == "target" else target / "target") / "package"


def claim_folder(parent: Path, owner: int) -> Path:
    """Take the copy folder in parent.

    The name is the same in each run, so cargo finds the copy's earlier build
    of the workspace members fresh. A folder that a stopped run left is
    deleted. A folder that a live run owns gives a name with this owner.
    """
    parent.mkdir(parents=True, exist_ok=True)
    staged = parent / f".{COPY_NAME}-{owner}.new"
    shutil.rmtree(staged, ignore_errors=True)
    for name in (COPY_NAME, f"{COPY_NAME}-{owner}"):
        folder = parent / name
        if folder.exists() and not owner_alive(folder, owner):
            shutil.rmtree(folder)
        staged.mkdir()
        _ = (staged / OWNER_NAME).write_text(str(owner), encoding="utf-8")
        try:
            os.rename(staged, folder)
        except OSError:
            shutil.rmtree(staged, ignore_errors=True)
            continue
        return folder
    raise OSError(f"{parent / COPY_NAME} is in use by another lint run")


def make_copy(repo_root: Path, metadata: CargoMetadata, owner: int,
              board_reader: BoardReader | None = None) -> CopyMade | InPlace | FixFailed:
    """Copy the worktree for the fix pass in a repository that cargo-berth configures."""
    board = (board_reader or read_board)(repo_root)
    if isinstance(board, berth_board.BoardUnavailable):
        return FixFailed(f"cargo-berth board: {board.reason}")
    if not board.configured:
        return InPlace()
    root = repo_root.resolve()
    workspace = Path(metadata["workspace_root"]).resolve()
    if not workspace.is_relative_to(root):
        return FixFailed(f"the cargo workspace {workspace} is not inside {root}")
    try:
        folder = claim_folder(copy_parent(Path(metadata["target_directory"])), owner)
    except OSError as error:
        return FixFailed(str(error))
    tree = folder / TREE_NAME
    try:
        sent: dict[str, FileStat] = {}
        for relative in listed_files(root):
            source = root / relative
            before = file_stat(source)
            if before is None:
                continue
            destination = tree / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(before[3]):
                os.symlink(os.readlink(source), destination)
            else:
                # copy2 keeps the modification time, so cargo finds unchanged files fresh.
                _ = shutil.copy2(source, destination)
            if file_stat(source) != before:
                raise OSError(f"{relative} changed while the copy was made")
            sent[relative] = before
        _ = (folder / SENT_NAME).write_text(json.dumps(sent), encoding="utf-8")
    except OSError as error:
        shutil.rmtree(folder, ignore_errors=True)
        return FixFailed(str(error))
    return CopyMade(folder, tree, tree / workspace.relative_to(root) / "Cargo.toml",
                    Path(metadata["target_directory"]))


def read_sent(folder: Path) -> dict[str, FileStat]:
    values = cast(dict[str, list[int]], json.loads((folder / SENT_NAME).read_text(encoding="utf-8")))
    return {relative: (status[0], status[1], status[2], status[3]) for relative, status in values.items()}


def copy_changes(repo_root: Path, tree: Path, sent: dict[str, FileStat]) -> MirrorChanges:
    """The regular files that the fix pass changed, added or removed in the copy."""
    changed: list[str] = []
    removed: list[str] = []
    for relative, status in sent.items():
        copied = tree / relative
        if not copied.is_symlink() and not copied.exists():
            removed.append(relative)
        elif not stat.S_ISLNK(status[3]) and not copied.is_symlink() and not filecmp.cmp(
                copied, repo_root / relative, shallow=False):
            changed.append(relative)
    for directory, _, names in os.walk(tree):
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(tree).as_posix()
            if relative not in sent and path.is_file() and not path.is_symlink():
                changed.append(relative)
    return MirrorChanges(tuple(sorted(changed)), tuple(sorted(removed)))


def return_copy(repo_root: Path, folder: Path, board_reader: BoardReader | None = None) -> FixResult:
    """Bring the held changes of the copy back, then delete the copy."""
    root = repo_root.resolve()
    try:
        sent = read_sent(folder)

        def unchanged() -> bool:
            return worktree_snapshot(root) == sent

        if not unchanged():
            return FixRefused("the worktree changed during the lint fix pass")
        changes = without_ignored(root, copy_changes(root, folder / TREE_NAME, sent))
        if isinstance(changes, FixFailed):
            return changes
        return apply_fix(root, folder / TREE_NAME, changes, unchanged, board_reader)
    except (OSError, ValueError, KeyError, IndexError) as error:
        return FixFailed(str(error))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def report_copy(result: FixResult) -> None:
    if isinstance(result, (FixApplied, FixPartial)):
        count = len(result.changed) + len(result.removed)
        print(f"verify.sh: the lint fix pass wrote {count} file{'s' if count != 1 else ''} into the worktree.")
    elif isinstance(result, (FixRefused, FixFailed)):
        print(f"verify.sh: the lint fix return stopped: {result.reason}.", file=sys.stderr)
    for line in withheld_lines(result):
        print(f"verify.sh: {line}.", file=sys.stderr)


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="fix_return.py")
    commands = parser.add_subparsers(dest="command", required=True)
    finishing = commands.add_parser("finish", help="complete a stopped Mac fix return")
    _ = finishing.add_argument("--repo-root", required=True)
    copying = commands.add_parser("copy", help="copy the worktree for the lint fix pass")
    _ = copying.add_argument("--repo-root", required=True)
    _ = copying.add_argument("--owner", required=True, type=int)
    returning = commands.add_parser("return-copy", help="bring the held changes of the copy back")
    _ = returning.add_argument("--repo-root", required=True)
    _ = returning.add_argument("--folder", required=True)
    parsed = parser.parse_args(arguments)
    command = cast(str, parsed.command)
    if command == "copy":
        try:
            metadata = cast(CargoMetadata, json.loads(sys.stdin.read()))
            made = make_copy(Path(cast(str, parsed.repo_root)), metadata, cast(int, parsed.owner))
        except (ValueError, KeyError, TypeError) as error:
            made = FixFailed(f"cargo metadata: {error}")
        if isinstance(made, FixFailed):
            print(f"verify.sh: the lint fix pass could not copy the worktree: {made.reason}", file=sys.stderr)
            return 2
        if isinstance(made, InPlace):
            print("in-place")
        else:
            print(f"copy\n{made.folder}\n{made.tree}\n{made.manifest}\n{made.target}")
        return 0
    if command == "return-copy":
        result = return_copy(Path(cast(str, parsed.repo_root)), Path(cast(str, parsed.folder)))
        report_copy(result)
        return 0 if local_return_succeeded(result) else 1
    try:
        done = finish(Path(cast(str, parsed.repo_root)))
    except (OSError, ValueError, KeyError) as error:
        print(f"fix_return.py: the stopped Mac fix could not be finished: {error}", file=sys.stderr)
        return 2
    if done:
        print(f"verify.sh: {done}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
