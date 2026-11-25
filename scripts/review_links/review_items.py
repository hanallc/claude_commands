"""Read the five review stores through one model of whose turn it is."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import json
import logging
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, cast, override

from scripts.production import unit_lookup, showrunner_tree
from scripts.agents.remote import local_machine
if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

from scripts.lib import urgency
from scripts.lib import user_zone as user_zone_config
from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, delivery_state, investigation, staff, update_registration
from scripts.production import issues as issue_store
from scripts.production import proposal_issue, proposals_folder, proposals_table, record_store
from scripts.production.record_store import state_root
from scripts.production.issues import Issue, PROPOSAL_TAG, source_paths
from scripts.production.proposal_issue import DeclinedDecision, InProgress, decision_answer, proposal_progress
from scripts.production.proposal_issue import REVIEW_MARK_FIELD, review_log, review_mark
from scripts.production.production_doc import production_field, Refusal
from scripts.for_you.action import ActionStore
from scripts.review_links.question_path import NoLaterTime as NoLaterTime
from scripts.review_links import question_path, efficiency_reviews, for_you, new_item_dialog, review_events, review_later, review_submissions
from scripts.review_links.review_events import ReviewLog as ReviewLog, ReviewMark


def source_stamp(path: Path) -> record_store.FileStamp:
    try:
        return record_store.file_stamp(path)
    except OSError:
        return record_store.MISSING_STAMP


class ReviewSourceStamps(list[Path]):
    """Record each source and its parent before a snapshot reads them."""
    def __init__(self) -> None:
        super().__init__()
        self.before_read: dict[Path, record_store.FileStamp] = {}
        self.notices: dict[tuple[Path, str], str] = {}
        self.folder_entries: dict[Path, tuple[Path, ...]] = {}

    def record_files(self, folder: Path, *, recursive: bool = False) -> Iterator[Path]:
        """Read each native directory once per snapshot, including missing directories."""
        if folder not in self.folder_entries:
            self.folder_entries[folder] = ()
            with guard_source(folder, self):
                try:
                    self.folder_entries[folder] = tuple(folder.iterdir())
                except FileNotFoundError:
                    pass
        for path in self.folder_entries[folder]:
            if not recursive and path.suffix != '.md':
                continue
            with guard_source(path, self):
                if recursive and not path.is_symlink() and path.is_dir():
                    yield from self.record_files(path, recursive=True)
                elif path.suffix == '.md' and path.is_file():
                    yield path

    def skipped(self, path: Path, entry: str, error: Exception) -> None:
        label = f'{path} ({entry})' if entry else str(path)
        self.notices[path, entry] = utf8_display(f'Could not load {label}.')
        logging.getLogger(__name__).warning('Review source %s: %s', label, error)

    @override
    def append(self, path: Path, /) -> None:
        super().append(path)
        for source in (path, path.parent):
            if source not in self.before_read:
                self.before_read[source] = source_stamp(source)

    @override
    def extend(self, paths: Iterable[Path], /) -> None:
        for path in paths:
            self.append(path)


@contextmanager
def guard_source(path: Path, sources: list[Path] | None, *,
                 skip: tuple[type[Exception], ...] = (), entry: str = '',
                 change_list: bool = False) -> Generator[None]:
    """Track the source and isolate read failures during Review scans."""
    if isinstance(sources, ReviewSourceStamps):
        sources.append(path)
    try:
        yield
    except Exception as error:
        source = error.path if isinstance(error, change_routing.ProductionPathUnreadable) else path
        if isinstance(sources, ReviewSourceStamps):
            sources.skipped(source, entry, error)
        elif change_list and isinstance(error, (change_review.ChangeReviewError, OSError, UnicodeError)):
            if sources is not None:
                raise ValueError(str(error)) from error
            print(f'change review: {path}: {error}', file=sys.stderr)
        elif isinstance(error, skip) or sources is None and isinstance(error, (OSError, UnicodeError)):
            label = f'{source} ({entry})' if entry else str(source)
            logging.getLogger(__name__).warning('Review source %s: %s', label, error)
        else:
            raise


def utf8_display(text: str) -> str:
    return text.encode('utf-8', errors='replace').decode('utf-8')


@contextmanager
def after_store_write(key: str) -> Generator[None]:
    """Keep a saved action when an operation after the store write fails."""
    try:
        yield
    except Exception as error:
        logging.getLogger(__name__).warning('Review saved %s: %s', key, error)


class ReviewType(StrEnum):
    QUESTION = 'question'
    CHANGE = 'change'
    INVESTIGATION = 'investigation'
    PROPOSAL = 'proposal'
    EFFICIENCY = 'efficiency'
    BUG = 'bug'
    RESPONSE = 'response'
    FOR_YOU = 'for-you'


class ReviewState(StrEnum):
    OPEN = 'Open'
    VALIDATING = 'Validating'
    WAITING = 'Waiting'
    DONE = 'Done'


REVIEW_TYPES = tuple(kind for kind in ReviewType if kind != ReviewType.FOR_YOU)
QUESTION_TYPES = (ReviewType.QUESTION, ReviewType.RESPONSE)
INVESTIGATION_TYPES = (ReviewType.INVESTIGATION, ReviewType.BUG)
REVIEW_STATES = tuple(ReviewState)
PROPOSAL_STATE_FIELDS = frozenset(("status", "stage", "unit", "style_answer", "style_answer_words", "proposed_by", "production_id", "record_type",
                                   REVIEW_MARK_FIELD))
# Approve and Decline close the item; Send feedback closes it; Ask question returns it.
DECISION_ACTIONS = ('approve', 'decline', 'feedback', 'question')
QUEUE_BY_STATE = {
    ReviewState.OPEN: question_path.Queue.NEEDS_ME,
    ReviewState.WAITING: question_path.Queue.WAITING_FOR_ANSWER,
    ReviewState.VALIDATING: question_path.Queue.VALIDATING,
    ReviewState.DONE: question_path.Queue.DONE,
}


def selected_states(show: str) -> tuple[ReviewState, ...]:
    """Select the states before a store loads complete records."""
    if show == 'all':
        return REVIEW_STATES
    if show == 'active':
        return (ReviewState.OPEN, ReviewState.VALIDATING, ReviewState.WAITING)
    if open_view(show):
        return (ReviewState.OPEN, ReviewState.VALIDATING)
    return (ReviewState(show),)


def open_view(show: str) -> bool:
    return show in ('open', 'Open', 'For you', 'for-you', 'for you')


@dataclass(frozen=True)
class ReviewTimes:
    filed: datetime
    updated: datetime
    later: datetime | NoLaterTime = NoLaterTime()


@dataclass(frozen=True)
class OpenFeedback(delivery_state.OpenQuestion):
    """The showrunner must act before this change returns to the user."""


@dataclass(frozen=True)
class CompletedFeedback(delivery_state.AnsweredQuestion):
    """The showrunner's note returns this change to the user."""


def decode_change_delivery(value: object) -> delivery_state.DeliverySlots:
    slots = delivery_state.decode_value(value)
    fields = cast(dict[str, object], value) if isinstance(value, dict) else {}
    saved = fields.get("question")
    question = slots.question
    if isinstance(saved, dict) and cast(dict[str, object], saved).get("kind") == "feedback":
        if isinstance(question, delivery_state.OpenQuestion):
            return replace(slots, question=OpenFeedback(question.question_id, question.text, question.delivery))
        if isinstance(question, delivery_state.AnsweredQuestion):
            return replace(slots, question=CompletedFeedback(question.question_id, question.text, question.answer))
    return slots


def _read_change_delivery_fields(path: Path, sources: list[Path] | None) -> object:
    if sources is not None:
        sources.append(path)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return cast(object, json.loads(content))
    except ValueError:
        if sources is not None:
            raise
        return None


def read_change_delivery(path: Path, *, sources: list[Path] | None = None) -> delivery_state.DeliverySlots:
    """Read change exchanges with their request type."""
    return decode_change_delivery(_read_change_delivery_fields(path, sources))


def validation_recipients(fields: object) -> tuple[staff.StaffId, ...]:
    """Read the recipients saved with the validation ID."""
    saved = cast(dict[str, object], fields).get('validation') if isinstance(fields, dict) else None
    return delivery_state.decode_recipients(cast(dict[str, object], saved).get('recipients')) if isinstance(
        saved, dict) else ()


def delivery_time(fields: object, field: str) -> datetime | None:
    """Read a saved time from the delivery record."""
    saved = cast(dict[str, object], fields).get(field) if isinstance(fields, dict) else None
    try:
        return datetime.fromisoformat(saved) if isinstance(saved, str) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ChangeSource:
    path: Path
    row: dict[str, str]
    assignment: change_routing.ChangeAssignment
    slots: delivery_state.DeliverySlots
    handed_at: datetime | None = None
    validators: tuple[staff.StaffId, ...] = ()


@dataclass(frozen=True)
class QuestionSource:
    record: update_registration.QuestionRecord


@dataclass(frozen=True)
class InvestigationSource:
    record: investigation.Investigation
    path: Path


@dataclass(frozen=True)
class ProposalSource:
    record: Issue


@dataclass(frozen=True)
class UnownedQuestionSource:
    record: update_registration.QuestionRecord


@dataclass(frozen=True)
class ProposalLedgerSource:
    path: Path


@dataclass(frozen=True)
class NoEfficiencyLedger:
    """The production has no efficiency ledger."""


@dataclass(frozen=True)
class EfficiencySource:
    path: Path
    line: str


@dataclass(frozen=True)
class ForYouSource:
    row: for_you.ForYouRow


type ReviewSource = (ChangeSource | QuestionSource | UnownedQuestionSource | InvestigationSource
                     | ProposalSource | ProposalLedgerSource | EfficiencySource | ForYouSource)


class AnswerTimeFromLog(StrEnum):
    USE_LOG = 'use log'


@dataclass(frozen=True)
class ReviewItem:
    key: str
    id: str
    type: ReviewType
    owner: staff.StaffId
    source_unit: staff.StaffId
    title: str
    state: ReviewState
    times: ReviewTimes
    log: tuple[ReviewLog, ...]
    actions: tuple[str, ...]
    source: ReviewSource
    urgency: urgency.Urgency = urgency.Urgency()
    after: str = ''
    queue: str = ''
    queue_key: str = ''
    queue_after: str = ''
    value: tuple[float, ...] = ()
    text: str = ''
    waiting_why: str = ''
    response: review_submissions.QuestionResponse | None = None
    answer_time: question_path.AnswerTime | AnswerTimeFromLog = AnswerTimeFromLog.USE_LOG
    body_answer_events: frozenset[int] = frozenset()

    def for_you(self, now: datetime) -> bool:
        """Count Open records whose next action belongs to the user."""
        return not on_list(self) and self.queue_at(now) is question_path.Queue.NEEDS_ME

    def hidden_until(self, now: datetime) -> bool:
        return self.queue_at(now) is question_path.Queue.LATER

    def queue_at(self, now: datetime) -> question_path.Queue:
        return question_path.queue(QUEUE_BY_STATE[self.state], unread_answer=self.unread_answer,
                                   later=question_path.later_active(self.times.later, now))

    @property
    def unread_answer(self) -> bool:
        if self.state not in (ReviewState.OPEN, ReviewState.VALIDATING):
            return False
        time = self.answer_time
        if time is AnswerTimeFromLog.USE_LOG:
            time = question_path.answer_time(question_path.NO_ANSWER_TIME, self.log)
        return question_path.has_unread_answer(time, self.log)

    @property
    def answer_read_event(self) -> str:
        if isinstance(self.source, ChangeSource):
            answer = self.source.slots.question
            if isinstance(answer, delivery_state.AnsweredQuestion):
                return 'Marked read: ' + answer.question_id
        return 'Marked read'


def source_path(item: ReviewItem) -> Path:
    """Return the path to watch if this record cannot be projected."""
    source = item.source
    if isinstance(source, (ProposalSource, QuestionSource, UnownedQuestionSource)):
        return source.record.path
    if isinstance(source, ForYouSource):
        return ActionStore().folder
    return source.path


def submission_origin(item: ReviewItem) -> new_item_dialog.SubmissionOrigin:
    """Read the origin from the retained native record."""
    source = item.source
    if isinstance(source, (QuestionSource, UnownedQuestionSource, ProposalSource, InvestigationSource)):
        return review_submissions.native_origin(source.record)
    return new_item_dialog.NoReviewSubmission()


@dataclass(frozen=True)
class ExactReviewRecord:
    """Select one record and its production sources."""

    path: Path
    key: str
    owner: staff.StaffId
    prerequisite: Path | None = None

    def includes(self, path: Path, key: str = '') -> bool:
        return self.path == path and (not key or self.key == key)


@dataclass(frozen=True)
class AllReviewRecords:
    """Select each record in the requested stores."""

    def includes(self, _path: Path, _key: str = '') -> bool:
        return True


type ReviewSelection = ExactReviewRecord | AllReviewRecords
ALL_RECORDS = AllReviewRecords()


def moment(value: str, zone: ZoneInfo | None = None) -> datetime:
    """Read stored times in the user's zone when the store has no zone."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone or user_zone())


def aware(value: datetime, zone: ZoneInfo) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=zone)


def dated_review_log(text: str, zone: ZoneInfo) -> tuple[ReviewLog, ...]:
    return tuple(replace(event, time=aware(event.time, zone)) if event.time.tzinfo is None else event
                 for event in review_events.decode(text))


def normalize_times(item: ReviewItem, zone: ZoneInfo) -> ReviewItem:
    times, rank = item.times, item.urgency
    if (times.filed.tzinfo is not None and times.updated.tzinfo is not None
            and (not isinstance(times.later, datetime) or times.later.tzinfo is not None)
            and (rank.due is None or rank.due.tzinfo is not None)
            and (rank.marked is None or rank.marked.tzinfo is not None)
            and all(event.time.tzinfo is not None for event in item.log)):
        return item
    return replace(item, times=ReviewTimes(aware(times.filed, zone), aware(times.updated, zone),
        aware(times.later, zone) if isinstance(times.later, datetime) else times.later),
        log=tuple(replace(event, time=aware(event.time, zone)) for event in item.log),
        urgency=replace(rank, due=None if rank.due is None else aware(rank.due, zone),
                        marked=None if rank.marked is None else aware(rank.marked, zone)))


def owner_for(identity: staff.StaffId, members: staff.StaffSnapshot) -> staff.StaffId:
    """Use the first showrunner or producer above the source unit."""
    member = members.by_id(identity)
    while member is not None:
        if member.kind in ('showrunner', 'producer'):
            return member.id
        member = members.parent_of(member)
    return identity


def name(identity: staff.StaffId, members: staff.StaffSnapshot) -> str:
    member = members.by_id(identity)
    return member.name if member is not None else str(identity)


def for_you_item(row: for_you.ForYouRow, members: staff.StaffSnapshot, *, zone: ZoneInfo | None = None) -> ReviewItem:
    """Place one action in the shared Review table."""
    requester = row.requester_reference or row.requested_by
    member = members.by_member_reference(requester) if requester else None
    owner = member.id if member is not None else staff.StaffId(row.requested_by or 'Not recorded')
    state = {'open': ReviewState.OPEN, 'done': ReviewState.DONE,
             'withdrawn': ReviewState.DONE, 'unknown': ReviewState.WAITING}[row.status]
    zone = zone or user_zone()
    filed = moment(row.created_at, zone) if row.created_at else datetime.fromtimestamp(0, zone)
    updated = moment(row.updated_at, zone) if row.updated_at else filed
    actions = ('done',) if row.status == 'open' and row.completion == 'manual' else ()
    identity = row.key[:15] if row.kind == 'action' else row.key
    return ReviewItem(row.key, identity, ReviewType.FOR_YOU, owner, owner, row.title, state,
                      ReviewTimes(filed, updated), (), actions, ForYouSource(row))


def change_state(row: dict[str, str], slots: delivery_state.DeliverySlots) -> ReviewState:
    if row.get('Verdict'):
        return ReviewState.DONE
    if slots.validation_requested:
        return ReviewState.VALIDATING
    return ReviewState.OPEN if slots.waits_for_user(False) else ReviewState.WAITING


def question_state(record: update_registration.QuestionRecord) -> ReviewState:
    if record.status == 'closed':
        return ReviewState.DONE
    return ReviewState.WAITING if 'followup' in record.item else ReviewState.OPEN


def investigation_state(item: investigation.Investigation) -> ReviewState:
    return {investigation.InvestigationState.OPEN: ReviewState.WAITING,
            investigation.InvestigationState.ANSWER_READY: ReviewState.OPEN,
            investigation.InvestigationState.READ: ReviewState.DONE}[item.state]


def change_delivery(root: Path, path: Path, row: str) -> delivery_state.DeliverySlots:
    try:
        return read_change_delivery(change_routing.delivery_path(root, path, row))
    except OSError:
        return delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())


def validation_log_label(words: str, validator: str) -> str:
    """Use plain labels for the active validation and its exchange."""
    match words.partition(':')[0].casefold():
        case 'validate':
            return f'Asked {validator or "the showrunner"} to validate'
        case 'question':
            return 'Asked a question about validation'
        case 'answer':
            return 'Answered the question'
        case _:
            return words


def change_item(path: Path, row: dict[str, str], assignment: change_routing.ChangeAssignment,
                members: staff.StaffSnapshot, queue: str, current: Path,
                slots: delivery_state.DeliverySlots, *, zone: ZoneInfo, handed_at: datetime | None = None,
                validators: tuple[staff.StaffId, ...] = (),
                answered_at: question_path.AnswerTime = question_path.NO_ANSWER_TIME,
                sources: list[Path] | None = None) -> ReviewItem:
    """Project a change once for the page, counts and queue."""
    prefix = path.stem.removeprefix(change_review.PREFIX)
    routed = assignment.owner
    owner = next((member for member in members.rows if member.name == routed.tab), None)
    routed_id = owner.id if owner is not None else staff.StaffId(routed.tab)
    state = change_state(row, slots)
    identity = (validators[0] if validators else staff.StaffId('')) if state == ReviewState.VALIDATING else routed_id
    source = staff.StaffId(row.get('Producer', '') or routed_id)
    entry = change_review.row_entries([row], sources=sources)[0]
    if handed_at is not None:
        handed_at = aware(handed_at, zone)
    if isinstance(answered_at, datetime):
        answered_at = aware(answered_at, zone)
    filed = entry.since.replace(tzinfo=zone) if entry.since is not None else datetime(1970, 1, 1, tzinfo=zone)
    logs = [ReviewLog(filed, name(source, members), 'Filed for your check')]
    saved_log = dated_review_log(row.get('Review log', ''), zone)
    words = {entry.words.casefold() for entry in saved_log}
    validating = state == ReviewState.VALIDATING
    validator_name = name(identity, members) if validating else ''
    validation_at = next((index for index in range(len(saved_log)-1, -1, -1)
                          if saved_log[index].words.partition(':')[0] == 'validate'), None)
    if slots.validation_requested and validation_at is None:
        logs.append(ReviewLog(filed, 'nate', validation_log_label('validate', validator_name)))
    request = slots.question
    status_time = question_path.answer_time(answered_at, saved_log)
    question_at = -1
    answer_at = -1
    validation_exchanges: set[str] = set()
    body_answer_events: set[int] = set()
    if isinstance(request, (delivery_state.OpenQuestion, delivery_state.AnsweredQuestion)):
        feedback = isinstance(request, (OpenFeedback, CompletedFeedback))
        prompt = ('feedback: ' if feedback else 'Question: ') + request.text
        question_at = next((index for index, event in enumerate(saved_log)
                            if event.words.casefold().startswith('feedback: ' if feedback else 'question: ')
                            and event.feedback_request_id == request.question_id), -1)
        # Keep the legacy display when the log has no exchange ID.
        if feedback:
            legacy_prompt = any(not event.feedback_request_id and event.matches_feedback(request.question_id, request.text)
                                for event in saved_log)
        else:
            legacy_prompt = any(not event.feedback_request_id and event.words.casefold() == prompt.casefold()
                                for event in saved_log)
            validation_exchanges.add(prompt.casefold())
        if question_at < 0 and not legacy_prompt:
            logs.append(ReviewLog(filed, 'nate', validation_log_label(prompt, validator_name)
                                  if validating and not feedback else prompt, request.question_id))

        if isinstance(request, delivery_state.AnsweredQuestion):
            answer = 'Answer: ' + request.answer
            answer_at = next((index for index in range(len(saved_log) - 1, -1, -1)
                              if saved_log[index].words.startswith('Answer: ') and saved_log[index].feedback_request_id
                              in (request.question_id, question_path.answer_log_id(request.question_id))), -1)
            legacy_answer = any(not event.feedback_request_id and event.words.casefold() == answer.casefold()
                                for event in saved_log)
            if not feedback:
                validation_exchanges.add(answer.casefold())
            if answer_at < 0 and not legacy_answer:
                if not validating or not feedback:
                    body_answer_events.add(len(logs))
                logs.append(ReviewLog(answered_at if isinstance(answered_at, datetime) else filed,
                                      name(identity, members), validation_log_label(answer, validator_name)
                                      if validating and not feedback else answer,
                                      question_path.answer_log_id(request.question_id)))
    if validating and isinstance(status_time, datetime):
        boundary_times = [] if handed_at is None else [handed_at]
        if validation_at is not None:
            boundary_times.append(saved_log[validation_at].time)
        if question_at >= 0:
            boundary_times.append(saved_log[question_at].time)
        elif isinstance(request, delivery_state.AnsweredQuestion):
            boundary_times.extend(event.time for event in saved_log if not event.feedback_request_id
                                  and event.words.casefold().startswith('question: '))
        if isinstance(request, delivery_state.OpenQuestion) or any(
                status_time.timestamp() <= stamp.timestamp() for stamp in boundary_times):
            status_time = question_path.NO_ANSWER_TIME
    if row.get('Verdict') and row['Verdict'].casefold() not in words and not any(
            entry.words.startswith('feedback sent:') for entry in saved_log):
        logs.append(ReviewLog(filed, 'nate', row['Verdict']))
    for index, event in enumerate(saved_log):
        if (index == answer_at and isinstance(request, delivery_state.AnsweredQuestion)
                and event.words == 'Answer: ' + request.answer and (not validating or not isinstance(request, CompletedFeedback))):
            body_answer_events.add(len(logs))
        legacy_validation = not event.feedback_request_id and (
            index >= validation_at if validation_at is not None else event.words.casefold() in validation_exchanges)
        during_validation = index in (validation_at, question_at, answer_at) or legacy_validation
        logs.append(replace(event, words=validation_log_label(event.words, validator_name))
                    if validating and during_validation else event)
    shown = change_review.display_id(row, routed.tab)
    after = entry.after if '/' in entry.after else prefix+'/'+entry.after if entry.after else ''
    local = path.absolute() == current.absolute()
    return ReviewItem(prefix+'/'+row['#'], shown, ReviewType.CHANGE, identity, source,
        row['Change'], state, ReviewTimes(filed, max(event.time for event in logs)), tuple(logs),
        ('works', 'not works', 'validate', 'feedback', 'question') if state == ReviewState.OPEN
        else ('question',) if state == ReviewState.VALIDATING and identity else (),
        ChangeSource(path, row, assignment, slots, handed_at, validators), entry.urgency, after, queue,
        row['#'] if local else prefix+'/'+row['#'], change_review.prerequisite_key(entry.after, path, current),
        entry.value, change_review.describe(row | {'#':shown},record=members),
        review_events.waiting_why(slots.waiting_why), answer_time=status_time,
        body_answer_events=frozenset(body_answer_events))


def change_queue_name(doc: Path, members: staff.StaffSnapshot, current: Path, below: bool = False) -> str:
    owner = unit_lookup.doc_owner(doc, members)
    queue = change_review.queue(current)
    if owner.name != showrunner_tree.root_of(owner.name, record=members):
        queue += '-'+owner.name
    return urgency.scope_queue(queue, below)


def change_items(root: Path, members: staff.StaffSnapshot, *, zone: ZoneInfo, document: Path | None = None,
                 below: bool = False, sources: list[Path] | None = None,
                 states: tuple[ReviewState, ...] = REVIEW_STATES,
                 owner_options: dict[staff.StaffId, str] | None = None,
                 selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    paths: list[Path]
    folder = change_review.lists_directory() if document is None else change_review.lists_directory(sources=sources)
    scope_queue: tuple[str, Path] | None = None
    delivery_names: set[str] | None = None
    if document is not None:
        current = change_review.scope_list(document, record=members, sources=sources)
        queue = change_queue_name(document, members, current, below)
        delivery_names = {path.name for path in source_paths(root, '.review-link-delivery-*', sources=sources)}
        listed = change_review.scope_lists(current, document, below, record=members, sources=sources)
        others = source_paths(folder, change_review.PREFIX+'*.md', sources=sources)
        paths = list(dict.fromkeys([*(listing.path.absolute() for listing in listed),
                                    *(path.absolute() for path in others)]))
        scope_queue = queue, current
        if sources is not None:
            sources.append(document)
    elif isinstance(selection, ExactReviewRecord):
        paths = [selection.path]
    else:
        paths = sorted(source_paths(folder, change_review.PREFIX+'*.md', sources=sources))
    snapshots: dict[Path, change_review.ReviewListSnapshot] = {}
    for path in paths:
        with guard_source(path, sources, change_list=document is not None):
            if sources is not None:
                sources.append(path)
            snapshots[path] = change_review.review_snapshot(path, include_reviewed=ReviewState.DONE in states,
                                                            collect_owners=owner_options is not None)
    documents = {member.name: member.doc.path for member in members.rows
                 if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))}
    routing = change_routing.StaffRouting(members, documents, sources=sources, lists=(
        (path.stem.removeprefix(change_review.PREFIX), snapshot.text) for path, snapshot in snapshots.items()))
    queues: dict[tuple[str, Path], tuple[str, Path]] = {}
    found: list[ReviewItem] = []
    for path, snapshot in snapshots.items():
        if owner_options is not None:
            known: set[tuple[str, str, bool]] = set()
            for row in (*snapshot.pending, *snapshot.reviewed, *snapshot.owner_rows):
                fields = (row.get('Producer', ''), row.get('Production', ''), bool(row.get('Verdict')))
                if fields in known:
                    continue
                known.add(fields)
                with guard_source(path, sources, entry=row['#']):
                    tab = routing.owner_and_production(path, row).owner.tab
                    owner = next((member for member in members.rows if member.name == tab), None)
                    identity = owner.id if owner is not None else staff.StaffId(tab)
                    owner_options[identity] = name(identity, members)
        list_items: list[ReviewItem] = []
        with guard_source(path, sources, change_list=document is not None):
            for row in (*snapshot.pending, *snapshot.reviewed):
                key = path.stem.removeprefix(change_review.PREFIX)+'/'+row['#']
                if not selection.includes(path, key):
                    continue
                delivery_path = change_routing.delivery_path(root, path, row['#'])
                with guard_source(delivery_path, sources, entry=row['#']):
                    if row.get('Verdict') and ReviewState.DONE not in states:
                        continue
                    if not row.get('Verdict') and states == (ReviewState.DONE,):
                        continue
                    fields = (_read_change_delivery_fields(delivery_path, sources)
                              if delivery_names is None or delivery_path.name in delivery_names else None)
                    slots = decode_change_delivery(fields)
                    state = change_state(row, slots)
                    if state not in states:
                        continue
                    assignment = routing.owner_and_production(path, row)
                    if scope_queue is None:
                        tab = assignment.owner.tab
                        doc = routing.production(tab)
                        queue_source = (tab, path if doc is None else doc)
                        if queue_source not in queues:
                            current = change_review.scope_list(doc, record=members, sources=sources) if doc is not None else path
                            queue = change_queue_name(doc, members, current) if doc is not None else change_review.queue(path)
                            queues[queue_source] = queue, current
                        queue, current = queues[queue_source]
                    else:
                        queue, current = scope_queue
                    recipients = validation_recipients(fields) if state == ReviewState.VALIDATING else ()
                    handed_at = delivery_time(fields, 'handed_at')
                    answered_at = delivery_time(fields, 'answered_at') or question_path.NO_ANSWER_TIME
                    list_items.append(change_item(path, row, assignment, members, queue, current, slots,
                                                  zone=zone, handed_at=handed_at, validators=recipients,
                                                  answered_at=answered_at,
                                                  sources=sources if document is not None else None))
            found.extend(list_items)
    return found


def question_item(record: update_registration.QuestionRecord, members: staff.StaffSnapshot, *,
                  unowned: bool = False, zone: ZoneInfo | None = None) -> ReviewItem:
    owner, fields, path = record.owner, record.item, record.path
    parsed = record.parsed_snapshot()
    zone = zone or user_zone()
    filed = aware(parsed.since, zone)
    entry = update_registration.ranked_question_entry(owner, fields, parsed)
    asker = staff.StaffId(fields.get('asked_by', ''))
    state = question_state(record)
    local_later = update_registration.outstanding_later(parsed.timing)
    later = aware(local_later, zone) if isinstance(local_later, datetime) else local_later
    saved_log = dated_review_log(fields.get('review_log', ''), zone)
    exchanges = fields.get('followups', [])
    asker_name = name(asker, members)
    raw_positions = fields.get('reply_log_positions', {})
    positions = {index: position for index in range(len(exchanges))
                 if 0 <= (position := raw_positions.get(str(index), -1)) < len(saved_log)
                 and saved_log[position].words.startswith('Answer: ')}
    positioned = set(positions.values())
    named_answers = {event.feedback_request_id.removeprefix('answer:'): index
                     for index, event in enumerate(saved_log)
                     if event.feedback_request_id and event.words.startswith('Answer: ') and index not in positioned}
    for index, exchange in enumerate(exchanges):
        identifier = exchange.get('question_id', '')
        if index not in positions and identifier in named_answers:
            positions[index] = named_answers[identifier]
    legacy_response = ('review_submission' in fields and 'followup' not in fields and bool(exchanges)
                       and len(exchanges) - 1 not in positions and not exchanges[-1].get('question_id'))
    linked_answers = set(positions.values())
    legacy_events = {index: event for index, event in enumerate(saved_log)
                     if index not in linked_answers and (legacy_response or not event.feedback_request_id)}
    # Keep the legacy display when an event has no link to an exchange.
    response = review_submissions.question_response(record, tuple(legacy_events.values()))
    logs = [ReviewLog(filed, asker_name, 'Asked') if response is None else ReviewLog(filed, 'you', 'Asked from New item')]
    legacy_words = {event.words.casefold() for event in legacy_events.values()}
    prompt_prefixes = ('question: ', 'followup: ')
    named_prompts = {event.feedback_request_id for event in saved_log
                     if event.words.casefold().startswith(prompt_prefixes) and event.feedback_request_id}
    body_answer_events: set[int] = set()
    body_log_positions: set[int] = set()
    for index, exchange in enumerate(exchanges):
        if (response is None or index != 0) and exchange.get('question_id', '') not in named_prompts and not any(
                (prefix + exchange['followup']).casefold() in legacy_words for prefix in prompt_prefixes):
            logs.append(ReviewLog(filed, 'nate', exchange['followup']))
        in_body = response is None or index == len(exchanges) - 1
        answer = 'Answer: ' + exchange['reply']
        if index in positions:
            position = positions[index]
            event = saved_log[position]
            if in_body and event.words == answer:
                body_log_positions.add(position)
                if response is not None:
                    response = replace(response, answered_at=event.time)
        elif answer.casefold() not in legacy_words:
            if in_body:
                body_answer_events.add(len(logs))
            stamp = exchange.get('replied_at')
            logs.append(ReviewLog(moment(stamp, zone) if stamp is not None else filed, asker_name, exchange['reply']))
    if response is not None and len(exchanges) - 1 not in positions:
        body_log_positions.update(index for index, event in legacy_events.items()
                                  if event.time == response.answered_at and event.who == owner
                                  and event.words == 'Answer: ' + response.answer)
    if isinstance(parsed.timing, update_registration.AwaitingReply) and 'followup' in fields:
        prompt = fields['followup']['text']
        encoded_prompts = {prefix + prompt.casefold() for prefix in prompt_prefixes}
        if not any(event.words.casefold() in encoded_prompts for event in saved_log):
            logs.append(ReviewLog(aware(parsed.timing.sent, zone), 'nate', prompt))
    body_answer_events.update(len(logs) + position for position in body_log_positions)
    logs.extend(saved_log)
    saved_answer_time = fields.get('answered_at')
    status_time = question_path.answer_time(
        moment(saved_answer_time, zone) if saved_answer_time is not None else question_path.NO_ANSWER_TIME, saved_log)
    source = UnownedQuestionSource(record) if unowned else QuestionSource(record)
    key = question_record_key(record)
    review_id = fields.get('review_id', path.stem)
    identity = name(owner, members)+'/'+review_id if review_id else ''
    opened = update_registration.OpenItem(name(owner, members), parsed, name(asker, members) if asker else '')
    kind = ReviewType.QUESTION if response is None else ReviewType.RESPONSE
    actions = ('answer', 'feedback', 'question') if response is None else ('read', 'question')
    return ReviewItem(key, identity, kind, owner, asker, fields['text'], state,
        ReviewTimes(filed, max(event.time for event in logs), later), tuple(logs),
        actions if state != ReviewState.DONE and not unowned else (), source, entry.urgency, '',
        update_registration.question_queue_name(owner), entry.key, entry.after, entry.value, opened.line(),
        review_events.waiting_why(fields.get('waiting_why', '')), response, status_time, frozenset(body_answer_events))


def store_paths(folders: tuple[Path, ...], sources: list[Path] | None) -> Iterator[Path]:
    """Watch each store and archive before reading its paths."""
    seen: set[Path] = set()
    for folder in folders:
        with guard_source(folder, sources):
            paths = (snapshot_store_paths(folder, sources) if isinstance(sources, ReviewSourceStamps)
                     else record_store.stored_paths((folder,), sources=sources, strict=sources is not None))
            for path in paths:
                if path not in seen:
                    seen.add(path)
                    yield path


def snapshot_store_paths(folder: Path, sources: ReviewSourceStamps) -> Iterator[Path]:
    yield from sources.record_files(folder)
    for archive in record_store.archive_folders((folder,)):
        yield from sources.record_files(archive, recursive=True)


def question_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *, zone: ZoneInfo,
                   owners: tuple[staff.StaffRow, ...] = (), sources: list[Path] | None = None,
                   include_unowned: bool = True,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
                   kinds: tuple[ReviewType, ...] = QUESTION_TYPES,
                   owner_options: dict[staff.StaffId, str] | None = None,
                   selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    identities = update_registration.QuestionStaff.for_record(members)
    selected = owners or tuple(member for member in members.rows if member.kind == 'producer' or
        member.kind == 'showrunner' and (include_unowned or
            isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path in documents))
    if isinstance(selection, ExactReviewRecord):
        selected = tuple(member for member in selected if member.id == selection.owner)
    found: list[ReviewItem] = []
    seen: set[Path] = set()
    identities_found: set[staff.StaffId] = set()
    statuses = tuple(status for status in ('open', 'closed')
                     if (ReviewState.DONE in states if status == 'closed' else states != (ReviewState.DONE,)))

    def append(record: update_registration.QuestionRecord, *, unowned: bool = False) -> None:
        if record.path in seen:
            return
        seen.add(record.path)
        if question_state(record) not in states or not selection.includes(
                record.path, 'question/'+record.owner+'/'+record.path.name):
            return
        item = question_item(record, members, unowned=unowned, zone=zone)
        if item.type in kinds:
            found.append(item)

    scopes: dict[Path, list[staff.StaffId]] = {}
    unowned_ids: set[staff.StaffId] = set()

    def add_owner(identity: staff.StaffId, document: Path | None) -> None:
        location = document if document is not None else state_root()
        current: tuple[Path, ...] = ()
        if document is not None:
            with guard_source(document, sources, skip=(FileNotFoundError,)):
                folder = update_registration.question_folder(document, identities)
                if isinstance(folder, update_registration.QuestionFolder):
                    current = (folder.path,)
        with guard_source(location, sources, skip=(FileNotFoundError,)):
            for folder in record_store.known_folders(record_store.RecordKind.QUESTION, identity, current,
                                                     document=document, strict=sources is not None, sources=sources):
                scopes.setdefault(folder, []).append(identity)

    if isinstance(selection, ExactReviewRecord):
        if include_unowned and not selected:
            unowned_ids.add(selection.owner)
        paths = {selection.path: [selection.owner]}
    else:
        for owner in selected:
            doc = record_store.local_document(owner)
            if (owner.kind == 'showrunner' and doc is None and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
                    and not include_unowned):
                print(f'questions: {owner.name}: ownership evidence is unavailable', file=sys.stderr)
                continue
            add_owner(owner.id, doc)
        if include_unowned:
            with guard_source(state_root() / "review-folders", sources):
                for identity, member in record_store.store_owners(record_store.RecordKind.QUESTION, members).items():
                    if member is None or member.kind not in ('showrunner', 'producer'):
                        unowned_ids.add(identity)
                        add_owner(identity, None)
        paths: dict[Path, list[staff.StaffId]] = {}
        for folder, identities_in_folder in scopes.items():
            for path in store_paths((folder,), sources):
                paths.setdefault(path, []).extend(identities_in_folder)
    for path, identities_in_path in paths.items():
        with guard_source(path, sources, skip=(FileNotFoundError,)):
            allowed = tuple(dict.fromkeys(identities_in_path))
            legacy_owner = allowed[0] if len(allowed) == 1 else ''
            for record in update_registration.selected_question_records(
                    [path], statuses, identities_found, sources, legacy_owner, strict=sources is not None,
                    allowed_owners=allowed):
                append(record, unowned=record.owner in unowned_ids)
    if owner_options is not None:
        for identity in identities_found:
            member = members.by_id(identity)
            owner_options[identity] = name(identity, members) if member is not None else 'Owner unavailable'
    return found


def investigation_items(members: staff.StaffSnapshot, *, owners: tuple[staff.StaffId, ...] = (),
                        sources: list[Path] | None = None,
                        states: tuple[ReviewState, ...] = REVIEW_STATES,
                        kinds: tuple[ReviewType, ...] = INVESTIGATION_TYPES,
                        owner_options: dict[staff.StaffId, str] | None = None,
                        selection: ReviewSelection = ALL_RECORDS, zone: ZoneInfo | None = None) -> list[ReviewItem]:
    zone = zone or user_zone()
    root = investigation.state_root()
    selected = owners
    with guard_source(root, sources):
        if isinstance(selection, ExactReviewRecord):
            selected = (staff.StaffId(selection.key.split('/')[1]),)
        elif not selected:
            selected = tuple(sorted(record_store.store_owners(record_store.RecordKind.INVESTIGATION)))
    found: list[ReviewItem] = []
    for identity in selected:
        folder = investigation.folder(identity)
        with guard_source(folder, sources):
            if isinstance(selection, ExactReviewRecord):
                paths = (selection.path,)
            else:
                folders = record_store.known_folders(record_store.RecordKind.INVESTIGATION, identity,
                                                    strict=sources is not None, sources=sources)
                paths = store_paths(folders, sources)
            for path in paths:
                if sources is not None and not isinstance(sources, ReviewSourceStamps):
                    sources.append(path)
                with guard_source(path, sources, skip=(FileNotFoundError, investigation.InvestigationError)):
                    if not path.is_file():
                        continue
                    if ReviewState.DONE not in states:
                        header = issue_store.frontmatter(path, sources=sources, keys=frozenset({'state'}))
                        if header.get('state') == [investigation.InvestigationState.READ]:
                            continue
                    record = investigation.read_review_record(path)
                    if record.owner != identity or investigation_type(record) not in kinds:
                        continue
                    if owner_options is not None:
                        for owner in (record.source_owner, owner_for(identity, members)):
                            owner_options[owner] = name(owner, members)
                    if investigation_state(record) in states:
                        found.append(investigation_item(record, members, path, zone=zone))
    return found


def investigation_type(record: investigation.Investigation) -> ReviewType:
    return ReviewType.BUG if record.kind == investigation.InvestigationKind.BUG else ReviewType.INVESTIGATION


def on_list(item: ReviewItem) -> bool:
    """Check if the source record belongs to a List item."""
    source = item.source
    if isinstance(source, InvestigationSource) and source.record.belongs_on_list:
        return True
    return any(list_move_event(event) for event in item.log)


def list_move_event(event: ReviewLog) -> bool:
    return event.who == 'nate' and re.fullmatch(r'moved to List item [1-9][0-9]*', event.words) is not None


def change_on_list(row: dict[str, str]) -> bool:
    return any(list_move_event(event) for event in review_events.decode(row.get('Review log', '')))


def can_move_wait(item: ReviewItem) -> bool:
    return (item.type != ReviewType.FOR_YOU and item.state in (ReviewState.WAITING, ReviewState.VALIDATING)
            and not on_list(item))


def list_reply(item: ReviewItem) -> str:
    """Read the latest reply from the native evidence for the List."""
    source = item.source
    if isinstance(source, ChangeSource):
        question = source.slots.question
        return question.answer if isinstance(question, delivery_state.AnsweredQuestion) else ''
    for index in reversed(range(len(item.log))):
        words = item.log[index].words
        if words.startswith('Answer: '):
            return words.removeprefix('Answer: ')
        if index in item.body_answer_events:
            return words
    return ''


@dataclass(frozen=True)
class ValidationLogStart:
    first_result_index: int


class LegacyValidationLog(StrEnum):
    MOVE_MARKER = 'use the List move entry'


def validation_request_words(feedback: str) -> str:
    return 'validate' + (': ' + feedback if feedback else '')


def validation_log_start(row: dict[str, str], pending_request: str = '') -> ValidationLogStart:
    """Start at the next log entry after the validation request."""
    events = review_events.decode(row.get('Review log', ''))
    if pending_request and not any(
            (event.who, event.words, event.feedback_request_id) == ('nate', pending_request, '')
            for event in events[-1:]):
        return ValidationLogStart(len(events)+1)
    return ValidationLogStart(len(events))


def validation_problem(item: ReviewItem, list_number: int, start: ValidationLogStart | LegacyValidationLog) -> str:
    """Read the problem for the validation that moved to this List item."""
    source = item.source
    if not isinstance(source, ChangeSource):
        return ''
    log = review_events.decode(source.row.get('Review log', ''))
    if isinstance(start, ValidationLogStart):
        events = iter(log[start.first_result_index:])
    else:
        marker = f'moved to List item {list_number}'
        events = iter(log)
        for event in events:
            if event.who == 'nate' and event.words == marker:
                break
        else:
            return ''

    prefix = 'Check found a problem: '
    problem = ''
    for event in events:
        if event.words.partition(':')[0] == 'validate':
            break
        if event.words.startswith(prefix):
            problem = event.words.removeprefix(prefix)
    return problem


def investigation_item(record: investigation.Investigation, members: staff.StaffSnapshot, path: Path, *,
                       zone: ZoneInfo | None = None) -> ReviewItem:
    zone = zone or user_zone()
    filed = moment(record.filed, zone)
    answered_at = moment(record.answered, zone) if record.answered else question_path.NO_ANSWER_TIME
    logs = [ReviewLog(filed, name(record.owner, members), 'Filed')]
    if isinstance(answered_at, datetime) and not any(entry.words.startswith('Answer: ') for entry in record.log):
        logs.append(ReviewLog(answered_at, name(record.owner, members), 'Answer ready'))
    if record.read and not any(entry.words.startswith(('Marked read','feedback sent:')) for entry in record.log):
        logs.append(ReviewLog(moment(record.read, zone), 'nate', 'Marked read'))
    logs.extend(replace(event, time=aware(event.time, zone)) if event.time.tzinfo is None else event for event in record.log)
    state = investigation_state(record)
    owner = owner_for(record.owner, members)
    key = investigation_record_key(record)
    identity_text = name(record.owner, members)+'/'+record.id
    return ReviewItem(key, identity_text, investigation_type(record), owner, record.source_owner, record.title, state,
        ReviewTimes(filed, logs[-1].time), tuple(logs),
        ('read', 'feedback', 'question') if state != ReviewState.DONE else (), InvestigationSource(record, path),
        queue='investigation-'+owner, queue_key=key,
        text=identity_text+' '+record.title+'\n'+record.question+'\n\nAnswer\n'+record.answer,
        waiting_why=review_events.waiting_why(record.waiting_why),
        answer_time=question_path.answer_time(answered_at, tuple(logs)))


def proposal_state(issue: Issue) -> ReviewState:
    """Finish the user's review when a proposal is approved, declined, closed or answered on the page."""
    mark = review_mark(issue)
    if (issue.value('status') != 'open' or isinstance(proposal_progress(issue), InProgress)
            or isinstance(decision_answer(issue), DeclinedDecision) or mark == ReviewMark.DONE):
        return ReviewState.DONE
    return ReviewState.WAITING if mark == ReviewMark.WAITING else ReviewState.OPEN


def proposal_item(key: str, issue: Issue, owner: staff.StaffId, who: staff.StaffId, title: str, filed: datetime,
                  members: staff.StaffSnapshot, entry: urgency.Entry[proposals_table.Row] | None = None,
                  queue: str = '') -> ReviewItem:
    """Project one proposal issue with its page log and its actions."""
    state = proposal_state(issue)
    logs = (ReviewLog(filed,name(who,members),'Proposed'), *review_log(issue))
    identity = name(owner,members)+'/'+(issue.value('proposal_id') or issue.path.stem)
    actions = DECISION_ACTIONS if state != ReviewState.DONE else ()
    if entry is None:
        return ReviewItem(key,identity,ReviewType.PROPOSAL,owner,who,title,state,ReviewTimes(filed,logs[-1].time),logs,
                          actions,ProposalSource(issue),waiting_why=review_events.waiting_why(issue.value('waiting_why')))
    return ReviewItem(key,identity,ReviewType.PROPOSAL,owner,who,title,state,ReviewTimes(filed,logs[-1].time),logs,
                      actions,ProposalSource(issue),entry.urgency,'proposal/'+entry.after if entry.after else '',queue,
                      entry.key,entry.after,entry.value,proposals_table.proposal_text(entry),
                      review_events.waiting_why(issue.value('waiting_why')))



def proposal_path(path: Path) -> Path:
    """Use the same folder path for a filed Proposal and a scanned Proposal."""
    return path.parent.resolve() / path.name


def proposal_prerequisite(path: Path, dependency: str, eligible: Callable[[Path], bool],
                          matching_paths: Iterable[Path]) -> str:
    """Prefer the sibling. Otherwise use the last eligible path in sorted order."""
    if not dependency or Path(dependency).name != dependency or dependency == path.stem:
        return ''
    sibling = path.with_name(dependency+'.md')
    if eligible(sibling):
        return str(sibling)
    return max((str(candidate) for candidate in matching_paths
                if candidate != sibling and eligible(candidate)), default='')


def issue_item(issue: Issue, owner: staff.StaffId, members: staff.StaffSnapshot, *,
               zone: ZoneInfo, sources: list[Path] | None = None, prerequisite: Callable[[Path, str], str] | None = None) -> ReviewItem:
    """Project a proposal for the Review index and its body."""
    issue = Issue(proposal_path(issue.path), issue.fields)
    row = proposals_table.issue_row(issue, members, sources=sources)
    key = proposal_record_key(issue.path)
    if not isinstance(row.progress, proposal_issue.AwaitingApproval):
        filed = moment(issue.value('date_created') or '1970-01-01', zone)
        return proposal_item(key, issue, owner, staff.StaffId(issue.value('proposed_by') or owner),
                             issue.path.stem, filed, members)
    after = prerequisite(issue.path, row.entry.after) if prerequisite is not None and row.entry.after else ''
    entry = replace(row.entry, after=after)
    filed = aware(entry.since, zone) if entry.since is not None else datetime(1970, 1, 1, tzinfo=zone)
    return proposal_item(key, issue, owner, staff.StaffId(issue.value('proposed_by')), issue.path.stem, filed,
                         members, entry, 'proposal-'+owner)


def question_record_key(record: update_registration.QuestionRecord) -> str:
    return f"question/{record.owner}/{record.path.name}"


def investigation_record_key(record: investigation.Investigation) -> str:
    return f"investigation/{record.owner}/{record.id}"


def proposal_record_key(path: Path) -> str:
    return f"proposal/{path}"


def submission_review_key(saved: review_submissions.SavedSubmission) -> str:
    """Name the saved record in the Review index."""
    record = saved.record
    if isinstance(record, update_registration.QuestionRecord):
        return question_record_key(record)
    if isinstance(record, Issue):
        return proposal_record_key(saved.path)
    return investigation_record_key(record)


def submission_item(saved: review_submissions.SavedSubmission, members: staff.StaffSnapshot) -> ReviewItem:
    """Project the native record returned by Send."""
    record = saved.record
    if isinstance(record, update_registration.QuestionRecord):
        return question_item(record, members)
    if isinstance(record, Issue):
        return issue_item(record, saved.owner, members, zone=user_zone(), sources=[])
    return investigation_item(record, members, saved.path)


def is_open_proposal(issue: Issue) -> bool:
    return (issue.value('record_type') != 'question' and issue.value('status') == 'open'
            and PROPOSAL_TAG in issue.fields.get('tags', []))


def sent_proposal_origin(issue: Issue) -> new_item_dialog.SubmissionOrigin:
    if issue.value('record_type') == 'proposal' and PROPOSAL_TAG in issue.fields.get('tags', []):
        return proposal_issue.review_submission(issue)
    return new_item_dialog.NoReviewSubmission()


def proposal_belongs_to(issue: Issue, owner: staff.StaffId, proposers: set[staff.StaffId], *, sent: bool) -> bool:
    proposer = staff.StaffId(issue.value('proposed_by'))
    legacy_owner = '' if sent else (owner if proposer in proposers else proposer)
    return proposal_issue.proposal_owner(issue, legacy_owner) == owner


def read_proposal_prerequisite(path: Path, dependency: str, owner: staff.StaffId,
                               proposers: set[staff.StaffId], retained: Path | None,
                               sources: list[Path] | None) -> str:
    """Check the sibling and the path retained by the snapshot."""
    def eligible(candidate: Path) -> bool:
        matches = False
        with guard_source(candidate, sources, skip=(OSError, UnicodeError, ValueError)):
            if not candidate.is_file():
                return False
            other = Issue(candidate, record_store.read_header(candidate, sources=sources, strict=True))
            sent = isinstance(sent_proposal_origin(other), new_item_dialog.ReviewSubmission)
            matches = (sent or is_open_proposal(other)) and proposal_belongs_to(other, owner, proposers, sent=sent)
        return matches

    matching = (retained,) if retained is not None and retained.stem == dependency else ()
    return proposal_prerequisite(path, dependency, eligible, matching)


def backlog_review_rows(path: Path, sources: list[Path] | None, *, exact: bool) -> Iterator[proposals_table.Row]:
    """Read linked files when the caller requires complete source reads."""
    if not exact and sources is not None and not isinstance(sources, ReviewSourceStamps):
        yield from (row for row in proposals_table.ledger_rows(path, sources=sources)
                    if row.state == proposals_table.REVIEW_STATE)
        return
    for line in proposals_table.read_lines(path, sources=sources):
        match = proposals_table.BACKLOG_LINE.fullmatch(line)
        if match is not None and match['state'] == 'proposed':
            title = proposals_table.BACKLOG_FILE.sub('', match['title'])
            yield proposals_table.Row('plan backlog', title, match['date'], proposals_table.REVIEW_STATE)


def proposal_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   sources: list[Path] | None = None, own: list[Issue] | None = None,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
                   owner_options: dict[staff.StaffId, str] | None = None,
                   include_registered: bool = False,
                   selection: ReviewSelection = ALL_RECORDS, zone: ZoneInfo | None = None) -> list[ReviewItem]:
    zone = zone or user_zone()
    found: dict[str, ReviewItem] = {}
    scopes: dict[Path | staff.StaffId, staff.StaffId] = {}
    exact = isinstance(selection, ExactReviewRecord)
    retained_prerequisite = selection.prerequisite if exact else None
    for doc in documents:
        with guard_source(doc, sources):
            member = members.by_doc(local_machine(), doc.resolve())
            if member is not None and member.kind == 'showrunner':
                scopes[doc] = member.id
    if include_registered:
        with guard_source(state_root() / "review-folders", sources):
            if exact:
                registered: dict[staff.StaffId, staff.StaffRow | None] = {row.id: row for row in members.rows}
                _ = registered.setdefault(selection.owner, None)
            else:
                registered = record_store.store_owners(record_store.RecordKind.PROPOSAL, members)
            for owner, row in registered.items():
                if owner not in scopes.values() and (row is None or row.kind in ('showrunner', 'producer')):
                    scopes[owner] = owner
        for row in members.find(kind='producer'):
            _ = scopes.setdefault(row.id, row.id)

    paths_by_scope: dict[Path, list[Path | staff.StaffId]] = {}
    if own is None:
        folders: dict[Path, list[Path | staff.StaffId]] = {}
        for scope, owner in scopes.items():
            if exact:
                if selection.key == 'proposal/'+str(selection.path):
                    paths_by_scope.setdefault(selection.path, []).append(scope)
                continue
            member = members.by_id(owner)
            doc = scope if isinstance(scope, Path) else record_store.local_document(member)
            current: tuple[Path, ...] = ()
            if member is not None and member.kind == 'producer' and isinstance(member.doc, staff.LiveDoc):
                producer_doc = member.doc.path
                with suppress(OSError):
                    if producer_doc.is_file():
                        if sources is not None:
                            sources.append(producer_doc)
                        if (configured := record_store.issues_folder(producer_doc)) is not None:
                            current = (configured,)
            with guard_source(doc if doc is not None else state_root(), sources):
                for folder in record_store.known_folders(record_store.RecordKind.PROPOSAL, owner, current, document=doc,
                                                         strict=sources is not None, sources=sources):
                    folders.setdefault(folder, []).append(scope)
        for folder, owners in folders.items():
            for path in store_paths((folder,), sources):
                paths_by_scope.setdefault(path, []).extend(owners)

    proposers = {owner:{owner, *(member.id for member in members.find(kind='unit', parent=owner))}
                 for owner in set(scopes.values())}
    references: dict[Path | staff.StaffId, dict[str, set[Path]]] = {scope:{} for scope in scopes}
    selected_issues: dict[Path | staff.StaffId, list[Issue]] = {scope:[] for scope in scopes}

    def add_issue(issue: Issue, candidates: Iterable[Path | staff.StaffId]) -> None:
        sent = isinstance(sent_proposal_origin(issue), new_item_dialog.ReviewSubmission)
        if not sent and not is_open_proposal(issue):
            return
        for scope in dict.fromkeys(candidates):
            owner = scopes[scope]
            if not proposal_belongs_to(issue, owner, proposers[owner], sent=sent):
                continue
            references[scope].setdefault(issue.path.stem, set()).add(issue.path)
            if proposal_state(issue) in states:
                selected_issues[scope].append(issue)

    if own is None:
        for path, candidates in sorted(paths_by_scope.items()):
            with guard_source(path, sources, skip=(FileNotFoundError, ValueError)):
                if not path.is_file():
                    continue
                fields = (issue_store.frontmatter(path, sources=sources) if exact or ReviewState.DONE not in states
                          else record_store.read_header(path, sources=sources, strict=sources is not None))
                issue = Issue(path, fields)
                add_issue(issue, candidates)
    else:
        for issue in own:
            add_issue(issue, scopes)
    for scope, owner in scopes.items():
        queue = 'proposal-'+owner
        for issue in selected_issues[scope]:
            with guard_source(issue.path, sources):
                def prerequisite(path: Path, dependency: str) -> str:
                    if exact:
                        return read_proposal_prerequisite(path, dependency, owner, proposers[owner],
                                                          retained_prerequisite, sources)
                    matching = references[scope].get(dependency, set())
                    return proposal_prerequisite(path, dependency, matching.__contains__, matching)
                item = issue_item(issue, owner, members, zone=zone, sources=sources, prerequisite=prerequisite)
                if item.state in states:
                    found[item.key] = item
        if not isinstance(scope, Path) or exact and (owner != selection.owner or selection.key == 'proposal/'+str(selection.path)):
            continue
        ledgers: list[Path] = []
        with guard_source(scope, sources):
            ledgers = proposals_table.improvement_ledgers(scope)
        for path in ledgers:
            if not selection.includes(path):
                continue
            with guard_source(path, sources):
                for row in backlog_review_rows(path, sources, exact=exact):
                    with guard_source(path, sources, entry=row.proposal):
                        since = proposals_table.day(row.date, sources=sources)
                        entry = urgency.Entry(f"{proposals_table.BACKLOG_KEY}{path}: {row.proposal}", row,
                                              since=since, value=(proposals_table.UNSCORED,))
                        filed = aware(since, zone) if since is not None else datetime(1970, 1, 1, tzinfo=zone)
                        key = 'proposal/'+entry.key
                        if ReviewState.OPEN in states and selection.includes(path, key) and key not in found:
                            found[key] = ReviewItem(key,row.proposal,ReviewType.PROPOSAL,owner,owner,row.proposal,
                                ReviewState.OPEN,ReviewTimes(filed,filed),(ReviewLog(filed,name(owner,members),'Proposed'),),(),
                                ProposalLedgerSource(path),entry.urgency,queue=queue,
                                queue_key=entry.key,value=entry.value,text=proposals_table.proposal_text(entry))
    if owner_options is not None:
        for item in found.values():
            owner_options[item.owner] = name(item.owner,members)
    return list(found.values())


EFFICIENCY_LEDGER_FIELD = 'Efficiency ledger'
EFFICIENCY_LINE = re.compile(r'^- \d{4}-\d\d-\d\d (?P<head>.*?) — (?P<status>.*)$')


def efficiency_ledger(doc: Path) -> Path | NoEfficiencyLedger:
    """Read the ledger path or name its absence."""
    try:
        return Path(production_field(doc.read_text(encoding='utf-8').splitlines(), EFFICIENCY_LEDGER_FIELD)).expanduser()
    except (Refusal, OSError, UnicodeError):
        return NoEfficiencyLedger()


def efficiency_state(result: str, review: efficiency_reviews.EfficiencyReview) -> ReviewState:
    """The ledger ending sets the state; a page mark changes only a proposed line."""
    if not result.startswith(('proposed', 'deferred')):
        return ReviewState.DONE
    if result.startswith('deferred') or review.mark == ReviewMark.WAITING:
        return ReviewState.WAITING
    return ReviewState.DONE if review.mark == ReviewMark.DONE else ReviewState.OPEN


def production_efficiency_ledgers(members: staff.StaffSnapshot, documents: tuple[Path, ...],
                                  sources: list[Path] | None) -> Generator[tuple[Path, staff.StaffId]]:
    """Use the first production that names each ledger in the snapshot."""
    seen: set[Path] = set()
    for doc in documents:
        path: Path | NoEfficiencyLedger = NoEfficiencyLedger()
        with guard_source(doc, sources, skip=(FileNotFoundError,)):
            path = efficiency_ledger(doc)
        if isinstance(path, NoEfficiencyLedger) or path in seen:
            continue
        seen.add(path)
        owner = next((member.id for member in members.rows if isinstance(member.doc,(staff.LiveDoc,staff.WrappedDoc))
                      and member.doc.path == doc),staff.StaffId(''))
        yield path, owner


def efficiency_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *, zone: ZoneInfo,
                     sources: list[Path] | None = None,
                     states: tuple[ReviewState, ...] = REVIEW_STATES,
                     owner_options: dict[staff.StaffId, str] | None = None,
                     selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    found: list[ReviewItem] = []
    ledgers = (((selection.path, selection.owner),) if isinstance(selection, ExactReviewRecord)
               else production_efficiency_ledgers(members, documents, sources))
    for path, owner in ledgers:
        if sources is not None:
            sources.append(path)
        with guard_source(path, sources, skip=(FileNotFoundError,)):
            lines = path.read_text(encoding='utf-8').splitlines()
            invalid: set[str] = set()
            def skip_review(source: Path, identity: str, error: Exception) -> None:
                invalid.add(identity)
                if isinstance(sources, ReviewSourceStamps):
                    sources.skipped(source, identity, error)
                elif sources is not None and (isinstance(selection, AllReviewRecords)
                        or identity == 'E'+str(int(selection.key.rsplit('/', 1)[-1])+1)):
                    raise error
                else:
                    logging.getLogger(__name__).warning('Review source %s (%s): %s', source, identity, error)
            with guard_source(efficiency_reviews.path(path), sources):
                reviews = efficiency_reviews.read(path, sources, skipped=skip_review)
                for index,line in enumerate(lines):
                    identity = f'E{index+1}'
                    with guard_source(path, sources, entry=identity):
                        if (not selection.includes(path, f'efficiency/{path}/{index}')
                                or EFFICIENCY_LINE.match(line) is None or identity in invalid):
                            continue
                        review = reviews.get(identity, efficiency_reviews.EfficiencyReview())
                        if efficiency_state(line.partition(' — ')[2], review) not in states:
                            continue
                        item = efficiency_item(path, index, line, review, owner, members, zone)
                        if owner_options is not None:
                            owner_options[owner] = name(owner,members)
                        found.append(item)
    return found


def efficiency_item(path: Path, index: int, line: str, review: efficiency_reviews.EfficiencyReview,
                    owner: staff.StaffId, members: staff.StaffSnapshot, zone: ZoneInfo) -> ReviewItem:
    head, _, result = line.partition(' — ')
    state = efficiency_state(result, review)
    filed = moment(head[2:12], zone)
    candidates_found = candidates.entries([head+' — proposed'])
    entry = candidates_found[0] if candidates_found else urgency.Entry(head,line)
    candidate = candidates_found[0].item if candidates_found else None
    who = staff.StaffId(candidate.by if candidate is not None else '')
    key = f'efficiency/{path}/{index}'
    logs = (ReviewLog(filed, name(owner, members), result),
            *(replace(event, time=aware(event.time, zone)) if event.time.tzinfo is None else event
              for event in review.log))
    return ReviewItem(key,name(owner,members)+f'/E{index+1}',ReviewType.EFFICIENCY,owner,who,
        head[13:],state,ReviewTimes(filed,logs[-1].time),logs,
        DECISION_ACTIONS if state != ReviewState.DONE else (),
        EfficiencySource(path,line),entry.urgency,queue=candidates.QUEUE,queue_key=entry.key,
        value=entry.value,text=line,waiting_why=review_events.waiting_why(review.waiting_why))


class ReviewRecordChanged(ValueError):
    """The current record does not accept the action on the displayed card."""


class ReviewRecordMissing(ReviewRecordChanged):
    """The source no longer contains the displayed record."""


def read_current(root: Path, item: ReviewItem, members: staff.StaffSnapshot) -> ReviewItem:
    """Reread the exact record. Use the snapshot owner for an Efficiency ledger."""
    source = item.source
    if isinstance(source, ForYouSource):
        if source.row.kind == 'action':
            record = ActionStore().get(item.key)
            requester = next((member.name for member in members.rows
                              if record.requested_by in (member.id, member.current_session)), record.requested_by)
            return for_you_item(for_you.action_row(record, requester), members)
        for row in for_you.read(members=members).rows:
            if row.key == item.key:
                return for_you_item(row, members)
        raise ReviewRecordMissing('This item is no longer on the list.')
    prerequisite = Path(item.queue_after) if isinstance(source, ProposalSource) and item.queue_after else None
    selection = ExactReviewRecord(source_path(item), item.key, item.owner, prerequisite)
    if isinstance(source, ProposalSource) and 'review_submission' in source.record.fields:
        return read_sent_proposal(selection, source.record, members)
    kinds = QUESTION_TYPES if item.type in QUESTION_TYPES else (item.type,)
    try:
        records = project(root, members, kinds, REVIEW_STATES, None, [], selection, user_zone())
    except FileNotFoundError:
        records = []
    for record in records:
        if record.key == item.key:
            return record
    raise ReviewRecordMissing('This item is no longer on the list.')


def read_sent_proposal(selection: ExactReviewRecord, previous: Issue, members: staff.StaffSnapshot) -> ReviewItem:
    """Read a sent proposal and its named prerequisite directly."""
    path = selection.path
    sources: list[Path] = []
    issue = Issue(path, record_store.read_header(path, sources=sources, strict=True))
    origin = sent_proposal_origin(issue)
    prior = sent_proposal_origin(previous)
    if (not isinstance(origin, new_item_dialog.ReviewSubmission)
            or not isinstance(prior, new_item_dialog.ReviewSubmission)
            or origin.request_id != prior.request_id
            or issue.value('proposal_id') != previous.value('proposal_id')):
        raise ReviewRecordMissing('This item is no longer on the list.')
    owner = staff.StaffId(proposal_issue.proposal_owner(issue, ''))
    if not owner:
        raise ReviewRecordMissing('This item no longer has a recipient.')
    def prerequisite(path: Path, dependency: str) -> str:
        proposers = {owner, *(member.id for member in members.find(kind='unit', parent=owner))}
        return read_proposal_prerequisite(path, dependency, owner, proposers, selection.prerequisite, sources)

    zone = user_zone()
    item, = with_later([issue_item(issue, owner, members, zone=zone, sources=sources, prerequisite=prerequisite)], sources, zone=zone)
    return normalize_times(item, zone)


def entry(item: ReviewItem) -> urgency.Entry[ReviewItem]:
    """Use the existing queue keys for pins, moves and prerequisites."""
    return urgency.Entry(item.queue_key,item,item.urgency,item.times.filed,item.value,item.queue_after)


def ordered(items: list[ReviewItem], sources: list[Path] | None, zone: ZoneInfo) -> list[ReviewItem]:
    """Apply queue pins and moves before combining the records."""
    states: dict[str, urgency.QueueState] = {}
    for queue in dict.fromkeys(name for item in items for name in (item.queue, 'next-'+item.owner)):
        with guard_source(urgency.queue_state_path(queue), sources):
            states[queue] = urgency.read_queue_state(queue, strict=sources is not None)
    entries: list[urgency.Entry[ReviewItem]] = []
    queues: dict[str, list[urgency.Entry[ReviewItem]]] = {}
    for item in items:
        if item.queue not in states or 'next-'+item.owner not in states:
            continue
        with guard_source(source_path(item), sources, entry=item.key):
            normalized = normalize_times(item, zone)
            ranked_entry = entry(normalized)
            # Check rank and time conversions for this record before comparing it with others.
            _ = urgency.order((ranked_entry,))
            entries.append(ranked_entry)
            queues.setdefault(item.queue, []).append(ranked_entry)
    ended: dict[str, datetime] = {}
    moved_by_queue: dict[str, dict[str, datetime]] = {}
    for queue, group in queues.items():
        moved = {key: aware(when, zone) for key, when in states[queue].ended.items()}
        moved_by_queue[queue] = moved
        ended.update({each.item.key: moved[each.key] for each in group if each.key in moved})
    question_keys = {each.key: each.item.key for each in entries if each.item.type == ReviewType.QUESTION}
    ranked = [each.item for each in urgency.order([
        replace(each, key=each.item.key, value=(), after=(
            question_keys.get(each.after, '') if each.item.type == ReviewType.QUESTION else each.item.after))
        for each in entries], ended=ended)]
    for queue, group in queues.items():
        # Preserve the global positions while applying each queue's current item.
        positions = [at for at,item in enumerate(ranked) if item.queue == queue]
        within = [each.item for each in urgency.order(group, states[queue].current, ended=moved_by_queue[queue])]
        for at,item in zip(positions,within):
            ranked[at] = item
    for owner in dict.fromkeys(item.owner for item in ranked):
        current = states['next-'+owner].current
        selected = next((item for item in ranked if item.owner == owner and item.queue_key == current
                         and item.key not in ended),None)
        if selected is not None:
            first = next(at for at,item in enumerate(ranked) if item.owner == owner)
            ranked.remove(selected)
            ranked.insert(first,selected)
    return ranked


def read(root: Path, *, members: staff.StaffSnapshot | None = None,
         kinds: tuple[ReviewType, ...] = REVIEW_TYPES, show: str = 'all',
         owner_options: dict[staff.StaffId, str] | None = None,
         sources: list[Path] | None = None, zone: ZoneInfo | None = None) -> list[ReviewItem]:
    """Read each record from its existing store without another stored copy."""
    members = staff.snapshot() if members is None else members
    zone = zone or user_zone()
    items = project(root, members, kinds, selected_states(show), owner_options, sources, ALL_RECORDS, zone)
    if open_view(show):
        now = datetime.now(zone)
        return [item for item in items if item.for_you(now)]
    return items


def project(root: Path, members: staff.StaffSnapshot, kinds: tuple[ReviewType, ...], states: tuple[ReviewState, ...],
            owner_options: dict[staff.StaffId, str] | None, sources: list[Path] | None,
            selection: ReviewSelection, zone: ZoneInfo) -> list[ReviewItem]:
    """Select the requested records and apply the shared projection."""
    documents = tuple(dict.fromkeys(doc for member in members.rows if isinstance(member.doc, staff.LiveDoc)
                                    if (doc := record_store.local_document(member)) is not None))
    items: list[ReviewItem] = []
    question_kinds = tuple(kind for kind in kinds if kind in QUESTION_TYPES)
    if question_kinds:
        items.extend(question_items(members,documents,zone=zone,states=states,kinds=question_kinds,owner_options=owner_options,
                                   sources=sources,selection=selection))
    if ReviewType.CHANGE in kinds:
        items.extend(change_items(root,members,zone=zone,states=states,owner_options=owner_options,sources=sources,selection=selection))
    reports = tuple(kind for kind in kinds if kind in INVESTIGATION_TYPES)
    if reports:
        items.extend(investigation_items(members,zone=zone,states=states,kinds=reports,owner_options=owner_options,sources=sources,selection=selection))
    if ReviewType.PROPOSAL in kinds:
        items.extend(proposal_items(members,documents,zone=zone,states=states,owner_options=owner_options,sources=sources,include_registered=True,selection=selection))
    if ReviewType.EFFICIENCY in kinds:
        items.extend(efficiency_items(members,documents,zone=zone,states=states,owner_options=owner_options,sources=sources,selection=selection))
    return ordered(with_later(items, sources, zone=zone), sources, zone)


def read_open(root: Path) -> list[ReviewItem]:
    """Share only open records during one console news request."""
    return read(root, show='open')


def with_later(items: list[ReviewItem], sources: list[Path] | None = None, *,
               zone: ZoneInfo | None = None) -> list[ReviewItem]:
    if not items:
        return []
    selected = {item.key for item in items}
    invalid: set[str] = set()

    def skip_time(path: Path, key: str, error: Exception) -> None:
        if isinstance(sources, ReviewSourceStamps):
            sources.skipped(path, key, error)
            invalid.add(key)
        elif key in selected:
            raise error

    try:
        times = review_later.times(sources, zone=zone, skipped=skip_time if sources is not None else None)
    except (OSError, UnicodeError) as error:
        if sources is not None:
            raise
        logging.getLogger(__name__).warning('Review source %s: %s', review_later.path(), error)
        return items
    return [replace(item, times=replace(item.times, later=times.get(item.key, item.times.later)))
            if item.state != ReviewState.VALIDATING else item
            for item in items if item.key not in invalid or item.state == ReviewState.VALIDATING]


def read_kind(kind: ReviewType, scope: Path | staff.StaffRow, members: staff.StaffSnapshot, *,
              below: bool = False, sources: list[Path] | None = None,
              own: list[Issue] | None = None, show: str = 'all', zone: ZoneInfo | None = None) -> list[ReviewItem]:
    zone = zone or user_zone()
    items = _read_kind(kind, scope, members, below=below, sources=sources, own=own,
                       states=selected_states(show), zone=zone)
    return with_later([normalize_times(item, zone) for item in items], sources, zone=zone)


def _read_kind(kind: ReviewType, scope: Path | staff.StaffRow, members: staff.StaffSnapshot, *,
              below: bool = False, sources: list[Path] | None = None,
              own: list[Issue] | None = None,
              states: tuple[ReviewState, ...] = REVIEW_STATES, zone: ZoneInfo) -> list[ReviewItem]:
    """Read the selected store into ReviewItem records for all consumers."""
    if isinstance(scope,staff.StaffRow):
        if kind in (ReviewType.CHANGE, ReviewType.PROPOSAL, ReviewType.EFFICIENCY):
            return []
        documents: tuple[Path,...] = ()
        owner = scope
    else:
        documents = tuple(proposals_folder.scope_docs(scope,below,record=members))
        owner = scope
    if kind in QUESTION_TYPES:
        owners = (scope,) if isinstance(scope,staff.StaffRow) else tuple(member for member in members.rows
            if member.kind == 'showrunner' and isinstance(member.doc,(staff.LiveDoc,staff.WrappedDoc)) and member.doc.path in documents)
        return question_items(members,documents,zone=zone,owners=owners,sources=sources,include_unowned=False,states=states,
                              kinds=QUESTION_TYPES if kind == ReviewType.QUESTION else (kind,))
    if kind in INVESTIGATION_TYPES:
        owners_ids = tuple(member.id for doc in documents for member in
            (unit_lookup.doc_owner(doc,members),*unit_lookup.doc_units(doc,members))) if documents else (cast(staff.StaffRow,owner).id,)
        return investigation_items(members,zone=zone,owners=owners_ids,sources=sources,states=states,
                                   kinds=INVESTIGATION_TYPES if kind == ReviewType.INVESTIGATION else (kind,))
    if kind == ReviewType.PROPOSAL:
        return proposal_items(members,documents,zone=zone,sources=sources,own=own,states=states)
    if kind == ReviewType.EFFICIENCY:
        return efficiency_items(members,documents,zone=zone,sources=sources,states=states)
    assert isinstance(scope,Path)
    root = change_routing.review_root() if sources is None else change_routing.review_root(sources=sources)
    items = change_items(root,members,zone=zone,document=scope,below=below,sources=sources,states=states)
    tabs = change_review.review_scope(scope,below,members)
    return [item for item in items if isinstance(item.source,ChangeSource) and
            (change_scope_tab(item, members) in tabs or isinstance(item.source.assignment.owner,change_routing.UnroutedChange))]


def change_scope_tab(item: ReviewItem, members: staff.StaffSnapshot) -> str:
    assert isinstance(item.source, ChangeSource)
    validator = members.by_id(item.owner) if item.state == ReviewState.VALIDATING and item.owner else None
    return validator.name if validator is not None else item.source.assignment.owner.tab


def change_counts(doc: Path, record: staff.StaffSnapshot, *, source: str = '',
                  sources: list[Path] | None = None) -> tuple[int,int]:
    try:
        zone = user_zone()
        now = datetime.now(zone)
        items = read_kind(ReviewType.CHANGE,doc,record,sources=sources,show='open',zone=zone)
        return (sum(item.for_you(now) and not isinstance(item.source.assignment.owner,change_routing.UnroutedChange)
                    and (not source or item.source_unit == source) for item in items if isinstance(item.source,ChangeSource)),
                0 if source else sum(item.for_you(now) and isinstance(item.source.assignment.owner,change_routing.UnroutedChange)
                    for item in items if isinstance(item.source,ChangeSource)))
    except staff.StaffUnreadable:
        if sources is not None:
            raise
        return 0,0
    except OSError:
        if sources is not None:
            raise
        return 0,0
    except change_review.ChangeReviewError:
        return 0,0


def proposals(doc: Path, *, source: str = '', sources: list[Path] | None = None, own: list[Issue] | None = None,
              record: staff.StaffSnapshot | None = None) -> int:
    record = staff.snapshot() if record is None else record
    zone = user_zone()
    items = read_kind(ReviewType.PROPOSAL, doc, record, sources=sources, own=own, show=ReviewState.OPEN, zone=zone)
    return count_items(items, datetime.now(zone), source)


def open_efficiency(line: str, *, source: str = '') -> bool:
    """Retain the legacy line predicate at its public boundary."""
    match = EFFICIENCY_LINE.match(line)
    return match is not None and match['status'].startswith('proposed') and (
        not source or match['head'].rstrip().endswith(f'[by: {source}]'))


def efficiency(doc: Path, *, source: str = '', sources: list[Path] | None = None,
               record: staff.StaffSnapshot | None = None) -> int:
    record = staff.snapshot() if record is None else record
    try:
        zone = user_zone()
        items = read_kind(ReviewType.EFFICIENCY, doc, record, sources=sources, show=ReviewState.OPEN, zone=zone)
        return count_items(items, datetime.now(zone), source)
    except (OSError,UnicodeError):
        if sources is not None:
            raise
        return 0


def investigations(doc: Path, record: staff.StaffSnapshot, *, source: str = '', sources: list[Path] | None = None) -> int:
    zone = user_zone()
    try:
        items = read_kind(ReviewType.INVESTIGATION,doc,record,sources=sources,show='open',zone=zone)
    except staff.StaffUnreadable:
        return 0
    return count_items(items,datetime.now(zone),source)


def question_counts(owner: staff.StaffRow, _production_zone: ZoneInfo, identities: QuestionStaff, sources: list[Path] | None,
                    source: str = '') -> tuple[int,str]:
    """Count Questions in the same zone as Review and /next."""
    if sources is not None and not os.environ.get('USER_ZONE'):
        sources.append(Path(user_zone_config.CONF))
    zone = user_zone()
    items = read_kind(ReviewType.QUESTION,owner,identities.record,sources=sources,show='open',zone=zone)
    now = datetime.now(zone)
    return (sum(item.for_you(now) and (not source or item.source_unit == source) for item in items),
            question_path.next_later_deadline((item.times.later for item in items), now))


def ready_investigations(owner: staff.StaffId, *, sources: list[Path] | None = None) -> int:
    zone = user_zone()
    items = investigation_items(staff.snapshot(), owners=(owner,), sources=sources, states=(ReviewState.OPEN,), zone=zone)
    return count_items(with_later(items, sources, zone=zone), datetime.now(zone))


def next_queue(kind: str, scope: Path | staff.StaffRow, below: bool, members: staff.StaffSnapshot) -> tuple[str,list[urgency.Entry[str]]]:
    zone = user_zone()
    items = read_kind(ReviewType(kind),scope,members,below=below,show='open',zone=zone)
    now = datetime.now(zone)
    selected = [item for item in items if item.for_you(now) and not
                (isinstance(item.source,ChangeSource) and isinstance(item.source.assignment.owner,change_routing.UnroutedChange))]
    if isinstance(scope, staff.StaffRow) and kind in ('change', 'proposal', 'efficiency'):
        queue = urgency.scope_queue(kind+'-'+scope.id,below)
    elif kind == 'change':
        assert isinstance(scope,Path)
        queue = change_queue_name(scope,members,change_review.scope_list(scope,record=members),below)
    elif kind in ('question', 'response'):
        owner = scope if isinstance(scope,staff.StaffRow) else unit_lookup.doc_owner(scope,members)
        queue = update_registration.question_queue_name(owner.id,below)
    elif kind == 'proposal':
        assert isinstance(scope,Path)
        queue = urgency.scope_queue(proposals_table.queue(scope,record=members),below)
    elif kind == 'efficiency':
        queue = urgency.scope_queue(candidates.QUEUE,below)
    else:
        owner = scope if isinstance(scope,staff.StaffRow) else unit_lookup.doc_owner(scope,members)
        queue = urgency.scope_queue('investigation-'+owner.id,below)
    return queue,[urgency.Entry(item.queue_key,item.text,item.urgency,item.times.filed.replace(tzinfo=None),
        item.value,item.queue_after) for item in selected]


def count_items(items: list[ReviewItem], now: datetime, source: str = '') -> int:
    """Count records whose next action belongs to the user."""
    return sum(item.for_you(now) and (not source or item.source_unit == source) for item in items)
