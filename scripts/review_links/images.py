"""Store console images and temporary change images in separate folders."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import re
import secrets
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from types import MappingProxyType
from typing import cast, override
from urllib.parse import parse_qs

from scripts.console_text import check as console_text
from scripts.notify.review import CaptionLayout

from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.user_zone import shown_time
from scripts.production.delivery_state import QuestionSendState, read_fields
from scripts.production import staff, the_list
from scripts.review_links import staff_names
from scripts.review_links import question_record
from scripts.review_links import change_browser, change_page, console, image_store, page_frame, page_question, review_link

PAGE_NAME = "images.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
LOCK = image_store.LOCK
SENDER = Path(__file__).stem
# Each answer button and the first line of the message that it sends to the owner.
# Feedback always means the image is not approved.
ANSWERS = {"approved": "your image is approved.", "feedback": "your image is not approved. The feedback follows."}
# A page loaded before the answer buttons existed can still post delete.
ACTIONS = frozenset(("delete", "question", "retry", *ANSWERS))
ANSWER_PREFIX = "From the user (via the Images page): "
STALE_ANSWER = "This page is stale. Review the new answer and try again."
_DECISION_FILE = re.compile(r"([0-9a-f]{12})-[0-9a-f]{64}\.json")


NO_OPEN_QUESTIONS: Mapping[str, QuestionSendState] = MappingProxyType({})


@dataclass(frozen=True)
class ImageReview:
    record: image_store.ImageRecord
    open_questions: Mapping[str, QuestionSendState] = NO_OPEN_QUESTIONS

    def visible(self) -> bool:
        if self.open_questions:
            return any(not delivery.waits for delivery in self.open_questions.values())
        return "feedback_at" not in self.record

    def refresh(self) -> bool:
        return any(delivery.waits for delivery in self.open_questions.values())


class AnswerNotSent(Exception):
    """The server could not save or send the answer."""


@dataclass(frozen=True)
class ImageAnswerSent:
    notice: str = ""
    draft_confirmed: bool = False


class _ImageAnswerOrigin(Enum):
    DIRECT = auto()
    SIGNED_FORM = auto()


@dataclass(frozen=True)
class ImageDecision:
    image_id: str
    answer: str
    feedback: str
    time: datetime


class NoImageDecision(Enum):
    OUTSIDE_STORE = auto()


@dataclass(frozen=True)
class _AnswerDraft:
    action: str
    words: str
    round_id: str | None
    request_id: str = ""

    def key(self, folder: Path, saved: image_store.ImageAnswers, signed: image_store.SignedImageAnswer | None = None) -> str:
        purpose = _signed_answer_key(saved, signed) if signed is not None and "previous_answer_key" in signed else "message"
        return self._key(folder, saved, purpose)

    def request_key(self, folder: Path, saved: image_store.ImageAnswers) -> str:
        return change_page.review_delivery_key(folder / image_store.RECORD, folder.name,
                                               saved["registered_at"], purpose=f"images-form-id:{self.request_id}")

    def form_attempt_key(self, folder: Path, saved: image_store.ImageAnswers) -> str | None:
        request_key = self.request_key(folder, saved)
        for key, attempt in saved["attempts"].items():
            keys = attempt.get("form_request_keys", [])
            if request_key in keys:
                return key
            if keys and (signed := attempt.get("signed_answer")) is not None:
                # Old keys include the saved words and the round if the form sent it.
                for round_id in (None, signed["round_id"]):
                    original = _AnswerDraft(signed["answer"], signed["feedback"], round_id)
                    if original._key(folder, saved, f"images-form:{self.request_id}") in keys:
                        return key
        return None

    def matches_answer(self, attempt: image_store.ImageAnswer) -> bool:
        signed = attempt.get("signed_answer")
        return signed is not None and (self.action, self.words) == (signed["answer"], signed["feedback"])

    def _key(self, folder: Path, saved: image_store.ImageAnswers, purpose: str) -> str:
        parts = [saved["registered_at"], self.action, self.words]
        if self.round_id is not None:
            parts.append(self.round_id)
        return change_page.review_delivery_key(folder / image_store.RECORD, folder.name, json.dumps(parts), purpose=purpose)


def images_root(root: Path) -> Path:
    return root / image_store.IMAGES_FOLDER


def decision_root(root: Path) -> Path:
    return root / ".review-link-image-decisions"


def read_image_decision(root: Path, path: Path) -> ImageDecision | NoImageDecision:
    folder = decision_root(root)
    name = _DECISION_FILE.fullmatch(path.name)
    if (name is None or folder.is_symlink() or path.is_symlink()
            or path.parent.resolve() != folder.resolve()):
        return NoImageDecision.OUTSIDE_STORE
    fields = read_fields(path)
    match fields:
        case {"image_id": str(identity), "source": str(source), "answer": str(answer),
              "feedback": str(feedback), "time": str(stamp)} if (
                identity == name[1] and source == the_list.IMAGE_ANSWER_SOURCE and answer in ANSWERS):
            time = datetime.fromisoformat(stamp)
            if time.utcoffset() == UTC.utcoffset(time):
                return ImageDecision(identity, answer, feedback, time)
        case _:
            pass
    raise ValueError(f"{path}: Invalid Images answer record.")


def _signed_answer_key(saved: image_store.ImageAnswers, signed: image_store.SignedImageAnswer) -> str:
    fields = [saved["id"], saved["registered_at"], signed["round_id"], signed["answer"], signed["feedback"]]
    if "previous_answer_key" in signed:
        fields.append(signed["previous_answer_key"])
    if "form_request_key" in signed:
        fields.append(signed["form_request_key"])
    return "review:" + hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def _save_image_decision(root: Path, saved: image_store.ImageAnswers, signed: image_store.SignedImageAnswer) -> None:
    """Save this distinct signed answer while the Images lock is held."""
    folder = decision_root(root)
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink():
        raise OSError(f"{folder}: Expected an Images answer folder.")
    identity = saved["id"]
    key = _signed_answer_key(saved, signed).removeprefix("review:")
    path = folder / f"{identity}-{key}.json"
    if path.is_symlink():
        raise OSError(f"{path}: Expected an Images answer file.")
    if not path.exists() and "previous_answer_key" not in signed and "form_request_key" not in signed:
        # Keep old evidence at the path that reviewers can already cite.
        old_key = hashlib.sha256(json.dumps((saved["registered_at"], signed["round_id"])).encode()).hexdigest()
        old_path = folder / f"{identity}-{old_key}.json"
        if old_path.is_file():
            try:
                old = read_image_decision(root, old_path)
            except ValueError:
                old = NoImageDecision.OUTSIDE_STORE
            if isinstance(old, ImageDecision) and (old.answer, old.feedback) == (signed["answer"], signed["feedback"]):
                path = old_path
    if path.exists():
        atomic_file.sync_folder(folder)
    else:
        heading = saved.get("heading")
        image_text = "\n".join((heading["title"], *image_store.look_for_message_lines(heading))) if heading else ""
        fields = {"image_id": identity, "source": the_list.IMAGE_ANSWER_SOURCE, "image_text": image_text,
                  "answer": signed["answer"], "feedback": signed["feedback"], "time": signed["time"]}
        atomic_file.write_text_atomic(path, json.dumps(fields) + "\n", new_file_mode=0o600)
    atomic_file.sync_folder(root)


def page_url() -> str:
    """Return the fixed Images page path; the console's browser token signs its deletes."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def image_path(record: image_store.ImageRecord, file: str = "") -> str:
    return f"/{image_store.IMAGES_FOLDER}/{record['id']}/{file or record['file']}"


def shown_files(record: image_store.ImageRecord) -> tuple[tuple[str, str], ...]:
    """Return each image's label and file: a pair shows Before, then After; one image has no label."""
    if "before" in record:
        return ("Before", record["before"]), ("After", record["file"])
    return ("", record["file"]),


def records(root: Path) -> list[image_store.ImageRecord]:
    """Read every registered image, oldest first; an unreadable record prints one line and stays out."""
    folder = images_root(root)
    found: list[image_store.ImageRecord] = []
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return found
    for entry in entries:
        if not image_store.IDENTITY.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
            continue
        try:
            found.append(image_store.read_record(entry))
        except (OSError, ValueError) as error:
            print(f"review-links images: {error}", file=sys.stderr)
    # A new image goes to the end, so the cards above it keep their places.
    return sorted(found, key=lambda record: record["registered_at"])


def question_delivery(status: change_page.DeliveryStatus, saved: change_page.SavedQuestionDelivery
                      ) -> QuestionSendState:
    """Read the delivery state while the caller holds the delivery lock."""
    delivery = change_page.question_delivery_view(status, saved)
    if delivery.state in (QuestionSendState.READY, QuestionSendState.PREPARED) and status.startup_recovery_pending.is_set():
        return QuestionSendState.SENDING
    return delivery.state


def image_review(status: change_page.DeliveryStatus, record: image_store.ImageRecord) -> ImageReview:
    open_questions = [question_record.read(saved) for saved in record.get('questions', []) if saved['answer'] is None]
    if not open_questions:
        return ImageReview(record)
    target = change_page.image_question_target(images_root(status.root) / record['id'] / image_store.RECORD, record)
    with status.locked():
        return ImageReview(record, {
            question.question_id: question_delivery(status, target.saved(question))
            for question in open_questions
        })


def waiting_images(request: console.NewsRequest, *, status: change_page.DeliveryStatus | None = None) -> frozenset[str]:
    """Name the images that wait for the user, for the console's new mark."""
    if status is None:
        status = change_page.DeliveryStatus(request.root)
    waiting: set[str] = set()
    for record in records(request.root):
        if image_review(status, record).visible():
            answers = [saved['question_id'] for saved in record.get('questions', []) if saved['answer'] is not None]
            waiting.add(record['id'] + (':'+answers[-1] if answers else ''))
    return frozenset(waiting)


def register(image: Path, title: str, owner: staff.StaffId, subject: str,
             *, look_for: tuple[str, ...] = (), before: Path | None = None,
             list_item: str | None = None, two_halves: bool = False) -> image_store.ImageRecord:
    """Register one image or a before and after pair for its staff owner."""
    title = title.strip()
    if title.splitlines() != [title]:
        raise ValueError("The image title must be one nonempty line.")
    limit = conf.read("review_links").whole("max_chars.image_title")
    if len(title) > limit:
        raise ValueError(f"--title has {len(title)} characters. The limit is {limit}. Put the details in --look-for lines.")
    lines = tuple(line.strip() for line in look_for)
    if any(line.splitlines() != [line] for line in lines):
        raise ValueError("Each --look-for value must be one nonempty line.")
    if staff.snapshot().by_id(owner) is None:
        raise ValueError(f"no staff record for owner {owner}")
    if two_halves and before is not None:
        raise ValueError("Use two halves with a single image.")
    layout: CaptionLayout = "single picture"
    if before is not None:
        layout = "before and after pair"
    elif two_halves:
        layout = "picture with two halves"
    caption = "\n".join((title, *(f"Look for: {line}" for line in lines)))
    console_text.require(caption, "image caption", caption_layout=layout)
    keeper = the_list.image_list_keeper(list_item) if list_item is not None else None
    return image_store.register_file(image, images_root(review_link.review_root().resolve()),
                                     title, str(owner), subject, look_for=lines, before=before,
                                     list_item=list_item, list_keeper=keeper)


def delete(root: Path, identity: str) -> None:
    if not image_store.IDENTITY.fullmatch(identity):
        raise ValueError(f"{identity}: not an image id")
    directory = images_root(root)
    if not directory.is_dir():
        raise ValueError(f"no image {identity}")
    folder = directory / identity
    with file_lock.exclusive(directory / LOCK):
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f"no image {identity}")
        record = image_store.read_record(folder)
        saved = _image_answers(directory, record)
        saved["deleted_feedback_round"] = feedback_round(answered_question_ids(record))
        if "list_item" in saved and "list_keeper" not in saved:
            saved["list_keeper"] = the_list.image_list_keeper(saved["list_item"])
        image_store.write_answers(directory, saved)
        shutil.rmtree(folder)


def console_token(root: Path) -> str:
    return change_browser.browser_manifest(change_page.console_folder(root))[0]


def request_token(token: str, identity: str) -> str:
    return hmac.new(token.encode(), ("image:" + identity).encode(), hashlib.sha256).hexdigest()


def owner_names(record: staff.StaffSnapshot, identity: str) -> tuple[str, str]:
    """Return the card's owner text, name and kind, and the group panel's owner text, the name alone."""
    member = record.by_id(staff.StaffId(identity))
    if member is None:
        unknown = f"Unknown session: {identity}"
        return unknown, unknown
    return f"{member.name} ({member.kind})", member.name


def picture(record: image_store.ImageRecord, label: str, file: str) -> str:
    """Link one image to the shared zoom; show its pair label above it and in the zoom."""
    path = html.escape(image_path(record, file), quote=True)
    alt, *_ = record["title"].splitlines() or [""]
    zoom_label = f' data-image-zoom-label="{html.escape(label, quote=True)}"' if label else ''
    linked = (f'<a data-image-zoom{zoom_label} href="{path}" target="_blank" rel="noopener"><img src="{path}" '
              + f'alt="{html.escape(f"{label}: {alt}" if label else alt, quote=True)}" loading="lazy"></a>')
    return f'<figure><figcaption>{label}</figcaption>{linked}</figure>' if label else linked


def question_html(question: question_record.Question, open_questions: Mapping[str, QuestionSendState]) -> str:
    answer = question.answer
    if answer is not None:
        return page_question.answered_question_html(question, answer)
    failed = open_questions.get(question.question_id) is QuestionSendState.FAILED
    return page_question.open_question_html(question, failed=failed)


def _answer_form(identity: str, token: str, round_id: str, feedback_rows: int, *, question: bool = True) -> str:
    closing_buttons = ('<button type="submit" name="action" value="approved">Approved</button>'
                       '<button type="submit" name="action" value="feedback">Send Feedback</button>')
    returning_buttons = '<button type="submit" name="action" value="question">Ask question</button>' if question else ""
    return ('<form method="post" data-image-form>'
            + f'<input type="hidden" name="image" value="{identity}">'
            + f'<input type="hidden" name="token" value="{token}">'
            + f'<input type="hidden" name="feedback_round" value="{round_id}">'
            + f'<label for="feedback-{identity}">Feedback</label>'
            + f'<textarea id="feedback-{identity}" name="feedback" rows="{feedback_rows}" autocomplete="off"></textarea>'
            + page_frame.review_action_groups(closing_buttons, returning_buttons)
            + '<span role="status" data-image-status hidden></span></form>')


def _card_heading(record: image_store.ImageHeading) -> str:
    title = html.escape(record["title"])
    lines = "".join(f"<li>{html.escape(line)}</li>" for line in record["look_for"])
    look_for = f'<ul class="image-look-for">{lines}</ul>' if lines else ""
    return f'<h2 class="image-title">{title}</h2>{look_for}'


def card(record: image_store.ImageRecord, token: str, members: staff.StaffSnapshot, feedback_rows: int,
         names: staff_names.StaffNames | staff_names.NameReading = staff_names.NameReading.CURRENT, *,
         open_questions: Mapping[str, QuestionSendState] = NO_OPEN_QUESTIONS) -> str:
    """Render an image card with its title, details, and actions."""
    pair = "before" in record
    pictures = "".join(picture(record, label, file) for label, file in shown_files(record))
    added = datetime.fromisoformat(record["registered_at"])
    owner, name = owner_names(members, record["owner"])
    names = staff_names.for_page(names, members)
    member = members.by_id(staff.StaffId(record["owner"]))
    shown_owner = (names.html(record["owner"], name) + f" ({names.role_label(member.kind, member.kind)})"
                   if member is not None else html.escape(owner))
    receiver = f' data-send-receiver="{html.escape(name, quote=True)}"' if member is not None else ""
    questions = sorted((question_record.read(saved) for saved in record.get('questions', [])),
                       key=lambda question: (question.answer is not None, int(question.question_id[1:])))
    round_id = feedback_round(answered_question_ids(record))
    return (f'<article id="image-{record["id"]}"{' class="pair"' if pair else ''} data-image="{record["id"]}" '
            + f'data-owner="{html.escape(record["owner"], quote=True)}" data-owner-name="{html.escape(name, quote=True)}"{receiver}>'
            + _card_heading(record) + '<dl class="image-details">'
            + f'<dt>Owner:</dt><dd>{shown_owner}</dd>'
            + f'<dt>For  :</dt><dd>{html.escape(record["subject"])}</dd>'
            + f'<dt>Added:</dt><dd><time datetime="{html.escape(record["registered_at"], quote=True)}">'
            + f'{shown_time(added)}</time></dd></dl>'
            + (f'<div class="image-pair" data-image-zoom-group>{pictures}</div>' if pair else pictures)
            + ''.join(question_html(question, open_questions) for question in questions)
            + _answer_form(record["id"], request_token(token, record["id"]), round_id, feedback_rows)
            + '</article>')


def _pending_answer_cards(root: Path, token: str) -> list[str]:
    directory = images_root(root)
    cards: list[str] = []
    for identity in image_store.deleted_answer_ids(directory):
        try:
            saved = image_store.read_answers(directory, identity)
        except (OSError, ValueError) as error:
            print(f"review-links images: {error}", file=sys.stderr)
            continue
        removed = f"Removed picture {identity}"
        heading = saved.get("heading")
        details = (_card_heading(heading) + f'<p>{removed}</p>' if heading is not None
                   else _card_heading({"title": removed, "look_for": []}))
        for key, answer in saved["attempts"].items():
            if answer["recipients"] and set(answer["recipients"]) <= set(answer["confirmed"]):
                continue
            words = html.escape(answer.get("words", answer["message"]))
            cards.append(f'<article>{details}'
                         + f'<p class="image-answer-words">{words}</p>'
                         + '<form method="post" data-image-form>'
                         + f'<input type="hidden" name="image" value="{identity}">'
                         + f'<input type="hidden" name="token" value="{request_token(token, identity)}">'
                         + f'<input type="hidden" name="retry_key" value="{html.escape(key, quote=True)}">'
                         + '<span role="status" data-image-status>Not sent yet</span>'
                         + '<div><button type="submit" name="action" value="retry">Retry</button></div>'
                         + '</form></article>')
    return cards


def render(status: change_page.DeliveryStatus) -> bytes:
    started, read_at = time.perf_counter(), time.time()
    root = status.root
    token = console_token(root)
    members = staff.snapshot()
    names = staff_names.StaffNames(members)
    settings = conf.read("review_links")
    rows = settings.whole("feedback_rows")
    card_motion = page_frame.CardMotion.read(settings)
    waiting: list[str] = []
    cards: list[str] = []
    refresh_needed = False
    for record in records(root):
        review = image_review(status, record)
        if review.visible():
            cards.append(card(record, token, members, rows, names, open_questions=review.open_questions))
        elif review.open_questions:
            waiting.append(record['id'])
        refresh_needed |= review.refresh()
    cards.extend(_pending_answer_cards(root, token))
    answer_refresh_seconds = settings.number("page_refresh_seconds")
    refresh_seconds = answer_refresh_seconds if refresh_needed else 0
    waiting_text = page_frame.QUESTION_WAITING.format(owner_role="owner")
    content = (STYLE + page_frame.title("Images", "images")
               + page_frame.action_failure("image-action-failure")
               + f'<p hidden data-image-waiting role="status">{waiting_text}</p>'
               + '<p hidden data-image-notice role="status"></p>'
               + '<div data-image-layout><nav data-image-groups aria-label="Image groups"></nav>'
               + f'<div><div data-images data-script="{SCRIPT_VERSION}" data-refresh-seconds="{refresh_seconds}"'
               + f' {card_motion.attributes()}'
               + f' data-answer-refresh-seconds="{answer_refresh_seconds}" data-waiting-images="{" ".join(waiting)}">'
               + f'{"".join(cards)}</div><p hidden data-empty></p></div></div>'
               + '<template data-image-draft-template><article><h2 class="image-title"></h2>'
               + _answer_form("", "", "", rows, question=False) + '</article></template>'
               + '<noscript>Enable JavaScript to answer on an image.</noscript>'
               + f'<script>{SCRIPT}</script>')
    return page_frame.rendered(change_page.document(content, "Images"), started, read_at)


def submit(root: Path, body: str, status: change_page.DeliveryStatus
           ) -> change_page.MessageDelivery | change_page.NoMessageDelivery | ImageAnswerSent:
    """Save a question, send an answer, or delete the picture."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    # A page loaded before Send feedback existed posts only the image and its token, which means delete.
    if not {"image", "token"} <= set(fields) <= {
            "image", "token", "feedback", "action", "feedback_round", "retry_key", "draft_action", "answer_request"} or any(
            len(value) != 1 for value in fields.values()):
        raise question_record.AnswerRefused("use the form on this page")
    identity = fields["image"][0]
    if not image_store.IDENTITY.fullmatch(identity):
        raise question_record.AnswerRefused("not an image id")
    expected = request_token(console_token(root), identity)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise question_record.AnswerRefused("the image token does not match this page")
    action = fields.get("action", ["delete"])[0]
    if action == "not approved":
        # A card loaded before I21 can still show the removed button.
        raise question_record.AnswerRefused("Not Approved was removed. Use Send Feedback.")
    if action not in ACTIONS:
        raise question_record.AnswerRefused("use the form on this page")
    request_id = fields.get("answer_request", [""])[0]
    if "answer_request" in fields and (action not in (*ANSWERS, "retry")
                                      or re.fullmatch(r"[0-9a-f]{32}", request_id) is None):
        raise question_record.AnswerRefused("use the form on this page")
    if action == "retry":
        required = {"image", "token", "action", "retry_key"}
        if not required <= fields.keys() or not fields["retry_key"][0]:
            raise question_record.AnswerRefused("use the Retry button on this page")
        draft_fields = fields.keys() - required
        draft_action = fields.get("draft_action", [""])[0]
        draft = None
        if draft_fields:
            if (not {"draft_action", "feedback"} <= draft_fields <= {
                    "draft_action", "feedback", "feedback_round", "answer_request"}
                    or draft_action not in ANSWERS):
                raise question_record.AnswerRefused("use the Retry button on this page")
            draft = _AnswerDraft(draft_action, fields["feedback"][0].strip(), fields.get("feedback_round", [None])[0], request_id)
        return _send_answer(root, identity, action, "", retry_key=fields["retry_key"][0], draft=draft,
                            origin=_ImageAnswerOrigin.SIGNED_FORM)
    if "retry_key" in fields or "draft_action" in fields:
        raise question_record.AnswerRefused("use the form on this page")
    if action == 'question':
        return ask_question(root, identity, fields.get('feedback', [''])[0], status)
    if action in ANSWERS:
        round_id = fields["feedback_round"][0] if "feedback_round" in fields else None
        return _send_answer(root, identity, action, fields.get("feedback", [""])[0].strip(), round_id=round_id,
                            origin=_ImageAnswerOrigin.SIGNED_FORM, request_id=request_id)
    try:
        delete(root, identity)
    except ValueError as error:
        raise question_record.AnswerRefused(str(error)) from error
    return change_page.NoMessageDelivery()


def ask_question(root: Path, identity: str, words: str, status: change_page.DeliveryStatus
                 ) -> change_page.MessageDelivery:
    store = image_store.ImageQuestionStore(images_root(root))
    folder = store.folder(identity)
    record = image_store.read_record(folder)
    target = change_page.image_question_target(folder / image_store.RECORD, record)
    return page_question.ask(store, target, words, status)


class ImageReplyStore(image_store.ImageQuestionStore):
    @override
    def set_answer(self, item: str, question_id: str, answer: question_record.Answer) -> None:
        console_text.require(answer.text, "reply", identity=f"image/{item}/{question_id}",
                             refusal=question_record.AnswerRefused)
        super().set_answer(item, question_id, answer)


def answer_question(root: Path, identity: str, question_id: str, text: str) -> None:
    result = page_question.answer(ImageReplyStore(images_root(root)), identity, question_id, text)
    if isinstance(result, page_question.AnswerRefused):
        raise question_record.AnswerRefused(result.reason)


def answer_message(record: image_store.ImageRecord, answer: str, feedback: str) -> str:
    """Name the answer, then the feedback when the user wrote some, the image, and the delete command."""
    origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
    return "\n".join((
        ANSWER_PREFIX + ANSWERS[answer],
        *((f"Feedback: {feedback}",) if feedback else ()),
        *(f"{label or 'Image'}: {origin}{image_path(record, file)}" for label, file in shown_files(record)),
        f"Title: {record['title']}", *image_store.look_for_message_lines(record), f"For: {record['subject']}",
        *((f"List item: {record['list_item']}",) if "list_item" in record else ()),
        "After you act on it, delete the image: "
        + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))


def answered_question_ids(record: image_store.ImageRecord) -> set[str]:
    return {question["question_id"] for question in record.get("questions", []) if question["answer"] is not None}


def feedback_round(answered: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(answered)).encode()).hexdigest()


def _image_answers(directory: Path, record: image_store.ImageRecord) -> image_store.ImageAnswers:
    try:
        answers = image_store.read_answers(directory, record["id"])
    except FileNotFoundError:
        return image_store.new_answers(record)
    if answers["registered_at"] != record["registered_at"] or answers["owner"] != record["owner"]:
        raise ValueError("This picture has a different registration.")
    answers["heading"] = {"title": record["title"], "look_for": record["look_for"]}
    return answers


def _answer_words(identity: str, answer: str, feedback: str) -> str:
    summary = f"Image {identity}: {ANSWERS[answer]}"
    return summary + "\n" + feedback if feedback else summary


def _legacy_answer_words(identity: str, item: str, message: str, answer: str) -> str:
    _, _, body = message.partition("\n")
    removed = f"Picture {identity} was removed.\n"
    if body.startswith(removed):
        feedback = body.removeprefix(removed).removesuffix(f"\nList item: {item}")
    else:
        link = re.search(rf"\n(?:Image|Before): https?://[^\n]+/images/{identity}/(?:image|before)\.[a-z]+\n", body)
        feedback = body[:link.start()] if link is not None else ""
    return _answer_words(identity, answer, feedback.removeprefix("Feedback: ") if feedback.startswith("Feedback: ") else "")


def _signed_answer_for_submission(attempt: image_store.ImageAnswer, saved: image_store.ImageAnswers,
                                  draft: _AnswerDraft) -> image_store.SignedImageAnswer:
    """Reuse the latest proof in the round when the answer matches."""
    stored = attempt.get("signed_answer")
    active_round = (stored["round_id"] if stored is not None
                    else saved.get("deleted_feedback_round", feedback_round(set(attempt["answered"]))))
    round_id = draft.round_id if draft.round_id is not None else active_round
    proofs = [proof for entry in reversed(saved["attempts"].values())
              if (proof := entry.get("signed_answer")) is not None and proof["round_id"] == round_id]
    latest = max(proofs, key=lambda proof: datetime.fromisoformat(proof["time"]), default=None)
    answer = draft.action, draft.words
    if latest is not None and (latest["answer"], latest["feedback"]) == answer:
        return latest
    signed: image_store.SignedImageAnswer = {"answer": draft.action, "feedback": draft.words,
                                             "time": datetime.now(UTC).isoformat(), "round_id": round_id}
    if latest is not None and any((proof["answer"], proof["feedback"]) == answer for proof in proofs):
        signed["previous_answer_key"] = _signed_answer_key(saved, latest)
    return signed


def _picture_answer(directory: Path, identity: str, draft: _AnswerDraft, retry_key: str | None,
                    origin: _ImageAnswerOrigin) -> tuple[str, image_store.ImageAnswers]:
    """Save the words, proof, and List recipient before the first send."""
    folder = directory / identity
    try:
        record = image_store.read_record(folder)
    except FileNotFoundError:
        record = None
    if record is None:
        try:
            saved = image_store.read_answers(directory, identity)
        except FileNotFoundError as error:
            raise question_record.AnswerRefused(f"no image {identity}") from error
    else:
        saved = _image_answers(directory, record)

    attempts = saved["attempts"]
    answer, feedback, round_id = draft.action, draft.words, draft.round_id
    signed_form = origin is _ImageAnswerOrigin.SIGNED_FORM
    form_key = draft.request_key(folder, saved) if signed_form and draft.request_id else ""
    form_retry = draft.form_attempt_key(folder, saved) if form_key else None
    answered: set[str] = answered_question_ids(record) if record is not None else set()
    current_round = feedback_round(answered) if record is not None else saved.get("deleted_feedback_round")
    if retry_key is not None:
        if record is not None or retry_key not in attempts:
            raise question_record.AnswerRefused("no saved answer for this removed picture")
        key = retry_key
    elif form_retry is not None:
        if not draft.matches_answer(attempts[form_retry]):
            raise question_record.AnswerRefused(STALE_ANSWER)
        key = form_retry
    else:
        if signed_form and round_id is not None and current_round is not None and round_id != current_round:
            raise question_record.AnswerRefused(STALE_ANSWER)
        key = form_key or draft.key(folder, saved)
    attempt: image_store.ImageAnswer | None = attempts.get(key)
    stale_round = False
    if attempt is None:
        previous: set[str] = set()
        if record is None:
            message = ANSWER_PREFIX + ANSWERS[answer] + f"\nPicture {identity} was removed."
            if feedback:
                message += "\nFeedback: " + feedback
            if "list_item" in saved:
                message += "\nList item: " + saved["list_item"]
        else:
            # Keep the original question round when a request started before the store moved.
            legacy = folder / (".review-link-feedback-" + key.removeprefix("review:"))
            try:
                previous = set(legacy.read_text(encoding="utf-8").splitlines())
            except FileNotFoundError:
                previous = answered
                stale_round = not signed_form and round_id is not None and round_id != feedback_round(previous)
            message = answer_message(record, answer, feedback)
        attempt = {"message": message, "words": _answer_words(identity, answer, feedback), "answered": sorted(previous),
                   "recipients": [], "confirmed": []}
    new_proof = False
    if signed_form and retry_key is None and form_retry is None:
        if form_key:
            proof: image_store.SignedImageAnswer = {
                "answer": answer, "feedback": feedback, "time": datetime.now(UTC).isoformat(),
                "round_id": current_round or round_id or feedback_round(answered), "form_request_key": form_key,
            }
        else:
            proof = _signed_answer_for_submission(attempt, saved, draft)
            signed_key = next((key for key, entry in attempts.items() if entry.get("signed_answer") == proof),
                              draft.key(folder, saved, proof))
            if signed_key != key:
                key = signed_key
                attempt = attempts.get(key) or {"message": attempt["message"], "words": _answer_words(identity, answer, feedback),
                                                "answered": attempt["answered"], "recipients": [], "confirmed": []}
        new_proof = attempt.get("signed_answer") != proof
        if new_proof:
            attempt["signed_answer"] = proof
    new_attempt = key not in attempts
    if stale_round and new_attempt:
        raise question_record.AnswerRefused("The image has a new answer. Review the answer and try again.")
    request_keys = attempt.get("form_request_keys", [])
    new_request = bool(form_key) and form_key not in request_keys
    if new_request:
        attempt["form_request_keys"] = [*request_keys, form_key]
    if new_attempt or new_proof:
        _ = attempts.pop(key, None)
        attempts[key] = attempt
    if new_attempt or new_proof or new_request:
        image_store.write_answers(directory, saved)
    else:
        atomic_file.sync_folder(directory)

    signed = attempt.get("signed_answer") if signed_form else None
    if signed is not None and "list_item" not in saved:
        _save_image_decision(directory.parent, saved, signed)
    needs_recipients = not attempt["recipients"]
    recipients = [saved["owner"]]
    if "list_item" in saved and (needs_recipients or signed is not None):
        item = saved["list_item"]
        if signed is not None:
            approved = signed["answer"] == "approved"
            words = _answer_words(identity, signed["answer"], signed["feedback"])
            signed_at = datetime.fromisoformat(signed["time"])
            list_key = _signed_answer_key(saved, signed)
        else:
            if "signed_answer" in attempt:
                raise question_record.AnswerRefused("use the form on this page")
            approved = attempt["message"].partition("\n")[0] == ANSWER_PREFIX + ANSWERS["approved"]
            action = "approved" if approved else "feedback"
            words = attempt.get("words") or _legacy_answer_words(identity, item, attempt["message"], action)
            signed_at = the_list.NoTimestamp.UNSET
            list_key = key
        try:
            keeper = staff.StaffId(saved["list_keeper"]) if "list_keeper" in saved else the_list.image_list_keeper(item)
            producer = the_list.save_image_answer(keeper, item, list_key, words, image_id=identity,
                                                  approved=approved, signed_at=signed_at)
        except (the_list.ListError, OSError) as error:
            raise AnswerNotSent(f"Could not save the answer on {item}: {error}. Try again.") from error
        saved["list_keeper"] = keeper
        attempt["words"] = words
        if producer not in recipients:
            recipients.append(producer)
    if needs_recipients:
        attempt["recipients"] = recipients
        image_store.write_answers(directory, saved)
    return key, saved


def _recipient_name(members: staff.StaffSnapshot, identity: str) -> str:
    row = members.by_id(staff.StaffId(identity))
    return row.name if row is not None else identity


def send_answer(root: Path, identity: str, answer: str, feedback: str, *, round_id: str | None = None,
                retry_key: str | None = None, draft: _AnswerDraft | None = None) -> ImageAnswerSent:
    """Send an answer without evidence from a signed form."""
    return _send_answer(root, identity, answer, feedback, origin=_ImageAnswerOrigin.DIRECT,
                        round_id=round_id, retry_key=retry_key, draft=draft)


def _send_answer(root: Path, identity: str, answer: str, feedback: str, *, origin: _ImageAnswerOrigin,
                 round_id: str | None = None, retry_key: str | None = None,
                 draft: _AnswerDraft | None = None, request_id: str = "") -> ImageAnswerSent:
    """Send to each recipient that has yet to confirm this answer."""
    if not image_store.IDENTITY.fullmatch(identity):
        raise question_record.AnswerRefused("not an image id")
    if answer == "feedback" and not feedback:
        raise question_record.AnswerRefused("enter feedback")
    directory = images_root(root)
    lock = directory / LOCK
    folder = directory / identity
    if not directory.is_dir():
        raise question_record.AnswerRefused(f"no image {identity}")
    with file_lock.exclusive(lock):
        try:
            submission = _AnswerDraft(answer, feedback, round_id, request_id)
            key, saved = _picture_answer(directory, identity, submission, retry_key, origin)
        except OSError as error:
            raise AnswerNotSent(f"Could not save the picture answer: {error}. Try again.") from error
    attempt = saved["attempts"][key]
    remaining = tuple(staff.StaffId(target) for target in attempt["recipients"] if target not in attempt["confirmed"])
    failures: list[change_page.DeliveryFailure] = []
    if remaining:
        settings = conf.read("review_links")
        failures = change_page.send_with_retries(
            SENDER, remaining, attempt["message"], settings.whole("delivery_tries"),
            settings.number("delivery_pause_seconds"), key=key, confirm=True)
    failed = {failure.recipient for failure in failures}
    with file_lock.exclusive(lock):
        current = image_store.read_answers(directory, identity)
        delivery = current["attempts"][key]
        confirmed = sorted(set(delivery["confirmed"]) | (set(remaining) - failed))
        if delivery["confirmed"] != confirmed:
            delivery["confirmed"] = confirmed
            image_store.write_answers(directory, current)
        missing = set(delivery["recipients"]) - set(confirmed)
        if not missing:
            try:
                record = image_store.read_record(folder)
            except FileNotFoundError:
                owner = _recipient_name(staff.snapshot(), saved["owner"])
                draft_confirmed = False
                if draft is not None:
                    if draft.request_id:
                        draft_confirmed = draft.form_attempt_key(folder, current) == key and draft.matches_answer(delivery)
                    else:
                        draft_confirmed = draft.key(folder, saved, delivery.get("signed_answer")) == key
                return ImageAnswerSent(f"This picture was removed; your words went to {owner}.",
                                       draft_confirmed=draft_confirmed)
            if (record["registered_at"] == saved["registered_at"] and record["owner"] == saved["owner"]
                    and "feedback_at" not in record and not answered_question_ids(record) - set(delivery["answered"])):
                record["feedback_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                image_store.write_record(folder, record)
            return ImageAnswerSent()
    members = staff.snapshot()
    reasons = [f"{_recipient_name(members, failure.recipient)} did not get the message ({failure.error})."
               for failure in failures if isinstance(failure.recipient, str) and failure.recipient in missing]
    raise AnswerNotSent(" ".join((*reasons, "Try again.")))


# The group index is as wide as its cells need above 900px, so no name or time wraps.
# At 1108px and wider, the page fills the frame inside its 32px gutters, and the image
# column keeps its full 680px while the index is 300px or narrower. 1108 = 32 + 300 + 64 + 680 + 32.
# There a single card keeps 680px, and a pair card takes up to two of them: 1376 = 680 + 16 + 680.
# A pair stacks, before on top, when its card is narrower than 600px.
STYLE = """<style>[hidden]{display:none!important}
[data-images] article{padding:24px 0;border-top:1px solid #8886}[data-images] article:first-child{border-top:0;padding-top:8px}
[data-images] img{display:block;max-width:100%;height:auto;border:1px solid #8886;border-radius:6px}
[data-images] article{container-type:inline-size}
.image-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}
.image-pair figure{margin:0}.image-pair figcaption{margin:0 0 6px;font-weight:600}
@container (max-width:599px){.image-pair{grid-template-columns:minmax(0,1fr)}}
.image-title{margin:0 0 8px;font:inherit;white-space:pre-line;overflow-wrap:anywhere}
.image-look-for{margin:0 0 12px;padding-inline-start:1.4em;color:var(--ink-2);font-size:.9rem}
.image-details{display:grid;grid-template-columns:max-content 1fr;column-gap:1ch;margin:0 0 12px;font:14px/1.45 ui-monospace,monospace;opacity:.8}
.image-details dt{white-space:pre}.image-details dd{margin:0}
[data-images] form{display:grid;gap:8px;margin-top:12px}
[data-images] textarea{font:inherit;width:100%;box-sizing:border-box;color:CanvasText;background:Canvas}
[data-image-form] .review-actions button{margin:0}
.page-question p,.image-answer-words{white-space:pre-wrap;overflow-wrap:anywhere}
@media (max-width:480px){[data-image-form] .review-actions button{font-size:15px;padding:6px 8px}}
[data-image-layout]{display:grid;grid-template-columns:max-content minmax(0,680px);gap:32px;align-items:start}
[data-image-groups]{position:sticky;
top:calc(var(--page-top-height,0px) + 16px);
max-height:calc(100vh - 32px - var(--page-top-height,0px));overflow:auto;display:grid;
grid-template-columns:repeat(3,max-content);font-size:14px;border:1px solid #8886;border-radius:8px;white-space:nowrap}
[data-image-groups] a{grid-column:1/-1;display:grid;grid-template-columns:subgrid;cursor:pointer;
color:inherit;text-decoration:none;overflow-wrap:anywhere}
[data-image-groups] a+a{border-top:1px solid #8884}
[data-image-groups] span{padding:6px 10px}[data-image-groups] span+span{border-left:1px solid #8884}
[data-image-groups] a:hover,[data-image-groups] a:focus-visible{background:#8883;outline:none}
[data-image-groups] .group-count{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:900px){[data-image-layout]{display:block}
[data-image-groups]{position:static;max-height:none;margin-bottom:16px;grid-template-columns:minmax(0,1fr) max-content max-content;white-space:normal}}
@media (min-width:1108px){
[data-image-layout]{grid-template-columns:max-content minmax(0,1fr);column-gap:64px}
[data-image-layout]>div{width:100%;max-width:1376px;justify-self:center}
[data-images] article:not(.pair),[data-empty]{max-width:680px;margin-inline:auto}}
</style>"""

SCRIPT = (page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + page_frame.MOTION_SCRIPT
          + page_frame.ACTION_STATUS_SCRIPT) + r"""(() => {
const empty = document.querySelector('[data-empty]');
const actionFailure = document.getElementById('image-action-failure');
const groups = document.querySelector('[data-image-groups]');
const layout = document.querySelector('[data-image-layout]');
const waiting = document.querySelector('[data-image-waiting]');
const notice = document.querySelector('[data-image-notice]');
let list = document.querySelector('[data-images]');
const motion = createCardMotion(() => list);
let refreshRequest = 0;
const staleAnswerReason = __STALE_ANSWER__;
let staleAnswerNotice = '';
const refreshSeconds = () => list.dataset.refreshSeconds;
const imageLink = () => new URLSearchParams(location.hash.slice(1));
function apply() {
  layout.hidden = list.dataset.waitingImages.split(' ').includes(imageLink().get('image'));
  waiting.hidden = !layout.hidden;
  empty.hidden = list.children.length > 0;
  showGroups();
  empty.textContent = 'No images registered.';
}
// One row for each run of shown images from the same owner; the row opens the first image of the run.
function showGroups() {
  const rows = [];
  list.querySelectorAll('article[data-owner]:not([hidden],[data-send-held])').forEach(card => {
    const last = rows[rows.length - 1];
    if (last && last.owner === card.dataset.owner) { last.count++; return; }
    rows.push({owner: card.dataset.owner, card, count: 1});
  });
  groups.replaceChildren(...rows.map(row => {
    const link = document.createElement('a');
    link.href = '#' + row.card.id;
    link.addEventListener('click', event => {
      event.preventDefault();
      selectImage(row.card);
    });
    const name = row.card.dataset.ownerName;
    [name, String(row.count), row.card.querySelector('time').textContent].forEach((text, index) => {
      const cell = document.createElement('span');
      if (index === 0) {
        const label = row.card.querySelector('.image-details dd .staff-name');
        if (label) cell.append(label.cloneNode(true)); else cell.textContent = text;
      } else cell.textContent = text;
      if (index === 1) cell.className = 'group-count';
      link.append(cell);
    });
    return link;
  }));
  groups.hidden = rows.length === 0;
}
const draftStorageKey = 'images.answer-drafts:' + location.pathname;
const pictureStates = new Map();
const forms = root => [...root.querySelectorAll('form[data-image-form]')];
const imageId = form => form.elements.image.value;
const attemptKey = form => form.elements.retry_key?.value;
const formStatus = form => form.querySelector('[data-image-status]');
function pictureState(image) {
  if (!pictureStates.has(image)) {
    pictureStates.set(image, {draft: null, revision: 0, active: null, draftError: '', attempts: new Map()});
  }
  return pictureStates.get(image);
}
try {
  for (const [image, draft] of JSON.parse(sessionStorage.getItem(draftStorageKey) || '[]')) {
    pictureState(image).draft = draft;
    if (draft.needsCurrentForm) staleAnswerNotice = 'Not sent: ' + staleAnswerReason;
  }
} catch { /* The browser may block storage. */ }
function preventDraftLoss(event) {
  event.preventDefault();
  event.returnValue = true;
}
function saveDrafts() {
  const drafts = [];
  for (const [image, {draft}] of pictureStates) {
    if (draft && (draft.words || draft.retryAction)) drafts.push([image, draft]);
  }
  try {
    if (drafts.length) sessionStorage.setItem(draftStorageKey, JSON.stringify(drafts));
    else sessionStorage.removeItem(draftStorageKey);
    removeEventListener('beforeunload', preventDraftLoss);
    return true;
  } catch {
    if (drafts.some(([, draft]) => draft.words)) {
      addEventListener('beforeunload', preventDraftLoss);
      return false;
    }
    removeEventListener('beforeunload', preventDraftLoss);
    return true;
  }
}
function readEdit(form, state, edited = false) {
  const {feedback, token, feedback_round: round} = form.elements;
  const changed = feedback.value !== (state.draft?.words ?? '');
  if (edited || changed) state.revision++;
  const prior = changed || state.draft?.needsCurrentForm ? null : state.draft;
  state.draft = {...prior, words: feedback.value, token: token.value, round: round.value};
}
function restoreForms(root, previous = []) {
  const editable = new Map(), attempts = new Set();
  for (const form of forms(root)) {
    const image = imageId(form), key = attemptKey(form), state = pictureState(image);
    if (key) {
      attempts.add(key);
      const error = state.attempts.get(key) || '';
      state.attempts.set(key, error);
      if (error) showActionFailure(formStatus(form), error);
    } else {
      editable.set(image, form);
    }
  }
  const previousEdits = new Map(previous.filter(form => !attemptKey(form)).map(form => [imageId(form), form]));
  for (const [image, state] of pictureStates) {
    for (const [key, error] of state.attempts) {
      // A missing card does not confirm delivery.
      if (!attempts.has(key) && !error) state.attempts.delete(key);
    }
    const draft = state.draft;
    let form = editable.get(image);
    if (draft && !draft.needsCurrentForm && !form && (draft.words || draft.retryAction || state.draftError)) {
      const template = document.querySelector('[data-image-draft-template]').content;
      const original = previousEdits.get(image)?.closest('article') ?? template.firstElementChild;
      const card = original.cloneNode(true);
      card.id = 'image-' + image;
      if (original === template.firstElementChild) card.querySelector('h2').textContent = 'Picture ' + image;
      form = card.querySelector('form');
      form.elements.image.value = image;
      form.elements.token.value = draft.token;
      form.elements.feedback_round.value = draft.round;
      form.elements.feedback.id = 'feedback-' + image;
      form.querySelector('label').htmlFor = form.elements.feedback.id;
      root.append(card);
    }
    if (form) {
      if (draft) form.elements.feedback.value = draft.words;
      showActionFailure(formStatus(form), state.draftError);
    } else if (!state.attempts.size && !draft?.needsCurrentForm) {
      pictureStates.delete(image);
    }
  }
}
function setPictureSubmitting(image, submitting) {
  for (const form of forms(list)) {
    if (imageId(form) === image) setCardSubmitting(form, submitting);
  }
}
function clearDraft(state, form) {
  state.draft = null;
  state.draftError = '';
  if (form) form.elements.feedback.value = '';
  saveDrafts();
}
function bind(root) {
  for (const form of forms(root)) {
    form.elements.feedback?.addEventListener('input', () => {
      readEdit(form, pictureState(imageId(form)), true);
      saveDrafts();
    });
    form.addEventListener('submit', submit);
  }
}
async function submit(event) {
  event.preventDefault();
  const form = event.currentTarget, image = imageId(form), state = pictureState(image);
  if (state.active || cardSubmitBlocked(form)) return;
  const action = event.submitter?.value, key = attemptKey(form), question = action === 'question';
  const edit = key ? forms(list).find(other => imageId(other) === image && !attemptKey(other)) : form;
  if (edit) readEdit(edit, state);
  const status = formStatus(form), draft = state.draft;
  if ((question || action === 'feedback') && !draft.words.trim()) {
    state.draftError = question ? 'Enter a question first.' : 'Enter feedback first.';
    showActionFailure(status, state.draftError);
    return;
  }
  if (staleAnswerNotice) {
    staleAnswerNotice = '';
    showActionFailure(actionFailure);
  }
  const {retryAction, retryRound, requestId} = draft ?? {};
  const body = new URLSearchParams(new FormData(form, event.submitter));
  if (key) {
    if (draft?.retryAction) {
      body.set('draft_action', draft.retryAction);
      body.set('feedback', draft.words);
      if (draft.retryRound !== undefined) body.set('feedback_round', draft.retryRound);
      if (draft.requestId) body.set('answer_request', draft.requestId);
    }
  } else {
    if (!question) {
      const sameDraft = draft.retryAction === action;
      if (sameDraft && draft.retryRound !== undefined) body.set('feedback_round', draft.retryRound);
      if (!sameDraft || !draft.requestId) {
        draft.requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
      }
      body.set('answer_request', draft.requestId);
      draft.retryAction = action;
      draft.retryRound = body.get('feedback_round') ?? undefined;
    }
    state.draftError = '';
  }
  const request = {revision: state.revision, key};
  state.active = request;
  ++refreshRequest;
  setPictureSubmitting(image, true);
  const field = form.elements.feedback;
  if (field) field.readOnly = true;
  if (!key) showActionFailure(status);
  saveDrafts();
  const card = form.closest('article');
  const hold = holdCard(card, hidden => { if (hidden) apply(); });
  let postFailed = false, retryable = true, staleAnswer = false;
  try {
    const response = await hold.post(location.pathname, {method: 'POST', body}, card.dataset.sendReceiver);
    if (!response.ok) {
      retryable = response.status >= 500;
      const reason = await response.json().then(body => body.reason, () => '');
      staleAnswer = response.status === 400 && reason === staleAnswerReason;
      throw new Error(reason || 'HTTP ' + response.status);
    }
    const result = question ? {} : await response.json().catch(() => ({}));
    if (edit && edit.elements.feedback.value !== (state.draft?.words ?? '')) {
      readEdit(edit, state);
      saveDrafts();
    }
    if (state.active === request && state.revision === request.revision
        && (!request.key || result.draft_confirmed === true)) {
      clearDraft(state, edit);
      if (request.key) edit?.closest('article').remove();
    }
    if (question) {
      card.remove();
      list.dataset.waitingImages += ' ' + card.dataset.image;
      list.dataset.refreshSeconds = list.dataset.answerRefreshSeconds;
      apply();
      pageRefresh.restart();
    } else {
      if (result.notice) {
        notice.textContent = result.notice;
        notice.hidden = false;
      }
      if (request.key) state.attempts.delete(request.key);
      if (hold.restore()) {
        card.remove();
        apply();
      } else await motion.removeCard(card, apply);
    }
  } catch (error) {
    hold.restore();
    postFailed = true;
    const undone = error instanceof SendUndone;
    const message = undone ? error.message : 'Not sent: ' + error.message;
    if (staleAnswer && !request.key) {
      state.draft = {words: field.value, needsCurrentForm: true};
      saveDrafts();
      card.remove();
      staleAnswerNotice = message;
      showActionFailure(actionFailure, message);
    } else if (request.key) state.attempts.set(request.key, message);
    else {
      state.draftError = message;
      if ((!retryable || undone) && !question && state.revision === request.revision) {
        state.draft.retryAction = undone ? retryAction : undefined;
        state.draft.retryRound = undone ? retryRound : undefined;
        state.draft.requestId = undone ? requestId : undefined;
        saveDrafts();
      }
    }
    showActionFailure(status, message);
    apply();
    if (undone) field?.focus();
  } finally {
    state.active = null;
    setPictureSubmitting(image, false);
    if (field) field.readOnly = false;
    if (question && !postFailed) void pageRefresh.refresh(true);
    else void refresh().catch(error => {
      if (!postFailed) showActionFailure(actionFailure, 'Update failed: ' + error.message);
    });
  }
}
const sending = () => [...pictureStates.values()].some(state => state.active);
async function refresh(_supersede=false,current=()=>true) {
  if (sending()) return;
  const own = ++refreshRequest;
  const isCurrent = () => own === refreshRequest && !sending() && current();
  try {
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const fresh = page.querySelector('[data-images]');
    if (!isCurrent()) return;
    if (!fresh) throw new Error('The page has no image list.');
    const previous = forms(list);
    for (const form of previous) {
      if (form.elements.feedback) readEdit(form, pictureState(imageId(form)));
    }
    const saved = saveDrafts();
    if (fresh.dataset.script !== list.dataset.script) {
      if (!saved) throw new Error('Could not save the draft before reloading.');
      location.reload();
      return;
    }
    const facts = pageRefreshFacts(page);
    showActionFailure(actionFailure, staleAnswerNotice);
    restoreForms(fresh, previous);
    const top = scrollY;
    const wasWaiting = !waiting.hidden;
    const previousRate = refreshSeconds();
    const restoreFocus = restoreFocusAfterRefresh();
    motion.finish();
    list.replaceWith(fresh);
    list = fresh;
    bind(list);
    apply();
    restoreFocus();
    if (previousRate !== refreshSeconds()) pageRefresh.restart();
    scrollTo({top, behavior: 'instant'});
    if (wasWaiting && waiting.hidden) scrollToLinkedImage();
    return facts;
  } catch (error) {
    if (isCurrent()) throw error;
  }
}
function selectImage(target) {
  void motion.scrollCard(target);
}
function scrollToLinkedImage(animate = false) {
  const start = imageLink();
  const target = document.getElementById('image-' + start.get('image'));
  if (target && animate) selectImage(target);
  else if (target) motion.scroll(target, true);
}
addEventListener('hashchange', () => {
  apply();
  scrollToLinkedImage(true);
});
const pageRefresh = createPageRefresh(refresh, refreshSeconds);
showActionFailure(actionFailure, staleAnswerNotice);
restoreForms(list);
bind(list);
// An image link opens its image at once.
apply();
scrollToLinkedImage();
})();""".replace('__STALE_ANSWER__', json.dumps(STALE_ANSWER))
SCRIPT_VERSION = change_page.script_version(SCRIPT + page_frame.ZOOM_SCRIPT + page_frame.ZOOM_STYLE)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Register an image for the console's Images page, or delete one.")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("register", help="copy an image or a before and after pair to the Images page")
    _ = add.add_argument("--image", type=Path, help="the image file to copy")
    _ = add.add_argument("--two-halves", action="store_true", help="the image shows two halves in one file")
    _ = add.add_argument("--before", type=Path, help="the before image of a pair; give --after with it")
    _ = add.add_argument("--after", type=Path, help="the after image of a pair; give --before with it")
    _ = add.add_argument("--title", required=True, help="a short title on one line")
    _ = add.add_argument("--look-for", action="append", default=[], help="one point to check; repeat for each point")
    _ = add.add_argument("--owner", required=True, help="the staff id that owns the image")
    subject = add.add_mutually_exclusive_group(required=True)
    _ = subject.add_argument("--change", help="the change id, such as sdlc/C12")
    _ = subject.add_argument("--phase", help="the phase id")
    _ = add.add_argument("--list-item", help="the item in morty's List, such as L324")
    remove = actions.add_parser("delete", help="remove an image and its folder")
    _ = remove.add_argument("--id", required=True, help="the image id")
    reply = actions.add_parser('answer', help='answer a question and return the image to the user')
    _ = reply.add_argument('--id', required=True, help='the image id')
    _ = reply.add_argument('--question', required=True, help='the question id, such as Q1')
    _ = reply.add_argument('--text', required=True, help='the answer for the user')
    args = parser.parse_args(arguments)
    try:
        if cast(str, args.action) == "register":
            change, phase = cast(str | None, args.change), cast(str | None, args.phase)
            image, before, after = (cast(Path | None, args.image), cast(Path | None, args.before),
                                    cast(Path | None, args.after))
            if image is not None and before is None and after is None:
                shown = image
            elif image is None and before is not None and after is not None:
                shown = after
            else:
                parser.error("give --image, or --before and --after")
            record = register(shown, cast(str, args.title), staff.StaffId(cast(str, args.owner)),
                              f"Change {change}" if change is not None else f"Phase {phase}",
                              look_for=tuple(cast(list[str], args.look_for)), before=before,
                              list_item=cast(str | None, args.list_item), two_halves=cast(bool, args.two_halves))
            origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
            print(f"registered {record['id']}: "
                  + " ".join(f"{origin}{image_path(record, file)}" for _, file in shown_files(record)))
            place = console.place_query("images", f"{PAGE_PATH}#image={record['id']}")
            print(f"Image link: {origin}/{change_page.CONSOLE_FOLDER}/{place}")
        elif cast(str, args.action) == 'answer':
            answer_question(review_link.review_root().resolve(), cast(str, args.id),
                            cast(str, args.question), cast(str, args.text))
            print(f"answered {cast(str, args.id)} {cast(str, args.question)}")
        else:
            delete(review_link.review_root().resolve(), cast(str, args.id))
            print(f"deleted {cast(str, args.id)}")
    except (*review_link.SNAPSHOT_ERRORS, staff.StaffRefused) as error:
        print(f"images: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
