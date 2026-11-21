#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""List free legacy gate folders for reviewed removal."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root:
        exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys

from scripts.lib import removal_guard, scratch
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


@dataclass(frozen=True)
class KeptByGuard:
    path: Path
    refusal: removal_guard.Refused


FolderDecision = (Deleted | WouldDelete | KeptBusy | KeptRepository | KeptRescueFailure
                  | KeptRootEntry | KeptNotFolder | KeptFilesystemError | KeptByGuard)


def decision_line(decision: FolderDecision) -> str:
    if isinstance(decision, (Deleted, WouldDelete)):
        action = "deleted" if isinstance(decision, Deleted) else "review"
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
    elif isinstance(decision, KeptByGuard):
        reason = str(decision.refusal)
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
                git_marker = entry.name.casefold() == ".git"
                if git_marker:
                    if entry.name == ".git" and stat.S_ISREG(information.st_mode):
                        git = scratch.git_directory(folder)
                        own_metadata = (isinstance(git, scratch.GitDirectory)
                                        and (git.checkout.git_dir == git.checkout.common_dir
                                             or git.checkout.common_dir.is_relative_to(path)))
                        (repositories if own_metadata else worktrees).append(folder)
                    else:
                        repositories.append(folder)
                if stat.S_ISDIR(information.st_mode):
                    if not git_marker:
                        pending.append(child)
                else:
                    size += information.st_size
    return GateContents(size, tuple(sorted(worktrees)), tuple(sorted(repositories)))


def assess_gate(path: Path) -> FolderDecision:
    try:
        with scratch.judgment_lock(path) as idle:
            if not idle:
                return KeptBusy(path)
            contents = inspect_folder(path)
            if contents.repositories:
                return KeptRepository(path)
            return WouldDelete(path, contents.size)
    except OSError as error:
        return KeptFilesystemError(path, str(error))


def delete_gate_folders(root: Path, dry_run: bool = False) -> tuple[FolderDecision, ...]:
    """Print each gate decision and publish the entries for review."""
    root = root.absolute()
    decisions: list[FolderDecision] = []
    plan = removal_guard.RemovalList(removal_guard.Tool.GATE)

    def report(decision: FolderDecision) -> None:
        decisions.append(decision)
        print(decision_line(decision))

    try:
        home = Path.home().resolve()
        if root.is_symlink() or home.is_relative_to(root.resolve()):
            report(KeptByGuard(root, removal_guard.Refused(removal_guard.RefusalReason.INVALID_ROOT)))
            return tuple(decisions)
        for repository in sorted(root.iterdir()):
            if repository.name.startswith(".") or repository.is_symlink() or not repository.is_dir():
                report(KeptRootEntry(repository))
                continue
            gates = sorted(repository.iterdir())
            if not gates:
                report(KeptByGuard(repository, removal_guard.Refused(removal_guard.RefusalReason.UNKNOWN_MAKER)))
            for gate in gates:
                if gate.is_symlink() or not gate.is_dir():
                    report(KeptNotFolder(gate))
                    continue
                decision = assess_gate(gate)
                report(decision)
                if isinstance(decision, WouldDelete) and not dry_run:
                    _ = plan.add_manual(removal_guard.Tool.GATE, gate, root, decision.size,
                                        "idle gate requires manual review")
    except removal_guard.Refused as refusal:
        report(KeptByGuard(root, refusal))
    except OSError as error:
        report(KeptFilesystemError(root, str(error)))
    if not dry_run:
        try:
            plan.report()
        except OSError as error:
            report(KeptFilesystemError(root, f"cannot write removal list: {error}"))
    return tuple(decisions)


class GateFolderArguments(argparse.Namespace):
    root: Path = Path()
    dry_run: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", required=True, type=Path)
    _ = parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(namespace=GateFolderArguments())
    _ = delete_gate_folders(arguments.root, arguments.dry_run)
    sys.exit(0)


if __name__ == "__main__":
    main()
