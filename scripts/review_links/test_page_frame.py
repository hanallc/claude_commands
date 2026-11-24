"""The shared page status keeps state, age and request timing together."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from typing import override

from scripts.review_links import page_frame, review_submissions, serve
from scripts.lib import conf
from scripts.production import staff
from scripts.review_links.browser_test_support import STATUS_PAGES, firefox_probe_results, status_configuration

SCRIPTS = Path(__file__).resolve().parents[1]
STICKY_HEADING = re.compile(rb':is\(\.page-heading,main>\.page-heading-actions\)\{[^}]*\}')
PAGE_OWNER = staff.StaffRow(staff.StaffId("owner"), "showrunner", "Page owner", staff.STAFF_ROOT,
                            current_session=staff.StaffId("session-owner"))


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
        self.assertIn(b'<div class="page-heading"><h1>The List</h1><div class="page-heading-actions">', page)
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
  const heading=document.querySelector('.page-heading');
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
  const boxes=doc=>['.page-owner-name','[data-owner-new]'].map(selector=>{
   const {x,y,width,height}=doc.querySelector(selector).getBoundingClientRect();
   return [x,y,width,height];
  });
  const columns=()=>JSON.stringify([main,table].map(element=>{
   const {left,width}=element.getBoundingClientRect();
   return [left,width];
  }));
  const bottom=()=>document.scrollingElement.scrollHeight-innerHeight;
  const line=()=>getComputedStyle(heading).boxShadow, lineColor=getComputedStyle(button).borderTopColor;
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
   const first=JSON.stringify(boxes(document)), layout=columns(), headingHeight=heading.getBoundingClientRect().height;
   verify(first===JSON.stringify(boxes(reference.contentDocument)),
    'The name and button match the original heading at scroll zero.');
   const background=getComputedStyle(heading).backgroundColor;
   verify(opaque(background) && background===getComputedStyle(document.body).backgroundColor,
    'The heading row has the opaque page background.');
   verify(line()==='none' && !heading.hasAttribute('data-page-heading-stuck'),'The heading row has no line at scroll zero.');
   const statusEnd=Math.ceil(banner.getBoundingClientRect().bottom+headingHeight);
   for(let top=0;top<=statusEnd;top+=2){
    scrollTo({top,behavior:'instant'});
    await painted();
    if(statusCovered()){verify(false,'The heading row covers the Late status at scroll '+top+'.');break;}
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
   verify(Math.abs(status.top-headingBox.bottom)<1 && reason.getBoundingClientRect().top>=state.bottom,
    'The sticky status spans two lines directly below the heading row.');
   verify(Math.abs(headingBox.top-8)<1 && heading.contains(title),'The heading row sticks 8 px below the top.');
   verify(Math.abs(last.right-headingBox.right)<1 && last.top>=headingBox.top && last.bottom<=status.top
    && last.bottom<=innerHeight,'The owner stays at the top right above the status.');
   verify(nameBox.width>0 && Math.abs(buttonBox.left-nameBox.right-8)<0.1 && buttonBox.width===36,
    'The name stays beside the full button.');
   verify(detail.getBoundingClientRect().top<headingBox.top,'The open row scrolls under the heading row.');
   verify(heading.hasAttribute('data-page-heading-stuck') && line().includes(lineColor) && line().includes('0px 1px 0px'),
    'The stuck heading row shows the header line.');
   verify(headingBox.height===headingHeight,'The line does not change the heading row height.');
   let hidden=true;
   for(let x=Math.ceil(last.left);x<last.right-1;x+=4){
    for(let y=Math.ceil(last.top);y<last.bottom-1;y+=4){
     hidden=hidden && heading.contains(document.elementFromPoint(x,y));
    }
   }
   verify(hidden,'No table text shows under the owner.');
   const x=(buttonBox.left+buttonBox.right)/2;
   verify(button.contains(document.elementFromPoint(x,buttonBox.top+1))
    && button.contains(document.elementFromPoint(x,buttonBox.bottom-1)),'The whole button is clickable.');
   scrollTo({top:0,behavior:'instant'});
   await painted();
   verify(JSON.stringify(boxes(document))===first && line()==='none','The original heading returns at scroll zero.');
   verify(view.revealMatchingItem('open'),'The item jump finds the row.');
   await painted();
   verify(document.activeElement===row.querySelector('button') && scrollY>0 &&
    row.getBoundingClientRect().top>=banner.getBoundingClientRect().bottom &&
    banner.getBoundingClientRect().top>=heading.getBoundingClientRect().bottom,
    'The item jump from scroll zero focuses the row below the heading row and the status.');
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
  const heading=document.querySelector('.page-heading'), banner=document.querySelector('[data-page-status]');
  const name=document.querySelector('.page-owner-name'), button=document.querySelector('[data-owner-new]');
  const main=document.querySelector('main'), table=document.querySelector('table');
  const reason=banner.querySelector('[data-status-reason]');
  const bottom=()=>document.scrollingElement.scrollHeight-innerHeight;
  const space=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--page-heading-space'));
  const background=getComputedStyle(document.body).backgroundColor;
  const opaque=color=>{const parts=color.match(/[0-9.]+/g);return parts!==null && (parts.length===3 || Number(parts[3])===1);};
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
  const band=()=>getComputedStyle(heading,'::before');
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
     const row=heading.getBoundingClientRect(), status=banner.getBoundingClientRect();
     const stuck=heading.hasAttribute('data-page-heading-stuck');
     verify(scrollY===top && innerWidth===width,'The page scrolls to the position.',top);
     verify(clear(name,1),'The owner name is fully in view and not covered.',top);
     verify(clear(button,3),'The New item button is fully in view and not covered.',top);
     verify(statusShown(),'The status line is fully in view and not covered by the row.',top);
     verify(row.bottom<=status.top+0.5,'The row stays above the status line.',top);
     verify(layout()===start,'The content does not move.',top);
     if(previous){
      // Each part moves up by at most the scroll distance and never moves down, so nothing steps.
      const distance=top-previous.top;
      verify([[row.top,previous.row],[status.top,previous.status]].every(([now,before])=>before-now>=-0.5 && before-now<=distance+0.5),
       'The row and the status line move by no more than the scroll distance.',top);
     }
     previous={top,row:row.top,status:status.top};
     if(top===0) verify(!stuck && band().content==='none','The row is not stuck and has no space band at scroll zero.',top);
     if(stuck){
      verify(Math.abs(row.top-space)<1,'The stuck row is 8 px below the top.',top);
      const fill=band().backgroundColor;
      verify(band().content!=='none' && parseFloat(band().height)===space && opaque(fill) && fill===background,
       'The stuck row has an opaque 8 px band above it.',top);
      verify(heading.contains(document.elementFromPoint((row.left+row.right)/2,row.top-space/2)),
       'The band covers the content above the stuck row.',top);
     }else{
      verify(band().content==='none','A row that is not stuck has no space band.',top);
     }
    }
    verify(heading.hasAttribute('data-page-heading-stuck') && Math.abs(heading.getBoundingClientRect().top-space)<1
     && Math.abs(banner.getBoundingClientRect().top-heading.getBoundingClientRect().bottom)<1,
     'At the bottom the row sticks 8 px below the top and the status line sticks directly below it.',bottom());
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
        self.assertEqual(firefox_probe_results(long_open_row_page().replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the AWS region filter.")
    def test_aws_region_filter_sticks_below_the_heading_row(self) -> None:
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
  const heading=document.querySelector('.page-heading'), regions=document.querySelector('nav.regions');
  const banner=document.querySelector('[data-page-status]');
  for(const state of ['Live','Late']){
   for(const width of [1440,320]){
    frameElement.style.width=width+'px';
    banner.dataset.state=state;
    scrollTo({top:2000,behavior:'instant'});
    await painted();
    const bar=regions.getBoundingClientRect(), row=heading.getBoundingClientRect();
    // A Late status line sticks directly below the heading row, so the filter sticks below the status line.
    const above=state==='Late' ? banner.getBoundingClientRect().bottom : row.bottom;
    const hit=document.elementFromPoint((bar.left+bar.right)/2,bar.top+1);
    if(scrollY<1000 || Math.abs(bar.top-above)>=1 || bar.bottom>innerHeight || !regions.contains(hit)){
     failures.push(width+' px, '+state+': the region filter must stick fully in view below the heading row and the status and take a click at its top.');
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
                                "At scroll zero the paused page must match the page without the sticky heading row.")
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
    """Remove the sticky heading row, so the page shows the heading as it was before."""
    assert STICKY_HEADING.search(page) is not None
    return STICKY_HEADING.sub(b'', page.replace(page_frame.HEADING_POSITION.encode(), b'', 1))


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


if __name__ == '__main__':
    _ = unittest.main()
