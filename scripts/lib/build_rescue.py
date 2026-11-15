"""Save the Git work in a folder under refs/rescue/<name> in its source repository.

A rescue commit has this tree:

- worktree: every file of the checkout, ignored source included, and a gitlink for each nested repository
- index: the index entries, one folder for each stage, so a conflict keeps stages 1, 2 and 3
- refs: the HEAD and ref lines of the repository

Its parents are HEAD, the ref tips of a repository that does not share the source's objects,
and the rescue commit of each nested repository. A folder with a CACHEDIR.TAG file is
generated data (Cargo writes one in each target folder), so the rescue skips it.
"""
from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import shutil
import subprocess

RESCUE_REF_PREFIX = "refs/rescue/"
CACHE_TAG = "CACHEDIR.TAG"
_IDENTITY = {"GIT_AUTHOR_NAME": "build folders", "GIT_AUTHOR_EMAIL": "rescue@build-folders.invalid",
             "GIT_COMMITTER_NAME": "build folders", "GIT_COMMITTER_EMAIL": "rescue@build-folders.invalid"}
# Inherited values would point Git at another repository or index.
_REPOSITORY_ENVIRONMENT = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                           "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE")
_NO_MAINTENANCE = ("-c", "maintenance.auto=false", "-c", "gc.auto=0")


@dataclass(frozen=True)
class Rescued:
    ref: str
    commit: str


@dataclass(frozen=True)
class NothingToRescue:
    """The folder holds no repository."""


class RescueFailed(RuntimeError):
    """The rescue ref is not complete in the source repository; keep the folder."""


@dataclass(frozen=True)
class _Contents:
    caches: tuple[str, ...]
    repositories: tuple[str, ...]


def _environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in _REPOSITORY_ENVIRONMENT}
    return {**environment, **_IDENTITY, **(extra or {})}


def _git(repository: Path, *arguments: str, stdin: bytes | None = None,
         environment: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(["git", *_NO_MAINTENANCE, "-C", str(repository), *arguments], input=stdin,
                            capture_output=True, env=_environment(environment), check=False)
    if result.returncode != 0:
        raise RescueFailed(f"git {arguments[0]} in {repository}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode()


def _contents(folder: Path) -> _Contents:
    """Find generated cache folders and nested repositories below folder, without following links."""
    caches: list[str] = []
    repositories: list[str] = []
    for top, folders, files in os.walk(folder):
        here = Path(top)
        if here != folder and (".git" in folders or ".git" in files):
            repositories.append(here.relative_to(folder).as_posix())
            folders.clear()
            continue
        if CACHE_TAG in files:
            caches.append(here.relative_to(folder).as_posix())
            folders.clear()
            continue
        if here == folder and ".git" in folders:
            folders.remove(".git")
    return _Contents(tuple(caches), tuple(repositories))


def _is_repository(folder: Path) -> bool:
    return (folder / ".git").exists()


def _common_directory(repository: Path) -> Path:
    return Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()


def _head(repository: Path) -> tuple[str, str]:
    """Return HEAD's commit (empty when unborn) and its line for the refs blob."""
    commit = subprocess.run(["git", "-C", str(repository), "rev-parse", "-q", "--verify", "HEAD^{commit}"],
                            capture_output=True, text=True, env=_environment(), check=False).stdout.strip()
    branch = subprocess.run(["git", "-C", str(repository), "symbolic-ref", "-q", "HEAD"],
                            capture_output=True, text=True, env=_environment(), check=False).stdout.strip()
    return commit, f"{commit or '-'} HEAD{' -> ' + branch if branch else ''}"


def _ref_lines(repository: Path) -> list[tuple[str, str, str]]:
    """Return (object, peeled commit, name) for each ref outside refs/rescue."""
    rows: list[tuple[str, str, str]] = []
    output = _git(repository, "for-each-ref", "--format=%(objectname) %(*objectname) %(objecttype) %(*objecttype) %(refname)")
    for line in output.splitlines():
        parts = line.split(" ")
        name = parts[-1]
        if name.startswith(RESCUE_REF_PREFIX):
            continue
        objectname, peeled, kind, peeled_kind = parts[0], parts[1], parts[2], parts[3]
        commit = objectname if kind == "commit" else peeled if peeled_kind == "commit" else ""
        rows.append((objectname, commit, name))
    return rows


@contextmanager
def _scratch_index(repository: Path) -> Generator[Path]:
    """Name a private index file in the repository's Git folder and remove it after use."""
    path = Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-path",
                     f"rescue-index-{secrets.token_hex(8)}").strip())
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _write_index(repository: Path, entries: bytes) -> str:
    """Write a tree from NUL-ended index-info entries."""
    with _scratch_index(repository) as index:
        environment = {"GIT_INDEX_FILE": str(index)}
        _ = _git(repository, "update-index", "-z", "--index-info", stdin=entries, environment=environment)
        return _git(repository, "write-tree", environment=environment).strip()


def _stage_tree(repository: Path) -> str:
    """Write each index entry under <stage>/<path> at stage 0, so a conflict keeps its stages."""
    entries = bytearray()
    for record in _git(repository, "ls-files", "--stage", "-z").encode().split(b"\0"):
        if record:
            info, path = record.split(b"\t", 1)
            mode, objectname, stage = info.split(b" ")
            entries += mode + b" " + objectname + b" 0\t" + stage + b"/" + path + b"\0"
    return _write_index(repository, bytes(entries))


def _worktree_tree(repository: Path, contents: _Contents, nested: Mapping[str, str]) -> str:
    """Add every file, ignored ones included, to a copy of the index; skip caches; link nested rescues."""
    real = Path(_git(repository, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
    excluded = [f":(top,exclude,literal){path}" for path in (*contents.caches, *contents.repositories)]
    pathspec = "\0".join([":(top)", *excluded]).encode() + b"\0"
    links = b"".join(f"160000 {commit} 0\t{path}".encode() + b"\0" for path, commit in nested.items())
    with _scratch_index(repository) as index:
        if real.exists():
            _ = shutil.copyfile(real, index)
        environment = {"GIT_INDEX_FILE": str(index)}
        _ = _git(repository, "add", "--all", "--force", "--pathspec-from-file=-", "--pathspec-file-nul",
                 stdin=pathspec, environment=environment)
        if links:
            _ = _git(repository, "update-index", "-z", "--index-info", stdin=links, environment=environment)
        return _git(repository, "write-tree", environment=environment).strip()


def _commit(repository: Path, tree: str, parents: Sequence[str], message: str) -> str:
    unique: list[str] = []
    for parent in parents:
        if parent and parent not in unique:
            unique.append(parent)
    flags = [argument for parent in unique for argument in ("-p", parent)]
    return _git(repository, "commit-tree", "--no-gpg-sign", tree, *flags, stdin=message.encode()).strip()


def _create_ref(repository: Path, ref: str, commit: str) -> None:
    _ = _git(repository, "update-ref", ref, commit, "0" * len(commit))


def _delete_ref(repository: Path, ref: str) -> None:
    _ = subprocess.run(["git", "-C", str(repository), "update-ref", "-d", ref], capture_output=True,
                       env=_environment(), check=False)


def _move_ref(destination: Path, origin: Path, ref: str) -> None:
    """Copy ref and its objects from origin into destination, then delete it in origin."""
    if _common_directory(origin) == _common_directory(destination):
        return
    try:
        _ = _git(destination, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                 str(origin), f"{ref}:{ref}")
    finally:
        _delete_ref(origin, ref)


def _nested(destination: Path, folder: Path, relative: tuple[str, ...], ref: str) -> tuple[dict[str, str], list[str]]:
    """Rescue each repository below folder into destination under <ref>.n<position>; return links and refs."""
    links: dict[str, str] = {}
    refs: list[str] = []
    for position, path in enumerate(relative):
        child_ref = f"{ref}.n{position}"
        links[path] = _snapshot(folder / path, child_ref, False)
        _move_ref(destination, folder / path, child_ref)
        refs.append(child_ref)
    return links, refs


def _snapshot(folder: Path, ref: str, shared: bool) -> str:
    """Write folder's rescue commit and create ref at it in folder's repository; return the commit."""
    contents = _contents(folder)
    links, child_refs = _nested(folder, folder, contents.repositories, ref)
    head, head_line = _head(folder)
    refs = _ref_lines(folder)
    listing = "".join(f"{objectname} {name}\n" for objectname, _, name in refs)
    refs_blob = _git(folder, "hash-object", "-w", "--stdin", stdin=(head_line + "\n" + listing).encode()).strip()
    top = (f"040000 tree {_worktree_tree(folder, contents, links)}\tworktree\n"
           f"040000 tree {_stage_tree(folder)}\tindex\n"
           f"100644 blob {refs_blob}\trefs\n")
    tree = _git(folder, "mktree", stdin=top.encode()).strip()
    tips = [] if shared else [commit for _, commit, _ in refs]
    commit = _commit(folder, tree, [head, *tips, *links.values()], f"rescue {folder}")
    _create_ref(folder, ref, commit)
    for child_ref in child_refs:
        _delete_ref(folder, child_ref)
    return commit


def _links_commit(folder: Path, source: Path, ref: str) -> str | None:
    """Rescue the repositories inside a folder that is not one, under one commit in source."""
    repositories = _contents(folder).repositories
    if not repositories:
        return None
    links, child_refs = _nested(source, folder, repositories, ref)
    entries = b"".join(f"160000 {commit} 0\tworktree/{path}".encode() + b"\0" for path, commit in links.items())
    commit = _commit(source, _write_index(source, entries), list(links.values()),
                     f"rescue {folder}")
    _create_ref(source, ref, commit)
    for child_ref in child_refs:
        _delete_ref(source, child_ref)
    return commit


def _verify(source: Path, ref: str, commit: str) -> None:
    named = _git(source, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if named != commit:
        raise RescueFailed(f"{ref} names {named}, not {commit}")
    missing = [line for line in _git(source, "rev-list", "--objects", "--missing=print", commit, "--not",
                                     f"--exclude={RESCUE_REF_PREFIX}*", "--all").splitlines()
               if line.startswith("?")]
    if missing:
        raise RescueFailed(f"{ref} lacks {len(missing)} objects in {source}")


def rescue_ref(name: str) -> str:
    """Make a unique rescue ref name from a caller name."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ref = f"{RESCUE_REF_PREFIX}{name}-{stamp}-{secrets.token_hex(3)}"
    if subprocess.run(["git", "check-ref-format", ref], env=_environment(), check=False).returncode != 0:
        raise ValueError(f"rescue name {name!r} does not make a valid ref")
    return ref


def rescue(folder: Path, source: Path, name: str) -> Rescued | NothingToRescue:
    """Save folder's Git work in source under a unique refs/rescue/<name>-<time>-<random>.

    Raise RescueFailed when the ref or any object it needs is not in source. The caller keeps the folder then.
    """
    folder = folder.resolve()
    source = source.resolve()
    ref = rescue_ref(name)
    if not _is_repository(folder):
        commit = _links_commit(folder, source, ref)
        if commit is None:
            return NothingToRescue()
    else:
        shared = _common_directory(folder) == _common_directory(source)
        commit = _snapshot(folder, ref, shared)
        if not shared:
            _move_ref(source, folder, ref)
    _verify(source, ref, commit)
    return Rescued(ref, commit)
