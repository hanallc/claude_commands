"""Show questions, changes, investigations, proposals and efficiencies in one review table."""
from __future__ import annotations

import html
import hashlib
import gzip
import threading
import logging
from dataclasses import dataclass, field, replace
import json
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import TypedDict, cast
from urllib.parse import parse_qs, parse_qsl, urlencode
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.lib.user_zone import CONF as USER_ZONE_CONF, user_zone
from scripts.production import change_review, proposal_issue, staff, update_registration
from scripts.production.record_store import state_root
from scripts.lib import urgency
from scripts.review_links import change_browser, change_page, console, page_frame, record_table, review_items, review_link
from scripts.review_links import review_legacy
from scripts.review_links import review_actions
from scripts.review_links.review_items import ReviewItem, ReviewSourceStamps, ReviewState, ReviewType, guard_source
from scripts.review_links.review_items import source_stamp as source_stamp

PAGE_PATH = '/console/review.html'
HEADERS = ('Id', 'Type', 'Owner', 'Item', 'State', 'Age')
TYPES = tuple(ReviewType)
ICONS = {ReviewType.QUESTION: 'circle-help', ReviewType.CHANGE: 'diff', ReviewType.INVESTIGATION: 'microscope',
         ReviewType.PROPOSAL: 'lamp', ReviewType.EFFICIENCY: 'zap'}
SHOWS = (('open', 'Open'), ('Waiting', 'Waiting'), ('Done', 'Done'), ('all', 'All'))
LABELS = {ReviewType.QUESTION: 'Questions', ReviewType.CHANGE: 'Changes', ReviewType.INVESTIGATION: 'Investigations',
          ReviewType.PROPOSAL: 'Proposals', ReviewType.EFFICIENCY: 'Efficiencies'}
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
    urgency: str


def record_revision(item: ReviewItem, assets: Mapping[str, str]) -> str:
    """Identify the complete retained record and its displayed assets."""
    source = item.source
    # A Change verdict follows the current production when staff move.
    record = (source.path, source.row, source.slots, item.times.later) if isinstance(source, review_items.ChangeSource) else item
    retained = (record, assets.get(item.key, ''), assets.get('image:'+item.key, ''))
    return hashlib.sha256(repr(retained).encode()).hexdigest()[:16]


def filter_index(records: list[ReviewItem], members: staff.StaffSnapshot) -> list[ReviewIndexEntry]:
    """Build the one index for Review counts, filters and column sorting."""
    return [ReviewIndexEntry(key=item.key, id=item.id, type=item.type, owner=str(item.owner),
            sourceOwner=str(item.source_unit), ownerName=owner_name(item, members), title=item.title,
            state=item.state, filed=int(item.times.filed.timestamp()), updated=item.times.updated.isoformat(),
            hiddenUntil=item.times.later.timestamp()*1000 if not isinstance(item.times.later,review_items.NoLaterTime) else 0,
            sourceName=review_items.name(item.source_unit,members), urgency=item.urgency.level) for item in records]


def index_json(entries: list[ReviewIndexEntry]) -> str:
    """Share names and encode one compact row per item for the wire."""
    names = dict.fromkeys((identity, name) for entry in entries for identity,name in (
        (entry['owner'],entry['ownerName']),(entry['sourceOwner'],entry['sourceName'])))
    references = {pair: at for at,pair in enumerate(names)}
    rows = [[entry['key'], '' if entry['id']==entry['key'] else entry['id'], TYPES.index(entry['type']),
             references[(entry['owner'],entry['ownerName'])], references[(entry['sourceOwner'],entry['sourceName'])],
             entry['title'], tuple(ReviewState).index(entry['state']), entry['filed'], entry['updated'],
             entry['hiddenUntil'], '' if entry['urgency']=='normal' else entry['urgency']] for entry in entries]
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
    assets: dict[str,str]
    zone: ZoneInfo
    source_stamps: dict[Path, tuple[int, int]]
    page: bytes = b''
    payload: bytes = b''
    page_gzip: bytes = b''
    payload_gzip: bytes = b''
    read_at: float = 0
    render_seconds: float = 0
    notices: tuple[str, ...] = ()
    unavailable_cards: frozenset[str] = frozenset()

    def facts(self) -> dict[str,float]:
        return {'readAt':self.read_at,'renderSeconds':self.render_seconds,'answeredAt':time.time()}

    def index_response(self) -> bytes:
        return self.payload

    def render(self, *, show: str = 'open') -> bytes:
        selected = show if show in dict(SHOWS) else 'open'
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
            return details(item, ReviewBodyInputs(self.browser, self.members, self.words_rows, self.assets, self.zone)).encode()
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


def card_entry(item: ReviewItem, members: staff.StaffSnapshot) -> ReviewIndexEntry:
    entry, = filter_index([item], members)
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
                    change_review.lists_directory(), urgency.state_dir(), conf.path('review_links'),
                    Path(USER_ZONE_CONF)))
    zone = user_zone()
    token,initial = change_browser.browser_manifest(folder)
    members = staff.snapshot()
    sources.extend(urgency.state_dir().glob('*.json'))
    records: dict[str, ReviewItem] = {}
    entries: list[ReviewIndexEntry] = []
    assets: dict[str, str] = {}
    unavailable_cards: set[str] = set()
    for item in review_items.read(root, members=members, sources=sources):
        path = review_items.source_path(item)
        with guard_source(path, sources, entry=item.id):
            item = review_items.normalize_times(item, zone)
            try:
                record_assets: dict[str, str] = {}
                retain_asset(item, record_assets)
                validate_body_inputs(item, record_assets)
                summary = card_entry(item, members)
            except (UnicodeError, TypeError, ValueError, OSError, RuntimeError) as error:
                sources.skipped(path, item.id, error)
                unavailable_cards.add(item.key)
                summary = card_entry(replace(item, title='Card unavailable'), members)
            else:
                assets.update(record_assets)
            entries.append(summary)
            records[item.key] = item
    browser = change_page.ReviewBrowser(token, initial)
    settings = conf.read('review_links')
    words_rows = settings.whole('review_words_rows')
    refresh_seconds = settings.number('page_refresh_seconds')
    index = index_json(entries)
    notices = tuple(sources.notices.values())
    retained = repr((records, assets, members.rows, browser, words_rows, refresh_seconds, zone.key, notices, unavailable_cards))
    version = hashlib.sha256((index+retained).encode()).hexdigest()[:20]
    snapshot = ReviewSnapshot(version,records,entries,members,
        browser,words_rows,refresh_seconds,assets,zone,sources.before_read,read_at=read_at,
        notices=notices, unavailable_cards=frozenset(unavailable_cards))
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


@dataclass
class ReviewSnapshotCache:
    """Publish complete snapshots from one background worker and retain the last good snapshot."""
    root: Path
    interval: float = 0.5
    start_worker: bool = True
    _snapshot: ReviewSnapshot | SnapshotUnavailable = field(default_factory=SnapshotUnavailable,init=False)
    _condition: threading.Condition = field(default_factory=threading.Condition,init=False)
    _save_lock: threading.Lock = field(default_factory=threading.Lock,init=False)
    _wake: threading.Event = field(default_factory=threading.Event,init=False)
    _stop: threading.Event = field(default_factory=threading.Event,init=False)
    _busy: bool = field(default=False,init=False)
    _initialize: bool = field(default=False,init=False)
    _generation: int = field(default=0,init=False)
    _thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        if (self.root/change_page.CONSOLE_FOLDER/change_browser.MANIFEST).exists():
            try:
                self._snapshot = build_snapshot(self.root)
            except Exception as error:
                logging.getLogger(__name__).warning('Review snapshot: %s',error)
        self._thread = threading.Thread(target=self._update,name='review-snapshot',daemon=True)
        if self.start_worker:
            self.start()

    def start(self) -> None:
        self._thread.start()

    def get(self) -> ReviewSnapshot:
        with self._condition:
            if isinstance(self._snapshot, SnapshotUnavailable) and self._thread.is_alive():
                if not self._busy:
                    self._initialize = True
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
                updated = self.publish_record(current, key)
                item = updated.records.get(key, ReviewRecordNotPublished())
                if (isinstance(item, ReviewItem) and isinstance(item.source, review_items.ChangeSource)
                        and fields['outcome'][0] in ('works', 'not works')):
                    answer = item.source.row.get('Verdict') or answer
                return ReviewSave(delivery, answer, updated.version, item)
            except Exception as error:
                logging.getLogger(__name__).warning('Review saved %s. Publication will retry: %s', key, error)
                return ReviewSave(delivery, answer, current.version, ReviewRecordNotPublished())
            finally:
                self.invalidate()

    def publish_record(self, current: ReviewSnapshot, key: str) -> ReviewSnapshot:
        """Replace or remove one record while serving the previous version to readers."""
        started = time.perf_counter()
        records = current.records.copy()
        assets = current.assets.copy()
        _ = assets.pop(key, None)
        _ = assets.pop('image:'+key, None)
        members = staff.snapshot()
        try:
            item = review_items.read_current(self.root, records[key], members)
            item = review_items.normalize_times(item, current.zone)
            retain_asset(item, assets)
        except (FileNotFoundError, review_items.ReviewRecordMissing):
            del records[key]
            entries = [entry for entry in current.entries if entry['key'] != key]
            record_version = key
        else:
            records[key] = item
            validate_body_inputs(item, {name: assets[name] for name in (key, 'image:'+key) if name in assets})
            updated_entry = card_entry(item, members)
            entries = [updated_entry if entry['key'] == key else entry for entry in current.entries]
            record_version = repr((item, assets.get(key), assets.get('image:'+key)))
        version = hashlib.sha256((current.version+record_version).encode()).hexdigest()[:20]
        fresh = pack_snapshot(replace(current, version=version, members=members, records=records, assets=assets, entries=entries,
                                      unavailable_cards=current.unavailable_cards-{key}),
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
                    if (isinstance(snapshot, SnapshotUnavailable) and not self._initialize
                            and not (self.root/change_page.CONSOLE_FOLDER/change_browser.MANIFEST).exists()):
                        self._wake.clear()
                        continue
                    if unchanged and not self._wake.is_set():
                        continue
                    self._initialize = False
                    self._wake.clear()
                    self._busy = True
                if not (self.root/change_page.CONSOLE_FOLDER/change_browser.MANIFEST).exists():
                    _ = change_page.console_folder(self.root)
                fresh = build_snapshot(self.root)
                with self._save_lock, self._condition:
                    if self._snapshot is snapshot and self._generation == generation:
                        self._snapshot = fresh
                    else:
                        self._wake.set()
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


def form(item: ReviewItem, browser: change_page.ReviewBrowser, words_rows: int, revision: str) -> str:
    fields = {'row': item.key, 'token': change_browser.row_token(browser, item.key), 'revision': revision}
    hidden = ''.join(f'<input type="hidden" name="{key}" value="{html.escape(value, quote=True)}">' for key, value in fields.items())
    closes: list[str] = []
    returns: list[str] = []
    labels = {'answer': 'Answer', 'works': 'Works', 'not works': 'Does not work', 'validate': 'Ask showrunner to check',
              'question': 'Ask question', 'feedback': 'Send feedback', 'read': 'Mark read',
              'approve': 'Approve', 'decline': 'Decline'}
    for index, action in enumerate(item.actions):
        button = f'<button name="outcome" value="{action}"'+(' class="primary"' if index == 0 else '')+'>'+labels[action]+'</button>'
        (returns if action == 'question' else closes).append(button)
    groups = '<div class="review-action-group"><span>Closes the item</span><div>'+''.join(closes)+'</div></div>' if closes else ''
    if returns:
        groups += '<div class="review-action-group"><span>The item returns to you</span><div>'+''.join(returns)+'</div></div>'
    groups += ('<div class="review-action-group review-later"><span>Hides it until</span><div>'
               +'<input type="datetime-local" name="later" aria-label="Later, until">'
               +'<button name="outcome" value="later">Later</button></div></div>')
    return ('<form method="post" data-review-form>'+hidden+f'<label for="words-{html.escape(item.key, quote=True)}">Your words</label>'
            + f'<textarea id="words-{html.escape(item.key, quote=True)}" name="feedback" rows="{words_rows}" '
            + 'placeholder="Type your words. They go with the button that you press."></textarea>'
            + '<div class="review-actions">'+groups+'</div><span role="status" data-send-status></span></form>')


def details(item: ReviewItem, inputs: ReviewBodyInputs) -> str:
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
        content, choices = change_browser.question_parts(source.record.item)
        question = source.record.item
        ask = question['parts']['ask'] if 'parts' in question else change_browser.question_text(question)[0]
        lead = change_page.review_text_html(ask, inline=True) if ask != question['text'] else ''
        body = '<section><h3>Question</h3>'+lead+content+choices+'</section>'
        if isinstance(source, review_items.UnownedQuestionSource):
            body += '<p>No showrunner owns this question.</p>'
    elif isinstance(source, review_items.ChangeSource):
        row = source.row
        body = section('How to check', row.get('How to check', ''))+section('Proof', row.get('What you see', '') or row.get('Proof', ''))
        if isinstance(source.slots.question, change_page.AnsweredQuestion):
            body += answer_section(source.slots.question.answer)
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
        +entry.time.astimezone(inputs.zone).strftime('%b %d %H:%M')+'</time><span>'+html.escape(entry.words)
        +' <small>— '+html.escape(entry.who)+'</small></span></li>' for entry in reversed(item.log))
    owner = owner_name(item, inputs.members)
    return ('<div class="list-panel review-panel '+item.type+'"><div>'+body+'</div><section><h3>Log · newest first</h3>'
            +'<ol class="list-log">'+logs+'</ol></section>'+form(item, inputs.browser, inputs.words_rows, record_revision(item, inputs.assets))
            +'<p class="review-source">Owner: '+html.escape(owner)+' · Source: '
            +html.escape(review_items.name(item.source_unit, inputs.members))+'</p></div>')


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
    owners = {entry['owner']:entry['ownerName'] for entry in entries}
    owners.update({entry['sourceOwner']:entry['sourceName'] for entry in entries if entry['type']==ReviewType.INVESTIGATION})
    options = ''.join(f'<option value="{html.escape(owner, quote=True)}" data-owner-name="{html.escape(name, quote=True)}">{html.escape(name)}</option>'
                     for owner, name in sorted(owners.items(), key=lambda pair:pair[1].casefold()))
    chips = '<button id="review-type-all" type="button" data-type-chip="" aria-pressed="true">All <b></b></button>'
    chips += ''.join('<button id="review-type-'+kind+'" type="button" data-type-chip="'+kind+'" aria-pressed="false">'+icon(kind)+LABELS[kind]
                     +' <b></b></button>' for kind in TYPES)
    shows = ''.join('<button id="review-show-'+value.lower().replace(' ', '-')+'" type="button" data-show-chip="'
                    +value+'" aria-pressed="'+str(value == show).lower()+'">'+label
                    +' <b></b></button>' for value, label in SHOWS)
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      +'<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    content = (record_table.STYLE+STYLE+page_frame.title('Review', 'review')
        +f'<div data-review data-version="{snapshot.version}" data-show="{show}" data-refresh-seconds="{snapshot.refresh_seconds}">'
        +'<div class="review-types" role="group" aria-label="Type">'+chips+'</div>'
        +'<div class="review-types review-shows" id="list-show" role="group" aria-label="Show">'+shows+'</div><div class="list-filters">'
        +'<label for="list-owner">Owner</label><select id="list-owner"><option value="">All</option>'+options+'</select>'
        +'<label class="list-search-label" for="list-search">Search</label><input type="search" id="list-search" placeholder="Filter by id or words">'
        +'</div><p role="status" data-review-notice hidden></p>'
        +'<ul data-review-source-notices'+('' if snapshot.notices else ' hidden')+'>'
        +''.join('<li>'+html.escape(notice)+'</li>' for notice in snapshot.notices)+'</ul>'
        +'<script>'+CARD_REQUEST_SCRIPT+'</script><script type="application/json" data-review-index>'+index+'</script>'
        +'<div class="list-table"><table aria-label="Review">'
        +'<colgroup><col style="width:13%"><col style="width:13%"><col style="width:14%"><col style="width:41%">'
        +'<col style="width:12%"><col style="width:7%"></colgroup><thead><tr>'+headers+'</tr></thead>'
        +'<tbody data-window-before></tbody><tbody data-window-after></tbody>'
        +'</table><p data-empty hidden>No items in this view.</p></div></div><script>'+SCRIPT+'</script>')
    return page_frame.rendered(change_page.document(content, 'Review'), started, read_at)


def review_fields(body: str) -> dict[str, list[str]]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    required = {'row', 'token', 'outcome', 'feedback'}
    if not required <= fields.keys() <= required | {'later', 'revision'} or any(len(values)!=1 for values in fields.values()):
        raise change_page.AnswerRefused('use the answer form on this page')
    return fields


def submit_snapshot(root: Path, snapshot: ReviewSnapshot, fields: dict[str, list[str]],
                    status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    key, token, outcome, words = (fields[field][0] for field in ('row', 'token', 'outcome', 'feedback'))
    if not secrets.compare_digest(token, change_browser.row_token(snapshot.browser, key)):
        raise change_page.AnswerRefused('row or page token does not match this page')
    retained = snapshot.records.get(key)
    if retained is None:
        raise review_items.ReviewRecordMissing('This item is no longer on the list.')
    def validate() -> ReviewItem:
        current = review_items.read_current(root, retained, staff.snapshot())
        assets: dict[str, str] = {}
        try:
            retain_asset(current, assets)
        except FileNotFoundError:
            raise review_items.ReviewRecordMissing('This item is no longer on the list.') from None
        if fields.get('revision', [''])[0] != record_revision(current, assets):
            raise review_items.ReviewRecordChanged('This item changed. Check it again before saving.')
        return current

    item = validate()
    if outcome == 'later':
        return change_page.NoMessageDelivery(), review_actions.later(
            item, fields.get('later', [''])[0], words, verify_revision=validate, root=root)
    if outcome not in item.actions:
        raise change_page.AnswerRefused('choose an action on this card')
    source = item.source
    if isinstance(source, review_items.ChangeSource):
        with change_review.checked_review_write(source.path, root, validate) as current:
            delivery = change_browser.submit_review_change(current, snapshot.browser, outcome, words, status)
        if isinstance(delivery, change_page.MessageDelivery):
            label = 'feedback sent' if outcome == 'feedback' else outcome
            with review_items.after_store_write(item.key):
                change_review.record_review_log(delivery.page.review_list, delivery.row,
                                                label+(': '+words if words else ''), delivery.page.production)
        return delivery, {'validate':'asked the showrunner to check', 'question':'question sent',
                          'feedback':'feedback sent'}.get(outcome, outcome)
    if isinstance(source, review_items.QuestionSource):
        if not words.strip():
            raise change_page.AnswerRefused('enter an answer')
        members = staff.snapshot()
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
        raise change_page.AnswerRefused('row or page token does not match this page')
    outcome, words = fields['outcome'][0], fields['feedback'][0]
    if outcome == 'later' or key.startswith(('investigation/', 'proposal/', 'efficiency/')):
        item = next((item for item in review_items.read(root) if item.key == key), None)
        if item is None:
            raise change_page.AnswerRefused('this item is not on the page')
        if outcome == 'later':
            return change_page.NoMessageDelivery(),review_actions.later(item,fields.get('later',[''])[0],words)
        if outcome not in item.actions:
            raise change_page.AnswerRefused('choose an action on this card')
        delivery, answer = (review_actions.investigation_action(item,outcome,words,status)
                            if item.type == ReviewType.INVESTIGATION else
                            review_actions.decision_action(item,outcome,words,status))
        if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery):
            delivery = review_actions.track_question(delivery, key, words, status)
        return delivery, answer
    _ = fields.pop('later',None)
    _ = fields.pop('revision',None)
    item = None
    if key.startswith('question/'):
        item = next((item for item in review_items.read(root, kinds=(ReviewType.QUESTION,)) if item.key == key), None)
    if item is not None and item.state == ReviewState.WAITING:
        if outcome not in item.actions:
            raise change_page.AnswerRefused('choose an action on this card')
        delivery, answer = review_actions.question_action(item, cast(change_page.QuestionOutcome, outcome), words, status)
    else:
        delivery, answer = change_browser.submit(browser, urlencode({key:values[0] for key,values in fields.items()}), status,
                                                   feedback_closes=True)
    if key.startswith('question/') and outcome == 'question' and isinstance(delivery, change_page.MessageDelivery):
        delivery = review_actions.track_question(delivery, key, words, status)
    words = ('feedback sent' if outcome == 'feedback' else outcome)+(': '+fields['feedback'][0] if fields['feedback'][0] else '')
    if isinstance(delivery, change_page.MessageDelivery):
        if not isinstance(delivery.page, change_page.UserQuestionPage):
            change_review.record_review_log(delivery.page.review_list, delivery.row, words, delivery.page.production)
    return delivery, ('feedback sent' if outcome == 'feedback' else answer or
                     {'validate':'asked the showrunner to check', 'question':'question sent'}.get(outcome,'answer'))


STYLE = '''<style>
.review-types{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.review-types button{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;min-height:36px;
background:var(--card);border:1px solid var(--line);border-radius:999px;color:var(--ink-2);padding:5px 14px;margin:0}
.review-types button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.review-icon{display:inline-flex;vertical-align:middle;margin-right:5px}.review-icon svg{width:18px;height:18px}
.question{--type-color:#b99aff}.change{--type-color:#62b7ef}.investigation{--type-color:#30c1ad}
.proposal{--type-color:#f27ab8}.efficiency{--type-color:#a3d63a}
.review-icon{color:var(--type-color)}.review-panel{border-left-color:var(--type-color);grid-template-columns:1.25fr 1fr;padding:0 20px 18px 58px;gap:4px 28px}
.review-panel [data-review-form]{grid-column:1/-1;border-top:1px solid var(--line);padding-top:12px;margin-top:10px}
[data-review-form] label{display:block;font-weight:600;margin-bottom:6px}
[data-review-form] textarea{display:block;box-sizing:border-box;width:100%;font:inherit;padding:9px 12px;background:var(--bg);
color:var(--ink);border:1px solid var(--line);border-radius:8px;resize:vertical}
.review-actions{display:flex;align-items:flex-end;gap:26px;margin-top:12px;flex-wrap:wrap}
.review-action-group>span{display:block;font-size:11px;color:var(--ink-3);text-transform:uppercase;margin-bottom:5px}
.review-action-group>div{display:flex;gap:8px;flex-wrap:wrap}.review-action-group+.review-action-group{padding-left:26px;border-left:1px solid var(--line)}
.review-later{margin-left:auto}.review-actions input{font:inherit;min-height:40px;max-width:100%;box-sizing:border-box;
padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
.list-log span{white-space:pre-wrap}
.review-actions button{margin:0;font:inherit;min-height:40px;padding:6px 14px;border:1px solid var(--ink-3);border-radius:8px;
background:var(--card);color:var(--ink)}.review-actions .primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.review-source{grid-column:1/-1;color:var(--ink-3);font-size:12px;margin-top:16px!important}
[data-review] .list-row td:nth-child(4) strong{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.list-table .list-row td:last-child{text-align:right}.list-table [data-detail]>td{text-align:left}
@media(max-width:960px){[data-review] .list-row td:last-child::before{content:'Age: '}
.review-panel{grid-template-columns:1fr;padding:0 14px 16px}.review-action-group+.review-action-group{padding-left:0;border:0}}
</style>'''

CARD_REQUEST_SCRIPT = r'''
function requestReviewCard(key,version){
 return fetch(location.pathname+'?'+new URLSearchParams({body:key,version}),{cache:'no-store'})
  .then(async response=>({response,text:response.ok ? await response.text() : ''}));
}
const firstReviewCard=(()=>{
 const key=new URLSearchParams(location.hash.slice(1)).get('item') || new URLSearchParams(location.search).get('item');
 if(!key) return null;
 const version=document.querySelector('[data-review]').dataset.version;
 const result=requestReviewCard(key,version);
 void result.catch(()=>{});
 return {key,version,result};
})();
'''

SCRIPT = page_frame.REFRESH_SCRIPT + record_table.SCRIPT + review_legacy.SCRIPT + r'''(() => {
let content=document.querySelector('[data-review]');
let selectedShow=content.dataset.show;
const drafts=new Map(),saved=new Map(),bodies=new Map(),bodyRequests=new Map();
const pendingSaves=new Map(),saveFailures=new Map();
const kinds=['question','change','investigation','proposal','efficiency'],states=['Open','Waiting','Done'];
let initialIndex=JSON.parse(content.querySelector('[data-review-index]').textContent);
let index=new Map(),focusAfterLoad=null,conflictKey='',firstRequest=firstReviewCard;
function indexEntry(payload,row,at){
 const owner=payload.n[row[3]],source=payload.n[row[4]];
 return {key:row[0],id:row[1] || row[0],type:kinds[row[2]],owner:owner[0],sourceOwner:source[0],
  ownerName:owner[1],sourceName:source[1],title:row[5],state:states[row[6]],filed:row[7],updated:row[8],
   hiddenUntil:row[9],urgency:row[10] || 'normal',order:at+row[6]*payload.r.length};
}
function readIndex(payload){
 return new Map(payload.r.map((row,at)=>[row[0],indexEntry(payload,row,at)]));
}
function allEntries(){
 if(initialIndex){
  const initial=index;
  index=readIndex(initialIndex);
  for(const [key,entry] of initial) index.set(key,entry);
  initialIndex=null;
 }
 return [...index.values()];
}
const escapeText=value=>String(value).replace(/[&<>"']/g,letter=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[letter]));
function summary(entry){
 const node=document.createElement('tbody'),panel='review-detail-'+encodeURIComponent(entry.key);
 Object.assign(node.dataset,{listItem:'',key:entry.key,number:entry.id.toLowerCase(),title:entry.title.toLowerCase(),
  owner:entry.owner,sourceOwner:entry.sourceOwner,sourceName:entry.sourceName,state:entry.state,type:entry.type,
  order:entry.order,updated:entry.updated,parked:'false'});
 const age=Math.max(0,Math.floor(Date.now()/1000-entry.filed));
 const stamp=age>=86400 ? Math.floor(age/86400)+' d' : age>=3600 ? Math.floor(age/3600)+' h' : Math.floor(age/60)+' min';
 const icon=content.querySelector('[data-type-chip="'+entry.type+'"] .review-icon').outerHTML;
 const cells=['<button type="button" class="list-toggle" aria-expanded="false" aria-controls="'+panel+'">'
  +'<span class="list-chevron" aria-hidden="true">›</span>'+escapeText(entry.id)+'</button>',
  icon+entry.type[0].toUpperCase()+entry.type.slice(1),escapeText(entry.ownerName)
  +(entry.sourceName!==entry.ownerName ? '<small>· '+escapeText(entry.sourceName)+'</small>' : ''),
  '<strong>'+escapeText(entry.title)+'</strong>'+(entry.urgency!=='normal' ? '<small>'+escapeText({user:'Important',blocks:'Blocks work',soon:'Due soon',later:'Later'}[entry.urgency])+'</small>' : ''),
  '<span class="list-state '+(entry.state==='Open' ? 'warning' : 'muted')+'">'+entry.state+'</span>',stamp];
 node.innerHTML='<tr class="list-row">'+cells.map((cell,at)=>'<td data-label="'+['Id','Type','Owner','Item','State','Age'][at]+'">'+cell+'</td>').join('')
  +'</tr><tr data-detail id="'+panel+'" hidden><td colspan="6"><p role="status">Loading the item…</p></td></tr>';
 return node;
}
const ownerOptions=new WeakMap();
function knownOwnerOptions(){
 const menu=content.querySelector('#list-owner');
 if(!ownerOptions.has(menu)) ownerOptions.set(menu,[...menu.options]);
 return ownerOptions.get(menu);
}
function matches(entry,{owner='',show='all',query='',type='',sourceOwner=''}){
 return entry.hiddenUntil<=Date.now() &&
  (!owner || entry.owner===owner || entry.type==='investigation' && entry.sourceOwner===owner) &&
  (!type || entry.type===type) && (!sourceOwner || entry.sourceOwner===sourceOwner) &&
  (!query || entry.id.toLowerCase().includes(query) || entry.title.toLowerCase().includes(query)) &&
  (show==='all' || entry.state===(show==='open' ? 'Open' : show));
}
function matching(filters){return [...index.values()].filter(entry=>matches(entry,filters));}
function savedEntry(key,result){
 const entry=index.get(key);if(!entry) return;
 entry.state=result.state;entry.hiddenUntil=result.later;
}
const view=createRecordTable({content:()=>content,single:true,
 entries:allEntries,mount:summary,mounted:()=>loadBodies(),
 focusKey:()=>focusAfterLoad?.key,
 unmount:row=>rememberForm(row.querySelector('[data-review-form]')),
 place:reviewPlace,
 resolveOwner:owner=>{
  const option=knownOwnerOptions().find(option=>option.value===owner || option.dataset.ownerName===owner);
  return option ? option.value : owner;
 },
 selectOwner:({owner,type,query})=>!owner || allEntries().some(entry=>matches(entry,{owner,type,query})) ? owner : '',
 filterOwner:(_items,{owner,show,type,query,sourceOwner})=>{
  const menu=content.querySelector('#list-owner');
  const eligibleOwners=matching({type,query});
  const matchingOwners=matching({show,type,query,sourceOwner});
  const available=knownOwnerOptions().filter(option=>{
   const count=matchingOwners.filter(entry=>!option.value || entry.owner===option.value ||
    entry.type==='investigation' && entry.sourceOwner===option.value).length;
   option.textContent=(option.dataset.ownerName || 'All')+' ('+count+')';
   return !option.value || eligibleOwners.some(entry=>entry.owner===option.value ||
    entry.type==='investigation' && entry.sourceOwner===option.value);
  });
  menu.replaceChildren(...available);
  return available.some(option=>option.value===owner) ? owner : '';
 },
 shows:['open','Waiting','Done','all'],
 visible:(data,filters)=>!saved.has(data.key) && index.has(data.key) && matches(index.get(data.key),filters),
 sortValues:data=>{
  const entry=index.get(data.key);
  return [entry.id,entry.type,entry.ownerName,entry.title,entry.state,entry.filed];
 },
 applied:(_items,{type,show,owner,query,sourceOwner})=>{
  selectedShow=show;content.dataset.show=show;loadBodies();
  content.querySelectorAll('[data-show-chip]').forEach(button=>{
   button.querySelector('b').textContent=matching({type,owner,query,sourceOwner,show:button.dataset.showChip}).length;
  });
  content.querySelectorAll('[data-type-chip]').forEach(button=>{
   button.setAttribute('aria-pressed',String(button.dataset.typeChip===type));
   button.querySelector('b').textContent=matching({owner,show,query,sourceOwner,type:button.dataset.typeChip}).length;
  });
 }
});
function mountedRow(key){return content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"]');}
function rememberFocus(){
 const field=document.activeElement,row=field?.closest('[data-list-item]');
 if(!row) return null;
 const selector=field.id ? '#'+CSS.escape(field.id) : field.localName+['name','value','href','class'].map(name=>
  field.hasAttribute(name) ? '['+name+'="'+CSS.escape(field.getAttribute(name))+'"]' : '').join('');
 return {key:row.dataset.key,selector,position:[...row.querySelectorAll(selector)].indexOf(field),
  start:field.selectionStart,end:field.selectionEnd};
}
function restoreFocus(key){
 if(focusAfterLoad?.key!==key) return;
 if(document.activeElement!==document.body){focusAfterLoad=null;return;}
 const field=mountedRow(key)?.querySelectorAll(focusAfterLoad.selector)[focusAfterLoad.position];
 if(!field) return;
 const {start,end}=focusAfterLoad;focusAfterLoad=null;
 field.focus({preventScroll:true});
 if(typeof field.setSelectionRange==='function' && typeof start==='number') field.setSelectionRange(start,end);
}
function loadBodies(){
 view.opened.forEach(key=>{
  const row=mountedRow(key);
  if(!row || row.dataset.bodyLoaded) return;
  const install=text=>{
   const current=mountedRow(key);
   if(!current || current.dataset.bodyLoaded) return;
   current.dataset.bodyLoaded='true';current.querySelector('[data-detail]>td').innerHTML=text;
   bindForm(current);restoreFocus(key);view.drawWindow();
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
    if(target && bodyRequests.get(key)===request) target.innerHTML='<p role="status">The item is not available: '+escapeText(error.message)+'</p>';
   })
   .finally(()=>{if(bodyRequests.get(key)===request) bodyRequests.delete(key);});
  bodyRequests.set(key,request);
 });
}
function rememberForm(form){
 if(!form) return;
 const row=form.closest('[data-list-item]');
 if(pendingSaves.get(row.dataset.key)?.saved) return;
 const choices=[...row.querySelectorAll('input[name=choice]')];
 drafts.set(row.dataset.key,{feedback:form.elements.feedback.value,later:form.elements.later.value,
  choice:choices.find(choice=>choice.checked)?.value || ''});
}
function drawSaveState(key,form=mountedRow(key)?.querySelector('[data-review-form]')){
 if(!form) return;
 const pending=pendingSaves.get(key);
 if(pending) form.dataset.sending='true';else delete form.dataset.sending;
 form.querySelectorAll('button').forEach(button=>button.disabled=Boolean(pending));
 form.querySelector('[data-send-status]').textContent=pending?.notice || saveFailures.get(key) || '';
}
function bindForm(row){
 const form=row.querySelector('[data-review-form]');if(!form || form.dataset.bound) return;
 form.dataset.bound='true';
 const key=row.dataset.key,words=form.elements.feedback,later=form.elements.later;
 drawSaveState(key,form);
 const choices=[...row.querySelectorAll('input[name=choice]')];
 if(drafts.has(key)){
  const draft=drafts.get(key);words.value=draft.feedback;later.value=draft.later;
  choices.forEach(choice=>choice.checked=choice.value===draft.choice);
 }
 const remember=()=>rememberForm(form);
 choices.forEach(choice=>choice.addEventListener('change',()=>{words.value=choice.value;remember();words.focus();}));
 [words,later].forEach(field=>{field.addEventListener('input',remember);field.addEventListener('change',remember);});
 form.addEventListener('submit',async event=>{
   event.preventDefault();if(pendingSaves.has(key)) return;
   const outcome=event.submitter.value;
   const data=new URLSearchParams(new FormData(form));data.set('outcome',outcome);
   later.required=data.get('outcome')==='later';
   const valid=later.reportValidity();later.required=false;
   if(!valid) return;
   remember();saveFailures.delete(key);
   const notice=content.querySelector('[data-review-notice]');
   notice.hidden=true;notice.textContent='';
   const pending={notice:'Saving…',saved:false};
   pendingSaves.set(key,pending);pageRefresh.abandon();drawSaveState(key);
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    const result=await response.json();
    if(!response.ok){
     const reason=result.reason || 'HTTP '+response.status;
     if(response.status===409){
      conflictKey=key;notice.textContent='Not saved: '+reason;notice.hidden=false;
     }
     throw new Error(reason);
    }
    pending.saved=true;pending.notice='Saved: '+result.answer;drawSaveState(key);drafts.delete(key);
    await new Promise(resolve=>setTimeout(resolve,900));
    saved.set(key,{updated:row.dataset.updated,previousState:row.dataset.state,
     state:result.state ?? (['validate','question'].includes(outcome) ? 'Waiting' : outcome==='later' ? row.dataset.state : 'Done'),
     later:result.later ?? (outcome==='later' ? Date.parse(data.get('later')) : 0),
     question:data.get('outcome')==='question',
     closes:!['validate','question','later'].includes(data.get('outcome'))});
    savedEntry(key,saved.get(key));
    view.opened.delete(key);view.apply(false);
   }catch(error){saveFailures.set(key,'Not saved: '+error.message);}
   finally{
    pendingSaves.delete(key);delete form.dataset.sending;drawSaveState(key);
    if(!pendingSaves.size) void pageRefresh.refresh(true);
   }
  });
}
function bind(){view.replace(false);}
function revealConflict(){
 const key=conflictKey;conflictKey='';
 const entry=index.get(key);
 if(!entry || !view.opened.has(key) || matches(entry,{show:selectedShow})) return false;
 const params=new URLSearchParams(location.hash.slice(1));
 params.set('show',entry.state==='Open' ? 'open' : entry.state);params.set('item',key);
 history.replaceState(null,'',location.pathname+'#'+params);
 view.place();
 return true;
}
async function refresh(_supersede=false,current=()=>true){
 if(pendingSaves.size) return;
 const response=await fetch(location.pathname+'?'+new URLSearchParams({version:'1'}),{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const version=await response.json();
 if(pendingSaves.size || !current()) return;
 if(version.version===content.dataset.version){
  for(const [key,result] of saved) if(result.later && Date.now()>=result.later) saved.delete(key);
  revealConflict();view.apply(false);content.dispatchEvent(new Event('review-refreshed'));return version.facts;
 }
 const fresh=await fetch(location.pathname+'?'+new URLSearchParams({index:'1',version:version.version}),{cache:'no-store'});
 if(!fresh.ok) throw new Error('HTTP '+fresh.status);
 const payload=await fresh.json();
 if(pendingSaves.size || !current()) return;
 const top=scrollY;focusAfterLoad=rememberFocus();
 content.querySelectorAll('[data-review-form]').forEach(rememberForm);
 initialIndex=null;index=readIndex(payload.index);content.dataset.version=payload.version;
 content.querySelector('[data-review-index]').textContent=JSON.stringify(payload.index);
 const notices=content.querySelector('[data-review-source-notices]');
 notices.replaceChildren(...(payload.notices || []).map(text=>{
  const line=document.createElement('li');line.textContent=text;return line;
 }));
 notices.hidden=!notices.children.length;
 bodies.clear();bodyRequests.clear();
 const menu=content.querySelector('#list-owner'),pairs=new Map();
 for(const entry of index.values()){
  pairs.set(entry.owner,entry.ownerName);
  if(entry.type==='investigation') pairs.set(entry.sourceOwner,entry.sourceName);
 }
 menu.replaceChildren(new Option('All',''),...[...pairs].sort((a,b)=>a[1].localeCompare(b[1])).map(([id,name])=>{
  const option=new Option(name,id);option.dataset.ownerName=name;return option;
 }));ownerOptions.delete(menu);
 for(const [key,result] of saved){
  const entry=index.get(key);
  if(!entry || (result.later && Date.now()>=result.later)) {saved.delete(key);continue;}
  // The index supplies the state and update time for each row.
  const submitted=entry.state===result.previousState && entry.updated===result.updated;
  if(!submitted && entry.state!==result.state){saved.delete(key);continue;}
  if(!result.closes && !result.question && !result.later && !submitted){
   saved.delete(key);
  }
 }
 for(const [key,result] of saved) savedEntry(key,result);
 const revealed=revealConflict();bind();view.apply(false);
 if(focusAfterLoad) restoreFocus(focusAfterLoad.key);
 content.dataset.loaded='true';content.dispatchEvent(new Event('review-refreshed'));
 if(!revealed) scrollTo({top,behavior:'instant'});
 return {...payload.facts,answeredAt:version.facts.answeredAt};
}
view.place();bind();
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
const start=firstRequest ? initialIndex.r.findIndex(row=>row[0]===firstRequest.key) : -1;
if(start>=0){
 const entry=indexEntry(initialIndex,initialIndex.r[start],start);
 index.set(entry.key,entry);
 if(view.openInitial(entry)){
  loadBodies();
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
   if(initialIndex){view.place();view.apply();}
  }));
 }else view.apply();
}else view.apply();
addEventListener('hashchange',()=>{const item=new URLSearchParams(location.hash.slice(1)).get('item');if(item && !saved.get(item)?.closes && !saved.get(item)?.question) saved.delete(item);view.place();view.apply(false);});
})();'''
