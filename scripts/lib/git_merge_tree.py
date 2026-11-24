"""Compare a Git merge result with its target tree."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Protocol

from scripts.lib.process_failure import failure_detail


class GitRunner(Protocol):
    def __call__(self, *arguments: str) -> CompletedProcess[str]: ...


@dataclass(frozen=True)
class MergeTarget:
    commit: str
    tree: str | None


def merge_tree_allowed(git: GitRunner, failure: Callable[[CompletedProcess[str]], str]) -> bool:
    drivers = git("config", "--get-regexp", r"^merge\..+\.driver$")
    if drivers.returncode not in (0, 1):
        raise OSError(failure(drivers))
    return drivers.returncode == 1


def target_tree(git: GitRunner, commit: str, failure: Callable[[CompletedProcess[str]], str]) -> str:
    current = git("rev-parse", f"{commit}^{{tree}}")
    if current.returncode:
        raise OSError(failure(current))
    return current.stdout.strip()


def prepare_target(git: GitRunner, commit: str) -> MergeTarget:
    """Read the tree of a pinned commit if there are no custom merge drivers."""
    tree = target_tree(git, commit, failure_detail) if merge_tree_allowed(git, failure_detail) else None
    return MergeTarget(commit, tree)


def leaves_target_unchanged(git: GitRunner, ref: str, target: str | MergeTarget, *,
                            merge_base: str | None = None,
                            failure: Callable[[CompletedProcess[str]], str] = failure_detail) -> bool:
    """Return true when a clean merge gives the same tree as the target.

    For a prepared target, pass the full incoming commit hash.
    """
    def commit(name: str) -> str:
        result = git("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if result.returncode:
            raise OSError(f"cannot resolve commit {name}: {failure(result)}")
        return result.stdout.strip()

    if isinstance(target, MergeTarget):
        incoming, target_commit, current = ref, target.commit, target.tree
        if current is None:
            return False
    else:
        incoming, target_commit, current = commit(ref), commit(target), None
        if not merge_tree_allowed(git, failure):
            return False

    base_option = (f"--merge-base={merge_base}",) if merge_base is not None else ()
    merged = git("merge-tree", "--write-tree", *base_option, target_commit, incoming)
    merged_tree = merged.stdout.partition("\n")[0]
    if merged.returncode:
        if merged.returncode == 1 and merged_tree:
            conflict_tree = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{merged_tree}^{{tree}}")
            if conflict_tree.returncode == 0 and conflict_tree.stdout.strip() == merged_tree:
                return False
        raise OSError(failure(merged))
    if current is None:
        current = target_tree(git, target_commit, failure)
    return bool(merged_tree) and merged_tree == current
