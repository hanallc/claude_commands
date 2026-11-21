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
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import parse_qs

from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.user_zone import shown_time
from scripts.production.delivery_state import QuestionSendState
from scripts.production import delivery_state, staff
from scripts.review_links import staff_names
from scripts.review_links import question_record
from scripts.review_links import change_browser, change_page, console, image_store, page_frame, page_question, review_link

PAGE_NAME = "images.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"
LOCK = image_store.LOCK
SENDER = Path(__file__).stem
FEEDBACK_PREFIX = ".review-link-feedback-"
FEEDBACK_FILE = re.compile(re.escape(FEEDBACK_PREFIX) + r"([0-9a-f]{64})(?:\.(?:pending|json))?")
# Each answer button and the first line of the message that it sends to the owner.
ANSWERS = {"approved": "your image is approved.", "not approved": "your image is not approved.",
           "feedback": "feedback on your image."}
# A page loaded before the answer buttons existed can still post delete.
ACTIONS = frozenset(("delete", "question", *ANSWERS))


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
    """No try reached the owner; the image stays on the page."""


class FeedbackUnavailable(OSError):
    """Keep a known final result when a later file cannot be read."""

    def __init__(self, error: Exception, final: change_page.QuestionProgress | None = None) -> None:
        super().__init__(str(error))
        self.final: change_page.QuestionProgress | None = final


def images_root(root: Path) -> Path:
    return root / image_store.IMAGES_FOLDER


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


def records(root: Path, *, on_error: Callable[[Path, Exception], None] | None = None) -> list[image_store.ImageRecord]:
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
            if on_error is None:
                print(f"review-links images: {error}", file=sys.stderr)
            else:
                on_error(entry, error)
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


def register(image: Path, caption: str, owner: staff.StaffId, subject: str,
             *, before: Path | None = None) -> image_store.ImageRecord:
    """Register one image or a before and after pair for its staff owner."""
    if not caption.strip():
        raise ValueError("the caption is empty; say what changed and what to look at")
    if staff.snapshot().by_id(owner) is None:
        raise ValueError(f"no staff record for owner {owner}")
    return image_store.register_file(image, images_root(review_link.review_root().resolve()),
                                     caption, str(owner), subject, before=before)


def delete(root: Path, identity: str) -> None:
    if not image_store.IDENTITY.fullmatch(identity):
        raise ValueError(f"{identity}: not an image id")
    folder = images_root(root) / identity
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"no image {identity}")
    shutil.rmtree(folder)


def console_token(root: Path) -> str:
    return change_browser.browser_manifest(change_page.console_folder(root))[0]


def request_token(token: str, identity: str) -> str:
    return hmac.new(token.encode(), ("image:" + identity).encode(), hashlib.sha256).hexdigest()


def feedback_source_token(token: str, identity: str, registered_at: str) -> str:
    return registered_at + ":" + request_token(token, identity + ":" + registered_at)


def owner_names(record: staff.StaffSnapshot, identity: str) -> tuple[str, str]:
    """Return the card's owner text, name and kind, and the group panel's owner text, the name alone."""
    member = record.by_id(staff.StaffId(identity))
    if member is None:
        unknown = f"Unknown session: {identity}"
        return unknown, unknown
    return f"{member.name} ({member.kind})", member.name


def picture(record: image_store.ImageRecord, label: str, file: str) -> str:
    """Link one image to the shared zoom; show its pair label above it."""
    path = html.escape(image_path(record, file), quote=True)
    alt = record["caption"].splitlines()[0]
    linked = (f'<a data-image-zoom href="{path}" target="_blank" rel="noopener"><img src="{path}" '
              + f'alt="{html.escape(f"{label}: {alt}" if label else alt, quote=True)}" loading="lazy"></a>')
    return f'<figure><figcaption>{label}</figcaption>{linked}</figure>' if label else linked


def question_html(question: question_record.Question, open_questions: Mapping[str, QuestionSendState]) -> str:
    answer = question.answer
    if answer is not None:
        return page_question.answered_question_html(question, answer)
    return page_question.open_question_html(question, failed=open_questions.get(question.question_id) is QuestionSendState.FAILED)


def card(record: image_store.ImageRecord, token: str, members: staff.StaffSnapshot, feedback_rows: int,
         names: staff_names.StaffNames | staff_names.NameReading = staff_names.NameReading.CURRENT, *,
         open_questions: Mapping[str, QuestionSendState] = NO_OPEN_QUESTIONS) -> str:
    """Show the caption as the title, then the aligned details, the image or pair, the feedback field and the answer buttons."""
    pair = "before" in record
    pictures = "".join(picture(record, label, file) for label, file in shown_files(record))
    caption = record["caption"]
    added = datetime.fromisoformat(record["registered_at"])
    owner, name = owner_names(members, record["owner"])
    names = staff_names.for_page(names, members)
    member = members.by_id(staff.StaffId(record["owner"]))
    shown_owner = names.html(record["owner"], name) + f" ({member.kind})" if member is not None else html.escape(owner)
    questions = sorted((question_record.read(saved) for saved in record.get('questions', [])),
                       key=lambda question: (question.answer is not None, int(question.question_id[1:])))
    round_id = feedback_round(answered_question_ids(record))
    feedback_source = html.escape(feedback_source_token(token, record["id"], record["registered_at"]), quote=True)
    closing_buttons = ('<button type="submit" name="action" value="approved">Approved</button>'
                       '<button type="submit" name="action" value="not approved">Not Approved</button>'
                       '<button type="submit" name="action" value="feedback">Send Feedback</button>')
    returning_buttons = '<button type="submit" name="action" value="question">Ask question</button>'
    return (f'<article id="image-{record["id"]}"{' class="pair"' if pair else ''} data-image="{record["id"]}" '
            + f'data-owner="{html.escape(record["owner"], quote=True)}" data-owner-name="{html.escape(name, quote=True)}">'
            + f'<h2 class="caption">{html.escape(caption)}</h2><dl class="image-details">'
            + f'<dt>Owner:</dt><dd>{shown_owner}</dd>'
            + f'<dt>For  :</dt><dd>{html.escape(record["subject"])}</dd>'
            + f'<dt>Added:</dt><dd><time datetime="{html.escape(record["registered_at"], quote=True)}">'
            + f'{shown_time(added)}</time></dd></dl>'
            + (f'<div class="image-pair">{pictures}</div>' if pair else pictures)
            + ''.join(question_html(question, open_questions) for question in questions)
            + '<form method="post" data-image-form>'
            + f'<input type="hidden" name="image" value="{record["id"]}">'
            + f'<input type="hidden" name="token" value="{request_token(token, record["id"])}">'
            + f'<input type="hidden" name="feedback_round" value="{round_id}">'
            + f'<input type="hidden" name="feedback_source" value="{feedback_source}">'
            + f'<label for="feedback-{record["id"]}">Feedback</label>'
            + f'<textarea id="feedback-{record["id"]}" name="feedback" rows="{feedback_rows}"></textarea>'
            + page_frame.review_action_groups(closing_buttons, returning_buttons)
            + '<span role="status" data-image-status></span></form></article>')


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
    answer_refresh_seconds = settings.number("page_refresh_seconds")
    refresh_seconds = answer_refresh_seconds if refresh_needed else 0
    waiting_text = page_frame.QUESTION_WAITING.format(owner_role="owner")
    content = (STYLE + page_frame.title("Images", "images")
               + f'<p hidden data-image-waiting role="status">{waiting_text}</p>'
               + '<div data-image-layout><nav data-image-groups aria-label="Image groups"></nav>'
               + f'<div><div data-images data-script="{SCRIPT_VERSION}" data-refresh-seconds="{refresh_seconds}"'
               + f' {card_motion.attributes()}'
               + f' data-answer-refresh-seconds="{answer_refresh_seconds}" data-waiting-images="{" ".join(waiting)}">'
               + f'{"".join(cards)}</div><p hidden data-empty></p></div></div>'
               + '<noscript>Enable JavaScript to answer on an image.</noscript>'
               + f'<script>{SCRIPT}</script>')
    return page_frame.rendered(change_page.document(content, "Images"), started, read_at)


def submit(root: Path, body: str, status: change_page.DeliveryStatus
           ) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Save a question for deferred delivery; keep feedback and delete actions as before."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    # A page loaded before Send feedback existed posts only the image and its token, which means delete.
    if not {"image", "token"} <= set(fields) <= {"image", "token", "feedback", "action", "feedback_round",
                                               "feedback_source"} or any(
            len(value) != 1 for value in fields.values()):
        raise question_record.AnswerRefused("use the form on this page")
    identity = fields["image"][0]
    if not image_store.IDENTITY.fullmatch(identity):
        raise question_record.AnswerRefused("not an image id")
    token = console_token(root)
    expected = request_token(token, identity)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise question_record.AnswerRefused("the image token does not match this page")
    action = fields.get("action", ["delete"])[0]
    if action not in ACTIONS:
        raise question_record.AnswerRefused("use the form on this page")
    if action == 'question':
        return ask_question(root, identity, fields.get('feedback', [''])[0], status)
    if action in ANSWERS:
        round_id = fields["feedback_round"][0] if "feedback_round" in fields else None
        registered_at = None
        if "feedback_source" in fields:
            source = fields["feedback_source"][0]
            registered_at, _, _signature = source.rpartition(":")
            expected_source = feedback_source_token(token, identity, registered_at)
            if not secrets.compare_digest(source.encode(), expected_source.encode()):
                raise question_record.AnswerRefused("the feedback source does not match this page")
        send_answer(root, identity, action, fields.get("feedback", [""])[0].strip(), round_id=round_id, status=status,
                    registered_at=registered_at)
        return change_page.NoMessageDelivery()
    with file_lock.exclusive(images_root(root) / LOCK):
        try:
            delete(root, identity)
        except ValueError as error:
            raise question_record.AnswerRefused(str(error)) from error
    return change_page.NoMessageDelivery()


def recover_questions(status: change_page.DeliveryStatus) -> None:
    """Read each image for questions without a delivery record."""
    for record in records(status.root, on_error=status.defer_source):
        if saved := record.get("questions"):
            path = images_root(status.root) / record["id"] / image_store.RECORD
            target = change_page.image_question_target(path, record)
            page_question.recover_source(status, target, tuple(question_record.read(question) for question in saved))


def ask_question(root: Path, identity: str, words: str, status: change_page.DeliveryStatus
                 ) -> change_page.MessageDelivery:
    store = image_store.ImageQuestionStore(images_root(root))
    folder = store.folder(identity)
    record = image_store.read_record(folder)
    target = change_page.image_question_target(folder / image_store.RECORD, record)
    return page_question.ask(store, target, words, status)


def answer_question(root: Path, identity: str, question_id: str, text: str) -> None:
    store = image_store.ImageQuestionStore(images_root(root))
    with change_page.question_answer_lock(store.folder(identity) / image_store.RECORD, question_id):
        result = page_question.answer(store, identity, question_id, text)
        if isinstance(result, page_question.AnswerRefused):
            raise question_record.AnswerRefused(result.reason)
        folder = store.folder(identity)
        record = image_store.read_record(folder)
        target = change_page.image_question_target(folder / image_store.RECORD, record)
        change_page.record_page_question_answer(change_page.DeliveryStatus(root), target, result.question)


def answer_message(record: image_store.ImageRecord, answer: str, feedback: str) -> str:
    """Name the answer, then the feedback when the user wrote some, the image, and the delete command."""
    origin = f"http://{review_link.link_host()}:{conf.read('review_links').whole('server_port')}"
    return "\n".join((
        f"From the user (via the Images page): {ANSWERS[answer]}",
        *((f"Feedback: {feedback}",) if feedback else ()),
        *(f"{label or 'Image'}: {origin}{image_path(record, file)}" for label, file in shown_files(record)),
        f"Caption: {record['caption']}", f"For: {record['subject']}",
        "After you act on it, delete the image: "
        + f"~/.claude/scripts/lib/py ~/.claude/scripts/review_links/images.py delete --id {record['id']}"))


def answered_question_ids(record: image_store.ImageRecord) -> set[str]:
    return {question["question_id"] for question in record.get("questions", []) if question["answer"] is not None}


def feedback_round(answered: Set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(answered)).encode()).hexdigest()


def feedback_key(record: image_store.ImageRecord, folder: Path, answer: str, feedback: str,
                 round_id: str | None) -> str:
    return feedback_key_for_source(folder, record["registered_at"], answer, feedback, round_id)


def feedback_key_for_source(folder: Path, registered_at: str, answer: str, feedback: str,
                            round_id: str | None) -> str:
    parts = [registered_at, answer, feedback]
    if round_id is not None:
        parts.append(round_id)
    return change_page.review_delivery_key(folder / image_store.RECORD, folder.name, json.dumps(parts))


@dataclass(frozen=True)
class ImageFeedback:
    key: str
    owner: str
    registered_at: str
    answered: frozenset[str]
    action: str
    text: str

    def save(self, path: Path) -> None:
        fields = {"key": self.key, "owner": self.owner, "registered_at": self.registered_at,
                  "answered": sorted(self.answered), "action": self.action, "text": self.text}
        atomic_file.write_text_atomic(path, json.dumps(fields) + "\n", new_file_mode=0o600)


def read_feedback_request(path: Path, key: str) -> ImageFeedback | None:
    try:
        fields = delivery_state.read_fields(path)
    except FileNotFoundError:
        return None
    owner, registered = fields.get("owner"), fields.get("registered_at")
    answered, action, text = fields.get("answered"), fields.get("action"), fields.get("text")
    if (fields.get("key") != key or not isinstance(owner, str) or not isinstance(registered, str)
            or not isinstance(answered, list)
            or not all(isinstance(value, str) for value in cast(list[object], answered))
            or not isinstance(action, str) or action not in ANSWERS or not isinstance(text, str)
            or (action == "feedback" and not text.strip())):
        raise ValueError("Invalid saved Images feedback.")
    return ImageFeedback(key, owner, registered, frozenset(cast(list[str], answered)), action, text)


def legacy_feedback_snapshot(path: Path) -> frozenset[str] | None:
    try:
        return frozenset(path.with_suffix("").read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return None


def legacy_feedback_action(record: image_store.ImageRecord, folder: Path, key: str,
                           previously_answered: frozenset[str]) -> str | None:
    """Use the key to verify an approval with no feedback text."""
    for round_id in (None, feedback_round(previously_answered)):
        for answer in ("approved", "not approved"):
            if feedback_key(record, folder, answer, "", round_id) == key:
                return answer
    return None


def send_answer(root: Path, identity: str, answer: str, feedback: str, *, round_id: str | None = None,
                status: change_page.DeliveryStatus | None = None, registered_at: str | None = None) -> None:
    """Report success only when the receipt confirms delivery."""
    if answer not in ANSWERS:
        raise question_record.AnswerRefused("use the form on this page")
    if answer == "feedback" and not feedback.strip():
        raise question_record.AnswerRefused("enter feedback")
    status = change_page.DeliveryStatus(root) if status is None else status
    directory = images_root(root)
    folder = directory / identity
    owner = ""
    if registered_at is None:
        with file_lock.exclusive(directory / LOCK):
            try:
                record = image_store.read_record(folder)
            except (OSError, ValueError) as error:
                if isinstance(error, FileNotFoundError) and not folder.exists():
                    raise question_record.AnswerRefused(f"no image {identity}") from error
                key = feedback_key_for_source(folder, "", answer, feedback, round_id)
                lookup = folder / (FEEDBACK_PREFIX + key.removeprefix("review:") + ".lookup")
                status.report_recovery_error(lookup, error, f"Images feedback {identity}")
                retry = change_page.PageFeedbackRetry(partial(retry_feedback_lookup, status, identity, answer,
                                                              feedback, round_id))
                status.defer_question(lookup, retry)
                raise FeedbackUnavailable(error) from error
            owner, registered_at = record["owner"], record["registered_at"]
    key = feedback_key_for_source(folder, registered_at, answer, feedback, round_id)
    path = folder / (FEEDBACK_PREFIX + key.removeprefix("review:") + ".pending")
    request = ImageFeedback(key, owner, registered_at, frozenset(), answer, feedback)
    progress = deliver_saved_feedback(status, path, request, round_id=round_id)
    if progress is None or progress.state is not change_page.QuestionDeliveryState.CONFIRMED:
        raise AnswerNotSent((progress.error if progress else "") or "The sender did not confirm delivery.")


def retry_feedback_lookup(status: change_page.DeliveryStatus, identity: str, answer: str, feedback: str,
                          round_id: str | None) -> None:
    try:
        send_answer(status.root, identity, answer, feedback, round_id=round_id, status=status)
    except (AnswerNotSent, FeedbackUnavailable, question_record.AnswerRefused):
        pass


def defer_feedback(status: change_page.DeliveryStatus, path: Path, request: ImageFeedback | None = None, *,
                   round_id: str | None = None, error: Exception | None = None) -> None:
    if error is not None:
        status.report_recovery_error(path, error, f"Images feedback {path.parent.name}")
    status.defer_question(path, change_page.PageFeedbackRetry(partial(recover_feedback, status, path, request,
                                                                      round_id=round_id)),
                          keep_existing=request is None)


def recover_feedback(status: change_page.DeliveryStatus, path: Path, request: ImageFeedback | None = None, *,
                     round_id: str | None = None) -> None:
    try:
        _ = deliver_saved_feedback(status, path, request, round_id=round_id)
    except (OSError, ValueError):
        # Delivery already handled the retry.
        pass


def deliver_saved_feedback(status: change_page.DeliveryStatus, path: Path, request: ImageFeedback | None = None, *,
                           round_id: str | None = None) -> change_page.QuestionProgress | None:
    """Use one path for live delivery and recovery of the same request."""
    path = path.with_suffix(".pending")
    try:
        progress = advance_feedback(path, request, round_id=round_id)
    except FeedbackUnavailable as error:
        defer_feedback(status, path, request, round_id=round_id, error=error)
        if error.final is not None:
            return error.final
        raise
    except question_record.AnswerRefused:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        defer_feedback(status, path, request, round_id=round_id, error=error)
        raise FeedbackUnavailable(error) from error
    if progress is not None and progress.state.pending:
        defer_feedback(status, path)
    else:
        status.forget_deferred_question(path)
    return progress


@dataclass(frozen=True)
class FeedbackFiles:
    progress: change_page.QuestionProgress | None
    request: ImageFeedback | None
    answered: frozenset[str] | None
    record: image_store.ImageRecord | None


def read_feedback_files(path: Path, key: str, *, need_image: bool = False,
                        previous: change_page.QuestionProgress | None = None) -> FeedbackFiles:
    """Read the files needed for the next step under the Images lock."""
    progress = previous
    try:
        current = change_page.read_page_feedback_progress(path.with_suffix(".json"), key)
        if current is None and previous is not None:
            raise ValueError("The saved Images feedback receipt is missing.")
        progress = current
        saved = read_feedback_request(path, key)
        answered = saved.answered if saved is not None else legacy_feedback_snapshot(path)
        record = None
        if answered is not None or (progress.state.pending if progress is not None else need_image):
            try:
                record = image_store.read_record(path.parent)
            except FileNotFoundError:
                if path.parent.exists():
                    raise
        if saved is not None and record is not None and (saved.owner, saved.registered_at) != (
                record["owner"], record["registered_at"]):
            raise ValueError("The saved Images feedback has a different source.")
    except (OSError, ValueError) as error:
        final = progress if progress is not None and not progress.state.pending else None
        raise FeedbackUnavailable(error, final) from error
    return FeedbackFiles(progress, saved, answered, record)


def finish_feedback(path: Path, progress: change_page.QuestionProgress, files: FeedbackFiles) -> None:
    record, answered = files.record, files.answered
    if (progress.state is change_page.QuestionDeliveryState.CONFIRMED and record is not None
            and answered is not None and "feedback_at" not in record and not answered_question_ids(record) - answered):
        record["feedback_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        image_store.write_record(path.parent, record)
    path.unlink(missing_ok=True)


def advance_feedback(path: Path, request: ImageFeedback | None, *,
                     round_id: str | None) -> change_page.QuestionProgress | None:
    match = FEEDBACK_FILE.fullmatch(path.name)
    assert match is not None
    folder = path.parent
    lock = folder.parent / LOCK
    key = "review:" + match[1]
    receipt = path.with_suffix(".json")
    with file_lock.exclusive(lock):
        files = read_feedback_files(path, key, need_image=request is not None)
        progress, saved, answered, record = files.progress, files.request, files.answered, files.record
        if progress is not None and progress.expire() != progress:
            with change_page.sender_claim(receipt.with_suffix(".lock")) as claimed:
                if not claimed:
                    return progress
                try:
                    progress = change_page.read_page_feedback_progress(receipt, key)
                except (OSError, ValueError) as error:
                    raise FeedbackUnavailable(error) from error
                if progress is not None and (expired := progress.expire()) != progress:
                    change_page.write_feedback_progress(receipt, expired)
                    progress = expired
        if progress is not None and not progress.state.pending:
            finish_feedback(path, progress, files)
            return progress
        if record is None:
            if request is not None:
                raise question_record.AnswerRefused(f"no image {folder.name}")
            return None
        if answered is None and request is None:
            return progress
        owner, registered = record["owner"], record["registered_at"]
        if saved is None and request is not None:
            if request.registered_at != registered or (request.owner and request.owner != owner):
                raise question_record.AnswerRefused("The image changed. Review the image and try again.")
            if answered is None:
                answered = frozenset(answered_question_ids(record))
                if round_id is not None and round_id != feedback_round(answered):
                    raise question_record.AnswerRefused("The image has a new answer. Review the answer and try again.")
            saved = replace(request, owner=owner, answered=answered)
            saved.save(path)
        elif saved is not None:
            # Sync a request that may have survived an unconfirmed directory sync.
            atomic_file.sync_folder(folder)
        assert answered is not None

    action = saved.action if saved is not None else legacy_feedback_action(record, folder, key, answered)
    if action is None:
        raise ValueError("The legacy Images request has no saved feedback text.")
    message = partial(answer_message, record, action, saved.text if saved is not None else "")
    progress = change_page.deliver_page_feedback(receipt, key, staff.StaffId(owner), message, sender=SENDER)
    if not progress.state.pending:
        with file_lock.exclusive(lock):
            files = read_feedback_files(path, key, previous=progress)
            current = files.record
            progress = files.progress or progress
            if not progress.state.pending and (
                    current is None or (current["owner"], current["registered_at"]) == (owner, registered)):
                finish_feedback(path, progress, files)
    return progress


def defer_feedback_scan(status: change_page.DeliveryStatus, path: Path, error: Exception) -> None:
    status.report_recovery_error(path, error, "Images feedback")
    status.defer_question(images_root(status.root), change_page.PageFeedbackRetry(partial(recover_pending, status)))


def recover_pending(status: change_page.DeliveryStatus) -> None:
    """Read saved Images feedback and pass it to the server retry worker."""
    folder = images_root(status.root)
    try:
        directories = tuple(folder.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        defer_feedback_scan(status, folder, error)
        return
    for directory in directories:
        if not image_store.IDENTITY.fullmatch(directory.name) or directory.is_symlink():
            continue
        try:
            paths = tuple(directory.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            defer_feedback_scan(status, directory, error)
            continue
        requests = {path.with_suffix(".pending") for path in paths
                    if FEEDBACK_FILE.fullmatch(path.name) and not path.is_symlink()}
        for path in requests:
            defer_feedback(status, path)


# At 1108px and wider, the page fills the frame inside its 32px gutters, and the
# image column keeps its full 680px. 1108 = 32 + 300 + 64 + 680 + 32.
# There a single card keeps 680px, and a pair card takes up to two of them: 1376 = 680 + 16 + 680.
# A pair stacks, before on top, when its card is narrower than 600px.
STYLE = """<style>[hidden]{display:none!important}
[data-images] article{padding:24px 0;border-top:1px solid #8886}[data-images] article:first-child{border-top:0;padding-top:8px}
[data-images] img{display:block;max-width:100%;height:auto;border:1px solid #8886;border-radius:6px}
[data-images] article{container-type:inline-size}
.image-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}
.image-pair figure{margin:0}.image-pair figcaption{margin:0 0 6px;font-weight:600}
@container (max-width:599px){.image-pair{grid-template-columns:minmax(0,1fr)}}
.caption{margin:0 0 8px;font-size:1.25rem;line-height:1.35;font-weight:600;white-space:pre-line}
.image-details{display:grid;grid-template-columns:max-content 1fr;column-gap:1ch;margin:0 0 12px;font:14px/1.45 ui-monospace,monospace;opacity:.8}
.image-details dt{white-space:pre}.image-details dd{margin:0}
[data-images] form{display:grid;gap:8px;margin-top:12px}
[data-images] textarea{font:inherit;width:100%;box-sizing:border-box;color:CanvasText;background:Canvas}
[data-image-form] .review-actions button{margin:0}
.page-question p{white-space:pre-wrap;overflow-wrap:anywhere}
@media (max-width:480px){[data-image-form] .review-actions button{font-size:15px;padding:6px 8px}}
[data-image-layout]{display:grid;grid-template-columns:300px minmax(0,680px);gap:32px;align-items:start}
[data-image-groups]{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;display:grid;
grid-template-columns:minmax(0,max-content) max-content max-content;font-size:14px;border:1px solid #8886;border-radius:8px}
[data-image-groups] a{grid-column:1/-1;display:grid;grid-template-columns:subgrid;cursor:pointer;
color:inherit;text-decoration:none;overflow-wrap:anywhere}
[data-image-groups] a+a{border-top:1px solid #8884}
[data-image-groups] span{padding:6px 10px}[data-image-groups] span+span{border-left:1px solid #8884}
[data-image-groups] a:hover,[data-image-groups] a:focus-visible{background:#8883;outline:none}
[data-image-groups] .group-count{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:900px){[data-image-layout]{display:block}
[data-image-groups]{position:static;max-height:none;margin-bottom:16px;grid-template-columns:minmax(0,1fr) max-content max-content}}
@media (min-width:1108px){
[data-image-layout]{grid-template-columns:300px minmax(0,1fr);column-gap:64px}
[data-image-layout]>div{width:100%;max-width:1376px;justify-self:center}
[data-images] article:not(.pair),[data-empty]{max-width:680px;margin-inline:auto}}
</style>"""

SCRIPT = page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + page_frame.MOTION_SCRIPT + r"""(() => {
const empty = document.querySelector('[data-empty]');
const groups = document.querySelector('[data-image-groups]');
const layout = document.querySelector('[data-image-layout]');
const waiting = document.querySelector('[data-image-waiting]');
let list = document.querySelector('[data-images]');
const motion = createCardMotion(() => list);
let sending = 0;
let refreshRequest = 0;
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
  list.querySelectorAll('article:not([hidden])').forEach(card => {
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
function bind(root) {
  root.querySelectorAll('form[data-image-form]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    if (cardSubmitBlocked(form)) return;
    const question = event.submitter?.value === 'question';
    const feedback = event.submitter?.value === 'feedback', status = form.querySelector('[data-image-status]');
    delete status.dataset.refreshFailed;
    if (question && !form.elements.feedback.value.trim()) {
      status.textContent = 'Enter a question first.';
      return;
    }
    if (feedback && !form.elements.feedback.value.trim()) {
      status.textContent = 'Enter feedback first.';
      return;
    }
    // Remove the card after the fade. Restore the card if the request fails.
    const card = form.closest('article'), home = card.parentNode, next = card.nextSibling;
    const body = new URLSearchParams(new FormData(form, event.submitter));
    status.textContent = '';
    let postFailed = false;
    setCardSubmitting(form, true);
    sending++;
    try {
      if (!question) await motion.removeCard(card, apply);
      const response = await fetch(location.pathname, {method: 'POST', body});
      if (!response.ok) {
        const reason = await response.json().then(body => body.reason, () => '');
        throw new Error(reason || 'HTTP ' + response.status);
      }
      if (question) {
        card.remove();
        list.dataset.waitingImages += ' ' + card.dataset.image;
        list.dataset.refreshSeconds = list.dataset.answerRefreshSeconds;
        apply();
        pageRefresh.restart();
      }
    } catch (error) {
      postFailed = true;
      if (!question) home.insertBefore(card, next?.parentNode === home ? next : null);
      status.textContent = 'Not sent: ' + error.message;
      apply();
    } finally {
      sending--;
      setCardSubmitting(form, false);
      if (question && !postFailed) void pageRefresh.refresh(true);
      else void refresh().catch(error => {
        if (postFailed) return;
        status.dataset.refreshFailed = 'true';
        status.textContent = 'Update failed: ' + error.message;
      });
    }
  }));
}
async function refresh(_supersede=false,current=()=>true) {
  if (sending) return;
  const own = ++refreshRequest;
  const isCurrent = () => own === refreshRequest && !sending && current();
  try {
    const response = await fetch(location.pathname, {cache: 'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const fresh = page.querySelector('[data-images]');
    if (!isCurrent()) return;
    if (!fresh) throw new Error('The page has no image list.');
    // A page from an older server script cannot bind the new cards, so it loads again.
    if (fresh.dataset.script !== list.dataset.script) {
      location.reload();
      return;
    }
    const facts = pageRefreshFacts(page);
    // Keep drafts and action results on cards that the server still shows.
    const forms = new Map([...list.querySelectorAll('form[data-image-form]')]
      .map(form => [form.elements.image.value, form]));
    fresh.querySelectorAll('form[data-image-form]').forEach(form => {
      const previous = forms.get(form.elements.image.value);
      if (!previous) return;
      form.elements.feedback.value = previous.elements.feedback.value;
      const oldStatus = previous.querySelector('[data-image-status]');
      if (!oldStatus.dataset.refreshFailed) form.querySelector('[data-image-status]').textContent = oldStatus.textContent;
    });
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
bind(list);
// An image link opens its image at once.
apply();
scrollToLinkedImage();
})();"""
SCRIPT_VERSION = change_page.script_version(SCRIPT + page_frame.ZOOM_SCRIPT + page_frame.ZOOM_STYLE)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Register an image for the console's Images page, or delete one.")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("register", help="copy an image or a before and after pair to the Images page")
    _ = add.add_argument("--image", type=Path, help="the image file to copy")
    _ = add.add_argument("--before", type=Path, help="the before image of a pair; give --after with it")
    _ = add.add_argument("--after", type=Path, help="the after image of a pair; give --before with it")
    _ = add.add_argument("--caption", required=True, help="what changed and what to look at")
    _ = add.add_argument("--owner", required=True, help="the staff id that owns the image")
    subject = add.add_mutually_exclusive_group(required=True)
    _ = subject.add_argument("--change", help="the change id, such as sdlc/C12")
    _ = subject.add_argument("--phase", help="the phase id")
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
            record = register(shown, cast(str, args.caption), staff.StaffId(cast(str, args.owner)),
                              f"Change {change}" if change is not None else f"Phase {phase}", before=before)
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
