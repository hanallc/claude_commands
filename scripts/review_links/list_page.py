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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, tzinfo
from enum import Enum, StrEnum
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict, cast
from urllib.parse import parse_qs, urlencode

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, footer_counts, record_store, session_staff, staff, the_list
from scripts.production.the_list import list_time
from scripts.production.update_registration import QuestionStaff, RegistrationFailure
from scripts.review_links import change_browser, change_page, console, page_frame, page_question, record_table, review_link
from scripts.review_links import review_items, review_page
from scripts.review_links import staff_names
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
    MORTY_ANSWERING = "morty-answering"
    WORKING = "working"
    BLOCKED = "blocked"
    PARKED = "parked"


CHIP_BY_STATE = {
    the_list.ItemState.NEW: ListChip.WORKING,
    the_list.ItemState.WORKING: ListChip.WORKING,
    the_list.ItemState.BLOCKED: ListChip.BLOCKED,
    the_list.ItemState.WAITING_FOR_NATE: ListChip.NEEDS_ME,
    the_list.ItemState.DELIVERED: ListChip.NEEDS_ME,
    the_list.ItemState.DONE: ListChip.ALL,
    the_list.ItemState.PAUSED: ListChip.BLOCKED,
}


def list_chip(state: the_list.ItemState, *, parked: bool, open_question: bool,
              unread_delivery: bool = False) -> ListChip:
    """Find the one status chip for this item."""
    chip = CHIP_BY_STATE[state]
    if parked:
        return ListChip.PARKED
    if unread_delivery:
        return ListChip.NEEDS_ME
    return ListChip.MORTY_ANSWERING if open_question else chip


def has_open_question(item: the_list.ListItem) -> bool:
    return any(question.answer is None for question in item.questions)


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
                     if isinstance(entry.item.unread_since, datetime) and not parked_for_delivery(entry.item))


def owner_name(members: staff.StaffSnapshot, owner: the_list.ItemOwner) -> str:
    member = members.by_id(owner) if not isinstance(owner, the_list.UnassignedOwner) else None
    return member.name if member is not None else str(owner)


def eta_text(eta: the_list.Eta, today: datetime) -> str:
    if isinstance(eta, datetime):
        return list_time(eta, today)
    if isinstance(eta, the_list.EtaRange):
        return list_time(eta.earliest, today) + " – " + list_time(eta.latest, today)
    return the_list.eta_text(eta)


def eta_cell_text(eta: the_list.Eta, today: datetime) -> str:
    if isinstance(eta, datetime):
        local = eta.astimezone(today.tzinfo)
        return f"{local:%b} {local.day} {local:%H:%M %Z}"
    if isinstance(eta, date):
        return f"{eta:%b} {eta.day}"
    if isinstance(eta, the_list.EtaRange):
        return eta_cell_text(eta.earliest, today) + " – " + eta_cell_text(eta.latest, today)
    return the_list.eta_text(eta)


def parked_label(moment: datetime | the_list.ParkUntilDelivered, zone: tzinfo) -> str:
    if moment is the_list.ParkUntilDelivered.DELIVERED:
        return moment.value
    local = moment.astimezone(zone)
    if local.hour or local.minute:
        return local.strftime("%Y-%m-%d %H:%M %Z")
    return local.date().isoformat()


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


def log_row(index: str, moment: datetime, body: str, today: datetime) -> str:
    return f'<li data-log-index="{index}">' + time_html(moment, today) + '<div>' + body + '</div></li>'


def log_entry_html(change: the_list.ItemChange, index: int, today: datetime, names: staff_names.StaffNames) -> str:
    content = change.what
    by = log_source_html(the_list.source_without_clock(change.source), names)
    if isinstance(content, the_list.FieldChange):
        heading = content.action + by
        fields = ''.join(f'<dt>{FIELD_LABELS[field]}</dt><dd>{field_html(field, value, today, names)}</dd>'
                         for field, value in content.fields.items())
        body = f'<strong>{heading}</strong><dl>{fields}</dl>'
    elif isinstance(content, the_list.PageFeedback):
        label = {"pending": "Feedback for morty", "sent": "Feedback sent to morty",
                 "alerted": "Feedback could not reach morty"}[content.state]
        body = label + by + change_browser.question_paragraphs(content.words)
    elif isinstance(content, the_list.QuestionAnswered):
        body = "Answered question " + html.escape(content.question_id) + by
    elif isinstance(content, the_list.QuestionClosed):
        body = ("Question " + html.escape(content.question.question_id) + " closed with the item" + by
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
    elif isinstance(content, str):
        body = html.escape(content) + by
    else:
        return ""
    return log_row(str(index), change.time, body, today)


def item_log_html(item: the_list.ListItem, today: datetime, names: staff_names.StaffNames,
                  unread_answers: frozenset[str]) -> str:
    answers = {question.question_id for question in item.questions if question.answer is not None}
    answer_entries: dict[str, the_list.ItemChange] = {}
    rows: list[tuple[datetime, str]] = []
    for index, change in enumerate(item.log):
        content = change.what
        if isinstance(content, the_list.QuestionAnswered) and content.question_id in answers:
            answer_entries[content.question_id] = change
        elif not the_list.is_page_request_receipt(change):
            rows.append((change.time, log_entry_html(change, index, today, names)))
    for question in item.questions:
        answer = question.answer
        identity = question.question_id
        if answer is None or identity in unread_answers:
            continue
        asked = datetime.fromisoformat(question.asked_at).astimezone(UTC)
        question_body = "Question " + html.escape(identity) + log_source_html("nate", names)
        entry = answer_entries.get(identity)
        answered = entry.time if entry is not None else datetime.fromisoformat(answer.answered_at).astimezone(UTC)
        source = the_list.source_without_clock(entry.source) if entry is not None else item.producer
        answer_body = "Answer" + log_source_html(source, names)
        rows.append((asked, log_row(f"{identity}-asked", asked,
                                   question_body + change_browser.question_paragraphs(question.words), today)))
        rows.append((answered, log_row(f"{identity}-answered", answered,
                                       answer_body + change_browser.question_paragraphs(answer.text), today)))
    if not answers.difference(unread_answers):
        return ''.join(markup for _, markup in reversed(rows))
    # Use reverse record order for events with the same time.
    return ''.join(markup for _, markup in sorted(reversed(rows), key=lambda row: row[0], reverse=True))


def item_link(keeper: staff.StaffId, number: int) -> str:
    return '#' + urlencode({"item": f"{keeper}/L{number}"})


@dataclass(frozen=True)
class ListFormRequestPrefix:
    """Identify the rendered form and the unread update it shows."""

    nonce: str
    seen_update: the_list.UnreadUpdate | the_list.NoUnreadUpdate

    @classmethod
    def parse(cls, request: str) -> ListFormRequestPrefix:
        match = re.fullmatch(r"(?P<nonce>[a-f0-9]{32})(?P<update>[a-f0-9]{64})?(?:\.[a-f0-9]{32})?", request)
        if match is None:
            raise question_record.AnswerRefused("the List request does not match this page")
        fingerprint = match["update"]
        return cls(match["nonce"], the_list.UnreadUpdate(fingerprint) if fingerprint else the_list.NoUnreadUpdate.ABSENT)

    def format(self) -> str:
        update = self.seen_update
        return self.nonce + (update.fingerprint if isinstance(update, the_list.UnreadUpdate) else "")


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
    request = ListFormRequestPrefix(secrets.token_hex(16), the_list.unread_update(entry.item)).format()
    return request, signed_token(secret, entry.keeper, entry.item.id, request)


def signed_form_fields(entry: ListRecord, secret: str) -> str:
    request, token = signed_item_request(entry, secret)
    fields = {"keeper": entry.keeper, "id": entry.item.id, "request": request, "token": token}
    return ''.join(f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
                   for name, value in fields.items())


def feedback_form(entry: ListRecord, secret: str, today: datetime, resends: tuple[the_list.QuestionResend, ...],
                  *, parking_label: str = "") -> str:
    hidden = signed_form_fields(entry, secret)
    read = '<button name="action" value="read">Mark read</button>' if isinstance(entry.item.unread_since, datetime) else ''
    key = html.escape(entry.key.replace('/', '-'), quote=True)
    parked = '' if parking_label else ' hidden'
    note = 'Parked until ' + parking_label if parking_label else ''
    retry = "".join(question_resend(resend, today) for resend in resends)
    return ('<form method="post" data-feedback-form>' + hidden + f'<p data-parked-detail{parked}>{note}</p>'
            + '<div class="list-actions"><button name="action" value="remove">Remove</button>' + read
            + f'<div class="list-parking-actions"><button name="action" value="unpark" id="unpark-{key}"{parked}>Unpark</button>'
            + f'<div class="list-park"><button type="button" id="park-{key}" aria-haspopup="dialog" aria-expanded="false" data-later-toggle>Park until</button>'
            + '<div role="dialog" aria-label="Park until" data-later-menu hidden>'
            + '<button type="submit" name="action" value="park-until-delivered" data-later-preset>Until delivered</button>'
            + record_table.date_time_picker(field="until", action="action", value="park") + '</div></div></div></div>'
            + retry
            + f'<textarea name="words" id="words-{key}" rows="4" maxlength="10000" '
            + 'placeholder="Your words for morty about this item" aria-label="Your words for morty about this item"></textarea>'
            + '<div class="list-tell"><span role="status" data-send-status></span>'
            + '<button name="action" value="question" disabled>Ask question</button>'
            + '<button name="action" value="tell" disabled>Tell morty</button></div></form>')


def question_resend(resend: the_list.QuestionResend, today: datetime) -> str:
    question = resend.question
    notice = {"failed": "did not reach morty", "ready": "is ready to send",
              "prepared": "is ready to send"}.get(resend.delivery.state, "may not have reached morty")
    block = resend.delivery
    action = (f'<p>{html.escape(block.reason)}</p>' if isinstance(block, change_page.ResendBlocked)
              else f'<button name="action" value="resend-{question.question_id}">Resend question {question.question_id}</button>')
    return (f'<section class="page-question" data-question="{question.question_id}">'
            + f'<h3>Question {question.question_id} {notice}</h3>'
            + question_time_html(question.asked_at, today)
            + f'<p>{html.escape(question.words)}</p>' + action + '</section>')


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


WAITING_NOTE = "The producer will answer."


def question_time_html(stamp: str, today: datetime) -> str:
    return time_html(datetime.fromisoformat(stamp), today)


def panel_questions(item: the_list.ListItem, today: datetime, unread_answers: frozenset[str],
                    resends: tuple[the_list.QuestionResend, ...] = ()) -> str:
    retry = {resend.question.question_id for resend in resends}
    sections: list[str] = []
    for question in item.questions:
        answer = question.answer
        if answer is None and question.question_id not in retry:
            sections.append(page_question.open_question_html(
                question, asked_time_html=question_time_html(question.asked_at, today)))
        elif answer is not None and question.question_id in unread_answers:
            sections.append(page_question.answered_question_html(question, answer,
                asked_time_html=question_time_html(question.asked_at, today),
                answer_time_html=question_time_html(answer.answered_at, today),
                answer_mark_html=' <span class="question-unread">Unread</span>'))
    return ''.join(sections)


def detail_panel(entry: ListRecord, sections: str, today: datetime, names: staff_names.StaffNames,
                 unread_answers: frozenset[str], secret: str) -> str:
    log = item_log_html(entry.item, today, names, unread_answers)
    log_id = html.escape('log-' + entry.key.replace('/', '-'), quote=True)
    read = ''
    keeper = names.members.by_id(entry.keeper)
    if unread_answers and keeper is not None and keeper.kind == "producer" and keeper.name == "morty":
        request, token = signed_item_request(entry, secret)
        read = f' data-open-read-request="{request}" data-open-read-token="{token}"'
    return (f'<div class="list-panel"{read}><div>' + sections
            + '</div><section class="list-log-panel"><h3>Log · newest first</h3>'
            + f'<div class="list-log-scroll" id="{log_id}" tabindex="0" role="region" aria-label="Log · newest first">'
            + '<ol class="list-log">' + log + '</ol></div></section></div>')


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
    return review_items.read(root, members=members, assign_ids=False)


def linked_reviews(root: Path, members: staff.StaffSnapshot, records: ImmediateReviewRecords,
                   items: Iterable[the_list.ListItem], *, defer_missing: bool = False) -> ReviewRecords:
    if (records is LinkedReviewRead.FROM_DISK
            or (not defer_missing and any(not has_all_linked_reviews(item, records) for item in items))):
        return {review.key: review for review in read_linked_reviews(root, members=members)}
    return records


def review_links(item: the_list.ListItem, reviews: ReviewDisplay, today: datetime) -> str:
    if not item.reviews:
        return ""
    if reviews is LinkedReviewRead.AFTER_RENDER:
        return '<div data-list-review-links></div>'
    cards = (review_card(identity, reviews) for identity in item.reviews)
    return '<p>' + review_summary(item, reviews, today) + '</p><p>' + ' · '.join(cards) + '</p>'


def render_review_updates(root: Path, members: staff.StaffSnapshot, review_records: ImmediateReviewRecords) -> str:
    """Render only the linked Review fields for items on the List."""
    entries = [entry for entry in records() if entry.item.reviews]
    if not entries:
        return ""
    reviews = linked_reviews(root, members, review_records, (entry.item for entry in entries))
    today = the_list.now()
    return ''.join(
        f'<template data-list-review-key="{html.escape(entry.key, quote=True)}"'
        + f' data-list-review-version="{review_version(entry.item)}">'
        + '<span data-list-review-summary>' + review_summary(entry.item, reviews, today) + '</span>'
        + '<div data-list-review-links>' + review_links(entry.item, reviews, today) + '</div></template>'
        for entry in entries)


def review_version(item: the_list.ListItem) -> str:
    return html.escape(json.dumps((str(item.updated), len(item.log), item.reviews)), quote=True)


def waiting_feedback_form(entry: ListRecord, secret: str, parking_label: str) -> str:
    unread = the_list.unread_delivery(entry.item)
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
            + '<span role="status" data-send-status></span></form>')


def waiting_details(entry: ListRecord, today: datetime, names: staff_names.StaffNames,
                    unread_answers: frozenset[str], secret: str,
                    reviews: ReviewDisplay, parking_label: str) -> str:
    sections = (f'<p data-waiting-note>{WAITING_NOTE}</p><div data-waiting-questions>'
                + panel_questions(entry.item, today, unread_answers) + '</div>'
                + review_links(entry.item, reviews, today) + waiting_feedback_form(entry, secret, parking_label))
    return detail_panel(entry, sections, today, names, unread_answers, secret)


def details(entry: ListRecord, today: datetime, secret: str, reviews: ReviewDisplay,
            names: staff_names.StaffNames, visibility: the_list.WaitingForAnswer | the_list.QuestionResends,
            unread_answers: frozenset[str], *, parking_label: str) -> str:
    item = entry.item
    if isinstance(visibility, the_list.WaitingForAnswer):
        return waiting_details(entry, today, names, unread_answers, secret, reviews, parking_label)
    status_time = item.latest_status_time
    status_stamp = " · " + time_html(status_time, today) if item.latest_status and isinstance(status_time, datetime) else ""
    sections = ''.join(
        f'<section><h3>{FIELD_LABELS[field]}{status_stamp if field == "latest_status" else ""}</h3>'
        + field_html(field, value, today, names) + '</section>'
        for field, value in (("latest_status", item.latest_status), ("done_when", item.done_when)))
    links = [f'<li><a data-record-jump href="{html.escape(item_link(entry.keeper, link.number), quote=True)}">#{link.number}</a>'
             + ' — ' + html.escape(link.condition) + '</li>' for link in item.blocked_by]
    links.extend(f'<li>Related: <a data-record-jump href="{html.escape(item_link(entry.keeper, number), quote=True)}">#{number}</a></li>'
                 for number in item.related_to)
    if links:
        sections += '<section><h3>Links</h3><ul>' + ''.join(links) + '</ul></section>'
    sections += review_links(item, reviews, today)
    questions = panel_questions(item, today, unread_answers, visibility.questions)
    form = feedback_form(entry, secret, today, visibility.questions, parking_label=parking_label)
    return f'<p data-waiting-note hidden>{WAITING_NOTE}</p>' + detail_panel(
        entry, sections + questions + form, today, names, unread_answers, secret)


def item_rows(entry: ListRecord, names: staff_names.StaffNames, today: datetime, index: int, secret: str,
              reviews: ReviewDisplay, *, parked: bool, open_question: bool,
              visibility: the_list.WaitingForAnswer | the_list.QuestionResends) -> str:
    item = entry.item
    owner = owner_name(names.members, item.owner)
    producer = owner_name(names.members, item.producer)
    unread = isinstance(item.unread_since, datetime)
    unread_answers = the_list.unread_answer_ids(item)
    waiting = isinstance(visibility, the_list.WaitingForAnswer)
    due = the_list.eta_due(item.eta)
    eta_sort: tuple[int, float | str] = (0, due.timestamp()) if due is not None else (1, eta_text(item.eta, today).casefold())
    values = (item.number, owner.casefold(), producer.casefold(), item.title.casefold(), item.state.value.casefold(),
              item.next_step.casefold(), eta_sort, item.unread_since.timestamp() if isinstance(item.unread_since, datetime) else 0)
    parking = (the_list.ParkUntilDelivered.DELIVERED.value
               if item.parked_until is the_list.ParkUntilDelivered.DELIVERED else str(parked).lower())
    attributes = {"key": entry.key, "number": str(item.number), "owner": str(item.owner), "producer": item.producer,
                  "keeper-produces": str(item.producer == entry.keeper).lower(), "state": item.state.value,
                  "unread": str(unread).lower(), "parked": parking, "waiting-for-answer": str(waiting).lower(),
                  "open-question": str(open_question).lower(), "unread-delivery": str(the_list.unread_delivery(item)).lower(),
                  "title": item.title.lower(),
                  "owner-name": owner.lower(),
                  "order": str(index), "sort-values": json.dumps(values, ensure_ascii=False)}
    attrs = ''.join(f' data-{key}="{html.escape(value, quote=True)}"' for key, value in attributes.items())
    panel = f'list-detail-{index}'
    row_id = html.escape(entry.key.replace("/", "-"), quote=True)
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
        state += '<small data-open-question-note>Morty answering</small>'
    if isinstance(item.next_check, datetime):
        check = field_html("next_check", item.next_check, today, names)
        if item.next_check < today:
            state += f'<small><span class="warning">Check overdue {check}</span></small>'
        else:
            state += f'<small>Check {check}</small>'
    elif isinstance(item.next_check, the_list.CheckCondition):
        state += '<small>Check: ' + field_html("next_check", item.next_check, today, names) + '</small>'
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
    sent = the_list.sent_to_morty(item)
    if isinstance(sent, datetime):
        feedback = tuple(the_list.feedback_requests(item).values())[-1]
        notice = "sent to " + names.html("morty")
        if feedback.state == "alerted":
            notice = ("not sent; " + names.html(feedback.alerted_to) + " told" if feedback.alerted_to
                      else "not sent; no showrunner for alert")
        update += '<small>' + notice + ", " + html.escape(list_time(sent, today)) + '</small>'
    overdue = due is not None and due < today and item.state != the_list.ItemState.CLOSED
    eta_class = "list-eta warning" if overdue else "list-eta"
    full_eta = field_html("eta", item.eta, today, names)
    short_eta = html.escape(eta_cell_text(item.eta, today))
    eta_cell = (f'<span class="{eta_class}" id="list-eta-{row_id}" tabindex="0" title="{full_eta}">'
                + f'<span class="list-eta-text">{short_eta}</span>'
                + f'<span class="list-eta-full" aria-hidden="true">{full_eta}</span></span>')
    cells = (number, field_html("owner", item.owner, today, names), field_html("producer", item.producer, today, names),
             title, state, field_html("next_step", item.next_step, today, names), eta_cell, update)
    row = ''.join(f'<td' + (' class="record-id"' if label == '#' else '')
                  + f' data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    return (f'<tbody data-list-item{attrs}><tr class="list-row">' + row
            + f'</tr><tr data-detail id="{panel}" hidden><td colspan="{len(HEADERS)}">'
            + details(entry, today, secret, reviews, names, visibility, unread_answers, parking_label=parking_label)
            + '</td></tr></tbody>')


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
    waiting_for_answer: str
    open_question: str
    unread_delivery: NotRequired[str]
    waiting_details_html: NotRequired[str]
    review_summary_html: NotRequired[str]
    park_request_saved: NotRequired[bool]


def saved_action_state(keeper: str, identity: str, *, status: change_page.DeliveryStatus | None = None,
                       park_request: str = "",
                       review_records: ImmediateReviewRecords = LinkedReviewRead.FROM_DISK) -> SavedActionState:
    owner = staff.StaffId(keeper)
    item = the_list.load(owner, identity)
    zone = user_zone()
    today = the_list.now()
    parking = item.parked_until
    parked_until = (parked_label(parking, zone)
                    if parking is not the_list.NoTimestamp.UNSET and the_list.parking_active(parking, today) else "")
    unread = item.unread_since
    reading = status if status is not None else change_page.DeliveryStatus(review_link.review_root())
    open_question = item.state != the_list.ItemState.CLOSED and has_open_question(item)
    waiting = open_question and isinstance(the_list.question_visibility(owner, item, reading), the_list.WaitingForAnswer)
    saved: SavedActionState = {"unread": str(isinstance(unread, datetime)).lower(),
                               "unread_sort": unread.timestamp() if isinstance(unread, datetime) else 0,
                               "item_state": item.state.value,
                               "parked": str(bool(parked_until)).lower(), "parked_until": parked_until,
                               "waiting_for_answer": str(waiting).lower(),
                               "open_question": str(open_question).lower(),
                               "unread_delivery": str(the_list.unread_delivery(item)).lower()}
    if park_request:
        saved["park_request_saved"] = the_list.has_park_receipt(item, park_request)
    if waiting:
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
        saved["waiting_details_html"] = waiting_details(
            ListRecord(owner, item), today.astimezone(zone), names, the_list.unread_answer_ids(item),
            form_secret(reading.root), reviews, parked_until)
    return saved


def _mark_seen_update_read(keeper: staff.StaffId, item: the_list.ListItem,
                           update: the_list.UnreadUpdate | the_list.NoUnreadUpdate) -> None:
    if isinstance(update, the_list.UnreadUpdate) and update == the_list.unread_update(item):
        try:
            _ = the_list.mark_read(keeper, item.id, source=the_list.PAGE_SOURCE, update=update)
        except (OSError, the_list.ListError):
            pass


def submit(root: Path, body: str, status: change_page.DeliveryStatus | None = None) -> ListActionSubmission:
    """Accept signed item actions and save their notices for morty."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"keeper", "id", "request", "token", "action", "words", "until"} or any(len(values) != 1 for values in fields.values()):
        raise question_record.AnswerRefused("use the form on this page")
    keeper, identity, request = fields["keeper"][0], fields["id"][0], fields["request"][0]
    if re.fullmatch(r"L[1-9]\d*", identity) is None:
        raise question_record.AnswerRefused("the List request does not match this page")
    prefix = ListFormRequestPrefix.parse(request)
    expected = signed_token(form_secret(root), keeper, identity, prefix.format())
    token = fields["token"][0].encode()
    if not secrets.compare_digest(token, expected.encode()):
        bootstrap = signed_token(bootstrap_form_secret(root), keeper, identity, prefix.format())
        if not secrets.compare_digest(token, bootstrap.encode()):
            raise question_record.AnswerRefused("the List token does not match this page")
    morty = staff.snapshot().by_id(staff.StaffId(keeper))
    if morty is None or morty.kind != "producer" or morty.name != "morty":
        raise question_record.AnswerRefused("the item has no morty producer")
    action, words, until = fields["action"][0], fields["words"][0].strip(), fields["until"][0]
    if action == "read":
        _ = the_list.mark_read(morty.id, identity, source=the_list.PAGE_SOURCE, update=prefix.seen_update,
                               page_request=the_list.PageRequest(request, "read", words))
        return change_page.NoMessageDelivery()
    item = the_list.load(morty.id, identity)
    resend = re.fullmatch(r"resend-(Q[1-9]\d*)", action)
    if action == "question" or resend is not None:
        status = change_page.DeliveryStatus(root) if status is None else status
        question = (the_list.resend_question(morty.id, identity, resend[1], request, status) if resend is not None
                    else the_list.ask_question(morty.id, identity, the_list.PageRequest(request, "question", words), status))
        if isinstance(question, change_page.MessageDelivery):
            if resend is None:
                _mark_seen_update_read(morty.id, item, prefix.seen_update)
            return ListQuestionDelivery(question, status)
        return question
    if action == "remove":
        notice = the_list.PageItemNotice(request, f"List page: nate removed item {item.number}.", "remove", words)
        changed = the_list.close(morty.id, identity, "Removed by nate on the List page.", source=the_list.PAGE_SOURCE, notice=notice)
    elif action == "park":
        if until != the_list.ParkUntilDelivered.DELIVERED and the_list.ISO_TIME.fullmatch(until) is None:
            raise question_record.AnswerRefused("choose Until delivered, a parking date, or a time with a UTC offset")
        parked_until = the_list.parking_value(until)
        assert parked_until is not the_list.NoTimestamp.UNSET
        label = parked_label(parked_until, user_zone())
        notice = the_list.PageItemNotice(request, f"List page: nate parked item {item.number} until {label}.", "park", words)
        changed = the_list.park(morty.id, identity, parked_until, source=the_list.PAGE_SOURCE, notice=notice)
    elif action == "unpark":
        notice = the_list.PageItemNotice(request, f"List page: nate unparked item {item.number}.", "unpark", words)
        changed = the_list.unpark(morty.id, identity, source=the_list.PAGE_SOURCE, notice=notice).item
    elif action == "tell" and words and len(words) <= 10000:
        saved = the_list.save_feedback(morty.id, identity,
                                       the_list.PageFeedback(request, words, read_update=prefix.seen_update))
        _mark_seen_update_read(morty.id, item, saved.read_update)
        return ListFeedbackDelivery(morty.id, identity, request) if saved.state == "pending" else change_page.NoMessageDelivery()
    else:
        raise question_record.AnswerRefused("enter words for morty")
    saved_notice = the_list.feedback_requests(changed).get(request)
    delivery = (ListFeedbackDelivery(morty.id, identity, request)
                if saved_notice is not None and saved_notice.state == "pending" else change_page.NoMessageDelivery())
    if action == "unpark":
        outcome = (UnparkOutcome(the_list.unpark_no_change_reason(changed, request).value)
                   if saved_notice is None else UnparkOutcome.CLEARED)
        return ListUnparkSubmission(outcome, delivery)
    return delivery


def alert_showrunners(item: the_list.ListItem, members: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """Find the first showrunner above the item owner without session or document reads."""
    return () if isinstance(item.owner, the_list.UnassignedOwner) else change_page.staff_showrunner(item.owner, members)


def deliver(submission: ListActionSubmission) -> None:
    """Send saved List words with the shared retry and alert helpers."""
    delivery = submission.delivery if isinstance(submission, ListUnparkSubmission) else submission
    if isinstance(delivery, change_page.NoMessageDelivery):
        return
    if isinstance(delivery, ListQuestionDelivery):
        change_page.deliver(delivery.delivery, delivery.status)
        return
    def send(item: the_list.ListItem, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
        settings = conf.read("review_links")
        tries = settings.whole("delivery_tries")
        message = (f"List page: nate's words for item {item.number}\n{feedback.words}"
                   if feedback.action == "tell" else feedback.words)
        source = the_list.folder(delivery.keeper) / f"{item.id}.md"
        key = change_page.review_delivery_key(source, item.id, feedback.request)
        failures = change_page.send_with_retries(change_page.SENDER, (delivery.keeper,), message,
                                                 tries, settings.number("delivery_pause_seconds"), key=key)
        if not failures:
            return replace(feedback, state="sent")
        owners = tuple(owner for owner in alert_showrunners(item, staff.snapshot()) if owner.id != delivery.keeper)
        reached = change_page.alert_delivery_failures(failures, tries, source=source, row=item.id, message=message,
            recipients=tuple(owner.id for owner in owners), attempt=feedback.request, title="List feedback needs attention")
        return replace(feedback, state="alerted", alerted_to=", ".join(owner.name for owner in owners)) if reached else feedback
    the_list.deliver_feedback(delivery.keeper, delivery.identity, delivery.request, send)


def recover_pending() -> None:
    """Retry each pending request once at server start."""
    for entry in records(include_closed=True):
        for feedback in the_list.feedback_requests(entry.item).values():
            if feedback.state == "pending":
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


def staff_chips(members: staff.StaffSnapshot, counts: Counter[the_list.ItemOwner]) -> list[record_table.Chip]:
    """Build a counted chip for each staff ID, in name order."""
    choices = [(re.sub(r"(?:-unit)+\Z", "", owner_name(members, identity)) or str(identity), identity, count)
               for identity, count in counts.items()]
    choices.sort(key=lambda choice: (choice[0].casefold(), choice[1]))
    return [record_table.Chip("all", "All", counts.total()),
            *(record_table.Chip(f"staff:{identity}", html.escape(label), count)
              for label, identity, count in choices)]


def render(root: Path, *, include_counts: bool = True, defer_counts: bool = False,
           status: change_page.DeliveryStatus | Literal["standalone"] = "standalone",
           review_records: ReviewRecords | LinkedReviewRead = LinkedReviewRead.FROM_DISK) -> bytes:
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
    secret = form_secret(root)
    owners: Counter[the_list.ItemOwner] = Counter()
    producers: Counter[the_list.ItemOwner] = Counter()
    rows: list[str] = []
    deferred_links = False
    chip_counts: Counter[ListChip] = Counter()
    for index, entry in enumerate(items):
        owners[entry.item.owner] += 1
        producers[entry.item.producer] += 1
        parked = the_list.parking_active(entry.item.parked_until, today)
        open_question = has_open_question(entry.item)
        visibility = the_list.question_visibility(entry.keeper, entry.item, status)
        chip_counts[list_chip(entry.item.state, parked=parked, open_question=open_question,
                              unread_delivery=the_list.unread_delivery(entry.item))] += 1
        row_reviews = reviews
        if entry.item.reviews and (
            reviews is LinkedReviewRead.AFTER_RENDER
            or (include_counts and defer_counts and review_records is not LinkedReviewRead.FROM_DISK
                and not has_all_linked_reviews(entry.item, reviews))
        ):
            row_reviews = LinkedReviewRead.AFTER_RENDER
            deferred_links = True
        rows.append(item_rows(entry, names, today, index, secret, row_reviews, parked=parked,
                              open_question=open_question, visibility=visibility))
    shows = [record_table.Chip("all", "All", len(items)),
             *(record_table.Chip(chip.value, chip.value.replace("-", " ").capitalize(), chip_counts[chip])
               for chip in ListChip if chip != ListChip.ALL)]
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" id="list-sort-{index}" data-sort="{index}">{label}'
                      + '<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    linked_headers = ''.join(f'<th scope="col">{label}</th>' for label in HEADERS)
    columns = ('<colgroup><col class="list-number-column"><col style="width:8%"><col style="width:10%">'
               '<col style="width:17%"><col style="width:14%"><col style="width:23%"><col style="width:14%">'
               '<col style="width:8%"></colgroup>')
    settings = conf.read("review_links")
    seconds = settings.number("page_refresh_seconds")
    request_timeout = settings.whole("page_request_timeout_seconds")
    card_motion = page_frame.CardMotion.read(settings)
    morning = record_table.clock_minutes(settings, "later_morning")
    log_max_height = settings.whole("list_log_max_height_rem")
    counts = ''
    if include_counts:
        if defer_counts:
            query = COUNTS_AND_LINKS_QUERY if deferred_links else COUNTS_QUERY
            counts = f'<div data-list-counts-url="{PAGE_PATH}?{html.escape(query)}"></div>'
        else:
            counts = counts_strip(members, names)
    expansion = ''.join(f'<button type="button" id="list-{action}-all" data-list-expansion="{action}"'
                        + f' aria-label="{label}" title="{label}">' + page_frame.icon_svg(icon) + '</button>'
                        for action, label, icon in (("expand", "Expand all", "chevrons-up-down"),
                                                    ("collapse", "Collapse all", "chevrons-down-up")))
    content = (STYLE + page_frame.title("The List", "list")
               + f'<div data-list data-refresh-seconds="{seconds}" data-zone="{html.escape(zone.key, quote=True)}"'
               + f' data-request-timeout-seconds="{request_timeout}"'
               + f' {card_motion.attributes()}'
               + f' data-closed-keys="{html.escape(json.dumps(closed_keys), quote=True)}"'
               + f' data-park-morning="{morning}" style="--list-log-max-height:{log_max_height}rem">'
               + '<div data-record-filters>'
               + record_table.chip_row('Owner', staff_chips(members, owners), 'all')
               + '<div class="list-producer-filter">'
               + record_table.chip_row('Producer', staff_chips(members, producers), 'all')
               + record_table.words_box('list-search', 'Filter by number, owner, item or state')
               + '</div>'
               + '<div class="list-show-filter"><div class="list-show-controls">' + record_table.chip_row('Show', shows, 'all')
               + '<label class="list-unread-switch"><input type="checkbox" role="switch" id="list-unread" value="1">'
               + 'Unread only</label></div>'
               + '<p data-hidden-action-status role="status" hidden></p></div>'
               + '</div>'
               + '<p data-missing-item role="status" hidden></p>'
               + '<div class="list-table-tools"><div class="list-expansion">' + expansion + '</div>'
               + '<span data-list-count role="status" aria-live="polite"></span></div>'
               + '<div class="list-table"><table aria-label="The List">' + columns + '<thead><tr>' + headers + '</tr></thead>'
               + ''.join(rows) + '</table>'
               + '<p data-empty hidden>No items in this view.</p></div>'
               + '<section data-linked-item hidden><h2 id="list-linked-heading">Linked item outside filters</h2>'
               + '<div class="list-table"><table aria-labelledby="list-linked-heading">' + columns
               + '<thead><tr>' + linked_headers + '</tr></thead></table></div></section>'
               + counts + '</div>'
               + '<dialog data-list-review-dialog aria-label="Review card"><div class="list-review-close">'
               + '<button type="button" autofocus>Close</button></div></dialog>'
               + '<script>' + SCRIPT + '</script>')
    return page_frame.rendered(change_page.document(content, "The List"), started, read_at)


STYLE = record_table.STYLE + record_table.DATE_TIME_STYLE + '''<style>
[data-list-review-dialog]{width:min(1060px,calc(100% - 48px));max-width:none;max-height:none;padding:0;
border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--ink);overflow:hidden;box-sizing:border-box}
[data-list-review-dialog]::backdrop{background:#0009}
.list-review-close{display:flex;justify-content:flex-end;padding:8px 12px}
.list-review-close button{margin:0;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
[data-list-review-dialog] iframe{display:block;width:100%;height:var(--review-card-height,15rem);
max-height:calc(100dvh - 108px);border:0}
:root:has([data-list-review-dialog][open][data-fullscreen]){overflow:hidden;scrollbar-gutter:auto}
[data-list-review-dialog][data-fullscreen]{width:100vw;height:100dvh;border:0;border-radius:0;margin:0}
[data-list-review-dialog][data-fullscreen][open]{display:grid;grid-template-rows:auto minmax(0,1fr)}
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
.list-table-tools{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.list-table-tools [data-list-count]{min-width:0;color:var(--ink-2);font-size:13px;text-align:right;overflow-wrap:anywhere}
[data-linked-item]{margin-top:16px}
[data-linked-item] h2{font-size:14px;margin:0 0 8px}
[data-linked-item] th{padding:8px}
.list-number-column{width:6%}
@media(960px < width <= 1100px){.list-number-column{width:62px}}
.list-expansion{display:flex;flex:none;gap:6px}
.list-expansion button{box-sizing:border-box;display:grid;place-items:center;width:26px;height:26px;min-height:0;padding:0;cursor:pointer}
.list-expansion svg{width:16px;height:16px}
.list-table td[data-label="Next step"]>:first-child{margin-top:0}.list-table td[data-label="Next step"]>:last-child{margin-bottom:0}
.list-actions{position:relative}
.list-parking-actions{display:flex;gap:inherit;margin-inline-start:auto}
.list-park [data-later-menu] [data-later-preset]{display:block;width:calc(100% - 12px);margin:6px 6px 4px}
.list-table tbody:has([data-parked-detail]:not([hidden])) .list-row.open [data-parked-note]{display:none}
.list-table:has(.list-park>[data-later-menu]:not([hidden]),.list-eta:focus-visible),
.list-table tbody:has(.list-park>[data-later-menu]:not([hidden]),.list-eta:focus-visible){overflow:visible}
.list-tell{display:flex;justify-content:flex-end;align-items:center;gap:10px;flex-wrap:wrap}
.list-tell [value="tell"]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.list-tell button:disabled{opacity:.45;cursor:default}.list-tell [data-send-status]{margin-left:0}
.list-log dl{display:grid;grid-template-columns:min(7rem,42%) minmax(0,1fr);gap:2px 12px;margin:4px 0 0}
.list-log dt{color:var(--ink-3)}.list-log dd{margin:0;overflow-wrap:anywhere}
.list-log dd>:last-child{margin-bottom:0}
.list-log dl li{display:list-item;padding:0;border:0}
.page-question p{white-space:pre-wrap;overflow-wrap:anywhere}
.page-question time{color:var(--ink-3);font-size:13px}
.page-question .question-unread{margin-inline-start:6px;color:var(--accent);text-transform:none}
.list-panel:has([data-waiting-note]){min-height:12rem}
.list-panel [data-waiting-note]{margin-top:16px}
.list-table td[data-label="State"]{color:var(--ink)}
.list-table td[data-label="Producer"],.list-table td[data-label="ETA"]{color:var(--ink-2)}
.list-eta{display:block;position:relative;min-width:0}
.list-eta-text{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.list-eta:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.list-eta-full{display:none;position:absolute;inset-block-start:calc(100% + 4px);inset-inline-end:0;z-index:1;
width:max-content;max-width:min(28rem,calc(100vw - 4rem));padding:6px 8px;background:var(--card);
border:1px solid var(--line);border-radius:6px;overflow-wrap:anywhere}
.list-eta:focus-visible .list-eta-full{display:block}
@media(max-width:960px){
.list-expansion button{width:34px;height:34px}
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

SCRIPT = (page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + page_frame.MOTION_SCRIPT + record_table.SCRIPT
          + record_table.DATE_TIME_SCRIPT + '''(() => {
const chipsByQuestion=''' + json.dumps({
    str(open_question).lower(): {
        str(parked).lower(): {
            state.value: list_chip(state, parked=parked, open_question=open_question).value for state in CHIP_BY_STATE
        } for parked in (False, True)
    } for open_question in (False, True)
}) + ''';
const shows=''' + json.dumps(list(ListChip)) + r''';
const chipsByState=chipsByQuestion.false.false;
const deliveryParking=''' + json.dumps(the_list.ParkUntilDelivered.DELIVERED) + r''';
const isParked=data=>data.parked==='true' || data.parked===deliveryParking;
const chipFor=data=>{
 if(data.state==='Closed') return;
 const parked=isParked(data);
 if(!parked && data.unreadDelivery==='true') return 'needs-me';
 return chipsByQuestion[data.openQuestion][String(parked)][data.state];
};
let content=document.querySelector('[data-list]');
let closedKeys=new Set(JSON.parse(content.dataset.closedKeys));
let saving=0,listSaveVersion=0;
function startListSave(){
 saving++;listSaveVersion++;pageRefresh.abandon();
}
function refreshAfterListSave(needed){
 if(savedReview) void refreshSavedReview();
 else if(needed) void pageRefresh.refresh(true);
}
const drafts=new Map();
const unparkMessages={
 cleared:'Unparked',
 cleared_newer_parking_active:'The original Unpark cleared parking. A newer parking time is active',
 expired:'Parking time already expired',
 no_active_parking:'No active parking time to clear',
};
/** @type {Map<string, {index: string, offset: number}>} */
const logPositions=new Map();
function visibleLogs(){
 return content.querySelectorAll('[data-list-item]:not([hidden]) [data-detail]:not([hidden]) .list-log-scroll');
}
function rememberLogPositions(){
 for(const log of visibleLogs()){
  if(!log.clientHeight) continue;
  const key=log.closest('[data-list-item]').dataset.key;
  if(!log.scrollTop){logPositions.delete(key);continue;}
  const top=log.getBoundingClientRect().top;
  for(const entry of log.querySelector('.list-log').children){
   const bounds=entry.getBoundingClientRect();
   if(bounds.bottom<=top) continue;
   logPositions.set(key,{index:entry.dataset.logIndex,offset:bounds.top-top});
   break;
  }
 }
}
function restoreLogPositions(){
 if(!logPositions.size) return;
 for(const log of visibleLogs()){
  const position=logPositions.get(log.closest('[data-list-item]').dataset.key);
  if(!position || !log.clientHeight) continue;
  const entry=log.querySelector('[data-log-index="'+position.index+'"]');
  if(entry) log.scrollTop+=entry.getBoundingClientRect().top-log.getBoundingClientRect().top-position.offset;
 }
}
function matchesWords(data,search){
 if(!search) return true;
 const number=search.replace(/^#?l?(?=\d)/,'');
 return [data.number,data.ownerName,data.title,data.state,data.parked!=='false' ? 'Parked' : '']
  .some(value=>(value || '').toLowerCase().includes(value===data.number ? number : search));
}
function syncUnread(item,form,unread,unreadSort){
 item.dataset.unread=String(unread);
 const sortValues=JSON.parse(item.dataset.sortValues);
 sortValues[7]=unread ? unreadSort : 0;
 item.dataset.sortValues=JSON.stringify(sortValues);
 const update=item.querySelector('[data-label="Update"]'),dot=update.querySelector('.list-dot');
 const read=form.querySelector('[value="read"]');
 if(unread){
  update.querySelector('[aria-label="No unread update"]')?.remove();
  if(!dot){const mark=document.createElement('span');mark.className='list-dot';mark.setAttribute('role','img');
   mark.setAttribute('aria-label','Unread update');update.prepend(mark);}
  const canRead=!form.hasAttribute('data-waiting-actions') || item.dataset.unreadDelivery==='true';
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
const pendingOpenReads=new Map(),keepOpenAfterRead=new Set();
/** @type {Map<string, {unseen: Set<Element>, seen: Set<string>}>} */
const watchedAnswers=new Map();
let unreadShow='',unreadFilterVersion=0;
// The second threshold reports a positive intersection after contact at the viewport edge.
const answerVisibility=new IntersectionObserver(entries=>{
 if(listIsHidden()) return;
 for(const entry of entries){
  if(!entry.isIntersecting || !entry.intersectionRect.width || !entry.intersectionRect.height) continue;
  const item=entry.target.closest('[data-list-item]');
  if(!item?.isConnected || item.hidden || entry.target.closest('[data-detail]').hidden) continue;
  const watch=watchedAnswers.get(item.dataset.key);
  if(!watch?.unseen.delete(entry.target)) continue;
  watch.seen.add(entry.target.parentElement.dataset.question);
  answerVisibility.unobserve(entry.target);
  if(!watch.unseen.size) void markOpenedAnswerRead(item,watch);
 }
},{threshold:[0,0.000001]});
function listIsHidden(){
 return document.visibilityState!=='visible' || window.frameElement?.hidden===true;
}
function stopWatchingOpenedAnswer(key){
 for(const answer of watchedAnswers.get(key)?.unseen ?? []) answerVisibility.unobserve(answer);
 watchedAnswers.delete(key);
}
function watchOpenedAnswer(item,seen=new Set()){
 const key=item.dataset.key;
 if(!tableView.opened.has(key) || pendingOpenReads.has(key) || watchedAnswers.has(key)) return;
 const unseen=new Set([...item.querySelectorAll(
  '.list-panel[data-open-read-request] .page-question:has(.question-unread) > p:last-child')]
  .filter(answer=>!seen.has(answer.parentElement.dataset.question)));
 if(!unseen.size) return;
 watchedAnswers.set(key,{unseen,seen});
 if(!listIsHidden()) for(const answer of unseen) answerVisibility.observe(answer);
}
function resumeOpenedAnswerReads(){
 if(listIsHidden()) return;
 for(const {unseen} of watchedAnswers.values()) for(const answer of unseen){
  answerVisibility.unobserve(answer);
  answerVisibility.observe(answer);
 }
}
async function markOpenedAnswerRead(item,watch){
 const key=item.dataset.key;
 if(watchedAnswers.get(key)!==watch || watch.unseen.size) return;
 if(item.hidden || !tableView.opened.has(key) || listIsHidden()) return;
 const panel=item.querySelector('[data-detail]:not([hidden]) .list-panel');
 const request=panel?.dataset.openReadRequest;
 if(!request || pendingOpenReads.has(key)) return;
 stopWatchingOpenedAnswer(key);
 const token=panel.dataset.openReadToken;
 const filterVersion=unreadFilterVersion;
 const [keeper,id]=key.split('/');
 const body=new URLSearchParams({keeper,id,request,token,action:'read',words:'',until:''});
 const submission=tableView.startAction(item);
 const pending={keepOpen:true};
 pendingOpenReads.set(key,pending);startListSave();
 let saved=false;
 try{
  const response=await fetch(location.pathname,{method:'POST',body});
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
  saved=true;
  if(result.unread==='false'){
   delete panel.dataset.openReadRequest;
   if(pending.keepOpen && unreadShow && filterVersion===unreadFilterVersion && tableView.opened.has(key))
    keepOpenAfterRead.add(key);
  }
 }catch(error){
  tableView.showActionResult(item,submission,'Item '+item.dataset.number,
   {kind:'failure',message:'Read not confirmed: '+error.message});
 }finally{
  pendingOpenReads.delete(key);saving--;
  refreshAfterListSave(saved);
 }
}
const tableView=createRecordTable({content:()=>content,
 shows,
 defaultShow:'all',
 place:params=>{
  const show=params.get('show');
  if(show==='unread'){params.set('show','all');params.set('unread','1');}
  const state=params.get('state'),stateChip=chipsByState[state];
  if(show==='done' || show==='closed' || state==='Closed') params.set('show','all');
  else if((show==='open' || show==='all' || show==='unread' || !show) && Object.hasOwn(chipsByState,state))
   params.set('show',stateChip);
 },
 filters:['producer','unread'],
 staffChipFilters:['owner','producer'],
 defaultOrder:data=>[Number(isParked(data)),Number(data.order)],
 beforeApply:rememberLogPositions,
 applied:(_items,{show,filters})=>{
  const nextUnreadShow=filters.unread ? show : '';
  if(nextUnreadShow!==unreadShow){
   unreadShow=nextUnreadShow;unreadFilterVersion++;keepOpenAfterRead.clear();
  }
  for(const key of watchedAnswers.keys()) if(!tableView.opened.has(key)) stopWatchingOpenedAnswer(key);
  for(const key of keepOpenAfterRead) if(!tableView.opened.has(key)) keepOpenAfterRead.delete(key);
  restoreLogPositions();
 },
 itemOpened:watchOpenedAnswer,
 expandedSelection:(data,selection)=>selection.filters.unread && selection.show===unreadShow &&
  keepOpenAfterRead.has(data.key) ? {...selection,filters:{...selection.filters,unread:''}} : undefined,
 hasKey:key=>!closedKeys.has(key) && !!content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]'),
 revealLinked:(data,{filters})=>data.openQuestion==='true' || (filters.unread && data.unread!=='true'),
 missingItem:key=>{
  const number=key.match(/\/L([1-9]\d*)$/)?.[1];
  if(!number) return '';
  return 'Item #'+number+(closedKeys.has(key) ? ' is closed' : ' is not on the List');
 },
 filterValues:(data,search,{filters})=>{
  const chip=chipFor(data);
  if(!chip || (filters.unread && data.unread!=='true') || !matchesWords(data,search)) return null;
  return {show:chip==='all' ? [] : [chip],owner:[data.owner],producer:[data.producer]};
 },
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
function bindListControls(){
 content.querySelectorAll('[data-list-expansion]').forEach(button=>button.addEventListener('click',()=>{
  tableView.setShownExpanded(button.dataset.listExpansion==='expand');
 }));
 content.querySelectorAll('[data-feedback-form]').forEach(bindFeedbackForm);
}
function bindFeedbackForm(form){
  const item=form.closest('[data-list-item]'),key=item.dataset.key;
  const notice=form.querySelector('[data-send-status]');
  const unpark=form.querySelector('[value="unpark"]'),parkedDetail=form.querySelector('[data-parked-detail]');
  notice.textContent=tableView.actionNotice(item);
  const waitingForm=form.hasAttribute('data-waiting-actions');
  const confirmed={state:item.dataset.state,parked:item.dataset.parked,
   unread:item.dataset.unread==='true',unreadDelivery:item.dataset.unreadDelivery,
   unreadSort:JSON.parse(item.dataset.sortValues)[7]};
  const syncConfirmed=()=>{
   item.dataset.state=confirmed.state;
   item.dataset.parked=confirmed.parked;
   item.dataset.unreadDelivery=confirmed.unreadDelivery;
   unpark.hidden=confirmed.parked==='false' || confirmed.state==='Closed';
   syncUnread(item,form,confirmed.unread,confirmed.unreadSort);
  };
  const applySavedState=result=>{
   delete item.dataset.listReviewVersion;
   if(typeof result.review_summary_html==='string'){
    const summary=item.querySelector('[data-list-review-summary]');
    if(summary) summary.innerHTML=result.review_summary_html;
   }
   if(typeof result.item_state==='string') confirmed.state=result.item_state;
   if(confirmed.state==='Closed'){
    closedKeys.add(key);
    tableView.closeCard(key);
   }
   const unread=result.unread==='true';
   if(['true','false'].includes(result.parked))
    confirmed.parked=result.parked_until===deliveryParking ? deliveryParking : result.parked;
   if(['true','false'].includes(result.unread_delivery)) confirmed.unreadDelivery=result.unread_delivery;
   if(['true','false'].includes(result.unread)){
    confirmed.unread=unread;
    confirmed.unreadSort=unread ? (result.unread_sort ?? confirmed.unreadSort) : 0;
   }
   syncConfirmed();
   const state=item.querySelector('.list-state');state.textContent=item.dataset.state;
   state.className='list-state '+({'Waiting for nate':'warning',Blocked:'error',Working:'accent',Delivered:'room',Done:'room'}[item.dataset.state] || 'muted');
   const sortValues=JSON.parse(item.dataset.sortValues);sortValues[4]=item.dataset.state.toLowerCase();item.dataset.sortValues=JSON.stringify(sortValues);
   if(typeof result.parked_until==='string'){
    let note=item.querySelector('[data-parked-note]');
    parkedDetail.hidden=!result.parked_until;
    parkedDetail.textContent=result.parked_until ? 'Parked until '+result.parked_until : '';
    if(result.parked_until){
     if(!note){note=document.createElement('small');note.dataset.parkedNote='';state.parentElement.append(note);}
     note.textContent=parkedDetail.textContent;
    }else note?.remove();
   }
   if(['true','false'].includes(result.open_question)) item.dataset.openQuestion=result.open_question;
   const questionNote=item.querySelector('[data-open-question-note]');
   if(item.dataset.openQuestion==='true'){
    if(!questionNote){
     const note=document.createElement('small');
     note.dataset.openQuestionNote='';
     note.textContent='Morty answering';
     state.after(note);
    }
   }else questionNote?.remove();
   const waiting=result.waiting_for_answer==='true';
   if(waiting){
    item.dataset.waitingForAnswer='true';
    const detail=item.querySelector('[data-detail] td');
    if(typeof result.waiting_details_html==='string'){
     detail.innerHTML=result.waiting_details_html;
     return detail.querySelector('[data-feedback-form]');
    }
    const note=detail.querySelector('[data-waiting-note]');
    note.hidden=false;
    detail.querySelector('.list-panel > div').replaceChildren(note);
   }
  };
  const words=form.elements.words,until=form.elements.until;
  const tell=form.querySelector('[value="tell"]'),ask=form.querySelector('[value="question"]');
  const syncTell=()=>{
   if(ask && tell) ask.disabled=tell.disabled=Boolean(form.dataset.sending) || !words.value.trim();
  };
  // Keep the signed update from the rendered row until the row refreshes.
  const signed={request:form.elements.request.value,token:form.elements.token.value};
  let fresh=signed;
  const newRequest=()=>({
   request:signed.request+'.'+Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join(''),
   token:signed.token
  });
  const draft=drafts.get(key);if(draft && !waitingForm){words.value=draft.words;until.value=draft.until;}
  const unconfirmed=draft?.unconfirmed ?? new Set();
  const matching=action=>[...unconfirmed].findLast(attempt=>attempt.words===words.value &&
   (attempt.action==='park-until-delivered' || attempt.until===until.value) &&
   (action===undefined || attempt.action===action));
  const identity=request=>{form.elements.request.value=request.request;form.elements.token.value=request.token;};
  const retainDraft=()=>{
   drafts.set(key,{words:waitingForm ? draft?.words ?? '' : words.value,
    until:waitingForm ? draft?.until ?? '' : until.value,unconfirmed});
   identity(matching() ?? fresh);
  };
  const retain=()=>{retainDraft();syncTell();};
  retain();
  words.addEventListener('input',retain);until.addEventListener('input',retain);
  const zone=content.dataset.zone;
  const parkToggle=form.querySelector('[data-later-toggle]');
  if(!waitingForm) bindDateTimePicker(form,{zone,defaultMinutes:()=>Number(content.dataset.parkMorning),summaryPrefix:'Parks until ',
   retryTime:()=>Date.parse(matching('park')?.parkingTime ?? '')});
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(cardSubmitBlocked(form,tableView.rowFading(key))) return;
   const submitter=event.submitter,action=submitter?.value || 'tell';
   const parkForDelivery=action==='park-until-delivered',parks=action==='park' || parkForDelivery;
   const focusUnpark=action==='unpark' && document.activeElement===unpark;
   const usesWords=action==='tell' || action==='question';
   const asksQuestion=action==='question' || action.startsWith('resend-');
   if(usesWords && !words.value.trim()) return;
   const retry=matching(action);
   let parkingTime=parkForDelivery ? deliveryParking : retry?.parkingTime ?? until.value;
   if(action==='park'){
    if(!retry && !/^\d{4}-\d{2}-\d{2}$/.test(parkingTime)){
     const at=dateTimeDraftEpoch(zone,parkingTime);
     if(!(at>Date.now())){notice.textContent='Choose a future parking time.';return;}
     parkingTime=new Date(at).toISOString();
    }
   }
   if(!waitingForm && (parks || action==='unpark')) closeDateTimePicker(form.querySelector('[data-later-menu]'),false);
   const attempt=retry ?? {...fresh,action,words:words.value,until:until.value,parkingTime};
   identity(attempt);
   const data=new URLSearchParams(new FormData(form));data.set('action',parks ? 'park' : action);
   if(parks) data.set('until',parkingTime);
   if(!retry){unconfirmed.add(attempt);fresh=newRequest();}
   retainDraft();
   startListSave();
   const hadFocus=asksQuestion && item.contains(document.activeElement);
   let focusChoice;
   const trackFocus=event=>{if(event.target!==document.body) focusChoice=event.target;};
   if(hadFocus) document.addEventListener('focusin',trackFocus);
   let saved=false,refused=false,nextForm;
   setCardSubmitting(form,true,item);
   notice.textContent=action==='read' ? 'Saving…' : 'Sending…';
   const clearsUnread=action==='read' || usesWords,unreadBefore=item.dataset.unread,wasVisible=!item.hidden;
   if(action==='remove'){
    item.dataset.state='Closed';
    unpark.hidden=true;
    syncUnread(item,form,false,0);
   }
   if(parks) item.dataset.parked=parkForDelivery ? deliveryParking : 'true';
   if(clearsUnread) item.dataset.unread='false';
   const releasesHolds=action==='read' || parks || action==='remove';
   const restoreLink=releasesHolds ? tableView.releaseLink(key) : undefined;
   const readFilterVersion=unreadFilterVersion;
   const hadReadHold=releasesHolds && keepOpenAfterRead.delete(key);
   const pendingRead=releasesHolds ? pendingOpenReads.get(key) : undefined;
   if(pendingRead) pendingRead.keepOpen=false;
   const submission=tableView.startAction(item);
   const hidUnread=clearsUnread && wasVisible && item.hidden;
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    refused=!response.ok && (response.status<500 || (!asksQuestion && !parks && action!=='remove'));
    const result=await response.json();
    if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
    saved=true;
    nextForm=applySavedState(result);
    const unread=result.unread==='true';
    const unparked=action==='unpark' && result.unpark_outcome==='cleared';
    const message=action==='remove' ? 'Removed' : parks ? (parkForDelivery ? 'Parked until delivered' : 'Parked') :
     action==='unpark' ? (unparkMessages[result.unpark_outcome] ?? 'Unpark result unavailable') :
     action==='read' ? (unread ? 'Update remains unread' : 'Marked read') :
     asksQuestion ? (unread ? 'Question saved; update remains unread' : 'Question saved') :
     action==='tell' && unread ? 'sent to morty; update remains unread' : 'sent to morty';
    if(usesWords && words.value===data.get('words')) words.value='';
    if(unparked) until.value='';
    if(action==='unpark' || parkForDelivery){
     const label='Item #'+item.dataset.number;
     const hiddenMessage=parkForDelivery ? label+' parked until delivered.'
      : unparked ? label+' unparked.' : label+': '+message+'.';
     tableView.showActionResult(item,submission,label,{kind:'success',message,hiddenMessage});
    }else{
     notice.textContent=message;
     tableView.applyAction();
    }
   }catch(error){
    const wasHidden=item.hidden;
    let restore=refused || releasesHolds;
    if(refused) syncConfirmed();
    else if(parks || action==='remove'){
     restore=false;
     try{
      const query=new URLSearchParams({'item-state':data.get('id'),keeper:data.get('keeper')});
      if(parks) query.set('park-request',attempt.request);
      const response=await fetch(location.pathname+'?'+query,{cache:'no-store'});
      if(!response.ok) throw new Error('HTTP '+response.status);
      const current=await response.json();
      nextForm=applySavedState(current);
      restore=confirmed.state!=='Closed' && (!parks || current.park_request_saved===false);
     }catch(_){
      // Keep the pending row state until a refresh can read the record.
     }
    }else if(clearsUnread) item.dataset.unread=unreadBefore;
    if(restore){
     restoreLink?.();
     if(readFilterVersion===unreadFilterVersion){
      if(hadReadHold) keepOpenAfterRead.add(key);
      if(pendingRead) pendingRead.keepOpen=true;
     }
    }
    if(refused || hidUnread) tableView.opened.add(key);
    const message=(refused ? 'Not saved: ' : 'Save not confirmed: ')+error.message;
    tableView.showActionResult(item,submission,'Item '+item.dataset.number,{kind:'failure',message});
    // The return animation now has the confirmed sort keys.
    if(refused && wasHidden && !item.hidden) tableView.applyAction();
   }
   finally{
    if(saved || refused) unconfirmed.delete(attempt);
    setCardSubmitting(form,false,item);syncTell();
    retainDraft();
    if(nextForm) bindFeedbackForm(nextForm);
    if(focusUnpark && document.hasFocus() &&
       (document.activeElement===document.body || document.activeElement===unpark)){
     const currentUnpark=item.querySelector('[value="unpark"]:not([hidden])');
     const target=saved && item.hidden ? content.querySelector('[data-filter-row="show"] [aria-pressed="true"]')
      : currentUnpark ?? item.querySelector('.list-toggle');
     if(target?.getClientRects().length){
      target.focus({preventScroll:true});
      if(saved && item.hidden){
       const status=content.querySelector('[data-hidden-action-status]');
       const top=target.getBoundingClientRect().top;
       const inset=document.querySelector('[data-page-status]').offsetHeight+12;
       if(top<inset || status.getBoundingClientRect().bottom>innerHeight)
        scrollBy({top:Math.floor(top-inset),behavior:'instant'});
      }
     }
    }
    if(parkForDelivery && saved && item.hidden && listOwnsFocus() && document.activeElement===document.body){
     content.querySelector('[data-filter-row="show"] [aria-pressed="true"]')?.focus({preventScroll:true});
    }
    if(!saved && parks){
     await tableView.settled();
     if(listOwnsFocus() && document.activeElement===document.body && parkToggle.getClientRects().length && !parkToggle.disabled){
      parkToggle.focus({preventScroll:true});
     }
    }
    if(hadFocus && !item.hidden && document.activeElement===document.body)
     await tableView.rowSettled(item);
    saving--;
    if(hadFocus){
     document.removeEventListener('focusin',trackFocus);
     if(listOwnsFocus()){
      if(item.hidden && (!focusChoice || item.contains(focusChoice))) focusAfterQuestion(item);
      else if(!focusChoice?.isConnected && !item.hidden && document.activeElement===document.body){
       const target=!saved && submitter?.matches(':enabled') ? submitter : item.querySelector('.list-toggle');
       target.focus({preventScroll:true});
      }
     }
    }
    refreshAfterListSave((saved && item.dataset.waitingForAnswer!=='true') ||
      (!saved && !refused && (parks || action==='remove')));
   }
  });
}
let listCounts=null,reviewSaveVersion=0;
function loadListCounts(){
 const source=content.querySelector('[data-list-counts-url]');
 if(!source || listCounts) return listCounts;
 listCounts=readListCounts(source);
 return listCounts;
}
async function readListCounts(source){
 const pendingRows=()=>[...content.querySelectorAll('[data-list-review-version]')];
 const versions=new Map(pendingRows()
  .map(row=>[row.dataset.key,row.dataset.listReviewVersion]));
 const saveVersion=reviewSaveVersion;
 const wasRequested=row=>saveVersion===reviewSaveVersion &&
  versions.get(row.dataset.key)===row.dataset.listReviewVersion;
 try{
  const signal=AbortSignal.timeout(Number(content.dataset.requestTimeoutSeconds)*1000);
  const response=await fetch(source.dataset.listCountsUrl,{cache:'no-store',signal});
  if(!response.ok) return;
  const markup=await response.text();
  const footer=content.querySelector('[data-list-counts-url]');
  if(signal.aborted || !footer) return;
  footer.innerHTML=markup;
  const updates=new Map([...footer.querySelectorAll('template[data-list-review-key]')]
   .map(update=>[update.dataset.listReviewKey,update]));
  if(updates.size) for(const row of pendingRows().filter(wasRequested)){
   const update=updates.get(row.dataset.key);
   if(!update || update.dataset.listReviewVersion!==row.dataset.listReviewVersion) continue;
   for(const selector of ['[data-list-review-summary]','[data-list-review-links]']){
    const target=row.querySelector(selector),source=update.content.querySelector(selector);
    if(target && source) target.replaceChildren(...source.childNodes);
   }
   delete row.dataset.listReviewVersion;
  }
  for(const update of updates.values()) update.remove();
 }catch(_){}finally{
  listCounts=null;
  const pending=pendingRows();
  for(const row of pending.filter(wasRequested)){
   const summary=row.querySelector('[data-list-review-summary]');
   if(summary) summary.textContent=summary.dataset.listReviewSummary;
  }
  if(pending.some(row=>!wasRequested(row))) await loadListCounts();
 }
}
async function refresh(_supersede=false,current=()=>true){
 if(saving || dateTimePickerOpen(content)) return;
 const signal=AbortSignal.timeout(Number(content.dataset.requestTimeoutSeconds)*1000);
 const response=await fetch(location.pathname,{cache:'no-store',signal});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html');
 const fresh=page.querySelector('[data-list]');
 if(!fresh) throw new Error('The page has no list.');
 await tableView.settled();
 if(saving || dateTimePickerOpen(content) || !current()) return;
 const top=scrollY,field=listOwnsFocus() ? document.activeElement : null;
 const focus=field?.id,start=field?.selectionStart,end=field?.selectionEnd;
 const reviewLink=field?.matches('[data-list-review-card]') ? reviewLinkPlace(field) : null;
 const action=field?.matches('button[name="action"]') ? field.value : '';
 const focusedRow=action || field?.matches('.list-toggle') ? field.closest('[data-list-item]')?.dataset.key : '';
 rememberLogPositions();
 const surviving=new Set([...fresh.querySelectorAll('[data-list-item]')].map(item=>item.dataset.key));
 for(const key of logPositions.keys()) if(!surviving.has(key)) logPositions.delete(key);
 const priorWatches=new Map(watchedAnswers);
 for(const key of priorWatches.keys()) stopWatchingOpenedAnswer(key);
 const footer=content.querySelector('[data-list-counts-url]'),nextFooter=fresh.querySelector('[data-list-counts-url]');
 if(footer && nextFooter) nextFooter.replaceChildren(...footer.childNodes);
 content.replaceWith(fresh);content=fresh;
 void loadListCounts();
 closedKeys=new Set(JSON.parse(content.dataset.closedKeys));
 bindListControls();tableView.replace();
 if(priorWatches.size) for(const item of content.querySelectorAll('[data-list-item]')){
  const watch=priorWatches.get(item.dataset.key);
  if(watch) watchOpenedAnswer(item,watch.seen);
 }
 if(focus){const field=document.getElementById(focus);field?.focus({preventScroll:true});
  if(typeof field?.setSelectionRange==='function' && typeof start==='number' && typeof end==='number') field.setSelectionRange(start,end);}
 if(reviewLink) restoreReviewLink(reviewLink);
 if(focusedRow){
  const row=[...content.querySelectorAll('[data-list-item]')].find(item=>item.dataset.key===focusedRow && !item.hidden);
  const button=action && row?.querySelector('[data-detail]:not([hidden]) button[name="action"][value="'+CSS.escape(action)+'"]:enabled');
  (button || row?.querySelector('.list-toggle') || content.querySelector('#list-search')).focus({preventScroll:true});
 }
 scrollTo({top,behavior:'instant'});
 watchLinkedAnswer();
 return pageRefreshFacts(page);
}
const reviewDialog=document.querySelector('[data-list-review-dialog]');
const reviewClose=reviewDialog.querySelector('button');
const reviewMotion=createCardMotion(()=>content);
const listHost=window.frameElement?.matches('iframe[data-console-page="list"]') ? window.frameElement : null;
let activeReview,savedReview,refreshingReview=false;
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
 const row=link.closest('[data-list-item]'),href=link.getAttribute('href');
 const selector='[data-list-review-card][href="'+CSS.escape(href)+'"]';
 return {key:row.dataset.key,selector,position:[...row.querySelectorAll(selector)].indexOf(link)};
}
function restoreReviewLink({key,selector,position}){
 const findRow=()=>content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]:not([hidden])');
 const visibleLink=row=>{
  const link=row?.querySelectorAll(selector)[position];
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
function closeReviewCard(){
 const card=activeReview;
 if(!card || card.frame.hasAttribute('data-saving')) return;
 activeReview=null;
 reviewMotion.finish();
 reviewDialog.close();
 card.restoreHost?.();
 card.remove();
 reviewClose.disabled=false;
 if(!listHost?.hidden && document.visibilityState==='visible') restoreReviewLink(card.link);
}
async function refreshSavedReview(){
 if(saving || refreshingReview || !savedReview) return;
 const card=savedReview;
 refreshingReview=true;
 const version=listSaveVersion;
 await pageRefresh.refresh(true);
 const row=content.querySelector('[data-list-item][data-key="'+CSS.escape(card.link.key)+'"]');
 if(activeReview===card && row?.hasAttribute('data-list-review-version')) await listCounts;
 refreshingReview=false;
 if(savedReview!==card || version!==listSaveVersion){
  void refreshSavedReview();
  return;
 }
 savedReview=null;
 if(activeReview===card)
  void reviewMotion.switchCard(card.frame,null,()=>{if(activeReview===card) closeReviewCard();});
}
if(listHost) new MutationObserver(()=>{
 if(!listHost.hidden) return;
 const doc=activeReview?.frame.contentDocument;
 const form=doc?.querySelector('[data-review-form]');
 if(!form?.elements.feedback?.value && !form?.elements.later?.value && !doc?.querySelector('[data-review-draft]'))
  closeReviewCard();
}).observe(listHost,{attributes:true,attributeFilter:['hidden']});
reviewClose.addEventListener('click',()=>closeReviewCard());
reviewDialog.addEventListener('cancel',event=>{event.preventDefault();closeReviewCard();});
let pressedBackdrop=false;
reviewDialog.addEventListener('pointerdown',event=>{pressedBackdrop=event.target===reviewDialog;});
reviewDialog.addEventListener('click',event=>{
 if(pressedBackdrop && event.target===reviewDialog) closeReviewCard();
});
document.addEventListener('click',event=>{
 const link=event.target instanceof Element && event.target.closest('a[data-list-review-card]');
 if(!link || event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
 event.preventDefault();
 if(activeReview) return;
 const frame=document.createElement('iframe');
 frame.title='Review card';
 frame.src=link.href;
 const observer=new ResizeObserver(([entry])=>frame.style.setProperty('--review-card-height',Math.ceil(entry.borderBoxSize[0].blockSize)+'px'));
 const card={frame,link:reviewLinkPlace(link),restoreHost:fitReviewDialog(),
  remove:()=>{observer.disconnect();frame.remove();}};
 activeReview=card;
 frame.addEventListener('load',()=>{
  const doc=frame.contentDocument;
  const main=doc?.querySelector('main');
  if(main) observer.observe(main,{box:'border-box'});
  doc?.addEventListener('keydown',event=>{
   if(event.key==='Escape' && !event.defaultPrevented && activeReview===card){
    event.preventDefault();closeReviewCard();
   }
  });
 });
 frame.addEventListener('review-card-busy',()=>{reviewClose.disabled=frame.hasAttribute('data-saving');});
 frame.addEventListener('review-card-saved',()=>{
  reviewSaveVersion++;
  savedReview=card;
  void refreshSavedReview();
 });
 reviewDialog.append(frame);
 pressedBackdrop=false;
 reviewDialog.showModal();
});
tableView.place();
tableView.bind();bindListControls();tableView.apply(true);
addEventListener('popstate',tableView.restorePlace);
addEventListener('hashchange',event=>{
 tableView.restorePlace(event);
 watchLinkedAnswer();
});
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
document.addEventListener('visibilitychange',resumeOpenedAnswerReads);
window.addEventListener('message',event=>{
 if(event.source===parent && event.origin===location.origin && event.data==='console-shown') resumeOpenedAnswerReads();
});
function watchLinkedAnswer(){
 if(!tableView.linkedItem) return;
 const row=[...content.querySelectorAll('[data-list-item]:not([hidden])')].find(item=>item.dataset.key===tableView.linkedItem);
 if(row) watchOpenedAnswer(row);
}
watchLinkedAnswer();
void loadListCounts();
})();''')
