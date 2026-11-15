"""The main column, the title and the image zoom that every console page shares."""

from __future__ import annotations

# Every console page centers its main column in the frame, with the same top
# gutter, so each page title has the same place and style in its column. A page
# sets only its own max-width. A page that measures its layout puts
# layout-measuring on the root element, so it measures with the desktop gutter.
STYLE = ('main{box-sizing:border-box;margin:0 auto;padding:32px 32px 64px}'
         '.page-title{font:700 26px/1.25 system-ui,sans-serif;letter-spacing:normal;margin:0 0 16px}'
         '@media (max-width:560px){:root:not(.layout-measuring) main{padding:24px 16px 48px}}')

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
 const fadeOutMs=120, fadeInMs=180;
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
