#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Remove free legacy gate folders after Git rescue."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root:
        exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from scripts.lib import git_files, git_rescue, scratch
from scripts.lint.sweep import gib


@dataclass(frozen=True)
class Deleted:
    path: Path
    size: int


@dataclass(frozen=True)
class PruneFailure:
    source: Path
    error: str


@dataclass(frozen=True)
class DeletedWithPruneFailures(Deleted):
    failures: tuple[PruneFailure, ...]


@dataclass(frozen=True)
class WouldDelete:
    path: Path
    size: int


@dataclass(frozen=True)
class KeptBusy:
    path: Path


@dataclass(frozen=True)
class KeptRepository:
    path: Path


@dataclass(frozen=True)
class KeptRescueFailure:
    path: Path
    error: str


@dataclass(frozen=True)
class KeptRootEntry:
    path: Path


@dataclass(frozen=True)
class KeptNotFolder:
    path: Path


@dataclass(frozen=True)
class KeptFilesystemError:
    path: Path
    error: str


FolderDecision = (Deleted | WouldDelete | KeptBusy | KeptRepository | KeptRescueFailure
                  | KeptRootEntry | KeptNotFolder | KeptFilesystemError)


def decision_line(decision: FolderDecision) -> str:
    if isinstance(decision, (Deleted, WouldDelete)):
        action = "deleted" if isinstance(decision, Deleted) else "would delete"
        line = f"{action} {decision.path}: {gib(decision.size)}"
        if isinstance(decision, DeletedWithPruneFailures):
            for failure in decision.failures:
                error = "; ".join(failure.error.splitlines())
                line += f"; prune failed {failure.source}: {error}"
        return line
    if isinstance(decision, KeptBusy):
        reason = "busy"
    elif isinstance(decision, KeptRepository):
        reason = "repository"
    elif isinstance(decision, KeptRescueFailure):
        reason = f"rescue failed: {decision.error}"
    elif isinstance(decision, KeptRootEntry):
        reason = "root entry"
    elif isinstance(decision, KeptNotFolder):
        reason = "not a folder"
    else:
        reason = decision.error
    return f"kept {decision.path}: {reason}"


@dataclass(frozen=True)
class GateContents:
    size: int
    worktrees: tuple[Path, ...]
    repositories: tuple[Path, ...]


def inspect_folder(path: Path) -> GateContents:
    """Measure entries and find Git markers without following symlinks."""
    size = 0
    worktrees: list[Path] = []
    repositories: list[Path] = []
    pending = [path]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                information = entry.stat(follow_symlinks=False)
                child = Path(entry.path)
                if entry.name == ".git":
                    if stat.S_ISREG(information.st_mode):
                        worktrees.append(folder)
                    else:
                        repositories.append(folder)
                if stat.S_ISDIR(information.st_mode):
                    if entry.name != ".git":
                        pending.append(child)
                else:
                    size += information.st_size
    return GateContents(size, tuple(sorted(worktrees)), tuple(sorted(repositories)))


def prune_sources(sources: set[Path]) -> tuple[PruneFailure, ...]:
    """Prune each source once after its linked worktrees are deleted."""
    failures: list[PruneFailure] = []
    for source in sorted(sources):
        try:
            result = subprocess.run(["git", "-C", str(source), "worktree", "prune", "--expire", "now"],
                                    capture_output=True, text=True, errors="replace", check=False)
        except OSError as error:
            failures.append(PruneFailure(source, str(error)))
            continue
        if result.returncode:
            failures.append(PruneFailure(source, result.stderr.strip() or f"git exited {result.returncode}"))
    return tuple(failures)


def remove_gate(path: Path, dry_run: bool) -> FolderDecision:
    try:
        with scratch.judgment_lock(path) if dry_run else scratch.removal_lock(path) as idle:
            if not idle:
                return KeptBusy(path)
            contents = inspect_folder(path)
            if contents.repositories:
                return KeptRepository(path)
            sources: set[Path] = set()
            for worktree in contents.worktrees:
                try:
                    source = git_files.at(worktree).common_dir
                except git_files.GitFilesError as error:
                    return KeptRescueFailure(path, str(error))
                if source.is_relative_to(path.resolve()):
                    return KeptRepository(path)
                sources.add(source)
            if dry_run:
                return WouldDelete(path, contents.size)
            for worktree in contents.worktrees:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                try:
                    _ = git_rescue.rescue(worktree, f"{worktree.name}-{stamp}", generated=[worktree / "target"])
                except git_rescue.RescueError as error:
                    return KeptRescueFailure(path, str(error))
            shutil.rmtree(path)
            failures = prune_sources(sources)
            if failures:
                return DeletedWithPruneFailures(path, contents.size, failures)
            return Deleted(path, contents.size)
    except OSError as error:
        return KeptFilesystemError(path, str(error))


def delete_gate_folders(root: Path, dry_run: bool = False) -> tuple[FolderDecision, ...]:
    """Print and return each gate decision and each empty repository removal."""
    root = root.absolute()
    decisions: list[FolderDecision] = []

    def report(decision: FolderDecision) -> None:
        decisions.append(decision)
        print(decision_line(decision))

    for repository in sorted(root.iterdir()):
        if repository.name.startswith(".") or repository.is_symlink() or not repository.is_dir():
            report(KeptRootEntry(repository))
            continue
        children = sorted(repository.iterdir())
        for gate in children:
            if gate.is_symlink() or not gate.is_dir():
                report(KeptNotFolder(gate))
            else:
                report(remove_gate(gate, dry_run))
        if dry_run:
            removable = {decision.path for decision in decisions if isinstance(decision, WouldDelete)}
            if all(child in removable for child in children):
                report(WouldDelete(repository, 0))
        else:
            try:
                if not any(repository.iterdir()):
                    repository.rmdir()
                    report(Deleted(repository, 0))
            except OSError as error:
                report(KeptFilesystemError(repository, str(error)))
    return tuple(decisions)


class GateFolderArguments(argparse.Namespace):
    root: Path = Path()
    dry_run: bool = False


def exit_status(decisions: tuple[FolderDecision, ...]) -> int:
    return int(any(isinstance(decision, (KeptRescueFailure, KeptFilesystemError, DeletedWithPruneFailures))
                   for decision in decisions))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", required=True, type=Path)
    _ = parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(namespace=GateFolderArguments())
    decisions = delete_gate_folders(arguments.root, arguments.dry_run)
    sys.exit(exit_status(decisions))


if __name__ == "__main__":
    main()
