"""Store items sent from the New item dialog and their replies."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, TypedDict, cast

from scripts.agents.remote import local_machine, this_machine
from scripts.lib import atomic_file, conf
from scripts.lib.file_lock import exclusive
from scripts.lib.user_zone import user_zone
from scripts.production import investigation, producer_map, proposal_issue, record_store, staff, update_registration
from scripts.production.issues import Issue
from scripts.production.production_doc import WRAPPED, Refusal
from scripts.review_links import image_store, list_draft, new_item_dialog, page_frame, reply_tokens, review_events, review_link
from scripts.review_links.new_item_dialog import (
    KEY_PREFIX as KEY_PREFIX, NATIVE_ID_PREFIX, check_recipient_id,
    ScopedSubmissionKey, read_submission_key, require_request_binding, submission_key as submission_key,
    IMAGE_FORMATS, MAX_IMAGES, MAX_IMAGE_BYTES, MAX_TOTAL_IMAGE_BYTES, ImageReference, max_base64_size,
    read_kind, read_request_id, read_words, read_submission,
    ByRequestId, BuildCell as BuildCell, BuildStepEvidence as BuildStepEvidence, Evidence as Evidence,
    MemoryReading as MemoryReading,
    MemorySegment as MemorySegment, MemoryTone as MemoryTone, NewItemRequest as NewItemRequest,
    NoEvidence as NoEvidence, PastedImage as PastedImage, ReviewSubmission as ReviewSubmission,
    SubmissionConflict as SubmissionConflict, SubmissionError as SubmissionError, SubmissionKind as SubmissionKind,
    TipCaption as TipCaption, TipDetail as TipDetail, TipField as TipField,
    read_evidence as read_evidence, read_request as read_request,
)
from scripts.review_links.list_draft import (
    ListAssignmentNotice as ListAssignmentNotice, ListDraftClaim as ListDraftClaim,
    ListDraftOrigin as ListDraftOrigin, NewItemDraft as NewItemDraft, NoNewItemDraft as NoNewItemDraft,
)

DRAFT_ID = re.compile(r"r2-[0-9a-f]{32}")
BROWSER_DRAFT_ID = re.compile(r"r6-[0-9a-f]{32}")
LEGACY_DRAFT_BINDING = re.compile(r"(r2-[0-9a-f]{32})-.+")
BROWSER_DRAFT_BINDING = re.compile(r"(r6-[0-9a-f]{32})-.+")
PAGE_SCREENSHOT = ImageReference("000000000000", "image.png")


def is_draft_id(value: object) -> bool:
    return isinstance(value, str) and BROWSER_DRAFT_ID.fullmatch(value) is not None


def submission_title(words: str) -> str:
    """The title of a New item: its first line with words, at most 120 characters."""
    return next(line.strip() for line in words.splitlines() if line.strip())[:120].rstrip()


@dataclass(frozen=True)
class CurrentBrowserDraftToken:
    digest: str = field(repr=False)


@dataclass(frozen=True)
class BrowserDraftRecoveryToken:
    digest: str = field(repr=False)


type BrowserDraftAuthorization = CurrentBrowserDraftToken | BrowserDraftRecoveryToken
_browser_draft_token: ContextVar[BrowserDraftAuthorization | None] = ContextVar("browser_draft_token", default=None)


@contextmanager
def browser_draft_token(authorization: BrowserDraftAuthorization) -> Generator[None]:
    """Use the HTTP token for this browser draft only."""
    scope = _browser_draft_token.set(authorization)
    try:
        yield
    finally:
        _browser_draft_token.reset(scope)


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
    browser_token_digest: str = field(default="", kw_only=True, repr=False)
    has_capture_intent: bool = field(default=False, kw_only=True)

    @property
    def address(self) -> SubmissionAddress:
        return SubmissionAddress(self.kind, self.recipient_id, self.request_id)


@dataclass(frozen=True)
class MoveContentDigests:
    """The digests of both copies when the move was planned."""

    source: str
    destination: str


@dataclass(frozen=True)
class MissingMoveDigests:
    """The move journal lacks a digest for one or both copies."""


@dataclass(frozen=True)
class SubmissionMove(NativeSubmissionDestination):
    """A native transfer that can resume before or after source removal."""

    source: Path
    source_owner: staff.StaffId
    digests: MoveContentDigests | MissingMoveDigests


@dataclass(frozen=True)
class ListCommandAssignee:
    """The staff member the command names for the List row."""

    id: staff.StaffId


class ListWindowAssignee(Enum):
    RECIPIENT = "recipient"


@dataclass(frozen=True)
class InitialDelivery:
    """File a console item and send its first message."""

    evidence_origin: str = ""
    list_assignee: ListCommandAssignee | ListWindowAssignee = ListWindowAssignee.RECIPIENT


@dataclass(frozen=True)
class NoInitialDelivery:
    """File the native record without scheduling its first message."""


NO_INITIAL_DELIVERY = NoInitialDelivery()


def list_command_assignee(value: object) -> ListCommandAssignee:
    if not isinstance(value, str):
        raise SubmissionError("Invalid List assignee ID.")
    identity = staff.StaffId(value)
    check_recipient_id(identity)
    return ListCommandAssignee(identity)


type NativeSubmittedRecord = update_registration.QuestionRecord | Issue | investigation.Investigation


def native_origin(record: NativeSubmittedRecord) -> new_item_dialog.SubmissionOrigin:
    """Read the typed origin from a native record."""
    if isinstance(record, update_registration.QuestionRecord):
        details = record.item.get("review_submission")
        return read_submission(details) if details is not None else new_item_dialog.NoReviewSubmission()
    if isinstance(record, Issue):
        return proposal_issue.review_submission(record)
    return record.submission


@dataclass(frozen=True)
class QuestionResponse:
    """The latest answer to a Question from New item."""

    question: str
    answer: str
    answered_at: datetime | None


def question_response(record: update_registration.QuestionRecord,
                      log: tuple[review_events.ReviewLog, ...]) -> QuestionResponse | None:
    fields = record.item
    exchanges = fields.get("followups", [])
    if "review_submission" not in fields or "followup" in fields or not exchanges:
        return None
    answer = exchanges[-1]["reply"]
    answered_at = next((event.time for event in reversed(log)
                        if event.who == record.owner and event.words == "Answer: " + answer), None)
    return QuestionResponse(fields.get("question", exchanges[0]["followup"]), answer, answered_at)


@dataclass(frozen=True)
class VerifiedAttachments:
    """Every saved attachment is available."""


@dataclass(frozen=True)
class UnavailableAttachments:
    """The saved attachments that this retry could not restore."""

    references: tuple[ImageReference, ...]


@dataclass(frozen=True)
class UncheckedAttachments:
    """A record lookup did not check its attachments."""


type SavedAttachments = VerifiedAttachments | UnavailableAttachments | UncheckedAttachments


@dataclass(frozen=True)
class SavedSubmission(NativeSubmissionDestination):
    """The receipt for a completed native write."""

    record: NativeSubmittedRecord = field(compare=False)
    reused: bool = field(default=False, compare=False)
    attachments: SavedAttachments = field(kw_only=True, compare=False)

    @property
    def words(self) -> str:
        match self.record:
            case update_registration.QuestionRecord(item=item):
                return item.get("question", item["text"])
            case investigation.Investigation(question=question):
                return question
            case Issue(path=path):
                text = path.read_text(encoding="utf-8").removeprefix("---\n")
                return text.partition("\n---\n")[2].removeprefix("\n").removesuffix("\n")

    @property
    def key(self) -> str:
        return submission_key(self.kind, self.recipient_id, self.id, self.request_id)

    @property
    def details(self) -> ReviewSubmission:
        details = native_origin(self.record)
        if not isinstance(details, ReviewSubmission):
            raise OSError(f"Cannot read the native Review origin for {self.id}.")
        return details

    @property
    def awaiting_reply(self) -> bool:
        return _awaiting_reply(self.record)

    @property
    def reply_token(self) -> str:
        return self.details.reply_token if self.awaiting_reply else ""


def _awaiting_reply(record: NativeSubmittedRecord) -> bool:
    match record:
        case update_registration.QuestionRecord():
            return update_registration.pending_review_reply(record)
        case Issue():
            return proposal_issue.pending_review_reply(record)
        case investigation.Investigation():
            return record.state == investigation.InvestigationState.OPEN


@dataclass(frozen=True)
class EditedSubmission(SavedSubmission):
    """The draft already names a saved item."""


@dataclass(frozen=True)
class ListDraftFiling:
    """The fields and capture prepared for the first filing of a List draft."""

    request: NewItemRequest
    screenshot: new_item_dialog.SubmissionScreenshot
    capture_requested: bool
    refusal: SubmissionError | None = None


@dataclass(frozen=True)
class RequestedListPreview:
    """A preview the sender selected for the List draft."""

    key: str


class NoRequestedListPreview(Enum):
    ABSENT = "no requested preview"


type ListPreviewRequest = RequestedListPreview | NoRequestedListPreview


@dataclass(frozen=True)
class ListDraftSend:
    """A List draft with fields to read for filing or retry comparison."""

    request_id: str
    prepare: Callable[[], ListDraftFiling]
    preview: ListPreviewRequest = NoRequestedListPreview.ABSENT


@dataclass(frozen=True)
class _ImageUpload:
    reference: ImageReference
    extension: str
    data: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


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
        reference = ImageReference(f"{len(uploads) + 1:012x}", f"image.{extension}")
        uploads.append(_ImageUpload(reference, extension, data))
    return tuple(uploads)


def _identify_image(extension: str, data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["magick", "identify", "-regard-warnings", "-limit", "width", "8192",
         "-limit", "height", "8192", "-limit", "area", "32MP",
         "-limit", "list-length", "32", "-limit", "memory", "128MiB",
         "-limit", "map", "128MiB", "-limit", "disk", "0",
         "-format", "%m\n", extension + ":-"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4, check=False)


def _validate_image(upload: _ImageUpload) -> None:
    try:
        result = _identify_image(upload.extension, upload.data)
    except subprocess.TimeoutExpired:
        raise SubmissionError("The pasted image could not be read in time.") from None
    names = result.stdout.splitlines()
    if result.returncode or not names or any(name != upload.extension.upper().encode() for name in names):
        raise SubmissionError("The pasted image does not match its type or exceeds the image limits.")


def _attachment_root(snapshot_root: Path | None = None) -> Path:
    snapshot_root = (review_link.review_root() if snapshot_root is None else snapshot_root).resolve()
    configured = conf.read_or_repository("review_links").text("review_attachments_root")
    root = (snapshot_root.parent / Path(configured).expanduser()).resolve()
    if root.is_relative_to(snapshot_root) or root.is_relative_to(record_store.state_root().resolve()):
        raise OSError("The Review attachment root must be outside the snapshot and staff state roots.")
    return root


def _request_folder(request_id: str) -> Path:
    identity = read_request_id(request_id)
    draft = LEGACY_DRAFT_BINDING.fullmatch(identity) or BROWSER_DRAFT_BINDING.fullmatch(identity)
    return _attachment_root() / (draft[1] if draft else identity)


def image_folder(item: SavedSubmission | ReviewSubmission) -> Path:
    """Return the private folder for the image IDs on the native record."""
    return _request_folder(item.request_id) / "images"


def _save_images(folder: Path, request: NewItemRequest, uploads: tuple[_ImageUpload, ...]) -> None:
    references = tuple(upload.reference for upload in uploads)
    if not references:
        return
    images = folder / "images"
    if images.exists():
        for reference, upload in zip(references, uploads, strict=True):
            if (images / reference.id / reference.file).read_bytes() != upload.data:
                raise OSError("A saved Review image does not match the request.")
        return

    # Publish the complete image batch with one rename.
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=folder) as temporary:
        workspace = Path(temporary)
        stage = workspace / "images"
        for reference, upload in zip(references, uploads, strict=True):
            if reference != PAGE_SCREENSHOT:
                _validate_image(upload)
            source = workspace / reference.file
            atomic_file.write_bytes_atomic(source, upload.data, mode=0o600)
            _ = image_store.register_file(source, stage, owner=request.recipient_id,
                                          subject="Review request " + request.request_id, identity=reference.id, private=True)
        atomic_file.sync_folder(stage)
        _ = stage.rename(images)
        atomic_file.sync_folder(folder)


def _restore_saved_image(images: Path, saved: NativeSubmissionDestination, upload: _ImageUpload) -> None:
    if upload.reference != PAGE_SCREENSHOT:
        _validate_image(upload)
    folder = images / upload.reference.id
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_file.write_bytes_atomic(folder / upload.reference.file, upload.data, mode=0o600)
    if not (folder / image_store.RECORD).is_file():
        image_store.write_record(folder, {
            "id": upload.reference.id, "file": upload.reference.file, "title": "", "look_for": [], "owner": saved.recipient_id,
            "subject": "Review request " + saved.request_id,
            "registered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        })
    atomic_file.sync_folder(images)
    atomic_file.sync_folder(images.parent)


def _verify_saved_images(saved: NativeSubmissionDestination, record: NativeSubmittedRecord,
                         retry_uploads: tuple[_ImageUpload, ...], *, payload_matches: bool
                         ) -> VerifiedAttachments | UnavailableAttachments:
    details = native_origin(record)
    if not isinstance(details, ReviewSubmission):
        raise OSError(f"Cannot read the native Review origin for {saved.id}.")
    images = image_folder(details)
    originals = dict(details.image_digests)
    candidates = {upload.reference: upload for upload in retry_uploads}
    missing: list[ImageReference] = []
    for reference in details.images:
        folder = images / reference.id
        path = folder / reference.file
        metadata = folder / image_store.RECORD
        if any(entry.is_symlink() for entry in (images, folder, path, metadata)):
            missing.append(reference)
            continue
        try:
            description = image_store.read_record(folder)
        except FileNotFoundError:
            description = None
        except ValueError:
            missing.append(reference)
            continue
        if description is not None and description["file"] != reference.file:
            missing.append(reference)
            continue
        digest = originals.get(reference.id)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_TOTAL_IMAGE_BYTES + 1)
        except FileNotFoundError:
            data = b""
        if description is not None and 0 < len(data) <= MAX_TOTAL_IMAGE_BYTES:
            if digest is not None:
                if hashlib.sha256(data).hexdigest() == digest:
                    continue
            else:
                try:
                    _validate_image(_ImageUpload(reference, Path(reference.file).suffix.removeprefix("."), data))
                except SubmissionError:
                    pass
                else:
                    continue
        upload = candidates.get(reference)
        matches = upload is not None and (upload.digest == digest if digest is not None else payload_matches)
        if not matches or upload is None:
            missing.append(reference)
            continue
        _restore_saved_image(images, saved, upload)
    return UnavailableAttachments(tuple(missing)) if missing else VerifiedAttachments()


@dataclass(frozen=True)
class NoRequestFile:
    """The request has no file at this location."""


@dataclass(frozen=True)
class PendingFiling:
    """The request payload is fixed but has no planned native record yet."""
    payload_hash: str


@dataclass(frozen=True)
class ListItemDestination:
    id: str
    claim: ListDraftClaim
    confirmed: tuple[staff.StaffId, ...] = ()

    @property
    def payload_hash(self) -> str:
        return self.claim.payload_hash


class DraftAlreadyFiled(SubmissionConflict):
    def __init__(self, identity: str) -> None:
        super().__init__(f"This item was already filed as {identity}. Any change after that Send was not sent.")
        self.id: str = identity


class DraftPending(SubmissionConflict):
    def __init__(self) -> None:
        super().__init__("Retry the original Send for this draft.")


@dataclass(frozen=True)
class UnreadableRequestFile:
    """The request file exists but cannot supply a lookup hint."""

    error: OSError


_NO_REQUEST_FILE = NoRequestFile()
type FilingDestination = NativeSubmissionDestination | ListItemDestination
type RequestFile = FilingDestination | PendingFiling | NoRequestFile | UnreadableRequestFile


def _list_request_file(path: Path, fields: dict[str, object], payload_hash: str) -> ListItemDestination:
    claim = list_draft.read_claim(fields, payload_hash)
    identity = fields.get("id")
    confirmed = fields.get("confirmed", [])
    if (path.name not in ("pending.json", "receipt.json") or not isinstance(identity, str)
            or not re.fullmatch(r"L[1-9][0-9]*", identity) or not isinstance(confirmed, list)):
        raise ValueError("Invalid List receipt.")
    recipients = cast(list[object], confirmed)
    if not all(isinstance(recipient, str) for recipient in recipients):
        raise ValueError("Invalid List receipt recipients.")
    return ListItemDestination(identity, claim, tuple(staff.StaffId(recipient) for recipient in cast(list[str], recipients)))


def _read_request_file(path: Path, request_id: str) -> RequestFile:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoRequestFile()
    except (ValueError, UnicodeError) as error:
        raise OSError(f"Cannot read the Review request file at {path}.") from error

    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        payload_hash = fields.get("payload_hash", fields.get("fingerprint"))
        if isinstance(payload_hash, str) and re.fullmatch(r"[0-9a-f]{64}", payload_hash):
            if fields.get("destination") == "list":
                try:
                    if not new_item_dialog.SCHEDULED_DRAFT_ID.fullmatch(request_id):
                        raise ValueError("Invalid Scheduled draft ID.")
                    return _list_request_file(path, fields, payload_hash)
                except ValueError as error:
                    raise OSError(f"Cannot read the Review request file at {path}.") from error
            if fields.keys() == {"payload_hash"} and path.name == "pending.json":
                return PendingFiling(payload_hash)
            kind, recipient = fields.get("kind"), fields.get("recipient")
            owner = fields.get("owner", recipient)
            identity, native_path = fields.get("id"), fields.get("path")
            original_path = fields.get("original_path", native_path)
            token_digest = fields.get("browser_token_digest", "")
            if (isinstance(kind, str) and kind in SubmissionKind and isinstance(recipient, str)
                    and isinstance(owner, str) and isinstance(identity, str) and isinstance(native_path, str)
                    and isinstance(original_path, str) and isinstance(token_digest, str)
                    and ("browser_token_digest" not in fields or re.fullmatch(r"[0-9a-f]{64}", token_digest))):
                kind = SubmissionKind(kind)
                if (re.fullmatch(NATIVE_ID_PREFIX[kind] + r"[1-9][0-9]*", identity)
                        and Path(native_path).is_absolute() and Path(original_path).is_absolute()):
                    filing_owner = staff.StaffId(owner)
                    try:
                        check_recipient_id(filing_owner)
                        check_recipient_id(staff.StaffId(recipient))
                    except SubmissionError as error:
                        raise OSError(f"Cannot read the Review request file at {path}.") from error
                    if path.name == "list-capture-target.json" or "request_id" in fields:
                        request_id = read_request_id(fields.get("request_id"))
                        require_request_binding(kind, staff.StaffId(recipient), request_id)
                    elif DRAFT_ID.fullmatch(request_id) or BROWSER_DRAFT_ID.fullmatch(request_id):
                        request_id = f"{request_id}-{kind}-{recipient}"
                    location = (identity, kind, staff.StaffId(recipient), request_id,
                                payload_hash, Path(native_path), Path(original_path), filing_owner)
                    has_capture_intent = fields.get("capture_intent") == "yes"
                    if path.name == "move.json":
                        source, source_owner = fields.get("source"), fields.get("source_owner")
                        if not (isinstance(source, str) and Path(source).is_absolute() and isinstance(source_owner, str)):
                            raise OSError(f"Cannot read the Review move at {path}.")
                        former = staff.StaffId(source_owner)
                        check_recipient_id(former)
                        source_digest, destination_digest = fields.get("source_digest"), fields.get("destination_digest")
                        digests = (MoveContentDigests(source_digest, destination_digest)
                                   if isinstance(source_digest, str) and re.fullmatch(r"[0-9a-f]{64}", source_digest)
                                   and isinstance(destination_digest, str) and re.fullmatch(r"[0-9a-f]{64}", destination_digest)
                                   else MissingMoveDigests())
                        return SubmissionMove(*location, Path(source), former, digests,
                                              browser_token_digest=token_digest, has_capture_intent=has_capture_intent)
                    return NativeSubmissionDestination(*location, browser_token_digest=token_digest,
                                                       has_capture_intent=has_capture_intent)
    raise OSError(f"Cannot read the Review request file at {path}.")


def _check_same_request(saved: NativeSubmissionDestination | PendingFiling, request: NewItemRequest,
                        payload_hash: str, *, draft: bool = False) -> None:
    if saved.payload_hash != payload_hash or (isinstance(saved, NativeSubmissionDestination)
            and (saved.kind != request.kind or saved.recipient_id != request.recipient_id)):
        if draft:
            raise OSError(f"The Review fingerprint does not match request {request.request_id}.")
        raise SubmissionConflict("This request ID already has different item data.")


def _image_fingerprints(uploads: tuple[_ImageUpload, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((upload.extension, upload.digest) for upload in uploads)


def _fingerprint(payload: list[object], *,
                 notice: new_item_dialog.Notice = new_item_dialog.DefaultSubmissionNotice.FILED_KIND_FROM_REVIEW) -> str:
    if isinstance(notice, new_item_dialog.SubmissionNotice):
        payload = [*payload, asdict(notice)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _legacy_draft_fingerprint(request_id: str, request: NewItemRequest, words: str,
                              uploads: tuple[_ImageUpload, ...]) -> str:
    return _fingerprint([request_id, request.kind, words, _image_fingerprints(uploads)], notice=request.notice)


def _submission_fingerprint(request: NewItemRequest, words: str, uploads: tuple[_ImageUpload, ...]) -> str:
    payload: list[object] = [request.kind, words, new_item_dialog.evidence_fields(request.evidence), _image_fingerprints(uploads)]
    if request.page:
        payload.append(request.page)
    if request.page_recipient != new_item_dialog.PageRecipient.PAGE_OWNER:
        payload.append(request.page_recipient)
    if request.page_recipient == new_item_dialog.PageRecipient.PRODUCER:
        payload.append(new_item_dialog.assignment_id(request.assignment))
    return _fingerprint(payload, notice=request.notice)


def _write_request_file(path: Path, fields: Mapping[str, object]) -> None:
    atomic_file.write_text_atomic(path, json.dumps(fields) + "\n", new_file_mode=0o600)


def _write_location(path: Path, destination: FilingDestination) -> None:
    if isinstance(destination, ListItemDestination):
        _write_request_file(path, {"destination": "list", **destination.claim.fields(),
                                   "id": destination.id, "confirmed": destination.confirmed})
        return
    fields: dict[str, object] = {"destination": "native","id": destination.id, "kind": destination.kind,
              "recipient": destination.recipient_id, "path": str(destination.path),
              "original_path": str(destination.original_path), "payload_hash": destination.payload_hash}
    if destination.owner != destination.recipient_id:
        fields["owner"] = destination.owner
    if destination.browser_token_digest:
        fields["browser_token_digest"] = destination.browser_token_digest
    if destination.has_capture_intent:
        fields["capture_intent"] = "yes"
    draft_id = path.parent.name
    if path.name == "list-capture-target.json" or (
            is_draft_id(draft_id) and _is_earlier_native_draft(path.parent, destination)):
        fields["request_id"] = destination.request_id
    if isinstance(destination, SubmissionMove):
        fields.update(source=str(destination.source), source_owner=destination.source_owner)
        if path.name == "move.json" and isinstance(destination.digests, MoveContentDigests):
            fields.update(source_digest=destination.digests.source, destination_digest=destination.digests.destination)
    _write_request_file(path, fields)


def _hint_paths(address: SubmissionAddress, hint: RequestFile) -> tuple[Path, ...]:
    if isinstance(hint, NativeSubmissionDestination) and hint.address == address:
        return tuple(dict.fromkeys((hint.path, hint.original_path)))
    return ()


def _read_location_hint(folder: Path, *, receipt_only: bool = False) -> RequestFile:
    receipt = _read_request_file(folder / "receipt.json", folder.name)
    if receipt_only or not isinstance(receipt, NoRequestFile):
        return receipt
    pending = _read_request_file(folder / "pending.json", folder.name)
    if not isinstance(pending, NoRequestFile):
        return pending
    return _read_request_file(folder / "moved.json", folder.name)


def _request_hint(address: SubmissionAddress, *, receipt_only: bool = False) -> RequestFile:
    folder = _request_folder(address.request_id)
    try:
        return _read_location_hint(folder, receipt_only=receipt_only)
    except OSError as error:
        return UnreadableRequestFile(error)


def _is_investigation(address: SubmissionAddress) -> bool:
    return address.kind in (SubmissionKind.BUG, SubmissionKind.INVESTIGATION)


@dataclass(frozen=True)
class LocatedSubmission:
    """The matching request metadata read from a native record."""
    destination: NativeSubmissionDestination
    details: ReviewSubmission
    record: NativeSubmittedRecord

    def saved(self) -> SavedSubmission:
        """Return the native record with its stable request location."""
        return _saved_submission(self.destination, self.record, attachments=UncheckedAttachments())


@dataclass(frozen=True)
class NoNativeSubmission:
    """No native record proves that this request was written."""


def _draft_address(kind: SubmissionKind, recipient: staff.StaffId, fingerprint: str, *,
                   page: str = "") -> SubmissionAddress:
    binding = f"r5-{_fingerprint([fingerprint, page])}" if page else f"r4-{fingerprint}"
    return SubmissionAddress(kind, recipient, f"{binding}-{kind}-{recipient}")


@dataclass(frozen=True)
class SubmissionLookup:
    """The file hint and the native result under the recipient locks."""

    hint: RequestFile
    native: LocatedSubmission | NoNativeSubmission
    members: staff.StaffSnapshot | None
    address: SubmissionAddress
    recipient: staff.StaffRow | None
    bind_draft: Callable[[], None] = lambda: None


@contextmanager
def _locked_request(address: SubmissionAddress, members: staff.StaffSnapshot | None = None, *,
                    for_send: bool = False, page: str = "", draft_fingerprint: str = "",
                    recovered: NativeSubmissionDestination | NoRequestFile = _NO_REQUEST_FILE,
                    search_owner_stores: bool = True,
                    list_recipient: staff.StaffId | None = None,
                    page_recipient: new_item_dialog.PageRecipient = new_item_dialog.PageRecipient.PAGE_OWNER
                    ) -> Generator[SubmissionLookup]:
    owners = {address.recipient_id}
    while True:
        with investigation.recipient_locks(*owners):
            current = members
            target = address
            recipient = None
            if page and draft_fingerprint:
                current = staff.snapshot()
                recipient = _send_recipient(address, page, current, page_recipient)
                if recipient.id not in owners:
                    owners.add(recipient.id)
                    continue
                list_owner = page == "list" or page_recipient == new_item_dialog.PageRecipient.LIST_OWNER
                binding = address.recipient_id if list_owner else recipient.id
                target = _draft_address(address.kind, binding, draft_fingerprint, page=page)

            hint = _request_hint(target)
            if isinstance(hint, (NoRequestFile, PendingFiling)) and isinstance(recovered, NativeSubmissionDestination):
                hint = recovered
            if list_recipient is not None and not _list_draft_matches(target, hint, list_recipient):
                yield SubmissionLookup(hint, NoNativeSubmission(), current, target, recipient)
                return
            if isinstance(hint, NativeSubmissionDestination) and hint.address == target:
                required = {hint.owner, target.recipient_id}
                if not owners.issuperset(required):
                    owners.update(required)
                    continue

            native: LocatedSubmission | NoNativeSubmission = NoNativeSubmission()
            # A draft gets a native location in pending.json before the write.
            find_native = not draft_fingerprint or isinstance(hint, NativeSubmissionDestination)
            if find_native:
                native = locate_native_submission(target, hint, search_stores=False)
            if isinstance(native, NoNativeSubmission):
                if for_send and recipient is None:
                    current = staff.snapshot()
                if find_native and search_owner_stores:
                    native = locate_native_submission(target, hint, members=current)
            required: set[staff.StaffId] = set()
            if isinstance(native, LocatedSubmission):
                required = {native.destination.owner}
            elif for_send and recipient is None:
                assert current is not None
                if not page:
                    required = set(investigation.owner_chain(address.recipient_id, current))
                    if not owners.issuperset(required):
                        owners.update(required)
                        continue
                recipient = _send_recipient(address, page, current, page_recipient)
                required = {recipient.id}
            if not owners.issuperset(required):
                owners.update(required)
                continue
            yield SubmissionLookup(hint, native, current, target, recipient)
            return


def _draft_binding(identity: str, kind: SubmissionKind, fingerprint: str, path: Path) -> SubmissionAddress:
    try:
        recipient = new_item_dialog.request_recipient(kind, identity)
    except SubmissionError as error:
        raise OSError(f"Cannot read the Review request at {path}.") from error
    standard = _draft_address(kind, recipient, fingerprint)
    if identity == standard.request_id or any(
        identity == _draft_address(kind, recipient, fingerprint, page=page).request_id
        for page in page_frame.OWNER_PAGES
    ):
        return SubmissionAddress(kind, recipient, identity)
    raise OSError(f"Cannot read the Review request at {path}.")


def _read_draft_marker(path: Path, kind: SubmissionKind, fingerprint: str) -> SubmissionAddress | None:
    try:
        identity = path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError:
        return None
    return _draft_binding(identity, kind, fingerprint, path)


def _write_draft_marker(path: Path, address: SubmissionAddress) -> None:
    atomic_file.write_text_atomic(path, address.request_id + "\n", new_file_mode=0o600)


def _saved_draft(lookup: SubmissionLookup, *, list_recipient: staff.StaffId | None = None) -> LocatedSubmission | None:
    if list_recipient is not None and not _list_draft_matches(lookup.address, lookup.hint, list_recipient):
        return None
    if isinstance(lookup.native, LocatedSubmission):
        return lookup.native
    if isinstance(lookup.hint, UnreadableRequestFile):
        raise lookup.hint.error
    identity = lookup.address.request_id
    if (_request_folder(identity) / "receipt.json").is_file():
        raise OSError(f"Cannot find the native record for request {identity}.")
    return None


def _older_draft_bindings(folder: Path, kind: SubmissionKind, fingerprint: str,
                          members: staff.StaffSnapshot | None) -> dict[Path, SubmissionAddress]:
    addresses = [_draft_binding(path.name, kind, fingerprint, path)
                 for path in sorted(folder.parent.glob(f"{folder.name}-{kind}-*"))]
    for path in sorted(folder.glob("page-*")):
        address = _read_draft_marker(path, kind, fingerprint)
        if address is not None:
            addresses.append(address)
    bindings: dict[Path, SubmissionAddress] = {}
    for address in dict.fromkeys(addresses):
        with _locked_request(address, members, draft_fingerprint=fingerprint) as lookup:
            saved = _saved_draft(lookup)
            if saved is not None:
                marker = folder / f"recipient-{saved.destination.owner}"
                if not marker.exists():
                    _ = bindings.setdefault(marker, address)
    return bindings


def _list_draft_matches(address: SubmissionAddress, hint: RequestFile, recipient: staff.StaffId) -> bool:
    if address.recipient_id == recipient:
        return True
    if isinstance(hint, NativeSubmissionDestination) and hint.address == address:
        return hint.owner == recipient
    return isinstance(hint, (NativeSubmissionDestination, UnreadableRequestFile))


def _list_draft_order(hint: RequestFile) -> tuple[bool, int]:
    # A move keeps the native ID. An unreadable receipt can hide a newer match.
    number = int(hint.id[1:]) if isinstance(hint, NativeSubmissionDestination) else 0
    return isinstance(hint, UnreadableRequestFile), number


def _list_draft_candidates(folder: Path, address: SubmissionAddress,
                           fingerprint: str) -> dict[SubmissionAddress, RequestFile]:
    hints: dict[SubmissionAddress, RequestFile] = {}
    for marker in (folder / "page-list", *folder.glob("page-list-*")):
        try:
            previous = _read_draft_marker(marker, address.kind, fingerprint)
        except (OSError, UnicodeError):
            if marker.name.startswith("page-list-"):
                bound = staff.StaffId(marker.name.removeprefix("page-list-"))
                canonical = _draft_address(address.kind, bound, fingerprint, page="list")
                # A receipt for the List binding can prove that it belongs to another owner.
                hint = _request_hint(canonical, receipt_only=True)
                if (isinstance(hint, NativeSubmissionDestination) and hint.address == canonical
                        and not _list_draft_matches(canonical, hint, address.recipient_id)):
                    continue
            raise
        if previous is not None and previous not in hints:
            hints[previous] = _request_hint(previous)
    candidates = [previous for previous, hint in hints.items()
                  if _list_draft_matches(previous, hint, address.recipient_id)]
    candidates.sort(key=lambda previous: _list_draft_order(hints[previous]), reverse=True)
    return {previous: hints[previous] for previous in candidates}


@contextmanager
def _locked_draft_request(address: SubmissionAddress, members: staff.StaffSnapshot | None, *,
                          page: str, fingerprint: str,
                          page_recipient: new_item_dialog.PageRecipient) -> Generator[SubmissionLookup]:
    # Lock the content across page and standard Sends.
    folder, existed = _prepare_request_folder(f"r4-{fingerprint}")
    list_recipient = address.recipient_id if page == "list" else None
    marker = f"page-{page}"
    if list_recipient is not None:
        marker += f"-{list_recipient}"
    elif page_recipient != new_item_dialog.PageRecipient.PAGE_OWNER:
        marker += f"-recipient-{address.recipient_id}"
    page_marker = folder / marker
    standard = _draft_address(address.kind, address.recipient_id, fingerprint)
    with exclusive(folder / ".lock"):
        indexed = folder / ".indexed"
        needs_index = not indexed.is_file()
        older_bindings = (_older_draft_bindings(folder, address.kind, fingerprint, members)
                          if needs_index and existed else {})

        def publish_index() -> None:
            if needs_index:
                for marker, saved_address in older_bindings.items():
                    _write_draft_marker(marker, saved_address)
                atomic_file.write_text_atomic(indexed, "", new_file_mode=0o600)

        if page == "list":
            previous_addresses = list(_list_draft_candidates(folder, address, fingerprint))
        elif page:
            previous = _read_draft_marker(page_marker, address.kind, fingerprint)
            previous_addresses = [] if previous is None else [previous]
        else:
            previous_addresses = [standard]
        checked: dict[SubmissionAddress, RequestFile] = {}
        while True:
            for previous in previous_addresses:
                with _locked_request(previous, members, draft_fingerprint=fingerprint,
                                     list_recipient=list_recipient) as lookup:
                    checked[previous] = lookup.hint
                    saved = _saved_draft(lookup, list_recipient=list_recipient)
                    if saved is None:
                        continue
                    if page == "list" and address.recipient_id not in (saved.destination.recipient_id,
                                                                      saved.destination.owner):
                        continue
                    if page:
                        atomic_file.sync_folder(folder)
                    publish_index()
                    yield lookup
                    return
            previous_addresses = []

            recipient = _send_recipient(address, page, staff.snapshot(), page_recipient).id
            recipient_marker = folder / f"recipient-{recipient}"
            candidate = _read_draft_marker(recipient_marker, address.kind, fingerprint) or older_bindings.get(recipient_marker)
            if candidate is not None and candidate not in checked:
                with _locked_request(candidate, members, draft_fingerprint=fingerprint,
                                     list_recipient=list_recipient) as match:
                    saved = _saved_draft(match, list_recipient=list_recipient)
                    if saved is not None and saved.destination.owner == recipient:
                        if _send_recipient(address, page, staff.snapshot(), page_recipient).id != recipient:
                            continue
                        publish_index()
                        if page:
                            _write_draft_marker(page_marker, match.address)
                        yield match
                        return

            with _locked_request(address if page else standard, members, for_send=True, page=page, draft_fingerprint=fingerprint,
                                 page_recipient=page_recipient) as lookup:
                current = lookup.recipient
                if current is not None and current.id != recipient:
                    continue
                if list_recipient is not None:
                    latest = _list_draft_candidates(folder, address, fingerprint)
                    previous_addresses = [previous for previous, hint in latest.items()
                                          if checked.get(previous) != hint]
                    if previous_addresses:
                        # Release the filing lock before taking the replay locks in order.
                        continue
                def bind_draft() -> None:
                    publish_index()
                    _write_draft_marker(recipient_marker, lookup.address)
                    if page:
                        _write_draft_marker(page_marker, lookup.address)
                    atomic_file.sync_folder(folder.parent)

                yield replace(lookup, bind_draft=bind_draft)
                return


def _located(address: SubmissionAddress, path: Path, owner: staff.StaffId, identity: str,
             record: NativeSubmittedRecord) -> LocatedSubmission | NoNativeSubmission:
    details = native_origin(record)
    if not isinstance(details, ReviewSubmission) or details.request_id != address.request_id:
        return NoNativeSubmission()
    if not re.fullmatch(NATIVE_ID_PREFIX[address.kind] + r"[1-9][0-9]*", identity):
        raise OSError(f"The native Review record at {path} has an invalid item ID.")
    path = path.resolve()
    return LocatedSubmission(NativeSubmissionDestination(identity, address.kind, address.recipient_id,
                             address.request_id, details.request_digest, path, path, owner,
                             has_capture_intent=isinstance(details.capture_intent, new_item_dialog.DeferredCaptureIntent)),
                             details, record)


def _read_native_submission(address: SubmissionAddress, path: Path,
                            owner: staff.StaffId) -> LocatedSubmission | NoNativeSubmission:
    try:
        _ = path.stat()
        match address.kind:
            case SubmissionKind.QUESTION:
                records = update_registration.selected_question_records([path], (), None, sources=[], owner=owner)
                if records and "review_submission" in records[0].item:
                    item = records[0].item
                    return _located(address, path, owner, item.get("review_id", ""), records[0])
            case SubmissionKind.PROPOSAL:
                issue = Issue(path, record_store.read_header(path, strict=True))
                if proposal_issue.proposal_owner(issue, issue.value("proposed_by")) == owner and "proposal" in issue.fields.get("tags", []):
                    return _located(address, path, owner, issue.value("proposal_id"), issue)
            case SubmissionKind.INVESTIGATION | SubmissionKind.BUG:
                report = investigation.read_review_record(path)
                if (report.owner == owner and report.kind.value == address.kind.value
                        and isinstance(report.submission, ReviewSubmission)):
                    return _located(address, path, owner, report.id, report)
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
                return _read_native_submission(address, issue.path, owner)
        case SubmissionKind.QUESTION:
            record = update_registration.find_review_question_in_folder(
                update_registration.QuestionFolder(record_store.default_question_folder(owner), owner), lookup,
                other_folders=folders, document=document)
            if record is not None:
                item = record.item
                return _located(address, record.path, owner, item.get("review_id", ""), record)
        case SubmissionKind.INVESTIGATION | SubmissionKind.BUG:
            found = investigation.find_review_submission(
                owner, investigation.InvestigationKind(address.kind.value), address.request_id)
            if found is not None:
                path, report = found
                return _located(address, path, owner, report.id, report)
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
        if original is not None and _submission_owner_is_active(original, members):
            return NoNativeSubmission()
    except _UnreadableOwnerDocument:
        # Search for a saved request before refusing a new Send.
        pass

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
            if unreadable is not None:
                raise unreadable
            return NoNativeSubmission()
        try:
            located = _search_native_stores(address, owner, paths, staff.snapshot() if members is None else members)
        except OSError:
            if unreadable is not None:
                raise unreadable from None
            raise
    if isinstance(located, LocatedSubmission):
        if paths and isinstance(hint, NativeSubmissionDestination):
            destination = located.destination
            token_digest = hint.browser_token_digest if hint.payload_hash == destination.payload_hash else ""
            located = replace(located, destination=replace(destination, original_path=hint.original_path,
                                                          browser_token_digest=token_digest))
        return located
    if unreadable is not None:
        raise unreadable
    return NoNativeSubmission()


def _publish_receipt(folder: Path, saved: FilingDestination) -> None:
    # Sync the saved record directory before publishing the receipt.
    record_folder = (saved.path.parent if isinstance(saved, NativeSubmissionDestination)
                     else list_draft.list_records(saved.claim.keeper).folder())
    atomic_file.sync_folder(record_folder)
    receipt = folder / "receipt.json"
    try:
        os.replace(folder / "pending.json", receipt)
    except FileNotFoundError:
        if _read_request_file(receipt, folder.name) != saved:
            raise
    atomic_file.sync_folder(folder)


def _has_receipt_for(saved: NativeSubmissionDestination) -> bool:
    receipt = _request_hint(saved.address, receipt_only=True)
    return (isinstance(receipt, NativeSubmissionDestination)
            and (receipt.address, receipt.id, receipt.owner, receipt.payload_hash, receipt.browser_token_digest)
            == (saved.address, saved.id, saved.owner, saved.payload_hash, saved.browser_token_digest))


def _saved_submission(saved: NativeSubmissionDestination, record: NativeSubmittedRecord, *,
                      attachments: SavedAttachments, edited: bool = False) -> SavedSubmission:
    result = EditedSubmission if edited else SavedSubmission
    return result(saved.id, saved.kind, saved.recipient_id, saved.request_id, saved.payload_hash,
                  saved.path, saved.original_path, saved.owner, record, browser_token_digest=saved.browser_token_digest,
                  has_capture_intent=saved.has_capture_intent, attachments=attachments)


def _complete_receipt(folder: Path, saved: NativeSubmissionDestination, hint: RequestFile,
                      record: NativeSubmittedRecord, *, first_send: bool = False, edited: bool = False,
                      retry_uploads: tuple[_ImageUpload, ...] = (), payload_matches: bool = False) -> SavedSubmission:
    """Verify the saved attachments before returning the receipt."""
    attachments = _verify_saved_images(saved, record, retry_uploads, payload_matches=payload_matches)
    receipt = folder / "receipt.json"
    try:
        if receipt.is_file():
            if hint != saved:
                atomic_file.sync_folder(saved.path.parent)
                _write_location(receipt, saved)
            else:
                atomic_file.sync_folder(folder)
        else:
            pending = folder / "pending.json"
            if hint != saved or not pending.is_file():
                _write_location(pending, saved)
            _publish_receipt(folder, saved)
        if is_draft_id(folder.name):
            indexed = _read_location_hint(_draft_index(folder), receipt_only=True)
            if indexed != saved:
                _write_draft_index(folder, FiledDraft(saved))
    except OSError as error:
        if not first_send and not _has_receipt_for(saved):
            raise
        words = " ".join(str(error).splitlines())
        print(f"Review receipt for {saved.id}: {words}", file=sys.stderr)
    return _saved_submission(saved, record, edited=edited, attachments=attachments)


def _stage_initial_delivery(folder: Path, address: SubmissionAddress,
                            delivery: InitialDelivery | NoInitialDelivery) -> None:
    if isinstance(delivery, InitialDelivery):
        path = folder / "first-message.json"
        if not path.exists():
            _write_request_file(path, {"kind": address.kind, "request_id": address.request_id,
                                       "evidence_origin": delivery.evidence_origin})
        atomic_file.sync_folder(folder)


class ListFilingRoute(TypedDict):
    keeper: staff.StaffId
    owner: staff.StaffId
    producer: staff.StaffId
    unit: str
    source: str


@dataclass(frozen=True)
class PendingProducerFiling:
    """Keep the selected List owner and producer while the keeper is unavailable."""

    owner: staff.StaffId
    producer: staff.StaffId
    unit: str
    source: str


@dataclass(frozen=True)
class ListFilingUnavailable:
    """The List row has no producer that can take it."""

    reason: str
    retry: ListCommandAssignee | ListWindowAssignee | PendingProducerFiling = ListWindowAssignee.RECIPIENT


def _list_filing_producer(member: staff.StaffRow, members: staff.StaffSnapshot
                         ) -> staff.StaffRow | ListFilingUnavailable:
    """Use the nearest map row or producer on the owner chain without Git recovery."""
    config = conf.read(producer_map.STEM)
    if not isinstance(config.settings, dict) and config.settings.missing:
        repository = Path(__file__).resolve().parents[2] / "config" / f"{producer_map.STEM}.conf"
        config = conf.read(repository)
    if not isinstance(config.settings, dict):
        return ListFilingUnavailable(f"Cannot read the producer map at {config.source}: {config.settings.reason}")

    keys = {key.lower(): key for key in config.settings}
    producers = {row.id: row for row in staff.active(members, "producer")}
    for candidate in investigation.owner_chain(member.id, members):
        holder = members.by_id(candidate)
        if holder is None:
            continue
        if holder.kind == "producer":
            if candidate in producers:
                return producers[candidate]
            break
        key = keys.get(candidate.lower())
        if key is None:
            continue
        try:
            name = config.value(key, str, recover_invalid=False, allow_blank=True)
        except conf.Unusable as error:
            return ListFilingUnavailable(f"Cannot read the producer map at {error}")
        identity = staff.StaffId(name.lower())
        if identity in producers:
            return producers[identity]
        stale = members.by_id(identity)
        return ListFilingUnavailable(producer_map.StaleRow(holder, stale.name if stale is not None else identity).reason())
    return ListFilingUnavailable(f"{member.name} has no producer yet.")


def _plan_list_filing(assignee: ListCommandAssignee | ListWindowAssignee, recipient: staff.StaffRow,
                      members: staff.StaffSnapshot) -> ListFilingRoute | ListFilingUnavailable:
    keeper = record_store.list_keeper(conf.read_or_repository("console"), members)
    if keeper is None:
        return ListFilingUnavailable("The List has no active producer.", assignee)
    member = recipient
    source = "nate via New item"
    if isinstance(assignee, ListCommandAssignee):
        member = producer_map.find_member(assignee.id, members, producer_map.ASSIGNEE_KINDS)
        if (isinstance(member, producer_map.UnknownMember) or member.id != assignee.id
                or recipient.id not in investigation.owner_chain(member.id, members)):
            raise SubmissionConflict("The List assignee or recipient changed. Run /new_item again.")
        source = "nate via /new_item"
    producer = _list_filing_producer(member, members)
    if isinstance(producer, ListFilingUnavailable):
        return replace(producer, retry=assignee)
    unit = member.name if member.kind == "unit" else ""
    return ListFilingRoute(keeper=keeper.id, owner=recipient.id, producer=producer.id,
                           unit=unit, source=source)


def assigned_member(assignment: new_item_dialog.Assignment, recipient: staff.StaffRow,
                    members: staff.StaffSnapshot) -> staff.StaffRow:
    """Resolve the assignee from the selected producer route."""
    if isinstance(assignment, new_item_dialog.ProducerAssigns) or assignment.id == recipient.id:
        return recipient
    row = members.rows_by_id.get(assignment.id)
    if row is None or row.kind not in producer_map.ASSIGNEE_KINDS or not _page_owner_is_active(row, members):
        raise SubmissionConflict("The assignee is unavailable. Choose another assignee.")
    return row


def _plan_producer_filing(filing: PendingProducerFiling,
                          members: staff.StaffSnapshot) -> ListFilingRoute | ListFilingUnavailable:
    keeper = list_producer(conf.read_or_repository("console"), members)
    if isinstance(keeper, PageOwnerUnavailable):
        return ListFilingUnavailable("The List has no active producer.", filing)
    return ListFilingRoute(keeper=keeper.id, owner=filing.owner, producer=filing.producer,
                           unit=filing.unit, source=filing.source)


@dataclass(frozen=True)
class NoListFiling:
    """This request has no List row to write or skip."""


def list_filing_route(saved: SavedSubmission) -> ListFilingRoute | ListFilingUnavailable | NoListFiling:
    """Read the saved List filing route or the assignee that still needs a route."""
    folder = _request_folder(saved.request_id)
    try:
        routing = (folder / "list-filing.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return NoListFiling()
    fields = cast(object, json.loads(routing))
    if not isinstance(fields, dict):
        raise OSError(f"Invalid List route for request {saved.request_id}.")
    values = cast(dict[str, object], fields)
    if isinstance(values.get("skipped"), str):
        return NoListFiling()
    reason = values.get("unavailable")
    if isinstance(reason, str):
        if "owner" in values:
            owner, producer = values["owner"], values.get("producer")
            unit, source = values.get("unit"), values.get("source")
            if not (isinstance(owner, str) and isinstance(producer, str)
                    and isinstance(unit, str) and isinstance(source, str)):
                raise OSError(f"Invalid List route for request {saved.request_id}.")
            return ListFilingUnavailable(reason, PendingProducerFiling(
                staff.StaffId(owner), staff.StaffId(producer), unit, source))
        assignee = (list_command_assignee(values["assignee"]) if "assignee" in values
                    else ListWindowAssignee.RECIPIENT)
        return ListFilingUnavailable(reason, assignee)
    keeper, owner, producer = values.get("keeper"), values.get("owner"), values.get("producer")
    unit, source = values.get("unit"), values.get("source")
    if not (isinstance(keeper, str) and isinstance(owner, str) and isinstance(producer, str)
            and isinstance(unit, str) and isinstance(source, str)):
        raise OSError(f"Invalid List route for request {saved.request_id}.")
    return ListFilingRoute(keeper=staff.StaffId(keeper), owner=staff.StaffId(owner), producer=staff.StaffId(producer),
                           unit=unit, source=source)


def retry_list_filing_route(saved: SavedSubmission, unavailable: ListFilingUnavailable,
                            members: staff.StaffSnapshot) -> ListFilingRoute:
    """Resolve the assignee again and save the route before reserving a List number."""
    retry = unavailable.retry
    if isinstance(retry, PendingProducerFiling):
        route = _plan_producer_filing(retry, members)
    else:
        recipient = members.by_id(saved.owner)
        if recipient is None:
            raise OSError(f"The recipient for {saved.id} is unavailable.")
        route = _plan_list_filing(retry, recipient, members)
    if isinstance(route, ListFilingUnavailable):
        raise OSError(f"Cannot file {saved.id} on the List: {route.reason}")
    _write_request_file(_request_folder(saved.request_id) / "list-filing.json", route)
    return route


def list_filing_reservation(saved: SavedSubmission) -> Path:
    """Return the number reservation for the saved request."""
    return _request_folder(saved.request_id) / "list-number"


def skip_list_filing(saved: SavedSubmission, reason: str) -> None:
    """Record the skipped row before the delivery claim."""
    list_filing_reservation(saved).unlink(missing_ok=True)
    _write_request_file(_request_folder(saved.request_id) / "list-filing.json", {"skipped": reason})
    print(f"Review List filing {saved.id}: {reason}", file=sys.stderr)


@dataclass(frozen=True)
class PendingInitialDelivery:
    """The request and evidence origin saved before its native write."""

    address: SubmissionAddress
    evidence_origin: str


def pending_initial_deliveries(root: Path) -> Iterator[PendingInitialDelivery]:
    """Read the first messages that have no durable claim yet."""
    for path in _attachment_root(root).glob("*/first-message.json"):
        try:
            fields = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            kind = read_kind(fields["kind"])
            request = read_request_id(fields["request_id"])
            address = _request_address(kind, new_item_dialog.request_recipient(kind, request), request)
            origin = fields["evidence_origin"]
            if not isinstance(origin, str) or path.parent != _request_folder(request):
                raise ValueError("Invalid first message.")
            yield PendingInitialDelivery(address, origin)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Review first message {path}: {error}", file=sys.stderr)


def clear_initial_delivery(address: SubmissionAddress) -> None:
    """Remove the pending marker after the delivery claim saves."""
    folder = _request_folder(address.request_id)
    try:
        (folder / "first-message.json").unlink()
        atomic_file.sync_folder(folder)
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"Review first message {address.request_id}: {error}", file=sys.stderr)


def pending_submission_moves(root: Path) -> Iterator[Path]:
    """Find Review transfers that still need cleanup."""
    return _attachment_root(root).glob("*/move.json")


def _recipient(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow:
    row = members.by_id(identity)
    if row is None or row.kind not in ("showrunner", "producer"):
        raise SubmissionError("Select a showrunner or producer.")
    return row


class _UnreadableOwnerDocument(OSError):
    """Send could not read a named local owner document."""

    def __init__(self, owner: staff.StaffRow, document: Path, error: OSError | UnicodeError,
                 members: staff.StaffSnapshot) -> None:
        state = "is missing" if isinstance(error, FileNotFoundError) else "cannot be read"
        parent = members.parent_of(owner)
        fixer = parent.name if parent is not None else "You"
        super().__init__(f"The {owner.name} production document {document} {state}. {fixer} must fix the {owner.name} staff row.")


def owner_is_active(row: staff.StaffRow) -> bool:
    """Check both the staff row and the production document."""
    try:
        return _document_is_active(row)
    except FileNotFoundError:
        return False


def _document_is_active(row: staff.StaffRow) -> bool:
    if isinstance(row.doc, staff.WrappedDoc) or isinstance(row.worktree, staff.RemovedWorktree):
        return False
    if isinstance(row.doc, staff.LiveDoc):
        if row.kind == "producer" and row.doc.path.is_dir():
            return True
        lines = row.doc.path.read_text(encoding="utf-8").splitlines()
        status = next((line for line in lines if line.startswith("> **Status:")), "")
        if row.kind == "showrunner":
            return status.startswith("> **Status: PRODUCTION — running.**")
        return not status.startswith(WRAPPED)
    return row.kind == "producer"


def _has_remote_document(row: staff.StaffRow) -> bool:
    return isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.machine != local_machine()


def sent_question_scope(owner: staff.StaffId, members: staff.StaffSnapshot) -> update_registration.QuestionScope:
    """Use the staff row for a sent Question whose production document is remote."""
    row = members.by_id(owner)
    if row is not None and _has_remote_document(row):
        return update_registration.ProducerQuestions(row)
    return update_registration.question_scope(owner, members)


def _submission_owner_is_active(row: staff.StaffRow, members: staff.StaffSnapshot) -> bool:
    if row.kind == "producer" and (row.id in members.pending_retire or isinstance(row.exited, staff.Exited)):
        return False
    if _has_remote_document(row):
        return (isinstance(row.doc, staff.LiveDoc) and not isinstance(row.worktree, staff.RemovedWorktree)
                and not isinstance(row.exited, staff.Exited) and row.id not in members.pending_retire)
    try:
        return _document_is_active(row)
    except (OSError, UnicodeError) as error:
        if not isinstance(row.doc, staff.LiveDoc):
            raise
        raise _UnreadableOwnerDocument(row, row.doc.path, error, members) from error


@dataclass(frozen=True)
class PageOwnerUnavailable:
    """This page owner is unavailable."""


@dataclass(frozen=True)
class PageOwnerRoute:
    """The recipient for new items and one sentence that says why the route chose it."""

    row: staff.StaffRow
    reason: str


@dataclass(frozen=True)
class PageOwners:
    shown: staff.StaffRow | PageOwnerUnavailable
    route: PageOwnerRoute | PageOwnerUnavailable


def _page_owner_is_active(row: staff.StaffRow, members: staff.StaffSnapshot) -> bool:
    return (row.id not in members.pending_retire and not isinstance(row.exited, staff.Exited)
            and not isinstance(row.doc, staff.WrappedDoc) and not isinstance(row.worktree, staff.RemovedWorktree)
            and (row.kind != "showrunner" or isinstance(row.doc, staff.LiveDoc)))


def page_owners(page: str, settings: conf.ConfigRead, members: staff.StaffSnapshot) -> PageOwners:
    """Resolve the owner to show and the recipient for new items."""
    key = f"page.{page}.owner"
    label_page = "studio" if page == "list" else page
    label_key = f"page.{label_page}.label"
    values = settings.values(key, label_key, "owner.default")
    designated = staff.StaffId(values.get(key, ""))
    default = staff.StaffId(values.get("owner.default", ""))
    # A page outside the console navigation, such as rebuilds, has no label setting.
    label = values.get(label_key) or label_page.replace("_", " ").title()
    route = _page_owner_route(label, designated, default, members)
    shown = members.rows_by_id.get(designated)
    if shown is not None and shown.kind in ("unit", "showrunner", "producer") and _page_owner_is_active(shown, members):
        return PageOwners(shown, route)
    return PageOwners(route.row if isinstance(route, PageOwnerRoute) else route, route)


def page_owner(page: str, settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | PageOwnerUnavailable:
    """Resolve the recipient for new items from a console page."""
    route = page_owners(page, settings, members).route
    return route.row if isinstance(route, PageOwnerRoute) else route


def _page_owner_route(label: str, designated: staff.StaffId, default: staff.StaffId,
                      members: staff.StaffSnapshot) -> PageOwnerRoute | PageOwnerUnavailable:
    owner = members.by_id(designated)
    missed: list[staff.StaffRow] = []
    for identity in (designated, default):
        for candidate in investigation.owner_chain(identity, members):
            allow_producer = candidate == designated
            row = _available_page_owner(candidate, members, allow_producer=allow_producer)
            if isinstance(row, staff.StaffRow):
                if identity == designated:
                    cause = _owner_chain_cause(label, owner, row, missed, members)
                else:
                    cause = _default_owner_cause(label, designated, owner, missed, row, members.by_id(default))
                return PageOwnerRoute(row, f"(why {row.name}? because {cause}!)")
            skipped = members.by_id(candidate)
            if (identity == designated and skipped is not None
                    and (skipped.kind == "showrunner" or skipped.kind == "producer" and allow_producer)):
                missed.append(skipped)
    return PageOwnerUnavailable()


def _owner_chain_cause(label: str, owner: staff.StaffRow | None, route: staff.StaffRow,
                       missed: list[staff.StaffRow], members: staff.StaffSnapshot) -> str:
    if missed:
        return f"{missed[0].name} is not available and {route.name} is the nearest available showrunner above it"
    if owner is None or owner == route:
        return f"{route.name} owns the {label} page"
    relation = "its showrunner" if members.parent_of(owner) == route else "the nearest showrunner above it"
    return f"{owner.name} owns the {label} page and {route.name} is {relation}"


def _default_owner_cause(label: str, designated: staff.StaffId, owner: staff.StaffRow | None,
                         missed: list[staff.StaffRow], route: staff.StaffRow,
                         default: staff.StaffRow | None) -> str:
    if not designated:
        cause = f"the {label} page has no owner"
    elif owner is None:
        cause = f"the {label} page owner is not on the staff list"
    elif missed:
        cause = f"{missed[0].name} is not available"
    else:
        cause = f"{owner.name} has no showrunner"
    if default is None or default == route:
        return f"{cause} and {route.name} is the default owner"
    return (f"{cause}, the default owner {default.name} is not available,"
            + f" and {route.name} is the nearest available showrunner above it")


def _available_page_owner(identity: staff.StaffId, members: staff.StaffSnapshot, *,
                          allow_producer: bool) -> staff.StaffRow | PageOwnerUnavailable:
    row = members.rows_by_id.get(identity)
    if row is not None and _page_owner_is_active(row, members) and (
            row.kind == "showrunner" or row.kind == "producer" and allow_producer):
        return row
    return PageOwnerUnavailable()


def list_producer(settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | PageOwnerUnavailable:
    """Find the active producer that owns the List."""
    return record_store.list_keeper(settings, members) or PageOwnerUnavailable()


def _send_recipient(address: SubmissionAddress, page: str, members: staff.StaffSnapshot,
                    choice: new_item_dialog.PageRecipient) -> staff.StaffRow:
    identity = address.recipient_id
    if choice == new_item_dialog.PageRecipient.LIST_OWNER:
        configured = record_store.configured_list_producer(conf.read_or_repository("console"), members)
        if not isinstance(configured, staff.StaffRow) or configured.id != identity:
            raise SubmissionConflict("The List owner is unavailable. Open New item again.")
    elif choice in (new_item_dialog.PageRecipient.SELECTED, new_item_dialog.PageRecipient.PRODUCER):
        row = selected_recipient(identity, members)
        if choice == new_item_dialog.PageRecipient.PRODUCER and row.kind != "producer":
            raise SubmissionError("Pick a producer.")
        return row
    elif page and page != "list":
        configured = page_owner(page, conf.read_or_repository("console"), members)
        if isinstance(configured, PageOwnerUnavailable):
            raise SubmissionConflict("The page owner is unavailable. Open New item again.")
        identity = configured.id
    return _active_recipient(identity, members)


def selected_recipient(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow:
    """Require the selected recipient to be available to receive a new item."""
    row = _recipient(identity, members)
    require_available_recipient(row, members)
    return row


def require_available_recipient(row: staff.StaffRow, members: staff.StaffSnapshot) -> None:
    """Check availability for a selected recipient or a CLI unit owner."""
    if (row.id not in members.rows_by_id or not _page_owner_is_active(row, members)
            or (not (row.kind == "unit" and isinstance(row.doc, staff.DocUnfilled))
                and not _submission_owner_is_active(row, members))):
        raise SubmissionConflict(f"{row.name} is unavailable. Choose another recipient.")


def _active_recipient(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow:
    row = _recipient(identity, members)
    for candidate in investigation.owner_chain(identity, members):
        member = members.rows_by_id.get(candidate)
        if member is None or member.kind not in ("showrunner", "producer"):
            continue
        if _submission_owner_is_active(member, members):
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


class AssigneeOption(TypedDict):
    id: staff.StaffId
    name: str


class NewItemRoutingOptions(TypedDict):
    recipients: list[RecipientOption]
    assignees: list[AssigneeOption]
    page_owner: staff.StaffId
    page_owner_reason: str
    page_assignee: staff.StaffId


def proposal_folder(recipient: staff.StaffRow, members: staff.StaffSnapshot) -> Path | NoNativeFolder:
    from scripts.production import proposals_folder

    if isinstance(recipient.doc, (staff.LiveDoc, staff.WrappedDoc)) and not _has_remote_document(recipient):
        try:
            folder = None if recipient.doc.path.is_dir() else proposals_folder.issues_folder(recipient.doc.path)
        except (OSError, UnicodeError) as error:
            raise _UnreadableOwnerDocument(recipient, recipient.doc.path, error, members) from error
        if folder is not None:
            return folder
    if recipient.kind == "producer":
        return record_store.state_root() / recipient.id / "proposals"
    return NoNativeFolder(SubmissionKind.PROPOSAL)


def _is_recipient_choice(row: staff.StaffRow, members: staff.StaffSnapshot, *,
                         owner: staff.StaffRow | PageOwnerUnavailable,
                         producer: staff.StaffRow | PageOwnerUnavailable) -> bool:
    return (_page_owner_is_active(row, members)
            and (row == owner or row == producer or _submission_owner_is_active(row, members)))


_NO_PAGE_OWNER = PageOwnerUnavailable()


def _recipient_choices(members: staff.StaffSnapshot, *,
                       owner: staff.StaffRow | PageOwnerUnavailable = _NO_PAGE_OWNER
                       ) -> Iterable[staff.StaffRow]:
    producer = list_producer(conf.read_or_repository("console"), members)
    for row in (*staff.active(members, "showrunner"), *staff.active(members, "producer")):
        try:
            if _is_recipient_choice(row, members, owner=owner, producer=producer):
                yield row
        except OSError:
            continue


def has_recipient_choice(members: staff.StaffSnapshot) -> bool:
    """Check whether the dialog has a recipient without reading Proposal folders."""
    return any(_recipient_choices(members))


def recipient_options(members: staff.StaffSnapshot, *,
                      owner: staff.StaffRow | PageOwnerUnavailable = _NO_PAGE_OWNER
                      ) -> list[RecipientOption]:
    """List active recipients and indicate who can receive proposals."""
    return [_recipient_option(row, members) for row in _recipient_choices(members, owner=owner)]


def _recipient_option(row: staff.StaffRow, members: staff.StaffSnapshot) -> RecipientOption:
    try:
        can_receive_proposals = isinstance(proposal_folder(row, members), Path)
    except OSError:
        can_receive_proposals = False
    return RecipientOption(id=row.id, name=row.name, proposal=can_receive_proposals)


def new_item_routing(page: str, settings: conf.ConfigRead, members: staff.StaffSnapshot) -> NewItemRoutingOptions:
    """Resolve producer choices and the preferred assignee from the current staff."""
    producers: dict[staff.StaffId, staff.StaffRow] = {}
    for row in sorted(staff.active(members, "producer"), key=lambda row: row.name.casefold()):
        try:
            if _page_owner_is_active(row, members) and _submission_owner_is_active(row, members):
                producers[row.id] = row
        except OSError:
            continue
    assignees = {row.id: row for row in members.rows if row.kind in ("showrunner", "unit")
                 and _page_owner_is_active(row, members)}
    # The List owner keeps the List. The List does not choose an assignee.
    key = f"page.{page}.owner"
    designated = staff.StaffId(settings.values(key).get(key, "")) if page != "list" else staff.StaffId("")
    producer = producers.get(designated)
    owner = assignees.get(designated) or producer
    if producer is not None:
        assignees[producer.id] = producer
    elif owner is not None:
        mapped = _list_filing_producer(owner, members)
        if isinstance(mapped, staff.StaffRow):
            producer = producers.get(mapped.id)
    reason = ""
    if owner is not None and producer is not None:
        reason = (f"{producer.name} owns this page." if producer.id == owner.id
                  else f"{producer.name} is the producer of {owner.name}, the owner of this page.")
    parent = staff.ReportsTo(owner.id) if owner is not None else None
    ordered = sorted(assignees.values(), key=lambda row: (row != owner, row.parent != parent, row.name.casefold()))
    return NewItemRoutingOptions(
        recipients=[_recipient_option(row, members) for row in producers.values()],
        assignees=[AssigneeOption(id=row.id, name=row.name) for row in ordered],
        page_owner=producer.id if producer is not None else staff.StaffId(""), page_owner_reason=reason,
        page_assignee=owner.id if owner is not None else staff.StaffId(""))


def _file_question_for_remote_owner(recipient: staff.StaffRow, title: str, members: staff.StaffSnapshot,
                                    outgoing: update_registration.QuestionFromReview
                                    ) -> update_registration.QuestionRecord:
    owner = recipient.id
    folder = update_registration.QuestionFolder(record_store.default_question_folder(owner), owner)
    limit = conf.read_or_repository("production").whole("question_title_max_chars")
    title = title[:limit].rstrip()
    sent = datetime.now(user_zone()).strftime("%Y-%m-%dT%H:%M")
    parts = update_registration.QuestionParts(ask=title, context=outgoing.words, items=[], recommend="", choices=[])
    item = update_registration.OutstandingItem(
        since=sent, text=title, question=outgoing.words, asked_by=owner, parts=parts,
        followup=update_registration.PendingFollowup(text=outgoing.words, sent=sent),
        waiting_why=outgoing.waiting_why, review_submission=new_item_dialog.stored_submission(outgoing.details))
    path = update_registration.review_question_path(folder, outgoing.details.request_id)
    record = update_registration.QuestionRecord(path, owner, owner, item, "open")
    with staff.supplied_snapshot(members), update_registration.outstanding_lock(
            update_registration.outstanding_path_for_owner(owner)):
        return update_registration._file_question(record, outgoing.before_write)  # pyright: ignore[reportPrivateUsage]


def _file(kind: SubmissionKind, recipient: staff.StaffRow, title: str, words: str, details: ReviewSubmission,
          members: staff.StaffSnapshot, folder: Path | NoNativeFolder,
          before_write: Callable[[str, Path], None]) -> NativeSubmittedRecord:
    why = f"{recipient.name} answers this"
    match kind:
        case SubmissionKind.QUESTION:
            outgoing = update_registration.QuestionFromReview(words, why, details, before_write)
            if recipient.kind == "showrunner" and _has_remote_document(recipient):
                return _file_question_for_remote_owner(recipient, title, members, outgoing)
            identities = update_registration.QuestionStaff.for_record(members)
            scope = update_registration.question_scope(recipient, members)
            parts = update_registration.QuestionParts(ask=title, context="", items=[], recommend="", choices=[])
            return update_registration.ask(
                scope, title, parts, "", update_registration.Ranking(), identities=identities,
                outgoing=outgoing)
        case SubmissionKind.PROPOSAL:
            assert isinstance(folder, Path)
            return proposal_issue.file_review_proposal(folder, recipient.id, title, words, why, details,
                                                       before_write=before_write)
        case SubmissionKind.BUG | SubmissionKind.INVESTIGATION:
            return investigation.file(recipient.id, title, words, waiting_why=why,
                                      kind=investigation.InvestigationKind(kind.value), submission=details,
                                      before_write=before_write)


def _prepare_request_folder(request_id: str) -> tuple[Path, bool]:
    folder = _request_folder(request_id)
    try:
        folder.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        return folder, True
    return folder, False


@contextmanager
def _locked_filing_draft(request_id: str) -> Generator[Path]:
    """Hold the same lock for either destination of a browser draft."""
    folder, _ = _prepare_request_folder(request_id)
    index = _draft_index(folder)
    index.mkdir(mode=0o700, parents=True, exist_ok=True)
    with exclusive(index / ".lock"), exclusive(folder / ".lock"):
        yield folder


def _sync_draft_parents(folder: Path) -> None:
    atomic_file.sync_folder(folder.parent.parent)
    atomic_file.sync_folder(folder.parent)


@dataclass(frozen=True)
class UnclaimedDraft:
    """The draft has no filing claim."""


@dataclass(frozen=True)
class ClaimedDraft:
    """A write has a claim but no confirmed saved destination."""

    destination: FilingDestination


@dataclass(frozen=True)
class NativeDraftAwaitingReceipt:
    """The native record exists and the draft index receipt is pending."""

    destination: NativeSubmissionDestination


@dataclass(frozen=True)
class FiledDraft:
    """The draft names a saved item."""

    destination: FilingDestination


type DraftFiling = UnclaimedDraft | ClaimedDraft | NativeDraftAwaitingReceipt | FiledDraft | PendingFiling

def _draft_index(folder: Path) -> Path:
    return folder.parent / ".draft-index" / folder.name


def _write_draft_index(folder: Path, filing: ClaimedDraft | NativeDraftAwaitingReceipt | FiledDraft) -> None:
    index = _draft_index(folder)
    # Sync each parent before a record can refer to the indexed claim.
    _sync_draft_parents(index)
    _write_location(index / ("receipt.json" if isinstance(filing, FiledDraft) else "pending.json"), filing.destination)


@dataclass(frozen=True)
class NoListRecordForClaim:
    """The reserved List number has no record."""


type LoadListDraft = Callable[[staff.StaffId, str], ListDraftOrigin]


class ListRecordCheck(Enum):
    OMIT = "read the draft index only"


def _indexed_list_record(draft_id: str, planned: ListItemDestination,
                         load_list_draft: LoadListDraft) -> FiledDraft | NoListRecordForClaim:
    path = list_draft.list_records(planned.claim.keeper).path(planned.id)
    try:
        _ = path.stat()
    except FileNotFoundError:
        return NoListRecordForClaim()
    try:
        draft = load_list_draft(planned.claim.keeper, planned.id)
    except ValueError as error:
        raise OSError(f"Cannot read the saved List draft at {path}.") from error
    if not isinstance(draft, NewItemDraft) or draft.id.value != draft_id or draft.claim != planned.claim:
        raise OSError(f"{planned.id}: the saved List draft differs from the filing claim.")
    atomic_file.sync_folder(path.parent)
    return FiledDraft(ListItemDestination(planned.id, draft.claim))


def _draft_filing(folder: Path, *, repair_index: bool = True,
                  load_list_draft: LoadListDraft | ListRecordCheck = ListRecordCheck.OMIT) -> DraftFiling:
    """Read this draft's index and repair missing entries from its files."""
    for location in (_draft_index(folder), folder):
        receipt = _read_location_hint(location, receipt_only=True)
        if isinstance(receipt, (ListItemDestination, NativeSubmissionDestination)):
            filing = FiledDraft(receipt)
        else:
            pending = _read_location_hint(location)
            if isinstance(pending, NoRequestFile):
                continue
            if isinstance(pending, UnreadableRequestFile):
                raise pending.error
            if isinstance(pending, PendingFiling):
                return pending
            if isinstance(pending, ListItemDestination):
                filing = ClaimedDraft(pending)
                if load_list_draft is not ListRecordCheck.OMIT:
                    record = _indexed_list_record(folder.name, pending, load_list_draft)
                    if isinstance(record, FiledDraft):
                        filing = record
            elif _is_earlier_native_draft(folder, pending):
                return NativeDraftAwaitingReceipt(pending)
            else:
                filing = ClaimedDraft(pending)
        if repair_index and (location == folder or isinstance(filing, FiledDraft) and filing.destination != receipt):
            _write_draft_index(folder, filing)
        elif (repair_index and isinstance(receipt, NativeSubmissionDestination)
              and _is_earlier_native_draft(folder, receipt)):
            atomic_file.sync_folder(location)
        return filing
    return UnclaimedDraft()


@contextmanager
def _locked_legacy_list_target(folder: Path, members: staff.StaffSnapshot | None
                               ) -> Generator[LocatedSubmission | NoNativeSubmission]:
    """Read the native record named by a base-format draft marker."""
    try:
        key = (folder / "list-receipt-key").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        hint = _read_request_file(folder / "list-capture-target.json", folder.name)
        if isinstance(hint, NativeSubmissionDestination):
            with _locked_request(hint.address, members, recovered=hint) as lookup:
                yield _saved_draft(lookup) or NoNativeSubmission()
        else:
            yield NoNativeSubmission()
    else:
        with scoped_submission(read_submission_key(key), members=members) as item:
            yield item


def _is_earlier_native_draft(folder: Path, destination: NativeSubmissionDestination) -> bool:
    return not destination.request_id.startswith(folder.name + "-")


def _claim_recovered_native_draft(folder: Path, saved: NativeSubmissionDestination) -> None:
    _write_location(folder / "pending.json", saved)
    _sync_draft_parents(folder)
    _write_draft_index(folder, NativeDraftAwaitingReceipt(saved))


def _publish_recovered_native_draft(folder: Path, saved: NativeSubmissionDestination) -> FiledDraft:
    atomic_file.sync_folder(saved.path.parent)
    filing = FiledDraft(saved)
    _write_draft_index(folder, filing)
    return filing


class ListDraftWriter(Protocol):
    def __call__(self, claim_number: Callable[[int], None]) -> str: ...


def file_list_draft(draft_id: new_item_dialog.ScheduledDraftId, payload_hash: str, *,
                    prepare: Callable[[], AbstractContextManager[tuple[ListDraftClaim, ListDraftWriter]]],
                    deliver: Callable[[ListAssignmentNotice, str, str], bool],
                    load_list_draft: LoadListDraft) -> ListItemDestination:
    """Recover the first Send or file a List item under its draft lock."""
    request_id = draft_id.value
    with _locked_filing_draft(request_id) as folder:
        _sync_draft_parents(folder)
        filing = _draft_filing(folder, load_list_draft=load_list_draft)
        candidate = filing.destination if isinstance(filing, (ClaimedDraft, FiledDraft)) else filing
        if not isinstance(candidate, (ListItemDestination, UnclaimedDraft)):
            raise DraftPending()
        if isinstance(filing, FiledDraft):
            assert isinstance(candidate, ListItemDestination)
            saved = candidate
            if saved.payload_hash != payload_hash:
                raise DraftAlreadyFiled(saved.id)
            local = _read_location_hint(folder, receipt_only=True)
            if isinstance(local, ListItemDestination) and (local.id, local.payload_hash) == (saved.id, saved.payload_hash):
                saved = local
        else:
            if isinstance(candidate, ListItemDestination) and candidate.payload_hash != payload_hash:
                raise DraftPending()
            pending = folder / "pending.json"
            try:
                with prepare() as (claim, save):
                    def claim_number(number: int) -> None:
                        planned = ListItemDestination(f"L{number}", claim)
                        _write_draft_index(folder, ClaimedDraft(planned))
                        _write_location(pending, planned)

                    saved = ListItemDestination(save(claim_number), claim)
            except SubmissionError:
                pending.unlink(missing_ok=True)
                (_draft_index(folder) / "pending.json").unlink(missing_ok=True)
                atomic_file.sync_folder(folder)
                atomic_file.sync_folder(_draft_index(folder))
                raise
            _write_draft_index(folder, FiledDraft(saved))
        receipt = folder / "receipt.json"
        if not receipt.is_file():
            _write_location(folder / "pending.json", saved)
            _publish_receipt(folder, saved)
        else:
            atomic_file.sync_folder(folder)
        failed = False
        for notice in saved.claim.notices:
            if notice.recipient in saved.confirmed:
                continue
            if deliver(notice, saved.id, f"new-item:{request_id}:{notice.role}"):
                saved = replace(saved, confirmed=(*saved.confirmed, notice.recipient))
                _write_location(receipt, saved)
            else:
                failed = True
        if failed:
            raise OSError(f"{saved.id} is saved. An assignment notice needs a retry.")
        return saved


@contextmanager
def _locked_browser_request(address: SubmissionAddress, members: staff.StaffSnapshot | None, *,
                            page: str, page_recipient: new_item_dialog.PageRecipient,
                            refusal: SubmissionError | None) -> Generator[SubmissionLookup]:
    with _locked_filing_draft(address.request_id) as folder:
        with _locked_filed_draft(folder, members) as lookup:
            if isinstance(lookup, SubmissionLookup):
                yield lookup
                return
        if refusal is not None:
            raise refusal
        with _locked_browser_recipient(address, page, page_recipient) as lookup:
            yield lookup


@contextmanager
def _locked_browser_recipient(address: SubmissionAddress, page: str, page_recipient: new_item_dialog.PageRecipient
                              ) -> Generator[SubmissionLookup]:
    owners = {address.recipient_id}
    while True:
        with investigation.recipient_locks(*owners):
            current = staff.snapshot()
            recipient = _send_recipient(address, page, current, page_recipient)
            if recipient.id not in owners:
                owners.add(recipient.id)
                continue
            target = SubmissionAddress(address.kind, recipient.id, f"{address.request_id}-{address.kind}-{recipient.id}")
            yield SubmissionLookup(_request_hint(target), NoNativeSubmission(), current, target, recipient)
            return


@contextmanager
def _locked_filed_draft(folder: Path, members: staff.StaffSnapshot | None, *, on_list: bool = False
                        ) -> Generator[SubmissionLookup | NoNativeSubmission]:
    token = _browser_draft_token.get()
    recovery = isinstance(token, BrowserDraftRecoveryToken)
    filing = _draft_filing(folder, repair_index=not recovery)
    if isinstance(filing, UnclaimedDraft) and not recovery:
        with _locked_legacy_list_target(folder, members) as item:
            if isinstance(item, LocatedSubmission):
                _claim_recovered_native_draft(folder, item.destination)
                filing = _publish_recovered_native_draft(folder, item.destination)
    if not isinstance(filing, (ClaimedDraft, NativeDraftAwaitingReceipt, FiledDraft)):
        yield NoNativeSubmission()
        return
    hint = filing.destination
    if isinstance(hint, NativeSubmissionDestination):
        earlier = _is_earlier_native_draft(folder, hint)
        if earlier:
            if recovery:
                yield NoNativeSubmission()
                return
            if not on_list:
                raise DraftAlreadyFiled(hint.id)
        # A legacy receipt has no digest to check. Recovery files nothing new.
        if recovery and hint.browser_token_digest and not hmac.compare_digest(hint.browser_token_digest, token.digest):
            yield NoNativeSubmission()
            return
        if isinstance(filing, NativeDraftAwaitingReceipt):
            index = _draft_index(folder)
            if isinstance(_read_request_file(index / "pending.json", index.name), NoRequestFile):
                _write_draft_index(folder, filing)
        local = _read_location_hint(folder)
        relocated = isinstance(local, NativeSubmissionDestination) and local.owner != hint.owner
        must_exist = isinstance(filing, (NativeDraftAwaitingReceipt, FiledDraft))
        with _locked_request(hint.address, members, recovered=hint,
                             search_owner_stores=must_exist or relocated) as lookup:
            saved = _saved_draft(lookup)
            if saved is not None and (not recovery or saved.destination.browser_token_digest == hint.browser_token_digest):
                yield lookup
                return
        if must_exist:
            raise OSError(f"Cannot find the native record for request {hint.request_id}.")
    yield NoNativeSubmission()


def _recover_unverified_draft(request_id: str, members: staff.StaffSnapshot | None,
                              error: SubmissionError, *,
                              capture_target: ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget
                              ) -> SavedSubmission:
    if is_draft_id(request_id):
        folder = _request_folder(request_id)
        if folder.is_dir() or _draft_index(folder).is_dir():
            with _locked_filing_draft(request_id), _locked_filed_draft(folder, members) as lookup:
                if isinstance(lookup, SubmissionLookup) and isinstance(lookup.native, LocatedSubmission):
                    item = lookup.native
                    _check_capture_target(capture_target, item.destination)
                    return _complete_receipt(folder, item.destination, lookup.hint, item.record, edited=True)
    raise error


def _discard_unfiled_send(folder: Path) -> None:
    images = folder / "images"
    if images.is_dir():
        shutil.rmtree(images)
    (folder / "pending.json").unlink(missing_ok=True)
    (folder / "first-message.json").unlink(missing_ok=True)
    index = _draft_index(folder)
    (index / "pending.json").unlink(missing_ok=True)
    atomic_file.sync_folder(index)
    (folder / "list-filing.json").unlink(missing_ok=True)
    (folder / "list-preview.json").unlink(missing_ok=True)
    atomic_file.sync_folder(folder)


def _check_capture_target(target: ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget,
                          saved: NativeSubmissionDestination) -> None:
    if isinstance(target, ScopedSubmissionKey) and target != ScopedSubmissionKey(
            saved.kind, saved.recipient_id, saved.id, saved.request_id):
        raise SubmissionConflict("This screenshot belongs to another item.")


def _browser_payload_hash(request: NewItemRequest, words: str, evidence: Evidence,
                          uploads: tuple[_ImageUpload, ...], screenshot: new_item_dialog.SubmissionScreenshot,
                          delivery: InitialDelivery | NoInitialDelivery) -> str:
    digest = "" if isinstance(screenshot, new_item_dialog.NoPageScreenshot) else screenshot.digest
    fields: list[object] = [request.kind, request.recipient_id, words, new_item_dialog.evidence_fields(evidence),
                            _image_fingerprints(uploads), request.page, request.page_recipient,
                            asdict(request.page_state) if request.page_state else None, digest]
    if isinstance(delivery, InitialDelivery) and isinstance(delivery.list_assignee, ListCommandAssignee):
        fields.append(delivery.list_assignee.id)
    if request.page_recipient == new_item_dialog.PageRecipient.PRODUCER:
        fields.append(new_item_dialog.assignment_id(request.assignment))
    return _fingerprint(fields, notice=request.notice)


@dataclass(frozen=True)
class CommittedListPreview:
    """Identify the screenshot that the first List Send attached."""

    key: str
    digest: str


class NoCommittedListPreview(Enum):
    ABSENT = "no committed preview"


def _committed_list_preview(folder: Path) -> CommittedListPreview | NoCommittedListPreview:
    try:
        fields = cast(object, json.loads((folder / "list-preview.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoCommittedListPreview.ABSENT
    if isinstance(fields, dict):
        values = cast(dict[str, object], fields)
        key = new_item_dialog.read_capture_key(values.get("key"))
        digest = values.get("digest")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            return CommittedListPreview(key, digest)
    raise OSError("Cannot read the committed List screenshot.")


@dataclass(frozen=True)
class ListDraftReplay:
    uploads: tuple[_ImageUpload, ...] = ()
    payload_matches: bool = False
    capture_matches: bool = False


def _list_draft_replay(folder: Path, send: ListDraftSend, intent: new_item_dialog.SubmissionCaptureIntent,
                       item: LocatedSubmission, delivery: InitialDelivery | NoInitialDelivery) -> ListDraftReplay:
    try:
        filing = send.prepare()
        request = filing.request
        if request.kind == SubmissionKind.PROPOSAL and request.page_recipient != new_item_dialog.PageRecipient.PRODUCER:
            request = replace(request, page_recipient=new_item_dialog.PageRecipient.SELECTED)
        if request.page != "list" or request.request_id != send.request_id or filing.refusal is not None:
            return ListDraftReplay()
        evidence = read_evidence(new_item_dialog.evidence_fields(request.evidence))
        words = read_words(request.words, "Your words", kind=request.kind)
        screenshot = filing.screenshot
        committed = _committed_list_preview(folder)
        requested = send.preview
        if isinstance(committed, CommittedListPreview):
            if not isinstance(requested, RequestedListPreview) or requested.key != committed.key:
                return ListDraftReplay()
            if isinstance(screenshot, new_item_dialog.NoPageScreenshot):
                screenshot = new_item_dialog.FiledPageScreenshot(committed.digest)
            elif screenshot.digest != committed.digest:
                return ListDraftReplay()
        elif isinstance(requested, RequestedListPreview) and isinstance(screenshot, new_item_dialog.NoPageScreenshot):
            return ListDraftReplay()
        uploads = _decode_images(request.images)
        saved_digest = item.destination.payload_hash
        if item.destination.request_id.startswith(send.request_id + "-"):
            digest = _browser_payload_hash(request, words, evidence, uploads, screenshot, delivery)
            matches = saved_digest == digest
            if (not matches and isinstance(request.notice, new_item_dialog.SubmissionNotice)
                    and request.notice.from_command and isinstance(item.details.notice, new_item_dialog.DefaultSubmissionNotice)):
                legacy = replace(request, notice=item.details.notice)
                matches = saved_digest == _browser_payload_hash(legacy, words, evidence, uploads, screenshot, delivery)
        else:
            earlier_id = "r2-" + send.request_id[3:]
            matches = saved_digest == _legacy_draft_fingerprint(earlier_id, request, words, uploads)
            if (not matches and request.notice == new_item_dialog.SubmissionNotice("feedback")
                    and isinstance(item.details.notice, new_item_dialog.DefaultSubmissionNotice)):
                legacy = replace(request, notice=item.details.notice)
                matches = saved_digest == _legacy_draft_fingerprint(earlier_id, legacy, words, uploads)
        if isinstance(screenshot, new_item_dialog.PageScreenshot):
            uploads = (*uploads, _ImageUpload(PAGE_SCREENSHOT, "png", screenshot.png))
        return ListDraftReplay(uploads, matches, item.details.capture_intent == intent)
    except SubmissionError:
        return ListDraftReplay()


def _submit_list_draft(send: ListDraftSend, members: staff.StaffSnapshot | None, *,
                       capture_intent: new_item_dialog.SubmissionCaptureIntent,
                       capture_target: ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget,
                       initial_delivery: InitialDelivery | NoInitialDelivery) -> SavedSubmission:
    if not is_draft_id(send.request_id):
        raise SubmissionError("Invalid List draft ID.")
    with _locked_filing_draft(send.request_id) as folder:
        with _locked_filed_draft(folder, members, on_list=True) as lookup:
            if isinstance(lookup, SubmissionLookup) and isinstance(lookup.native, LocatedSubmission):
                item = lookup.native
                if item.details.page != "list":
                    raise DraftAlreadyFiled(item.destination.id)
                saved = item.destination
                _check_capture_target(capture_target, saved)
                replay = _list_draft_replay(folder, send, capture_intent, item, initial_delivery)
                earlier = _is_earlier_native_draft(folder, saved)
                receipt_folder = _request_folder(saved.request_id) if earlier else folder
                completed = _complete_receipt(receipt_folder, saved, lookup.hint, item.record,
                                              edited=earlier or not (replay.payload_matches and replay.capture_matches),
                                              retry_uploads=replay.uploads, payload_matches=replay.payload_matches)
                if earlier and _read_location_hint(_draft_index(folder), receipt_only=True) != saved:
                    _ = _publish_recovered_native_draft(folder, saved)
                return completed
        if isinstance(capture_target, ScopedSubmissionKey):
            raise SubmissionConflict("This screenshot belongs to another item.")
        filing = send.prepare()
        request = filing.request
        if request.page != "list" or request.request_id != send.request_id:
            raise SubmissionError("Invalid List draft request.")
        return _submit(request, members=members, screenshot=filing.screenshot,
                       capture_requested=filing.capture_requested, refusal=filing.refusal,
                       capture_intent=capture_intent, capture_target=capture_target, initial_delivery=initial_delivery,
                       list_preview=send.preview)


def _recover_pre_upgrade_native_draft(draft_folder: Path, address: SubmissionAddress, words: str,
                                      uploads: tuple[_ImageUpload, ...], members: staff.StaffSnapshot | None, *,
                                      request: NewItemRequest,
                                      capture_target: ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget
                                      ) -> SavedSubmission | None:
    """Recover a native record from the base draft format."""
    legacy_id = "r2-" + address.request_id[3:]
    exact = _legacy_draft_fingerprint(legacy_id, request, words, uploads)
    fingerprints = [exact]
    default_notice = new_item_dialog.DefaultSubmissionNotice.FILED_KIND_FROM_REVIEW
    if request.notice == new_item_dialog.SubmissionNotice("feedback"):
        legacy = replace(request, notice=default_notice)
        fingerprints.append(_legacy_draft_fingerprint(legacy_id, legacy, words, uploads))
    address = replace(address, request_id=legacy_id)
    for fingerprint in fingerprints:
        folder = _request_folder(f"r4-{fingerprint}")
        if not folder.is_dir():
            continue
        with exclusive(folder / ".lock"):
            for candidate in _list_draft_candidates(folder, address, fingerprint):
                with _locked_request(candidate, members, list_recipient=address.recipient_id) as lookup:
                    item = _saved_draft(lookup, list_recipient=address.recipient_id)
                    if item is None or address.recipient_id not in (item.destination.recipient_id, item.destination.owner):
                        continue
                    if fingerprint != exact and item.details.notice != default_notice:
                        continue
                    saved = item.destination
                    _check_capture_target(capture_target, saved)
                    _claim_recovered_native_draft(draft_folder, saved)
                    completed = _complete_receipt(_request_folder(saved.request_id), saved, lookup.hint, item.record,
                                                  edited=True, retry_uploads=uploads,
                                                  payload_matches=saved.payload_hash == fingerprint)
                    _ = _publish_recovered_native_draft(draft_folder, saved)
                    return completed
    return None


@contextmanager
def _locked_list_draft(address: SubmissionAddress, members: staff.StaffSnapshot | None, *,
                       fingerprint: str, page_recipient: new_item_dialog.PageRecipient
                       ) -> Generator[SubmissionLookup]:
    """Recover the first deferred Send before the content lookup."""
    folder, _ = _prepare_request_folder(address.request_id)
    with exclusive(folder / ".lock"):
        hint = _read_request_file(folder / "list-capture-target.json", address.request_id)
        if isinstance(hint, NativeSubmissionDestination):
            with _locked_request(hint.address, members) as lookup:
                if _saved_draft(lookup) is not None:
                    yield lookup
                    return
        with _locked_draft_request(address, members, page="list", fingerprint=fingerprint,
                                   page_recipient=page_recipient) as lookup:
            yield lookup


def refuse_scheduled_draft_reuse(
        request_id: str, *, load_list_draft: LoadListDraft | ListRecordCheck = ListRecordCheck.OMIT) -> None:
    """Refuse a native Send with a Scheduled draft ID."""
    if not new_item_dialog.SCHEDULED_DRAFT_ID.fullmatch(request_id):
        return
    with _locked_filing_draft(request_id) as folder:
        filing = _draft_filing(folder, load_list_draft=load_list_draft)
        if isinstance(filing, FiledDraft) and isinstance(filing.destination, ListItemDestination):
            raise DraftAlreadyFiled(filing.destination.id)
        if isinstance(filing, ClaimedDraft) and isinstance(filing.destination, ListItemDestination):
            raise DraftPending()
        raise SubmissionConflict("This draft ID is for Scheduled. Start a new draft for this item type.")


def submit(
    request: NewItemRequest | new_item_dialog.UnverifiedDraftRetry | ListDraftSend, *, members: staff.StaffSnapshot | None = None,
    screenshot: new_item_dialog.SubmissionScreenshot = new_item_dialog.NO_PAGE_SCREENSHOT,
    capture_requested: bool = False, refusal: SubmissionError | None = None,
    capture_intent: new_item_dialog.SubmissionCaptureIntent = new_item_dialog.NoDeferredCapture.ABSENT,
    capture_target: ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget = new_item_dialog.NoBoundCaptureTarget.UNBOUND,
    initial_delivery: InitialDelivery | NoInitialDelivery = NO_INITIAL_DELIVERY,
) -> SavedSubmission:
    """Save the native item or recover the first Send for this draft."""
    refuse_scheduled_draft_reuse(request.request_id)
    if isinstance(capture_intent, new_item_dialog.DeferredCaptureIntent) and (
            capture_intent.submitted_draft_id != request.request_id
            or not new_item_dialog.capture_key_matches_draft(capture_intent.capture_key, request.request_id)
            or (DRAFT_ID.fullmatch(request.request_id)
                and (not isinstance(request, NewItemRequest) or request.page != "list"))):
        raise SubmissionError("The screenshot belongs to another draft.")
    if isinstance(request, new_item_dialog.UnverifiedDraftRetry):
        return _recover_unverified_draft(request.request_id, members, request.error, capture_target=capture_target)
    if isinstance(request, ListDraftSend):
        return _submit_list_draft(request, members, capture_intent=capture_intent, capture_target=capture_target,
                                  initial_delivery=initial_delivery)
    if request.page == "list":
        if BROWSER_DRAFT_ID.fullmatch(request.request_id):
            filing = ListDraftFiling(request, screenshot, capture_requested, refusal)
            return _submit_list_draft(ListDraftSend(request.request_id, lambda: filing), members,
                                      capture_intent=capture_intent, capture_target=capture_target, initial_delivery=initial_delivery)
        return _submit(request, members=members, screenshot=screenshot,
                       capture_requested=capture_requested, refusal=refusal,
                       capture_intent=capture_intent, capture_target=capture_target, initial_delivery=initial_delivery)
    try:
        return _submit(request, members=members, screenshot=screenshot,
                       capture_requested=capture_requested, refusal=refusal,
                       capture_intent=capture_intent, capture_target=capture_target, initial_delivery=initial_delivery)
    except SubmissionError as error:
        return _recover_unverified_draft(request.request_id, members, error, capture_target=capture_target)


def _submit(request: NewItemRequest, *, members: staff.StaffSnapshot | None,
            screenshot: new_item_dialog.SubmissionScreenshot, capture_requested: bool,
            refusal: SubmissionError | None, capture_intent: new_item_dialog.SubmissionCaptureIntent,
            capture_target: ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget,
            initial_delivery: InitialDelivery | NoInitialDelivery,
            list_preview: ListPreviewRequest = NoRequestedListPreview.ABSENT) -> SavedSubmission:
    check_recipient_id(request.recipient_id)
    request_id = read_request_id(request.request_id)
    submitted_draft_id = request_id
    kind = SubmissionKind.INVESTIGATION if request.kind == "enhancement" else read_kind(request.kind)
    notice = request.notice
    if isinstance(notice, new_item_dialog.DefaultSubmissionNotice) and request.kind == "enhancement":
        notice = new_item_dialog.SubmissionNotice("enhancement")
    if request.page and request.page not in page_frame.OWNER_PAGES:
        raise SubmissionError("Open the New item dialog from a page owner.")
    if not request.page and request.page_recipient != new_item_dialog.PageRecipient.PAGE_OWNER:
        raise SubmissionError("Select a recipient from the New item dialog.")
    producer_route = request.page_recipient == new_item_dialog.PageRecipient.PRODUCER
    if isinstance(request.assignment, new_item_dialog.ChosenAssignee) and not producer_route:
        raise SubmissionError("Select a producer for the assignee.")
    if request.page and kind == SubmissionKind.PROPOSAL and not producer_route:
        request = replace(request, page_recipient=new_item_dialog.PageRecipient.SELECTED)
    legacy_draft = DRAFT_ID.fullmatch(request_id) is not None
    browser_draft = BROWSER_DRAFT_ID.fullmatch(request_id) is not None
    list_draft = browser_draft and request.page == "list"
    legacy_list_draft = legacy_draft and request.page == "list"
    if producer_route and legacy_draft:
        raise SubmissionError("Open New item again to select a producer.")
    if request_id.startswith(("r2-", "r3-", "r4-", "r5-", "r6-")) and not (legacy_draft or browser_draft):
        raise SubmissionError("Invalid draft request ID.")
    address = (SubmissionAddress(kind, request.recipient_id, request_id) if legacy_draft or browser_draft
               else _request_address(kind, request.recipient_id, request_id))
    words = read_words(request.words, "Your words", kind=request.kind)
    evidence = read_evidence(new_item_dialog.evidence_fields(request.evidence))
    uploads = _decode_images(request.images)
    if list_draft:
        previous = _recover_pre_upgrade_native_draft(_request_folder(request_id), address, words, uploads, members,
                                                    request=request, capture_target=capture_target)
        if previous is not None:
            return previous
    state = request.page_state
    missing_capture = capture_requested and isinstance(screenshot, new_item_dialog.NoPageScreenshot)
    unverified = refusal is not None or missing_capture
    if refusal is None and (missing_capture or isinstance(screenshot, new_item_dialog.FiledPageScreenshot)):
        refusal = new_item_dialog.SubmissionScreenshotExpired()
    if refusal is not None and (list_draft or not (legacy_draft or browser_draft)):
        raise refusal

    payload_hash = ""
    if browser_draft:
        payload_hash = _browser_payload_hash(request, words, evidence, uploads, screenshot, initial_delivery)
    elif legacy_draft:
        payload_hash = _legacy_draft_fingerprint(request_id, request, words, uploads)
    else:
        payload_hash = _submission_fingerprint(replace(request, evidence=evidence), words, uploads)
    if isinstance(screenshot, new_item_dialog.PageScreenshot):
        total = len(screenshot.png) + sum(len(upload.data) for upload in uploads)
        if len(uploads) >= MAX_IMAGES or total > MAX_TOTAL_IMAGE_BYTES:
            raise SubmissionError("The screenshot and pasted images exceed the attachment limits.")
        uploads = (*uploads, _ImageUpload(PAGE_SCREENSHOT, "png", screenshot.png))
        if kind != SubmissionKind.BUG and isinstance(evidence, new_item_dialog.PageDataEvidence):
            evidence = NoEvidence()
        if state is not None:
            state = replace(state, viewport=screenshot.viewport, include_screenshot=True)
    elif state is not None and (isinstance(capture_intent, new_item_dialog.DeferredCaptureIntent)
                                or not (legacy_draft or list_draft)):
        state = replace(state, viewport=None, include_screenshot=False)
    request = replace(request, kind=kind, evidence=evidence, page_state=state)
    try:
        if list_draft:
            locked = _locked_browser_recipient(address, request.page, request.page_recipient)
        elif browser_draft:
            locked = _locked_browser_request(address, members, page=request.page,
                                             page_recipient=request.page_recipient, refusal=refusal)
        elif legacy_list_draft:
            locked = _locked_list_draft(address, members, fingerprint=payload_hash, page_recipient=request.page_recipient)
        elif legacy_draft:
            locked = _locked_draft_request(address, members, page=request.page, fingerprint=payload_hash,
                                           page_recipient=request.page_recipient)
        else:
            folder = _request_folder(request_id)
            request_exists = folder.is_dir()
            if request_exists:
                # Only a receipt proves the first writer finished publication.
                receipt = _request_hint(address, receipt_only=True)
                hint = _request_hint(address) if isinstance(receipt, NoRequestFile) else receipt
                try:
                    found = locate_native_submission(address, hint, members=members)
                except OSError:
                    found = NoNativeSubmission()
                if (isinstance(receipt, NativeSubmissionDestination) and isinstance(found, LocatedSubmission)
                        and found.destination == replace(receipt, path=found.destination.path)):
                    destination = found.destination
                    location_changed = destination.path != receipt.path
                    with investigation.recipient_locks(receipt.owner) if location_changed else nullcontext():
                        # Check the receipt and record again under the transfer lock.
                        if location_changed:
                            found = (locate_native_submission(address, destination, search_stores=False)
                                     if _request_hint(address, receipt_only=True) == receipt else NoNativeSubmission())
                        if isinstance(found, LocatedSubmission) and found.destination == destination:
                            _check_same_request(receipt, request, payload_hash)
                            _stage_initial_delivery(folder, address, initial_delivery)
                            return _complete_receipt(folder, destination, receipt, found.record,
                                                     retry_uploads=uploads, payload_matches=True)
            locked = _locked_request(address, members, for_send=True, page=request.page,
                                     page_recipient=request.page_recipient)

        with locked as lookup:
            hint, located, current = lookup.hint, lookup.native, lookup.members
            address = lookup.address
            request_id = address.request_id
            request = replace(request, request_id=request_id, recipient_id=address.recipient_id)
            folder = _request_folder(request_id)
            request_exists = folder.is_dir()
            if isinstance(located, LocatedSubmission):
                saved = located.destination
                original_capture = located.details.capture_intent
                _check_capture_target(capture_target, saved)
                if isinstance(original_capture, new_item_dialog.DeferredCaptureIntent):
                    edited = (unverified or saved.payload_hash != payload_hash
                              or (isinstance(capture_intent, new_item_dialog.DeferredCaptureIntent)
                                  and capture_intent.capture_key != original_capture.capture_key))
                elif browser_draft:
                    edited = unverified or saved.payload_hash != payload_hash
                else:
                    _check_same_request(saved, request, payload_hash, draft=legacy_draft)
                    edited = False
                _stage_initial_delivery(folder, address, initial_delivery)
                lookup.bind_draft()
                completed = _complete_receipt(folder, saved, hint, located.record, edited=edited,
                                              retry_uploads=uploads, payload_matches=saved.payload_hash == payload_hash)
                return replace(completed, reused=True) if legacy_draft else completed
            if isinstance(capture_target, ScopedSubmissionKey):
                raise SubmissionConflict("This screenshot belongs to another item.")
            if refusal is not None:
                raise refusal
            if (folder / "receipt.json").is_file():
                raise OSError(f"Cannot find the native record for request {request_id}.")
            if isinstance(hint, UnreadableRequestFile):
                raise hint.error
            if isinstance(hint, ListItemDestination):
                raise OSError("The draft has another destination.")
            if not browser_draft and not isinstance(hint, NoRequestFile):
                _check_same_request(hint, request, payload_hash, draft=legacy_draft)

            assert current is not None
            recipient = lookup.recipient
            assert recipient is not None
            if isinstance(initial_delivery, InitialDelivery) and recipient.kind != "producer":
                # A pending record can resume with its original recipient and payload.
                if (not isinstance(hint, (PendingFiling, NativeSubmissionDestination))
                        or not (folder / "pending.json").is_file()
                        or isinstance(hint, NativeSubmissionDestination) and hint.owner != recipient.id):
                    raise SubmissionError("Pick a producer.")
                _check_same_request(hint, request, payload_hash)
            destination = proposal_folder(recipient, current) if kind == SubmissionKind.PROPOSAL else NoNativeFolder(kind)
            if kind == SubmissionKind.PROPOSAL and isinstance(destination, NoNativeFolder):
                raise SubmissionError("Select a recipient with a native proposal folder.")
            route: ListFilingRoute | ListFilingUnavailable | NoListFiling = NoListFiling()
            if producer_route:
                member = assigned_member(request.assignment, recipient, current)
                if isinstance(initial_delivery, InitialDelivery):
                    source = ("nate via /new_item" if isinstance(notice, new_item_dialog.SubmissionNotice)
                              and notice.from_command else "nate via New item")
                    unit = member.name if member.kind == "unit" else ""
                    route = _plan_producer_filing(PendingProducerFiling(member.id, recipient.id, unit, source), current)
            elif request.page == "list" and isinstance(initial_delivery, InitialDelivery):
                route = _plan_list_filing(initial_delivery.list_assignee, recipient, current)
            pending_file = folder / "pending.json"
            images = tuple(upload.reference for upload in uploads)
            proof = reply_tokens.mint()
            details = ReviewSubmission(request_id, payload_hash, evidence, images, proof.sha256,
                                       page=request.page, page_state=request.page_state, capture_intent=capture_intent,
                                       reply_token_nonce=proof.nonce, notice=notice, assignment=request.assignment,
                                       image_digests=tuple((upload.reference.id, upload.digest) for upload in uploads))
            title = submission_title(words)
            destinations: list[NativeSubmissionDestination] = []
            staging_started = False

            def before_write(identity: str, path: Path) -> None:
                nonlocal staging_started
                if not staging_started:
                    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                    staging_started = True
                    if browser_draft and (not isinstance(hint, NoRequestFile) or (folder / "images").exists()):
                        _discard_unfiled_send(folder)
                    if legacy_list_draft and (folder / "images").is_dir():
                        # The draft fingerprint excludes the page screenshot.
                        shutil.rmtree(folder / "images")
                    _save_images(folder, request, uploads)
                    lookup.bind_draft()
                    _sync_draft_parents(folder)
                path = path.resolve()
                token = _browser_draft_token.get() if browser_draft else None
                token_digest = token.digest if isinstance(token, CurrentBrowserDraftToken) else ""
                saved = NativeSubmissionDestination(identity, kind, address.recipient_id, request_id,
                                                    payload_hash, path, path, recipient.id,
                                                    browser_token_digest=token_digest,
                                                    has_capture_intent=isinstance(capture_intent,
                                                                                 new_item_dialog.DeferredCaptureIntent))
                if list_draft and saved.has_capture_intent:
                    _write_location(_request_folder(submitted_draft_id) / "list-capture-target.json", saved)
                if browser_draft:
                    _write_draft_index(folder, ClaimedDraft(saved))
                _write_location(pending_file, saved)
                if isinstance(route, dict):
                    _write_request_file(folder / "list-filing.json", route)
                elif isinstance(route, ListFilingUnavailable):
                    fields = {"unavailable": route.reason}
                    retry = route.retry
                    if isinstance(retry, ListCommandAssignee):
                        fields["assignee"] = retry.id
                    elif isinstance(retry, PendingProducerFiling):
                        fields.update(owner=retry.owner, producer=retry.producer, unit=retry.unit, source=retry.source)
                    _write_request_file(folder / "list-filing.json", fields)
                if isinstance(list_preview, RequestedListPreview) and isinstance(screenshot, new_item_dialog.PageScreenshot):
                    _write_request_file(folder / "list-preview.json",
                                        {"key": list_preview.key, "digest": screenshot.digest})
                _stage_initial_delivery(folder, address, initial_delivery)
                destinations[:] = [saved]

            try:
                record = _file(kind, recipient, title, words, details, current, destination, before_write)
            except Exception:
                if not browser_draft or not staging_started:
                    raise
                if destinations:
                    saved = destinations[0]
                    found = _read_native_submission(address, saved.path, saved.owner)
                    if isinstance(found, LocatedSubmission):
                        _check_same_request(found.destination, request, payload_hash, draft=legacy_draft)
                        filed = replace(found.destination, browser_token_digest=saved.browser_token_digest)
                        return _complete_receipt(folder, filed, saved, found.record, first_send=True)
                _discard_unfiled_send(folder)
                raise
            saved, = destinations
            return _complete_receipt(folder, saved, saved, record, first_send=browser_draft or (
                (legacy_draft or not request_exists) and isinstance(hint, NoRequestFile)))
    except (investigation.InvestigationError, proposal_issue.Refusal, update_registration.RegistrationFailure,
            update_registration.InputError, record_store.RecordAllocationRefused, Refusal,
            _UnreadableOwnerDocument) as error:
        raise SubmissionError(str(error)) from error


def _request_address(kind: SubmissionKind, recipient_id: staff.StaffId, request_id: str) -> SubmissionAddress:
    require_request_binding(kind, recipient_id, request_id)
    return SubmissionAddress(kind, recipient_id, request_id)


class SavedSubmissionMissing(SubmissionError):
    """The scoped item no longer exists."""


@contextmanager
def submission_for_delivery(address: SubmissionAddress) -> Generator[LocatedSubmission | NoNativeSubmission]:
    """Read the current native owner under the same locks as a move."""
    with _locked_request(address) as lookup:
        if isinstance(lookup.native, NoNativeSubmission) and isinstance(lookup.hint, UnreadableRequestFile):
            raise lookup.hint.error
        yield lookup.native


@contextmanager
def scoped_submission(scoped: ScopedSubmissionKey, *, members: staff.StaffSnapshot | None = None
                      ) -> Generator[LocatedSubmission]:
    """Select the exact native record and hold its lock through the action."""
    address = _request_address(scoped.kind, scoped.recipient_id, scoped.request_id)
    with _locked_request(address, members) as lookup:
        located = lookup.native
        if not isinstance(located, LocatedSubmission) or located.destination.id != scoped.item_id:
            raise SavedSubmissionMissing("The sent item does not exist.")
        yield located


def committed_page_captures() -> Iterator[tuple[ScopedSubmissionKey, new_item_dialog.DeferredCaptureIntent, bool]]:
    """Read capture intents from the native items named by receipt hints."""
    for folder in _attachment_root().glob("*"):
        if not folder.name.startswith(("r2-", "r4-", "r5-", "r6-")) or not folder.is_dir():
            continue
        try:
            hint = _read_location_hint(folder)
            if (not isinstance(hint, NativeSubmissionDestination) or not hint.has_capture_intent
                    or _request_folder(hint.request_id) != folder):
                continue
            with _locked_request(hint.address) as lookup:
                located = lookup.native
                if not isinstance(located, LocatedSubmission):
                    continue
                intent = located.details.capture_intent
                attached = PAGE_SCREENSHOT in located.details.images
                saved = located.destination
            if isinstance(intent, new_item_dialog.DeferredCaptureIntent):
                yield ScopedSubmissionKey(saved.kind, saved.recipient_id, saved.id, saved.request_id), intent, attached
        except (OSError, ValueError) as error:
            print(f"Review capture recovery for {folder.name}: {error}", file=sys.stderr)


def read_attached_page_screenshot(target: ScopedSubmissionKey, request: new_item_dialog.PageCaptureRequest
                                  ) -> new_item_dialog.PageScreenshot:
    """Read the PNG already attached to the native record."""
    with scoped_submission(target) as located:
        if PAGE_SCREENSHOT not in located.details.images:
            raise SubmissionError("The screenshot is no longer attached.")
        path = image_folder(located.details) / PAGE_SCREENSHOT.id / PAGE_SCREENSHOT.file
        with path.open("rb") as stream:
            image = stream.read(MAX_IMAGE_BYTES + 1)
    if len(image) > MAX_IMAGE_BYTES or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SubmissionError("The attached screenshot is invalid.")
    return new_item_dialog.PageScreenshot(image, new_item_dialog.PageViewport(request.width, request.height, request.dark))


def current_reply_proof(key: str) -> reply_tokens.ReplyTokenProof:
    """Read the proof from the current native note for a saved delivery."""
    scoped = read_submission_key(key)
    address = _request_address(scoped.kind, scoped.recipient_id, scoped.request_id)
    located = locate_native_submission(address, _request_hint(address))
    if not isinstance(located, LocatedSubmission) or located.destination.id != scoped.item_id:
        raise reply_tokens.UnavailableReplyToken("Cannot find the note for this reply token.")
    return located.details.reply_proof


def renew_legacy_reply(located: LocatedSubmission) -> SavedSubmission:
    """Give a waiting legacy note a new proof while the caller holds the recipient locks."""
    saved = located.saved()
    if not saved.awaiting_reply or saved.details.reply_token_nonce:
        return saved
    target = located.destination
    lock = (update_registration.wrap_question_locks(target.owner) if target.kind == SubmissionKind.QUESTION
            else proposal_issue.issue_lock(target.path) if target.kind == SubmissionKind.PROPOSAL
            else nullcontext())
    with lock:
        current = _read_native_submission(target.address, target.path, target.owner)
        if not isinstance(current, LocatedSubmission) or current.destination.id != target.id:
            raise reply_tokens.UnavailableReplyToken("Cannot find the note for this reply token.")
        record = current.record
        if _awaiting_reply(record) and not current.details.reply_token_nonce:
            proof = reply_tokens.mint()
            match record:
                case update_registration.QuestionRecord():
                    fields = record.item.get("review_submission", {})
                    _ = update_registration.write_review_submission(
                        record, new_item_dialog.renewed_submission_fields(fields, proof))
                case Issue():
                    proposal_issue.renew_reply_proof(record, proof)
                case investigation.Investigation():
                    investigation.renew_reply_proof(target.path, record, proof)
            updated = _read_native_submission(target.address, target.path, target.owner)
            if not isinstance(updated, LocatedSubmission) or updated.destination.id != target.id:
                raise reply_tokens.UnavailableReplyToken("Cannot read the renewed reply proof.")
            record = updated.record
        return replace(saved, path=target.path, owner=target.owner, record=record)


def _with_screenshot(details: ReviewSubmission, owner: staff.StaffId,
                     image: bytes | new_item_dialog.PageScreenshot) -> ReviewSubmission:
    """Store the screenshot and keep the page data for a Bug report."""
    state = details.page_state
    evidence = details.evidence if state is not None else NoEvidence()
    if isinstance(image, new_item_dialog.PageScreenshot):
        if state is not None:
            state = replace(state, viewport=image.viewport, include_screenshot=True)
        evidence = details.evidence
        image = image.png
    reference = PAGE_SCREENSHOT
    if ((details.page_state is None and not isinstance(details.evidence, (new_item_dialog.PageDataEvidence, NoEvidence)))
            or len(details.images) >= MAX_IMAGES or reference in details.images):
        return details
    images = image_folder(details)
    used = sum((images / item.id / item.file).stat().st_size for item in details.images)
    path = images / reference.id / reference.file
    size = path.stat().st_size if path.is_file() else len(image)
    if used + size > MAX_TOTAL_IMAGE_BYTES:
        return details
    if not path.is_file():
        with tempfile.TemporaryDirectory(prefix=".screenshot-", dir=images.parent) as temporary:
            source = Path(temporary) / reference.file
            _ = source.write_bytes(image)
            _ = image_store.register_file(source, images, owner=owner, subject="Review request " + details.request_id,
                                          identity=reference.id, private=True)
            atomic_file.sync_folder(images)
            atomic_file.sync_folder(images.parent)
    return replace(details, evidence=evidence, images=(*details.images, reference), page_state=state,
                   image_digests=(*details.image_digests, (reference.id, hashlib.sha256(path.read_bytes()).hexdigest())))


def _write_investigation_submission(located: LocatedSubmission, details: ReviewSubmission) -> None:
    record = located.record
    assert isinstance(record, investigation.Investigation)
    investigation.write_review_submission(located.destination.path, record, details)


class ScreenshotAttachment(Enum):
    ATTACHED = "screenshot attached"
    ALREADY_ATTACHED = "screenshot already attached"
    UNAVAILABLE = "screenshot cannot be attached"


def attach_page_screenshot(key: str, image: bytes) -> bool:
    return attach_submission_screenshot(read_submission_key(key), image) is ScreenshotAttachment.ATTACHED


def attach_submission_screenshot(scoped: ScopedSubmissionKey, image: bytes | new_item_dialog.PageScreenshot
                                 ) -> ScreenshotAttachment:
    """Update the current native record under its existing write locks."""
    with scoped_submission(scoped) as located:
        identity, path, owner = scoped.item_id, located.destination.path, located.destination.owner
        match located.record:
            case update_registration.QuestionRecord():
                with update_registration.wrap_question_locks(owner):
                    question = update_registration.read_review_question(path, owner)
                    origin = native_origin(question)
                    if (question.owner != owner or question.item.get("review_id") != identity
                            or not isinstance(origin, ReviewSubmission) or origin.request_id != scoped.request_id):
                        raise SubmissionError("The selected Question changed.")
                    details = _with_screenshot(origin, owner, image)
                    if details == origin:
                        return (ScreenshotAttachment.ALREADY_ATTACHED if PAGE_SCREENSHOT in origin.images
                                else ScreenshotAttachment.UNAVAILABLE)
                    question.item["review_submission"] = new_item_dialog.stored_submission(details)
                    update_registration.write_question_record(question)
            case investigation.Investigation(submission=origin) if isinstance(origin, ReviewSubmission):
                details = _with_screenshot(origin, owner, image)
                if details == origin:
                    return (ScreenshotAttachment.ALREADY_ATTACHED if PAGE_SCREENSHOT in origin.images
                            else ScreenshotAttachment.UNAVAILABLE)
                _write_investigation_submission(located, details)
            case _:
                return ScreenshotAttachment.UNAVAILABLE
    return ScreenshotAttachment.ATTACHED


def save_bug_page_state(saved: NativeSubmissionDestination, item_key: str, request: NewItemRequest | None,
                        screenshot: Callable[[str, new_item_dialog.BugPageState], bool]) -> bool:
    """Create one case record and report whether a replay adds state to the Bug."""
    folder = review_link.review_root() / "page-states"
    path = folder / (saved.request_id + ".json")
    if path.is_file():
        return False
    with exclusive(_request_folder(saved.request_id) / ".page-state.lock"):
        if path.is_file():
            return False
        key = ScopedSubmissionKey(saved.kind, saved.recipient_id, saved.id, saved.request_id)
        with scoped_submission(key) as located:
            report = located.record
            assert isinstance(report, investigation.Investigation)
            details = report.submission
            assert isinstance(details, ReviewSubmission)
            added = details.page_state is None
            if added:
                if request is None or request.page_state is None:
                    return False
                page_data = request.evidence
                if not isinstance(page_data, new_item_dialog.PageDataEvidence):
                    if not request.page_state.title:
                        return False
                    page_data = new_item_dialog.PageDataEvidence(request.page_state.title, ())
                details = replace(details, page=request.page, page_state=request.page_state, page_state_data=page_data)
                _write_investigation_submission(located, details)
        state, data = details.page_state, details.bug_page_data
        assert state is not None
        captured = PAGE_SCREENSHOT in details.images
        attached = False
        capture_error: OSError | ValueError | RuntimeError | None = None
        if state.include_screenshot and not captured and len(details.images) < MAX_IMAGES:
            try:
                attached = screenshot(details.page, state)
            except (OSError, ValueError, RuntimeError) as error:
                capture_error = error
        record = {
            "request_id": saved.request_id, "item_key": item_key,
            "filed_at": datetime.fromisoformat(report.filed).astimezone(user_zone()).isoformat(timespec="seconds"),
            "machine": this_machine(), "page": details.page, "url": state.url,
            **({"viewport": asdict(state.viewport)} if state.viewport is not None else {}),
            "render_seconds": state.render_seconds, "notices": state.notices,
            "script_errors": [asdict(error) for error in state.script_errors],
            "page_data": {**(data.page_data() if data else {"rows": ()}),
                          "title": state.title or (data.title if data else details.page)},
            "screenshot": PAGE_SCREENSHOT.id if captured or attached else None, "words": report.question,
        }
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ = atomic_file.create_text_atomic(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        if capture_error is not None:
            raise capture_error
        return added and not attached


def record_deferred_bug_screenshot(target: new_item_dialog.ScopedSubmissionKey,
                                   viewport: new_item_dialog.PageViewport) -> None:
    """Reconcile the Bug case with the screenshot on the native record."""
    if target.kind != SubmissionKind.BUG:
        return
    with scoped_submission(target) as located:
        if located.details.page_state is None:
            return
        saved = located.destination
    item_key = f"investigation/{saved.owner}/{saved.id}"
    _ = save_bug_page_state(saved, item_key, None, lambda _page, _state: False)
    path = review_link.review_root() / "page-states" / (target.request_id + ".json")
    with exclusive(_request_folder(target.request_id) / ".page-state.lock"):
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record.update(screenshot=PAGE_SCREENSHOT.id, viewport=asdict(viewport))
        atomic_file.write_text_atomic(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")


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
                save = update_registration.prepare_submission_reply(path, scoped, words, reply_token, owner=owner)
            case SubmissionKind.PROPOSAL:
                save = proposal_issue.prepare_submission_reply(path, scoped, words, reply_token, owner=owner)
            case SubmissionKind.BUG | SubmissionKind.INVESTIGATION:
                save = investigation.prepare_submission_reply(path, scoped, words, reply_token, owner=owner)
        with scoped_submission(scoped, members=members) as current:
            if (current.destination.path, current.destination.owner) != (path, owner):
                raise SubmissionError("The selected item changed.")
            return save()
    except (investigation.InvestigationError, proposal_issue.Refusal, update_registration.RegistrationFailure) as error:
        raise SubmissionError(str(error)) from error


def scrub_reply_tokens(members: staff.StaffSnapshot) -> int:
    """Convert legacy reply tokens in the native stores and archives."""
    kinds = (record_store.RecordKind.QUESTION, record_store.RecordKind.PROPOSAL,
             record_store.RecordKind.INVESTIGATION)
    folders = tuple(folder for kind in kinds for folder in record_store.staff_store_folders(kind, members=members))
    changed = 0
    keys = frozenset(("record_type", "registration", "review_submission", "id", "owner"))
    for path in record_store.stored_paths(folders, strict=True):
        fields = Issue(path, record_store.read_header(path, strict=True, keys=keys))
        if fields.value("record_type") == "question":
            if '"reply_token"' not in fields.value("registration"):
                continue
            question = update_registration.read_review_question(path)
            saved = update_registration.scrub_reply_token(path, question.owner)
        elif not re.search(r'\breply_token\\?"\s*:', fields.value("review_submission")):
            continue
        elif fields.value("record_type") == "proposal":
            saved = proposal_issue.scrub_reply_token(path)
        elif fields.value("id") and fields.value("owner"):
            saved = investigation.scrub_reply_token(path, staff.StaffId(fields.value("owner")), fields.value("id"))
        else:
            continue
        if saved:
            changed += 1
            print(f"Rewrote {path}")
    return changed


def main(argv: list[str]) -> int:
    if argv != ["scrub-reply-tokens"]:
        print("usage: python -m scripts.review_links.review_submissions scrub-reply-tokens", file=sys.stderr)
        return 2
    try:
        _ = scrub_reply_tokens(staff.snapshot())
    except (OSError, ValueError, update_registration.RegistrationFailure) as error:
        print(f"Reply token scrub: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
