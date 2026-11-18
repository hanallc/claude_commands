"""Store items sent from the New item dialog and their replies."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib import atomic_file, conf
from scripts.production import investigation, proposal_issue, record_store, staff, update_registration
from scripts.production.issues import Issue
from scripts.production.production_doc import WRAPPED, Refusal
from scripts.review_links import image_store, review_link
from scripts.review_links.new_item_dialog import (
    KEY_PREFIX as KEY_PREFIX, NATIVE_ID_PREFIX, check_recipient_id,
    ScopedSubmissionKey, read_submission_key, require_request_binding, submission_key as submission_key,
    IMAGE_FORMATS, MAX_IMAGES, MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES, ImageReference, max_base64_size, new_reply_token,
    read_kind, read_request_id, read_words, read_submission,
    ByRequestId, BuildCell as BuildCell, BuildStepEvidence as BuildStepEvidence, Evidence as Evidence,
    MemoryReading as MemoryReading,
    MemorySegment as MemorySegment, MemoryTone as MemoryTone, NewItemRequest as NewItemRequest,
    NoEvidence as NoEvidence, PastedImage as PastedImage, ReviewSubmission as ReviewSubmission,
    SubmissionConflict as SubmissionConflict, SubmissionError as SubmissionError, SubmissionKind as SubmissionKind,
    TipCaption as TipCaption, TipDetail as TipDetail, TipField as TipField,
    read_evidence as read_evidence, read_request as read_request,
)

@dataclass(frozen=True)
class SubmissionAddress:
    """The request identity and its original recipient."""
    kind: SubmissionKind
    recipient_id: staff.StaffId
    request_id: str


@dataclass(frozen=True)
class NativeSubmissionDestination:
    """The native location and ID selected for a request."""
    id: str
    kind: SubmissionKind
    recipient_id: staff.StaffId
    request_id: str
    payload_hash: str
    path: Path
    original_path: Path
    owner: staff.StaffId

    @property
    def address(self) -> SubmissionAddress:
        return SubmissionAddress(self.kind, self.recipient_id, self.request_id)

    def saved(self) -> SavedSubmission:
        return SavedSubmission(self.id, self.kind, self.recipient_id, self.request_id, self.payload_hash, self.path,
                               self.original_path, self.owner)


@dataclass(frozen=True)
class SavedSubmission(NativeSubmissionDestination):
    """The receipt for a completed native write."""

    @property
    def key(self) -> str:
        return submission_key(self.kind, self.recipient_id, self.id, self.request_id)

    @property
    def details(self) -> ReviewSubmission:
        """Read the submission details from the native record."""
        with _locked_request(self.address, fallback=self) as lookup:
            located = lookup.native
        if isinstance(located, LocatedSubmission):
            return located.details
        raise OSError(f"Cannot find the native record for request {self.request_id}.")

    @property
    def reply_token(self) -> str:
        return self.details.reply_token


@dataclass(frozen=True)
class _ImageUpload:
    extension: str
    data: bytes


def _decode_images(images: tuple[PastedImage, ...]) -> tuple[_ImageUpload, ...]:
    if len(images) > MAX_IMAGES:
        raise SubmissionError(f"Paste up to {MAX_IMAGES} images.")
    uploads: list[_ImageUpload] = []
    total = 0
    for image in images:
        extension = IMAGE_FORMATS.get(image.media_type)
        if extension is None:
            raise SubmissionError("Paste PNG, JPEG, GIF or WebP images.")
        if len(image.base64_data) > max_base64_size():
            raise SubmissionError("Each image must be at most 5 MiB.")
        try:
            data = base64.b64decode(image.base64_data, validate=True)
        except (binascii.Error, ValueError):
            raise SubmissionError("The pasted image data is invalid.") from None
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise SubmissionError("Each image must contain at most 5 MiB of image data.")
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise SubmissionError("The pasted images must total at most 12 MiB.")
        uploads.append(_ImageUpload(extension, data))
    return tuple(uploads)


def _validate_image(upload: _ImageUpload) -> None:
    try:
        result = subprocess.run(
            ["magick", "identify", "-regard-warnings", "-limit", "width", "8192",
             "-limit", "height", "8192", "-limit", "area", "32MP",
             "-limit", "list-length", "32", "-limit", "memory", "128MiB",
             "-limit", "map", "128MiB", "-limit", "disk", "0",
             "-format", "%m\n", upload.extension + ":-"],
            input=upload.data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4, check=False)
    except subprocess.TimeoutExpired:
        raise SubmissionError("The pasted image could not be read in time.") from None
    names = result.stdout.splitlines()
    if result.returncode or not names or any(name != upload.extension.upper().encode() for name in names):
        raise SubmissionError("The pasted image does not match its type or exceeds the image limits.")


def _request_folder(request_id: str) -> Path:
    snapshot_root = review_link.review_root().resolve()
    configured = conf.read_or_repository("review_links").text("review_attachments_root")
    root = (snapshot_root.parent / Path(configured).expanduser()).resolve()
    if root.is_relative_to(snapshot_root) or root.is_relative_to(record_store.state_root().resolve()):
        raise OSError("The Review attachment root must be outside the snapshot and staff state roots.")
    return root / read_request_id(request_id)


def image_folder(item: SavedSubmission) -> Path:
    """Return the private folder for the image IDs on the native record."""
    return _request_folder(item.request_id) / "images"


def _image_references(uploads: tuple[_ImageUpload, ...]) -> tuple[ImageReference, ...]:
    return tuple(ImageReference(f"{number:012x}", f"image.{upload.extension}")
                 for number, upload in enumerate(uploads, 1))


def _save_images(folder: Path, request: NewItemRequest, uploads: tuple[_ImageUpload, ...]) -> tuple[ImageReference, ...]:
    references = _image_references(uploads)
    if not references:
        return references
    images = folder / "images"
    if images.exists():
        for reference, upload in zip(references, uploads, strict=True):
            if (images / reference.id / reference.file).read_bytes() != upload.data:
                raise OSError("A saved Review image does not match the request.")
        return references

    # Publish the complete image batch with one rename.
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=folder) as temporary:
        workspace = Path(temporary)
        stage = workspace / "images"
        for reference, upload in zip(references, uploads, strict=True):
            _validate_image(upload)
            source = workspace / reference.file
            atomic_file.write_bytes_atomic(source, upload.data, mode=0o600)
            _ = image_store.register_file(source, stage, owner=request.recipient_id,
                                          subject="Review request " + request.request_id, identity=reference.id, private=True)
        atomic_file.sync_folder(stage)
        _ = stage.rename(images)
        atomic_file.sync_folder(folder)
    return references


@dataclass(frozen=True)
class NoRequestFile:
    """The request has no file at this location."""


@dataclass(frozen=True)
class PendingFiling:
    """The request payload is fixed but has no planned native record yet."""
    payload_hash: str


@dataclass(frozen=True)
class UnreadableRequestFile:
    """The request file exists but cannot supply a lookup hint."""

    error: OSError


_NO_REQUEST_FILE = NoRequestFile()
type RequestFile = NativeSubmissionDestination | PendingFiling | NoRequestFile | UnreadableRequestFile


def _read_request_file(path: Path, request_id: str) -> RequestFile:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoRequestFile()
    except (ValueError, UnicodeError) as error:
        raise OSError(f"Cannot read the Review request file at {path}.") from error

    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        payload_hash = fields.get("payload_hash")
        if isinstance(payload_hash, str) and re.fullmatch(r"[0-9a-f]{64}", payload_hash):
            if fields.keys() == {"payload_hash"} and path.name == "pending.json":
                return PendingFiling(payload_hash)
            kind, recipient = fields.get("kind"), fields.get("recipient")
            owner = fields.get("owner", recipient)
            identity, native_path = fields.get("id"), fields.get("path")
            original_path = fields.get("original_path", native_path)
            if (isinstance(kind, str) and kind in SubmissionKind and isinstance(recipient, str)
                    and isinstance(owner, str) and isinstance(identity, str) and isinstance(native_path, str)
                    and isinstance(original_path, str)):
                kind = SubmissionKind(kind)
                if (re.fullmatch(NATIVE_ID_PREFIX[kind] + r"[1-9][0-9]*", identity)
                        and Path(native_path).is_absolute() and Path(original_path).is_absolute()):
                    filing_owner = staff.StaffId(owner)
                    try:
                        check_recipient_id(filing_owner)
                    except SubmissionError as error:
                        raise OSError(f"Cannot read the Review request file at {path}.") from error
                    return NativeSubmissionDestination(identity, kind, staff.StaffId(recipient), request_id,
                                                       payload_hash, Path(native_path), Path(original_path), filing_owner)
    raise OSError(f"Cannot read the Review request file at {path}.")


def _check_same_request(saved: NativeSubmissionDestination | PendingFiling, request: NewItemRequest,
                        payload_hash: str) -> None:
    if saved.payload_hash != payload_hash or (isinstance(saved, NativeSubmissionDestination)
            and (saved.kind != request.kind or saved.recipient_id != request.recipient_id)):
        raise SubmissionConflict("This request ID already has different item data.")


def _write_request_file(path: Path, fields: dict[str, str]) -> None:
    atomic_file.write_text_atomic(path, json.dumps(fields) + "\n", new_file_mode=0o600)


def _write_location(path: Path, destination: NativeSubmissionDestination) -> None:
    fields = {"id": destination.id, "kind": destination.kind,
              "recipient": destination.recipient_id, "path": str(destination.path),
              "original_path": str(destination.original_path), "payload_hash": destination.payload_hash}
    if destination.owner != destination.recipient_id:
        fields["owner"] = destination.owner
    _write_request_file(path, fields)


def _hint_paths(address: SubmissionAddress, hint: RequestFile) -> tuple[Path, ...]:
    if isinstance(hint, NativeSubmissionDestination) and hint.address == address:
        return tuple(dict.fromkeys((hint.path, hint.original_path)))
    return ()


def _request_hint(address: SubmissionAddress, *, receipt_only: bool = False) -> RequestFile:
    folder = _request_folder(address.request_id)
    try:
        receipt = _read_request_file(folder / "receipt.json", address.request_id)
        if receipt_only or not isinstance(receipt, NoRequestFile):
            return receipt
        return _read_request_file(folder / "pending.json", address.request_id)
    except OSError as error:
        return UnreadableRequestFile(error)


def _is_investigation(address: SubmissionAddress) -> bool:
    return address.kind in (SubmissionKind.BUG, SubmissionKind.INVESTIGATION)


@dataclass(frozen=True)
class LocatedSubmission:
    """The matching request metadata read from a native record."""
    destination: NativeSubmissionDestination
    details: ReviewSubmission


@dataclass(frozen=True)
class NoNativeSubmission:
    """No native record proves that this request was written."""


@dataclass(frozen=True)
class SubmissionLookup:
    """The file hint and the native result under the recipient locks."""

    hint: RequestFile
    native: LocatedSubmission | NoNativeSubmission
    members: staff.StaffSnapshot | None


@contextmanager
def _locked_request(address: SubmissionAddress, members: staff.StaffSnapshot | None = None, *,
                    fallback: RequestFile = _NO_REQUEST_FILE, for_send: bool = False) -> Generator[SubmissionLookup]:
    owners = {address.recipient_id}
    while True:
        with investigation.recipient_locks(*owners):
            hint = _request_hint(address)
            if isinstance(hint, (NoRequestFile, UnreadableRequestFile)) and isinstance(fallback, NativeSubmissionDestination):
                hint = fallback
            if isinstance(hint, NativeSubmissionDestination) and hint.address == address and hint.owner not in owners:
                owners.add(hint.owner)
                continue

            current = members
            native = locate_native_submission(address, hint, search_stores=False)
            if isinstance(native, NoNativeSubmission):
                current = staff.snapshot() if for_send else members
                native = locate_native_submission(address, hint, members=current)
            required: set[staff.StaffId]
            if isinstance(native, LocatedSubmission):
                required = {native.destination.owner}
            elif for_send:
                assert current is not None
                required = set(investigation.owner_chain(address.recipient_id, current))
            else:
                required = set()
            if not owners.issuperset(required):
                owners.update(required)
                continue
            yield SubmissionLookup(hint, native, current)
            return


def _located(address: SubmissionAddress, path: Path, owner: staff.StaffId, identity: str,
             details: ReviewSubmission) -> LocatedSubmission | NoNativeSubmission:
    if details.request_id != address.request_id:
        return NoNativeSubmission()
    if not re.fullmatch(NATIVE_ID_PREFIX[address.kind] + r"[1-9][0-9]*", identity):
        raise OSError(f"The native Review record at {path} has an invalid item ID.")
    path = path.resolve()
    return LocatedSubmission(NativeSubmissionDestination(identity, address.kind, address.recipient_id,
                             address.request_id, details.request_digest, path, path, owner), details)


def _read_native_submission(address: SubmissionAddress, path: Path,
                            owner: staff.StaffId) -> LocatedSubmission | NoNativeSubmission:
    try:
        _ = path.stat()
        match address.kind:
            case SubmissionKind.QUESTION:
                records = update_registration.selected_question_records([path], (), None, sources=[], owner=owner)
                if records and "review_submission" in records[0].item:
                    item = records[0].item
                    return _located(address, path, owner, item.get("review_id", ""), read_submission(item.get("review_submission")))
            case SubmissionKind.PROPOSAL:
                issue = Issue(path, record_store.read_header(path, strict=True))
                if proposal_issue.proposal_owner(issue, issue.value("proposed_by")) == owner and "proposal" in issue.fields.get("tags", []):
                    origin = proposal_issue.review_submission(issue)
                    if isinstance(origin, ReviewSubmission):
                        return _located(address, path, owner, issue.value("proposal_id"), origin)
            case SubmissionKind.INVESTIGATION | SubmissionKind.BUG:
                report = investigation.read_review_record(path)
                if (report.owner == owner and report.kind.value == address.kind.value
                        and isinstance(report.submission, ReviewSubmission)):
                    return _located(address, path, owner, report.id, report.submission)
    except FileNotFoundError:
        return NoNativeSubmission()
    except (OSError, ValueError, update_registration.RegistrationFailure) as error:
        raise OSError(f"Cannot read the native Review record at {path}.") from error
    return NoNativeSubmission()


def _search_owner_store(address: SubmissionAddress, owner: staff.StaffId, folders: tuple[Path, ...],
                        document: Path | None) -> LocatedSubmission | NoNativeSubmission:
    lookup = ByRequestId(address.request_id)
    match address.kind:
        case SubmissionKind.PROPOSAL:
            issue = proposal_issue.find_review_proposal(folders, owner, lookup, document=document)
            if issue is not None:
                origin = proposal_issue.review_submission(issue)
                assert isinstance(origin, ReviewSubmission)
                return _located(address, issue.path, owner, issue.value("proposal_id"), origin)
        case SubmissionKind.QUESTION:
            record = update_registration.find_review_question_in_folder(
                update_registration.QuestionFolder(record_store.default_question_folder(owner), owner), lookup,
                other_folders=folders, document=document)
            if record is not None:
                item = record.item
                return _located(address, record.path, owner, item.get("review_id", ""), read_submission(item.get("review_submission")))
        case SubmissionKind.INVESTIGATION | SubmissionKind.BUG:
            found = investigation.find_review_submission(
                owner, investigation.InvestigationKind(address.kind.value), address.request_id)
            if found is not None:
                path, report = found
                assert isinstance(report.submission, ReviewSubmission)
                return _located(address, path, owner, report.id, report.submission)
    return NoNativeSubmission()


def _search_native_stores(address: SubmissionAddress, owner: staff.StaffId, paths: tuple[Path, ...],
                          members: staff.StaffSnapshot) -> LocatedSubmission | NoNativeSubmission:
    folders = tuple(dict.fromkeys(path.parent for path in paths))
    for candidate in dict.fromkeys((owner, address.recipient_id)):
        found = _search_owner_store(address, candidate, folders if candidate == owner else (),
                                    record_store.local_document(members.by_id(candidate)))
        if isinstance(found, LocatedSubmission):
            return found
    original = members.by_id(address.recipient_id)
    try:
        active = original is not None and owner_is_active(original)
    except OSError:
        active = False
    if active:
        return NoNativeSubmission()

    # Without a receipt, find the original request even if the staff chain changed.
    kind = record_store.RecordKind.INVESTIGATION if _is_investigation(address) else record_store.RecordKind(address.kind)
    registered = record_store.store_owners(kind, strict=True)
    chain = investigation.owner_chain(address.recipient_id, members)
    for candidate in dict.fromkeys((*chain, *registered)):
        if candidate in (owner, address.recipient_id):
            continue
        found = _search_owner_store(address, candidate, (), record_store.local_document(members.by_id(candidate)))
        if isinstance(found, LocatedSubmission):
            return found
    return NoNativeSubmission()


def locate_native_submission(address: SubmissionAddress, hint: RequestFile,
                             *, members: staff.StaffSnapshot | None = None, search_stores: bool = True
                             ) -> LocatedSubmission | NoNativeSubmission:
    """Find the request by its native identity, using the receipt as a hint."""
    paths = _hint_paths(address, hint)
    owner = hint.owner if paths and isinstance(hint, NativeSubmissionDestination) else address.recipient_id
    unreadable: OSError | None = None
    for path in paths:
        try:
            located = _read_native_submission(address, path, owner)
        except OSError as error:
            unreadable = error
            continue
        if isinstance(located, LocatedSubmission):
            break
    else:
        if not search_stores:
            return NoNativeSubmission()
        try:
            located = _search_native_stores(address, owner, paths, staff.snapshot() if members is None else members)
        except OSError:
            if unreadable is not None:
                raise unreadable from None
            raise
    if isinstance(located, LocatedSubmission):
        if paths and isinstance(hint, NativeSubmissionDestination):
            located = replace(located, destination=replace(located.destination, original_path=hint.original_path))
        return located
    if unreadable is not None:
        raise unreadable
    return NoNativeSubmission()


def _publish_receipt(folder: Path, saved: NativeSubmissionDestination) -> None:
    # Confirm the native directory before making the receipt visible.
    atomic_file.sync_folder(saved.path.parent)
    receipt = folder / "receipt.json"
    try:
        os.replace(folder / "pending.json", receipt)
    except FileNotFoundError:
        if _read_request_file(receipt, saved.request_id) != saved:
            raise
    atomic_file.sync_folder(folder)


def _has_receipt_for(saved: NativeSubmissionDestination) -> bool:
    receipt = _request_hint(saved.address, receipt_only=True)
    return (isinstance(receipt, NativeSubmissionDestination)
            and (receipt.address, receipt.id, receipt.owner, receipt.payload_hash)
            == (saved.address, saved.id, saved.owner, saved.payload_hash))


def _complete_receipt(folder: Path, saved: NativeSubmissionDestination,
                      hint: RequestFile, *, first_send: bool = False) -> SavedSubmission:
    """Complete the receipt before a retry can return the native result."""
    try:
        if (folder / "receipt.json").is_file():
            if hint != saved:
                atomic_file.sync_folder(saved.path.parent)
                _write_location(folder / "receipt.json", saved)
            else:
                atomic_file.sync_folder(folder)
        else:
            if hint != saved:
                _write_location(folder / "pending.json", saved)
            _publish_receipt(folder, saved)
    except OSError as error:
        if not first_send and not _has_receipt_for(saved):
            raise
        words = " ".join(str(error).splitlines())
        print(f"Review receipt for {saved.id}: {words}", file=sys.stderr)
    return saved.saved()


def _recipient(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow:
    row = members.by_id(identity)
    if row is None or row.kind not in ("showrunner", "producer"):
        raise SubmissionError("Select a showrunner or producer.")
    return row


def owner_is_active(row: staff.StaffRow) -> bool:
    """Check both the staff row and the production document."""
    if isinstance(row.doc, staff.WrappedDoc) or isinstance(row.worktree, staff.RemovedWorktree):
        return False
    if isinstance(row.doc, staff.LiveDoc):
        try:
            lines = row.doc.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return False
        status = next((line for line in lines if line.startswith("> **Status:")), "")
        if row.kind == "showrunner":
            return status.startswith("> **Status: PRODUCTION — running.**")
        return not status.startswith(WRAPPED)
    return row.kind == "producer"


def _active_recipient(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow:
    row = _recipient(identity, members)
    for candidate in investigation.owner_chain(identity, members):
        member = members.by_id(candidate)
        if member is not None and member.kind in ("showrunner", "producer") and owner_is_active(member):
            return member
    raise SubmissionConflict(f"{row.name} has finished; choose another recipient")


@dataclass(frozen=True)
class NoNativeFolder:
    """The recipient has no native folder for this kind of record."""

    kind: SubmissionKind


class RecipientOption(TypedDict):
    id: staff.StaffId
    name: str
    proposal: bool


def _proposal_folder(recipient: staff.StaffRow) -> Path | NoNativeFolder:
    from scripts.production import proposals_folder

    if isinstance(recipient.doc, (staff.LiveDoc, staff.WrappedDoc)):
        folder = proposals_folder.issues_folder(recipient.doc.path)
        if folder is not None:
            return folder
    return NoNativeFolder(SubmissionKind.PROPOSAL)


def recipient_options(members: staff.StaffSnapshot) -> list[RecipientOption]:
    """List active recipients and indicate who can receive proposals."""
    options: list[RecipientOption] = []
    for row in (*staff.active(members, "showrunner"), *staff.active(members, "producer")):
        try:
            active = owner_is_active(row)
        except OSError:
            continue
        if not active:
            continue
        try:
            can_receive_proposals = isinstance(_proposal_folder(row), Path)
        except OSError:
            can_receive_proposals = False
        options.append(RecipientOption(id=row.id, name=row.name, proposal=can_receive_proposals))
    return options


def _file(kind: SubmissionKind, recipient: staff.StaffRow, title: str, words: str, details: ReviewSubmission,
          members: staff.StaffSnapshot, proposal_folder: Path | NoNativeFolder,
          before_write: Callable[[str, Path], None]) -> None:
    why = f"{recipient.name} answers this"
    match kind:
        case SubmissionKind.QUESTION:
            identities = update_registration.QuestionStaff.for_record(members)
            scope = update_registration.question_scope(recipient, members)
            parts = update_registration.QuestionParts(ask=title, context="", items=[], recommend="", choices=[])
            _ = update_registration.ask(
                scope, title, parts, "", update_registration.Ranking(), identities=identities,
                outgoing=update_registration.QuestionFromReview(words, why, details, before_write))
        case SubmissionKind.PROPOSAL:
            assert isinstance(proposal_folder, Path)
            _ = proposal_issue.file_review_proposal(proposal_folder, recipient.id, title, words, why, details,
                                                   before_write=before_write)
        case SubmissionKind.BUG | SubmissionKind.INVESTIGATION:
            _ = investigation.file(recipient.id, title, words, waiting_why=why,
                                   kind=investigation.InvestigationKind(kind.value), submission=details,
                                   before_write=before_write)


def submit(request: NewItemRequest, *, members: staff.StaffSnapshot | None = None) -> SavedSubmission:
    """Validate and file the item once in its native store."""
    check_recipient_id(request.recipient_id)
    request_id = read_request_id(request.request_id)
    kind = read_kind(request.kind)
    address = _request_address(kind, request.recipient_id, request_id)
    words = read_words(request.words, "Your words")
    evidence = read_evidence(asdict(request.evidence))
    uploads = _decode_images(request.images)
    payload_hash = hashlib.sha256(json.dumps(
        [kind, words, asdict(evidence), [(upload.extension, hashlib.sha256(upload.data).hexdigest())
                                       for upload in uploads]], sort_keys=True).encode()).hexdigest()
    folder = _request_folder(request_id)
    try:
        folder.mkdir(mode=0o700, parents=True)
        request_exists = False
    except FileExistsError:
        request_exists = True
    pending_file = folder / "pending.json"
    receipt_file = folder / "receipt.json"
    try:
        if request_exists:
            # Only a receipt proves the first writer finished publication.
            receipt = _request_hint(address, receipt_only=True)
            hint = _request_hint(address) if isinstance(receipt, NoRequestFile) else receipt
            try:
                found = locate_native_submission(address, hint, members=members)
            except OSError:
                found = NoNativeSubmission()
            if isinstance(receipt, NativeSubmissionDestination) and isinstance(found, LocatedSubmission):
                _check_same_request(receipt, request, payload_hash)
                _check_same_request(found.destination, request, payload_hash)
                return _complete_receipt(folder, found.destination, receipt)
        with _locked_request(address, members, for_send=True) as lookup:
            hint, located, current = lookup.hint, lookup.native, lookup.members
            if isinstance(located, LocatedSubmission):
                saved = located.destination
                _check_same_request(saved, request, payload_hash)
                return _complete_receipt(folder, saved, hint)
            if receipt_file.is_file():
                raise OSError(f"Cannot find the native record for request {request_id}.")
            if isinstance(hint, UnreadableRequestFile):
                raise hint.error
            if not isinstance(hint, NoRequestFile):
                _check_same_request(hint, request, payload_hash)

            assert current is not None
            members = current
            recipient = _active_recipient(request.recipient_id, members)
            destination = _proposal_folder(recipient) if kind == SubmissionKind.PROPOSAL else NoNativeFolder(kind)
            if kind == SubmissionKind.PROPOSAL and isinstance(destination, NoNativeFolder):
                raise SubmissionError("Select a recipient with a native proposal folder.")
            if isinstance(hint, NoRequestFile):
                _write_request_file(pending_file, {"payload_hash": payload_hash})
            atomic_file.sync_folder(folder.parent)
            atomic_file.sync_folder(folder.parent.parent)
            images = _save_images(folder, request, uploads)
            details = ReviewSubmission(request_id, payload_hash, evidence, images, new_reply_token())
            title = next(line.strip() for line in words.splitlines() if line.strip())[:120].rstrip()
            destinations: list[NativeSubmissionDestination] = []

            def before_write(identity: str, path: Path) -> None:
                path = path.resolve()
                saved = NativeSubmissionDestination(identity, kind, address.recipient_id, request_id, payload_hash, path, path,
                                                    recipient.id)
                _write_location(pending_file, saved)
                destinations[:] = [saved]

            _file(kind, recipient, title, words, details, members, destination, before_write)
            saved, = destinations
            return _complete_receipt(folder, saved, saved, first_send=not request_exists and isinstance(hint, NoRequestFile))
    except (investigation.InvestigationError, proposal_issue.Refusal, update_registration.RegistrationFailure,
            update_registration.InputError, Refusal) as error:
        raise SubmissionError(str(error)) from error


def _request_address(kind: SubmissionKind, recipient_id: staff.StaffId, request_id: str) -> SubmissionAddress:
    require_request_binding(kind, recipient_id, request_id)
    return SubmissionAddress(kind, recipient_id, request_id)


@contextmanager
def scoped_submission(scoped: ScopedSubmissionKey, *, members: staff.StaffSnapshot | None = None
                      ) -> Generator[LocatedSubmission]:
    """Select the exact native record and hold its lock through the action."""
    address = _request_address(scoped.kind, scoped.recipient_id, scoped.request_id)
    with _locked_request(address, members) as lookup:
        located = lookup.native
        if not isinstance(located, LocatedSubmission) or located.destination.id != scoped.item_id:
            raise SubmissionError("The sent item does not exist.")
        yield located


@contextmanager
def sent_question_path(scoped: ScopedSubmissionKey, members: staff.StaffSnapshot) -> Generator[Path]:
    """Give the Question command its native path while holding the recipient lock."""
    with scoped_submission(scoped, members=members) as located:
        yield located.destination.path


def answer(key: str, words: str, reply_token: str, *, members: staff.StaffSnapshot | None = None) -> bool:
    """Apply the scoped reply to the native record."""
    scoped = read_submission_key(key)
    words = read_words(words.strip(), "Answer")
    try:
        with scoped_submission(scoped, members=members) as located:
            path = located.destination.path
            owner = located.destination.owner
            match scoped.kind:
                case SubmissionKind.QUESTION:
                    return update_registration.reply_to_submission(path, scoped, words, reply_token, owner=owner)
                case SubmissionKind.PROPOSAL:
                    return proposal_issue.reply_to_submission(path, scoped, words, reply_token, owner=owner)
                case SubmissionKind.BUG | SubmissionKind.INVESTIGATION:
                    return investigation.answer_submission(path, scoped, words, reply_token, owner=owner)
    except (investigation.InvestigationError, proposal_issue.Refusal, update_registration.RegistrationFailure) as error:
        raise SubmissionError(str(error)) from error
