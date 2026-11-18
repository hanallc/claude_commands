"""Show questions, changes, investigations, proposals and efficiencies in one review table."""
from __future__ import annotations

import html
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import parse_qs, parse_qsl, urlencode

from scripts.lib import conf
from scripts.lib.user_zone import user_zone, user_zone_name
from scripts.production import change_review, proposal_issue, staff
from scripts.review_links import change_browser, change_page, console, page_frame, record_table, review_items, review_link
from scripts.review_links import question_record
from scripts.review_links import review_legacy
from scripts.review_links import review_actions
from scripts.review_links import staff_names
from scripts.review_links.review_items import ReviewItem, ReviewState, ReviewType

PAGE_PATH = '/console/review.html'
HEADERS = ('Id', 'Type', 'Owner', 'Item', 'State', 'Age')
TYPES = tuple(ReviewType)
ICONS = {ReviewType.QUESTION: 'circle-help', ReviewType.CHANGE: 'diff', ReviewType.INVESTIGATION: 'microscope',
         ReviewType.PROPOSAL: 'lamp', ReviewType.EFFICIENCY: 'zap'}
SHOWS = (('open', 'Open'), ('Waiting', 'Waiting'), ('Done', 'Done'), ('all', 'All'))
LABELS = {ReviewType.QUESTION: 'Questions', ReviewType.CHANGE: 'Changes', ReviewType.INVESTIGATION: 'Investigations',
          ReviewType.PROPOSAL: 'Proposals', ReviewType.EFFICIENCY: 'Efficiencies'}
# The Later menu's presets, in menu order; the client resolves each time when the menu opens.
LATER_PRESETS = (('soon', ''), ('evening', 'This evening'), ('morning', 'Tomorrow morning'), ('week', 'Next week'))
# Weekday names in the order of the browser's getUTCDay.
WEEKDAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
CLOCK = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')
FRONT_MATTER = re.compile(r'\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)


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


def filter_index(records: list[ReviewItem], members: staff.StaffSnapshot, now: datetime) -> list[ReviewIndexEntry]:
    """Build the one index for Review counts, filters and column sorting."""
    return [ReviewIndexEntry(key=item.key, id=item.id, type=item.type, owner=str(item.owner),
            sourceOwner=str(item.source_unit), ownerName=owner_name(item, members), title=item.title,
            state=item.state, filed=int(item.times.filed.timestamp()), updated=item.times.updated.isoformat(),
            hiddenUntil=item.times.later.timestamp() * 1000
            if isinstance(item.times.later, datetime) and item.times.later > now else 0)
            for item in records]


def index_json(entries: list[ReviewIndexEntry]) -> str:
    """Encode the index for an HTML script element without executable text."""
    return json.dumps(entries, ensure_ascii=False, separators=(',', ':')).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')


@dataclass(frozen=True)
class CardSettings:
    """Settings that every Review card form shares in one render."""
    words_rows: int
    soon_label: str


def clock_minutes(settings: conf.ConfigRead, key: str) -> int:
    value = settings.text(key)
    match = CLOCK.fullmatch(value)
    if match is None:
        raise ValueError(f'review_links.conf: {key}={value!r} is not a time such as 09:00')
    return int(match[1])*60+int(match[2])


def later_settings(settings: conf.ConfigRead) -> dict[str, int]:
    """Return the Later menu's preset settings for the page script, in minutes and getUTCDay numbers."""
    day = settings.text('later_week_day')
    if day not in WEEKDAYS:
        raise ValueError(f'review_links.conf: later_week_day={day!r} is not a weekday name such as Monday')
    return {'soon': settings.whole('later_soon_minutes'), 'evening': clock_minutes(settings, 'later_evening'),
            'eveningUntil': clock_minutes(settings, 'later_evening_until'), 'morning': clock_minutes(settings, 'later_morning'),
            'weekDay': WEEKDAYS.index(day), 'weekTime': clock_minutes(settings, 'later_week_time')}


def soon_label(minutes: int) -> str:
    count, unit = (minutes//60, 'hour') if minutes % 60 == 0 else (minutes, 'minute')
    return f'In {count} {unit}'+('s' if count > 1 else '')


def page_url() -> str:
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def waiting_items(request: console.NewsRequest) -> frozenset[str]:
    now = datetime.now(user_zone())
    return frozenset(item.key+'/'+item.times.updated.isoformat() for item in request.shared(review_items.read_open)
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


def short_title(title: str, limit: int) -> str:
    first = title.splitlines()[0] if title else ''
    visible = first[:limit]
    return visible+'…' if visible != title else visible


def answer_section(text: str) -> str:
    return '<section><h3>Answer</h3>'+change_browser.answer_html(text)+'</section>'


def form(item: ReviewItem, browser: change_page.ReviewBrowser, settings: CardSettings) -> str:
    fields = {'row': item.key, 'token': change_browser.row_token(browser, item.key)}
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
    presets = ''.join(f'<button type="button" role="menuitem" tabindex="-1" data-later-preset="{key}">'
                      +f'<span>{html.escape(label or settings.soon_label)}</span><small></small></button>' for key, label in LATER_PRESETS)
    groups += ('<div class="review-action-group review-later"><span>Hides it until</span><div class="later-menu">'
               +'<button type="button" aria-haspopup="menu" aria-expanded="false" data-later-toggle>Later <span aria-hidden="true">▾</span></button>'
               +'<div role="menu" aria-label="Hide until" data-later-menu hidden>'+presets+'<hr>'
               +'<button type="button" role="menuitem" tabindex="-1" data-later-choose><span>Choose a time…</span></button>'
               +'<div class="later-custom" data-later-custom hidden>'
               +'<input type="hidden" name="later">'
               +'<div class="later-month-header"><strong data-later-month aria-live="polite"></strong><div>'
               +'<button type="button" tabindex="-1" data-later-month-step="-1" aria-label="Previous month">‹</button>'
               +'<button type="button" tabindex="-1" data-later-month-step="1" aria-label="Next month">›</button></div></div>'
               +'<div role="grid" aria-label="Choose a day" data-later-grid></div>'
               +'<label class="later-time">Time<input type="text" inputmode="numeric" autocomplete="off" data-later-time></label>'
               +'<div class="later-save"><span data-later-summary aria-live="polite"></span>'
               +'<button class="primary" name="outcome" value="later">Save</button></div></div></div></div></div>')
    return ('<form method="post" data-review-form>'+hidden+f'<label for="words-{html.escape(item.key, quote=True)}">Your words</label>'
            + f'<textarea id="words-{html.escape(item.key, quote=True)}" name="feedback" rows="{settings.words_rows}" '
            + 'placeholder="Type your words. They go with the button that you press."></textarea>'
            + '<div class="review-actions">'+groups+'</div><span role="status" data-send-status></span></form>')


def details(item: ReviewItem, browser: change_page.ReviewBrowser, members: staff.StaffSnapshot, settings: CardSettings,
            names: staff_names.StaffNames | staff_names.NameReading = staff_names.NameReading.CURRENT) -> str:
    names = staff_names.for_page(names, members)
    source = item.source
    if isinstance(source, (review_items.QuestionSource, review_items.UnownedQuestionSource)):
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
        body = section('How to check', row.get('How to check', ''))+section('Proof', row.get('What you see', '') or row.get('Proof', ''))
        if isinstance(source.slots.question, change_page.AnsweredQuestion):
            body += answer_section(source.slots.question.answer)
        image_id = row.get('Image', '')
        if image_id:
            body = change_page.change_content(row, body)
    elif isinstance(source, review_items.InvestigationSource):
        body = section('You asked', source.record.question)
        if source.record.answer:
            body += answer_section(source.record.answer)
    elif isinstance(source, review_items.ProposalSource):
        # The issue text loads when the card opens, so the Open view does not carry every proposal body.
        body = ('<section><h3>Proposal</h3><div data-proposal-body>'
                +'<p data-title-ignore>Loading proposal…</p></div></section>')
    elif isinstance(source, review_items.EfficiencySource):
        body = section('Proposal', item.title)
    else:
        body = ''
    fallback = '<section data-title-fallback hidden><h3>Item</h3><p>'+html.escape(item.title)+'</p></section>'
    logs = ''.join('<li><time datetime="'+entry.time.isoformat()+'">'
        +entry.time.astimezone(user_zone()).strftime('%b %d %H:%M')+'</time><span>'+html.escape(entry.words)
        +' <small>— '+names.html(entry.who)+'</small></span></li>' for entry in reversed(item.log))
    owner = owner_name(item, members)
    return ('<div class="list-panel review-panel '+item.type+'"><div>'+fallback+'<div data-review-body>'+body
            +'</div></div><section><h3>Log · newest first</h3>'
            +'<ol class="list-log">'+logs+'</ol></section>'+form(item, browser, settings)
            +'<p class="review-source">Owner: '+names.html(item.owner, owner)+' · Source: '
            +names.html(item.source_unit, review_items.name(item.source_unit, members))+'</p></div>')


def owner_name(item: ReviewItem, members: staff.StaffSnapshot) -> str:
    return 'Owner unavailable' if isinstance(item.source, review_items.UnownedQuestionSource) else review_items.name(item.owner, members)


def age(filed: datetime, now: datetime) -> str:
    seconds = max(0, int((now-filed).total_seconds()))
    return f'{seconds//86400} d' if seconds >= 86400 else f'{seconds//3600} h' if seconds >= 3600 else f'{seconds//60} min'


def item_rows(item: ReviewItem, browser: change_page.ReviewBrowser, members: staff.StaffSnapshot, now: datetime, index: int,
              title_limit: int, settings: CardSettings,
              names: staff_names.StaffNames | staff_names.NameReading = staff_names.NameReading.CURRENT) -> str:
    names = staff_names.for_page(names, members)
    owner = owner_name(item, members)
    source = review_items.name(item.source_unit, members)
    title = ('<strong class="review-title" title="'+html.escape(item.title, quote=True)+'">'
             +html.escape(short_title(item.title, title_limit))+'</strong>')
    if item.urgency.level != 'normal':
        title += '<small>'+html.escape({'user': 'Important', 'blocks': 'Blocks work', 'soon': 'Due soon', 'later': 'Later'}[item.urgency.level])+'</small>'
    values = (item.id, item.type.value, owner, item.title, item.state.value, item.times.filed.timestamp())
    attrs = {'key':item.key, 'number':item.id.casefold(), 'title':item.title.casefold(), 'owner':str(item.owner),
             'source-owner':str(item.source_unit), 'source-name':source,
             'state':item.state.value, 'type':item.type.value, 'order':str(index), 'sort-values':json.dumps(values),
             'parked':str(item.hidden_until(now)).lower(), 'updated':item.times.updated.isoformat()}
    attributes = ''.join(f' data-{key}="{html.escape(value, quote=True)}"' for key, value in attrs.items())
    panel = 'review-detail-'+str(index)
    toggle = (f'<button type="button" class="list-toggle" aria-expanded="false" aria-controls="{panel}">'
              +'<span class="list-chevron" aria-hidden="true">›</span>'+html.escape(item.id)+'</button>')
    state = '<span class="list-state '+('warning' if item.state == ReviewState.OPEN else 'muted')+'">'+item.state+'</span>'
    cells = (toggle, icon(item.type)+item.type.title(), names.html(item.owner, owner)+('<small>· '+names.html(item.source_unit, source)+'</small>' if source != owner else ''),
             title, state, age(item.times.filed, now))
    row = ''.join(f'<td' + (' class="record-id"' if label == 'Id' else '')
                  + f' data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    hidden = ' hidden' if item.state == ReviewState.DONE or item.hidden_until(now) else ''
    return ('<tbody data-list-item'+attributes+hidden+'><tr class="list-row">'+row+'</tr>'
            +f'<tr data-detail id="{panel}" hidden><td colspan="6">'+details(item, browser, members, settings, names)+'</td></tr></tbody>')


def render(root: Path, *, show: str = 'open') -> bytes:
    started, read_at = time.perf_counter(), time.time()
    title_limit = conf.read_or_repository('production').whole('question_title_max_chars')
    members = staff.snapshot()
    names = staff_names.StaffNames(members)
    now = datetime.fromtimestamp(read_at, user_zone())
    if show not in dict(SHOWS):
        show = 'open'
    owners: dict[staff.StaffId, str] = {}
    records = review_items.read(root, members=members, kinds=TYPES, owner_options=owners)
    items = [item for item in records
             if show == 'all' or (show == 'open' and item.state == ReviewState.OPEN) or item.state == show]
    items.sort(key=lambda item: (item.state != ReviewState.OPEN, item.state == ReviewState.DONE))
    token, initial = change_browser.browser_manifest(change_page.console_folder(root))
    browser = change_page.ReviewBrowser(token, initial)
    settings = conf.read('review_links')
    later = later_settings(settings)
    cards = CardSettings(settings.whole('review_words_rows'), soon_label(later['soon']))
    entries = filter_index(records, members, now)
    owner_chips = '<button id="review-owner-all" type="button" data-owner-chip="" aria-pressed="true">All <b></b></button>'
    for owner, name in sorted(owners.items(), key=lambda pair:pair[1].casefold()):
        identity = html.escape(owner, quote=True)
        owner_chips += (f'<button id="review-owner-id-{identity}" type="button" data-owner-chip="{identity}"'
                        f' data-owner-name="{html.escape(name, quote=True)}" aria-pressed="false">'
                        f'{names.html(owner, name)} <b></b></button>')
    chips = '<button id="review-type-all" type="button" data-type-chip="" aria-pressed="true">All <b></b></button>'
    chips += ''.join('<button id="review-type-'+kind+'" type="button" data-type-chip="'+kind+'" aria-pressed="false">'+icon(kind)+LABELS[kind]
                     +' <b></b></button>' for kind in TYPES)
    shows = ''.join('<button id="review-show-'+value.lower().replace(' ', '-')+'" type="button" data-show-chip="'
                    +value+'" aria-pressed="'+str(value == show).lower()+'">'+label
                    +' <b></b></button>' for value, label in SHOWS)
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      +'<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    content = (record_table.STYLE+STYLE+page_frame.title('Review', 'review')
        +f'<div data-review data-show="{show}" data-refresh-seconds="{settings.number("page_refresh_seconds")}"'
        +f' data-zone="{html.escape(user_zone_name(), quote=True)}" data-later="{html.escape(json.dumps(later), quote=True)}">'
        +'<div class="review-types" role="group" aria-label="Type">'+chips+'</div>'
        +'<div class="review-types review-shows" id="list-show" role="group" aria-label="Show">'+shows+'</div>'
        +'<div class="review-types" id="list-owner" role="group" aria-label="Owner">'+owner_chips+'</div>'
        +'<div class="list-filters">'
        +'<button type="button" data-clear-linked-reviews aria-label="Clear linked reviews filter"'
        +' aria-pressed="true" hidden>Linked reviews <span aria-hidden="true">×</span></button>'
        +'<label class="list-search-label" for="list-search">Search</label><input type="search" id="list-search" placeholder="Filter by id or words">'
        +'</div><p data-missing-item role="status" hidden></p>'
        +'<p data-hidden-action-status role="status" hidden></p>'
        +'<script type="application/json" data-review-index>'+index_json(entries)+'</script>'
        +'<div class="list-table"><table aria-label="Review">'
        +'<colgroup><col style="width:17%"><col style="width:13%"><col style="width:14%"><col style="width:37%">'
        +'<col style="width:12%"><col style="width:7%"></colgroup><thead><tr>'+headers+'</tr></thead>'
        +''.join(item_rows(item,browser,members,now,index,title_limit,cards,names) for index,item in enumerate(items))
        +'</table><p data-empty hidden>No items in this view.</p></div></div><script>'+SCRIPT+'</script>')
    return page_frame.rendered(change_page.document(content, 'Review'), started, read_at)


def proposal_body(root: Path, key: str) -> bytes:
    """Render one proposal's issue text for its open card."""
    item = next((item for item in review_items.read(root, kinds=(ReviewType.PROPOSAL,)) if item.key == key), None)
    if item is None or not isinstance(item.source, review_items.ProposalSource):
        raise LookupError('this proposal is not on the page')
    text = FRONT_MATTER.sub('', proposal_issue.read_issue(item.source.record.path), count=1)
    return change_page.review_text_html(text.strip(), inline=True).encode()


def submit(root: Path, body: str, status: change_page.DeliveryStatus) -> tuple[change_page.MessageDelivery | change_page.NoMessageDelivery, str]:
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) not in ({'row','token','outcome','feedback'},{'row','token','outcome','feedback','later'}) or any(len(values)!=1 for values in fields.values()):
        raise question_record.AnswerRefused('use the answer form on this page')
    token, initial = change_browser.browser_manifest(change_page.console_folder(root))
    browser = change_page.ReviewBrowser(token,initial)
    key = fields['row'][0]
    if not secrets.compare_digest(fields['token'][0],change_browser.row_token(browser,key)):
        raise question_record.AnswerRefused('row or page token does not match this page')
    outcome, words = fields['outcome'][0], fields['feedback'][0]
    if outcome == 'later' or key.startswith(('investigation/', 'proposal/', 'efficiency/')):
        item = next((item for item in review_items.read(root) if item.key == key),None)
        if item is None:
            raise question_record.AnswerRefused('this item is not on the page')
        if outcome == 'later':
            return change_page.NoMessageDelivery(),review_actions.later(item,fields.get('later',[''])[0],words)
        if outcome not in item.actions:
            raise question_record.AnswerRefused('choose an action on this card')
        delivery, answer = (review_actions.investigation_action(item,outcome,words,status)
                            if item.type == ReviewType.INVESTIGATION else
                            review_actions.decision_action(item,outcome,words,status))
        if outcome == 'question' and isinstance(delivery, change_page.MessageDelivery):
            delivery = review_actions.track_question(delivery, key, words, status)
        return delivery, answer
    _ = fields.pop('later',None)
    item = next((item for item in review_items.read(root, kinds=(ReviewType.QUESTION,)) if item.key == key), None) if key.startswith('question/') else None
    if item is not None and item.state == ReviewState.WAITING:
        if outcome not in item.actions:
            raise question_record.AnswerRefused('choose an action on this card')
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
.review-title{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.review-types{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
:is(.review-types button,[data-clear-linked-reviews]){display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:14px;min-height:36px;
background:var(--card);border:1px solid var(--line);border-radius:999px;color:var(--ink-2);padding:5px 14px;margin:0}
:is(.review-types button,[data-clear-linked-reviews])[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
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
.later-menu{position:relative}
[data-later-menu]{position:absolute;right:0;bottom:calc(100% + 6px);z-index:5;width:280px;max-width:calc(100vw - 32px);padding:5px;box-sizing:border-box;
background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.35)}
[data-later-menu] [hidden]{display:none!important}
.review-actions [data-later-menu] [role=menuitem]{display:flex;justify-content:space-between;align-items:center;gap:24px;width:100%;
min-height:32px;padding:5px 10px;border:0;border-radius:6px;background:none;color:var(--ink);text-align:left}
.review-actions [data-later-menu] [role=menuitem]:is(:hover,:focus){background:var(--accent);color:#fff;outline:none}
[data-later-menu] small{font-size:12px;color:var(--ink-3)}
[data-later-menu] [role=menuitem]:is(:hover,:focus) small{color:inherit;opacity:.85}
[data-later-menu] hr{border:0;border-top:1px solid var(--line);margin:5px 4px}
[data-later-menu][data-choosing]>:is([role=menuitem],hr){display:none}
.later-custom{padding:6px}
.later-month-header,.later-month-header>div,.later-save{display:flex;align-items:center;justify-content:space-between;gap:4px}
.later-month-header strong{font-size:14px}
.review-actions .later-month-header button{min-height:28px;width:28px;padding:0;border:0;font-size:24px;background:none}
[data-later-grid]{margin:8px 0 12px}
[data-later-grid]>[role=row]{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:2px;text-align:center}
[data-later-grid] [role=columnheader]{font-size:11px;color:var(--ink-3);padding:4px 0}
.review-actions [data-later-grid] button{width:100%;min-width:0;min-height:30px;padding:0;border:1px solid transparent;border-radius:50%;font-size:13px;background:none}
.review-actions [data-later-grid] [aria-current=date]{border-color:var(--accent)}
.review-actions [data-later-grid] [aria-selected=true]{background:var(--accent);color:#fff}
.review-actions [data-later-grid] button:disabled{opacity:.3}
.review-actions [data-later-grid] button:focus-visible{outline:2px solid var(--ink);outline-offset:1px}
[data-review-form] .later-time{display:flex;align-items:center;justify-content:space-between;font-size:12px;font-weight:400;margin:0 0 10px}
.review-actions .later-time input{min-height:32px;width:120px;padding:3px 8px;color-scheme:light dark}
.review-actions .later-time input[aria-invalid=true]{border-color:var(--error);color:var(--error)}
.later-save [data-later-summary]{font-size:11px;white-space:nowrap;color:var(--ink-3)}
.review-actions .later-save button{min-height:32px;padding:4px 10px;font-size:12px;flex-shrink:0}
.review-actions .later-save button:disabled{opacity:.4}
.review-actions button{margin:0;font:inherit;min-height:40px;padding:6px 14px;border:1px solid var(--ink-3);border-radius:8px;
background:var(--card);color:var(--ink)}.review-actions .primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.review-source{grid-column:1/-1;color:var(--ink-3);font-size:12px;margin-top:16px!important}
.list-table .list-row td:last-child{text-align:right}.list-table [data-detail]>td{text-align:left}
@media(max-width:960px){[data-review] .list-row td:last-child::before{content:'Age: '}
.review-panel{grid-template-columns:1fr;padding:0 14px 16px}.review-action-group+.review-action-group{padding-left:0;border:0}}
</style>'''

SCRIPT = page_frame.REFRESH_SCRIPT + record_table.SCRIPT + review_legacy.SCRIPT + r'''(() => {
let content=document.querySelector('[data-review]'),saving=0;
let selectedShow=content.dataset.show,requestedShow=selectedShow;
let entryJump=new URLSearchParams(location.hash.slice(1)).get('jump');
const drafts=new Map(),departures=new Map(),bodies=new Map();
let index=readIndex();
function readIndex(){
 return new Map(JSON.parse(content.querySelector('[data-review-index]').textContent).map(entry=>[entry.key,entry]));
}
function matchesOwner(entry,owner){
 return !owner || entry.owner===owner || entry.type==='investigation' && entry.sourceOwner===owner;
}
function matches(entry,{owner='',show='all',query='',type='',sourceOwner='',keys}){
 const linked=keys.has(entry.key);
 return (!keys.size || linked) && (linked || view.linkedItem===entry.key || entry.hiddenUntil<=Date.now()) &&
  matchesOwner(entry,owner) &&
  (!type || entry.type===type) && (!sourceOwner || entry.sourceOwner===sourceOwner) &&
  (!query || entry.id.toLowerCase().includes(query) || entry.title.toLowerCase().includes(query)) &&
  (show==='all' || entry.state===(show==='open' ? 'Open' : show));
}
function matching(filters,includePending=false){
 return [...index.values()].filter(entry=>{
  const departure=departures.get(entry.key);
  if(departure?.kind==='pending' && !includePending) return false;
  const projected=departure?.kind==='saved'
   ? {...entry,state:departure.state,hiddenUntil:departure.hiddenUntil}
   : entry;
  return matches(projected,filters);
 });
}
const view=createRecordTable({content:()=>content,single:true,
 place:reviewPlace,
 persistLinkedTarget:true,
 missingItem:()=> 'This review is no longer on the Review page.',
 resolveOwner:owner=>{
  const button=[...content.querySelectorAll('[data-owner-chip]')].find(button=>
   button.dataset.ownerChip===owner || button.dataset.ownerName===owner);
  return button ? button.dataset.ownerChip : owner;
 },
 selectOwner:({owner,type,query,keys})=>matching({owner,type,query,keys},true).length ? owner : '',
 shows:['open','Waiting','Done','all'],
 hasKey:key=>index.has(key),
 visible:(data,filters)=>{
  const entry=index.get(data.key);
  return Boolean(entry && !departures.has(data.key) && matches(entry,filters));
 },
 sortValues:data=>{
  const entry=index.get(data.key);
  return entry && [entry.id,entry.type,entry.ownerName,entry.title,entry.state,entry.filed];
 },
 applied:(_items,{type,show,owner,query,sourceOwner,keys})=>{
  selectedShow=show;loadBodies();
  const eligibleOwners=matching({type,query,keys},true);
  const shownOwners=matching({show,type,query,keys});
  content.querySelectorAll('[data-owner-chip]').forEach(button=>{
   const value=button.dataset.ownerChip;
   button.hidden=Boolean(value) && !eligibleOwners.some(entry=>matchesOwner(entry,value));
   button.querySelector('b').textContent=shownOwners.filter(entry=>matchesOwner(entry,value)).length;
  });
  content.querySelectorAll('[data-show-chip]').forEach(button=>{
   button.querySelector('b').textContent=matching({type,owner,query,sourceOwner,keys,show:button.dataset.showChip}).length;
  });
  content.querySelectorAll('[data-type-chip]').forEach(button=>{
   button.setAttribute('aria-pressed',String(button.dataset.typeChip===type));
   button.querySelector('b').textContent=matching({owner,show,query,sourceOwner,keys,type:button.dataset.typeChip}).length;
  });
  if(show!==requestedShow){
   requestedShow=show;
   if(!entryJump) queueMicrotask(()=>pageRefresh.refresh(true));
  }
 }
});
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
  const target=content.querySelector('[data-list-item][data-key="'+CSS.escape(key)+'"] [data-proposal-body]');
  if(!target || target.dataset.loaded) return;
  target.dataset.loaded='true';
  const panel=target.closest('.review-panel');
  if(bodies.has(key)){target.innerHTML=bodies.get(key);updateTitleFallback(panel);return;}
  fetch(location.pathname+'?'+new URLSearchParams({body:key}),{cache:'no-store'})
   .then(response=>response.ok ? response.text() : Promise.reject(new Error('HTTP '+response.status)))
   .then(text=>{bodies.set(key,text);if(target.isConnected){target.innerHTML=text;updateTitleFallback(panel);}})
   .catch(error=>{
    target.textContent='The proposal text is not available: '+error.message;
    target.dataset.titleIgnore='';updateTitleFallback(panel);
   });
 });
}
function bind(){
 content.querySelectorAll('.review-panel').forEach(updateTitleFallback);
 view.replace();
 content.querySelectorAll('[data-review-form]').forEach(form=>{
  const row=form.closest('[data-list-item]'),key=row.dataset.key,words=form.elements.feedback,later=form.elements.later;
  const notice=form.querySelector('[data-send-status]');
  notice.textContent=view.actionNotice(row);
  const choices=[...row.querySelectorAll('input[name=choice]')];
  if(drafts.has(key)){
   const draft=drafts.get(key);words.value=draft.feedback;later.value=draft.later;
   choices.forEach(choice=>choice.checked=choice.value===draft.choice);
  }
  const remember=()=>drafts.set(key,{feedback:words.value,later:later.value,choice:choices.find(choice=>choice.checked)?.value || ''});
  choices.forEach(choice=>choice.addEventListener('change',()=>{words.value=choice.value;remember();words.focus();}));
  [words,later].forEach(field=>{field.addEventListener('input',remember);field.addEventListener('change',remember);});
  const send=async(outcome,until)=>{
   if(form.dataset.sending) return;
   const data=new URLSearchParams(new FormData(form));data.set('outcome',outcome);
   if(outcome==='later') data.set('later',until);
   form.dataset.sending='true';saving++;pageRefresh.abandon();
   const buttons=[...form.querySelectorAll('button')];
   buttons.forEach(button=>button.disabled=true);notice.textContent='';
   departures.set(key,{kind:'pending'});
   view.startAction(row);
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    const result=await response.json();if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
    drafts.delete(key);
    const waits=outcome==='validate' || outcome==='question';
    departures.set(key,{kind:'saved',outcome,updated:row.dataset.updated,previousState:row.dataset.state,
     state:waits ? 'Waiting' : outcome==='later' ? row.dataset.state : 'Done',
     hiddenUntil:outcome==='later' ? Date.parse(until) : 0});
    view.closeCard(key);
    view.showActionResult(row,form.elements.row.value,'Saved: '+result.answer);
   }catch(error){
    departures.delete(key);
    view.showActionResult(row,form.elements.row.value,'Not saved: '+error.message);
    buttons.forEach(button=>button.disabled=false);
   }
   finally{
    delete form.dataset.sending;saving--;
    if(!saving && selectedShow!==content.dataset.show) void pageRefresh.refresh(true);
   }
  };
  form.addEventListener('submit',event=>{
   event.preventDefault();
   const outcome=event.submitter.value;
   if(outcome==='later'){
    const draft=parseLaterDraft(later.value);
    if(!draft) return;
    const at=laterEpoch(draft.date,draft.minutes);
    if(!(at>Date.now())) return;
    closeLater(form.querySelector('[data-later-menu]'),false);
    void send(outcome,new Date(at).toISOString());return;
   }
   void send(outcome,later.value);
  });
  bindLater(form,until=>void send('later',until));
 });
}
// The Later menu: presets resolve in the user zone each time the menu opens.
function zoneParts(epoch){
 const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:content.dataset.zone,hourCycle:'h23',
  year:'numeric',month:'numeric',day:'numeric',hour:'numeric',minute:'numeric',second:'numeric'})
  .formatToParts(epoch).map(part=>[part.type,Number(part.value)]));
 return parts;
}
function zoneEpoch(year,month,day,minutes){
 // Use both offsets near the wall time. Choose the earlier repeat or advance through a skipped hour.
 const wall=Date.UTC(year,month-1,day,0,minutes);
 const shownWall=epoch=>{const shown=zoneParts(epoch);
  return Date.UTC(shown.year,shown.month-1,shown.day,shown.hour,shown.minute,shown.second);};
 const offsets=new Set([-86400000,0,86400000].map(delta=>shownWall(wall+delta)-(wall+delta)));
 const candidates=[...offsets].map(offset=>wall-offset),exact=candidates.filter(epoch=>shownWall(epoch)===wall);
 return exact.length ? Math.min(...exact) : Math.max(...candidates);
}
function parseLaterDraft(wall){
 const match=/^(\d{4}-\d{2}-\d{2})T([^T]+)$/.exec(wall);
 if(!match) return null;
 const [,day,clock]=match,minutes=laterMinutes(clock);
 if(!Number.isFinite(minutes)) return null;
 const date=new Date(day+'T00:00:00Z');
 if(!Number.isFinite(date.getTime()) || date.toISOString().slice(0,10)!==day) return null;
 return {date,minutes};
}
function laterEpoch(date,minutes){
 return zoneEpoch(date.getUTCFullYear(),date.getUTCMonth()+1,date.getUTCDate(),minutes);
}
function laterMinutes(value){
 const text=value.trim(),match=/^(\d{1,2}):(\d{2})$/.exec(text) || /^(\d{2})(\d{2})$/.exec(text);
 if(!match) return NaN;
 const hour=Number(match[1]),minute=Number(match[2]);
 return hour<24 && minute<60 ? hour*60+minute : NaN;
}
function laterClock(minutes){
 return String(Math.floor(minutes/60)).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');
}
function laterTimes(now){
 const settings=JSON.parse(content.dataset.later),today=zoneParts(now);
 const at=(days,minutes)=>zoneEpoch(today.year,today.month,today.day+days,minutes);
 const weekday=new Date(Date.UTC(today.year,today.month-1,today.day)).getUTCDay();
 return {soon:now+settings.soon*60000,
  evening:today.hour*60+today.minute<settings.eveningUntil ? at(0,settings.evening) : 0,
  morning:at(1,settings.morning),week:at((settings.weekDay-weekday+7)%7 || 7,settings.weekTime)};
}
function closeLater(menu,focus){
 menu.hidden=true;menu.querySelector('[data-later-custom]').hidden=true;delete menu.dataset.choosing;
 const toggle=menu.parentElement.querySelector('[data-later-toggle]');toggle.setAttribute('aria-expanded','false');
 if(focus) toggle.focus();
}
function bindLater(form,pick){
 const toggle=form.querySelector('[data-later-toggle]'),menu=form.querySelector('[data-later-menu]');
 const custom=menu.querySelector('[data-later-custom]'),items=()=>[...menu.querySelectorAll('[role=menuitem]:not([hidden])')];
 const shown=new Intl.DateTimeFormat('en-US',{timeZone:content.dataset.zone,weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
 toggle.addEventListener('click',()=>{
  if(!menu.hidden){closeLater(menu,true);return;}
  const times=laterTimes(Date.now());
  menu.querySelectorAll('[data-later-preset]').forEach(item=>{
   const at=times[item.dataset.laterPreset];item.hidden=!at;item.dataset.at=at ? String(at) : '';
   item.querySelector('small').textContent=at ? shown.format(at) : '';
  });
  menu.hidden=false;toggle.setAttribute('aria-expanded','true');items()[0].focus();
 });
 menu.querySelectorAll('[data-later-preset]').forEach(item=>item.addEventListener('click',()=>{
  // Resolve the time again at the click, so a menu left open does not send a stale time.
  const at=laterTimes(Date.now())[item.dataset.laterPreset] || Number(item.dataset.at);
  closeLater(menu,false);pick(new Date(at).toISOString());
 }));
 const grid=custom.querySelector('[data-later-grid]'),time=custom.querySelector('[data-later-time]');
 const save=custom.querySelector('[value="later"]'),month=custom.querySelector('[data-later-month]');
 const dayText=date=>date.toISOString().slice(0,10);
 const monthText=new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'long',year:'numeric'});
 const dayLabel=new Intl.DateTimeFormat('en-US',{timeZone:'UTC',dateStyle:'full'});
 const summary=new Intl.DateTimeFormat('en-GB',{timeZone:content.dataset.zone,weekday:'short',day:'numeric',month:'short'});
 let selected=new Date(),display=new Date();
 const today=()=>{const now=zoneParts(Date.now());return new Date(Date.UTC(now.year,now.month-1,now.day));};
 const update=(normalize=false)=>{
  const minutes=laterMinutes(time.value),valid=Number.isFinite(minutes);
  const clock=valid ? laterClock(minutes) : '';
  time.setAttribute('aria-invalid',String(!valid));
  if(valid && normalize) time.value=clock;
  form.elements.later.value=valid ? dayText(selected)+'T'+clock : '';
  form.elements.later.dispatchEvent(new Event('input',{bubbles:true}));
  const at=valid ? laterEpoch(selected,minutes) : NaN;
  const resolved=Number.isFinite(at) ? zoneParts(at) : null;
  custom.querySelector('[data-later-summary]').textContent=resolved ? 'Hides until '+summary.format(at)+' '+
   laterClock(resolved.hour*60+resolved.minute) : 'Choose a time';
  save.disabled=!(at>Date.now());
 };
 const draw=()=>{
  month.textContent=monthText.format(display);grid.replaceChildren();
  grid.setAttribute('aria-label',month.textContent);
  const header=document.createElement('div');header.setAttribute('role','row');
  ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'].forEach(day=>{
   const cell=document.createElement('span');cell.setAttribute('role','columnheader');cell.setAttribute('aria-label',day);
   cell.textContent=day[0];header.append(cell);
  });grid.append(header);
  const first=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth(),1));
  const count=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth()+1,0)).getUTCDate();
  let row;
  for(let cell=0;cell<first.getUTCDay()+count;cell++){
   if(cell%7===0){row=document.createElement('div');row.setAttribute('role','row');grid.append(row);}
   const day=cell-first.getUTCDay()+1;
   if(day<1){const blank=document.createElement('span');blank.setAttribute('role','gridcell');row.append(blank);continue;}
   const date=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth(),day)),button=document.createElement('button');
   button.type='button';button.setAttribute('role','gridcell');button.textContent=day;
   button.dataset.date=dayText(date);button.setAttribute('aria-label',dayLabel.format(date));
   button.setAttribute('aria-selected',String(dayText(date)===dayText(selected)));
   button.tabIndex=dayText(date)===dayText(selected) ? 0 : -1;button.disabled=date<today();
   if(dayText(date)===dayText(today())) button.setAttribute('aria-current','date');
   button.addEventListener('click',()=>{selected=date;draw();grid.querySelector('[tabindex="0"]').focus();});row.append(button);
  }
  update();
 };
 menu.querySelector('[data-later-choose]').addEventListener('click',()=>{
  const draft=parseLaterDraft(form.elements.later.value);
  if(draft && laterEpoch(draft.date,draft.minutes)>Date.now()){
   selected=draft.date;time.value=laterClock(draft.minutes);
  }else{
   selected=today();selected.setUTCDate(selected.getUTCDate()+1);
   time.value=laterClock(JSON.parse(content.dataset.later).morning);
  }
  display=new Date(selected);
  custom.hidden=false;menu.dataset.choosing='true';draw();grid.querySelector('[tabindex="0"]').focus();
 });
 custom.querySelectorAll('[data-later-month-step]').forEach(button=>button.addEventListener('click',()=>{
  display=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth()+Number(button.dataset.laterMonthStep),1));
  draw();
 }));
 time.addEventListener('input',()=>update());
 time.addEventListener('blur',()=>update(true));
 time.addEventListener('keydown',event=>{
  const step={ArrowUp:15,ArrowDown:-15}[event.key];
  if(step===undefined) return;
  event.preventDefault();
  const minutes=laterMinutes(time.value);
  if(!Number.isFinite(minutes)) return;
  time.value=laterClock((minutes+step+1440)%1440);update();
 });
 grid.addEventListener('keydown',event=>{
  const days={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}[event.key];
  if(days===undefined) return;
  event.preventDefault();const date=new Date(selected);date.setUTCDate(date.getUTCDate()+days);
  if(date<today()) return;
  selected=date;display=new Date(date);draw();grid.querySelector('[tabindex="0"]').focus();
 });
 menu.addEventListener('keydown',event=>{
  if(event.key==='Escape'){event.preventDefault();closeLater(menu,true);return;}
  if(custom.contains(event.target)){
   if(event.key==='Enter'){
    event.preventDefault();update(true);if(!save.disabled) form.requestSubmit(save);
   }
   return;
  }
  const list=items(),index=list.indexOf(document.activeElement);
  const next={ArrowDown:index+1,ArrowUp:index-1+list.length,Home:0,End:list.length-1}[event.key];
  if(next===undefined) return;
  event.preventDefault();list[next%list.length].focus();
 });
}
document.addEventListener('pointerdown',event=>{
 content.querySelectorAll('[data-later-menu]:not([hidden])').forEach(menu=>{
  if(!menu.parentElement.contains(event.target)) closeLater(menu,false);
 });
});
async function refresh(_supersede=false,current=()=>true){
 // An open Later menu holds the page, so a refresh does not close it under the pointer.
 if(saving || [...content.querySelectorAll('[data-later-menu]:not([hidden])')].some(menu=>menu.getClientRects().length)) return;
 const show=selectedShow;
 const response=await fetch(location.pathname+'?'+new URLSearchParams({show}),{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html'),fresh=page.querySelector('[data-review]');
 if(!fresh) throw new Error('The page has no review table.');
 await view.settled();
 if(saving || !current() || show!==selectedShow) return;
 const top=scrollY,restoreFocus=restoreFocusAfterRefresh();
 const active=document.activeElement;
 const focusedOwnerId=active?.hasAttribute('data-owner-chip') ? active.id : '';
 content.replaceWith(fresh);content=fresh;index=readIndex();
 for(const [key,result] of departures){
  if(result.kind==='pending') continue;
  const entry=index.get(key);
  if(!entry || (result.outcome==='later' && Date.now()>=result.hiddenUntil)) {departures.delete(key);continue;}
  // The index includes the state and revision of rows outside this Show.
  const submitted=entry.state===result.previousState && entry.updated===result.updated;
  if(!submitted && (entry.state!==result.state || result.outcome==='validate')){
   departures.delete(key);
  }
 }
 bind();view.apply(false);
 const ownerChip=focusedOwnerId ? document.getElementById(focusedOwnerId) : null;
 if(focusedOwnerId && (!ownerChip || ownerChip.hidden)){
  content.querySelector('[data-owner-chip][aria-pressed="true"]')?.focus({preventScroll:true});
 }else restoreFocus();
 scrollTo({top,behavior:'instant'});return pageRefreshFacts(page);
}
view.place();
if(entryJump) view.jump(entryJump,{pushPlace:false});
bind();
if(entryJump) entryJump=null;
else view.apply();
addEventListener('hashchange',()=>{
 const item=new URLSearchParams(location.hash.slice(1)).get('item'),departure=departures.get(item);
 if(departure?.kind==='saved' && ['validate','later'].includes(departure.outcome)) departures.delete(item);
 view.place();view.apply(false);
});
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
})();'''
