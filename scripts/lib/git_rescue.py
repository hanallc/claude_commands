"""Save all source work in Git before a caller resets or removes a checkout."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from scripts.lib import git_files, git_index


_OPERATION_HEAD_FILES = (
    "MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
    "rebase-merge/orig-head", "rebase-apply/orig-head", "sequencer/head",
)


@dataclass(frozen=True)
class NothingToSave:
    """Source refs already retain the clean checkout."""


@dataclass(frozen=True)
class Saved:
    ref: str
    commit: str


@dataclass(frozen=True)
class _RescueCommit:
    commit: str
    tree: str


@dataclass(frozen=True)
class _NestedRescue:
    commit: str
    refs: dict[str, str]


RescueResult = NothingToSave | Saved


class RescueError(RuntimeError):
    """A rescue did not finish or could not be verified."""

    def __init__(self, command: Sequence[str], stderr: str) -> None:
        self.command: tuple[str, ...] = tuple(command)
        self.stderr: str = stderr
        super().__init__(f"{shlex.join(command)}: {stderr.strip()}")


def _git(work_tree: Path, *arguments: str, data: bytes = b"", index: Path | None = None,
         accepted: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
    command = ["git", "--no-optional-locks", "-C", str(work_tree), *arguments]
    environment = dict(os.environ)
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=environment, check=False)
    except OSError as error:
        raise RescueError(command, str(error)) from error
    if result.returncode not in accepted:
        raise RescueError(command, result.stderr.decode(errors="replace"))
    return result


def _oid(work_tree: Path, *arguments: str, data: bytes = b"", index: Path | None = None) -> str:
    return _git(work_tree, *arguments, data=data, index=index).stdout.decode().strip()


def _excluded(work_tree: Path, generated: Sequence[Path]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for folder in generated:
        absolute = folder if folder.is_absolute() else work_tree / folder
        relative = absolute.absolute().relative_to(work_tree)
        if not relative.parts or ".." in relative.parts:
            raise ValueError(f"generated folder must be inside the checkout: {folder}")
        paths.append(relative)
    return tuple(paths)


def _walk_error(error: OSError) -> None:
    raise error


def _nested_repositories(work_tree: Path, generated: tuple[Path, ...]) -> tuple[Path, ...]:
    nested: list[Path] = []
    for directory, folders, files in os.walk(work_tree, onerror=_walk_error):
        current = Path(directory)
        if current != work_tree and ".git" in (*folders, *files):
            nested.append(current.relative_to(work_tree))
            folders.clear()
            continue
        folders[:] = [name for name in folders if name != ".git"
                      and current.joinpath(name).relative_to(work_tree) not in generated]
    return tuple(nested)


def _status_has_source(work_tree: Path, status: bytes, generated: tuple[Path, ...]) -> bool:
    # Git still reports ignored folders when an exclusion pathspec removes them.
    for entry in status.split(b"\0"):
        if not entry:
            continue
        if not entry.startswith(b"! "):
            return True
        relative = Path(os.fsdecode(entry[2:]))
        if any(relative.is_relative_to(folder) for folder in generated):
            continue
        if not any(folder.is_relative_to(relative) for folder in generated):
            return True
        for directory, folders, files in os.walk(work_tree / relative, onerror=_walk_error):
            current = Path(directory)
            folders[:] = [name for name in folders
                          if current.joinpath(name).relative_to(work_tree) not in generated]
            if files:
                return True
    return False


def _operation_heads(checkout: git_files.Checkout) -> tuple[str, ...]:
    heads: list[str] = []
    for relative in _OPERATION_HEAD_FILES:
        path = checkout.git_dir / relative
        if path.exists():
            heads.extend(path.read_text().splitlines())
    todo = checkout.git_dir / "sequencer/todo"
    if todo.exists():
        for line in todo.read_text().splitlines():
            fields = line.split(maxsplit=2)
            if len(fields) >= 2 and fields[0] in ("pick", "revert"):
                heads.append(fields[1])
    return tuple(dict.fromkeys(heads))


def _commit(work_tree: Path, tree: str, parents: Sequence[str], message: str) -> str:
    arguments = ["commit-tree", tree]
    for parent in dict.fromkeys(parents):
        arguments.extend(("-p", parent))
    return _oid(work_tree, *arguments, data=(message + "\n").encode())


def _index_entries(work_tree: Path, index: Path, entries: Sequence[tuple[str, str, bytes]]) -> None:
    data = b"".join(mode.encode() + b" " + commit.encode() + b"\t" + path + b"\0"
                    for mode, commit, path in entries)
    if data:
        _ = _git(work_tree, "update-index", "-z", "--index-info", data=data, index=index)


def _snapshot(work_tree: Path, checkout: git_files.Checkout, head: str, generated: tuple[Path, ...],
              nested: dict[Path, _NestedRescue], temporary: Path) -> tuple[str, tuple[str, ...]]:
    staged = temporary / "staged"
    original = checkout.git_dir / "index"
    conflicts = _git(work_tree, "ls-files", "--unmerged", "-z").stdout
    metadata = (".rescue-conflicts",) if conflicts else ()
    if nested:
        metadata += (".rescue-nested",)
    if metadata:
        tracked = _git(work_tree, "ls-files", "-z", "--", *metadata).stdout
        if tracked or any((work_tree / path).exists() or (work_tree / path).is_symlink() for path in metadata):
            raise RescueError(("git", "-C", str(work_tree), "rescue"),
                              f"source occupies a required metadata path: {', '.join(metadata)}")
    parents = [head]
    if original.exists():
        git_index.private_copy(original, staged)
        if conflicts:
            paths = dict.fromkeys(record.split(b"\t", 1)[1] for record in conflicts.split(b"\0") if record)
            _ = _git(work_tree, "update-index", "--force-remove", "-z", "--stdin",
                     data=b"".join(path + b"\0" for path in paths), index=staged)
        staged_tree = _oid(work_tree, "write-tree", index=staged)
        if staged_tree != _oid(work_tree, "rev-parse", "HEAD^{tree}"):
            parents.append(_commit(work_tree, staged_tree, (head,), "Saved staged index"))
    parents.extend(_operation_heads(checkout))
    parents.extend(saved.commit for saved in nested.values())
    index = temporary / "working"
    _ = _git(work_tree, "read-tree", "--empty", index=index)
    exclusions = tuple(dict.fromkeys((*generated, *nested)))
    _ = _git(work_tree, "add", "--force", "--all", "--", ".",
             *(f":(top,exclude,literal){path.as_posix()}" for path in exclusions), index=index)
    entries: list[tuple[str, str, bytes]] = []
    for record in conflicts.split(b"\0"):
        if not record:
            continue
        header, path = record.split(b"\t", 1)
        mode, blob, stage = header.split()
        entries.append((mode.decode(), blob.decode(), b".rescue-conflicts/" + stage + b"/" + path))
    if nested:
        manifest = json.dumps({path.as_posix(): {"commit": saved.commit, "refs": saved.refs}
                               for path, saved in nested.items()}, sort_keys=True) + "\n"
        blob = _oid(work_tree, "hash-object", "-w", "--stdin", data=manifest.encode())
        entries.append(("100644", blob, b".rescue-nested"))
        entries.extend(("160000", saved.commit, os.fsencode(path)) for path, saved in nested.items())
    _index_entries(work_tree, index, entries)
    return _oid(work_tree, "write-tree", index=index), tuple(parents)


def _local_ref_tips(work_tree: Path) -> dict[str, str]:
    names = _oid(work_tree, "for-each-ref", "--format=%(refname)",
                 "refs/heads/", "refs/tags/", "refs/stash").splitlines()
    revisions = {name: name for name in names}
    if "refs/stash" in revisions:
        stashes = _oid(work_tree, "reflog", "show", "--format=%H", "refs/stash").splitlines()
        revisions.update({f"refs/stash@{{{index}}}": commit for index, commit in enumerate(stashes)})
    if not revisions:
        return {}
    peeled = _git(work_tree, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                  data="".join(f"{revision}^{{commit}}\n" for revision in revisions.values()).encode()).stdout.decode().splitlines()
    refs: dict[str, str] = {}
    for name, entry in zip(revisions, peeled, strict=True):
        commit, kind = entry.split()
        if kind != "commit":
            raise RescueError(("git", "cat-file", name + "^{commit}"), "local ref does not name a commit")
        refs[name] = commit
    return refs


def _build_rescue(work_tree: Path, name: str, checkout: git_files.Checkout, excluded: tuple[Path, ...],
                  nested_paths: tuple[Path, ...], ref_tips: Sequence[str] = ()) -> _RescueCommit:
    head = _oid(work_tree, "rev-parse", "HEAD")
    nested: dict[Path, _NestedRescue] = {}
    for relative in nested_paths:
        folder = work_tree / relative
        nested_generated = tuple(path.relative_to(relative) for path in excluded if path.is_relative_to(relative))
        refs = _local_ref_tips(folder)
        saved = _build_rescue(folder, name, git_files.at(folder), nested_generated,
                              _nested_repositories(folder, nested_generated), tuple(refs.values()))
        _ = _git(work_tree, "fetch", "--no-tags", "--no-write-fetch-head", str(folder),
                 *dict.fromkeys((saved.commit, *refs.values())))
        nested[relative] = _NestedRescue(saved.commit, refs)
    with tempfile.TemporaryDirectory(prefix="rescue-", dir=checkout.git_dir) as directory:
        tree, parents = _snapshot(work_tree, checkout, head, excluded, nested, Path(directory))
        if ref_tips:
            independent = set(_oid(work_tree, "merge-base", "--independent",
                                   *dict.fromkeys((*parents, *ref_tips))).splitlines())
            parents += tuple(tip for tip in dict.fromkeys(ref_tips) if tip in independent and tip not in parents)
        return _RescueCommit(_commit(work_tree, tree, parents, f"Rescue {name}"), tree)


def rescue(work_tree: Path, name: str, *, generated: Sequence[Path] = (),
           retain_local_refs: bool = False) -> RescueResult:
    """Retain work in the common object store without changing the checkout or its index."""
    work_tree = work_tree.absolute()
    command = ["git", "-C", str(work_tree), "rescue"]
    try:
        checkout = git_files.at(work_tree)
        excluded = _excluded(work_tree, generated)
        status = _git(work_tree, "status", "--porcelain=v2", "--untracked-files=all", "--ignored=matching", "-z",
                      "--", ".", *(f":(top,exclude,literal){path.as_posix()}" for path in excluded)).stdout
        unheld = _git(work_tree, "rev-list", "--max-count=1", "HEAD", "--not",
                      "--branches", "--tags", "--remotes").stdout
        nested_paths = _nested_repositories(work_tree, excluded)
        operation = any((checkout.git_dir / path).exists()
                        for path in (*_OPERATION_HEAD_FILES, "sequencer/todo", "rebase-merge", "rebase-apply"))
        flagged = any(entry[:1].islower() or entry[:1] == b"S"
                      for entry in _git(work_tree, "ls-files", "-v", "-z").stdout.split(b"\0") if entry)
        refs = _local_ref_tips(work_tree) if retain_local_refs else {}
        if (not refs and not _status_has_source(work_tree, status, excluded) and not unheld and not operation and not nested_paths
                and not flagged):
            return NothingToSave()
        base = f"refs/rescue/{name}"
        _ = _git(work_tree, "check-ref-format", base)
        saved = _build_rescue(work_tree, name, checkout, excluded, nested_paths, tuple(refs.values()))
        suffix = 1
        while True:
            ref = base if suffix == 1 else f"{base}-{suffix}"
            result = _git(work_tree, "show-ref", "--verify", "--quiet", ref, accepted=(0, 1))
            if result.returncode == 0:
                suffix += 1
                continue
            _ = _git(work_tree, "update-ref", ref, saved.commit, "0" * len(saved.commit))
            break
        _ = _git(work_tree, "cat-file", "-e", ref + "^{commit}")
        actual = _oid(work_tree, "rev-parse", ref + "^{tree}")
        if actual != saved.tree:
            raise RescueError(("git", "rev-parse", ref + "^{tree}"), f"expected tree {saved.tree}, found {actual}")
        return Saved(ref, saved.commit)
    except (OSError, ValueError) as error:
        raise RescueError(command, str(error)) from error
