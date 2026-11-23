"""Show Review items from their native stores in one table."""
from __future__ import annotations

import html
import hashlib
import gzip
import threading
import logging
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
import json
import re
import secrets
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import NotRequired, TypedDict, cast
from urllib.parse import parse_qs, parse_qsl, urlencode
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.for_you.action import ActionStore, ActionStoreError, REVIEW_PATH
from scripts.lib.user_zone import CONF as USER_ZONE_CONF, user_zone
from scripts.production import change_review, change_routing, proposal_issue, staff, update_registration
from scripts.production.record_store import FileStamp, state_root
from scripts.lib import urgency
from scripts.review_links import change_browser, change_page, console, for_you, page_frame, page_version, record_table
from scripts.review_links import review_items, review_link
from scripts.review_links import new_item_dialog, reply_tokens, review_submissions
from scripts.review_links import question_record, review_actions, review_events, staff_names
from scripts.review_links.review_items import ReviewItem, ReviewSourceStamps, ReviewState, ReviewType, guard_source
from scripts.review_links.review_items import source_stamp as source_stamp

PAGE_PATH = REVIEW_PATH
NEW_ITEM_PATH = '/console/review/new-item'
ATTACHMENT_PATH = '/console/review/attachment'
HEADERS = ('Id', 'Type', 'Owner', 'Item', 'State', 'Age')
CARD_COLUMNS = ('<colgroup><col style="width:17%"><col style="width:13%"><col style="width:14%">'
                '<col style="width:37%"><col style="width:12%"><col style="width:7%"></colgroup>')
TYPES = tuple(ReviewType)
WIRE_TYPES = tuple(kind for kind in TYPES if kind != ReviewType.RESPONSE)
TYPE_CHIPS = (ReviewType.FOR_YOU, *review_items.QUESTION_TYPES,
              *(kind for kind in TYPES if kind not in (*review_items.QUESTION_TYPES, ReviewType.FOR_YOU)))
ICONS = {ReviewType.QUESTION: 'circle-help', ReviewType.CHANGE: 'diff', ReviewType.INVESTIGATION: 'microscope',
         ReviewType.PROPOSAL: 'lamp', ReviewType.EFFICIENCY: 'zap', ReviewType.BUG: 'bug', ReviewType.RESPONSE: 'inbox',
         ReviewType.FOR_YOU: 'inbox'}
INDEX_STATES = (ReviewState.OPEN, ReviewState.WAITING, ReviewState.VALIDATING, ReviewState.DONE)
SHOWS = (('all', 'All'), ('open', 'Open'), (ReviewState.WAITING, 'Waiting'), (ReviewState.DONE, 'Done'),
         (ReviewState.VALIDATING, 'Validating'))
LABELS = {ReviewType.QUESTION: 'Questions', ReviewType.CHANGE: 'Changes', ReviewType.INVESTIGATION: 'Investigations',
          ReviewType.PROPOSAL: 'Proposals', ReviewType.EFFICIENCY: 'Efficiencies', ReviewType.BUG: 'Bugs',
          ReviewType.RESPONSE: 'Responses', ReviewType.FOR_YOU: 'For you'}
LATER_PRESETS = (('soon', ''), ('evening', 'This evening'), ('morning', 'Tomorrow morning'), ('week', 'Next week'))
# Weekday names in the order of the browser's getUTCDay.
WEEKDAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
FRONT_MATTER = re.compile(r'\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)
CARD_ERROR = b'<p role="status" data-review-card-unavailable>This card is not available.</p>'


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
    return [card_entry(item, members, names) for item in records
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
             *([None, None, False, entry['actionStatus']] if 'actionStatus' in entry
               else [entry.get('handoff'), entry.get('waitingWhy'), True] if entry['type'] == ReviewType.RESPONSE
               else [entry.get('handoff'), entry['waitingWhy']] if 'waitingWhy' in entry
               else [entry['handoff']] if 'handoff' in entry else [])] for entry in entries]
    return json.dumps({'n':list(names),'r':rows},ensure_ascii=False,separators=(',',':')).replace(
        '&','\\u0026').replace('<','\\u003c').replace('>','\\u003e')


def later_settings(settings: conf.ConfigRead) -> dict[str, int]:
    """Return the Later menu's preset settings for the page script, in minutes and getUTCDay numbers."""
    day = settings.text('later_week_day')
    if day not in WEEKDAYS:
        raise ValueError(f'review_links.conf: later_week_day={day!r} is not a weekday name such as Monday')
    return {'soon': settings.whole('later_soon_minutes'), 'evening': record_table.clock_minutes(settings, 'later_evening'),
            'eveningUntil': record_table.clock_minutes(settings, 'later_evening_until'), 'morning': record_table.clock_minutes(settings, 'later_morning'),
            'weekDay': WEEKDAYS.index(day), 'weekTime': record_table.clock_minutes(settings, 'later_week_time')}


def soon_label(minutes: int) -> str:
    count, unit = (minutes//60, 'hour') if minutes % 60 == 0 else (minutes, 'minute')
    return f'In {count} {unit}'+('s' if count > 1 else '')


@dataclass(frozen=True)
class ReviewBodyInputs:
    """Keep the data required to render a card without reading its store."""
    browser: change_page.ReviewBrowser
    members: staff.StaffSnapshot
    words_rows: int
    soon_label: str
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
    delivery_window_seconds: int = record_table.DELIVERY_WINDOW_SECONDS
    submission_keys: dict[str, str] = field(default_factory=dict)

    def facts(self) -> dict[str,float]:
        return {'readAt':self.read_at,'renderSeconds':self.render_seconds,'answeredAt':time.time()}

    def index_response(self) -> bytes:
        return self.full_index.body

    def render(self, *, show: str = 'open') -> bytes:
        selected = next((value for value, _ in SHOWS if value.lower() == show.lower()), 'open')
        page = self.page.replace(b'data-show="open"', ('data-show="'+selected+'"').encode(), 1)
        if selected != 'open':
            page = page.replace(b'data-show-chip="open" aria-pressed="true"',b'data-show-chip="open" aria-pressed="false"',1)
            page = page.replace(('data-show-chip="'+selected+'" aria-pressed="false"').encode(),
                                ('data-show-chip="'+selected+'" aria-pressed="true"').encode(),1)
        return page

    def body(self, key: str, *, view: ReviewBodyView = ReviewBodyView.TABLE) -> bytes:
        """Render one retained record without a store read."""
        item = self.records.get(key)
        if item is None:
            raise LookupError('This item is not in the current snapshot.')
        body = self.render_body(item, self.assets)
        if view is ReviewBodyView.TABLE:
            return body
        entry = next(entry for entry in self.entries if entry['key'] == key)
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
        entry = card_entry(item, self.members, self.names)
        return render_card(self, entry, body.decode(), waiting_why=item.waiting_why)

    def render_body(self, item: ReviewItem, assets: Mapping[str, str]) -> bytes:
        body = CARD_ERROR
        if item.key not in self.unavailable_cards:
            try:
                body = details(item, ReviewBodyInputs(self.browser, self.members, self.words_rows,
                    soon_label(self.later.get("soon", 60)), assets, self.zone, self.names)).encode()
            except Exception as error:
                logging.getLogger(__name__).warning('Review card %s: %s', item.key, error)
        return body


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


def retain_record_assets(item: ReviewItem, status: change_page.DeliveryStatus,
                         sources: ReviewSourceStamps) -> dict[str, str]:
    """Keep the assets and delivery notice for one Review record."""
    assets: dict[str, str] = {}
    retain_asset(item, assets)
    retain_question_delivery(item, assets, status, sources)
    return assets


def submission_assets(key: str, origin: new_item_dialog.ReviewSubmission) -> str:
    """Render the saved evidence and references for one lazy card."""
    evidence = origin.evidence
    body = ''
    images = origin.images
    if isinstance(evidence, new_item_dialog.BuildStepEvidence):
        cells = ''.join('<div><dt>'+html.escape(cell.label)+'</dt><dd>'+html.escape(cell.value)+'</dd></div>'
                        for cell in evidence.cells)
        memory = evidence.memory
        segments = ' · '.join(f'{part.label} {part.gib:g} GiB' for part in memory.segments)
        tip = ''.join('<div><dd>'+html.escape(part.text)+'</dd></div>'
                      if isinstance(part, (new_item_dialog.TipDetail, new_item_dialog.TipCaption))
                      else '<div><dt>'+html.escape(part.label)+'</dt><dd>'+html.escape(part.value)+'</dd></div>'
                      for part in evidence.tip)
        body = ('<section data-submission-evidence><h3>Evidence</h3><strong>'+html.escape(evidence.title)
                +'</strong><p>'+html.escape(evidence.owner)+' · '+html.escape(evidence.captured_at)
                +'</p><dl>'+cells+'</dl><p>'+f'{memory.free_for_next_gib:g} GiB free for the next step · '
                +f'Queue cap {memory.queue_cap_gib:g} GiB</p><p>'+html.escape(segments)
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
    """Show the state from the Bug and its screenshot thumbnail."""
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
        figure = (f'<figure><a href="{url}" target="_blank" rel="noopener">'
                  f'<img src="{url}" loading="lazy" alt="{caption}"></a><figcaption>{caption}</figcaption></figure>')
    return ('<section class="page-state-box" data-submission-page-state><h4>Page state</h4>'
            f'<dl class="page-state">{fields}</dl>{figure}</section>')


def card_entry(item: ReviewItem, members: staff.StaffSnapshot, names: staff_names.StaffNames) -> ReviewIndexEntry:
    owner = owner_name(item, members)
    source = review_items.name(item.source_unit, members)
    entry = ReviewIndexEntry(key=item.key, id=item.id, type=item.type, owner=str(item.owner),
        sourceOwner=str(item.source_unit), ownerName=owner, title=item.title, state=item.state,
        filed=int(item.times.filed.timestamp()), updated=item.times.updated.isoformat(),
        hiddenUntil=item.times.later.timestamp()*1000 if isinstance(item.times.later, datetime) else 0,
        sourceName=source, urgency=item.urgency.level, ownerHtml=names.html(item.owner, owner),
        sourceHtml=names.html(item.source_unit, source))
    if isinstance(item.source, review_items.ChangeSource) and item.source.handed_at is not None:
        requested = item.source.handed_at.astimezone(user_zone())
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
    members = staff.snapshot()
    names = staff_names.StaffNames(members)
    sources.extend(urgency.state_dir().glob('*.json'))
    records: dict[str, ReviewItem] = {}
    entries: list[ReviewIndexEntry] = []
    assets: dict[str, str] = {}
    submission_keys: dict[str, str] = {}
    unavailable_cards: set[str] = set()
    status = change_page.DeliveryStatus(root)
    items = review_items.read(root, members=members, show='active', sources=sources)
    sources.extend(for_you.source_paths())
    actions = for_you.read(members=members)
    items.extend(review_items.for_you_item(row, members) for row in actions.rows)
    for item in items:
        path = review_items.source_path(item)
        with guard_source(path, sources, entry=item.id):
            item = review_items.normalize_times(item, zone)
            try:
                record_assets = retain_record_assets(item, status, sources)
                validate_body_inputs(item, record_assets)
                summary = card_entry(item, members, names)
            except (UnicodeError, TypeError, ValueError, OSError, RuntimeError) as error:
                sources.skipped(path, item.id, error)
                unavailable_cards.add(item.key)
                summary = card_entry(replace(item, title='Card unavailable'), members, names)
            else:
                assets.update(record_assets)
                origin = review_items.submission_origin(item)
                if isinstance(origin, new_item_dialog.ReviewSubmission):
                    submission_keys[origin.request_id] = item.key
            entries.append(summary)
            records[item.key] = item
    browser = change_page.ReviewBrowser(token, initial)
    settings = conf.read('review_links')
    words_rows = settings.whole('review_words_rows')
    refresh_seconds = settings.number('page_refresh_seconds')
    request_timeout = settings.whole('page_request_timeout_seconds')
    delivery_window = record_table.delivery_window_seconds(settings)
    card_motion = page_frame.CardMotion.read(settings)
    later = later_settings(settings)
    title_limit = conf.read_or_repository('production').whole('question_title_max_chars')
    log_max_height = record_table.log_max_height_rem(settings)
    index = index_json(entries)
    notices = tuple(sources.notices.values())
    action_notices = tuple(review_items.utf8_display(f'{failure.source}: {failure.reason}') for failure in actions.unavailable)
    retained = repr((records, assets, members.rows, browser, words_rows, refresh_seconds, request_timeout,
                     card_motion, zone.key, notices, action_notices, unavailable_cards, later, title_limit, delivery_window, names.colors,
                     log_max_height))
    version = hashlib.sha256((index+retained).encode()).hexdigest()[:20]
    snapshot = ReviewSnapshot(version,records,entries,members,
        browser,words_rows,refresh_seconds,request_timeout,card_motion,assets,zone,sources.before_read,later,names,log_max_height,read_at=read_at,
        notices=notices, action_notices=action_notices, unavailable_cards=frozenset(unavailable_cards),
        title_limit=title_limit, delivery_window_seconds=delivery_window, submission_keys=submission_keys)
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
class ReviewSave:
    """The saved record, its published version and any message to deliver."""
    delivery: change_page.MessageDelivery | change_page.NoMessageDelivery
    answer: str
    version: str
    item: ReviewItem | ReviewRecordNotPublished
    detail: ReviewActionDetail = field(default_factory=ReviewDetailUnavailable)


@dataclass(frozen=True)
class PublishedSubmission:
    """A native Send in the current Review index."""
    saved: review_submissions.SavedSubmission
    key: str
    version: str


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
        if not self.initialize_on_start and (self.root/change_page.CONSOLE_FOLDER/change_browser.MANIFEST).exists():
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

    def save(self, body: str, status: change_page.DeliveryStatus) -> ReviewSave:
        """Serialize Review writes and publish the saved record before returning."""
        _ = self.get()
        fields = review_fields(body)
        key = fields['row'][0]
        with self._save_lock:
            current = self.get()
            try:
                delivery, answer = submit_snapshot(self.root, current, fields, status)
            except review_items.ReviewRecordChanged:
                try:
                    if key in current.records:
                        _ = self.publish_record(current, key)
                except Exception as error:
                    logging.getLogger(__name__).warning('Review conflict publication for %s: %s', key, error)
                finally:
                    self.invalidate()
                raise
            version = current.version
            detail: ReviewActionDetail = ReviewDetailUnavailable()
            item: ReviewItem | ReviewRecordNotPublished = ReviewRecordNotPublished()
            try:
                updated, item = self.publish_record(current, key)
                version = updated.version
                if not isinstance(item, ReviewItem):
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
            return ReviewSave(delivery, answer, version, item, detail)

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
        refusal: new_item_dialog.SubmissionError | None = None,
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
            with staff.supplied_snapshot(members) if remote_staff else nullcontext():
                saved = review_submissions.submit(request, members=members, screenshot=screenshot,
                                                  capture_requested=capture_requested, refusal=refusal,
                                                  capture_intent=capture_intent, capture_target=capture_target,
                                                  initial_delivery=initial_delivery)
            if not remote_staff:
                members = staff.snapshot()
            path = saved.path
            stamps = {source: source_stamp(source) for source in (path, path.parent)}
            try:
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
            return PublishedSubmission(saved, item.key, fresh.version)

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
        if isinstance(item, ReviewRecordNotPublished) or (item.state == ReviewState.DONE and item.type != ReviewType.FOR_YOU):
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
            updated_entry = card_entry(item, members, names)
            entries = [updated_entry if entry['key'] == previous_key else entry for entry in current.entries
                       if entry['key'] != key or key == previous_key]
            if previous_key not in current.records:
                entries.append(updated_entry)
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
    now = datetime.now(user_zone())
    records = snapshot.records.values() if snapshot is not None else request.shared(review_items.read_open)
    return frozenset(item.key+'/'+item.times.updated.isoformat() for item in records
                     if item.type != ReviewType.FOR_YOU and item.for_you(now))


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
    options = ''.join(f'<option value="{html.escape(member.id, quote=True)}"{names.style(member.id)}>'
                      +f'{html.escape(member.name)}</option>'
                      for member in review_actions.approval_assignees(names.members))
    return page_frame.action_select('assignee', 'Assign', f'<option value="">{html.escape(shown)} decides</option>'+options)


def form(item: ReviewItem, browser: change_page.ReviewBrowser, words_rows: int, revision: str, *,
         soon: str = "In 1 hour", owner: str = '', validator: str = '', assignment: str = '') -> str:
    if item.state == ReviewState.DONE or (item.type == ReviewType.FOR_YOU and not item.actions):
        return ''
    fields = {'row': item.key, 'token': change_browser.row_token(browser, item.key), 'revision': revision}
    hidden = ''.join(f'<input type="hidden" name="{key}" value="{html.escape(value, quote=True)}">' for key, value in fields.items())
    closes: list[str] = []
    returns: list[str] = []
    labels = {'answer': 'Answer', 'works': 'Works', 'not works': 'Does not work', 'validate': 'Ask showrunner to check',
              'question': 'Ask question', 'feedback': 'Send feedback', 'read': 'Mark read',
              'approve': 'Approve', 'decline': 'Decline', 'done': 'Done'}
    if item.type == ReviewType.RESPONSE:
        if not item.actions:
            return ''
        labels.update(read='Got it', question='Ask a follow-up')
    elif validator:
        labels['question'] = 'Ask a question'
    for index, action in enumerate(item.actions):
        button = f'<button name="outcome" value="{action}"'+(' class="primary"' if index == 0 and not validator else '')+'>'+labels[action]+'</button>'
        (returns if action == 'question' or (item.type == ReviewType.CHANGE and action in change_page.CHANGE_HANDOFFS)
         else closes).append(button)
        if action == 'approve':
            closes.append(assignment)
    heading = (page_frame.ReturningActionHeading.SENDS_TO_SHOWRUNNER if item.type == ReviewType.CHANGE
               else page_frame.ReturningActionHeading.ITEM_RETURNS)
    later = ''
    if (item.state not in (ReviewState.DONE, ReviewState.VALIDATING)
            and item.type not in (ReviewType.RESPONSE, ReviewType.FOR_YOU) and not waiting_change(item)):
        presets = ''.join(f'<button type="button" role="menuitem" tabindex="-1" data-later-preset="{key}">'
                          +f'<span>{html.escape(label or soon)}</span><small></small></button>' for key, label in LATER_PRESETS)
        later = ('<button type="button" aria-haspopup="menu" aria-expanded="false" data-later-toggle>Later <span aria-hidden="true">▾</span></button>'
                 +'<div role="menu" aria-label="Hide until" data-later-menu hidden>'+presets+'<hr>'
                 +'<button type="button" role="menuitem" tabindex="-1" data-later-choose><span>Choose a time…</span></button>'
                 +record_table.date_time_picker(field='later', action='outcome', value='later')+'</div>')
    words = ''
    if item.type == ReviewType.FOR_YOU:
        words = '<input type="hidden" name="feedback" value="">'
    elif closes or returns or later:
        field = 'words-'+html.escape(item.key, quote=True)
        if item.type == ReviewType.RESPONSE:
            placeholder = 'Type your words for Ask a follow-up.'
        elif validator:
            placeholder = 'Ask the validator about this item.'
        else:
            placeholder = 'Type your words. They go with the button that you press.'
        words = (f'<label for="{field}">Your words</label><textarea id="{field}" name="feedback" rows="{words_rows}" '
                 + f'placeholder="{placeholder}"></textarea>')
    if item.type == ReviewType.RESPONSE:
        groups = ('<div class="review-actions"><div class="review-action-group"><span>Closes the item</span><div>'
                  +''.join(closes)+'</div></div><div class="review-action-group"><span>Sends to '
                  +html.escape(owner)+'</span><div>'+''.join(returns)+'</div></div></div>')
    elif validator:
        groups = ('<div class="review-actions"><div class="review-action-group"><span>Sends to '
                  +html.escape(validator)+'</span><div>'+''.join(returns)+'</div></div></div>')
    else:
        groups = page_frame.review_action_groups(''.join(closes), ''.join(returns), later, returning_heading=heading)
    return ('<form method="post" data-review-form>'+hidden+words+groups
            +'<span role="status" data-send-status></span></form>')


def log_html(item: ReviewItem, inputs: ReviewBodyInputs, owner: str) -> str:
    rows: list[str] = []
    response = item.response
    for index, entry in enumerate(item.log):
        time = entry.time.isoformat()
        words = ('Answered' if response is not None and entry.time == response.answered_at
                 and entry.who == item.owner and entry.words == 'Answer: '+response.answer else entry.words)
        actor = inputs.names.html(entry.who, owner if response is not None and entry.who == item.owner else entry.who)
        rows.append(f'<li data-log-index="{index}"><time datetime="{time}">'
                    +entry.time.astimezone(inputs.zone).strftime('%b %d %H:%M')+'</time><span>'
                    +html.escape(words)+' <small>— '+actor+'</small></span></li>')
    return ''.join(reversed(rows))


def for_you_details(item: ReviewItem, row: for_you.ForYouRow, inputs: ReviewBodyInputs) -> str:
    """Render the action in the shared Review card."""
    requester = row.requested_by or 'Not recorded'
    fields = (
        ('Machine', html.escape(row.machine)),
        ('Working directory', html.escape(row.cwd or 'Not recorded')),
        ('Requester', inputs.names.html(item.owner, requester)),
        ('Waiting work', html.escape(row.waits)),
        ('Completion', 'Automatic after rebuild' if row.completion == 'automatic' else 'Manual'),
    )
    metadata = '<dl class="page-state">'+''.join(f'<dt>{label}</dt><dd>{text}</dd>' for label, text in fields)+'</dl>'
    reason = section('Reason', row.reason) if row.reason else ''
    evidence = section('Withdrawal reason' if row.status == 'withdrawn' else 'Evidence', row.evidence) if row.evidence else ''
    link = '<p><a href="/console/?page=rebuilds" target="_top">Rebuild details</a></p>' if row.kind == 'rebuild' else ''
    return ('<div class="list-panel review-panel for-you"><div data-review-body>'
            +page_frame.copy_code_block(row.command)+reason+evidence+link+'</div><div>'+metadata+'</div>'
            +form(item, inputs.browser, inputs.words_rows, record_revision(item, inputs.assets))+'</div>')


def details(item: ReviewItem, inputs: ReviewBodyInputs) -> str:
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
            handoff = '<p class="review-handoff">With '+names.html(item.owner, validator)+duration+'</p>'
            if isinstance(question, (change_page.OpenQuestion, change_page.AnsweredQuestion)) and not isinstance(
                    question, (change_page.OpenFeedback, change_page.CompletedFeedback)):
                escaped = html.escape(validator)
                exchange = section('Question for '+escaped, question.text)
                if isinstance(question, change_page.AnsweredQuestion):
                    exchange += ('<section><h3>Answer from '+escaped+'</h3>'
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
            +form(item, inputs.browser, inputs.words_rows, record_revision(item, inputs.assets), soon=inputs.soon_label,
                  owner=owner, validator=validator,
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
               + '<script>' + record_table.DATE_TIME_SCRIPT + page_frame.MOTION_SCRIPT
               + page_frame.CARD_SUBMIT_SCRIPT + page_frame.SAVE_TIMEOUT_SCRIPT + record_table.DETAIL_SCRIPT + REVIEW_CARD_SCRIPT
               + CARD_VIEW_SCRIPT + '</script>')
    return change_page.document(content, 'Review card')


def render_shell(snapshot: ReviewSnapshot, index: str, started: float, read_at: float) -> bytes:
    """Build the controls and compact index without any card body."""
    show = SHELL_SHOW
    entries = snapshot.entries
    owners = {entry['owner']:(entry['ownerName'], entry['ownerHtml']) for entry in entries}
    owners.update({entry['sourceOwner']:(entry['sourceName'], entry['sourceHtml'])
                   for entry in entries if entry['type'] in review_items.INVESTIGATION_TYPES})
    owner_chips = '<button id="review-owner-all" type="button" data-owner-chip="" aria-pressed="true">All <b></b></button>'
    for owner, (name, markup) in sorted(owners.items(), key=lambda pair: pair[1][0].casefold()):
        identity, label = html.escape(owner, quote=True), html.escape(name, quote=True)
        owner_chips += (f'<button id="review-owner-{identity}" type="button" data-owner-chip="{identity}" '
                        +f'data-owner-name="{label}" aria-pressed="false">{markup or label} <b></b></button>')
    content = (record_table.STYLE+record_table.DATE_TIME_STYLE+STYLE
        +f'<div data-review data-version="{snapshot.version}" data-show="{show}" data-refresh-seconds="{snapshot.refresh_seconds}" '
        +f'data-request-timeout-seconds="{snapshot.request_timeout_seconds}" '
        +f'data-delivery-window-seconds="{snapshot.delivery_window_seconds}" '
        +snapshot.card_motion.attributes()+' '
        +f'data-title-limit="{snapshot.title_limit}" data-zone="{html.escape(snapshot.zone.key, quote=True)}" '
        +f'data-later="{html.escape(json.dumps(dict(snapshot.later)), quote=True)}" '
        +f'style="--list-log-max-height:{snapshot.log_max_height_rem}rem">'
        +page_frame.title('Review', 'review', SCRIPT_VERSION)
        +SHELL_FILTERS
        +record_table.labelled_chip_row('Owner', owner_chips, chip_classes='review-types', chip_id='list-owner')
        +SHELL_NOTICES
        +'<ul data-review-source-notices'+('' if snapshot.notices else ' hidden')+'>'
        +''.join('<li>'+html.escape(notice)+'</li>' for notice in snapshot.notices)+'</ul>'
        +'<ul data-for-you-notices hidden>'
        +''.join('<li>'+html.escape(notice)+'</li>' for notice in snapshot.action_notices)+'</ul>'
        +'<script type="application/json" data-review-index>'+index+'</script><script>'+CARD_REQUEST_SCRIPT+'</script>'
        +SHELL_TABLE+record_table.render_inline_detail()+'<script>'+SCRIPT+'</script>')
    return page_frame.rendered(change_page.document(content, 'Review'), started, read_at)


def review_fields(body: str) -> dict[str, list[str]]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    required = {'row', 'token', 'outcome', 'feedback'}
    if not required <= fields.keys() <= required | {'later', 'revision', 'assignee'} or any(len(values)!=1 for values in fields.values()):
        raise question_record.AnswerRefused('use the answer form on this page')
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
    reply = new_item_dialog.read_submission(fields['review_submission'])
    path = source.record.path
    page = status.metadata_page(change_page.ReviewPage(path, path.parent, (path.stem,), ''))
    return (isinstance(page, change_page.UserQuestionPage) and page.saved.get('kind') == 'question'
            and reply_tokens.matches_reference(reply.reply_token_sha256, page.saved.get('question_id', ''))
            and page.saved.get('question_text') == pending['text'])


def submit_response(item: ReviewItem, outcome: str, words: str, status: change_page.DeliveryStatus,
                    verify_revision: Callable[[], ReviewItem]
                    ) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    source = item.source
    if outcome not in item.actions or not isinstance(source, review_items.QuestionSource):
        raise question_record.AnswerRefused('Choose an action on this card.')
    scope = review_submissions.sent_question_scope(item.owner, staff.snapshot())
    if outcome == 'read':
        with update_registration.outstanding_lock(update_registration.outstanding_path(scope)):
            _ = verify_revision()
            update_registration.review_sent_question(source.record, scope, 'read', '')
        return change_page.NoMessageDelivery(), 'got it'
    words = new_item_dialog.read_words(words, 'Your words')
    delivery, _ = review_actions.sent_question_action(item, source.record, scope, 'question', words, status,
                                                      verify_revision=verify_revision)
    return delivery, 'follow-up sent'


def submit_snapshot(root: Path, snapshot: ReviewSnapshot, fields: dict[str, list[str]],
                    status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    key, token, outcome, words = (fields[field][0] for field in ('row', 'token', 'outcome', 'feedback'))
    if not secrets.compare_digest(token, change_browser.row_token(snapshot.browser, key)):
        raise question_record.AnswerRefused('row or page token does not match this page')
    retained = snapshot.records.get(key)
    if retained is None:
        raise review_items.ReviewRecordMissing('This item is no longer on the list.')

    def read_current() -> ReviewItem:
        return review_items.read_current(root, retained, staff.snapshot())

    def validate_revision(current: ReviewItem) -> ReviewItem:
        if current.state == ReviewState.DONE:
            raise review_items.ReviewRecordChanged('This item is already Done.')
        assets: dict[str, str] = {}
        try:
            retain_asset(current, assets)
        except FileNotFoundError:
            raise review_items.ReviewRecordMissing('This item is no longer on the list.') from None
        revision = fields.get('revision', [''])[0]
        if revision != record_revision(current, assets) and not (
                outcome == 'feedback' and _matches_feedback_retry(current, assets, revision, token, words)):
            raise review_items.ReviewRecordChanged('This item changed. Check it again before saving.')
        return current

    def validate() -> ReviewItem:
        return validate_revision(read_current())

    if retained.type == ReviewType.FOR_YOU:
        item = read_current()
        source = item.source
        if (outcome != 'done' or not isinstance(source, review_items.ForYouSource)
                or source.row.kind != 'action' or source.row.completion != 'manual'):
            raise question_record.AnswerRefused('Choose an action on this card.')
        if source.row.status == 'done':
            return change_page.NoMessageDelivery(), 'Done'
        _ = validate_revision(item)
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
        return change_page.NoMessageDelivery(), 'Done'

    item = validate()
    if item.type == ReviewType.RESPONSE:
        return submit_response(item, outcome, words, status, validate)
    if waiting_change(item) or (item.state == ReviewState.VALIDATING and outcome != 'question'):
        raise change_page.ChangeHandoffRefused(change_page.WITH_SHOWRUNNER)
    if outcome == 'later':
        return change_page.NoMessageDelivery(), review_actions.later(
            item, fields.get('later', [''])[0], words, verify_revision=validate, root=root)
    if outcome not in item.actions:
        raise question_record.AnswerRefused('choose an action on this card')
    source = item.source
    if isinstance(source, review_items.ChangeSource):
        with change_review.checked_review_write(source.path, root, validate) as current:
            action = change_page.VALIDATION_QUESTION if current.state == ReviewState.VALIDATING else outcome
            delivery = change_browser.submit_review_change(current, snapshot.browser, action, words, status)
        if isinstance(delivery, change_page.MessageDelivery) and outcome not in ('validate', 'feedback'):
            label = outcome
            with review_items.after_store_write(item.key):
                change_review.record_review_log(delivery.page.review_list, delivery.row,
                                                label+(': '+words if words else ''), delivery.page.production)
        return delivery, {'validate':'asked the showrunner to check', 'question':'question saved',
                          'feedback':'feedback sent'}.get(outcome, outcome)
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
        delivery, answer = review_actions.investigation_action(item, outcome, words, status, verify_revision=validate)
    else:
        delivery, answer = review_actions.decision_action(
            item, outcome, words, status, assignee=fields.get('assignee', [''])[0], verify_revision=validate)
    if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery) and item.type == ReviewType.QUESTION:
        with review_items.after_store_write(item.key):
            delivery = review_actions.track_question(delivery, key, words, status)
        answer = 'question sent'
    return delivery, 'feedback sent' if outcome == 'feedback' else answer


def submit(root: Path, body: str, status: change_page.DeliveryStatus, *,
           snapshot: ReviewSnapshot | None = None) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    fields = review_fields(body)
    if snapshot is not None:
        return submit_snapshot(root, snapshot, fields, status)
    browser = change_page.ReviewBrowser(*change_browser.browser_manifest(change_page.console_folder(root)))
    key = fields['row'][0]
    if not secrets.compare_digest(fields['token'][0],change_browser.row_token(browser,key)):
        raise question_record.AnswerRefused('row or page token does not match this page')
    outcome, words = fields['outcome'][0], fields['feedback'][0]
    if key.startswith('question/'):
        question_item = next((item for item in review_items.read(root, kinds=review_items.QUESTION_TYPES)
                              if item.key == key), None)
        if question_item is not None and repeats_response_followup(question_item, outcome, words, status):
            raise review_items.ReviewRecordChanged('This item changed. Check it again before saving.')
        if question_item is not None and question_item.type == ReviewType.RESPONSE:
            def validate_response() -> ReviewItem:
                current = review_items.read_current(root, question_item, staff.snapshot())
                if current != question_item:
                    raise review_items.ReviewRecordChanged('This item changed. Check it again before saving.')
                return current

            return submit_response(question_item, outcome, words, status, validate_response)
    else:
        question_item = None
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
                    return change_page.NoMessageDelivery(),review_actions.later(item,fields.get('later',[''])[0],words)
            return change_page.NoMessageDelivery(),review_actions.later(item,fields.get('later',[''])[0],words)
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
        if outcome not in ('validate', 'feedback'):
            words = fields['feedback'][0]
            log = outcome + (': ' + words if words else '')
            change_review.record_review_log(delivery.page.review_list, delivery.row, log, delivery.page.production)
    if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery) and not isinstance(
            delivery.page, (change_page.UserQuestionPage, change_page.ReviewMessagePage)):
        return delivery, 'question saved'
    return delivery, ('feedback sent' if outcome == 'feedback' else answer or
                     {'validate':'asked the showrunner to check', 'question':'question sent'}.get(outcome,'answer'))


QUESTION_COLOR_RULE = '.question{--type-color:#b99aff}'
STYLE = '<style>' + QUESTION_COLOR_RULE + '''
main:has(>[data-review]){max-width:1124px}
[data-review]{display:flex;flex-wrap:wrap;align-items:center;column-gap:16px}
[data-review]>:is(.page-title,.page-heading,.review-queue){flex:0 0 100%;min-width:0}
[data-review]>:is(.page-status,.record-filter-words){margin:0}
[data-review]>.page-status{flex:1 1 0;min-width:0}
[data-review]>.page-status>[data-status-reason]{white-space:normal;overflow-wrap:anywhere;min-width:0}
[data-review]>.record-filter-words{flex:0 0 300px}
.review-queue{margin-top:12px}
.review-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.review-waiting-why{display:block;color:var(--ink-3);font-size:.85em}
.review-attachments img{max-width:min(240px,100%);max-height:160px;object-fit:contain;margin-inline-end:8px}
[data-review] .record-filter-row{margin-bottom:12px}
[data-review] .list-filters:has(>[data-clear-linked-reviews][hidden]){display:none}
.record-filter-row>.review-types{gap:8px}
.review-types button{max-width:100%;overflow-wrap:anywhere}
:is(.review-types button,[data-clear-linked-reviews]){display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;min-height:36px;
background:var(--card);border:1px solid var(--line);border-radius:999px;color:var(--ink-2);padding:5px 14px;margin:0}
:is(.review-types button,[data-clear-linked-reviews])[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
#review-show-validating[data-empty-count]{opacity:.55}
.review-icon{display:inline-flex;vertical-align:middle;margin-right:5px}.review-icon svg{width:18px;height:18px}
.change{--type-color:#62b7ef}.investigation{--type-color:#30c1ad}
.proposal{--type-color:#f27ab8}.efficiency{--type-color:#a3d63a}.bug{--type-color:#ed876b}.response,.for-you{--type-color:#edc46d}
.review-panel.for-you dd{overflow-wrap:anywhere}
.review-icon{color:var(--type-color)}.review-panel{border-left-color:var(--type-color);grid-template-columns:1.25fr 1fr;padding:0 20px 18px 58px;gap:4px 28px}
.response-answer time{display:block;margin-bottom:8px;color:var(--ink-3);font-size:12px}
.review-log-column{display:flex;flex-direction:column;min-width:0}.review-log-column>.list-log-panel{flex:1 0 0}
.review-log-column>.list-log-panel:not(:last-child){min-height:8rem}
.review-panel [data-review-form]{grid-column:1/-1;border-top:1px solid var(--line);padding-top:12px;margin-top:10px}
[data-review-form] label{display:block;font-weight:600;margin-bottom:6px}
[data-review-form] textarea{display:block;box-sizing:border-box;width:100%;font:inherit;padding:9px 12px;background:var(--bg);
color:var(--ink);border:1px solid var(--line);border-radius:8px;resize:vertical}
.review-actions{margin-top:12px}
.review-later{margin-left:auto}
.list-log span{white-space:pre-wrap}
.review-actions [data-later-menu] [role=menuitem]{display:flex;justify-content:space-between;align-items:center;gap:24px;width:100%;
min-height:32px;padding:5px 10px;border:0;border-radius:6px;background:none;color:var(--ink);text-align:left}
.review-actions [data-later-menu] [role=menuitem]:focus{background:var(--accent);color:#fff;outline:none}
[data-later-menu] small{font-size:12px;color:var(--ink-3)}
[data-later-menu] [role=menuitem]:focus small{color:inherit;opacity:.85}
[data-later-menu] hr{border:0;border-top:1px solid var(--line);margin:5px 4px}
[data-later-menu][data-choosing]>:is([role=menuitem],hr){display:none}
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
@media(max-width:800px){[data-review]>:is(.page-status,.record-filter-words){flex-basis:100%}
[data-review]>.record-filter-words{margin-top:8px}
[data-review]>.record-filter-words input{width:100%}}
@media(max-width:480px){[data-review] .record-filter-row{grid-template-columns:minmax(0,1fr)}}
</style>'''

CARD_VIEW_STYLE = '''<style>
:root:has([data-review-card]){overflow:hidden;scrollbar-gutter:auto}
:root:not(.layout-measuring) main:has(>[data-review-card]){max-width:none;padding:0}
.review-box{position:relative;box-sizing:border-box;display:flex;flex-direction:column;max-height:100dvh;padding:24px;background:var(--card)}
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

REVIEW_CARD_SCRIPT = r'''
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
const reviewWaiting=entry=>entry.state==='Waiting' && entry.waitingWhy
 ? '<span class="review-waiting-why">'+escapeText(entry.waitingWhy)+'</span>' : '';
function reviewCardSummary(entry,titleLimit,icon){
 const node=document.createElement('tbody'),panel=detailId(entry.key);
 Object.assign(node.dataset,{listItem:'',key:entry.key,number:entry.id.toLowerCase(),title:entry.title.toLowerCase(),
  owner:entry.owner,sourceOwner:entry.sourceOwner,sourceName:entry.sourceName,state:entry.state,type:entry.type,
  order:entry.order,updated:entry.updated,parked:'false',sortValues:JSON.stringify(entrySortValues(entry))});
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
  '<strong class="review-title" title="'+escapeText(entry.title)+'">'+escapeText(short===entry.title ? short : short+'…')+'</strong>'+handoffLine+reviewWaiting(entry)+(urgency ? '<small>'+escapeText(urgency)+'</small>' : ''),
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
function laterTimes(now,zone,settings){
 const today=zoneParts(zone,now);
 const at=(days,minutes)=>zoneEpoch(zone,today.year,today.month,today.day+days,minutes);
 const weekday=new Date(Date.UTC(today.year,today.month-1,today.day)).getUTCDay();
 return {soon:now+settings.soon*60000,
  evening:today.hour*60+today.minute<settings.eveningUntil ? at(0,settings.evening) : 0,
  morning:at(1,settings.morning),week:at((settings.weekDay-weekday+7)%7 || 7,settings.weekTime)};
}
function bindLater(form,zone,settings,pick){
 const shown=new Intl.DateTimeFormat('en-US',{timeZone:zone(),weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
 const prepareMenu=menu=>{
  const times=laterTimes(Date.now(),zone(),settings());
  menu.querySelectorAll('[data-later-preset]').forEach(item=>{
   const at=times[item.dataset.laterPreset];item.hidden=!at;item.dataset.at=at ? String(at) : '';
   item.querySelector('small').textContent=at ? shown.format(at) : '';
  });
 };
 form.querySelectorAll('[data-later-preset]').forEach(item=>item.addEventListener('click',()=>{
  // Resolve the time again at the click, so a menu left open does not send a stale time.
  const at=laterTimes(Date.now(),zone(),settings())[item.dataset.laterPreset] || Number(item.dataset.at);
  closeDateTimePicker(form.querySelector('[data-later-menu]'),false);pick(new Date(at).toISOString());
 }));
 bindDateTimePicker(form,{zone:zone(),defaultMinutes:()=>settings().morning,
  summaryPrefix:'Hides until ',prepareMenu});
}
function installReviewBody(row,text){
 row.dataset.bodyLoaded='true';
 const detail=recordDetails(row);
 detail.innerHTML=text;
 const panel=detail.querySelector('.review-panel');
 if(panel) updateTitleFallback(panel);
 updateHandoffAges(detail);
}
const reviewDrafts=new Map();
function bindReviewForm(form,{zone,settings,remember=()=>rememberReviewDraft(form,true),send}){
 const words=form.elements.feedback,later=form.elements.later,assignee=form.elements.assignee;
 const row=recordFromDetail(form);
 const stored=reviewDrafts.get(row.dataset.key);
 if(stored) restoreReviewDraft(form,stored.draft);
 const choices=recordDetails(row).querySelectorAll('input[name=choice]');
 const submit=(outcome,until)=>{
  const data=new URLSearchParams(new FormData(form));data.set('outcome',outcome);
  if(outcome==='later') data.set('later',until);
  if(outcome!=='approve') data.delete('assignee');
  void send(outcome,data);
 };
 choices.forEach(choice=>choice.addEventListener('change',()=>{words.value=choice.value;remember();words.focus();}));
 [words,later,assignee].filter(Boolean).forEach(field=>{
  field.addEventListener('input',remember);field.addEventListener('change',remember);
 });
 form.addEventListener('submit',event=>{
  event.preventDefault();
  const outcome=event.submitter.value;
  if(outcome==='later'){
   const at=dateTimeDraftEpoch(zone(),later.value);
   if(!(at>Date.now())) return;
   closeDateTimePicker(form.querySelector('[data-later-menu]'),false);
   submit(outcome,new Date(at).toISOString());return;
  }
  submit(outcome);
 });
 if(later) bindLater(form,zone,settings,until=>submit('later',until));
}
function readReviewDraft(form){
 if(!form?.elements.feedback) return null;
 const choices=[...recordDetails(recordFromDetail(form)).querySelectorAll('input[name=choice]')];
 const assignee=form.elements.assignee?.selectedOptions[0];
 return {feedback:form.elements.feedback.value,later:form.elements.later?.value ?? '',
  assignee:assignee?.value ?? '',assigneeName:assignee?.dataset.assigneeName ?? assignee?.textContent ?? '',
  choice:choices.find(choice=>choice.checked)?.value || ''};
}
function rememberReviewDraft(form,edited=false){
 const draft=readReviewDraft(form);
 if(!draft) return;
 const key=recordFromDetail(form).dataset.key;
 const previous=reviewDrafts.get(key);
 // The animation copy can clear the live radio selection.
 draft.choice=draft.choice || previous?.draft.choice || '';
 const stored={draft,stamp:(previous?.stamp ?? 0)+Number(edited)};
 reviewDrafts.set(key,stored);
 return stored;
}
function restoreReviewDraft(form,draft){
 form.elements.feedback.value=draft.feedback;
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
async function recoverReviewRevision(reason,{form,notice,reload,current=()=>true,present=apply=>apply(),report}){
 const revision=form.elements.revision?.value,key=recordFromDetail(form).dataset.key;
 const atConflict=reviewDrafts.get(key) || rememberReviewDraft(form);
 const message='Not saved: '+reason;
 const notify=report ?? (text=>{notice.textContent=text;notice.hidden=false;});
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
   const latest=reviewDrafts.get(key);
   const unchanged=latest?.stamp===atConflict?.stamp;
   const draft=unchanged ? atConflict?.draft : latest?.draft;
   const fresh=recordDetails(row)?.querySelector('[data-review-form]')
    ?? row?.querySelector('.review-box-footer [data-review-form]');
   if(fresh?.elements.feedback){
    if(atConflict && unchanged){
     restoreReviewDraft(fresh,atConflict.draft);
     reviewDrafts.set(key,atConflict);
    }
    if(!report) fresh.querySelector('[data-send-status]').textContent=message;
   }else if(row){
    const detail=recordDetails(row);
    if(!report){
     const status=document.createElement('p');
     status.setAttribute('role','status');status.textContent=message;detail.append(status);
    }
    if(draft?.feedback){
     const label=document.createElement('label'),words=document.createElement('textarea');
     label.textContent='Your unsaved words';
     words.value=draft.feedback;words.readOnly=true;words.dataset.reviewDraft='';
     label.append(words);detail.append(label);
    }
   }
  };
  await present(()=>apply().catch(refreshError));
 }catch(error){
  refreshError(error);
 }
}
// The save ends after `seconds`, the page_request_timeout_seconds setting.
// A save without an answer calls `failed` with words that say the result is unknown, and `unknown` true.
async function saveReviewAction(data,{seconds,confirmedSave,saved,failed,displayFailed,recover,finished}){
 let conflict=false,confirmed=false;
 try{
  const response=await fetch(location.pathname,{method:'POST',body:data,signal:AbortSignal.timeout(seconds*1000)});
  conflict=response.status===409;
  confirmed=response.ok;
  if(confirmed) confirmedSave?.();
  const result=await response.json();
  if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
  await saved(result);
 }catch(error){
  if(confirmed) displayFailed();
  else if(conflict) await recover(error.message);
  else{const unknown=saveTimeoutWords(error,seconds);failed(unknown || error.message,Boolean(unknown));}
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
 notice.textContent=text;
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
 const whole=fixedHeight+minBody>document.documentElement.clientHeight;
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
  +reviewWaiting(entry)+(urgency ? '<span>'+escapeText(urgency)+'</span>' : '')+'</div></header>'
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
 const form=row.querySelector('[data-review-form]');
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
   const response=await fetch(location.href,{cache:'no-store',signal});
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
 const send=async(_outcome,data)=>{
  if(cardSubmitBlocked(form) || !showUpdates.hidden) return;
  rememberReviewDraft(form);
  busy(true);
  if(conflict){
   await recover(conflict);
   busy(false);
   return;
  }
  report('Saving…');
  let saved=false;
  await saveReviewAction(data,{
   seconds:requestTimeoutSeconds,
   saved:result=>{
    saved=true;
    report('Saved: '+result.answer);
    if(frame){
     frame.removeAttribute('data-saving');
     frame.dispatchEvent(new Event('review-card-busy'));
     frame.dispatchEvent(new Event('review-card-saved'));
    }
   },
   failed:(reason,unknown)=>report(unknown ? reason : 'Not saved: '+reason),
   displayFailed:()=>report('Saved; the item could not refresh.'),
   recover,
   finished:()=>{if(!saved) busy(false);}
  });
 };
 bindReviewForm(form,{zone:()=>zone,settings:()=>settings,send});
 return row;
}
mountCard(document);
setInterval(updateHandoffAges,5000);
})();'''

CARD_REQUEST_SCRIPT = record_table.SCRIPT + console.REVIEW_PLACE_SCRIPT + r'''
const kinds=''' + json.dumps(WIRE_TYPES) + ''',states=''' + json.dumps(INDEX_STATES) + r''';
function indexEntry(payload,row,at){
 const owner=payload.n[row[3]],source=payload.n[row[4]];
 return {key:row[0],id:row[1] || row[0],type:row[13]?'response':kinds[row[2]],owner:owner[0],sourceOwner:source[0],
  ownerName:owner[1],sourceName:source[1],title:row[5],state:states[row[6]],filed:row[7],updated:row[8],
   hiddenUntil:row[9],ownerHtml:owner[2],sourceHtml:source[2],urgency:row[10] || 'normal',handoff:row[11],
   waitingWhy:row[12],actionStatus:row[14],order:at+row[6]*payload.r.length};
}
function readIndex(payload){
 return new Map(payload.r.map((row,at)=>[row[0],indexEntry(payload,row,at)]));
}
let index=readIndex(JSON.parse(document.querySelector('[data-review-index]').textContent));
function reviewRequestSignal(){
 const seconds=Number(document.querySelector('[data-review]').dataset.requestTimeoutSeconds);
 return AbortSignal.timeout(seconds*1000);
}
function requestReviewCard(key,version,saved=false){
 const query=new URLSearchParams({body:key,version});
 if(saved) query.set('saved','1');
 return fetch(location.pathname+'?'+query,{cache:'no-store',signal:reviewRequestSignal()})
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
 const params=recordPlaceParams();
 legacyReviewPlace(params);
 const key=recordItemLink(params,value=>resolveShownRecordId(index,value));
 if(!index.has(key)) return null;
 const version=document.querySelector('[data-review]').dataset.version;
 const result=requestReviewCard(key,version);
 void result.catch(()=>{});
 return {key,version,result};
})();
'''

SCRIPT = (page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + record_table.DATE_TIME_SCRIPT
          + REVIEW_CARD_SCRIPT + r'''(reviewPlace => {
let content=document.querySelector('[data-review]');
const saved=new Map(),bodies=new Map(),bodyRequests=new Map();
const pendingSaves=new Map(),recovering=new Map();
const scrollDepartures=new Set();
const savedVerdictsKey='review-saved-verdicts:'+location.pathname;
let focusAfterLoad=null,firstRequest=firstReviewCard;
let deferredLink=null,revealCreated=null,newItemRefresh;
function allEntries(){return [...index.values()];}
function isInvestigation(entry){return entry.type==='investigation' || entry.type==='bug';}
function rowOwners(entry){
 return isInvestigation(entry) && entry.sourceOwner!==entry.owner ? [entry.owner,entry.sourceOwner] : [entry.owner];
}
function rowTypes(entry){return isInvestigation(entry) ? ['',entry.type,'investigations'] : ['',entry.type];}
function rowShow(entry){return entry.actionStatus==='withdrawn' ? '' : entry.state==='Open' ? 'open' : entry.state;}
function typeChipShow(show,type){
 return type==='for-you' || type==='response' && show==='Validating' ? 'open' : show;
}
function availableForFilters(entry,{query,keys,now=Date.now()}){
 const linked=keys?.has(entry.key);
 return (!keys?.size || linked) && (linked || view.linkedItem===entry.key || entry.hiddenUntil<=now) &&
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
 if(pendingSaves.get(key)?.departing || awaitsHandoff(key)) return false;
 return (!departure || entry.type==='for-you' || departure.handed && entry.state!=='Open') &&
  availableForFilters(entry,filters) && (!owner || rowOwners(entry).includes(owner)) &&
  rowTypes(entry).includes(type) && (!sourceOwner || entry.sourceOwner===sourceOwner) &&
  (show==='all' || show===rowShow(entry));
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
  const row=item.dataset,ownersForRow=rowOwners(row),typesForRow=rowTypes(row),state=rowShow(row);
  const showsForRow=state ? ['all',state] : ['all'];
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
 content.querySelectorAll('[data-owner-chip]').forEach(button=>{
  const id=button.dataset.ownerChip;
  button.hidden=Boolean(id) && id!==owner && !eligibleOwners.has(id);
  button.querySelector('b').textContent=id===owner ? shown : owners.get(id) ?? 0;
 });
 const showCounts=owner && !ownerTypes.has(type) ? anyOwnerShowCounts : shows;
 content.querySelectorAll('[data-show-chip]').forEach(button=>{
  const value=button.dataset.showChip;
  button.hidden=['response','for-you'].includes(type) && value==='Validating';
  const count=value===show ? shown : showCounts.get(value) ?? 0;
  button.querySelector('b').textContent=count;
  button.toggleAttribute('data-empty-count',count===0);
 });
 content.querySelectorAll('[data-type-chip]').forEach(button=>{
  const kind=button.dataset.typeChip;
  if(kind==='investigations') button.hidden=type!=='investigations';
  button.setAttribute('aria-pressed',String(kind===type));
  // A State reset checks the current Type before the new Type applies.
  const resetsState=typeChipShow(show,kind)!==show;
  const resetsOwner=owner && (!ownerTypes.has(kind) || resetsState && !ownerTypes.has(type));
  const counts=resetsOwner ? anyOwnerTypeCounts : types;
  button.querySelector('b').textContent=kind===type ? shown : counts.get(kind) ?? 0;
 });
}
function savedEntry(key,result){
 const entry=index.get(key);if(!entry) return;
 // A Done row that shows its own result stays in the index and shows its new state.
 if(result.state==='Done' && entry.type!=='for-you' && !result.retained){index.delete(key);return;}
 entry.state=result.state;entry.hiddenUntil=result.later;
 if(result.state==='Done') entry.actionStatus='done';
 if(result.type) entry.type=result.type;
}
function snapshotPrecedesSave(entry,result){
 return entry?.state===result.previousState && entry.updated===result.updated;
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
const showValues=[...content.querySelectorAll('[data-show-chip]')].map(button=>button.dataset.showChip);
const view=createRecordTable({content:()=>content,
 entries:allEntries,mount:entry=>reviewCardSummary(entry,Number(content.dataset.titleLimit),
  content.querySelector('[data-type-chip="'+entry.type+'"] .review-icon').outerHTML),mounted:()=>loadBodies(),
 focusKey:()=>focusAfterLoad?.key,
 pinOpenRow:key=>{
  const pending=pendingSaves.get(key);
  return (!pending || pending.keepOpen) && !awaitsHandoff(key);
 },
 unmount:rememberItem,
 rememberItem,
 place:params=>{
  reviewPlace(params);
  const show=showValues.find(value=>value.toLowerCase()===params.get('show')?.toLowerCase());
  if(show) params.set('show',['response','for-you'].includes(params.get('type')) && show==='Validating' ? 'open' : show);
 },
 persistLinkedTarget:true,
 missingItem:()=> 'This review is no longer on the Review page.',
 resolveOwner:owner=>{
  const button=[...content.querySelectorAll('[data-owner-chip]')].find(button=>
   button.dataset.ownerChip===owner || button.dataset.ownerName===owner);
  return button ? button.dataset.ownerChip : owner;
 },
 selectOwner:(rows,filters)=>!filters.owner || rows.some(({dataset:entry})=>
  availableForFilters(entry,filters) && ownerHasType(entry,filters)) ? filters.owner : '',
 resolveItem:value=>resolveShownRecordId(index,value),
 hasKey:key=>index.has(key),
 shows:showValues,
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
  content.dataset.show=show;loadBodies();
  const actionNotices=content.querySelector('[data-for-you-notices]');
  actionNotices.hidden=type!=='for-you' || !actionNotices.children.length;
  updateReviewChips(rows,filters,matches,count);
 }
});
async function afterRowAnimations(run,current=()=>true){
 while(current() && view.hasActiveRowAnimations) await view.settled();
 if(current()) return run();
}
addEventListener('scroll',()=>{
 const waiting=scrollDepartures.size>0;
 for(const save of pendingSaves.values()){
  if(save.departing && !save.saved && Math.abs(scrollY-save.top)>=innerHeight) scrollDepartures.add(save);
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
function applyReviewDetail(key,row,detail,text){
 installReviewBody(row,text);
 const departure=saved.get(key);
 if(departure?.closes && departure.awaitingSnapshot)
  detail.querySelector('[data-review-form]')?.remove();
 bindForm(row);
 view.showDraftCopy(reviewDrafts.get(key)?.draft.feedback,Boolean(detail.querySelector('[name="feedback"]')));
}
function loadBodies(){
 view.opened.forEach(key=>{
  const row=view.openItem;
  if(row?.dataset.key!==key || !index.has(key) || !needsBody(row)) return;
  const install=text=>{
   const current=view.openItem;
   if(current?.dataset.key!==key || !index.has(key)) return;
   const detail=recordDetails(current);
   const apply=(row,detail)=>applyReviewDetail(key,row,detail,text);
   view.itemError('');
   current.dataset.bodyLoaded='true';
   view.preserveRecordRowTop(()=>{
    view.presentItemRevision(current,text,apply,{initial:true});
    drawSaveControls(detail.querySelector('[data-review-form]'));
    restoreFocus(key);
   });
  };
  if(bodies.has(key)){install(bodies.get(key));return;}
  if(bodyRequests.has(key)) return;
  drawSaveControls(recordDetails(row).querySelector('[data-review-form]'));
  const version=content.dataset.version;
  const prefetched=firstRequest?.key===key && firstRequest.version===version;
  const result=prefetched ? firstRequest.result : requestReviewCard(key,version);
  if(prefetched) firstRequest=null;
  const request=result
   .then(({response,text})=>{
    if(bodyRequests.get(key)!==request || content.dataset.version!==version) return;
    const bodyVersion=response.headers.get('X-Review-Version');
    if(bodyVersion && bodyVersion!==version){
     bodyRequests.delete(key);
     void pageRefresh.refresh(true);
     return;
    }
    if(!response.ok) throw new Error('HTTP '+response.status);
    if(bodyVersion!==version) throw new Error('The item has no snapshot version.');
    if(text.includes(' data-review-card-unavailable>')) throw new Error('The item body is unavailable.');
    bodies.set(key,text);install(text);
   }).catch(error=>{
    const current=view.openItem;
    if(current?.dataset.key===key && index.has(key) && bodyRequests.get(key)===request){
     current.dataset.bodyLoaded='failed';
     const loading=recordDetails(current).querySelector('[data-record-loading]');
     if(loading) loading.hidden=true;
     view.itemError('The item could not refresh: '+error.message);
    }
   })
   .finally(()=>{if(bodyRequests.get(key)===request) bodyRequests.delete(key);});
  bodyRequests.set(key,request);
 });
}
function rememberForm(form,edited=false){
 if(!form?.elements.feedback) return;
 return rememberReviewDraft(form,edited);
}
function rememberItem(row){
 if(!saved.has(row.dataset.key)) rememberForm(recordDetails(row).querySelector('[data-review-form]'));
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
 const disabled=row.dataset.bodyLoaded!=='true' || !index.has(key) || recoveringForm(key,form);
 form.querySelectorAll('button').forEach(button=>button.disabled=disabled);
}
function drawSaveState(key,form=recordDetails(reviewRow(key))?.querySelector('[data-review-form]')){
 if(!form) return;
 drawSaveControls(form);
 form.querySelector('[data-send-status]').textContent=pendingSaves.get(key)?.notice || view.actionNotice(recordFromDetail(form));
}
function bindForm(row){
 const form=recordDetails(row).querySelector('[data-review-form]');if(!form || form.dataset.bound) return;
 form.dataset.bound='true';
 const key=row.dataset.key;
 drawSaveState(key,form);
 if(!form.elements.feedback) return;
 const send=async(outcome,data)=>{
   const current=recordFromDetail(form);
   if(!view.canSubmit(form,data.get('row')) || view.openItem!==current || current.dataset.bodyLoaded!=='true' ||
    !index.has(key) || cardSubmitBlocked(form,view.rowFading(key)) ||
    pendingSaves.has(key) || recoveringForm(key,form)) return;
   const row=current;
   recovering.delete(key);
   const draftAtSubmit=rememberForm(form);
   const notice=content.querySelector('[data-review-notice]');
   notice.hidden=true;notice.textContent='';
   const validationQuestion=row.dataset.state==='Validating' && outcome==='question';
   const handed=row.dataset.type==='change' && ['validate','question','feedback'].includes(outcome);
   // A question or feedback on a change or a proposal shows its own result, so its row stays open through the save.
   const keepOpen=['question','feedback'].includes(outcome) && ['change','proposal'].includes(row.dataset.type);
   const pending={notice:'Saving…',saved:false,departing:!validationQuestion,keepOpen,top:scrollY};
   pendingSaves.set(key,pending);pageRefresh.abandon();drawSaveState(key);
   const request=view.startItemRequest(key),session=view.openSession;
   const closes=outcome==='later' || !['question','feedback','validate'].includes(outcome);
   const submission=view.preserveRecordRowTop(()=>view.startAction(row));
   const reopened=()=>view.openItem?.dataset.key===key && view.opened.has(key) &&
    !view.currentItemRequest(request);
   const showResult=result=>{
    if(reopened()) view.applyAction();
    else view.showActionResult(row,submission,key,result);
   };
   const settle=async()=>{
    pendingSaves.delete(key);setCardSubmitting(form,false);drawSaveState(key);pageRefresh.changed();
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
   const fail=(reason,unknown)=>{
    pendingSaves.delete(key);
    view.preserveRecordRowTop(()=>showResult({kind:'failure',message:unknown ? reason : 'Not saved: '+reason}));
   };
   await saveReviewAction(data,{
   seconds:pageRequestSeconds(),
   confirmedSave:()=>{
    pending.saved=true;
    if(view.currentItemRequest(request) && reviewDrafts.get(key)?.stamp===draftAtSubmit?.stamp){
     reviewDrafts.delete(key);
     form.elements.feedback.value='';
    }
   },
   saved:async result=>{
    const showOwnDetail=view.currentItemRequest(request) && keepOpen && !result.detail_closed;
    const savedMessage=result.detail_unavailable && !showOwnDetail
     ? {kind:'failure',message:'Saved; the item could not refresh.'}
     : {kind:'success',message:'Saved: '+result.answer};
    if(showOwnDetail){
     view.startDeliveryWindow(request,result);
     let text=result.detail_html;
     if(typeof text!=='string' && ('delivery_pending' in result || result.detail_revision || result.detail_unavailable)){
      const {response,text:body}=await requestReviewCard(key,result.version,true);
      if(!response.ok || body.includes('data-review-card-unavailable')) throw new Error('The saved item body is unavailable.');
      text=body;
     }
     if(typeof text==='string' && view.currentItemRequest(request))
      view.preserveRecordRowTop(()=>view.installOwnItemResult(request,text,(item,detail)=>applyReviewDetail(key,item,detail,text)));
    }
    if(validationQuestion){
     view.preserveRecordRowTop(()=>{
      if(result.detail_closed && !reopened()) view.closeCard(key);
      showResult(savedMessage);
     });
     return;
    }
    const waitState=outcome==='validate' ? 'Validating' : 'Waiting';
    const marker={updated:row.dataset.updated,previousState:row.dataset.state,
     handed,state:result.state ?? (handed || outcome==='question' ? waitState : outcome==='later' ? row.dataset.state : 'Done'),
     type:result.type,
     later:result.later ?? (outcome==='later' ? Date.parse(data.get('later')) : 0),
     question:outcome==='question',
     retained:showOwnDetail && result.state==='Done' ? index.get(key) : null,ownSession:showOwnDetail ? session : undefined,
     closes:result.detail_closed ?? (!handed && !['question','later'].includes(outcome)),awaitingSnapshot:true};
    const applySaved=()=>view.preserveRecordRowTop(()=>{
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
   displayFailed:()=>{
    if(!view.currentItemRequest(request)) return;
    view.preserveRecordRowTop(()=>{
     if(closes){
      view.closeCard(key);
      showResult({kind:'failure',message:'Saved; the item could not refresh.'});
     }else view.savedItemError(request);
    });
   },
   recover:async reason=>{
    if(!view.currentItemRequest(request)){
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
      reload:async()=>{
       fail(reason);
       await settle();
       if(!current()) return;
       await pageRefresh.refresh(true);
       await bodyRequests.get(key);
       const fresh=reviewRow(key);
       return {revision:recordDetails(fresh)?.querySelector('[name="revision"]')?.value,show:()=>fresh};
      }
     });
    }finally{
     if(current()){
      recovering.delete(key);
      const fresh=recordDetails(reviewRow(key))?.querySelector('[data-review-form]');
      rememberForm(fresh);
      drawSaveControls(fresh);
     }
    }
   },
   finished:async conflict=>{
    if(conflict) return;
    await settle();
    if(!pendingSaves.size) void pageRefresh.refresh(true);
   }});
  };
 bindReviewForm(form,{zone:()=>content.dataset.zone,settings:()=>JSON.parse(content.dataset.later),
  remember:()=>rememberForm(form,true),send});
}
function bind(){view.replace(false);}
content.addEventListener('click',event=>{
 const chip=event.target.closest('[data-type-chip][aria-pressed="false"]');
 const show=content.dataset.show;
 if(chip && typeChipShow(show,chip.dataset.typeChip)!==show)
  document.getElementById('review-show-open').click();
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
 const canApply=()=>!pendingSaves.size && current();
 const options={cache:'no-store',signal:reviewRequestSignal()};
 const delivery=view.deliveryRequest(),query=new URLSearchParams({version:'1'});
 if(delivery) query.set('delivery',delivery.delivery);
 const response=await fetch(location.pathname+'?'+query,options);
 if(!response.ok) throw new Error('HTTP '+response.status);
 const version=await response.json();
 if(!current()) return;
 const newer=reloadForNewScript(version.script,()=>pendingSaves.size>0);
 if(newer || !canApply() || dateTimePickerOpen(document)) return newer;
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
 const fresh=await fetch(location.pathname+'?'+new URLSearchParams({index:'1',version:version.version,'for-you':'1'}),options);
 if(!fresh.ok) throw new Error('HTTP '+fresh.status);
 const payload=await fresh.json();
 if(!canApply() || dateTimePickerOpen(document)) return;
 return afterRowAnimations(async()=>{
  view.preserveRecordRowTop(()=>applyReviewIndex(payload));
  await finishDelivery();
  return {...payload.facts,answeredAt:version.facts.answeredAt};
 },canApply);
}
function applyReviewIndex(payload){
 focusAfterLoad=rememberFocus();
 rememberForm(recordDetails(view.openItem)?.querySelector('[data-review-form]'));
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
  const line=document.createElement('li');line.textContent=text;return line;
 }));
 notices.hidden=!notices.children.length;
 content.querySelector('[data-for-you-notices]').replaceChildren(...(payload.actionNotices || []).map(text=>{
  const line=document.createElement('li');line.textContent=text;return line;
 }));
 bodies.clear();bodyRequests.clear();
 const menu=content.querySelector('#list-owner'),pairs=new Map();
 const focusedOwner=document.activeElement?.dataset.ownerChip;
 const selectedOwner=menu.querySelector('[aria-pressed="true"]')?.dataset.ownerChip;
 for(const entry of index.values()){
  pairs.set(entry.owner,{name:entry.ownerName,markup:entry.ownerHtml});
 }
 for(const entry of index.values()){
  if(isInvestigation(entry)) pairs.set(entry.sourceOwner,{name:entry.sourceName,markup:entry.sourceHtml});
 }
 for(const button of menu.querySelectorAll('[data-owner-chip]')){
  const id=button.dataset.ownerChip;
  if(id && id!==selectedOwner && !pairs.has(id)) button.remove();
 }
 for(const [id,{name,markup}] of [...pairs].sort((a,b)=>a[1].name.localeCompare(b[1].name))){
  let button=menu.querySelector('[data-owner-chip="'+CSS.escape(id)+'"]');
  if(!button){
   button=document.createElement('button');button.type='button';
   button.id='review-owner-'+id;button.dataset.ownerChip=id;menu.append(button);
  }
  const count=button.querySelector('b') || document.createElement('b');
  button.dataset.ownerName=name;
  button.innerHTML=colored(name,markup)+' ';button.append(count);
 }
 for(const [key,result] of saved){
  const entry=index.get(key);
  if(!entry && result.retained && view.opened.has(key)){index.set(key,result.retained);continue;}
  if(!entry || (result.later && Date.now()>=result.later)) {saved.delete(key);continue;}
  // The index supplies the state and update time for each row.
  const submitted=snapshotPrecedesSave(entry,result);
  if(!submitted && entry.type==='for-you'){saved.delete(key);continue;}
  if(!submitted && (result.restored || result.handed || entry.state!==result.state)){saved.delete(key);continue;}
  if(!result.closes && !result.question && !result.later && !submitted) saved.delete(key);
  else result.awaitingSnapshot=submitted;
 }
 storeSavedVerdicts();
 for(const [key,result] of saved) savedEntry(key,result);
 bind();
 const openedLink=view.retryItemLink();
 if(!openedLink) view.apply(false);
 if(focusAfterLoad) restoreFocus(focusAfterLoad.key);
 if(focusedOwner!==undefined && (!document.activeElement?.hasAttribute('data-owner-chip') || document.activeElement.hidden))
  menu.querySelector('[aria-pressed="true"]')?.focus({preventScroll:true});
 content.dataset.loaded='true';
 revealNewItem();
 content.dispatchEvent(new Event('review-refreshed'));
}
bind();
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
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
 location.hash=recordPlaceParams(target).toString();
};
})(legacyReviewPlace);''')
# The fixed shell markup. A refresh reads only the index and card bodies, so a change here needs a reload.
SHELL_SHOW = 'open'
SHELL_TYPE_CHIPS = (
    '<button id="review-type-all" type="button" data-type-chip="" aria-pressed="true">All <b></b></button>'
    + ''.join('<button id="review-type-' + kind + '" type="button" data-type-chip="' + kind + '" aria-pressed="false">'
              + icon(kind) + LABELS[kind] + ' <b></b></button>' for kind in TYPE_CHIPS)
    + '<button type="button" data-type-chip="investigations" aria-pressed="false" hidden>'
    + 'Investigations and Bugs <b></b></button>')
SHELL_SHOW_CHIPS = ''.join('<button id="review-show-' + value.lower().replace(' ', '-') + '" type="button" data-show-chip="'
                           + value + '" aria-pressed="' + str(value == SHELL_SHOW).lower() + '">' + label
                           + ' <b></b></button>' for value, label in SHOWS)
SHELL_FILTERS = (
    '<div class="record-filter-words"><label class="list-search-label" for="list-search">Search</label>'
    + '<input type="search" id="list-search" placeholder="Filter by id or words" data-reload-safe></div>'
    + '<div class="review-queue">'
    + record_table.labelled_chip_row('Type', SHELL_TYPE_CHIPS, chip_classes='review-types')
    + record_table.labelled_chip_row('State', SHELL_SHOW_CHIPS, chip_classes='review-types review-shows', chip_id='list-show'))
SHELL_NOTICES = (
    '<div class="list-filters">'
    + '<button type="button" data-clear-linked-reviews aria-label="Clear linked reviews filter"'
    + ' aria-pressed="true" hidden>Linked reviews <span aria-hidden="true">×</span></button>'
    + '</div><p data-missing-item role="status" hidden></p><p role="status" data-review-notice hidden></p>'
    + '<p data-hidden-action-status role="status" hidden></p>')
SHELL_TABLE = (
    '<div class="list-table"><table aria-label="Review">' + CARD_COLUMNS + '<thead><tr>'
    + ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
              + '<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    + '</tr></thead><tbody data-window-before></tbody><tbody data-window-after></tbody>'
    + '</table><p data-empty hidden>No items in this view.</p></div></div></div>')
SHELL_PARTS = (SHELL_FILTERS, SHELL_NOTICES, SHELL_TABLE, record_table.render_inline_detail())
VERSION_PARTS = (change_page.DOCUMENT_ASSETS, record_table.STYLE, record_table.DATE_TIME_STYLE,
                 STYLE, CARD_REQUEST_SCRIPT, *SHELL_PARTS, SCRIPT)
SCRIPT_VERSION = page_version.script_version(*VERSION_PARTS)
