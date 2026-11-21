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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

from scripts.agents.remote import local_machine, this_machine
from scripts.lib import atomic_file, conf
from scripts.lib.file_lock import exclusive
from scripts.lib.user_zone import user_zone
from scripts.production import investigation, proposal_issue, record_store, staff, update_registration
from scripts.production.issues import Issue
from scripts.production.production_doc import WRAPPED, Refusal
from scripts.review_links import image_store, new_item_dialog, page_frame, review_events, review_link
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

DRAFT_ID = re.compile(r"r2-[0-9a-f]{32}")
LEGACY_DRAFT_BINDING = re.compile(r"(r2-[0-9a-f]{32})-.+")
PAGE_SCREENSHOT = ImageReference("000000000000", "image.png")


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
class SavedSubmission(NativeSubmissionDestination):
    """The receipt for a completed native write."""

    record: NativeSubmittedRecord = field(compare=False)
    reused: bool = field(default=False, compare=False)

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


def _request_folder(request_id: str) -> Path:
    snapshot_root = review_link.review_root().resolve()
    configured = conf.read_or_repository("review_links").text("review_attachments_root")
    root = (snapshot_root.parent / Path(configured).expanduser()).resolve()
    if root.is_relative_to(snapshot_root) or root.is_relative_to(record_store.state_root().resolve()):
        raise OSError("The Review attachment root must be outside the snapshot and staff state roots.")
    identity = read_request_id(request_id)
    draft = LEGACY_DRAFT_BINDING.fullmatch(identity)
    return root / (draft[1] if draft else identity)


def image_folder(item: SavedSubmission | ReviewSubmission) -> Path:
    """Return the private folder for the image IDs on the native record."""
    return _request_folder(item.request_id) / "images"


def _image_references(uploads: tuple[_ImageUpload, ...]) -> tuple[ImageReference, ...]:
    return tuple(ImageReference(f"{number:012x}", f"image.{upload.extension}")
                 for number, upload in enumerate(uploads, 1))


def _save_images(folder: Path, request: NewItemRequest, uploads: tuple[_ImageUpload, ...],
                 references: tuple[ImageReference, ...]) -> None:
    if not references:
        return
    images = folder / "images"
    if images.exists():
        for reference, upload in zip(references, uploads, strict=True):
            if (images / reference.id / reference.file).read_bytes() != upload.data:
                raise OSError("A saved Review image does not match the request.")
        return

    for upload in uploads:
        _validate_image(upload)
    # Publish the complete image batch with one rename.
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=folder) as temporary:
        workspace = Path(temporary)
        stage = workspace / "images"
        for reference, upload in zip(references, uploads, strict=True):
            source = workspace / reference.file
            atomic_file.write_bytes_atomic(source, upload.data, mode=0o600)
            _ = image_store.register_file(source, stage, owner=request.recipient_id,
                                          subject="Review request " + request.request_id, identity=reference.id, private=True)
        atomic_file.sync_folder(stage)
        _ = stage.rename(images)
        atomic_file.sync_folder(folder)


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
                        check_recipient_id(staff.StaffId(recipient))
                    except SubmissionError as error:
                        raise OSError(f"Cannot read the Review request file at {path}.") from error
                    if DRAFT_ID.fullmatch(request_id):
                        request_id = f"{request_id}-{kind}-{recipient}"
                    return NativeSubmissionDestination(identity, kind, staff.StaffId(recipient), request_id,
                                                       payload_hash, Path(native_path), Path(original_path), filing_owner)
    raise OSError(f"Cannot read the Review request file at {path}.")


def _check_same_request(saved: NativeSubmissionDestination | PendingFiling, request: NewItemRequest,
                        payload_hash: str, *, draft: bool = False) -> None:
    if saved.payload_hash != payload_hash or (isinstance(saved, NativeSubmissionDestination)
            and (saved.kind != request.kind or saved.recipient_id != request.recipient_id)):
        if draft:
            raise OSError(f"The Review fingerprint does not match request {request.request_id}.")
        raise SubmissionConflict("This request ID already has different item data.")


def _image_fingerprints(uploads: tuple[_ImageUpload, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((upload.extension, hashlib.sha256(upload.data).hexdigest()) for upload in uploads)


def _fingerprint(payload: list[object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _submission_fingerprint(request: NewItemRequest, words: str, uploads: tuple[_ImageUpload, ...]) -> str:
    payload: list[object] = [request.kind, words, asdict(request.evidence), _image_fingerprints(uploads)]
    if request.page:
        payload.append(request.page)
    if request.page_recipient == new_item_dialog.PageRecipient.LIST_OWNER:
        payload.append(request.page_recipient)
    return _fingerprint(payload)


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
        receipt = _read_request_file(folder / "receipt.json", folder.name)
        if receipt_only or not isinstance(receipt, NoRequestFile):
            return receipt
        return _read_request_file(folder / "pending.json", folder.name)
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
                if find_native:
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
    elif page_recipient == new_item_dialog.PageRecipient.LIST_OWNER:
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
                             address.request_id, details.request_digest, path, path, owner), details, record)


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
        if _read_request_file(receipt, folder.name) != saved:
            raise
    atomic_file.sync_folder(folder)


def _has_receipt_for(saved: NativeSubmissionDestination) -> bool:
    receipt = _request_hint(saved.address, receipt_only=True)
    return (isinstance(receipt, NativeSubmissionDestination)
            and (receipt.address, receipt.id, receipt.owner, receipt.payload_hash)
            == (saved.address, saved.id, saved.owner, saved.payload_hash))


def _complete_receipt(folder: Path, saved: NativeSubmissionDestination,
                      hint: RequestFile, record: NativeSubmittedRecord, *, first_send: bool = False) -> SavedSubmission:
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
    return SavedSubmission(saved.id, saved.kind, saved.recipient_id, saved.request_id, saved.payload_hash,
                           saved.path, saved.original_path, saved.owner, record)


def _recipient(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffRow:
    row = members.by_id(identity)
    if row is None or row.kind not in ("showrunner", "producer"):
        raise SubmissionError("Select a showrunner or producer.")
    return row


class _UnreadableOwnerDocument(OSError):
    """Send could not read a named local owner document."""

    def __init__(self, owner: staff.StaffRow) -> None:
        super().__init__(f"Cannot read the production document for {owner.name}.")


def owner_is_active(row: staff.StaffRow, *, require_document: bool = False) -> bool:
    """Check both the staff row and the production document."""
    if isinstance(row.doc, staff.WrappedDoc) or isinstance(row.worktree, staff.RemovedWorktree):
        return False
    if isinstance(row.doc, staff.LiveDoc):
        try:
            if row.kind == "producer" and row.doc.path.is_dir():
                return True
            lines = row.doc.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            if require_document:
                raise _UnreadableOwnerDocument(row) from error
            if isinstance(error, FileNotFoundError):
                return False
            raise
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
    return owner_is_active(row, require_document=True)


@dataclass(frozen=True)
class PageOwnerUnavailable:
    """The configured production has no active recipient."""


def page_owner(page: str, settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | PageOwnerUnavailable:
    """Resolve the page owner from retained staff state on either machine."""
    key = f"page.{page}.owner"
    values = settings.values(key, "owner.default")
    designated = staff.StaffId(values.get(key, ""))
    default = staff.StaffId(values.get("owner.default", ""))
    for identity in (designated, default):
        for candidate in investigation.owner_chain(identity, members):
            row = _available_page_owner(candidate, members, allow_producer=candidate == designated)
            if isinstance(row, staff.StaffRow):
                return row
    return PageOwnerUnavailable()


def _available_page_owner(identity: staff.StaffId, members: staff.StaffSnapshot, *,
                          allow_producer: bool) -> staff.StaffRow | PageOwnerUnavailable:
    row = members.rows_by_id.get(identity)
    if row is None or identity in members.pending_retire or isinstance(row.exited, staff.Exited):
        return PageOwnerUnavailable()
    if isinstance(row.doc, staff.WrappedDoc) or isinstance(row.worktree, staff.RemovedWorktree):
        return PageOwnerUnavailable()
    if row.kind == "showrunner" and isinstance(row.doc, staff.LiveDoc) or row.kind == "producer" and allow_producer:
        return row
    return PageOwnerUnavailable()


def _configured_list_producer(settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | PageOwnerUnavailable:
    configured = settings.origin("page.list.owner", str)
    if isinstance(configured, conf.UnavailableValue):
        return PageOwnerUnavailable()
    row = members.by_id(staff.StaffId(configured.value))
    return row if row is not None and row.kind == "producer" else PageOwnerUnavailable()


def list_producer(settings: conf.ConfigRead, members: staff.StaffSnapshot) -> staff.StaffRow | PageOwnerUnavailable:
    """Find the active producer that owns the List."""
    row = _configured_list_producer(settings, members)
    if (isinstance(row, PageOwnerUnavailable) or row.id not in members.rows_by_id or row.id in members.pending_retire
            or isinstance(row.exited, staff.Exited) or isinstance(row.doc, staff.WrappedDoc)
            or isinstance(row.worktree, staff.RemovedWorktree)):
        return PageOwnerUnavailable()
    return row


def _send_recipient(address: SubmissionAddress, page: str, members: staff.StaffSnapshot,
                    choice: new_item_dialog.PageRecipient) -> staff.StaffRow:
    identity = address.recipient_id
    if choice == new_item_dialog.PageRecipient.LIST_OWNER:
        configured = _configured_list_producer(conf.read_or_repository("console"), members)
        if not isinstance(configured, staff.StaffRow) or configured.id != identity:
            raise SubmissionConflict("The List owner is unavailable. Open New item again.")
    elif page and page != "list":
        configured = page_owner(page, conf.read_or_repository("console"), members)
        if isinstance(configured, PageOwnerUnavailable):
            raise SubmissionConflict("The page owner is unavailable. Open New item again.")
        identity = configured.id
    return _active_recipient(identity, members)


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


def proposal_folder(recipient: staff.StaffRow) -> Path | NoNativeFolder:
    from scripts.production import proposals_folder

    if isinstance(recipient.doc, (staff.LiveDoc, staff.WrappedDoc)) and not _has_remote_document(recipient):
        try:
            folder = None if recipient.doc.path.is_dir() else proposals_folder.issues_folder(recipient.doc.path)
        except OSError as error:
            raise _UnreadableOwnerDocument(recipient) from error
        if folder is not None:
            return folder
    return NoNativeFolder(SubmissionKind.PROPOSAL)


def recipient_options(members: staff.StaffSnapshot) -> list[RecipientOption]:
    """List active recipients and indicate who can receive proposals."""
    options: list[RecipientOption] = []
    producer = list_producer(conf.read_or_repository("console"), members)
    for row in (*staff.active(members, "showrunner"), *staff.active(members, "producer")):
        try:
            active = row == producer or _submission_owner_is_active(row, members)
        except OSError:
            continue
        if not active:
            continue
        try:
            can_receive_proposals = isinstance(proposal_folder(row), Path)
        except OSError:
            can_receive_proposals = False
        options.append(RecipientOption(id=row.id, name=row.name, proposal=can_receive_proposals))
    return options


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


def submit(request: NewItemRequest, *, members: staff.StaffSnapshot | None = None) -> SavedSubmission:
    """Validate and file the item once in its native store."""
    check_recipient_id(request.recipient_id)
    request_id = read_request_id(request.request_id)
    kind = read_kind(request.kind)
    if request.page and (request.page not in page_frame.OWNER_PAGES or kind == SubmissionKind.PROPOSAL):
        raise SubmissionError("Open the New item dialog from a page owner.")
    if not request.page and request.page_recipient != new_item_dialog.PageRecipient.PAGE_OWNER:
        raise SubmissionError("Select a recipient from the New item dialog.")
    is_draft = DRAFT_ID.fullmatch(request_id) is not None
    if request_id.startswith(("r2-", "r3-", "r4-", "r5-")) and not is_draft:
        raise SubmissionError("Invalid draft request ID.")
    address = (SubmissionAddress(kind, request.recipient_id, request_id) if is_draft
               else _request_address(kind, request.recipient_id, request_id))
    words = read_words(request.words, "Your words")
    evidence = read_evidence(asdict(request.evidence))
    uploads = _decode_images(request.images)
    request = replace(request, kind=kind, evidence=evidence)
    payload_hash = _fingerprint([request_id, kind, words, _image_fingerprints(uploads)]) if is_draft else ""
    folder = None
    request_exists = False
    try:
        if not is_draft:
            payload_hash = _submission_fingerprint(request, words, uploads)
            folder = _request_folder(request_id)
            request_exists = folder.exists()
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
                    return _complete_receipt(folder, found.destination, receipt, found.record)

        locked = (_locked_draft_request(address, members, page=request.page, fingerprint=payload_hash,
                                       page_recipient=request.page_recipient)
                  if is_draft else _locked_request(address, members, for_send=True, page=request.page,
                                                  page_recipient=request.page_recipient))
        with locked as lookup:
            hint, located, current = lookup.hint, lookup.native, lookup.members
            if is_draft:
                address = lookup.address
                request_id = address.request_id
                request = replace(request, request_id=request_id, recipient_id=address.recipient_id)
                folder = _request_folder(request_id)
            assert folder is not None
            if isinstance(located, LocatedSubmission):
                saved = located.destination
                _check_same_request(saved, request, payload_hash, draft=is_draft)
                lookup.bind_draft()
                completed = _complete_receipt(folder, saved, hint, located.record)
                return replace(completed, reused=True) if is_draft else completed
            if (folder / "receipt.json").is_file():
                raise OSError(f"Cannot find the native record for request {request_id}.")
            if isinstance(hint, UnreadableRequestFile):
                raise hint.error
            if not isinstance(hint, NoRequestFile):
                _check_same_request(hint, request, payload_hash, draft=is_draft)

            assert current is not None
            members = current
            recipient = lookup.recipient
            assert recipient is not None
            destination = proposal_folder(recipient) if kind == SubmissionKind.PROPOSAL else NoNativeFolder(kind)
            if kind == SubmissionKind.PROPOSAL and isinstance(destination, NoNativeFolder):
                raise SubmissionError("Select a recipient with a native proposal folder.")
            pending_file = folder / "pending.json"
            images = _image_references(uploads)
            details = ReviewSubmission(request_id, payload_hash, evidence, images, new_reply_token(),
                                       page=request.page, page_state=request.page_state)
            title = next(line.strip() for line in words.splitlines() if line.strip())[:120].rstrip()
            destinations: list[NativeSubmissionDestination] = []

            def before_write(identity: str, path: Path) -> None:
                path = path.resolve()
                saved = NativeSubmissionDestination(identity, kind, address.recipient_id, request_id, payload_hash, path, path,
                                                    recipient.id)
                if not destinations:
                    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _save_images(folder, request, uploads, images)
                    lookup.bind_draft()
                    atomic_file.sync_folder(folder.parent)
                    atomic_file.sync_folder(folder.parent.parent)
                _write_location(pending_file, saved)
                destinations[:] = [saved]

            record = _file(kind, recipient, title, words, details, members, destination, before_write)
            saved, = destinations
            return _complete_receipt(folder, saved, saved, record,
                                     first_send=(is_draft or not request_exists) and isinstance(hint, NoRequestFile))
    except (investigation.InvestigationError, proposal_issue.Refusal, update_registration.RegistrationFailure,
            update_registration.InputError, record_store.RecordAllocationRefused, Refusal,
            _UnreadableOwnerDocument) as error:
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


def _with_screenshot(details: ReviewSubmission, owner: staff.StaffId, image: bytes) -> ReviewSubmission:
    """Store the screenshot and keep the page data for a Bug report."""
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
    evidence = details.evidence if details.page_state is not None else NoEvidence()
    return replace(details, evidence=evidence, images=(*details.images, reference))


def _write_investigation_submission(located: LocatedSubmission, details: ReviewSubmission) -> None:
    owner, identity, path = located.destination.owner, located.destination.id, located.destination.path
    store = investigation.Investigations(owner, "investigations", "I", "investigation",
                                         investigation.InvestigationError)
    fields, body = store.read(identity, path=path)
    fields["review_submission"] = json.dumps(new_item_dialog.stored_submission(details))
    with investigation.investigation_numbers() as numbers, numbers.write(path, identity, owner, folder=store.folder()):
        store.write(identity, fields, body, path=path)


def attach_page_screenshot(key: str, image: bytes) -> bool:
    """Update the current native record under its existing write locks."""
    scoped = read_submission_key(key)
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
                        return False
                    question.item["review_submission"] = new_item_dialog.stored_submission(details)
                    update_registration.write_question_record(question)
            case investigation.Investigation(submission=origin) if isinstance(origin, ReviewSubmission):
                details = _with_screenshot(origin, owner, image)
                if details == origin:
                    return False
                _write_investigation_submission(located, details)
            case _:
                return False
    return True


def save_bug_page_state(saved: SavedSubmission, item_key: str, request: NewItemRequest,
                        screenshot: Callable[[str, new_item_dialog.BugPageState], bool]) -> bool:
    """Create one case record and report whether a replay adds state to the Bug."""
    folder = review_link.review_root() / "page-states"
    path = folder / (saved.request_id + ".json")
    if path.is_file():
        return False
    with exclusive(_request_folder(saved.request_id) / ".page-state.lock"):
        if path.is_file():
            return False
        with scoped_submission(read_submission_key(saved.key)) as located:
            report = located.record
            assert isinstance(report, investigation.Investigation)
            details = report.submission
            assert isinstance(details, ReviewSubmission)
            added = details.page_state is None
            if added:
                if request.page_state is None or not isinstance(request.evidence, new_item_dialog.PageDataEvidence):
                    return False
                details = replace(details, page=request.page, page_state=request.page_state, page_state_data=request.evidence)
                _write_investigation_submission(located, details)
        state, data = details.page_state, details.bug_page_data
        assert state is not None and data is not None
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
            "machine": this_machine(), "page": details.page, "url": state.url, "viewport": asdict(state.viewport),
            "render_seconds": state.render_seconds, "notices": state.notices,
            "script_errors": [asdict(error) for error in state.script_errors],
            "page_data": {"title": data.title, "rows": data.rows},
            "screenshot": PAGE_SCREENSHOT.id if captured or attached else None, "words": report.question,
        }
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ = atomic_file.create_text_atomic(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        if capture_error is not None:
            raise capture_error
        return added and not attached


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
