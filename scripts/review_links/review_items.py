"""Read the five review stores through one model of whose turn it is."""
from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
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
from scripts.production import proposals_folder, proposals_table
from scripts.production.record_store import state_root
from scripts.production.issues import Issue, PROPOSAL_TAG, source_paths
from scripts.production.proposal_issue import AwaitingApproval, DeclinedDecision, InProgress, decision_answer, proposal_progress
from scripts.production.proposal_issue import REVIEW_MARK_FIELD, review_log, review_mark
from scripts.production.production_doc import production_field, Refusal
from scripts.review_links import efficiency_reviews, review_events, review_later
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
    """Track the source first and isolate its failure during snapshot reads."""
    if isinstance(sources, ReviewSourceStamps):
        sources.append(path)
    try:
        yield
    except Exception as error:
        if isinstance(sources, ReviewSourceStamps):
            source = error.path if isinstance(error, change_routing.ProductionPathUnreadable) else path
            sources.skipped(source, entry, error)
        elif isinstance(error, skip):
            logging.getLogger(__name__).warning('Review source %s: %s', path, error)
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


class ReviewState(StrEnum):
    OPEN = 'Open'
    WAITING = 'Waiting'
    DONE = 'Done'


REVIEW_TYPES = tuple(ReviewType)
REVIEW_STATES = tuple(ReviewState)
PROPOSAL_STATE_FIELDS = frozenset(("status", "stage", "unit", "style_answer", "style_answer_words", "proposed_by", "record_type",
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
    if isinstance(source, InvestigationSource):
        return investigation.folder(source.record.owner)/(source.record.id+'.md')
    return source.path


@dataclass(frozen=True)
class ExactReviewRecord:
    """Select one record and its production sources."""

    path: Path
    key: str
    owner: staff.StaffId

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
    return ReviewState.OPEN if slots.waits_for_user(False) else ReviewState.WAITING


def question_state(record: update_registration.QuestionRecord) -> ReviewState:
    if record.status == 'closed':
        return ReviewState.DONE
    return ReviewState.WAITING if 'followup' in record.item else ReviewState.OPEN


def investigation_state(item: investigation.Investigation) -> ReviewState:
    return {investigation.InvestigationState.OPEN: ReviewState.WAITING,
            investigation.InvestigationState.ANSWER_READY: ReviewState.OPEN,
            investigation.InvestigationState.READ: ReviewState.DONE}[item.state]


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
    if isinstance(slots.question, delivery_state.OpenQuestion):
        if ('question: '+slots.question.text).casefold() not in words:
            logs.append(ReviewLog(filed, 'nate', 'Question: ' + slots.question.text))
    elif isinstance(slots.question, delivery_state.AnsweredQuestion):
        if ('question: '+slots.question.text).casefold() not in words:
            logs.append(ReviewLog(filed, 'nate', 'Question: ' + slots.question.text))
        if ('answer: '+slots.question.answer).casefold() not in words:
            logs.append(ReviewLog(filed, name(identity, members), 'Answer: ' + slots.question.answer))
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
        ('works', 'not works', 'validate', 'feedback', 'question') if state != ReviewState.DONE else (),
        ChangeSource(path, row, assignment, slots), entry.urgency, after, queue,
        row['#'] if local else prefix+'/'+row['#'], change_review.prerequisite_key(entry.after, path, current),
        entry.value, change_review.describe(row | {'#':shown},record=members))


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
                slots = change_routing.delivery_slots(path, sources=sources)
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
                slots = change_routing.delivery_slots(delivery_path, sources=sources)
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


def question_item(record: update_registration.QuestionRecord, members: staff.StaffSnapshot, *, unowned: bool = False) -> ReviewItem:
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
    identity = name(owner, members)+'/'+fields.get('review_id',path.stem)
    opened = update_registration.OpenItem(name(owner, members), parsed, name(asker, members) if asker else '')
    return ReviewItem(key, identity, ReviewType.QUESTION, owner, asker, fields['text'], state,
        ReviewTimes(filed, logs[-1].time, later), tuple(logs),
        ('answer', 'feedback', 'question') if state != ReviewState.DONE and not unowned else (), source, entry.urgency, '',
        update_registration.question_queue_name(owner), entry.key, entry.after, entry.value, opened.line())


def question_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   owners: tuple[staff.StaffRow, ...] = (), sources: list[Path] | None = None,
                   include_unowned: bool = True,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
                   owner_options: dict[staff.StaffId, str] | None = None,
                   selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    identities = update_registration.QuestionStaff.for_record(members)
    selected = owners or tuple(member for member in members.rows if member.kind == 'producer' or
        member.kind == 'showrunner' and isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path in documents)
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
        found.append(question_item(record, members, unowned=unowned))

    def read_folder(location: Path, owner: str = '', *, unowned: bool = False) -> None:
        with guard_source(location, sources):
            if isinstance(selection, ExactReviewRecord):
                paths = [selection.path] if selection.path.parent == location else []
            else:
                paths = source_paths(location, 'question-*.md', sources=sources)
            for path in paths:
                with guard_source(path, sources):
                    for record in update_registration.selected_question_records(
                            [path], statuses, identities_found, sources, owner):
                        append(record, unowned=unowned)

    for owner in selected:
        if isinstance(owner.doc,(staff.LiveDoc,staff.WrappedDoc)) and owner.doc.machine != local_machine():
            print(f'questions: {owner.name}: ownership evidence is unavailable',file=sys.stderr)
            continue
        document = owner if owner.kind == 'producer' else cast(staff.LiveDoc, owner.doc).path
        if sources is not None and isinstance(document, Path):
            sources.append(document)
        if isinstance(sources, ReviewSourceStamps) or isinstance(selection, ExactReviewRecord):
            with guard_source(document if isinstance(document, Path) else state_root(), sources):
                folder = update_registration.question_folder(document, identities)
                if isinstance(folder, update_registration.QuestionFolder):
                    read_folder(folder.path, folder.owner)
                else:
                    location = proposals_folder.issues_folder(folder.document)
                    if location is not None:
                        read_folder(location)
        else:
            for record in update_registration.question_records_for_document(
                    document, identities, include_unrouted=True, sources=sources,
                    statuses=statuses, owner_ids=identities_found):
                append(record)
    if include_unowned:
        if isinstance(sources, ReviewSourceStamps) or isinstance(selection, ExactReviewRecord):
            root = state_root()
            owned = {row.id for row in members.rows if row.kind == 'showrunner'}
            with guard_source(root, sources):
                paths = [selection.path.parent.parent] if isinstance(selection, ExactReviewRecord) else sorted(root.glob('*'))
                for path in paths:
                    with guard_source(path, sources):
                        if path.parent == root and path.is_dir() and path.name not in owned:
                            read_folder(path/'questions', unowned=True)
        else:
            for record in update_registration.unowned_question_records(identities, statuses=statuses, owner_ids=identities_found):
                if sources is not None:
                    sources.extend((record.path,record.path.parent))
                append(record, unowned=True)
    if owner_options is not None:
        for identity in identities_found:
            member = members.by_id(identity)
            owner_options[identity] = name(identity, members) if member is not None else 'Owner unavailable'
    return found


def investigation_items(members: staff.StaffSnapshot, *, owners: tuple[staff.StaffId, ...] = (),
                        sources: list[Path] | None = None,
                        states: tuple[ReviewState, ...] = REVIEW_STATES,
                        owner_options: dict[staff.StaffId, str] | None = None,
                        selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    root = investigation.state_root()
    selected = owners
    with guard_source(root, sources):
        if isinstance(selection, ExactReviewRecord):
            owner_folder = selection.path.parent.parent
            identity = staff.StaffId(owner_folder.name)
            selected = (identity,) if owner_folder.parent == root and selection.path.parent == investigation.folder(identity) else ()
        elif not selected and root.exists():
            selected = tuple(staff.StaffId(path.name) for path in sorted(root.iterdir())
                             if path.is_dir() and not path.is_symlink())
    found: list[ReviewItem] = []
    investigation_states = tuple(state for state,review_state in (
        (investigation.InvestigationState.OPEN, ReviewState.WAITING),
        (investigation.InvestigationState.ANSWER_READY, ReviewState.OPEN),
        (investigation.InvestigationState.READ, ReviewState.DONE)) if review_state in states)
    for identity in selected:
        folder = investigation.folder(identity)
        with guard_source(folder, sources):
            if owner_options is not None and any(folder.glob('I*.md')):
                for owner in (identity, owner_for(identity, members)):
                    owner_options[owner] = name(owner, members)
            if isinstance(sources, ReviewSourceStamps) or isinstance(selection, ExactReviewRecord):
                paths = ([selection.path] if isinstance(selection, ExactReviewRecord)
                         else source_paths(folder, 'I*.md', sources=sources))
                for path in paths:
                    with guard_source(path, sources):
                        if isinstance(selection, ExactReviewRecord):
                            _ = path.stat()
                        record = investigation.load(identity, path.stem, sources=sources)
                        if record.state in investigation_states:
                            found.append(investigation_item(record, members))
            else:
                for record in investigation.records(identity, include_read=True, sources=sources, states=investigation_states):
                    found.append(investigation_item(record, members))
    return found


def investigation_item(record: investigation.Investigation, members: staff.StaffSnapshot) -> ReviewItem:
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
    return ReviewItem(key, identity_text, ReviewType.INVESTIGATION, owner, record.owner, record.title, state,
        ReviewTimes(filed, logs[-1].time), tuple(logs),
        ('read', 'feedback', 'question') if state != ReviewState.DONE else (), InvestigationSource(record),
        queue='investigation-'+owner, queue_key=key,
        text=identity_text+' '+record.title+'\n'+record.question+'\n\nAnswer\n'+record.answer)


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
                          actions,ProposalSource(issue))
    return ReviewItem(key,identity,ReviewType.PROPOSAL,owner,who,title,state,ReviewTimes(filed,logs[-1].time),logs,
                      actions,ProposalSource(issue),entry.urgency,'proposal/'+entry.after if entry.after else '',queue,
                      entry.key,entry.after,entry.value,proposals_table.proposal_text(entry))



def is_open_proposal(issue: Issue) -> bool:
    return (issue.value('record_type') != 'question' and issue.value('status') == 'open'
            and PROPOSAL_TAG in issue.fields.get('tags', []))


def proposal_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   sources: list[Path] | None = None, own: list[Issue] | None = None,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
                   owner_options: dict[staff.StaffId, str] | None = None,
                   selection: ReviewSelection = ALL_RECORDS) -> list[ReviewItem]:
    reads_issues = isinstance(selection, AllReviewRecords) or selection.key == 'proposal/'+str(selection.path)
    exact_fields: dict[str, list[str]] = {}
    if isinstance(selection, ExactReviewRecord):
        owner = selection.owner
        if reads_issues:
            exact_fields = issue_store.frontmatter(selection.path, sources=sources, select=is_open_proposal,
                                                   keys=PROPOSAL_STATE_FIELDS | {'tags'})
            if not exact_fields:
                return []
            proposer = members.by_id(staff.StaffId(Issue(selection.path, exact_fields).value('proposed_by')))
            if proposer is None:
                return []
            if proposer.kind == 'unit':
                proposer = members.parent_of(proposer)
            if proposer is None or proposer.kind != 'showrunner':
                return []
            owner = proposer.id
        member = members.by_id(owner)
        documents = (member.doc.path,) if member is not None and isinstance(member.doc, staff.LiveDoc) else ()
    found: dict[str, ReviewItem] = {}
    owners: dict[Path, staff.StaffId] = {}
    for doc in documents:
        try:
            owners[doc] = unit_lookup.doc_owner(doc, members).id
        except staff.StaffUnreadable:
            owners[doc] = staff.StaffId('')
    proposers = {doc:{owner, *(member.id for member in members.find(kind='unit', parent=owner))}
                 for doc, owner in owners.items()}
    references: dict[Path, dict[str, str]] = {doc:{} for doc in documents}
    selected_issues: dict[Path, list[Issue]] = {doc:[] for doc in documents}
    if own is None and reads_issues:
        folders: dict[Path, list[Path]] = {}
        for doc in documents:
            with guard_source(doc, sources):
                folder = proposals_folder.issues_folder(doc)
                if folder is not None and (isinstance(selection, AllReviewRecords) or selection.path.parent == folder):
                    folders.setdefault(folder, []).append(doc)
        for folder, scoped_documents in folders.items():
            documents_by_proposer: dict[str, list[Path]] = {}
            for doc in scoped_documents:
                for proposer in proposers[doc]:
                    documents_by_proposer.setdefault(proposer, []).append(doc)

            def selected(issue: Issue) -> bool:
                if not is_open_proposal(issue):
                    return False
                scoped = documents_by_proposer.get(issue.value('proposed_by'), [])
                for doc in scoped:
                    references[doc][issue.path.stem] = str(issue.path)
                return bool(scoped) and proposal_state(issue) in states

            paths: list[Path] = []
            if isinstance(selection, ExactReviewRecord):
                paths = [selection.path]
            else:
                with guard_source(folder, sources):
                    paths = source_paths(folder, '*.md', sources=sources)
            for path in sorted(paths):
                with guard_source(path, sources, skip=(ValueError, FileNotFoundError)):
                    if not path.is_file():
                        continue
                    if isinstance(selection, ExactReviewRecord):
                        fields = exact_fields if selected(Issue(path, exact_fields)) else {}
                    else:
                        fields = issue_store.frontmatter(path, sources=sources, select=selected,
                                                         keys=PROPOSAL_STATE_FIELDS | {'tags'})
                    if not fields:
                        continue
                    issue = Issue(path, fields)
                    for doc in documents_by_proposer[issue.value('proposed_by')]:
                        selected_issues[doc].append(issue)
                    if isinstance(selection, ExactReviewRecord):
                        dependency = issue.value('depends_on')
                        if dependency and Path(dependency).name == dependency and dependency != path.stem:
                            prerequisite = folder/(dependency+'.md')
                            with guard_source(prerequisite, sources, skip=(OSError, UnicodeError, ValueError)):
                                if prerequisite.is_file():
                                    _ = issue_store.frontmatter(prerequisite, sources=sources, select=selected,
                                                                keys=PROPOSAL_STATE_FIELDS | {'tags'})
    elif own is not None:
        for doc in documents:
            references[doc] = {issue.path.stem:str(issue.path) for issue in own
                               if issue.value('proposed_by') in proposers[doc]}
            selected_issues[doc] = [issue for issue in own if proposal_state(issue) in states]
    for doc in documents:
        owner = owners[doc]
        queue = 'proposal-'+owner
        for issue in selected_issues[doc]:
            with guard_source(issue.path, sources):
                row = proposals_table.issue_row(issue, members, sources=sources)
                key = 'proposal/'+str(issue.path)
                if isinstance(row.progress, AwaitingApproval):
                    entry = replace(row.entry, after=references[doc].get(row.entry.after, ''))
                    filed = entry.since.replace(tzinfo=user_zone()) if entry.since is not None else moment('1970-01-01')
                    item = proposal_item(key, issue, owner, staff.StaffId(issue.value('proposed_by')), issue.path.stem,
                                         filed, members, entry, queue)
                else:
                    filed = moment(issue.value('date_created') or '1970-01-01')
                    item = proposal_item(key, issue, owner, staff.StaffId(issue.value('proposed_by') or owner),
                                         issue.path.stem, filed, members)
                if item.state in states:
                    found[key] = item
        if isinstance(selection, ExactReviewRecord) and reads_issues:
            continue
        ledgers: list[Path] = []
        with guard_source(doc, sources):
            ledgers = proposals_table.improvement_ledgers(doc)
        for path in ledgers:
            if not selection.includes(path):
                continue
            with guard_source(path, sources):
                for line in proposals_table.read_lines(path, sources=sources):
                    match = proposals_table.BACKLOG_LINE.fullmatch(line)
                    if match is None or match['state'] != 'proposed':
                        continue
                    title = proposals_table.BACKLOG_FILE.sub('', match['title'])
                    row = proposals_table.Row('plan backlog', title, match['date'], proposals_table.REVIEW_STATE)
                    with guard_source(path, sources, entry=title):
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
    except Refusal:
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
        value=entry.value,text=line)


class ReviewRecordChanged(ValueError):
    """The current record does not accept the action on the displayed card."""


class ReviewRecordMissing(ReviewRecordChanged):
    """The source no longer contains the displayed record."""


def read_current(root: Path, item: ReviewItem, members: staff.StaffSnapshot) -> ReviewItem:
    """Reread the exact record. Use the snapshot owner for an Efficiency ledger."""
    selection = ExactReviewRecord(source_path(item), item.key, item.owner)
    try:
        records = project(root, members, (item.type,), REVIEW_STATES, None, [], selection)
    except FileNotFoundError:
        records = []
    for record in records:
        if record.key == item.key:
            return record
    raise ReviewRecordMissing('This item is no longer on the list.')


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
         sources: list[Path] | None = None) -> list[ReviewItem]:
    """Read each record from its existing store without another stored copy."""
    members = staff.snapshot() if members is None else members
    return project(root, members, kinds, selected_states(show), owner_options, sources, ALL_RECORDS)


def project(root: Path, members: staff.StaffSnapshot, kinds: tuple[ReviewType, ...], states: tuple[ReviewState, ...],
            owner_options: dict[staff.StaffId, str] | None, sources: list[Path] | None,
            selection: ReviewSelection) -> list[ReviewItem]:
    """Select the requested records and apply the shared projection."""
    documents = tuple(dict.fromkeys(member.doc.path for member in members.rows
                      if member.kind == 'showrunner' and isinstance(member.doc, staff.LiveDoc)))
    items: list[ReviewItem] = []
    if ReviewType.QUESTION in kinds:
        items.extend(question_items(members,documents,states=states,owner_options=owner_options,
                                   sources=sources,selection=selection))
    if ReviewType.CHANGE in kinds:
        items.extend(change_items(root,members,states=states,owner_options=owner_options,sources=sources,selection=selection))
    if ReviewType.INVESTIGATION in kinds:
        items.extend(investigation_items(members,states=states,owner_options=owner_options,sources=sources,selection=selection))
    if ReviewType.PROPOSAL in kinds:
        items.extend(proposal_items(members,documents,states=states,owner_options=owner_options,sources=sources,selection=selection))
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

    times = review_later.times(sources, skipped=skip_time if sources is not None else None)
    return [replace(item, times=replace(item.times, later=times.get(item.key, item.times.later)))
            for item in items if item.key not in invalid]


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
    if kind == ReviewType.INVESTIGATION:
        owners_ids = tuple(member.id for doc in documents for member in
            (unit_lookup.doc_owner(doc,members),*unit_lookup.doc_units(doc,members))) if documents else (cast(staff.StaffRow,owner).id,)
        return investigation_items(members,owners=owners_ids,sources=sources,states=states)
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
