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
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

from scripts.lib import urgency
from scripts.lib.user_zone import user_zone
from scripts.production import change_review, footer_counts, investigation, next_item, staff
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

    def test_primary_actions_keep_the_user_words_on_each_record(self) -> None:
        question = self.case.question()
        ready = self.case.investigation()
        cases = ((question,'answer'),('page-owner/C1','works'),('page-owner/C2','not works'),
                 (f'investigation/{ready.owner}/{ready.id}','read'))
        for key,action in cases:
            with self.subTest(action=action):
                delivery,_ = review_page.submit(self.case.root,self.case.body(key,action,'Keep these words.'),self.case.status)
                change_page.deliver(delivery,self.case.status)
                item = next(item for item in review_items.read(self.case.root) if item.key == key)
                self.assertEqual(item.state,review_items.ReviewState.DONE)
                self.assertTrue(any('Keep these words.' in entry.words for entry in item.log))

    def test_investigation_feedback_uses_the_shared_retry_and_owner_alert(self) -> None:
        ready = self.case.investigation()
        key = f'investigation/{ready.owner}/{ready.id}'
        delivery,_ = review_page.submit(self.case.root,self.case.body(key,'feedback','Check the next report.'),self.case.status)
        calls: list[str] = []
        def send(_recipient: staff.StaffId, _sender: str, message: str) -> None:
            calls.append(message)
            if not message.startswith('Change review delivery needs attention'):
                raise RuntimeError('fixture send failed')
        with patch.object(change_page,'send_to_staff',side_effect=send):
            change_page.deliver(delivery,self.case.status)
        self.assertEqual(len(calls),4)
        self.assertIn('Check the next report.',calls[-1])
        self.assertIn('Last error: fixture send failed',calls[-1])
        self.assertEqual(next(item.state for item in review_items.read(self.case.root) if item.key == key),review_items.ReviewState.DONE)

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
                route = urlsplit(self.path)
                if route.path in ('/console/changes.html', '/console/questions.html', '/console/investigations.html'):
                    self.send_response(303)
                    self.send_header('Location', review_page.legacy_url(route.path, route.query))
                    self.end_headers()
                    return
                if route.path == '/owner-answer':
                    key = parse_qs(route.query)['item'][0]
                    item = next(item for item in review_items.read(case.root) if item.key == key)
                    question = item.source.slots.question if isinstance(item.source, review_items.ChangeSource) else change_page.NoQuestion()
                    identity = question.question_id if isinstance(question, change_page.OpenQuestion) else ''
                    _ = change_page.answer_item(key,'The owner checked the next case.',identity,case.status)
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
                    _ = self.wfile.write(review_page.render(case.root, show=parse_qs(route.query).get('show', ['open'])[0]))
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
        question = self.case.question()
        ready = self.case.investigation()
        markup = review_page.render(self.case.root).decode()
        setup = '''<script>const nativeFetch=fetch;window.setInterval=()=>0;
window.fetch=(url,options={})=>{
 return nativeFetch(options.method==='POST'?'/action':'/fresh'+new URL(url,location.href).search,options);
};</script>'''
        markup = markup.replace('<head>','<head>'+setup,1)
        probe = r'''
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 const nextView=()=>new Promise(resolve=>{
  const previous=document.querySelector('[data-review]');
  const observer=new MutationObserver(()=>{
   if(document.querySelector('[data-review]')!==previous){observer.disconnect();resolve();}
  });
  observer.observe(document.body,{childList:true,subtree:true});
 });
 const row=id=>document.querySelector('[data-key="page-owner/'+id+'"]');
 const refresh=async()=>{const loaded=nextView();dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await loaded;};
 const save=async(form,value)=>{
  const complete=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{if(!form.dataset.sending){observer.disconnect();resolve();}});
   observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
  });
  form.requestSubmit(form.querySelector('[value="'+value+'"]'));await complete;
 };
 const show=async value=>{const field=document.getElementById('list-show');if(field.value===value) return;const loaded=nextView();field.value=value;field.dispatchEvent(new Event('change'));await loaded;};
 try{
  row('C1').querySelector('.list-toggle').click();
  const form=row('C1').querySelector('form');await save(form,'validate');
  check(row('C1').hidden,'the saved check leaves Open');
  await refresh();check(!row('C1') || row('C1').hidden,'a refresh keeps the waiting check out of Open');
  await show('Waiting');check(!row('C1').hidden,'Waiting can show the active check');await show('open');
  await nativeFetch('/problem');await refresh();check(!row('C1').hidden,'a problem returns to For you');
  row('C2').querySelector('.list-toggle').click();
  const second=row('C2').querySelector('form');await save(second,'validate');
  await nativeFetch('/passed');await refresh();check(!row('C2') || row('C2').hidden,'a passed check stays out of Open');
  await show('Done');check(!row('C2').hidden && row('C2').dataset.state==='Done','a passed check records Done');
  await show('all');row('C3').querySelector('.list-toggle').click();
  const third=row('C3').querySelector('form');await save(third,'works');
  check(row('C3').hidden,'F010: Works leaves All');
  for(let pass=0;pass<2;pass++){
   await refresh();
   for(const choice of ['open','For you','Waiting','Done','all']){
    await show(choice);check(!row('C3') || row('C3').hidden,'F010: refresh keeps Works hidden in '+choice);
   }
  }
  const keys=QUESTION_KEYS;
  for(const key of keys){
   await show('open');
   const asked=()=>document.querySelector('[data-key="'+key+'"]');
   asked().querySelector('.list-toggle').click();
   const asking=asked().querySelector('form');asking.elements.feedback.value='Check the next case.';
   await save(asking,'question');
   check(asked().hidden,'F021: Ask question leaves Open for '+key);
   for(let pass=0;pass<2;pass++){
    await refresh();check(!asked() || asked().hidden,'F021: Waiting stays hidden after refresh for '+key);
   }
   await nativeFetch('/owner-answer?item='+encodeURIComponent(key));await refresh();
   check(!asked().hidden && asked().dataset.state==='For you','F021: the owner answer returns '+key);
  }
  location.hash=new URLSearchParams({owner:UNIT_OWNER,show:'open',type:'investigation'});
  await new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  const investigation=document.querySelector('[data-key="'+keys.at(-1)+'"]');
  investigation.querySelector('.list-toggle').click();await save(investigation.querySelector('form'),'read');
  await refresh();
  const owner=document.getElementById('list-owner');
  check(owner.value===UNIT_OWNER && owner.selectedOptions[0]?.textContent===UNIT_NAME+' (0)',
   'F002: the selected unit Owner remains with a zero count after its last Open row disappears');
  check(!document.querySelector('[data-list-item]:not([hidden])'),'F002: the empty unit filter stays active');
  await show('Done');
  check(document.getElementById('list-owner').value===UNIT_OWNER,
   'F002: the unit Owner remains selected when Done returns its record');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        probe = probe.replace('QUESTION_KEYS',json.dumps([question,'page-owner/C1',f'investigation/{ready.owner}/{ready.id}']))
        probe = probe.replace('UNIT_OWNER', json.dumps(str(ready.owner))).replace(
            'UNIT_NAME', json.dumps(review_items.name(ready.owner, staff.snapshot())))
        self.assertEqual(self.browser(markup,probe),[])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_legacy_list_count_links_keep_the_review_owner(self) -> None:
        question_key = self.case.question()
        ready = self.case.investigation()
        _ = self.case.investigation(False)
        read = self.case.investigation()
        _ = investigation.mark_read(read.owner, read.id)
        members = staff.snapshot()
        owner = next(row for row in members.rows if row.name == 'page-owner')
        _ = investigation.file(owner.id, 'Another owner', 'Read its log.')
        markup = console.render(console.read_pages(),console.read_rebuilds()).decode()
        probe = r'''
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 const loadedView=(page,show)=>new Promise(resolve=>{
  if(page.querySelector('[data-review]')?.dataset.show===show){resolve();return;}
  const observer=new MutationObserver(()=>{
   if(page.querySelector('[data-review]')?.dataset.show===show){observer.disconnect();resolve();}
  });
  observer.observe(page.body,{childList:true,subtree:true});
 });
 try{
  const frame=[...document.querySelectorAll('iframe')].find(frame=>!frame.hidden);
  if(!frame.contentDocument.querySelector('[data-review]')) await new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  const place=new URL(frame.src);const filters=new URLSearchParams(place.hash.slice(1));
  check(filters.get('owner')==='page-owner','the alias keeps the selected owner');
  check(filters.get('type')==='question','the alias keeps the question type');
  const rows=[...frame.contentDocument.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
  check(rows.length===1 && rows[0].dataset.type==='question','the Review filters select the owner question');
  check(frame.contentDocument.getElementById('list-owner').value===OWNER,'the owner name resolves to its staff ID');
  const cases=CASES;
  for(const entry of cases){
   const legacy=document.createElement('iframe');legacy.src=entry.url;
   const loaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);await loaded;
   const page=legacy.contentDocument;await loadedView(page,entry.show);
   check(page.getElementById('list-show').value===entry.show,'F012: legacy Show '+entry.url);
   const visible=[...page.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
   check(visible.length===entry.count && visible.every(row=>row.dataset.type===entry.type),'F012: legacy records '+entry.url);
   if(entry.item) check(!page.querySelector('[data-key="'+entry.item+'"] [data-detail]').hidden,'F012: legacy row opens its card');
   legacy.remove();
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''.replace('OWNER',json.dumps(str(next(item.owner for item in review_items.read(self.case.root) if item.type == review_items.ReviewType.QUESTION))))
        cases = [{'url':'/console/changes.html#'+urlencode({'showrunner':'page-owner','row':'page-owner/C2'}),
                  'show':'open','count':3,'type':'change','item':'page-owner/C2'},
                 {'url':'/console/changes.html?type=questions#'+urlencode({'showrunner':'page-owner','row':question_key}),
                  'show':'open','count':1,'type':'question','item':question_key}]
        cases.extend({'url':'/console/investigations.html#'+urlencode({'owner':ready.owner,'show':old}),
                      'show':new,'count':count,'type':'investigation','item':''}
                     for old,new,count in (('not-read','open',2),('answer ready','For you',1),
                                           ('open','Waiting',1),('read','Done',1),('all','all',3)))
        cases.extend(({'url':'/console/investigations.html#'+urlencode({'owner':owner.id,'show':'all'}),
                       'show':'all','count':1,'type':'investigation','item':''},
                      {'url':'/console/investigations.html#'+urlencode({'owner':ready.owner}),
                       'show':'open','count':2,'type':'investigation','item':''},
                      {'url':'/console/changes.html?'+urlencode({'type':'changes','showrunner':'page-owner','row':'page-owner/C2'}),
                       'show':'open','count':3,'type':'change','item':'page-owner/C2'},
                      {'url':'/console/questions.html#'+urlencode({'showrunner':'page-owner','row':question_key}),
                       'show':'open','count':1,'type':'question','item':question_key}))
        probe = probe.replace('CASES', json.dumps(cases))
        place='/?'+urlencode({'page':'questions','at.questions':'/console/changes.html?type=questions#showrunner=page-owner'})
        self.assertEqual(self.browser(markup,probe,place),[])
        alias_probe = r'''
(async()=>{
 const failures=[];
 try{
  const frame=[...document.querySelectorAll('iframe')].find(frame=>!frame.hidden);
  if(!frame.contentDocument.querySelector('[data-review]')) await new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  const page=frame.contentDocument;
  if(page.querySelector('[data-review]').dataset.show!=='Done') await new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    if(page.querySelector('[data-review]').dataset.show==='Done'){observer.disconnect();resolve();}
   });
   observer.observe(page.body,{childList:true,subtree:true});
  });
  const rows=[...page.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
  if(page.getElementById('list-show').value!=='Done' || rows.length!==1 || rows[0].dataset.state!=='Done')
   failures.push('F012: the console alias retains Read and its exact owner');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        place = '/?'+urlencode({'page':'investigations','at.investigations':'/console/investigations.html#'+urlencode({'owner':ready.owner,'show':'read'})})
        self.assertEqual(self.browser(markup, alias_probe, place), [])


if __name__ == '__main__':
    _ = unittest.main()
