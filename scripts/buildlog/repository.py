"""Read repository identities without loading build record writers."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess

from scripts.buildlog import store
from scripts.lib import git_files

GIT_TIMEOUT_S = 5
MANIFEST_IDENTITY_PREFIX = "manifest-path:"

def output(args: list[str], cwd: str, timeout: float) -> str | None:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def remote_identity(remote: str, directory: str) -> str:
    """Normalize a remote URL or path to the repository key."""
    if "://" not in remote and ":" in remote and not remote.startswith(("/", ".")):
        host, path = remote.split(":", 1)
        hostname = host.rsplit("@", 1)[-1]
        if (hostname and (host == hostname or host == f"git@{hostname}")
                and hostname.isascii() and all(letter.isalnum() or letter in ".-" for letter in hostname)
                and not any(letter in path for letter in "?#\r\n\t")):
            return f"https://{hostname.lower()}{('/' + path).rstrip('/').removesuffix('.git')}"
        remote = f"ssh://{host}/{path}"
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(remote)
    if parsed.hostname:
        path = parsed.path.rstrip("/").removesuffix(".git")
        port = f":{parsed.port}" if parsed.port and parsed.port not in (22, 443) else ""
        return urlunsplit(("https", parsed.hostname.lower() + port, path, "", ""))
    path = Path(parsed.path if parsed.scheme == "file" else remote).expanduser()
    if not path.is_absolute():
        path = Path(directory) / path
    return str(path.resolve()).rstrip("/").removesuffix(".git")


def repository_key(directory: str) -> str:
    """Keep Git identities or use the nearest real manifest without Git."""
    folder = Path(directory).resolve()
    ceilings = {Path(value).resolve() for value in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if value}
    git_parents: list[Path] = []
    for parent in (folder, *folder.parents):
        if parent != folder and parent in ceilings:
            break
        git_parents.append(parent)
    if folder.is_dir() and not any((parent / ".git").exists() for parent in git_parents):
        for parent in (folder, *folder.parents):
            manifest = parent / "Cargo.toml"
            if manifest.is_file():
                return MANIFEST_IDENTITY_PREFIX + str(manifest.resolve())
    remote = output(["git", "-C", directory, "remote", "get-url", "origin"], directory, GIT_TIMEOUT_S)
    if remote and remote.strip():
        return remote_identity(remote.strip(), directory)
    return git_root_key(directory)


def git_root_key(directory: str) -> str:
    """Read the history key for a Git repository without an origin."""
    roots = output(["git", "-C", directory, "rev-list", "--max-parents=0", "--reverse", "HEAD"],
                   directory, GIT_TIMEOUT_S)
    return "git-root:" + roots.strip().splitlines()[0] if roots and roots.strip() else store.UNKNOWN_IDENTITY


GIT_DIR = ".git"
ALTERNATES_FILE = os.path.join("objects", "info", "alternates")


def repo_name(directory: str) -> str | None:
    """Read the repository name from its source Git folder.

    Worktrees share the same name. A local clone uses its source repository name.
    Read the Git files without a Git command.
    """
    common = git_common_dir(directory)
    if common is None:
        return None
    return os.path.basename(os.path.dirname(source_git_dir(common))) or None


def git_common_dir(directory: str) -> str | None:
    """Return the Git folder that the repository worktrees share."""
    checkout = _git_checkout(directory)
    return str(checkout.common_dir) if checkout is not None else None


def _git_checkout(directory: str) -> git_files.Checkout | None:
    try:
        return git_files.locate(Path(directory))
    except git_files.GitFilesError:
        return None


@dataclass(frozen=True)
class MacSourceRepository:
    """The source Git folder and key from one checkout origin read."""

    git_dir: str
    key: str
    checkout_origin: str = field(repr=False)


def mac_source_repository(worktree: str, common: str) -> MacSourceRepository:
    """Follow local clone origins from the checkout to the source repository."""
    checkout_origin = _mac_origin(worktree)
    origin = checkout_origin
    directory = worktree
    seen = {common}
    while origin:
        if _relative_local_origin(origin):
            directory = _relative_origin_directory(directory)
        source = _local_origin_checkout(origin, directory)
        if source is None or str(source.common_dir) in seen:
            return MacSourceRepository(common, remote_identity(origin, directory), checkout_origin)
        common = str(source.common_dir)
        seen.add(common)
        directory = str(source.toplevel)
        origin = _mac_origin(directory)
    return MacSourceRepository(common, "git-dir:" + common, checkout_origin)


def _relative_origin_directory(directory: str) -> str:
    checkout = git_files.locate(Path(directory))
    if checkout.git_dir == checkout.common_dir:
        return str(checkout.toplevel)
    from scripts.lib.build_slots import SlotGitFailure, source_checkout

    try:
        return str(source_checkout(checkout.toplevel))
    except SlotGitFailure as error:
        raise ValueError(str(error)) from error


def _mac_origin(directory: str) -> str:
    result = subprocess.run(["git", "-C", directory, "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    if result.returncode == 2:
        return ""
    result.check_returncode()
    return result.stdout.strip()


def source_git_dir(common: str) -> str:
    """Follow local clone sources without a Git command."""
    seen = {common}
    while True:
        source = borrowed_git_dir(common) or _stored_local_origin_git_dir(common)
        if source is None or source in seen:
            return common
        seen.add(source)
        common = source


def borrowed_git_dir(common: str) -> str | None:
    try:
        with open(os.path.join(common, ALTERNATES_FILE), encoding="utf-8") as handle:
            objects = handle.readline().strip()
    except OSError:
        return None
    if not objects:
        return None
    return os.path.dirname(os.path.normpath(os.path.join(common, "objects", objects)))


def local_origin_git_dir(origin: str, directory: str = "") -> str | None:
    """Return the shared Git folder when origin names a local directory."""
    checkout = _local_origin_checkout(origin, directory)
    return str(checkout.common_dir) if checkout is not None else None


def _relative_local_origin(origin: str) -> bool:
    return ":" not in origin and not os.path.isabs(origin)


def _local_origin_checkout(origin: str, directory: str) -> git_files.Checkout | None:
    url = origin.removeprefix("file://")
    if directory and _relative_local_origin(origin):
        url = os.path.join(directory, url)
    return _git_checkout(url) if os.path.isabs(url) and os.path.isdir(url) else None


def _stored_local_origin_git_dir(common: str) -> str | None:
    """Read a local origin from the repository configuration without a Git command."""
    try:
        with open(os.path.join(common, "config"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            in_origin = text == '[remote "origin"]'
        elif in_origin and text.partition("=")[0].strip() == "url":
            return local_origin_git_dir(text.partition("=")[2].strip())
    return None
