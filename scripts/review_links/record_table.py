"""Share table styles, filters, sorting and open rows across console pages."""
from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from scripts.lib import conf


@dataclass(frozen=True)
class Chip:
    value: str
    label_html: str
    count: int | None = None


def chip_row(name: str, chips: Iterable[Chip], selected: str) -> str:
    """Render a named filter with All as its first choice."""
    key = name.lower().replace(' ', '-')
    choices = {chip.value: chip for chip in chips}
    all_chip = choices.pop('all', Chip('all', 'All'))
    selected = selected if selected in choices else 'all'
    buttons: list[str] = []
    for chip in (all_chip, *choices.values()):
        value = html.escape(chip.value, quote=True)
        pressed = str(chip.value == selected).lower()
        count = '' if chip.count is None else str(chip.count)
        buttons.append(f'<button type="button" id="filter-{html.escape(key, quote=True)}-{quote(chip.value, safe="")}"'
                       + f' data-filter-value="{value}" aria-pressed="{pressed}" data-filter-count="{count}">'
                       + f'{chip.label_html}<b>{count}</b></button>')
    return labelled_chip_row(name, ''.join(buttons), filter_key=key)


def labelled_chip_row(name: str, buttons: str, *, filter_key: str = '', chip_classes: str = '', chip_id: str = '') -> str:
    """Render a visible label and a group of filter buttons."""
    label = html.escape(name, quote=True)
    group = f' role="group" aria-label="{label}"'
    key = f' data-filter-row="{html.escape(filter_key, quote=True)}"' if filter_key else ''
    classes = f' class="{html.escape(chip_classes, quote=True)}"' if chip_classes else ''
    identity = f' id="{html.escape(chip_id, quote=True)}"' if chip_id else ''
    return (f'<div class="record-filter-row"{group + key if filter_key else ""}>'
            + f'<span aria-hidden="true">{label}</span>'
            + f'<div{classes}{identity}{"" if filter_key else group}>{buttons}</div></div>')


def words_box(element_id: str, placeholder: str) -> str:
    """Render the words box above the filter rows."""
    identity = html.escape(element_id, quote=True)
    words = html.escape(placeholder, quote=True)
    return (f'<div class="record-filter-words"><input type="search" id="{identity}" data-filter-words'
            + f' aria-label="{words}" placeholder="{words}"></div>')


CLOCK = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')


def clock_minutes(settings: conf.ConfigRead, key: str) -> int:
    """Read a configured clock time as minutes since midnight."""
    value = settings.text(key)
    match = CLOCK.fullmatch(value)
    if match is None:
        raise ValueError(f'review_links.conf: {key}={value!r} is not a time such as 09:00')
    return int(match[1])*60+int(match[2])


def date_time_picker(*, field: str, action: str, value: str) -> str:
    """Render the date and time sheet inside an action popover."""
    return ('<div class="later-custom" data-later-custom hidden>'
            +f'<input type="hidden" name="{html.escape(field, quote=True)}">'
            +'<div class="later-month-header"><strong data-later-month aria-live="polite"></strong><div>'
            +'<button type="button" tabindex="-1" data-later-month-step="-1" aria-label="Previous month">‹</button>'
            +'<button type="button" tabindex="-1" data-later-month-step="1" aria-label="Next month">›</button></div></div>'
            +'<div role="grid" aria-label="Choose a day" data-later-grid></div>'
            +'<label class="later-time">Time<input type="text" inputmode="numeric" autocomplete="off" data-later-time></label>'
            +'<div class="later-save"><span data-later-summary aria-live="polite"></span>'
            +f'<button class="primary" name="{html.escape(action, quote=True)}" value="{html.escape(value, quote=True)}">Save</button>'
            +'</div></div>')


STYLE = '''<style>
[hidden]{display:none!important}body{background:var(--bg);color:var(--ink)}
main{font-size:15px;line-height:1.5}
.list-filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.list-filters label{display:inline;color:var(--ink-2)}
.list-filters select,.list-filters input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);border-radius:8px;max-width:100%;min-width:0}
.list-filters input{width:260px}
.list-search-label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.record-filter-words{display:flex;justify-content:flex-end;margin-bottom:10px}
.record-filter-words input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--ink);width:300px;max-width:100%;min-width:0;box-sizing:border-box}
.record-filter-row{display:grid;grid-template-columns:5rem minmax(0,1fr);align-items:baseline;gap:8px;margin-bottom:8px}
.record-filter-row>span{color:var(--ink-2);font-size:13px}
.record-filter-row>div{display:flex;flex-wrap:wrap;gap:6px}
[data-filter-row] button,.list-expansion button{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:13px;min-height:36px;
color:var(--ink);border:1px solid var(--line);border-radius:999px;background:var(--card);padding:4px 10px;margin:0}
[data-filter-row] button[aria-pressed=true]{background:var(--filter-selected-bg);border-color:var(--filter-selected-bg);
color:var(--filter-selected-ink)}
.record-filter-row button[aria-pressed=true] .staff-name{color:inherit!important}
[data-filter-row] button[data-filter-count="0"][aria-pressed=false]{opacity:.6}
[data-filter-row] button[data-filter-count="0"][aria-pressed=true]{
background:color-mix(in srgb,var(--filter-selected-bg) 75%,var(--card))}
[data-filter-row] b{font-variant-numeric:tabular-nums}[data-filter-row] b:empty{display:none}
.record-filter-row+[data-list-count]{display:block;margin:8px 0 12px;text-align:right}
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
.list-log-scroll{min-height:0;overflow-y:auto}
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
[data-later-menu] .later-time{display:flex;align-items:center;justify-content:space-between;font-size:12px;font-weight:400;margin:0 0 10px}
[data-later-menu] .later-time input{font:inherit;min-height:32px;width:120px;max-width:100%;box-sizing:border-box;padding:3px 8px;
border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);color-scheme:light dark}
[data-later-menu] .later-time input[aria-invalid=true]{border-color:var(--error);color:var(--error)}
.later-save [data-later-summary]{font-size:11px;white-space:nowrap;color:var(--ink-3)}
[data-later-menu] .later-save button{min-height:32px;padding:4px 10px;font-size:12px;flex-shrink:0}
[data-later-menu] .later-save button:disabled{opacity:.4}
</style>'''


SCRIPT = r'''const filterControl={
 buttons(row){return [...row.querySelectorAll('[data-filter-value]')];},
 setCounts(row,counts){
  for(const button of this.buttons(row)){
   const count=Object.hasOwn(counts,button.dataset.filterValue) ? counts[button.dataset.filterValue] : 0;
   button.dataset.filterCount=String(count);
   button.querySelector('b').textContent=String(count);
  }
 },
 setVisible(row,values){
  const visible=new Set(values),buttons=this.buttons(row);
  const selected=buttons.find(button=>button.getAttribute('aria-pressed')==='true');
  for(const button of buttons)
   button.hidden=button.dataset.filterValue!=='all' && !visible.has(button.dataset.filterValue);
  return this.select(row,selected?.dataset.filterValue);
 },
 select(row,value){
  const buttons=this.buttons(row);
  const selected=buttons.some(button=>!button.hidden && button.dataset.filterValue===value) ? value : 'all';
  for(const button of buttons) button.setAttribute('aria-pressed',String(button.dataset.filterValue===selected));
  return selected;
 },
 bind(control,change){
  control.addEventListener('filterchange',event=>change(event.detail));
  const emit=value=>control.dispatchEvent(new CustomEvent('filterchange',{bubbles:true,detail:value}));
  if(control.hasAttribute('data-filter-words')){
   let timer;
   control.addEventListener('input',()=>{
    clearTimeout(timer);
    timer=setTimeout(()=>{if(control.isConnected) emit(control.value);},120);
   });
  }else{
   for(const button of this.buttons(control))
    button.addEventListener('click',()=>{if(!button.hidden) emit(this.select(control,button.dataset.filterValue));});
  }
 }
};
function createRecordTable(options){
const contentProxy=()=>options.content();
let content=contentProxy(),owner='',show='open',query='',sort='',direction='asc',type='',sourceOwner='',keys=new Set();
let linkedItem='';
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
const measured=new Map(),mounted=new Map(),windowGaps=[];
const resize=typeof ResizeObserver==='function' ? new ResizeObserver(()=>scheduleWindow()) : null;
function scheduleWindow(){if(options.mount && !pendingFrame) pendingFrame=requestAnimationFrame(()=>{pendingFrame=0;drawWindow();});}
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
 if(revealKey){
  const at=entries.findIndex(({data})=>data.key===revealKey);
  if(at>=0){spacer(before,0);spacer(after,prefix.at(-1));const y=origin+prefix[at];if(y<scrollY || y>scrollY+innerHeight-120) scrollTo({top:y-16,behavior:'instant'});}
  revealKey='';
 }
 const start=Math.max(0,locate(prefix,scrollY-origin)-8);
 const end=Math.min(entries.length,start+90,Math.max(start+18,locate(prefix,scrollY-origin+innerHeight)+9));
 const focused=document.activeElement?.closest('[data-list-item]')?.dataset.key || options.focusKey?.();
 const focusAt=focused ? entries.findIndex(({data})=>data.key===focused) : -1;
 const ranges=[[start,end],...motionRanges];
 if(focusAt>=0) ranges.push([Math.max(0,focusAt-1),Math.min(entries.length,focusAt+2)]);
 const windows=mergeRanges(ranges);
 const keys=new Set(windows.flatMap(([from,to])=>entries.slice(from,to)
  .filter(entry=>entry.visible).map(({data})=>data.key)));
 for(const [key,node] of mounted){if(!keys.has(key)){options.unmount?.(node);resize?.unobserve(node);node.hidden=true;node.remove();mounted.delete(key);}}
 for(const gap of windowGaps.splice(windows.length-1)) gap.remove();
 spacer(before,prefix[windows[0][0]]);
 spacer(after,prefix.at(-1)-prefix[windows.at(-1)[1]]);
 let cursor=before;
 const placeNode=node=>{
  if(cursor.nextElementSibling!==node) cursor.after(node);
  cursor=node;
 };
 for(const [position,[from,to]] of windows.entries()){
  if(position){
   const gap=windowGaps[position-1] ??= document.createElement('tbody');
   gap.setAttribute('data-window-gap','');
   spacer(gap,prefix[from]-prefix[windows[position-1][1]]);
   placeNode(gap);
  }
  for(const {data,visible} of entries.slice(from,to)){
   if(visible) placeNode(mount(data));
   const motion=motions.get(data.key);
   if(motion) placeNode(motion.spacer);
  }
 }
 showHiddenActionResults([...mounted.values()]);
 options.mounted?.();
}
function mount(data){
 const key=data.key;
 let node=mounted.get(key);
 if(!node){node=options.mount(data);mounted.set(key,node);resize?.observe(node);}
 node.hidden=false;
 const expanded=!node.hasAttribute('data-validating-change') && opened.has(key);
 node.querySelector('[data-detail]').hidden=!expanded;
 node.querySelector('.list-toggle').setAttribute('aria-expanded',String(expanded));
 node.querySelector('.list-row').classList.toggle('open',expanded);
 return node;
}
if(options.mount){
 addEventListener('scroll',scheduleWindow,{passive:true});
 addEventListener('resize',()=>{measured.clear();scheduleWindow();});
 content.addEventListener('focusout',scheduleWindow);
}


const sortValues=data=>options.sortValues?.(data) ?? JSON.parse(data.sortValues);
const words=()=>content.querySelector('[data-filter-words]') ?? content.querySelector('#list-search');
const sharedWords=()=>words().hasAttribute?.('data-filter-words');
const chipRow=name=>content.querySelector('[data-filter-row="'+name+'"]');
function filterSelection(name){
 const row=chipRow(name),all=name==='show' ? 'all' : '';
 if(row) return {
  set(value){const selected=filterControl.select(row,value || 'all');return selected==='all' ? all : selected;},
  bind(change){filterControl.bind(row,value=>change(value==='all' ? all : value));}
 };
 const control=content.querySelector('#list-'+name),attribute='data-'+name+'-chip';
 return {
  set(value){
   control.querySelectorAll('['+attribute+']').forEach(button=>button.setAttribute('aria-pressed',String(button.getAttribute(attribute)===value)));
   return value;
  },
  bind(change){
   control.addEventListener('click',event=>{
   const button=event.target.closest('['+attribute+']');
   if(button) change(button.getAttribute(attribute));
  });
  }
 };
}
function placeParams(){
 const params=new URLSearchParams(location.search);
 const fragment=new URLSearchParams(location.hash.slice(1));
 for(const key of new Set(fragment.keys())) params.delete(key);
 for(const [key,value] of fragment) params.append(key,value);
 options.place?.(params);
 return params;
}
function place(){return readPlace(placeParams());}
function readPlace(params,followingItemLink=false){
 placeHash=location.hash;
 keys=new Set(params.getAll('key'));
 owner=params.get('owner') || params.get('showrunner') || '';show=params.get('show') || 'open';query=params.get('q') || '';
 sort=params.get('sort') || '';direction=params.get('dir')==='desc' ? 'desc' : 'asc';
 type=params.get('type') || '';
 sourceOwner=params.get('source-owner') || '';
 for(const name of filterNames) filters[name]=params.get(name) || '';
 if(!options.shows.includes(show)) show='open';
 if(!columns.includes(sort)) sort='';
 const item=params.get('item');if(item && !followingItemLink){
  if(keys.size) keys.add(item);
  if(!['owner','showrunner','show','q','sort','dir','type','source-owner',...filterNames].some(key=>params.has(key))){
   owner='';show='all';query='';type='';for(const name of filterNames) filters[name]='';}
  if(options.single) opened.clear();opened.add(item);revealKey=item;
 }
 const saved=followingItemLink ? null : history.state?.recordTable;
 linkedItem=options.missingItem ? saved?.linkedItem ?? (
  !options.persistLinkedTarget || params.get('linked')==='1' ? item ?? '' : '') : '';
 if(saved && (!options.mount || saved.top!==undefined)){
  opened.clear();
  saved.opened.forEach(key=>opened.add(key));
 }
 owner=options.resolveOwner?.(owner) ?? owner;
 if(sharedWords()) words().value=query;
 return saved?.top;
}
function followItemLink({inheritFilters=false,unappliedOnly=false,beforeJump}={}){
 if(unappliedOnly && (location.hash===placeHash || history.state?.recordTable)) return false;
 const params=placeParams();
 const jumpKey=params.get('jump'),key=jumpKey || params.get('item');
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
 jump(key,{pushPlace:false,focusTarget:Boolean(jumpKey),keepMatchingFilters:true});
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
 const detail=item.querySelector('[data-detail]'),changed=detail.hidden===expanded;
 detail.hidden=!expanded;
 item.querySelector('.list-toggle').setAttribute('aria-expanded',String(expanded));
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
 spacer.dataset.recordMotion='';spacer.inert=true;spacer.setAttribute('aria-hidden','true');
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
 const animation=clip.animate([
  {height:(heightBefore ?? (showRow ? 0 : height))+'px',opacity:opacityBefore},
  {height:(showRow ? height : 0)+'px',opacity:Number(showRow)},
 ],{duration:200,easing:'ease-in-out',fill:'both'});
 const finished=animation.finished.then(()=>{
  if(rowMotion.get(item)?.animation!==animation) return;
  stopMotion(item);
  scheduleWindow();
 },()=>{});
 rowMotion.set(item,{spacer,clip,copy,animation,finished,sortValues:showRow ? sortValues(item.dataset) :
  previous?.sortValues ?? sortValues(item.dataset)});
}
function showHiddenActionResults(items){
 const status=content.querySelector('[data-hidden-action-status]');
 if(!status) return;
 const hidden=key=>{
  const item=items.find(item=>item.dataset.key===key);
  return !item || item.hidden || isLinkedOnly(item) || item.querySelector('[data-detail]').hidden;
 };
 const messages=[];
 for(const [key,{hiddenMessage}] of actionNotices)
  if(hiddenMessage && hidden(key)) messages.push(hiddenMessage);
 if(latestActionFailure && hidden(latestActionFailure.key)) messages.push(latestActionFailure.message);
 const text=messages.join(' ');
 if(status.textContent!==text) status.textContent=text;
 status.hidden=!text;
}
/** @param {{kind:'success' | 'failure', message:string, hiddenMessage?:string}} result */
function showActionResult(item,submission,label,{kind,message,hiddenMessage}){
 const key=item.dataset.key;
 actionNotices.set(key,{message,hiddenMessage});
 if(kind==='failure' && submission===latestSubmission) latestActionFailure={key,message:label+': '+message};
 const status=item.querySelector('[data-send-status]');
 if(status) status.textContent=message;
 apply(false,{animate:true});
 const restored=mounted.get(key);
 if(kind==='failure' && restored && restored!==item && !matchMedia('(prefers-reduced-motion: reduce)').matches){
  animateRow(content.querySelector('table'),restored,true,item);
 }
}
function isSelected(item){return !keys.size || keys.has(item.dataset.key);}
function isLinkedOnly(item){return options.linkedOnly?.(item.dataset);}
function matches(item,selection,counts){
 if(isLinkedOnly(item) || !isSelected(item)) return false;
 if(!options.filterValues) return options.visible(item.dataset,selection);
 const values=options.filterValues(item.dataset,selection.query);
 if(!values) return false;
 const selected={show:selection.show,owner:selection.owner,...selection.filters};
 const excluded=Object.entries(values).filter(([name,choices])=>
  selected[name] && selected[name]!=='all' && !choices.includes(selected[name]));
 // A record counts for a row when it matches every other row.
 if(counts && excluded.length<2){
  for(const [name,choices] of Object.entries(values)){
   if(excluded.length && excluded[0][0]!==name) continue;
   for(const value of ['all',...choices]) counts[name][value]=(counts[name][value] ?? 0)+1;
  }
 }
 return excluded.length===0;
}
function currentSelection(overrides={}){
 return {owner,show,query:query.trim().toLowerCase(),type,sourceOwner,keys,filters:{...filters},...overrides};
}
function apply(write=true,layout={}){
 options.beforeApply?.();
 applyRows(write,layout);
}
function applyRows(write,{animate=false,dismissMissingItem=false,pushPlace=false,top}={}){
 const motion=animate && !matchMedia('(prefers-reduced-motion: reduce)').matches;
 if(!motion) for(const item of rowMotion.keys()) stopMotion(item);
 const rows=options.entries ? options.entries().map(dataset=>({dataset})) : [...content.querySelectorAll('[data-list-item]')];
 const hasKey=options.hasKey ?? (key=>rows.some(item=>item.dataset.key===key));
 const missingLinkedItem=linkedItem!=='' && !hasKey(linkedItem);
 if(missingLinkedItem){
  keys.delete(linkedItem);
  opened.delete(linkedItem);
  if(dismissMissingItem) linkedItem='';
 }
 if(dismissMissingItem && rows.some(item=>item.dataset.key===linkedItem && isLinkedOnly(item))){
  opened.delete(linkedItem);
  linkedItem='';
 }
 const previousOwner=owner,previousShow=show,previousFilters={...filters};
 owner=options.resolveOwner?.(owner) ?? owner;
 const search=query.trim().toLowerCase();
 owner=options.selectOwner?.({owner,type,query:search,keys}) ?? owner;
 owner=filterSelection('owner').set(owner);
 if(previousOwner && !owner) sourceOwner='';
 show=filterSelection('show').set(show);
 if(!sharedWords()) words().value=query;
 for(const name of filterNames)
  filters[name]=filterSelection(name).set(filters[name]);
 const active=currentSelection();
 const counts=options.filterValues ? Object.fromEntries(['show','owner',...filterNames].map(name=>[name,Object.create(null)])) : undefined;
 const column=columns.indexOf(sort);
 const windowMotions=options.mount && rowMotion.size ? motionsByKey() : null;
 const sortValue=item=>{
  const transition=windowMotions ? windowMotions.get(item.dataset.key) : rowMotion.get(item);
  return (transition?.sortValues ?? sortValues(item.dataset))[column];
 };
 const items=rows.toSorted((a,b)=>column<0 ? Number(a.dataset.order)-Number(b.dataset.order) :
  (compare(sortValue(a),sortValue(b)) ||
   Number(a.dataset.order)-Number(b.dataset.order))*(direction==='desc' ? -1 : 1));
 const table=content.querySelector('table');let count=0,linkedVisible=false;
 if(options.mount){
  const visibleKeys=new Set(items.filter(item=>matches(item,active,counts)).map(item=>item.dataset.key));
  count=visibleKeys.size;
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
  if(items.some((item,index)=>item!==rows[index])) for(const item of items){
   table.append(item);
   const spacer=rowMotion.get(item)?.spacer;
   if(spacer) table.append(spacer);
  }
  items.forEach(item=>{
   const expanded=opened.has(item.dataset.key);
   const linked=expanded && item.dataset.key===linkedItem && isLinkedOnly(item);
   const matched=matches(item,active,counts),showRow=matched || linked,wasShown=!item.hidden;
   if(motion && wasShown && !showRow) animateRow(table,item,false);
   item.hidden=!showRow;if(matched) count++;
   if(linked) linkedVisible=true;
   const expansionChanged=setExpanded(item,expanded);
   const transition=rowMotion.get(item);
   if(motion && showRow && (!wasShown || (expansionChanged && transition))) animateRow(table,item,true);
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
  notice.textContent=missing.map(key=>key+' is no longer on the '+table.getAttribute('aria-label')+' page.').join('\n');
  notice.hidden=!missing.length;
 }
 empty.hidden=count>0 || linkedVisible || missing.length>0;
 const missingItem=content.querySelector('[data-missing-item]');
 if(missingItem){
  const message=linkedItem && missingLinkedItem ? options.missingItem(linkedItem) : '';
  missingItem.textContent=message;
  missingItem.hidden=!message;
 }
 const group=content.querySelector('[data-clear-linked-reviews]');
 if(group) group.hidden=!keys.size;
 const selection=items.filter(item=>!isLinkedOnly(item) && isSelected(item));
 if(counts) for(const [name,values] of Object.entries(counts)) filterControl.setCounts(chipRow(name),values);
 const countLine=content.querySelector('[data-list-count]');
 if(countLine) countLine.textContent=options.counts(selection,count,{show,keys});
 if(!options.mount) showHiddenActionResults(items);
 options.applied?.(selection,{owner,show,query:search,type,sourceOwner,keys});
 if(write || location.hash===placeHash &&
  (owner!==previousOwner || show!==previousShow || filterNames.some(name=>filters[name]!==previousFilters[name])))
  writePlace(pushPlace,top);
}
function writePlace(push=false,top){
 const place=new URLSearchParams({owner,show,q:query,sort,dir:direction,...(type ? {type} : {}),...(sourceOwner ? {'source-owner':sourceOwner} : {}),
  ...Object.fromEntries(filterNames.filter(name=>filters[name]).map(name=>[name,filters[name]]))});
 const item=linkedItem || [...opened][0];
 if(item) place.set('item',item);
 if(options.persistLinkedTarget && linkedItem) place.set('linked','1');
 keys.forEach(key=>place.append('key',key));
 const hash='#'+place,state={recordTable:{opened:[...opened],linkedItem,top}};
 if(push && hash!==location.hash) history.pushState(state,'',location.pathname+hash);
 else history.replaceState(state,'',location.pathname+hash);
 placeHash=location.hash;
 if(parent!==window) parent.postMessage('console-place',location.origin);
}
function restorePlace(event){
 // A changed fragment sends hashchange after popstate.
 if(event.type==='popstate' && location.hash!==placeHash) return;
 const top=place();
 apply(false,{top});
 if(Number.isFinite(top)) scrollTo({top,behavior:'instant'});
}
function saveDeparturePlace(){
 if(sharedWords() && query!==words().value){
  query=words().value;
  writePlace(false,scrollY);
 }else history.replaceState({recordTable:{opened:[...opened],linkedItem,top:scrollY}},'');
}
function jump(key,{pushPlace=true,focusTarget=true,keepMatchingFilters=false}={}){
 if(pushPlace) saveDeparturePlace();
 const target=options.entries ? options.entries().map(dataset=>({dataset})).find(item=>item.dataset.key===key) :
  [...content.querySelectorAll('[data-list-item]')].find(item=>item.dataset.key===key);
 linkedItem=options.missingItem ? key : '';
 if(target){
  if(keys.size) keys.add(key);
  if(options.single) opened.clear();
  opened.add(key);
  if(!isLinkedOnly(target) && !matches(target,currentSelection())){
   const unfiltered=currentSelection({owner:'',show:'all',query:'',type:'',sourceOwner:'',filters:{}});
   if(!keepMatchingFilters || owner && !matches(target,{...unfiltered,owner})) owner='';
   if(!keepMatchingFilters || sourceOwner && !matches(target,{...unfiltered,sourceOwner})) sourceOwner='';
   if(!keepMatchingFilters || show!=='all' && !matches(target,{...unfiltered,show})) show='all';
   if(query && !matches(target,{...unfiltered,query:query.trim().toLowerCase()})) query='';
   if(type && !matches(target,{...unfiltered,type})) type='';
   for(const name of filterNames)
    if(filters[name] && !matches(target,{...unfiltered,filters:{[name]:filters[name]}})) filters[name]='';
   if(sharedWords()) words().value=query;
  }
 }
 if(options.mount && target) revealKey=key;
 apply(true,{pushPlace});
 const mountedTarget=options.mount ? mounted.get(key) : target;
 if(!mountedTarget){
  content.querySelector('[data-missing-item]')?.scrollIntoView({block:'nearest',behavior:'instant'});
  return;
 }
 const row=mountedTarget.querySelector('.list-row');
 if(mountedTarget.hidden || !focusTarget) return;
 highlightedRow?.removeAttribute('data-record-highlight');
 clearTimeout(highlightTimer);
 const banner=document.querySelector('[data-page-status]');
 const bannerHeight=banner && getComputedStyle(banner).position==='sticky' ? banner.getBoundingClientRect().height : 0;
 row.style.scrollMarginTop=(bannerHeight+16)+'px';
 row.querySelector('.list-toggle').focus({preventScroll:true});
 row.scrollIntoView({block:'start',behavior:'instant'});
 row.dataset.recordHighlight='';
 highlightedRow=row;
 highlightTimer=setTimeout(()=>row.removeAttribute('data-record-highlight'),1200);
}
function setRowsExpanded(items,expanded){
 for(const item of items){
  const key=item.dataset.key;
  if(opened.has(key)===expanded) continue;
  linkedItem='';
  if(expanded){
   if(options.single) opened.clear();
   opened.add(key);
  }else opened.delete(key);
  measured.delete(key);
 }
 apply(true,{animate:true});
}
function bind(){
 if(boundRoots.has(content)) return;boundRoots.add(content);
 const changePlace=()=>apply(true,{dismissMissingItem:true});
 const changeFilter=()=>{if(sharedWords()) query=words().value;changePlace();};
 content.querySelector('[data-clear-linked-reviews]')?.addEventListener('click',()=>{
  keys.clear();changeFilter();words().focus({preventScroll:true});
 });
 content.addEventListener('click',event=>{
  const link=event.target.closest('a[data-record-jump]');
  if(!link || event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const key=new URLSearchParams(link.hash.slice(1)).get('item');
  if(!key) return;
  event.preventDefault();
  jump(key);
 });
 filterSelection('owner').bind(value=>{if(value===owner) return;owner=value;sourceOwner='';changeFilter();});
 filterSelection('show').bind(value=>{if(value===show) return;show=value;changeFilter();});
 for(const name of filterNames)
  filterSelection(name).bind(value=>{filters[name]=value;changeFilter();});
 if(sharedWords()) filterControl.bind(words(),value=>{if(value!==query){query=value;changePlace();}});
 else words().addEventListener('input',event=>{query=event.target.value;changePlace();});
 content.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{
  const selected=columns[Number(button.dataset.sort)];direction=sort===selected && direction==='asc' ? 'desc' : 'asc';
  sort=selected;changePlace();
 }));
 content.querySelector('table').addEventListener('click',event=>{
  const row=event.target.closest('.list-row');if(!row) return;
  if(event.target.closest('a') || (event.target.closest('button') && !event.target.closest('.list-toggle'))) return;
  const item=row.parentElement;
  setRowsExpanded([item],!opened.has(item.dataset.key));
 });
 content.querySelectorAll('[data-type-chip]').forEach(button=>button.addEventListener('click',()=>{
  type=button.dataset.typeChip;changePlace();
 }));
}
return {place,apply,bind,opened,restorePlace,drawWindow,followItemLink,saveDeparturePlace,get linkedItem(){return linkedItem;},
 setShownExpanded(expanded){
  const currentQuery=words().value;
  if(query!==currentQuery){
   query=currentQuery;
   apply(false,{dismissMissingItem:true});
  }
  setRowsExpanded(content.querySelectorAll('[data-list-item]:not([hidden])'),expanded);
 },
 closeCard(key){
  const wasOpen=opened.delete(key);
  if(linkedItem===key) linkedItem='';
  else if(!wasOpen) return;
  if(location.hash===placeHash) writePlace();
 },
 startAction(item){
  const submission=++latestSubmission;
  latestActionFailure=null;
  actionNotices.delete(item.dataset.key);
  apply(false,{animate:true});
  return submission;
 },
 showActionResult,
 actionNotice(item){return actionNotices.get(item.dataset.key)?.message ?? '';},
 applyAction(){apply(false,{animate:true});},
 async rowSettled(item){while(rowMotion.has(item)) await rowMotion.get(item).finished;},
 settled(){return Promise.all([...rowMotion.values()].map(motion=>motion.finished));},
 replace(draw=true){
  const previous=words(),previousQuery=query;
  for(const gap of windowGaps.splice(0)) gap.remove();
  for(const node of mounted.values()){resize?.unobserve(node);options.unmount?.(node);node.remove();}
  mounted.clear();measured.clear();content=contentProxy();
  if(sharedWords()){query=previous.value;words().value=query;}
  bind();if(draw) applyRows(query!==previousQuery);
 }};
}
'''


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
function closeDateTimePicker(menu,focus){
 menu.hidden=true;menu.querySelector('[data-later-custom]').hidden=true;delete menu.dataset.choosing;
 const toggle=menu.parentElement.querySelector('[data-later-toggle]');toggle.setAttribute('aria-expanded','false');
 if(focus) toggle.focus();
}
function bindDateTimePicker(form,{zone,defaultMinutes,summaryPrefix,prepareMenu=()=>{},retryTime=()=>NaN}){
 const toggle=form.querySelector('[data-later-toggle]'),menu=form.querySelector('[data-later-menu]');
 const custom=menu.querySelector('[data-later-custom]'),chooseItem=menu.querySelector('[data-later-choose]');
 const items=()=>[...menu.querySelectorAll('[role=menuitem]:not([hidden])')];
 toggle.addEventListener('click',()=>{
  if(!menu.hidden){closeDateTimePicker(menu,true);return;}
  prepareMenu(menu);
  menu.hidden=false;toggle.setAttribute('aria-expanded','true');
  if(chooseItem) items()[0].focus();
  else choose();
 });
 const grid=custom.querySelector('[data-later-grid]'),time=custom.querySelector('[data-later-time]');
 const focusCalendar=()=> (grid.querySelector('[tabindex="0"]') ?? time).focus();
 const save=custom.querySelector('.later-save button'),month=custom.querySelector('[data-later-month]');
 const previousMonth=custom.querySelector('[data-later-month-step="-1"]');
 const field=custom.querySelector('input[type="hidden"]');
 const dayText=date=>date.toISOString().slice(0,10);
 const monthText=new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'long',year:'numeric'});
 const dayLabel=new Intl.DateTimeFormat('en-US',{timeZone:'UTC',dateStyle:'full'});
 const summary=new Intl.DateTimeFormat('en-GB',{timeZone:zone,weekday:'short',day:'numeric',month:'short'});
 let selected=new Date(),display=new Date();
 const today=()=>{const now=zoneParts(zone,Date.now());return new Date(Date.UTC(now.year,now.month-1,now.day));};
 const update=(normalize=false)=>{
  const minutes=dateTimeMinutes(time.value),valid=Number.isFinite(minutes);
  const clock=valid ? dateTimeClock(minutes) : '';
  time.setAttribute('aria-invalid',String(!valid));
  if(valid && normalize) time.value=clock;
  field.value=valid ? dayText(selected)+'T'+clock : '';
  field.dispatchEvent(new Event('input',{bubbles:true}));
  const retry=retryTime();
  const at=Number.isFinite(retry) ? retry : valid ? dateTimeEpoch(zone,selected,minutes) : NaN;
  const resolved=Number.isFinite(at) ? zoneParts(zone,at) : null;
  custom.querySelector('[data-later-summary]').textContent=resolved ? summaryPrefix+summary.format(at)+' '+
   dateTimeClock(resolved.hour*60+resolved.minute) : 'Choose a time';
  save.disabled=!Number.isFinite(retry) && !(at>Date.now());
 };
 const draw=()=>{
  const current=today(),currentText=dayText(current),selectedText=dayText(selected);
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
  const draft=parseDateTimeDraft(field.value);
  if(draft && (Number.isFinite(retryTime()) || dateTimeEpoch(zone,draft.date,draft.minutes)>Date.now())){
   selected=draft.date;time.value=dateTimeClock(draft.minutes);
  }else{
   selected=today();selected.setUTCDate(selected.getUTCDate()+1);
   time.value=dateTimeClock(defaultMinutes());
  }
  const current=today();
  display=new Date(selected<current ? current : selected);
  custom.hidden=false;menu.dataset.choosing='true';draw();
  focusCalendar();
 }
 chooseItem?.addEventListener('click',choose);
 custom.querySelectorAll('[data-later-month-step]').forEach(button=>button.addEventListener('click',()=>{
  const hadFocus=document.activeElement===button;
  display=new Date(Date.UTC(display.getUTCFullYear(),display.getUTCMonth()+Number(button.dataset.laterMonthStep),1));
  draw();
  if(hadFocus && button.disabled) focusCalendar();
 }));
 time.addEventListener('input',()=>update());
 time.addEventListener('blur',()=>update(true));
 time.addEventListener('keydown',event=>{
  const step={ArrowUp:15,ArrowDown:-15}[event.key];
  if(step===undefined) return;
  event.preventDefault();
  const minutes=dateTimeMinutes(time.value);
  if(!Number.isFinite(minutes)) return;
  time.value=dateTimeClock((minutes+step+1440)%1440);update();
 });
 grid.addEventListener('keydown',event=>{
  const days={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7}[event.key];
  const day=event.target.dataset.date;
  if(days===undefined || !day) return;
  event.preventDefault();
  const date=new Date(day+'T00:00:00Z');
  date.setUTCDate(date.getUTCDate()+days);
  if(date<today()) return;
  selected=date;display=new Date(date);draw();focusCalendar();
 });
 menu.addEventListener('keydown',event=>{
  if(event.key==='Escape'){event.preventDefault();closeDateTimePicker(menu,true);return;}
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
 document.querySelectorAll('[data-later-menu]:not([hidden])').forEach(menu=>{
  if(!menu.parentElement.contains(event.target)) closeDateTimePicker(menu,false);
 });
});
function dateTimePickerOpen(content){
 return [...content.querySelectorAll('[data-later-menu]:not([hidden])')].some(menu=>menu.getClientRects().length);
}
'''
