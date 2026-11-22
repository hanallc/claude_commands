"""Read repository identities without loading build record writers."""

from __future__ import annotations

from collections.abc import Callable
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


def git_origin(directory: str, *, git_dir: str = "") -> str | None:
    """Read the first origin URL with Git's configuration rules."""
    command = ["git", "-C", directory]
    if git_dir:
        command.append(f"--git-dir={git_dir}")
    remote = output([*command, "remote", "get-url", "origin"], directory, GIT_TIMEOUT_S)
    return remote.strip() if remote and remote.strip() else None


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
    remote = git_origin(directory)
    if remote is not None:
        return remote_identity(remote, directory)
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
    try:
        return str(git_files.locate(Path(directory)).common_dir)
    except git_files.GitFilesError:
        return None


def _git_dir_origin(common: str) -> str | None:
    return git_origin(os.path.dirname(common), git_dir=common)


def source_repository(common: str, *, read_origin: Callable[[str], str | None] = _git_dir_origin) -> tuple[str, str | None]:
    """Read the source Git folder and its origin through local clone links."""
    seen = {common}
    while True:
        borrowed = borrowed_git_dir(common)
        if borrowed is not None and borrowed not in seen:
            common = borrowed
            seen.add(common)
            continue
        origin = read_origin(common)
        source = local_origin_git_dir(origin) if origin else None
        if source is None or source in seen:
            return common, origin
        seen.add(source)
        common = source


def source_git_dir(common: str) -> str:
    """Follow local clone sources without a Git command."""
    source, _ = source_repository(common, read_origin=_stored_origin_url)
    return source


def borrowed_git_dir(common: str) -> str | None:
    try:
        with open(os.path.join(common, ALTERNATES_FILE), encoding="utf-8") as handle:
            objects = handle.readline().strip()
    except OSError:
        return None
    if not objects:
        return None
    return os.path.dirname(os.path.normpath(os.path.join(common, "objects", objects)))


def local_origin_git_dir(origin: str) -> str | None:
    """Return the shared Git folder when origin names a local directory."""
    url = origin.removeprefix("file://")
    return git_common_dir(url) if os.path.isabs(url) and os.path.isdir(url) else None


def _stored_origin_url(common: str) -> str | None:
    """Read origin from the repository configuration without a Git command."""
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
            return text.partition("=")[2].strip()
    return None
