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
from scripts.review_links import setting_controls

ICONS = Path(__file__).with_name("lucide")
QUESTION_WAITING = "The {owner_role} will answer this"
OWNER_PAGES = frozenset(("accounts", "aws", "builds", "disks", "images", "list", "aws_machines", "placement_disk",
                         "rebuilds", "review"))
TITLE_ELEMENT = re.compile(rb"<h1(?=[\s>])[^>]*>.*?</h1>", re.DOTALL)
HEADING_ACTIONS = '<div class="page-heading-actions">'


class OwnerNewItem(StrEnum):
    FOR_SHOWN_OWNER = "shown"
    CHOOSE_RECIPIENT = "choose-recipient"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class NoShownOwner:
    """There is no staff name to show in the page heading."""


def owner_control(page: str, owner: staff.StaffRow | NoShownOwner, members: staff.StaffSnapshot, *,
                  recipient: str | OwnerNewItem = OwnerNewItem.FOR_SHOWN_OWNER) -> str:
    """Render the owner name and its New item button."""
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
    return f'<div class="page-owner" data-page-owner="{page}">' + name + button + '</div>'


def with_owner(content: bytes, page: str, owner: staff.StaffRow | NoShownOwner, members: staff.StaffSnapshot,
               dialog: Callable[[], str], *,
               recipient: str | OwnerNewItem = OwnerNewItem.FOR_SHOWN_OWNER) -> bytes:
    """Add the shared owner control to one served console document."""
    main = re.search(rb"<main(?=[\s>])[^>]*>", content)
    if main is None or b'data-page-owner="' in content:
        return content
    control = owner_control(page, owner, members, recipient=recipient).encode()
    actions = HEADING_ACTIONS.encode()
    heading = TITLE_ELEMENT.search(content, main.end())
    if heading is not None:
        row = b'<div class="page-heading">' + heading[0] + actions + control + b'</div></div>'
        content = content[:heading.start()] + row + content[heading.end():]
    else:
        content = content[:main.end()] + actions + control + b'</div>' + content[main.end():]
    head = re.search(rb"<head(?=[\s>])[^>]*>", content)
    if head is not None:
        content = content[:head.end()] + PAGE_SCRIPT_ERRORS.encode() + content[head.end():]
    row_style = HEADING_STYLE.encode()
    style = (OWNER_STYLE + HEADING_POSITION).encode()
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


HEADING_STYLE = ('.page-heading{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin-bottom:8px}'
                 + '.page-heading>h1{margin:0}'
                 + '.page-heading-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;'
                 + 'max-width:100%;margin-inline-start:auto}')


# The stacking order, lowest to highest: page content with its own sticky bars (z-index 3 or lower),
# the heading row (4), overlays such as tooltips, menus and the Builds paused badge (5 to 10),
# and the sticky status line (20).
OWNER_STYLE = """<style>
.page-owner{display:flex;align-items:center;gap:8px;min-width:0;font:600 14px/1.4 system-ui,sans-serif}
.page-owner-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.page-owner button{display:grid;place-items:center;flex:none;width:36px;height:36px;min-height:36px;
margin:0;padding:7px;background:var(--card,#1a1a19);color:var(--ink-2,#c3c2b7);border:1px solid var(--line,#34332f);border-radius:8px;cursor:pointer}
.page-owner svg{width:18px;height:18px}
:root{--page-heading-space:8px;--page-heading-top:var(--page-heading-space)}
:is(.page-heading,main>.page-heading-actions){position:sticky;top:var(--page-heading-space);z-index:4;
background:var(--bg,Canvas)}
:is(.page-heading,main>.page-heading-actions)[data-page-heading-stuck]{box-shadow:0 1px 0 var(--line)}
:is(.page-heading,main>.page-heading-actions)[data-page-heading-stuck]::before{content:'';position:absolute;
inset:auto 0 100%;height:var(--page-heading-space);background:inherit}
</style>"""


# The heading row sticks with a space above it. At scroll zero it stays in its normal place.
# A stuck row shows an opaque band in the space and a line under it. Neither takes layout space.
# A Late or Failed status line sticks directly below the stuck row, so the page order never changes.
HEADING_POSITION = """<script>
function stickyPageStatusHeight(){
 const banner=document.querySelector('[data-page-status]');
 return banner && getComputedStyle(banner).position==='sticky' ? banner.getBoundingClientRect().height : 0;
}
function pageHeadingSpace(){
 return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--page-heading-space'));
}
function stuckPageHeadingBottom(){
 const row=document.querySelector('.page-heading,main>.page-heading-actions');
 return pageHeadingSpace()+row.getBoundingClientRect().height+stickyPageStatusHeight();
}
document.addEventListener('DOMContentLoaded',()=>{
 const root=document.documentElement.style;
 const row=document.querySelector('.page-heading,main>.page-heading-actions');
 const banner=document.querySelector('[data-page-status]');
 const set=(name,pixels)=>{if(root.getPropertyValue(name)!==pixels+'px') root.setProperty(name,pixels+'px');};
 // The row is stuck when its sticky top clips it.
 new IntersectionObserver(entries=>row.toggleAttribute('data-page-heading-stuck',entries.at(-1).intersectionRatio<1),
  {root:document,rootMargin:-(pageHeadingSpace()+1)+'px 0px 0px 0px',threshold:1}).observe(row);
 const place=()=>{
  set('--page-status-height',stickyPageStatusHeight());
  set('--page-heading-height',row.getBoundingClientRect().height);
 };
 const resize=new ResizeObserver(place);
 resize.observe(row);
 if(banner){
  resize.observe(banner);
  // The clock writes the same state each second; only a new state can change the position.
  new MutationObserver(records=>{if(records.some(record=>record.oldValue!==banner.dataset.state)) place();})
   .observe(banner,{attributeFilter:['data-state'],attributeOldValue:true});
 }
 place();
},{once:true});
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


def action_failure(notice: str | None, message: str = "") -> str:
    identity = f' id="{html.escape(notice, quote=True)}"' if notice else ""
    hidden = " hidden" if not message else ""
    return f'<p class="action-failure"{identity} role="alert"{hidden}>{html.escape(message)}</p>'


ACTION_STATUS_SCRIPT = r'''function showActionFailure(status,message=''){
 if(status.textContent!==message) status.textContent=message;
 status.hidden=!message;
}
'''


def template(source: str, text: str, page: str) -> str:
    """Fill the shared title, style and script in a static page template."""
    return (source.replace("__PAGE_FRAME_STYLE__", STYLE).replace("__PAGE_TITLE__", title(text, page))
            .replace("__PAGE_REFRESH_SCRIPT__", REFRESH_SCRIPT)
            .replace("__ACTION_STATUS_SCRIPT__", ACTION_STATUS_SCRIPT)
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
 function setBusy(text){
  busy=text;if(text) failure='';
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
 return {restart,refresh,changed,abandon,setBusy};
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
# A page that measures its layout uses the desktop gutter.
STYLE = (BUTTON_STATES
         + ':root{scrollbar-gutter:stable}'
         + 'body{background:var(--bg,Canvas);color:var(--ink,CanvasText)}'
         + 'main{box-sizing:border-box;margin:0 auto;padding:32px 32px 64px}'
         + 'main:has(>.page-status):not(.page-wide){max-width:1124px}'
         + '.page-title{font:700 26px/1.25 system-ui,sans-serif;letter-spacing:normal;margin:0 0 8px;color:var(--ink,CanvasText)}'
         + HEADING_STYLE
         + '@media (max-width:560px){:root:not(.layout-measuring) main{padding:24px 16px 48px}}'
         + r'''
.page-status{display:flex;align-items:center;flex-wrap:wrap;gap:6px 0;font:14px/1.5 system-ui,sans-serif;
color:var(--ink-2);margin:0 0 20px;min-height:21px;background:var(--bg,Canvas)}
.action-failure{color:var(--error);white-space:pre-line;overflow-wrap:anywhere}
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
top:calc(var(--page-heading-space,0px) + var(--page-heading-height,0px))}
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
 width: min(560px, calc(100vw - 32px)); max-width: none; max-height: calc(100dvh - 32px);
 color: var(--ink, #f0efec); background: var(--card, #1a1a19); border: 1px solid var(--line, #34332f);
 border-radius: 12px; box-shadow: 0 20px 60px #0006; overflow: hidden;
 font: 15px/1.45 system-ui, sans-serif;
}
[data-console-dialog] *, [data-console-dialog] *::before, [data-console-dialog] *::after { box-sizing: border-box; }
[data-console-dialog] [hidden] { display: none; }
[data-console-dialog]::backdrop { background: #0009; }
.console-dialog-frame { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; margin: 0;
 max-height: calc(100dvh - 34px); }
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
function bindConsoleDialog(dialog,close){
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
        ("Closes the item", closing_buttons, ""),
        (returning_heading, returning_buttons, ""),
        ("Hides it until", later_controls, " review-later"),
    )
    return '<div class="review-actions">' + "".join(
        f'<div class="review-action-group{group_class}"><span>{label}</span>'
        + f'<div>{controls}</div></div>'
        for label, controls, group_class in groups if controls
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
function show(button,copied,failed=false){
 button.setAttribute('aria-label',failed ? 'Copy failed' : 'Copy code');
 button.title=failed ? 'Copy failed' : 'Copy';
 button.querySelector('[data-copy-icon]').toggleAttribute('hidden',copied);
 button.querySelector('[data-copied-icon]').toggleAttribute('hidden',!copied);
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
 let copied=false;
 try{
  if(navigator.clipboard?.writeText) await navigator.clipboard.writeText(code.textContent);
  else copyText(code.textContent);
  copied=true;
 }catch{}
 if(request!==state.request) return;
 show(button,copied,!copied);state.timer=setTimeout(()=>show(button,false),resultMs);
});
})();'''

# A plain click on a data-image-zoom link shows its image over the page; Escape returns.
# A click with a modifier key, or any other button, keeps the browser's link action.
ZOOM_STYLE = '''
:root:has([data-image-zoom-dialog][open]){overflow:hidden;scrollbar-gutter:stable}
[data-image-zoom]{cursor:zoom-in}
[data-image-zoom-dialog]{position:fixed;inset:0;box-sizing:border-box;width:100vw;height:100dvh;
max-width:none;max-height:none;margin:0;padding:16px;border:0;background:#000d;outline:none;overflow:hidden}
[data-image-zoom-dialog][open]{display:grid;place-items:center}
[data-image-zoom-dialog]::backdrop{background:transparent}
[data-image-zoom-dialog] img{display:block;width:auto;height:auto;max-width:100%;max-height:calc(100dvh - 32px);
border:0;border-radius:0;cursor:zoom-out}
'''

ZOOM_SCRIPT = r'''(() => {
const dialog=document.createElement('dialog'), image=document.createElement('img');
dialog.dataset.imageZoomDialog='';dialog.setAttribute('role','dialog');
dialog.setAttribute('aria-modal','true');dialog.tabIndex=-1;
dialog.append(image);document.body.append(dialog);
let source=null, position=[0,0];
function close(){
 if(!dialog.open) return;
 dialog.close();image.removeAttribute('src');
 if(source?.isConnected) source.focus({preventScroll:true});
 window.scrollTo(...position);source=null;
}
dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
dialog.addEventListener('keydown',event=>{
 if(event.key==='Escape'){event.preventDefault();close();}
});
document.addEventListener('click',event=>{
 if(dialog.open && (event.target===dialog || event.target===image)){close();return;}
 if(event.defaultPrevented || event.button!==0 || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return;
 const link=event.target instanceof Element ? event.target.closest('a[data-image-zoom]') : null;
 const thumbnail=link?.querySelector('img');
 if(!thumbnail) return;
 event.preventDefault();source=link;position=[window.scrollX,window.scrollY];
 image.alt=thumbnail.alt;image.src=link.href;
 dialog.setAttribute('aria-label',thumbnail.alt || 'Image');
 dialog.showModal();dialog.focus({preventScroll:true});
});
})();'''


CARD_SUBMIT_SCRIPT = r'''function cardSubmitBlocked(form,fading=false){
 return !form.isConnected || Boolean(form.dataset.sending) || fading;
}
function setCardSubmitting(form,submitting,controls=form){
 if(submitting) form.dataset.sending='true';else delete form.dataset.sending;
 controls.querySelectorAll('button').forEach(button=>button.disabled=submitting);
}
'''


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
