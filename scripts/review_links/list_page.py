"""Show The List from the producer's numbered records."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlencode

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, record_store, session_staff, staff, the_list
from scripts.production.update_registration import QuestionStaff, RegistrationFailure
from scripts.review_links import change_browser, change_page, console, page_frame, record_table, review_link
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


def signed_token(secret: str, keeper: str, identity: str, request: str) -> str:
    return hmac.new(secret.encode(), f"list:{keeper}:{identity}:{request}".encode(), hashlib.sha256).hexdigest()


def request_token(root: Path, keeper: str, identity: str, request: str) -> str:
    secret = change_browser.browser_manifest(change_page.console_folder(root))[0]
    return signed_token(secret, keeper, identity, request)


def feedback_form(entry: ListRecord, secret: str) -> str:
    request = secrets.token_hex(16)
    token = signed_token(secret, entry.keeper, entry.item.id, request)
    fields = {"keeper": entry.keeper, "id": entry.item.id, "request": request, "token": token}
    hidden = ''.join(f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}">'
                     for name, value in fields.items())
    read = '<button name="action" value="read">Mark read</button>' if isinstance(entry.item.unread_since, datetime) else ''
    key = html.escape(entry.key.replace('/', '-'), quote=True)
    return ('<form method="post" data-feedback-form>' + hidden + '<div class="list-actions">' + read
            + '<button name="action" value="remove">Remove</button>'
            + f'<label for="park-{key}">Park until</label><input type="date" name="until" id="park-{key}">'
            + '<button name="action" value="park">Park</button></div>'
            + f'<label for="words-{key}">Tell morty</label><textarea name="words" id="words-{key}" rows="2" maxlength="10000"></textarea>'
            + '<button name="action" value="tell">Send</button><span role="status" data-send-status></span></form>')


def details(entry: ListRecord, today: datetime, secret: str) -> str:
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
    return ('<div class="list-panel"><div>' + sections + feedback_form(entry, secret)
            + '</div><section><h3>Log · newest first</h3><ol class="list-log">' + log + '</ol></section></div>')


def item_rows(entry: ListRecord, members: staff.StaffSnapshot, today: datetime, index: int, secret: str) -> str:
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
    sent = the_list.sent_to_morty(item)
    if isinstance(sent, datetime):
        feedback = tuple(the_list.feedback_requests(item).values())[-1]
        notice = "sent to morty"
        if feedback.state == "alerted":
            notice = "not sent; " + feedback.alerted_to + " told" if feedback.alerted_to else "not sent; no showrunner for alert"
        update += '<small>' + html.escape(notice + ", " + local_time(sent, today)) + '</small>'
    cells = (number, html.escape(owner), title, state, html.escape(item.next_step or "Not recorded"),
             html.escape(eta_text(item.eta, today)), update)
    row = ''.join(f'<td data-label="{label}">{cell}</td>' for label, cell in zip(HEADERS, cells))
    hidden = ' hidden' if parked or item.state in (the_list.ItemState.DONE, the_list.ItemState.CLOSED) else ''
    return (f'<tbody data-list-item{attrs}{hidden}><tr class="list-row">' + row
            + f'</tr><tr data-detail id="{panel}" hidden><td colspan="7">' + details(entry, today, secret) + '</td></tr></tbody>')


@dataclass(frozen=True)
class ListFeedbackDelivery:
    keeper: staff.StaffId
    identity: str
    request: str


def fresh_request(root: Path, keeper: str, identity: str) -> dict[str, str]:
    request = secrets.token_hex(16)
    return {"request": request, "token": request_token(root, keeper, identity, request)}


def submit(root: Path, body: str) -> ListFeedbackDelivery | change_page.NoMessageDelivery:
    """Accept nate's signed words and leave item changes to morty."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"keeper", "id", "request", "token", "action", "words", "until"} or any(len(values) != 1 for values in fields.values()):
        raise change_page.AnswerRefused("use the form on this page")
    keeper, identity, request = fields["keeper"][0], fields["id"][0], fields["request"][0]
    if re.fullmatch(r"[a-f0-9]{32}", request) is None or re.fullmatch(r"L[1-9]\d*", identity) is None:
        raise change_page.AnswerRefused("the List request does not match this page")
    expected = request_token(root, keeper, identity, request)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise change_page.AnswerRefused("the List token does not match this page")
    morty = staff.snapshot().by_id(staff.StaffId(keeper))
    if morty is None or morty.kind != "producer" or morty.name != "morty":
        raise change_page.AnswerRefused("the item has no morty producer")
    item = the_list.load(morty.id, identity)
    action, words, until = fields["action"][0], fields["words"][0].strip(), fields["until"][0]
    if action == "read":
        _ = the_list.mark_read(morty.id, identity, source=the_list.PAGE_SOURCE)
        return change_page.NoMessageDelivery()
    if action == "remove":
        words = f"Remove {item.number}"
    elif action == "park":
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until) is None:
            raise change_page.AnswerRefused("enter a parking date")
        _ = date.fromisoformat(until)
        words = f"Park {item.number} until {until}"
    elif action != "tell" or not words or len(words) > 10000:
        raise change_page.AnswerRefused("enter words for morty")
    saved = the_list.save_feedback(morty.id, identity, the_list.PageFeedback(request, words))
    if saved.state != "pending":
        return change_page.NoMessageDelivery()
    return ListFeedbackDelivery(morty.id, identity, request)


def alert_showrunners(item: the_list.ListItem, members: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """Find the first showrunner above the item owner without session or document reads."""
    owner = members.by_id(item.owner) if not isinstance(item.owner, the_list.UnassignedOwner) else None
    visited: set[staff.StaffId] = set()
    while owner is not None and owner.id not in visited:
        if owner.kind == "showrunner":
            return (owner,)
        visited.add(owner.id)
        owner = members.parent_of(owner)
    return ()


def deliver(delivery: ListFeedbackDelivery | change_page.NoMessageDelivery) -> None:
    """Send saved List words with the shared retry and alert helpers."""
    if isinstance(delivery, change_page.NoMessageDelivery):
        return
    def send(item: the_list.ListItem, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
        settings = conf.read("review_links")
        tries = settings.whole("delivery_tries")
        message = f"List page: nate's words for item {item.number}\n{feedback.words}"
        failures = change_page.send_with_retries(change_page.SENDER, (delivery.keeper,), message,
                                                 tries, settings.number("delivery_pause_seconds"))
        if not failures:
            return replace(feedback, state="sent")
        owners = alert_showrunners(item, staff.snapshot())
        source = the_list.folder(delivery.keeper) / f"{item.id}.md"
        reached = change_page.alert_delivery_failures(failures, tries, source=source, row=item.id, message=message,
            recipients=tuple(owner.id for owner in owners), title="List feedback needs attention")
        return replace(feedback, state="alerted", alerted_to=", ".join(owner.name for owner in owners)) if reached else feedback
    the_list.deliver_feedback(delivery.keeper, delivery.identity, delivery.request, send)


def recover_pending() -> None:
    """Retry each pending request once at server start."""
    for entry in records():
        for feedback in the_list.feedback_requests(entry.item).values():
            if feedback.state == "pending":
                try:
                    deliver(ListFeedbackDelivery(entry.keeper, entry.item.id, feedback.request))
                except (OSError, ValueError) as error:
                    print(f"List feedback recovery {entry.key}: {error}", file=sys.stderr)


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


def render(root: Path) -> bytes:
    started, read_at = time.perf_counter(), time.time()
    members = staff.snapshot()
    items = records()
    today = datetime.fromtimestamp(read_at, user_zone())
    secret = change_browser.browser_manifest(change_page.console_folder(root))[0]
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
               + ''.join(item_rows(entry, members, today, index, secret) for index, entry in enumerate(items)) + '</table>'
               + '<p data-empty hidden>No items in this view.</p></div>' + counts_strip(members) + '</div><script>' + SCRIPT + '</script>')
    return page_frame.rendered(change_page.document(content, "The List"), started, read_at)


STYLE = record_table.STYLE

SCRIPT = page_frame.REFRESH_SCRIPT + record_table.SCRIPT + r'''(() => {
let content=document.querySelector('[data-list]');
let saving=0;
const drafts=new Map();
const tableView=createRecordTable({content:()=>content,
 shows:['open','needs-me','unread','done','parked','closed','all'],
 visible:(data,{owner,show,query:search})=>{
  if(owner && data.owner!==owner) return false;
  const number=search.replace(/^#?l?(?=\d)/,'');
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
 },
 counts:(items,count)=>count+' of '+items.length+' items shown · '+
  items.filter(item=>item.dataset.state==='Waiting for nate' && item.dataset.parked!=='true').length+' need you · '+
  items.filter(item=>item.dataset.unread==='true').length+' unread'
});
const apply=(write=true)=>tableView.apply(write),place=()=>tableView.place();
function bind(){
 tableView.replace();
 content.querySelectorAll('[data-feedback-form]').forEach(form=>{
  const key=form.closest('[data-list-item]').dataset.key;
  const words=form.elements.words,until=form.elements.until;
  const fresh={request:form.elements.request.value,token:form.elements.token.value};
  const draft=drafts.get(key);if(draft){words.value=draft.words;until.value=draft.until;}
  let attempt=draft?.attempt;
  const same=action=>attempt && attempt.words===words.value && attempt.until===until.value &&
   (action===undefined || attempt.action===action);
  const identity=request=>{form.elements.request.value=request.request;form.elements.token.value=request.token;};
  const retain=()=>{drafts.set(key,{words:words.value,until:until.value,attempt});identity(same() ? attempt : fresh);};
  retain();
  words.addEventListener('input',retain);until.addEventListener('input',retain);
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(form.dataset.sending) return;
   const action=event.submitter?.value || 'tell';identity(same(action) ? attempt : fresh);
   const data=new URLSearchParams(new FormData(form));data.set('action',action);
   attempt={words:words.value,until:until.value,action,request:data.get('request'),token:data.get('token')};retain();
   form.dataset.sending='true';saving++;pageRefresh.abandon();
   const buttons=[...form.querySelectorAll('button')],notice=form.querySelector('[data-send-status]');
   buttons.forEach(button=>button.disabled=true);notice.textContent=action==='read' ? 'Saving…' : 'Sending…';
   try{
    const response=await fetch(location.pathname,{method:'POST',body:data});
    const result=await response.json();if(!response.ok) throw new Error(result.reason || 'HTTP '+response.status);
    notice.textContent=action==='read' ? 'Marked read' : 'sent to morty';
    fresh.request=result.request;fresh.token=result.token;attempt=undefined;
    if(action==='tell' && words.value===data.get('words')) words.value='';retain();
    if(action==='read'){
     const item=form.closest('[data-list-item]');item.dataset.unread='false';
     item.querySelector('.list-dot')?.remove();form.querySelector('[value="read"]')?.remove();apply(false);
    }
   }catch(error){notice.textContent='Not saved: '+error.message;}
   finally{
    delete form.dataset.sending;saving--;buttons.forEach(button=>button.disabled=false);
    void pageRefresh.refresh(true);
   }
  });
 });
}
async function refresh(_supersede=false,current=()=>true){
 if(saving) return;
 const response=await fetch(location.pathname,{cache:'no-store'});
 if(!response.ok) throw new Error('HTTP '+response.status);
 const page=new DOMParser().parseFromString(await response.text(),'text/html');
 const fresh=page.querySelector('[data-list]');
 if(!fresh) throw new Error('The page has no list.');
 if(saving || !current()) return;
 const top=scrollY,focus=document.activeElement?.id;
 const field=document.activeElement,start=field?.selectionStart,end=field?.selectionEnd;
 content.replaceWith(fresh);content=fresh;bind();apply(false);
 if(focus){const field=document.getElementById(focus);field?.focus({preventScroll:true});
  if(typeof field?.setSelectionRange==='function' && typeof start==='number' && typeof end==='number') field.setSelectionRange(start,end);}
 scrollTo({top,behavior:'instant'});return pageRefreshFacts(page);
}
place();bind();apply();
addEventListener('hashchange',()=>{place();apply(false);});
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
})();'''
