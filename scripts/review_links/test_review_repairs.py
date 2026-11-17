"""Check the Review contracts reported in the first review."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast, override
import unittest
from urllib.parse import urlencode

from scripts.lib import urgency
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, footer_counts, next_item, staff
from scripts.review_links import change_page, console, review_items, review_page, test_review_page


class ReviewRepairsTests(unittest.TestCase):
    case: test_review_page.ReviewPageTests = test_review_page.ReviewPageTests('runTest')

    @override
    def setUp(self) -> None:
        self.case = test_review_page.ReviewPageTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_efficiency_state_count_and_next_use_the_same_first_status(self) -> None:
        fixture = self.case.fixture
        ledger = fixture.temporary / 'efficiency.md'
        proposed = '- 2026-11-01 tests: Smaller checks — proposed; related item — done'
        _ = ledger.write_text(proposed+'\n- 2026-11-02 tests: Shipped — accepted; related item — proposed\n')
        _ = fixture.production.write_text(fixture.production.read_text()+f'- **Efficiency ledger:** `{ledger}`\n')
        items = [item for item in review_items.read(self.case.root) if item.type == review_items.ReviewType.EFFICIENCY]
        self.assertEqual([item.state for item in items],[review_items.ReviewState.FOR_YOU,review_items.ReviewState.DONE])
        self.assertEqual(footer_counts.efficiency(fixture.production),1)
        queue = next_item.read_queue('efficiency',fixture.production)
        self.assertEqual([entry.item for entry in queue.entries],[proposed])
        selected = review_items.read_kind(review_items.ReviewType.EFFICIENCY,fixture.production,staff.snapshot())
        self.assertEqual([item.state for item in selected],[review_items.ReviewState.FOR_YOU,review_items.ReviewState.DONE])

    def test_review_retains_the_change_queue_pin_and_move(self) -> None:
        fixture = self.case.fixture
        queue = next_item.read_queue('change',fixture.production)
        urgency.pin(queue.name,'C2')
        items = [item for item in review_items.read(self.case.root) if item.type == review_items.ReviewType.CHANGE]
        self.assertEqual(items[0].key,'page-owner/C2')
        urgency.move_to_end(queue.name,'C2',datetime.now(user_zone()).replace(tzinfo=None))
        expected = urgency.queue_order(queue.entries,queue.name)
        items = [item for item in review_items.read(self.case.root) if item.type == review_items.ReviewType.CHANGE]
        self.assertEqual([item.queue_key for item in items],[entry.key for entry in expected])
        self.assertEqual(items[-1].key,'page-owner/C2')

    def test_change_prerequisites_use_the_full_review_key(self) -> None:
        fixture = self.case.fixture
        _ = fixture.review_list.write_text('## To review\n\n| # | Change | How to check | Producer | Urgency |\n'
            +'| --- | --- | --- | --- | --- |\n'
            +f'| C1 | Dependent | Read | {fixture.unit.id} | after C2 |\n'
            +f'| C2 | First | Read | {fixture.unit.id} | |\n')
        items = [item for item in review_items.read(self.case.root) if item.type == review_items.ReviewType.CHANGE]
        self.assertEqual([item.key for item in items],['page-owner/C2','page-owner/C1'])
        self.assertEqual(items[1].after,items[0].key)
        queue = next_item.read_queue('change',fixture.production)
        self.assertEqual([entry.key for entry in urgency.queue_order(queue.entries,queue.name)],['C2','C1'])

    def test_a_missing_efficiency_ledger_has_a_named_absence(self) -> None:
        self.assertIsInstance(review_items.efficiency_ledger(self.case.fixture.production),review_items.NoEfficiencyLedger)
        self.assertIsInstance(footer_counts.efficiency_ledger(self.case.fixture.production),review_items.NoEfficiencyLedger)

    def test_automation_docs_name_review_and_only_answer_ready_counts(self) -> None:
        source = (Path(__file__).parents[2]/'docs/as-built/showrunner-automation.md').read_text()
        self.assertIn('The status line counts only answer-ready investigations after questions.',source)
        self.assertNotIn('The status line counts open and answer-ready investigations',source)
        self.assertNotIn('Questions page',source)
        self.assertNotIn('Investigations page',source)

    def browser(self, markup: str, probe: str, place: str = '/') -> list[str]:
        results: list[list[str]] = []
        complete = threading.Event()
        case = self.case
        document = markup.replace('</body>','<img hidden src="/complete"><script>'+probe+'</script></body>').encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == '/problem':
                    _ = change_review.save_verdict(case.fixture.review_list,'C1','not works: The spacing differs.',case.fixture.production)
                if self.path == '/passed':
                    _ = change_review.save_verdict(case.fixture.review_list,'C2','works',case.fixture.production)
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.end_headers()
                if self.path == '/complete':
                    _ = complete.wait(25)
                elif self.path.startswith('/fresh') or self.path.startswith('/console/review.html'):
                    _ = self.wfile.write(review_page.render(case.root))
                else:
                    _ = self.wfile.write(document)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/action':
                    delivery,answer = review_page.submit(case.root,body.decode(),case.status)
                    change_page.deliver(delivery,case.status)
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.end_headers()
                    _ = self.wfile.write(json.dumps({'answer':answer}).encode())
                    return
                results.append(cast(list[str],json.loads(body)))
                complete.set()
                self.send_response(204)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory(dir=self.case.fixture.temporary) as profile:
                    done = subprocess.run(['firefox','--headless','--no-remote','--profile',profile,
                        '--window-size','1440,1100','--screenshot',str(self.case.fixture.temporary/'repair.png'),
                        f'http://127.0.0.1:{server.server_port}'+place],capture_output=True,text=True,timeout=35)
                self.assertEqual(done.returncode,0,done.stderr)
            finally:
                server.shutdown()
                thread.join(5)
        self.assertEqual(len(results),1)
        return results[0]

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_showrunner_check_stays_out_of_open_after_refresh_and_returns_only_for_a_problem(self) -> None:
        markup = review_page.render(self.case.root).decode()
        setup = '''<script>const nativeFetch=fetch;window.setInterval=()=>0;
window.fetch=(url,options={})=>nativeFetch(options.method==='POST'?'/action':'/fresh',options);</script>'''
        markup = markup.replace('<head>','<head>'+setup,1)
        probe = r'''
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const row=id=>document.querySelector('[data-key="page-owner/'+id+'"]');
 const refresh=async()=>{dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await pause(120);};
 const show=value=>{const field=document.getElementById('list-show');field.value=value;field.dispatchEvent(new Event('change'));};
 try{
  row('C1').querySelector('.list-toggle').click();
  const form=row('C1').querySelector('form');form.requestSubmit(form.querySelector('[value="validate"]'));
  await pause(1100);check(row('C1').hidden,'the saved check leaves Open');
  await refresh();check(row('C1').hidden,'a refresh keeps the waiting check out of Open');
  show('Waiting');check(!row('C1').hidden,'Waiting can show the active check');show('open');
  await nativeFetch('/problem');await refresh();check(!row('C1').hidden,'a problem returns to For you');
  row('C2').querySelector('.list-toggle').click();
  const second=row('C2').querySelector('form');second.requestSubmit(second.querySelector('[value="validate"]'));
  await pause(1100);await nativeFetch('/passed');await refresh();check(row('C2').hidden,'a passed check stays out of Open');
  show('Done');check(!row('C2').hidden && row('C2').dataset.state==='Done','a passed check records Done');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        self.assertEqual(self.browser(markup,probe),[])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_legacy_list_count_links_keep_the_review_owner(self) -> None:
        _ = self.case.question()
        markup = console.render(console.read_pages(),console.read_rebuilds()).decode()
        probe = r'''
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 try{
  const frame=[...document.querySelectorAll('iframe')].find(frame=>!frame.hidden);
  if(!frame.contentDocument.querySelector('[data-review]')) await new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  const place=new URL(frame.src);const filters=new URLSearchParams(place.hash.slice(1));
  check(filters.get('owner')==='page-owner','the alias keeps the selected owner');
  check(filters.get('type')==='question','the alias keeps the question type');
  const rows=[...frame.contentDocument.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
  check(rows.length===1 && rows[0].dataset.type==='question','the Review filters select the owner question');
  check(frame.contentDocument.getElementById('list-owner').value===OWNER,'the owner name resolves to its staff ID');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''.replace('OWNER',json.dumps(str(next(item.owner for item in review_items.read(self.case.root) if item.type == review_items.ReviewType.QUESTION))))
        place='/?'+urlencode({'page':'questions','at.questions':'/console/changes.html?type=questions#showrunner=page-owner'})
        self.assertEqual(self.browser(markup,probe,place),[])


if __name__ == '__main__':
    _ = unittest.main()
