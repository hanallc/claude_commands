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
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from enum import Enum

from scripts.agents import remote
from scripts.lib import conf, git_files
from scripts.lib.atomic_file import create_text_atomic, prepared_replacement, sync_folder, write_text_atomic
from scripts.production import showrunners, staff
from scripts.production.production_doc import (
    EnrolledPaths, Refusal, production_backup_checkout, production_backup_root, read_production, resolve_production_paths,
    staged_notes_directory,
)
from scripts.production.production_names import BACKUP_DOCUMENT_DIRECTORY, BACKUP_ROOT, LEGACY_COPIES, MAC_BACKUP_REPOSITORY


SNAPSHOT_FOLDERS = (BACKUP_DOCUMENT_DIRECTORY, "log", "notes")
BACKUP_BRANCH = "refs/heads/main"
SSH_OPTIONS = ("ssh", "-o", "BatchMode=yes")
BACKUP_MARKER = ".production-backup"
BACKUP_MARKER_TEXT = "production backup checkout\n"
HOURLY_ACTIVE = ".hourly-active"


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


def command(*arguments: str, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False, env=env)
    if result.returncode:
        raise OSError(result.stderr.strip() or f"{arguments[0]} exited with code {result.returncode}")
    return result.stdout.strip()


def git(repository: Path, *arguments: str) -> str:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return command("git", "-C", str(repository), "-c", "gc.auto=0", "-c", "core.autocrlf=false",
                   "-c", "core.hooksPath=/dev/null", *arguments, env=environment)


def _check_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise OSError(f"{path}: expected a private backup directory")


def _check_backup_marker(repository: Path) -> None:
    _check_directory(repository.parent)
    _check_directory(repository)
    marker = repository / BACKUP_MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_text() != BACKUP_MARKER_TEXT:
        raise OSError(f"{repository}: no production backup marker")


def _check_backup_checkout(repository: Path) -> None:
    _check_backup_marker(repository)
    git_dir = repository / ".git"
    _check_directory(git_dir)
    _check_directory(git_dir / "info")
    checkout = git_files.at(repository)
    if checkout.git_dir != git_dir.resolve() or checkout.common_dir != checkout.git_dir:
        raise OSError(f"{repository}: expected a separate backup Git repository")


def _prepare_backup_checkout(repository: Path) -> None:
    _check_directory(repository.parent)
    _check_directory(repository)
    repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not any(repository.iterdir()):
        write_text_atomic(repository / BACKUP_MARKER, BACKUP_MARKER_TEXT, new_file_mode=0o600)
    _check_backup_marker(repository)
    git_dir = repository / ".git"
    _check_directory(git_dir)
    if not git_dir.exists():
        _ = git(repository, "init", "--initial-branch=main")
    _check_backup_checkout(repository)


@contextmanager
def _backup_lock(root: Path) -> Generator[None]:
    _check_directory(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _save_legacy_copy(target: Path, content: bytes) -> None:
    with prepared_replacement(target, content, mode=0o600) as temporary:
        os.link(temporary, target, follow_symlinks=False)
    sync_folder(target.parent)


def _copy_legacy_logs(source: Path, destination: Path) -> None:
    if not os.path.lexists(source):
        return
    _check_directory(source)
    _check_directory(destination.parent)
    _check_directory(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    sync_folder(destination.parent.parent)
    sync_folder(destination.parent)
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_symlink() or not entry.is_file() or target.is_symlink():
            raise OSError(f"{entry}: legacy LOG backup must be a regular file")
        content = entry.read_bytes()
        if target.exists():
            if target.is_file() and target.read_bytes() == content:
                continue
            raise OSError(f"{target}: legacy LOG backup differs from {entry}")
        _save_legacy_copy(target, content)
        if target.read_bytes() != content or entry.read_bytes() != content:
            raise OSError(f"{entry}: cannot verify the legacy LOG copy")


def backup_during_check(root: Path, state: Path, reference: str) -> None:
    """Keep dated LOG copies until the hourly job verifies migration."""
    marker = root / HOURLY_ACTIVE
    if not marker.is_symlink() and marker.is_file():
        return
    with _backup_lock(root):
        if not marker.is_symlink() and marker.is_file():
            return
        owner = showrunners.owner_for(reference, staff.authority_snapshot())
        if not isinstance(owner.doc, staff.LiveDoc) or owner.doc.machine != remote.local_machine():
            return
        production = read_production(owner)
        try:
            content = production.log.read_bytes()
        except FileNotFoundError:
            return
        if not content.strip():
            return
        directory = state / production.owner.id / LEGACY_COPIES
        _check_directory(directory.parent)
        _check_directory(directory)
        kept = sorted(directory.glob("log-*.md"))
        if kept and kept[-1].read_bytes() == content:
            return
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(production.zone).strftime("%Y%m%dT%H%M%S%f")
        target = directory / f"log-{stamp}.md"
        _save_legacy_copy(target, content)


def preserve_legacy_logs(state: Path, owner: staff.StaffId) -> None:
    """Verify and remove old LOG copies before cleanup removes their state."""
    root = production_backup_root()
    source = state / owner / LEGACY_COPIES
    with _backup_lock(root):
        if os.path.lexists(source):
            _check_directory(source.parent)
            _copy_legacy_logs(source, root / owner / LEGACY_COPIES)
            shutil.rmtree(source)
            sync_folder(source.parent)


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
    _prepare_backup_checkout(repository)
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
    relative = (BACKUP_ROOT / identity / MAC_BACKUP_REPOSITORY).as_posix()
    remote_path = '"$HOME"/' + shlex.quote(relative)

    def maintenance(repack: bool) -> str:
        measure = f"du -sk -- {remote_path}"
        check = f"test -d {remote_path}/objects"
        collect = f"git --git-dir={remote_path} gc --no-prune"
        return command(*SSH_OPTIONS, peer, " && ".join([check, collect, measure] if repack else [check, measure]))

    return BackupMirror(f"{peer}:{relative}", maintenance)


def publish(repository: Path, mirror: BackupMirror, repack_after_commits: int) -> None:
    """Keep local history and retry the push on each run."""
    _check_backup_checkout(repository)
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
               mirror: Callable[[staff.StaffId], BackupMirror], repack_after_commits: int, *,
               hourly: bool = False) -> int:
    standard_root = production_backup_root()
    failures = 0
    with _backup_lock(root):
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
                _copy_legacy_logs(state / identity / LEGACY_COPIES, folder / LEGACY_COPIES)
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
        if hourly and not failures:
            _ = create_text_atomic(root / HOURLY_ACTIVE, "legacy LOG migration verified\n")
    return int(failures != 0)


def main(arguments: list[str]) -> int:
    if len(arguments) > 1 or (arguments and arguments[0].startswith("-") and arguments != ["--hourly"]):
        print("usage: log_backup.py [--hourly | <production>]", file=sys.stderr)
        return 2
    try:
        root = production_backup_root()
        state = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
        if arguments and arguments != ["--hourly"]:
            backup_during_check(root, state, arguments[0])
            return 0
        if sys.platform != "linux":
            print("log_backup: run this job on natedev", file=sys.stderr)
            return 1
        peer = remote.other_machine()
        repack_after_commits = conf.read("production_backup").whole("repack_after_commits")
        return backup_all(root, state, staff.local_snapshot(), lambda identity: mac_mirror(identity, peer),
                          repack_after_commits, hourly=arguments == ["--hourly"])
    except BlockingIOError:
        print("log_backup: another backup is active", file=sys.stderr)
        return 1
    except (OSError, ValueError, Refusal, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"log_backup: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
