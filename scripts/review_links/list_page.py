"""Show The List from the producer's numbered records."""
from __future__ import annotations

import html
import json
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, record_store, session_staff, staff, the_list
from scripts.production.update_registration import QuestionStaff, RegistrationFailure
from scripts.review_links import change_browser, change_page, console, page_frame, review_link
from scripts.statusline import counts_row, rebuild_row

PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/list.html"
HEADERS = ("#", "Owner", "Item", "State", "Next step", "ETA", "Update")
SHOWS = (("open", "Open"), ("needs-me", "Needs me"), ("unread", "Unread"), ("done", "Done"),
         ("parked", "Parked"), ("closed", "Closed"), ("all", "All"))


class ListRecord(NamedTuple):
    """An item and the fixed producer ID that owns its record."""

    keeper: staff.StaffId
    item: the_list.ListItem

    @property
    def key(self) -> str:
        return f"{self.keeper}/{self.item.id}"


def page_url() -> str:
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def records() -> list[ListRecord]:
    """Read the list folders even when a producer has no current staff row."""
    try:
        owners = sorted(record_store.state_root().iterdir())
    except FileNotFoundError:
        return []
    found = [ListRecord(staff.StaffId(owner.name), item) for owner in owners
             if owner.is_dir() and not owner.is_symlink()
             for item in the_list.records(staff.StaffId(owner.name), include_closed=True)]
    return sorted(found, key=lambda entry: the_list.order(entry.item))


def waiting_updates(_request: console.NewsRequest) -> frozenset[str]:
    """Identify each unread update by its record and time."""
    return frozenset(f"{entry.key}/{entry.item.unread_since.isoformat()}" for entry in records()
                     if isinstance(entry.item.unread_since, datetime))


def owner_name(members: staff.StaffSnapshot, owner: the_list.ItemOwner) -> str:
    member = members.by_id(owner) if not isinstance(owner, the_list.UnassignedOwner) else None
    return member.name if member is not None else str(owner)


def local_time(moment: datetime, today: datetime) -> str:
    local = moment.astimezone(today.tzinfo)
    return local.strftime("%H:%M %Z" if local.date() == today.date() else "%Y-%m-%d %H:%M %Z")


def eta_text(eta: the_list.Eta, today: datetime) -> str:
    if isinstance(eta, datetime):
        return local_time(eta, today)
    if isinstance(eta, the_list.EtaRange):
        return local_time(eta.earliest, today) + " – " + local_time(eta.latest, today)
    return the_list.eta_text(eta)


def item_link(keeper: staff.StaffId, number: int) -> str:
    return '#' + urlencode({"item": f"{keeper}/L{number}"})


def details(entry: ListRecord, today: datetime) -> str:
    item = entry.item
    sections = ''.join('<section><h3>' + heading + '</h3>'
                       + change_browser.question_paragraphs(text or "Not recorded") + '</section>'
                       for heading, text in (("Latest status", item.latest_status), ("Done when", item.done_when),
                                             ("Evidence", item.evidence)))
    links = [f'<li><a href="{html.escape(item_link(entry.keeper, link.number), quote=True)}">#{link.number}</a>'
             + ' — ' + html.escape(link.condition) + '</li>' for link in item.blocked_by]
    links.extend(f'<li>Related: <a href="{html.escape(item_link(entry.keeper, number), quote=True)}">#{number}</a></li>'
                 for number in item.related_to)
    if links:
        sections += '<section><h3>Links</h3><ul>' + ''.join(links) + '</ul></section>'
    if item.state == the_list.ItemState.CLOSED:
        sections += '<section><h3>Closing words</h3>' + change_browser.question_paragraphs(item.closing_words) + '</section>'
    log = ''.join('<li><time datetime="' + change.time.isoformat() + '">' + html.escape(local_time(change.time, today))
                  + '</time><span>' + html.escape(change.what) + ' <small>— ' + html.escape(change.source)
                  + '</small></span></li>' for change in reversed(item.log))
    return '<div class="list-panel"><div>' + sections + '</div><section><h3>Log · newest first</h3><ol class="list-log">' + log + '</ol></section></div>'


def item_rows(entry: ListRecord, members: staff.StaffSnapshot, today: datetime, index: int) -> str:
    item = entry.item
    owner = owner_name(members, item.owner)
    unread = isinstance(item.unread_since, datetime)
    parked = isinstance(item.parked_until, datetime) and item.parked_until > today
    eta = item.eta.earliest if isinstance(item.eta, the_list.EtaRange) else item.eta
    eta_sort: tuple[int, float | str] = (0, eta.timestamp()) if isinstance(eta, datetime) else (1, eta_text(eta, today).casefold())
    values = (item.number, owner.casefold(), item.title.casefold(), item.state.value.casefold(),
              item.next_step.casefold(), eta_sort, item.unread_since.timestamp() if isinstance(item.unread_since, datetime) else 0)
    attributes = {"key": entry.key, "number": str(item.number), "owner": str(item.owner), "state": item.state.value,
                  "unread": str(unread).lower(), "parked": str(parked).lower(), "title": item.title.lower(),
                  "order": str(index), "sort-values": json.dumps(values, ensure_ascii=False)}
    attrs = ''.join(f' data-{key}="{html.escape(value, quote=True)}"' for key, value in attributes.items())
    panel = f'list-detail-{index}'
    number = (f'<button type="button" class="list-toggle" aria-expanded="false" aria-controls="{panel}" '
              + f'aria-label="Show item {item.number}"><span class="list-chevron" aria-hidden="true">›</span>{item.number}</button>')
    title = '<strong>' + html.escape(item.title) + '</strong>'
    if item.unit:
        title += '<small>' + html.escape(item.unit) + '</small>'
    tone = {the_list.ItemState.WAITING_FOR_NATE: "warning", the_list.ItemState.BLOCKED: "error",
            the_list.ItemState.WORKING: "accent", the_list.ItemState.DELIVERED: "room",
            the_list.ItemState.DONE: "room"}.get(item.state, "muted")
    state = f'<span class="list-state {tone}">{html.escape(item.state.value)}</span>'
    if item.state != the_list.ItemState.CLOSED:
        if isinstance(item.next_check, datetime):
            label = "Check overdue " if item.next_check < today else "Check "
            state += '<small>' + label + html.escape(local_time(item.next_check, today)) + '</small>'
        elif isinstance(item.next_check, the_list.CheckCondition):
            state += '<small>Check: ' + html.escape(item.next_check.text) + '</small>'
    if parked and isinstance(item.parked_until, datetime):
        state += '<small>Parked until ' + html.escape(local_time(item.parked_until, today)) + '</small>'
    update = '<span class="list-dot" role="img" aria-label="Unread update"></span>' if unread else '<span aria-label="No unread update">—</span>'
    cells = (number, html.escape(owner), title, state, html.escape(item.next_step or "Not recorded"),
             html.escape(eta_text(item.eta, today)), update)
    row = ''.join(f'<td data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    hidden = ' hidden' if parked or item.state in (the_list.ItemState.DONE, the_list.ItemState.CLOSED) else ''
    return (f'<tbody data-list-item{attrs}{hidden}><tr class="list-row">' + row
            + f'</tr><tr data-detail id="{panel}" hidden><td colspan="7">' + details(entry, today) + '</td></tr></tbody>')


def change_counts() -> dict[str, int]:
    """Count each Changes page tab from one read of the global lists."""
    snapshots = {path: change_review.review_snapshot(path)
                 for path in sorted(change_review.lists_directory().glob(change_review.PREFIX + "*.md"))}
    prepared = change_review.PreparedChangeLists(snapshots, change_page.list_routing(snapshots))
    totals: dict[str, int] = {}
    for _, owner, _ in prepared.routed:
        totals[owner.tab] = totals.get(owner.tab, 0) + 1
    return totals


def count_link(page: str, place: str, text: str) -> str:
    """Open the count's page through the console navigation."""
    path = f"/{change_page.CONSOLE_FOLDER}/" + console.place_query(page, place)
    return f'<a href="{html.escape(path, quote=True)}" target="_top">{text}</a>'


def counts_strip(members: staff.StaffSnapshot) -> str:
    """Use the status line readers for the counts beside their existing pages."""
    identities = QuestionStaff(members, session_staff.MemberSources(()))
    chips: list[str] = []
    owners = sorted((owner for owner in staff.active(members, "showrunner") if isinstance(owner.doc, staff.LiveDoc)),
                    key=lambda member: member.name.casefold())
    errors = (*console.ITEM_ERRORS, staff.StaffRefused, RegistrationFailure)
    changes: dict[str, int] = {}
    if owners:
        try:
            changes = change_counts()
        except errors:
            pass
    for owner in owners:
        try:
            questions, _ = counts_row.question_counts(owner, user_zone(), identities, None)
        except errors:
            questions = 0
        parts: list[str] = []
        for count, label, path in ((questions, "question", "/console/changes.html?type=questions"),
                                   (changes.get(owner.name, 0), "change", "/console/changes.html")):
            if not count:
                continue
            text = f'<b>{count}</b> {label}' + ('s' if count != 1 else '')
            path += '#' + urlencode({"showrunner": owner.name})
            parts.append(count_link("questions" if label == "question" else "changes", path, text))
        if parts:
            chips.append('<span class="list-count-chip">' + html.escape(owner.name) + ' · ' + ' · '.join(parts) + '</span>')
    try:
        machines = rebuild_row.read(conf.path("statusline")).machines
    except errors:
        machines = ()
    if machines:
        label = "rebuild" if len(machines) == 1 else "rebuilds"
        chips.append('<span class="list-count-chip">'
                     + count_link("rebuilds", "/console/rebuilds.html", f'<b>{len(machines)}</b> {label}')
                     + '</span>')
    return ('<aside class="list-counts" aria-label="Waiting on other pages"><span>Waiting on other pages:</span>'
            + ''.join(chips) + '</aside>') if chips else ''


def render(_root: Path) -> bytes:
    started, read_at = time.perf_counter(), time.time()
    members = staff.snapshot()
    items = records()
    today = datetime.fromtimestamp(read_at, user_zone())
    owners = {str(entry.item.owner): owner_name(members, entry.item.owner) for entry in items}
    options = ''.join(f'<option value="{html.escape(owner, quote=True)}">{html.escape(name)}</option>'
                      for owner, name in sorted(owners.items(), key=lambda pair: (pair[1].casefold(), pair[0])))
    shows = ''.join(f'<option value="{value}">{label}</option>' for value, label in SHOWS)
    headers = ''.join(f'<th scope="col" aria-sort="none"><button type="button" data-sort="{index}">{label}'
                      + '<span data-sort-mark aria-hidden="true"></span></button></th>' for index, label in enumerate(HEADERS))
    seconds = conf.read("review_links").number("page_refresh_seconds")
    content = (STYLE + page_frame.title("The List", "list")
               + f'<div data-list data-refresh-seconds="{seconds}"><div class="list-filters">'
               + '<label for="list-owner">Owner</label><select id="list-owner"><option value="">All</option>' + options + '</select>'
               + '<label for="list-show">Show</label><select id="list-show">' + shows + '</select>'
               + '<label class="list-search-label" for="list-search">Search</label>'
               + '<input type="search" id="list-search" placeholder="Filter by number or title">'
               + '<span data-list-count role="status" aria-live="polite"></span></div>'
               + '<div class="list-table"><table aria-label="The List"><colgroup><col style="width:5%"><col style="width:8%">'
               + '<col style="width:22%"><col style="width:15%"><col style="width:27%"><col style="width:17%"><col style="width:6%">'
               + '</colgroup><thead><tr>' + headers + '</tr></thead>'
               + ''.join(item_rows(entry, members, today, index) for index, entry in enumerate(items)) + '</table>'
               + '<p data-empty hidden>No items in this view.</p></div>' + counts_strip(members) + '</div><script>' + SCRIPT + '</script>')
    return page_frame.rendered(change_page.document(content, "The List"), started, read_at)


STYLE = '''<style>
[hidden]{display:none!important}body{background:var(--bg);color:var(--ink)}
main{max-width:1440px;font-size:15px;line-height:1.5}
.list-filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.list-filters label{display:inline;color:var(--ink-2)}
.list-filters select,.list-filters input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);border-radius:8px;max-width:100%;min-width:0}
.list-filters input{width:260px}[data-list-count]{margin-left:auto;color:var(--ink-2);font-size:13px}
.list-search-label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.list-table{border:1px solid var(--line);border-radius:12px;background:var(--card);overflow:hidden}
.list-table table{width:100%;border-collapse:collapse;table-layout:fixed}
.list-table th{text-align:left;color:var(--ink-2);font-size:13px;border-bottom:1px solid var(--line)}
.list-table th button{font:inherit;font-weight:600;color:inherit;background:none;border:0;margin:0;padding:10px 8px;
min-height:44px;width:100%;text-align:left;border-radius:0}
.list-table th[aria-sort=ascending],.list-table th[aria-sort=descending]{color:var(--accent)}
.list-table td{padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
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
.list-log li{display:grid;grid-template-columns:130px 1fr;gap:12px;padding:6px 0;border-bottom:1px dashed var(--line)}
.list-log time,.list-log small{color:var(--ink-3)}.list-log small{display:inline;font-size:inherit}
.list-counts{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:20px;color:var(--ink-2);font-size:13px}
.list-count-chip{border:1px solid var(--line);border-radius:999px;background:var(--card);padding:5px 12px}
[data-empty]{padding:16px;margin:0;color:var(--ink-2)}
@media(max-width:960px){
.list-table{border:0;background:none;border-radius:0}.list-table table,.list-table tbody{display:block;width:100%}
.list-table colgroup{display:none}.list-table thead{display:flex;margin-bottom:12px}.list-table thead tr{display:flex;flex-wrap:wrap}
.list-table th{border:0}.list-table th button{border:1px solid var(--line);border-radius:8px;width:auto;padding:8px;margin:0 6px 6px 0}
.list-table tbody{border:1px solid var(--line);border-radius:12px;background:var(--card);margin-bottom:16px;overflow:hidden}
.list-row{display:grid;grid-template-columns:72px minmax(0,1fr)}.list-table .list-row td{border:0;padding:5px 14px}
.list-table .list-row td:first-child{grid-column:1;grid-row:1;padding-top:14px}
.list-table .list-row td:nth-child(2){grid-column:2;grid-row:1;padding-top:14px;text-align:right}
.list-table .list-row td:nth-child(n+3){grid-column:1/-1}.list-table .list-row td:nth-child(3){font-size:17px}
.list-table .list-row td:nth-child(5)::before,.list-table .list-row td:nth-child(6)::before{content:attr(data-label) ': ';color:var(--ink-3)}
.list-table .list-row td:last-child{text-align:left;padding-bottom:14px}.list-table .list-row td:last-child::before{content:'Update: ';color:var(--ink-3)}
[data-detail]{display:block}[data-detail]>td{display:block}.list-panel{grid-template-columns:1fr;padding:0 14px 16px;gap:4px}
.list-log li{grid-template-columns:105px minmax(0,1fr)}[data-list-count]{width:100%;margin-left:0}
}
@media(prefers-reduced-motion:reduce){.list-chevron{transition:none}}
</style>'''

SCRIPT = page_frame.REFRESH_SCRIPT + r'''(() => {
let content=document.querySelector('[data-list]');
const columns=[...content.querySelectorAll('[data-sort]')].map(button=>button.textContent);
let owner='',show='open',query='',sort='',direction='asc',opened=new Set();
function place(){
 const params=new URLSearchParams(location.hash.slice(1));
 owner=params.get('owner') || '';show=params.get('show') || 'open';query=params.get('q') || '';
 sort=params.get('sort') || '';direction=params.get('dir')==='desc' ? 'desc' : 'asc';
 if(!['open','needs-me','unread','done','parked','closed','all'].includes(show)) show='open';
 if(!columns.includes(sort)) sort='';
 const item=params.get('item');if(item){opened.add(item);show='all';owner='';query='';}
}
function compare(a,b){
 if(Array.isArray(a)) return a[0]-b[0] || compare(a[1],b[1]);
 return typeof a==='number' ? a-b : a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'});
}
function apply(write=true){
 content.querySelector('#list-owner').value=owner;content.querySelector('#list-show').value=show;
 content.querySelector('#list-search').value=query;
 const items=[...content.querySelectorAll('[data-list-item]')], search=query.trim().toLowerCase();
 const number=search.replace(/^#?l?(?=\d)/,'');
 const visible=item=>{
  const data=item.dataset;
  if(owner && data.owner!==owner) return false;
  if(search && !data.number.includes(number) && !data.title.includes(search)) return false;
  switch(show){
   case 'open':return !['Done','Closed'].includes(data.state) && data.parked!=='true';
   case 'needs-me':return data.state==='Waiting for nate' && data.parked!=='true';
   case 'unread':return data.unread==='true';
   case 'done':return data.state==='Done';
   case 'parked':return data.parked==='true';
   case 'closed':return data.state==='Closed';
   default:return true;
  }
 };
 const column=columns.indexOf(sort);
 items.sort((a,b)=>column<0 ? Number(a.dataset.order)-Number(b.dataset.order) :
  (compare(JSON.parse(a.dataset.sortValues)[column],JSON.parse(b.dataset.sortValues)[column]) ||
   Number(a.dataset.order)-Number(b.dataset.order))*(direction==='desc' ? -1 : 1));
 const table=content.querySelector('table');let count=0;
 items.forEach(item=>{
  item.hidden=!visible(item);if(!item.hidden) count++;
  const expanded=opened.has(item.dataset.key);
  item.querySelector('[data-detail]').hidden=!expanded;
  item.querySelector('.list-toggle').setAttribute('aria-expanded',String(expanded));
  item.querySelector('.list-row').classList.toggle('open',expanded);table.append(item);
 });
 content.querySelectorAll('[data-sort]').forEach((button,index)=>{
  const active=column===index;
  button.parentElement.setAttribute('aria-sort',active ? direction==='desc' ? 'descending' : 'ascending' : 'none');
  button.querySelector('[data-sort-mark]').textContent=active ? direction==='desc' ? ' ↓' : ' ↑' : '';
 });
 content.querySelector('[data-empty]').hidden=count>0;
 content.querySelector('[data-list-count]').textContent=count+' of '+items.length+' items shown · '+
  items.filter(item=>item.dataset.state==='Waiting for nate' && item.dataset.parked!=='true').length+' need you · '+
  items.filter(item=>item.dataset.unread==='true').length+' unread';
 if(write){history.replaceState(null,'',location.pathname+'#'+new URLSearchParams({owner,show,q:query,sort,dir:direction}));
  if(parent!==window) parent.postMessage('console-place',location.origin);}
}
function bind(){
 content.querySelector('#list-owner').addEventListener('change',event=>{owner=event.target.value;apply();});
 content.querySelector('#list-show').addEventListener('change',event=>{show=event.target.value;apply();});
 content.querySelector('#list-search').addEventListener('input',event=>{query=event.target.value;apply();});
 content.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{
  const selected=columns[Number(button.dataset.sort)];direction=sort===selected && direction==='asc' ? 'desc' : 'asc';
  sort=selected;apply();
 }));
 content.querySelectorAll('.list-row').forEach(row=>row.addEventListener('click',event=>{
  if(event.target.closest('a') || (event.target.closest('button') && !event.target.closest('.list-toggle'))) return;
  const key=row.parentElement.dataset.key;
  if(opened.has(key)) opened.delete(key);else opened.add(key);
  apply(false);
 }));
}
async function refresh(_supersede=false,current=()=>true){
 const response=await fetch(location.pathname,{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html');
 const fresh=page.querySelector('[data-list]');
 if(!fresh) throw new Error('The page has no list.');
 if(!current()) return;
 const top=scrollY,focus=document.activeElement?.id;
 const search=content.querySelector('#list-search'), start=search.selectionStart, end=search.selectionEnd;
 content.replaceWith(fresh);content=fresh;bind();apply(false);
 if(focus){const field=document.getElementById(focus);field?.focus({preventScroll:true});
  if(field===content.querySelector('#list-search') && start!==null && end!==null) field.setSelectionRange(start,end);}
 scrollTo({top,behavior:'instant'});return pageRefreshFacts(page);
}
place();bind();apply();
addEventListener('hashchange',()=>{place();apply(false);});
createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
})();'''
