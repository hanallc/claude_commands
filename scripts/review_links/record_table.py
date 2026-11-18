"""Share table styles, filters, sorting and open rows across console pages."""
from __future__ import annotations

import html
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote


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
    label = html.escape(name, quote=True)
    return (f'<div class="record-filter-row" role="group" aria-label="{label}" data-filter-row="{html.escape(key, quote=True)}">'
            + f'<span aria-hidden="true">{label}</span><div>' + ''.join(buttons) + '</div></div>')


def words_box(element_id: str, placeholder: str) -> str:
    """Render the words box above the filter rows."""
    identity = html.escape(element_id, quote=True)
    words = html.escape(placeholder, quote=True)
    return (f'<div class="record-filter-words"><input type="search" id="{identity}" data-filter-words'
            + f' aria-label="{words}" placeholder="{words}"></div>')


STYLE = '''<style>
[hidden]{display:none!important}body{background:var(--bg);color:var(--ink)}
main{font-size:15px;line-height:1.5}
.list-filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.list-filters label{display:inline;color:var(--ink-2)}
.list-filters select,.list-filters input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);border-radius:8px;max-width:100%;min-width:0}
.list-filters input{width:260px}[data-list-count]{margin-left:auto;color:var(--ink-2);font-size:13px}
.list-search-label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.record-filter-words{display:flex;justify-content:flex-end;margin-bottom:10px}
.record-filter-words input{font:inherit;min-height:44px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--ink);width:300px;max-width:100%;min-width:0;box-sizing:border-box}
.record-filter-row{display:grid;grid-template-columns:5rem minmax(0,1fr);align-items:baseline;gap:8px;margin-bottom:8px}
.record-filter-row>span{color:var(--ink-2);font-size:13px}
.record-filter-row>div{display:flex;flex-wrap:wrap;gap:6px}
.record-filter-row button{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:13px;min-height:36px;
color:var(--ink);border:1px solid var(--line);border-radius:999px;background:var(--card);padding:4px 10px;margin:0}
.record-filter-row button[aria-pressed=true]{background:var(--filter-selected-bg);border-color:var(--filter-selected-bg);
color:var(--filter-selected-ink)}
.record-filter-row button[aria-pressed=true] .staff-name{color:inherit!important}
.record-filter-row button[data-filter-count="0"][aria-pressed=false]{opacity:.6}
.record-filter-row button[data-filter-count="0"][aria-pressed=true]{
background:color-mix(in srgb,var(--filter-selected-bg) 75%,var(--card))}
.record-filter-row b{font-variant-numeric:tabular-nums}.record-filter-row b:empty{display:none}
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
[data-empty],[data-missing-keys]{padding:16px;margin:0;color:var(--ink-2)}
[data-missing-keys],[data-hidden-action-status]{white-space:pre-line}
[data-missing-item]{color:var(--ink-2);scroll-margin-top:16px}
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
.list-log li{grid-template-columns:105px minmax(0,1fr)}[data-list-count]{width:100%;margin-left:0}
}
@media(prefers-reduced-motion:reduce){.list-chevron{transition:none}}
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
const opened=new Set(),filterNames=options.filters || [],filters={};
const columns=[...content.querySelectorAll('[data-sort]')].map(button=>button.textContent);
const rowMotion=new Map(),actionResults=new Map();
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
   control.querySelectorAll('['+attribute+']').forEach(button=>button.addEventListener('click',()=>change(button.getAttribute(attribute))));
  }
 };
}
function place(){
 placeHash=location.hash;
 const params=new URLSearchParams(location.search);
 const fragment=new URLSearchParams(location.hash.slice(1));
 for(const key of new Set(fragment.keys())) params.delete(key);
 for(const [key,value] of fragment) params.append(key,value);
 options.place?.(params);
 keys=new Set(params.getAll('key'));
 owner=params.get('owner') || params.get('showrunner') || '';show=params.get('show') || 'open';query=params.get('q') || '';
 sort=params.get('sort') || '';direction=params.get('dir')==='desc' ? 'desc' : 'asc';
 type=params.get('type') || '';
 sourceOwner=params.get('source-owner') || '';
 for(const name of filterNames) filters[name]=params.get(name) || '';
 if(!options.shows.includes(show)) show='open';
 if(!columns.includes(sort)) sort='';
 const item=params.get('item');if(item){
  if(keys.size) keys.add(item);
  if(!['owner','showrunner','show','q','sort','dir','type','source-owner',...filterNames].some(key=>params.has(key))){
   owner='';show='all';query='';type='';for(const name of filterNames) filters[name]='';}
  if(options.single) opened.clear();opened.add(item);
 }
 const saved=history.state?.recordTable;
 linkedItem=options.missingItem ? saved?.linkedItem ?? item ?? '' : '';
 if(saved){
  opened.clear();
  saved.opened.forEach(key=>opened.add(key));
 }
 owner=options.resolveOwner?.(owner) ?? owner;
 if(sharedWords()) words().value=query;
 return saved?.top;
}
function compare(a,b){
 if(Array.isArray(a)) return a[0]-b[0] || compare(a[1],b[1]);
 return typeof a==='number' ? a-b : a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'});
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
function animateRow(table,item,showRow){
 const previous=rowMotion.get(item);
 const heightBefore=previous?.clip.getBoundingClientRect().height;
 const opacityBefore=previous ? Number(getComputedStyle(previous.clip).opacity) : Number(!showRow);
 stopMotion(item);
 const height=item.getBoundingClientRect().height+parseFloat(getComputedStyle(item).marginBottom);
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
  if(rowMotion.get(item)?.animation===animation) stopMotion(item);
 },()=>{});
 rowMotion.set(item,{spacer,clip,copy,animation,finished,sortValues:showRow ? sortValues(item.dataset) :
  previous?.sortValues ?? sortValues(item.dataset)});
}
function showHiddenActionResults(items){
 const status=content.querySelector('[data-hidden-action-status]');
 if(!status) return;
 const messages=[];
 for(const [key,{label,message}] of actionResults){
  const item=items.find(item=>item.dataset.key===key);
  if(!item || item.hidden || item.querySelector('[data-detail]').hidden) messages.push(label+': '+message);
 }
 const text=messages.join('\n');
 if(status.textContent!==text) status.textContent=text;
 status.hidden=!text;
}
function showActionResult(item,label,message){
 actionResults.set(item.dataset.key,{label,message});
 item.querySelector('[data-send-status]').textContent=message;
 apply(false,{animate:true});
}
function isSelected(item){return !keys.size || keys.has(item.dataset.key);}
function matches(item,selection,counts){
 if(!isSelected(item)) return false;
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
function apply(write=true,{animate=false,dismissMissingItem=false,pushPlace=false,top}={}){
 const motion=animate && !matchMedia('(prefers-reduced-motion: reduce)').matches;
 if(!motion) for(const item of rowMotion.keys()) stopMotion(item);
 const rows=[...content.querySelectorAll('[data-list-item]')];
 const hasKey=options.hasKey ?? (key=>rows.some(item=>item.dataset.key===key));
 const missingLinkedItem=linkedItem!=='' && !hasKey(linkedItem);
 if(missingLinkedItem){
  keys.delete(linkedItem);
  opened.delete(linkedItem);
  if(dismissMissingItem) linkedItem='';
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
 const sortValue=item=>(rowMotion.get(item)?.sortValues ?? sortValues(item.dataset))[column];
 const items=rows.toSorted((a,b)=>column<0 ? Number(a.dataset.order)-Number(b.dataset.order) :
  (compare(sortValue(a),sortValue(b)) ||
   Number(a.dataset.order)-Number(b.dataset.order))*(direction==='desc' ? -1 : 1));
 const table=content.querySelector('table');let count=0;
 if(items.some((item,index)=>item!==rows[index])) for(const item of items){
  table.append(item);
  const spacer=rowMotion.get(item)?.spacer;
  if(spacer) table.append(spacer);
 }
 items.forEach(item=>{
  const showRow=matches(item,active,counts),wasShown=!item.hidden;
  if(motion && wasShown && !showRow) animateRow(table,item,false);
  item.hidden=!showRow;if(showRow) count++;
  const expanded=opened.has(item.dataset.key),expansionChanged=setExpanded(item,expanded);
  const transition=rowMotion.get(item);
  if(motion && showRow && (!wasShown || (expansionChanged && transition))) animateRow(table,item,true);
  else if(expansionChanged && transition) setExpanded(transition.copy,expanded);
 });
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
 empty.hidden=count>0 || missing.length>0;
 const missingItem=content.querySelector('[data-missing-item]');
 if(missingItem){
  const message=linkedItem && missingLinkedItem ? options.missingItem(linkedItem) : '';
  missingItem.textContent=message;
  missingItem.hidden=!message;
 }
 const selection=keys.size ? items.filter(isSelected) : items;
 if(counts) for(const [name,values] of Object.entries(counts)) filterControl.setCounts(chipRow(name),values);
 const countLine=content.querySelector('[data-list-count]');
 if(countLine) countLine.textContent=options.counts(selection,count,{show,keys});
 showHiddenActionResults(items);
 options.applied?.(selection,{owner,show,query:search,type,sourceOwner,keys});
 if(write || owner!==previousOwner || show!==previousShow || filterNames.some(name=>filters[name]!==previousFilters[name]))
  writePlace(pushPlace,top);
}
function writePlace(push=false,top){
 const place=new URLSearchParams({owner,show,q:query,sort,dir:direction,...(type ? {type} : {}),...(sourceOwner ? {'source-owner':sourceOwner} : {}),
  ...Object.fromEntries(filterNames.filter(name=>filters[name]).map(name=>[name,filters[name]]))});
 const item=linkedItem || [...opened][0];
 if(item) place.set('item',item);
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
function jump(key){
 if(sharedWords() && query!==words().value){
  query=words().value;
  writePlace(false,scrollY);
 }else history.replaceState({recordTable:{opened:[...opened],linkedItem,top:scrollY}},'');
 const target=[...content.querySelectorAll('[data-list-item]')].find(item=>item.dataset.key===key);
 linkedItem=key;
 if(target){
  if(keys.size) keys.add(key);
  if(!matches(target,currentSelection())){
   const unfiltered=currentSelection({owner:'',show:'all',query:'',type:'',sourceOwner:'',filters:{}});
   if(owner && !matches(target,{...unfiltered,owner})) owner='';
   if(query && !matches(target,{...unfiltered,query:query.trim().toLowerCase()})) query='';
   if(type && !matches(target,{...unfiltered,type})) type='';
   if(sourceOwner && !matches(target,{...unfiltered,sourceOwner})) sourceOwner='';
   for(const name of filterNames)
    if(filters[name] && !matches(target,{...unfiltered,filters:{[name]:filters[name]}})) filters[name]='';
   if(!matches(target,currentSelection())) show='all';
   if(sharedWords()) words().value=query;
  }
  if(options.single) opened.clear();
  opened.add(key);
 }
 apply(true,{pushPlace:true});
 if(!target){
  content.querySelector('[data-missing-item]')?.scrollIntoView({block:'nearest',behavior:'instant'});
  return;
 }
 const row=target.querySelector('.list-row');
 const banner=document.querySelector('[data-page-status]');
 const bannerHeight=banner && getComputedStyle(banner).position==='sticky' ? banner.getBoundingClientRect().height : 0;
 row.style.scrollMarginTop=(bannerHeight+16)+'px';
 row.querySelector('.list-toggle').focus({preventScroll:true});
 row.scrollIntoView({block:'start',behavior:'instant'});
 row.animate([{backgroundColor:'color-mix(in srgb,var(--accent) 28%,var(--card))'},{backgroundColor:'var(--hover)'}],1200);
}
function bind(){
 const changePlace=()=>apply(true,{dismissMissingItem:true});
 const changeFilter=()=>{if(sharedWords()) query=words().value;changePlace();};
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
 content.querySelectorAll('.list-row').forEach(row=>row.addEventListener('click',event=>{
  if(event.target.closest('a') || (event.target.closest('button') && !event.target.closest('.list-toggle'))) return;
  const key=row.parentElement.dataset.key;
  linkedItem='';
  if(opened.has(key)) opened.delete(key);else {if(options.single) opened.clear();opened.add(key);}
  apply(true,{animate:true});
 }));
 content.querySelectorAll('[data-type-chip]').forEach(button=>button.addEventListener('click',()=>{
  type=button.dataset.typeChip;changePlace();
 }));
}
return {place,apply,bind,opened,restorePlace,
 startAction(item){
  actionResults.delete(item.dataset.key);
  apply(false,{animate:true});
 },
 showActionResult,
 actionNotice(item){return actionResults.get(item.dataset.key)?.message ?? '';},
 applyAction(){apply(false,{animate:true});},
 settled(){return Promise.all([...rowMotion.values()].map(motion=>motion.finished));},
 replace(){
  const previous=words(),previousQuery=query;
  content=contentProxy();
  if(sharedWords()){query=previous.value;words().value=query;}
  bind();apply(query!==previousQuery);
 }};
}
'''
