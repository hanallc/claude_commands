"""Check the Review contracts reported in the first review."""
from __future__ import annotations

from datetime import datetime, timedelta
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

from scripts.lib import conf, urgency
from scripts.lib.user_zone import user_zone
from scripts.lint.memory_test_support import write_config_fixture
from scripts.production import change_review, footer_counts, investigation, next_item, staff, update_registration
from scripts.production.production_doc import read_production
from scripts.production.staff_test_support import fixture_member
from scripts.review_links import change_page, console, review_actions, review_items, review_page, test_review_page
from scripts.review_links import question_record


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
        self.assertEqual([item.state for item in items],[review_items.ReviewState.OPEN,review_items.ReviewState.DONE])
        self.assertEqual(footer_counts.efficiency(fixture.production),1)
        queue = next_item.read_queue('efficiency',fixture.production)
        self.assertEqual([entry.item for entry in queue.entries],[proposed])
        selected = review_items.read_kind(review_items.ReviewType.EFFICIENCY,fixture.production,staff.snapshot())
        self.assertEqual([item.state for item in selected],[review_items.ReviewState.OPEN,review_items.ReviewState.DONE])

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

    def test_investigation_feedback_uses_the_shared_retry_and_showrunner_alert(self) -> None:
        producer = fixture_member("producer", name="bob")
        runner = staff.snapshot().by_path("page-owner")
        assert runner is not None
        _ = write_config_fixture(conf.path("review_links"), "unrouted_alert_recipient=page-owner\n")
        record = investigation.file(producer.id, "Why did the gate stop?", "Read the gate log.",
                                    waiting_why="The owner must complete the investigation.")
        ready = investigation.answer(record.owner, record.id, "The test failed. The repair is ready.")
        key = f'investigation/{ready.owner}/{ready.id}'
        delivery,_ = review_page.submit(self.case.root,self.case.body(key,'feedback','Check the next report.'),self.case.status)
        calls: list[str] = []
        targets: list[staff.StaffId] = []
        keys: list[str] = []

        def send(recipient: staff.StaffId, _sender: str, message: str, *, key: str) -> None:
            calls.append(message)
            targets.append(recipient)
            keys.append(key)
            if not message.startswith('Change review delivery needs attention'):
                raise RuntimeError('fixture send failed')

        with patch.object(change_page,'send_to_staff',side_effect=send):
            change_page.deliver(delivery,self.case.status)
        self.assertEqual(len(calls),4)
        self.assertEqual(targets, [producer.id] * 3 + [runner.id])
        self.assertEqual(len(set(keys[:3])), 1)
        self.assertNotEqual(keys[0], keys[-1])
        self.assertIn('Check the next report.',calls[-1])
        self.assertIn('Last error: fixture send failed',calls[-1])
        self.assertEqual(next(item.state for item in review_items.read(self.case.root) if item.key == key),review_items.ReviewState.DONE)

    def test_waiting_unowned_question_keeps_only_later(self) -> None:
        key = self.case.question()
        members = staff.snapshot()
        owner = next(row for row in members.rows if row.name == 'page-owner')
        scope = read_production(owner, members)
        identities = update_registration.QuestionStaff.for_record(members)
        question = next(item for item in review_items.read(self.case.root) if item.key == key)
        _ = update_registration.followup(scope, question.title, 'Check the old case.', identities=identities)
        without_owner = staff.StaffSnapshot(tuple(row for row in members.rows if row.id != owner.id))
        with patch.object(staff, 'snapshot', return_value=without_owner):
            waiting = next(item for item in review_items.read(self.case.root) if item.key == key)
            self.assertEqual(waiting.state, review_items.ReviewState.WAITING)
            self.assertIsInstance(waiting.source, review_items.UnownedQuestionSource)
            assert isinstance(waiting.source, review_items.UnownedQuestionSource)
            before = waiting.source.record.path.read_bytes()
            self.assertEqual(waiting.actions, ())
            form = review_page.form(waiting, change_page.ReviewBrowser('fixture-token'),
                                    conf.read('review_links').whole('review_words_rows'), 'fixture-revision')
            self.assertIn('aria-haspopup="menu" aria-expanded="false" data-later-toggle>Later', form)
            self.assertIn('value="later">Save', form)
            for action in ('answer', 'feedback', 'question'):
                with self.subTest(action=action):
                    self.assertNotIn(f'value="{action}"', form)
                    with self.assertRaises(question_record.AnswerRefused):
                        _ = review_page.submit(self.case.root, self.case.body(key, action, 'Use the new case.'), self.case.status)
                    self.assertEqual(waiting.source.record.path.read_bytes(), before)

    def test_empty_actions_keep_each_wait_and_its_question_id(self) -> None:
        proposal, efficiency = self.case.decisions()
        question = self.case.question()
        report = self.case.investigation()
        keys = (question, 'page-owner/C1', f'investigation/{report.owner}/{report.id}', proposal, efficiency)
        for key in keys:
            delivery, _ = review_page.submit(self.case.root, self.case.body(key, 'question', 'Check the old case.'), self.case.status)
            assert isinstance(delivery, change_page.MessageDelivery)
            change_page.deliver(delivery, self.case.status)
            waiting = next(item for item in review_items.read(self.case.root) if item.key == key)
            slots = self.case.status.slots(delivery.page, delivery.row)
            actions = ('answer', 'feedback', 'question') if waiting.type == review_items.ReviewType.QUESTION else ('feedback', 'question')
            for action in actions:
                with self.subTest(kind=waiting.type, action=action):
                    with self.assertRaises((ValueError, update_registration.RegistrationFailure)):
                        _ = review_page.submit(self.case.root, self.case.body(key, action, ' \n '), self.case.status)
                    self.assertEqual(next(item for item in review_items.read(self.case.root) if item.key == key), waiting)
                    self.assertEqual(self.case.status.slots(delivery.page, delivery.row), slots)

    def test_native_question_followup_accepts_each_action(self) -> None:
        for action in ('answer', 'feedback', 'question'):
            with self.subTest(action=action):
                case = test_review_page.ReviewPageTests('runTest')
                case.setUp()
                try:
                    key = case.question()
                    members = staff.snapshot()
                    owner = next(row for row in members.rows if row.name == 'page-owner')
                    scope = read_production(owner, members)
                    identities = update_registration.QuestionStaff.for_record(members)
                    question = next(item for item in review_items.read(case.root) if item.key == key)
                    _ = update_registration.followup(scope, question.title, 'Check the old case.', identities=identities)
                    waiting = next(item for item in review_items.read(case.root) if item.key == key)
                    self.assertEqual(waiting.state, review_items.ReviewState.WAITING)
                    delivery, _ = review_page.submit(case.root, case.body(key, action, 'Use the new case.'), case.status)
                    change_page.deliver(delivery, case.status)
                    updated = next(item for item in review_items.read(case.root) if item.key == key)
                    expected = review_items.ReviewState.WAITING if action == 'question' else review_items.ReviewState.DONE
                    self.assertEqual(updated.state, expected)
                    assert isinstance(updated.source, review_items.QuestionSource)
                    self.assertEqual('followup' in updated.source.record.item, action == 'question')
                    self.assertEqual(updated.source.record.item.get('followups', []), [])
                    self.assertTrue(any('superseded' in entry.words.casefold() for entry in updated.log))
                    self.assertTrue(any('Check the old case.' in entry.words for entry in updated.log))
                    self.assertIn('superseded', scope.log.read_text().casefold())
                    before = updated.source.record.path.read_bytes()
                    with self.assertRaises((ValueError, update_registration.RegistrationFailure)):
                        _ = change_page.answer_item(key, 'The old case passes.', '', case.status)
                    self.assertEqual(updated.source.record.path.read_bytes(), before)
                finally:
                    case.doCleanups()

    def test_recovery_sends_only_the_current_request(self) -> None:
        for kind in review_items.REVIEW_TYPES:
            with self.subTest(kind=kind):
                case = test_review_page.ReviewPageTests('runTest')
                case.setUp()
                try:
                    if kind == review_items.ReviewType.QUESTION:
                        key = case.question()
                    elif kind == review_items.ReviewType.CHANGE:
                        key = 'page-owner/C1'
                    elif kind == review_items.ReviewType.INVESTIGATION:
                        report = case.investigation()
                        key = f'investigation/{report.owner}/{report.id}'
                    else:
                        proposal, efficiency = case.decisions()
                        key = proposal if kind == review_items.ReviewType.PROPOSAL else efficiency
                    old, _ = review_page.submit(case.root, case.body(key, 'question', 'FIRST_REQUEST'), case.status)
                    if kind == review_items.ReviewType.CHANGE:
                        with self.assertRaisesRegex(question_record.AnswerRefused, 'already with the showrunner'):
                            _, _ = review_page.submit(case.root, case.body(key, 'feedback', 'SECOND_REQUEST'), case.status)
                        expected_text, excluded_text = 'FIRST_REQUEST', 'SECOND_REQUEST'
                        expected_state = review_items.ReviewState.WAITING
                    else:
                        _, _ = review_page.submit(case.root, case.body(key, 'feedback', 'SECOND_REQUEST'), case.status)
                        expected_text, excluded_text = 'SECOND_REQUEST', 'FIRST_REQUEST'
                        expected_state = review_items.ReviewState.DONE
                    restarted = change_page.DeliveryStatus(case.root)
                    change_page.recover_pending(restarted)
                    messages = tuple(case.fixture.messages)
                    self.assertEqual(len(messages), 1)
                    self.assertIn(expected_text, messages[0][2])
                    self.assertNotIn(excluded_text, messages[0][2])
                    change_page.deliver(old, case.status)
                    change_page.recover_pending(restarted)
                    self.assertEqual(tuple(case.fixture.messages), messages)
                    updated = next(item for item in review_items.read(case.root) if item.key == key)
                    self.assertEqual(updated.state, expected_state)
                finally:
                    case.doCleanups()

    def test_question_recovery_keeps_the_id_before_tracking_finishes(self) -> None:
        for kind in (review_items.ReviewType.QUESTION, review_items.ReviewType.INVESTIGATION,
                     review_items.ReviewType.PROPOSAL, review_items.ReviewType.EFFICIENCY):
            with self.subTest(kind=kind):
                case = test_review_page.ReviewPageTests('runTest')
                case.setUp()
                try:
                    if kind == review_items.ReviewType.QUESTION:
                        key = case.question()
                    elif kind == review_items.ReviewType.INVESTIGATION:
                        report = case.investigation()
                        key = f'investigation/{report.owner}/{report.id}'
                    else:
                        proposal, efficiency = case.decisions()
                        key = proposal if kind == review_items.ReviewType.PROPOSAL else efficiency
                    with patch.object(review_actions, 'track_question', side_effect=RuntimeError('fixture crash')):
                        with self.assertRaisesRegex(RuntimeError, 'fixture crash'):
                            _ = review_page.submit(case.root, case.body(key, 'question', 'Check this request.'), case.status)
                    waiting = next(item for item in review_items.read(case.root) if item.key == key)
                    page = review_actions.delivery_page(waiting, case.status)
                    slots = case.status.slots(page, page.rows[0])
                    self.assertIsInstance(slots.question, change_page.OpenQuestion)
                    assert isinstance(slots.question, change_page.OpenQuestion)
                    identity = slots.question.question_id
                    fields = cast(change_page.StoredDeliverySlots, json.loads(case.status.path(page, page.rows[0]).read_text()))
                    saved = fields.get('user_answer') or fields.get('review_message')
                    assert saved is not None
                    self.assertIn('--question '+identity, saved['message'])
                    restarted = change_page.DeliveryStatus(case.root)
                    change_page.recover_pending(restarted)
                    self.assertEqual(len(case.fixture.messages), 1)
                    self.assertIn('--question '+identity, case.fixture.messages[0][2])
                    self.assertEqual(case.status.slots(page, page.rows[0]).question, slots.question)
                finally:
                    case.doCleanups()

    def test_automation_docs_name_review_and_only_answer_ready_counts(self) -> None:
        source = (Path(__file__).parents[2]/'docs/as-built/showrunner-automation.md').read_text()
        self.assertIn('The status line counts only answer-ready investigations after questions.',source)
        self.assertNotIn('The status line counts open and answer-ready investigations',source)
        self.assertNotIn('Questions page',source)
        self.assertNotIn('Investigations page',source)

    def browser(self, markup: str, probe: str, place: str = '/', width: int = 1440) -> list[str]:
        results: list[list[str]] = []
        complete = threading.Event()
        probe_timeout = 60
        case = self.case
        markup=markup.replace('<head>','<head><script>'+test_review_page.BODY_READY_SCRIPT+'</script>',1)
        document = markup.replace('</body>','<img hidden src="/complete"><script>'+probe+'</script></body>').encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                route = urlsplit(self.path)
                if route.path == '/complete':
                    if complete.wait(probe_timeout):
                        self.send_response(204)
                        self.end_headers()
                    else:
                        self.send_error(504, 'The Review probe did not report a result.')
                    return
                if route.path in ('/console/changes.html', '/console/questions.html', '/console/investigations.html'):
                    self.send_response(303)
                    self.send_header('Location', review_page.legacy_url(route.path, route.query))
                    self.end_headers()
                    return
                if route.path == '/owner-answer':
                    key = parse_qs(route.query)['item'][0]
                    item = next(item for item in review_items.read(case.root) if item.key == key)
                    if isinstance(item.source, review_items.ChangeSource):
                        question = item.source.slots.question
                    else:
                        page = review_actions.delivery_page(item, case.status)
                        question = case.status.slots(page, page.rows[0]).question
                    identity = question.question_id if isinstance(question, change_page.OpenQuestion) else ''
                    _ = change_page.answer_item(key,'The owner checked the next case.',identity,case.status)
                if self.path in ('/problem', '/passed'):
                    row, verdict = ('C1', 'not works: The spacing differs.') if self.path == '/problem' else ('C2', 'works')
                    page = change_page.ReviewPage(case.fixture.review_list, case.fixture.production, (row,), '')
                    validation = case.status.slots(page, row).validation
                    assert isinstance(validation, (change_page.DeliveryIntent, change_page.ValidationRequested))
                    _ = change_review.save_verdict(case.fixture.review_list, row, verdict, case.fixture.production,
                                                   validation_id=validation.attempt)
                fields = parse_qs(route.query)
                snapshot = review_page.build_snapshot(case.root) if route.path in ('/fresh', review_page.PAGE_PATH) else None
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                if snapshot is not None and 'body' in fields:
                    self.send_header('X-Review-Version', snapshot.version)
                self.end_headers()
                if snapshot is not None:
                    if 'body' in fields:
                        _ = self.wfile.write(snapshot.body(fields['body'][0]))
                    elif 'index' in fields:
                        _ = self.wfile.write(snapshot.index_response())
                    elif 'version' in fields:
                        _ = self.wfile.write(json.dumps({'version':snapshot.version,'facts':snapshot.facts()}).encode())
                    else:
                        _ = self.wfile.write(snapshot.render(show=fields.get('show',['open'])[0]))
                else:
                    _ = self.wfile.write(document)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/action':
                    cache = review_page.ReviewSnapshotCache(case.root, start_worker=False)
                    saved = cache.save(body.decode(), case.status)
                    result: dict[str, object] = {'answer': saved.answer, 'version': saved.version}
                    if isinstance(saved.item, review_items.ReviewItem):
                        later = saved.item.times.later
                        result.update(state=saved.item.state, later=0 if isinstance(later, review_items.NoLaterTime)
                                      else later.timestamp()*1000)
                    try:
                        content = json.dumps(result).encode()
                        self.send_response(200)
                        self.send_header('Content-Type','application/json')
                        self.send_header('Content-Length', str(len(content)))
                        self.end_headers()
                        _ = self.wfile.write(content)
                        self.wfile.flush()
                    finally:
                        delivery = change_page.finish_verdict_cleanup(saved.delivery, case.status)
                        change_page.deliver(delivery, case.status)
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
                        '--window-size',f'{width},1100','--screenshot',str(self.case.fixture.temporary/'repair.png'),
                        f'http://127.0.0.1:{server.server_port}'+place],capture_output=True,text=True,
                        timeout=probe_timeout+30)
                self.assertEqual(done.returncode,0,done.stderr)
            finally:
                server.shutdown()
                thread.join(5)
        self.assertEqual(len(results),1,'The Review probe must post one result.')
        return results[0]

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_owner_chips_count_reset_restore_and_refresh(self) -> None:
        _ = self.case.question('Which layout should we use?')
        _ = self.case.question('Which color should we use?')
        waiting = self.case.investigation(False)
        setup = """<script>
const nativeFetch=fetch;
window.fetch=(url,options)=>{
 const target=new URL(url,location.href);
 return nativeFetch(options?.method==='POST' || !target.search ? url : '/fresh'+target.search,options);
};
</script>"""
        markup = review_page.render(self.case.root).decode().replace('<head>', '<head>'+setup, 1)
        probe = r'''
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const chip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
 const count=value=>chip(value)?.querySelector('b')?.textContent;
 const pressed=value=>chip(value)?.getAttribute('aria-pressed')==='true';
 const address=()=>new URLSearchParams(location.hash.slice(1));
 const visible=()=>[...document.querySelectorAll('[data-owner-chip]:not([hidden])')];
 const navigate=async params=>{
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams(params);await changed;
 };
 try{
  const group=document.querySelector('[role="group"][aria-label="Owner"]');
  check(group?.firstElementChild===chip('') && pressed('') &&
   visible().every(button=>button.type==='button' && button.tabIndex===0 && button.querySelector('b')),
   'Owner uses native chips with All first and pressed.');
  const search=document.querySelector('#list-search');
  document.querySelector('[data-type-chip="question"]').click();
  check(count('')==='2' && count(PAGE_OWNER)==='2','Owner counts use the selected Type.');
  search.value='layout';search.dispatchEvent(new Event('input'));
  check(count('')==='1' && count(PAGE_OWNER)==='1','Owner counts use the words filter.');
  chip(PAGE_OWNER)?.click();
  check(pressed(PAGE_OWNER) && count('')==='1' && address().get('owner')===PAGE_OWNER,
   'An Owner chip sets the address and leaves the All count unchanged.');
  await navigate({owner:UNIT_OWNER,type:'investigation',show:'open'});
  check(pressed(UNIT_OWNER) && address().get('owner')===UNIT_OWNER,'The address restores the Owner chip.');
  check(pressed(UNIT_OWNER) && !chip(UNIT_OWNER)?.hidden && count(UNIT_OWNER)==='0' && count('')==='0',
   'The selected owner stays visible with zero in another Show.');
  const previous=document.querySelector('[data-review]');
  chip(UNIT_OWNER)?.focus();
  const refreshed=new Promise(resolve=>previous.addEventListener('review-refreshed',resolve,{once:true}));
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await refreshed;
  check(pressed(UNIT_OWNER) && count(UNIT_OWNER)==='0' && document.activeElement===chip(UNIT_OWNER),
   'Refresh keeps the selected Owner chip, its count and its focus.');
  const unitChip=chip(UNIT_OWNER);
  document.querySelector('[data-type-chip="question"]').click();
  check(pressed('') && !address().get('owner') && unitChip && unitChip===chip(UNIT_OWNER) && unitChip.hidden,
   'A Type without the selected owner resets to All and hides the same chip.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        probe = probe.replace('UNIT_OWNER', json.dumps(str(waiting.owner))).replace(
            'PAGE_OWNER', json.dumps(str(next(row.id for row in staff.snapshot().rows if row.name == 'page-owner'))))
        self.assertEqual(self.browser(markup, probe), [])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_legacy_source_owner_does_not_limit_owner_chip_counts(self) -> None:
        first = self.case.investigation()
        owner = fixture_member('showrunner', name='second-owner')
        second = investigation.file(
            owner.id, 'Why did the second gate stop?', 'Read the log.',
            waiting_why='The owner must complete the investigation.')
        _ = investigation.answer(second.owner, second.id, 'The repair is ready.')
        _ = self.case.investigation(False)
        probe = r'''
(()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const chip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
 const counts=()=>['',FIRST,SECOND].map(value=>chip(value)?.querySelector('b').textContent).join('|');
 const address=()=>new URLSearchParams(location.hash.slice(1));
 const rows=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')];
 check(chip(FIRST)?.getAttribute('aria-pressed')==='true' && address().get('source-owner')===FIRST &&
  rows().length===1 && rows()[0].dataset.sourceOwner===FIRST,'The legacy link keeps its selected owner and source.');
 check(counts()==='2|1|1','Expected Owner counts 2|1|1, got '+counts()+'.');
 chip(SECOND)?.click();
 check(chip(SECOND)?.getAttribute('aria-pressed')==='true' && !address().has('source-owner') &&
  rows().length===1 && rows()[0].dataset.sourceOwner===SECOND,'The second chip selects its matching row.');
 void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''.replace('FIRST', json.dumps(str(first.owner))).replace('SECOND', json.dumps(str(second.owner)))
        place = '/#'+urlencode({'legacy':'investigation', 'owner':first.owner, 'show':'not-read',
                                 'type':'investigation', 'q':'gate'})
        self.assertEqual(self.browser(test_review_page.browser_markup(self.case.root), probe, place), [])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_refresh_keeps_the_selected_owner_and_focus_without_matches(self) -> None:
        without_owner = test_review_page.browser_markup(self.case.root)
        ready = self.case.investigation()
        before = test_review_page.browser_markup(self.case.root)
        key = f'investigation/{ready.owner}/{ready.id}'
        until = datetime.now(user_zone()) + timedelta(days=1)
        body = self.case.body(key, 'later') + '&' + urlencode({'later':until.isoformat()})
        _ = review_page.submit(self.case.root, body, self.case.status)
        with_hidden_owner = test_review_page.browser_markup(self.case.root)
        setup = r"""<script>
const nativeFetch=fetch;
window.fetch=(url,options)=>options?.method==='POST' ? nativeFetch(url,options) :
 Promise.resolve(new Response(FRESH));
</script>"""
        probe = r'''
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const chip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
 const owner=chip(OWNER);
 owner.focus();
 check(document.activeElement===owner && owner.getAttribute('aria-pressed')==='true' &&
  owner.querySelector('b').textContent==='1','The selected Owner starts focused with one match.');
 const previous=document.querySelector('[data-review]');
 const refreshed=new Promise(resolve=>previous.addEventListener('review-refreshed',resolve,{once:true}));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await refreshed;
 const replacement=chip(OWNER),all=chip('');
 check(replacement?.getAttribute('aria-pressed')==='true' && !replacement.hidden &&
  replacement.querySelector('b').textContent==='0' && all.querySelector('b').textContent==='0' &&
  !document.querySelector('[data-list-item]:not([hidden])'),'Refresh keeps the selected Owner visible with zero matches.');
 check(new URLSearchParams(location.hash.slice(1)).get('owner')===OWNER,'The address keeps the selected Owner.');
 check(document.activeElement===replacement,'Refresh keeps focus on the selected Owner.');
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        place = '/#'+urlencode({'owner':ready.owner, 'show':'open', 'type':'investigation'})
        for reason, fresh in (('owner absent', without_owner), ('last row deferred', with_hidden_owner)):
            with self.subTest(reason=reason):
                script = setup.replace('FRESH', json.dumps(fresh).replace('</', '<\\/'))
                markup = before.replace('<head>', '<head>'+script, 1)
                check = probe.replace('OWNER', json.dumps(str(ready.owner)))
                self.assertEqual(self.browser(markup, check, place), [])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_refresh_focuses_selected_owner_when_another_chip_disappears(self) -> None:
        selected = self.case.investigation()
        fresh = test_review_page.browser_markup(self.case.root)
        other = fixture_member('showrunner', name='second-owner')
        _ = investigation.file(
            other.id, 'Why did the second gate stop?', 'Read the log.',
            waiting_why='The owner must complete the investigation.')
        setup = r"""<script>
const nativeFetch=fetch;
window.fetch=(url,options)=>options?.method==='POST' ? nativeFetch(url,options) :
 Promise.resolve(new Response(FRESH));
</script>""".replace('FRESH', json.dumps(fresh).replace('</', '<\\/'))
        markup = test_review_page.browser_markup(self.case.root).replace('<head>', '<head>'+setup, 1)
        probe = r'''
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const chip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
 const pressed=button=>button?.getAttribute('aria-pressed')==='true';
 chip(OTHER).focus();
 check(document.activeElement===chip(OTHER) && pressed(chip(SELECTED)) && !pressed(chip(OTHER)),
  'The other chip has focus while the selected chip stays pressed.');
 const previous=document.querySelector('[data-review]');
 const refreshed=new Promise(resolve=>previous.addEventListener('review-refreshed',resolve,{once:true}));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await refreshed;
 check(!chip(OTHER) && pressed(chip(SELECTED)) && new URLSearchParams(location.hash.slice(1)).get('owner')===SELECTED,
  'Refresh removes the other chip and keeps the selected owner.');
 check(document.activeElement===chip(SELECTED),
  'Expected focus on the pressed Owner chip, got '+(document.activeElement.id || document.activeElement.tagName)+'.');
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''.replace('SELECTED', json.dumps(str(selected.owner))).replace('OTHER', json.dumps(str(other.id)))
        place = '/#'+urlencode({'owner':selected.owner, 'show':'open', 'type':'investigation'})
        self.assertEqual(self.browser(markup, probe, place), [])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_index_refresh_and_owner_constraints(self) -> None:
        _ = self.case.question()
        ready = self.case.investigation()
        setup = '''<script>const nativeFetch=fetch;window.setInterval=()=>0;
window.fetch=(url,options={})=>nativeFetch(options.method==='POST'?'/action':'/fresh'+new URL(url,location.href).search,options);
</script>'''
        probe = r'''
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const nextView=()=>new Promise(resolve=>document.querySelector('[data-review]').addEventListener('review-refreshed',resolve,{once:true}));
 const refresh=async()=>{const loaded=nextView();dispatchEvent(new MessageEvent('message',{
  source:parent,origin:location.origin,data:'console-shown'}));await loaded;};
 const show=async value=>{
  const chip=document.querySelector('[data-show-chip="'+value+'"]');
  if(chip.getAttribute('aria-pressed')==='true') return;
  chip.click();await new Promise(resolve=>requestAnimationFrame(resolve));
 };
 const count=value=>Number(document.querySelector('[data-show-chip="'+value+'"] b').textContent);
 const ownerChip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
 const selectedOwner=()=>document.querySelector('[data-owner-chip][aria-pressed="true"]');
 const ownerChips=()=>[...document.querySelectorAll('[data-owner-chip]:not([hidden])')];
 const chooseOwner=value=>ownerChip(value).click();

 const type=value=>document.querySelector('[data-type-chip="'+value+'"]').click();
 const search=value=>{const field=document.querySelector('#list-search');field.value=value;field.dispatchEvent(new Event('input'));};
 const save=async form=>{
  const complete=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{if(!form.dataset.sending){observer.disconnect();resolve();}});
   observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
  });form.elements.feedback.value='Check the next case.';
  form.requestSubmit(form.querySelector('[value="question"]'));await complete;
 };
 try{
  check(count('Done')===0,'Done has no rows for these records.');
  for(const [id,selected] of [['C1','all'],['C2','Validating']]){
   await show('open');const before=count('open'),all=count('all');
   const key='page-owner/'+id,row=document.querySelector('[data-key="'+key+'"]');
   row.querySelector('.list-toggle').click();await save(await readyReviewCard(row));
   await show(selected);await nativeFetch('/owner-answer?item='+encodeURIComponent(key));await refresh();
   check(count('open')===before && count('all')===all,
    'F005: the fresh index restores the counts after an owner answer in '+selected);
   await show('open');
   check(!document.querySelector('[data-key="'+key+'"]').hidden,
    'F005: Open shows the owner answer after refresh from '+selected);
  }
  const navigated=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({legacy:'investigation',owner:'unknown',type:'investigation',show:'not-read'});
  await navigated;
  const address=new URLSearchParams(location.hash.slice(1));
  check(selectedOwner().dataset.ownerChip==='' && ownerChip('').textContent==='All 1' && count('open')===1 &&
   !address.get('owner') && !address.has('source-owner') &&
   document.querySelectorAll('[data-list-item]:not([hidden])').length===1,
   'F006: hash navigation clears every owner constraint before it counts');
  chooseOwner(UNIT_OWNER);await show('Validating');
  check(selectedOwner().dataset.ownerChip===UNIT_OWNER && selectedOwner().textContent===UNIT_NAME+' 0' &&
   count('open')===1 && count('Validating')===0,
   'F007: the owner stays selected when its rows match another state.');
  check(!ownerChip(UNIT_OWNER).hidden,
   'F007: Owner lists matching type and words across every Show');
  type('question');
  check(selectedOwner().dataset.ownerChip==='' && ownerChips().length===2 &&
   ownerChips()[1].dataset.ownerName==='page-owner' &&
   !new URLSearchParams(location.hash.slice(1)).get('owner'),
   'F007: an owner without the selected type resets to All and hides its chip');
  await show('open');type('investigation');chooseOwner(UNIT_OWNER);search('no matching title');
  const owner=ownerChip(UNIT_OWNER);
  owner.focus();
  await refresh();
  check(selectedOwner()===owner && document.activeElement===owner && ownerChips().length===2 &&
   owner.querySelector('b').textContent==='0' && ownerChip('').textContent==='All 0' && count('open')===0,
   'F007: refresh keeps the selected Owner and its focus when the search has no matches.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        probe = probe.replace('UNIT_OWNER', json.dumps(str(ready.owner))).replace(
            'UNIT_NAME', json.dumps(review_items.name(ready.owner, staff.snapshot())))
        for width in (1440, 390):
            with self.subTest(width=width):
                markup = review_page.render(self.case.root).decode().replace('<head>', '<head>'+setup, 1)
                self.assertEqual(self.browser(markup, probe, width=width), [])

    @unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
    def test_showrunner_check_stays_out_of_open_after_refresh_and_returns_only_for_a_problem(self) -> None:
        decisions = self.case.decisions()
        question = self.case.question()
        ready = self.case.investigation()
        markup = review_page.render(self.case.root).decode()
        setup = '''<script>const nativeFetch=fetch;window.setInterval=()=>0;
let allowReviewRefresh=Promise.resolve();
window.fetch=async(url,options={})=>{
 if(options.method!=='POST') await allowReviewRefresh;
 return nativeFetch(options.method==='POST'?'/action':'/fresh'+new URL(url,location.href).search,options);
};</script>'''
        markup = markup.replace('<head>','<head>'+setup,1)
        probe = r'''
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 const nextView=()=>new Promise(resolve=>document.querySelector('[data-review]').addEventListener('review-refreshed',resolve,{once:true}));
 const row=id=>document.querySelector('[data-key="page-owner/'+id+'"]');
 const refresh=async()=>{const loaded=nextView();dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await loaded;};
 const save=async(form,value)=>{
  const complete=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{if(!form.dataset.sending){observer.disconnect();resolve();}});
   observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
  });
  form.requestSubmit(form.querySelector('[value="'+value+'"]'));await complete;
 };
 const show=async value=>{
  const chip=document.querySelector('[data-show-chip="'+value+'"]');
  if(chip.getAttribute('aria-pressed')==='true') return;
  chip.click();await new Promise(resolve=>requestAnimationFrame(resolve));
 };
 try{
  row('C1').querySelector('.list-toggle').click();
  const count=value=>Number(document.querySelector('[data-show-chip="'+value+'"] b').textContent);
  const counts=()=>['open','Validating','all'].map(count);
  check(count('Done')===0,'Done has no rows for these records.');
  const before=counts();
  const form=await readyReviewCard(row('C1'));await save(form,'validate');
  check(JSON.stringify(counts())===JSON.stringify([before[0]-1,before[1]+1,before[2]]),
   'A save moves the entry from Open to Validating without changing All.');
  check(!row('C1') || row('C1').hidden,'the saved check leaves Open');
  await refresh();check(!row('C1') || row('C1').hidden,'a refresh keeps the active check out of Open');
  check(counts()[1]===before[1]+1 && counts()[2]===before[2],
   'F001: refresh replaces the counts with the saved state');
  await show('Validating');
  check(row('C1') && !row('C1').hidden && row('C1').dataset.state==='Validating',
   'Validating shows the saved check.');
  await show('all');
  check(!row('C1').hidden && row('C1').querySelector('button.list-toggle') &&
   row('C1').textContent.includes('With page-owner · Validating for'),
   'All shows the elapsed time and a button to open the active check.');
  await show('open');
  await nativeFetch('/problem');await refresh();check(!row('C1').hidden,'a problem returns to Open');
  row('C2').querySelector('.list-toggle').click();
  const second=await readyReviewCard(row('C2'));await save(second,'validate');
  const [openBeforePass,validatingBeforePass,allBeforePass]=counts();
  await nativeFetch('/passed');await refresh();check(!row('C2') || row('C2').hidden,'a passed check stays out of Open');
  const [openAfterPass,validatingAfterPass,allAfterPass]=counts();
  check(openAfterPass===openBeforePass && validatingAfterPass===validatingBeforePass-1 && allAfterPass===allBeforePass-1,
   'A passed check leaves the Validating and All counts.');
  await show('all');
  check(!row('C2') || row('C2').hidden,'A passed check stays out of All.');
  row('C3').querySelector('.list-toggle').click();
  const third=await readyReviewCard(row('C3'));
  const [openBeforeWorks,validatingBeforeWorks,allBeforeWorks]=counts();
  let resumeRefresh;
  allowReviewRefresh=new Promise(resolve=>{resumeRefresh=resolve;});
  try{
   await save(third,'works');
   check(!row('C3') || row('C3').hidden,'F010: Works leaves All');
   const [open,validating,all]=counts();
   check(open===openBeforeWorks-1 && validating===validatingBeforeWorks && all===allBeforeWorks-1,
    'F010: Save removes the completed item from the Open and All counts before refresh.');
   await show('open');
   check(!row('C3') || row('C3').hidden,'F010: Save removes the completed item from Open before refresh.');
  }finally{resumeRefresh();}
  for(let pass=0;pass<2;pass++){
   await refresh();
   for(const choice of ['open','Validating','all']){
    await show(choice);check(!row('C3') || row('C3').hidden,'F010: refresh keeps Works hidden in '+choice);
   }
   const [open,validating,all]=counts();
   check(open===openBeforeWorks-1 && validating===validatingBeforeWorks && all===allBeforeWorks-1,
    'F010: refresh keeps completed work out of the counts.');
  }
  const keys=QUESTION_KEYS;
  for(const key of keys){
   await show('open');
   const asked=()=>document.querySelector('[data-key="'+key+'"]');
   asked().querySelector('.list-toggle').click();
   const asking=await readyReviewCard(asked());asking.elements.feedback.value='Check the next case.';
   const openBeforeAsk=count('open');
   await save(asking,'question');
   check(count('open')===openBeforeAsk-1,'F021: Ask question reduces the Open count for '+key);
   // A change or proposal Ask keeps its row open; any other row that stays open fails below.
   if(STAYS_OPEN.includes(key)) asked().querySelector('.list-toggle').click();
   check(!asked() || asked().hidden,'F021: Ask question leaves Open for '+key);
   for(let pass=0;pass<2;pass++){
    await refresh();check(!asked() || asked().hidden,'F021: the sent request stays out of Open after refresh for '+key);
   }
   await nativeFetch('/owner-answer?item='+encodeURIComponent(key));await refresh();
   const returnedState='Open';
   check(!asked().hidden && asked().dataset.state===returnedState,'F021: the owner answer returns '+key);
  }
  location.hash=new URLSearchParams({owner:UNIT_OWNER,show:'open',type:'investigation'});
  await new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  const investigation=()=>document.querySelector('[data-key="'+keys.at(-1)+'"]');
  const ownerChip=value=>document.querySelector('[data-owner-chip="'+value+'"]');
  const selectedOwner=()=>document.querySelector('[data-owner-chip][aria-pressed="true"]');
  check(selectedOwner()===ownerChip(UNIT_OWNER),'The investigation owner starts selected.');
  investigation().querySelector('.list-toggle').click();
  await readyReviewCard(investigation());
  const latest=()=>recordDetails(investigation())?.textContent.includes('The owner checked the next case.');
  const deadline=performance.now()+8000;
  while(!latest() && performance.now()<deadline) await new Promise(resolve=>requestAnimationFrame(resolve));
  check(latest(),'The row shows the current answer before marking it read.');
  await save(await readyReviewCard(investigation()),'read');
  await refresh();
  check(selectedOwner().dataset.ownerChip===UNIT_OWNER && selectedOwner().textContent===UNIT_NAME+' 0' &&
    ownerChip('').textContent==='All 0' && counts()[0]===0,
   'Review keeps the selected Owner with zero after its last Open row disappears.');
  check(!document.querySelector('[data-list-item]:not([hidden])'),'the selected type and Show still have no rows');
  await show('all');
  check(ownerChip('').textContent==='All 0' && counts().every(count=>count===0) &&
    !document.querySelector('[data-list-item]:not([hidden])'),
   'All excludes the completed investigation from the rows and counts.');
 }catch(error){failures.push(String(error));}
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''
        probe = probe.replace('QUESTION_KEYS',json.dumps([question,'page-owner/C1',*decisions,f'investigation/{ready.owner}/{ready.id}']))
        probe = probe.replace('STAYS_OPEN', json.dumps(['page-owner/C1', decisions[0]]))
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
        _ = investigation.file(owner.id, 'Another owner', 'Read its log.', waiting_why='The owner must complete the investigation.')
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
  check(frame.contentDocument.querySelector('[data-owner-chip][aria-pressed="true"]').dataset.ownerChip===OWNER,'the owner name resolves to its staff ID');
  const cases=CASES;
  for(const entry of cases){
   const legacy=document.createElement('iframe');legacy.src=entry.url;
   const loaded=new Promise(resolve=>legacy.addEventListener('load',resolve,{once:true}));document.body.append(legacy);await loaded;
   const page=legacy.contentDocument;await loadedView(page,entry.show);
   check(page.querySelector('[data-show-chip][aria-pressed="true"]').dataset.showChip===entry.show,'F012: legacy Show '+entry.url);
   const visible=[...page.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
   check(visible.length===entry.count && visible.every(row=>row.dataset.type===entry.type),'F012: legacy records '+entry.url);
   if(entry.owner) check(page.querySelector('[data-owner-chip][aria-pressed="true"]').dataset.ownerChip===entry.owner &&
    visible.every(row=>row.dataset.sourceOwner===entry.owner),'F012: the legacy link keeps its owner.');
   if(entry.type==='investigation') check(page.querySelector('[data-show-chip="Done"] b')?.textContent==='0' &&
    !page.querySelector('[data-state="Done"]'),
    'F012: the legacy link shows only active states.');
   if(entry.item){
    const item=page.querySelector('[data-list-item][data-key="'+entry.item+'"]');
    const summary=item?.querySelector('.list-toggle'),detail=item?.querySelector('[data-detail]');
    const owner=page.querySelector('[data-owner-chip][aria-pressed="true"]');
    check(summary?.getAttribute('aria-expanded')==='true' && detail && !detail.hidden &&
     owner?.dataset.ownerChip===item.dataset.owner,'F012: the legacy row opens in its row with the review owner kept.');
   }
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
                      'show':new,'count':count,'type':'investigation','item':'','owner':str(ready.owner)}
                     for old,new,count in (('not-read','open',1),('answer ready','open',1),
                                           ('open','Waiting',1),('read','open',1),('all','all',2)))
        cases.extend(({'url':'/console/investigations.html#'+urlencode({'owner':owner.id,'show':'all'}),
                       'show':'all','count':1,'type':'investigation','item':'','owner':str(owner.id)},
                      {'url':'/console/investigations.html#'+urlencode({'owner':ready.owner}),
                       'show':'open','count':1,'type':'investigation','item':'','owner':str(ready.owner)},
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
  const count=value=>frame.contentDocument.querySelector('[data-show-chip="'+value+'"] b')?.textContent;
  const deadline=performance.now()+8000;
  while(!count('open') || !count('all')){
   if(performance.now()>deadline) throw new Error('F012: Review did not populate the filtered counts.');
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
  const page=frame.contentDocument;
  const place=new URLSearchParams(frame.contentWindow.location.hash.slice(1));
  const rows=[...page.querySelectorAll('[data-list-item]')].filter(row=>!row.hidden);
  if(place.get('show')!=='open' || place.get('source-owner')!==INVESTIGATION_OWNER ||
   page.querySelector('[data-show-chip][aria-pressed="true"]').dataset.showChip!=='open' ||
   page.querySelector('[data-owner-chip][aria-pressed="true"]').dataset.ownerChip!==INVESTIGATION_OWNER ||
   rows.length!==1 || rows[0].dataset.key!==READY_KEY || rows[0].dataset.sourceOwner!==INVESTIGATION_OWNER ||
   page.querySelector('[data-key="'+READ_KEY+'"],[data-state="Done"]') ||
   count('open')!=='1' || count('all')!=='2' || count('Done')!=='0')
   failures.push('F012: the Read alias opens Open for its owner and excludes the completed record.');
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();'''.replace('INVESTIGATION_OWNER', json.dumps(str(ready.owner))).replace(
            'READY_KEY', json.dumps(f'investigation/{ready.owner}/{ready.id}')).replace(
            'READ_KEY', json.dumps(f'investigation/{read.owner}/{read.id}'))
        place = '/?'+urlencode({'page':'investigations','at.investigations':'/console/investigations.html#'+urlencode({'owner':ready.owner,'show':'read'})})
        self.assertEqual(self.browser(markup, alias_probe, place), [])


if __name__ == '__main__':
    _ = unittest.main()
