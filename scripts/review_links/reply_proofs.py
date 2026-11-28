"""Store private reply proofs and staff question request IDs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from functools import cache, cached_property
from pathlib import Path
from typing import BinaryIO, NewType, cast

from scripts.lib import atomic_file, git_files
from scripts.lib.file_lock import exclusive
from scripts.lib.showrunner_state import state_root
from scripts.production import issues
from scripts.review_links import reply_tokens


StaffQuestionRequestKey = NewType("StaffQuestionRequestKey", str)
ReplyWaitRevision = NewType("ReplyWaitRevision", str)
INITIAL_REPLY_WAIT = ReplyWaitRevision("")


@dataclass(frozen=True)
class NoStaffQuestionRequest:
    """The question was filed without a request key."""


NO_STAFF_REQUEST = NoStaffQuestionRequest()


@dataclass(frozen=True)
class SubmissionIdentity:
    """A Review item keeps this identity when it moves to another folder or owner."""

    number: str
    request_id: str
    revision: ReplyWaitRevision = INITIAL_REPLY_WAIT


@dataclass(frozen=True)
class StaffQuestionIdentity:
    """A staff question keeps its number within the owner's records."""

    owner: str
    number: str


type NoteIdentity = SubmissionIdentity | StaffQuestionIdentity


@dataclass(frozen=True)
class NoteProof:
    reply: reply_tokens.ReplyProof = reply_tokens.MISSING_REPLY_PROOF
    request: StaffQuestionRequestKey | NoStaffQuestionRequest = field(default=NO_STAFF_REQUEST, repr=False)


SUBMISSION_FIELDS = ("reply_token", "reply_token_sha256", "reply_token_nonce")


class ProofMoveRefusal(OSError):
    """A move failure with a message that contains no field values."""


def folder() -> Path:
    return state_root() / "review-reply-proofs"


def path_for(identity: NoteIdentity) -> Path:
    match identity:
        case SubmissionIdentity(number, request_id, revision):
            parts = ("submission", number, request_id)
            if revision != INITIAL_REPLY_WAIT:
                parts = (*parts, revision)
        case StaffQuestionIdentity(owner, number):
            parts = ("staff-question", owner, number)
    name = hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    return folder() / f"{name}.json"


def _proof(value: object, nonce: object) -> reply_tokens.StoredReplyProof:
    if not isinstance(value, str) or not reply_tokens.valid_hash(value):
        raise ValueError("Invalid saved reply hash.")
    digest = reply_tokens.SavedReplyHash(value)
    if nonce == "":
        return reply_tokens.LegacyReplyProof(digest)
    if not isinstance(nonce, str) or not reply_tokens.valid_nonce(nonce):
        raise ValueError("Invalid saved reply nonce.")
    return reply_tokens.ReplyTokenProof(nonce, digest)


def _request(value: object) -> StaffQuestionRequestKey:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid staff question request key.")
    return StaffQuestionRequestKey(value)


def _read(path: Path) -> NoteProof:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return NoteProof()
    try:
        value = cast(object, json.loads(raw))
        if not isinstance(value, dict):
            raise ValueError
        row = cast(dict[str, object], value)
        reply = (_proof(row["sha256"], row.get("nonce", "")) if "sha256" in row
                 else reply_tokens.MISSING_REPLY_PROOF)
        request = _request(row["request"]) if "request" in row else NO_STAFF_REQUEST
        return NoteProof(reply, request)
    except ValueError:
        raise OSError("Cannot read a record in the Review reply proof store.") from None


def read(identity: NoteIdentity) -> NoteProof:
    path = path_for(identity)
    if not path.exists():
        return NoteProof()
    with exclusive(path.with_suffix(".lock")):
        return _read(path)


def _write(path: Path, proof: NoteProof) -> None:
    fields: dict[str, str] = {}
    if isinstance(proof.reply, (reply_tokens.ReplyTokenProof, reply_tokens.LegacyReplyProof)):
        fields["sha256"] = proof.reply.sha256
    if isinstance(proof.reply, reply_tokens.ReplyTokenProof):
        fields["nonce"] = proof.reply.nonce
    if not isinstance(proof.request, NoStaffQuestionRequest):
        fields["request"] = proof.request
    if fields:
        atomic_file.write_locked_atomic(path, (json.dumps(fields) + "\n").encode(), mode=0o600)
    else:
        path.unlink(missing_ok=True)
        atomic_file.sync_folder(path.parent)


def _prepare_folder(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_file.sync_folder(path.parent.parent)


def stage_wait(identity: SubmissionIdentity, proof: reply_tokens.ReplyTokenProof) -> None:
    """Save the next proof before the note selects its revision."""
    if identity.revision == INITIAL_REPLY_WAIT:
        raise ValueError("A new wait must advance the reply revision.")
    path = path_for(identity)
    _prepare_folder(path)
    with exclusive(path.with_suffix(".lock")):
        _write(path, NoteProof(reply=proof))


@contextmanager
def publishing(identity: NoteIdentity, proof: NoteProof, *, moving: bool = False) -> Generator[None]:
    """Save the proof before the note changes. Hold the note's lock on entry."""
    path = path_for(identity)
    _prepare_folder(path)
    with exclusive(path.with_suffix(".lock")):
        previous = _read(path)
        if moving:
            _check_move_proof(previous, proof)
        if previous == proof:
            if moving:
                atomic_file.sync_folder(path.parent)
            yield
            return
        _write(path, proof)
        if moving and _read(path) != proof:
            raise ProofMoveRefusal("Cannot verify the saved Review reply proof.")
        try:
            yield
        except Exception:
            if not moving:
                _write(path, previous)
            raise


def _check_move_proof(previous: NoteProof, proof: NoteProof) -> None:
    if previous not in (NoteProof(), proof):
        raise ProofMoveRefusal("The note and the saved proof differ.")


def has_legacy_submission(fields: dict[str, object]) -> bool:
    return any(name in fields for name in SUBMISSION_FIELDS)


def extract_submission(fields: dict[str, object]
                       ) -> tuple[dict[str, object], reply_tokens.ReplyProof, int]:
    """Convert proof fields in a note during the move."""
    count = sum(name in fields for name in SUBMISSION_FIELDS)
    if not count:
        return fields, reply_tokens.MISSING_REPLY_PROOF, 0
    digest = fields.get("reply_token_sha256")
    raw = fields.get("reply_token")
    if digest is None and isinstance(raw, str) and reply_tokens.valid(raw):
        digest = reply_tokens.digest(reply_tokens.RawReplyToken(raw))
    proof = _proof(digest, fields.get("reply_token_nonce", ""))
    return {name: value for name, value in fields.items() if name not in SUBMISSION_FIELDS}, proof, count


def legacy_request(value: object) -> NoteProof:
    """Convert a staff question's request key during the move."""
    return NoteProof(request=_request(value))


class ProofNoteLayout(Enum):
    QUESTION = auto()
    PROPOSAL = auto()
    # Investigations and Bugs store the submission as a JSON string.
    SUBMISSION = auto()


@dataclass(frozen=True)
class ProofFieldRemoval:
    """The original note with only the named JSON members removed."""

    content: str = field(repr=False)
    submission: dict[str, object] = field(default_factory=dict, repr=False)
    proof: NoteProof = field(default_factory=NoteProof, repr=False)
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProofMemberDeletion:
    """JSON members to delete and the decoded values that must remain."""

    remaining: dict[str, object] = field(repr=False)
    submission: dict[str, object] = field(repr=False)
    proof: NoteProof
    fields: tuple[str, ...]
    spans: list[TextSpan]


@dataclass(frozen=True)
class JsonMember:
    name: str
    key_start: int
    value_start: int
    value_end: int


_JSON = json.JSONDecoder()
_JSON_STRING_CHAR = re.compile(
    r"\\u[dD][89aAbB][0-9A-Fa-f]{2}\\u[dD][c-fC-F][0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|\\.|.")
_JSON_SPACE = re.compile(r"[ \t\r\n]*")


def _json_object(text: str) -> dict[str, object]:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("Expected an object in the saved Review note.")
    return cast(dict[str, object], value)


def _json_members(text: str) -> Generator[JsonMember]:
    """Locate the members of a JSON object that passed JSON decoding."""
    start = _JSON_SPACE.match(text, text.index("{") + 1)
    assert start is not None
    cursor = start.end()
    while text[cursor] != "}":
        key, end = cast(tuple[object, int], _JSON.raw_decode(text, cursor))
        colon = text.index(":", end)
        space = _JSON_SPACE.match(text, colon + 1)
        assert space is not None
        value_start = space.end()
        value_end = _JSON.raw_decode(text, value_start)[1]
        yield JsonMember(cast(str, key), cursor, value_start, value_end)
        cursor = value_end
        while text[cursor] in " \t\r\n":
            cursor += 1
        if text[cursor] == ",":
            cursor += 1
            while text[cursor] in " \t\r\n":
                cursor += 1


type TextSpan = tuple[int, int]


def _removal_spans(text: str, members: tuple[JsonMember, ...], fields: tuple[str, ...]) -> list[TextSpan]:
    """Remove each member and the separator that joins it to its neighbors."""
    spans: list[TextSpan] = []
    index = 0
    for name in fields:
        if sum(member.name == name for member in members) != 1:
            raise ProofMoveRefusal("The Review note has a duplicate reply proof field.")
    while index < len(members):
        if members[index].name not in fields:
            index += 1
            continue
        first = index
        while index < len(members) and members[index].name in fields:
            index += 1
        if first:
            start = text.index(",", members[first - 1].value_end)
            end = members[index - 1].value_end
        else:
            start = members[first].key_start
            end = members[index].key_start if index < len(members) else members[-1].value_end
        spans.append((start, end))
    return spans


def _delete(text: str, spans: list[TextSpan]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def _submission_removal(text: str) -> ProofMemberDeletion:
    original = _json_object(text)
    names = tuple(name for name in SUBMISSION_FIELDS if name in original)
    clean, proof, _ = extract_submission(original)
    spans = _removal_spans(text, tuple(_json_members(text)), names) if names else []
    return ProofMemberDeletion(clean, clean, NoteProof(reply=proof), names, spans)


def _question_submission_members(members: tuple[JsonMember, ...]) -> list[JsonMember]:
    nested = [member for member in members if member.name == "review_submission"]
    if len(nested) > 1:
        raise ProofMoveRefusal("The Review note has duplicate submission fields.")
    return nested


def _question_removal(text: str) -> ProofMemberDeletion:
    registration = _json_object(text)
    members = tuple(_json_members(text))
    nested = _question_submission_members(members)
    submission: dict[str, object] = {}
    names: tuple[str, ...] = ()
    reply: reply_tokens.ReplyProof = reply_tokens.MISSING_REPLY_PROOF
    spans: list[TextSpan] = []
    if nested:
        value = nested[0]
        removal = _submission_removal(text[value.value_start:value.value_end])
        submission, reply, names = removal.submission, removal.proof.reply, removal.fields
        if names:
            registration["review_submission"] = submission
            spans.extend((value.value_start + start, value.value_start + end) for start, end in removal.spans)
    request = NoteProof()
    if "request_key" in registration:
        request = legacy_request(registration.pop("request_key"))
        names += ("request_key",)
        spans.extend(_removal_spans(text, members, ("request_key",)))
    return ProofMemberDeletion(registration, submission, NoteProof(reply, request.request), names, spans)


def _issue_proof_key(layout: ProofNoteLayout) -> str:
    return "registration" if layout is ProofNoteLayout.QUESTION else "review_submission"


def _issue_proof_values(fields: dict[str, list[issues.IssueField]], key: str) -> list[issues.IssueFieldValue]:
    declarations = fields.get(key, [])
    if not declarations:
        return []
    if len(declarations) > 1 or len(declarations[0].values) > 1:
        raise ProofMoveRefusal("The Review note has duplicate proof containers.")
    return declarations[0].values


def _has_legacy_submission_name(value: str) -> bool:
    """Recognize the legacy names in inline JSON or a numbered JSON string."""
    return re.search(r'\breply_token(?:_sha256|_nonce)?\\?"\s*:', value) is not None


def _has_legacy_proof(value: str, layout: ProofNoteLayout) -> bool:
    if not value:
        return False
    try:
        fields = _json_object(value)
    except ValueError:
        if layout is ProofNoteLayout.QUESTION:
            if any(f'"{name}"' in value for name in (*SUBMISSION_FIELDS, "request_key")):
                raise ProofMoveRefusal("Cannot decode the legacy Question registration.") from None
        elif _has_legacy_submission_name(value):
            raise ProofMoveRefusal("Cannot decode the legacy Review submission.") from None
        return False
    if layout is not ProofNoteLayout.QUESTION:
        return has_legacy_submission(fields)
    if "\\" in value or value.count('"review_submission"') > 1:
        _ = _question_submission_members(tuple(_json_members(value)))
    submission = fields.get("review_submission")
    return "request_key" in fields or (isinstance(submission, dict)
                                        and has_legacy_submission(cast(dict[str, object], submission)))


def issue_header_has_legacy_proof(fields: dict[str, list[issues.IssueField]], layout: ProofNoteLayout) -> bool:
    values = _issue_proof_values(fields, _issue_proof_key(layout))
    return bool(values and _has_legacy_proof(values[0].text, layout))


def _numbered_submission_has_legacy_proof(encoded: str) -> bool:
    try:
        value = issues.decode_numbered_field(encoded)
    except ValueError:
        if _has_legacy_submission_name(encoded):
            raise ProofMoveRefusal("Cannot decode the legacy Review submission.") from None
        return False
    return _has_legacy_proof(value, ProofNoteLayout.SUBMISSION)


def numbered_header_has_legacy_proof(header: issues.IssueHeaderSource, submissions: list[str],
                                     issue_value: str) -> bool:
    if not submissions and _has_legacy_submission_name(issue_value):
        raise ProofMoveRefusal("Cannot decode the legacy Review submission.")
    legacy = any(_numbered_submission_has_legacy_proof(value) for value in submissions)
    if legacy and header.closing:
        try:
            _ = issues.numbered_header_fields(header)
        except issues.DuplicateNumberedField:
            raise ProofMoveRefusal("The Review note has duplicate numbered fields.") from None
    return legacy


def note_header_bounds(text: str) -> tuple[int, int]:
    """Find the header between the opening and closing fences."""
    header = issues.issue_text_header(text)
    if not header.closing:
        raise ValueError("The Review note has no closing header fence.")
    start = len(header.opening)
    return start, start + sum(map(len, header.lines))


def _rewrite_issue_proof(text: str, layout: ProofNoteLayout) -> ProofFieldRemoval:
    header = issues.issue_text_header(text)
    if not header.closing:
        raise ProofMoveRefusal("The Review note has no closing header fence.")
    fields = header.located_fields()
    key = _issue_proof_key(layout)
    values = _issue_proof_values(fields, key)
    if not values:
        return ProofFieldRemoval(text)
    value = values[0]
    deletion = (_question_removal(value.text) if layout is ProofNoteLayout.QUESTION
                else _submission_removal(value.text))
    if not deletion.fields:
        return ProofFieldRemoval(text)
    changed = _delete(text, [(value.start + start, value.start + end) for start, end in deletion.spans])
    original = issues.decoded_header_fields(fields)
    saved = issues.issue_text_header(changed).decoded_fields()
    if (_json_object(next(iter(saved.pop(key, [])), "")) != deletion.remaining
            or saved != {name: values for name, values in original.items() if name != key}):
        raise ProofMoveRefusal("The proof move would change another issue field.")
    return ProofFieldRemoval(changed, deletion.submission, deletion.proof, deletion.fields)


def _rewrite_numbered_proof(text: str) -> ProofFieldRemoval:
    original = issues.located_numbered_note(text)
    key = "review_submission"
    if key not in original.fields or not original.fields[key].text:
        return ProofFieldRemoval(text)
    value = original.fields[key]
    deletion = _submission_removal(value.text)
    if not deletion.fields:
        return ProofFieldRemoval(text)
    opening = value.encoded.index('"') + 1
    offsets = [value.start + opening]
    fragments = _JSON_STRING_CHAR.finditer(value.encoded, opening)
    for _, fragment in zip(value.text, fragments):
        offsets.append(value.start + fragment.end())
    if len(offsets) != len(value.text) + 1:
        raise ProofMoveRefusal("Cannot locate the encoded proof fields.")
    changed = _delete(text, [(offsets[start], offsets[end]) for start, end in deletion.spans])
    saved = issues.located_numbered_note(changed)
    remaining = saved.decoded_fields()
    expected = original.decoded_fields()
    if (_json_object(remaining.pop(key)) != deletion.remaining or saved.body != original.body
            or remaining != {name: field for name, field in expected.items() if name != key}):
        raise ProofMoveRefusal("The proof move would change another numbered field.")
    return ProofFieldRemoval(changed, deletion.submission, deletion.proof, deletion.fields)


def prepare_note_removal(text: str, layout: ProofNoteLayout) -> ProofFieldRemoval:
    """Compute and validate the byte-preserving rewrite for a saved note."""
    if layout is ProofNoteLayout.SUBMISSION:
        return _rewrite_numbered_proof(text)
    return _rewrite_issue_proof(text, layout)


@dataclass(frozen=True)
class ProofMoveSummary:
    notes: int
    fields: int
    outside_git: int
    open_items: int


def _git_paths(checkout: Path, command: tuple[str, ...], paths: list[Path]) -> set[bytes]:
    result = subprocess.run(
        ("git", "--no-optional-locks", "--literal-pathspecs", "-C", str(checkout),
         "-c", "core.fsmonitor=false", *command, "--", *(str(path) for path in paths)),
        capture_output=True, check=False)
    if result.returncode:
        raise OSError("Cannot read the Git status of Review notes.")
    return set(result.stdout.rstrip(b"\0").split(b"\0")) if result.stdout else set()


def _outside_git(paths: dict[Path, tuple[str, ...]]) -> int:
    @cache
    def checkout_for(parent: Path) -> Path | None:
        try:
            return git_files.locate(parent).toplevel
        except git_files.NotACheckout:
            return None

    outside = 0
    checkouts: dict[Path, list[Path]] = {}
    for path in paths:
        checkout = checkout_for(path.parent)
        if checkout is None:
            outside += 1
        else:
            checkouts.setdefault(checkout, []).append(path.relative_to(checkout))
    for checkout, notes in checkouts.items():
        tracked = _git_paths(checkout, ("ls-files", "-z", "--cached"), notes)
        dirty = _git_paths(checkout, ("status", "--porcelain=v1", "-z", "--untracked-files=no",
                                      "--no-renames", "--ignore-submodules=all"), notes)
        unchanged = tracked - {entry[3:] for entry in dirty}
        outside += sum(os.fsencode(note) not in unchanged for note in notes)
    return outside


_REWRITE_JOURNAL_NAME = "rewrites.jsonl"
_JOURNAL_WRITE_FLAGS = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK


def move_journal_path() -> Path:
    return state_root().resolve() / "review-reply-proof-backups" / _REWRITE_JOURNAL_NAME


@dataclass
class ProofMoveJournal:
    """Index complete journal lines for rewrites and skipped backups."""

    fields_by_path: dict[Path, tuple[str, ...]] = field(default_factory=dict, init=False)
    skipped_backups: set[Path] = field(default_factory=set, init=False)
    _end: int = field(default=0, init=False)
    _parent_synced: bool = field(default=False, init=False)
    _checked: bool = field(default=False, init=False)

    def read(self, stream: BinaryIO) -> None:
        """Read complete lines after the last read or append."""
        _ = stream.seek(self._end)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            entry = cast(object, json.loads(line))
            if not isinstance(entry, dict):
                raise ValueError("The Review proof move journal entry must be an object.")
            record = cast(dict[str, object], entry)
            if "skipped_backup" in record:
                backup = record["skipped_backup"]
                if len(record) != 1 or not isinstance(backup, str) or not Path(backup).is_absolute():
                    raise ValueError("The Review proof move journal backup path is invalid.")
                self.skipped_backups.add(Path(backup))
                self._end += len(line)
                continue
            path, names = record.get("path"), record.get("fields")
            if (not isinstance(path, str) or not Path(path).is_absolute()
                    or not isinstance(names, list) or not names
                    or any(name not in (*SUBMISSION_FIELDS, "request_key") for name in cast(list[object], names))):
                raise ValueError("The Review proof move journal entry is invalid.")
            note = Path(path)
            previous = self.fields_by_path.get(note, ())
            self.fields_by_path[note] = tuple(dict.fromkeys((*previous, *cast(list[str], names))))
            self._end += len(line)

    def _read_saved(self, stream: BinaryIO) -> int:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o600:
            raise ProofMoveRefusal("The Review proof move journal must be a file with mode 0600.")
        if self._end > status.st_size:
            raise ProofMoveRefusal("The Review proof move journal lost complete entries.")
        self.read(stream)
        return status.st_size

    def _recover_tail(self, stream: BinaryIO) -> None:
        size = self._read_saved(stream)
        if self._end < size:
            os.ftruncate(stream.fileno(), self._end)

    def _sync(self, journal: Path, stream: BinaryIO) -> None:
        os.fsync(stream.fileno())
        if not self._parent_synced:
            atomic_file.sync_folder(journal.parent)
            self._parent_synced = True

    def check(self, journal: Path, *, dry_run: bool) -> None:
        """Validate the journal once. In a move, also recover and sync it."""
        if self._checked:
            return
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK if dry_run else _JOURNAL_WRITE_FLAGS
        try:
            descriptor = os.open(journal, flags)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(descriptor, "rb" if dry_run else "r+b") as stream:
                if dry_run:
                    _ = self._read_saved(stream)
                else:
                    self._recover_tail(stream)
                    self._sync(journal, stream)
        self._checked = True

    @contextmanager
    def _writable(self, journal: Path) -> Generator[BinaryIO]:
        descriptor = os.open(journal, _JOURNAL_WRITE_FLAGS | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            self._recover_tail(stream)
            yield stream
            self._sync(journal, stream)

    @staticmethod
    def _write_lines(stream: BinaryIO, lines: bytes) -> None:
        if lines and os.write(stream.fileno(), lines) != len(lines):
            raise OSError("Cannot append the complete Review proof move journal line.")

    def append(self, journal: Path, path: Path, names: tuple[str, ...]) -> None:
        """Sync new field names before the note changes. Hold the transfer lock."""
        with self._writable(journal) as stream:
            previous = self.fields_by_path.get(path, ())
            added = tuple(name for name in names if name not in previous)
            line = (json.dumps({"path": str(path), "fields": added}) + "\n").encode() if added else b""
            self._write_lines(stream, line)
        self.fields_by_path[path] = previous + added
        self._end += len(line)

    def append_skipped_backups(self, journal: Path, paths: tuple[Path, ...]) -> None:
        """Sync the skipped backup paths. Hold the transfer lock."""
        with self._writable(journal) as stream:
            added = tuple(path for path in paths if path not in self.skipped_backups)
            lines = "".join(json.dumps({"skipped_backup": str(path)}) + "\n" for path in added).encode()
            self._write_lines(stream, lines)
        self.skipped_backups.update(added)
        self._end += len(lines)


def read_move_report() -> dict[Path, tuple[str, ...]]:
    """Read the field names recorded for each note in the durable journal."""
    report = ProofMoveJournal()
    try:
        stream = move_journal_path().open("rb")
    except FileNotFoundError:
        return {}
    with stream:
        report.read(stream)
    return report.fields_by_path


def _check_backup_folder(parent: Path) -> None:
    try:
        status = parent.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(status.st_mode):
        raise ProofMoveRefusal("The Review reply proof backup folder must not be a symbolic link.")
    if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o700:
        raise ProofMoveRefusal("The Review reply proof backup folder must have mode 0700.")


@dataclass
class ProofMoveRun:
    """Keep time-stamped backups and count the notes a move processes."""

    dry_run: bool
    configured_vaults: Callable[[], tuple[Path, ...]]
    _entries: dict[Path, tuple[str, ...]] = field(default_factory=dict, init=False)
    _open_items: int = field(default=0, init=False)
    _backup: Path | None = field(default=None, init=False)
    _journal: ProofMoveJournal = field(default_factory=ProofMoveJournal, init=False)
    _planned_proofs: dict[NoteIdentity, NoteProof] = field(default_factory=dict, init=False, repr=False)

    @cached_property
    def _checked_backup_parent(self) -> Path:
        parent = move_journal_path().parent
        if any(parent.is_relative_to(vault) for vault in self.configured_vaults()):
            raise ProofMoveRefusal("The Review reply proof backup must stay outside the vault.")
        _check_backup_folder(parent)
        existing = next(folder for folder in (parent, *parent.parents) if folder.exists())
        if not existing.is_dir():
            raise ProofMoveRefusal("The Review reply proof backup location must be a folder.")
        try:
            _ = git_files.locate(existing)
        except git_files.NotACheckout:
            pass
        else:
            raise ProofMoveRefusal("The Review reply proof backup must stay outside Git.")
        return parent

    def check_backup_location(self) -> None:
        """Check where the backup will go before taking the transfer lock."""
        _ = self._checked_backup_parent

    def check_journal(self) -> None:
        """Check the journal under the transfer lock before processing notes."""
        self._journal.check(self._checked_backup_parent / _REWRITE_JOURNAL_NAME, dry_run=self.dry_run)

    def _prepare_backup_parent(self) -> Path:
        parent = self._checked_backup_parent
        state = parent.parent
        state.mkdir(parents=True, exist_ok=True)
        parent.mkdir(mode=0o700, exist_ok=True)
        _check_backup_folder(parent)
        atomic_file.sync_folder(state.parent)
        atomic_file.sync_folder(state)
        return parent

    def _backup_folder(self) -> Path:
        if self._backup is not None:
            return self._backup
        self.check_journal()
        parent = self._prepare_backup_parent()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self._backup = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=parent))
        atomic_file.sync_folder(parent)
        return self._backup

    def backup(self, path: Path, content: bytes, fields: tuple[str, ...]) -> None:
        if self.dry_run or not fields:
            return
        folder = self._backup_folder()
        source = path.resolve()
        target = folder / source.relative_to(source.anchor)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise OSError("The Review note changed after its backup.")
        else:
            atomic_file.write_locked_atomic(target, content, mode=0o600)
        directory = target.parent
        while directory != folder:
            atomic_file.sync_folder(directory.parent)
            directory = directory.parent

    def record_rewrite(self, *notes: tuple[Path, tuple[str, ...]]) -> None:
        """Append and sync each line before changing its backed-up note."""
        pending = {path.resolve(): names for path, names in notes if names}
        if not pending:
            return
        backup = self._backup
        if backup is None:
            raise OSError("Cannot report a Review rewrite without its backup.")
        for path, names in pending.items():
            self._journal.append(backup.parent / _REWRITE_JOURNAL_NAME, path, names)
        self._entries.update(pending)

    def record_skipped_backups(self, paths: tuple[Path, ...]) -> None:
        """Add new path entries after a completed scan."""
        if self.dry_run or not paths:
            return
        self.check_journal()
        if self._journal.skipped_backups.issuperset(paths):
            return
        parent = self._prepare_backup_parent()
        self._journal.append_skipped_backups(parent / _REWRITE_JOURNAL_NAME, paths)

    def _plan_rewrite(self, path: Path, fields: tuple[str, ...], proofs: list[tuple[NoteIdentity, NoteProof]],
                      check_write: Callable[[], None]) -> None:
        """Check the preview against saved proofs and proofs from earlier notes."""
        self.check_journal()
        pending: dict[NoteIdentity, NoteProof] = {}
        for identity, proof in proofs:
            if identity in pending:
                previous = pending[identity]
            elif identity in self._planned_proofs:
                previous = self._planned_proofs[identity]
            else:
                proof_path = path_for(identity)
                with atomic_file.existing_writer_lock(proof_path.with_suffix(".lock")):
                    previous = _read(proof_path)
            _check_move_proof(previous, proof)
            pending[identity] = proof
        check_write()
        self._entries[path.resolve()] = fields
        self._planned_proofs.update(pending)

    def apply(self, path: Path, original: str, removal: ProofFieldRemoval,
              proofs: list[tuple[NoteIdentity, NoteProof]], write: Callable[[str], None], *, open_item: bool) -> int:
        """Check the rewrite under the writer lock. The callback honors the run mode."""
        if not removal.fields:
            return 0
        if self.dry_run:
            self._plan_rewrite(path, removal.fields, proofs, lambda: write(removal.content))
        else:
            self.backup(path, original.encode(), removal.fields)
            with ExitStack() as publications:
                for identity, proof in proofs:
                    _ = publications.enter_context(publishing(identity, proof, moving=True))
                self.record_rewrite((path, removal.fields))
                write(removal.content)
                if path.read_bytes() != removal.content.encode():
                    raise OSError("Cannot verify the Review note after the proof move.")
        self._open_items += open_item
        return len(removal.fields)

    def summary(self) -> ProofMoveSummary:
        return ProofMoveSummary(len(self._entries), sum(len(fields) for fields in self._entries.values()),
                                _outside_git(self._entries) if self.dry_run else 0, self._open_items)
