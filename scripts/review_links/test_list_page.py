"""Test The List page with private records and a real browser."""
from __future__ import annotations

import html
import io
import json
import os
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
from typing import cast, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from scripts.accounts import accounts
from scripts.agents.remote import local_machine
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import change_review, change_routing, delivery_state, investigation, session_staff, showrunners, staff, the_list
from scripts.production.update_registration import QuestionStaff
from scripts.review_links import console, list_page, staff_names
from scripts.review_links import change_browser, change_page, question_record
from scripts.review_links import review_items, review_page
from scripts.review_links.test_change_page import BrowserForms
from scripts.review_links.test_page_frame import assert_status_states
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

    def test_review_links_keep_the_console_and_select_exact_cards_in_every_state(self) -> None:
        source = review_items.ProposalLedgerSource(self.root / "proposals.md")
        ready = review_items.ReviewItem("proposal/sdlc/P10", "sdlc/P10", review_items.ReviewType.PROPOSAL,
            OWNER.id, OWNER.id, "Check this proposal", review_items.ReviewState.FOR_YOU,
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
                self.assertEqual(parse_qs(place.fragment), {"key": [review.key], "item": [review.key]})
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

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_review_links_open_exact_cards_inside_the_console(self) -> None:
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1830", "How to check": "Read the card."},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        ready = review_items.ReviewItem("change/sdlc/C1830", "sdlc/C1830", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check the linked change", review_items.ReviewState.FOR_YOU,
            review_items.ReviewTimes(NOW, NOW), (), (), source)
        reviews = [ready, replace(ready, key="change/hana/P10", id="hana/P10", state=review_items.ReviewState.DONE),
                   replace(ready, key="change/sdlc/C1831", id="sdlc/C1831", state=review_items.ReviewState.WAITING),
                   replace(ready, key="change/sdlc/C1832", id="sdlc/C1832",
                           times=review_items.ReviewTimes(NOW, NOW, NOW + timedelta(days=1)))]
        item = self.add("Linked review cards", number=111)
        _ = the_list.link(MORTY.id, item.id, tuple(review.key for review in reviews), source="morty")
        reviews.append(replace(ready, key="change/other/C9", id="other/C9", title="Unlinked review"))
        with patch.object(review_items, "read", return_value=reviews), patch.object(list_page, "counts_strip", return_value=""):
            listing = list_page.render(self.root / "reviews")
            review = review_page.render(self.root / "reviews", show="all")
        pages = (console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage(list_page.PAGE_PATH)),
                 console.ConsolePage("review", "Review", "inbox", "Review", console.AvailablePage(review_page.PAGE_PATH)))
        shell = console.render(pages, console.UnavailablePage()).decode()
        list_url = "/console/" + console.place_query("list", list_page.PAGE_PATH + '#' + urlencode({"item": f"{MORTY.id}/{item.id}"}))
        probe = r'''<script>
const listUrl=LIST_URL,keys=KEYS,proof=new URLSearchParams(location.search).get('proof');
let finished=false;
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
async function inspect(frame){
 try{
  const doc=frame.contentDocument;
  if(!doc?.querySelector('[data-list-item]')) return;
  check(!!document.querySelector('.sidebar .nav-entry'),'the console navigator remains visible');
  const stage=proof==='review' ? keys.length : Number(sessionStorage.getItem('link-stage') || 0);
  if(doc.querySelector('[data-list]')){
   ids(doc);
   const row=doc.querySelector('[data-number="111"]');
   check(!row.hidden && !row.querySelector('[data-detail]').hidden,'List item 111 is open');
   if(proof==='list'){await finish();return;}
   const links=[...row.querySelector('[data-detail]').querySelectorAll('a')];
   const link=stage<keys.length ? links.find(link=>{
    const at=new URL(link.href).searchParams.get('at.review');
    return at && new URLSearchParams(new URL(at,location.href).hash.slice(1)).get('item')===keys[stage];
   }) : row.querySelector('[data-label="State"] a');
   check(!!link,'the selected List link exists');
   if(link) link.click();else await finish();
  }else if(doc.querySelector('[data-review]')){
   ids(doc);
   const visible=[...doc.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
   if(stage<keys.length){
    check(visible.length===1 && visible[0].dataset.key===keys[stage],'the exact linked card is shown');
    check(visible[0]?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true','the linked card is expanded');
    check(!visible[0]?.querySelector('[data-detail]').hidden,'the linked card detail is open');
    sessionStorage.setItem('link-stage',String(stage+1));location.href=listUrl;
   }else{
    check(visible.length===keys.length && visible.every(row=>keys.includes(row.dataset.key)),'the summary shows only linked cards');
    const done=visible.find(row=>row.dataset.state==='Done');
    if(done?.querySelector('.list-toggle').getAttribute('aria-expanded')!=='true') done?.querySelector('.list-toggle').click();
    check(done?.querySelector('.list-toggle').getAttribute('aria-expanded')==='true','the Done card can open in the linked view');
    await finish();
   }
  }
 }catch(error){failures.push(String(error));await finish();}
}
for(const frame of document.querySelectorAll('iframe')) frame.addEventListener('load',()=>inspect(frame));
</script><img hidden src="/complete">'''.replace('LIST_URL', json.dumps(list_url)).replace(
            'KEYS', json.dumps([entry.key for entry in reviews[:-1]]))
        shell = shell.replace('</body>', probe + '</body>')
        output = Path(os.environ.get("LISTLINKS_PROOF_DIR", str(self.root)))
        output.mkdir(parents=True, exist_ok=True)
        for shot in ("clicks", "review", "list"):
            with self.subTest(shot=shot):
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        path = urlsplit(self.path).path
                        if path == '/complete':
                            _ = complete.wait(30)
                        body = (listing if path == list_page.PAGE_PATH else review if path == review_page.PAGE_PATH
                                else b'{}' if path.endswith('.json') else shell.encode())
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json' if path.endswith('.json') else 'text/html; charset=utf-8')
                        self.end_headers()
                        try:
                            _ = self.wfile.write(body)
                        except BrokenPipeError:
                            pass

                    def do_POST(self) -> None:
                        if self.path == '/results':
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
                        profile = self.root / f'links-profile-{shot}'
                        profile.mkdir()
                        _ = (profile / 'user.js').write_text('user_pref("ui.systemUsesDarkTheme", 1);\n')
                        destination = list_url
                        if shot == 'review':
                            destination = '/console/' + console.place_query('review', review_page.PAGE_PATH + '?show=all#'
                                + urlencode([('key', entry.key) for entry in reviews[:-1]] + [('item', reviews[1].key)]))
                        url = f'http://127.0.0.1:{server.server_port}' + destination
                        command = ['firefox', '--headless', '--no-remote', '--profile', str(profile), '--window-size', '1440,1100']
                        if shot == 'clicks':
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

    def test_staff_owner_and_producer_names_have_role_colors_but_nate_stays_plain(self) -> None:
        _ = self.add("Staff names")
        _ = self.add("User name", owner=the_list.UnassignedOwner.NATE)
        body = list_page.render(self.root / "reviews").decode()
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(body, r'data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn('<td data-label="Owner">nate</td>', body)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="list-page-test-")))
        config = self.root / "config"
        config.mkdir()
        for name in ("review_links", "console", "change_review"):
            _ = shutil.copyfile(Path(__file__).resolve().parents[2] / f"config/{name}.conf", config / f"{name}.conf")
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
        body = without_role_colors(list_page.render(self.root / "reviews").decode())
        self.assertEqual(body.count('<tbody data-list-item'), 6)
        for label in list_page.HEADERS:
            self.assertIn(label + '<span data-sort-mark', body)
        for value, label in list_page.SHOWS:
            self.assertIn(f'<option value="{value}">{label}</option>', body)
        self.assertIn('<option value="' + OWNER.id + '">sdlc</option>', body)
        self.assertIn('<option value="' + OTHER.id + '">builds</option>', body)
        producers = body.split('<select id="list-producer">', 1)[1].split('</select>', 1)[0]
        self.assertEqual(producers, '<option value="">All</option><option value="' + DWIGHT.id + '">dwight</option>'
                         + '<option value="' + MORTY.id + '">morty</option>')
        self.assertEqual(body.count('<td data-label="Producer">dwight</td>'), 1)
        self.assertEqual(body.count('<td data-label="Producer">morty</td>'), 5)
        self.assertIn(f'data-producer="{DWIGHT.id}"', body)
        for text in ('Latest status', 'Done when', 'Log · newest first', 'Links', 'Closing words',
                     'Check overdue', '14:00 EST', '15:00 EST'):
            self.assertIn(text, body)
        self.assertNotIn('<h3>Evidence</h3>', body)
        self.assertEqual(the_list.load(MORTY.id, items[0].id).evidence, items[0].evidence)
        self.assertEqual(body.count('<form method="post" data-feedback-form>'), 6)
        self.assertEqual(body.count('value="read">Mark read</button>'), 6)
        self.assertEqual([entry.item.number for entry in list_page.records()],
                         [item.number for item in sorted(the_list.records(MORTY.id, include_closed=True), key=the_list.order)])
        closed = body.split(f'data-key="{MORTY.id}/L54"', 1)[1].split('</tbody>', 1)[0]
        self.assertNotIn('Check overdue', closed)
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

        ready = (change("C1", review_items.ReviewState.FOR_YOU), change("C2", review_items.ReviewState.FOR_YOU))
        waiting = change("C3", review_items.ReviewState.WAITING)
        done = change("C4", review_items.ReviewState.DONE)
        later = replace(change("C5", review_items.ReviewState.FOR_YOU),
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
                place = review_page.PAGE_PATH + '?show=all#' + urlencode(
                    [('key', key) for key in identities] + ([('item', identities[0])] if len(identities) == 1 else []))
                href = html.escape('/console/' + console.place_query('review', place), quote=True)
                self.assertIn(f'href="{href}" target="_top">{expected}</a>', state)
                detail = row.split('<div class="list-panel"><div>', 1)[1]
                self.assertLess(detail.index(expected), detail.index('<form'))
                positions: list[int] = []
                for identity in identities:
                    matching = [review for review in reviews if review.key == identity]
                    if matching:
                        place = review_page.PAGE_PATH + '?show=all#' + urlencode({'key': identity, 'item': identity})
                        href = html.escape('/console/' + console.place_query('review', place), quote=True)
                        card = f'<a href="{href}" target="_top">{html.escape(matching[0].id)}</a>'
                    else:
                        card = html.escape(identity)
                        self.assertNotIn('#' + urlencode({"item": identity}), detail)
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
            OWNER.id, OWNER.id, "Check C1", review_items.ReviewState.FOR_YOU, review_items.ReviewTimes(NOW, NOW), (), (), source)
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
                place = review_page.PAGE_PATH + '?show=all#' + urlencode({'key': review.key, 'item': review.key})
                card = '/console/' + console.place_query('review', place)
                if review.type in review_page.TYPES:
                    self.assertIn(f'<a href="{html.escape(card, quote=True)}" target="_top">{review.id}</a>', row)
                else:
                    self.assertNotIn(card, row)
                    self.assertIn('<p>' + review.id + '</p>', row)

    def test_unknown_owners_and_unassigned_owners_keep_their_labels(self) -> None:
        missing = staff.StaffId("owner-without-row")
        for owner in (missing, the_list.UnassignedOwner.NATE, the_list.UnassignedOwner.NOT_RECORDED):
            _ = self.add("Work for " + owner, owner=owner)
        body = list_page.render(self.root / "reviews").decode()
        for label in (str(missing), "nate", "Not recorded"):
            self.assertIn('>' + label + '</option>', body)
        _ = (self.root / "staff.json").write_text('{"schema":1,"rows":[]}')
        self.assertEqual(len(list_page.records()), 3)

    def test_feedback_button_belongs_to_the_words_box_and_starts_disabled(self) -> None:
        item = self.add("Explain this item")
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), "fixture-secret")
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
        details = body.split('<div class="list-panel"><div>', 1)[1].split('<section><h3>Log · newest first', 1)[0]
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
        request = uuid4().hex
        return {"keeper": MORTY.id, "id": item.id, "request": request,
                "token": list_page.request_token(self.root / "reviews", MORTY.id, item.id, request),
                "action": action, "words": "", "until": "", **changes}

    def test_remove_closes_before_delivery_and_repeated_requests_do_nothing(self) -> None:
        item = self.add("Remove this item now")
        fields = self.fields(item, "remove")
        root = self.root / "reviews"
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            closed = the_list.load(MORTY.id, item.id)
            self.assertEqual(closed.state, the_list.ItemState.CLOSED)
            self.assertEqual(sum(entry.what.startswith("Closed:") for entry in closed.log), 1)
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

        read = {**park, "action": "read", "request": park["request"] + "." + uuid4().hex}
        _ = list_page.submit(root, urlencode(read))
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
  json:async()=>({request:String(requests.length).padStart(32,'0'),token:'b'.repeat(64),unread:'false',...result})}));
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const row=number=>document.querySelector('[data-number="'+number+'"]');
 const change=(id,value,event='change')=>{const field=document.getElementById(id);field.value=value;field.dispatchEvent(new Event(event));};
 const visible=()=>[...document.querySelectorAll('[data-list-item]')].filter(item=>!item.hidden).map(item=>item.dataset.number).sort().join(',');
 const choose=(show,query)=>{change('list-show',show);change('list-search',query,'input');};
 const submit=(number,action)=>{const form=row(number).querySelector('form');form.requestSubmit(form.querySelector('[value="'+action+'"]'));return form;};
 try{
  for(const [query,expected] of [['105','105'],['L105','105'],['#105','105'],['BUILDS','105'],
    ['Straße','106'],['WORKING','105,106'],['Waiting','58'],['Parked','41'],['Closed','54']]){
   choose('all',query);check(visible()===expected,'words filter: '+query);
  }
  choose('open','106');row('106').querySelector('.list-toggle').click();
  const removed=submit('106','remove');
  check(row('106').hidden,'Remove hides the row before the response');
  check([...row('106').querySelectorAll('button')].every(button=>button.disabled),'Remove disables the row buttons');
  submit('106','remove');check(requests.length===1,'a second Remove press sends nothing');
  finishPost({item_state:'Closed',parked:'false'});await flush();await flush();
  check(row('106').hidden,'Remove stays outside Open after success');
  choose('closed','106');check(visible()==='106','Remove updates the Closed view');
  check(row('106').querySelector('.list-state').textContent==='Closed','Remove updates the state text');
  choose('open','105');row('105').querySelector('.list-toggle').click();
  const refused=submit('105','remove');check(row('105').hidden,'a refused Remove hides the row while pending');
  finishPost({ok:false,reason:'The fixture refuses this close.'});await flush();await flush();
  check(!row('105').hidden && row('105').dataset.state==='Working','a refused Remove restores the row');
  check(refused.querySelector('[data-send-status]').textContent==='Not saved: The fixture refuses this close.','a refused Remove shows its reason');
  check(!refused.querySelector('[value="remove"]').disabled,'a refused Remove enables another attempt');
  refused.elements.until.value='2026-12-03';const parked=submit('105','park');
  check(row('105').hidden,'Park hides the row before the response');
  check(document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
   'Park animates the row when it leaves Open');
  submit('105','park');check(requests.length===3,'a second Park press sends nothing');
  finishPost({item_state:'Working',parked:'true'});await flush();await flush();
  check(row('105').hidden,'Park stays outside Open after success');
  choose('parked','105');check(visible()==='105','Park updates the Parked view');
  check(parked.querySelector('[data-send-status]').textContent==='Parked','Park shows its result');
  choose('all','58');row('58').querySelector('.list-toggle').click();
  const form=row('58').querySelector('form');
  check(row('58').dataset.unread==='false' && !form.querySelector('[value="read"]'),'the unread sync case starts read');
  form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
  submit('58','tell');finishPost({item_state:'Waiting for nate',parked:'false',unread:'true'});await flush();await flush();
  check(row('58').dataset.unread==='true' && row('58').querySelector('.list-dot'),'an unread response restores the dot');
  check(Boolean(form.querySelector('[value="read"]')),'an unread response restores Mark read');
  if(form.querySelector('[value="read"]')){
   choose('unread','58');
   submit('58','read');finishPost({item_state:'Waiting for nate',parked:'false',unread:'false'});await flush();await flush();
   check(!row('58').querySelector('.list-dot') && !form.querySelector('[value="read"]'),'the restored Mark read button works');
   check(row('58').hidden && document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
    'Mark read animates the row when it leaves Unread');
  }
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe)

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
   json:async()=>({request:'a'.repeat(32),token:'b'.repeat(64),unread:'true',...result})});
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
  check(labels().join() === 'Parked until 2026-11-18 13:00 EST','starts with one saved date label');
  form.elements.until.value='2026-12-03';
  const park=submit('park');losePost();await flush();
  check(submit('park')===park,'an exact Park retry keeps its request ID');
  losePost();await flush();
  check(submit('remove')!==park,'Remove gets a different ID after a lost Park response');
  finishPost({reason:'Fixture refuses Remove'});await flush();
  form.elements.until.value='2026-12-04';
  check(submit('park')!==park,'a different Park date gets a different ID');
  finishPost({item_state:'Working',parked:'true',parked_until:'2026-12-05 00:00 EST'});await flush();
  check(labels().length===1,'a successful Park keeps one date label');
  check(labels().join()==='Parked until 2026-12-05 00:00 EST','the label uses the saved date from the response');
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe)

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
 finishPost({item_state:'Closed',parked:'true'});await flush();
 for(const [date,original] of [['2026-12-03',first],['2026-12-04',second]]){
  form.elements.until.value=date;
  const retry=submit('park');
  check(retry.id===original.id && retry.token===original.token,'Park retains the signed ID for '+date);
  finishPost({item_state:'Closed',parked:'true'});await flush();
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
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(!hiddenStatus.hidden && hiddenStatus.textContent==='Item 1: Save not confirmed: Fixture lost response',
  'the table shows the lost response while the details are closed');
 row.querySelector('.list-toggle').click();
 check(hiddenStatus.hidden && form.querySelector('[data-send-status]').getClientRects().length>0,
  'opening the details shows the inline notice only');
 row.querySelector('.list-toggle').click();
 check(!hiddenStatus.hidden,'closing the details restores the notice above the table');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_a_separate_notice_for_each_hidden_remove(self) -> None:
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
 check(shows(1) && shows(2),'both hidden notices appear above the table');
 row.querySelector('.list-toggle').click();
 check(!shows(1) && shows(2),'opening row one removes only its banner notice');
 check(form.querySelector('[data-send-status]').textContent==='Save not confirmed: Fixture lost response',
  'row one shows its notice inline');
 second.querySelector('.list-toggle').click();
 check(status.hidden,'opening both rows hides the banner');
 row.querySelector('.list-toggle').click();
 check(shows(1) && !shows(2),'closing row one restores only its banner notice');
''')

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

    def test_saved_question_and_answer_appear_on_the_item_and_the_answer_is_unread(self) -> None:
        root = self.root / "reviews"
        item = self.add("Inspect the List", number=106)
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        _ = list_page.submit(root, urlencode(self.fields(item, "question", words="What changed in <List>?")))
        opened = list_page.render(root).decode()
        self.assertIn("Question Q1 — waiting for an answer", opened)
        self.assertIn("What changed in &lt;List&gt;?", opened)
        self.assertNotIn('aria-label="Unread update"', opened)
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": MORTY.current_session}),
              redirect_stdout(io.StringIO())):
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The <filter> changed."]), 0)
        answered = list_page.render(root).decode()
        self.assertNotIn("<h3>Question Q1 — waiting for an answer</h3>", answered)
        self.assertIn("<h3>Answer</h3><p>The &lt;filter&gt; changed.</p>", answered)
        self.assertIn('aria-label="Unread update"', answered)
        self.assertIn('value="read">Mark read</button>', answered)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_open_row_and_shows_the_refusal_after_a_lost_remove(self) -> None:
        self.browser_action_sequence('''
 const remove=submit('remove');
 check(row.hidden,'Remove immediately hides the row in Open');
 losePost();await flush();
 show('closed');
 check(!row.hidden,'the unconfirmed row appears in Closed');
 check(submit('remove').id===remove.id,'the exact Remove retry uses the same ID');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && hiddenStatus.getClientRects().length>0 &&
  hiddenStatus.textContent==='Item 1: Not saved: Fixture refuses Remove',
  'the refusal is visible when the Closed filter hides the restored row');
 show('open');
 check(!row.hidden && row.dataset.state==='Working','the refusal restores the confirmed row to Open');
 const notice=form.querySelector('[data-send-status]');
 check(notice.getClientRects().length>0 && notice.textContent==='Not saved: Fixture refuses Remove',
  'the refusal is visible on the restored row');
 check(hiddenStatus.hidden,'the page hides the extra notice when the restored row is visible');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_collapses_and_refusal_restores_the_row(self) -> None:
        _ = self.add("Next item", number=2)
        self.browser_action_sequence(r"""
 document.querySelector('[data-sort="0"]').click();
 const next=document.querySelector('[data-number="2"]');
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
 finishPost({item_state:'Closed'});await flush();
 show('closed');
 check(!row.hidden && !motion(),'a filter change shows the row at once');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_respects_reduced_motion(self) -> None:
        _ = self.add("Check the motion preference", number=1)
        for reduced_motion in (False, True):
            with self.subTest(reduced_motion=reduced_motion):
                self.browser_action_sequence(r"""
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
 show('unread');
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

    def browser_action_sequence(self, steps: str, *, reduced_motion: bool = False, add_item: bool = True) -> None:
        if add_item:
            _ = self.add("Test List action retries", number=1)
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
const requests=[];let finishPost,losePost;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 requests.push(new URLSearchParams(options.body));
 return new Promise((resolve,reject)=>{
  losePost=()=>reject(new Error('Fixture lost response'));
  finishPost=result=>resolve({ok:!result.reason,status:result.status ?? (result.reason ? 400 : 200),
   json:async()=>({request:String(requests.length).padStart(32,'0'),token:'b'.repeat(64),unread:'true',...result})});
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
 const show=value=>{
  const filter=document.getElementById('list-show');
  filter.value=value;filter.dispatchEvent(new Event('change'));
 };
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 try{
  row.querySelector('.list-toggle').click();
''' + steps + '''
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe, reduced_motion=reduced_motion)

    def browser_actions(self, markup: str, setup: str, probe: str, *, reduced_motion: bool = False) -> None:
        page = self.root / 'actions.html'
        _ = page.write_text(markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>'))
        result = subprocess.run(['google-chrome', '--headless', '--no-sandbox', '--disable-gpu',
            *(['--force-prefers-reduced-motion'] if reduced_motion else []),
            '--user-data-dir=' + str(self.root / 'chrome-actions'), '--virtual-time-budget=1500',
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
            self.assertEqual(item.log[-1].what, "Marked read")
            for action, extra, words in (
                    ("park", {"until": "2026-12-03"}, "List page: nate parked item 106 until 2026-12-03."),
                    ("tell", {"words": "Explain this simply.\nKeep these words."}, "Explain this simply.\nKeep these words."),
                    ("remove", {}, "List page: nate removed item 106.")):
                with self.subTest(action=action):
                    fields = self.fields(item, action, **extra)
                    delivery = list_page.submit(root, urlencode(fields))
                    item = the_list.load(MORTY.id, item.id)
                    self.assertEqual(the_list.feedback_requests(item)[fields["request"]].words, words)
                    if action == "tell":
                        self.assertEqual(item.log[-1].what, "Marked read")
                    self.assertIn(json.dumps(words), item.log[-2 if action == "tell" else -1].what)
                    self.assertEqual(the_list.sent_to_morty(item), NOW)
                    body = list_page.render(root).decode()
                    self.assertIn("sent to morty, 13:00 EST", without_role_colors(body))
                    self.assertIn('data-feedback-form', body)
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
        self.assertEqual([(entry.what, entry.source) for entry in saved.log if entry.what == "Marked read"],
                         [("Marked read", the_list.PAGE_SOURCE)])
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    def test_tell_on_a_read_item_adds_no_read_log_entry(self) -> None:
        item = self.add("An update already read")
        item = the_list.mark_read(MORTY.id, item.id, source="nate")
        before = tuple(entry for entry in item.log if entry.what == "Marked read")
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            _ = list_page.submit(self.root / "reviews", urlencode(fields))
        mark.assert_not_called()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(tuple(entry for entry in saved.log if entry.what == "Marked read"), before)
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
        self.assertFalse(any(entry.what == "Marked read" for entry in saved.log))
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")
        self.assertEqual(list_page.fresh_request(root, MORTY.id, item.id)["unread"], "true")
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
        self.assertEqual(sum(entry.what == "Marked read" for entry in saved.log), 1)

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
        self.assertFalse(any(entry.what == "Marked read" for entry in saved.log))
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_read_and_tell_clear_the_unread_controls_without_a_refresh(self) -> None:
        _ = self.add("Check the update")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 return {ok:true,json:async()=>({unread:'UNREAD',request:'a'.repeat(32),token:'b'.repeat(64)})};
};
</script>'''
        probe = '''<script>
(async()=>{
 const row=document.querySelector('[data-list-item]'),form=row.querySelector('form');
 const show=document.getElementById('list-show');show.value='unread';show.dispatchEvent(new Event('change'));
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
        for action, unread, notice in (("read", "false", "Marked read"), ("tell", "false", "sent to morty"),
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
            _ = the_list.mark_read(MORTY.id, item.id, source="morty")
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
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
  const result={ok:true,status:200,json:async()=>({state:'sent',unread:'false',request:String(posts.length).padStart(32,'0'),token:'b'.repeat(64)})};
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
const rows=()=>[...document.querySelectorAll('[data-list-item]')];
const visible=()=>rows().filter(row=>!row.hidden);
const change=(id,value,event='change')=>{const field=document.getElementById(id);field.value=value;field.dispatchEvent(new Event(event,{bubbles:true}));};
try{
 await flush();
 if(innerWidth>=960){
  check(document.querySelector('main').getBoundingClientRect().width===1124,'the desktop uses the shared column width');
  for(const header of document.querySelectorAll('[data-sort]')){
   const label=document.createRange();label.selectNodeContents(header.firstChild);
   check(label.getClientRects().length===1,'the desktop header fits on one line: '+header.textContent);
  }
 }
 check(visible().map(row=>row.dataset.number).join(',')==='58,105,106','the default order and Open filter');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='58,105,106','the number sort is numeric');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='106,105,58','a second click reverses the sort');
 for(let column=1;column<8;column++){
  document.querySelector('[data-sort="'+column+'"]').click();
  check(document.querySelector('[data-sort="'+column+'"]').parentElement.getAttribute('aria-sort')==='ascending','sort column '+column);
 }
 change('list-show','needs-me');check(visible().length===1 && visible()[0].dataset.number==='58','Needs me');
 change('list-show','unread');check(visible().length===5,'Unread includes the saved unread updates');
 change('list-show','done');check(visible().length===1 && visible()[0].dataset.number==='63','Done');
 change('list-show','parked');check(visible().length===1 && visible()[0].dataset.number==='41','Parked');
 change('list-show','closed');check(visible().length===1 && visible()[0].dataset.number==='54','Closed');
 change('list-show','all');check(visible().length===6,'All');
 change('list-owner',OWNER);check(visible().length===5,'Owner');
 change('list-owner','');
 change('list-producer',DWIGHT);check(visible().map(row=>row.dataset.number).join(',')==='105','Producer');
 check(new URLSearchParams(location.hash.slice(1)).get('producer')===DWIGHT,'the address keeps the producer');
 change('list-producer','');
 check(visible().length===6 && !new URLSearchParams(location.hash.slice(1)).has('producer'),'All producers');
 change('list-search','#106','input');
 check(visible().length===1 && visible()[0].dataset.number==='106','number search');
 change('list-search','console','input');check(visible().length===2,'title search');
 check(new URLSearchParams(location.hash.slice(1)).get('q')==='console','the address keeps the query');
 for(const title of ['Straße','STRAẞE']){
  change('list-search',title,'input');
  check(visible().length===1 && visible()[0].dataset.number==='106','Unicode title search: '+title);
 }
 change('list-search','console','input');
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
  if(holdPost){await flush();form.elements.words.value='New draft during send';form.elements.words.dispatchEvent(new Event('input'));
   check(form.querySelector('[value="tell"]').disabled,'editing words during delivery keeps Tell morty disabled');finishPost();}
  check(form.querySelector('[data-send-status]').textContent===(action==='read' ? 'Saving…' : 'Sending…'),'immediate response '+action);
  await flush();await flush();
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
 check(visible().length===1 && visible()[0].dataset.number==='54','hash navigation restores the filters');
 location.hash=new URLSearchParams({show:'all',producer:DWIGHT});await flush();
 check(document.getElementById('list-producer').value===DWIGHT && visible().map(row=>row.dataset.number).join(',')==='105',
   'an address restores the producer filter');
 location.hash=new URLSearchParams({show:'all',producer:'missing'});await flush();
 check(document.getElementById('list-producer').value==='' && visible().length===6,'an unknown producer shows all items');

 for(const target of rows().filter(row=>['Done','Closed'].includes(row.dataset.state) || row.dataset.parked==='true')){
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  check(!target.hidden && !target.querySelector('[data-detail]').hidden,'an item link shows '+target.dataset.state);
  check(document.getElementById('list-show').value==='all' && document.getElementById('list-owner').value==='' &&
    document.getElementById('list-producer').value==='' && document.getElementById('list-search').value==='','an item link clears filters');
 }
 const retained=rows().find(row=>row.dataset.number==='106');
 location.hash=new URLSearchParams({item:retained.dataset.key,owner:retained.dataset.owner,show:'open',q:'106',sort:'#',dir:'desc'});await flush();
 check(document.getElementById('list-owner').value===retained.dataset.owner && document.getElementById('list-show').value==='open' &&
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
})();</script>'''.replace('OWNER', json.dumps(OWNER.id)).replace('DWIGHT', json.dumps(DWIGHT.id))
        for width in (1440, 390):
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
                            result = {"state": "sent", **list_page.fresh_request(root, fields["keeper"][0], fields["id"][0])}
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
                            sends.assert_called_once()
                        self.assertEqual(done.returncode, 0, done.stderr)
                    finally:
                        server.shutdown()
                        thread.join(5)
                self.assertEqual(results, [[]])


if __name__ == '__main__':
    _ = unittest.main()
