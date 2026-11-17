"""Read the five review stores through one model of whose turn it is."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
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
class LegacyQuestionSource:
    item: update_registration.Outstanding
    path: Path


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


type ReviewSource = (ChangeSource | QuestionSource | LegacyQuestionSource | InvestigationSource
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


def change_item(path: Path, row: dict[str, str], assignment: change_routing.ChangeAssignment,
                members: staff.StaffSnapshot, root: Path, queue: str, current: Path) -> ReviewItem:
    """Project a change once for the page, counts and queue."""
    prefix = path.stem.removeprefix(change_review.PREFIX)
    routed = assignment.owner
    owner = next((member for member in members.rows if member.name == routed.tab), None)
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
    after = entry.after if '/' in entry.after else prefix+'/'+entry.after if entry.after else ''
    local = path.absolute() == current.absolute()
    return ReviewItem(prefix+'/'+row['#'], shown, ReviewType.CHANGE, identity, source,
        row['Change'], state, ReviewTimes(filed, logs[-1].time), tuple(logs),
        ('works', 'not works', 'validate', 'question', 'feedback') if state == ReviewState.FOR_YOU else (),
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
                 below: bool = False, sources: list[Path] | None = None) -> list[ReviewItem]:
    if document is not None:
        current = change_review.scope_list(document, record=members, sources=sources)
        queue = change_queue_name(document, members, current, below)
        routed = (change_review.routed_review_entries(current,document,below,record=members,sources=sources) if below else
                  change_review.routed_review_entries(current,document,record=members,sources=sources) if sources is not None else
                  change_review.routed_review_entries(current,document,record=members))
        return [change_item(listing.path, row, change_routing.ChangeAssignment(owner, document), members, root, queue, current)
                for listing, owner, row in routed]
    snapshots = {path: change_review.review_snapshot(path)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + '*.md'))}
    documents = {member.name: member.doc.path for member in members.rows
                 if isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc))}
    documents = change_routing.production_documents(documents,
        ((path.stem.removeprefix(change_review.PREFIX), snapshot.text) for path, snapshot in snapshots.items()))
    routing = change_routing.StaffRouting(members, documents)
    queues: dict[str, tuple[str, Path]] = {}
    found: list[ReviewItem] = []
    for path, snapshot in snapshots.items():
        for row in (*snapshot.pending, *snapshot.reviewed):
            assignment = routing.owner_and_production(path, row)
            tab = assignment.owner.tab
            if tab not in queues:
                doc = documents.get(tab)
                current = change_review.scope_list(doc, record=members) if doc is not None else path
                queue = change_queue_name(doc, members, current) if doc is not None else change_review.queue(path)
                queues[tab] = queue, current
            queue, current = queues[tab]
            found.append(change_item(path, row, assignment, members, root, queue, current))
    return found


def question_item(owner: staff.StaffId, fields: update_registration.OutstandingItem, path: Path,
                  members: staff.StaffSnapshot, record: update_registration.QuestionRecord | None = None) -> ReviewItem:
    parsed = update_registration.parse_outstanding_entries([fields], path)[0]
    filed = moment(fields['since'])
    entry = update_registration.question_entry(owner, fields, path)
    asker = staff.StaffId(fields.get('asked_by', ''))
    state = question_state(record) if record is not None else (
        ReviewState.WAITING if isinstance(parsed.timing, update_registration.AwaitingReply) else ReviewState.FOR_YOU)
    after = fields.get('after','')
    later = moment(after) if after else NoLaterTime()
    logs = [ReviewLog(filed, name(asker, members), 'Asked')]
    for exchange in fields.get('followups', []):
        logs.extend((ReviewLog(filed, 'nate', exchange['followup']),
                     ReviewLog(filed, name(asker, members), exchange['reply'])))
    if 'followup' in fields:
        logs.append(ReviewLog(moment(fields['followup']['sent']), 'nate', fields['followup']['text']))
    logs.extend(review_events.decode(fields.get('review_log', '')))
    source: ReviewSource = QuestionSource(record) if record is not None else LegacyQuestionSource(parsed, path)
    key = 'question/'+owner+'/'+path.name if record is not None else 'question/'+entry.key
    identity = name(owner, members)+'/'+fields.get('review_id',path.stem) if record is not None else entry.key
    opened = update_registration.OpenItem(name(owner, members), parsed, name(asker, members) if asker else '')
    return ReviewItem(key, identity, ReviewType.QUESTION, owner, asker, fields['text'], state,
        ReviewTimes(filed, logs[-1].time, later), tuple(logs),
        ('answer',) if state == ReviewState.FOR_YOU and record is not None else (), source, entry.urgency, '',
        update_registration.question_queue_name(owner), entry.key, entry.after, entry.value, opened.line())


def question_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   owners: tuple[staff.StaffRow, ...] = (), sources: list[Path] | None = None,
                   assign_ids: bool = False) -> list[ReviewItem]:
    identities = update_registration.QuestionStaff.for_record(members)
    selected = owners or tuple(member for member in members.rows if member.kind == 'producer' or
        member.kind == 'showrunner' and isinstance(member.doc, (staff.LiveDoc, staff.WrappedDoc)) and member.doc.path in documents)
    found: list[ReviewItem] = []
    seen: set[Path] = set()
    for owner in selected:
        if isinstance(owner.doc,(staff.LiveDoc,staff.WrappedDoc)) and owner.doc.machine != local_machine():
            print(f'questions: {owner.name}: ownership evidence is unavailable',file=sys.stderr)
            continue
        document = owner if owner.kind == 'producer' else cast(staff.LiveDoc, owner.doc).path
        if sources is not None and isinstance(document, Path):
            sources.append(document)
        records = update_registration.question_records_for_document(document, identities, include_unrouted=True, sources=sources)
        for record in records:
            if record.path not in seen:
                seen.add(record.path)
                if assign_ids:
                    _ = update_registration.question_review_id(record)
                found.append(question_item(record.owner, record.item, record.path, members, record))
        legacy = update_registration.outstanding_file(owner.id, identities=identities)
        if sources is not None:
            sources.append(legacy)
        found.extend(question_item(owner.id, fields, legacy, members) for fields in update_registration.stored_entries(legacy))
    keys = {item.queue_key:item.key for item in found}
    return [replace(item, after=keys.get(item.queue_after,'')) for item in found]


def investigation_items(members: staff.StaffSnapshot, *, owners: tuple[staff.StaffId, ...] = (),
                        sources: list[Path] | None = None) -> list[ReviewItem]:
    root = investigation.state_root()
    selected = owners or tuple(staff.StaffId(path.name) for path in sorted(root.iterdir()) if path.is_dir() and not path.is_symlink()) if root.exists() else owners
    found: list[ReviewItem] = []
    for identity in selected:
        for record in investigation.records(identity, include_read=True, sources=sources):
            filed = moment(record.filed)
            logs = [ReviewLog(filed, name(record.owner, members), 'Filed')]
            if record.answered:
                logs.append(ReviewLog(moment(record.answered), name(record.owner, members), 'Answer ready'))
            if record.read:
                logs.append(ReviewLog(moment(record.read), 'nate', 'Marked read'))
            logs.extend(record.log)
            state = investigation_state(record)
            owner = owner_for(record.owner, members)
            key = 'investigation/'+record.owner+'/'+record.id
            identity_text = name(record.owner, members)+'/'+record.id
            found.append(ReviewItem(key, identity_text, ReviewType.INVESTIGATION, owner, record.owner, record.title, state,
                ReviewTimes(filed, logs[-1].time), tuple(logs),
                ('read',) if state == ReviewState.FOR_YOU else (), InvestigationSource(record),
                queue='investigation-'+owner, queue_key=key,
                text=identity_text+' '+record.title+'\n'+record.question+'\n\nAnswer\n'+record.answer))
    return found


def proposal_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                   sources: list[Path] | None = None, own: list[Issue] | None = None) -> list[ReviewItem]:
    found: dict[str, ReviewItem] = {}
    for doc in documents:
        owner = next((member.id for member in members.rows if isinstance(member.doc,(staff.LiveDoc,staff.WrappedDoc)) and member.doc.path == doc),staff.StaffId(''))
        issues = proposals_table.scope_issues(doc, sources=sources) if own is None else own
        waiting, _ = proposals_table.waiting_entries(doc, sources=sources, issues=issues)
        by_key = {str(issue.path):issue for issue in issues}
        queue = 'proposal-'+owner
        for entry in waiting:
            issue = by_key.get(entry.key)
            path = issue.path if issue is not None else Path(entry.key.removeprefix(proposals_table.BACKLOG_KEY).split(': ',1)[0])
            source: ReviewSource = ProposalSource(issue) if issue is not None else ProposalLedgerSource(path)
            who = staff.StaffId(issue.value('proposed_by')) if issue is not None else owner
            filed = entry.since.replace(tzinfo=user_zone()) if entry.since is not None else moment('1970-01-01')
            key = 'proposal/'+entry.key
            found[key] = ReviewItem(key, issue.value('proposal_id') or path.stem if issue is not None else entry.item.proposal,
                ReviewType.PROPOSAL, owner, who, entry.item.proposal, ReviewState.FOR_YOU, ReviewTimes(filed,filed),
                (ReviewLog(filed,name(who,members),'Proposed'),), (), source, entry.urgency,
                'proposal/'+entry.after if entry.after else '', queue,entry.key,entry.after,entry.value,
                proposals_table.proposal_text(entry))
        waiting_keys = {entry.key for entry in waiting}
        for issue in issues:
            if str(issue.path) in waiting_keys:
                continue
            filed = moment(issue.value('date_created') or '1970-01-01')
            key = 'proposal/'+str(issue.path)
            who = staff.StaffId(issue.value('proposed_by') or owner)
            found[key] = ReviewItem(key,issue.value('proposal_id') or issue.path.stem,ReviewType.PROPOSAL,
                owner,who,issue.path.stem,ReviewState.WAITING,ReviewTimes(filed,filed),(),(),ProposalSource(issue))
    return list(found.values())


EFFICIENCY_LEDGER_FIELD = 'Efficiency ledger'
EFFICIENCY_LINE = re.compile(r'^- \d{4}-\d\d-\d\d (?P<head>.*?) — (?P<status>.*)$')


def efficiency_ledger(doc: Path) -> Path | NoEfficiencyLedger:
    """Read the ledger path or name its absence."""
    try:
        return Path(production_field(doc.read_text(encoding='utf-8').splitlines(), EFFICIENCY_LEDGER_FIELD)).expanduser()
    except Refusal:
        return NoEfficiencyLedger()


def efficiency_items(members: staff.StaffSnapshot, documents: tuple[Path, ...], *,
                     sources: list[Path] | None = None) -> list[ReviewItem]:
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
        for index,line in enumerate(lines):
            match = EFFICIENCY_LINE.match(line)
            if match is None:
                continue
            head, _, result = line.partition(' — ')
            filed = moment(head[2:12])
            state = (ReviewState.FOR_YOU if result.startswith('proposed') else
                     ReviewState.WAITING if result.startswith('deferred') else ReviewState.DONE)
            candidates_found = candidates.entries([head+' — proposed'])
            entry = candidates_found[0] if candidates_found else urgency.Entry(head,line)
            candidate = candidates_found[0].item if candidates_found else None
            who = staff.StaffId(candidate.by if candidate is not None else '')
            key = f'efficiency/{path}/{index}'
            found.append(ReviewItem(key,name(owner,members)+f'/E{index+1}',ReviewType.EFFICIENCY,owner,who,
                head[13:],state,ReviewTimes(filed,filed),(ReviewLog(filed,name(owner,members),result),),(),
                EfficiencySource(path,line),entry.urgency,queue=candidates.QUEUE,queue_key=entry.key,
                value=entry.value,text=line))
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


def read(root: Path, *, members: staff.StaffSnapshot | None = None) -> list[ReviewItem]:
    """Read each record from its existing store without another stored copy."""
    members = staff.snapshot() if members is None else members
    documents = tuple(dict.fromkeys(member.doc.path for member in members.rows
                      if member.kind == 'showrunner' and isinstance(member.doc, staff.LiveDoc)))
    return ordered([*question_items(members,documents,assign_ids=True),*change_items(root,members),
        *investigation_items(members),*proposal_items(members,documents),*efficiency_items(members,documents)])


def read_kind(kind: ReviewType, scope: Path | staff.StaffRow, members: staff.StaffSnapshot, *,
              below: bool = False, sources: list[Path] | None = None,
              own: list[Issue] | None = None) -> list[ReviewItem]:
    """Read the selected store into ReviewItem records for all consumers."""
    if isinstance(scope,staff.StaffRow):
        documents: tuple[Path,...] = ()
        owner = scope
    else:
        documents = tuple(proposals_folder.scope_docs(scope,below,record=members))
        owner = scope
    if kind == ReviewType.QUESTION:
        owners = (scope,) if isinstance(scope,staff.StaffRow) else tuple(member for member in members.rows
            if member.kind == 'showrunner' and isinstance(member.doc,(staff.LiveDoc,staff.WrappedDoc)) and member.doc.path in documents)
        return question_items(members,documents,owners=owners,sources=sources)
    if kind == ReviewType.INVESTIGATION:
        owners_ids = tuple(member.id for doc in documents for member in
            (unit_lookup.doc_owner(doc,members),*unit_lookup.doc_units(doc,members))) if documents else (cast(staff.StaffRow,owner).id,)
        return investigation_items(members,owners=owners_ids,sources=sources)
    if kind == ReviewType.PROPOSAL:
        return proposal_items(members,documents,sources=sources,own=own)
    if kind == ReviewType.EFFICIENCY:
        return efficiency_items(members,documents,sources=sources)
    assert isinstance(scope,Path)
    root = change_routing.review_root() if sources is None else change_routing.review_root(sources=sources)
    items = change_items(root,members,document=scope,below=below,sources=sources)
    tabs = change_review.review_scope(scope,below,members)
    return [item for item in items if isinstance(item.source,ChangeSource) and
            (item.source.assignment.owner.tab in tabs or isinstance(item.source.assignment.owner,change_routing.UnroutedChange))]


def change_counts(doc: Path, record: staff.StaffSnapshot, *, source: str = '',
                  sources: list[Path] | None = None) -> tuple[int,int]:
    try:
        items = read_kind(ReviewType.CHANGE,doc,record,sources=sources)
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
               for item in read_kind(ReviewType.PROPOSAL,doc,record,sources=sources,own=own))


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
                   for item in read_kind(ReviewType.EFFICIENCY,doc,record,sources=sources))
    except (OSError,UnicodeError):
        if sources is not None:
            raise
        return 0


def investigations(doc: Path, record: staff.StaffSnapshot, *, source: str = '', sources: list[Path] | None = None) -> int:
    try:
        items = read_kind(ReviewType.INVESTIGATION,doc,record,sources=sources)
    except staff.StaffUnreadable:
        return 0
    return count_items(items,datetime.now(user_zone()),source)


def question_counts(owner: staff.StaffRow, zone: ZoneInfo, identities: QuestionStaff, sources: list[Path] | None,
                    source: str = '') -> tuple[int,str]:
    items = read_kind(ReviewType.QUESTION,owner,identities.record,sources=sources)
    now = datetime.now(zone)
    afters = [item.times.later for item in items if isinstance(item.times.later,datetime) and item.times.later > now]
    return (sum(item.for_you(now) and (not source or item.source_unit == source) for item in items),
            '' if not afters else str(int(min(afters).timestamp())))


def ready_investigations(owner: staff.StaffId, *, sources: list[Path] | None = None) -> int:
    items = investigation_items(staff.snapshot(),owners=(owner,),sources=sources)
    return sum(item.for_you(datetime.now(user_zone())) for item in items)


def next_queue(kind: str, scope: Path | staff.StaffRow, below: bool, members: staff.StaffSnapshot) -> tuple[str,list[urgency.Entry[str]]]:
    items = read_kind(ReviewType(kind),scope,members,below=below)
    now = datetime.now(user_zone())
    selected = [item for item in items if item.for_you(now) and not
                (isinstance(item.source,ChangeSource) and isinstance(item.source.assignment.owner,change_routing.UnroutedChange))]
    if kind == 'change':
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
