"""Show Review items from their native stores in one table."""
from __future__ import annotations

import html
import hashlib
import gzip
import threading
import logging
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import dataclass, field, replace
import json
import re
import secrets
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import parse_qs, parse_qsl, urlencode
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.for_you.action import ActionStore, ActionStoreError, REVIEW_PATH
from scripts.lib.user_zone import CONF as USER_ZONE_CONF, user_zone
from scripts.production import change_review, change_routing, investigation, proposal_issue
from scripts.production import record_store, staff, the_list, update_registration
from scripts.production.production_doc import Refusal as ProductionRefusal
from scripts.production.record_store import FileStamp, state_root
from scripts.rebuild import need
from scripts.lib import urgency
from scripts.review_links import change_browser, change_page, console, for_you, page_frame, record_table, review_items, review_link
from scripts.review_links import new_item_dialog, review_submissions
from scripts.review_links import question_path, question_record, review_actions, review_events, send_hold, staff_names
from scripts.review_links.review_items import ReviewItem, ReviewSourceStamps, ReviewState, ReviewType, guard_source
from scripts.review_links.review_items import source_stamp as source_stamp
from scripts.statusline import rebuild_row

PAGE_PATH = REVIEW_PATH
SAVE_FAILURE_PATH = '/console/review-save-failure'
DELIVERY_RETRY_PATH = '/console/review-delivery-retry'
SAVE_ATTEMPT_HEADER = 'X-Review-Save-Attempt'
NEW_ITEM_PATH = '/console/review/new-item'
ATTACHMENT_PATH = '/console/review/attachment'
HEADERS = ('Id', 'Type', 'Owner', 'Item', 'State', 'Age')
CARD_COLUMNS = ('<colgroup><col style="width:17%"><col style="width:13%"><col style="width:14%">'
                '<col style="width:37%"><col style="width:12%"><col style="width:7%"></colgroup>')
TYPES = tuple(ReviewType)
WIRE_TYPES = tuple(kind for kind in TYPES if kind != ReviewType.RESPONSE)
TYPE_CHIPS = (ReviewType.FOR_YOU, *review_items.QUESTION_TYPES,
              *(kind for kind in TYPES
                if kind not in (*review_items.QUESTION_TYPES, ReviewType.FOR_YOU, ReviewType.BUG)))
ICONS = {ReviewType.QUESTION: 'circle-help', ReviewType.CHANGE: 'diff', ReviewType.INVESTIGATION: 'microscope',
         ReviewType.PROPOSAL: 'lamp', ReviewType.EFFICIENCY: 'zap', ReviewType.BUG: 'bug', ReviewType.RESPONSE: 'inbox',
         ReviewType.FOR_YOU: 'inbox'}
INDEX_STATES = (ReviewState.OPEN, ReviewState.WAITING, ReviewState.VALIDATING, ReviewState.DONE)
SHOWS = (('all', 'All'), ('open', 'Open'), (ReviewState.DONE, 'Done'))
SHOW_VALUES = tuple(value for value, _ in SHOWS)
SHOW_CHIPS = tuple(record_table.Chip(value, label) for value, label in SHOWS)
LABELS = {ReviewType.QUESTION: 'Questions', ReviewType.CHANGE: 'Changes', ReviewType.INVESTIGATION: 'Investigations',
          ReviewType.PROPOSAL: 'Proposals', ReviewType.EFFICIENCY: 'Efficiencies',
          ReviewType.RESPONSE: 'Responses', ReviewType.FOR_YOU: 'For you'}
FRONT_MATTER = re.compile(r'\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)
CARD_ERROR = b'<p role="status" data-review-card-unavailable>This card is not available.</p>'
REVIEW_REVISION_CHANGED = 'this item changed while Review was open.'
REVIEW_DRAFT_CHOICE_CHANGED = 'this item changed. Open the card to choose again.'


class ReviewBodyView(Enum):
    TABLE = 'table'
    CARD = 'card'


def card_url(key: str) -> str:
    return PAGE_PATH + '?' + urlencode({'body': key, 'view': ReviewBodyView.CARD.value})


class ReviewIndexEntry(TypedDict):
    """Filter and column values for one Review item, without its body."""
    key: str
    id: str
    type: ReviewType
    owner: str
    sourceOwner: str
    ownerName: str
    title: str
    state: ReviewState
    filed: int
    updated: str
    hiddenUntil: float
    sourceName: str
    ownerHtml: str
    sourceHtml: str
    urgency: str
    handoff: NotRequired[tuple[str, str]]
    waitingWhy: NotRequired[str]
    actionStatus: NotRequired[str]
    unreadAnswer: NotRequired[bool]


def record_revision(item: ReviewItem, assets: Mapping[str, str]) -> str:
    """Identify the complete retained record and its displayed assets."""
    source = item.source
    # A Change verdict follows the current production when staff move.
    record = (source.path, source.row, source.slots, item.times.later) if isinstance(source, review_items.ChangeSource) else item
    if isinstance(source, review_items.ChangeSource) and item.state == ReviewState.VALIDATING:
        record = (record, item.owner)
    retained = (record, assets.get(item.key, ''), assets.get('image:'+item.key, ''),
                assets.get('submission:'+item.key, ''))
    return hashlib.sha256(repr(retained).encode()).hexdigest()[:16]


def _matches_feedback_retry(item: ReviewItem, assets: Mapping[str, str], revision: str, token: str, words: str) -> bool:
    """Check the revision before the interrupted Send added its log entry."""
    source = item.source
    if not isinstance(source, review_items.ChangeSource) or item.state != ReviewState.OPEN:
        return False
    request_id = change_page.feedback_request_id(token, source.row, words, source.slots)
    log = review_events.decode(source.row.get('Review log', ''))
    if not log or log[-1].feedback_request_id != request_id:
        return False
    row = dict(source.row)
    previous = log[:-1]
    row['Review log'] = review_events.encode(previous) if previous else ''
    if record_revision(replace(item, source=replace(source, row=row)), assets) == revision:
        return True
    if previous:
        return False
    del row['Review log']
    return record_revision(replace(item, source=replace(source, row=row)), assets) == revision


def filter_index(records: list[ReviewItem], members: staff.StaffSnapshot) -> list[ReviewIndexEntry]:
    """Build the index for the filters and columns."""
    names = staff_names.StaffNames(members)
    zone = user_zone()
    return [card_entry(item, members, names, zone) for item in records
            if item.type == ReviewType.FOR_YOU or item.state != ReviewState.DONE]


def index_json(entries: list[ReviewIndexEntry]) -> str:
    """Share names and encode one compact row per item for the wire."""
    names = dict.fromkeys(pair for entry in entries for pair in (
        (entry['owner'], entry['ownerName'], entry['ownerHtml']),
        (entry['sourceOwner'], entry['sourceName'], entry['sourceHtml'])))
    references = {pair: at for at,pair in enumerate(names)}
    # Older Review tabs read the Question type and ignore the Response flag.
    rows = [[entry['key'], '' if entry['id']==entry['key'] else entry['id'],
             WIRE_TYPES.index(ReviewType.QUESTION if entry['type'] == ReviewType.RESPONSE else entry['type']),
             references[(entry['owner'],entry['ownerName'],entry['ownerHtml'])],
             references[(entry['sourceOwner'],entry['sourceName'],entry['sourceHtml'])],
             entry['title'], INDEX_STATES.index(entry['state']), entry['filed'], entry['updated'],
             entry['hiddenUntil'], '' if entry['urgency']=='normal' else entry['urgency'],
             *([entry.get('handoff'), entry.get('waitingWhy'), entry['type'] == ReviewType.RESPONSE,
                entry.get('actionStatus'), True] if entry.get('unreadAnswer')
               else [None, None, False, entry['actionStatus']] if 'actionStatus' in entry
               else [entry.get('handoff'), entry.get('waitingWhy'), True] if entry['type'] == ReviewType.RESPONSE
               else [entry.get('handoff'), entry['waitingWhy']] if 'waitingWhy' in entry
               else [entry['handoff']] if 'handoff' in entry else [])] for entry in entries]
    return json.dumps({'n':list(names),'r':rows},ensure_ascii=False,separators=(',',':')).replace(
        '&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')


@dataclass(frozen=True)
class ReviewBodyInputs:
    """Keep the data required to render a card without reading its store."""
    browser: change_page.ReviewBrowser
    members: staff.StaffSnapshot
    words_rows: int
    assets: Mapping[str, str]
    zone: ZoneInfo
    names: staff_names.StaffNames


@dataclass(frozen=True)
class ReviewIndexResponse:
    """Keep both encodings of a prepared index."""
    body: bytes = b''
    gzip_body: bytes = b''


@dataclass(frozen=True)
class ReviewSnapshot:
    """Own the current records, compact index and shell until the next complete update."""
    version: str
    records: dict[str, ReviewItem]
    entries: list[ReviewIndexEntry]
    members: staff.StaffSnapshot
    browser: change_page.ReviewBrowser
    words_rows: int
    refresh_seconds: float
    request_timeout_seconds: int
    card_motion: page_frame.CardMotion
    assets: dict[str,str]
    zone: ZoneInfo
    source_stamps: dict[Path, FileStamp]
    later: Mapping[str, int]
    names: staff_names.StaffNames
    log_max_height_rem: int
    page: bytes = b''
    page_gzip: bytes = b''
    full_index: ReviewIndexResponse = ReviewIndexResponse()
    legacy_index: ReviewIndexResponse = ReviewIndexResponse()
    read_at: float = 0
    render_seconds: float = 0
    notices: tuple[str, ...] = ()
    action_notices: tuple[str, ...] = ()
    unavailable_cards: frozenset[str] = frozenset()
    title_limit: int = 100
    undo_enabled: bool = False
    delivery_window_seconds: int = record_table.DELIVERY_WINDOW_SECONDS
    submission_keys: dict[str, str] = field(default_factory=dict)

    def facts(self) -> dict[str,float]:
        return {'readAt':self.read_at,'renderSeconds':self.render_seconds,'answeredAt':time.time()}

    def index_response(self) -> bytes:
        return self.full_index.body

    def render(self, *, show: str = 'open') -> bytes:
        selected = next((value for value in SHOW_VALUES if value.lower() == show.lower()), 'open')
        if selected == 'open':
            return self.page
        return self.page.replace(b'data-show="open"', f'data-show="{selected}"'.encode(), 1).replace(
            record_table.chip_row('Show', SHOW_CHIPS, 'open').encode(),
            record_table.chip_row('Show', SHOW_CHIPS, selected).encode(), 1)

    def body(self, key: str, *, view: ReviewBodyView = ReviewBodyView.TABLE) -> bytes:
        """Render one retained record without a store read."""
        item = self.records.get(key)
        if item is None:
            raise LookupError('This item is not in the current snapshot.')
        body = self.render_body(item, self.assets)
        if view is ReviewBodyView.TABLE:
            if body == CARD_ERROR:
                return body
            summary = index_json([card_entry(item, self.members, self.names, self.zone)])
            return body + f'<script type="application/json" data-review-summary>{summary}</script>'.encode()
        entry = next((entry for entry in self.entries if entry['key'] == key), None)
        if entry is None:
            entry = card_entry(item, self.members, self.names, self.zone)
        return render_card(self, entry, body.decode(), waiting_why=item.waiting_why)

    def linked_card(self, root: Path, key: str, *, view: ReviewBodyView = ReviewBodyView.CARD) -> bytes:
        """Serve an active or completed card."""
        if key in self.records:
            return self.body(key, view=view)
        item = next((item for item in review_items.read(
            root, members=self.members, kinds=TYPES, show=ReviewState.DONE)
            if item.key == key), None)
        if item is None:
            raise LookupError('This item is not in the current snapshot.')
        item = review_items.normalize_times(item, self.zone)
        assets: dict[str, str] = {}
        retain_asset(item, assets)
        body = self.render_body(item, assets)
        if view is ReviewBodyView.TABLE:
            return body
        entry = card_entry(item, self.members, self.names, self.zone)
        return render_card(self, entry, body.decode(), waiting_why=item.waiting_why)

    def render_body(self, item: ReviewItem, assets: Mapping[str, str]) -> bytes:
        body = CARD_ERROR
        if item.key not in self.unavailable_cards:
            try:
                body = details(item, ReviewBodyInputs(self.browser, self.members, self.words_rows,
                    assets, self.zone, self.names)).encode()
            except Exception as error:
                logging.getLogger(__name__).warning('Review card %s: %s', item.key, error)
        return body


def list_destination(item: ReviewItem) -> investigation.ListDestination | investigation.NoListDestination:
    source = item.source
    return (source.record.list_destination if isinstance(source, review_items.InvestigationSource)
            else investigation.NoListDestination.ABSENT)


def report_url(destination: investigation.ListDestination) -> str:
    return page_frame.STUDIO_PAGE_PATH + page_frame.list_item_fragment(destination.keeper, destination.number)


def list_report_details(report: the_list.ListReport) -> str:
    """Use the Review card renderers for the report words and picture."""
    content = section(report.kind.value, report.words)
    if not report.evidence_key.startswith('investigation/'):
        return content
    try:
        if report.submission_scope:
            scope = new_item_dialog.read_submission_key(report.submission_scope)
            with review_submissions.scoped_submission(scope) as located:
                original = located.record
                if not isinstance(original, investigation.Investigation):
                    raise investigation.InvestigationError("The source of this report changed.")
        else:
            original = investigation.list_evidence_record(report.evidence_key)
    except (OSError, ValueError) as error:
        logging.getLogger(__name__).warning("List report evidence %s: %s", report.evidence_key, error)
        return content + '<p>The original evidence is unavailable.</p>'
    if original.answer:
        content += answer_section(original.answer)
    if isinstance(original.submission, new_item_dialog.ReviewSubmission):
        content += submission_assets(review_items.investigation_record_key(original), original.submission)
    return content


def retain_asset(item: ReviewItem, assets: dict[str, str]) -> None:
    source = item.source
    if isinstance(source, review_items.ProposalSource):
        try:
            text = proposal_issue.read_issue(source.record.path)
        except proposal_issue.Refusal:
            try:
                _ = source.record.path.stat()
            except FileNotFoundError:
                raise review_items.ReviewRecordMissing('This item is no longer on the list.') from None
            raise
        assets[item.key] = change_page.review_text_html(FRONT_MATTER.sub('', text, count=1).strip(), inline=True)
    elif isinstance(source, review_items.ChangeSource) and source.row.get('Image'):
        assets['image:'+item.key] = change_review.image_path(source.row['Image'])
    origin = review_items.submission_origin(item)
    if isinstance(origin, new_item_dialog.ReviewSubmission):
        assets['submission:'+item.key] = submission_assets(item.key, origin)


def retain_question_delivery(item: ReviewItem, assets: dict[str, str], status: change_page.DeliveryStatus,
                             sources: ReviewSourceStamps) -> None:
    source = item.source
    if isinstance(source, review_items.ChangeSource):
        row = source.row['#']
        page = change_page.ReviewPage(source.path, source.path.parent, (row,), '')
        question = source.slots.question
    elif item.state == ReviewState.WAITING and isinstance(
            source, (review_items.QuestionSource, review_items.InvestigationSource,
                     review_items.ProposalSource, review_items.EfficiencySource)):
        page = review_actions.delivery_page(item, status)
        row = page.rows[0]
        path = status.path(page, row)
        sources.append(path)
        question = status.slots(page, row).question
    else:
        return
    if not isinstance(question, change_page.OpenQuestion) or isinstance(question, change_page.OpenFeedback):
        return
    sources.append(status.closeout_path(page, row))
    if change_page.review_question_failed(status, page, row, question):
        assets['question-delivery:'+item.key] = change_page.question_failure_html(True)


def _captured_memory_text(text: str | None, gib: float | None) -> str:
    if text is not None:
        return text
    return 'unavailable' if gib is None else f'{gib:g} GiB'


def retain_record_assets(item: ReviewItem, status: change_page.DeliveryStatus,
                         sources: ReviewSourceStamps) -> dict[str, str]:
    """Keep the assets and delivery notice for one Review record."""
    assets: dict[str, str] = {}
    retain_asset(item, assets)
    retain_question_delivery(item, assets, status, sources)
    return assets


SUBMISSION_EVIDENCE_STYLE = """
.review-attachments img{max-width:min(240px,100%);max-height:160px;object-fit:contain;margin-inline-end:8px}
.page-state-box[data-submission-page-state]{margin:12px 0 0;padding:12px 14px}
[data-submission-page-state]>details>summary{min-height:24px}
[data-submission-page-state] .page-state{margin-top:8px}
[data-submission-page-state] img{max-width:min(420px,100%);max-height:270px;object-fit:contain}
"""


def submission_assets(key: str, origin: new_item_dialog.ReviewSubmission) -> str:
    """Render the saved evidence and references for one lazy card."""
    evidence = origin.evidence
    body = ''
    images = origin.images
    if isinstance(evidence, new_item_dialog.BuildStepEvidence):
        cells = ''.join('<div><dt>'+html.escape(cell.label)+'</dt><dd>'+html.escape(cell.value)+'</dd></div>'
                        for cell in evidence.cells)
        memory = evidence.memory
        segments = ' · '.join(f'{part.label} {_captured_memory_text(part.text, part.gib)}' for part in memory.segments)
        free = _captured_memory_text(memory.free_text, memory.free_for_next_gib)
        cap = _captured_memory_text(memory.cap_text, memory.queue_cap_gib)
        reading = html.escape(f'{free} free for the next step · Queue cap {cap}')
        tip = ''.join('<div><dd>'+html.escape(part.text)+'</dd></div>'
                      if isinstance(part, (new_item_dialog.TipDetail, new_item_dialog.TipCaption))
                      else '<div><dt>'+html.escape(part.label)+'</dt><dd>'+html.escape(part.value)+'</dd></div>'
                      for part in evidence.tip)
        body = ('<section data-submission-evidence><h3>Evidence</h3><strong>'+html.escape(evidence.title)
                +'</strong><p>'+html.escape(evidence.owner)+' · '+html.escape(evidence.captured_at)
                +'</p><dl>'+cells+'</dl><p>'+reading+'</p><p>'+html.escape(segments)
                +'</p><strong>'+html.escape(evidence.tip_title)+'</strong><p>'+html.escape(evidence.tip_status)
                +'</p><dl>'+tip+'</dl></section>')
    page_data = origin.bug_page_data
    if origin.page_state is not None:
        screenshot = review_submissions.PAGE_SCREENSHOT
        title = origin.page_state.title or (page_data.title if page_data else origin.page)
        body += bug_page_state_box(key, origin.page_state, title, screenshot in images)
        images = tuple(image for image in images if image != screenshot)
    elif isinstance(evidence, new_item_dialog.PageDataEvidence):
        page_data = evidence
    if page_data is not None:
        data = json.dumps(page_data.page_data(), ensure_ascii=False, indent=2)
        body += '<section data-submission-evidence><h3>Page data</h3><pre>'+html.escape(data)+'</pre></section>'
    if images:
        links: list[str] = []
        for at, image in enumerate(images, 1):
            url = attachment_url(key, image)
            links.append(f'<a href="{url}" target="_blank" rel="noopener"><img loading="lazy" '
                         + f'src="{url}" alt="Attachment {at}"></a>')
        body += '<section class="review-attachments"><h3>Attachments</h3>'+''.join(links)+'</section>'
    return body


def attachment_url(key: str, image: new_item_dialog.ImageReference) -> str:
    return html.escape(ATTACHMENT_PATH+'?'+urlencode({'key': key, 'id': image.id}), quote=True)


def bug_page_state_box(key: str, state: new_item_dialog.BugPageState, title: str, captured: bool) -> str:
    """Show the Bug screenshot and keep the page details in a disclosure."""
    errors: list[str] = []
    for count, label in ((len(state.notices), 'error line'), (len(state.script_errors), 'script error')):
        if count:
            errors.append(f'{count} {label}'+('' if count == 1 else 's'))
    rows = (
        ('Page', title), ('Filters', state.filters or 'None'),
        ('Render time', 'Unavailable' if state.render_seconds is None else f'{state.render_seconds:.2f} s'),
        ('Errors', ' · '.join(errors) or 'None'),
    )
    fields = ''.join(f'<dt>{label}</dt><dd>{html.escape(value)}</dd>' for label, value in rows)
    figure = ''
    if captured:
        url = attachment_url(key, review_submissions.PAGE_SCREENSHOT)
        caption = html.escape('Screenshot of '+title, quote=True)
        figure = (f'<figure><a data-image-zoom href="{url}" target="_blank" rel="noopener">'
                  f'<img src="{url}" loading="lazy" alt="{caption}"></a><figcaption>{caption}</figcaption></figure>')
    return ('<section class="page-state-box" data-submission-page-state><details><summary>Page details</summary>'
            f'<dl class="page-state">{fields}</dl></details>{figure}</section>')


def card_entry(item: ReviewItem, members: staff.StaffSnapshot, names: staff_names.StaffNames,
               zone: ZoneInfo) -> ReviewIndexEntry:
    owner = owner_name(item, members)
    source = review_items.name(item.source_unit, members)
    entry = ReviewIndexEntry(key=item.key, id=item.id, type=item.type, owner=str(item.owner),
        sourceOwner=str(item.source_unit), ownerName=owner, title=item.title, state=item.state,
        filed=int(item.times.filed.timestamp()), updated=item.times.updated.isoformat(),
        hiddenUntil=item.times.later.timestamp()*1000 if isinstance(item.times.later, datetime) else 0,
        sourceName=source, urgency=item.urgency.level, ownerHtml=names.html(item.owner, owner),
        sourceHtml=names.html(item.source_unit, source))
    if item.unread_answer:
        entry['unreadAnswer'] = True
    if isinstance(item.source, review_items.ChangeSource) and item.source.handed_at is not None:
        requested = item.source.handed_at.astimezone(zone)
        entry['handoff'] = (requested.isoformat(), requested.strftime('%b %d %H:%M %Z'))
    if (item.state == ReviewState.WAITING and item.waiting_why and (
            isinstance(item.source, review_items.ChangeSource)
            or isinstance(review_items.submission_origin(item), new_item_dialog.ReviewSubmission))):
        entry['waitingWhy'] = item.waiting_why
    if isinstance(item.source, review_items.ForYouSource):
        entry['actionStatus'] = item.source.row.status
    validate_text(entry['title'])
    for value in entry.values():
        if isinstance(value, str):
            _ = value.encode('utf-8')
    return entry


def validate_body_inputs(item: ReviewItem, assets: Mapping[str, str]) -> None:
    for event in item.log:
        validate_text(event.who)
        validate_text(event.words)
    for text in assets.values():
        validate_text(text)


def validate_text(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError('A card field must be text.')
    _ = value.encode('utf-8')


def pack_snapshot(snapshot: ReviewSnapshot, index: str, started: float) -> ReviewSnapshot:
    snapshot = replace(snapshot, render_seconds=time.perf_counter()-started)
    metadata = (',"version":'+json.dumps(snapshot.version)
               +',"notices":'+json.dumps(snapshot.notices)+',"actionNotices":'+json.dumps(snapshot.action_notices)
               +',"presentation":'+json.dumps({'zone':snapshot.zone.key,'later':dict(snapshot.later),
                                                'titleLimit':snapshot.title_limit,
                                                'logMaxHeightRem':snapshot.log_max_height_rem,
                                                'requestTimeoutSeconds':snapshot.request_timeout_seconds,
                                                'deliveryWindowSeconds':snapshot.delivery_window_seconds,
                                                'cardFadeOutMs':snapshot.card_motion.fade_out_ms,
                                                'cardFadeInMs':snapshot.card_motion.fade_in_ms})
               +',"facts":'+json.dumps(snapshot.facts())+'}').encode()
    def prepare_index(rows: str) -> ReviewIndexResponse:
        body = b'{"index":'+rows.encode()+metadata
        return ReviewIndexResponse(body, gzip.compress(body, compresslevel=1, mtime=0))

    full_index = prepare_index(index)
    legacy_entries = [entry for entry in snapshot.entries if entry['type'] != ReviewType.FOR_YOU]
    legacy_index = full_index if len(legacy_entries) == len(snapshot.entries) else prepare_index(index_json(legacy_entries))
    page = render_shell(snapshot,index,started,snapshot.read_at)
    return replace(snapshot, page=page, page_gzip=gzip.compress(page, compresslevel=1, mtime=0),
                   full_index=full_index, legacy_index=legacy_index)


def moved_report_evidence(members: staff.StaffSnapshot, sources: ReviewSourceStamps, zone: ZoneInfo
                          ) -> list[ReviewItem]:
    """Retain image access for moved reports without reading other Done records."""
    kind = record_store.RecordKind.INVESTIGATION
    items: list[ReviewItem] = []
    for owner in record_store.store_owners(kind):
        folders = record_store.known_folders(kind, owner, sources=sources)
        for path in review_items.store_paths(folders, sources):
            with guard_source(path, sources):
                header = record_store.read_header(path, keys=frozenset(('state', 'list_destination')), strict=True)
                if header.get('state') == [investigation.InvestigationState.READ] and header.get('list_destination'):
                    record = investigation.read_review_record(path)
                    items.append(review_items.investigation_item(record, members, path, zone=zone))
    return items


def build_snapshot(root: Path) -> ReviewSnapshot:
    """Read the stores before publication and retain all request dependencies."""
    started, read_at = time.perf_counter(), time.time()
    folder = root / change_page.CONSOLE_FOLDER
    sources = ReviewSourceStamps()
    sources.extend((folder/change_browser.MANIFEST, staff.record_path(), state_root(),
                    change_review.lists_directory(), urgency.state_dir(), conf.path('review_links'), conf.path('production'), conf.path('statusline'),
                    Path(USER_ZONE_CONF)))
    zone = user_zone()
    token,initial = change_browser.browser_manifest(folder)
    sources.extend(for_you.source_paths())
    actions = for_you.read()
    members = actions.members
    names = staff_names.StaffNames(members)
    sources.extend(urgency.state_dir().glob('*.json'))
    records: dict[str, ReviewItem] = {}
    entries: list[ReviewIndexEntry] = []
    assets: dict[str, str] = {}
    submission_keys: dict[str, str] = {}
    unavailable_cards: set[str] = set()
    status = change_page.DeliveryStatus(root)
    items = review_items.read(root, members=members, show='active', sources=sources, zone=zone)
    items.extend(moved_report_evidence(members, sources, zone))
    items.extend(review_items.for_you_item(row, members, zone=zone) for row in actions.rows)
    for item in items:
        path = review_items.source_path(item)
        with guard_source(path, sources, entry=item.id):
            item = review_items.normalize_times(item, zone)
            try:
                record_assets = retain_record_assets(item, status, sources)
                validate_body_inputs(item, record_assets)
                summary = card_entry(item, members, names, zone)
            except (UnicodeError, TypeError, ValueError, OSError, RuntimeError) as error:
                sources.skipped(path, item.id, error)
                unavailable_cards.add(item.key)
                summary = card_entry(replace(item, title='Card unavailable', log=(),
                                             answer_time=question_path.NO_ANSWER_TIME), members, names, zone)
            else:
                assets.update(record_assets)
                origin = review_items.submission_origin(item)
                if isinstance(origin, new_item_dialog.ReviewSubmission):
                    submission_keys[origin.request_id] = item.key
            if not review_items.on_list(item):
                entries.append(summary)
            records[item.key] = item
    browser = change_page.ReviewBrowser(token, initial)
    settings = conf.read('review_links')
    words_rows = settings.whole('review_words_rows')
    refresh_seconds = settings.number('page_refresh_seconds')
    request_timeout = settings.whole('page_request_timeout_seconds')
    delivery_window = record_table.delivery_window_seconds(settings)
    card_motion = page_frame.CardMotion.read(settings)
    undo_enabled = settings.whole('undo_seconds') > 0
    later = record_table.park_settings(settings)
    title_limit = conf.read_or_repository('production').whole('question_title_max_chars')
    log_max_height = record_table.log_max_height_rem(settings)
    index = index_json(entries)
    notices = tuple(sources.notices.values())
    action_notices = tuple(review_items.utf8_display(f'{failure.source}: {failure.reason}') for failure in actions.unavailable)
    retained = repr((records, assets, members.rows, browser, words_rows, refresh_seconds, request_timeout,
                     card_motion, zone.key, notices, action_notices, unavailable_cards, later, title_limit, delivery_window, names.colors,
                     log_max_height, undo_enabled))
    version = hashlib.sha256((index+retained).encode()).hexdigest()[:20]
    snapshot = ReviewSnapshot(version,records,entries,members,
        browser,words_rows,refresh_seconds,request_timeout,card_motion,assets,zone,sources.before_read,later,names,log_max_height,read_at=read_at,
        notices=notices, action_notices=action_notices, unavailable_cards=frozenset(unavailable_cards),
        title_limit=title_limit, undo_enabled=undo_enabled, delivery_window_seconds=delivery_window,
        submission_keys=submission_keys)
    return pack_snapshot(snapshot, index, started)


@dataclass(frozen=True)
class SnapshotUnavailable:
    """No complete snapshot has passed its first read."""


@dataclass(frozen=True)
class ReviewRecordNotPublished:
    """The response has no published record for this save."""


@dataclass(frozen=True)
class ConfirmedReviewDetail:
    body: str
    revision: str


@dataclass(frozen=True)
class ClosedReviewDetail:
    pass


@dataclass(frozen=True)
class ReviewDetailUnavailable:
    pass


ReviewActionDetail = ConfirmedReviewDetail | ClosedReviewDetail | ReviewDetailUnavailable


@dataclass(frozen=True)
class ReviewActionSave:
    """The action result before Review publishes the record."""
    delivery: change_page.MessageDelivery | change_page.NoMessageDelivery
    answer: str
    words_saved: bool


class ResponseDeliveryReservation(Enum):
    NOT_TRACKED = "not tracked"


type ReserveResponseDelivery = Callable[[Path, str], None] | Literal[ResponseDeliveryReservation.NOT_TRACKED]


@dataclass(frozen=True)
class ReviewSave:
    """The saved record, its published version and any message to deliver."""
    delivery: change_page.MessageDelivery | change_page.NoMessageDelivery
    answer: str
    version: str
    item: ReviewItem | ReviewRecordNotPublished
    submitted_type: ReviewType
    words_saved: bool
    detail: ReviewActionDetail = field(default_factory=ReviewDetailUnavailable)


@dataclass(frozen=True)
class PublishedSubmission:
    """A native Send in the current Review index."""
    saved: review_submissions.SavedSubmission
    key: str
    version: str
    members: staff.StaffSnapshot


@dataclass
class ReviewSnapshotCache:
    """Publish complete snapshots from one background worker and retain the last good snapshot."""
    root: Path
    interval: float = 0.5
    start_worker: bool = True
    initialize_on_start: bool = False
    staff_scope: Callable[[], AbstractContextManager[None]] = field(default=nullcontext, kw_only=True)
    _snapshot: ReviewSnapshot | SnapshotUnavailable = field(default_factory=SnapshotUnavailable,init=False)
    _condition: threading.Condition = field(default_factory=threading.Condition,init=False)
    _save_lock: threading.Lock = field(default_factory=threading.Lock,init=False)
    _wake: threading.Event = field(default_factory=threading.Event,init=False)
    _stop: threading.Event = field(default_factory=threading.Event,init=False)
    _busy: bool = field(default=False,init=False)
    _initialization_requested: bool = field(default=False,init=False)
    _generation: int = field(default=0,init=False)
    _thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        if self.initialize_on_start:
            try:
                _ = review_submissions.move_reply_proofs(review_submissions.LocalProofMove(self.root), dry_run=False)
            except (review_submissions.ProofMoveNoteFailure, record_store.ReviewScanRefusal) as error:
                raise OSError(f"Cannot start Review links because the reply proof move failed. {error}") from None
            except (OSError, ValueError, update_registration.RegistrationFailure):
                raise OSError("Cannot start Review links because the reply proof move failed.") from None
        elif (self.root/change_page.CONSOLE_FOLDER/change_browser.MANIFEST).exists():
            self._initialization_requested = True
            try:
                with self.staff_scope():
                    self._snapshot = build_snapshot(self.root)
            except Exception as error:
                logging.getLogger(__name__).warning('Review snapshot: %s',error)
        self._thread = threading.Thread(target=self._update,name='review-snapshot',daemon=True)
        if self.start_worker:
            self.start()

    def start(self) -> None:
        if self.initialize_on_start:
            self._initialization_requested = True
            self._wake.set()
        self._thread.start()

    def get(self) -> ReviewSnapshot:
        with self._condition:
            if isinstance(self._snapshot, SnapshotUnavailable) and self._thread.is_alive():
                if not self._busy:
                    self._initialization_requested = True
                    self._wake.set()
                _ = self._condition.wait_for(
                    lambda: not isinstance(self._snapshot, SnapshotUnavailable) or
                    (not self._busy and not self._wake.is_set()), timeout=15)
            current = self._snapshot
        if isinstance(current,SnapshotUnavailable):
            raise ValueError('The Review snapshot is not available.')
        return current

    def invalidate(self) -> None:
        with self._condition:
            self._initialization_requested = True
            self._generation += 1
            self._wake.set()

    def failure_card_label(self, key: str) -> str:
        """Use the card title when Review still has it."""
        with self._condition:
            current = self._snapshot
        if isinstance(current, ReviewSnapshot) and key in current.records:
            item = current.records[key]
            if item.id and item.id != key:
                return f'{item.id} · {item.title} ({key})'
            return f'{key} · {item.title}'
        return key

    def save(self, body: str, status: change_page.DeliveryStatus, *,
             reserve_response_delivery: ReserveResponseDelivery = ResponseDeliveryReservation.NOT_TRACKED) -> ReviewSave:
        """Serialize Review writes and publish the saved record before returning."""
        _ = self.get()
        fields = review_fields(body)
        key = fields['row'][0]
        with self._save_lock:
            current = self.get()
            try:
                action = submit_snapshot(self.root, current, fields, status,
                                         reserve_response_delivery=reserve_response_delivery)
            except review_items.ReviewRecordChanged:
                try:
                    if key in current.records:
                        _ = self.publish_record(current, key)
                except Exception as error:
                    logging.getLogger(__name__).warning('Review conflict publication for %s: %s', key, error)
                finally:
                    self.invalidate()
                raise
            answer = action.answer
            version = current.version
            detail: ReviewActionDetail = ReviewDetailUnavailable()
            item: ReviewItem | ReviewRecordNotPublished = ReviewRecordNotPublished()
            try:
                updated, item = self.publish_record(current, key)
                version = updated.version
                if not isinstance(item, ReviewItem) or review_items.on_list(item):
                    detail = ClosedReviewDetail()
                elif fields['outcome'][0] not in ('question', 'feedback', 'validate'):
                    detail = ClosedReviewDetail()
                else:
                    item = review_items.normalize_times(item, updated.zone)
                    assets: dict[str, str] = updated.assets
                    if item.state == ReviewState.DONE:
                        assets = {}
                        retain_asset(item, assets)
                    rendered = updated.render_body(item, assets)
                    if rendered != CARD_ERROR:
                        detail = ConfirmedReviewDetail(rendered.decode(), record_revision(item, assets))
                if (isinstance(item, ReviewItem) and isinstance(item.source, review_items.ChangeSource)
                        and fields['outcome'][0] in ('works', 'not works')):
                    answer = item.source.row.get('Verdict') or answer
            except Exception as error:
                logging.getLogger(__name__).warning('Review saved %s. Publication will retry: %s', key, error)
            finally:
                self.invalidate()
            return ReviewSave(action.delivery, answer, version, item, current.records[key].type,
                              words_saved=action.words_saved, detail=detail)

    def publish_delivery_result(self, key: str) -> None:
        """Publish the delivery result before reporting that delivery ended."""
        with self._save_lock:
            current = self.get()
            if key not in current.records:
                return
            try:
                _ = self.publish_record(current, key)
            finally:
                self.invalidate()

    def publish_record(self, current: ReviewSnapshot, key: str, *,
                       members: staff.StaffSnapshot | None = None
                       ) -> tuple[ReviewSnapshot, ReviewItem | ReviewRecordNotPublished]:
        """Replace or remove one record while serving the previous version to readers."""
        if members is None:
            members = staff.snapshot()
        try:
            item: ReviewItem | ReviewRecordNotPublished = review_items.read_current(self.root, current.records[key], members)
        except (FileNotFoundError, review_items.ReviewRecordMissing):
            item = ReviewRecordNotPublished()
        return self.publish_item(current, key, item, members), item

    def create_submission(
        self, request: (new_item_dialog.NewItemRequest | new_item_dialog.UnverifiedDraftRetry
                        | review_submissions.ListDraftSend), *,
        screenshot: new_item_dialog.SubmissionScreenshot = new_item_dialog.NO_PAGE_SCREENSHOT,
        capture_requested: bool = False,
        refusal: record_store.SubmissionError | None = None,
        capture_intent: new_item_dialog.SubmissionCaptureIntent = new_item_dialog.NoDeferredCapture.ABSENT,
        capture_target: new_item_dialog.ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget
        = new_item_dialog.NoBoundCaptureTarget.UNBOUND,
        initial_delivery: review_submissions.InitialDelivery | review_submissions.NoInitialDelivery
        = review_submissions.NO_INITIAL_DELIVERY,
    ) -> PublishedSubmission:
        """File the new record and publish it from its exact native path."""
        _ = self.get()
        with self._save_lock:
            current = self.get()
            remote_staff = not staff.is_owner()
            members = staff.snapshot()
            with (staff.supplied_snapshot(members) if remote_staff else nullcontext(), ExitStack() as filing):
                def select_type(item: new_item_dialog.NewItemRequest) -> None:
                    if item.kind == new_item_dialog.SubmissionKind.BUG:
                        filing.enter_context(investigation.file_for_list(investigation.InvestigationPurpose.BUG_REPORT))
                    elif item.kind == "enhancement":
                        filing.enter_context(investigation.file_for_list(investigation.InvestigationPurpose.ENHANCEMENT))
                    elif item.kind == new_item_dialog.SubmissionKind.INVESTIGATION:
                        filing.enter_context(investigation.file_for_list(
                            investigation.InvestigationPurpose.LIST_INVESTIGATION))

                if isinstance(request, review_submissions.ListDraftSend):
                    prepare = request.prepare

                    def prepare_typed_draft() -> review_submissions.ListDraftFiling:
                        draft = prepare()
                        select_type(draft.request)
                        return draft

                    request = replace(request, prepare=prepare_typed_draft)
                elif isinstance(request, new_item_dialog.NewItemRequest):
                    select_type(request)
                saved = review_submissions.submit(request, members=members, screenshot=screenshot,
                                                  capture_requested=capture_requested, refusal=refusal,
                                                  capture_intent=capture_intent, capture_target=capture_target,
                                                  initial_delivery=initial_delivery)
            if not remote_staff:
                members = staff.snapshot()
            path = saved.path
            stamps = {source: source_stamp(source) for source in (path, path.parent)}
            try:
                if isinstance(saved.record, investigation.Investigation) and saved.record.belongs_on_list:
                    with staff.supplied_snapshot(members):
                        review_actions.ensure_submission_list_item(saved.address)
                    stamps[path] = source_stamp(path)
                projected = review_items.submission_item(saved, members)
                previous_key = current.submission_keys.get(saved.request_id, projected.key)
                previous = current.records.get(previous_key)
                if isinstance(projected.source, review_items.ProposalSource) and previous is not None:
                    projected = replace(projected, queue_after=previous.queue_after)
                item = review_items.read_current(self.root, projected, members)
                origin = review_items.submission_origin(item)
                if not isinstance(origin, new_item_dialog.ReviewSubmission) or origin.request_id != saved.request_id:
                    raise review_items.ReviewRecordChanged('The saved Review item changed.')
                fresh = self.publish_item(current, item.key, item, members, stamps=stamps,
                                          replaced_key=previous_key)
            finally:
                self.invalidate()
            return PublishedSubmission(saved, item.key, fresh.version, members)

    def attach_page_screenshot(self, saved: review_submissions.SavedSubmission, image: bytes) -> bool:
        """Add the screenshot to the native record and refresh its Review card."""
        with self._save_lock:
            if review_submissions.attach_page_screenshot(saved.key, image):
                self._publish_submission_evidence(saved.request_id)
                return True
            return False

    def attach_deferred_page_screenshot(self, target: new_item_dialog.ScopedSubmissionKey,
                                        image: new_item_dialog.PageScreenshot) -> review_submissions.ScreenshotAttachment:
        """Attach a deferred screenshot and publish the updated Review card."""
        with self._save_lock:
            attached = review_submissions.attach_submission_screenshot(target, image)
            if attached is not review_submissions.ScreenshotAttachment.UNAVAILABLE:
                self._publish_submission_evidence(target.request_id)
            return attached

    def publish_submission_evidence(self, request_id: str) -> None:
        """Publish evidence added to an existing submission."""
        with self._save_lock:
            self._publish_submission_evidence(request_id)

    def _publish_submission_evidence(self, request_id: str) -> None:
        try:
            current = self.get()
            key = current.submission_keys.get(request_id)
            if key is not None:
                _ = self.publish_record(current, key, members=current.members)
        finally:
            self.invalidate()

    def publish_item(self, current: ReviewSnapshot, key: str, item: ReviewItem | ReviewRecordNotPublished,
                     members: staff.StaffSnapshot, *, stamps: Mapping[Path, FileStamp] | None = None,
                     replaced_key: str = '') -> ReviewSnapshot:
        """Insert, replace or remove one retained record."""
        started = time.perf_counter()
        records = current.records.copy()
        assets = current.assets.copy()
        asset_keys = (key, 'image:'+key, 'submission:'+key, 'question-delivery:'+key)
        previous_key = replaced_key or key
        previous = records.get(previous_key)
        if previous_key != key:
            _ = records.pop(previous_key, None)
        for removed in {previous_key, key}:
            for asset in (removed, 'image:'+removed, 'submission:'+removed, 'question-delivery:'+removed):
                _ = assets.pop(asset, None)
        submission_keys = current.submission_keys.copy()
        if previous is not None:
            previous_origin = review_items.submission_origin(previous)
            if isinstance(previous_origin, new_item_dialog.ReviewSubmission):
                _ = submission_keys.pop(previous_origin.request_id, None)
        names = staff_names.StaffNames(members)
        sources = ReviewSourceStamps()
        if isinstance(item, ReviewRecordNotPublished) or (
                item.state == ReviewState.DONE and item.type != ReviewType.FOR_YOU
                and not review_items.on_list(item)):
            _ = records.pop(key, None)
            entries = [entry for entry in current.entries if entry['key'] not in (key, previous_key)]
            record_version = key
        else:
            item = review_items.normalize_times(item, current.zone)
            record_assets = retain_record_assets(item, change_page.DeliveryStatus(self.root), sources)
            assets.update(record_assets)
            if (stamps is not None and current.records.get(key) == item and members == current.members
                    and key not in current.unavailable_cards
                    and all(assets.get(asset) == current.assets.get(asset) for asset in asset_keys)):
                return current
            records[key] = item
            origin = review_items.submission_origin(item)
            if isinstance(origin, new_item_dialog.ReviewSubmission):
                submission_keys[origin.request_id] = key
            validate_body_inputs(item, record_assets)
            if not review_items.on_list(item):
                updated_entry = card_entry(item, members, names, current.zone)
                entries = [updated_entry if entry['key'] == previous_key else entry for entry in current.entries
                           if entry['key'] != key or key == previous_key]
                if not any(entry['key'] == key for entry in entries):
                    entries.append(updated_entry)
            else:
                entries = [entry for entry in current.entries if entry['key'] not in (key, previous_key)]
            record_version = repr((item, tuple(assets.get(asset) for asset in asset_keys)))
        version = hashlib.sha256((current.version+record_version).encode()).hexdigest()[:20]
        fresh = pack_snapshot(replace(current, version=version, members=members, names=names, records=records, assets=assets, entries=entries,
                                      source_stamps={**current.source_stamps, **(stamps or {}), **sources.before_read},
                                      unavailable_cards=current.unavailable_cards-{key, previous_key},
                                      submission_keys=submission_keys),
                              index_json(entries), started)
        with self._condition:
            self._snapshot = fresh
        return fresh

    def synchronize(self) -> None:
        """Let tests and shutdown observe completed background work."""
        with self._condition:
            _ = self._condition.wait_for(lambda:not self._busy and not self._wake.is_set(),timeout=30)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.ident is not None:
            self._thread.join(5)

    def _update(self) -> None:
        checked = time.monotonic()
        last_failure: str | None = None
        while not self._stop.is_set():
            _ = self._wake.wait(self.interval)
            if self._stop.is_set():
                return
            try:
                with self._condition:
                    snapshot = self._snapshot
                    generation = self._generation
                unchanged = (isinstance(snapshot, ReviewSnapshot) and time.monotonic()-checked<30
                             and all(source_stamp(path)==stamp for path, stamp in snapshot.source_stamps.items()))
                with self._condition:
                    if isinstance(snapshot, SnapshotUnavailable) and not self._initialization_requested:
                        continue
                    if unchanged and not self._wake.is_set():
                        continue
                    self._wake.clear()
                    self._busy = True
                if not (self.root/change_page.CONSOLE_FOLDER/change_browser.MANIFEST).exists():
                    _ = change_page.console_folder(self.root)
                with self.staff_scope():
                    fresh = build_snapshot(self.root)
                with self._save_lock, self._condition:
                    if self._snapshot is not snapshot or self._generation != generation:
                        self._wake.set()
                        continue
                    self._snapshot = fresh
                    self._condition.notify_all()
                checked = time.monotonic()
                last_failure = None
            except Exception as error:
                failure = ' '.join(str(error).splitlines())
                if failure != last_failure:
                    logging.getLogger(__name__).warning('Review snapshot update: %s', failure)
                    last_failure = failure
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()


def page_url() -> str:
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def waiting_items(request: console.NewsRequest, *, snapshot: ReviewSnapshot | None = None) -> frozenset[str]:
    if snapshot is None:
        records = request.shared(review_items.read_open)
    else:
        now = datetime.now(snapshot.zone)
        records = (item for item in snapshot.records.values() if item.for_you(now))
    return frozenset(item.key+'/'+item.times.updated.isoformat() for item in records
                     if item.type != ReviewType.FOR_YOU and not review_items.on_list(item))


def legacy_url(path: str, query: str) -> str:
    filters = dict(parse_qsl(query, keep_blank_values=True))
    filters['type'] = ('investigation' if path.endswith('investigations.html') else
                       'question' if path.endswith('questions.html') or filters.get('type') == 'questions' else 'change')
    if filters['type'] == 'investigation':
        filters['legacy'] = 'investigation'
    return PAGE_PATH + '?' + urlencode(filters)


def icon(kind: ReviewType) -> str:
    return '<span class="review-icon '+kind+'">'+page_frame.icon_svg(ICONS[kind])+'</span>'


def section(heading: str, text: str) -> str:
    paragraphs = change_browser.question_paragraphs(text)
    empty = '' if paragraphs else ' data-empty-review-section'
    return '<section'+empty+'><h3>'+heading+'</h3>'+paragraphs+'</section>'


def answer_section(text: str) -> str:
    return '<section><h3>Answer</h3>'+change_browser.answer_html(text)+'</section>'


def waiting_change(item: ReviewItem) -> bool:
    return item.type == ReviewType.CHANGE and item.state == ReviewState.WAITING


def assign_menu(item: ReviewItem, names: staff_names.StaffNames) -> str:
    shown = review_items.name(item.owner, names.members)
    options = ''.join(f'<option value="{html.escape(member.id, quote=True)}" '
                      + f'data-send-receiver="{html.escape(member.name, quote=True)}"{names.style(member.id)}>'
                      +f'{html.escape(member.name)}</option>'
                      for member in review_actions.approval_assignees(names.members))
    return page_frame.action_select('assignee', 'Assign',
        f'<option value=""{names.style(item.owner)}>{html.escape(shown)} decides</option>'+options)


def form(item: ReviewItem, browser: change_page.ReviewBrowser, words_rows: int, revision: str, *,
         owner: str = '', validator: str = '', assignment: str = '', routes: str = '',
         names: staff_names.StaffNames | None = None) -> str:
    if (review_items.on_list(item) or item.state == ReviewState.DONE
            or (item.type == ReviewType.FOR_YOU and not item.actions)):
        return ''
    fields = {'row': item.key, 'token': change_browser.row_token(browser, item.key), 'revision': revision}
    hidden = ''.join(f'<input type="hidden" name="{key}" value="{html.escape(value, quote=True)}">' for key, value in fields.items())
    closes: list[str] = []
    returns: list[str] = []
    labels = {'answer': 'Answer', 'validate': 'Ask showrunner to check',
              'question': 'Ask question', 'feedback': 'Send feedback', 'read': 'Mark read',
              'approve': 'Approve', 'decline': 'Decline', 'done': 'Done'}
    acknowledge = ('<button name="outcome" value="read">Mark read</button>'
                   if can_acknowledge_answer(item) else '')
    if item.type == ReviewType.RESPONSE:
        if not item.actions and not acknowledge:
            return ''
        labels.update(read='Got it', question='Ask a follow-up')
    elif validator:
        labels['question'] = 'Ask a question'
    for index, action in enumerate(item.actions):
        primary = index == 0 and not validator
        if action in (page_frame.VerdictAction.WORKS, page_frame.VerdictAction.NOT_FIXED):
            button = page_frame.verdict_button(page_frame.VerdictItemType.CHANGE, page_frame.VerdictAction(action),
                                               primary=primary)
        else:
            style = ' class="primary"' if primary else ''
            button = f'<button name="outcome" value="{action}"{style}>{labels[action]}</button>'
        (returns if action == 'question' or (item.type == ReviewType.CHANGE and action in change_page.CHANGE_HANDOFFS)
         else closes).append(button)
        if action == 'approve':
            closes.append(assignment)
    later = ''
    if (item.state not in (ReviewState.DONE, ReviewState.VALIDATING)
            and item.type not in (ReviewType.RESPONSE, ReviewType.FOR_YOU) and not waiting_change(item)):
        later = record_table.park_control('Later', field='later', action='outcome', value='later')
    words = ''
    if item.type == ReviewType.FOR_YOU or (acknowledge and not (closes or returns or later)):
        words = '<input type="hidden" name="feedback" value="">'
    elif closes or returns or later:
        field = 'words-'+html.escape(item.key, quote=True)
        if item.type == ReviewType.RESPONSE:
            placeholder = 'Got it sends your words and closes the item. Ask a follow-up sends a question.'
        elif validator:
            placeholder = 'Ask the validator about this item.'
        else:
            placeholder = 'Type your words. They go with the button that you press.'
        words = (f'<label for="{field}">Your words</label><textarea id="{field}" name="feedback" rows="{words_rows}" '
                 + f'placeholder="{placeholder}"></textarea>')
    if item.type == ReviewType.RESPONSE:
        shown_owner = names.html(item.owner, owner) if names is not None else html.escape(owner)
        groups = ('<div class="review-actions"><div class="review-action-group"><span>Closes the item</span><div>'
                  +''.join(closes)+'</div></div><div class="review-action-group"><span>Sends to '
                  +shown_owner+'</span><div>'+''.join(returns)+'</div></div></div>')
    elif validator:
        shown_validator = names.html(item.owner, validator) if names is not None else html.escape(validator)
        groups = ('<div class="review-actions"><div class="review-action-group"><span>Sends to '
                  +shown_validator+'</span><div>'+''.join(returns)+'</div></div></div>')
    else:
        heading = ''
        if item.type == ReviewType.CHANGE and returns:
            names = names if names is not None else staff_names.StaffNames(staff.StaffSnapshot(()))
            heading = 'Sends to ' + names.role_label('showrunner', 'showrunner')
        groups = page_frame.review_action_groups(''.join(closes), ''.join(returns), later, returning_heading_html=heading)
    return (f'<form method="post" data-review-form data-review-type="{item.type}" data-review-state="{item.state}" {routes}>'
            +hidden+words+groups+acknowledge
            +'<span role="status" data-send-status hidden></span></form>')


def log_html(item: ReviewItem, inputs: ReviewBodyInputs, owner: str) -> str:
    rows: list[str] = []
    response = item.response
    for index, entry in enumerate(item.log):
        time = entry.time.isoformat()
        words = entry.words
        if index in item.body_answer_events and words != 'Answered the question':
            words = 'Answered'
        actor = inputs.names.html(entry.who, owner if response is not None and entry.who == item.owner else entry.who)
        rows.append(f'<li data-log-index="{index}"><time datetime="{time}">'
                    +entry.time.astimezone(inputs.zone).strftime('%b %d %H:%M')+'</time><span>'
                    +html.escape(words)+' <small>— '+actor+'</small></span></li>')
    return ''.join(reversed(rows))


def for_you_details(item: ReviewItem, row: for_you.ForYouRow, inputs: ReviewBodyInputs) -> str:
    """Render the action in the shared Review card."""
    requester = row.requested_by or 'Not recorded'
    rebuild = row.kind == 'rebuild'
    asker = inputs.names.html(row.requester_reference, requester) if rebuild else inputs.names.html(item.owner, requester)
    if rebuild and not row.requests:
        asker = 'Found by the system check'
    completion = 'Manual'
    if row.completion == 'automatic':
        completion = 'Automatic after rebuild' if rebuild else 'Automatic'
    fields = (
        ('Machine', html.escape(row.machine)),
        ('Working directory', html.escape(row.cwd or 'Not recorded')),
        ('Asked by' if rebuild else 'Requester', asker),
        *(() if rebuild else (('Waiting work', html.escape(row.waits)),)),
        ('Completion', completion),
    )
    metadata = '<dl class="page-state">'+''.join(f'<dt>{label}</dt><dd>{text}</dd>' for label, text in fields)+'</dl>'
    reason = section('Reason', row.reason) if row.reason else ''
    evidence = section('Withdrawal reason' if row.status == 'withdrawn' else 'Evidence', row.evidence) if row.evidence else ''
    link = '<p><a href="/console/?page=rebuilds" target="_top">Rebuild details</a></p>' if rebuild else ''
    if rebuild:
        why: list[str] = []
        for request in row.requests:
            shown = datetime.fromisoformat(request.created_at).astimezone(inputs.zone)
            name = inputs.names.html(request.requester_reference, request.requested_by or 'Not recorded')
            label = f'{shown:%b} {shown.day}, {shown:%H:%M %Z}'
            text = ('<span>' + html.escape(request.reason) + '</span>' if request.reason
                    else f'<span class="rebuild-secondary">{need.NO_REASON}</span>')
            why.append(f'<li>{text}<small>{name} · <time datetime="{shown.isoformat()}">{label}</time></small></li>')
        history = '<ul class="rebuild-why">' + ''.join(why) + '</ul>' if why else ''
        comparison = (f'<p>{html.escape(row.reason or rebuild_row.CHANGES_NOT_READ)}</p>'
                      if row.reason or not why else '')
        reason = '<section><h3>Why</h3>' + history + comparison + '</section>'
        if not row.running_revision:
            reason += '<p class="rebuild-secondary">The running revision is unknown.</p>'
    form_item = replace(item, actions=('done',)) if row.kind == 'action' and row.status == 'open' else item
    return ('<div class="list-panel review-panel for-you"><div data-review-body>'
            +page_frame.copy_code_block(row.command)+reason+evidence+link+'</div><div>'+metadata+'</div>'
            +form(form_item, inputs.browser, inputs.words_rows, record_revision(item, inputs.assets),
                  routes=change_browser.send_route_attributes({"done": None}))+'</div>')


def review_action_recipients(item: ReviewItem, members: staff.StaffSnapshot) -> dict[str, tuple[staff.StaffId, ...]]:
    """Resolve the staff IDs that each action uses."""
    source = item.source
    recipients: dict[str, tuple[staff.StaffId, ...]] = {'read': (), 'later': ()}
    if item.owner:
        recipients['later-with-words'] = (item.owner,)
    if isinstance(source, review_items.ChangeSource):
        if item.state == ReviewState.VALIDATING:
            return {**recipients, 'question': source.validators}
        routed = source.assignment.owner
        return {**recipients, **{action: routed.showrunner_recipients for action in change_page.CHANGE_HANDOFFS},
                'works': change_page.verdict_recipients(routed, 'works'),
                'works-with-words': change_page.verdict_recipients(routed, 'works: words'),
                'not works': change_page.verdict_recipients(routed, 'not works')}
    if isinstance(source, review_items.QuestionSource):
        record = source.record
        owner = (record.owner,)
        recipients.update(question=owner, feedback=owner)
        if item.type == ReviewType.RESPONSE:
            recipients['read-with-words'] = owner
        if 'review_submission' in record.item:
            return {**recipients, 'answer': owner, 'followup': owner}
        try:
            scope = update_registration.question_scope(record.owner, members)
            relay = update_registration.question_relay(record, scope, update_registration.QuestionStaff.for_record(members))
        except (OSError, ValueError, ProductionRefusal, update_registration.RegistrationFailure):
            return recipients
        return {**recipients, 'answer': (change_browser.question_receiver(record.owner, 'answer', relay),),
                'followup': (change_browser.question_receiver(record.owner, 'followup', relay),)}
    recipients = {**{action: (item.owner,) for action in item.actions}, **recipients}
    if isinstance(source, review_items.InvestigationSource) and item.owner and not can_acknowledge_answer(item):
        recipients['read-with-words'] = (item.owner,)
    if isinstance(source, review_items.EfficiencySource):
        recipients['decline'] = ()
        if item.owner:
            recipients['decline-with-words'] = (item.owner,)
    return recipients


def review_toast_routes(item: ReviewItem, members: staff.StaffSnapshot) -> str:
    """Give the form the recipient IDs and their names for the toast."""
    recipients = review_action_recipients(item, members)
    labels: dict[str, str | None] = {}
    for action, route in recipients.items():
        if action == 'later':
            continue
        if action in ('works', 'works-with-words', 'not works'):
            route = tuple(reversed(route))
        named_handoff = isinstance(item.source, review_items.ChangeSource) and action in change_page.CHANGE_HANDOFFS
        labels[action] = change_browser.receiver_names(members, route) if route or named_handoff else None
    return (change_browser.send_route_attributes(labels)
            +f' data-review-recipients="{html.escape(json.dumps(recipients), quote=True)}"')


def details(item: ReviewItem, inputs: ReviewBodyInputs) -> str:
    destination = list_destination(item)
    names = inputs.names
    source = item.source
    if isinstance(source, review_items.ForYouSource):
        return for_you_details(item, source.row, inputs)
    owner = owner_name(item, inputs.members)
    validator = owner if item.state == ReviewState.VALIDATING and isinstance(source, review_items.ChangeSource) else ''
    handoff = ''
    exchange = ''
    response = item.response
    if response is not None:
        timestamp = ''
        if response.answered_at is not None:
            time = review_items.aware(response.answered_at, inputs.zone).astimezone(inputs.zone)
            timestamp = f'<time datetime="{time.isoformat()}">{time:%b %d at %H:%M %Z}</time>'
        body = (section('You asked', response.question)
                +'<section class="response-answer"><h3>Answer from '
                +names.html(item.owner, owner)+'</h3>'+timestamp+change_browser.answer_html(response.answer)+'</section>')
    elif isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        question = source.record.item
        ask = question['parts']['ask'] if 'parts' in question else change_browser.question_text(question)[0]
        # The lead shows the ask. Keep the shared body renderer from repeating it.
        body_question = question.copy()
        if 'parts' in question:
            body_question['text'] = ask
        content, choices = change_browser.question_parts(body_question)
        lead = change_page.review_text_html(ask, inline=True)
        body = '<section><h3>Question</h3>'+lead+content+choices+'</section>'
        if isinstance(source, review_items.UnownedQuestionSource):
            body += '<p>No showrunner owns this question.</p>'
    elif isinstance(source, review_items.ChangeSource):
        row = source.row
        check = row.get('How to check', '')
        proof = row.get('What you see', '') or row.get('Proof', '')
        if validator and not proof and ' You see: ' in check:
            check, _, proof = check.partition(' You see: ')
        body = section('How to check', check)
        if proof.strip() or (validator and inputs.assets.get('image:'+item.key)):
            body += section('Proof', proof)
        question = source.slots.question
        if validator:
            duration = (' · Validating for <time data-validating-age datetime="'
                        +source.handed_at.isoformat()+'"></time>') if source.handed_at is not None else ''
            shown_validator = names.html(item.owner, validator)
            handoff = '<p class="review-handoff">With '+shown_validator+duration+'</p>'
            if isinstance(question, (change_page.OpenQuestion, change_page.AnsweredQuestion)) and not isinstance(
                    question, (change_page.OpenFeedback, change_page.CompletedFeedback)):
                exchange = section('Question for '+shown_validator, question.text)
                if isinstance(question, change_page.AnsweredQuestion):
                    exchange += ('<section><h3>Answer from '+shown_validator+'</h3>'
                                 +change_browser.answer_html(question.answer)+'</section>')
        else:
            if isinstance(question, change_page.OpenQuestion) and not isinstance(question, change_page.OpenFeedback):
                body += section('Question', question.text)
            elif isinstance(question, change_page.AnsweredQuestion):
                body += answer_section(question.answer)
        if path := inputs.assets.get('image:'+item.key):
            alt = html.escape('Screenshot: '+item.title,quote=True)
            body = ('<div class="change-text">'+body+'</div><a data-image-zoom class="change-image" href="'
                    +html.escape(path,quote=True)+'" target="_blank" rel="noopener"><img src="'
                    +html.escape(path,quote=True)+'" alt="'+alt+'"></a>')
    elif isinstance(source, review_items.InvestigationSource):
        body = section('You asked', source.record.question)
        if isinstance(destination, investigation.ListDestination):
            body = f'<p><a href="{report_url(destination)}">Open List item {destination.number}</a></p>' + body
        if source.record.answer:
            body += answer_section(source.record.answer)
    elif isinstance(source, review_items.ProposalSource):
        body = ('<section><h3>Proposal</h3><div data-proposal-body>'
                +inputs.assets[item.key]+'</div></section>')
    elif isinstance(source, review_items.EfficiencySource):
        body = section('Proposal', item.title)
    else:
        body = ''
    footer = ('' if response is not None else '<p class="review-source">Owner: '+names.html(item.owner, owner)
              +' · Source: '+names.html(item.source_unit, review_items.name(item.source_unit, inputs.members))+'</p>')
    fallback = '<section data-title-fallback hidden><h3>Item</h3><p>'+html.escape(item.title)+'</p></section>'
    body += inputs.assets.get('question-delivery:'+item.key, '')
    body += inputs.assets.get('submission:'+item.key, '')
    log_id = html.escape('review-log-'+item.key.replace('/', '-'), quote=True)
    return ('<div class="list-panel review-panel '+item.type+'"><div>'+fallback+handoff+'<div data-review-body>'+body+'</div></div>'
            +'<div class="review-log-column">'+record_table.log_panel(log_id, log_html(item, inputs, owner))+exchange+'</div>'
            +form(item, inputs.browser, inputs.words_rows, record_revision(item, inputs.assets),
                  owner=owner, validator=validator, routes=review_toast_routes(item, inputs.members), names=names,
                  assignment=assign_menu(item, names) if 'approve' in item.actions else '')+footer+'</div>')


def owner_name(item: ReviewItem, members: staff.StaffSnapshot) -> str:
    return 'Owner unavailable' if isinstance(item.source, review_items.UnownedQuestionSource) else review_items.name(item.owner, members)


def render(root: Path, *, show: str = 'open') -> bytes:
    """Build a standalone page; the HTTP server uses its retained snapshot."""
    _ = change_page.console_folder(root)
    return build_snapshot(root).render(show=show)


def render_card(snapshot: ReviewSnapshot, entry: ReviewIndexEntry, body: str, *, waiting_why: str) -> bytes:
    """Serve one expanded Review card with its own document styles."""
    if entry['state'] == ReviewState.WAITING and waiting_why:
        entry = {**entry, 'waitingWhy': waiting_why}
    settings = json.dumps({'entry': entry, 'titleText': change_page.CODE_SPAN.sub(r"\1", entry['title']),
                           'zone': snapshot.zone.key, 'later': dict(snapshot.later),
                           'requestTimeoutSeconds': snapshot.request_timeout_seconds})
    content = (record_table.STYLE + record_table.DATE_TIME_STYLE + STYLE + CARD_VIEW_STYLE
               + f'<div data-review-card {snapshot.card_motion.attributes()}>'
               + '<div class="review-card-status" data-review-card-updates>'
               + '<span role="status" data-review-card-notice></span>'
               + '<button type="button" data-show-record-updates hidden>Show updates</button></div></div>'
               + '<template data-card-body>' + body + '</template>'
               + '<div data-card-settings hidden>' + html.escape(settings) + '</div>'
               + '<script>' + page_frame.ACTION_STATUS_SCRIPT + record_table.DATE_TIME_SCRIPT + page_frame.MOTION_SCRIPT
               + page_frame.CARD_SUBMIT_SCRIPT + record_table.DETAIL_SCRIPT + REVIEW_CARD_SCRIPT + CARD_VIEW_SCRIPT + '</script>')
    return change_page.document(content, 'Review card')


def render_shell(snapshot: ReviewSnapshot, index: str, started: float, read_at: float) -> bytes:
    """Build the controls and compact index without any card body."""
    show = 'open'
    entries = snapshot.entries
    owners = {entry['owner']:(entry['ownerName'], entry['ownerHtml']) for entry in entries}
    owners.update({entry['sourceOwner']:(entry['sourceName'], entry['sourceHtml'])
                   for entry in entries if entry['type'] in review_items.INVESTIGATION_TYPES})
    owner_chips = [record_table.Chip(f'staff:{owner}', markup or html.escape(name), owner_name=name)
                   for owner, (name, markup) in sorted(owners.items(), key=lambda pair: pair[1][0].casefold())]
    type_chips = [*(record_table.Chip(kind, icon(kind) + LABELS[kind]) for kind in TYPE_CHIPS),
                  record_table.Chip('investigations', 'Investigations and Bugs', hidden=True)]
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      +'<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    search = ('<div class="record-filter-words"><label class="list-search-label" for="list-search">Search</label>'
              '<input type="search" id="list-search" placeholder="Filter by id or words"></div>')
    content = (record_table.STYLE+record_table.DATE_TIME_STYLE+STYLE
        +f'<div data-review data-version="{snapshot.version}" data-show="{show}" data-refresh-seconds="{snapshot.refresh_seconds}" '
        +f'data-request-timeout-seconds="{snapshot.request_timeout_seconds}" '
        +f'data-delivery-window-seconds="{snapshot.delivery_window_seconds}" '
        +f'data-undo-enabled="{str(snapshot.undo_enabled).lower()}" '
        +snapshot.card_motion.attributes()+' '
        +f'data-title-limit="{snapshot.title_limit}" data-zone="{html.escape(snapshot.zone.key, quote=True)}" '
        +f'data-later="{html.escape(json.dumps(dict(snapshot.later)), quote=True)}" '
        +f'style="--list-log-max-height:{snapshot.log_max_height_rem}rem">'
        +page_frame.title('Review', 'review', status_companion=search)
        +'<div class="review-queue">'
        +record_table.chip_row('Type', type_chips, 'all')
        +'<template data-review-bug-icon>'+icon(ReviewType.BUG)+'</template>'
        +record_table.chip_row('Show', SHOW_CHIPS, show)
        +record_table.chip_row('Owner', owner_chips, 'all', encode_values=True)
        +'<div class="list-filters">'
        +'<button type="button" data-clear-linked-reviews aria-label="Clear linked reviews filter"'
        +' aria-pressed="true" hidden>Linked reviews <span aria-hidden="true">×</span></button>'
        +'</div><p data-missing-item role="status" hidden></p><p data-hidden-action-status role="status" hidden></p>'
        +'<p data-review-notice role="status" hidden></p><ul data-review-source-notices'+('' if snapshot.notices else ' hidden')+'>'
        +''.join('<li>'+html.escape(notice)+'</li>' for notice in snapshot.notices)+'</ul>'
        +'<ul data-for-you-notices hidden>'
        +''.join('<li>'+html.escape(notice)+'</li>' for notice in snapshot.action_notices)+'</ul>'
        +'<script type="application/json" data-review-index>'+index+'</script><script>'+CARD_REQUEST_SCRIPT+'</script>'
        +'<div class="list-table"><table aria-label="Review">'
        +CARD_COLUMNS+'<thead><tr>'+headers+'</tr></thead>'
        +'<tbody data-window-before></tbody><tbody data-window-after></tbody>'
        +'</table><p data-empty hidden>No items in this view.</p></div></div></div>'
        +record_table.render_inline_detail()
        +'<div class="review-drafts" hidden><ul id="review-draft-list" aria-label="Unsaved Review words" tabindex="0" hidden></ul>'
        +'<button type="button" data-review-draft-toggle aria-controls="review-draft-list" aria-expanded="false"></button></div>'
        +page_frame.console_dialog('review-bug-dialog', 'review-bug-title', 'List item',
                                   '<iframe title="List item"></iframe>', body_class='review-bug-content')
        +'<script>'+SCRIPT+'</script>')
    return page_frame.rendered(change_page.document(content, 'Review'), started, read_at)


def review_fields(body: str) -> dict[str, list[str]]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    required = {'row', 'token', 'outcome', 'feedback'}
    optional = {'later', 'revision', 'assignee', 'retry_recipients', 'failure_attempt', 'feedback_retry'}
    if not required <= fields.keys() <= required | optional or any(len(values) != 1 for values in fields.values()):
        raise question_record.AnswerRefused('use the answer form on this page')
    if 'feedback_retry' in fields and fields['feedback_retry'] != ['1']:
        raise question_record.AnswerRefused('use Retry to send the saved words')
    return fields


def repeats_response_followup(item: ReviewItem, outcome: str, words: str, status: change_page.DeliveryStatus) -> bool:
    """Keep the saved delivery when the form repeats the current follow-up."""
    source = item.source
    if outcome != 'question' or 'question' not in item.actions or not isinstance(source, review_items.QuestionSource):
        return False
    fields = source.record.item
    pending = fields.get('followup')
    if not fields.get('followups') or pending is None or pending['text'] != words.strip():
        return False
    if 'review_submission' not in fields:
        return False
    reply = update_registration.review_submission(source.record)
    path = source.record.path
    page = status.metadata_page(change_page.ReviewPage(path, path.parent, (path.stem,), ''))
    return (isinstance(page, change_page.UserQuestionPage) and page.saved.get('kind') == 'question'
            and new_item_dialog.matches_reply_reference(reply, page.saved.get('question_id', ''))
            and page.saved.get('question_text') == pending['text'])


def submit_response(item: ReviewItem, outcome: str, words: str, status: change_page.DeliveryStatus,
                    verify_revision: Callable[[], ReviewItem],
                    reserve_delivery: ReserveResponseDelivery = ResponseDeliveryReservation.NOT_TRACKED
                    ) -> ReviewActionSave:
    source = item.source
    if outcome not in item.actions or not isinstance(source, review_items.QuestionSource):
        raise question_record.AnswerRefused('Choose an action on this card.')
    scope = review_submissions.sent_question_scope(item.owner, staff.snapshot())
    if outcome == 'read' and not words.strip():
        with update_registration.outstanding_lock(update_registration.outstanding_path(scope)):
            _ = verify_revision()
            update_registration.review_sent_question(source.record, scope, 'read', '')
        return ReviewActionSave(change_page.NoMessageDelivery(), 'got it', words_saved=False)
    words = new_item_dialog.read_words(words, 'Your words')
    if outcome == 'read':
        def verify_feedback() -> ReviewItem:
            current = verify_revision()
            if callable(reserve_delivery):
                path = source.record.path
                row = path.stem
                page = change_page.ReviewPage(path, path.parent, (row,), '')
                intent = status.slots(page, row).verdict
                if not isinstance(intent, change_page.DeliveryIntent):
                    raise OSError('The Response has no saved feedback delivery intent.')
                reserve_delivery(path, intent.attempt)
            return current

        delivery, answer = review_actions.sent_question_action(item, source.record, scope, 'feedback', words, status,
                                                               verify_revision=verify_feedback)
        return ReviewActionSave(delivery, answer, words_saved=True)
    delivery, _ = review_actions.sent_question_action(item, source.record, scope, 'question', words, status,
                                                      verify_revision=verify_revision)
    return ReviewActionSave(delivery, 'follow-up sent', words_saved=True)


def submit_snapshot(root: Path, snapshot: ReviewSnapshot, fields: dict[str, list[str]],
                    status: change_page.DeliveryStatus, *,
                    reserve_response_delivery: ReserveResponseDelivery = ResponseDeliveryReservation.NOT_TRACKED
                    ) -> ReviewActionSave:
    key, token, outcome, words = (fields[field][0] for field in ('row', 'token', 'outcome', 'feedback'))
    if not secrets.compare_digest(token, change_browser.row_token(snapshot.browser, key)):
        raise question_record.AnswerRefused('row or page token does not match this page')
    revision = fields.get('revision', [''])[0]
    until = fields.get('later', [''])[0]
    retry = review_actions.retry_action_feedback(status, key, outcome, words, revision, until)
    if not isinstance(retry, review_actions.NoActionFeedbackReceipt):
        delivery, answer = retry
        return ReviewActionSave(delivery, answer, words_saved=True)
    if 'feedback_retry' in fields:
        raise question_record.AnswerRefused('No saved Review words were found.')
    retained = snapshot.records.get(key)
    if retained is None:
        raise review_items.ReviewRecordMissing('This item is no longer on the list.')

    checked_recipients = fields.get('retry_recipients')
    retry_recipients = cast(object, json.loads(checked_recipients[0])) if checked_recipients is not None else None
    assignee = fields.get('assignee', [''])[0]

    def validate_revision(current: ReviewItem, members: staff.StaffSnapshot) -> ReviewItem:
        if review_items.on_list(current):
            raise review_items.ReviewRecordChanged('Open the report on the List.')
        if current.state == ReviewState.DONE:
            raise review_items.ReviewRecordChanged('This item is already Done.')
        if checked_recipients is not None:
            routes = review_action_recipients(current, members)
            with_words = outcome+'-with-words'
            route = with_words if words.strip() and with_words in routes else outcome
            recipients = (staff.StaffId(assignee),) if outcome == 'approve' and assignee else routes.get(route)
            if recipients is None or list(recipients) != retry_recipients:
                raise review_items.ReviewRecordChanged(REVIEW_DRAFT_CHOICE_CHANGED)
        assets: dict[str, str] = {}
        try:
            retain_asset(current, assets)
        except FileNotFoundError:
            raise review_items.ReviewRecordMissing('This item is no longer on the list.') from None
        if revision != record_revision(current, assets) and not (
                outcome == 'feedback' and _matches_feedback_retry(current, assets, revision, token, words)):
            raise review_items.ReviewRecordChanged(REVIEW_REVISION_CHANGED)
        return current

    def validate() -> ReviewItem:
        members = staff.snapshot()
        return validate_revision(review_items.read_current(root, retained, members), members)

    if retained.type == ReviewType.FOR_YOU:
        members = staff.snapshot()
        item = review_items.read_current(root, retained, members)
        source = item.source
        if (outcome != 'done' or not isinstance(source, review_items.ForYouSource)
                or source.row.kind != 'action'):
            raise question_record.AnswerRefused('Choose an action on this card.')
        if source.row.status == 'done':
            return ReviewActionSave(change_page.NoMessageDelivery(), 'Done', words_saved=False)
        _ = validate_revision(item, members)
        store = ActionStore()
        try:
            _ = store.mark_done(key, evidence='Marked Done in Review.')
        except (OSError, ActionStoreError):
            try:
                completed = store.get(key).status == 'done'
            except (OSError, ActionStoreError):
                completed = False
            if not completed:
                raise
        return ReviewActionSave(change_page.NoMessageDelivery(), 'Done', words_saved=False)

    item = validate()
    if outcome == 'read' and can_acknowledge_answer(item):
        return acknowledge_answer(root, item, validate)
    if item.type == ReviewType.RESPONSE:
        return submit_response(item, outcome, words, status, validate, reserve_response_delivery)
    if waiting_change(item) or (item.state == ReviewState.VALIDATING and outcome != 'question'):
        raise change_page.ChangeHandoffRefused(change_page.WITH_SHOWRUNNER)
    if outcome == 'later':
        delivery, answer = review_actions.later_action(item, until, words, status,
                                                      verify_revision=validate, root=root, feedback_revision=revision)
        return ReviewActionSave(delivery, answer, words_saved=bool(words.strip()))
    if outcome not in item.actions:
        raise question_record.AnswerRefused('choose an action on this card')
    source = item.source
    if isinstance(source, review_items.ChangeSource):
        with (change_review.checked_review_write(source.path, root, validate) as current,
              change_page.file_change_actions(review_actions.file_change_work)):
            action = change_page.VALIDATION_QUESTION if current.state == ReviewState.VALIDATING else outcome
            delivery = change_browser.submit_review_change(current, snapshot.browser, action, words, status)
        if isinstance(delivery, change_page.MessageDelivery) and outcome not in ('validate', 'feedback', 'question'):
            label = outcome
            with review_items.after_store_write(item.key):
                change_review.record_review_log(delivery.page.review_list, delivery.row,
                                                label+(': '+words if words else ''), delivery.page.production)
        answer = {'validate':'asked the showrunner to check', 'question':'question saved',
                  'feedback':'feedback sent'}.get(outcome, outcome)
        return ReviewActionSave(delivery, answer, words_saved=bool(words.strip()))
    if isinstance(source, review_items.QuestionSource):
        if not words.strip():
            raise question_record.AnswerRefused('enter an answer')
        members = staff.snapshot()
        if 'review_submission' in source.record.item:
            scope = review_submissions.sent_question_scope(item.owner, members)
            delivery, answer = review_actions.sent_question_action(
                item, source.record, scope, cast(change_page.QuestionOutcome, outcome), words, status,
                verify_revision=validate)
        else:
            scope = update_registration.question_scope(item.owner, members)
            question = change_browser.WaitingQuestion(key, review_items.name(item.owner, members), source.record,
                update_registration.question_source(scope), item.owner, review_items.name(source.record.asker, members))
            delivery, answer = change_browser.submit_question(
                question, cast(change_page.QuestionOutcome, outcome), words, status,
                save=change_browser.ExactQuestionSave(validate))
    elif isinstance(source, review_items.InvestigationSource):
        delivery, answer = review_actions.investigation_action(
            item, outcome, words, status, verify_revision=validate, feedback_revision=revision)
    else:
        delivery, answer = review_actions.decision_action(
            item, outcome, words, status, assignee=assignee, verify_revision=validate, feedback_revision=revision)
    if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery) and item.type == ReviewType.QUESTION:
        with review_items.after_store_write(item.key):
            delivery = review_actions.track_question(delivery, key, words, status)
        answer = 'question sent'
    return ReviewActionSave(delivery, 'feedback sent' if outcome == 'feedback' else answer,
                            words_saved=bool(words.strip()))


def can_acknowledge_answer(item: ReviewItem) -> bool:
    source = item.source
    if isinstance(source, review_items.ChangeSource) and not isinstance(source.assignment.production, Path):
        return False
    return item.unread_answer and 'read' not in item.actions


def acknowledge_answer(root: Path, item: ReviewItem, verify_revision: Callable[[], object]
                       ) -> ReviewActionSave:
    review_actions.record_action(item, item.answer_read_event, root=root, check=verify_revision)
    return ReviewActionSave(change_page.NoMessageDelivery(), 'marked read', words_saved=False)


def submit(root: Path, body: str, status: change_page.DeliveryStatus, *,
           snapshot: ReviewSnapshot | None = None) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    fields = review_fields(body)
    if snapshot is not None:
        saved = submit_snapshot(root, snapshot, fields, status)
        return saved.delivery, saved.answer
    browser = change_page.ReviewBrowser(*change_browser.browser_manifest(change_page.console_folder(root)))
    key = fields['row'][0]
    if not secrets.compare_digest(fields['token'][0],change_browser.row_token(browser,key)):
        raise question_record.AnswerRefused('row or page token does not match this page')
    outcome, words = fields['outcome'][0], fields['feedback'][0]
    until = fields.get('later', [''])[0]
    retry = review_actions.retry_action_feedback(status, key, outcome, words, '', until)
    if not isinstance(retry, review_actions.NoActionFeedbackReceipt):
        return retry
    if 'feedback_retry' in fields:
        raise question_record.AnswerRefused('No saved Review words were found.')
    if key.startswith('question/'):
        question_item = next((item for item in review_items.read(root, kinds=review_items.QUESTION_TYPES)
                              if item.key == key), None)
        if question_item is not None and repeats_response_followup(question_item, outcome, words, status):
            raise review_items.ReviewRecordChanged(REVIEW_REVISION_CHANGED)
        if (question_item is not None and question_item.type == ReviewType.RESPONSE
                and not (outcome == 'read' and can_acknowledge_answer(question_item))):
            def validate_response() -> ReviewItem:
                current = review_items.read_current(root, question_item, staff.snapshot())
                if current != question_item:
                    raise review_items.ReviewRecordChanged(REVIEW_REVISION_CHANGED)
                return current

            saved = submit_response(question_item, outcome, words, status, validate_response)
            return saved.delivery, saved.answer
    else:
        question_item = None
    if outcome == 'read':
        item = question_item or next((item for item in review_items.read(root) if item.key == key), None)
        if item is not None and can_acknowledge_answer(item):
            def verify_answer() -> None:
                if review_items.read_current(root, item, staff.snapshot()) != item:
                    raise review_items.ReviewRecordChanged(REVIEW_REVISION_CHANGED)

            saved = acknowledge_answer(root, item, verify_answer)
            return saved.delivery, saved.answer
    if outcome == 'later' or key.startswith(('investigation/', 'proposal/', 'efficiency/')):
        item = next((item for item in review_items.read(root) if item.key == key), None)
        if item is None:
            raise question_record.AnswerRefused('this item is not on the page')
        if outcome == 'later':
            if isinstance(item.source, review_items.ChangeSource):
                source = item.source
                with status.locked():
                    path = change_routing.delivery_path(root, source.path, source.row['#'])
                    change_page.require_user_turn(change_routing.delivery_slots(path))
                    return review_actions.later_action(item,until,words,status)
            return review_actions.later_action(item,until,words,status)
        if outcome not in item.actions:
            raise question_record.AnswerRefused('choose an action on this card')
        delivery, answer = (review_actions.investigation_action(item,outcome,words,status)
                            if isinstance(item.source, review_items.InvestigationSource) else
                            review_actions.decision_action(item,outcome,words,status,assignee=fields.get('assignee', [''])[0]))
        if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery):
            delivery = review_actions.track_question(delivery, key, words, status)
        return delivery, answer
    _ = fields.pop('later',None)
    _ = fields.pop('revision',None)
    _ = fields.pop('assignee',None)
    item = question_item
    if item is not None and item.state == ReviewState.WAITING:
        if outcome not in item.actions:
            raise question_record.AnswerRefused('choose an action on this card')
        delivery, answer = review_actions.question_action(item, cast(change_page.QuestionOutcome, outcome), words, status)
    else:
        delivery, answer = change_browser.submit(browser, urlencode({key:values[0] for key,values in fields.items()}), status)
    if key.startswith('question/') and outcome == 'question' and isinstance(delivery, change_page.MessageDelivery):
        delivery = review_actions.track_question(delivery, key, words, status)
    if isinstance(delivery, change_page.MessageDelivery) and not isinstance(delivery.page, change_page.UserQuestionPage):
        if outcome not in ('validate', 'feedback', 'question'):
            words = fields['feedback'][0]
            log = outcome + (': ' + words if words else '')
            change_review.record_review_log(delivery.page.review_list, delivery.row, log, delivery.page.production)
    if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery) and not isinstance(
            delivery.page, (change_page.UserQuestionPage, change_page.ReviewMessagePage)):
        return delivery, 'question saved'
    return delivery, ('feedback sent' if outcome == 'feedback' else answer or
                     {'validate':'asked the showrunner to check', 'question':'question sent'}.get(outcome,'answer'))


QUESTION_COLOR_RULE = '.question{--type-color:#b99aff}'
STYLE = '<style>' + QUESTION_COLOR_RULE + SUBMISSION_EVIDENCE_STYLE + '''
main:has(>[data-review]){max-width:1124px}
[data-review]{display:flex;flex-wrap:wrap;align-items:center}
[data-review]>:is(.page-title,.page-heading,.review-queue){flex:0 0 100%;min-width:0}
[data-review]>.page-top{margin-bottom:4px}
[data-review]>.page-top>.page-status{margin:0;min-width:0}
[data-review]>.page-top>.record-filter-words{margin:0}
.review-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.review-waiting-why{display:block;color:var(--ink-3);font-size:.85em}
[data-review] .record-filter-row{margin-bottom:12px}
[data-review] .list-filters:has(>[data-clear-linked-reviews][hidden]){display:none}
[data-review] .record-filter-row>div{gap:8px}
[data-clear-linked-reviews]{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;min-height:36px;
background:var(--card);border:1px solid var(--line);border-radius:999px;color:var(--ink-2);padding:5px 14px;margin:0}
[data-clear-linked-reviews][aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.review-icon{display:inline-flex;vertical-align:middle;margin-right:5px}.review-icon svg{width:18px;height:18px}
.change{--type-color:#62b7ef}.investigation{--type-color:#30c1ad}
.proposal{--type-color:#f27ab8}.efficiency{--type-color:#a3d63a}.bug{--type-color:#ed876b}.response,.for-you{--type-color:#edc46d}
.review-panel.for-you dd{overflow-wrap:anywhere}
.rebuild-why{padding:0;margin:0;list-style:none}
.rebuild-why li+li{margin-top:12px}
.rebuild-why li>span{white-space:pre-wrap;overflow-wrap:anywhere}
.rebuild-why small{display:block;margin-top:4px;font-size:12px;overflow-wrap:anywhere}
.rebuild-why small,.rebuild-secondary{color:var(--ink-3)}
.review-icon{color:var(--type-color)}.review-panel{border-left-color:var(--type-color);grid-template-columns:1.25fr 1fr;padding:0 20px 18px 58px;gap:4px 28px}
.response-answer time{display:block;margin-bottom:8px;color:var(--ink-3);font-size:12px}
.review-log-column{display:flex;flex-direction:column;min-width:0}.review-log-column>.list-log-panel{flex:1 0 0}
.review-log-column>.list-log-panel:not(:last-child){min-height:8rem}
.review-panel [data-review-form]{grid-column:1/-1;border-top:1px solid var(--line);padding-top:12px;margin-top:10px}
[data-review-form] label{display:block;font-weight:600;margin-bottom:6px}
[data-review-form] textarea,[data-review-draft],.review-drafts textarea{display:block;box-sizing:border-box;width:100%;font:inherit;padding:9px 12px;background:var(--bg);
color:var(--ink);border:1px solid var(--line);border-radius:8px;resize:vertical}
.review-drafts{--draft-gutter:24px;--draft-bottom:max(24px,calc(var(--send-undo-space,0px) + 8px));
position:fixed;z-index:6;right:var(--draft-gutter);bottom:var(--draft-bottom);
display:flex;flex-direction:column;align-items:flex-end;gap:8px;max-width:calc(100vw - 2 * var(--draft-gutter));
max-height:max(0px,calc(100dvh - var(--draft-bottom) - 8px));overflow:hidden}
@media(max-width:560px){.review-drafts{--draft-gutter:16px}}
.review-drafts [hidden],.review-drafts[hidden]{display:none}
.review-drafts button,.review-drafts ul{background:var(--card);color:var(--ink);border:1px solid var(--line);
border-radius:8px;font:inherit}
.review-drafts button{flex:none;min-height:36px;margin:0;padding:5px 12px;font-weight:600}
.review-drafts button:not(:disabled):hover,.review-drafts [aria-expanded=true]{border-color:var(--accent)}
.review-drafts ul{box-sizing:border-box;min-height:0;width:min(26rem,calc(100vw - 2 * var(--draft-gutter)));overflow:auto;overscroll-behavior:contain;
list-style:none;padding:0;margin:0}
.review-drafts li{padding:12px 16px;overflow-wrap:anywhere}
.review-drafts li+li{border-top:1px solid var(--line)}
.review-drafts p{margin:4px 0 10px}
.review-drafts p a{color:inherit;white-space:nowrap}
#review-bug-dialog{width:min(1060px,calc(100vw - 32px))}
#review-bug-dialog .review-bug-content{display:flex;padding:0}
#review-bug-dialog iframe{width:100%;height:72dvh;border:0}
.review-drafts label{display:block;font-weight:600}
.review-drafts textarea{margin:4px 0 8px;resize:none;font-weight:400}
.review-actions{margin-top:12px}
.review-later{margin-left:auto}
.list-table small[data-new-answer]{color:var(--accent)}
.list-log span{white-space:pre-wrap}
.review-actions button{margin:0;font:inherit;min-height:40px;padding:6px 14px;border:1px solid var(--ink-3);border-radius:8px;
background:var(--card);color:var(--ink)}.review-actions .primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.review-source{grid-column:1/-1;color:var(--ink-3);font-size:12px;margin-top:16px!important}
.list-table .list-row td:last-child{text-align:right}.list-table [data-detail]>td{text-align:left}
.review-handoff{display:block;margin-top:4px;color:var(--ink-2)}
.review-handoff>:is(span,time){white-space:nowrap}
.review-handoff>.staff-name{display:inline-block;min-width:0;max-width:100%;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom}
@media(min-width:961px){.review-handoff{display:flex;align-items:baseline;gap:.25em}}
@media(max-width:960px){[data-review] .list-row td:last-child::before{content:'Age: '}
.review-panel{grid-template-columns:1fr;padding:0 14px 16px}.review-log-column{display:block}}
@media(width>800px){
 [data-review]>.page-top{display:grid;grid-template-columns:minmax(0,1fr) 300px;align-items:center;column-gap:16px}
 [data-review]>.page-top>:is(.page-title,.page-heading,[data-page-actions]){grid-column:1/-1}
}
@media(max-width:800px){
 [data-review]>.page-top>.record-filter-words{width:100%;margin-top:8px}
}
@media(max-width:640px){
 [data-review]>.page-top>.record-filter-words{flex:0 0 100%}
 .review-queue{margin-top:12px}
}
@media(max-width:480px){[data-review] .record-filter-row{grid-template-columns:minmax(0,1fr)}}
</style>'''

CARD_VIEW_STYLE = '''<style>
:root:has([data-review-card]){overflow:hidden;scrollbar-gutter:auto}
:root[data-send-undo-space]:has([data-review-card]){padding-bottom:0;scroll-padding-bottom:auto}
:root:not(.layout-measuring) main:has(>[data-review-card]){max-width:none;padding:0}
.review-box{position:relative;box-sizing:border-box;display:flex;flex-direction:column;max-height:calc(100dvh - var(--send-undo-space,0px));padding:24px;background:var(--card)}
.review-box-header{flex:none}
.review-box-header h1{margin:0 0 8px;padding-right:84px;font-size:24px;white-space:pre-line;overflow-wrap:anywhere}
.review-box-meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;color:var(--ink-3);font-size:13px}
.review-box-scroll{min-height:var(--review-body-min,0px);overflow:auto;scrollbar-gutter:stable}
:is(.review-box,.review-box-scroll){scrollbar-width:thin;
scrollbar-color:color-mix(in srgb,var(--ink-3) 45%,var(--card)) transparent}
.review-box[data-whole-scroll]{overflow:auto}
.review-box[data-whole-scroll] .review-box-scroll{flex:none;overflow:visible}
.review-box .review-panel{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
column-gap:24px;row-gap:0;border:0;padding:0}
.review-box .review-panel>div{min-width:0}
.review-box :is([data-title-fallback],[data-empty-review-section],.list-log-panel:has(.list-log:empty)){display:none}
/* The box scrolls as one area, so its log takes its full height. */
.review-box .review-log-column>.list-log-panel{flex:none;contain:none;min-height:0}.review-box .list-log-scroll{max-height:none;overflow:visible}
.review-box .review-panel:not(:has(.review-handoff,[data-review-body]>:not([data-empty-review-section])))
>div:nth-child(2){grid-column:1/-1}
.review-box .review-source{margin:12px 0 0!important}
.review-box-footer{position:relative;flex:none;margin-top:16px;padding-top:12px;border-top:1px solid var(--line)}
.review-box-footer form{margin:0}
.review-box-footer textarea{margin:0;max-height:9rem}
.review-box .later-menu{position:static}
.review-box [data-later-menu]{bottom:calc(100% + 6px)}
@media(max-width:799px){.review-box .review-panel{grid-template-columns:minmax(0,1fr)}}
@media(max-width:560px){
.review-box .review-actions{flex-direction:row;flex-wrap:wrap;gap:8px}
.review-box .review-action-group>div>button{font-size:14px;min-height:36px;padding-inline:10px}
.review-box-footer textarea{max-height:4rem}
}
.review-box [data-review-card-updates]{padding-inline:0}
[data-review-card-notice]{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style>'''

REVIEW_SAVE_REPORTS_SCRIPT = r'''
const reviewSaveFailurePath=''' + json.dumps(SAVE_FAILURE_PATH) + r''';
const reviewDeliveryRetryPath=''' + json.dumps(DELIVERY_RETRY_PATH) + r''';
const reviewWordsUnsent='Saved. Your words are still unsent.';
const reviewReplyDeliveryFailure=result=>typeof result?.send_failure==='string' ? result.send_failure : '';
const reviewReceiptSavedWords=result=>result.outcome==='saved' && result.words_saved===true && !reviewReplyDeliveryFailure(result);
function rememberReviewDeliveryRetry(draft,body){
 const attempt=body.get('failure_attempt');
 if(draft.deliveryRetry?.get('failure_attempt')===attempt) return;
 draft.deliveryRetry=new URLSearchParams({row:body.get('row'),token:body.get('token'),failure_attempt:attempt});
}
function createReviewSaveReports(drafts,requestSignal,changed){
 const pending=drafts.pendingSaveReports ??= new Map();
 let active=false,retry=false;
 function presentOutcome(key,draft,result){
  if(reviewReceiptSavedWords(result)){
   draft.notice?.remove();
   drafts.delete(key);
  }else{
   const message=draft.notice?.querySelector('p');
   if(message){message.textContent=draft.message;message.hidden=!draft.message;}
  }
  changed(key,draft,result);
 }
 async function retryDelivery(key,draft){
  const body=draft.deliveryRetry,edit=draft.edit,attempt=body.get('failure_attempt');
  pending.delete(attempt);
  let result;
  try{
   const response=await fetch(reviewDeliveryRetryPath,{method:'POST',body,signal:requestSignal()});
   result=await response.json();
   if(!response.ok || (result?.outcome!=='pending' && (result?.outcome!=='saved' || typeof result.words_saved!=='boolean')))
    throw new Error(result?.reason || 'The saved delivery could not retry.');
  }catch(error){result={send_failure:error.message};}
  if(drafts.get(key)!==draft || draft.edit!==edit || draft.deliveryRetry!==body) return;
  const failure=reviewReplyDeliveryFailure(result);
  if(failure){
   draft.message=failure;
   delete draft.filedBug;
  }
  presentOutcome(key,draft,result);
  if(result.outcome==='pending' || failure){
   const report=new URLSearchParams(body);
   report.set('feedback',edit.draft.feedback);
   report.set('reason',failure || 'The sender has not confirmed delivery.');
   draft.failureAttempt=attempt;
   pending.set(attempt,{body:report,edit});
   void flush();
  }
 }
 async function flush(){
  if(!pending.size) return;
  if(active){retry=true;return;}
  active=true;
  retry=false;
  try{
   for(const [attempt,report] of pending){
    const response=await fetch(reviewSaveFailurePath,{method:'POST',body:report.body,signal:requestSignal()});
    if(response.status>=500) break;
    if(!response.ok) continue;
    const result=await response.json();
    if(pending.get(attempt)!==report || !['saved','refused','missing','undone'].includes(result?.outcome)) continue;
    const bug=result.bug;
    const filedBug=typeof bug?.label==='string' && typeof bug.href==='string' ? bug : null;
    const needsBug=Boolean(reviewReplyDeliveryFailure(result)) && !filedBug;
    if(!needsBug) pending.delete(attempt);
    const key=report.body.get('row'),draft=drafts.get(key);
    if(draft?.failureAttempt!==attempt || draft.edit!==report.edit) continue;
    if(!reviewReceiptSavedWords(result)){
     draft.filedBug=filedBug;
     if(report.outcomeUnknown && typeof result.reason==='string') draft.message='Not saved: '+result.reason;
     if(result.outcome==='undone') draft.message='This send was undone.';
     if(result.outcome==='saved'){
      const failure=reviewReplyDeliveryFailure(result);
      draft.message=failure || reviewWordsUnsent;
      if(!failure) delete draft.retry;
      if(result.retry_delivery===true) rememberReviewDeliveryRetry(draft,report.body);
      if(!needsBug) delete draft.failureAttempt;
     }
    }
    presentOutcome(key,draft,result);
   }
  }catch{}finally{
   active=false;
   if(retry) void flush();
  }
 }
 return {flush,retryDelivery,get active(){return active;},add(attempt,report){pending.set(attempt,report);void flush();}};
}
'''

REVIEW_CARD_SCRIPT = REVIEW_SAVE_REPORTS_SCRIPT + r'''
const escapeText=value=>String(value).replace(/[&<>"']/g,letter=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[letter]));
const colored=(name,markup)=>markup || escapeText(name);
const entrySortValues=entry=>[entry.id,entry.type,entry.ownerName,entry.title,entry.state,entry.filed];
const detailId=key=>'review-detail-'+encodeURIComponent(key);
function reviewAge(since,full=false){
 const minutes=Math.max(0,Math.floor((Date.now()-since)/60000));
 if(minutes>=1440) return Math.floor(minutes/1440)+' d';
 if(minutes>=60) return Math.floor(minutes/60)+' h'+(full && minutes%60 ? ' '+minutes%60+' m' : '');
 return minutes+' min';
}
function updateHandoffAges(root=document){
 for(const stamp of root.querySelectorAll('[data-validating-age]')){
  const label=reviewAge(Date.parse(stamp.dateTime),true);
  if(stamp.textContent!==label) stamp.textContent=label;
 }
}
const reviewUrgency=entry=>({user:'Important',blocks:'Blocks work',soon:'Due soon',later:'Later'})[entry.urgency] || '';
const newAnswerMarkup=''' + json.dumps(question_path.NEW_ANSWER_HTML) + r''';
const reviewWaiting=entry=>entry.state==='Waiting' && entry.waitingWhy
 ? '<span class="review-waiting-why">'+escapeText(entry.waitingWhy)+'</span>' : '';
function reviewCardSummary(entry,titleLimit,icon){
 const node=document.createElement('tbody'),panel=detailId(entry.key);
 Object.assign(node.dataset,{listItem:'',key:entry.key,cardId:entry.id,number:entry.id.toLowerCase(),title:entry.title.toLowerCase(),
  owner:entry.owner,sourceOwner:entry.sourceOwner,sourceName:entry.sourceName,state:entry.state,type:entry.type,
  order:entry.order,updated:entry.updated,unreadAnswer:entry.unreadAnswer,parked:'false',
  sortValues:JSON.stringify(entrySortValues(entry))});
 const first=entry.title.split(/\r?\n/)[0],short=first.slice(0,titleLimit);
 const stamp=entry.filed ? reviewAge(entry.filed*1000) : '';
 const typeLabel=entry.type==='for-you' ? 'For you' : entry.type[0].toUpperCase()+entry.type.slice(1);
 const stateLabel=entry.actionStatus==='withdrawn' ? 'Withdrawn' : entry.state;
 const validating=entry.state==='Validating' && entry.type==='change';
 const handoff=entry.handoff?.[0];
 const age=handoff ? ' · Validating for <time data-validating-age datetime="'+escapeText(handoff)+'">'
  +reviewAge(Date.parse(handoff),true)+'</time>' : '';
 const handoffLine=validating && entry.owner ? '<span class="review-handoff">With '+colored(entry.ownerName,entry.ownerHtml)+age+'</span>' : '';
 const urgency=reviewUrgency(entry);
 const cells=['<button type="button" class="list-toggle" aria-expanded="false" aria-controls="'+panel+'">'
  +'<span class="list-chevron" aria-hidden="true">›</span>'+escapeText(entry.id)+'</button>',
  icon+typeLabel,colored(entry.ownerName,entry.ownerHtml)
  +(entry.sourceName!==entry.ownerName ? '<small>· '+colored(entry.sourceName,entry.sourceHtml)+'</small>' : ''),
  '<strong class="review-title" title="'+escapeText(entry.title)+'">'+escapeText(short===entry.title ? short : short+'…')+'</strong>'+handoffLine+reviewWaiting(entry)+(entry.unreadAnswer ? newAnswerMarkup : '')+(urgency ? '<small>'+escapeText(urgency)+'</small>' : ''),
  '<span class="list-state '+(entry.state==='Open' ? 'warning' : 'muted')+'">'+stateLabel+'</span>',stamp];
 node.innerHTML='<tr class="list-row">'+cells.map((cell,at)=>'<td'+(at===0 ? ' class="record-id"' : '')+' data-label="'+['Id','Type','Owner','Item','State','Age'][at]+'">'+cell+'</td>').join('')
  +'</tr><tr data-detail id="'+panel+'" hidden><td colspan="6"><p data-record-loading role="status">Loading the item…</p></td></tr>';
 return node;
}
const plainReviewText=text=>text.replace(/\s+/g,' ').trim();
function reviewBodyText(element,omitHeading=false){
 const body=element.cloneNode(true);
 if(omitHeading) body.querySelector(':scope > h3')?.remove();
 body.querySelectorAll('[data-title-fallback],[data-title-ignore],.list-log,form,button,input,select,textarea,label,script,style')
  .forEach(node=>node.remove());
 const blocks=new Set('ADDRESS ARTICLE ASIDE BLOCKQUOTE BR DD DETAILS DIALOG DIV DL DT FIELDSET FIGCAPTION FIGURE FOOTER H1 H2 H3 H4 H5 H6 HEADER HGROUP HR LI MAIN MENU NAV OL P PRE SECTION SUMMARY TABLE TBODY TD TFOOT TH THEAD TR UL'.split(' '));
 const bodyText=node=>{
  if(node.nodeType===Node.TEXT_NODE) return node.textContent;
  const text=[...node.childNodes].map(bodyText).join('');
  return blocks.has(node.nodeName) ? ' '+text+' ' : text;
 };
 return plainReviewText(bodyText(body));
}
function updateTitleFallback(panel){
 const fallback=panel.querySelector('[data-title-fallback]');
 if(!fallback) return;
 fallback.hidden=reviewBodyText(panel.querySelector('[data-review-body]'))
  .includes(plainReviewText(fallback.querySelector('p').textContent));
}
function installReviewBody(row,body){
 row.dataset.bodyLoaded='true';
 const detail=recordDetails(row);
 if(typeof body==='string') detail.innerHTML=body;
 else detail.replaceChildren(body);
 const panel=detail.querySelector('.review-panel');
 if(panel) updateTitleFallback(panel);
 updateHandoffAges(detail);
}
const reviewDrafts=window.frameElement?.linkedReviewDrafts ?? new Map();
const reviewFormEdits=new WeakMap();
let presentReviewDraftCopy=update=>update(),reviewDraftsChanged=()=>{};
let presentReviewSaveOutcome=()=>{};
// Match Python str.strip() when the server checks for words.
const blankReviewWords=/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;
function reviewActionData(form,outcome,until){
 const data=new URLSearchParams(new FormData(form));data.set('outcome',outcome);
 if(outcome==='later') data.set('later',until);
 if(outcome!=='approve') data.delete('assignee');
 return data;
}
function reviewActionLabel(button){
 return (button.querySelector('span') ?? button).textContent.trim();
}
function bindReviewForm(form,{zone,settings,send}){
 const remember=event=>rememberReviewDraft(form,event?.detail?.edited!==false);
 const words=form.elements.feedback,later=form.elements.later,assignee=form.elements.assignee;
 const row=recordFromDetail(form);
 const stored=reviewDrafts.get(row.dataset.key);
 const showFeedbackRetry=data=>{
  let button=form.querySelector('[data-review-feedback-retry]');
  if(!button){
   button=document.createElement('button');
   button.type='button';
   button.dataset.reviewFeedbackRetry='';
   button.textContent='Retry';
   form.append(button);
  }
  const retry=new URLSearchParams(data);
  retry.set('feedback_retry','1');
  button.onclick=()=>void send(retry.get('outcome'),retry,'Retry');
 };
 form.showFeedbackRetry=showFeedbackRetry;
 if(stored?.feedbackRetry) showFeedbackRetry(stored.feedbackRetry);
 if(stored) restoreReviewDraft(form,stored.edit.draft);
 reviewFormEdits.set(form,stored?.edit);
 const choices=recordDetails(row).querySelectorAll('input[name=choice]');
 const submit=(outcome,until,button)=>send(outcome,reviewActionData(form,outcome,until),reviewActionLabel(button));
 choices.forEach(choice=>choice.addEventListener('change',()=>{words.value=choice.value;remember();words.focus();}));
 [words,later,assignee].filter(Boolean).forEach(field=>{
  field.addEventListener('input',remember);field.addEventListener('change',remember);
 });
 form.addEventListener('submit',event=>{
  event.preventDefault();
  const button=event.submitter,outcome=button.value;
  if(outcome==='later' || button.dataset.laterPreset){
   const until=parkRequestTime(form,button,zone(),settings(),later.value);
   if(until) void submit('later',until,button);
   return;
  }
  void submit(outcome,undefined,button);
 });
 if(later) bindDateTimePicker(form,{zone,settings,summaryPrefix:'Hides until '});
 return send;
}
function readReviewDraft(form){
 if(!form?.elements.feedback) return null;
 const choices=[...recordDetails(recordFromDetail(form)).querySelectorAll('input[name=choice]')];
 const assignee=form.elements.assignee?.selectedOptions[0];
 const feedback=form.elements.feedback.value;
 return {feedback:blankReviewWords.test(feedback) ? '' : feedback,later:form.elements.later?.value ?? '',
  assignee:assignee?.value ?? '',assigneeName:assignee?.dataset.assigneeName ?? assignee?.textContent ?? '',
  choice:choices.find(choice=>choice.checked)?.value || ''};
}
function rememberReviewDraft(form,edited=false){
 const draft=readReviewDraft(form);
 if(!draft) return;
 const row=recordFromDetail(form),{key,cardId}=row.dataset;
 const stored=reviewDrafts.get(key);
 const previous=stored?.edit;
 if(form.elements.feedback.type!=='textarea' || (!edited && reviewFormEdits.get(form)!==previous))
  return previous;
 // A row can lose its selected radio button during a fade.
 draft.choice=draft.choice || previous?.draft.choice || '';
 if(!edited && previous && Object.keys(draft).every(field=>draft[field]===previous.draft[field])) return previous;
 if(!previous && !draft.feedback && !draft.later && !draft.choice && !draft.assignee) return;
 const edit={draft};
 const record=stored ?? {cardId:cardId || key,title:row.querySelector('.review-title')?.title,message:'',notice:null};
 if(previous?.draft.feedback!==draft.feedback){
  record.message='';
  delete record.retry;
  delete record.deliveryRetry;
  delete record.failureAttempt;
  delete record.filedBug;
 }
 record.edit=edit;
 reviewDrafts.set(key,record);
 reviewFormEdits.set(form,edit);
 drawReviewDraftNotice(record);
 reviewDraftsChanged();
 return edit;
}
function clearReviewDraft(key){
 hideReviewDraftNotice(key);
 reviewDrafts.delete(key);
 reviewDraftsChanged();
}
function confirmReviewDraft(key,submitted,words,form){
 const stored=reviewDrafts.get(key);
 if(stored && (stored.edit!==submitted || stored.edit.draft.feedback!==words)){
  if(stored.edit.draft.feedback) setReviewDraftMessage(key,stored.edit===submitted
   ? reviewWordsUnsent : 'Saved. Your newer words are unsent.');
  return;
 }
 clearReviewDraft(key);
 if(form?.elements.feedback){
  form.elements.feedback.value='';
  reviewFormEdits.set(form,null);
 }
}
function reviewDraftWords(key){return reviewDrafts.get(key)?.edit.draft.feedback || '';}
function restoreReviewDraft(form,draft){
 if(form.elements.feedback.type==='textarea') form.elements.feedback.value=draft.feedback;
 if(form.elements.later) form.elements.later.value=draft.later;
 const assignee=form.elements.assignee;
 if(assignee){
  if(draft.assignee && ![...assignee.options].some(option=>option.value===draft.assignee)){
   const unavailable=new Option((draft.assigneeName || draft.assignee)+' — not available',draft.assignee);
   unavailable.dataset.assigneeName=draft.assigneeName || '';
   assignee.add(unavailable);
  }
  assignee.value=draft.assignee;
 }
 recordDetails(recordFromDetail(form)).querySelectorAll('input[name=choice]').forEach(choice=>choice.checked=choice.value===draft.choice);
}
function drawReviewDraftNotice(record,anchor){
 const copy=record?.notice;
 if(!copy) return;
 showActionFailure(copy.querySelector('p'),reviewDraftMessage(record));
 presentReviewDraftCopy(()=>{
  if(record.notice!==copy) return;
  const words=record.edit.draft.feedback;
  copy.hidden=!words;
  const field=copy.querySelector('textarea');
  if(field.value!==words) field.value=words;
  if(anchor?.isConnected && copy.previousElementSibling!==anchor) anchor.after(copy);
 });
}
function reviewDraftMessage(record){return record.message || 'Your words are unsent.';}
function setReviewDraftMessage(key,message){
 const record=reviewDrafts.get(key);
 if(!record) return;
 record.message=message;
 drawReviewDraftNotice(record);
 reviewDraftsChanged();
}
function hideReviewDraftNotice(key){
 const record=reviewDrafts.get(key);
 if(!record?.notice) return;
 const copy=record.notice;
 record.notice=null;
 presentReviewDraftCopy(()=>copy.remove());
}
function showReviewDraftNotice(key,anchor){
 if(!reviewDraftWords(key)) return;
 const record=reviewDrafts.get(key),doc=anchor.ownerDocument;
 if(!record.notice){
  const copy=doc.createElement('section');
  copy.dataset.unsavedReviewKey=key;
  const status=doc.createElement('p');
  status.className='action-failure';
  status.setAttribute('role','status');
  const label=doc.createElement('label'),words=doc.createElement('textarea');
  label.textContent='Your words';
  words.readOnly=true;words.dataset.reviewDraft='';
  label.append(words);copy.append(status,label);
  record.notice=copy;
 }
 drawReviewDraftNotice(record,anchor);
}
async function recoverReviewRevision(reason,{form,notice,reload,current=()=>true,present=apply=>apply(),report,missing}){
 const revision=form.elements.revision?.value,key=recordFromDetail(form).dataset.key;
 const message='Not saved: '+reason;
 const notify=text=>{
  if(report) report(text);
  else{
   setReviewDraftMessage(key,text);
   showActionFailure(notice,text);
  }
 };
 notify(message);
 const refreshError=error=>{
  if(current()) notify(message+' The card could not refresh. '+error.message);
 };
 try{
  const next=await reload();
  if(!current()) return;
  if(revision && next.revision===revision) throw new Error('The current revision is not available yet.');
  const apply=async()=>{
   const row=await next.show();
   if(!current()) return;
   const fresh=recordDetails(row)?.querySelector('[data-review-form]')
    ?? row?.querySelector('.review-box-footer [data-review-form]');
   if(fresh?.elements.feedback){
    if(!report) showActionFailure(fresh.querySelector('[data-send-status]'),message);
   }else missing?.();
  };
  await present(()=>apply().catch(refreshError));
 }catch(error){
  refreshError(error);
 }
}
function confirmedReviewWords(data,result){
 return result.words_saved===true ? data.get('feedback') : '';
}
const reviewSaveAttemptHeader=''' + json.dumps(SAVE_ATTEMPT_HEADER) + r''';
const reviewSaveUnconfirmed=''' + json.dumps(send_hold.MAY_HAVE_LEFT) + r''';
const reviewHeldUndone=''' + json.dumps(send_hold.HoldState.UNDONE.value) + r''';
const reviewHeldMissing=''' + json.dumps(send_hold.HoldState.MISSING.value) + r''';
const reviewSaveUnknown='Not saved: The save outcome is not known yet.';
function reviewRequestSignal(){
 const page=document.querySelector('[data-review]');
 const seconds=page ? Number(page.dataset.requestTimeoutSeconds) :
  JSON.parse(document.querySelector('[data-card-settings]').textContent).requestTimeoutSeconds;
 return AbortSignal.timeout(seconds*1000);
}
function reviewSaveOutcomeChanged(key,draft,result){
 presentReviewSaveOutcome(key,draft,result);
 const row=document.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');
 const form=row?.querySelector('[data-review-form]');
 if(reviewReceiptSavedWords(result) && reviewFormEdits.get(form)===draft.edit){
  form.elements.feedback.value='';
  reviewFormEdits.delete(form);
 }
 reviewDraftsChanged();
}
const reviewSaveReports=window.frameElement?.linkedReviewSaveReports ??
 createReviewSaveReports(reviewDrafts,reviewRequestSignal,reviewSaveOutcomeChanged);
const flushReviewSaveReports=reviewSaveReports.flush;
addEventListener('online',()=>void flushReviewSaveReports());
async function reviewFetch(url,options){
 const response=await fetch(url,options);
 void flushReviewSaveReports();
 return response;
}
async function saveReviewAction(data,{
 form,hold,receiver,sending,confirmedSave,saved,failed,unconfirmed,deliveryFailed,undone=failed,displayFailed,recover,finished
}){
 const key=data.get('row'),draft=reviewDrafts.get(key),edit=draft?.edit;
 const retrying=data.has('feedback_retry'),action=data.get('outcome');
 const routes=JSON.parse(form.dataset.reviewRecipients || '{}');
 const carriesOptionalWords=(action==='later' || action==='decline' ||
  (action==='read' && form.dataset.reviewType!=='response')) && routes[action+'-with-words']?.length &&
  !blankReviewWords.test(data.get('feedback') ?? '');
 const offerFeedbackRetry=()=>{
  if(draft && reviewDrafts.get(key)===draft) draft.feedbackRetry=new URLSearchParams(data);
  form.showFeedbackRetry(data);
 };
 const attempt=Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
 let conflict=false,confirmed=false,response,result;
 const noteFailure=(reason,outcomeUnknown,failedAttempt=attempt,retryDelivery=false)=>{
  const body=new URLSearchParams({row:key,token:data.get('token'),feedback:data.get('feedback'),
   reason,failure_attempt:failedAttempt});
  if(draft && reviewDrafts.get(key)===draft && draft.edit===edit){
   draft.failureAttempt=failedAttempt;
   delete draft.filedBug;
   if(retryDelivery) rememberReviewDeliveryRetry(draft,body);
   reviewDraftsChanged();
  }
  reviewSaveReports.add(failedAttempt,{body,edit,outcomeUnknown});
 };
 try{
  const saveAction=['later','validate'].includes(data.get('outcome'));
  sending?.();
   const options={method:'POST',body:data,headers:{[reviewSaveAttemptHeader]:attempt}};
   response=retrying ? await fetch(location.pathname,options) :
    await hold.post(location.pathname,options,saveAction ? null : receiver);
  void flushReviewSaveReports();
  conflict=response.status===409;
  result=await response.json().catch(()=>null);
  if(response.status===404 && result?.hold_state===reviewHeldUndone) throw new SendUndone();
  if(!response.ok) throw new Error(result?.reason || 'HTTP '+response.status);
  const sendFailure=reviewReplyDeliveryFailure(result);
  if(sendFailure){
   hold.restore();
    if(result.action_feedback===true) offerFeedbackRetry();
   deliveryFailed(sendFailure);
    if(result.action_feedback!==true || typeof result.words_saved==='boolean')
     noteFailure(sendFailure,false,attempt,result.retry_delivery===true);
   return;
  }
   if(result?.action_feedback===true && typeof result.words_saved!=='boolean') result.words_saved=true;
  if(typeof result?.answer!=='string' || typeof result.words_saved!=='boolean'){
   hold.restore();
    if(carriesOptionalWords) offerFeedbackRetry();
   unconfirmed(reviewSaveUnknown);
   noteFailure('The server response is unreadable.',true);
   return;
  }
  confirmed=true;
  confirmedSave?.(result);
   if(retrying){
    if(draft) delete draft.feedbackRetry;
    form.querySelector('[data-review-feedback-retry]')?.remove();
    if(reviewDraftWords(key) && reviewDraftWords(key)!==data.get('feedback')){
     form.dataset.reviewSavedDraft='';
     hold.restore();
     deliveryFailed('Sent the original words. Your new words are in the box.');
     return;
    }
   }
  await saved(result);
 }catch(error){
  const wasHeld=hold.restore();
  if(error instanceof SendUndone) await undone(error.message);
  else if(confirmed) displayFailed(wasHeld);
  else{
   const lostReply=!response || !result || (response.status===404 && result.hold_state===reviewHeldMissing) ||
    (response.status===503 &&
    (error.message===reviewSaveUnconfirmed || error.message.startsWith('The held send failed: ')));
   const refusedAttempt=typeof result?.failure_attempt==='string' ? result.failure_attempt : undefined;
   if(lostReply && !refusedAttempt){
    if(carriesOptionalWords && !draft?.deliveryRetry) offerFeedbackRetry();
    unconfirmed(reviewSaveUnknown);
    noteFailure(error.message,true);
    return;
   }
   if(refusedAttempt) noteFailure(error.message,false,refusedAttempt);
   if(conflict) await recover(error.message);
   else failed(error.message);
  }
 }finally{
  await finished(conflict);
 }
}
'''

CARD_VIEW_SCRIPT = r'''(() => {
const frame=window.frameElement;
const card=document.querySelector('[data-review-card]'),updates=card.querySelector('[data-review-card-updates]');
const notice=updates.querySelector('[data-review-card-notice]'),showUpdates=updates.querySelector('[data-show-record-updates]');
function report(text){
 setReviewDraftMessage(card.firstElementChild?.dataset.key,text);
 showActionFailure(notice,text);
 notice.title=text;
}
const motion=createCardMotion(()=>card);
const boxFrame=frame?.closest('[data-list-review-dialog]') ? frame : null;
function fitReviewBox(){
 const box=card.firstElementChild;
 const header=box.querySelector('.review-box-header'),footer=box.querySelector('.review-box-footer');
 const detail=box.querySelector('[data-review-detail]'),menu=footer?.querySelector('[data-later-menu]');
 const style=getComputedStyle(box),oldMin=box.style.getPropertyValue('--review-body-min');
 const minBody=footer ? Math.max(96,menu?.offsetHeight || 0) : 0;
 const fixedHeight=parseFloat(style.paddingTop)+header.getBoundingClientRect().height+parseFloat(style.paddingBottom)
  +(footer ? parseFloat(getComputedStyle(footer).marginTop)+footer.getBoundingClientRect().height : 0);
 const min=minBody+'px';
 box.style.setProperty('--review-body-min',min);
 const height=Math.ceil(fixedHeight+Math.max(minBody,detail.getBoundingClientRect().height));
 boxFrame?.style.setProperty('--review-card-height',height+'px');
 const whole=fixedHeight+minBody>parseFloat(style.maxHeight);
 box.toggleAttribute('data-whole-scroll',whole);
 if(whole && min!==oldMin && menu?.contains(document.activeElement))
  document.activeElement.scrollIntoView({block:'nearest'});
}
const resizeBox=new ResizeObserver(fitReviewBox);
window.addEventListener('resize',fitReviewBox);
function showCard(row){
 row.querySelector('.review-box-header').append(updates);
 card.replaceChildren(row);
 resizeBox.disconnect();
 resizeBox.observe(row,{box:'border-box'});
 for(const element of row.querySelectorAll('.review-box-header,[data-review-detail],.review-box-footer,[data-later-menu]'))
  resizeBox.observe(element,{box:'border-box'});
 fitReviewBox();
}
function mountCard(page,replaceCard=false){
 const {entry,titleText,zone,later:settings,requestTimeoutSeconds}=JSON.parse(page.querySelector('[data-card-settings]').textContent);
 const row=document.createElement('article');
 row.className='review-box';
 Object.assign(row.dataset,{listItem:'',key:entry.key,state:entry.state,type:entry.type});
 const type=entry.type==='for-you' ? 'For you' : entry.type[0].toUpperCase()+entry.type.slice(1);
 const state=entry.actionStatus==='withdrawn' ? 'Withdrawn' : entry.state,urgency=reviewUrgency(entry);
 row.innerHTML='<header class="review-box-header"><h1>'+escapeText(entry.title)+'</h1><div class="review-box-meta">'
  +'<span>'+escapeText(entry.id)+'</span><span>'+type+'</span><span>'+colored(entry.ownerName,entry.ownerHtml)+'</span>'
  +'<span class="list-state '+(entry.state==='Open' ? 'warning' : 'muted')+'">'+state+'</span>'
  +(entry.filed ? '<time datetime="'+new Date(entry.filed*1000).toISOString()+'">'+reviewAge(entry.filed*1000)+'</time>' : '')
  +reviewWaiting(entry)+(entry.unreadAnswer ? newAnswerMarkup : '')+(urgency ? '<span>'+escapeText(urgency)+'</span>' : '')+'</div></header>'
  +'<div class="review-box-scroll" role="region" aria-label="Review details" tabindex="0"><div data-review-detail></div></div>';
 row.openDetails=row.querySelector('[data-review-detail]');
 installReviewBody(row,page.querySelector('[data-card-body]').innerHTML);
 const title=plainReviewText(titleText);
 for(const section of row.querySelectorAll('[data-review-body] > section,[data-review-body] > .change-text > section')){
  if(!section.querySelector('a[href]') && reviewBodyText(section,true)===title) section.dataset.emptyReviewSection='';
 }
 const form=row.querySelector('[data-review-form]');
 if(form?.elements.feedback){
  const footer=document.createElement('footer');
  footer.className='review-box-footer';footer.append(form);row.append(footer);
 }else form?.remove();
 if(replaceCard){
  if(form?.elements.feedback) setCardSubmitting(form,true);
  return motion.switchCard(card.firstElementChild,row,()=>showCard(row)).then(()=>{
   if(form?.elements.feedback) setCardSubmitting(form,false);
   return bindCard(row,zone,settings,requestTimeoutSeconds);
  });
 }
 showCard(row);
 return bindCard(row,zone,settings,requestTimeoutSeconds);
}
function bindCard(row,zone,settings,requestTimeoutSeconds){
 const form=row.querySelector('[data-review-form]'),key=row.dataset.key;
 const settled=(settledKey,draft,result)=>{
  if(settledKey!==key || reviewFormEdits.get(form)!==draft.edit) return;
  const wordsSaved=reviewReceiptSavedWords(result);
  if(wordsSaved){
   form.elements.feedback.value='';
   reviewFormEdits.delete(form);
  }
  report(wordsSaved ? '' : draft.message);
 };
 presentReviewSaveOutcome=settled;
 const revealDraft=()=>{
  if(form?.elements.feedback?.type==='textarea') hideReviewDraftNotice(key);
  else showReviewDraftNotice(key,notice);
 };
 if(boxFrame) boxFrame.reviewDraft={
  reveal:revealDraft,
  settled,
  hide:anchor=>{
   rememberReviewDraft(form);
   showReviewDraftNotice(key,anchor);
  },
  release:()=>{if(!reviewDraftWords(key)) clearReviewDraft(key);},
  hasDraft:()=>{
   rememberReviewDraft(form);
   const draft=reviewDrafts.get(key)?.edit.draft;
   return Boolean(draft?.feedback || draft?.later);
  },
 };
 revealDraft();
 showUpdates.onclick=null;
 if(!form?.elements.feedback) return row;
 form.querySelector('[data-send-status]')?.remove();
 let conflict='',applyUpdate;
 showUpdates.onclick=async()=>{
  if(!applyUpdate) return;
  const apply=applyUpdate;applyUpdate=null;
  const hadFocus=document.activeElement===showUpdates;
  showUpdates.disabled=true;
  await apply();
  showUpdates.disabled=false;
  if(form.isConnected){applyUpdate=apply;return;}
  const focus=document.activeElement;
  showUpdates.hidden=true;
  if(hadFocus && (focus===showUpdates || focus===document.body))
   card.querySelector('[name="feedback"]')?.focus({preventScroll:true});
 };
 const busy=active=>{
  setCardSubmitting(form,active);
  if(form.hasAttribute('data-review-saved-draft'))
   form.querySelectorAll('button').forEach(button=>button.disabled=true);
  if(frame){
   frame.toggleAttribute('data-saving',active);
   frame.dispatchEvent(new Event('review-card-busy'));
  }
 };
 const recover=reason=>{
  conflict=reason;
  const present=apply=>{
   applyUpdate=apply;
   showUpdates.hidden=false;
   for(const button of form.querySelectorAll('button:is([type="submit"],:not([type]))'))
    button.setAttribute('aria-disabled','true');
  };
  return recoverReviewRevision(reason,{form,report,present,reload:async()=>{
   const signal=AbortSignal.timeout(requestTimeoutSeconds*1000);
   const response=await reviewFetch(location.href,{cache:'no-store',signal});
   if(!response.ok) throw new Error('HTTP '+response.status);
   const template=document.createElement('template');
   template.innerHTML=await response.text();
   const page=template.content;
   const body=page.querySelector('[data-card-body]').content;
   const {cardFadeOutMs,cardFadeInMs}=page.querySelector('[data-review-card]').dataset;
   Object.assign(card.dataset,{cardFadeOutMs,cardFadeInMs});
   return {revision:body.querySelector('[name="revision"]')?.value,show:()=>mountCard(page,true)};
  }});
 };
 const send=async(outcome,data)=>{
  if(cardSubmitBlocked(form) || form.hasAttribute('data-review-saved-draft') || !showUpdates.hidden) return;
  const submitted=rememberReviewDraft(form,true);
  busy(true);
  if(conflict){
   await recover(conflict);
   busy(false);
   return;
  }
  report('');
  let saved=false;
  const hideForSend=()=>boxFrame?.dispatchEvent(new Event('review-card-sending'));
  const responseFollowup=row.dataset.type==='response' && outcome==='question';
  const hold=holdCard(row,responseFollowup ? held=>{if(held) hideForSend();} : undefined);
  const finishSave=(message='')=>{
   if(reviewDraftWords(key)){
    hold.restore();
    report(reviewDrafts.get(key).message);
    if(boxFrame){
     boxFrame.dataset.focusReviewDraft='';
     boxFrame.dispatchEvent(new Event('review-card-saved-with-draft'));
    }
    return;
   }
   saved=true;
   if(frame){
    frame.removeAttribute('data-saving');
    frame.dispatchEvent(new CustomEvent('review-card-saved',{detail:message}));
   }else location.reload();
  };
  await saveReviewAction(data,{
   form,hold,
   receiver:sendActionReceiver(form,data),
   sending:responseFollowup || data.has('feedback_retry') ? undefined : hideForSend,
   confirmedSave:result=>confirmReviewDraft(key,submitted,confirmedReviewWords(data,result),form),
   saved:result=>finishSave(result.detail_unavailable ? 'Saved; the item could not refresh.' : ''),
   failed:reason=>report('Not saved: '+reason),
   unconfirmed:report,
   deliveryFailed:report,
   undone:message=>{
    report(message);
    if(boxFrame) boxFrame.dataset.focusReviewDraft='';
    else form.elements.feedback.focus();
   },
   displayFailed:()=>finishSave('Saved; the item could not refresh.'),
   recover,
   finished:()=>{if(!saved) busy(false);}
  });
 };
 bindReviewForm(form,{zone:()=>zone,settings:()=>settings,send});
 return row;
}
mountCard(document);
void flushReviewSaveReports();
setInterval(updateHandoffAges,5000);
})();'''

CARD_REQUEST_SCRIPT = record_table.SCRIPT + console.REVIEW_PLACE_SCRIPT + r'''
const kinds=''' + json.dumps(WIRE_TYPES) + ''',states=''' + json.dumps(INDEX_STATES) + r''';
function indexEntry(payload,row,at){
 const owner=payload.n[row[3]],source=payload.n[row[4]];
 return {key:row[0],id:row[1] || row[0],type:row[13]?'response':kinds[row[2]],owner:owner[0],sourceOwner:source[0],
  ownerName:owner[1],sourceName:source[1],title:row[5],state:states[row[6]],filed:row[7],updated:row[8],
   hiddenUntil:row[9],ownerHtml:owner[2],sourceHtml:source[2],urgency:row[10] || 'normal',handoff:row[11],
   waitingWhy:row[12],actionStatus:row[14],unreadAnswer:row[15]===true,order:at+row[6]*payload.r.length};
}
function readIndex(payload){
 return new Map(payload.r.map((row,at)=>[row[0],indexEntry(payload,row,at)]));
}
let index=readIndex(JSON.parse(document.querySelector('[data-review-index]').textContent));
function requestReviewCard(key,version,saved=false){
 const query=new URLSearchParams({body:key,version});
 if(saved) query.set('saved','1');
 return reviewFetch(location.pathname+'?'+query,{cache:'no-store',signal:reviewRequestSignal()})
  .then(async response=>({response,text:response.ok ? await response.text() : ''}));
}
const firstReviewCard=(()=>{
 const frame=window.frameElement;
 if(frame?.reviewPlace){
  const {place,table}=frame.reviewPlace;
  delete frame.reviewPlace;
  if(!history.state?.recordTable && place===location.pathname+location.search+location.hash)
   history.replaceState({recordTable:table},'');
 }
 const params=recordPlaceParams(reviewPlaceOptions);
 legacyReviewPlace(params);
 const key=recordItemLink(params,value=>resolveShownRecordId(index,value));
 if(!index.has(key)) return null;
 const version=document.querySelector('[data-review]').dataset.version;
 const result=requestReviewCard(key,version);
 void result.catch(()=>{});
 return {key,version,result};
})();
'''

REVIEW_QUEUE_SCRIPT = (question_path.queue_script() + '''
const reviewQueues=''' + json.dumps(review_items.QUEUE_BY_STATE) + ''';
const showsForQueue=''' + json.dumps({
    queue: 'open' if state == ReviewState.OPEN else state for state, queue in review_items.QUEUE_BY_STATE.items()
}) + ';\n')


SCRIPT = (page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + record_table.DATE_TIME_SCRIPT
          + REVIEW_CARD_SCRIPT + r'''(reviewPlace => {
''' + REVIEW_QUEUE_SCRIPT + r'''
let content=document.querySelector('[data-review]');
const saved=new Map(),bodies=new Map(),bodyRequests=new Map();
const pendingSaves=new Map(),recovering=new Map();
const scrollDepartures=new Set();
const savedVerdictsKey='review-saved-verdicts:'+location.pathname;
let focusAfterLoad=null,firstRequest=firstReviewCard;
let deferredLink=null,revealCreated=null,newItemRefresh;
let failedDelivery=null;
presentReviewSaveOutcome=(key,draft,result)=>{
 const retained=failedDelivery,row=view.openItem;
 if(retained?.entry?.key!==key || retained.edit!==draft.edit) return;
 if(reviewReplyDeliveryFailure(result)){
  if(retained.session===view.openSession && row?.dataset.key===key)
   view.preserveRecordRowTop(()=>view.showActionResult(row,retained.submission,key,{kind:'failure',message:draft.message}));
 }else{
  failedDelivery=null;
  if(row?.dataset.key===key && !index.has(key)){view.closeCard(key);view.applyAction();}
 }
};
function missingDeliveryEntry(key){
 const entry=failedDelivery?.entry;
 return entry && entry.key===key && !index.has(key) &&
  failedDelivery.session===view.openSession && view.opened.has(key) ? entry : null;
}
function allEntries(){
 const entries=[...index.values()],missing=missingDeliveryEntry(failedDelivery?.entry.key);
 if(missing) entries.push(missing);
 return entries;
}
function isInvestigation(entry){return entry.type==='investigation' || entry.type==='bug';}
function rowOwners(entry){
 return isInvestigation(entry) && entry.sourceOwner!==entry.owner ? [entry.owner,entry.sourceOwner] : [entry.owner];
}
function rowTypes(entry){return isInvestigation(entry) ? ['',entry.type,'investigations'] : ['',entry.type];}
function reviewQueue(entry,now){
 return questionQueue(reviewQueues[entry.state],entry.unreadAnswer,entry.hiddenUntil>now,false,false);
}
function rowShows(entry){
 if(entry.actionStatus==='withdrawn') return [];
 const queue=questionQueue(reviewQueues[entry.state],entry.unreadAnswer,false,false,false);
 return [showsForQueue[queue]];
}
function typeChipShow(show,type){
 return type==='for-you' ? 'open' : show;
}
function availableForFilters(entry,{query,keys,now=Date.now()}){
 const linked=keys?.has(entry.key);
 return (!keys?.size || linked) && (linked || view.linkedItem===entry.key || reviewQueue(entry,now)!=='later') &&
  (!query || entry.id.toLowerCase().includes(query) || entry.title.toLowerCase().includes(query));
}
function ownerHasType(entry,{owner,type}){
 return rowOwners(entry).includes(owner) && rowTypes(entry).includes(type);
}
// A linked item that a save handed off waits for the snapshot that confirms it,
// unless its own save kept the row open with the result and the row has stayed open since.
function awaitsHandoff(key){
 const departure=saved.get(key);
 const ownRowOpen=departure?.ownSession===view.openSession && view.opened.has(key);
 return Boolean(departure?.handed && departure.awaitingSnapshot && !ownRowOpen && view.linkedItem===key);
}
function isListed(entry,filters){
 const {owner,show,type,sourceOwner}=filters;
 const key=entry.key,departure=saved.get(key);
 const pending=pendingSaves.get(key);
 if(!index.has(key) || pending?.toast || pending?.departing || awaitsHandoff(key)) return false;
 return (!departure || entry.type==='for-you' || departure.readAnswer && entry.state!=='Done' ||
  departure.handed && entry.state!=='Open') &&
  availableForFilters(entry,filters) && (!owner || rowOwners(entry).includes(owner)) &&
  rowTypes(entry).includes(type) && (!sourceOwner || entry.sourceOwner===sourceOwner) &&
  (show==='all' || rowShows(entry).includes(show));
}
function updateReviewChips(rows,filters,matches,shown){
 const {owner,show,type}=filters;
 const allOwners={...filters,owner:'',sourceOwner:''};
 const allShows={...filters,show:'all'},allTypeShows={...allShows,type:''};
 const anyOwnerShows={...allOwners,show:'all'},anyOwnerTypeShows={...anyOwnerShows,type:''};
 const owners=new Map(),types=new Map(),shows=new Map();
 const anyOwnerTypeCounts=new Map(),anyOwnerShowCounts=new Map();
 const eligibleOwners=new Set(),ownerTypes=new Set();
 const add=(counts,choices)=>{
  for(const value of choices) counts.set(value,(counts.get(value) ?? 0)+1);
 };
 for(const item of rows){
  const row=item.dataset,ownersForRow=rowOwners(row),typesForRow=rowTypes(row);
  const showsForRow=['all',...rowShows(row)];
  const typesForClick=typesForRow.filter(kind=>showsForRow.includes(typeChipShow(show,kind)));
  if(availableForFilters(row,filters)){
   if(typesForRow.includes(type)) for(const id of ownersForRow) eligibleOwners.add(id);
   if(ownersForRow.includes(owner)) for(const kind of typesForRow) ownerTypes.add(kind);
  }
  if(matches(item,allOwners)) add(owners,['',...ownersForRow.filter(Boolean)]);
  if(matches(item,allTypeShows)) add(types,typesForClick);
  if(matches(item,allShows)) add(shows,showsForRow);
  if(owner && matches(item,anyOwnerTypeShows)) add(anyOwnerTypeCounts,typesForClick);
  if(owner && matches(item,anyOwnerShows)) add(anyOwnerShowCounts,showsForRow);
 }
 const ownerRow=filterRow('owner'),showRow=filterRow('show'),typeRow=filterRow('type');
 filterControl.setVisible(ownerRow,[owner,...eligibleOwners].map(staffChipValue));
 for(const button of filterControl.buttons(ownerRow)){
  const id=filterControl.ownerId(button);
  filterControl.setCount(button,id===owner ? shown : owners.get(id) ?? 0);
 }
 const showCounts=owner && !ownerTypes.has(type) ? anyOwnerShowCounts : shows;
 for(const button of filterControl.buttons(showRow)){
  const value=filterControl.value(button);
  filterControl.setCount(button,value===show ? shown : showCounts.get(value) ?? 0);
 }
 for(const button of filterControl.buttons(typeRow)){
  const value=filterControl.value(button),kind=value==='all' ? '' : value;
  if(kind==='investigations') button.hidden=type!=='investigations';
  // A Show reset checks the current Type before the new Type applies.
  const resetsShow=typeChipShow(show,kind)!==show;
  const resetsOwner=owner && (!ownerTypes.has(kind) || resetsShow && !ownerTypes.has(type));
  const counts=resetsOwner ? anyOwnerTypeCounts : types;
  filterControl.setCount(button,kind===type ? shown : counts.get(kind) ?? 0);
 }
}
function savedEntry(key,result){
 const entry=index.get(key);if(!entry) return;
 // A Done row that shows its own result stays in the index and shows its new state.
 if(result.state==='Done' && entry.type!=='for-you' && !result.retained){index.delete(key);return;}
 entry.state=result.state;entry.hiddenUntil=result.later;
 if(result.acknowledged) entry.unreadAnswer=false;
 if(result.state==='Done') entry.actionStatus='done';
 if(result.type) entry.type=result.type;
}
function snapshotPrecedesSave(entry,result){
 return entry?.state===result.previousState && entry.updated===result.updated &&
  (result.previousUnreadAnswer===undefined || entry.unreadAnswer===result.previousUnreadAnswer);
}
function storeSavedVerdicts(){
 const verdicts=[...saved].filter(([,result])=>result.closes && result.awaitingSnapshot);
 try{
  if(verdicts.length) sessionStorage.setItem(savedVerdictsKey,JSON.stringify(verdicts));
  else sessionStorage.removeItem(savedVerdictsKey);
 }catch{}
}
try{
 for(const [key,result] of JSON.parse(sessionStorage.getItem(savedVerdictsKey) || '[]')){
  if(!snapshotPrecedesSave(index.get(key),result)) continue;
  saved.set(key,{...result,restored:true});savedEntry(key,result);
 }
}catch{}
storeSavedVerdicts();
const filterRow=name=>filterControl.row(content,name);
const showValues=filterControl.buttons(filterRow('show')).map(button=>filterControl.value(button));
const mountReviewRow=entry=>reviewCardSummary(entry,Number(content.dataset.titleLimit),
 entry.type==='bug' ? content.querySelector('[data-review-bug-icon]').innerHTML :
  filterRow('type').querySelector('[data-filter-value="'+entry.type+'"] .review-icon').outerHTML);
const missingReview='This review is no longer on the Review page.';
let missingCard=false,visibleReviewDraftKey='';
function reviewNotice(){return content.querySelector('[data-review-notice]');}
function syncReviewDraft(){
 const row=view.openItem,key=row?.dataset.key;
 const form=recordDetails(row)?.querySelector('[data-review-form]');
 const visible=row?.isConnected && !row.hidden && !pendingSaves.get(key)?.toast &&
  form?.elements.feedback?.type==='textarea' ? key : '';
 if(visibleReviewDraftKey && visibleReviewDraftKey!==visible)
  showReviewDraftNotice(visibleReviewDraftKey,reviewNotice());
 if(visible) hideReviewDraftNotice(visible);
 visibleReviewDraftKey=visible;
}
const view=createRecordTable({...reviewPlaceOptions,content:()=>content,
 entries:allEntries,mount:mountReviewRow,mounted:()=>{loadBodies();syncReviewDraft();},
 retainDetailState:(previous,row)=>{
  const version=previous.dataset.bodyVersion;
  if(previous.dataset.bodyLoaded==='true' && version===content.dataset.version){
   row.dataset.bodyVersion=version;row.dataset.bodyLoaded='true';
  }
 },
 focusKey:()=>focusAfterLoad?.key,
 pinOpenRow:key=>{
  const pending=pendingSaves.get(key);
  return (!pending || !pending.toast && pending.keepOpen) && !awaitsHandoff(key);
 },
 unmount:rememberItem,
 rememberItem,
 place:params=>{
  reviewPlace(params);
  if(params.get('type')==='bug') params.delete('type');
  const show=showValues.find(value=>value.toLowerCase()===params.get('show')?.toLowerCase());
  if(show) params.set('show',show);
 },
 selectShow:show=>showValues.includes(show) ? show : 'open',
 persistLinkedTarget:true,
 missingItem:()=>missingReview,
 resolveOwner:owner=>{
  const button=filterControl.buttons(filterRow('owner')).find(button=>
   filterControl.ownerId(button)===owner || button.dataset.ownerName===owner);
  return button ? filterControl.ownerId(button) : owner;
 },
 selectOwner:(rows,filters)=>!filters.owner || rows.some(({dataset:entry})=>
  availableForFilters(entry,filters) && ownerHasType(entry,filters)) ? filters.owner : '',
 resolveItem:value=>resolveShownRecordId(index,value),
 hasKey:key=>index.has(key) || Boolean(missingDeliveryEntry(key)),
 shows:showValues,
 staffChipFilters:['owner'],
 visible:isListed,
 sortValues:data=>data.sortValues===undefined ? entrySortValues(data) : undefined,
 applied:(items,filters,{rows,matches,count})=>{
  const {type,show}=filters;
  content.querySelector('table').pageData=()=>({
   columns:['Id','Type','Owner','Source','Item','State','Filed'],
   rows:(function*(){
    for(const item of items){
     if(!index.has(item.dataset.key) || !matches(item,filters)) continue;
     const entry=item.dataset;
     yield [entry.id,entry.type,entry.ownerName,entry.sourceName,entry.title,entry.state,
      new Date(entry.filed*1000).toISOString()];
    }
   })()
  });
  content.dataset.show=show;
  if(view.openItem) missingCard=false;
  if(missingCard) showActionFailure(content.querySelector('[data-missing-item]'),missingReview);
  loadBodies();
  const actionNotices=content.querySelector('[data-for-you-notices]');
  actionNotices.hidden=type!=='for-you' || !actionNotices.children.length;
  updateReviewChips(rows,filters,matches,count);
 }
});
async function afterRowAnimations(run,current=()=>true){
 while(current() && view.hasActiveRowAnimations) await view.settled();
 if(current()) return run();
}
// Wait for the table update and its row fades before a draft copy changes the layout.
presentReviewDraftCopy=update=>queueMicrotask(()=>{void afterRowAnimations(update);});
addEventListener('scroll',()=>{
 const waiting=scrollDepartures.size>0;
 for(const save of pendingSaves.values()){
  if(save.departing && !save.saved && !save.held && Math.abs(scrollY-save.top)>=innerHeight) scrollDepartures.add(save);
 }
 if(waiting || !scrollDepartures.size) return;
 void afterRowAnimations(()=>{
  let redraw=false;
  for(const save of pendingSaves.values()){
   if(!scrollDepartures.has(save) || save.saved) continue;
   save.departing=false;redraw=true;
  }
  scrollDepartures.clear();
  if(redraw) view.apply(false);
 });
},{passive:true});
function mountedRow(key){return content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');}
function reviewRow(key){
 const item=view.openItem;
 return index.has(key) && item?.dataset.key===key ? item : mountedRow(key);
}
function rememberFocus(){
 const field=document.activeElement,row=field?.closest('[data-list-item]');
 if(!row) return null;
 const selector=field.id ? '#'+CSS.escape(field.id) : field.localName+['name','value','href','class'].map(name=>
  field.hasAttribute(name) ? '['+name+'="'+CSS.escape(field.getAttribute(name))+'"]' : '').join('');
 return {key:row.dataset.key,selector,position:[...row.querySelectorAll(selector)].indexOf(field),
  start:field.selectionStart,end:field.selectionEnd,direction:field.selectionDirection};
}
function restoreFocus(key){
 if(focusAfterLoad?.key!==key) return;
 if(document.activeElement!==document.body){focusAfterLoad=null;return;}
 const field=mountedRow(key)?.querySelectorAll(focusAfterLoad.selector)[focusAfterLoad.position];
 if(!field) return;
 const {start,end,direction}=focusAfterLoad;focusAfterLoad=null;
 field.focus({preventScroll:true});
 if(typeof field.setSelectionRange==='function' && typeof start==='number') field.setSelectionRange(start,end,direction);
}
function needsBody(row){
 const pending=saved.get(row.dataset.key);
 return !row.dataset.bodyLoaded && !pending?.retained && !(pending?.handed && pending.awaitingSnapshot);
}
function applyReviewSummary(row,entry){
 const next=mountReviewRow({...entry,order:Number(row.dataset.order)});
 const previous=row.querySelector('.list-row'),summary=next.querySelector('.list-row');
 const focused=previous.querySelector('.list-toggle')===document.activeElement;
 Object.assign(row.dataset,next.dataset);
 summary.classList.add('open');
 const toggle=summary.querySelector('.list-toggle');
 toggle.setAttribute('aria-expanded','true');
 previous.replaceWith(summary);
 return focused ? toggle : null;
}
function applyReviewDetail(key,row,detail,text,bodyVersion=''){
 const template=document.createElement('template');
 template.innerHTML=text;
 const summary=template.content.querySelector('[data-review-summary]');
 let focus=null;
 if(bodyVersion && bodyVersion!==content.dataset.version){
  const entry=summary && readIndex(JSON.parse(summary.textContent)).get(key);
  if(!entry) throw new Error('The item has no summary for its snapshot.');
  focus=applyReviewSummary(row,entry);
 }
 summary?.remove();
 installReviewBody(row,template.content);
 const departure=saved.get(key);
 if(departure?.closes && departure.awaitingSnapshot)
  detail.querySelector('[data-review-form]')?.remove();
 bindForm(row);
 syncReviewDraft();
 focus?.focus({preventScroll:true});
}
function loadBodies(refreshKey=''){
 view.opened.forEach(key=>{
  const row=view.openItem;
  const refresh=key===refreshKey;
  if(row?.dataset.key!==key || !index.has(key) || (!refresh && !needsBody(row))) return;
  const install=({text,version})=>{
   const current=view.openItem;
   if(current?.dataset.key!==key || !index.has(key)) return;
   const detail=recordDetails(current);
   const apply=(row,detail)=>applyReviewDetail(key,row,detail,text,version);
   view.itemError('');
   current.dataset.bodyLoaded='true';
   view.preserveRecordRowTop(()=>{
    view.presentItemRevision(current,text,apply,{force:version!==content.dataset.version});
    current.dataset.bodyVersion=version;
    drawSaveControls(detail.querySelector('[data-review-form]'));
    restoreFocus(key);
   });
  };
  const cached=refresh ? undefined : bodies.get(key);
  if(cached){install(cached);return;}
  if(bodyRequests.has(key)) return;
  drawSaveControls(recordDetails(row).querySelector('[data-review-form]'));
  const requestedIndex=index;
  const version=content.dataset.version;
  const prefetched=!refresh && firstRequest?.key===key && firstRequest.version===version;
  const result=prefetched ? firstRequest.result : requestReviewCard(key,version);
  if(prefetched) firstRequest=null;
  const current=()=>bodyRequests.get(key)===request && view.openItem?.dataset.key===key &&
   view.opened.has(key) && index.has(key) && !pendingSaves.has(key);
  const request=result
   .then(({response,text})=>{
    if(!current()) return;
    const bodyVersion=response.headers.get('X-Review-Version');
    const pageVersion=content.dataset.version;
    if(requestedIndex!==index && bodyVersion!==pageVersion){
     bodyRequests.delete(key);loadBodies();
     return;
    }
    if(response.status===404){
     missingCard=true;view.closeCard(key);view.applyAction();
     if(bodyVersion && bodyVersion!==pageVersion) void pageRefresh.refresh(true);
     return;
    }
    if(!response.ok) throw new Error('HTTP '+response.status);
    if(!bodyVersion) throw new Error('The item has no snapshot version.');
    if(text.includes(' data-review-card-unavailable>')) throw new Error('The item body is unavailable.');
    const body={version:bodyVersion,text};
    install(body);bodies.set(key,body);
    if(bodyVersion!==pageVersion) void pageRefresh.refresh(true);
   }).catch(error=>{
    if(current()){
     const item=view.openItem;
     item.dataset.bodyLoaded='failed';
     const loading=recordDetails(item).querySelector('[data-record-loading]');
     if(loading) loading.hidden=true;
     view.itemError('The item could not refresh: '+error.message);
    }
   })
   .finally(()=>{if(bodyRequests.get(key)===request) bodyRequests.delete(key);});
  bodyRequests.set(key,request);
 });
}
function rememberItem(row){
 rememberReviewDraft(recordDetails(row).querySelector('[data-review-form]'));
}
function recoveringForm(key,form){
 return recovering.has(key) && recovering.get(key).revision===form.elements.revision?.value;
}
function drawSaveControls(form){
 if(!form) return;
 const row=recordFromDetail(form),key=row.dataset.key;
 const pending=pendingSaves.has(key);
 setCardSubmitting(form,pending);
 if(pending) return;
 const disabled=row.dataset.bodyLoaded!=='true' || !index.has(key) || recoveringForm(key,form) ||
  form.hasAttribute('data-review-saved-draft');
 form.querySelectorAll('button').forEach(button=>button.disabled=disabled);
}
function drawSaveState(key,form=recordDetails(reviewRow(key))?.querySelector('[data-review-form]')){
 if(!form) return;
 drawSaveControls(form);
 showActionFailure(form.querySelector('[data-send-status]'),pendingSaves.get(key)?.notice || view.actionNotice(recordFromDetail(form)));
}
const reviewFormSubmissions=new WeakMap();
const reviewDraftChoiceChanged=''' + json.dumps(REVIEW_DRAFT_CHOICE_CHANGED) + r''';
function reviewActionIntent(form,data){
 const outcome=data.get('outcome'),until=outcome==='later' ? data.get('later') : '';
 const assignee=data.get('assignee') || '';
 let recipients;
 if(assignee){
  const selected=form.elements.assignee?.selectedOptions[0];
  if(selected?.value===assignee && selected.hasAttribute('data-send-receiver')) recipients=[assignee];
 }else{
  const routes=JSON.parse(form.dataset.reviewRecipients || '{}'),withWords=outcome+'-with-words';
  const route=!blankReviewWords.test(data.get('feedback') ?? '') && Object.hasOwn(routes,withWords) ? withWords : outcome;
  recipients=routes[route];
 }
 return {outcome,until,assignee,recipients};
}
function sameReviewAction(chosen,current){
 return chosen.outcome===current.outcome && chosen.until===current.until && chosen.assignee===current.assignee &&
  Array.isArray(chosen.recipients) && Array.isArray(current.recipients) && chosen.recipients.length===current.recipients.length &&
  chosen.recipients.every((recipient,at)=>recipient===current.recipients[at]);
}
function bindForm(row){
 const form=recordDetails(row).querySelector('[data-review-form]');if(!form || form.dataset.bound) return;
 form.dataset.bound='true';
 const key=row.dataset.key;
 drawSaveState(key,form);
 if(!form.elements.feedback) return;
 const send=async(outcome,data,label)=>{
   const retrying=data.has('feedback_retry');
   if(retrying) await view.rowSettled(recordFromDetail(form));
   const current=recordFromDetail(form);
   if(!view.canSubmit(form,data.get('row')) || view.openItem!==current || current.dataset.bodyLoaded!=='true' ||
    !index.has(key) || cardSubmitBlocked(form,view.rowFading(key)) ||
    pendingSaves.has(key) || recoveringForm(key,form) || form.hasAttribute('data-review-saved-draft')) return;
   const row=current;
   recovering.delete(key);
   failedDelivery=null;
   const draftAtSubmit=rememberReviewDraft(form,true);
   const draft=reviewDrafts.get(key);
   const {reviewType:cardType,reviewState:cardState}=form.dataset;
   const retry={cardType,cardState,label,action:reviewActionIntent(form,data),refusedRevision:''};
   if(draftAtSubmit && draft) draft.retry=retry;
   const notice=reviewNotice();
   showActionFailure(notice);
   const validationQuestion=row.dataset.state==='Validating' && outcome==='question';
   const handed=row.dataset.type==='change' && ['validate','question','feedback'].includes(outcome);
   // A question or feedback on a change or a proposal shows its own result, so its row stays open through the save.
   const keepOpen=['question','feedback'].includes(outcome) && ['change','proposal'].includes(row.dataset.type);
   const undoEnabled=content.dataset.undoEnabled==='true';
   const responseFollowup=outcome==='question' && row.dataset.type==='response';
   const held=!retrying && undoEnabled && ['later','validate'].includes(outcome);
   const departing=!retrying && !validationQuestion && !(undoEnabled && responseFollowup);
   const pending={saved:false,departing,held,keepOpen,toast:false,top:scrollY};
   pendingSaves.set(key,pending);pageRefresh.abandon();drawSaveState(key);
   reviewDraftsChanged();
   const request=view.startItemRequest(key),session=view.openSession;
   const closes=outcome==='later' || !['question','feedback','validate'].includes(outcome);
   const submission=view.preserveRecordRowTop(()=>view.startAction(row));
   const hold=holdCard(row,hidden=>{
    pending.toast=hidden;
    if(hidden){pending.held=true;view.applyAction();}
   });
   const reopened=()=>view.openItem?.dataset.key===key && view.opened.has(key) &&
    !view.currentItemRequest(request);
   const showResult=result=>{
    if(reopened()) view.applyAction();
    else view.showActionResult(row,submission,key,result);
   };
   const settle=async()=>{
    pendingSaves.delete(key);setCardSubmitting(form,false);drawSaveState(key);
    reviewDraftsChanged();
    if(isDeferredLink(key)){
     const link=deferredLink;
     await afterRowAnimations(()=>{
      deferredLink=null;
      followItemLink({inheritFilters:true});
     },()=>deferredLink===link && isDeferredLink(key));
    }else if(deferredLink?.href!==location.href){
     deferredLink=null;
     followItemLink({inheritFilters:true,unappliedOnly:true});
    }
   };
   const showFailure=message=>{
    setReviewDraftMessage(key,message);
    pendingSaves.delete(key);
    view.preserveRecordRowTop(()=>showResult({kind:'failure',message}));
   };
   const retainDeliveryCard=()=>{
    if(view.currentItemRequest(request))
     failedDelivery={entry:index.get(key),session,submission,edit:draftAtSubmit};
   };
   const fail=reason=>showFailure('Not saved: '+reason);
   await saveReviewAction(data,{
   form,
   hold,
   receiver:sendActionReceiver(form,data),
   confirmedSave:result=>{
    pending.saved=true;
    bodies.delete(key);
    const currentForm=view.openItem?.dataset.key===key
     ? recordDetails(view.openItem).querySelector('[data-review-form]') : form;
    confirmReviewDraft(key,draftAtSubmit,confirmedReviewWords(data,result),currentForm);
   },
   saved:async result=>{
    const showOwnDetail=view.currentItemRequest(request) && keepOpen && !result.detail_closed;
    const savedMessage=result.detail_unavailable && !showOwnDetail
     ? {kind:'failure',message:'Saved; the item could not refresh.'}
     : {kind:'success',message:outcome==='done' ? 'Saved: '+result.answer : ''};
    if(showOwnDetail){
     view.startDeliveryWindow(request,result);
     let text=result.detail_html;
     if(typeof text!=='string' && ('delivery_pending' in result || result.detail_revision || result.detail_unavailable)){
      const {response,text:body}=await requestReviewCard(key,result.version,true);
      if(!response.ok || body.includes('data-review-card-unavailable')) throw new Error('The saved item body is unavailable.');
      text=body;
     }
     if(typeof text==='string' && view.currentItemRequest(request))
      view.preserveRecordRowTop(()=>view.installOwnItemResult(request,text,(item,detail)=>{
       delete item.dataset.bodyVersion;
       applyReviewDetail(key,item,detail,text);
      }));
    }
    if(validationQuestion){
     hold.restore();
     view.preserveRecordRowTop(()=>{
      if(result.detail_closed && !reopened()) view.closeCard(key);
      showResult(savedMessage);
     });
     return;
    }
    const waitState=outcome==='validate' ? 'Validating' : 'Waiting';
    const previousUnreadAnswer=row.dataset.unreadAnswer==='true';
    const marker={updated:row.dataset.updated,previousState:row.dataset.state,
     previousUnreadAnswer,readAnswer:outcome==='read' && previousUnreadAnswer,
     handed,state:result.state ?? (handed || outcome==='question' ? waitState : outcome==='later' ? row.dataset.state : 'Done'),
     type:result.type,
     later:result.later ?? (outcome==='later' ? Date.parse(data.get('later')) : 0),
     question:outcome==='question',acknowledged:outcome==='read' || outcome==='later' || outcome==='question',
     retained:showOwnDetail && result.state==='Done' ? index.get(key) : null,ownSession:showOwnDetail ? session : undefined,
     closes:result.detail_closed ?? (!handed && !['question','later'].includes(outcome)),awaitingSnapshot:true};
    const applySaved=()=>view.preserveRecordRowTop(()=>{
     hold.restore();
     if(!reopened() && !showOwnDetail && (outcome==='later' || !isDeferredLink(key))) view.closeCard(key);
     saved.set(key,marker);savedEntry(key,marker);
     if(marker.closes) storeSavedVerdicts();
     pendingSaves.delete(key);
     if(outcome==='done' && mountedRow(key)) bind();
     showResult({
      ...savedMessage,hiddenMessage:outcome==='done' && savedMessage.kind==='success' ? savedMessage.message : undefined});
     if((handed || outcome==='done') && parent!==window) parent.postMessage('for-you-completed',location.origin);
    });
    if(outcome==='later') await afterRowAnimations(applySaved);
    else applySaved();
   },
   failed:fail,
   unconfirmed:message=>{
    if(draftAtSubmit?.draft.feedback) retainDeliveryCard();
    showFailure(message);
   },
   deliveryFailed:message=>{
    retainDeliveryCard();
    showFailure(message);
   },
   undone:async message=>{
    pendingSaves.delete(key);
    showResult({kind:'undone',message});
    if(!view.currentItemRequest(request)) return;
    await view.rowSettled(view.openItem);
    if(view.currentItemRequest(request))
     recordDetails(view.openItem).querySelector('textarea[name="feedback"]')?.focus();
   },
   displayFailed:wasHeld=>{
    if(!view.currentItemRequest(request)) return;
    view.preserveRecordRowTop(()=>{
     if(closes){
      view.closeCard(key);
      showResult({kind:'failure',message:'Saved; the item could not refresh.'});
     }else{
      if(wasHeld) view.applyAction();
      view.savedItemError(request);
     }
    });
   },
   recover:async reason=>{
    bodies.delete(key);
    if(reason===reviewDraftChoiceChanged){
     if(draft?.retry===retry) delete draft.retry;
     showFailure('Not saved: '+reason);
     await settle();
     if(!pendingSaves.size) void pageRefresh.refresh(true);
     return;
    }
    if(draft?.retry===retry) retry.refusedRevision=data.get('revision');
    if(!view.currentItemRequest(request)){
     setReviewDraftMessage(key,'Not saved: '+reason);
     showReviewDraftNotice(key,notice);
     await settle();
     if(!pendingSaves.size) void pageRefresh.refresh(true);
     return;
    }
    const recovery={revision:data.get('revision')};
    recovering.set(key,recovery);
    const current=()=>recovering.get(key)===recovery;
    try{
     await recoverReviewRevision(reason,{
      form:recordDetails(reviewRow(key))?.querySelector('[data-review-form]') || form,
      notice,current,
      missing:()=>{
       showReviewDraftNotice(key,notice);
       showActionFailure(notice);
      },
      reload:async()=>{
       fail(reason);
       await settle();
       if(!current()) return;
       await pageRefresh.refresh(true);
       await bodyRequests.get(key);
       const fresh=view.openItem?.dataset.key===key && index.has(key) ? view.openItem : undefined;
       return {revision:recordDetails(fresh)?.querySelector('[name="revision"]')?.value,show:()=>fresh};
      }
     });
    }finally{
     if(current()){
      recovering.delete(key);
      const fresh=recordDetails(reviewRow(key))?.querySelector('[data-review-form]');
      drawSaveControls(fresh);
      reviewDraftsChanged();
     }
    }
   },
   finished:async conflict=>{
    if(conflict) return;
    await settle();
    if(!pendingSaves.size) void pageRefresh.refresh(true);
   }});
  };
 const submit=bindReviewForm(form,{zone:()=>content.dataset.zone,settings:()=>JSON.parse(content.dataset.later),send});
 reviewFormSubmissions.set(form,submit);
}
function bind(){view.replace(false);}
content.addEventListener('click',event=>{
 const chip=event.target.closest('[data-filter-row="type"] [data-filter-value][aria-pressed="false"]');
 const show=content.dataset.show,kind=chip && filterControl.value(chip);
 if(chip && typeChipShow(show,kind==='all' ? '' : kind)!==show)
  filterControl.buttons(filterRow('show')).find(button=>filterControl.value(button)==='open').click();
},true);
function revealNewItem(){
 if(!revealCreated || !index.has(revealCreated.key)) return false;
 const {key,id}=revealCreated;
 revealCreated=null;
 if(view.revealMatchingItem(key)) return true;
 const notice=content.querySelector('[data-review-notice]');
 notice.textContent='The current filters hide '+id+'.';notice.hidden=false;
 return false;
}
async function refresh(_supersede=false,current=()=>true){
 try{
  return await refreshReview(current);
 }catch(error){
  if(current() && view.openItem) view.itemError('Review could not refresh: '+error.message);
  throw error;
 }
}
async function refreshReview(current){
 const hasOpenDraft=()=>Boolean(recordDetails(view.openItem)?.querySelector(
  '[data-review-feedback-retry],[data-review-saved-draft]'));
 const canApply=()=>!pendingSaves.size && !dateTimePickerOpen(document) && !hasOpenDraft() && current();
 if(!canApply()) return;
 const options={cache:'no-store',signal:reviewRequestSignal()};
 const delivery=view.deliveryRequest(),query=new URLSearchParams({version:'1'});
 if(delivery) query.set('delivery',delivery.delivery);
 const response=await reviewFetch(location.pathname+'?'+query,options);
 if(!response.ok) throw new Error('HTTP '+response.status);
 const version=await response.json();
 if(!canApply()) return;
 view.itemError('');
 const finishDelivery=async()=>{
  if(!delivery || version.delivery_pending!==false) return;
  await bodyRequests.get(delivery.key);
  if(canApply()) view.finishDeliveryWindow(delivery);
 };
 if(version.version===content.dataset.version){
  return afterRowAnimations(async()=>{
   for(const [key,result] of saved) if(result.later && Date.now()>=result.later) saved.delete(key);
   content.querySelectorAll('[data-body-loaded="failed"]').forEach(row=>delete row.dataset.bodyLoaded);
   if(view.openItem?.dataset.bodyLoaded==='failed') delete view.openItem.dataset.bodyLoaded;
   view.preserveRecordRowTop(()=>view.apply(false));revealNewItem();await finishDelivery();
   content.dispatchEvent(new Event('review-refreshed'));return version.facts;
  },canApply);
 }
 const fresh=await reviewFetch(location.pathname+'?'+new URLSearchParams({index:'1',version:version.version,'for-you':'1'}),options);
 if(!fresh.ok) throw new Error('HTTP '+fresh.status);
 const payload=await fresh.json();
 return afterRowAnimations(async()=>{
  view.preserveRecordRowTop(()=>applyReviewIndex(payload));
  await finishDelivery();
  return {...payload.facts,answeredAt:version.facts.answeredAt};
 },canApply);
}
function applyReviewIndex(payload){
 focusAfterLoad=rememberFocus();
 if(view.openItem) rememberItem(view.openItem);
 index=readIndex(payload.index);content.dataset.version=payload.version;
 if(payload.presentation){
  content.dataset.zone=payload.presentation.zone;
  content.dataset.later=JSON.stringify(payload.presentation.later);
  content.dataset.titleLimit=payload.presentation.titleLimit;
  content.style.setProperty('--list-log-max-height',payload.presentation.logMaxHeightRem+'rem');
  content.dataset.requestTimeoutSeconds=payload.presentation.requestTimeoutSeconds ?? content.dataset.requestTimeoutSeconds;
  content.dataset.deliveryWindowSeconds=payload.presentation.deliveryWindowSeconds ?? content.dataset.deliveryWindowSeconds;
  content.dataset.cardFadeOutMs=payload.presentation.cardFadeOutMs;
  content.dataset.cardFadeInMs=payload.presentation.cardFadeInMs;
 }
 content.querySelector('[data-review-index]').textContent=JSON.stringify(payload.index);
 const notices=content.querySelector('[data-review-source-notices]');
 notices.replaceChildren(...(payload.notices || []).map(text=>{
  const line=document.createElement('li');showActionFailure(line,text);return line;
 }));
 notices.hidden=!notices.children.length;
 content.querySelector('[data-for-you-notices]').replaceChildren(...(payload.actionNotices || []).map(text=>{
  const line=document.createElement('li');line.textContent=text;return line;
 }));
 for(const [key,body] of bodies) if(body.version!==payload.version) bodies.delete(key);
 const ownerRow=filterRow('owner'),pairs=new Map();
 const focusedOwner=ownerRow.contains(document.activeElement);
 for(const entry of index.values()){
  pairs.set(entry.owner,{name:entry.ownerName,label:colored(entry.ownerName,entry.ownerHtml)});
 }
 for(const entry of index.values()){
  if(isInvestigation(entry)) pairs.set(entry.sourceOwner,{name:entry.sourceName,label:colored(entry.sourceName,entry.sourceHtml)});
 }
 filterControl.updateOwners(ownerRow,new Map([...pairs].sort((a,b)=>a[1].name.localeCompare(b[1].name))));
 for(const [key,result] of saved){
  const entry=index.get(key);
  if(!entry && result.retained && view.opened.has(key)){index.set(key,result.retained);continue;}
  if(!entry || (result.later && Date.now()>=result.later)) {saved.delete(key);continue;}
  // The index supplies the state and update time for each row.
  const submitted=snapshotPrecedesSave(entry,result);
  if(!submitted && (entry.type==='for-you' || entry.unreadAnswer || result.readAnswer)){saved.delete(key);continue;}
  if(!submitted && (result.restored || result.handed || entry.state!==result.state)){saved.delete(key);continue;}
  if(!result.closes && !result.question && !result.later && !submitted) saved.delete(key);
  else result.awaitingSnapshot=submitted;
 }
 storeSavedVerdicts();
 for(const [key,result] of saved) savedEntry(key,result);
 const openKey=view.openItem?.dataset.key;
 if(view.opened.has(openKey) && !index.has(openKey) && !missingDeliveryEntry(openKey)){
  missingCard=true;view.closeCard(openKey);
 }
 bind();
 const openedLink=view.retryItemLink();
 if(!openedLink) view.apply(false);
 if(missingDeliveryEntry(openKey))
  drawSaveControls(recordDetails(view.openItem)?.querySelector('[data-review-form]'));
 if(focusAfterLoad) restoreFocus(focusAfterLoad.key);
 if(focusedOwner && (!ownerRow.contains(document.activeElement) || document.activeElement.hidden))
  ownerRow.querySelector('[aria-pressed="true"]')?.focus({preventScroll:true});
 content.dataset.loaded='true';
 reviewDraftsChanged();
 revealNewItem();
 content.dispatchEvent(new Event('review-refreshed'));
}
bind();
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
const draftArea=document.querySelector('.review-drafts');
const draftList=draftArea.querySelector('ul'),draftToggle=draftArea.querySelector('button');
const bugDialog=document.getElementById('review-bug-dialog'),bugFrame=bugDialog.querySelector('iframe');
const draftRows=new Map();
let bugLink;
bindConsoleDialog(bugDialog,()=>bugDialog.close());
bugDialog.addEventListener('close',()=>{
 bugFrame.removeAttribute('src');
 bugLink?.focus({preventScroll:true});
 bugLink=null;
});
bugFrame.addEventListener('load',()=>bugFrame.contentDocument?.addEventListener('keydown',event=>{
 if(event.key==='Escape' && !event.defaultPrevented && !event.isComposing &&
  !bugFrame.contentDocument.querySelector('dialog[open]')) bugDialog.close();
}));
draftList.addEventListener('click',event=>{
 const link=event.target.closest('a[data-review-bug-link]');
 if(!link || event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey ||
  event.shiftKey || event.altKey) return;
 event.preventDefault();
 bugLink=link;
 bugFrame.src=link.href;
 bugDialog.showModal();
});
let openingDraftKey='';
function setDraftListOpen(open){
 draftList.hidden=!open;
 draftToggle.setAttribute('aria-expanded',String(open));
}
function createDraftRow(key){
 const row=document.createElement('li'),title=document.createElement('strong'),message=document.createElement('p');
 message.className='action-failure';message.setAttribute('role','status');
 const bug=document.createElement('a'),filed=document.createElement('span');
 bug.dataset.reviewBugLink='';
 filed.append(' ',bug,' is filed.');
 message.textContent='Not saved.';
 const label=document.createElement('label'),words=document.createElement('textarea');
 label.textContent='Your words';
 words.readOnly=true;words.rows=3;
 label.append(words);
 const action=document.createElement('button');
 action.type='button';
 action.addEventListener('click',()=>void sendReviewDraft(key));
 row.dataset.draftListKey=key;
 row.append(title,message,label,action);draftList.append(row);
 return {row,title,message,bug,filed,words,action};
}
function renderReviewDrafts(){
 const drafts=[...reviewDrafts].filter(([,record])=>record.edit.draft.feedback);
 draftArea.hidden=!drafts.length;
 if(!drafts.length) setDraftListOpen(false);
 const count='Not saved: '+drafts.length;
 if(draftToggle.textContent!==count) draftToggle.textContent=count;
 for(const [key,entry] of draftRows){
  if(reviewDraftWords(key)) continue;
  entry.row.remove();draftRows.delete(key);
 }
 for(const [key,record] of drafts){
  const entry=draftRows.get(key) ?? createDraftRow(key);
  draftRows.set(key,entry);
  const title=record.title ? record.cardId+' · '+record.title : record.cardId;
  if(entry.title.textContent!==title) entry.title.textContent=title;
  const bug=record.filedBug;
  if(bug){
   if(entry.bug.textContent!==bug.label) entry.bug.textContent=bug.label;
   if(entry.bug.getAttribute('href')!==bug.href) entry.bug.setAttribute('href',bug.href);
   if(!entry.filed.isConnected) entry.message.append(entry.filed);
  }else entry.filed.remove();
  const words=record.edit.draft.feedback;
  if(entry.words.value!==words) entry.words.value=words;
  const action=record.deliveryRetry ? 'Got it' : record.retry?.label || 'Open the card';
  if(entry.action.textContent!==action) entry.action.textContent=action;
  entry.action.disabled=(!record.deliveryRetry && !index.has(key)) || Boolean(openingDraftKey) ||
   pendingSaves.has(key) || recovering.has(key);
 }
}
function requireReviewDraftChoice(key,record){
 delete record.retry;
 setReviewDraftMessage(key,'Not saved: '+reviewDraftChoiceChanged);
}
async function sendReviewDraft(key){
 const record=reviewDrafts.get(key),retry=record?.retry;
 if(!record?.edit.draft.feedback || (!record.deliveryRetry && !index.has(key)) || openingDraftKey ||
  pendingSaves.has(key) || recovering.has(key)) return;
 openingDraftKey=key;
 renderReviewDrafts();
 try{
  if(record.deliveryRetry){await reviewSaveReports.retryDelivery(key,record);return;}
  const session=view.openSession;
  await afterRowAnimations(()=>{
   if(view.openItem?.dataset.key!==key){view.openCard(key);view.apply();}
  },()=>view.openSession===session);
  const openedSession=view.openSession;
  const current=()=>view.openSession===openedSession && view.openItem?.dataset.key===key &&
   reviewDrafts.get(key)===record && record.retry===retry;
  const openForm=()=>recordDetails(view.openItem)?.querySelector('[data-review-form]');
  const needsRevision=()=>retry?.refusedRevision && openForm()?.elements.revision?.value===retry.refusedRevision;
  while(current() && bodyRequests.has(key)) await bodyRequests.get(key);
  if(current() && needsRevision()){
   loadBodies(key);
   while(current() && bodyRequests.has(key)) await bodyRequests.get(key);
  }
  await afterRowAnimations(()=>{
   const form=openForm();
   if(!retry){
    setDraftListOpen(false);
    const words=form?.querySelector('textarea[name="feedback"]');
    (words ?? view.openItem.querySelector('.list-toggle'))?.focus();
    return;
   }
   if(view.openItem.dataset.bodyLoaded!=='true'){
    setReviewDraftMessage(key,'Not saved: The card is unavailable.');
    return;
   }
   const {outcome,until}=retry.action;
   const button=form?.querySelector('button[name="outcome"][value="'+CSS.escape(outcome)+'"]');
   if(!button || !retry.cardType || !retry.cardState ||
    form.dataset.reviewType!==retry.cardType || form.dataset.reviewState!==retry.cardState ||
    form.elements.feedback?.type!=='textarea'){
    requireReviewDraftChoice(key,record);
    return;
   }
   const data=reviewActionData(form,outcome,until);
   data.set('feedback',record.edit.draft.feedback);
   if(!sameReviewAction(retry.action,reviewActionIntent(form,data))){
    requireReviewDraftChoice(key,record);
    return;
   }
   if(needsRevision()) return;
   const submit=reviewFormSubmissions.get(form);
   if(!submit || !view.canSubmit(form,key)){
    setReviewDraftMessage(key,'Not saved: The card is unavailable.');
    return;
   }
   if(outcome!=='later' && button.disabled) return;
   data.set('retry_recipients',JSON.stringify(retry.action.recipients));
   form.elements.feedback.value=data.get('feedback');
   void submit(outcome,data,retry.label);
  },current);
 }finally{
  openingDraftKey='';
  renderReviewDrafts();
 }
}
reviewDraftsChanged=renderReviewDrafts;
draftToggle.addEventListener('click',()=>{
 const open=draftList.hidden;
 setDraftListOpen(open);
 renderReviewDrafts();
 if(open) draftList.focus({preventScroll:true});
});
document.addEventListener('keydown',event=>{
 if(event.key!=='Escape' || event.defaultPrevented || event.isComposing || bugDialog.open || draftList.hidden) return;
 event.preventDefault();
 setDraftListOpen(false);
 draftToggle.focus({preventScroll:true});
});
renderReviewDrafts();
setInterval(updateHandoffAges,5000);
document.addEventListener('review-item-saved',()=>{
 const notice=content.querySelector('[data-review-notice]');
 notice.textContent='';notice.hidden=true;
 newItemRefresh=pageRefresh.refresh(true);
});
document.addEventListener('review-item-created',event=>{
 const {key,id}=event.detail ?? {};
 if(typeof key!=='string' || typeof id!=='string') return;
 revealCreated={key,id};
 revealNewItem();
 void newItemRefresh?.then(()=>{
  if(revealCreated?.key===key) void pageRefresh.refresh(true);
 });
});
function followItemLink(options){
 const applyLink=unappliedOnly=>view.followItemLink({...options,unappliedOnly,beforeJump:key=>{
  if(pendingSaves.has(key)) return false;
  const marker=saved.get(key);
  if(marker && !marker.closes && !marker.question && !marker.handed) saved.delete(key);
  return true;
 }});
 const key=view.hasActiveRowAnimations ? view.itemLinkKey() : null;
 if(!key || !content.querySelector('[data-record-motion-key="'+CSS.escape(key)+'"]'))
  return applyLink(options?.unappliedOnly);
 const href=location.href;
 void afterRowAnimations(()=>applyLink(true),()=>location.href===href);
 return true;
}
if(history.state?.recordTable) view.restorePlace();
else if(!followItemLink()){view.place();view.apply(true,{normalizeOwner:true});}
function isDeferredLink(key){return deferredLink?.key===key && deferredLink.href===location.href;}
function restorePlace(event){
 const key=view.itemLinkKey();
 if(pendingSaves.has(key)){
  deferredLink={key,href:location.href};return;
 }
 deferredLink=null;
 if(event.type==='hashchange' && !history.state?.recordTable && followItemLink({inheritFilters:true})) return;
 view.restorePlace(event);
}
addEventListener('popstate',restorePlace);
addEventListener('hashchange',restorePlace);
window.consoleNavigate=place=>{
 const target=new URL(place,location.href);
 location.hash=recordPlaceParams(reviewPlaceOptions,target).toString();
};
})(legacyReviewPlace);''')
