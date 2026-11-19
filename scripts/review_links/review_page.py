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
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import NotRequired, TypedDict, cast
from urllib.parse import parse_qs, parse_qsl, urlencode
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.lib.user_zone import CONF as USER_ZONE_CONF, user_zone
from scripts.production import change_review, change_routing, proposal_issue, staff, update_registration
from scripts.production.record_store import FileStamp, state_root
from scripts.lib import urgency
from scripts.review_links import change_browser, change_page, console, page_frame, record_table, review_items, review_link
from scripts.review_links import new_item_dialog, review_submissions
from scripts.review_links import question_record, review_actions, staff_names
from scripts.review_links.review_items import ReviewItem, ReviewSourceStamps, ReviewState, ReviewType, guard_source
from scripts.review_links.review_items import source_stamp as source_stamp

PAGE_PATH = '/console/review.html'
NEW_ITEM_PATH = '/console/review/new-item'
ATTACHMENT_PATH = '/console/review/attachment'
HEADERS = ('Id', 'Type', 'Owner', 'Item', 'State', 'Age')
TYPES = tuple(ReviewType)
WIRE_TYPES = tuple(kind for kind in TYPES if kind != ReviewType.RESPONSE)
TYPE_CHIPS = (*review_items.QUESTION_TYPES, *(kind for kind in TYPES if kind not in review_items.QUESTION_TYPES))
ICONS = {ReviewType.QUESTION: 'circle-help', ReviewType.CHANGE: 'diff', ReviewType.INVESTIGATION: 'microscope',
         ReviewType.PROPOSAL: 'lamp', ReviewType.EFFICIENCY: 'zap', ReviewType.BUG: 'bug', ReviewType.RESPONSE: 'inbox'}
INDEX_STATES = (ReviewState.OPEN, ReviewState.WAITING, ReviewState.VALIDATING)
SHOWS = (('open', 'Open'), (ReviewState.VALIDATING, ReviewState.VALIDATING), ('all', 'All'))
LABELS = {ReviewType.QUESTION: 'Questions', ReviewType.CHANGE: 'Changes', ReviewType.INVESTIGATION: 'Investigations',
          ReviewType.PROPOSAL: 'Proposals', ReviewType.EFFICIENCY: 'Efficiencies', ReviewType.BUG: 'Bugs',
          ReviewType.RESPONSE: 'Responses'}
LATER_PRESETS = (('soon', ''), ('evening', 'This evening'), ('morning', 'Tomorrow morning'), ('week', 'Next week'))
# Weekday names in the order of the browser's getUTCDay.
WEEKDAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
FRONT_MATTER = re.compile(r'\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)
CARD_ERROR = b'<p role="status">This card is not available.</p>'


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


def filter_index(records: list[ReviewItem], members: staff.StaffSnapshot) -> list[ReviewIndexEntry]:
    """Build the index for the filters and columns."""
    names = staff_names.StaffNames(members)
    return [card_entry(item, members, names) for item in records if item.state != ReviewState.DONE]


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
             *([entry.get('handoff'), entry.get('waitingWhy'), True] if entry['type'] == ReviewType.RESPONSE
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
class ReviewSnapshot:
    """Own the current records, compact index and shell until the next complete update."""
    version: str
    records: dict[str, ReviewItem]
    entries: list[ReviewIndexEntry]
    members: staff.StaffSnapshot
    browser: change_page.ReviewBrowser
    words_rows: int
    refresh_seconds: float
    card_motion: page_frame.CardMotion
    assets: dict[str,str]
    zone: ZoneInfo
    source_stamps: dict[Path, FileStamp]
    later: Mapping[str, int]
    names: staff_names.StaffNames
    page: bytes = b''
    payload: bytes = b''
    page_gzip: bytes = b''
    payload_gzip: bytes = b''
    read_at: float = 0
    render_seconds: float = 0
    notices: tuple[str, ...] = ()
    unavailable_cards: frozenset[str] = frozenset()
    title_limit: int = 100
    submission_keys: dict[str, str] = field(default_factory=dict)

    def facts(self) -> dict[str,float]:
        return {'readAt':self.read_at,'renderSeconds':self.render_seconds,'answeredAt':time.time()}

    def index_response(self) -> bytes:
        return self.payload

    def render(self, *, show: str = 'open') -> bytes:
        selected = 'all' if show == 'Waiting' else show if show in dict(SHOWS) else 'open'
        page = self.page.replace(b'data-show="open"', ('data-show="'+selected+'"').encode(), 1)
        if selected != 'open':
            page = page.replace(b'data-show-chip="open" aria-pressed="true"',b'data-show-chip="open" aria-pressed="false"',1)
            page = page.replace(('data-show-chip="'+selected+'" aria-pressed="false"').encode(),
                                ('data-show-chip="'+selected+'" aria-pressed="true"').encode(),1)
        return page

    def body(self, key: str) -> bytes:
        """Render one retained record without a store read."""
        item = self.records.get(key)
        if item is None:
            raise LookupError('This item is not in the current snapshot.')
        if key in self.unavailable_cards:
            return CARD_ERROR
        try:
            return details(item, ReviewBodyInputs(self.browser, self.members, self.words_rows, soon_label(self.later.get("soon", 60)), self.assets, self.zone, self.names)).encode()
        except Exception as error:
            logging.getLogger(__name__).warning('Review card %s: %s', key, error)
            return CARD_ERROR


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
    if origin.page_state is not None and (page_data := origin.bug_page_data) is not None:
        screenshot = review_submissions.PAGE_SCREENSHOT
        body += bug_page_state_box(key, origin.page_state, page_data.title, screenshot in images)
        images = tuple(image for image in images if image != screenshot)
    elif isinstance(evidence, new_item_dialog.PageDataEvidence):
        data = json.dumps({'title': evidence.title, 'rows': evidence.rows}, ensure_ascii=False, indent=2)
        body = '<section data-submission-evidence><h3>Page data</h3><pre>'+html.escape(data)+'</pre></section>'
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
    payload = ('{"version":'+json.dumps(snapshot.version)+',"index":'+index
               +',"notices":'+json.dumps(snapshot.notices)
               +',"presentation":'+json.dumps({'zone':snapshot.zone.key,'later':dict(snapshot.later),
                                                'titleLimit':snapshot.title_limit,
                                                'cardFadeOutMs':snapshot.card_motion.fade_out_ms,
                                                'cardFadeInMs':snapshot.card_motion.fade_in_ms})
               +',"facts":'+json.dumps(snapshot.facts())+'}').encode()
    page = render_shell(snapshot,index,started,snapshot.read_at)
    return replace(snapshot,page=page,payload=payload,page_gzip=gzip.compress(page,compresslevel=1,mtime=0),
                   payload_gzip=gzip.compress(payload,compresslevel=1,mtime=0))


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
    for item in review_items.read(root, members=members, show='active', sources=sources, assign_ids=False):
        path = review_items.source_path(item)
        with guard_source(path, sources, entry=item.id):
            item = review_items.normalize_times(item, zone)
            try:
                record_assets: dict[str, str] = {}
                retain_asset(item, record_assets)
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
    card_motion = page_frame.CardMotion.read(settings)
    later = later_settings(settings)
    title_limit = conf.read_or_repository('production').whole('question_title_max_chars')
    index = index_json(entries)
    notices = tuple(sources.notices.values())
    retained = repr((records, assets, members.rows, browser, words_rows, refresh_seconds, card_motion, zone.key,
                     notices, unavailable_cards, later, title_limit, names.colors))
    version = hashlib.sha256((index+retained).encode()).hexdigest()[:20]
    snapshot = ReviewSnapshot(version,records,entries,members,
        browser,words_rows,refresh_seconds,card_motion,assets,zone,sources.before_read,later,names,read_at=read_at,
        notices=notices, unavailable_cards=frozenset(unavailable_cards),
        title_limit=title_limit, submission_keys=submission_keys)
    return pack_snapshot(snapshot, index, started)


@dataclass(frozen=True)
class SnapshotUnavailable:
    """No complete snapshot has passed its first read."""


@dataclass(frozen=True)
class ReviewRecordNotPublished:
    """The response has no published record for this save."""


@dataclass(frozen=True)
class ReviewSave:
    """The saved record, its published version and any message to deliver."""
    delivery: change_page.MessageDelivery | change_page.NoMessageDelivery
    answer: str
    version: str
    item: ReviewItem | ReviewRecordNotPublished


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

    def _after_snapshot_update(self, _snapshot: ReviewSnapshot, /) -> None:
        """Run background work after the worker publishes a snapshot."""

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
            try:
                updated, item = self.publish_record(current, key)
                if (isinstance(item, ReviewItem) and isinstance(item.source, review_items.ChangeSource)
                        and fields['outcome'][0] in ('works', 'not works')):
                    answer = item.source.row.get('Verdict') or answer
                return ReviewSave(delivery, answer, updated.version, item)
            except Exception as error:
                logging.getLogger(__name__).warning('Review saved %s. Publication will retry: %s', key, error)
                return ReviewSave(delivery, answer, current.version, ReviewRecordNotPublished())
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

    def create_submission(self, request: new_item_dialog.NewItemRequest) -> PublishedSubmission:
        """File the new record and publish it from its exact native path."""
        _ = self.get()
        with self._save_lock:
            current = self.get()
            remote_staff = not staff.is_owner()
            members = staff.snapshot()
            with staff.supplied_snapshot(members) if remote_staff else nullcontext():
                saved = review_submissions.submit(request, members=members)
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
        asset_keys = (key, 'image:'+key, 'submission:'+key)
        previous_key = replaced_key or key
        previous = records.get(previous_key)
        if previous_key != key:
            _ = records.pop(previous_key, None)
        for removed in {previous_key, key}:
            for asset in (removed, 'image:'+removed, 'submission:'+removed):
                _ = assets.pop(asset, None)
        submission_keys = current.submission_keys.copy()
        if previous is not None:
            previous_origin = review_items.submission_origin(previous)
            if isinstance(previous_origin, new_item_dialog.ReviewSubmission):
                _ = submission_keys.pop(previous_origin.request_id, None)
        names = staff_names.StaffNames(members)
        if isinstance(item, ReviewRecordNotPublished) or item.state == ReviewState.DONE:
            _ = records.pop(key, None)
            entries = [entry for entry in current.entries if entry['key'] not in (key, previous_key)]
            record_version = key
        else:
            item = review_items.normalize_times(item, current.zone)
            retain_asset(item, assets)
            if (stamps is not None and current.records.get(key) == item and members == current.members
                    and key not in current.unavailable_cards
                    and all(assets.get(asset) == current.assets.get(asset) for asset in asset_keys)):
                return current
            records[key] = item
            origin = review_items.submission_origin(item)
            if isinstance(origin, new_item_dialog.ReviewSubmission):
                submission_keys[origin.request_id] = key
            validate_body_inputs(item, {asset: assets[asset] for asset in asset_keys if asset in assets})
            updated_entry = card_entry(item, members, names)
            entries = [updated_entry if entry['key'] == previous_key else entry for entry in current.entries
                       if entry['key'] != key or key == previous_key]
            if previous_key not in current.records:
                entries.append(updated_entry)
            record_version = repr((item, tuple(assets.get(asset) for asset in asset_keys)))
        version = hashlib.sha256((current.version+record_version).encode()).hexdigest()[:20]
        fresh = pack_snapshot(replace(current, version=version, members=members, names=names, records=records, assets=assets, entries=entries,
                                      source_stamps={**current.source_stamps, **stamps} if stamps is not None else current.source_stamps,
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
                self._after_snapshot_update(fresh)
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
                     if item.for_you(now))


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
    return '<section><h3>'+heading+'</h3>'+change_browser.question_paragraphs(text)+'</section>'


def answer_section(text: str) -> str:
    return '<section><h3>Answer</h3>'+change_browser.answer_html(text)+'</section>'


def waiting_change(item: ReviewItem) -> bool:
    return item.type == ReviewType.CHANGE and item.state == ReviewState.WAITING


def form(item: ReviewItem, browser: change_page.ReviewBrowser, words_rows: int, revision: str, *,
         soon: str = "In 1 hour", owner: str = '', validator: str = '') -> str:
    fields = {'row': item.key, 'token': change_browser.row_token(browser, item.key), 'revision': revision}
    hidden = ''.join(f'<input type="hidden" name="{key}" value="{html.escape(value, quote=True)}">' for key, value in fields.items())
    closes: list[str] = []
    returns: list[str] = []
    labels = {'answer': 'Answer', 'works': 'Works', 'not works': 'Does not work', 'validate': 'Ask showrunner to check',
              'question': 'Ask question', 'feedback': 'Send feedback', 'read': 'Mark read',
              'approve': 'Approve', 'decline': 'Decline'}
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
    heading = (page_frame.ReturningActionHeading.SENDS_TO_SHOWRUNNER if item.type == ReviewType.CHANGE
               else page_frame.ReturningActionHeading.ITEM_RETURNS)
    later = ''
    if (item.state not in (ReviewState.DONE, ReviewState.VALIDATING)
            and item.type != ReviewType.RESPONSE and not waiting_change(item)):
        presets = ''.join(f'<button type="button" role="menuitem" tabindex="-1" data-later-preset="{key}">'
                          +f'<span>{html.escape(label or soon)}</span><small></small></button>' for key, label in LATER_PRESETS)
        later = ('<button type="button" aria-haspopup="menu" aria-expanded="false" data-later-toggle>Later <span aria-hidden="true">▾</span></button>'
                 +'<div role="menu" aria-label="Hide until" data-later-menu hidden>'+presets+'<hr>'
                 +'<button type="button" role="menuitem" tabindex="-1" data-later-choose><span>Choose a time…</span></button>'
                 +record_table.date_time_picker(field='later', action='outcome', value='later')+'</div>')
    words = ''
    if closes or returns or later:
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


def details(item: ReviewItem, inputs: ReviewBodyInputs) -> str:
    names = inputs.names
    source = item.source
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
        body = (section('You asked', response.question)+'<section class="response-answer"><h3>Answer from '
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
                    exchange += '<section><h3>Answer from '+escaped+'</h3>'+change_browser.answer_html(question.answer)+'</section>'
        else:
            if isinstance(question, change_page.OpenQuestion):
                body += section('Feedback' if isinstance(question, change_page.OpenFeedback) else 'Question', question.text)
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
    logs = ''.join('<li><time datetime="'+entry.time.isoformat()+'">'
        +entry.time.astimezone(inputs.zone).strftime('%b %d %H:%M')+'</time><span>'
        +html.escape('Answered' if response is not None and entry.time == response.answered_at
                     and entry.who == item.owner and entry.words == 'Answer: '+response.answer else entry.words)
        +' <small>— '+names.html(entry.who, owner if response is not None and entry.who == item.owner else entry.who)
        +'</small></span></li>' for entry in reversed(item.log))
    footer = ('' if response is not None else '<p class="review-source">Owner: '+names.html(item.owner, owner)
              +' · Source: '+names.html(item.source_unit, review_items.name(item.source_unit, inputs.members))+'</p>')
    fallback = '<section data-title-fallback hidden><h3>Item</h3><p>'+html.escape(item.title)+'</p></section>'
    body += inputs.assets.get('submission:'+item.key, '')
    return ('<div class="list-panel review-panel '+item.type+'"><div>'+fallback+handoff+'<div data-review-body>'+body+'</div></div><div><section><h3>Log · newest first</h3>'
            +'<ol class="list-log">'+logs+'</ol></section>'+exchange+'</div>'
            +form(item, inputs.browser, inputs.words_rows, record_revision(item, inputs.assets), soon=inputs.soon_label,
                  owner=owner, validator=validator)+footer+'</div>')


def owner_name(item: ReviewItem, members: staff.StaffSnapshot) -> str:
    return 'Owner unavailable' if isinstance(item.source, review_items.UnownedQuestionSource) else review_items.name(item.owner, members)


def render(root: Path, *, show: str = 'open') -> bytes:
    """Build a standalone page; the HTTP server uses its retained snapshot."""
    _ = change_page.console_folder(root)
    return build_snapshot(root).render(show=show)


def render_shell(snapshot: ReviewSnapshot, index: str, started: float, read_at: float) -> bytes:
    """Build the controls and compact index without any card body."""
    show = 'open'
    entries = snapshot.entries
    owners = {entry['owner']:(entry['ownerName'], entry['ownerHtml']) for entry in entries}
    owners.update({entry['sourceOwner']:(entry['sourceName'], entry['sourceHtml'])
                   for entry in entries if entry['type'] in review_items.INVESTIGATION_TYPES})
    owner_chips = '<button id="review-owner-all" type="button" data-owner-chip="" aria-pressed="true">All <b></b></button>'
    for owner, (name, markup) in sorted(owners.items(), key=lambda pair: pair[1][0].casefold()):
        identity, label = html.escape(owner, quote=True), html.escape(name, quote=True)
        owner_chips += (f'<button id="review-owner-{identity}" type="button" data-owner-chip="{identity}" '
                        +f'data-owner-name="{label}" aria-pressed="false">{markup or label} <b></b></button>')
    chips = '<button id="review-type-all" type="button" data-type-chip="" aria-pressed="true">All <b></b></button>'
    chips += ('<button type="button" data-type-chip="investigations" aria-pressed="false" hidden>'
              'Investigations and Bugs <b></b></button>')
    chips += ''.join('<button id="review-type-'+kind+'" type="button" data-type-chip="'+kind+'" aria-pressed="false">'+icon(kind)+LABELS[kind]
                     +' <b></b></button>' for kind in TYPE_CHIPS)
    shows = ''.join('<button id="review-show-'+value.lower().replace(' ', '-')+'" type="button" data-show-chip="'
                    +value+'" aria-pressed="'+str(value == show).lower()+'">'+label
                    +' <b></b></button>' for value, label in SHOWS)
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      +'<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    new_button = '<button class="review-new" type="button" data-review-new><span aria-hidden="true">+</span>New</button>'
    content = (record_table.STYLE+record_table.DATE_TIME_STYLE+STYLE+page_frame.title('Review', 'review', action=new_button)
        +f'<div data-review data-version="{snapshot.version}" data-show="{show}" data-refresh-seconds="{snapshot.refresh_seconds}" '
        +snapshot.card_motion.attributes()+' '
        +f'data-title-limit="{snapshot.title_limit}" data-zone="{html.escape(snapshot.zone.key, quote=True)}" '
        +f'data-later="{html.escape(json.dumps(dict(snapshot.later)), quote=True)}">'
        +record_table.labelled_chip_row('Type', chips, chip_classes='review-types')
        +record_table.labelled_chip_row('State', shows, chip_classes='review-types review-shows', chip_id='list-show')
        +record_table.labelled_chip_row('Owner', owner_chips, chip_classes='review-types', chip_id='list-owner')
        +'<div class="list-filters">'
        +'<button type="button" data-clear-linked-reviews aria-label="Clear linked reviews filter"'
        +' aria-pressed="true" hidden>Linked reviews <span aria-hidden="true">×</span></button>'
        +'<label class="list-search-label" for="list-search">Search</label><input type="search" id="list-search" placeholder="Filter by id or words">'
        +'</div><p data-missing-item role="status" hidden></p><p role="status" data-review-notice hidden></p><p data-hidden-action-status role="status" hidden></p>'
        +'<ul data-review-source-notices'+('' if snapshot.notices else ' hidden')+'>'
        +''.join('<li>'+html.escape(notice)+'</li>' for notice in snapshot.notices)+'</ul>'
        +'<script>'+CARD_REQUEST_SCRIPT+'</script><script type="application/json" data-review-index>'+index+'</script>'
        +'<div class="list-table"><table aria-label="Review">'
        +'<colgroup><col style="width:17%"><col style="width:13%"><col style="width:14%"><col style="width:37%">'
        +'<col style="width:12%"><col style="width:7%"></colgroup><thead><tr>'+headers+'</tr></thead>'
        +'<tbody data-window-before></tbody><tbody data-window-after></tbody>'
        +'</table><p data-empty hidden>No items in this view.</p></div></div>'
        +new_item_dialog.render()+'<script>'+SCRIPT+'</script>')
    return page_frame.rendered(change_page.document(content, 'Review'), started, read_at)


def review_fields(body: str) -> dict[str, list[str]]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    required = {'row', 'token', 'outcome', 'feedback'}
    if not required <= fields.keys() <= required | {'later', 'revision'} or any(len(values)!=1 for values in fields.values()):
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
            and page.saved.get('question_id') == reply.reply_token and page.saved.get('question_text') == pending['text'])


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
            update_registration.review_sent_question(source.record, scope, 'read', '', '')
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
    def validate() -> ReviewItem:
        current = review_items.read_current(root, retained, staff.snapshot())
        if current.state == ReviewState.DONE:
            raise review_items.ReviewRecordChanged('This item is already Done.')
        assets: dict[str, str] = {}
        try:
            retain_asset(current, assets)
        except FileNotFoundError:
            raise review_items.ReviewRecordMissing('This item is no longer on the list.') from None
        if fields.get('revision', [''])[0] != record_revision(current, assets):
            raise review_items.ReviewRecordChanged('This item changed. Check it again before saving.')
        return current

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
                question, cast(change_page.QuestionOutcome, outcome), words, status, exact_record=True,
                verify_revision=validate)
    elif isinstance(source, review_items.InvestigationSource):
        delivery, answer = review_actions.investigation_action(item, outcome, words, status, verify_revision=validate)
    else:
        delivery, answer = review_actions.decision_action(item, outcome, words, status, verify_revision=validate)
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
                            review_actions.decision_action(item,outcome,words,status))
        if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery):
            delivery = review_actions.track_question(delivery, key, words, status)
        return delivery, answer
    _ = fields.pop('later',None)
    _ = fields.pop('revision',None)
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


STYLE = '''<style>
.review-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.review-new span{font-size:1.2em;line-height:1}
.review-waiting-why{display:block;color:var(--ink-3);font-size:.85em}
.review-attachments img{max-width:min(240px,100%);max-height:160px;object-fit:contain;margin-inline-end:8px}
[data-review] .record-filter-row{margin-bottom:12px}
.record-filter-row>.review-types{gap:8px}
.review-types button{max-width:100%;overflow-wrap:anywhere}
:is(.review-types button,[data-clear-linked-reviews],.review-new){display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;min-height:36px;
background:var(--card);border:1px solid var(--line);border-radius:999px;color:var(--ink-2);padding:5px 14px;margin:0}
.page-heading-actions>.review-new{flex:none;color:var(--accent);white-space:nowrap}
:is(.review-types button,[data-clear-linked-reviews])[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
#review-show-validating[data-empty-count]{opacity:.55}
.review-icon{display:inline-flex;vertical-align:middle;margin-right:5px}.review-icon svg{width:18px;height:18px}
.question{--type-color:#b99aff}.change{--type-color:#62b7ef}.investigation{--type-color:#30c1ad}
.proposal{--type-color:#f27ab8}.efficiency{--type-color:#a3d63a}.bug{--type-color:#ed876b}.response{--type-color:#edc46d}
.review-icon{color:var(--type-color)}.review-panel{border-left-color:var(--type-color);grid-template-columns:1.25fr 1fr;padding:0 20px 18px 58px;gap:4px 28px}
.response-answer time{display:block;margin-bottom:8px;color:var(--ink-3);font-size:12px}
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
@media(max-width:960px){[data-review] .list-row td:last-child::before{content:'Age: '}
.review-panel{grid-template-columns:1fr;padding:0 14px 16px}}
@media(max-width:480px){[data-review] .record-filter-row{grid-template-columns:minmax(0,1fr)}}
</style>'''

CARD_REQUEST_SCRIPT = r'''
function requestReviewCard(key,version){
 return fetch(location.pathname+'?'+new URLSearchParams({body:key,version}),{cache:'no-store'})
  .then(async response=>({response,text:response.ok ? await response.text() : ''}));
}
const firstReviewCard=(()=>{
 const fragment=new URLSearchParams(location.hash.slice(1));
 const key=fragment.get('jump') || fragment.get('item') || new URLSearchParams(location.search).get('item');
 if(!key) return null;
 const version=document.querySelector('[data-review]').dataset.version;
 const result=requestReviewCard(key,version);
 void result.catch(()=>{});
 return {key,version,result};
})();
'''

SCRIPT = (page_frame.REFRESH_SCRIPT + page_frame.CARD_SUBMIT_SCRIPT + record_table.SCRIPT
          + record_table.DATE_TIME_SCRIPT + console.REVIEW_PLACE_SCRIPT + r'''(reviewPlace => {
let content=document.querySelector('[data-review]');
const drafts=new Map(),saved=new Map(),bodies=new Map(),bodyRequests=new Map();
const pendingSaves=new Map();
const scrollDepartures=new Set();
const savedVerdictsKey='review-saved-verdicts:'+location.pathname;
const kinds=''' + json.dumps(WIRE_TYPES) + ''',states=''' + json.dumps(INDEX_STATES) + r''';
let index=readIndex(JSON.parse(content.querySelector('[data-review-index]').textContent));
let focusAfterLoad=null,firstRequest=firstReviewCard;
let deferredLink=null,revealCreated=null;
function indexEntry(payload,row,at){
 const owner=payload.n[row[3]],source=payload.n[row[4]];
 return {key:row[0],id:row[1] || row[0],type:row[13]?'response':kinds[row[2]],owner:owner[0],sourceOwner:source[0],
  ownerName:owner[1],sourceName:source[1],title:row[5],state:states[row[6]],filed:row[7],updated:row[8],
   hiddenUntil:row[9],ownerHtml:owner[2],sourceHtml:source[2],urgency:row[10] || 'normal',handoff:row[11],
   waitingWhy:row[12],order:at+row[6]*payload.r.length};
}
function readIndex(payload){
 return new Map(payload.r.map((row,at)=>[row[0],indexEntry(payload,row,at)]));
}
function allEntries(){return [...index.values()];}
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
function updateHandoffAges(root=content){
 for(const stamp of root.querySelectorAll('[data-validating-age]')){
  const label=reviewAge(Date.parse(stamp.dateTime),true);
  if(stamp.textContent!==label) stamp.textContent=label;
 }
}
function summary(entry){
 const node=document.createElement('tbody'),panel=detailId(entry.key);
 Object.assign(node.dataset,{listItem:'',key:entry.key,number:entry.id.toLowerCase(),title:entry.title.toLowerCase(),
  owner:entry.owner,sourceOwner:entry.sourceOwner,sourceName:entry.sourceName,state:entry.state,type:entry.type,
  order:entry.order,updated:entry.updated,parked:'false',sortValues:JSON.stringify(entrySortValues(entry))});
 const limit=Number(content.dataset.titleLimit),first=entry.title.split(/\r?\n/)[0],short=first.slice(0,limit);
 const stamp=reviewAge(entry.filed*1000);
 const icon=content.querySelector('[data-type-chip="'+entry.type+'"] .review-icon').outerHTML;
 const validating=entry.state==='Validating' && entry.type==='change';
 const handoff=entry.handoff?.[0];
 const age=handoff ? ' · Validating for <time data-validating-age datetime="'+escapeText(handoff)+'">'
  +reviewAge(Date.parse(handoff),true)+'</time>' : '';
 const handoffLine=validating && entry.owner ? '<span class="review-handoff">With '+colored(entry.ownerName,entry.ownerHtml)+age+'</span>' : '';
 const waitingLine=entry.state==='Waiting' && entry.waitingWhy ?
  '<span class="review-waiting-why">'+escapeText(entry.waitingWhy)+'</span>' : '';
 const cells=['<button type="button" class="list-toggle" aria-expanded="false" aria-controls="'+panel+'">'
  +'<span class="list-chevron" aria-hidden="true">›</span>'+escapeText(entry.id)+'</button>',
  icon+entry.type[0].toUpperCase()+entry.type.slice(1),colored(entry.ownerName,entry.ownerHtml)
  +(entry.sourceName!==entry.ownerName ? '<small>· '+colored(entry.sourceName,entry.sourceHtml)+'</small>' : ''),
  '<strong class="review-title" title="'+escapeText(entry.title)+'">'+escapeText(short===entry.title ? short : short+'…')+'</strong>'+handoffLine+waitingLine+(entry.urgency!=='normal' ? '<small>'+escapeText({user:'Important',blocks:'Blocks work',soon:'Due soon',later:'Later'}[entry.urgency])+'</small>' : ''),
  '<span class="list-state '+(entry.state==='Open' ? 'warning' : 'muted')+'">'+entry.state+'</span>',stamp];
 node.innerHTML='<tr class="list-row">'+cells.map((cell,at)=>'<td'+(at===0 ? ' class="record-id"' : '')+' data-label="'+['Id','Type','Owner','Item','State','Age'][at]+'">'+cell+'</td>').join('')
  +'</tr><tr data-detail id="'+panel+'" hidden><td colspan="6"><p role="status">Loading the item…</p></td></tr>';
 return node;
}
function matches(entry,{owner='',show='all',query='',type='',sourceOwner='',keys}){
 const linked=keys?.has(entry.key);
 return (!keys?.size || linked) && (linked || view.linkedItem===entry.key || entry.hiddenUntil<=Date.now()) &&
  (!owner || entry.owner===owner || isInvestigation(entry) && entry.sourceOwner===owner) &&
  (!type || (type==='investigations' ? isInvestigation(entry) : entry.type===type)) &&
  (!sourceOwner || entry.sourceOwner===sourceOwner) &&
  (!query || entry.id.toLowerCase().includes(query) || entry.title.toLowerCase().includes(query)) &&
  (show==='all' || entry.state===(show==='open' ? 'Open' : show));
}
function isInvestigation(entry){return entry.type==='investigation' || entry.type==='bug';}
function matching(filters){return allEntries().filter(entry=>!pendingSaves.has(entry.key) && matches(entry,filters));}
function savedEntry(key,result){
 if(result.state==='Done'){index.delete(key);return;}
 const entry=index.get(key);if(!entry) return;
 entry.state=result.state;entry.hiddenUntil=result.later;
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
const view=createRecordTable({content:()=>content,single:true,
 entries:allEntries,mount:summary,mounted:()=>loadBodies(),
 focusKey:()=>focusAfterLoad?.key,
 unmount:row=>{if(!saved.has(row.dataset.key)) rememberForm(row.querySelector('[data-review-form]'));},
 place:params=>{
  reviewPlace(params);
  if(params.get('show')==='Waiting') params.set('show','all');
  if(params.get('type')==='response' && params.get('show')==='Validating') params.set('show','open');
 },
 persistLinkedTarget:true,
 missingItem:()=> 'This review is no longer on the Review page.',
 resolveOwner:owner=>{
  const button=[...content.querySelectorAll('[data-owner-chip]')].find(button=>
   button.dataset.ownerChip===owner || button.dataset.ownerName===owner);
  return button ? button.dataset.ownerChip : owner;
 },
 selectOwner:({owner,type,query,keys})=>!owner || allEntries().some(entry=>matches(entry,{owner,type,query,keys})) ? owner : '',
 hasKey:key=>allEntries().some(entry=>entry.key===key),
  shows:['open','Validating','all'],
 visible:(data,filters)=>{
  const key=data.key,entry=index.get(key),departure=saved.get(key);
  if(!entry || pendingSaves.get(key)?.departing) return false;
  if(departure?.handed && departure.awaitingSnapshot && view.linkedItem===key) return false;
  return (!departure || departure.handed && entry.state!=='Open') && matches(entry,filters);
 },
 sortValues:data=>data.sortValues===undefined ? entrySortValues(data) : undefined,
 applied:(_items,{type,show,owner,query,sourceOwner,keys})=>{
  content.dataset.show=show;loadBodies();
  const eligibleOwners=allEntries().filter(entry=>matches(entry,{type,query,keys}));
  const shownOwners=matching({show,type,query,keys});
  const matchesOwner=(entry,id)=>!id || entry.owner===id || isInvestigation(entry) && entry.sourceOwner===id;
  content.querySelectorAll('[data-owner-chip]').forEach(button=>{
   const id=button.dataset.ownerChip;
   button.hidden=Boolean(id) && id!==owner && !eligibleOwners.some(entry=>matchesOwner(entry,id));
   button.querySelector('b').textContent=shownOwners.filter(entry=>matchesOwner(entry,id)).length;
  });
  content.querySelectorAll('[data-show-chip]').forEach(button=>{
   const show=button.dataset.showChip;
   button.hidden=type==='response' && show==='Validating';
   const count=matching({type,owner,query,sourceOwner,keys,show}).length;
   button.querySelector('b').textContent=count;
   button.toggleAttribute('data-empty-count',count===0);
  });
  content.querySelectorAll('[data-type-chip]').forEach(button=>{
   if(button.dataset.typeChip==='investigations') button.hidden=type!=='investigations';
   button.setAttribute('aria-pressed',String(button.dataset.typeChip===type));
   button.querySelector('b').textContent=matching({owner,show,query,sourceOwner,keys,type:button.dataset.typeChip}).length;
  });
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
function updateTitleFallback(panel){
 const fallback=panel.querySelector('[data-title-fallback]');
 const body=panel.querySelector('[data-review-body]').cloneNode(true);
 body.querySelectorAll('[data-title-fallback],[data-title-ignore],.list-log,form,button,input,select,textarea,label,script,style')
  .forEach(node=>node.remove());
 const blocks=new Set('ADDRESS ARTICLE ASIDE BLOCKQUOTE BR DD DETAILS DIALOG DIV DL DT FIELDSET FIGCAPTION FIGURE FOOTER H1 H2 H3 H4 H5 H6 HEADER HGROUP HR LI MAIN MENU NAV OL P PRE SECTION SUMMARY TABLE TBODY TD TFOOT TH THEAD TR UL'.split(' '));
 const bodyText=node=>{
  if(node.nodeType===Node.TEXT_NODE) return node.textContent;
  const text=[...node.childNodes].map(bodyText).join('');
  return blocks.has(node.nodeName) ? ' '+text+' ' : text;
 };
 const plain=text=>text.replace(/\s+/g,' ').trim();
 fallback.hidden=plain(bodyText(body)).includes(plain(fallback.querySelector('p').textContent));
}
function loadBodies(){
 view.opened.forEach(key=>{
  const row=mountedRow(key),pending=saved.get(key);
  if(!row || row.dataset.bodyLoaded || (pending?.handed && pending.awaitingSnapshot)) return;
  const install=text=>{
   const current=mountedRow(key);
   if(!current || current.dataset.bodyLoaded) return;
   current.dataset.bodyLoaded='true';current.querySelector('[data-detail]>td').innerHTML=text;
   const panel=current.querySelector('.review-panel');if(panel) updateTitleFallback(panel);
   updateHandoffAges(current);bindForm(current);restoreFocus(key);view.drawWindow();
  };
  if(bodies.has(key)){install(bodies.get(key));return;}
  if(bodyRequests.has(key)) return;
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
    bodies.set(key,text);install(text);
   }).catch(error=>{
    const target=mountedRow(key)?.querySelector('[data-detail]>td');
    if(target && bodyRequests.get(key)===request){
     target.closest('[data-list-item]').dataset.bodyLoaded='failed';
     target.innerHTML='<p role="status">The item is not available: '+escapeText(error.message)+'</p>';
    }
   })
   .finally(()=>{if(bodyRequests.get(key)===request) bodyRequests.delete(key);});
  bodyRequests.set(key,request);
 });
}
function rememberForm(form){
 if(!form?.elements.feedback) return;
 const row=form.closest('[data-list-item]');
 if(pendingSaves.get(row.dataset.key)?.saved) return;
 const choices=[...row.querySelectorAll('input[name=choice]')];
 drafts.set(row.dataset.key,{feedback:form.elements.feedback.value,later:form.elements.later?.value ?? '',
  choice:choices.find(choice=>choice.checked)?.value || ''});
}
function drawSaveState(key,form=mountedRow(key)?.querySelector('[data-review-form]')){
 if(!form) return;
 const pending=pendingSaves.get(key);
 setCardSubmitting(form,Boolean(pending));
 form.querySelector('[data-send-status]').textContent=pending?.notice || view.actionNotice(form.closest('[data-list-item]'));
}
function bindForm(row){
 const form=row.querySelector('[data-review-form]');if(!form || form.dataset.bound) return;
 form.dataset.bound='true';
 const key=row.dataset.key,words=form.elements.feedback,later=form.elements.later;
 drawSaveState(key,form);
 if(!words) return;
 const choices=[...row.querySelectorAll('input[name=choice]')];
 if(drafts.has(key)){
  const draft=drafts.get(key);words.value=draft.feedback;if(later) later.value=draft.later;
  choices.forEach(choice=>choice.checked=choice.value===draft.choice);
 }
 const remember=()=>rememberForm(form);
 choices.forEach(choice=>choice.addEventListener('change',()=>{words.value=choice.value;remember();words.focus();}));
 [words,later].filter(Boolean).forEach(field=>{field.addEventListener('input',remember);field.addEventListener('change',remember);});
 const send=async(outcome,until)=>{
   if(cardSubmitBlocked(form,view.rowFading(key)) || pendingSaves.has(key)) return;
   const data=new URLSearchParams(new FormData(form));data.set('outcome',outcome);
   if(outcome==='later') data.set('later',until);
   remember();
   const notice=content.querySelector('[data-review-notice]');
   notice.hidden=true;notice.textContent='';
   const validationQuestion=row.dataset.state==='Validating' && outcome==='question';
   const pending={notice:'Saving…',saved:false,departing:!validationQuestion,top:scrollY};
   pendingSaves.set(key,pending);pageRefresh.abandon();drawSaveState(key);
   const submission=view.startAction(row);
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    const result=await response.json();
    if(!response.ok){
     const reason=result.reason || 'HTTP '+response.status;
     if(response.status===409){
      notice.textContent='Not saved: '+reason;notice.hidden=false;
     }
     throw new Error(reason);
    }
    pending.saved=true;
    drafts.delete(key);
    if(validationQuestion){
     words.value='';
     view.showActionResult(row,submission,key,{kind:'success',message:'Saved: '+result.answer});
     return;
    }
    const handed=row.dataset.type==='change' && ['validate','question','feedback'].includes(outcome);
    const waitState=outcome==='validate' ? 'Validating' : 'Waiting';
    const marker={updated:row.dataset.updated,previousState:row.dataset.state,
     handed,state:result.state ?? (handed || outcome==='question' ? waitState : outcome==='later' ? row.dataset.state : 'Done'),
     type:result.type,
     later:result.later ?? (outcome==='later' ? Date.parse(data.get('later')) : 0),
     question:outcome==='question',
     closes:!handed && !['question','later'].includes(outcome),awaitingSnapshot:true};
    const applySaved=()=>{
     saved.set(key,marker);savedEntry(key,marker);
     if(marker.closes) storeSavedVerdicts();
     pendingSaves.delete(key);
     if(!isDeferredLink(key)) view.closeCard(key);
     view.showActionResult(row,submission,key,{kind:'success',message:'Saved: '+result.answer});
     if(handed && parent!==window) parent.postMessage('for-you-completed',location.origin);
    };
    if(outcome==='later') await afterRowAnimations(applySaved);
    else applySaved();
   }catch(error){
    pendingSaves.delete(key);
    view.showActionResult(row,submission,key,{kind:'failure',message:'Not saved: '+error.message});
   }
   finally{
    pendingSaves.delete(key);setCardSubmitting(form,false);drawSaveState(key);
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
    if(!pendingSaves.size) void pageRefresh.refresh(true);
   }
  };
  form.addEventListener('submit',event=>{
   event.preventDefault();
   const outcome=event.submitter.value;
   if(outcome==='later'){
    const at=dateTimeDraftEpoch(content.dataset.zone,later.value);
    if(!(at>Date.now())) return;
    closeDateTimePicker(form.querySelector('[data-later-menu]'),false);
    void send(outcome,new Date(at).toISOString());return;
   }
   void send(outcome);
  });
  if(later) bindLater(form,until=>void send('later',until));
}
function laterTimes(now){
 const settings=JSON.parse(content.dataset.later),today=zoneParts(content.dataset.zone,now);
 const at=(days,minutes)=>zoneEpoch(content.dataset.zone,today.year,today.month,today.day+days,minutes);
 const weekday=new Date(Date.UTC(today.year,today.month-1,today.day)).getUTCDay();
 return {soon:now+settings.soon*60000,
  evening:today.hour*60+today.minute<settings.eveningUntil ? at(0,settings.evening) : 0,
  morning:at(1,settings.morning),week:at((settings.weekDay-weekday+7)%7 || 7,settings.weekTime)};
}
function bindLater(form,pick){
 const shown=new Intl.DateTimeFormat('en-US',{timeZone:content.dataset.zone,weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
 const prepareMenu=menu=>{
  const times=laterTimes(Date.now());
  menu.querySelectorAll('[data-later-preset]').forEach(item=>{
   const at=times[item.dataset.laterPreset];item.hidden=!at;item.dataset.at=at ? String(at) : '';
   item.querySelector('small').textContent=at ? shown.format(at) : '';
  });
 };
 form.querySelectorAll('[data-later-preset]').forEach(item=>item.addEventListener('click',()=>{
  // Resolve the time again at the click, so a menu left open does not send a stale time.
  const at=laterTimes(Date.now())[item.dataset.laterPreset] || Number(item.dataset.at);
  closeDateTimePicker(form.querySelector('[data-later-menu]'),false);pick(new Date(at).toISOString());
 }));
 bindDateTimePicker(form,{zone:content.dataset.zone,defaultMinutes:()=>JSON.parse(content.dataset.later).morning,
  summaryPrefix:'Hides until ',prepareMenu});
}
function bind(){view.replace(false);}
content.addEventListener('click',event=>{
 if(content.dataset.show==='Validating' && event.target.closest('[data-type-chip="response"]'))
  document.getElementById('review-show-open').click();
},true);
function revealNewItem(){
 if(!revealCreated || !index.has(revealCreated.key)) return false;
 const {key,id}=revealCreated;
 revealCreated=null;
 if(view.revealMatchingItem(key)) return true;
 const notice=content.querySelector('[data-review-notice]');
 notice.textContent='Saved '+id+'.';notice.hidden=false;
 return false;
}
async function refresh(_supersede=false,current=()=>true){
 if(pendingSaves.size || dateTimePickerOpen(content)) return;
 const canApply=()=>!pendingSaves.size && current();
 const response=await fetch(location.pathname+'?'+new URLSearchParams({version:'1'}),{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const version=await response.json();
 if(!canApply() || dateTimePickerOpen(content)) return;
 if(version.version===content.dataset.version){
  return afterRowAnimations(()=>{
   for(const [key,result] of saved) if(result.later && Date.now()>=result.later) saved.delete(key);
   content.querySelectorAll('[data-body-loaded="failed"]').forEach(row=>delete row.dataset.bodyLoaded);
   view.apply(false);revealNewItem();content.dispatchEvent(new Event('review-refreshed'));return version.facts;
  },canApply);
 }
 const fresh=await fetch(location.pathname+'?'+new URLSearchParams({index:'1',version:version.version}),{cache:'no-store'});
 if(!fresh.ok) throw new Error('HTTP '+fresh.status);
 const payload=await fresh.json();
 if(!canApply() || dateTimePickerOpen(content)) return;
 return afterRowAnimations(()=>{
  applyReviewIndex(payload);
  return {...payload.facts,answeredAt:version.facts.answeredAt};
 },canApply);
}
function applyReviewIndex(payload){
 const top=scrollY;focusAfterLoad=rememberFocus();
 content.querySelectorAll('[data-review-form]').forEach(rememberForm);
 index=readIndex(payload.index);content.dataset.version=payload.version;
 if(payload.presentation){
  content.dataset.zone=payload.presentation.zone;
  content.dataset.later=JSON.stringify(payload.presentation.later);
  content.dataset.titleLimit=payload.presentation.titleLimit;
  content.dataset.cardFadeOutMs=payload.presentation.cardFadeOutMs;
  content.dataset.cardFadeInMs=payload.presentation.cardFadeInMs;
 }
 content.querySelector('[data-review-index]').textContent=JSON.stringify(payload.index);
 const notices=content.querySelector('[data-review-source-notices]');
 notices.replaceChildren(...(payload.notices || []).map(text=>{
  const line=document.createElement('li');line.textContent=text;return line;
 }));
 notices.hidden=!notices.children.length;
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
  if(!entry || (result.later && Date.now()>=result.later)) {saved.delete(key);continue;}
  // The index supplies the state and update time for each row.
  const submitted=snapshotPrecedesSave(entry,result);
  if(!submitted && (result.restored || result.handed || entry.state!==result.state)){saved.delete(key);continue;}
  if(!result.closes && !result.question && !result.later && !submitted) saved.delete(key);
  else result.awaitingSnapshot=submitted;
 }
 storeSavedVerdicts();
 for(const [key,result] of saved) savedEntry(key,result);
 bind();view.apply(false);
 if(focusAfterLoad) restoreFocus(focusAfterLoad.key);
 if(focusedOwner!==undefined && (!document.activeElement?.hasAttribute('data-owner-chip') || document.activeElement.hidden))
  menu.querySelector('[aria-pressed="true"]')?.focus({preventScroll:true});
 content.dataset.loaded='true';
 if(!revealNewItem()) scrollTo({top,behavior:'instant'});
 content.dispatchEvent(new Event('review-refreshed'));
}
bind();
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
setInterval(updateHandoffAges,5000);
document.querySelector('[data-review-new]').addEventListener('click',event=>{
 window.reviewNewItem.open({trigger:event.currentTarget});
});
document.addEventListener('review-item-created',event=>{
 const {key,id}=event.detail ?? {};
 if(typeof key!=='string' || typeof id!=='string') return;
 const notice=content.querySelector('[data-review-notice]');
 notice.textContent='';notice.hidden=true;
 revealCreated={key,id};
 void pageRefresh.refresh(true);
});
function itemLinkKey(){
 const fragment=new URLSearchParams(location.hash.slice(1)),search=new URLSearchParams(location.search);
 const field=name=>fragment.get(name) ?? search.get(name);
 return field('jump') || (field('item') ?? field('row'));
}
function followItemLink(options){
 const applyLink=unappliedOnly=>view.followItemLink({...options,unappliedOnly,beforeJump:key=>{
  if(pendingSaves.has(key)) return false;
  const marker=saved.get(key);
  if(marker && !marker.closes && !marker.question && !marker.handed) saved.delete(key);
  return true;
 }});
 const key=view.hasActiveRowAnimations ? itemLinkKey() : null;
 if(!key || !content.querySelector('[data-record-motion] .list-toggle[aria-controls="'+CSS.escape(detailId(key))+'"]'))
  return applyLink(options?.unappliedOnly);
 const href=location.href;
 void afterRowAnimations(()=>applyLink(true),()=>location.href===href);
 return true;
}
const frame=window.frameElement;
if(frame?.reviewPlace){
 const {place,table}=frame.reviewPlace;
 delete frame.reviewPlace;
 if(!history.state?.recordTable && place===location.pathname+location.search+location.hash)
  history.replaceState({recordTable:table},'');
}
if(history.state?.recordTable) view.restorePlace();
else if(!followItemLink()){view.place();view.apply(true,{normalizeOwner:true});}
function isDeferredLink(key){return deferredLink?.key===key && deferredLink.href===location.href;}
function restorePlace(event){
 const key=itemLinkKey();
 if(pendingSaves.has(key)){
  deferredLink={key,href:location.href};return;
 }
 deferredLink=null;
 if(event.type==='hashchange' && !history.state?.recordTable && followItemLink({inheritFilters:true})) return;
 view.restorePlace(event);
}
addEventListener('popstate',restorePlace);
addEventListener('hashchange',restorePlace);
})(legacyReviewPlace);''')
