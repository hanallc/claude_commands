"""Compare a Git merge result with its target tree."""
from __future__ import annotations

from collections.abc import Callable
from subprocess import CompletedProcess
from typing import Protocol

from scripts.lib.process_failure import failure_detail


class GitRunner(Protocol):
    def __call__(self, *arguments: str) -> CompletedProcess[str]: ...


def leaves_target_unchanged(git: GitRunner, ref: str, target: str, *,
                            failure: Callable[[CompletedProcess[str]], str] = failure_detail) -> bool:
    """Return true when a clean merge gives the same tree as the target."""
    def commit(name: str) -> str:
        result = git("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if result.returncode:
            raise OSError(f"cannot resolve commit {name}: {failure(result)}")
        return result.stdout.strip()

    incoming = commit(ref)
    target_commit = commit(target)
    drivers = git("config", "--get-regexp", r"^merge\..+\.driver$")
    if drivers.returncode == 0:
        return False
    if drivers.returncode != 1:
        raise OSError(failure(drivers))

    merged = git("merge-tree", "--write-tree", target_commit, incoming)
    merged_tree = merged.stdout.partition("\n")[0]
    if merged.returncode:
        if merged.returncode == 1 and merged_tree:
            conflict_tree = git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{merged_tree}^{{tree}}")
            if conflict_tree.returncode == 0 and conflict_tree.stdout.strip() == merged_tree:
                return False
        raise OSError(failure(merged))
    current = git("rev-parse", f"{target_commit}^{{tree}}")
    if current.returncode:
        raise OSError(failure(current))
    return bool(merged_tree) and merged_tree == current.stdout.strip()
