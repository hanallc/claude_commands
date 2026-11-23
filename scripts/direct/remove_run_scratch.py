#!/usr/bin/env python3
"""Report the scratch folders an ended run keeps.

The report includes Verify caches, outermost Git checkouts, and Cargo targets.
The report skips folders that still host live commands.
With --tracked, record the end of the run's scratch task.
With --owned-workspaces, report the caller's retained workspaces.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

from scripts.direct import workers
from scripts.lib import git_files, scratch

VERIFY_CACHE = "verify_cache"
# A folder holding any of these is a checkout or a cargo target.
COPY_MARKERS = (".git", ".rustc_info.json", "CACHEDIR.TAG")


def is_copy(folder: str) -> bool:
    return any(os.path.lexists(os.path.join(folder, marker)) for marker in COPY_MARKERS)


def scratch_folders(run: Path) -> list[Path]:
    """verify_cache/ and each outermost checkout or cargo target under run, never run itself."""
    if run.is_symlink() or not run.is_dir():
        return []
    protected = workers.protected_working_directories(run)
    found: list[Path] = []
    frontier = [str(run)]
    while frontier:
        current = frontier.pop()
        try:
            with os.scandir(current) as entries:
                listed = list(entries)
                folders = {entry.path for entry in listed if entry.is_dir(follow_symlinks=False)}
        except OSError:
            continue
        for folder in sorted(folders):
            candidate = Path(folder).resolve()
            if candidate in protected:
                continue
            if ((current == str(run) and os.path.basename(folder) == VERIFY_CACHE)
                    or is_copy(folder) or scratch.removal_mark(Path(folder)).exists()):
                if not any(candidate in cwd.parents for cwd in protected):
                    found.append(Path(folder))
            else:
                frontier.append(folder)
    return found


def worktree_source(folder: Path) -> Path | None:
    """The common git folder of the repo folder is a linked worktree of, or None when folder is no
    linked worktree: its .git is a file, not a link."""
    if (folder / ".git").is_symlink() or not (folder / ".git").is_file():
        return None
    try:
        return git_files.at(folder).common_dir
    except git_files.GitFilesError:
        return None


def git(common: Path, *arguments: str) -> bool:
    try:
        result = subprocess.run(["git", "--git-dir", str(common), "worktree", *arguments],
                                check=False, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def remove_tree(folder: Path) -> None:
    """Remove one scratch folder; a linked worktree through its source repo, so no entry stays."""
    source = worktree_source(folder)
    # Twice --force also removes a locked worktree: the run that locked it has ended.
    if source is not None and git(source, "remove", "--force", "--force", str(folder)):
        return
    shutil.rmtree(folder)
    if source is not None:
        _ = git(source, "prune")


def end_tracked_scratch(run: Path) -> list[scratch.KeptFolder]:
    """End the run's tracked scratch except folders still hosting its commands."""
    task = f"run:{run.name}"
    protected = workers.protected_working_directories(run)

    def retained(path: Path) -> bool:
        resolved = path.resolve()
        return any(cwd == resolved or resolved in cwd.parents for cwd in protected)

    return scratch.end(task, keep=retained)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("runs", nargs="*", help="run folders whose scratch folders stay")
    _ = parser.add_argument("--tracked", action="store_true", help="also end tracked scratch for these runs")
    _ = parser.add_argument("--owned-workspaces", action="store_true", help="also report the caller's workspaces")
    args = parser.parse_args()
    runs = [Path(run) for run in cast("list[str]", args.runs)]
    reported: set[Path] = set()

    def report(folder: Path, reason: str) -> None:
        if not scratch.retained_folder_exists(folder):
            return
        resolved = folder.resolve()
        if resolved not in reported:
            print(f"kept for a manual removal list: {folder} (remove_run_scratch, {reason})", file=sys.stderr)
            reported.add(resolved)

    for run in dict.fromkeys(runs):
        if cast(bool, args.tracked):
            reported.update(folder.path.resolve() for folder in end_tracked_scratch(run))
        for folder in scratch_folders(run):
            report(folder, "the run ended")
    if cast(bool, args.owned_workspaces):
        from scripts.copies import copies

        try:
            for folder, _ in copies.owned_workspaces(copies.caller_owner(), purpose=copies.WorkspaceScanPurpose.REPORT):
                report(folder, "the session ended")
        except (copies.CopyError, OSError, scratch.ScratchError) as error:
            print(f"remove_run_scratch: cannot report owned workspaces: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
