"""Read the five review stores through one model of whose turn it is."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
import re
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from scripts.production import unit_lookup
if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

from scripts.lib import urgency
from scripts.lib.user_zone import user_zone
from scripts.nightly_review import candidates
from scripts.production import change_review, change_routing, delivery_state, investigation, staff, update_registration
from scripts.production import proposals_folder, proposals_table
from scripts.production.issues import Issue
from scripts.production.production_doc import production_field, Refusal
from scripts.review_links import review_events
from scripts.review_links.review_events import ReviewLog as ReviewLog


class ReviewType(StrEnum):
    QUESTION = 'question'
    CHANGE = 'change'
    INVESTIGATION = 'investigation'
    PROPOSAL = 'proposal'
    EFFICIENCY = 'efficiency'


class ReviewState(StrEnum):
    FOR_YOU = 'For you'
    WAITING = 'Waiting'
    DONE = 'Done'


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
class EfficiencySource:
    path: Path
    line: str


type ReviewSource = ChangeSource | QuestionSource | InvestigationSource | ProposalSource | EfficiencySource


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

    def for_you(self, now: datetime) -> bool:
        return self.state == ReviewState.FOR_YOU and not self.hidden_until(now)

    def hidden_until(self, now: datetime) -> bool:
        return isinstance(self.times.later, datetime) and self.times.later > now


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
    return ReviewState.FOR_YOU if slots.waits_for_user(False) else ReviewState.WAITING


def question_state(record: update_registration.QuestionRecord) -> ReviewState:
    if record.status == 'closed':
        return ReviewState.DONE
    return ReviewState.WAITING if 'followup' in record.item else ReviewState.FOR_YOU


def investigation_state(item: investigation.Investigation) -> ReviewState:
    return {investigation.InvestigationState.OPEN: ReviewState.WAITING,
            investigation.InvestigationState.ANSWER_READY: ReviewState.FOR_YOU,
            investigation.InvestigationState.READ: ReviewState.DONE}[item.state]


def change_items(root: Path, members: staff.StaffSnapshot) -> list[ReviewItem]:
    snapshots = {path: change_review.review_snapshot(path)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + '*.md'))}
    documents = {member.name: member.doc.path for member in members.rows
                 if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))}
    documents = change_routing.production_documents(documents,
        ((path.stem.removeprefix(change_review.PREFIX), snapshot.text) for path, snapshot in snapshots.items()))
    routing = change_routing.StaffRouting(members, documents)
    found: list[ReviewItem] = []
    for path, snapshot in snapshots.items():
        prefix = path.stem.removeprefix(change_review.PREFIX)
        for row in (*snapshot.pending, *snapshot.reviewed):
            assignment = routing.owner_and_production(path, row)
            routed = assignment.owner
            owner = routing.runners_by_name.get(routed.tab)
            identity = owner.id if owner is not None else staff.StaffId(routed.tab)
            source = staff.StaffId(row.get('Producer', '') or identity)
            slots = change_routing.delivery_slots(change_routing.delivery_path(root, path, row['#']))
            state = change_state(row, slots)
            entry = change_review.row_entries([row])[0]
            filed = entry.since.replace(tzinfo=user_zone()) if entry.since is not None else moment('1970-01-01')
            logs = [ReviewLog(filed, name(source, members), 'Filed for your check')]
            if isinstance(slots.question, delivery_state.OpenQuestion):
                logs.append(ReviewLog(filed, 'nate', 'Question: ' + slots.question.text))
            elif isinstance(slots.question, delivery_state.AnsweredQuestion):
                logs.extend((ReviewLog(filed, 'nate', 'Question: ' + slots.question.text),
                             ReviewLog(filed, name(identity, members), 'Answer: ' + slots.question.answer)))
            if slots.validation_requested:
                logs.append(ReviewLog(filed, 'nate', 'Asked the showrunner to check'))
            if row.get('Verdict'):
                logs.append(ReviewLog(filed, 'nate', row['Verdict']))
            logs.extend(review_events.decode(row.get('Review log', '')))
            shown = change_review.display_id(row, routed.tab)
            found.append(ReviewItem(prefix+'/'+row['#'], shown, ReviewType.CHANGE, identity, source,
                row['Change'], state, ReviewTimes(filed, logs[-1].time), tuple(logs),
                ('works', 'not works', 'validate', 'question', 'feedback') if state == ReviewState.FOR_YOU else (),
                ChangeSource(path, row, assignment, slots), entry.urgency, entry.after))
    return found


def question_items(members: staff.StaffSnapshot, documents: tuple[Path, ...]) -> list[ReviewItem]:
    identities = update_registration.QuestionStaff.for_record(members)
    records = {item.path: item for document in documents for item in
               update_registration.question_records_for_document(document, identities, include_unrouted=True)}
    records.update({item.path: item for item in update_registration.unowned_question_records(identities)})
    found: list[ReviewItem] = []
    for record in sorted(records.values(), key=lambda item: (item.item['since'], str(item.path))):
        item = record.item
        filed = moment(item['since'])
        entry = update_registration.question_entry(record.owner, item, record.path)
        after = item.get('after', '')
        later = moment(after) if after else NoLaterTime()
        state = question_state(record)
        logs = [ReviewLog(filed, name(record.asker, members), 'Asked')]
        for exchange in item.get('followups', []):
            logs.extend((ReviewLog(filed, 'nate', exchange['followup']),
                         ReviewLog(filed, name(record.asker, members), exchange['reply'])))
        if 'followup' in item:
            logs.append(ReviewLog(moment(item['followup']['sent']), 'nate', item['followup']['text']))
        logs.extend(review_events.decode(item.get('review_log', '')))
        found.append(ReviewItem('question/'+record.owner+'/'+record.path.name,
            name(record.owner, members)+'/'+update_registration.question_review_id(record), ReviewType.QUESTION, record.owner,
            record.asker, item['text'], state, ReviewTimes(filed, logs[-1].time, later), tuple(logs),
            ('answer',) if state == ReviewState.FOR_YOU else (), QuestionSource(record), entry.urgency, entry.after))
    return found


def investigation_items(members: staff.StaffSnapshot) -> list[ReviewItem]:
    root = investigation.state_root()
    owners = sorted(root.iterdir()) if root.exists() else []
    found: list[ReviewItem] = []
    for folder in owners:
        if not folder.is_dir() or folder.is_symlink():
            continue
        for record in investigation.records(staff.StaffId(folder.name), include_read=True):
            filed = moment(record.filed)
            logs = [ReviewLog(filed, name(record.owner, members), 'Filed')]
            if record.answered:
                logs.append(ReviewLog(moment(record.answered), name(record.owner, members), 'Answer ready'))
            if record.read:
                logs.append(ReviewLog(moment(record.read), 'nate', 'Marked read'))
            logs.extend(record.log)
            state = investigation_state(record)
            found.append(ReviewItem('investigation/'+record.owner+'/'+record.id,
                name(record.owner, members)+'/'+record.id, ReviewType.INVESTIGATION,
                owner_for(record.owner, members), record.owner, record.title, state,
                ReviewTimes(filed, logs[-1].time), tuple(logs),
                ('read',) if state == ReviewState.FOR_YOU else (), InvestigationSource(record)))
    return found


def proposal_items(members: staff.StaffSnapshot, documents: tuple[Path, ...]) -> list[ReviewItem]:
    records = {issue.path: (doc, issue) for doc in documents for issue in proposals_folder.proposal_issues(doc)}
    found: list[ReviewItem] = []
    for path, (doc, issue) in records.items():
        owner = next(member.id for member in members.rows if isinstance(member.doc, staff.LiveDoc) and member.doc.path == doc)
        waiting = proposals_table.issue_entries([issue])[0]
        state = ReviewState.FOR_YOU if waiting else ReviewState.DONE
        filed = moment(issue.value('date_created') or '1970-01-01')
        source = staff.StaffId(issue.value('proposed_by') or owner)
        found.append(ReviewItem('proposal/'+str(path), issue.value('proposal_id') or path.stem, ReviewType.PROPOSAL,
            owner, source, issue.value('title') or path.stem, state, ReviewTimes(filed, filed),
            (ReviewLog(filed, name(source, members), 'Proposed'),), (), ProposalSource(issue), proposals_table.issue_urgency(issue)))
    return found


def efficiency_items(members: staff.StaffSnapshot, documents: tuple[Path, ...]) -> list[ReviewItem]:
    found: list[ReviewItem] = []
    seen: set[Path] = set()
    for doc in documents:
        try:
            path = Path(production_field(doc.read_text().splitlines(), 'Efficiency ledger')).expanduser()
        except Refusal:
            continue
        if path in seen:
            continue
        seen.add(path)
        lines = path.read_text().splitlines() if path.exists() else []
        owner = next(member.id for member in members.rows if isinstance(member.doc, staff.LiveDoc) and member.doc.path == doc)
        for index, line in enumerate(lines):
            if not line.startswith('- ') or ' — ' not in line:
                continue
            head, _, result = line.rpartition(' — ')
            if len(head) < 12:
                continue
            filed = moment(head[2:12])
            state = ReviewState.FOR_YOU if result.startswith('proposed') else ReviewState.WAITING if result.startswith('deferred') else ReviewState.DONE
            entry = candidates.entries([line])
            level = entry[0].urgency if entry else urgency.Urgency()
            found.append(ReviewItem(f'efficiency/{path}/{index}', name(owner, members)+f'/E{index+1}',
                ReviewType.EFFICIENCY, owner, owner, head[13:], state, ReviewTimes(filed, filed),
                (ReviewLog(filed, name(owner, members), result),), (), EfficiencySource(path, line), level))
    return found


def read(root: Path, *, members: staff.StaffSnapshot | None = None) -> list[ReviewItem]:
    """Read each record from its existing store without another stored copy."""
    members = staff.snapshot() if members is None else members
    documents = tuple(dict.fromkeys(member.doc.path for member in members.rows
                      if member.kind == 'showrunner' and isinstance(member.doc, staff.LiveDoc)))
    items = [*question_items(members, documents), *change_items(root, members),
             *investigation_items(members), *proposal_items(members, documents), *efficiency_items(members, documents)]
    entries = [urgency.Entry(item.key, item, item.urgency, item.times.filed.replace(tzinfo=None), after=item.after)
               for item in items]
    return [entry.item for entry in urgency.order(entries, '', urgency.URGENT, {})]


EFFICIENCY_LEDGER_FIELD = "Efficiency ledger"
EFFICIENCY_LINE = re.compile(r"^- \d{4}-\d\d-\d\d (?P<head>.*?) — (?P<status>.*)$")

def change_counts(doc: Path, record: staff.StaffSnapshot, *, source: str = "",
                  sources: list[Path] | None = None) -> tuple[int, int]:
    """Count the production tab and the unrouted tab in one routed pass."""
    try:
        scope = change_review.review_scope(doc, False, record)
        listing = (change_review.scope_list(doc, record=record) if sources is None
                   else change_review.scope_list(doc, record=record, sources=sources))
        if sources is None:
            routed = change_review.routed_review_entries(listing, doc, record=record)
        else:
            routed = change_review.routed_review_entries(listing, doc, record=record, sources=sources)
        return (sum(owner.tab in scope and (not source or row.get("Producer") == source)
                    for _, owner, row in routed),
                0 if source else sum(isinstance(owner, change_routing.UnroutedChange) for _, owner, _ in routed))
    except OSError:
        if sources is not None:
            raise
        return 0, 0
    except change_review.ChangeReviewError:
        return 0, 0


def proposals(doc: Path, *, source: str = "", sources: list[Path] | None = None,
              own: list[Issue] | None = None) -> int:
    """Count the waiting proposals. `own` is the doc's `open_proposals` when the caller already read them."""
    found = proposals_table.scope_issues(doc, sources=sources) if own is None else own
    if source:
        waiting = proposals_table.issue_entries(
            [issue for issue in found if issue.value("proposed_by") == source], sources=sources)[0]
        return len(waiting)
    return len(proposals_table.waiting_entries(doc, sources=sources, issues=found)[0])


def open_efficiency(line: str, *, source: str = "") -> bool:
    """Match an open ledger line and its source before the first status delimiter."""
    match = EFFICIENCY_LINE.match(line)
    return (match is not None and match.group("status").strip().startswith("proposed")
            and (not source or match.group("head").rstrip().endswith(f" [by: {source}]")))


def efficiency_ledger(doc: Path) -> Path | None:
    """Read the configured efficiency ledger, or return None when it is unconfigured."""
    try:
        return Path(production_field(doc.read_text(encoding="utf-8").splitlines(), EFFICIENCY_LEDGER_FIELD)).expanduser()
    except Refusal:
        return None


def efficiency(doc: Path, *, source: str = "", sources: list[Path] | None = None) -> int:
    """Efficiency proposals that wait for the user in the doc's ledger."""
    try:
        path = efficiency_ledger(doc)
    except (OSError, UnicodeError):
        if sources is not None:
            raise
        return 0
    try:
        if path is None:
            return 0
        if sources is not None:
            sources.append(path)
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError):
        if sources is not None:
            raise
        return 0
    return sum(open_efficiency(line, source=source) for line in lines)


def investigations(doc: Path, record: staff.StaffSnapshot, *, source: str = "",
                   sources: list[Path] | None = None) -> int:
    """Count the owner and the same units used by production proposals."""
    if source:
        return ready_investigations(staff.StaffId(source), sources=sources)
    try:
        owner = unit_lookup.doc_owner(doc, record)
    except staff.StaffUnreadable:
        return 0
    owners = (owner, *unit_lookup.doc_units(doc, record))
    return sum(ready_investigations(member.id, sources=sources) for member in owners)



def question_counts(owner: staff.StaffRow, zone: ZoneInfo, identities: QuestionStaff, sources: list[Path] | None,
                    source: str = "") -> tuple[int, str]:
    """Count due questions and retain the next deferred deadline."""
    from scripts.production.update_registration import DeferredUntil, OutstandingReminder, outstanding_file, read_open_items

    items = read_open_items(outstanding_file(owner.id, identities=identities), identities=identities, sources=sources)
    if source:
        items[:] = [item for item in items if not isinstance(item, OutstandingReminder) and item.asked_by == source]
    now = datetime.now(zone).replace(tzinfo=None)
    afters = [item.timing.at for item in items if isinstance(item.timing, DeferredUntil) and item.timing.at > now]
    return count_due(items, now), "" if not afters else str(int(min(afters).replace(tzinfo=zone).timestamp()))


def ready_investigations(owner: staff.StaffId, *, sources: list[Path] | None = None) -> int:
    """Count only answers that require the user's action."""
    return sum(investigation_state(item) == ReviewState.FOR_YOU for item in investigation.records(owner, sources=sources))


def next_queue(kind: str, scope: Path | staff.StaffRow, below: bool, members: staff.StaffSnapshot) -> tuple[str, list[urgency.Entry[str]]]:
    """Read one kind through its shared store adapter without reading unrelated stores."""
    from scripts.production.production_doc import read_production
    if kind == 'question':
        identities = update_registration.QuestionStaff.for_record(members)
        owner = scope if isinstance(scope, staff.StaffRow) else identities.owner(scope)
        production = update_registration.ProducerQuestions(owner) if owner.kind == 'producer' else read_production(owner)
        due = update_registration.open_lists(production, below, identities=identities).due
        entries = [urgency.Entry(entry.key, entry.item.line(), entry.urgency, entry.since, entry.value, entry.after) for entry in due]
        return update_registration.question_queue(production, below), entries
    if isinstance(scope, staff.StaffRow):
        raise ValueError('this review kind requires a production document')
    doc = scope
    if kind == 'change':
        queue = change_review.change_queue(change_review.scope_list(doc), doc, below)
        return queue.name, [urgency.Entry(entry.key, change_review.describe(entry.item), entry.urgency,
                            entry.since, entry.value, entry.after) for entry in queue.entries]
    if kind == 'proposal':
        waiting = proposals_table.waiting_entries(doc, below)
        return urgency.scope_queue(proposals_table.queue(doc, record=members), below), [
            urgency.Entry(entry.key, proposals_table.proposal_text(entry), entry.urgency, entry.since, entry.value, entry.after)
            for entry in waiting[0]]
    documents = proposals_folder.scope_docs(doc, below, record=members)
    if kind == 'efficiency':
        ledgers = dict.fromkeys(path for each in documents if (path := efficiency_ledger(each)) is not None)
        lines = [line for ledger in ledgers for line in candidates.read_lines(ledger) if open_efficiency(line)]
        return urgency.scope_queue(candidates.QUEUE, below), [
            urgency.Entry(entry.key, entry.item.line, entry.urgency, entry.since, entry.value, entry.after)
            for entry in candidates.entries(lines)]
    owners = {member.id for member in members.rows if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path in documents}
    now = datetime.now(user_zone())
    items = [item for item in investigation_items(members) if item.owner in owners and item.for_you(now)]
    owner = next(member.id for member in members.rows if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path == doc)
    return urgency.scope_queue('investigation-'+owner, below), [urgency.Entry(item.key,
        item.id+' '+item.title+'\n'+item.source.record.question+'\n\nAnswer\n'+item.source.record.answer,
        item.urgency,item.times.filed.replace(tzinfo=None)) for item in items if isinstance(item.source, InvestigationSource)]


def count_due(items: list[update_registration.Outstanding], now: datetime, source: str = "") -> int:
    """Count only due records whose next action belongs to the user."""
    return sum(not source or (not isinstance(item, update_registration.OutstandingReminder) and item.asked_by == source)
               for item in update_registration.due(items, now))
