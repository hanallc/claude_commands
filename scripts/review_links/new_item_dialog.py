"""Render the New item dialog shared by Review and Builds."""
from __future__ import annotations

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
MAX_REQUEST_ID_LENGTH = 192
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
BUILD_EVIDENCE_LIMITS = {"title": 300, "label": 200, "text": 4_000, "tipEntries": 64}
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}


class SubmissionError(ValueError):
    """The request cannot be saved or applied to this item."""


class SubmissionConflict(SubmissionError):
    """The request ID already names different item data."""

    status: Literal[409] = 409


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
    free_for_next_gib: float | None
    queue_cap_gib: float | None
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


type Evidence = NoEvidence | BuildStepEvidence


@dataclass(frozen=True)
class PastedImage:
    media_type: str
    base64_data: str


@dataclass(frozen=True)
class ImageReference:
    id: str
    file: str


@dataclass(frozen=True)
class NewItemRequest:
    request_id: str
    kind: SubmissionKind
    recipient_id: staff.StaffId
    words: str
    evidence: Evidence
    images: tuple[PastedImage, ...] = ()


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
    binding = re.fullmatch(r"r1-[0-9a-f]{32}-(.+)", request_id)
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


def read_evidence(value: object) -> Evidence:
    """Decode the structured evidence supplied by the dialog."""
    data = _mapping(value, "Evidence")
    match data.get("kind"):
        case "none":
            return NoEvidence()
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
                              _gib(segment.get("gib")), MemoryTone(segment.get("tone")))
                for entry in _entries(memory.get("segments"), "Memory segments", 32)
                for segment in (_mapping(entry, "Memory segment"),))
            return BuildStepEvidence(
                captured_at, _text(data.get("step_id"), "Step ID", BUILD_EVIDENCE_LIMITS["title"]),
                _text(data.get("title"), "Step title", BUILD_EVIDENCE_LIMITS["title"]),
                _text(data.get("owner"), "Step owner", BUILD_EVIDENCE_LIMITS["title"], allow_empty=True), cells,
                MemoryReading(_optional_gib(memory.get("free_for_next_gib", "")),
                              _optional_gib(memory.get("queue_cap_gib", "")),
                              segments),
                _text(data.get("tip_title"), "Tooltip title", BUILD_EVIDENCE_LIMITS["title"]),
                _text(data.get("tip_status"), "Tooltip status", BUILD_EVIDENCE_LIMITS["title"], allow_empty=True),
                tuple(_tip_entry(entry) for entry in _entries(data.get("tip"), "Tooltip entries", BUILD_EVIDENCE_LIMITS["tipEntries"])))
        case _:
            raise SubmissionError("Select a valid evidence type.")


def read_request(value: object) -> NewItemRequest:
    """Decode the JSON fields after the route checks its form token."""
    data = _mapping(value, "New item")
    images = tuple(
        PastedImage(_text(image.get("media_type"), "Image type", 32),
                    _text(image.get("base64_data"), "Image data", max_base64_size()))
        for entry in _entries(data.get("images"), "Images", MAX_IMAGES)
        for image in (_mapping(entry, "Image"),))
    return NewItemRequest(
        _text(data.get("request_id"), "Request ID", MAX_REQUEST_ID_LENGTH), read_kind(data.get("kind")),
        staff.StaffId(_text(data.get("recipient_id"), "Recipient ID", 128)),
        _text(data.get("words"), "Your words", MAX_WORDS_LENGTH),
        read_evidence(data.get("evidence")), images)


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
    return cast(dict[str, object], asdict(details))


def read_submission(value: object) -> ReviewSubmission:
    row = _mapping(value, "Review submission")
    images = _entries(row.get("images"), "Image references", MAX_IMAGES)
    return ReviewSubmission(
        read_request_id(row.get("request_id")), _text(row.get("request_digest"), "Request digest", 64),
        read_evidence(row.get("evidence")), tuple(read_image_reference(image) for image in images),
        _text(row.get("reply_token"), "Reply token", 43))


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



KINDS = ((SubmissionKind.QUESTION, "Question", "circle-help"),
         (SubmissionKind.PROPOSAL, "Proposal", "lamp"),
         (SubmissionKind.INVESTIGATION, "Investigation", "microscope"),
         (SubmissionKind.BUG, "Bug", "bug"))


def render() -> str:
    """Mount the dialog once for window.reviewNewItem.open({trigger, kind, recipientName, evidence, tipPreview})."""
    chips = "".join(
        f'<label class="new-item-chip" data-kind="{kind}"><input type="radio" name="kind" value="{kind}">'
        + f'<span>{page_frame.icon_svg(icon)}{label}</span></label>'
        for kind, label, icon in KINDS)
    limits = json.dumps({"types": list(IMAGE_FORMATS), "images": MAX_IMAGES,
                         "imageBytes": MAX_IMAGE_BYTES,
                         "totalImageBytes": MAX_TOTAL_IMAGE_BYTES,
                         "buildEvidence": BUILD_EVIDENCE_LIMITS})
    return f'''<style>{STYLE}</style>
<dialog id="review-new-item" aria-labelledby="new-item-title">
 <form>
  <header><h2 id="new-item-title">New item</h2><button type="button" data-close aria-label="Close">×</button></header>
  <div class="new-item-body">
   <fieldset><legend>Type</legend><div class="new-item-chips">{chips}</div></fieldset>
   <fieldset><legend>To</legend><p data-recipients-status role="status" hidden></p>
    <div class="new-item-chips" data-recipients></div>
   </fieldset>
   <section data-evidence aria-labelledby="new-item-evidence-title" hidden>
    <h3 id="new-item-evidence-title">Evidence</h3>
    <div class="new-item-evidence"></div>
   </section>
   <textarea name="words" rows="4" maxlength="{MAX_WORDS_LENGTH}" aria-label="Your words"
    placeholder="Type your words. You can paste an image." required></textarea>
   <div class="new-item-previews" data-previews hidden></div>
  </div>
  <footer>
   <p class="new-item-error" role="alert" hidden></p>
   <div><button type="button" data-cancel>Cancel</button><button type="submit" disabled>Send</button></div>
  </footer>
 </form>
</dialog>
<script>{SCRIPT.replace("__LIMITS__", limits)}</script>'''


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
#review-new-item button { font: inherit; cursor: pointer; }
#review-new-item button:disabled { opacity: .55; cursor: default; }
#review-new-item button:focus-visible, #review-new-item textarea:focus-visible,
#review-new-item input:focus-visible + span { outline: 2px solid var(--accent, #3987e5); outline-offset: 2px; }
#review-new-item [data-close] {
 border: 0; border-radius: 6px; background: transparent; width: 30px; height: 30px;
 color: var(--ink-2, #c3c2b7); font-size: 22px; line-height: 1;
}
#review-new-item [data-close]:hover { background: var(--hover, #242422); }
#review-new-item .new-item-body {
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
#review-new-item .new-item-chip input {
 position: absolute; width: 1px; height: 1px; padding: 0; margin: 0; opacity: 0;
}
#review-new-item .new-item-chip span {
 display: flex; align-items: center; gap: 7px; padding: 7px 14px; border-radius: 999px;
 border: 1px solid var(--line, #34332f); color: var(--ink-2, #c3c2b7);
 font-size: 14px; overflow-wrap: anywhere; cursor: pointer;
}
#review-new-item .new-item-chip:hover span { background: var(--hover, #242422); }
#review-new-item .new-item-chip input:checked + span,
#review-new-item button[type="submit"] { background: #2670c5; color: white; border-color: transparent; }
#review-new-item .new-item-chip svg { width: 16px; height: 16px; flex: none; }
#review-new-item .new-item-chip[data-kind="question"] { --item-icon: #af90ed; }
#review-new-item .new-item-chip[data-kind="proposal"] { --item-icon: #ed7da9; }
#review-new-item .new-item-chip[data-kind="investigation"] { --item-icon: #1cc1b5; }
#review-new-item .new-item-chip[data-kind="bug"] { --item-icon: #ed876b; }
#review-new-item .new-item-chip input:not(:checked) + span svg { color: var(--item-icon); }
#review-new-item [data-recipients-status] { margin: 0; color: var(--ink-2, #c3c2b7); }
#review-new-item textarea {
 display: block; width: 100%; min-width: 0; padding: 10px 12px; resize: vertical;
 border: 1px solid var(--line, #34332f); border-radius: 8px;
 background: var(--bg, #121211); color: inherit; font: 16px/1.45 system-ui, sans-serif;
}
#review-new-item textarea::placeholder { color: var(--ink-3, #9a988f); }
#review-new-item .new-item-evidence {
 background: #141413; color: #f0efec; border: 1px solid #34332f; border-radius: 8px;
 padding: 10px; font-size: 12px; min-width: 0; overflow-wrap: anywhere;
}
#review-new-item .new-item-evidence h4 { font-size: inherit; margin: 0 0 4px; }
#review-new-item .new-item-evidence-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0 8px; }
#review-new-item .new-item-muted { color: #c3c2b7; }
#review-new-item .new-item-cells {
 display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 10px; margin: 7px 0 10px;
}
#review-new-item .new-item-cells div { min-width: 0; max-width: 100%; }
#review-new-item .new-item-cells dt { font-weight: 600; color: #c3c2b7; }
#review-new-item .new-item-cells dd, #review-new-item .new-item-tip dd { margin: 0; white-space: pre-wrap; }
#review-new-item .new-item-memory { padding: 8px 0; border-block: 1px solid #34332f; margin-bottom: 8px; }
#review-new-item .new-item-memory p { margin: 0; }
#review-new-item .new-item-memory strong { font-size: 16px; }
#review-new-item .new-item-cap { color: #ef7372; font-weight: 650; }
#review-new-item .new-item-memory-bar { display: flex; height: 12px; margin: 6px 0; }
#review-new-item .new-item-memory-bar span { min-width: 0; background: var(--segment); border-radius: 2px; }
#review-new-item .new-item-memory-legend { display: flex; flex-wrap: wrap; gap: 3px 12px; color: #c3c2b7; }
#review-new-item .new-item-memory-legend span::before {
 content: ''; display: inline-block; width: 7px; height: 7px; margin-right: 4px;
 border-radius: 2px; background: var(--segment);
}
#review-new-item [data-tone="build"] { --segment: #3987e5; }
#review-new-item [data-tone="free"] { --segment: #199e70; }
#review-new-item [data-tone="tools"] { --segment: #c98500; }
#review-new-item [data-tone="system"] { --segment: #9585e0; }
#review-new-item [data-tone="other"] { --segment: #6c6b66; }
#review-new-item .new-item-tip {
 background: #242422; color: #f0efec; border: 1px solid #34332f; border-radius: 8px;
 padding: 10px 12px; font-size: .85rem; line-height: 1.45;
}
#review-new-item .new-item-tip dl { margin: 0; }
#review-new-item .new-item-tip dl > div {
 display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr); gap: 12px;
}
#review-new-item .new-item-tip dd { text-align: right; font-variant-numeric: tabular-nums; }
#review-new-item .new-item-tip p { margin: 3px 0; color: #c3c2b7; white-space: pre-wrap; }
#review-new-item .new-item-tip h4 { font-weight: 700; }
#review-new-item .new-item-previews { display: flex; flex-wrap: wrap; gap: 8px; }
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
#review-new-item .new-item-error { color: var(--error, #ef7372); margin: 0 0 8px; overflow-wrap: anywhere; }
@media (max-width: 480px) {
 #review-new-item header, #review-new-item footer, #review-new-item .new-item-body { padding-inline: 16px; }
 #review-new-item header { padding-block: 14px 12px; }
 #review-new-item footer { padding-bottom: 14px; }
 #review-new-item .new-item-body { gap: 14px; }
 #review-new-item .new-item-cells { display: grid; grid-template-columns: repeat(auto-fit, minmax(42px, 1fr)); }
}
'''


SCRIPT = r'''(() => {
 const endpoint = '/console/review/new-item';
 const limits = __LIMITS__;
 const dialog = document.querySelector('#review-new-item');
 const form = dialog.querySelector('form'), body = dialog.querySelector('.new-item-body');
 const recipientChips = dialog.querySelector('[data-recipients]');
 const recipientStatus = dialog.querySelector('[data-recipients-status]');
 const evidenceSection = dialog.querySelector('[data-evidence]');
 const words = form.elements.words, previews = dialog.querySelector('[data-previews]');
 const send = dialog.querySelector('[type="submit"]'), error = dialog.querySelector('.new-item-error');
 const fields = [...form.querySelectorAll('fieldset'), words, dialog.querySelector('[data-close]'),
                 dialog.querySelector('[data-cancel]')];
 const failedDrafts = new Map();
 let launcher, opening, evidence, tipPreview, draftKey, token, recipients, images = [], sending = false;
 let draftRevision = 0, sentRevision = -1, requestId, keepDraft = false;

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

 function showEvidence(tipPreview) {
  const box = evidenceSection.querySelector('.new-item-evidence');
  box.replaceChildren();
  evidenceSection.hidden = evidence.kind !== 'build-step';
  if (evidenceSection.hidden) return;
  const heading = node('div', '', 'new-item-evidence-heading');
  heading.append(node('h4', evidence.title), node('span', evidence.owner, 'new-item-muted'));
  const cells = node('dl', '', 'new-item-cells');
  for (const cell of evidence.cells) cells.append(captionAndValue(cell.label, cell.value));

  const memory = node('div', '', 'new-item-memory');
  memory.append(node('h4', 'Overall memory'));
  const figures = node('div', '', 'new-item-evidence-heading'), free = node('p');
  const amount = gib => gib === null ? 'unavailable' : gib.toFixed(1) + ' GiB';
  free.append(node('strong', amount(evidence.memory.free_for_next_gib)), ' Free for the next step');
  figures.append(free, node('p', 'Queue cap ' + amount(evidence.memory.queue_cap_gib), 'new-item-cap'));
  memory.append(figures);
  const bar = node('div', '', 'new-item-memory-bar');
  bar.setAttribute('aria-hidden', 'true');
  const legend = node('div', '', 'new-item-memory-legend');
  for (const segment of evidence.memory.segments) {
   const section = node('span'), key = node('span', segment.label + ' ' + segment.gib.toFixed(1) + ' GiB');
   section.dataset.tone = key.dataset.tone = segment.tone;
   section.style.flexGrow = String(segment.gib);
   bar.append(section);
   legend.append(key);
  }
  if (legend.childElementCount) memory.append(bar, legend);

  if (tipPreview) {
   box.append(heading, cells, memory, tipPreview);
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
  box.append(heading, cells, memory, tip);
 }

 function setError(reason) {
  error.textContent = reason;
  error.hidden = !reason;
 }

 function updateSend() {
  send.disabled = sending || !token || !recipientChips.querySelector('input:checked') ||
                  !words.value.trim() || words.value.length > words.maxLength;
 }

 function showRecipients(recipientName, previousRecipient) {
  if (!recipients) return;
  const available = recipients.filter(recipient => form.elements.kind.value !== 'proposal' || recipient.proposal);
  const selected = recipientChips.querySelector('input:checked')?.value ?? previousRecipient ??
                   available.find(recipient => recipient.name === recipientName)?.id;
  recipientChips.replaceChildren();
  for (const recipient of available) {
   const chip = node('label', '', 'new-item-chip'), input = node('input');
   input.type = 'radio';
   input.name = 'recipient_id';
   input.value = recipient.id;
   input.checked = recipient.id === selected;
   chip.append(input, node('span', recipient.name));
   recipientChips.append(chip);
  }
  recipientStatus.textContent = available.length ? '' : 'No recipients are available.';
  recipientStatus.hidden = !!available.length;
 }

 function editDraft(event) {
  if (event?.target.name === 'kind') showRecipients();
  draftRevision += 1;
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
  previews.hidden = false;
  editDraft();
 });

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
   throw failure;
  }
  return data;
 }

 async function loadOptions(signal) {
  const response = await fetch(endpoint, {cache: 'no-store', signal});
  const data = await responseData(response, 'Could not load recipients.');
  if (!Array.isArray(data.recipients) || typeof data.token !== 'string' || !data.token) {
   throw new Error('Could not load recipients.');
  }
  return data;
 }

 async function post(payload) {
  const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify(payload)});
  return responseData(response, 'Could not send this item.');
 }

 function close() {
  if (!dialog.open || sending) return;
  opening.abort();
  dialog.close();
  if (keepDraft) {
   failedDrafts.set(draftKey, {evidence, tipPreview, images, draftRevision, sentRevision, requestId,
    words: words.value, kind: form.elements.kind.value, error: error.textContent,
    recipient: recipientChips.querySelector('input:checked')?.value});
  } else {
   for (const image of images) URL.revokeObjectURL(image.url);
  }
  images = [];
  previews.replaceChildren();
  previews.hidden = true;
  if (launcher?.isConnected) launcher.focus({preventScroll: true});
  launcher = null;
 }

 function setSending(value) {
  sending = value;
  for (const control of [...fields, ...previews.querySelectorAll('button')]) control.disabled = value;
  send.textContent = value ? 'Sending…' : 'Send';
  form.setAttribute('aria-busy', String(value));
  updateSend();
 }

 async function open({trigger, kind = 'question', recipientName, evidence: capture = {kind: 'none'}, tipPreview: preview} = {}) {
  if (dialog.open || sending) return;
  launcher = trigger || document.activeElement;
  token = '';
  draftKey = capture.kind === 'build-step' ? capture.step_id : null;
  const draft = failedDrafts.get(draftKey);
  failedDrafts.delete(draftKey);
  keepDraft = !!draft;
  evidence = draft?.evidence ?? structuredClone(capture);
  tipPreview = draft ? draft.tipPreview : preview;
  recipients = null;
  draftRevision = draft?.draftRevision ?? 0;
  sentRevision = draft?.sentRevision ?? -1;
  requestId = draft?.requestId;
  form.reset();
  words.value = draft?.words ?? '';
  setError(draft?.error ?? '');
  recipientChips.replaceChildren();
  const kinds = [...form.querySelectorAll('input[name="kind"]')];
  (kinds.find(input => input.value === (draft?.kind ?? kind)) || kinds[0]).checked = true;
  images = draft?.images ?? [];
  previews.replaceChildren(...images.map(image => image.row));
  previews.hidden = !images.length;
  showEvidence(tipPreview);
  recipientStatus.textContent = 'Loading recipients…';
  recipientStatus.hidden = false;
  updateSend();
  const controller = new AbortController();
  opening = controller;
  dialog.showModal();
  const focus = keepDraft ? words : form.querySelector('input[name="kind"]:checked');
  focus.focus({preventScroll: true});
  body.scrollTop = 0;
  try {
   const data = await loadOptions(controller.signal);
   if (controller.signal.aborted) return;
   token = data.token;
   recipients = data.recipients;
   showRecipients(draft ? undefined : recipientName, draft?.recipient);
   updateSend();
  } catch (failure) {
   if (!controller.signal.aborted) recipientStatus.textContent = failure.message;
  }
 }

 form.addEventListener('input', editDraft);
 dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
 for (const control of dialog.querySelectorAll('[data-close], [data-cancel]')) {
  control.addEventListener('click', () => close());
 }
 form.addEventListener('submit', async event => {
  event.preventDefault();
  if (send.disabled) return;
  const kind = form.elements.kind.value, recipient = recipientChips.querySelector('input:checked');
  if (sentRevision !== draftRevision) {
   const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)),
                            byte => byte.toString(16).padStart(2, '0')).join('');
   requestId = ['r1', nonce, kind, recipient.value].join('-');
   sentRevision = draftRevision;
  }
  const payload = {token, request_id: requestId, kind, recipient_id: recipient.value,
                   words: words.value, evidence};
  setError('');
  setSending(true);
  let saved;
  try {
   payload.images = await Promise.all(images.map(image => pastedData(image.file)));
   let result;
   try {
    result = await post(payload);
   } catch (failure) {
    if (!failure.staleToken) throw failure;
    const fresh = await loadOptions(opening.signal);
    token = fresh.token;
    result = await post({...payload, token});
   }
   const prefix = {question:'Q', proposal:'P', investigation:'I', bug:'I'}[kind];
   if (typeof result.id !== 'string' || !new RegExp('^' + prefix + '[1-9][0-9]*$').test(result.id)) {
    throw new Error('The server did not return a saved item ID.');
   }
   saved = result;
   keepDraft = false;
  } catch (failure) {
   keepDraft = true;
   setError(failure.message);
  } finally {
   setSending(false);
  }
  if (saved) {
   close();
   dialog.dispatchEvent(new CustomEvent('review-item-created', {bubbles: true, detail: saved}));
  } else {
   send.focus();
  }
 });

 window.reviewNewItem = {open, evidenceLimits: Object.freeze(limits.buildEvidence)};
})();'''
