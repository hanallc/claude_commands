"""The main column, the title and the image zoom that every console page shares."""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.production import staff
from scripts.review_links import send_hold, setting_controls

ICONS = Path(__file__).with_name("lucide")
QUESTION_WAITING = "The {owner_role} will answer this"
OWNER_PAGES = frozenset(("accounts", "aws", "builds", "disks", "images", "list", "aws_machines", "placement_disk",
                         "rebuilds", "review"))
TITLE_ELEMENT = re.compile(rb"<h1(?=[\s>])[^>]*>.*?</h1>", re.DOTALL)
HEADING_ACTIONS = '<div class="page-heading-actions" data-page-actions>'


class OwnerNewItem(StrEnum):
    FOR_SHOWN_OWNER = "shown"
    CHOOSE_RECIPIENT = "choose-recipient"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class NoShownOwner:
    """There is no staff name to show in the page heading."""


RESERVED_PAGE_ACTIONS = frozenset(("new-item", "owner"))


def page_action_button(action: str, icon: str, label: str, dialog_id: str) -> str:
    """Render one page action button. The frame opens its dialog when the button is pressed."""
    if action in RESERVED_PAGE_ACTIONS:
        raise ValueError(f"reserved page action: {action}")
    action, label, dialog_id = (html.escape(value, quote=True) for value in (action, label, dialog_id))
    return (f'<button type="button" class="page-action" data-page-action="{action}" aria-label="{label}" '
            + f'title="{label}" aria-haspopup="dialog" aria-controls="{dialog_id}">' + icon_svg(icon) + '</button>')


def owner_control(page: str, owner: staff.StaffRow | NoShownOwner, members: staff.StaffSnapshot, *,
                  recipient: str | OwnerNewItem = OwnerNewItem.FOR_SHOWN_OWNER, actions: tuple[str, ...] = ()) -> str:
    """Render the owner name, the page action buttons and the New item button for the page action area."""
    from scripts.review_links import staff_names

    name = ""
    if isinstance(owner, staff.StaffRow):
        rendered = staff_names.StaffNames(members).html(owner.id, owner.name)
        name = f'<span class="page-owner-name">{rendered}</span>'
    button = ""
    if recipient is not OwnerNewItem.UNAVAILABLE:
        label = "New item"
        if recipient is OwnerNewItem.FOR_SHOWN_OWNER and isinstance(owner, staff.StaffRow):
            label = f"New item for {owner.name}"
        elif not isinstance(recipient, OwnerNewItem):
            label = f"New item for {recipient}"
        label = html.escape(label, quote=True)
        button = (f'<button type="button" data-owner-new aria-label="{label}" title="{label}">'
                  + icon_svg("message-circle-question") + '</button>')
    return f'<div class="page-owner" data-page-owner="{page}">' + name + "".join(actions) + button + '</div>'


def with_owner(content: bytes, page: str, owner: staff.StaffRow | NoShownOwner, members: staff.StaffSnapshot,
               dialog: Callable[[], str], *,
               recipient: str | OwnerNewItem = OwnerNewItem.FOR_SHOWN_OWNER, actions: tuple[str, ...] = ()) -> bytes:
    """Add the shared owner control to one served console document."""
    main = re.search(rb"<main(?=[\s>])[^>]*>", content)
    if main is None or b'data-page-owner="' in content:
        return content
    control = owner_control(page, owner, members, recipient=recipient, actions=actions).encode()
    # The action area comes just before the heading row. Only the area stays in view; the title scrolls.
    # The script at the end of the row places the area before the browser shows the content after it.
    area = HEADING_ACTIONS.encode() + control + b'</div>'
    heading = TITLE_ELEMENT.search(content, main.end())
    if heading is not None:
        row = area + b'<div class="page-heading">' + heading[0] + HEADING_MEASURE.encode() + b'</div>'
        content = content[:heading.start()] + row + content[heading.end():]
    else:
        row = area + b'<div class="page-heading">' + HEADING_MEASURE.encode() + b'</div>'
        content = content[:main.end()] + row + content[main.end():]
    head = re.search(rb"<head(?=[\s>])[^>]*>", content)
    if head is not None:
        content = content[:head.end()] + PAGE_SCRIPT_ERRORS.encode() + content[head.end():]
    row_style = HEADING_STYLE.encode()
    style = (OWNER_STYLE + ACTIONS_SCRIPT).encode()
    if BUTTON_STATES.encode() not in content:
        style = b'<style>' + BUTTON_STATES.encode() + b'</style>' + style
    if row_style not in content:
        style = b'<style>' + row_style + b'</style>' + style
    content = content.replace(b'</head>', style + b'</head>', 1)
    if recipient is not OwnerNewItem.UNAVAILABLE:
        markup = dialog()
        if CONSOLE_DIALOG_ASSETS.encode() in content:
            markup = markup.replace(CONSOLE_DIALOG_ASSETS, '', 1)
        content = content.replace(b'</body>', markup.encode() + b'</body>', 1)
    return content


HEADING_MEASURE = '<script>window.measurePageActions?.();</script>'
# Without the script, the area takes its height above the title, so the title never goes under the controls.
# When the full title and the controls fit on one line, the script puts the controls beside the title.
HEADING_STYLE = ('.page-heading{box-sizing:border-box;display:flex;align-items:center;margin-bottom:8px}'
                 + '.page-heading>h1{margin:0}'
                 + '[data-page-actions-beside]+.page-heading{min-height:var(--page-actions-height,0px);'
                 + 'padding-inline-end:var(--page-actions-reserve,0px)}')


# The stacking order, lowest to highest: page content with its own sticky bars (z-index 3 or lower),
# the action area (4), overlays such as tooltips, menus and the Builds paused badge (5 to 10),
# and the sticky status line (20).
# Beside the title, the action area has no height, so it takes no layout space.
# Its controls overflow it at the top right, and the heading row keeps space for them.
# Only the area sticks; the title scrolls with the page.
# A copy of the area shows in each open page window. The window rule in ACTIONS_SCRIPT adds and removes it.
OWNER_STYLE = """<style>
.page-owner{display:flex;align-items:center;gap:8px;min-width:0;font:600 14px/1.4 system-ui,sans-serif}
.page-owner-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.page-owner button{display:grid;place-items:center;flex:none;width:36px;height:36px;min-height:36px;
margin:0;padding:7px;background:var(--card,#1a1a19);color:var(--ink-2,#c3c2b7);border:1px solid var(--line,#34332f);border-radius:8px;cursor:pointer}
.page-owner svg{width:18px;height:18px}
:root{--page-actions-space:8px;--page-actions-height:36px}
[data-page-actions]{position:sticky;top:var(--page-actions-space);z-index:4;flex:0 0 100%;display:flex;
justify-content:flex-end;align-items:flex-start;gap:8px;max-width:100%;margin-bottom:8px;color:var(--ink,CanvasText)}
[data-page-actions][data-page-actions-beside]{height:0;margin-bottom:0}
[data-page-actions]>*{background:var(--bg,Canvas);box-shadow:0 0 0 4px var(--bg,Canvas)}
[data-page-window-actions]{position:fixed;margin:0;pointer-events:none}
[data-page-window-actions]>*{background:none;box-shadow:none;pointer-events:auto}
</style>"""


# The script sets the sizes that the heading row and other sticky parts use.
# A Late or Failed status line sticks directly below the stuck area, so the page order never changes.
# The window rule: while a page window (a modal dialog) is open, the rest of the page is inert.
# Thus each open window gets a copy of the action area at the same place, above the window.
# The window keeps the copy until it closes. When a window meets the controls from left to right,
# the frame sets --page-window-top on it to the bottom of the action area. A centered window limits
# its height with it, so it stays centered and clears the copy. A window at the top starts below it.
# Without the value, each window has the place and the height of main.
# A screenshot address names the open window, so the capture shows the page as the user sees it.
ACTIONS_SCRIPT = """<script>
function stickyPageStatusHeight(){
 const banner=document.querySelector('[data-page-status]');
 return banner && getComputedStyle(banner).position==='sticky' ? banner.getBoundingClientRect().height : 0;
}
function pageActionsSpace(){
 return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--page-actions-space'));
}
function pageActionsArea(){
 return document.querySelector('[data-page-actions]:not([data-page-window-actions])');
}
// The area has no height; this box holds the controls that overflow it.
function pageActionsBox(area=pageActionsArea()){
 const home=area.getBoundingClientRect();
 let left=home.right,bottom=home.top;
 for(const control of area.children){
  const box=control.getBoundingClientRect();
  left=Math.min(left,box.left);bottom=Math.max(bottom,box.bottom);
 }
 return {top:home.top,left,right:home.right,bottom,width:home.right-left,height:bottom-home.top};
}
function stuckPageActionsBottom(){
 const area=pageActionsArea();
 return area ? pageActionsSpace()+pageActionsBox(area).height+stickyPageStatusHeight() : stickyPageStatusHeight();
}
// The full title and the controls must fit on one line, or the controls stay above the title.
function measurePageActions(){
 const root=document.documentElement, home=pageActionsArea();
 if(!home) return;
 const row=home.nextElementSibling, title=row?.querySelector(':scope>h1');
 const set=(name,pixels)=>{if(root.style.getPropertyValue(name)!==pixels+'px') root.style.setProperty(name,pixels+'px');};
 const box=pageActionsBox(home);
 let beside=box.width>0;
 if(title && beside){
  const style=title.getAttribute('style');
  title.style.cssText+=';flex:none;width:max-content';
  beside=title.getBoundingClientRect().width+box.width+12<=row.clientWidth;
  if(style===null) title.removeAttribute('style'); else title.setAttribute('style',style);
 }
 if(home.hasAttribute('data-page-actions-beside')!==beside) home.toggleAttribute('data-page-actions-beside',beside);
 set('--page-status-height',stickyPageStatusHeight());
 set('--page-actions-height',box.height);
 set('--page-actions-reserve',beside ? box.width+12 : 0);
}
(()=>{
 const root=document.documentElement, windows=[];
 let clicked=null, reopen='', reopenClick=null;
 // Remove the window name from the address before the page reads it.
 const named=location.hash.match(/(?:^#|&)console-window=([^&]*)/);
 if(named){
  try{reopen=decodeURIComponent(named[1]);}catch(_){}
  const hash=location.hash.replace(named[0],named[0].startsWith('#') ? '#' : '').replace(/^#&/,'#');
  history.replaceState(history.state,'',location.pathname+location.search+(hash==='#' ? '' : hash));
 }
 document.addEventListener('click',event=>{
  clicked=event.target instanceof Element ? event.target : null;
  setTimeout(()=>{clicked=null;});
 },true);
 // A link that opens no window must not move the page.
 addEventListener('click',event=>{if(event===reopenClick) event.preventDefault();});
 // A page action button opens its dialog. When the dialog closes, focus goes back to the button.
 // A press on the copy inside the open dialog moves the focus to the first control of the dialog.
 document.addEventListener('click',event=>{
  const button=event.target instanceof Element ? event.target.closest('[data-page-action]') : null;
  const dialog=button && document.getElementById(button.getAttribute('aria-controls'));
  if(!(dialog instanceof HTMLDialogElement)) return;
  if(dialog.open){
   const controls=dialog.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])');
   const first=[...controls].find(control=>!control.closest('[data-page-window-actions]') && !control.disabled);
   (first ?? dialog).focus({preventScroll:true});
   return;
  }
  bindConsoleDialog(dialog,()=>dialog.close());
  dialog.addEventListener('close',()=>button.focus({preventScroll:true}),{once:true});
  dialog.showModal();
 });
 // A scroll does not change the left and right edges, so it only moves the copy and the value (measure=false).
 function place(measure=true){
  if(!windows.length) return;
  const home=pageActionsArea();
  if(!home) return;
  const box=home.getBoundingClientRect(), area=pageActionsBox(home);
  for(const entry of windows){
   const {dialog,copy}=entry;
   Object.assign(copy.style,{top:box.top+'px',left:box.left+'px',width:box.width+'px'});
   if(measure){
    // The window gets the value only where its parts meet the controls from left to right.
    const meets=()=>[...dialog.children].some(part=>{
     const edges=part.getBoundingClientRect();
     return part!==copy && edges.width>0 && edges.left<area.right && edges.right>area.left;
    });
    // The value only makes parts narrower, because the zoom image follows its height limit.
    // Thus a window that meets with the value also meets without it.
    // Remove the value only when the window does not meet with it, so a measure keeps the scroll of the window.
    entry.meets=meets() || (dialog.style.getPropertyValue('--page-window-top')!==''
     && (dialog.style.removeProperty('--page-window-top'),meets()));
   }
   const value=entry.meets ? area.bottom+'px' : '';
   if(dialog.style.getPropertyValue('--page-window-top')!==value) dialog.style.setProperty('--page-window-top',value);
  }
 }
 document.addEventListener('beforetoggle',event=>{
  const dialog=event.target, home=pageActionsArea();
  if(!(dialog instanceof HTMLDialogElement) || !home) return;
  const index=windows.findIndex(entry=>entry.dialog===dialog);
  if(event.newState==='open'){
   if(index>=0) return;
   const copy=home.cloneNode(true);
   copy.toggleAttribute('data-page-window-actions',true);
   copy.querySelectorAll('[id]').forEach(node=>node.removeAttribute('id'));
   // The control that opened the window names it in a screenshot address.
   const launcher=clicked?.closest('[data-window-place],a[href]');
   windows.push({dialog,copy,place:launcher ? launcher.dataset.windowPlace ?? launcher.getAttribute('href') : ''});
   // The copy comes last, so the window keeps its own first focus.
   dialog.append(copy);
   // The window has its box only after it opens. The measure runs before the browser shows it.
   queueMicrotask(place);
  }else if(index>=0){
   windows.splice(index,1)[0].copy.remove();
   dialog.style.removeProperty('--page-window-top');
   place();
  }
 },true);
 // The edges change only when a window opens or closes, the view or the area changes size, or a picture loads.
 // The width of each window follows only these, so no observer of the window is necessary.
 addEventListener('resize',()=>place());
 addEventListener('scroll',()=>place(false),{passive:true});
 // A picture in a window gets its width when it loads.
 document.addEventListener('load',()=>place(),true);
 window.pageCapturePlace=(except)=>{
  const address=location.pathname+location.search+location.hash;
  const shown=windows.filter(entry=>entry.dialog!==except && entry.dialog.open).at(-1)?.place;
  return shown ? address+(location.hash ? '&' : '#')+'console-window='+encodeURIComponent(shown) : address;
 };
 function openNamed(){
  if(!reopen) return true;
  let launcher=null;
  try{
   launcher=document.querySelector('[data-window-place="'+CSS.escape(reopen)+'"]')
    || document.querySelector('a[href="'+CSS.escape(reopen)+'"]');
  }catch(_){reopen='';return true;}
  if(!launcher) return false;
  reopen='';
  reopenClick=new MouseEvent('click',{bubbles:true,cancelable:true,button:0,view:window});
  launcher.dispatchEvent(reopenClick);
  reopenClick=null;
  return true;
 }
 if(reopen){
  // The page can add its controls after it loads its data. The screenshot waits only for the load event.
  const watch=new MutationObserver(()=>{if(openNamed()) watch.disconnect();});
  document.addEventListener('DOMContentLoaded',()=>{
   if(!openNamed()) watch.observe(root,{childList:true,subtree:true});
  },{once:true});
  addEventListener('load',()=>{openNamed();watch.disconnect();reopen='';},{once:true});
 }
 document.addEventListener('DOMContentLoaded',()=>{
  const home=pageActionsArea(), banner=document.querySelector('[data-page-status]');
  if(!home) return;
  const row=home.nextElementSibling, title=row?.querySelector(':scope>h1');
  const measure=()=>{measurePageActions();place();};
  const resize=new ResizeObserver(measure);
  resize.observe(home);
  for(const control of home.children) resize.observe(control);
  if(title){resize.observe(row);resize.observe(title);}
  if(banner){
   resize.observe(banner);
   // The clock writes the same state each second; only a new state can change the position.
   new MutationObserver(records=>{if(records.some(record=>record.oldValue!==banner.dataset.state)) measure();})
    .observe(banner,{attributeFilter:['data-state'],attributeOldValue:true});
  }
  measure();
 },{once:true});
})();
</script>"""


PAGE_SCRIPT_ERRORS = r"""<script>(() => {
 const errors = [];
 window.consoleScriptErrors = errors;
 const clip = (value, length) => value.match(new RegExp('^.{0,' + length + '}', 'su'))[0];
 function remember(message, source = '', line = 0, column = 0) {
  errors.push({message: clip(message.trim() || 'Script error', 512), source: clip(source, 2048),
               line, column, at: new Date().toISOString()});
  if (errors.length > 20) errors.shift();
 }
 window.addEventListener('error', event => {
  if (event instanceof ErrorEvent) remember(event.message, event.filename, event.lineno, event.colno);
 });
 window.addEventListener('unhandledrejection', event => {
  const reason = event.reason;
  remember(reason instanceof Error ? reason.message :
           typeof reason === 'string' ? reason : 'Unhandled promise rejection');
 });
})();</script>"""


def icon_svg(name: str) -> str:
    """Read a vendored Lucide icon without a path outside the icon folder."""
    if not re.fullmatch(r"[a-z][a-z-]*", name):
        raise ValueError(f"unsupported console icon: {name}")
    source = (ICONS / f"{name}.svg").read_text(encoding="utf-8")
    return source[source.index("<svg"):].replace("<svg", '<svg aria-hidden="true" focusable="false"', 1)


def title(text: str, page: str) -> str:
    """Build the title and refresh status."""
    settings = conf.read_or_repository("console")
    options = {"roundTripLimit": settings.number("status.round_trip_limit_seconds"),
               "renderLimit": settings.number("status.render_limit_seconds"),
               "readingDelay": settings.number("status.reading_delay_seconds"),
               "ageLimit": settings.number(f"status.{page}.age_limit_seconds"), "zone": str(user_zone())}
    icons = ''.join(f'<span data-status-icon="{state}"' + ('' if state == 'Reading' else ' hidden')
                    + '>' + icon_svg(icon) + '</span>' for state, icon in (
                        ('Live', 'radio'), ('Reading', 'loader-circle'), ('Late', 'clock-alert'),
                        ('Failed', 'triangle-alert'), ('Paused', 'circle-pause')))
    heading = f'<h1 class="page-title">{html.escape(text)}</h1>'
    return (heading
            + '<p class="page-status" data-page-status data-state="Reading" role="status" aria-live="off" '
            + f'data-status-options="{html.escape(json.dumps(options), quote=True)}" data-page-facts>'
            + '<span class="page-status-state">' + icons + '<span data-status-word>Reading…</span></span>'
            + '<span data-status-age hidden></span><span data-status-trip hidden></span>'
            + '<span data-status-render hidden></span><span data-status-reason hidden></span>'
            + '<span class="page-status-announcement" aria-live="polite" data-status-announcement></span></p>')


def rendered(content: bytes, started: float, read_at: float) -> bytes:
    """Attach the measured server build time and the data read time."""
    facts = f'data-page-facts data-read-at="{read_at:.6f}" data-render-seconds="{time.perf_counter() - started:.6f}"'
    return content.replace(b'data-page-facts', facts.encode(), 1)


def template(source: str, text: str, page: str) -> str:
    """Fill the shared title, style and script in a static page template."""
    return (source.replace("__PAGE_FRAME_STYLE__", STYLE).replace("__PAGE_TITLE__", title(text, page))
            .replace("__PAGE_REFRESH_SCRIPT__", REFRESH_SCRIPT)
            .replace("__PAGE_SETTING_CONTROLS__", setting_controls.ASSETS))


CONSOLE_READY_SCRIPT = """(() => {
 const ready = () => window.frameElement?.dispatchEvent(new Event('console-document-ready'));
 document.addEventListener('DOMContentLoaded', ready, {once:true});
 addEventListener('pageshow', event => { if (event.persisted) ready(); });
})();"""


REFRESH_SCRIPT = r'''function pageRefreshFacts(page=document){
 const line=page.querySelector('[data-page-status]');
 if(!line || !line.dataset.readAt) throw new Error('The page has no data time.');
 return {readAt:Number(line.dataset.readAt),renderSeconds:Number(line.dataset.renderSeconds),
  renderMedian:line.dataset.renderMedian,renderCount:line.dataset.renderCount,failure:line.dataset.readError};
}
function restoreFocusAfterRefresh(){
 const {id,selectionStart,selectionEnd,selectionDirection}=document.activeElement || {};
 return ()=>{
  if(!id) return;
  const field=document.getElementById(id);
  field?.focus({preventScroll:true});
  if(typeof field?.setSelectionRange==='function' && typeof selectionStart==='number' && typeof selectionEnd==='number'){
   field.setSelectionRange(selectionStart,selectionEnd,selectionDirection);
  }
 };
}
function createPageRefresh(load,seconds=()=>0,hold=()=>'',initial){
 const line=document.querySelector('[data-page-status]');
 const options=line ? JSON.parse(line.dataset.statusOptions) : {};
 const hidden=()=>document.visibilityState!=='visible' || window.frameElement?.hidden===true;
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 let last=null,failure='',reading=false,busy='';
 let request=0,active=false,started=0,pending=null,timer,clock,delay;
 let trip=null,state='',nextAt=0;
 const duration=value=>Number(value)<10 ? Number(value).toFixed(2) : Number(value).toFixed(0);
 const held=()=>hidden() ? 'This page is hidden.' : hold();
 function draw(){
  if(!line) return;
  const reason=held(),age=last ? Math.max(0,Math.floor(Date.now()/1000-last.readAt)) : 0;
  // An on-demand page can report a server answer separately from the data time.
  const unanswered=last ? Math.max(0,Math.floor(Date.now()/1000-(last.answeredAt ?? last.readAt))) : 0;
  const next=reason ? 'Paused' : failure ? 'Failed' : last && unanswered>options.ageLimit ? 'Late' : busy || last?.busy ? 'Paused' : reading || !last ? 'Reading' : 'Live';
  line.dataset.state=next;
  line.querySelectorAll('[data-status-icon]').forEach(icon=>icon.hidden=icon.dataset.statusIcon!==next);
  line.querySelector('[data-status-word]').textContent=next==='Reading' ? 'Reading…' : next;
  if(next!==state){line.querySelector('[data-status-announcement]').textContent=next;state=next;}
  const part=(key,text)=>{const node=line.querySelector('[data-status-'+key+']');node.hidden=!text;node.textContent=text;return node;};
  part('age',last ? age<60 ? age+' s ago' : Math.floor(age/60)+' min ago' : '');
  const why=next==='Paused' ? reason || busy || last?.busy : next==='Failed' ? failure : next==='Late' ? 'The feed has stopped.' : '';
  part('reason',why);
  const timings=last && !why;
  const round=part('trip',timings && trip!==null ? 'round trip '+duration(trip)+' s' : '');
  round.classList.toggle('page-status-slow',trip>options.roundTripLimit);
  const render=part('render',timings ? 'render '+duration(last.renderSeconds)+' s' : '');
  render.classList.toggle('page-status-slow',last?.renderSeconds>options.renderLimit);
  render.title=last?.renderMedian ? 'Median '+duration(last.renderMedian)+' s of the last '+last.renderCount+' renders.' : '';
 }
 function failed(error,own){if(own===request){failure=error.message || 'The read failed.';reading=false;draw();}}
 async function present(value,began,own,initial=false){
  try{
  if(!Number.isFinite(value.readAt) || !Number.isFinite(value.renderSeconds)) throw new Error('The data has no render time.');
  if(initial){last=value;if(line) line.dataset.renderSeconds=value.renderSeconds;failure=value.failure || '';draw();}
  await frame();if(own!==request) return;
  if(held()){pending={value,began,own};draw();return;}
  last=value;if(line) line.dataset.renderSeconds=value.renderSeconds;
  trip=(performance.now()-began)/1000;failure=value.failure || '';reading=false;pending=null;draw();
  }catch(error){failed(error,own);}
 }
 async function refresh(supersede=false){
  if(hidden()){draw();return;}
  if(active && !supersede) return;
  const own=++request,began=performance.now();active=true;started=began;
  clearTimeout(delay);if(!last) reading=true;
  delay=setTimeout(()=>{if(own===request){reading=true;draw();}},options.readingDelay*1000);
  draw();
  try{
   const value=await load(supersede,()=>own===request);
   if(own===request && value) await present(value,began,own);
  }catch(error){failed(error,own);}
  finally{if(own===request){active=false;clearTimeout(delay);reading=false;draw();}}
 }
 function abandon(){
  request++;active=false;clearTimeout(delay);reading=false;pending=null;draw();
 }
 function setStatus(text,reason=''){
  busy=text;failure=reason;
  if(last) last={...last,busy:text,answeredAt:Date.now()/1000};
  draw();
 }
 function changed(){
  if(pending && !held()){const value=pending;pending=null;void present(value.value,value.began,value.own);}
  draw();
 }
 function restart(){
  clearInterval(timer);clearInterval(clock);
  const period=Number(seconds());nextAt=performance.now()+period*1000;
  clock=setInterval(changed,1000);
  if(!hidden() && period>0) timer=setInterval(()=>{
   if(performance.now()>=nextAt){nextAt=performance.now()+period*1000;void refresh();}
  },period*1000);
  draw();
 }
 document.addEventListener('visibilitychange',()=>{restart();void refresh(true);});
 window.addEventListener('message',event=>{
  if(event.source===parent && event.origin===location.origin && event.data==='console-shown'){
   restart();void refresh(true);
  }
 });
 restart();
 const first=initial || (line?.dataset.readAt ? pageRefreshFacts() : null);
 if(first){const navigation=performance.getEntriesByType('navigation')[0];
  void present(first,navigation?.requestStart || started,request,true);}
 return {restart,refresh,changed,abandon,setStatus};
}
'''

CODE_BLOCK = re.compile(r"(?m)^[ \t]*```[^`\n]*\n([\s\S]*?)\n[ \t]*```[ \t]*(?=\n|$)"
                        + r"|^[ \t]*`([^`\n]+)`[ \t]*(?=\n|$)")


def code_block_html(block: re.Match[str]) -> str:
    """Build a code block with the shared copy button."""
    text = block[1] if block[1] is not None else block[2]
    return copy_code_block(text)


def copy_code_block(text: str) -> str:
    """Show plain text with the shared copy button."""
    return ('<div class="code-block"><pre><code>' + html.escape(text)
            + '</code></pre>' + COPY_BUTTON + '</div>')


def render_code_blocks(text: str, render_text: Callable[[str], str]) -> str:
    """Render code blocks and pass the text between them to the caller's renderer."""
    content: list[str] = []
    start = 0
    for block in CODE_BLOCK.finditer(text):
        before = text[start:block.start()]
        if before.strip():
            content.append(render_text(before))
        content.append(code_block_html(block))
        start = block.end()
    after = text[start:]
    if after.strip() or not content:
        content.append(render_text(after))
    return "".join(content)


BUTTON_STATES = '''
:where(button[aria-pressed=true]){--button-hover-tint:rgb(0 0 0 / 24%)}
:where(:is(button:enabled,summary,th[aria-sort][tabindex="0"]):not([aria-disabled=true]):hover,
label:hover>input:enabled+[data-button-face],tr[aria-expanded]:not([aria-disabled=true]):hover>:is(th,td)){
box-shadow:inset 0 0 0 1px color-mix(in srgb,currentColor 75%,transparent),
inset 0 0 0 100vmax var(--button-hover-tint,rgb(255 255 255 / 28%))}
:where(:is(button:enabled,summary,th[aria-sort][tabindex="0"]):not([aria-disabled=true]):active,
label:active>input:enabled+[data-button-face],tr[aria-expanded]:not([aria-disabled=true]):active>:is(th,td)){
box-shadow:inset 0 0 0 2px currentColor,
inset 0 0 0 100vmax light-dark(rgb(0 0 0 / 32%),rgb(0 0 0 / 64%))}
'''

# The title, status and content share one centered column and page background.
# A text page keeps a 1124 px column; a page whose main has the page-wide class uses the pane width.
# The heading row with its title also sets the column, so the action area script measures the final width.
# A page that measures its layout uses the desktop gutter.
STYLE = (BUTTON_STATES
         + ':root{scrollbar-gutter:stable}'
         + 'body{background:var(--bg,Canvas);color:var(--ink,CanvasText)}'
         + 'main{box-sizing:border-box;margin:0 auto;padding:32px 32px 64px}'
         + 'main:has(>.page-status,>.page-heading>h1):not(.page-wide){max-width:1124px}'
         + '.page-title{font:700 26px/1.25 system-ui,sans-serif;letter-spacing:normal;margin:0 0 8px;color:var(--ink,CanvasText)}'
         + HEADING_STYLE
         + '@media (max-width:560px){:root:not(.layout-measuring) main{padding:24px 16px 48px}}'
         + r'''
.page-status{display:flex;align-items:center;flex-wrap:wrap;gap:6px 0;font:14px/1.5 system-ui,sans-serif;
color:var(--ink-2);margin:0 0 20px;min-height:21px;background:var(--bg,Canvas)}
.page-status>[hidden],.page-status [hidden]{display:none!important}
.page-status>span:not(.page-status-announcement){white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.page-status>span:not(:first-child):not(.page-status-announcement)::before{content:'·';margin:0 10px;color:var(--ink-3)}
.page-status svg{display:block;width:16px;height:16px;flex:none}
.page-status-state{font-weight:600;color:var(--room)}
.page-status[data-state=Reading] .page-status-state{color:var(--accent)}
.page-status[data-state=Late] .page-status-state,.page-status-slow{color:var(--warning)}
.page-status[data-state=Failed] .page-status-state{color:var(--error)}
.page-status[data-state=Paused] .page-status-state{color:var(--ink-3)}
.page-status[data-state=Late],.page-status[data-state=Failed]{position:sticky;z-index:20;padding:4px 0;
top:calc(var(--page-actions-space,0px) + var(--page-actions-height,0px))}
.page-status-announcement{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
[data-status-icon=Reading] svg{animation:page-status-turn 1s linear infinite}
@keyframes page-status-turn{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){[data-status-icon=Reading] svg{animation:none}}
.review-actions{display:flex;align-items:flex-end;gap:26px;flex-wrap:wrap}
.review-action-group>span{display:block;font-size:11px;color:var(--ink-3);text-transform:uppercase;margin-bottom:5px}
.review-action-group>div{display:flex;gap:8px;flex-wrap:wrap}
.review-actions label.action-select{display:inline-flex;align-items:center;gap:8px;margin:0;font:inherit;max-width:100%}
.action-select select{box-sizing:border-box;min-height:44px;max-width:min(23ch,65vw);padding:6px 10px;
border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font:inherit}
@supports (appearance:base-select){
.action-select select,.action-select select::picker(select){appearance:base-select}
.action-select select::picker(select){max-height:20rem;overflow:auto;padding:4px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--ink)}
.action-select option{padding:5px 10px;border-radius:4px}
.action-select option:hover{background:var(--bg)}
}
.review-action-group+.review-action-group{padding-left:26px;border-left:1px solid var(--line)}
@media(max-width:960px){.review-action-group+.review-action-group{padding-left:0;border:0}}
@media(max-width:560px){.review-actions{flex-direction:column;align-items:flex-start;flex-wrap:nowrap}}
''')

class ReturningActionHeading(StrEnum):
    ITEM_RETURNS = "The item returns to you"
    SENDS_TO_SHOWRUNNER = "Sends to showrunner"


def console_dialog(dialog_id: str, title_id: str, title: str, body: str, *,
                   before_body: str = '', footer: str = '', form: bool = False, body_class: str = '') -> str:
    """Render a console dialog with its shared assets."""
    wrapper = 'form' if form else 'div'
    markup = (f'<dialog id="{dialog_id}" aria-labelledby="{title_id}" data-console-dialog>'
              f'<{wrapper} class="console-dialog-frame"><header><h2 id="{title_id}">{title}</h2>'
              '<button type="button" data-close aria-label="Close">×</button></header>'
              f'{before_body}'
              f'<div class="console-dialog-body{(" " + body_class) if body_class else ""}">'
              f'{body}</div>{footer}</{wrapper}></dialog>')
    return CONSOLE_DIALOG_ASSETS + markup


CONSOLE_DIALOG_STYLE = '''
[data-console-dialog] {
 position: fixed; inset: 0; box-sizing: border-box; margin: auto; padding: 0;
 width: min(560px, calc(100vw - 32px)); max-width: none;
 max-height: calc(100dvh - 2 * max(16px, var(--page-window-top, 16px)));
 color: var(--ink, #f0efec); background: var(--card, #1a1a19); border: 1px solid var(--line, #34332f);
 border-radius: 12px; box-shadow: 0 20px 60px #0006; overflow: hidden;
 font: 15px/1.45 system-ui, sans-serif;
}
[data-console-dialog] *, [data-console-dialog] *::before, [data-console-dialog] *::after { box-sizing: border-box; }
[data-console-dialog] [hidden] { display: none; }
[data-console-dialog]::backdrop { background: #0009; }
.console-dialog-frame { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; margin: 0;
 max-height: calc(100dvh - 2px - 2 * max(16px, var(--page-window-top, 16px))); }
.console-dialog-frame > :is(header, footer) { padding-inline: 24px; }
.console-dialog-frame > header {
 display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-block: 20px 16px;
}
.console-dialog-frame > header h2 { margin: 0; font-size: 22px; line-height: 1.2; }
.console-dialog-frame > header [data-close] {
 flex: none; min-height: 30px; margin: 0; padding: 0; cursor: pointer;
 border: 0; border-radius: 6px; background: transparent; width: 30px; height: 30px;
 color: var(--ink-2, #c3c2b7); font-size: 22px; line-height: 1;
}
.console-dialog-body {
 min-width: 0; padding: 4px 24px 20px; overflow-y: auto; overscroll-behavior: contain;
 scrollbar-width: thin; scrollbar-color: var(--ink-3, #9a988f) var(--card, #1a1a19);
}
@media (max-width: 480px) {
 .console-dialog-frame > :is(header, footer), .console-dialog-body { padding-inline: 16px; }
 .console-dialog-frame > header { padding-block: 14px 12px; }
}
'''


CONSOLE_DIALOG_SCRIPT = '''
// A dialog takes one binding. A second call for the same dialog does nothing.
function bindConsoleDialog(dialog,close){
 if(dialog.dataset.consoleDialogBound) return;
 dialog.dataset.consoleDialogBound='true';
 dialog.querySelector('[data-close]').addEventListener('click',()=>close());
 dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
 let pressedBackdrop=false;
 dialog.addEventListener('pointerdown',event=>{pressedBackdrop=event.target===dialog;});
 dialog.addEventListener('click',event=>{
  if(pressedBackdrop && event.target===dialog) close();
  pressedBackdrop=false;
 });
}
'''


CONSOLE_DIALOG_ASSETS = f'<style>{CONSOLE_DIALOG_STYLE}</style><script>{CONSOLE_DIALOG_SCRIPT}</script>'


def review_action_groups(closing_buttons: str, returning_buttons: str, later_controls: str = "", *,
                         returning_heading: ReturningActionHeading = ReturningActionHeading.ITEM_RETURNS) -> str:
    """Render the Review action groups that have controls."""
    groups = (
        ("Closes the item", closing_buttons, "", ""),
        (returning_heading, returning_buttons, "", ""),
        ("Hides it until", later_controls, " review-later", ' class="later-menu"'),
    )
    return '<div class="review-actions">' + "".join(
        f'<div class="review-action-group{group_class}"><span>{label}</span>'
        + f'<div{controls_class}>{controls}</div></div>'
        for label, controls, group_class, controls_class in groups if controls
    ) + '</div>'


def action_select(name: str, label: str, options: str) -> str:
    """Render a select next to the action that uses it."""
    return (f'<label class="action-select">{html.escape(label)}'
            + f'<select name="{html.escape(name, quote=True)}">{options}</select></label>')


COPY_BUTTON = '''<button type="button" data-copy-code aria-label="Copy code" title="Copy">
<svg data-copy-icon width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>
<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
<svg data-copied-icon hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
<path d="M20 6 9 17l-5-5"/></svg></button>'''

COPY_STYLE = '''
.code-block{position:relative;margin:16px 0}
.code-block pre{margin:0;padding-right:48px}
.code-block [data-copy-code]{position:absolute;top:6px;right:6px;display:grid;place-items:center;
box-sizing:border-box;width:28px;min-height:28px;height:28px;padding:4px;margin:0;border:0;border-radius:4px;
background:transparent;color:inherit;opacity:.55;cursor:pointer;user-select:none}
.code-block:hover [data-copy-code],.code-block [data-copy-code]:focus-visible{opacity:1}
.code-block [data-copy-code]:focus-visible{background:#8882}
[data-copy-code] [hidden]{display:none}
'''

COPY_SCRIPT = r'''(() => {
const resultMs=2000, states=new WeakMap();
function show(button,label){
 button.setAttribute('aria-label',label);button.title=label==='Copy code' ? 'Copy' : label;
 button.querySelector('[data-copy-icon]').toggleAttribute('hidden',label==='Copied');
 button.querySelector('[data-copied-icon]').toggleAttribute('hidden',label!=='Copied');
}
function copyText(text){
 const active=document.activeElement, selection=getSelection();
 const ranges=selection ? Array.from({length:selection.rangeCount},(_,index)=>selection.getRangeAt(index).cloneRange()) : [];
 const field=active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? active : null;
 const start=field?.selectionStart,end=field?.selectionEnd,direction=field?.selectionDirection;
 const area=document.createElement('textarea');area.value=text;area.readOnly=true;area.tabIndex=-1;
 area.setAttribute('aria-hidden','true');area.style.cssText='position:fixed;left:-9999px;top:0;opacity:0';
 document.body.append(area);
 try{area.select();if(!document.execCommand('copy')) throw new Error('Copy failed');}
 finally{
  area.remove();active?.focus({preventScroll:true});
  if(field && start!==null && start!==undefined && end!==null && end!==undefined) field.setSelectionRange(start,end,direction);
  if(selection){selection.removeAllRanges();ranges.forEach(range=>selection.addRange(range));}
 }
}
document.addEventListener('click',async event=>{
 const button=event.target instanceof Element ? event.target.closest('[data-copy-code]') : null;
 if(!button) return;
 const code=button.closest('.code-block')?.querySelector('pre code');
 if(!code) return;
 const state=states.get(button) || {timer:0,request:0};states.set(button,state);
 clearTimeout(state.timer);const request=++state.request;
 let label='Copied';
 try{
  if(navigator.clipboard?.writeText) await navigator.clipboard.writeText(code.textContent);
  else copyText(code.textContent);
 }catch(error){label='Copy failed';}
 if(request!==state.request) return;
 show(button,label);state.timer=setTimeout(()=>show(button,'Copy code'),resultMs);
});
})();'''

# A plain click on a data-image-zoom link shows its image over the page; Escape returns focus to that link.
# A click with a modifier key, or any other button, keeps the browser's link action.
# In a data-image-zoom-group, the left and right arrow keys and the arrow buttons show the previous
# and next image, and a badge names the shown image (Before or After for a pair).
# A button's hover tip names its key (the ◀ and ▶ keys on a Mac) and the image it shows.
# A touch screen has no hover, so a tap only moves and never shows a tip that names a key.
# The zoom keeps hrefs, not link nodes, so a page refresh while it is open changes nothing.
ZOOM_STYLE = '''
:root:has([data-image-zoom-dialog][open]){overflow:hidden;scrollbar-gutter:stable}
[data-image-zoom]{cursor:zoom-in}
[data-image-zoom-dialog]{position:fixed;inset:0;box-sizing:border-box;width:100vw;height:100dvh;
max-width:none;max-height:none;margin:0;padding:16px;border:0;background:#000d;outline:none;overflow:hidden}
[data-image-zoom-dialog][open]{display:grid;place-items:center;align-content:center;gap:8px}
[data-image-zoom-dialog] [data-image-zoom-badge]{padding:4px 12px;border-radius:8px;background:#000c;color:#fff;
font:600 14px/20px system-ui,sans-serif}
[data-image-zoom-dialog]:has([data-image-zoom-badge]:not([hidden])) img{max-height:calc(100dvh - 68px)}
[data-image-zoom-dialog]::backdrop{background:transparent}
[data-image-zoom-step]{position:fixed;top:50%;transform:translateY(-50%);width:48px;height:48px;padding:0;
display:grid;place-items:center;border:1px solid #fff4;border-radius:24px;background:#000c;color:#fff;cursor:pointer}
[data-image-zoom-step]:not([disabled]):is(:hover,:focus-visible){background:#222;border-color:#fff8}
[data-image-zoom-step][disabled]{opacity:.3;cursor:default}
[data-image-zoom-step][hidden]{display:none}
[data-image-zoom-step] svg{width:24px;height:24px}
[data-image-zoom-step="-1"]{left:24px}
[data-image-zoom-step="1"]{right:24px}
[data-image-zoom-tip]{position:absolute;top:calc(100% + 10px);padding:7px 11px;border-radius:6px;background:#e8e8e8;
color:#111;font:500 13px/18px system-ui,sans-serif;white-space:nowrap;box-shadow:0 3px 12px #0006;pointer-events:none;
visibility:hidden}
[data-image-zoom-step="-1"] [data-image-zoom-tip]{left:0}
[data-image-zoom-step="1"] [data-image-zoom-tip]{right:0}
[data-image-zoom-step]:not([disabled]):focus-visible [data-image-zoom-tip]{visibility:visible}
@media (hover:hover){[data-image-zoom-step]:not([disabled]):hover [data-image-zoom-tip]{visibility:visible}}
[data-image-zoom-tip] kbd{display:inline-block;min-width:18px;margin:0 2px;padding:0 4px;border:1px solid #999;
border-bottom-width:2px;border-radius:4px;background:#fff;font:700 16px/18px system-ui,sans-serif;text-align:center}
[data-image-zoom-dialog] img{display:block;width:auto;height:auto;max-width:100%;
max-height:calc(100dvh - 2 * max(16px, var(--page-window-top, 16px)));
border:0;border-radius:0;cursor:zoom-out}
'''

ZOOM_SCRIPT = r'''(() => {
const dialog=document.createElement('dialog'), image=document.createElement('img'), badge=document.createElement('div');
dialog.dataset.imageZoomDialog='';dialog.setAttribute('role','dialog');
dialog.setAttribute('aria-modal','true');dialog.tabIndex=-1;
badge.dataset.imageZoomBadge='';badge.setAttribute('aria-live','polite');badge.hidden=true;
const mac=/Mac/.test(navigator.userAgentData?.platform || navigator.platform);
const arrow=paths=>'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
 +'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
 +paths.map(path=>'<path d="'+path+'"/>').join('')+'</svg>';
const steps=[[-1,mac ? '◀' : '←','ArrowLeft',['m12 19-7-7 7-7','M19 12H5'],'previous'],
 [1,mac ? '▶' : '→','ArrowRight',['M5 12h14','m12 5 7 7-7 7'],'next']].map(([step,key,shortcut,paths,place])=>{
 const button=document.createElement('button'), tip=document.createElement('span'), kbd=document.createElement('kbd');
 const name=document.createTextNode('');
 button.type='button';button.dataset.imageZoomStep=String(step);button.hidden=true;
 button.setAttribute('aria-keyshortcuts',shortcut);button.innerHTML=arrow(paths);
 kbd.textContent=key;tip.dataset.imageZoomTip='';tip.setAttribute('aria-hidden','true');
 tip.append('Press ',kbd,' to show ',name);button.append(tip);return {step,place,button,name};
});
dialog.append(badge,image,...steps.map(item=>item.button));document.body.append(dialog);
let opener='', shown='', position=[0,0];
const live=href=>[...document.querySelectorAll('a[data-image-zoom]')].find(link=>link.getAttribute('href')===href);
function neighbor(step){
 const current=live(shown), group=current?.closest('[data-image-zoom-group]');
 const links=group ? [...group.querySelectorAll('a[data-image-zoom]')].filter(item=>item.querySelector('img')) : [];
 return {grouped:links.length>1, next:links[links.indexOf(current)+step]};
}
// A page refresh can change the group while the zoom is open, so each move checks the buttons again.
function syncSteps(){
 for(const {step,place,button,name} of steps){
  const {grouped,next}=neighbor(step), text=next?.dataset.imageZoomLabel || 'the '+place+' image';
  button.hidden=!grouped;button.disabled=!next;button.setAttribute('aria-label','Show '+text);name.data=text;
 }
}
function show(link){
 const thumbnail=link.querySelector('img');
 shown=link.getAttribute('href');image.alt=thumbnail.alt;image.src=link.href;
 badge.textContent=link.dataset.imageZoomLabel || '';badge.hidden=!badge.textContent;
 dialog.setAttribute('aria-label',thumbnail.alt || 'Image');
 syncSteps();
}
function move(step){
 const {next}=neighbor(step);
 if(next) show(next);else syncSteps();
 return Boolean(next);
}
function close(){
 if(!dialog.open) return;
 dialog.close();image.removeAttribute('src');
 live(opener)?.focus({preventScroll:true});
 window.scrollTo(...position);opener=shown='';
}
dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
dialog.addEventListener('keydown',event=>{
 if(event.key==='Escape'){event.preventDefault();close();return;}
 const step={ArrowLeft:-1,ArrowRight:1}[event.key];
 if(!step || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
 if(move(step)) event.preventDefault();
});
// A button that turns off at the end of the group hands focus back to the large view, so the keys keep working.
for(const {step,button} of steps) button.addEventListener('click',()=>{
 move(step);
 if(button.disabled) dialog.focus({preventScroll:true});
});
document.addEventListener('click',event=>{
 if(dialog.open && (event.target===dialog || event.target===image)){close();return;}
 if(event.defaultPrevented || event.button!==0 || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
 const link=event.target instanceof Element ? event.target.closest('a[data-image-zoom]') : null;
 const thumbnail=link?.querySelector('img');
 if(!thumbnail) return;
 event.preventDefault();position=[window.scrollX,window.scrollY];
 opener=link.getAttribute('href');show(link);
 dialog.showModal();dialog.focus({preventScroll:true});
});
})();'''


# The server holds a marked send for undo_seconds (send_hold.py).
# heldFetch shows a fading Undo toast on the top page or in its open modal dialog.
# Undo throws SendUndone. After the hold, heldFetch returns the response of the real send.
# New item and linked Review callers close their dialogs before the hold and reopen them on Undo or failure.
# The card and dialog scripts share one definition of heldFetch and SendUndone in each frame.
HELD_SEND_STYLE = '''
[data-send-undo-stack]{position:fixed;right:24px;bottom:24px;z-index:2147483647;
display:grid;justify-items:end;gap:8px;margin:0;width:max-content;max-width:calc(100% - 48px);
max-height:calc(100% - 48px);overflow-y:auto}
@media (max-width:560px){[data-send-undo-stack]{right:16px;max-width:calc(100% - 32px)}}
[data-send-undo]{position:relative;display:flex;align-items:center;gap:16px;padding:10px 10px 12px 16px;
border:1px solid var(--border,#2d3748);border-radius:8px;background:var(--nav,#141b28);color:var(--text,#e4eaf4);
box-shadow:0 4px 16px #0008;font:500 14px/20px system-ui,sans-serif;overflow:hidden;min-width:0}
[data-send-undo] span{min-width:0;overflow-wrap:anywhere}
[data-send-undo] button{flex:none;padding:4px 10px;border:0;border-radius:4px;background:none;color:var(--accent,#c2d4ff);
font:600 14px/20px system-ui,sans-serif;cursor:pointer}
[data-send-undo] button:is(:hover,:focus-visible){background:#fff2}
[data-send-undo-bar]{position:absolute;left:0;right:0;bottom:0;height:3px;background:var(--accent,#c2d4ff);
transform-origin:left}
'''

HELD_SEND_SCRIPT = (f'window.heldFetch || (() => {{\nconst SEND_HOLD_PATH={json.dumps(send_hold.HOLD_PATH)},'
                    f'SEND_UNDO_STYLE={json.dumps(HELD_SEND_STYLE)};\n') + r'''class SendUndone extends Error{
 constructor(){super('Undone. Edit and send again.');this.name='SendUndone';}
}
function sendUndoHost(){
 try{ return window.top.document; }catch{ return document; }
}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function showSendUndo(hold,seconds,receiver){
 const host=sendUndoHost(),view=host.defaultView || window;
 if(!host.querySelector('style[data-send-undo-style]')){
  const style=host.createElement('style');style.dataset.sendUndoStyle='';style.textContent=SEND_UNDO_STYLE;
  host.head.append(style);
 }
 let stack=host.querySelector('[data-send-undo-stack]');
 if(!stack){
  stack=host.createElement('div');stack.dataset.sendUndoStack='';stack.setAttribute('role','status');
 }
 // The stack moves to the top open modal dialog, or to the body, so no dialog hides it or makes it inert.
 const rehome=()=>{
  const place=host.activeElement?.closest('dialog:modal') ||
   [...host.querySelectorAll('dialog:modal')].pop() || host.body;
  if(stack.parentElement!==place) place.append(stack);
 };
 rehome();
 const watch=new MutationObserver(rehome);
 watch.observe(host.body,{subtree:true,childList:true,attributes:true,attributeFilter:['open']});
 const toast=host.createElement('div'),text=host.createElement('span'),button=host.createElement('button');
 const bar=host.createElement('div');
 const sent=receiver===null ? 'Saved.' : receiver ? 'Sent to '+receiver+'.' : 'Sent.';
 toast.dataset.sendUndo='';text.textContent=receiver || receiver===null ? sent : 'Sending.';
 button.type='button';button.textContent='Undo';
 bar.dataset.sendUndoBar='';toast.append(text,button,bar);stack.append(toast);
 // The Undo stays clear for the first half of the time, then fades out until the send leaves.
 const timing={duration:seconds*1000,easing:'linear',fill:'forwards'};
 const fade=toast.animate([{opacity:1},{opacity:1,offset:.5},{opacity:0}],timing);
 const shrink=bar.animate([{transform:'scaleX(1)'},{transform:'scaleX(0)'}],timing);
 return new Promise(resolve=>{
  let done=false;
  const remove=()=>{
   toast.remove();watch.disconnect();view.removeEventListener('pagehide',closed);removeEventListener('pagehide',closed);
   if(!stack.children.length) stack.remove();
  };
  const finish=undone=>{
   if(done) return;
   done=true;remove();resolve(undone);
  };
  const reportResult=async(response,late=false)=>{
   const result=await response?.clone().json().catch(()=>null);
   if(!late && !result?.send_failure){remove();return;}
   const summary=response?.ok && result && !result.send_failure
    ? (result.no_message ? 'Saved.' : sent)
    : result?.send_failure || result?.reason || result?.error || 'The send result is unavailable.';
   text.textContent=(late ? 'Too late to undo. ' : '')+summary;
   fade.cancel();toast.style.display='';
   pause(3000).then(remove);
  };
  // A closed console or card frame sends at once.
  const closed=()=>{
   if(done) return;
   done=true;remove();
   fetch(SEND_HOLD_PATH+'/'+hold+'/send',{method:'POST',keepalive:true}).catch(()=>{});
   // A page that comes back from the browser cache reads the result.
   resolve(false);
  };
  view.addEventListener('pagehide',closed);
  // A card frame that goes away takes its toast with it, so it sends at once too.
  if(view!==window) addEventListener('pagehide',closed);
  fade.finished.then(()=>{
   if(done) return;
   done=true;
   button.remove();bar.remove();toast.style.display='none';
   resolve(reportResult);
  },()=>{});
  button.addEventListener('click',async()=>{
   button.disabled=true;fade.pause();shrink.pause();
   let answer=null;
   for(let tries=0;tries<2 && !answer;tries++){
    try{ answer=await fetch(SEND_HOLD_PATH+'/'+hold+'/undo',{method:'POST'}); }catch{ await pause(500); }
   }
   if(answer?.ok){finish(true);return;}
   done=true;
   button.remove();bar.remove();fade.cancel();
   if(answer?.status===409){
    text.textContent='Too late to undo. Checking the result…';
    resolve(response=>reportResult(response,true));
    return;
   }
   text.textContent='Undo failed. The send can still leave.';
   resolve(false);
   pause(3000).then(remove);
  });
 });
}
async function heldFetch(url,init={},receiver){
 const headers=new Headers(init.headers);headers.set('X-Undo-Hold','1');
 const first=await fetch(url,{...init,headers});
 if(first.headers.get('X-Send-Hold')!=='held') return first;
 const {hold,seconds}=await first.json();
 const undo=await showSendUndo(hold,seconds,receiver);
 if(undo===true) throw new SendUndone();
 // A server restart drops the connection; the held send waits in its file, so read its result again.
 for(let failures=0;;){
  try{
   const response=await fetch(SEND_HOLD_PATH+'/'+hold,{cache:'no-store'});
   if(response.headers.get('X-Send-Hold')!=='pending'){
    if(typeof undo==='function') await undo(response);
    return response;
   }
  }catch(error){
   if(++failures>=30){
    if(typeof undo==='function') await undo();
    throw error;
   }
   await pause(1000);
  }
 }
}
function sendActionReceiver(form,data){
 const action=data.get('outcome');
 const verdict=action==='works' && data.get('feedback')?.trim() ? 'works-with-words' : action;
 if(action==='approve' && data.get('assignee')){
  const option=form.elements.assignee?.selectedOptions[0];
  return option?.value===data.get('assignee') ? option.dataset.sendReceiver : undefined;
 }
 return JSON.parse(form.dataset.sendLabels || '{}')[verdict];
}
window.SendUndone=SendUndone;window.heldFetch=heldFetch;window.sendActionReceiver=sendActionReceiver;
})();
'''

CARD_SUBMIT_SCRIPT = r'''function cardSubmitBlocked(form,fading=false){
 return !form.isConnected || Boolean(form.dataset.sending) || fading;
}
function setCardSubmitting(form,submitting,controls=form){
 if(submitting) form.dataset.sending='true';else delete form.dataset.sending;
 controls.querySelectorAll('button').forEach(button=>button.disabled=submitting);
}
''' + HELD_SEND_SCRIPT


@dataclass(frozen=True)
class CardMotion:
    fade_out_ms: int
    fade_in_ms: int

    @classmethod
    def read(cls, settings: conf.ConfigRead) -> CardMotion:
        return cls(settings.whole("card_fade_out_ms"), settings.whole("card_fade_in_ms"))

    def attributes(self) -> str:
        return f'data-card-fade-out-ms="{self.fade_out_ms}" data-card-fade-in-ms="{self.fade_in_ms}"'


# Complete a cancelled motion before the next motion starts.
MOTION_SCRIPT = r'''function createCardMotion(content){
 const fadeOutMs=()=>Number(content().dataset.cardFadeOutMs);
 const fadeInMs=()=>Number(content().dataset.cardFadeInMs);
 const reduced=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
 let generation=0, animations=[], commit=()=>{}, pending=Promise.resolve();
 function finish(){
  generation++;
  animations.forEach(animation=>animation.cancel());animations=[];
  const complete=commit;commit=()=>{};complete();
 }
 async function play(element,frames,duration){
  const animation=element.animate(frames,{duration,easing:'ease-out',fill:'both'});
  animations.push(animation);
  try{await animation.finished;}catch(error){}
  animation.cancel();animations=animations.filter(item=>item!==animation);
 }
 function scroll(element,always=false){
  if(element && (always || element.getBoundingClientRect().top<0))
   element.scrollIntoView({block:'start',behavior:reduced() ? 'instant' : 'smooth'});
 }
 function fadeIn(target){
  return play(target,[{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'none'}],fadeInMs());
 }
 async function fadeCard(previous,target,reveal,alwaysScroll=false){
  finish();const operation=generation;commit=reveal;
  if(reduced()){finish();scroll(target,alwaysScroll);return;}
  if(previous?.isConnected) await play(previous,[{opacity:1},{opacity:0}],fadeOutMs());
  if(operation!==generation) return;
  commit=()=>{};reveal();scroll(target,alwaysScroll);
  if(target?.isConnected) await fadeIn(target);
 }
 function switchCard(...args){pending=fadeCard(...args);return pending;}
 async function revealCard(target){
  finish();const operation=generation;
  if(reduced()){scroll(target,true);return;}
  const top=Math.min(Math.max(0,scrollY+target.getBoundingClientRect().top),
   Math.max(0,document.documentElement.scrollHeight-innerHeight));
  if(Math.abs(top-scrollY)>1) await new Promise(resolve=>{
   const done=()=>{clearTimeout(timeout);removeEventListener('scrollend',done);resolve();};
   const timeout=setTimeout(done,1000);
   addEventListener('scrollend',done,{once:true});scroll(target,true);
  });
  if(operation!==generation || !target.isConnected) return;
  await fadeIn(target);
 }
 function scrollCard(target){pending=revealCard(target);return pending;}
 async function removeCard(card,removed){
  finish();const operation=generation;
  const siblings=[...card.parentNode.children].filter(item=>item!==card && !item.hidden);
  const closeGap=()=>{
   const tops=siblings.map(item=>item.getBoundingClientRect().top);
   card.remove();removed();
   if(!reduced()) siblings.forEach((item,index)=>{
    const offset=tops[index]-item.getBoundingClientRect().top;
    if(offset) void play(item,[{transform:`translateY(${offset}px)`},{transform:'none'}],fadeInMs());
   });
  };
  commit=()=>{card.remove();removed();};
  if(!reduced()) await play(card,[{opacity:1},{opacity:0}],fadeOutMs());
  if(operation!==generation) return;
  commit=()=>{};closeGap();
 }
 return {switchCard,scrollCard,removeCard,scroll,finish,settled:()=>pending};
}
'''
