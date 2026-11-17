"""Share table styles, filters, sorting and open rows across console pages."""
from __future__ import annotations

STYLE = '''<style>
[hidden]{display:none!important}body{background:var(--bg);color:var(--ink)}
main{font-size:15px;line-height:1.5}
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
[data-missing-keys]{white-space:pre-line}
[data-missing-item]{color:var(--ink-2);scroll-margin-top:16px}
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


SCRIPT = r'''function createRecordTable(options){
const contentProxy=()=>options.content();
let content=contentProxy(),owner='',show='open',query='',sort='',direction='asc',type='',sourceOwner='',keys=new Set();
let linkedItem='';
let placeHash=location.hash;
const opened=new Set(),filterNames=options.filters || [],filters={};
const columns=[...content.querySelectorAll('[data-sort]')].map(button=>button.textContent);
function filterControl(id,attribute){
 const control=content.querySelector(id);
 return {
  set(value){
   if(control.matches('select')){control.value=value;return control.value;}
   control.querySelectorAll('['+attribute+']').forEach(button=>button.setAttribute('aria-pressed',String(button.getAttribute(attribute)===value)));
   return value;
  },
  bind(change){
   if(control.matches('select')) control.addEventListener('change',event=>change(event.target.value));
   else control.querySelectorAll('['+attribute+']').forEach(button=>button.addEventListener('click',()=>change(button.getAttribute(attribute))));
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
}
function compare(a,b){
 if(Array.isArray(a)) return a[0]-b[0] || compare(a[1],b[1]);
 return typeof a==='number' ? a-b : a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'});
}
function isSelected(item){return !keys.size || keys.has(item.dataset.key);}
function matches(item,selection={}){
 return isSelected(item) &&
  options.visible(item.dataset,{owner,show,query:query.trim().toLowerCase(),type,sourceOwner,keys,filters:{...filters},...selection});
}
function apply(write=true,{dismissMissingItem=false,pushPlace=false}={}){
 const items=[...content.querySelectorAll('[data-list-item]')];
 const hasKey=options.hasKey ?? (key=>items.some(item=>item.dataset.key===key));
 const missingLinkedItem=linkedItem!=='' && !hasKey(linkedItem);
 if(missingLinkedItem){
  keys.delete(linkedItem);
  opened.delete(linkedItem);
  if(dismissMissingItem) linkedItem='';
 }
 const previousOwner=owner;
 owner=options.resolveOwner?.(owner) ?? owner;
 const search=query.trim().toLowerCase();
 owner=options.selectOwner?.({owner,type,query:search,keys}) ?? owner;
 if(previousOwner && !owner) sourceOwner='';
 filterControl('#list-owner','data-owner-chip').set(owner);
 filterControl('#list-show','data-show-chip').set(show);
 content.querySelector('#list-search').value=query;
 for(const name of filterNames)
  filters[name]=filterControl('#list-'+name,'data-'+name+'-chip').set(filters[name]);
 const column=columns.indexOf(sort);
 items.sort((a,b)=>column<0 ? Number(a.dataset.order)-Number(b.dataset.order) :
  (compare((options.sortValues?.(a.dataset) ?? JSON.parse(a.dataset.sortValues))[column],
           (options.sortValues?.(b.dataset) ?? JSON.parse(b.dataset.sortValues))[column]) ||
   Number(a.dataset.order)-Number(b.dataset.order))*(direction==='desc' ? -1 : 1));
 const table=content.querySelector('table');let count=0;
 items.forEach(item=>{
  item.hidden=!matches(item);if(!item.hidden) count++;
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
 const countLine=content.querySelector('[data-list-count]');
 if(countLine) countLine.textContent=options.counts(selection,count,{show,keys});
 options.applied?.(selection,{owner,show,query:search,type,sourceOwner,keys});
 if(write || owner!==previousOwner) writePlace(pushPlace);
}
function writePlace(push=false){
 const place=new URLSearchParams({owner,show,q:query,sort,dir:direction,...(type ? {type} : {}),...(sourceOwner ? {'source-owner':sourceOwner} : {}),
  ...Object.fromEntries(filterNames.filter(name=>filters[name]).map(name=>[name,filters[name]]))});
 const item=linkedItem || [...opened][0];
 if(item) place.set('item',item);
 keys.forEach(key=>place.append('key',key));
 const hash='#'+place,state={recordTable:{opened:[...opened],linkedItem}};
 if(push && hash!==location.hash) history.pushState(state,'',location.pathname+hash);
 else history.replaceState(state,'',location.pathname+hash);
 placeHash=location.hash;
 if(parent!==window) parent.postMessage('console-place',location.origin);
}
function restorePlace(event){
 // A changed fragment sends hashchange after popstate.
 if(event.type==='popstate' && location.hash!==placeHash) return;
 place();
 apply(false);
 const top=history.state?.recordTable?.top;
 if(Number.isFinite(top)) scrollTo({top,behavior:'instant'});
}
function jump(key){
 history.replaceState({recordTable:{opened:[...opened],linkedItem,top:scrollY}},'');
 const target=[...content.querySelectorAll('[data-list-item]')].find(item=>item.dataset.key===key);
 linkedItem=key;
 if(target){
  if(keys.size) keys.add(key);
  if(!matches(target)){
   const unfiltered={owner:'',show:'all',query:'',type:'',sourceOwner:'',filters:{}};
   if(owner && !matches(target,{...unfiltered,owner})) owner='';
   if(query && !matches(target,{...unfiltered,query:query.trim().toLowerCase()})) query='';
   if(type && !matches(target,{...unfiltered,type})) type='';
   if(sourceOwner && !matches(target,{...unfiltered,sourceOwner})) sourceOwner='';
   for(const name of filterNames)
    if(filters[name] && !matches(target,{...unfiltered,filters:{[name]:filters[name]}})) filters[name]='';
   if(!matches(target)) show='all';
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
 content.addEventListener('click',event=>{
  const link=event.target.closest('a[data-record-jump]');
  if(!link || event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const key=new URLSearchParams(link.hash.slice(1)).get('item');
  if(!key) return;
  event.preventDefault();
  jump(key);
 });
 filterControl('#list-owner','data-owner-chip').bind(value=>{if(value===owner) return;owner=value;sourceOwner='';changePlace();});
 filterControl('#list-show','data-show-chip').bind(value=>{if(value===show) return;show=value;changePlace();});
 for(const name of filterNames)
  filterControl('#list-'+name,'data-'+name+'-chip').bind(value=>{filters[name]=value;changePlace();});
 content.querySelector('#list-search').addEventListener('input',event=>{query=event.target.value;changePlace();});
 content.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{
  const selected=columns[Number(button.dataset.sort)];direction=sort===selected && direction==='asc' ? 'desc' : 'asc';
  sort=selected;changePlace();
 }));
 content.querySelectorAll('.list-row').forEach(row=>row.addEventListener('click',event=>{
  if(event.target.closest('a') || (event.target.closest('button') && !event.target.closest('.list-toggle'))) return;
  const key=row.parentElement.dataset.key;
  linkedItem='';
  if(opened.has(key)) opened.delete(key);else {if(options.single) opened.clear();opened.add(key);}
  apply();
 }));
 content.querySelectorAll('[data-type-chip]').forEach(button=>button.addEventListener('click',()=>{
  type=button.dataset.typeChip;changePlace();
 }));
}
return {place,apply,bind,opened,restorePlace,replace(){content=contentProxy();bind();apply(false);}};
}
'''
