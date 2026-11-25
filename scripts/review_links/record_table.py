"""Share table styles, filters, sorting and open rows across console pages."""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import quote

from scripts.lib import conf
from scripts.review_links import page_frame


DELIVERY_WINDOW_SECONDS = 30


def delivery_window_seconds(settings: conf.ConfigRead) -> int:
    """Read the limit for immediate refreshes after a save."""
    return settings.value('page_delivery_window_seconds', conf.positive_whole, default=DELIVERY_WINDOW_SECONDS)


def log_max_height_rem(settings: conf.ConfigRead) -> int:
    """Read the log height limit on a narrow page."""
    return settings.whole('list_log_max_height_rem')


def log_panel(log_id: str, entries: str) -> str:
    """Render a log that scrolls in its own area."""
    return ('<section class="list-log-panel"><h3>Log · newest first</h3>'
            + f'<div class="list-log-scroll" id="{log_id}" tabindex="0" role="region" aria-label="Log · newest first">'
            + '<ol class="list-log">' + entries + '</ol></div></section>')


RECORD_MISSING = '<span data-record-missing role="status" hidden>This item is no longer in the list.</span>'
RECORD_DETAIL_ERROR = '<div data-record-error role="status" hidden></div>'
RECORD_LINK_NOTICE = '<div data-record-link-notice role="status" hidden></div>'


def render_inline_detail() -> str:
    """Render the template for the open row."""
    return ('<template id="record-inline-view"><div data-record-inline-view>'
            + '<div class="record-row-status">' + RECORD_MISSING + RECORD_LINK_NOTICE + '</div>'
            + '<div data-record-inline-detail></div>' + RECORD_DETAIL_ERROR + '</div></template>')


@dataclass(frozen=True)
class Chip:
    value: str
    label_html: str
    count: int | None = None
    owner_name: str | None = None
    hidden: bool = False


class FilterFacet(NamedTuple):
    name: str
    field: str
    bit: int
    count_mask: int


def filter_facets(fields: Mapping[str, str]) -> tuple[FilterFacet, ...]:
    """Give each count a mask that excludes its own filter."""
    all_bits = (1 << len(fields)) - 1
    return tuple(FilterFacet(name, field, 1 << index, all_bits ^ (1 << index))
                 for index, (name, field) in enumerate(fields.items()))


def count_filter_row(facets: tuple[FilterFacet, ...], data: Mapping[str, str],
                     selected: Mapping[str, str], counts: Mapping[str, Counter[str]], *,
                     staff_filters: tuple[str, ...]) -> bool:
    """Count an eligible row for every facet and return whether it matches."""
    excluded = sum(bit for name, field, bit, _ in facets
                   if (choice := selected.get(name)) and choice != data[field]
                   and (name != "show" or choice != "all"))
    for name, field, _, count_mask in facets:
        if excluded & count_mask:
            continue
        total = counts[name]
        total["all"] += 1
        value = f"staff:{data[field]}" if name in staff_filters else data[field]
        if value != "all":
            total[value] += 1
    return not excluded


OWNER_QUERY_KEYS = ("owner", "showrunner")
LIST_QUERY_FIELDS = ("show", "unread", *OWNER_QUERY_KEYS, "producer", "q", "key", "item", "reveal", "state")


def identity_attribute(value: str) -> str:
    """Preserve identity characters that the HTML parser changes."""
    if "%" in value or "\r" in value or "\0" in value:
        value = value.replace("%", "%25").replace("\r", "%0D").replace("\0", "%00")
    return html.escape(value, quote=True)


def chip_row(name: str, chips: Iterable[Chip], selected: str, *, encode_values: bool = False,
             label_html: str = '') -> str:
    """Render a named filter with All as its first choice."""
    key = name.lower().replace(' ', '-')
    identity = html.escape(key, quote=True)
    choices = {chip.value: chip for chip in chips}
    all_chip = choices.pop('all', Chip('all', 'All'))
    selected = selected if selected in choices else 'all'
    buttons: list[str] = []
    for chip in (all_chip, *choices.values()):
        value = identity_attribute(chip.value) if encode_values else html.escape(chip.value, quote=True)
        pressed = str(chip.value == selected).lower()
        count = '' if chip.count is None else str(chip.count)
        owner = (f' data-owner-name="{html.escape(chip.owner_name, quote=True)}"'
                 if chip.owner_name is not None else '')
        hidden = ' hidden' if chip.hidden else ''
        buttons.append(f'<button type="button"{owner}{hidden} id="filter-{identity}-{quote(chip.value, safe="")}"'
                       + f' data-filter-value="{value}" aria-pressed="{pressed}" data-filter-count="{count}">'
                       + f'{chip.label_html} <b>{count}</b></button>')
    return labelled_chip_row(name, ''.join(buttons), filter_key=key, chip_id=f"list-{key}", label_html=label_html)


def labelled_chip_row(name: str, buttons: str, *, filter_key: str = '', chip_classes: str = '', chip_id: str = '',
                      label_html: str = '') -> str:
    """Render a visible label and a group of filter buttons."""
    label = html.escape(name, quote=True)
    group = f' role="group" aria-label="{label}"'
    key = f' data-filter-row="{html.escape(filter_key, quote=True)}"' if filter_key else ''
    classes = f' class="{html.escape(chip_classes, quote=True)}"' if chip_classes else ''
    identity = f' id="{html.escape(chip_id, quote=True)}"' if chip_id else ''
    return (f'<div class="record-filter-row"{group + key if filter_key else ""}>'
            + f'<span aria-hidden="true">{label_html or label}</span>'
            + f'<div{classes}{identity}{"" if filter_key else group}>{buttons}</div></div>')


def words_box(element_id: str, placeholder: str, *, value: str = '') -> str:
    """Render the words box for the filters."""
    identity = html.escape(element_id, quote=True)
    words = html.escape(placeholder, quote=True)
    initial = f' value="{html.escape(value, quote=True)}"' if value else ''
    return (f'<div class="record-filter-words"><input type="search" id="{identity}" data-filter-words'
            + f' autocomplete="off" aria-label="{words}" placeholder="{words}"{initial}>'
            + f'<button type="button" id="{identity}-clear" data-clear-filter-words aria-label="Clear filter">×</button></div>')


WEEKDAYS = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
CLOCK = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')


def clock_minutes(settings: conf.ConfigRead, key: str) -> int:
    """Read a configured clock time as minutes since midnight."""
    value = settings.text(key)
    match = CLOCK.fullmatch(value)
    if match is None:
        raise ValueError(f'review_links.conf: {key}={value!r} is not a time such as 09:00')
    return int(match[1])*60+int(match[2])


def park_settings(settings: conf.ConfigRead) -> dict[str, int]:
    """Read the settings for the shared park control."""
    day = settings.text('later_week_day')
    if day not in WEEKDAYS:
        raise ValueError(f'review_links.conf: later_week_day={day!r} is not a weekday name such as Monday')
    return {'soon': settings.whole('later_soon_minutes'), 'morning': clock_minutes(settings, 'later_morning'),
            'weekDay': WEEKDAYS.index(day), 'weekTime': clock_minutes(settings, 'later_week_time'),
            'step': settings.whole('park_time_step_minutes')}


def park_control(label: str, *, field: str, action: str, value: str, identity: str = '',
                 allow_delivery: bool = False) -> str:
    """Render the park menu and date picker."""
    id_html = f' id="{html.escape(identity, quote=True)}"' if identity else ''
    action = html.escape(action, quote=True)
    presets: tuple[tuple[str, str], ...] = (('delivered', 'Until delivered'),) if allow_delivery else ()
    presets += (('soon', '1 hour'), ('morning', 'Tomorrow'), ('week', 'Next week'))
    buttons = ''.join(
        f'<button type="submit" role="menuitem" tabindex="-1" name="{action}" value="park-until-{key}"'
        + f' data-later-preset="{key}"><span>{text}</span><small></small></button>'
        for key, text in presets
    )
    return ('<div class="later-menu"><button type="button"' + id_html
            + f' aria-haspopup="menu" aria-expanded="false" data-later-toggle>{label}'
            + ' <span aria-hidden="true">▾</span></button>'
            + f'<div role="menu" aria-label="{label}" data-later-menu hidden>' + buttons + '<hr>'
            + '<button type="button" role="menuitem" tabindex="-1" data-later-choose><span>Pick a date…</span></button>'
            + '<div class="later-custom" data-later-custom hidden>'
            +f'<input type="hidden" name="{html.escape(field, quote=True)}">'
            +'<div class="later-month-header"><strong data-later-month aria-live="polite"></strong><div>'
            +'<button type="button" tabindex="-1" data-later-month-step="-1" aria-label="Previous month">‹</button>'
            +'<button type="button" tabindex="-1" data-later-month-step="1" aria-label="Next month">›</button></div></div>'
            +'<div role="grid" aria-label="Choose a day" data-later-grid></div>'
            # Keep Enter in the time field from submitting the first form action.
            +'<div class="later-time"><label>Time<input type="text" inputmode="numeric" autocomplete="off" form="" '
            +'role="combobox" aria-expanded="false" data-later-time></label>'
            +'<button type="button" tabindex="-1" aria-label="Show suggested times" data-later-time-choices>▾</button>'
            +'<div role="listbox" aria-label="Suggested times" data-later-times hidden></div></div>'
            +'<div class="later-save"><span data-later-summary aria-live="polite"></span>'
            +f'<button class="primary" name="{action}" value="{html.escape(value, quote=True)}">Save</button>'
            +'</div></div></div></div>')


EMPTY_RESULT_STYLE = '''<style>
[data-empty-result] button[hidden]{display:none!important}
[data-empty-result] td.empty-result-cell{padding:12px 16px;text-align:left;white-space:normal;color:var(--ink-2)}
.empty-result-content,.empty-result-actions{display:flex;align-items:center;flex-wrap:wrap}
.empty-result-content{gap:8px 16px;overflow-wrap:anywhere}.empty-result-actions{gap:8px}
.empty-result-actions button{font:inherit;min-height:44px;margin:0;padding:6px 12px;
border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);cursor:pointer}
.empty-result-actions button:hover{background:var(--hover)}
@media(max-width:960px){.list-table [data-empty-result] :is(tr,td){display:block}}
</style>'''

EMPTY_RESULT_SCRIPT = r'''
const emptyResultRows=new WeakMap();
const emptyResultList=new Intl.ListFormat('en',{type:'conjunction'});
function updateEmptyResult(table,{empty,noun='item',search='',labels=[],clearSearch,resetFilters}){
 const words=search.trim();
 let row=emptyResultRows.get(table);
 if(!empty || (!words && !labels.length)){
  row?.remove();
  return false;
 }
 if(!row){
  row=table.createTBody();
  row.dataset.emptyResult='';
  const cell=row.insertRow().insertCell();
  cell.className='empty-result-cell';
  cell.colSpan=[...table.tHead.rows[0].cells].reduce((width,heading)=>width+heading.colSpan,0);
  cell.innerHTML='<div class="empty-result-content"><span role="status"></span>'
   +'<div class="empty-result-actions"><button type="button" data-clear-empty-search>Clear search</button>'
   +'<button type="button" data-reset-empty-filters>Show all items</button></div></div>';
  emptyResultRows.set(table,row);
 }
 if(row.parentElement!==table) table.append(row);
 const filters=emptyResultList.format(labels);
 const status=row.querySelector('[role="status"]');
 const clear=row.querySelector('[data-clear-empty-search]');
 const reset=row.querySelector('[data-reset-empty-filters]');
 const subject='No '+noun;
 const text=words ? subject+' contains "'+words+'"'+(filters ? ' and matches '+filters : '')+'.'
  : subject+' matches '+filters+'.';
 clear.hidden=!words;
 clear.onclick=clearSearch ?? null;
 reset.hidden=!filters;
 reset.onclick=resetFilters ?? null;
 if(status.textContent!==text) status.textContent=text;
 return true;
}
'''


STYLE = EMPTY_RESULT_STYLE + '''<style>
[hidden]{display:none!important}body{background:var(--bg);color:var(--ink)}
main{font-size:15px;line-height:1.5}
.list-filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.list-filters label{display:inline;color:var(--ink-2)}
.list-filters input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);border-radius:8px;max-width:100%;min-width:0}
.list-filters input{width:260px}
.list-search-label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.record-filter-words{position:relative;width:300px;max-width:100%;margin:0 0 10px auto}
.record-filter-words input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--ink);width:100%;min-width:0;box-sizing:border-box}
.record-filter-words input:not(:placeholder-shown){padding-inline-end:44px;border-color:var(--filter-selected-bg)}
.record-filter-words input::-webkit-search-cancel-button{appearance:none}
.record-filter-words input:placeholder-shown+[data-clear-filter-words]{display:none}
@media(max-width:340px){.record-filter-words input::placeholder{font-size:14px}}
[data-clear-filter-words]{position:absolute;inset-block:0;inset-inline-end:0;width:44px;margin:0;padding:0;
border:0;border-radius:8px;background:none;color:var(--ink-2);font-size:22px;cursor:pointer}
[data-clear-filter-words]:hover{color:var(--ink)}
.record-filter-row{display:grid;grid-template-columns:5rem minmax(0,1fr);align-items:baseline;gap:8px;margin-bottom:8px}
.record-filter-row>span{color:var(--ink-2);font-size:13px}
.record-filter-row>div{display:flex;flex-wrap:wrap;gap:6px}
[data-filter-row] button{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:13px;min-height:36px;
color:var(--ink);border:1px solid var(--line);border-radius:999px;background:var(--card);padding:4px 10px;margin:0;
max-width:100%;overflow-wrap:anywhere}
[data-filter-row] button[aria-pressed=true]{background:var(--filter-selected-bg);border-color:var(--filter-selected-bg);
color:var(--filter-selected-ink)}
.record-filter-row button[aria-pressed=true] .staff-name{color:inherit!important}
[data-filter-row] button[data-filter-count="0"][aria-pressed=false]{opacity:.6}
[data-filter-row] button[data-filter-count="0"][aria-pressed=true]{
background:color-mix(in srgb,var(--filter-selected-bg) 75%,var(--card))}
[data-filter-row] b{font-variant-numeric:tabular-nums}[data-filter-row] b:empty{display:none}
.list-table{border:1px solid var(--line);border-radius:12px;background:var(--card);overflow:hidden}
.list-table table{width:100%;border-collapse:collapse;table-layout:fixed}
.list-table th{text-align:left;color:var(--ink-2);font-size:13px;border-bottom:1px solid var(--line)}
.list-table th button{font:inherit;font-weight:600;color:inherit;background:none;border:0;margin:0;padding:10px 8px;
min-height:44px;width:100%;text-align:left;border-radius:0}
.list-table th[aria-sort=ascending],.list-table th[aria-sort=descending]{color:var(--accent)}
.list-table td{padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
.list-table .record-id{white-space:nowrap;overflow-wrap:normal;width:1%}
.list-toggle{white-space:nowrap}
.list-table tbody:last-child td{border-bottom:0}.list-row{cursor:pointer}.list-row:hover,.list-row.open{background:var(--hover)}
.list-table td:nth-child(2),.list-table td:nth-child(5),.list-table td:nth-child(6){color:var(--ink-2)}
.list-table small{display:block;color:var(--ink-3);font-size:12px;margin-top:4px}
.list-toggle{display:flex;gap:4px;align-items:center;padding:0!important;background:none;border:0;color:var(--ink-2);
font:inherit;font-variant-numeric:tabular-nums;margin:0;min-height:24px}
.list-chevron{font-size:22px;line-height:1;transition:transform .12s}.list-toggle[aria-expanded=true] .list-chevron{transform:rotate(90deg)}
.list-state{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:600}
.warning{color:var(--warning)}.error{color:var(--error)}.accent{color:var(--accent)}.room{color:var(--room)}.muted{color:var(--ink-3)}
.list-dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--accent)}
.list-table td:last-child{text-align:center}[data-detail]>td{padding:0;background:var(--hover)}
.list-panel{display:grid;grid-template-columns:1fr 1fr;gap:32px;padding:4px 24px 24px;border-left:3px solid var(--accent);text-align:left}
.list-panel h3{font-size:12px;letter-spacing:.03em;text-transform:uppercase;color:var(--ink-3);margin:16px 0 5px}
.list-panel p,.list-panel ul,.list-panel ol{margin:0 0 8px}.list-panel section+section{border:0;margin:0;padding:0}
.list-panel a,.list-counts a{color:var(--accent)}.list-panel ol.list-log{list-style:none;padding:0;font-size:13px}
.list-log-panel{contain:size;display:flex;flex-direction:column;min-width:0;min-height:0}
.list-log-panel h3{flex:none}
.list-log-scroll{min-height:0;overflow-y:auto;scrollbar-color:var(--ink-3) transparent}
.list-log-scroll .list-log{margin-bottom:0}
.list-log li{display:grid;grid-template-columns:130px 1fr;gap:12px;padding:6px 0;border-bottom:1px dashed var(--line)}
.list-log time,.list-log small{color:var(--ink-3)}.list-log small{display:inline;font-size:inherit}
[data-feedback-form]{margin-top:20px;border-top:1px solid var(--line);padding-top:16px}
.list-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
[data-feedback-form] label{font-size:13px;color:var(--ink-2);display:block}
[data-feedback-form] input,[data-feedback-form] textarea{font:inherit;background:var(--card);color:var(--ink);
border:1px solid var(--line);border-radius:8px;padding:8px;min-width:0;max-width:100%;box-sizing:border-box}
[data-feedback-form] textarea{display:block;width:100%;resize:vertical;margin:6px 0 8px}
[data-feedback-form] button{margin:0;min-height:44px}[data-send-status]{margin-left:10px;font-size:13px;color:var(--ink-2)}
.list-counts{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:20px;color:var(--ink-2);font-size:13px}
.list-count-chip{border:1px solid var(--line);border-radius:999px;background:var(--card);padding:5px 12px}
[data-window-before],[data-window-after],[data-window-gap]{border:0!important;margin:0!important;border-radius:0!important}
:is(.record-row-status,.review-card-status){height:52px;box-sizing:border-box;padding-inline:24px;display:flex;align-items:center;gap:8px}
.review-card-status [data-show-record-updates]{flex:none;margin:0;font:inherit;min-height:44px;padding:6px 14px;
border:1px solid var(--ink-3);border-radius:8px;background:var(--card);color:var(--ink)}
.review-card-status [data-show-record-updates]:focus-visible{outline:2px solid currentColor;outline-offset:2px}
@media(max-width:480px){:is(.record-row-status,.review-card-status){padding-inline:16px}}
[data-record-inline-view] .record-row-status{border-inline-start:3px solid var(--accent);text-align:left}
[data-record-inline-view] .record-row-status:not(:has(> :not([hidden]))){display:none}
[data-record-inline-view] [data-record-error]{padding-inline:24px;text-align:left}
[data-record-inline-view] [data-record-link-notice]{padding:0;border:0}
:is([data-record-missing],[data-record-error],[data-record-link-notice]){color:var(--warning)}
:is([data-record-error],[data-record-link-notice]){padding-block:12px;border-top:1px solid var(--line);overflow-wrap:anywhere}
[data-review-card] button[aria-disabled="true"]{opacity:.5;cursor:default}
[data-empty],[data-missing-keys]{padding:16px;margin:0;color:var(--ink-2)}
[data-missing-keys]{white-space:pre-line}
[data-missing-item]{color:var(--ink-2);scroll-margin-top:16px}
.list-table .list-row[data-record-highlight]{background:color-mix(in srgb,var(--accent) 28%,var(--card))}
@media(prefers-reduced-motion:no-preference){
.list-table .list-row[data-record-highlight]{animation:record-highlight 1200ms ease-out}
@keyframes record-highlight{to{background:var(--hover)}}
}
.list-table .record-motion-source{display:none!important}
.list-table td.record-motion-cell{padding:0;border:0;text-align:left}
.record-motion-clip{position:relative;overflow:hidden;overflow-anchor:none}
.list-table .record-motion-clip>table{position:absolute;inset:0 0 auto;width:100%}
@media(max-width:960px){
.list-table{border:0;background:none;border-radius:0}.list-table table,.list-table tbody{display:block;width:100%}
.list-table colgroup{display:none}.list-table thead{display:flex;margin-bottom:12px}.list-table thead tr{display:flex;flex-wrap:wrap}
.list-table th{border:0}.list-table th button{border:1px solid var(--line);border-radius:8px;width:auto;padding:8px;margin:0 6px 6px 0}
.list-table tbody{border:1px solid var(--line);border-radius:12px;background:var(--card);margin-bottom:16px;overflow:hidden}
.list-table [data-record-motion]{margin:0;border:0;background:none}
.list-table .record-motion-cell{display:block}
.list-row{display:grid;grid-template-columns:72px minmax(0,1fr)}.list-table .list-row td{border:0;padding:5px 14px}
.list-table .list-row td:first-child{grid-column:1;grid-row:1;padding-top:14px}
.list-table .list-row td:nth-child(2){grid-column:2;grid-row:1;padding-top:14px;text-align:right}
.list-table .list-row td:nth-child(n+3){grid-column:1/-1}.list-table .list-row td:nth-child(3){font-size:17px}
.list-table .list-row td:nth-child(5)::before,.list-table .list-row td:nth-child(6)::before{content:attr(data-label) ': ';color:var(--ink-3)}
.list-table .list-row td:last-child{text-align:left;padding-bottom:14px}.list-table .list-row td:last-child::before{content:'Update: ';color:var(--ink-3)}
[data-detail]{display:block}[data-detail]>td{display:block}.list-panel{grid-template-columns:1fr;padding:0 14px 16px;gap:4px}
.list-log-panel{contain:none}.list-log-scroll{max-height:var(--list-log-max-height)}
.list-log li{grid-template-columns:105px minmax(0,1fr)}
}
@media(prefers-reduced-motion:reduce){.list-chevron{transition:none}}
</style>'''


DATE_TIME_STYLE = '''<style>
.later-menu{position:relative}
[data-later-menu]{position:absolute;right:0;bottom:calc(100% + 6px);z-index:5;width:280px;max-width:calc(100vw - 32px);padding:5px;box-sizing:border-box;
background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,.35)}
[data-later-menu] [hidden]{display:none!important}
[data-later-menu] button{margin:0;font:inherit;min-height:40px;padding:6px 14px;border:1px solid var(--ink-3);border-radius:8px;
background:var(--card);color:var(--ink)}[data-later-menu] .primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
.later-custom{padding:6px}
.later-month-header,.later-month-header>div,.later-save{display:flex;align-items:center;justify-content:space-between;gap:4px}
.later-month-header strong{font-size:14px}
[data-later-menu] .later-month-header button{min-height:28px;width:28px;padding:0;border:0;font-size:24px;background:none}
[data-later-grid]{margin:8px 0 12px}
[data-later-grid]>[role=row]{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:2px;text-align:center}
[data-later-grid] [role=columnheader]{font-size:11px;color:var(--ink-3);padding:4px 0}
[data-later-menu] [data-later-grid] button{width:100%;min-width:0;min-height:30px;padding:0;border:1px solid transparent;border-radius:50%;font-size:13px;background:none}
[data-later-menu] [data-later-grid] [aria-current=date]{border-color:var(--accent)}
[data-later-menu] [data-later-grid] [aria-selected=true]{background:var(--accent);color:#fff}
[data-later-menu] [data-later-grid] button:disabled{opacity:.3}
[data-later-menu] [data-later-grid] button:focus-visible{outline:2px solid var(--ink);outline-offset:1px}
[data-later-menu] .later-time{position:relative;margin:0 0 10px}
[data-later-menu] .later-time label{display:flex;align-items:center;justify-content:space-between;font-size:12px;font-weight:400}
[data-later-menu] .later-time input{font:inherit;min-height:32px;width:120px;max-width:100%;box-sizing:border-box;padding:3px 28px 3px 8px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);color-scheme:light dark}
[data-later-menu] .later-time input[aria-invalid=true]{border-color:var(--error);color:var(--error)}
[data-later-menu] [data-later-time-choices]{position:absolute;right:2px;top:2px;width:26px;min-height:28px;padding:0;border:0;background:none}
[data-later-times]{position:absolute;right:0;top:100%;z-index:1;box-sizing:border-box;width:120px;max-height:144px;overflow:auto;
background:var(--card);border:1px solid var(--line);border-radius:6px}
[data-later-times] [role=option]{padding:4px 8px;cursor:pointer}
[data-later-times] [role=option]:hover,[data-later-times] [aria-selected=true]{background:var(--accent);color:#fff}
[data-later-menu] button[role=menuitem]{display:flex;justify-content:space-between;align-items:center;gap:24px;width:100%;
min-height:32px;padding:5px 10px;border:0;border-radius:6px;background:none;color:var(--ink);text-align:left}
[data-later-menu] [role=menuitem]:focus{background:var(--accent);color:#fff;outline:none}
[data-later-menu] small{font-size:12px;color:var(--ink-3)}
[data-later-menu] [role=menuitem]:focus small{color:inherit;opacity:.85}
[data-later-menu] hr{border:0;border-top:1px solid var(--line);margin:5px 4px}
[data-later-menu][data-choosing]>:is([role=menuitem],hr){display:none}
.later-save [data-later-summary]{font-size:11px;white-space:nowrap;color:var(--ink-3)}
[data-later-menu] .later-save button{min-height:32px;padding:4px 10px;font-size:12px;flex-shrink:0}
[data-later-menu] .later-save button:disabled{opacity:.4}
</style>'''


DETAIL_SCRIPT = r'''
function recordDetails(item){return item?.openDetails ?? item?.querySelector('[data-detail]>td');}
function recordIsOpen(item){return item?.querySelector('.list-toggle')?.getAttribute('aria-expanded')==='true';}
function recordFromDetail(element){
 return element?.closest('[data-list-item]');
}
'''


PLACE_SCRIPT = 'const listQueryFields=' + json.dumps(LIST_QUERY_FIELDS) + r''';
function recordPlaceParams({expandFilters}={}, {search,hash}=location){
 const params=new URLSearchParams(search);
 expandFilters?.(params);
 const preferred=new Set(listQueryFields.filter(key=>params.has(key)));
 // The server cannot see the fragment, so its values apply after load.
 const fragment=new URLSearchParams(hash.slice(1));
 expandFilters?.(fragment);
 for(const key of new Set(fragment.keys())) if(!preferred.has(key)) params.delete(key);
 for(const [key,value] of fragment) if(!preferred.has(key)) params.append(key,value);
 return params;
}
'''

SCRIPT = (DETAIL_SCRIPT + PLACE_SCRIPT + EMPTY_RESULT_SCRIPT + page_frame.ACTION_STATUS_SCRIPT
          + 'const ownerQueryKeys=' + json.dumps(OWNER_QUERY_KEYS) + r''';
const staffChipPrefix='staff:';
const encodeIdentity=value=>value.replace(/[%\r\0]/g,character=>encodeURIComponent(character));
const staffChipId=value=>decodeURIComponent(value.slice(staffChipPrefix.length));
const staffChipValue=id=>staffChipPrefix+encodeIdentity(id);
let recordSpaceAbove,recordSpaceBelow,recordScrollTop,trackedRecordRow;
const recordScrollInputs=['wheel','touchmove','keydown'];
const recordScrollKeys=new Set(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' ']);
function recordSpaceHeight(space){return space?.getBoundingClientRect().height ?? 0;}
function setRecordSpace(space,height){
 if(height>0){space.style.height=height+'px';return space;}
 space?.remove();
}
function createRecordSpace(frame,above){
 const space=document.createElement('div');
 space.setAttribute('aria-hidden','true');
 space.style.overflowAnchor='none';
 if(above) frame.prepend(space);else frame.append(space);
 return space;
}
function bindRecordScroll(){
 if(recordSpaceAbove || recordSpaceBelow || trackedRecordRow)
  addEventListener('scroll',recordScrolled,{passive:true});
 else removeEventListener('scroll',recordScrolled);
 for(const type of recordScrollInputs){
  if(recordSpaceAbove) addEventListener(type,recordScrollAtLimit,{passive:true});
  else removeEventListener(type,recordScrollAtLimit);
 }
}
function stopTrackingRecordRow(){
 trackedRecordRow?.observer.disconnect();
 trackedRecordRow=undefined;
 bindRecordScroll();
}
function clearRecordSpace(){
 recordSpaceAbove?.remove();recordSpaceBelow?.remove();
 recordSpaceAbove=recordSpaceBelow=undefined;
 stopTrackingRecordRow();
}
function releaseRecordPosition(current){
 const height=recordSpaceHeight(recordSpaceAbove);
 clearRecordSpace();
 const target=Math.max(0,current-height);
 if(height && target!==window.scrollY) window.scrollTo({top:target,behavior:'instant'});
}
function recordScrolled(){
 const current=window.scrollY;
 if(current!==recordScrollTop) releaseRecordPosition(current);
}
function recordScrollAtLimit(event){
 // Input at a scroll limit can leave the scroll position unchanged.
 const {scrollHeight,clientHeight}=document.scrollingElement;
 if(window.scrollY>0 && window.scrollY<scrollHeight-clientHeight) return;
 if(event.defaultPrevented || (event.type==='wheel' && (!event.deltaY || event.ctrlKey))) return;
 if(event.type==='keydown'){
  if(!recordScrollKeys.has(event.key) || event.altKey || event.metaKey) return;
  const target=event.target instanceof Element ? event.target : null;
  if(target?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])') ||
     (event.key===' ' && target?.closest('button,summary,[role="button"]'))) return;
 }
 releaseRecordPosition(window.scrollY);
}
function alignRecordRowTop(row,top,remainingShrink){
 const difference=row.getBoundingClientRect().top-top;
 if(!difference && !remainingShrink && !recordSpaceBelow) return;
 const frame=row.closest('main');
 if(!frame){
  window.scrollBy({top:difference,behavior:'instant'});
  recordScrollTop=window.scrollY;
  return;
 }
 const hadSpaceAbove=Boolean(recordSpaceAbove),hadSpaceBelow=Boolean(recordSpaceBelow);
 const scroller=document.scrollingElement;
 // Scroll anchoring would move the page when the top space changes.
 const anchor=scroller.style.overflowAnchor;
 scroller.style.overflowAnchor='none';
 const desired=window.scrollY+difference-recordSpaceHeight(recordSpaceAbove);
 // Firefox scrolls by whole pixels from a fractional position, so the target keeps the current fraction.
 const target=Math.max(0,window.scrollY+Math.ceil(desired-window.scrollY)),above=target-desired;
 if(above>0 && !recordSpaceAbove) recordSpaceAbove=createRecordSpace(frame,true);
 recordSpaceAbove=setRecordSpace(recordSpaceAbove,above);
 const {scrollHeight,clientHeight}=document.scrollingElement;
 // scrollHeight is at least the viewport height, even when the body is shorter.
 const documentHeight=scrollHeight>clientHeight ? scrollHeight : document.body.getBoundingClientRect().bottom+window.scrollY;
 // Keep the current scroll reachable even if the animation shrinks in one frame.
 const below=target ? Math.ceil(target+clientHeight+remainingShrink-documentHeight+recordSpaceHeight(recordSpaceBelow)) : 0;
 if(below>0 && !recordSpaceBelow) recordSpaceBelow=createRecordSpace(frame,false);
 recordSpaceBelow=setRecordSpace(recordSpaceBelow,below);
 window.scrollTo({top:target,behavior:'instant'});
 scroller.style.overflowAnchor=anchor;
 recordScrollTop=window.scrollY;
 if(hadSpaceAbove!==Boolean(recordSpaceAbove) || hadSpaceBelow!==Boolean(recordSpaceBelow)) bindRecordScroll();
}
function visibleRecordRow(item){
 const row=item?.querySelector('.list-row');
 if(row?.isConnected && row.getClientRects().length) return row;
}
function trackRecordRowDuringMotion(row,top,motions){
 stopTrackingRecordRow();
 if(!row) return;
 const clips=[...motions.values()].filter(motion=>motion.leaving).map(motion=>motion.clip);
 const remainingShrink=()=>clips.reduce((height,clip)=>height+clip.getBoundingClientRect().height,0);
 alignRecordRowTop(row,top,remainingShrink());
 if(!motions.size) return;
 const observer=new ResizeObserver(()=>{
  if(trackedRecordRow?.observer!==observer) return;
  recordScrolled();
  if(trackedRecordRow && row.isConnected && row.getClientRects().length) alignRecordRowTop(row,top,remainingShrink());
 });
 trackedRecordRow={row,top,observer};
 for(const {clip} of motions.values()) observer.observe(clip);
 recordScrollTop=window.scrollY;
 bindRecordScroll();
}
function activeChipLabels(content,selector){
 return [...content.querySelectorAll(selector)].map(button=>{
  const label=[...button.childNodes].filter(node=>!node.matches?.('b,[aria-hidden="true"]'))
   .map(node=>node.textContent).join('').trim();
  return button.closest('[role="group"]').getAttribute('aria-label')+': '+label;
 });
}
const filterControl={
 wordValues:new WeakMap(),
 getWords(control){return this.wordValues.get(control) ?? '';},
 setWords(control,value){
  this.wordValues.set(control,value);
  if(control.value!==value) control.value=value;
 },
 row(root,name){return root.querySelector('[data-filter-row="'+name+'"]');},
 buttons(row){return [...row.querySelectorAll('[data-filter-value]')];},
 value(button){return button.dataset.filterValue;},
 ownerId(button){return this.value(button)==='all' ? '' : staffChipId(this.value(button));},
 setCount(button,count){
  button.dataset.filterCount=String(count);
  button.querySelector('b').textContent=String(count);
 },
 setCounts(row,counts){
  for(const button of this.buttons(row))
   this.setCount(button,Object.hasOwn(counts,this.value(button)) ? counts[this.value(button)] : 0);
 },
 setVisible(row,values){
  const visible=new Set(values),buttons=this.buttons(row);
  const selected=buttons.find(button=>button.getAttribute('aria-pressed')==='true');
  for(const button of buttons)
   button.hidden=button.dataset.filterValue!=='all' && !visible.has(button.dataset.filterValue);
  return this.select(row,selected?.dataset.filterValue);
 },
 select(row,value,{allowHidden=false,fallback='all'}={}){
  const buttons=this.buttons(row);
  const selected=buttons.some(button=>(allowHidden || !button.hidden) && button.dataset.filterValue===value) ? value : fallback;
  for(const button of buttons) button.setAttribute('aria-pressed',String(button.dataset.filterValue===selected));
  return selected;
 },
 updateOwners(row,owners){
  const [all,...choices]=this.buttons(row);
  const current=new Map(choices.map(button=>[this.ownerId(button),button]));
  for(const [id,button] of current)
   if(!owners.has(id) && button.getAttribute('aria-pressed')!=='true') button.remove();
  for(const [id,{name,label}] of owners){
   let button=current.get(id);
   if(!button){
    button=all.cloneNode(true);
    button.id='filter-owner-'+encodeURIComponent(staffChipPrefix+id).replace(/[!'()*]/g,character=>
     '%'+character.charCodeAt(0).toString(16).toUpperCase());
    button.dataset.filterValue=staffChipValue(id);
    button.setAttribute('aria-pressed','false');
    all.parentElement.append(button);
   }
   const count=button.querySelector('b');
   button.dataset.ownerName=name;
   button.innerHTML=label+' ';button.append(count);
  }
 },
 bind(control,change){
  control.addEventListener('filterchange',event=>change(event.detail));
  const emit=value=>control.dispatchEvent(new CustomEvent('filterchange',{bubbles:true,detail:value}));
  if(control.hasAttribute('data-filter-words')){
   let timer;
   const clear=()=>{
    clearTimeout(timer);
    this.setWords(control,'');
    control.focus({preventScroll:true});
    emit('');
   };
   control.addEventListener('input',()=>{
    this.setWords(control,control.value);
    clearTimeout(timer);
    timer=setTimeout(()=>{if(control.isConnected) emit(this.getWords(control));},120);
   });
   control.parentElement.querySelector('[data-clear-filter-words]').addEventListener('click',clear);
   control.addEventListener('keydown',event=>{
    if(event.key!=='Escape' || event.defaultPrevented || event.isComposing || !control.value) return;
    event.preventDefault();
    clear();
   });
  }else{
   control.addEventListener('click',event=>{
    const button=event.target.closest('[data-filter-value]');
    if(button && !button.hidden) emit(this.select(control,button.dataset.filterValue));
   });
  }
 }
};
function recordItemLink(params,resolve){
 const item=params.get('jump') || params.get('item'),saved=history.state?.recordTable;
 return item && (!saved || saved.pendingItem) ? resolve(item) : item;
}
function resolveShownRecordId(index,value){
 if(!value || index.has(value)) return value;
 const shown=value.toLowerCase();
 let key=null;
 for(const entry of index.values()){
  if(entry.id.toLowerCase()!==shown) continue;
  if(key!==null) return value;
  key=entry.key;
 }
 return key ?? value;
}
function createRecordTable(options){
const contentProxy=()=>options.content();
const defaultShow=options.defaultShow ?? 'open';
let content=contentProxy(),owner='',show=defaultShow,query='',sort='',direction='asc',type='',sourceOwner='',keys=new Set();
let linkedItem='',revealedLinkedItem='',pendingItem=false;
let placeHash=location.hash;
let highlightedRow,highlightTimer;
const opened=new Set(),filterNames=options.filters || [],filters={};
const columns=[...content.querySelectorAll('[data-sort]')].map(button=>button.textContent);
const rowMotion=new Map(),actionNotices=new Map();
const motionsByKey=()=>new Map([...rowMotion].map(([node,motion])=>[node.dataset.key,motion]));
let latestSubmission=0,latestActionFailure=null;
const boundRoots=new WeakSet();
const collator=new Intl.Collator(undefined,{numeric:true,sensitivity:'base'});
let windowEntries=[],pendingFrame=0,revealKey='';
const inlineView=document.getElementById('record-inline-view')?.content.firstElementChild.cloneNode(true);
const viewDetail=inlineView?.querySelector('[data-record-inline-detail]');
const detailFocus=()=>openItem?.querySelector('.list-toggle');
const detailMissing=inlineView?.querySelector('[data-record-missing]');
const detailError=inlineView?.querySelector('[data-record-error]');
const detailLinkNotice=inlineView?.querySelector('[data-record-link-notice]');
let openItem=null,openNeedsRecord=false,focusShowOnClose=false;
// Changes when a row opens or closes; never on a remount, a snapshot or a link to the open row.
let openSession=0;
let shownRevision,statusError='',savedDisplayFailed=false;
let itemRequest=null;
const measured=new Map(),mounted=new Map(),windowGaps=[];
const resize=typeof ResizeObserver==='function' ? new ResizeObserver(()=>scheduleWindow()) : null;
function preserveRecordRowTop(update){
 const before=visibleRecordRow(openItem),key=openItem?.dataset.key;
 const top=before?.getBoundingClientRect().top;
 const result=update();
 const after=visibleRecordRow(openItem);
 if(top!==undefined && after && key===openItem.dataset.key)
  trackRecordRowDuringMotion(after,top,rowMotion);
 return result;
}
/** @typedef {{key: string, index: string, offset: number}} LogPosition */
/** @type {LogPosition | null} */
let heldLog=null;
const openLog=()=>recordDetails(openItem)?.querySelector('.list-log-scroll');
// Moving or replacing the open row resets its log scroll, so hold the first visible entry for this task.
function holdLog(){
 const log=openLog();
 if(heldLog || !log?.clientHeight || !log.scrollTop) return;
 const top=log.getBoundingClientRect().top;
 const entry=[...log.querySelector('.list-log').children].find(entry=>entry.getBoundingClientRect().bottom>top);
 if(!entry) return;
 heldLog={key:openItem.dataset.key,index:entry.dataset.logIndex,offset:entry.getBoundingClientRect().top-top};
 queueMicrotask(()=>{heldLog=null;});
}
function restoreLog(){
 const log=openLog();
 if(!heldLog || openItem?.dataset.key!==heldLog.key) return;
 const entry=log?.querySelector('[data-log-index="'+CSS.escape(heldLog.index)+'"]');
 if(entry) log.scrollTop+=entry.getBoundingClientRect().top-log.getBoundingClientRect().top-heldLog.offset;
}
function scheduleWindow(){
 if(!options.mount || pendingFrame) return;
 pendingFrame=requestAnimationFrame(()=>{
  pendingFrame=0;
  const card=openItem?.getBoundingClientRect();
  if(!rowMotion.size && !revealKey && card?.bottom>0 && card.top<innerHeight)
   preserveRecordRowTop(drawWindow);
  else drawWindow();
 });
}
function spacer(node,height){
 node.innerHTML=height>0 ? '<tr aria-hidden="true"><td colspan="'+columns.length+'" style="height:'+height+'px;padding:0;border:0"></td></tr>' : '';
}
function locate(prefix,y){
 let low=0,high=prefix.length-1;
 while(low<high){const middle=(low+high)>>1;if(prefix[middle+1]<y) low=middle+1;else high=middle;}
 return low;
}
function mergeRanges(ranges){
 const merged=[];
 for(const [start,end] of ranges.sort((a,b)=>a[0]-b[0])){
  const last=merged.at(-1);
  if(last && start<=last[1]) last[1]=Math.max(last[1],end);
  else merged.push([start,end]);
 }
 return merged;
}
function drawWindow(){
 if(!options.mount) return;
 holdLog();
 const table=content.querySelector('table'),before=table.querySelector('[data-window-before]'),after=table.querySelector('[data-window-after]');
 for(const [key,node] of mounted){const height=node.getBoundingClientRect().height;if(height>0) measured.set(key,height);}
 const motions=motionsByKey();
 const entries=windowEntries.filter(({data,visible})=>visible || motions.has(data.key));
 const prefix=[0],motionRanges=[],estimate=innerWidth<=960 ? 230 : 84;
 for(const [at,{data}] of entries.entries()){
  const motion=motions.get(data.key);
  if(motion) motionRanges.push([at,at+1]);
  const height=motion ? motion.clip.getBoundingClientRect().height : measured.get(data.key) || estimate;
  prefix.push(prefix.at(-1)+height);
 }
 const origin=scrollY+table.getBoundingClientRect().top+table.tHead.getBoundingClientRect().height;
 const reveal=revealKey;
 revealKey='';
 const revealAt=reveal ? entries.findIndex(({data,visible})=>visible && data.key===reveal) : -1;
 const inset=revealAt>=0 && typeof stuckPageActionsBottom==='function' ? stuckPageActionsBottom() : 0;
 const outsideView=top=>top<inset || top>innerHeight-120;
 let windowTop=scrollY;
 if(revealAt>=0 && !mounted.has(reveal)){
  const y=origin+prefix[revealAt];
  if(outsideView(y-scrollY)) windowTop=Math.max(0,y-inset-16);
 }
 const start=Math.max(0,locate(prefix,windowTop-origin)-8);
 const end=Math.min(entries.length,start+90,Math.max(start+18,locate(prefix,windowTop-origin+innerHeight)+9));
 const focusedRow=document.activeElement?.closest('[data-list-item]');
 const focused=focusedRow?.dataset.key || options.focusKey?.();
 const ranges=[[start,end],...motionRanges];
 if(revealAt>=0) ranges.push([Math.max(0,revealAt-1),Math.min(entries.length,revealAt+2)]);
 for(const key of new Set([focused,opened.values().next().value,openItem?.dataset.key])){
  if(!key || options.pinOpenRow?.(key)===false) continue;
  const at=entries.findIndex(({data})=>data.key===key);
  if(at>=0) ranges.push([Math.max(0,at-1),Math.min(entries.length,at+2)]);
 }
 const windows=mergeRanges(ranges);
 const keys=new Set(windows.flatMap(([from,to])=>entries.slice(from,to)
  .filter(entry=>entry.visible).map(({data})=>data.key)));
 for(const [key,node] of mounted){if(!keys.has(key)){options.unmount?.(node);resize?.unobserve(node);node.hidden=true;node.remove();mounted.delete(key);}}
 for(const gap of windowGaps.splice(windows.length-1)) gap.remove();
 spacer(before,prefix[windows[0][0]]);
 spacer(after,prefix.at(-1)-prefix[windows.at(-1)[1]]);
 const nodes=[];
 for(const [position,[from,to]] of windows.entries()){
  if(position){
   const gap=windowGaps[position-1] ??= document.createElement('tbody');
   gap.setAttribute('data-window-gap','');
   spacer(gap,prefix[from]-prefix[windows[position-1][1]]);
   nodes.push(gap);
  }
  for(const {data,visible} of entries.slice(from,to)){
   if(visible) nodes.push(mount(data));
   const motion=motions.get(data.key);
   if(motion) nodes.push(motion.spacer);
  }
 }
 // Move the rows around the focused row to keep its focus.
 const anchor=nodes.indexOf(focusedRow);
 let next=focusedRow;
 for(let i=anchor-1;i>=0;i--){
  const node=nodes[i];
  if(next.previousElementSibling!==node) next.before(node);
  next=node;
 }
 let cursor=anchor<0 ? before : focusedRow;
 for(let i=anchor+1;i<nodes.length;i++){
  const node=nodes[i];
  if(cursor.nextElementSibling!==node) cursor.after(node);
  cursor=node;
 }
 syncOpenItem();
 if(revealAt>=0){
  const row=mounted.get(reveal)?.querySelector('.list-row');
  if(row){
   const top=row.getBoundingClientRect().top;
   if(outsideView(top)) scrollTo({top:scrollY+top-inset-16,behavior:'instant'});
  }
 }
 restoreLog();
 showHiddenActionResults([...mounted.values()]);
 options.mounted?.();
}
function updateItemDetail(item,update){
 const detail=recordDetails(item);
 if(detail!==viewDetail){update(detail);return;}
 const field=document.activeElement;
 const selector=detail.contains(field) && (field.id ? '#'+CSS.escape(field.id) :
  field.localName+['name','value','href'].map(name=>field.hasAttribute(name) ?
   '['+name+'="'+CSS.escape(field.getAttribute(name))+'"]' : '').join(''));
 const at=selector ? [...detail.querySelectorAll(selector)].indexOf(field) : -1;
 const start=field?.selectionStart,end=field?.selectionEnd,direction=field?.selectionDirection;
 holdLog();
 update(detail);
 restoreLog();
 if(at>=0 && !field.isConnected){
  const next=detail.querySelectorAll(selector)[at];
  (next ?? detailFocus()).focus({preventScroll:true});
  if(typeof start==='number' && next?.setSelectionRange) next.setSelectionRange(start,end,direction);
 }
}
function drawDetailStatus(){
 if(!inlineView) return;
 const error=savedDisplayFailed ? 'Saved; the item could not refresh.' : statusError;
 detailError.hidden=!error || !detailMissing.hidden;
 detailError.textContent=error;
}
function itemError(message){statusError=message;drawDetailStatus();}
function queueItemUpdate(item,update){
 if(item!==openItem){update?.(item,recordDetails(item));return;}
 if(update){
  options.rememberItem?.(item);
  updateItemDetail(item,detail=>update(item,detail));
 }
 drawDetailStatus();
}
function installItemRevision(revision,render){
 return (row,detail)=>{
  render(row,detail);
  shownRevision=revision;
  savedDisplayFailed=false;
 };
}
function presentItemRevision(item,revision,render,{initial=false,force=false}={}){
 const install=installItemRevision(revision,render);
 if(initial){
  updateItemDetail(item,detail=>install(item,detail));
  drawDetailStatus();
  return;
 }
 queueItemUpdate(item,force || revision!==shownRevision ? install : null);
}
function installOwnItemResult(request,revision,render){
 if(!currentItemRequest(request)) return;
 const item=openItem;
 options.rememberItem?.(item);
 const install=installItemRevision(revision,render);
 updateItemDetail(item,detail=>install(item,detail));
 drawDetailStatus();
}
function startItemRequest(key){
 const request={key,delivery:'',until:0};
 itemRequest=request;
 savedDisplayFailed=false;
 drawDetailStatus();
 return request;
}
function startDeliveryWindow(request,result){
 if(!result.delivery_pending || !result.delivery || !currentItemRequest(request)) return;
 request.delivery=result.delivery;
 request.until=performance.now()+Number(content.dataset.deliveryWindowSeconds)*1000;
}
function deliveryRequest(){
 return performance.now()<itemRequest?.until && currentItemRequest(itemRequest) ? itemRequest : null;
}
function finishDeliveryWindow(request){
 if(currentItemRequest(request)) request.until=0;
}
function currentItemRequest(request){
 return Boolean(request) && itemRequest===request && openItem?.dataset.key===request.key && opened.has(request.key);
}
function savedItemError(request){
 if(!currentItemRequest(request)) return;
 savedDisplayFailed=true;
 drawDetailStatus();
}
function showDraftCopy(words,editable){
 if(!words || editable || viewDetail.querySelector('[data-review-draft]')) return;
 const label=document.createElement('label'),field=document.createElement('textarea');
 label.textContent='Your unsaved words';
 field.value=words;field.readOnly=true;field.dataset.reviewDraft='';
 label.append(field);viewDetail.append(label);
}
function returnItemDetail(){
 if(!openItem) return;
 options.rememberItem?.(openItem);
 itemRequest=null;
 savedDisplayFailed=false;
 statusError='';
 delete openItem.openDetails;
 recordDetails(openItem).replaceChildren(...viewDetail.childNodes);
 inlineView.remove();
 clearRecordSpace();
 openItem=null;
 focusShowOnClose=false;
}
function syncOpenItem(){
 if(!inlineView) return;
 const key=opened.values().next().value;
 if(!key){
  if(!openItem) return;
  const previous=openItem.dataset.key;
  const focused=document.activeElement;
  // A park choice hides its menu, so the page can drop focus before the row closes.
  const keepsFocus=inlineView.contains(focused) || (focusShowOnClose && focused===document.body);
  const restoreFocus=keepsFocus && options.ownsFocus?.()!==false;
  const showChip=focusShowOnClose && chipRow('show')?.querySelector('[aria-pressed="true"]');
  returnItemDetail();
  if(restoreFocus){
   const row=options.mount ? mounted.get(previous) : itemWithKey(previous);
   const target=row && !row.hidden ? row.querySelector('.list-toggle') : showChip || words();
   target.focus({preventScroll:true});
  }
  return;
 }
 const sameItem=openItem?.dataset.key===key;
 let item=options.mount ? mounted.get(key) ?? (sameItem && !openNeedsRecord ? openItem : null) : itemWithKey(key);
 if(options.mount && !item && options.hasKey(key)){
  item=options.mount(itemWithKey(key).dataset);
  item.hidden=true;setExpanded(item,true);
 }
 if(!item || (options.hasKey && !options.hasKey(key))){
  if(sameItem){
   detailMissing.hidden=false;
   drawDetailStatus();
   viewDetail.querySelectorAll('form button').forEach(control=>control.disabled=true);
  }
  return;
 }
 openNeedsRecord=false;
 if(item===openItem) return;
 const wasMissing=!detailMissing.hidden;
 const keepBody=sameItem && !viewDetail.querySelector('[data-record-loading]');
 const incoming=recordDetails(item);
 const children=[...incoming.childNodes];
 if(keepBody){
  options.retainDetailState?.(openItem,item);
  delete openItem.openDetails;
  }else{
  if(openItem) returnItemDetail();
  viewDetail.replaceChildren(...children);
  shownRevision=options.itemRevision?.(item);
 }
 incoming.replaceChildren(inlineView);
 openItem=item;item.openDetails=viewDetail;
 detailMissing.hidden=true;
 if(options.itemRevision && keepBody) presentNewDetail(item,children,wasMissing);
 else if(!keepBody) options.detailInstalled?.(item);
 drawDetailStatus();
}
function presentNewDetail(item,children,wasMissing){
 presentItemRevision(item,options.itemRevision(item),(row,detail)=>{
  const slot=options.retainedDetailSlot;
  const retained=slot && detail.querySelector(slot);
  detail.replaceChildren(...children);
  if(retained) detail.querySelector(slot)?.replaceWith(retained.cloneNode(true));
  options.detailInstalled?.(row);
 },{force:wasMissing});
}
// The open row keeps its node through a new snapshot and takes the new data.
function keepOpenRow(){
 const item=openItem,key=item?.dataset.key;
 const next=inlineView && options.itemRevision && item && !item.isConnected && opened.has(key) && itemWithKey(key);
 if(!next) return;
 for(const {name} of [...item.attributes]) if(!next.hasAttribute(name)) item.removeAttribute(name);
 for(const {name,value} of next.attributes) item.setAttribute(name,value);
 item.querySelector('.list-row').replaceWith(next.querySelector('.list-row'));
 const children=[...recordDetails(next).childNodes],wasMissing=!detailMissing.hidden;
 next.replaceWith(item);
 detailMissing.hidden=true;
 presentNewDetail(item,children,wasMissing);
}
function mount(data){
 const key=data.key;
 let node=mounted.get(key);
 if(!node){
  node=!openNeedsRecord && options.pinOpenRow?.(key)!==false && openItem?.dataset.key===key
   ? openItem : options.mount(data);
  mounted.set(key,node);resize?.observe(node);
 }
 node.hidden=false;
 const expanded=!node.hasAttribute('data-validating-change') && opened.has(key);
 setExpanded(node,expanded);
 return node;
}
if(options.mount){
 addEventListener('scroll',scheduleWindow,{passive:true});
 addEventListener('resize',()=>{measured.clear();scheduleWindow();});
 content.addEventListener('focusin',event=>{
  const row=event.target.closest('[data-list-item]');
  if(row && !row.contains(event.relatedTarget)) drawWindow();
 });
 content.addEventListener('focusout',scheduleWindow);
}


const sortValues=data=>options.sortValues?.(data) ?? JSON.parse(data.sortValues);
const words=()=>content.querySelector('[data-filter-words]') ?? content.querySelector('#list-search');
const sharedWords=()=>words().hasAttribute?.('data-filter-words');
const currentWords=()=>sharedWords() ? filterControl.getWords(words()) : words().value;
if(sharedWords()) addEventListener('pageshow',()=>setTimeout(()=>filterControl.setWords(words(),currentWords()),0));
const chipRow=name=>filterControl.row(content,name);
const isStaffChipFilter=name=>options.staffChipFilters?.includes(name);
const filterChipValue=(name,value)=>value ? (isStaffChipFilter(name) ? staffChipPrefix+value : value) : 'all';
function filterSelection(name){
 const row=chipRow(name),all=name==='show' ? 'all' : '';
 if(row){
  const filterValue=value=>value==='all' ? all : isStaffChipFilter(name) ? staffChipId(value) : value;
  return {
   set(value,fallback='all'){
    const stored=isStaffChipFilter(name) ? encodeIdentity(value) : value;
    const allowHidden=name!=='show' || Boolean(options.selectShow);
    return filterValue(filterControl.select(row,filterChipValue(name,stored),{allowHidden,fallback}));
   },
   bind(change){filterControl.bind(row,value=>change(filterValue(value)));},
   setCounts(counts){filterControl.setCounts(row,counts);}
  };
 }
 const control=content.querySelector('#list-'+name);
 return {
  set(value){control.checked=value===control.value;return control.checked ? control.value : '';},
  bind(change){control.addEventListener('change',()=>change(control.checked ? control.value : ''));}
 };
}
function placeParams(){
 const params=recordPlaceParams(options);
 options.place?.(params);
 return params;
}
const resolveItem=value=>options.resolveItem?.(value) ?? value;
const itemLinkKey=()=>recordItemLink(placeParams(),resolveItem);
function place(){return readPlace(placeParams());}
function itemWithKey(key){
 if(options.hasKey && !options.hasKey(key)) return;
 return options.entries ? options.entries().map(dataset=>({dataset})).find(item=>item.dataset.key===key) :
  [...content.querySelectorAll('[data-list-item]')].find(item=>item.dataset.key===key);
}
function readPlace(params,followingItemLink=false){
 placeHash=location.hash;
 keys=new Set(params.getAll('key'));
 owner=ownerQueryKeys.map(key=>params.get(key)).find(Boolean) || '';show=params.get('show') || defaultShow;query=params.get('q') || '';
 sort=params.get('sort') || '';direction=params.get('dir')==='desc' ? 'desc' : 'asc';
 type=params.get('type') || '';
 sourceOwner=params.get('source-owner') || '';
 for(const name of filterNames) filters[name]=params.get(name) || '';
 if(!options.shows.includes(show)) show=defaultShow;
 show=options.selectShow?.(show,type) ?? show;
 if(!columns.includes(sort)) sort='';
 const item=params.get('item');
 const saved=followingItemLink ? null : history.state?.recordTable;
 if(saved){
  const key=saved.opened.includes(item) ? item : saved.opened[0];
  if(key) openCard(key);else opened.clear();
 }else if(!followingItemLink && item){
  if(keys.size) keys.add(item);
  if(!['owner','showrunner','show','q','sort','dir','type','source-owner',...filterNames].some(key=>params.has(key))){
   owner='';show='all';
   query='';type='';for(const name of filterNames) filters[name]='';}
  if(itemWithKey(item)){openCard(item);revealKey=item;}
  else if(inlineView && !options.keepItemOnMissingLink) opened.clear();
 }else if(inlineView && !followingItemLink) opened.clear();
 linkedItem=options.missingItem ? saved?.linkedItem ?? (
  !options.persistLinkedTarget || params.get('linked')==='1' ? item ?? '' : '') : '';
 pendingItem=Boolean(options.resolveItem && linkedItem && saved?.pendingItem);
 const reveal=options.revealLinked && params.get('reveal')==='1';
 revealedLinkedItem=reveal || saved?.revealedLinkedItem===linkedItem ? linkedItem : '';
 owner=options.resolveOwner?.(owner) ?? owner;
 if(sharedWords()) filterControl.setWords(words(),query);
 return saved?.top;
}
function followItemLink({inheritFilters=false,unappliedOnly=false,beforeJump}={}){
 if(unappliedOnly && location.hash===placeHash) return false;
 const params=placeParams();
 let unresolved=false;
 const key=recordItemLink(params,value=>{
  const resolved=resolveItem(value);
  unresolved=Boolean(options.resolveItem && !options.hasKey?.(resolved));
  return resolved;
 });
 if(!key) return false;
 const motion=rowMotion.size ? motionsByKey().get(key) : undefined;
 if(motion){
  const hash=location.hash;
  void motion.finished.then(()=>{
   if(location.hash===hash) followItemLink({inheritFilters,unappliedOnly:true,beforeJump});
  });
  return true;
 }
 if(beforeJump?.(key)===false) return true;
 if(inheritFilters){
  for(const [name,value] of Object.entries({owner,show,q:query,sort,dir:direction,type,'source-owner':sourceOwner,...filters}))
   if(!params.has(name)) params.set(name,value);
  if(!params.has('key')) keys.forEach(key=>params.append('key',key));
 }
 readPlace(params,true);
 jump(key,{pushPlace:false,focusTarget:Boolean(params.get('jump')),keepMatchingFilters:true,unresolved});
 return true;
}
function retryItemLink(){
 if(!pendingItem || !linkedItem || location.hash!==placeHash) return false;
 const key=resolveItem(linkedItem);
 if(!options.hasKey?.(key)) return false;
 jump(key,{pushPlace:false,focusTarget:false,keepMatchingFilters:true});
 return true;
}
function compare(a,b){
 if(Array.isArray(a)) return a[0]-b[0] || compare(a[1],b[1]);
 return typeof a==='number' ? a-b : collator.compare(a,b);
}
function stopMotion(item){
 const motion=rowMotion.get(item);
 if(!motion) return;
 rowMotion.delete(item);
 motion.animation.cancel();
 motion.spacer.remove();
 item.classList.remove('record-motion-source');
}
function setExpanded(item,expanded){
 const detail=item.querySelector('[data-detail]'),toggle=item.querySelector('.list-toggle');
 const changed=recordIsOpen(item)!==expanded;
 detail.hidden=!expanded;
 toggle.setAttribute('aria-expanded',String(expanded));
 item.querySelector('.list-row').classList.toggle('open',expanded);
 return changed;
}
function animateRow(table,item,showRow,previousItem=item){
 const previous=rowMotion.get(previousItem);
 const heightBefore=previous?.clip.getBoundingClientRect().height;
 const opacityBefore=previous ? Number(getComputedStyle(previous.clip).opacity) : Number(!showRow);
 stopMotion(previousItem);
 const height=item.getBoundingClientRect().height+(parseFloat(getComputedStyle(item).marginBottom) || 0);
 if(!height) return;
 // A copy can fade and shrink while the record leaves the view at once.
 const spacer=document.createElement('tbody');
 spacer.dataset.recordMotion='';spacer.dataset.recordMotionKey=item.dataset.key;spacer.inert=true;spacer.setAttribute('aria-hidden','true');
 const cell=spacer.insertRow().insertCell();
 const columns=table.querySelector(':scope > colgroup');
 cell.colSpan=columns.children.length;cell.className='record-motion-cell';
 const clip=document.createElement('div');clip.className='record-motion-clip';
 const copyTable=document.createElement('table'),copy=item.cloneNode(true);
 for(const name of ['data-list-item','data-key','data-number']) copy.removeAttribute(name);
 copy.querySelectorAll('[id]').forEach(element=>element.removeAttribute('id'));
 copyTable.append(columns.cloneNode(true),copy);clip.append(copyTable);cell.append(clip);
 item.after(spacer);
 if(showRow) item.classList.add('record-motion-source');
 const duration=Number(showRow ? content.dataset.cardFadeInMs : content.dataset.cardFadeOutMs);
 const animation=clip.animate([
  {height:(heightBefore ?? (showRow ? 0 : height))+'px',opacity:opacityBefore},
  {height:(showRow ? height : 0)+'px',opacity:Number(showRow)},
 ],{duration,easing:'ease-in-out',fill:'both'});
 const finished=animation.finished.then(()=>{
  if(rowMotion.get(item)?.animation!==animation) return;
  if(trackedRecordRow) recordScrolled();
  const tracked=trackedRecordRow;
  stopMotion(item);
  if(table.closest('[data-linked-item]')) apply(false,{animate:true});
  else scheduleWindow();
  if(tracked && tracked.row===visibleRecordRow(openItem))
   trackRecordRowDuringMotion(tracked.row,tracked.top,rowMotion);
 },()=>{});
 rowMotion.set(item,{spacer,clip,copy,animation,finished,leaving:!showRow,sortValues:showRow ? sortValues(item.dataset) :
  previous?.sortValues ?? sortValues(item.dataset)});
}
function showHiddenActionResults(items){
 const status=content.querySelector('[data-hidden-action-status]');
 if(!status) return;
 const hidden=key=>{
  const item=items.find(item=>item.dataset.key===key);
  return !item || item.hidden || openItem!==item ||
   !recordDetails(item)?.querySelector('[data-send-status]');
 };
 const messages=[];
 for(const [key,{hiddenMessage}] of actionNotices)
  if(hiddenMessage && hidden(key)) messages.push(hiddenMessage);
 if(latestActionFailure && hidden(latestActionFailure.key)) messages.push(latestActionFailure.message);
 showActionFailure(status,messages.join(' '));
}
/** @param {{kind:'success' | 'failure' | 'undone', message:string, hiddenMessage?:string}} result */
function showActionResult(item,submission,label,{kind,message,hiddenMessage}){
 const key=item.dataset.key;
 actionNotices.set(key,{message,hiddenMessage});
 if(kind==='failure' && submission===latestSubmission) latestActionFailure={key,message:label+': '+message};
 const status=recordDetails(item)?.querySelector('[data-send-status]');
 if(status) showActionFailure(status,message);
 apply(false,{animate:true});
 const restored=mounted.get(key);
 // A refused or undone send brings its departing row back.
 if(kind!=='success' && restored && rowMotion.has(item)){
  animateRow(content.querySelector('table'),restored,true,item);
 }
}
function isSelected(item){return !keys.size || keys.has(item.dataset.key);}
function canRevealLinked(item,selection){
 return item.dataset.key===revealedLinkedItem || options.revealLinked?.(item.dataset,selection);
}
function matches(item,selection,counts){
 if(selection.keys.size && !selection.keys.has(item.dataset.key)) return false;
 const data=item.dataset;
 if(options.visible) return options.visible(data,selection);
 const chip=options.eligibleChip(data,selection.query,selection);
 if(!chip) return false;
 let excluded=0;
 const fieldValue=field=>field==='chip' ? chip : data[field];
 for(const [name,field,bit] of options.filterFacets){
  const chosen=selection[name] ?? selection.filters[name];
  if(!chosen || (name==='show' && chosen==='all')) continue;
  // A staff field holds an encoded identity; a selection holds the decoded identity.
  const raw=fieldValue(field),actual=isStaffChipFilter(name) ? decodeURIComponent(raw) : raw;
  if(chosen!==actual) excluded|=bit;
 }
 if(counts) for(const [name,field,,countMask] of options.filterFacets){
  if(excluded & countMask) continue;
  const values=counts[name];
  values.all=(values.all ?? 0)+1;
  const value=filterChipValue(name,fieldValue(field));
  if(value!=='all') values[value]=(values[value] ?? 0)+1;
 }
 return !excluded;
}
const normalizeWords=value=>options.normalizeWords?.(value) ?? value.trim().toLowerCase();
function currentSelection(overrides={}){
 return {owner,show,query:normalizeWords(query),type,sourceOwner,keys,filters:{...filters},...overrides};
}
function apply(write=true,layout={}){
 holdLog();
 applyRows(write,layout);
}
function applyRows(write,{animate=false,dismissMissingItem=false,normalizeOwner=false,pushPlace=false,top}={}){
 const previousReveal=revealedLinkedItem;
 const motion=animate && !matchMedia('(prefers-reduced-motion: reduce)').matches;
 if(!motion) for(const item of rowMotion.keys()) stopMotion(item);
 const rows=options.entries ? options.entries().map(dataset=>({dataset})) : [...content.querySelectorAll('[data-list-item]')];
 const hasKey=options.hasKey ?? (key=>rows.some(item=>item.dataset.key===key));
 const missingLinkedItem=linkedItem!=='' && !hasKey(linkedItem);
 if(missingLinkedItem){
  keys.delete(linkedItem);
  if(openItem?.dataset.key!==linkedItem) opened.delete(linkedItem);
  if(dismissMissingItem) linkedItem='';
 }
 if(dismissMissingItem && rows.some(item=>item.dataset.key===linkedItem && canRevealLinked(item,currentSelection()))){
  opened.delete(linkedItem);
  linkedItem='';
 }
 if(revealedLinkedItem!==linkedItem) revealedLinkedItem='';
 const previousOwner=owner,previousShow=show,previousFilters={...filters};
 owner=options.resolveOwner?.(owner) ?? owner;
 // Use one clock read for the rows, the owner selection, and the counts.
 const now=Date.now();
 if(normalizeOwner) owner=options.selectOwner?.(rows,currentSelection({now})) ?? owner;
 owner=filterSelection('owner').set(owner);
 if(previousOwner && !owner) sourceOwner='';
 if(chipRow('type')) filterSelection('type').set(type,null);
 show=options.selectShow?.(show,type) ?? show;
 show=filterSelection('show').set(show);
 if(!sharedWords()) words().value=query;
 for(const name of filterNames)
  filters[name]=filterSelection(name).set(filters[name]);
 const active=currentSelection({now});
 const counts=options.filterFacets && Object.fromEntries(options.filterFacets.map(([name])=>[name,Object.create(null)]));
 const column=columns.indexOf(sort);
 const windowMotions=options.mount && rowMotion.size ? motionsByKey() : null;
 const currentKeys=windowMotions && new Set(rows.map(item=>item.dataset.key));
 const departingRows=currentKeys ? [...rowMotion.keys()].filter(item=>!currentKeys.has(item.dataset.key)) : [];
 const sortValue=item=>{
  const transition=windowMotions ? windowMotions.get(item.dataset.key) : rowMotion.get(item);
  return (transition?.sortValues ?? sortValues(item.dataset))[column];
 };
 const compareOrder=(a,b)=>compare(Number(a.dataset.order),Number(b.dataset.order));
 const items=[...rows,...departingRows].sort((a,b)=>column<0 ? compareOrder(a,b) :
  (compare(sortValue(a),sortValue(b)) || compareOrder(a,b))*(direction==='desc' ? -1 : 1));
 const table=content.querySelector('table');let count=0,closedHiddenItem=false;
 if(options.mount){
  const visibleKeys=new Set(rows.filter(item=>matches(item,active,counts)).map(item=>item.dataset.key));
  count=visibleKeys.size;
  const openKey=opened.values().next().value;
  if(inlineView && hasKey(openKey) && !visibleKeys.has(openKey)){
   if(dismissMissingItem) closeCard(openKey,{write:false});
   else if(options.pinOpenRow?.(openKey)!==false) visibleKeys.add(openKey);
  }
  for(const [key,node] of mounted){
   if(visibleKeys.has(key)) continue;
   if(motion) animateRow(table,node,false);
   options.unmount?.(node);resize?.unobserve(node);node.hidden=true;node.remove();mounted.delete(key);
  }
  const movingKeys=new Set([...rowMotion.keys()].map(item=>item.dataset.key));
  windowEntries=items.flatMap(({dataset:data})=>{
   const visible=visibleKeys.has(data.key);
   return visible || movingKeys.has(data.key) ? [{data,visible}] : [];
  });
  drawWindow();
 }else{
  const linkedSection=content.querySelector('[data-linked-item]');
  const linkedTable=linkedSection?.querySelector('table');
  const linked=linkedTable && opened.has(linkedItem) && items.find(item=>
   item.dataset.key===linkedItem && canRevealLinked(item,active) && !matches(item,active));
  if(linked){
   // Keep the link when a refresh changes the item's filter values.
   revealedLinkedItem=linkedItem;
   if(linked.parentElement!==linkedTable){
    stopMotion(linked);
    linkedTable.append(linked);
   }
  }
  const leavingLinked=motion && linkedTable && items.find(item=>item!==linked && item.parentElement===linkedTable &&
   (!item.hidden || rowMotion.has(item)) && !matches(item,active));
  const results=linked || leavingLinked ? items.filter(item=>item!==linked && item!==leavingLinked) : items;
  const tableRows=linkedTable ? rows.filter(item=>item.parentElement===table) : rows;
  if(results.some((item,index)=>item!==tableRows[index])) for(const item of results){
   if(linkedTable && item.parentElement===linkedTable) stopMotion(item);
   table.append(item);
   const spacer=rowMotion.get(item)?.spacer;
   if(spacer) table.append(spacer);
  }
  if(linkedSection) linkedSection.hidden=!linked && !leavingLinked;
  items.forEach(item=>{
   const matched=matches(item,active,counts);
   const showRow=matched || item===linked,wasShown=!item.hidden;
   if(!showRow && opened.has(item.dataset.key) && options.closeHiddenItem?.(dismissMissingItem)){
    closeCard(item.dataset.key,{write:false});
    closedHiddenItem=true;
   }
   if(motion && wasShown && !showRow) animateRow(item.parentElement,item,false);
   item.hidden=!showRow;if(matched) count++;
   const expanded=opened.has(item.dataset.key);
   const expansionChanged=setExpanded(item,expanded);
   const transition=rowMotion.get(item);
   if(motion && showRow && (!wasShown || (expansionChanged && transition))) animateRow(item.parentElement,item,true);
   else if(expansionChanged && transition) setExpanded(transition.copy,expanded);
  });
 }
 content.querySelectorAll('[data-sort]').forEach((button,index)=>{
  const active=column===index;
  button.parentElement.setAttribute('aria-sort',active ? direction==='desc' ? 'descending' : 'ascending' : 'none');
  button.querySelector('[data-sort-mark]').textContent=active ? direction==='desc' ? ' ↓' : ' ↑' : '';
 });
 const empty=content.querySelector('[data-empty]');
 const missing=[...keys].filter(key=>!hasKey(key));
 let notice=content.querySelector('[data-missing-keys]');
 if(missing.length || notice || options.missingItem){
  if(!notice){
   notice=document.createElement('p');notice.dataset.missingKeys='';notice.setAttribute('role','status');
   empty.after(notice);
  }
  showActionFailure(notice,missing.map(key=>key+' is no longer on the '+table.getAttribute('aria-label')+' page.').join('\n'));
 }
 const noResults=count===0 && missing.length===0;
 if(!options.mount) syncOpenItem();
 const missingItem=content.querySelector('[data-missing-item]');
 if(missingItem){
  const message=linkedItem && missingLinkedItem && openItem?.dataset.key!==linkedItem ? options.missingItem(linkedItem) : '';
  const inside=openItem && message;
  showActionFailure(missingItem,inside ? '' : message);
  if(detailLinkNotice) showActionFailure(detailLinkNotice,inside || '');
 }
 const group=content.querySelector('[data-clear-linked-reviews]');
 if(group) group.hidden=!keys.size;
 const selection=items.filter(isSelected);
 if(counts) for(const [name,values] of Object.entries(counts)) filterSelection(name).setCounts?.(values);
 const countLine=content.querySelector('[data-list-count]');
 if(countLine) countLine.textContent=options.counts(selection,count,{show,keys,counts});
 if(!options.mount) showHiddenActionResults(items);
 restoreLog();
 options.applied?.(selection,active,{rows,matches,count});
 const reset=options.emptyFilters && noResults ? resetSelection(active.now) : null;
 const canResetToResults=reset!==null && rows.some(item=>matches(item,reset));
 const filteredEmpty=options.emptyFilters && updateEmptyResult(table,{
  empty:canResetToResults,search:query,
  labels:canResetToResults ? options.emptyFilters(content,active) : [],
  clearSearch,resetFilters
 });
 empty.hidden=!noResults || Boolean(filteredEmpty);
 if(write || location.hash===placeHash &&
  (closedHiddenItem || revealedLinkedItem!==previousReveal || owner!==previousOwner || show!==previousShow ||
   filterNames.some(name=>filters[name]!==previousFilters[name])))
  writePlace(pushPlace,top);
 if(inlineView && (rowMotion.size || trackedRecordRow)){
  const row=visibleRecordRow(openItem);
  if(row && rowMotion.size){
   if(trackedRecordRow?.row!==row) trackRecordRowDuringMotion(row,row.getBoundingClientRect().top,rowMotion);
  }else if(trackedRecordRow) stopTrackingRecordRow();
 }
}
function placeState(top){
 return {recordTable:{opened:[...opened],linkedItem,revealedLinkedItem,top,...(linkedItem && pendingItem ? {pendingItem:true} : {})}};
}
function writePlace(push=false,top){
 const place=new URLSearchParams({owner,show,q:query,sort,dir:direction,...(type ? {type} : {}),...(sourceOwner ? {'source-owner':sourceOwner} : {}),
  ...Object.fromEntries(filterNames.filter(name=>filters[name]).map(name=>[name,filters[name]]))});
 const item=linkedItem || [...opened][0];
 if(item) place.set('item',item);
 if(options.persistLinkedTarget && linkedItem) place.set('linked','1');
 if(linkedItem && revealedLinkedItem===linkedItem) place.set('reveal','1');
 keys.forEach(key=>place.append('key',key));
 const hash='#'+place,state=placeState(top);
 if(push && hash!==location.hash) history.pushState(state,'',location.pathname+hash);
 else history.replaceState(state,'',location.pathname+hash);
 placeHash=location.hash;
 if(parent!==window) parent.postMessage('console-place',location.origin);
}
function restorePlace(event){
 // A changed fragment sends hashchange after popstate.
 if(event?.type==='popstate' && location.hash!==placeHash) return;
 const top=place();
 if(retryItemLink()) return;
 apply(false,{top,normalizeOwner:!history.state?.recordTable});
 if(options.keepItemOnMissingLink && !history.state?.recordTable && linkedItem && !itemWithKey(linkedItem))
  history.replaceState(placeState(scrollY),'');
 if(Number.isFinite(top)) scrollTo({top,behavior:'instant'});
}
function saveDeparturePlace(){
 if(sharedWords() && query!==currentWords()){
  query=currentWords();
  writePlace(false,scrollY);
 }else history.replaceState(placeState(scrollY),'');
}
function jump(key,{pushPlace=true,focusTarget=true,keepMatchingFilters=false,preserveFilters=false,unresolved=false,
 keepItemOnMissing=options.keepItemOnMissingLink}={}){
 if(pushPlace) saveDeparturePlace();
 const target=itemWithKey(key);
 linkedItem=options.missingItem ? key : '';
 pendingItem=unresolved;
 if(target){
  if(keys.size) keys.add(key);
  openCard(key);
  const selection=currentSelection();
  if(!preserveFilters && !canRevealLinked(target,selection) && !matches(target,selection)){
   const unfiltered=currentSelection({owner:'',show:'all',query:'',type:'',sourceOwner:'',filters:{}});
   if(!keepMatchingFilters || owner && !matches(target,{...unfiltered,owner})) owner='';
   if(!keepMatchingFilters || sourceOwner && !matches(target,{...unfiltered,sourceOwner})) sourceOwner='';
   if(!keepMatchingFilters || show!=='all' && !matches(target,{...unfiltered,show})) show='all';
   if(query && !matches(target,{...unfiltered,query:normalizeWords(query)})) query='';
   if(type && !matches(target,{...unfiltered,type})) type='';
   for(const name of filterNames)
    if(filters[name] && !matches(target,{...unfiltered,filters:{[name]:filters[name]}})) filters[name]='';
   if(sharedWords()) filterControl.setWords(words(),query);
  }
 }
 // A link to a missing item closes the open row, so the page shows the missing item notice.
 if(!target && !keepItemOnMissing) opened.clear();
 if(options.mount && target) revealKey=key;
 apply(true,{pushPlace,normalizeOwner:!preserveFilters});
 const mountedTarget=options.mount ? mounted.get(key) : target;
 if(!mountedTarget){
  const notice=inlineView?.isConnected && !detailLinkNotice.hidden ? detailLinkNotice :
   content.querySelector('[data-missing-item]');
  notice?.scrollIntoView({block:'nearest',behavior:'instant'});
  return;
 }
 const row=mountedTarget.querySelector('.list-row');
 if(mountedTarget.hidden) return;
 if(!focusTarget) return;
 highlightedRow?.removeAttribute('data-record-highlight');
 clearTimeout(highlightTimer);
 row.style.scrollMarginTop='16px';
 row.querySelector('.list-toggle').focus({preventScroll:true});
 row.scrollIntoView({block:'start',behavior:'instant'});
 row.dataset.recordHighlight='';
 highlightedRow=row;
 highlightTimer=setTimeout(()=>row.removeAttribute('data-record-highlight'),1200);
}
function openCard(key){
 if(!opened.has(key)) openSession++;
 opened.clear();
 opened.add(key);
 if(linkedItem!==key){linkedItem='';revealedLinkedItem='';}
}
function closeCard(key,{write=true,focusShow=false}={}){
 const wasOpen=opened.delete(key);
 if(wasOpen) openSession++;
 if(openItem?.dataset.key===key) focusShowOnClose=focusShow;
 if(!wasOpen && linkedItem!==key) return;
 linkedItem='';revealedLinkedItem='';
 if(write && location.hash===placeHash) writePlace();
}
function ownerChipSelection(selection,nextOwner){
 return nextOwner===selection.owner ? selection : {...selection,owner:nextOwner,sourceOwner:''};
}
function followRecordLink(event){
 const link=event.target.closest('a[data-record-jump]');
 if(!link || event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
 const key=new URLSearchParams(link.hash.slice(1)).get('item');
 if(!key) return;
 event.preventDefault();
 jump(key,{keepItemOnMissing:true});
}
function clearSearch(){
 query='';
 if(sharedWords()) filterControl.setWords(words(),query);
 apply(true);
 words().focus({preventScroll:true});
}
function resetSelection(now){
 return {owner:'',sourceOwner:'',show:'all',type:'',query:'',keys:new Set(),
  filters:Object.fromEntries(filterNames.map(name=>[name,''])),now};
}
function resetFilters(){
 const reset=resetSelection();
 ({owner,sourceOwner,show,type,query,keys}=reset);
 Object.assign(filters,reset.filters);
 if(sharedWords()) filterControl.setWords(words(),query);
 apply(true);
 options.firstFilter(content).focus({preventScroll:true});
}
function bind(){
 if(boundRoots.has(content)) return;boundRoots.add(content);
 const changePlace=(normalizeOwner=false)=>{
   apply(true,{dismissMissingItem:true,normalizeOwner});
 };
 const changeFilter=()=>{if(sharedWords()) query=currentWords();changePlace(true);};
 content.querySelector('[data-clear-linked-reviews]')?.addEventListener('click',()=>{
  keys.clear();changeFilter();words().focus({preventScroll:true});
 });
 content.addEventListener('click',followRecordLink);
 filterSelection('owner').bind(value=>{
  const current=currentSelection(),next=ownerChipSelection(current,value);
  if(next===current) return;
  owner=next.owner;sourceOwner=next.sourceOwner;changeFilter();
 });
 filterSelection('show').bind(value=>{if(value===show) return;show=value;changeFilter();});
 if(chipRow('type')) filterSelection('type').bind(value=>{if(value===type) return;type=value;changeFilter();});
 for(const name of filterNames)
  filterSelection(name).bind(value=>{if(value===filters[name]) return;filters[name]=value;changeFilter();});
 if(sharedWords()) filterControl.bind(words(),value=>{if(value!==query){query=value;changePlace();}});
 else words().addEventListener('input',event=>{query=event.target.value;changePlace();});
 content.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{
  const selected=columns[Number(button.dataset.sort)];direction=sort===selected && direction==='asc' ? 'desc' : 'asc';
  sort=selected;changePlace();
 }));
 content.addEventListener('click',event=>{
  const item=event.target.closest('.list-row')?.closest('[data-list-item]');if(!item) return;
  if(event.target.closest('a') || (event.target.closest('button') && !event.target.closest('.list-toggle'))) return;
  const key=item.dataset.key,expanded=!opened.has(key);
  if(expanded) openCard(key);else closeCard(key,{write:false});
  apply(true,{animate:true});
 });
}
return {place,apply,bind,opened,openCard,closeCard,restorePlace,drawWindow,updateItemDetail,presentItemRevision,
 preserveRecordRowTop,holdLog,restoreLog,
 startItemRequest,currentItemRequest,startDeliveryWindow,deliveryRequest,finishDeliveryWindow,installOwnItemResult,savedItemError,itemError,showDraftCopy,get openItem(){return openItem;},get openSession(){return openSession;},followItemLink,retryItemLink,itemLinkKey,saveDeparturePlace,get linkedItem(){return linkedItem;},
 canSubmit(form,key){
  return openItem && !openItem.hidden && location.hash===placeHash && openItem.dataset.key===key &&
   opened.has(key) && detailMissing.hidden && viewDetail.contains(form);
 },
 revealMatchingItem(key){
  const target=itemWithKey(key);
  const selection=currentSelection({query:normalizeWords(currentWords())});
  if(!target || !matches(target,selection)) return false;
  jump(key,{preserveFilters:true});
  return true;
 },
 releaseLink(key){
  if(linkedItem!==key) return;
  const wasRevealed=revealedLinkedItem===key;
  linkedItem='';revealedLinkedItem='';
  if(location.hash===placeHash) writePlace();
  const releasedAt=placeHash;
  return ()=>{
   if(linkedItem || location.hash!==releasedAt || placeHash!==releasedAt) return;
   linkedItem=key;
   if(wasRevealed) revealedLinkedItem=key;
   writePlace();
  };
 },
 startAction(item){
  const submission=++latestSubmission;
  latestActionFailure=null;
  actionNotices.delete(item.dataset.key);
  const status=recordDetails(item)?.querySelector('[data-send-status]');
  if(status) showActionFailure(status);
  apply(false,{animate:true});
  return submission;
 },
 showActionResult,
 actionNotice(item){return actionNotices.get(item.dataset.key)?.message ?? '';},
 applyAction(){apply(false,{animate:true});},
 rowFading(key){return [...rowMotion.keys()].some(item=>item.dataset.key===key);},
 async rowSettled(item){while(rowMotion.has(item)) await rowMotion.get(item).finished;},
 get hasActiveRowAnimations(){return rowMotion.size>0;},
 settled(){return Promise.all([...rowMotion.values()].map(motion=>motion.finished));},
 replace(draw=true){
  const previous=currentWords(),previousQuery=query;
  holdLog();
  openNeedsRecord=Boolean(openItem && options.mount);
  for(const gap of windowGaps.splice(0)) gap.remove();
  for(const node of mounted.values()){resize?.unobserve(node);options.unmount?.(node);node.remove();}
  mounted.clear();measured.clear();content=contentProxy();
  keepOpenRow();
  if(sharedWords()){query=previous;filterControl.setWords(words(),query);}
  bind();if(draw) applyRows(query!==previousQuery);
 }};
}
''')


DATE_TIME_SCRIPT = r'''
function zoneParts(zone,epoch){
 const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:zone,hourCycle:'h23',
  year:'numeric',month:'numeric',day:'numeric',hour:'numeric',minute:'numeric',second:'numeric'})
  .formatToParts(epoch).map(part=>[part.type,Number(part.value)]));
 return parts;
}
function zoneEpoch(zone,year,month,day,minutes){
 // Use both offsets near the wall time. Choose the earlier repeat or advance through a skipped hour.
 const wall=Date.UTC(year,month-1,day,0,minutes);
 const shownWall=epoch=>{const shown=zoneParts(zone,epoch);
  return Date.UTC(shown.year,shown.month-1,shown.day,shown.hour,shown.minute,shown.second);};
 const offsets=new Set([-86400000,0,86400000].map(delta=>shownWall(wall+delta)-(wall+delta)));
 const candidates=[...offsets].map(offset=>wall-offset),exact=candidates.filter(epoch=>shownWall(epoch)===wall);
 return exact.length ? Math.min(...exact) : Math.max(...candidates);
}
function parseDateTimeDraft(wall){
 const match=/^(\d{4}-\d{2}-\d{2})T([^T]+)$/.exec(wall);
 if(!match) return null;
 const [,day,clock]=match,minutes=dateTimeMinutes(clock);
 if(!Number.isFinite(minutes)) return null;
 const date=new Date(day+'T00:00:00Z');
 if(!Number.isFinite(date.getTime()) || date.toISOString().slice(0,10)!==day) return null;
 return {date,minutes};
}
function dateTimeEpoch(zone,date,minutes){
 return zoneEpoch(zone,date.getUTCFullYear(),date.getUTCMonth()+1,date.getUTCDate(),minutes);
}
function dateTimeMinutes(value){
 const text=value.trim(),match=/^(\d{1,2}):(\d{2})$/.exec(text) || /^(\d{2})(\d{2})$/.exec(text);
 if(!match) return NaN;
 const hour=Number(match[1]),minute=Number(match[2]);
 return hour<24 && minute<60 ? hour*60+minute : NaN;
}
function dateTimeClock(minutes){
 return String(Math.floor(minutes/60)).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');
}
function dateTimeDraftEpoch(zone,wall){
 const draft=parseDateTimeDraft(wall);
 return draft ? dateTimeEpoch(zone,draft.date,draft.minutes) : NaN;
}
function parkPresetTime(preset,now,zone,settings){
 if(preset==='soon') return now+settings.soon*60000;
 const today=zoneParts(zone,now);
 const at=(days,minutes)=>zoneEpoch(zone,today.year,today.month,today.day+days,minutes);
 if(preset==='morning') return at(1,settings.morning);
 const weekday=new Date(Date.UTC(today.year,today.month-1,today.day)).getUTCDay();
 return at((settings.weekDay-weekday+7)%7 || 7,settings.weekTime);
}
function showParkPresets(menu,settings){
 const hours=settings.soon%60===0,count=hours ? settings.soon/60 : settings.soon;
 const unit=hours ? 'hour' : 'minute';
 menu.querySelector('[data-later-preset="soon"] span').textContent=count+' '+unit+(count===1 ? '' : 's');
 menu.querySelector('[data-later-preset="morning"] small').textContent=dateTimeClock(settings.morning);
 const weekdays=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
 menu.querySelector('[data-later-preset="week"] small').textContent=
  weekdays[settings.weekDay]+' '+dateTimeClock(settings.weekTime);
}
function parkRequestTime(form,submitter,zone,settings,wall){
 const preset=submitter?.dataset.laterPreset;
 let until='';
 if(preset==='delivered') until='delivered';
 else if(preset) until=new Date(parkPresetTime(preset,Date.now(),zone,settings)).toISOString();
 else{
  if(/^\d{4}-\d{2}-\d{2}$/.test(wall)) until=wall;
  else{
   const at=dateTimeDraftEpoch(zone,wall);
   if(at>Date.now()) until=new Date(at).toISOString();
  }
 }
 if(until) closeDateTimePicker(form.querySelector('[data-later-menu]'),false);
 return until;
}
function closeDateTimePicker(menu,focus){
 menu.hidden=true;menu.querySelector('[data-later-custom]').hidden=true;delete menu.dataset.choosing;
 const toggle=menu.parentElement.querySelector('[data-later-toggle]');toggle.setAttribute('aria-expanded','false');
 if(focus) toggle.focus();
}
function pickerSummary(zone,at){
 const date=new Intl.DateTimeFormat('en-GB',{timeZone:zone,weekday:'short',day:'numeric',month:'short'});
 const {hour,minute}=zoneParts(zone,at);
 return date.format(at)+' '+dateTimeClock(hour*60+minute);
}
let parkTimeListId=0;
function bindDateTimePicker(form,{zone,settings,summaryPrefix,formatSummary,retryTime=()=>NaN}){
 const currentZone=typeof zone==='function' ? zone : ()=>zone;
 const toggle=form.querySelector('[data-later-toggle]'),menu=form.querySelector('[data-later-menu]');
 const custom=menu.querySelector('[data-later-custom]'),chooseItem=menu.querySelector('[data-later-choose]');
 const items=()=>[...menu.querySelectorAll('[role=menuitem]')];
 toggle.addEventListener('click',()=>{
  if(!menu.hidden){closeDateTimePicker(menu,true);return;}
  showParkPresets(menu,settings());
  menu.hidden=false;toggle.setAttribute('aria-expanded','true');
  items()[0].focus();
 });
 const grid=custom.querySelector('[data-later-grid]'),time=custom.querySelector('[data-later-time]');
 const times=custom.querySelector('[data-later-times]'),timeChoices=custom.querySelector('[data-later-time-choices]');
 times.id='park-times-'+(++parkTimeListId);time.setAttribute('aria-controls',times.id);
 const focusCalendar=()=> (grid.querySelector('[tabindex="0"]') ?? time).focus();
 const save=custom.querySelector('.later-save button'),month=custom.querySelector('[data-later-month]');
 const previousMonth=custom.querySelector('[data-later-month-step="-1"]');
 const field=custom.querySelector('input[type="hidden"]');
 const dayText=date=>date.toISOString().slice(0,10);
 const monthText=new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'long',year:'numeric'});
 const dayLabel=new Intl.DateTimeFormat('en-US',{timeZone:'UTC',dateStyle:'full'});
 let selected=new Date(),display=new Date();
 const today=zone=>{const now=zoneParts(zone,Date.now());return new Date(Date.UTC(now.year,now.month-1,now.day));};
 const update=(normalize=false)=>{
  const zone=currentZone();
  const minutes=dateTimeMinutes(time.value),valid=Number.isFinite(minutes);
  const clock=valid ? dateTimeClock(minutes) : '';
  time.setAttribute('aria-invalid',String(!valid));
  if(valid && normalize) time.value=clock;
  field.value=valid ? dayText(selected)+'T'+clock : '';
  field.dispatchEvent(new Event('input',{bubbles:true}));
  const retry=retryTime();
  const at=Number.isFinite(retry) ? retry : valid ? dateTimeEpoch(zone,selected,minutes) : NaN;
  custom.querySelector('[data-later-summary]').textContent=Number.isFinite(at)
   ? summaryPrefix+(formatSummary ? formatSummary(at) : pickerSummary(zone,at)) : 'Choose a time';
  save.disabled=!Number.isFinite(retry) && !(at>Date.now());
 };
 const activeTime=()=>times.querySelector('[aria-selected="true"]');
 const showTimes=open=>{
  times.hidden=!open;
  time.setAttribute('aria-expanded',String(open));
  if(!open){
   activeTime()?.setAttribute('aria-selected','false');
   time.removeAttribute('aria-activedescendant');
  }
 };
 const acceptTime=option=>{
  time.value=option.textContent;
  showTimes(false);
  update();
 };
 timeChoices.addEventListener('mousedown',event=>event.preventDefault());
 timeChoices.addEventListener('click',()=>{
  time.focus({preventScroll:true});showTimes(times.hidden);
 });
 times.addEventListener('mousedown',event=>{
  if(event.target.closest('[role=option]')) event.preventDefault();
 });
 times.addEventListener('click',event=>{
  const option=event.target.closest('[role=option]');
  if(option) acceptTime(option);
 });
 time.addEventListener('keydown',event=>{
  if(event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if(event.key==='ArrowDown' || event.key==='ArrowUp'){
   if(!times.firstElementChild) return;
   event.preventDefault();
   showTimes(true);
   const active=activeTime();
   const down=event.key==='ArrowDown';
   const next=down ? active?.nextElementSibling : active?.previousElementSibling;
   const option=next ?? active ?? (down ? times.firstElementChild : times.lastElementChild);
   active?.setAttribute('aria-selected','false');
   option.setAttribute('aria-selected','true');
   time.setAttribute('aria-activedescendant',option.id);
   option.scrollIntoView({block:'nearest'});
  }else if(!times.hidden && (event.key==='Enter' || event.key==='Escape')){
   event.preventDefault();
   const option=activeTime();
   if(event.key==='Enter' && option) acceptTime(option);
   else showTimes(false);
  }
 });
 const draw=()=>{
  const zone=currentZone();
  const current=today(zone),currentText=dayText(current),selectedText=dayText(selected);
  const step=settings().step,now=zoneParts(zone,Date.now());
  const firstTime=(Math.floor((now.hour*60+now.minute)/step)+1)*step;
  const clockValues=[];
  for(let minutes=firstTime;minutes<1440;minutes+=step) clockValues.push(minutes);
  if(selected>current) for(let minutes=0;minutes<Math.min(firstTime,1440);minutes+=step) clockValues.push(minutes);
  showTimes(false);
  times.replaceChildren(...clockValues.map(minutes=>{
   const option=document.createElement('div');
   option.id=times.id+'-'+minutes;
   option.setAttribute('role','option');
   option.setAttribute('aria-selected','false');
   option.textContent=dateTimeClock(minutes);
   return option;
  }));
  timeChoices.disabled=!clockValues.length;
  month.textContent=monthText.format(display);grid.replaceChildren();
  grid.setAttribute('aria-label',month.textContent);
  const header=document.createElement('div');header.setAttribute('role','row');
  ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'].forEach(day=>{
   const cell=document.createElement('span');cell.setAttribute('role','columnheader');cell.setAttribute('aria-label',day);
   cell.textContent=day[0];header.append(cell);
  });grid.append(header);
  const first=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth(),1));
  const count=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth()+1,0)).getUTCDate();
  const last=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth(),count));
  const firstEnabled=first>current ? first : current;
  const tabDate=selected>=firstEnabled && selected<=last ? selectedText : dayText(firstEnabled);
  previousMonth.disabled=first<=current;
  let row;
  for(let cell=0;cell<first.getUTCDay()+count;cell++){
   if(cell%7===0){row=document.createElement('div');row.setAttribute('role','row');grid.append(row);}
   const day=cell-first.getUTCDay()+1;
   if(day<1){const blank=document.createElement('span');blank.setAttribute('role','gridcell');row.append(blank);continue;}
   const date=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth(),day)),button=document.createElement('button');
   button.type='button';button.setAttribute('role','gridcell');button.textContent=day;
   button.dataset.date=dayText(date);button.setAttribute('aria-label',dayLabel.format(date));
   button.setAttribute('aria-selected',String(button.dataset.date===selectedText));
   button.tabIndex=button.dataset.date===tabDate ? 0 : -1;button.disabled=date<current;
   if(button.dataset.date===currentText) button.setAttribute('aria-current','date');
   button.addEventListener('click',()=>{selected=date;draw();focusCalendar();});row.append(button);
  }
  update();
 };
 function choose(){
  const zone=currentZone(),draft=parseDateTimeDraft(field.value);
  if(draft && (Number.isFinite(retryTime()) || dateTimeEpoch(zone,draft.date,draft.minutes)>Date.now())){
   selected=draft.date;time.value=dateTimeClock(draft.minutes);
  }else{
   selected=today(zone);selected.setUTCDate(selected.getUTCDate()+1);
   time.value=dateTimeClock(settings().morning);
  }
  const current=today(zone);
  display=new Date(selected<current ? current : selected);
  custom.hidden=false;menu.dataset.choosing='true';draw();
  focusCalendar();
 }
 chooseItem.addEventListener('click',choose);
 custom.querySelectorAll('[data-later-month-step]').forEach(button=>button.addEventListener('click',()=>{
  const hadFocus=document.activeElement===button;
  display=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth()+Number(button.dataset.laterMonthStep),1));
  draw();
  if(hadFocus && button.disabled) focusCalendar();
 }));
 time.addEventListener('input',()=>{showTimes(false);update();});
 time.addEventListener('blur',()=>{showTimes(false);update(true);});
 toggle.parentElement.addEventListener('focusout',event=>{
  if(menu.hidden) return;
  const control=toggle.parentElement;
  if(event.relatedTarget){
   if(!control.contains(event.relatedTarget)) closeDateTimePicker(menu,false);
  }else{
   // The calendar can replace the active day before it sets focus again.
   requestAnimationFrame(()=>{
    if(!menu.hidden && !control.contains(document.activeElement)) closeDateTimePicker(menu,false);
   });
  }
 });
 grid.addEventListener('keydown',event=>{
  const days={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}[event.key];
  const day=event.target.dataset.date;
  if(days===undefined || !day) return;
  event.preventDefault();
  const date=new Date(day+'T00:00:00Z');
  date.setUTCDate(date.getUTCDate()+days);
  if(date<today(currentZone())) return;
  selected=date;display=new Date(date);draw();focusCalendar();
 });
 menu.addEventListener('keydown',event=>{
  if(event.defaultPrevented || event.isComposing) return;
  if(event.target===time &&
     (event.repeat || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey)) return;
  if(event.key==='Escape'){event.preventDefault();closeDateTimePicker(menu,true);return;}
  if(custom.contains(event.target)){
   if(event.key==='Enter' && event.target!==timeChoices){
    event.preventDefault();update(true);if(!save.disabled) form.requestSubmit(save);
   }
   return;
  }
  const list=items();
  const index=list.indexOf(document.activeElement);
  const next={ArrowDown:index+1,ArrowUp:index-1+list.length,Home:0,End:list.length-1}[event.key];
  if(next===undefined) return;
  event.preventDefault();list[next%list.length].focus();
 });
}
document.addEventListener('pointerdown',event=>{
 document.querySelectorAll('[data-later-menu]:not([hidden])').forEach(menu=>{
  if(!menu.parentElement.contains(event.target)) closeDateTimePicker(menu,false);
 });
});
function dateTimePickerOpen(content){
 return [...content.querySelectorAll('[data-later-menu]:not([hidden])')].some(menu=>menu.getClientRects().length);
}
'''
