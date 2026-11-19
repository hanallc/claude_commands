#!/usr/bin/env python3
"""Remove the scratch copies an ended run left in its run folder.

A run folder under /tmp/claude/unit/ (or /tmp/claude/delegate/) collects the
copies its phases make: gate copies under phases/, exports under scratch/, and
before and after builds such as p62_bin and p26_before. Each is a git checkout,
a cargo target, or both. Every non-retired worker's live detached command keeps its
working directory and containing checkout. end_session.sh passes every run
folder the ending director holds. In each one this removes:

  * verify_cache/, the verify.sh pass records; the time they saved is already
    in ~/.local/state/verify/saved.jsonl;
  * every folder, at any depth, that holds .git (a folder or a file): a checkout;
  * every folder, at any depth, that holds .rustc_info.json or CACHEDIR.TAG: a
    cargo target.

With --tracked it also ends tracked scratch for these runs, preserving live
command directories and the scratch registry's existing unsaved-work holds.

For a linked worktree, cleanup also removes its registration in the source repository.
The coordinator checks mount IDs before it descends into each directory.
The run folder stays with its records and logs.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
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
                folders.update(str(path) for path in scratch.marked_folders(Path(entry.path) for entry in listed))
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


def worktree_checkout(folder: Path) -> git_files.Checkout | None:
    """Read a linked worktree's checkout and registration."""
    if (folder / ".git").is_symlink() or not (folder / ".git").is_file():
        return None
    try:
        return git_files.at(folder)
    except git_files.GitFilesError:
        return None


def worktree_source(folder: Path) -> Path | None:
    checkout = worktree_checkout(folder)
    return checkout.common_dir if checkout is not None else None


def git(common: Path, operation: str, registration: scratch.WorktreeRegistration | None = None) -> bool:
    """Remove a Git registration or prune stale registrations through the coordinator."""
    with scratch.coordinate_removals() as removal:
        if operation == "remove" and registration is not None:
            return removal.remove_worktree_registration(registration)
        if operation == "prune":
            try:
                removal.prune_worktrees(common)
            except subprocess.SubprocessError:
                return False
            return True
        raise ValueError(f"unsupported worktree operation: {operation}")


def remove(folder: Path) -> None:
    scratch.remove_marked(folder, source_deletion=scratch.SourceDeletion.RUN)


def remove_tree(folder: Path) -> None:
    """Delete the checkout before removing its Git registration."""
    def cleanup(registration: scratch.WorktreeRegistration) -> bool:
        return git(registration.common, "remove", registration) or git(registration.common, "prune")

    scratch.remove_journaled_tree(folder, worktree_checkout(folder), cleanup=cleanup)


def end_tracked_scratch(run: Path) -> int:
    """End the run's tracked scratch except folders still hosting its commands."""
    task = f"run:{run.name}"
    protected = workers.protected_working_directories(run)

    def retained(path: Path) -> bool:
        resolved = path.resolve()
        return any(cwd == resolved or resolved in cwd.parents for cwd in protected)

    result = scratch.end(task, keep=retained)
    for folder in result.kept:
        print(f"remove_run_scratch: kept {folder.path}: {folder.reason}", file=sys.stderr)
    return len(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("runs", nargs="*", help="run folders whose scratch copies go")
    _ = parser.add_argument("--tracked", action="store_true", help="also end tracked scratch for these runs")
    args = parser.parse_args()
    runs = [Path(run) for run in cast("list[str]", args.runs)]
    failed = 0
    for run in runs:
        removed = 0
        try:
            _ = scratch.recover_folder_operations((run,))
        except (OSError, scratch.ScratchError) as error:
            failed += 1
            print(f"remove_run_scratch: could not recover {run}: {error}", file=sys.stderr)
            continue
        for folder in scratch_folders(run):
            try:
                remove(folder)
            except (OSError, scratch.ScratchError) as error:
                failed += 1
                print(f"remove_run_scratch: could not remove {folder}: {error}", file=sys.stderr)
                continue
            removed += 1
        if cast(bool, args.tracked):
            removed += end_tracked_scratch(run)
        if removed:
            print(f"remove_run_scratch: removed {removed} scratch folders from {run}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
