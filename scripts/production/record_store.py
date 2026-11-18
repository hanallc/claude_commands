"""Read and atomically write numbered Markdown records under a staff owner."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import chain
from pathlib import Path
from typing import NamedTuple, cast

from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, prepared_replacement, sync_folder, write_bytes_atomic, write_text_atomic
from scripts.lib.file_lock import exclusive
from scripts.agents.remote import local_machine
from scripts.production import issues, staff
from scripts.production.issues import ALL_FIELDS, Issue, header_fields, source_paths
from scripts.production.production_doc import Refusal, production_field


class RecordText(NamedTuple):
    fields: dict[str, str]
    body: str


def state_root() -> Path:
    """Return the shared root for records owned by staff."""
    return Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")


class RecordKind(StrEnum):
    QUESTION = "question"
    PROPOSAL = "proposal"
    INVESTIGATION = "investigation"

    @property
    def prefix(self) -> str:
        return self.value[0].upper()


def record_owner(kind: RecordKind, issue: Issue, legacy_owner: str) -> staff.StaffId:
    """Use the recorded owner when the record has one."""
    field = "owner" if kind is RecordKind.INVESTIGATION else "production_id"
    return staff.StaffId(issue.value(field) or legacy_owner)


def _folder_registry(owner: str, kind: str) -> Path:
    return state_root() / "review-folders" / (owner or ".unowned") / (kind + ".json")


def _registered_folders(path: Path) -> tuple[Path, ...]:
    try:
        folders = cast(list[str], json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ()
    return tuple(Path(folder) for folder in folders)


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
    # A relative folder starts at the checkout that contains docs/plans.
    return folder if folder.is_absolute() else doc.parent.parent.parent / folder


def historical_issue_folders(doc: Path) -> Iterator[Path]:
    """Read previous Proposals settings from the document's Git history."""
    if not os.environ.get("GIT_DIR"):
        root = next((folder for folder in doc.resolve().parents
                     if (folder / ".git").exists() or (folder / "HEAD").is_file() and (folder / "objects").is_dir()), None)
        if root is None or root.name == ".git" and (root / "objects").is_dir():
            return
    try:
        result = subprocess.run(["git", "-C", str(doc.parent), "log", "--all", "--follow", "--format=", "--no-ext-diff",
                                 "--no-textconv", "-p", "--", doc.name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OSError(f"Cannot read folder history for {doc}: {error}") from error
    if result.returncode != 0:
        if "not a git repository" not in result.stderr:
            raise OSError(f"Cannot read folder history for {doc}: {result.stderr.strip()}")
        return
    prefix = f"- **{PROPOSALS_FIELD}:**"
    for line in result.stdout.split("\n"):
        if line.startswith(("+" + prefix, "-" + prefix)):
            if (folder := issues_folder(doc, lines=[line[1:]])) is not None:
                yield folder


def known_folders(kind: RecordKind, owner: staff.StaffId, current: Iterable[Path] = (),
                  *, document: Path | None = None, strict: bool = False,
                  include_history: bool = True, require_document: bool = True,
                  require_existing_document: bool = False, sources: list[Path] | None = None) -> tuple[Path, ...]:
    """Return current and former store roots. Use stored_paths to include their archives."""
    configured_roots = tuple(current)
    roots = list(configured_roots)
    if sources is not None:
        sources.append(_folder_registry(owner, kind))
    try:
        roots.extend(review_folders(owner, kind))
    except (OSError, ValueError):
        if strict:
            raise
    if kind is RecordKind.QUESTION and owner:
        roots.append(default_question_folder(owner))
    if kind is RecordKind.INVESTIGATION and owner:
        roots.append(state_root() / owner / "investigations")
    if document is not None and kind is not RecordKind.INVESTIGATION:
        require_existing = strict and require_existing_document
        try:
            read_document = require_existing or document.parent.is_dir()
        except OSError:
            if strict and require_document:
                raise
            read_document = False
        if read_document:
            if not configured_roots or kind is RecordKind.PROPOSAL or not require_document:
                try:
                    if (configured := issues_folder(document)) is not None:
                        roots.append(configured)
                except FileNotFoundError:
                    if require_existing:
                        raise
                except OSError:
                    if strict and require_document:
                        raise
            if include_history:
                try:
                    roots.extend(historical_issue_folders(document))
                except (OSError, UnicodeError):
                    if strict and require_document:
                        raise
    resolved: dict[Path, None] = {}
    for path in roots:
        try:
            resolved[path.resolve()] = None
        except (OSError, RuntimeError):
            if strict:
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
        first, _, tail = path.read_text(encoding="utf-8").partition("\n")
    except (OSError, UnicodeError) as error:
        raise OSError(f"Cannot read record {path}: {error}") from error
    if first.strip() == "---":
        header: list[str] = []
        for line in tail.split("\n"):
            if line.strip() == "---":
                return Issue(path, header_fields(header, keys))
            header.append(line)
    return Issue(path, {})


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


def owner_state_paths(root: Path, *names: str, strict: bool) -> Iterator[Path]:
    """List the named paths below each directory in the state root."""
    try:
        parents = tuple(root.iterdir())
    except FileNotFoundError:
        return
    except OSError:
        if strict:
            raise
        return
    for parent in parents:
        for name in names:
            path = parent / name
            try:
                _ = path.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                if strict:
                    raise
                continue
            yield path


def store_owners(kind: RecordKind, members: staff.StaffSnapshot | None = None, *, strict: bool = False
                 ) -> dict[staff.StaffId, staff.StaffRow | None]:
    """Find staff and former staff with local stores."""
    root = state_root()
    owners: dict[staff.StaffId, staff.StaffRow | None] = {}
    if members is not None:
        owners.update((row.id, row) for row in members.rows
                      if kind is not RecordKind.PROPOSAL or row.kind in ("showrunner", "producer"))
    for path in owner_state_paths(root / "review-folders", kind.value + ".json", strict=strict):
        _ = owners.setdefault(staff.StaffId("" if path.parent.name == ".unowned" else path.parent.name), None)
    if kind in (RecordKind.QUESTION, RecordKind.INVESTIGATION):
        for path in owner_state_paths(root, kind.value + "s", "archive", strict=strict):
            _ = owners.setdefault(staff.StaffId(path.parent.name), None)
    return owners


def local_document(row: staff.StaffRow | None) -> Path | None:
    """Identify the production document on this machine, if present."""
    doc = row.doc if row is not None and row.kind == "showrunner" else None
    return doc.path if isinstance(doc, (staff.LiveDoc, staff.WrappedDoc)) and doc.machine == local_machine() else None


def staff_store_folders(kind: RecordKind, *, members: staff.StaffSnapshot | None = None,
                        covered_owner: staff.StaffId | None = None) -> Iterator[Path]:
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
            yield from known_folders(kind, owner, document=local_document(row), strict=True)


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
            folders += tuple(staff_store_folders(self.kind, covered_owner=covered_owner))
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
              folder: Path | None = None, new_record: bool = False) -> Generator[None]:
        """Reserve the ID before the native write. Restore its bytes if the write fails."""
        try:
            root = (path.parent if folder is None else folder).resolve()
            ceiling = self.number(identity)
            if ceiling > self.high_water:
                sequence = _sequence_path(self.kind)
                write_text_atomic(sequence, f"{ceiling}\n")
                sync_folder(sequence.parent.parent)
                self.high_water = ceiling
            registry = _folder_registry(owner, self.kind)
            folders = _registered_folders(registry)
            with keep_file_on_error(registry):
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
def record_numbers(kind: RecordKind, read_id: Callable[[Path], str]) -> Generator[LockedRecordNumbers]:
    """Hold one sequence lock across its checks and native publication."""
    path = _sequence_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(path.with_suffix(".lock")):
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
    """Split at LF and keep each line ending."""
    *lines, tail = text.split("\n")
    return [*(line + "\n" for line in lines), *([tail] if tail else [])]


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

    def next_number(self) -> int:
        paths = source_paths(self.folder(), f"{self.prefix}*.md")
        return max((int(path.stem[len(self.prefix):]) for path in paths if self.valid_id(path.stem)), default=0) + 1

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
        try:
            if not text.startswith("---\n"):
                raise ValueError("missing frontmatter")
            header, body = text[4:].split("\n---\n\n", 1)
            fields: dict[str, str] = {}
            for line in header.split("\n"):
                key, value = line.split(": ", 1)
                decoded = cast(object, json.loads(value))
                if not isinstance(decoded, str) or key in fields:
                    raise ValueError("invalid field")
                fields[key] = decoded
            if fields["id"] != identity:
                raise ValueError("record ID differs from the selected ID")
            return RecordText(fields, body)
        except (ValueError, KeyError) as error:
            raise self.invalid(identity, error) from None

    def field(self, identity: str, name: str, *, path: Path | None = None) -> str:
        """Read one header field before loading the record body or log."""
        try:
            with (self.path(identity) if path is None else path).open(encoding="utf-8", newline="\n") as stream:
                if stream.readline() != "---\n":
                    raise ValueError("missing frontmatter")
                for line in stream:
                    if line.rstrip("\n") == "---":
                        break
                    if line.startswith(name+": "):
                        value = cast(object, json.loads(line[len(name)+2:]))
                        if isinstance(value, str):
                            return value
                        raise ValueError("invalid field")
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
