"""Render the shared New item dialog."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast

from scripts.production import staff
from scripts.review_links import page_frame


MAX_WORDS_LENGTH = 40_000
MAX_REQUEST_ID_LENGTH = 210
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PAGE_DATA_BYTES = 64 * 1024
MAX_PAGE_ROWS = 48
MAX_BUG_PAGE_STATE_BYTES = 512 * 1024
MAX_PAGE_URL_LENGTH = 8192
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


@dataclass(frozen=True)
class MemoryReading:
    free_for_next_gib: float
    queue_cap_gib: float
    segments: tuple[MemorySegment, ...]


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
class PageDataEvidence:
    """Keep the visible data when the server cannot take a screenshot."""

    title: str
    rows: tuple[tuple[str, ...], ...]
    kind: Literal["page-data"] = field(default="page-data", init=False)


type Evidence = NoEvidence | BuildStepEvidence | PageDataEvidence


@dataclass(frozen=True)
class PageCaptureRequest:
    """Describe the browser viewport for a console page capture."""

    place: str
    width: int
    height: int
    dark: bool


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


@dataclass(frozen=True)
class NewItemRequest:
    request_id: str
    kind: SubmissionKind
    recipient_id: staff.StaffId
    words: str
    evidence: Evidence
    images: tuple[PastedImage, ...] = ()
    page: str = ""
    page_state: BugPageState | None = None
    page_recipient: PageRecipient = PageRecipient.PAGE_OWNER


@dataclass(frozen=True)
class NoReviewSubmission:
    """The item came from its native filing path."""


@dataclass(frozen=True)
class ReviewSubmission:
    request_id: str
    request_digest: str
    evidence: Evidence
    images: tuple[ImageReference, ...]
    reply_token: str = field(repr=False)
    page: str = ""
    page_state: BugPageState | None = None
    page_state_data: PageDataEvidence | None = None

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
            return TipCaption(_text(row.get("text"), "Tooltip caption", 4000))
        case "field":
            return TipField(_text(row.get("label"), "Tooltip label", 200),
                            _text(row.get("value"), "Tooltip value", 4000, allow_empty=True))
        case "detail":
            return TipDetail(_text(row.get("text"), "Tooltip detail", 4000))
        case _:
            raise SubmissionError("Select a valid tooltip entry type.")


def read_evidence(value: object) -> Evidence:
    """Decode the structured evidence supplied by the dialog."""
    data = _mapping(value, "Evidence")
    match data.get("kind"):
        case "none":
            return NoEvidence()
        case "page-data":
            rows = tuple(tuple(_text(cell, "Page value", 240, allow_empty=True)
                               for cell in _entries(row, "Page row", 16))
                         for row in _entries(data.get("rows"), "Page rows", MAX_PAGE_ROWS))
            return PageDataEvidence(_text(data.get("title"), "Page title", 240), rows)
        case "build-step":
            captured_at = _text(data.get("captured_at"), "Capture time", 80)
            _ = _time(captured_at)
            cells = tuple(
                BuildCell(_text(cell.get("label"), "Cell label", 200),
                          _text(cell.get("value"), "Cell value", 4000, allow_empty=True))
                for entry in _entries(data.get("cells"), "Row cells", 32)
                for cell in (_mapping(entry, "Row cell"),))
            memory = _mapping(data.get("memory"), "Memory reading")
            segments = tuple(
                MemorySegment(_text(segment.get("label"), "Memory label", 200),
                              _gib(segment.get("gib")), MemoryTone(segment.get("tone")))
                for entry in _entries(memory.get("segments"), "Memory segments", 32)
                for segment in (_mapping(entry, "Memory segment"),))
            return BuildStepEvidence(
                captured_at, _text(data.get("step_id"), "Step ID", 300),
                _text(data.get("title"), "Step title", 300),
                _text(data.get("owner"), "Step owner", 300, allow_empty=True), cells,
                MemoryReading(_gib(memory.get("free_for_next_gib")), _gib(memory.get("queue_cap_gib")),
                              segments),
                _text(data.get("tip_title"), "Tooltip title", 300),
                _text(data.get("tip_status"), "Tooltip status", 300, allow_empty=True),
                tuple(_tip_entry(entry) for entry in _entries(data.get("tip"), "Tooltip entries", 64)))
        case _:
            raise SubmissionError("Select a valid evidence type.")


def read_capture_key(value: object) -> str:
    """Check a key issued for one dialog session."""
    key = _text(value, "Capture key", 32)
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise SubmissionError("Invalid capture key.")
    return key


def read_page_capture(value: object) -> PageCaptureRequest:
    """Check a viewport before starting Firefox."""
    data = _mapping(value, "Page capture")
    width, height, dark = data.get("width"), data.get("height"), data.get("dark")
    if (type(width) is not int or not 240 <= width <= 4096
            or type(height) is not int or not 200 <= height <= 2160 or not isinstance(dark, bool)):
        raise SubmissionError("The page size is outside the screenshot limits.")
    place = _text(data.get("place"), "Page address", 2048)
    if not place.startswith("/") or place.startswith("//") or re.search(r"[\\\x00-\x20\x7f]", place):
        raise SubmissionError("Use a console page address.")
    return PageCaptureRequest(place, width, height, dark)


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


def read_request(value: object, *, has_capture: bool = False) -> NewItemRequest:
    """Decode the JSON fields for Send."""
    data = _mapping(value, "New item")
    page = _text(data.get("page", ""), "Console page", 32, allow_empty=True)
    kind = read_kind(data.get("kind"))
    if page and page not in page_frame.OWNER_PAGES:
        raise SubmissionError("Open the New item dialog from a page owner.")
    try:
        page_recipient = PageRecipient(data.get("page_recipient", PageRecipient.PAGE_OWNER))
    except ValueError as error:
        raise SubmissionError("Select a recipient from the New item dialog.") from error
    if not page and page_recipient != PageRecipient.PAGE_OWNER:
        raise SubmissionError("Select a recipient from the New item dialog.")
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
    if page_state is not None and len(
            json.dumps(asdict(evidence), ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PAGE_DATA_BYTES:
        raise SubmissionError("The Bug page data exceeds its size limit.")
    images = tuple(
        PastedImage(_text(image.get("media_type"), "Image type", 32),
                    _text(image.get("base64_data"), "Image data", max_base64_size()))
        for entry in _entries(data.get("images"), "Images", MAX_IMAGES)
        for image in (_mapping(entry, "Image"),))
    return NewItemRequest(
        _text(data.get("request_id"), "Request ID", MAX_REQUEST_ID_LENGTH), kind,
        staff.StaffId(_text(data.get("recipient_id"), "Recipient ID", 128)),
        _text(data.get("words"), "Your words", MAX_WORDS_LENGTH),
        evidence, images, page, page_state, page_recipient)


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
    if details.page_state is None:
        _ = fields.pop("page_state")
    if details.page_state_data is None:
        _ = fields.pop("page_state_data")
    return fields


def read_submission(value: object) -> ReviewSubmission:
    row = _mapping(value, "Review submission")
    images = _entries(row.get("images"), "Image references", MAX_IMAGES)
    page = _text(row.get("page", ""), "Console page", 32, allow_empty=True)
    if page and page not in page_frame.OWNER_PAGES:
        raise SubmissionError("Invalid page in Review submission.")
    data = read_evidence(row["page_state_data"]) if row.get("page_state_data") is not None else None
    if data is not None and not isinstance(data, PageDataEvidence):
        raise SubmissionError("Invalid page data in Review submission.")
    return ReviewSubmission(
        read_request_id(row.get("request_id")), _text(row.get("request_digest"), "Request digest", 64),
        read_evidence(row.get("evidence")), tuple(read_image_reference(image) for image in images),
        _text(row.get("reply_token"), "Reply token", 43), page, read_bug_page_state(row.get("page_state")), data)


def decode_submission(value: str) -> SubmissionOrigin:
    return read_submission(cast(object, json.loads(value))) if value else NoReviewSubmission()


def require_reply_token(details: SubmissionOrigin, token: str) -> None:
    if not isinstance(details, ReviewSubmission) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token) or not (
            hmac.compare_digest(token, details.reply_token)):
        raise SubmissionError("The reply token does not match this item.")


def new_reply_token() -> str:
    return "r" + secrets.token_urlsafe(31)


def max_base64_size() -> int:
    return 4 * math.ceil(MAX_IMAGE_BYTES / 3)


def read_request_id(value: object) -> str:
    identity = _text(value, "Request ID", MAX_REQUEST_ID_LENGTH)
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", identity):
        raise SubmissionError("Invalid request ID.")
    return identity


def read_words(value: object, label: str) -> str:
    return _text(value, label, MAX_WORDS_LENGTH)



KINDS = ((SubmissionKind.BUG, "Bug", "bug"),
         (SubmissionKind.QUESTION, "Question", "circle-help"),
         ("feedback", "Feedback", "message-circle-question"),
         (SubmissionKind.PROPOSAL, "Proposal", "lamp"),
         (SubmissionKind.INVESTIGATION, "Investigation", "microscope"))


def render() -> str:
    """Render the dialog that the page owner button opens."""
    chips = "".join(
        f'<label class="new-item-chip" data-kind="{kind}"><input type="radio" name="kind" value="{kind}">'
        + f'<span data-button-face>{page_frame.icon_svg(icon)}<span data-kind-label>{label}</span></span></label>'
        for kind, label, icon in KINDS)
    limits = json.dumps({"types": list(IMAGE_FORMATS), "images": MAX_IMAGES,
                         "imageBytes": MAX_IMAGE_BYTES,
                         "totalImageBytes": MAX_TOTAL_IMAGE_BYTES, "pageDataBytes": MAX_PAGE_DATA_BYTES,
                         "pageRows": MAX_PAGE_ROWS,
                         "pageUrlLength": MAX_PAGE_URL_LENGTH})
    return f'''<style>{STYLE}{PAGE_STATE_STYLE}</style>
<dialog id="review-new-item" aria-labelledby="new-item-title">
 <form>
  <header><h2 id="new-item-title" tabindex="-1">New item</h2><button type="button" data-close aria-label="Close">×</button></header>
  <div class="new-item-body">
   <fieldset><legend>Type</legend><div class="new-item-chips">{chips}</div></fieldset>
   <fieldset><legend>To</legend><p data-recipients-status role="status" hidden></p>
    <div class="new-item-chips" data-recipients></div>
   </fieldset>
   <textarea name="words" rows="4" maxlength="{MAX_WORDS_LENGTH}" aria-label="Your words"
    placeholder="Type your words. You can paste an image." required></textarea>
   <section class="page-state-box sent-details" data-sent-details aria-labelledby="sent-details-title">
    <h4 id="sent-details-title">Sent with your words</h4>
    <div data-bug-page-state hidden>
     <dl class="page-state"></dl>
     <p data-visible-text>Visible text from this page (up to {MAX_PAGE_ROWS} rows)</p>
    </div>
    <label class="new-item-page-capture">
     <input type="checkbox" name="include_page" checked><span>Include a screenshot</span>
    </label>
    <div class="capture-slot" data-capture-slot hidden>
     <a data-shot target="_blank" rel="noopener" hidden>
      <img alt="Screenshot of this page">
      <span>Open full size ↗</span>
     </a>
     <div data-taking role="status"><span class="capture-spinner" aria-hidden="true"></span>Taking a screenshot</div>
    </div>
    <p data-capture-expired role="status" hidden>The screenshot expired. A new one is being taken. Send again when it shows.</p>
    <p data-fallback role="status" hidden>Screenshot unavailable. Visible text from this page goes instead (up to {MAX_PAGE_ROWS} rows).</p>
    <div class="new-item-previews" data-previews hidden></div>
   </section>
  </div>
  <footer>
   <p data-send-disabled role="status" hidden></p>
   <p class="new-item-error" role="alert" hidden></p>
   <p id="new-item-saved" role="status" hidden></p>
   <div data-send-controls><button type="button" data-cancel>Cancel</button><button type="submit" disabled>Send</button></div>
   <div data-saved-controls hidden><button type="button" data-done aria-describedby="new-item-saved">Done</button></div>
  </footer>
 </form>
</dialog>
<script>{SCRIPT.replace("__LIMITS__", limits)}</script>'''


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
#review-new-item {
 position: fixed; inset: 0; box-sizing: border-box; margin: auto; padding: 0;
 width: min(560px, calc(100vw - 32px)); max-width: none; max-height: calc(100dvh - 32px);
 color: var(--ink, #f0efec); background: var(--card, #1a1a19); border: 1px solid var(--line, #34332f);
 border-radius: 12px; box-shadow: 0 20px 60px #0006; overflow: hidden;
 font: 15px/1.45 system-ui, sans-serif;
}
#review-new-item *, #review-new-item *::before, #review-new-item *::after { box-sizing: border-box; }
#review-new-item [hidden] { display: none; }
#review-new-item::backdrop { background: #0009; }
#review-new-item form {
 display: grid; grid-template-rows: auto minmax(0, 1fr) auto; margin: 0;
 max-height: calc(100dvh - 34px);
}
#review-new-item header, #review-new-item footer { padding-inline: 24px; }
#review-new-item header {
 display: flex; align-items: center; justify-content: space-between; padding-block: 20px 16px;
}
#review-new-item h2 { margin: 0; font-size: 22px; line-height: 1.2; }
#review-new-item button { margin: 0; min-height: 0; font: inherit; cursor: pointer; }
#review-new-item button:disabled { opacity: .55; cursor: default; }
#review-new-item button:focus-visible, #review-new-item textarea:focus-visible,
#review-new-item input:focus-visible + span { outline: 2px solid var(--accent, #3987e5); outline-offset: 2px; }
#review-new-item [data-close] {
 border: 0; border-radius: 6px; background: transparent; width: 30px; height: 30px;
 color: var(--ink-2, #c3c2b7); font-size: 22px; line-height: 1;
}
#review-new-item .new-item-body:not([hidden]) {
 display: grid; align-content: start; gap: 18px; min-width: 0; padding: 4px 24px 20px;
 overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin;
 scrollbar-color: var(--ink-3, #9a988f) var(--card, #1a1a19);
}
#review-new-item fieldset { min-inline-size: 0; margin: 0; padding: 0; border: 0; }
#review-new-item legend, #review-new-item h3 {
 margin: 0 0 8px; padding: 0; font-size: 11px; line-height: 1.3; font-weight: 500;
 text-transform: uppercase; color: var(--ink-3, #9a988f);
}
#review-new-item .new-item-chips { display: flex; flex-wrap: wrap; gap: 8px; }
#review-new-item .new-item-chip { position: relative; display: inline-flex; max-width: 100%; }
#review-new-item .new-item-chip[hidden] { display: none; }
#review-new-item .new-item-chip input {
 position: absolute; width: 1px; height: 1px; padding: 0; margin: 0; opacity: 0;
}
#review-new-item .new-item-chip > span {
 display: flex; align-items: center; gap: 7px; padding: 7px 14px; border-radius: 999px;
 border: 1px solid var(--line, #34332f); color: var(--ink-2, #c3c2b7);
 font-size: 14px; overflow-wrap: anywhere; cursor: pointer;
}
#review-new-item .new-item-chip input:checked + span,
#review-new-item button[type="submit"], #review-new-item [data-done] {
 background: #2670c5; color: white; border-color: transparent;
}
#review-new-item .new-item-chip svg { width: 16px; height: 16px; flex: none; }
#review-new-item .new-item-chip[data-kind="question"] { --item-icon: #af90ed; }
#review-new-item .new-item-chip[data-kind="proposal"] { --item-icon: #ed7da9; }
#review-new-item .new-item-chip[data-kind="investigation"] { --item-icon: #1cc1b5; }
#review-new-item .new-item-chip[data-kind="bug"] { --item-icon: #ed876b; }
#review-new-item .new-item-chip input:not(:checked) + span svg { color: var(--item-icon); }
#review-new-item [data-recipients-status] { margin: 0; color: var(--ink-2, #c3c2b7); }
#review-new-item textarea {
 display: block; width: 100%; min-width: 0; margin: 0; padding: 10px 12px; resize: vertical;
 border: 1px solid var(--line, #34332f); border-radius: 8px;
 background: var(--bg, #121211); color: inherit; font: 16px/1.45 system-ui, sans-serif;
}
#review-new-item textarea::placeholder { color: var(--ink-3, #9a988f); }
#review-new-item[data-page-bug] textarea { resize: none; }
#review-new-item .new-item-page-capture:not([hidden]) { display: flex; align-items: center; gap: 9px; margin: 0; }
#review-new-item .new-item-page-capture input { margin: 0; width: 16px; height: 16px; accent-color: var(--accent); }
#review-new-item h2:focus { outline: none; }
#review-new-item .sent-details h4 { margin: 0 0 11px; }
#review-new-item [data-bug-page-state] { margin: 0 0 12px; padding: 0 0 12px; border-bottom: 1px solid var(--line); }
#review-new-item [data-bug-page-state] dd { min-width: 0; overflow-wrap: anywhere; }
#review-new-item .sent-details p { margin: 8px 0 0; font-size: 13px; color: var(--ink-2); }
#review-new-item .sent-details .new-item-page-capture { font-size: 14px; }
#review-new-item .sent-details .new-item-previews { margin-top: 10px; }
#review-new-item .capture-slot {
 position: relative; width: min(320px, 100%); aspect-ratio: 1440 / 1000; margin-top: 10px;
 overflow: hidden; background: var(--card); border-radius: 7px; border: 1px solid var(--line);
}
#review-new-item .capture-slot a, #review-new-item [data-taking] { position: absolute; inset: 0; }
#review-new-item .capture-slot img { display: block; width: 100%; height: 100%; object-fit: contain; }
#review-new-item .capture-slot a span {
 position: absolute; right: 8px; bottom: 8px; padding: 4px 8px; font-size: 12px;
 border: 1px solid var(--line); border-radius: 5px; background: var(--bg); color: var(--ink);
}
#review-new-item .capture-slot a:is(:hover, :focus-visible) span { border-color: var(--accent); }
#review-new-item [data-taking]:not([hidden]) {
 display: flex; justify-content: center; align-items: center; gap: 9px; color: var(--ink-2); font-size: 14px;
}
#review-new-item .capture-spinner {
 width: 14px; height: 14px; border: 2px solid var(--line);
 border-top-color: var(--accent); border-right-color: var(--accent); border-radius: 50%;
}
#review-new-item .new-item-previews:not([hidden]) { display: flex; flex-wrap: wrap; gap: 8px; }
#review-new-item .new-item-preview {
 position: relative; border: 1px solid var(--line, #34332f); border-radius: 7px;
 max-width: 100%; background: var(--bg, #121211);
}
#review-new-item .new-item-preview img { display: block; width: 145px; height: 80px; object-fit: contain; }
#review-new-item .new-item-preview button {
 position: absolute; top: 3px; right: 3px; width: 28px; height: 28px; padding: 0;
 border: 1px solid #6c6b66; border-radius: 5px; background: #1a1a19; color: #f0efec;
}
#review-new-item footer { padding-block: 0 20px; }
#review-new-item footer > div {
 display: flex; justify-content: flex-end; gap: 8px; border-top: 1px solid var(--line, #34332f);
 padding-top: 14px;
}
#review-new-item footer button {
 min-height: 38px; padding: 6px 14px; border: 1px solid var(--ink-3, #9a988f); border-radius: 8px;
 background: transparent; color: inherit;
}
#review-new-item [data-send-disabled] { margin: 0 0 8px; color: var(--ink-2); }
#review-new-item .new-item-error { color: var(--error, #ef7372); margin: 0 0 8px; overflow-wrap: anywhere; }
#review-new-item #new-item-saved { margin: 0 0 12px; }
@media (max-width: 480px) {
 #review-new-item header, #review-new-item footer, #review-new-item .new-item-body { padding-inline: 16px; }
 #review-new-item header { padding-block: 14px 12px; }
 #review-new-item footer { padding-bottom: 14px; }
 #review-new-item .new-item-body { gap: 14px; }
}
'''


SCRIPT = r'''(() => {
 const endpoint = '/console/review/new-item';
 const limits = __LIMITS__;
 const dialog = document.querySelector('#review-new-item');
 const form = dialog.querySelector('form'), body = dialog.querySelector('.new-item-body');
 const recipientChips = dialog.querySelector('[data-recipients]');
 const recipientStatus = dialog.querySelector('[data-recipients-status]');
 const words = form.elements.words, previews = dialog.querySelector('[data-previews]');
 const includePage = form.elements.include_page, sentDetails = dialog.querySelector('[data-sent-details]');
 const bugStateBox = dialog.querySelector('[data-bug-page-state]');
 const slot = dialog.querySelector('[data-capture-slot]'), shot = slot.querySelector('[data-shot]');
 const preview = shot.querySelector('img'), taking = slot.querySelector('[data-taking]');
 const fallback = sentDetails.querySelector('[data-fallback]');
 const fallbackText = fallback.textContent;
 const expired = sentDetails.querySelector('[data-capture-expired]');
 const send = dialog.querySelector('[type="submit"]'), error = dialog.querySelector('.new-item-error');
 const sendNotice = dialog.querySelector('[data-send-disabled]');
 const confirmation = dialog.querySelector('#new-item-saved'), done = dialog.querySelector('[data-done]');
 const sendControls = dialog.querySelector('[data-send-controls]'), savedControls = done.parentElement;
 const fields = [...form.querySelectorAll('fieldset'), words, includePage, dialog.querySelector('[data-close]'),
                 dialog.querySelector('[data-cancel]')];
 const listSendKey = 'console.list.pending-send';
 const listDraftId = /^r6-[0-9a-f]{32}$/;
 let launcher, opening, token, recipients, images = [], sending = false;
 let draftId, keepDraft = false, ownerPage = '';
 // Null means the user must choose again. An empty string means the page has no default.
 let selectedRecipient = '';
 let captureSession, pageContext, savedItem;

 function node(tag, text = '', css = '') {
  const element = document.createElement(tag);
  element.textContent = text;
  if (css) element.className = css;
  return element;
 }

 function setError(reason) {
  error.textContent = reason;
  error.hidden = !reason;
 }

 function updateSend() {
  send.disabled = !!savedItem || sending || (!slot.hidden && !taking.hidden) || !token || !recipientChips.querySelector('input:checked') ||
                  !words.value.trim() || words.value.length > words.maxLength;
 }

 function showRecipients() {
  if (!recipients) return;
  const proposal = form.elements.kind.value === 'proposal';
  const available = recipients.filter(recipient => !proposal || recipient.proposal);
  if (selectedRecipient && !available.some(recipient => recipient.id === selectedRecipient)) {
   selectedRecipient = null;
   storeListDraft();
  }
  recipientChips.replaceChildren();
  for (const recipient of available) {
   const chip = node('label', '', 'new-item-chip'), input = node('input');
   input.type = 'radio';
   input.name = 'recipient_id';
   input.value = recipient.id;
   input.checked = recipient.id === selectedRecipient;
   const face = node('span', recipient.name);
   face.setAttribute('data-button-face', '');
   chip.append(input, face);
   recipientChips.append(chip);
  }
  recipientStatus.textContent = selectedRecipient === null
   ? 'The recipient you chose is not available. Choose a recipient.'
   : available.length ? '' : 'No recipients are available.';
  recipientStatus.hidden = !recipientStatus.textContent;
 }

 function editDraft(event) {
  if (sending) return;
  if (event?.target.name === 'kind') {
   showRecipients();
   showBugPage();
  }
  if (event?.target.name === 'recipient_id') {
   selectedRecipient = event.target.value;
   storeListDraft();
   recipientStatus.hidden = true;
  }
  if (event?.target === includePage) startCapture();
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
  if (sending) return;
  const files = [...(event.clipboardData?.files || [])].filter(file => file.type.startsWith('image/'));
  if (!files.length) return;
  event.preventDefault();
  const total = [...images.map(image => image.file), ...files].reduce((sum, file) => sum + file.size, 0);
  if (files.some(file => !limits.types.includes(file.type))) {
   setError('Paste PNG, JPEG, GIF or WebP images.'); return;
  }
  if (images.length + files.length > limits.images || total > limits.totalImageBytes ||
      files.some(file => !file.size || file.size > limits.imageBytes)) {
   setError('Paste up to four images, at most 5 MiB each and 12 MiB total.'); return;
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

 function pastedData(file) {
  return new Promise((resolve, reject) => {
   const reader = new FileReader();
   reader.addEventListener('load', () => resolve({
    media_type: file.type, base64_data: reader.result.split(',', 2)[1]
   }));
   reader.addEventListener('error', () => reject(new Error('Could not read a pasted image.')));
   reader.readAsDataURL(file);
  });
 }

 async function responseData(response, fallback) {
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
   const failure = new Error(data?.error || data?.reason || fallback);
   failure.staleToken = response.status === 403 && data?.code === 'stale_token';
   failure.screenshotExpired = response.status === 409 && data?.code === 'screenshot_expired';
   if (response.status === 409 && data?.code === 'already_filed' &&
       typeof data.id === 'string' && /^[QPI][1-9][0-9]*$/.test(data.id)) {
    failure.filedId = data.id;
   }
   throw failure;
  }
  return data;
 }

 async function loadOptions(signal) {
  const path = endpoint + '?' + new URLSearchParams({page: ownerPage});
  const response = await fetch(path, {cache: 'no-store', signal});
  const data = await responseData(response, 'Could not load recipients.');
  if (!Array.isArray(data.recipients) || typeof data.token !== 'string' || (!data.token && !data.send_disabled)) {
   throw new Error('Could not load recipients.');
  }
  return data;
 }

 function setOptions(data) {
  token = data.token;
  sendNotice.textContent = data.send_disabled || '';
  sendNotice.hidden = !data.send_disabled;
  includePage.closest('label').hidden = !sendNotice.hidden;
  recipients = data.recipients;
  showRecipients();
 }

 async function post(payload, keepalive = false) {
  const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify(payload), keepalive});
  return responseData(response, 'Could not send this item.');
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
  const data = {kind: 'page-data', title, rows: []};
  const encoder = new TextEncoder();
  let bytes = encoder.encode(JSON.stringify(data)).length;
  for (const element of main.querySelectorAll('h2,h3,p,dl,tr')) {
   if (!visible(element)) continue;
   const cells = element.matches('tr,dl') ? [...element.children].slice(0, 16).map(text) : [text(element)];
   if (!cells.some(Boolean)) continue;
   bytes += encoder.encode(JSON.stringify(cells)).length + (data.rows.length ? 1 : 0);
   if (bytes > limits.pageDataBytes) break;
   data.rows.push(cells);
   if (data.rows.length === limits.pageRows) break;
  }
  return data;
 }

 function viewport() {
  const dark = document.documentElement.dataset.theme
   ? document.documentElement.dataset.theme === 'dark'
   : matchMedia('(prefers-color-scheme: dark)').matches;
  return {width: innerWidth, height: innerHeight, dark};
 }

 function collectPageContext() {
  const filters = [];
  for (const chip of document.querySelectorAll('[data-type-chip],[data-show-chip],[data-owner-chip],[data-filter-value]')) {
   if (chip.getAttribute('aria-pressed') !== 'true' || !visible(chip)) continue;
   const group = chip.closest('[role="group"]')?.getAttribute('aria-label');
   if (!group) continue;
   const label = chip.cloneNode(true);
   label.querySelector('b')?.remove();
   filters.push((group === 'Show' ? 'State' : group) + ' ' + clip(label.textContent));
  }
  const search = document.querySelector('#list-search,[data-filter-words]');
  if (search?.value && visible(search)) filters.push('Search ' + clip(search.value));
  const notices = new Set();
  const selector = '[data-review-source-notices] li,[data-review-notice],[data-missing-item],.notice,' +
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
   place: location.pathname + location.search + location.hash,
   viewport: viewport(),
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
  bugStateBox.hidden = !bug;
  fallback.textContent = bug ? 'Screenshot unavailable.' : fallbackText;
  if (!bug || !pageContext) return;
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

 function showSentDetails() {
  const image = captureSession?.image;
  const include = includePage.checked && sendNotice.hidden;
  const unavailable = include && (image === null || !hasCaptureSpace(image));
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
  if ((savedItem && !renewed) || !pageContext || !includePage.checked || !token || captureSession) return;
  const {place, viewport} = pageContext;
  const session = {token, renewed, controller: new AbortController()};
  captureSession = session;
  showSentDetails();
  session.key = post({token, page_capture: {place, ...viewport}})
   .then(result => result.capture_key || '', () => '');
  void session.key.then(async key => {
   if (!key || captureSession !== session) return null;
   const response = await fetch(endpoint + '?' + new URLSearchParams({capture_key: key}),
    {cache: 'no-store', headers: {'X-Review-Token': session.token}, signal: session.controller.signal});
   if (!response.ok) return null;
   const blob = await response.blob();
   return captureSession === session ? {key, size: blob.size, blob, url: URL.createObjectURL(blob)} : null;
  }).catch(() => null).then(image => {
   if (captureSession !== session) return null;
   session.image = image;
   showSentDetails();
   return image;
  });
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
  clearCapturePreview(session);
  showSentDetails();
  if (!used) {
   void session.key.then(key => key && post({token: session.token, discard_capture: key}, true)).catch(() => {});
  }
 }

 function clearCapturePreview(session) {
  if (session.image) URL.revokeObjectURL(session.image.url);
  shot.removeAttribute('href');
  preview.removeAttribute('src');
  session.image = null;
 }

 function storedListDraft() {
  try {
   const stored = sessionStorage.getItem(listSendKey);
   if (!stored) return null;
   if (listDraftId.test(stored)) return {id: stored, recipient: null};
   const draft = JSON.parse(stored);
   if (typeof draft?.id !== 'string' || !listDraftId.test(draft.id)) return null;
   const recipient = typeof draft.recipient === 'string' && draft.recipient ? draft.recipient : null;
   return {id: draft.id, recipient};
  } catch (_) { return null; }
 }

 function storeListDraft() {
  if (ownerPage !== 'list' || !draftId) return;
  try {
   sessionStorage.setItem(listSendKey, JSON.stringify({id: draftId, recipient: selectedRecipient}));
  } catch (_) {}
 }

 function finishDraft() {
  draftId = undefined;
  if (ownerPage === 'list') {
   try { sessionStorage.removeItem(listSendKey); } catch (_) {}
  }
 }

 function showConfirmation(item) {
  savedItem = item;
  const saved = Boolean(item);
  confirmation.textContent = saved ? 'Saved ' + item.id + '.' : '';
  confirmation.hidden = savedControls.hidden = !saved;
  body.hidden = sendControls.hidden = saved;
  updateSend();
  if (saved) done.focus();
 }

 function close({sent = false} = {}) {
  if (!dialog.open || sending) return;
  const result = savedItem;
  discardCapture();
  opening.abort();
  dialog.close();
  keepDraft = !sent && !result && Boolean(words.value || images.length);
  if (!keepDraft) {
   if (ownerPage !== 'list') draftId = undefined;
   for (const image of images) URL.revokeObjectURL(image.url);
   images = [];
   previews.replaceChildren();
   previews.hidden = true;
  }
  if (launcher?.isConnected) launcher.focus({preventScroll: true});
  launcher = null;
  if (result) {
   showConfirmation();
   dialog.dispatchEvent(new CustomEvent('review-item-created', {bubbles: true, detail: result}));
  }
 }

 function setSending(value) {
  sending = value;
  for (const control of [...fields, ...previews.querySelectorAll('button')]) control.disabled = value;
  send.textContent = value ? 'Sending…' : 'Send';
  form.setAttribute('aria-busy', String(value));
  updateSend();
 }

 async function open(trigger, page) {
  if (dialog.open || sending) return;
  ownerPage = page;
  pageContext = collectPageContext();
  launcher = trigger;
  token = '';
  if (ownerPage === 'list' && !draftId) {
   const stored = storedListDraft();
   if (stored) {
    draftId = stored.id;
    selectedRecipient = stored.recipient;
   }
  }
  const newDraft = !keepDraft && !draftId;
  if (newDraft) selectedRecipient = '';
  sendNotice.hidden = true;
  includePage.closest('label').hidden = false;
  recipients = null;
  recipientChips.replaceChildren();
  if (!keepDraft) {
   form.reset();
   setError('');
   form.elements.kind.value = 'bug';
  }
  recipientStatus.textContent = 'Loading recipients…';
  recipientStatus.hidden = false;
  showBugPage();
  showSentDetails();
  updateSend();
  const controller = new AbortController();
  opening = controller;
  dialog.showModal();
  const focus = keepDraft ? words : dialog.querySelector('h2');
  focus.focus({preventScroll: true});
  body.scrollTop = 0;
  try {
   const data = await loadOptions(controller.signal);
   if (controller.signal.aborted) return;
   if (newDraft) selectedRecipient = data.page_owner || '';
   setOptions(data);
   startCapture();
   showSentDetails();
  } catch (failure) {
   if (!controller.signal.aborted) recipientStatus.textContent = failure.message;
  }
 }

 form.addEventListener('input', editDraft);
 window.addEventListener('pagehide', () => { if (!sending) discardCapture(); });
 window.addEventListener('pageshow', event => {
  if (event.persisted && dialog.open) startCapture();
 });
 dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
 for (const control of dialog.querySelectorAll('[data-close], [data-cancel], [data-done]')) {
  control.addEventListener('click', () => close());
 }
 form.addEventListener('submit', async event => {
  event.preventDefault();
  if (send.disabled) return;
  const onList = ownerPage === 'list';
  let saved, captureKey = '';
  setError('');
  setSending(true);
  try {
   const type = form.elements.kind.value, selected = recipientChips.querySelector('input:checked');
   const kind = type === 'feedback' ? 'investigation' : type;
   if (!selected) throw new Error('Select a recipient.');
   if (!draftId) {
    draftId = 'r6-' + Array.from(crypto.getRandomValues(new Uint8Array(16)),
                                byte => byte.toString(16).padStart(2, '0')).join('');
   }
   storeListDraft();
   const screenshot = slot.hidden || shot.hidden ? null : captureSession?.image;
   captureKey = screenshot?.key || '';
   const bug = isPageBug();
   const evidence = bug || !fallback.hidden ? pageContext.data : {kind: 'none'};
   const files = await Promise.all(images.map(image => pastedData(image.file)));
   const item = {request_id: draftId, kind, recipient_id: selected.value, words: words.value,
                 page: ownerPage, page_recipient: 'selected', evidence, images: files,
                 ...(captureKey && {capture_key: captureKey}), ...(bug && {page_state: pageContext.state})};
   const sendItem = () => post({...item, token});
   let result;
   try {
    result = await sendItem();
   } catch (failure) {
    if (onList || !failure?.staleToken) throw failure;
    const fresh = await loadOptions(opening.signal);
    setOptions(fresh);
    if (selectedRecipient !== selected.value) return;
    result = await sendItem();
   }
   if (typeof result.id !== 'string' || !/^[QPI][1-9][0-9]*$/.test(result.id)) {
    throw new Error('The server did not return a saved item ID.');
   }
   saved = result;
  } catch (failure) {
   if (failure?.filedId) {
    finishDraft();
    setError('This item was already filed as ' + failure.filedId + '. Any change after that Send was not sent.');
    discardCapture();
    void renewCapture();
   } else {
    setError(failure?.message || 'Could not send this item.');
    if (failure?.screenshotExpired && includePage.checked) {
     discardCapture(!onList);
     void renewCapture();
    } else if (onList && failure?.staleToken) {
     discardCapture();
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
  if (saved) {
   finishDraft();
   discardCapture(Boolean(captureKey));
   if (onList) {
    close({sent: true});
    dialog.dispatchEvent(new CustomEvent('review-item-created', {bubbles: true, detail: saved}));
   } else {
    showConfirmation(saved);
    dialog.dispatchEvent(new CustomEvent('review-item-saved', {bubbles: true, detail: saved}));
   }
  } else {
   send.focus();
  }
 });

 document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-owner-new]');
  const page = trigger?.closest('[data-page-owner]')?.dataset.pageOwner;
  if (page) void open(trigger, page);
 });
})();'''
