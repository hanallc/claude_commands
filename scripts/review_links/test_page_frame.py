"""The shared page status keeps state, age and request timing together."""
from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from typing import cast, override

from scripts.review_links import console, page_frame, review_submissions, serve
from scripts.review_links.probe_test_support import firefox_probe_results
from scripts.lib import conf
from scripts.production import staff
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

NO_DATA_FILE: dict[str, object] = {}
SCRIPTS = Path(__file__).resolve().parents[1]
STICKY_ACTIONS = b'position:sticky;top:var(--page-actions-space);'
PAGE_OWNER = staff.StaffRow(staff.StaffId("owner"), "showrunner", "Page owner", staff.STAFF_ROOT,
                            current_session=staff.StaffId("session-owner"))
STATUS_PAGES = (
    ("accounts", "Accounts", "users", "Accounts", 45),
    ("aws", "AWS", "cloud", "AWS", 3900),
    ("aws_machines", "AWS Machines", "server", "AWS Machines", 3900),
    ("builds", "Builds", "hammer", "Builds", 15),
    ("disks", "Disks", "hard-drive", "Disks", 600),
    ("images", "Images", "image", "Images", 45),
    ("list", "List", "list-checks", "The List", 45),
    ("review", "Review", "inbox", "Review", 45),
)


@contextmanager
def status_configuration() -> Generator[None]:
    with tempfile.TemporaryDirectory(prefix="page-status-config-") as temporary:
        root = Path(temporary)
        pages = "".join(
            f"page.{name}.label={label}\npage.{name}.icon={icon}\npage.{name}.title={title}\n"
            + f"# range: 0.001..86400\nstatus.{name}.age_limit_seconds={age}\n"
            for name, label, icon, title, age in STATUS_PAGES
        )
        _ = (root / "console.conf").write_text(
            "# range: 0.001..86400\nstatus.round_trip_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.render_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.reading_delay_seconds=1\n"
            + pages)
        _ = (root / "review_links.conf").write_text(
            "# range: 5..300\npage_refresh_seconds=15\n"
            + "# range: 0.5..60\nframe_retry_seconds=2\n")
        with patch.dict(os.environ, {"CONF_DIR": temporary, "USER_ZONE": "America/New_York"}):
            yield


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
        self.assertIn(b'<main><div class="page-heading-actions" data-page-actions><div class="page-owner" data-page-owner="list">',
                      page)
        self.assertIn(b'</div></div><div class="page-heading"><h1>The List</h1>' + page_frame.HEADING_MEASURE.encode()
                      + b'</div></main>', page)
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

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the owner button position.")
    def test_page_owner_stays_visible_above_a_long_open_row(self) -> None:
        page = long_open_row_page()
        reference = json.dumps(main_heading(page).decode()).replace('<', r'\u003c').encode()
        probe = b'''<script>
(async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  frameElement.style.height='800px';
  const button=document.querySelector('[data-owner-new]');
  const owner=button.closest('.page-owner'), name=owner.querySelector('.page-owner-name');
  const actions=document.querySelector('[data-page-actions]'), heading=document.querySelector('.page-heading');
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
  for(const width of [1440,1024,320]){
   const verify=(ok,message)=>check(ok,width+' px: '+message);
   frameElement.style.width=width+'px';
   reference.style.width=width+'px';
   scrollTo({top:0,behavior:'instant'});
   await painted();
   const first=JSON.stringify(boxes(document)), layout=columns(), titleTop=title.getBoundingClientRect().top;
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
   const last=owner.getBoundingClientRect(), status=banner.getBoundingClientRect(), headingBox=heading.getBoundingClientRect();
   const nameBox=name.getBoundingClientRect(), buttonBox=button.getBoundingClientRect();
   verify(innerWidth===width && scrollY>innerHeight && scrollY===bottom(),'The page scrolls to the bottom of the long row.');
   const state=banner.querySelector('.page-status-state').getBoundingClientRect();
   verify(Math.abs(status.top-last.bottom)<1 && reason.getBoundingClientRect().top>=state.bottom,
    'The sticky status spans two lines directly below the action area.');
   verify(Math.abs(last.top-8)<1 && actions.contains(owner),'The action area sticks 8 px below the top.');
   verify(Math.abs(title.getBoundingClientRect().top-(titleTop-scrollY))<1 && headingBox.bottom<0,
    'The title scrolls with the page.');
   verify(Math.abs(last.right-headingBox.right)<1 && last.bottom<=status.top+0.5 && last.bottom<=innerHeight,
    'The owner stays at the top right above the status.');
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
    row.getBoundingClientRect().top>=banner.getBoundingClientRect().bottom &&
    banner.getBoundingClientRect().top>=owner.getBoundingClientRect().bottom-0.5,
    'The item jump from scroll zero focuses the row below the action area and the status.');
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
  const space=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--page-actions-space'));
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
   for(const width of [1440,320]){
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
     verify(row.top>=space-0.5,'The action area stays at least 8 px below the top.',top);
     verify(layout()===start,'The content does not move.',top);
     if(previous){
      // Each part moves up by at most the scroll distance and never moves down, so nothing steps.
      const distance=top-previous.top;
      verify([[row.top,previous.row],[status.top,previous.status]].every(([now,before])=>before-now>=-0.5 && before-now<=distance+0.5),
       'The action area and the status line move by no more than the scroll distance.',top);
     }
     previous={top,row:row.top,status:status.top};
    }
    verify(Math.abs(owner.getBoundingClientRect().top-space)<1 && title.getBoundingClientRect().bottom<0
     && Math.abs(banner.getBoundingClientRect().top-owner.getBoundingClientRect().bottom)<1,
     'At the bottom the title is out of view, the action area sticks 8 px below the top and the status line sticks directly below it.',bottom());
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(long_open_row_page().replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the AWS region filter.")
    def test_aws_region_filter_sticks_below_the_action_area(self) -> None:
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
  const owner=document.querySelector('.page-owner'), regions=document.querySelector('nav.regions');
  const banner=document.querySelector('[data-page-status]');
  for(const state of ['Live','Late']){
   for(const width of [1440,320]){
    frameElement.style.width=width+'px';
    banner.dataset.state=state;
    scrollTo({top:2000,behavior:'instant'});
    await painted();
    const bar=regions.getBoundingClientRect(), row=owner.getBoundingClientRect();
    // A Late status line sticks directly below the action area, so the filter sticks below the status line.
    const above=state==='Late' ? banner.getBoundingClientRect().bottom : row.bottom;
    const hit=document.elementFromPoint((bar.left+bar.right)/2,bar.top+1);
    if(scrollY<1000 || Math.abs(bar.top-above)>=1 || bar.bottom>innerHeight || !regions.contains(hit)){
     failures.push(width+' px, '+state+': the region filter must stick fully in view below the action area and the status and take a click at its top.');
    }
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(markup.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the Builds paused badge.")
    def test_builds_paused_badge_matches_main_and_stays_visible(self) -> None:
        from scripts.build_queue import memory_feed
        from scripts.review_links import console_theme

        source = console_theme.with_theme((SCRIPTS / "build_queue" / "memory_live.html").read_text(encoding="utf-8"))
        page = (page_frame.template(source, "Builds", "builds").replace("__MEMORY_FEED_INTERVAL_SECONDS__", "15")
                .replace("__BUILD_LIMIT_ICONS__", json.dumps(memory_feed.build_limit_icons())))
        markup = page_frame.with_owner(page.encode(), "builds", PAGE_OWNER, staff.StaffSnapshot((PAGE_OWNER,)), lambda: "")
        # The page stays paused, so the status line does not stick and nothing moves.
        paused = (b'<script>document.addEventListener("DOMContentLoaded",()=>setPaused(true));</script>'
                  + b'<style>*,*::before,*::after{animation:none!important;transition:none!important}</style></head>')
        markup = markup.replace(b'</head>', paused, 1)
        still = markup.replace(b'<head>', b'<head><script>window.fetch=()=>new Promise(()=>{});</script>', 1)
        for width in (1440, 320):
            with self.subTest(width=width):
                self.assertTrue(firefox_screenshot(still, width, 800) == firefox_screenshot(main_heading(still), width, 800),
                                "At scroll zero the paused page must match the page without the sticky action area.")
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

    def test_a_page_can_show_busy_and_failure_without_waiting_for_a_read(self) -> None:
        options = {"ageLimit": 60, "readingDelay": .2, "roundTripLimit": 1, "renderLimit": 1}
        source = self.environment(json.dumps(options)) + page_frame.REFRESH_SCRIPT + r"""
(async()=>{
 const check=(ok,reason)=>{if(!ok) throw new Error(reason);};
 const refresh=createPageRefresh(async()=>({readAt:now/1000,renderSeconds:.03}));
 refresh.setStatus('Measuring…');
 check(line.dataset.state==='Paused' && parts.reason.textContent==='Measuring…',
  'Busy status must appear before the first read.');
 refresh.setStatus('','Launch failed.');
 check(line.dataset.state==='Failed' && parts.reason.textContent==='Launch failed.',
  'A failure must appear without a read.');
 await refresh.refresh();
 check(line.dataset.state==='Live','A good read must clear the failure.');
 now+=61000;refresh.changed();
 check(line.dataset.state==='Late','An unanswered page must become Late.');
 refresh.setStatus('Measuring…');
 check(line.dataset.state==='Paused' && parts.age.textContent==='1 min ago',
  'Busy status must appear at once and keep the data age.');
 refresh.setStatus('','The measure did not finish.');
 refresh.changed();
 check(line.dataset.state==='Failed' && parts.reason.textContent==='The measure did not finish.',
  'The age clock must keep the failure.');
 refresh.setStatus('');
 check(line.dataset.state==='Live','Clearing the status must clear the busy text and failure.');
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


def main_heading(page: bytes) -> bytes:
    """Remove the sticky rule of the action area, so the page shows the heading as it is before a scroll."""
    assert page.count(STICKY_ACTIONS) == 1
    return page.replace(STICKY_ACTIONS, b'', 1)


def firefox_screenshot(page: bytes, width: int, height: int) -> bytes:
    """Load one page in a headless Firefox window and return its PNG."""
    with tempfile.TemporaryDirectory(prefix="page-frame-shot-") as temporary:
        root = Path(temporary)
        (root / "profile").mkdir()
        document = root / "page.html"
        _ = document.write_bytes(page)
        run = subprocess.run(["firefox", "--headless", "--no-remote", "--profile", str(root / "profile"),
                              "--window-size", f"{width},{height}", "--screenshot", str(root / "page.png"),
                              document.as_uri()],
                             capture_output=True, text=True, timeout=45,
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



def assert_status_states(case: unittest.TestCase, markup: str, period_seconds: float,
                         reading: dict[str, object] = NO_DATA_FILE, failed_markup: str = "") -> None:
    """Run the complete page with its loader and its configured request period."""
    setup = r'''<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, intervals=new Map();
let timerId=0, wall=Date.now(), perf=0, statusVisibility='visible', mode='good', reads=0, measureFails=false, saveFails=false;
const pending=[], errors=[];
const original=MARKUP, savedReading=READING, expectedPeriod=PERIOD, failedPage=FAILED_PAGE;
if(original.includes('<title>Builds</title>')) mode='retained';
if(original.includes('<title>Disks</title>')) measureFails=true;
Date.now=()=>wall;
Object.defineProperty(performance,'now',{value:()=>perf});
Object.defineProperty(document,'visibilityState',{get:()=>statusVisibility});
window.setInterval=(run,ms)=>{intervals.set(++timerId,{run,ms});return timerId;};
window.clearInterval=key=>intervals.delete(key);
window.requestAnimationFrame=run=>nativeTimeout(()=>run(perf),0);
addEventListener('error',event=>errors.push(event.message));
addEventListener('unhandledrejection',event=>errors.push(String(event.reason)));
const payload=()=>({...savedReading,read_at:wall/1000,measured_at:wall/1000,render_seconds:.42});
function response(status=200){
 const doc=new DOMParser().parseFromString(mode==='cached' ? failedPage : original,'text/html');
 const facts=doc.querySelector('[data-page-status]');
 facts.dataset.readAt=wall/1000;facts.dataset.renderSeconds=.42;
 const root=doc.querySelector('[data-accounts],[data-review-content],[data-images],[data-investigations],[data-list],[data-review]');
 if(root) root.dataset.loaded='true';
 const aws=doc.querySelector('#aws-prices-data');
 if(aws) aws.textContent=JSON.stringify({...JSON.parse(aws.textContent),read_at:wall/1000,render_seconds:.42});
 return {ok:status===200,status,text:async()=>doc.documentElement.outerHTML,json:async()=>payload(),
  headers:{get:()=>null}};
}
window.fetch=async(url,options={})=>{
 if(options.method==='HEAD') return response();
 if(String(url).includes('report.json')) return {ok:false,status:404};
 // The Disks page also reads the Builds data; the status checks count only the page's own reads.
 if(original.includes('<title>Disks</title>') && String(url).includes('memory.json')) return {ok:false,status:404};
 if(options.method==='POST' && String(url)!=='measure')
  return saveFails ? {...response(502),json:async()=>({reason:'HTTP 502'})} : response();
 if(String(url)==='measure') return {ok:!measureFails,status:measureFails ? 502 : 200,
  json:async()=>({state:'current',reason:measureFails ? 'HTTP 502' : ''})};
 reads++;
 if(mode==='pending') return new Promise(resolve=>pending.push(resolve));
 if(mode==='failed') return response(502);
 if(mode==='missing') return response(404);
 if(mode==='retained') return {...response(),json:async()=>({...payload(),read_error:'queue unavailable'})};
 if(mode==='invalid') return {...response(),text:async()=>
  (await response().text()).replace('data-render-seconds="0.42"','data-render-seconds="bad"')};
 return response();
};
</script>'''
    setup = (setup.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
             .replace('READING', json.dumps(reading).replace('</', '<\\/'))
             .replace('PERIOD', str(period_seconds * 1000))
             .replace('FAILED_PAGE', json.dumps(failed_markup).replace('</', '<\\/')))
    probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>nativeTimeout(resolve,0))));
 const line=document.querySelector('[data-page-status]');
 const part=name=>line.querySelector('[data-status-'+name+']');
 const age=()=>part('age').textContent;
 const state=()=>line.dataset.state;
 const measure=line.querySelector('#measure-now');
 const checkMeasure=()=>{
  if(document.title!=='Disks') return;
  const bar=line.getBoundingClientRect(),button=measure?.getBoundingClientRect();
  check(measure===line.querySelector('#measure-now') && measure?.checkVisibility() &&
   measure.innerText.trim()==='Measure now' && button && Math.abs(button.right-bar.right)<1 &&
   button.top>=bar.top && button.bottom<=bar.bottom,
   'the '+state()+' line shows the same labeled Measure now button at its right edge');
 };
 const shown=()=>dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 const requestTimer=()=>[...intervals.values()].find(timer=>timer.ms===expectedPeriod);
 const clock=()=>[...intervals.values()].find(timer=>timer.ms===1000);
 const read=async()=>{if(expectedPeriod){perf+=expectedPeriod+1;requestTimer().run();}else shown();await flush();};
 try{
  checkMeasure();
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  await flush();
  if(mode==='retained' || measureFails){
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a first failed read with shown data keeps its data time');
   mode='good';measureFails=false;
  }
  shown();await flush();
  check(reads>0 && state()==='Live','the real loader completes a good read');
  checkMeasure();
  check(part('render').textContent==='render 0.42 s','the real loader supplies render time');
  check(!part('trip').hidden && /^round trip [\d.]+ s$/.test(part('trip').textContent),
   'the Live line shows round trip before render time');
  const positions=[...line.children].filter(node=>!node.hidden && !node.matches('[data-status-announcement]'));
  check(positions.length===4 && positions[0].matches('.page-status-state') && positions[1]===part('age') &&
   positions[2]===part('trip') && positions[3]===part('render'),'the Live line has the same four positions');
  const title=document.querySelector('.page-title'),titleBox=title.getBoundingClientRect(),lineBox=line.getBoundingClientRect();
  const headingBox=(title.closest('.page-heading') || title).getBoundingClientRect();
  const main=document.querySelector('main'),mainBox=main.getBoundingClientRect(),width=document.documentElement.clientWidth;
  // A wide page uses the pane width; a text page keeps the 1124 px column.
  check(mainBox.width===(main.classList.contains('page-wide')?width:Math.min(width,1124)) &&
   Math.abs(mainBox.left-(width-mainBox.right))<1,'the page uses the shared centered column');
  check(main.classList.contains('page-wide')===(document.title==='Builds'),'only the Builds page is wide');
  check(titleBox.x===mainBox.x+32 && headingBox.y+scrollY===32,'the title starts at the shared column edge');
  const searchField=main.querySelector('[data-review] #list-search');
  const search=searchField?.getBoundingClientRect();
  if(search){
   check(lineBox.x===titleBox.x && search.top===headingBox.bottom+8,'the status and search row follows the title');
   check(lineBox.right<=search.left && Math.abs((lineBox.top+lineBox.bottom)-(search.top+search.bottom))<2,
    'the live status and search share the row');
  }else{
   check(lineBox.x===titleBox.x && lineBox.y===headingBox.bottom+8,'the line follows the title row in the same column');
   check(lineBox.width===headingBox.width,'the line uses the full title row width');
  }
  check(lineBox.height===21,'the line uses the shared height');
  check(getComputedStyle(title).fontSize==='26px' && getComputedStyle(title).fontWeight==='700',
   'the title uses the shared size and weight');
  check(getComputedStyle(line).fontSize==='14px' && getComputedStyle(line).lineHeight==='21px' &&
   getComputedStyle(line).marginBottom===(search ? '0px' : '20px'),'the line uses the shared font and the expected lower space');
  const ink=document.createElement('span');ink.style.color='var(--ink)';line.append(ink);
  check(getComputedStyle(title).color===getComputedStyle(ink).color,'the title uses the shared ink color');ink.remove();
  check(getComputedStyle(document.body).backgroundColor===getComputedStyle(line).backgroundColor,
   'the line uses the page background without a separate band');
  const content=document.querySelector('[data-review-content],[data-investigations],[data-image-layout],[data-list],[data-review]');
  if(content) check(content.getBoundingClientRect().x===titleBox.x,'the page content starts at the title edge');
  const icon=line.querySelector('[data-status-icon=Live] svg').getBoundingClientRect();
  check(icon.width===16 && icon.height===16,'the Live icon uses the shared size');
  const root=document.querySelector('[data-accounts],[data-review-content],[data-images],[data-investigations],[data-list],[data-review]');
  if(root) check(root.dataset.loaded==='true','the real loader replaces page content');
  if(expectedPeriod) check(!!requestTimer(),'the real request period runs');
  else check([...intervals.values()].every(timer=>timer.ms===1000),'the zero-period page has only its age timer');
  const checkReviewSticky=()=>{
   if(!searchField) return;
   const spacer=document.createElement('div');spacer.style.height='200vh';
   root.querySelector('.review-queue').append(spacer);
   const top=scrollY;
   scrollTo({top:innerHeight,behavior:'instant'});
   check(scrollY>0 && title.getBoundingClientRect().bottom<0 && Math.abs(line.getBoundingClientRect().top)<1,
    'The '+state()+' status stays visible when the Review queue scrolls.');
   scrollTo({top,behavior:'instant'});spacer.remove();
  };
  const count=reads;
  wall+=(JSON.parse(line.dataset.statusOptions).ageLimit+65)*1000;clock().run();
  check(state()==='Late' && age().includes('min ago'),'the shown data becomes Late');
  checkReviewSticky();
  checkMeasure();
  check(reads===count,'the age clock does not read');
  mode='failed';await read();
  check(state()==='Failed' && !part('age').hidden && part('reason').textContent==='HTTP 502',
   'a real failed read retains the shown data time');
  checkMeasure();
  checkReviewSticky();
  if(searchField){
   const originalWidth=frameElement.style.width,reason=part('reason'),originalReason=reason.textContent;
   frameElement.style.width='801px';
   reason.textContent='Could not read /fixtures/'+ 'unavailable-source-path'.repeat(20);
   const bounds=reason.getBoundingClientRect(),searchBox=searchField.getBoundingClientRect();
   check(innerWidth===801 && bounds.height>21 && bounds.right<=searchBox.left &&
    reason.scrollWidth<=reason.clientWidth && document.documentElement.scrollWidth<=innerWidth,
    'A long error wraps without overlapping Search just above the layout breakpoint.');
   frameElement.style.width=originalWidth;reason.textContent=originalReason;
  }
  mode='good';await read();
  check(state()==='Live','a later real read recovers');
  if(document.querySelector('[data-review-content]')){
   const field=document.querySelector('textarea,input[type=text]');
   if(field){field.value='Keep this text.';field.dispatchEvent(new Event('input',{bubbles:true}));
    await read();check(state()==='Paused','a real review field holds the page');
    field.value='';field.dispatchEvent(new Event('input',{bubbles:true}));await flush();
    check(state()==='Live','clearing the review field applies its held page');}
  }
  if(expectedPeriod){
   mode='pending';await read();shown();await flush();
   check(pending.length===2,'a visible page supersedes the unresolved read');
   pending[1](response());await flush();
   const before=reads;await read();
   check(reads===before+1,'the next scheduled read ignores the superseded request');
   pending[2]?.(response());await flush();pending[0](response());await flush();mode='good';
  }
  const failedSaveForm=document.querySelector('[data-read-form]');
  if(failedSaveForm){
   mode='pending';saveFails=true;const first=pending.length;await read();
   failedSaveForm.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
   check(failedSaveForm.querySelector('[data-save-status]').textContent==='Not saved: HTTP 502',
    'the failed investigation save retains its error');
   const before=reads;await read();
   check(reads===before+1,'a failed investigation save permits the next scheduled read while the older read remains pending');
   pending[first+1]?.(response());await flush();pending[first](response());await flush();
   mode='good';saveFails=false;
  }
  const readForm=document.querySelector('[data-read-form]');
  if(readForm){
   mode='pending';const first=pending.length;await read();
   readForm.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
   check(pending.length===first+2,'a saved investigation supersedes its older read');
   pending[first+1](response());await flush();
   const before=reads;await read();
   check(reads===before+1,'a saved investigation permits a scheduled read while the older read remains pending');
   pending[first+2]?.(response());await flush();pending[first](response());await flush();mode='good';
  }
  if(root){
   mode='invalid';await read();
   check(state()==='Failed' && part('reason').textContent==='The data has no render time.',
    'a real HTML read rejects an invalid render time');
   mode='good';await read();
  }
  if(failedPage){
   mode='cached';await read();
   check(state()==='Failed' && !part('age').hidden && part('reason').textContent==='The Accounts render failed.',
    'a cached Accounts response reports its render failure');
   check(document.querySelector('[data-accounts]').textContent.includes('board'),
    'the Accounts board stays visible after a render failure');
   mode='good';await read();check(state()==='Live','a successful Accounts render clears failure');
  }
  if(document.title==='Builds'){
   mode='retained';await read();
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a retained Builds reading has its data time after a queue failure');
   check(document.querySelector('#v-cap').textContent!=='unavailable','the retained Builds data remains visible');
  }
  if(document.title==='Disks'){
   check(document.querySelector('#drives .card')!==null,'the cached drive remains visible');
   mode='missing';const kept=document.querySelector('#drives').innerHTML;await read();
   check(state()==='Failed' && part('reason').textContent==='HTTP 404','a missing Disks file reports failure');
   check(document.querySelector('#drives').innerHTML===kept,'a missing Disks file keeps the shown drives');
   mode='good';measureFails=true;await read();
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a failed Disks measure retains the time of the shown data');
  }
  statusVisibility='hidden';document.dispatchEvent(new Event('visibilitychange'));await flush();
  check(state()==='Paused','a hidden page pauses');
  checkMeasure();
 }catch(error){failures.push(String(error));}
 failures.push(...errors);
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
    page = markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
    match = re.search(r'<title>([^<]+)</title>', markup)
    assert match is not None
    identity = 'list' if match[1] == 'The List' else match[1].split()[0].lower()
    if identity in ('changes', 'questions', 'investigations'):
        identity = 'review'
    with status_configuration():
        settings = conf.read("console").values()
        pages = tuple(console.ConsolePage(name, settings[f"page.{name}.label"], settings[f"page.{name}.icon"],
                                         settings[f"page.{name}.title"], console.AvailablePage('/page'))
                      for name, *_ in STATUS_PAGES)
        shell = console.render(pages, console.UnavailablePage()).replace(b'</body>', b'<img hidden src="/complete"></body>')
    results: list[list[str]] = []
    complete = threading.Event()

    class BrowserProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            if self.path == '/complete':
                _ = complete.wait(TEST_HANG_GUARD_SECS)
            elif self.path.startswith('/?page='):
                _ = self.wfile.write(shell)
            else:
                _ = self.wfile.write(page)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers['Content-Length']))
            if self.path != '/results':
                self.send_response(200)
                self.end_headers()
                _ = self.wfile.write(b'{"new":[]}')
                return
            results.append(cast(list[str], json.loads(body)))
            complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(('127.0.0.1', 0), BrowserProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / 'profile'
            profile.mkdir()
            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                                   '--window-size', '1600,900',
                                   '--screenshot', str(Path(temporary) / 'page.png'),
                                   f'http://127.0.0.1:{server.server_port}/?page={identity}'],
                                  capture_output=True, text=True, timeout=45)
            case.assertEqual(done.returncode, 0, done.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    case.assertEqual(results, [[]], 'The real page loader must complete every status check.')


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
  const words=dialog.querySelector('textarea[name="words"]'),done=dialog.querySelector('[data-done]');
  check(document.activeElement===words,'A second press moves the focus to the words box.');
  words.value='A saved item.';
  words.dispatchEvent(new Event('input',{bubbles:true}));
  const send=dialog.querySelector('[type="submit"]');
  await until(()=>!send.disabled,'Send');
  send.click();
  await until(()=>dialog.querySelector('#new-item-saved').textContent==='Saved Q7.' && !words.checkVisibility(),
   'the Saved state');
  check(pressNewItem('over the saved item')!==null,'The press reaches New item in the Saved state.');
  await painted();
  check(dialog.open && document.activeElement===done,'In the Saved state a press moves the focus to Done.');
  check(dialog.getBoundingClientRect().top>=actionsBottom(),'New item starts below the action area.');
  dialog.querySelector('[data-cancel]').click();
  await painted();
  check(!dialog.open && !document.querySelector('[data-page-window-actions]'),'Closing New item removes the copy.');
  check(document.activeElement===document.querySelector('[data-page-actions] [data-owner-new]'),
   'Focus returns to the New item button on the page.');
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

    def test_the_heading_row_is_right_before_the_script_and_does_not_move_on_load(self) -> None:
        # The hook records the column width when the script in the heading row measures it the first time.
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

    def test_only_the_action_area_stays_in_view_at_the_end_of_a_long_page(self) -> None:
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
  check(title.getBoundingClientRect().bottom<0,'The title scrolls out of view.');
  const after=button.getBoundingClientRect();
  check(Math.abs(after.top-8)<1 && Math.abs(after.right-before[2].right)<1,'The New item button sticks 8 px below the top.');
  check(onTop(name),'The owner name stays in view and on top.');
  check(pressNewItem('at the end of the page')!==null,'The press reaches New item at the end of the page.');
  await until(()=>captures.length>0,'the New item screenshot request');
  check(newItem().open && onTop(newItem()),'New item opens at the end of the page.');
 }catch(error){failures.push(String(error));}
 await report();
})();''')


if __name__ == '__main__':
    _ = unittest.main()
