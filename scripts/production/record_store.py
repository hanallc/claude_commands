"""Read and atomically write numbered Markdown records under a staff owner."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections import OrderedDict
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from io import StringIO
from itertools import chain
from pathlib import Path
from threading import Lock
from typing import NamedTuple, cast

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, existing_writer_lock, prepared_replacement, sync_folder, write_bytes_atomic, write_text_atomic
from scripts.lib.file_lock import exclusive
from scripts.lib import conf, git_files
from scripts.lib.showrunner_state import state_root as state_root
from scripts.agents.remote import local_machine
from scripts.production import issues, staff
from scripts.production.issues import ALL_FIELDS, Issue, source_paths
from scripts.production.production_doc import (
    Refusal, document_history_file, former_live_documents, legacy_document_path,
    production_backup_checkout, production_field,
)

from scripts.production.production_names import BACKUP_DOCUMENT_DIRECTORY


class RecordText(NamedTuple):
    fields: dict[str, str]
    body: str


class RecordKind(StrEnum):
    QUESTION = "question"
    PROPOSAL = "proposal"
    INVESTIGATION = "investigation"

    @property
    def prefix(self) -> str:
        return self.value[0].upper()


def configured_list_producer(settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | None:
    """Read the producer that owns the List."""
    configured = settings.origin("page.list.owner", str)
    if isinstance(configured, conf.UnavailableValue):
        return None
    row = members.by_id(staff.StaffId(configured.value))
    return row if row is not None and row.kind == "producer" else None


def list_keeper(settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | None:
    """Find the active producer that owns the List."""
    row = configured_list_producer(settings, members)
    if (row is None or row.id not in members.rows_by_id or row.id in members.pending_retire
            or isinstance(row.exited, staff.Exited) or isinstance(row.doc, staff.WrappedDoc)
            or isinstance(row.worktree, staff.RemovedWorktree)):
        return None
    return row


def record_owner(kind: RecordKind, issue: Issue, legacy_owner: str) -> staff.StaffId:
    """Use the recorded owner when the record has one."""
    field = "owner" if kind is RecordKind.INVESTIGATION else "production_id"
    return staff.StaffId(issue.value(field) or legacy_owner)


def _folder_registry(owner: str, kind: str) -> Path:
    return state_root() / "review-folders" / (owner or ".unowned") / (kind + ".json")


def _registered_folders(path: Path, *, review_scan: bool = False) -> tuple[Path, ...]:
    try:
        with review_scan_input(path, enabled=review_scan, missing_ok=True):
            folders = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if review_scan and (not isinstance(folders, list) or not all(
                    isinstance(folder, str) and folder for folder in cast(list[object], folders))):
                raise ValueError("Expected a list of store paths.")
            return tuple(Path(folder) for folder in cast(list[str], folders))
    except FileNotFoundError:
        pass
    return ()


def review_folders(owner: staff.StaffId, kind: str) -> tuple[Path, ...]:
    """Read this owner's recorded native folders."""
    return _registered_folders(_folder_registry(owner, kind))


PROPOSALS_FIELD = "Proposals"


def issues_folder(doc: Path, *, lines: list[str] | None = None) -> Path | None:
    """Return the configured folder, or None when the document has no proposals field."""
    if lines is None:
        try:
            lines = doc.read_text(encoding="utf-8").splitlines()
        except UnicodeError as error:
            raise OSError(f"{doc}: {error}") from error
    try:
        folder = Path(production_field(lines, PROPOSALS_FIELD)).expanduser()
    except Refusal:
        return None
    if folder.is_absolute():
        return folder
    try:
        root = git_files.locate(doc.parent).toplevel
    except git_files.NotACheckout:
        root = doc.parent.parent.parent
    except git_files.GitFilesError as error:
        raise OSError(f"{doc}: cannot find the repository: {error}") from error
    return root / folder


FileStamp = tuple[int, int, int, int, int, int]
MISSING_STAMP: FileStamp = (0, 0, 0, 0, 0, 0)


def file_stamp(path: Path) -> FileStamp:
    """Stamp a file without its access time."""
    try:
        status = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return MISSING_STAMP
    return (status.st_mode, status.st_dev, status.st_ino, status.st_size,
            status.st_mtime_ns, status.st_ctime_ns)


@dataclass(frozen=True)
class _FolderHistory:
    query: Path
    environment: tuple[tuple[str, str], ...]
    stamps: dict[Path, FileStamp]
    folders: tuple[Path, ...]


_history_cache: OrderedDict[Path, _FolderHistory] = OrderedDict()
_history_lock = Lock()
_HISTORY_LIMIT = 128
_INCLUDE_SECTION = re.compile(r"^\[\s*include(?:if)?(?=[.\s\]])", re.IGNORECASE)
_INCLUDE_PATH = re.compile(r"^path\s*=\s*(.*)$", re.IGNORECASE)
_HISTORY_OVERRIDES = ("GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_SHALLOW_FILE",
                      "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def _config_includes(path: Path) -> Iterator[Path]:
    """Track every include, including paths under conditions that currently fail."""
    text = path.read_text(encoding="utf-8-sig", errors="surrogateescape")
    include = False
    for line in text.replace("\\\n", "").split("\n"):
        line = line.strip()
        if line.startswith("["):
            include = _INCLUDE_SECTION.match(line) is not None
            line = line.rpartition("]")[2].lstrip()
        if not include or (match := _INCLUDE_PATH.fullmatch(line)) is None:
            continue
        value = match[1]
        if re.search(r"\\[ntb]", value) or "%(" in value:
            raise ValueError("The Git include path needs Git to expand it.")
        lexer = shlex.shlex(value, posix=True)
        lexer.whitespace = ""
        lexer.whitespace_split = True
        lexer.commenters = "#;"
        location = "".join(lexer).strip()
        if location:
            yield path.parent / Path(location).expanduser()


def _history_inputs(root: Path, doc: Path, sources: list[Path] | None) -> dict[Path, FileStamp]:
    stamps: dict[Path, FileStamp] = {}

    def track(path: Path) -> FileStamp:
        if path not in stamps:
            if sources is not None:
                sources.append(path)
            stamps[path] = file_stamp(path)
        return stamps[path]

    def track_tree(path: Path) -> None:
        if not stat.S_ISDIR(track(path)[0]):
            return
        if path.is_symlink():
            raise ValueError("Git must read the linked ref directory.")
        for child in path.iterdir():
            if not child.name.endswith(".lock"):
                track_tree(child)

    # A new nested checkout changes the repository that Git finds.
    for parent in doc.parents:
        if parent == root:
            break
        _ = track(parent / ".git")
    marker = root / ".git"
    if marker.exists():
        if not marker.is_dir() or marker.is_symlink():
            _ = track(marker)
        checkout = git_files.at(root)
        git_dir, common = checkout.git_dir, checkout.common_dir
        _ = track(git_dir / "commondir")
        if git_files.at(root) != checkout:
            raise ValueError("The Git directory changed during the read.")
    else:
        _ = track(marker)
        git_dir = common = root
    for name in ("packed-refs", "shallow", "info/grafts"):
        _ = track(common / name)

    worktrees = common / "worktrees"
    directories = {common, git_dir}
    if stat.S_ISDIR(track(worktrees)[0]):
        directories.update(path for path in worktrees.iterdir() if path.is_dir())
    for directory in directories:
        for name in ("HEAD", "gitdir"):
            _ = track(directory / name)
        for name in ("refs", "reftable"):
            track_tree(directory / name)

    global_config = os.environ.get("GIT_CONFIG_GLOBAL")
    system_config = os.environ.get("GIT_CONFIG_SYSTEM")
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    configs = [common / "config", git_dir / "config.worktree"]
    configs.extend([doc.parent / global_config] if global_config else
                   [doc.parent / xdg_config / "git/config", Path.home() / ".gitconfig"])
    if system_config:
        configs.append(doc.parent / system_config)
    else:
        configs.append(Path("/etc/gitconfig"))
        if executable := shutil.which("git"):
            configs.append(Path(executable).resolve().parent.parent / "etc/gitconfig")
    seen: set[Path] = set()
    while configs:
        config = Path(os.path.abspath(configs.pop()))
        if config in seen:
            continue
        seen.add(config)
        if stat.S_ISREG(track(config)[0]):
            configs.extend(_config_includes(config))
    return stamps


def _stamps_match(stamps: Mapping[Path, FileStamp]) -> bool:
    try:
        return all(file_stamp(path) == stamp for path, stamp in stamps.items())
    except OSError:
        return False


def _read_folder_history(doc: Path, *, review_scan: bool) -> tuple[Path, ...] | None:
    return _read_folder_log(doc.parent, doc.name, doc, review_scan=review_scan)


def _read_folder_log(repository: Path, relative: str, doc: Path, *, review_scan: bool) -> tuple[Path, ...] | None:
    with review_scan_input(repository, enabled=review_scan):
        try:
            result = subprocess.run(["git", "-C", str(repository), "--literal-pathspecs",
                                     "log", "--all", "--follow", "--format=", "--no-ext-diff",
                                     "--no-textconv", "-p", "--", relative], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OSError(f"Cannot read folder history for {doc}: {error}") from error
        if result.returncode != 0:
            if "not a git repository" in result.stderr:
                return None
            raise OSError(f"Cannot read folder history for {doc}: {result.stderr.strip()}")
    prefix = f"- **{PROPOSALS_FIELD}:**"
    folders: list[Path] = []
    with review_scan_input(doc, enabled=review_scan):
        for line in result.stdout.split("\n"):
            if line.startswith(("+" + prefix, "-" + prefix)):
                if (folder := issues_folder(doc, lines=[line[1:]])) is not None:
                    folders.append(folder)
    return tuple(folders)


def historical_issue_folders(doc: Path, *, sources: list[Path] | None = None,
                             review_scan: bool = False) -> Iterator[Path]:
    """Reuse successful Git history until a repository input changes."""
    doc = legacy_document_path(doc)
    if os.environ.get("GIT_DIR"):
        yield from _read_folder_history(doc, review_scan=review_scan) or ()
        return
    with review_scan_input(doc, enabled=review_scan):
        resolved = doc.resolve()
    query = doc.absolute()
    environment = tuple(sorted((key, value) for key, value in os.environ.items()
                               if key.startswith("GIT_") or key in ("HOME", "PATH", "XDG_CONFIG_HOME")))
    with _history_lock:
        cached = _history_cache.get(resolved)
        if cached is not None:
            _history_cache.move_to_end(resolved)
    if cached is not None and cached.query == query and cached.environment == environment:
        if sources is not None:
            sources.extend(cached.stamps)
        if _stamps_match(cached.stamps):
            yield from cached.folders
            return

    with review_scan_input(resolved.parent, enabled=review_scan):
        root = next((folder for folder in resolved.parents
                     if (folder / ".git").exists() or (folder / "HEAD").is_file() and (folder / "objects").is_dir()), None)
        if root is None or root.name == ".git" and (root / "objects").is_dir():
            return
    stamps: dict[Path, FileStamp] = {}
    command_includes = any(key.startswith("GIT_CONFIG_KEY_") and value.lower().startswith(("include.", "includeif."))
                           or key == "GIT_CONFIG_PARAMETERS" and re.search(r"\binclude(?:if)?\.", value, re.IGNORECASE)
                           for key, value in environment)
    if not command_includes and not any(os.environ.get(name) for name in _HISTORY_OVERRIDES):
        try:
            stamps = _history_inputs(root, resolved, sources)
        except (OSError, RuntimeError, ValueError):
            # Git still reports the original result or error when metadata cannot be stamped.
            pass
    folders = _read_folder_history(doc, review_scan=review_scan)
    if folders is None:
        return
    if stamps and _stamps_match(stamps):
        with _history_lock:
            _history_cache[resolved] = _FolderHistory(query, environment, stamps, folders)
            _history_cache.move_to_end(resolved)
            if len(_history_cache) > _HISTORY_LIMIT:
                _ = _history_cache.popitem(last=False)
    yield from folders


_skipped_production_backups: ContextVar[set[Path]] = ContextVar("_skipped_production_backups")


@contextmanager
def collect_skipped_production_backups() -> Generator[set[Path]]:
    """Collect backup paths that Git did not recognize during this scan."""
    paths: set[Path] = set()
    scope = _skipped_production_backups.set(paths)
    try:
        yield paths
    finally:
        _skipped_production_backups.reset(scope)


def _backup_issue_folders(owner: staff.StaffId, doc: Path, sources: list[Path] | None, *,
                          review_scan: bool = False) -> tuple[Path, ...]:
    backup = production_backup_checkout(owner)
    if sources is not None:
        sources.append(backup / ".git")
    relative = (Path(BACKUP_DOCUMENT_DIRECTORY) / doc.name).as_posix()
    with review_scan_input(backup, enabled=review_scan):
        if not backup.is_dir():
            return ()
        try:
            if sources is not None:
                _ = _history_inputs(backup, backup / relative, sources)
            folders = _read_folder_log(backup, relative, doc, review_scan=review_scan)
            if folders is None:
                skipped = _skipped_production_backups.get(None)
                if skipped is not None:
                    skipped.add(backup)
                return ()
            return folders
        except (git_files.GitFilesError, ValueError) as error:
            raise OSError(f"{doc}: cannot find backup history: {error}") from error


def _document_rename_paths(document: Path, owner: staff.StaffId, sources: list[Path] | None, *,
                           review_scan: bool) -> tuple[Path, ...]:
    with review_scan_input(document, enabled=review_scan):
        try:
            checkout = git_files.locate(document.parent)
        except git_files.NotACheckout:
            return ()
        history = document_history_file(checkout, owner)
    if sources is not None:
        sources.append(history)
    with review_scan_input(history, enabled=review_scan):
        return former_live_documents(history)


def known_folders(kind: RecordKind, owner: staff.StaffId, current: Iterable[Path] = (),
                  *, document: Path | None = None, strict: bool = False,
                  include_history: bool = True, require_document: bool = True,
                  require_existing_document: bool = False, sources: list[Path] | None = None,
                  review_scan: bool = False) -> tuple[Path, ...]:
    """Return current and former store roots. Use stored_paths to include their archives."""
    configured_roots = tuple(current)
    roots = list(configured_roots)
    registry = _folder_registry(owner, kind)
    if sources is not None:
        sources.append(registry)
    try:
        roots.extend(_registered_folders(registry, review_scan=review_scan))
    except (OSError, ValueError):
        if strict or review_scan:
            raise
    if kind is RecordKind.QUESTION and owner:
        roots.append(default_question_folder(owner))
    if kind is RecordKind.INVESTIGATION and owner:
        roots.append(state_root() / owner / "investigations")
    if document is not None and kind is not RecordKind.INVESTIGATION:
        require_existing = strict and require_existing_document
        try:
            with review_scan_input(document.parent, enabled=review_scan):
                read_document = require_existing or document.parent.is_dir()
        except OSError:
            if review_scan or strict and require_document:
                raise
            read_document = False
        if read_document:
            if not configured_roots or kind is RecordKind.PROPOSAL or not require_document:
                try:
                    with review_scan_input(document, enabled=review_scan, missing_ok=not require_existing):
                        if (configured := issues_folder(document)) is not None:
                            roots.append(configured)
                except FileNotFoundError:
                    if require_existing:
                        raise
                except OSError:
                    if review_scan or strict and require_document:
                        raise
            if include_history:
                try:
                    history = _document_rename_paths(document, owner, sources, review_scan=review_scan) if owner else ()
                    if history and document in history:
                        roots.extend(historical_issue_folders(history[0], sources=sources, review_scan=review_scan))
                        for path in history:
                            roots.extend(_backup_issue_folders(owner, path, sources, review_scan=review_scan))
                    else:
                        roots.extend(historical_issue_folders(document, sources=sources, review_scan=review_scan))
                        if owner and legacy_document_path(document) != document:
                            roots.extend(_backup_issue_folders(owner, document, sources, review_scan=review_scan))
                except (OSError, UnicodeError, git_files.MissingReferencedGitDirectory):
                    if review_scan or strict and require_document:
                        raise
    resolved: dict[Path, None] = {}
    for path in roots:
        try:
            with review_scan_input(path, enabled=review_scan):
                resolved[path.resolve()] = None
        except (OSError, RuntimeError):
            if strict or review_scan:
                raise
    return tuple(resolved)


def default_question_folder(owner: staff.StaffId) -> Path:
    return state_root() / owner / "questions"


def _strict_paths(folder: Path, *, recursive: bool) -> Iterator[Path]:
    try:
        paths = list(folder.iterdir())
    except FileNotFoundError:
        return
    for path in paths:
        if not recursive and path.suffix != ".md":
            continue
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode) and recursive and not path.is_symlink():
            yield from _strict_paths(path, recursive=True)
        elif stat.S_ISREG(mode) and path.suffix == ".md":
            yield path


def archive_folders(folders: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(archive for folder in folders for archive in (folder / "archive", folder.parent / "archive")))


def stored_paths(folders: Iterable[Path], *, strict: bool = False,
                 sources: list[Path] | None = None) -> Iterator[Path]:
    """Find records in each named folder and its archives."""
    roots = tuple(dict.fromkeys(folders))
    archives = archive_folders(roots)
    if sources is not None:
        sources.extend((*roots, *archives))
    seen: set[Path] = set()
    for folder, recursive in chain(((root, False) for root in roots), ((archive, True) for archive in archives)):
        try:
            paths = _strict_paths(folder, recursive=recursive) if strict else folder.glob("**/*.md" if recursive else "*.md")
            for path in paths:
                if path in seen:
                    continue
                if not strict:
                    try:
                        if not path.is_file():
                            continue
                    except OSError:
                        continue
                seen.add(path)
                yield path
        except OSError:
            if strict:
                raise


def _configured_vault(stem: str, key: str) -> Iterator[Path]:
    path = conf.path(stem)
    with review_scan_input(path):
        settings = conf.read(stem)
        if not isinstance(settings.settings, dict):
            if not settings.settings.missing or not _review_input_absent(path):
                raise OSError("Cannot read the vault configuration.")
            if not settings.repository_allowed:
                return
        value = settings.text(key, recover_invalid=False)
        if not value:
            raise ValueError("Expected a vault path.")
        vault = Path(value).expanduser()
    yield _resolve_review_root(vault)


@dataclass(frozen=True)
class ReviewScanFolder:
    path: Path
    recursive: bool


class ReviewScanRefusal(OSError):
    """A discovery input that the reply proof move cannot read."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{str(path)!r}: Cannot read the Review discovery input.")


def _review_input_absent(path: Path) -> bool:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


@contextmanager
def review_scan_input(path: Path, *, enabled: bool = True, missing_ok: bool = False) -> Generator[None]:
    """Refuse a failed discovery read and omit the data from its error."""
    try:
        yield
    except ReviewScanRefusal:
        raise
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        if not enabled:
            raise
        if missing_ok and isinstance(error, (FileNotFoundError, staff.StaffAbsent)) and _review_input_absent(path):
            return
        refused = error.path if isinstance(error, git_files.UnreadableGitFiles) else path
        raise ReviewScanRefusal(refused) from None


def _resolve_review_root(path: Path) -> Path:
    with review_scan_input(path):
        return path.expanduser().resolve()


def _review_scan_entries(folder: Path) -> tuple[os.DirEntry[str], ...]:
    with os.scandir(folder) as stream:
        return tuple(stream)


def configured_review_vaults() -> tuple[Path, ...]:
    return (*_configured_vault("staff", "producer_folder_root"), *_configured_vault("issue_score", "vault_root"))


def local_review_staff() -> staff.StaffSnapshot:
    """Read the local staff record for Review discovery."""
    members = staff.StaffSnapshot(())
    with review_scan_input(staff.record_path(), missing_ok=True):
        members = staff.local_snapshot()
    return members


def _local_document_store_folders() -> Iterator[Path]:
    """Find current and former stores for local production documents."""
    for row in local_review_staff().rows:
        if (document := local_document(row)) is not None:
            yield from known_folders(RecordKind.QUESTION, row.id, document=document, review_scan=True)


def local_review_paths(extra_roots: Iterable[Path] = ()) -> Iterator[Path]:
    """Scan vaults and native, registered, declared, or explicit stores."""
    vaults = configured_review_vaults()
    state = state_root()
    native = owner_state_paths(state, "questions", "investigations", "proposals", review_scan=True)
    archives = owner_state_paths(state, "archive", review_scan=True)
    registrations = owner_state_paths(state / "review-folders", *(kind.value + ".json" for kind in RecordKind),
                                      review_scan=True)
    registered = (folder for registration in registrations for folder in _registered_folders(registration, review_scan=True))
    stores = tuple(dict.fromkeys(_resolve_review_root(path) for path in
                                 chain(native, registered, _local_document_store_folders(), extra_roots)))
    pending = [
        *(ReviewScanFolder(path, False) for path in stores),
        *(ReviewScanFolder(path, True) for path in (*archive_folders(stores), *map(_resolve_review_root, archives))),
        *(ReviewScanFolder(path, True) for path in vaults),
    ]
    visited: set[ReviewScanFolder] = set()
    notes: set[Path] = set()
    while pending:
        folder = pending.pop()
        if folder in visited:
            continue
        visited.add(folder)
        entries: tuple[os.DirEntry[str], ...] = ()
        with review_scan_input(folder.path, missing_ok=True):
            entries = _review_scan_entries(folder.path)
        for entry in entries:
            try:
                if folder.recursive and entry.name != ".git" and entry.is_dir(follow_symlinks=False):
                    pending.append(ReviewScanFolder(Path(entry.path), True))
                    continue
                if not entry.name.endswith(".md") or not entry.is_file(follow_symlinks=False):
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                raise ReviewScanRefusal(Path(entry.path)) from None
            path = Path(entry.path)
            if path not in notes:
                notes.add(path)
                yield path


@dataclass(frozen=True)
class VanishedReviewNote:
    """A writer removed the note before the migration could read it."""


def read_note_for_proof_move(path: Path) -> str | VanishedReviewNote:
    try:
        return path.read_text(encoding="utf-8", newline="")
    except FileNotFoundError:
        return VanishedReviewNote()


def read_header(path: Path, *, sources: list[Path] | None = None, keys: frozenset[str] = ALL_FIELDS,
                select: Callable[[Issue], bool] | None = None, strict: bool = False) -> dict[str, list[str]]:
    """Read a header. A source collector requires readable files."""
    if strict or (sources is not None and "archive" in path.parts):
        if sources is not None:
            sources.append(path)
        fields = number_fields(path, ALL_FIELDS if select is not None else keys).fields
        return fields if select is None or select(Issue(path, fields)) else {}
    return issues.frontmatter(path, sources=sources, keys=keys, select=select)


def number_fields(path: Path, keys: frozenset[str]) -> Issue:
    """Read every byte and report a file that cannot supply its fields."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise OSError(f"Cannot read record {path}: {error}") from error
    return record_text_fields(path, text, keys)


def record_text_fields(path: Path, text: str, keys: frozenset[str] = ALL_FIELDS) -> Issue:
    return Issue(path, issues.issue_text_header(text).decoded_fields(keys))


def _sequence_path(kind: RecordKind) -> Path:
    return state_root() / "record-ids" / (kind.value + ".water")


def _read_high_water(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="ascii"))
        if value < 1:
            raise ValueError("Expected a positive number")
        return value
    except FileNotFoundError:
        return 0
    except (ValueError, OSError) as error:
        raise OSError(f"Cannot read the ID sequence {path}: {error}") from error


def owner_state_paths(root: Path, *names: str, strict: bool = False, review_scan: bool = False) -> Iterator[Path]:
    """List the named paths below each directory in the state root."""
    parents: tuple[Path, ...] = ()
    try:
        with review_scan_input(root, enabled=review_scan, missing_ok=True):
            parents = tuple(root.iterdir())
    except FileNotFoundError:
        return
    except OSError:
        if strict or review_scan:
            raise
        return
    for parent in parents:
        for name in names:
            path = parent / name
            try:
                with review_scan_input(path, enabled=review_scan, missing_ok=True):
                    try:
                        _ = path.stat()
                    except NotADirectoryError:
                        continue
                    yield path
            except FileNotFoundError:
                continue
            except OSError:
                if strict or review_scan:
                    raise


def store_owners(kind: RecordKind, members: staff.StaffSnapshot | None = None, *, strict: bool = False,
                 review_scan: bool = False
                 ) -> dict[staff.StaffId, staff.StaffRow | None]:
    """Find staff and former staff with local stores."""
    root = state_root()
    owners: dict[staff.StaffId, staff.StaffRow | None] = {}
    if members is not None:
        owners.update((row.id, row) for row in members.rows
                      if kind is not RecordKind.PROPOSAL or row.kind in ("showrunner", "producer"))
    for path in owner_state_paths(root / "review-folders", kind.value + ".json", strict=strict, review_scan=review_scan):
        _ = owners.setdefault(staff.StaffId("" if path.parent.name == ".unowned" else path.parent.name), None)
    if kind in (RecordKind.QUESTION, RecordKind.INVESTIGATION):
        for path in owner_state_paths(root, kind.value + "s", "archive", strict=strict, review_scan=review_scan):
            _ = owners.setdefault(staff.StaffId(path.parent.name), None)
    return owners


def local_document(row: staff.StaffRow | None) -> Path | None:
    """Identify the production document on this machine, if present."""
    doc = row.doc if row is not None and row.kind == "showrunner" else None
    return doc.path if isinstance(doc, (staff.LiveDoc, staff.WrappedDoc)) and doc.machine == local_machine() else None


def staff_store_folders(kind: RecordKind, *, members: staff.StaffSnapshot | None = None,
                        covered_owner: staff.StaffId | None = None,
                        require_existing_document: bool = False) -> Iterator[Path]:
    """Find store roots for owners whose folders the caller still needs."""
    if members is None:
        if kind is RecordKind.INVESTIGATION:
            # Investigation folders are local. Their locations do not depend on production documents.
            try:
                members = staff.local_snapshot()
            except staff.StaffAbsent:
                members = staff.StaffSnapshot(())
        else:
            members = staff.initial_snapshot()
    owners = store_owners(kind, members, strict=True)
    for owner, row in owners.items():
        if owner != covered_owner:
            yield from known_folders(kind, owner, document=local_document(row), strict=True,
                                     require_existing_document=require_existing_document)


def _legacy_high_water(kind: RecordKind) -> int:
    return max((_read_high_water(path) for path in owner_state_paths(_sequence_path(kind).parent, kind.value, strict=True)), default=0)


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_bytes(path: Path, previous: bytes | None) -> None:
    if _file_bytes(path) == previous:
        return
    if previous is None:
        path.unlink()
        sync_folder(path.parent)
    else:
        write_bytes_atomic(path, previous)


@contextmanager
def keep_file_on_error(path: Path) -> Generator[None]:
    """Restore the previous contents when a write fails."""
    previous = _file_bytes(path)
    try:
        yield
    except Exception:
        _restore_bytes(path, previous)
        raise


class RecordAllocationRefused(OSError):
    """The allocator could not check all native record folders."""


class OccupiedRecordPath(OSError):
    """Another writer published at the destination first."""


class NumberedRecordUpdateNotSaved(OSError):
    """The previous numbered record is still visible after an unconfirmed write."""


@dataclass
class LockedRecordNumbers:
    """The machine-wide sequence for one kind under its publication lock."""

    kind: RecordKind
    read_id: Callable[[Path], str]
    high_water: int
    scanned_floor: int = -1

    def number(self, identity: str) -> int:
        match = re.fullmatch(self.kind.prefix + r"([1-9][0-9]*)", identity)
        return int(match[1]) if match else 0

    def floor(self, *folders: Path, covered_owner: staff.StaffId | None = None) -> int:
        """Read all stores. The supplied folders include the covered owner's roots."""
        if self.scanned_floor < 0:
            try:
                folders += tuple(staff_store_folders(self.kind, covered_owner=covered_owner,
                                                     require_existing_document=True))
            except staff.StaffUnreadable:
                raise
            except OSError as error:
                raise RecordAllocationRefused(f"Cannot check the record folders. {error}") from error
            self.scanned_floor = max(_legacy_high_water(self.kind),
                                     max((self.number(self.read_id(path))
                                          for path in stored_paths(folders, strict=True)), default=0))
        return max(self.high_water, self.scanned_floor)

    def next_id(self, *folders: Path, covered_owner: staff.StaffId | None = None) -> str:
        return f"{self.kind.prefix}{self.floor(*folders, covered_owner=covered_owner) + 1}"

    def next_id_after_collision(self, identity: str, path: Path) -> str:
        occupied = self.number(self.read_id(path)) if path.is_file() and not path.is_symlink() else 0
        if occupied < self.number(identity):
            return identity
        return f"{self.kind.prefix}{max(occupied, self.high_water) + 1}"

    @contextmanager
    def write(self, path: Path, identity: str, owner: staff.StaffId, *,
              folder: Path | None = None, new_record: bool = False, preview: bool = False,
              before_write: Callable[[], None] = lambda: None) -> Generator[None]:
        """Check the registry and reserve the ID unless this is a preview."""
        try:
            root = (path.parent if folder is None else folder).resolve()
            ceiling = self.number(identity)
            registry = _folder_registry(owner, self.kind)
            folders = _registered_folders(registry)
            if preview:
                _ = _file_bytes(registry)
                if not new_record:
                    _ = _file_bytes(path)
                yield
                return
            with keep_file_on_error(registry):
                before_write()
                if ceiling > self.high_water:
                    sequence = _sequence_path(self.kind)
                    write_text_atomic(sequence, f"{ceiling}\n")
                    sync_folder(sequence.parent.parent)
                    self.high_water = ceiling
                if root not in folders:
                    write_text_atomic(registry, json.dumps(list(map(str, (*folders, root)))) + "\n")
                    sync_folder(registry.parent.parent)
                    sync_folder(registry.parent.parent.parent)
                if new_record:
                    yield
                    return
                with keep_file_on_error(path):
                    yield
        except AtomicDurabilityUnconfirmed as error:
            if new_record:
                raise
            # A reservation can fail before the record write. A record write
            # that fails restores the previous contents before reaching here.
            raise NumberedRecordUpdateNotSaved(
                error.errno, "numbered record update did not stay saved", str(path)) from error


@contextmanager
def record_numbers(kind: RecordKind, read_id: Callable[[Path], str], *,
                   existing_only: bool = False) -> Generator[LockedRecordNumbers]:
    """Hold one sequence lock across its checks and native publication."""
    path = _sequence_path(kind)
    if not existing_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with existing_writer_lock(lock_path) if existing_only else exclusive(lock_path):
        yield LockedRecordNumbers(kind, read_id, _read_high_water(path))


def free_record_path(path: Path, reserved_names: Collection[str] = ()) -> Path:
    """Choose an unused filename in the requested folder."""
    candidate = path
    number = 2
    while candidate.name in reserved_names or candidate.exists() or candidate.is_symlink():
        candidate = path.with_stem(f"{path.stem}-{number}")
        number += 1
    return candidate


class ReusedReviewRecord(Exception):
    """The file at the destination already contains this Review request."""


class ReviewPublication(NamedTuple):
    request_id: str
    digest: str


def _review_publication(path: Path) -> ReviewPublication | None:
    issue = number_fields(path, frozenset({"registration", "review_submission", "id"}))
    try:
        if registration := issue.value("registration"):
            fields = cast(object, json.loads(registration))
            submission = cast(dict[str, object], fields).get("review_submission") if isinstance(fields, dict) else None
        elif raw := issue.value("review_submission"):
            if issue.value("id").startswith("I"):
                # NumberedRecords stores the JSON object as a JSON string.
                raw = cast(str, json.loads(f'"{raw}"'))
            submission = cast(object, json.loads(raw))
        else:
            return None
    except (ValueError, TypeError):
        return None
    if isinstance(submission, dict):
        fields = cast(dict[str, object], submission)
        identity, digest = fields.get("request_id"), fields.get("request_digest")
        if isinstance(identity, str) and isinstance(digest, str):
            return ReviewPublication(identity, digest)
    return None


def create_record_text(path: Path, text: str) -> None:
    """Publish a new record without replacing a peer's file."""
    with prepared_replacement(path, text, mode=0o600) as temporary:
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            if (path.is_file() and not path.is_symlink()
                    and (request := _review_publication(temporary)) is not None
                    and (existing := _review_publication(path)) is not None
                    and request.request_id == existing.request_id):
                if request.digest != existing.digest:
                    from scripts.review_links.new_item_dialog import SubmissionConflict

                    raise SubmissionConflict("This request ID already has different item data.") from error
                raise ReusedReviewRecord(str(path)) from error
            raise OccupiedRecordPath(str(path)) from error
        try:
            sync_folder(path.parent)
        except OSError:
            if not path.is_symlink() and path.exists() and path.samefile(temporary):
                path.unlink()
                sync_folder(path.parent)
            raise


def section(name: str) -> str:
    return f"\n\n## {name}\n\n"


def lines_with_endings(text: str) -> list[str]:
    """Split at CR or LF and keep each line ending."""
    return list(StringIO(text, newline=""))


def has_line_break(text: str) -> bool:
    """Check text that must fit on one display line."""
    return re.search(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]", text) is not None


def split_section(body: str, length: int, name: str) -> tuple[str, str]:
    """Split exact text without treating a heading inside it as a boundary."""
    marker = section(name)
    if length < 0 or body[length:length + len(marker)] != marker:
        raise ValueError(f"missing {name} section")
    return body[:length], body[length + len(marker):]


@dataclass(frozen=True)
class NumberedRecords:
    owner: staff.StaffId
    collection: str
    prefix: str
    noun: str
    error_type: type[ValueError]

    def valid_id(self, identity: str) -> bool:
        return re.fullmatch(re.escape(self.prefix) + r"[1-9][0-9]*", identity) is not None

    def folder(self) -> Path:
        if not self.owner or Path(self.owner).name != self.owner or self.owner in (".", ".."):
            raise self.error_type("invalid owner staff id")
        return state_root() / self.owner / self.collection

    def path(self, identity: str) -> Path:
        if not self.valid_id(identity):
            raise self.error_type(f"id must be {self.prefix} followed by a positive number")
        return self.folder() / f"{identity}.md"

    @contextmanager
    def locked(self, *, check: Callable[[], object] | None = None) -> Generator[None]:
        lock = self.lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        with exclusive(lock):
            if check is not None:
                _ = check()
            yield

    def lock_path(self) -> Path:
        return self.folder() / ".lock"

    def counter_path(self) -> Path:
        return self.folder() / ".next-number"

    def require_list_keeper(self) -> None:
        keeper = list_keeper(conf.read_or_repository("console"), staff.snapshot())
        if keeper is None:
            raise self.error_type("The List keeper is unavailable.")
        if self.collection != "list" or self.prefix != "L" or keeper.id != self.owner:
            raise self.error_type(f"only {keeper.name}'s List folder can allocate List numbers")

    @contextmanager
    def list_allocation(self) -> Generator[None]:
        """Check the keeper before creating or locking its folder."""
        with staff.supplied_snapshot(staff.snapshot()):
            self.require_list_keeper()
            root = state_root()
            root.mkdir(parents=True, exist_ok=True)
            with exclusive(root / ".list-allocation.lock"), self.locked(check=self.require_list_keeper):
                yield

    def _list_folders(self) -> Iterator[Path]:
        for owner in state_root().iterdir():
            if owner.is_dir() and not owner.is_symlink():
                folder = owner / self.collection
                if not folder.is_symlink():
                    yield folder

    def _previous_numbers(self) -> Iterator[int]:
        """Read record names and the migration log, including closed records."""
        for folder in self._list_folders():
            try:
                names = [path.name for path in folder.iterdir()]
            except FileNotFoundError:
                continue
            if ".migration-complete.json" in names:
                log = folder / ".migration-complete.json"
                try:
                    data = cast(object, json.loads(log.read_text(encoding="utf-8")))
                    if not isinstance(data, dict):
                        raise ValueError("expected an object")
                    records = cast(dict[str, object], data).get("records")
                    if not isinstance(records, dict):
                        raise ValueError("expected a record index")
                    names.extend(cast(dict[str, object], records))
                except ValueError as error:
                    raise self.error_type(f"{log}: invalid migration log: {error}") from error
            for name in names:
                identity = name.removesuffix(".md")
                if identity != name and self.valid_id(identity):
                    yield int(identity[len(self.prefix):])

    def _saved_next_number(self, path: Path) -> int:
        if path.is_symlink():
            raise self.error_type(f"{path}: expected a regular counter file")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return 0
        if re.fullmatch(r"[1-9][0-9]*", value) is None:
            raise self.error_type(f"{path}: invalid next number")
        return int(value)

    def next_number(self) -> int:
        """Read the next number under the store lock."""
        self.require_list_keeper()
        counter = self.counter_path()
        saved = self._saved_next_number(counter)
        former = (self._saved_next_number(folder / counter.name) for folder in self._list_folders()
                  if folder != counter.parent)
        number = max(saved or max(self._previous_numbers(), default=0) + 1, max(former, default=0))
        while os.path.lexists(self.path(f"{self.prefix}{number}")):
            number += 1
        return number

    def reserve_number(self, number: int) -> None:
        """Advance the counter before publishing a record under the same lock."""
        if number < self.next_number():
            raise self.error_type("the number is below the saved sequence")
        write_text_atomic(self.counter_path(), f"{number + 1}\n", new_file_mode=0o600)

    def invalid(self, identity: str, error: object) -> ValueError:
        return self.error_type(f"{identity}: invalid {self.noun}: {error}")

    def read(self, identity: str, *, sources: list[Path] | None = None, path: Path | None = None) -> RecordText:
        path = self.path(identity) if path is None else path
        if sources is not None:
            sources.append(path)
        try:
            with path.open(encoding="utf-8", newline="") as stream:
                text = stream.read()
        except FileNotFoundError:
            raise self.error_type(f"{identity}: no {self.noun} for owner {self.owner}") from None
        except UnicodeDecodeError as error:
            raise self.invalid(identity, error) from None
        return self.parse_text(identity, text)

    def parse_text(self, identity: str, text: str) -> RecordText:
        try:
            note = issues.located_numbered_note(text)
            record = RecordText(note.decoded_fields(), note.body)
            if record.fields["id"] != identity:
                raise ValueError("record ID differs from the selected ID")
            return record
        except (ValueError, KeyError) as error:
            raise self.invalid(identity, error) from None

    def field(self, identity: str, name: str, *, path: Path | None = None) -> str:
        """Read one header field before loading the record body or log."""
        try:
            with (self.path(identity) if path is None else path).open(encoding="utf-8", newline="\n") as stream:
                if stream.readline().rstrip("\r\n") != "---":
                    raise ValueError("missing frontmatter")
                for line in stream:
                    if line.rstrip("\r\n") == "---":
                        break
                    if line.startswith(name+": "):
                        return issues.decode_numbered_field(line[len(name)+2:])
            raise ValueError("missing "+name)
        except FileNotFoundError:
            raise self.error_type(f"{identity}: no {self.noun} for owner {self.owner}") from None
        except (ValueError, UnicodeError) as error:
            raise self.invalid(identity, error) from None

    def write(self, identity: str, fields: Mapping[str, str], body: str, *, path: Path | None = None,
              new_record: bool = False) -> None:
        header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
        write_text = create_record_text if new_record else write_text_atomic
        write_text(self.path(identity) if path is None else path, f"---\n{header}\n---\n\n{body}")

    def records[T](self, load: Callable[[str], T], *, sources: list[Path] | None = None,
                   states: tuple[str, ...] = ()) -> list[T]:
        items: list[T] = []
        for path in source_paths(self.folder(), f"{self.prefix}*.md", sources=sources):
            if sources is not None:
                sources.append(path)
            try:
                if not states or self.field(path.stem, "state") in states:
                    items.append(load(path.stem))
            except self.error_type as error:
                print(f"{self.noun}: {path}: {str(error).replace(chr(10), ' ')}", file=sys.stderr)
        return items
