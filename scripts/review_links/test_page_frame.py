"""The shared page status keeps state, age and request timing together."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast, override
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.lib.process_group import run_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.review_links import change_page, list_page, page_frame, record_table, review_items, review_page, review_submissions, serve, staff_names
from scripts.review_links.browser_test_support import STATUS_PAGES, firefox_probe_results, status_configuration

SCRIPTS = Path(__file__).resolve().parents[1]
PAGE_OWNER = staff.StaffRow(staff.StaffId("owner"), "showrunner", "Page owner", staff.STAFF_ROOT,
                            current_session=staff.StaffId("session-owner"))


class CodeBlockTests(unittest.TestCase):
    def test_fenced_code_ends_at_its_first_closing_line(self) -> None:
        for line_end in ("\n", "\r\n"):
            with self.subTest(line_end=line_end):
                code = "  printf x" + line_end * 2
                adjacent = line_end.join(("```sh", "```", "```sh", code, "```"))
                self.assertIsNone(page_frame.CODE_BLOCK.fullmatch(adjacent))
                self.assertEqual(page_frame.render_code_blocks(adjacent, self.fail),
                                 page_frame.copy_code_block("") + page_frame.copy_code_block(code))

    def test_one_fence_fullmatches_with_lf_and_crlf(self) -> None:
        for line_end in ("\n", "\r\n"):
            for code in ("", "  printf x" + line_end + "printf y  " + line_end):
                with self.subTest(line_end=line_end, code=code):
                    lines = ("```sh", code, "```") if code else ("```sh", "```")
                    match = page_frame.CODE_BLOCK.fullmatch(line_end.join(lines))
                    if match is None:
                        self.fail("The code fence must match the complete text.")
                    self.assertEqual(page_frame.code_block_html(match), page_frame.copy_code_block(code))


class PageStatusTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.enterContext(status_configuration())

    def test_title_places_one_status_directly_after_the_title(self) -> None:
        for page, *_ in STATUS_PAGES:
            with self.subTest(page=page):
                markup = page_frame.title(page.title(), page)
                self.assertIn('</h1><p class="page-status"', markup)
                self.assertEqual(markup.count('data-page-status '), 1)
                self.assertIn('role="status" aria-live="off"', markup)
                self.assertEqual(markup.count('data-status-icon='), 5)
                self.assertIn('aria-live="polite" data-status-announcement', markup)
        scripts = Path(__file__).resolve().parents[1]
        for page, path in (('builds', 'build_queue/memory_live.html'),
                           ('aws', 'aws_target/aws_prices.html'),
                           ('disks', 'disks/disks.html')):
            with self.subTest(page=page):
                source = (scripts / path).read_text(encoding='utf-8')
                self.assertEqual(page_frame.template(source, page.title(), page).count(page_frame.BUTTON_STATES), 1)

    def test_page_owner_uses_current_name_and_parent_chain(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = root / "console.conf"
        _ = config.write_text("page.list.owner=owner\nowner.default=default\n", encoding="utf-8")
        document = root / "remote" / "production.md"
        live = staff.LiveDoc("natedev", document)
        parent = staff.StaffRow(staff.StaffId("parent"), "showrunner", "Nearest", staff.STAFF_ROOT,
                                current_session=staff.StaffId("session-parent"), doc=live)
        default = replace(parent, id=staff.StaffId("default"), name="Default")
        owner = replace(parent, id=staff.StaffId("owner"), name="Current & name", parent=staff.ReportsTo(parent.id))
        rows = staff.StaffSnapshot((parent, default, owner))
        settings = conf.read(config)
        self.assertEqual(review_submissions.page_owner("list", settings, rows), owner)
        self.assertEqual(review_submissions.page_owner("list", settings, replace(rows, pending_retire=(owner.id,))), parent)
        wrapped = replace(owner, doc=staff.WrappedDoc("natedev", document))
        self.assertEqual(review_submissions.page_owner("list", settings, replace(rows, rows=(parent, default, wrapped))), parent)
        self.assertEqual(review_submissions.page_owner("list", settings, replace(rows, rows=(parent, default))), default)
        self.assertEqual(review_submissions.page_owner("images", settings, rows), default)
        with (patch.object(staff, "local_snapshot", return_value=rows),
              patch.dict(os.environ, {"CONF_DIR": str(root)}),
              patch.object(subprocess, "Popen", side_effect=AssertionError("The owner launched a process."))):
            page = serve.owned_document(b'<html><head></head><body><main><h1>The List</h1></main></body></html>', "list")
        self.assertIn(b'data-page-owner="list"', page)
        self.assertIn(b'<main><div class="page-top"><div class="page-heading-actions" data-page-actions>'
                      + b'<div class="page-owner" data-page-owner="list">', page)
        self.assertIn(b'</div></div><div class="page-heading"><h1>The List</h1></div></div>'
                      + page_frame.HEADING_MEASURE.encode() + b'</main>', page)
        self.assertIn(page_frame.HEADING_STYLE.encode(), page)
        self.assertIn(b'class="staff-name" data-color-text="role.showrunner"', page)
        self.assertIn(b'aria-label="New item for Current &amp; name" title="New item for Current &amp; name"', page)
        self.assertEqual(page.count(b'<dialog id="review-new-item"'), 1)
        self.assertEqual(page_frame.with_owner(page, "list", owner, rows, lambda: self.fail("The dialog already exists.")), page)

    def test_list_owner_setting_keeps_morty_in_new_item_recipients(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(root)}))
        config = conf.path("console")
        self.assertEqual(config, root / "console.conf")
        repository = Path(__file__).resolve().parents[2] / "config" / "console.conf"
        identity = staff.StaffId(conf.read(repository).text("page.list.owner"))
        morty = staff.StaffRow(identity, "producer", "morty", staff.STAFF_ROOT,
                               current_session=staff.StaffId("current-morty-session"),
                               doc=staff.LiveDoc("natedev", config.parent / "missing.md"))
        members = staff.StaffSnapshot((morty,))
        with patch.object(review_submissions, "local_machine", return_value="natedev"):
            _ = config.write_text("page.list.owner=producer-staff\n", encoding="utf-8")
            self.assertEqual(review_submissions.recipient_options(members), [])
            _ = config.write_text(f"page.list.owner={identity}\n", encoding="utf-8")
            self.assertEqual(review_submissions.recipient_options(members),
                             [{"id": identity, "name": "morty", "proposal": False}])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the frozen top.")
    def test_desktop_frozen_top_keeps_its_position_and_clears_scroll_targets(self) -> None:
        probe = b'''<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  frameElement.style.width='1440px';
  frameElement.style.height='800px';
  await painted();
  const frozen=document.querySelector('.page-top');
  const parts=[frozen,...frozen.querySelectorAll('h1,.page-owner,[data-page-status]')];
  const boxes=()=>parts.map(element=>element.getBoundingClientRect());
  const content=document.querySelector('#jump-link');
  const before=boxes(), contentTop=content.getBoundingClientRect().top;
  scrollTo({top:600,behavior:'instant'});
  await painted();
  check(scrollY===600 && Math.abs(content.getBoundingClientRect().top-contentTop+600)<1,
   'The content scrolls 600 px.');
  check(parts.length===4 && boxes().every((box,index)=>box.height>0 &&
   Math.abs(box.top-before[index].top+before[0].top)<1),
   'The title, actions, and status keep their positions in the block when it reaches the top.');
  const clear=target=>{
   const bounds=target.getBoundingClientRect();
   return bounds.top>=frozen.getBoundingClientRect().bottom-0.5 && bounds.bottom<=innerHeight;
  };
  content.click();
  await painted();
  check(location.hash==='#anchor' && clear(document.querySelector('#anchor')),
   'The anchor jump puts the full target below the frozen top.');
  const target=document.querySelector('#scroll-target'), beforeScroll=scrollY;
  target.scrollIntoView({block:'start',behavior:'instant'});
  await painted();
  check(scrollY>beforeScroll && clear(target),'scrollIntoView puts the full target below the frozen top.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        content = ('<a id="jump-link" href="#anchor">Jump to target</a><div style="height:1200px"></div>'
                   '<p id="anchor">Anchor target</p><div style="height:1200px"></div>'
                   '<p id="scroll-target">Scroll target</p><div style="height:1200px"></div>')
        for width_style, page, before_content in (
            ("text", long_open_row_page(), b'<div data-test-list>'),
            ("page-wide", builds_page(), b'<div class="queue-error"'),
        ):
            with self.subTest(width_style=width_style):
                self.assertIn(before_content, page)
                markup = page.replace(before_content, content.encode() + before_content, 1)
                self.assertEqual(firefox_probe_results(markup.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the frozen title.")
    def test_only_the_title_stays_frozen_at_640_pixels(self) -> None:
        probe = b'''<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 try{
  frameElement.style.width='640px';
  frameElement.style.height='800px';
  document.querySelector('[data-detail]').hidden=false;
  await painted();
  const heading=document.querySelector('.page-heading'), title=heading.querySelector('h1');
  const owner=document.querySelector('.page-owner-name'), button=document.querySelector('[data-owner-new]');
  const status=document.querySelector('[data-page-status]');
  const top=heading.getBoundingClientRect().top;
  const controls=[owner,button,status];
  const before=controls.map(element=>element.getBoundingClientRect());
  const hitCenter=box=>document.elementFromPoint(box.left+box.width/2,box.top+box.height/2);
  check(innerWidth===640 && before.every((box,index)=>box.top>=0 && box.bottom<=innerHeight &&
   controls[index].contains(hitCenter(box))),'The actions and status start in view at 640 px.');
  const main=document.querySelector('main').getBoundingClientRect();
  scrollTo({top:600,behavior:'instant'});
  await painted();
  const bounds=title.getBoundingClientRect(), stuck=heading.getBoundingClientRect();
  check(scrollY===600 && top>stuck.top && Math.abs(stuck.top-8)<1 && title.contains(hitCenter(bounds)),
   'The title stays in view at the top.');
  check(controls.every((element,index)=>{
   const bottom=element.getBoundingClientRect().bottom;
   return bottom<0 && Math.abs(bottom-before[index].bottom+600)<1;
  }),'The owner, New item button, and status scroll out of view.');
  const after=document.querySelector('main').getBoundingClientRect();
  check(after.left===main.left && after.width===main.width,'The content keeps its left edge and width.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        page = long_open_row_page()
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    def check_phone_undo_stack(self, checks: str, *, linked_card: bytes = b"", height: int = 844) -> None:
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const host=window.top.document,root=document.documentElement;
 const stack=()=>host.querySelector('[data-send-undo-stack]');
 const controls=document.querySelector('#last-card-actions');
 const bottom=()=>root.scrollHeight-innerHeight;
 const inset=(doc=document)=>parseFloat(doc.defaultView.getComputedStyle(doc.documentElement).scrollPaddingBottom);
 const realFetch=fetch.bind(window);
 const holdPath='/send-hold/';
 window.fetch=async url=>{
  const path=new URL(url,location.href).pathname;
  if(path.startsWith('/hold-')){
   const id=path.slice(6).padStart(24,'0');
   return Response.json({hold:id,seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  if(path.endsWith('/undo')) return Response.json({outcome:'undone'});
  if(path.startsWith(holdPath)){
   return path.endsWith('bad') ? Response.json({send_failure:'The recipient is offline.'},{status:503})
    : Response.json({outcome:'sent'});
  }
  return realFetch(url);
 };
 const hold=(id,receiver='The producer of the final review card')=>
  heldFetch('/hold-'+id,{method:'POST'},receiver).catch(error=>error);
 const undoButtons=()=>[...stack().querySelectorAll('button')];
 const clearButtons=()=>{
  const top=stack().getBoundingClientRect().top;
  return [...controls.querySelectorAll('button')].every(button=>{
   const box=button.getBoundingClientRect();
   const hit=host.elementFromPoint(box.left+box.width/2,box.top+box.height/2+56);
   return box.top>=0 && box.bottom+56<=top+1 && hit===frameElement;
  });
 };
 try{
  host.body.style.cssText='margin:0;overflow:hidden;padding-top:56px';
  host.body.id='console-shell';
  frameElement.style.cssText='display:block;width:390px;border:0';
  frameElement.style.height=(host.defaultView.innerHeight-56)+'px';
  await painted();
  CHECKS
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        card = json.dumps(linked_card.decode()).replace("<", r"\u003c")
        content = ('<style>main{padding-bottom:8px}</style>'
                   + page_frame.title("Review", "review")
                   + '<section style="padding-top:1000px"><h2>Last card</h2><div id="last-card-actions">'
                   + '<button style="height:40px">Approve</button><button>Send feedback</button><button>Later</button>'
                   + '</div></section>'
                   + (list_page.STYLE + '<dialog data-list-review-dialog data-fullscreen></dialog>' if linked_card else '')
                   + '<script id="held-send-fixture">' + page_frame.HELD_SEND_SCRIPT + '</script>'
                   + probe.replace("CHECKS", checks.replace("LINKED_CARD", card)))
        page = change_page.document(content, "Review")
        self.assertEqual(firefox_probe_results(page, window_size=(390, height), dark=True), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the Undo stack.")
    def test_phone_undo_stack_leaves_room_for_the_last_card_controls(self) -> None:
        self.check_phone_undo_stack(r'''
  scrollTo({top:bottom(),behavior:'instant'});
  const baseline=bottom(),start=controls.getBoundingClientRect().bottom;
  const first=hold('a');
  await painted();
  const one=stack().getBoundingClientRect();
  check(one.width<=280 && Math.abs(one.right-374)<1 && Math.abs(one.bottom-820)<1,
   'The stack wraps at 280 px and stays at the bottom right.');
  check(start+56>one.top,'The last card starts under the stack.');
  check(Math.abs(inset()-one.height-24)<1 && Math.abs(bottom()-baseline-inset())<1,
   'The scroll range and scroll padding use the measured stack height and bottom gap.');
  const second=hold('b','Another producer');
  await painted();
  const both=stack().getBoundingClientRect();
  check(both.height>one.height && Math.abs(inset()-both.height-24)<1,
   'The scroll padding grows when a second toast appears.');
  controls.scrollIntoView({block:'end',behavior:'instant'});
  await painted();
  check(clearButtons(),'All last card buttons can scroll fully above the stack.');
  undoButtons().at(-1).click();
  await second;await painted();
  check(Math.abs(inset()-one.height-24)<1,'The scroll padding shrinks when one toast leaves.');
  undoButtons()[0].click();
  await first;await painted();
  check(!stack() && !root.hasAttribute('data-send-undo-space') &&
   getComputedStyle(root).scrollPaddingBottom==='auto' && bottom()===baseline,
   'The scroll space and scroll padding disappear when the stack empties.');
''')

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the stack limit.")
    def test_phone_undo_stack_caps_below_the_frozen_header(self) -> None:
        self.check_phone_undo_stack(r'''
  const sends=Array.from({length:20},(_,index)=>hold(index.toString(16)));
  await painted();
  const box=stack(),initial=box.getBoundingClientRect();
  check(box.scrollHeight>box.clientHeight && initial.top>=96,
   'The capped stack scrolls below the 56 px console header and a full button.');
  let scans=0;
  const query=host.querySelectorAll.bind(host);
  host.querySelectorAll=selector=>{if(selector==='iframe') scans++;return query(selector);};
  root.style.setProperty('--page-top-height','100px');
  dispatchEvent(new Event('page-top-resize'));
  dispatchEvent(new Event('page-top-resize'));
  host.defaultView.dispatchEvent(new Event('page-top-resize'));
  await new Promise(resolve=>host.defaultView.requestAnimationFrame(resolve));
  check(scans===1,'The resize events share one measurement in the next frame.');
  host.querySelectorAll=query;
  await painted();
  const top=box.getBoundingClientRect().top;
  check(top>=196 && top>initial.top,'The cap uses the page header inside the frame.');
  controls.scrollIntoView({block:'end',behavior:'instant'});
  await painted();
  check(clearButtons() && controls.getBoundingClientRect().top>=100,
   'The last card buttons fit between the frozen header and the stack.');
  const buttons=undoButtons();
  box.scrollTop=box.scrollHeight;
  const last=buttons.at(-1).getBoundingClientRect();
  check(last.bottom<=box.getBoundingClientRect().bottom && last.top>=top,
   'The last Undo button scrolls fully into view.');
  buttons.forEach(button=>button.click());
  await Promise.all(sends);await painted();
  check(!stack(),'Undo removes the capped stack.');
''')

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the stack limit.")
    def test_short_undo_stack_fits_one_full_toast(self) -> None:
        self.check_phone_undo_stack(r'''
  const first=hold('a'),second=hold('b');
  await painted();
  const box=stack();
  box.scrollTop=box.scrollHeight;
  const area=box.getBoundingClientRect(),last=box.lastElementChild.getBoundingClientRect();
  check(host.defaultView.innerHeight===160 && box.scrollTop>0 && last.top>=area.top-1 &&
   last.bottom<=area.bottom+1,'A full toast fits in the capped stack at 160 px.');
  const button=undoButtons().at(-1),rect=button.getBoundingClientRect();
  check(host.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2)===button,
   'Undo is clickable in the short viewport.');
  undoButtons().forEach(button=>button.click());
  await Promise.all([first,second]);await painted();
''', height=160)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the linked Review card.")
    def test_linked_review_later_clears_the_undo_stack(self) -> None:
        members = staff.StaffSnapshot((PAGE_OWNER,))
        snapshot = review_page.ReviewSnapshot(
            version="test", records={}, entries=[], members=members, browser=change_page.ReviewBrowser("test"),
            words_rows=3, refresh_seconds=15, request_timeout_seconds=10, card_motion=page_frame.CardMotion(0, 0),
            assets={}, zone=ZoneInfo("UTC"), source_stamps={}, later={"soon": 60, "morning": 480, "weekDay": 0,
                                                                    "weekTime": 480, "step": 15},
            names=staff_names.StaffNames(members), log_max_height_rem=15,
        )
        entry = review_page.ReviewIndexEntry(
            key="test/C1", id="C1", type=review_items.ReviewType.CHANGE, owner="owner",
            sourceOwner="owner", ownerName="Page owner", title="Linked change", state=review_items.ReviewState.OPEN,
            filed=0, updated="", hiddenUntil=0, sourceName="Page owner", ownerHtml="", sourceHtml="",
            urgency="normal",
        )
        later = record_table.park_control("Later", field="later", action="outcome", value="later")
        body = ('<div class="review-panel"><div data-review-body><p style="height:1400px">Details</p></div>'
                + '<form data-review-form><textarea name="feedback"></textarea><div class="review-actions">'
                + '<button type="submit" value="approve">Approve</button>' + later + '</div></form></div>')
        card = review_page.render_card(snapshot, entry, body, waiting_why="")
        self.check_phone_undo_stack(r'''
  const dialog=document.querySelector('[data-list-review-dialog]');
  const frame=document.createElement('iframe');
  const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  frame.srcdoc=LINKED_CARD;dialog.append(frame);dialog.showModal();await loaded;await painted();
  const doc=frame.contentDocument,body=doc.querySelector('.review-box-scroll');
  const later=doc.querySelector('[data-later-toggle]'),footer=later.closest('footer');
  const top=()=>stack().getBoundingClientRect().top;
  const bounds=()=>frame.getBoundingClientRect();
  const clearsStack=()=>{
   const button=later.getBoundingClientRect(),area=bounds();
   const x=button.left+button.width/2,y=button.top+button.height/2;
   return area.top+56+button.bottom<=top()+1 && button.top>=0 &&
    host.elementFromPoint(area.left+x,area.top+56+y)===frameElement &&
    document.elementFromPoint(area.left+x,area.top+y)===frame &&
    doc.elementFromPoint(x,y)?.closest('[data-later-toggle]')===later;
  };
  const box=footer.closest('.review-box');
  const baseline=footer.getBoundingClientRect().bottom;
  const first=hold('a'),second=hold('b');
  await painted();
  check(footer.getBoundingClientRect().bottom<baseline && clearsStack(),
   'Later fits above the stack in a full-height linked card.');
  const bottomWithStack=footer.getBoundingClientRect().bottom;
  body.scrollTop=body.scrollHeight;
  check(body.scrollTop>0 && footer.getBoundingClientRect().bottom===bottomWithStack,
   'The linked body scrolls and the footer stays fixed.');
  later.click();await painted();
  check(!doc.querySelector('[data-later-menu]').hidden,'Later opens its menu.');
  undoButtons().forEach(button=>button.click());
  await Promise.all([first,second]);await painted();
  check(!stack() && !doc.documentElement.hasAttribute('data-send-undo-space') &&
   footer.getBoundingClientRect().bottom===baseline,'Undo restores the full-height card.');
  later.click();
  const sends=Array.from({length:20},(_,index)=>hold(index.toString(16)));
  await painted();
  box.scrollTop=box.scrollHeight;
  check(box.hasAttribute('data-whole-scroll') && box.scrollTop>0 && clearsStack(),
   'With a capped stack, the full card scrolls until Later clears the stack.');
  undoButtons().forEach(button=>button.click());
  await Promise.all(sends);await painted();
  check(!box.hasAttribute('data-whole-scroll') && footer.getBoundingClientRect().bottom===baseline,
   'The body scrolls again after the stack leaves.');
''', linked_card=card)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check cleanup.")
    def test_undo_stack_clears_space_after_timeout_failure_and_frame_close(self) -> None:
        self.check_phone_undo_stack(r'''
  const frame=document.createElement('iframe');
  frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0';
  const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  frame.srcdoc='<p>Linked frame</p>';document.body.append(frame);await loaded;
  const doc=frame.contentDocument,frameRoot=doc.documentElement,loadListeners=new Set();
  const add=doc.addEventListener.bind(doc),remove=doc.removeEventListener.bind(doc);
  doc.addEventListener=(name,handler,...options)=>{
   if(name==='load') loadListeners.add(handler);
   add(name,handler,...options);
  };
  doc.removeEventListener=(name,handler,...options)=>{
   if(name==='load') loadListeners.delete(handler);
   remove(name,handler,...options);
  };
  const timeout=hold('a');
  await painted();
  check(frameRoot.hasAttribute('data-send-undo-space') && loadListeners.size===1,
   'The linked frame has the inset and one load listener.');
  const measured=stack().getBoundingClientRect().height;
  frame.remove();
  await painted();
  check(!frameRoot.hasAttribute('data-send-undo-space') &&
   !frameRoot.style.getPropertyValue('--send-undo-space') && loadListeners.size===0 &&
   stack().getBoundingClientRect().height===measured,'Removing the frame clears it while the toast stays.');
  const expire=()=>stack().lastElementChild.getAnimations({subtree:true}).forEach(animation=>animation.finish());
  expire();await timeout;await painted();
  check(!stack() && !root.hasAttribute('data-send-undo-space'),'A completed send clears the inset after timeout.');
  const failure=hold('bad');
  await painted();expire();await failure;await painted();
  check(stack()?.textContent.includes('The recipient is offline.') && root.hasAttribute('data-send-undo-space'),
   'The failure message keeps the inset while it is visible.');
  await new Promise(resolve=>setTimeout(resolve,3100));await painted();
  check(!stack() && !root.hasAttribute('data-send-undo-space'),'The inset clears after the failure message leaves.');
''')

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check cleanup.")
    def test_undo_stack_clears_when_the_sending_frame_closes_during_failure(self) -> None:
        self.check_phone_undo_stack(r'''
  const frame=document.createElement('iframe');
  frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0';
  const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  frame.srcdoc='<p>Sending frame</p>';document.body.append(frame);await loaded;
  const sender=frame.contentWindow,frameRoot=frame.contentDocument.documentElement;
  const script=frame.contentDocument.createElement('script');
  script.textContent=document.querySelector('#held-send-fixture').textContent;
  frame.contentDocument.head.append(script);
  sender.fetch=window.fetch;
  const failure=sender.heldFetch(location.origin+'/hold-bad',{method:'POST'},'The producer');
  await painted();
  stack().lastElementChild.getAnimations({subtree:true}).forEach(animation=>animation.finish());
  await failure;await painted();
  check(stack()?.textContent.includes('The recipient is offline.') &&
   root.hasAttribute('data-send-undo-space') && frameRoot.hasAttribute('data-send-undo-space'),
   'The sending frame shows a failure in the top window.');
  frame.remove();
  await painted();
  check(!stack(),'Closing the sender removes the failure toast.');
  check(!root.hasAttribute('data-send-undo-space') && !frameRoot.hasAttribute('data-send-undo-space'),
   'Closing the sender clears both insets.');
''')

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the owner button position.")
    def test_page_owner_stays_visible_above_a_long_open_row(self) -> None:
        page = long_open_row_page()
        reference = json.dumps(without_frozen_top(page).decode()).replace('<', r'\u003c').encode()
        probe = b'''<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  frameElement.style.height='800px';
  const button=document.querySelector('[data-owner-new]');
  const owner=button.closest('.page-owner'), name=owner.querySelector('.page-owner-name');
  const actions=document.querySelector('[data-page-actions]'), frozen=document.querySelector('.page-top');
  const reference=document.createElement('iframe');
  reference.style.cssText='position:fixed;left:-10000px;top:0;border:0;height:800px';
  const referenceReady=new Promise(resolve=>reference.addEventListener('load',resolve,{once:true}));
  reference.srcdoc=MAIN_PAGE;
  document.body.append(reference);
  await referenceReady;
  const title=document.querySelector('h1'), banner=document.querySelector('[data-page-status]');
  const main=document.querySelector('main'), table=document.querySelector('table'), row=table.querySelector('.list-row');
  const detail=document.querySelector('[data-detail]');
  banner.dataset.state='Late';
  banner.querySelector('[data-status-word]').textContent='Late';
  const reason=banner.querySelector('[data-status-reason]');
  reason.hidden=false;
  reason.textContent='The feed has stopped.';
  reason.style.flexBasis='100%';
  const view=createRecordTable({content:()=>document.querySelector('[data-test-list]'),
   shows:['all'],defaultShow:'all',visible:()=>true,applied:()=>{detail.hidden=false;}});
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const boxes=doc=>['.page-owner-name','[data-owner-new]','h1'].map(selector=>{
   const {x,y,width,height}=doc.querySelector(selector).getBoundingClientRect();
   return [x,y,width,height];
  });
  const columns=()=>JSON.stringify([main,table].map(element=>{
   const {left,width}=element.getBoundingClientRect();
   return [left,width];
  }));
  const bottom=()=>document.scrollingElement.scrollHeight-innerHeight;
  const opaque=color=>{const parts=color.match(/[0-9.]+/g);return parts!==null && (parts.length===3 || Number(parts[3])===1);};
  const statusCovered=()=>{
   const box=banner.getBoundingClientRect(), top=Math.max(box.top,0);
   return [box.left+2,(box.left+box.right)/2,box.right-2].some(x=>[top+1,(top+box.bottom)/2,box.bottom-1]
    .some(y=>!banner.contains(document.elementFromPoint(x,y))));
  };
  for(const width of [1440,1024]){
   const verify=(ok,message)=>check(ok,width+' px: '+message);
   frameElement.style.width=width+'px';
   reference.style.width=width+'px';
   scrollTo({top:0,behavior:'instant'});
   await painted();
   const first=JSON.stringify(boxes(document)), layout=columns();
   verify(first===JSON.stringify(boxes(reference.contentDocument)),
    'The name, the button and the title match the page without the sticky rule at scroll zero.');
   const background=getComputedStyle(owner).backgroundColor;
   verify(opaque(background) && background===getComputedStyle(document.body).backgroundColor,
    'The owner control has the opaque page background.');
   const statusEnd=Math.ceil(banner.getBoundingClientRect().bottom+owner.getBoundingClientRect().height);
   for(let top=0;top<=statusEnd;top+=2){
    scrollTo({top,behavior:'instant'});
    await painted();
    if(statusCovered()){verify(false,'The action area covers the Late status at scroll '+top+'.');break;}
   }
   for(const top of [1,Math.floor(bottom()/2),bottom()]){
    scrollTo({top,behavior:'instant'});
    await painted();
    verify(scrollY===top && columns()===layout,'The content keeps its left edge and width at scroll '+top+'.');
   }
   const last=owner.getBoundingClientRect(), status=banner.getBoundingClientRect();
   const block=frozen.getBoundingClientRect(), heading=title.getBoundingClientRect();
   const nameBox=name.getBoundingClientRect(), buttonBox=button.getBoundingClientRect();
   verify(innerWidth===width && scrollY>innerHeight && scrollY===bottom(),'The page scrolls to the bottom of the long row.');
   const state=banner.querySelector('.page-status-state').getBoundingClientRect();
   verify(status.top>=Math.max(last.bottom,heading.bottom)-0.5 && reason.getBoundingClientRect().top>=state.bottom,
    'The status spans two lines below the title and the actions.');
   verify(Math.abs(last.top-block.top-8)<1 && actions.contains(owner),'The actions stay 8 px below the frozen top.');
   verify(heading.height>0 && heading.top>=block.top && heading.bottom<=status.top,
    'The title stays in view above the status.');
   verify(Math.abs(last.right-block.right)<1 && last.bottom<=innerHeight,
    'The owner stays at the right edge of the frozen top.');
   verify(nameBox.width>0 && Math.abs(buttonBox.left-nameBox.right-8)<0.1 && buttonBox.width===36,
    'The name stays beside the full button.');
   verify(detail.getBoundingClientRect().top<last.top,'The open row scrolls under the action area.');
   let hidden=true;
   for(let x=Math.ceil(last.left);x<last.right-1;x+=4){
    for(let y=Math.ceil(last.top);y<last.bottom-1;y+=4){
     hidden=hidden && owner.contains(document.elementFromPoint(x,y));
    }
   }
   verify(hidden,'No table text shows under the owner.');
   const x=(buttonBox.left+buttonBox.right)/2;
   verify(button.contains(document.elementFromPoint(x,buttonBox.top+1))
    && button.contains(document.elementFromPoint(x,buttonBox.bottom-1)),'The whole button is clickable.');
   scrollTo({top:0,behavior:'instant'});
   await painted();
   verify(JSON.stringify(boxes(document))===first,'The original heading returns at scroll zero.');
   verify(view.revealMatchingItem('open'),'The item jump finds the row.');
   await painted();
   verify(document.activeElement===row.querySelector('button') && scrollY>0 &&
    row.getBoundingClientRect().top>=frozen.getBoundingClientRect().bottom-0.5,
    'The item jump from scroll zero focuses the row below the frozen top.');
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''.replace(b'MAIN_PAGE', reference)
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the owner under a sticky status.")
    def test_late_or_failed_status_never_covers_the_owner(self) -> None:
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.height='800px';
  document.querySelector('[data-detail]').hidden=false;
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const owner=document.querySelector('.page-owner'), banner=document.querySelector('[data-page-status]');
  const name=document.querySelector('.page-owner-name'), button=document.querySelector('[data-owner-new]');
  const main=document.querySelector('main'), table=document.querySelector('table'), title=document.querySelector('h1');
  const reason=banner.querySelector('[data-status-reason]');
  const bottom=()=>document.scrollingElement.scrollHeight-innerHeight;
  const frozen=document.querySelector('.page-top');
  const layout=()=>JSON.stringify([main,table].map(element=>{
   const {top,left,width}=element.getBoundingClientRect();
   return [top+scrollY,left,width];
  }));
  // A box is clear when it is fully in view and a hit test at its corners and center finds it.
  const clear=(element,inset)=>{
   const box=element.getBoundingClientRect();
   const points=[[box.left+inset,box.top+inset],[box.right-inset,box.top+inset],[box.left+inset,box.bottom-inset],
    [box.right-inset,box.bottom-inset],[(box.left+box.right)/2,(box.top+box.bottom)/2]];
   return box.width>0 && box.top>=0 && box.left>=0 && box.bottom<=innerHeight && box.right<=innerWidth
    && points.every(([x,y])=>element.contains(document.elementFromPoint(x,y)));
  };
  const statusShown=()=>{
   const box=banner.getBoundingClientRect();
   return box.top>=0 && box.bottom<=innerHeight && [box.left+2,(box.left+box.right)/2,box.right-2]
    .every(x=>[box.top+1,(box.top+box.bottom)/2,box.bottom-1].every(y=>banner.contains(document.elementFromPoint(x,y))));
  };
  for(const [state,text] of [['Late','The feed has stopped.'],['Failed','The read failed with HTTP 502.']]){
   banner.dataset.state=state;
   banner.querySelector('[data-status-word]').textContent=state;
   reason.hidden=false;
   reason.textContent=text;
   for(const width of [1440,1024]){
    const found=new Set();
    const verify=(ok,message,top)=>{
     if(!ok && !found.has(message)){found.add(message);failures.push(state+', '+width+' px, scroll '+top+': '+message);}
    };
    frameElement.style.width=width+'px';
    scrollTo({top:0,behavior:'instant'});
    await painted();
    const start=layout();
    const positions=[];
    for(let top=0;top<=200;top+=2) positions.push(top);
    positions.push(Math.floor(bottom()/2),bottom());
    let previous=null;
    for(const top of positions){
     scrollTo({top,behavior:'instant'});
     await painted();
     const row=owner.getBoundingClientRect(), status=banner.getBoundingClientRect();
     verify(scrollY===top && innerWidth===width,'The page scrolls to the position.',top);
     verify(clear(name,1),'The owner name is fully in view and not covered.',top);
     verify(clear(button,3),'The New item button is fully in view and not covered.',top);
     verify(statusShown(),'The status line is fully in view and not covered by the action area.',top);
     verify(row.bottom<=status.top+0.5,'The action area stays above the status line.',top);
     verify(row.top>=7.5,'The action area stays at least 8 px below the viewport top.',top);
     verify(layout()===start,'The content does not move.',top);
     if(previous){
      // Each part moves up by at most the scroll distance and never moves down, so nothing steps.
      const distance=top-previous.top;
      verify([[row.top,previous.row],[status.top,previous.status]].every(([now,before])=>before-now>=-0.5 && before-now<=distance+0.5),
       'The action area and the status line move by no more than the scroll distance.',top);
     }
     previous={top,row:row.top,status:status.top};
    }
    verify(Math.abs(frozen.getBoundingClientRect().top)<1 && clear(title,1)
     && banner.getBoundingClientRect().top>=Math.max(title.getBoundingClientRect().bottom,owner.getBoundingClientRect().bottom)-0.5,
     'At the bottom the frozen top shows the title and the actions above the status.',bottom());
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(long_open_row_page().replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the AWS region filter.")
    def test_aws_region_filter_sticks_below_the_frozen_top(self) -> None:
        from scripts.review_links import console_theme

        source = console_theme.with_theme((SCRIPTS / "aws_target" / "aws_prices.html").read_text(encoding="utf-8"))
        page = (page_frame.template(source, "AWS", "aws").replace("__AWS_QUOTA_HELP__", "", 1)
                .replace("__AWS_PRICES_DATA__", "null").replace("__AWS_GATE_TIME_DATA__", "null"))
        markup = page_frame.with_owner(page.encode(), "aws", PAGE_OWNER, staff.StaffSnapshot((PAGE_OWNER,)), lambda: "")
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.height='800px';
  document.querySelector('main').insertAdjacentHTML('beforeend','<div style="height:4000px"></div>');
  document.querySelector('#geographies').textContent='Geography';
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const frozen=document.querySelector('.page-top'), regions=document.querySelector('nav.regions');
  const banner=document.querySelector('[data-page-status]');
  for(const state of ['Live','Late']){
   for(const width of [1440,320]){
    frameElement.style.width=width+'px';
    banner.dataset.state=state;
    scrollTo({top:2000,behavior:'instant'});
    await painted();
    const bar=regions.getBoundingClientRect();
    const above=(width<=640 ? frozen.querySelector('.page-heading') : frozen).getBoundingClientRect().bottom;
    const hit=document.elementFromPoint((bar.left+bar.right)/2,bar.top+1);
    if(scrollY<1000 || Math.abs(bar.top-above)>=1 || bar.bottom>innerHeight || !regions.contains(hit)){
     failures.push(width+' px, '+state+': the region filter must stay in view below the frozen top and take a click at its top.');
    }
   }
  }
  const quotas=document.querySelector('#prices-quotas');
  const spacer=document.createElement('div');
  spacer.style.height='400px';
  quotas.before(spacer);
  for(const width of [1440,440]){
   frameElement.style.width=width+'px';
   history.replaceState(null,'',location.pathname);
   scrollTo({top:0,behavior:'instant'});
   await painted();
   location.hash='prices-quotas';
   await painted();
   const target=quotas.getBoundingClientRect(), filter=regions.getBoundingClientRect();
   if(scrollY<300 || target.top<filter.bottom-1 || target.top>=innerHeight){
    failures.push(width+' px: the jump to Prices and quotas must clear the region filter.');
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(markup.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the Placement panel.")
    def test_focused_placement_control_clears_the_frozen_panel_header(self) -> None:
        markup = (b'<!doctype html><html><head><style>'
                  b'#panel{height:280px;width:340px;overflow:auto;padding:16px}'
                  b'</style></head><body><main id="stage"></main><aside id="panel"><h1>Placement disk</h1>'
                  b'<div style="height:500px"></div><button id="target">Select disk</button>'
                  b'<div style="height:500px"></div></aside></body></html>')
        page = page_frame.with_owner(markup, "placement_disk", PAGE_OWNER,
                                     staff.StaffSnapshot((PAGE_OWNER,)), lambda: "")
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.width='1000px';
  frameElement.style.height='700px';
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  await painted();
  const panel=document.querySelector('#panel'), header=panel.querySelector('.page-top');
  const target=panel.querySelector('#target');
  target.focus({preventScroll:true});
  target.scrollIntoView({block:'start',behavior:'instant'});
  await painted();
  const box=target.getBoundingClientRect();
  const hit=document.elementFromPoint((box.left+box.right)/2,(box.top+box.bottom)/2);
  if(document.activeElement!==target || panel.scrollTop<300 || scrollY!==0 ||
     box.top<header.getBoundingClientRect().bottom-1 || !target.contains(hit)){
   failures.push('The focused control must stay visible below the header inside the panel.');
  }
  target.blur();
  const headerBox=header.getBoundingClientRect();
  panel.scrollTop+=box.top-(headerBox.top+headerBox.height/2);
  await painted();
  const start=panel.scrollTop, covered=target.getBoundingClientRect();
  if(covered.top<panel.getBoundingClientRect().top || covered.top>=header.getBoundingClientRect().bottom){
   failures.push('The control must start under the frozen header inside the panel.');
  }
  target.focus();
  await painted();
  const focused=target.getBoundingClientRect();
  const focusedHit=document.elementFromPoint((focused.left+focused.right)/2,(focused.top+focused.bottom)/2);
  if(document.activeElement!==target || panel.scrollTop>=start || scrollY!==0 ||
     focused.top<header.getBoundingClientRect().bottom-1 || !target.contains(focusedHit)){
   failures.push('Native focus must scroll the control below the frozen panel header.');
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the Builds paused badge.")
    def test_builds_paused_badge_matches_main_and_stays_visible(self) -> None:
        markup = builds_page()
        paused = (b'<script>document.addEventListener("DOMContentLoaded",()=>setPaused(true));</script>'
                  + b'<style>*,*::before,*::after{animation:none!important;transition:none!important}</style></head>')
        markup = markup.replace(b'</head>', paused, 1)
        still = markup.replace(b'<head>', b'<head><script>window.fetch=()=>new Promise(()=>{});</script>', 1)
        for width in (1440, 320):
            with self.subTest(width=width):
                self.assertTrue(firefox_screenshot(still, width, 800) == firefox_screenshot(without_frozen_top(still), width, 800),
                                "At scroll zero the paused page must match the page without the frozen top.")
        probe = b'''<script>
(async()=>{
 const failures=[];
 try{
  frameElement.style.height='800px';
  document.querySelector('main').insertAdjacentHTML('beforeend','<div style="height:4000px"></div>');
  const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const badge=document.querySelector('#paused');
  // The badge takes no clicks; a hit test with clicks on shows what is drawn on top.
  badge.style.pointerEvents='auto';
  for(const width of [1440,320]){
   frameElement.style.width=width+'px';
   scrollTo({top:0,behavior:'instant'});
   await painted();
   const start=badge.getBoundingClientRect();
   scrollTo({top:1200,behavior:'instant'});
   await painted();
   const box=badge.getBoundingClientRect(), inset=box.height/2;
   const shown=[box.left+inset,(box.left+box.right)/2,box.right-inset].every(x=>[box.top+2,(box.top+box.bottom)/2,box.bottom-2]
    .every(y=>badge.contains(document.elementFromPoint(x,y))));
   if(badge.hidden || scrollY!==1200 || box.top!==start.top || box.height===0 || !shown){
    failures.push(width+' px: the paused badge must stay in place and in view when the page scrolls.');
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(markup.replace(b'</body>', probe + b'</body>')), [[]])

    def test_accounts_shows_the_configured_owner_and_names_the_route(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = root / "console.conf"
        document = root / "production.md"
        showrunner = staff.StaffRow(staff.StaffId("showrunner"), "showrunner", "natedev", staff.STAFF_ROOT,
                                    current_session=staff.StaffId("session-showrunner"),
                                    doc=staff.LiveDoc("natedev", document))
        unit = staff.StaffRow(staff.StaffId("unit"), "unit", "accounts", staff.ReportsTo(showrunner.id),
                              current_session=staff.StaffId("session-unit"), unit_type=staff.UnitDetails("direct"))
        producer = staff.StaffRow(staff.StaffId("producer"), "producer", "producer", staff.STAFF_ROOT,
                                  current_session=staff.StaffId("session-producer"))
        rows = staff.StaffSnapshot((unit, showrunner, producer))
        content = b'<html><head></head><body><main><h1>Accounts</h1></main></body></html>'
        cases = (
            ("unit", unit, rows, unit, showrunner, "role.unit-director"),
            ("showrunner", showrunner, rows, showrunner, showrunner, "role.showrunner"),
            ("producer", producer, rows, producer, producer, "role.producer"),
            ("inactive unit", unit, replace(rows, pending_retire=(unit.id,)), showrunner, showrunner, "role.showrunner"),
        )
        for case, configured, members, shown, route, color in cases:
            with self.subTest(case=case):
                _ = config.write_text(f"page.accounts.owner={configured.id}\n", encoding="utf-8")
                settings = conf.read(config)
                self.assertEqual(review_submissions.page_owner("accounts", settings, members), route)
                with (patch.object(staff, "local_snapshot", return_value=members),
                      patch.dict(os.environ, {"CONF_DIR": str(root)})):
                    page = serve.owned_document(content, "accounts").decode()
                self.assertIn('data-page-owner="accounts"', page)
                self.assertRegex(page, r'<span class="page-owner-name"><span class="staff-name" ' +
                                 rf'data-color-text="{re.escape(color)}" [^>]+>{shown.name}</span></span>')
                label = f"New item for {route.name}"
                self.assertIn(f'aria-label="{label}" title="{label}"', page)

        _ = config.write_text(f"page.accounts.owner={unit.id}\n", encoding="utf-8")
        with (patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((unit,))),
              patch.dict(os.environ, {"CONF_DIR": str(root)})):
            page = serve.owned_document(content, "accounts")
        self.assertIn(b'>accounts</span></span>', page)
        self.assertNotIn(b'data-owner-new', page)

    def test_each_page_uses_the_same_states_and_retains_the_last_good_read(self) -> None:
        for page, *_ in STATUS_PAGES:
            with self.subTest(page=page):
                assert_component_states(self, page_frame.title(page.title(), page))

    def test_only_the_configured_status_pages_have_age_limits(self) -> None:
        path = Path(__file__).resolve().parents[2] / "config" / "console.conf"
        limits = set(re.findall(r"(?m)^[ \t]*(status\.[^\s=]+\.age_limit_seconds)[ \t]*=",
                                path.read_text(encoding="utf-8")))
        self.assertEqual(limits, {f"status.{name}.age_limit_seconds" for name, *_ in STATUS_PAGES})

    def test_age_uses_seconds_then_whole_minutes_on_every_page(self) -> None:
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        for page, *_ in STATUS_PAGES:
            with self.subTest(page=page):
                match = re.search(r'data-status-options="([^"]+)"', page_frame.title(page.title(), page))
                assert match is not None
                source = self.environment(html.unescape(match[1])) + page_frame.REFRESH_SCRIPT + r'''
(async()=>{
 const refresh=createPageRefresh(async()=>({readAt:now/1000,renderSeconds:.03}));
 await refresh.refresh();
 const start=now;
 for(const [seconds,expected] of [[0,'0 s ago'],[59,'59 s ago'],[60,'1 min ago'],[119,'1 min ago'],[7200,'120 min ago']]){
  now=start+seconds*1000;refresh.changed();
  if(parts.age.textContent!==expected) throw new Error(parts.age.textContent+' != '+expected);
 }
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
                done = subprocess.run([node, '-e', source], text=True, capture_output=True, timeout=20)
                self.assertEqual((done.returncode, done.stdout.strip()), (0, 'PASS'), done.stderr)

    def test_invalid_initial_facts_report_failed_without_an_unhandled_rejection(self) -> None:
        markup = page_frame.title("AWS", "aws")
        match = re.search(r'data-status-options="([^"]+)"', markup)
        assert match is not None
        for value in ('{readAt:now/1000,renderSeconds:NaN}', '{readAt:NaN,renderSeconds:.1}'):
            with self.subTest(initial=value):
                source = self.environment(html.unescape(match[1])) + page_frame.REFRESH_SCRIPT + """
createPageRefresh(async()=>null,()=>0,()=>'',INITIAL);
setImmediate(()=>{
 if(line.dataset.state!=='Failed' || parts.render.textContent.includes('NaN')) process.exitCode=1;
 else console.log('PASS');
});
""".replace('INITIAL', value)
                node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
                done = subprocess.run([node, '--unhandled-rejections=strict', '-e', source],
                                      text=True, capture_output=True, timeout=20)
                self.assertEqual((done.returncode, done.stdout.strip()), (0, 'PASS'), done.stderr)

    def test_failed_visibility_refresh_keeps_initial_facts_before_the_first_frames(self) -> None:
        markup = page_frame.title("Review", "review")
        match = re.search(r'data-status-options="([^"]+)"', markup)
        assert match is not None
        environment = self.environment(html.unescape(match[1])).replace(
            'const requestAnimationFrame=run=>run();',
            'const frames=[];const requestAnimationFrame=run=>frames.push(run);')
        source = environment + page_frame.REFRESH_SCRIPT + r"""
(async()=>{
 visible='hidden';
 createPageRefresh(async()=>{throw new Error('HTTP 502');},()=>15,()=>'',
  {readAt:now/1000-10,renderSeconds:.1});
 if(frames.length!==1) throw new Error('The initial frames must remain pending.');
 visible='visible';handlers.visibilitychange();
 for(let i=0;i<12;i++) await Promise.resolve();
 if(line.dataset.state!=='Failed' || parts.age.hidden || parts.age.textContent!=='10 s ago')
  throw new Error('A failed visibility refresh must keep the initial data time.');
 frames.shift()();frames.shift()();
 for(let i=0;i<12;i++) await Promise.resolve();
 if(line.dataset.state!=='Failed' || parts.age.hidden || parts.age.textContent!=='10 s ago')
  throw new Error('The older initial frames must not replace the failed refresh.');
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        done = subprocess.run([node, '--unhandled-rejections=strict', '-e', source],
                              text=True, capture_output=True, timeout=20)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'PASS'), done.stderr)

    def test_a_page_can_set_and_clear_busy_before_a_read(self) -> None:
        options = {"ageLimit": 60, "readingDelay": .2, "roundTripLimit": 1, "renderLimit": 1}
        source = self.environment(json.dumps(options)) + page_frame.REFRESH_SCRIPT + r"""
(async()=>{
 const check=(ok,reason)=>{if(!ok) throw new Error(reason);};
 let fail=false;
 const refresh=createPageRefresh(async()=>{
  if(fail) throw new Error('The read failed.');
  return {readAt:now/1000,renderSeconds:.03};
 });
 refresh.setBusy('Measuring…');
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'Busy status must appear before the first read.');
 refresh.setBusy('');
 await refresh.refresh();
 check(line.dataset.state==='Live','A good read must show Live.');
 now+=61000;refresh.changed();
 check(line.dataset.state==='Late','An unanswered page must become Late.');
 refresh.setBusy('Measuring…');
 check(line.dataset.state==='Paused' && parts.age.textContent==='1 min ago',
  'Busy status must appear at once and keep the data age.');
 fail=true;
 await refresh.refresh();
 refresh.setBusy('');
 check(line.dataset.state==='Failed' && parts.reason.textContent==='The read failed.',
  'Clearing the busy status must preserve a read failure.');
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        done = subprocess.run([node, '--unhandled-rejections=strict', '-e', source],
                              text=True, capture_output=True, timeout=20)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'PASS'), done.stderr)

    @staticmethod
    def environment(options: str) -> str:
        return 'const options=' + json.dumps(options) + r''';
let now=1794847200000,perf=0,visible='visible',id=0;
const handlers={},timeouts=new Map(),timeoutAt=new Map(),intervals=new Map(),frameElement={hidden:false};
function node(){return {hidden:false,textContent:'',title:'',dataset:{},
 classList:{values:new Set(),toggle(key,on){if(on)this.values.add(key);else this.values.delete(key);},has(key){return this.values.has(key);}}};}
const parts=Object.fromEntries(['word','age','trip','render','reason','announcement'].map(key=>[key,node()]));
const icons=['Live','Reading','Late','Failed','Paused'].map(state=>({...node(),dataset:{statusIcon:state}}));
const line={dataset:{statusOptions:options},querySelector:selector=>parts[selector.match(/data-status-(\w+)/)[1]],
 querySelectorAll:()=>icons};
const document={visibilityState:'visible',querySelector:()=>line,addEventListener:(key,run)=>handlers[key]=run};
Object.defineProperty(document,'visibilityState',{get:()=>visible});
const window={frameElement,addEventListener:(key,run)=>handlers[key]=run},parent={},location={origin:'http://fixture.invalid'};
Date.now=()=>now;
const performance={now:()=>perf,getEntriesByType:()=>[]};
const setTimeout=(run,ms=0)=>{timeouts.set(++id,run);timeoutAt.set(id,now+ms);return id;};
const clearTimeout=key=>{timeouts.delete(key);timeoutAt.delete(key);};
function advanceTime(ms){
 now+=ms;
 for(const [key,run] of [...timeouts]) if(timeouts.has(key) && timeoutAt.get(key)<=now){clearTimeout(key);run();}
}
const setInterval=run=>{intervals.set(++id,run);return id;},clearInterval=key=>intervals.delete(key);
const requestAnimationFrame=run=>run();
'''


def long_open_row_page() -> bytes:
    """Render a List page whose open row is long enough to scroll far past the heading row."""
    from scripts.review_links import change_page, record_table

    detail = "".join(f'<p>Open row line {index}</p>' for index in range(60))
    table = ('<div data-test-list><input id="list-search"><div id="list-owner"></div><div id="list-show"></div>'
             + '<div class="list-table"><table><tbody><tr><td style="height:800px">Above the item</td></tr></tbody>'
             + '<tbody data-list-item data-key="open" data-order="1"><tr class="list-row">'
             + '<td><button class="list-toggle" aria-expanded="true">Open row</button></td></tr>'
             + f'<tr data-detail><td style="text-align:right">{detail}</td></tr></tbody></table>'
             + '<p data-empty hidden>No items</p></div></div>')
    content = record_table.STYLE + page_frame.title("The List", "list") + table
    markup = change_page.document(content + '<script>' + record_table.SCRIPT + '</script>', "The List")
    return page_frame.with_owner(markup, "list", PAGE_OWNER, staff.StaffSnapshot((PAGE_OWNER,)), lambda: "")


def builds_page() -> bytes:
    """Render the owned Builds page for the browser checks."""
    from scripts.build_queue import memory_feed

    page = memory_feed.page_document(15)
    return page_frame.with_owner(page.encode(), "builds", PAGE_OWNER, staff.StaffSnapshot((PAGE_OWNER,)), lambda: "")


def without_frozen_top(page: bytes) -> bytes:
    """Disable the frozen top for the comparison at scroll zero."""
    style = b'<style>.page-top,.page-top>:is(.page-heading,.page-title){position:relative;top:0}</style>'
    return page.replace(b'</head>', style + b'</head>', 1)


def firefox_screenshot(page: bytes, width: int, height: int) -> bytes:
    """Load one page in a headless Firefox window and return its PNG."""
    with tempfile.TemporaryDirectory(prefix="page-frame-shot-") as temporary:
        root = Path(temporary)
        (root / "profile").mkdir()
        document = root / "page.html"
        _ = document.write_bytes(page)
        run = run_process_group(["firefox", "--headless", "--no-remote", "--profile", str(root / "profile"),
                                 "--window-size", f"{width},{height}", "--screenshot", str(root / "page.png"),
                                 document.as_uri()],
                                timeout=45,
                                env={**os.environ, "MOZ_HEADLESS_WIDTH": str(width), "MOZ_HEADLESS_HEIGHT": str(height)})
        if run.returncode:
            raise AssertionError(run.stderr)
        return (root / "page.png").read_bytes()


def assert_component_states(case: unittest.TestCase, markup: str) -> None:
    """Check the states with the settings from a page's rendered header."""
    node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
    match = re.search(r'data-status-options="([^"]+)"', markup)
    assert match is not None
    options = html.unescape(match[1])
    source = PageStatusTests.environment(options) + page_frame.REFRESH_SCRIPT + r'''
(async()=>{
 const checks=[],check=(ok,label)=>{if(!ok) checks.push(label);};
 const settle=async()=>{for(let i=0;i<12;i++) await Promise.resolve();};
 let result={readAt:now/1000,renderSeconds:.03,renderMedian:.02,renderCount:10},hold='',release;
 let load=async()=>result;
 const refresh=createPageRefresh((value,current)=>load(value,current),()=>15,()=>hold);
 check(line.dataset.state==='Reading','the first read has no data');
 check(parts.age.hidden && parts.trip.hidden && parts.render.hidden,'the first read shows only Reading');
 await refresh.refresh();
 check(line.dataset.state==='Live','a good read is Live');
 check(parts.age.textContent==='0 s ago','the age starts at the data time');
 check(parts.render.textContent==='render 0.03 s','the render time comes from the server');
 check(parts.trip.textContent==='round trip 0.00 s','the round trip starts at the request');
 check(parts.render.title==='Median 0.02 s of the last 10 renders.','the median is hover text');
 const announcement=parts.announcement.textContent;
 now+=4000;refresh.changed();
 check(parts.age.textContent==='4 s ago','the age advances without a read');
 check(parts.announcement.textContent===announcement,'age updates do not speak');
 load=()=>new Promise(resolve=>release=resolve);const slow=refresh.refresh();
 [...timeouts.values()].forEach(run=>run());
 check(line.dataset.state==='Reading' && !parts.age.hidden && !parts.render.hidden,'a slow read keeps the old facts');
 perf+=210;release(result);await slow;
 check(parts.trip.textContent==='round trip 0.21 s','the round trip includes the complete read');
 now+=(JSON.parse(line.dataset.statusOptions).ageLimit+61)*1000;refresh.changed();
 check(line.dataset.state==='Late','old data is Late');
 check(parts.age.textContent.includes('min ago') && parts.reason.textContent==='The feed has stopped.','Late keeps time before its reason');
 load=async()=>{throw new Error('HTTP 502');};await refresh.refresh();
 check(line.dataset.state==='Failed' && parts.reason.textContent==='HTTP 502','a failed read has its reason');
 check(!parts.age.hidden && parts.trip.hidden && parts.render.hidden,'Failed keeps data time');
 hold='You have text in a field.';refresh.changed();
 check(line.dataset.state==='Paused' && parts.reason.textContent===hold,'a held page is Paused before Failed');
 result={readAt:now/1000,renderSeconds:5};load=async()=>result;await refresh.refresh();
 check(line.dataset.state==='Paused','new data waits while held');
 hold='';refresh.changed();await settle();
 check(line.dataset.state==='Live' && parts.render.textContent==='render 5.00 s','a released page uses its held data');
 check(parts.render.classList.has('page-status-slow'),'a slow render uses the warning color');
 result={readAt:now/1000-10,answeredAt:now/1000,renderSeconds:.03,busy:'Measuring…'};
 await refresh.refresh();
 check(line.dataset.state==='Paused' && parts.reason.textContent===result.busy,'a busy answer shows its reason');
 check(parts.age.textContent==='10 s ago','a busy answer records its data time');
 now+=1000;result={...result,readAt:now/1000-2};await refresh.refresh();
 check(parts.age.textContent==='2 s ago','a later busy answer records its new data time');
 load=async()=>{throw new Error('HTTP 502');};await refresh.refresh();
 check(line.dataset.state==='Failed' && parts.reason.textContent==='HTTP 502','a failed read overrides the busy reason');
 result={readAt:now/1000,renderSeconds:.03};load=async()=>result;await refresh.refresh();
 check(line.dataset.state==='Live','an answer without a busy reason clears the busy state');
 let calls=0;load=async()=>{calls++;return result;};visible='hidden';await refresh.refresh();
 check(calls===0 && line.dataset.state==='Paused','a hidden tab does not read');
 visible='visible';handlers.visibilitychange();await settle();check(calls===1,'a visible tab reads at once');
 frameElement.hidden=true;await refresh.refresh();check(calls===1,'a hidden console frame does not read');
 frameElement.hidden=false;
 const pending=[],currentReads=[];load=(value,current)=>{currentReads.push(current);return new Promise(resolve=>pending.push(resolve));};
 const older=refresh.refresh(),skip=refresh.refresh(),newer=refresh.refresh(true);
 check(pending.length===2,'timed reads do not overlap and an explicit read can supersede');
 check(!currentReads[0]() && currentReads[1](),'loaders can refuse an older response before changing content');
 pending[1]({readAt:now/1000,renderSeconds:.04});await newer;
 const following=refresh.refresh();
 check(pending.length===3,'a completed replacement permits a read while the old request remains pending');
 pending[2]({readAt:now/1000,renderSeconds:.04});await following;
 pending[0]({readAt:1,renderSeconds:8});await older;await skip;
 check(parts.render.textContent==='render 0.04 s','an older response cannot replace a newer response');
 if(checks.length) throw new Error(checks.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    done = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=20)
    case.assertEqual(done.returncode, 0, done.stdout + done.stderr)
    case.assertEqual(done.stdout.strip(), 'PASS')


# A probe helper for the page window rule. A press is what a person does: the element on top at the center of
# the New item button takes the focus and the click. A modal window makes the page under it inert, so without
# the rule the press finds the window backdrop.
WINDOW_PROBE = r'''
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const nested=window.parent!==window && window.parent.parent!==window.parent;
const painted=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
async function until(test,description){
 for(let tries=0;tries<250;tries++){if(test()) return;await new Promise(resolve=>setTimeout(resolve,20));}
 throw new Error('Timed out: '+description);
}
const captures=[],realFetch=window.fetch;
if(!nested) window.fetch=async(path,options={})=>{
 if(!String(path).startsWith('/console/review/new-item')) return realFetch(path,options);
 if(!options.body) return Response.json({token:'fixture',page_owner:'owner',
  recipients:[{id:'owner',name:'Page owner',proposal:true}]});
 const body=JSON.parse(options.body);
 if(body.words) return Response.json({id:'Q7'});
 if(body.page_capture) captures.push(body.page_capture);
 return Response.json({});
};
const newItem=()=>document.querySelector('#review-new-item');
const actionsBottom=()=>Math.max(...[...document.querySelector('[data-page-actions]').children]
 .map(control=>control.getBoundingClientRect().bottom));
function onTop(element){
 const box=element.getBoundingClientRect();
 return element.contains(document.elementFromPoint((box.left+box.right)/2,(box.top+box.bottom)/2));
}
function pressNewItem(description){
 const button=document.querySelector('[data-page-actions]:not([data-page-window-actions]) [data-owner-new]');
 const box=button.getBoundingClientRect();
 check(box.top>=0 && box.bottom<=innerHeight && box.left>=0 && box.right<=innerWidth,
  'The New item button is in view '+description+'.');
 const hit=document.elementFromPoint((box.left+box.right)/2,(box.top+box.bottom)/2)?.closest('button');
 check(Boolean(hit?.matches('[data-owner-new]')),'The New item button is on top at its place '+description+'.');
 if(!hit?.matches('[data-owner-new]')) return null;
 const label=hit.getAttribute('aria-label');
 check(label===button.getAttribute('aria-label') && label.startsWith('New item'),'The button keeps its label '+description+'.');
 hit.focus();
 check(document.activeElement===hit,'The New item button takes the focus '+description+'.');
 hit.click();
 return hit;
}
async function newItemOver(under,description){
 await until(()=>captures.length>0,'the New item screenshot request over '+description);
 const dialog=newItem();
 check(dialog.open && dialog.matches(':modal') && onTop(dialog),'New item opens on top of '+description+'.');
 check(under.open,description+' stays open under New item.');
 return captures.at(-1);
}
async function closeNewItem(under,description){
 newItem().querySelector('[data-cancel]').click();
 await painted();
 check(!newItem().open && under.open && under.contains(document.activeElement),
  'Closing New item leaves '+description+' open with the focus in it.');
}
// The screenshot service loads the address and takes the picture at the load event.
async function opensAgain(place,ready,description){
 const frame=document.createElement('iframe');
 frame.style.cssText='border:0;width:'+innerWidth+'px;height:'+innerHeight+'px';
 const loaded=new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
 frame.src=place;document.body.append(frame);
 await loaded;
 check(ready(frame.contentDocument),'The screenshot address opens '+description+' by the load event.');
 check(!frame.contentWindow.location.hash.includes('console-window'),'The page removes the window name from its address.');
 frame.remove();
}
async function report(){await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});}
'''


def window_probe_results(documents: dict[str, bytes], start: str, browser: str) -> list[list[str]]:
    """Serve the documents, show the start path in a 1100 x 700 frame and return the failure lists of the probe."""
    results: list[list[str]] = []
    complete = threading.Event()
    outer = (f'<!doctype html><body style="margin:0"><iframe src="{html.escape(start, quote=True)}" '
             + 'style="border:0;width:1100px;height:700px"></iframe><img hidden src="/complete"></body>').encode()

    class WindowProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.partition('?')[0]
            if path == '/complete':
                _ = complete.wait(TEST_HANG_GUARD_SECS)
                body, kind = b'', 'image/png'
            elif path == '/':
                body, kind = outer, 'text/html; charset=utf-8'
            elif path in documents:
                body = documents[path]
                kind = 'image/svg+xml' if path.endswith('.svg') else 'text/html; charset=utf-8'
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers['Content-Length']))
            if self.path == '/results':
                results.append(cast(list[str], json.loads(body)))
                complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory(prefix='page-window-') as temporary, \
            ThreadingHTTPServer(('127.0.0.1', 0), WindowProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/'
        try:
            # Each browser takes its screenshot after the load event, and /complete holds that event for the results.
            profile, shot = Path(temporary) / 'profile', Path(temporary) / 'page.png'
            profile.mkdir()
            command = (['google-chrome', '--headless', '--no-sandbox', '--disable-gpu', '--no-first-run',
                        f'--user-data-dir={profile}', '--window-size=1440,900', f'--screenshot={shot}', url]
                       if browser == 'chrome' else
                       ['firefox', '--headless', '--no-remote', '--profile', str(profile),
                        '--window-size', '1440,900', '--screenshot', str(shot), url])
            done = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if done.returncode or not complete.is_set():
                raise AssertionError(f'{browser} did not report the window probe: {done.stderr}')
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    return results


PICTURE = (b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
           + b'<rect width="400" height="300" fill="#c33"/></svg>')
WINDOW_BROWSERS = tuple(name for name in ('chrome', 'firefox') if shutil.which('google-chrome' if name == 'chrome' else name))


def window_page(probe: str, actions: tuple[str, ...] = (), before_title: str = '', after_title: str = '') -> bytes:
    """Render a long owned page with a picture link, the New item window, the page actions and a probe."""
    from scripts.review_links import change_page, console_theme, new_item_dialog

    filler = ''.join(f'<p>Page line {index}</p>' for index in range(120))
    content = (before_title + page_frame.title('Images', 'images') + after_title
               + '<p><a href="/fixture/picture.svg" data-image-zoom><img src="/fixture/picture.svg" alt="A red picture" '
               + 'width="80" height="60"></a></p>' + filler)
    markup = change_page.document(content, 'Images').replace(
        b'<html', b'<html data-theme="dark"', 1).replace(b'</head>', b'<style>' + console_theme.CSS.encode()
                                                         + b'</style></head>', 1)
    page = page_frame.with_owner(markup, 'images', PAGE_OWNER, staff.StaffSnapshot((PAGE_OWNER,)),
                                 new_item_dialog.render, actions=actions)
    return page.replace(b'</body>', b'<script>' + WINDOW_PROBE.encode() + probe.encode() + b'</script></body>', 1)


class PageWindowTests(unittest.TestCase):
    """While a page window is open, the action area stays above it and New item works."""

    def run_window_probe(self, probe: str, actions: tuple[str, ...] = (), before_title: str = '',
                         after_title: str = '') -> None:
        if not WINDOW_BROWSERS:
            self.skipTest('Chrome or Firefox is required for the window rule.')
        with status_configuration():
            page = window_page(probe, actions, before_title, after_title)
        for browser in WINDOW_BROWSERS:
            with self.subTest(browser=browser):
                self.assertEqual(window_probe_results({'/fixture/page.html': page, '/fixture/picture.svg': PICTURE},
                                                      '/fixture/page.html', browser), [[]])

    def test_new_item_opens_above_a_picture_and_its_screenshot_shows_the_picture(self) -> None:
        self.run_window_probe(r'''
(async()=>{
 if(nested) return;
 try{
  await painted();
  const link=document.querySelector('a[data-image-zoom]');
  // The copy keeps the layout of the area when the window opens. The full width layout above the title is
  // the case where the copy is a strip across the window.
  document.querySelector('[data-page-actions]').removeAttribute('data-page-actions-beside');
  link.click();
  const picture=document.querySelector('[data-image-zoom-dialog]');
  await painted();
  check(picture.open,'The picture opens.');
  const strip=picture.querySelector('[data-page-window-actions]');
  const name=strip.querySelector('.page-owner-name').getBoundingClientRect();
  check(strip.getBoundingClientRect().left<name.left-40
   && document.elementFromPoint(name.left-20,(name.top+name.bottom)/2)===picture,
   'A click left of the owner name reaches the window backdrop.');
  check([...picture.querySelectorAll('[data-page-window-actions]>*')].every(control=>
   getComputedStyle(control).boxShadow==='none' && getComputedStyle(control).backgroundColor==='rgba(0, 0, 0, 0)'),
   'The copy in the window has no plate and no ring around its controls.');
  check(pressNewItem('over the picture')!==null,'The press reaches New item.');
  const capture=await newItemOver(picture,'the picture');
  check(capture.place===location.pathname+'#console-window='+encodeURIComponent('/fixture/picture.svg'),
   'The screenshot address names the open picture: '+capture.place);
  await closeNewItem(picture,'the picture');
  await opensAgain(capture.place,doc=>doc.querySelector('[data-image-zoom-dialog]')?.open===true,'the picture');
 }catch(error){failures.push(String(error));}
 await report();
})();''')

    def test_new_item_stays_usable_while_new_item_is_open(self) -> None:
        self.run_window_probe(r'''
(async()=>{
 if(nested) return;
 try{
  await painted();
  check(pressNewItem('with no window open')!==null,'The first press reaches New item.');
  await until(()=>captures.length>0,'the first New item screenshot request');
  const dialog=newItem();
  check(dialog.open && onTop(dialog),'New item opens.');
  check(captures[0].place===location.pathname,'With no other window open the screenshot address is the page.');
  const copy=pressNewItem('over New item');
  check(copy!==null && dialog.contains(copy),'The New item button over New item belongs to its window.');
  await painted();
  check(dialog.open && dialog.matches(':modal') && onTop(dialog),'A second press keeps New item open on top.');
  const words=dialog.querySelector('textarea[name="words"]');
  check(document.activeElement===words,'A second press moves the focus to the words box.');
  words.value='A saved item.';
  words.dispatchEvent(new Event('input',{bubbles:true}));
  const send=dialog.querySelector('[type="submit"]');
  await until(()=>!send.disabled,'Send');
  let saved;
  dialog.addEventListener('review-item-created',event=>{saved=event.detail.id;},{once:true});
  send.click();
  await until(()=>saved==='Q7','the saved item ID');
  check(!dialog.open && !document.querySelector('[data-page-window-actions]'),'Send closes New item and removes the copy.');
  check(document.activeElement===document.querySelector('[data-page-actions] [data-owner-new]'),
   'Focus returns to the New item button on the page.');
  // On a phone, New item is taller than the view. It stays centered, and its height clears the copy.
  Object.assign(frameElement.style,{width:'390px',height:'844px'});
  await painted();
  check(pressNewItem('at 390 px')!==null,'The press reaches New item at 390 px.');
  await until(()=>dialog.open,'New item at 390 px');
  await painted();
  for(const [name,control] of [['Close',dialog.querySelector('[data-close]')],['The words box',words],['Send',send]])
   check(onTop(control),'At 390 px the New item control is on top: '+name+'.');
  const box=dialog.getBoundingClientRect();
  check(Math.abs(box.top-(innerHeight-box.bottom))<1 && box.top>=actionsBottom(),
   'At 390 px New item stays centered below the action area.');
  // A picture load or a resize measures New item again. The measure keeps the scroll of its body.
  const body=dialog.querySelector('.new-item-body');
  words.style.minHeight='1000px';
  body.scrollTop=1e4;
  await painted();
  const end=body.scrollTop;
  check(end>0,'At 390 x 844 the New item body scrolls: '+end+'.');
  document.dispatchEvent(new Event('load'));
  dispatchEvent(new Event('resize'));
  await painted();
  check(body.scrollTop===end,'A picture load and a resize keep the scroll of the New item body: '+end+' became '+body.scrollTop+'.');
  // On a desktop, the copy is beside New item, so New item keeps the place and the height of main.
  Object.assign(frameElement.style,{width:'1440px',height:'900px'});
  await painted();
  const wide=dialog.getBoundingClientRect();
  check(Math.abs(wide.top-(innerHeight-wide.bottom))<1 && getComputedStyle(dialog).maxHeight===(innerHeight-32)+'px'
   && (Math.abs(wide.top-16)<1 || wide.height<innerHeight-32),
   'At 1440 x 900 New item has the place and the height limit of main: '+JSON.stringify(wide));
  dialog.querySelector('[data-cancel]').click();
  // A 24-letter owner name makes the controls wider. At 900 px a 10-letter name does not meet New item; this one does.
  document.querySelector('[data-page-actions] .page-owner-name').textContent='Review and console admin';
  Object.assign(frameElement.style,{width:'900px',height:'700px'});
  await painted();
  check(pressNewItem('with a long owner name')!==null,'The press reaches New item with a long owner name.');
  await until(()=>dialog.open,'New item with a long owner name');
  await painted();
  const long=dialog.getBoundingClientRect();
  check(onTop(dialog.querySelector('[data-close]')) && long.top>=actionsBottom()
   && Math.abs(long.top-(innerHeight-long.bottom))<1,
   'With a long owner name at 900 px, Close is on top and New item stays centered below the action area.');
  dialog.querySelector('[data-cancel]').click();
 }catch(error){failures.push(String(error));}
 await report();
})();''')

    def test_a_page_action_sits_before_new_item_and_opens_its_dialog_over_a_window(self) -> None:
        report = page_frame.page_action_button('report', 'list-checks', 'Report', 'report-dialog')
        with self.assertRaisesRegex(ValueError, 'reserved page action: new-item'):
            _ = page_frame.page_action_button('new-item', 'list-checks', 'New item', 'report-dialog')
        dialog = page_frame.console_dialog('report-dialog', 'report-title', 'Report', '<p>The report.</p>')
        self.run_window_probe(r'''
document.body.insertAdjacentHTML('beforeend',REPORT_DIALOG);
(async()=>{
 if(nested) return;
 try{
  await painted();
  const owner=document.querySelector('[data-page-actions] .page-owner');
  const order=[...owner.children].map(control=>control.matches('.page-owner-name') ? 'name'
   : control.matches('[data-page-action="report"]') ? 'report' : control.matches('[data-owner-new]') ? 'new' : '?');
  check(order.join()==='name,report,new','The page action sits between the owner name and New item: '+order.join());
  document.querySelector('a[data-image-zoom]').click();
  const picture=document.querySelector('[data-image-zoom-dialog]');
  await painted();
  const home=owner.querySelector('[data-page-action]').getBoundingClientRect();
  const hit=document.elementFromPoint((home.left+home.right)/2,(home.top+home.bottom)/2)?.closest('button');
  check(picture.open && Boolean(hit) && picture.contains(hit) && hit.matches('[data-page-action="report"]')
   && hit.getAttribute('aria-label')==='Report','The open picture has a copy of the page action on top at its place.');
  if(!hit) throw new Error('The press does not reach the page action.');
  hit.focus();
  hit.click();
  const dialog=document.querySelector('#report-dialog');
  await painted();
  check(dialog.open && onTop(dialog) && picture.open,'The page action opens its dialog above the picture.');
  const own=document.elementFromPoint((home.left+home.right)/2,(home.top+home.bottom)/2)?.closest('button');
  check(Boolean(own) && dialog.contains(own) && own.matches('[data-page-action="report"]'),
   'The open dialog has its own copy of the page action on top.');
  own?.focus();
  own?.click();
  await painted();
  check(dialog.open && document.activeElement===dialog.querySelector('[data-close]'),
   'A press on the copy inside its own open dialog moves the focus to the first control of the dialog.');
  dialog.querySelector('[data-close]').click();
  await painted();
  check(!dialog.open && picture.open && document.activeElement===hit,
   'Closing the dialog leaves the picture open with the focus on the page action copy.');
 }catch(error){failures.push(String(error));}
 await report();
})();'''.replace('REPORT_DIALOG', json.dumps(dialog).replace('<', '\\u003c')), (report,))

    def test_the_frozen_top_is_measured_before_the_content_loads(self) -> None:
        # Record the column width at the first measurement.
        hook = ('<script>{const measure=measurePageActions;window.measurePageActions=()=>{'
                + 'window.columnAtMeasure??=getComputedStyle(document.querySelector(\'main\')).maxWidth;measure();};}'
                + '</script>')
        parsed = ('<script>window.besideAtParse=document.querySelector(\'[data-page-actions]\')'
                  + '.hasAttribute(\'data-page-actions-beside\');</script>')
        self.run_window_probe(r'''
(async()=>{
 if(nested) return;
 try{
  await painted();
  const area=document.querySelector('[data-page-actions]'), title=document.querySelector('h1');
  check(window.besideAtParse===true && area.hasAttribute('data-page-actions-beside'),
   'The area is beside the title before the browser reads the content after the title.');
  check(window.columnAtMeasure==='1124px' && getComputedStyle(document.querySelector('main')).maxWidth==='1124px',
   'The measure script sees the final column width: '+window.columnAtMeasure);
  // Read the layout at once: the frame script measures again before the next frame.
  area.removeAttribute('data-page-actions-beside');
  const controls=Math.max(...[...area.children].map(control=>control.getBoundingClientRect().bottom));
  check(title.getBoundingClientRect().top>=controls,'Without the script the controls stay above the title.');
 }catch(error){failures.push(String(error));}
 await report();
})();''', before_title=hook, after_title=parsed)

    def test_the_frozen_top_keeps_new_item_available_at_the_end_of_a_long_page(self) -> None:
        self.run_window_probe(r'''
(async()=>{
 if(nested) return;
 try{
  await painted();
  const title=document.querySelector('h1'),area=document.querySelector('[data-page-actions]');
  const name=area.querySelector('.page-owner-name'),button=area.querySelector('[data-owner-new]');
  const before=[title,name,button].map(element=>element.getBoundingClientRect());
  check(Math.abs(before[0].top-before[2].top-(before[2].height-before[0].height)/2)<1.5,
   'At scroll zero the title and the button share one row.');
  scrollTo({top:document.scrollingElement.scrollHeight,behavior:'instant'});
  await painted();
  check(scrollY>2000,'The page scrolls far.');
  const frozen=document.querySelector('.page-top').getBoundingClientRect();
  const status=document.querySelector('[data-page-status]').getBoundingClientRect();
  check(frozen.top===0 && title.getBoundingClientRect().top>=0 && status.bottom<=frozen.bottom,
   'The title and status stay inside the frozen top.');
  const after=button.getBoundingClientRect();
  check(Math.abs(after.top-8)<1 && Math.abs(after.right-before[2].right)<1,'The New item button sticks 8 px below the top.');
  check(onTop(name),'The owner name stays in view and on top.');
  check(pressNewItem('at the end of the page')!==null,'The press reaches New item at the end of the page.');
  await until(()=>captures.length>0,'the New item screenshot request');
  const dialog=newItem();
  check(dialog.matches(':modal') && onTop(dialog),'New item opens at the end of the page.');
  const overTop=document.elementFromPoint(frozen.left+2,frozen.top+2);
  check(overTop===dialog || dialog.contains(overTop),'The dialog backdrop covers the frozen top.');
  dialog.querySelector('[data-cancel]').click();
  await painted();
  document.querySelector('a[data-image-zoom]').click();
  const zoom=document.querySelector('[data-image-zoom-dialog]');
  await painted();
  check(zoom.matches(':modal') && zoom.contains(document.elementFromPoint(frozen.left+2,frozen.top+2)),
   'The zoom covers the frozen top.');
 }catch(error){failures.push(String(error));}
 await report();
})();''')


if __name__ == '__main__':
    _ = unittest.main()
