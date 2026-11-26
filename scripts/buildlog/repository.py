"""Read repository identities without loading build record writers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess

from scripts.buildlog import store
from scripts.lib import git_files

GIT_TIMEOUT_S = 5
MANIFEST_IDENTITY_PREFIX = "manifest-path:"
LINT_COPY_FOLDER_STEM = "verify-lint-copy"
LINT_COPY_TREE_NAME = "tree"
LINT_COPY_REPOSITORY_KEY_FILE = "repository-key"
LINT_COPY_ALLOCATION_FILE = "allocation-id"
LINT_COPY_ALLOCATION_BYTES = 16


def lint_copy_folder_names(owner: str, allocation_id: str) -> tuple[str, str, str]:
    """Return the lint copy names in allocation order."""
    owner_folder = f"{LINT_COPY_FOLDER_STEM}-{owner}"
    return LINT_COPY_FOLDER_STEM, owner_folder, f"{owner_folder}-{allocation_id}"


_LINT_COPY_ALLOCATION_TOKEN = rf"[0-9a-f]{{{2 * LINT_COPY_ALLOCATION_BYTES}}}"
_LINT_COPY_FOLDER_NAME = re.compile("|".join(lint_copy_folder_names(
    r"(?:0|-?[1-9][0-9]*)", _LINT_COPY_ALLOCATION_TOKEN
)))


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


def _lint_copy_key(tree: Path) -> str | None:
    copy = tree.parent
    if tree.name != LINT_COPY_TREE_NAME or not _LINT_COPY_FOLDER_NAME.fullmatch(copy.name):
        return None
    marker = copy / LINT_COPY_REPOSITORY_KEY_FILE
    allocation = copy / LINT_COPY_ALLOCATION_FILE
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        if allocation.is_symlink() or not allocation.is_file():
            return None
        token = allocation.read_text(encoding="utf-8")
        if not re.fullmatch(_LINT_COPY_ALLOCATION_TOKEN, token):
            return None
        return marker.read_text(encoding="utf-8").removesuffix("\n") or None
    except (OSError, UnicodeError):
        return None


def repository_key(directory: str) -> str:
    """Find the closest Git checkout or lint copy. Use the manifest as a fallback."""
    folder = Path(directory).resolve()
    ceiling_paths = os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
    ceilings = {Path(value).resolve() for value in ceiling_paths if value}
    if folder.is_dir():
        parents = (folder, *folder.parents)
        search_git = True
        for parent in parents:
            if parent != folder and parent in ceilings:
                search_git = False
            if search_git and (parent / GIT_DIR).exists():
                break
            copy_key = _lint_copy_key(parent)
            if copy_key is not None:
                return copy_key
        else:
            for parent in parents:
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
    """The source Git folder and key from the shared origin."""

    git_dir: str
    key: str


class UnrecordedSourceCheckout:
    """Git gives the shared metadata path without the main checkout path."""


def mac_source_repository(worktree: str, common: str) -> MacSourceRepository | UnrecordedSourceCheckout:
    """Follow local clone origins from the checkout to the source repository."""
    origin = _mac_origin(Path(common))
    directory = worktree
    seen = {common}
    while origin:
        if _relative_local_origin(origin):
            source_directory = _relative_origin_directory(Path(common), directory)
            if isinstance(source_directory, UnrecordedSourceCheckout):
                return source_directory
            directory = source_directory
        source = _local_origin_checkout(origin, directory)
        if source is None or str(source.common_dir) in seen:
            return MacSourceRepository(common, remote_identity(origin, directory))
        common = str(source.common_dir)
        seen.add(common)
        directory = str(source.toplevel)
        origin = _mac_origin(source.common_dir)
    return MacSourceRepository(common, "git-dir:" + common)


def _relative_origin_directory(common: Path, worktree: str) -> str | UnrecordedSourceCheckout:
    # Resolve relative origins from the primary checkout.
    configured = _git_config(common, "--local", "--includes", "--default", str(common.parent), "--get", "core.worktree")
    configured.check_returncode()
    directory = (common / configured.stdout.removesuffix("\n")).resolve()
    try:
        source = git_files.at(directory)
    except git_files.NotACheckout:
        source = None
    if source is not None and source.git_dir == common and source.common_dir == common:
        return str(source.toplevel)
    current = git_files.locate(Path(worktree))
    if current.git_dir == common and current.common_dir == common:
        return str(current.toplevel)
    return UnrecordedSourceCheckout()


def _git_config(common: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Read Git configuration from the shared Git directory."""
    environment = {**os.environ, "GIT_CONFIG_COUNT": "0"}
    for name in ("GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS"):
        _ = environment.pop(name, None)
    return subprocess.run(
        ["git", "--git-dir", str(common), "config", *arguments],
        cwd=common, env=environment, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
    )


def _mac_rewrites_without_worktree(common: Path) -> str:
    from tempfile import NamedTemporaryFile

    paths: list[str] = []
    for variable in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
        result = subprocess.run(
            ["git", "--git-dir", os.devnull, "var", variable], cwd=common, capture_output=True,
            text=True, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
        if result.returncode != 1:
            result.check_returncode()
            paths.extend(result.stdout.removesuffix("\n").split("\n"))
    paths.append(str(common / "config"))

    # The config must support a second read for hasconfig includes.
    with NamedTemporaryFile(mode="w", encoding="utf-8") as configuration:
        _ = configuration.write("[include]\n")
        for path in paths:
            escaped = str(common / path).replace("\\", "\\\\").replace('"', '\\"')
            _ = configuration.write(f' path = "{escaped}"\n')
        configuration.flush()
        result = _git_config(common, "--file", configuration.name, "--includes", "--null",
                             "--show-scope", "--get-regexp", r"^url\..*\.insteadof$")
    if result.returncode == 1:
        return ""
    result.check_returncode()
    return result.stdout


def _mac_origin(common: Path) -> str:
    urls: list[str] = []
    if (common / "config.worktree").exists():
        # A worktree origin can activate a conditional include in another scope.
        result = _git_config(common, "--local", "--includes", "--null", "--get-all", "remote.origin.url")
        if result.returncode == 1:
            return ""
        result.check_returncode()
        urls.append(result.stdout.split("\0", 1)[0])
        fields = _mac_rewrites_without_worktree(common).split("\0")
        scopes = ("command",)
    else:
        result = _git_config(common, "--includes", "--null", "--show-scope",
                             "--get-regexp", r"^(remote\.origin\.url|url\..*\.insteadof)$")
        if result.returncode == 1:
            return ""
        result.check_returncode()
        fields = result.stdout.split("\0")
        scopes = ("system", "global", "local")
    rewrites: dict[str, list[str]] = {}
    for scope, entry in zip(fields[::2], fields[1::2]):
        if scope not in scopes:
            continue
        key, separator, value = entry.partition("\n")
        if not separator:
            continue
        if key == "remote.origin.url":
            if scope == "local":
                urls.append(value)
        else:
            base = key.removeprefix("url.").removesuffix(".insteadof")
            rewrites.setdefault(base, []).append(value)
    if not urls:
        return ""
    origin = urls[0]
    prefix, base = max(
        ((prefix, base) for base, prefixes in rewrites.items() for prefix in prefixes if origin.startswith(prefix)),
        key=lambda rewrite: len(rewrite[0]), default=("", ""),
    )
    return (base + origin[len(prefix):]).strip()


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
