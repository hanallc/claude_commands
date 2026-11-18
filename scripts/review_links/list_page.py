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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, TypedDict, cast
from urllib.parse import parse_qs, urlencode

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, footer_counts, record_store, session_staff, staff, the_list
from scripts.production.update_registration import QuestionStaff, RegistrationFailure
from scripts.review_links import change_browser, change_page, console, page_frame, page_question, record_table, review_link
from scripts.review_links import review_items, review_page
from scripts.review_links import staff_names
from scripts.review_links import question_record
from scripts.statusline import counts_row, rebuild_row

PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/list.html"
FIELD_LABELS = {
    "title": "Title", "owner": "Owner", "producer": "Producer", "unit": "Unit", "state": "State",
    "next_check": "Next check", "eta": "ETA", "blocked_by": "Blocked by", "related_to": "Related to",
    "requested_by": "Requested by", "next_step": "Next step", "latest_status": "Latest status",
    "evidence": "Evidence", "done_when": "Done when", "parked_until": "Parked until",
    "closing_words": "Closing words",
}
HEADERS = ("#", FIELD_LABELS["owner"], FIELD_LABELS["producer"], "Item",
           *(FIELD_LABELS[field] for field in ("state", "next_step", "eta")), "Update")
SHOWS = (("open", "Open"), ("needs-me", "Needs me"), ("unread", "Unread"), ("done", "Done"),
         ("parked", "Parked"), ("closed", "Closed"), ("all", "All"))


class ListRecord(NamedTuple):
    """An item and the fixed producer ID that owns its record."""

    keeper: staff.StaffId
    item: the_list.ListItem

    @property
    def key(self) -> str:
        return f"{self.keeper}/{self.item.id}"


def page_url() -> str:
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def records() -> list[ListRecord]:
    """Read the list folders even when a producer has no current staff row."""
    try:
        owners = sorted(record_store.state_root().iterdir())
    except FileNotFoundError:
        return []
    found = [ListRecord(staff.StaffId(owner.name), item) for owner in owners
             if owner.is_dir() and not owner.is_symlink()
             for item in the_list.records(staff.StaffId(owner.name), include_closed=True)]
    return sorted(found, key=lambda entry: the_list.order(entry.item))


def waiting_updates(_request: console.NewsRequest) -> frozenset[str]:
    """Identify each unread update by its record and time."""
    return frozenset(f"{entry.key}/{entry.item.unread_since.isoformat()}" for entry in records()
                     if isinstance(entry.item.unread_since, datetime))


def owner_name(members: staff.StaffSnapshot, owner: the_list.ItemOwner) -> str:
    member = members.by_id(owner) if not isinstance(owner, the_list.UnassignedOwner) else None
    return member.name if member is not None else str(owner)


def local_time(moment: datetime, today: datetime) -> str:
    local = moment.astimezone(today.tzinfo)
    return local.strftime("%H:%M %Z" if local.date() == today.date() else "%Y-%m-%d %H:%M %Z")


def eta_text(eta: the_list.Eta, today: datetime) -> str:
    if isinstance(eta, datetime):
        return local_time(eta, today)
    if isinstance(eta, the_list.EtaRange):
        return local_time(eta.earliest, today) + " – " + local_time(eta.latest, today)
    return the_list.eta_text(eta)


def field_html(field: str, value: object, today: datetime, names: staff_names.StaffNames) -> str:
    if field in ("owner", "producer"):
        owner = cast(the_list.ItemOwner, value)
        return names.html(owner, owner_name(names.members, owner))
    if field == "eta":
        return html.escape(eta_text(cast(the_list.Eta, value), today))
    if field == "parked_until":
        if not isinstance(value, datetime):
            return "Not parked"
        zone = today.tzinfo
        assert zone is not None
        return the_list.parking_date(value, zone)
    if isinstance(value, datetime):
        return html.escape(local_time(value, today))
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


def log_entry_html(change: the_list.ItemChange, index: int, today: datetime, names: staff_names.StaffNames) -> str:
    content = change.what
    by = " <small>— " + names.html(change.source, owner_name(names.members, staff.StaffId(change.source))) + "</small>"
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
    elif isinstance(content, the_list.MarkedRead):
        body = "Marked read" + by
    elif isinstance(content, the_list.Reopened):
        body = "Reopened" + by
    elif isinstance(content, the_list.ReviewLinksChanged):
        body = content.action + ": " + html.escape(", ".join(content.keys)) + by
    elif isinstance(content, the_list.EvidenceCopyRemoved):
        body = f"Removed the migration copy of {content.copy} from {FIELD_LABELS['evidence']}" + by
    elif isinstance(content, str):
        body = html.escape(content) + by
    else:
        return ""
    return (f'<li data-log-index="{index}"><time datetime="' + change.time.isoformat() + '">' + html.escape(local_time(change.time, today))
            + '</time><div>' + body + '</div></li>')


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


def request_token(root: Path, keeper: str, identity: str, prefix: str) -> str:
    """Authorize the form prefix for this item."""
    secret = change_browser.browser_manifest(change_page.console_folder(root))[0]
    return signed_token(secret, keeper, identity, prefix)


def feedback_form(entry: ListRecord, secret: str) -> str:
    request = ListFormRequestPrefix(secrets.token_hex(16), the_list.unread_update(entry.item)).format()
    token = signed_token(secret, entry.keeper, entry.item.id, request)
    fields = {"keeper": entry.keeper, "id": entry.item.id, "request": request, "token": token}
    hidden = ''.join(f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
                     for name, value in fields.items())
    read = '<button name="action" value="read">Mark read</button>' if isinstance(entry.item.unread_since, datetime) else ''
    key = html.escape(entry.key.replace('/', '-'), quote=True)
    return ('<form method="post" data-feedback-form>' + hidden + '<div class="list-actions">' + read
            + '<button name="action" value="remove">Remove</button>'
            + f'<label for="park-{key}">Park until</label><input type="date" name="until" id="park-{key}">'
            + '<button name="action" value="park">Park</button></div>'
            + f'<textarea name="words" id="words-{key}" rows="2" maxlength="10000" '
            + 'placeholder="Your words for morty about this item" aria-label="Your words for morty about this item"></textarea>'
            + '<div class="list-tell"><span role="status" data-send-status></span>'
            + '<button name="action" value="question" disabled>Ask question</button>'
            + '<button name="action" value="tell" disabled>Tell morty</button></div></form>')


def review_place(keys: tuple[str, ...]) -> str:
    """Open one linked card or select a group of linked reviews."""
    if len(keys) == 1:
        return review_page.PAGE_PATH + '?show=all#' + urlencode({"show": "open", "jump": keys[0]})
    return review_page.PAGE_PATH + '?show=all#' + urlencode([("key", key) for key in keys])


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
    else:
        text = "Reviews done — decide: close, keep or park"
    destinations = tuple(review.key for review in linked if review.type in review_page.TYPES)
    return count_link("review", review_place(destinations), text) if destinations else text


def review_card(identity: str, reviews: Mapping[str, review_items.ReviewItem]) -> str:
    """Link the shown ID only when the Review page contains its card type."""
    if identity not in reviews:
        return html.escape(identity)
    review = reviews[identity]
    label = html.escape(review.id)
    if review.type not in review_page.TYPES:
        return label
    return count_link("review", review_place((review.key,)), label)


def details(entry: ListRecord, today: datetime, secret: str, reviews: Mapping[str, review_items.ReviewItem],
            names: staff_names.StaffNames) -> str:
    item = entry.item
    sections = ''.join(f'<section><h3>{FIELD_LABELS[field]}</h3>{field_html(field, value, today, names)}</section>'
                       for field, value in (("latest_status", item.latest_status), ("done_when", item.done_when)))
    links = [f'<li><a data-record-jump href="{html.escape(item_link(entry.keeper, link.number), quote=True)}">#{link.number}</a>'
             + ' — ' + html.escape(link.condition) + '</li>' for link in item.blocked_by]
    links.extend(f'<li>Related: <a data-record-jump href="{html.escape(item_link(entry.keeper, number), quote=True)}">#{number}</a></li>'
                 for number in item.related_to)
    if links:
        sections += '<section><h3>Links</h3><ul>' + ''.join(links) + '</ul></section>'
    if item.state == the_list.ItemState.CLOSED:
        sections += (f'<section><h3>{FIELD_LABELS["closing_words"]}</h3>'
                     + field_html("closing_words", item.closing_words, today, names) + '</section>')
    if item.reviews:
        cards = (review_card(identity, reviews) for identity in item.reviews)
        sections += '<p>' + review_summary(item, reviews, today) + '</p><p>' + ' · '.join(cards) + '</p>'
    sections += ''.join(page_question.open_question_html(question) if question.answer is None
                        else page_question.answered_question_html(question) for question in item.questions)
    # The log appends entries in storage order. Each index stays stable when new entries arrive.
    log = ''.join(reversed([log_entry_html(change, index, today, names) for index, change in enumerate(item.log)
                            if not the_list.is_page_request_receipt(change)]))
    log_id = html.escape('log-' + entry.key.replace('/', '-'), quote=True)
    return ('<div class="list-panel"><div>' + sections + feedback_form(entry, secret)
            + '</div><section class="list-log-panel"><h3>Log · newest first</h3>'
            + f'<div class="list-log-scroll" id="{log_id}" tabindex="0" role="region" aria-label="Log · newest first">'
            + '<ol class="list-log">' + log + '</ol></div></section></div>')


def item_rows(entry: ListRecord, members: staff.StaffSnapshot, today: datetime, index: int, secret: str,
              reviews: Mapping[str, review_items.ReviewItem],
              names: staff_names.StaffNames | staff_names.NameReading = staff_names.NameReading.CURRENT) -> str:
    names = staff_names.for_page(names, members)
    item = entry.item
    owner = owner_name(members, item.owner)
    producer = owner_name(members, item.producer)
    unread = isinstance(item.unread_since, datetime)
    parked = isinstance(item.parked_until, datetime) and item.parked_until > today
    eta = item.eta.earliest if isinstance(item.eta, the_list.EtaRange) else item.eta
    eta_sort: tuple[int, float | str] = (0, eta.timestamp()) if isinstance(eta, datetime) else (1, eta_text(eta, today).casefold())
    values = (item.number, owner.casefold(), producer.casefold(), item.title.casefold(), item.state.value.casefold(),
              item.next_step.casefold(), eta_sort, item.unread_since.timestamp() if isinstance(item.unread_since, datetime) else 0)
    attributes = {"key": entry.key, "number": str(item.number), "owner": str(item.owner), "producer": item.producer,
                  "keeper-produces": str(item.producer == entry.keeper).lower(), "state": item.state.value,
                  "unread": str(unread).lower(), "parked": str(parked).lower(), "title": item.title.lower(),
                  "owner-name": owner.lower(),
                  "order": str(index), "sort-values": json.dumps(values, ensure_ascii=False)}
    attrs = ''.join(f' data-{key}="{html.escape(value, quote=True)}"' for key, value in attributes.items())
    panel = f'list-detail-{index}'
    number = (f'<button type="button" class="list-toggle" aria-expanded="false" aria-controls="{panel}" '
              + f'aria-label="Show item {item.number}"><span class="list-chevron" aria-hidden="true">›</span>{item.number}</button>')
    title = '<strong>' + html.escape(item.title) + '</strong>'
    if item.unit:
        title += '<small>' + field_html("unit", item.unit, today, names) + '</small>'
    tone = {the_list.ItemState.WAITING_FOR_NATE: "warning", the_list.ItemState.BLOCKED: "error",
            the_list.ItemState.WORKING: "accent", the_list.ItemState.DELIVERED: "room",
            the_list.ItemState.DONE: "room"}.get(item.state, "muted")
    state = f'<span class="list-state {tone}">' + field_html("state", item.state, today, names) + '</span>'
    if item.state != the_list.ItemState.CLOSED:
        if isinstance(item.next_check, datetime):
            label = "Check overdue " if item.next_check < today else "Check "
            state += '<small>' + label + field_html("next_check", item.next_check, today, names) + '</small>'
        elif isinstance(item.next_check, the_list.CheckCondition):
            state += '<small>Check: ' + field_html("next_check", item.next_check, today, names) + '</small>'
    if parked and isinstance(item.parked_until, datetime):
        state += '<small data-parked-note>Parked until ' + field_html("parked_until", item.parked_until, today, names) + '</small>'
    if item.reviews:
        state += '<small>' + review_summary(item, reviews, today) + '</small>'
    update = '<span class="list-dot" role="img" aria-label="Unread update"></span>' if unread else '<span aria-label="No unread update">—</span>'
    sent = the_list.sent_to_morty(item)
    if isinstance(sent, datetime):
        feedback = tuple(the_list.feedback_requests(item).values())[-1]
        notice = "sent to " + names.html("morty")
        if feedback.state == "alerted":
            notice = ("not sent; " + names.html(feedback.alerted_to) + " told" if feedback.alerted_to
                      else "not sent; no showrunner for alert")
        update += '<small>' + notice + ", " + html.escape(local_time(sent, today)) + '</small>'
    cells = (number, field_html("owner", item.owner, today, names), field_html("producer", item.producer, today, names),
             title, state, field_html("next_step", item.next_step, today, names), field_html("eta", item.eta, today, names), update)
    row = ''.join(f'<td' + (' class="record-id"' if label == '#' else '')
                  + f' data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    hidden = ' hidden' if parked or item.state in (the_list.ItemState.DONE, the_list.ItemState.CLOSED) else ''
    return (f'<tbody data-list-item{attrs}{hidden}><tr class="list-row">' + row
            + f'</tr><tr data-detail id="{panel}" hidden><td colspan="{len(HEADERS)}">' + details(entry, today, secret, reviews, names) + '</td></tr></tbody>')


@dataclass(frozen=True)
class ListFeedbackDelivery:
    keeper: staff.StaffId
    identity: str
    request: str


@dataclass(frozen=True)
class ListQuestionDelivery:
    delivery: change_page.MessageDelivery
    status: change_page.DeliveryStatus


class SavedActionState(TypedDict):
    unread: str
    unread_sort: float
    item_state: str
    parked: str
    parked_until: str


def saved_action_state(keeper: str, identity: str) -> SavedActionState:
    item = the_list.load(staff.StaffId(keeper), identity)
    zone = user_zone()
    today = the_list.now()
    parked_until = (the_list.parking_date(item.parked_until, zone)
                    if isinstance(item.parked_until, datetime) and item.parked_until > today else "")
    unread = item.unread_since
    return {"unread": str(isinstance(unread, datetime)).lower(),
            "unread_sort": unread.timestamp() if isinstance(unread, datetime) else 0,
            "item_state": item.state.value,
            "parked": str(bool(parked_until)).lower(), "parked_until": parked_until}


def _mark_seen_update_read(keeper: staff.StaffId, item: the_list.ListItem,
                           update: the_list.UnreadUpdate | the_list.NoUnreadUpdate) -> None:
    if isinstance(update, the_list.UnreadUpdate) and update == the_list.unread_update(item):
        try:
            _ = the_list.mark_read(keeper, item.id, source=the_list.PAGE_SOURCE, update=update)
        except (OSError, the_list.ListError):
            pass


def submit(root: Path, body: str, status: change_page.DeliveryStatus | None = None
           ) -> ListFeedbackDelivery | ListQuestionDelivery | change_page.NoMessageDelivery:
    """Accept signed item actions and save their notices for morty."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"keeper", "id", "request", "token", "action", "words", "until"} or any(len(values) != 1 for values in fields.values()):
        raise question_record.AnswerRefused("use the form on this page")
    keeper, identity, request = fields["keeper"][0], fields["id"][0], fields["request"][0]
    if re.fullmatch(r"L[1-9]\d*", identity) is None:
        raise question_record.AnswerRefused("the List request does not match this page")
    prefix = ListFormRequestPrefix.parse(request)
    expected = request_token(root, keeper, identity, prefix.format())
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise question_record.AnswerRefused("the List token does not match this page")
    morty = staff.snapshot().by_id(staff.StaffId(keeper))
    if morty is None or morty.kind != "producer" or morty.name != "morty":
        raise question_record.AnswerRefused("the item has no morty producer")
    action, words, until = fields["action"][0], fields["words"][0].strip(), fields["until"][0]
    if action == "read":
        _ = the_list.mark_read(morty.id, identity, source=the_list.PAGE_SOURCE,
                               page_request=the_list.PageRequest(request, "read", words))
        return change_page.NoMessageDelivery()
    item = the_list.load(morty.id, identity)
    if action == "question":
        status = change_page.DeliveryStatus(root) if status is None else status
        question = the_list.ask_question(morty.id, identity, the_list.PageRequest(request, "question", words), status)
        if isinstance(question, change_page.MessageDelivery):
            _mark_seen_update_read(morty.id, item, prefix.seen_update)
            return ListQuestionDelivery(question, status)
        return question
    if action == "remove":
        notice = the_list.PageItemNotice(request, f"List page: nate removed item {item.number}.", "remove", words)
        changed = the_list.close(morty.id, identity, "Removed by nate on the List page.", source=the_list.PAGE_SOURCE, notice=notice)
    elif action == "park":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until) is None:
            raise question_record.AnswerRefused("enter a parking date")
        notice = the_list.PageItemNotice(request, f"List page: nate parked item {item.number} until {until}.", "park", words)
        changed = the_list.park(morty.id, identity, the_list.time_value(until), source=the_list.PAGE_SOURCE, notice=notice)
    elif action == "tell" and words and len(words) <= 10000:
        saved = the_list.save_feedback(morty.id, identity,
                                       the_list.PageFeedback(request, words, read_update=prefix.seen_update))
        _mark_seen_update_read(morty.id, item, saved.read_update)
        return ListFeedbackDelivery(morty.id, identity, request) if saved.state == "pending" else change_page.NoMessageDelivery()
    else:
        raise question_record.AnswerRefused("enter words for morty")
    saved_notice = the_list.feedback_requests(changed).get(request)
    if saved_notice is not None and saved_notice.state == "pending":
        return ListFeedbackDelivery(morty.id, identity, request)
    return change_page.NoMessageDelivery()


def alert_showrunners(item: the_list.ListItem, members: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """Find the first showrunner above the item owner without session or document reads."""
    owner = members.by_id(item.owner) if not isinstance(item.owner, the_list.UnassignedOwner) else None
    visited: set[staff.StaffId] = set()
    while owner is not None and owner.id not in visited:
        if owner.kind == "showrunner":
            return (owner,)
        visited.add(owner.id)
        owner = members.parent_of(owner)
    return ()


def deliver(delivery: ListFeedbackDelivery | ListQuestionDelivery | change_page.NoMessageDelivery) -> None:
    """Send saved List words with the shared retry and alert helpers."""
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
        failures = change_page.send_with_retries(change_page.SENDER, (delivery.keeper,), message,
                                                 tries, settings.number("delivery_pause_seconds"))
        if not failures:
            return replace(feedback, state="sent")
        owners = alert_showrunners(item, staff.snapshot())
        source = the_list.folder(delivery.keeper) / f"{item.id}.md"
        reached = change_page.alert_delivery_failures(failures, tries, source=source, row=item.id, message=message,
            recipients=tuple(owner.id for owner in owners), title="List feedback needs attention")
        return replace(feedback, state="alerted", alerted_to=", ".join(owner.name for owner in owners)) if reached else feedback
    the_list.deliver_feedback(delivery.keeper, delivery.identity, delivery.request, send)


def recover_pending() -> None:
    """Retry each pending request once at server start."""
    for entry in records():
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
    review_link_attr = ' data-list-review-link=""' if page == "review" else ''
    return f'<a href="{html.escape(path, quote=True)}" target="_top"{review_link_attr}>{text}</a>'


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


def staff_chips(labels: Mapping[str, str], names: staff_names.StaffNames) -> list[record_table.Chip]:
    """Build one chip for each staff ID, in name order."""
    return [record_table.Chip(value, names.html(value, label))
            for value, label in sorted(labels.items(), key=lambda pair: (pair[1].casefold(), pair[0]))]


def render(root: Path, *, include_counts: bool = True) -> bytes:
    started, read_at = time.perf_counter(), time.time()
    members = staff.snapshot()
    names = staff_names.StaffNames(members)
    items = records()
    reviews = ({review.key: review for review in review_items.read(root, members=members)}
               if any(entry.item.reviews for entry in items) else {})
    today = datetime.fromtimestamp(read_at, user_zone())
    secret = change_browser.browser_manifest(change_page.console_folder(root))[0]
    owners = staff_chips({str(entry.item.owner): owner_name(members, entry.item.owner) for entry in items}, names)
    producers = staff_chips({str(entry.item.producer): owner_name(members, entry.item.producer) for entry in items}, names)
    states = [record_table.Chip(state.value, state.value) for state in the_list.ItemState]
    shows = [record_table.Chip(value, label) for value, label in SHOWS if value != 'all']
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      + '<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    settings = conf.read("review_links")
    seconds = settings.number("page_refresh_seconds")
    log_max_height = settings.whole("list_log_max_height_rem")
    counts = counts_strip(members, names) if include_counts else ''
    content = (STYLE + page_frame.title("The List", "list")
               + f'<div data-list data-refresh-seconds="{seconds}" style="--list-log-max-height:{log_max_height}rem">'
               + '<div data-record-filters>'
               + record_table.words_box('list-search', 'Filter by number, owner, item or state')
               + record_table.chip_row('Show', shows, 'open')
               + record_table.chip_row('State', states, 'all')
               + record_table.chip_row('Owner', owners, 'all')
               + record_table.chip_row('Producer', producers, 'all')
               + '<span data-list-count role="status" aria-live="polite"></span></div>'
               + '<p data-missing-item role="status" hidden></p>'
               + '<p data-hidden-action-status role="status" hidden></p>'
               + '<div class="list-table"><table aria-label="The List"><colgroup><col style="width:6%"><col style="width:8%">'
               + '<col style="width:10%"><col style="width:17%"><col style="width:14%"><col style="width:23%"><col style="width:14%">'
               + '<col style="width:8%"></colgroup><thead><tr>' + headers + '</tr></thead>'
               + ''.join(item_rows(entry, members, today, index, secret, reviews, names) for index, entry in enumerate(items)) + '</table>'
               + '<p data-empty hidden>No items in this view.</p></div>' + counts + '</div><script>' + SCRIPT + '</script>')
    return page_frame.rendered(change_page.document(content, "The List"), started, read_at)


STYLE = record_table.STYLE + '''<style>
.list-table td[data-label="Next step"]>:first-child{margin-top:0}.list-table td[data-label="Next step"]>:last-child{margin-bottom:0}
.list-tell{display:flex;justify-content:flex-end;align-items:center;gap:10px;flex-wrap:wrap}
.list-tell [value="tell"]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.list-tell button:disabled{opacity:.45;cursor:default}.list-tell [data-send-status]{margin-left:0}
.list-log dl{display:grid;grid-template-columns:min(7rem,42%) minmax(0,1fr);gap:2px 12px;margin:4px 0 0}
.list-log dt{color:var(--ink-3)}.list-log dd{margin:0;overflow-wrap:anywhere}
.list-log dd>:last-child{margin-bottom:0}
.list-log dl li{display:list-item;padding:0;border:0}
.page-question p{white-space:pre-wrap;overflow-wrap:anywhere}
.list-table td[data-label="State"]{color:var(--ink)}
.list-table td[data-label="Producer"],.list-table td[data-label="ETA"]{color:var(--ink-2)}
@media(max-width:960px){
.list-table .list-row td[data-label="Producer"]{font-size:inherit}.list-table .list-row td[data-label="Item"]{font-size:17px}
.list-table .list-row td[data-label="State"]::before{content:none}
.list-table [data-keeper-produces=true] .list-row td[data-label="Producer"]{display:none}
.list-table .list-row td[data-label="Producer"]::before,.list-table .list-row td[data-label="ETA"]::before{
content:attr(data-label) ': ';color:var(--ink-3)}
}
</style>'''

SCRIPT = page_frame.REFRESH_SCRIPT + record_table.SCRIPT + r'''(() => {
let content=document.querySelector('[data-list]');
let saving=0;
const drafts=new Map();
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
 return [data.number,data.ownerName,data.title,data.state,data.parked==='true' ? 'Parked' : '']
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
  if(!read){const button=document.createElement('button');button.name='action';button.value='read';
   button.textContent='Mark read';button.disabled=Boolean(form.dataset.sending);form.querySelector('.list-actions').prepend(button);}
 }else{
  dot?.remove();read?.remove();
  if(!update.querySelector('[aria-label="No unread update"]')){const empty=document.createElement('span');
   empty.setAttribute('aria-label','No unread update');empty.textContent='—';update.prepend(empty);}
 }
}
const tableView=createRecordTable({content:()=>content,
 shows:['open','needs-me','unread','done','parked','closed','all'],
 filters:['state','producer'],
 beforeApply:rememberLogPositions,
 applied:restoreLogPositions,
 missingItem:key=>{
  const number=key.match(/\/L([1-9]\d*)$/)?.[1];
  return number ? 'Item #'+number+' is not on the List' : '';
 },
 filterValues:(data,search)=>{
  if(!matchesWords(data,search)) return null;
  const show=[],parked=data.parked==='true';
  if(!parked && !['Done','Closed'].includes(data.state)) show.push('open');
  if(!parked && data.state==='Waiting for nate') show.push('needs-me');
  if(data.unread==='true') show.push('unread');
  if(data.state==='Done') show.push('done');
  if(parked) show.push('parked');
  if(data.state==='Closed') show.push('closed');
  return {show,state:[data.state],owner:[data.owner],producer:[data.producer]};
 },
 counts:(items,count)=>count+' of '+items.length+' items shown · '+
  items.filter(item=>item.dataset.state==='Waiting for nate' && item.dataset.parked!=='true').length+' need you · '+
  items.filter(item=>item.dataset.unread==='true').length+' unread',
});
function bindForms(){
 content.querySelectorAll('[data-feedback-form]').forEach(form=>{
  const item=form.closest('[data-list-item]'),key=item.dataset.key;
  const notice=form.querySelector('[data-send-status]');
  notice.textContent=tableView.actionNotice(item);
  const confirmed={state:item.dataset.state,parked:item.dataset.parked,
   unread:item.dataset.unread==='true',unreadSort:JSON.parse(item.dataset.sortValues)[7]};
  const syncConfirmed=()=>{
   item.dataset.state=confirmed.state;
   item.dataset.parked=confirmed.parked;
   syncUnread(item,form,confirmed.unread,confirmed.unreadSort);
  };
  const words=form.elements.words,until=form.elements.until;
  const tell=form.querySelector('[value="tell"]'),ask=form.querySelector('[value="question"]');
  const syncTell=()=>{ask.disabled=tell.disabled=Boolean(form.dataset.sending) || !words.value.trim();};
  // Keep the signed update from the rendered row until the row refreshes.
  const signed={request:form.elements.request.value,token:form.elements.token.value};
  let fresh=signed;
  const newRequest=()=>({
   request:signed.request+'.'+Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join(''),
   token:signed.token
  });
  const draft=drafts.get(key);if(draft){words.value=draft.words;until.value=draft.until;}
  const unconfirmed=draft?.unconfirmed ?? new Set();
  const matching=action=>[...unconfirmed].findLast(attempt=>attempt.words===words.value && attempt.until===until.value &&
   (action===undefined || attempt.action===action));
  const identity=request=>{form.elements.request.value=request.request;form.elements.token.value=request.token;};
  const retainDraft=()=>{drafts.set(key,{words:words.value,until:until.value,unconfirmed});identity(matching() ?? fresh);};
  const retain=()=>{retainDraft();syncTell();};
  retain();
  words.addEventListener('input',retain);until.addEventListener('input',retain);
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(form.dataset.sending) return;
   const action=event.submitter?.value || 'tell';
   const usesWords=action==='tell' || action==='question';
   if(usesWords && !words.value.trim()) return;
   if(action==='park' && !until.value){form.querySelector('[data-send-status]').textContent='Choose a parking date.';return;}
   const retry=matching(action);
   const attempt=retry ?? {...fresh,action,words:words.value,until:until.value};
   identity(attempt);
   const data=new URLSearchParams(new FormData(form));data.set('action',action);
   if(!retry){unconfirmed.add(attempt);fresh=newRequest();}
   retainDraft();
   form.dataset.sending='true';saving++;pageRefresh.abandon();
   let buttons=[...item.querySelectorAll('button')],saved=false,refused=false;
   buttons.forEach(button=>button.disabled=true);notice.textContent=action==='read' ? 'Saving…' : 'Sending…';
   const clearsUnread=action==='read' || usesWords,unreadBefore=item.dataset.unread,wasVisible=!item.hidden;
   if(action==='remove'){
    item.dataset.state='Closed';
    syncUnread(item,form,false,0);
   }
   if(action==='park') item.dataset.parked='true';
   if(clearsUnread) item.dataset.unread='false';
   const submission=tableView.startAction(item);
   const hidUnread=clearsUnread && wasVisible && item.hidden;
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    refused=!response.ok && (action!=='question' || response.status<500);
    const result=await response.json();
    if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
    saved=true;
    if(typeof result.item_state==='string') confirmed.state=result.item_state;
    const closed=confirmed.state==='Closed',unread=!closed && result.unread==='true';
    notice.textContent=action==='remove' ? 'Removed' : action==='park' ? 'Parked' : action==='read' ? 'Marked read' :
     action==='question' ? (unread ? 'Question saved; update remains unread' : 'Question saved') :
     action==='tell' && unread ? 'sent to morty; update remains unread' : 'sent to morty';
    if(usesWords && words.value===data.get('words')) words.value='';
    if(['true','false'].includes(result.parked)) confirmed.parked=result.parked;
    if(closed || ['true','false'].includes(result.unread)){
     confirmed.unread=unread;
     confirmed.unreadSort=unread ? (result.unread_sort ?? confirmed.unreadSort) : 0;
    }
    syncConfirmed();
    const state=item.querySelector('.list-state');state.textContent=item.dataset.state;
    state.className='list-state '+({'Waiting for nate':'warning',Blocked:'error',Working:'accent',Delivered:'room',Done:'room'}[item.dataset.state] || 'muted');
    const sortValues=JSON.parse(item.dataset.sortValues);sortValues[3]=item.dataset.state.toLowerCase();item.dataset.sortValues=JSON.stringify(sortValues);
    if(typeof result.parked_until==='string'){
     let note=item.querySelector('[data-parked-note]');
     if(result.parked_until){
      if(!note){note=document.createElement('small');note.dataset.parkedNote='';state.parentElement.append(note);}
      note.textContent='Parked until '+result.parked_until;
     }else note?.remove();
    }
    tableView.applyAction();
   }catch(error){
    const wasHidden=item.hidden;
    if(refused) syncConfirmed();
    else if(clearsUnread) item.dataset.unread=unreadBefore;
    if(refused || hidUnread) tableView.opened.add(key);
    const message=(refused ? 'Not saved: ' : 'Save not confirmed: ')+error.message;
    tableView.showActionResult(item,submission,'Item '+item.dataset.number,{kind:'failure',message});
    // The return animation now has the confirmed sort keys.
    if(refused && wasHidden && !item.hidden) tableView.applyAction();
   }
   finally{
    if(saved || refused) unconfirmed.delete(attempt);
    buttons=[...item.querySelectorAll('button')];
    delete form.dataset.sending;saving--;buttons.forEach(button=>button.disabled=false);syncTell();
    retainDraft();
    if(saved) void pageRefresh.refresh(true);
   }
  });
 });
}
async function refresh(_supersede=false,current=()=>true){
 if(saving) return;
 const response=await fetch(location.pathname,{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html');
 const fresh=page.querySelector('[data-list]');
 if(!fresh) throw new Error('The page has no list.');
 await tableView.settled();
 if(saving || !current()) return;
 const top=scrollY,focus=document.activeElement?.id;
 const field=document.activeElement,start=field?.selectionStart,end=field?.selectionEnd;
 rememberLogPositions();
 const surviving=new Set([...fresh.querySelectorAll('[data-list-item]')].map(item=>item.dataset.key));
 for(const key of logPositions.keys()) if(!surviving.has(key)) logPositions.delete(key);
 content.replaceWith(fresh);content=fresh;
 bindForms();tableView.replace();
 if(focus){const field=document.getElementById(focus);field?.focus({preventScroll:true});
  if(typeof field?.setSelectionRange==='function' && typeof start==='number' && typeof end==='number') field.setSelectionRange(start,end);}
 scrollTo({top,behavior:'instant'});return pageRefreshFacts(page);
}
const listReturnParam='list-return',listReturnsKey='console.list.review-returns';
const maxListReturns=8,maxOpenRows=128,maxRowKeyLength=128,listReturnAge=60*60*1000;
function reviewDestination(link){
 const destination=new URL(link.href),target=new URL(destination.searchParams.get('at.review'),location.href);
 const jump=new URLSearchParams(target.hash.slice(1)).get('jump');
 if(!jump) return;
 const previous=new URL(parent.location.href).searchParams.get('at.review');
 const filters=new URLSearchParams({show:'open',jump});
 if(previous){
  const place=new URL(previous,location.href);
  if(place.origin===location.origin && place.pathname===target.pathname){
   const fragment=new URLSearchParams(place.hash.slice(1));
   for(const name of ['show','owner','type','q','sort','dir','source-owner']){
    const value=fragment.get(name) ?? place.searchParams.get(name);
    if(value!==null) filters.set(name,value);
   }
  }
 }
 target.hash=filters;
 destination.searchParams.set('at.review',target.pathname+target.search+target.hash);
 link.href=destination;
}
function listReturns(){
 const raw=sessionStorage.getItem(listReturnsKey);
 if(!raw || raw.length>maxListReturns*(maxOpenRows*(maxRowKeyLength+3)+200)) return [];
 let records;
 try{records=JSON.parse(raw);}catch(_){return [];}
 if(!Array.isArray(records)) return [];
 const now=Date.now();
 return records.filter(record=>record && typeof record.id==='string' &&
  Number.isFinite(record.saved) && record.saved<=now && now-record.saved<listReturnAge &&
  Number.isFinite(record.top) && record.top>=0 &&
  Array.isArray(record.opened) && record.opened.length<=maxOpenRows &&
  record.opened.every(key=>typeof key==='string' && key.length<=maxRowKeyLength)).slice(-maxListReturns);
}
function writeListReturns(records){
 if(records.length) sessionStorage.setItem(listReturnsKey,JSON.stringify(records));
 else sessionStorage.removeItem(listReturnsKey);
}
function saveListReturn(){
 tableView.saveDeparturePlace();
 const outer=new URL(parent.location.href),prior=outer.searchParams.get(listReturnParam);
 const records=listReturns();
 const id=records.some(record=>record.id===prior) ? prior : Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
 const opened=[...tableView.opened].filter(key=>key.length<=maxRowKeyLength).slice(0,maxOpenRows);
 const retained=records.filter(record=>record.id!==id);
 writeListReturns([...retained,{id,saved:Date.now(),opened,top:scrollY}].slice(-maxListReturns));
 outer.searchParams.set(listReturnParam,id);
 if(parent!==window) outer.searchParams.set('at.list',location.pathname+location.search+location.hash);
 parent.history.replaceState(parent.history.state,'',outer);
}
function prepareReviewLink(event){
 const link=event.target.closest('a[data-list-review-link]');
 if(!link || event.defaultPrevented) return;
 try{
  reviewDestination(link);
  if(event.type==='click' && event.button===0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey)
   saveListReturn();
 }catch(_){}
}
for(const event of ['click','auxclick','contextmenu','focusin']) document.addEventListener(event,prepareReviewLink);
function listReturnForEntry(){
 const outer=new URL(parent.location.href),id=outer.searchParams.get(listReturnParam);
 if(!id) return;
 try{
  const records=listReturns();
  writeListReturns(records);
  return records.find(record=>record.id===id);
 }catch(_){}
}
function restoreListRows(record){
 const surviving=new Set([...content.querySelectorAll('[data-list-item]')].map(row=>row.dataset.key));
 tableView.opened.clear();
 for(const key of record.opened) if(surviving.has(key)) tableView.opened.add(key);
}
function restoreListScroll(top){
 requestAnimationFrame(()=>requestAnimationFrame(()=>scrollTo({top,behavior:'instant'})));
}
tableView.place();
const listReturn=parent.performance.getEntriesByType('navigation')[0]?.type==='back_forward' ? listReturnForEntry() : undefined;
if(listReturn) restoreListRows(listReturn);
tableView.bind();bindForms();tableView.apply(true,{top:listReturn?.top,dismissMissingItem:Boolean(listReturn)});
if(listReturn) restoreListScroll(listReturn.top);
addEventListener('pageshow',event=>{
 if(!event.persisted) return;
 const record=listReturnForEntry();
 if(!record) return;
 tableView.place();restoreListRows(record);
 tableView.apply(true,{top:record.top,dismissMissingItem:true});
 restoreListScroll(record.top);
});
addEventListener('popstate',tableView.restorePlace);
addEventListener('hashchange',tableView.restorePlace);
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
})();'''
