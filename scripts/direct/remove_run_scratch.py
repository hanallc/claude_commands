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
    cargo target;
  * each generated assignment prompt that no current dispatch can use.

With --tracked it also ends tracked scratch for these runs, preserving live
command directories and the scratch registry's existing unsaved-work holds.

A checkout whose .git is a file is a linked worktree. It goes through its
source repo (git worktree remove), so the source keeps no stale entry; a stale
entry keeps the branch "already checked out" and the next git worktree add of
it refuses. When that fails, the folder is deleted and the source pruned.

It never descends into a folder it removes. A symbolic link is never followed:
it is not a folder to this walk, and shutil.rmtree unlinks a link it finds, so
nothing outside the run folder is reached. The run folder itself stays, with
its ledgers, logs, diffs and json.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

from scripts.direct import recorder_state, workers
from scripts.lib import git_files, scratch

VERIFY_CACHE = "verify_cache"
ASSIGNMENT_PROMPT_LOCK = "assignment_prompts.lock"
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


def remove(folder: Path) -> None:
    scratch.remove_marked(folder, source_deletion=scratch.SourceDeletion.RUN)


def remove_tree(folder: Path) -> None:
    """Remove one scratch folder; a linked worktree through its source repo, so no entry stays."""
    source = worktree_source(folder)
    # Twice --force also removes a locked worktree: the run that locked it has ended.
    if source is not None and git(source, "remove", "--force", "--force", str(folder)):
        return
    shutil.rmtree(folder)
    if source is not None:
        _ = git(source, "prune")


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


def remove_assignment_prompts(run: Path) -> None:
    """Keep prompts for current passes that can still accept a retry."""
    if run.is_symlink() or not run.is_dir():
        return
    phases = run / "phases"
    if phases.is_symlink() or not phases.is_dir():
        return
    with (run / ASSIGNMENT_PROMPT_LOCK).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = recorder_state.read_state(run / recorder_state.STATE_FILENAME)
        except FileNotFoundError:
            state = {}
        slots = state.get("pass", {})
        if not isinstance(slots, dict):
            raise ValueError("Invalid assignment pass records")
        slots = cast(dict[str, object], slots)
        passes = (slots,) if "instance_id" in slots else slots.values()
        retained = set[str]()
        for item in passes:
            if isinstance(item, dict):
                passed = cast(dict[str, object], item)
                if passed.get("status") in ("active", "refused", "completed"):
                    prompt = passed.get("prompt_file")
                    if isinstance(prompt, str):
                        retained.add(prompt)
        for phase in phases.iterdir():
            if phase.is_symlink() or not phase.is_dir():
                continue
            prompts = phase / "prompts"
            if prompts.is_symlink() or not prompts.is_dir():
                continue
            for path in prompts.iterdir():
                if str(path.resolve()) in retained:
                    continue
                if re.fullmatch(r"[0-9a-f]{64}\.txt", path.name) and not path.is_symlink() and path.is_file():
                    path.unlink(missing_ok=True)


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
            remove_assignment_prompts(run)
        except (OSError, ValueError, recorder_state.OldRecorderState) as error:
            failed += 1
            print(f"remove_run_scratch: could not remove assignment prompts from {run}: {error}", file=sys.stderr)
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
