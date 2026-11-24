"""Store console images and temporary change images in separate folders."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import secrets
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import parse_qs

from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.user_zone import shown_time
from scripts.production.delivery_state import QuestionSendState
from scripts.production import staff
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


def register(image: Path, caption: str, owner: staff.StaffId, subject: str,
             *, before: Path | None = None) -> image_store.ImageRecord:
    """Register one image or a before and after pair for its staff owner."""
    if not caption.strip():
        raise ValueError("the caption is empty; say the change in the fewest plain words that stay accurate")
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
    alt = record["caption"].splitlines()[0]
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
    receiver = f' data-send-receiver="{html.escape(name, quote=True)}"' if member is not None else ""
    questions = sorted((question_record.read(saved) for saved in record.get('questions', [])),
                       key=lambda question: (question.answer is not None, int(question.question_id[1:])))
    round_id = feedback_round(answered_question_ids(record))
    closing_buttons = ('<button type="submit" name="action" value="approved">Approved</button>'
                       '<button type="submit" name="action" value="feedback">Send Feedback</button>')
    returning_buttons = '<button type="submit" name="action" value="question">Ask question</button>'
    return (f'<article id="image-{record["id"]}"{' class="pair"' if pair else ''} data-image="{record["id"]}" '
            + f'data-owner="{html.escape(record["owner"], quote=True)}" data-owner-name="{html.escape(name, quote=True)}"{receiver}>'
            + f'<h2 class="caption">{html.escape(caption)}</h2><dl class="image-details">'
            + f'<dt>Owner:</dt><dd>{shown_owner}</dd>'
            + f'<dt>For  :</dt><dd>{html.escape(record["subject"])}</dd>'
            + f'<dt>Added:</dt><dd><time datetime="{html.escape(record["registered_at"], quote=True)}">'
            + f'{shown_time(added)}</time></dd></dl>'
            + (f'<div class="image-pair" data-image-zoom-group>{pictures}</div>' if pair else pictures)
            + ''.join(question_html(question, open_questions) for question in questions)
            + '<form method="post" data-image-form>'
            + f'<input type="hidden" name="image" value="{record["id"]}">'
            + f'<input type="hidden" name="token" value="{request_token(token, record["id"])}">'
            + f'<input type="hidden" name="feedback_round" value="{round_id}">'
            + f'<label for="feedback-{record["id"]}">Feedback</label>'
            + f'<textarea id="feedback-{record["id"]}" name="feedback" rows="{feedback_rows}"></textarea>'
            + page_frame.review_action_groups(closing_buttons, returning_buttons)
            + '<span role="status" data-image-status hidden></span></form></article>')


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
               + page_frame.action_failure("image-action-failure")
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
    if not {"image", "token"} <= set(fields) <= {"image", "token", "feedback", "action", "feedback_round"} or any(
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
    if action == 'question':
        return ask_question(root, identity, fields.get('feedback', [''])[0], status)
    if action in ANSWERS:
        round_id = fields["feedback_round"][0] if "feedback_round" in fields else None
        send_answer(root, identity, action, fields.get("feedback", [""])[0].strip(), round_id=round_id)
        return change_page.NoMessageDelivery()
    with file_lock.exclusive(images_root(root) / LOCK):
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


def answer_question(root: Path, identity: str, question_id: str, text: str) -> None:
    result = page_question.answer(image_store.ImageQuestionStore(images_root(root)), identity, question_id, text)
    if isinstance(result, page_question.AnswerRefused):
        raise question_record.AnswerRefused(result.reason)


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


def feedback_round(answered: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(answered)).encode()).hexdigest()


def send_answer(root: Path, identity: str, answer: str, feedback: str, *, round_id: str | None = None) -> None:
    """Hide the image only if no question received an answer after the first submission."""
    if answer == "feedback" and not feedback:
        raise question_record.AnswerRefused("enter feedback")
    directory = images_root(root)
    folder = directory / identity
    lock = directory / LOCK
    with file_lock.exclusive(lock):
        try:
            record = image_store.read_record(folder)
        except (OSError, ValueError) as error:
            raise question_record.AnswerRefused(f"no image {identity}") from error
        parts = [record["registered_at"], answer, feedback]
        if round_id is not None:
            parts.append(round_id)
        attempt = json.dumps(parts)
        key = change_page.review_delivery_key(folder / image_store.RECORD, identity, attempt)
        snapshot = folder / (".review-link-feedback-" + key.removeprefix("review:"))
        try:
            previously_answered = set(snapshot.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            previously_answered = answered_question_ids(record)
            if round_id is not None and round_id != feedback_round(previously_answered):
                raise question_record.AnswerRefused("The image has a new answer. Review the answer and try again.")
            atomic_file.write_text_atomic(snapshot, "".join(f"{item}\n" for item in sorted(previously_answered)),
                                          new_file_mode=0o600)
        else:
            # A prior attempt can leave the file visible after a failed directory sync.
            atomic_file.sync_folder(folder)
    settings = conf.read("review_links")
    owner = staff.StaffId(record["owner"])
    failures = change_page.send_with_retries(SENDER, (owner,),
                                             answer_message(record, answer, feedback), settings.whole("delivery_tries"),
                                             settings.number("delivery_pause_seconds"), key=key)
    if failures:
        raise AnswerNotSent(failures[-1].error)
    with file_lock.exclusive(lock):
        try:
            current = image_store.read_record(folder)
        except (OSError, ValueError):
            # The owner deleted the image while the message was on its way.
            return
        if current["registered_at"] != record["registered_at"] or current["owner"] != owner:
            return
        if answered_question_ids(current) - previously_answered:
            return
        current["feedback_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        image_store.write_record(folder, current)


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
.caption{margin:0 0 8px;font-size:1.25rem;line-height:1.35;font-weight:600;white-space:pre-line}
.image-details{display:grid;grid-template-columns:max-content 1fr;column-gap:1ch;margin:0 0 12px;font:14px/1.45 ui-monospace,monospace;opacity:.8}
.image-details dt{white-space:pre}.image-details dd{margin:0}
[data-images] form{display:grid;gap:8px;margin-top:12px}
[data-images] textarea{font:inherit;width:100%;box-sizing:border-box;color:CanvasText;background:Canvas}
[data-image-form] .review-actions button{margin:0}
.page-question p{white-space:pre-wrap;overflow-wrap:anywhere}
@media (max-width:480px){[data-image-form] .review-actions button{font-size:15px;padding:6px 8px}}
[data-image-layout]{display:grid;grid-template-columns:max-content minmax(0,680px);gap:32px;align-items:start}
[data-image-groups]{position:sticky;
top:calc(var(--page-actions-space,0px) + var(--page-actions-height,0px) + var(--page-status-height,0px) + 16px);
max-height:calc(100vh - 32px - var(--page-status-height,0px) - var(--page-actions-space,0px)
- var(--page-actions-height,0px));overflow:auto;display:grid;
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
    if (question && !form.elements.feedback.value.trim()) {
      showActionFailure(status, 'Enter a question first.');
      return;
    }
    if (feedback && !form.elements.feedback.value.trim()) {
      showActionFailure(status, 'Enter feedback first.');
      return;
    }
    // Remove the card after the fade. Restore the card if the request fails.
    const card = form.closest('article'), home = card.parentNode, next = card.nextSibling;
    const body = new URLSearchParams(new FormData(form, event.submitter));
    showActionFailure(status);
    let postFailed = false;
    setCardSubmitting(form, true);
    sending++;
    try {
      if (!question) await motion.removeCard(card, apply);
      const response = await heldFetch(location.pathname, {method: 'POST', body}, card.dataset.sendReceiver);
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
      const undone = error instanceof SendUndone;
      showActionFailure(status, undone ? error.message : 'Not sent: ' + error.message);
      apply();
      if (undone) form.elements.feedback.focus();
    } finally {
      sending--;
      setCardSubmitting(form, false);
      if (question && !postFailed) void pageRefresh.refresh(true);
      else void refresh().catch(error => {
        if (postFailed) return;
        showActionFailure(actionFailure, 'Update failed: ' + error.message);
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
    showActionFailure(actionFailure);
    // Keep drafts and action failures on cards that the server still shows.
    const forms = new Map([...list.querySelectorAll('form[data-image-form]')]
      .map(form => [form.elements.image.value, form]));
    fresh.querySelectorAll('form[data-image-form]').forEach(form => {
      const previous = forms.get(form.elements.image.value);
      if (!previous) return;
      form.elements.feedback.value = previous.elements.feedback.value;
      const oldStatus = previous.querySelector('[data-image-status]');
      showActionFailure(form.querySelector('[data-image-status]'), oldStatus.textContent);
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
    _ = add.add_argument("--caption", required=True, help="the change in the fewest plain words that stay accurate")
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
