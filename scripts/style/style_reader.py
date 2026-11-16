"""Build the local reader on use and read tree inventories and blob names."""

from __future__ import annotations

from array import array
from collections.abc import Generator, Iterator, Sequence
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
from typing import TYPE_CHECKING, BinaryIO, Literal, cast
from uuid import uuid4
import zlib

from scripts.style import style_config
from scripts.style.git_process import GitReadFailed, TreeEntry, run_git

if TYPE_CHECKING:
    from scripts.style import declarations, style_footprint


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


class UnreadableReason(Enum):
    """The unavailable blob or token state reported by the reader."""

    MISSING_BLOB = "missing_blob"
    NOT_UTF8 = "not_utf8"
    TOKEN_SPANS = "token_spans"


@dataclass(frozen=True)
class UnreadableFact:
    """The blob or requested token reading is unavailable."""

    reason: UnreadableReason


@dataclass(frozen=True)
class CodeIdentity:
    """The SHA-256 identity of code tokens and inner documentation comments."""

    digest: bytes


@dataclass(frozen=True)
class NoCrateLibrary:
    """The module move has no library name outside its crate."""


@dataclass(frozen=True)
class ModuleMove:
    """One module move with its resolved crate context."""

    old: str
    new: str
    file_module: style_footprint.CrateModule
    parent: tuple[str, ...]
    library: str | NoCrateLibrary

    def wire(self) -> dict[str, object]:
        """Write the context expected by the reader's module query."""
        return {"old": self.old, "new": self.new,
                "file_module": {"crate": list(self.file_module)} if isinstance(self.file_module, tuple) else "outside_crate",
                "parent": list(self.parent),
                "library": {"name": self.library} if isinstance(self.library, str) else "none"}


@dataclass(frozen=True)
class ModuleQuery:
    """The module moves to inspect within one blob and file path."""

    blob: str
    path: str
    moves: tuple[ModuleMove, ...]

    def wire(self) -> dict[str, object]:
        """Write one query without reading source in Python."""
        return {"blob": self.blob, "path": self.path, "moves": [move.wire() for move in self.moves]}


class ModuleSiteRefusal(Enum):
    """The reader cannot prove a module move's reference sites."""

    NO_RENAMED_TEXT = "no_renamed_text"
    UNCERTAIN_MODULE_PATH = "uncertain_module_path"


class ModulePathForm(Enum):
    """A module path that is implicit or cannot be resolved."""

    DEFAULT = "default"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ModuleDeclarationFact:
    """One module declaration in the reader's source order."""

    name: str
    scope: tuple[str, ...]
    path: str | ModulePathForm


@dataclass(frozen=True)
class ModuleScanFact:
    """The module declarations without a dependency on the scheduler."""

    declarations: tuple[ModuleDeclarationFact, ...]


@dataclass(frozen=True)
class BlobFacts:
    """Compressed facts and compact token positions for one Rust blob."""

    archived_words: bytes
    archived_kinds: bytes
    token_starts: array[int]
    token_lines: array[int]
    archived_declarations: bytes
    archived_name_sites: bytes
    archived_modules: bytes | UnreadableFact
    code: CodeIdentity | UnreadableFact

    def tokens(self) -> tuple[style_footprint.TokenSpan, ...]:
        """Expand tokens for the current consumer without retaining them."""
        from scripts.style import style_footprint
        words = cast(list[str], json.loads(zlib.decompress(self.archived_words)))
        kinds = cast(list[Literal["identifier", "literal", "punctuation", "unknown"]],
                     json.loads(zlib.decompress(self.archived_kinds)))
        return tuple(style_footprint.TokenSpan(style_footprint.Token(word, kind), start, start + len(word))
                     for word, kind, start in zip(words, kinds, self.token_starts, strict=True))

    def declared_file(self, text: str) -> declarations.DeclaredFile:
        """Restore the existing declaration type from the reader's compact facts."""
        from scripts.style import declarations
        try:
            value = _object(cast(object, json.loads(zlib.decompress(self.archived_declarations))))
            return declarations.decode_reader_fact(text, self.tokens(), value, tuple(self.token_lines))
        except (KeyError, TypeError, ValueError) as error:
            raise ReaderError(f"the reader returned invalid declaration facts: {error}") from error

    def name_sites(self, file: declarations.DeclaredFile) -> tuple[declarations.NameSites | declarations.UnreadableBody, ...]:
        """Restore body name sites in the declaration order."""
        try:
            from scripts.style import declarations
            readings: list[declarations.NameSites | declarations.UnreadableBody] = []
            values = _list(cast(object, json.loads(zlib.decompress(self.archived_name_sites))))
            for item, value in zip(file.declared_items, values, strict=True):
                body = declarations.item_body(file, item)
                if value == "unreadable" or isinstance(body, declarations.UnreadableBody):
                    readings.append(declarations.UnreadableBody())
                else:
                    sites = tuple((_integer(pair[0]), _integer(pair[1]))
                                  for pair in (_list(site) for site in _list(_object(value)["sites"])))
                    readings.append(declarations.NameSites("\n".join(body.lines), sites))
            return tuple(readings)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ReaderError(f"the reader returned invalid name sites: {error}") from error

    def module_scan(self) -> ModuleScanFact | UnreadableFact:
        """Decode module declarations without importing the refill scheduler."""
        try:
            if isinstance(self.archived_modules, UnreadableFact):
                return self.archived_modules
            value = _object(cast(object, json.loads(zlib.decompress(self.archived_modules))))
            found: list[ModuleDeclarationFact] = []
            for entry in _list(value["declarations"]):
                row = _object(entry)
                path = row["path"]
                location = ModulePathForm(path) if isinstance(path, str) else _text(_object(path)["literal"])
                found.append(ModuleDeclarationFact(_text(row["name"]),
                             tuple(_text(name) for name in _list(row["scope"])), location))
            return ModuleScanFact(tuple(found))
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ReaderError(f"the reader returned invalid module declarations: {error}") from error


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


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise ReaderError("the reader returned a field that is not an integer")
    return value


def _blob_facts(rows: dict[str, dict[str, object]]) -> BlobFacts | UnreadableFact:
    if "unreadable" in rows["tokens"]:
        return UnreadableFact(UnreadableReason(_text(rows["tokens"]["unreadable"])))
    words: list[str] = []
    kinds: list[str] = []
    starts = array("I")
    lines = array("I")
    for entry in _list(rows["tokens"]["value"]):
        token = _object(entry)
        words.append(_text(token["value"]))
        kinds.append(_text(token["kind"]))
        starts.append(_integer(token["start"]))
        lines.append(_integer(token["line"]))
    module = rows["module_scan"]
    code = rows["rust_code"]
    return BlobFacts(_archive(words), _archive(kinds), starts, lines,
                     _archive(_object(rows["declarations"]["value"])),
                     _archive(_list(rows["name_sites"]["value"])),
                     UnreadableFact(UnreadableReason(_text(module["unreadable"]))) if "unreadable" in module else _archive(_object(module["value"])),
                     UnreadableFact(UnreadableReason(_text(code["unreadable"]))) if "unreadable" in code
                     else CodeIdentity(bytes.fromhex(_text(code["value"]))))


def _archive(value: object) -> bytes:
    """Compress a fact without retaining its decoded values."""
    return zlib.compress(json.dumps(value, ensure_ascii=True).encode(), level=1)


def _reply_lines(output: BinaryIO) -> Iterator[dict[str, object]]:
    """Parse one reply line at a time."""
    for line in output:
        try:
            yield _object(cast(object, json.loads(line)))
        except (ValueError, UnicodeError) as error:
            raise ReaderError(f"the reader returned an invalid JSON line: {error}") from error


@dataclass(frozen=True)
class ReaderClient:
    """Execute one local binary without selecting or building another binary."""

    binary: Path

    @contextmanager
    def _request(self, repository: Path, **fields: object) -> Generator[Iterator[dict[str, object]]]:
        request: dict[str, object] = {"repository": str(repository), "commits": [], "facts": []}
        request.update(fields)
        # Files prevent request writes and stderr from blocking the reply stream.
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as errors:
            _ = source.write(json.dumps(request).encode())
            _ = source.seek(0)
            try:
                process = subprocess.Popen([str(self.binary)], stdin=source, stdout=subprocess.PIPE, stderr=errors)
            except OSError as error:
                raise ReaderError(f"cannot start the reader at {self.binary}: {error}") from error
            with process:
                output = cast(BinaryIO, process.stdout)
                try:
                    yield _reply_lines(output)
                finally:
                    # Drain replies after a consumer refuses a row, so the process can exit.
                    for line in output:
                        del line
                    status = process.wait()
                    if status:
                        _ = errors.seek(0)
                        raise ReaderError(f"reader exited {status}: {errors.read().decode(errors='replace').strip()}")

    def read_trees(self, repository: Path, commits: Sequence[str]) -> dict[str, TreeInventory]:
        """Read complete inventories, including directories, for the requested revisions."""
        inventories: dict[str, TreeInventory] = {}
        with self._request(repository, commits=list(commits), trees=True, extensions=[]) as rows:
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
        names: dict[str, frozenset[str] | UnreadableNames] = {}
        with self._request(repository, blobs=list(blobs), facts=["names"], name_word_class=word_class.value) as rows:
            for row in rows:
                blob = _text(row.get("blob"))
                if row.get("fact") != "names":
                    raise ReaderError("the reader returned a fact other than names")
                names[blob] = (UnreadableNames(_text(row["unreadable"])) if "unreadable" in row
                               else frozenset(_text(word) for word in _list(row.get("value"))))
        if set(names) != set(blobs):
            raise ReaderError("the reader did not return every requested blob")
        return names

    def read_blob_facts(self, repository: Path, blobs: Sequence[str], *,
                        commits: Sequence[str] = ()) -> dict[str, BlobFacts | UnreadableFact]:
        """Read all Rust facts for the supplied blobs and revisions through one process."""
        kinds = {"tokens", "declarations", "name_sites", "module_scan", "rust_code"}
        try:
            grouped: dict[str, dict[str, dict[str, object]]] = {}
            readings: dict[str, BlobFacts | UnreadableFact] = {}
            with self._request(repository, blobs=list(blobs), commits=list(commits), facts=sorted(kinds)) as rows:
                for row in rows:
                    blob = _text(row["blob"])
                    facts = grouped.setdefault(blob, {})
                    facts[_text(row["fact"])] = row
                    if set(facts) == kinds:
                        readings[blob] = _blob_facts(facts)
                        del grouped[blob]
            if not set(blobs).issubset(readings) or grouped:
                raise ReaderError("the reader did not return every requested blob fact")
            return readings
        except (KeyError, ValueError) as error:
            raise ReaderError(f"the reader returned invalid blob facts: {error}") from error

    def read_module_sites(self, repository: Path, queries: Sequence[ModuleQuery]
                          ) -> dict[tuple[str, str], tuple[bool | ModuleSiteRefusal, ...] | UnreadableFact]:
        """Read every module query through one process."""
        try:
            results: dict[tuple[str, str], tuple[bool | ModuleSiteRefusal, ...] | UnreadableFact] = {}
            with self._request(repository, module_sites=[query.wire() for query in queries]) as rows:
                for row in rows:
                    key = (_text(row["blob"]), _text(row["path"]))
                    if "unreadable" in row:
                        results[key] = UnreadableFact(UnreadableReason(_text(row["unreadable"])))
                    else:
                        values: list[bool | ModuleSiteRefusal] = []
                        for value in _list(row["value"]):
                            if isinstance(value, str):
                                values.append(ModuleSiteRefusal(value))
                            else:
                                sites = _object(value)["module_sites"]
                                if not isinstance(sites, bool):
                                    raise ReaderError("the reader returned module sites that are not boolean")
                                values.append(sites)
                        results[key] = tuple(values)
            if set(results) != {(query.blob, query.path) for query in queries}:
                raise ReaderError("the reader did not return every requested module query")
            return results
        except (KeyError, ValueError) as error:
            raise ReaderError(f"the reader returned invalid module sites: {error}") from error


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
    environment = {key: value for key, value in os.environ.items() if key != "CONF_DIR"}
    result = subprocess.run(command, cwd=CHECKOUT, capture_output=True, text=True, env=environment)
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


def read_blob_facts(repository: Path, blobs: Sequence[str], *,
                    commits: Sequence[str] = ()) -> dict[str, BlobFacts | UnreadableFact]:
    """Read blob facts with the selected client."""
    selected = _client.get()
    client = selected if isinstance(selected, ReaderClient) else ReaderClient(reader_binary())
    return client.read_blob_facts(repository, blobs, commits=commits)


def read_module_sites(repository: Path, queries: Sequence[ModuleQuery]
                      ) -> dict[tuple[str, str], tuple[bool | ModuleSiteRefusal, ...] | UnreadableFact]:
    """Read module queries with the selected client."""
    selected = _client.get()
    client = selected if isinstance(selected, ReaderClient) else ReaderClient(reader_binary())
    return client.read_module_sites(repository, queries)
