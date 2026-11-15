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
