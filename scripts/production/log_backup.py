#!/usr/bin/env python3
"""Save production files locally and publish their history to the Mac."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from enum import Enum

from scripts.agents import remote
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.production_doc import (
    EnrolledPaths, Refusal, production_backup_checkout, production_backup_root, read_production, resolve_production_paths,
    staged_notes_directory,
)
from scripts.production.production_names import BACKUP_DOCUMENT_DIRECTORY


LEGACY_COPIES = "log-backups"
SNAPSHOT_FOLDERS = (BACKUP_DOCUMENT_DIRECTORY, "log", "notes")
BACKUP_BRANCH = "refs/heads/main"
SSH_OPTIONS = ("ssh", "-o", "BatchMode=yes")


class NoStagedNotes(Enum):
    ABSENT = "the production has no staged notes directory"


@dataclass(frozen=True)
class BackupSources:
    document: Path
    log: Path
    notes: Path | NoStagedNotes


@dataclass(frozen=True)
class BackupMirror:
    destination: str
    maintenance: Callable[[bool], str]


def command(*arguments: str) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or f"{arguments[0]} exited with code {result.returncode}")
    return result.stdout.strip()


def git(repository: Path, *arguments: str) -> str:
    return command("git", "-C", str(repository), "-c", "gc.auto=0", "-c", "core.autocrlf=false",
                   "-c", "core.hooksPath=/dev/null", *arguments)


def production_sources(row: staff.StaffRow, doc: Path) -> BackupSources:
    paths = resolve_production_paths(doc)
    if isinstance(paths, EnrolledPaths):
        return BackupSources(paths.live_doc, paths.log, paths.notes)
    production = read_production(row)
    notes = staged_notes_directory(production.doc)
    return BackupSources(production.doc, production.log, notes if notes is not None else NoStagedNotes.ABSENT)


def backup(sources: BackupSources, repository: Path) -> None:
    """Copy the selected files and commit their exact bytes when they change."""
    if not sources.document.is_file() or not sources.log.is_file():
        raise OSError("production document or LOG is missing")
    notes = sources.notes
    if isinstance(notes, Path):
        if notes.exists() and not notes.is_dir():
            raise OSError(f"{notes}: expected a notes directory")
        if any(path.name.casefold() == ".git" for path in notes.rglob("*")):
            raise OSError(f"{notes}: nested Git metadata cannot be backed up")
    repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (repository / ".git").is_dir():
        _ = git(repository, "init", "--initial-branch=main")
    # Override attributes in the snapshot so a checkout restores the same bytes.
    write_text_atomic(repository / ".git/info/attributes", "* -filter -text -ident -working-tree-encoding -crlf\n")
    for folder in SNAPSHOT_FOLDERS:
        target = repository / folder
        if target.exists():
            shutil.rmtree(target)
        if folder == "notes":
            if isinstance(notes, Path) and notes.is_dir():
                _ = shutil.copytree(notes, target, symlinks=True)
        else:
            source = sources.document if folder == BACKUP_DOCUMENT_DIRECTORY else sources.log
            target.mkdir()
            _ = shutil.copyfile(source, target / source.name)
    # Include tracked paths that disappeared and exclude incidental files.
    tracked = git(repository, "ls-files", "--", *SNAPSHOT_FOLDERS)
    selected = tuple(folder for folder in SNAPSHOT_FOLDERS if (repository / folder).exists()
                     or any(path.startswith(folder + "/") for path in tracked.splitlines()))
    _ = git(repository, "add", "-A", "-f", "--", *selected)
    if git(repository, "diff", "--cached", "--name-only"):
        _ = git(repository, "-c", "user.name=Production Backup", "-c", "user.email=backup@localhost",
                "commit", "-m", "Save production files")


def mac_mirror(identity: staff.StaffId, peer: str) -> BackupMirror:
    relative = production_backup_checkout(identity).with_suffix(".git").relative_to(Path.home()).as_posix()
    remote_path = '"$HOME"/' + shlex.quote(relative)

    def maintenance(repack: bool) -> str:
        measure = f"du -sk -- {remote_path}"
        check = f"test -d {remote_path}/objects"
        collect = f"git --git-dir={remote_path} gc --no-prune"
        return command(*SSH_OPTIONS, peer, " && ".join([check, collect, measure] if repack else [check, measure]))

    return BackupMirror(f"{peer}:{relative}", maintenance)


def publish(repository: Path, mirror: BackupMirror, repack_after_commits: int) -> None:
    """Keep local history and retry the push on each run."""
    tip = git(repository, "log", "-1", "--format=%H", BACKUP_BRANCH)
    total = int(git(repository, "rev-list", "--count", tip))
    for location in ("local", "mac"):
        marker = repository / ".git" / f"backup-repack-{location}"
        try:
            previous = int(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = 0
        repack = total - previous >= repack_after_commits
        if location == "local":
            if repack:
                _ = git(repository, "gc", "--no-prune")
            size = command("du", "-sk", "--", str(repository))
        else:
            _ = git(repository, "-c", f"core.sshCommand={shlex.join(SSH_OPTIONS)}",
                    "push", "--", mirror.destination, f"{tip}:{BACKUP_BRANCH}")
            size = mirror.maintenance(repack)
        if repack:
            write_text_atomic(marker, f"{total}\n")
        print(f"{location}: {size}", flush=True)


def backup_all(root: Path, state: Path, record: staff.StaffSnapshot,
               mirror: Callable[[staff.StaffId], BackupMirror], repack_after_commits: int) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    standard_root = production_backup_root()
    failures = 0
    with (root / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owners = {row.id: row for row in record.find(kind="showrunner")
                  if isinstance(row.doc, staff.LiveDoc) and row.doc.machine == remote.local_machine()}
        identities = owners.keys() | {staff.StaffId(path.name) for path in root.iterdir() if path.is_dir()}
        identities |= {staff.StaffId(path.parent.name) for path in state.glob(f"*/{LEGACY_COPIES}")}
        for identity in sorted(identities):
            if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity):
                continue
            repository = root / production_backup_checkout(identity).relative_to(standard_root)
            folder = repository.parent
            try:
                old = state / identity / LEGACY_COPIES
                if old.is_dir():
                    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _ = shutil.copytree(old, folder / LEGACY_COPIES, dirs_exist_ok=True, symlinks=True)
                owner = owners.get(identity)
                if owner is not None and isinstance(owner.doc, staff.LiveDoc):
                    backup(production_sources(owner, owner.doc.path), repository)
            except (OSError, ValueError, Refusal) as error:
                print(f"log_backup: {identity}: {error}", file=sys.stderr)
                failures += 1
            if (repository / ".git").is_dir():
                try:
                    publish(repository, mirror(identity), repack_after_commits)
                except (OSError, ValueError) as error:
                    print(f"log_backup: {identity}: {error}", file=sys.stderr)
                    failures += 1
    return int(failures != 0)


def main(arguments: list[str]) -> int:
    if arguments:
        print("usage: log_backup.py", file=sys.stderr)
        return 2
    if sys.platform != "linux":
        print("log_backup: run this job on natedev", file=sys.stderr)
        return 1
    try:
        root = production_backup_root()
        state = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
        peer = remote.other_machine()
        repack_after_commits = conf.read("production_backup").whole("repack_after_commits")
        return backup_all(root, state, staff.local_snapshot(), lambda identity: mac_mirror(identity, peer),
                          repack_after_commits)
    except BlockingIOError:
        print("log_backup: another backup is active", file=sys.stderr)
        return 1
    except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"log_backup: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
