"""Show The List from the producer's numbered records."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, tzinfo
from enum import Enum, StrEnum
from functools import cache
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict, cast
from urllib.parse import parse_qs, quote, unquote, urlencode

from scripts.lib import conf
from scripts.lib.file_lock import exclusive
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, delivery_state, footer_counts, record_store, session_staff, staff, the_list
from scripts.production.the_list import list_time
from scripts.production.update_registration import QuestionStaff, RegistrationFailure
from scripts.review_links import change_browser, change_page, console, go_ledger, page_frame, page_question, question_path
from scripts.review_links import record_table, review_actions, review_items, review_legacy, review_link, review_page
from scripts.review_links import search_fold, staff_names
from scripts.review_links import question_record
from scripts.statusline import counts_row, rebuild_row

PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/list.html"
COUNTS_QUERY = "counts=1"
REVIEW_UNAVAILABLE = "Linked review status unavailable"
COUNTS_AND_LINKS_QUERY = COUNTS_QUERY + "&linked-reviews=1"
_BOOTSTRAP_FORM_SECRET = secrets.token_bytes(32)
FIELD_LABELS = {
    "title": "Title", "owner": "Owner", "producer": "Producer", "unit": "Unit", "state": "State",
    "next_check": "Next check", "eta": "ETA", "blocked_by": "Blocked by", "related_to": "Related to",
    "requested_by": "Requested by", "next_step": "Next step", "latest_status": "Latest status",
    "evidence": "Evidence", "done_when": "Done when", "parked_until": "Parked until",
    "closing_words": "Closing words",
}
HEADERS = ("#", FIELD_LABELS["owner"], FIELD_LABELS["producer"], "Item",
           *(FIELD_LABELS[field] for field in ("state", "next_step", "eta")), "Update")


class ListChip(StrEnum):
    ALL = "all"
    NEEDS_ME = "needs-me"
    PRODUCER_ANSWERING = "producer-answering"
    WORKING = "working"
    BLOCKED = "blocked"
    PARKED = "parked"

    @property
    def label(self) -> str:
        return self.value.replace("-", " ").capitalize()


QUEUE_BY_STATE = {
    the_list.ItemState.NEW: question_path.Queue.WORKING,
    the_list.ItemState.WORKING: question_path.Queue.WORKING,
    the_list.ItemState.BLOCKED: question_path.Queue.BLOCKED,
    the_list.ItemState.WAITING_FOR_NATE: question_path.Queue.NEEDS_ME,
    the_list.ItemState.DELIVERED: question_path.Queue.NEEDS_ME,
    the_list.ItemState.DONE: question_path.Queue.DONE,
    the_list.ItemState.PAUSED: question_path.Queue.BLOCKED,
    the_list.ItemState.CLOSED: question_path.Queue.CLOSED,
}

CHIP_BY_QUEUE = {
    question_path.Queue.NEEDS_ME: ListChip.NEEDS_ME,
    question_path.Queue.WAITING_FOR_ANSWER: ListChip.PRODUCER_ANSWERING,
    question_path.Queue.LATER: ListChip.PARKED,
    question_path.Queue.WORKING: ListChip.WORKING,
    question_path.Queue.BLOCKED: ListChip.BLOCKED,
    question_path.Queue.DONE: ListChip.ALL,
    question_path.Queue.CLOSED: ListChip.ALL,
}


FILTER_FIELDS = {"show": "chip", "owner": "owner", "producer": "producer"}
FILTER_FACETS = record_table.filter_facets(FILTER_FIELDS)
STAFF_FILTERS = ("owner", "producer")
SEARCH_FIELDS = {"number": r"^#?l?(?=[0-9])", "ownerName": "", "title": "", "state": "", "parkingLabel": ""}
PARKED_SEARCH_LABEL = search_fold.fold("Parked")
STATE_SEARCH_TEXT = {state: search_fold.fold(state.value) for state in the_list.ItemState}
UNREAD_FILTER = ("unread", "1", "true")
UNREAD_FIELD, UNREAD_VALUE, UNREAD_MATCH = UNREAD_FILTER
LINK_REVEAL_FIELDS = (("openQuestion", "true", ""), (UNREAD_FIELD, "false", UNREAD_FIELD))
# Match the whitespace that String.trim removes.
QUERY_WHITESPACE = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
                    + "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def normalize_search_text(text: str) -> str:
    """Match the text that the browser reads from HTML attributes."""
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\0" in text:
        text = text.replace("\0", "\ufffd")
    return search_fold.fold(text)


class SearchText(NamedTuple):
    owner: str
    title: str


def show_aliases() -> dict[str, dict[str, ListChip]]:
    aliases: dict[str, dict[str, ListChip]] = {}
    for show in ("", "*", "open", "done", "closed", UNREAD_FIELD, *ListChip):
        states = {"Closed": ListChip.ALL}
        if show in ("", "open", ListChip.ALL, UNREAD_FIELD):
            states.update({state.value: CHIP_BY_QUEUE[queue] for state, queue in QUEUE_BY_STATE.items()})
        if show in ("done", "closed", UNREAD_FIELD):
            states[""] = ListChip.ALL
        aliases[show] = states
    for show in ("waiting-answer", "morty-answering"):
        aliases[show] = {"Closed": ListChip.ALL, "": ListChip.PRODUCER_ANSWERING}
    return aliases


SHOW_ALIASES = show_aliases()


def list_chip(state: the_list.ItemState, *, parked: bool, open_question: bool,
              unread_delivery: bool = False, unread_answer: bool = False, open_ask: bool = False) -> ListChip:
    """Find the one status chip for this item."""
    queue = question_path.queue(QUEUE_BY_STATE[state], unread_answer=unread_answer or open_ask, later=parked,
                                unread_delivery=unread_delivery, waiting_for_answer=open_question)
    return CHIP_BY_QUEUE[queue]


class ListRecord(NamedTuple):
    """An item and the fixed producer ID that owns its record."""

    keeper: staff.StaffId
    item: the_list.ListItem

    @property
    def key(self) -> str:
        return f"{self.keeper}/{self.item.id}"


def page_url() -> str:
    return PAGE_PATH


def records(*, include_closed: bool = False) -> list[ListRecord]:
    """Read the list folders even when a producer has no current staff row."""
    try:
        owners = sorted(record_store.state_root().iterdir())
    except FileNotFoundError:
        return []
    found = [ListRecord(staff.StaffId(owner.name), item) for owner in owners
             if owner.is_dir() and not owner.is_symlink()
             for item in the_list.records(staff.StaffId(owner.name), include_closed=include_closed)]
    return sorted(found, key=lambda entry: the_list.order(entry.item))


def parked_for_delivery(item: the_list.ListItem) -> bool:
    return (item.parked_until is the_list.ParkUntilDelivered.DELIVERED
            and item.state not in (the_list.ItemState.CLOSED, the_list.ItemState.DONE))


def waiting_updates(_request: console.NewsRequest) -> frozenset[str]:
    """Identify each unread update by its record and time."""
    return frozenset(f"{entry.key}/{entry.item.unread_since.isoformat()}" for entry in records()
                     if isinstance(entry.item.unread_since, datetime)
                     and (not parked_for_delivery(entry.item) or the_list.unread_answer_ids(entry.item)))


def owner_name(members: staff.StaffSnapshot, owner: the_list.ItemOwner) -> str:
    member = members.by_id(owner) if not isinstance(owner, the_list.UnassignedOwner) else None
    return member.name if member is not None else str(owner)


def eta_text(eta: the_list.Eta, today: datetime) -> str:
    if isinstance(eta, datetime):
        return list_time(eta, today)
    if isinstance(eta, the_list.EtaRange):
        return list_time(eta.earliest, today) + " – " + list_time(eta.latest, today)
    return the_list.eta_text(eta)


def short_list_time(moment: datetime, zone: tzinfo | None) -> str:
    local = moment.astimezone(zone)
    return f"{local:%b} {local.day} {local:%H:%M %Z}"


def check_row_html(check: the_list.NextCheck, today: datetime, *, parked: bool) -> str:
    if parked:
        return ""
    if isinstance(check, datetime):
        value = html.escape(short_list_time(check, today.tzinfo))
        if check < today:
            return f'<small data-list-check><span class="warning">Check overdue {value}</span></small>'
        return f'<small data-list-check>Check {value}</small>'
    if isinstance(check, the_list.CheckCondition):
        return f'<small data-list-check>Check: {html.escape(check.text)}</small>'
    return ""


def eta_cell_text(eta: the_list.Eta, today: datetime) -> str:
    if isinstance(eta, datetime):
        return short_list_time(eta, today.tzinfo)
    if isinstance(eta, date):
        return f"{eta:%b} {eta.day}"
    if isinstance(eta, the_list.EtaRange):
        return eta_cell_text(eta.earliest, today) + " – " + eta_cell_text(eta.latest, today)
    return the_list.eta_text(eta)


def parked_label(moment: datetime | the_list.ParkUntilDelivered, zone: tzinfo) -> str:
    # Keep the delivery value in the save response for existing List tabs.
    return short_list_time(moment, zone) if isinstance(moment, datetime) else question_path.write_later(moment)


def parking_notice_label(moment: datetime | the_list.ParkUntilDelivered, zone: tzinfo) -> str:
    def time_label(time: datetime) -> str:
        local = time.astimezone(zone)
        if local.hour or local.minute:
            return local.strftime("%Y-%m-%d %H:%M %Z")
        return local.date().isoformat()

    label = question_path.later_label(moment, time_label)
    return f"until {label}" if isinstance(moment, datetime) else label.lower()


def field_html(field: str, value: object, today: datetime, names: staff_names.StaffNames) -> str:
    if field in ("owner", "producer"):
        owner = cast(the_list.ItemOwner, value)
        return names.html(owner, owner_name(names.members, owner))
    if field == "eta":
        return html.escape(eta_text(cast(the_list.Eta, value), today))
    if field == "parked_until":
        if not isinstance(value, (datetime, the_list.ParkUntilDelivered)):
            return "Not parked"
        zone = today.tzinfo
        assert zone is not None
        return parked_label(value, zone)
    if isinstance(value, datetime):
        if field == "next_check":
            return html.escape(short_list_time(value, today.tzinfo))
        return html.escape(list_time(value, today))
    if isinstance(value, the_list.CheckCondition):
        return html.escape(value.text)
    if field in ("next_step", "latest_status", "done_when", "closing_words"):
        return change_browser.question_paragraphs(cast(str, value) or "Not recorded")
    if field == "unit":
        return names.html(cast(str, value)) or "Not recorded"
    if field == "blocked_by":
        dependencies = cast(tuple[the_list.ItemDependency, ...], value)
        return html.escape(", ".join(f"L{entry.number} ({entry.condition})" for entry in dependencies)) or "None"
    if field == "related_to":
        return ", ".join(f"L{number}" for number in cast(tuple[int, ...], value)) or "None"
    return html.escape(cast(str, value)) or "Not recorded"


def log_source_html(source: str, names: staff_names.StaffNames) -> str:
    label = names.html(source, owner_name(names.members, staff.StaffId(source)))
    return " <small>— " + label + "</small>"


def time_html(moment: datetime, today: datetime) -> str:
    return f'<time datetime="{moment.isoformat()}">{html.escape(list_time(moment, today))}</time>'


def log_row(index: str, moment: datetime, body: str, today: datetime, *,
            question: Literal["Question", "Answer"] | None = None) -> str:
    color_class = ''
    if question is not None:
        color_class = ' class="question"'
        body = f'<strong>{question}</strong>' + body
    return f'<li{color_class} data-log-index="{index}">' + time_html(moment, today) + '<div>' + body + '</div></li>'


def log_field_html(field: str, value: object, today: datetime, names: staff_names.StaffNames, evidence_id: str) -> str:
    if field == "evidence":
        text = html.escape(cast(str, value)) or "Not recorded"
        return f'<details class="log-evidence"><summary id="{evidence_id}">Details</summary><div>{text}</div></details>'
    return field_html(field, value, today, names)


def log_entry_html(change: the_list.ItemChange, index: int, today: datetime, names: staff_names.StaffNames,
                   fields: Mapping[str, object], evidence_id: str, keeper: staff.StaffId) -> str:
    content = change.what
    by = log_source_html(the_list.source_without_clock(change.source), names)
    question: Literal["Question", "Answer"] | None = None
    if isinstance(content, the_list.FieldChange):
        heading = content.action + by
        body = f'<strong>{heading}</strong>'
        if fields:
            body += '<dl>' + ''.join(
                f'<dt>{FIELD_LABELS[field]}</dt><dd>{log_field_html(field, value, today, names, evidence_id)}</dd>'
                for field, value in fields.items()) + '</dl>'
    elif isinstance(content, the_list.PageFeedback):
        label = {"pending": "Feedback for", "sent": "Feedback sent to", "alerted": "Feedback could not reach"}[content.state]
        recipient = content.recipient_id(keeper)
        body = label + " " + names.html(recipient, owner_name(names.members, recipient)) + by
        body += change_browser.question_paragraphs(content.words)
    elif isinstance(content, the_list.QuestionAnswered):
        question = "Answer"
        body = " to question " + html.escape(content.question_id) + by
    elif isinstance(content, the_list.QuestionClosed):
        question = "Question"
        body = (" " + html.escape(content.question.question_id) + " closed with the item" + by
                + change_browser.question_paragraphs(content.question.words))
    elif isinstance(content, the_list.MarkedRead):
        body = "Marked read" + by
    elif isinstance(content, the_list.Reopened):
        body = "Reopened" + by
    elif isinstance(content, the_list.UnparkedForDelivery):
        reason = content.reason
        body = ("Unparked because change " + html.escape(reason.key) + " is ready."
                if isinstance(reason, the_list.LinkedChangeDelivered)
                else "Unparked because the item was delivered.")
        body += by
    elif isinstance(content, the_list.Unparked):
        body = "Unparked" + by
    elif isinstance(content, the_list.ReviewLinksChanged):
        body = content.action + ": " + html.escape(", ".join(content.keys)) + by
    elif isinstance(content, the_list.EvidenceCopyRemoved):
        body = f"Removed the migration copy of {content.copy} from {FIELD_LABELS['evidence']}" + by
    elif isinstance(content, (the_list.AskOpened, the_list.GoGiven)):
        ask = content.ask
        target = names.html(ask.target, owner_name(names.members, ask.target))
        if isinstance(content, the_list.AskOpened):
            body = "Asked nate for " + ("a go" if ask.kind is the_list.AskKind.GO else "approval") + by + ": "
        else:
            body = f"<strong>{ASK_BUTTONS[ask.kind]}</strong>" + by + ": "
        body += html.escape(ask.what) + " → " + target
    elif isinstance(content, the_list.GoNotDelivered):
        recipient = names.html(content.recipient, owner_name(names.members, content.recipient))
        body = "Go not delivered to " + recipient + by + ": " + html.escape(content.ask.what)
    elif isinstance(content, str):
        body = html.escape(content) + by
    else:
        return ""
    return log_row(str(index), change.time, body, today, question=question)


def item_log_html(record: ListRecord, today: datetime, names: staff_names.StaffNames,
                  unread_answers: frozenset[str]) -> str:
    item = record.item
    row_id = html.escape(record.key.replace("/", "-"), quote=True)
    answers = {question.question_id for question in item.questions if question.answer is not None}
    answer_entries: dict[str, the_list.ItemChange] = {}
    rows: list[tuple[datetime, str]] = []
    for index, (change, fields) in enumerate(zip(item.log, the_list.log_field_changes(item), strict=True)):
        content = change.what
        if isinstance(content, the_list.QuestionAnswered) and content.question_id in answers:
            answer_entries[content.question_id] = change
        elif not the_list.is_page_request_receipt(change):
            rows.append((change.time, log_entry_html(change, index, today, names, fields,
                                                    f"list-evidence-{row_id}-{index}", record.keeper)))
    for question in item.questions:
        answer = question.answer
        identity = question.question_id
        if answer is None or identity in unread_answers:
            continue
        asked = datetime.fromisoformat(question.asked_at).astimezone(UTC)
        question_body = " " + html.escape(identity) + log_source_html("nate", names)
        answer_entry = answer_entries.get(identity)
        answered = answer_entry.time if answer_entry is not None else datetime.fromisoformat(answer.answered_at).astimezone(UTC)
        source = (the_list.source_without_clock(answer_entry.source) if answer_entry is not None
                  else question.recipient_id(record.keeper))
        answer_body = log_source_html(source, names)
        rows.append((asked, log_row(f"{identity}-asked", asked,
                                   question_body + change_browser.question_paragraphs(question.words), today,
                                   question="Question")))
        rows.append((answered, log_row(f"{identity}-answered", answered,
                                       answer_body + change_browser.question_paragraphs(answer.text), today,
                                       question="Answer")))
    if not answers.difference(unread_answers):
        return ''.join(markup for _, markup in reversed(rows))
    # Use reverse record order for events with the same time.
    return ''.join(markup for _, markup in sorted(reversed(rows), key=lambda row: row[0], reverse=True))


def item_link(keeper: staff.StaffId, number: int) -> str:
    return '#' + urlencode({"item": f"{keeper}/L{number}"})


@dataclass(frozen=True)
class ListFormRequestPrefix:
    """Identify the form, its unread update, and its feedback recipient."""

    nonce: str
    seen_update: the_list.UnreadUpdate | the_list.NoUnreadUpdate
    recipient: the_list.FeedbackRecipient = the_list.LegacyFeedbackRecipient.KEEPER

    @classmethod
    def parse(cls, request: str) -> tuple[ListFormRequestPrefix, str]:
        """Read the signed prefix and its optional retry suffix."""
        match = re.fullmatch(r"(?P<nonce>[a-f0-9]{32})(?P<update>[a-f0-9]{64})?" +
                             r"(?:~(?P<escaped>\.)?(?P<recipient>[^.\s]+))?(?P<retry>\.[a-f0-9]{32})?", request)
        if match is None:
            raise question_record.AnswerRefused("the List request does not match this page")
        fingerprint = match["update"]
        recipient: the_list.FeedbackRecipient = the_list.LegacyFeedbackRecipient.KEEPER
        if value := match["recipient"]:
            try:
                recipient = the_list.staff_id(unquote(value) if match["escaped"] else value, "producer")
            except the_list.ListError as error:
                raise question_record.AnswerRefused("the List request does not match this page") from error
        update = the_list.UnreadUpdate(fingerprint) if fingerprint else the_list.NoUnreadUpdate.ABSENT
        return cls(match["nonce"], update, recipient), match["retry"] or ""

    def format(self) -> str:
        update = self.seen_update
        unread = update.fingerprint if isinstance(update, the_list.UnreadUpdate) else ""
        if self.recipient is the_list.LegacyFeedbackRecipient.KEEPER:
            return self.nonce + unread
        recipient = self.recipient
        # Keep the retry suffix separate from the recipient.
        if "." in recipient or any(char.isspace() for char in recipient):
            recipient = "." + quote(recipient, safe="").replace(".", "%2E")
        return f"{self.nonce}{unread}~{recipient}"


def signed_token(secret: str, keeper: str, identity: str, prefix: str) -> str:
    return hmac.new(secret.encode(), f"list:{keeper}:{identity}:{prefix}".encode(), hashlib.sha256).hexdigest()


def bootstrap_form_secret(root: Path) -> str:
    """Sign List forms before a console manifest exists."""
    return hmac.new(_BOOTSTRAP_FORM_SECRET, bytes(root.resolve()), hashlib.sha256).hexdigest()


def form_secret(root: Path) -> str:
    try:
        return change_browser.browser_manifest(root / change_page.CONSOLE_FOLDER)[0]
    except FileNotFoundError:
        return bootstrap_form_secret(root)


def request_token(root: Path, keeper: str, identity: str, prefix: str) -> str:
    """Create a token that remains valid across processes."""
    secret = change_browser.browser_manifest(change_page.console_folder(root))[0]
    return signed_token(secret, keeper, identity, prefix)


def signed_item_request(entry: ListRecord, secret: str) -> tuple[str, str]:
    request = ListFormRequestPrefix(secrets.token_hex(16), the_list.unread_update(entry.item), entry.item.producer).format()
    return request, signed_token(secret, entry.keeper, entry.item.id, request)


def signed_form_fields(entry: ListRecord, secret: str) -> str:
    request, token = signed_item_request(entry, secret)
    fields = {"keeper": entry.keeper, "id": entry.item.id, "request": request, "token": token}
    return ''.join(f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
                   for name, value in fields.items())


def feedback_form(entry: ListRecord, secret: str, *, members: staff.StaffSnapshot, parking_label: str = "") -> str:
    hidden = signed_form_fields(entry, secret)
    read = '<button name="action" value="read">Mark read</button>' if isinstance(entry.item.unread_since, datetime) else ''
    key = html.escape(entry.key.replace('/', '-'), quote=True)
    parked = '' if parking_label else ' hidden'
    note = 'Parked until ' + parking_label if parking_label else ''
    producer_name = owner_name(members, entry.item.producer)
    producer_label = html.escape(producer_name, quote=True)
    words_label = html.escape(f"Your words for {producer_name} about this item", quote=True)
    return (f'<form method="post" data-feedback-form data-recipient-name="{producer_label}">' + hidden
            + f'<p data-parked-detail{parked}>{note}</p>'
            + '<div class="list-actions"><button name="action" value="remove">Remove</button>' + read
            + f'<div class="list-parking-actions"><button name="action" value="unpark" id="unpark-{key}"{parked}>Unpark</button>'
            + record_table.park_control("Park", field="until", action="action", value="park",
                                        identity="park-" + key, allow_delivery=True) + '</div></div>'
            + f'<textarea name="words" id="words-{key}" rows="4" maxlength="10000" '
            + f'placeholder="{words_label}" aria-label="{words_label}"></textarea>'
            + '<div class="list-tell"><span role="status" data-send-status hidden></span>'
            + '<button name="action" value="question" disabled>Ask question</button>'
            + f'<button name="action" value="tell" disabled>Tell {producer_label}</button></div></form>')


def review_card_link(key: str, label: str) -> str:
    url = html.escape(review_page.card_url(key), quote=True)
    return f'<a href="{url}" data-list-review-card="" aria-haspopup="dialog">{html.escape(label)}</a>'


def review_summary(item: the_list.ListItem, reviews: Mapping[str, review_items.ReviewItem], today: datetime) -> str:
    """Describe the current turn for the linked reviews without storing their states."""
    if not item.reviews:
        return ""
    linked = [reviews[identity] for identity in item.reviews if identity in reviews]
    ready = [review for review in linked if review.for_you(today)]
    waiting = [review for review in linked if review.state != review_items.ReviewState.DONE and not review.for_you(today)]
    if ready:
        kind = "change" if all(review.type == review_items.ReviewType.CHANGE for review in ready) else "review"
        text = footer_counts.plural(len(ready), kind) + " for you"
    elif waiting:
        text = footer_counts.plural(len(waiting), "review") + " waiting on others"
    elif not has_all_linked_reviews(item, reviews):
        text = REVIEW_UNAVAILABLE
    else:
        text = "Reviews done — decide: close, keep or park"
    destinations = tuple(review.key for review in linked if review.type in review_page.TYPES)
    return review_card_link(destinations[0], text) if len(destinations) == 1 else text


def review_card(identity: str, reviews: Mapping[str, review_items.ReviewItem]) -> str:
    """Link the shown ID only when the Review page contains its card type."""
    if identity not in reviews:
        return html.escape(identity)
    review = reviews[identity]
    if review.type not in review_page.TYPES:
        return html.escape(review.id)
    return review_card_link(review.key, review.id)


NO_FAILED_QUESTIONS: frozenset[str] = frozenset()


def waiting_note(entry: ListRecord, names: staff_names.StaffNames, *, hidden: bool = False) -> str:
    item = entry.item
    notes: dict[str, None] = {}
    for question in item.questions:
        if question.answer is not None:
            continue
        recipient = owner_name(names.members, question.recipient_id(entry.keeper))
        note = f"{recipient} will answer."
        if isinstance(question.recipient, question_record.KeeperForUnavailableProducer):
            producer = owner_name(names.members, question.recipient.producer)
            note = f"{producer} is not live, so {note}"
        notes[note] = None
    text = " ".join(notes) or f"{owner_name(names.members, item.producer)} will answer."
    return f'<p data-waiting-note{" hidden" if hidden else ""}>{html.escape(text)}</p>'


def question_time_html(stamp: str, today: datetime) -> str:
    return time_html(datetime.fromisoformat(stamp), today)


def panel_questions(item: the_list.ListItem, today: datetime, unread_answers: frozenset[str],
                    failed: frozenset[str] = NO_FAILED_QUESTIONS) -> str:
    sections: list[str] = []
    for question in item.questions:
        answer = question.answer
        if answer is None:
            sections.append(page_question.open_question_html(
                question, asked_time_html=question_time_html(question.asked_at, today),
                failed=question.question_id in failed))
        elif question.question_id in unread_answers:
            sections.append(page_question.answered_question_html(question, answer,
                asked_time_html=question_time_html(question.asked_at, today),
                answer_time_html=question_time_html(answer.answered_at, today),
                answer_mark_html=' <span class="question-unread">Unread</span>'))
    return ''.join(sections)


ASK_HEADINGS = {the_list.AskKind.GO: "Needs your go", the_list.AskKind.APPROVAL: "Needs your approval"}
ASK_BUTTONS = {the_list.AskKind.GO: "Go", the_list.AskKind.APPROVAL: "Approved"}
ASK_NOTES = {the_list.AskKind.GO: "Pressing Go sends your go to {agent}.",
             the_list.AskKind.APPROVAL: "Pressing Approved sends your approval to {agent}."}
STAFF_ROLES = {"showrunner": "a showrunner", "unit": "a unit director", "worker": "a worker", "producer": "a producer"}
UNIT_ROLES = {"direct": "a unit director", "merge": "the merge director", "style": "the style director"}


def staff_role_html(member: staff.StaffRow, names: staff_names.StaffNames) -> str:
    """Name the role as the Go box shows it, such as "the merge director of sdlc" or "the List producer"."""
    if member.kind == "producer" and member.name == "morty":
        return "the List producer"
    role = UNIT_ROLES[member.unit_type.value] if isinstance(member.unit_type, staff.UnitDetails) else STAFF_ROLES[member.kind]
    parent = names.members.by_id(member.parent.id) if isinstance(member.parent, staff.ReportsTo) else None
    return role + (" of " + names.html(parent.id, parent.name) if parent is not None else "")


def ask_boxes(entry: ListRecord, secret: str, names: staff_names.StaffNames) -> str:
    """Show one box with a Go or Approved button for each open ask."""
    boxes: list[str] = []
    row_id = entry.key.replace("/", "-")
    for ask in the_list.answerable_asks(entry.item, names.members):
        member = names.members.by_id(ask.target)
        assert member is not None
        box_id = html.escape(f"ask-{row_id}-{ask.ask_id}", quote=True)
        token = signed_token(secret, entry.keeper, entry.item.id, go_ledger.token_prefix(ask.ask_id))
        hidden = ''.join(f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
                         for name, value in (("keeper", entry.keeper), ("id", entry.item.id),
                                             ("ask", ask.ask_id), ("token", token)))
        boxes.append(
            f'<section class="list-ask" data-ask="{ask.ask_id}" aria-labelledby="{box_id}">'
            + f'<h3 id="{box_id}">{ASK_HEADINGS[ask.kind]}</h3>'
            + f'<dl><dt>What</dt><dd>{html.escape(ask.what)}</dd>'
            + f'<dt>Who needs it</dt><dd>{names.html(member.id, member.name)}, {staff_role_html(member, names)}</dd>'
            + f'<dt>Why</dt><dd>{html.escape(ask.why)}</dd></dl>'
            + f'<form method="post" action="{go_ledger.GO_PATH}" data-go-form>' + hidden
            + f'<button type="submit" aria-describedby="{box_id}-note">{ASK_BUTTONS[ask.kind]}</button>'
            + f'<small id="{box_id}-note">{html.escape(ASK_NOTES[ask.kind].format(agent=member.name))}</small>'
            + '</form></section>')
    return '<div class="list-asks">' + ''.join(boxes) + '</div>' if boxes else ''


def detail_panel(entry: ListRecord, sections: str, today: datetime, names: staff_names.StaffNames,
                 unread_answers: frozenset[str], asks: str = "") -> str:
    log = item_log_html(entry, today, names, unread_answers)
    log_id = html.escape('log-' + entry.key.replace('/', '-'), quote=True)
    item = entry.item
    status_time = item.latest_status_time
    status_stamp = " · " + time_html(status_time, today) if item.latest_status and isinstance(status_time, datetime) else ""
    fields = ''.join(
        f'<section class="list-field-{field}"><h3>{FIELD_LABELS[field]}'
        + (status_stamp if field == "latest_status" else "") + '</h3>'
        + field_html(field, value, today, names) + '</section>'
        for field, value in (("state", item.state), ("eta", item.eta), ("latest_status", item.latest_status),
                             ("next_step", item.next_step), ("done_when", item.done_when)))
    return ('<div class="list-panel"><div>' + asks + '<div class="list-item-fields">' + fields
            + '</div><div data-list-item-content>' + sections + '</div>'
            + '</div>' + record_table.log_panel(log_id, log) + '</div>')


class LinkedReviewRead(StrEnum):
    FROM_DISK = "read from disk"
    AFTER_RENDER = "read after the first render"


type ReviewRecords = Mapping[str, review_items.ReviewItem]
type ImmediateReviewRecords = ReviewRecords | Literal[LinkedReviewRead.FROM_DISK]
type ReviewDisplay = ReviewRecords | Literal[LinkedReviewRead.AFTER_RENDER]


def has_all_linked_reviews(item: the_list.ListItem, reviews: ReviewRecords) -> bool:
    return all(identity in reviews for identity in item.reviews)


def read_linked_reviews(root: Path, *, members: staff.StaffSnapshot) -> list[review_items.ReviewItem]:
    """Read the Review records without assigning IDs to legacy questions."""
    return review_items.read(root, members=members)


def linked_reviews(root: Path, members: staff.StaffSnapshot, records: ImmediateReviewRecords,
                   items: Iterable[the_list.ListItem], *, defer_missing: bool = False) -> ReviewRecords:
    if (records is LinkedReviewRead.FROM_DISK
            or (not defer_missing and any(not has_all_linked_reviews(item, records) for item in items))):
        return {review.key: review for review in read_linked_reviews(root, members=members)}
    return records


def review_links(item: the_list.ListItem, reviews: ReviewDisplay, today: datetime) -> str:
    links = ''
    if item.reviews and reviews is not LinkedReviewRead.AFTER_RENDER:
        cards = (review_card(identity, reviews) for identity in item.reviews)
        links = '<p>' + review_summary(item, reviews, today) + '</p><p>' + ' · '.join(cards) + '</p>'
    return '<div data-list-review-links>' + links + '</div>'


def render_review_updates(root: Path, members: staff.StaffSnapshot, review_records: ImmediateReviewRecords) -> str:
    """Render only the linked Review fields for items on the List."""
    entries = [entry for entry in records() if entry.item.reviews]
    if not entries:
        return ""
    reviews = linked_reviews(root, members, review_records, (entry.item for entry in entries))
    today = the_list.now()
    return ''.join(
        f'<template data-list-review-key="{record_table.identity_attribute(entry.key)}"'
        + f' data-list-review-version="{review_version(entry.item)}">'
        + '<span data-list-review-summary>' + review_summary(entry.item, reviews, today) + '</span>'
        + review_links(entry.item, reviews, today) + '</template>'
        for entry in entries)


def review_version(item: the_list.ListItem) -> str:
    return html.escape(json.dumps((str(item.updated), len(item.log), item.reviews)), quote=True)


def waiting_feedback_form(entry: ListRecord, secret: str, parking_label: str, *, unread: bool) -> str:
    if not parking_label and not unread:
        return ""
    parked = '' if parking_label else ' hidden'
    unpark = parked if entry.item.state != the_list.ItemState.CLOSED else ' hidden'
    key = html.escape(entry.key.replace('/', '-'), quote=True)
    note = 'Parked until ' + parking_label if parking_label else ''
    read = '<button name="action" value="read">Mark read</button>' if unread else ''
    return ('<form method="post" data-feedback-form data-waiting-actions>' + signed_form_fields(entry, secret)
            + '<input type="hidden" name="words"><input type="hidden" name="until">'
            + f'<p data-parked-detail{parked}>{note}</p>'
            + '<div class="list-actions">' + read
            + '<div class="list-parking-actions">'
            + f'<button name="action" value="unpark" id="unpark-{key}"{unpark}>Unpark</button></div></div>'
            + '<span role="status" data-send-status hidden></span></form>')


def waiting_details(entry: ListRecord, today: datetime, names: staff_names.StaffNames,
                    unread_answers: frozenset[str], secret: str,
                    reviews: ReviewDisplay, parking_label: str) -> str:
    sections = (waiting_note(entry, names) + '<div data-waiting-questions>'
                + panel_questions(entry.item, today, unread_answers) + '</div>'
                + review_links(entry.item, reviews, today) + waiting_feedback_form(
                    entry, secret, parking_label, unread=bool(unread_answers) or the_list.unread_delivery(entry.item)))
    return detail_panel(entry, sections, today, names, unread_answers, ask_boxes(entry, secret, names))


def details(entry: ListRecord, today: datetime, secret: str, reviews: ReviewDisplay,
            names: staff_names.StaffNames, visibility: the_list.WaitingForAnswer | the_list.QuestionAttention,
            unread_answers: frozenset[str], *, parking_label: str) -> str:
    item = entry.item
    if isinstance(visibility, the_list.WaitingForAnswer):
        return waiting_details(entry, today, names, unread_answers, secret, reviews, parking_label)
    links = [f'<li><a data-record-jump href="{html.escape(item_link(entry.keeper, link.number), quote=True)}">#{link.number}</a>'
             + ' — ' + html.escape(link.condition) + '</li>' for link in item.blocked_by]
    links.extend(f'<li>Related: <a data-record-jump href="{html.escape(item_link(entry.keeper, number), quote=True)}">#{number}</a></li>'
                 for number in item.related_to)
    sections = '<section><h3>Links</h3><ul>' + ''.join(links) + '</ul></section>' if links else ''
    sections += review_links(item, reviews, today)
    questions = panel_questions(item, today, unread_answers, visibility.failed)
    form = feedback_form(entry, secret, members=names.members, parking_label=parking_label)
    return waiting_note(entry, names, hidden=True) + detail_panel(
        entry, sections + questions + form, today, names, unread_answers, ask_boxes(entry, secret, names))


def item_rows(entry: ListRecord, names: staff_names.StaffNames, today: datetime, index: int, secret: str,
              reviews: ReviewDisplay, *, owner_cell: str, owner_sort: str, search_text: SearchText, parked: bool,
              open_question: bool, open_ask: bool, visibility: the_list.WaitingForAnswer | the_list.QuestionAttention,
              unread_answers: frozenset[str],
              hidden: bool = False) -> str:
    item = entry.item
    producer = owner_name(names.members, item.producer)
    unread = isinstance(item.unread_since, datetime)
    waiting = isinstance(visibility, the_list.WaitingForAnswer)
    due = the_list.eta_due(item.eta)
    eta_sort: tuple[int, float | str] = (0, due.timestamp()) if due is not None else (1, eta_text(item.eta, today).casefold())
    values = (item.number, owner_sort, producer.casefold(), item.title.casefold(), item.state.value.casefold(),
              item.next_step.casefold(), eta_sort, item.unread_since.timestamp() if isinstance(item.unread_since, datetime) else 0)
    parking = (the_list.ParkUntilDelivered.DELIVERED.value
               if item.parked_until is the_list.ParkUntilDelivered.DELIVERED else str(parked).lower())
    key = entry.key
    attributes = {
        "key": key, "number": str(item.number), "owner": str(item.owner), "producer": item.producer,
        "keeper-produces": str(item.producer == entry.keeper).lower(), "state": item.state.value,
        "unread": str(unread).lower(), "parked": parking, "waiting-for-answer": str(waiting).lower(),
        "open-question": str(open_question).lower(), "unread-delivery": str(the_list.unread_delivery(item)).lower(),
        "unread-answer": str(bool(unread_answers)).lower(), "open-ask": str(open_ask).lower(),
        "title": search_text.title, "owner-name": search_text.owner,
        "order": str(index), "sort-values": json.dumps(values, ensure_ascii=False),
    }
    keeper = names.members.by_id(entry.keeper)
    if keeper is not None:
        attributes["send-receiver"] = keeper.name
    attrs = ''
    for name, value in attributes.items():
        escaped = (record_table.identity_attribute(value) if name in ("key", "owner", "producer")
                   else html.escape(value, quote=True))
        attrs += f' data-{name}="{escaped}"'
    panel = f'list-detail-{index}'
    row_id = html.escape(key.replace("/", "-"))
    number = (f'<button type="button" class="list-toggle" aria-expanded="false" aria-controls="{panel}" '
              + f'aria-label="Show item {item.number}" id="list-toggle-{row_id}">'
              + f'<span class="list-chevron" aria-hidden="true">›</span>{item.number}</button>')
    title = '<strong>' + html.escape(item.title) + '</strong>'
    if item.unit:
        title += '<small>' + field_html("unit", item.unit, today, names) + '</small>'
    tone = {the_list.ItemState.WAITING_FOR_NATE: "warning", the_list.ItemState.BLOCKED: "error",
            the_list.ItemState.WORKING: "accent", the_list.ItemState.DELIVERED: "room",
            the_list.ItemState.DONE: "room"}.get(item.state, "muted")
    state = f'<span class="list-state {tone}">' + field_html("state", item.state, today, names) + '</span>'
    if open_question:
        state += f'<small data-open-question-note>{ListChip.PRODUCER_ANSWERING.label}</small>'
    state += check_row_html(item.next_check, today, parked=parked)
    parking_label = field_html("parked_until", item.parked_until, today, names) if parked else ""
    if parking_label:
        state += '<small data-parked-note>Parked until ' + parking_label + '</small>'
    if item.reviews:
        if reviews is LinkedReviewRead.AFTER_RENDER:
            attrs += f' data-list-review-version="{review_version(item)}"'
            state += f'<small data-list-review-summary="{REVIEW_UNAVAILABLE}">Checking linked reviews…</small>'
        else:
            state += '<small data-list-review-summary>' + review_summary(item, reviews, today) + '</small>'
    update = '<span class="list-dot" role="img" aria-label="Unread update"></span>' if unread else '<span aria-label="No unread update">—</span>'
    if unread_answers:
        update += question_path.NEW_ANSWER_HTML
    sent = the_list.feedback_notice_time(item)
    if isinstance(sent, datetime):
        feedback = tuple(the_list.feedback_requests(item).values())[-1]
        recipient_id = feedback.recipient_id(entry.keeper)
        recipient = names.html(recipient_id, owner_name(names.members, recipient_id))
        notice = "sent to " + recipient
        if feedback.state == "alerted":
            alert = names.html(feedback.alerted_to) + " told" if feedback.alerted_to else "no showrunner for alert"
            notice = f"not sent to {recipient}; {alert}"
        update += '<small>' + notice + ", " + html.escape(list_time(sent, today)) + '</small>'
    overdue = due is not None and due < today and item.state != the_list.ItemState.CLOSED
    eta_class = "list-eta warning" if overdue else "list-eta"
    full_eta = field_html("eta", item.eta, today, names)
    short_eta = html.escape(eta_cell_text(item.eta, today))
    eta_cell = (f'<span class="{eta_class}" id="list-eta-{row_id}" tabindex="0" title="{full_eta}">'
                + f'<span class="list-eta-text">{short_eta}</span>'
                + f'<span class="list-eta-full" aria-hidden="true">{full_eta}</span></span>')
    cells = (number, owner_cell, field_html("producer", item.producer, today, names),
             title, state, field_html("next_step", item.next_step, today, names), eta_cell, update)
    row = ''.join(f'<td' + (' class="record-id"' if label == '#' else '')
                  + f' data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    hidden_attr = ' hidden' if hidden else ''
    body = details(entry, today, secret, reviews, names, visibility, unread_answers, parking_label=parking_label)
    revision = detail_revision(item, body)
    return (f'<tbody data-list-item{attrs} data-detail-revision="{revision}"{hidden_attr}><tr class="list-row">' + row
            + f'</tr><tr data-detail id="{panel}" hidden><td colspan="{len(HEADERS)}">'
            + body + '</td></tr></tbody>')


def detail_revision(item: the_list.ListItem, body: str) -> str:
    version = re.sub(r'<div data-list-review-links>.*?</div>', '', body, count=1, flags=re.DOTALL)
    # Keep the unread fingerprint in the detail revision.
    version = re.sub(r'(name="request" value=")[a-f0-9]{32}', r'\1', version)
    version = re.sub(r'(name="token" value=")[a-f0-9]{64}', r'\1', version)
    return hashlib.sha256(f'{item.updated}\0{item.title}\0{version}'.encode()).hexdigest()

@dataclass(frozen=True)
class ListFeedbackDelivery:
    keeper: staff.StaffId
    identity: str
    request: str


@dataclass(frozen=True)
class ListQuestionDelivery:
    delivery: change_page.MessageDelivery
    status: change_page.DeliveryStatus


class UnparkOutcome(Enum):
    """The Unpark result to report on the List page."""

    CLEARED = "cleared"
    CLEARED_NEWER_PARKING_ACTIVE = "cleared_newer_parking_active"
    EXPIRED = the_list.UnparkNoChangeReason.EXPIRED.value
    NO_ACTIVE_PARKING = the_list.UnparkNoChangeReason.NO_ACTIVE_PARKING.value


@dataclass(frozen=True)
class ListUnparkSubmission:
    outcome: UnparkOutcome
    delivery: ListFeedbackDelivery | change_page.NoMessageDelivery

    def response_outcome(self, current: SavedActionState) -> UnparkOutcome:
        if self.outcome is UnparkOutcome.CLEARED and current["parked"] == "true":
            return UnparkOutcome.CLEARED_NEWER_PARKING_ACTIVE
        return self.outcome


ListActionSubmission = ListFeedbackDelivery | ListQuestionDelivery | ListUnparkSubmission | change_page.NoMessageDelivery


class SavedActionState(TypedDict):
    unread: str
    unread_sort: float
    item_state: str
    parked: str
    parked_until: str
    check_html: str
    waiting_for_answer: str
    open_question: str
    open_ask: str
    unread_delivery: NotRequired[str]
    unread_answer: NotRequired[str]
    waiting_details_html: NotRequired[str]
    review_summary_html: NotRequired[str]
    park_request_saved: NotRequired[bool]
    detail_html: NotRequired[str]
    detail_revision: NotRequired[str]
    detail_closed: NotRequired[bool]


def saved_action_state(keeper: str, identity: str, *, status: change_page.DeliveryStatus | None = None,
                       park_request: str = "",
                       review_records: ImmediateReviewRecords = LinkedReviewRead.FROM_DISK,
                       include_detail: bool = False) -> SavedActionState:
    owner = staff.StaffId(keeper)
    return action_state(owner, the_list.load(owner, identity), status=status,
                        park_request=park_request, review_records=review_records, include_detail=include_detail)


def action_state(owner: staff.StaffId, item: the_list.ListItem, *,
                 status: change_page.DeliveryStatus | None = None, park_request: str = "",
                 review_records: ImmediateReviewRecords = LinkedReviewRead.FROM_DISK,
                 include_detail: bool = False) -> SavedActionState:
    zone = user_zone()
    today = the_list.now()
    local_today = today.astimezone(zone)
    parking = item.parked_until
    parked = the_list.parking_active(parking, today)
    parked_until = parked_label(parking, zone) if parked and parking is not the_list.NoTimestamp.UNSET else ""
    unread = item.unread_since
    unread_answers = the_list.unread_answer_ids(item)
    reading = status if status is not None else change_page.DeliveryStatus(review_link.review_root())
    open_question = item.state != the_list.ItemState.CLOSED and the_list.has_open_question(item)
    visibility = the_list.question_visibility(owner, item, reading)
    waiting = open_question and isinstance(visibility, the_list.WaitingForAnswer)
    open_ask = bool(the_list.answerable_asks(item, staff.snapshot()))
    saved: SavedActionState = {"unread": str(isinstance(unread, datetime)).lower(),
                               "unread_sort": unread.timestamp() if isinstance(unread, datetime) else 0,
                               "item_state": item.state.value,
                               "parked": str(parked).lower(), "parked_until": parked_until,
                               "check_html": check_row_html(item.next_check, local_today, parked=parked),
                               "waiting_for_answer": str(waiting).lower(),
                               "open_question": str(open_question).lower(),
                               "open_ask": str(open_ask).lower(),
                               "unread_delivery": str(the_list.unread_delivery(item)).lower(),
                               "unread_answer": str(bool(unread_answers)).lower()}
    if park_request:
        try:
            prefix, retry = ListFormRequestPrefix.parse(park_request)
        except question_record.AnswerRefused:
            # Older receipts can have opaque request IDs.
            pass
        else:
            park_request = prefix.format() + retry
        saved["park_request_saved"] = the_list.has_park_receipt(item, park_request)
    if waiting or include_detail and item.state != the_list.ItemState.CLOSED:
        members = staff.snapshot()
        names = staff_names.StaffNames(members)
        reviews: ReviewRecords = {}
        if item.reviews:
            try:
                reviews = linked_reviews(reading.root, members, review_records, (item,))
            except (OSError, ValueError, change_review.ChangeReviewError) as error:
                print(f"review-links List links: {error}", file=sys.stderr)
                if review_records is not LinkedReviewRead.FROM_DISK:
                    reviews = review_records
            saved["review_summary_html"] = review_summary(item, reviews, today)
        secret = form_secret(reading.root)
        body = details(ListRecord(owner, item), local_today, secret, reviews, names,
                       visibility, unread_answers, parking_label=parked_until)
        if waiting:
            saved["waiting_details_html"] = body
        if include_detail:
            revision = detail_revision(item, body)
            saved.update(detail_html=body, detail_revision=revision)
    if include_detail and item.state == the_list.ItemState.CLOSED:
        saved["detail_closed"] = True
    return saved


def _mark_seen_update_read(keeper: staff.StaffId, item: the_list.ListItem,
                           update: the_list.UnreadUpdate | the_list.NoUnreadUpdate) -> None:
    if isinstance(update, the_list.UnreadUpdate) and update == the_list.unread_update(item):
        try:
            _ = the_list.mark_read(keeper, item.id, source=the_list.PAGE_SOURCE, update=update)
        except (OSError, the_list.ListError):
            pass


def _replay_ask(keeper: staff.StaffId, item: the_list.ListItem, receipt: the_list.QuestionRequestReceipt,
                status: change_page.DeliveryStatus) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    if item.state == the_list.ItemState.CLOSED:
        return change_page.NoMessageDelivery()
    question = next((question for question in item.questions
                     if question.question_id == receipt.question_id and question.answer is None), None)
    if question is None:
        return change_page.NoMessageDelivery()
    target = the_list.list_question_target(keeper, item, question.recipient_id(keeper))
    saved = target.saved(question)
    page = saved.page()
    path = status.path(page, saved.source.row)
    with status.locked():
        view = change_page.question_delivery_view(status, saved)
        attempt = view.attempt
        if not isinstance(attempt, delivery_state.QuestionAttempt):
            return change_page.NoMessageDelivery()
        if isinstance(view, change_page.QuestionSendPermitted) and view.state is not delivery_state.QuestionSendState.FAILED:
            return change_page.ready_and_claim_question(status, path, attempt, saved)
        if isinstance(view, change_page.ResendBlocked):
            status.defer_question(path)
        return change_page.question_envelope(page, attempt)


def _submit_ask(keeper: staff.StaffId, identity: str, request: the_list.PageRequest,
                status: change_page.DeliveryStatus) -> change_page.MessageDelivery | change_page.NoMessageDelivery:
    """Serialize the receipt check and submission of List Ask requests."""
    with exclusive(the_list.folder(keeper) / f".{identity}.ask.lock"):
        item = the_list.load(keeper, identity)
        receipt = next((entry.what for entry in item.log
                        if entry.source == the_list.PAGE_SOURCE and isinstance(entry.what, the_list.QuestionRequestReceipt)
                        and entry.what.request.request == request.request), None)
        if receipt is None:
            return the_list.ask_question(keeper, identity, request, status)
        if receipt.request != request:
            raise the_list.ListError("this request already has a different action or words")
        return _replay_ask(keeper, item, receipt, status)


def submit(root: Path, body: str, status: change_page.DeliveryStatus | None = None) -> ListActionSubmission:
    return save_action(root, body, status)[0]


def save_action(root: Path, body: str, status: change_page.DeliveryStatus | None = None, *,
                review_records: ImmediateReviewRecords = LinkedReviewRead.FROM_DISK,
                include_detail: bool = False) -> tuple[ListActionSubmission, SavedActionState | None]:
    """Accept signed item actions and save their notices."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if (set(fields) != {"keeper", "id", "request", "token", "action", "words", "until"}
            or any(len(values) != 1 for values in fields.values())):
        raise question_record.AnswerRefused("use the form on this page")
    keeper, identity = staff.StaffId(fields["keeper"][0]), fields["id"][0]
    if re.fullmatch(r"L[1-9]\d*", identity) is None:
        raise question_record.AnswerRefused("the List request does not match this page")
    prefix, retry = ListFormRequestPrefix.parse(fields["request"][0])
    signed_prefix = prefix.format()
    request = signed_prefix + retry
    expected = signed_token(form_secret(root), keeper, identity, signed_prefix)
    token = fields["token"][0].encode()
    if not secrets.compare_digest(token, expected.encode()):
        bootstrap = signed_token(bootstrap_form_secret(root), keeper, identity, signed_prefix)
        if not secrets.compare_digest(token, bootstrap.encode()):
            raise question_record.AnswerRefused("the List token does not match this page")
    action, words, until = fields["action"][0], fields["words"][0].strip(), fields["until"][0]
    read_edit = None
    if action == "read":
        read_edit = the_list.mark_read_edit(keeper, identity, source=the_list.PAGE_SOURCE, update=prefix.seen_update,
                                           page_request=the_list.PageRequest(request, "read", words))
        delivery = _notice_delivery(keeper, read_edit.item, request)
    else:
        delivery = _submit_action(root, keeper, identity, request, prefix, action, words, until, status)
    if not include_detail:
        return delivery, None
    try:
        item = read_edit.item if read_edit is not None else the_list.load(keeper, identity)
        state = action_state(keeper, item, status=status, review_records=review_records, include_detail=True)
    except Exception as error:
        print(f"review-links List saved {identity}: {error}", file=sys.stderr)
        return delivery, None
    return delivery, state


def _submit_action(root: Path, keeper: staff.StaffId, identity: str, request: str,
                   prefix: ListFormRequestPrefix, action: str, words: str, until: str,
                   status: change_page.DeliveryStatus | None) -> ListActionSubmission:
    item = the_list.load(keeper, identity)
    if action == "question":
        status = change_page.DeliveryStatus(root) if status is None else status
        question = _submit_ask(keeper, identity, the_list.PageRequest(request, "question", words), status)
        if isinstance(question, change_page.MessageDelivery):
            _mark_seen_update_read(keeper, item, prefix.seen_update)
            return ListQuestionDelivery(question, status)
        return question
    if action == "remove":
        notice = the_list.PageItemNotice(request, f"List page: nate removed item {item.number}.", "remove", words)
        changed = the_list.close(keeper, identity, "Removed by nate on the List page.", source=the_list.PAGE_SOURCE, notice=notice)
    elif action == "park":
        if until != the_list.ParkUntilDelivered.DELIVERED and the_list.ISO_TIME.fullmatch(until) is None:
            raise question_record.AnswerRefused("choose Until delivered, a parking date, or a time with a UTC offset")
        zone = user_zone()

        def save_park(parked_until: datetime | the_list.ParkUntilDelivered,
                      check_deadline: Callable[[datetime], None] | None = None) -> the_list.ListItem:
            label = parking_notice_label(parked_until, zone)
            notice = the_list.PageItemNotice(request, f"List page: nate parked item {item.number} {label}.", "park", words)
            return the_list.park(keeper, identity, parked_until, source=the_list.PAGE_SOURCE,
                                 notice=notice, check_deadline=check_deadline)

        changed = (save_park(the_list.ParkUntilDelivered.DELIVERED) if until == the_list.ParkUntilDelivered.DELIVERED
                   else review_actions.save_timed_park(until, zone, save_park, require_offset=len(until) != 10))
    elif action == "unpark":
        notice = the_list.PageItemNotice(request, f"List page: nate unparked item {item.number}.", "unpark", words)
        changed = the_list.unpark(keeper, identity, source=the_list.PAGE_SOURCE, notice=notice).item
    elif action == "tell" and words and len(words) <= 10000:
        saved = the_list.save_feedback(keeper, identity, the_list.PageFeedback(
            request, words, read_update=prefix.seen_update, recipient=prefix.recipient))
        _mark_seen_update_read(keeper, item, saved.read_update)
        return ListFeedbackDelivery(keeper, identity, request) if saved.state == "pending" else change_page.NoMessageDelivery()
    else:
        recipient = keeper if prefix.recipient is the_list.LegacyFeedbackRecipient.KEEPER else prefix.recipient
        raise question_record.AnswerRefused(f"Enter words for {owner_name(staff.snapshot(), recipient)}.")
    delivery = _notice_delivery(keeper, changed, request)
    if action == "unpark":
        outcome = (UnparkOutcome(the_list.unpark_no_change_reason(changed, request).value)
                   if request not in the_list.feedback_requests(changed) else UnparkOutcome.CLEARED)
        return ListUnparkSubmission(outcome, delivery)
    return delivery


def _notice_delivery(keeper: staff.StaffId, item: the_list.ListItem,
                     request: str) -> ListFeedbackDelivery | change_page.NoMessageDelivery:
    """Send the notice that an item action saved, if it is still pending."""
    notice = the_list.feedback_requests(item).get(request)
    return (ListFeedbackDelivery(keeper, item.id, request)
            if notice is not None and notice.state == "pending" else change_page.NoMessageDelivery())


def alert_showrunners(item: the_list.ListItem, members: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """Find the first showrunner above the item owner without session or document reads."""
    return () if isinstance(item.owner, the_list.UnassignedOwner) else change_page.staff_showrunner(item.owner, members)


def deliver(submission: ListActionSubmission) -> None:
    _ = deliver_with_outcome(submission)


def deliver_with_outcome(submission: ListActionSubmission) -> change_page.DeliveryOutcome:
    """Send saved List words with the shared retry and alert helpers."""
    delivery = submission.delivery if isinstance(submission, ListUnparkSubmission) else submission
    if isinstance(delivery, change_page.NoMessageDelivery):
        return change_page.DeliveryUnconfirmed()
    if isinstance(delivery, ListQuestionDelivery):
        return change_page.deliver_with_outcome(delivery.delivery, delivery.status)
    outcome: change_page.DeliveryOutcome = change_page.DeliveryUnconfirmed()

    def send(item: the_list.ListItem, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
        nonlocal outcome
        settings = conf.read("review_links")
        tries = settings.whole("delivery_tries")
        message = (f"List page: nate's words for item {item.number}\n{feedback.words}"
                   if feedback.action == "tell" else feedback.words)
        source = the_list.folder(delivery.keeper) / f"{item.id}.md"
        key = change_page.review_delivery_key(source, item.id, feedback.request)
        recipient = feedback.recipient_id(delivery.keeper)
        failures = change_page.send_with_retries(change_page.SENDER, (recipient,), message,
                                                 tries, settings.number("delivery_pause_seconds"), key=key)
        if not failures:
            outcome = change_page.DeliveryConfirmed()
            return replace(feedback, state="sent")
        outcome = change_page.FailedDelivery(feedback.request, source, item.id, failures[-1].error)
        owners = tuple(owner for owner in alert_showrunners(item, staff.snapshot()) if owner.id != recipient)
        reached = change_page.alert_delivery_failures(failures, tries, source=source, row=item.id, message=message,
            recipients=tuple(owner.id for owner in owners), attempt=feedback.request, title="List feedback needs attention")
        return replace(feedback, state="alerted", alerted_to=", ".join(owner.name for owner in owners)) if reached else feedback
    the_list.deliver_feedback(delivery.keeper, delivery.identity, delivery.request, send)
    return outcome


def _signed_go(root: Path, body: str) -> tuple[staff.StaffId, str, str, str]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"keeper", "id", "ask", "token", "page"} or any(len(values) != 1 for values in fields.values()):
        raise question_record.AnswerRefused("use the Go button on this page")
    keeper = staff.StaffId(fields["keeper"][0])
    identity, ask_id = fields["id"][0], fields["ask"][0]
    if re.fullmatch(r"L[1-9]\d*", identity) is None or the_list.ASK_ID.fullmatch(ask_id) is None:
        raise question_record.AnswerRefused("the Go does not match this page")
    prefix = go_ledger.token_prefix(ask_id)
    token = fields["token"][0].encode()
    if not any(secrets.compare_digest(token, signed_token(secret, keeper, identity, prefix).encode())
               for secret in (form_secret(root), bootstrap_form_secret(root))):
        raise question_record.AnswerRefused("the List token does not match this page")
    return keeper, identity, ask_id, fields["page"][0]


class GoSavedUnsent(OSError):
    """The List holds the Go, but the server did not send it; the next server start sends it."""


def _remove_unheld_go(ledger: Path, keeper: staff.StaffId, identity: str, go_id: str) -> bool | None:
    """Remove the ledger record only when a read of the item shows that no ask holds this go id.

    Return True when the record is removed and False when an ask holds the go id.
    Return None when the read fails: the record stays, and `check` and `deliver_go` need a closed ask with this go id.
    The caller holds the ledger lock, so no Go click is between its record and its List save.
    """
    try:
        item = the_list.load(keeper, identity)
    except (OSError, ValueError) as error:
        print(f"review-links List Go {identity}: the ledger record stays; the read failed: {error}", file=sys.stderr)
        return None
    if any(ask.go_id == go_id for ask in item.asks):
        return False
    go_ledger.remove(ledger, go_id)
    return True


def save_go(root: Path, ledger: Path, body: str, client: str, *,
            review_records: ImmediateReviewRecords = LinkedReviewRead.FROM_DISK
            ) -> tuple[str, SavedActionState | None]:
    """Write the ledger record, then close the ask with its go id."""
    keeper, identity, ask_id, page = _signed_go(root, body)
    members = staff.snapshot()
    item = the_list.load(keeper, identity)
    ask = next((ask for ask in the_list.answerable_asks(item, members) if ask.ask_id == ask_id), None)
    target = members.by_id(ask.target) if ask is not None else None
    if ask is None or target is None:
        raise question_record.AnswerRefused("This ask is no longer open.")
    recipients = tuple(dict.fromkeys((target.id, the_list.producer_id(members, "morty"))))
    with go_ledger.locked(ledger):
        entry = go_ledger.record(ledger, ask=ask, item=f"{keeper}/{identity}", recipients=recipients, client=client,
                                 page=page, moment=the_list.now())
        try:
            edit = the_list.give_go(keeper, identity, ask_id, entry["go_id"], source="nate")
        except the_list.SavedReadFailed as error:
            raise GoSavedUnsent(str(error)) from error
        except BaseException as error:
            if _remove_unheld_go(ledger, keeper, identity, entry["go_id"]) is False:
                raise GoSavedUnsent(str(error)) from error
            raise
    try:
        state = action_state(keeper, edit.item, review_records=review_records, include_detail=True)
    except Exception as error:
        print(f"review-links List Go {identity}: {error}", file=sys.stderr)
        return entry["go_id"], None
    return entry["go_id"], state


def deliver_go(ledger: Path, go_id: str) -> None:
    """Send the Go to each unsent recipient through the List Tell sender and keep the result in the ledger.

    When the Go moved the item to Working, the producer gets one message with the next step.
    A producer that is a Go recipient gets the Go and the next step together; another producer gets the next step alone.
    A failed send alerts the item's showrunners and adds a "Go not delivered" log line for nate, once per recipient.
    """
    entry = go_ledger.read(ledger, go_id)
    found = go_ledger.closed_ask(entry)
    pending = tuple(staff.StaffId(recipient) for recipient, sent in entry["sent"].items() if not sent)
    if found is None or not pending:
        return
    keeper, item, ask = found
    members = staff.snapshot()
    target = members.by_id(ask.target)
    settings = conf.read("review_links")
    tries = settings.whole("delivery_tries")
    pause = settings.number("delivery_pause_seconds")
    message = go_ledger.message(item, ask, go_id, target.name if target is not None else ask.target)
    source = the_list.folder(keeper) / f"{item.id}.md"
    key = change_page.review_delivery_key(source, item.id, go_id)
    next_step = the_list.go_next_step_request(go_id)
    notice = the_list.feedback_requests(item).get(next_step)
    producer = notice.recipient_id(keeper) if notice is not None and notice.state == "pending" else None
    plain = tuple(recipient for recipient in pending if recipient != producer)
    failures = change_page.send_with_retries(change_page.SENDER, plain, message, tries, pause, key=key)
    failed = {failure.recipient for failure in failures}
    sent = [recipient for recipient in plain if recipient not in failed]
    if producer is not None and producer in pending:
        def send(_: the_list.ListItem, saved: the_list.PageFeedback) -> the_list.PageFeedback:
            missed = change_page.send_with_retries(change_page.SENDER, (producer,), message + "\n" + saved.words,
                                                   tries, pause, key=key)
            failures.extend(missed)
            if missed:
                return saved
            sent.append(producer)
            return replace(saved, state="sent")
        the_list.deliver_feedback(keeper, item.id, next_step, send)
    go_ledger.mark(ledger, go_id, "sent", sent)
    if producer is not None and producer not in entry["sent"]:
        deliver(ListFeedbackDelivery(keeper, item.id, next_step))
    # Report each failed recipient to nate once per Go; a resend at a later start reports nothing new.
    new = [failure for failure in failures if not entry["reported"].get(str(failure.recipient), False)]
    if not new:
        return
    owners = tuple(owner.id for owner in alert_showrunners(item, members) if owner.id not in pending)
    _ = change_page.alert_delivery_failures(new, tries, source=source, row=item.id, message=message,
                                            recipients=owners, attempt=go_id, title="List Go needs attention")
    reported = tuple(staff.StaffId(failure.recipient) for failure in new if isinstance(failure.recipient, str))
    for recipient in reported:
        _ = the_list.log_go_not_delivered(keeper, item.id, go_id, recipient)
    go_ledger.mark(ledger, go_id, "reported", reported)


def recover_goes(ledger: Path) -> None:
    """Send each unsent Go again once at server start, through the same path as a click.

    Remove each record whose item a read shows with no ask that holds its go id.
    """
    for go_id in go_ledger.unsent(ledger):
        try:
            with go_ledger.locked(ledger):
                keeper, _, identity = go_ledger.read(ledger, go_id)["item"].partition("/")
                if _remove_unheld_go(ledger, staff.StaffId(keeper), identity, go_id):
                    continue
            deliver_go(ledger, go_id)
        except (OSError, ValueError) as error:
            print(f"List Go recovery {go_id}: {error}", file=sys.stderr)


def _go_sends_notice(ledger: Path, keeper: staff.StaffId, feedback: the_list.PageFeedback) -> bool:
    """Find a Go's next-step notice that only the Go path sends: the Go record has its recipient unsent.

    The Go path sends that recipient the Go and the next step in one message.
    """
    go_id = the_list.next_step_go_id(feedback.request)
    if go_id is None:
        return False
    try:
        entry = go_ledger.read(ledger, go_id)
    except (OSError, ValueError):
        return False
    return entry["sent"].get(feedback.recipient_id(keeper)) is False


def recover_pending(ledger: Path) -> None:
    """Retry each pending request once at server start; leave a Go's next-step notice to the Go path."""
    for entry in records(include_closed=True):
        for feedback in the_list.feedback_requests(entry.item).values():
            if feedback.state == "pending" and not _go_sends_notice(ledger, entry.keeper, feedback):
                try:
                    deliver(ListFeedbackDelivery(entry.keeper, entry.item.id, feedback.request))
                except (OSError, ValueError) as error:
                    print(f"List feedback recovery {entry.key}: {error}", file=sys.stderr)


def change_counts() -> dict[str, int]:
    """Count each Changes page tab from one read of the global lists."""
    snapshots = {path: change_review.review_snapshot(path, include_reviewed=False)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + "*.md"))}
    prepared = change_review.PreparedChangeLists(snapshots, change_page.list_routing(snapshots))
    totals: dict[str, int] = {}
    for _, owner, _ in prepared.routed:
        totals[owner.tab] = totals.get(owner.tab, 0) + 1
    return totals


def count_link(page: str, place: str, text: str) -> str:
    """Open the count's page through the console navigation."""
    path = f"/{change_page.CONSOLE_FOLDER}/" + console.place_query(page, place)
    return f'<a href="{html.escape(path, quote=True)}" target="_top">{text}</a>'


def counts_strip(members: staff.StaffSnapshot, names: staff_names.StaffNames) -> str:
    """Use the status line readers for the counts beside their existing pages."""
    identities = QuestionStaff(members, session_staff.MemberSources(()))
    chips: list[str] = []
    owners = sorted((owner for owner in staff.active(members, "showrunner") if isinstance(owner.doc, staff.LiveDoc)),
                    key=lambda member: member.name.casefold())
    errors = (*console.ITEM_ERRORS, staff.StaffRefused, RegistrationFailure)
    changes: dict[str, int] = {}
    if owners:
        try:
            changes = change_counts()
        except errors:
            pass
    for owner in owners:
        try:
            questions, _ = counts_row.question_counts(owner, user_zone(), identities, None)
        except errors:
            questions = 0
        parts: list[str] = []
        for count, label, path in ((questions, "question", "/console/changes.html?type=questions"),
                                   (changes.get(owner.name, 0), "change", "/console/changes.html")):
            if not count:
                continue
            text = f'<b>{count}</b> {label}' + ('s' if count != 1 else '')
            path += '#' + urlencode({"showrunner": owner.name})
            parts.append(count_link("questions" if label == "question" else "changes", path, text))
        if parts:
            chips.append('<span class="list-count-chip">' + names.html(owner.id, owner.name) + ' · ' + ' · '.join(parts) + '</span>')
    try:
        machines = rebuild_row.read(conf.path("statusline")).machines
    except errors:
        machines = ()
    if machines:
        label = "rebuild" if len(machines) == 1 else "rebuilds"
        chips.append('<span class="list-count-chip">'
                     + count_link("rebuilds", "/console/rebuilds.html", f'<b>{len(machines)}</b> {label}')
                     + '</span>')
    return ('<aside class="list-counts" aria-label="Waiting on other pages"><span>Waiting on other pages:</span>'
            + ''.join(chips) + '</aside>') if chips else ''


def staff_chips(members: staff.StaffSnapshot, identities: Iterable[the_list.ItemOwner],
                counts: Counter[str]) -> list[record_table.Chip]:
    """Build a counted chip for each staff ID, in name order."""
    choices = [(re.sub(r"(?:-unit)+\Z", "", owner_name(members, identity)) or str(identity), f"staff:{identity}")
               for identity in identities]
    choices.sort(key=lambda choice: (choice[0].casefold(), choice[1]))
    return [record_table.Chip("all", "All", counts["all"]),
            *(record_table.Chip(value, html.escape(label), counts[value]) for label, value in choices)]


@dataclass(frozen=True)
class ListQuery:
    selected: dict[str, str]
    words: str
    unread: bool
    keys: frozenset[str]
    item: str
    reveal: bool

    @classmethod
    def read(cls, query: str, items: list[ListRecord]) -> ListQuery:
        fields = {name: values for name, values in parse_qs(query, keep_blank_values=True).items()
                  if name in record_table.LIST_QUERY_FIELDS}

        def first(name: str) -> str:
            return fields.get(name, [""])[0]

        raw_show = first("show")
        if raw_show == UNREAD_FIELD:
            fields[UNREAD_FIELD] = [UNREAD_VALUE]
        aliases = SHOW_ALIASES.get(raw_show, SHOW_ALIASES["*"])
        show = aliases.get(first("state"), aliases.get("", raw_show))
        owner = next((value for key in record_table.OWNER_QUERY_KEYS if (value := first(key))), "")
        producer = first("producer")
        keys = frozenset(fields.get("key", ()))
        item = first("item")
        if keys and item:
            if any(entry.key == item for entry in items):
                keys |= {item}
            else:
                keys -= {item}
        return cls({
            "show": show if show in ListChip and show != ListChip.ALL else "",
            "owner": owner if owner and any(entry.item.owner == owner for entry in items) else "",
            "producer": producer if producer and any(entry.item.producer == producer for entry in items) else "",
        }, first("q"), first(UNREAD_FIELD) == UNREAD_VALUE, keys, item, first("reveal") == "1")

    def eligible(self, entry: ListRecord, text: SearchText, search: str, *, parked: bool) -> bool:
        item = entry.item
        if self.keys and entry.key not in self.keys:
            return False
        if self.unread and str(isinstance(item.unread_since, datetime)).lower() != UNREAD_MATCH:
            return False
        if search:
            search_values = {"number": str(item.number), "ownerName": text.owner, "title": text.title,
                             "state": STATE_SEARCH_TEXT[item.state],
                             "parkingLabel": PARKED_SEARCH_LABEL if parked else ""}
            if not any((re.sub(pattern, "", search) if pattern else search) in search_values[field]
                       for field, pattern in SEARCH_FIELDS.items()):
                return False
        return True

    def reveals(self, entry: ListRecord, open_question: bool) -> bool:
        if self.item != entry.key:
            return False
        values = {"openQuestion": str(open_question).lower(),
                  UNREAD_FIELD: str(isinstance(entry.item.unread_since, datetime)).lower()}
        active = {UNREAD_FIELD: self.unread}
        return self.reveal or any((not filter_name or active[filter_name]) and values[field] == value
                                  for field, value, filter_name in LINK_REVEAL_FIELDS)


def render(root: Path, *, include_counts: bool = True, defer_counts: bool = False,
           status: change_page.DeliveryStatus | Literal["standalone"] = "standalone",
           review_records: ReviewRecords | LinkedReviewRead = LinkedReviewRead.FROM_DISK, query: str = "") -> bytes:
    started, read_at = time.perf_counter(), time.time()
    members = staff.snapshot()
    names = staff_names.StaffNames(members)
    items: list[ListRecord] = []
    closed_keys: list[str] = []
    for entry in records(include_closed=True):
        if entry.item.state == the_list.ItemState.CLOSED:
            closed_keys.append(entry.key)
        else:
            items.append(entry)
    if status == "standalone":
        status = change_page.DeliveryStatus(root)
    reviews: ReviewDisplay = {}
    if any(entry.item.reviews for entry in items):
        reviews = (review_records if review_records is LinkedReviewRead.AFTER_RENDER
                   else linked_reviews(root, members, review_records, (entry.item for entry in items),
                                       defer_missing=include_counts and defer_counts))
    zone = user_zone()
    today = datetime.fromtimestamp(read_at, zone)
    ordered_items = [(entry, the_list.parking_active(entry.item.parked_until, today)) for entry in items]
    ordered_items.sort(key=lambda pair: pair[1])
    park_deadline = min((deadline.timestamp() for entry, parked in ordered_items
                         if parked and isinstance(deadline := entry.item.parked_until, datetime)), default=None)
    deadline_attr = f' data-park-deadline="{park_deadline}"' if park_deadline is not None else ''
    secret = form_secret(root)
    selection = ListQuery.read(query, items)
    chip_totals: dict[str, Counter[str]] = {facet.name: Counter() for facet in FILTER_FACETS}
    search = normalize_search_text(selection.words.strip(QUERY_WHITESPACE))
    owners: set[the_list.ItemOwner] = set()
    producers: set[the_list.ItemOwner] = set()
    rows: list[str] = []
    linked_row = ''
    deferred_links = False
    shown = 0

    @cache
    def owner_display(identity: the_list.ItemOwner) -> tuple[str, str, str]:
        owner = owner_name(members, identity)
        cell = ('<span class="list-owner-name">' + '<wbr>'.join(
            f'<span>{names.html(identity, part[0])}</span>' for part in re.finditer(r"[^-]*-|[^-]+", owner)) + '</span>')
        return cell, owner.casefold(), normalize_search_text(owner)

    for index, (entry, parked) in enumerate(ordered_items):
        owners.add(entry.item.owner)
        producers.add(entry.item.producer)
        open_question = the_list.has_open_question(entry.item)
        visibility = the_list.question_visibility(entry.keeper, entry.item, status)
        unread_answers = the_list.unread_answer_ids(entry.item)
        open_ask = bool(the_list.answerable_asks(entry.item, members))
        chip = list_chip(entry.item.state, parked=parked, open_question=open_question, open_ask=open_ask,
                         unread_delivery=the_list.unread_delivery(entry.item), unread_answer=bool(unread_answers))
        owner_cell, owner_sort, owner_search = owner_display(entry.item.owner)
        search_text = SearchText(owner_search, normalize_search_text(entry.item.title))
        visible = selection.eligible(entry, search_text, search, parked=parked) and record_table.count_filter_row(
            FILTER_FACETS, {"chip": chip, "owner": entry.item.owner, "producer": entry.item.producer},
            selection.selected, chip_totals, staff_filters=STAFF_FILTERS)
        linked = not visible and selection.reveals(entry, open_question)
        shown += visible
        row_reviews = reviews
        if entry.item.reviews and (
            reviews is LinkedReviewRead.AFTER_RENDER
            or (include_counts and defer_counts and review_records is not LinkedReviewRead.FROM_DISK
                and not has_all_linked_reviews(entry.item, reviews))
        ):
            row_reviews = LinkedReviewRead.AFTER_RENDER
            deferred_links = True
        row = item_rows(entry, names, today, index, secret, row_reviews, owner_cell=owner_cell, owner_sort=owner_sort,
                        search_text=search_text, parked=parked, open_question=open_question, open_ask=open_ask, visibility=visibility,
                        unread_answers=unread_answers, hidden=not (visible or linked))
        if linked:
            linked_row = row
        else:
            rows.append(row)
    shows = [record_table.Chip(chip.value, chip.label, chip_totals["show"][chip])
             for chip in ListChip]
    owner_chips = staff_chips(members, owners, chip_totals["owner"])
    producer_chips = staff_chips(members, producers, chip_totals["producer"])
    owner_selected = f'staff:{selection.selected["owner"]}' if selection.selected["owner"] else "all"
    producer_selected = f'staff:{selection.selected["producer"]}' if selection.selected["producer"] else "all"
    show_selected = selection.selected["show"] or "all"
    check_unread = ' checked' if selection.unread else ''
    words = selection.words
    hide_empty = ' hidden' if shown or not query else ''
    hide_link = '' if linked_row else ' hidden'
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" id="list-sort-{index}" data-sort="{index}">{label}'
                      + '<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    linked_headers = ''.join(f'<th scope="col">{label}</th>' for label in HEADERS)
    columns = ('<colgroup><col class="list-number-column"><col class="list-owner-column"><col style="width:10%">'
               '<col><col style="width:14%"><col class="list-next-step-column"><col style="width:14%">'
               '<col style="width:8%"></colgroup>')
    settings = conf.read("review_links")
    seconds = settings.number("page_refresh_seconds")
    request_timeout = settings.whole("page_request_timeout_seconds")
    delivery_window = record_table.delivery_window_seconds(settings)
    park_retry = settings.number("frame_retry_seconds")
    card_motion = page_frame.CardMotion.read(settings)
    parking = html.escape(json.dumps(record_table.park_settings(settings)), quote=True)
    log_max_height = record_table.log_max_height_rem(settings)
    counts = ''
    if include_counts:
        if defer_counts:
            counts_query = COUNTS_AND_LINKS_QUERY if deferred_links else COUNTS_QUERY
            counts = f'<div data-list-counts-url="{PAGE_PATH}?{html.escape(counts_query)}"></div>'
        else:
            counts = counts_strip(members, names)
    content = (STYLE + page_frame.title("The List", "list")
               + '<p data-review-refresh-error role="status" hidden></p>'
               + f'<div data-list data-refresh-seconds="{seconds}" data-zone="{html.escape(zone.key, quote=True)}"'
               + f' data-request-timeout-seconds="{request_timeout}" data-delivery-window-seconds="{delivery_window}"'
               + f' data-park-retry-seconds="{park_retry}"' + deadline_attr
               + f' {card_motion.attributes()}'
               + f' data-closed-keys="{html.escape(json.dumps(closed_keys), quote=True)}"'
               + f' data-parking="{parking}" style="--list-log-max-height:{log_max_height}rem">'
               + '<div data-record-filters>'
               + record_table.chip_row('Owner', owner_chips, owner_selected, encode_values=True)
               + '<div class="list-producer-filter">'
               + record_table.chip_row('Producer', producer_chips, producer_selected, encode_values=True)
               + record_table.words_box('list-search', 'Filter by number, owner, item or state', value=words)
               + '</div>'
               + '<div class="list-show-filter"><div class="list-show-controls">' + record_table.chip_row('Show', shows, show_selected)
               + f'<label class="list-unread-switch"><input type="checkbox" role="switch" id="list-unread" value="{UNREAD_VALUE}"{check_unread}>'
               + 'Unread only</label></div>'
               + '<p data-hidden-action-status role="status" hidden></p></div>'
               + '</div>'
               + '<p data-missing-item role="status" hidden></p>'
               + '<div data-list-count role="status" aria-live="polite"></div>'
               + '<div class="list-table"><table aria-label="The List">' + columns + '<thead><tr>' + headers + '</tr></thead>'
               + ''.join(rows) + '</table>'
               + f'<p data-empty{hide_empty}>No items in this view.</p></div>'
               + f'<section data-linked-item{hide_link}><h2 id="list-linked-heading">Linked item outside filters</h2>'
               + '<div class="list-table"><table aria-labelledby="list-linked-heading">' + columns
               + '<thead><tr>' + linked_headers + '</tr></thead>' + linked_row + '</table></div></section>'
               + counts + '</div>'
               + page_frame.CONSOLE_DIALOG_ASSETS
               + '<dialog data-list-review-dialog aria-label="Review card">'
               + '<button class="list-review-close" type="button" data-close autofocus>Close</button></dialog>'
               + record_table.render_inline_detail()
               + '<script>' + SCRIPT + '</script>')
    return page_frame.rendered(change_page.document(content, "The List"), started, read_at)


STYLE = record_table.STYLE + record_table.DATE_TIME_STYLE + '<style>' + review_page.QUESTION_COLOR_RULE + '''
[data-list-review-dialog]{position:fixed;inset:0;margin:calc(var(--page-window-top,0px) + 16px) auto auto;
width:min(1060px,calc(100% - 48px));
max-width:none;max-height:none;padding:0;border:1px solid var(--line);border-radius:12px;
background:var(--card);color:var(--ink);overflow:hidden;box-sizing:border-box}
[data-list-review-dialog]::backdrop{background:rgb(0 0 0 / 90%);backdrop-filter:blur(3px)}
.list-review-close{position:absolute;top:24px;right:24px;z-index:1;margin:0;min-height:32px;padding:4px 12px;
font-size:13px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
[data-list-review-dialog] iframe{display:block;width:100%;height:var(--review-card-height,15rem);
max-height:calc(100dvh - 98px - var(--page-window-top,0px));border:0}
:root:has([data-list-review-dialog][open]){overflow:hidden}
:root:has([data-list-review-dialog][open]) [data-list]{opacity:.35}
:root:has([data-list-review-dialog][open][data-fullscreen]){scrollbar-gutter:auto}
[data-list-review-dialog][data-fullscreen]{width:100vw;height:calc(100dvh - var(--page-window-top,0px));border:0;
border-radius:0;margin:var(--page-window-top,0px) 0 0}
[data-list-review-dialog][data-fullscreen] iframe{height:100%;max-height:none}
[data-record-filters] .record-filter-row button{max-width:100%;overflow-wrap:anywhere}
.list-producer-filter{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin-bottom:8px}
.list-producer-filter .record-filter-row{flex:1 1 auto;min-width:0;max-width:100%;margin:0}
.list-producer-filter .record-filter-words{flex:0 1 300px;min-width:0;margin:0 0 0 auto}
.list-show-controls{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;margin-bottom:8px}
.list-show-controls .record-filter-row{min-width:0;margin-bottom:0}
.list-unread-switch{display:inline-flex;align-items:center;gap:7px;min-height:34px;color:var(--ink-2);
font-size:13px;cursor:pointer;margin-inline-start:auto}
.list-unread-switch input{appearance:none;position:relative;width:32px;height:18px;margin:0;border:1px solid var(--ink-3);
border-radius:12px;background:var(--card);cursor:pointer}
.list-unread-switch input::before{content:"";position:absolute;inset:3px auto 3px 3px;aspect-ratio:1;
background:var(--ink-2);border-radius:50%}
.list-unread-switch input:checked{border-color:var(--filter-selected-ink);background:var(--filter-selected-bg)}
.list-unread-switch input:checked::before{inset-inline: auto 3px;background:var(--filter-selected-ink)}
.list-show-filter [data-hidden-action-status]{margin:0 0 8px calc(5rem + 8px)}
@media(max-width:620px){
.list-show-filter{display:flex;flex-direction:column;gap:4px;margin-bottom:8px}
.list-show-controls{display:contents}
.list-show-filter [data-hidden-action-status]{margin-bottom:0}
.list-unread-switch{order:1}
}
@media(max-width:620px) and (max-height:260px){
[data-record-filters] .record-filter-row{grid-template-columns:minmax(0,1fr)}
.list-show-filter [data-hidden-action-status]{margin-inline-start:0}
}
[data-list-count]{margin-bottom:8px;color:var(--ink-2);font-size:13px;text-align:right;overflow-wrap:anywhere}
[data-linked-item]{margin-top:16px}
[data-linked-item] h2{font-size:14px;margin:0 0 8px}
[data-linked-item] th{padding:8px}
.list-number-column{width:6%}
@media(960px < width <= 1100px){.list-number-column{width:62px}}
.list-owner-column{width:var(--list-owner-width,4.5rem)}
.list-next-step-column{width:var(--list-next-step-width,23%)}
.list-owner-name{display:block;overflow-wrap:anywhere;hyphens:none}
.list-table td[data-label="Next step"]>:first-child{margin-top:0}.list-table td[data-label="Next step"]>:last-child{margin-bottom:0}
.list-item-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:16px}
.list-item-fields section{grid-column:span 2}
.list-item-fields :is(.list-field-state,.list-field-eta){grid-column:auto}
.list-actions{position:relative}
:is(.list-tell,.list-actions,.list-parking-actions)>button,.list-park>[data-later-toggle]{
 border:1px solid var(--ink-3);border-radius:8px;background:var(--card);color:var(--ink);cursor:pointer
}
.list-tell>[data-send-status]:empty{display:none}
.list-ask{border:1px solid var(--warning);border-radius:8px;padding:12px 14px;margin:0 0 12px}
.list-ask h3{color:var(--warning);margin:0 0 8px}
.list-ask dl{display:grid;grid-template-columns:7.5rem minmax(0,1fr);gap:4px 12px;margin:0 0 10px}
.list-ask dt{color:var(--ink-3)}.list-ask dd{margin:0;overflow-wrap:anywhere}
.list-ask form{display:flex;align-items:center;flex-wrap:wrap;gap:6px 12px}
.list-ask button{min-height:32px;padding:6px 18px;border:0;border-radius:6px;background:var(--warning);color:#1b1b1b;
font-weight:600;cursor:pointer}
.list-ask button:disabled{opacity:.45;cursor:default}
.list-ask small{color:var(--ink-3)}
.list-parking-actions{display:flex;gap:inherit;margin-inline-start:auto}
.list-table [data-parked]:not([data-parked="false"]) [data-list-check]{display:none}
.list-table .list-row.open [data-parked-note]{display:none}
.list-table:has([data-later-menu]:not([hidden]),.list-eta:focus-visible),
.list-table tbody:has([data-later-menu]:not([hidden]),.list-eta:focus-visible){overflow:visible}
.list-tell{display:flex;justify-content:flex-end;align-items:center;gap:10px;flex-wrap:wrap}
.list-tell [value="tell"]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.list-tell button:disabled{opacity:.45;cursor:default}.list-tell [data-send-status]{margin-left:0}
.list-log dl{display:grid;grid-template-columns:min(7rem,42%) minmax(0,1fr);gap:2px 12px;margin:4px 0 0}
.list-log dt{color:var(--ink-3)}.list-log dd{margin:0;overflow-wrap:anywhere}
.list-log dd>:last-child{margin-bottom:0}
.list-log dl li{display:list-item;padding:0;border:0}
.list-log>li.question{border-inline-start:3px solid var(--type-color);padding-inline-start:6px}
.list-log .log-evidence{margin:0}
.list-log .log-evidence summary{min-height:24px}
.list-log .log-evidence div{white-space:pre-wrap}
.page-question p{white-space:pre-wrap;overflow-wrap:anywhere}
.page-question time{color:var(--ink-3);font-size:13px}
.list-table small[data-new-answer]{color:var(--accent)}
.page-question .question-unread{margin-inline-start:6px;color:var(--accent);text-transform:none}
.list-panel:has([data-waiting-note]){min-height:12rem}
.list-panel [data-waiting-note]{margin-top:16px}
.list-table td[data-label="State"]{color:var(--ink)}
.list-table td[data-label="Producer"],.list-table td[data-label="ETA"]{color:var(--ink-2)}
.list-eta{display:block;position:relative;min-width:0}
.list-eta-text{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.list-eta:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.list-eta-full{display:none;position:absolute;inset-block-start:calc(100% + 4px);inset-inline-end:0;z-index:5;
width:max-content;max-width:min(28rem,calc(100vw - 4rem));padding:6px 8px;background:var(--card);
border:1px solid var(--line);border-radius:6px;overflow-wrap:anywhere}
.list-eta:focus-visible .list-eta-full{display:block}
@media(max-width:960px){
.list-table .list-row .record-id{width:auto}
.list-table .list-row td[data-label="Producer"]{font-size:inherit}.list-table .list-row td[data-label="Item"]{font-size:17px}
.list-table .list-row td[data-label="State"]::before{content:none}
.list-table [data-keeper-produces=true] .list-row td[data-label="Producer"]{display:none}
.list-table .list-row td[data-label="ETA"]{display:flex;gap:4px}
.list-eta-text{white-space:normal;overflow:visible}
.list-table .list-row td[data-label="Producer"]::before,.list-table .list-row td[data-label="ETA"]::before{
content:attr(data-label) ': ';color:var(--ink-3)}
}
</style>'''

# Use the Python case-folding table for browser search.
SCRIPT = (page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + page_frame.MOTION_SCRIPT + record_table.SCRIPT
          + review_legacy.SHOW_ALIAS_SCRIPT
          + record_table.DATE_TIME_SCRIPT + '''(() => {
''' + question_path.queue_script() + '''
const queuesByState=''' + json.dumps(QUEUE_BY_STATE) + ''';
const chipsByQueue=''' + json.dumps(CHIP_BY_QUEUE) + ''';
const newAnswerMarkup=''' + json.dumps(question_path.NEW_ANSWER_HTML) + ''';
const shows=''' + json.dumps(list(ListChip)) + r''';
const showAliases=''' + json.dumps(SHOW_ALIASES) + r''';
const [unreadField,unreadValue,unreadMatch]=''' + json.dumps(UNREAD_FILTER) + r''';
const filterFacets=''' + json.dumps(FILTER_FACETS) + r''';
const staffFilters=''' + json.dumps(STAFF_FILTERS) + r''';
const linkRevealFields=''' + json.dumps(LINK_REVEAL_FIELDS) + r''';
const parkedSearchLabel=''' + json.dumps(PARKED_SEARCH_LABEL) + r''';
const stateSearchText=''' + json.dumps(STATE_SEARCH_TEXT) + r''';
const [foldRanges,searchCaseFold]=''' + json.dumps(
    (search_fold.RANGES, search_fold.EXCEPTIONS), ensure_ascii=False, separators=(",", ":")) + r''';
for(const [start,end,step,offset] of foldRanges)
 for(let point=start;point<=end;point+=step) searchCaseFold[point]=String.fromCodePoint(point+offset);
const normalizeWords=words=>words.trim().replace(/\r\n?/g,'\n').replace(/\0/g,'\ufffd')
 .replace(/[A-Z\u0080-\u{10ffff}]/gu,character=>searchCaseFold[character.codePointAt(0)] ?? character);
const searchFields=Object.entries(''' + json.dumps(SEARCH_FIELDS) + r''').map(([field,pattern])=>
 [field,pattern && new RegExp(pattern)]);
const deliveryParking=''' + json.dumps(the_list.ParkUntilDelivered.DELIVERED) + r''';
const isParked=data=>data.parked==='true' || data.parked===deliveryParking;
const chipFor=data=>{
 if(data.state==='Closed') return;
 const parked=isParked(data);
 return chipsByQueue[questionQueue(queuesByState[data.state],data.openAsk==='true' || data.unread==='true' && data.unreadAnswer==='true',
  parked,data.unreadDelivery==='true',data.openQuestion==='true')];
};
let content=document.querySelector('[data-list]');
function decodeListKeys(list){
 for(const row of list.querySelectorAll('[data-list-item][data-key*="%"]'))
  row.dataset.key=decodeURIComponent(row.dataset.key);
}
decodeListKeys(content);
let closedKeys=new Set(JSON.parse(content.dataset.closedKeys));
let saving=0,listSaveVersion=0,refreshNeeded=false;
function listRequestSignal(){
 return AbortSignal.timeout(Number(content.dataset.requestTimeoutSeconds)*1000);
}
function startListSave(){
 saving++;listSaveVersion++;pageRefresh.abandon();
}
function refreshAfterListSave(needed){
 refreshNeeded ||= needed;
 if(saving) return;
 if(savedReviews.size) void refreshSavedReview();
 else if(refreshNeeded || parkExpiryOwed) void pageRefresh.refresh(true);
}
let parkExpiryTimer,parkExpiryOwed=false;
const parkExpiryObserver=new MutationObserver(refreshAfterParkExpiry);
function parkPickerBlocksRefresh(){
 const open=dateTimePickerOpen(content);
 if(open && parkExpiryOwed)
  parkExpiryObserver.observe(content,{subtree:true,childList:true,attributes:true,attributeFilter:['hidden']});
 return open;
}
function refreshAfterParkExpiry(){
 if(parkPickerBlocksRefresh()) return;
 parkExpiryObserver.disconnect();
 refreshAfterListSave(false);
}
function scheduleParkExpiry({readAt},requestStarted){
 clearTimeout(parkExpiryTimer);
 parkExpiryObserver.disconnect();
 parkExpiryOwed=false;
 if(!content.dataset.parkDeadline) return;
 const expiresAt=requestStarted+(Number(content.dataset.parkDeadline)-readAt)*1000;
 const schedule=()=>{
  const delay=Math.ceil(expiresAt-performance.now());
  // The browser cannot schedule a timeout longer than a signed 32-bit integer.
  parkExpiryTimer=setTimeout(check,Math.max(0,Math.min(delay,2147483647)));
 };
 const check=()=>{
  if(performance.now()<expiresAt){schedule();return;}
  parkExpiryOwed=true;
  refreshAfterParkExpiry();
 };
 schedule();
}
const drafts=new Map();
const unparkMessages={
 cleared:'Unparked',
 cleared_newer_parking_active:'The original Unpark cleared parking. A newer parking time is active',
 expired:'Parking time already expired',
 no_active_parking:'No active parking time to clear',
};
function restoreEvidenceDetails(fresh){
 const opened=new Map();
 for(const details of content.querySelectorAll('.log-evidence[open]')){
  const key=recordFromDetail(details).dataset.key;
  if(!opened.has(key)) opened.set(key,new Set());
  opened.get(key).add(details.closest('[data-log-index]').dataset.logIndex);
 }
 if(!opened.size) return;
 for(const row of fresh.querySelectorAll('[data-list-item]')){
  const entries=opened.get(row.dataset.key);
  if(!entries) continue;
  for(const details of row.querySelectorAll('.log-evidence'))
   details.open=entries.has(details.closest('[data-log-index]').dataset.logIndex);
 }
}
function matchesWords(data,search){
 return searchFields.some(([field,pattern])=>{
  const text=field==='parkingLabel' ? (isParked(data) ? parkedSearchLabel : '') :
   field==='state' ? stateSearchText[data.state] : data[field] || '';
  return text.includes(pattern ? search.replace(pattern,'') : search);
 });
}
function syncUnread(item,form,unread,unreadSort){
 item.dataset.unread=String(unread);
 const sortValues=JSON.parse(item.dataset.sortValues);
 sortValues[7]=unread ? unreadSort : 0;
 item.dataset.sortValues=JSON.stringify(sortValues);
 const update=item.querySelector('[data-label="Update"]'),dot=update.querySelector('.list-dot');
 const read=form?.querySelector('[value="read"]');
 const answerMark=update.querySelector('[data-new-answer]');
 if(unread && item.dataset.unreadAnswer==='true'){
  if(!answerMark) update.insertAdjacentHTML('beforeend',newAnswerMarkup);
 }else answerMark?.remove();
 if(unread){
  update.querySelector('[aria-label="No unread update"]')?.remove();
  if(!dot){const mark=document.createElement('span');mark.className='list-dot';mark.setAttribute('role','img');
   mark.setAttribute('aria-label','Unread update');update.prepend(mark);}
  if(!form) return;
  const canRead=!form.hasAttribute('data-waiting-actions') ||
   item.dataset.unreadDelivery==='true' || item.dataset.unreadAnswer==='true';
  if(!canRead) read?.remove();
  if(canRead && !read){
   const button=document.createElement('button');button.name='action';button.value='read';
   button.textContent='Mark read';button.disabled=Boolean(form.dataset.sending);
   const remove=form.querySelector('[value="remove"]');
   if(remove) remove.after(button);
   else form.querySelector('.list-actions').prepend(button);
  }
 }else{
  dot?.remove();read?.remove();
  if(!update.querySelector('[aria-label="No unread update"]')){const empty=document.createElement('span');
   empty.setAttribute('aria-label','No unread update');empty.textContent='—';update.prepend(empty);}
 }
}
function installListDetail(item){
  const detail=recordDetails(item),form=detail.querySelector('[data-feedback-form]');
  if(form && !form.retainDraft) bindFeedbackForm(form);
  tableView.showDraftCopy(drafts.get(item.dataset.key)?.words,form?.elements.words?.type==='textarea');
}
function renderListDetail(revision,text,{retainLinks=false}={}){
 return (row,detail)=>{
  const links=retainLinks ? detail.querySelector('[data-list-review-links]') : null;
  row.dataset.detailRevision=revision;
  detail.innerHTML=text;
  if(links) detail.querySelector('[data-list-review-links]')?.replaceWith(links);
  installListDetail(row);
 };
}
function installSavedListDetail(request,revision,text,options){
 tableView.installOwnItemResult(request,revision,renderListDetail(revision,text,options));
}
function presentListDetail(item,result){
 const revision=result.detail_revision,text=result.detail_html;
 if(revision && typeof text==='string')
  tableView.presentItemRevision(item,revision,renderListDetail(revision,text,{retainLinks:true}));
}
const tableView=createRecordTable({content:()=>content,
 shows,
 defaultShow:'all',
 normalizeWords,
 expandFilters:params=>{
  const show=params.get('show') || '';
  if(show===unreadField) params.set(unreadField,unreadValue);
  else if(params.has(unreadField) && params.get(unreadField)!==unreadValue) params.set(unreadField,'');
  expandStateAlias(params,(show,state)=>{
   show=show || '';
   const aliases=Object.hasOwn(showAliases,show) ? showAliases[show] : showAliases['*'];
   const alias=Object.hasOwn(aliases,state) ? state : '';
   return Object.hasOwn(aliases,alias) ? aliases[alias] : show;
  });
  const [owner,showrunner]=ownerQueryKeys;
  if(params.has(showrunner)){
   params.set(owner,params.get(owner) || params.get(showrunner));
   params.delete(showrunner);
  }
 },
 filters:['producer',unreadField],
 staffChipFilters:staffFilters,
 emptyFilters:(root,{filters})=>[
  ...activeChipLabels(root,'[data-filter-row] [aria-pressed="true"]:not([data-filter-value="all"])'),
  ...(filters[unreadField] ? ['Unread only'] : []),
 ],
 firstFilter:root=>root.querySelector('#filter-owner-all'),
 closeHiddenItem:changingFilters=>changingFilters || !saving,
 ownsFocus:listOwnsFocus,
 keepItemOnMissingLink:true,
 rememberItem:item=>{
  recordDetails(item).querySelector('[data-feedback-form]')?.retainDraft?.();
 },
 itemRevision:item=>item.dataset.detailRevision,
 retainedDetailSlot:'[data-list-review-links]',
 detailInstalled:installListDetail,
 hasKey:key=>!closedKeys.has(key) && !!rowIn(content,key),
 revealLinked:(data,{filters})=>linkRevealFields.some(([field,value,filter])=>
  (!filter || filters[filter]) && (data[field] || 'false')===value),
 missingItem:key=>{
  const number=key.match(/\/L([1-9]\d*)$/)?.[1];
  if(!number) return '';
  return 'Item #'+number+(closedKeys.has(key) ? ' is closed' : ' is not on the List');
 },
 eligibleChip:(data,search,{filters})=>{
  if((filters[unreadField] && data[unreadField]!==unreadMatch) || (search && !matchesWords(data,search))) return;
  return chipFor(data);
 },
 filterFacets,
 counts:(items,count,{counts})=>{
  const open=items.filter(item=>item.dataset.state!=='Closed');
  return count+' of '+open.length+' items shown · '+(counts.show['needs-me'] ?? 0)+' need you · '+
   open.filter(item=>item.dataset.unread==='true').length+' unread';
 },
});
function listOwnsFocus(){
 if(parent===window || document.hasFocus()) return true;
 const frame=window.frameElement;
 return frame!==null && frame.ownerDocument.activeElement===frame;
}
function focusAfterQuestion(item){
 const rows=[...content.querySelectorAll('[data-list-item]')],index=rows.indexOf(item);
 const candidates=[...rows.slice(index+1),...rows.slice(0,index).reverse()];
 for(const row of candidates){
  const toggle=row.querySelector('.list-toggle');
  if(toggle?.getClientRects().length){
   toggle.focus();
   if(document.activeElement===toggle) return;
  }
 }
 content.querySelector('#list-search').focus();
}
let ownerNameWidth=0;
function sizeListColumns(){
 const table=content.querySelector('table');
 const numberWidth=table.querySelector('.list-number-column').getBoundingClientRect().width;
 if(!numberWidth) return;
 const tableWidth=table.getBoundingClientRect().width;
 const headingWidth=4.5*parseFloat(getComputedStyle(document.documentElement).fontSize);
 const ownerWidth=Math.min(Math.max(headingWidth,ownerNameWidth+16),tableWidth*.24);
 // Producer, State, ETA, and Update use 46% of the table.
 const nextStepWidth=(tableWidth*.54-numberWidth-ownerWidth)*23/40;
 content.style.setProperty('--list-owner-width',ownerWidth+'px');
 content.style.setProperty('--list-next-step-width',nextStepWidth+'px');
}
addEventListener('resize',sizeListColumns);
function bindListControls(){
 const ownerNames=content.querySelectorAll('.list-owner-name');
 ownerNameWidth=0;
 if(ownerNames.length){
  const measure=document.createElement('canvas').getContext('2d');
  if(measure){
   measure.font=getComputedStyle(ownerNames[0]).font;
   const names=new Set(Array.from(ownerNames,name=>name.textContent));
   for(const name of names) ownerNameWidth=Math.max(ownerNameWidth,measure.measureText(name).width);
   ownerNameWidth=Math.ceil(ownerNameWidth);
  }
 }
 sizeListColumns();
}
function listPickerSummary(zone){
 const format=new Intl.DateTimeFormat('en-US',{timeZone:zone,month:'short',day:'numeric',
  hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'short'});
 return at=>{
  const parts=Object.fromEntries(format.formatToParts(at).map(part=>[part.type,part.value]));
  return parts.month+' '+parts.day+' '+parts.hour+':'+parts.minute+' '+parts.timeZoneName;
 };
}
function bindFeedbackForm(form){
  let item=recordFromDetail(form);
  const key=item.dataset.key;
  const notice=form.querySelector('[data-send-status]');
  const unpark=form.querySelector('[value="unpark"]'),parkedDetail=form.querySelector('[data-parked-detail]');
  showActionFailure(notice,tableView.actionNotice(item));
  const waitingForm=form.hasAttribute('data-waiting-actions');
  const confirmation=()=>({state:item.dataset.state,parked:item.dataset.parked,
   unread:item.dataset.unread==='true',unreadDelivery:item.dataset.unreadDelivery,unreadAnswer:item.dataset.unreadAnswer,
   unreadSort:JSON.parse(item.dataset.sortValues)[7]});
  let confirmed=confirmation();
  const syncConfirmed=(metadataOnly=false)=>{
   item.dataset.state=confirmed.state;
   item.dataset.parked=confirmed.parked;
   item.dataset.unreadDelivery=confirmed.unreadDelivery;
   item.dataset.unreadAnswer=confirmed.unreadAnswer;
   if(!metadataOnly) unpark.hidden=!isParked(confirmed) || confirmed.state==='Closed';
   syncUnread(item,metadataOnly ? null : form,confirmed.unread,confirmed.unreadSort);
  };
  const applySavedState=(result,{keepDetailFocus=false,hasDetail=false,metadataOnly=false}={})=>{
   delete item.dataset.listReviewVersion;
   if(typeof result.review_summary_html==='string'){
    const summary=item.querySelector('[data-list-review-summary]');
    if(summary) summary.innerHTML=result.review_summary_html;
   }
   if(typeof result.item_state==='string') confirmed.state=result.item_state;
   if(confirmed.state==='Closed'){
    closedKeys.add(key);
    if(!metadataOnly) tableView.closeCard(key);
   }
   const unread=result.unread==='true';
   if(['true','false'].includes(result.parked))
    confirmed.parked=result.parked_until===deliveryParking ? deliveryParking : result.parked;
   if(['true','false'].includes(result.unread_delivery)) confirmed.unreadDelivery=result.unread_delivery;
   if(['true','false'].includes(result.unread_answer)) confirmed.unreadAnswer=result.unread_answer;
   if(['true','false'].includes(result.unread)){
    confirmed.unread=unread;
    confirmed.unreadSort=unread ? (result.unread_sort ?? confirmed.unreadSort) : 0;
   }
   syncConfirmed(metadataOnly);
   const state=item.querySelector('.list-state');state.textContent=item.dataset.state;
   state.className='list-state '+({'Waiting for nate':'warning',Blocked:'error',Working:'accent',Delivered:'room',Done:'room'}[item.dataset.state] || 'muted');
   const sortValues=JSON.parse(item.dataset.sortValues);sortValues[4]=item.dataset.state.toLowerCase();item.dataset.sortValues=JSON.stringify(sortValues);
   if(typeof result.parked_until==='string'){
    let note=item.querySelector('[data-parked-note]');
    const text=result.parked_until ? 'Parked until '+result.parked_until : '';
    if(!metadataOnly){
     parkedDetail.hidden=!result.parked_until;
     parkedDetail.textContent=text;
    }
    if(result.parked_until){
     if(!note){note=document.createElement('small');note.dataset.parkedNote='';state.parentElement.append(note);}
     note.textContent=text;
    }else note?.remove();
   }
   if(['true','false'].includes(result.open_question)) item.dataset.openQuestion=result.open_question;
   if(['true','false'].includes(result.open_ask)) item.dataset.openAsk=result.open_ask;
   const questionNote=item.querySelector('[data-open-question-note]');
   if(item.dataset.openQuestion==='true'){
    if(!questionNote){
     const note=document.createElement('small');
     note.dataset.openQuestionNote='';
     note.textContent=''' + json.dumps(ListChip.PRODUCER_ANSWERING.label) + r''';
     state.after(note);
    }
   }else questionNote?.remove();
   if(typeof result.check_html==='string'){
    const check=item.querySelector('[data-list-check]');
    if(check) check.outerHTML=result.check_html;
    else if(result.check_html)
     (item.querySelector('[data-open-question-note]') ?? state).insertAdjacentHTML('afterend',result.check_html);
   }
   const waiting=result.waiting_for_answer==='true';
   if(typeof result.waiting_for_answer==='string') item.dataset.waitingForAnswer=result.waiting_for_answer;
   if(waiting && !hasDetail && !metadataOnly && confirmed.state!=='Closed'){
    const detail=recordDetails(item);
    if(typeof result.waiting_details_html==='string'){
     const field=keepDetailFocus && listOwnsFocus() && detail.contains(document.activeElement)
      ? document.activeElement : null;
     const reviewLink=field?.matches('[data-list-review-card]') ? reviewLinkPlace(field) : null;
     tableView.updateItemDetail(item,detail=>{detail.innerHTML=result.waiting_details_html;});
     if(reviewLink) restoreReviewLink(reviewLink);
     else if(field?.id && field!==unpark) document.getElementById(field.id)?.focus({preventScroll:true});
     return detail.querySelector('[data-feedback-form]');
    }
    const note=detail.querySelector('[data-waiting-note]');
    note.hidden=false;
    detail.querySelector('[data-list-item-content]').replaceChildren(note);
   }
  };
  const words=form.elements.words,until=form.elements.until;
  const tell=form.querySelector('[value="tell"]'),ask=form.querySelector('[value="question"]');
  const syncTell=()=>{
   if(!ask || !tell) return;
   const name=matching('tell')?.recipientName ?? signed.recipientName;
   const wordsLabel='Your words for '+name+' about this item';
   tell.textContent='Tell '+name;
   words.placeholder=wordsLabel;
   words.setAttribute('aria-label',wordsLabel);
   ask.disabled=tell.disabled=Boolean(form.dataset.sending) || !words.value.trim();
  };
  // Keep the signed update and recipient from the rendered form.
  const signed={request:form.elements.request.value,token:form.elements.token.value,recipientName:form.dataset.recipientName};
  let fresh=signed;
  const newRequest=()=>({
   ...signed,
   request:signed.request+'.'+Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join(''),
  });
  const draft=drafts.get(key);if(draft && !waitingForm){words.value=draft.words;until.value=draft.until;}
  let draftStamp=draft?.stamp ?? 0;
  const unconfirmed=draft?.unconfirmed ?? new Set();
  const matching=action=>[...unconfirmed].findLast(attempt=>
   attempt.words===words.value && attempt.until===until.value && (action===undefined || attempt.action===action));
  const identity=request=>{form.elements.request.value=request.request;form.elements.token.value=request.token;};
  const zone=content.dataset.zone;
  const parkSettings=()=>JSON.parse(content.dataset.parking);
  const parkToggle=form.querySelector('[data-later-toggle]');
  const parkSummary=waitingForm ? null : listPickerSummary(zone);
  const retainDraft=()=>{
   drafts.set(key,{words:waitingForm ? draft?.words ?? '' : words.value,
    until:waitingForm ? draft?.until ?? '' : until.value,stamp:draftStamp,unconfirmed});
   identity(matching() ?? fresh);
  };
  const retain=()=>{retainDraft();syncTell();};
  form.retainDraft=retainDraft;
  form.applySavedState=applySavedState;
  retain();
  const edit=()=>{draftStamp++;retain();};
  words.addEventListener('input',edit);until.addEventListener('input',edit);
  if(!waitingForm) bindDateTimePicker(form,{zone,settings:parkSettings,summaryPrefix:'Parks until ',
   formatSummary:parkSummary,retryTime:()=>Date.parse(matching('park')?.parkingTime ?? '')});
  form.addEventListener('submit',async event=>{
   event.preventDefault();
   const formKey=form.elements.keeper.value+'/'+form.elements.id.value;
   const current=recordFromDetail(form);
   if(!tableView.canSubmit(form,formKey) || current!==tableView.openItem || !content.contains(current) ||
    cardSubmitBlocked(form,tableView.rowFading(key))) return;
   if(item!==current){item=current;confirmed=confirmation();}
   const submitter=event.submitter,action=submitter?.value || 'tell';
   const parkForDelivery=action==='park-until-delivered',parks=action==='park' || parkForDelivery || Boolean(submitter?.dataset.laterPreset);
   const closeSavedCard=()=>tableView.closeCard(key,{focusShow:parkForDelivery});
   const changesParking=parks || action==='unpark';
   const focusUnpark=action==='unpark' && document.activeElement===unpark;
   const usesWords=action==='tell' || action==='question';
   const asksQuestion=action==='question';
   if(usesWords && !words.value.trim()) return;
   const retry=submitter?.dataset.laterPreset ? undefined : matching(parks ? 'park' : action);
   const parkingTime=parks ? retry?.parkingTime ?? parkRequestTime(form,submitter,zone,parkSettings(),until.value) : '';
   if(parks && !parkingTime){showActionFailure(notice,'Choose a future parking time.');return;}
   if(!waitingForm && action==='unpark') closeDateTimePicker(form.querySelector('[data-later-menu]'),false);
   const attempt=retry ?? {...fresh,action:parks ? 'park' : action,words:words.value,until:until.value,parkingTime};
   identity(attempt);
   const data=new URLSearchParams(new FormData(form));data.set('action',parks ? 'park' : action);
   if(parks){data.set('until',parkingTime);data.set('words','');}
   if(parks && retry) closeDateTimePicker(form.querySelector('[data-later-menu]'),false);
   if(!retry){
    if(!submitter?.dataset.laterPreset) unconfirmed.add(attempt);
    fresh=newRequest();
   }
   retainDraft();
   const stampAtSubmit=draftStamp;
   const request=tableView.startItemRequest(key);
   item.listRequest=request;
   startListSave();
   const hadFocus=asksQuestion && recordDetails(item).contains(document.activeElement);
   let focusChoice;
   const trackFocus=event=>{if(event.target!==document.body) focusChoice=event.target;};
   if(hadFocus) document.addEventListener('focusin',trackFocus);
   let saved=false,refused=false,retried=false,stale=false,focusParkedAfterClose=false,nextForm;
   const clearsUnread=action==='read' || usesWords,unreadBefore=item.dataset.unread,wasVisible=!item.hidden;
   const releasesLink=action==='read' || parks || action==='remove';
   let restoreLink;
   const submission=tableView.preserveRecordRowTop(()=>{
    setCardSubmitting(form,true,recordDetails(item));
    showActionFailure(notice);
    if(action==='remove'){
     item.dataset.state='Closed';
     unpark.hidden=true;
     syncUnread(item,form,false,0);
    }
    if(parks) item.dataset.parked=parkForDelivery ? deliveryParking : 'true';
    if(clearsUnread) item.dataset.unread='false';
    if(releasesLink) restoreLink=tableView.releaseLink(key);
    return tableView.startAction(item);
   });
   const hidUnread=clearsUnread && wasVisible && item.hidden;
   const showUnparkResult=result=>{
    const unparked=result.unpark_outcome==='cleared';
    const message=unparkMessages[result.unpark_outcome] ?? 'Unpark result unavailable';
    const label='Item #'+item.dataset.number;
    const hiddenMessage=unparked ? label+' unparked.' : label+': '+message+'.';
    tableView.showActionResult(item,submission,label,{kind:'success',message,hiddenMessage});
   };
   const post=async()=>{
    const options={method:'POST',body:data,signal:parks ? listRequestSignal() : undefined};
    const receiver=action==='tell' ? attempt.recipientName : current.dataset.sendReceiver;
    const response=await (usesWords ? heldFetch(location.pathname,options,receiver)
                                   : fetch(location.pathname,options));
    refused=!response.ok && (response.status<500 || (!asksQuestion && !parks && action!=='remove'));
    if(!parks) saved=response.ok;
    if(saved && tableView.currentItemRequest(request) && usesWords &&
       draftStamp===stampAtSubmit && words.value===attempt.words) words.value='';
    const result=await response.json();
    stale=!tableView.currentItemRequest(request);
    if(item.listRequest!==request) return;
    if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
    return result;
   };
   try{
    let result;
    try{result=await post();}
    catch(error){
     if(!parks || refused) throw error;
     await new Promise(resolve=>setTimeout(resolve,Number(content.dataset.parkRetrySeconds)*1000));
     retried=true;
     result=await post();
    }
    if(item.listRequest!==request) return;
    saved=true;
    if(stale){
     if(tableView.openItem?.dataset.key===key) return;
     tableView.preserveRecordRowTop(()=>{
      applySavedState(result,{metadataOnly:true});
      if(action==='unpark') showUnparkResult(result);
      else tableView.applyAction();
     });
     return;
    }
    const closes=parks || action==='remove' || result.detail_closed;
    if(closes){
     tableView.preserveRecordRowTop(()=>{
      nextForm=applySavedState(result,{keepDetailFocus:changesParking,hasDetail:true});
      closeSavedCard();
      tableView.applyAction();
      if(result.detail_unavailable)
       tableView.showActionResult(item,submission,'Item '+item.dataset.number,
        {kind:'failure',message:'Saved; the item could not refresh.'});
     });
     return;
    }
    tableView.startDeliveryWindow(request,result);
    let revision=result.detail_revision,text=result.detail_html ?? result.waiting_details_html;
    if(typeof text!=='string' && ('delivery_pending' in result || revision || result.detail_unavailable)){
     const response=await fetch(location.pathname,{cache:'no-store',signal:listRequestSignal()});
     if(!response.ok) throw new Error('The saved item body is unavailable.');
     const page=new DOMParser().parseFromString(await response.text(),'text/html');
     const updated=page.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
     if(!updated) throw new Error('The saved item is unavailable.');
     revision=updated.dataset.detailRevision;
     text=recordDetails(updated).innerHTML;
    }
    stale=!tableView.currentItemRequest(request);
    if(stale) return;
    const hasDetail=typeof text==='string' && Boolean(revision);
    tableView.preserveRecordRowTop(()=>{
     nextForm=applySavedState(result,{keepDetailFocus:changesParking,hasDetail});
     if(hasDetail){
      unconfirmed.delete(attempt);
      retainDraft();
      installSavedListDetail(request,revision,text);
     }
     if(action==='unpark'){
      if(result.unpark_outcome==='cleared') until.value='';
      showUnparkResult(result);
     }else if(action==='tell'){
      const message='sent to '+attempt.recipientName+(result.unread==='true' ? '; update remains unread' : '');
      tableView.showActionResult(item,submission,'Item #'+item.dataset.number,{kind:'success',message});
     }else tableView.applyAction();
     if(wasVisible && item.hidden && tableView.opened.has(key)){
      const focus=document.activeElement;
      const inside=recordDetails(item).contains(focus);
      focusParkedAfterClose=focusUnpark && (inside || focus===document.body);
      tableView.closeCard(key);tableView.applyAction();
      if(hadFocus && listOwnsFocus() && (inside || focus===document.body)) focusAfterQuestion(item);
     }
    });
   }catch(error){
    stale=!tableView.currentItemRequest(request);
    if(item.listRequest!==request) return;
    // An undone send saved nothing, so the item returns to its state before Send and keeps the words.
    const undone=error instanceof SendUndone;
    if(undone) refused=true;
    if(saved){
     if(stale) return;
     tableView.preserveRecordRowTop(()=>{
      if(parks || action==='remove'){
       if(action==='remove') closedKeys.add(key);
       closeSavedCard();
       tableView.showActionResult(item,submission,'Item '+item.dataset.number,
        {kind:'failure',message:'Saved; the item could not refresh.'});
      }else tableView.savedItemError(request);
     });
     return;
    }
    const wasHidden=item.hidden;
    let current;
    if(retried || (!refused && (parks || action==='remove'))){
     try{
      const query=new URLSearchParams({'item-state':data.get('id'),keeper:data.get('keeper')});
      if(parks) query.set('park-request',attempt.request);
      const response=await fetch(location.pathname+'?'+query,{cache:'no-store',signal:listRequestSignal()});
      if(!response.ok) throw new Error('HTTP '+response.status);
      current=await response.json();
     }catch(_){}
     stale=!tableView.currentItemRequest(request);
     if(item.listRequest!==request) return;
     // A failed read back after a retry does not show that the server refused the park.
     if(retried && !current) refused=false;
    }
    tableView.preserveRecordRowTop(()=>{
     let restore=refused || releasesLink,confirmedByReadback=false;
     if(current){
      applySavedState(current,{metadataOnly:true});
      restore=confirmed.state!=='Closed' && (!parks || current.park_request_saved===false);
      confirmedByReadback=confirmed.state==='Closed' || current.park_request_saved===true;
      if(confirmedByReadback){
       saved=true;
       if(!stale) closeSavedCard();
       tableView.applyAction();
      }else if(!stale) presentListDetail(item,current);
     }else if(refused) syncConfirmed(stale);
     else if(parks || action==='remove'){
      syncConfirmed(stale);
      restore=confirmed.state!=='Closed';
     }else if(clearsUnread) item.dataset.unread=unreadBefore;
     if(restore) restoreLink?.();
     if(!confirmedByReadback){
      if(!stale && (refused || hidUnread) && !tableView.opened.size) tableView.openCard(key);
      const message=undone ? error.message : (refused ? 'Not saved: ' : 'Save not confirmed: ')+error.message;
      tableView.showActionResult(item,submission,'Item '+item.dataset.number,{kind:undone ? 'undone' : 'failure',message});
     }
     // The return animation uses the confirmed sort keys.
     if(!saved && refused && wasHidden && !item.hidden) tableView.applyAction();
    });
   }
   finally{
    if(saved || refused) unconfirmed.delete(attempt);
    tableView.preserveRecordRowTop(()=>{
     setCardSubmitting(form,false,stale ? form : recordDetails(item));syncTell();
     if(!stale) retainDraft();
     if(nextForm && !stale){
      bindFeedbackForm(nextForm);
      if(tableView.currentItemRequest(request))
       tableView.showDraftCopy(drafts.get(key)?.words,nextForm.elements.words?.type==='textarea');
     }
    });
    if(!stale && focusUnpark && document.hasFocus() &&
       (focusParkedAfterClose || document.activeElement===document.body || document.activeElement===unpark)){
     const currentUnpark=recordDetails(item).querySelector('[value="unpark"]:not([hidden])');
     const target=saved && item.hidden ? content.querySelector('[data-filter-row="show"] [aria-pressed="true"]')
      : currentUnpark ?? item.querySelector('.list-toggle');
     if(target?.getClientRects().length){
      target.focus({preventScroll:true});
      if(saved && item.hidden){
       const status=content.querySelector('[data-hidden-action-status]');
       const top=target.getBoundingClientRect().top;
       const inset=(typeof stuckPageActionsBottom==='function' ? stuckPageActionsBottom()
        : document.querySelector('[data-page-status]').offsetHeight)+12;
       if(top<inset || status.getBoundingClientRect().bottom>innerHeight)
        scrollBy({top:Math.floor(top-inset),behavior:'instant'});
      }
     }
    }
    if(!stale && !saved && parks){
     await tableView.settled();
     if(listOwnsFocus() && document.activeElement===document.body && parkToggle.getClientRects().length && !parkToggle.disabled){
      parkToggle.focus({preventScroll:true});
     }
    }
    if(!stale && hadFocus && !item.hidden && document.activeElement===document.body)
     await tableView.rowSettled(item);
    saving--;
    if(hadFocus){
     document.removeEventListener('focusin',trackFocus);
     if(!stale && listOwnsFocus()){
      if(item.hidden && (!focusChoice || recordDetails(item).contains(focusChoice))) focusAfterQuestion(item);
      else if(!focusChoice?.isConnected && !item.hidden && document.activeElement===document.body){
       const target=!saved && submitter?.matches(':enabled') ? submitter : item.querySelector('.list-toggle');
       target.focus({preventScroll:true});
      }
     }
    }
    refreshAfterListSave(stale || Boolean(tableView.deliveryRequest()) ||
      (saved && (changesParking || action==='remove' || item.dataset.waitingForAnswer!=='true')) ||
      (!saved && !refused && (parks || action==='remove')));
   }
  });
}
// A Go form sends nate's Go or Approved for one ask; the server closes the ask.
document.addEventListener('submit',async event=>{
 const form=event.target instanceof Element ? event.target.closest('form[data-go-form]') : null;
 if(!form || !content.contains(form)) return;
 event.preventDefault();
 const item=recordFromDetail(form),box=form.closest('[data-ask]'),button=form.querySelector('button');
 if(button.disabled) return;
 const key=item.dataset.key,hadFocus=box.contains(document.activeElement);
 const data=new URLSearchParams(new FormData(form));data.set('page',location.href);
 button.disabled=true;
 startListSave();
 const submission=tableView.startAction(item);
 let saved=false;
 try{
  const response=await fetch(form.action,{method:'POST',body:data});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
  saved=true;
  tableView.preserveRecordRowTop(()=>{
   const asks=box.parentElement;
   box.remove();
   if(!asks.children.length) asks.remove();
   // The row's feedback form applies the saved state, so a Go that sets Working moves the row as Mark read does.
   recordDetails(item).querySelector('[data-feedback-form]').applySavedState(result);
   tableView.applyAction();
   if(item.hidden && tableView.opened.has(key)){tableView.closeCard(key);tableView.applyAction();}
  });
  if(hadFocus && listOwnsFocus()){
   if(item.hidden) focusAfterQuestion(item);
   else (recordDetails(item)?.querySelector('[data-go-form] button') ?? item.querySelector('.list-toggle')).focus();
  }
 }catch(error){
  button.disabled=false;
  tableView.showActionResult(item,submission,'Item '+item.dataset.number,
   {kind:'failure',message:'Not sent: '+(error instanceof Error ? error.message : String(error))});
 }finally{
  saving--;
  refreshAfterListSave(saved);
 }
});
let listCounts=null,reviewSaveVersion=0;
const listCountsSource=()=>content.querySelector('[data-list-counts-url]');
function presentReviewLinks(row,links){
 const target=recordDetails(row).querySelector('[data-list-review-links]');
 if(target && links && target.innerHTML!==links.innerHTML)
  tableView.updateItemDetail(row,detail=>detail.querySelector('[data-list-review-links]').replaceChildren(...links.childNodes));
}
function rowIn(root,key){
 return root.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
}
function loadListCounts(){
 const source=listCountsSource();
 if(!source || listCounts) return listCounts;
 listCounts=refreshListCounts(source);
 return listCounts;
}
async function readListCounts(source){
 try{
  const response=await fetch(source.dataset.listCountsUrl,{cache:'no-store',signal:listRequestSignal()});
  if(response.ok) return await response.text();
 }catch(_){}
 return undefined;
}
async function refreshListCounts(source){
 const pendingRows=()=>[...content.querySelectorAll('[data-list-review-version]')];
 const versions=new Map(pendingRows().map(row=>[row.dataset.key,row.dataset.listReviewVersion]));
 const saveVersion=reviewSaveVersion;
 const wasRequested=row=>saveVersion===reviewSaveVersion &&
  versions.get(row.dataset.key)===row.dataset.listReviewVersion;
 try{
  const markup=await readListCounts(source);
  if(source!==listCountsSource()) return;
  const rows=pendingRows().filter(wasRequested);
  tableView.preserveRecordRowTop(()=>updateLinkedRows(rows,fillListCounts(source,markup)));
 }catch(_){}finally{
  listCounts=null;
  if(source!==listCountsSource() || pendingRows().some(row=>!wasRequested(row))) await loadListCounts();
 }
}
function fillListCounts(footer,markup){
 if(markup!==undefined) footer.innerHTML=markup;
 const updates=new Map();
 for(const update of footer.querySelectorAll('template[data-list-review-key]')){
  updates.set(decodeURIComponent(update.dataset.listReviewKey),update);
  update.remove();
 }
 return updates;
}
function updateLinkedRows(rows,updates){
 for(const row of rows){
  const version=row.dataset.listReviewVersion;
  if(!version) continue;
  const update=updates.get(row.dataset.key);
  const summary=row.querySelector('[data-list-review-summary]');
  if(!update || update.dataset.listReviewVersion!==version){
   if(summary) summary.textContent=summary.dataset.listReviewSummary;
   continue;
  }
  if(summary){
   summary.replaceChildren(...update.content.querySelector('[data-list-review-summary]').childNodes);
   summary.dataset.listReviewSummary='';
  }
  const links=update.content.querySelector('[data-list-review-links]');
  presentReviewLinks(row,links);
  delete row.dataset.listReviewVersion;
 }
}
async function refresh(_supersede=false,current=()=>true){
 if(saving || parkPickerBlocksRefresh()) return;
 const signal=listRequestSignal();
 const delivery=tableView.deliveryRequest();
 const query=delivery ? '?'+new URLSearchParams({delivery:delivery.delivery}) : '';
 const requestStarted=performance.now();
 const response=await fetch(location.pathname+query,{cache:'no-store',signal});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html');
 const fresh=page.querySelector('[data-list]');
 if(!fresh) throw new Error('The page has no list.');
 decodeListKeys(fresh);
 await tableView.settled();
 if(!current() || saving || parkPickerBlocksRefresh()) return;
 refreshNeeded=false;
 tableView.preserveRecordRowTop(()=>applyListSnapshot(fresh));
 reviewRefreshError.textContent='';
 reviewRefreshError.hidden=true;
 if(delivery && response.headers?.get('X-Item-Delivery-Pending')==='false')
  tableView.finishDeliveryWindow(delivery);
 const facts=pageRefreshFacts(page);
 scheduleParkExpiry(facts,requestStarted);
 return facts;
}
function applyListSnapshot(fresh){
 const field=listOwnsFocus() && content.contains(document.activeElement) ? document.activeElement : null;
 const focus=field?.id,start=field?.selectionStart,end=field?.selectionEnd;
 const direction=field?.selectionDirection;
 const reviewLink=field?.matches('[data-list-review-card]') ? reviewLinkPlace(field) : null;
 const action=field?.matches('button[name="action"]') ? field.value : '';
 const focusedRow=action || field?.matches('.list-toggle') ? field.closest('[data-list-item]')?.dataset.key : '';
 recordDetails(tableView.openItem)?.querySelector('[data-feedback-form]')?.retainDraft?.();
 tableView.holdLog();
 const footer=content.querySelector('[data-list-counts-url]'),nextFooter=fresh.querySelector('[data-list-counts-url]');
 if(footer && nextFooter && !nextFooter.childNodes.length)
  nextFooter.replaceChildren(...footer.childNodes);
 restoreEvidenceDetails(fresh);
 const key=tableView.openItem?.dataset.key;
 const nextItem=key && rowIn(fresh,key);
 if(key && !nextItem) tableView.closeCard(key);
 const links=nextItem && !nextItem.dataset.listReviewVersion &&
  recordDetails(nextItem).querySelector('[data-list-review-links]');
 content.replaceWith(fresh);content=fresh;
 closedKeys=new Set(JSON.parse(content.dataset.closedKeys));
 bindListControls();tableView.replace();
 const openItem=tableView.openItem;
 if(links && openItem?.dataset.key===key) presentReviewLinks(openItem,links);
 tableView.restoreLog();
 if(field?.isConnected) field.focus({preventScroll:true});
 else if(focus){const field=document.getElementById(focus);field?.focus({preventScroll:true});
  if(typeof field?.setSelectionRange==='function' && typeof start==='number' && typeof end==='number')
   field.setSelectionRange(start,end,direction);}
 if(reviewLink && !field?.isConnected) restoreReviewLink(reviewLink);
 if(focusedRow && !field?.isConnected){
  const row=[...content.querySelectorAll('[data-list-item]')].find(item=>item.dataset.key===focusedRow && !item.hidden);
  const button=action && recordDetails(row)?.querySelector('button[name="action"][value="'+CSS.escape(action)+'"]:enabled');
  (button || row?.querySelector('.list-toggle') || content.querySelector('#list-search')).focus({preventScroll:true});
 }
 void loadListCounts();
}
const reviewDialog=document.querySelector('[data-list-review-dialog]');
const reviewClose=reviewDialog.querySelector('button');
const reviewMotion=createCardMotion(()=>content);
const listHost=window.frameElement?.matches('iframe[data-console-page="list"]') ? window.frameElement : null;
let activeReview,refreshingReview=false;
const savedReviews=new Set(),returnedReviews=[];
const reviewRefreshError=document.querySelector('[data-review-refresh-error]');
function fitReviewDialog(){
 const phone=(listHost?.ownerDocument.defaultView || window).matchMedia(''' + json.dumps(console.PHONE_QUERY) + r''');
 const style=listHost?.ownerDocument.createElement('style');
 if(style){
  style.textContent='.frame-area > iframe[data-list-review-fullscreen]{position:fixed;inset:0;width:100vw;height:100dvh;z-index:9}';
  listHost.ownerDocument.head.append(style);
 }
 const fit=()=>{
  reviewDialog.toggleAttribute('data-fullscreen',phone.matches);
  listHost?.toggleAttribute('data-list-review-fullscreen',phone.matches);
 };
 phone.addEventListener('change',fit);fit();
 return ()=>{
  phone.removeEventListener('change',fit);style?.remove();
  reviewDialog.removeAttribute('data-fullscreen');listHost?.removeAttribute('data-list-review-fullscreen');
 };
}
function reviewLinkPlace(link){
 const row=recordFromDetail(link),href=link.getAttribute('href');
 const selector='[data-list-review-card][href="'+CSS.escape(href)+'"]';
 const detail=recordDetails(row).contains(link),scope=detail ? recordDetails(row) : row;
 return {key:row.dataset.key,selector,detail,position:[...scope.querySelectorAll(selector)].indexOf(link)};
}
function restoreReviewLink({key,selector,detail,position}){
 const findRow=()=>content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]:not([hidden])');
 const visibleLink=row=>{
  const scope=detail ? recordDetails(row) : row;
  const link=scope?.querySelectorAll(selector)[position];
  return link?.getClientRects().length ? link : null;
 };
 const row=findRow(),link=visibleLink(row);
 const target=link || row?.querySelector('.list-toggle') || content.querySelector('#list-search');
 target.focus({preventScroll:true});
 if(!link && row?.hasAttribute('data-list-review-version') && listCounts){
  void listCounts.then(()=>{
   if(!activeReview && listOwnsFocus() && document.activeElement===target)
    visibleLink(findRow())?.focus({preventScroll:true});
  });
 }
}
function showReviewCard(card){
 activeReview=card;
 card.frame.hidden=false;
 card.restoreHost=fitReviewDialog();
 reviewClose.disabled=card.frame.hasAttribute('data-saving');
 reviewDialog.showModal();
}
function hideReviewCard(card){
 if(activeReview!==card) return;
 activeReview=null;
 reviewDialog.close();
 card.restoreHost();
 card.frame.hidden=true;
 if(!listHost?.hidden && document.visibilityState==='visible') restoreReviewLink(card.link);
}
function reopenReturnedReview(){
 if(!activeReview && returnedReviews.length) showReviewCard(returnedReviews.shift());
}
function closeReviewCard(){
 const card=activeReview;
 if(!card || card.frame.hasAttribute('data-saving')) return;
 reviewMotion.finish();
 hideReviewCard(card);
 card.frame.remove();
 reviewClose.disabled=false;
 reopenReturnedReview();
}
async function refreshSavedReview(){
 if(saving || refreshingReview || !savedReviews.size) return;
 const cards=[...savedReviews];
 refreshingReview=true;
 const version=listSaveVersion;
 await pageRefresh.refresh(true);
 if(cards.some(card=>rowIn(content,card.link.key)?.hasAttribute('data-list-review-version'))) await listCounts;
 refreshingReview=false;
 if(version!==listSaveVersion){
  void refreshSavedReview();
  return;
 }
 for(const card of cards){
  savedReviews.delete(card);
  if(activeReview===card) void reviewMotion.switchCard(card.frame,null,()=>{if(activeReview===card) closeReviewCard();});
  else card.frame.remove();
 }
 void refreshSavedReview();
}
if(listHost) new MutationObserver(()=>{
 if(!listHost.hidden) return;
 const doc=activeReview?.frame.contentDocument;
 const form=doc?.querySelector('[data-review-form]');
 if(!form?.elements.feedback?.value && !form?.elements.later?.value && !doc?.querySelector('[data-review-draft]'))
  closeReviewCard();
}).observe(listHost,{attributes:true,attributeFilter:['hidden']});
bindConsoleDialog(reviewDialog,closeReviewCard);
document.addEventListener('click',event=>{
 const link=event.target instanceof Element && event.target.closest('a[data-list-review-card]');
 if(!link || event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
 event.preventDefault();
 if(activeReview) return;
 const frame=document.createElement('iframe');
 frame.title='Review card';
 frame.src=link.href;
 const card={frame,link:reviewLinkPlace(link)};
 activeReview=card;
 frame.addEventListener('load',()=>{
  const doc=frame.contentDocument;
  doc?.addEventListener('keydown',event=>{
   if(event.key==='Escape' && !event.defaultPrevented && activeReview===card){
    event.preventDefault();closeReviewCard();
   }
  });
 });
 frame.addEventListener('review-card-sending',()=>{
  hideReviewCard(card);
  reopenReturnedReview();
 });
 frame.addEventListener('review-card-busy',()=>{
  const busy=frame.hasAttribute('data-saving');
  if(activeReview===card) reviewClose.disabled=busy;
  else if(!busy){
   returnedReviews.push(card);
   reopenReturnedReview();
  }
 });
 frame.addEventListener('review-card-saved',event=>{
  if(activeReview===card) reviewClose.disabled=false;
  if(event.detail){
   reviewRefreshError.textContent=event.detail;
   reviewRefreshError.hidden=false;
  }
  reviewSaveVersion++;
  savedReviews.add(card);
  refreshAfterListSave(false);
 });
 reviewDialog.append(frame);
 showReviewCard(card);
});
tableView.place();
tableView.bind();bindListControls();tableView.apply(true);
addEventListener('popstate',tableView.restorePlace);
addEventListener('hashchange',tableView.restorePlace);
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
function refreshAfterNewItem(){
 listSaveVersion++;
 refreshAfterListSave(true);
}
document.addEventListener('review-item-saved',refreshAfterNewItem);
document.addEventListener('review-item-created',refreshAfterNewItem);
scheduleParkExpiry(pageRefreshFacts(),performance.getEntriesByType('navigation')[0]?.requestStart ?? 0);
void loadListCounts();
})();''')
