"""Make a worktree after checking for finished worktrees."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys

from scripts.agents.remote import local_machine
from scripts.lib import build_folders, git_files, process_start
from scripts.production import staff
from scripts.worktree import worktrees


MAC_SCAN_SECONDS = 1


@dataclass(frozen=True)
class ProcessDirectoriesRead:
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class ProcessDirectoriesUnreadable:
    """The scan cannot prove that a worktree has no process inside it."""


def linux_process_directories(proc: Path) -> ProcessDirectoriesRead | ProcessDirectoriesUnreadable:
    paths: list[Path] = []
    uid = os.getuid()
    try:
        for process in proc.iterdir():
            if not process.name.isdecimal():
                continue
            try:
                if process.stat().st_uid != uid:
                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            tasks = process / "task"
            try:
                threads = [thread for thread in tasks.iterdir() if thread.name.isdecimal()]
            except (FileNotFoundError, ProcessLookupError):
                threads = []
            if not threads:
                # The task directory can be unavailable after the leader exits.
                if isinstance(process_start.proc_table_start(int(process.name), proc), process_start.Gone):
                    continue
                return ProcessDirectoriesUnreadable()

            before = len(paths)
            permission_denied = False
            for thread in threads:
                try:
                    cwd = (thread / "cwd").readlink()
                except PermissionError:
                    permission_denied = True
                    continue
                except OSError:
                    state = process_start.proc_table_start(int(thread.name), tasks)
                    if isinstance(state, (process_start.Gone, process_start.Zombie)):
                        continue
                    return ProcessDirectoriesUnreadable()
                if not cwd.is_absolute():
                    return ProcessDirectoriesUnreadable()
                paths.append(cwd)
            if len(paths) == before and not permission_denied:
                state = process_start.proc_table_start(int(process.name), proc)
                if not isinstance(state, (process_start.Gone, process_start.Zombie)):
                    return ProcessDirectoriesUnreadable()
    except OSError:
        return ProcessDirectoriesUnreadable()
    return ProcessDirectoriesRead(tuple(paths)) if paths else ProcessDirectoriesUnreadable()


def mac_process_directories() -> ProcessDirectoriesRead | ProcessDirectoriesUnreadable:
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-u", str(os.getuid())], capture_output=True,
                                text=True, errors="surrogateescape", timeout=MAC_SCAN_SECONDS, check=False)
        if result.returncode not in (0, 1) or result.stderr or not result.stdout.startswith("p"):
            return ProcessDirectoriesUnreadable()
        paths: list[Path] = []
        for process in result.stdout[1:].rstrip("\n").split("\np"):
            pid, *fields = process.split("\n")
            if not pid.isdecimal() or not fields or len(fields) % 2:
                return ProcessDirectoriesUnreadable()
            for descriptor, name in zip(fields[::2], fields[1::2], strict=True):
                if descriptor != "fcwd" or not name.startswith("n/") or "\\" in name:
                    return ProcessDirectoriesUnreadable()
                paths.append(Path(name[1:]))
        return ProcessDirectoriesRead(tuple(paths))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ProcessDirectoriesUnreadable()


def process_directories() -> ProcessDirectoriesRead | ProcessDirectoriesUnreadable:
    system = platform.system()
    if system == "Linux":
        return linux_process_directories(Path("/proc"))
    if system == "Darwin":
        return mac_process_directories()
    return ProcessDirectoriesUnreadable()


class NoUnit(Enum):
    NO_UNIT = "no unit owns the worktree"


def tree_unit(path: Path, unit_folders: Mapping[Path, str], snapshot: staff.StaffSnapshot) -> str | NoUnit:
    """Name the unit that owns a linked worktree; it reads the checkout record once.

    The record names the unit. A tree with no record, or a record that names no unit,
    belongs to the unit whose folder in `unit_folders` holds the tree.
    """
    folder_unit = unit_folders.get(path.parent, NoUnit.NO_UNIT)
    try:
        record = worktrees.checkout_record(path)
        if record is None:
            return NoUnit.NO_UNIT if worktrees.record_file(path).is_symlink() else folder_unit
    except (OSError, ValueError):
        return NoUnit.NO_UNIT
    if isinstance(record.layout, worktrees.RecordedWorktreeOutput):
        return record.layout.unit
    owner = snapshot.owner_of(record.owner)
    if owner is not None and owner.kind == "unit":
        return owner.name
    if record.owner in unit_folders.values() or any(row.kind == "unit" and row.name == record.owner
                                                    for row in snapshot.rows):
        return record.owner
    return folder_unit


def belongs_to_unit(path: Path, unit_folder: Path, unit: str, snapshot: staff.StaffSnapshot) -> bool:
    return tree_unit(path, {unit_folder: unit}, snapshot) == unit


def matches_repository(path: Path, common_dir: Path) -> bool:
    try:
        return git_files.at(path).common_dir == common_dir
    except git_files.GitFilesError:
        return False


def finished_worktrees(source: Path, destination: Path, unit: str, snapshot: staff.StaffSnapshot, *,
                       read_directories: Callable[[], ProcessDirectoriesRead | ProcessDirectoriesUnreadable]
                       = process_directories) -> tuple[Path, ...]:
    """Return each finished worktree that prevents creation at the destination."""
    paths = tuple(tree.path for tree in worktrees.linked_trees(source))
    if not paths:
        return ()
    common_dir = git_files.at(source).common_dir
    if destination in paths and matches_repository(destination, common_dir):
        # A retry for a registered checkout creates no new worktree.
        return ()
    machine = local_machine()
    homes = {row.worktree.path.resolve() for row in snapshot.rows if row.kind == "unit"
             and isinstance(row.worktree, staff.PresentWorktree) and row.worktree.machine == machine}
    clean_landed: list[Path] = []
    for path in paths:
        if path in homes or not matches_repository(path, common_dir):
            continue
        if not belongs_to_unit(path, destination.parent, unit, snapshot):
            continue
        ancestor = worktrees.git(path, "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main",
                                 check=False)
        if ancestor.returncode:
            continue
        status = worktrees.git(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all",
                               check=False)
        if not status.returncode and not status.stdout and not status.stderr:
            clean_landed.append(path)
    if not clean_landed:
        return ()
    directories = read_directories()
    if isinstance(directories, ProcessDirectoriesUnreadable):
        return ()
    deleted_paths = tuple(Path(str(cwd).removesuffix(" (deleted)"))
                          for cwd in directories.paths if cwd.name.endswith(" (deleted)"))
    occupied_paths = directories.paths + deleted_paths
    return tuple(path for path in clean_landed if not any(
        cwd.is_relative_to(path) for cwd in occupied_paths))


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        print("usage: make_worktree.sh <repo-word> <unit> <short-name>", file=sys.stderr)
        return 2
    repo, unit, short_name = arguments
    try:
        destination = build_folders.worktree_path(repo, unit, short_name).resolve()
        source = worktrees.main_checkout_for_word(repo)
        snapshot = worktrees.staff_record()
        finished = finished_worktrees(source, destination, unit, snapshot)
        if not finished:
            print(worktrees.make(repo, unit, short_name, snapshot))
            return 0
    except worktrees.Refused as error:
        print(error, file=sys.stderr)
        return error.code
    except (OSError, ValueError, KeyError, staff.StaffRefused, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{unit} has finished worktrees.", *finished, sep="\n", file=sys.stderr)
    print("1. Get the showrunner's yes for each exact path.", file=sys.stderr)
    print("2. Write each record in ~/.local/state/removal-lists/by-hand.log as " +
          "~/.claude/docs/as-built/removal-by-hand.md specifies.", file=sys.stderr)
    print("3. Run one removal command per path.", file=sys.stderr)
    for path in finished:
        print(shlex.join(["git", "-C", str(source), "worktree", "remove", str(path)]), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
