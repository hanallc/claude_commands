"""Read the five review stores through one model of whose turn it is."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import json
import logging
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
from scripts.review_links import efficiency_reviews, new_item_dialog, review_events, review_later, review_submissions
from scripts.review_links.review_events import ReviewLog as ReviewLog, ReviewMark


def source_stamp(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


class ReviewSourceStamps(list[Path]):
    """Record each source and its parent before a snapshot reads them."""
    def __init__(self) -> None:
        super().__init__()
        self.before_read: dict[Path, tuple[int, int]] = {}
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
                 skip: tuple[type[Exception], ...] = (), entry: str = '') -> Generator[None]:
    """Track the source and isolate read failures during Review scans."""
    if isinstance(sources, ReviewSourceStamps):
        sources.append(path)
    try:
        yield
    except Exception as error:
        source = error.path if isinstance(error, change_routing.ProductionPathUnreadable) else path
        if isinstance(sources, ReviewSourceStamps):
            sources.skipped(source, entry, error)
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


class ReviewState(StrEnum):
    OPEN = 'Open'
    VALIDATING = 'Validating'
    WAITING = 'Waiting'
    DONE = 'Done'


REVIEW_TYPES = tuple(ReviewType)
INVESTIGATION_TYPES = (ReviewType.INVESTIGATION, ReviewType.BUG)
REVIEW_STATES = tuple(ReviewState)
PROPOSAL_STATE_FIELDS = frozenset(("status", "stage", "unit", "style_answer", "style_answer_words", "proposed_by", "production_id", "record_type",
                                   REVIEW_MARK_FIELD))
# Approve and Decline close the item; Send feedback closes it; Ask question returns it.
DECISION_ACTIONS = ('approve', 'decline', 'feedback', 'question')


def selected_states(show: str) -> tuple[ReviewState, ...]:
    """Select the states before a store loads complete records."""
    if show == 'all':
        return REVIEW_STATES
    if show in ('open', 'For you', 'for-you', 'for you'):
        return (ReviewState.OPEN,)
    return (ReviewState(show),)


@dataclass(frozen=True)
class NoLaterTime:
    """The record has no date that hides it."""


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


def read_change_delivery(path: Path, *, sources: list[Path] | None = None) -> delivery_state.DeliverySlots:
    """Read change exchanges with their request type."""
    if sources is not None:
        sources.append(path)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    try:
        value = cast(object, json.loads(content))
    except ValueError:
        if sources is not None:
            raise
        return delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    return decode_change_delivery(value)


@dataclass(frozen=True)
class ChangeSource:
    path: Path
    row: dict[str, str]
    assignment: change_routing.ChangeAssignment
    slots: delivery_state.DeliverySlots


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


type ReviewSource = (ChangeSource | QuestionSource | UnownedQuestionSource | InvestigationSource
                     | ProposalSource | ProposalLedgerSource | EfficiencySource)


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

    def for_you(self, now: datetime) -> bool:
        """Count Open records whose next action belongs to the user."""
        return self.state == ReviewState.OPEN and not self.hidden_until(now)

    def hidden_until(self, now: datetime) -> bool:
        return not isinstance(self.times.later, NoLaterTime) and self.times.later > now


def source_path(item: ReviewItem) -> Path:
    """Return the path to watch if this record cannot be projected."""
    source = item.source
    if isinstance(source, (ProposalSource, QuestionSource, UnownedQuestionSource)):
        return source.record.path
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


def moment(value: str) -> datetime:
    """Read stored times in the user's zone when the store has no zone."""
    parsed = datetime.fromisoformat(value)
    return aware(parsed, user_zone())


def aware(value: datetime, zone: ZoneInfo) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=zone)


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


def change_state(row: dict[str, str], slots: delivery_state.DeliverySlots) -> ReviewState:
    if row.get('Verdict'):
        return ReviewState.DONE
    return ReviewState.OPEN if slots.waits_for_user(False) else ReviewState.VALIDATING


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


def change_item(path: Path, row: dict[str, str], assignment: change_routing.ChangeAssignment,
                members: staff.StaffSnapshot, queue: str, current: Path,
                slots: delivery_state.DeliverySlots) -> ReviewItem:
    """Project a change once for the page, counts and queue."""
    prefix = path.stem.removeprefix(change_review.PREFIX)
    routed = assignment.owner
    owner = next((member for member in members.rows if member.name == routed.tab), None)
    identity = owner.id if owner is not None else staff.StaffId(routed.tab)
    source = staff.StaffId(row.get('Producer', '') or identity)
    state = change_state(row, slots)
    entry = change_review.row_entries([row])[0]
    filed = entry.since.replace(tzinfo=user_zone()) if entry.since is not None else moment('1970-01-01')
    logs = [ReviewLog(filed, name(source, members), 'Filed for your check')]
    saved_log = review_events.decode(row.get('Review log', ''))
    words = {entry.words.casefold() for entry in saved_log}
    request = slots.question
    if isinstance(request, (delivery_state.OpenQuestion, delivery_state.AnsweredQuestion)) and not isinstance(
            request, (OpenFeedback, CompletedFeedback)):
        event = 'Question: ' + request.text
        if event.casefold() not in words:
            logs.append(ReviewLog(filed, 'nate', event))
    if isinstance(request, delivery_state.AnsweredQuestion):
        answer = 'Answer: ' + request.answer
        if answer.casefold() not in words:
            logs.append(ReviewLog(filed, name(identity, members), answer))
    if slots.validation_requested and not any(entry.words.startswith('validate') for entry in saved_log):
        logs.append(ReviewLog(filed, 'nate', 'Asked the showrunner to check'))
    if row.get('Verdict') and row['Verdict'].casefold() not in words and not any(
            entry.words.startswith('feedback sent:') for entry in saved_log):
        logs.append(ReviewLog(filed, 'nate', row['Verdict']))
    logs.extend(saved_log)
    shown = change_review.display_id(row, routed.tab)
    after = entry.after if '/' in entry.after else prefix+'/'+entry.after if entry.after else ''
    local = path.absolute() == current.absolute()
    return ReviewItem(prefix+'/'+row['#'], shown, ReviewType.CHANGE, identity, source,
        row['Change'], state, ReviewTimes(filed, logs[-1].time), tuple(logs),
        ('works', 'not works', 'validate', 'feedback', 'question') if state == ReviewState.OPEN else (),
        ChangeSource(path, row, assignment, slots), entry.urgency, after, queue,
        row['#'] if local else prefix+'/'+row['#'], change_review.prerequisite_key(entry.after, path, current),
        entry.value, change_review.describe(row | {'#':shown},record=members),
        review_events.waiting_why(slots.waiting_why))


def change_queue_name(doc: Path, members: staff.StaffSnapshot, current: Path, below: bool = False) -> str:
    owner = unit_lookup.doc_owner(doc, members)
    queue = change_review.queue(current)
    if owner.name != showrunner_tree.root_of(owner.name, record=members):
        queue += '-'+owner.name
    return urgency.scope_queue(queue, below)


def change_items(root: Path, members: staff.StaffSnapshot, *, document: Path | None = None,
                 below: bool = False, sources: list[Path] | None = None,
                 states: tuple[ReviewState, ...] = REVIEW_STATES,
                 owner_options: dict[staff.StaffId, str] | None = None,
                 selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    if document is not None:
        current = change_review.scope_list(document, record=members, sources=sources)
        queue = change_queue_name(document, members, current, below)
        routed = (change_review.routed_review_entries(current,document,below,record=members,sources=sources) if below else
                  change_review.routed_review_entries(current,document,record=members,sources=sources) if sources is not None else
                  change_review.routed_review_entries(current,document,record=members))
        scoped: list[ReviewItem] = []
        for listing, owner, row in routed:
            if ReviewState.OPEN not in states:
                continue
            path = change_routing.delivery_path(root, listing.path, row['#'])
            with guard_source(path, sources, entry=row['#']):
                slots = read_change_delivery(path, sources=sources)
                scoped.append(change_item(listing.path, row, change_routing.ChangeAssignment(owner, document),
                                         members, queue, current, slots))
        return scoped
    folder = change_review.lists_directory()
    if isinstance(selection, ExactReviewRecord):
        paths = [selection.path]
    else:
        paths = source_paths(folder, change_review.PREFIX+'*.md', sources=sources)
    snapshots: dict[Path, change_review.ReviewListSnapshot] = {}
    for path in sorted(paths):
        with guard_source(path, sources):
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
                slots = read_change_delivery(delivery_path, sources=sources)
                if change_state(row, slots) not in states:
                    continue
                assignment = routing.owner_and_production(path, row)
                tab = assignment.owner.tab
                doc = routing.production(tab)
                queue_source = (tab, path if doc is None else doc)
                if queue_source not in queues:
                    current = change_review.scope_list(doc, record=members, sources=sources) if doc is not None else path
                    queue = change_queue_name(doc, members, current) if doc is not None else change_review.queue(path)
                    queues[queue_source] = queue, current
                queue, current = queues[queue_source]
                found.append(change_item(path, row, assignment, members, queue, current, slots))
    return found


def question_item(record: update_registration.QuestionRecord, members: staff.StaffSnapshot, *,
                  unowned: bool = False, assign_ids: bool = False) -> ReviewItem:
    owner, fields, path = record.owner, record.item, record.path
    parsed = update_registration.parse_outstanding_entries([fields], path)[0]
    filed = moment(fields['since'])
    entry = update_registration.question_entry(owner, fields, path)
    asker = staff.StaffId(fields.get('asked_by', ''))
    state = question_state(record)
    after = fields.get('after','')
    later = moment(after) if after else NoLaterTime()
    logs = [ReviewLog(filed, name(asker, members), 'Asked')]
    saved_log = review_events.decode(fields.get('review_log', ''))
    words = {entry.words.casefold() for entry in saved_log}
    for exchange in fields.get('followups', []):
        if not any((prefix+exchange['followup']).casefold() in words for prefix in ('question: ','followup: ')):
            logs.append(ReviewLog(filed,'nate',exchange['followup']))
        if ('Answer: '+exchange['reply']).casefold() not in words:
            logs.append(ReviewLog(filed,name(asker,members),exchange['reply']))
    if 'followup' in fields and not any((prefix+fields['followup']['text']).casefold() in words
                                      for prefix in ('question: ','followup: ')):
        logs.append(ReviewLog(moment(fields['followup']['sent']), 'nate', fields['followup']['text']))
    logs.extend(saved_log)
    source = UnownedQuestionSource(record) if unowned else QuestionSource(record)
    key = 'question/'+owner+'/'+path.name
    review_id = fields.get('review_id', path.stem)
    if assign_ids:
        try:
            review_id = update_registration.question_review_id(record)
            fields['review_id'] = review_id
        except (OSError, ValueError, staff.StaffRefused, update_registration.RegistrationFailure):
            review_id = ''
    identity = name(owner, members)+'/'+review_id if review_id else ''
    opened = update_registration.OpenItem(name(owner, members), parsed, name(asker, members) if asker else '')
    return ReviewItem(key, identity, ReviewType.QUESTION, owner, asker, fields['text'], state,
        ReviewTimes(filed, logs[-1].time, later), tuple(logs),
        ('answer', 'feedback', 'question') if state != ReviewState.DONE and not unowned else (), source, entry.urgency, '',
        update_registration.question_queue_name(owner), entry.key, entry.after, entry.value, opened.line(),
        review_events.waiting_why(fields.get('waiting_why', '')))


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


def question_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   owners: tuple[staff.StaffRow, ...] = (), sources: list[Path] | None = None,
                   include_unowned: bool = True, assign_ids: bool = False,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
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
        item = (question_item(record, members, unowned=unowned, assign_ids=True) if assign_ids
                else question_item(record, members, unowned=unowned))
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
                        selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
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
                    record = investigation.read_review_record(path)
                    if record.owner != identity or investigation_type(record) not in kinds:
                        continue
                    if owner_options is not None:
                        for owner in (record.source_owner, owner_for(identity, members)):
                            owner_options[owner] = name(owner, members)
                    if investigation_state(record) in states:
                        found.append(investigation_item(record, members, path))
    return found


def investigation_type(record: investigation.Investigation) -> ReviewType:
    return ReviewType.BUG if record.kind == investigation.InvestigationKind.BUG else ReviewType.INVESTIGATION


def investigation_item(record: investigation.Investigation, members: staff.StaffSnapshot, path: Path) -> ReviewItem:
    filed = moment(record.filed)
    logs = [ReviewLog(filed, name(record.owner, members), 'Filed')]
    if record.answered and not any(entry.words.startswith('Answer: ') for entry in record.log):
        logs.append(ReviewLog(moment(record.answered), name(record.owner, members), 'Answer ready'))
    if record.read and not any(entry.words.startswith(('Marked read','feedback sent:')) for entry in record.log):
        logs.append(ReviewLog(moment(record.read), 'nate', 'Marked read'))
    logs.extend(record.log)
    state = investigation_state(record)
    owner = owner_for(record.owner, members)
    key = 'investigation/'+record.owner+'/'+record.id
    identity_text = name(record.owner, members)+'/'+record.id
    return ReviewItem(key, identity_text, investigation_type(record), owner, record.source_owner, record.title, state,
        ReviewTimes(filed, logs[-1].time), tuple(logs),
        ('read', 'feedback', 'question') if state != ReviewState.DONE else (), InvestigationSource(record, path),
        queue='investigation-'+owner, queue_key=key,
        text=identity_text+' '+record.title+'\n'+record.question+'\n\nAnswer\n'+record.answer,
        waiting_why=review_events.waiting_why(record.waiting_why))


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
               sources: list[Path] | None = None, prerequisite: Callable[[Path, str], str] | None = None) -> ReviewItem:
    """Project a proposal for the Review index and its body."""
    issue = Issue(proposal_path(issue.path), issue.fields)
    row = proposals_table.issue_row(issue, members, sources=sources)
    key = 'proposal/'+str(issue.path)
    if not isinstance(row.progress, proposal_issue.AwaitingApproval):
        filed = moment(issue.value('date_created') or '1970-01-01')
        return proposal_item(key, issue, owner, staff.StaffId(issue.value('proposed_by') or owner),
                             issue.path.stem, filed, members)
    after = prerequisite(issue.path, row.entry.after) if prerequisite is not None and row.entry.after else ''
    entry = replace(row.entry, after=after)
    filed = entry.since.replace(tzinfo=user_zone()) if entry.since is not None else moment('1970-01-01')
    return proposal_item(key, issue, owner, staff.StaffId(issue.value('proposed_by')), issue.path.stem, filed,
                         members, entry, 'proposal-'+owner)


def submission_item(saved: review_submissions.SavedSubmission, members: staff.StaffSnapshot) -> ReviewItem:
    """Project the native record returned by Send."""
    record = saved.record
    if isinstance(record, update_registration.QuestionRecord):
        return question_item(record, members)
    if isinstance(record, Issue):
        return issue_item(record, saved.owner, members, sources=[])
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


def proposal_store_folders(owner: staff.StaffId, member: staff.StaffRow | None, doc: Path | None,
                           sources: list[Path] | None) -> tuple[Path, ...]:
    current: tuple[Path, ...] = ()
    if member is not None and member.kind == 'producer' and isinstance(member.doc, staff.LiveDoc):
        with guard_source(member.doc.path, sources, skip=(FileNotFoundError,)):
            folder = review_submissions.proposal_folder(member)
            if isinstance(folder, Path):
                current = (folder,)
    return record_store.known_folders(record_store.RecordKind.PROPOSAL, owner, current, document=doc,
                                      strict=sources is not None, sources=sources)


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
                   selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
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
            member = members.by_id(owner)
            doc = scope if isinstance(scope, Path) else record_store.local_document(member)
            if exact:
                if selection.key == 'proposal/'+str(selection.path):
                    paths_by_scope.setdefault(selection.path, []).append(scope)
                continue
            with guard_source(doc if doc is not None else state_root(), sources):
                for folder in proposal_store_folders(owner, member, doc, sources):
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
                fields = (issue_store.frontmatter(path, sources=sources) if exact else
                          record_store.read_header(path, sources=sources, strict=sources is not None))
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
                item = issue_item(issue, owner, members, sources=sources, prerequisite=prerequisite)
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
                        filed = since.replace(tzinfo=user_zone()) if since is not None else moment('1970-01-01')
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


def efficiency_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
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
                        item = efficiency_item(path, index, line, review, owner, members)
                        if item.state not in states:
                            continue
                        if owner_options is not None:
                            owner_options[owner] = name(owner,members)
                        found.append(item)
    return found


def efficiency_item(path: Path, index: int, line: str, review: efficiency_reviews.EfficiencyReview,
                    owner: staff.StaffId, members: staff.StaffSnapshot) -> ReviewItem:
    head, _, result = line.partition(' — ')
    state = efficiency_state(result, review)
    filed = moment(head[2:12])
    candidates_found = candidates.entries([head+' — proposed'])
    entry = candidates_found[0] if candidates_found else urgency.Entry(head,line)
    candidate = candidates_found[0].item if candidates_found else None
    who = staff.StaffId(candidate.by if candidate is not None else '')
    key = f'efficiency/{path}/{index}'
    logs = (ReviewLog(filed,name(owner,members),result), *review.log)
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
    prerequisite = Path(item.queue_after) if isinstance(source, ProposalSource) and item.queue_after else None
    selection = ExactReviewRecord(source_path(item), item.key, item.owner, prerequisite)
    if isinstance(source, ProposalSource) and 'review_submission' in source.record.fields:
        return read_sent_proposal(selection, source.record, members)
    try:
        records = project(root, members, (item.type,), REVIEW_STATES, None, [], selection)
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

    item, = with_later([issue_item(issue, owner, members, sources=sources, prerequisite=prerequisite)], sources)
    return normalize_times(item, user_zone())


def entry(item: ReviewItem) -> urgency.Entry[ReviewItem]:
    """Use the existing queue keys for pins, moves and prerequisites."""
    return urgency.Entry(item.queue_key,item,item.urgency,item.times.filed,item.value,item.queue_after)


def ordered(items: list[ReviewItem], sources: list[Path] | None = None) -> list[ReviewItem]:
    """Apply queue pins and moves before combining the records."""
    zone = user_zone()
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
         sources: list[Path] | None = None, assign_ids: bool = True) -> list[ReviewItem]:
    """Read each record from its existing store without another stored copy."""
    members = staff.snapshot() if members is None else members
    return project(root, members, kinds, selected_states(show), owner_options, sources, ALL_RECORDS, assign_ids=assign_ids)


def project(root: Path, members: staff.StaffSnapshot, kinds: tuple[ReviewType, ...], states: tuple[ReviewState, ...],
            owner_options: dict[staff.StaffId, str] | None, sources: list[Path] | None,
            selection: ReviewSelection, *, assign_ids: bool = False) -> list[ReviewItem]:
    """Select the requested records and apply the shared projection."""
    documents = tuple(dict.fromkeys(doc for member in members.rows if isinstance(member.doc, staff.LiveDoc)
                                    if (doc := record_store.local_document(member)) is not None))
    items: list[ReviewItem] = []
    if ReviewType.QUESTION in kinds:
        items.extend(question_items(members,documents,states=states,owner_options=owner_options,
                                   sources=sources,selection=selection,assign_ids=assign_ids))
    if ReviewType.CHANGE in kinds:
        items.extend(change_items(root,members,states=states,owner_options=owner_options,sources=sources,selection=selection))
    reports = tuple(kind for kind in kinds if kind in INVESTIGATION_TYPES)
    if reports:
        items.extend(investigation_items(members,states=states,kinds=reports,owner_options=owner_options,sources=sources,selection=selection))
    if ReviewType.PROPOSAL in kinds:
        items.extend(proposal_items(members,documents,states=states,owner_options=owner_options,sources=sources,include_registered=True,selection=selection))
    if ReviewType.EFFICIENCY in kinds:
        items.extend(efficiency_items(members,documents,states=states,owner_options=owner_options,sources=sources,selection=selection))
    return ordered(with_later(items,sources), sources)


def read_open(root: Path) -> list[ReviewItem]:
    """Share only open records during one console news request."""
    return read(root, show='open')


def with_later(items: list[ReviewItem], sources: list[Path] | None = None) -> list[ReviewItem]:
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
        times = review_later.times(sources, skipped=skip_time if sources is not None else None)
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
              own: list[Issue] | None = None, show: str = 'all') -> list[ReviewItem]:
    return with_later(_read_kind(kind,scope,members,below=below,sources=sources,own=own,states=selected_states(show)),sources)


def _read_kind(kind: ReviewType, scope: Path | staff.StaffRow, members: staff.StaffSnapshot, *,
              below: bool = False, sources: list[Path] | None = None,
              own: list[Issue] | None = None,
              states: tuple[ReviewState, ...] = REVIEW_STATES) -> list[ReviewItem]:
    """Read the selected store into ReviewItem records for all consumers."""
    if isinstance(scope,staff.StaffRow):
        if kind in (ReviewType.CHANGE, ReviewType.PROPOSAL, ReviewType.EFFICIENCY):
            return []
        documents: tuple[Path,...] = ()
        owner = scope
    else:
        documents = tuple(proposals_folder.scope_docs(scope,below,record=members))
        owner = scope
    if kind == ReviewType.QUESTION:
        owners = (scope,) if isinstance(scope,staff.StaffRow) else tuple(member for member in members.rows
            if member.kind == 'showrunner' and isinstance(member.doc,(staff.LiveDoc,staff.WrappedDoc)) and member.doc.path in documents)
        return question_items(members,documents,owners=owners,sources=sources,include_unowned=False,states=states)
    if kind in INVESTIGATION_TYPES:
        owners_ids = tuple(member.id for doc in documents for member in
            (unit_lookup.doc_owner(doc,members),*unit_lookup.doc_units(doc,members))) if documents else (cast(staff.StaffRow,owner).id,)
        return investigation_items(members,owners=owners_ids,sources=sources,states=states,
                                   kinds=INVESTIGATION_TYPES if kind == ReviewType.INVESTIGATION else (kind,))
    if kind == ReviewType.PROPOSAL:
        return proposal_items(members,documents,sources=sources,own=own,states=states)
    if kind == ReviewType.EFFICIENCY:
        return efficiency_items(members,documents,sources=sources,states=states)
    assert isinstance(scope,Path)
    root = change_routing.review_root() if sources is None else change_routing.review_root(sources=sources)
    items = change_items(root,members,document=scope,below=below,sources=sources,states=states)
    tabs = change_review.review_scope(scope,below,members)
    return [item for item in items if isinstance(item.source,ChangeSource) and
            (item.source.assignment.owner.tab in tabs or isinstance(item.source.assignment.owner,change_routing.UnroutedChange))]


def change_counts(doc: Path, record: staff.StaffSnapshot, *, source: str = '',
                  sources: list[Path] | None = None) -> tuple[int,int]:
    try:
        items = read_kind(ReviewType.CHANGE,doc,record,sources=sources,show='open')
        now = datetime.now(user_zone())
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
    return sum(item.for_you(datetime.now(user_zone())) and (not source or item.source_unit == source)
               for item in read_kind(ReviewType.PROPOSAL,doc,record,sources=sources,own=own,show=ReviewState.OPEN))


def open_efficiency(line: str, *, source: str = '') -> bool:
    """Retain the legacy line predicate at its public boundary."""
    match = EFFICIENCY_LINE.match(line)
    return match is not None and match['status'].startswith('proposed') and (
        not source or match['head'].rstrip().endswith(f'[by: {source}]'))


def efficiency(doc: Path, *, source: str = '', sources: list[Path] | None = None,
               record: staff.StaffSnapshot | None = None) -> int:
    record = staff.snapshot() if record is None else record
    try:
        return sum(item.for_you(datetime.now(user_zone())) and (not source or item.source_unit == source)
                   for item in read_kind(ReviewType.EFFICIENCY,doc,record,sources=sources,show=ReviewState.OPEN))
    except (OSError,UnicodeError):
        if sources is not None:
            raise
        return 0


def investigations(doc: Path, record: staff.StaffSnapshot, *, source: str = '', sources: list[Path] | None = None) -> int:
    try:
        items = read_kind(ReviewType.INVESTIGATION,doc,record,sources=sources,show='open')
    except staff.StaffUnreadable:
        return 0
    return count_items(items,datetime.now(user_zone()),source)


def question_counts(owner: staff.StaffRow, zone: ZoneInfo, identities: QuestionStaff, sources: list[Path] | None,
                    source: str = '') -> tuple[int,str]:
    items = read_kind(ReviewType.QUESTION,owner,identities.record,sources=sources,show='open')
    now = datetime.now(zone)
    afters = [item.times.later for item in items if not isinstance(item.times.later,NoLaterTime) and item.times.later > now]
    return (sum(item.for_you(now) and (not source or item.source_unit == source) for item in items),
            '' if not afters else str(int(min(afters).timestamp())))


def ready_investigations(owner: staff.StaffId, *, sources: list[Path] | None = None) -> int:
    items = with_later(investigation_items(staff.snapshot(),owners=(owner,),sources=sources,states=(ReviewState.OPEN,)),sources)
    return sum(item.for_you(datetime.now(user_zone())) for item in items)


def next_queue(kind: str, scope: Path | staff.StaffRow, below: bool, members: staff.StaffSnapshot) -> tuple[str,list[urgency.Entry[str]]]:
    items = read_kind(ReviewType(kind),scope,members,below=below,show='open')
    now = datetime.now(user_zone())
    selected = [item for item in items if item.for_you(now) and not
                (isinstance(item.source,ChangeSource) and isinstance(item.source.assignment.owner,change_routing.UnroutedChange))]
    if isinstance(scope, staff.StaffRow) and kind in ('change', 'proposal', 'efficiency'):
        queue = urgency.scope_queue(kind+'-'+scope.id,below)
    elif kind == 'change':
        assert isinstance(scope,Path)
        queue = change_queue_name(scope,members,change_review.scope_list(scope,record=members),below)
    elif kind == 'question':
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
