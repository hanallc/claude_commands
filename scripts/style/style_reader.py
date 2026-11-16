"""Build the local reader on use and read tree inventories and blob names."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from typing import cast
from uuid import uuid4

from scripts.style import style_config
from scripts.style.git_process import GitReadFailed, TreeEntry, run_git


CHECKOUT = Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = Path.home() / ".claude/scripts/direct/verify.sh"
BUILD_ARGUMENTS = ("build", "style_reader", "--release", "--attached")


class ReaderError(RuntimeError):
    """The reader cannot build, execute, or provide the requested facts."""


class NameWordClass(Enum):
    """The character class of the caller's word search."""

    POSIX_ALNUM = "posix_alnum"
    ASCII = "ascii"


def names_word_class(locale: tuple[str, ...]) -> NameWordClass:
    """Select the word class from LC_ALL, LC_CTYPE, and LANG in priority order."""
    effective = next((value for value in locale if value), "C").lower().replace("-", "")
    return NameWordClass.POSIX_ALNUM if "utf8" in effective else NameWordClass.ASCII


@dataclass(frozen=True)
class TreeInventory:
    """The root tree and the complete inventory of one revision."""

    commit: str
    tree: str
    entries: dict[str, TreeEntry]


@dataclass(frozen=True)
class UnreadableNames:
    """The reason that a blob has no readable word set."""

    reason: str


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReaderError("the reader returned a JSON value that is not an object")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ReaderError("the reader returned a field that is not text")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ReaderError("the reader returned a field that is not a list")
    return cast(list[object], value)


@dataclass(frozen=True)
class ReaderClient:
    """Execute one local binary without selecting or building another binary."""

    binary: Path

    def _request(self, repository: Path, **fields: object) -> list[dict[str, object]]:
        request: dict[str, object] = {"repository": str(repository), "commits": [], "facts": []}
        request.update(fields)
        try:
            result = subprocess.run([str(self.binary)], input=json.dumps(request).encode(), capture_output=True)
        except OSError as error:
            raise ReaderError(f"cannot start the reader at {self.binary}: {error}") from error
        if result.returncode:
            raise ReaderError(f"reader exited {result.returncode}: {result.stderr.decode(errors='replace').strip()}")
        try:
            return [_object(cast(object, json.loads(line))) for line in result.stdout.splitlines()]
        except (ValueError, UnicodeError) as error:
            raise ReaderError(f"the reader returned an invalid JSON line: {error}") from error

    def read_trees(self, repository: Path, commits: Sequence[str]) -> dict[str, TreeInventory]:
        """Read complete inventories, including directories, for the requested revisions."""
        rows = self._request(repository, commits=list(commits), trees=True, extensions=[])
        inventories: dict[str, TreeInventory] = {}
        for row in rows:
            entries: dict[str, TreeEntry] = {}
            for value in _list(row.get("entries")):
                fields = tuple(_text(field) for field in _list(value))
                if len(fields) != 4:
                    raise ReaderError("the reader returned a tree entry without four fields")
                path, mode, kind, blob = fields
                entries[path] = TreeEntry(mode, kind, blob)
            commit = _text(row.get("commit"))
            inventories[commit] = TreeInventory(commit, _text(row.get("tree")), entries)
        if set(inventories) != set(commits):
            raise ReaderError("the reader did not return every requested tree")
        return inventories

    def read_names(self, repository: Path, blobs: Sequence[str],
                   word_class: NameWordClass = NameWordClass.POSIX_ALNUM) -> dict[str, frozenset[str] | UnreadableNames]:
        """Read all search-domain blobs through one reader process."""
        rows = self._request(repository, blobs=list(blobs), facts=["names"], name_word_class=word_class.value)
        names: dict[str, frozenset[str] | UnreadableNames] = {}
        for row in rows:
            blob = _text(row.get("blob"))
            if row.get("fact") != "names":
                raise ReaderError("the reader returned a fact other than names")
            names[blob] = (UnreadableNames(_text(row["unreadable"])) if "unreadable" in row
                           else frozenset(_text(word) for word in _list(row.get("value"))))
        if set(names) != set(blobs):
            raise ReaderError("the reader did not return every requested blob")
        return names


@dataclass(frozen=True)
class ReaderBuildNeeded:
    """The local installation does not match the requested reader source."""


def _installed(folder: Path, tree: str) -> Path | ReaderBuildNeeded:
    """Return a complete installation with the requested stamp."""
    current = (folder / "current").resolve()
    try:
        if (current / "stamp").read_text(encoding="ascii").strip() == tree:
            binary = current / "style_reader"
            if os.access(binary, os.X_OK):
                return binary
    except (OSError, UnicodeError):
        pass
    return ReaderBuildNeeded()


def _source_tree() -> str:
    """Read the source stamp from the current checkout."""
    return run_git(("git", "-C", str(CHECKOUT), "rev-parse", "HEAD:rust/style_reader")).stdout.decode().strip()


@dataclass(frozen=True)
class ReaderSourceChanged:
    """The build finished after the checkout changed to another source tree."""


def _build(folder: Path, tree: str) -> Path | ReaderSourceChanged:
    command = ["bash", str(VERIFY_SCRIPT), *BUILD_ARGUMENTS]
    if platform.system() == "Darwin":
        command.extend(("--mac", "build the local style reader"))
    result = subprocess.run(command, cwd=CHECKOUT, capture_output=True, text=True)
    if _source_tree() != tree:
        return ReaderSourceChanged()
    if result.returncode:
        raise ReaderError(f"reader build exited {result.returncode}: {result.stdout}\n{result.stderr}")
    paths = [line.removeprefix("binary: ") for line in result.stdout.splitlines() if line.startswith("binary: ")]
    if len(paths) != 1 or not Path(paths[0]).is_absolute():
        raise ReaderError(f"the reader build did not return one local binary path: {result.stdout}")
    with tempfile.TemporaryDirectory(prefix=".install-", dir=folder) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()
        _ = shutil.copy2(paths[0], staged / "style_reader")
        _ = (staged / "stamp").write_text(tree + "\n", encoding="ascii")
        installed = folder / f"{tree}-{uuid4().hex}"
        _ = staged.rename(installed)
        link = Path(temporary) / "current"
        link.symlink_to(installed.name, target_is_directory=True)
        os.replace(link, folder / "current")
    return installed / "style_reader"


def reader_binary() -> Path:
    """Resolve the source stamp and install one matching binary under the build lock."""
    try:
        tree = _source_tree()
        folder = style_config.reader_install_folder()
        installed = _installed(folder, tree)
        if isinstance(installed, Path):
            return installed
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "build.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            while True:
                tree = _source_tree()
                installed = _installed(folder, tree)
                if isinstance(installed, Path):
                    return installed
                built = _build(folder, tree)
                if isinstance(built, Path):
                    return built
    except (OSError, UnicodeError, GitReadFailed) as error:
        raise ReaderError(f"cannot resolve the local reader: {error}") from error


class ReaderSelection(Enum):
    """Resolve the installed reader when no caller supplies a binary."""

    INSTALLED = "installed"


_client: ContextVar[ReaderClient | ReaderSelection] = ContextVar("style_reader_client", default=ReaderSelection.INSTALLED)


@contextmanager
def use_client(client: ReaderClient) -> Generator[None]:
    """Use a resolved binary for this caller and restore the previous selection afterward."""
    token = _client.set(client)
    try:
        yield
    finally:
        _client.reset(token)


def read_trees(repository: Path, commits: Sequence[str]) -> dict[str, TreeInventory]:
    """Read the requested trees with the selected client."""
    selected = _client.get()
    client = selected if isinstance(selected, ReaderClient) else ReaderClient(reader_binary())
    return client.read_trees(repository, commits)


def read_names(repository: Path, blobs: Sequence[str],
               word_class: NameWordClass = NameWordClass.POSIX_ALNUM) -> dict[str, frozenset[str] | UnreadableNames]:
    """Read the requested blob names with the selected client."""
    selected = _client.get()
    client = selected if isinstance(selected, ReaderClient) else ReaderClient(reader_binary())
    return client.read_names(repository, blobs, word_class)
