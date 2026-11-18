"""Read the five review stores through one model of whose turn it is."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, cast

from scripts.production import unit_lookup, showrunner_tree
from scripts.agents.remote import local_machine
if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

from scripts.lib import urgency
from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, delivery_state, investigation, staff, update_registration
from scripts.production import proposals_folder, proposals_table
from scripts.production.issues import Issue, PROPOSAL_TAG, open_issues
from scripts.production.proposal_issue import DeclinedDecision, InProgress, decision_answer, proposal_progress
from scripts.production.proposal_issue import REVIEW_MARK_FIELD, review_log, review_mark
from scripts.production.production_doc import production_field, Refusal
from scripts.review_links import efficiency_reviews, review_events, review_later
from scripts.review_links.review_events import ReviewLog as ReviewLog, ReviewMark


class ReviewType(StrEnum):
    QUESTION = 'question'
    CHANGE = 'change'
    INVESTIGATION = 'investigation'
    PROPOSAL = 'proposal'
    EFFICIENCY = 'efficiency'


class ReviewState(StrEnum):
    OPEN = 'Open'
    VALIDATING = 'Validating'
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


def read_change_delivery(path: Path) -> delivery_state.DeliverySlots:
    """Read change exchanges with their request type."""
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery())
    try:
        value = cast(object, json.loads(content))
    except ValueError:
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


def moment(value: str) -> datetime:
    """Read stored times in the user's zone when the store has no zone."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=user_zone())


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
                members: staff.StaffSnapshot, root: Path, queue: str, current: Path,
                slots: delivery_state.DeliverySlots | None = None) -> ReviewItem:
    """Project a change once for the page, counts and queue."""
    prefix = path.stem.removeprefix(change_review.PREFIX)
    routed = assignment.owner
    owner = next((member for member in members.rows if member.name == routed.tab), None)
    identity = owner.id if owner is not None else staff.StaffId(routed.tab)
    source = staff.StaffId(row.get('Producer', '') or identity)
    slots = (change_delivery(root, path, row['#'])
             if slots is None else slots)
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
                 owner_options: dict[staff.StaffId, str] | None = None) -> list[ReviewItem]:
    if owner_options is None and states == (ReviewState.WAITING,):
        return []
    if document is not None:
        current = change_review.scope_list(document, record=members, sources=sources)
        queue = change_queue_name(document, members, current, below)
        routed = (change_review.routed_review_entries(current,document,below,record=members,sources=sources) if below else
                  change_review.routed_review_entries(current,document,record=members,sources=sources) if sources is not None else
                  change_review.routed_review_entries(current,document,record=members))
        return [change_item(listing.path, row, change_routing.ChangeAssignment(owner, document), members, root, queue, current)
                for listing, owner, row in routed if ReviewState.OPEN in states]
    snapshots = {path: change_review.review_snapshot(path, include_reviewed=ReviewState.DONE in states,
                                                    collect_owners=owner_options is not None)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + '*.md'))}
    documents = {member.name: member.doc.path for member in members.rows
                 if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))}
    documents = change_routing.production_documents(documents,
        ((path.stem.removeprefix(change_review.PREFIX), snapshot.text) for path, snapshot in snapshots.items()))
    routing = change_routing.StaffRouting(members, documents)
    queues: dict[str, tuple[str, Path]] = {}
    found: list[ReviewItem] = []
    for path, snapshot in snapshots.items():
        if owner_options is not None:
            known: set[tuple[str, str, bool]] = set()
            for row in (*snapshot.pending, *snapshot.reviewed, *snapshot.owner_rows):
                fields = (row.get('Producer', ''), row.get('Production', ''), bool(row.get('Verdict')))
                if fields in known:
                    continue
                known.add(fields)
                tab = routing.owner_and_production(path, row).owner.tab
                owner = next((member for member in members.rows if member.name == tab), None)
                identity = owner.id if owner is not None else staff.StaffId(tab)
                owner_options[identity] = name(identity, members)
        for row in (*snapshot.pending, *snapshot.reviewed):
            if row.get('Verdict') and ReviewState.DONE not in states:
                continue
            if not row.get('Verdict') and states == (ReviewState.DONE,):
                continue
            slots = change_delivery(root, path, row['#'])
            if change_state(row, slots) not in states:
                continue
            assignment = routing.owner_and_production(path, row)
            tab = assignment.owner.tab
            if tab not in queues:
                doc = documents.get(tab)
                current = change_review.scope_list(doc, record=members) if doc is not None else path
                queue = change_queue_name(doc, members, current) if doc is not None else change_review.queue(path)
                queues[tab] = queue, current
            queue, current = queues[tab]
            found.append(change_item(path, row, assignment, members, root, queue, current, slots))
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
        update_registration.question_queue_name(owner), entry.key, entry.after, entry.value, opened.line(),
        review_events.waiting_why(fields.get('waiting_why', '')))


def question_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   owners: tuple[staff.StaffRow, ...] = (), sources: list[Path] | None = None,
                   assign_ids: bool = False, include_unowned: bool = True,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
                   owner_options: dict[staff.StaffId, str] | None = None) -> list[ReviewItem]:
    identities = update_registration.QuestionStaff.for_record(members)
    selected = owners or tuple(member for member in members.rows if member.kind == 'producer' or
        member.kind == 'showrunner' and isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path in documents)
    found: list[ReviewItem] = []
    seen: set[Path] = set()
    identities_found: set[staff.StaffId] = set()
    statuses = tuple(status for status in ('open', 'closed')
                     if (ReviewState.DONE in states if status == 'closed' else states != (ReviewState.DONE,)))
    for owner in selected:
        if isinstance(owner.doc,(staff.LiveDoc,staff.WrappedDoc)) and owner.doc.machine != local_machine():
            print(f'questions: {owner.name}: ownership evidence is unavailable',file=sys.stderr)
            continue
        document = owner if owner.kind == 'producer' else cast(staff.LiveDoc, owner.doc).path
        if sources is not None and isinstance(document, Path):
            sources.append(document)
        records = update_registration.question_records_for_document(document, identities, include_unrouted=True, sources=sources,
            statuses=statuses, owner_ids=identities_found)
        for record in records:
            if record.path not in seen:
                seen.add(record.path)
                if question_state(record) not in states:
                    continue
                if assign_ids:
                    record.item['review_id'] = update_registration.question_review_id(record)
                found.append(question_item(record, members))
    if include_unowned:
        for record in update_registration.unowned_question_records(identities, statuses=statuses, owner_ids=identities_found):
            if record.path in seen:
                continue
            seen.add(record.path)
            if question_state(record) not in states:
                continue
            if assign_ids:
                record.item['review_id'] = update_registration.question_review_id(record)
            found.append(question_item(record, members, unowned=True))
    if owner_options is not None:
        for identity in identities_found:
            member = members.by_id(identity)
            owner_options[identity] = name(identity, members) if member is not None else 'Owner unavailable'
    keys = {item.queue_key:item.key for item in found}
    return [replace(item, after=keys.get(item.queue_after,'')) for item in found]


def investigation_items(members: staff.StaffSnapshot, *, owners: tuple[staff.StaffId, ...] = (),
                        sources: list[Path] | None = None,
                        states: tuple[ReviewState, ...] = REVIEW_STATES,
                        owner_options: dict[staff.StaffId, str] | None = None) -> list[ReviewItem]:
    root = investigation.state_root()
    selected = owners or tuple(staff.StaffId(path.name) for path in sorted(root.iterdir()) if path.is_dir() and not path.is_symlink()) if root.exists() else owners
    found: list[ReviewItem] = []
    investigation_states = tuple(state for state,review_state in (
        (investigation.InvestigationState.OPEN, ReviewState.WAITING),
        (investigation.InvestigationState.ANSWER_READY, ReviewState.OPEN),
        (investigation.InvestigationState.READ, ReviewState.DONE)) if review_state in states)
    for identity in selected:
        if owner_options is not None and any(investigation.folder(identity).glob('I*.md')):
            for owner in (identity, owner_for(identity, members)):
                owner_options[owner] = name(owner, members)
        for record in investigation.records(identity, include_read=True, sources=sources, states=investigation_states):
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
            found.append(ReviewItem(key, identity_text, ReviewType.INVESTIGATION, owner, record.owner, record.title, state,
                ReviewTimes(filed, logs[-1].time), tuple(logs),
                ('read', 'feedback', 'question') if state != ReviewState.DONE else (), InvestigationSource(record),
                queue='investigation-'+owner, queue_key=key,
                text=identity_text+' '+record.title+'\n'+record.question+'\n\nAnswer\n'+record.answer,
                waiting_why=review_events.waiting_why(record.waiting_why)))
    return found


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


def proposal_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   sources: list[Path] | None = None, own: list[Issue] | None = None,
                   states: tuple[ReviewState, ...] = REVIEW_STATES,
                   owner_options: dict[staff.StaffId, str] | None = None) -> list[ReviewItem]:
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
    if own is None:
        folders: dict[Path, list[Path]] = {}
        for doc in documents:
            folder = proposals_folder.issues_folder(doc)
            if folder is not None:
                folders.setdefault(folder, []).append(doc)
        for folder, scoped_documents in folders.items():
            documents_by_proposer: dict[str, list[Path]] = {}
            for doc in scoped_documents:
                for proposer in proposers[doc]:
                    documents_by_proposer.setdefault(proposer, []).append(doc)

            def selected(issue: Issue) -> bool:
                if issue.value('record_type') == 'question':
                    return False
                scoped = documents_by_proposer.get(issue.value('proposed_by'), [])
                for doc in scoped:
                    references[doc][issue.path.stem] = str(issue.path)
                return bool(scoped) and proposal_state(issue) in states

            for issue in open_issues(folder, PROPOSAL_TAG, sources=sources, select=selected, keys=PROPOSAL_STATE_FIELDS):
                for doc in documents_by_proposer[issue.value('proposed_by')]:
                    selected_issues[doc].append(issue)
    else:
        for doc in documents:
            references[doc] = {issue.path.stem:str(issue.path) for issue in own
                               if issue.value('proposed_by') in proposers[doc]}
            selected_issues[doc] = [issue for issue in own if proposal_state(issue) in states]
    for doc in documents:
        owner = owners[doc]
        issues = selected_issues[doc]
        waiting, _ = proposals_table.waiting_entries(doc, sources=sources, issues=issues, record=members)
        by_key = {str(issue.path):issue for issue in issues}
        queue = 'proposal-'+owner
        for entry in waiting:
            issue = by_key.get(entry.key)
            if issue is not None:
                dependency = next(iter(issue.fields.get('depends_on', [])), '')
                entry = replace(entry, after=references[doc].get(dependency, ''))
            filed = entry.since.replace(tzinfo=user_zone()) if entry.since is not None else moment('1970-01-01')
            key = 'proposal/'+entry.key
            if issue is not None:
                # The id column holds the P number, so the title is the issue name alone.
                item = proposal_item(key,issue,owner,staff.StaffId(issue.value('proposed_by')),issue.path.stem,filed,
                                     members,entry,queue)
            else:
                # A plan backlog line has no issue to hold a page answer; the user answers it in chat.
                path = Path(entry.key.removeprefix(proposals_table.BACKLOG_KEY).split(': ',1)[0])
                item = ReviewItem(key,entry.item.proposal,ReviewType.PROPOSAL,owner,owner,entry.item.proposal,
                    ReviewState.OPEN,ReviewTimes(filed,filed),(ReviewLog(filed,name(owner,members),'Proposed'),),(),
                    ProposalLedgerSource(path),entry.urgency,'proposal/'+entry.after if entry.after else '',queue,
                    entry.key,entry.after,entry.value,proposals_table.proposal_text(entry))
            if item.state not in states:
                continue
            found[key] = item
        waiting_keys = {entry.key for entry in waiting}
        for issue in issues:
            if str(issue.path) in waiting_keys:
                continue
            filed = moment(issue.value('date_created') or '1970-01-01')
            key = 'proposal/'+str(issue.path)
            found[key] = proposal_item(key,issue,owner,staff.StaffId(issue.value('proposed_by') or owner),issue.path.stem,
                                       filed,members)
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


def efficiency_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                     sources: list[Path] | None = None,
                     states: tuple[ReviewState, ...] = REVIEW_STATES,
                     owner_options: dict[staff.StaffId, str] | None = None) -> list[ReviewItem]:
    found: list[ReviewItem] = []
    seen: set[Path] = set()
    for doc in documents:
        path = efficiency_ledger(doc)
        if isinstance(path, NoEfficiencyLedger) or path in seen:
            continue
        seen.add(path)
        if sources is not None:
            sources.append(path)
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            continue
        owner = next((member.id for member in members.rows if isinstance(member.doc,(staff.LiveDoc,staff.WrappedDoc)) and member.doc.path == doc),staff.StaffId(''))
        reviews = efficiency_reviews.read(path, sources)
        for index,line in enumerate(lines):
            match = EFFICIENCY_LINE.match(line)
            if match is None:
                continue
            head, _, result = line.partition(' — ')
            review = reviews.get(f'E{index+1}', efficiency_reviews.EfficiencyReview())
            state = efficiency_state(result, review)
            if state not in states:
                continue
            if owner_options is not None:
                owner_options[owner] = name(owner,members)
            filed = moment(head[2:12])
            candidates_found = candidates.entries([head+' — proposed'])
            entry = candidates_found[0] if candidates_found else urgency.Entry(head,line)
            candidate = candidates_found[0].item if candidates_found else None
            who = staff.StaffId(candidate.by if candidate is not None else '')
            key = f'efficiency/{path}/{index}'
            logs = (ReviewLog(filed,name(owner,members),result), *review.log)
            found.append(ReviewItem(key,name(owner,members)+f'/E{index+1}',ReviewType.EFFICIENCY,owner,who,
                head[13:],state,ReviewTimes(filed,logs[-1].time),logs,
                DECISION_ACTIONS if state != ReviewState.DONE else (),
                EfficiencySource(path,line),entry.urgency,queue=candidates.QUEUE,queue_key=entry.key,
                value=entry.value,text=line,waiting_why=review_events.waiting_why(review.waiting_why)))
    return found


def entry(item: ReviewItem) -> urgency.Entry[ReviewItem]:
    """Use the existing queue keys for pins, moves and prerequisites."""
    return urgency.Entry(item.queue_key,item,item.urgency,item.times.filed.replace(tzinfo=None),item.value,item.queue_after)


def ordered(items: list[ReviewItem]) -> list[ReviewItem]:
    """Apply queue pins and moves before combining the records."""
    queues: dict[str,list[ReviewItem]] = {}
    for item in items:
        queues.setdefault(item.queue,[]).append(item)
    ended: dict[str,datetime] = {}
    for queue,group in queues.items():
        moved = urgency.ended(queue)
        ended.update({item.key:moved[item.queue_key] for item in group if item.queue_key in moved})
    ranked = [each.item for each in urgency.order([
        urgency.Entry(item.key,item,item.urgency,item.times.filed.replace(tzinfo=None),after=item.after)
        for item in items],ended=ended)]
    for queue,group in queues.items():
        # Preserve the global positions while applying each queue's current item.
        positions = [at for at,item in enumerate(ranked) if item.queue == queue]
        within = [each.item for each in urgency.queue_order([entry(item) for item in group],queue)]
        for at,item in zip(positions,within):
            ranked[at] = item
    for owner in dict.fromkeys(item.owner for item in ranked):
        current = urgency.pinned('next-'+owner)
        selected = next((item for item in ranked if item.owner == owner and item.queue_key == current
                         and item.key not in ended),None)
        if selected is not None:
            first = next(at for at,item in enumerate(ranked) if item.owner == owner)
            ranked.remove(selected)
            ranked.insert(first,selected)
    return ranked


def read(root: Path, *, members: staff.StaffSnapshot | None = None,
         kinds: tuple[ReviewType, ...] = REVIEW_TYPES, show: str = 'all',
         owner_options: dict[staff.StaffId, str] | None = None, assign_ids: bool = True) -> list[ReviewItem]:
    """Read each record from its existing store without another stored copy."""
    members = staff.snapshot() if members is None else members
    documents = tuple(dict.fromkeys(member.doc.path for member in members.rows
                      if member.kind == 'showrunner' and isinstance(member.doc, staff.LiveDoc)))
    states = selected_states(show)
    items: list[ReviewItem] = []
    if ReviewType.QUESTION in kinds:
        items.extend(question_items(members,documents,assign_ids=assign_ids,states=states,owner_options=owner_options))
    if ReviewType.CHANGE in kinds:
        items.extend(change_items(root,members,states=states,owner_options=owner_options))
    if ReviewType.INVESTIGATION in kinds:
        items.extend(investigation_items(members,states=states,owner_options=owner_options))
    if ReviewType.PROPOSAL in kinds:
        items.extend(proposal_items(members,documents,states=states,owner_options=owner_options))
    if ReviewType.EFFICIENCY in kinds:
        items.extend(efficiency_items(members,documents,states=states,owner_options=owner_options))
    return ordered(with_later(items))


def read_open(root: Path) -> list[ReviewItem]:
    """Share only open records during one console news request."""
    return read(root, show='open')


def with_later(items: list[ReviewItem], sources: list[Path] | None = None) -> list[ReviewItem]:
    times = review_later.times(sources)
    return [replace(item, times=replace(item.times, later=times[item.key]))
            if item.state != ReviewState.VALIDATING and item.key in times else item for item in items]


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
