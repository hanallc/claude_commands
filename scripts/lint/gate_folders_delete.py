#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Remove free legacy gate folders after Git rescue."""

from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root:
        exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from scripts.lib import build_folders, git_files, git_rescue, scratch
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
class KeptSidecarConflict:
    path: Path


@dataclass(frozen=True)
class KeptFilesystemError:
    path: Path
    error: str


FolderDecision = (Deleted | WouldDelete | KeptBusy | KeptRepository | KeptRescueFailure
                  | KeptRootEntry | KeptNotFolder | KeptSidecarConflict | KeptFilesystemError)


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
    elif isinstance(decision, KeptSidecarConflict):
        reason = "unverified lease sidecars"
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


def prune_sources(sources: set[Path], lock_fds: tuple[int, ...]) -> tuple[PruneFailure, ...]:
    """Prune each source once after its linked worktrees are deleted."""
    failures: list[PruneFailure] = []
    for source in sorted(sources):
        try:
            result = subprocess.run(["git", "-C", str(source), "worktree", "prune", "--expire", "now"],
                                    capture_output=True, text=True, errors="replace", check=False, pass_fds=lock_fds)
        except OSError as error:
            failures.append(PruneFailure(source, str(error)))
            continue
        if result.returncode:
            failures.append(PruneFailure(source, result.stderr.strip() or f"git exited {result.returncode}"))
    return tuple(failures)


@dataclass(frozen=True)
class ExistingGate:
    identity: os.stat_result


@dataclass(frozen=True)
class AbsentGate:
    """The gate folder is absent and only the selected sidecars remain."""


@dataclass(frozen=True)
class GateGroup:
    path: Path
    source: ExistingGate | AbsentGate
    sidecar_names: tuple[str, ...]

    def check_source(self, directory: int) -> None:
        """Check that the source still matches the discovered folder."""
        if not os.path.samestat(self.path.parent.stat(follow_symlinks=False), os.fstat(directory)):
            raise build_folders.RemovalPathChanged(f"{self.path}: the gate parent changed")
        try:
            current = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            if isinstance(self.source, AbsentGate):
                return
        else:
            if isinstance(self.source, ExistingGate) and os.path.samestat(current, self.source.identity):
                return
        raise build_folders.RemovalPathChanged(f"{self.path}: the gate folder changed")


def gate_groups(repository: Path, directory: int, entries: set[str]) -> list[GateGroup | KeptNotFolder]:
    """Group sidecar names with an existing folder or a gate named in a lease."""
    sidecars: dict[str, list[str]] = {}
    sources: dict[str, ExistingGate | AbsentGate] = {}
    for name in sorted(entries):
        candidate = build_folders.classify_removal_target(name)
        if isinstance(candidate, build_folders.UnverifiedSidecarName):
            sidecars.setdefault(candidate.folder, []).append(name)
        elif isinstance(candidate, build_folders.FolderTarget):
            status = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                sources[name] = ExistingGate(status)
    for folder, names in sidecars.items():
        if folder not in entries:
            group = build_folders.classify_removal_target(folder, directory=directory, sidecars=names,
                                                          ownership=build_folders.SidecarOwnership.GATE)
            if isinstance(group, build_folders.VerifiedSidecarGroup):
                sources[folder] = AbsentGate()

    groups = [GateGroup(repository / name, source, tuple(sidecars.get(name, ())))
              for name, source in sources.items()]
    assigned = set(sources)
    assigned.update(name for group in groups for name in group.sidecar_names)
    return sorted([*groups, *(KeptNotFolder(repository / name) for name in entries - assigned)],
                  key=lambda item: item.path)


@dataclass(frozen=True)
class PreparedGateRemoval:
    group: GateGroup
    sidecars: build_folders.VerifiedRemovalSidecars
    lease: build_folders.HeldFolderRemovalLease | build_folders.UnreservedRemovalFolder
    contents: GateContents
    sources: set[Path]
    lock_fds: tuple[int, ...]

    def consumed_names(self) -> tuple[str, ...]:
        """List the repository entries that removal consumes."""
        sidecars = tuple(name for name, _ in self.sidecars.entries)
        if isinstance(self.group.source, ExistingGate):
            return (self.group.path.name, *sidecars)
        return sidecars


def prepare_gate(group: GateGroup, directory: int, locks: ExitStack, *,
                 dry_run: bool) -> PreparedGateRemoval | KeptSidecarConflict | KeptBusy | KeptRescueFailure | KeptRepository:
    """Verify the same removal group for the dry run and real run."""
    path = group.path
    verified = build_folders.classify_removal_target(path.name, directory=directory, sidecars=group.sidecar_names,
                                                     ownership=build_folders.SidecarOwnership.GATE)
    if isinstance(verified, build_folders.VerifiedSidecarGroup):
        sidecars = verified.sidecars
    elif isinstance(verified, build_folders.FolderTarget):
        sidecars = build_folders.VerifiedRemovalSidecars()
    else:
        return KeptSidecarConflict(path)
    lease = locks.enter_context(build_folders.folder_removal_lease(
        path, children=True, wait=not scratch.removal_mark(path).is_file(), dir_fd=directory))
    if isinstance(lease, build_folders.RefusedRemovalLease):
        return KeptBusy(path)

    names = build_folders.sidecar_names_for(path.name, os.listdir(directory))
    if names != group.sidecar_names:
        return KeptSidecarConflict(path)
    if isinstance(lease, build_folders.HeldFolderRemovalLease):
        lease.guard().check_path()
        lease.check_sidecars(sidecars)
        current = build_folders.classify_removal_target(path.name, directory=directory, sidecars=names,
                                                        ownership=build_folders.SidecarOwnership.GATE)
        if current != verified:
            return KeptSidecarConflict(path)
    elif sidecars.entries:
        return KeptSidecarConflict(path)
    group.check_source(directory)
    busy_fds: list[int] = []
    contents = GateContents(0, (), ())
    sources: set[Path] = set()
    if isinstance(group.source, ExistingGate):
        if not locks.enter_context(scratch.busy_locks(path, create=not dry_run, inherited_fds=busy_fds)):
            return KeptBusy(path)
        contents = inspect_folder(path)
        if contents.repositories:
            return KeptRepository(path)
        for worktree in contents.worktrees:
            try:
                source = git_files.at(worktree).common_dir
            except git_files.GitFilesError as error:
                return KeptRescueFailure(path, str(error))
            if source.is_relative_to(path.resolve()):
                return KeptRepository(path)
            sources.add(source)
    return PreparedGateRemoval(group, sidecars, lease, contents, sources,
                               (*build_folders.held_build_folder_descriptors(), *busy_fds))


def remove_prepared_gate(prepared: PreparedGateRemoval, directory: int) -> FolderDecision:
    """Remove the prepared source and its verified sidecars."""
    group, lease, contents = prepared.group, prepared.lease, prepared.contents
    path = group.path
    failures: list[PruneFailure] = []

    def cleanup(guard: build_folders.FolderRemovalGuard | None) -> build_folders.CleanupCompleted:
        for worktree in contents.worktrees:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            if guard is not None:
                guard.check_path()
            _ = git_rescue.rescue(worktree, f"{worktree.name}-{stamp}", generated=[worktree / "target"],
                                  pass_fds=prepared.lock_fds)
        if isinstance(lease, build_folders.HeldFolderRemovalLease):
            lease.guard().check_path()
            lease.check_sidecars(prepared.sidecars)
        group.check_source(directory)
        if isinstance(group.source, ExistingGate):
            shutil.rmtree(path.name, dir_fd=directory)
        failures.extend(prune_sources(prepared.sources, prepared.lock_fds))
        return build_folders.CleanupCompleted()

    if isinstance(lease, build_folders.HeldFolderRemovalLease):
        result = lease.remove(cleanup, sidecars=prepared.sidecars)
        if isinstance(result, build_folders.RefusedRemovalLease):
            return KeptBusy(path)
        if isinstance(result, build_folders.IncompleteBuildFolderRemoval):
            return KeptFilesystemError(path, result.reason)
    else:
        _ = cleanup(None)
    if failures:
        return DeletedWithPruneFailures(path, contents.size, tuple(failures))
    return Deleted(path, contents.size)


def decide_gate(group: GateGroup, directory: int, remaining: set[str], dry_run: bool) -> FolderDecision:
    try:
        with ExitStack() as locks:
            prepared = prepare_gate(group, directory, locks, dry_run=dry_run)
            if not isinstance(prepared, PreparedGateRemoval):
                return prepared
            if dry_run:
                remaining.difference_update(prepared.consumed_names())
                return WouldDelete(group.path, prepared.contents.size)
            return remove_prepared_gate(prepared, directory)
    except git_rescue.RescueError as error:
        return KeptRescueFailure(group.path, str(error))
    except build_folders.RemovalPathChanged:
        return KeptBusy(group.path)
    except OSError as error:
        return KeptFilesystemError(group.path, str(error))


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
        try:
            directory = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                entries = set(os.listdir(directory))
                for group in gate_groups(repository, directory, entries):
                    report(decide_gate(group, directory, entries, dry_run) if isinstance(group, GateGroup) else group)
                if dry_run:
                    if not entries:
                        report(WouldDelete(repository, 0))
                elif not os.listdir(directory) and os.path.samestat(
                        repository.stat(follow_symlinks=False), os.fstat(directory)):
                    repository.rmdir()
                    report(Deleted(repository, 0))
            finally:
                os.close(directory)
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
