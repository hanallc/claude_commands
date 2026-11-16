"""The main column, the title and the image zoom that every console page shares."""

from __future__ import annotations

import html
import re

CODE_BLOCK = re.compile(r"(?m)^[ \t]*```[^`\n]*\n([\s\S]*?)\n[ \t]*```[ \t]*(?=\n|$)"
                        + r"|^[ \t]*`([^`\n]+)`[ \t]*(?=\n|$)")


def code_block_html(block: re.Match[str]) -> str:
    """Build a code block with the shared copy button."""
    text = block[1] if block[1] is not None else block[2]
    return ('<div class="code-block"><pre><code>' + html.escape(text)
            + '</code></pre>' + COPY_BUTTON + '</div>')


# Every console page centers its main column in the frame, with the same top
# gutter, so each page title has the same place and style in its column. A page
# sets only its own max-width. A page that measures its layout puts
# layout-measuring on the root element, so it measures with the desktop gutter.
STYLE = ('main{box-sizing:border-box;margin:0 auto;padding:32px 32px 64px}'
         '.page-title{font:700 26px/1.25 system-ui,sans-serif;letter-spacing:normal;margin:0 0 16px}'
         '@media (max-width:560px){:root:not(.layout-measuring) main{padding:24px 16px 48px}}')

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
.code-block [data-copy-code]:hover,.code-block [data-copy-code]:focus-visible{background:#8882}
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

# Both pages use these durations and complete a cancelled motion before the next motion.
MOTION_SCRIPT = r'''function createCardMotion(){
 const fadeOutMs=180, fadeInMs=270;
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
  return play(target,[{opacity:0,transform:'translateY(4px)'},{opacity:1,transform:'none'}],fadeInMs);
 }
 async function fadeCard(previous,target,reveal,alwaysScroll=false){
  finish();const operation=generation;commit=reveal;
  if(reduced()){finish();scroll(target,alwaysScroll);return;}
  if(previous?.isConnected) await play(previous,[{opacity:1},{opacity:0}],fadeOutMs);
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
    if(offset) void play(item,[{transform:`translateY(${offset}px)`},{transform:'none'}],fadeInMs);
   });
  };
  commit=()=>{card.remove();removed();};
  if(!reduced()) await play(card,[{opacity:1},{opacity:0}],fadeOutMs);
  if(operation!==generation) return;
  commit=()=>{};closeGap();
 }
 return {switchCard,scrollCard,removeCard,scroll,finish,settled:()=>pending};
}
'''
