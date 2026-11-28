"""Render the shared New item dialog."""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import math
import re
import zlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum, StrEnum
from io import BytesIO
from typing import Literal, cast
from uuid import UUID, uuid4

from scripts.production import staff
from scripts.review_links import page_frame, reply_proofs, reply_tokens


MAX_WORDS_LENGTH = 40_000
MAX_REQUEST_ID_LENGTH = 210
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_LIVE_CAPTURE_BYTES = 5 * 1024 * 1024
MAX_LIVE_CAPTURE_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_LIVE_CAPTURE_BASE64_LENGTH = 4 * ((MAX_LIVE_CAPTURE_BYTES + 2) // 3)
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PAGE_DATA_BYTES = 64 * 1024
MAX_PAGE_ROWS = 48
MAX_PAGE_SECTIONS = 16
MAX_PAGE_SECTION_ROWS = 128
MAX_BUG_PAGE_STATE_BYTES = 512 * 1024
MAX_PAGE_URL_LENGTH = 8192
BUILD_EVIDENCE_LIMITS = {"title": 300, "label": 200, "text": 4_000, "tipEntries": 64}
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}


class SubmissionError(ValueError):
    """The request cannot be saved or applied to this item."""


class SubmissionConflict(SubmissionError):
    """The request ID already names different item data."""

    status: Literal[409] = 409


class SubmissionScreenshotExpired(SubmissionConflict):
    """A new Send needs a fresh screenshot."""

    def __init__(self) -> None:
        super().__init__("The screenshot expired.")


class SubmissionScreenshotReload(SubmissionConflict):
    """An unfiled Send needs a reload to include the screenshot."""


@dataclass(frozen=True)
class UnverifiedDraftRetry:
    """A Send with a draft ID and fields that cannot be used to file."""

    request_id: str
    error: SubmissionError


class SubmissionKind(StrEnum):
    QUESTION = "question"
    PROPOSAL = "proposal"
    INVESTIGATION = "investigation"
    BUG = "bug"


@dataclass(frozen=True)
class ByItemId:
    value: str


@dataclass(frozen=True)
class ByRequestId:
    value: str


type ReviewSubmissionLookup = ByItemId | ByRequestId


@dataclass(frozen=True)
class NoEvidence:
    kind: Literal["none"] = field(default="none", init=False)


@dataclass(frozen=True)
class BuildCell:
    label: str
    value: str


class MemoryTone(StrEnum):
    BUILD = "build"
    FREE = "free"
    TOOLS = "tools"
    SYSTEM = "system"
    OTHER = "other"


@dataclass(frozen=True)
class MemorySegment:
    label: str
    gib: float
    tone: MemoryTone
    text: str | None = None


@dataclass(frozen=True)
class MemoryReading:
    free_for_next_gib: float | None
    queue_cap_gib: float | None
    segments: tuple[MemorySegment, ...]
    free_text: str | None = None
    cap_text: str | None = None


@dataclass(frozen=True)
class TipCaption:
    text: str
    kind: Literal["caption"] = field(default="caption", init=False)


@dataclass(frozen=True)
class TipField:
    label: str
    value: str
    kind: Literal["field"] = field(default="field", init=False)


@dataclass(frozen=True)
class TipDetail:
    text: str
    kind: Literal["detail"] = field(default="detail", init=False)


type TipEntry = TipCaption | TipField | TipDetail


@dataclass(frozen=True)
class BuildStepEvidence:
    captured_at: str
    step_id: str
    title: str
    owner: str
    cells: tuple[BuildCell, ...]
    memory: MemoryReading
    tip_title: str
    tip_status: str
    tip: tuple[TipEntry, ...]
    kind: Literal["build-step"] = field(default="build-step", init=False)


@dataclass(frozen=True)
class PageDataSection:
    """Keep the column labels with the page values."""

    label: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class PageDataEvidence:
    """Keep the page data available when a screenshot fails."""

    title: str
    rows: tuple[tuple[str, ...], ...]
    sections: tuple[PageDataSection, ...] = ()
    kind: Literal["page-data"] = field(default="page-data", init=False)

    def page_data(self) -> dict[str, object]:
        fields: dict[str, object] = {"title": self.title, "rows": self.rows}
        if self.sections:
            fields["sections"] = tuple(asdict(section) for section in self.sections)
        return fields


type Evidence = NoEvidence | BuildStepEvidence | PageDataEvidence


def evidence_fields(evidence: Evidence) -> dict[str, object]:
    """Preserve the fields used by existing request digests."""
    if isinstance(evidence, PageDataEvidence):
        return {**evidence.page_data(), "kind": evidence.kind}
    fields = cast(dict[str, object], asdict(evidence))
    if isinstance(evidence, BuildStepEvidence):
        # Keep the digest of requests saved before display text was added.
        memory = cast(dict[str, object], fields["memory"])
        for field_name in ("free_text", "cap_text"):
            if memory[field_name] is None:
                del memory[field_name]
        for segment in cast(tuple[dict[str, object], ...], memory["segments"]):
            if segment["text"] is None:
                del segment["text"]
    return fields


@dataclass(frozen=True)
class PageCaptureRequest:
    """Identify the source of a console screenshot and its viewport."""

    place: str
    width: int
    height: int
    dark: bool


@dataclass(frozen=True)
class LivePageCapture:
    """Keep the live document until Firefox renders it."""

    request: PageCaptureRequest
    document: str


class UnavailableCapture(Enum):
    UNAVAILABLE = "capture unavailable"


@dataclass(frozen=True)
class DeferredCaptureIntent:
    capture_key: str
    submitted_draft_id: str
    request: PageCaptureRequest | UnavailableCapture


class NoDeferredCapture(Enum):
    ABSENT = "no deferred capture"


type SubmissionCaptureIntent = DeferredCaptureIntent | NoDeferredCapture


class NoBoundCaptureTarget(Enum):
    UNBOUND = "no saved item"


@dataclass(frozen=True)
class PageViewport:
    width: int
    height: int
    dark: bool


@dataclass(frozen=True)
class PageScreenshot:
    png: bytes
    viewport: PageViewport

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.png).hexdigest()


@dataclass(frozen=True)
class FiledPageScreenshot:
    """The digest of a screenshot that Send already filed."""

    digest: str


@dataclass(frozen=True)
class NoPageScreenshot:
    pass


NO_PAGE_SCREENSHOT = NoPageScreenshot()
type SubmissionScreenshot = PageScreenshot | FiledPageScreenshot | NoPageScreenshot


@dataclass(frozen=True)
class PageScriptError:
    message: str
    source: str
    line: int
    column: int
    at: str


@dataclass(frozen=True)
class BugPageState:
    url: str
    viewport: PageViewport | None
    render_seconds: float | None
    notices: tuple[str, ...]
    script_errors: tuple[PageScriptError, ...]
    filters: str
    include_screenshot: bool
    title: str = ""


@dataclass(frozen=True)
class PastedImage:
    media_type: str
    base64_data: str


@dataclass(frozen=True)
class ImageReference:
    id: str
    file: str


class PageRecipient(StrEnum):
    PAGE_OWNER = "page-owner"
    LIST_OWNER = "list-owner"
    SELECTED = "selected"
    PRODUCER = "producer"


def is_list_submission_page(page: object) -> bool:
    """Recognize the Studio and requests from the former List page."""
    return page in ("studio", "list")


class DefaultSubmissionNotice(StrEnum):
    FILED_KIND_FROM_REVIEW = "filed-kind-from-review"


@dataclass(frozen=True)
class SubmissionNotice:
    kind: SubmissionKind | Literal["feedback", "efficiency", "enhancement"]
    from_command: bool = False


type Notice = SubmissionNotice | DefaultSubmissionNotice


@dataclass(frozen=True)
class ChosenAssignee:
    id: staff.StaffId


@dataclass(frozen=True)
class ProducerAssigns:
    """The producer chooses the assignee."""


type Assignment = ChosenAssignee | ProducerAssigns


def assignment_id(assignment: Assignment) -> staff.StaffId | None:
    """Write the assignee ID in the existing request format."""
    return assignment.id if isinstance(assignment, ChosenAssignee) else None


@dataclass(frozen=True)
class NewItemRequest:
    request_id: str
    kind: SubmissionKind | Literal["enhancement"]
    recipient_id: staff.StaffId
    words: str
    evidence: Evidence
    images: tuple[PastedImage, ...] = ()
    page: str = ""
    page_state: BugPageState | None = None
    page_recipient: PageRecipient = PageRecipient.PAGE_OWNER
    notice: Notice = DefaultSubmissionNotice.FILED_KIND_FROM_REVIEW
    assignment: Assignment = ProducerAssigns()


@dataclass(frozen=True)
class NoReviewSubmission:
    """The item came from its native filing path."""


@dataclass(frozen=True)
class ReviewSubmission:
    request_id: str
    request_digest: str
    evidence: Evidence
    images: tuple[ImageReference, ...]
    page: str = ""
    page_state: BugPageState | None = None
    page_state_data: PageDataEvidence | None = None
    capture_intent: SubmissionCaptureIntent = NoDeferredCapture.ABSENT
    notice: Notice = DefaultSubmissionNotice.FILED_KIND_FROM_REVIEW
    assignment: Assignment = ProducerAssigns()
    image_digests: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)

    reply_proof: reply_tokens.ReplyProof = field(default=reply_tokens.MISSING_REPLY_PROOF, kw_only=True, repr=False)
    reply_wait_revision: reply_proofs.ReplyWaitRevision = field(default=reply_proofs.INITIAL_REPLY_WAIT, kw_only=True)

    @property
    def reply_token(self) -> str:
        return reply_tokens.derive(self.reply_proof) if isinstance(self.reply_proof, reply_tokens.ReplyTokenProof) else ""

    @property
    def reply_token_sha256(self) -> str:
        return self.reply_proof.sha256 if not isinstance(self.reply_proof, reply_tokens.MissingReplyProof) else ""

    @property
    def reply_token_nonce(self) -> str:
        return self.reply_proof.nonce if isinstance(self.reply_proof, reply_tokens.ReplyTokenProof) else ""

    def with_reply_proof(self, proof: reply_tokens.ReplyProof) -> ReviewSubmission:
        return replace(self, reply_proof=proof)

    def proof_identity(self, number: str) -> reply_proofs.SubmissionIdentity:
        return reply_proofs.SubmissionIdentity(number, self.request_id, self.reply_wait_revision)

    def next_reply_wait(self, number: str, proof: reply_tokens.ReplyTokenProof) -> ReviewSubmission:
        """Save the next proof before the caller writes the next wait."""
        next_wait = replace(self, reply_proof=proof, reply_wait_revision=reply_proofs.ReplyWaitRevision(str(uuid4())))
        reply_proofs.stage_wait(next_wait.proof_identity(number), proof)
        return next_wait

    @property
    def bug_page_data(self) -> PageDataEvidence | None:
        if self.page_state is None:
            return None
        if self.page_state_data is not None:
            return self.page_state_data
        return self.evidence if isinstance(self.evidence, PageDataEvidence) else None


type SubmissionOrigin = ReviewSubmission | NoReviewSubmission

KEY_PREFIX = "submission/"
NATIVE_ID_PREFIX = {SubmissionKind.QUESTION: "Q", SubmissionKind.PROPOSAL: "P",
                    SubmissionKind.INVESTIGATION: "I", SubmissionKind.BUG: "I"}


def submission_key(kind: SubmissionKind, owner: staff.StaffId, identity: str, request_id: str) -> str:
    """Address the native record and preserve legacy keys during reads."""
    try:
        recipient = request_recipient(kind, request_id)
    except SubmissionError:
        recipient = owner
    return f"{KEY_PREFIX}{kind}/{recipient}/{identity}/{request_id}"


@dataclass(frozen=True)
class ScopedSubmissionKey:
    kind: SubmissionKind
    recipient_id: staff.StaffId
    item_id: str
    request_id: str


def check_recipient_id(recipient: staff.StaffId) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", recipient):
        raise SubmissionError("Invalid recipient ID.")


def _read_assignment(value: object) -> Assignment:
    if value is None:
        return ProducerAssigns()
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise SubmissionError("Invalid assignee ID.")
    return ChosenAssignee(staff.StaffId(value))


def request_recipient(kind: SubmissionKind, request_id: str) -> staff.StaffId:
    binding = re.fullmatch(r"r(?:[126]-[0-9a-f]{32}|[3-5]-[0-9a-f]{64})-(.+)", request_id)
    if binding is None:
        raise SubmissionError("Invalid request ID.")
    prefix = f"{kind}-"
    if not binding[1].startswith(prefix):
        raise SubmissionConflict("This request ID already has different item data.")
    recipient = staff.StaffId(binding[1][len(prefix):])
    check_recipient_id(recipient)
    return recipient


def require_request_binding(kind: SubmissionKind, recipient: staff.StaffId, request_id: str) -> None:
    if request_recipient(kind, request_id) != recipient:
        raise SubmissionConflict("This request ID already has different item data.")


def read_submission_key(key: str) -> ScopedSubmissionKey:
    parts = key.split("/")
    if len(parts) != 5 or parts[0] != KEY_PREFIX[:-1]:
        raise SubmissionError("Use the sent item's full key.")
    kind, recipient, identity = read_kind(parts[1]), staff.StaffId(parts[2]), parts[3]
    check_recipient_id(recipient)
    if not re.fullmatch(NATIVE_ID_PREFIX[kind] + r"[1-9][0-9]*", identity):
        raise SubmissionError("The item ID does not match its type.")
    request_id = read_request_id(parts[4])
    require_request_binding(kind, recipient, request_id)
    return ScopedSubmissionKey(kind, recipient, identity, request_id)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SubmissionError(f"{label} must be an object.")
    return cast(dict[str, object], value)


def _entries(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        entries = tuple(cast(tuple[object, ...] | list[object], value))
        if len(entries) <= maximum:
            return entries
    raise SubmissionError(f"{label} must be an array with up to {maximum} entries.")


def _text(value: object, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise SubmissionError(f"{label} must be text with at most {maximum} characters.")
    try:
        _ = value.encode("utf-8")
    except UnicodeError as error:
        raise SubmissionError(f"{label} must use valid Unicode.") from error
    return value


def _gib(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= cast(float, value) <= 1_000_000:
        raise SubmissionError("Memory must use a nonnegative GiB value.")
    return float(cast(float, value))


def _optional_gib(value: object) -> float | None:
    return None if value is None else _gib(value)


def _optional_memory_text(value: object) -> str | None:
    return None if value is None else _text(value, "Displayed memory", 32)


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise SubmissionError("The time must include a time zone.")
    return result


def read_kind(value: object) -> SubmissionKind:
    try:
        return SubmissionKind(value)
    except ValueError:
        raise SubmissionError("Select a valid item type.") from None


def _tip_entry(value: object) -> TipEntry:
    row = _mapping(value, "Tooltip entry")
    match row.get("kind"):
        case "caption":
            return TipCaption(_text(row.get("text"), "Tooltip caption", BUILD_EVIDENCE_LIMITS["text"]))
        case "field":
            return TipField(_text(row.get("label"), "Tooltip label", BUILD_EVIDENCE_LIMITS["label"]),
                            _text(row.get("value"), "Tooltip value", BUILD_EVIDENCE_LIMITS["text"], allow_empty=True))
        case "detail":
            return TipDetail(_text(row.get("text"), "Tooltip detail", BUILD_EVIDENCE_LIMITS["text"]))
        case _:
            raise SubmissionError("Select a valid tooltip entry type.")


def _page_cells(value: object) -> tuple[str, ...]:
    return tuple(_text(cell, "Page value", 240, allow_empty=True)
                 for cell in _entries(value, "Page cells", 16))


def read_evidence(value: object) -> Evidence:
    """Decode the structured evidence supplied by the dialog."""
    data = _mapping(value, "Evidence")
    match data.get("kind"):
        case "none":
            return NoEvidence()
        case "page-data":
            rows = tuple(_page_cells(row) for row in _entries(data.get("rows"), "Page rows", MAX_PAGE_ROWS))
            sections = tuple(
                PageDataSection(_text(section.get("label"), "Page section", 240), _page_cells(section.get("columns")),
                                tuple(_page_cells(row) for row in _entries(
                                    section.get("rows"), "Section rows", MAX_PAGE_SECTION_ROWS)))
                for value in _entries(data.get("sections", ()), "Page sections", MAX_PAGE_SECTIONS)
                for section in (_mapping(value, "Page section"),))
            evidence = PageDataEvidence(_text(data.get("title"), "Page title", 240), rows, sections)
            if sections and len(json.dumps(
                    evidence_fields(evidence), ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PAGE_DATA_BYTES:
                raise SubmissionError("The page data exceeds its size limit.")
            return evidence
        case "build-step":
            captured_at = _text(data.get("captured_at"), "Capture time", 80)
            _ = _time(captured_at)
            cells = tuple(
                BuildCell(_text(cell.get("label"), "Cell label", BUILD_EVIDENCE_LIMITS["label"]),
                          _text(cell.get("value"), "Cell value", BUILD_EVIDENCE_LIMITS["text"], allow_empty=True))
                for entry in _entries(data.get("cells"), "Row cells", 32)
                for cell in (_mapping(entry, "Row cell"),))
            memory = _mapping(data.get("memory"), "Memory reading")
            segments = tuple(
                MemorySegment(_text(segment.get("label"), "Memory label", BUILD_EVIDENCE_LIMITS["label"]),
                              _gib(segment.get("gib")), MemoryTone(segment.get("tone")),
                              _optional_memory_text(segment.get("text")))
                for entry in _entries(memory.get("segments"), "Memory segments", 32)
                for segment in (_mapping(entry, "Memory segment"),))
            return BuildStepEvidence(
                captured_at, _text(data.get("step_id"), "Step ID", BUILD_EVIDENCE_LIMITS["title"]),
                _text(data.get("title"), "Step title", BUILD_EVIDENCE_LIMITS["title"]),
                _text(data.get("owner"), "Step owner", BUILD_EVIDENCE_LIMITS["title"], allow_empty=True), cells,
                MemoryReading(_optional_gib(memory.get("free_for_next_gib", "")),
                              _optional_gib(memory.get("queue_cap_gib", "")),
                              segments, _optional_memory_text(memory.get("free_text")),
                              _optional_memory_text(memory.get("cap_text"))),
                _text(data.get("tip_title"), "Tooltip title", BUILD_EVIDENCE_LIMITS["title"]),
                _text(data.get("tip_status"), "Tooltip status", BUILD_EVIDENCE_LIMITS["title"], allow_empty=True),
                tuple(_tip_entry(entry) for entry in _entries(data.get("tip"), "Tooltip entries", BUILD_EVIDENCE_LIMITS["tipEntries"])))
        case _:
            raise SubmissionError("Select a valid evidence type.")


PAGE_CAPTURE_KEY = re.compile(r"(?:[0-9a-f]{32}|r[26]-[0-9a-f]{64})")


def read_capture_key(value: object) -> str:
    """Check a legacy key or a key that starts with its draft ID."""
    key = _text(value, "Capture key", 67)
    if not PAGE_CAPTURE_KEY.fullmatch(key):
        raise SubmissionError("Invalid capture key.")
    return key


def capture_key_matches_draft(key: str, draft: str) -> bool:
    """Check that the key starts with the ID of its draft."""
    return len(key) == 67 and key[:35] == draft and PAGE_CAPTURE_KEY.fullmatch(key) is not None


def read_capture_viewport(value: object) -> PageViewport:
    """Check the viewport dimensions for a screenshot."""
    data = _mapping(value, "Capture viewport")
    width, height, dark = data.get("width"), data.get("height"), data.get("dark")
    if (type(width) is not int or not 240 <= width <= 4096
            or type(height) is not int or not 200 <= height <= 2160 or not isinstance(dark, bool)):
        raise SubmissionError("The page size is outside the screenshot limits.")
    return PageViewport(width, height, dark)


def _capture_place(data: dict[str, object]) -> str:
    place = _text(data.get("place"), "Page address", 2048)
    if not place.startswith("/") or place.startswith("//") or re.search(r"[\\\x00-\x20\x7f]", place):
        raise SubmissionError("Use a console page address.")
    return place


def read_page_capture(value: object) -> PageCaptureRequest:
    """Check the source address and viewport of a screenshot."""
    data = _mapping(value, "Page capture")
    viewport = read_capture_viewport(data)
    place = _capture_place(data)
    return PageCaptureRequest(place, viewport.width, viewport.height, viewport.dark)


def read_live_page_capture(value: object) -> LivePageCapture:
    """Read a gzip snapshot within both size limits."""
    data = _mapping(value, "Live page capture")
    request = read_page_capture(data)
    encoded = data.get("document_gzip")
    if not isinstance(encoded, str) or not encoded:
        raise SubmissionError("Supply a compressed live page.")
    if len(encoded) > MAX_LIVE_CAPTURE_BASE64_LENGTH:
        raise SubmissionError("The live page exceeds the screenshot limit.")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) > MAX_LIVE_CAPTURE_BYTES:
            raise SubmissionError("The live page exceeds the screenshot limit.")
        with gzip.GzipFile(fileobj=BytesIO(compressed)) as stream:
            document = stream.read(MAX_LIVE_CAPTURE_DOCUMENT_BYTES + 1)
    except (binascii.Error, EOFError, OSError, zlib.error) as error:
        raise SubmissionError("Invalid compressed live page.") from error
    if len(document) > MAX_LIVE_CAPTURE_DOCUMENT_BYTES:
        raise SubmissionError("The live page exceeds the screenshot limit.")
    try:
        markup = document.decode("utf-8")
    except UnicodeError as error:
        raise SubmissionError("The live page must use valid Unicode.") from error
    if not markup.strip():
        raise SubmissionError("Supply a live page.")
    return LivePageCapture(request, markup)


def _page_integer(value: object, label: str, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        raise SubmissionError(f"Invalid {label}.")
    return value


def _script_error(value: object) -> PageScriptError:
    row = _mapping(value, "Script error")
    at = _text(row.get("at"), "Error time", 80)
    _ = _time(at)
    return PageScriptError(
        _text(row.get("message"), "Error message", 512),
        _text(row.get("source"), "Error source", 2048, allow_empty=True),
        _page_integer(row.get("line"), "error line", 0), _page_integer(row.get("column"), "error column", 0), at)


def read_bug_page_state(value: object) -> BugPageState | None:
    """Check the page state supplied by the Bug dialog."""
    if value is None:
        return None
    row = _mapping(value, "Page state")
    reported_url = row.get("url")
    url = reported_url[:MAX_PAGE_URL_LENGTH] if isinstance(reported_url, str) else ""
    url = url.encode("utf-8", errors="replace").decode("utf-8")
    viewport = None
    if row.get("viewport") is not None:
        display = _mapping(row["viewport"], "Page viewport")
        dark = display.get("dark")
        if not isinstance(dark, bool):
            raise SubmissionError("Invalid page display settings.")
        viewport = PageViewport(_page_integer(display.get("width"), "viewport width", 1),
                                _page_integer(display.get("height"), "viewport height", 1), dark)
    screenshot = row.get("include_screenshot", False)
    if not isinstance(screenshot, bool):
        raise SubmissionError("Invalid page display settings.")
    seconds = row.get("render_seconds")
    if seconds is not None and (
            type(seconds) not in (int, float) or not 0 <= cast(float, seconds) <= 1_000_000):
        raise SubmissionError("Invalid page render time.")
    return BugPageState(
        url, viewport,
        cast(float | None, seconds),
        tuple(_text(line, "Notice", 512) for line in _entries(row.get("notices"), "Notices", 48)),
        tuple(_script_error(error) for error in _entries(row.get("script_errors"), "Script errors", 20)),
        _text(row.get("filters", ""), "Page filters", 1024, allow_empty=True), screenshot,
        _text(row.get("title", ""), "Page title", 240, allow_empty=True))


def _read_notice(value: object) -> Notice:
    if value is None or value == DefaultSubmissionNotice.FILED_KIND_FROM_REVIEW:
        return DefaultSubmissionNotice.FILED_KIND_FROM_REVIEW
    fields = _mapping(value, "Submission notice")
    kind = fields.get("kind")
    from_command = fields.get("from_command", False)
    if not isinstance(from_command, bool):
        raise SubmissionError("Invalid submission notice source.")
    if isinstance(kind, str) and (kind == "feedback" or kind == "efficiency" or kind == "enhancement"):
        return SubmissionNotice(kind, from_command)
    return SubmissionNotice(read_kind(kind), from_command)


def read_request(value: object, *, has_capture: bool = False) -> NewItemRequest:
    """Decode the JSON fields for Send."""
    data = _mapping(value, "New item")
    page = _text(data.get("page", ""), "Console page", 32, allow_empty=True)
    kind_value = data.get("kind")
    kind = "enhancement" if kind_value == "enhancement" else read_kind(kind_value)
    notice = _read_notice(data.get("notice"))
    if isinstance(notice, SubmissionNotice):
        notice_kind = SubmissionKind.INVESTIGATION if notice.kind in ("feedback", "efficiency") else notice.kind
        if notice_kind != kind:
            raise SubmissionError("The submission notice does not match the item type.")
    if page and page not in page_frame.OWNER_PAGES:
        raise SubmissionError("Open the New item dialog from a page owner.")
    try:
        page_recipient = PageRecipient(data.get("page_recipient", PageRecipient.PAGE_OWNER))
    except ValueError as error:
        raise SubmissionError("Select a recipient from the New item dialog.") from error
    if not page and page_recipient != PageRecipient.PAGE_OWNER:
        raise SubmissionError("Select a recipient from the New item dialog.")
    assignment = _read_assignment(data.get("assignee_id"))
    if isinstance(assignment, ChosenAssignee) and page_recipient != PageRecipient.PRODUCER:
        raise SubmissionError("Select a producer for the assignee.")
    try:
        evidence = read_evidence(data.get("evidence"))
    except SubmissionError:
        if not (page and has_capture):
            raise
        evidence = NoEvidence()
    page_state = read_bug_page_state(data.get("page_state"))
    if page_state is not None and (
            not page or kind != SubmissionKind.BUG or
            not (isinstance(evidence, PageDataEvidence) or isinstance(evidence, NoEvidence) and page_state.title)):
        raise SubmissionError("Send page state from a console Bug dialog.")
    if page_state is not None and len(json.dumps(
            evidence_fields(evidence), ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PAGE_DATA_BYTES:
        raise SubmissionError("The Bug page data exceeds its size limit.")
    images = tuple(
        PastedImage(_text(image.get("media_type"), "Image type", 32),
                    _text(image.get("base64_data"), "Image data", max_base64_size()))
        for entry in _entries(data.get("images"), "Images", MAX_IMAGES)
        for image in (_mapping(entry, "Image"),))
    return NewItemRequest(
        read_request_id(data.get("request_id")), kind,
        staff.StaffId(_text(data.get("recipient_id"), "Recipient ID", 128)),
        _text(data.get("words"), "Your words", MAX_WORDS_LENGTH),
        evidence, images, page, page_state, page_recipient, notice, assignment)


def read_image_reference(value: object) -> ImageReference:
    row = _mapping(value, "Image reference")
    identity = _text(row.get("id"), "Image ID", 12)
    name = _text(row.get("file"), "Image file", 16)
    if not re.fullmatch(r"[0-9a-f]{12}", identity) or name not in {
        "image." + extension for extension in IMAGE_FORMATS.values()
    }:
        raise SubmissionError("Invalid image reference.")
    return ImageReference(identity, name)


def stored_submission(details: ReviewSubmission) -> dict[str, object]:
    """Write the submission fields inside the native record."""
    fields = cast(dict[str, object], asdict(details))
    fields["evidence"] = evidence_fields(details.evidence)
    _ = fields.pop("reply_proof")
    if details.reply_wait_revision == reply_proofs.INITIAL_REPLY_WAIT:
        _ = fields.pop("reply_wait_revision")
    if details.image_digests:
        fields["image_digests"] = dict(details.image_digests)
    else:
        _ = fields.pop("image_digests")
    if isinstance(details.notice, DefaultSubmissionNotice):
        _ = fields.pop("notice")
    _ = fields.pop("assignment")
    if isinstance(details.assignment, ChosenAssignee):
        fields["assignee_id"] = details.assignment.id
    if details.page_state is None:
        _ = fields.pop("page_state")
    if details.page_state_data is None:
        _ = fields.pop("page_state_data")
    else:
        fields["page_state_data"] = evidence_fields(details.page_state_data)
    if isinstance(details.capture_intent, DeferredCaptureIntent):
        intent = details.capture_intent
        fields["capture_intent"] = {
            "capture_key": intent.capture_key, "submitted_draft_id": intent.submitted_draft_id,
            **({"request": asdict(intent.request)} if isinstance(intent.request, PageCaptureRequest) else {}),
        }
    else:
        _ = fields.pop("capture_intent")
    return fields


def _read_capture_intent(value: object) -> DeferredCaptureIntent:
    row = _mapping(value, "Capture intent")
    key = read_capture_key(row.get("capture_key"))
    draft = read_request_id(row.get("submitted_draft_id"))
    if not capture_key_matches_draft(key, draft):
        raise SubmissionError("The screenshot belongs to another draft.")
    request = read_page_capture(row["request"]) if "request" in row else UnavailableCapture.UNAVAILABLE
    return DeferredCaptureIntent(key, draft, request)


def read_submission(value: object) -> ReviewSubmission:
    row = _mapping(value, "Review submission")
    images = tuple(read_image_reference(image) for image in _entries(row.get("images"), "Image references", MAX_IMAGES))
    image_ids = {image.id for image in images}
    image_digests: list[tuple[str, str]] = []
    for identity, digest in _mapping(row.get("image_digests", {}), "Image digests").items():
        if identity not in image_ids or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SubmissionError("Invalid saved image digest.")
        image_digests.append((identity, digest))
    page = _text(row.get("page", ""), "Console page", 32, allow_empty=True)
    if page and page not in page_frame.OWNER_PAGES:
        raise SubmissionError("Invalid page in Review submission.")
    data = read_evidence(row["page_state_data"]) if row.get("page_state_data") is not None else None
    if data is not None and not isinstance(data, PageDataEvidence):
        raise SubmissionError("Invalid page data in Review submission.")
    revision = row.get("reply_wait_revision", reply_proofs.INITIAL_REPLY_WAIT)
    if not isinstance(revision, str):
        raise SubmissionError("Invalid reply wait revision.")
    if revision:
        try:
            if str(UUID(revision)) != revision:
                raise ValueError
        except ValueError:
            raise SubmissionError("Invalid reply wait revision.") from None
    return ReviewSubmission(
        read_request_id(row.get("request_id")), _text(row.get("request_digest"), "Request digest", 64),
        read_evidence(row.get("evidence")), images,
        page, read_bug_page_state(row.get("page_state")), data,
        capture_intent=_read_capture_intent(row["capture_intent"]) if "capture_intent" in row else NoDeferredCapture.ABSENT,
        reply_wait_revision=reply_proofs.ReplyWaitRevision(revision),
        image_digests=tuple(image_digests),
        notice=_read_notice(row.get("notice")), assignment=_read_assignment(row.get("assignee_id")))


def decode_submission(value: str) -> SubmissionOrigin:
    return read_submission(cast(object, json.loads(value))) if value else NoReviewSubmission()


def matches_reply_token(details: ReviewSubmission, token: str) -> bool:
    proof = details.reply_proof
    return (not isinstance(proof, reply_tokens.MissingReplyProof)
            and reply_tokens.matches(proof.sha256, reply_tokens.RawReplyToken(token)))


def matches_reply_reference(details: ReviewSubmission, reference: str) -> bool:
    proof = details.reply_proof
    return (not isinstance(proof, reply_tokens.MissingReplyProof)
            and reply_tokens.matches_reference(proof.sha256, reply_tokens.read_reference(reference)))


def require_reply_token(details: SubmissionOrigin, token: str) -> None:
    if not isinstance(details, ReviewSubmission) or not matches_reply_token(details, token):
        raise SubmissionError("The reply token does not match this item.")


def max_base64_size() -> int:
    return 4 * math.ceil(MAX_IMAGE_BYTES / 3)


def read_request_id(value: object) -> str:
    identity = _text(value, "Request ID", MAX_REQUEST_ID_LENGTH)
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", identity):
        raise SubmissionError("Invalid request ID.")
    return identity


def read_words(value: object, label: str, *, kind: SubmissionKind | Literal["enhancement"] | None = None) -> str:
    """Check the user's words before adding the Enhancement prefix."""
    words = _text(value, label, MAX_WORDS_LENGTH)
    return f"Enhancement: {words.lstrip()}" if kind == "enhancement" else words


def optional_feedback(words: str) -> str:
    """Apply the Tell limit and trim the outside of the words."""
    words = words.strip()
    return _text(words, "Your words", 10000) if words else ""


ACTION_WORDS_HEADING = "\n\nNate's words:\n"


def append_feedback(message: str, words: str) -> str:
    """Add the words from the box to an action message."""
    return message + ACTION_WORDS_HEADING + words if words.strip() else message


SCHEDULED_KIND = "scheduled"
SCHEDULED_DRAFT_ID = re.compile(r"s1-[0-9a-f]{32}")


class ScheduledDraftRequired(SubmissionConflict):
    def __init__(self) -> None:
        super().__init__("This draft ID is invalid for Scheduled. Start a new Scheduled draft.")


@dataclass(frozen=True)
class ScheduledDraftId:
    """A draft ID reserved for a Scheduled Send."""

    value: str

    def __post_init__(self) -> None:
        if not SCHEDULED_DRAFT_ID.fullmatch(self.value):
            raise ScheduledDraftRequired()


def read_scheduled_draft_id(value: object) -> ScheduledDraftId:
    if not isinstance(value, str):
        raise ScheduledDraftRequired()
    return ScheduledDraftId(value)


@dataclass(frozen=True)
class ScheduledSubmission:
    request_id: ScheduledDraftId
    recipient_id: staff.StaffId
    words: str
    page: str
    producer_route: bool = False
    assignment: Assignment = ProducerAssigns()


def read_scheduled_request(fields: dict[str, object]) -> ScheduledSubmission:
    """Read the Scheduled Send fields."""
    page = fields.get("page")
    route = fields.get("page_recipient")
    if (not isinstance(page, str) or page not in page_frame.OWNER_PAGES
            or route not in (PageRecipient.SELECTED, PageRecipient.PRODUCER)):
        raise SubmissionError("Select a recipient from the New item dialog.")
    allowed = {"kind", "page", "page_recipient", "request_id", "recipient_id", "token", "words"}
    producer_route = route == PageRecipient.PRODUCER
    if producer_route:
        allowed.add("assignee_id")
    if set(fields) - allowed:
        raise SubmissionError("Send the instruction, start, and repeat for a Scheduled item.")
    recipient = staff.StaffId(_text(fields.get("recipient_id"), "Recipient ID", 128))
    check_recipient_id(recipient)
    return ScheduledSubmission(read_scheduled_draft_id(fields.get("request_id")), recipient,
                               read_words(fields.get("words"), "Your words"), page, producer_route,
                               _read_assignment(fields.get("assignee_id")))


_SPARKLES = """<!-- @license lucide-static v0.468.0 - ISC -->
<svg aria-hidden="true" focusable="false" class="lucide lucide-sparkles"
    xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
  <path d="M20 3v4" />
  <path d="M22 5h-4" />
  <path d="M4 17v2" />
  <path d="M5 18H3" />
</svg>"""

KINDS = ((SubmissionKind.BUG, "Bug", "bug"),
         (SubmissionKind.QUESTION, "Question", "circle-help"),
         ("feedback", "Feedback", "message-circle-question"),
         (SubmissionKind.PROPOSAL, "Proposal", "lamp"),
         (SubmissionKind.INVESTIGATION, "Investigation", "microscope"),
         ("enhancement", "Enhancement", "sparkles"),
         (SCHEDULED_KIND, "Scheduled", "clock-alert"))


def render() -> str:
    """Render the dialog for the page owner and Builds row buttons."""
    chips = "".join(
        f'<label class="new-item-chip" data-kind="{kind}"><input type="radio" name="kind" value="{kind}">'
        + f'<span data-button-face>{_SPARKLES if icon == "sparkles" else page_frame.icon_svg(icon)}'
        + f'<span data-kind-label>{label}</span></span></label>'
        for kind, label, icon in KINDS)
    limits = json.dumps({"types": list(IMAGE_FORMATS), "images": MAX_IMAGES,
                         "imageBytes": MAX_IMAGE_BYTES, "captureBytes": MAX_LIVE_CAPTURE_BYTES,
                         "captureDocumentBytes": MAX_LIVE_CAPTURE_DOCUMENT_BYTES,
                         "totalImageBytes": MAX_TOTAL_IMAGE_BYTES, "pageDataBytes": MAX_PAGE_DATA_BYTES,
                         "pageRows": MAX_PAGE_ROWS, "pageSections": MAX_PAGE_SECTIONS,
                         "sectionRows": MAX_PAGE_SECTION_ROWS,
                         "pageUrlLength": MAX_PAGE_URL_LENGTH, "buildEvidence": BUILD_EVIDENCE_LIMITS})
    body = f'''
   <fieldset><legend>Type</legend><div class="new-item-chips">{chips}</div></fieldset>
   <fieldset data-recipient-group><legend>To</legend>
    <p data-owner-reason hidden><span id="review-new-item-owner-reason"></span>&nbsp;<span aria-hidden="true">&#x1F986;</span></p>
    <p data-recipients-loading role="status" hidden>Loading recipients…</p>
    <p data-recipients-status role="status" hidden></p>
    <div class="new-item-chips" data-recipients></div>
   </fieldset>
   <section class="new-item-route" data-route aria-labelledby="new-item-route-title" hidden>
    <h3 id="new-item-route-title">Where it goes</h3>
    <div class="page-state-box">
     <div class="route-steps">
      <div><strong>You</strong></div>{_ROUTE_ARROW}
      <div><strong data-route-producer></strong><small>tracks it</small></div>{_ROUTE_ARROW}
      <div>
       <div class="route-assignment"><strong data-route-assignee></strong>
        <button type="button" data-route-change aria-expanded="false" hidden>Change {page_frame.icon_svg("chevron-down")}</button>
        <fieldset class="route-assignees" id="new-item-assignees" data-assignees aria-label="Assignee" tabindex="-1" hidden>
         <div data-assignee-menu>
          <button type="button" class="route-assignee-option" data-other-assignees>Other studio personnel</button>
         </div>
         <div class="new-item-chips" data-assignee-personnel role="group" aria-label="Other studio personnel" hidden></div>
        </fieldset>
       </div>
       <small data-route-assigned hidden>assigned</small>
      </div>
     </div>
     <p data-route-copy aria-live="polite"></p>
    </div>
   </section>
   <textarea name="words" rows="4" maxlength="{MAX_WORDS_LENGTH}" aria-label="Your words"
    placeholder="Type your words. You can paste an image." required></textarea>
   <p data-scheduled-help hidden>Enter the instruction. Then add <code>Start Mon Nov 30 09:00.</code>
    You can add <code>Repeat every 2 weeks on Monday and Thursday.</code></p>
   <section class="page-state-box sent-details" data-sent-details aria-labelledby="sent-details-title">
    <h4 id="sent-details-title">Sent with your words</h4>
    <div class="new-item-evidence" data-evidence hidden></div>
    <div data-bug-page-state hidden>
     <dl class="page-state"></dl>
     <p data-visible-text>Page data and visible text</p>
    </div>
    <label class="new-item-page-capture">
     <input type="checkbox" name="include_page" checked><span>Include a screenshot</span>
    </label>
    <div class="capture-slot" data-capture-slot hidden>
     <a data-shot target="_blank" rel="noopener" hidden>
      <img alt="Screenshot of this page">
      <span>Open full size ↗</span>
     </a>
     <div data-taking role="status"><span class="capture-spinner" aria-hidden="true"></span>Taking a screenshot. You can send now. The screenshot joins the saved item when ready.</div>
    </div>
    <p data-capture-expired role="status" hidden>The screenshot expired.</p>
    <div data-replace-capture hidden>
     <button type="button" data-renew-capture>Renew screenshot</button>
     <button type="button" data-omit-capture>Omit screenshot</button>
    </div>
    <p data-fallback role="status" hidden>Screenshot unavailable. Page data and visible text go instead.</p>
    <div class="new-item-previews" data-previews hidden></div>
   </section>
'''
    footer = '''
  <footer>
   <p data-send-disabled role="status" hidden></p>
   <p class="new-item-error" role="alert" hidden></p>
   <p id="new-item-saved" data-saved-confirmation role="status" hidden></p>
   <div data-send-controls>
    <span id="new-item-producer-hint" data-producer-hint hidden>Pick a producer</span>
    <button type="button" data-later-draft hidden>Edit a separate draft</button>
    <button type="button" data-cancel>Cancel</button><button type="submit" disabled>Send</button>
   </div>
   <div data-saved-controls hidden>
    <button type="button" data-done aria-describedby="new-item-saved">Done</button>
   </div>
  </footer>
'''
    dialog = page_frame.console_dialog('review-new-item', 'new-item-title', 'New item', body,
                                       footer=footer, form=True, body_class='new-item-body')
    return (f'<style>{STYLE}{PAGE_STATE_STYLE}</style>{dialog}'
            f'<script>{page_frame.HELD_SEND_SCRIPT}{SCRIPT.replace("__LIMITS__", limits)}</script>')


_ROUTE_ARROW = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                '<path d="M5 12h14m-7-7 7 7-7 7"/></svg>')


PAGE_STATE_STYLE = '''
.page-state-box { background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
 padding: 12px 14px; min-width: 0; overflow-wrap: anywhere; }
.page-state-box h4 { margin: 0 0 8px; font-size: 14px; font-weight: 650; }
.page-state { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 5px 16px;
 margin: 0; font-size: 13px; }
.page-state dt { color: var(--ink-3); }
.page-state dd { margin: 0; color: var(--ink); white-space: pre-line; }
.page-state .error-line { color: var(--error); }
.page-state-box figure { margin: 12px 0 0; }
.page-state-box figure img { display: block; max-width: min(320px, 100%); height: auto;
 max-height: 180px; border: 1px solid var(--line); border-radius: 6px; object-fit: contain; }
.page-state-box figcaption { margin-top: 4px; font-size: 12px; color: var(--ink-3); }
'''


STYLE = '''
/* Keep the page at the same scroll position when a save adds a row. */
:root:has(:is(#review-new-item,[data-new-item-draft])[open]) { overflow-anchor: none; }
:is(#review-new-item,[data-new-item-draft]) button:where(:not([data-close])) { margin: 0; min-height: 0; font: inherit; cursor: pointer; }
:is(#review-new-item,[data-new-item-draft]) button:disabled { opacity: .55; cursor: default; }
:is(#review-new-item,[data-new-item-draft]) button:focus-visible, :is(#review-new-item,[data-new-item-draft]) textarea:focus-visible,
:is(#review-new-item,[data-new-item-draft]) input:focus-visible + span { outline: 2px solid var(--accent, #3987e5); outline-offset: 2px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-body {
 display: grid; align-content: start; gap: 18px;
}
:is(#review-new-item,[data-new-item-draft]) fieldset { min-inline-size: 0; margin: 0; padding: 0; border: 0; }
:is(#review-new-item,[data-new-item-draft]) legend, :is(#review-new-item,[data-new-item-draft]) h3 {
 margin: 0 0 8px; padding: 0; font-size: 11px; line-height: 1.3; font-weight: 500;
 text-transform: uppercase; color: var(--ink-3, #9a988f);
}
:is(#review-new-item,[data-new-item-draft]) .new-item-chips { display: flex; flex-wrap: wrap; gap: 8px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip { position: relative; display: inline-flex; max-width: 100%; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[hidden] { display: none; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip input {
 position: absolute; width: 1px; height: 1px; padding: 0; margin: 0; opacity: 0;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-chip > span {
 display: flex; align-items: center; gap: 7px; padding: 7px 14px; border-radius: 999px;
 border: 1px solid var(--line, #34332f); color: var(--ink-2, #c3c2b7);
 font-size: 14px; overflow-wrap: anywhere; cursor: pointer;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-chip input:checked + span,
:is(#review-new-item,[data-new-item-draft]) button[type="submit"] {
 background: #2670c5; color: white; border-color: transparent;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-chip svg { width: 16px; height: 16px; flex: none; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[data-kind="question"] { --item-icon: #af90ed; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[data-kind="proposal"] { --item-icon: #ed7da9; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[data-kind="investigation"] { --item-icon: #1cc1b5; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[data-kind="bug"] { --item-icon: #ed876b; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[data-kind="enhancement"] { --item-icon: #efbd63; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip[data-kind="scheduled"] { --item-icon: #4fd1d9; }
:is(#review-new-item,[data-new-item-draft]) [data-scheduled-help] { margin: 0; color: var(--ink-2); }
:is(#review-new-item,[data-new-item-draft]).scheduled-locked .new-item-chip:has(input:disabled:not(:checked)) > span { opacity: .55; cursor: default; }
:is(#review-new-item,[data-new-item-draft]) .new-item-chip input:not(:checked) + span svg { color: var(--item-icon); }
:is(#review-new-item,[data-new-item-draft]) :is([data-recipients-status], [data-recipients-loading]) {
 margin: 0; color: var(--ink-2, #c3c2b7);
}
:is(#review-new-item,[data-new-item-draft]) [data-recipient-group] { display: flow-root; }
:is(#review-new-item,[data-new-item-draft]) [data-recipient-group] > legend { float: left; margin-right: 12px; }
:is(#review-new-item,[data-new-item-draft]) [data-owner-reason] {
 margin: 0 0 8px; font-size: 12px; line-height: 1.2; text-align: right; text-wrap: balance; overflow-wrap: anywhere;
 color: var(--ink-3, #9a988f);
}
:is(#review-new-item,[data-new-item-draft]) :is([data-recipients-status], [data-recipients-loading], [data-recipients]) {
 clear: both;
}
.new-item-route .route-steps {
 display: grid; gap: 8px;
 grid-template-columns: max-content minmax(16px, 1fr) minmax(0, 1.45fr) minmax(16px, 1fr) minmax(0, 2.8fr);
}
.new-item-route .route-steps strong { font-weight: 650; }
.new-item-route .route-steps small:not([hidden]) { display: block; color: var(--ink-3); font-size: 12px; }
.new-item-route .route-steps > svg { justify-self: center; width: 16px; height: 16px; margin-top: 3px; color: var(--ink-3); }
.new-item-route .route-assignment { position: relative; display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 9px; }
:is(#review-new-item,[data-new-item-draft]) [data-route-change]:not([hidden]) {
 display: inline-flex; align-items: center; gap: 5px; padding: 0; border: 0; background: none;
 color: var(--accent); font-size: 13px; text-decoration: underline; text-underline-offset: 2px;
}
.new-item-route [data-route-change] svg { width: 14px; height: 14px; transform: rotate(-90deg); }
.new-item-route [data-route-change][aria-expanded="true"] svg { transform: none; }
.new-item-route [data-route-copy] { margin: 10px 0 0; font-size: 14px; color: var(--ink-2); }
:is(#review-new-item,[data-new-item-draft]) .route-assignees {
 position: absolute; z-index: 1; inset-inline-end: 0; inset-block-start: calc(100% + var(--assignee-gap, 6px));
 inline-size: min(14rem, 65vw);
 overflow: auto; overscroll-behavior: contain;
 padding: 4px; border: 1px solid var(--line); border-radius: 8px;
 background: var(--card); box-shadow: 0 8px 24px #0005;
}
:is(#review-new-item,[data-new-item-draft]) .route-assignees[data-above] {
 inset-block-start: auto; inset-block-end: calc(100% + var(--assignee-gap, 6px));
}
.new-item-route .route-assignee-option {
 display: flex; align-items: center; gap: 10px; min-height: 36px; padding: 6px 8px;
 border-radius: 4px; cursor: pointer; font-size: 14px; overflow-wrap: anywhere;
}
:is(#review-new-item,[data-new-item-draft]) [data-other-assignees] {
 width: 100%; min-height: 36px; padding: 6px 8px; border: 0; background: none;
 color: var(--ink); font-size: 14px; text-align: start;
}
.new-item-route .route-assignee-option:is(:hover, :focus-within) { background: var(--bg); }
:is(#review-new-item,[data-new-item-draft]) [data-other-assignees]:focus-visible { outline-offset: -2px; }
:is(#review-new-item,[data-new-item-draft]) .route-assignees:has([data-assignee-personnel]:not([hidden])) {
 inline-size: min(20rem, 65vw); padding: 8px;
}
:is(#review-new-item,[data-new-item-draft]) [data-assignee-personnel][hidden] { display: none; }
:is(#review-new-item,[data-new-item-draft]) [data-assignee-personnel] .new-item-chip {
 border: 1px solid var(--line); border-radius: 999px; padding: 7px 14px;
 background: transparent; color: var(--ink-2); font-size: 14px; overflow-wrap: anywhere;
}
:is(#review-new-item,[data-new-item-draft]) [data-assignee-personnel] .new-item-chip[aria-pressed="true"] {
 background: #2670c5; color: white; border-color: transparent;
}
.new-item-route .route-assignee-option:has(input:focus-visible) {
 outline: 2px solid var(--accent); outline-offset: -2px;
}
:is(#review-new-item,[data-new-item-draft]) .route-assignee-option :is(input:focus-visible, input:focus-visible + span) {
 outline: none;
}
.new-item-route .route-assignee-option input { flex: none; margin: 0; accent-color: var(--accent); }
.new-item-route .route-assignee-option span { min-width: 0; }
:is(#review-new-item,[data-new-item-draft]) [data-producer-hint] {
 align-self: center; margin-right: 4px; color: var(--ink-2); font-size: 13px;
}
:is(#review-new-item,[data-new-item-draft]) textarea {
 display: block; width: 100%; min-width: 0; margin: 0; padding: 10px 12px; resize: vertical;
 border: 1px solid var(--line, #34332f); border-radius: 8px;
 background: var(--bg, #121211); color: inherit; font: 16px/1.45 system-ui, sans-serif;
}
:is(#review-new-item,[data-new-item-draft]) textarea::placeholder { color: var(--ink-3, #9a988f); }
:is(#review-new-item,[data-new-item-draft])[data-page-bug] textarea { resize: none; }
:is(#review-new-item,[data-new-item-draft]) .new-item-page-capture:not([hidden]) { display: flex; align-items: center; gap: 9px; margin: 0; }
:is(#review-new-item,[data-new-item-draft]) .new-item-page-capture input { margin: 0; width: 16px; height: 16px; accent-color: var(--accent); }
:is(#review-new-item,[data-new-item-draft]) h2:focus { outline: none; }
:is(#review-new-item,[data-new-item-draft]) .sent-details h4 { margin: 0 0 11px; }
:is(#review-new-item,[data-new-item-draft]) [data-bug-page-state] { margin: 0 0 12px; padding: 0 0 12px; border-bottom: 1px solid var(--line); }
:is(#review-new-item,[data-new-item-draft]) [data-bug-page-state] dd { min-width: 0; overflow-wrap: anywhere; }
:is(#review-new-item,[data-new-item-draft]) .sent-details p { margin: 8px 0 0; font-size: 13px; color: var(--ink-2); }
:is(#review-new-item,[data-new-item-draft]) .sent-details .new-item-page-capture { font-size: 14px; }
:is(#review-new-item,[data-new-item-draft]) .sent-details .new-item-previews { margin-top: 10px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-evidence {
 background: #141413; color: #f0efec; border: 1px solid #34332f; border-radius: 8px;
 padding: 10px; font-size: 12px; min-width: 0; overflow-wrap: anywhere;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-evidence h4 { font-size: inherit; margin: 0 0 4px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-evidence-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0 8px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-muted { color: #c3c2b7; }
:is(#review-new-item,[data-new-item-draft]) .new-item-cells {
 display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 10px; margin: 7px 0 10px;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-cells div { min-width: 0; max-width: 100%; }
:is(#review-new-item,[data-new-item-draft]) .new-item-cells dt { font-weight: 600; color: #c3c2b7; }
:is(#review-new-item,[data-new-item-draft]) .new-item-cells dd, :is(#review-new-item,[data-new-item-draft]) .new-item-tip dd { margin: 0; white-space: pre-wrap; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory { padding: 8px 0; border-block: 1px solid #34332f; margin-bottom: 8px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory p { margin: 0; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory strong { font-size: 16px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-cap { color: #ef7372; font-weight: 650; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory-bar { display: flex; height: 12px; margin: 6px 0; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory-bar span { min-width: 0; background: var(--segment); border-radius: 2px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory-legend { display: flex; flex-wrap: wrap; gap: 3px 12px; color: #c3c2b7; }
:is(#review-new-item,[data-new-item-draft]) .new-item-memory-legend span::before {
 content: ''; display: inline-block; width: 7px; height: 7px; margin-right: 4px;
 border-radius: 2px; background: var(--segment);
}
:is(#review-new-item,[data-new-item-draft]) [data-tone="build"] { --segment: #3987e5; }
:is(#review-new-item,[data-new-item-draft]) [data-tone="free"] { --segment: #199e70; }
:is(#review-new-item,[data-new-item-draft]) [data-tone="tools"] { --segment: #c98500; }
:is(#review-new-item,[data-new-item-draft]) [data-tone="system"] { --segment: #9585e0; }
:is(#review-new-item,[data-new-item-draft]) [data-tone="other"] { --segment: #6c6b66; }
:is(#review-new-item,[data-new-item-draft]) .new-item-tip {
 background: #242422; color: #f0efec; border: 1px solid #34332f; border-radius: 8px;
 padding: 10px 12px; font-size: .85rem; line-height: 1.45;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-tip dl { margin: 0; }
:is(#review-new-item,[data-new-item-draft]) .new-item-tip dl > div {
 display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr); gap: 12px;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-tip dd { text-align: right; font-variant-numeric: tabular-nums; }
:is(#review-new-item,[data-new-item-draft]) .new-item-tip p { margin: 3px 0; color: #c3c2b7; white-space: pre-wrap; }
:is(#review-new-item,[data-new-item-draft]) .new-item-tip h4 { font-weight: 700; }
:is(#review-new-item,[data-new-item-draft]) .capture-slot {
 position: relative; width: min(320px, 100%); aspect-ratio: 1440 / 1000; margin-top: 10px;
 overflow: hidden; background: var(--card); border-radius: 7px; border: 1px solid var(--line);
}
:is(#review-new-item,[data-new-item-draft]) .capture-slot a, :is(#review-new-item,[data-new-item-draft]) [data-taking] { position: absolute; inset: 0; }
:is(#review-new-item,[data-new-item-draft]) .capture-slot img { display: block; width: 100%; height: 100%; object-fit: contain; }
:is(#review-new-item,[data-new-item-draft]) .capture-slot a span {
 position: absolute; right: 8px; bottom: 8px; padding: 4px 8px; font-size: 12px;
 border: 1px solid var(--line); border-radius: 5px; background: var(--bg); color: var(--ink);
}
:is(#review-new-item,[data-new-item-draft]) .capture-slot a:is(:hover, :focus-visible) span { border-color: var(--accent); }
:is(#review-new-item,[data-new-item-draft]) [data-taking]:not([hidden]) {
 display: flex; justify-content: center; align-items: center; gap: 9px; color: var(--ink-2); font-size: 14px;
}
:is(#review-new-item,[data-new-item-draft]) .capture-spinner {
 width: 14px; height: 14px; border: 2px solid var(--line);
 border-top-color: var(--accent); border-right-color: var(--accent); border-radius: 50%;
}
:is(#review-new-item,[data-new-item-draft]) .new-item-previews:not([hidden]) { display: flex; flex-wrap: wrap; gap: 8px; }
:is(#review-new-item,[data-new-item-draft]) .new-item-preview {
 position: relative; border: 1px solid var(--line, #34332f); border-radius: 7px;
 max-width: 100%; background: var(--bg, #121211);
}
:is(#review-new-item,[data-new-item-draft]) .new-item-preview img { display: block; width: 145px; height: 80px; object-fit: contain; }
:is(#review-new-item,[data-new-item-draft]) .new-item-preview button {
 position: absolute; top: 3px; right: 3px; width: 28px; height: 28px; padding: 0;
 border: 1px solid #6c6b66; border-radius: 5px; background: #1a1a19; color: #f0efec;
}
:is(#review-new-item,[data-new-item-draft]) footer { padding-block: 0 20px; }
:is(#review-new-item,[data-new-item-draft]) footer > div:not([hidden]) {
 display: flex; justify-content: flex-end; gap: 8px; border-top: 1px solid var(--line, #34332f);
 padding-top: 14px;
}
:is(#review-new-item,[data-new-item-draft]) [data-replace-capture]:not([hidden]) { display: flex; gap: 8px; flex-wrap: wrap; }
:is(#review-new-item,[data-new-item-draft]) [data-send-controls] { flex-wrap: wrap; }
:is(#review-new-item,[data-new-item-draft]) footer button, :is(#review-new-item,[data-new-item-draft]) [data-replace-capture] button {
 min-height: 38px; padding: 6px 14px; border: 1px solid var(--ink-3, #9a988f); border-radius: 8px;
 background: transparent; color: inherit;
}
:is(#review-new-item,[data-new-item-draft]) [data-send-disabled] { margin: 0 0 8px; color: var(--ink-2); }
:is(#review-new-item,[data-new-item-draft]) .new-item-error { color: var(--error, #ef7372); margin: 0 0 8px; overflow-wrap: anywhere; }
:is(#review-new-item,[data-new-item-draft]) [data-saved-confirmation] { margin: 0 0 12px; white-space: pre-line; }
@media (max-width: 480px) {
 :is(#review-new-item,[data-new-item-draft]) footer { padding-bottom: 14px; }
 :is(#review-new-item,[data-new-item-draft]) .new-item-body { gap: 14px; }
}
'''


SCRIPT = page_frame.ACTION_STATUS_SCRIPT + r'''(() => {
 const endpoint = '/console/review/new-item';
 const limits = __LIMITS__;
 const original = document.querySelector('#review-new-item');
 const template = original.cloneNode(true);
 const drafts = [];
 const listSendKey = 'console.list.pending-send';
 const listDraftPrefix = listSendKey + '.';
 const listStatePrefix = listDraftPrefix + 'state.';
 const browserDraftId = /^(?:r6|s1)-[0-9a-f]{32}$/;
 const submittedKey = 'console.new-item.submitted';
 const submittedPrefix = submittedKey + '.';
 const reportDraftPrefix = source => 'console.report.pending-send.' + source + '.';
 const isReportKind = kind => kind === 'bug' || kind === 'enhancement';
 let nextDraft = 0;

 function storedListDraft() {
  try {
   const legacy = sessionStorage.getItem(listSendKey);
   if (legacy !== null) {
    try {
     const draft = browserDraftId.test(legacy) ? {id: legacy, recipient: null} : JSON.parse(legacy);
     if (typeof draft?.id === 'string' && browserDraftId.test(draft.id)) {
      const key = listDraftPrefix + draft.id;
      if (sessionStorage.getItem(key) === null) {
       const recipient = draft.assignee ? JSON.stringify({recipient: draft.recipient || null, assignee: draft.assignee})
                                        : draft.recipient || '';
       sessionStorage.setItem(key, recipient);
       if (typeof draft.awaitingSend === 'boolean')
        sessionStorage.setItem(listStatePrefix + draft.id, String(draft.awaitingSend));
      }
     }
    } catch (_) {}
    sessionStorage.removeItem(listSendKey);
   }
   for (let index = 0; index < sessionStorage.length; index++) {
    const key = sessionStorage.key(index);
    if (!key.startsWith(listDraftPrefix)) continue;
    const id = key.slice(listDraftPrefix.length);
    if (!browserDraftId.test(id) || drafts.some(draft => draft.id === id)) continue;
    const state = sessionStorage.getItem(listStatePrefix + id);
    const stored = sessionStorage.getItem(key) || '';
    const route = stored.startsWith('{') ? JSON.parse(stored) : {recipient: stored || null};
    return {id, ...route, awaitingSend: state === null ? undefined : state === 'true'};
   }
  } catch (_) {}
  return null;
 }

 function storedReportDraft(source) {
  try {
   const prefix = reportDraftPrefix(source);
   for (let index = 0; index < sessionStorage.length; index++) {
    const key = sessionStorage.key(index);
    if (!key?.startsWith(prefix)) continue;
    const id = key.slice(prefix.length);
    if (!id.startsWith('r6-') || !browserDraftId.test(id) || drafts.some(draft => draft.id === id)) continue;
    const item = JSON.parse(sessionStorage.getItem(key));
    if (isReportKind(item?.kind)) return {id, kind: item.kind, recipient: item.recipient || null};
   }
  } catch (_) {}
  return null;
 }

function bindDraft(dialog, fresh = false) {
 const heading = dialog.querySelector('h2');
 heading.tabIndex = -1;
 const form = dialog.querySelector('form'), body = dialog.querySelector('.new-item-body');
 const recipientChips = dialog.querySelector('[data-recipients]');
 const recipientStatus = dialog.querySelector('[data-recipients-status]');
 const recipientLoading = dialog.querySelector('[data-recipients-loading]');
 const recipientGroup = dialog.querySelector('[data-recipient-group]');
 const ownerReason = dialog.querySelector('[data-owner-reason]');
 const ownerReasonText = ownerReason.querySelector('span');
 const route = dialog.querySelector('[data-route]'), changeAssignee = dialog.querySelector('[data-route-change]');
 const routeProducer = route.querySelector('[data-route-producer]'), routeAssignee = route.querySelector('[data-route-assignee]');
 const routeAssigned = route.querySelector('[data-route-assigned]'), routeCopy = route.querySelector('[data-route-copy]');
 const assigneeGroup = route.querySelector('[data-assignees]');
 const assigneeMenu = assigneeGroup.querySelector('[data-assignee-menu]');
 const otherAssignees = assigneeMenu.querySelector('[data-other-assignees]');
 const personnel = assigneeGroup.querySelector('[data-assignee-personnel]');
 const assignment = changeAssignee.parentElement;
 const assigneeMenuResize = new ResizeObserver(placeAssigneeChoices);
 changeAssignee.setAttribute('aria-controls', assigneeGroup.id);
 const words = form.elements.words, previews = dialog.querySelector('[data-previews]');
 const wordsPlaceholder = words.placeholder;
 const includePage = form.elements.include_page, sentDetails = dialog.querySelector('[data-sent-details]');
 const bugStateBox = dialog.querySelector('[data-bug-page-state]');
 const evidenceBox = sentDetails.querySelector('[data-evidence]');
 const slot = dialog.querySelector('[data-capture-slot]'), shot = slot.querySelector('[data-shot]');
 const preview = shot.querySelector('img'), taking = slot.querySelector('[data-taking]');
 const fallback = sentDetails.querySelector('[data-fallback]');
 const fallbackText = fallback.textContent;
 const expired = sentDetails.querySelector('[data-capture-expired]');
 const replaceCapture = sentDetails.querySelector('[data-replace-capture]');
 const renewButton = replaceCapture.querySelector('[data-renew-capture]');
 const omitButton = replaceCapture.querySelector('[data-omit-capture]');
 const laterButton = dialog.querySelector('[data-later-draft]');
 const send = dialog.querySelector('[type="submit"]'), error = dialog.querySelector('.new-item-error');
 const scheduledHelp = dialog.querySelector('[data-scheduled-help]');
 const kindInputs = [...form.querySelectorAll('input[name="kind"]')];
 const sendNotice = dialog.querySelector('[data-send-disabled]');
 const confirmation = dialog.querySelector('[data-saved-confirmation]'), done = dialog.querySelector('[data-done]');
 const closeButton = dialog.querySelector('[data-close]');
 const sendControls = dialog.querySelector('[data-send-controls]'), savedControls = done.parentElement;
 const producerHint = dialog.querySelector('[data-producer-hint]');
 const fields = [...form.querySelectorAll('fieldset'), changeAssignee, words, includePage, closeButton,
                 dialog.querySelector('[data-cancel]')];
 let launcher, opening, token, recipients, images = [], sending = false;
 let draftId, keepDraft = false, ownerPage = '', launcherPage = '';
 const isListPage = () => ownerPage === 'studio' || ownerPage === 'list';
 // Null means the user must choose again. An empty string means the page has no default.
 let selectedRecipient = '';
 let pageOwner = '';
 let assignees, selectedAssignee = null, pageAssignee = '';
 let captureSession, pageContext, savedItem, laterDraft, laterBase;
 let awaitingEarlierSend = false, awaitingNativeRecovery = false, submittedDraft;
 let separateFromNativeDraft = false, nativeListEdit, separateListEdit;
 let draftDatabase, submittedStorageKey;
 let pageCapture = {status: 'unavailable'};

 let source = '', evidence = {kind: 'none'}, tipPreview = null;

 function node(tag, text = '', css = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (css) element.className = css;
  return element;
 }

 function captionAndValue(label, value) {
  const row = node('div');
  row.append(node('dt', label), node('dd', value));
  return row;
 }

 function showEvidence() {
  evidenceBox.replaceChildren();
  evidenceBox.hidden = evidence.kind !== 'build-step';
  if (evidenceBox.hidden) return;
  const heading = node('div', '', 'new-item-evidence-heading');
  heading.append(node('h4', evidence.title), node('span', evidence.owner, 'new-item-muted'));
  const cells = node('dl', '', 'new-item-cells');
  for (const cell of evidence.cells) cells.append(captionAndValue(cell.label, cell.value));

  const memory = node('div', '', 'new-item-memory');
  memory.append(node('h4', 'Overall memory'));
  const figures = node('div', '', 'new-item-evidence-heading'), free = node('p');
  free.append(node('strong', evidence.memory.free_text || 'unavailable'), ' Free for the next step');
  figures.append(free, node('p', 'Queue cap ' + (evidence.memory.cap_text || 'unavailable'), 'new-item-cap'));
  memory.append(figures);
  const bar = node('div', '', 'new-item-memory-bar');
  bar.setAttribute('aria-hidden', 'true');
  const legend = node('div', '', 'new-item-memory-legend');
  for (const segment of evidence.memory.segments) {
   const section = node('span'), key = node('span', segment.label + ' ' + (segment.text || 'unavailable'));
   section.dataset.tone = key.dataset.tone = segment.tone;
   section.style.flexGrow = String(segment.gib);
   bar.append(section);
   legend.append(key);
  }
  if (legend.childElementCount) memory.append(bar, legend);

  if (tipPreview) {
   evidenceBox.append(heading, cells, memory, tipPreview);
   return;
  }
  const tip = node('div', '', 'new-item-tip'), tipHeading = node('div', '', 'new-item-evidence-heading');
  tipHeading.append(node('h4', evidence.tip_title), node('strong', evidence.tip_status));
  tip.append(tipHeading);
  let table = null;
  for (const entry of evidence.tip) {
   if (entry.kind === 'field') {
    if (!table) { table = node('dl'); tip.append(table); }
    table.append(captionAndValue(entry.label, entry.value));
   } else {
    table = null;
    tip.append(node('p', entry.text));
   }
  }
  evidenceBox.append(heading, cells, memory, tip);
 }

 function setError(reason) {
  showActionFailure(error, reason);
 }

 function isScheduled() {
  return form.elements.kind.value === 'scheduled';
 }

 function isRecovering() {
  return submittedDraft?.mode === 'retry' || submittedDraft?.mode === 'screenshot-expired';
 }

 function isEditingLater() {
  return submittedDraft?.mode === 'edit-later';
 }

 function pendingPayload() {
  if (isRecovering()) return submittedDraft.payload;
 }

 function canEditSeparateListDraft() {
  const canResume = separateListEdit && !words.value && !images.length;
  return isListPage() && token && !savedItem && !submittedDraft &&
    !separateFromNativeDraft && (awaitingNativeRecovery || canResume);
 }

 function updateSend() {
  const recipient = recipientChips.querySelector('input:checked');
  const scheduled = isScheduled();
  const recovering = isRecovering();
  const locked = recovering || (scheduled && !!savedItem);
  dialog.classList.toggle('scheduled-locked', scheduled && locked);
  words.readOnly = locked;
  includePage.disabled = sending || locked;
  changeAssignee.disabled = sending || locked;
  if (changeAssignee.disabled) showAssigneeChoices(false);
  for (const button of previews.querySelectorAll('button')) button.disabled = sending || locked;
  for (const fieldset of form.querySelectorAll('fieldset')) fieldset.disabled = sending || locked;
  send.textContent = sending ? 'Sending…' : recovering ? 'Retry' : 'Send';
  laterButton.hidden = !canEditSeparateListDraft() && !['retry', 'edit-later'].includes(submittedDraft?.mode);
  laterButton.disabled = sending;
  laterButton.textContent = isEditingLater() ? 'Return to Retry' : 'Edit a separate draft';
  sentDetails.hidden = scheduled;
  scheduledHelp.hidden = !scheduled || !!savedItem;
  words.placeholder = scheduled ? 'Enter the instruction, start time, and repeat.' : wordsPlaceholder;
  for (const input of kindInputs) {
   input.disabled = sending || locked || !!savedItem ||
     (input.value === 'scheduled' && awaitingEarlierSend && !isEditingLater());
  }
  send.disabled = !!savedItem || sending || !token || isEditingLater() || submittedDraft?.mode === 'screenshot-expired' ||
    (scheduled && awaitingEarlierSend && !recovering) ||
    (!pendingPayload() && (!recipient || !words.value.trim() || words.value.length > words.maxLength));
  renewButton.disabled = sending || !token || !canCapturePage();
  omitButton.disabled = sending || !token;
  producerHint.hidden = !assignees || !token || !!recipient || !!pendingPayload();
  if (producerHint.hidden) send.removeAttribute('aria-describedby');
  else send.setAttribute('aria-describedby', producerHint.id);
 }

 function radioChoice(name, choice, selected, className) {
  const label = node('label', '', className), input = node('input');
  input.type = 'radio';
  input.name = name;
  input.value = choice.id;
  input.checked = choice.id === selected;
  const face = node('span', choice.name);
  face.setAttribute('data-button-face', '');
  label.append(input, face);
  return label;
 }

 function placeAssigneeChoices() {
  if (assigneeGroup.hidden) return;
  const anchor = assignment.getBoundingClientRect(), bounds = body.getBoundingClientRect(), gap = 6;
  const top = bounds.top + gap, bottom = bounds.bottom - gap;
  const aboveEdge = Math.min(anchor.top - gap, bottom), belowEdge = Math.max(anchor.bottom + gap, top);
  const above = Math.max(0, aboveEdge - top), below = Math.max(0, bottom - belowEdge);
  const scrollTop = assigneeGroup.scrollTop;
  assigneeGroup.style.maxBlockSize = '';
  const preferred = assigneeGroup.getBoundingClientRect().height;
  const opensAbove = below < preferred && above > below;
  assigneeGroup.toggleAttribute('data-above', opensAbove);
  assigneeGroup.style.setProperty('--assignee-gap', `${opensAbove ? anchor.top - aboveEdge : belowEdge - anchor.bottom}px`);
  assigneeGroup.style.maxBlockSize = `${opensAbove ? above : below}px`;
  assigneeGroup.scrollTop = scrollTop;
  if (!personnel.hidden) {
   const focused = document.activeElement;
   const chip = personnel.contains(focused) ? focused : personnel.querySelector('button[aria-pressed="true"]');
   if (chip) revealAssigneeChoice(chip);
  }
 }

 function showAssigneeChoices(open) {
  if (open === !assigneeGroup.hidden) return;
  changeAssignee.setAttribute('aria-expanded', String(open));
  assigneeGroup.hidden = !open;
  if (open) {
   placeAssigneeChoices();
   assigneeMenuResize.observe(body);
   assigneeMenuResize.observe(assignment);
  } else {
   assigneeMenuResize.disconnect();
   personnel.hidden = true;
   assigneeMenu.hidden = false;
  }
 }

 function revealAssigneeChoice(choice) {
  const top = assigneeGroup.getBoundingClientRect().top + assigneeGroup.clientTop;
  const bottom = top + assigneeGroup.clientHeight;
  const row = choice.getBoundingClientRect();
  if (row.bottom > bottom) assigneeGroup.scrollTop += Math.ceil(row.bottom - bottom);
  else if (row.top < top) assigneeGroup.scrollTop += Math.floor(row.top - top);
 }

 function showStudioPersonnel() {
  const alphabetical = new Intl.Collator(undefined, {sensitivity: 'base'});
  const ordered = [...assignees].sort((a, b) => alphabetical.compare(a.name, b.name));
  const chips = ordered.map(choice => {
   const chip = node('button', choice.name, 'new-item-chip');
   chip.type = 'button';
   chip.value = choice.id;
   chip.setAttribute('aria-pressed', String(choice.id === pageAssignee));
   return chip;
  });
  personnel.replaceChildren(...chips);
  personnel.hidden = false;
  const firstFocus = chips.find(chip => chip.value === pageAssignee) || chips[0];
  firstFocus.focus({preventScroll: true});
  assigneeMenu.hidden = true;
  assigneeGroup.scrollTop = 0;
  placeAssigneeChoices();
 }

 function closeAssigneeChoices() {
  showAssigneeChoices(false);
  changeAssignee.focus({preventScroll: true});
 }

 function showRoute() {
  route.hidden = !assignees;
  if (route.hidden) return;
  for (const input of assigneeMenu.querySelectorAll('input'))
   input.checked = input.value === (selectedAssignee ?? '');
  const producer = recipients.find(choice => choice.id === selectedRecipient);
  const assignee = assignees.find(choice => choice.id === selectedAssignee);
  const kind = form.elements.kind.value || 'item';
  const subject = producer?.name || 'The producer';
  routeProducer.textContent = producer?.name || 'a producer';
  routeAssignee.textContent = assignee?.name || subject + ' assigns it';
  routeAssigned.hidden = !assignee;
  changeAssignee.hidden = !producer || !assignees.length;
  if (changeAssignee.hidden) showAssigneeChoices(false);
  routeCopy.textContent = producer && assignee
   ? subject + ' tracks this ' + kind + ' and gives it to ' + assignee.name + '.'
   : subject + ' tracks this ' + kind + ' and assigns it.';
 }

 function showRecipients() {
  if (!recipients) return;
  const proposal = form.elements.kind.value === 'proposal';
  const available = recipients.filter(recipient => !proposal || recipient.proposal);
  if (selectedRecipient && !available.some(recipient => recipient.id === selectedRecipient)) {
   if (isRecovering() && selectedRecipient === submittedDraft.payload.recipient_id) {
    available.push({id: selectedRecipient, name: submittedDraft?.recipientName || selectedRecipient});
   } else {
    selectedRecipient = null;
    storeListDraft();
   }
  }
  recipientChips.replaceChildren();
  for (const recipient of available)
   recipientChips.append(radioChoice('recipient_id', recipient, selectedRecipient, 'new-item-chip'));
  showActionFailure(recipientStatus, selectedRecipient === null
   ? (assignees ? 'The producer you chose is unavailable.' : 'The recipient you chose is not available. Choose a recipient.')
   : available.length ? '' : (assignees ? 'No producers are available.' : 'No recipients are available.'));
  showOwnerReason();
 }

 function showOwnerReason() {
  const shown = !!pageOwner && selectedRecipient === pageOwner && !!ownerReasonText.textContent
   && !!recipientChips.querySelector('input:checked');
  ownerReason.hidden = !shown;
  if (shown) recipientGroup.setAttribute('aria-describedby', ownerReasonText.id);
  else recipientGroup.removeAttribute('aria-describedby');
 }

 function editDraft(event) {
  if (sending || isRecovering()) return;
  if (event?.target.name === 'kind') {
   discardCapture();
   if (!awaitingNativeRecovery || isEditingLater()) {
    if (!awaitingEarlierSend && !isEditingLater()) forgetListDraft();
    draftId = undefined;
   }
   ensureDraftId();
   showRecipients();
   showBugPage();
   storeListDraft();
   startCapture();
  }
  if (event?.target.name === 'recipient_id') {
   selectedRecipient = event.target.value;
   storeListDraft();
   recipientStatus.hidden = true;
   showOwnerReason();
  }
  if (event?.target.name === 'assignee_id') {
   selectedAssignee = event.target.value || null;
   storeListDraft();
  }
  if (event?.target === includePage && !pendingPayload()) startCapture();
  showRoute();
  showSentDetails();
  setError('');
  updateSend();
 }

 function removeImage(image) {
  const next = image.row.nextElementSibling || image.row.previousElementSibling;
  images = images.filter(entry => entry !== image);
  URL.revokeObjectURL(image.url);
  image.row.remove();
  previews.hidden = images.length === 0;
  (next?.querySelector('button') || words).focus();
  editDraft();
 }

 words.addEventListener('paste', event => {
  if (sending || isScheduled() || isRecovering()) return;
  const files = [...(event.clipboardData?.files || [])].filter(file => file.type.startsWith('image/'));
  if (!files.length) return;
  event.preventDefault();
  const total = [...images.map(image => image.file), ...files].reduce((sum, file) => sum + file.size, 0);
  if (files.some(file => !limits.types.includes(file.type))) {
   showActionFailure(error, 'Paste PNG, JPEG, GIF or WebP images.'); return;
  }
  if (images.length + files.length > limits.images || total > limits.totalImageBytes ||
      files.some(file => !file.size || file.size > limits.imageBytes)) {
   showActionFailure(error, 'Paste up to four images, at most 5 MiB each and 12 MiB total.'); return;
  }
  addImagePreviews(files);
  editDraft();
 });

 function addImagePreviews(files) {
  for (const file of files) {
   const row = node('div', '', 'new-item-preview'), photo = node('img');
   const url = URL.createObjectURL(file);
   photo.src = url;
   photo.alt = 'Pasted image';
   const remove = node('button', '×');
   remove.type = 'button';
   remove.setAttribute('aria-label', 'Remove pasted image');
   const image = {file, row, url};
   remove.addEventListener('click', () => removeImage(image));
   row.append(photo, remove);
   images.push(image);
   previews.append(row);
  }
  previews.hidden = !images.length;
 }

 function pastedData(file, signal) {
  return new Promise((resolve, reject) => {
   signal?.throwIfAborted();
   const reader = new FileReader();
   const abort = () => {
    reader.abort();
    reject(signal.reason);
   };
   signal?.addEventListener('abort', abort, {once: true});
   reader.addEventListener('loadend', () => signal?.removeEventListener('abort', abort), {once: true});
   reader.addEventListener('load', () => resolve({
    media_type: file.type, base64_data: reader.result.split(',', 2)[1]
   }));
   reader.addEventListener('error', () => reject(new Error('Could not read a pasted image.')));
   reader.readAsDataURL(file);
  });
 }

 function listFilingSkipped(data) {
  // New item receipts have a Review version. A receipt without a List number saved only the Review item.
  return typeof data.version === 'string' && (!Number.isInteger(data.list_number) || data.list_number < 1);
 }

 async function responseData(response, fallback) {
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
   const failure = new Error(data?.error || data?.reason || fallback);
   failure.definiteRefusal = response.status >= 400 && response.status < 500 && data?.code !== 'draft_pending';
   failure.staleToken = response.status === 403 && data?.code === 'stale_token';
   failure.screenshotExpired = response.status === 409 && data?.code === 'screenshot_expired';
   if (response.status === 409 && data?.code === 'already_filed' &&
       typeof data.id === 'string' && /^[QPIL][1-9][0-9]*$/.test(data.id)) {
    failure.filedId = data.id;
    failure.listFilingSkipped = listFilingSkipped(data);
   }
   throw failure;
  }
  return data;
 }

 async function loadOptions(signal, page = ownerPage) {
  const path = page ? endpoint + '?' + new URLSearchParams({page}) : endpoint;
  const response = await fetch(path, {cache: 'no-store', signal, headers: {'X-New-Item-Route': 'producer'}});
  const data = await responseData(response, 'Could not load recipients.');
  if (!Array.isArray(data.recipients) || typeof data.token !== 'string' || (!data.token && !data.send_disabled)) {
   throw new Error('Could not load recipients.');
  }
  return data;
 }

 function setOptions(data, newDraft = false, recipientName = '') {
  token = data.token;
  sendNotice.textContent = data.send_disabled || '';
  sendNotice.hidden = !data.send_disabled;
  includePage.closest('label').hidden = !pageContext || !sendNotice.hidden;
  recipients = data.recipients;
  assignees = ownerPage && Array.isArray(data.assignees)
   ? [...new Map([...recipients, ...data.assignees].map(choice => [choice.id, choice])).values()] : null;
  const pageRole = assignees?.find(choice => choice.id === data.page_assignee);
  pageAssignee = pageRole?.id || '';
  pageOwner = data.page_owner || '';
  ownerReasonText.textContent = data.page_owner_reason || '';
  if (newDraft) {
   selectedRecipient = ownerPage ? pageOwner : recipients.find(recipient => recipient.name === recipientName)?.id || '';
   selectedAssignee = pageAssignee || null;
  }
  if (assignees && selectedAssignee !== null && !assignees.some(choice => choice.id === selectedAssignee))
   selectedAssignee = null;
  const choices = assignees ? [{id:'', name:'None'}] : [];
  if (pageRole) choices.push(pageRole);
  assigneeMenu.replaceChildren(...choices.map(choice =>
   radioChoice('assignee_id', choice, selectedAssignee ?? '', 'route-assignee-option')), otherAssignees);
  showRecipients();
  showRoute();
  updateSend();
 }

 async function post(payload, {keepalive = false, hold = false, receiver, signal, holdRequestId} = {}) {
  const options = {method: 'POST', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify(payload), keepalive, signal};
  const response = hold ? heldFetch(endpoint, {...options, holdRequestId}, receiver) : fetch(endpoint, options);
  return response.then(reply => responseData(reply, 'Could not send this item.'));
 }

 function clip(value, length = 240) {
  return value.trim().match(new RegExp('^.{0,' + length + '}', 'su'))[0];
 }

 function visible(element) {
  return !element.closest('dialog,[aria-hidden=true]') && element.getClientRects().length &&
         getComputedStyle(element).visibility === 'visible';
 }

 function visiblePageData() {
  const main = document.querySelector('main') || document.body;
  const text = element => clip(element.innerText || '');
  const title = clip(main.querySelector('h1')?.textContent || document.title || ownerPage);
  const data = {kind: 'page-data', title, rows: [], sections: []};
  const encoder = new TextEncoder();
  const size = value => encoder.encode(JSON.stringify(value)).length;
  let bytes = size(data);
  function append(entries, entry) {
   const added = size(entry) + Number(entries.length > 0);
   if (bytes + added > limits.pageDataBytes) return false;
   entries.push(entry);
   bytes += added;
   return true;
  }
  function addSection(label, columns, rows) {
   if (!columns.some(Boolean) || data.sections.length >= limits.pageSections) return;
   const section = {label: label || title, columns, rows: []};
   const before = bytes;
   if (!append(data.sections, section)) return;
   for (const values of rows) {
    const row = values.slice(0, 16).map(value => clip(value));
    if (!row.some(Boolean)) continue;
    if (!append(section.rows, row) || section.rows.length === limits.sectionRows) break;
   }
   if (!section.rows.length) {
    data.sections.pop();
    bytes = before;
   }
  }
  if (ownerPage === 'builds') {
   const memory = function* () {
    for (const row of main.querySelectorAll('.legend .row[data-tip]')) {
     if (visible(row)) yield [text(row.querySelector('.name') || row), text(row.querySelector('.v') || row)];
    }
   };
   addSection('Build memory', ['Name', 'Value'], memory());
  }
  for (const element of main.querySelectorAll('table,dl')) {
   if (!visible(element)) continue;
   const heading = element.closest('section,article,details')?.querySelector('h2,h3,summary');
   const label = clip(element.caption?.innerText || element.getAttribute('aria-label') || heading?.innerText || '');
   if (element.pageData) {
    const {columns, rows} = element.pageData();
    addSection(label, columns, rows);
   } else if (element.matches('table')) {
    const header = element.tHead?.rows[0] || element.rows[0];
    if (!header) continue;
    const columns = [...header.cells].slice(0, 16).map(cell => cell.tagName === 'TH' && visible(cell) ? text(cell) : '');
    const rows = function* () {
     for (const row of element.rows) {
      if (row === header || row.closest('thead') || row.matches('[data-detail]') || !visible(row)) continue;
      yield columns.map((column, at) => column && row.cells[at] ? text(row.cells[at]) : '');
     }
    };
    addSection(label, columns, rows());
   } else {
    const rows = function* () {
     for (const term of element.querySelectorAll('dt')) {
      const value = term.nextElementSibling;
      if (visible(term) && value?.matches('dd') && visible(value)) yield [text(term), text(value)];
     }
    };
    addSection(label, ['Name', 'Value'], rows());
   }
  }
  for (const element of main.querySelectorAll('h2,h3,p,dl,tr')) {
   if (!visible(element)) continue;
   const cells = element.matches('tr,dl') ? [...element.children].slice(0, 16).map(text) : [text(element)];
   if (!cells.some(Boolean)) continue;
   if (!append(data.rows, cells) || data.rows.length === limits.pageRows) break;
  }
  if (!data.sections.length) delete data.sections;
  return data;
 }

 function viewport() {
  const dark = document.documentElement.dataset.theme
   ? document.documentElement.dataset.theme === 'dark'
   : matchMedia('(prefers-color-scheme: dark)').matches;
  return {width: innerWidth, height: innerHeight, dark};
 }

 function copyLiveDocument(doc, depth = 0, focused = doc.activeElement) {
  const view = doc.defaultView, root = doc.documentElement;
  const copy = root.cloneNode(true);
  const originals = root.querySelectorAll('*'), copies = copy.querySelectorAll('*');
  const windows = view.pageCaptureWindows?.() ?? [...root.querySelectorAll('dialog:modal')];
  function imagePixels(image) {
   const canvas = doc.createElement('canvas');
   canvas.width = image.naturalWidth;
   canvas.height = image.naturalHeight;
   canvas.getContext('2d').drawImage(image, 0, 0);
   return canvas.toDataURL();
  }
  function storeScroll(element, duplicate) {
   if (element.scrollLeft || element.scrollTop) {
    duplicate.setAttribute('data-capture-scroll', element.scrollLeft + ' ' + element.scrollTop);
   }
  }
  if (view.scrollX || view.scrollY) copy.setAttribute('data-capture-scroll', view.scrollX + ' ' + view.scrollY);
  for (let i = 0; i < originals.length; i++) {
   const element = originals[i];
   let duplicate = copies[i];
   if (!copy.contains(duplicate)) continue;
   if (element.matches('script,base,template,#review-new-item,[data-new-item-draft]')) {
    duplicate.remove();
    continue;
   }
   if (element instanceof view.HTMLInputElement) {
    if (element.type === 'hidden' || element.type === 'file') duplicate.removeAttribute('value');
    else duplicate.setAttribute('value', element.type === 'password' ? '•'.repeat(element.value.length) : element.value);
    duplicate.toggleAttribute('checked', element.checked);
    duplicate.toggleAttribute('data-capture-indeterminate', element.indeterminate);
   } else if (element instanceof view.HTMLTextAreaElement) {
    duplicate.textContent = element.value;
   } else if (element instanceof view.HTMLOptionElement) {
    duplicate.toggleAttribute('selected', element.selected);
   } else if (element instanceof view.HTMLCanvasElement) {
    const image = doc.createElement('img');
    for (const attribute of duplicate.attributes) image.setAttribute(attribute.name, attribute.value);
    const style = view.getComputedStyle(element);
    image.style.width = style.width;
    image.style.height = style.height;
    image.src = element.toDataURL();
    duplicate.replaceWith(image);
    duplicate = image;
   } else if (element instanceof view.HTMLImageElement && element.currentSrc) {
    const url = new URL(element.currentSrc);
    const source = url.protocol === 'blob:' ? imagePixels(element)
     : url.origin === view.location.origin ? url.pathname + url.search + url.hash : url.href;
    duplicate.closest('picture')?.querySelectorAll('source').forEach(source => source.remove());
    duplicate.removeAttribute('srcset');
    duplicate.removeAttribute('loading');
    duplicate.setAttribute('src', source);
   } else if (element instanceof view.HTMLDialogElement && element.matches(':modal')) {
    duplicate.setAttribute('data-capture-modal', Math.max(0, windows.indexOf(element)));
   } else if (element instanceof view.HTMLIFrameElement) {
    duplicate.removeAttribute('src');
    duplicate.removeAttribute('srcdoc');
    duplicate.removeAttribute('loading');
    const inside = element.contentDocument;
    if (depth < 3 && inside?.documentElement) {
     duplicate.setAttribute('data-capture-frame-path', inside.location.pathname);
     duplicate.setAttribute('srcdoc', copyLiveDocument(inside, depth + 1,
      element === focused ? inside.activeElement : null));
    }
   }
   if (element !== doc.scrollingElement) storeScroll(element, duplicate);
   if (element === focused && element !== doc.body) {
    const visible = element.matches(':focus-visible');
    duplicate.dataset.captureFocus = visible ? 'visible' : 'hidden';
    if (visible) {
     // Firefox screenshots use an inactive window. Keep the paint from the live window.
     const style = view.getComputedStyle(element);
     for (const property of ['outline', 'outline-offset', 'box-shadow']) {
      duplicate.style.setProperty(property, style.getPropertyValue(property), 'important');
     }
    }
   }
  }
  return '<!doctype html>' + copy.outerHTML;
 }

 async function preparePageSnapshot(signal) {
  try {
   const request = {place: location.pathname, ...viewport()};
   const documentBytes = new Blob([copyLiveDocument(document)]);
   if (documentBytes.size > limits.captureDocumentBytes) return;
   const stream = documentBytes.stream().pipeThrough(new CompressionStream('gzip'), {signal});
   const compressed = await new Response(stream).blob();
   if (compressed.size > limits.captureBytes) return;
   const {base64_data} = await pastedData(compressed, signal);
   if (!signal.aborted) pageCapture = {status: 'prepared', request, document_gzip: base64_data};
  } catch (_) {
   // Use the page data if the browser cannot prepare a screenshot.
  } finally {
   if (!signal.aborted && pageCapture.status === 'preparing') pageCapture = {status: 'unavailable'};
  }
 }

 function prepareCapture(signal) {
  const preparation = {status: 'preparing'};
  pageCapture = preparation;
  preparation.ready = preparePageSnapshot(signal);
  return preparation.ready;
 }

 function snapshotUpload() {
  return {...pageCapture.request, document_gzip: pageCapture.document_gzip};
 }

 function collectPageContext() {
  const filters = [];
  for (const chip of document.querySelectorAll('[data-filter-row] [data-filter-value]')) {
   if (chip.getAttribute('aria-pressed') !== 'true' || !visible(chip)) continue;
   const group = chip.closest('[role="group"]')?.getAttribute('aria-label');
   if (!group) continue;
   const label = chip.cloneNode(true);
   label.querySelector('b')?.remove();
   filters.push(group + ' ' + clip(label.textContent));
  }
  const search = document.querySelector('#list-search,[data-filter-words]');
  if (search?.value && visible(search)) filters.push('Search ' + clip(search.value));
  const notices = new Set();
  const selector = '[data-review-source-notices] li,[data-review-notice],[data-hidden-action-status],[data-missing-item],.notice,' +
                   '[role="alert"],[data-page-status][data-state="Failed"] [data-status-reason]';
  for (const element of document.querySelectorAll(selector)) {
   if (!visible(element)) continue;
   const line = clip(element.innerText || '', 512);
   if (line) notices.add(line);
   if (notices.size === 48) break;
  }
  const status = document.querySelector('[data-page-status]');
  const seconds = Number(status?.dataset.renderSeconds ?? NaN);
  const data = visiblePageData();
  return {
   data,
   state: {url: clip(location.href, limits.pageUrlLength), title: data.title,
           filters: clip(filters.join(' · '), 1024),
           render_seconds: Number.isFinite(seconds) && seconds >= 0 ? seconds : null,
           notices: [...notices], script_errors: (window.consoleScriptErrors || []).slice()}
  };
 }

 function isPageBug() {
  return form.elements.kind.value === 'bug';
 }

 function showBugPage() {
  const bug = isPageBug();
  dialog.toggleAttribute('data-page-bug', Boolean(bug));
  words.rows = bug ? 3 : 4;
  bugStateBox.hidden = !bug || !pageContext;
  fallback.textContent = bug ? 'Screenshot unavailable.' : fallbackText;
  if (!bug || !pageContext?.state) {
   bugStateBox.querySelector('dl').replaceChildren();
   return;
  }
  const {state} = pageContext;
  const rows = [
   ['Page', state.title],
   ['Address', state.url],
   ['Filters', state.filters || 'None'],
   ['Render time', state.render_seconds === null ? 'Unavailable' : state.render_seconds.toFixed(2) + ' s']
  ];
  if (state.notices.length) rows.push(['Error lines', state.notices.join('\n'), true]);
  if (state.script_errors.length) rows.push(['Script errors', state.script_errors.map(error => error.message).join('\n'), true]);
  bugStateBox.querySelector('dl').replaceChildren(...rows.flatMap(([label, value, failed]) =>
   [node('dt', label), node('dd', value, failed ? 'error-line' : '')]));
 }

 function hasCaptureSpace(image) {
  return images.length < limits.images &&
         images.reduce((total, entry) => total + entry.file.size, image?.size ?? 0) <= limits.totalImageBytes;
 }

 function includesPageCapture() {
  return Boolean(pageContext) && includePage.checked && sendNotice.hidden;
 }

 function capturePreview() {
  if (captureSession) return captureSession.preview;
  return {status: pageCapture.status === 'unavailable' && !pendingPayload()?.defer_capture ? 'unavailable' : 'pending'};
 }

 function showSentDetails() {
  const choosing = submittedDraft?.mode === 'screenshot-expired';
  replaceCapture.hidden = !choosing;
  const key = submittedDraft?.payload.capture_key;
  const hasOriginalPreview = key && (captureSession?.requestKey === key || captureSession?.preview.key === key);
  if (isRecovering() && (choosing || !hasOriginalPreview || captureSession?.preview.status === 'unavailable')) {
   slot.hidden = true;
   expired.hidden = !choosing;
   fallback.hidden = choosing || !includePage.checked;
   fallback.textContent = submittedDraft.payload.defer_capture
    ? 'The screenshot joins the saved item when ready.'
    : key ? 'Retry includes the screenshot.' : 'Retry sends the page data without a screenshot.';
   updateSend();
   return;
  }
  const previewState = capturePreview();
  const image = previewState.status === 'ready' ? previewState : undefined;
  const include = includesPageCapture();
  const unavailable = include && (previewState.status === 'unavailable' || !hasCaptureSpace(image));
  slot.hidden = !include || unavailable;
  taking.hidden = !!image;
  shot.hidden = !image;
  fallback.hidden = !unavailable;
  expired.hidden = slot.hidden || taking.hidden || !captureSession?.renewed;
  if (image && shot.href !== image.url) {
   shot.href = preview.src = image.url;
  }
  updateSend();
 }

 function startCapture(renewed = false) {
  if (isScheduled() || isRecovering() || isEditingLater() || (savedItem && !renewed) ||
      !includesPageCapture() || !token || captureSession || pageCapture.status !== 'prepared') return;
  const requestKey = ensureDraftId() + randomNonce();
  const session = {token, renewed, requestKey, sent: false, page: ownerPage,
                   controller: new AbortController(), preview: {status: 'pending'}};
  captureSession = session;
  showSentDetails();
  const key = post({token, page_capture: snapshotUpload(), capture_key: requestKey},
                   {signal: session.controller.signal}).then(result => result.capture_key);
  loadCapturePreview(session, key);
 }

 function loadCapturePreview(session, captureKey) {
  const signal = session.controller.signal;
  void captureKey.then(async key => {
   if (!key) return null;
   const path = endpoint + '?' + new URLSearchParams({capture_key: key});
   const getPreview = () => fetch(path, {cache: 'no-store',
    headers: {'X-Review-Token': session.token}, signal});
   let refreshToken = true;
   while (!signal.aborted) {
    const response = await getPreview().catch(() => null);
    if (signal.aborted) return null;
    if ((!response || response.status === 403) && refreshToken) {
     const fresh = await loadOptions(signal, session.page);
     if (!fresh.token) return null;
     session.token = fresh.token;
     if (captureSession === session) setOptions(fresh);
     refreshToken = false;
    } else if (response?.status === 202) {
     await new Promise(resolve => setTimeout(resolve, 250));
    } else {
     return response?.ok ? {key, blob: await response.blob()} : null;
    }
   }
   return null;
  }).catch(() => null).then(image => {
   if (captureSession !== session) return;
   session.preview = image
    ? {status: 'ready', key: image.key, size: image.blob.size, url: URL.createObjectURL(image.blob)}
    : {status: 'unavailable'};
   showSentDetails();
  });
 }

 function randomNonce() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)),
                    byte => byte.toString(16).padStart(2, '0')).join('');
 }

 function ensureDraftId() {
  const prefix = isScheduled() ? 's1-' : 'r6-';
  if (!draftId?.startsWith(prefix)) draftId = prefix + randomNonce();
  return draftId;
 }

 async function renewCapture() {
  if (!pageContext || !includePage.checked) return;
  const signal = opening.signal;
  const fresh = await loadOptions(signal).catch(() => null);
  if (signal.aborted) return;
  if (fresh) setOptions(fresh);
  startCapture(true);
  showSentDetails();
 }

 function discardCapture(used = false) {
  const session = captureSession;
  captureSession = undefined;
  if (!session) return;
  session.controller.abort();
  if (session.preview.status === 'ready') URL.revokeObjectURL(session.preview.url);
  shot.removeAttribute('href');
  preview.removeAttribute('src');
  session.preview = {status: 'unavailable'};
  showSentDetails();
  if (!used && !session.sent) {
   void post({token: session.token, discard_capture: session.requestKey}, {keepalive: true}).catch(() => {});
  }
 }

 function isShippedListDraft(draft) {
  return draft?.id.startsWith('r6-') && draft.awaitingSend === undefined;
 }

 function storeListDraft() {
  if (!isListPage() || !draftId || isEditingLater() ||
      separateFromNativeDraft || !sendNotice.hidden) return;
  try {
   const saved = assignees ? JSON.stringify({recipient: selectedRecipient || null, assignee: selectedAssignee})
                           : selectedRecipient || '';
   sessionStorage.setItem(listDraftPrefix + draftId, saved);
   if (!awaitingNativeRecovery) sessionStorage.setItem(listStatePrefix + draftId, String(awaitingEarlierSend));
  } catch (_) {}
 }

 function draftTransaction(mode, action) {
  draftDatabase ||= new Promise((resolve, reject) => {
   const request = indexedDB.open('console.new-item', 1);
   request.onupgradeneeded = () => request.result.createObjectStore('submitted');
   request.onsuccess = () => resolve(request.result);
   request.onerror = () => reject(request.error);
  });
  return draftDatabase.then(database => new Promise((resolve, reject) => {
   const transaction = database.transaction('submitted', mode);
   const request = action(transaction.objectStore('submitted'));
   transaction.oncomplete = () => resolve(request.result);
   transaction.onabort = () => reject(transaction.error);
  }));
 }

 async function storeSubmittedDraft(draft) {
  const id = draft.payload.request_id;
  const key = submittedStorageKey || (sessionStorage.getItem(submittedKey) ? submittedPrefix + id : submittedKey);
  const earlier = sessionStorage.getItem(key);
  let pointer;
  try {
   sessionStorage.setItem(key, JSON.stringify(draft));
  } catch (failure) {
   if (failure?.name !== 'QuotaExceededError') throw failure;
   await draftTransaction('readwrite', store => store.put(draft, id));
   pointer = id;
   sessionStorage.setItem(key, pointer);
  }
  if (earlier && browserDraftId.test(earlier) && earlier !== pointer) {
   void draftTransaction('readwrite', store => store.delete(earlier)).catch(() => {});
  }
  submittedStorageKey = key;
  submittedDraft = draft;
 }

 async function restoreSubmittedDraft() {
  if (submittedDraft) return submittedDraft;
  const keys = draftId ? [submittedPrefix + draftId, submittedKey] :
   [...Object.keys(sessionStorage).filter(key => key.startsWith(submittedPrefix)), submittedKey];
  for (const key of keys) {
   const stored = sessionStorage.getItem(key);
   if (!stored) continue;
   const saved = browserDraftId.test(stored)
    ? await draftTransaction('readonly', store => store.get(stored)) : JSON.parse(stored);
   const id = saved?.payload?.request_id || '';
   const prefix = saved?.payload?.kind === 'scheduled' ? 's1-' : 'r6-';
   if (!browserDraftId.test(id) || !id.startsWith(prefix)) throw new Error('Could not restore the submitted New item.');
   if (drafts.some(other => other !== draft && other.id === id)) continue;
   submittedDraft = {...saved, mode: saved.mode || 'retry'};
   submittedStorageKey = key;
   break;
  }
  return submittedDraft;
 }

 function clearSubmittedDraft() {
  if (!submittedDraft) return;
  const key = submittedStorageKey;
  const stored = sessionStorage.getItem(key);
  sessionStorage.removeItem(key);
  submittedDraft = undefined;
  submittedStorageKey = undefined;
  if (stored && browserDraftId.test(stored)) {
   void draftTransaction('readwrite', store => store.delete(stored)).catch(() => {});
  }
 }

 function forgetListDraft() {
  if (isListPage() && draftId && !separateFromNativeDraft) {
   try {
    sessionStorage.removeItem(listDraftPrefix + draftId);
    sessionStorage.removeItem(listStatePrefix + draftId);
   } catch (_) {}
  }
 }

 function finishDraft() {
  clearSubmittedDraft();
  forgetListDraft();
  if (draftId && !isListPage()) {
   try { sessionStorage.removeItem(reportDraftPrefix(source) + draftId); } catch (_) {}
  }
  draftId = undefined;
  awaitingEarlierSend = awaitingNativeRecovery = false;
 }

 function replaceImages(files) {
  for (const image of images) URL.revokeObjectURL(image.url);
  images = [];
  previews.replaceChildren();
  addImagePreviews(files);
 }

 function editSnapshot() {
  return {kind: form.elements.kind.value, recipient: selectedRecipient, assignee: selectedAssignee, words: words.value,
          include: includePage.checked, files: images.map(image => image.file), pageContext, ownerPage, id: draftId};
 }

 function showLaterDraft() {
  const draft = laterDraft;
  laterDraft = undefined;
  if (draft) showEdit(draft);
 }

 function showEdit(draft) {
  ownerPage = draft.ownerPage;
  pageContext = draft.pageContext;
  form.elements.kind.value = draft.kind;
  draftId = draft.id;
  selectedRecipient = draft.recipient;
  selectedAssignee = draft.assignee || null;
  words.value = draft.words;
  includePage.checked = draft.include;
  replaceImages(draft.files);
  showRecipients();
  showRoute();
  showBugPage();
 }

 function decodePastedFiles(uploads) {
  return uploads.map(image => new File([
   Uint8Array.from(atob(image.base64_data), character => character.charCodeAt(0))
  ], 'Pasted image', {type: image.media_type}));
 }

 function showSubmittedDraft(draft) {
  const original = draft.payload;
  const editingLater = isEditingLater();
  if (editingLater) {
   const draft = editSnapshot();
   const sameFiles = draft.files.length === laterBase.files.length &&
    draft.files.every((file, index) => file === laterBase.files[index]);
   if (draft.words !== laterBase.words || draft.kind !== laterBase.kind || draft.recipient !== laterBase.recipient ||
       draft.assignee !== laterBase.assignee || draft.include !== laterBase.include || !sameFiles) laterDraft = draft;
   laterBase = undefined;
  }
  draft.mode = draft.mode === 'screenshot-expired' ? 'screenshot-expired' : 'retry';
  draftId = original.request_id;
  ownerPage = original.page ?? '';
  if (isListPage()) {
   const stored = storedListDraft();
   separateFromNativeDraft = isShippedListDraft(stored) && stored.id !== draftId;
   if (separateFromNativeDraft) {
    awaitingNativeRecovery = false;
    if (!nativeListEdit && draft.nativeListEdit) {
     nativeListEdit = {...draft.nativeListEdit, files: decodePastedFiles(draft.nativeListEdit.files)};
    }
   }
  }
  awaitingEarlierSend = true;
  form.elements.kind.value = draft.selectedKind || original.kind;
  selectedRecipient = original.recipient_id;
  selectedAssignee = original.assignee_id || null;
  words.value = original.words;
  includePage.checked = draft.includeScreenshot ?? Boolean(original.capture_key);
  const url = original.page_state?.url ? new URL(original.page_state.url) : null;
  pageContext = draft.pageContext || {
   state: original.page_state, data: original.evidence,
   place: original.defer_capture?.place || (url ? url.pathname + url.search + url.hash : ''),
   viewport: original.defer_capture || original.page_state?.viewport || viewport()
  };
  if (editingLater || images.length !== (original.images?.length || 0)) {
   replaceImages(decodePastedFiles(original.images || []));
  }
  if (!keepDraft || error.hidden || draft.mode === 'screenshot-expired') {
   setError(draft.mode === 'retry' ? 'Retry to confirm the save.' : '');
  }
  showRecipients();
  showRoute();
  showBugPage();
  showSentDetails();
 }

 laterButton.addEventListener('click', () => {
  if (canEditSeparateListDraft()) {
   discardCapture();
   if (awaitingNativeRecovery) {
    nativeListEdit = editSnapshot();
    separateFromNativeDraft = true;
    awaitingEarlierSend = awaitingNativeRecovery = false;
   }
   showEdit(separateListEdit || {...nativeListEdit, kind: 'scheduled', id: undefined, words: '', files: []});
   separateListEdit = undefined;
   ensureDraftId();
   setError('');
   showSentDetails();
   startCapture();
   words.focus();
   return;
  }
  if (isEditingLater()) {
   showSubmittedDraft(submittedDraft);
   setError('Retry to confirm the save.');
   send.focus();
   return;
  }
  if (!isRecovering()) return;
  laterBase = editSnapshot();
  submittedDraft.mode = 'edit-later';
  discardCapture();
  draftId = undefined;
  showLaterDraft();
  ensureDraftId();
  setError('Confirm the earlier Send before you send these edits.');
  showSentDetails();
  words.focus();
 });

 async function chooseScreenshot(renew) {
  if (submittedDraft?.mode !== 'screenshot-expired' || sending) return;
  const payload = {...submittedDraft.payload};
  delete payload.capture_key;
  delete payload.defer_capture;
  if (payload.page_state) {
   payload.page_state = {...payload.page_state, include_screenshot: false};
  }
  if (renew) {
   payload.capture_key = draftId + randomNonce();
   if (pageCapture.status !== 'prepared') await prepareCapture(opening.signal);
   if (pageCapture.status !== 'prepared') {setError('The screenshot is unavailable.');return;}
   payload.defer_capture = pageCapture.request;
  }
  setSending(true);
  try {
   await storeSubmittedDraft({...submittedDraft, payload, mode: 'retry', includeScreenshot: renew,
     holdRequestId: heldFetch.newId()});
   includePage.checked = renew;
   setError('');
  } catch (failure) {
   setError(failure.message);
  } finally {
   setSending(false);
   showSentDetails();
  }
  send.focus();
 }

 renewButton.addEventListener('click', () => void chooseScreenshot(true));
 omitButton.addEventListener('click', () => void chooseScreenshot(false));

 function showConfirmation(item, {missingListRow = false} = {}) {
  savedItem = item;
  const saved = Boolean(item);
  const scheduled = saved && item.id.startsWith('L');
  confirmation.textContent = scheduled
   ? ['Saved List item ' + item.id.slice(1) + '.', item.receipt?.replace(/ List item [1-9][0-9]*\.$/, ''),
      ...(item.readback || []), item.notice_warning].filter(Boolean).join('\n')
   : missingListRow ? 'Saved ' + item.id + ' in Review. No List row was filed.'
   : saved ? ['Saved ' + item.id + '.', item.attachment_warning].filter(Boolean).join('\n') : '';
  confirmation.hidden = savedControls.hidden = !saved;
  body.hidden = saved && !scheduled;
  sendControls.hidden = saved;
  closeButton.hidden = Boolean(item?.attachment_warning);
  updateSend();
  if (saved) done.focus();
 }

 function releasePageCapture(used = false) {
  opening.abort();
  pageCapture = {status: 'unavailable'};
  pageContext = null;
  includePage.closest('label').hidden = true;
  showBugPage();
  if (captureSession) discardCapture(used);
  else showSentDetails();
 }

 function close({sent = false, acknowledged = false} = {}) {
  if (sending || (!sent && !dialog.open)) return;
  const result = savedItem;
  if (result?.attachment_warning) {
   if (!acknowledged) return;
   discardCapture();
   finishDraft();
   if (laterDraft) {
    showConfirmation();
    showLaterDraft();
    showSentDetails();
    startCapture();
    updateSend();
    words.focus();
    dialog.dispatchEvent(new CustomEvent('review-item-created', {bubbles: true, detail: result}));
    return;
   }
  }
  if (isEditingLater()) showSubmittedDraft(submittedDraft);
  const returnToNative = separateFromNativeDraft && !submittedDraft;
  if (returnToNative) {
   if (!sent && !result) separateListEdit = editSnapshot();
   separateFromNativeDraft = false;
   draftId = undefined;
   awaitingEarlierSend = false;
  }
  releasePageCapture();
  if (dialog.open) hideDialog();
  keepDraft = !returnToNative && !sent && !result &&
    Boolean(submittedDraft || awaitingEarlierSend || words.value || images.length);
  if (!keepDraft) {
   if (!returnToNative) finishDraft();
   if (result) showConfirmation();
   for (const image of images) URL.revokeObjectURL(image.url);
   images = [];
   previews.replaceChildren();
   previews.hidden = true;
   source = '';
  }
  launcher = null;
  if (fresh && !keepDraft) {
   dialog.remove();
   drafts.splice(drafts.indexOf(draft), 1);
  }
 }

 function hideDialog() {
  showAssigneeChoices(false);
  dialog.close();
  if (launcher?.isConnected) launcher.focus({preventScroll: true});
 }

 function setSending(value) {
  sending = value;
  for (const control of [...fields, ...previews.querySelectorAll('button')]) control.disabled = value;
  form.setAttribute('aria-busy', String(value));
  updateSend();
 }

 async function open(trigger, page, key, {kind = 'bug', recipientName = '',
                                        capture = {kind: 'none'}, shownTip = null} = {}) {
  if (dialog.open) {
   (savedItem ? done : words).focus({preventScroll: true});
   return;
  }
  if (sending) return;
  source = key;
  if (!keepDraft) {
   evidence = structuredClone(capture);
   tipPreview = shownTip;
  }
  ownerPage = submittedDraft ? submittedDraft.payload.page ?? '' : page;
  launcher = trigger;
  launcherPage = page;
  token = '';
  if (!draftId && !keepDraft) {
   const stored = isListPage() ? storedListDraft() : storedReportDraft(source);
   if (stored) {
    draftId = stored.id;
    selectedRecipient = stored.recipient;
    if (stored.kind) kind = stored.kind;
    selectedAssignee = stored.assignee || null;
    // The shipped List dialog stores no Send state.
    awaitingNativeRecovery = ownerPage === 'list' && isShippedListDraft(stored);
    awaitingEarlierSend = awaitingNativeRecovery || Boolean(stored.kind) || stored.awaitingSend === true;
   }
  }
  const newDraft = !keepDraft && !draftId;
  if (newDraft) {
   selectedRecipient = '';
   selectedAssignee = null;
   showAssigneeChoices(false);
  }
  if (!keepDraft) {
   form.reset();
   setError('');
   form.elements.kind.value = kind;
   if (nativeListEdit && awaitingNativeRecovery && nativeListEdit.id === draftId) {
    showEdit(nativeListEdit);
    nativeListEdit = undefined;
   }
  }
  await openDraftForm({focus: keepDraft ? words : heading, newDraft, recipientName, resetScroll: true});
 }

 function canCapturePage() {
  return Boolean(ownerPage) && (ownerPage === launcherPage ||
    isListPage() && ['list', 'studio'].includes(launcherPage));
 }

 async function openDraftForm({focus, newDraft = false, recipientName = '', resetScroll = false}) {
  token = '';
  sendNotice.hidden = true;
  sendNotice.textContent = '';
  recipients = null;
  assignees = null;
  showAssigneeChoices(false);
  route.hidden = true;
  recipientChips.replaceChildren();
  pageOwner = '';
  showOwnerReason();
  showActionFailure(recipientStatus);
  recipientLoading.hidden = false;
  opening = new AbortController();
  const signal = opening.signal;
  pageContext = canCapturePage() ? collectPageContext() : null;
  if (pageContext) prepareCapture(signal);
  includePage.closest('label').hidden = !pageContext;
  showEvidence();
  showBugPage();
  showSentDetails();
  updateSend();
  dialog.showModal();
  const initialFocus = focus.disabled ? words : focus;
  initialFocus.focus({preventScroll: true});
  if (resetScroll) body.scrollTop = 0;
  try {
   const restored = await restoreSubmittedDraft();
   if (signal.aborted) return;
   const original = restored?.payload;
   if (original) showSubmittedDraft(restored);
   const data = await loadOptions(signal);
   if (signal.aborted) return;
   setOptions(data, newDraft && !original, recipientName);
   if (pageCapture.status === 'preparing') await pageCapture.ready;
   if (signal.aborted) return;
   if (focus === send && document.activeElement === initialFocus) send.focus({preventScroll: true});
   startCapture();
   showSentDetails();
  } catch (failure) {
   if (!signal.aborted) showActionFailure(recipientStatus, failure.message);
  } finally {
   if (!signal.aborted) recipientLoading.hidden = true;
  }
 }

 form.addEventListener('input', editDraft);
 body.addEventListener('scroll', placeAssigneeChoices);
 assignment.addEventListener('mousedown', event => {
  if (!assigneeGroup.hidden && event.button === 0 &&
      (changeAssignee.contains(event.target) || event.target.closest('label.route-assignee-option') &&
       !event.target.matches('input'))) event.preventDefault();
 });
 changeAssignee.addEventListener('click', () => {
  if (!assigneeGroup.hidden) {
   closeAssigneeChoices();
   return;
  }
  showAssigneeChoices(true);
  const selected = assigneeMenu.querySelector('input:checked');
  assigneeGroup.scrollTop = 0;
  (selected || otherAssignees).focus({preventScroll: true});
  revealAssigneeChoice(selected ? selected.labels[0] : otherAssignees);
 });
 otherAssignees.addEventListener('click', showStudioPersonnel);
 personnel.addEventListener('click', event => {
  const chip = event.target.closest('button');
  if (!chip) return;
  selectedAssignee = chip.value;
  storeListDraft();
  closeAssigneeChoices();
  editDraft();
 });
 assigneeGroup.addEventListener('focusout', event => {
  if (!assigneeGroup.contains(event.relatedTarget)) showAssigneeChoices(false);
 });
 assigneeGroup.addEventListener('click', event => {
  if (event.target.matches('input')) closeAssigneeChoices();
 });
 assigneeGroup.addEventListener('keydown', event => {
  if (event.key !== 'Enter' || !event.target.matches('input[name="assignee_id"]')) return;
  event.preventDefault();
  event.target.click();
 });
 dialog.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || assigneeGroup.hidden) return;
  event.preventDefault();
  event.stopPropagation();
  closeAssigneeChoices();
 });
 dialog.addEventListener('click', event => {
  if (!assigneeGroup.hidden && !changeAssignee.contains(event.target) && !assigneeGroup.contains(event.target))
   showAssigneeChoices(false);
 });
 bindConsoleDialog(dialog, close);
 dialog.querySelector('[data-cancel]').addEventListener('click', () => close());
 done.addEventListener('click', () => close({acknowledged: true}));
 async function submissionPayload() {
  const pending = pendingPayload();
  if (pending) return pending;
  const type = form.elements.kind.value, recipient = recipientChips.querySelector('input:checked');
  if (!recipient) throw new Error('Select a recipient.');
  const kind = type === 'feedback' ? 'investigation' : type;
  const payload = {request_id: ensureDraftId(), kind, recipient_id: recipient.value, words: words.value,
                   ...(ownerPage && {page: ownerPage, page_recipient: assignees ? 'producer' : 'selected'}),
                   ...(assignees && {assignee_id: selectedAssignee}),
                   ...(type === 'feedback' && {notice: {kind: type}})};
  if (includesPageCapture() && hasCaptureSpace() && pageCapture.status === 'preparing') {
   await pageCapture.ready;
   startCapture();
  }
  const previewState = capturePreview();
  const include = includesPageCapture() && hasCaptureSpace(previewState.status === 'ready' ? previewState : undefined);
  let key = '';
  const deferredCaptureKey = include && previewState.status === 'pending' && pageCapture.status === 'prepared'
   ? captureSession?.requestKey : undefined;
  if (include && previewState.status === 'ready') key = previewState.key;
  const bug = isPageBug();
  const fallbackData = includesPageCapture() && !key;
  const data = evidence.kind !== 'none' ? evidence
   : pageContext && (bug || deferredCaptureKey || fallbackData) ? pageContext.data : {kind: 'none'};
  const state = bug && pageContext?.state;
  const files = await Promise.all(images.map(image => pastedData(image.file)));
  if (deferredCaptureKey && captureSession?.requestKey === deferredCaptureKey) captureSession.sent = true;
  const item = {...payload, evidence: data, images: files,
                ...(deferredCaptureKey && {capture_key: deferredCaptureKey, defer_capture: pageCapture.request}),
                ...(key && {capture_key: key}), ...(state && {page_state: state})};
  return item;
 }

 form.addEventListener('submit', async event => {
  event.preventDefault();
  if (send.disabled) return;
  const onList = isListPage();
  const hadEarlierSend = awaitingEarlierSend;
  let saved, usedPreview = false, posted = false;
  setError('');
  setSending(true);
  if (!isScheduled()) hideDialog();
  try {
   const item = await submissionPayload();
   if (submittedDraft?.payload !== item) {
    const recipientName = recipientChips.querySelector('input:checked')?.nextElementSibling.textContent;
    const draft = {payload: item, mode: 'live', recipientName, pageContext, selectedKind: form.elements.kind.value,
                   includeScreenshot: includePage.checked, holdRequestId: heldFetch.newId()};
    if (separateFromNativeDraft && nativeListEdit) {
     const files = await Promise.all(nativeListEdit.files.map(pastedData));
     draft.nativeListEdit = {...nativeListEdit, files};
    }
    await storeSubmittedDraft(draft);
   }
   awaitingEarlierSend = true;
   storeListDraft();
   usedPreview = Boolean(item.capture_key && !item.defer_capture);
   const sendItem = () => {
    posted = true;
    if (usedPreview && captureSession?.preview.key === item.capture_key) captureSession.sent = true;
    const receiver = !hadEarlierSend && recipients?.find(recipient => recipient.id === item.recipient_id)?.name;
    const capture = pageCapture.status === 'prepared' && item.defer_capture &&
      (captureSession?.requestKey === item.capture_key || submittedDraft?.includeScreenshot &&
       item.defer_capture === pageCapture.request) ? {defer_capture: snapshotUpload()} : {};
    return post({...item, ...capture, token}, {hold: item.kind !== 'scheduled', receiver: receiver || undefined,
                holdRequestId: submittedDraft?.holdRequestId});
   };
   let result;
   try {
    result = await sendItem();
   } catch (failure) {
    if ((onList && item.kind !== 'scheduled') || !failure?.staleToken) throw failure;
    const fresh = await loadOptions(opening.signal);
    setOptions(fresh);
    if (!pendingPayload() && selectedRecipient !== item.recipient_id) throw failure;
    if (submittedDraft) await storeSubmittedDraft({...submittedDraft, holdRequestId: heldFetch.newId()});
    result = await sendItem();
   }
   const expectedId = item.kind === 'scheduled' ? /^L[1-9][0-9]*$/ : /^[QPI][1-9][0-9]*$/;
   if (typeof result.id !== 'string' || !expectedId.test(result.id)) {
    throw new Error('The server did not return a saved item ID.');
   }
   saved = result;
  } catch (failure) {
   if (!dialog.open) dialog.showModal();
   if (failure instanceof SendUndone) {
    if (!hadEarlierSend) {
     clearSubmittedDraft();
     awaitingEarlierSend = false;
     if (captureSession) captureSession.sent = false;
     storeListDraft();
    }
    setError(failure.message);
    return;
   }
   if (posted && !failure?.definiteRefusal && submittedDraft && !isRecovering()) {
    showSubmittedDraft(submittedDraft);
    try { await storeSubmittedDraft(submittedDraft); } catch (_) {}
   }
   const chooseCapture = failure?.screenshotExpired && isRecovering();
   if (chooseCapture) {
    submittedDraft.mode = 'screenshot-expired';
    try { await storeSubmittedDraft(submittedDraft); } catch (_) {}
    discardCapture(!onList);
    showSentDetails();
   } else if (failure?.definiteRefusal) {
    if (!failure.staleToken || !hadEarlierSend) {
     const recovering = isRecovering();
     clearSubmittedDraft();
     if (recovering) {
      showLaterDraft();
      showRecipients();
      showBugPage();
      showSentDetails();
     }
    }
    awaitingEarlierSend = awaitingNativeRecovery || (failure.staleToken && hadEarlierSend);
    storeListDraft();
   }
   if (failure?.filedId) {
    releasePageCapture();
    finishDraft();
    void openDraftForm({focus: send});
    const warning = onList && failure.listFilingSkipped ? ' No List row was filed.' : '';
    setError('This item was already filed as ' + failure.filedId + '. Any change after that Send was not sent.' + warning);
   } else {
    setError(chooseCapture ? '' : failure?.message || 'Could not send this item.');
    if (failure?.screenshotExpired && !chooseCapture && includePage.checked) {
     discardCapture(!onList);
     void renewCapture();
    } else if (onList && (failure?.staleToken || failure instanceof TypeError)) {
     if (!pendingPayload() && (failure?.staleToken || captureSession?.preview.status !== 'ready')) discardCapture();
     const signal = opening.signal;
     const fresh = await loadOptions(signal).catch(() => null);
     if (!signal.aborted && fresh) {
      setOptions(fresh);
      if (selectedRecipient === null) setError('');
      startCapture(true);
     }
    }
   }
  } finally {
   setSending(false);
  }
  if (saved?.attachment_warning) {
   if (!dialog.open) dialog.showModal();
   showConfirmation(saved);
   dialog.dispatchEvent(new CustomEvent('review-item-saved', {bubbles: true, detail: saved}));
  } else if (saved) {
   const keepEdits = Boolean(laterDraft);
   releasePageCapture(usedPreview);
   finishDraft();
   showLaterDraft();
   showSentDetails();
   if (keepEdits) {
    const place = onList && listFilingSkipped(saved) ? ' in Review. No List row was filed.' : '.';
    void openDraftForm({focus: send});
    setError('Saved ' + saved.id + place + ' Your edits are still in the dialog.');
    dialog.dispatchEvent(new CustomEvent('review-item-saved', {bubbles: true, detail: saved}));
   } else if (onList && listFilingSkipped(saved)) {
    words.value = '';
    replaceImages([]);
    if (!dialog.open) dialog.showModal();
    showConfirmation(saved, {missingListRow: true});
    dialog.dispatchEvent(new CustomEvent('review-item-saved', {bubbles: true, detail: saved}));
   } else if (!saved.id.startsWith('L')) {
    close({sent: true});
    const target = dialog.isConnected ? dialog : document;
    if (!onList) target.dispatchEvent(new CustomEvent('review-item-saved', {bubbles: true, detail: saved}));
    target.dispatchEvent(new CustomEvent('review-item-created', {bubbles: true, detail: saved}));
   } else {
    showConfirmation(saved);
    dialog.dispatchEvent(new CustomEvent('review-item-saved', {bubbles: true, detail: saved}));
   }
  } else {
   send.focus();
   return;
  }
 });

 const draft = {
  dialog, open,
  get id() { return draftId; },
  get source() { return source; },
  get sending() { return sending; },
  pagehide() { if (!sending) discardCapture(); },
  pageshow() { if (dialog.open) startCapture(); },
 };
 return draft;
}

 drafts.push(bindDraft(original));
 function newDraft() {
  const dialog = template.cloneNode(true), suffix = '-' + (++nextDraft);
  dialog.dataset.newItemDraft = '';
  dialog.id += suffix;
  for (const element of dialog.querySelectorAll('[id]')) element.id += suffix;
  for (const attr of ['aria-labelledby', 'aria-describedby', 'for'])
   for (const element of [dialog, ...dialog.querySelectorAll('[' + attr + ']')]) {
    if (element.hasAttribute(attr))
     element.setAttribute(attr, element.getAttribute(attr).split(' ').map(id => id + suffix).join(' '));
   }
  original.after(dialog);
  const draft = bindDraft(dialog, true);
  drafts.push(draft);
  return draft;
 }
 window.addEventListener('pagehide', () => drafts.forEach(draft => draft.pagehide()));
 window.addEventListener('pageshow', event => {
  if (event.persisted) drafts.forEach(draft => draft.pageshow());
 });
 function openDraft(trigger, page, key, options) {
  const active = drafts.find(draft => draft.dialog.open && draft.dialog.contains(trigger));
  if (!active && drafts.some(draft => draft.dialog.open)) return null;
  const available = drafts.filter(draft => !draft.sending);
  const draft = active || available.findLast(draft => draft.source === key)
   || available.findLast(draft => !draft.source) || newDraft();
  void draft.open(trigger, page, key, options);
  return draft.dialog;
 }
 window.reviewNewItem = {
  open: ({trigger, kind = 'question', recipientName, evidence: capture, tipPreview: shownTip}) =>
   openDraft(trigger, '', 'step:' + capture.step_id, {kind, recipientName, capture, shownTip}),
  evidenceLimits: Object.freeze(limits.buildEvidence)
 };
 document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-owner-new]');
  const page = trigger?.closest('[data-page-owner]')?.dataset.pageOwner;
  if (page) openDraft(trigger, page, 'page:' + page);
 });
})();'''
