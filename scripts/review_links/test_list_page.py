"""Test The List page with private records and a real browser."""
from __future__ import annotations
from scripts.lint.memory_test_support import write_config_fixture

import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, cast, override
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from scripts.accounts import accounts
from scripts.agents.remote import local_machine
from scripts.lib import conf, process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import send_result
from scripts.review_links.test_page_question import FakeQuestionTransport, question_outcome
from scripts.production import change_review, change_routing, delivery_state, investigation, session_staff, showrunners, staff, the_list
from scripts.production.update_registration import QuestionStaff
from scripts.review_links import console, list_page, serve, staff_names
from scripts.review_links import change_browser, change_page, page_question, question_record
from scripts.review_links import review_items, review_page
from scripts.review_links.test_change_page import BrowserForms
from scripts.review_links.test_page_frame import assert_status_states
from scripts.review_links.test_record_table import save_notice_failures
from scripts.review_links.test_staff_names import without_role_colors
from scripts.statusline import counts_row, rebuild_row

MORTY = staff.StaffRow(staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"), "producer", "morty", staff.StaffRoot(),
                        current_session=staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"))
OWNER = staff.StaffRow(staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"), "showrunner", "sdlc", staff.StaffRoot(),
                        current_session=staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"))
OTHER = staff.StaffRow(staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8"), "showrunner", "builds", staff.StaffRoot(),
                        current_session=staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8"))
DWIGHT = staff.StaffRow(staff.StaffId("920b0a64-6eee-479e-8c07-e03e4dcd4ef6"), "producer", "dwight", staff.StaffRoot(),
                        current_session=staff.StaffId("920b0a64-6eee-479e-8c07-e03e4dcd4ef6"))
NOW = datetime(2026, 11, 16, 18, tzinfo=UTC)


class ListPageTests(unittest.TestCase):
    root: Path = Path()

    def test_review_links_keep_the_console_and_jump_to_cards_in_every_state(self) -> None:
        source = review_items.ProposalLedgerSource(self.root / "proposals.md")
        ready = review_items.ReviewItem("proposal/sdlc/P10", "sdlc/P10", review_items.ReviewType.PROPOSAL,
            OWNER.id, OWNER.id, "Check this proposal", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), source)
        for state in review_items.ReviewState:
            with self.subTest(state=state):
                review = replace(ready, state=state)
                link = ElementTree.fromstring(list_page.review_card(review.key, {review.key: review}))
                query = parse_qs(urlsplit(link.attrib["href"]).query)
                self.assertEqual(query["page"], ["review"])
                place = urlsplit(query["at.review"][0])
                self.assertEqual(place.path, review_page.PAGE_PATH)
                self.assertEqual(parse_qs(place.query), {"show": ["all"]})
                self.assertEqual(parse_qs(place.fragment), {"show": ["open"], "jump": [review.key]})
                self.assertEqual(link.attrib["target"], "_top")
        item = replace(self.add("Linked work"), reviews=("proposal/sdlc/P10", "change/sdlc/C1830"))
        for state in review_items.ReviewState:
            with self.subTest(summary=state):
                reviews = {key: replace(ready, key=key, state=state) for key in item.reviews}
                link = ElementTree.fromstring(list_page.review_summary(item, reviews, NOW))
                query = parse_qs(urlsplit(link.attrib["href"]).query)
                self.assertEqual(query["page"], ["review"])
                place = urlsplit(query["at.review"][0])
                self.assertEqual(parse_qs(place.query), {"show": ["all"]})
                self.assertEqual(parse_qs(place.fragment), {"key": list(item.reviews)})

    def test_list_ids_use_the_shared_no_wrap_cell(self) -> None:
        _ = self.add("Readable item number")
        body = list_page.render(self.root / "reviews").decode()
        self.assertIn('<td class="record-id" data-label="#">', body)
        self.assertIn('.list-table .record-id{white-space:nowrap;overflow-wrap:normal;width:1%}', body)
        self.assertIn('.list-toggle{white-space:nowrap}', body)

    def test_bulk_controls_show_lucide_icons_and_accessible_labels(self) -> None:
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        controls = markup.split('<div class="list-expansion">', 1)[1].split('</div>', 1)[0]
        for action, label, icon in (("expand", "Expand all", "chevrons-up-down"),
                                    ("collapse", "Collapse all", "chevrons-down-up")):
            with self.subTest(label=label):
                self.assertRegex(controls, rf'<button\b[^>]*aria-label="{label}"[^>]*>\s*<svg\b')
                self.assertIn(f'id="list-{action}-all" data-list-expansion="{action}"', controls)
                control = controls.split(f'aria-label="{label}"', 1)[1].split('</button>', 1)[0]
                self.assertIn(f'title="{label}"', control)
                self.assertIn(f'class="lucide lucide-{icon}"', control)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_expands_only_items_that_match_show_chips_and_words(self) -> None:
        for title in ("Deploy alpha", "Deploy beta", "Inspect logs"):
            _ = self.add(title, the_list.ItemState.WAITING_FOR_NATE)
        _ = self.add("Deploy gamma", the_list.ItemState.WAITING_FOR_NATE, owner=OTHER.id)
        _ = self.add("Deploy delta")
        self.browser_bulk_expansion(r'''
 chip('show','needs-me').click();
 selectOwner(OWNER);
 const words=document.querySelector('#list-search');
 const filtered=new Promise(resolve=>words.addEventListener('filterchange',resolve,{once:true}));
 words.value='deploy';words.dispatchEvent(new Event('input'));
 await filtered;
 check(rows.filter(row=>!row.hidden).length===2,'Two items match all filters.');
 document.querySelector('[aria-label="Expand all"]').click();
 for(const row of rows) check(isExpanded(row,!row.hidden),'Expand all leaves hidden items closed.');
'''.replace('OWNER', json.dumps(OWNER.id)))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_collapses_shown_items_and_preserves_hidden_items(self) -> None:
        _ = self.add("Deploy alpha")
        _ = self.add("Deploy beta")
        _ = self.add("Deploy gamma", owner=OTHER.id)
        self.browser_bulk_expansion(r'''
 for(const row of rows) row.querySelector('.list-toggle').click();
 check(rows.every(row=>isExpanded(row,true)),'All items start open.');
 selectOwner(OWNER);
 check(rows.filter(row=>!row.hidden).length===2,'Two items match the owner filter.');
 document.querySelector('[aria-label="Collapse all"]').click();
 for(const row of rows) check(isExpanded(row,row.hidden),'Collapse all leaves hidden items open.');
 selectOwner('');
 check(rows.find(row=>row.dataset.owner!==OWNER).querySelector('[data-detail]').hidden===false,
  'The hidden item remains open after the filter clears.');
'''.replace('OWNER', json.dumps(OWNER.id)))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_bulk_controls_apply_words_before_the_search_timer_ends(self) -> None:
        _ = self.add("Deploy alpha", number=1)
        _ = self.add("Deploy beta", number=2)
        self.browser_bulk_expansion(r'''
 const alpha=rows.find(row=>row.dataset.number==='1'),beta=rows.find(row=>row.dataset.number==='2');
 const words=document.querySelector('#list-search');
 let changes=0;
 words.addEventListener('filterchange',()=>changes++);
 const search=value=>{words.value=value;words.dispatchEvent(new Event('input'));};
 search('alpha');
 document.querySelector('[aria-label="Expand all"]').click();
 check(changes===0,'Expand all runs before the search timer fires.');
 check(!alpha.hidden && isExpanded(alpha,true) && beta.hidden && isExpanded(beta,false),
  'Expand all opens only the match for the new search.');
 const cleared=new Promise(resolve=>words.addEventListener('filterchange',resolve,{once:true}));
 search('');await cleared;
 beta.querySelector('.list-toggle').click();
 const beforeCollapse=changes;
 search('beta');
 document.querySelector('[aria-label="Collapse all"]').click();
 check(changes===beforeCollapse,'Collapse all runs before the search timer fires.');
 check(alpha.hidden && isExpanded(alpha,true) && !beta.hidden && isExpanded(beta,false),
  'Collapse all closes only the match for the new search.');
''')

    def browser_bulk_expansion(self, steps: str) -> None:
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const rows=[...document.querySelectorAll('[data-list-item]')];
const chip=(group,value)=>document.querySelector('[data-filter-row="'+group+'"] [data-filter-value="'+value+'"]');
const selectOwner=value=>{
 const menu=document.getElementById('list-owner');menu.value=value;menu.dispatchEvent(new Event('change'));
};
const isExpanded=(row,expanded)=>row.querySelector('[data-detail]').hidden===!expanded &&
 row.querySelector('.list-toggle').getAttribute('aria-expanded')===String(expanded);
try{
''' + steps + '''
}catch(error){failures.push(String(error));}
const result=document.createElement('p');
result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, "", probe)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_review_links_jump_inside_the_full_review_list(self) -> None:
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1830", "How to check": "Read the card."},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        ready = review_items.ReviewItem("change/sdlc/C1830", "sdlc/C1830", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check the linked change", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), source)
        reviews = [ready, replace(ready, key="change/hana/P10", id="hana/P10", state=review_items.ReviewState.DONE),
                   replace(ready, key="change/sdlc/C1831", id="sdlc/C1831", state=review_items.ReviewState.WAITING),
                   replace(ready, key="change/sdlc/C1832", id="sdlc/C1832",
                           times=review_items.ReviewTimes(NOW, NOW, datetime.now(UTC) + timedelta(days=1)))]
        item = self.add("Linked review cards", number=111)
        linked_keys = tuple(review.key for review in reviews)
        _ = the_list.link(MORTY.id, item.id, linked_keys, source="morty")
        missing = replace(ready, key="change/sdlc/C1833", id="sdlc/C1833")
        missing_item = self.add("Review removed after rendering", number=112)
        _ = the_list.link(MORTY.id, missing_item.id, (missing.key,), source="morty")
        for number in range(1, 16):
            _ = self.add(f"List fixture {number}", number=number)
        reviews.append(replace(ready, key="change/other/C9", id="other/C9", title="Unlinked review"))
        reviews.append(replace(ready, key="change/other/C10", id="other/C10", title="Check another change"))
        reviews.append(replace(ready, key="change/builds/C17", id="builds/C17", title="Check a build",
                               owner=OTHER.id, source_unit=OTHER.id))

        def read_reviews(*_args: object, **kwargs: object) -> list[review_items.ReviewItem]:
            owners = cast(dict[staff.StaffId, str] | None, kwargs.get("owner_options"))
            if owners is not None:
                owners.update({OWNER.id: OWNER.name, OTHER.id: OTHER.name})
            return reviews

        with patch.object(review_items, "read", side_effect=read_reviews), patch.object(list_page, "counts_strip", return_value=""):
            reviews.append(missing)
            listing = list_page.render(self.root / "reviews")
            _ = reviews.pop()
            review = review_page.build_snapshot(self.root / "reviews")
            reviews[0] = replace(ready, times=review_items.ReviewTimes(NOW, NOW, datetime.now(UTC) + timedelta(days=1)))
            parked_review = review_page.build_snapshot(self.root / "reviews")
        pages = (console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage(list_page.PAGE_PATH)),
                 console.ConsolePage("review", "Review", "inbox", "Review", console.AvailablePage(review_page.PAGE_PATH)))
        shell = console.render(pages, console.UnavailablePage()).decode()
        list_url = "/console/" + console.place_query("list", list_page.PAGE_PATH + '#' + urlencode({"item": f"{MORTY.id}/{item.id}"}))
        probe = r'''<script>
const listUrl=LIST_URL,keys=KEYS,ownerId=OWNER_ID,otherId=OTHER_ID;
const params=new URLSearchParams(location.search),proof=params.get('proof'),returnWords=params.get('pending') || '';
let finished=false,inspected=false;
const frames=win=>new Promise(done=>win.requestAnimationFrame(()=>win.requestAnimationFrame(done)));
const nav=name=>document.querySelector('[data-page="'+name+'"]');
const selected=(doc,name)=>doc.querySelector('[data-'+name+'-chip][aria-pressed="true"]').dataset[name+'Chip'];
const failures=JSON.parse(sessionStorage.getItem('link-failures') || '[]');
const check=(ok,text)=>{
 if(!ok){failures.push(text);sessionStorage.setItem('link-failures',JSON.stringify(failures));}
};
async function finish(){
 if(finished) return;finished=true;
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
}
function ids(doc){
 for(const cell of doc.querySelectorAll('.record-id')){
  if(cell.closest('tbody').hidden) continue;
  const button=cell.querySelector('.list-toggle'),style=doc.defaultView.getComputedStyle(cell);
  check(style.whiteSpace==='nowrap','the ID cell does not wrap');
  check(button.scrollWidth<=cell.clientWidth-16,'the ID and arrow fit their column: '+button.textContent);
  const range=doc.createRange();range.selectNodeContents(button.lastChild);
  check(range.getClientRects().length===1,'the ID text has one line');
 }
}
function reviewPlaceRecorded(win,matches){
 return new Promise(resolve=>{
  const recordedPlace=event=>{
   const at=new URL(location.href).searchParams.get('at.review');
   if(event.source!==win || event.data!=='console-place' || !at ||
      !matches(new URLSearchParams(new URL(at,location.href).hash.slice(1)))) return;
   removeEventListener('message',recordedPlace);resolve();
  };
  addEventListener('message',recordedPlace);
 });
}
async function configureReview(show,words){
 nav('review').click();
 const frame=document.querySelector('iframe:not([hidden])');
 const loaded=frame.contentDocument?.querySelector('[data-review]') ? frame.contentDocument :
  await new Promise(resolve=>frame.addEventListener('load',()=>resolve(frame.contentDocument),{once:true}));

 const doc=loaded,win=doc.defaultView;
 if(doc.querySelector('[data-review]').dataset.show!==show){
  await new Promise(resolve=>{
   const observer=new win.MutationObserver(()=>{
    if(doc.querySelector('[data-review]').dataset.show===show){observer.disconnect();resolve();}
   });
   observer.observe(doc.body,{childList:true,subtree:true});
   doc.querySelector('[data-show-chip="'+show+'"]').click();
  });
 }
 doc.querySelector('[data-type-chip="change"]').click();
 doc.querySelector('[data-owner-chip="'+ownerId+'"]').click();
 const sort=doc.querySelector('[data-sort="3"]');
 if(sort.parentElement.getAttribute('aria-sort')==='none') sort.click();
 const search=doc.querySelector('#list-search');
 const recorded=reviewPlaceRecorded(win,place=>place.get('q')===words);
 search.value=words;search.dispatchEvent(new win.Event('input',{bubbles:true}));
 await recorded;
 nav('list').click();
}
async function inspect(frame){
 try{
  const doc=frame.contentDocument;
  if(inspected || !doc?.querySelector('[data-list-item]')) return;
  inspected=true;
  check(!!document.querySelector('.sidebar .nav-entry'),'the console navigator remains visible');
  const stage=proof==='review' ? keys.length : Number(sessionStorage.getItem('link-stage') || 0);
  if(doc.querySelector('[data-list]')){
   ids(doc);
   const row=doc.querySelector('[data-number="111"]');
   check(!row.hidden && !row.querySelector('[data-detail]').hidden,'List item 111 is open');
   if(proof==='list'){await finish();return;}
   const win=doc.defaultView;
   if(stage===0 && !proof){
    await configureReview('all','Check');
    const extra=doc.querySelector('[data-number="15"]');
    extra.querySelector('.list-toggle').click();
    check(!extra.querySelector('[data-detail]').hidden,'the extra List row is open before leaving');
    const stale=new URL(win.location.href),filters=new URLSearchParams(stale.hash.slice(1));
    filters.set('producer','unavailable');stale.hash=filters;
    win.history.replaceState(win.history.state,'',stale);
    const outer=new URL(location.href);outer.searchParams.set('at.list',stale.pathname+stale.hash);
    history.replaceState(history.state,'',outer);
    win.history.scrollRestoration='manual';
    win.scrollTo({top:120,behavior:'instant'});
    sessionStorage.setItem('list-top',String(win.scrollY));
    check(win.scrollY>0,'the List has a scroll position to restore');
   }
   if(stage===1 && !proof){
    await frames(win);
    const restored=new URLSearchParams(win.location.hash.slice(1));
    check(!restored.has('producer') && restored.get('item')===row.dataset.key,'Back normalizes the stale List filter and keeps the source');
    check(!doc.querySelector('[data-number="15"] [data-detail]').hidden,'Back restores the extra open List row');
    check(Math.abs(win.scrollY-Number(sessionStorage.getItem('list-top')))<=1,
      'Back restores the List scroll from '+sessionStorage.getItem('list-top')+' to '+win.scrollY);
    check(doc.querySelector('#list-search').value===returnWords,'Back restores words typed just before the link click');
    const returns=Number(sessionStorage.getItem('list-returns') || 0)+1;
    sessionStorage.setItem('list-returns',String(returns));
    if(returns===1){
     doc.body.dataset.firstReturn='';
     history.forward();return;
    }
    check(!doc.body.hasAttribute('data-first-return'),'the second Back loads a new List document');
    await configureReview('open','Unlinked review');
   }
   const source=stage===keys.length+1 ? doc.querySelector('[data-number="112"]') : row;
   if(source!==row) source.querySelector('.list-toggle').click();
   const links=[...source.querySelector('[data-detail]').querySelectorAll('a')];
   const key=stage<keys.length ? keys[stage] : stage===keys.length+2 ? keys[0] : '';
   const link=key ? links.find(link=>{
    const at=new URL(link.href).searchParams.get('at.review');
    return at && new URLSearchParams(new URL(at,location.href).hash.slice(1)).get('jump')===key;
   }) : source.querySelector('[data-label="State"] a');
   check(!!link,'the selected List link exists');
   if(stage===0 && returnWords){
    const search=doc.querySelector('#list-search');
    search.value=returnWords;search.dispatchEvent(new win.Event('input',{bubbles:true}));
   }
   if(link) link.click();else await finish();
  }else if(doc.querySelector('[data-review]')){
   if(stage===1 && sessionStorage.getItem('list-returns')==='1'){history.back();return;}
   ids(doc);
   const visibleRows=()=>[...doc.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
   const visible=visibleRows();
   if(stage<keys.length){
    const target=visible.find(row=>row.dataset.key===keys[stage]);
    check(!!target && visible.some(row=>row.dataset.key==='change/other/C10'),'the linked card and an unlinked card are shown');
    check(target?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true','the linked card is expanded');
    check(!target?.querySelector('[data-detail]').hidden,'the linked card detail is open');
    const place=new URLSearchParams(doc.location.hash.slice(1)),search=doc.querySelector('#list-search');
    check(!place.has('key') && !place.has('jump') && place.get('item')===keys[stage],'the address keeps only an open item target');
    check(selected(doc,'show')===(stage<3 ? 'all' : 'open'),'Review keeps the selected All view or widens Show for a card outside Open');
    const reloaded=sessionStorage.getItem('review-reload')===String(stage);
    if(!reloaded){
     const row=target?.querySelector('.list-row'),box=row?.getBoundingClientRect();
     check(box?.top>=0 && box?.bottom<=doc.defaultView.innerHeight,'the linked row is in view');
     check(row?.hasAttribute('data-record-highlight'),'the linked row has the shared highlight');
     const reduced=doc.defaultView.matchMedia('(prefers-reduced-motion: reduce)').matches;
     check(row?.getAnimations().some(animation=>animation.playState==='running')===!reduced,
       'the highlight respects reduced motion');
    }
    if(stage===0){
     check(selected(doc,'owner')===ownerId && selected(doc,'type')==='change' && search.value==='Check',
       'the generated link keeps the Review filters selected in the controls');
     check(doc.querySelector('[data-sort="3"]').parentElement.getAttribute('aria-sort')==='ascending',
       'the generated link keeps the Review sort selected in the controls');
    }
    if(stage===1){
     check(selected(doc,'owner')==='','the jump resets Owner to All when words or Show hide the card');
     check(search.value==='' && selected(doc,'type')==='change','the jump clears conflicting words and keeps the matching type');
    }
    if(stage===0 && !reloaded){
     const input=value=>{
      search.value=value;search.dispatchEvent(new doc.defaultView.Event('input',{bubbles:true}));
     };
     input('Unlinked review');
     check(visibleRows().length===1 && visibleRows()[0].dataset.key==='change/other/C9','the words box still filters');
     input('');
     doc.querySelector('[data-type-chip="question"]').click();
     check(visibleRows().length===0,'the type chips still filter');
     doc.querySelector('[data-type-chip=""]').click();
     doc.querySelector('[data-owner-chip="'+otherId+'"]').click();
     check(visibleRows().length===1 && visibleRows()[0].dataset.owner===otherId,'the owner chips still filter');
     doc.querySelector('[data-owner-chip=""]').click();
     doc.querySelector('[data-show-chip="open"]').click();
     check(visibleRows().every(row=>row.dataset.state==='Open'),'the Show chips still filter');
     doc.querySelector('[data-show-chip="all"]').click();
     doc.querySelector('[data-owner-chip="'+ownerId+'"]').click();
     doc.querySelector('[data-type-chip="change"]').click();
     input('Check');
     await frames(doc.defaultView);
    }
    if((stage===0 || stage===3) && !reloaded){
     sessionStorage.setItem('review-reload',String(stage));
     location.reload();return;
    }
    sessionStorage.setItem('link-stage',String(stage+1));
    if(stage===0) history.back();else location.href=listUrl;
   }else if(stage===keys.length){
    check(visible.length===keys.length && visible.every(row=>keys.includes(row.dataset.key)),'the summary shows only linked cards');
    const done=visible.find(row=>row.dataset.state==='Done');
    if(done?.querySelector('.list-toggle').getAttribute('aria-expanded')!=='true') done?.querySelector('.list-toggle').click();
    check(done?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true','the Done card can open in the linked view');
    if(proof){await finish();return;}
    const group=doc.querySelector('[data-clear-linked-reviews]');
    check(group && !group.hidden,'the linked group has one visible filter chip');
    group?.click();
    check(visibleRows().some(row=>row.dataset.key==='change/other/C9'),'one click restores the normal Review list');
    check(group?.hidden && !new URLSearchParams(doc.location.hash.slice(1)).has('key'),'the group chip clears the selection');
    sessionStorage.setItem('link-stage',String(stage+1));
    location.href=listUrl;
   }else if(stage===keys.length+1){
    const notice=doc.querySelector('[data-missing-item]');
    check(notice && !notice.hidden && notice.textContent==='This review is no longer on the Review page.',
      'a removed linked review shows a missing-target notice');
    check(visibleRows().some(row=>row.dataset.key==='change/other/C9'),'a missing link leaves the normal Review list visible');
    sessionStorage.setItem('link-stage',String(stage+1));
    location.href=listUrl;
   }else{
    const target=doc.querySelector('[data-key="'+keys[0]+'"]');
    if(sessionStorage.getItem('later-reload')){
     const response=await doc.defaultView.fetch('/console/review.html?index=1');
     const payload=await response.json(),entry=payload.index.r.find(row=>row[0]===keys[0]);
     check(entry?.[9]>Date.now() && !target && !new URLSearchParams(doc.location.hash.slice(1)).has('item'),
       'a parked linked card stays hidden and closed after reload');
     await finish();return;
    }
    check(!target.hidden && !target.querySelector('[data-detail]').hidden,'the card opens from List before Later');
    const form=await new Promise((resolve,reject)=>{
     const observe=new doc.defaultView.MutationObserver(()=>{
      const form=target.querySelector('form');
      if(form){observe.disconnect();resolve(form);}
     });
     const form=target.querySelector('form');
     if(form) resolve(form);
     else{
      observe.observe(target,{childList:true,subtree:true});
      setTimeout(()=>{observe.disconnect();reject(new Error('The Review card body did not load.'));},8000);
     }
    });
    const settled=new Promise(resolve=>{
     const observer=new doc.defaultView.MutationObserver(()=>{
      if(!form.hasAttribute('data-sending')){observer.disconnect();resolve();}
     });
     observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
    });
    const recorded=reviewPlaceRecorded(doc.defaultView,place=>!place.has('item') && !place.has('linked'));
    form.querySelector('[data-later-toggle]').click();
    form.querySelector('[data-later-preset="morning"]').click();
    if(!form.hasAttribute('data-sending')) throw new Error('Later did not start.');
    await settled;
    check(form.querySelector('[data-send-status]').textContent==='Saved: parked','Later confirms the save on the card');
    const status=doc.querySelector('[data-hidden-action-status]');
    check(status.hidden && !status.textContent,'Later shows no success notice above the table');
    const place=new URLSearchParams(doc.location.hash.slice(1));
    check(!place.has('item') && !place.has('linked'),'Later removes the linked target from the Review address');
    sessionStorage.setItem('later-reload','yes');
    await recorded;
    location.reload();
   }
  }
 }catch(error){failures.push(String(error));await finish();}
}
for(const frame of document.querySelectorAll('iframe')) frame.addEventListener('load',()=>inspect(frame));
addEventListener('pageshow',event=>{
 if(event.persisted){
  inspected=false;
  const active=document.querySelector('iframe:not([hidden])');
  requestAnimationFrame(()=>requestAnimationFrame(()=>inspect(active)));
 }
});
if(proof){const keepOpen=new Image();keepOpen.hidden=true;keepOpen.src='/complete';document.body.append(keepOpen);}
</script>'''.replace('LIST_URL', json.dumps(list_url)).replace('KEYS', json.dumps(linked_keys)).replace(
            'OWNER_ID', json.dumps(OWNER.id)).replace('OTHER_ID', json.dumps(OTHER.id))
        shell = shell.replace('</body>', probe + '</body>')
        output = Path(os.environ.get("LISTLINKS_PROOF_DIR", str(self.root)))
        output.mkdir(parents=True, exist_ok=True)
        for shot in ("clicks", "reduced", "review", "list"):
            with self.subTest(shot=shot):
                results: list[list[str]] = []
                complete = threading.Event()
                later_saved = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        route = urlsplit(self.path)
                        path = route.path
                        if path == '/complete':
                            _ = complete.wait(30)
                        fields = parse_qs(route.query)
                        current = parked_review if later_saved.is_set() else review
                        content_type = 'text/html; charset=utf-8'
                        if path == review_page.PAGE_PATH:
                            if 'body' in fields:
                                try:
                                    body = current.body(fields['body'][0])
                                except LookupError:
                                    self.send_error(404)
                                    return
                            elif 'index' in fields:
                                body = current.index_response()
                                content_type = 'application/json'
                            elif 'version' in fields:
                                body = json.dumps({'version':current.version, 'facts':current.facts()}).encode()
                                content_type = 'application/json'
                            else:
                                body = current.render(show=fields.get('show', ['open'])[0])
                        elif path == list_page.PAGE_PATH:
                            body = listing
                        elif path.endswith('.json'):
                            body = b'{}'
                            content_type = 'application/json'
                        else:
                            body = shell.encode()
                        self.send_response(200)
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Content-Type', content_type)
                        self.send_header('X-Review-Version', current.version)
                        self.end_headers()
                        try:
                            _ = self.wfile.write(body)
                        except BrokenPipeError:
                            pass

                    def do_POST(self) -> None:
                        body = self.rfile.read(int(self.headers['Content-Length']))
                        if self.path == review_page.PAGE_PATH and parse_qs(body.decode()).get('outcome') == ['later']:
                            later_saved.set()
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            _ = self.wfile.write(b'{"answer":"parked"}')
                            return
                        if self.path == '/results':
                            results.append(cast(list[str], json.loads(body)))
                            complete.set()
                        self.send_response(204)
                        self.end_headers()

                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass

                with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        profile = self.root / f'links-profile-{shot}'
                        profile.mkdir()
                        _ = (profile / 'user.js').write_text('user_pref("ui.systemUsesDarkTheme", 1);\n'
                            + 'user_pref("browser.sessionhistory.max_total_viewers", 0);\n'
                            + f'user_pref("ui.prefersReducedMotion", {int(shot == "reduced")});\n')
                        destination = list_url + ('&pending=i' if shot == 'reduced' else '')
                        if shot == 'review':
                            destination = '/console/' + console.place_query('review', review_page.PAGE_PATH + '?show=all#'
                                + urlencode([('key', key) for key in linked_keys] + [('item', reviews[1].key)]))
                        url = f'http://127.0.0.1:{server.server_port}' + destination
                        command = ['firefox', '--headless', '--no-remote', '--profile', str(profile), '--window-size', '1440,1100']
                        if shot in ('clicks', 'reduced'):
                            with subprocess.Popen([*command, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as browser:
                                try:
                                    self.assertTrue(complete.wait(35), "The browser must report its click results")
                                finally:
                                    browser.terminate()
                                    _ = browser.wait(timeout=10)
                        else:
                            done = subprocess.run([*command, '--screenshot', str(output / f'listlinks-{shot}-dark.png'),
                                url + '&proof=' + shot], capture_output=True, text=True, timeout=45)
                            self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        complete.set()
                        server.shutdown()
                        thread.join(5)
                self.assertEqual(results, [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_list_links_show_the_target_inside_the_console(self) -> None:
        source = self.add("Follow the linked items", number=1)
        for number in range(2, 29):
            _ = self.add(f"Fixture item {number}", number=number)
        closed = self.add("Closed linked item", number=29, owner=OTHER.id)
        _ = the_list.close(MORTY.id, closed.id, "The work is complete.", source="morty")
        _ = self.add("Open blocker", number=30)
        other_producer = self.add("Linked item from another producer", number=31)
        _ = the_list.update(MORTY.id, other_producer.id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.update(MORTY.id, source.id, {"related_to": (29,),
            "blocked_by": (the_list.ItemDependency(30, "The blocker is complete."),)}, source="morty")
        records = list_page.records()
        for index, entry in enumerate(records):
            if entry.item.id == source.id:
                records[index] = list_page.ListRecord(entry.keeper, replace(entry.item, related_to=(2, 29, 31, 99), unread_since=NOW))
            elif entry.item.id == "L30":
                records[index] = list_page.ListRecord(entry.keeper, replace(entry.item, unread_since=NOW))
        with patch.object(list_page, "records", return_value=records), patch.object(list_page, "counts_strip", return_value=""):
            listing = list_page.render(self.root / "reviews")
        listing = listing.replace(b'data-page-facts ', b'data-page-facts data-read-error="Test read failed" ', 1)
        listing = listing.replace(b'</body>', b'''<script>
addEventListener('message',event=>{
 if(event.source!==parent || event.origin!==location.origin || !event.data.clickListLink) return;
 if(event.data.pendingWords){
  const search=document.querySelector('#list-search');
  search.value=event.data.pendingWords;search.dispatchEvent(new Event('input'));
 }
 const links=document.querySelectorAll('[data-number="1"] [data-detail] a');
 [...links].find(link=>link.textContent==='#'+event.data.clickListLink).click();
});
</script></body>''')
        page = console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage(list_page.PAGE_PATH))
        shell = console.render((page,), console.UnavailablePage()).decode()
        probe = r'''<script>
const params=new URLSearchParams(location.search),number=params.get('number'),scenario=params.get('case');
const proof=scenario==='proof';
const frame=document.querySelector('iframe');
const frames=win=>new Promise(done=>win.requestAnimationFrame(()=>win.requestAnimationFrame(done)));
const changed=win=>new Promise(done=>win.addEventListener('hashchange',done,{once:true}));
frame.addEventListener('load',async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  const doc=frame.contentDocument,win=frame.contentWindow;
  const row=id=>doc.querySelector('[data-number="'+id+'"]');
  const source=row(1),target=row(number),search=doc.querySelector('#list-search');
  const chip=(name,value)=>doc.querySelector('[data-filter-row="'+name+'"] [data-filter-value="'+value+'"]');
  const selected=name=>doc.querySelector('[data-filter-row="'+name+'"] [aria-pressed="true"]').dataset.filterValue;
  const menu=name=>doc.getElementById('list-'+name);
  const unread=doc.getElementById('list-unread');
  const setMenu=(name,value)=>{const field=menu(name);field.value=value;field.dispatchEvent(new win.Event('change'));};
  const filters=()=>[menu('owner').value,menu('producer').value,search.value,unread.checked];
  const pendingWords=scenario==='pending-words' ? 'item' : '';
  const originalFilters=filters();
  if(pendingWords) originalFilters[2]=pendingWords;
  const link=[...source.querySelectorAll('a')].find(a=>a.textContent==='#'+number);
  const expanded=item=>item.querySelector('.list-toggle').getAttribute('aria-expanded')==='true';
  const inView=element=>{
   const rect=element.getBoundingClientRect();
   return rect.top>=0 && rect.bottom<=win.innerHeight;
  };
  const originalShow='working';
  check(!source.hidden && expanded(source) && selected('show')===originalShow,'the source starts open in the selected view');
  check(!!document.querySelector('.sidebar .nav-entry'),'the console navigator is visible');
  link.scrollIntoView({block:'center'});
  if(scenario==='history') win.scrollTo(0,120);
  const unavailableProducer=scenario==='unavailable-producer';
  if(unavailableProducer){
   const saved=new URL(win.location.href),params=new URLSearchParams(saved.hash.slice(1));
   params.set('producer','unavailable');
   saved.hash=String(params);
   win.history.replaceState(win.history.state,'',saved);
   win.history.scrollRestoration='manual';
   win.scrollTo(0,270);
   check(win.scrollY===270,'the previous page can scroll to 270');
  }
  const banner=doc.querySelector('[data-page-status]');
  if(scenario==='banner') banner.style.minHeight='60px';
  const originalTop=win.scrollY,beforeJump=win.location.href;
  const original=new URL(beforeJump),params=new URLSearchParams(original.hash.slice(1));
  if(pendingWords){params.set('q',pendingWords);original.hash=String(params);}
  const originalPlace=original.href;
  const debounced=pendingWords ? new Promise(done=>search.addEventListener('filterchange',done,{once:true})) : null;
  const consolePlace=()=>new URL(new URLSearchParams(location.search).get('at.list'),location.href).href;
  const updated=new Promise(resolve=>{
   const done=()=>{
    clearTimeout(timeout);removeEventListener('message',record);resolve();
   };
   const record=event=>{
    if(event.source===win && event.data==='console-place' && win.location.href!==beforeJump &&
       consolePlace()===win.location.href) done();
   };
   addEventListener('message',record);
   const timeout=setTimeout(done,1500);
  });
  win.postMessage({clickListLink:number,pendingWords},location.origin);await updated;await frames(win);
  if(debounced){
   await debounced;
   check(!target.hidden && expanded(target) && search.value==='','the target stays open after the pending input settles');
  }
  const missing=doc.querySelector('[data-missing-item]');
  check(doc===frame.contentDocument,'the click stays inside the same console frame');
  check(consolePlace()===win.location.href,'the console recorded '+consolePlace()+' instead of '+win.location.href);
  if(number==='99'){
   check(!missing.hidden && missing.textContent==='Item #99 is not on the List' && inView(missing),'the missing message is in view');
  }else{
   const title=target.querySelector('.list-row');
   const fallback=['closed','repeat','producer','open','pending-words','unread-words','proof'].includes(scenario);
   check(selected('show')===(fallback ? 'all' : 'working'),'the view after the click is '+selected('show'));
   check(!target.hidden && expanded(target),'the target is visible and open');
   check(inView(title),'the target row is outside the viewport at '+title.getBoundingClientRect().top);
   check(title.getAnimations().some(animation=>animation.playState==='running'),'the target row has a short highlight');
   if(scenario==='banner'){
    const bottom=banner.getBoundingClientRect().bottom,top=title.getBoundingClientRect().top;
    check(win.getComputedStyle(banner).position==='sticky' && bottom>=60,'the tall status banner is sticky');
    check(top>=bottom+15,'the target top '+top+' leaves a gap below the banner bottom '+bottom);
   }
  }
  const expected=originalFilters.slice();
  if(['closed','producer','open','pending-words'].includes(scenario)) expected[0]='';
  if(scenario==='producer') expected[1]='';
  if(['open','unread-words'].includes(scenario) || pendingWords) expected[2]='';
  check(filters().every((value,index)=>value===expected[index]),'the jump keeps each matching filter: '+JSON.stringify(filters()));
  if(scenario==='repeat'){
   const jumpPlace=win.location.href,jumpHistoryLength=win.history.length;
   link.click();await frames(win);
   check(win.location.href===jumpPlace && win.history.length===jumpHistoryLength,'a second click stays in the same history entry');
  }
  if(scenario==='history'){
   target.querySelector('.list-toggle').click();
   row(3).querySelector('.list-toggle').click();
   check(win.location.href===originalPlace && expanded(row(3)),'the new history entry has the original address and an extra open row');
  }
  if(!proof){
   const back=scenario==='history' ? new Promise(done=>win.addEventListener('popstate',done,{once:true})) : changed(win);
   history.back();await back;await frames(win);
   if(unavailableProducer){
    const restored=new URLSearchParams(win.location.hash.slice(1));
    check(!restored.has('producer'),'Back removes the unavailable Producer from the frame address');
    check(win.history.state.recordTable.top===270,'the corrected history state keeps top: 270');
    check(win.scrollY===270,'Back restores scroll to 270, got '+win.scrollY);
   }else{
    check(win.location.href===originalPlace,'Back restores the frame address');
    check(inView(link) && Math.abs(win.scrollY-originalTop)<=1,'Back restored scroll '+win.scrollY+' instead of '+originalTop);
   }
   check(selected('show')===originalShow && !source.hidden && expanded(source),'Back restores the open source');
   check(filters().every((value,index)=>value===originalFilters[index]),'Back restores the menus, search and switch.');
   check(!target || !expanded(target),'Back collapses the target row');
   if(scenario==='history') check(!expanded(row(3)),'Back collapses the extra row even when the address stays the same');
  }
  if(scenario==='missing'){
   const missingKey=new URLSearchParams(link.hash.slice(1)).get('item');
   const saved=new URLSearchParams(new URL(originalPlace).hash.slice(1));
   saved.set('key',source.dataset.key);saved.set('item',missingKey);
   const clearSearch=()=>new Promise(done=>{
    search.addEventListener('filterchange',done,{once:true});
    search.value='';search.dispatchEvent(new win.Event('input'));
   });
   const changes=[
    ['Show',()=>chip('show','all').click()],
    ['Unread only',()=>unread.click()],
    ['Owner',()=>setMenu('owner','')],
    ['Producer',()=>setMenu('producer','')],
    ['Search',clearSearch],
    ['sort',()=>doc.querySelector('[data-sort]').click()],
   ];
   for(const [label,select] of changes){
    const restored=changed(win);win.location.hash=String(saved);await restored;
    check(!missing.hidden && doc.querySelector('[data-missing-keys]').hidden,'the restored missing item shows one notice');
    await select();
    const after=new URLSearchParams(win.location.hash.slice(1));
    check(missing.hidden && !doc.querySelector('[data-missing-keys]').textContent,label+' clears the missing item notice');
    check(after.get('item')!==missingKey && after.getAll('key').join()===source.dataset.key,label+' omits the missing item from the address');
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/list-jump-result',{method:'POST',body:JSON.stringify(failures)});
},{once:true});
</script><img hidden src="/list-jump-ready">'''
        shell = shell.replace('</body>', probe + '</body>')
        results: list[list[str]] = []
        complete = threading.Event()

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == '/list-jump-ready':
                    _ = complete.wait(35)
                body = listing if path == list_page.PAGE_PATH else shell.encode() if path == "/console/" else b'{}'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if body != b'{}' else 'application/json')
                self.end_headers()
                try:
                    _ = self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_POST(self) -> None:
                if self.path == '/list-jump-result':
                    results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    complete.set()
                self.send_response(204)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                proof = os.environ.get("LIST_JUMP_PROOF")
                cases = [(30, "open"), (29, "closed"), (31, "producer"), (99, "missing"), (29, "repeat"),
                         (2, "history"), (2, "banner"), (30, "pending-words"), (30, "unavailable-producer"),
                         (30, "unread-words")]
                if proof:
                    cases.append((29, "proof"))
                for number, case in cases:
                    with self.subTest(link=case):
                        complete.clear()
                        results.clear()
                        profile = self.root / f"list-jump-{case}"
                        profile.mkdir()
                        _ = (profile / "user.js").write_text('user_pref("ui.systemUsesDarkTheme", 1);\n')
                        filters = {"sort": "#", "item": f"{MORTY.id}/{source.id}"}
                        if case in ("open", "closed", "producer", "missing"):
                            filters.update(owner=OWNER.id, producer=MORTY.id, q="linked item")
                        if case == "unread-words":
                            filters.update(unread="1", q="item")
                        place = list_page.PAGE_PATH + "?show=working#" + urlencode(filters)
                        url = "/console/" + console.place_query("list", place)
                        destination = f'http://127.0.0.1:{server.server_port}{url}&number={number}&case={case}'
                        command = ['firefox', '--headless', '--no-remote', '--profile', str(profile), '--window-size', '1440,1100']
                        if case == "proof" and proof:
                            result = subprocess.run([*command, '--screenshot', proof, destination],
                                                    capture_output=True, text=True, timeout=45)
                            self.assertEqual(result.returncode, 0, result.stderr)
                        else:
                            with subprocess.Popen([*command, destination], stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL) as browser:
                                try:
                                    self.assertTrue(complete.wait(30), "The browser must report the result of the link click.")
                                finally:
                                    browser.terminate()
                                    _ = browser.wait(timeout=10)
                        self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join(5)

    def test_staff_owner_and_producer_names_have_role_colors_but_nate_stays_plain(self) -> None:
        _ = self.add("Staff names")
        _ = self.add("User name", owner=the_list.UnassignedOwner.NATE)
        body = list_page.render(self.root / "reviews").decode()
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(body, r'data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn('<td data-label="Owner">nate</td>', body)

    def test_added_log_shows_staff_colors_local_eta_and_field_labels(self) -> None:
        _ = self.add("Log labels")
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
        self.assertRegex(log, r'Owner</dt><dd><span[^>]*data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(log, r'Producer</dt><dd><span[^>]*data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn("ETA</dt><dd>14:00 EST – 15:00 EST</dd>", log)
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertIn("Next step</dt><dd>", log)
        self.assertIn("Latest status</dt><dd>", log)
        for text in (OWNER.id, MORTY.id, "EtaRange(", "next_step", "latest_status"):
            self.assertNotIn(text, log)

    def test_updated_log_shares_the_eta_and_next_step_renderers(self) -> None:
        item = self.add("Log an update")
        _ = the_list.update(MORTY.id, item.id, {
            "eta": NOW + timedelta(hours=3), "next_step": "Read the **new** result <first>."}, source="morty")
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</li>", 1)[0]
        self.assertIn("Updated", log)
        for label, expected in (("ETA", "16:00 EST"), ("Next step", "Read the <strong>new</strong> result &lt;first&gt;.")):
            cell = body.split(f'<td data-label="{label}">', 1)[1].split("</td>", 1)[0]
            self.assertIn(expected, cell)
            self.assertIn(f"{label}</dt><dd>{cell}</dd>", log)

    def test_legacy_log_text_renders_with_a_new_field_change(self) -> None:
        item = self.add("Keep the original log")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        base, _, _ = path.read_text().partition("## Log\n\n")
        _ = path.write_text(base + "## Log\n\n- 2026-11-16 17:30:00+00:00 Recorded <old words> — nate\n")
        original = the_list.load(MORTY.id, item.id)
        _ = the_list.update(MORTY.id, item.id, {"eta": NOW + timedelta(hours=3),
            "evidence": 'Source checkins.json item:\n\n{"owner": "sdlc"}\n'}, source="morty")
        _ = the_list.clean_evidence(MORTY.id, source="morty")
        _ = the_list.link(MORTY.id, item.id, ("review<key>",), source="morty")
        _ = the_list.unlink(MORTY.id, item.id, ("review<key>",), source="morty")
        _ = the_list.close(MORTY.id, item.id, "Keep this record.", source="morty")
        _ = the_list.reopen(MORTY.id, item.id, source="morty")
        self.assertEqual(the_list.load(MORTY.id, item.id).log[0], original.log[0])
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
        self.assertIn("Recorded &lt;old words&gt;", log)
        self.assertIn("ETA</dt><dd>16:00 EST</dd>", log)
        self.assertIn("Linked reviews: review&lt;key&gt;", log)
        self.assertIn("Unlinked reviews: review&lt;key&gt;", log)
        self.assertIn("Reopened <small>", log)
        self.assertIn("Removed the migration copy of checkins.json from Evidence", log)

    def legacy_log(self, *lines: str) -> str:
        item = self.add("Keep the original log")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        body, marker, _ = path.read_text().partition("## Log\n\n")
        _ = path.write_text(body + marker + "\n".join(lines) + "\n")
        rendered = list_page.render(self.root / "reviews").decode()
        return rendered.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]

    def test_old_added_log_uses_the_field_renderer(self) -> None:
        fields = {"title": "Keep the original log", "owner": OWNER.id, "producer": MORTY.id,
                  "state": "Working", "eta": repr(the_list.EtaRange(NOW + timedelta(hours=1), NOW + timedelta(hours=2))),
                  "next_step": 'Read "the result"; then check the page.\nKeep the note.'}
        note = "; ".join(f"{key}: {json.dumps(value)}" for key, value in fields.items())
        log = self.legacy_log("- 2026-11-16 18:00:00+00:00 Added; " + note + " — nate")
        self.assertRegex(log, r'Owner</dt><dd><span[^>]*data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(log, r'Producer</dt><dd><span[^>]*data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertIn("ETA</dt><dd>14:00 EST – 15:00 EST</dd>", log)
        self.assertIn("Next step</dt><dd>", log)
        self.assertIn('Read &quot;the result&quot;; then check the page.', log)
        self.assertNotIn("EtaRange", log)

    def test_old_updated_log_reads_descriptions_and_datetimes(self) -> None:
        log = self.legacy_log(
            "- 2026-11-16 18:10:00+00:00 Updated eta: "
            + json.dumps("EtaDescription(text='Pending (check 16:45 EST)')")
            + '; next_step: "Wait for the check." — morty',
            '- 2026-11-16 18:20:00+00:00 Updated eta: "2026-11-16 21:00:00+00:00" — morty',
            '- 2026-11-17 16:54:58.780107+00:00 Updated next_check: "2026-11-17 16:00:00-05:00" — morty, 11:54 EST',
            '- 2026-11-17 03:12:17.088559+00:00 Updated state: "Working" — builds, 22:12 EST')
        self.assertIn("ETA</dt><dd>Pending (check 16:45 EST)</dd>", log)
        self.assertIn("ETA</dt><dd>16:00 EST</dd>", log)
        self.assertIn("Next check</dt><dd>2026-11-17 16:00 EST</dd>", log)
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertNotIn("EtaDescription", log)

    def test_old_feedback_log_uses_the_feedback_renderer(self) -> None:
        rows = [
            json.dumps({"request": "feedback", "words": "Read the **result** <today>.",
                        "state": state, "alerted_to": alerted_to})
            for state, alerted_to in (("pending", ""), ("sent", ""), ("alerted", "sdlc"))
        ]
        log = self.legacy_log(*(
            "- 2026-11-16 18:10:00+00:00 Feedback to morty: " + row + " — nate via List" for row in rows))
        for label in ("Feedback for morty", "Feedback sent to morty", "Feedback could not reach morty"):
            self.assertIn(label, log)
        self.assertEqual(log.count("Read the <strong>result</strong> &lt;today&gt;."), 3)
        self.assertNotIn("&quot;request&quot;", log)

    def test_old_park_log_uses_the_local_date_renderer(self) -> None:
        log = self.legacy_log(
            "- 2026-11-16 18:10:00+00:00 Parked until 2026-11-17T05:00:00+00:00 — nate",
            "- 2026-11-16 18:20:00+00:00 Parked until not parked — nate")
        self.assertIn("Parked until</dt><dd>2026-11-17</dd>", log)
        self.assertIn("Parked until</dt><dd>Not parked</dd>", log)
        self.assertNotIn("2026-11-17T05:00:00+00:00", log)

    def test_park_date_matches_the_response_and_table(self) -> None:
        root = self.root / "reviews"
        item = self.add("Park this item")
        _ = the_list.park(MORTY.id, item.id, the_list.time_value("2026-11-18T02:00:00+00:00"), source="nate")
        with self.subTest(view="table"):
            body = list_page.render(root).decode()
            self.assertIn("Parked until 2026-11-17 21:00 EST</small>", body)
            self.assertIn("Parked until</dt><dd>2026-11-17 21:00 EST</dd>", body)
        with self.subTest(view="response"):
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "2026-11-17 21:00 EST")

    def test_old_closed_log_uses_the_closing_words_renderer(self) -> None:
        log = self.legacy_log("- 2026-11-16 18:10:00+00:00 Closed: The **check** passed. — nate")
        self.assertIn("Closing words</dt><dd>", log)
        self.assertIn("The <strong>check</strong> passed.", log)

    def test_old_log_keeps_unparseable_notes_as_text(self) -> None:
        log = self.legacy_log(
            "- 2026-11-16 18:00:00+00:00 Added on sdlc's request <old words> — nate",
            '- 2026-11-16 18:10:00+00:00 Updated state: "Working"; next_step: invalid — nate',
            '- 2026-11-16 18:20:00+00:00 Updated state: "Done" — nate',
            '- 2026-11-16 18:30:00+00:00 Updated title: "" — nate',
            r'- 2026-11-16 18:40:00+00:00 Updated unit: "first\nsecond" — nate')
        self.assertIn("Added on sdlc&#x27;s request &lt;old words&gt;", log)
        self.assertIn("Updated title: &quot;&quot;", log)
        self.assertIn(r"Updated unit: &quot;first\nsecond&quot;", log)
        self.assertIn("next_step: invalid", log)
        self.assertEqual(log.count("<dl>"), 1)
        self.assertIn("State</dt><dd>Done</dd>", log)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_log_fits_the_left_column_and_keeps_each_scroll_position(self) -> None:
        for number in (1, 2):
            item = self.add(f"Log for item {number}", number=number)
            for update in range(18):
                _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        _ = the_list.add(MORTY.id, "Short log", number=3, owner=OWNER.id, source="nate", changes={})
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = r'''<script>
const freshList=MARKUP;
let reads=0;
window.fetch=async()=>{reads++;return {ok:true,text:async()=>freshList};};
</script>'''.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const panel=number=>document.querySelector('[data-number="'+number+'"] .list-panel');
const open=number=>document.querySelector('[data-number="'+number+'"] .list-toggle').click();
const scrollArea=right=>right.querySelector('.list-log-scroll') ?? right.querySelector('.list-log');
const height=node=>node.getBoundingClientRect().height;
try{
 for(const number of [1,2,3]) open(number);
 const [left,right]=panel(1).children,log=scrollArea(right),heading=right.querySelector('h3');
 right.style.display='none';
 const naturalHeight=height(left);
 right.style.display='';
 check(height(right)===naturalHeight && height(left)===naturalHeight,'The left column sets the height of a long log.');
 const headingTop=heading.getBoundingClientRect().top;
 log.scrollTop=90;
 check(log.scrollTop===90 && log.scrollHeight>log.clientHeight &&
  heading.getBoundingClientRect().top===headingTop,'The long log scrolls below its fixed heading.');
 const secondLog=scrollArea(panel(2).lastElementChild);
 secondLog.scrollTop=45;
 const shortLog=scrollArea(panel(3).lastElementChild);
 check(shortLog.scrollHeight===shortLog.clientHeight && getComputedStyle(shortLog).overflowY==='auto',
  'The short log fits without a scroll bar.');
 const original=document.querySelector('[data-list]');
 const replaced=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(!original.isConnected){observer.disconnect();resolve();}});
  observer.observe(original.parentNode,{childList:true});
 });
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await replaced;
 check(reads===1 && scrollArea(panel(1).lastElementChild).scrollTop===90 &&
  scrollArea(panel(2).lastElementChild).scrollTop===45 && scrollArea(panel(3).lastElementChild).scrollTop===0,
  'The shared refresh restores each open log position.');
}catch(error){failures.push(String(error));}
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
})();</script>'''
        self.browser_actions(markup, setup, probe, window_size=(1440, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_log_refresh_preserves_the_visible_entry_and_keeps_the_top_at_zero(self) -> None:
        self.browser_log_refresh('''
 open(1);open(2);
 log(1).scrollTop=239;
 const before=firstVisible(log(1));
 check(before.offset<0 && log(1).scrollTop>0,'The first item starts partway through an entry.');
 check(log(2).scrollTop===0,'The second item starts at the top.');
 await refresh();
 const after=firstVisible(log(1));
 check(after.text===before.text && Math.abs(after.offset-before.offset)<=1,
  'The first item keeps the same entry and offset after the new entry arrives.');
 check(log(2).scrollTop===0 && firstVisible(log(2)).text.includes('New result for item 2.'),
  'The second item stays at the top and shows the new entry.');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_log_refresh_restores_the_position_when_a_filter_reveals_the_item(self) -> None:
        self.browser_log_refresh('''
 open(1);
 log(1).scrollTop=239;
 const before=firstVisible(log(1));
 const show=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();
 show('closed');
 check(row(1).hidden,'The filter hides the expanded row.');
 await refresh();
 await refresh();
 check(row(1).hidden,'The row stays hidden after both refreshes.');
 show('working');
 check(!row(1).hidden && !row(1).querySelector('[data-detail]').hidden,'The filter reveals the expanded row.');
 const after=firstVisible(log(1));
 check(after.text===before.text && Math.abs(after.offset-before.offset)<=1,
  'The revealed log restores its entry and offset after two hidden refreshes.');
''')

    def browser_log_refresh(self, steps: str) -> None:
        for number in (1, 2):
            item = self.add(f"Log for item {number}", number=number)
            for update in range(18):
                _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
            for number in (1, 2):
                _ = the_list.update(MORTY.id, f"L{number}", {"next_step": f"New result for item {number}."}, source="morty")
            fresh = list_page.render(self.root / "reviews").decode()
        setup = r'''<script>
const freshList=MARKUP;
window.fetch=async()=>({ok:true,text:async()=>freshList});
</script>'''.replace('MARKUP', json.dumps(fresh).replace('</', '<\\/'))
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const row=number=>document.querySelector('[data-number="'+number+'"]');
const open=number=>row(number).querySelector('.list-toggle').click();
const log=number=>row(number).querySelector('.list-log-scroll');
function firstVisible(log){
 const top=log.getBoundingClientRect().top;
 const entry=[...log.querySelector('.list-log').children].find(entry=>entry.getBoundingClientRect().bottom>top);
 return {text:entry.textContent,offset:entry.getBoundingClientRect().top-top};
}
function refresh(){
 const original=document.querySelector('[data-list]');
 return new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(!original.isConnected){observer.disconnect();resolve();}});
  observer.observe(original.parentNode,{childList:true});
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 });
}
try{
''' + steps + '''
}catch(error){failures.push(String(error));}
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
})();</script>'''
        self.browser_actions(markup, setup, probe, window_size=(1440, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_log_uses_the_configured_height_at_narrow_width(self) -> None:
        item = self.add("Long log for a narrow view", number=1)
        for update in range(18):
            _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        for limit in (8, 12):
            with self.subTest(limit=limit):
                _ = write_config_fixture(self.root / "config/review_links.conf", f"list_log_max_height_rem={limit}\n")
                with patch.object(list_page, "counts_strip", return_value=""):
                    markup = list_page.render(self.root / "reviews").decode()
                probe = r'''<script>
document.querySelector('.list-toggle').click();
const [left,right]=document.querySelector('.list-panel').children;
const log=right.querySelector('.list-log-scroll') ?? right.querySelector('.list-log');
const expected=LIMIT*parseFloat(getComputedStyle(document.documentElement).fontSize);
const fits=right.getBoundingClientRect().top>=left.getBoundingClientRect().bottom &&
 log.clientHeight===expected && log.scrollHeight>log.clientHeight;
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(fits ? [] : ['The narrow log uses the configured height.']);
document.body.append(result);
</script>'''.replace('LIMIT', str(limit))
                self.browser_actions(markup, "", probe, window_size=(500, 900))

    def test_log_entries_share_one_label_column(self) -> None:
        self.assertIn(".list-log dl{display:grid;grid-template-columns:min(7rem,42%) minmax(0,1fr)", list_page.STYLE)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="list-page-test-")))
        config = self.root / "config"
        config.mkdir()
        _ = write_config_fixture(config / "change_review.conf")
        _ = write_config_fixture(config / "review_links.conf", "page_refresh_seconds=15\ndelivery_pause_seconds=0\n")
        _ = write_config_fixture(config / "console.conf",
            "page.list.url=python:scripts.review_links.list_page:page_url\n"
            + "page.list.items=python:scripts.review_links.list_page:waiting_updates\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "STAFF_FILE": str(self.root / "staff.json"),
            "USER_ZONE": "America/New_York", "REVIEW_LINKS_ROOT": str(self.root / "reviews")})))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, DWIGHT, OWNER, OTHER)]}))
        _ = self.enterContext(patch.object(the_list, "now", return_value=NOW))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.enterContext(patch.object(rebuild_row, "read", return_value=rebuild_row.RebuildAnswer((), False)))

    def add(self, title: str, state: the_list.ItemState = the_list.ItemState.WORKING, *,
            owner: the_list.ItemOwner = OWNER.id, number: int | the_list.NextNumber = the_list.NextNumber.AVAILABLE,
            next_check: the_list.NextCheck = the_list.NoTimestamp.UNSET) -> the_list.ListItem:
        return the_list.add(MORTY.id, title, owner=owner, number=number, source="nate", changes={
            "state": state, "next_check": next_check, "next_step": "Read the result and check the page.",
            "latest_status": "The page reads the saved records.", "done_when": "The checks pass.",
            "evidence": "Read https://example.invalid/proof?x=1&y=2.",
            "eta": the_list.EtaRange(NOW + timedelta(hours=1), NOW + timedelta(hours=2))})

    def fixture_items(self) -> list[the_list.ListItem]:
        items = [self.add("Remove the mesh code", the_list.ItemState.WAITING_FOR_NATE, number=58),
                 self.add("Console status", number=105, owner=OTHER.id, next_check=NOW - timedelta(minutes=10)),
                 self.add("The List in the console Straße", number=106, next_check=NOW + timedelta(hours=1)),
                 self.add("Shared documentation structure", the_list.ItemState.PAUSED, number=41),
                 self.add("Read the delivered page", the_list.ItemState.DONE, number=63),
                 self.add("Removed task", number=54)]
        _ = the_list.mark_read(MORTY.id, items[0].id, source="nate")
        _ = the_list.update(MORTY.id, items[1].id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.park(MORTY.id, items[3].id, NOW + timedelta(days=2), source="nate")
        _ = the_list.close(MORTY.id, items[5].id, "The user removed this task.", source="nate")
        return items

    def test_records_share_cli_order_and_each_render_reads_current_fields(self) -> None:
        items = self.fixture_items()
        _ = the_list.update(MORTY.id, items[0].id, {"related_to": (106,),
            "blocked_by": (the_list.ItemDependency(105, "The status page reaches main"),)}, source="morty")
        markup = list_page.render(self.root / "reviews").decode()
        body = without_role_colors(markup)
        self.assertEqual(body.count('<tbody data-list-item'), 6)
        for label in list_page.HEADERS:
            self.assertIn(label + '<span data-sort-mark', body)
        controls = body.split('data-record-filters>', 1)[1].split('<p data-missing-item', 1)[0]
        self.assertEqual(controls.count('<select'), 2)
        self.assertEqual(controls.count('type="search"'), 1)
        self.assertEqual([part.split('"', 1)[0] for part in controls.split('data-filter-row="')[1:]],
                         ['show'])
        self.assertEqual(re.findall(r'data-filter-value="([^"]+)"[^>]*>([^<]+)<b>', controls),
                         [("all", "All"), ("needs-me", "Needs me"), ("morty-answering", "Morty answering"),
                          ("working", "Working"), ("blocked", "Blocked"), ("parked", "Parked"), ("closed", "Closed")])
        self.assertIn('role="switch" id="list-unread" value="1">Unread only</label>', controls)
        owners = controls.split('<select id="list-owner">', 1)[1].split('</select>', 1)[0]
        self.assertRegex(owners, f'value="{OWNER.id}"[^>]*>sdlc</option>')
        self.assertRegex(owners, f'value="{OTHER.id}"[^>]*>builds</option>')
        producers = controls.split('<select id="list-producer">', 1)[1].split('</select>', 1)[0]
        self.assertEqual(re.findall(r'<option value="([^"]*)"[^>]*>([^<]+)</option>', producers),
                         [('', 'All'), (DWIGHT.id, 'dwight'), (MORTY.id, 'morty')])
        self.assertEqual(body.count('<td data-label="Producer">dwight</td>'), 1)
        self.assertEqual(body.count('<td data-label="Producer">morty</td>'), 5)
        self.assertIn(f'data-producer="{DWIGHT.id}"', body)
        for text in ('Latest status', 'Done when', 'Log · newest first', 'Links', 'Closing words',
                     'Check overdue', '14:00 EST', '15:00 EST'):
            self.assertIn(text, body)
        self.assertNotIn('<h3>Evidence</h3>', body)
        self.assertEqual(the_list.load(MORTY.id, items[0].id).evidence, items[0].evidence)
        self.assertEqual(body.count('<form method="post" data-feedback-form>'), 6)
        self.assertEqual(body.count('value="read">Mark read</button>'), 5)
        self.assertEqual([entry.item.number for entry in list_page.records()],
                         [item.number for item in sorted(the_list.records(MORTY.id, include_closed=True), key=the_list.order)])
        closed = body.split(f'data-key="{MORTY.id}/L54"', 1)[1].split('</tbody>', 1)[0]
        self.assertNotIn('Check overdue', closed)
        self.assertNotIn('value="read">Mark read</button>', closed)
        with patch.dict(list_page.FIELD_LABELS, {"closing_words": "Recorded closing words"}):
            renamed = list_page.render(self.root / "reviews").decode()
        self.assertIn("<h3>Recorded closing words</h3>", renamed)
        self.assertIn("<dt>Recorded closing words</dt>", renamed)
        _ = the_list.update(MORTY.id, "L106", {"title": "New title <script>", "latest_status": "New status"}, source="morty")
        changed = list_page.render(self.root / "reviews").decode()
        self.assertIn('New title &lt;script&gt;', changed)
        self.assertNotIn('<script>New title', changed)
        self.assertIn('New status', changed)

    def test_linked_reviews_share_one_current_read_and_link_to_the_review_cards(self) -> None:
        def change(identity: str, state: review_items.ReviewState) -> review_items.ReviewItem:
            source = review_items.ChangeSource(self.root / "changes.md", {"#": identity},
                change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
                delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
            return review_items.ReviewItem("sdlc/" + identity, identity, review_items.ReviewType.CHANGE,
                OWNER.id, OWNER.id, "Check " + identity, state, review_items.ReviewTimes(NOW, NOW), (), (), source)

        ready = (change("C1", review_items.ReviewState.OPEN), change("C2", review_items.ReviewState.OPEN))
        waiting = change("C3", review_items.ReviewState.WAITING)
        done = change("C4", review_items.ReviewState.DONE)
        later = replace(change("C5", review_items.ReviewState.OPEN),
                        times=review_items.ReviewTimes(NOW, NOW, NOW + timedelta(days=1)))
        question = investigation.file(OWNER.id, "Find the cause", "Why did it stop?", waiting_why='The owner must complete the investigation.')
        _ = investigation.answer(OWNER.id, question.id, "The cause is known.")
        answer = review_items.investigation_items(staff.snapshot())[0]
        reviews = [*ready, waiting, done, later, answer]
        cases = (
            ((ready[0].key, ready[1].key), "2 changes for you"),
            ((ready[0].key, answer.key), "2 reviews for you"),
            ((waiting.key, done.key, "missing/C9"), "1 review waiting on others"),
            ((waiting.key, later.key), "2 reviews waiting on others"),
            ((answer.key,), "1 review for you"),
            ((done.key,), "Reviews done — decide: close, keep or park"),
            (("missing/C9",), "Reviews done — decide: close, keep or park"),
            ((), ""),
            ((later.key,), "1 review waiting on others"),
            ((ready[0].key, waiting.key, done.key), "1 change for you"),
        )
        items: list[the_list.ListItem] = []
        for index, (identities, _) in enumerate(cases):
            item = self.add(f"Review group {index}")
            items.append(the_list.link(MORTY.id, item.id, identities, source="morty"))
        paths = [the_list.folder(MORTY.id) / f"{item.id}.md" for item in items]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        root = self.root / "reviews"
        with patch.object(review_items, "read", return_value=reviews) as read, patch.object(list_page, "counts_strip", return_value=""):
            body = list_page.render(root).decode()
        read.assert_called_once_with(root, members=staff.snapshot())
        for item, (identities, expected) in zip(items, cases):
            with self.subTest(item=item.id):
                row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                state = row.split('<td data-label="State">', 1)[1].split('</td>', 1)[0]
                if not identities:
                    self.assertNotIn(review_page.PAGE_PATH, row)
                    continue
                self.assertIn(expected, state)
                self.assertEqual(row.count(expected), 2)
                found = [key for key in identities if any(review.key == key for review in reviews)]
                if not found:
                    self.assertNotIn('<a ', state)
                else:
                    target = ([('show', 'open'), ('jump', found[0])] if len(found) == 1
                              else [('key', key) for key in found])
                    place = review_page.PAGE_PATH + '?show=all#' + urlencode(target)
                    href = html.escape('/console/' + console.place_query('review', place), quote=True)
                    self.assertIn(f'href="{href}" target="_top" data-list-review-link="">{expected}</a>', state)
                detail = row.split('<div class="list-panel"><div>', 1)[1]
                self.assertLess(detail.index(expected), detail.index('<form'))
                positions: list[int] = []
                for identity in identities:
                    matching = [review for review in reviews if review.key == identity]
                    place = review_page.PAGE_PATH + '?show=all#' + urlencode({'show': 'open', 'jump': identity})
                    href = html.escape('/console/' + console.place_query('review', place), quote=True)
                    anchor = f'<a href="{href}" target="_top" data-list-review-link="">'
                    if matching:
                        card = f'{anchor}{html.escape(matching[0].id)}</a>'
                    else:
                        card = html.escape(identity)
                        self.assertNotIn(f'{anchor}{card}</a>', detail)
                    self.assertIn(card, detail)
                    positions.append(detail.index(card))
                self.assertEqual(positions, sorted(positions))
        with patch.object(review_items, "read", return_value=[
                replace(review, state=review_items.ReviewState.DONE) for review in reviews]) as read:
            refreshed = list_page.render(root).decode()
        read.assert_called_once()
        first = refreshed.split(f'data-key="{MORTY.id}/{items[0].id}"', 1)[1].split('</tbody>', 1)[0]
        self.assertIn("Reviews done — decide: close, keep or park", first)
        self.assertNotIn("2 changes for you", first)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_unlinked_rows_do_not_read_the_review_stores(self) -> None:
        _ = self.add("Work without reviews")
        with patch.object(review_items, "read", side_effect=AssertionError("No review read is needed")) as read:
            body = list_page.render(self.root / "reviews").decode()
        read.assert_not_called()
        self.assertIn('<strong>Work without reviews</strong>', body)
        self.assertNotIn("Reviews done — decide", body)

    def test_review_keys_keep_duplicate_ids_distinct_and_link_only_supported_cards(self) -> None:
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1"},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        ready = review_items.ReviewItem("change/sdlc/C1", "C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check C1", review_items.ReviewState.OPEN, review_items.ReviewTimes(NOW, NOW), (), (), source)
        done = replace(ready, key="change/builds/C1", state=review_items.ReviewState.DONE)
        proposal = replace(ready, key="proposal/sdlc/P1", id="P1", type=review_items.ReviewType.PROPOSAL,
                           source=review_items.ProposalLedgerSource(self.root / "proposals.md"))
        efficiency = replace(proposal, key="efficiency/sdlc/E1", id="sdlc/E1", type=review_items.ReviewType.EFFICIENCY,
                             source=review_items.EfficiencySource(self.root / "efficiencies.md", "Result"))
        for review, expected in ((ready, "1 change for you"), (done, "Reviews done — decide: close, keep or park"),
                                 (proposal, "1 review for you"), (efficiency, "1 review for you")):
            with self.subTest(key=review.key):
                item = self.add("Review " + review.key)
                _ = the_list.link(MORTY.id, item.id, (review.key,), source="morty")
                with patch.object(review_items, "read", return_value=[ready, done, proposal, efficiency]) as read, patch.object(
                        list_page, "counts_strip", return_value=""):
                    body = list_page.render(self.root / "reviews").decode()
                read.assert_called_once()
                row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                self.assertIn(expected, row)
                place = review_page.PAGE_PATH + '?show=all#' + urlencode({'show': 'open', 'jump': review.key})
                card = '/console/' + console.place_query('review', place)
                if review.type in review_page.TYPES:
                    self.assertIn(f'<a href="{html.escape(card, quote=True)}" target="_top" data-list-review-link="">{review.id}</a>', row)
                else:
                    self.assertNotIn(card, row)
                    self.assertIn('<p>' + review.id + '</p>', row)

    def test_unknown_owners_and_unassigned_owners_keep_their_labels(self) -> None:
        missing = staff.StaffId("owner-without-row")
        for owner in (missing, the_list.UnassignedOwner.NATE, the_list.UnassignedOwner.NOT_RECORDED):
            _ = self.add("Work for " + owner, owner=owner)
        body = list_page.render(self.root / "reviews").decode()
        owners = body.split('<select id="list-owner">', 1)[1].split('</select>', 1)[0]
        for label in (str(missing), "nate", "Not recorded"):
            self.assertIn('>' + label + '</option>', owners)
        _ = (self.root / "staff.json").write_text('{"schema":1,"rows":[]}')
        self.assertEqual(len(list_page.records()), 3)

    def test_feedback_button_belongs_to_the_words_box_and_starts_disabled(self) -> None:
        item = self.add("Explain this item")
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), "fixture-secret", NOW, ())
        actions, words = form.split('</div>', 1)
        self.assertNotIn('value="tell"', actions)
        self.assertNotIn('<label for="words-', words)
        self.assertIn('placeholder="Your words for morty about this item"', words)
        self.assertIn('aria-label="Your words for morty about this item"', words)
        self.assertIn('</textarea><div class="list-tell"><span role="status" data-send-status></span>'
                      + '<button name="action" value="question" disabled>Ask question</button>'
                      + '<button name="action" value="tell" disabled>Tell morty</button></div>', words)
        self.assertIn("!words.value.trim()", list_page.SCRIPT)
        self.assertIn("words.addEventListener('input',retain)", list_page.SCRIPT)
        self.assertIn("buttons.forEach(button=>button.disabled=false);syncTell()", list_page.SCRIPT)
        self.assertIn('rows="4"', words)

    def test_record_fields_share_the_markdown_formatter(self) -> None:
        item = self.add("Formatted record")
        names = (("latest_status", "Status"), ("done_when", "Completion"), ("next_step", "Next"))

        def text(heading: str) -> str:
            return f"## {heading}\n\n**{heading} field:** Ready.\n- One\n- Two"

        _ = the_list.update(MORTY.id, item.id, {"latest_status": text("Status"), "done_when": text("Completion"),
            "evidence": text("Evidence"), "next_step": text("Next")}, source="morty")
        _ = the_list.close(MORTY.id, item.id, "## Closing\n\n**Closing field:** Closed.\n1. First\n2. Second", source="nate")
        body = list_page.render(self.root / "reviews").decode()
        next_step = body.split('<td data-label="Next step">', 1)[1].split('</td>', 1)[0]
        details = body.split('<div class="list-panel"><div>', 1)[1].split('<section class="list-log-panel"><h3>Log · newest first', 1)[0]
        self.assertNotIn('<h3>Evidence</h3>', details)
        self.assertNotIn('<h2>Evidence</h2>', details)
        self.assertEqual(the_list.load(MORTY.id, item.id).evidence, text("Evidence"))
        for _, heading in (*names, ("closing_words", "Closing")):
            rendered = next_step if heading == "Next" else details
            self.assertIn(f'<h2>{heading}</h2>', rendered)
            self.assertIn(f'<strong>{heading} field:</strong>', rendered)
            self.assertNotIn('**', rendered)
            self.assertNotIn('## ', rendered)
        self.assertIn('<ul><li>One</li><li>Two</li></ul>', details)
        self.assertIn('<ol><li>First</li><li>Second</li></ol>', details)

    def test_bad_records_leave_valid_rows_and_nav_news(self) -> None:
        good = self.add("Valid item")
        bad = self.add("Invalid item")
        path = the_list.folder(MORTY.id) / f"{bad.id}.md"
        _ = path.write_text(path.read_text().replace('state: "Working"', 'state: "bad"'))
        errors = io.StringIO()
        with redirect_stderr(errors):
            body = list_page.render(self.root / "reviews").decode()
            news = list_page.waiting_updates(console.NewsRequest(self.root))
        self.assertIn('<strong>Valid item</strong>', body)
        self.assertNotIn('<strong>Invalid item</strong>', body)
        self.assertEqual(news, frozenset({f"{MORTY.id}/{good.id}/{NOW.isoformat()}"}))
        self.assertEqual(len(errors.getvalue().splitlines()), 2)

    def test_nav_marks_new_update_times_and_keeps_read_operations_out_of_the_page(self) -> None:
        pages = tuple(page for page in console.read_pages() if page.identity == "list")
        root = self.root / "reviews"
        self.assertEqual(list_page.page_url(), '/console/list.html')
        self.assertEqual(console.news(root, pages, "changes"), ())
        item = self.add("Unread item")
        self.assertEqual(console.news(root, pages, "changes"), ("list",))
        self.assertEqual(console.news(root, pages, "list"), ())
        self.assertEqual(console.news(root, pages, "changes"), ())
        with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=1)):
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new result arrived."}, source="morty")
        self.assertEqual(console.news(root, pages, "changes"), ("list",))
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        self.assertEqual(console.news(root, pages, "changes"), ())

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unread_excludes_legacy_closed_records(self) -> None:
        open_item = self.add("Open with an unread update", number=1)
        closed = self.add("Closed with a stored unread time", number=2)
        _ = the_list.close(MORTY.id, closed.id, "The report is retired.", source="nate")
        _ = the_list.mark_read(MORTY.id, closed.id, source="nate")
        path = the_list.folder(MORTY.id) / f"{closed.id}.md"
        field = f'unread_since: {json.dumps(NOW.isoformat())}\n'
        stored = path.read_text().replace('unread_since: ""\n', field, 1)
        self.assertIn(field, stored)
        _ = path.write_text(stored)
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        probe = '''<script>
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const chip=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')];
const unread=document.getElementById('list-unread');
unread.click();
check(chip('all').querySelector('b').textContent==='1','All counts only the unread item when the switch is on.');
check(visible().map(row=>row.dataset.number).join() === '1','the Unread view shows only the open item');
check(document.querySelector('[data-list-count]').textContent.endsWith(' · 1 unread'),'the page total counts one unread item');
unread.click();
chip('closed').click();
const closed=visible()[0];
check(closed?.dataset.number==='2','the closed record remains in Closed');
check(closed?.querySelector('[aria-label="No unread update"]'),'the closed record has no unread dot');
check(!closed?.querySelector('[value="read"]'),'the closed record has no Mark read button');
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
</script>'''
        self.browser_actions(markup, "", probe)
        self.assertEqual(list_page.waiting_updates(console.NewsRequest(self.root)),
                         frozenset({f"{MORTY.id}/{open_item.id}/{NOW.isoformat()}"}))
        self.assertEqual(list_page.saved_action_state(MORTY.id, closed.id)["unread"], "false")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_status_counts_partition_all_items_with_and_without_unread(self) -> None:
        for offset in (0, 8):
            for number, state in enumerate(the_list.ItemState, start=1 + offset):
                closed = state is the_list.ItemState.CLOSED
                item = self.add(f"Check {state.value}", the_list.ItemState.WORKING if closed else state, number=number)
                if offset:
                    _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="nate")
                if closed:
                    _ = the_list.close(MORTY.id, item.id, "The work is complete.", source="nate")
                elif number in (2, 5, 7, 10, 12, 14):
                    _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        for number, state in ((17, the_list.ItemState.DELIVERED), (18, the_list.ItemState.WORKING),
                              (19, the_list.ItemState.WORKING), (20, the_list.ItemState.BLOCKED)):
            item = self.add("Check the question result", state, number=number)
            if number == 18:
                item = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="nate")
            outcome = question_outcome("refused", "Producer offline") if number == 19 else question_outcome()
            with (patch.object(change_page, "send_question", return_value=outcome),
                  patch.object(change_page, "alert_delivery_failures")):
                list_page.deliver(list_page.submit(root, urlencode(
                    self.fields(item, "question", words="Which check passed?")), status))
            if number in (18, 19):
                _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new check passed."}, source="morty")
            if number == 20:
                with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1",
                                                    "--text", "The gate passed."]), 0)
        markup = list_page.render(root, status=status, include_counts=False).decode()
        controls = markup.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'data-filter-count="(\d+)"', controls), ["20", "2", "3", "2", "3", "6", "4"])
        probe = '''<script>
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const chips=[...document.querySelectorAll('[data-filter-row="show"] button')];
const unread=document.getElementById('list-unread');
const total=document.querySelector('[data-list-count]');
const entries=[
 ['all',Array.from({length:20},(_,index)=>index+1),[1,3,4,6,9,11,13,15,18,19,20]],
 ['needs-me',[4,5],[4]],['morty-answering',[17,18,19],[18,19]],['working',[1,2],[1]],['blocked',[3,7,20],[3,20]],
 ['parked',[9,10,11,12,13,15],[9,11,13,15]],['closed',[6,8,14,16],[6]],
];
const count=button=>Number(button.dataset.filterCount);
const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')]
 .map(row=>Number(row.dataset.number)).sort((a,b)=>a-b);
for(const onlyUnread of [false,true]){
 if(unread.checked!==onlyUnread) unread.click();
 check(count(chips[0])===chips.slice(1).reduce((sum,button)=>sum+count(button),0),'The six counts sum to All.');
 check(total.textContent.includes(' · '+count(chips[1])+' need you · '),'The total equals the Needs me count.');
 entries.forEach(([name,all,unreadItems],index)=>{
  const expected=onlyUnread ? unreadItems : all;
  const button=chips[index];
  button.click();
  check(button.dataset.filterValue===name && count(button)===expected.length && visible().join()===expected.join(),
   name+' counts and shows its items with Unread only '+(onlyUnread ? 'on' : 'off')+'.');
 });
}
const row=number=>document.querySelector('[data-number="'+number+'"]');
const expand=number=>row(number).querySelector('.list-toggle').click();
const detail=number=>row(number).querySelector('[data-detail]:not([hidden])');
unread.click();
chips[2].click();
expand(17);expand(18);expand(19);
check(!row(17).hidden && detail(17)?.querySelector('[data-waiting-note]'),
 'The unparked item shows the waiting note in Morty answering.');
check(!row(18).hidden && row(18).dataset.parked==='true' && detail(18)?.querySelector('[data-waiting-note]'),
 'The parked item shows the waiting note in Morty answering.');
check(!row(19).hidden && detail(19)?.querySelector('[value="resend-Q1"]'),
 'The failed question stays in Morty answering with Resend.');
chips[4].click();
document.getElementById('list-expand-all').click();
const answer=detail(20)?.querySelector('.list-panel [data-question="Q1"]');
check(!row(20).hidden && row(20).querySelector('[aria-label="Unread update"]') &&
 answer?.textContent.includes('The gate passed.') && answer.querySelector('.question-unread') &&
 !detail(20).querySelector('[data-log-index="Q1-answered"]'),
 'The answered item returns to Blocked with its unread answer in the panel.');
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
</script>'''
        self.browser_actions(markup, "", probe)

    def test_count_strip_colors_its_showrunner_with_the_page_name_reading(self) -> None:
        members = staff.StaffSnapshot((replace(OWNER, doc=staff.LiveDoc(local_machine(), self.root / "sdlc.md")),))
        with (patch.object(staff, "snapshot", return_value=members),
              patch.object(list_page, "change_counts", return_value={"sdlc": 1}),
              patch.object(accounts, "role_colors",
                           wraps=accounts.role_colors) as colors,
              patch.object(counts_row, "question_counts", return_value=(0, ""))):
            body = list_page.render(self.root / "reviews").decode()
        colors.assert_called_once()
        chip = body.split('<span class="list-count-chip">', 1)[1].split('<a ', 1)[0]
        self.assertRegex(chip, r'data-color-text="role.showrunner"[^>]*>sdlc</span> · ')

    def test_counts_use_status_line_readers(self) -> None:
        owners: list[staff.StaffRow] = []
        listings: list[Path] = []
        for owner, count in ((OWNER, 3), (OTHER, 1)):
            doc = self.root / f"{owner.name}.md"
            listing = self.root / f"{change_review.PREFIX}{owner.name}.md"
            _ = doc.write_text(f'# Production\n- **Change review:** `{listing}`\n')
            _ = listing.write_text('## To review\n\n| # | Change | How to check | Producer |\n'
                + '| --- | --- | --- | --- |\n'
                + ''.join(f'| C{number} | Work | Read it | {owner.id} |\n' for number in range(1, count + 1)))
            owners.append(replace(owner, doc=staff.LiveDoc(local_machine(), doc)))
            listings.append(listing)
        members = staff.StaffSnapshot(tuple(owners))
        with listings[0].open('a') as output:
            _ = output.write(f'| C4 | A change for builds | Read it | {OTHER.id} |\n')
        _ = self.add("A valid List row")
        with (patch.object(counts_row, "question_counts", return_value=(2, "")) as questions,
              patch.object(staff, "snapshot", return_value=members),
              patch.object(showrunners, "timer_docs", return_value={owner.name: owner.doc.path
                  for owner in owners if isinstance(owner.doc, staff.LiveDoc)}),
              patch.object(change_review, "lists_directory", return_value=self.root),
              patch.object(change_review, "review_snapshot", wraps=change_review.review_snapshot) as read,
              patch.object(change_routing, "StaffRouting", wraps=change_routing.StaffRouting) as routing,
              patch.object(session_staff, "member_sources", side_effect=ValueError("Invalid pane record")) as panes,
              patch.object(rebuild_row, "read", return_value=rebuild_row.RebuildAnswer(("mac",), False)) as rebuilds):
            body = list_page.counts_strip(members, staff_names.StaffNames(members))
            self.assertEqual(questions.call_count, 2)
            self.assertEqual(questions.call_args_list[0].args[0], owners[1])
            self.assertEqual(questions.call_args_list[1].args[0], owners[0])
            self.assertCountEqual([call.args[0] for call in read.call_args_list], listings)
            routing.assert_called_once()
            panes.assert_not_called()
            rebuilds.assert_called_once_with(conf.path("statusline"))
            root = self.root / "reviews"
            root.mkdir(exist_ok=True)
            status = change_page.DeliveryStatus(root)
            with patch.object(QuestionStaff, "for_record", return_value=QuestionStaff(members, session_staff.MemberSources(()))):
                for validation in (False, True):
                    with self.subTest(validation=validation):
                        if validation:
                            page = change_page.ReviewPage(listings[0], listings[0], ("C1",), "")
                            _ = status.path(page, "C1").write_text(json.dumps({"validation": {
                                "attempt": "recorded", "state": "pending", "feedback": "Check it"}}))
                        forms = BrowserForms()
                        forms.feed(change_browser.render(change_page.ReviewBrowser("fixture"), status).decode())
                        totals = list_page.change_counts()
                        self.assertEqual(totals, forms.counts)
                        self.assertEqual(totals, {"sdlc": 2 if validation else 3, "builds": 2})
                        chips = list_page.counts_strip(members, staff_names.StaffNames(members))
                        for name, count in totals.items():
                            chip = without_role_colors(chips).split(name + ' · ', 1)[1].split('</span>', 1)[0]
                            self.assertIn(f'<b>{count}</b> changes', chip)
            for reader, label in (("questions", "questions"), ("changes", "changes"), ("rebuilds", "rebuild")):
                target = (counts_row if reader == "questions" else list_page if reader == "changes" else rebuild_row)
                function = {"questions": "question_counts", "changes": "change_counts", "rebuilds": "read"}[reader]
                with (self.subTest(failed_count=reader), patch.object(target, function, side_effect=OSError("Unavailable")),
                      patch.object(staff, "snapshot", return_value=members)):
                    available = list_page.render(self.root / "reviews").decode()
                    self.assertIn('<strong>A valid List row</strong>', available)
                    self.assertNotIn(f'</b> {label}<', available)
                    self.assertNotIn(f'</b> {label}s<', available)
                    self.assertIn('</b> changes<' if reader == "questions" else '</b> questions<', available)
        for page, place in (("questions", '/console/changes.html?type=questions#showrunner=sdlc'),
                            ("changes", '/console/changes.html#showrunner=sdlc'),
                            ("rebuilds", '/console/rebuilds.html')):
            link = '/console/' + console.place_query(page, place)
            self.assertIn(f'href="{html.escape(link, quote=True)}" target="_top"', body)
        for text in ('<b>2</b> questions', '<b>3</b> changes', '<b>1</b> rebuild'):
            self.assertIn(text, body)
        self.assertNotIn("proposals", body)
        self.assertNotIn("efficiencies", body)

    def fields(self, item: the_list.ListItem, action: str, **changes: str) -> dict[str, str]:
        folder = change_page.console_folder(self.root / "reviews")
        secret = change_browser.browser_manifest(folder)[0]
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), secret, NOW, ())
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))
        return {**fields, "action": action, "words": "", "until": "", **changes}

    def test_unpark_saves_before_delivery_and_sends_one_notice(self) -> None:
        root = self.root / "reviews"
        item = self.add("Unpark this item", number=106)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        fields = self.fields(item, "unpark")
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            saved = the_list.load(MORTY.id, item.id)
            self.assertIs(saved.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(saved.state, item.state)
            self.assertEqual(saved.log[-2].what, the_list.Unparked())
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "")
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, 1)
            self.assertEqual((send.call_args.args[0], send.call_args.args[2]),
                             (MORTY.id, "List page: nate unparked item 106."))
            delivered = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(the_list.load(MORTY.id, item.id), delivered)
            fields = self.fields(delivered, "unpark")
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            receipt = the_list.UnparkNoChangeReceipt(
                the_list.PageRequest(fields["request"], "unpark", "List page: nate unparked item 106."),
                the_list.UnparkNoChangeReason.NO_ACTIVE_PARKING)
            self.assertEqual(saved.log[-1].what, receipt)
            self.assertEqual(replace(saved, log=saved.log[:-1]), delivered)
            self.assertEqual(send.call_count, 1)

    def test_remove_closes_before_delivery_and_repeated_requests_do_nothing(self) -> None:
        item = self.add("Remove this item now")
        fields = self.fields(item, "remove")
        root = self.root / "reviews"
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            closed = the_list.load(MORTY.id, item.id)
            self.assertEqual(closed.state, the_list.ItemState.CLOSED)
            self.assertEqual(sum(isinstance(entry.what, the_list.FieldChange) and entry.what.action == "Closed"
                                 for entry in closed.log), 1)
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_args.args[2], f"List page: nate removed item {item.number}.")
            saved = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_writes_the_date_before_delivery_and_repeated_requests_do_nothing(self) -> None:
        item = self.add("Park this item now")
        fields = self.fields(item, "park", until="2026-12-03")
        root = self.root / "reviews"
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            parked = the_list.load(MORTY.id, item.id)
            self.assertEqual(parked.parked_until, the_list.time_value("2026-12-03"))
            self.assertEqual(parked.state, item.state)
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_args.args[2], f"List page: nate parked item {item.number} until 2026-12-03.")
            saved = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_refused_remove_and_notice_write_failure_keep_the_item(self) -> None:
        item = self.add("Keep this item when Remove fails")
        root = self.root / "reviews"
        with patch.object(the_list, "close", side_effect=the_list.ListError("The fixture refuses this close.")):
            with self.assertRaisesRegex(the_list.ListError, "refuses this close"):
                _ = list_page.submit(root, urlencode(self.fields(item, "remove")))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        with patch.object(the_list, "_save", side_effect=OSError("The fixture refuses this write.")):
            with self.assertRaises(OSError):
                _ = list_page.submit(root, urlencode(self.fields(item, "remove")))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_read_rejects_a_saved_park_request_and_its_retry_preserves_newer_updates(self) -> None:
        item = self.add("Keep each request with its action")
        root = self.root / "reviews"
        park = self.fields(item, "park", until="2026-12-03")
        _ = list_page.submit(root, urlencode(park))
        parked = the_list.load(MORTY.id, item.id)
        with self.assertRaisesRegex(the_list.ListError, "different action or words"):
            _ = list_page.submit(root, urlencode({**park, "action": "read"}))
        self.assertEqual(the_list.load(MORTY.id, item.id), parked)

        read = self.fields(parked, "read")
        _ = list_page.submit(root, urlencode(read))
        self.assertIs(the_list.load(MORTY.id, item.id).unread_since, the_list.NoTimestamp.UNSET)
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "Keep this newer update unread."}, source="morty")
        _ = list_page.submit(root, urlencode(read))
        self.assertEqual(the_list.load(MORTY.id, item.id), newer)
        for changed in ({"action": "remove"}, {"words": "Different words."}):
            with self.subTest(changed=changed), self.assertRaisesRegex(the_list.ListError, "different action or words"):
                _ = list_page.submit(root, urlencode({**read, **changed}))
        self.assertEqual(the_list.load(MORTY.id, item.id), newer)

    def test_park_retry_succeeds_after_the_date_arrives(self) -> None:
        item = self.add("Retry this Park after midnight")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-03")
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            with patch.object(the_list, "now", return_value=datetime(2026, 12, 3, 5, tzinfo=UTC)):
                list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_saves_the_picked_instant_in_the_user_zone(self) -> None:
        item = self.add("Park until a time")
        root = self.root / "reviews"
        until = "2026-12-04T00:30:00.000Z"
        _ = list_page.submit(root, urlencode(self.fields(item, "park", until=until)))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.parked_until, datetime(2026, 12, 4, 0, 30, tzinfo=UTC))
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "2026-12-03 19:30 EST")
        body = list_page.render(root).decode()
        self.assertIn("Parked until 2026-12-03 19:30 EST</small>", body)
        self.assertIn("Parked until</dt><dd>2026-12-03 19:30 EST</dd>", body)

    def test_park_retry_compares_the_saved_instant_to_the_second(self) -> None:
        item = self.add("Retry the exact parking time")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-04T00:30:05Z")
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            with self.assertRaisesRegex(the_list.ListError, "different parking time"):
                _ = list_page.submit(root, urlencode({**fields, "until": "2026-12-04T00:30:55Z"}))
            same_time = {**fields, "until": "2026-12-03T19:30:05-05:00"}
            list_page.deliver(list_page.submit(root, urlencode(same_time)))
            send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_retry_accepts_a_legacy_date_only_record(self) -> None:
        item = self.add("Retry a Park saved by the old page")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-03")
        request_fields = the_list._request_fields  # pyright: ignore[reportPrivateUsage]

        def legacy_fields(request: the_list.PageRequest) -> dict[str, str]:
            return {key: value for key, value in request_fields(request).items() if key != "parked_until"}

        with (patch.object(the_list, "_request_fields", side_effect=legacy_fields),
              patch.object(change_page, "send_to_staff") as send):
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            later = datetime(2026, 12, 6, 5, tzinfo=UTC)
            saved = the_list.park(MORTY.id, item.id, later, source="morty")
            with patch.object(the_list, "now", return_value=datetime(2026, 12, 3, 5, tzinfo=UTC)):
                list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_retry_refuses_an_earlier_park_when_its_own_event_is_missing(self) -> None:
        item = self.add("Retry a Park with missing evidence")
        root = self.root / "reviews"
        earlier = self.fields(item, "park", until="2026-12-03")
        original = {**earlier, "request": earlier["request"] + "." + "a" * 32, "until": "2026-12-04"}
        request_fields = the_list._request_fields  # pyright: ignore[reportPrivateUsage]

        def legacy_fields(request: the_list.PageRequest) -> dict[str, str]:
            return {key: value for key, value in request_fields(request).items() if key != "parked_until"}

        with patch.object(the_list, "_request_fields", side_effect=legacy_fields):
            _ = list_page.submit(root, urlencode(earlier))
            _ = list_page.submit(root, urlencode(original))
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        body, marker, log = path.read_text().partition("## Log\n\n")
        lines = log.splitlines()
        self.assertIn('"action": "Parked"', lines[-2])
        del lines[-2]
        _ = path.write_text(body + marker + "\n".join(lines) + "\n")
        saved = the_list.load(MORTY.id, item.id)
        with self.assertRaisesRegex(the_list.ListError, "different parking time"):
            _ = list_page.submit(root, urlencode({**original, "until": earlier["until"]}))
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_new_park_checks_the_date_at_the_locked_write(self) -> None:
        item = self.add("Refuse a Park that reaches the writer at midnight")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-03")
        midnight = datetime(2026, 12, 3, 5, tzinfo=UTC)
        park = the_list.park

        def at_midnight(keeper: staff.StaffId, identity: str, until: the_list.Timestamp, *,
                        source: str, notice: the_list.PageItemNotice) -> the_list.ListItem:
            with patch.object(the_list, "now", return_value=midnight):
                return park(keeper, identity, until, source=source, notice=notice)

        with patch.object(the_list, "park", side_effect=at_midnight):
            with self.assertRaisesRegex(ValueError, "Choose a future parking date"):
                _ = list_page.submit(root, urlencode(fields))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_item_actions_unread_sync_and_words_fields(self) -> None:
        _ = self.fixture_items()
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
const requests=[];let finishPost;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 requests.push(new URLSearchParams(options.body));
 return new Promise(resolve=>finishPost=result=>resolve({ok:result.ok!==false,status:result.ok===false ? 400 : 200,
  json:async()=>({unread:'false',...result})}));
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const row=number=>document.querySelector('[data-number="'+number+'"]');
 const visible=()=>[...document.querySelectorAll('[data-list-item]')].filter(item=>!item.hidden).map(item=>item.dataset.number).sort().join(',');
 const choose=(show,query)=>new Promise(done=>{
  document.querySelector('[data-filter-row="show"] [data-filter-value="'+show+'"]').click();
  const field=document.getElementById('list-search');
  field.addEventListener('filterchange',done,{once:true});field.value=query;field.dispatchEvent(new Event('input'));
 });
 const submit=(number,action)=>{const form=row(number).querySelector('form');form.requestSubmit(form.querySelector('[value="'+action+'"]'));return form;};
 try{
  for(const [query,expected] of [['105','105'],['L105','105'],['#105','105'],['BUILDS','105'],
    ['Straße','106'],['WORKING','105,106'],['Waiting','58'],['Parked','41'],['Closed','54']]){
   await choose('all',query);check(visible()===expected,'words filter: '+query);
  }
  await choose('working','106');row('106').querySelector('.list-toggle').click();
  const removed=submit('106','remove');
  check(row('106').hidden,'Remove hides the row before the response');
  check(row('106').dataset.unread==='false' && !row('106').querySelector('.list-dot') &&
   !removed.querySelector('[value="read"]'),'Remove clears the unread controls before the response');
  check([...row('106').querySelectorAll('button')].every(button=>button.disabled),'Remove disables the row buttons');
  submit('106','remove');check(requests.length===1,'a second Remove press sends nothing');
  finishPost({item_state:'Closed',parked:'false'});await flush();await flush();
  check(row('106').hidden,'Remove stays outside Working after success');
  await choose('closed','106');check(visible()==='106','Remove updates the Closed view');
  check(row('106').querySelector('.list-state').textContent==='Closed','Remove updates the state text');
  await choose('working','105');row('105').querySelector('.list-toggle').click();
  const refused=submit('105','remove');check(row('105').hidden,'a refused Remove hides the row while pending');
  finishPost({ok:false,reason:'The fixture refuses this close.'});await flush();await flush();
  check(!row('105').hidden && row('105').dataset.state==='Working','a refused Remove restores the row');
  check(row('105').dataset.unread==='true' && row('105').querySelector('.list-dot') &&
   refused.querySelector('[value="read"]'),'a refused Remove restores the unread controls');
  check(refused.querySelector('[data-send-status]').textContent==='Not saved: The fixture refuses this close.','a refused Remove shows its reason');
  check(!refused.querySelector('[value="remove"]').disabled,'a refused Remove enables another attempt');
  refused.elements.until.value='2026-12-03';const parked=submit('105','park');
  check(row('105').hidden,'Park hides the row before the response');
  check(document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
   'Park animates the row when it leaves Working');
  submit('105','park');check(requests.length===3,'a second Park press sends nothing');
  finishPost({item_state:'Working',parked:'true'});await flush();await flush();
  check(row('105').hidden,'Park stays outside Working after success');
  await choose('parked','105');check(visible()==='105','Park updates the Parked view');
  check(parked.querySelector('[data-send-status]').textContent==='Parked','Park shows its result');
  await choose('all','58');row('58').querySelector('.list-toggle').click();
  const form=row('58').querySelector('form');
  check(row('58').dataset.unread==='false' && !form.querySelector('[value="read"]'),'the unread sync case starts read');
  form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
  submit('58','tell');finishPost({item_state:'Waiting for nate',parked:'false',unread:'true'});await flush();await flush();
  check(row('58').dataset.unread==='true' && row('58').querySelector('.list-dot'),'an unread response restores the dot');
  check(Boolean(form.querySelector('[value="read"]')),'an unread response restores Mark read');
  if(form.querySelector('[value="read"]')){
   document.getElementById('list-unread').click();
   submit('58','read');finishPost({item_state:'Waiting for nate',parked:'false',unread:'false'});await flush();await flush();
   check(!row('58').querySelector('.list-dot') && !form.querySelector('[value="read"]'),'the restored Mark read button works');
   check(row('58').hidden && document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
    'Mark read animates the row when it leaves Unread');
  }
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe, virtual_time_ms=3000)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_opens_the_shared_picker_and_submits_the_chosen_time(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 const toggle=form.querySelector('[data-later-toggle]');
 check(!form.querySelector('input[type=date]'),'Park has no native date input');
 toggle.click();
 const sheet=form.querySelector('[data-later-custom]');
 check(!sheet.hidden && !sheet.closest('[data-later-menu]').hidden,'Park opens the shared sheet');
 check(document.activeElement.dataset.date==='2026-12-03','Focus starts on the selected date');
 sheet.querySelector('[data-date="2026-12-03"]').click();
 const field=sheet.querySelector('[data-later-time]');
 field.value='1930';field.dispatchEvent(new Event('input',{bubbles:true}));
 check(sheet.querySelector('[data-later-summary]').textContent==='Parks until Thu 3 Dec 19:30',
  'The shared sheet shows the 24-hour time in the user zone');
 check(!requests.length,'Changing the selection does not submit the form');
 sheet.querySelector('[value="park"]').click();
 check(sheet.hidden && requests.length===1,'Save closes the shared sheet and submits once');
 check(requests[0]?.get('action')==='park' && requests[0].get('until')==='2026-12-04T00:30:00.000Z',
  'Save sends the selected instant');
 finishPost({item_state:'Working',parked:'true',parked_until:'2026-12-03 19:30 EST'});
 await flush();
 check(row.querySelector('[data-parked-note]').textContent==='Parked until 2026-12-03 19:30 EST',
  'The row shows the saved time');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_focus_moves_to_the_calendar_when_previous_month_disables(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
 form.querySelector('[data-later-toggle]').click();
 const previous=form.querySelector('[data-later-month-step="-1"]');
 const grid=form.querySelector('[data-later-grid]');
 previous.focus();
 check(document.activeElement===previous,'Previous month has focus in December');
 previous.click();
 const day=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(previous.disabled && day?.dataset.date==='2026-11-30','November has an enabled day');
 check(document.activeElement===day,'Focus moves from Previous month to the enabled day');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_the_same_parking_time_after_it_passes(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 const time=form.querySelector('[data-later-time]'),save=form.querySelector('[value="park"]');
 time.value='1930';time.dispatchEvent(new Event('input',{bubbles:true}));
 save.click();
 const first=requests[0];
 check(first?.get('until')==='2026-12-04T00:30:00.000Z','The first save sends the chosen instant');
 losePost();await flush();
 now=Date.parse('2026-12-05T13:00:00Z');
 toggle.click();
 check(time.value==='19:30' && !save.disabled,'The picker retains and enables the expired retry');
 save.click();
 check(requests.length===2 && requests[1].get('request')===first.get('request') &&
  requests[1].get('until')===first.get('until'),'Save retries the same ID and instant');
 if(requests.length===2){finishPost({parked:'false',parked_until:''});await flush();}
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_from_enter_restores_focus(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 const time=form.querySelector('[data-later-time]');
 show('working');
 time.focus();
 time.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
 check(requests.length===1 && row.hidden,'Enter submits Park and hides the row from Working');
 finishPost({reason:'The fixture refuses Park'});
 await flush();
 for(const animation of document.getAnimations()) animation.finish();
 await flush();
 check(!row.hidden && !toggle.disabled && document.activeElement===toggle,
  'The restored row returns focus to Park until');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_expired_park_retry_keeps_the_calendar_in_the_tab_order(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]'),grid=form.querySelector('[data-later-grid]');
 const save=form.querySelector('[value="park"]');
 toggle.click();
 save.click();
 const first=requests[0];
 losePost();await flush();
 now=Date.parse('2026-12-15T13:00:00Z');
 toggle.click();
 const entry=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(entry?.dataset.date==='2026-12-15','The calendar has an enabled day in the Tab order');
 check(entry===document.activeElement && !save.disabled,'The calendar has focus and Save can retry');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 now=Date.parse('2027-01-02T13:00:00Z');
 toggle.click();
 const nextMonth=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(nextMonth?.dataset.date==='2027-01-02','The calendar opens on an enabled month');
 if(nextMonth){
  nextMonth.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
  check(document.activeElement.dataset.date==='2027-01-03','ArrowRight selects a new date from the focused day');
  save.click();
  check(requests[1]?.get('request')!==first.get('request') && requests[1]?.get('until')?.startsWith('2027-01-03'),
   'The new date sends a new request');
  if(requests.length===2){finishPost({parked:'true'});await flush();}
 }
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_keeps_focus_on_search(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 form.querySelector('[data-later-toggle]').click();
 const time=form.querySelector('[data-later-time]');
 show('working');
 time.focus();
 time.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
 check(requests.length===1 && row.hidden,'Enter submits Park and hides the row from Working');
 finishPost({reason:'The fixture refuses Park'});
 await flush();
 const search=document.getElementById('list-search');
 search.focus();
 check(document.activeElement===search,'Search receives focus while the row returns');
 for(const animation of document.getAnimations()) animation.finish();
 await flush();
 check(!row.hidden && document.activeElement===search,'A refused Park keeps focus on Search');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reserves_a_lost_park_id_and_updates_the_saved_date_label(self) -> None:
        item = self.add("Change the parking date")
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
const requests=[];let finishPost,losePost;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 requests.push(new URLSearchParams(options.body));
 return new Promise((resolve,reject)=>{
  losePost=()=>reject(new Error('Fixture lost response'));
  finishPost=result=>resolve({ok:!result.reason,status:result.reason ? 400 : 200,
   json:async()=>({unread:'true',...result})});
 });
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const row=document.querySelector('[data-list-item]'),form=row.querySelector('form');
 const submit=action=>{
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  return requests.at(-1).get('request');
 };
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const labels=()=>[...row.querySelectorAll('[data-label="State"] small')].map(note=>note.textContent);
 try{
  check(labels().join() === 'Parked until 2026-11-18 13:00 EST','The row starts with one saved time label');
  form.elements.until.value='2026-12-03';
  const park=submit('park');losePost();await flush();
  check(submit('park')===park,'an exact Park retry keeps its request ID');
  losePost();await flush();
  check(submit('remove')!==park,'Remove gets a different ID after a lost Park response');
  finishPost({reason:'Fixture refuses Remove'});await flush();
  form.elements.until.value='2026-12-04';
  check(submit('park')!==park,'a different Park date gets a different ID');
  finishPost({item_state:'Working',parked:'true',parked_until:'2026-12-05'});await flush();
  check(labels().length===1,'a successful Park keeps one date label');
  check(labels().join()==='Parked until 2026-12-05','the label uses the saved date from the response');
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unpark_controls_layout_and_focus(self) -> None:
        parked = self.add("Parked item", number=1)
        _ = self.add("Unparked item", number=2)
        _ = the_list.park(MORTY.id, parked.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence(r"""
 show('all');
 const other=document.querySelector('[data-number="2"]');
 other.querySelector('.list-toggle').click();
 const rectangle=element=>element.getBoundingClientRect();
 const near=(a,b)=>Math.abs(a-b)<1;
 for(const item of [row,other]){
  const pick=selector=>item.querySelector(selector);
  const box=rectangle(pick('textarea'));
  const remove=rectangle(pick('[value=remove]')),park=rectangle(pick('[data-later-toggle]'));
  check(pick('textarea').rows===4 && remove.bottom<box.top,'four lines below the controls');
  check(near(remove.left,box.left) && near(park.right,box.right) && near(remove.top,park.top),'one aligned control row');
  check(pick('[value=unpark]').hidden===(item===other),'Unpark only shows on the parked item');
  check(pick('[data-parked-detail]').hidden===(item===other),'parking line only shows on the parked item');
  if(item===row){
   check(near(park.left-rectangle(pick('[value=unpark]')).right,8),'parking controls are adjacent');
   check(getComputedStyle(pick('[data-parked-note]')).display==='none','the expanded row shows one parking line');
   pick('[data-later-toggle]').click();
   check(near(rectangle(pick('[data-later-menu]')).right,box.right),'picker aligns with the text box');
   pick('[data-later-toggle]').click();
  }
 }
 const unpark=form.querySelector('[value=unpark]');
 show('parked');
 unpark.focus();
 const first=submit('unpark');
 losePost();await flush();
 check(!row.hidden && !unpark.hidden,'a lost response leaves Unpark available');
 check(document.activeElement===unpark,'a lost response returns focus to Unpark');
 check(submit('unpark').id===first.id,'an exact Unpark retry keeps its request ID');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});await flush();
 check(row.hidden && document.activeElement===document.querySelector('#filter-show-parked'),'focus moves to Parked');
 const status=document.querySelector('[data-hidden-action-status][role="status"]');
 check(!status.hidden && status.getClientRects().length>0 && status.textContent==='Item #1 unparked.',
       'Parked announces which item left');
 show('all');
 check(status.hidden,'All shows the confirmation in the row');
 form.elements.words.value='Keep these words.';
 form.elements.until.value='2026-12-03';
 submit('park');
 finishPost({item_state:'Working',parked:'true',parked_until:'2026-12-03'});await flush();
 document.querySelector('[data-sort="3"]').click();
 unpark.focus();
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});await flush();
 check(!row.hidden && !row.querySelector('[data-detail]').hidden,'Unpark in All keeps the row open');
 check(document.querySelector('[data-list-item]')===row,'Unpark preserves the item order by title');
 check(document.activeElement===row.querySelector('.list-toggle'),'focus moves to the item number');
 check(unpark.hidden && form.querySelector('[data-parked-detail]').hidden,'the parking controls update');
 check(form.elements.until.value==='' && form.elements.words.value==='Keep these words.',
       'Unpark clears the parking draft and keeps the words');
 show('working');
 check(!row.hidden && !row.querySelector('[data-detail]').hidden,'Working shows the expanded unparked row');
""", reduced_motion=True, add_item=False, window_size=(1440, 1120))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_expired_parking_and_keeps_the_parking_draft(self) -> None:
        item = self.add("Parking expires before Unpark", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 form.elements.until.value='2026-12-03';
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'expired'});
 await flush();
 const status=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && !status.hidden && status.textContent==='Item #1: Parking time already expired.',
       'Parked reports that the parking time already expired.');
 show('all');
 check(form.querySelector('[data-send-status]').textContent==='Parking time already expired',
       'All reports that the parking time already expired.');
 check(form.elements.until.value==='2026-12-03','An expired time leaves the parking draft intact.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_unpark_replay_with_newer_parking(self) -> None:
        item = self.add("Reparked before the retry", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 const unpark=form.querySelector('[value=unpark]');
 form.elements.until.value='2026-12-03';
 unpark.focus();
 const first=submit('unpark');
 losePost();await flush();
 check(submit('unpark').id===first.id,'The retry sends the original request ID.');
 finishPost({item_state:'Working',parked:'true',parked_until:'2026-12-04',
  unpark_outcome:'cleared_newer_parking_active'});
 await flush();
 const message=form.querySelector('[data-send-status]').textContent;
 check(message==='The original Unpark cleared parking. A newer parking time is active',
       'The replay reports the original result and the newer parking time.');
 check(!row.hidden && !unpark.hidden && document.activeElement===unpark,
       'The reparked row stays in view and Unpark keeps focus.');
 check(form.querySelector('[data-parked-detail]').textContent==='Parked until 2026-12-04',
       'The row shows the newer parking time.');
 check(form.elements.until.value==='2026-12-03','The replay preserves the parking draft.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_unpark_with_no_parking_time(self) -> None:
        item = self.add("Parking cleared by another tab", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 form.elements.until.value='2026-12-03';
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'no_active_parking'});
 await flush();
 const status=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && !status.hidden && status.textContent==='Item #1: No active parking time to clear.',
       'The result shows that there was no active parking time.');
 check(form.elements.until.value==='2026-12-03','The request preserves the parking draft.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_preserves_both_overlapping_unpark_confirmations(self) -> None:
        for number in (1, 2):
            item = self.add(f"Parked item {number}", number=number)
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 const second=document.querySelector('[data-number="2"]');
 second.querySelector('.list-toggle').click();
 submit('unpark');
 const finishFirst=finishPost;
 submit('unpark',second.querySelector('form'));
 const result={item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'};
 const status=document.querySelector('[data-hidden-action-status]');
 finishFirst(result);await flush();
 check(row.hidden && !second.hidden && !status.hidden && status.textContent==='Item #1 unparked.',
       'The first request shows its result while the second request runs.');
 finishPost(result);await flush();
 check(second.hidden && !status.hidden && status.textContent==='Item #1 unparked. Item #2 unparked.',
       'Both results stay visible after both rows leave Parked.');
 show('all');
 check(status.hidden,'All shows the results in their expanded rows.');
 show('parked');
 check(!status.hidden && status.textContent==='Item #1 unparked. Item #2 unparked.',
       'Parked shows both results again.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_brings_parked_filter_and_confirmation_into_view(self) -> None:
        for number in range(1, 25):
            item = self.add(f"Parked item {number}", number=number)
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        steps = '''
 show('parked');
 const sort=document.querySelector('[data-sort="0"]');
 sort.click();sort.click();
 const filter=document.querySelector('#filter-show-parked');
 if(SHORT_VIEWPORT){
  const buttons=filter.closest('[data-filter-row="show"]').querySelectorAll('button');
  check(innerHeight<=260 && new Set([...buttons].map(button=>button.offsetTop)).size>1,
        'The Show filter wraps in the short viewport.');
 }
 const unpark=form.querySelector('[value=unpark]');
 unpark.scrollIntoView({block:'center',behavior:'instant'});
 unpark.focus({preventScroll:true});
 check(filter.getBoundingClientRect().bottom<0,'The focused row is below the viewport with the filters.');
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});
 await flush();
 const withinViewport=element=>{
  const {top,bottom}=element.getBoundingClientRect();
  return !element.hidden && top>=0 && bottom<=innerHeight;
 };
 const status=document.querySelector('[data-hidden-action-status]');
 const bounds=filter.getBoundingClientRect();
 const hit=document.elementFromPoint(bounds.left+bounds.width/2,bounds.top+bounds.height/2);
 const screen=JSON.stringify({innerWidth,innerHeight,scrollY,
  focus:document.activeElement.id,top:bounds.top,statusBottom:status.getBoundingClientRect().bottom,
  hit:hit?.outerHTML.slice(0,150),pageStatus:document.querySelector('[data-page-status]').getBoundingClientRect().height});
 check(row.hidden && document.activeElement===filter && withinViewport(filter) && filter.contains(hit),
       'Unpark moves focus and brings the Parked filter into view. '+screen);
 check(withinViewport(status) && status.textContent==='Item #1 unparked.',
       'Unpark brings the confirmation into the same viewport. '+screen);
'''
        for window_size in ((1440, 900), (500, 260)):
            with self.subTest(window_size=window_size):
                viewport_steps = steps.replace("SHORT_VIEWPORT", "true" if window_size[1] <= 260 else "false")
                self.browser_action_sequence(viewport_steps, reduced_motion=True, add_item=False, window_size=window_size)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_hides_unpark_for_closed_items_and_updates_the_state_sort_key(self) -> None:
        active = self.add("Active parked item", number=1)
        closed = self.add("Closed parked item", number=2)
        for item in (active, closed):
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        _ = the_list.close(MORTY.id, closed.id, "Removed from the List.", source="nate")
        self.browser_action_sequence('''
 show('all');
 const closed=document.querySelector('[data-number="2"]');
 closed.querySelector('.list-toggle').click();
 check(closed.dataset.parked==='true' && closed.querySelector('[value=unpark]').hidden,
       'A rendered Closed row can have a parking time and hides Unpark.');
 const unpark=form.querySelector('[value=unpark]');
 submit('remove');
 check(unpark.hidden,'Remove hides Unpark immediately.');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 check(!unpark.hidden,'A refusal restores Unpark.');
 submit('remove');
 finishPost({item_state:'Closed',parked:'true',unread:'false'});await flush();
 check(unpark.hidden && row.dataset.parked==='true','A confirmed Remove leaves Unpark hidden.');
 const sortValues=JSON.parse(row.dataset.sortValues);
 check(sortValues[3]==='active parked item' && sortValues[4]==='closed',
       'Remove keeps the Item key and updates the State key.');
''', add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_preserves_unpark_focus_during_periodic_refresh(self) -> None:
        item = self.add("Parked item", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "The refreshed status."}, source="morty")
            fresh = list_page.render(self.root / "reviews").decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="1"', markup, count=1)
        setup = r'''<script>
window.fetch=async()=>new Response(FRESH);
</script>'''.replace('FRESH', json.dumps(fresh).replace('</', '<\\/'))
        probe = r'''<script>
const original=document.querySelector('[data-list]');
original.querySelector('#filter-show-parked').click();
original.querySelector('.list-toggle').click();
const button=original.querySelector('[value=unpark]');
button.focus();
const focusedBefore=document.activeElement===button;
const observer=new MutationObserver(()=>{
 if(original.isConnected) return;
 observer.disconnect();
 const fresh=document.querySelector('[data-list]');
 const unpark=fresh.querySelector('[value=unpark]');
 const failures=[];
 if(!fresh.textContent.includes('The refreshed status.')) failures.push('The page renders the refreshed status.');
 if(!focusedBefore || document.activeElement!==unpark || !button.id || unpark.id!==button.id)
  failures.push('The refresh restores focus to Unpark with the same ID.');
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
});
observer.observe(original.parentNode,{childList:true});
</script>'''
        self.browser_actions(markup, setup, probe, virtual_time_ms=2000)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_a_lost_tell_after_a_refused_park(self) -> None:
        self.browser_action_sequence('''
 form.elements.words.value='Tell morty once.';
 const tell=submit('tell');losePost();await flush();
 form.elements.until.value='2026-12-03';
 const park=submit('park');
 check(park.id!==tell.id,'Park uses its own request ID');
 finishPost({reason:'Fixture refuses Park'});await flush();
 check(submit('park').id!==park.id,'a refused Park leaves no unconfirmed request');
 finishPost({reason:'Fixture refuses Park again'});await flush();
 form.elements.until.value='';
 const retry=submit('tell');
 check(retry.id===tell.id && retry.token===tell.token,'Tell retains its signed ID across refusals');
 finishPost({item_state:'Working',parked:'false'});await flush();
 form.elements.words.value='Tell morty once.';
 check(submit('tell').id!==tell.id,'a confirmed Tell leaves no unconfirmed request');
 finishPost({item_state:'Working',parked:'false'});await flush();
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_two_lost_park_dates_after_a_confirmed_remove(self) -> None:
        self.browser_action_sequence('''
 form.elements.until.value='2026-12-03';
 const first=submit('park');losePost();await flush();
 form.elements.until.value='2026-12-04';
 const second=submit('park');losePost();await flush();
 check(first.id!==second.id,'each Park date gets its own request ID');
 const remove=submit('remove');
 check(remove.id!==second.id,'Remove uses its own request ID');
 finishPost({item_state:'Closed',parked:'true',unread:'false'});await flush();
 for(const [date,original] of [['2026-12-03',first],['2026-12-04',second]]){
  form.elements.until.value=date;
  const retry=submit('park');
  check(retry.id===original.id && retry.token===original.token,'Park retains the signed ID for '+date);
  finishPost({item_state:'Closed',parked:'true',unread:'false'});await flush();
 }
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_a_lost_remove_notice_when_its_details_are_closed(self) -> None:
        self.browser_action_sequence('''
 show('all');
 submit('remove');
 check(!row.hidden,'the row stays visible in All during Remove');
 row.querySelector('.list-row').click();
 check(row.querySelector('[data-detail]').hidden,'the user can close the details while waiting');
 losePost();await flush();
 check(row.dataset.unread==='false' && !row.querySelector('.list-dot'),'a lost Remove keeps the row read');
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(!hiddenStatus.hidden && hiddenStatus.textContent==='Item 1: Save not confirmed: Fixture lost response',
  'the table shows the lost response while the details are closed');
 row.querySelector('.list-toggle').click();
 check(hiddenStatus.hidden && form.querySelector('[data-send-status]').getClientRects().length>0,
  'opening the details shows the inline notice only');
 row.querySelector('.list-toggle').click();
 check(!hiddenStatus.hidden,'closing the details restores the notice above the table');
 submit('remove');finishPost({reason:'Fixture refuses Remove'});await flush();
 check(row.dataset.state==='Working' && row.dataset.unread==='true' && row.querySelector('.list-dot'),
  'a refused Remove retry restores the last saved unread state');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_bulk_controls_move_a_lost_remove_notice(self) -> None:
        self.browser_action_sequence('''
 show('all');
 submit('remove');
 losePost();await flush();
 const above=document.querySelector('[data-hidden-action-status]');
 const inline=form.querySelector('[data-send-status]');
 const message='Save not confirmed: Fixture lost response';
 const inlineOnly=()=>above.hidden && inline.textContent===message && inline.getClientRects().length>0;
 const aboveOnly=()=>!above.hidden && above.textContent==='Item 1: '+message && !inline.getClientRects().length;
 check(inlineOnly(),'The open row shows the warning only in its details.');
 document.querySelector('[aria-label="Collapse all"]').click();
 check(aboveOnly(),'Collapse all moves the warning above the table.');
 const toggle=row.querySelector('.list-toggle');
 toggle.click();toggle.click();
 check(aboveOnly(),'Closing the row also moves the warning above the table.');
 document.querySelector('[aria-label="Expand all"]').click();
 check(inlineOnly(),'Expand all shows the warning only in the row details.');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_only_the_latest_hidden_remove_notice(self) -> None:
        _ = self.add("Second List item", number=2)
        self.browser_action_sequence('''
 show('all');
 const second=document.querySelector('[data-number="2"]');
 second.querySelector('.list-toggle').click();
 for(const item of [row,second]){
  submit('remove',item.querySelector('form'));
  item.querySelector('.list-row').click();
  losePost();await flush();
 }
 const status=document.querySelector('[data-hidden-action-status]');
 const message=number=>'Item '+number+': Save not confirmed: Fixture lost response';
 const shows=number=>!status.hidden && status.textContent.includes(message(number));
 check(!shows(1) && shows(2),'only the latest hidden notice appears above the table');
 row.querySelector('.list-toggle').click();
 check(!shows(1) && shows(2),'opening row one preserves the latest notice');
 check(form.querySelector('[data-send-status]').textContent==='Save not confirmed: Fixture lost response',
  'row one shows its notice inline');
 second.querySelector('.list-toggle').click();
 check(status.hidden,'opening the latest failed row hides the notice above the table');
 row.querySelector('.list-toggle').click();
 check(status.hidden,'closing row one does not restore the older notice');
''')

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_clears_the_list_save_notice(self) -> None:
        for number in range(1, 9):
            _ = self.add(f"List item {number}", number=number)
        with patch.object(list_page, "counts_strip", return_value=""):
            page = list_page.render(self.root / "reviews")
        self.assertEqual(save_notice_failures(page, 'remove'), [[]])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_the_question_request_after_a_service_failure(self) -> None:
        self.browser_action_sequence('''
 form.elements.words.value='Which check passed?';
 form.elements.words.dispatchEvent(new Event('input'));
 const attempt=submit('question');
 finishPost({status:503,reason:'Fixture delivery status unavailable'});await flush();
 check(form.querySelector('[data-send-status]').textContent===
  'Save not confirmed: Fixture delivery status unavailable','a service error leaves the save unconfirmed');
 const retry=submit('question');
 check(retry.id===attempt.id && retry.token===attempt.token,'the retry uses the original signature');
 finishPost({item_state:'Working',parked:'false'});await flush();
 check(form.elements.words.value==='','the confirmed reply clears the question');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_ask_uses_its_own_request_and_keeps_the_exact_retry(self) -> None:
        self.browser_action_sequence('''
 const words=form.elements.words,ask=form.querySelector('[value="question"]');
 check(ask?.disabled,'Ask question starts disabled');
 words.value=' ';words.dispatchEvent(new Event('input'));
 check(ask?.disabled,'spaces keep Ask question disabled');
 words.value='What changed?';words.dispatchEvent(new Event('input'));
 check(!ask?.disabled,'words enable Ask question');
 const question=submit('question');losePost();await flush();
 const feedback=submit('tell');losePost();await flush();
 check(question.id!==feedback.id,'Ask and Tell use different request IDs');
 const retry=submit('question');
 check(retry.id===question.id && retry.token===question.token,'an exact Ask retry keeps its signature');
 finishPost({item_state:'Working',parked:'false'});await flush();
 check(words.value==='' && ask.disabled,'a saved question clears the box');
''')

    def test_ask_marks_the_seen_update_read_after_it_saves(self) -> None:
        item = self.add("Read the update before asking")
        root = self.root / "reviews"
        fields = self.fields(item, "question", words="Which check passed?")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            for _ in range(2):
                _ = list_page.submit(root, urlencode(fields))
        mark.assert_called_once_with(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=the_list.unread_update(item))
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.unread_since, the_list.NoTimestamp.UNSET)
        self.assertIsInstance(saved.log[-2].what, the_list.QuestionRequestReceipt)
        self.assertEqual(saved.log[-1].what, the_list.MarkedRead())

    def test_words_actions_keep_updates_that_arrive_after_render_unread(self) -> None:
        root = self.root / "reviews"
        for action in ("question", "tell"):
            with self.subTest(action=action):
                item = self.add("Keep the update that arrives after render")
                fields = self.fields(item, action, words="Which check passed?")
                newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
                _ = list_page.submit(root, urlencode(fields))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(the_list.unread_update(saved), the_list.unread_update(newer))
                self.assertFalse(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))
                _ = list_page.submit(root, urlencode(self.fields(saved, action, words="Which check is next?")))
                self.assertIs(the_list.load(MORTY.id, item.id).unread_since, the_list.NoTimestamp.UNSET)

    def test_saved_question_and_answer_appear_on_the_item_and_the_answer_is_unread(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Inspect the List", number=106)
        fields = self.fields(item, "question", words="What changed in <List>?")
        asked_at = NOW - timedelta(days=1)
        answered_at = NOW - timedelta(minutes=25)
        asked_time = f'<time datetime="{asked_at.isoformat()}">2026-11-15 13:00 EST</time>'
        answer_time = f'<time datetime="{answered_at.isoformat()}">12:35 EST</time>'
        question_clock = Mock(wraps=datetime)
        question_clock.configure_mock(**{"now.side_effect": (asked_at, answered_at)})
        with patch.object(page_question, "datetime", question_clock):
            delivery = list_page.submit(root, urlencode(fields), status)
            opened = list_page.render(root, status=status).decode().split('<script>', 1)[0]
            self.assertIn('data-waiting-for-answer="true"', opened)
            self.assertIn('<p data-waiting-note>The producer will answer.</p>', opened)
            self.assertIn('Question Q1 — waiting for an answer</h3>' + asked_time, opened)
            self.assertIn("What changed in &lt;List&gt;?", opened)
            self.assertNotIn('value="question"', opened)
            self.assertNotIn('aria-label="Unread update"', opened)
            with patch.object(change_page, "send_question", return_value=question_outcome()):
                list_page.deliver(delivery)
            with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": MORTY.current_session}),
                  patch.object(the_list, "now", return_value=answered_at), redirect_stdout(io.StringIO())):
                self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The <filter> changed."]), 0)
        _ = list_page.submit(root, urlencode(fields))
        answered = list_page.render(root).decode().split('<script>', 1)[0]
        self.assertIn('data-waiting-for-answer="false"', answered)
        self.assertIn('value="question"', answered)
        self.assertIn('aria-label="Unread update"', answered)
        self.assertIn('value="read">Mark read</button>', answered)
        panel, log = answered.split('<ol class="list-log">', 1)
        self.assertIn("Marked read <small>", log)
        self.assertNotIn("Answered question Q1 <small>", log)
        self.assertNotIn('What changed in &lt;List&gt;?', log)
        self.assertNotIn('The &lt;filter&gt; changed.', log)
        self.assertIn('Question Q1</h3>' + asked_time + '<p>What changed in &lt;List&gt;?</p>', panel)
        self.assertIn('<h3>Answer <span class="question-unread">Unread</span></h3>'
                      + answer_time + '<p>The &lt;filter&gt; changed.</p>', panel)

        current = the_list.load(MORTY.id, item.id)
        _ = list_page.submit(root, urlencode(self.fields(current, "read")))
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer update arrived."}, source="morty")
        read = list_page.render(root).decode().split('<script>', 1)[0]
        self.assertIn('aria-label="Unread update"', read)
        panel, log = read.split('<ol class="list-log">', 1)
        self.assertNotIn('data-question="Q1"', panel)
        self.assertNotIn('class="question-unread"', panel)
        log = re.sub(r'<span class="staff-name"[^>]*>(.*?)</span>', r'\1', log)
        question_row = '<li data-log-index="Q1-asked">' + asked_time + '<div>Question Q1 <small>— nate</small>'
        answer_row = '<li data-log-index="Q1-answered">' + answer_time + '<div>Answer <small>— morty</small>'
        self.assertIn(question_row + '<p>What changed in &lt;List&gt;?</p></div></li>', log)
        self.assertIn(answer_row + '<p>The &lt;filter&gt; changed.</p></div></li>', log)
        self.assertLess(log.index(answer_row), log.index(question_row))

    def test_stale_mark_read_keeps_the_newer_answer_in_the_panel(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Read the answer shown by the form")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        already_read = self.fields(the_list.load(MORTY.id, item.id), "read")
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            first = self.fields(the_list.load(MORTY.id, item.id), "question", words="Which check passed?")
            list_page.deliver(list_page.submit(root, urlencode(first), status))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The first check passed."]), 0)
            current = the_list.load(MORTY.id, item.id)
            stale = self.fields(current, "read")
            second = self.fields(item, "question", words="Did the next check pass?")
            list_page.deliver(list_page.submit(root, urlencode(second), status))
            waiting = list_page.saved_action_state(MORTY.id, item.id, status=status).get("waiting_details_html", "")
            self.assertIn('data-question="Q1"', waiting)
            credential = re.search(r'data-open-read-request="([^"]+)" data-open-read-token="([^"]+)"', waiting)
            assert credential is not None
            request, token = credential.groups()
            _ = list_page.submit(root, urlencode({**stale, "request": request, "token": token}), status)
            self.assertIn('data-log-index="Q1-answered"',
                          list_page.saved_action_state(MORTY.id, item.id, status=status).get("waiting_details_html", ""))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q2", "--text", "The next check passed."]), 0)

        current = the_list.load(MORTY.id, item.id)
        for fields in (stale, already_read):
            with self.subTest(request=fields["request"]):
                _ = list_page.submit(root, urlencode(fields), status)
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(the_list.unread_update(saved), the_list.unread_update(current))
                markup = list_page.render(root, include_counts=False, status=status).decode().split('<script>', 1)[0]
                panel, log = markup.split('<ol class="list-log">', 1)
                self.assertIn('data-question="Q2"', panel)
                self.assertIn('class="question-unread">Unread</span>', panel)
                self.assertIn('The next check passed.', panel)
                self.assertIn('data-log-index="Q1-answered"', log)
                self.assertNotIn('data-log-index="Q2-answered"', log)

    def test_answer_returns_a_parked_item_with_its_current_parking(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        until = NOW + timedelta(days=7)
        for unpark in (False, True):
            with self.subTest(unpark=unpark):
                item = self.add("Check the parked item")
                parked = the_list.park(MORTY.id, item.id, until, source="morty")
                earlier_unpark = self.fields(parked, "unpark")
                with patch.object(change_page, "send_question", return_value=question_outcome()):
                    list_page.deliver(list_page.submit(root, urlencode(
                        self.fields(parked, "question", words="Which check passed?")), status))
                waiting = list_page.saved_action_state(MORTY.id, item.id, status=status)
                self.assertEqual((waiting["waiting_for_answer"], waiting["parked"]), ("true", "true"))
                self.assertEqual(the_list.load(MORTY.id, item.id).parked_until, until)

                if unpark:
                    submission = list_page.submit(root, urlencode(earlier_unpark), status)
                    self.assertIsInstance(submission, list_page.ListUnparkSubmission)
                    assert isinstance(submission, list_page.ListUnparkSubmission)
                    current = list_page.saved_action_state(MORTY.id, item.id, status=status)
                    self.assertIs(submission.response_outcome(current), list_page.UnparkOutcome.CLEARED)
                    self.assertEqual((current["waiting_for_answer"], current["parked"]), ("true", "false"))

                with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1",
                                                    "--text", "The gate passed."]), 0)
                answered = list_page.saved_action_state(MORTY.id, item.id, status=status)
                self.assertEqual(answered["waiting_for_answer"], "false")
                self.assertEqual(answered["unread"], "true")
                self.assertEqual(answered["parked"], "false" if unpark else "true")

    def test_another_question_keeps_the_failed_questions_resend_available(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the first question available")

        def ask(words: str) -> list_page.ListQuestionDelivery:
            current = the_list.load(MORTY.id, item.id)
            delivery = list_page.submit(root, urlencode(self.fields(current, "question", words=words)), status)
            assert isinstance(delivery, list_page.ListQuestionDelivery)
            return delivery

        first = ask("Which check passed?")
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "Producer offline")),
              patch.object(change_page, "alert_delivery_failures")):
            list_page.deliver(first)
        second = ask("Which check is next?")
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            for pending in (True, False):
                with self.subTest(second_pending=pending):
                    if not pending:
                        list_page.deliver(second)
                        send.assert_called_once()
                    self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "false")
                    markup = list_page.render(root, include_counts=False, status=status).decode().split('<script>', 1)[0]
                    self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
                    self.assertIn('value="resend-Q1">Resend question Q1</button>', markup)
                    self.assertNotIn('value="resend-Q2"', markup)
            current = the_list.load(MORTY.id, item.id)
            retry = list_page.submit(root, urlencode(self.fields(current, "resend-Q1")), status)
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "true")
            list_page.deliver(retry)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(the_list.load(MORTY.id, item.id).questions, current.questions)

    def test_recorded_question_failure_takes_priority_when_the_intent_clear_fails(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the failed question available")
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check passed?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        _ = delivery.job.release()
        failure = change_page.FailedDelivery(delivery.attempt, delivery.page.review_list, delivery.row, "Producer offline")
        saved: change_page.StoredReviewMessage = {"saved": True, "source": str(delivery.page.review_list),
            "owner": MORTY.id, "message": delivery.message, "row": delivery.row}
        intent = change_page.DeliveryIntent(delivery.attempt, "verdict", recipients=(MORTY.id,))
        page = change_page.ReviewMessagePage(delivery.page.review_list, delivery.page.production,
                                             delivery.page.rows, "", saved)
        with status.locked():
            status.write_slots(page, delivery.row, change_page.DeliverySlots(change_page.NoDelivery(), intent))
            status.record_result_locked(delivery, failure)
        reopened = change_page.DeliveryStatus(root)
        self.assertEqual(reopened.slots(page, delivery.row).verdict, intent)
        self.assertEqual(reopened.failed_delivery(page, delivery.row), failure)
        markup = list_page.render(root, include_counts=False).decode().split('<script>', 1)[0]
        self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
        self.assertIn('Question Q1 did not reach morty', markup)
        self.assertIn('value="resend-Q1">Resend question Q1</button>', markup)

        current = the_list.load(MORTY.id, item.id)
        retry = list_page.submit(root, urlencode(self.fields(current, "resend-Q1")), reopened)
        assert isinstance(retry, list_page.ListQuestionDelivery)
        assert isinstance(retry.delivery, change_page.MessageDelivery)
        self.assertNotEqual(retry.delivery.attempt, failure.attempt)
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=reopened)["waiting_for_answer"], "true")
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            list_page.deliver(retry)
        send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), current)

    def test_question_result_write_failure_leaves_the_saved_question_available_for_resend(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        first_jobs: dict[str, the_list.ListItem] = {}
        for number in (1, 2):
            item = self.add(f"Recovery question {number}", number=number)
            asked = list_page.submit(root, urlencode(self.fields(item, "question", words=f"Which check {number}?")), status)
            assert isinstance(asked, list_page.ListQuestionDelivery)
            delivery = asked.delivery
            assert isinstance(delivery, change_page.PageQuestionSend)
            assert isinstance(delivery.job, change_page.QuestionJob)
            self.assertEqual(item.id, delivery.job.record.source.item_id)
            first_jobs[delivery.job.record.message_id] = item
            _ = delivery.job.release()
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        recovering = change_page.DeliveryStatus(root)
        transport = FakeQuestionTransport()
        publish = change_page.publish_question_attempt
        lost: list[str] = []

        def fail_first_result(path: Path, record: delivery_state.QuestionAttempt) -> None:
            if record.state is delivery_state.QuestionSendState.SENT and not lost:
                lost.append(record.message_id)
                raise OSError("Cannot save the result")
            publish(path, record)

        def send(recipient: staff.StaffId, message: str, message_id: str
                 ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            if lost:
                first = first_jobs[lost[0]]
                self.assertEqual(list_page.saved_action_state(MORTY.id, first.id, status=recovering)["waiting_for_answer"], "false")
                second = first_jobs[message_id]
                self.assertEqual(list_page.saved_action_state(MORTY.id, second.id, status=recovering)["waiting_for_answer"], "true")
                markup = list_page.render(root, include_counts=False, status=recovering).decode().split('<script>', 1)[0]
                self.assertIn('Question Q1 may not have reached morty', markup)
                self.assertEqual(len(list_page.waiting_updates(console.NewsRequest(root))), 2)
            return transport.send(recipient, message, message_id)

        with (patch.object(change_page, "publish_question_attempt", side_effect=fail_first_result),
              patch.object(change_page, "send_question", side_effect=send)):
            change_page.recover_review_messages(recovering)
            change_page.recover_review_messages(change_page.DeliveryStatus(root))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(transport.receipts), 2)

    def test_accepted_question_has_no_resend_after_job_cleanup_fails(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("The accepted question")
        fields = self.fields(item, "question", words="Which check passed?")
        delivery = list_page.submit(root, urlencode(fields), status)
        transport = FakeQuestionTransport()

        with patch.object(change_page, "send_question", side_effect=transport.send):
            with (patch.object(status.question_jobs, "discard", side_effect=OSError("Cannot clear the job.")),
                  self.assertRaisesRegex(OSError, "Cannot clear the job.")):
                list_page.deliver(delivery)
            restarted = change_page.DeliveryStatus(root)
            change_page.recover_review_messages(restarted)
            list_page.deliver(list_page.submit(root, urlencode(fields), restarted))
            current = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(self.fields(current, "resend-Q1")), restarted))
        markup = list_page.render(root, include_counts=False).decode().split('<script>', 1)[0]
        self.assertIn('data-waiting-for-answer="true"', markup)
        self.assertNotIn('value="resend-Q1"', markup)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_resend_repairs_a_mismatch_after_the_previous_sender_finishes(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Repair the delivery")
        words = "Which check passed?"
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words=words)), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        path = status.path(delivery.page, delivery.row)
        saved = delivery.job.record
        damaged = replace(saved, source=replace(saved.source, words="A different question."), recipient_id=OTHER.id,
                          message="A different message.", resend_requests=("earlier-request",))
        with status.locked():
            change_page.advance_question_attempt(path, saved, damaged)
        damaged = change_page.read_question_attempt(path)
        current = the_list.load(MORTY.id, item.id)
        fields = urlencode(self.fields(current, "resend-Q1", words="Ignore these words."))
        active = "Resend is unavailable while the previous sender is active."
        body = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
        self.assertIn('data-waiting-for-answer="false"', body)
        self.assertIn(active, body)
        self.assertNotIn('value="resend-Q1"', body)
        with patch.object(change_page, "send_question") as send:
            with self.assertRaisesRegex(question_record.AnswerRefused, re.escape(active)):
                list_page.deliver(list_page.submit(root, fields, status))
            send.assert_not_called()
        self.assertEqual(change_page.read_question_attempt(path), damaged)

        receipt = delivery_state.QuestionSendReceipt("sent", "Receipt for the wrong question.")
        with status.locked():
            change_page.advance_question_attempt(path, damaged, replace(
                damaged, state=delivery_state.QuestionSendState.SENT,
                owner=delivery_state.NoQuestionSendOwner(), receipt=receipt))
        _ = delivery.job.release()
        previous = change_page.read_question_attempt(path)
        body = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
        self.assertIn('data-waiting-for-answer="false"', body)
        self.assertIn('value="resend-Q1">Resend question Q1</button>', body)
        message = (f'List item {item.number}: Ask question\n{item.title}\n{words}\n'
                   + 'Answer with this command: ~/.claude/scripts/lib/py ~/.claude/scripts/production/the_list.py '
                   + f'answer {item.id} --question Q1 --text "<answer>"')
        outcome = (send_result.UnconfirmedSendOutcome(), "No reply from transport.")
        with patch.object(change_page, "send_question", return_value=outcome) as send:
            for _ in range(2):
                list_page.deliver(list_page.submit(root, fields, status))
            send.assert_called_once_with(MORTY.id, message, saved.message_id)
        repaired = change_page.read_question_attempt(path)
        self.assertEqual(repaired.source, saved.source)
        self.assertEqual(repaired.recipient_id, MORTY.id)
        self.assertEqual(repaired.message, message)
        self.assertNotEqual(repaired.attempt_id, previous.attempt_id)
        self.assertGreater(repaired.revision, previous.revision)
        self.assertIsInstance(repaired.owner, delivery_state.NoQuestionSendOwner)
        self.assertIsInstance(repaired.receipt, delivery_state.NoQuestionSendReceipt)
        self.assertTrue(repaired.earlier_unconfirmed)
        self.assertEqual(repaired.resend_requests, ("earlier-request", parse_qs(fields)["request"][0]))
        self.assertEqual(repaired.state, delivery_state.QuestionSendState.UNKNOWN)
        with status.locked(), self.assertRaisesRegex(ValueError, "stale"):
            change_page.advance_question_attempt(path, damaged, previous)
        self.assertEqual(change_page.read_question_attempt(path), repaired)
        self.assertEqual(the_list.load(MORTY.id, item.id), current)
        with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              redirect_stdout(io.StringIO())):
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The check passed."]), 0)
        answer = the_list.load(MORTY.id, item.id).questions[0].answer
        assert answer is not None
        self.assertEqual(answer.text, "The check passed.")

    def test_resend_uses_a_released_lock_when_the_previous_process_is_unreadable(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Check the previous sender")
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check finished?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        claimed = delivery.job.record
        assert isinstance(claimed.owner, delivery_state.QuestionSendOwner)
        previous_owner = replace(claimed.owner, pid=claimed.owner.pid + 1)
        path = status.path(delivery.page, delivery.row)
        with status.locked():
            change_page.advance_question_attempt(path, claimed, replace(
                claimed, owner=previous_owner, state=delivery_state.QuestionSendState.SENDING))
        fields = urlencode(self.fields(the_list.load(MORTY.id, item.id), "resend-Q1"))
        process_identity = process_start.process_start

        def read_identity(pid: int) -> process_start.ProcessStart:
            if pid == previous_owner.pid:
                return process_start.Unreadable("Cannot read the previous sender.")
            return process_identity(pid)

        blocked = "Resend is unavailable until the sender can be checked."
        with (patch.object(process_start, "process_start", side_effect=read_identity) as identity,
              patch.object(change_page, "send_question", return_value=question_outcome()) as send):
            markup = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
            self.assertIn('data-waiting-for-answer="false"', markup)
            self.assertIn(blocked, markup)
            self.assertNotIn('value="resend-Q1"', markup)
            with self.assertRaisesRegex(question_record.AnswerRefused, re.escape(blocked)):
                list_page.deliver(list_page.submit(root, fields, status))
            send.assert_not_called()
            _ = delivery.job.release()
            self.assertTrue(Path(cast(str, delivery.job.lock.name)).is_file())
            identity.reset_mock()
            markup = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
            self.assertNotIn(blocked, markup)
            self.assertIn('value="resend-Q1">Resend question Q1</button>', markup)
            identity.assert_not_called()
            list_page.deliver(list_page.submit(root, fields, status))
            list_page.deliver(list_page.submit(root, fields, status))
            send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)

    def test_a_title_edit_preserves_ready_sent_and_queued_questions(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        state = delivery_state.QuestionSendState
        for number, previous_state in enumerate((state.READY, state.SENT, state.QUEUED), 1):
            with self.subTest(state=previous_state):
                item = self.add("Original title", number=number)
                words = "Which check passed?\nWhat did you find?"
                asked = list_page.submit(root, urlencode(self.fields(item, "question", words=words)), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                claimed = delivery.job.record
                path = status.path(delivery.page, delivery.row)
                if previous_state is state.READY:
                    with status.locked():
                        change_page.advance_question_attempt(path, claimed, replace(
                            claimed, state=state.READY, owner=delivery_state.NoQuestionSendOwner()))
                    _ = delivery.job.release()
                else:
                    outcome = "queued" if previous_state is state.QUEUED else "sent"
                    with patch.object(change_page, "send_question", return_value=question_outcome(outcome)):
                        list_page.deliver(asked)

                edited = the_list.update(MORTY.id, item.id, {"title": "New title"}, source="morty")
                target = change_page.list_question_target(Path(claimed.source.path), edited.id, edited.number,
                                                          edited.title, MORTY.id)
                with status.locked(), self.subTest(check="state after edit"):
                    view = change_page.question_delivery_view(status, target.saved(edited.questions[0]))
                    self.assertEqual(view.state, previous_state)

                with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                    change_page.recover_review_messages(status)
                    if previous_state is state.READY:
                        send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
                    else:
                        send.assert_not_called()
                final_state = state.SENT if previous_state is state.READY else previous_state
                self.assertEqual(change_page.read_question_attempt(path).state, final_state)
                self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "true")
                body = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
                self.assertNotIn('value="resend-Q1"', body)

    def test_recovery_and_list_reject_a_changed_question_message(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        state = delivery_state.QuestionSendState
        cases = ((state.READY, False), (state.SENT, False), (state.QUEUED, False), (state.READY, True))
        for number, (previous_state, changed_command) in enumerate(cases, 1):
            with self.subTest(state=previous_state, changed_command=changed_command):
                item = self.add("Check the saved question", number=number)
                words = "Which check finished?"
                asked = list_page.submit(root, urlencode(self.fields(item, "question", words=words)), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                claimed = delivery.job.record
                path = status.path(delivery.page, delivery.row)
                receipt: delivery_state.NoQuestionSendReceipt | delivery_state.QuestionSendReceipt
                if previous_state is state.READY:
                    receipt = delivery_state.NoQuestionSendReceipt()
                else:
                    receipt = delivery_state.QuestionSendReceipt("queued" if previous_state is state.QUEUED else "sent", "")
                wrong_message = (claimed.message.replace("--question Q1", "--question Q99") if changed_command
                                 else claimed.message.replace(words, "A different question."))
                with status.locked():
                    change_page.advance_question_attempt(path, claimed, replace(
                        claimed, state=previous_state, message=wrong_message,
                        owner=delivery_state.NoQuestionSendOwner(), receipt=receipt))
                _ = delivery.job.release()

                with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                    change_page.recover_review_messages(status)
                    send.assert_not_called()
                    self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "false")
                    body = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
                    self.assertIn('value="resend-Q1">Resend question Q1</button>', body)
                    current = the_list.load(MORTY.id, item.id)
                    fields = urlencode(self.fields(current, "resend-Q1"))
                    for _ in range(2):
                        list_page.deliver(list_page.submit(root, fields, status))
                    send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
                self.assertEqual(the_list.load(MORTY.id, item.id), current)

    def test_a_malformed_job_id_keeps_sender_exclusion_until_the_lock_releases(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the sender lock")
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check finished?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        lock_path = Path(cast(str, delivery.job.lock.name))
        path = status.path(delivery.page, delivery.row)
        claimed = delivery.job.record
        current = the_list.load(MORTY.id, item.id)
        fields = urlencode(self.fields(current, "resend-Q1"))
        reopened = change_page.DeliveryStatus(root)
        blocked = "Resend is unavailable while the previous sender is active."

        def send_with_damage(_recipient: staff.StaffId, _message: str, _message_id: str
                             ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            with status.locked():
                sending = change_page.read_question_attempt(path)
                assert isinstance(sending.owner, delivery_state.QuestionSendOwner)
                change_page.publish_question_attempt(path, replace(sending, owner=replace(sending.owner, job_id="damaged")))
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=reopened)["waiting_for_answer"], "false")
            body = list_page.render(root, include_counts=False, status=reopened).decode().split("<script>", 1)[0]
            self.assertIn(blocked, body)
            self.assertNotIn('value="resend-Q1"', body)
            with self.assertRaisesRegex(question_record.AnswerRefused, re.escape(blocked)):
                _ = list_page.submit(root, fields, reopened)
            return question_outcome()

        with patch.object(change_page, "send_question", side_effect=send_with_damage) as send:
            list_page.deliver(asked)
            send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        with self.assertRaisesRegex(ValueError, "Invalid delivery field job_id"):
            _ = change_page.read_question_attempt(path)
        body = list_page.render(root, include_counts=False, status=reopened).decode().split("<script>", 1)[0]
        self.assertIn('value="resend-Q1">Resend question Q1</button>', body)
        outcome = (send_result.UnconfirmedSendOutcome(), "No reply from transport.")
        with patch.object(change_page, "send_question", return_value=outcome) as send:
            retried = list_page.submit(root, fields, reopened)
            assert isinstance(retried, list_page.ListQuestionDelivery)
            assert isinstance(retried.delivery, change_page.PageQuestionSend)
            assert isinstance(retried.delivery.job, change_page.QuestionJob)
            self.assertEqual(Path(cast(str, retried.delivery.job.lock.name)), lock_path)
            list_page.deliver(retried)
            list_page.deliver(list_page.submit(root, fields, reopened))
            send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        self.assertEqual(the_list.load(MORTY.id, item.id), current)

    def test_mixed_question_results_keep_counts_and_unread_in_agreement(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("The item with four questions")
        cases = (("Already answered?", question_outcome()), ("Sent to the owner?", question_outcome()),
                 ("The owner refused?", question_outcome("refused", "The owner is offline.")),
                 ("What happened?", (send_result.UnconfirmedSendOutcome(), "The response is missing.")))
        for words, outcome in cases:
            current = the_list.load(MORTY.id, item.id)
            delivery = list_page.submit(root, urlencode(self.fields(current, "question", words=words)), status)
            with (patch.object(change_page, "send_question", return_value=outcome),
                  patch.object(change_page, "alert_delivery_failures")):
                list_page.deliver(delivery)
        with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              redirect_stdout(io.StringIO())):
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The check passed."]), 0)
        reopened = change_page.DeliveryStatus(root)
        body = list_page.render(root, include_counts=False, status=reopened).decode().split("<script>", 1)[0]
        self.assertIn('data-waiting-for-answer="false"', body)
        self.assertIn('aria-label="Unread update"', body)
        self.assertIn("Question Q3 did not reach morty", body)
        self.assertIn("Question Q4 may not have reached morty", body)
        for identity in ("Q1", "Q2", "Q3", "Q4"):
            self.assertEqual(f'value="resend-{identity}"' in body, identity in ("Q3", "Q4"))
        self.assertEqual(len(list_page.waiting_updates(console.NewsRequest(root))), 1)
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=reopened)["unread"], "true")
        current = the_list.load(MORTY.id, item.id)
        fields = self.fields(current, "resend-Q4")
        with patch.object(change_page, "send_question", return_value=cases[-1][1]) as send:
            for _ in range(2):
                list_page.deliver(list_page.submit(root, urlencode(fields), reopened))
            send.assert_called_once()

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_a_saved_question_to_morty_answering(self) -> None:
        _ = self.add("Only this row matches the search", the_list.ItemState.WAITING_FOR_NATE, number=1)
        _ = self.add("Other item", number=2, owner=OTHER.id)
        self.browser_action_sequence(r"""
 const input=document.getElementById('list-search');
 const count=document.querySelector('[data-list-count]');
 const changes=[];
 window.fetch=new Proxy(window.fetch,{apply(fetch,receiver,args){
  changes.push(args[1]?.method || 'GET');return Reflect.apply(fetch,receiver,args);
 }});
 show('all');
 form.elements.words.value='Which check passed?';
 submit('question');
 check(!row.hidden,'The row stays in All until the save succeeds');
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(!row.hidden,'The saved row stays in All.');
 check(row.querySelector('[data-open-question-note]').getClientRects().length &&
  row.querySelector('.list-state').textContent==='Waiting for nate','The note appears below the saved state.');
 check(changes.join()==='POST','The chip changes without a refresh.');
 check(count.textContent==='2 of 2 items shown · 0 need you · 1 unread','All counts the saved row.');
 const needsMe=document.getElementById('filter-show-needs-me');
 const option=(menu,value)=>[...document.getElementById('list-'+menu).options].find(choice=>choice.value===value);
 check(needsMe.dataset.filterCount==='0' && option('owner',row.dataset.owner).dataset.filterCount==='1',
  'Needs me excludes the saved row and Owner includes it.');
 check(option('producer',row.dataset.producer).dataset.filterCount==='2','Producer counts the saved row.');
 for(const view of ['needs-me','blocked','parked','closed','working']){
  show(view);check(row.hidden,view+' hides the saved row');
 }
 location.hash=new URLSearchParams({show:'working',item:row.dataset.key}).toString();
 await flush();
 check(!row.hidden && row.closest('[data-linked-item]:not([hidden])') && !row.querySelector('[data-detail]').hidden &&
  row.querySelector('[data-waiting-note]').textContent==='The producer will answer.' &&
  !row.querySelector('[value="question"]'),'A direct link opens only the waiting row without Ask');
 check(count.textContent.startsWith('1 of 2'),'The linked row counts in All and stays outside Working.');
 show('morty-answering');
 input.value='Only this row';input.dispatchEvent(new Event('input'));
 await new Promise(resolve=>setTimeout(resolve,150));
 check(!row.hidden && count.textContent.startsWith('1 of 2'),'Search finds the waiting row in Morty answering.');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unpark_replay_keeps_a_waiting_item_hidden(self) -> None:
        item = self.add("Check the parked item", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=7), source="morty")
        self.browser_action_sequence('''
 show('parked');
 const chip=document.querySelector('[data-filter-row="show"] [data-filter-value="parked"]');
 const status=document.querySelector('[data-hidden-action-status]');
 const unpark=form.querySelector('[value="unpark"]');
 check(!row.hidden && chip.dataset.filterCount==='1','Parked includes the item before the question.');
 unpark.focus();
 submit('unpark');
 finishPost({parked:'true',waiting_for_answer:'true',open_question:'true',unpark_outcome:'cleared_newer_parking_active'});
 await flush();
 check(row.hidden && chip.dataset.filterCount==='0','The waiting item leaves Parked and its count.');
 check(document.activeElement===chip,'The Parked filter receives focus.');
 check(!status.hidden && status.textContent.includes('A newer parking time is active'),
  'Show keeps the Unpark confirmation for the waiting item.');
 location.hash=new URLSearchParams({show:'parked',item:row.dataset.key}).toString();
 await flush();
 check(!row.hidden && !row.querySelector('[data-detail]').hidden &&
  row.querySelector('[data-waiting-note]').textContent==='The producer will answer.' &&
  !row.querySelector('form'),'The direct link opens the item with only the waiting line.');
 check(chip.dataset.filterCount==='0' && !status.hidden,
  'The direct link leaves the item out of the count and keeps the Unpark confirmation.');
 show('morty-answering');
 check(!row.hidden && !status.hidden,'Morty answering keeps the Unpark confirmation.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_open_row_and_shows_the_refusal_after_a_lost_remove(self) -> None:
        self.browser_action_sequence('''
 show('working');
 const remove=submit('remove');
 check(row.hidden,'Remove immediately hides the row in Working');
 losePost();await flush();
 show('closed');
 check(!row.hidden,'the unconfirmed row appears in Closed');
 check(submit('remove').id===remove.id,'the exact Remove retry uses the same ID');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && hiddenStatus.getClientRects().length>0 &&
  hiddenStatus.textContent==='Item 1: Not saved: Fixture refuses Remove',
  'the refusal is visible when the Closed filter hides the restored row');
 show('working');
 check(!row.hidden && row.dataset.state==='Working','the refusal restores the confirmed row to Working');
 const notice=form.querySelector('[data-send-status]');
 check(notice.getClientRects().length>0 && notice.textContent==='Not saved: Fixture refuses Remove',
  'the refusal is visible on the restored row');
 check(hiddenStatus.hidden,'the page hides the extra notice when the restored row is visible');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_updates_the_unread_sort_before_refresh(self) -> None:
        with patch.object(the_list, "now", return_value=NOW - timedelta(minutes=1)):
            _ = self.add("An older unread update", number=2)
        self.browser_action_sequence("""
 show('all');
 const sort=document.querySelector('[data-sort="7"]');sort.click();sort.click();
 const first=()=>document.querySelector('[data-list-item]').dataset.number;
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 const original=updateKey();
 check(original>0 && first()==='1','the newest unread item sorts first');
 submit('remove');
 check(updateKey()===0 && first()==='2','a pending Remove sorts as read');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 check(updateKey()===original && first()==='1','a refusal restores the exact Update sort key');
 submit('remove');
 finishPost({item_state:'Closed',parked:'false',unread:'false'});await flush();
 check(updateKey()===0 && first()==='2','a confirmed Remove sorts as read without a refresh');
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_refusal_restores_update_order_during_motion(self) -> None:
        with patch.object(the_list, "now", return_value=NOW - timedelta(minutes=1)):
            _ = self.add("An older unread update", number=2)
        self.browser_action_sequence(r"""
 show('working');
 const sort=document.querySelector('[data-sort="7"]');sort.click();sort.click();
 const first=()=>document.querySelector('[data-list-item]:not([hidden])').dataset.number;
 const animation=()=>document.querySelector('[data-record-motion]')?.getAnimations({subtree:true})[0];
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 const original=updateKey();
 check(original>0 && first()==='1','the newest unread item starts first');
 submit('remove');
 const exit=animation();
 if(!exit) throw new Error('Remove must animate the exit.');
 exit.pause();exit.currentTime=100;
 check(row.hidden && updateKey()===0 && first()==='2','Remove clears the Update key during the exit');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const restore=animation();
 check(!row.hidden && restore && restore!==exit,'the refusal reverses the exit animation');
 check(updateKey()===original && first()==='1','the restored Update key puts the row first');
 restore?.finish();await flush();
 check(!animation() && first()==='1','the correct order remains after the animation');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_closed_response_clears_unread_when_the_server_reports_read(self) -> None:
        self.browser_action_sequence(r"""
 show('all');
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 check(updateKey()>0 && row.querySelector('.list-dot'),'the item starts unread');
 submit('remove');
 finishPost({item_state:'Closed',unread:'false'});await flush();
 check(row.dataset.state==='Closed' && row.dataset.unread==='false','the Closed item is read');
 check(updateKey()===0,'the Closed item has a zero Update key');
 check(!row.querySelector('.list-dot') && !form.querySelector('[value="read"]'),
  'the Closed item has no unread controls');
 onlyUnread();
 check(row.hidden,'the Closed item stays out of Unread');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_unread_answer_on_a_closed_item(self) -> None:
        item = self.add("Check the result after closure", number=1)
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            list_page.deliver(list_page.submit(root, urlencode(
                self.fields(item, "question", words="Which check passed?")), status))
        _ = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              redirect_stdout(io.StringIO())):
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1",
                                            "--text", "The gate passed."]), 0)
        self.browser_action_sequence(r"""
 show('closed');onlyUnread();
 check(!row.hidden && row.querySelector('.list-dot') && form.querySelector('[value="read"]'),
  'Closed with Unread only shows the answer and Mark read.');
 check(document.getElementById('filter-show-closed').dataset.filterCount==='1' &&
  !row.querySelector('[data-open-question-note]'),'The answer counts in Closed and clears the question note.');
 const answer=row.querySelector('.list-panel [data-question="Q1"]');
 check(answer?.querySelectorAll('time[datetime]').length===2 &&
  answer.querySelector('.question-unread')?.textContent==='Unread' &&
  answer.textContent.includes('The gate passed.') && !row.querySelector('[data-log-index="Q1-answered"]'),
  'The Closed answer stays in the panel with both times until read.');
 submit('read');finishPost({item_state:'Closed',unread:'false',open_question:'false'});await flush();
 check(row.hidden && document.getElementById('filter-show-closed').dataset.filterCount==='0',
  'Mark read removes the item from Unread only.');
""", add_item=False, reduced_motion=True, bulk_expand=True)
        current = the_list.load(MORTY.id, item.id)
        _ = list_page.submit(root, urlencode(self.fields(current, "read")), status)
        read = the_list.load(MORTY.id, item.id)
        self.assertIs(read.state, the_list.ItemState.CLOSED)
        self.assertIs(the_list.unread_update(read), the_list.NoUnreadUpdate.ABSENT)
        panel, log = list_page.render(root, include_counts=False, status=status).decode().split('<ol class="list-log">', 1)
        self.assertNotIn('data-question="Q1"', panel)
        self.assertIn('data-log-index="Q1-asked"', log)
        self.assertIn('data-log-index="Q1-answered"', log)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_restores_unread_after_a_lost_remove(self) -> None:
        self.browser_action_sequence("""
 const original=JSON.parse(row.dataset.sortValues)[7];
 submit('remove');losePost();await flush();
 show('closed');
 check(!row.hidden && row.dataset.unread==='false','the unconfirmed Closed row is read');
 form.elements.until.value='2026-12-03';
 submit('park');
 finishPost({reason:'Fixture refuses Park'});await flush();
 show('all');onlyUnread();
 check(!row.hidden && row.dataset.state==='Working','the refusal returns the Working row to Unread');
 check(row.querySelector('.list-dot') && form.querySelector('[value="read"]'),
  'the refusal restores the unread controls');
 check(JSON.parse(row.dataset.sortValues)[7]===original,'the refusal restores the Update sort key');
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_collapses_and_refusal_restores_the_row(self) -> None:
        _ = self.add("Next item", number=2)
        self.browser_action_sequence(r"""
 document.querySelector('[data-sort="0"]').click();
 const next=document.querySelector('[data-number="2"]');
 show('working');
 const top=element=>element.getBoundingClientRect().top;
 const start=top(next),first=top(row);
 const motion=()=>document.querySelector('[data-record-motion]');
 const sample=(animation,time)=>{animation.pause();animation.currentTime=time;};
 submit('remove');
 check(row.hidden && requests.length===1,'Remove starts the POST and hides the record in the same turn');
 const leaving=motion();
 check(leaving?.querySelector('[data-detail]:not([hidden])'),'the open details leave with the row');
 const exit=leaving?.getAnimations({subtree:true})[0];
 check(exit,'Remove starts the shared animation');
 if(!exit) throw new Error('No exit animation');
 sample(exit,100);
 const opacity=Number(getComputedStyle(exit.effect.target).opacity);
 check(opacity>0 && opacity<1,'the row fades before it disappears');
 check(top(next)<start && top(next)>first,'the next row moves up during the fade');
 exit.finish();await flush();
 check(!motion() && Math.abs(top(next)-first)<2,'the next row ends in the removed row position');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const restored=motion()?.getAnimations({subtree:true})[0];
 check(!row.hidden && restored,'the refusal restores the row with an animation');
 if(!restored) throw new Error('No restore animation');
 sample(restored,100);
 check(top(next)<start && top(next)>first,'the next row moves down during restore');
 submit('remove');
 const secondExit=motion()?.getAnimations({subtree:true})[0];
 check(row.hidden && secondExit && secondExit!==restored,'a second Remove reverses the restore');
 secondExit?.finish();await flush();
 check(!motion() && Math.abs(top(next)-first)<2,'the second Remove leaves the final layout intact');
 finishPost({item_state:'Closed',unread:'false'});await flush();
 show('closed');
 check(!row.hidden && !motion(),'a filter change shows the row at once');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_respects_reduced_motion(self) -> None:
        _ = self.add("Check the motion preference", number=1)
        for reduced_motion in (False, True):
            with self.subTest(reduced_motion=reduced_motion):
                self.browser_action_sequence(r"""
 show('working');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
 check(reduced===EXPECTED_REDUCTION,'the browser uses the selected motion preference');
 submit('remove');
 check(row.hidden,'Remove hides the item before the response');
 check(Boolean(document.querySelector('[data-record-motion]'))===!reduced,
  'only the full motion setting animates the exit');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 check(!row.hidden,'the refusal restores the row');
 check(Boolean(document.querySelector('[data-record-motion]'))===!reduced,
  'only the full motion setting animates the restoration');
""".replace('EXPECTED_REDUCTION', json.dumps(reduced_motion)), reduced_motion=reduced_motion, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_mark_read_fades_at_click_and_restores_after_a_lost_response(self) -> None:
        item = self.add("Check the unread update", number=1)
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "An update is ready."}, source="morty")
        self.browser_action_sequence(r"""
 onlyUnread();
 const motion=()=>document.querySelector('[data-record-motion]')?.getAnimations({subtree:true})[0];
 const read=submit('read'),firstExit=motion();
 check(row.hidden && firstExit,'Mark read starts the fade before the response');
 losePost();await flush();
 const restored=motion();
 check(!row.hidden && restored && restored!==firstExit,'a lost Mark read response slides the row back');
 check(form.querySelector('[data-send-status]').textContent==='Save not confirmed: Fixture lost response',
  'the restored item shows the failure reason');
 check(!form.querySelector('[value="read"]').disabled,'the restored item can retry Mark read');
 check(submit('read').id===read.id,'the exact Mark read retry uses the same ID');
 finishPost({unread:'true'});await flush();
 check(!row.hidden && row.dataset.unread==='true','a newer unread update returns the row to Unread');
 form.elements.words.value='Send the update.';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('tell');
 check(row.hidden && motion(),'Tell morty starts the fade before the response in Unread');
 finishPost({unread:'false'});await flush();
 check(row.hidden,'Tell morty keeps the row outside Unread after confirmation');
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_ask_leaves_unread_and_clears_the_dot_after_saving(self) -> None:
        self.browser_action_sequence(r"""
 onlyUnread();
 check(!row.hidden && row.querySelector('.list-dot'),'the item starts unread');
 form.elements.words.value='Which check passed?';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('question');
 check(row.hidden,'Ask question starts the fade before the response in Unread');
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(row.hidden && row.dataset.unread==='false','the saved question keeps the row outside Unread');
 onlyUnread(false);show('working');
 check(row.hidden && row.dataset.unread==='false' && !row.querySelector('.list-dot') && !form.querySelector('[value="read"]'),
  'The saved question also keeps the row outside Working with unread cleared.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_question_focus_only_when_the_submitted_row_had_focus(self) -> None:
        _ = self.add("The question item", number=1)
        _ = self.add("The next item", number=2, owner=OTHER.id)
        for destination in ("next row", "empty view", "user selection", "same row"):
            with self.subTest(destination=destination):
                self.browser_action_sequence("const destination=" + json.dumps(destination) + r""";
 const search=document.getElementById('list-search');
 const next=document.querySelector('[data-number="2"] .list-toggle');
 const all=document.querySelector('[data-filter-row="show"] [data-filter-value="all"]');
 if(destination!=='same row') show('working');
 if(destination==='empty view'){
  search.value='The question item';
  search.dispatchEvent(new CustomEvent('filterchange',{detail:search.value}));
 }
 const input=form.elements.words;
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 form.querySelector('[value="question"]').focus();
 submit('question');
 if(destination==='user selection') all.focus();
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 const expected=destination==='same row' ? row.querySelector('.list-toggle') :
  destination==='next row' ? next : destination==='empty view' ? search : all;
 check(document.activeElement===expected,'The browser keeps focus at '+destination);
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_related_link_reveals_waiting_item_without_changing_filters(self) -> None:
        first = self.add("Check the current result", number=1)
        target = self.add("Check the later result", number=2)
        _ = the_list.update(MORTY.id, first.id, {"related_to": (target.number,)}, source="nate")
        status = change_page.DeliveryStatus(self.root / "reviews")
        delivery = the_list.ask_question(MORTY.id, target.id, the_list.PageRequest("ask", "question", "What passed?"), status)
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            change_page.deliver(delivery, status)
        self.browser_action_sequence(r"""
 const search=document.getElementById('list-search');
 const target=document.querySelector('[data-number="2"]');
 const owner=document.getElementById('list-owner');
 show('working');
 owner.value=row.dataset.owner;owner.dispatchEvent(new Event('change'));
 search.value='check';search.dispatchEvent(new CustomEvent('filterchange',{detail:search.value}));
 check(target.hidden,'The waiting item stays out of Working.');
 row.querySelector('a[data-record-jump]').click();
 const place=new URLSearchParams(location.hash.slice(1));
 check(!target.hidden && target.closest('[data-linked-item]:not([hidden])') &&
  !target.querySelector('[data-detail]').hidden,'The related link opens the waiting item outside the results.');
 check(place.get('show')==='working' && place.get('owner')===row.dataset.owner && search.value==='check',
  'The related link keeps Show, Owner and the search');
 const results=document.querySelector('table[aria-label="The List"]');
 check(results.querySelectorAll('[data-list-item]:not([hidden])').length===1 &&
  document.querySelector('[data-list-count]').textContent.startsWith('1 of 2'),
  'The linked waiting row counts in All and stays outside Working.');
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_direct_link_keeps_a_read_item_outside_unread_results(self) -> None:
        _ = self.add("The unread item", number=1)
        target = self.add("The read item", number=2)
        _ = the_list.mark_read(MORTY.id, target.id, source="nate")
        self.browser_action_sequence(r"""
 const target=document.querySelector('[data-number="2"]');
 const results=document.querySelector('table[aria-label="The List"]');
 const section=document.querySelector('[data-linked-item]');
 const count=document.querySelector('[data-list-count]');
 const unread=document.getElementById('list-unread');
 location.hash=new URLSearchParams({show:'all',unread:'1',item:target.dataset.key}).toString();
 await flush();
 check(unread.checked && results.querySelectorAll('[data-list-item]:not([hidden])').length===1 &&
  count.textContent.startsWith('1 of 2'),'Unread only keeps one row in the results.');
 check(!section.hidden && section.contains(target) && !target.hidden && !target.querySelector('[data-detail]').hidden,
  'The direct link opens the read item outside the results.');
 onlyUnread(false);
 check(section.hidden && results.contains(target) && !target.hidden && count.textContent.startsWith('2 of 2'),
  'The read item rejoins the results when Unread only is off.');
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_ask_focus_after_a_failed_post(self) -> None:
        self.browser_action_sequence(r"""
 onlyUnread();
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 check(row.hidden,'The row leaves Unread during the request');
 ask.blur();
 check(document.activeElement===document.body,'The browser has no focused control while Ask is disabled');
 losePost();await flush();
 check(!row.hidden && !ask.disabled && document.activeElement===ask,'The restored Ask button has focus');
 submit('question');
 const search=document.getElementById('list-search');
 search.focus();
 finishPost({reason:'The POST was refused'});await flush();
 check(document.activeElement===search,'A failed request preserves the focus the user chose');
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_ask_focus_after_the_unread_row_finishes_its_animation(self) -> None:
        _ = self.add("Restore the unread row", number=1)
        for user_selects_search in (False, True):
            with self.subTest(user_selects_search=user_selects_search):
                self.browser_action_sequence(r"""
 onlyUnread();
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 losePost();await flush();
 const spacer=document.querySelector('[data-record-motion]');
 const animation=spacer?.getAnimations({subtree:true})[0];
 check(animation && getComputedStyle(row).display==='none','The row is still hidden by the animation');
 if(!animation) throw new Error('The restored row must animate.');
 const search=document.getElementById('list-search');
 if(USER_SELECTS_SEARCH) search.focus();
 animation.finish();await flush();
 check(!row.hidden && !ask.disabled,'The row and Ask button are available');
 check(document.activeElement===(USER_SELECTS_SEARCH ? search : ask),'Focus returns only if the user did not move it');
""".replace('USER_SELECTS_SEARCH', json.dumps(user_selects_search)), add_item=False)

    def add_item_with_failed_question(self) -> the_list.ListItem:
        root = self.root / "reviews"
        item = self.add("Keep the row with a failed question", number=1)
        delivery = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check failed?")))
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "Producer offline")),
              patch.object(change_page, "alert_delivery_failures")):
            list_page.deliver(delivery)
        return the_list.load(MORTY.id, item.id)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_row_focus_through_refresh_when_an_older_question_needs_resend(self) -> None:
        _ = self.add_item_with_failed_question()
        self.browser_action_sequence(r"""
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 check(Boolean(form.querySelector('[value="resend-Q1"]')),'The older question needs a resend');
 refreshMarkup=document.documentElement.outerHTML;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed!==row && !refreshed.hidden,'The refresh keeps the row visible');
 check(document.activeElement===refreshed.querySelector('.list-toggle'),'The row toggle keeps focus through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_focus_when_a_mixed_question_save_removes_the_row_from_unread(self) -> None:
        item = self.add_item_with_failed_question()
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        _ = self.add("The next unread item", number=2)
        self.browser_action_sequence(r"""
 onlyUnread();
 check(!row.hidden && form.querySelector('[value="resend-Q1"]'),'Unread shows the item with a failed question');
 const snapshot=document.documentElement.cloneNode(true);
 snapshot.querySelector('[data-number="1"]').dataset.unread='false';
 refreshMarkup=snapshot.outerHTML;
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed!==row && refreshed.hidden,'The item leaves Unread and the List refreshes');
 check(document.activeElement===document.querySelector('[data-number="2"] .list-toggle'),
  'Focus moves to the next unread item and stays there through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_the_selected_action_on_another_item_after_refresh(self) -> None:
        _ = self.add_item_with_failed_question()
        _ = self.add("The next open item", number=2)
        self.browser_action_sequence(r"""
 const other=document.querySelector('[data-number="2"]');
 other.querySelector('.list-toggle').click();
 const remove=other.querySelector('button[value="remove"]');
 refreshMarkup=document.documentElement.outerHTML;
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 remove.focus();
 check(document.activeElement===remove,'The user selects Remove on the other item during the request');
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
 const refreshed=document.querySelector('[data-number="2"]');
 check(refreshed!==other && !refreshed.hidden,'The List refreshes the other item');
 check(document.activeElement===refreshed.querySelector('button[value="remove"]'),
  'Remove on the same item keeps focus through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_the_column_sort_focus_after_refresh(self) -> None:
        _ = self.add_item_with_failed_question()
        self.browser_action_sequence(r"""
 const sort=document.querySelector('[data-sort="3"]');
 refreshMarkup=document.documentElement.outerHTML;
 form.elements.words.value='Which check is next?';form.elements.words.dispatchEvent(new Event('input'));
 form.querySelector('[value="question"]').focus();
 submit('question');
 sort.focus();
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
 const renewed=document.querySelector('[data-sort="3"]');
 check(renewed!==sort,'The List replaces the sort button during refresh');
 check(document.activeElement===renewed,'The selected column sort keeps focus');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_skips_a_row_that_is_still_in_its_return_animation(self) -> None:
        _ = self.add("The first unread item", number=1)
        _ = self.add("The second unread item", number=2)
        self.browser_action_sequence(r"""
 onlyUnread();
 const second=document.querySelector('[data-number="2"]');
 second.querySelector('.list-toggle').click();
 function ask(target){
  target.elements.words.value='Which check passed?';target.elements.words.dispatchEvent(new Event('input'));
  target.querySelector('[value="question"]').focus();
  submit('question',target);
 }
 ask(form);
 const failFirst=finishPost;
 ask(second.querySelector('form'));
 failFirst({reason:'The save failed'});await flush();
 const returning=row.nextElementSibling.getAnimations({subtree:true})[0];
 check(!row.hidden && !row.querySelector('.list-toggle').getClientRects().length,
  'The first row returns but its animation still hides its toggle');
 returning.pause();
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(document.activeElement===document.getElementById('list-search'),
  'Search receives focus because neither row has an available toggle');
 returning.finish();
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_question_response_preserves_focus_in_the_parent_console(self) -> None:
        _ = self.add("The question item", number=1)
        _ = self.add("The next item", number=2, owner=OTHER.id)
        self.browser_action_sequence(r"""
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 const nav=parent.document.getElementById('console-nav');
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 check(document.hasFocus() && document.activeElement===ask,'Ask starts with focus in the List frame');
 submit('question');
 nav.focus();
 check(!document.hasFocus() && parent.document.activeElement===nav,'The user focuses the console during the request');
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(!row.hidden,'The saved question stays in All.');
 check(parent.document.activeElement===nav,'The response preserves the console focus');
""", add_item=False, reduced_motion=True, in_frame=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_direct_answer_link_waits_for_visibility(self) -> None:
        root = self.root / "reviews"
        item = self.add("Read the answer after opening the tab")
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            fields = self.fields(item, "question", words="Which check passed?")
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The gate passed."]), 0)
        markup = list_page.render(root, include_counts=False).decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        fragment = urlencode({"show": "unread", "item": f"{MORTY.id}/{item.id}"})
        setup = """<script>
let visibility='hidden';
Object.defineProperty(document,'visibilityState',{get:()=>visibility});
window.frameElement.hidden=true;
history.replaceState(null,'','#FRAGMENT');
const reads=[];
window.fetch=async(_url,options)=>{
 if(options?.method!=='POST') return new Response('',{status:503});
 reads.push(new URLSearchParams(options.body));
 return new Response(JSON.stringify({unread:'true'}));
};
</script>""".replace('FRAGMENT', fragment)
        probe = """<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const row=document.querySelector('[data-list-item]'),detail=row.querySelector('[data-detail]');
 const panel=detail.querySelector('.list-panel');
 const unread=()=>row.dataset.unread==='true' && panel.querySelector('.question-unread');
 const showTab=()=>{visibility='visible';document.dispatchEvent(new Event('visibilitychange'));};
 const showFrame=()=>{
  window.frameElement.hidden=false;
  window.dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 };
 try{
  check(!row.hidden && !detail.hidden,'The direct link opens the unread row');
  check(!reads.length && unread(),'A hidden tab keeps the answer unread');
  if(CLOSE_ROW){
   row.querySelector('.list-toggle').click();
   document.getElementById('list-expand-all').click();
  }
  REVEAL_FIRST();
  check(!reads.length && unread(),'The answer stays unread while the tab or frame is hidden');
  REVEAL_SECOND();
  if(CLOSE_ROW) check(!reads.length && unread(),'Closing the row cancels its deferred read');
  else check(reads.length===1 && reads[0].get('action')==='read' &&
   reads[0].get('request')===panel.dataset.openReadRequest && reads[0].get('token')===panel.dataset.openReadToken,
   'The visible row sends its signed read once');
 }catch(error){failures.push(String(error));}
 const result=parent.document.createElement('p');result.id='probe-results';
 result.textContent=JSON.stringify(failures);parent.document.body.append(result);
})();
</script>"""
        for first, second, close_row in (('showTab', 'showFrame', False), ('showFrame', 'showTab', False),
                                         ('showTab', 'showFrame', True)):
            with self.subTest(first=first, close_row=close_row):
                case = (probe.replace('REVEAL_FIRST', first).replace('REVEAL_SECOND', second)
                        .replace('CLOSE_ROW', str(close_row).lower()))
                self.browser_actions(markup, setup, case, in_frame=True)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_ask_saves_and_shows_the_newer_update_and_answer(self) -> None:
        item = self.add("Check the current result")
        root = self.root / "reviews"
        results: list[list[str]] = []
        complete = threading.Event()
        probe = """<!doctype html><meta charset="utf-8"><iframe></iframe><img hidden src="/complete"><script>
const frame=document.querySelector('iframe'), errors=[];
const check=(ok,message)=>{if(!ok) errors.push(message);};
const open=(show,key='',unread='')=>new Promise(resolve=>{
 frame.onload=resolve;
 frame.src='LIST_PATH?fixture='+show+unread+'#'+new URLSearchParams({show,unread,...(key ? {item:key} : {})});
});
const row=()=>frame.contentDocument.querySelector('[data-list-item]');
const hasDot=()=>Boolean(row().querySelector('.list-dot'));
const act=async (action,words='')=>{
 const win=frame.contentWindow, current=row();
 const form=current.querySelector('form'), input=form.elements.words;
 const fetch=win.fetch.bind(win);
 let post, observer;
 const settled=new Promise(resolve=>{
  observer=new win.MutationObserver(()=>{if(!form.dataset.sending) resolve();});
  observer.observe(form,{attributeFilter:['data-sending']});
 });
 win.fetch=(url,options)=>options?.method==='POST'
  ? (post=fetch(url,options))
  : Promise.resolve(new win.Response('',{status:503}));
 try{
  if(current.querySelector('[data-detail]').hidden){
   if(action==='read') frame.contentDocument.getElementById('list-expand-all').click();
   else current.querySelector('.list-toggle').click();
  }
  input.value=words;input.dispatchEvent(new win.Event('input'));
  form.querySelector('[value="'+action+'"]').click();
  if(!post || !(await post).ok) throw new Error('The List POST must succeed.');
  await settled;
  return form.querySelector('[data-send-status]').textContent;
 }finally{observer.disconnect();win.fetch=fetch;}
};
const change=async path=>{
 if(!(await fetch(path,{method:'POST'})).ok) throw new Error('Fixture update failed: '+path);
};
(async()=>{
 try{
  await open('all','','1');
  check(!row().hidden && hasDot(),'the item starts in Unread');
  check(await act('question','Which check passed?')==='Question saved','the first question saves');
  check(row().hidden && !hasDot(),'the saved question leaves Unread and clears the dot');
  check(row().querySelector('[data-waiting-questions] time[datetime]'),
   'the save response supplies the asked time for the waiting row');
  const key=row().dataset.key;
  await open('working');
  check(row().hidden && !row().querySelector('[data-open-question-note]').hidden,
   'The saved question has its note and stays out of Working.');
  await open('all',key);
  check(!row().hidden && !row().querySelector('[data-detail]').hidden &&
   row().querySelector('[data-waiting-note]')?.textContent==='The producer will answer.' &&
   !row().querySelector('[value="question"]'),'a direct link shows the waiting line without Ask');
  const waitingQuestion=row().querySelector('[data-waiting-questions] [data-question="Q1"]');
  check(/(?:EST|EDT)$/.test(waitingQuestion?.querySelector('time[datetime]')?.textContent),
   'the waiting question shows the asked time in the user zone');
  await change('/answer');
  await open('working','','1');
  const unreadQuestion=row().querySelector('.list-panel [data-question="Q1"]');
  check(!row().hidden && hasDot() && unreadQuestion?.textContent.includes('Which check passed?') &&
   unreadQuestion.textContent.includes('The gate passed.') && !row().querySelector('[data-open-question-note]'),
   'Working with Unread only shows the answer in the panel and clears the question note.');
  check(unreadQuestion?.querySelectorAll('time[datetime]').length===2 &&
   unreadQuestion.querySelector('.question-unread')?.textContent==='Unread',
   'the unread answer shows both times and the Unread mark');
  check(!row().querySelector('.list-log').textContent.includes('The gate passed.'),
   'the log has no unread answer');
  check(await act('read')==='Marked read','Mark read saves the existing read action');
  await open('working',key);
  const log=row().querySelector('.list-log');
  check(!hasDot() && !row().querySelector('[data-question="Q1"]'),
   'the read question leaves the panel');
  check(log.querySelector('[data-log-index="Q1-answered"]').textContent.includes('Answer — morty') &&
   log.querySelector('[data-log-index="Q1-asked"]').textContent.includes('Question Q1 — nate'),
   'the log shows both parts of the read question with their authors');
  await change('/update');
  await open('working','','1');
  await change('/newer');
  check(await act('question','Which check is next?')==='Question saved; update remains unread',
   'the notice says the newer update remains unread');
  check(hasDot() && row().hidden,'The unread item leaves Working after the question saves.');
  frame.contentDocument.getElementById('filter-show-morty-answering').click();
  check(hasDot() && !row().hidden,'Morty answering shows the unread item.');
  const nextQuestion=(current)=>current.querySelector('[data-waiting-questions] [data-question="Q2"]');
  const firstAnswer=(current)=>current.querySelector('[data-log-index="Q1-answered"]');
  check(nextQuestion(row()) && firstAnswer(row())?.textContent.includes('The gate passed.'),
   'the Ask response keeps the first answer in the log beside the new waiting question');
  await open('all',key);
  check(!row().querySelector('[data-detail]').hidden && nextQuestion(row()) && firstAnswer(row()),
   'a direct link shows both the waiting question and the first answer in the log');
  await change('/second-answer');
  await open('working','','1');
  const original=row(), doc=frame.contentDocument;
  check(hasDot() && original.querySelector('[data-question="Q2"] .question-unread'),
   'the second answer stays unread before the row opens');
  const refreshed=new Promise(resolve=>{
   const observer=new frame.contentWindow.MutationObserver(()=>{
    if(row()!==original && row().querySelector('[data-log-index="Q2-answered"]')){
     observer.disconnect();resolve();
    }
   });
   observer.observe(doc.body,{childList:true,subtree:true});
  });
  original.querySelector('.list-toggle').click();
  await refreshed;
  check(!hasDot() && !row().hidden && !row().querySelector('[data-detail]').hidden,
   'opening the row marks the answer read and keeps the row open in Unread');
  check(row().querySelector('[data-log-index="Q2-answered"]').textContent.includes('The next check passed.'),
   'opening the row moves the answer to the log');
  row().querySelector('.list-toggle').click();
  check(row().hidden,'closing the read row removes it from Unread');
 }catch(error){errors.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(errors)});
})();
</script>""".replace('LIST_PATH', list_page.PAGE_PATH).encode()

        class Probe(serve.ReviewLinksHandler):
            @override
            def send_head(self) -> BinaryIO | None:
                if self.path == '/proof':
                    self.html_response(200, probe)
                elif self.path == '/complete':
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                    self.json_response(200, {})
                else:
                    return super().send_head()
                return None

            @override
            def do_POST(self) -> None:
                if self.path in ('/update', '/newer'):
                    _ = the_list.update(MORTY.id, item.id, {"latest_status": f"New result from {self.path}."}, source="morty")
                    self.json_response(200, {})
                elif self.path in ('/answer', '/second-answer'):
                    question_id, words = (("Q1", "The gate passed.") if self.path == '/answer'
                                          else ("Q2", "The next check passed."))
                    result = the_list.main(["answer", item.id, "--question", question_id, "--text", words])
                    self.json_response(200 if result == 0 else 500, {})
                elif self.path == '/results':
                    results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    self.json_response(200, {})
                    complete.set()
                else:
                    super().do_POST()

        with (patch.object(serve, "ReviewLinksHandler", Probe),
              patch.object(review_page.ReviewSnapshotCache, "start"),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(list_page, "counts_strip", return_value=""),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              patch.object(change_page, "send_question", return_value=question_outcome()),
              serve.ReviewLinksServer(("127.0.0.1", 0), root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                profile = self.root / "ask-profile"
                profile.mkdir()
                browser = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                    '--window-size', '1440,900', '--screenshot', str(self.root / 'ask.png'),
                    f'http://127.0.0.1:{server.server_port}/proof'], capture_output=True, text=True, timeout=45)
                self.assertEqual(browser.returncode, 0, browser.stderr)
                self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join()

    def browser_action_sequence(self, steps: str, *, reduced_motion: bool = False, add_item: bool = True,
                                window_size: tuple[int, int] | None = None, in_frame: bool = False,
                                bulk_expand: bool = False) -> None:
        if add_item:
            _ = self.add("Test List action retries", number=1)
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        setup = '''<script>
const requests=[];let finishPost,losePost,refreshMarkup;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST'){
  if(refreshMarkup===undefined) throw new Error('Fixture refresh refused');
  return {ok:true,text:async()=>refreshMarkup};
 }
 requests.push(new URLSearchParams(options.body));
 return new Promise((resolve,reject)=>{
  losePost=()=>reject(new Error('Fixture lost response'));
  finishPost=result=>resolve({ok:!result.reason,status:result.status ?? (result.reason ? 400 : 200),
   json:async()=>({unread:'true',...result})});
 });
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const row=document.querySelector('[data-number="1"]'),form=row.querySelector('form');
 const submit=(action,target=form)=>{
  const count=requests.length;
  target.requestSubmit(target.querySelector('[value="'+action+'"]'));
  if(requests.length!==count+1) throw new Error('The test submission did not start.');
  const request=requests.at(-1);
  return {id:request.get('request'),token:request.get('token')};
 };
 const show=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();
 const onlyUnread=(on=true)=>{const control=document.getElementById('list-unread');if(control.checked!==on) control.click();};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 try{
  document.querySelector('OPEN_CONTROL').click();
''' + steps + '''
 }catch(error){failures.push(String(error));}
 const result=parent.document.createElement('p');result.id='probe-results';
 result.textContent=JSON.stringify(failures);parent.document.body.append(result);
})();
</script>'''
        open_control = '#list-expand-all' if bulk_expand else '[data-number="1"] .list-toggle'
        probe = probe.replace('OPEN_CONTROL', open_control)
        self.browser_actions(markup, setup, probe, reduced_motion=reduced_motion,
                             window_size=window_size, in_frame=in_frame)

    def browser_actions(self, markup: str, setup: str, probe: str, *,
                        reduced_motion: bool = False, virtual_time_ms: int = 1500,
                        window_size: tuple[int, int] | None = None, in_frame: bool = False) -> None:
        page = self.root / 'actions.html'
        _ = page.write_text(markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>'))
        if in_frame:
            page = self.root / 'actions-console.html'
            _ = page.write_text('<!doctype html><button id="console-nav">Console navigation</button>'
                                + '<iframe title="The List" src="actions.html"></iframe>')
        result = subprocess.run(['google-chrome', '--headless', '--no-sandbox', '--disable-gpu',
            *(['--force-prefers-reduced-motion'] if reduced_motion else []),
            *([f'--window-size={window_size[0]},{window_size[1]}'] if window_size else []),
            *(['--allow-file-access-from-files'] if in_frame else []),
            '--user-data-dir=' + str(self.root / 'chrome-actions'), f'--virtual-time-budget={virtual_time_ms}',
            '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = '<p id="probe-results">'
        self.assertIn(marker, result.stdout[-3000:], 'The browser must complete the item action checks.')
        failures = cast(list[str], json.loads(html.unescape(result.stdout.split(marker, 1)[1].split('</p>', 1)[0])))
        self.assertEqual(failures, [])

    def test_delivery_notices_color_staff_names_and_keep_the_recorded_words(self) -> None:
        _ = self.add("Delivery notice")
        cases: tuple[tuple[the_list.PageFeedback, str, tuple[str, ...]], ...] = (
            (the_list.PageFeedback("request", "Words"), "sent to morty", ("morty",)),
            (the_list.PageFeedback("request", "Words", "sent"), "sent to morty", ("morty",)),
            (the_list.PageFeedback("request", "Words", "alerted", "sdlc"), "not sent; sdlc told", ("sdlc",)),
            (the_list.PageFeedback("request", "Words", "alerted", "<unknown>"), "not sent; <unknown> told", ()),
            (the_list.PageFeedback("request", "Words", "alerted"), "not sent; no showrunner for alert", ()),
        )
        for feedback, text, colored in cases:
            with (self.subTest(state=feedback.state, recipient=feedback.alerted_to),
                  patch.object(the_list, "sent_to_morty", return_value=NOW),
                  patch.object(the_list, "feedback_requests", return_value={feedback.request: feedback})):
                body = list_page.render(self.root / "reviews").decode()
                cell = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
                notice = ElementTree.fromstring('<root>' + cell + '</root>').find('small')
                assert notice is not None
                self.assertEqual(''.join(notice.itertext()), text + ", 13:00 EST")
                self.assertEqual(tuple(span.text for span in notice.findall('span')), colored)

    def test_signed_feedback_logs_words_and_uses_the_shared_sender(self) -> None:
        root = self.root / "reviews"
        item = self.add("The feedback row", number=106)
        initial = item
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(self.fields(item, "read")))
            list_page.deliver(delivery)
            send.assert_not_called()
            item = the_list.load(MORTY.id, item.id)
            self.assertIs(item.unread_since, the_list.NoTimestamp.UNSET)
            self.assertEqual(item.log[-1].what, the_list.MarkedRead())
            for action, extra, words in (
                    ("park", {"until": "2026-12-03"}, "List page: nate parked item 106 until 2026-12-03."),
                    ("tell", {"words": "Explain this simply.\nKeep these words."}, "Explain this simply.\nKeep these words."),
                    ("remove", {}, "List page: nate removed item 106.")):
                with self.subTest(action=action):
                    fields = self.fields(item, action, **extra)
                    delivery = list_page.submit(root, urlencode(fields))
                    item = the_list.load(MORTY.id, item.id)
                    feedback = the_list.feedback_requests(item)[fields["request"]]
                    self.assertEqual(feedback.words, words)
                    if action == "tell":
                        self.assertEqual(item.log[-1].what, the_list.MarkedRead())
                    self.assertEqual(item.log[-2 if action == "tell" else -1].what, feedback)
                    self.assertEqual(the_list.sent_to_morty(item), NOW)
                    body = list_page.render(root).decode()
                    self.assertIn("sent to morty, 13:00 EST", without_role_colors(body))
                    self.assertIn('data-feedback-form', body)
                    log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
                    self.assertIn("Feedback for morty", log)
                    self.assertIn(html.escape(words.splitlines()[0]), log)
                    self.assertNotIn(fields["request"], log)
                    list_page.deliver(delivery)
                    self.assertEqual(send.call_args.args[0], MORTY.id)
                    expected = "List page: nate's words for item 106\n" + words if action == "tell" else words
                    self.assertEqual(send.call_args.args[2], expected)
                    item = the_list.load(MORTY.id, item.id)
                    before = send.call_count, item.log
                    list_page.deliver(list_page.submit(root, urlencode(fields)))
                    self.assertEqual((send.call_count, the_list.load(MORTY.id, item.id).log), before)
            self.assertEqual(item.state, the_list.ItemState.CLOSED)
            self.assertEqual(item.parked_until, the_list.time_value("2026-12-03"))
            self.assertEqual((item.title, item.owner, item.next_step), (initial.title, initial.owner, initial.next_step))
            _ = the_list.reopen(MORTY.id, item.id, source="morty")
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty acts."}, source="morty")
            self.assertNotIn("<small>sent to morty,", without_role_colors(list_page.render(root).decode()))
            good = self.fields(item, "tell", words="Keep it.")
            for body in (urlencode({**good, "token": "bad"}), urlencode(good) + '&id=L2',
                         urlencode({**good, "words": ""}), urlencode({**good, "words": " \t\n "}),
                         urlencode({**good, "action": "unknown"}),
                         urlencode({**good, "action": "park", "until": "2026-02-30"})):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    _ = list_page.submit(root, body)

    def test_tell_marks_an_unread_update_read_once_and_saves_feedback(self) -> None:
        item = self.add("Read the update before sending feedback")
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            for _ in range(2):
                _ = list_page.submit(self.root / "reviews", urlencode(fields))
        mark.assert_called_once_with(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=the_list.unread_update(item))
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual([(entry.what, entry.source) for entry in saved.log if isinstance(entry.what, the_list.MarkedRead)],
                         [(the_list.MarkedRead(), the_list.PAGE_SOURCE)])
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    def test_tell_on_a_read_item_adds_no_read_log_entry(self) -> None:
        item = self.add("An update already read")
        item = the_list.mark_read(MORTY.id, item.id, source="nate")
        before = tuple(entry for entry in item.log if isinstance(entry.what, the_list.MarkedRead))
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            _ = list_page.submit(self.root / "reviews", urlencode(fields))
        mark.assert_not_called()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(tuple(entry for entry in saved.log if isinstance(entry.what, the_list.MarkedRead)), before)
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    def test_refused_tell_keeps_the_unread_update_and_log(self) -> None:
        item = self.add("Keep the unread update after a refusal")
        for words in ("", " \t\n "):
            with self.subTest(words=words), self.assertRaises(question_record.AnswerRefused):
                _ = list_page.submit(self.root / "reviews", urlencode(self.fields(item, "tell", words=words)))
            self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_tell_storage_failure_keeps_the_unread_update_and_log(self) -> None:
        item = self.add("Keep the update when feedback storage fails")
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "save_feedback", side_effect=OSError("Fixture storage failure")):
            with self.assertRaises(OSError):
                _ = list_page.submit(self.root / "reviews", urlencode(fields))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_tell_read_failure_keeps_feedback_and_reports_the_unread_update(self) -> None:
        item = self.add("Keep feedback when the read write fails")
        root = self.root / "reviews"
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", side_effect=OSError("Fixture read failure")):
            delivery = list_page.submit(root, urlencode(fields))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.unread_since, item.unread_since)
        self.assertFalse(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["unread"], "true")
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(delivery)
        send.assert_called_once()

    def test_repeated_tell_keeps_a_newer_update_unread(self) -> None:
        item = self.add("Read only the original update")
        fields = self.fields(item, "tell", words="Keep this result.")
        root = self.root / "reviews"
        _ = list_page.submit(root, urlencode(fields))
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        _ = list_page.submit(root, urlencode(fields))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved, newer)
        self.assertEqual(len(the_list.feedback_requests(saved)), 1)
        self.assertEqual(sum(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log), 1)

    def test_legacy_tell_retry_uses_only_its_saved_read_target(self) -> None:
        root = self.root / "reviews"
        for case in ("saved", "newer", "first_send"):
            with self.subTest(case=case):
                item = self.add("Keep the legacy read target")
                request = uuid4().hex
                words = "Keep this result."
                token = list_page.request_token(root, MORTY.id, item.id, request)
                body = urlencode(self.fields(item, "tell", request=request, token=token, words=words))
                if case != "first_send":
                    feedback = the_list.PageFeedback(request, words, read_update=the_list.unread_update(item))
                    _ = the_list.save_feedback(MORTY.id, item.id, feedback)
                if case == "newer":
                    item = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
                _ = list_page.submit(root, body)
                saved = the_list.load(MORTY.id, item.id)
                expected = the_list.NoTimestamp.UNSET if case == "saved" else item.unread_since
                self.assertEqual(saved.unread_since, expected)
                self.assertEqual(len(the_list.feedback_requests(saved)), 1)
                self.assertEqual(sum(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log), int(case == "saved"))

    def test_conflicting_tell_keeps_a_newer_update_unread(self) -> None:
        item = self.add("Refuse new words under the saved request")
        fields = self.fields(item, "tell", words="Keep this result.")
        root = self.root / "reviews"
        _ = list_page.submit(root, urlencode(fields))
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        with self.assertRaises(the_list.ListError):
            _ = list_page.submit(root, urlencode({**fields, "words": "Different words."}))
        self.assertEqual(the_list.load(MORTY.id, item.id), newer)

    def test_tell_keeps_an_update_that_arrives_after_feedback_storage_unread(self) -> None:
        item = self.add("Keep the update that arrives during Tell")
        fields = self.fields(item, "tell", words="Keep this result.")
        save_feedback = the_list.save_feedback

        def save(keeper: staff.StaffId, identity: str, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
            saved = save_feedback(keeper, identity, feedback)
            _ = the_list.update(keeper, identity, {"latest_status": "A newer result arrived."}, source="morty")
            return saved

        with patch.object(the_list, "save_feedback", side_effect=save):
            _ = list_page.submit(self.root / "reviews", urlencode(fields))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.unread_since, item.unread_since)
        self.assertEqual(saved.latest_status, "A newer result arrived.")
        self.assertFalse(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_read_and_tell_clear_the_unread_controls_without_a_refresh(self) -> None:
        _ = self.add("Check the update")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 return {ok:true,json:async()=>({unread:'UNREAD'})};
};
</script>'''
        probe = '''<script>
(async()=>{
 const row=document.querySelector('[data-list-item]'),form=row.querySelector('form');
 document.getElementById('list-unread').click();
 const before=row.dataset.unread==='true' && row.querySelector('.list-dot') && form.querySelector('[value="read"]') && !row.hidden;
 form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
 form.requestSubmit(form.querySelector('[value="ACTION"]'));
 await new Promise(resolve=>setTimeout(resolve,0));
 const cleared=row.dataset.unread==='false' && !row.querySelector('.list-dot') && !form.querySelector('[value="read"]') && row.hidden;
 const retained=row.dataset.unread==='true' && row.querySelector('.list-dot') && form.querySelector('[value="read"]') && !row.hidden;
 const after=EXPECTED_UNREAD ? retained : cleared;
 const notice=form.querySelector('[data-send-status]').textContent;
 const result=document.createElement('p');result.id='probe-result';
 result.textContent=before && after && notice==='NOTICE' ? 'PASS' : 'FAIL';document.body.append(result);
})();
</script>'''
        for action, unread, notice in (("read", "false", "Marked read"), ("read", "true", "Update remains unread"),
                                      ("tell", "false", "sent to morty"),
                                      ("tell", "true", "sent to morty; update remains unread")):
            with self.subTest(action=action, unread=unread):
                page = self.root / f"{action}-{unread}.html"
                _ = page.write_text(markup.replace('<head>', '<head>' + setup.replace('UNREAD', unread), 1).replace('</body>',
                    probe.replace('ACTION', action).replace('NOTICE', notice).replace('EXPECTED_UNREAD', unread) + '</body>'))
                result = subprocess.run(['google-chrome', '--headless', '--no-sandbox', '--disable-gpu',
                    '--user-data-dir=' + str(self.root / f'chrome-{action}-{unread}'), '--virtual-time-budget=1000',
                    '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue('<p id="probe-result">PASS</p>' in result.stdout,
                                f"The {action} action must show the saved unread state and apply the Unread filter.")

    def test_feedback_recovers_after_restart_and_alerts_after_failed_tries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Recover the message")
        _ = list_page.submit(root, urlencode(self.fields(item, "tell", words="Ask sdlc for an ETA.")))
        saved = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff") as send,
              patch.object(change_review, "lists_directory", return_value=self.root / "lists")):
            list_page.recover_pending()
            send.assert_called_once()
            self.assertIn("Ask sdlc for an ETA.", cast(str, send.call_args.args[2]))
            list_page.recover_pending()
            send.assert_called_once()
        self.assertEqual(the_list.feedback_requests(the_list.load(MORTY.id, item.id)),
                         {key: replace(value, state="sent") for key, value in the_list.feedback_requests(saved).items()})
        delivery = list_page.submit(root, urlencode(self.fields(item, "remove")))
        def fail_message(recipient: staff.StaffId, _sender: str, _message: str) -> None:
            if recipient == MORTY.id:
                raise RuntimeError("fixture offline")
        with (patch.object(change_page, "send_to_staff", side_effect=fail_message) as send,
              patch.object(time, "sleep"), redirect_stderr(io.StringIO())):
            list_page.deliver(delivery)
            tries = conf.read("review_links").whole("delivery_tries")
            self.assertEqual(send.call_count, tries + 1)
            self.assertIn("List feedback needs attention", cast(str, send.call_args.args[2]))
            self.assertIn("nate removed item 1", cast(str, send.call_args.args[2]))
            self.assertEqual(send.call_args.args[0], OWNER.id)
            list_page.recover_pending()
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, tries + 1)
            alerted = the_list.load(MORTY.id, item.id)
            latest = tuple(the_list.feedback_requests(alerted).values())[-1]
            self.assertEqual((latest.state, latest.alerted_to), ("alerted", "sdlc"))
            self.assertIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
            self.assertEqual(the_list.mark_read(MORTY.id, item.id, source="morty"), alerted)
            self.assertIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
        self.assertEqual(the_list.load(MORTY.id, item.id).state, the_list.ItemState.CLOSED)

    def test_failed_alert_keeps_feedback_pending_for_post_and_startup_retries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Retry when the alert fails")
        fields = self.fields(item, "tell", words="Keep trying until someone receives this.")
        delivery = list_page.submit(root, urlencode(fields))
        pending = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff", side_effect=RuntimeError("all sends fail")) as send,
              patch.object(time, "sleep"), redirect_stderr(io.StringIO())):
            list_page.deliver(delivery)
            tries = conf.read("review_links").whole("delivery_tries")
            self.assertEqual(send.call_count, tries + 1)
            self.assertEqual(the_list.feedback_requests(the_list.load(MORTY.id, item.id))[fields["request"]].state, "pending")
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(send.call_count, 2 * (tries + 1))
            list_page.recover_pending()
            self.assertEqual(send.call_count, 3 * (tries + 1))
            self.assertEqual(the_list.load(MORTY.id, item.id), pending)
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            list_page.recover_pending()
            send.assert_called_once()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].state, "sent")

    def test_feedback_save_failures_leave_recoverable_requests_only(self) -> None:
        root = self.root / "reviews"
        item = self.add("Save before sending")
        for published in (False, True):
            with self.subTest(published=published):
                fields = self.fields(item, "tell", words="Keep these words.")
                original = the_list.save_feedback
                def failed_save(keeper: staff.StaffId, identity: str, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
                    if published:
                        _ = original(keeper, identity, feedback)
                    raise OSError("fixture save failed")
                with patch.object(the_list, "save_feedback", side_effect=failed_save):
                    with self.assertRaisesRegex(OSError, "fixture save failed"):
                        _ = list_page.submit(root, urlencode(fields))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(isinstance(the_list.sent_to_morty(saved), datetime), published)
                self.assertEqual(fields["request"] in the_list.feedback_requests(saved), published)
                with patch.object(change_page, "send_to_staff") as send:
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        send.assert_called_once()
                    list_page.recover_pending()
                    list_page.recover_pending()
                    self.assertEqual(send.call_count, int(published))
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        self.assertEqual(send.call_count, 1)
                if published:
                    item = the_list.update(MORTY.id, item.id, {"latest_status": "Recorded"}, source="morty")

    def test_feedback_claim_releases_the_list_lock_during_delivery(self) -> None:
        root = self.root / "reviews"
        item = self.add("Send without blocking edits")
        other = self.add("Mark this item read")
        delivery = list_page.submit(root, urlencode(self.fields(item, "tell", words="Keep these words.")))
        entered, release = threading.Event(), threading.Event()
        def send(_recipient: staff.StaffId, _sender: str, _message: str) -> None:
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("the test did not release delivery")
        def edit_items() -> None:
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty edited during delivery"}, source="morty")
            _ = the_list.mark_read(MORTY.id, other.id, source=the_list.PAGE_SOURCE)
        with patch.object(change_page, "send_to_staff", side_effect=send) as sends, ThreadPoolExecutor(max_workers=2) as pool:
            sending = pool.submit(list_page.deliver, delivery)
            try:
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                list_page.deliver(delivery)
                edited = pool.submit(edit_items)
                edited.result(TEST_HANG_GUARD_SECS)
                self.assertIs(the_list.load(MORTY.id, other.id).unread_since, the_list.NoTimestamp.UNSET)
                self.assertEqual(sends.call_count, 1)
            finally:
                release.set()
            sending.result(TEST_HANG_GUARD_SECS)
        current = the_list.load(MORTY.id, item.id)
        self.assertEqual(current.latest_status, "Morty edited during delivery")
        self.assertEqual(tuple(the_list.feedback_requests(current).values())[-1].state, "sent")
        self.assertIs(the_list.sent_to_morty(current), the_list.NoTimestamp.UNSET)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_shared_page_status_uses_the_real_list_loader(self) -> None:
        _ = self.fixture_items()
        assert_status_states(self, list_page.render(self.root / "reviews").decode(), 15)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_sort_filters_hash_refresh_open_rows_and_phone_cards(self) -> None:
        _ = self.fixture_items()
        self.browser(list_page.render(self.root / "reviews").decode())

    def browser(self, markup: str) -> None:
        setup = r'''<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, errors=[], reads=[],posts=[];
let original=MARKUP,holdPost=false,finishPost,failRefresh=false,realFeedback=false,loseResponse=false;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.fetch=async(_path,options={})=>{
 if(options.method==='POST'){
  const fields=new URLSearchParams(options.body);posts.push(fields);
  if(realFeedback){
   const result=await nativeFetch('/feedback',options);
   if(loseResponse){loseResponse=false;throw new Error('fixture lost response');}
   return result;
  }
  if(fields.get('action')==='read'){
   const page=new DOMParser().parseFromString(original,'text/html');
   const row=[...page.querySelectorAll('[data-list-item]')].find(row=>row.dataset.number==='106');
   row.dataset.unread='false';row.querySelector('.list-dot')?.remove();row.querySelector('[value="read"]')?.remove();
   original=page.documentElement.outerHTML;
  }
  const result={ok:true,status:200,json:async()=>({state:'sent',unread:'false'})};
  if(holdPost) return await new Promise(resolve=>finishPost=()=>resolve(result));
  return result;
 }
 reads.push(1);if(failRefresh) throw new Error('fixture refresh failed');
 if(realFeedback) return await nativeFetch('/fresh');
 return {ok:true,status:200,text:async()=>original};
};
addEventListener('error',event=>errors.push(event.message));
addEventListener('unhandledrejection',event=>errors.push(String(event.reason)));
</script>'''
        probe = r'''<script>
(async()=>{
const failed=[],check=(ok,text)=>{if(!ok) failed.push(text);};
const flush=()=>new Promise(resolve=>nativeTimeout(resolve,50));
const submitted=(form,button)=>new Promise((resolve,reject)=>{
 if(form.dataset.sending){reject(new Error('The test submission did not start.'));return;}
 const observer=new MutationObserver(()=>{
  if(!form.dataset.sending){observer.disconnect();resolve();}
 });
 observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
 form.requestSubmit(button);
 if(!form.dataset.sending){observer.disconnect();reject(new Error('The test submission did not start.'));}
});
const until=async (ready,label)=>{
 const deadline=performance.now()+8000;
 while(!ready()){
  if(performance.now()>deadline) throw new Error('Timed out: '+label);
  await flush();
 }
};
const rows=()=>[...document.querySelectorAll('[data-list-item]')];
const visible=()=>rows().filter(row=>!row.hidden);
const chip=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
const show=value=>chip(value).click();
const selectedShow=()=>document.querySelector('[data-filter-row="show"] [aria-pressed="true"]').dataset.filterValue;
const menu=name=>document.getElementById('list-'+name);
const select=(name,value)=>{const field=menu(name);field.value=value;field.dispatchEvent(new Event('change'));};
const count=value=>Number(chip(value).querySelector('b').textContent);
const menuCount=(name,value)=>Number([...menu(name).options].find(option=>option.value===value).dataset.filterCount);
const unread=()=>document.getElementById('list-unread');
const bulk=action=>document.querySelector('.list-table-tools [data-list-expansion="'+action+'"]');
const search=value=>new Promise(done=>{
 const field=document.getElementById('list-search');
 field.addEventListener('filterchange',done,{once:true});field.value=value;field.dispatchEvent(new Event('input'));
});
try{
 await flush();
 const tools=document.querySelector('.list-table-tools'),table=tools.nextElementSibling;
 const countLine=tools.querySelector('[data-list-count]');
 const bounds=element=>element.getBoundingClientRect();
 const center=rect=>(rect.top+rect.bottom)/2;
 const near=(a,b)=>Math.abs(a-b)<1;
 const expand=bounds(bulk('expand')),collapse=bounds(bulk('collapse')),shown=bounds(countLine);
 check(table.matches('.list-table') && bounds(tools).bottom<=bounds(table).top,
       'The controls and count sit directly above the table.');
 check(near(expand.left,bounds(table).left) && near(shown.right,bounds(table).right) &&
       near(center(expand),center(collapse)) && near(center(expand),center(shown)) && expand.right<collapse.left,
       'The two buttons and the right-aligned count share one row.');
 check(countLine.textContent.startsWith('6 of 6 items shown') && countLine.getAttribute('role')==='status',
       'The count in the row reports the visible items.');
 for(const action of ['expand','collapse']){
  const button=bulk(action),box=bounds(button),icon=bounds(button.querySelector('svg'));
  const minimum=innerWidth<960 ? 32 : 24;
  check(box.width>=minimum && box.height>=minimum && box.width<36 && box.height<36 &&
        icon.width>=16 && icon.height>=16 && icon.left>box.left && icon.right<box.right &&
        icon.top>box.top && icon.bottom<box.bottom,'The small button fits its icon and has a hit area.');
  const hit=document.elementFromPoint(box.left+3,center(box));
  check(button.contains(hit),'The button accepts a click near its edge.');
 }
 check(expand.left>=0 && shown.right<=innerWidth && collapse.right<shown.left &&
       document.documentElement.scrollWidth<=innerWidth,'The row fits without clipping or horizontal overflow.');
 if(innerWidth>=960){
  check(bounds(document.querySelector('main')).width===Math.min(1124,document.documentElement.clientWidth),
        'The desktop uses the available width up to the shared limit.');
  check(collapse.right<=bounds(document.querySelector('[data-sort="0"]').closest('th')).right,
        'Both buttons sit above the number column.');
  for(const header of document.querySelectorAll('[data-sort]')){
   const label=document.createRange();label.selectNodeContents(header.firstChild);
   check(label.getClientRects().length===1,'the desktop header fits on one line: '+header.textContent);
  }
 }
 check(visible().map(row=>row.dataset.number).join(',')==='58,105,106,41,63,54','The default order in All.');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='41,54,58,63,105,106','the number sort is numeric');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='106,105,63,58,54,41','a second click reverses the sort');
 for(let column=1;column<8;column++){
  document.querySelector('[data-sort="'+column+'"]').click();
  check(document.querySelector('[data-sort="'+column+'"]').parentElement.getAttribute('aria-sort')==='ascending','sort column '+column);
 }
 show('needs-me');check(visible().length===1 && visible()[0].dataset.number==='58','Needs me');
 show('all');unread().click();
 check(visible().map(row=>row.dataset.number).sort().join() === '105,106,41,63','Unread excludes the closed item');
 unread().click();
 show('parked');check(visible().length===1 && visible()[0].dataset.number==='41','Parked');
 show('closed');
 check(visible().map(row=>row.dataset.number).sort().join()==='54,63','Closed includes Done and Closed.');
 show('all');check(visible().length===6,'All');
 show('working');
 const showRow=chip('working').closest('[data-filter-row]');
 const showValues=filterControl.buttons(showRow).map(button=>button.dataset.filterValue);
 filterControl.setVisible(showRow,showValues.filter(value=>value!=='working'));
 dispatchEvent(new PopStateEvent('popstate'));
 check(selectedShow()==='all' && visible().length===6,'Hiding Working selects All and shows all six items.');
 check(new URLSearchParams(location.hash.slice(1)).get('show')==='all','the address records the fallback to All');
 filterControl.setVisible(showRow,showValues);
 const theme=document.documentElement.dataset.theme;
 const pixel=document.createElement('canvas').getContext('2d',{willReadFrequently:true});
 const luminance=color=>{
  pixel.fillStyle=color;pixel.fillRect(0,0,1,1);
  const [r,g,b]=[...pixel.getImageData(0,0,1,1).data].slice(0,3).map(byte=>{
   const value=byte/255;
   return value<=.04045 ? value/12.92 : ((value+.055)/1.055)**2.4;
  });
  return .2126*r+.7152*g+.0722*b;
 };
 show('working');select('owner',OWNER);select('producer',MORTY);
 check(menu('owner').selectedOptions[0].textContent.startsWith('sdlc (') &&
  menu('producer').selectedOptions[0].textContent.startsWith('morty ('),'The menus show the selected names and counts.');
 for(const themeName of ['light','dark']){
  document.documentElement.dataset.theme=themeName;
  for(const view of ['working','blocked']){
   show(view);
   for(const control of [chip(view),menu('owner'),menu('producer')]){
    const background=luminance(getComputedStyle(control).backgroundColor);
    for(const element of [control,...control.querySelectorAll('b')]){
     const ink=luminance(getComputedStyle(element).color);
     const contrast=(Math.max(ink,background)+.05)/(Math.min(ink,background)+.05);
     check(getComputedStyle(control).opacity==='1' && contrast>=4.5,
      themeName+' contrast for '+control.textContent+': '+contrast);
    }
   }
  }
 }
 if(theme===undefined) delete document.documentElement.dataset.theme;
 else document.documentElement.dataset.theme=theme;
 select('producer','');select('owner','');show('all');
 select('owner',OWNER);check(visible().length===5,'Owner');
 select('owner','');
 select('producer',DWIGHT);check(visible().map(row=>row.dataset.number).join(',')==='105','Producer');
 check(new URLSearchParams(location.hash.slice(1)).get('producer')===DWIGHT,'the address keeps the producer');
 select('producer','');
 check(visible().length===6 && !new URLSearchParams(location.hash.slice(1)).has('producer'),'All producers');
 check(showRow.querySelector('button').dataset.filterValue==='all' &&
  menu('owner').options[0].value==='' && menu('producer').options[0].value==='','Each filter starts with All.');
 show('working');
 check(visible().length===2 && count('working')===2 && count('blocked')===0,
  'Working shows both matching records. Blocked counts zero.');
 const zero=chip('blocked'),zeroPosition=zero.getBoundingClientRect();
 check(!zero.hidden && !zero.disabled && Number(getComputedStyle(zero).opacity)<1 && zeroPosition.width>0,
  'a zero chip stays in the row, dimmed and enabled');
 show('blocked');
 check(visible().length===0 && count('working')===2,'Selecting a zero chip keeps the other status counts.');
 show('closed');
 check(visible().map(row=>row.dataset.number).sort().join()==='54,63','Closed replaces the Blocked selection.');
 show('working');select('owner',OWNER);select('producer',DWIGHT);
 check(visible().length===0 && menuCount('owner','')===1 && menuCount('producer','')===1 && count('all')===0,
  'Each count ignores only its own filter.');
 select('producer','');await search('console');
 check(visible().map(row=>row.dataset.number).join(',')==='106' && count('working')===1 &&
  menuCount('producer',DWIGHT)===0 && menuCount('owner','')===2,'The search, menus and status chip combine.');
 check(zero.getBoundingClientRect().left===zeroPosition.left,'a zero chip retains its place');
 await search('');show('all');select('owner','');
 await search('#106');
 check(visible().length===1 && visible()[0].dataset.number==='106','number search');
 await search('console');check(visible().length===2,'title search');
 check(new URLSearchParams(location.hash.slice(1)).get('q')==='console','the address keeps the query');
 for(const title of ['Straße','STRAẞE']){
  await search(title);
  check(visible().length===1 && visible()[0].dataset.number==='106','Unicode title search: '+title);
 }
 await search('console');
 const selected=visible().find(row=>row.dataset.number==='106');selected.querySelector('.list-toggle').click();
 check(!selected.querySelector('[data-detail]').hidden,'a row opens');
 const words=selected.querySelector('textarea'),tell=selected.querySelector('[value="tell"]');
 check(tell.textContent==='Tell morty','the button names its recipient');
 check(words.placeholder==='Your words for morty about this item' && words.getAttribute('aria-label')===words.placeholder,'the words box has its accessible name');
 check(!selected.querySelector('label[for="'+words.id+'"]'),'the words box has no repeated visible label');
 check(!tell.closest('.list-actions') && tell.parentElement.querySelector('[data-send-status]'),'the status stays beside the separate primary button');
 const wordsRect=words.getBoundingClientRect(),tellRect=tell.getBoundingClientRect();
 check(tellRect.top>=wordsRect.bottom && Math.abs(tellRect.right-wordsRect.right)<1,'the button sits below the box at its right edge');
 check(tell.disabled,'empty words disable Tell morty');
 words.value=' \t\n ';words.dispatchEvent(new Event('input'));
 check(tell.disabled,'spaces disable Tell morty');
 const emptyPosts=posts.length;tell.click();check(posts.length===emptyPosts,'empty words cannot send');
 words.value='x';words.dispatchEvent(new Event('input'));check(!tell.disabled,'typed text enables Tell morty immediately');
 words.value='';words.dispatchEvent(new Event('input'));check(tell.disabled,'cleared words disable Tell morty immediately');
 words.value='Keep my draft';words.dispatchEvent(new Event('input'));
 const field=document.querySelector('#list-search');field.focus();field.setSelectionRange(1,3);
 document.body.style.minHeight='2400px';scrollTo(0,100);
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
 const refreshed=rows().find(row=>row.dataset.number==='106');
 check(reads.length===1,'the shared status requests a fresh page');
 check(document.querySelector('#list-search').value==='console','refresh keeps the query');
 check(document.activeElement.id==='list-search','refresh keeps focus');
 check(document.activeElement.selectionStart===1 && document.activeElement.selectionEnd===3,'refresh keeps the selection');
 check(!refreshed.querySelector('[data-detail]').hidden,'refresh keeps open rows');
 check(refreshed.querySelector('textarea').value==='Keep my draft','refresh keeps feedback words');
 check(!refreshed.querySelector('[value="tell"]').disabled,'a restored draft enables Tell morty');
 check(scrollY===100,'refresh keeps scroll');
 check(refreshed!==selected,'refresh replaces the row');
 bulk('expand').click();
 check(visible().length===2 && visible().every(row=>!row.querySelector('[data-detail]').hidden),
  'Expand all opens both matching rows after refresh');
 bulk('collapse').click();
 check(visible().length===2 && visible().every(row=>row.querySelector('[data-detail]').hidden),
  'Collapse all closes both matching rows after refresh');
 refreshed.querySelector('.list-toggle').click();
 realFeedback=true;loseResponse=true;
 const tellRow=rows().find(row=>row.dataset.number==='105');tellRow.querySelector('.list-toggle').click();
 check(tellRow.dataset.unread==='true' && tellRow.querySelector('[value="read"]'),'Tell starts with a fresh unread row');
 let retryForm=tellRow.querySelector('form');
 retryForm.elements.words.value='Deliver this feedback once';retryForm.elements.words.dispatchEvent(new Event('input'));
 const retryId=retryForm.elements.request.value,retryToken=retryForm.elements.token.value,startPosts=posts.length;
 await submitted(retryForm,retryForm.querySelector('[value="tell"]'));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();await flush();
 retryForm=rows().find(row=>row.dataset.number==='105').querySelector('form');
 check(retryForm.elements.request.value===retryId && retryForm.elements.token.value===retryToken,'refresh keeps the failed request identity');
 await submitted(retryForm,retryForm.querySelector('[value="tell"]'));
 check(posts.length===startPosts+2,'the lost response has one retry');
 check(posts.at(-1).get('request')===retryId && posts.at(-1).get('token')===retryToken,'the retry uses its original signature');
 retryForm=rows().find(row=>row.dataset.number==='105').querySelector('form');
 check(retryForm.elements.request.value!==retryId,'success clears the saved retry identity');
 check(retryForm.elements.words.value==='' && retryForm.querySelector('[value="tell"]').disabled,'a successful send clears the box and disables Tell morty');
 realFeedback=false;failRefresh=true;
 const requestIds=[];
 for(const action of ['read','remove','park','tell']){
  const row=rows().find(row=>row.dataset.number==='106'),form=row.querySelector('form');
  form.elements.until.value='2026-12-03';form.elements.words.value='Explain this simply';
  requestIds.push(form.elements.request.value);
  holdPost=action==='tell';
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  if(holdPost){await until(()=>typeof finishPost==='function','the held response');
   form.elements.words.value='New draft during send';form.elements.words.dispatchEvent(new Event('input'));
   check(form.querySelector('[value="tell"]').disabled,'editing words during delivery keeps Tell morty disabled');finishPost();}
  check(form.querySelector('[data-send-status]').textContent===(action==='read' ? 'Saving…' : 'Sending…'),'immediate response '+action);
  await until(()=>!form.dataset.sending,'the '+action+' response');
  check(posts.at(-1).get('action')===action && posts.at(-1).get('id')==='L106','signed feedback '+action);
  check(posts.at(-1).get('token').length===64,'the form carries its token');
  check(form.elements.request.value!==requestIds.at(-1),'success renews the request without refresh');
  if(action==='tell'){
   check(form.elements.words.value==='New draft during send','a newer draft survives the send');
   check(!form.querySelector('[value="tell"]').disabled,'the newer draft enables Tell morty after delivery');
  }
  check(!rows().find(row=>row.dataset.number==='106').querySelector('[data-detail]').hidden,'feedback keeps the row open');
 }
 check(new Set(requestIds).size===4,'each new action uses a fresh request');
 realFeedback=true;failRefresh=false;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();await flush();await flush();await flush();
 realFeedback=false;failRefresh=true;
 location.hash=new URLSearchParams({show:'closed',sort:'#',dir:'desc'});await flush();
 check(visible().map(row=>row.dataset.number).join(',')==='63,54','Hash navigation restores Closed and the sort order.');
 location.hash=new URLSearchParams({show:'done'});await flush();
 check(selectedShow()==='closed' && visible().map(row=>row.dataset.number).sort().join()==='54,63',
  'An old Done address opens Closed.');
 location.hash=new URLSearchParams({show:'all',producer:DWIGHT});await flush();
 check(menu('producer').value===DWIGHT && visible().map(row=>row.dataset.number).join(',')==='105',
   'an address restores the producer filter');
 location.hash=new URLSearchParams({show:'all',producer:'missing'});await flush();
 check(menu('producer').value==='' && visible().length===6,'an unknown producer shows all items');
 location.hash=new URLSearchParams({show:'unread',state:'Working',owner:OWNER,q:'106'});await flush();
 check(selectedShow()==='working' && menu('owner').value===OWNER && unread().checked &&
  visible().map(row=>row.dataset.number).join(',')==='106','An old address restores the Working chip, switch, menu and search.');

 for(const target of rows().filter(row=>['Done','Closed'].includes(row.dataset.state) || row.dataset.parked==='true')){
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  check(!target.hidden && !target.querySelector('[data-detail]').hidden,'an item link shows '+target.dataset.state);
  check(selectedShow()==='all' && menu('owner').value==='' && !unread().checked &&
    menu('producer').value==='' && document.getElementById('list-search').value==='','an item link clears filters');
 }
 const retained=rows().find(row=>row.dataset.number==='106');
 location.hash=new URLSearchParams({item:retained.dataset.key,owner:retained.dataset.owner,show:'working',q:'106',sort:'#',dir:'desc'});await flush();
 check(menu('owner').value===retained.dataset.owner && selectedShow()==='working' &&
   document.getElementById('list-search').value==='106','an address with an open item keeps its filters');
 check(!retained.hidden && !retained.querySelector('[data-detail]').hidden,'an address restores its open item');
 check(document.querySelectorAll('[data-feedback-form]').length===6,'each row has feedback controls');
 check(document.documentElement.scrollWidth<=innerWidth,'the page has no horizontal overflow');
 if(innerWidth<960){
  check(getComputedStyle(visible()[0]).display==='block','a phone row is a card');
  const card=number=>rows().find(row=>row.dataset.number===number);
  const cell=(number,label,part=null)=>getComputedStyle(card(number).querySelector('td[data-label="'+label+'"]'),part);
  check(cell('105','Item').fontSize==='17px' && cell('105','Producer').fontSize!=='17px','a phone card shows the title largest');
  check(cell('105','Producer','::before').content.includes('Producer'),'a phone card labels another producer');
  check(cell('106','Producer').display==='none','a phone card leaves out the keeper as producer');
 }
}catch(error){failed.push(String(error));}
await nativeFetch('/results',{method:'POST',body:JSON.stringify([...failed,...errors])});
})();</script>'''
        for name, identity in (("OWNER", OWNER.id), ("DWIGHT", DWIGHT.id), ("MORTY", MORTY.id)):
            probe = probe.replace(name, json.dumps(identity))
        for width in (1440, 961, 390):
            with self.subTest(width=width):
                for identity in ("L105", "L106"):
                    _ = the_list.update(MORTY.id, identity,
                        {"latest_status": f"The fixture uses a width of {width} pixels."}, source="morty")
                markup = list_page.render(self.root / "reviews").decode()
                setup_for_width = setup.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
                page = markup.replace('<head>', '<head>' + setup_for_width, 1).replace('</body>', probe + '</body>').encode()
                root = self.root / "reviews"
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/fresh':
                            _ = self.wfile.write(list_page.render(root).decode().encode())
                        elif self.path == '/complete':
                            _ = complete.wait(40)
                        else:
                            _ = self.wfile.write(page if self.path == '/page' else b'<iframe style="border:0;width:100vw;height:100vh" src="/page"></iframe><img hidden src="/complete">')

                    def do_POST(self) -> None:
                        if self.path == '/feedback':
                            body = self.rfile.read(int(self.headers['Content-Length'])).decode()
                            delivery = list_page.submit(root, body)
                            list_page.deliver(delivery)
                            fields = parse_qs(body)
                            result = {"state": "sent", **list_page.saved_action_state(fields["keeper"][0], fields["id"][0])}
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            _ = self.wfile.write(json.dumps(result).encode())
                            return
                        results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                        complete.set()
                        self.send_response(204)
                        self.end_headers()

                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass

                with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        profile = self.root / f'profile-{width}'
                        profile.mkdir()
                        with patch.object(change_page, "send_to_staff") as sends:
                            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                                '--window-size', f'{width},900', '--screenshot', str(self.root / f'page-{width}.png'),
                                f'http://127.0.0.1:{server.server_port}/'], capture_output=True, text=True, timeout=45)
                            self.assertEqual(done.returncode, 0, done.stderr)
                            self.assertEqual(results, [[]])
                            sends.assert_called_once()
                    finally:
                        server.shutdown()
                        thread.join(5)


if __name__ == '__main__':
    _ = unittest.main()
