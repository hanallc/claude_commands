"""Show investigation questions and answers for every fixed staff owner."""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import investigation, staff
from scripts.review_links import change_browser, change_page, console, page_frame, review_link

PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/investigations.html"


def page_url() -> str:
    """Return the fixed page path and prepare its form token."""
    _ = change_page.console_folder(review_link.review_root().resolve())
    return PAGE_PATH


def records() -> list[investigation.Investigation]:
    """Read every owner folder, including owners without current staff rows."""
    try:
        owners = sorted(investigation.state_root().iterdir())
    except FileNotFoundError:
        return []
    items: list[investigation.Investigation] = []
    for owner in owners:
        if owner.is_dir() and not owner.is_symlink():
            items.extend(investigation.records(staff.StaffId(owner.name), include_read=True))
    return sorted(items, key=order)


def order(item: investigation.Investigation) -> tuple[int, float, str, str]:
    """Put recent answers first, waiting questions next, and read answers last."""
    state = investigation.InvestigationState
    rank = {state.ANSWER_READY: 0, state.OPEN: 1, state.READ: 2}[item.state]
    moment = item.answered if rank == 0 else item.filed if rank == 1 else item.read
    timestamp = datetime.fromisoformat(moment).timestamp()
    return rank, timestamp if rank == 1 else -timestamp, item.owner, item.id


def waiting_investigations(_request: console.NewsRequest) -> frozenset[str]:
    """Name each ready answer by its owner, id, and answer time."""
    return frozenset(f"{item.owner}/{item.id}/{item.answered}" for item in records()
                     if item.state == investigation.InvestigationState.ANSWER_READY)


def owner_name(members: staff.StaffSnapshot, owner: staff.StaffId) -> str:
    member = members.by_id(owner)
    return member.name if member is not None else str(owner)


def request_token(root: Path, owner: str, identity: str) -> str:
    token = change_browser.browser_manifest(change_page.console_folder(root))[0]
    return hmac.new(token.encode(), f"investigation:{owner}:{identity}".encode(), hashlib.sha256).hexdigest()


def card(item: investigation.Investigation, root: Path, members: staff.StaffSnapshot) -> str:
    filed = datetime.fromisoformat(item.filed).astimezone(user_zone())
    name = change_page.source_name(members, item.owner)
    details = (("source", name), ("date", filed.strftime("%Y-%m-%d")), ("time", filed.strftime("%H:%M")))
    content = (f'<article data-owner="{html.escape(item.owner, quote=True)}" data-state="{item.state}" '
               + f'data-key="{html.escape(f"{item.owner}/{item.id}", quote=True)}">'
               + f'<h2>{html.escape(item.title)}</h2>' + change_browser.question_paragraphs(item.question))
    if item.answer:
        content += '<h3>Answer</h3>' + change_browser.question_paragraphs(item.answer)
    if item.state == investigation.InvestigationState.ANSWER_READY:
        content += ('<form method="post" data-read-form>'
                    + f'<input type="hidden" name="owner" value="{html.escape(item.owner, quote=True)}">'
                    + f'<input type="hidden" name="id" value="{item.id}">'
                    + f'<input type="hidden" name="token" value="{request_token(root, item.owner, item.id)}">'
                    + '<button>Mark read</button><span role="status" data-save-status></span></form>')
    elif item.state == investigation.InvestigationState.OPEN:
        content += '<p>Waiting for an answer</p>'
    content += '<dl class="change-details">' + ''.join(
        f'<dt>{label}:</dt><dd>{html.escape(value)}</dd>' for label, value in details) + '</dl></article>'
    return content


def render(root: Path) -> bytes:
    started, read_at = time.perf_counter(), time.time()
    members = staff.snapshot()
    items = records()
    owners = {item.owner: owner_name(members, item.owner) for item in items}
    options = ''.join(f'<option value="{html.escape(owner, quote=True)}">{html.escape(name)}</option>'
                      for owner, name in sorted(owners.items(), key=lambda pair: (pair[1].casefold(), pair[0])))
    seconds = conf.read("review_links").number("page_refresh_seconds")
    content = (STYLE + page_frame.title("Investigations", "investigations")
               + f'<div data-investigations data-refresh-seconds="{seconds}">'
               + '<div data-filters><label for="investigation-owner">Owner:</label>'
               + f'<select id="investigation-owner"><option value="">All</option>{options}</select>'
               + '<label for="investigation-show">Show:</label><select id="investigation-show">'
               + '<option value="not-read">Not read</option><option value="answer ready">Answer ready</option>'
               + '<option value="open">Waiting</option><option value="read">Read</option><option value="all">All</option>'
               + '</select></div><div data-cards>' + ''.join(card(item, root, members) for item in items)
               + '</div><p data-empty hidden>No investigations in this view.</p></div>'
               + '<script>' + SCRIPT + '</script>')
    return page_frame.rendered(change_page.document(content, "Investigations"), started, read_at)


def submit(root: Path, body: str) -> None:
    """Mark the record named by a signed form as read."""
    fields = parse_qs(body, keep_blank_values=True, strict_parsing=True)
    if set(fields) != {"owner", "id", "token"} or any(len(value) != 1 for value in fields.values()):
        raise change_page.AnswerRefused("use the form on this page")
    owner, identity = fields["owner"][0], fields["id"][0]
    expected = request_token(root, owner, identity)
    if not secrets.compare_digest(fields["token"][0].encode(), expected.encode()):
        raise change_page.AnswerRefused("the investigation token does not match this page")
    _ = investigation.mark_read(staff.StaffId(owner), identity)


STYLE = '''<style>[hidden]{display:none!important}
[data-filters]{display:flex;align-items:center;gap:8px;margin-bottom:16px;flex-wrap:wrap}
[data-filters] select{font:inherit;min-height:44px;min-width:0;max-width:100%;color:CanvasText;background:Canvas}
[data-cards] article{border-top:1px solid #8886;padding:24px 0}
[data-cards] article:first-child{border-top:0;padding-top:8px}
[data-cards] h2{margin:0 0 16px}[data-cards] h3{font-size:17px;margin:16px 0}
[data-read-form]{display:flex;align-items:center;gap:8px}[data-read-form] button{margin:0}
</style>'''

SCRIPT = page_frame.REFRESH_SCRIPT + r'''(() => {
let content=document.querySelector('[data-investigations]'), sending=0, active=false, generation=0;
const start=new URLSearchParams(location.hash.slice(1));
let owner=start.get('owner') || '', show=start.get('show') || 'not-read';
if(!['not-read','answer ready','open','read','all'].includes(show)) show='not-read';
function apply(){
 const ownerMenu=content.querySelector('#investigation-owner'), showMenu=content.querySelector('#investigation-show');
 ownerMenu.value=owner;showMenu.value=show;
 let count=0;
 content.querySelectorAll('article').forEach(card=>{
  card.hidden=!!(owner && card.dataset.owner!==owner) ||
   (show==='not-read' ? card.dataset.state==='read' : show!=='all' && card.dataset.state!==show);
  if(!card.hidden) count++;
 });
 content.querySelector('[data-empty]').hidden=count>0;
 history.replaceState(null,'',location.pathname+'#'+new URLSearchParams({owner,show}));
 if(parent!==window) parent.postMessage('console-place',location.origin);
}
function bind(){
 content.querySelector('#investigation-owner').addEventListener('change',event=>{owner=event.target.value;apply();});
 content.querySelector('#investigation-show').addEventListener('change',event=>{show=event.target.value;apply();});
 content.querySelectorAll('[data-read-form]').forEach(form=>form.addEventListener('submit',async event=>{
  event.preventDefault();if(form.dataset.sending) return;
  form.dataset.sending='true';sending++;generation++;active=false;pageRefresh.abandon();
  const status=form.querySelector('[data-save-status]'), button=form.querySelector('button');
  button.disabled=true;status.textContent='';let saved=false;
  try{
   const response=await fetch(location.pathname,{method:'POST',body:new URLSearchParams(new FormData(form))});
   if(!response.ok){const body=await response.json();throw new Error(body.reason || 'HTTP '+response.status);}
   const card=form.closest('article'), list=card.parentNode;
   card.dataset.state='read';form.remove();
   const firstRead=[...list.children].find(other=>other!==card && other.dataset.state==='read');
   list.insertBefore(card,firstRead || null);apply();saved=true;
  }catch(error){status.textContent='Not saved: '+error.message;button.disabled=false;}
  finally{delete form.dataset.sending;sending--;if(saved) void pageRefresh.refresh(true);}
 }));
}
async function refresh(supersede=false,current=()=>true){
 if(sending || (active && !supersede)) return;
 const request=++generation;active=true;
 try{
  const response=await fetch(location.pathname,{cache:'no-store'});
  if(!response.ok) throw new Error('HTTP '+response.status);
  const page=new DOMParser().parseFromString(await response.text(),'text/html');
  const fresh=page.querySelector('[data-investigations]');
  if(sending || request!==generation || !current()) return;
  if(!fresh) throw new Error('The page has no investigation list.');
  const top=scrollY, focus=document.activeElement?.id;
  content.replaceWith(fresh);content=fresh;bind();apply();
  if(focus) document.getElementById(focus)?.focus({preventScroll:true});
  scrollTo({top,behavior:'instant'});return pageRefreshFacts(page);
 }finally{if(request===generation) active=false;}
}
bind();apply();
window.addEventListener('hashchange',()=>{
 const place=new URLSearchParams(location.hash.slice(1));
 owner=place.get('owner') || '';show=place.get('show') || 'not-read';apply();
});
const pageRefresh=createPageRefresh(refresh,()=>content.dataset.refreshSeconds);
})();'''
