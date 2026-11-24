"""Prepare a legacy workspace registration inside a test fixture."""
from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from scripts.copies import copies
from scripts.lib import build_folders, git_files, scratch


def registered_workspace(owner: str, repo: str | Path, commit: str, *, name: str, branch: str,
                         task: str = "", path: Path | None = None,
                         run_manifest: Path | None = None,
                         on_create: build_folders.FolderCreationNotice = "untracked") -> AbstractContextManager[Path]:
    """Resume an existing workspace or first supply a legacy record for the test."""
    if path is None:
        source = Path(repo).expanduser().resolve()
        for gate in copies.ACTIVE_GATES.get():
            if gate.tree == source:
                source = gate.source
                break
        tree = copies.workspace_path(owner, source, name)
        if not tree.exists():
            tree.mkdir(parents=True)
            if callable(on_create):
                on_create(tree, tree.lstat())
            scratch.register_copy(tree, task=task or f"gate:{name}", owner_unit=owner, source_path=str(source),
                                  source_commit=copies.commit_at(source, commit), copy_commit="", target_mode="none",
                                  seed_source="", borrows_from=str(git_files.at(source).common_dir),
                                  repository_identity=copies.repository_name(source),
                                  crate_run=str(run_manifest.resolve()) if run_manifest is not None else "")
    return copies.workspace(owner, repo, commit, name=name, branch=branch, task=task, path=path,
                            run_manifest=run_manifest)
